#!/usr/bin/env bash
# Fail closed before a paid retention-pair arm unless the deploy script is the
# exact frozen v0.9.0/A4500 substrate registered for both arms.
set -euo pipefail

DEPLOY_SCRIPT="${1:?frozen deploy script path required}"
EXPECTED_DEPLOY_SHA256="0ae94a6398c653f655435fd3cec5981466eaa999a0830bcd609afb53c8ec6cfe"

[ -f "$DEPLOY_SCRIPT" ] || {
  echo "retention substrate refused: deploy script is missing: $DEPLOY_SCRIPT" >&2
  exit 1
}

actual_sha=$(shasum -a 256 "$DEPLOY_SCRIPT" | awk '{print $1}')
[ "$actual_sha" = "$EXPECTED_DEPLOY_SHA256" ] || {
  echo "retention substrate refused: deploy sha ${actual_sha} != frozen v0.9.0 ${EXPECTED_DEPLOY_SHA256}" >&2
  exit 1
}

render_arm() {
  local quiet_cycles="$1"
  ADMIN_KEY="retention-preflight-admin" \
    CUSTOMER_KEY="retention-preflight-customer" \
    WORKER_KEY="retention-preflight-worker" \
    WEINFER_DEMAND_QUIET_CYCLES="$quiet_cycles" \
    bash "$DEPLOY_SCRIPT" --render-env
}

arm_a=$(render_arm 1000000)
arm_b=$(render_arm 3)

python3 - "$arm_a" "$arm_b" <<'PY'
import json
import sys

arm_a = json.loads(sys.argv[1])
arm_b = json.loads(sys.argv[2])

expected = {
    "WEINFER_GATEWAY_URL": "https://github.com/JonathanRosado/weinfer-pod/releases/download/gateway-v0.9.0/weinfer-gateway",
    "WEINFER_GATEWAY_SHA256": "e94bd6e6a87c5c802f0c8339db78c195923847e43321488cb531d5918b6f041e",
    "WEINFER_WORKER_URL": "https://github.com/JonathanRosado/weinfer-pod/releases/download/worker-v0.4.0/weinfer-worker",
    "WEINFER_WORKER_SHA256": "7bd6f06f07f68afb24bbd8fec086bf3be04d574ebe5a86791e9f2c230cca5f6b",
    "WEINFER_GPU_TYPE": "NVIDIA RTX A4500",
    "WEINFER_GPU_VRAM_GB": "20",
    "WEINFER_CLOUD": "COMMUNITY",
    "WEINFER_CUDA_VERSIONS": "12.8",
    "WEINFER_BACKEND_MAX_CONTEXT": "8192",
    "WEINFER_SERVED_MODEL": "Qwen/Qwen2.5-7B-Instruct",
    "WEINFER_MODEL_REVISION": "a09a35458c702b33eeacc393d103063234e8bc28",
    "WEINFER_TOKENIZER_REVISION": "a09a35458c702b33eeacc393d103063234e8bc28",
    "WEINFER_IMAGE": "ghcr.io/jonathanrosado/weinfer-pod@sha256:160a926826565b1ed0134335f3f68e65ed457fcb034058639fc5c9b5c7ec2613",
    "VLLM_EXTRA_ARGS": "--seed 0 --max-num-batched-tokens 16384 --max-num-seqs 256 --gpu-memory-utilization 0.92 --enable-chunked-prefill",
    "WEINFER_CONCURRENCY": "64",
    "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True",
    "WEINFER_MAX_GPU_RATE": "0.40",
    "WEINFER_POLL_MARGIN_SECS": "30",
}
for arm_name, value in (("A", arm_a), ("B", arm_b)):
    for key, wanted in expected.items():
        got = value.get(key)
        if got != wanted:
            raise SystemExit(
                f"retention substrate refused: Arm {arm_name} {key}={got!r}, expected {wanted!r}"
            )

    forbidden = {
        "WEINFER_BOOTSTRAP_MODE",
        "WEINFER_BOOTSTRAP_HARDWARE",
        "WEINFER_PLACEMENT_PROFILES",
    }
    present = sorted(forbidden.intersection(value))
    if present:
        raise SystemExit(
            "retention substrate refused: bootstrap/placement ranking must be OFF; "
            + ", ".join(present)
            + " present"
        )

if arm_a.get("WEINFER_DEMAND_QUIET_CYCLES") != "1000000":
    raise SystemExit("retention substrate refused: Arm A quiet-cycle control drifted")
if arm_b.get("WEINFER_DEMAND_QUIET_CYCLES") != "3":
    raise SystemExit("retention substrate refused: Arm B quiet-cycle treatment drifted")

normalized_a = dict(arm_a)
normalized_b = dict(arm_b)
normalized_a.pop("WEINFER_DEMAND_QUIET_CYCLES", None)
normalized_b.pop("WEINFER_DEMAND_QUIET_CYCLES", None)
if normalized_a != normalized_b:
    differing = sorted(
        key
        for key in set(normalized_a).union(normalized_b)
        if normalized_a.get(key) != normalized_b.get(key)
    )
    raise SystemExit(
        "retention substrate refused: arms differ beyond retention policy: "
        + ", ".join(differing)
    )

print(
    "RETENTION SUBSTRATE PASS: gateway-v0.9.0, one A4500/CUDA-12.8 identity, "
    "bootstrap OFF, retention knob only"
)
PY
