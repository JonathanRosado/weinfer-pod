#!/usr/bin/env python3
"""Fail-closed comparison for the same-session retention A/B pair.

Each arm directory contains the mechanically collected public-batch profile
snapshot plus a small operator receipt.  This program does no networking and
creates no capacity.  It will not report a ratio unless both arms are complete,
same-identity, sequential on distinct pods, chained through one boot-sample
provenance, and carry the pre-registered policy values.
"""

from __future__ import annotations

from fractions import Fraction
import hashlib
import json
from pathlib import Path
import sys
from typing import Any


WORKLOAD_SHA256 = "2392bb588923e88dc3f1473a9393a0e099a19a4889ccbd1d945b33df9e5ed205"
GATEWAY_SHA256 = "e94bd6e6a87c5c802f0c8339db78c195923847e43321488cb531d5918b6f041e"
ARM_A_QUIET_CYCLES = 1_000_000
ARM_B_QUIET_CYCLES = 3
EXPECTED_JOBS = 300
MIN_TOKENS = 1_000_000
REGISTERED_MAX_RATIO = Fraction(9, 10)
CONTINUITY_BASELINE_CHARGE = 64_285
CONTINUITY_BASELINE_TOKENS = 1_199_544


def load(path: Path) -> Any:
    return json.loads(path.read_text())


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def first_jsonl(path: Path) -> dict[str, Any]:
    with path.open() as handle:
        line = handle.readline()
    if not line:
        raise RuntimeError(f"empty evidence stream: {path}")
    return json.loads(line)


def arm(root: Path, expected_arm: str) -> dict[str, Any]:
    receipt_path = root / "arm_receipt.json"
    summary_path = root / "summary.json"
    candidate_path = root / "profile_candidate.json"
    batch_path = root / "batch_result.json"
    raw_path = root / "raw_profile_evidence.jsonl"
    manifest_path = root / "MANIFEST.json"
    required = [receipt_path, summary_path, candidate_path, batch_path, raw_path, manifest_path]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError(f"arm {expected_arm} incomplete: {missing}")

    manifest = load(manifest_path)
    if manifest.get("status") != "candidate":
        raise RuntimeError(f"arm {expected_arm} profile is not a candidate")
    for name, expected in manifest.get("files", {}).items():
        path = root / name
        if not path.is_file() or sha(path) != expected:
            raise RuntimeError(f"arm {expected_arm} manifest mismatch for {name}")

    receipt = load(receipt_path)
    summary = load(summary_path)
    candidate = load(candidate_path)
    batch = load(batch_path)
    raw = first_jsonl(raw_path)
    if receipt.get("object") != "retention_pair_arm" or receipt.get("arm") != expected_arm:
        raise RuntimeError(f"arm {expected_arm} receipt identity mismatch")
    if receipt.get("run_id") != batch.get("run_id") or summary.get("run_id") != batch.get("run_id"):
        raise RuntimeError(f"arm {expected_arm} run identity mismatch")
    if batch.get("workload_sha256") != WORKLOAD_SHA256:
        raise RuntimeError(f"arm {expected_arm} workload drift")
    if batch.get("completed_jobs") != EXPECTED_JOBS or summary.get("jobs") != EXPECTED_JOBS:
        raise RuntimeError(f"arm {expected_arm} did not complete exactly 300 jobs")
    if int(summary.get("billable_tokens", 0)) < MIN_TOKENS:
        raise RuntimeError(f"arm {expected_arm} is below the token floor")
    if int(summary.get("provider_charge_micro_usd", 0)) <= 0:
        raise RuntimeError(f"arm {expected_arm} has no positive provider charge")
    if summary.get("pod_id") != raw.get("evidence", {}).get("pod_id"):
        raise RuntimeError(f"arm {expected_arm} pod attribution mismatch")
    return {
        "root": root,
        "receipt": receipt,
        "summary": summary,
        "candidate": candidate,
        "batch": batch,
        "raw": raw,
        "candidate_sha256": sha(candidate_path),
        "launch_contract_digest": raw.get("launch_contract_digest"),
    }


def as_decimal(value: Fraction) -> str:
    return f"{float(value):.12f}"


