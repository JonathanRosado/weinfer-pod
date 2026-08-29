#!/usr/bin/env bash
# Execute exactly one registered retention-pair window. The durable lock is
# intentionally never removed: a launchd restart cannot repeat provider work.
set -euo pipefail

INDEX="${1:?window index required}"
TARGET_EPOCH="${2:?target epoch required}"
FRESH_SUFFIX="${3:?fresh attempt suffix required}"
CAMPAIGN="${WEINFER_RETENTION_CAMPAIGN_ROOT:?campaign root required}"
OPERATOR="${WEINFER_RETENTION_OPERATOR:?operator path required}"
EXPECTED_OPERATOR_SHA256="${WEINFER_RETENTION_OPERATOR_SHA256:?operator sha required}"
MAX_LATENESS_SECONDS="${WEINFER_RETENTION_MAX_LATENESS_SECONDS:-300}"
NOW="${WEINFER_RETENTION_WINDOW_NOW:-$(date +%s)}"

case "$INDEX" in 2|3) ;; *) echo "window index must be 2 or 3" >&2; exit 1 ;; esac
case "$TARGET_EPOCH" in *[!0-9]*|'') echo "target epoch must be numeric" >&2; exit 1 ;; esac
case "$NOW" in *[!0-9]*|'') echo "window clock must be numeric" >&2; exit 1 ;; esac
case "$MAX_LATENESS_SECONDS" in
  *[!0-9]*|'') echo "maximum lateness must be numeric" >&2; exit 1 ;;
esac
case "$FRESH_SUFFIX" in
  *[!A-Za-z0-9_-]*|'') echo "fresh attempt suffix is invalid" >&2; exit 1 ;;
esac

mkdir -p "$CAMPAIGN"
chmod 700 "$CAMPAIGN"
LOCK="$CAMPAIGN/window-${INDEX}-${FRESH_SUFFIX}.lock"
RESULT="$CAMPAIGN/window-${INDEX}-${FRESH_SUFFIX}.result.json"
PAIR_ID="retpair-${TARGET_EPOCH}-a${INDEX}-${FRESH_SUFFIX}"
ROOT="$CAMPAIGN/attempt-${INDEX}-${PAIR_ID}"

write_result() {
  local status="$1" rc="$2" started="$3" ended="$4" detail="$5"
  python3 -c '
import json, os, sys
path, index, target, pair, root, status, rc, started, ended, detail = sys.argv[1:]
value = {
    "object": "retention_pair_window",
    "window_index": int(index),
    "target_epoch": int(target),
    "pair_id": pair,
    "attempt_root": root,
    "status": status,
    "operator_exit_code": int(rc),
    "started_epoch": int(started),
    "ended_epoch": int(ended),
    "lateness_seconds": max(0, int(started) - int(target)),
    "detail": detail,
}
tmp = path + ".tmp"
with open(tmp, "w") as handle:
    json.dump(value, handle, sort_keys=True, indent=2)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
os.chmod(tmp, 0o600)
os.replace(tmp, path)
' "$RESULT" "$INDEX" "$TARGET_EPOCH" "$PAIR_ID" "$ROOT" "$status" \
    "$rc" "$started" "$ended" "$detail"
}

# An operator can fail before its ordinary post-batch classifier runs. The
# window owns the retry-ladder contract, so it may normalize raw exit 1 to the
# capacity/no-spend exit 42 only from a complete, ordered deploy transcript:
# the create endpoint definitively refused capacity, cleanup ran, zero live
# pods were proven, and no created-pod or unresolved-cleanup marker exists.
# Anything missing or contradictory stays terminal. The proof is persisted
# beside the attempt so the raw operator exit is never erased by normalization.
normalize_clean_deploy_capacity_refusal() {
  local raw_rc="$1" arm log proof
  [ "$raw_rc" = "1" ] || return 1
  proof="$ROOT/deploy-capacity-classification.json"
  [ ! -e "$proof" ] || return 1
  for arm in arm-a arm-b; do
    log="$ROOT/$arm/deploy.log"
    [ -f "$log" ] || continue
    if python3 - "$log" "$proof" "$arm" "$PAIR_ID" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import re
import sys

log_path, proof_path, arm, pair_id = sys.argv[1:]
raw = Path(log_path).read_bytes()
if not raw or len(raw) > 1024 * 1024:
    raise SystemExit(1)
text = raw.decode("utf-8", errors="replace")
folded = text.casefold()
capacity = re.search(
    r"runpod post /pods http [45][0-9]{2}:.*(?:"
    r"there are no longer any instances available with the requested specifications"
    r"|no instances available)",
    folded,
)
failed = folded.find("launch failed — cleaning up (volume kept as the durable asset)")
zero_live = folded.find("zero-live verification: 0 live pods remain")
forbidden = (
    "cleanup failed",
    "cleanup unresolved",
    "pod may be live and billing",
    "state persisted:",
    "control plane live:",
)
created = re.search(r"(?m)^pod [a-z0-9]+ created at epoch [0-9]+$", folded)
if (
    capacity is None
    or failed < capacity.start()
    or zero_live < failed
    or any(marker in folded for marker in forbidden)
    or created is not None
):
    raise SystemExit(1)
value = {
    "object": "retention_pair_deploy_capacity_classification",
    "pair_id": pair_id,
    "arm": arm,
    "raw_operator_exit_code": 1,
    "normalized_exit_code": 42,
    "classification": "clean_deploy_time_capacity_refusal",
    "deploy_log_sha256": hashlib.sha256(raw).hexdigest(),
    "deploy_log_bytes": len(raw),
    "create_refusal_observed": True,
    "cleanup_completed": True,
    "zero_live_verified": True,
    "created_pod_observed": False,
    "unresolved_cleanup_observed": False,
}
temporary = proof_path + ".tmp"
with open(temporary, "w") as handle:
    json.dump(value, handle, sort_keys=True, indent=2)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
os.chmod(temporary, 0o600)
os.replace(temporary, proof_path)
PY
    then
      return 0
    fi
  done
  return 1
}

