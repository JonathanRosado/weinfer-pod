#!/usr/bin/env python3
"""Verify a secret-free deploy projection against the baked image contract."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import shlex
import sys
from typing import Any


PROJECTION_KEYS = {
    "bootstrap_hardware",
    "catalog",
    "max_gpu_rate",
    "max_context",
    "model_revision",
    "probe_budget",
    "probe_delay_seconds",
    "served_model",
    "serving_profile",
    "tokenizer_revision",
    "vllm_extra_args",
}


class ProfileContractError(RuntimeError):
    """A fail-closed deploy/image profile mismatch."""


def _positive_int(value: object, name: str) -> int:
    if not isinstance(value, str) or not value.isascii() or not value.isdigit():
        raise ProfileContractError(f"{name} is not a positive base-10 integer")
    parsed = int(value)
    if parsed <= 0:
        raise ProfileContractError(f"{name} is not a positive base-10 integer")
    return parsed


def _effective_alias(canonical: list[str], model: str) -> str:
    values: list[str] = []
    for index, token in enumerate(canonical):
        if token == "--served-model-name" and index + 1 < len(canonical):
            values.append(canonical[index + 1])
        elif token.startswith("--served-model-name="):
            values.append(token.split("=", 1)[1])
    if len(values) > 1:
        raise ProfileContractError("canonical argv repeats --served-model-name")
    return values[0] if values else model


def validate_projection(
    contract: dict[str, Any], projection: dict[str, Any]
) -> dict[str, Any]:
    if set(projection) != PROJECTION_KEYS:
        raise ProfileContractError("deploy profile projection schema drift")
    if contract.get("object") != "weinfer_pod_runtime_contract_v1":
        raise ProfileContractError("runtime contract object drift")
    profiles = contract.get("profiles")
    if not isinstance(profiles, dict):
        raise ProfileContractError("runtime contract profiles are missing")
    name = projection["serving_profile"]
    if not isinstance(name, str):
        raise ProfileContractError("serving profile is absent from the runtime contract")
    profile = profiles.get(name)
    if not isinstance(profile, dict):
        raise ProfileContractError("serving profile is absent from the runtime contract")

    max_context_tokens = profile.get("max_context_tokens")
    if (
        not isinstance(max_context_tokens, int)
        or isinstance(max_context_tokens, bool)
        or max_context_tokens <= 0
    ):
        raise ProfileContractError("runtime contract max context is invalid")

    expected_strings = {
        "served_model": profile.get("model"),
        "model_revision": profile.get("model_revision"),
        "tokenizer_revision": profile.get("tokenizer_revision"),
        "max_context": str(max_context_tokens),
    }
    for field, expected in expected_strings.items():
        if not isinstance(expected, str) or projection[field] != expected:
            raise ProfileContractError(f"deploy {field} contradicts the runtime contract")

    canonical = (
        f"{str(projection['vllm_extra_args']).strip()} "
        f"--revision {projection['model_revision']} "
        f"--tokenizer-revision {projection['tokenizer_revision']} "
        f"--max-model-len {projection['max_context']}"
    )
    expected_canonical = profile.get("vllm_canonical_args")
    if not isinstance(expected_canonical, str) or shlex.split(canonical) != shlex.split(
        expected_canonical
    ):
        raise ProfileContractError("deploy canonical argv contradicts the runtime contract")

    try:
        rate_micro = Decimal(str(projection["max_gpu_rate"])) * Decimal(1_000_000)
    except InvalidOperation:
        raise ProfileContractError("deploy maximum GPU rate is not a decimal") from None
    expected_rate = profile.get("max_provider_rate_micro_per_hour")
    if (
        not rate_micro.is_finite()
        or rate_micro != rate_micro.to_integral_value()
        or not isinstance(expected_rate, int)
        or isinstance(expected_rate, bool)
        or rate_micro != expected_rate
    ):
        raise ProfileContractError("deploy maximum GPU rate contradicts the runtime contract")

    probe_budget = _positive_int(projection["probe_budget"], "probe budget")
    probe_delay = _positive_int(
        projection["probe_delay_seconds"], "probe delay"
    )
    image_timeout = profile.get("engine_ready_timeout_seconds")
    if (
        not isinstance(image_timeout, int)
        or isinstance(image_timeout, bool)
        or image_timeout <= 0
        or probe_budget * probe_delay < image_timeout
    ):
        raise ProfileContractError("manager readiness ends before the image contract")

    try:
        catalog = json.loads(projection["catalog"])
        hardware = json.loads(projection["bootstrap_hardware"])
    except (TypeError, json.JSONDecodeError):
        raise ProfileContractError("deploy catalog or hardware is not valid JSON") from None
    models = catalog.get("models") if isinstance(catalog, dict) else None
    if not isinstance(models, list) or len(models) != 1 or not isinstance(models[0], dict):
        raise ProfileContractError("deploy catalog must contain one model")
    sold = models[0]
    canonical_tokens = shlex.split(canonical)
    if sold.get("id") != _effective_alias(canonical_tokens, projection["served_model"]):
        raise ProfileContractError("sold model alias contradicts the runtime contract")
    if sold.get("context_length") != max_context_tokens:
        raise ProfileContractError("sold context contradicts the runtime contract")

    if name == "gpt-oss-120b-h100-v1":
        if not isinstance(hardware, list) or len(hardware) != 1:
            raise ProfileContractError("H100 profile must contain one hardware identity")
        row = hardware[0]
        if not isinstance(row, dict) or (
            row.get("gpu_sku") != "NVIDIA H100 NVL"
            or row.get("cuda_class") != profile.get("cuda_class")
            or row.get("vram_gb") != 94
        ):
            raise ProfileContractError(
                "registered H100 launch identity contradicts the runtime contract"
            )
    return profile


def read_contract(path: Path) -> tuple[dict[str, Any], str]:
    if not path.is_file() or path.is_symlink():
        raise ProfileContractError("runtime contract is missing or symlinked")
    body = path.read_bytes()
    try:
        contract = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ProfileContractError("runtime contract is not valid JSON") from None
    if not isinstance(contract, dict):
        raise ProfileContractError("runtime contract is not a JSON object")
    return contract, hashlib.sha256(body).hexdigest()


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(
            "usage: verify_serving_profile_contract.py CONTRACT_PATH PROJECTION_JSON",
            file=sys.stderr,
        )
        return 2
    try:
        contract, digest = read_contract(Path(argv[1]))
        projection = json.loads(argv[2])
        if not isinstance(projection, dict):
            raise ProfileContractError("deploy profile projection is not an object")
        profile = validate_projection(contract, projection)
    except (json.JSONDecodeError, ProfileContractError) as exc:
        print(f"SERVING PROFILE CONTRACT REFUSED: {exc}", file=sys.stderr)
        return 1
    print(
        "SERVING PROFILE CONTRACT VERIFIED: "
        f"profile={projection['serving_profile']} contract={digest} "
        f"model={profile['model']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
