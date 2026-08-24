#!/usr/bin/env bash
# WeInfer control-plane entrypoint: durable Postgres + the gateway,
# one container.
#
# Required env:
#   WEINFER_GATEWAY_URL     static x86_64-linux gateway binary URL
#   WEINFER_GATEWAY_SHA256  pinned binary digest (refuse unverified)
#   ...plus the gateway's own required env (WEINFER_API_KEYS,
#   WEINFER_BACKEND_URL, and the managed-mode set when it owns a
#   pool).  WEINFER_DATABASE_URL is OWNED BY THIS SCRIPT — the
#   authoritative store is the loopback Postgres on the persistent
#   volume, never an external database.
# Optional:
#   WEINFER_PGDATA          Postgres data dir (default /workspace/pgdata
#                           — the RunPod volume mount, so state
#                           survives pod stop/start and image upgrades)
set -euo pipefail

: "${WEINFER_GATEWAY_URL:?WEINFER_GATEWAY_URL is required}"
: "${WEINFER_GATEWAY_SHA256:?WEINFER_GATEWAY_SHA256 is required}"
PGDATA_DIR="${WEINFER_PGDATA:-/workspace/pgdata}"

# The public callback base is DERIVED from the provider's own pod
# identity (codex 0162): RunPod injects RUNPOD_POD_ID, and the proxy
# URL is a pure function of it — no operator-supplied value to drift.
# An explicit WEINFER_PUBLIC_BASE still wins (CI, non-RunPod hosts).
if [ -z "${WEINFER_PUBLIC_BASE:-}" ] && [ -n "${RUNPOD_POD_ID:-}" ]; then
  export WEINFER_PUBLIC_BASE="https://${RUNPOD_POD_ID}-8080.proxy.runpod.net"
  echo "[entrypoint] derived WEINFER_PUBLIC_BASE=${WEINFER_PUBLIC_BASE}"
fi

echo "[entrypoint] fetching gateway binary from ${WEINFER_GATEWAY_URL}"
curl -fsSL --retry 5 --retry-delay 5 -o /usr/local/bin/weinfer-gateway \
  "$WEINFER_GATEWAY_URL"
GOT_SHA="$(sha256sum /usr/local/bin/weinfer-gateway | awk '{print $1}')"
if [ "$GOT_SHA" != "$WEINFER_GATEWAY_SHA256" ]; then
  echo "[entrypoint] sha256 mismatch: got ${GOT_SHA}, pinned ${WEINFER_GATEWAY_SHA256}" >&2
  exit 1
fi
chmod +x /usr/local/bin/weinfer-gateway
echo "[entrypoint] gateway binary verified"

# Durable Postgres on the persistent volume, LOOPBACK ONLY.  The
# provider proxy exposes 8080 (the gateway) and nothing else; local
# trust auth is confined to the container's own loopback.
mkdir -p "$PGDATA_DIR"
chown -R postgres:postgres "$PGDATA_DIR"
chmod 700 "$PGDATA_DIR"
# CI forces the live RunPod mount behavior: the provider filesystem
# reports uniform 0777 even after chmod succeeds. This flag is never
# present in production; it makes the zero-GPU container gate execute
# the same compatibility branch the real mount selects naturally.
if [ "${WEINFER_TEST_PGDATA_WORLD_MODE:-0}" = "1" ]; then
  chmod 777 "$PGDATA_DIR"
fi

PGDATA_MODE=$(stat -c '%a' "$PGDATA_DIR")
PGDATA_OWNER=$(stat -c '%u:%g' "$PGDATA_DIR")
PG_PRELOAD=()
case "$PGDATA_MODE" in
  700|750)
    echo "[entrypoint] pgdata permissions ${PGDATA_MODE} (${PGDATA_OWNER}) accepted natively"
    ;;
  *)
    # RunPod Network Volumes report uniform permission bits even when
    # chmod returns success. PostgreSQL refuses that mount before it
    # can initialize. Load the exact-path stat shim ONLY into
    # initdb/postgres; all bytes still go directly to the durable
    # volume and the gateway never loads the shim.
    PG_PRELOAD=(env
      LD_PRELOAD=/usr/local/lib/weinfer-pgdata-mode.so
      WEINFER_PGDATA="$PGDATA_DIR")
    echo "[entrypoint] pgdata permissions ${PGDATA_MODE} (${PGDATA_OWNER}); exact-path compatibility shim active"
    ;;
esac
POSTGRES_UID=$(id -u postgres)
POSTGRES_GID=$(id -g postgres)
PGDATA_PROCESS_VIEW=$(gosu postgres "${PG_PRELOAD[@]}" stat -c '%a:%u:%g' "$PGDATA_DIR")
case "$PGDATA_PROCESS_VIEW" in
  700:"${POSTGRES_UID}":"${POSTGRES_GID}"|750:"${POSTGRES_UID}":"${POSTGRES_GID}") ;;
  *)
    echo "[entrypoint] pgdata compatibility preflight failed: postgres sees ${PGDATA_PROCESS_VIEW}" >&2
    exit 1
    ;;
esac
echo "[entrypoint] postgres pgdata view ${PGDATA_PROCESS_VIEW} accepted"
if [ ! -s "$PGDATA_DIR/PG_VERSION" ]; then
  echo "[entrypoint] initializing Postgres data dir at ${PGDATA_DIR}"
  gosu postgres "${PG_PRELOAD[@]}" initdb -D "$PGDATA_DIR" --auth=trust >/dev/null
fi
echo "[entrypoint] starting Postgres (loopback only)"
gosu postgres "${PG_PRELOAD[@]}" postgres -D "$PGDATA_DIR" \
  -c listen_addresses=127.0.0.1 -c port=5432 &
PG_PID=$!

for i in $(seq 1 60); do
  if gosu postgres pg_isready -h 127.0.0.1 -p 5432 >/dev/null 2>&1; then
    break
  fi
  [ "$i" = "60" ] && { echo "[entrypoint] Postgres never became ready" >&2; exit 1; }
  sleep 1
done
gosu postgres psql -h 127.0.0.1 -tAc "SELECT 1 FROM pg_database WHERE datname='weinfer'" \
  | grep -q 1 || gosu postgres createdb -h 127.0.0.1 weinfer
echo "[entrypoint] Postgres ready; database 'weinfer' present"

# The gateway migrates the schema itself at connect (the REAL
# production path) and refuses to serve on any failure.
echo "[entrypoint] starting the gateway"
WEINFER_DATABASE_URL="postgres://postgres@127.0.0.1:5432/weinfer" \
  /usr/local/bin/weinfer-gateway &
GW_PID=$!

# Graceful stop: hand the signal to the gateway first (it drains,
# terminates owned pods, and provisionally settles), then Postgres.
term() {
  kill -TERM "$GW_PID" 2>/dev/null || true
  wait "$GW_PID" 2>/dev/null || true
  kill -TERM "$PG_PID" 2>/dev/null || true
  wait "$PG_PID" 2>/dev/null || true
  exit 0
}
trap term TERM INT

# If EITHER process dies, fail the container: a dead store must never
# leave a listening gateway, and a dead gateway must never bill idle.
wait -n $PG_PID $GW_PID
echo "[entrypoint] a component exited; failing the container" >&2
kill $PG_PID $GW_PID 2>/dev/null || true
exit 1
