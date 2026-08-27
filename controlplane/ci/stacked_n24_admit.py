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
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ANCHOR_SHA256 = "2392bb588923e88dc3f1473a9393a0e099a19a4889ccbd1d945b33df9e5ed205"
ACCEPTED_MARKER = "300/300 accepted; row-0 replay identical"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


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


def main() -> int:
    if len(sys.argv) != 6:
        raise SystemExit(
            "usage: stacked_n24_admit.py <anchor|realistic> <public-base> "
            "<credentials-file> <run-prefix> <artifact-root>"
        )
    variant, base, credentials_text, run_prefix, root_text = sys.argv[1:]
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
    root = Path(root_text).resolve()
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(root, 0o700)
    lock = root / "one-shot.lock"
    try:
        lock.mkdir(mode=0o700)
    except FileExistsError as error:
        raise SystemExit(f"series identity already started: {lock}") from error

    batches_root = root / "batches"
    logs_root = root / "logs"
    keys_root = root / "keys"
    cache_root = root / "cache-samples"
    for path in (batches_root, logs_root, keys_root, cache_root):
        path.mkdir(mode=0o700)
    expected_sha256 = (
        ANCHOR_SHA256 if variant == "anchor" else REALISTIC_AGENT_WORKLOAD_SHA256
    )
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
            "first_accept_epoch_micros": first_accept,
            "last_accept_epoch_micros": last_accept,
            "acceptance_span_micros": admission_span,
            "admission_limit_seconds": ADMISSION_LIMIT_SECONDS,
            "provider_created_at_micros": provider_created,
            "accepted_before_provider_create": True,
            "pod_identity": {
                key: first_sample[key]
                for key in (
                    "pod_id",
                    "pool",
                    "model",
                    "gpu_sku",
                    "launch_contract_digest",
                    "provider_rate_micro_per_hour",
                )
            },
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
