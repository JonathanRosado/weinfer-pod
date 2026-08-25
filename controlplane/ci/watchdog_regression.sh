#!/usr/bin/env bash
# GPU-watchdog regression (codex 0164) against the OFFICIAL v2 shape
# (status/cost): churn-proof cumulative accounting, fail-closed
# malformed rates, outage resilience, control-plane-first kill, a
# post-breach resurrection swept, and triple zero-live confirmation.
# Zero spend, zero real provider calls.
set -euo pipefail
cd "$(dirname "$0")/.."

FAKE="${FAKE:-infra/controlplane/ci/fake_runpod_official.py}"
WATCHDOG="${WATCHDOG:-scripts/gpu_watchdog.sh}"
PORT=18991
CTRL="http://127.0.0.1:${PORT}/control"
PREFIX="weinfer-community-qwen7b-0-"

python3 "$FAKE" "$PORT" & FAKE_PID=$!
trap 'kill $FAKE_PID 2>/dev/null' EXIT
sleep 1
rm -f /tmp/fake-official-deletes.log /tmp/wd-state.json /tmp/wd.log
printf 'fake-key' > /tmp/wd-key

spawn() { curl -fsS -X POST "$CTRL/spawn" -d "$1" >/dev/null; }
kill_pod() { curl -fsS -X POST "$CTRL/kill" -d "{\"id\":\"$1\"}" >/dev/null; }

# --- Scenario A: churn-proof cumulative cap + CP-first + resurrection ---
# Control pod (non-matching name) + one worker already 1000s old at
# $0.19/hr (~$0.0528 accrued).  Ceiling $0.10.
spawn '{"id":"cp1","name":"weinfer-controlplane-x","cost":0.05,"age_secs":2000}'
spawn '{"id":"w1","name":"'"$PREFIX"'aaa","cost":0.19,"age_secs":1000}'
EPOCH=$(( $(date +%s) - 3600 ))
WEINFER_RUNPOD_API="http://127.0.0.1:${PORT}/v2" WEINFER_WATCHDOG_INTERVAL=2 \
  bash "$WATCHDOG" /tmp/wd-key "$EPOCH" 0.10 "$PREFIX" cp1 /tmp/wd-state.json \
  > /tmp/wd.log 2>&1 & WD_PID=$!
sleep 5
# CHURN: w1 replaced by w2 (800s old, ~$0.0422).  w1's spend must be
# RETAINED, so cumulative ($0.0528 + $0.0422 + drift) crosses $0.10.
kill_pod w1
spawn '{"id":"w2","name":"'"$PREFIX"'bbb","cost":0.19,"age_secs":800}'
# Wait for the control-plane kill (breach start), then attempt a
# RESURRECTION the watchdog must sweep.
for i in $(seq 1 45); do
  if grep -q '^cp1$' /tmp/fake-official-deletes.log 2>/dev/null; then break; fi
  if [ "$i" = 45 ]; then echo "FAIL: breach never killed the control plane"; cat /tmp/wd.log; exit 1; fi
  sleep 2
done
spawn '{"id":"w3","name":"'"$PREFIX"'resurrected","cost":0.19,"age_secs":0}'
for i in $(seq 1 60); do
  kill -0 "$WD_PID" 2>/dev/null || break
  if [ "$i" = 60 ]; then echo "FAIL: watchdog never exited after breach"; cat /tmp/wd.log; exit 1; fi
  sleep 2
done
wait "$WD_PID" && CODE=0 || CODE=$?
[ "$CODE" = "2" ] || { echo "FAIL: expected exit 2, got $CODE"; cat /tmp/wd.log; exit 1; }
# Order: control plane FIRST; both workers (incl. the resurrection) killed.
FIRST=$(head -1 /tmp/fake-official-deletes.log)
[ "$FIRST" = "cp1" ] || { echo "FAIL: first delete was $FIRST, not the control plane"; exit 1; }
grep -q '^w2$' /tmp/fake-official-deletes.log || { echo "FAIL: w2 not deleted"; exit 1; }
grep -q '^w3$' /tmp/fake-official-deletes.log || { echo "FAIL: resurrected w3 not swept"; exit 1; }
grep -q "zero-live read 3/3" /tmp/wd.log || { echo "FAIL: no triple zero-live confirmation"; cat /tmp/wd.log; exit 1; }
python3 - <<'PY'
import json
state = json.load(open("/tmp/wd-state.json"))
assert "w1" in state and "terminal_at" in state["w1"], state.keys()
spent = state["w1"]["rate"] * (state["w1"]["terminal_at"] - state["w1"]["created"]) / 3600
assert spent > 0.04, ("churned pod spend was NOT retained", spent)
print(f"   churn-proof: w1 retained ${spent:.4f} after termination")
PY
echo "ok: scenario A — churn-proof cap, CP-first, resurrection swept, 3/3 zero-live"

