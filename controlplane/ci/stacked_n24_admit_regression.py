#!/usr/bin/env python3
"""Zero-provider regression for the 24-batch admission coordinator."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import Any


SCRIPTS = Path(__file__).resolve().parent
PROJECT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location(
    "stacked_n24_admit_under_test", SCRIPTS / "stacked_n24_admit.py"
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("could not load stacked_n24_admit.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FakeProcess:
    mode = "good"
    next_pid = 40_000

    def __init__(
        self,
        _argv: list[str],
        *,
        stdout: Any,
        env: dict[str, str],
        **_kwargs: Any,
    ) -> None:
        type(self).next_pid += 1
        self.pid = type(self).next_pid
        self.returncode = 0
        self.polls = 0
        run_id = env["BATCH_RUN_ID"]
        batch = int(run_id.rsplit("-b", 1)[1])
        batch_root = Path(env["BATCH_ARTIFACT_DIR"])
        observation = batch_root / "observation"
        observation.mkdir(parents=True)
        first = 1_900_000_000_000_000 + batch * 1_000_000
        org = f"org-batch-{run_id}"
        if self.mode == "duplicate_org" and batch == MODULE.BATCHES:
            org = f"org-batch-{run_id.rsplit('-b', 1)[0]}-b01"
        job_ids = [f"job-{run_id}-{index:03d}" for index in range(300)]
        if self.mode == "duplicate_job" and batch == 2:
            job_ids[0] = f"job-{run_id.rsplit('-b', 1)[0]}-b01-000"
        reserved = 1 if self.mode == "bad_conservation" and batch == 7 else 0
        spent = 123_000
        credits = 2_000_000
        result = {
            "run_id": run_id,
            "workload_sha256": MODULE.ANCHOR_SHA256,
            "intended_jobs": 300,
            "accepted_jobs": 300,
            "completed_jobs": 300,
            "deadline_seconds": MODULE.DEADLINE_SECONDS,
            "poll_concurrency": MODULE.POLL_CONCURRENCY,
            "poll_interval_seconds": MODULE.POLL_INTERVAL_SECONDS,
            "org_id": org,
            "prompt_tokens": 1_188_000,
            "completion_tokens": 11_500,
            "billable_tokens": 1_199_499 if self.mode == "bad_tokens" and batch == 8 else 1_199_500,
            "customer_charge_micro_usd": spent,
            "customer_balance": {
                "billing_enforced": True,
                "credits_micro_usd": credits,
                "spent_micro_usd": spent,
                "reserved_micro_usd": reserved,
                "available_micro_usd": credits - spent - reserved,
            },
            "timestamps_micros": {
                "first_accept": first,
                "last_accept": first + 10_000,
            },
        }
        MODULE.atomic_json(observation / "result.json", result)
        MODULE.atomic_json(
            batch_root / "run.json",
            {"run_id": run_id, "latest_observation": str(observation)},
        )
        MODULE.atomic_json(batch_root / "job_ids.json", {"job_ids": job_ids})
        stdout.write((MODULE.ACCEPTED_MARKER + "\n").encode())

    def poll(self) -> int | None:
        self.polls += 1
        return None if self.polls == 1 else self.returncode


class FakeClient:
    mode = "good"

    def __init__(self, _base: str, _failures: Path) -> None:
        self.calls = 0

    def request(self, *_args: Any, **_kwargs: Any) -> tuple[int, dict[str, Any]]:
        self.calls += 1
        if len(_args) >= 2 and "placement-decisions" in str(_args[1]):
            assert "&compact=true" in str(_args[1])
            since = int(
                str(_args[1]).split("since_micros=", 1)[1].split("&", 1)[0]
            )
            first = since + 50_000_000
            created = first + 150_000_000
            if self.mode == "slow_acquisition":
                created = first + MODULE.ACQUISITION_LIMIT_MICROS + 1
            pod_id = "pod-regression"
            if self.mode == "audit_foreign_pod":
                pod_id = "pod-foreign"
            complete = self.mode != "audit_incomplete"
            base_row = {
                "pool": "community-qwen7b-0",
                "backlog_tokens": MODULE.REGISTERED_BACKLOG_TOKENS,
                "selected_sku": "NVIDIA GeForce RTX 4090",
                "plan_id": "plan-regression",
                "attempt_ordinal": 0,
                "matched_pod_count": 1,
                "pod_id": pod_id,
                "pod_created_at_micros": created,
                "launch_contract_digest": "a" * 64,
            }
            if self.mode == "audit_full_row":
                base_row["candidates"] = []
            return 200, {
                "object": "placement_decision_audit",
                "pool": "community-qwen7b-0",
                "since_micros": since,
                "limit": MODULE.PLACEMENT_AUDIT_LIMIT,
                "compact": self.mode != "audit_not_compact",
                "complete_within_requested_window": complete,
                "authority": "append_only_scheduler_observation_never_placement_input",
                "rows": [
                    {**base_row, "at_micros": first, "attempt_outcome": "attempting"},
                    {
                        **base_row,
                        "at_micros": created + 1,
                        "attempt_outcome": "adopted",
                    },
                ],
            }
        queries = self.calls * 1_000
        hits = self.calls * 4
        if self.mode == "backward_cache" and self.calls == 2:
            queries = 500
            hits = 2
        pod_id = "pod-regression"
        if self.mode == "identity_drift" and self.calls == 2:
            pod_id = "pod-foreign"
        provider_created = 1_900_000_100_000_000
        if self.mode == "early_create":
            provider_created = 1_900_000_000_000_001
        return 200, {
            "evidence": {
                "pod_id": pod_id,
                "pool": "community-qwen7b-0",
                "model": "Qwen/Qwen2.5-7B-Instruct",
                "gpu_sku": "NVIDIA GeForce RTX 4090",
                "launch_contract_digest": "a" * 64,
                "provider_created_at_micros": provider_created,
                "provider_rate_micro_per_hour": 340_000,
                "engine_prefix_queries_at_ready": 0,
                "engine_prefix_hits_at_ready": 0,
                "engine_prefix_queries_latest": queries,
                "engine_prefix_hits_latest": hits,
                "engine_cache_latest_at_micros": 2_000_000 + self.calls,
            }
        }


MODULE.subprocess.Popen = FakeProcess
MODULE.Client = FakeClient
MODULE.parse_admin_key = lambda _path: "test-admin-key"


def invoke(
    root: Path,
    *,
    process_mode: str = "good",
    client_mode: str = "good",
    receipt_mode: str = "good",
    arming_mode: str = "good",
) -> int:
    FakeProcess.mode = process_mode
    FakeClient.mode = client_mode
    credentials = root.parent / f"credentials-{root.name}.env"
    credentials.write_text("WEINFER_ADMIN_KEY=test-admin-key\n")
    pre_spend = root.parent / f"pre-spend-{root.name}.json"
    now = int(time.time())
    registered_sources = {
        "product/scripts/arm_stacked_n24_admit_launchd.py": MODULE.sha256(
            SCRIPTS / "arm_stacked_n24_admit_launchd.py"
        ),
        "product/scripts/stacked_n24_admit.py": MODULE.sha256(
            SCRIPTS / "stacked_n24_admit.py"
        ),
        "product/scripts/public_batch_canary.py": MODULE.sha256(
            SCRIPTS / "public_batch_canary.py"
        ),
    }
    if receipt_mode == "source_drift":
        registered_sources["product/scripts/stacked_n24_admit.py"] = "0" * 64
    MODULE.atomic_json(
        pre_spend,
        {
            "object": "stacked_n24_pre_spend_receipt_v1",
            "variant": "anchor",
            "run_id": f"regression-{root.name}",
            "captured_at_epoch": now - (3_600 if receipt_mode == "stale" else 0),
            "registered_commit": "a" * 40,
            "registered_sources": registered_sources,
            "workload": {
                "sha256": MODULE.ANCHOR_SHA256,
                "logical_batches": MODULE.BATCHES,
                "jobs_per_batch": MODULE.JOBS_PER_BATCH,
                "deadline_seconds": MODULE.DEADLINE_SECONDS,
            },
            "scheduling": {
                "admission_limit_seconds": MODULE.ADMISSION_LIMIT_SECONDS,
                "boot_fraction": "0.04",
                "boot_seed_seconds": 452,
                "policy_tokens_per_second": 4000,
            },
            "observer": {"pre_launch_epoch": now - 10},
            "rendered_environment": {"bootstrap_shape": "full_nine_row_queue"},
            "predictions": {
                "rows": 9,
                "runtime_rate_authority": (
                    "managed_pods.provider_rate_micro_per_hour"
                ),
                "rate_parameter_domain_micro_usd_per_hour": [1, 400_000],
                "all_create_rates_under_the_ceiling_are_precommitted": True,
            },
            "ceilings": {
                "gpu_create_usd_per_hour": "0.40",
                "gpu_cumulative_usd": "1.00",
                "control_plane_create_usd_per_hour": "0.10",
            },
        },
    )
    run_prefix = f"regression-{root.name}"
    label = f"com.weinfer.{run_prefix}.stacked-n24-admit"
    public_base = "https://regression.invalid"
    arming_receipt = root.parent / f"arming-{root.name}.json"
    armer_sha256 = MODULE.sha256(SCRIPTS / "arm_stacked_n24_admit_launchd.py")
    arming_value = {
        "object": "stacked_n24_admit_launchd_arming_receipt_v1",
        "attests": "launchd_one_shot_started",
        "does_not_attest": "coordinator_completion_or_series_success",
        "armed_at_epoch": now - (3_600 if arming_mode == "stale" else 0),
        "label": "com.weinfer.wrong.stacked-n24-admit"
        if arming_mode == "wrong_receipt_label"
        else label,
        "teardown_required_bootout_target": f"gui/{os.getuid()}/{label}",
        "job_remains_loaded_after_coordinator_exit": True,
        "launchctl_submit_or_kickstart_used": False,
        "launchctl_proof_sha256": "a" * 64,
        "launchctl_recheck_proof_sha256": "b" * 64,
        "launchctl_proof_text_persisted": False,
        "coordinator_blocks_before_arming_receipt": arming_mode != "no_gate",
        "coordinator_arming_gate_seconds": MODULE.ARMING_RECEIPT_WAIT_SECONDS,
        "armer_nominal_proof_budget_seconds": 5.0,
        "armer_proof_execution_margin_seconds": 2,
        "credentials_file_contents_read": False,
        "credentials_file_digest_recorded": False,
        "coordinator_source_sha256": MODULE.sha256(
            SCRIPTS / "stacked_n24_admit.py"
        ),
        "armer_source_sha256": "0" * 64
        if arming_mode == "armer_source_drift"
        else armer_sha256,
        "pre_spend_receipt_path": str(pre_spend.resolve()),
        "pre_spend_receipt_sha256": MODULE.sha256(pre_spend),
        "coordinator_argv_identities": {
            "variant": "anchor",
            "public_base": public_base,
            "credentials_file_path": str(credentials.resolve()),
            "run_prefix": run_prefix,
            "artifact_root": str(root.resolve()),
            "pre_spend_receipt": str(pre_spend.resolve()),
        },
    }
    if arming_mode != "missing":
        MODULE.atomic_json(arming_receipt, arming_value)
        os.chmod(arming_receipt, 0o644 if arming_mode == "world_readable" else 0o600)
    argv = sys.argv
    old_xpc = os.environ.get("XPC_SERVICE_NAME")
    old_receipt = os.environ.get(MODULE.ARMING_RECEIPT_ENV)
    sys.argv = [
        "stacked_n24_admit.py",
        "anchor",
        public_base,
        str(credentials),
        run_prefix,
        str(root),
        str(pre_spend),
    ]
    if arming_mode == "unmanaged":
        os.environ.pop("XPC_SERVICE_NAME", None)
    else:
        os.environ["XPC_SERVICE_NAME"] = (
            "com.weinfer.wrong.stacked-n24-admit"
            if arming_mode == "wrong_xpc_label"
            else label
        )
    os.environ[MODULE.ARMING_RECEIPT_ENV] = str(arming_receipt.resolve())
    old_wait = MODULE.ARMING_RECEIPT_WAIT_SECONDS
    if arming_mode == "missing":
        MODULE.ARMING_RECEIPT_WAIT_SECONDS = 0
    try:
        return MODULE.main()
    finally:
        MODULE.ARMING_RECEIPT_WAIT_SECONDS = old_wait
        if old_xpc is None:
            os.environ.pop("XPC_SERVICE_NAME", None)
        else:
            os.environ["XPC_SERVICE_NAME"] = old_xpc
        if old_receipt is None:
            os.environ.pop(MODULE.ARMING_RECEIPT_ENV, None)
        else:
            os.environ[MODULE.ARMING_RECEIPT_ENV] = old_receipt
        sys.argv = argv


def expect_failure(root: Path, text: str, **kwargs: str) -> None:
    try:
        invoke(root, **kwargs)
    except RuntimeError as error:
        if text not in str(error):
            raise AssertionError(f"wrong refusal: {error}") from error
    else:
        raise AssertionError(f"expected refusal containing {text!r}")


def main() -> None:
    for incomplete in ([], [{}] * (MODULE.BATCHES - 1)):
        try:
            MODULE.require_complete_samples(incomplete)
        except RuntimeError as error:
            assert "cache observation set is incomplete" in str(error)
        else:
            raise AssertionError("incomplete cache observations were indexable")
    old_cwd = Path.cwd()
    os.chdir(PROJECT)
    try:
        with tempfile.TemporaryDirectory(prefix="stacked-n24-regression-") as temp:
            base = Path(temp)
            good = base / "good"
            assert invoke(good) == 0
            terminal = json.loads((good / "terminal-receipt.json").read_text())
            samples = json.loads((good / "cache-samples.json").read_text())["samples"]
            assert terminal["logical_batches"] == terminal["organizations"] == 24
            assert terminal["accepted_jobs"] == terminal["completed_jobs"] == 7_200
            assert terminal["distinct_job_ids"] == 7_200
            assert terminal["accepted_before_provider_create"] is True
            assert terminal["pod_identity"]["gpu_sku"] == "NVIDIA GeForce RTX 4090"
            assert terminal["billable_tokens"] == 24 * 1_199_500
            conservation_path = good / "customer-conservation.json"
            conservation = json.loads(conservation_path.read_text())
            assert conservation["object"] == "stacked_n24_customer_conservation_v1"
            assert conservation["variant"] == "anchor"
            assert conservation["organizations"] == 24
            assert conservation["customer"] == {
                "credits_micro_usd": 24 * 2_000_000,
                "spent_micro_usd": 24 * 123_000,
                "reserved_micro_usd": 0,
                "available_micro_usd": 24 * (2_000_000 - 123_000),
                "exact": True,
            }
            assert terminal["customer_conservation_relative"] == (
                "customer-conservation.json"
            )
            assert terminal["customer_conservation_sha256"] == MODULE.sha256(
                conservation_path
            )
            assert terminal["acquisition_classification"]["status"] == "continuity_eligible"
            assert (
                terminal["acquisition_classification"]["continuity_headline_eligible"]
                is True
            )
            assert terminal["acquisition_classification"]["duration_micros"] == 150_000_000
            assert (good / "placement-decisions.json").is_file()
            assert (good / "acquisition-classification.json").is_file()
            source = json.loads((good / "source-receipt.json").read_text())
            assert source["poll_concurrency_per_batch"] == MODULE.POLL_CONCURRENCY == 8
            assert source["poll_interval_seconds"] == MODULE.POLL_INTERVAL_SECONDS == 60
            source_pre_spend = base / "pre-spend-good.json"
            sealed_pre_spend = good / "pre-spend-receipt.json"
            assert sealed_pre_spend.read_bytes() == source_pre_spend.read_bytes()
            assert source["pre_spend_receipt_relative"] == "pre-spend-receipt.json"
            assert source["pre_spend_receipt_sha256"] == MODULE.sha256(
                sealed_pre_spend
            )
            source_arming = base / "arming-good.json"
            sealed_arming = good / "launchd-arming-receipt.json"
            assert sealed_arming.read_bytes() == source_arming.read_bytes()
            assert source["launchd_arming_receipt_relative"] == (
                "launchd-arming-receipt.json"
            )
            assert source["launchd_arming_receipt_sha256"] == MODULE.sha256(
                sealed_arming
            )
            assert source["launchd_label"] == (
                "com.weinfer.regression-good.stacked-n24-admit"
            )
            assert source["launchd_teardown_required_bootout_target"].endswith(
                "/com.weinfer.regression-good.stacked-n24-admit"
            )
            assert len(samples) == 24
            assert [sample["logical_batch_index"] for sample in samples] == list(
                range(1, 25)
            )
            try:
                invoke(good)
            except SystemExit as error:
                assert "already started" in str(error)
            else:
                raise AssertionError("one-shot identity was reusable")

            stale = base / "stale-pre-spend"
            expect_failure(stale, "does not bind", receipt_mode="stale")
            assert not stale.exists()
            drifted = base / "source-drift"
            expect_failure(
                drifted,
                "does not bind current source bytes",
                receipt_mode="source_drift",
            )
            assert not drifted.exists()

            for name, mode, refusal in (
                (
                    "unmanaged-coordinator",
                    "unmanaged",
                    "exact registered launchd label",
                ),
                (
                    "wrong-xpc-label",
                    "wrong_xpc_label",
                    "exact registered launchd label",
                ),
                (
                    "missing-arming-receipt",
                    "missing",
                    "did not appear before the demand gate timed out",
                ),
                (
                    "wrong-receipt-label",
                    "wrong_receipt_label",
                    "arming receipt is incoherent",
                ),
                (
                    "stale-arming-receipt",
                    "stale",
                    "arming receipt is incoherent",
                ),
                (
                    "arming-source-drift",
                    "armer_source_drift",
                    "arming receipt is incoherent",
                ),
                (
                    "arming-gate-claim-absent",
                    "no_gate",
                    "arming receipt is incoherent",
                ),
                (
                    "world-readable-arming-receipt",
                    "world_readable",
                    "not private 0600",
                ),
            ):
                refused_root = base / name
                expect_failure(refused_root, refusal, arming_mode=mode)
                assert not refused_root.exists(), (
                    f"{name} created series state before the launchd arming gate"
                )

            expect_failure(
                base / "duplicate-org",
                "terminal result violates the registered batch",
                process_mode="duplicate_org",
            )
            expect_failure(
                base / "duplicate-job",
                "job ids overlap",
                process_mode="duplicate_job",
            )
            expect_failure(
                base / "bad-conservation",
                "customer conservation is not exact",
                process_mode="bad_conservation",
            )
            expect_failure(
                base / "bad-tokens",
                "terminal token conservation is not exact",
                process_mode="bad_tokens",
            )
            expect_failure(
                base / "backward-cache",
                "cache observation moved backward",
                client_mode="backward_cache",
            )
            expect_failure(
                base / "identity-drift",
                "pod identity changed across samples",
                client_mode="identity_drift",
            )
            expect_failure(
                base / "early-create",
                "provider create preceded complete 7,200-job acceptance",
                client_mode="early_create",
            )
            slow = base / "slow-acquisition"
            assert invoke(slow, client_mode="slow_acquisition") == 0
            slow_terminal = json.loads((slow / "terminal-receipt.json").read_text())
            assert (
                slow_terminal["acquisition_classification"]["status"]
                == "acquisition_regime"
            )
            assert (
                slow_terminal["acquisition_classification"][
                    "continuity_headline_eligible"
                ]
                is False
            )
            expect_failure(
                base / "audit-foreign",
                "terminal pod must match exactly one durable placement attempt",
                client_mode="audit_foreign_pod",
            )
            expect_failure(
                base / "audit-incomplete",
                "placement decision audit is incomplete or incoherent",
                client_mode="audit_incomplete",
            )
            expect_failure(
                base / "audit-not-compact",
                "placement decision audit is incomplete or incoherent",
                client_mode="audit_not_compact",
            )
            expect_failure(
                base / "audit-full-row",
                "placement decision audit is incomplete or incoherent",
                client_mode="audit_full_row",
            )
    finally:
        os.chdir(old_cwd)
    print(
        "STACKED N24 ADMISSION REGRESSION PASS: 24 tenants/7200 distinct jobs; "
        "one-shot lock; exact customer conservation; monotone pod-cache stamps; "
        "duplicate tenant/job, token mismatch, identity drift, early create, and "
        "backward-cache reds; immutable exact pre-spend copy with stale/source-drift "
        "refusals; launchd arming receipt gates all demand before filesystem or network "
        "effects; same-clock acquisition classification and 600-second headline fence"
    )


if __name__ == "__main__":
    main()
