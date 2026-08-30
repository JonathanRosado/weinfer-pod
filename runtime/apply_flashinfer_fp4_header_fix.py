#!/usr/bin/env python3
"""Permit FlashInfer's FP4-only MoE build without enabling FP8 branches."""

from __future__ import annotations

import hashlib
import json
import os
import sysconfig
from pathlib import Path


PREIMAGE_SHA256 = "baf9ac18311c8aae4b3a79cbb25db9bcb3f9c7dd850995f59d0d1fbd04e53808"
POSTIMAGE_SHA256 = "5e49703e055c8167b32a09a6b6b0ff09d499bf72e5e70e4a4bdd56304f21ca39"
POLICY = (
    "remove the duplicate use_wfp4afp4 fallback so ENABLE_FP4 does not require "
    "ENABLE_FP8"
)
TARGET_RELATIVE = Path(
    "flashinfer/data/csrc/nv_internal/tensorrt_llm/kernels/cutlass_kernels/"
    "include/moe_gemm_kernels.h"
)
OLD_DECLARATION = b"""#else
  static constexpr bool use_fp8 = false;
  static constexpr bool use_w4afp8 = false;
  static constexpr bool use_wfp4afp4 = false;
#endif
  static constexpr bool use_w4_groupwise = use_w4afp8 || use_wfp4a16;
"""
NEW_DECLARATION = b"""#else
  static constexpr bool use_fp8 = false;
  static constexpr bool use_w4afp8 = false;
#endif
  static constexpr bool use_w4_groupwise = use_w4afp8 || use_wfp4a16;
"""


def sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def transform(body: bytes) -> bytes:
    if body.count(OLD_DECLARATION) != 1:
        raise RuntimeError("FlashInfer FP4 header-fix preimage drift")
    fixed = body.replace(OLD_DECLARATION, NEW_DECLARATION)
    if sha256(fixed) != POSTIMAGE_SHA256:
        raise RuntimeError("FlashInfer FP4 header-fix postimage mismatch")
    return fixed


def main() -> int:
    purelib = Path(sysconfig.get_paths()["purelib"])
    target = purelib / TARGET_RELATIVE
    marker = target.parent / ".weinfer-fp4-header-fix.json"
    if not target.is_file() or target.is_symlink():
        raise SystemExit("FlashInfer MoE GEMM header missing or symlinked")
    body = target.read_bytes()
    before = sha256(body)
    if before == PREIMAGE_SHA256:
        try:
            body = transform(body)
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from None
        temporary = target.with_name(f".{target.name}.tmp.{os.getpid()}")
        temporary.write_bytes(body)
        os.replace(temporary, target)
    elif before != POSTIMAGE_SHA256:
        raise SystemExit(f"FlashInfer MoE GEMM header drift: observed={before}")

    marker_body = (
        json.dumps(
            {
                "object": "weinfer_flashinfer_fp4_header_fix_v1",
                "policy": POLICY,
                "preimage_sha256": PREIMAGE_SHA256,
                "source_sha256": POSTIMAGE_SHA256,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    temporary = marker.with_name(f".{marker.name}.tmp.{os.getpid()}")
    temporary.write_bytes(marker_body)
    os.replace(temporary, marker)
    print(f"FlashInfer FP4 header fixed: source_sha256={POSTIMAGE_SHA256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
