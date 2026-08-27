#!/usr/bin/env bash
# Deploy the PERSISTENT WeInfer control plane: one cheap RunPod CPU
# pod running the pinned control-plane image (durable Postgres on a
# network volume + the sha-pinned gateway), public HTTPS at the
# provider proxy, owning a managed GPU pool through the cost planner.
#
# COST WHEN RUN: CPU pod cents/hour + network volume cents/GB-month.
# The launch ceiling is IN CODE below: health deadline, direct pod
# DELETE on any failure, zero-live verification, and an explicit
# ongoing burn ceiling printed for the record.  The volume is the one
# deliberate durable asset and is kept on failure.
#
# TRUST ROOTS ARE COMMITTED CONSTANTS (codex 0162): image digest and
# both binary sha256 values are pinned here; remote release metadata
# is used only as a CONSISTENCY CHECK, never as the authority.
set -euo pipefail
cd "$(dirname "$0")/.."

# --render-env: print the EXACT pod env JSON and exit — no provider
# calls, no credential writes.  The managed CI regression consumes
# this so the environment it proves is the environment we deploy.
RENDER_ONLY=0
[ "${1:-}" = "--render-env" ] && RENDER_ONLY=1

# Retention-policy arm selector.  Production defaults to the shipped
# demand-aware policy (three complete quiet cycles).  Paid comparisons may
# raise this value so the effective quiet window caps at one-boot parity,
# producing a same-binary parity control.  Validate before trust-root or
# provider calls: an arm typo must spend nothing.
DEMAND_QUIET_CYCLES="${WEINFER_DEMAND_QUIET_CYCLES:-3}"
python3 - "$DEMAND_QUIET_CYCLES" <<'PY'
import sys
raw = sys.argv[1]
if not raw.isascii() or not raw.isdigit() or int(raw) <= 0:
    raise SystemExit(
        f"WEINFER_DEMAND_QUIET_CYCLES must be a positive base-10 integer, got {raw!r}"
    )
PY

KEY_FILE="../rig/scaffold/runpod_account_a.txt"
DEPLOY_TEST="${WEINFER_DEPLOY_TEST:-0}"
if [ "$RENDER_ONLY" = "0" ] && [ "$DEPLOY_TEST" = "0" ]; then
  [ -f "$KEY_FILE" ] || { echo "key file missing" >&2; exit 1; }
fi
API="https://rest.runpod.io/v1"
if [ "$DEPLOY_TEST" = "1" ]; then
  # TEST MODE (codex 0165: the destructive tail must be executable
  # against a fake): provider + health bases point at the harness.
  # Never set in production; the banner makes accidental use loud.
  API="${WEINFER_DEPLOY_API_BASE:?test mode requires WEINFER_DEPLOY_API_BASE}"
  echo "### DEPLOY TEST MODE: provider=${API} — NO REAL RESOURCES ###" >&2
fi
read_provider_key() {
  # Render and test modes NEVER read the real key — even when the
  # file exists.  The rendered JSON is written to disk/CI logs; the
  # provider key must never appear anywhere but live pod env +
  # provider calls.
  if [ "$RENDER_ONLY" = "1" ] || [ "$DEPLOY_TEST" = "1" ]; then
    printf 'ci-fake-provider-key'
  else
    tr -d '[:space:]' < "$KEY_FILE"
  fi
}

# ---------- pinned trust roots (verify remote against THESE) ----------
CP_IMAGE="ghcr.io/jonathanrosado/weinfer-controlplane@sha256:693db10834a098d0267949098edc334593c5e418c3f8e6b5b944ee41d5b741de"
GW_TAG="gateway-v0.17.0"
GW_SHA="3ff9bc7de2b3654a9c75376b7168cd2f4461108bef7585faa27327d4c4b9f397"
WORKER_TAG="worker-v0.6.0"
WORKER_SHA="0d9b0be9c2a756716a5630966172c32f199e4387c7ee57bf8cb4ccc69f7354fe"
POD_IMAGE="ghcr.io/jonathanrosado/weinfer-pod@sha256:160a926826565b1ed0134335f3f68e65ed457fcb034058639fc5c9b5c7ec2613"
QWEN_REV="a09a35458c702b33eeacc393d103063234e8bc28"

