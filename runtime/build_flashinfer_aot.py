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


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
        gen_cutlass_fused_moe_sm90_module(),
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
