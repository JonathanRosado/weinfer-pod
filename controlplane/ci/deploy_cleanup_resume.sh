#!/usr/bin/env bash
# Independently executable cleanup for an UNRESOLVED launch (codex
# 0166): finishes what deploy_controlplane.sh could not when the
# provider was unreadable.  Discovers the unique launch name, deletes
# to TERMINATED/404, requires three spaced zero-live reads, and only
# then removes the persisted state — it NEVER claims clean on a list
# failure.
#
#   scripts/deploy_cleanup_resume.sh <unresolved-state-file> [key-file]
set -euo pipefail

STATE_FILE="${1:?unresolved-state file required}"
KEY_FILE="${2:-../rig/scaffold/runpod_account_a.txt}"
NAME=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['name'])" "$STATE_FILE")
API=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['api'])" "$STATE_FILE")
KNOWN_POD=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('pod_id',''))" "$STATE_FILE")

auth() {
  if [ "${WEINFER_DEPLOY_TEST:-0}" = "1" ]; then
    printf 'ci-fake-provider-key'
  else
    tr -d '[:space:]' < "$KEY_FILE"
  fi
}
rp() {
  curl -fsS --connect-timeout 10 --max-time 30 "$@" \
    -H "Authorization: Bearer $(auth)"
}

pod_terminated() { # pod_id -> 0 iff 404/TERMINATED
  local body code
  body=$(curl -sS --connect-timeout 10 --max-time 30 -w '\n%{http_code}' \
    "$API/pods/$1" -H "Authorization: Bearer $(auth)") || return 1
  code="${body##*$'\n'}"
  [ "$code" = "404" ] && return 0
  [ "$code" = "200" ] || return 1
  printf '%s' "${body%$'\n'*}" | python3 -c "
import json,sys
pod=json.load(sys.stdin)
sys.exit(0 if (pod.get('status') or pod.get('desiredStatus')) == 'TERMINATED' else 1)"
}

echo "[resume] cleaning unresolved launch ${NAME}"
ATTEMPTS=0
while :; do
  ATTEMPTS=$((ATTEMPTS + 1))
  [ "$ATTEMPTS" -le 60 ] || { echo "[resume] 60 attempts exhausted; resource still unresolved" >&2; exit 1; }
  # Discover: the known pod id, else by unique name.
  POD_ID="$KNOWN_POD"
  if [ -z "$POD_ID" ]; then
    if LISTING=$(rp "$API/pods" 2>/dev/null); then
      POD_ID=$(python3 - "$NAME" "$LISTING" <<'PY'
import json, sys
pods = json.loads(sys.argv[2])
pods = pods.get("pods", pods) if isinstance(pods, dict) else pods
match = [p for p in pods
         if p.get("name") == sys.argv[1]
         and (p.get("status") or p.get("desiredStatus")) != "TERMINATED"]
print(match[0]["id"] if match else "")
PY
)
    else
      echo "[resume] provider list FAILED — retrying (never claiming clean)" >&2
      sleep 10
      continue
    fi
  fi
  if [ -n "$POD_ID" ] && ! pod_terminated "$POD_ID"; then
    curl -sS --connect-timeout 10 --max-time 30 -X DELETE "$API/pods/${POD_ID}" \
      -H "Authorization: Bearer $(auth)" >/dev/null 2>&1 || true
    sleep 3
    continue
  fi
  # Nothing found (or found terminated): demand THREE spaced clean
  # name-scoped reads before declaring the launch resolved.
  CLEAN=0
  while [ "$CLEAN" -lt 3 ]; do
    if LISTING=$(rp "$API/pods" 2>/dev/null); then
      LIVE=$(python3 - "$NAME" "$LISTING" <<'PY'
import json, sys
pods = json.loads(sys.argv[2])
pods = pods.get("pods", pods) if isinstance(pods, dict) else pods
print(sum(1 for p in pods
          if p.get("name") == sys.argv[1]
          and (p.get("status") or p.get("desiredStatus")) != "TERMINATED"))
PY
)
      if [ "$LIVE" = "0" ]; then
        CLEAN=$((CLEAN + 1))
        sleep 3
      else
        echo "[resume] a live pod reappeared; re-sweeping" >&2
        CLEAN=-1
        break
      fi
    else
      echo "[resume] list FAILED during confirmation — restarting reads" >&2
      CLEAN=0
      sleep 10
    fi
  done
  [ "$CLEAN" = "3" ] || continue
  rm -f "$STATE_FILE"
  echo "[resume] RESOLVED: ${NAME} verified gone (3/3 clean reads); state cleared"
  exit 0
done
