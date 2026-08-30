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
    "pinned vLLM launch path"
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
)

TRANSFORMS = {
    "explicit_instantiations": (
        INSTANTIATION_START,
        INSTANTIATION_END,
        INSTANTIATION_PREFIX,
    ),
    "runtime_binding": (BINDING_START, BINDING_END, BINDING_PREFIX),
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
