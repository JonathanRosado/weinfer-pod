#!/usr/bin/env python3
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import urllib.parse


ADMIN = "admin-regression"
CUSTOMER = "customer-regression"
PRICE_IN = 100_000
PRICE_OUT = 400_000
HOLD = 846
CHARGE = 398
N = 300


class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.credits = 0
        self.reserved = 0
        self.spent = 0
        self.grants: dict[str, dict] = {}
        self.jobs: dict[str, dict] = {}
        self.idempotency: dict[str, str] = {}
        self.new_jobs = 0
        self.poll_started = False
        self.job_post_calls = 0
        self.transient_job_404s: set[str] = set()
        self.typed_missing_model_calls = 0

    def balance(self) -> dict:
        return {
            "credits_micro_usd": self.credits,
            "reserved_micro_usd": self.reserved,
            "spent_micro_usd": self.spent,
            "available_micro_usd": self.credits - self.reserved - self.spent,
            "billing_enforced": True,
        }

    def complete_all(self) -> None:
        if self.poll_started:
            return
        self.poll_started = True
        self.reserved = 0
        self.spent = CHARGE * len(self.jobs)
        for job in self.jobs.values():
            job["status"] = "completed"


STATE = State()


def launch_contract() -> tuple[dict, str, str, dict]:
    value = {
        "version": "pod-launch-v1",
        "served_model": "Qwen/Qwen2.5-7B-Instruct",
        "model_revision": "a09a35458c702b33eeacc393d103063234e8bc28",
        "tokenizer_revision": "a09a35458c702b33eeacc393d103063234e8bc28",
        "image_digest": "ghcr.io/jonathanrosado/weinfer-pod@sha256:160a926826565b1ed0134335f3f68e65ed457fcb034058639fc5c9b5c7ec2613",
        "pod_args": "",
        "vllm_canonical_args": "--seed 0 --max-num-batched-tokens 16384 --max-num-seqs 256 --gpu-memory-utilization 0.92 --enable-chunked-prefill --revision a09a35458c702b33eeacc393d103063234e8bc28 --tokenizer-revision a09a35458c702b33eeacc393d103063234e8bc28 --max-model-len 8192",
        "concurrency": "64",
        "allocator_config": "expandable_segments:True",
        "worker_sha256": "7bd6f06f07f68afb24bbd8fec086bf3be04d574ebe5a86791e9f2c230cca5f6b",
        "gpu_sku": "NVIDIA RTX A4500",
        "cuda_class": "12",
        "cuda_pin": ["12.8"],
        "max_context_tokens": 8192,
        "vram_gb": 20,
    }
    raw = json.dumps(value, separators=(",", ":"))
    digest = hashlib.sha256(raw.encode()).hexdigest()
    identity = {
        "served_model": value["served_model"],
        "model_revision": value["model_revision"],
        "tokenizer_revision": value["tokenizer_revision"],
        "image_digest": value["image_digest"],
        "engine_config_digest": "0846aa7e4989aa1ed918b8c0f0a529ee155337d1566df365e0bcc94413fdf82b",
        "gpu_sku": value["gpu_sku"],
        "cuda_class": value["cuda_class"],
    }
    return value, raw, digest, identity


