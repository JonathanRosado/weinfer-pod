# weinfer-pod

Container image for WeInfer managed GPU pods: digest-pinned
[vLLM](https://github.com/vllm-project/vllm), an immutable H100 runtime,
and the WeInfer pull worker's entrypoint. Built and pushed to GHCR by CI;
worker binaries are attached to releases and pinned by sha256 at pod boot.

The `gpt-oss-120b-h100-v1` profile bakes `flashinfer-python==0.3.1`,
the exact vLLM FP8-KV-scale backport, and three SM90 FlashInfer modules into
the image.  The expensive fused-MoE module is intentionally narrower than a
general FlashInfer build: it contains the one BF16-activation/MXFP4-weight
tactic selected by FlashInfer's upstream `FAST_BUILD` contract.  Its tactic,
source set, and AOT-before-JIT runtime binding are recorded in the image
manifest. The upstream `ENABLE_FP8` define is retained because a vendored shared
utility header places generic and BF16 packed types behind that guard. An
exact-hash source transform disables the separate FP8 and FP8/int4 runner
branches in both the binding and explicit-instantiation sources while leaving
the MXFP4 block intact; no FP8 activation tactic or kernel source is compiled,
and the manifest records the compatibility define, scope define, and both
transforms.
FlashInfer's misleadingly named `flashinfer_cutlass_fused_moe_sm100_ops.cu` is
also retained and named in the manifest because upstream uses it as the shared
PyTorch binding source for both its SM90 and SM100 module generators.
Runtime JIT entry points are disabled; missing or drifting AOT bytes
fail before vLLM or the worker starts. The runtime verifier also requires one
SM90 H100, FlashAttention 3, and the `SM90_FI_MXFP4_BF16` backend.

Engine readiness is part of each named profile, not a shared timeout. The
H100 profile allows 1,200 seconds at no more than $2.70/hour: a never-ready
engine therefore stops at a $0.90 image-level sub-ceiling before the
transaction's $1 GPU ceiling. The legacy consumer profile retains its
3,600-second bound at no more than $0.40/hour.

`WEINFER_SERVING_PROFILE` is mandatory. Missing and unknown profiles are
refused rather than falling through to the legacy Qwen configuration.
