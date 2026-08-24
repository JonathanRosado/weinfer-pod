#!/usr/bin/env bash
# Independent GPU-dollar watchdog v2 (codex 0163/0164): a hard
# CUMULATIVE dollar cap on manager-created GPU capacity, enforced
# OUTSIDE the gateway with the provider key read directly — it
# survives gateway failure, pod churn, and control-plane loss.
#
#   scripts/gpu_watchdog.sh <key-file> <pre-launch-epoch> <ceiling-usd> \
#       <pod-name-prefix> <control-plane-pod-id> [state-file]
#
# Accounting is CHURN-PROOF: every matching pod ever seen is recorded
# in a persistent state file keyed by pod id; terminated or replaced
# pods KEEP their accrued spend, so supervisor churn can never reset
# the meter.  An unknown or malformed rate accrues at the MAXIMUM
# permitted rate (fail-closed, never zero).  A wall-clock deadline
# derived from the ceiling and the max rate backstops the whole watch
# even if every survey fails.
#
# On breach: (1) the CONTROL-PLANE pod dies FIRST so desired capacity
# cannot resurrect workers, (2) every matching worker is deleted with
# bounded retries and per-pod verification, (3) THREE spaced zero-live
# reads must agree before the watchdog exits.  Field names accept the
# official v2 schema (status/cost) and the create-response variant
# (desiredStatus/costPerHr).
#
# Exit 2 = ceiling or deadline breached, kills verified, zero-live
# confirmed three times.  Runs forever otherwise.
set -uo pipefail

KEY_FILE="${1:?key file required}"
EPOCH="${2:?pre-launch epoch (seconds) required}"
CEILING_USD="${3:?ceiling in USD required}"
PREFIX="${4:?pod name prefix required}"
CONTROL_POD="${5:?control-plane pod id required (killed FIRST on breach)}"
STATE_FILE="${6:-/tmp/gpu_watchdog_state.json}"
INTERVAL="${WEINFER_WATCHDOG_INTERVAL:-60}"
API="${WEINFER_RUNPOD_API:-https://api.runpod.io/v2}"
MAX_RATE_USD_HR="${WEINFER_MAX_GPU_RATE:-0.40}"   # fail-closed accrual rate

[ -f "$KEY_FILE" ] || { echo "key file missing" >&2; exit 1; }
[ -f "$STATE_FILE" ] || echo '{}' > "$STATE_FILE"

# Wall-clock backstop: even with every survey failing, the watch ends
# (and kills) once the ceiling COULD have been spent at the max rate.
DEADLINE_EPOCH=$(python3 -c "print(int($EPOCH + float('$CEILING_USD')/float('$MAX_RATE_USD_HR')*3600))")

auth() { tr -d '[:space:]' < "$KEY_FILE"; }

list_pods() {
  curl -fsS --connect-timeout 10 --max-time 30 "$API/pods" \
    -H "Authorization: Bearer $(auth)"
}

# survey <pods-json>: updates the persistent ledger, prints
# "ACCRUED <usd> <live-count> <live-ids...>"
survey() {
  python3 - "$STATE_FILE" "$EPOCH" "$PREFIX" "$CONTROL_POD" "$MAX_RATE_USD_HR" "$1" <<'PY'
import datetime, json, sys
state_file, epoch, prefix, control, max_rate, raw = (
    sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4], float(sys.argv[5]), sys.argv[6])
state = json.load(open(state_file))
data = json.loads(raw)
pods = data.get("pods", data) if isinstance(data, dict) else data
now = datetime.datetime.now(datetime.timezone.utc).timestamp()

live_ids = []
seen_now = set()
for pod in pods:
    pid = pod.get("id")
    if not pid or pid == control:
        continue
    if not pod.get("name", "").startswith(prefix):
        continue
    status = pod.get("status") or pod.get("desiredStatus") or ""
    # EXITED is STOPPED (machine still assigned), not gone: it keeps
    # accruing fail-closed and stays in the sweep set until the
    # provider says TERMINATED (codex 0165).
    terminal = status == "TERMINATED"
    created = pod.get("createdAt")
    try:
        created_ts = datetime.datetime.fromisoformat(
            created.replace("Z", "+00:00")).timestamp()
    except Exception:
        created_ts = float(state.get(pid, {}).get("created", epoch))
    if created_ts < epoch - 60:
        continue
    raw_rate = pod.get("cost", pod.get("costPerHr"))
    try:
        rate = float(raw_rate)
        if rate <= 0:
            rate = max_rate          # zero/negative rate: fail-closed
    except (TypeError, ValueError):
        rate = max_rate              # unknown rate: fail-closed
    entry = state.get(pid, {"created": created_ts, "rate": rate})
    entry["rate"] = max(entry.get("rate", 0) or max_rate, rate)
    entry["created"] = min(float(entry.get("created", created_ts)), created_ts)
    entry["last_seen"] = now
    if terminal:
        entry["terminal_at"] = min(float(entry.get("terminal_at", now)), now)
    state[pid] = entry
    seen_now.add(pid)
    if not terminal:
        live_ids.append(pid)

