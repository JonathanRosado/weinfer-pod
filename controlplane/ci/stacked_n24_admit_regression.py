#!/usr/bin/env python3
"""Zero-provider regression for the 24-batch admission coordinator."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
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


def invoke(root: Path, *, process_mode: str = "good", client_mode: str = "good") -> int:
    FakeProcess.mode = process_mode
    FakeClient.mode = client_mode
    credentials = root.parent / f"credentials-{root.name}.env"
    credentials.write_text("WEINFER_ADMIN_KEY=test-admin-key\n")
    argv = sys.argv
    sys.argv = [
        "stacked_n24_admit.py",
        "anchor",
        "http://127.0.0.1:1",
        str(credentials),
        f"regression-{root.name}",
        str(root),
    ]
    try:
        return MODULE.main()
    finally:
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
            source = json.loads((good / "source-receipt.json").read_text())
            assert source["poll_concurrency_per_batch"] == MODULE.POLL_CONCURRENCY == 8
            assert source["poll_interval_seconds"] == MODULE.POLL_INTERVAL_SECONDS == 60
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
    finally:
        os.chdir(old_cwd)
    print(
        "STACKED N24 ADMISSION REGRESSION PASS: 24 tenants/7200 distinct jobs; "
        "one-shot lock; exact customer conservation; monotone pod-cache stamps; "
        "duplicate tenant/job, token mismatch, identity drift, early create, and "
        "backward-cache reds"
    )


if __name__ == "__main__":
    main()
