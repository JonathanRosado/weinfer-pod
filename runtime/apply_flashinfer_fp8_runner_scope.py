#!/usr/bin/env python3
"""Retain shared FP8-gated types while disabling unused FP8 MoE runners."""

from __future__ import annotations

import hashlib
import json
import os
import sysconfig
from pathlib import Path


POLICY = (
    "retain ENABLE_FP8 for vendored shared PackType definitions while compiling "
    "the binding and explicit-instantiation sources with unused FP8 runner "
    "branches disabled"
)
OLD_GUARD = b"#ifdef ENABLE_FP8\n"
NEW_GUARD = (
    b"#if defined(ENABLE_FP8) && "
    b"!defined(WEINFER_DISABLE_FP8_RUNNER_BRANCHES)\n"
)
TARGETS = (
    {
        "path": (
            "data/csrc/fused_moe/cutlass_backend/"
            "cutlass_fused_moe_instantiation.cu"
        ),
        "guard_count": 1,
        "preimage_sha256": (
            "56c5cdb2e92fe48cbe8952e17e91d46ce61a82a45dca27f82fa13a43bacced1f"
        ),
        "source_sha256": (
            "b24efa82fab95a873cb1e563cb1e140ed9bd2e0eabefb52ae895b6618b59b311"
        ),
    },
    {
        "path": (
            "data/csrc/fused_moe/cutlass_backend/"
            "flashinfer_cutlass_fused_moe_sm100_ops.cu"
        ),
        "guard_count": 4,
        "preimage_sha256": (
            "87bf2788f40f752bff7172c13758e6f99d48cb0b73eea11a9fc301203fe90655"
        ),
        "source_sha256": (
            "81866ae743a6ca1c19cd7e1e55894163ddfe133c290a77f5c1f25249e41dc4b5"
        ),
    },
)


def sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def transform(body: bytes, target: dict[str, object]) -> bytes:
    if sha256(body) != target["preimage_sha256"]:
        raise RuntimeError(f"FlashInfer FP8 runner preimage drift: {target['path']}")
    if body.count(OLD_GUARD) != target["guard_count"]:
        raise RuntimeError(f"FlashInfer FP8 runner guard-count drift: {target['path']}")
    fixed = body.replace(OLD_GUARD, NEW_GUARD)
    if sha256(fixed) != target["source_sha256"]:
        raise RuntimeError(f"FlashInfer FP8 runner postimage mismatch: {target['path']}")
    return fixed


def main() -> int:
    flashinfer_root = Path(sysconfig.get_paths()["purelib"]) / "flashinfer"
    for record in TARGETS:
        target = flashinfer_root / str(record["path"])
        if not target.is_file() or target.is_symlink():
            raise SystemExit(f"FlashInfer FP8 runner source missing or symlinked: {target}")
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
                f"FlashInfer FP8 runner source drift: path={target} observed={before}"
            )

    marker = (
        flashinfer_root
        / "data/csrc/fused_moe/cutlass_backend/.weinfer-fp8-runner-scope.json"
    )
    marker_body = (
        json.dumps(
            {
                "object": "weinfer_flashinfer_fp8_runner_scope_v1",
                "policy": POLICY,
                "sources": [
                    {
                        "path": record["path"],
                        "preimage_sha256": record["preimage_sha256"],
                        "source_sha256": record["source_sha256"],
                    }
                    for record in TARGETS
                ],
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
        "FlashInfer unused FP8 runner branches disabled: "
        f"source_count={len(TARGETS)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
