#!/usr/bin/env python3
"""Append one secret-free control-plane utilization sample from a pod listing.

The raw provider payload is read from stdin, hashed, and never written. Only an
explicit allowlist of resource/runtime fields reaches the append-only JSONL
artifact; provider environment, SSH data, ports, and registry fields cannot.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import stat
import sys
import time
from typing import Any


def classified_number(
    container: dict[str, Any], key: str, *, allow_zero: bool
) -> tuple[int | float | None, str]:
    if key not in container:
        return None, "absent"
    value = container[key]
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value < 0
        or (value == 0 and not allow_zero)
    ):
        return None, "invalid"
    return value, "observed"


def aggregate_status(statuses: list[str]) -> str:
    if "invalid" in statuses:
        return "invalid"
    if all(status == "absent" for status in statuses):
        return "absent"
    if all(status == "observed" for status in statuses):
        return "observed"
    return "partial"


def sanitized_sample(raw: bytes, control_pod_id: str) -> dict[str, Any]:
    data = json.loads(raw)
    pods = data.get("pods", data) if isinstance(data, dict) else data
    if not isinstance(pods, list):
        raise ValueError("provider listing has no pod array")
    matches = [pod for pod in pods if isinstance(pod, dict) and pod.get("id") == control_pod_id]
    if len(matches) != 1:
        raise ValueError(
            f"expected exactly one control-plane pod {control_pod_id!r}, found {len(matches)}"
        )
    pod = matches[0]
    runtime = pod.get("runtime") if isinstance(pod.get("runtime"), dict) else {}
    runtime_cpu = (
        runtime.get("cpu") if isinstance(runtime.get("cpu"), dict) else {}
    )
    runtime_memory = (
        runtime.get("memory") if isinstance(runtime.get("memory"), dict) else {}
    )
    cpu = pod.get("cpu") if isinstance(pod.get("cpu"), dict) else {}
    status_value = pod.get("status") or pod.get("desiredStatus")
    rate, rate_status = classified_number(
        pod, "cost" if "cost" in pod else "costPerHr", allow_zero=False
    )
    disk, disk_status = classified_number(pod, "disk", allow_zero=False)
    vcpu, vcpu_status = classified_number(cpu, "vcpuCount", allow_zero=False)
    memory_gb, memory_gb_status = classified_number(cpu, "memory", allow_zero=False)
    uptime, uptime_status = classified_number(runtime, "uptime", allow_zero=True)
    cpu_util, cpu_util_status = classified_number(runtime_cpu, "util", allow_zero=True)
    memory_util, memory_util_status = classified_number(
        runtime_memory, "util", allow_zero=True
    )
    return {
        "captured_at_epoch_micros": time.time_ns() // 1_000,
        "raw_listing_sha256": hashlib.sha256(raw).hexdigest(),
        "control_plane_pod_id": control_pod_id,
        "name": pod.get("name") if isinstance(pod.get("name"), str) else None,
        "status": status_value if isinstance(status_value, str) else None,
        "cloud": pod.get("cloud") if isinstance(pod.get("cloud"), str) else None,
        "data_center_id": (
            pod.get("dataCenterId")
            if isinstance(pod.get("dataCenterId"), str)
            else None
        ),
        "rate_usd_per_hour": rate,
        "container_disk_gb": disk,
        "cpu": {
            "id": cpu.get("id") if isinstance(cpu.get("id"), str) else None,
            "vcpu_count": vcpu,
            "memory_gb": memory_gb,
        },
        "runtime": {
            "uptime_seconds": uptime,
            "cpu_util": cpu_util,
            "memory_util": memory_util,
        },
        "field_status": {
            "rate_usd_per_hour": rate_status,
            "container_disk_gb": disk_status,
            "vcpu_count": vcpu_status,
            "memory_gb": memory_gb_status,
            "uptime_seconds": uptime_status,
            "cpu_util": cpu_util_status,
            "memory_util": memory_util_status,
        },
        "runtime_observation_status": aggregate_status(
            [uptime_status, cpu_util_status, memory_util_status]
        ),
        "authority": "provider_runtime_snapshot_observation_only",
        "raw_payload_persisted": False,
    }


def append_once(path: Path, sample: dict[str, Any]) -> None:
    if not path.is_absolute():
        raise ValueError("telemetry output path must be absolute")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("telemetry output is not a regular file")
        os.fchmod(descriptor, 0o600)
        payload = (json.dumps(sample, sort_keys=True, separators=(",", ":")) + "\n").encode()
        written = os.write(descriptor, payload)
        if written != len(payload):
            raise OSError(f"short telemetry append: wrote {written} of {len(payload)} bytes")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit(
            "usage: controlplane_telemetry.py <control-plane-pod-id> <absolute-output-jsonl>"
        )
    control_pod_id, output_text = sys.argv[1:]
    if not control_pod_id:
        raise ValueError("control-plane pod id must be nonempty")
    raw = sys.stdin.buffer.read()
    if not raw:
        raise ValueError("provider listing is empty")
    append_once(Path(output_text), sanitized_sample(raw, control_pod_id))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"CONTROL-PLANE TELEMETRY REFUSED: {error}", file=sys.stderr)
        raise SystemExit(1)
