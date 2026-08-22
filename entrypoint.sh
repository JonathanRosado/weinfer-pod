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
# Optional:
#   VLLM_EXTRA_ARGS        extra vLLM flags (the stacked knobs)
#   WEINFER_CONCURRENCY    worker jobs in flight (default 8)
set -euo pipefail

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
python3 -m vllm.entrypoints.openai.api_server \
  --model "$WEINFER_SERVED_MODEL" \
  --host 127.0.0.1 --port 8000 \
  ${VLLM_EXTRA_ARGS:-} &
VLLM_PID=$!

echo "[entrypoint] starting the pull worker"
WEINFER_ENGINE_BASE="http://127.0.0.1:8000" \
  /usr/local/bin/weinfer-worker &
WORKER_PID=$!

# If EITHER process dies, fail the container: the manager's silent-pod
# reaper then terminates the pod instead of billing idle.
wait -n $VLLM_PID $WORKER_PID
echo "[entrypoint] a component exited; failing the container" >&2
kill $VLLM_PID $WORKER_PID 2>/dev/null || true
exit 1
