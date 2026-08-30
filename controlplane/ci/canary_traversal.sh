#!/usr/bin/env bash
# The COMPLETE customer path against a live control plane (codex
# 0162/0163): durable org -> bounded credit -> issued credential ->
# priced model discovery -> job submit + idempotent replay -> exact
# reservation arithmetic -> (live) poll to completion -> frozen-rate
# charge, hold release, and balance conservation — every claim
# ASSERTED from the public APIs, never merely printed.
#
# EVERY identity is bound to CANARY_RUN_ID (required): a retry of one
# run must REPLAY (same job, unchanged reservation); a new run must
# DISPATCH new work in a fresh org.  A stale success can never
# false-green a later run (codex 0163).
#
# Usage: CANARY_RUN_ID=<id> CANARY_SERVING_PROFILE=<profile> \
#   scripts/canary_traversal.sh <public-base> <credentials-file>
set -euo pipefail

BASE="${1:?public base URL required}"
CRED_FILE="${2:?credentials file required}"
RUN_ID="${CANARY_RUN_ID:?CANARY_RUN_ID is required: it binds the org, grant, key, and idempotency identities}"
SERVING_PROFILE="${CANARY_SERVING_PROFILE:-qwen7b-consumer-v1}"
case "$SERVING_PROFILE" in
  qwen7b-consumer-v1)
    MODEL="Qwen/Qwen2.5-7B-Instruct"
    CONTEXT_TOKENS=8192
    INPUT_PRICE=100000
    OUTPUT_PRICE=400000
    DEFAULT_DEADLINE_SECONDS=1200
    ;;
  gpt-oss-120b-h100-v1)
    MODEL="openai/gpt-oss-120b"
    CONTEXT_TOKENS=131072
    INPUT_PRICE=900000
    OUTPUT_PRICE=2700000
    DEFAULT_DEADLINE_SECONDS=2400
    ;;
  *)
    echo "unknown CANARY_SERVING_PROFILE: ${SERVING_PROFILE}" >&2
    exit 1
    ;;
esac
EXPECT_COMPLETION="${CANARY_EXPECT_COMPLETION:-1}"
ADMIN_KEY=$(awk -F= '/^WEINFER_ADMIN_KEY=/{print $2}' "$CRED_FILE" | awk '{print $1}')
[ -n "$ADMIN_KEY" ] || { echo "admin key missing from $CRED_FILE" >&2; exit 1; }

ORG="org-canary-${RUN_ID}"
IDEM="canary-${RUN_ID}-1"
CREDITS=2000000
MAX_TOKENS=16
# The reservation is derived from the selected profile's exact catalog
# authority.  It is never a hand-maintained second copy of either profile's
# arithmetic: ceil(context * input rate / 1e6) plus ceil(max output * output
# rate / 1e6).
EXPECT_HOLD=$((
  (CONTEXT_TOKENS * INPUT_PRICE + 999999) / 1000000
  + (MAX_TOKENS * OUTPUT_PRICE + 999999) / 1000000
))
DEADLINE_SECS="${CANARY_DEADLINE_SECONDS:-$DEFAULT_DEADLINE_SECONDS}"
python3 - "$DEADLINE_SECS" <<'PY'
import sys
raw = sys.argv[1]
if not raw.isascii() or not raw.isdigit() or int(raw) <= 0:
    raise SystemExit(
        f"CANARY_DEADLINE_SECONDS must be a positive base-10 integer, got {raw!r}"
    )
PY
POLL_BUDGET_SECONDS="${CANARY_POLL_BUDGET_SECONDS:-$((10#$DEADLINE_SECS + 600))}"
python3 - "$DEADLINE_SECS" "$POLL_BUDGET_SECONDS" <<'PY'
import sys
raw = sys.argv[2]
if not raw.isascii() or not raw.isdigit() or int(raw) <= 0:
    raise SystemExit(
        f"CANARY_POLL_BUDGET_SECONDS must be a positive base-10 integer, got {raw!r}"
    )
if int(raw) < int(sys.argv[1]):
    raise SystemExit("CANARY_POLL_BUDGET_SECONDS must cover the customer deadline")
PY
ARTIFACT_DIR="${CANARY_ARTIFACT_DIR:-${HOME}/.weinfer/canary-${RUN_ID}}"
mkdir -p "$ARTIFACT_DIR"; chmod 700 "$ARTIFACT_DIR"

say() { echo "[canary ${RUN_ID}] $*"; }

