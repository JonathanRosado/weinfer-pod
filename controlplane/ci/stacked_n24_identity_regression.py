#!/usr/bin/env python3
"""Zero-provider regression for N=24 cross-session identity binding."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile


SOURCE = Path(__file__).resolve().parent / "stacked_n24_identity_check.py"
ANCHOR_SHA = "2392bb588923e88dc3f1473a9393a0e099a19a4889ccbd1d945b33df9e5ed205"
REALISTIC_SHA = "b9cc41bb6b9985bd077ca4204a4c6f0c16e1012410919f5a3514e5ff3219d6e5"


def receipt(variant: str, workload: str, digest: str = "d" * 64) -> dict:
    return {
        "status": "complete",
        "variant": variant,
        "workload_sha256": workload,
        "logical_batches": 24,
        "accepted_jobs": 7_200,
        "completed_jobs": 7_200,
        "accepted_before_provider_create": True,
        "pod_identity": {
            "pod_id": f"pod-{variant}",
            "pool": "community-qwen7b-0",
            "model": "Qwen/Qwen2.5-7B-Instruct",
            "gpu_sku": "NVIDIA GeForce RTX 4090",
            "launch_contract_digest": digest,
            "provider_rate_micro_per_hour": 340_000,
        },
    }


def invoke(anchor: Path, realistic: Path, output: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SOURCE), str(anchor), str(realistic), str(output)],
        capture_output=True,
        text=True,
        check=False,
    )


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="stacked-n24-identity-") as temporary:
        root = Path(temporary).resolve()
        anchor = root / "anchor.json"
        realistic = root / "realistic.json"
        output = root / "proof.json"
        anchor.write_text(json.dumps(receipt("anchor", ANCHOR_SHA)))
        realistic.write_text(json.dumps(receipt("realistic", REALISTIC_SHA)))
        passed = invoke(anchor, realistic, output)
        assert passed.returncode == 0, passed.stderr
        proof = json.loads(output.read_text())
        assert proof["status"] == "identity_bound"
        assert proof["anchor_pod_id"] != proof["realistic_pod_id"]
        assert proof["cross_workload_numeric_comparison_emitted"] is False
        assert output.stat().st_mode & 0o777 == 0o600

        mismatch = root / "realistic-mismatch.json"
        mismatch.write_text(
            json.dumps(receipt("realistic", REALISTIC_SHA, digest="e" * 64))
        )
        refused_output = root / "refused.json"
        refused = invoke(anchor, mismatch, refused_output)
        assert refused.returncode != 0
        assert "launch identity mismatch" in refused.stderr
        assert not refused_output.exists()

        repeated = invoke(anchor, realistic, output)
        assert repeated.returncode != 0
        assert "refusing to replace" in repeated.stderr

    print(
        "STACKED N24 IDENTITY REGRESSION PASS: exact launch binding; different "
        "physical pods allowed; mismatch and proof overwrite refuse; no numeric ratio"
    )


if __name__ == "__main__":
    main()
