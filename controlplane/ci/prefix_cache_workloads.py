#!/usr/bin/env python3
"""Two immutable PARA workload series for prefix-cache measurement.

`frozen_anchor_v1` delegates to the already-sealed public-batch generator and
therefore retains sha256 2392bb58... byte-for-byte.  Its varying case id comes
first, making the long common suffix unreachable to a prefix cache; a measured
zero hit rate is the registered PASS outcome.

`realistic_agent_prefix_v1` carries the same words and request parameters but
places the shared operations context before the varying question.  It models
the system-prompt/document-first shape of background-agent turns.  It is a
separate workload series: its cost must never be blended into, substituted for,
or described as a technology win over the frozen-anchor series.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from public_batch_canary import (
    EXPECTED_PROMPT_TOKENS,
    EXPECTED_WORKLOAD_SHA256,
    MAX_COMPLETION_TOKENS,
    MODEL,
    N_JOBS,
    PARA,
    workload as frozen_anchor_workload,
)


FROZEN_ANCHOR_WORKLOAD = "frozen_anchor_v1"
REALISTIC_AGENT_WORKLOAD = "realistic_agent_prefix_v1"
REALISTIC_AGENT_WORKLOAD_SHA256 = (
    "b9cc41bb6b9985bd077ca4204a4c6f0c16e1012410919f5a3514e5ff3219d6e5"
)


def workload(variant: str) -> tuple[list[dict[str, Any]], bytes]:
    if variant == FROZEN_ANCHOR_WORKLOAD:
        return frozen_anchor_workload()
    if variant != REALISTIC_AGENT_WORKLOAD:
        raise ValueError(f"unknown immutable workload variant {variant!r}")

    rows = []
    for index in range(N_JOBS):
        question = (
            f"Case {index:04d}. Read the operations context and answer in one "
            "sentence: which single lever most reduces delivered cost?"
        )
        rows.append(
            {
                "model": MODEL,
                "messages": [
                    {"role": "user", "content": PARA * 55 + "\n\n" + question}
                ],
                "max_completion_tokens": MAX_COMPLETION_TOKENS,
                "temperature": 0,
                "seed": 0,
            }
        )
    blob = "\n".join(json.dumps(row, sort_keys=True) for row in rows).encode()
    digest = hashlib.sha256(blob).hexdigest()
    if digest != REALISTIC_AGENT_WORKLOAD_SHA256:
        raise RuntimeError(
            "realistic agent workload drift: "
            f"{digest} != {REALISTIC_AGENT_WORKLOAD_SHA256}"
        )
    return rows, blob


def cache_prediction(variant: str) -> dict[str, Any]:
    """Return the prediction registered before any scored cache run."""
    if variant == FROZEN_ANCHOR_WORKLOAD:
        return {
            "kind": "pre_registered_prediction_not_measurement",
            "workload_sha256": EXPECTED_WORKLOAD_SHA256,
            "vllm_block_tokens": 16,
            "cross_request_hit_fraction_numerator": 0,
            "cross_request_hit_fraction_denominator": N_JOBS
            * EXPECTED_PROMPT_TOKENS,
            "interpretation": (
                "approximately zero is expected and is a PASS: the unique case "
                "identifier precedes the common suffix"
            ),
        }
    if variant == REALISTIC_AGENT_WORKLOAD:
        # 3,920 block-aligned common-prefix tokens out of each 3,960-token
        # prompt; request 1 is cold and requests 2..300 may hit.  Exact
        # registered fraction: 1,172,080 / 1,188,000 = 98.6599326599%.
        return {
            "kind": "pre_registered_prediction_not_measurement",
            "workload_sha256": REALISTIC_AGENT_WORKLOAD_SHA256,
            "vllm_block_tokens": 16,
            "cacheable_common_prefix_tokens_per_warm_request": 3_920,
            "prompt_tokens_per_request": EXPECTED_PROMPT_TOKENS,
            "cold_requests": 1,
            "warm_requests": N_JOBS - 1,
            "cross_request_hit_fraction_numerator": (N_JOBS - 1) * 3_920,
            "cross_request_hit_fraction_denominator": N_JOBS
            * EXPECTED_PROMPT_TOKENS,
            "interpretation": (
                "predicted 98.66% prompt-token hit fraction on realistic "
                "shared-context-first traffic"
            ),
        }
    raise ValueError(f"unknown immutable workload variant {variant!r}")


def contract() -> dict[str, Any]:
    return {
        "object": "prefix_cache_workload_contract",
        "series": [
            {
                "name": FROZEN_ANCHOR_WORKLOAD,
                "sha256": EXPECTED_WORKLOAD_SHA256,
                "continuity_anchor": True,
                "cache_prediction": cache_prediction(FROZEN_ANCHOR_WORKLOAD),
            },
            {
                "name": REALISTIC_AGENT_WORKLOAD,
                "sha256": REALISTIC_AGENT_WORKLOAD_SHA256,
                "continuity_anchor": False,
                "cache_prediction": cache_prediction(REALISTIC_AGENT_WORKLOAD),
            },
        ],
        "comparison_rule": (
            "realistic_agent_prefix_v1 is a separate workload series and MUST NOT "
            "be blended into or compared as a technology win against the frozen "
            "anchor's sealed cost series"
        ),
    }


if __name__ == "__main__":
    print(json.dumps(contract(), sort_keys=True, indent=2))