# Success predicate: EXACT equality after whitespace strip (codex
# 0165: substring matching accepted refusals that merely mentioned
# the expected text).  Self-tested with mutations before any use.
answer_ok() { # <content> -> exit 0 iff exactly canary-ok
  python3 -c "import sys;sys.exit(0 if sys.argv[1].strip() == 'canary-ok' else 1)" "$1"
}
answer_ok "canary-ok" || { echo "PREDICATE BROKEN: exact match rejected" >&2; exit 1; }
answer_ok " canary-ok
" || { echo "PREDICATE BROKEN: strip failed" >&2; exit 1; }
if answer_ok "I cannot comply; the requested text was canary-ok"; then
  echo "PREDICATE BROKEN: refusal text passed" >&2; exit 1
fi
if answer_ok "canary-ok! extra"; then
  echo "PREDICATE BROKEN: suffixed text passed" >&2; exit 1
fi
admin() { # method path [json]
  local method="$1" path="$2" body="${3:-}"
  if [ -n "$body" ]; then
    curl -fsS --connect-timeout 10 --max-time 30 -X "$method" "$BASE$path" -H "Authorization: Bearer $ADMIN_KEY" \
      -H "Content-Type: application/json" -d "$body"
  else
    curl -fsS --connect-timeout 10 --max-time 30 -X "$method" "$BASE$path" -H "Authorization: Bearer $ADMIN_KEY"
  fi
}
balance() {
  curl -fsS --connect-timeout 10 --max-time 30 "$BASE/v1/balance" -H "Authorization: Bearer $CUSTOMER_KEY"
}

say "1/8 durable organization ${ORG}"
admin POST /admin/organizations \
  "{\"org_id\":\"${ORG}\",\"name\":\"Canary ${RUN_ID}\"}" >/dev/null 2>&1 || true

KEY_STORE="${CANARY_KEY_STORE:-$HOME/.weinfer}/canary-${RUN_ID}.key"
if [ -f "$KEY_STORE" ]; then
  say "2/8 reusing this run's persisted credential (retry — no new key minted)"
  CUSTOMER_KEY=$(cat "$KEY_STORE")
else
  say "2/8 issue the customer credential (persisted 0600, bound to the run id)"
  CUSTOMER_KEY=$(admin POST "/admin/organizations/${ORG}/keys" '{}' \
    | python3 -c "import json,sys;print(json.load(sys.stdin)['raw_key'])")
  mkdir -p "$(dirname "$KEY_STORE")"; chmod 700 "$(dirname "$KEY_STORE")" 2>/dev/null || true
  umask 077; printf '%s' "$CUSTOMER_KEY" > "$KEY_STORE"; chmod 600 "$KEY_STORE"
fi

say "3/8 run-idempotent credit grant (grant_id bound to the run; \$2.00 exactly once)"
FUNDED=$(balance | python3 -c "import json,sys;print(json.load(sys.stdin)['credits_micro_usd'])")
if [ "$FUNDED" != "0" ] && [ "$FUNDED" != "$CREDITS" ]; then
  echo "FOREIGN LEDGER STATE: org ${ORG} holds ${FUNDED} micro (expected 0 or ${CREDITS}) — refusing" >&2
  exit 1
fi
# The grant_id IS the idempotency key: the server's exact-idempotent
# grant makes a concurrent or retried setup a REPLAY, never a second
# credit row (a mismatched replay is a 409, surfaced by -f).
admin POST "/admin/organizations/${ORG}/credits" \
  "{\"grant_id\":\"canary-grant-${RUN_ID}\",\"amount_micro_usd\":${CREDITS},\"memo\":\"canary-${RUN_ID}\"}" >/dev/null
FUNDED=$(balance | python3 -c "import json,sys;print(json.load(sys.stdin)['credits_micro_usd'])")
[ "$FUNDED" = "$CREDITS" ] || {
  echo "FUNDING NOT EXACT before submit: ${FUNDED} != ${CREDITS}" >&2
  exit 1
}

say "4/8 priced discovery: exact ${SERVING_PROFILE} model, prices, context, routable"
MODELS_JSON=$(curl -fsS --connect-timeout 10 --max-time 30 "$BASE/v1/models" -H "Authorization: Bearer $CUSTOMER_KEY")
python3 - "$MODEL" "$CONTEXT_TOKENS" "$INPUT_PRICE" "$OUTPUT_PRICE" "$MODELS_JSON" <<'PY'
import json, sys
model_id, context, input_price, output_price, payload = sys.argv[1:]
models = json.loads(payload)['data']
assert models, 'catalog is EMPTY: the canary would run unpriced'
m = [x for x in models if x['id'] == model_id]
assert m, 'the canary model is missing from the catalog'
m = m[0]
assert m['pricing']['input_micro_usd_per_mtok'] == int(input_price), m['pricing']
assert m['pricing']['output_micro_usd_per_mtok'] == int(output_price), m['pricing']
assert m['context_length'] == int(context), m
assert m['routable'] is True, (m, 'the model must be ROUTABLE before the canary submits')
print('   model priced exactly and routable')
PY