GW_URL="https://github.com/JonathanRosado/weinfer-pod/releases/download/${GW_TAG}/weinfer-gateway"
WORKER_URL="https://github.com/JonathanRosado/weinfer-pod/releases/download/${WORKER_TAG}/weinfer-worker"

# Consistency check only — a sidecar mismatch means the release was
# tampered with or republished; refuse to proceed either way.
for pair in "$GW_URL.sha256:$GW_SHA" "$WORKER_URL.sha256:$WORKER_SHA"; do
  url="${pair%:*}"; pinned="${pair##*:}"
  remote="$(curl -fsSL "$url" | awk '{print $1}')"
  [ "$remote" = "$pinned" ] || {
    echo "TRUST ROOT MISMATCH for $url: remote $remote, pinned $pinned" >&2
    exit 1
  }
done
echo "trust roots consistent (gateway ${GW_SHA:0:12}…, worker ${WORKER_SHA:0:12}…)" >&2

# ---------- burn ceiling ----------
CEILING_CPU_USD_HR="0.10"     # refuse any CPU flavor above this
VOLUME_GB=10                  # ~$0.70/month at the documented $0.07/GB-mo
HEALTH_DEADLINE_SECS="${WEINFER_HEALTH_DEADLINE_SECS:-900}"  # /healthz within 15 min or we delete

sha() { python3 -c "import hashlib,sys;print(hashlib.sha256(sys.argv[1].encode()).hexdigest())" "$1"; }
rand() { python3 -c "import secrets;print(secrets.token_urlsafe(24))"; }

# CI presets these in --render-env mode; live deploys generate fresh.
ADMIN_KEY="${ADMIN_KEY:-wf-admin-$(rand)}"
CUSTOMER_KEY="${CUSTOMER_KEY:-wf-live-$(rand)}"
WORKER_KEY="${WORKER_KEY:-wf-worker-$(rand)}"

rp() { # method path [json]
  local method="$1" path="$2" body="${3:-}" tmp code rc detail secret provider_secret
  tmp=$(mktemp "${TMPDIR:-/tmp}/weinfer-runpod-response.XXXXXX")
  if [ -n "$body" ]; then
    set +e
    code=$(curl -sS --connect-timeout 10 --max-time 60 --max-filesize 1048576 \
      -o "$tmp" -w '%{http_code}' -X "$method" "$API$path" \
      -H "Authorization: Bearer $(read_provider_key)" \
      -H "Content-Type: application/json" -d "$body")
    rc=$?
    set -e
  else
    set +e
    code=$(curl -sS --connect-timeout 10 --max-time 60 --max-filesize 1048576 \
      -o "$tmp" -w '%{http_code}' -X "$method" "$API$path" \
      -H "Authorization: Bearer $(read_provider_key)")
    rc=$?
    set -e
  fi
  if [ "$rc" != "0" ]; then
    rm -f "$tmp"
    echo "RunPod ${method} ${path} transport failure (curl ${rc})" >&2
    return "$rc"
  fi
  case "$code" in
    2??) cat "$tmp"; rm -f "$tmp"; return 0 ;;
  esac
  # Extract only a bounded provider-authored error string. Never emit
  # the response object itself: a provider could reflect the create
  # body, which contains live launch credentials.
  detail=$(python3 - "$tmp" <<'PY'
import json, sys
try:
    value = json.load(open(sys.argv[1]))
except Exception:
    value = None
detail = None
if isinstance(value, dict):
    for key in ("error", "message", "detail"):
        candidate = value.get(key)
        if isinstance(candidate, str):
            detail = candidate
            break
        if isinstance(candidate, dict):
            for nested in ("message", "detail", "code"):
                if isinstance(candidate.get(nested), str):
                    detail = candidate[nested]
                    break
        if detail:
            break
if not detail:
    detail = "response detail unavailable"
print(" ".join(detail.split())[:160])
PY
)
  provider_secret=$(read_provider_key)
  for secret in "$ADMIN_KEY" "$CUSTOMER_KEY" "$WORKER_KEY" "$provider_secret"; do
    if [ -n "$secret" ]; then detail="${detail//$secret/[redacted]}"; fi
  done
  rm -f "$tmp"
  echo "RunPod ${method} ${path} HTTP ${code}: ${detail}" >&2
  return 22
}