class Handler(BaseHTTPRequestHandler):
    server_version = "WeInferBatchFake/1"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def body(self) -> dict:
        length = int(self.headers.get("content-length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def send_json(self, status: int, value: dict) -> None:
        payload = json.dumps(value, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def authorized(self, expected: str) -> bool:
        return (
            self.headers.get("authorization") == f"Bearer {expected}"
            and self.headers.get("user-agent") == "WeInfer-Batch-Canary/1.0"
        )

    def do_POST(self) -> None:
        if self.path == "/admin/organizations":
            if not self.authorized(ADMIN):
                return self.send_json(401, {"error": {"message": "bad admin"}})
            self.body()
            return self.send_json(200, {"status": "active"})
        if self.path.endswith("/keys") and self.path.startswith("/admin/organizations/"):
            if not self.authorized(ADMIN):
                return self.send_json(401, {"error": {"message": "bad admin"}})
            self.body()
            return self.send_json(200, {"raw_key": CUSTOMER})
        if self.path.endswith("/credits") and self.path.startswith("/admin/organizations/"):
            if not self.authorized(ADMIN):
                return self.send_json(401, {"error": {"message": "bad admin"}})
            body = self.body()
            with STATE.lock:
                prior = STATE.grants.get(body["grant_id"])
                if prior is not None and prior != body:
                    return self.send_json(409, {"error": {"message": "grant conflict"}})
                if prior is None:
                    STATE.grants[body["grant_id"]] = body
                    STATE.credits += int(body["amount_micro_usd"])
            return self.send_json(200, {"status": "granted"})
        if self.path == "/v1/jobs":
            if not self.authorized(CUSTOMER):
                return self.send_json(401, {"error": {"message": "bad key"}})
            body = self.body()
            idem = self.headers.get("idempotency-key")
            if not idem or self.headers.get("x-weinfer-deadline-seconds") != "2400":
                return self.send_json(400, {"error": {"message": "contract drift"}})
            with STATE.lock:
                STATE.job_post_calls += 1
                if body.get("model") == "Missing/Model":
                    STATE.typed_missing_model_calls += 1
                    return self.send_json(
                        404,
                        {
                            "error": {
                                "type": "model_not_found",
                                "code": "model_not_found",
                                "message": "model is not in the catalog",
                                "param": None,
                            }
                        },
                    )
                if idem not in STATE.transient_job_404s:
                    STATE.transient_job_404s.add(idem)
                    payload = b"proxy route warming"
                    self.send_response(404)
                    self.send_header("content-type", "text/plain")
                    self.send_header("content-length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if idem in STATE.idempotency:
                    job_id = STATE.idempotency[idem]
                else:
                    if body.get("model") != "Qwen/Qwen2.5-7B-Instruct":
                        return self.send_json(400, {"error": {"message": "wrong model"}})
                    job_id = f"resp-batch-{len(STATE.jobs):04d}"
                    STATE.idempotency[idem] = job_id
                    STATE.jobs[job_id] = {"request": body, "status": "queued"}
                    STATE.new_jobs += 1
                    STATE.reserved += HOLD
            return self.send_json(202, {"job_id": job_id, "status": "queued"})
        return self.send_json(404, {"error": {"message": "not found"}})

    def do_GET(self) -> None:
        if self.path == "/v1/balance":
            if not self.authorized(CUSTOMER):
                return self.send_json(401, {"error": {"message": "bad key"}})
            with STATE.lock:
                return self.send_json(200, STATE.balance())
        if self.path == "/v1/models":
            if not self.authorized(CUSTOMER):
                return self.send_json(401, {"error": {"message": "bad key"}})
            return self.send_json(
                200,
                {
                    "object": "list",
                    "data": [
                        {
                            "id": "Qwen/Qwen2.5-7B-Instruct",
                            "context_length": 8192,
                            "routable": True,
                            "pricing": {
                                "input_micro_usd_per_mtok": PRICE_IN,
                                "output_micro_usd_per_mtok": PRICE_OUT,
                            },
                        }
                    ],
                },
            )
        if self.path.startswith("/v1/jobs/"):
            if not self.authorized(CUSTOMER):
                return self.send_json(401, {"error": {"message": "bad key"}})
            job_id = self.path.rsplit("/", 1)[1]
            with STATE.lock:
                STATE.complete_all()
                job = STATE.jobs.get(job_id)
                if job is None:
                    return self.send_json(404, {"error": {"message": "not found"}})
                return self.send_json(200, terminal(job_id))
        if self.path.startswith("/v1/usage"):
            if not self.authorized(CUSTOMER):
                return self.send_json(401, {"error": {"message": "bad key"}})
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            limit = int(query.get("limit", ["200"])[0])
            start = int(query.get("after", ["0"])[0])
            with STATE.lock:
                ids = sorted(STATE.jobs)
                page_ids = ids[start : start + limit]
                rows = [usage_row(job_id) for job_id in page_ids]
                next_after = str(start + limit) if start + limit < len(ids) else None
            return self.send_json(200, {"data": rows, "next_after": next_after})
        if self.path.startswith("/admin/jobs/") and self.path.endswith("/profile-evidence"):
            if not self.authorized(ADMIN):
                return self.send_json(401, {"error": {"message": "bad admin"}})
            job_id = self.path.split("/")[3]
            with STATE.lock:
                if job_id not in STATE.jobs:
                    return self.send_json(404, {"error": {"message": "not found"}})
                return self.send_json(200, profile_evidence(job_id))
        return self.send_json(404, {"error": {"message": "not found"}})


def index_for(job_id: str) -> int:
    return int(job_id.rsplit("-", 1)[1])


def terminal(job_id: str) -> dict:
    return {
        "job_id": job_id,
        "status": "completed",
        "reconciliation": "billed",
        "usage": {"prompt_tokens": 3960, "completion_tokens": 4},
        "charge": {
            "catalog_revision": "cat-live-1",
            "input_price_micro_per_mtok": PRICE_IN,
            "output_price_micro_per_mtok": PRICE_OUT,
            "total_micro_usd": CHARGE,
        },
        "response": {
            "choices": [
                {"message": {"role": "assistant", "content": f"batch-ok-{index_for(job_id):04d}"}}
            ]
        },
    }


def usage_row(job_id: str) -> dict:
    result = terminal(job_id)
    return {
        "job_id": job_id,
        "status": "completed",
        "usage": result["usage"],
        "charge": result["charge"],
        "reconciliation": "billed",
    }


def profile_evidence(job_id: str) -> dict:
    idx = index_for(job_id)
    contract, raw, digest, identity = launch_contract()
    completed = 21_000_000 + idx * 300_000
    evidence = {
        "job_id": job_id,
        "logical_response_id": job_id,
        "job_state": "completed",
        "job_failure": None,
        "lease_generation": 1,
        "pod_id": "pod-batch-regression",
        "pool": "community-qwen7b-0",
        "pod_state": "settled_provisional",
        "model": "Qwen/Qwen2.5-7B-Instruct",
        "gpu_sku": "NVIDIA RTX A4500",
        "created_at_micros": 2_000_000,
        "provider_created_at_micros": 1_000_000,
        "ready_at_micros": 12_000_000,
        "completed_at_micros": completed,
        "draining_at_micros": 120_000_000,
        "terminate_requested_at_micros": 120_010_000,
        "terminated_at_micros": 123_000_000,
        "charged_at_micros": 200_000_000,
        "settled_at_micros": 201_000_000,
        "charge_micro_usd": 10_000,
        "allocated_cost_micro_usd": 10_000,
        "lifetime_micros": 122_000_000,
        "launch_contract": raw,
        "launch_contract_digest": digest,
        "provider_rate_micro_per_hour": 190_000,
        "recent_provision_failures": 2,
        "attempts": [
            {
                "attempt": 1,
                "runtime_micros": 1_000_000,
                "physical_prompt_tokens": 3960,
                "physical_completion_tokens": 4,
                "billable": True,
                "needs_reconciliation": False,
            }
        ],
    }
    return {
        "object": "profile_evidence",
        "launch_contract_digest": digest,
        "engine_config_digest": identity["engine_config_digest"],
        "profile_identity": identity,
        "launch_contract": contract,
        "evidence": evidence,
    }


def run() -> None:
    with tempfile.TemporaryDirectory(prefix="weinfer-public-batch-") as temp:
        root = Path(temp)
        credentials = root / "credentials.env"
        credentials.write_text(f"WEINFER_ADMIN_KEY={ADMIN}\n")
        os.chmod(credentials, 0o600)
        artifacts = root / "artifacts"
        key_root = root / "keys"
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        env = os.environ.copy()
        env.update(
            {
                "BATCH_RUN_ID": "regression-1",
                "BATCH_ARTIFACT_DIR": str(artifacts),
                "BATCH_KEY_STORE_ROOT": str(key_root),
                "BATCH_DEADLINE_SECONDS": "2400",
                "BATCH_POLL_BUDGET_SECONDS": "2400",
            }
        )
        first = subprocess.run(
            ["bash", "scripts/public_batch_canary.sh", base, str(credentials)],
            env=env,
            text=True,
            capture_output=True,
            timeout=120,
        )
        if first.returncode != 0:
            raise SystemExit(first.stdout + first.stderr)
        candidate_root = root / "profile"
        collected = subprocess.run(
            [
                "bash",
                "scripts/public_batch_profile_collect.sh",
                base,
                str(credentials),
                "regression-1",
                str(candidate_root),
            ],
            env=env,
            text=True,
            capture_output=True,
            timeout=120,
        )
        if collected.returncode != 0:
            raise SystemExit(collected.stdout + collected.stderr)
        second = subprocess.run(
            ["bash", "scripts/public_batch_canary.sh", base, str(credentials)],
            env=env,
            text=True,
            capture_output=True,
            timeout=120,
        )
        if second.returncode != 0:
            raise SystemExit(second.stdout + second.stderr)
        snapshots = sorted(candidate_root.glob("observation-*"))
        assert len(snapshots) == 1
        candidate = json.loads((snapshots[0] / "profile_candidate.json").read_text())
        summary = json.loads((snapshots[0] / "summary.json").read_text())
        result_dirs = sorted(artifacts.glob("observation-*"))
        assert len(result_dirs) == 2
        first_result = json.loads((result_dirs[0] / "result.json").read_text())
        second_result = json.loads((result_dirs[1] / "result.json").read_text())
        assert first_result["job_ids_sha256"] == second_result["job_ids_sha256"]
        assert first_result["workload_sha256"] == (
            "2392bb588923e88dc3f1473a9393a0e099a19a4889ccbd1d945b33df9e5ed205"
        )
        assert first_result["accepted_reservation_micro_usd"] == 253_800
        assert first_result["billable_tokens"] == 300 * (3960 + 4)
        assert first_result["customer_charge_micro_usd"] == N * CHARGE
        first_http_failures = [
            json.loads(line)
            for line in (result_dirs[0] / "http_failures.jsonl").read_text().splitlines()
        ]
        assert len(first_http_failures) == N
        assert all(
            row["status"] == 404
            and row["path"] == "/v1/jobs"
            and row["typed_weinfer_error"] is False
            and row["retrying"] is True
            and row["payload_sha256"]
            == hashlib.sha256(b"proxy route warming").hexdigest()
            and row["payload_base64"] == "cHJveHkgcm91dGUgd2FybWluZw=="
            for row in first_http_failures
        )
        assert summary["jobs"] == N and summary["billable_tokens"] == 300 * (3960 + 4)
        assert summary["provider_charge_micro_usd"] == 10_000
        assert candidate["status"] == "candidate_only"
        assert candidate["promotion"] == {
            "eligible": False,
            "boot_samples_observed": 3,
            "boot_samples_required": 5,
            "saturation_samples_observed": 1,
            "decision": "review only; minimum boot-sample rule not met",
        }
        derivation = candidate["derivation"]
        assert len(derivation["boot_samples_micros"]) == 3
        assert len(derivation["boot_sample_sources"]) == 3
        assert len(set(derivation["boot_sample_sources"])) == 3
        expected_prior = env.get(
            "BATCH_PRIOR_PROFILE",
            "evidence/public-batch-live-1787630415/profile/final/profile_candidate.json",
        )
        assert derivation["prior_profile_source"] == expected_prior
        replay_env = env.copy()
        replay_env["BATCH_PRIOR_PROFILE"] = str(
            snapshots[0] / "profile_candidate.json"
        )
        self_prior = subprocess.run(
            [
                "bash",
                "scripts/public_batch_profile_collect.sh",
                base,
                str(credentials),
                "regression-1",
                str(root / "self-prior-profile"),
            ],
            env=replay_env,
            text=True,
            capture_output=True,
            timeout=120,
        )
        assert self_prior.returncode != 0
        assert "same batch run (would double count)" in (
            self_prior.stdout + self_prior.stderr
        )
        assert STATE.new_jobs == N
        # First run: one transient + one accepted POST per job, plus row-0
        # replay. Second run: one durable replay per job, plus row-0 replay.
        assert STATE.job_post_calls == N * 3 + 2
        assert len(STATE.grants) == 1
        assert STATE.credits == 2_000_000
        assert STATE.reserved == 0
        assert STATE.spent == N * CHARGE
        manifest_doc = json.loads((snapshots[0] / "MANIFEST.json").read_text())
        for name, expected in manifest_doc["files"].items():
            actual = hashlib.sha256((snapshots[0] / name).read_bytes()).hexdigest()
            assert actual == expected
        from public_batch_canary import Client, HttpFailure

        typed_log = root / "typed-http-failures.jsonl"
        typed_client = Client(base, typed_log)
        try:
            typed_client.request(
                "POST",
                "/v1/jobs",
                bearer=CUSTOMER,
                body={"model": "Missing/Model"},
                headers={
                    "Idempotency-Key": "typed-missing-model",
                    "x-weinfer-deadline-seconds": "2400",
                },
                allowed=(202,),
            )
        except HttpFailure as error:
            assert error.status == 404
            assert "model is not in the catalog" in str(error)
        else:
            raise AssertionError("typed model_not_found was retried or accepted")
        typed_rows = [json.loads(line) for line in typed_log.read_text().splitlines()]
        assert len(typed_rows) == 1
        assert typed_rows[0]["typed_weinfer_error"] is True
        assert typed_rows[0]["error_code"] == "model_not_found"
        assert typed_rows[0]["retrying"] is False
        assert STATE.typed_missing_model_calls == 1
        server.shutdown()
        thread.join(timeout=5)
        print("PUBLIC BATCH REGRESSION PASS: 300 accepted/completed, replay exact, pagination exact, profile conserved")


if __name__ == "__main__":
    run()
