#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

PREFLIGHT="${PREFLIGHT:-scripts/retention_pair_substrate_preflight.sh}"
FROZEN_DEPLOY="${FROZEN_DEPLOY:-scripts/retention_pair_deploy_v0_9.sh}"
LIVE_DEPLOY="${LIVE_DEPLOY:-scripts/deploy_controlplane.sh}"
test_tmp=$(mktemp -d "${TMPDIR:-/tmp}/weinfer-retention-substrate.XXXXXX")
trap 'rm -rf "$test_tmp"' EXIT

bash "$PREFLIGHT" "$FROZEN_DEPLOY" > "$test_tmp/frozen.out" 2> "$test_tmp/frozen.err"
grep -q 'RETENTION SUBSTRATE PASS' "$test_tmp/frozen.out"

ADMIN_KEY="retention-regression-admin" \
  CUSTOMER_KEY="retention-regression-customer" \
  WORKER_KEY="retention-regression-worker" \
  WEINFER_DEMAND_QUIET_CYCLES=3 \
  bash "$LIVE_DEPLOY" --render-env > "$test_tmp/live.json" 2> "$test_tmp/live.err"
python3 - "$test_tmp/live.json" <<'PY'
import json
import sys

value = json.load(open(sys.argv[1]))
assert value["WEINFER_GATEWAY_SHA256"] == "cc97e659266a3d2be00ce579a0effea7cc53bbc80c95612b897497fc6399fbcd"
assert value["WEINFER_BOOTSTRAP_MODE"] == "1"
hardware = json.loads(value["WEINFER_BOOTSTRAP_HARDWARE"])
assert len(hardware) == 11
assert hardware[0]["gpu_sku"] == "NVIDIA RTX A5000"
assert hardware[2]["gpu_sku"] == "NVIDIA RTX A4500"
PY

if bash "$PREFLIGHT" "$LIVE_DEPLOY" > "$test_tmp/live-preflight.out" 2> "$test_tmp/live-preflight.err"; then
  echo "live multi-SKU deploy script passed the frozen retention preflight" >&2
  exit 1
fi
grep -q 'retention substrate refused: deploy sha' "$test_tmp/live-preflight.err"

cp "$FROZEN_DEPLOY" "$test_tmp/tampered.sh"
python3 - "$test_tmp/tampered.sh" <<'PY'
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
text = path.read_text()
text = text.replace('GW_TAG="gateway-v0.9.0"', 'GW_TAG="gateway-v0.19.0"', 1)
path.write_text(text)
PY
if bash "$PREFLIGHT" "$test_tmp/tampered.sh" > "$test_tmp/tampered.out" 2> "$test_tmp/tampered.err"; then
  echo "tampered frozen deploy script passed the retention preflight" >&2
  exit 1
fi
grep -q 'retention substrate refused: deploy sha' "$test_tmp/tampered.err"

echo "RETENTION SUBSTRATE REGRESSION PASS: frozen v0.9/A4500 accepted; live current/exact-minor multi-identity and tamper refused"