actual_operator_sha256=$(shasum -a 256 "$OPERATOR" 2>/dev/null | awk '{print $1}')
if [ "$actual_operator_sha256" != "$EXPECTED_OPERATOR_SHA256" ]; then
  echo "retention window refused: operator sha ${actual_operator_sha256:-missing} != ${EXPECTED_OPERATOR_SHA256}" >&2
  exit 1
fi

if ! mkdir "$LOCK" 2>/dev/null; then
  echo "retention window refused: durable lock already exists: $LOCK" >&2
  exit 1
fi
chmod 700 "$LOCK"
printf '%s\n' "$NOW" > "$LOCK/acquired_epoch"
chmod 600 "$LOCK/acquired_epoch"

if [ "$NOW" -lt "$TARGET_EPOCH" ]; then
  write_result "refused_early" 1 "$NOW" "$NOW" "calendar trigger arrived before its registered target"
  echo "retention window refused: trigger arrived before target" >&2
  exit 1
fi
lateness=$((NOW - TARGET_EPOCH))
if [ "$lateness" -gt "$MAX_LATENESS_SECONDS" ]; then
  write_result "missed_without_spend" 43 "$NOW" "$NOW" \
    "trigger was ${lateness}s late, beyond ${MAX_LATENESS_SECONDS}s ceiling; operator not invoked"
  echo "retention window missed without spend: ${lateness}s late" >&2
  exit 43
fi

if [ "$INDEX" = "3" ]; then
  prior_result="$CAMPAIGN/window-2-${FRESH_SUFFIX}.result.json"
  if [ ! -f "$prior_result" ]; then
    write_result "blocked_by_missing_prior_result" 1 "$NOW" "$NOW" \
      "window 2 has no durable result"
    echo "retention window 3 refused: window 2 result missing" >&2
    exit 1
  fi
  prior_rc=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["operator_exit_code"])' "$prior_result")
  case "$prior_rc" in
    42|43) ;;
    0)
      write_result "skipped_after_pair_success" 0 "$NOW" "$NOW" \
        "window 2 completed the pair"
      echo "retention window 3 skipped: prior pair succeeded"
      exit 0
      ;;
    *)
      write_result "blocked_by_prior_unexpected_failure" 1 "$NOW" "$NOW" \
        "window 2 exit ${prior_rc} is not a retryable capacity/no-spend result"
      echo "retention window 3 refused: prior exit ${prior_rc}" >&2
      exit 1
      ;;
  esac
fi

if [ -e "$ROOT" ]; then
  write_result "refused_existing_attempt_root" 1 "$NOW" "$NOW" \
    "fresh attempt root already exists"
  echo "retention window refused: attempt root already exists: $ROOT" >&2
  exit 1
fi

echo "[window] attempt ${INDEX} starts $(date -u +%FT%TZ): ${PAIR_ID}"
set +e
bash "$OPERATOR" "$PAIR_ID" "$ROOT"
raw_rc=$?
set -e
ended=$(date +%s)
rc="$raw_rc"
normalized_deploy_capacity=0
if normalize_clean_deploy_capacity_refusal "$raw_rc"; then
  rc=42
  normalized_deploy_capacity=1
fi
case "$rc" in
  0) status="pair_completed"; detail="both arms completed" ;;
  42)
    status="definitive_capacity_refusal"
    if [ "$normalized_deploy_capacity" = "1" ]; then
      detail="window proved clean deploy-time capacity refusal; raw operator exit 1; see deploy-capacity-classification.json"
    else
      detail="operator proved retryable create truth"
    fi
    ;;
  *) status="unexpected_failure"; detail="operator failure is terminal for the ladder" ;;
esac
write_result "$status" "$rc" "$NOW" "$ended" "$detail"
if [ "$rc" = "0" ]; then
  printf '%s\n' "$ROOT" > "$CAMPAIGN/success-root.txt"
  chmod 600 "$CAMPAIGN/success-root.txt"
elif [ "$rc" != "42" ]; then
  printf '%s\n' "$ROOT" > "$CAMPAIGN/diagnosis-root.txt"
  chmod 600 "$CAMPAIGN/diagnosis-root.txt"
fi
exit "$rc"
