#!/usr/bin/env python3
"""Run the frozen 300-job PARA workload through the public customer API.

This is deliberately a customer, not operator, workload path after initial
organization/key/funding setup.  It freezes every job body and idempotency key,
proves all work was durably held before capacity became ready, walks the public
usage ledger through pagination, and emits only secret-free artifacts.
"""

from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request


MODEL = "Qwen/Qwen2.5-7B-Instruct"
MODEL_REVISION = "a09a35458c702b33eeacc393d103063234e8bc28"
EXPECTED_WORKLOAD_SHA256 = (
    "2392bb588923e88dc3f1473a9393a0e099a19a4889ccbd1d945b33df9e5ed205"
)
INPUT_PRICE = 100_000
OUTPUT_PRICE = 400_000
CONTEXT_TOKENS = 8_192
MAX_COMPLETION_TOKENS = 64
EXPECTED_PROMPT_TOKENS = 3_960
EXPECTED_HOLD_PER_JOB = 846
CREDITS = 2_000_000
N_JOBS = 300
PARA = (
    "Background agents batch work across long horizons; the serving plane "
    "trades latency for throughput under strict cost accounting. Queue "
    "depth, chunked prefill, KV reuse, and admission control each move "
    "delivered dollars per token, and every scheduling gap is billed idle "
    "capacity. Ledger conservation requires that every micro-USD of a "
    "pod's charge lands on exactly one logical response. "
)


class HttpFailure(RuntimeError):
    def __init__(self, status: int, path: str, detail: str):
        super().__init__(f"HTTP {status} for {path}: {detail[:240]}")
        self.status = status


def atomic_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def atomic_json(path: Path, value: Any) -> None:
    atomic_bytes(
        path,
        (json.dumps(value, sort_keys=True, indent=2) + "\n").encode(),
    )


def parse_admin_key(path: Path) -> str:
    for line in path.read_text().splitlines():
        if line.startswith("WEINFER_ADMIN_KEY="):
            value = line.split("=", 1)[1].split()[0]
            if value:
                return value
    raise RuntimeError(f"admin key missing from {path}")