# ---------- serving bytes + explicit UNMEASURED hardware queue ----------
# The engine bytes equal the sealed stacked launch, but the current
# worker is a different executable identity.  Therefore NO SKU below
# carries promoted throughput/boot economics.  At the moment demand requires a
# pod, the gateway reads the live provider catalog, filters this exact
# hardware allow-list by cloud/VRAM/rate/CUDA, and ranks admitted rows
# by an explicit hypothesis-only boot+serve+drain cost at the live
# backlog.  Throughput and fixed-cost priors are independent and neither
# can affect admission, deadline feasibility, promotion, or a delivered-cost
# claim.  Catalog presence is only a hint:
# a definitive create denial falls through to the next row in the
# same plan.  A SKU earns delivered-cost facts only from its own later
# sealed traversal.
SERVED_MODEL="Qwen/Qwen2.5-7B-Instruct"
MAX_CTX=8192
VLLM_EXTRA_ARGS="--seed 0 --max-num-batched-tokens 16384 --max-num-seqs 256 --gpu-memory-utilization 0.92 --enable-chunked-prefill --enable-prefix-caching"
CONCURRENCY="64"
ALLOC_CONF="expandable_segments:True"
# Keep the JSON on one physical line: --render-env is also consumed as a
# Docker env-file by the release smoke, whose format does not permit embedded
# newlines in values.
DEFAULT_BOOTSTRAP_HARDWARE='[{"gpu_sku":"NVIDIA RTX A5000","cuda_class":"12","vram_gb":24,"throughput_seed_tokens_per_sec":4000,"throughput_seed_kind":"policy_prior","throughput_seed_source":"bootstrap-policy-v1; no traffic observation","boot_seed_micros":664034722,"drain_seed_micros":731031,"fixed_seed_kind":"policy_prior","fixed_seed_source":"max sealed activation plus max sealed drain across A4500, SFF Ada, and RTX 4090; no SKU traffic observation"},{"gpu_sku":"NVIDIA RTX 4000 SFF Ada Generation","cuda_class":"12","vram_gb":20,"throughput_seed_tokens_per_sec":2681,"throughput_seed_kind":"traffic_observed_cross_identity","throughput_seed_source":"sealed amort3full-1787755326; tps_low=2681; basis=ready_to_batch1_completion_rederived_from_sealed_phases; workload_sha256=2392bb588923e88dc3f1473a9393a0e099a19a4889ccbd1d945b33df9e5ed205; candidate_only 1/5 boots","boot_seed_micros":492992942,"drain_seed_micros":685232,"fixed_seed_kind":"traffic_observed_cross_identity","fixed_seed_source":"sealed amort3full-1787755326; activation=492992942; drain=685232; candidate_only 1/5 boots"},{"gpu_sku":"NVIDIA RTX A4500","cuda_class":"12","vram_gb":20,"throughput_seed_tokens_per_sec":4161,"throughput_seed_kind":"traffic_observed_cross_identity","throughput_seed_source":"sealed batch-live-1787630415; tps_low=4161; basis=ready_window_tps_low; workload_sha256=2392bb588923e88dc3f1473a9393a0e099a19a4889ccbd1d945b33df9e5ed205; candidate_only 2/5 boots","boot_seed_micros":429080126,"drain_seed_micros":731031,"fixed_seed_kind":"traffic_observed_cross_identity","fixed_seed_source":"sealed batch-live-1787630415; boot_high=429080126; drain=731031; candidate_only 2/5 boots"},{"gpu_sku":"NVIDIA RTX 4000 Ada Generation","cuda_class":"12","vram_gb":20,"throughput_seed_tokens_per_sec":4000,"throughput_seed_kind":"policy_prior","throughput_seed_source":"bootstrap-policy-v1; no traffic observation","boot_seed_micros":664034722,"drain_seed_micros":731031,"fixed_seed_kind":"policy_prior","fixed_seed_source":"max sealed activation plus max sealed drain across A4500, SFF Ada, and RTX 4090; no SKU traffic observation"},{"gpu_sku":"NVIDIA GeForce RTX 3090","cuda_class":"12","vram_gb":24,"throughput_seed_tokens_per_sec":6000,"throughput_seed_kind":"spec_derived","throughput_seed_source":"analytic-v1 FP16-compute extrapolation from sealed A4500 anchor; no traffic observation","boot_seed_micros":664034722,"drain_seed_micros":731031,"fixed_seed_kind":"policy_prior","fixed_seed_source":"max sealed activation plus max sealed drain across A4500, SFF Ada, and RTX 4090; no SKU traffic observation"},{"gpu_sku":"NVIDIA GeForce RTX 3090 Ti","cuda_class":"12","vram_gb":24,"throughput_seed_tokens_per_sec":6700,"throughput_seed_kind":"spec_derived","throughput_seed_source":"analytic-v1 FP16-compute extrapolation from sealed A4500 anchor; no traffic observation","boot_seed_micros":664034722,"drain_seed_micros":731031,"fixed_seed_kind":"policy_prior","fixed_seed_source":"max sealed activation plus max sealed drain across A4500, SFF Ada, and RTX 4090; no SKU traffic observation"},{"gpu_sku":"NVIDIA RTX A6000","cuda_class":"12","vram_gb":48,"throughput_seed_tokens_per_sec":6500,"throughput_seed_kind":"spec_derived","throughput_seed_source":"analytic-v1 FP16-compute extrapolation from sealed A4500 anchor; no traffic observation","boot_seed_micros":664034722,"drain_seed_micros":731031,"fixed_seed_kind":"policy_prior","fixed_seed_source":"max sealed activation plus max sealed drain across A4500, SFF Ada, and RTX 4090; no SKU traffic observation"},{"gpu_sku":"NVIDIA GeForce RTX 4090","cuda_class":"12","vram_gb":24,"throughput_seed_tokens_per_sec":9548,"throughput_seed_kind":"traffic_observed_cross_identity","throughput_seed_source":"sealed seed4090-1787834610 profile candidate; tps_low=9548; basis=ready_window_tps_low; workload_sha256=2392bb588923e88dc3f1473a9393a0e099a19a4889ccbd1d945b33df9e5ed205; candidate_only 1/5 boots","boot_seed_micros":664034722,"drain_seed_micros":633859,"fixed_seed_kind":"traffic_observed_cross_identity","fixed_seed_source":"sealed seed4090-1787834610 profile candidate; activation=664034722; drain=633859; candidate_only 1/5 boots"},{"gpu_sku":"NVIDIA A40","cuda_class":"12","vram_gb":48,"throughput_seed_tokens_per_sec":4000,"throughput_seed_kind":"policy_prior","throughput_seed_source":"bootstrap-policy-v1; no traffic observation","boot_seed_micros":664034722,"drain_seed_micros":731031,"fixed_seed_kind":"policy_prior","fixed_seed_source":"max sealed activation plus max sealed drain across A4500, SFF Ada, and RTX 4090; no SKU traffic observation"}]'
# A paid exact-identity observation may narrow the default queue to ONE known
# row. This is an experiment substrate selector, not a planner fact: it keeps
# the row's typed prior and provenance byte-for-byte, and cannot add or mutate
# identities. An unknown name refuses before any provider-side create.
BOOTSTRAP_ONLY_GPU_SKU="${WEINFER_BOOTSTRAP_ONLY_GPU_SKU:-}"
if [ -n "$BOOTSTRAP_ONLY_GPU_SKU" ]; then
  BOOTSTRAP_HARDWARE=$(python3 - "$DEFAULT_BOOTSTRAP_HARDWARE" "$BOOTSTRAP_ONLY_GPU_SKU" <<'PY'
import json, sys
rows = json.loads(sys.argv[1])
selected = [row for row in rows if row.get("gpu_sku") == sys.argv[2]]
if len(selected) != 1:
    raise SystemExit(
        f"WEINFER_BOOTSTRAP_ONLY_GPU_SKU must name exactly one configured identity: {sys.argv[2]!r}"
    )
print(json.dumps(selected, separators=(",", ":")))
PY
  )
