#!/usr/bin/env bash
# GPU-watchdog regression (codex 0164) against the OFFICIAL v2 shape
# (status/cost): churn-proof cumulative accounting, fail-closed
# malformed rates, outage resilience, control-plane-first kill, a
# post-breach resurrection swept, and triple zero-live confirmation.
# Zero spend, zero real provider calls.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SELF="${SCRIPT_DIR}/$(basename "$0")"
cd "${SCRIPT_DIR}/.."

# Every scenario below intentionally shares one fake-provider port and a fixed
# set of /tmp paths.  Concurrent harnesses would therefore corrupt each
# other's state and can leave a failure looking like a product regression.
# Refuse a second owner explicitly before either harness can touch those paths.
REGRESSION_LOCK="${TMPDIR:-/tmp}/weinfer-watchdog-regression.lock"
if ! mkdir "$REGRESSION_LOCK" 2>/dev/null; then
  echo "WATCHDOG REGRESSION REFUSED: another run owns $REGRESSION_LOCK" >&2
  exit 73
fi

cleanup() {
  local child
  while IFS= read -r child; do
    [ -n "$child" ] && kill "$child" 2>/dev/null || true
  done < <(jobs -pr)
  wait 2>/dev/null || true
  rmdir "$REGRESSION_LOCK" 2>/dev/null || true
}
on_signal() {
  local code="$1"
  trap - EXIT
  cleanup
  exit "$code"
}
trap cleanup EXIT
trap 'on_signal 130' INT
trap 'on_signal 143' TERM HUP

# Red for the bug class: while this run owns the global fixtures, a second
# invocation must fail as contention (73), never enter a scenario and emit a
# plausible FAIL about the observer under test.
set +e
LOCK_REFUSAL="$(bash "$SELF" 2>&1)"
LOCK_REFUSAL_CODE=$?
set -e
[ "$LOCK_REFUSAL_CODE" = "73" ] || {
  echo "FAIL: concurrent watchdog regression exited $LOCK_REFUSAL_CODE, expected 73"
  echo "$LOCK_REFUSAL"
  exit 1
}
case "$LOCK_REFUSAL" in
  *"WATCHDOG REGRESSION REFUSED: another run owns "*) ;;
  *) echo "FAIL: concurrent watchdog regression refusal was not explicit"; echo "$LOCK_REFUSAL"; exit 1 ;;
esac
echo "ok: regression ownership — concurrent run refused explicitly before shared fixtures"

FAKE="${FAKE:-infra/controlplane/ci/fake_runpod_official.py}"
WATCHDOG="${WATCHDOG:-scripts/gpu_watchdog.sh}"
CAMPAIGN_WATCHDOG="${CAMPAIGN_WATCHDOG:-${WATCHDOG%/*}/gpu_watchdog_campaign.sh}"
PORT=18991
CTRL="http://127.0.0.1:${PORT}/control"
PREFIX="weinfer-community-qwen7b-0-"

python3 "$FAKE" "$PORT" & FAKE_PID=$!
sleep 1
rm -f /tmp/fake-official-deletes.log /tmp/wd-state.json /tmp/wd.log
printf 'fake-key' > /tmp/wd-key

# --- Arming contract: persistent observers are launchd-only --------
# An interactive shell on macOS can inherit XPC_SERVICE_NAME=0, so a mere
# nonempty check is decorative.  Both absent and literal-zero contexts must
# refuse before creating state or printing an armed banner.
assert_unmanaged_refused() {
  local observer="$1" name="$2" state="/tmp/wd-arm-${2}.json" log="/tmp/wd-arm-${2}.log"
  shift 2
  rm -f "$state" "$log"
  set +e
  env -u WEINFER_WATCHDOG_ALLOW_UNMANAGED "$@" \
    bash "$observer" /tmp/wd-key "$(date +%s)" 1.00 "$PREFIX" cp-none "$state" \
    > "$log" 2>&1
  local code=$?
  set -e
  [ "$code" -ne 0 ] || { echo "FAIL: unmanaged ${name} observer armed"; exit 1; }
  [ ! -e "$state" ] || { echo "FAIL: refused ${name} observer created state"; exit 1; }
  grep -q 'WATCHDOG ARM REFUSED' "$log" || {
    echo "FAIL: ${name} refusal was not explicit"; cat "$log"; exit 1;
  }
  ! grep -q '\[watchdog\] armed:' "$log" || {
    echo "FAIL: refused ${name} observer claimed to be armed"; cat "$log"; exit 1;
  }
}

