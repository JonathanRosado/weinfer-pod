#!/usr/bin/env python3
"""Constrain FlashInfer's retained runner sources to the frozen launch tuple."""

from __future__ import annotations

import hashlib
import json
import os
import sysconfig
from pathlib import Path


SCOPE_DEFINE = "WEINFER_EXACT_SM90_BF16_MXFP4_RUNNER_SCOPE"
POLICY = (
    "retain ENABLE_FP8 for vendored shared PackType definitions while compiling "
    "only the exact SM90 BF16-activation/MXFP4-weight runner requested by the "
    "pinned vLLM launch path, and bind its candidate and dispatch authorities "
    "to the one generated FAST_BUILD tactic"
)
RUNNER_CONTRACT = {
    "activation_dtype": "bfloat16",
    "output_dtype": "bfloat16",
    "use_deepseek_fp8_block_scale": False,
    "use_mxfp8_act_scaling": False,
    "use_w4_group_scaling": True,
    "weight_storage_dtype": "uint8",
    "weight_template_dtype": "mxfp4_e2m1",
}
TACTIC_CONTRACT = {
    "candidate_flags": ["weight_only", "hopper", "grouped_gemm"],
    "cluster_shape": [1, 1, 1],
    "cta_shape": [128, 128, 128],
    "epilogue_schedule": "auto_config_to_tma_warp_specialized_cooperative",
    "mainloop_schedule": "pingpong",
}

INSTANTIATION_START = (
    b"// ==================== Variable batched GEMM specializations "
    b"==================================\n"
)
INSTANTIATION_END = (
    b"};  // namespace tensorrt_llm::kernels::cutlass_kernels\n"
)
INSTANTIATION_PREFIX = f"""// ==================== WeInfer exact SM90 BF16/MXFP4 specialization ============================
#if defined({SCOPE_DEFINE})
#if !defined(ENABLE_BF16) || !defined(ENABLE_FP4)
#error "WeInfer exact SM90 BF16/MXFP4 runner requires ENABLE_BF16 and ENABLE_FP4"
#endif
template class CutlassMoeFCRunner<__nv_bfloat16, __nv_fp4_e2m1>;
#else
""".encode()

BINDING_START = (
    b"    // keep consistent with cpp/tensorrt_llm/plugins/mixtureOfExperts/"
    b"mixtureOfExpertsPlugin.cpp\n"
)
BINDING_END = b"    if (!mKernelRunner) {\n"
BINDING_PREFIX = f"""#if defined({SCOPE_DEFINE})
#if !defined(ENABLE_BF16) || !defined(ENABLE_FP4)
#error "WeInfer exact SM90 BF16/MXFP4 runner requires ENABLE_BF16 and ENABLE_FP4"
#endif
    if (mActivationDtype == c10::ScalarType::BFloat16 &&
        mWeightDtype == c10::ScalarType::Byte &&
        mOutputDtype == c10::ScalarType::BFloat16 &&
        !mUseDeepSeekFP8BlockScaling && mUseW4GroupScaling &&
        !mUseMxfp8ActScaling) {{
      mInnerDimMultiplier = 2;
      mKernelRunner =
          std::make_shared<kernels::CutlassMoeFCRunner<__nv_bfloat16, __nv_fp4_e2m1>>();
    }} else {{
      C10_THROW_ERROR_FORMATTED(
          Error,
          "WeInfer exact SM90 BF16/MXFP4 runner scope refused Activation: "
              << torch::toString(mActivationDtype) << ", Weight storage: "
              << torch::toString(mWeightDtype) << ", Output: "
              << torch::toString(mOutputDtype) << ", deepseek_fp8_block_scale: "
              << mUseDeepSeekFP8BlockScaling << ", w4_group_scaling: "
              << mUseW4GroupScaling << ", mxfp8_act_scaling: "
              << mUseMxfp8ActScaling);
    }}
#else
""".encode()

CANDIDATE_START = (
    b"std::vector<CutlassGemmConfig> get_candidate_configs_sm90(\n"
)
CANDIDATE_END = (
    b"\nstd::vector<CutlassGemmConfig> get_candidate_configs_sm100(\n"
)
CANDIDATE_PREFIX = f"""#if defined({SCOPE_DEFINE})
#if !defined(FAST_BUILD)
#error "WeInfer exact SM90 BF16/MXFP4 tactic authority requires FAST_BUILD"
#endif
std::vector<CutlassGemmConfig> get_candidate_configs_sm90(
    CutlassGemmConfig::CandidateConfigTypeParam const config) {{
  // WFP4A16 is classified as weight-only grouped GEMM. Upstream's FP4_ONLY
  // flag covers FP4 activations and WFP4AFP4, not BF16-activation/FP4-weight.
  constexpr int expected_flags =
      CutlassGemmConfig::WEIGHT_ONLY | CutlassGemmConfig::HOPPER |
      CutlassGemmConfig::GROUPED_GEMM;
  if (static_cast<int>(config) != expected_flags) {{
    TLLM_THROW(
        "WeInfer exact SM90 BF16/MXFP4 tactic authority refused candidate flags: "
        "observed=%d expected=%d",
        static_cast<int>(config), expected_flags);
  }}
  return {{CutlassGemmConfig{{CutlassTileConfigSM90::CtaShape128x128x128B,
                             MainloopScheduleType::PINGPONG,
                             EpilogueScheduleType::AUTO,
                             ClusterShape::ClusterShape_1x1x1}}}};
}}
#else
""".encode()

