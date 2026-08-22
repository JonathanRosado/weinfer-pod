# WeInfer managed-pod image: stock vLLM + our entrypoint.
#
# RunPod's REST v2 create surface has NO entrypoint override (`args`
# only appends to the image entrypoint), so the ONLY way to run the
# vLLM engine and the WeInfer pull worker in one container is to own
# the entrypoint.  This image adds a single tiny layer to the stock
# vLLM release; the worker binary itself is fetched at boot from a
# release URL pinned by sha256 env, so worker iterations never rebuild
# or repush this image.
FROM vllm/vllm-openai:v0.11.0

COPY entrypoint.sh /weinfer/entrypoint.sh
RUN chmod +x /weinfer/entrypoint.sh

ENTRYPOINT ["/bin/bash", "/weinfer/entrypoint.sh"]
