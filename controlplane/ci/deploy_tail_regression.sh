#!/usr/bin/env bash
# Executable regression for the DESTRUCTIVE deploy tail (codex 0165):
# the exact create/rate/trap/verify code that will spend real money
# runs against a v1-shaped fake — lost create with delayed
# visibility, missing/malformed/over-ceiling rates, list failure
# during cleanup, EXITED-first delete verification, and the full
# success path.  Zero spend, zero real provider calls.
set -euo pipefail
cd "$(dirname "$0")/.."

FAKE="${FAKE:-infra/controlplane/ci/fake_runpod_v1.py}"
DEPLOY="${DEPLOY:-scripts/deploy_controlplane.sh}"
PORT=18992
CTRL="http://127.0.0.1:${PORT}/control"

python3 "$FAKE" "$PORT" & FAKE_PID=$!
trap 'kill $FAKE_PID 2>/dev/null' EXIT
sleep 1

run_deploy() { # -> exit code in $DEPLOY_CODE, log in /tmp/deploy-tail.log
  set +e
  ADMIN_KEY=t CUSTOMER_KEY=t WORKER_KEY=t \
  WEINFER_DEPLOY_TEST=1 \
  WEINFER_DEPLOY_API_BASE="http://127.0.0.1:${PORT}" \
  WEINFER_DEPLOY_HEALTH_BASE="http://127.0.0.1:${PORT}" \
  WEINFER_HEALTH_DEADLINE_SECS=10 \
  WEINFER_CONTROLPLANE_STORAGE_MODE="${STORAGE_MODE:-network-volume}" \
  HOME=/tmp/deploy-tail-home \
    bash "$DEPLOY" > /tmp/deploy-tail.log 2>&1
  DEPLOY_CODE=$?
  set -e
}
mode() { curl -fsS -X POST "$CTRL/mode" -d "$1" >/dev/null; }
assert_all_terminated() { # every fake pod must be TERMINATED
  LIVE=$(curl -fsS "http://127.0.0.1:${PORT}/pods" | python3 -c "
import json,sys
pods=json.load(sys.stdin)
print(sum(1 for p in pods if p.get('status') != 'TERMINATED'))")
  [ "$LIVE" = "0" ] || { echo "$1: ${LIVE} NON-TERMINATED pods remain — a green scenario may not leave a billable resource"; exit 1; }
}

# --- S1: create COMMITTED, response LOST, pod visible only after a
# delay — the trap must discover by exact name, delete to TERMINATED,
# and verify zero-live.
rm -f /tmp/fake-v1-deletes.log
mode '{"mode":"lost","list_delay":2}'
run_deploy
[ "$DEPLOY_CODE" != "0" ] || { echo "S1 FAIL: lost create exited 0"; cat /tmp/deploy-tail.log; exit 1; }
grep -q "deleted and verified gone" /tmp/deploy-tail.log || { echo "S1 FAIL: no verified delete"; cat /tmp/deploy-tail.log; exit 1; }
grep -q "zero-live verification: 0" /tmp/deploy-tail.log || { echo "S1 FAIL: no zero-live"; cat /tmp/deploy-tail.log; exit 1; }
D1=$(grep -c '^cpupod1$' /tmp/fake-v1-deletes.log)
[ "$D1" -ge 2 ] || { echo "S1 FAIL: EXITED-first delete needs a second sweep (got $D1)"; exit 1; }
STATUS=$(curl -fsS "http://127.0.0.1:${PORT}/pods/cpupod1" | python3 -c "import json,sys;print(json.load(sys.stdin)['status'])")
[ "$STATUS" = "TERMINATED" ] || { echo "S1 FAIL: pod left $STATUS"; exit 1; }
assert_all_terminated "S1"
echo "ok: S1 lost create -> delayed discovery -> TERMINATED + zero-live"

# --- S2-S4: missing / malformed / over-ceiling rates all refuse,
# delete their pod to TERMINATED, and exit nonzero.
for CASE in norate badrate highrate; do
  rm -f /tmp/fake-v1-deletes.log
  mode "{\"mode\":\"${CASE}\",\"list_delay\":0}"
  run_deploy
  [ "$DEPLOY_CODE" != "0" ] || { echo "$CASE FAIL: exited 0"; cat /tmp/deploy-tail.log; exit 1; }
  grep -q "refusing" /tmp/deploy-tail.log || { echo "$CASE FAIL: no refusal"; cat /tmp/deploy-tail.log; exit 1; }
  grep -q "deleted and verified gone" /tmp/deploy-tail.log || { echo "$CASE FAIL: no verified delete"; cat /tmp/deploy-tail.log; exit 1; }
  assert_all_terminated "S-${CASE}"
  echo "ok: S-${CASE} refused, deleted, verified"
done

# --- S5: a FINITE list outage during cleanup must be RETRIED and
# recovered — discovery resumes, the pod is TERMINATED, zero-live
# verified.  Loud failure alone would ratify a running billable pod
# (codex 0166).
rm -f /tmp/fake-v1-deletes.log
mode '{"mode":"lost","list_delay":0}'
curl -fsS -X POST "$CTRL/outage" -d '{"count":4}' >/dev/null
run_deploy
[ "$DEPLOY_CODE" != "0" ] || { echo "S5 FAIL: exited 0"; exit 1; }
grep -q "provider list FAILED" /tmp/deploy-tail.log || { echo "S5 FAIL: outage not surfaced"; cat /tmp/deploy-tail.log; exit 1; }
grep -q "deleted and verified gone" /tmp/deploy-tail.log || { echo "S5 FAIL: cleanup did not recover after the outage"; cat /tmp/deploy-tail.log; exit 1; }
grep -q "zero-live verification: 0" /tmp/deploy-tail.log || { echo "S5 FAIL: no zero-live after recovery"; cat /tmp/deploy-tail.log; exit 1; }
assert_all_terminated "S5"
echo "ok: S5 finite outage -> retried -> recovered -> TERMINATED + zero-live"

# --- S5b: an UNRECOVERED outage persists an independently executable
# cleanup state and never claims clean; the resume script finishes
# the job once the provider returns.
rm -f /tmp/fake-v1-deletes.log
rm -rf /tmp/deploy-tail-home/.weinfer/unresolved-launch-*.json 2>/dev/null || true
mode '{"mode":"lost","list_delay":0}'
curl -fsS -X POST "$CTRL/outage" -d '{"count":500}' >/dev/null
run_deploy
[ "$DEPLOY_CODE" != "0" ] || { echo "S5b FAIL: exited 0"; exit 1; }
grep -q "CLEANUP UNRESOLVED" /tmp/deploy-tail.log || { echo "S5b FAIL: no unresolved marker"; cat /tmp/deploy-tail.log; exit 1; }
if grep -q "zero-live verification: 0" /tmp/deploy-tail.log; then
  echo "S5b FAIL: claimed clean while unreadable"; exit 1
fi
STATE=$(ls /tmp/deploy-tail-home/.weinfer/unresolved-launch-*.json | head -1)
[ -s "$STATE" ] || { echo "S5b FAIL: no persisted cleanup state"; exit 1; }
# Provider recovers; the resume script must finish the cleanup.
curl -fsS -X POST "$CTRL/outage" -d '{"count":0}' >/dev/null
WEINFER_DEPLOY_TEST=1 bash "${RESUME:-scripts/deploy_cleanup_resume.sh}" "$STATE" > /tmp/resume.log 2>&1   || { echo "S5b FAIL: resume failed"; cat /tmp/resume.log; exit 1; }
grep -q "RESOLVED" /tmp/resume.log || { echo "S5b FAIL: no resolution"; cat /tmp/resume.log; exit 1; }
[ ! -f "$STATE" ] || { echo "S5b FAIL: state not cleared"; exit 1; }
assert_all_terminated "S5b"
echo "ok: S5b unrecovered outage -> persisted state -> resume TERMINATED + cleared"

# --- S6: the full success path — create, rate within ceiling, health
# 200, LAUNCH_OK, credentials 0600, no cleanup delete.
rm -f /tmp/fake-v1-deletes.log /tmp/fake-v1-bodies.jsonl /tmp/deploy-tail-home/.weinfer/controlplane-credentials-* 2>/dev/null || true
mkdir -p /tmp/deploy-tail-home
mode '{"mode":"ok","list_delay":0}'
run_deploy
[ "$DEPLOY_CODE" = "0" ] || { echo "S6 FAIL: success path exited $DEPLOY_CODE"; cat /tmp/deploy-tail.log; exit 1; }
grep -q "CONTROL PLANE LIVE" /tmp/deploy-tail.log || { echo "S6 FAIL: no live banner"; cat /tmp/deploy-tail.log; exit 1; }
[ ! -s /tmp/fake-v1-deletes.log ] || { echo "S6 FAIL: success path deleted a pod"; exit 1; }
CRED=$(ls /tmp/deploy-tail-home/.weinfer/controlplane-credentials-*.env | head -1)
PERM=$(stat -c '%a' "$CRED" 2>/dev/null || stat -f '%Lp' "$CRED")
[ "$PERM" = "600" ] || { echo "S6 FAIL: credentials mode $PERM"; exit 1; }
grep -q "WEINFER_ADMIN_KEY=" "$CRED" || { echo "S6 FAIL: credentials incomplete"; exit 1; }
python3 - /tmp/fake-v1-bodies.jsonl <<'PY'
import json, sys
body = json.loads(open(sys.argv[1]).readlines()[-1])
assert body["cpuFlavorIds"] == [
    "cpu3c", "cpu5c", "cpu3g", "cpu5g", "cpu3m", "cpu5m"
], body
assert body["cpuFlavorPriority"] == "availability", body
assert body["vcpuCount"] == 2, body
assert body["env"]["WEINFER_CONTROLPLANE_STORAGE_MODE"] == "network-volume", body
assert isinstance(body.get("networkVolumeId"), str) and body["networkVolumeId"], body
assert "volumeInGb" not in body, body
PY
echo "ok: S6 success path — live, credentials 0600, nothing deleted"
# (S6's pod is legitimately RUNNING; terminate it so the suite's final
# state is clean, then confirm.)
S6_POD=$(curl -fsS "http://127.0.0.1:${PORT}/pods" | python3 -c "
import json,sys
pods=[p for p in json.load(sys.stdin) if p.get('status') != 'TERMINATED']
print(pods[0]['id'] if pods else '')")
if [ -n "$S6_POD" ]; then
  curl -fsS -X DELETE "http://127.0.0.1:${PORT}/pods/${S6_POD}" >/dev/null
  curl -fsS -X DELETE "http://127.0.0.1:${PORT}/pods/${S6_POD}" >/dev/null
fi
assert_all_terminated "S6-final"

# --- S7: the registered N=24 storage mode uses a run-scoped Pod volume,
# exposes the mode in the rendered authority, and cannot accidentally retain a
# network-volume binding.
rm -f /tmp/fake-v1-deletes.log /tmp/fake-v1-bodies.jsonl
mode '{"mode":"ok","list_delay":0}'
STORAGE_MODE=run-scoped-pod run_deploy
[ "$DEPLOY_CODE" = "0" ] || { echo "S7 FAIL: run-scoped path exited $DEPLOY_CODE"; cat /tmp/deploy-tail.log; exit 1; }
python3 - /tmp/fake-v1-bodies.jsonl <<'PY'
import json, sys
body = json.loads(open(sys.argv[1]).readlines()[-1])
assert body["env"]["WEINFER_CONTROLPLANE_STORAGE_MODE"] == "run-scoped-pod", body
assert body["volumeInGb"] == 10, body
assert body["volumeMountPath"] == "/workspace", body
assert "networkVolumeId" not in body, body
PY
grep -q "run-scoped Pod volume (10GB, deleted with Pod)" /tmp/deploy-tail.log || {
  echo "S7 FAIL: run-scoped storage was not reported truthfully"; cat /tmp/deploy-tail.log; exit 1;
}
S7_POD=$(curl -fsS "http://127.0.0.1:${PORT}/pods" | python3 -c "
import json,sys
pods=[p for p in json.load(sys.stdin) if p.get('status') != 'TERMINATED']
print(pods[0]['id'] if pods else '')")
if [ -n "$S7_POD" ]; then
  curl -fsS -X DELETE "http://127.0.0.1:${PORT}/pods/${S7_POD}" >/dev/null
  curl -fsS -X DELETE "http://127.0.0.1:${PORT}/pods/${S7_POD}" >/dev/null
fi
assert_all_terminated "S7-final"
echo "ok: S7 run-scoped Pod volume — exact body, truthful output, zero-live"

echo "DEPLOY TAIL REGRESSION PASS"