else
  BOOTSTRAP_HARDWARE="$DEFAULT_BOOTSTRAP_HARDWARE"
fi

# NO PLACEMENT PROFILES (codex 0164): the paid pair measured the
# worker-v0.1.0 identity; production runs worker-v0.6.0 — a DIFFERENT
# exact identity by the launch-contract digest's own authority.  The
# available SKU therefore runs the explicit unmeasured-bootstrap path
# with per-attempt live CUDA pinning, shared exact engine bytes, and
# the STATIC context authority.  Its own immutable lifecycle/usage
# records become that SKU's first current-worker measurement.  The old
# pair remains Conservative historical evidence only.

# ---------- priced customer catalog ----------
CATALOG='{"revision":"cat-live-1","models":[{"id":"Qwen/Qwen2.5-7B-Instruct","input_price_micro_per_mtok":100000,"output_price_micro_per_mtok":400000,"capabilities":["chat"],"created":1787432264,"context_length":8192}]}'

# ---------- assemble pod env (argv-passed: no quoting drift) ----------
ENV_JSON=$(APIKEYS="org-live:key-live:$(sha "$CUSTOMER_KEY")" \
  ADMIN_VER="$(sha "$ADMIN_KEY")" \
  WORKER_RING="pod-live:$(sha "$WORKER_KEY")" \
  RP_KEY="$(read_provider_key)" \
  GW_URL="$GW_URL" GW_SHA="$GW_SHA" WORKER_URL="$WORKER_URL" \
  WORKER_SHA="$WORKER_SHA" POD_IMAGE="$POD_IMAGE" \
  SERVED_MODEL="$SERVED_MODEL" QWEN_REV="$QWEN_REV" CATALOG="$CATALOG" \
  MAX_CTX="$MAX_CTX" VLLM_EXTRA_ARGS="$VLLM_EXTRA_ARGS" \
  CONCURRENCY="$CONCURRENCY" ALLOC_CONF="$ALLOC_CONF" \
  BOOTSTRAP_HARDWARE="$BOOTSTRAP_HARDWARE" \
  DEMAND_QUIET_CYCLES="$DEMAND_QUIET_CYCLES" \
  python3 - <<'PY'
import json, os
e = os.environ
env = {
    "WEINFER_GATEWAY_URL": e["GW_URL"],
    "WEINFER_GATEWAY_SHA256": e["GW_SHA"],
    "WEINFER_API_KEYS": e["APIKEYS"],
    "WEINFER_ADMIN_KEY_SHA256": e["ADMIN_VER"],
    "WEINFER_BACKEND_URL": "http://127.0.0.1:9",
    "WEINFER_LISTEN": "0.0.0.0:8080",
    # WEINFER_PUBLIC_BASE derived in-container from RUNPOD_POD_ID.
    "WEINFER_MANAGED": "1",
    "WEINFER_RESIDENCY": "1",
    # Demand-aware retention defaults to three complete 60s empty-horizon
    # observations.  The registered A/B control may raise this value until
    # the gateway's one-boot parity cap becomes the effective TTL.
    "WEINFER_DEMAND_QUIET_CYCLES": e["DEMAND_QUIET_CYCLES"],
    "WEINFER_RUNPOD_API_KEY": e["RP_KEY"],
    "WEINFER_WORKER_KEYS": e["WORKER_RING"],
    "WEINFER_WORKER_URL": e["WORKER_URL"],
    "WEINFER_WORKER_SHA256": e["WORKER_SHA"],
    "WEINFER_POOL": "community-qwen7b-0",
    # Required legacy spec field; every bootstrap create overrides it
    # with the selected exact SKU + live CUDA pin.
    "WEINFER_GPU_TYPE": "NVIDIA RTX A4500",
    "WEINFER_CLOUD": "COMMUNITY",
    "WEINFER_IMAGE": e["POD_IMAGE"],
    "WEINFER_SERVED_MODEL": e["SERVED_MODEL"],
    "WEINFER_MODEL_REVISION": e["QWEN_REV"],
    "WEINFER_TOKENIZER_REVISION": e["QWEN_REV"],
    "WEINFER_MODEL_CATALOG": e["CATALOG"],
    # STATIC context authority (unmeasured bootstrap path, no profiles):
    # the catalog may never sell context the engine cannot execute.
    "WEINFER_BACKEND_MAX_CONTEXT": e["MAX_CTX"],
    "WEINFER_BOOTSTRAP_MODE": "1",
    "WEINFER_BOOTSTRAP_HARDWARE": e["BOOTSTRAP_HARDWARE"],
    # Immutable ordinary-traffic profile provenance: the gateway
    # canonicalizes revision/context into the EXECUTED argv and stamps
    # the complete secret-free launch contract before provider create.
    "WEINFER_PROFILE_EVIDENCE": "1",
    # Aggregation release margin: the default 2s models only the
    # worker poll gap, so an underfilled batch releases at the razor
    # edge of its deadline and can EXPIRE at grant time (observed in
    # the CI worker e2e: one job released at deadline-2s and missed).
    # 30s covers poll cadence + grant latency honestly.
    "WEINFER_POLL_MARGIN_SECS": "30",
    # The GPU create-rate bound (v0.4.0): creation itself refuses
    # missing/malformed/over-bound rates, making the watchdog's
    # wall-clock cap a HARD cap (same value the watchdog assumes).
    "WEINFER_MAX_GPU_RATE": "0.40",
    "VLLM_EXTRA_ARGS": e["VLLM_EXTRA_ARGS"],
    "WEINFER_CONCURRENCY": e["CONCURRENCY"],
    "PYTORCH_CUDA_ALLOC_CONF": e["ALLOC_CONF"],
    "WEINFER_POD_DISK_GB": "40",
    "WEINFER_POD_HTTP_PORT": "8000",
}
print(json.dumps(env))
PY
)

