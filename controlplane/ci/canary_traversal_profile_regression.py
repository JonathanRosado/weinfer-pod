#!/usr/bin/env python3
"""Exercise both priced canary profiles without any provider transport."""

from __future__ import annotations

from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import threading
from typing import Any

SCRIPT = Path(__file__).with_name("canary_traversal.sh")
CREDITS = 2_000_000


def ceil_price(tokens: int, rate: int) -> int:
    return (tokens * rate + 999_999) // 1_000_000


@dataclass(frozen=True)
class Profile:
    name: str
    model: str
    context: int
    input_price: int
    output_price: int
    max_tokens: int
    deadline: int

    @property
    def hold(self) -> int:
        return ceil_price(self.context, self.input_price) + ceil_price(
            self.max_tokens, self.output_price
        )

    @property
    def charge(self) -> int:
        return ceil_price(10, self.input_price) + ceil_price(1, self.output_price)


QWEN = Profile(
    name="qwen7b-consumer-v1",
    model="Qwen/Qwen2.5-7B-Instruct",
    context=8192,
    input_price=100_000,
    output_price=400_000,
    max_tokens=16,
    deadline=1200,
)
H100 = Profile(
    name="gpt-oss-120b-h100-v1",
    model="openai/gpt-oss-120b",
    context=131_072,
    input_price=900_000,
    output_price=2_700_000,
    max_tokens=8192,
    deadline=2400,
)


class State:
    def __init__(
        self,
        profile: Profile,
        *,
        content: str = "canary-ok",
        completion_tokens: int = 1,
    ):
        self.profile = profile
        self.content = content
        self.completion_tokens = completion_tokens
        self.credits = 0
        self.reserved = 0
        self.spent = 0
        self.job_posts = 0
        self.requests = 0
        self.deadlines: list[str | None] = []
        self.models: list[str] = []

    def balance(self) -> dict[str, int]:
        return {
            "credits_micro_usd": self.credits,
            "reserved_micro_usd": self.reserved,
            "spent_micro_usd": self.spent,
            "available_micro_usd": self.credits - self.reserved - self.spent,
        }

    def terminal(self) -> dict[str, Any]:
        charge = ceil_price(10, self.profile.input_price) + ceil_price(
            self.completion_tokens, self.profile.output_price
        )
        return {
            "job_id": "job-profile-canary",
            "status": "completed",
            "response": {
                "choices": [{"message": {"content": self.content}}],
            },
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": self.completion_tokens,
            },
            "charge": {
                "total_micro_usd": charge,
                "input_price_micro_per_mtok": self.profile.input_price,
                "output_price_micro_per_mtok": self.profile.output_price,
            },
            "reconciliation": "billed",
        }


def handler_for(state: State):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            return

        def send_json(self, status: int, value: object) -> None:
            payload = json.dumps(value, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def body(self) -> object:
            length = int(self.headers.get("Content-Length", "0"))
            return json.loads(self.rfile.read(length) or b"{}")

        def do_GET(self) -> None:  # noqa: N802
            state.requests += 1
            if self.path == "/v1/balance":
                self.send_json(200, state.balance())
                return
            if self.path == "/v1/models":
                profile = state.profile
                self.send_json(
                    200,
                    {
                        "data": [
                            {
                                "id": profile.model,
                                "context_length": profile.context,
                                "routable": True,
                                "pricing": {
                                    "input_micro_usd_per_mtok": profile.input_price,
                                    "output_micro_usd_per_mtok": profile.output_price,
                                },
                            }
                        ]
                    },
                )
                return
            if self.path == "/v1/jobs/job-profile-canary":
                state.reserved = 0
                state.spent = state.terminal()["charge"]["total_micro_usd"]
                self.send_json(200, state.terminal())
                return
            if self.path == "/v1/usage":
                self.send_json(200, {"data": [state.terminal()]})
                return
            self.send_json(404, {"error": "unexpected GET"})

        def do_POST(self) -> None:  # noqa: N802
            state.requests += 1
            body = self.body()
            if self.path == "/admin/organizations":
                self.send_json(200, {})
                return
            if self.path.endswith("/keys"):
                self.send_json(200, {"raw_key": "profile-customer-key"})
                return
            if self.path.endswith("/credits"):
                if not isinstance(body, dict) or body.get("amount_micro_usd") != CREDITS:
                    self.send_json(400, {"error": "wrong credit grant"})
                    return
                state.credits = CREDITS
                self.send_json(200, {})
                return
            if self.path == "/v1/jobs":
                if not isinstance(body, dict):
                    self.send_json(400, {"error": "job body is not an object"})
                    return
                state.models.append(str(body.get("model")))
                state.deadlines.append(self.headers.get("x-weinfer-deadline-seconds"))
                if (
                    body.get("model") != state.profile.model
                    or body.get("max_tokens") != state.profile.max_tokens
                    or self.headers.get("Idempotency-Key") is None
                ):
                    self.send_json(400, {"error": "wrong profile job"})
                    return
                state.job_posts += 1
                if state.job_posts == 1:
                    state.reserved = state.profile.hold
                self.send_json(202, {"job_id": "job-profile-canary"})
                return
            self.send_json(404, {"error": "unexpected POST"})

    return Handler


def run_profile(profile: Profile, *, explicit_profile: bool) -> None:
    state = State(profile)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="weinfer-profile-canary.") as raw:
            root = Path(raw)
            credentials = root / "credentials.env"
            credentials.write_text("WEINFER_ADMIN_KEY=profile-admin-key\n")
            credentials.chmod(0o600)
            artifacts = root / "artifacts"
            key_store = root / "keys"
            env = os.environ.copy()
            env.update(
                {
                    "HOME": str(root / "home"),
                    "CANARY_RUN_ID": f"regression-{profile.name}",
                    "CANARY_ARTIFACT_DIR": str(artifacts),
                    "CANARY_KEY_STORE": str(key_store),
                    "CANARY_POLL_BUDGET_SECONDS": str(profile.deadline),
                }
            )
            env.pop("CANARY_SERVING_PROFILE", None)
            if explicit_profile:
                env["CANARY_SERVING_PROFILE"] = profile.name
            completed = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    f"http://127.0.0.1:{server.server_port}",
                    str(credentials),
                ],
                env=env,
                text=True,
                capture_output=True,
                timeout=30,
            )
            assert completed.returncode == 0, (completed.stdout, completed.stderr)
            assert state.job_posts == 2, state.job_posts
            assert state.models == [profile.model, profile.model], state.models
            assert state.deadlines == [str(profile.deadline)] * 2, state.deadlines
            assert state.reserved == 0
            assert state.spent == state.terminal()["charge"]["total_micro_usd"]
            pointer = json.loads((artifacts / "run.json").read_text())
            assert pointer["serving_profile"] == profile.name, pointer
            assert pointer["model"] == profile.model, pointer
            assert pointer["job_id"] == "job-profile-canary", pointer
            key_path = key_store / f"canary-regression-{profile.name}.key"
            assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
            assert "CANARY COMPLETE" in completed.stdout
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def unknown_profile_refuses_before_transport() -> None:
    with tempfile.TemporaryDirectory(prefix="weinfer-profile-canary-red.") as raw:
        env = os.environ.copy()
        env.update(
            {
                "CANARY_RUN_ID": "regression-unknown",
                "CANARY_SERVING_PROFILE": "unknown-profile",
            }
        )
        completed = subprocess.run(
            ["bash", str(SCRIPT), "http://127.0.0.1:1", str(Path(raw) / "missing")],
            env=env,
            text=True,
            capture_output=True,
            timeout=10,
        )
        assert completed.returncode != 0
        assert "unknown CANARY_SERVING_PROFILE" in completed.stderr
        assert "curl:" not in completed.stderr


