#!/usr/bin/env bash
# Deploy the WeInfer control plane: one cheap RunPod CPU pod running the
# pinned control-plane image (Postgres + the sha-pinned gateway), public HTTPS
# at the provider proxy, owning a managed GPU pool through the cost planner.
# Persistent deployments default to a durable network volume.  The registered
# N=24 measurement selects a run-scoped Pod volume so CPU placement is not
# stranded by network-volume host scarcity; it survives Pod restarts and is
# deleted with the Pod after all database-backed evidence is sealed locally.
#
# COST WHEN RUN: CPU pod cents/hour + the selected storage substrate.
# The launch ceiling is IN CODE below: health deadline, direct pod
# DELETE on any failure, zero-live verification, and an explicit
# ongoing burn ceiling printed for the record.
#
# TRUST ROOTS ARE COMMITTED CONSTANTS (codex 0162): image digest and
# both binary sha256 values are pinned here; remote release metadata
# is used only as a CONSISTENCY CHECK, never as the authority.
set -euo pipefail
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$SCRIPT_DIR/.."

# --render-env: print the EXACT pod env JSON and exit — no provider
# calls, no credential writes.  The managed CI regression consumes
# this so the environment it proves is the environment we deploy.
RENDER_ONLY=0
[ "${1:-}" = "--render-env" ] && RENDER_ONLY=1

# A deployment is one named product profile.  Missing and unknown names refuse
# before any trust-root fetch or provider call: a typo may never fall back to a
# cheaper model while retaining the requested profile's labels.
SERVING_PROFILE="${WEINFER_SERVING_PROFILE:-}"
case "$SERVING_PROFILE" in
  qwen7b-consumer-v1|gpt-oss-120b-h100-v1) ;;
  "")
    echo "WEINFER_SERVING_PROFILE is required" >&2
    exit 1
    ;;
  *)
    echo "unknown WEINFER_SERVING_PROFILE: ${SERVING_PROFILE}" >&2
    exit 1
    ;;
esac

CONTROLPLANE_STORAGE_MODE="${WEINFER_CONTROLPLANE_STORAGE_MODE:-network-volume}"
case "$CONTROLPLANE_STORAGE_MODE" in
  network-volume|run-scoped-pod) ;;
  *)
    echo "WEINFER_CONTROLPLANE_STORAGE_MODE must be network-volume or run-scoped-pod" >&2
    exit 1
    ;;
esac

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

# Worker readiness is the LOCAL engine-health registration handshake, not a
# provider status bit.  The consumer profile retains its registered 60-minute
# allowance.  The H100 profile is immutable at 126 x 10s: even if the image's
# own 1,200s readiness bound is reached, provider-create through manager
# deletion remains below the frozen $1 GPU ceiling at the profile's $2.70/hr
# maximum.  A caller may restate an H100 value exactly, never widen it.
# Keep these simple literal authorities: the completed N=24 protocol parses
# and byte-binds the consumer profile rather than borrowing binary defaults.
POLICY_BOOT_SEED_SECS="452"
POLICY_TOKENS_PER_SEC="4000"
if [ "$SERVING_PROFILE" = "qwen7b-consumer-v1" ]; then
  READY_PROBE_BUDGET="${WEINFER_PROBE_BUDGET:-360}"
  READY_PROBE_DELAY_SECS="${WEINFER_PROBE_DELAY_SECS:-10}"
  BOOT_FRACTION="${WEINFER_BOOT_FRACTION:-0.2}"
else
  if [ -n "${WEINFER_PROBE_BUDGET+x}" ] && [ "$WEINFER_PROBE_BUDGET" != "126" ]; then
    echo "gpt-oss-120b-h100-v1 requires WEINFER_PROBE_BUDGET=126" >&2
    exit 1
  fi
  if [ -n "${WEINFER_PROBE_DELAY_SECS+x}" ] && [ "$WEINFER_PROBE_DELAY_SECS" != "10" ]; then
    echo "gpt-oss-120b-h100-v1 requires WEINFER_PROBE_DELAY_SECS=10" >&2
    exit 1
  fi
  if [ -n "${WEINFER_BOOT_FRACTION+x}" ] && [ "$WEINFER_BOOT_FRACTION" != "0.2" ]; then
    echo "gpt-oss-120b-h100-v1 requires WEINFER_BOOT_FRACTION=0.2" >&2
    exit 1
  fi
  READY_PROBE_BUDGET="126"
  READY_PROBE_DELAY_SECS="10"
  BOOT_FRACTION="0.2"
  # Cost-derived readiness ceiling, not a measured H100 boot time.  The
  # acquisition planner takes max(fleet floor, this profile bound).
  POLICY_BOOT_SEED_SECS="1200"
  # Conservative cross-launch-identity policy prior. The matching sealed
  # seqs8 point was accepted with stability enforcement disabled (CV 0.494),
  # so its 2768.891832 point estimate does not quantify a resolved gain. The
  # new immutable image and worker remain unmeasured.
  POLICY_TOKENS_PER_SEC="2600"
