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
  HOME=/tmp/deploy-tail-home \
    bash "$DEPLOY" > /tmp/deploy-tail.log 2>&1
  DEPLOY_CODE=$?
  set -e
}
mode() { curl -fsS -X POST "$CTRL/mode" -d "$1" >/dev/null; }

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
  echo "ok: S-${CASE} refused, deleted, verified"
done

# --- S5: list failure during cleanup must NEVER report clean.
rm -f /tmp/fake-v1-deletes.log
mode '{"mode":"lost","list_delay":0}'
curl -fsS -X POST "$CTRL/outage" -d '{"count":20}' >/dev/null
run_deploy
[ "$DEPLOY_CODE" != "0" ] || { echo "S5 FAIL: exited 0"; exit 1; }
grep -q "CLEANUP FAILED" /tmp/deploy-tail.log || { echo "S5 FAIL: no loud cleanup failure"; cat /tmp/deploy-tail.log; exit 1; }
if grep -q "zero-live verification: 0" /tmp/deploy-tail.log; then
  echo "S5 FAIL: reported clean during an outage"; cat /tmp/deploy-tail.log; exit 1
fi
echo "ok: S5 outage during cleanup fails LOUDLY, never clean"

# --- S6: the full success path — create, rate within ceiling, health
# 200, LAUNCH_OK, credentials 0600, no cleanup delete.
rm -f /tmp/fake-v1-deletes.log /tmp/deploy-tail-home/.weinfer/controlplane-credentials-* 2>/dev/null || true
mkdir -p /tmp/deploy-tail-home
mode '{"mode":"ok","list_delay":0}'
run_deploy
[ "$DEPLOY_CODE" = "0" ] || { echo "S6 FAIL: success path exited $DEPLOY_CODE"; cat /tmp/deploy-tail.log; exit 1; }
grep -q "CONTROL PLANE LIVE" /tmp/deploy-tail.log || { echo "S6 FAIL: no live banner"; cat /tmp/deploy-tail.log; exit 1; }
[ ! -s /tmp/fake-v1-deletes.log ] || { echo "S6 FAIL: success path deleted a pod"; exit 1; }
CRED=$(ls /tmp/deploy-tail-home/.weinfer/controlplane-credentials-*.env | head -1)
PERM=$(stat -f '%Lp' "$CRED" 2>/dev/null || stat -c '%a' "$CRED")
[ "$PERM" = "600" ] || { echo "S6 FAIL: credentials mode $PERM"; exit 1; }
grep -q "WEINFER_ADMIN_KEY=" "$CRED" || { echo "S6 FAIL: credentials incomplete"; exit 1; }
echo "ok: S6 success path — live, credentials 0600, nothing deleted"

echo "DEPLOY TAIL REGRESSION PASS"
