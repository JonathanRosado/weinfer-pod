#!/usr/bin/env python3
"""Run the realistic shared-context-first series through the proven client.

This is intentionally a separate entrypoint.  The armed retention experiment
hash-pins `public_batch_canary.py`, and the frozen continuity generator must
remain byte-identical.  We reuse its transport, idempotency, billing, and
conservation implementation while injecting only the separately hashed request
rows, then label the resulting artifacts as a non-continuity series.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import prefix_cache_workloads
import public_batch_canary


def artifact_root(run_id: str) -> Path:
    return Path(
        os.environ.get(
            "BATCH_ARTIFACT_DIR",
            str(Path.home() / ".weinfer" / f"public-batch-{run_id}"),
        )
    )


def label_result(run_id: str) -> None:
    root = artifact_root(run_id)
    pointer_path = root / "run.json"
    pointer = json.loads(pointer_path.read_text())
    if (
        pointer.get("workload_sha256")
        != prefix_cache_workloads.REALISTIC_AGENT_WORKLOAD_SHA256
    ):
        raise RuntimeError("realistic workload pointer carries the wrong sha256")
    result_path = Path(pointer["latest_observation"]) / "result.json"
    result = json.loads(result_path.read_text())
    prediction = prefix_cache_workloads.cache_prediction(
        prefix_cache_workloads.REALISTIC_AGENT_WORKLOAD
    )
    result.update(
        {
            "workload_variant": prefix_cache_workloads.REALISTIC_AGENT_WORKLOAD,
            "continuity_anchor": False,
            "cache_prediction": prediction,
        }
    )
    public_batch_canary.atomic_json(result_path, result)
    pointer["workload_variant"] = prefix_cache_workloads.REALISTIC_AGENT_WORKLOAD
    pointer["continuity_anchor"] = False
    public_batch_canary.atomic_json(pointer_path, pointer)
    public_batch_canary.atomic_json(
        root / "workload-contract.json", prefix_cache_workloads.contract()
    )


def main() -> int:
    run_id = os.environ.get("BATCH_RUN_ID", "")
    if not run_id:
        raise SystemExit("BATCH_RUN_ID is required")

    def realistic_workload():
        return prefix_cache_workloads.workload(
            prefix_cache_workloads.REALISTIC_AGENT_WORKLOAD
        )

    # The base client's comparison and artifact paths read this module
    # constant and function.  Rebinding in this separate process changes
    # no frozen source bytes and no other run.
    public_batch_canary.EXPECTED_WORKLOAD_SHA256 = (
        prefix_cache_workloads.REALISTIC_AGENT_WORKLOAD_SHA256
    )
    public_batch_canary.workload = realistic_workload
    outcome = public_batch_canary.main()
    if outcome == 0:
        label_result(run_id)
    return outcome


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"REALISTIC AGENT BATCH FAILED: {error}", file=sys.stderr)
        raise