fi
python3 - "$READY_PROBE_BUDGET" "$READY_PROBE_DELAY_SECS" <<'PY'
import sys

for name, raw in (
    ("WEINFER_PROBE_BUDGET", sys.argv[1]),
    ("WEINFER_PROBE_DELAY_SECS", sys.argv[2]),
):
    if not raw.isascii() or not raw.isdigit() or int(raw) <= 0:
        raise SystemExit(f"{name} must be a positive base-10 integer, got {raw!r}")
PY

# Cold-start amortization target. Production keeps the shipped 20% default;
# a registered deep-backlog measurement may lower it so the full intended
# backlog is durably visible before acquisition on the consumer profile.  The
# H100 product profile is fixed at 20%. This affects only the cold BootForBacklog
# threshold. Validate before any trust-root or provider work.
python3 - "$BOOT_FRACTION" <<'PY'
from decimal import Decimal, InvalidOperation
import sys

raw = sys.argv[1]
try:
    value = Decimal(raw)
except InvalidOperation:
    raise SystemExit(
        f"WEINFER_BOOT_FRACTION must be a finite decimal strictly inside (0, 1), got {raw!r}"
    )
if not value.is_finite() or not (Decimal(0) < value < Decimal(1)):
    raise SystemExit(
        f"WEINFER_BOOT_FRACTION must be a finite decimal strictly inside (0, 1), got {raw!r}"
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
GW_TAG="gateway-v0.26.0"
GW_SHA="abe8f8cd86848bf2845b95fa78d5a2677b01d5afc17e5a27dbfc4784f27e827c"
WORKER_TAG="worker-v0.6.0"
WORKER_SHA="0d9b0be9c2a756716a5630966172c32f199e4387c7ee57bf8cb4ccc69f7354fe"
POD_IMAGE="ghcr.io/jonathanrosado/weinfer-pod@sha256:160a926826565b1ed0134335f3f68e65ed457fcb034058639fc5c9b5c7ec2613"
QWEN_REV="a09a35458c702b33eeacc393d103063234e8bc28"
H100_POD_IMAGE="ghcr.io/jonathanrosado/weinfer-pod@sha256:92377f4077b2faef0a1e1eec7cf56ffe2f04b3815e8254db87bd2a96f2cbe214"
H100_REV="b5c939de8f754692c1647ca79fbf85e8c1e70f8a"
RUNTIME_CONTRACT_SHA256=""
if [ "$SERVING_PROFILE" = "gpt-oss-120b-h100-v1" ]; then
  POD_IMAGE="$H100_POD_IMAGE"
  RUNTIME_CONTRACT_PATH=""
  for candidate in \
    "$SCRIPT_DIR/../pod-image/runtime/runtime-contract.json" \
    "$SCRIPT_DIR/../../runtime/runtime-contract.json"; do
    if [ -f "$candidate" ] && [ ! -L "$candidate" ]; then
      RUNTIME_CONTRACT_PATH="$candidate"
      break
    fi
  done
  [ -n "$RUNTIME_CONTRACT_PATH" ] || {
    echo "gpt-oss-120b-h100-v1 runtime-contract authority is missing" >&2
    exit 1
  }
  RUNTIME_CONTRACT_SHA256="$(python3 - "$RUNTIME_CONTRACT_PATH" <<'PY'
import hashlib, pathlib, sys
path = pathlib.Path(sys.argv[1])
print(hashlib.sha256(path.read_bytes()).hexdigest())
PY
)"
fi
python3 - "$SERVING_PROFILE" "$POD_IMAGE" <<'PY'
import re, sys
profile, image = sys.argv[1:]
if re.fullmatch(r"ghcr\.io/[a-z0-9._/-]+@sha256:[0-9a-f]{64}", image) is None:
    raise SystemExit(
        f"{profile} has an unresolved or non-immutable WEINFER image authority"
    )
PY

GW_URL="https://github.com/JonathanRosado/weinfer-pod/releases/download/${GW_TAG}/weinfer-gateway"
WORKER_URL="https://github.com/JonathanRosado/weinfer-pod/releases/download/${WORKER_TAG}/weinfer-worker"

