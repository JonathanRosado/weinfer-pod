#!/usr/bin/env python3
"""Admit and observe one registered 24-batch serving series.

The coordinator starts one proven 300-job canary at a time, waits for its
durable 300/300 acceptance marker, then starts the next while all prior
canaries continue polling.  It never chooses or creates provider capacity.
Acquisition remains entirely inside the shipped gateway.

Run this coordinator itself as a launchd one-shot.  Its artifact directory is
write-once: a crashed or interrupted series is evidence, never implicitly
resumed under the same identity.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any

from public_batch_canary import Client, atomic_json, parse_admin_key
from prefix_cache_workloads import REALISTIC_AGENT_WORKLOAD_SHA256


BATCHES = 24
JOBS_PER_BATCH = 300
DEADLINE_SECONDS = 14_400
ADMISSION_LIMIT_SECONDS = 600
POLL_BUDGET_SECONDS = DEADLINE_SECONDS + 600
POLL_CONCURRENCY = 8
POLL_INTERVAL_SECONDS = 60
PREFLIGHT_FRESHNESS_SECONDS = 300
REGISTERED_BACKLOG_TOKENS = 39_218_400
ACQUISITION_LIMIT_MICROS = 600_000_000
PLACEMENT_AUDIT_LIMIT = 2_000
PLACEMENT_AUDIT_COMPACT_ROW_KEYS = frozenset(
    {
        "pool",
        "at_micros",
        "backlog_tokens",
        "selected_sku",
        "attempt_outcome",
        "plan_id",
        "attempt_ordinal",
        "matched_pod_count",
        "pod_id",
        "pod_created_at_micros",
        "launch_contract_digest",
    }
)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ANCHOR_SHA256 = "2392bb588923e88dc3f1473a9393a0e099a19a4889ccbd1d945b33df9e5ed205"
ACCEPTED_MARKER = "300/300 accepted; row-0 replay identical"
ARMING_RECEIPT_ENV = "WEINFER_STACKED_N24_ARMING_RECEIPT"
ARMING_RECEIPT_WAIT_SECONDS = 10
ARMING_RECEIPT_MAX_AGE_SECONDS = 30


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def require_pre_spend_receipt(
    path: Path, *, variant: str, run_prefix: str, expected_sha256: str
) -> tuple[dict[str, Any], bytes, str]:
    raw = path.read_bytes()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RuntimeError("pre-spend receipt is not JSON") from error
    if not isinstance(value, dict):
        raise RuntimeError("pre-spend receipt is not an object")
    workload = value.get("workload", {})
    scheduling = value.get("scheduling", {})
    observer = value.get("observer", {})
    predictions = value.get("predictions", {})
    ceilings = value.get("ceilings", {})
    captured_at = value.get("captured_at_epoch")
    now = int(time.time())
    if (
        value.get("object") != "stacked_n24_pre_spend_receipt_v1"
        or value.get("variant") != variant
        or value.get("run_id") != run_prefix
        or workload.get("sha256") != expected_sha256
        or workload.get("logical_batches") != BATCHES
        or workload.get("jobs_per_batch") != JOBS_PER_BATCH
        or workload.get("deadline_seconds") != DEADLINE_SECONDS
        or scheduling.get("admission_limit_seconds") != ADMISSION_LIMIT_SECONDS
        or scheduling.get("boot_fraction") != "0.04"
        or scheduling.get("boot_seed_seconds") != 452
        or scheduling.get("policy_tokens_per_second") != 4_000
        or not isinstance(value.get("registered_commit"), str)
        or len(value["registered_commit"]) != 40
        or not all(character in "0123456789abcdef" for character in value["registered_commit"])
        or not isinstance(captured_at, int)
        or captured_at <= 0
        or captured_at > now + 5
        or now - captured_at > PREFLIGHT_FRESHNESS_SECONDS
        or not isinstance(observer.get("pre_launch_epoch"), int)
        or observer["pre_launch_epoch"] <= 0
        or predictions.get("rows") != 9
        or predictions.get("runtime_rate_authority")
        != "managed_pods.provider_rate_micro_per_hour"
        or predictions.get("rate_parameter_domain_micro_usd_per_hour")
        != [1, 400_000]
        or predictions.get("all_create_rates_under_the_ceiling_are_precommitted")
        is not True
        or ceilings.get("gpu_create_usd_per_hour") != "0.40"
        or ceilings.get("gpu_cumulative_usd") != "1.00"
        or ceilings.get("control_plane_create_usd_per_hour") != "0.10"
    ):
        raise RuntimeError("pre-spend receipt does not bind this registered series")
    expected_shape = "full_nine_row_queue" if variant == "anchor" else "target_only"
    if value.get("rendered_environment", {}).get("bootstrap_shape") != expected_shape:
        raise RuntimeError("pre-spend receipt bootstrap shape differs from this series")
    registered_sources = value.get("registered_sources")
    if not isinstance(registered_sources, dict):
        raise RuntimeError("pre-spend receipt has no registered source authorities")
    source_authorities = {
        "product/scripts/arm_stacked_n24_admit_launchd.py": (
            PROJECT_ROOT / "scripts/arm_stacked_n24_admit_launchd.py"
        ),
        "product/scripts/stacked_n24_admit.py": Path(__file__).resolve(),
        "product/scripts/public_batch_canary.py": (
            PROJECT_ROOT / "scripts/public_batch_canary.py"
        ),
    }
    if variant == "realistic":
        source_authorities["product/scripts/realistic_agent_batch_canary.py"] = (
            PROJECT_ROOT / "scripts/realistic_agent_batch_canary.py"
        )
    for relative, source_path in source_authorities.items():
        if registered_sources.get(relative) != sha256(source_path):
            raise RuntimeError(
                f"pre-spend receipt does not bind current source bytes: {relative}"
            )
    return value, raw, hashlib.sha256(raw).hexdigest()


def require_launchd_arming_receipt(
    *,
    variant: str,
    public_base: str,
    credentials: Path,
    run_prefix: str,
    artifact_root: Path,
    pre_spend_path: Path,
    pre_spend: dict[str, Any],
    pre_spend_sha256: str,
) -> tuple[dict[str, Any], bytes, str]:
    """Hold all customer demand until the source-pinned armer proves one-shot start.

    The armer has to start this process before launchd can prove it running.  The
    coordinator therefore waits here, before creating its artifact root or
    issuing a request, until the armer atomically publishes the receipt.  This
    closes the otherwise dangerous race where a failed arming proof could leave
    durable queued demand capable of buying a GPU without an arming receipt.
    """

    expected_label = f"com.weinfer.{run_prefix}.stacked-n24-admit"
    if os.environ.get("XPC_SERVICE_NAME") != expected_label:
        raise RuntimeError(
            "coordinator must run under the exact registered launchd label"
        )
    receipt_text = os.environ.get(ARMING_RECEIPT_ENV, "")
    receipt_path = Path(receipt_text)
    if not receipt_text or not receipt_path.is_absolute():
        raise RuntimeError("coordinator arming-receipt gate path is absent or relative")

    deadline = time.monotonic() + ARMING_RECEIPT_WAIT_SECONDS
    while not os.path.lexists(receipt_path):
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "coordinator arming receipt did not appear before the demand gate timed out"
            )
        time.sleep(0.05)
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise RuntimeError("coordinator arming receipt is not an immutable regular file")
    if (receipt_path.stat().st_mode & 0o777) != 0o600:
        raise RuntimeError("coordinator arming receipt is not private 0600")
    try:
        raw = receipt_path.read_bytes()
        value = json.loads(raw)
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("coordinator arming receipt is not readable JSON") from error
    if not isinstance(value, dict):
        raise RuntimeError("coordinator arming receipt is not an object")

    registered_sources = pre_spend.get("registered_sources", {})
    armer_path = PROJECT_ROOT / "scripts/arm_stacked_n24_admit_launchd.py"
    armer_sha256 = sha256(armer_path)
    coordinator_sha256 = sha256(Path(__file__).resolve())
    identities = value.get("coordinator_argv_identities")
    armed_at = value.get("armed_at_epoch")
    now = int(time.time())
    proof_digests = (
        value.get("launchctl_proof_sha256"),
        value.get("launchctl_recheck_proof_sha256"),
    )
    if (
        value.get("object")
        != "stacked_n24_admit_launchd_arming_receipt_v1"
        or value.get("attests") != "launchd_one_shot_started"
        or value.get("does_not_attest")
        != "coordinator_completion_or_series_success"
        or value.get("label") != expected_label
        or value.get("teardown_required_bootout_target")
        != f"gui/{os.getuid()}/{expected_label}"
        or value.get("job_remains_loaded_after_coordinator_exit") is not True
        or value.get("launchctl_submit_or_kickstart_used") is not False
        or value.get("coordinator_blocks_before_arming_receipt") is not True
        or value.get("coordinator_arming_gate_seconds")
        != ARMING_RECEIPT_WAIT_SECONDS
        or value.get("armer_nominal_proof_budget_seconds") != 5.0
        or value.get("armer_proof_execution_margin_seconds") != 2
        or value["armer_nominal_proof_budget_seconds"]
        + value["armer_proof_execution_margin_seconds"]
        >= ARMING_RECEIPT_WAIT_SECONDS
        or value.get("credentials_file_contents_read") is not False
        or value.get("credentials_file_digest_recorded") is not False
        or value.get("launchctl_proof_text_persisted") is not False
        or value.get("coordinator_source_sha256") != coordinator_sha256
        or value.get("armer_source_sha256") != armer_sha256
        or registered_sources.get(
            "product/scripts/arm_stacked_n24_admit_launchd.py"
        )
        != armer_sha256
        or value.get("pre_spend_receipt_sha256") != pre_spend_sha256
        or type(armed_at) is not int
        or armed_at <= 0
        or armed_at > now + 5
        or now - armed_at > ARMING_RECEIPT_MAX_AGE_SECONDS
        or not all(
            isinstance(digest, str)
            and len(digest) == 64
            and all(character in "0123456789abcdef" for character in digest)
            for digest in proof_digests
        )
        or not isinstance(identities, dict)
    ):
        raise RuntimeError("coordinator arming receipt is incoherent")
    try:
        paths_match = (
            Path(str(identities.get("credentials_file_path"))).resolve()
            == credentials
            and Path(str(identities.get("artifact_root"))).resolve()
            == artifact_root
            and Path(str(identities.get("pre_spend_receipt"))).resolve()
            == pre_spend_path
            and Path(str(value.get("pre_spend_receipt_path"))).resolve()
            == pre_spend_path
        )
    except (OSError, RuntimeError, ValueError) as error:
        raise RuntimeError("coordinator arming receipt paths are incoherent") from error
    if not paths_match or identities.get("variant") != variant or identities.get(
        "public_base"
    ) != public_base or identities.get("run_prefix") != run_prefix:
        raise RuntimeError("coordinator arming receipt binds a different series")
    return value, raw, hashlib.sha256(raw).hexdigest()


def write_once_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.parent / f".{path.name}.{os.getpid()}.tmp"
    if path.exists() or temporary.exists():
        raise RuntimeError(f"refusing to replace immutable artifact: {path}")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        written = os.write(descriptor, payload)
        if written != len(payload):
            raise RuntimeError(
                f"short artifact write for {path}: {written}/{len(payload)} bytes"
            )
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.link(temporary, path)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def require_complete_samples(samples: list[dict[str, Any]]) -> dict[str, Any]:
    if len(samples) != BATCHES:
        raise RuntimeError(
            f"cache observation set is incomplete: {len(samples)}/{BATCHES}"
        )
    return samples[0]


def terminate(processes: list[subprocess.Popen[bytes]]) -> None:
    for process in processes:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and any(
        process.poll() is None for process in processes
    ):
        time.sleep(0.1)
    for process in processes:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def require_terminal_result(
    batch_root: Path,
    *,
    run_id: str,
    expected_sha256: str,
) -> tuple[dict[str, Any], str]:
    pointer = load_json(batch_root / "run.json")
    if pointer.get("run_id") != run_id:
        raise RuntimeError(f"{run_id}: run pointer identity mismatch")
    result_path = Path(pointer["latest_observation"]) / "result.json"
    result = load_json(result_path)
    if (
        result.get("run_id") != run_id
        or result.get("workload_sha256") != expected_sha256
        or result.get("intended_jobs") != JOBS_PER_BATCH
        or result.get("accepted_jobs") != JOBS_PER_BATCH
        or result.get("completed_jobs") != JOBS_PER_BATCH
        or result.get("deadline_seconds") != DEADLINE_SECONDS
        or result.get("org_id") != f"org-batch-{run_id}"
        or result.get("poll_concurrency") != POLL_CONCURRENCY
        or result.get("poll_interval_seconds") != POLL_INTERVAL_SECONDS
    ):
        raise RuntimeError(f"{run_id}: terminal result violates the registered batch")
    token_fields = (
        result.get("prompt_tokens"),
        result.get("completion_tokens"),
        result.get("billable_tokens"),
    )
    if (
        not all(isinstance(value, int) and value >= 0 for value in token_fields)
        or token_fields[0] + token_fields[1] != token_fields[2]
        or token_fields[2] <= 0
    ):
        raise RuntimeError(f"{run_id}: terminal token conservation is not exact")
    timestamps = result.get("timestamps_micros", {})
    if (
        not isinstance(timestamps.get("first_accept"), int)
        or not isinstance(timestamps.get("last_accept"), int)
        or timestamps["first_accept"] <= 0
        or timestamps["last_accept"] < timestamps["first_accept"]
    ):
        raise RuntimeError(f"{run_id}: acceptance timestamps are incoherent")
    balance = result.get("customer_balance", {})
    if (
        balance.get("billing_enforced") is not True
        or balance.get("reserved_micro_usd") != 0
        or not all(
            isinstance(balance.get(field), int)
            for field in (
                "credits_micro_usd",
                "spent_micro_usd",
                "reserved_micro_usd",
                "available_micro_usd",
            )
        )
        or balance["credits_micro_usd"]
        != balance["spent_micro_usd"]
        + balance["reserved_micro_usd"]
        + balance["available_micro_usd"]
        or result.get("customer_charge_micro_usd") != balance["spent_micro_usd"]
    ):
        raise RuntimeError(f"{run_id}: customer conservation is not exact")
    return result, sha256(result_path)


def require_job_ids(batch_root: Path, *, run_id: str) -> list[str]:
    job_ids = load_json(batch_root / "job_ids.json").get("job_ids")
    if (
        not isinstance(job_ids, list)
        or len(job_ids) != JOBS_PER_BATCH
        or len(set(job_ids)) != JOBS_PER_BATCH
        or not all(isinstance(job_id, str) and job_id for job_id in job_ids)
    ):
        raise RuntimeError(f"{run_id}: job-id authority is incomplete")
    return job_ids


def cache_sample(
    *,
    client: Client,
    admin_key: str,
    cache_root: Path,
    batch_index: int,
    run_id: str,
    job_ids: list[str],
    observation_sequence: int,
) -> dict[str, Any]:
    job_id = str(job_ids[0])
    _, profile = client.request(
        "GET",
        f"/admin/jobs/{job_id}/profile-evidence",
        bearer=admin_key,
    )
    raw_path = cache_root / f"sample-{observation_sequence:02d}-batch-{batch_index:02d}.json"
    atomic_json(raw_path, profile)
    evidence = profile.get("evidence", {})
    fields = {
        "pod_id": evidence.get("pod_id"),
        "pool": evidence.get("pool"),
        "model": evidence.get("model"),
        "gpu_sku": evidence.get("gpu_sku"),
        "launch_contract_digest": evidence.get("launch_contract_digest"),
        "provider_created_at_micros": evidence.get("provider_created_at_micros"),
        "provider_rate_micro_per_hour": evidence.get("provider_rate_micro_per_hour"),
        "queries_at_ready": evidence.get("engine_prefix_queries_at_ready"),
        "hits_at_ready": evidence.get("engine_prefix_hits_at_ready"),
        "queries_latest": evidence.get("engine_prefix_queries_latest"),
        "hits_latest": evidence.get("engine_prefix_hits_latest"),
        "latest_at_micros": evidence.get("engine_cache_latest_at_micros"),
    }
    if (
        not all(
            isinstance(fields[name], str) and fields[name]
            for name in ("pod_id", "pool", "model", "gpu_sku", "launch_contract_digest")
        )
        or not all(
            isinstance(fields[name], int) and fields[name] > 0
            for name in (
                "provider_created_at_micros",
                "provider_rate_micro_per_hour",
                "latest_at_micros",
            )
        )
        or not all(
            isinstance(fields[name], int)
            for name in (
                "queries_at_ready",
                "hits_at_ready",
                "queries_latest",
                "hits_latest",
            )
        )
        or not 0 <= fields["hits_at_ready"] <= fields["queries_at_ready"]
        or not fields["queries_at_ready"] <= fields["queries_latest"]
        or not fields["hits_at_ready"] <= fields["hits_latest"] <= fields["queries_latest"]
        or fields["latest_at_micros"] <= 0
    ):
        raise RuntimeError(f"{run_id}: cache observation is incoherent: {fields}")
    return {
        "observation_sequence": observation_sequence,
        "logical_batch_index": batch_index,
        "run_id": run_id,
        "sampled_job_id": job_id,
        "sampled_at_epoch_micros": time.time_ns() // 1_000,
        "raw_profile_relative": str(raw_path.relative_to(cache_root.parent)),
        "raw_profile_sha256": sha256(raw_path),
        **fields,
        "interpretation": (
            "stamped cumulative observation; adjacent differences are approximate "
            "deltas, never exact per-batch hit rates"
        ),
    }


def require_placement_audit(
    value: object, *, pool: str, since_micros: int
) -> list[dict[str, Any]]:
    if not isinstance(value, dict):
        raise RuntimeError("placement decision audit is not an object")
    rows = value.get("rows")
    if (
        value.get("object") != "placement_decision_audit"
        or value.get("pool") != pool
        or value.get("since_micros") != since_micros
        or value.get("limit") != PLACEMENT_AUDIT_LIMIT
        or value.get("compact") is not True
        or value.get("complete_within_requested_window") is not True
        or value.get("authority")
        != "append_only_scheduler_observation_never_placement_input"
        or not isinstance(rows, list)
        or not rows
        or len(rows) > PLACEMENT_AUDIT_LIMIT
        or not all(isinstance(row, dict) for row in rows)
        or any(set(row) != PLACEMENT_AUDIT_COMPACT_ROW_KEYS for row in rows)
    ):
        raise RuntimeError("placement decision audit is incomplete or incoherent")
    timestamps = [row.get("at_micros") for row in rows]
    if (
        not all(
            isinstance(at_micros, int) and at_micros >= since_micros
            for at_micros in timestamps
        )
        or timestamps != sorted(timestamps)
        or any(row.get("pool") != pool for row in rows)
    ):
        raise RuntimeError("placement decision audit chronology is incoherent")
    return rows


def classify_acquisition(
    *, rows: list[dict[str, Any]], pod_identity: dict[str, Any], variant: str
) -> dict[str, Any]:
    attempts = [row for row in rows if row.get("attempt_outcome") == "attempting"]
    if not attempts:
        raise RuntimeError("placement decision audit carries no durable attempting row")
    if any(
        row.get("backlog_tokens") != REGISTERED_BACKLOG_TOKENS
        or not isinstance(row.get("plan_id"), str)
        or not row["plan_id"]
        or not isinstance(row.get("attempt_ordinal"), int)
        or row["attempt_ordinal"] < 0
        or not isinstance(row.get("selected_sku"), str)
        or not row["selected_sku"]
        for row in attempts
    ):
        raise RuntimeError("placement attempt was not made against the full registered backlog")
    pod_id = pod_identity["pod_id"]
    matched = [row for row in attempts if row.get("pod_id") == pod_id]
    if len(matched) != 1:
        raise RuntimeError(
            "terminal pod must match exactly one durable placement attempt, "
            f"got {len(matched)}"
        )
    success = matched[0]
    if (
        success.get("matched_pod_count") != 1
        or success.get("selected_sku") != pod_identity["gpu_sku"]
        or success.get("launch_contract_digest")
        != pod_identity["launch_contract_digest"]
        or not isinstance(success.get("pod_created_at_micros"), int)
        or success["pod_created_at_micros"] <= 0
    ):
        raise RuntimeError("terminal pod does not match its durable placement identity")
    first_attempt = attempts[0]
    created_at = success["pod_created_at_micros"]
    duration = created_at - first_attempt["at_micros"]
    if duration < 0:
        raise RuntimeError("successful provider-create adoption predates its first attempt")
    other_pods = sorted(
        {
            row["pod_id"]
            for row in attempts
            if isinstance(row.get("pod_id"), str) and row["pod_id"] != pod_id
        }
    )
    within_limit = duration <= ACQUISITION_LIMIT_MICROS and not other_pods
    outcomes_for_success = sorted(
        {
            str(row.get("attempt_outcome"))
            for row in rows
            if row.get("plan_id") == success["plan_id"]
            and row.get("attempt_ordinal") == success["attempt_ordinal"]
            and row.get("attempt_outcome")
            in {"adopted", "recovered-adopted", "resolved-adopted"}
        }
    )
    return {
        "status": "continuity_eligible" if within_limit else "acquisition_regime",
        "continuity_headline_eligible": within_limit if variant == "anchor" else None,
        "clock_basis": "gateway_database_micros_for_both_endpoints",
        "start_basis": "first durable attempting row after the pre-spend observer epoch",
        "end_basis": (
            "managed_pods.created_at_micros for the terminal pod's exact plan and ordinal"
        ),
        "first_attempt_at_micros": first_attempt["at_micros"],
        "successful_create_adopted_at_micros": created_at,
        "duration_micros": duration,
        "limit_micros": ACQUISITION_LIMIT_MICROS,
        "terminal_pod_id": pod_id,
        "terminal_gpu_sku": pod_identity["gpu_sku"],
        "successful_plan_id": success["plan_id"],
        "successful_attempt_ordinal": success["attempt_ordinal"],
        "successful_outcome_rows": outcomes_for_success,
        "durable_success_basis": (
            "managed_pod_adoption_plus_outcome_row"
            if outcomes_for_success
            else "managed_pod_adoption; outcome row absent and never inferred"
        ),
        "attempt_count": len(attempts),
        "definitive_rejections_before_success": sum(
            row.get("attempt_outcome") == "definitive-rejection"
            and isinstance(row.get("at_micros"), int)
            and row["at_micros"] <= created_at
            for row in rows
        ),
        "other_created_pods_in_window": other_pods,
    }


def main() -> int:
    if len(sys.argv) != 7:
        raise SystemExit(
            "usage: stacked_n24_admit.py <anchor|realistic> <public-base> "
            "<credentials-file> <run-prefix> <artifact-root> <pre-spend-receipt>"
        )
    variant, base, credentials_text, run_prefix, root_text, pre_spend_text = sys.argv[1:]
    if variant not in {"anchor", "realistic"}:
        raise SystemExit("variant must be anchor or realistic")
    if (
        not run_prefix
        or len(run_prefix) > 72
        or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for character in run_prefix)
    ):
        raise SystemExit("run-prefix must be 1-72 alphanumeric/-/_ characters")
    credentials = Path(credentials_text).resolve()
    if not credentials.is_file():
        raise SystemExit(f"credentials file is absent: {credentials}")
    expected_sha256 = (
        ANCHOR_SHA256 if variant == "anchor" else REALISTIC_AGENT_WORKLOAD_SHA256
    )
    pre_spend_path = Path(pre_spend_text).resolve()
    pre_spend, pre_spend_raw, pre_spend_sha256 = require_pre_spend_receipt(
        pre_spend_path,
        variant=variant,
        run_prefix=run_prefix,
        expected_sha256=expected_sha256,
    )
    root = Path(root_text).resolve()
    arming, arming_raw, arming_sha256 = require_launchd_arming_receipt(
        variant=variant,
        public_base=base,
        credentials=credentials,
        run_prefix=run_prefix,
        artifact_root=root,
        pre_spend_path=pre_spend_path,
        pre_spend=pre_spend,
        pre_spend_sha256=pre_spend_sha256,
    )
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(root, 0o700)
    lock = root / "one-shot.lock"
    try:
        lock.mkdir(mode=0o700)
    except FileExistsError as error:
        raise SystemExit(f"series identity already started: {lock}") from error

    sealed_pre_spend_path = root / "pre-spend-receipt.json"
    write_once_bytes(sealed_pre_spend_path, pre_spend_raw)
    sealed_arming_path = root / "launchd-arming-receipt.json"
    write_once_bytes(sealed_arming_path, arming_raw)

    batches_root = root / "batches"
    logs_root = root / "logs"
    keys_root = root / "keys"
    cache_root = root / "cache-samples"
    for path in (batches_root, logs_root, keys_root, cache_root):
        path.mkdir(mode=0o700)
    canary = (
        PROJECT_ROOT / "scripts/public_batch_canary.py"
        if variant == "anchor"
        else PROJECT_ROOT / "scripts/realistic_agent_batch_canary.py"
    )
    source_receipt = {
        "coordinator_sha256": sha256(Path(__file__).resolve()),
        "canary_relative": str(canary.relative_to(PROJECT_ROOT)),
        "canary_sha256": sha256(canary),
        "frozen_transport_sha256": sha256(
            PROJECT_ROOT / "scripts/public_batch_canary.py"
        ),
        "variant": variant,
        "workload_sha256": expected_sha256,
        "batches": BATCHES,
        "jobs_per_batch": JOBS_PER_BATCH,
        "deadline_seconds": DEADLINE_SECONDS,
        "admission_limit_seconds": ADMISSION_LIMIT_SECONDS,
        "poll_concurrency_per_batch": POLL_CONCURRENCY,
        "poll_interval_seconds": POLL_INTERVAL_SECONDS,
        "credentials_path": str(credentials),
        "pre_spend_receipt_source_path": str(pre_spend_path),
        "pre_spend_receipt_relative": str(sealed_pre_spend_path.relative_to(root)),
        "pre_spend_receipt_sha256": pre_spend_sha256,
        "launchd_arming_receipt_source_path": os.environ[ARMING_RECEIPT_ENV],
        "launchd_arming_receipt_relative": str(sealed_arming_path.relative_to(root)),
        "launchd_arming_receipt_sha256": arming_sha256,
        "launchd_label": arming["label"],
        "launchd_teardown_required_bootout_target": arming[
            "teardown_required_bootout_target"
        ],
        "registered_commit": pre_spend["registered_commit"],
        "started_at_epoch_micros": time.time_ns() // 1_000,
    }
    atomic_json(root / "source-receipt.json", source_receipt)

    processes: list[subprocess.Popen[bytes]] = []
    log_handles = []
    records: list[dict[str, Any]] = []
    admission_started = time.monotonic()

    def interrupted(signum: int, _frame: object) -> None:
        raise KeyboardInterrupt(f"received signal {signum}")

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        for batch_index in range(1, BATCHES + 1):
            run_id = f"{run_prefix}-b{batch_index:02d}"
            batch_root = batches_root / f"batch-{batch_index:02d}"
            log_path = logs_root / f"batch-{batch_index:02d}.log"
            log_handle = log_path.open("xb", buffering=0)
            log_handles.append(log_handle)
            environment = os.environ.copy()
            environment.update(
                {
                    "BATCH_RUN_ID": run_id,
                    "BATCH_ARTIFACT_DIR": str(batch_root),
                    "BATCH_KEY_STORE_ROOT": str(keys_root),
                    "BATCH_DEADLINE_SECONDS": str(DEADLINE_SECONDS),
                    "BATCH_POLL_BUDGET_SECONDS": str(POLL_BUDGET_SECONDS),
                    "BATCH_POLL_CONCURRENCY": str(POLL_CONCURRENCY),
                    "BATCH_POLL_INTERVAL_SECONDS": str(POLL_INTERVAL_SECONDS),
                }
            )
            process = subprocess.Popen(
                [sys.executable, str(canary), base, str(credentials)],
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                env=environment,
                start_new_session=True,
            )
            processes.append(process)
            record = {
                "logical_batch_index": batch_index,
                "run_id": run_id,
                "pid": process.pid,
                "started_at_epoch_micros": time.time_ns() // 1_000,
                "log_relative": str(log_path.relative_to(root)),
                "batch_root_relative": str(batch_root.relative_to(root)),
            }
            records.append(record)
            while True:
                if process.poll() is not None:
                    raise RuntimeError(
                        f"{run_id}: exited {process.returncode} before durable acceptance"
                    )
                if log_path.is_file() and ACCEPTED_MARKER in log_path.read_text(
                    errors="replace"
                ):
                    record["accepted_marker_observed_at_epoch_micros"] = (
                        time.time_ns() // 1_000
                    )
                    record["log_sha256_at_acceptance"] = sha256(log_path)
                    atomic_json(root / "admission-progress.json", {"batches": records})
                    break
                elapsed = time.monotonic() - admission_started
                if elapsed >= ADMISSION_LIMIT_SECONDS:
                    raise RuntimeError(
                        f"{run_id}: 24-batch admission exceeded "
                        f"{ADMISSION_LIMIT_SECONDS}s before batch {batch_index} completed"
                    )
                time.sleep(0.1)

        source_receipt["all_acceptance_markers_at_epoch_micros"] = time.time_ns() // 1_000
        source_receipt["marker_admission_span_micros"] = int(
            (time.monotonic() - admission_started) * 1_000_000
        )
        source_receipt["batch_processes"] = records
        atomic_json(root / "admission-receipt.json", source_receipt)

        admin_key = parse_admin_key(credentials)
        client = Client(base, cache_root / "http_failures.jsonl")
        unsampled = set(range(BATCHES))
        samples: list[dict[str, Any]] = []
        results: list[dict[str, Any]] = []
        all_job_ids: set[str] = set()
        while unsampled:
            progressed = False
            for process_index in sorted(unsampled):
                process = processes[process_index]
                returncode = process.poll()
                if returncode is None:
                    continue
                progressed = True
                if returncode != 0:
                    raise RuntimeError(
                        f"{records[process_index]['run_id']}: canary exited {returncode}"
                    )
                batch_index = process_index + 1
                run_id = records[process_index]["run_id"]
                batch_root = batches_root / f"batch-{batch_index:02d}"
                result, result_sha256 = require_terminal_result(
                    batch_root,
                    run_id=run_id,
                    expected_sha256=expected_sha256,
                )
                job_ids = require_job_ids(batch_root, run_id=run_id)
                overlap = all_job_ids.intersection(job_ids)
                if overlap:
                    raise RuntimeError(
                        f"{run_id}: job ids overlap another logical tenant batch"
                    )
                all_job_ids.update(job_ids)
                results.append(result)
                sample = cache_sample(
                    client=client,
                    admin_key=admin_key,
                    cache_root=cache_root,
                    batch_index=batch_index,
                    run_id=run_id,
                    job_ids=job_ids,
                    observation_sequence=len(samples) + 1,
                )
                if samples and any(
                    sample[key] != samples[-1][key]
                    for key in (
                        "pod_id",
                        "pool",
                        "model",
                        "gpu_sku",
                        "launch_contract_digest",
                        "provider_created_at_micros",
                        "provider_rate_micro_per_hour",
                    )
                ):
                    raise RuntimeError(f"{run_id}: pod identity changed across samples")
                if samples and (
                    sample["queries_latest"] < samples[-1]["queries_latest"]
                    or sample["hits_latest"] < samples[-1]["hits_latest"]
                    or sample["latest_at_micros"] < samples[-1]["latest_at_micros"]
                ):
                    raise RuntimeError(
                        f"{run_id}: cumulative cache observation moved backward"
                    )
                samples.append(sample)
                records[process_index].update(
                    {
                        "terminal_at_epoch_micros": time.time_ns() // 1_000,
                        "terminal_result_sha256": result_sha256,
                        "cache_observation_sequence": sample["observation_sequence"],
                    }
                )
                unsampled.remove(process_index)
                atomic_json(root / "cache-samples.json", {"samples": samples})
                atomic_json(root / "terminal-progress.json", {"batches": records})
            if not progressed:
                time.sleep(1)

        first_accept = min(
            int(result["timestamps_micros"]["first_accept"]) for result in results
        )
        last_accept = max(
            int(result["timestamps_micros"]["last_accept"]) for result in results
        )
        admission_span = last_accept - first_accept
        if admission_span > ADMISSION_LIMIT_SECONDS * 1_000_000:
            raise RuntimeError(
                f"actual acceptance span {admission_span}us exceeded the "
                f"registered {ADMISSION_LIMIT_SECONDS}s limit"
            )
        first_sample = require_complete_samples(samples)
        provider_created = int(first_sample["provider_created_at_micros"])
        if provider_created < last_accept:
            raise RuntimeError(
                "provider create preceded complete 7,200-job acceptance: "
                f"provider_created={provider_created}, last_accept={last_accept}"
            )
        organizations = {str(result["org_id"]) for result in results}
        if len(organizations) != BATCHES:
            raise RuntimeError("logical batches did not use 24 distinct organizations")
        if len(all_job_ids) != BATCHES * JOBS_PER_BATCH:
            raise RuntimeError("logical batches did not produce 7,200 distinct jobs")
        prompt_tokens = sum(int(result["prompt_tokens"]) for result in results)
        completion_tokens = sum(
            int(result["completion_tokens"]) for result in results
        )
        customer_charge_micro_usd = sum(
            int(result["customer_charge_micro_usd"]) for result in results
        )
        customer_conservation = {
            "credits_micro_usd": sum(
                int(result["customer_balance"]["credits_micro_usd"])
                for result in results
            ),
            "spent_micro_usd": sum(
                int(result["customer_balance"]["spent_micro_usd"])
                for result in results
            ),
            "reserved_micro_usd": sum(
                int(result["customer_balance"]["reserved_micro_usd"])
                for result in results
            ),
            "available_micro_usd": sum(
                int(result["customer_balance"]["available_micro_usd"])
                for result in results
            ),
        }
        if (
            customer_conservation["spent_micro_usd"]
            != customer_charge_micro_usd
            or customer_conservation["credits_micro_usd"]
            != customer_conservation["spent_micro_usd"]
            + customer_conservation["reserved_micro_usd"]
            + customer_conservation["available_micro_usd"]
        ):
            raise RuntimeError("aggregate customer conservation is not exact")
        conservation_document = {
            "object": "stacked_n24_customer_conservation_v1",
            "variant": variant,
            "run_prefix": run_prefix,
            "organizations": BATCHES,
            "customer": {
                **customer_conservation,
                "exact": True,
            },
            "basis": (
                "sum of all 24 independently terminal customer balances; each "
                "tenant was already required to conserve before aggregation"
            ),
        }
        conservation_path = root / "customer-conservation.json"
        atomic_json(conservation_path, conservation_document)
        pod_identity = {
            key: first_sample[key]
            for key in (
                "pod_id",
                "pool",
                "model",
                "gpu_sku",
                "launch_contract_digest",
                "provider_rate_micro_per_hour",
            )
        }
        since_micros = int(pre_spend["observer"]["pre_launch_epoch"]) * 1_000_000
        _, placement_document = client.request(
            "GET",
            f"/admin/pools/{pod_identity['pool']}/placement-decisions"
            f"?since_micros={since_micros}&limit={PLACEMENT_AUDIT_LIMIT}"
            "&compact=true",
            bearer=admin_key,
        )
        placement_path = root / "placement-decisions.json"
        atomic_json(placement_path, placement_document)
        placement_rows = require_placement_audit(
            placement_document,
            pool=pod_identity["pool"],
            since_micros=since_micros,
        )
        acquisition = classify_acquisition(
            rows=placement_rows,
            pod_identity=pod_identity,
            variant=variant,
        )
        acquisition["placement_decisions_sha256"] = sha256(placement_path)
        acquisition["placement_decision_rows"] = len(placement_rows)
        atomic_json(root / "acquisition-classification.json", acquisition)
        terminal = {
            "status": "complete",
            "variant": variant,
            "workload_sha256": expected_sha256,
            "logical_batches": BATCHES,
            "organizations": BATCHES,
            "accepted_jobs": BATCHES * JOBS_PER_BATCH,
            "completed_jobs": BATCHES * JOBS_PER_BATCH,
            "distinct_job_ids": len(all_job_ids),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "billable_tokens": prompt_tokens + completion_tokens,
            "customer_charge_micro_usd": customer_charge_micro_usd,
            "customer_conservation_relative": str(
                conservation_path.relative_to(root)
            ),
            "customer_conservation_sha256": sha256(conservation_path),
            "first_accept_epoch_micros": first_accept,
            "last_accept_epoch_micros": last_accept,
            "acceptance_span_micros": admission_span,
            "admission_limit_seconds": ADMISSION_LIMIT_SECONDS,
            "provider_created_at_micros": provider_created,
            "accepted_before_provider_create": True,
            "pod_identity": pod_identity,
            "acquisition_classification": acquisition,
            "cache_observation_order": [
                sample["logical_batch_index"] for sample in samples
            ],
            "cache_interpretation": (
                "stamped cumulative samples in terminal-observation order; "
                "adjacent differences are approximate and batches may overlap"
            ),
            "batch_records": records,
        }
        atomic_json(root / "terminal-receipt.json", terminal)
        print(
            f"STACKED N24 {variant.upper()} COMPLETE: 24 organizations, "
            f"7200/7200 jobs, acceptance span {admission_span}us",
            flush=True,
        )
        return 0
    except BaseException:
        terminate(processes)
        raise
    finally:
        for handle in log_handles:
            handle.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"STACKED N24 ADMISSION FAILED: {error}", file=sys.stderr)
        raise
