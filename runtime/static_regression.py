#!/usr/bin/env python3
"""Zero-GPU regression for the image's immutable runtime/profile contract."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shlex
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def flag_value(tokens: list[str], flag: str) -> str:
    assert tokens.count(flag) == 1, (flag, tokens)
    index = tokens.index(flag)
    assert index + 1 < len(tokens), (flag, tokens)
    return tokens[index + 1]


def main() -> int:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    entrypoint = (ROOT / "entrypoint.sh").read_text(encoding="utf-8")
    contract_path = RUNTIME / "runtime-contract.json"
    contract = json.loads(contract_path.read_text())
    contract_sha256 = hashlib.sha256(contract_path.read_bytes()).hexdigest()
    profiles = contract["profiles"]

    assert "FROM vllm/vllm-openai@sha256:d8d39b59" in dockerfile
    assert "FROM vllm/vllm-openai:v0.11.0" not in dockerfile
    assert "992017d193dfbbc62e67401a6d5416629bf90b640872d14b7863de45e9371446" in dockerfile
    assert "ARG WEINFER_RUNTIME_CONTRACT_SHA256" in dockerfile
    assert "/weinfer/runtime/runtime-contract.json | sha256sum -c -" in dockerfile
    assert (
        'LABEL ai.weinfer.runtime-contract-sha256="${WEINFER_RUNTIME_CONTRACT_SHA256}"'
        in dockerfile
    )
    assert len(contract_sha256) == 64
    for executable in (
        "install_flashinfer.py",
        "apply_vllm_h100_backport.sh",
        "apply_flashinfer_exact_mxfp4_runner_scope.py",
        "apply_flashinfer_no_jit.py",
        "build_flashinfer_aot.py",
        "verify_runtime.py --static",
    ):
        assert executable in dockerfile, executable

    assert set(profiles) == {"gpt-oss-120b-h100-v1", "qwen7b-consumer-v1"}
    h100 = profiles["gpt-oss-120b-h100-v1"]
    assert h100["model"] == "openai/gpt-oss-120b"
    assert h100["model_revision"] == "b5c939de8f754692c1647ca79fbf85e8c1e70f8a"
    assert h100["tokenizer_revision"] == h100["model_revision"]
    assert h100["max_context_tokens"] == 131072
    assert h100["cuda_class"] == "12"
    assert h100["engine_ready_timeout_seconds"] == 1200
    assert h100["max_provider_rate_micro_per_hour"] == 2_700_000
    assert (
        h100["engine_ready_timeout_seconds"]
        * h100["max_provider_rate_micro_per_hour"]
        / 3_600_000_000
        == 0.9
    )
    qwen = profiles["qwen7b-consumer-v1"]
    assert qwen["engine_ready_timeout_seconds"] == 3600
    assert qwen["max_provider_rate_micro_per_hour"] == 400_000
    assert (
        qwen["engine_ready_timeout_seconds"]
        * qwen["max_provider_rate_micro_per_hour"]
        / 3_600_000_000
        == 0.4
    )
    h100_args = shlex.split(h100["vllm_canonical_args"])
    expected_values = {
        "--max-num-batched-tokens": "8192",
        "--max-num-seqs": "8",
        "--gpu-memory-utilization": "0.95",
        "--dtype": "bfloat16",
        "--kv-cache-dtype": "fp8",
        "--tensor-parallel-size": "1",
        "--served-model-name": h100["model"],
        "--revision": h100["model_revision"],
        "--tokenizer-revision": h100["tokenizer_revision"],
        "--max-model-len": "131072",
    }
    for flag, value in expected_values.items():
        assert flag_value(h100_args, flag) == value
    for flag in (
        "--calculate-kv-scales",
        "--enable-chunked-prefill",
        "--enable-prefix-caching",
    ):
        assert h100_args.count(flag) == 1
    ignore = h100_args.index("--ignore-patterns")
    assert h100_args[ignore + 1 : ignore + 3] == ["original/*", "metal/*"]

    assert 'case "${WEINFER_SERVING_PROFILE:-}"' in entrypoint
    assert '"${VLLM_ARGS[@]}" &' in entrypoint
    assert "${VLLM_EXTRA_ARGS:-} &" not in entrypoint
    assert "wait_for_engine.py" in entrypoint
    assert "ENGINE_READY_TIMEOUT_SECONDS=1200" in entrypoint
    assert "ENGINE_READY_TIMEOUT_SECONDS=3600" in entrypoint
    assert "verify_runtime.py --post-engine" in entrypoint
    assert entrypoint.index("verify_runtime.py --post-engine") < entrypoint.index(
        'echo "[entrypoint] starting the pull worker"'
    )
    for value in (
        "FLASHINFER_DISABLE_JIT=1",
        "VLLM_ATTENTION_BACKEND=FLASH_ATTN",
        "VLLM_FLASH_ATTN_VERSION=3",
        "VLLM_USE_FLASHINFER_MOE_MXFP4_BF16=1",
    ):
        assert value in entrypoint

    # Exercise the runtime profile gate itself. No installed vLLM/FlashInfer
    # package is imported by this pure branch.
    verifier = load_module("verify_runtime", RUNTIME / "verify_runtime.py")
    names = {
        "WEINFER_SERVING_PROFILE",
        "WEINFER_SERVED_MODEL",
        "WEINFER_MODEL_REVISION",
        "WEINFER_TOKENIZER_REVISION",
        "WEINFER_BACKEND_MAX_CONTEXT",
        "VLLM_EXTRA_ARGS",
    }
    saved = {name: os.environ.get(name) for name in names}
    try:
        for name in names:
            os.environ.pop(name, None)
        try:
            verifier.verify_profile(contract)
            raise AssertionError("missing profile passed")
        except RuntimeError as exc:
            assert "WEINFER_SERVING_PROFILE is required" in str(exc)

        os.environ["WEINFER_SERVING_PROFILE"] = "typo"
        try:
            verifier.verify_profile(contract)
            raise AssertionError("unknown profile passed")
        except RuntimeError as exc:
            assert "unknown WEINFER_SERVING_PROFILE" in str(exc)

        os.environ.update(
            {
                "WEINFER_SERVING_PROFILE": "gpt-oss-120b-h100-v1",
                "WEINFER_SERVED_MODEL": h100["model"],
                "WEINFER_MODEL_REVISION": h100["model_revision"],
                "WEINFER_TOKENIZER_REVISION": h100["tokenizer_revision"],
                "WEINFER_BACKEND_MAX_CONTEXT": str(h100["max_context_tokens"]),
                "VLLM_EXTRA_ARGS": h100["vllm_canonical_args"],
            }
        )
        assert verifier.verify_profile(contract)[0] == "gpt-oss-120b-h100-v1"
        os.environ["WEINFER_BACKEND_MAX_CONTEXT"] = "8192"
        try:
            verifier.verify_profile(contract)
            raise AssertionError("context mismatch passed")
        except RuntimeError as exc:
            assert "contradicts serving profile" in str(exc)
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    no_jit = load_module("apply_flashinfer_no_jit", RUNTIME / "apply_flashinfer_no_jit.py")
    synthetic = b"".join(old for old, _ in no_jit.REPLACEMENTS)
    for old, new in no_jit.REPLACEMENTS:
        assert synthetic.count(old) == 1
        synthetic = synthetic.replace(old, new)
    assert synthetic.count(b"FLASHINFER_DISABLE_JIT") == 3

    exact_runner_scope = load_module(
        "apply_flashinfer_exact_mxfp4_runner_scope",
        RUNTIME / "apply_flashinfer_exact_mxfp4_runner_scope.py",
    )
    assert exact_runner_scope.SCOPE_DEFINE == (
        "WEINFER_EXACT_SM90_BF16_MXFP4_RUNNER_SCOPE"
    )
    assert [target["kind"] for target in exact_runner_scope.TARGETS] == [
        "explicit_instantiations",
        "runtime_binding",
        "candidate_authority",
        "tactic_dispatch",
    ]
    assert exact_runner_scope.RUNNER_CONTRACT == {
        "activation_dtype": "bfloat16",
        "output_dtype": "bfloat16",
        "use_deepseek_fp8_block_scale": False,
        "use_mxfp8_act_scaling": False,
        "use_w4_group_scaling": True,
        "weight_storage_dtype": "uint8",
        "weight_template_dtype": "mxfp4_e2m1",
    }
    assert exact_runner_scope.TACTIC_CONTRACT == {
        "candidate_flags": ["weight_only", "hopper", "grouped_gemm"],
        "cluster_shape": [1, 1, 1],
        "cta_shape": [128, 128, 128],
        "epilogue_schedule": "auto_config_to_tma_warp_specialized_cooperative",
        "mainloop_schedule": "pingpong",
    }
    for target in exact_runner_scope.TARGETS:
        try:
            exact_runner_scope.transform(b"non-pinned source\n", target)
            raise AssertionError("non-pinned exact runner source preimage passed")
        except RuntimeError as exc:
            assert "preimage drift" in str(exc)
        start, end, prefix = exact_runner_scope.TRANSFORMS[target["kind"]]
        region = start + b"upstream runner selection\n"
        synthetic = b"header\n" + region + end + b"tail\n"
        synthetic_target = {
            "kind": target["kind"],
            "path": "synthetic",
            "region_sha256": hashlib.sha256(region).hexdigest(),
        }
        scoped = exact_runner_scope.scope_region(synthetic, synthetic_target)
        assert scoped.count(prefix) == 1
        assert scoped.count(region) == 1
        assert exact_runner_scope.recover_upstream(
            scoped, synthetic_target
        ) == synthetic
    assert (
        b"template class CutlassMoeFCRunner<__nv_bfloat16, __nv_fp4_e2m1>;"
        in exact_runner_scope.INSTANTIATION_PREFIX
    )
    assert (
        b"WeInfer exact SM90 BF16/MXFP4 runner scope refused"
        in exact_runner_scope.BINDING_PREFIX
    )
    assert (
        b"return {CutlassGemmConfig{CutlassTileConfigSM90::CtaShape128x128x128B"
        in exact_runner_scope.CANDIDATE_PREFIX
    )
    for candidate_flag in (
        b"CutlassGemmConfig::WEIGHT_ONLY",
        b"CutlassGemmConfig::HOPPER",
        b"CutlassGemmConfig::GROUPED_GEMM",
    ):
        assert exact_runner_scope.CANDIDATE_PREFIX.count(candidate_flag) == 1
    assert b"CutlassGemmConfig::FP4_ONLY" not in exact_runner_scope.CANDIDATE_PREFIX
    assert (
        b"WeInfer exact SM90 BF16/MXFP4 tactic authority refused candidate flags"
        in exact_runner_scope.CANDIDATE_PREFIX
    )
    assert (
        exact_runner_scope.DISPATCH_PREFIX.count(
            b"sm90_generic_mixed_moe_gemm_kernelLauncher<"
        )
        == 2
    )
    assert (
        exact_runner_scope.DISPATCH_PREFIX.count(
            b"Shape<_128, _128, _128>, Shape<_1, _1, _1>,\n"
            b"      cutlass::gemm::KernelTmaWarpSpecializedPingpong,\n"
            b"      cutlass::epilogue::TmaWarpSpecializedCooperative,\n"
            b"      cutlass::WeightOnlyQuantOp::FINEGRAINED_SCALE_ONLY>"
        )
        == 2
    )
    assert (
        b"WeInfer exact SM90 BF16/MXFP4 tactic dispatch refused config"
        in exact_runner_scope.DISPATCH_PREFIX
    )

    aot = load_module("build_flashinfer_aot", RUNTIME / "build_flashinfer_aot.py")
    assert len(aot.TARGET_SOURCE_SUFFIXES) == 14
    assert aot.TARGET_SOURCE_SUFFIXES[-1] == (
        "weinfer_exact_sm90_bf16_mxfp4.generated.cu"
    )
    assert (
        aot.UPSTREAM_FP8_BLOCKSCALE_LINK_STUB_SOURCE
        in aot.TARGET_STATIC_SOURCE_SUFFIXES
    )
    assert (
        verifier.EXPECTED_UPSTREAM_FP8_BLOCKSCALE_LINK_STUB_SOURCE
        == aot.UPSTREAM_FP8_BLOCKSCALE_LINK_STUB_SOURCE
    )
    assert (
        verifier.EXPECTED_UPSTREAM_FP8_BLOCKSCALE_LINK_STUB_SHA256
        == aot.UPSTREAM_FP8_BLOCKSCALE_LINK_STUB_SHA256
        == "aae25878e2520693265c46e073b74f9a8f4f7daa351fc80b6f9a7fc8227c4257"
    )
    forbidden = ("fp8", "fp16", "uint4", "uint8", "fp32")
    for suffix in aot.TARGET_STATIC_SOURCE_SUFFIXES:
        if suffix == aot.UPSTREAM_FP8_BLOCKSCALE_LINK_STUB_SOURCE:
            continue
        assert not any(value in Path(suffix).name for value in forbidden), suffix
    assert aot.TARGET_TACTIC.endswith(
        "1x1x1_warpspecialized_pingpong_epi_tma"
    )
    assert verifier.EXPECTED_FUSED_MOE_TACTIC == aot.TARGET_TACTIC
    assert verifier.EXPECTED_FUSED_MOE_SOURCE_SUFFIXES == list(
        aot.TARGET_SOURCE_SUFFIXES
    )
    expected_runner_scope_sources = exact_runner_scope.source_records()
    direct_patched_source_suffixes = {
        record["path"].removeprefix("data/csrc/")
        for record in expected_runner_scope_sources
        if record["kind"] != "tactic_dispatch"
    }
    assert direct_patched_source_suffixes.issubset(
        set(aot.TARGET_STATIC_SOURCE_SUFFIXES)
    )
    dispatch_records = [
        record
        for record in expected_runner_scope_sources
        if record["kind"] == "tactic_dispatch"
    ]
    assert len(dispatch_records) == 1
    assert dispatch_records[0]["path"].endswith(
        "moe_gemm/moe_gemm_template_dispatch_tma_ws_mixed_dtype.h"
    )
    assert any(
        suffix.endswith("moe_gemm_kernels_bf16_fp4.cu")
        for suffix in aot.TARGET_STATIC_SOURCE_SUFFIXES
    )
    assert (
        verifier.FLASHINFER_EXACT_MXFP4_RUNNER_SCOPE_POLICY
        == exact_runner_scope.POLICY
    )
    assert (
        verifier.FLASHINFER_EXACT_MXFP4_RUNNER_SCOPE_SOURCES
        == list(aot.EXACT_MXFP4_RUNNER_SCOPE_SOURCES)
        == expected_runner_scope_sources
    )
    assert (
        verifier.FLASHINFER_EXACT_MXFP4_RUNNER_CONTRACT
        == aot.EXACT_MXFP4_RUNNER_CONTRACT
        == exact_runner_scope.RUNNER_CONTRACT
    )
    assert (
        verifier.FLASHINFER_EXACT_MXFP4_TACTIC_CONTRACT
        == aot.EXACT_MXFP4_TACTIC_CONTRACT
        == exact_runner_scope.TACTIC_CONTRACT
    )
    assert verifier.VLLM_MXFP4_CALL_SOURCE == aot.VLLM_MXFP4_CALL_SOURCE
    assert (
        verifier.VLLM_MXFP4_CALL_SOURCE_SHA256
        == aot.VLLM_MXFP4_CALL_SOURCE_SHA256
        == "69f4105640bd466d463ccf9302164d35e9299f8e9228568dd52a9c7d66146b75"
    )
    assert verifier.EXPECTED_COMPATIBILITY_COMPILE_DEFINES == list(
        aot.REQUIRED_COMPATIBILITY_COMPILE_DEFINES
    )
    assert verifier.EXPECTED_RUNNER_SCOPE_COMPILE_DEFINES == list(
        aot.REQUIRED_RUNNER_SCOPE_COMPILE_DEFINES
    )
    assert verifier.EXPECTED_REMOVED_COMPILE_DEFINES == list(
        aot.REMOVED_COMPILE_DEFINES
    )
    assert (
        verifier.EXPECTED_UPSTREAM_SHARED_BINDING_SOURCE
        == aot.UPSTREAM_SHARED_BINDING_SOURCE
    )
    assert verifier.EXPECTED_RUNTIME_BINDING == aot.RUNTIME_BINDING
    assert aot.ninja_assignment_tokens(
        "cflags = $common_cflags $\n"
        "    -fPIC $\n"
        "    -DFAST_BUILD $\n"
        "    -DWEINFER_EXACT_SM90_BF16_MXFP4_RUNNER_SCOPE\n"
        "post_cflags =\n",
        "cflags",
    ) == [
        "$common_cflags",
        "-fPIC",
        "-DFAST_BUILD",
        "-DWEINFER_EXACT_SM90_BF16_MXFP4_RUNNER_SCOPE",
    ]

    class SyntheticEnum:
        def __init__(self, name: str):
            self.name = name

    class SyntheticOperation:
        def __init__(self, name: str, **changes):
            self.name = name
            values = {
                "act_type": SyntheticEnum("bf16"),
                "arch": 90,
                "bias_type": SyntheticEnum("bf16"),
                "cga_shape": (1, 1, 1),
                "cta_shape": (128, 128, 128),
                "epi_fusion": None,
                "epi_schedule": SyntheticEnum("TmaWarpSpecializedCooperative"),
                "epi_tag": SyntheticEnum("epilogue_op_default"),
                "gemm_kind": SyntheticEnum("Grouped"),
                "is_mx_fpx": False,
                "mainloop_schedule": SyntheticEnum("TmaWarpSpecializedPingpong"),
                "output_type": SyntheticEnum("bf16"),
                "quant_op": SyntheticEnum("finegrained_scale_only"),
                "scalezero_type": SyntheticEnum("ue8m0"),
                "stages": 0,
                "warp_shape": (0, 0, 0),
                "weight_type": SyntheticEnum("e2m1"),
            }
            values.update(changes)
            for key, value in values.items():
                setattr(self, key, value)

        def __repr__(self) -> str:
            return self.name

    target = SyntheticOperation(aot.TARGET_TACTIC)
    wrong = SyntheticOperation(aot.TARGET_TACTIC.replace("bf16", "fp16", 1))
    assert aot.select_target_operation([wrong, target]) is target
    assert aot.validate_target_operation(target) == {
        "activation_dtype": "bfloat16",
        "cluster_shape": [1, 1, 1],
        "cta_shape": [128, 128, 128],
        "generated_tactic": aot.TARGET_TACTIC,
        "mainloop_schedule": "pingpong",
        "scale_dtype": "ue8m0",
        "tactic_contract": aot.EXACT_MXFP4_TACTIC_CONTRACT,
        "weight_dtype": "mxfp4_e2m1",
    }
    for field, changed in (
        ("cta_shape", (64, 64, 128)),
        ("cga_shape", (1, 2, 1)),
        ("mainloop_schedule", SyntheticEnum("TmaWarpSpecializedCooperative")),
        ("epi_schedule", SyntheticEnum("NoSmemWarpSpecialized")),
        ("quant_op", SyntheticEnum("finegrained_scale_and_zeros")),
        ("weight_type", SyntheticEnum("e4m3")),
    ):
        try:
            aot.validate_target_operation(
                SyntheticOperation(aot.TARGET_TACTIC, **{field: changed})
            )
            raise AssertionError(f"TARGET_TACTIC {field} drift passed")
        except RuntimeError as exc:
            assert "operation-field drift" in str(exc)
    for operations in ([wrong], [target, SyntheticOperation(aot.TARGET_TACTIC)]):
        try:
            aot.select_target_operation(operations)
            raise AssertionError("non-unique AOT tactic selection passed")
        except RuntimeError as exc:
            assert "expected one exact SM90 BF16/MXFP4 tactic" in str(exc)

    class SyntheticSpec:
        def __init__(self, ninja_path: Path) -> None:
            self.name = "fused_moe_90"
            self.ninja_path = ninja_path
            self.sources: list[Path] = []
            self.extra_cflags = ["-DFAST_BUILD"]
            self.extra_cuda_cflags = [
                "-DCOMPILE_HOPPER_TMA_GEMMS",
                "-DCOMPILE_HOPPER_TMA_GROUPED_GEMMS",
                "-DENABLE_BF16",
                "-DENABLE_FP8",
                "-DENABLE_FP4",
                "-DUSING_OSS_CUTLASS_MOE_GEMM",
            ]
            self.write_count = 0
            self.written_sources: tuple[Path, ...] = ()
            self.written_flags: tuple[str, ...] = ()
            self.written_cuda_flags: tuple[str, ...] = ()

        def write_ninja(self) -> None:
            self.write_count += 1
            self.written_sources = tuple(self.sources)
            self.written_flags = tuple(self.extra_cflags)
            self.written_cuda_flags = tuple(self.extra_cuda_cflags)
            lines = [
                "cflags = " + " ".join(self.extra_cflags),
                "cuda_cflags = " + " ".join(self.extra_cuda_cflags),
            ]
            lines.extend(
                f"build $name/{source.stem}.o: "
                f"{'cuda_compile' if source.suffix == '.cu' else 'compile'} "
                f"{source.resolve()}"
                for source in self.sources
            )
            self.ninja_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    selected = [Path("/flashinfer") / suffix for suffix in aot.TARGET_SOURCE_SUFFIXES]
    with tempfile.TemporaryDirectory() as temporary:
        synthetic_spec = SyntheticSpec(Path(temporary) / "build.ninja")
        assert aot.reemit_narrow_fused_moe_spec(synthetic_spec, selected) is synthetic_spec
        assert synthetic_spec.write_count == 1
        assert synthetic_spec.written_sources == tuple(selected)
        assert synthetic_spec.written_flags.count("-DFAST_BUILD") == 1
        assert (
            synthetic_spec.written_flags.count(
                "-DWEINFER_EXACT_SM90_BF16_MXFP4_RUNNER_SCOPE"
            )
            == 1
        )
        assert synthetic_spec.written_cuda_flags.count("-DENABLE_FP8") == 1
        assert (
            synthetic_spec.written_cuda_flags.count(
                "-DWEINFER_EXACT_SM90_BF16_MXFP4_RUNNER_SCOPE"
            )
            == 1
        )
        assert "-DCOMPILE_HOPPER_TMA_GEMMS" not in synthetic_spec.written_cuda_flags
        assert "-DCOMPILE_HOPPER_TMA_GROUPED_GEMMS" in synthetic_spec.written_cuda_flags
        graph = aot.verify_emitted_ninja(synthetic_spec, selected)
        assert graph["source_count"] == 14
        assert len(graph["ninja_sha256"]) == 64
        good_ninja = synthetic_spec.ninja_path.read_text()
        synthetic_spec.ninja_path.write_text(
            good_ninja.replace(
                "-DENABLE_FP8 ",
                "",
                1,
            ),
            encoding="utf-8",
        )
        try:
            aot.verify_emitted_ninja(synthetic_spec, selected)
            raise AssertionError("missing ENABLE_FP8 compatibility define passed")
        except RuntimeError as exc:
            assert "lost compatibility flag -DENABLE_FP8" in str(exc)
        synthetic_spec.ninja_path.write_text(
            good_ninja.replace(
                "cflags = -DFAST_BUILD "
                "-DWEINFER_EXACT_SM90_BF16_MXFP4_RUNNER_SCOPE",
                "cflags = -DFAST_BUILD",
                1,
            ),
            encoding="utf-8",
        )
        try:
            aot.verify_emitted_ninja(synthetic_spec, selected)
            raise AssertionError("missing exact MXFP4 runner-scope define passed")
        except RuntimeError as exc:
            assert (
                "lost runner-scope flag "
                "-DWEINFER_EXACT_SM90_BF16_MXFP4_RUNNER_SCOPE from host or CUDA"
                in str(exc)
            )
        cuda_line = next(
            line
            for line in good_ninja.splitlines()
            if line.startswith("cuda_cflags = ")
        )
        synthetic_spec.ninja_path.write_text(
            good_ninja.replace(
                cuda_line,
                cuda_line.replace(
                    " -DWEINFER_EXACT_SM90_BF16_MXFP4_RUNNER_SCOPE", "", 1
                ),
                1,
            ),
            encoding="utf-8",
        )
        try:
            aot.verify_emitted_ninja(synthetic_spec, selected)
            raise AssertionError("missing CUDA exact tactic-scope define passed")
        except RuntimeError as exc:
            assert (
                "lost runner-scope flag "
                "-DWEINFER_EXACT_SM90_BF16_MXFP4_RUNNER_SCOPE from host or CUDA"
                in str(exc)
            )
        synthetic_spec.ninja_path.write_text(
            good_ninja.replace(
                "cuda_cflags = ",
                "cuda_cflags = -DCOMPILE_HOPPER_TMA_GEMMS ",
                1,
            ),
            encoding="utf-8",
        )
        try:
            aot.verify_emitted_ninja(synthetic_spec, selected)
            raise AssertionError("removed ungrouped Hopper GEMM branch passed")
        except RuntimeError as exc:
            assert "retained forbidden flag -DCOMPILE_HOPPER_TMA_GEMMS" in str(exc)
        synthetic_spec.ninja_path.write_text(
            good_ninja + "build $name/stale.o: cuda_compile /flashinfer/stale.cu\n",
            encoding="utf-8",
        )
        try:
            aot.verify_emitted_ninja(synthetic_spec, selected)
            raise AssertionError("stale full Ninja graph passed")
        except RuntimeError as exc:
            assert "expected 14, observed 15" in str(exc)
        try:
            aot.reemit_narrow_fused_moe_spec(synthetic_spec, selected[:-1])
            raise AssertionError("incomplete narrow source set passed")
        except RuntimeError as exc:
            assert "narrow FlashInfer AOT source set drift" in str(exc)

    class SyntheticLoadSpec:
        name = "fused_moe_90"

        def __init__(self) -> None:
            self.calls = []

        def load(self, path: Path, class_name: str | None = None):
            self.calls.append((path, class_name))
            return object()

    synthetic_load = SyntheticLoadSpec()
    synthetic_library = Path("/flashinfer/aot/fused_moe_90.so")
    assert (
        aot.validate_fused_moe_aot_load(synthetic_load, synthetic_library)
        == "torch.classes.FusedMoeRunner"
    )
    assert synthetic_load.calls == [(synthetic_library, "FusedMoeRunner")]
    synthetic_load.name = "sampling"
    try:
        aot.validate_fused_moe_aot_load(synthetic_load, synthetic_library)
        raise AssertionError("wrong AOT operator passed fused-MoE load validation")
    except RuntimeError as exc:
        assert "wrong operator" in str(exc)

    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        operator = workspace / "cached_ops" / "fused_moe_90"
        operator.mkdir(parents=True)
        graph_path = operator / "build.ninja"
        graph_path.write_text("rule cuda_compile\n", encoding="utf-8")
        assert verifier.scan_runtime_workspace(workspace) == [
            {
                "path": "cached_ops/fused_moe_90/build.ninja",
                "sha256": hashlib.sha256(graph_path.read_bytes()).hexdigest(),
            }
        ]

    for relative in (
        "cached_ops/fused_moe_90/fused_moe_90.so",
        "cached_ops/fused_moe_90/kernel.o",
        "cached_ops/fused_moe_90/kernel.ptx",
        "cached_ops/.ninja_log",
    ):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            artifact = workspace / relative
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_bytes(b"compiled")
            try:
                verifier.scan_runtime_workspace(workspace)
                raise AssertionError(f"runtime compiled artifact passed: {relative}")
            except RuntimeError as exc:
                assert relative in str(exc)

    waiter = load_module("wait_for_engine", RUNTIME / "wait_for_engine.py")
    assert waiter.pid_alive(os.getpid()) is True
    assert waiter.pid_alive(2**31 - 1) is False

    print(
        "PASS: pinned base + exact profiles + H100 argv + one SM90 BF16/MXFP4 "
        "AOT tactic + no-JIT transform + fail-closed profile gate"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