# ---------- burn ceiling ----------
CEILING_CPU_USD_HR="0.10"     # refuse any CPU flavor above this
VOLUME_GB=10                  # network volume or run-scoped Pod volume
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
# the provider's overall availability label changes scarcity timing but does
# not exclude a structurally compatible row.
# a definitive create denial falls through to the next row in the
# same plan.  A SKU earns delivered-cost facts only from its own later
# sealed traversal.
SERVED_MODEL="Qwen/Qwen2.5-7B-Instruct"
MAX_CTX=8192
VLLM_EXTRA_ARGS="--seed 0 --max-num-batched-tokens 16384 --max-num-seqs 256 --gpu-memory-utilization 0.92 --enable-chunked-prefill --enable-prefix-caching"
CONCURRENCY="64"
ALLOC_CONF="expandable_segments:True"
MODEL_REV="$QWEN_REV"
TOKENIZER_REV="$QWEN_REV"
POOL_NAME="community-qwen7b-0"
LEGACY_GPU_TYPE="NVIDIA RTX A4500"
MIN_VRAM_GB="20"
MAX_GPU_RATE="0.40"
POD_DISK_GB="40"
# Keep the JSON on one physical line: --render-env is also consumed as a
# Docker env-file by the release smoke, whose format does not permit embedded
# newlines in values.
DEFAULT_BOOTSTRAP_HARDWARE='[{"gpu_sku":"NVIDIA RTX A5000","cuda_class":"12","vram_gb":24,"throughput_seed_tokens_per_sec":2628,"throughput_seed_kind":"policy_prior","throughput_seed_source":"prior-calibration-v1; raw_seed=4000; basis=ready_window_tps_low; effective=floor(raw_seed*3943/6000)=2628; worst scored ratio across sealed seed4090-1787834610 and seed3090-1787841661; original_kind=policy_prior; no traffic observation for this SKU","boot_seed_micros":664034722,"drain_seed_micros":740377,"fixed_seed_kind":"policy_prior","fixed_seed_source":"max sealed activation=664034722 plus max sealed drain=740377 across batch-live-1787630415/A4500, amort3full-1787755326/SFF Ada, seed4090-1787834610/RTX 4090, and seed3090-1787841661/RTX 3090; no SKU traffic observation"},{"gpu_sku":"NVIDIA RTX 4000 SFF Ada Generation","cuda_class":"12","vram_gb":20,"throughput_seed_tokens_per_sec":2681,"throughput_seed_kind":"traffic_observed_cross_identity","throughput_seed_source":"sealed amort3full-1787755326; tps_low=2681; basis=ready_to_batch1_completion_rederived_from_sealed_phases; workload_sha256=2392bb588923e88dc3f1473a9393a0e099a19a4889ccbd1d945b33df9e5ed205; candidate_only 1/5 boots","boot_seed_micros":492992942,"drain_seed_micros":685232,"fixed_seed_kind":"traffic_observed_cross_identity","fixed_seed_source":"sealed amort3full-1787755326; activation=492992942; drain=685232; candidate_only 1/5 boots"},{"gpu_sku":"NVIDIA RTX A4500","cuda_class":"12","vram_gb":20,"throughput_seed_tokens_per_sec":4161,"throughput_seed_kind":"traffic_observed_cross_identity","throughput_seed_source":"sealed batch-live-1787630415; tps_low=4161; basis=ready_window_tps_low; workload_sha256=2392bb588923e88dc3f1473a9393a0e099a19a4889ccbd1d945b33df9e5ed205; candidate_only 2/5 boots","boot_seed_micros":429080126,"drain_seed_micros":731031,"fixed_seed_kind":"traffic_observed_cross_identity","fixed_seed_source":"sealed batch-live-1787630415; boot_high=429080126; drain=731031; candidate_only 2/5 boots"},{"gpu_sku":"NVIDIA RTX 4000 Ada Generation","cuda_class":"12","vram_gb":20,"throughput_seed_tokens_per_sec":2628,"throughput_seed_kind":"policy_prior","throughput_seed_source":"prior-calibration-v1; raw_seed=4000; basis=ready_window_tps_low; effective=floor(raw_seed*3943/6000)=2628; worst scored ratio across sealed seed4090-1787834610 and seed3090-1787841661; original_kind=policy_prior; no traffic observation for this SKU","boot_seed_micros":664034722,"drain_seed_micros":740377,"fixed_seed_kind":"policy_prior","fixed_seed_source":"max sealed activation=664034722 plus max sealed drain=740377 across batch-live-1787630415/A4500, amort3full-1787755326/SFF Ada, seed4090-1787834610/RTX 4090, and seed3090-1787841661/RTX 3090; no SKU traffic observation"},{"gpu_sku":"NVIDIA GeForce RTX 3090","cuda_class":"12","vram_gb":24,"throughput_seed_tokens_per_sec":3943,"throughput_seed_kind":"traffic_observed_cross_identity","throughput_seed_source":"sealed seed3090-1787841661 profile candidate; tps_low=3943; basis=ready_window_tps_low; workload_sha256=2392bb588923e88dc3f1473a9393a0e099a19a4889ccbd1d945b33df9e5ed205; candidate_only 1/5 boots","boot_seed_micros":462995788,"drain_seed_micros":740377,"fixed_seed_kind":"traffic_observed_cross_identity","fixed_seed_source":"sealed seed3090-1787841661 profile candidate; activation=462995788; drain=740377; candidate_only 1/5 boots"},{"gpu_sku":"NVIDIA GeForce RTX 3090","cuda_class":"13","vram_gb":24,"throughput_seed_tokens_per_sec":3943,"throughput_seed_kind":"traffic_observed_cross_identity","throughput_seed_source":"sealed seed3090-1787841661 profile candidate; tps_low=3943; basis=ready_window_tps_low; workload_sha256=2392bb588923e88dc3f1473a9393a0e099a19a4889ccbd1d945b33df9e5ed205; candidate_only 1/5 boots; CUDA class 13 is an unmeasured exact-identity variant reusing this SKU prior","boot_seed_micros":462995788,"drain_seed_micros":740377,"fixed_seed_kind":"traffic_observed_cross_identity","fixed_seed_source":"sealed seed3090-1787841661 profile candidate; activation=462995788; drain=740377; candidate_only 1/5 boots; CUDA class 13 is an unmeasured exact-identity variant reusing this SKU prior"},{"gpu_sku":"NVIDIA GeForce RTX 3090 Ti","cuda_class":"12","vram_gb":24,"throughput_seed_tokens_per_sec":4403,"throughput_seed_kind":"spec_derived","throughput_seed_source":"prior-calibration-v1; raw_seed=6700; basis=ready_window_tps_low; effective=floor(raw_seed*3943/6000)=4403; worst scored ratio across sealed seed4090-1787834610 and seed3090-1787841661; original=analytic-v1 FP16-compute extrapolation from sealed A4500 ready_window_tps_low anchor; no traffic observation for this SKU","boot_seed_micros":664034722,"drain_seed_micros":740377,"fixed_seed_kind":"policy_prior","fixed_seed_source":"max sealed activation=664034722 plus max sealed drain=740377 across batch-live-1787630415/A4500, amort3full-1787755326/SFF Ada, seed4090-1787834610/RTX 4090, and seed3090-1787841661/RTX 3090; no SKU traffic observation"},{"gpu_sku":"NVIDIA GeForce RTX 3090 Ti","cuda_class":"13","vram_gb":24,"throughput_seed_tokens_per_sec":4403,"throughput_seed_kind":"spec_derived","throughput_seed_source":"prior-calibration-v1; raw_seed=6700; basis=ready_window_tps_low; effective=floor(raw_seed*3943/6000)=4403; worst scored ratio across sealed seed4090-1787834610 and seed3090-1787841661; original=analytic-v1 FP16-compute extrapolation from sealed A4500 ready_window_tps_low anchor; no traffic observation for this SKU; CUDA class 13 is an unmeasured exact-identity variant reusing this SKU prior","boot_seed_micros":664034722,"drain_seed_micros":740377,"fixed_seed_kind":"policy_prior","fixed_seed_source":"max sealed activation=664034722 plus max sealed drain=740377 across batch-live-1787630415/A4500, amort3full-1787755326/SFF Ada, seed4090-1787834610/RTX 4090, and seed3090-1787841661/RTX 3090; no SKU traffic observation; CUDA class 13 is an unmeasured exact-identity variant reusing this SKU prior"},{"gpu_sku":"NVIDIA RTX A6000","cuda_class":"12","vram_gb":48,"throughput_seed_tokens_per_sec":4271,"throughput_seed_kind":"spec_derived","throughput_seed_source":"prior-calibration-v1; raw_seed=6500; basis=ready_window_tps_low; effective=floor(raw_seed*3943/6000)=4271; worst scored ratio across sealed seed4090-1787834610 and seed3090-1787841661; original=analytic-v1 FP16-compute extrapolation from sealed A4500 ready_window_tps_low anchor; no traffic observation for this SKU","boot_seed_micros":664034722,"drain_seed_micros":740377,"fixed_seed_kind":"policy_prior","fixed_seed_source":"max sealed activation=664034722 plus max sealed drain=740377 across batch-live-1787630415/A4500, amort3full-1787755326/SFF Ada, seed4090-1787834610/RTX 4090, and seed3090-1787841661/RTX 3090; no SKU traffic observation"},{"gpu_sku":"NVIDIA GeForce RTX 4090","cuda_class":"12","vram_gb":24,"throughput_seed_tokens_per_sec":9548,"throughput_seed_kind":"traffic_observed_cross_identity","throughput_seed_source":"sealed seed4090-1787834610 profile candidate; tps_low=9548; basis=ready_window_tps_low; workload_sha256=2392bb588923e88dc3f1473a9393a0e099a19a4889ccbd1d945b33df9e5ed205; candidate_only 1/5 boots","boot_seed_micros":664034722,"drain_seed_micros":633859,"fixed_seed_kind":"traffic_observed_cross_identity","fixed_seed_source":"sealed seed4090-1787834610 profile candidate; activation=664034722; drain=633859; candidate_only 1/5 boots"},{"gpu_sku":"NVIDIA A40","cuda_class":"12","vram_gb":48,"throughput_seed_tokens_per_sec":2628,"throughput_seed_kind":"policy_prior","throughput_seed_source":"prior-calibration-v1; raw_seed=4000; basis=ready_window_tps_low; effective=floor(raw_seed*3943/6000)=2628; worst scored ratio across sealed seed4090-1787834610 and seed3090-1787841661; original_kind=policy_prior; no traffic observation for this SKU","boot_seed_micros":664034722,"drain_seed_micros":740377,"fixed_seed_kind":"policy_prior","fixed_seed_source":"max sealed activation=664034722 plus max sealed drain=740377 across batch-live-1787630415/A4500, amort3full-1787755326/SFF Ada, seed4090-1787834610/RTX 4090, and seed3090-1787841661/RTX 3090; no SKU traffic observation"}]'
H100_BOOTSTRAP_HARDWARE='[{"gpu_sku":"NVIDIA H100 80GB HBM3","cuda_class":"12","vram_gb":80,"throughput_seed_tokens_per_sec":2600,"throughput_seed_kind":"traffic_observed_cross_identity","throughput_seed_source":"sealed company/research/bench_runs/cycle3-tokenbench-scan1/tb-sweep/points/n-sweep__seqs8-batch8192-cacheoff__n-32__arrival-saturation__repeat-0/load-report.json; sha256=f73161fa3331599e161970e643a9d3c7b4bdb8f2adcecc5145c792448cb9ec44; command.json sha256=4e4b5f8229bec7bfe0e3896be892ce9aa76d41bf5e71502ef36998ada2d43c37; max_num_seqs=8; max_num_batched_tokens=8192; prefix_caching=false; hardware=NVIDIA H100 80GB HBM3; model=gpt-oss-120b; tokenizer_revision=b5c939de8f754692c1647ca79fbf85e8c1e70f8a; observed_tps=2768.891832; require_stable=false; block_throughput_cv=0.49436819; block_count=5; point_estimate_only_no_resolved_seqs8_gain; policy_prior=2600; new immutable image and worker are unmeasured","boot_seed_micros":1200000000,"drain_seed_micros":30000000,"fixed_seed_kind":"policy_prior","fixed_seed_source":"cost-derived readiness ceiling: 1200s at 2700000 uUSD/hr = 900000 uUSD; drain=30s policy allowance; no lifecycle observation for this image and worker"}]'