def invalid_deadline_refuses_before_transport() -> None:
    with tempfile.TemporaryDirectory(prefix="weinfer-profile-canary-time-red.") as raw:
        credentials = Path(raw) / "credentials.env"
        credentials.write_text("WEINFER_ADMIN_KEY=profile-admin-key\n")
        env = os.environ.copy()
        env.update(
            {
                "CANARY_RUN_ID": "regression-invalid-deadline",
                "CANARY_SERVING_PROFILE": H100.name,
                "CANARY_DEADLINE_SECONDS": "not-a-number",
            }
        )
        completed = subprocess.run(
            ["bash", str(SCRIPT), "http://127.0.0.1:1", str(credentials)],
            env=env,
            text=True,
            capture_output=True,
            timeout=10,
        )
        assert completed.returncode != 0
        assert "CANARY_DEADLINE_SECONDS must be a positive" in completed.stderr
        assert "curl:" not in completed.stderr


def h100_output_cap_hit_names_the_failure() -> None:
    state = State(
        H100,
        content="reasoning fragment without a final answer",
        completion_tokens=H100.max_tokens,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="weinfer-profile-cap-red.") as raw:
            root = Path(raw)
            credentials = root / "credentials.env"
            credentials.write_text("WEINFER_ADMIN_KEY=profile-admin-key\n")
            credentials.chmod(0o600)
            env = os.environ.copy()
            env.update(
                {
                    "HOME": str(root / "home"),
                    "CANARY_RUN_ID": "regression-h100-cap-hit",
                    "CANARY_SERVING_PROFILE": H100.name,
                    "CANARY_ARTIFACT_DIR": str(root / "artifacts"),
                    "CANARY_KEY_STORE": str(root / "keys"),
                    "CANARY_POLL_BUDGET_SECONDS": str(H100.deadline),
                }
            )
            completed = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    f"http://127.0.0.1:{server.server_port}",
                    str(credentials),
                ],
                env=env,
                text=True,
                capture_output=True,
                timeout=30,
            )
            assert completed.returncode != 0
            assert "OUTPUT CAP HIT" in completed.stderr, completed.stderr
            assert "NOT an image/AOT compatibility failure" in completed.stderr
            assert "task answer is not exactly" not in completed.stderr
            assert state.job_posts == 2, state.job_posts
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def main() -> int:
    assert QWEN.hold == 827, QWEN.hold
    assert H100.hold == 140_084, H100.hold
    run_profile(QWEN, explicit_profile=False)
    run_profile(H100, explicit_profile=True)
    unknown_profile_refuses_before_transport()
    invalid_deadline_refuses_before_transport()
    h100_output_cap_hit_names_the_failure()
    print(
        "CANARY PROFILE REGRESSION PASS: Qwen default and H100 explicit traverse "
        "priced discovery, exact reservation, replay, settlement, and usage on "
        "loopback; unknown profile and invalid deadline refuse before transport "
        "and an H100 completion at its output cap is classified separately "
        "from image/AOT compatibility (5 cases)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
