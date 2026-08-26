#!/usr/bin/env bash
# Zero-capacity executable contract for the public 300-job driver and
# pod-wide profile collector.  The fake API deliberately completes only
# after every job has been accepted, making the exact 253,800-micro hold
# observable before settlement.
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 scripts/public_batch_canary_regression.py
