#!/usr/bin/env bash
# WeInfer managed-pod entrypoint: vLLM + the pull worker, one container.
#
# Required env (all injected by the pool manager at pod create):
#   WEINFER_GATEWAY_BASE   gateway URL (outbound HTTPS; the worker's
#                          registration handshake IS pod readiness)
#   WEINFER_WORKER_KEY     ONE-TIME pod credential (identity source)
#   WEINFER_SERVED_MODEL   model id (also passed to vLLM)
#   WEINFER_WORKER_URL     static x86_64-linux worker binary URL
#   WEINFER_WORKER_SHA256  pinned binary digest (refuse unverified)
#   WEINFER_SERVING_PROFILE exact named runtime contract
#   WEINFER_MODEL_REVISION exact model commit
#   WEINFER_TOKENIZER_REVISION exact tokenizer commit
#   WEINFER_BACKEND_MAX_CONTEXT exact executed/sold context
# Optional:
#   VLLM_EXTRA_ARGS        extra vLLM flags (the stacked knobs)
#   WEINFER_CONCURRENCY    worker jobs in flight (default 8)
set -euo pipefail

case "${WEINFER_SERVING_PROFILE:-}" in
  qwen7b-consumer-v1)
    ENGINE_READY_TIMEOUT_SECONDS=3600
    ;;
  gpt-oss-120b-h100-v1)
    # 1,200s at the profile's $2.70/hr maximum is $0.90, so image
    # readiness itself loses before the transaction's $1 GPU ceiling.
    ENGINE_READY_TIMEOUT_SECONDS=1200
    # These values select kernels; they are execution inputs, not
    # descriptive labels.  The runtime verifier resolves the backend
    # on the actual GPU before either paid process starts.
    export CUDA_VISIBLE_DEVICES=0
    export FLASHINFER_DISABLE_JIT=1
    export FLASHINFER_WORKSPACE_BASE=/tmp/weinfer-flashinfer-runtime
    export VLLM_USE_V1=1
    export VLLM_ATTENTION_BACKEND=FLASH_ATTN
    export VLLM_FLASH_ATTN_VERSION=3
    export VLLM_USE_FLASHINFER_MOE_MXFP4_BF16=1
    ;;
  "")
    echo "WEINFER_SERVING_PROFILE is required" >&2
    exit 2
    ;;
  *)
    echo "unknown WEINFER_SERVING_PROFILE" >&2
    exit 2
    ;;
esac
readonly ENGINE_READY_TIMEOUT_SECONDS

python3 /weinfer/runtime/verify_runtime.py --runtime

echo "[entrypoint] fetching worker binary from ${WEINFER_WORKER_URL}"
# python3 is guaranteed in the vLLM image; curl is not.  Fetch and
# verify in one step — the binary never executes unverified.
python3 - "$WEINFER_WORKER_URL" "$WEINFER_WORKER_SHA256" /usr/local/bin/weinfer-worker <<'PY'
import hashlib, sys, urllib.request
url, expected, dest = sys.argv[1:4]
data = urllib.request.urlopen(url, timeout=180).read()
digest = hashlib.sha256(data).hexdigest()
if digest != expected:
    sys.exit(f"sha256 mismatch: got {digest}, pinned {expected}")
with open(dest, "wb") as f:
    f.write(data)
PY
chmod +x /usr/local/bin/weinfer-worker
echo "[entrypoint] worker binary verified"

echo "[entrypoint] starting vLLM (${WEINFER_SERVED_MODEL})"
# Loopback only: the engine is NEVER reachable from outside the pod.
# The gateway emits a whitespace-tokenized canonical vector.  Reading it
# into an array preserves those tokens and prevents shell wildcard expansion
# (notably the registered `original/*` and `metal/*` ignore patterns).
read -r -a VLLM_ARGS <<< "${VLLM_EXTRA_ARGS:-}"
VLLM_PID=""
WORKER_PID=""
cleanup() {
  [[ -n "$VLLM_PID" ]] && kill "$VLLM_PID" 2>/dev/null || true
  [[ -n "$WORKER_PID" ]] && kill "$WORKER_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM
python3 -m vllm.entrypoints.openai.api_server \
  --model "$WEINFER_SERVED_MODEL" \
  --host 127.0.0.1 --port 8000 \
  "${VLLM_ARGS[@]}" &
VLLM_PID=$!

# The worker's registration handshake is the manager's paid readiness
# authority. Hold it until the engine is healthy AND a second verifier has
# inspected the completed boot. This makes "no runtime compilation" an
# observation after boot as well as a source-level impossibility before it.
python3 /weinfer/runtime/wait_for_engine.py \
  --pid "$VLLM_PID" --timeout-seconds "$ENGINE_READY_TIMEOUT_SECONDS"
python3 /weinfer/runtime/verify_runtime.py --post-engine

echo "[entrypoint] starting the pull worker"
WEINFER_ENGINE_BASE="http://127.0.0.1:8000" \
  /usr/local/bin/weinfer-worker &
WORKER_PID=$!

# If EITHER process dies, fail the container: the manager's silent-pod
# reaper then terminates the pod instead of billing idle.
set +e
wait -n "$VLLM_PID" "$WORKER_PID"
set -e
echo "[entrypoint] a component exited; failing the container" >&2
exit 1
