#!/usr/bin/env bash
# The rendered deployment must sell EXACTLY what the sealed evidence
# measured (codex 0163): one profile, generated from pair
# p1787432264's stacked A4500 arm, whose launch bytes match the
# probe's registered stacked configuration.  Any GPU / config / tps /
# boot / context mismatch REFUSES.  Zero spend, zero provider calls.
set -euo pipefail
cd "$(dirname "$0")/.."

# Overridable for the CI mirror layout (weinfer-pod/controlplane/ci/).
DEPLOY_SCRIPT="${DEPLOY_SCRIPT:-scripts/deploy_controlplane.sh}"
EVIDENCE_FILE="${EVIDENCE_FILE:-evidence/pair-p1787432264/pair_verdict.updated-1787440344.json}"
PROBE_FILE="${PROBE_FILE:-scripts/stacked_probe.sh}"

ADMIN_KEY=ci-admin CUSTOMER_KEY=ci-customer WORKER_KEY=ci-worker \
  bash "$DEPLOY_SCRIPT" --render-env 2>/dev/null > /tmp/evidence-env.json

EVIDENCE_FILE="$EVIDENCE_FILE" PROBE_FILE="$PROBE_FILE" python3 - <<'PY'
import json, math, os, re, sys

env = json.load(open("/tmp/evidence-env.json"))
verdict = json.load(open(os.environ["EVIDENCE_FILE"]))
arm = verdict["arms"]["stacked"]
probe = open(os.environ["PROBE_FILE"]).read()

fails = []
def check(name, ok, detail=""):
    if not ok:
        fails.append(f"{name}: {detail}")

# --- ONE profile, the sealed arm's identity ---
profiles = json.loads(env["WEINFER_PLACEMENT_PROFILES"])
check("single-profile", len(profiles) == 1,
      f"{len(profiles)} profiles rendered; only the sealed stacked A4500 arm is measured")
p = profiles[0]
check("gpu", p["identity"]["gpu_sku"] == arm["gpu"],
      f"profile {p['identity']['gpu_sku']!r} vs sealed {arm['gpu']!r}")
check("cuda", p["cuda_pin"] == [arm["cuda_pin"]],
      f"profile {p['cuda_pin']} vs sealed {arm['cuda_pin']!r}")

# --- tps and boot are THE ARM'S OWN numbers ---
tps = arm["tokens_per_sec_diagnostic"]
check("tps", p["tps_low"] == math.floor(tps) and p["tps_high"] == math.ceil(tps),
      f"profile [{p['tps_low']},{p['tps_high']}] vs sealed {tps}")
check("boot", p["boot_low_micros"] == p["boot_high_micros"] == arm["boot_secs"] * 1_000_000,
      f"profile [{p['boot_low_micros']},{p['boot_high_micros']}] vs sealed {arm['boot_secs']}s")
check("scopes", p["tps_scope"] == p["boot_scope"] == "SingleIdentity"
      and p["tps_evidence"] == p["fixed_evidence"] == "Measured", p)
check("observed-at", p["observed_at_epoch"] == verdict["observed_at_epoch"],
      f"{p['observed_at_epoch']} vs {verdict['observed_at_epoch']}")

# --- the rate is consistent with the sealed provisional charge ---
rate = p["rate_micro_per_hour"]
check("rate-pin", rate == 190_000, f"{rate} != the create-proven $0.19/hr")
wall = arm["load_done_epoch"] - arm["launch_epoch"]
implied = arm["charge_micro_usd"] / wall * 3600
check("rate-vs-charge", abs(implied - rate) / rate < 0.15,
      f"sealed charge implies {implied:.0f} micro/hr vs profile {rate}")

# --- registered launch bytes: the probe's stacked arm ---
pin = re.search(r'^PIN_FLAGS="(.+)"', probe, re.M).group(1)
stacked_line = re.search(r'^\s*VLLM_ARGS="\$PIN_FLAGS (.+)"', probe, re.M)
stacked = stacked_line.group(1)
after_stacked = probe[stacked_line.end():]
conc = re.search(r'^\s*WORKER_CONCURRENCY=(\d+)\s*$', after_stacked, re.M).group(1)
alloc = re.search(r'^\s*ALLOC_CONF="([^"]+)"', after_stacked, re.M).group(1)
model_rev = re.search(r'^MODEL_REV="([0-9a-f]{40})"', probe, re.M).group(1)

extra = env["VLLM_EXTRA_ARGS"]
for flag in stacked.split("--"):
    flag = flag.strip()
    if flag:
        check(f"flag:--{flag.split()[0]}", f"--{flag}".strip() in extra,
              f"registered stacked flag '--{flag}' missing from VLLM_EXTRA_ARGS {extra!r}")
seed = re.search(r"--seed (\d+)", pin)
check("seed", f"--seed {seed.group(1)}" in extra,
      f"registered '--seed {seed.group(1)}' missing from {extra!r}")
mml = int(re.search(r"--max-model-len (\d+)", pin).group(1))
check("max-model-len", p["max_context_tokens"] == mml,
      f"profile executes {p['max_context_tokens']} vs registered {mml}")
check("revision", env["WEINFER_MODEL_REVISION"] == env["WEINFER_TOKENIZER_REVISION"] == model_rev,
      f"env pins {env['WEINFER_MODEL_REVISION']} vs registered {model_rev}")
check("concurrency", env["WEINFER_CONCURRENCY"] == conc,
      f"env {env['WEINFER_CONCURRENCY']} vs registered {conc}")
check("alloc", env["PYTORCH_CUDA_ALLOC_CONF"] == alloc,
      f"env {env['PYTORCH_CUDA_ALLOC_CONF']!r} vs registered {alloc!r}")
check("cuda-env", env["WEINFER_CUDA_VERSIONS"] == arm["cuda_pin"],
      f"env {env['WEINFER_CUDA_VERSIONS']} vs sealed {arm['cuda_pin']}")

# --- the catalog sells no context beyond the executed bound ---
catalog = json.loads(env["WEINFER_MODEL_CATALOG"])
model = catalog["models"][0]
check("catalog-model", model["id"] == env["WEINFER_SERVED_MODEL"], model)
check("catalog-context", model["context_length"] <= p["max_context_tokens"],
      f"catalog sells {model['context_length']} over executed {p['max_context_tokens']}")

if fails:
    print("PROFILE EVIDENCE REGRESSION FAIL:")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("PROFILE EVIDENCE REGRESSION PASS: the deployment sells exactly what the sealed arm measured")
PY