if [ "$SERVING_PROFILE" = "gpt-oss-120b-h100-v1" ]; then
  SERVED_MODEL="openai/gpt-oss-120b"
  MODEL_REV="$H100_REV"
  TOKENIZER_REV="$H100_REV"
  MAX_CTX=131072
  VLLM_EXTRA_ARGS="--seed 0 --max-num-batched-tokens 8192 --max-num-seqs 8 --gpu-memory-utilization 0.95 --enable-chunked-prefill --enable-prefix-caching --dtype bfloat16 --kv-cache-dtype fp8 --calculate-kv-scales --tensor-parallel-size 1 --served-model-name openai/gpt-oss-120b --ignore-patterns original/* metal/*"
  CONCURRENCY="4"
  ALLOC_CONF="expandable_segments:True"
  DEFAULT_BOOTSTRAP_HARDWARE="$H100_BOOTSTRAP_HARDWARE"
  POOL_NAME="community-gpt-oss-120b-h100-0"
  LEGACY_GPU_TYPE="NVIDIA H100 80GB HBM3"
  MIN_VRAM_GB="80"
  MAX_GPU_RATE="2.70"
  POD_DISK_GB="120"
fi
# A paid exact-identity observation may narrow the default queue to ONE known
# SKU/CUDA-class row plus the sealed exact minor pin. This is an experiment substrate selector, not a planner
# fact: it keeps the row's typed prior and provenance byte-for-byte, and cannot
# add or mutate identities. An unknown pair refuses before provider create.
BOOTSTRAP_ONLY_GPU_SKU="${WEINFER_BOOTSTRAP_ONLY_GPU_SKU:-}"
BOOTSTRAP_ONLY_CUDA_CLASS="${WEINFER_BOOTSTRAP_ONLY_CUDA_CLASS:-}"
BOOTSTRAP_ONLY_CUDA_PIN="${WEINFER_BOOTSTRAP_ONLY_CUDA_PIN:-}"
if [ "$SERVING_PROFILE" = "gpt-oss-120b-h100-v1" ] && \
   { [ -n "$BOOTSTRAP_ONLY_GPU_SKU" ] || [ -n "$BOOTSTRAP_ONLY_CUDA_CLASS" ] || [ -n "$BOOTSTRAP_ONLY_CUDA_PIN" ]; }; then
  echo "gpt-oss-120b-h100-v1 is already an exact one-row hardware profile; BOOTSTRAP_ONLY selectors are forbidden" >&2
  exit 1
