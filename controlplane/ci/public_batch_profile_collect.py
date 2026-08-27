#!/usr/bin/env python3
"""Collect 300 attempt-exact public-batch rows into one pod profile candidate."""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time
from typing import Any

from public_batch_canary import Client, atomic_bytes, atomic_json, parse_admin_key


MIN_BOOT_SAMPLES = 5
EXPECTED_JOBS = 300
EXPECTED_WORKLOAD_SHA256 = (
    "2392bb588923e88dc3f1473a9393a0e099a19a4889ccbd1d945b33df9e5ed205"
)


class Pending(RuntimeError):
    pass


def same(records: list[dict[str, Any]], field: str) -> Any:
    values = {json.dumps(record["evidence"].get(field), sort_keys=True) for record in records}
    if len(values) != 1:
        raise AssertionError((field, values, "pod-wide evidence disagrees"))
    return records[0]["evidence"].get(field)


def manifest(root: Path, names: list[str], status: str) -> None:
    files = {}
    for name in names:
        files[name] = hashlib.sha256((root / name).read_bytes()).hexdigest()
    atomic_json(root / "MANIFEST.json", {"status": status, "files": files})


def main() -> int:
    if len(sys.argv) not in (4, 5):
        raise SystemExit(
            "usage: public_batch_profile_collect.py <base> <credentials> <run-id> [out-root]"
        )
    base, credentials, run_id = sys.argv[1:4]
    run_root = Path(
        os.environ.get(
            "BATCH_ARTIFACT_DIR",
            str(Path.home() / ".weinfer" / f"public-batch-{run_id}"),
        )
    )
    out_root = Path(sys.argv[4]) if len(sys.argv) == 5 else run_root / "profile-evidence"
    run_pointer = json.loads((run_root / "run.json").read_text())
    if run_pointer["run_id"] != run_id:
        raise RuntimeError("batch run pointer identity mismatch")
    if run_pointer["workload_sha256"] != EXPECTED_WORKLOAD_SHA256:
        raise RuntimeError("batch run pointer carries the wrong workload")
    job_pointer = json.loads((run_root / "job_ids.json").read_text())
    job_ids = job_pointer["job_ids"]
    if len(job_ids) != EXPECTED_JOBS or len(set(job_ids)) != EXPECTED_JOBS:
        raise RuntimeError("batch job pointer is not exactly 300 unique jobs")
    batch_result_path = Path(run_pointer["latest_observation"]) / "result.json"
    batch_result = json.loads(batch_result_path.read_text())
    if (
        batch_result["completed_jobs"] != EXPECTED_JOBS
        or batch_result["workload_sha256"] != EXPECTED_WORKLOAD_SHA256
    ):
        raise RuntimeError("batch result is incomplete or carries workload drift")

    out_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(out_root, 0o700)
    admin_key = parse_admin_key(Path(credentials))
    client = Client(base, out_root / "http_failures.jsonl")
    records_by_job: dict[str, dict[str, Any]] = {}

    def fetch(job_id: str) -> tuple[str, dict[str, Any]]:
        _, value = client.request(
            "GET", f"/admin/jobs/{job_id}/profile-evidence", bearer=admin_key
        )
        return job_id, value

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
        futures = [executor.submit(fetch, job_id) for job_id in job_ids]
        for done, future in enumerate(concurrent.futures.as_completed(futures), 1):
            job_id, value = future.result()
            records_by_job[job_id] = value
            if done % 50 == 0:
                print(f"profile evidence {done}/{EXPECTED_JOBS}", flush=True)
    records = [records_by_job[job_id] for job_id in job_ids]

    snapshot = out_root / f"observation-{time.time_ns()}"
    snapshot.mkdir(mode=0o700)
    raw_name = "raw_profile_evidence.jsonl"
    atomic_bytes(
        snapshot / raw_name,
        ("\n".join(json.dumps(record, sort_keys=True) for record in records) + "\n").encode(),
    )

    if any(record.get("object") != "profile_evidence" for record in records):
        raise AssertionError("unexpected profile evidence object")
    states = {record["evidence"]["pod_state"] for record in records}
    if not states <= {"charged", "settled_provisional", "settled"}:
        manifest(snapshot, [raw_name], "pending")
        raise Pending(f"provider accounting still pending: {states}")

    invariant_fields = [
        "pod_id",
        "pool",
        "model",
        "gpu_sku",
        "created_at_micros",
        "provider_created_at_micros",
        "engine_cache_config_metric",
        "engine_cache_config_digest",
        "engine_prefix_queries_at_ready",
        "engine_prefix_hits_at_ready",
        "engine_prefix_queries_latest",
        "engine_prefix_hits_latest",
        "engine_cache_latest_at_micros",
        "ready_at_micros",
        "draining_at_micros",
        "terminate_requested_at_micros",
        "terminated_at_micros",
        "charged_at_micros",
        "settled_at_micros",
        "charge_micro_usd",
        "allocated_cost_micro_usd",
        "lifetime_micros",
        "launch_contract",
        "launch_contract_digest",
        "provider_rate_micro_per_hour",
        "recent_provision_failures",
    ]
    invariants = {field: same(records, field) for field in invariant_fields}
    profile_identities = {
        json.dumps(record["profile_identity"], sort_keys=True) for record in records
    }
    if len(profile_identities) != 1:
        raise AssertionError("batch crossed profile identities")
    profile_identity = records[0]["profile_identity"]
    launch_contract = records[0]["launch_contract"]
    launch_contract_bytes = invariants["launch_contract"].encode()
    if hashlib.sha256(launch_contract_bytes).hexdigest() != invariants["launch_contract_digest"]:
        raise AssertionError("stored launch contract digest mismatch")
    if launch_contract != json.loads(invariants["launch_contract"]):
        raise AssertionError("parsed launch contract differs from stored bytes")

    required = [
        "provider_created_at_micros",
        "created_at_micros",
        "engine_cache_config_metric",
        "engine_cache_config_digest",
        "engine_prefix_queries_at_ready",
        "engine_prefix_hits_at_ready",
        "engine_prefix_queries_latest",
        "engine_prefix_hits_latest",
        "engine_cache_latest_at_micros",
        "ready_at_micros",
        "draining_at_micros",
        "terminated_at_micros",
        "charged_at_micros",
        "settled_at_micros",
        "charge_micro_usd",
        "allocated_cost_micro_usd",
        "lifetime_micros",
        "provider_rate_micro_per_hour",
    ]
    missing = [field for field in required if invariants[field] is None]
    if missing:
        raise Pending(f"missing batch lifecycle facts: {missing}")
    provider_created = int(invariants["provider_created_at_micros"])
    created = int(invariants["created_at_micros"])
    cache_config_metric = str(invariants["engine_cache_config_metric"])
    cache_config_digest = str(invariants["engine_cache_config_digest"])
    cache_queries_at_ready = int(invariants["engine_prefix_queries_at_ready"])
    cache_hits_at_ready = int(invariants["engine_prefix_hits_at_ready"])
    cache_queries_latest = int(invariants["engine_prefix_queries_latest"])
    cache_hits_latest = int(invariants["engine_prefix_hits_latest"])
    cache_latest_at = int(invariants["engine_cache_latest_at_micros"])
    ready = int(invariants["ready_at_micros"])
    draining = int(invariants["draining_at_micros"])
    terminated = int(invariants["terminated_at_micros"])
    charged = int(invariants["charged_at_micros"])
    settled = int(invariants["settled_at_micros"])
    charge = int(invariants["charge_micro_usd"])
    allocated = int(invariants["allocated_cost_micro_usd"])
    lifetime = int(invariants["lifetime_micros"])
    provider_rate = int(invariants["provider_rate_micro_per_hour"])
    if not (
        0 < provider_created <= created <= ready <= draining <= terminated <= charged <= settled
    ):
        raise AssertionError("pod lifecycle ordering is invalid")
    if lifetime != terminated - provider_created:
        raise AssertionError((lifetime, terminated - provider_created, "lifetime mismatch"))
    if charge <= 0 or allocated != charge or provider_rate <= 0:
        raise AssertionError((charge, allocated, provider_rate, "cost does not conserve"))
    if (
        'enable_prefix_caching="True"' not in cache_config_metric
        or hashlib.sha256(cache_config_metric.encode()).hexdigest()
        != cache_config_digest
        or not 0 <= cache_hits_at_ready <= cache_queries_at_ready
        or not cache_queries_at_ready <= cache_queries_latest
        or not cache_hits_at_ready <= cache_hits_latest <= cache_queries_latest
        or cache_latest_at <= 0
    ):
        raise AssertionError("effective engine cache observation is incoherent")
    cache_query_delta = cache_queries_latest - cache_queries_at_ready
    cache_hit_delta = cache_hits_latest - cache_hits_at_ready

    total_prompt = 0
    total_completion = 0
    total_attempt_runtime = 0
    first_engine_start = None
    last_completion = 0
    failed_generations = 0
    for record in records:
        evidence = record["evidence"]
        if evidence["job_state"] != "completed" or evidence["job_failure"] is not None:
            raise AssertionError((evidence["job_id"], evidence["job_state"]))
        attempts = evidence["attempts"]
        if len(attempts) != 1:
            raise AssertionError((evidence["job_id"], len(attempts), "one served attempt required"))
        attempt = attempts[0]
        if attempt["billable"] is not True or attempt["needs_reconciliation"] is not False:
            raise AssertionError((evidence["job_id"], attempt))
        prompt = int(attempt["physical_prompt_tokens"])
        completion = int(attempt["physical_completion_tokens"])
        runtime = int(attempt["runtime_micros"])
        completed = int(evidence["completed_at_micros"])
        if prompt != 3_960 or not 0 <= completion <= 64 or runtime <= 0:
            raise AssertionError((evidence["job_id"], prompt, completion, runtime))
        start = completed - runtime
        first_engine_start = start if first_engine_start is None else min(first_engine_start, start)
        last_completion = max(last_completion, completed)
        total_prompt += prompt
        total_completion += completion
        total_attempt_runtime += runtime
        failed_generations += max(0, int(evidence["lease_generation"]) - len(attempts))
    assert first_engine_start is not None
    if not ready <= first_engine_start <= last_completion <= draining:
        raise AssertionError((ready, first_engine_start, last_completion, draining))
    billable_tokens = total_prompt + total_completion
    if billable_tokens != int(batch_result["billable_tokens"]):
        raise AssertionError((billable_tokens, batch_result["billable_tokens"]))
    if total_prompt != int(batch_result["prompt_tokens"]):
        raise AssertionError((total_prompt, batch_result["prompt_tokens"]))

    provider_pre_adopt = created - provider_created
    boot = ready - created
    activation = ready - provider_created
    pre_service_idle = first_engine_start - ready
    serving_window = last_completion - first_engine_start
    ready_window = last_completion - ready
    retained_idle = draining - last_completion
    drain = terminated - draining
    time_sum = (
        provider_pre_adopt + boot + pre_service_idle + serving_window + retained_idle + drain
    )
    if time_sum != lifetime:
        raise AssertionError((time_sum, lifetime, "phase time does not conserve"))
    rate_clock_floor = math.ceil(provider_rate * lifetime / 3_600_000_000)
    if charge < rate_clock_floor:
        manifest(snapshot, [raw_name], "pending")
        raise Pending(
            f"provider bucket still accruing: charge {charge} < rate clock {rate_clock_floor}"
        )
    effective_rate = math.ceil(charge * 3_600_000_000 / lifetime)
    ready_tps_low = billable_tokens * 1_000_000 // ready_window
    ready_tps_high = math.ceil(billable_tokens * 1_000_000 / ready_window)
    active_tps_low = billable_tokens * 1_000_000 // serving_window
    active_tps_high = math.ceil(billable_tokens * 1_000_000 / serving_window)

    prior_path = Path(
        os.environ.get(
            "BATCH_PRIOR_PROFILE",
            "evidence/public-batch-live-1787630415/profile/final/profile_candidate.json",
        )
    )
    boot_samples_with_sources: list[tuple[int, str]] = [
        (activation, f"public-batch:{run_id}")
    ]
    prior_source = None
    if prior_path.exists():
        prior = json.loads(prior_path.read_text())
        if prior["profile_facts"]["identity"] != profile_identity:
            raise AssertionError("prior candidate is a different exact identity")
        prior_derivation = prior["derivation"]
        prior_run = prior_derivation.get("batch_run_id")
        if prior_run == run_id:
            raise AssertionError("prior candidate is the same batch run (would double count)")
        raw_samples = prior_derivation.get("boot_samples_micros")
        if raw_samples is None:
            prior_samples = [int(prior_derivation["activation_micros"])]
            prior_sources = [str(prior_path)]
        else:
            if not isinstance(raw_samples, list) or not raw_samples:
                raise AssertionError("prior boot sample chain is malformed")
            prior_samples = [int(value) for value in raw_samples]
            if any(value <= 0 for value in prior_samples):
                raise AssertionError("prior boot sample chain contains a nonpositive value")
            raw_sources = prior_derivation.get("boot_sample_sources")
            if raw_sources is not None:
                if not isinstance(raw_sources, list) or len(raw_sources) != len(prior_samples):
                    raise AssertionError("prior boot sample provenance does not travel with samples")
                prior_sources = [str(value) for value in raw_sources]
            elif len(prior_samples) == 1:
                prior_sources = [str(prior_path)]
            elif len(prior_samples) == 2 and prior_derivation.get("prior_profile_source"):
                # Compatibility for the already-sealed two-sample
                # public candidate, which predates explicit source
                # vectors but names its earlier source exactly.
                prior_sources = [
                    str(prior_derivation["prior_profile_source"]),
                    str(prior_path),
                ]
            else:
                raise AssertionError("multi-sample prior lacks exact provenance")
        if int(prior_derivation["activation_micros"]) not in prior_samples:
            raise AssertionError("prior candidate's own activation is absent from its chain")
        if len(set(prior_sources)) != len(prior_sources):
            raise AssertionError("prior boot sample provenance would double count a run")
        boot_samples_with_sources.extend(zip(prior_samples, prior_sources, strict=True))
        prior_source = str(prior_path)
    if len({source for _, source in boot_samples_with_sources}) != len(boot_samples_with_sources):
        raise AssertionError("boot sample chain contains duplicate provenance")
    boot_samples_with_sources.sort(key=lambda pair: (pair[0], pair[1]))
    boot_samples = [sample for sample, _ in boot_samples_with_sources]
    boot_sample_sources = [source for _, source in boot_samples_with_sources]
    observed = int(time.time())
    promotion_eligible = len(boot_samples) >= MIN_BOOT_SAMPLES
    source = (
        f"public frozen batch {run_id}; {EXPECTED_JOBS} completed jobs; exact launch "
        f"contract {invariants['launch_contract_digest']}; provider-v2 provisional charge; "
        "throughput uses READY-to-last-completion product service window; all job usage and "
        "segments are attempt-exact; no engine knob changed"
    )
    candidate = {
        "status": "candidate_only",
        "charge_finality": "provisional",
        "promotion": {
            "eligible": promotion_eligible,
            "boot_samples_observed": len(boot_samples),
            "boot_samples_required": MIN_BOOT_SAMPLES,
            "saturation_samples_observed": 1,
            "decision": (
                "review only; minimum boot-sample rule not met"
                if not promotion_eligible
                else "review required; no automatic activation"
            ),
        },
        "profile_facts": {
            "identity": profile_identity,
            "rate_micro_per_hour": effective_rate,
            "tps_low": ready_tps_low,
            "tps_high": ready_tps_high,
            "tps_evidence": "Measured",
            "tps_scope": "SingleIdentity",
            "boot_low_micros": min(boot_samples),
            "boot_high_micros": max(boot_samples),
            "drain_low_micros": drain,
            "drain_high_micros": drain,
            "fixed_evidence": "Measured",
            "boot_scope": "SingleIdentity",
            "source": source,
            "observed_at_epoch": observed,
            "vram_gb": int(launch_contract["vram_gb"]),
            "max_context_tokens": int(launch_contract["max_context_tokens"]),
            "catalog_available": True,
            "recent_acquisition_failures": int(invariants["recent_provision_failures"]),
            "cuda_pin": launch_contract["cuda_pin"],
        },
        "derivation": {
            "batch_run_id": run_id,
            "workload_sha256": EXPECTED_WORKLOAD_SHA256,
            "jobs": EXPECTED_JOBS,
            "billable_tokens": billable_tokens,
            "prompt_tokens": total_prompt,
            "completion_tokens": total_completion,
            "provider_create_rate_micro_per_hour": provider_rate,
            "effective_lifecycle_rate_micro_per_hour": effective_rate,
            "charge_micro_usd": charge,
            "allocated_cost_micro_usd": allocated,
            "rate_clock_floor_micro_usd": rate_clock_floor,
            "provider_pre_adopt_micros": provider_pre_adopt,
            "boot_micros": boot,
            "activation_micros": activation,
            "boot_samples_micros": boot_samples,
            "boot_sample_sources": boot_sample_sources,
            "prior_profile_source": prior_source,
            "pre_service_idle_micros": pre_service_idle,
            "serving_window_micros": serving_window,
            "ready_window_micros": ready_window,
            "retained_idle_micros": retained_idle,
            "drain_micros": drain,
            "lifetime_micros": lifetime,
            "time_conservation": time_sum,
            "cost_conservation": allocated,
            "sum_attempt_runtime_micros": total_attempt_runtime,
            "ready_window_tps_low": ready_tps_low,
            "ready_window_tps_high": ready_tps_high,
            "active_window_tps_low": active_tps_low,
            "active_window_tps_high": active_tps_high,
            "failed_generations": failed_generations,
            "engine_cache_observation": {
                "authority": "vllm_loopback_metrics_observation_only",
                "cache_config_metric": cache_config_metric,
                "cache_config_digest": cache_config_digest,
                "prefix_queries_at_ready": cache_queries_at_ready,
                "prefix_hits_at_ready": cache_hits_at_ready,
                "prefix_queries_latest": cache_queries_latest,
                "prefix_hits_latest": cache_hits_latest,
                "prefix_query_delta": cache_query_delta,
                "prefix_hit_delta": cache_hit_delta,
                "prefix_hit_fraction": (
                    cache_hit_delta / cache_query_delta
                    if cache_query_delta > 0
                    else None
                ),
                "latest_at_micros": cache_latest_at,
                "planner_input": False,
                "promotion_input": False,
                "pricing_input": False,
            },
            "settlement_visibility_lag_micros": charged - terminated,
            "settlement_commit_micros": settled - charged,
        },
    }
    summary = {
        "run_id": run_id,
        "pod_id": invariants["pod_id"],
        "pool": invariants["pool"],
        "jobs": EXPECTED_JOBS,
        "billable_tokens": billable_tokens,
        "provider_charge_micro_usd": charge,
        "delivered_usd_per_mtok": charge / billable_tokens,
        "customer_charge_micro_usd": int(batch_result["customer_charge_micro_usd"]),
        "ready_window_tps": billable_tokens * 1_000_000 / ready_window,
        "active_window_tps": billable_tokens * 1_000_000 / serving_window,
        "provider_pre_adopt_micros": provider_pre_adopt,
        "boot_micros": boot,
        "pre_service_idle_micros": pre_service_idle,
        "serving_window_micros": serving_window,
        "retained_idle_micros": retained_idle,
        "drain_micros": drain,
        "lifetime_micros": lifetime,
        "settlement_visibility_lag_micros": charged - terminated,
        "settlement_commit_micros": settled - charged,
        "promotion_eligible": promotion_eligible,
        "boot_samples_observed": len(boot_samples),
        "engine_prefix_query_delta": cache_query_delta,
        "engine_prefix_hit_delta": cache_hit_delta,
        "engine_prefix_hit_fraction": (
            cache_hit_delta / cache_query_delta if cache_query_delta > 0 else None
        ),
    }
    atomic_json(snapshot / "profile_candidate.json", candidate)
    atomic_json(snapshot / "summary.json", summary)
    atomic_bytes(snapshot / "batch_result.json", batch_result_path.read_bytes())
    manifest(
        snapshot,
        [raw_name, "profile_candidate.json", "summary.json", "batch_result.json"],
        "candidate",
    )
    print(f"PUBLIC BATCH PROFILE CANDIDATE SEALED: {snapshot}")
    print(json.dumps(summary, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Pending as error:
        print(f"PUBLIC BATCH PROFILE PENDING: {error}", file=sys.stderr)
        raise SystemExit(2)
