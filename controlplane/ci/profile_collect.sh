#!/usr/bin/env bash
# FREE, idempotent post-canary profile collector.
#
# Fetches the admin-gated immutable launch/lifecycle/usage evidence for
# one run, validates all conservation identities, and writes an
# append-only raw snapshot + a machine-readable ProfileFacts candidate.
# It never creates capacity and never reads the provider key.
#
# Usage:
#   scripts/profile_collect.sh <public-base> <credentials-file> <run-id> [out-root]
# Exit 2 means the pod charge is still pending/accruing; rerun later.
set -euo pipefail

BASE="${1:?public base URL required}"
CRED_FILE="${2:?credentials file required}"
RUN_ID="${3:?canary run id required}"
OUT_ROOT="${4:-${HOME}/.weinfer/canary-${RUN_ID}/profile-evidence}"
RUN_FILE="${CANARY_ARTIFACT_DIR:-${HOME}/.weinfer/canary-${RUN_ID}}/run.json"
ADMIN_KEY=$(awk -F= '/^WEINFER_ADMIN_KEY=/{print $2}' "$CRED_FILE" | awk '{print $1}')
[ -n "$ADMIN_KEY" ] || { echo "admin key missing from $CRED_FILE" >&2; exit 1; }
[ -f "$RUN_FILE" ] || { echo "canary run pointer missing: $RUN_FILE" >&2; exit 1; }
JOB_ID=$(python3 -c "import json,sys; r=json.load(open(sys.argv[1])); assert r['run_id']==sys.argv[2]; print(r['job_id'])" "$RUN_FILE" "$RUN_ID")

mkdir -p "$OUT_ROOT"; chmod 700 "$OUT_ROOT"
TMP=$(mktemp -d "${TMPDIR:-/tmp}/weinfer-profile.XXXXXX")
trap 'rm -rf "$TMP"' EXIT

curl -fsS --connect-timeout 10 --max-time 30 \
  "$BASE/admin/jobs/$JOB_ID/profile-evidence" \
  -H "Authorization: Bearer $ADMIN_KEY" > "$TMP/raw.json"

set +e
python3 - "$TMP/raw.json" "$TMP/candidate.json" "$TMP/summary.json" <<'PY'
import hashlib, json, math, sys, time
raw_path, candidate_path, summary_path = sys.argv[1:4]
raw = json.load(open(raw_path))
assert raw["object"] == "profile_evidence", raw
e = raw["evidence"]
c = raw["launch_contract"]

def pending(reason):
    print(f"PROFILE EVIDENCE PENDING: {reason}", file=sys.stderr)
    raise SystemExit(2)

if e["job_state"] != "completed": pending(f"job state {e['job_state']}")
if e["pod_state"] not in ("charged", "settled_provisional", "settled"):
    pending(f"pod state {e['pod_state']} (termination/billing not settled yet)")
required = ["created_at_micros", "ready_at_micros", "completed_at_micros",
            "draining_at_micros", "terminate_requested_at_micros",
            "terminated_at_micros", "charged_at_micros", "settled_at_micros",
            "charge_micro_usd", "lifetime_micros", "provider_rate_micro_per_hour"]
missing = [k for k in required if e.get(k) is None]
if missing: pending(f"missing lifecycle facts: {missing}")

# The stored JSON bytes, not a reconstructed dict, are the digest
# authority.  Server-side validation performs the same comparison.
stored_contract = e["launch_contract"].encode()
assert hashlib.sha256(stored_contract).hexdigest() == raw["launch_contract_digest"]
assert raw["profile_identity"]["gpu_sku"] == e["gpu_sku"] == c["gpu_sku"]
assert raw["profile_identity"]["served_model"] == e["model"] == c["served_model"]

created, ready, completed, draining, terminate_requested, terminated, charged, settled = (
    int(e[k]) for k in ("created_at_micros", "ready_at_micros",
                        "completed_at_micros", "draining_at_micros",
                        "terminate_requested_at_micros", "terminated_at_micros",
                        "charged_at_micros", "settled_at_micros"))