# --- Scenario B: malformed rate accrues at MAX rate (fail-closed) ---
curl -fsS -X POST "$CTRL/kill" -d '{"id":"w2"}' >/dev/null 2>&1 || true
rm -f /tmp/wd-state.json /tmp/fake-official-deletes.log /tmp/wd.log
spawn '{"id":"cp2","name":"weinfer-controlplane-y","cost":0.05,"age_secs":100}'
spawn '{"id":"wm","name":"'"$PREFIX"'garbage","cost":"not-a-number","age_secs":600}'
WEINFER_RUNPOD_API="http://127.0.0.1:${PORT}/v2" WEINFER_WATCHDOG_INTERVAL=2 \
  WEINFER_MAX_GPU_RATE=0.40 \
  bash "$WATCHDOG" /tmp/wd-key "$(( $(date +%s) - 3600 ))" 0.05 "$PREFIX" cp2 /tmp/wd-state.json \
  > /tmp/wd.log 2>&1 & WD_PID=$!
# 600s at the fail-closed $0.40/hr = $0.0667 >= $0.05: breach at once.
for i in $(seq 1 60); do
  kill -0 "$WD_PID" 2>/dev/null || break
  if [ "$i" = 60 ]; then echo "FAIL: malformed rate never breached"; cat /tmp/wd.log; exit 1; fi
  sleep 2
done
wait "$WD_PID" && CODE=0 || CODE=$?
[ "$CODE" = "2" ] || { echo "FAIL: expected exit 2 on malformed rate, got $CODE"; cat /tmp/wd.log; exit 1; }
echo "ok: scenario B — unknown rate accrued at max permitted rate (fail-closed)"

# --- Scenario C: provider outage — retries loudly, never exits blind ---
rm -f /tmp/wd-state.json /tmp/wd.log
curl -fsS -X POST "$CTRL/outage" -d '{"count":5}' >/dev/null
WEINFER_RUNPOD_API="http://127.0.0.1:${PORT}/v2" WEINFER_WATCHDOG_INTERVAL=2 \
  bash "$WATCHDOG" /tmp/wd-key "$(date +%s)" 5.00 "$PREFIX" cp-none /tmp/wd-state.json \
  > /tmp/wd.log 2>&1 & WD_PID=$!
sleep 16
kill -0 "$WD_PID" 2>/dev/null || { echo "FAIL: watchdog died during outage"; cat /tmp/wd.log; exit 1; }
grep -q "provider list FAILED" /tmp/wd.log || { echo "FAIL: outage not surfaced"; cat /tmp/wd.log; exit 1; }
grep -q "cumulative" /tmp/wd.log || { echo "FAIL: never recovered after outage"; cat /tmp/wd.log; exit 1; }
kill "$WD_PID" 2>/dev/null; wait "$WD_PID" 2>/dev/null || true
echo "ok: scenario C — outage retried loudly, recovered, never exited blind"

# --- Scenario D: a pod vanishes between collection reads -----------
# The paid canary exposed the provider behavior: a terminated pod can
# disappear from /pods before its final billed tail.  The watchdog
# must close it at the exact-id 404 observation, never retroactively
# at its older collection sighting.
PREFIX_D="weinfer-watchdog-missing-"
rm -f /tmp/wd-state.json /tmp/wd.log
spawn '{"id":"wdrop","name":"'"$PREFIX_D"'worker","cost":0.19,"age_secs":100}'
WEINFER_RUNPOD_API="http://127.0.0.1:${PORT}/v2" WEINFER_WATCHDOG_INTERVAL=1 \
  bash "$WATCHDOG" /tmp/wd-key "$(( $(date +%s) - 3600 ))" 5.00 "$PREFIX_D" cp-none /tmp/wd-state.json \
  > /tmp/wd.log 2>&1 & WD_PID=$!
