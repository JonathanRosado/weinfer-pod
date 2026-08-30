#!/usr/bin/env python3
"""Zero-GPU regression for the image's immutable runtime/profile contract."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shlex
import tempfile
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
    contract_path = RUNTIME / "runtime-contract.json"
    contract = json.loads(contract_path.read_text())
    contract_sha256 = hashlib.sha256(contract_path.read_bytes()).hexdigest()
    profiles = contract["profiles"]

    assert "FROM vllm/vllm-openai@sha256:d8d39b59" in dockerfile
    assert "FROM vllm/vllm-openai:v0.11.0" not in dockerfile
    assert "992017d193dfbbc62e67401a6d5416629bf90b640872d14b7863de45e9371446" in dockerfile
    assert "ARG WEINFER_RUNTIME_CONTRACT_SHA256" in dockerfile
    assert "/weinfer/runtime/runtime-contract.json | sha256sum -c -" in dockerfile
    assert (
        'LABEL ai.weinfer.runtime-contract-sha256="${WEINFER_RUNTIME_CONTRACT_SHA256}"'
        in dockerfile
    )
    assert len(contract_sha256) == 64
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
        "--max-num-seqs": "8",
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

    aot = load_module("build_flashinfer_aot", RUNTIME / "build_flashinfer_aot.py")
    assert len(aot.TARGET_SOURCE_SUFFIXES) == 14
    assert aot.TARGET_SOURCE_SUFFIXES[-1] == (
        "weinfer_exact_sm90_bf16_mxfp4.generated.cu"
    )
    assert (
        aot.UPSTREAM_FP8_BLOCKSCALE_LINK_STUB_SOURCE
        in aot.TARGET_STATIC_SOURCE_SUFFIXES
    )
    assert (
        verifier.EXPECTED_UPSTREAM_FP8_BLOCKSCALE_LINK_STUB_SOURCE
        == aot.UPSTREAM_FP8_BLOCKSCALE_LINK_STUB_SOURCE
    )
    assert (
        verifier.EXPECTED_UPSTREAM_FP8_BLOCKSCALE_LINK_STUB_SHA256
        == aot.UPSTREAM_FP8_BLOCKSCALE_LINK_STUB_SHA256
        == "aae25878e2520693265c46e073b74f9a8f4f7daa351fc80b6f9a7fc8227c4257"
    )
    forbidden = ("fp8", "fp16", "uint4", "uint8", "fp32")
    for suffix in aot.TARGET_STATIC_SOURCE_SUFFIXES:
        if suffix == aot.UPSTREAM_FP8_BLOCKSCALE_LINK_STUB_SOURCE:
            continue
        assert not any(value in Path(suffix).name for value in forbidden), suffix
    assert aot.TARGET_TACTIC.endswith(
        "1x1x1_warpspecialized_pingpong_epi_tma"
    )
    assert verifier.EXPECTED_FUSED_MOE_TACTIC == aot.TARGET_TACTIC
    assert verifier.EXPECTED_FUSED_MOE_SOURCE_SUFFIXES == list(
        aot.TARGET_SOURCE_SUFFIXES
    )
    assert verifier.EXPECTED_HEADER_COMPATIBILITY_DEFINES == list(
        aot.HEADER_COMPATIBILITY_DEFINES
    )
    assert (
        verifier.EXPECTED_UPSTREAM_SHARED_BINDING_SOURCE
        == aot.UPSTREAM_SHARED_BINDING_SOURCE
    )
    assert verifier.EXPECTED_RUNTIME_BINDING == aot.RUNTIME_BINDING

    class SyntheticOperation:
        def __init__(self, name: str):
            self.name = name

        def __repr__(self) -> str:
            return self.name

    target = SyntheticOperation(aot.TARGET_TACTIC)
    wrong = SyntheticOperation(aot.TARGET_TACTIC.replace("bf16", "fp16", 1))
    assert aot.select_target_operation([wrong, target]) is target
    for operations in ([wrong], [target, SyntheticOperation(aot.TARGET_TACTIC)]):
        try:
            aot.select_target_operation(operations)
            raise AssertionError("non-unique AOT tactic selection passed")
        except RuntimeError as exc:
            assert "expected one exact SM90 BF16/MXFP4 tactic" in str(exc)

    class SyntheticSpec:
        def __init__(self, ninja_path: Path) -> None:
            self.name = "fused_moe_90"
            self.ninja_path = ninja_path
            self.sources: list[Path] = []
            self.extra_cuda_cflags = [
                "-DCOMPILE_HOPPER_TMA_GEMMS",
                "-DCOMPILE_HOPPER_TMA_GROUPED_GEMMS",
                "-DENABLE_BF16",
                "-DENABLE_FP8",
                "-DENABLE_FP4",
                "-DUSING_OSS_CUTLASS_MOE_GEMM",
            ]
            self.write_count = 0
            self.written_sources: tuple[Path, ...] = ()
            self.written_cuda_flags: tuple[str, ...] = ()

        def write_ninja(self) -> None:
            self.write_count += 1
            self.written_sources = tuple(self.sources)
            self.written_cuda_flags = tuple(self.extra_cuda_cflags)
            lines = [
                "cflags = -DFAST_BUILD",
                "cuda_cflags = " + " ".join(self.extra_cuda_cflags),
            ]
            lines.extend(
                f"build $name/{source.stem}.o: "
                f"{'cuda_compile' if source.suffix == '.cu' else 'compile'} "
                f"{source.resolve()}"
                for source in self.sources
            )
            self.ninja_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    selected = [Path("/flashinfer") / suffix for suffix in aot.TARGET_SOURCE_SUFFIXES]
    with tempfile.TemporaryDirectory() as temporary:
        synthetic_spec = SyntheticSpec(Path(temporary) / "build.ninja")
        assert aot.reemit_narrow_fused_moe_spec(synthetic_spec, selected) is synthetic_spec
        assert synthetic_spec.write_count == 1
        assert synthetic_spec.written_sources == tuple(selected)
        assert synthetic_spec.written_cuda_flags.count("-DENABLE_FP8") == 1
        assert "-DCOMPILE_HOPPER_TMA_GEMMS" not in synthetic_spec.written_cuda_flags
        assert "-DCOMPILE_HOPPER_TMA_GROUPED_GEMMS" in synthetic_spec.written_cuda_flags
        graph = aot.verify_emitted_ninja(synthetic_spec, selected)
        assert graph["source_count"] == 14
        assert len(graph["ninja_sha256"]) == 64
        good_ninja = synthetic_spec.ninja_path.read_text()
        synthetic_spec.ninja_path.write_text(
            good_ninja.replace("-DENABLE_FP8", ""),
            encoding="utf-8",
        )
        try:
            aot.verify_emitted_ninja(synthetic_spec, selected)
            raise AssertionError("missing FP4 header compatibility define passed")
        except RuntimeError as exc:
            assert "lost ENABLE_FP8 header compatibility define" in str(exc)
        synthetic_spec.ninja_path.write_text(
            good_ninja + "build $name/stale.o: cuda_compile /flashinfer/stale.cu\n",
            encoding="utf-8",
        )
        try:
            aot.verify_emitted_ninja(synthetic_spec, selected)
            raise AssertionError("stale full Ninja graph passed")
        except RuntimeError as exc:
            assert "expected 14, observed 15" in str(exc)
        try:
            aot.reemit_narrow_fused_moe_spec(synthetic_spec, selected[:-1])
            raise AssertionError("incomplete narrow source set passed")
        except RuntimeError as exc:
            assert "narrow FlashInfer AOT source set drift" in str(exc)

    class SyntheticLoadSpec:
        name = "fused_moe_90"

        def __init__(self) -> None:
            self.calls = []

        def load(self, path: Path, class_name: str | None = None):
            self.calls.append((path, class_name))
            return object()

    synthetic_load = SyntheticLoadSpec()
    synthetic_library = Path("/flashinfer/aot/fused_moe_90.so")
    assert (
        aot.validate_fused_moe_aot_load(synthetic_load, synthetic_library)
        == "torch.classes.FusedMoeRunner"
    )
    assert synthetic_load.calls == [(synthetic_library, "FusedMoeRunner")]
    synthetic_load.name = "sampling"
    try:
        aot.validate_fused_moe_aot_load(synthetic_load, synthetic_library)
        raise AssertionError("wrong AOT operator passed fused-MoE load validation")
    except RuntimeError as exc:
        assert "wrong operator" in str(exc)

    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        operator = workspace / "cached_ops" / "fused_moe_90"
        operator.mkdir(parents=True)
        graph_path = operator / "build.ninja"
        graph_path.write_text("rule cuda_compile\n", encoding="utf-8")
        assert verifier.scan_runtime_workspace(workspace) == [
            {
                "path": "cached_ops/fused_moe_90/build.ninja",
                "sha256": hashlib.sha256(graph_path.read_bytes()).hexdigest(),
            }
        ]

    for relative in (
        "cached_ops/fused_moe_90/fused_moe_90.so",
        "cached_ops/fused_moe_90/kernel.o",
        "cached_ops/fused_moe_90/kernel.ptx",
        "cached_ops/.ninja_log",
    ):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            artifact = workspace / relative
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_bytes(b"compiled")
            try:
                verifier.scan_runtime_workspace(workspace)
                raise AssertionError(f"runtime compiled artifact passed: {relative}")
            except RuntimeError as exc:
                assert relative in str(exc)

    waiter = load_module("wait_for_engine", RUNTIME / "wait_for_engine.py")
    assert waiter.pid_alive(os.getpid()) is True
    assert waiter.pid_alive(2**31 - 1) is False

    print(
        "PASS: pinned base + exact profiles + H100 argv + one SM90 BF16/MXFP4 "
        "AOT tactic + no-JIT transform + fail-closed profile gate"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