class Client:
    def __init__(self, base: str, http_failure_log: Path | None = None):
        self.base = base.rstrip("/")
        self.http_failure_log = http_failure_log
        self.http_failure_lock = threading.Lock()

    @staticmethod
    def typed_weinfer_error(payload: bytes) -> tuple[bool, str, str | None]:
        try:
            value = json.loads(payload)
            error = value.get("error")
        except Exception:
            return False, "request refused", None
        if not isinstance(error, dict):
            return False, "request refused", None
        kind = error.get("code")
        message = error.get("message")
        typed = (
            isinstance(error.get("type"), str)
            and isinstance(kind, str)
            and isinstance(message, str)
        )
        return typed, str(message) if isinstance(message, str) else "request refused", (
            str(kind) if isinstance(kind, str) else None
        )

    def record_http_failure(
        self,
        *,
        method: str,
        path: str,
        status: int,
        content_type: str,
        payload: bytes,
        attempt: int,
        typed_weinfer_error: bool,
        error_code: str | None,
        retrying: bool,
    ) -> None:
        if self.http_failure_log is None:
            return
        record = {
            "attempt": attempt,
            "content_type": content_type,
            "error_code": error_code,
            "method": method,
            "path": path,
            "payload_base64": base64.b64encode(payload).decode(),
            "payload_sha256": hashlib.sha256(payload).hexdigest(),
            "recorded_at_micros": time.time_ns() // 1_000,
            "retrying": retrying,
            "status": status,
            "typed_weinfer_error": typed_weinfer_error,
        }
        encoded = (json.dumps(record, sort_keys=True) + "\n").encode()
        self.http_failure_log.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.http_failure_lock:
            fd = os.open(
                self.http_failure_log,
                os.O_APPEND | os.O_CREAT | os.O_WRONLY,
                0o600,
            )
            try:
                os.write(fd, encoded)
                os.fsync(fd)
            finally:
                os.close(fd)

    def request(
        self,
        method: str,
        path: str,
        *,
        bearer: str,
        body: Any | None = None,
        headers: dict[str, str] | None = None,
        allowed: tuple[int, ...] = (200,),
        retries: int = 4,
    ) -> tuple[int, Any]:
        encoded = None
        # The provider proxy rejects Python's default Python-urllib/*
        # fingerprint before a request reaches WeInfer (observed on the
        # first public batch POST).  Pin a truthful product client agent;
        # this changes no application/auth bytes and is regression-enforced.
        wire_headers = {
            "Authorization": f"Bearer {bearer}",
            "User-Agent": "WeInfer-Batch-Canary/1.0",
        }
        if body is not None:
            encoded = json.dumps(body, separators=(",", ":")).encode()
            wire_headers["Content-Type"] = "application/json"
        if headers:
            wire_headers.update(headers)
        for attempt in range(retries):
            request = urllib.request.Request(
                self.base + path,
                data=encoded,
                method=method,
                headers=wire_headers,
            )
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    status_code = response.status
                    payload = response.read(2 * 1024 * 1024)
                    content_type = response.headers.get("content-type", "")
            except urllib.error.HTTPError as error:
                status_code = error.code
                payload = error.read(4096)
                content_type = error.headers.get("content-type", "")
            except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
                if attempt + 1 == retries:
                    raise RuntimeError(f"transport failed for {path}: {error}") from error
                time.sleep(min(2**attempt, 8))
                continue
            if status_code in allowed:
                if not payload:
                    return status_code, None
                try:
                    return status_code, json.loads(payload)
                except json.JSONDecodeError as error:
                    raise RuntimeError(f"non-JSON success for {path}") from error
            typed, detail, error_code = self.typed_weinfer_error(payload)
            transient_proxy_404 = (
                status_code == 404
                and not typed
                and (
                    (method == "POST" and path == "/v1/jobs")
                    or (method == "GET" and path.startswith("/v1/jobs/"))
                    or (
                        method == "GET"
                        and path.startswith("/admin/jobs/")
                        and path.endswith("/profile-evidence")
                    )
                )
            )
            retrying = (
                status_code in (408, 500, 502, 503, 504) or transient_proxy_404
            ) and attempt + 1 < retries
            self.record_http_failure(
                method=method,
                path=path,
                status=status_code,
                content_type=content_type,
                payload=payload,
                attempt=attempt + 1,
                typed_weinfer_error=typed,
                error_code=error_code,
                retrying=retrying,
            )
            if retrying:
                time.sleep(min(2**attempt, 8))
                continue
            if not typed:
                detail = (
                    "untyped response "
                    f"sha256={hashlib.sha256(payload).hexdigest()}"
                )
            raise HttpFailure(status_code, path, str(detail))
        raise AssertionError("unreachable")


def workload() -> tuple[list[dict[str, Any]], bytes]:
    rows = []
    for index in range(N_JOBS):
        content = (
            f"Case {index:04d}. Read the operations context and answer in one "
            "sentence: which single lever most reduces delivered cost?\n\n"
            + PARA * 55
        )
        rows.append(
            {
                "model": MODEL,
                "messages": [{"role": "user", "content": content}],
                "max_completion_tokens": MAX_COMPLETION_TOKENS,
                "temperature": 0,
                "seed": 0,
            }
        )
    blob = "\n".join(json.dumps(row, sort_keys=True) for row in rows).encode()
    digest = hashlib.sha256(blob).hexdigest()
    if digest != EXPECTED_WORKLOAD_SHA256:
        raise RuntimeError(
            f"frozen workload drift: {digest} != {EXPECTED_WORKLOAD_SHA256}"
        )
    return rows, blob


def ceil_price(tokens: int, rate: int) -> int:
    return (tokens * rate + 999_999) // 1_000_000


