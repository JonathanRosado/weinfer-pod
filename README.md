# weinfer-pod

Container image for WeInfer managed GPU pods: stock
[vLLM](https://github.com/vllm-project/vllm) plus the WeInfer pull
worker's entrypoint. Built and pushed to GHCR by CI; worker binaries
are attached to releases and pinned by sha256 at pod boot.
