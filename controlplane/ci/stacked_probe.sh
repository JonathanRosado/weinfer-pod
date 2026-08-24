#!/usr/bin/env bash
# Stacked product-path probe — ONE arm per invocation (codex-gated).
#
#   PAIR_ID=<shared-id> ARM=baseline scripts/stacked_probe.sh
#   PAIR_ID=<shared-id> ARM=stacked  scripts/stacked_probe.sh
#   scripts/stacked_collect.sh <shared-id>     # FREE: settle + results
#   scripts/stacked_compare.sh <shared-id>     # the pair verdict
#
# This script owns ONLY the paid arm through durable termination and
# exits immediately — provider billing latency lives entirely in the
# free, idempotent collector, never on the innovation clock.  A fresh
# pair is enforced (baseline refuses an existing pair; stacked refuses
# a re-run); any retry is a NEW pair id.
#
# ΔSTACK-02 contract (codex 0109):
#  - unique pair/run namespace: pool, org, idempotency keys, database,
#    run dir all carry PAIR_ID+ARM — arms can NEVER replay each other;
#  - allocator env EXPORTED in a subshell (an inline ${X:+A=b} word is
#    NOT an assignment in bash — scripts/probe_env_test.sh regresses
#    the old broken expansion);
#  - model AND tokenizer pinned to one immutable revision;
#  - deterministic sampling: temperature=0, per-request seed=0, engine
#    --seed 0;
#  - frozen prefill-heavy product-native load (~4K prompt / 64
#    completion tokens), workload JSONL hashed and recorded;
#  - fail closed: every intended job must COMPLETE; metrics only after
#    charge>0 and state charged|settled_provisional|settled; the
#    measured-token floor is enforced by stacked_compare.sh;
#  - watchdog bound to THIS run's pod-name prefix with the pre-launch
#    epoch; provider list/parse failures are FATAL, never "empty".
set -euo pipefail
cd "$(dirname "$0")/.."

ARM="${ARM:?set ARM=baseline|stacked}"
PAIR_ID="${PAIR_ID:?set PAIR_ID (one shared id for both arms of a pair)}"
N_JOBS="${N_JOBS:-300}"
# DEMAND-LED WINDOW (codex 0113): jobs land in the durable queue
# before the pod exists, so their deadline must cover cold boot
# (observed: image pull alone ~12 min community; pull+extract+15GB
# model+engine load bounded below by 27.7GB transfer) PLUS the load.
BOOT_BUDGET_SECS="${BOOT_BUDGET_SECS:-2700}"
LOAD_WINDOW_SECS="${LOAD_WINDOW_SECS:-1500}"
DEADLINE_SECS="${DEADLINE_SECS:-$((BOOT_BUDGET_SECS + LOAD_WINDOW_SECS))}"
MODEL="Qwen/Qwen2.5-7B-Instruct"
MODEL_REV="a09a35458c702b33eeacc393d103063234e8bc28"
# GPU SKU is a parameter (codex 0115): the paired design is invariant
# to the substrate; only the pool scope label changes.
PROBE_GPU="${PROBE_GPU:-NVIDIA GeForce RTX 3090}"
if [ "$ARM" = "stacked" ]; then
  # The stacked arm's substrate comes ONLY from baseline's record —
  # resolved BEFORE the pool tag so the pair can never be mislabeled.
  BASE_META="/tmp/weinfer-stacked/${PAIR_ID}/baseline/arm_meta.json"
  [ -f "$BASE_META" ] || { echo "REFUSED: baseline arm_meta.json missing for pair ${PAIR_ID}" >&2; exit 1; }
  PROBE_GPU=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('gpu',''))" "$BASE_META")
  [ -n "$PROBE_GPU" ] || { echo "REFUSED: baseline arm_meta lacks gpu" >&2; exit 1; }
fi
case "$PROBE_GPU" in
  *"RTX 3090"*) POOL_TAG="3090c";;
  *"RTX 4090"*) POOL_TAG="4090c";;
  *"A6000"*)    POOL_TAG="a6000c";;
  *"A4500"*)    POOL_TAG="a4500c";;
  *) POOL_TAG=$(echo "$PROBE_GPU" | tr -dc 'A-Za-z0-9' | tr 'A-Z' 'a-z' | tail -c 8);;