say "5/8 submit + idempotent replay (idempotency key ${IDEM})"
REQ=$(python3 - "$MODEL" "$MAX_TOKENS" <<'PY'
import json, sys
print(json.dumps({
    "model": sys.argv[1],
    "messages": [{"role": "user", "content": "Reply with exactly: canary-ok"}],
    "max_tokens": int(sys.argv[2]),
}, separators=(",", ":")))
PY
)
# The consumer default remains its registered 1,200 seconds.  The H100
# profile uses a 2,400-second customer expiry and a 3,000-second poll horizon.
# GPT-OSS is currently an explicit unsized gateway request: this deadline does
# not claim that the 2,600-token/s policy prior sized its service time.  The
# profile-bound long-campaign watchdog, not this customer expiry, remains the
# spend authority; the first paid traversal isolates image/AOT compatibility.
R1=$(curl -fsS --connect-timeout 10 --max-time 30 -X POST "$BASE/v1/jobs" -H "Authorization: Bearer $CUSTOMER_KEY" \
  -H "Content-Type: application/json" -H "Idempotency-Key: ${IDEM}" \
  -H "x-weinfer-deadline-seconds: $DEADLINE_SECS" -d "$REQ")
JOB_ID=$(echo "$R1" | python3 -c "import json,sys;print(json.load(sys.stdin)['job_id'])")
B1=$(balance)
python3 - "$CREDITS" "$EXPECT_HOLD" "$B1" <<'PY'
import json, sys
credits, hold = int(sys.argv[1]), int(sys.argv[2])
b = json.loads(sys.argv[3])
assert b['credits_micro_usd'] == credits, b
if b['reserved_micro_usd'] == hold:
    # LIVE state: the exact ceil-per-side reservation is held.
    assert b['available_micro_usd'] == credits - hold, b
    assert b['spent_micro_usd'] == 0, b
    print('   reservation exact:', hold, 'micro; available', b['available_micro_usd'])
elif b['reserved_micro_usd'] == 0 and b['spent_micro_usd'] > 0:
    # RETRY AFTER COMPLETION: the hold released at settlement; the
    # conservation identity is the valid claim in this state.
    assert b['available_micro_usd'] + b['spent_micro_usd'] == credits, (b, 'conservation')
    print('   replay of a COMPLETED run: settled', b['spent_micro_usd'], 'micro; conservation holds')
else:
    raise AssertionError((b, 'neither a live reservation nor a settled replay state'))
PY
R2=$(curl -fsS --connect-timeout 10 --max-time 30 -X POST "$BASE/v1/jobs" -H "Authorization: Bearer $CUSTOMER_KEY" \
  -H "Content-Type: application/json" -H "Idempotency-Key: ${IDEM}" \
  -H "x-weinfer-deadline-seconds: $DEADLINE_SECS" -d "$REQ")
JOB_ID2=$(echo "$R2" | python3 -c "import json,sys;print(json.load(sys.stdin)['job_id'])")
[ "$JOB_ID" = "$JOB_ID2" ] || { echo "replay returned a DIFFERENT job" >&2; exit 1; }

say "6/8 replay held the ledger still (no double reservation, no double charge)"
python3 - "$CREDITS" "$EXPECT_HOLD" "$B1" "$(balance)" <<'PY'
import json, sys
credits, hold = int(sys.argv[1]), int(sys.argv[2])
before, after = json.loads(sys.argv[3]), json.loads(sys.argv[4])
assert after['reserved_micro_usd'] in (hold, 0), (after, 'replay must not double-reserve')
assert after['available_micro_usd'] + after['spent_micro_usd'] + after['reserved_micro_usd'] == credits, (after, 'conservation')
assert after['spent_micro_usd'] >= before['spent_micro_usd'], (before, after)
print('   replay identical; ledger conserved')
PY
say "   job ${JOB_ID} (replay identical)"
printf '%s' "$JOB_ID" > "/tmp/canary-${RUN_ID}.job"
python3 - "$ARTIFACT_DIR/run.json" "$RUN_ID" "$JOB_ID" "$BASE" "$SERVING_PROFILE" "$MODEL" <<'PY'
import json, os, sys, tempfile, time
path, run_id, job_id, base, serving_profile, model = sys.argv[1:7]
record = {
    "run_id": run_id,
    "job_id": job_id,
    "public_base": base,
    "serving_profile": serving_profile,
    "model": model,
    "recorded_at_epoch": int(time.time()),
}
fd, tmp = tempfile.mkstemp(prefix="run.", dir=os.path.dirname(path), text=True)
with os.fdopen(fd, "w") as f:
    json.dump(record, f, sort_keys=True, indent=2)
    f.write("\n")
