#!/usr/bin/env python3
"""Verify immutable image bytes and, on a pod, the resolved H100 runtime."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import subprocess
import sys
import sysconfig
from pathlib import Path
from typing import Any


RUNTIME_ROOT = Path("/weinfer/runtime")
CONTRACT_PATH = RUNTIME_ROOT / "runtime-contract.json"
AOT_MANIFEST_PATH = RUNTIME_ROOT / "flashinfer-aot-manifest.json"
ATTESTATION_PATH = Path("/tmp/weinfer-runtime-attestation.json")
POST_ENGINE_ATTESTATION_PATH = Path(
    "/tmp/weinfer-runtime-post-engine-attestation.json"
)

VLLM_VERSION = "0.11.0"
VLLM_BACKPORT_COMMIT = "c42ff4f4fdc4a4d48ccef18b8067995f6c19e6ec"
VLLM_PATCH_SHA256 = "69e6909b439a45baf68ea9fe02f5ca208aea5aa62e1eaf4e559f26a55378f1ad"
VLLM_SOURCE_SHA256 = "a8a13a30446f621a190674663e46c00a1e49175ce5591c1b05aaa79bab888567"
FLASHINFER_VERSION = "0.3.1"
FLASHINFER_SOURCE_SHA256 = "dbca0c0c36d4fd2f559021b5d9c356681501b14a3cf2ec3d37d53d17527dcc7b"
H100_CANONICAL_ARGS = (
    "--seed 0 --max-num-batched-tokens 8192 --max-num-seqs 4 "
    "--gpu-memory-utilization 0.95 --enable-chunked-prefill "
    "--enable-prefix-caching --dtype bfloat16 --kv-cache-dtype fp8 "
    "--calculate-kv-scales --tensor-parallel-size 1 "
    "--served-model-name openai/gpt-oss-120b "
    "--ignore-patterns original/* metal/* "
    "--revision b5c939de8f754692c1647ca79fbf85e8c1e70f8a "
    "--tokenizer-revision b5c939de8f754692c1647ca79fbf85e8c1e70f8a "
    "--max-model-len 131072"
)
QWEN_CANONICAL_ARGS = (
    "--seed 0 --max-num-batched-tokens 16384 --max-num-seqs 256 "
    "--gpu-memory-utilization 0.92 --enable-chunked-prefill "
    "--enable-prefix-caching "
    "--revision a09a35458c702b33eeacc393d103063234e8bc28 "
    "--tokenizer-revision a09a35458c702b33eeacc393d103063234e8bc28 "
    "--max-model-len 8192"
)
EXPECTED_PROFILES = {"gpt-oss-120b-h100-v1", "qwen7b-consumer-v1"}
EXPECTED_OPERATORS = {
    "fused_moe_90": 100_000_000,
    "sampling": 1_000_000,
    "trtllm_utils": 100_000,
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f"required image record missing or symlinked: {path.name}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"required image record is not an object: {path.name}")
    return value


def require_keys(value: dict[str, Any], keys: set[str], label: str) -> None:
    if set(value) != keys:
        raise RuntimeError(f"{label} schema drift")


def verify_contract() -> dict[str, Any]:
    contract = read_json(CONTRACT_PATH)
    require_keys(contract, {"object", "profiles"}, "runtime contract")
    if contract["object"] != "weinfer_pod_runtime_contract_v1":
        raise RuntimeError("runtime contract object drift")
    profiles = contract["profiles"]
    if not isinstance(profiles, dict) or set(profiles) != EXPECTED_PROFILES:
        raise RuntimeError("runtime contract profile set drift")
    h100 = profiles["gpt-oss-120b-h100-v1"]
    require_keys(
        h100,
        {
            "cuda_class",
            "engine_ready_timeout_seconds",
            "max_context_tokens",
            "max_provider_rate_micro_per_hour",
            "model",
            "model_revision",
            "required_hardware",
            "tokenizer_revision",
            "vllm_canonical_args",
        },
        "H100 profile",
    )
    if (
        h100["model"] != "openai/gpt-oss-120b"
        or h100["model_revision"]
        != "b5c939de8f754692c1647ca79fbf85e8c1e70f8a"
        or h100["tokenizer_revision"] != h100["model_revision"]
        or h100["max_context_tokens"] != 131072
        or h100["cuda_class"] != "12"
        or h100["engine_ready_timeout_seconds"] != 1200
        or h100["max_provider_rate_micro_per_hour"] != 2700000
        or h100["vllm_canonical_args"] != H100_CANONICAL_ARGS
    ):
        raise RuntimeError("H100 profile authority drift")
    # A never-ready engine must lose to the transaction's $1 GPU ceiling.
    # At the profile's maximum admitted rate, 1,200s costs exactly $0.90.
    if (
        h100["engine_ready_timeout_seconds"]
        * h100["max_provider_rate_micro_per_hour"]
        > 900_000 * 3600
    ):
        raise RuntimeError("H100 readiness wait exceeds its $0.90 sub-ceiling")
    qwen = profiles["qwen7b-consumer-v1"]
    require_keys(
        qwen,
        {
            "engine_ready_timeout_seconds",
            "max_context_tokens",
            "max_provider_rate_micro_per_hour",
            "model",
            "model_revision",
            "tokenizer_revision",
            "vllm_canonical_args",
        },
        "Qwen profile",
    )
    if (
        qwen["model"] != "Qwen/Qwen2.5-7B-Instruct"
        or qwen["model_revision"]
        != "a09a35458c702b33eeacc393d103063234e8bc28"
        or qwen["tokenizer_revision"] != qwen["model_revision"]
        or qwen["max_context_tokens"] != 8192
        or qwen["engine_ready_timeout_seconds"] != 3600
        or qwen["max_provider_rate_micro_per_hour"] != 400000
        or qwen["vllm_canonical_args"] != QWEN_CANONICAL_ARGS
    ):
        raise RuntimeError("Qwen profile authority drift")
    return contract


def verify_static() -> dict[str, Any]:
    contract = verify_contract()
    if importlib.metadata.version("vllm") != VLLM_VERSION:
        raise RuntimeError("vLLM package version drift")
    if importlib.metadata.version("flashinfer-python") != FLASHINFER_VERSION:
        raise RuntimeError("FlashInfer package version drift")

    purelib = Path(sysconfig.get_paths()["purelib"])
    vllm_source = purelib / "vllm" / "attention" / "layer.py"
    vllm_marker_path = vllm_source.parent / ".weinfer-kv-scale-backport.json"
    if not vllm_source.is_file() or vllm_source.is_symlink():
        raise RuntimeError("vLLM attention source missing or symlinked")
    vllm_marker = read_json(vllm_marker_path)
    require_keys(
        vllm_marker,
        {"patch_sha256", "source_sha256", "upstream_commit"},
        "vLLM backport marker",
    )
    if vllm_marker != {
        "patch_sha256": VLLM_PATCH_SHA256,
        "source_sha256": VLLM_SOURCE_SHA256,
        "upstream_commit": VLLM_BACKPORT_COMMIT,
    } or sha256(vllm_source) != VLLM_SOURCE_SHA256:
        raise RuntimeError("vLLM backport source/marker drift")

    flashinfer_root = purelib / "flashinfer"
    flashinfer_source = flashinfer_root / "jit" / "core.py"
    flashinfer_marker_path = flashinfer_source.parent / ".weinfer-no-runtime-jit.json"
    if not flashinfer_source.is_file() or flashinfer_source.is_symlink():
        raise RuntimeError("FlashInfer JIT source missing or symlinked")
    flashinfer_marker = read_json(flashinfer_marker_path)
    require_keys(
        flashinfer_marker,
        {"object", "policy", "preimage_sha256", "source_sha256"},
        "FlashInfer no-JIT marker",
    )
    if (
        flashinfer_marker["object"] != "weinfer_flashinfer_no_runtime_jit_v1"
        or flashinfer_marker["source_sha256"] != FLASHINFER_SOURCE_SHA256
        or sha256(flashinfer_source) != FLASHINFER_SOURCE_SHA256
    ):
        raise RuntimeError("FlashInfer no-JIT source/marker drift")

    manifest = read_json(AOT_MANIFEST_PATH)
    require_keys(
        manifest,
        {
            "build_mode",
            "cuda_arch",
            "flashinfer_python_version",
            "object",
            "operators",
        },
        "FlashInfer AOT manifest",
    )
    if (
        manifest["object"] != "weinfer_flashinfer_aot_manifest_v1"
        or manifest["build_mode"] != "image_build_aot_no_runtime_compilation"
        or manifest["cuda_arch"] != "9.0"
        or manifest["flashinfer_python_version"] != FLASHINFER_VERSION
    ):
        raise RuntimeError("FlashInfer AOT manifest authority drift")
    rows = manifest["operators"]
    if not isinstance(rows, list) or len(rows) != len(EXPECTED_OPERATORS):
        raise RuntimeError("FlashInfer AOT operator count drift")
    observed_names: set[str] = set()
    expected_paths: set[Path] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("FlashInfer AOT row is not an object")
        require_keys(row, {"bytes", "name", "path", "sha256"}, "FlashInfer AOT row")
        name = row["name"]
        if name not in EXPECTED_OPERATORS or name in observed_names:
            raise RuntimeError("FlashInfer AOT operator identity drift")
        observed_names.add(name)
        expected_path = flashinfer_root / "data" / "aot" / name / f"{name}.so"
        expected_paths.add(expected_path.resolve())
        if row["path"] != str(expected_path):
            raise RuntimeError("FlashInfer AOT manifest path drift")
        if not expected_path.is_file() or expected_path.is_symlink():
            raise RuntimeError(f"FlashInfer AOT operator missing: {name}")
        if row["bytes"] != expected_path.stat().st_size:
            raise RuntimeError(f"FlashInfer AOT size drift: {name}")
        if row["bytes"] < EXPECTED_OPERATORS[name]:
            raise RuntimeError(f"FlashInfer AOT operator implausibly small: {name}")
        if row["sha256"] != sha256(expected_path):
            raise RuntimeError(f"FlashInfer AOT digest drift: {name}")
    if observed_names != set(EXPECTED_OPERATORS):
        raise RuntimeError("FlashInfer AOT operator set drift")
    actual_paths = {
        path.resolve()
        for path in (flashinfer_root / "data" / "aot").rglob("*.so")
        if path.is_file()
    }
    if actual_paths != expected_paths:
        raise RuntimeError("unregistered FlashInfer AOT shared object present")

    return {
        "aot_manifest_sha256": sha256(AOT_MANIFEST_PATH),
        "flashinfer_no_jit_source_sha256": FLASHINFER_SOURCE_SHA256,
        "profiles": sorted(contract["profiles"]),
        "vllm_backport_source_sha256": VLLM_SOURCE_SHA256,
    }


def required_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def verify_profile(contract: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    profile_name = required_env("WEINFER_SERVING_PROFILE")
    profiles = contract["profiles"]
    if profile_name not in profiles:
        raise RuntimeError("unknown WEINFER_SERVING_PROFILE")
    profile = profiles[profile_name]
    for name in ("engine_ready_timeout_seconds", "max_provider_rate_micro_per_hour"):
        if not isinstance(profile[name], int) or isinstance(profile[name], bool) or profile[name] < 1:
            raise RuntimeError(f"invalid serving profile {name}")
    expected = {
        "WEINFER_SERVED_MODEL": str(profile["model"]),
        "WEINFER_MODEL_REVISION": str(profile["model_revision"]),
        "WEINFER_TOKENIZER_REVISION": str(profile["tokenizer_revision"]),
        "WEINFER_BACKEND_MAX_CONTEXT": str(profile["max_context_tokens"]),
        "VLLM_EXTRA_ARGS": str(profile["vllm_canonical_args"]),
    }
    for name, wanted in expected.items():
        if required_env(name) != wanted:
            raise RuntimeError(f"{name} contradicts serving profile")
    return profile_name, profile


def verify_h100_runtime() -> dict[str, Any]:
    expected_environment = {
        "CUDA_VISIBLE_DEVICES": "0",
        "FLASHINFER_DISABLE_JIT": "1",
        "VLLM_ATTENTION_BACKEND": "FLASH_ATTN",
        "VLLM_FLASH_ATTN_VERSION": "3",
        "VLLM_USE_FLASHINFER_MOE_MXFP4_BF16": "1",
        "VLLM_USE_V1": "1",
    }
    for name, wanted in expected_environment.items():
        if required_env(name) != wanted:
            raise RuntimeError(f"H100 runtime environment drift: {name}")
    workspace = Path(required_env("FLASHINFER_WORKSPACE_BASE"))
    if not workspace.is_absolute():
        raise RuntimeError("FLASHINFER_WORKSPACE_BASE must be absolute")

    result = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=name,compute_cap",
            "--format=csv,noheader",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    rows = [row.strip() for row in result.stdout.splitlines() if row.strip()]
    if len(rows) != 1:
        raise RuntimeError("H100 profile requires exactly one visible GPU")
    name, separator, capability = rows[0].rpartition(",")
    if not separator or "H100" not in name.upper() or capability.strip() != "9.0":
        raise RuntimeError("H100 hardware drift")

    # Third-party probes must not steal the single structured attestation line.
    with contextlib.redirect_stdout(sys.stderr):
        from vllm.attention.utils.fa_utils import get_flash_attn_version
        from vllm.model_executor.layers.quantization.mxfp4 import (
            Mxfp4Backend,
            get_mxfp4_backend,
        )

        flash_attention_version = get_flash_attn_version()
        mxfp4_backend = get_mxfp4_backend()
    if flash_attention_version != 3:
        raise RuntimeError("H100 attention backend did not resolve to FlashAttention 3")
    if mxfp4_backend != Mxfp4Backend.SM90_FI_MXFP4_BF16:
        raise RuntimeError("H100 MXFP4 backend did not resolve to SM90_FI_MXFP4_BF16")

    # Direct and batch compilation are structurally disabled by the verified
    # source above. Refuse as well if any compiled runtime artifact pre-exists.
    forbidden_names = {"build.ninja"}
    forbidden_suffixes = {".cubin", ".fatbin", ".o", ".so"}
    if workspace.exists():
        for path in workspace.rglob("*"):
            if path.is_file() and (
                path.name in forbidden_names or path.suffix in forbidden_suffixes
            ):
                raise RuntimeError("runtime FlashInfer compilation artifact present")

    return {
        "attention_backend": "FLASH_ATTN",
        "compute_capability": "9.0",
        "flash_attention_version": 3,
        "gpu": name.strip(),
        "mxfp4_backend": mxfp4_backend.name,
        "runtime_jit": "disabled",
    }


def write_attestation(path: Path, value: dict[str, Any]) -> None:
    body = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(body)


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--static", action="store_true")
    mode.add_argument("--runtime", action="store_true")
    mode.add_argument("--post-engine", action="store_true")
    args = parser.parse_args()

    static = verify_static()
    if args.static:
        print(json.dumps({"object": "weinfer_image_static_verification_v1", **static}, sort_keys=True))
        return 0

    contract = read_json(CONTRACT_PATH)
    profile_name, _ = verify_profile(contract)
    resolved: dict[str, Any] = {}
    if profile_name == "gpt-oss-120b-h100-v1":
        resolved = verify_h100_runtime()
    attestation = {
        "image_static": static,
        "object": "weinfer_pod_runtime_attestation_v1",
        "phase": "post_engine" if args.post_engine else "pre_engine",
        "profile": profile_name,
        "resolved": resolved,
    }
    destination = POST_ENGINE_ATTESTATION_PATH if args.post_engine else ATTESTATION_PATH
    write_attestation(destination, attestation)
    print(json.dumps(attestation, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Fixed prefix plus exception class/message; none of the validated
        # values is a credential and no environment dictionary is printed.
        raise SystemExit(f"RUNTIME PREFLIGHT REFUSED: {exc}") from exc
