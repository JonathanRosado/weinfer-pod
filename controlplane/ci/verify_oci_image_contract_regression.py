#!/usr/bin/env python3
"""Zero-network regression for the digest-bound GHCR contract verifier."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from urllib.parse import urlparse

import verify_oci_image_contract as verifier


REPOSITORY = "jonathanrosado/weinfer-pod"
CONTRACT = "1" * 64


def encode(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode()


class Response:
    def __init__(self, body: bytes, status: int = 200):
        self.body = io.BytesIO(body)
        self.status = status
        self.headers = {"Content-Length": str(len(body))}

    def read(self, size: int = -1) -> bytes:
        return self.body.read(size)

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *_: object) -> None:
        return None


class Registry:
    def __init__(self, *, label: object = CONTRACT, config_digest_ok: bool = True):
        config = {"config": {"Labels": {verifier.CONTRACT_LABEL: label}}}
        self.config_bytes = encode(config)
        real_config_sha = hashlib.sha256(self.config_bytes).hexdigest()
        self.config_sha = real_config_sha if config_digest_ok else "2" * 64
        manifest = {
            "schemaVersion": 2,
            "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
            "config": {
                "mediaType": "application/vnd.docker.container.image.v1+json",
                "digest": f"sha256:{self.config_sha}",
                "size": len(self.config_bytes),
            },
            "layers": [],
        }
        self.manifest_bytes = encode(manifest)
        self.manifest_sha = hashlib.sha256(self.manifest_bytes).hexdigest()
        self.requests: list[str] = []

    @property
    def image(self) -> str:
        return f"ghcr.io/{REPOSITORY}@sha256:{self.manifest_sha}"

    def __call__(self, request: object, timeout: int) -> Response:
        assert timeout == 30
        url = getattr(request, "full_url")
        headers = {key.lower(): value for key, value in request.header_items()}
        self.requests.append(url)
        parsed = urlparse(url)
        if parsed.path == "/token":
            assert "repository%3Ajonathanrosado%2Fweinfer-pod%3Apull" in url
            return Response(encode({"token": "fixture-token"}))
        assert headers["authorization"] == "Bearer fixture-token"
        if "/manifests/" in parsed.path:
            assert "application/vnd.oci.image.manifest.v1+json" in headers["accept"]
            return Response(self.manifest_bytes)
        if "/blobs/" in parsed.path:
            return Response(self.config_bytes)
        raise AssertionError(url)


def refuse(image: str, contract: str, registry: Registry, expected: str) -> None:
    before = len(registry.requests)
    try:
        verifier.verify_image_contract(image, contract, opener=registry)
    except verifier.ContractVerificationError as exc:
        assert expected in str(exc), exc
    else:
        raise AssertionError(f"expected refusal containing {expected!r}")
    if expected.startswith("image must") or expected.startswith("expected runtime"):
        assert len(registry.requests) == before, registry.requests


def main() -> int:
    good = Registry()
    result = verifier.verify_image_contract(good.image, CONTRACT, opener=good)
    assert result.image == good.image
    assert result.manifest_sha256 == good.manifest_sha
    assert result.config_sha256 == good.config_sha
    assert result.runtime_contract_sha256 == CONTRACT
    assert len(good.requests) == 3

    wrong_label = Registry(label="3" * 64)
    refuse(wrong_label.image, CONTRACT, wrong_label, "label does not match")
    missing_label = Registry(label=None)
    refuse(missing_label.image, CONTRACT, missing_label, "label does not match")
    wrong_config = Registry(config_digest_ok=False)
    refuse(wrong_config.image, CONTRACT, wrong_config, "config bytes do not match")

    adjacent = Registry()
    adjacent_image = adjacent.image[:-1] + ("0" if adjacent.image[-1] != "0" else "1")
    refuse(adjacent_image, CONTRACT, adjacent, "manifest bytes do not match")
    refuse(
        "ghcr.io/jonathanrosado/weinfer-pod:latest",
        CONTRACT,
        Registry(),
        "image must",
    )
    refuse(
        good.image.replace("jonathanrosado", "JonathanRosado"),
        CONTRACT,
        Registry(),
        "image must",
    )
    refuse(good.image[:-1], CONTRACT, Registry(), "image must")
    refuse(good.image, "A" * 64, Registry(), "expected runtime")

    print(
        "OCI IMAGE CONTRACT REGRESSION PASS: 1 verified; 8 red cases; "
        "no network or provider calls"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
