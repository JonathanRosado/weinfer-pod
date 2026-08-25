#!/usr/bin/env bash
# Zero-dollar executable contract for scripts/profile_collect.sh.
set -euo pipefail
cd "$(dirname "$0")/.."
COLLECTOR="${COLLECTOR:-scripts/profile_collect.sh}"

TMP=$(mktemp -d "${TMPDIR:-/tmp}/weinfer-profile-regression.XXXXXX")
SERVER_PID=""
cleanup() {
  set +e
  [ -z "$SERVER_PID" ] || kill "$SERVER_PID" >/dev/null 2>&1
  [ -z "$SERVER_PID" ] || wait "$SERVER_PID" >/dev/null 2>&1
  rm -rf "$TMP"
}
trap cleanup EXIT

printf 'ok\n' > "$TMP/mode"
python3 - "$TMP/port" "$TMP/mode" <<'PY' &
import hashlib, http.server, json, sys
port_file, mode_file = sys.argv[1:3]
contract = {
    "version":"pod-launch-v1", "served_model":"Qwen/Qwen2.5-7B-Instruct",
    "model_revision":"rev", "tokenizer_revision":"rev",
    "image_digest":"image@sha256:" + "a"*64, "pod_args":"",
    "vllm_canonical_args":"--revision rev --tokenizer-revision rev --max-model-len 8192",
    "concurrency":"64", "allocator_config":"expandable_segments:True",
    "worker_sha256":"b"*64, "gpu_sku":"NVIDIA RTX A4500",
    "cuda_class":"12", "cuda_pin":["12.8"],
    "max_context_tokens":8192, "vram_gb":20,
}
contract_raw = json.dumps(contract, separators=(",", ":"))
digest = hashlib.sha256(contract_raw.encode()).hexdigest()
identity = {
    "served_model":contract["served_model"], "model_revision":"rev",
    "tokenizer_revision":"rev", "image_digest":contract["image_digest"],
    "engine_config_digest":"c"*64, "gpu_sku":contract["gpu_sku"],
    "cuda_class":"12",
}
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        mode=open(mode_file).read().strip()
        charge = 100 if mode == "pending" else 600
        allocated = 599 if mode == "bad-cost" else charge
        evidence = {
            "job_id":"job-reg", "logical_response_id":"resp-reg",
            "job_state":"completed", "job_failure":None, "lease_generation":1,
            "pod_id":"pod-reg", "pool":"pool-reg", "pod_state":"settled_provisional",
            "model":contract["served_model"], "gpu_sku":contract["gpu_sku"],
            "created_at_micros":1_000_000, "ready_at_micros":3_000_000,
            "completed_at_micros":5_000_000, "draining_at_micros":7_000_000,
            "terminate_requested_at_micros":8_000_000,
            "terminated_at_micros":9_000_000, "charged_at_micros":10_000_000,
            "settled_at_micros":11_000_000, "charge_micro_usd":charge,
            "allocated_cost_micro_usd":allocated,
            "lifetime_micros":0 if mode == "zero-lifetime" else 10_000_000,
            "launch_contract":contract_raw, "launch_contract_digest":digest,
            "provider_rate_micro_per_hour":190_000,
            "recent_provision_failures":2,
            "attempts":[{"attempt":1,"runtime_micros":1_000_000,
                         "physical_prompt_tokens":10,"physical_completion_tokens":2,
                         "billable":True,"needs_reconciliation":False}],
        }
        body=json.dumps({"object":"profile_evidence", "launch_contract_digest":digest,
                         "launch_contract":contract, "engine_config_digest":"c"*64,
                         "profile_identity":identity, "evidence":evidence}).encode()
        self.send_response(200); self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body)
server=http.server.HTTPServer(("127.0.0.1",0),H)
open(port_file,"w").write(str(server.server_port)); server.serve_forever()
PY
SERVER_PID=$!
for _ in $(seq 1 100); do [ -s "$TMP/port" ] && break; sleep 0.02; done
[ -s "$TMP/port" ] || { echo "profile fake did not start" >&2; exit 1; }
BASE="http://127.0.0.1:$(cat "$TMP/port")"
printf 'WEINFER_ADMIN_KEY=admin-reg\n' > "$TMP/creds.env"
mkdir -p "$TMP/home/.weinfer/canary-reg"
printf '{"run_id":"reg","job_id":"job-reg"}\n' > "$TMP/home/.weinfer/canary-reg/run.json"