fi
if [ -n "$BOOTSTRAP_ONLY_GPU_SKU" ] || [ -n "$BOOTSTRAP_ONLY_CUDA_CLASS" ] || [ -n "$BOOTSTRAP_ONLY_CUDA_PIN" ]; then
  if [ -z "$BOOTSTRAP_ONLY_GPU_SKU" ] || [ -z "$BOOTSTRAP_ONLY_CUDA_CLASS" ] || [ -z "$BOOTSTRAP_ONLY_CUDA_PIN" ]; then
    echo "WEINFER_BOOTSTRAP_ONLY_GPU_SKU, WEINFER_BOOTSTRAP_ONLY_CUDA_CLASS, and WEINFER_BOOTSTRAP_ONLY_CUDA_PIN must be set together" >&2
    exit 1
  fi
  BOOTSTRAP_HARDWARE=$(python3 - "$DEFAULT_BOOTSTRAP_HARDWARE" "$BOOTSTRAP_ONLY_GPU_SKU" "$BOOTSTRAP_ONLY_CUDA_CLASS" "$BOOTSTRAP_ONLY_CUDA_PIN" <<'PY'
import json, sys
rows = json.loads(sys.argv[1])
selected = [
    row for row in rows
    if row.get("gpu_sku") == sys.argv[2] and str(row.get("cuda_class")) == sys.argv[3]
]
if len(selected) != 1:
    raise SystemExit(
        "WEINFER_BOOTSTRAP_ONLY_GPU_SKU/WEINFER_BOOTSTRAP_ONLY_CUDA_CLASS/"
        "WEINFER_BOOTSTRAP_ONLY_CUDA_PIN "
        f"must name exactly one configured identity: {sys.argv[2]!r} CUDA {sys.argv[3]!r}"
    )
pin = sys.argv[4]
parts = pin.split(".", 1)
if len(parts) != 2 or not all(part.isdigit() for part in parts) or parts[0] != sys.argv[3]:
    raise SystemExit(
        f"WEINFER_BOOTSTRAP_ONLY_CUDA_PIN must be an exact CUDA {sys.argv[3]} minor: {pin!r}"
    )
selected = [dict(selected[0], cuda_pin=pin)]
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
if [ "$SERVING_PROFILE" = "gpt-oss-120b-h100-v1" ]; then
  # Internal first-traversal tariff, not a published market claim.  The first
  # sealed product lifecycle will replace this conservative price authority;
  # customer economics never masquerade as measured COGS.
  CATALOG='{"revision":"cat-internal-gpt-oss-120b-v1","models":[{"id":"openai/gpt-oss-120b","input_price_micro_per_mtok":900000,"output_price_micro_per_mtok":2700000,"capabilities":["chat"],"created":1788050671,"context_length":131072}]}'
fi

# Bind the deployer's own model, revision, canonical argv, context, price,
# readiness ordering, catalog, and exact H100 identity to the same local
# contract whose digest the OCI label authenticates.  This projection contains
# no key or credential and runs before release fetches or provider work.
if [ "$SERVING_PROFILE" = "gpt-oss-120b-h100-v1" ]; then
  PROFILE_CONTRACT_PROJECTION="$(python3 - \
    "$SERVING_PROFILE" "$SERVED_MODEL" "$MODEL_REV" "$TOKENIZER_REV" \
    "$MAX_CTX" "$VLLM_EXTRA_ARGS" "$MAX_GPU_RATE" "$READY_PROBE_BUDGET" \
    "$READY_PROBE_DELAY_SECS" "$DEFAULT_BOOTSTRAP_HARDWARE" "$CATALOG" <<'PY'
import json, sys
(
    profile, model, model_revision, tokenizer_revision, max_context,
    vllm_extra_args, max_gpu_rate, probe_budget, probe_delay,
    bootstrap_hardware, catalog,
) = sys.argv[1:]
print(json.dumps({
    "bootstrap_hardware": bootstrap_hardware,
    "catalog": catalog,
    "max_gpu_rate": max_gpu_rate,
    "max_context": max_context,
    "model_revision": model_revision,
    "probe_budget": probe_budget,
    "probe_delay_seconds": probe_delay,
    "served_model": model,
    "serving_profile": profile,
    "tokenizer_revision": tokenizer_revision,
    "vllm_extra_args": vllm_extra_args,
}, separators=(",", ":")))
PY
)"
  python3 "$SCRIPT_DIR/verify_serving_profile_contract.py" \
    "$RUNTIME_CONTRACT_PATH" "$PROFILE_CONTRACT_PROJECTION" >&2