# A pod that vanished from the listing is DONE accruing as of its
# last sighting — but its spend up to then is kept forever.
for pid, entry in state.items():
    if pid not in seen_now and "terminal_at" not in entry:
        entry["terminal_at"] = float(entry.get("last_seen", now))

total = 0.0
for entry in state.values():
    end = float(entry.get("terminal_at", now))
    total += float(entry["rate"]) * max(0.0, end - float(entry["created"])) / 3600.0

json.dump(state, open(state_file, "w"))
print(f"ACCRUED {total:.6f} {len(live_ids)} {' '.join(live_ids)}")
PY
}

delete_verified() { # pod_id -> 0 iff provider says 404/terminal
  local pod_id="$1"
  for attempt in 1 2 3; do
    curl -sS --connect-timeout 10 --max-time 30 -X DELETE "$API/pods/${pod_id}" \
      -H "Authorization: Bearer $(auth)" >/dev/null 2>&1 || true
    sleep 3
    local body code
    body=$(curl -sS --connect-timeout 10 --max-time 30 -w '\n%{http_code}' \
      "$API/pods/${pod_id}" -H "Authorization: Bearer $(auth)") || { sleep 5; continue; }
    code="${body##*$'\n'}"
    [ "$code" = "404" ] && return 0
    if [ "$code" = "200" ]; then
      printf '%s' "${body%$'\n'*}" | python3 -c "
import json,sys
pod=json.load(sys.stdin)
s=pod.get('status') or pod.get('desiredStatus') or ''
sys.exit(0 if s == 'TERMINATED' else 1)" && return 0
    fi
    sleep $((attempt * 5))
  done
  return 1
}

breach() {
  echo "[watchdog] CAP BREACHED — control plane dies FIRST (resurrection barrier)" >&2
  until delete_verified "$CONTROL_POD"; do
    echo "[watchdog] control-plane kill unverified; retrying" >&2
    sleep "$INTERVAL"
  done
  # Kill workers, then demand THREE spaced clean reads.
  local clean_reads=0
  while [ "$clean_reads" -lt 3 ]; do
    local listing out live ids
    listing=$(list_pods) || {
      echo "[watchdog] list failed during kill; retrying (fail-closed)" >&2
      sleep "$INTERVAL"; continue
    }
    out=$(survey "$listing") || { sleep 10; continue; }
    live=$(echo "$out" | awk '{print $3}')
    ids=$(echo "$out" | cut -d' ' -f4-)
    if [ "$live" = "0" ]; then
      clean_reads=$((clean_reads + 1))
      echo "[watchdog] zero-live read ${clean_reads}/3" >&2
      sleep "$INTERVAL"
    else
      clean_reads=0
      for pod_id in $ids; do
        delete_verified "$pod_id" \
          || echo "[watchdog] ${pod_id} kill unverified; will re-sweep" >&2
      done
    fi
  done
  echo "[watchdog] breach handled: control plane down, workers gone, 3/3 zero-live reads" >&2
  exit 2
}

echo "[watchdog] armed: ceiling \$${CEILING_USD} cumulative, max-rate \$${MAX_RATE_USD_HR}/hr, wall deadline epoch ${DEADLINE_EPOCH}, prefix ${PREFIX}, control ${CONTROL_POD}"
FAILS=0
while :; do
  NOW=$(date +%s)
  if [ "$NOW" -ge "$DEADLINE_EPOCH" ]; then
    echo "[watchdog] WALL-CLOCK deadline reached — treating as breach (fail-closed)" >&2
    breach
  fi
  if LISTING=$(list_pods); then
    FAILS=0
    OUT=$(survey "$LISTING") || { sleep 30; continue; }
    ACCRUED=$(echo "$OUT" | awk '{print $2}')
    LIVE=$(echo "$OUT" | awk '{print $3}')
    echo "[watchdog] $(date -u +%H:%M:%SZ) cumulative \$${ACCRUED} (${LIVE} live)"
    OVER=$(python3 -c "print(1 if float('$ACCRUED') >= float('$CEILING_USD') else 0)")
    [ "$OVER" = "1" ] && breach
  else
    FAILS=$((FAILS + 1))
    echo "[watchdog] provider list FAILED (${FAILS} consecutive) — retrying, never exiting blind" >&2
  fi
  sleep "$INTERVAL"
done
