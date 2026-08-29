#!/usr/bin/env python3
"""Zero-dollar contract for one-shot, wake-safe retention windows."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile


WINDOW = "scripts/retention_pair_window.sh"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def invoke(
    campaign: Path,
    operator: Path,
    *,
    index: int,
    target: int,
    suffix: str,
    now: int,
    expected_sha: str | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "WEINFER_RETENTION_CAMPAIGN_ROOT": str(campaign),
            "WEINFER_RETENTION_OPERATOR": str(operator),
            "WEINFER_RETENTION_OPERATOR_SHA256": expected_sha or sha(operator),
            "WEINFER_RETENTION_WINDOW_NOW": str(now),
            "WEINFER_RETENTION_MAX_LATENESS_SECONDS": "300",
        }
    )
    return subprocess.run(
        ["bash", WINDOW, str(index), str(target), suffix],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )


def make_operator(path: Path, exit_code: int) -> None:
    path.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "printf '%s|%s\\n' \"$1\" \"$2\" >> \"${WEINFER_FAKE_CALLS:?}\"\n"
        "mkdir -p \"$2\"\n"
        f"exit {exit_code}\n"
    )
    path.chmod(0o700)


def make_deploy_failure_operator(path: Path, deploy_log: str) -> None:
    encoded = base64.b64encode(deploy_log.encode()).decode()
    path.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "printf '%s|%s\\n' \"$1\" \"$2\" >> \"${WEINFER_FAKE_CALLS:?}\"\n"
        "mkdir -p \"$2/arm-a\"\n"
        "python3 -c 'import base64,sys; "
        "open(sys.argv[1], \"wb\").write(base64.b64decode(sys.argv[2]))' "
        f"\"$2/arm-a/deploy.log\" {encoded}\n"
        "exit 1\n"
    )
    path.chmod(0o700)


def run() -> None:
    with tempfile.TemporaryDirectory(prefix="weinfer-retention-window-") as temp:
        root = Path(temp)
        calls = root / "calls"
        os.environ["WEINFER_FAKE_CALLS"] = str(calls)
        operator = root / "operator.sh"
        make_operator(operator, 42)
        campaign = root / "campaign"

        first = invoke(
            campaign,
            operator,
            index=2,
            target=1_000,
            suffix="infra1",
            now=1_000,
        )
        assert first.returncode == 42, first.stderr
        result = json.loads((campaign / "window-2-infra1.result.json").read_text())
        assert result["status"] == "definitive_capacity_refusal"
        assert result["pair_id"] == "retpair-1000-a2-infra1"
        assert len(calls.read_text().splitlines()) == 1

        replay = invoke(
            campaign,
            operator,
            index=2,
            target=1_000,
            suffix="infra1",
            now=1_001,
        )
        assert replay.returncode == 1
        assert "durable lock already exists" in replay.stderr
        assert len(calls.read_text().splitlines()) == 1

        third = invoke(
            campaign,
            operator,
            index=3,
            target=2_000,
            suffix="infra1",
            now=2_003,
        )
        assert third.returncode == 42, third.stderr
        assert len(calls.read_text().splitlines()) == 2

        late_campaign = root / "late"
        late = invoke(
            late_campaign,
            operator,
            index=2,
            target=3_000,
            suffix="infra2",
            now=3_301,
        )
        assert late.returncode == 43
        late_result = json.loads(
            (late_campaign / "window-2-infra2.result.json").read_text()
        )
        assert late_result["status"] == "missed_without_spend"
        assert late_result["lateness_seconds"] == 301
        assert len(calls.read_text().splitlines()) == 2

        after_miss = invoke(
            late_campaign,
            operator,
            index=3,
            target=4_000,
            suffix="infra2",
            now=4_000,
        )
        assert after_miss.returncode == 42
        assert len(calls.read_text().splitlines()) == 3

        bad_campaign = root / "bad"
        bad_operator = root / "bad-operator.sh"
        make_operator(bad_operator, 1)
        bad = invoke(
            bad_campaign,
            bad_operator,
            index=2,
            target=5_000,
            suffix="infra3",
            now=5_000,
        )
        assert bad.returncode == 1
        blocked = invoke(
            bad_campaign,
            operator,
            index=3,
            target=6_000,
            suffix="infra3",
            now=6_000,
        )
        assert blocked.returncode == 1
        assert "prior exit 1" in blocked.stderr

        # Incident 0015: a definitive create-time capacity refusal happened
        # before the operator's post-batch classifier, so raw exit 1 was
        # incorrectly filed as unexpected. The window now normalizes ONLY an
        # ordered, clean no-spend transcript and persists the raw exit proof.
        clean_capacity_log = (
            "RunPod POST /pods HTTP 500: create pod: There are no longer any "
            "instances available with the requested specifications.\n"
            "LAUNCH FAILED — cleaning up (volume kept as the durable asset)\n"
            "zero-live verification: 0 live pods remain\n"
        )
        capacity_operator = root / "deploy-capacity-operator.sh"
        make_deploy_failure_operator(capacity_operator, clean_capacity_log)
        capacity_campaign = root / "deploy-capacity"
        capacity = invoke(
            capacity_campaign,
            capacity_operator,
            index=2,
            target=6_100,
            suffix="deploycap",
            now=6_100,
        )
        capacity_attempt = (
            capacity_campaign / "attempt-2-retpair-6100-a2-deploycap"
        )
        assert capacity.returncode == 42, (
            capacity.stdout,
            capacity.stderr,
            (capacity_attempt / "arm-a/deploy.log").read_text(),
        )
        capacity_result = json.loads(
            (capacity_campaign / "window-2-deploycap.result.json").read_text()
        )
        assert capacity_result["status"] == "definitive_capacity_refusal"
        assert "raw operator exit 1" in capacity_result["detail"]
        capacity_root = Path(capacity_result["attempt_root"])
        proof = json.loads(
            (capacity_root / "deploy-capacity-classification.json").read_text()
        )
        assert proof["raw_operator_exit_code"] == 1
        assert proof["normalized_exit_code"] == 42
        assert proof["classification"] == "clean_deploy_time_capacity_refusal"
        assert proof["create_refusal_observed"] is True
        assert proof["zero_live_verified"] is True
        assert not (capacity_campaign / "diagnosis-root.txt").exists()

        # The refusal string alone is not enough: absent zero-live proof,
        # unresolved cleanup, or any created pod keeps raw exit 1 terminal.
        unsafe_logs = {
            "no-zero": clean_capacity_log.replace(
                "zero-live verification: 0 live pods remain\n", ""
            ),
            "unresolved": clean_capacity_log
            + "CLEANUP UNRESOLVED: pod may be LIVE and billing\n",
            "created": clean_capacity_log.replace(
                "LAUNCH FAILED",
                "pod fakepod created at epoch 6100\nLAUNCH FAILED",
            ),
        }
        for offset, (suffix, unsafe_log) in enumerate(unsafe_logs.items(), start=1):
            unsafe_operator = root / f"deploy-{suffix}-operator.sh"
            make_deploy_failure_operator(unsafe_operator, unsafe_log)
            unsafe_campaign = root / f"deploy-{suffix}"
            unsafe = invoke(
                unsafe_campaign,
                unsafe_operator,
                index=2,
                target=6_100 + offset,
                suffix=suffix,
                now=6_100 + offset,
            )
            assert unsafe.returncode == 1, (suffix, unsafe.stderr)
            unsafe_result = json.loads(
                (unsafe_campaign / f"window-2-{suffix}.result.json").read_text()
            )
            assert unsafe_result["status"] == "unexpected_failure", suffix
            unsafe_root = Path(unsafe_result["attempt_root"])
            assert not (unsafe_root / "deploy-capacity-classification.json").exists()
            assert (unsafe_campaign / "diagnosis-root.txt").exists()

        hash_refusal = invoke(
            root / "hash-refusal",
            operator,
            index=2,
            target=7_000,
            suffix="infra4",
            now=7_000,
            expected_sha="0" * 64,
        )
        assert hash_refusal.returncode == 1
        assert "operator sha" in hash_refusal.stderr

        early = invoke(
            root / "early",
            operator,
            index=2,
            target=8_000,
            suffix="infra5",
            now=7_999,
        )
        assert early.returncode == 1
        assert "before target" in early.stderr

    print(
        "RETENTION WINDOW REGRESSION PASS: fresh one-shot IDs, durable replay lock, "
        "late no-spend refusal, clean deploy-capacity normalization, unsafe-cleanup "
        "fail-closed, retry ladder, prior-failure and hash fences"
    )


if __name__ == "__main__":
    run()