os.chmod(tmp, 0o600)
os.replace(tmp, path)
PY
say "   run pointer sealed at ${ARTIFACT_DIR}/run.json"

if [ "$EXPECT_COMPLETION" = "1" ]; then
  say "7/8 poll to completion"
  DEADLINE=$(( $(date +%s) + POLL_BUDGET_SECONDS ))
  while :; do
    S=$(curl -fsS --connect-timeout 10 --max-time 30 "$BASE/v1/jobs/$JOB_ID" -H "Authorization: Bearer $CUSTOMER_KEY")
    STATUS=$(echo "$S" | python3 -c "import json,sys;print(json.load(sys.stdin)['status'])")
    [ "$STATUS" = "completed" ] && break
    [ "$STATUS" = "failed" ] && { echo "$S"; echo "job FAILED" >&2; exit 1; }
    [ "$(date +%s)" -ge "$DEADLINE" ] && { echo "poll deadline exceeded" >&2; exit 1; }
    sleep 10
  done
  say "8/8 frozen-rate charge, hold release, balance conservation"
  # ONE predicate (codex 0166): the live answer goes through the
  # SAME self-mutation-tested answer_ok used above — the acceptance
  # rule cannot drift from its own test.
  CONTENT=$(python3 - "$S" <<'PY'
import json, sys
print(json.loads(sys.argv[1])['response']['choices'][0]['message']['content'])
PY
)
  answer_ok "$CONTENT" || {
    echo "task answer is not exactly canary-ok: ${CONTENT}" >&2
    exit 1
  }
  python3 - "$MAX_TOKENS" "$INPUT_PRICE" "$OUTPUT_PRICE" "$S" <<'PY'
import json, sys
max_tokens, input_price, output_price = map(int, sys.argv[1:4])
s = json.loads(sys.argv[4])
assert s['reconciliation'] == 'billed', s
u, c = s['usage'], s['charge']
assert u['completion_tokens'] <= max_tokens, u
expect = -(-u['prompt_tokens'] * input_price // 1000000) + -(-u['completion_tokens'] * output_price // 1000000)
assert c['total_micro_usd'] == expect, (c, expect, 'charge must equal the frozen-rate ceil-per-side derivation')
assert c['input_price_micro_per_mtok'] == input_price and c['output_price_micro_per_mtok'] == output_price, c
print('   charge exact:', c['total_micro_usd'], 'micro for', u)
open('/tmp/canary_charge', 'w').write(str(c['total_micro_usd']))
PY
  CHARGE=$(cat /tmp/canary_charge)
  python3 - "$CREDITS" "$CHARGE" "$(balance)" <<'PY'
import json, sys
credits, charge = int(sys.argv[1]), int(sys.argv[2])
b = json.loads(sys.argv[3])
assert b['reserved_micro_usd'] == 0, (b, 'the hold must RELEASE at settlement')
assert b['spent_micro_usd'] == charge, b
assert b['available_micro_usd'] == credits - charge, b
assert b['available_micro_usd'] + b['spent_micro_usd'] == credits, (b, 'conservation')
print('   hold released; spent + available == credits; CANARY COMPLETE')
PY
  python3 - "$JOB_ID" "$CHARGE" "$(curl -fsS --connect-timeout 10 --max-time 30 "$BASE/v1/usage" -H "Authorization: Bearer $CUSTOMER_KEY")" <<'PY'
import json, sys
job_id, charge = sys.argv[1], int(sys.argv[2])
rows = [r for r in json.loads(sys.argv[3])['data'] if r['job_id'] == job_id]
assert rows and rows[0]['reconciliation'] == 'billed', rows
assert rows[0]['charge']['total_micro_usd'] == charge, rows[0]
print('   usage ledger row agrees with the job surface')
PY
else
  say "7/8 skipped completion wait (fake provider); accepted + exact reservation is the CI proof"
  say "8/8 n/a"
fi
