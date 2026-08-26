#!/usr/bin/env bash
# Free, idempotent post-batch profile/economics collector.
#
# Usage:
#   scripts/public_batch_profile_collect.sh <public-base> \
#       <controlplane-credentials-file> <batch-run-id> [out-root]
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 scripts/public_batch_profile_collect.py "$@"
