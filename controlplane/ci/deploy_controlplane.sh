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
CP_IMAGE="ghcr.io/jonathanrosado/weinfer-controlplane@sha256:${CP_IMAGE_SHA256:-REPLACED_AT_SHIP}"
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
VOLUME_GB=10                  # ~cents/month; the deliberate durable asset
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
    curl -fsS -X "$method" "$API$path" \
      -H "Authorization: Bearer $(read_provider_key)" \
      -H "Content-Type: application/json" -d "$body"
  else
    curl -fsS -X "$method" "$API$path" \
      -H "Authorization: Bearer $(read_provider_key)"
  fi
}

# ---------- serving configuration (the SHIPPED stacked arm) ----------
SERVED_MODEL="Qwen/Qwen2.5-7B-Instruct"
MAX_CTX=32768
VLLM_EXTRA_ARGS="--max-num-batched-tokens 16384 --max-num-seqs 256 --gpu-memory-utilization 0.92 --enable-chunked-prefill"
CONCURRENCY="64"
ALLOC_CONF="expandable_segments:True"
CUDA_VERSIONS="12.8"

# The engine-contract digest EXACTLY as the gateway derives it
# (main.rs launch_contract_digest): any replication drift here makes
# the gateway exit pre-listener with a named reason — the managed CI
# regression runs this exact env and catches drift at $0.
ENGINE_DIGEST=$(python3 - "$SERVED_MODEL" "$POD_IMAGE" "$VLLM_EXTRA_ARGS" "$QWEN_REV" "$MAX_CTX" "$CONCURRENCY" "$ALLOC_CONF" "$WORKER_SHA" "$CUDA_VERSIONS" <<'PY'
import hashlib, sys
model, image, extra, rev, ctx, conc, alloc, wsha, cuda = sys.argv[1:10]
canonical = f"{extra.strip()} --revision {rev} --tokenizer-revision {rev} --max-model-len {ctx}"
contract = (f"model={model}\nimage={image}\npod_args=\nvllm={canonical}"
            f"\nconcurrency={conc}\nalloc={alloc}\nworker_sha={wsha}\ncuda={cuda}")
print(hashlib.sha256(contract.encode()).hexdigest())
PY
)

# ---------- measured placement identities (the cost planner's facts) ----------
# A4500: $0.19/hr create-proven; measured delivered band [24546,35763]
# micro/Mtok => serving tps [1475,2150]; own-config boot 452s.
# RTX4090: $0.34/hr; measured serving 3985-4053 tok/s; boot 202-452s
# across its two paid runs.  Sources: pair p1787432264 + pair 1
# (progress.md ΔSTACK ledger, 2026-08-22).
PROFILES=$(python3 - "$SERVED_MODEL" "$QWEN_REV" "$POD_IMAGE" "$ENGINE_DIGEST" "$MAX_CTX" <<'PY'
import json, sys
model, rev, image, digest, ctx = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5])
def identity(sku):
    return {"served_model": model, "model_revision": rev,
            "tokenizer_revision": rev, "image_digest": image,
            "engine_config_digest": digest, "gpu_sku": sku, "cuda_class": "12"}
common = {"drain_low_micros": 60_000_000, "drain_high_micros": 60_000_000,
          "fixed_evidence": "Measured", "boot_scope": "SingleIdentity",
          "tps_evidence": "Measured", "tps_scope": "SingleIdentity",
          "observed_at_epoch": 1787432264, "vram_gb": 20,
          "max_context_tokens": ctx, "catalog_available": True,
          "recent_acquisition_failures": 0, "cuda_pin": ["12.8"]}
profiles = [
    dict(common, identity=identity("NVIDIA RTX A4500"),
         rate_micro_per_hour=190_000, tps_low=1475, tps_high=2150,
         boot_low_micros=452_000_000, boot_high_micros=452_000_000,
         source="pair-p1787432264-baseline"),
    dict(common, identity=identity("NVIDIA GeForce RTX 4090"),
         rate_micro_per_hour=340_000, tps_low=3985, tps_high=4053,
         boot_low_micros=202_000_000, boot_high_micros=452_000_000,
         vram_gb=24, source="pair-1-stacked-vs-baseline"),
]
print(json.dumps(profiles))
PY
)

