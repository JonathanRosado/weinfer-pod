#!/usr/bin/env python3
"""Build exactly the FlashInfer operators used by gpt-oss-120B on SM90."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from importlib.metadata import version
from pathlib import Path


MANIFEST = Path("/weinfer/runtime/flashinfer-aot-manifest.json")
EXPECTED_ARCH = "9.0"
EXPECTED_VERSION = "0.3.1"
TARGET_TACTIC = (
    "gemm_grouped_sm90_nv_bf16_nv_e2m1_ue8m0_bf16_bf16_fgs_lc_"
    "128x128x128_0x0x0_0_1x1x1_warpspecialized_pingpong_epi_tma"
)
TARGET_GENERATED_SOURCE = "weinfer_exact_sm90_bf16_mxfp4.generated.cu"
TARGET_GENERATED_LAUNCHER = (
    "tensorrt_llm/kernels/cutlass_kernels/moe_gemm/launchers/"
    "moe_gemm_tma_ws_mixed_input_launcher.inl"
)
UPSTREAM_SHARED_BINDING_SOURCE = (
    "fused_moe/cutlass_backend/flashinfer_cutlass_fused_moe_sm100_ops.cu"
)
TARGET_STATIC_SOURCE_SUFFIXES = (
    "nv_internal/tensorrt_llm/kernels/cutlass_kernels/moe_gemm/"
    "moe_gemm_tma_warp_specialized_input.cu",
    "nv_internal/tensorrt_llm/kernels/cutlass_kernels/moe_gemm/"
    "moe_gemm_kernels_bf16_fp4.cu",
    UPSTREAM_SHARED_BINDING_SOURCE,
    "fused_moe/cutlass_backend/cutlass_fused_moe_instantiation.cu",
    "nv_internal/cpp/common/envUtils.cpp",
    "nv_internal/cpp/common/logger.cpp",
    "nv_internal/cpp/common/stringUtils.cpp",
    "nv_internal/cpp/common/tllmException.cpp",
    "nv_internal/cpp/common/memoryUtils.cu",
    "nv_internal/tensorrt_llm/kernels/preQuantScaleKernel.cu",
    "nv_internal/tensorrt_llm/kernels/cutlass_kernels/cutlass_heuristic.cpp",
    "nv_internal/tensorrt_llm/kernels/lora/lora.cpp",
)
TARGET_SOURCE_SUFFIXES = TARGET_STATIC_SOURCE_SUFFIXES + (TARGET_GENERATED_SOURCE,)
HEADER_COMPATIBILITY_DEFINES = ("ENABLE_FP8",)
RUNTIME_BINDING = (
    "flashinfer.fused_moe.core:get_cutlass_fused_moe_module->"
    "flashinfer.jit.core:JitSpec.build_and_load:is_aot"
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def select_target_operation(operations):
    """Select the one generator record named by FlashInfer's SM90 fast build."""
    matches = [operation for operation in operations if repr(operation) == TARGET_TACTIC]
    if len(matches) != 1:
        raise RuntimeError(
            f"expected one exact SM90 BF16/MXFP4 tactic, observed {len(matches)}"
        )
    return matches[0]


def source_for_suffix(sources: list[Path], suffix: str) -> Path:
    matches = [source for source in sources if source.as_posix().endswith(suffix)]
    if len(matches) != 1:
        raise RuntimeError(
            f"expected one FlashInfer source ending in {suffix}, observed {len(matches)}"
        )
    return matches[0]


