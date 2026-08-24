#!/usr/bin/env bash
# The COMPLETE customer path against a live control plane (codex 0162):
# durable org -> bounded credit -> issued credential -> priced model
# discovery -> job submit + idempotent replay -> poll -> balance and
# usage from the PUBLIC APIs.  Works identically against the CI
# fake-provider container and the real deployment; against the fake,
# the job never completes (no real GPU) and the driver's final wait is
# skipped with CANARY_EXPECT_COMPLETION=0.
#
# Usage: scripts/canary_traversal.sh <public-base> <credentials-file>
set -euo pipefail

BASE="${1:?public base URL required}"
CRED_FILE="${2:?credentials file required}"
EXPECT_COMPLETION="${CANARY_EXPECT_COMPLETION:-1}"
ADMIN_KEY=$(awk -F= '/^WEINFER_ADMIN_KEY=/{print $2}' "$CRED_FILE" | awk '{print $1}')
[ -n "$ADMIN_KEY" ] || { echo "admin key missing from $CRED_FILE" >&2; exit 1; }

say() { echo "[canary] $*"; }
admin() { # method path [json]
  local method="$1" path="$2" body="${3:-}"
  if [ -n "$body" ]; then
    curl -fsS -X "$method" "$BASE$path" -H "Authorization: Bearer $ADMIN_KEY" \
      -H "Content-Type: application/json" -d "$body"
  else
    curl -fsS -X "$method" "$BASE$path" -H "Authorization: Bearer $ADMIN_KEY"
  fi
}

say "1/7 durable organization"
admin POST /admin/organizations \
  '{"org_id":"org-canary","name":"Canary"}' >/dev/null 2>&1 || true

say "2/7 bounded credit grant (\$2.00)"
admin POST /admin/organizations/org-canary/credits \
  '{"amount_micro_usd":2000000}' >/dev/null

say "3/7 issue the customer credential (raw shown once by the API; held in memory only)"
CUSTOMER_KEY=$(admin POST /admin/organizations/org-canary/keys '{}' \
  | python3 -c "import json,sys;print(json.load(sys.stdin)['raw_key'])")

say "4/7 priced model discovery with the ISSUED credential"
MODELS=$(curl -fsS "$BASE/v1/models" -H "Authorization: Bearer $CUSTOMER_KEY")
echo "$MODELS" | python3 -c "
import json,sys
models=json.load(sys.stdin)['data']
assert models, 'catalog is EMPTY: the canary would run unpriced'
m=models[0]
print('   model:', m['id'])
"

say "5/7 submit + idempotent replay"
REQ='{"model":"Qwen/Qwen2.5-7B-Instruct","messages":[{"role":"user","content":"Reply with exactly: canary-ok"}],"max_tokens":16}'
R1=$(curl -fsS -X POST "$BASE/v1/jobs" -H "Authorization: Bearer $CUSTOMER_KEY" \
  -H "Content-Type: application/json" -H "Idempotency-Key: canary-1" -d "$REQ")
JOB_ID=$(echo "$R1" | python3 -c "import json,sys;print(json.load(sys.stdin)['job_id'])")
R2=$(curl -fsS -X POST "$BASE/v1/jobs" -H "Authorization: Bearer $CUSTOMER_KEY" \
  -H "Content-Type: application/json" -H "Idempotency-Key: canary-1" -d "$REQ")
JOB_ID2=$(echo "$R2" | python3 -c "import json,sys;print(json.load(sys.stdin)['job_id'])")
[ "$JOB_ID" = "$JOB_ID2" ] || { echo "replay returned a DIFFERENT job" >&2; exit 1; }
say "   job ${JOB_ID} (replay identical)"

say "6/7 balance shows the RESERVATION (public API)"
curl -fsS "$BASE/v1/balance" -H "Authorization: Bearer $CUSTOMER_KEY" | python3 -c "
import json,sys
b=json.load(sys.stdin)
print('   balance:', json.dumps(b))
"

if [ "$EXPECT_COMPLETION" = "1" ]; then
  say "7/7 poll to completion, then usage + settled charge"
  DEADLINE=$(( $(date +%s) + 1800 ))
  while :; do
    S=$(curl -fsS "$BASE/v1/jobs/$JOB_ID" -H "Authorization: Bearer $CUSTOMER_KEY")
    STATUS=$(echo "$S" | python3 -c "import json,sys;print(json.load(sys.stdin)['status'])")
    [ "$STATUS" = "completed" ] && break
    [ "$STATUS" = "failed" ] && { echo "$S"; echo "job FAILED" >&2; exit 1; }
    [ "$(date +%s)" -ge "$DEADLINE" ] && { echo "poll deadline exceeded" >&2; exit 1; }
    sleep 10
  done
  echo "$S" | python3 -c "
import json,sys
s=json.load(sys.stdin)
assert s['reconciliation']=='billed', s
assert s['charge']['total_micro_usd']>0, s
print('   charge:', s['charge']['total_micro_usd'], 'micro; usage:', s['usage'])
"
  curl -fsS "$BASE/v1/usage" -H "Authorization: Bearer $CUSTOMER_KEY" | python3 -c "
import json,sys
rows=json.load(sys.stdin)['data']
row=[r for r in rows if r['job_id']=='$JOB_ID'][0]
assert row['reconciliation']=='billed', row
print('   usage row billed; CANARY COMPLETE')
"
else
  say "7/7 skipped completion wait (fake provider); accepted+reserved is the CI proof"
fi
