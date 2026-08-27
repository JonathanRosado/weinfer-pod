#!/usr/bin/env bash
# Evidence-truth gate (codex 0163/0164): the deployment may only claim
# what the sealed record measured.
#
# The paid pair measured the worker-v0.1.0 identity; production runs
# worker-v0.6.0 — a DIFFERENT exact identity by the launch-contract
# digest's own authority.  Therefore:
#   (1) the rendered deployment must carry NO measured placement
#       profile; its explicit hardware queue may carry only identity
#       plus a typed hypothesis-only throughput PRIOR — never
#       tps_low/high, boot, rate, facts, or promotion evidence;
#   (2) the engine-side launch bytes may differ from the frozen run by
#       EXACTLY one registered product change: the positive valueless
#       --enable-prefix-caching flag. Historical effective cache state
#       is UNKNOWN because no sealed run captured engine metrics. The
#       new worker independently observes the effective vLLM config
#       before READY; argv is declaration, never runtime evidence;
#   (3) the run contract itself is immutable: sha256-pinned here and
#       recorded in the sealed MANIFEST.
# The gate self-tests its own detector with mutations before ruling.
# Zero spend, zero provider calls.
set -euo pipefail
cd "$(dirname "$0")/.."

DEPLOY_SCRIPT="${DEPLOY_SCRIPT:-scripts/deploy_controlplane.sh}"
CONTRACT_FILE="${CONTRACT_FILE:-evidence/pair-p1787432264/run_contract.stacked.json}"
CONTRACT_SHA="cbe07a73e5e60a1761937db267e9e37f6bdbe0ead1a2ee743235921015fbc2f3"