esac
DB="weinfer_probe_${PAIR_ID}"
PAIR_ROOT="/tmp/weinfer-stacked/${PAIR_ID}"
RUN_DIR="${PAIR_ROOT}/${ARM}"

# Fresh-namespace enforcement (codex 0110): a retry NEVER reuses a
# pair — pool-wide aggregation would otherwise drop prior spend.
db_exists() { psql -lqt | cut -d'|' -f1 | grep -qw "$DB"; }
if [ "$ARM" = "baseline" ]; then
  if db_exists || [ -e "$PAIR_ROOT" ]; then
    echo "REFUSED: pair ${PAIR_ID} already exists (db or run root); a retry is a NEW pair id" >&2
    exit 1
  fi
else
  db_exists || { echo "REFUSED: pair DB ${DB} missing — run the baseline arm first" >&2; exit 1; }
  [ -d "${PAIR_ROOT}/baseline" ] || { echo "REFUSED: baseline run dir missing for pair ${PAIR_ID}" >&2; exit 1; }
  if [ -e "${RUN_DIR}/gateway.log" ]; then
    echo "REFUSED: stacked arm already ran for pair ${PAIR_ID}; a retry is a NEW pair id" >&2
    exit 1
  fi
fi
mkdir -p "$RUN_DIR"

KEY_FILE="../rig/scaffold/runpod_account_a.txt"
[ -f "$KEY_FILE" ] || { echo "key file missing" >&2; exit 1; }

PIN_FLAGS="--revision ${MODEL_REV} --tokenizer-revision ${MODEL_REV} --seed 0 --max-model-len 8192"
case "$ARM" in
  baseline)
    VLLM_ARGS="$PIN_FLAGS"
    WORKER_CONCURRENCY=8
    ALLOC_CONF=""
    POOL="${POOL_TAG}-${PAIR_ID}-base"
    ;;
  stacked)
    VLLM_ARGS="$PIN_FLAGS --max-num-batched-tokens 16384 --max-num-seqs 256 --gpu-memory-utilization 0.92 --enable-chunked-prefill"
    WORKER_CONCURRENCY=64
    ALLOC_CONF="expandable_segments:True"
    POOL="${POOL_TAG}-${PAIR_ID}-stack"
    ;;
  *) echo "unknown ARM=$ARM" >&2; exit 1;;
esac
ORG="org-${PAIR_ID}-${ARM}"
# Pod names are weinfer-{pool}-{intent}: the prefix is the arm's
# EXACT pool — the paired arm's pods are foreign to this watchdog.
POD_PREFIX="weinfer-${POOL}"

GATEWAY_KEY="sk-weinfer-${PAIR_ID}-${ARM}"
VERIFIER=$(python3 -c "import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest())" "$GATEWAY_KEY")

echo "== per-pair database: $DB =="
[ "$ARM" = "baseline" ] && createdb "$DB" || true

echo "== building release =="
cargo build --release -p weinfer-gateway

echo "== frozen workload (deterministic; hashed) =="
python3 - "$N_JOBS" "$MODEL" "$RUN_DIR" <<'PY'
import hashlib, json, sys
n, model, run_dir = int(sys.argv[1]), sys.argv[2], sys.argv[3]
# Product-native prefill-heavy shape: a background agent hands the
# model a large frozen context and wants a short structured verdict.
PARA = ("Background agents batch work across long horizons; the serving plane "
        "trades latency for throughput under strict cost accounting. Queue "
        "depth, chunked prefill, KV reuse, and admission control each move "
        "delivered dollars per token, and every scheduling gap is billed idle "
        "capacity. Ledger conservation requires that every micro-USD of a "
        "pod's charge lands on exactly one logical response. ")
