# weinfer-pod

Container image for WeInfer managed GPU pods: digest-pinned
[vLLM](https://github.com/vllm-project/vllm), an immutable H100 runtime,
and the WeInfer pull worker's entrypoint. Built and pushed to GHCR by CI;
worker binaries are attached to releases and pinned by sha256 at pod boot.

The `gpt-oss-120b-h100-v1` profile bakes `flashinfer-python==0.3.1`,
the exact vLLM FP8-KV-scale backport, and the three SM90 FlashInfer
operators observed in the frozen H100 launch into the image. Runtime JIT
entry points are disabled; missing or drifting AOT bytes fail before vLLM
or the worker starts. The runtime verifier also requires one SM90 H100,
FlashAttention 3, and the `SM90_FI_MXFP4_BF16` backend.

Engine readiness is part of each named profile, not a shared timeout. The
H100 profile allows 1,200 seconds at no more than $2.70/hour: a never-ready
engine therefore stops at a $0.90 image-level sub-ceiling before the
transaction's $1 GPU ceiling. The legacy consumer profile retains its
3,600-second bound at no more than $0.40/hour.

`WEINFER_SERVING_PROFILE` is mandatory. Missing and unknown profiles are
refused rather than falling through to the legacy Qwen configuration.