GOT_SHA=$(python3 -c "import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "$CONTRACT_FILE")
[ "$GOT_SHA" = "$CONTRACT_SHA" ] || {
  echo "RUN CONTRACT MUTATED: $GOT_SHA != pinned $CONTRACT_SHA" >&2
  exit 1
}

ADMIN_KEY=ci-admin CUSTOMER_KEY=ci-customer WORKER_KEY=ci-worker \
  bash "$DEPLOY_SCRIPT" --render-env 2>/dev/null > /tmp/evidence-env.json

CONTRACT_FILE="$CONTRACT_FILE" python3 - <<'PY'
import json, os, sys

env = json.load(open("/tmp/evidence-env.json"))
contract = json.load(open(os.environ["CONTRACT_FILE"]))

def canonical_pairs(argv):
    """Order-independent canonical form; duplicates and extras count."""
    tokens = argv.split()
    pairs, i = [], 0
    while i < len(tokens):
        flag = tokens[i]
        if i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
            pairs.append((flag, tokens[i + 1])); i += 2
        else:
            pairs.append((flag, "")); i += 1
    return sorted(pairs)

def production_canonical(env):
    """Reconstruct the canonical argv exactly as the gateway derives
    it: VLLM_EXTRA_ARGS + appended revision pins + --max-model-len
    from the static context authority."""
    rev = env["WEINFER_MODEL_REVISION"]
    return (f"{env['VLLM_EXTRA_ARGS'].strip()} --revision {rev} "
            f"--tokenizer-revision {rev} "
            f"--max-model-len {env['WEINFER_BACKEND_MAX_CONTEXT']}")

def evaluate(env, contract):
    fails = []
    def check(name, ok, detail=""):
        if not ok:
            fails.append(f"{name}: {detail}")
    # The exact rendered map is serialized into Docker's env-file format by
    # the release workflow. Embedded newlines would split a JSON value into
    # bogus environment assignments and prevent the production container from
    # starting, even though the JSON document itself parses successfully.
    check("docker-env-file-shape",
          all("\n" not in value and "\r" not in value
              for value in env.values() if isinstance(value, str)),
          "rendered string values must each fit on one physical env-file line")
    # (1) NO measured profile while the executable is unmeasured.
    prod_worker = env["WEINFER_WORKER_SHA256"]
    measured_worker = contract["worker_sha256"]
    if "WEINFER_PLACEMENT_PROFILES" in env:
        check("unmeasured-executable",
              prod_worker == measured_worker,
              f"profiles claim Measured facts but production worker {prod_worker[:12]} "
              f"!= measured worker {measured_worker[:12]} — a different exact identity")
    check("bootstrap-mode", env.get("WEINFER_BOOTSTRAP_MODE") == "1")
    try:
        bootstrap = json.loads(env["WEINFER_BOOTSTRAP_HARDWARE"])
    except Exception as error:
        bootstrap = []
        check("bootstrap-json", False, str(error))
    exact_keys = {
        "gpu_sku", "cuda_class", "vram_gb",
        "throughput_seed_tokens_per_sec", "throughput_seed_kind",
        "throughput_seed_source",
    }
    check("bootstrap-shape",
          bool(bootstrap) and all(set(row) == exact_keys for row in bootstrap),
          "unmeasured hardware may carry identity plus typed ordering prior only")
    expected = {
        "NVIDIA RTX A5000": 24,
        "NVIDIA RTX 4000 SFF Ada Generation": 20,
        "NVIDIA RTX A4500": 20,
        "NVIDIA RTX 4000 Ada Generation": 20,
        "NVIDIA GeForce RTX 3090": 24,
        "NVIDIA GeForce RTX 3090 Ti": 24,
        "NVIDIA RTX A6000": 48,
        "NVIDIA GeForce RTX 4090": 24,
        "NVIDIA A40": 48,
    }
    actual = {row.get("gpu_sku"): row.get("vram_gb") for row in bootstrap}
    check("bootstrap-set", actual == expected, f"{actual} != {expected}")
    check("bootstrap-cuda-class",
          all(row.get("cuda_class") == "12" for row in bootstrap))
    expected_seeds = {
        "NVIDIA RTX A5000": (4000, "policy_prior"),
        "NVIDIA RTX 4000 SFF Ada Generation":
            (2681, "traffic_observed_cross_identity"),
        "NVIDIA RTX A4500": (4161, "traffic_observed_cross_identity"),
        "NVIDIA RTX 4000 Ada Generation": (4000, "policy_prior"),
        "NVIDIA GeForce RTX 3090": (6000, "spec_derived"),
        "NVIDIA GeForce RTX 3090 Ti": (6700, "spec_derived"),
        "NVIDIA RTX A6000": (6500, "spec_derived"),
        "NVIDIA GeForce RTX 4090": (13900, "spec_derived"),
        "NVIDIA A40": (4000, "policy_prior"),
    }
    actual_seeds = {
        row.get("gpu_sku"): (
            row.get("throughput_seed_tokens_per_sec"),
            row.get("throughput_seed_kind"),
        ) for row in bootstrap
    }
    check("bootstrap-seed-map", actual_seeds == expected_seeds,
          f"{actual_seeds} != {expected_seeds}")
    check("bootstrap-seed-provenance",
          all(isinstance(row.get("throughput_seed_source"), str)
              and row["throughput_seed_source"].strip()
              and ((row["throughput_seed_kind"] == "traffic_observed_cross_identity"
                    and "workload_sha256=2392bb58" in row["throughput_seed_source"]
                    and "candidate_only" in row["throughput_seed_source"])
                   or (row["throughput_seed_kind"] !=
                       "traffic_observed_cross_identity"
                       and "no traffic observation" in
                           row["throughput_seed_source"]))
              for row in bootstrap),
          "traffic-backed and analytic/policy priors must remain visibly distinct")
    check("no-static-cuda-pin", "WEINFER_CUDA_VERSIONS" not in env,
          "bootstrap CUDA is selected from the live catalog per attempt")
    # (2) engine launch bytes: one exact registered delta, never a
    # permissive superset comparison.
    prod = canonical_pairs(production_canonical(env))
    sealed = canonical_pairs(contract["vllm_canonical_argv"])
    registered_delta = [("--enable-prefix-caching", "")]
    check("argv-registered-cache-delta", prod == sorted(sealed + registered_delta),
          f"production {prod} != sealed {sealed} + {registered_delta}")
    check("positive-prefix-cache-flag",
          prod.count(("--enable-prefix-caching", "")) == 1
          and all(flag != "--no-enable-prefix-caching" for flag, _ in prod),
          "production must declare exactly one positive valueless cache flag")
    check("image", env["WEINFER_IMAGE"] == contract["image"],
          f"{env['WEINFER_IMAGE']} != {contract['image']}")
    # A4500 remains one explicit hardware identity from the sealed
    # contract, but it no longer owns the whole deployment.  The other
    # eight SKUs are unmeasured candidates under the same engine bytes.
    check("sealed-hardware-present", actual.get(contract["gpu"]) == 20)
    check("sealed-cuda-class",
          all(str(pin).split(".", 1)[0] == "12" for pin in [contract["cuda_pin"]]))
    check("revision", env["WEINFER_MODEL_REVISION"] == contract["model_revision"])
    check("concurrency", env["WEINFER_CONCURRENCY"] == contract["concurrency"])
    check("alloc", env["PYTORCH_CUDA_ALLOC_CONF"] == contract["alloc_conf"])
    check("model", env["WEINFER_SERVED_MODEL"] == contract["model"])
    check("recording-mode", env.get("WEINFER_PROFILE_EVIDENCE") == "1",
          "ordinary canary must stamp its launch contract before create")
    # (3) the catalog sells no context beyond the executed bound.
    catalog = json.loads(env["WEINFER_MODEL_CATALOG"])
    ctx = int(env["WEINFER_BACKEND_MAX_CONTEXT"])
    check("catalog-context",
          all(m["context_length"] <= ctx for m in catalog["models"]),
          f"catalog sells beyond the executed {ctx}")
    return fails

# --- mutation self-test: the detector must catch what it claims ---
mut1 = dict(contract)
mut1["vllm_canonical_argv"] = contract["vllm_canonical_argv"] + " --enable-lora"
assert any("argv-registered-cache-delta" in f for f in evaluate(env, mut1)), \
    "DETECTOR BROKEN: an extra unmeasured flag passed"
mut2 = dict(env)
mut2["WEINFER_PLACEMENT_PROFILES"] = "[]"
assert any("unmeasured-executable" in f for f in evaluate(mut2, contract)), \
    "DETECTOR BROKEN: profiles under a different worker identity passed"
mut3 = dict(env)
bad_bootstrap = json.loads(env["WEINFER_BOOTSTRAP_HARDWARE"])
bad_bootstrap[0]["tps_low"] = 999999
mut3["WEINFER_BOOTSTRAP_HARDWARE"] = json.dumps(bad_bootstrap)
assert any("bootstrap-shape" in f for f in evaluate(mut3, contract)), \
    "DETECTOR BROKEN: unmeasured hardware smuggled performance facts"
mut4 = dict(env)
mut4["WEINFER_BOOTSTRAP_HARDWARE"] = "[\n]"
assert any("docker-env-file-shape" in f for f in evaluate(mut4, contract)), \
    "DETECTOR BROKEN: a multiline Docker env-file value passed"
mut5 = dict(env)
forged = json.loads(env["WEINFER_BOOTSTRAP_HARDWARE"])
forged[0]["throughput_seed_kind"] = "measured_exact_identity"
mut5["WEINFER_BOOTSTRAP_HARDWARE"] = json.dumps(forged)
assert any("bootstrap-seed-map" in f for f in evaluate(mut5, contract)), \
    "DETECTOR BROKEN: a bootstrap prior laundered itself as measured"
mut6 = dict(env)
mut6["VLLM_EXTRA_ARGS"] = env["VLLM_EXTRA_ARGS"].replace(
    " --enable-prefix-caching", "")
assert any("argv-registered-cache-delta" in f for f in evaluate(mut6, contract)), \
    "DETECTOR BROKEN: missing declared prefix caching passed"
mut7 = dict(env)
mut7["VLLM_EXTRA_ARGS"] = env["VLLM_EXTRA_ARGS"] + " --enable-prefix-caching"
assert any("argv-registered-cache-delta" in f for f in evaluate(mut7, contract)), \
    "DETECTOR BROKEN: duplicate prefix caching declaration passed"
mut8 = dict(env)
mut8["VLLM_EXTRA_ARGS"] = env["VLLM_EXTRA_ARGS"].replace(
    "--enable-prefix-caching", "--no-enable-prefix-caching")
assert any("positive-prefix-cache-flag" in f for f in evaluate(mut8, contract)), \
    "DETECTOR BROKEN: negative prefix caching declaration passed"

fails = evaluate(env, contract)
if fails:
    print("PROFILE EVIDENCE REGRESSION FAIL:")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("PROFILE EVIDENCE REGRESSION PASS: nine explicit unmeasured SKU identities, no "
      "economic claim; typed ordering priors cannot become Measured facts; launch bytes "
      "equal the frozen contract plus one registered positive cache flag; historical "
      "effective cache state UNKNOWN; detector self-test red")
PY