fi

# The H100 image carries an OCI config label containing the exact contract hash
# baked at build time. After the local projection passes, resolve the
# digest-pinned public manifest and config, verify both content hashes, and
# compare the label before any trust-root fetch, storage call, or provider
# create. Render and fake-provider modes stay wholly local; the verifier has
# its own zero-network transport regression.
if [ "$SERVING_PROFILE" = "gpt-oss-120b-h100-v1" ] && \
   [ "$RENDER_ONLY" = "0" ] && [ "$DEPLOY_TEST" = "0" ]; then
  python3 "$SCRIPT_DIR/verify_oci_image_contract.py" \
    "$POD_IMAGE" "$RUNTIME_CONTRACT_SHA256"
fi

# Consistency check only — a sidecar mismatch means the release was
# tampered with or republished; refuse to proceed either way.  Every local
# profile/contract check above runs first, so drift costs no remote fetch.
for pair in "$GW_URL.sha256:$GW_SHA" "$WORKER_URL.sha256:$WORKER_SHA"; do
  url="${pair%:*}"; pinned="${pair##*:}"
  remote="$(curl -fsSL "$url" | awk '{print $1}')"
  [ "$remote" = "$pinned" ] || {
    echo "TRUST ROOT MISMATCH for $url: remote $remote, pinned $pinned" >&2
    exit 1
  }
done
echo "trust roots consistent (gateway ${GW_SHA:0:12}…, worker ${WORKER_SHA:0:12}…)" >&2

