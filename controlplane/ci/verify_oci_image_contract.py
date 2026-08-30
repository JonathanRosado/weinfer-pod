#!/usr/bin/env python3
"""Bind a digest-pinned public GHCR image to one runtime-contract digest.

This verifier performs no provider call and reads no credential.  It resolves
the immutable manifest, verifies the manifest and config blob bytes against
their content digests, and then checks the image's baked contract label.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import sys
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


CONTRACT_LABEL = "ai.weinfer.runtime-contract-sha256"
HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
IMAGE_REFERENCE = re.compile(
    r"^ghcr\.io/"
    r"(?P<repository>[a-z0-9]+(?:[._-][a-z0-9]+)*"
    r"(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+)"
    r"@sha256:(?P<manifest>[0-9a-f]{64})$"
)
MANIFEST_MEDIA_TYPES = {
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
}
TOKEN_LIMIT = 64 * 1024
MANIFEST_LIMIT = 4 * 1024 * 1024
CONFIG_LIMIT = 4 * 1024 * 1024


class ContractVerificationError(RuntimeError):
    """A fail-closed image/contract binding refusal."""


@dataclass(frozen=True)
class VerifiedContract:
    image: str
    manifest_sha256: str
    config_sha256: str
    runtime_contract_sha256: str


OpenUrl = Callable[..., Any]


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read(
    opener: OpenUrl,
    url: str,
    *,
    headers: dict[str, str],
    limit: int,
    stage: str,
) -> bytes:
    request = Request(url, headers=headers, method="GET")
    try:
        with opener(request, timeout=30) as response:
            status = getattr(response, "status", 200)
            if status != 200:
                raise ContractVerificationError(
                    f"{stage} returned unexpected HTTP status {status}"
                )
            raw_length = response.headers.get("Content-Length")
            if raw_length is not None:
                try:
                    declared = int(raw_length)
                except ValueError as exc:
                    raise ContractVerificationError(
                        f"{stage} returned a malformed Content-Length"
                    ) from exc
                if declared < 0 or declared > limit:
                    raise ContractVerificationError(f"{stage} exceeds its byte limit")
            data = response.read(limit + 1)
    except ContractVerificationError:
        raise
    except HTTPError as exc:
        raise ContractVerificationError(
            f"{stage} returned unexpected HTTP status {exc.code}"
        ) from None
    except (OSError, URLError):
        raise ContractVerificationError(f"{stage} transport failure") from None
    if len(data) > limit:
        raise ContractVerificationError(f"{stage} exceeds its byte limit")
    return data


def _json_object(data: bytes, stage: str) -> dict[str, Any]:
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ContractVerificationError(f"{stage} is not valid JSON") from None
    if not isinstance(value, dict):
        raise ContractVerificationError(f"{stage} is not a JSON object")
    return value


def verify_image_contract(
    image: str,
    expected_contract_sha256: str,
    *,
    opener: OpenUrl = urlopen,
) -> VerifiedContract:
    """Verify *image* and return its authenticated contract binding."""

    match = IMAGE_REFERENCE.fullmatch(image)
    if match is None:
        raise ContractVerificationError(
            "image must be a lowercase public GHCR repository pinned by full sha256"
        )
    if HEX_SHA256.fullmatch(expected_contract_sha256) is None:
        raise ContractVerificationError(
            "expected runtime-contract digest must be 64 lowercase hex characters"
        )

    repository = match.group("repository")
    manifest_sha256 = match.group("manifest")
    token_url = "https://ghcr.io/token?" + urlencode(
        {"service": "ghcr.io", "scope": f"repository:{repository}:pull"}
    )
    token_body = _json_object(
        _read(opener, token_url, headers={}, limit=TOKEN_LIMIT, stage="GHCR token"),
        "GHCR token",
    )
    token = token_body.get("token")
    if not isinstance(token, str) or not token or len(token) > 16 * 1024:
        raise ContractVerificationError("GHCR token response has no bounded token")
    auth = {"Authorization": f"Bearer {token}"}

    manifest_url = (
        f"https://ghcr.io/v2/{repository}/manifests/sha256:{manifest_sha256}"
    )
    manifest_headers = {
        **auth,
        "Accept": ", ".join(sorted(MANIFEST_MEDIA_TYPES)),
    }
    manifest_bytes = _read(
        opener,
        manifest_url,
        headers=manifest_headers,
        limit=MANIFEST_LIMIT,
        stage="GHCR manifest",
    )
    if _sha256(manifest_bytes) != manifest_sha256:
        raise ContractVerificationError(
            "GHCR manifest bytes do not match the pinned image digest"
        )
    manifest = _json_object(manifest_bytes, "GHCR manifest")
    if manifest.get("schemaVersion") != 2:
        raise ContractVerificationError("GHCR manifest schemaVersion is not 2")
    if manifest.get("mediaType") not in MANIFEST_MEDIA_TYPES:
        raise ContractVerificationError("GHCR response is not an image manifest")
    config = manifest.get("config")
    if not isinstance(config, dict):
        raise ContractVerificationError("GHCR image manifest has no config descriptor")
    config_digest = config.get("digest")
    if (
        not isinstance(config_digest, str)
        or not config_digest.startswith("sha256:")
        or HEX_SHA256.fullmatch(config_digest.removeprefix("sha256:")) is None
    ):
        raise ContractVerificationError("GHCR config descriptor has no sha256 digest")
    config_sha256 = config_digest.removeprefix("sha256:")

    config_bytes = _read(
        opener,
        f"https://ghcr.io/v2/{repository}/blobs/{config_digest}",
        headers=auth,
        limit=CONFIG_LIMIT,
        stage="GHCR config blob",
    )
    if _sha256(config_bytes) != config_sha256:
        raise ContractVerificationError(
            "GHCR config bytes do not match the manifest descriptor"
        )
    declared_size = config.get("size")
    if (
        not isinstance(declared_size, int)
        or isinstance(declared_size, bool)
        or declared_size != len(config_bytes)
    ):
        raise ContractVerificationError(
            "GHCR config size does not match the manifest descriptor"
        )
    config_object = _json_object(config_bytes, "GHCR config blob")
    runtime_config = config_object.get("config")
    if not isinstance(runtime_config, dict):
        raise ContractVerificationError("GHCR config blob has no runtime config")
    labels = runtime_config.get("Labels")
    if not isinstance(labels, dict):
        raise ContractVerificationError("GHCR config blob has no image labels")
    observed = labels.get(CONTRACT_LABEL)
    if observed != expected_contract_sha256:
        raise ContractVerificationError(
            "image runtime-contract label does not match the local contract bytes"
        )

    return VerifiedContract(
        image=image,
        manifest_sha256=manifest_sha256,
        config_sha256=config_sha256,
        runtime_contract_sha256=expected_contract_sha256,
    )


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(
            "usage: verify_oci_image_contract.py IMAGE@sha256:DIGEST "
            "RUNTIME_CONTRACT_SHA256",
            file=sys.stderr,
        )
        return 2
    try:
        verified = verify_image_contract(argv[1], argv[2])
    except ContractVerificationError as exc:
        print(f"OCI IMAGE CONTRACT REFUSED: {exc}", file=sys.stderr)
        return 1
    print(
        "OCI IMAGE CONTRACT VERIFIED: "
        f"manifest={verified.manifest_sha256} "
        f"contract={verified.runtime_contract_sha256}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