if [ "$RENDER_ONLY" = "1" ]; then
  printf '%s\n' "$ENV_JSON"
  exit 0
fi

# ---------- credentials handoff: 0600 file, NEVER echoed ----------
CRED_DIR="${HOME}/.weinfer"
mkdir -p "$CRED_DIR"; chmod 700 "$CRED_DIR"
CRED_FILE="${CRED_DIR}/controlplane-credentials-$(date +%s).env"
umask 077
cat > "$CRED_FILE" <<EOF
WEINFER_ADMIN_KEY=${ADMIN_KEY}
WEINFER_CUSTOMER_KEY=${CUSTOMER_KEY}   # org-live/key-live
WEINFER_WORKER_KEY=${WORKER_KEY}       # pod-live
EOF
chmod 600 "$CRED_FILE"
echo "credentials written ONCE to ${CRED_FILE} (mode 0600)"
echo "  fingerprints: admin $(sha "$ADMIN_KEY" | cut -c1-12)… customer $(sha "$CUSTOMER_KEY" | cut -c1-12)… worker $(sha "$WORKER_KEY" | cut -c1-12)…"

# ---------- network volume (the deliberate durable asset) ----------
echo "== network volume =="
VOL_ID="${VOLUME_ID:-}"
if [ -z "$VOL_ID" ]; then
  VOL_ID=$(rp GET /networkvolumes | python3 -c "
import json,sys
vols=[v for v in json.load(sys.stdin) if v.get('name')=='weinfer-controlplane']
print(vols[0]['id'] if vols else '')")
fi
if [ -z "$VOL_ID" ]; then
  VOL_ID=$(rp POST /networkvolumes \
    '{"name":"weinfer-controlplane","size":'"$VOLUME_GB"',"dataCenterId":"'"${DATACENTER:-EU-RO-1}"'"}' \
    | python3 -c "import json,sys;print(json.load(sys.stdin)['id'])")
  echo "created volume $VOL_ID (${VOLUME_GB}GB)"
else
  echo "reusing volume $VOL_ID"
fi

# ---------- create the pod: FAIL-CLOSED launch cap (codex 0163) ----------
# Unique launch identity + EXIT trap installed BEFORE the create side
# effect.  Any failure after this point deletes the exact pod (found
# by id, or discovered by this launch's UNIQUE name on an ambiguous
# create), verifies it is gone, and verifies zero live pods — a
# verification that cannot run is a FAILURE, never a shrug.
PRE_CREATE_EPOCH=$(date +%s)
CP_NAME="weinfer-controlplane-${PRE_CREATE_EPOCH}-$(python3 -c "import secrets;print(secrets.token_hex(4))")"
POD_ID=""
LAUNCH_OK=0

verify_pod_gone() { # pod_id -> 0 iff provider says 404/terminated
  local pod_id="$1" body code
  body=$(curl -sS --connect-timeout 10 --max-time 30 -w '\n%{http_code}' "$API/pods/${pod_id}" \
    -H "Authorization: Bearer $(read_provider_key)") || return 1
  code="${body##*$'\n'}"
  if [ "$code" = "404" ]; then return 0; fi
  [ "$code" = "200" ] || return 1
  # EXITED means STOPPED (machine still assigned) in the official v2
  # schema — only TERMINATED or 404 count as gone (codex 0165).
  printf '%s' "${body%$'\n'*}" | python3 -c "
import json,sys
pod=json.load(sys.stdin)
status=pod.get('status') or pod.get('desiredStatus') or ''
sys.exit(0 if status == 'TERMINATED' else 1)"
}

delete_pod_verified() { # pod_id -> 0 iff deleted AND verified gone
  local pod_id="$1"
  for attempt in 1 2 3; do
    curl -sS --connect-timeout 10 --max-time 30 -X DELETE "$API/pods/${pod_id}" \
      -H "Authorization: Bearer $(read_provider_key)" >/dev/null 2>&1 || true
    sleep 2
    if verify_pod_gone "$pod_id"; then return 0; fi
    sleep $((attempt * 3))
  done
  return 1
}

# The resource may be LIVE and the provider unreadable: persist an
# independently executable cleanup state and NEVER claim clean
# (codex 0166: a loud failure is not a spend ceiling).
persist_unresolved() {
  local why="$1"
  local state_file="${HOME}/.weinfer/unresolved-launch-${CP_NAME}.json"
  mkdir -p "$(dirname "$state_file")"
  printf '{"name":"%s","api":"%s","epoch":%s,"pod_id":"%s"}\n' \
    "$CP_NAME" "$API" "$PRE_CREATE_EPOCH" "${POD_ID:-}" > "$state_file"
  echo "CLEANUP UNRESOLVED: ${why}; the pod may be LIVE and billing" >&2
  echo "state persisted: ${state_file}" >&2
  echo "finish with: scripts/deploy_cleanup_resume.sh ${state_file}" >&2
}

on_exit() {
  local code=$?
  if [ "$LAUNCH_OK" = "1" ]; then exit "$code"; fi
  # errexit would abort this trap on any failing guard — the cleanup
  # itself must never be skippable.
  set +e
  echo "LAUNCH FAILED — cleaning up (volume kept as the durable asset)" >&2
  # Resolve the pod id: captured, or discovered by this launch's
  # unique name (the ambiguous-create path).
  if [ -z "$POD_ID" ]; then
    # Provider eventual consistency + OUTAGE RECOVERY (codex 0166): a
    # list failure retries loudly and VOIDS the quiescence count —
    # only successful EMPTY reads count toward "nothing was created".
    EMPTY_READS=0
    ATTEMPTS=0
    while [ "$EMPTY_READS" -lt 6 ] && [ "$ATTEMPTS" -lt 30 ]; do
      ATTEMPTS=$((ATTEMPTS + 1))
      if LISTING=$(rp GET /pods 2>/dev/null); then
        FOUND=$(printf '%s' "$LISTING" | python3 -c "
import json,sys
pods=json.load(sys.stdin)
pods=pods.get('pods', pods) if isinstance(pods, dict) else pods
match=[p for p in pods if p.get('name')=='${CP_NAME}']
print(match[0]['id'] if match else '')" 2>/dev/null)
        if [ -n "$FOUND" ]; then POD_ID="$FOUND"; break; fi
        EMPTY_READS=$((EMPTY_READS + 1))
      else
        echo "cleanup: provider list FAILED (attempt ${ATTEMPTS}) — retrying, outage voids quiescence" >&2
        EMPTY_READS=0
      fi
      sleep 5
    done
    if [ -z "$POD_ID" ] && [ "$EMPTY_READS" -lt 6 ]; then
      persist_unresolved "provider listing never recovered during ambiguous-create discovery"
      exit 1
    fi
  fi
  if [ -n "$POD_ID" ]; then
    delete_pod_verified "$POD_ID" || {
      persist_unresolved "pod ${POD_ID} could not be verified gone"
      exit 1
    }
    echo "pod ${POD_ID} deleted and verified gone" >&2
  fi
  # Zero-live verification, FAIL-CLOSED: an unreadable list is a
  # failure, never a question mark.
  LIVE=$(rp GET /pods | python3 -c "
import json,sys
pods=json.load(sys.stdin)
pods=pods.get('pods', pods) if isinstance(pods, dict) else pods
print(sum(1 for p in pods if (p.get('status') or p.get('desiredStatus')) != 'TERMINATED'))") || {
    persist_unresolved "zero-live verification could not run"
    exit 1
  }
  echo "zero-live verification: ${LIVE} live pods remain" >&2
  [ "$LIVE" = "0" ] || { echo "CLEANUP FAILED: live pods remain" >&2; exit 1; }
  exit 1
}
trap on_exit EXIT

echo "== control-plane pod (${CP_NAME}) =="
CREATE_BODY=$(python3 - "$ENV_JSON" "$CP_IMAGE" "$VOL_ID" "$CP_NAME" <<'PY_EOF'
import json, sys
env, image, vol, name = json.loads(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
print(json.dumps({
    "name": name,
    "imageName": image,
    "computeType": "CPU",
    # The network volume fixes the data center, so one exhausted CPU
    # family must not strand the durable control plane.  RunPod's
    # availability priority selects among these official 2-vCPU
    # families; the exact returned hourly rate remains subject to the
    # hard $0.10 refusal/delete ceiling below.
    "cpuFlavorIds": ["cpu3c", "cpu5c", "cpu3g", "cpu5g", "cpu3m", "cpu5m"],
    "cpuFlavorPriority": "availability",
    "vcpuCount": 2,
    "containerDiskInGb": 10,
    "networkVolumeId": vol,
    "volumeMountPath": "/workspace",
    "ports": ["8080/http"],
    "env": env,
}))
PY_EOF
)
# The create call itself: any transport/API/JSON failure lands in the
# EXIT trap's ambiguous-create discovery.
POD=$(rp POST /pods "$CREATE_BODY")
POD_ID=$(printf '%s' "$POD" | python3 -c "import json,sys;print(json.load(sys.stdin)['id'])")
echo "pod ${POD_ID} created at epoch ${PRE_CREATE_EPOCH}"

# Rate ceiling: the rate must be PRESENT, positive, and an exactly
# parsed Decimal at or below the ceiling — absent or malformed rates
# REFUSE (fail-closed), never default to zero.
POD_RATE=$(python3 - "$CEILING_CPU_USD_HR" "$POD" <<'PY_EOF'
import decimal, json, sys
ceiling = decimal.Decimal(sys.argv[1])
pod = json.loads(sys.argv[2])
raw = pod.get("costPerHr", pod.get("costPerHour"))
if raw is None:
    sys.exit("rate MISSING from the create response: refusing (fail-closed)")
try:
    rate = decimal.Decimal(str(raw))
except decimal.InvalidOperation:
    sys.exit(f"rate {raw!r} is not a decimal: refusing (fail-closed)")
if rate <= 0:
    sys.exit(f"rate {rate} is not positive: refusing (fail-closed)")
if rate > ceiling:
    sys.exit(f"rate {rate}/hr exceeds ceiling {ceiling}/hr: refusing")
print(rate)
PY_EOF
)
echo "rate \$${POD_RATE}/hr within ceiling \$${CEILING_CPU_USD_HR}/hr"

PUBLIC_BASE="https://${POD_ID}-8080.proxy.runpod.net"
if [ "$DEPLOY_TEST" = "1" ]; then PUBLIC_BASE="${WEINFER_DEPLOY_HEALTH_BASE:-$API}"; fi
echo "== waiting for /healthz (deadline ${HEALTH_DEADLINE_SECS}s) =="
DEADLINE=$(( $(date +%s) + HEALTH_DEADLINE_SECS ))
until curl -sf --connect-timeout 10 --max-time 15 "${PUBLIC_BASE}/healthz" >/dev/null 2>&1; do
  if [ "$(date +%s)" -ge "$DEADLINE" ]; then
    echo "health deadline exceeded" >&2
    exit 1
  fi
  sleep 10
done
LAUNCH_OK=1

echo
echo "CONTROL PLANE LIVE:"
echo "  public base:  ${PUBLIC_BASE}"
echo "  pod:          ${POD_ID} (\$${POD_RATE:-?}/hr; ceiling \$${CEILING_CPU_USD_HR}/hr)"
echo "  volume:       ${VOL_ID} (${VOLUME_GB}GB, durable)"
echo "  credentials:  ${CRED_FILE}"
echo "  retention:    demand_quiet_cycles=${DEMAND_QUIET_CYCLES}"
echo "  ONGOING BURN: pod \$${POD_RATE:-?}/hr + volume ~\$0.70/mo — founder-visible, deliberate"
echo
echo "next: scripts/canary_traversal.sh ${PUBLIC_BASE} ${CRED_FILE}"
