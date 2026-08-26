#!/usr/bin/env python3
"""Zero-dollar contract for one-shot, wake-safe retention windows."""

from __future__ import annotations

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
        "late no-spend refusal, retry ladder, prior-failure and hash fences"
    )


if __name__ == "__main__":
    run()
