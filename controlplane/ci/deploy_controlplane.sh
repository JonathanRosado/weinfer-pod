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

KEY_FILE="../rig/scaffold/runpod_account_a.txt"
if [ "$RENDER_ONLY" = "0" ]; then
  [ -f "$KEY_FILE" ] || { echo "key file missing" >&2; exit 1; }
fi
API="https://rest.runpod.io/v1"
read_provider_key() {
  # Render mode NEVER reads the real key — even when the file exists.
  # The rendered JSON is written to disk/CI logs; the provider key
  # must never appear anywhere but live pod env + provider calls.
  if [ "$RENDER_ONLY" = "1" ]; then
    printf 'ci-fake-provider-key'
  else
    tr -d '[:space:]' < "$KEY_FILE"
  fi
}

# ---------- pinned trust roots (verify remote against THESE) ----------
CP_IMAGE="ghcr.io/jonathanrosado/weinfer-controlplane@sha256:42cc6195dc1ff6dec4a33689e507db396d2a34e6359ba52aa40d662eb8422f86"
GW_TAG="gateway-v0.2.0"
GW_SHA="ca12e53e8729ae97cf4a3c05eef1372ccc0a00be506e470e928ca083789a4abf"
WORKER_TAG="worker-v0.4.0"
WORKER_SHA="7bd6f06f07f68afb24bbd8fec086bf3be04d574ebe5a86791e9f2c230cca5f6b"
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
HEALTH_DEADLINE_SECS=900      # /healthz must answer within 15 min or we delete

sha() { python3 -c "import hashlib,sys;print(hashlib.sha256(sys.argv[1].encode()).hexdigest())" "$1"; }
rand() { python3 -c "import secrets;print(secrets.token_urlsafe(24))"; }

# CI presets these in --render-env mode; live deploys generate fresh.
ADMIN_KEY="${ADMIN_KEY:-wf-admin-$(rand)}"
CUSTOMER_KEY="${CUSTOMER_KEY:-wf-live-$(rand)}"
WORKER_KEY="${WORKER_KEY:-wf-worker-$(rand)}"

rp() { # method path [json]
  local method="$1" path="$2" body="${3:-}"
  if [ -n "$body" ]; then
    curl -fsS --connect-timeout 10 --max-time 60 -X "$method" "$API$path" \
      -H "Authorization: Bearer $(read_provider_key)" \
      -H "Content-Type: application/json" -d "$body"
  else
    curl -fsS --connect-timeout 10 --max-time 60 -X "$method" "$API$path" \
      -H "Authorization: Bearer $(read_provider_key)"
  fi
}

# ---------- serving configuration: the SEALED STACKED A4500 ARM ----------
# EXACT production identity = the registered launch bytes of pair
# p1787432264's stacked arm (codex 0163): same model length, seed,
# flags, concurrency, allocator, GPU, CUDA pin.  We sell no context
# beyond the executed bound.  profile_evidence_regression.sh refuses
# any drift between this block, the sealed evidence, and the probe's
# registered bytes.
SERVED_MODEL="Qwen/Qwen2.5-7B-Instruct"
MAX_CTX=8192
VLLM_EXTRA_ARGS="--seed 0 --max-num-batched-tokens 16384 --max-num-seqs 256 --gpu-memory-utilization 0.92 --enable-chunked-prefill"
CONCURRENCY="64"
ALLOC_CONF="expandable_segments:True"
CUDA_VERSIONS="12.8"

# NO PLACEMENT PROFILES (codex 0164): the paid pair measured the
# worker-v0.1.0 identity; production runs worker-v0.4.0 — a DIFFERENT
# exact identity by the launch-contract digest's own authority.  The
# first public canary therefore runs the legacy single-pool
# provisioning path with explicit A4500/CUDA/8192/current-worker
# launch bytes and the STATIC context authority; its own immutable
# lifecycle/usage records become the v0.4 identity's first
# measurement.  The old pair remains Conservative historical evidence
# only.  profile_evidence_regression.sh enforces all of this.

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
  CUDA_VERSIONS="$CUDA_VERSIONS" \
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
    "WEINFER_RUNPOD_API_KEY": e["RP_KEY"],
    "WEINFER_WORKER_KEYS": e["WORKER_RING"],
    "WEINFER_WORKER_URL": e["WORKER_URL"],
    "WEINFER_WORKER_SHA256": e["WORKER_SHA"],
    "WEINFER_POOL": "community-qwen7b-0",
    "WEINFER_GPU_TYPE": "NVIDIA RTX A4500",
    "WEINFER_CLOUD": "COMMUNITY",
    "WEINFER_IMAGE": e["POD_IMAGE"],
    "WEINFER_SERVED_MODEL": e["SERVED_MODEL"],
    "WEINFER_MODEL_REVISION": e["QWEN_REV"],
    "WEINFER_TOKENIZER_REVISION": e["QWEN_REV"],
    "WEINFER_MODEL_CATALOG": e["CATALOG"],
    # STATIC context authority (legacy bootstrap path, no profiles):
    # the catalog may never sell context the engine cannot execute.
    "WEINFER_BACKEND_MAX_CONTEXT": e["MAX_CTX"],
    "VLLM_EXTRA_ARGS": e["VLLM_EXTRA_ARGS"],
    "WEINFER_CONCURRENCY": e["CONCURRENCY"],
    "PYTORCH_CUDA_ALLOC_CONF": e["ALLOC_CONF"],
    "WEINFER_CUDA_VERSIONS": e["CUDA_VERSIONS"],
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
  printf '%s' "${body%$'\n'*}" | python3 -c "
import json,sys
pod=json.load(sys.stdin)
sys.exit(0 if pod.get('desiredStatus') in ('EXITED','TERMINATED') else 1)"
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
    # Provider eventual consistency: poll the exact launch name over
    # a quiescence window before concluding nothing was created.
    for read in 1 2 3 4 5 6; do
      FOUND=$(rp GET /pods | python3 -c "
import json,sys
pods=json.load(sys.stdin)
pods=pods.get('pods', pods) if isinstance(pods, dict) else pods
match=[p for p in pods if p.get('name')=='${CP_NAME}']
print(match[0]['id'] if match else '')" 2>/dev/null) || {
        echo "CLEANUP FAILED: could not list pods to resolve the ambiguous create" >&2
        exit 1
      }
      if [ -n "$FOUND" ]; then POD_ID="$FOUND"; break; fi
      [ "$read" = "6" ] || sleep 10
    done
  fi
  if [ -n "$POD_ID" ]; then
    delete_pod_verified "$POD_ID" || {
      echo "CLEANUP FAILED: pod ${POD_ID} could not be verified gone" >&2
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
print(sum(1 for p in pods if p.get('desiredStatus') not in ('EXITED','TERMINATED')))") || {
    echo "CLEANUP FAILED: zero-live verification could not run" >&2
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
    "cpuFlavorIds": ["cpu3c"],
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
echo "  ONGOING BURN: pod \$${POD_RATE:-?}/hr + volume ~\$0.70/mo — founder-visible, deliberate"
echo
echo "next: scripts/canary_traversal.sh ${PUBLIC_BASE} ${CRED_FILE}"
