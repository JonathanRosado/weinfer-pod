#!/usr/bin/env python3
"""Zero-provider regression for secret-free control-plane telemetry."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile


SOURCE = Path(__file__).resolve().parent / "controlplane_telemetry.py"


def invoke(payload: bytes, pod_id: str, output: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, str(SOURCE), pod_id, str(output)],
        input=payload,
        capture_output=True,
        check=False,
    )


def main() -> None:
    secret = "provider-secret-must-never-persist"
    pod = {
        "id": "cp-1",
        "name": "weinfer-controlplane-regression",
        "status": "RUNNING",
        "cloud": "SECURE",
        "dataCenterId": "EU-RO-1",
        "cost": 0.06,
        "disk": 10,
        "cpu": {"id": "cpu3c", "memory": 4, "vcpuCount": 2},
        "runtime": {
            "uptime": 123,
            "cpu": {"util": 37.5},
            "memory": {"util": 42},
            "ports": [{"public": 12345}],
        },
        "env": {"WEINFER_RUNPOD_API_KEY": secret},
        "ssh": {"proxy": {"command": secret}},
    }
    raw = (json.dumps({"pods": [pod]}, sort_keys=True) + "\n").encode()
    with tempfile.TemporaryDirectory(prefix="cp-telemetry-regression-") as temporary:
        output = Path(temporary).resolve() / "runtime" / "samples.jsonl"
        first = invoke(raw, "cp-1", output)
        assert first.returncode == 0, first.stderr.decode()
        second = invoke(raw, "cp-1", output)
        assert second.returncode == 0, second.stderr.decode()
        rows = [json.loads(line) for line in output.read_text().splitlines()]
        assert len(rows) == 2
        assert rows[0]["raw_listing_sha256"] == hashlib.sha256(raw).hexdigest()
        assert rows[0]["runtime"] == {
            "cpu_util": 37.5,
            "memory_util": 42,
            "uptime_seconds": 123,
        }
        assert rows[0]["runtime_observation_status"] == "observed"
        assert set(rows[0]["field_status"].values()) == {"observed"}
        assert rows[0]["cpu"] == {
            "id": "cpu3c",
            "memory_gb": 4,
            "vcpu_count": 2,
        }
        assert secret not in output.read_text()
        assert output.stat().st_mode & 0o777 == 0o600

        invalid = json.loads(raw)
        invalid["pods"][0]["runtime"]["cpu"]["util"] = "garbage"
        invalid_raw = json.dumps(invalid).encode()
        invalid_result = invoke(invalid_raw, "cp-1", output)
        assert invalid_result.returncode == 0, invalid_result.stderr.decode()
        invalid_row = json.loads(output.read_text().splitlines()[-1])
        assert invalid_row["runtime"]["cpu_util"] is None
        assert invalid_row["field_status"]["cpu_util"] == "invalid"
        assert invalid_row["runtime_observation_status"] == "invalid"

        before = output.read_bytes()
        for bad_payload, label in (
            (json.dumps({"pods": []}).encode(), "missing"),
            (json.dumps({"pods": [pod, pod]}).encode(), "duplicate"),
            (b"not-json", "malformed"),
        ):
            refused = invoke(bad_payload, "cp-1", output)
            assert refused.returncode != 0, label
            assert output.read_bytes() == before, label

    print(
        "CONTROL-PLANE TELEMETRY REGRESSION PASS: exact pod; append-only 0600; "
        "runtime/resource allowlist; raw hash retained; secret fields absent; "
        "missing, duplicate, and malformed listings refuse without partial writes"
    )


if __name__ == "__main__":
    main()
