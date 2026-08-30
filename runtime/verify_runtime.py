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
VLLM_MXFP4_CALL_SOURCE = (
    "vllm/model_executor/layers/quantization/mxfp4.py"
)
VLLM_MXFP4_CALL_SOURCE_SHA256 = (
    "69f4105640bd466d463ccf9302164d35e9299f8e9228568dd52a9c7d66146b75"
)
FLASHINFER_VERSION = "0.3.1"
FLASHINFER_SOURCE_SHA256 = "dbca0c0c36d4fd2f559021b5d9c356681501b14a3cf2ec3d37d53d17527dcc7b"
FLASHINFER_EXACT_MXFP4_RUNNER_SCOPE_POLICY = (
    "retain ENABLE_FP8 for vendored shared PackType definitions while compiling "
    "only the exact SM90 BF16-activation/MXFP4-weight runner requested by the "
    "pinned vLLM launch path, and bind its candidate and dispatch authorities "
    "to the one generated FAST_BUILD tactic"
)
FLASHINFER_EXACT_MXFP4_RUNNER_CONTRACT = {
    "activation_dtype": "bfloat16",
    "output_dtype": "bfloat16",
    "use_deepseek_fp8_block_scale": False,
    "use_mxfp8_act_scaling": False,
    "use_w4_group_scaling": True,
    "weight_storage_dtype": "uint8",
    "weight_template_dtype": "mxfp4_e2m1",
}
FLASHINFER_EXACT_MXFP4_TACTIC_CONTRACT = {
    "candidate_flags": ["weight_only", "hopper", "grouped_gemm"],
    "cluster_shape": [1, 1, 1],
    "cta_shape": [128, 128, 128],
    "epilogue_schedule": "auto_config_to_tma_warp_specialized_cooperative",
    "mainloop_schedule": "pingpong",
}
FLASHINFER_EXACT_MXFP4_RUNNER_SCOPE_SOURCES = [
    {
        "kind": "explicit_instantiations",
        "path": (
            "data/csrc/fused_moe/cutlass_backend/"
            "cutlass_fused_moe_instantiation.cu"
        ),
        "preimage_sha256": (
            "56c5cdb2e92fe48cbe8952e17e91d46ce61a82a45dca27f82fa13a43bacced1f"
        ),
        "region_sha256": (
            "f1abc4251525d03273b1529bae2b31e54664abadb32c9e13bd1768f1de4e632f"
        ),
        "source_sha256": (
            "5bf1e23d1fb79f1dd786b52b1995cf65c061bf425fbd290cfae3450cbf4f1804"
        ),
    },
    {
        "kind": "runtime_binding",
        "path": (
            "data/csrc/fused_moe/cutlass_backend/"
            "flashinfer_cutlass_fused_moe_sm100_ops.cu"
        ),
        "preimage_sha256": (
            "87bf2788f40f752bff7172c13758e6f99d48cb0b73eea11a9fc301203fe90655"
        ),
        "region_sha256": (
            "8f73110314a13c2914ac1d457502ac7c566f8038d7fe1e1fb0da6e32c20eaee5"
        ),
        "source_sha256": (
            "7661c90f654156ad2ad42134a6e8c4194ccf8664601501187fa984c937553928"
        ),
    },
    {
        "kind": "candidate_authority",
        "path": (
            "data/csrc/nv_internal/tensorrt_llm/kernels/cutlass_kernels/"
            "cutlass_heuristic.cpp"
        ),
        "preimage_sha256": (
            "1625b594408f4992d34d6e12ec361b56ed41878841e25313c0eb0be9561aa25d"
        ),
        "region_sha256": (
            "47a7e068d60d472cdfd2d4202a02ce99680c41e7c6e98484be72a43259ddea26"
        ),
        "source_sha256": (
            "610b72988afda7217ce30c10502d3dab43de4b312e53e5649654c807b8791f9b"
        ),
    },
    {
        "kind": "tactic_dispatch",
        "path": (
            "data/csrc/nv_internal/tensorrt_llm/kernels/cutlass_kernels/"
            "moe_gemm/moe_gemm_template_dispatch_tma_ws_mixed_dtype.h"
        ),
        "preimage_sha256": (
            "1ffdaed0f314181c3404b81e3538e87e26cb8b15eaf66d695571176b7b2033c8"
        ),
        "region_sha256": (
            "bb8cae4c049566f040098fe73e3669adaf672f7eea760c2aa1dc64ece890e740"
        ),
        "source_sha256": (
            "749abd05d89dfcd36e0a7a8985afe3fcc4b26842045da3f68c6b3895e736dc2b"
        ),
    },
]
H100_CANONICAL_ARGS = (
    "--seed 0 --max-num-batched-tokens 8192 --max-num-seqs 8 "
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
    "fused_moe_90": 1_000_000,
    "sampling": 1_000_000,
    "trtllm_utils": 100_000,
}
EXPECTED_FUSED_MOE_TACTIC = (
    "gemm_grouped_sm90_nv_bf16_nv_e2m1_ue8m0_bf16_bf16_fgs_lc_"
    "128x128x128_0x0x0_0_1x1x1_warpspecialized_pingpong_epi_tma"
)
EXPECTED_UPSTREAM_SHARED_BINDING_SOURCE = (
    "fused_moe/cutlass_backend/flashinfer_cutlass_fused_moe_sm100_ops.cu"
)
EXPECTED_UPSTREAM_FP8_BLOCKSCALE_LINK_STUB_SOURCE = (
    "nv_internal/tensorrt_llm/kernels/cutlass_kernels/"
    "fp8_blockscale_gemm/fp8_blockscale_gemm_stub.cu"
)
EXPECTED_UPSTREAM_FP8_BLOCKSCALE_LINK_STUB_SHA256 = (
    "aae25878e2520693265c46e073b74f9a8f4f7daa351fc80b6f9a7fc8227c4257"
)
EXPECTED_FUSED_MOE_SOURCE_SUFFIXES = [
    "nv_internal/tensorrt_llm/kernels/cutlass_kernels/moe_gemm/"
    "moe_gemm_tma_warp_specialized_input.cu",
    "nv_internal/tensorrt_llm/kernels/cutlass_kernels/moe_gemm/"
    "moe_gemm_kernels_bf16_fp4.cu",
    EXPECTED_UPSTREAM_FP8_BLOCKSCALE_LINK_STUB_SOURCE,
    EXPECTED_UPSTREAM_SHARED_BINDING_SOURCE,
    "fused_moe/cutlass_backend/cutlass_fused_moe_instantiation.cu",
    "nv_internal/cpp/common/envUtils.cpp",
    "nv_internal/cpp/common/logger.cpp",
    "nv_internal/cpp/common/stringUtils.cpp",
    "nv_internal/cpp/common/tllmException.cpp",
    "nv_internal/cpp/common/memoryUtils.cu",
    "nv_internal/tensorrt_llm/kernels/preQuantScaleKernel.cu",
    "nv_internal/tensorrt_llm/kernels/cutlass_kernels/cutlass_heuristic.cpp",
    "nv_internal/tensorrt_llm/kernels/lora/lora.cpp",
    "weinfer_exact_sm90_bf16_mxfp4.generated.cu",
]
EXPECTED_COMPATIBILITY_COMPILE_DEFINES = ["ENABLE_FP8"]
EXPECTED_RUNNER_SCOPE_COMPILE_DEFINES = [
    "WEINFER_EXACT_SM90_BF16_MXFP4_RUNNER_SCOPE"
]
EXPECTED_REMOVED_COMPILE_DEFINES = ["COMPILE_HOPPER_TMA_GEMMS"]
EXPECTED_RUNTIME_BINDING = (
    "flashinfer.fused_moe.core:get_cutlass_fused_moe_module->"
    "flashinfer.jit.core:JitSpec.build_and_load:is_aot"
)


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
    vllm_mxfp4_source = purelib / VLLM_MXFP4_CALL_SOURCE
    if (
        not vllm_mxfp4_source.is_file()
        or vllm_mxfp4_source.is_symlink()
        or sha256(vllm_mxfp4_source) != VLLM_MXFP4_CALL_SOURCE_SHA256
    ):
        raise RuntimeError("vLLM SM90 MXFP4 call source drift")

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

    exact_runner_scope_marker_path = (
        flashinfer_root
        / "data/csrc/fused_moe/cutlass_backend/"
        ".weinfer-exact-mxfp4-runner-scope.json"
    )
    exact_runner_scope_marker = read_json(exact_runner_scope_marker_path)
    require_keys(
        exact_runner_scope_marker,
        {"object", "policy", "runner", "sources", "tactic"},
        "FlashInfer exact MXFP4 runner-scope marker",
    )
    if (
        exact_runner_scope_marker["object"]
        != "weinfer_flashinfer_exact_mxfp4_runner_scope_v1"
        or exact_runner_scope_marker["policy"]
        != FLASHINFER_EXACT_MXFP4_RUNNER_SCOPE_POLICY
        or exact_runner_scope_marker["runner"]
        != FLASHINFER_EXACT_MXFP4_RUNNER_CONTRACT
        or exact_runner_scope_marker["tactic"]
        != FLASHINFER_EXACT_MXFP4_TACTIC_CONTRACT
        or exact_runner_scope_marker["sources"]
        != FLASHINFER_EXACT_MXFP4_RUNNER_SCOPE_SOURCES
    ):
        raise RuntimeError("FlashInfer exact MXFP4 runner-scope marker drift")
    for record in FLASHINFER_EXACT_MXFP4_RUNNER_SCOPE_SOURCES:
        source = flashinfer_root / record["path"]
        if not source.is_file() or source.is_symlink():
            raise RuntimeError(
                "FlashInfer exact MXFP4 runner-scope source missing or "
                f"symlinked: {record['path']}"
            )
        if sha256(source) != record["source_sha256"]:
            raise RuntimeError(
                f"FlashInfer exact MXFP4 runner-scope source drift: {record['path']}"
            )

    manifest = read_json(AOT_MANIFEST_PATH)
    require_keys(
        manifest,
        {
            "build_mode",
            "cuda_arch",
            "flashinfer_python_version",
            "fused_moe_build",
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
    fused_moe_build = manifest["fused_moe_build"]
    if fused_moe_build != {
        "activation_dtype": "bfloat16",
        "cluster_shape": [1, 1, 1],
        "compiled_source_count": len(EXPECTED_FUSED_MOE_SOURCE_SUFFIXES),
        "compile_define": "FAST_BUILD",
        "compatibility_compile_defines": EXPECTED_COMPATIBILITY_COMPILE_DEFINES,
        "cta_shape": [128, 128, 128],
        "exact_mxfp4_runner_contract": (
            FLASHINFER_EXACT_MXFP4_RUNNER_CONTRACT
        ),
        "exact_mxfp4_runner_scope_sources": (
            FLASHINFER_EXACT_MXFP4_RUNNER_SCOPE_SOURCES
        ),
        "exact_mxfp4_tactic_contract": (
            FLASHINFER_EXACT_MXFP4_TACTIC_CONTRACT
        ),
        "generated_tactic": EXPECTED_FUSED_MOE_TACTIC,
        "image_build_dynamic_load_validation": "torch.classes.FusedMoeRunner",
        "link_satisfaction_source": EXPECTED_UPSTREAM_FP8_BLOCKSCALE_LINK_STUB_SOURCE,
        "link_satisfaction_source_sha256": (
            EXPECTED_UPSTREAM_FP8_BLOCKSCALE_LINK_STUB_SHA256
        ),
        "mainloop_schedule": "pingpong",
        "removed_compile_defines": EXPECTED_REMOVED_COMPILE_DEFINES,
        "runner_scope_compile_defines": EXPECTED_RUNNER_SCOPE_COMPILE_DEFINES,
        "runtime_binding": EXPECTED_RUNTIME_BINDING,
        "scale_dtype": "ue8m0",
        "source_suffixes": EXPECTED_FUSED_MOE_SOURCE_SUFFIXES,
        "upstream_fast_build": True,
        "upstream_shared_binding_source": EXPECTED_UPSTREAM_SHARED_BINDING_SOURCE,
        "vllm_mxfp4_call_source": VLLM_MXFP4_CALL_SOURCE,
        "vllm_mxfp4_call_source_sha256": VLLM_MXFP4_CALL_SOURCE_SHA256,
        "weight_dtype": "mxfp4_e2m1",
    }:
        raise RuntimeError("FlashInfer fused-MoE AOT scope drift")
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
        "flashinfer_exact_mxfp4_runner_scope_sources": (
            FLASHINFER_EXACT_MXFP4_RUNNER_SCOPE_SOURCES
        ),
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


def scan_runtime_workspace(workspace: Path) -> list[dict[str, str]]:
    """Record declarative graphs and refuse evidence that a compiler executed."""
    build_graphs: list[dict[str, str]] = []
    forbidden_names = {".ninja_deps", ".ninja_log"}
    forbidden_suffixes = {".cubin", ".fatbin", ".o", ".ptx", ".so"}
    if workspace.exists():
        for path in workspace.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(workspace).as_posix()
            if path.name == "build.ninja":
                build_graphs.append({"path": relative, "sha256": sha256(path)})
            elif path.name in forbidden_names or path.suffix in forbidden_suffixes:
                raise RuntimeError(
                    f"runtime FlashInfer compiled artifact present: {relative}"
                )
    return sorted(build_graphs, key=lambda row: row["path"])


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
    # source above. FlashInfer eagerly writes build.ninja while constructing a
    # JitSpec, before build_and_load discovers the installed AOT object. That
    # graph is not evidence of compilation. Record it, but refuse compiler
    # outputs and Ninja execution metadata; those can exist only if a runtime
    # build actually ran.
    build_graphs = scan_runtime_workspace(workspace)

    return {
        "attention_backend": "FLASH_ATTN",
        "compute_capability": "9.0",
        "flash_attention_version": 3,
        "gpu": name.strip(),
        "mxfp4_backend": mxfp4_backend.name,
        "runtime_build_graphs": build_graphs,
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
