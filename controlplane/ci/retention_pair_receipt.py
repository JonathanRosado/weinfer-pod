#!/usr/bin/env python3
"""Bind one collected arm to its paid-session policy and watchdog ledger."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time

from retention_pair_compare import GATEWAY_SHA256, WORKLOAD_SHA256


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_json(path: Path, value: object) -> None:
    encoded = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def main() -> int:
    if len(sys.argv) not in (10, 11):
        raise SystemExit(
            "usage: retention_pair_receipt.py <pair-id> <A|B> <run-id> "
            "<quiet-cycles> <session-epoch> <watchdog-state-path> <deploy-log> "
            "<profile-dir> <output-path> [prior-candidate]"
        )
    (
        pair_id,
        arm,
        run_id,
        raw_cycles,
        raw_epoch,
        raw_watchdog,
        raw_deploy,
        raw_profile,
        raw_output,
    ) = sys.argv[1:10]
    prior = Path(sys.argv[10]) if len(sys.argv) == 11 else None
    if arm not in ("A", "B"):
        raise RuntimeError("arm must be A or B")
    quiet_cycles = int(raw_cycles)
    session_epoch = int(raw_epoch)
    if quiet_cycles <= 0 or session_epoch <= 0:
        raise RuntimeError("quiet cycles and session epoch must be positive")
    deploy_path = Path(raw_deploy)
    profile = Path(raw_profile)
    output = Path(raw_output)
    deploy = deploy_path.read_text(errors="replace")
    retention = re.findall(r"retention:\s+demand_quiet_cycles=(\d+)", deploy)
    pods = re.findall(r"pod\s+([a-z0-9]+)\s+created at epoch\s+(\d+)", deploy)
    bases = re.findall(r"public base:\s+(https://[^\s]+)", deploy)
    rates = re.findall(r"pod:\s+[a-z0-9]+\s+\(\$([0-9.]+)/hr", deploy)
    if retention != [str(quiet_cycles)]:
        raise RuntimeError("deploy log does not prove the requested retention policy")
    if len(pods) != 1 or len(bases) != 1 or len(rates) != 1:
        raise RuntimeError("deploy log does not carry one complete control-plane launch")
    control_pod, control_created = pods[0]
    if int(control_created) < session_epoch:
        raise RuntimeError("control-plane creation predates the watchdog session")
    batch = json.loads((profile / "batch_result.json").read_text())
    summary = json.loads((profile / "summary.json").read_text())
    if batch.get("run_id") != run_id or summary.get("run_id") != run_id:
        raise RuntimeError("profile snapshot and receipt run differ")
    if batch.get("workload_sha256") != WORKLOAD_SHA256:
        raise RuntimeError("profile snapshot workload drift")
    if arm == "B" and prior is None:
        raise RuntimeError("Arm B requires Arm A's candidate")
    if arm == "A" and prior is not None:
        raise RuntimeError("Arm A cannot name an in-pair prior")
    watchdog_id = hashlib.sha256(
        f"{pair_id}:{session_epoch}:{Path(raw_watchdog).resolve()}".encode()
    ).hexdigest()
    receipt = {
        "object": "retention_pair_arm",
        "pair_id": pair_id,
        "arm": arm,
        "run_id": run_id,
        "demand_quiet_cycles": quiet_cycles,
        "gateway_sha256": GATEWAY_SHA256,
        "workload_sha256": WORKLOAD_SHA256,
        "session_epoch": session_epoch,
        "watchdog_state_id": watchdog_id,
        "watchdog_state_path_sha256": hashlib.sha256(
            str(Path(raw_watchdog).resolve()).encode()
        ).hexdigest(),
        "control_plane_pod_id": control_pod,
        "control_plane_created_epoch": int(control_created),
        "control_plane_rate_usd_per_hour": rates[0],
        "public_base": bases[0],
        "served_pod_id": summary["pod_id"],
        "profile_candidate_sha256": digest(profile / "profile_candidate.json"),
        "prior_candidate_sha256": digest(prior) if prior is not None else None,
        "deploy_log_sha256": digest(deploy_path),
        "recorded_at_epoch": int(time.time()),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(output, receipt)
    print(json.dumps(receipt, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"RETENTION ARM RECEIPT REFUSED: {error}", file=sys.stderr)
        raise SystemExit(1)
