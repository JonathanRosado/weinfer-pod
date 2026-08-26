#!/usr/bin/env bash
# One run-scoped, product-representative public batch canary.
#
# Usage:
#   BATCH_RUN_ID=<fresh-id> scripts/public_batch_canary.sh \
#       <public-base> <controlplane-credentials-file>
#
# The Python implementation owns the exact workload bytes, HTTP
# idempotency, pagination, token/charge validation, and artifact writes.
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 scripts/public_batch_canary.py "$@"
