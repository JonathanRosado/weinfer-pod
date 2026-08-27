#!/usr/bin/env bash
# Evidence-truth gate (codex 0163/0164): the deployment may only claim
# what the sealed record measured.
#
# The paid pair measured the worker-v0.1.0 identity; production runs
# worker-v0.6.0 — a DIFFERENT exact identity by the launch-contract
# digest's own authority.  Therefore:
#   (1) the rendered deployment must carry NO measured placement
#       profile; its explicit hardware queue may carry only identity
#       plus independent typed hypothesis-only throughput and fixed-cost
#       PRIORS — never tps_low/high, promoted boot, rate, facts, or
#       promotion evidence;
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

# The paid exact-identity selector may only narrow the default queue. It must
# preserve the selected row's typed prior/provenance exactly and may not forge
# a tenth identity. This render is zero-provider and never reads the real key.
ADMIN_KEY=ci-admin CUSTOMER_KEY=ci-customer WORKER_KEY=ci-worker \
  WEINFER_BOOTSTRAP_ONLY_GPU_SKU="NVIDIA GeForce RTX 4090" \
  bash "$DEPLOY_SCRIPT" --render-env 2>/dev/null > /tmp/evidence-env-4090.json

python3 - <<'PY'
import json

default = json.load(open("/tmp/evidence-env.json"))
targeted = json.load(open("/tmp/evidence-env-4090.json"))
default_rows = json.loads(default["WEINFER_BOOTSTRAP_HARDWARE"])
targeted_rows = json.loads(targeted["WEINFER_BOOTSTRAP_HARDWARE"])
expected = [
    row for row in default_rows
    if row["gpu_sku"] == "NVIDIA GeForce RTX 4090"
]
assert len(default_rows) == 9, default_rows
assert len(expected) == 1, expected
assert targeted_rows == expected, (targeted_rows, expected)
assert targeted_rows[0]["throughput_seed_tokens_per_sec"] == 9548
assert targeted_rows[0]["throughput_seed_kind"] == "traffic_observed_cross_identity"
assert "seed4090-1787834610" in targeted_rows[0]["throughput_seed_source"]
assert targeted_rows[0]["boot_seed_micros"] == 664034722
assert targeted_rows[0]["drain_seed_micros"] == 633859
assert targeted_rows[0]["fixed_seed_kind"] == "traffic_observed_cross_identity"
assert "seed4090-1787834610" in targeted_rows[0]["fixed_seed_source"]
PY

if ADMIN_KEY=ci-admin CUSTOMER_KEY=ci-customer WORKER_KEY=ci-worker \
  WEINFER_BOOTSTRAP_ONLY_GPU_SKU="NVIDIA Imaginary GPU" \
  bash "$DEPLOY_SCRIPT" --render-env >/tmp/evidence-env-invalid.json 2>/tmp/evidence-env-invalid.log; then
  echo "PROFILE EVIDENCE REGRESSION FAIL: unknown exact-identity selector passed" >&2
  exit 1