def assert_balance(balance: dict[str, Any]) -> None:
    credits = int(balance["credits_micro_usd"])
    spent = int(balance["spent_micro_usd"])
    reserved = int(balance["reserved_micro_usd"])
    available = int(balance["available_micro_usd"])
    if credits != spent + reserved + available:
        raise AssertionError((balance, "credits != spent + reserved + available"))


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit(
            "usage: BATCH_RUN_ID=<id> public_batch_canary.py <public-base> <credentials-file>"
        )
    run_id = os.environ.get("BATCH_RUN_ID", "")
    if not run_id or len(run_id) > 96 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in run_id):
        raise SystemExit("BATCH_RUN_ID must be 1-96 visible alphanumeric/-/_ characters")
    base = sys.argv[1]
    credential_file = Path(sys.argv[2])
    deadline_seconds = int(os.environ.get("BATCH_DEADLINE_SECONDS", "2400"))
    poll_budget_seconds = int(
        os.environ.get("BATCH_POLL_BUDGET_SECONDS", str(deadline_seconds + 600))
    )
    submit_workers = int(os.environ.get("BATCH_SUBMIT_CONCURRENCY", "16"))
    poll_workers = int(os.environ.get("BATCH_POLL_CONCURRENCY", "32"))
    poll_interval_seconds = int(os.environ.get("BATCH_POLL_INTERVAL_SECONDS", "10"))
    if deadline_seconds < 1 or poll_budget_seconds < deadline_seconds:
        raise SystemExit("deadline/poll budget invalid")
    if not 1 <= submit_workers <= 64 or not 1 <= poll_workers <= 64:
        raise SystemExit("batch concurrency must be within 1..64")
    if not 1 <= poll_interval_seconds <= 600:
        raise SystemExit("batch poll interval must be within 1..600 seconds")

    artifact_root = Path(
        os.environ.get(
            "BATCH_ARTIFACT_DIR",
            str(Path.home() / ".weinfer" / f"public-batch-{run_id}"),
        )
    )
    artifact_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(artifact_root, 0o700)
    observation = artifact_root / f"observation-{time.time_ns()}"
    observation.mkdir(mode=0o700)
    run_pointer = artifact_root / "run.json"
    preexisting_run = run_pointer.exists()
    rows, blob = workload()
    atomic_bytes(artifact_root / "workload.jsonl", blob)
    atomic_bytes(
        artifact_root / "workload.sha256",
        (EXPECTED_WORKLOAD_SHA256 + "\n").encode(),
    )

    client = Client(base, observation / "http_failures.jsonl")
    admin_key = parse_admin_key(credential_file)
    org_id = f"org-batch-{run_id}"
    key_root = Path(os.environ.get("BATCH_KEY_STORE_ROOT", str(Path.home() / ".weinfer")))
    key_file = key_root / f"public-batch-{run_id}.key"
    key_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(key_root, 0o700)
    started_at = time.time_ns() // 1_000

    print(f"[batch {run_id}] organization + issued customer credential", flush=True)
    client.request(
        "POST",
        "/admin/organizations",
        bearer=admin_key,
        body={"org_id": org_id, "name": f"Public batch {run_id}"},
        allowed=(200, 409),
    )
    if key_file.exists():
        customer_key = key_file.read_text()
        if not customer_key:
            raise RuntimeError("persisted customer key is empty")
        if stat.S_IMODE(key_file.stat().st_mode) != 0o600:
            raise RuntimeError("persisted customer key is not mode 0600")
        print(f"[batch {run_id}] reusing run-scoped credential", flush=True)
    else:
        _, issued = client.request(
            "POST",
            f"/admin/organizations/{org_id}/keys",
            bearer=admin_key,
            body={},
        )
        customer_key = issued["raw_key"]
        atomic_bytes(key_file, customer_key.encode())
        print(f"[batch {run_id}] issued credential persisted mode 0600", flush=True)

    def get_balance() -> dict[str, Any]:
        _, result = client.request("GET", "/v1/balance", bearer=customer_key)
        assert_balance(result)
        return result

    before_grant = get_balance()
    if int(before_grant["credits_micro_usd"]) not in (0, CREDITS):
        raise RuntimeError(f"foreign credit state for {org_id}: {before_grant}")
    client.request(
        "POST",
        f"/admin/organizations/{org_id}/credits",
        bearer=admin_key,
        body={
            "grant_id": f"batch-grant-{run_id}",
            "amount_micro_usd": CREDITS,
            "memo": f"public-batch-{run_id}",
        },
    )
    funded = get_balance()
    if int(funded["credits_micro_usd"]) != CREDITS:
        raise AssertionError((funded, "funding is not exact"))

    _, models = client.request("GET", "/v1/models", bearer=customer_key)
    matching = [model for model in models["data"] if model["id"] == MODEL]
    if len(matching) != 1:
        raise AssertionError((models, "exactly one requested model required"))
    model = matching[0]
    expected_model = {
        "context_length": CONTEXT_TOKENS,
        "routable": True,
    }
    for key, value in expected_model.items():
        if model[key] != value:
            raise AssertionError((model, f"unexpected {key}"))
    if model["pricing"] != {
        "input_micro_usd_per_mtok": INPUT_PRICE,
        "output_micro_usd_per_mtok": OUTPUT_PRICE,
    }:
        raise AssertionError((model, "unexpected prices"))

    print(
        f"[batch {run_id}] submitting {N_JOBS} frozen jobs "
        f"(sha256 {EXPECTED_WORKLOAD_SHA256}, deadline {deadline_seconds}s)",
        flush=True,
    )
    accept_lock = threading.Lock()
    first_accept: int | None = None
    last_accept: int | None = None

    def submit(index: int) -> tuple[int, dict[str, Any]]:
        nonlocal first_accept, last_accept
        _, accepted = client.request(
            "POST",
            "/v1/jobs",
            bearer=customer_key,
            body=rows[index],
            headers={
                "Idempotency-Key": f"batch-{run_id}-{index:04d}",
                "x-weinfer-deadline-seconds": str(deadline_seconds),
            },
            allowed=(202,),
        )
        now = time.time_ns() // 1_000
        with accept_lock:
            first_accept = now if first_accept is None else min(first_accept, now)
            last_accept = now if last_accept is None else max(last_accept, now)
        return index, accepted

    accepted_by_index: dict[int, dict[str, Any]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=submit_workers) as executor:
        futures = [executor.submit(submit, index) for index in range(N_JOBS)]
        for done, future in enumerate(concurrent.futures.as_completed(futures), 1):
            index, accepted = future.result()
            accepted_by_index[index] = accepted
            if done % 50 == 0:
                print(f"[batch {run_id}] accepted {done}/{N_JOBS}", flush=True)
    if set(accepted_by_index) != set(range(N_JOBS)):
        raise AssertionError("submission set is incomplete")
    job_ids = [accepted_by_index[index]["job_id"] for index in range(N_JOBS)]
    if len(set(job_ids)) != N_JOBS:
        raise AssertionError("300 bodies did not create 300 distinct logical jobs")
    accepted_at = time.time_ns() // 1_000
    atomic_json(artifact_root / "job_ids.json", {"run_id": run_id, "job_ids": job_ids})
    atomic_bytes(
        observation / "accept_responses.jsonl",
        (
            "\n".join(
                json.dumps(
                    {
                        "index": index,
                        "idempotency_key": f"batch-{run_id}-{index:04d}",
                        "response": accepted_by_index[index],
                    },
                    sort_keys=True,
                )
                for index in range(N_JOBS)
            )
            + "\n"
        ).encode(),
    )
    after_accept = get_balance()
    if not preexisting_run:
        expected_reserve = EXPECTED_HOLD_PER_JOB * N_JOBS
        if (
            int(after_accept["reserved_micro_usd"]) != expected_reserve
            or int(after_accept["spent_micro_usd"]) != 0
            or int(after_accept["available_micro_usd"]) != CREDITS - expected_reserve
        ):
            raise AssertionError(
                (after_accept, f"fresh acceptance must hold exactly {expected_reserve}")
            )
    balance_before_replay = get_balance()
    _, replay = client.request(
        "POST",
        "/v1/jobs",
        bearer=customer_key,
        body=rows[0],
        headers={
            "Idempotency-Key": f"batch-{run_id}-0000",
            "x-weinfer-deadline-seconds": str(deadline_seconds),
        },
        allowed=(202,),
    )
    if replay["job_id"] != job_ids[0]:
        raise AssertionError("idempotent replay returned a different job")
    if get_balance() != balance_before_replay:
        raise AssertionError("idempotent replay changed the balance ledger")
    print(
        f"[batch {run_id}] 300/300 accepted; row-0 replay identical; "
        f"fresh hold {EXPECTED_HOLD_PER_JOB * N_JOBS} micro",
        flush=True,
    )

    poll_started = time.time_ns() // 1_000
    pending = set(job_ids)
    terminal: dict[str, dict[str, Any]] = {}
    poll_deadline = time.monotonic() + poll_budget_seconds

    def poll(job_id: str) -> tuple[str, dict[str, Any]]:
        _, result = client.request("GET", f"/v1/jobs/{job_id}", bearer=customer_key)
        return job_id, result

    while pending and time.monotonic() < poll_deadline:
        with concurrent.futures.ThreadPoolExecutor(max_workers=poll_workers) as executor:
            futures = [executor.submit(poll, job_id) for job_id in sorted(pending)]
            for future in concurrent.futures.as_completed(futures):
                job_id, result = future.result()
                if result.get("status") in ("completed", "failed", "expired"):
                    terminal[job_id] = result
                    pending.remove(job_id)
        print(
            f"[batch {run_id}] terminal {len(terminal)}/{N_JOBS}; "
            f"pending {len(pending)}",
            flush=True,
        )
        if pending:
            time.sleep(poll_interval_seconds)
    completed_at = time.time_ns() // 1_000
    if pending:
        atomic_json(observation / "failure.json", {"reason": "poll timeout", "pending": sorted(pending)})
        raise RuntimeError(f"poll timeout with {len(pending)} jobs pending")
    statuses: dict[str, int] = {}
    for result in terminal.values():
        status_name = result["status"]
        statuses[status_name] = statuses.get(status_name, 0) + 1
    if statuses != {"completed": N_JOBS}:
        atomic_json(observation / "terminal_results.json", terminal)
        raise RuntimeError(f"not every job completed: {statuses}")

    total_prompt = 0
    total_completion = 0
    total_customer_charge = 0
    normalized_results = []
    for index, job_id in enumerate(job_ids):
        result = terminal[job_id]
        usage = result["usage"]
        prompt = int(usage["prompt_tokens"])
        completion = int(usage["completion_tokens"])
        if prompt != EXPECTED_PROMPT_TOKENS:
            raise AssertionError((index, prompt, "frozen prompt-token count drifted"))
        if not 0 <= completion <= MAX_COMPLETION_TOKENS:
            raise AssertionError((index, completion, "completion bound breached"))
        charge = result["charge"]
        expected_charge = ceil_price(prompt, INPUT_PRICE) + ceil_price(
            completion, OUTPUT_PRICE
        )
        if int(charge["total_micro_usd"]) != expected_charge:
            raise AssertionError((index, charge, expected_charge))
        if (
            int(charge["input_price_micro_per_mtok"]) != INPUT_PRICE
            or int(charge["output_price_micro_per_mtok"]) != OUTPUT_PRICE
            or result["reconciliation"] != "billed"
        ):
            raise AssertionError((index, charge, result["reconciliation"]))
        choices = result.get("response", {}).get("choices", [])
        content = choices[0].get("message", {}).get("content", "") if choices else ""
        if not isinstance(content, str) or not content.strip():
            raise AssertionError((index, "empty model result"))
        total_prompt += prompt
        total_completion += completion
        total_customer_charge += expected_charge
        normalized_results.append(
            {
                "index": index,
                "job_id": job_id,
                "usage": usage,
                "charge": charge,
                "reconciliation": result["reconciliation"],
                "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
            }
        )
    if total_prompt + total_completion < 1_000_000:
        raise AssertionError("measured billable-token floor was not reached")

    usage_rows: dict[str, dict[str, Any]] = {}
    after: str | None = None
    while True:
        path = "/v1/usage?limit=200"
        if after is not None:
            path += "&after=" + urllib.parse.quote(after, safe="")
        _, page = client.request("GET", path, bearer=customer_key)
        for row in page["data"]:
            if row["job_id"] in usage_rows:
                raise AssertionError("usage pagination returned a duplicate job")
            usage_rows[row["job_id"]] = row
        after = page.get("next_after")
        if after is None:
            break
    if set(usage_rows) != set(job_ids):
        raise AssertionError(
            (len(usage_rows), len(job_ids), "usage page walk did not equal the run jobs")
        )
    for result in normalized_results:
        row = usage_rows[result["job_id"]]
        if (
            row["usage"] != result["usage"]
            or row["charge"] != result["charge"]
            or row["reconciliation"] != "billed"
        ):
            raise AssertionError((result["job_id"], "usage/job surface mismatch"))
    final_balance = get_balance()
    if (
        int(final_balance["reserved_micro_usd"]) != 0
        or int(final_balance["spent_micro_usd"]) != total_customer_charge
        or int(final_balance["available_micro_usd"]) != CREDITS - total_customer_charge
    ):
        raise AssertionError((final_balance, total_customer_charge))

    summary = {
        "object": "public_batch_result",
        "run_id": run_id,
        "org_id": org_id,
        "model": MODEL,
        "model_revision": MODEL_REVISION,
        "workload_sha256": EXPECTED_WORKLOAD_SHA256,
        "intended_jobs": N_JOBS,
        "accepted_jobs": N_JOBS,
        "completed_jobs": N_JOBS,
        "deadline_seconds": deadline_seconds,
        "submit_concurrency": submit_workers,
        "poll_concurrency": poll_workers,
        "poll_interval_seconds": poll_interval_seconds,
        "expected_hold_per_job_micro_usd": EXPECTED_HOLD_PER_JOB,
        "accepted_reservation_micro_usd": EXPECTED_HOLD_PER_JOB * N_JOBS,
        "prompt_tokens": total_prompt,
        "completion_tokens": total_completion,
        "billable_tokens": total_prompt + total_completion,
        "customer_charge_micro_usd": total_customer_charge,
        "customer_balance": final_balance,
        "timestamps_micros": {
            "started": started_at,
            "first_accept": first_accept,
            "last_accept": last_accept,
            "all_accepted": accepted_at,
            "poll_started": poll_started,
            "all_completed": completed_at,
        },
        "job_ids_sha256": hashlib.sha256(
            ("\n".join(job_ids) + "\n").encode()
        ).hexdigest(),
        "usage_rows": len(usage_rows),
        "idempotent_replay_job_id": job_ids[0],
    }
    atomic_json(observation / "result.json", summary)
    atomic_bytes(
        observation / "terminal_results.jsonl",
        (
            "\n".join(json.dumps(row, sort_keys=True) for row in normalized_results)
            + "\n"
        ).encode(),
    )
    atomic_json(
        observation / "balances.json",
        {
            "before_grant": before_grant,
            "funded": funded,
            "after_accept": after_accept,
            "before_replay": balance_before_replay,
            "final": final_balance,
        },
    )
    atomic_json(
        run_pointer,
        {
            "run_id": run_id,
            "org_id": org_id,
            "public_base": base,
            "workload_sha256": EXPECTED_WORKLOAD_SHA256,
            "job_ids_file": str(artifact_root / "job_ids.json"),
            "latest_observation": str(observation),
            "recorded_at_epoch": int(time.time()),
        },
    )
    print(
        f"[batch {run_id}] COMPLETE: {N_JOBS}/{N_JOBS}, "
        f"{total_prompt + total_completion} billable tokens, "
        f"customer charge {total_customer_charge} micro; artifacts {observation}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"PUBLIC BATCH FAILED: {error}", file=sys.stderr)
        raise