# ---------- assemble pod env (argv-passed: no quoting drift) ----------
ENV_JSON=$(APIKEYS="org-live:key-live:$(sha "$CUSTOMER_KEY")" \
  ADMIN_VER="$(sha "$ADMIN_KEY")" \
  WORKER_RING="pod-live:$(sha "$WORKER_KEY")" \
  RP_KEY="$(read_provider_key)" \
  GW_URL="$GW_URL" GW_SHA="$GW_SHA" WORKER_URL="$WORKER_URL" \
  WORKER_SHA="$WORKER_SHA" POD_IMAGE="$POD_IMAGE" \
  RUNTIME_CONTRACT_SHA256="$RUNTIME_CONTRACT_SHA256" \
  SERVING_PROFILE="$SERVING_PROFILE" SERVED_MODEL="$SERVED_MODEL" \
  MODEL_REV="$MODEL_REV" TOKENIZER_REV="$TOKENIZER_REV" CATALOG="$CATALOG" \
  MAX_CTX="$MAX_CTX" VLLM_EXTRA_ARGS="$VLLM_EXTRA_ARGS" \
  CONCURRENCY="$CONCURRENCY" ALLOC_CONF="$ALLOC_CONF" \
  POOL_NAME="$POOL_NAME" LEGACY_GPU_TYPE="$LEGACY_GPU_TYPE" \
  MIN_VRAM_GB="$MIN_VRAM_GB" MAX_GPU_RATE="$MAX_GPU_RATE" \
  POD_DISK_GB="$POD_DISK_GB" \
  BOOTSTRAP_HARDWARE="$BOOTSTRAP_HARDWARE" \
  DEMAND_QUIET_CYCLES="$DEMAND_QUIET_CYCLES" \
  READY_PROBE_BUDGET="$READY_PROBE_BUDGET" \
  READY_PROBE_DELAY_SECS="$READY_PROBE_DELAY_SECS" \
  BOOT_FRACTION="$BOOT_FRACTION" \
  POLICY_BOOT_SEED_SECS="$POLICY_BOOT_SEED_SECS" \
  POLICY_TOKENS_PER_SEC="$POLICY_TOKENS_PER_SEC" \
  CONTROLPLANE_STORAGE_MODE="$CONTROLPLANE_STORAGE_MODE" \
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
    # Bounded cold-start liveness allowance.  This is recorded separately
    # from the planner's boot prior and never widens a customer deadline.
    "WEINFER_PROBE_BUDGET": e["READY_PROBE_BUDGET"],
    "WEINFER_PROBE_DELAY_SECS": e["READY_PROBE_DELAY_SECS"],
    # Demand-aware retention defaults to three complete 60s empty-horizon
    # observations.  The registered A/B control may raise this value until
    # the gateway's one-boot parity cap becomes the effective TTL.
    "WEINFER_DEMAND_QUIET_CYCLES": e["DEMAND_QUIET_CYCLES"],
    # Evidence-only restatement. The container does not branch on this value;
    # preflight checks it in the separately rendered environment. No artifact
    # currently binds this restatement to the live deployment invocation.
    "WEINFER_CONTROLPLANE_STORAGE_MODE": e["CONTROLPLANE_STORAGE_MODE"],
    # Cold-pod backlog trigger only. Production is 0.2; registered deep-
    # backlog measurements must record any override as an explicit variable.
    "WEINFER_BOOT_FRACTION": e["BOOT_FRACTION"],
    "WEINFER_BOOT_SEED_SECS": e["POLICY_BOOT_SEED_SECS"],
    "WEINFER_POOL_TPS": e["POLICY_TOKENS_PER_SEC"],
    "WEINFER_RUNPOD_API_KEY": e["RP_KEY"],
    "WEINFER_WORKER_KEYS": e["WORKER_RING"],
    "WEINFER_WORKER_URL": e["WORKER_URL"],
    "WEINFER_WORKER_SHA256": e["WORKER_SHA"],
    "WEINFER_POOL": e["POOL_NAME"],
    # Required legacy spec field; every bootstrap create overrides it
    # with the selected exact SKU + live CUDA pin.
    "WEINFER_GPU_TYPE": e["LEGACY_GPU_TYPE"],
    "WEINFER_CLOUD": "COMMUNITY",
    "WEINFER_IMAGE": e["POD_IMAGE"],
    "WEINFER_SERVED_MODEL": e["SERVED_MODEL"],
    "WEINFER_MODEL_REVISION": e["MODEL_REV"],
    "WEINFER_TOKENIZER_REVISION": e["TOKENIZER_REV"],
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
    "WEINFER_MAX_GPU_RATE": e["MAX_GPU_RATE"],
    "VLLM_EXTRA_ARGS": e["VLLM_EXTRA_ARGS"],
    "WEINFER_CONCURRENCY": e["CONCURRENCY"],
    "PYTORCH_CUDA_ALLOC_CONF": e["ALLOC_CONF"],
    "WEINFER_POD_DISK_GB": e["POD_DISK_GB"],
    "WEINFER_POD_HTTP_PORT": "8000",
}
if e["SERVING_PROFILE"] == "gpt-oss-120b-h100-v1":
    # The H100 worker pod receives the exact image-side profile authority.  The
    # gateway adds served model and canonical argv from this same top-level
    # profile when it creates the pod; no runtime variable can widen it.  Qwen
    # deliberately emits none of these new keys so its frozen rendered map is
    # byte-identical to the pre-profile deployment.
    env["WEINFER_SERVING_PROFILE"] = e["SERVING_PROFILE"]
    env["WEINFER_RUNTIME_CONTRACT_SHA256"] = e["RUNTIME_CONTRACT_SHA256"]
    env["WEINFER_MIN_VRAM_GB"] = e["MIN_VRAM_GB"]
    env["WEINFER_POD_ENV"] = json.dumps(
        {
            "WEINFER_SERVING_PROFILE": e["SERVING_PROFILE"],
            "WEINFER_MODEL_REVISION": e["MODEL_REV"],
            "WEINFER_TOKENIZER_REVISION": e["TOKENIZER_REV"],
            "WEINFER_BACKEND_MAX_CONTEXT": e["MAX_CTX"],
        },
        separators=(",", ":"),
    )
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