def reemit_narrow_fused_moe_spec(spec, selected: list[Path]):
    """Replace the eager upstream Ninja graph with the frozen source set."""
    observed_suffixes = tuple(
        suffix
        for source in selected
        for suffix in TARGET_SOURCE_SUFFIXES
        if source.as_posix().endswith(suffix)
    )
    if observed_suffixes != TARGET_SOURCE_SUFFIXES:
        raise RuntimeError("narrow FlashInfer AOT source set drift")

    # The frozen call path is BF16 activation x MXFP4 weight.  Ungrouped GEMM,
    # FP8 activation source files, FP16 and INT4 variants are outside that
    # contract.  Keep ENABLE_FP8: the vendored moe_gemm_kernels.h declares its
    # FP4 predicate twice when ENABLE_FP4 is set without this upstream companion
    # guard.  The exact generated tactic and source list still contain no FP8
    # activation kernel.
    spec.extra_cuda_cflags = [
        flag
        for flag in (spec.extra_cuda_cflags or [])
        if flag != "-DCOMPILE_HOPPER_TMA_GEMMS"
    ]
    if spec.extra_cuda_cflags.count("-DENABLE_FP8") != 1:
        raise RuntimeError("FlashInfer FP4 header compatibility define drift")
    spec.sources = selected

    # FlashInfer's gen_jit_spec eagerly writes build.ninja before returning the
    # JitSpec.  Mutating only the Python object leaves that original full graph
    # authoritative.  Re-emit after every mutation so Ninja receives the same
    # 13-source set and flags that the manifest records.
    spec.write_ninja()
    return spec


def verify_emitted_ninja(spec, selected: list[Path]) -> dict[str, object]:
    """Prove the on-disk graph, not the already-correct Python object."""
    ninja = spec.ninja_path.read_text(encoding="utf-8")
    emitted = []
    for line in ninja.splitlines():
        if not line.startswith("build "):
            continue
        rule_and_input = line.split(": ", 1)
        if len(rule_and_input) != 2:
            continue
        rule, source = rule_and_input[1].split(" ", 1)
        if rule in {"compile", "cuda_compile"}:
            emitted.append(source)
    expected = [str(source.resolve()) for source in selected]
    if emitted != expected:
        raise RuntimeError(
            "emitted FlashInfer Ninja graph does not match the narrow source set: "
            f"expected {len(expected)}, observed {len(emitted)}"
        )
    if ninja.count("-DFAST_BUILD") != 1:
        raise RuntimeError("emitted FlashInfer Ninja graph lost FAST_BUILD")
    if ninja.count("-DENABLE_FP8") != 1:
        raise RuntimeError(
            "emitted FlashInfer Ninja graph lost ENABLE_FP8 header compatibility define"
        )
    for forbidden in ("-DCOMPILE_HOPPER_TMA_GEMMS",):
        if forbidden in ninja:
            raise RuntimeError(
                f"emitted FlashInfer Ninja graph retained forbidden flag {forbidden}"
            )
    return {
        "module": spec.name,
        "ninja_sha256": sha256(spec.ninja_path),
        "object": "weinfer_flashinfer_prebuild_graph_v1",
        "source_count": len(emitted),
    }


def narrow_fused_moe_spec(spec):
    """Bind the AOT module to FlashInfer's single upstream fast-build tactic."""
    from flashinfer.jit import env as jit_env
    from flashinfer.jit.cutlass_gemm.generate_kernels import (
        generate_sm90_mixed_type_grouped_gemm_operations,
        write_file,
    )

    if (
        spec.name != "fused_moe_90"
        or (spec.extra_cflags or []).count("-DFAST_BUILD") != 1
    ):
        raise RuntimeError("FlashInfer SM90 fast-build contract drift")
    required_cuda_flags = {
        "-DCOMPILE_HOPPER_TMA_GROUPED_GEMMS",
        "-DENABLE_BF16",
        "-DENABLE_FP8",
        "-DENABLE_FP4",
        "-DUSING_OSS_CUTLASS_MOE_GEMM",
    }
    observed_cuda_flags = set(spec.extra_cuda_cflags or [])
    if not required_cuda_flags.issubset(observed_cuda_flags):
        raise RuntimeError("FlashInfer SM90 BF16/MXFP4 compile flags drift")

    operation = select_target_operation(
        generate_sm90_mixed_type_grouped_gemm_operations(True)
    )
    generated = (
        jit_env.FLASHINFER_CSRC_DIR
        / "nv_internal/tensorrt_llm/cutlass_instantiations/90"
        / TARGET_GENERATED_SOURCE
    )
    write_file([TARGET_GENERATED_LAUNCHER], [operation], str(generated))

    selected = [
        source_for_suffix(spec.sources, suffix)
        for suffix in TARGET_STATIC_SOURCE_SUFFIXES
    ] + [generated]
    narrowed = reemit_narrow_fused_moe_spec(spec, selected)
    print(
        json.dumps(verify_emitted_ninja(narrowed, selected), sort_keys=True),
        flush=True,
    )
    return narrowed