rows = []
for i in range(n):
    # PARA*55 = exactly 3,960 post-template prompt tokens at the
    # pinned tokenizer revision (measured with the real Qwen tokenizer;
    # invariant across case ids), + 64 completion = ~4,024 logical
    # tokens per job — 300 jobs ≈ 1.21M intended vs the 1M floor.
    content = (f"Case {i:04d}. Read the operations context and answer in one "
               f"sentence: which single lever most reduces delivered cost?\n\n"
               + PARA * 55)
    rows.append({
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "max_completion_tokens": 64,
        "temperature": 0,
        "seed": 0,
    })
blob = "\n".join(json.dumps(r, sort_keys=True) for r in rows)
open(f"{run_dir}/workload.jsonl", "w").write(blob)
digest = hashlib.sha256(blob.encode()).hexdigest()
open(f"{run_dir}/workload.sha256", "w").write(digest)
print(f"workload: {n} jobs, sha256 {digest}")
PY

if [ "$ARM" = "stacked" ]; then
  # Substrate gpu was locked above; the CUDA pin locks the same way.
  CUDA_PIN=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('cuda_pin',''))" "$BASE_META")
  [ -n "$CUDA_PIN" ] || { echo "REFUSED: baseline arm_meta lacks cuda_pin" >&2; exit 1; }
  echo "stacked arm locked to baseline substrate: $PROBE_GPU @ CUDA $CUDA_PIN"
