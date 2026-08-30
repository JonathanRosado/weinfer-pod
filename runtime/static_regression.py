#!/usr/bin/env python3
"""Zero-GPU regression for the image's immutable runtime/profile contract."""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def flag_value(tokens: list[str], flag: str) -> str:
    assert tokens.count(flag) == 1, (flag, tokens)
    index = tokens.index(flag)
    assert index + 1 < len(tokens), (flag, tokens)
    return tokens[index + 1]


def main() -> int:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    entrypoint = (ROOT / "entrypoint.sh").read_text(encoding="utf-8")
    contract = json.loads((RUNTIME / "runtime-contract.json").read_text())
    profiles = contract["profiles"]

    assert "FROM vllm/vllm-openai@sha256:d8d39b59" in dockerfile
    assert "FROM vllm/vllm-openai:v0.11.0" not in dockerfile
    assert "992017d193dfbbc62e67401a6d5416629bf90b640872d14b7863de45e9371446" in dockerfile
    for executable in (
        "install_flashinfer.py",
        "apply_vllm_h100_backport.sh",
        "apply_flashinfer_no_jit.py",
        "build_flashinfer_aot.py",
        "verify_runtime.py --static",
    ):
        assert executable in dockerfile, executable

    assert set(profiles) == {"gpt-oss-120b-h100-v1", "qwen7b-consumer-v1"}
    h100 = profiles["gpt-oss-120b-h100-v1"]
    assert h100["model"] == "openai/gpt-oss-120b"
    assert h100["model_revision"] == "b5c939de8f754692c1647ca79fbf85e8c1e70f8a"
    assert h100["tokenizer_revision"] == h100["model_revision"]
    assert h100["max_context_tokens"] == 131072
    assert h100["cuda_class"] == "12"
    assert h100["engine_ready_timeout_seconds"] == 1200
    assert h100["max_provider_rate_micro_per_hour"] == 2_700_000
    assert (
        h100["engine_ready_timeout_seconds"]
        * h100["max_provider_rate_micro_per_hour"]
        / 3_600_000_000
        == 0.9
    )
    qwen = profiles["qwen7b-consumer-v1"]
    assert qwen["engine_ready_timeout_seconds"] == 3600
    assert qwen["max_provider_rate_micro_per_hour"] == 400_000
    assert (
        qwen["engine_ready_timeout_seconds"]
        * qwen["max_provider_rate_micro_per_hour"]
        / 3_600_000_000
        == 0.4
    )
    h100_args = shlex.split(h100["vllm_canonical_args"])
    expected_values = {
        "--max-num-batched-tokens": "8192",
        "--max-num-seqs": "4",
        "--gpu-memory-utilization": "0.95",
        "--dtype": "bfloat16",
        "--kv-cache-dtype": "fp8",
        "--tensor-parallel-size": "1",
        "--served-model-name": h100["model"],
        "--revision": h100["model_revision"],
        "--tokenizer-revision": h100["tokenizer_revision"],
        "--max-model-len": "131072",
    }
    for flag, value in expected_values.items():
        assert flag_value(h100_args, flag) == value
    for flag in (
        "--calculate-kv-scales",
        "--enable-chunked-prefill",
        "--enable-prefix-caching",
    ):
        assert h100_args.count(flag) == 1
    ignore = h100_args.index("--ignore-patterns")
    assert h100_args[ignore + 1 : ignore + 3] == ["original/*", "metal/*"]

    assert 'case "${WEINFER_SERVING_PROFILE:-}"' in entrypoint
    assert '"${VLLM_ARGS[@]}" &' in entrypoint
    assert "${VLLM_EXTRA_ARGS:-} &" not in entrypoint
    assert "wait_for_engine.py" in entrypoint
    assert "ENGINE_READY_TIMEOUT_SECONDS=1200" in entrypoint
    assert "ENGINE_READY_TIMEOUT_SECONDS=3600" in entrypoint
    assert "verify_runtime.py --post-engine" in entrypoint
    assert entrypoint.index("verify_runtime.py --post-engine") < entrypoint.index(
        'echo "[entrypoint] starting the pull worker"'
    )
    for value in (
        "FLASHINFER_DISABLE_JIT=1",
        "VLLM_ATTENTION_BACKEND=FLASH_ATTN",
        "VLLM_FLASH_ATTN_VERSION=3",
        "VLLM_USE_FLASHINFER_MOE_MXFP4_BF16=1",
    ):
        assert value in entrypoint

    # Exercise the runtime profile gate itself. No installed vLLM/FlashInfer
    # package is imported by this pure branch.
    verifier = load_module("verify_runtime", RUNTIME / "verify_runtime.py")
    names = {
        "WEINFER_SERVING_PROFILE",
        "WEINFER_SERVED_MODEL",
        "WEINFER_MODEL_REVISION",
        "WEINFER_TOKENIZER_REVISION",
        "WEINFER_BACKEND_MAX_CONTEXT",
        "VLLM_EXTRA_ARGS",
    }
    saved = {name: os.environ.get(name) for name in names}
    try:
        for name in names:
            os.environ.pop(name, None)
        try:
            verifier.verify_profile(contract)
            raise AssertionError("missing profile passed")
        except RuntimeError as exc:
            assert "WEINFER_SERVING_PROFILE is required" in str(exc)

        os.environ["WEINFER_SERVING_PROFILE"] = "typo"
        try:
            verifier.verify_profile(contract)
            raise AssertionError("unknown profile passed")
        except RuntimeError as exc:
            assert "unknown WEINFER_SERVING_PROFILE" in str(exc)

        os.environ.update(
            {
                "WEINFER_SERVING_PROFILE": "gpt-oss-120b-h100-v1",
                "WEINFER_SERVED_MODEL": h100["model"],
                "WEINFER_MODEL_REVISION": h100["model_revision"],
                "WEINFER_TOKENIZER_REVISION": h100["tokenizer_revision"],
                "WEINFER_BACKEND_MAX_CONTEXT": str(h100["max_context_tokens"]),
                "VLLM_EXTRA_ARGS": h100["vllm_canonical_args"],
            }
        )
        assert verifier.verify_profile(contract)[0] == "gpt-oss-120b-h100-v1"
        os.environ["WEINFER_BACKEND_MAX_CONTEXT"] = "8192"
        try:
            verifier.verify_profile(contract)
            raise AssertionError("context mismatch passed")
        except RuntimeError as exc:
            assert "contradicts serving profile" in str(exc)
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    no_jit = load_module("apply_flashinfer_no_jit", RUNTIME / "apply_flashinfer_no_jit.py")
    synthetic = b"".join(old for old, _ in no_jit.REPLACEMENTS)
    for old, new in no_jit.REPLACEMENTS:
        assert synthetic.count(old) == 1
        synthetic = synthetic.replace(old, new)
    assert synthetic.count(b"FLASHINFER_DISABLE_JIT") == 3

    waiter = load_module("wait_for_engine", RUNTIME / "wait_for_engine.py")
    assert waiter.pid_alive(os.getpid()) is True
    assert waiter.pid_alive(2**31 - 1) is False

    print("PASS: pinned base + exact profiles + H100 argv + no-JIT transform + fail-closed profile gate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
