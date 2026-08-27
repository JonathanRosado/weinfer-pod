#!/usr/bin/env python3
"""Bind the realistic N=24 session to the anchor launch identity.

This checker emits identity evidence only. It deliberately computes no
cross-workload cost, throughput, cache, or quality comparison.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import os
import sys
import tempfile
from typing import Any


ANCHOR_SHA256 = "2392bb588923e88dc3f1473a9393a0e099a19a4889ccbd1d945b33df9e5ed205"
REALISTIC_SHA256 = "b9cc41bb6b9985bd077ca4204a4c6f0c16e1012410919f5a3514e5ff3219d6e5"
BINDING_FIELDS = ("pool", "model", "gpu_sku", "launch_contract_digest")


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_once(path: Path, payload: bytes) -> None:
    if path.exists():
        raise FileExistsError(f"refusing to replace identity proof: {path}")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.link(temporary, path)
        os.unlink(temporary)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def main() -> int:
    if len(sys.argv) != 4:
        raise SystemExit(
            "usage: stacked_n24_identity_check.py "
            "<anchor-terminal-receipt> <realistic-terminal-receipt> <output-json>"
        )
    anchor_path, realistic_path, output_path = map(
        lambda raw: Path(raw).resolve(), sys.argv[1:]
    )
    output_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    anchor = load(anchor_path)
    realistic = load(realistic_path)
    for value, variant, workload in (
        (anchor, "anchor", ANCHOR_SHA256),
        (realistic, "realistic", REALISTIC_SHA256),
    ):
        if (
            value.get("status") != "complete"
            or value.get("variant") != variant
            or value.get("workload_sha256") != workload
            or value.get("logical_batches") != 24
            or value.get("accepted_jobs") != 7_200
            or value.get("completed_jobs") != 7_200
            or value.get("accepted_before_provider_create") is not True
        ):
            raise ValueError(f"{variant} terminal receipt is not a complete N=24 series")
    anchor_identity = anchor.get("pod_identity", {})
    realistic_identity = realistic.get("pod_identity", {})
    if not all(
        isinstance(anchor_identity.get(field), str) and anchor_identity.get(field)
        for field in BINDING_FIELDS
    ) or not all(
        isinstance(realistic_identity.get(field), str) and realistic_identity.get(field)
        for field in BINDING_FIELDS
    ):
        raise ValueError("one series lacks a complete launch identity")
    mismatches = {
        field: {
            "anchor": anchor_identity.get(field),
            "realistic": realistic_identity.get(field),
        }
        for field in BINDING_FIELDS
        if anchor_identity.get(field) != realistic_identity.get(field)
    }
    if mismatches:
        raise ValueError(f"realistic session launch identity mismatch: {mismatches}")
    proof = {
        "status": "identity_bound",
        "anchor_terminal_sha256": sha256(anchor_path),
        "realistic_terminal_sha256": sha256(realistic_path),
        "binding_fields": {field: anchor_identity[field] for field in BINDING_FIELDS},
        "anchor_pod_id": anchor_identity.get("pod_id"),
        "realistic_pod_id": realistic_identity.get("pod_id"),
        "anchor_provider_rate_micro_per_hour": anchor_identity.get(
            "provider_rate_micro_per_hour"
        ),
        "realistic_provider_rate_micro_per_hour": realistic_identity.get(
            "provider_rate_micro_per_hour"
        ),
        "cross_workload_numeric_comparison_emitted": False,
    }
    write_once(
        output_path, (json.dumps(proof, indent=2, sort_keys=True) + "\n").encode()
    )
    print("STACKED N24 IDENTITY PASS: realistic session matches anchor launch contract")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"STACKED N24 IDENTITY REFUSED: {error}", file=sys.stderr)
        raise SystemExit(1)