assert_unmanaged_refused "$WATCHDOG" short-env-absent env -u XPC_SERVICE_NAME
assert_unmanaged_refused "$WATCHDOG" short-env-zero env XPC_SERVICE_NAME=0
assert_unmanaged_refused "$CAMPAIGN_WATCHDOG" campaign-env-zero \
  env XPC_SERVICE_NAME=0 WEINFER_WATCHDOG_CAMPAIGN_SECONDS=30

# A launchd-shaped label passes the contract and reaches the ordinary loop.
rm -f /tmp/wd-arm-launchd.json /tmp/wd-arm-launchd.log
XPC_SERVICE_NAME=com.weinfer.test.watchdog \
  WEINFER_RUNPOD_API="http://127.0.0.1:${PORT}/v2" WEINFER_WATCHDOG_INTERVAL=1 \
  bash "$WATCHDOG" /tmp/wd-key "$(date +%s)" 5.00 "$PREFIX" cp-none \
    /tmp/wd-arm-launchd.json > /tmp/wd-arm-launchd.log 2>&1 & ARM_PID=$!
for i in $(seq 1 20); do
  grep -q '\[watchdog\] armed:' /tmp/wd-arm-launchd.log 2>/dev/null && break
  [ "$i" = 20 ] && {
    echo "FAIL: launchd-shaped observer never armed"; cat /tmp/wd-arm-launchd.log; exit 1;
  }
  sleep 1
done
kill "$ARM_PID" 2>/dev/null; wait "$ARM_PID" 2>/dev/null || true
[ -f /tmp/wd-arm-launchd.json ] || { echo "FAIL: armed observer created no state"; exit 1; }
echo "ok: observer arming — absent/zero XPC refused pre-state; com.weinfer launchd accepted"

# The remaining scenarios own every child lifetime explicitly; this escape
# hatch exists only so the regression can exercise the observer loop directly.
export WEINFER_WATCHDOG_ALLOW_UNMANAGED=1

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

# --- Scenario F: healthy empty surveys may outlive the spend horizon ---
# With a $0.001 ceiling and $3.60/hr maximum, the legacy wall would
# fire after one second.  The long-campaign observer must remain alive
# across healthy EMPTY surveys because no GPU spend exists.
rm -f /tmp/wd-state.json /tmp/wd.log /tmp/wd-standdown /tmp/fake-official-deletes.log
PREFIX_LONG="weinfer-watchdog-long-"
spawn '{"id":"cp6","name":"weinfer-controlplane-long","cost":0.05,"age_secs":1}'
WEINFER_RUNPOD_API="http://127.0.0.1:${PORT}/v2" WEINFER_WATCHDOG_INTERVAL=1 \
  WEINFER_MAX_GPU_RATE=3.60 WEINFER_WATCHDOG_CAMPAIGN_SECONDS=30 \
  WEINFER_WATCHDOG_STANDDOWN_FILE=/tmp/wd-standdown \
  bash "$CAMPAIGN_WATCHDOG" /tmp/wd-key "$(date +%s)" 0.001 "$PREFIX_LONG" cp6 /tmp/wd-state.json \
  > /tmp/wd.log 2>&1 & WD_PID=$!
sleep 3
kill -0 "$WD_PID" 2>/dev/null || {
  echo "FAIL: healthy empty long campaign died at the short spend horizon"; cat /tmp/wd.log; exit 1;
}
grep -q 'cumulative \$0.000000 (0 live)' /tmp/wd.log || {
  echo "FAIL: healthy empty surveys were not proven"; cat /tmp/wd.log; exit 1;
}
touch /tmp/wd-standdown
for i in $(seq 1 30); do
  kill -0 "$WD_PID" 2>/dev/null || break
  [ "$i" = 30 ] && { echo "FAIL: long-campaign stand-down did not finish"; cat /tmp/wd.log; exit 1; }
  sleep 1
done
wait "$WD_PID" && CODE=0 || CODE=$?
[ "$CODE" = "0" ] || { echo "FAIL: long-campaign stand-down exited $CODE"; cat /tmp/wd.log; exit 1; }
echo "ok: scenario F — healthy empty surveys extend time without extending dollars"