else
echo "== host CUDA discovery (exact-match pin: only versions with live machines) =="
CUDA_PIN=$(curl -sf "https://api.runpod.io/v2/catalog/gpus?include=AVAILABILITY&product=POD" \
  -K <(printf 'header = "Authorization: Bearer %s"\n' "$(tr -d '[:space:]' < "$KEY_FILE")") | TARGET="$PROBE_GPU" python3 -c '
import json, os, sys
TARGET = os.environ["TARGET"]
doc = json.load(sys.stdin)
items = doc if isinstance(doc, list) else doc.get("gpus") or doc.get("items") or []
for g in items:
    if g.get("id") == TARGET:
        vs = sorted(c["version"] for c in g.get("cudaVersions", [])
                    if c.get("available") and c["version"].startswith("12."))
        # ONE version, deterministically (newest available): a multi-
        # version exact-match filter amplifies the create race — every
        # listed version must still have machines at create time.
        print(vs[-1] if vs else ""); break
')
[ -n "$CUDA_PIN" ] || { echo "REFUSED: no 12.x community hosts for $PROBE_GPU right now" >&2; exit 1; }
echo "pinning host CUDA: $CUDA_PIN (single newest available; recorded in arm_meta)"
fi

echo "== outward tunnel =="
cloudflared tunnel --url http://127.0.0.1:8080 --no-autoupdate \
  > "$RUN_DIR/tunnel.log" 2>&1 &
TUNNEL_PID=$!
PUBLIC_BASE=""
for _ in $(seq 1 60); do
  PUBLIC_BASE=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$RUN_DIR/tunnel.log" | head -1 || true)
  [ -n "$PUBLIC_BASE" ] && break
  sleep 1
done
[ -n "$PUBLIC_BASE" ] || { echo "tunnel URL never appeared" >&2; kill $TUNNEL_PID; exit 1; }
echo "tunnel: $PUBLIC_BASE"

echo "== launching managed gateway (arm: $ARM, pool: $POOL) =="
LAUNCH_EPOCH=$(date +%s)
# Subshell with EXPLICIT exports: ${X:+VAR=y} expands to a command
# WORD, never an assignment (see scripts/probe_env_test.sh) — the
# stacked arm's allocator config must be exported for real, and the
# baseline arm must have it genuinely UNSET.
(
  export WEINFER_MANAGED=1
  # Externally managed one-shot probe: the residency policy must not
  # gate its deterministic single boot.
  export WEINFER_RESIDENCY=0
  export WEINFER_RUNPOD_API_KEY="$(tr -d '[:space:]' < "$KEY_FILE")"
  export WEINFER_API_KEYS="${ORG}:key-${ARM}:${VERIFIER}"
  export WEINFER_BACKEND_URL="http://127.0.0.1:9"
  export WEINFER_DATABASE_URL="postgres://localhost/${DB}"
  export WEINFER_POOL="$POOL"
  export WEINFER_GPU_TYPE="$PROBE_GPU"
  export WEINFER_CLOUD="COMMUNITY"
  export WEINFER_CUDA_VERSIONS="$CUDA_PIN"
  export WEINFER_IMAGE="${WEINFER_IMAGE:-ghcr.io/jonathanrosado/weinfer-pod@sha256:160a926826565b1ed0134335f3f68e65ed457fcb034058639fc5c9b5c7ec2613}"
  export WEINFER_POD_ARGS=""
  export WEINFER_POD_DISK_GB=60
  export WEINFER_POD_HTTP_PORT=8000
  export WEINFER_SERVED_MODEL="$MODEL"
  export WEINFER_PUBLIC_BASE="$PUBLIC_BASE"
  export WEINFER_WORKER_URL="https://github.com/JonathanRosado/weinfer-pod/releases/download/worker-v0.4.0/weinfer-worker"
  export WEINFER_WORKER_SHA256="7bd6f06f07f68afb24bbd8fec086bf3be04d574ebe5a86791e9f2c230cca5f6b"
  export VLLM_EXTRA_ARGS="$VLLM_ARGS"
  export WEINFER_CONCURRENCY="$WORKER_CONCURRENCY"
  if [ -n "$ALLOC_CONF" ]; then
    export PYTORCH_CUDA_ALLOC_CONF="$ALLOC_CONF"
  else
    unset PYTORCH_CUDA_ALLOC_CONF
  fi
  # ONE-SHOT: a pod that fails its boot budget fails the window; no
  # refill may burn the remaining time on a doomed second cold boot.
  export WEINFER_PROVISION_ATTEMPTS=1
  export WEINFER_PROBE_BUDGET=$((BOOT_BUDGET_SECS / 10))
  export WEINFER_PROBE_DELAY_SECS=10
  export WEINFER_LISTEN="127.0.0.1:8080"
  exec ./target/release/weinfer-gateway
) > "$RUN_DIR/gateway.log" 2>&1 &
GATEWAY_PID=$!
trap 'kill -INT $GATEWAY_PID 2>/dev/null || true; wait $GATEWAY_PID 2>/dev/null || true; kill $TUNNEL_PID 2>/dev/null || true' EXIT

# Watchdog: THIS run's pod prefix + the PRE-LAUNCH epoch (cost clock
# covers provisioning, not just post-discovery).
WEINFER_KEY_FILE="$KEY_FILE" WATCHDOG_LOG_DIR="$RUN_DIR" \
WATCHDOG_CAP_SECONDS=$((BOOT_BUDGET_SECS + LOAD_WINDOW_SECS + 300)) \
  bash scripts/traversal_watchdog.sh $GATEWAY_PID "$POD_PREFIX" "$LAUNCH_EPOCH" \
  > "$RUN_DIR/watchdog.stdout" 2>&1 &
WATCHDOG_PID=$!

echo "== waiting for the control plane, then queueing ALL jobs BEFORE the pod =="
until curl -sf http://127.0.0.1:8080/healthz >/dev/null 2>&1; do
  kill -0 $GATEWAY_PID 2>/dev/null || { echo "gateway exited before listening" >&2; exit 1; }
  sleep 2
done
echo "control plane up; queueing ${N_JOBS} durable jobs (demand-led: the pod boots against known work)"

# set -e must NOT skip the drain on a failed load: capture the status,
# drain unconditionally, THEN fail closed.
LOAD_STATUS=0
python3 - "$DEADLINE_SECS" "$GATEWAY_KEY" "$PAIR_ID" "$ARM" "$RUN_DIR" <<'PY' || LOAD_STATUS=$?
import json, sys, time, urllib.request
deadline, key, pair_id, arm, run_dir = sys.argv[1:6]
base = "http://127.0.0.1:8080"
rows = [json.loads(l) for l in open(f"{run_dir}/workload.jsonl")]
ids = []
t0 = time.time()
for i, row in enumerate(rows):
    body = json.dumps(row).encode()
    req = urllib.request.Request(base + "/v1/jobs", data=body, method="POST", headers={
        "Authorization": f"Bearer {key}",
        "content-type": "application/json",
        "x-weinfer-deadline-seconds": deadline,
        "Idempotency-Key": f"{pair_id}-{arm}-{i:04d}",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        assert resp.status == 202, resp.status
        ids.append(json.load(resp)["job_id"])
print(f"submitted {len(ids)} jobs in {time.time()-t0:.1f}s", flush=True)
terminal = {}
while len(terminal) < len(ids):
    for jid in ids:
        if jid in terminal:
            continue
        req = urllib.request.Request(f"{base}/v1/jobs/{jid}",
                                     headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            doc = json.load(resp)
        if doc.get("status") in ("completed", "failed", "expired"):
            terminal[jid] = doc["status"]
    print(f"progress: {len(terminal)}/{len(ids)} terminal, {time.time()-t0:.0f}s", flush=True)
    if len(terminal) < len(ids):
        time.sleep(10)
elapsed = time.time() - t0
counts = {}
for status in terminal.values():
    counts[status] = counts.get(status, 0) + 1
json.dump({"elapsed_secs": elapsed, "counts": counts, "intended": len(ids)},
          open(f"{run_dir}/load_result.json", "w"))
print(f"ALL TERMINAL in {elapsed:.0f}s: {counts}")
if counts.get("completed", 0) != len(ids):
    sys.exit(f"FAIL CLOSED: {len(ids)-counts.get('completed',0)} jobs did not complete")
PY

echo "== drain immediately =="
kill -INT $GATEWAY_PID
wait $GATEWAY_PID || true
trap 'kill $TUNNEL_PID 2>/dev/null || true' EXIT
WATCHDOG_STATUS=0
wait $WATCHDOG_PID || WATCHDOG_STATUS=$?
tail -3 "$RUN_DIR/watchdog.log" || true

# The arm is ACCEPTED only if the load completed, the watchdog verdict
# is clean (zero-live proven; provider list never failed closed), and
# every pod in this arm's pool is durably OFF the potentially-live set.
LIVE=$(psql -t -A "$DB" -c "SELECT COUNT(*) FROM managed_pods WHERE pool='$POOL' AND state IN ('intent','created','ready','draining','terminate_requested');")
ARM_STATUS="accepted"
[ "$LOAD_STATUS" = "0" ] || ARM_STATUS="failed:incomplete-jobs"
[ "$WATCHDOG_STATUS" = "0" ] || ARM_STATUS="failed:watchdog-$WATCHDOG_STATUS"
[ "$LIVE" = "0" ] || ARM_STATUS="failed:still-live-$LIVE"
CUDA_PIN_RECORD="$CUDA_PIN" PROBE_GPU_RECORD="$PROBE_GPU" python3 - "$PAIR_ID" "$ARM" "$POOL" "$ARM_STATUS" "$RUN_DIR" <<'PYEOF'
import json, sys
pair_id, arm, pool, status, run_dir = sys.argv[1:6]
json.dump({"pair_id": pair_id, "arm": arm, "pool": pool, "arm_status": status,
           "cuda_pin": __import__("os").environ.get("CUDA_PIN_RECORD", ""),
           "gpu": __import__("os").environ.get("PROBE_GPU_RECORD", ""),
           "workload_sha256": open(f"{run_dir}/workload.sha256").read().strip()},
          open(f"{run_dir}/arm_meta.json", "w"), indent=1)
PYEOF
if [ "$ARM_STATUS" != "accepted" ]; then
  echo "ARM $ARM REJECTED ($ARM_STATUS) — this pair is DEAD; start a NEW pair id" >&2
  exit 1
fi
echo "ARM $ARM accepted through durable termination (pool $POOL)."
echo "Billing settles asynchronously: run scripts/stacked_collect.sh $PAIR_ID (free, idempotent), then scripts/stacked_compare.sh $PAIR_ID."