DISPATCH_START = (
    b"template <typename T, typename WeightType, typename GemmOutputType, "
    b"typename EpilogueTag,\n"
    b"          typename CTAShape, typename ClusterShape>\n"
    b"void sm90_dispatch_mainloop_schedules(\n"
)
DISPATCH_END = (
    b"}  // namespace tensorrt_llm::kernels::cutlass_kernels\n"
)
DISPATCH_PREFIX = f"""#if defined({SCOPE_DEFINE})
#if !defined(COMPILE_HOPPER_TMA_GROUPED_GEMMS)
#error "WeInfer exact SM90 BF16/MXFP4 tactic dispatch requires grouped Hopper GEMM"
#endif
template <typename T, typename WeightType, typename GemmOutputType, typename EpilogueTag,
          int PackedScalesNum>
void sm90_dispatch_moe_mixed_dtype_gemm_to_cutlass(
    GroupedGemmInput<T, WeightType, GemmOutputType, GemmOutputType> inputs,
    TmaWarpSpecializedGroupedGemmInput hopper_inputs, int sm_count_, size_t* workspace_size) {{
  static_assert(std::is_same_v<T, __nv_bfloat16>);
  static_assert(std::is_same_v<WeightType, __nv_fp4_e2m1>);
  static_assert(std::is_same_v<GemmOutputType, __nv_bfloat16>);
  static_assert(
      std::is_same_v<EpilogueTag, tensorrt_llm::cutlass_extensions::EpilogueOpDefault>);
  static_assert(PackedScalesNum == 1);

  auto const& config = inputs.gemm_config;
  if (config.sm_version != 90 || !config.is_tma_warp_specialized ||
      config.tile_config_sm90 != tkc::CutlassTileConfigSM90::CtaShape128x128x128B ||
      config.cluster_shape != tkc::ClusterShape::ClusterShape_1x1x1 ||
      config.mainloop_schedule != tkc::MainloopScheduleType::PINGPONG ||
      config.epilogue_schedule != tkc::EpilogueScheduleType::AUTO) {{
    TLLM_THROW(
        "WeInfer exact SM90 BF16/MXFP4 tactic dispatch refused config: "
        "sm=%d tma=%d tile=%d cluster=%d mainloop=%d epilogue=%d",
        config.sm_version, static_cast<int>(config.is_tma_warp_specialized),
        static_cast<int>(config.tile_config_sm90),
        static_cast<int>(config.cluster_shape),
        static_cast<int>(config.mainloop_schedule),
        static_cast<int>(config.epilogue_schedule));
  }}

  sm90_generic_mixed_moe_gemm_kernelLauncher<
      T, WeightType, GemmOutputType, EpilogueTag,
      Shape<_128, _128, _128>, Shape<_1, _1, _1>,
      cutlass::gemm::KernelTmaWarpSpecializedPingpong,
      cutlass::epilogue::TmaWarpSpecializedCooperative,
      cutlass::WeightOnlyQuantOp::FINEGRAINED_SCALE_ONLY>(
      inputs, hopper_inputs, sm_count_, workspace_size);
}}

template <typename T, typename WeightType, typename OutputType>
size_t calcMaxWorkspaceSizeTmaWarpSpecializedMixedInput(int num_experts, int sm_count_) {{
  static_assert(std::is_same_v<T, __nv_bfloat16>);
  static_assert(std::is_same_v<WeightType, __nv_fp4_e2m1>);
  static_assert(std::is_same_v<OutputType, __nv_bfloat16>);

  size_t count = 0;
  GroupedGemmInput<T, WeightType, OutputType, OutputType> inputs{{}};
  inputs.num_experts = num_experts;
  inputs.gemm_config = tkc::CutlassGemmConfig{{
      tkc::CutlassTileConfigSM90::CtaShape128x128x128B,
      tkc::MainloopScheduleType::PINGPONG,
      tkc::EpilogueScheduleType::AUTO,
      tkc::ClusterShape::ClusterShape_1x1x1}};
  sm90_generic_mixed_moe_gemm_kernelLauncher<
      T, WeightType, OutputType,
      tensorrt_llm::cutlass_extensions::EpilogueOpDefault,
      Shape<_128, _128, _128>, Shape<_1, _1, _1>,
      cutlass::gemm::KernelTmaWarpSpecializedPingpong,
      cutlass::epilogue::TmaWarpSpecializedCooperative,
      cutlass::WeightOnlyQuantOp::FINEGRAINED_SCALE_ONLY>(
      inputs, TmaWarpSpecializedGroupedGemmInput{{}}, sm_count_, &count);
  return count;
}}
#else
""".encode()