# ---------- priced customer catalog ----------
CATALOG='{"revision":"cat-live-1","models":[{"id":"Qwen/Qwen2.5-7B-Instruct","input_price_micro_per_mtok":100000,"output_price_micro_per_mtok":400000,"capabilities":["chat"],"created":1787432264,"context_length":32768}]}'

# ---------- assemble pod env (argv-passed: no quoting drift) ----------
ENV_JSON=$(APIKEYS="org-live:key-live:$(sha "$CUSTOMER_KEY")" \
  ADMIN_VER="$(sha "$ADMIN_KEY")" \
  WORKER_RING="pod-live:$(sha "$WORKER_KEY")" \
  RP_KEY="$(read_provider_key)" \
  GW_URL="$GW_URL" GW_SHA="$GW_SHA" WORKER_URL="$WORKER_URL" \
  WORKER_SHA="$WORKER_SHA" POD_IMAGE="$POD_IMAGE" \
  SERVED_MODEL="$SERVED_MODEL" QWEN_REV="$QWEN_REV" CATALOG="$CATALOG" \
  PROFILES="$PROFILES" VLLM_EXTRA_ARGS="$VLLM_EXTRA_ARGS" \
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
    "WEINFER_PLACEMENT_PROFILES": e["PROFILES"],
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

# ---------- create the pod, ceiling enforced in code ----------
echo "== control-plane pod =="
PRE_CREATE_EPOCH=$(date +%s)
POD=$(rp POST /pods "$(python3 - "$ENV_JSON" "$CP_IMAGE" "$VOL_ID" <<'PY'
import json, sys
env, image, vol = json.loads(sys.argv[1]), sys.argv[2], sys.argv[3]
print(json.dumps({
    "name": "weinfer-controlplane",
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
PY
)")
POD_ID=$(printf '%s' "$POD" | python3 -c "import json,sys;print(json.load(sys.stdin)['id'])")
POD_RATE=$(printf '%s' "$POD" | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('costPerHr', d.get('costPerHour','')))")
echo "pod ${POD_ID} created at epoch ${PRE_CREATE_EPOCH}, rate \$${POD_RATE:-unknown}/hr"

cleanup_pod() {
  echo "DELETING control-plane pod ${POD_ID} (failure path; volume kept)" >&2
  rp DELETE "/pods/${POD_ID}" >/dev/null 2>&1 || true
  LIVE=$(rp GET /pods | python3 -c "
import json,sys
pods=json.load(sys.stdin)
pods=pods.get('pods', pods) if isinstance(pods, dict) else pods
print(sum(1 for p in pods if p.get('desiredStatus') not in ('EXITED','TERMINATED')))" 2>/dev/null || echo "?")
  echo "zero-live verification: ${LIVE} live pods remain" >&2
}

# Rate ceiling: refuse to keep a flavor above the ceiling.
if [ -n "$POD_RATE" ]; then
  OVER=$(python3 -c "print(1 if float('$POD_RATE') > float('$CEILING_CPU_USD_HR') else 0)" 2>/dev/null || echo 0)
  if [ "$OVER" = "1" ]; then
    echo "rate \$${POD_RATE}/hr exceeds ceiling \$${CEILING_CPU_USD_HR}/hr" >&2
    cleanup_pod; exit 1
  fi
fi

PUBLIC_BASE="https://${POD_ID}-8080.proxy.runpod.net"
echo "== waiting for /healthz (deadline ${HEALTH_DEADLINE_SECS}s) =="
DEADLINE=$(( $(date +%s) + HEALTH_DEADLINE_SECS ))
until curl -sf "${PUBLIC_BASE}/healthz" >/dev/null 2>&1; do
  if [ "$(date +%s)" -ge "$DEADLINE" ]; then
    echo "health deadline exceeded" >&2
    cleanup_pod; exit 1
  fi
  sleep 10
done

echo
echo "CONTROL PLANE LIVE:"
echo "  public base:  ${PUBLIC_BASE}"
echo "  pod:          ${POD_ID} (\$${POD_RATE:-?}/hr; ceiling \$${CEILING_CPU_USD_HR}/hr)"
echo "  volume:       ${VOL_ID} (${VOLUME_GB}GB, durable)"
echo "  credentials:  ${CRED_FILE}"
echo "  ONGOING BURN: pod \$${POD_RATE:-?}/hr + volume ~\$0.07/mo — founder-visible, deliberate"
echo
echo "next: scripts/canary_traversal.sh ${PUBLIC_BASE} ${CRED_FILE}"