fi
grep -q "must name exactly one configured identity" /tmp/evidence-env-invalid.log || {
  echo "PROFILE EVIDENCE REGRESSION FAIL: unknown selector lacked named refusal" >&2
  exit 1
}

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
        "throughput_seed_source", "boot_seed_micros", "drain_seed_micros",
        "fixed_seed_kind", "fixed_seed_source",
    }
    check("bootstrap-shape",
          bool(bootstrap) and all(set(row) == exact_keys for row in bootstrap),
          "unmeasured hardware may carry identity plus independent typed ordering priors only")
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
        "NVIDIA RTX A5000": (2628, "policy_prior"),
        "NVIDIA RTX 4000 SFF Ada Generation":
            (2681, "traffic_observed_cross_identity"),
        "NVIDIA RTX A4500": (4161, "traffic_observed_cross_identity"),
        "NVIDIA RTX 4000 Ada Generation": (2628, "policy_prior"),
        "NVIDIA GeForce RTX 3090":
            (3943, "traffic_observed_cross_identity"),
        "NVIDIA GeForce RTX 3090 Ti": (4403, "spec_derived"),
        "NVIDIA RTX A6000": (4271, "spec_derived"),
        "NVIDIA GeForce RTX 4090":
            (9548, "traffic_observed_cross_identity"),
        "NVIDIA A40": (2628, "policy_prior"),
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
                    and "candidate_only" in row["throughput_seed_source"]
                    and "basis=" in row["throughput_seed_source"])
                   or (row["throughput_seed_kind"] !=
                       "traffic_observed_cross_identity"
                       and "no traffic observation" in
                           row["throughput_seed_source"]
                       and "basis=ready_window_tps_low" in
                           row["throughput_seed_source"]))
              for row in bootstrap),
          "traffic-backed and analytic/policy priors must remain visibly distinct")
    raw_unobserved_seeds = {
        "NVIDIA RTX A5000": 4000,
        "NVIDIA RTX 4000 Ada Generation": 4000,
        "NVIDIA GeForce RTX 3090 Ti": 6700,
        "NVIDIA RTX A6000": 6500,
        "NVIDIA A40": 4000,
    }
    throughput_sources = {
        row.get("gpu_sku"): row.get("throughput_seed_source", "")
        for row in bootstrap
    }
    check("bootstrap-ready-basis-calibration",
          all(actual_seeds.get(sku, (None, None))[0] == raw * 3943 // 6000
              and f"raw_seed={raw}" in throughput_sources.get(sku, "")
              and "effective=floor(raw_seed*3943/6000)" in
                  throughput_sources.get(sku, "")
              and "seed4090-1787834610" in throughput_sources.get(sku, "")
              and "seed3090-1787841661" in throughput_sources.get(sku, "")
              for sku, raw in raw_unobserved_seeds.items()),
          "every unobserved row must use the exact worst scored ready-window ratio")
    check("observed-throughput-undiscounted",
          {row["gpu_sku"]: row["throughput_seed_tokens_per_sec"]
           for row in bootstrap
           if row["throughput_seed_kind"] ==
              "traffic_observed_cross_identity"} == {
              "NVIDIA RTX 4000 SFF Ada Generation": 2681,
              "NVIDIA RTX A4500": 4161,
              "NVIDIA GeForce RTX 3090": 3943,
              "NVIDIA GeForce RTX 4090": 9548,
          },
          "the calibration may never haircut a traffic-observed row")
    expected_fixed = {
        "NVIDIA RTX A5000": (664034722, 740377, "policy_prior"),
        "NVIDIA RTX 4000 SFF Ada Generation":
            (492992942, 685232, "traffic_observed_cross_identity"),
        "NVIDIA RTX A4500":
            (429080126, 731031, "traffic_observed_cross_identity"),
        "NVIDIA RTX 4000 Ada Generation": (664034722, 740377, "policy_prior"),
        "NVIDIA GeForce RTX 3090":
            (462995788, 740377, "traffic_observed_cross_identity"),
        "NVIDIA GeForce RTX 3090 Ti": (664034722, 740377, "policy_prior"),
        "NVIDIA RTX A6000": (664034722, 740377, "policy_prior"),
        "NVIDIA GeForce RTX 4090":
            (664034722, 633859, "traffic_observed_cross_identity"),
        "NVIDIA A40": (664034722, 740377, "policy_prior"),
    }
    actual_fixed = {
        row.get("gpu_sku"): (
            row.get("boot_seed_micros"), row.get("drain_seed_micros"),
            row.get("fixed_seed_kind"),
        ) for row in bootstrap
    }
    check("bootstrap-fixed-seed-map", actual_fixed == expected_fixed,
          f"{actual_fixed} != {expected_fixed}")
    check("bootstrap-fixed-seed-provenance",
          all(isinstance(row.get("fixed_seed_source"), str)
              and row["fixed_seed_source"].strip()
              and ((row["fixed_seed_kind"] == "traffic_observed_cross_identity"
                    and "candidate_only" in row["fixed_seed_source"])
                   or (row["fixed_seed_kind"] == "policy_prior"
                       and "no SKU traffic observation" in
                           row["fixed_seed_source"]))
              for row in bootstrap),
          "traffic-backed and policy fixed-cost priors must remain visibly distinct")
    unobserved = [
        row for row in bootstrap
        if row["throughput_seed_kind"] != "traffic_observed_cross_identity"
    ]
    check("unobserved-capacity-preserved", len(unobserved) == 5,
          "calibration must keep all five unobserved identities configured")
    check("unobserved-drain-maximum",
          all(row["drain_seed_micros"] == 740377
              and "batch-live-1787630415/A4500" in row["fixed_seed_source"]
              and "amort3full-1787755326/SFF Ada" in row["fixed_seed_source"]
              and "seed4090-1787834610/RTX 4090" in row["fixed_seed_source"]
              and "seed3090-1787841661/RTX 3090" in row["fixed_seed_source"]
              for row in unobserved),
          "the value and four-run maximum provenance must travel together")

    # Exact community create rates from seed3090-1787841661's immutable
    # pre-spend catalog, sha256 1250915f6e6d3fdb02eb55682bf5f41bfa0103844a069b0d56ef24c2e2463cb0.
    # They make this an evidence-backed counterfactual red, never a claim that
    # provider rates are static.
    rates = {
        "NVIDIA RTX A5000": 160000,
        "NVIDIA RTX 4000 SFF Ada Generation": 180000,
        "NVIDIA RTX A4500": 190000,
        "NVIDIA RTX 4000 Ada Generation": 200000,
        "NVIDIA GeForce RTX 3090": 220000,
        "NVIDIA GeForce RTX 3090 Ti": 270000,
        "NVIDIA RTX A6000": 330000,
        "NVIDIA GeForce RTX 4090": 340000,
        "NVIDIA A40": 350000,
    }
    delivered_batch_tokens = 1199508
    # The production ranker consumes the conservative chars/4 request-body
    # estimate, not terminal billable usage.  The frozen request is 21,533
    # compact JSON bytes, hence floor(21533/4)+64 = 5,447 tokens/job and
    # 1,634,100 tokens per 300-job batch.  Keep both bases explicit: the
    # delivered-token series is useful cost sensitivity, while only the
    # estimated-token series describes the live placement row.
    estimated_batch_tokens = 1634100
    def ordering_cost_at_tokens(row, tokens):
        tps = row["throughput_seed_tokens_per_sec"]
        serve = (tokens * 1000000 + tps - 1) // tps
        numerator = (rates[row["gpu_sku"]]
                     * (row["boot_seed_micros"] + serve
                        + row["drain_seed_micros"])
                     * 1000000)
        denominator = 3600000000 * tokens
        return (numerator + denominator - 1) // denominator
    delivered_proxy_leaders = {
        1: ("NVIDIA RTX A4500", 31596),
        3: ("NVIDIA RTX A4500", 18988),
        12: ("NVIDIA GeForce RTX 4090", 14253),
        24: ("NVIDIA GeForce RTX 4090", 12073),
    }
    live_estimated_leaders = {
        1: ("NVIDIA RTX A4500", 26566),
        3: ("NVIDIA RTX A4500", 17312),
        12: ("NVIDIA GeForce RTX 4090", 13093),
        24: ("NVIDIA GeForce RTX 4090", 11493),
    }
    if (set(row.get("gpu_sku") for row in bootstrap) == set(rates)
            and all(isinstance(row.get("throughput_seed_tokens_per_sec"), int)
                    and row["throughput_seed_tokens_per_sec"] > 0
                    and isinstance(row.get("boot_seed_micros"), int)
                    and isinstance(row.get("drain_seed_micros"), int)
                    for row in bootstrap)):
        delivered_ranked = {
            batches: sorted((
                ordering_cost_at_tokens(row, delivered_batch_tokens * batches),
                row["gpu_sku"], row)
                for row in bootstrap)
            for batches in delivered_proxy_leaders
        }
        live_ranked = {
            batches: sorted((
                ordering_cost_at_tokens(row, estimated_batch_tokens * batches),
                row["gpu_sku"], row)
                for row in bootstrap)
            for batches in live_estimated_leaders
        }
        check("observed-leader-at-delivered-token-proxies",
              all((delivered_ranked[batches][0][1],
                   delivered_ranked[batches][0][0]) == expected
                  and delivered_ranked[batches][0][2]["throughput_seed_kind"] ==
                      "traffic_observed_cross_identity"
                  for batches, expected in delivered_proxy_leaders.items()),
              {batches: [(cost, sku) for cost, sku, _ in rows[:4]]
               for batches, rows in delivered_ranked.items()})
        check("observed-leader-at-live-estimated-backlogs",
              all((live_ranked[batches][0][1],
                   live_ranked[batches][0][0]) == expected
                  and live_ranked[batches][0][2]["throughput_seed_kind"] ==
                      "traffic_observed_cross_identity"
                  for batches, expected in live_estimated_leaders.items()),
              {batches: [(cost, sku) for cost, sku, _ in rows[:4]]
               for batches, rows in live_ranked.items()})
        a5000 = next(row for row in bootstrap
                     if row["gpu_sku"] == "NVIDIA RTX A5000")
        raw_a5000 = dict(a5000)
        raw_a5000["throughput_seed_tokens_per_sec"] = 4000
        raw_cost = ordering_cost_at_tokens(
            raw_a5000, 12 * estimated_batch_tokens)
        calibrated_cost = ordering_cost_at_tokens(
            a5000, 12 * estimated_batch_tokens)
        check("calibration-omission-red",
              raw_cost == 12618
              and calibrated_cost == 18419
              and raw_cost < live_ranked[12][0][0]
              and calibrated_cost > live_ranked[12][2][0],
              "without calibration the unobserved A5000 would lead at the live 12-batch estimated backlog")
    else:
        check("observed-leader-at-delivered-token-proxies", False,
              "exact nine-row typed input is required")
        check("observed-leader-at-live-estimated-backlogs", False,
              "exact nine-row typed input is required")
        check("calibration-omission-red", False,
              "exact nine-row typed input is required")
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
mut9 = dict(env)
forged_fixed = json.loads(env["WEINFER_BOOTSTRAP_HARDWARE"])
forged_fixed[0]["fixed_seed_kind"] = "measured_exact_identity"
mut9["WEINFER_BOOTSTRAP_HARDWARE"] = json.dumps(forged_fixed)
assert any("bootstrap-fixed-seed-map" in f for f in evaluate(mut9, contract)), \
    "DETECTOR BROKEN: a bootstrap fixed-cost prior laundered itself as measured"
