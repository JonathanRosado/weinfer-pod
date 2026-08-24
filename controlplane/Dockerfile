# WeInfer control-plane image: durable Postgres + the WeInfer gateway,
# one container, built for a cheap CPU pod behind the provider's TLS
# proxy.
#
# Same never-rebuild discipline as the worker pod image: the gateway
# binary is fetched at boot from a release URL pinned by sha256 env,
# so gateway iterations never rebuild or repush this image.  Postgres
# state lives on the pod's persistent volume (WEINFER_PGDATA), so a
# pod restart or image upgrade keeps every customer row.
FROM debian:bookworm-slim AS pgdata-mode-shim

RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pgdata_mode_shim.c /src/pgdata_mode_shim.c
RUN gcc -shared -fPIC -O2 -Wall -Wextra -Werror \
    -o /weinfer-pgdata-mode.so /src/pgdata_mode_shim.c -ldl

FROM postgres:16-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /weinfer/entrypoint.sh
COPY --from=pgdata-mode-shim /weinfer-pgdata-mode.so /usr/local/lib/weinfer-pgdata-mode.so
RUN chmod +x /weinfer/entrypoint.sh

# The gateway's public listener.  TLS terminates at the provider proxy
# (https://<pod-id>-8080.proxy.runpod.net); Postgres binds loopback
# only and is NEVER reachable from outside the container.
EXPOSE 8080

ENTRYPOINT ["/bin/bash", "/weinfer/entrypoint.sh"]