HOME="$TMP/home" "$COLLECTOR" "$BASE" "$TMP/creds.env" reg "$TMP/out" \
  > "$TMP/ok.log"
SNAP=$(find "$TMP/out" -mindepth 1 -maxdepth 1 -type d | head -1)
python3 - "$SNAP" <<'PY'
import json, os, sys
r=sys.argv[1]; c=json.load(open(os.path.join(r,"profile_candidate.json")))
d=c["derivation"]
assert d["charge_micro_usd"] == d["allocated_cost_micro_usd"] == 600
assert d["provider_pre_adopt_micros"] == 2_000_000
assert d["boot_micros"] == 2_000_000
assert d["activation_micros"] == 4_000_000
assert d["pre_service_idle_micros"] == 1_000_000
assert d["serving_micros"] == 1_000_000
assert d["retained_idle_micros"] == 2_000_000
assert d["drain_micros"] == 2_000_000
assert d["time_conservation"] == d["lifetime_micros"] == 10_000_000
assert c["profile_facts"]["rate_micro_per_hour"] == 216_000
assert c["profile_facts"]["tps_low"] == c["profile_facts"]["tps_high"] == 12
assert c["profile_facts"]["boot_low_micros"] == c["profile_facts"]["boot_high_micros"] == 4_000_000
assert c["profile_facts"]["recent_acquisition_failures"] == 2
PY

# Pre-0040 live shape: provider billing arrived after the exact pod
# resource was 404, so the stored lifetime is zero.  A sealed watchdog
# provider-createdAt observation restores the exact decomposition and
# must travel in the manifest.
printf 'zero-lifetime\n' > "$TMP/mode"
printf '{"pod-reg":{"created":0.5,"last_seen":8.0,"terminal_at":8.0,"rate":0.19}}\n' \
  > "$TMP/provider-observation.json"
HOME="$TMP/home" "$COLLECTOR" "$BASE" "$TMP/creds.env" reg "$TMP/zero" \
  "$TMP/provider-observation.json" > "$TMP/zero.log"
ZERO_SNAP=$(find "$TMP/zero" -mindepth 1 -maxdepth 1 -type d | head -1)
python3 - "$ZERO_SNAP" <<'PY'
import json, os, sys
r=sys.argv[1]
c=json.load(open(os.path.join(r,"profile_candidate.json")))
d=c["derivation"]
assert d["lifetime_source"] == "sealed_watchdog_provider_created_at"
assert d["provider_created_at_micros"] == 500_000
assert d["lifetime_micros"] == d["time_conservation"] == 8_500_000
assert d["provider_pre_adopt_micros"] == 500_000
assert d["provider_observation_fields_used"] == ["created", "rate"]
assert d["provider_observation_fields_ignored"] == ["last_seen", "terminal_at"]
assert os.path.exists(os.path.join(r,"provider_observation.json"))
m=json.load(open(os.path.join(r,"MANIFEST.json")))
assert "provider_observation.json" in m["files"]
PY

printf 'bad-cost\n' > "$TMP/mode"
set +e
HOME="$TMP/home" "$COLLECTOR" "$BASE" "$TMP/creds.env" reg "$TMP/bad" \
  > "$TMP/bad.log" 2>&1
BAD=$?
set -e
[ "$BAD" != "0" ] || { echo "cost mismatch false-green" >&2; exit 1; }

printf 'pending\n' > "$TMP/mode"
set +e
HOME="$TMP/home" "$COLLECTOR" "$BASE" "$TMP/creds.env" reg "$TMP/pending" \
  > "$TMP/pending.log" 2>&1
PENDING=$?
set -e
[ "$PENDING" = "2" ] || { echo "pending charge returned $PENDING, expected 2" >&2; exit 1; }
PENDING_MANIFEST=$(find "$TMP/pending" -name MANIFEST.json -print -quit)
[ -n "$PENDING_MANIFEST" ] || { echo "pending manifest missing" >&2; exit 1; }
grep -q '"status": "pending"' "$PENDING_MANIFEST"

echo "PROFILE COLLECT REGRESSION PASS: exact phases + provider-createdAt fallback + money conservation; bad cost red; pending is resumable"