def compare(pair_id: str, arm_a_root: Path, arm_b_root: Path) -> dict[str, Any]:
    a = arm(arm_a_root, "A")
    b = arm(arm_b_root, "B")
    for value in (a, b):
        receipt = value["receipt"]
        if receipt.get("pair_id") != pair_id:
            raise RuntimeError("pair id differs across arm receipts")
        if receipt.get("gateway_sha256") != GATEWAY_SHA256:
            raise RuntimeError("arm did not execute the registered v0.9.0 gateway")
        if receipt.get("workload_sha256") != WORKLOAD_SHA256:
            raise RuntimeError("receipt workload differs from the frozen bytes")

    if int(a["receipt"].get("demand_quiet_cycles", 0)) != ARM_A_QUIET_CYCLES:
        raise RuntimeError("Arm A is not the registered parity control")
    if int(b["receipt"].get("demand_quiet_cycles", 0)) != ARM_B_QUIET_CYCLES:
        raise RuntimeError("Arm B is not the registered demand-aware policy")
    if a["receipt"].get("session_epoch") != b["receipt"].get("session_epoch"):
        raise RuntimeError("arms do not share one watchdog session epoch")
    if a["receipt"].get("watchdog_state_id") != b["receipt"].get("watchdog_state_id"):
        raise RuntimeError("arms do not share one cumulative watchdog ledger")
    if a["summary"]["pod_id"] == b["summary"]["pod_id"]:
        raise RuntimeError("the two arms did not use fresh pods")
    if a["launch_contract_digest"] != b["launch_contract_digest"]:
        raise RuntimeError("launch contract differs across arms")
    if (
        a["candidate"]["profile_facts"]["identity"]
        != b["candidate"]["profile_facts"]["identity"]
    ):
        raise RuntimeError("exact profile identity differs across arms")

    a_terminated = int(a["raw"]["evidence"]["terminated_at_micros"])
    b_started = int(b["batch"]["timestamps_micros"]["started"])
    if a_terminated > b_started:
        raise RuntimeError("Arm B began before Arm A was durably terminated")
    prior_source = b["candidate"]["derivation"].get("prior_profile_source")
    if not isinstance(prior_source, str) or Path(prior_source).name != "profile_candidate.json":
        raise RuntimeError("Arm B did not name a profile candidate as its prior")
    if b["receipt"].get("prior_candidate_sha256") != a["candidate_sha256"]:
        raise RuntimeError("Arm B receipt does not hash-bind Arm A as its prior")
    sources = b["candidate"]["derivation"].get("boot_sample_sources", [])
    if f"public-batch:{a['batch']['run_id']}" not in sources:
        raise RuntimeError("Arm A boot sample is absent from Arm B provenance")
    if f"public-batch:{b['batch']['run_id']}" not in sources:
        raise RuntimeError("Arm B boot sample is absent from Arm B provenance")

    a_charge = int(a["summary"]["provider_charge_micro_usd"])
    a_tokens = int(a["summary"]["billable_tokens"])
    b_charge = int(b["summary"]["provider_charge_micro_usd"])
    b_tokens = int(b["summary"]["billable_tokens"])
    ratio = Fraction(b_charge * a_tokens, a_charge * b_tokens)
    validated = ratio <= REGISTERED_MAX_RATIO
    a_vs_continuity = Fraction(
        a_charge * CONTINUITY_BASELINE_TOKENS,
        CONTINUITY_BASELINE_CHARGE * a_tokens,
    )
    b_vs_continuity = Fraction(
        b_charge * CONTINUITY_BASELINE_TOKENS,
        CONTINUITY_BASELINE_CHARGE * b_tokens,
    )
    return {
        "object": "retention_pair_verdict",
        "pair_id": pair_id,
        "valid_pair": True,
        "registered_rule": {
            "predicate": "B delivered USD/Mtoken <= 0.90 * A delivered USD/Mtoken",
            "exact_integer_form": "10 * B_charge * A_tokens <= 9 * A_charge * B_tokens",
            "threshold_numerator": 9,
            "threshold_denominator": 10,
            "validated": validated,
            "verdict": "validated" if validated else "not_validated",
        },
        "arms": {
            "A": {
                "run_id": a["batch"]["run_id"],
                "pod_id": a["summary"]["pod_id"],
                "demand_quiet_cycles": ARM_A_QUIET_CYCLES,
                "provider_charge_micro_usd": a_charge,
                "billable_tokens": a_tokens,
                # micro-USD/token is numerically USD/million-token.
                "delivered_usd_per_mtok": a_charge / a_tokens,
                "candidate_sha256": a["candidate_sha256"],
            },
            "B": {
                "run_id": b["batch"]["run_id"],
                "pod_id": b["summary"]["pod_id"],
                "demand_quiet_cycles": ARM_B_QUIET_CYCLES,
                "provider_charge_micro_usd": b_charge,
                "billable_tokens": b_tokens,
                "delivered_usd_per_mtok": b_charge / b_tokens,
                "candidate_sha256": b["candidate_sha256"],
                "prior_candidate_sha256": b["receipt"]["prior_candidate_sha256"],
            },
        },
        "exact_ratio": {
            "numerator": ratio.numerator,
            "denominator": ratio.denominator,
            "decimal": as_decimal(ratio),
        },
        "continuity_baseline": {
            "charge_micro_usd": CONTINUITY_BASELINE_CHARGE,
            "billable_tokens": CONTINUITY_BASELINE_TOKENS,
            "delivered_usd_per_mtok": CONTINUITY_BASELINE_CHARGE
            / CONTINUITY_BASELINE_TOKENS,
            "A_ratio": as_decimal(a_vs_continuity),
            "B_ratio": as_decimal(b_vs_continuity),
        },
        "shared": {
            "gateway_sha256": GATEWAY_SHA256,
            "workload_sha256": WORKLOAD_SHA256,
            "profile_identity": a["candidate"]["profile_facts"]["identity"],
            "launch_contract_digest": a["launch_contract_digest"],
            "session_epoch": a["receipt"]["session_epoch"],
            "watchdog_state_id": a["receipt"]["watchdog_state_id"],
        },
    }


def main() -> int:
    if len(sys.argv) != 5:
        raise SystemExit(
            "usage: retention_pair_compare.py <pair-id> <arm-a-profile-dir> "
            "<arm-b-profile-dir> <output-json>"
        )
    pair_id, a_root, b_root, output = sys.argv[1:]
    verdict = compare(pair_id, Path(a_root), Path(b_root))
    encoded = (json.dumps(verdict, sort_keys=True, indent=2) + "\n").encode()
    Path(output).write_bytes(encoded)
    print(json.dumps(verdict, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"RETENTION PAIR INVALID: {error}", file=sys.stderr)
        raise SystemExit(1)
