# WeInfer managed-pod image: pinned vLLM + immutable H100 runtime + our entrypoint.
#
# RunPod's REST v2 create surface has NO entrypoint override (`args`
# only appends to the image entrypoint), so the ONLY way to run the
# vLLM engine and the WeInfer pull worker in one container is to own
# entrypoint.  The linux/amd64 digest below is the v0.11.0 image built
# on CUDA 12.8.1; a moving tag is never an execution authority.
FROM vllm/vllm-openai@sha256:d8d39b59e909d2378ac4feeb191f7e7b6f1342477dc66b7c47cec89e9985ad8a

ARG FLASHINFER_SDIST_URL="https://files.pythonhosted.org/packages/ba/71/dd3001b8be8174d90561764a5f3be4ca219517bde2841189ea6973a3873f/flashinfer_python-0.3.1.tar.gz"
ARG FLASHINFER_SDIST_SHA256="992017d193dfbbc62e67401a6d5416629bf90b640872d14b7863de45e9371446"
ARG WEINFER_RUNTIME_CONTRACT_SHA256

COPY runtime /weinfer/runtime

# The digest-bound config label lets the deployer prove, before provider
# create, that its rendered argv and this image were built from the same
# contract. Refuse the build if the workflow supplies any adjacent hash.
RUN printf '%s  %s\n' "${WEINFER_RUNTIME_CONTRACT_SHA256}" \
        /weinfer/runtime/runtime-contract.json | sha256sum -c -
LABEL ai.weinfer.runtime-contract-sha256="${WEINFER_RUNTIME_CONTRACT_SHA256}"

# `patch` is used once to land the exact upstream vLLM fix.  FlashInfer
# is installed from the sha-bound 0.3.1 source distribution because
# PyPI publishes no wheel for this release.  Its three gpt-oss/H100
# operators are then compiled for SM90 into the package's AOT directory.
# The final verifier refuses source drift, missing AOT bytes, or any
# runtime that could silently fall back to JIT compilation.
RUN apt-get update \
    && apt-get install -y --no-install-recommends patch \
    && rm -rf /var/lib/apt/lists/* \
    && python3 /weinfer/runtime/install_flashinfer.py \
        --url "${FLASHINFER_SDIST_URL}" \
        --sha256 "${FLASHINFER_SDIST_SHA256}" \
    && PYTHON_BIN="$(command -v python3)" \
        bash /weinfer/runtime/apply_vllm_h100_backport.sh \
    && python3 /weinfer/runtime/apply_flashinfer_fp8_runner_scope.py \
    && python3 /weinfer/runtime/apply_flashinfer_no_jit.py \
    && FLASHINFER_CUDA_ARCH_LIST=9.0 \
       FLASHINFER_WORKSPACE_BASE=/tmp/weinfer-flashinfer-build \
        python3 /weinfer/runtime/build_flashinfer_aot.py \
    && python3 /weinfer/runtime/verify_runtime.py --static \
    && rm -rf /tmp/weinfer-flashinfer-build /tmp/weinfer-flashinfer-source

COPY entrypoint.sh /weinfer/entrypoint.sh
RUN chmod +x /weinfer/entrypoint.sh

ENTRYPOINT ["/bin/bash", "/weinfer/entrypoint.sh"]