mut10 = dict(env)
forged_observed = json.loads(env["WEINFER_BOOTSTRAP_HARDWARE"])
forged_observed[0]["throughput_seed_kind"] = "traffic_observed_cross_identity"
forged_observed[0]["throughput_seed_source"] = (
    "fabricated sealed source; basis=ready_window_tps_low; "
    "workload_sha256=2392bb58; candidate_only 1/5 boots"
)
mut10["WEINFER_BOOTSTRAP_HARDWARE"] = json.dumps(forged_observed)
assert any("bootstrap-seed-map" in f for f in evaluate(mut10, contract)), \
    "DETECTOR BROKEN: an unobserved SKU escaped calibration through fake traffic provenance"
mut11 = dict(env)
discounted_observed = json.loads(env["WEINFER_BOOTSTRAP_HARDWARE"])
for row in discounted_observed:
    if row["gpu_sku"] == "NVIDIA GeForce RTX 3090":
        row["throughput_seed_tokens_per_sec"] = 2591
mut11["WEINFER_BOOTSTRAP_HARDWARE"] = json.dumps(discounted_observed)
assert any("observed-throughput-undiscounted" in f
           for f in evaluate(mut11, contract)), \
    "DETECTOR BROKEN: the calibration was applied to an observed row"

fails = evaluate(env, contract)
if fails:
    print("PROFILE EVIDENCE REGRESSION FAIL:")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("PROFILE EVIDENCE REGRESSION PASS: nine explicit unmeasured SKU identities, no "
      "economic claim; scored ready-window calibration preserves all candidates and "
      "an observed leader at delivered-token proxies and live estimated backlogs for "
      "1/3/12/24 batches; independent typed throughput/fixed-cost "
      "priors cannot become Measured facts or deadline authority; launch bytes "
      "equal the frozen contract plus one registered positive cache flag; historical "
      "effective cache state UNKNOWN; detector self-test red")
PY