# ---------- explicit storage substrate ----------
VOL_ID=""
if [ "$CONTROLPLANE_STORAGE_MODE" = "network-volume" ]; then
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
else
  [ -z "${VOLUME_ID:-}" ] || {
    echo "VOLUME_ID is incompatible with run-scoped-pod storage" >&2
    exit 1
  }
  [ -z "${DATACENTER:-}" ] || {
    echo "DATACENTER is incompatible with availability-priority run-scoped-pod storage" >&2
    exit 1
  }
  echo "== run-scoped Pod volume =="
  echo "using ${VOLUME_GB}GB at /workspace (survives restarts; deleted with Pod)"
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
  if [ "$CONTROLPLANE_STORAGE_MODE" = "network-volume" ]; then
    echo "LAUNCH FAILED — cleaning up (network volume kept as the durable asset)" >&2
  else
    echo "LAUNCH FAILED — cleaning up (run-scoped storage dies with the Pod)" >&2
  fi
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
CREATE_BODY=$(python3 - "$ENV_JSON" "$CP_IMAGE" "$VOL_ID" "$CP_NAME" "$CONTROLPLANE_STORAGE_MODE" "$VOLUME_GB" <<'PY_EOF'
import json, sys
env, image, vol, name, storage_mode, volume_gb = (
    json.loads(sys.argv[1]),
    sys.argv[2],
    sys.argv[3],
    sys.argv[4],
    sys.argv[5],
    int(sys.argv[6]),
)
body = {
    "name": name,
    "imageName": image,
    "computeType": "CPU",
    # Availability priority selects among every official 2-vCPU family. In
    # network-volume mode the volume fixes the data center; run-scoped mode can
    # use any provider-selected location. The returned rate remains subject to
    # the same exact $0.10 refusal/delete ceiling below.
    "cpuFlavorIds": ["cpu3c", "cpu5c", "cpu3g", "cpu5g", "cpu3m", "cpu5m"],
    "cpuFlavorPriority": "availability",
    "vcpuCount": 2,
    "containerDiskInGb": 10,
    "volumeMountPath": "/workspace",
    "ports": ["8080/http"],
    "env": env,
}
if storage_mode == "network-volume":
    if not vol:
        raise SystemExit("network-volume mode requires a volume id")
    body["networkVolumeId"] = vol
elif storage_mode == "run-scoped-pod":
    if vol:
        raise SystemExit("run-scoped-pod mode may not carry a network volume id")
    body["volumeInGb"] = volume_gb
else:
    raise SystemExit("unknown control-plane storage mode")
print(json.dumps(body))
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
if [ "$CONTROLPLANE_STORAGE_MODE" = "network-volume" ]; then
  echo "  storage:      network volume ${VOL_ID} (${VOLUME_GB}GB, survives Pod deletion)"
else
  echo "  storage:      run-scoped Pod volume (${VOLUME_GB}GB, deleted with Pod)"
fi
echo "  credentials:  ${CRED_FILE}"
echo "  retention:    demand_quiet_cycles=${DEMAND_QUIET_CYCLES}"
echo "  cold target:  boot_fraction=${BOOT_FRACTION}"
echo "  cold inputs:  boot_seed=${POLICY_BOOT_SEED_SECS}s policy_tps=${POLICY_TOKENS_PER_SEC}"
echo "  readiness:    ${READY_PROBE_BUDGET} probes x ${READY_PROBE_DELAY_SECS}s (bounded)"
if [ "$CONTROLPLANE_STORAGE_MODE" = "network-volume" ]; then
  echo "  ONGOING BURN: pod \$${POD_RATE:-?}/hr + network volume ~\$0.70/mo — founder-visible, deliberate"
else
  echo "  ONGOING BURN: pod \$${POD_RATE:-?}/hr with run-scoped storage — founder-visible, deliberate"
fi
echo
echo "next: CANARY_RUN_ID=<unique-run-id> CANARY_SERVING_PROFILE=${SERVING_PROFILE} scripts/canary_traversal.sh ${PUBLIC_BASE} ${CRED_FILE}"
