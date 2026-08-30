#!/usr/bin/env bash
set -euo pipefail

readonly UPSTREAM_COMMIT="c42ff4f4fdc4a4d48ccef18b8067995f6c19e6ec"
readonly PREIMAGE_SHA256="83136ff53e104322f5ab1425c19038d75c4ea8b06085b627af13211a39bc2653"
readonly POSTIMAGE_SHA256="a8a13a30446f621a190674663e46c00a1e49175ce5591c1b05aaa79bab888567"
readonly PATCH_SHA256="69e6909b439a45baf68ea9fe02f5ca208aea5aa62e1eaf4e559f26a55378f1ad"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3}"
PATCH_PATH="${SCRIPT_DIR}/vllm-0.11.0-kv-scale-compile.patch"
readonly SCRIPT_DIR PYTHON_BIN PATCH_PATH

if [[ "${PYTHON_BIN}" != /* || ! -x "${PYTHON_BIN}" ]]; then
  echo "PYTHON_BIN must be an executable absolute path" >&2
  exit 2
fi
if [[ ! -r "${PATCH_PATH}" ]] || ! command -v patch >/dev/null 2>&1; then
  echo "vLLM backport requires its readable patch and the patch utility" >&2
  exit 2
fi

PURELIB="$(${PYTHON_BIN} - <<'PY'
import sysconfig
print(sysconfig.get_paths()["purelib"])
PY
)"
TARGET="${PURELIB}/vllm/attention/layer.py"
MARKER="${PURELIB}/vllm/attention/.weinfer-kv-scale-backport.json"
readonly PURELIB TARGET MARKER

sha256_file() {
  sha256sum "$1" | awk '{print $1}'
}

[[ -f "${TARGET}" ]] || { echo "vLLM attention source missing" >&2; exit 2; }
[[ "$(sha256_file "${PATCH_PATH}")" == "${PATCH_SHA256}" ]] || {
  echo "vLLM backport patch drift" >&2
  exit 2
}

before="$(sha256_file "${TARGET}")"
case "${before}" in
  "${PREIMAGE_SHA256}")
    (cd "${PURELIB}" && patch --batch --forward -p1 < "${PATCH_PATH}" >&2)
    ;;
  "${POSTIMAGE_SHA256}")
    echo "vLLM KV-scale backport already applied" >&2
    ;;
  *)
    echo "vLLM attention source drift: observed=${before}" >&2
    exit 2
    ;;
esac

after="$(sha256_file "${TARGET}")"
[[ "${after}" == "${POSTIMAGE_SHA256}" ]] || {
  echo "vLLM backport postimage mismatch" >&2
  exit 2
}

tmp="${MARKER}.tmp.$$"
trap 'rm -f "${tmp}"' EXIT
printf '{"patch_sha256":"%s","source_sha256":"%s","upstream_commit":"%s"}\n' \
  "${PATCH_SHA256}" "${after}" "${UPSTREAM_COMMIT}" > "${tmp}"
mv "${tmp}" "${MARKER}"
trap - EXIT

echo "vLLM backport verified: ${after}" >&2