# --- Scenario G: provider blindness still has a dollar backstop ---
# The same one-second remaining-spend horizon MUST kill during a
# continuous list outage even though the campaign wall is 30 seconds.
rm -f /tmp/wd-state.json /tmp/wd.log /tmp/fake-official-deletes.log
spawn '{"id":"cp7","name":"weinfer-controlplane-blind","cost":0.05,"age_secs":1}'
curl -fsS -X POST "$CTRL/outage" -d '{"count":5}' >/dev/null
WEINFER_RUNPOD_API="http://127.0.0.1:${PORT}/v2" WEINFER_WATCHDOG_INTERVAL=1 \
  WEINFER_MAX_GPU_RATE=3.60 WEINFER_WATCHDOG_CAMPAIGN_SECONDS=30 \
  bash "$CAMPAIGN_WATCHDOG" /tmp/wd-key "$(date +%s)" 0.001 "$PREFIX_LONG" cp7 /tmp/wd-state.json \
  > /tmp/wd.log 2>&1 & WD_PID=$!
for i in $(seq 1 30); do
  kill -0 "$WD_PID" 2>/dev/null || break
  [ "$i" = 30 ] && { echo "FAIL: blind interval never consumed spend authority"; cat /tmp/wd.log; exit 1; }
  sleep 1
done
wait "$WD_PID" && CODE=0 || CODE=$?
[ "$CODE" = "2" ] || { echo "FAIL: blind long campaign exited $CODE"; cat /tmp/wd.log; exit 1; }
grep -q 'unreadable interval consumed the remaining spend authority' /tmp/wd.log || {
  echo "FAIL: blind-spend backstop not named"; cat /tmp/wd.log; exit 1;
}
[ "$(head -1 /tmp/fake-official-deletes.log)" = "cp7" ] || {
  echo "FAIL: blind-spend backstop did not kill the control plane first"; exit 1;
}
echo "ok: scenario G — provider blindness cannot outlive the remaining dollar budget"

# --- Scenario H: a local control process is the resurrection barrier ------
# The public-tunnel path has no provider control-pod id. Its real gateway PID
# must receive TERM and exit BEFORE any provider worker delete is issued.
rm -f /tmp/wd-state.json /tmp/wd.log /tmp/wd-standdown /tmp/fake-official-deletes.log
(
  trap 'echo LOCAL_CONTROL_TERM >> /tmp/fake-official-deletes.log; exit 0' TERM
  while :; do sleep 1; done
) & LOCAL_CONTROL_PID=$!
spawn '{"id":"w8","name":"'"$PREFIX_LONG"'local-control","cost":0.19,"age_secs":1}'
WEINFER_RUNPOD_API="http://127.0.0.1:${PORT}/v2" WEINFER_WATCHDOG_INTERVAL=1 \
  WEINFER_MAX_GPU_RATE=0.40 WEINFER_WATCHDOG_CAMPAIGN_SECONDS=30 \
  WEINFER_WATCHDOG_CONTROL_PID="$LOCAL_CONTROL_PID" \
  WEINFER_WATCHDOG_STANDDOWN_FILE=/tmp/wd-standdown \
  bash "$CAMPAIGN_WATCHDOG" /tmp/wd-key "$(date +%s)" 1.00 "$PREFIX_LONG" local-control /tmp/wd-state.json \
  > /tmp/wd.log 2>&1 & WD_PID=$!
for i in $(seq 1 20); do
  grep -q "local_pid ${LOCAL_CONTROL_PID}" /tmp/wd.log 2>/dev/null && break
  [ "$i" = 20 ] && { echo "FAIL: local-control watchdog never armed"; cat /tmp/wd.log; exit 1; }
  sleep 1
done
touch /tmp/wd-standdown
for i in $(seq 1 30); do
  kill -0 "$WD_PID" 2>/dev/null || break
  [ "$i" = 30 ] && { echo "FAIL: local-control stand-down never completed"; cat /tmp/wd.log; exit 1; }
  sleep 1
done
wait "$WD_PID" && CODE=0 || CODE=$?
[ "$CODE" = "0" ] || { echo "FAIL: local-control stand-down exited $CODE"; cat /tmp/wd.log; exit 1; }
kill -0 "$LOCAL_CONTROL_PID" 2>/dev/null && { echo "FAIL: local control survived closeout"; exit 1; }
[ "$(head -1 /tmp/fake-official-deletes.log)" = "LOCAL_CONTROL_TERM" ] || {
  echo "FAIL: provider delete preceded local control termination"; cat /tmp/fake-official-deletes.log; exit 1;
}
grep -q '^w8$' /tmp/fake-official-deletes.log || { echo "FAIL: local-control worker not swept"; exit 1; }
grep -q "local control process ${LOCAL_CONTROL_PID} joined" /tmp/wd.log || {
  echo "FAIL: local control join not proven"; cat /tmp/wd.log; exit 1;
}
echo "ok: scenario H — local control joined before provider worker sweep"

echo "WATCHDOG REGRESSION PASS"