for i in $(seq 1 20); do
  python3 -c 'import json,sys; d=json.load(open("/tmp/wd-state.json")); sys.exit(0 if "wdrop" in d else 1)' \
    2>/dev/null && break
  [ "$i" = 20 ] && { echo "FAIL: disappearing pod was never observed"; cat /tmp/wd.log; exit 1; }
  sleep 1
done
sleep 2
curl -fsS -X POST "$CTRL/drop" -d '{"id":"wdrop"}' >/dev/null
for i in $(seq 1 20); do
  python3 - <<'PY' 2>/dev/null && break
import json, sys
e = json.load(open("/tmp/wd-state.json"))["wdrop"]
sys.exit(0 if e.get("terminal_at", 0) > e.get("last_seen", 0) else 1)
PY
  [ "$i" = 20 ] && { echo "FAIL: exact 404 did not conservatively close the pod"; cat /tmp/wd.log; cat /tmp/wd-state.json; exit 1; }
  sleep 1
done
kill "$WD_PID" 2>/dev/null; wait "$WD_PID" 2>/dev/null || true
python3 - <<'PY'
import json
e = json.load(open("/tmp/wd-state.json"))["wdrop"]
assert e["terminal_at"] > e["last_seen"], e
print(f"   missing-tail closure: +{e['terminal_at'] - e['last_seen']:.3f}s conservatively accrued")
PY
echo "ok: scenario D — vanished pod reconciled by exact id, never frozen at last sighting"

# --- Scenario E: deliberate campaign stand-down is not a breach ---
rm -f /tmp/wd-state.json /tmp/wd.log /tmp/wd-standdown /tmp/fake-official-deletes.log
spawn '{"id":"cp5","name":"weinfer-controlplane-standdown","cost":0.05,"age_secs":10}'
spawn '{"id":"w5","name":"'"$PREFIX"'standdown","cost":0.19,"age_secs":10}'
WEINFER_RUNPOD_API="http://127.0.0.1:${PORT}/v2" WEINFER_WATCHDOG_INTERVAL=1 \
  WEINFER_WATCHDOG_STANDDOWN_FILE=/tmp/wd-standdown \
  bash "$WATCHDOG" /tmp/wd-key "$(date +%s)" 1.00 "$PREFIX" cp5 /tmp/wd-state.json \
  > /tmp/wd.log 2>&1 & WD_PID=$!
for i in $(seq 1 20); do
  grep -q "cumulative" /tmp/wd.log 2>/dev/null && break
  [ "$i" = 20 ] && { echo "FAIL: stand-down watchdog never surveyed"; cat /tmp/wd.log; exit 1; }
  sleep 1
done
touch /tmp/wd-standdown
for i in $(seq 1 30); do
  kill -0 "$WD_PID" 2>/dev/null || break
  [ "$i" = 30 ] && { echo "FAIL: stand-down never completed"; cat /tmp/wd.log; exit 1; }
  sleep 1
done
wait "$WD_PID" && CODE=0 || CODE=$?
[ "$CODE" = "0" ] || { echo "FAIL: stand-down exited $CODE"; cat /tmp/wd.log; exit 1; }
[ "$(head -1 /tmp/fake-official-deletes.log)" = "cp5" ] || {
  echo "FAIL: stand-down did not delete the control plane first"; cat /tmp/fake-official-deletes.log; exit 1;
}
grep -q '^w5$' /tmp/fake-official-deletes.log || { echo "FAIL: stand-down worker not swept"; exit 1; }
grep -q "stand-down handled: control plane down, workers gone, 3/3 zero-live reads" /tmp/wd.log || {
  echo "FAIL: stand-down closeout was not labeled/proven"; cat /tmp/wd.log; exit 1;
}
echo "ok: scenario E — deliberate stand-down exits 0 after CP-first and 3/3 zero-live"

echo "WATCHDOG REGRESSION PASS"