charge = int(e["charge_micro_usd"])
allocated = int(e["allocated_cost_micro_usd"])
lifetime = int(e["lifetime_micros"])
provider_rate = int(e["provider_rate_micro_per_hour"])
assert 0 < created <= ready <= completed <= draining <= terminate_requested <= terminated
assert terminated <= charged <= settled
assert charge > 0 and lifetime > 0 and provider_rate > 0
assert allocated == charge, (allocated, charge, "provider charge must conserve into the ledger")

attempts = e["attempts"]
assert attempts, "no attempt-exact served segment"
service_runtime = 0
tokens = 0
for a in attempts:
    assert a["runtime_micros"] > 0, a
    assert a["billable"] is True and a["needs_reconciliation"] is False, a
    assert a["physical_prompt_tokens"] is not None
    assert a["physical_completion_tokens"] is not None
    assert a["physical_prompt_tokens"] >= 0 and a["physical_completion_tokens"] >= 0
    service_runtime += int(a["runtime_micros"])
    tokens += int(a["physical_prompt_tokens"]) + int(a["physical_completion_tokens"])
assert tokens > 0 and service_runtime > 0

boot = ready - created
ready_to_completion = completed - ready
pre_service_idle = ready_to_completion - service_runtime
retained_idle = draining - completed
drain = terminated - draining
provider_pre_adopt = lifetime - (terminated - created)
assert pre_service_idle >= 0, (ready_to_completion, service_runtime,
    "completion wall time must cover served-attempt runtime")
assert provider_pre_adopt >= 0, (lifetime, terminated - created,
    "provider lifetime must cover the DB-observed lifecycle")
assert (provider_pre_adopt + boot + pre_service_idle + service_runtime
        + retained_idle + drain) == lifetime
settlement_visibility_lag = charged - terminated
settlement_commit = settled - charged
failed_generations = max(0, int(e["lease_generation"]) - len(attempts))
activation = provider_pre_adopt + boot

# Do not derive economics while the provisional bucket is visibly
# below the create-rate clock.  v2 has no finality marker, but this
# floor prevents an early partial amount from becoming a profile.
gpu_clock_cost = math.ceil(provider_rate * lifetime / 3_600_000_000)
if charge < gpu_clock_cost:
    pending(f"provider bucket still accruing: charge {charge} < rate-clock floor {gpu_clock_cost}")

# Effective lifecycle rate captures every provider dollar (GPU plus
# any provider-side fixed/disk increment), rounded upward so planning
# never understates this observation.
effective_rate = math.ceil(charge * 3_600_000_000 / lifetime)
tps_low = tokens * 1_000_000 // service_runtime
tps_high = math.ceil(tokens * 1_000_000 / service_runtime)
assert tps_low > 0
observed = int(time.time())
source = (f"ordinary canary {e['job_id']} / pod {e['pod_id']}; exact launch contract "
          f"{raw['launch_contract_digest']}; provider-v2 provisional charge; "
          "throughput is a single-request service observation, not a saturation claim; "
          "lifecycle phases are immutable DB/provider records")