def main() -> int:
    if os.environ.get("FLASHINFER_CUDA_ARCH_LIST") != EXPECTED_ARCH:
        raise SystemExit("AOT build requires FLASHINFER_CUDA_ARCH_LIST=9.0")
    if os.environ.get("FLASHINFER_DISABLE_JIT") == "1":
        raise SystemExit("AOT image build cannot run with runtime JIT disabled")
    if version("flashinfer-python") != EXPECTED_VERSION:
        raise SystemExit("FlashInfer version drift before AOT build")

    # Imports happen only after the explicit architecture has been checked;
    # FlashInfer snapshots it into its compilation context at import time.
    from flashinfer.fused_moe.core import gen_cutlass_fused_moe_sm90_module
    from flashinfer.jit import build_jit_specs
    from flashinfer.sampling import gen_sampling_module
    from flashinfer.tllm_utils import gen_trtllm_utils_module

    specs = [
        narrow_fused_moe_spec(gen_cutlass_fused_moe_sm90_module(use_fast_build=True)),
        gen_sampling_module(),
        gen_trtllm_utils_module(),
    ]
    if [spec.name for spec in specs] != ["fused_moe_90", "sampling", "trtllm_utils"]:
        raise SystemExit("FlashInfer AOT operator set drift")
    build_jit_specs(specs, verbose=True, skip_prebuilt=False)

    rows = []
    for spec in specs:
        source = spec.jit_library_path
        destination = spec.aot_path
        if not source.is_file() or source.is_symlink() or source.stat().st_size < 1:
            raise SystemExit(f"FlashInfer AOT build did not produce {spec.name}")
        destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        rows.append(
            {
                "bytes": destination.stat().st_size,
                "name": spec.name,
                "path": str(destination),
                "sha256": sha256(destination),
            }
        )

    manifest = {
        "build_mode": "image_build_aot_no_runtime_compilation",
        "cuda_arch": EXPECTED_ARCH,
        "flashinfer_python_version": EXPECTED_VERSION,
        "fused_moe_build": {
            "activation_dtype": "bfloat16",
            "cluster_shape": [1, 1, 1],
            "compiled_source_count": len(specs[0].sources),
            "compile_define": "FAST_BUILD",
            "cta_shape": [128, 128, 128],
            "generated_tactic": TARGET_TACTIC,
            "header_compatibility_defines": list(HEADER_COMPATIBILITY_DEFINES),
            "mainloop_schedule": "pingpong",
            "runtime_binding": RUNTIME_BINDING,
            "scale_dtype": "ue8m0",
            "source_suffixes": list(TARGET_SOURCE_SUFFIXES),
            "upstream_fast_build": True,
            "upstream_shared_binding_source": UPSTREAM_SHARED_BINDING_SOURCE,
            "weight_dtype": "mxfp4_e2m1",
        },
        "object": "weinfer_flashinfer_aot_manifest_v1",
        "operators": rows,
    }
    temporary = MANIFEST.with_name(f".{MANIFEST.name}.tmp.{os.getpid()}")
    temporary.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, MANIFEST)
    print(json.dumps(manifest, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