TARGETS = (
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
)

TRANSFORMS = {
    "explicit_instantiations": (
        INSTANTIATION_START,
        INSTANTIATION_END,
        INSTANTIATION_PREFIX,
    ),
    "runtime_binding": (BINDING_START, BINDING_END, BINDING_PREFIX),
    "candidate_authority": (
        CANDIDATE_START,
        CANDIDATE_END,
        CANDIDATE_PREFIX,
    ),
    "tactic_dispatch": (DISPATCH_START, DISPATCH_END, DISPATCH_PREFIX),
}


def sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def scope_region(body: bytes, target: dict[str, object]) -> bytes:
    start, end, prefix = TRANSFORMS[str(target["kind"])]
    if body.count(start) != 1 or body.count(end) != 1:
        raise RuntimeError(
            f"FlashInfer exact MXFP4 runner region drift: {target['path']}"
        )
    first = body.index(start)
    last = body.index(end)
    if first >= last:
        raise RuntimeError(
            f"FlashInfer exact MXFP4 runner region ordering drift: {target['path']}"
        )
    original = body[first:last]
    if sha256(original) != target["region_sha256"]:
        raise RuntimeError(
            f"FlashInfer exact MXFP4 runner region hash drift: {target['path']}"
        )
    return body[:first] + prefix + original + b"#endif\n" + body[last:]


def recover_upstream(body: bytes, target: dict[str, object]) -> bytes:
    """Remove only our wrapper and recover the byte-identical upstream source."""
    _, end, prefix = TRANSFORMS[str(target["kind"])]
    suffix = b"#endif\n" + end
    if body.count(prefix) != 1 or body.count(suffix) != 1:
        raise RuntimeError(
            f"FlashInfer exact MXFP4 runner wrapper drift: {target['path']}"
        )
    first = body.index(prefix)
    last = body.index(suffix, first + len(prefix))
    return body[:first] + body[first + len(prefix) : last] + body[last + 7 :]


def transform(body: bytes, target: dict[str, object]) -> bytes:
    if sha256(body) != target["preimage_sha256"]:
        raise RuntimeError(
            f"FlashInfer exact MXFP4 runner preimage drift: {target['path']}"
        )
    fixed = scope_region(body, target)
    if recover_upstream(fixed, target) != body:
        raise RuntimeError(
            f"FlashInfer exact MXFP4 runner upstream recovery mismatch: "
            f"{target['path']}"
        )
    if sha256(fixed) != target["source_sha256"]:
        raise RuntimeError(
            f"FlashInfer exact MXFP4 runner postimage mismatch: {target['path']}"
        )
    return fixed


def source_records() -> list[dict[str, object]]:
    return [
        {
            "kind": target["kind"],
            "path": target["path"],
            "preimage_sha256": target["preimage_sha256"],
            "region_sha256": target["region_sha256"],
            "source_sha256": target["source_sha256"],
        }
        for target in TARGETS
    ]


def main() -> int:
    flashinfer_root = Path(sysconfig.get_paths()["purelib"]) / "flashinfer"
    for record in TARGETS:
        target = flashinfer_root / str(record["path"])
        if not target.is_file() or target.is_symlink():
            raise SystemExit(
                f"FlashInfer exact MXFP4 runner source missing or symlinked: {target}"
            )
        body = target.read_bytes()
        before = sha256(body)
        if before == record["preimage_sha256"]:
            try:
                body = transform(body, record)
            except RuntimeError as exc:
                raise SystemExit(str(exc)) from None
            temporary = target.with_name(f".{target.name}.tmp.{os.getpid()}")
            temporary.write_bytes(body)
            os.replace(temporary, target)
        elif before != record["source_sha256"]:
            raise SystemExit(
                f"FlashInfer exact MXFP4 runner source drift: "
                f"path={target} observed={before}"
            )

    marker = (
        flashinfer_root
        / "data/csrc/fused_moe/cutlass_backend/"
        ".weinfer-exact-mxfp4-runner-scope.json"
    )
    marker_body = (
        json.dumps(
            {
                "object": "weinfer_flashinfer_exact_mxfp4_runner_scope_v1",
                "policy": POLICY,
                "runner": RUNNER_CONTRACT,
                "sources": source_records(),
                "tactic": TACTIC_CONTRACT,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    temporary = marker.with_name(f".{marker.name}.tmp.{os.getpid()}")
    temporary.write_bytes(marker_body)
    os.replace(temporary, marker)
    print(
        "FlashInfer runner scope fixed to exact SM90 BF16/MXFP4 launch: "
        f"source_count={len(TARGETS)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