facts = {
    "identity": raw["profile_identity"],
    "rate_micro_per_hour": effective_rate,
    "tps_low": tps_low,
    "tps_high": tps_high,
    "tps_evidence": "Measured",
    "tps_scope": "SingleIdentity",
    # Planner boot means provider-create request through READY.  The
    # provider lifetime starts before the durable adopt mark, so the
    # pre-adopt interval must travel with boot for both deadline and
    # delivered-cost planning.
    "boot_low_micros": activation,
    "boot_high_micros": activation,
    "drain_low_micros": drain,
    "drain_high_micros": drain,
    "fixed_evidence": "Measured",
    "boot_scope": "SingleIdentity",
    "source": source,
    "observed_at_epoch": observed,
    "vram_gb": int(c["vram_gb"]),
    "max_context_tokens": int(c["max_context_tokens"]),
    "catalog_available": True,
    "recent_acquisition_failures": 0,
    "cuda_pin": c["cuda_pin"],
}
candidate = {
    "status": "candidate_only",
    "charge_finality": "provisional",
    "promotion_note": "review before activation; one ordinary request is measured but not a saturation throughput sample",
    "profile_facts": facts,
    "derivation": {
        "provider_create_rate_micro_per_hour": provider_rate,
        "effective_lifecycle_rate_micro_per_hour": effective_rate,
        "charge_micro_usd": charge,
        "allocated_cost_micro_usd": allocated,
        "gpu_rate_clock_floor_micro_usd": gpu_clock_cost,
        "billable_tokens": tokens,
        "service_runtime_micros": service_runtime,
        "provider_pre_adopt_micros": provider_pre_adopt,
        "boot_micros": boot,
        "activation_micros": activation,
        "pre_service_idle_micros": pre_service_idle,
        "serving_micros": service_runtime,
        "retained_idle_micros": retained_idle,
        "drain_micros": drain,
        "failed_generations": failed_generations,
        "settlement_visibility_lag_micros": settlement_visibility_lag,
        "settlement_commit_micros": settlement_commit,
        "lifetime_micros": lifetime,
        "time_conservation": (provider_pre_adopt + boot + pre_service_idle
                              + service_runtime + retained_idle + drain),
        "cost_conservation": allocated,
    },
}
with open(candidate_path, "w") as f:
    json.dump(candidate, f, sort_keys=True, indent=2); f.write("\n")
with open(summary_path, "w") as f:
    json.dump({
        "job_id": e["job_id"], "pod_id": e["pod_id"], "pool": e["pool"],
        "charge_micro_usd": charge, "billable_tokens": tokens,
        "delivered_usd_per_mtok": charge / tokens,
        "provider_pre_adopt_micros": provider_pre_adopt,
        "boot_micros": boot, "activation_micros": activation,
        "pre_service_idle_micros": pre_service_idle,
        "service_runtime_micros": service_runtime,
        "retained_idle_micros": retained_idle, "drain_micros": drain,
        "failed_generations": failed_generations,
        "settlement_visibility_lag_micros": settlement_visibility_lag,
        "settlement_commit_micros": settlement_commit,
        "lifetime_micros": lifetime,
    }, f, sort_keys=True, indent=2); f.write("\n")
PY
STATUS=$?
set -e

OBS_EPOCH=$(python3 -c 'import time; print(time.time_ns())')
SNAP="${OUT_ROOT}/observation-${OBS_EPOCH}"
mkdir "$SNAP"; chmod 700 "$SNAP"
cp "$TMP/raw.json" "$SNAP/raw.json"
if [ "$STATUS" = "2" ]; then
  python3 - "$SNAP" <<'PY'
import hashlib, json, os, sys
root=sys.argv[1]; p=os.path.join(root,"raw.json")
json.dump({"files":{"raw.json":hashlib.sha256(open(p,"rb").read()).hexdigest()},
           "status":"pending"}, open(os.path.join(root,"MANIFEST.json"),"w"), indent=2)
PY
  echo "profile evidence pending; raw snapshot: $SNAP" >&2
  exit 2
fi
[ "$STATUS" = "0" ] || exit "$STATUS"
cp "$TMP/candidate.json" "$SNAP/profile_candidate.json"
cp "$TMP/summary.json" "$SNAP/summary.json"
python3 - "$SNAP" <<'PY'
import hashlib, json, os, sys
root=sys.argv[1]; names=["raw.json","profile_candidate.json","summary.json"]
manifest={"status":"candidate","files":{}}
for name in names:
    manifest["files"][name]=hashlib.sha256(open(os.path.join(root,name),"rb").read()).hexdigest()
with open(os.path.join(root,"MANIFEST.json"),"w") as f:
    json.dump(manifest,f,sort_keys=True,indent=2); f.write("\n")
PY
chmod 600 "$SNAP"/*
echo "PROFILE CANDIDATE SEALED: $SNAP"
cat "$SNAP/summary.json"
