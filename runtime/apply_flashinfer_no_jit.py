#!/usr/bin/env python3
"""Fail-closed backport of a runtime JIT kill switch to FlashInfer 0.3.1."""

from __future__ import annotations

import hashlib
import json
import os
import sysconfig
from pathlib import Path


PREIMAGE_SHA256 = "67944877a58b087ebfb9502d4626dd094c16d57a6d51a2c8b289e5e6262fb9d3"
POSTIMAGE_SHA256 = "dbca0c0c36d4fd2f559021b5d9c356681501b14a3cf2ec3d37d53d17527dcc7b"
POLICY = "FLASHINFER_DISABLE_JIT=1 refuses individual, load-time, and batch compilation"

REPLACEMENTS = (
    (
        b"""    def build(self, verbose: bool, need_lock: bool = True) -> None:\n        lock = (\n""",
        b"""    def build(self, verbose: bool, need_lock: bool = True) -> None:\n        if os.environ.get(\"FLASHINFER_DISABLE_JIT\") == \"1\":\n            raise RuntimeError(f\"FlashInfer JIT disabled: {self.name}\")\n        lock = (\n""",
    ),
    (
        b"""    def build_and_load(self, class_name: str = None):\n        if self.is_aot:\n            return self.load(self.aot_path, class_name)\n\n        # Guard both build and load with the same lock to avoid race condition\n""",
        b"""    def build_and_load(self, class_name: str = None):\n        if self.is_aot:\n            return self.load(self.aot_path, class_name)\n        if os.environ.get(\"FLASHINFER_DISABLE_JIT\") == \"1\":\n            raise RuntimeError(\n                f\"FlashInfer JIT disabled and AOT artifact absent: {self.name}\"\n            )\n\n        # Guard both build and load with the same lock to avoid race condition\n""",
    ),
    (
        b"""def build_jit_specs(\n    specs: List[JitSpec],\n    verbose: bool = False,\n    skip_prebuilt: bool = True,\n) -> None:\n    lines: List[str] = []\n""",
        b"""def build_jit_specs(\n    specs: List[JitSpec],\n    verbose: bool = False,\n    skip_prebuilt: bool = True,\n) -> None:\n    if os.environ.get(\"FLASHINFER_DISABLE_JIT\") == \"1\":\n        raise RuntimeError(\"FlashInfer batch JIT disabled\")\n    lines: List[str] = []\n""",
    ),
)


def sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def main() -> int:
    purelib = Path(sysconfig.get_paths()["purelib"])
    target = purelib / "flashinfer" / "jit" / "core.py"
    marker = target.parent / ".weinfer-no-runtime-jit.json"
    if not target.is_file() or target.is_symlink():
        raise SystemExit("FlashInfer JIT source missing or symlinked")
    body = target.read_bytes()
    before = sha256(body)
    if before == PREIMAGE_SHA256:
        for old, new in REPLACEMENTS:
            if body.count(old) != 1:
                raise SystemExit("FlashInfer no-JIT transform preimage drift")
            body = body.replace(old, new)
        if sha256(body) != POSTIMAGE_SHA256:
            raise SystemExit("FlashInfer no-JIT transform postimage mismatch")
        temporary = target.with_name(f".{target.name}.tmp.{os.getpid()}")
        temporary.write_bytes(body)
        os.replace(temporary, target)
    elif before != POSTIMAGE_SHA256:
        raise SystemExit(f"FlashInfer JIT source drift: observed={before}")

    marker_body = (
        json.dumps(
            {
                "object": "weinfer_flashinfer_no_runtime_jit_v1",
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
    print(f"FlashInfer runtime JIT disabled: source_sha256={POSTIMAGE_SHA256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
