#!/usr/bin/env python3
"""Zero-dollar executable contract for the retention pair."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

from retention_pair_compare import (
    ARM_A_QUIET_CYCLES,
    ARM_B_QUIET_CYCLES,
    GATEWAY_SHA256,
    WORKLOAD_SHA256,
    compare,
)


IDENTITY = {
    "served_model": "Qwen/Qwen2.5-7B-Instruct",
    "model_revision": "a09a35458c702b33eeacc393d103063234e8bc28",
    "tokenizer_revision": "a09a35458c702b33eeacc393d103063234e8bc28",
    "image_digest": "image@sha256:one",
    "engine_config_digest": "engine-one",
    "gpu_sku": "NVIDIA RTX A4500",
    "cuda_class": "12",
}
LAUNCH_DIGEST = "a" * 64
DEPLOY_SCRIPT = os.environ.get("DEPLOY_SCRIPT", "scripts/deploy_controlplane.sh")


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture(
    root: Path,
    *,
    pair_id: str,
    arm_name: str,
    run_id: str,
    quiet_cycles: int,
    pod_id: str,
    charge: int,
    tokens: int,
    started: int,
    terminated: int,
    arm_a_candidate: Path | None,
) -> None:
    root.mkdir()
    batch = {
        "run_id": run_id,
        "workload_sha256": WORKLOAD_SHA256,
        "completed_jobs": 300,
        "billable_tokens": tokens,
        "timestamps_micros": {"started": started, "all_completed": started + 1_000},
    }
    summary = {
        "run_id": run_id,
        "pod_id": pod_id,
        "jobs": 300,
        "billable_tokens": tokens,
        "provider_charge_micro_usd": charge,
    }
    sources = ["sealed-sample-1", "sealed-sample-2", f"public-batch:{run_id}"]
    prior = "sealed-profile.json"
    if arm_a_candidate is not None:
        sources = [
            "sealed-sample-1",
            "sealed-sample-2",
            "public-batch:pair-regression-A",
            f"public-batch:{run_id}",
        ]
        prior = str(arm_a_candidate)
    candidate = {
        "status": "candidate_only",
        "profile_facts": {"identity": IDENTITY},
        "derivation": {
            "prior_profile_source": prior,
            "boot_sample_sources": sources,
        },
    }
    raw = {
        "launch_contract_digest": LAUNCH_DIGEST,
        "evidence": {
            "pod_id": pod_id,
            "terminated_at_micros": terminated,
        },
    }
    receipt = {
        "object": "retention_pair_arm",
        "pair_id": pair_id,
        "arm": arm_name,
        "run_id": run_id,
        "demand_quiet_cycles": quiet_cycles,
        "gateway_sha256": GATEWAY_SHA256,
        "workload_sha256": WORKLOAD_SHA256,
        "session_epoch": 1_787_650_000,
        "watchdog_state_id": "retention-pair-regression-ledger",
        "prior_candidate_sha256": (
            file_sha(arm_a_candidate) if arm_a_candidate is not None else None
        ),
    }
    write_json(root / "arm_receipt.json", receipt)
    write_json(root / "summary.json", summary)
    write_json(root / "profile_candidate.json", candidate)
    write_json(root / "batch_result.json", batch)
    (root / "raw_profile_evidence.jsonl").write_text(json.dumps(raw, sort_keys=True) + "\n")
    names = [
        "summary.json",
        "profile_candidate.json",
        "batch_result.json",
        "raw_profile_evidence.jsonl",
    ]
    write_json(
        root / "MANIFEST.json",
        {"status": "candidate", "files": {name: file_sha(root / name) for name in names}},
    )


def rendered_env(cycles: int | None) -> dict[str, str]:
    env = os.environ.copy()
    env.update({"ADMIN_KEY": "a", "CUSTOMER_KEY": "c", "WORKER_KEY": "w"})
    if cycles is None:
        env.pop("WEINFER_DEMAND_QUIET_CYCLES", None)
    else:
        env["WEINFER_DEMAND_QUIET_CYCLES"] = str(cycles)
    result = subprocess.run(
        ["bash", DEPLOY_SCRIPT, "--render-env"],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stdout + result.stderr)
    return json.loads(result.stdout)


def run() -> None:
    default = rendered_env(None)
    parity = rendered_env(ARM_A_QUIET_CYCLES)
    assert default["WEINFER_DEMAND_QUIET_CYCLES"] == str(ARM_B_QUIET_CYCLES)
    assert parity["WEINFER_DEMAND_QUIET_CYCLES"] == str(ARM_A_QUIET_CYCLES)
    differing = {key for key in default if default[key] != parity.get(key)}
    assert differing == {"WEINFER_DEMAND_QUIET_CYCLES"}, differing

    bad_env = os.environ.copy()
    bad_env.update(
        {
            "ADMIN_KEY": "a",
            "CUSTOMER_KEY": "c",
            "WORKER_KEY": "w",
            "WEINFER_DEMAND_QUIET_CYCLES": "0",
        }
    )
    invalid = subprocess.run(
        ["bash", DEPLOY_SCRIPT, "--render-env"],
        env=bad_env,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert invalid.returncode != 0
    assert "positive base-10 integer" in invalid.stderr
    assert "trust roots consistent" not in invalid.stderr

    with tempfile.TemporaryDirectory(prefix="weinfer-retention-pair-") as temp:
        root = Path(temp)
        a = root / "A"
        b = root / "B"
        fixture(
            a,
            pair_id="pair-regression",
            arm_name="A",
            run_id="pair-regression-A",
            quiet_cycles=ARM_A_QUIET_CYCLES,
            pod_id="pod-a",
            charge=100_000,
            tokens=1_200_000,
            started=2_000_000,
            terminated=3_000_000,
            arm_a_candidate=None,
        )
        fixture(
            b,
            pair_id="pair-regression",
            arm_name="B",
            run_id="pair-regression-B",
            quiet_cycles=ARM_B_QUIET_CYCLES,
            pod_id="pod-b",
            charge=90_000,
            tokens=1_200_000,
            started=4_000_000,
            terminated=5_000_000,
            arm_a_candidate=a / "profile_candidate.json",
        )
        verdict = compare("pair-regression", a, b)
        assert verdict["registered_rule"]["validated"] is True
        assert verdict["exact_ratio"] == {
            "numerator": 9,
            "denominator": 10,
            "decimal": "0.900000000000",
        }
        assert verdict["arms"]["A"]["delivered_usd_per_mtok"] == 1 / 12

        summary_path = b / "summary.json"
        summary = json.loads(summary_path.read_text())
        summary["provider_charge_micro_usd"] = 90_001
        write_json(summary_path, summary)
        manifest = load_json(b / "MANIFEST.json")
        manifest["files"]["summary.json"] = file_sha(summary_path)
        write_json(b / "MANIFEST.json", manifest)
        verdict = compare("pair-regression", a, b)
        assert verdict["registered_rule"]["validated"] is False
        assert verdict["exact_ratio"]["decimal"] > "0.900000000000"

        try:
            compare("pair-regression", a, root / "missing-B")
        except RuntimeError as error:
            assert "incomplete" in str(error)
        else:
            raise AssertionError("one-arm measurement produced a verdict")

        candidate_path = b / "profile_candidate.json"
        candidate = load_json(candidate_path)
        candidate["profile_facts"]["identity"]["gpu_sku"] = "NVIDIA RTX 4090"
        write_json(candidate_path, candidate)
        manifest = load_json(b / "MANIFEST.json")
        manifest["files"]["profile_candidate.json"] = file_sha(candidate_path)
        write_json(b / "MANIFEST.json", manifest)
        try:
            compare("pair-regression", a, b)
        except RuntimeError as error:
            assert "identity differs" in str(error)
        else:
            raise AssertionError("cross-identity pair did not fail closed")

    print(
        "RETENTION PAIR REGRESSION PASS: same-binary arm envs, parity cap, "
        "exact 0.90 rule, one-arm/cross-identity fail-closed"
    )


def load_json(path: Path) -> dict:
    return json.loads(path.read_text())


if __name__ == "__main__":
    run()
