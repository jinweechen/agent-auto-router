#!/usr/bin/env python3
"""Load and validate the offline, versioned benchmark-prior snapshot."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from model_registry import TIERS, ModelRegistry

DEFAULT_BENCHMARK_PRIORS_PATH = Path(__file__).with_name("benchmark_priors.json")
REQUIRED_GUIDANCE = {
    "validatedBoundedCoding",
    "complexDebugging",
    "longContext",
    "multiFile",
    "computerUse",
}


@dataclass(frozen=True)
class BenchmarkPriors:
    version: str
    as_of: str
    source: str
    runtime_network_access: bool
    sources: tuple[dict[str, str], ...]
    model_evidence: dict[str, dict[str, Any]]
    routing_guidance: dict[str, dict[str, Any]]
    limitations: tuple[str, ...]
    raw_payload: dict[str, Any]

    def guidance(self, signal: str) -> dict[str, Any]:
        return dict(self.routing_guidance[signal])


def _require_number(value: Any, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric")
    number = float(value)
    if positive and number <= 0:
        raise ValueError(f"{label} must be positive")
    if not positive and not 0 <= number <= 100:
        raise ValueError(f"{label} must be between 0 and 100")
    return number


def benchmark_priors_from_dict(
    payload: dict[str, Any],
    *,
    registry: ModelRegistry | None = None,
    source: str = "memory",
) -> BenchmarkPriors:
    if payload.get("schemaVersion") != 1:
        raise ValueError("unsupported benchmark priors schemaVersion")
    version = payload.get("version")
    if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", version):
        raise ValueError("benchmark priors version is invalid")
    as_of = payload.get("asOf")
    try:
        date.fromisoformat(as_of)
    except (TypeError, ValueError) as exc:
        raise ValueError("benchmark priors asOf must be an ISO date") from exc
    if payload.get("runtimeNetworkAccess") is not False:
        raise ValueError("benchmark priors must disable runtime network access")

    raw_sources = payload.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise ValueError("benchmark priors sources must be a non-empty list")
    sources: list[dict[str, str]] = []
    source_ids: set[str] = set()
    for index, item in enumerate(raw_sources):
        if not isinstance(item, dict):
            raise ValueError(f"benchmark source {index} must be an object")
        source_id = item.get("id")
        url = item.get("url")
        if not isinstance(source_id, str) or not source_id or source_id in source_ids:
            raise ValueError("benchmark source ids must be unique non-empty strings")
        parsed = urlparse(url) if isinstance(url, str) else None
        if parsed is None or parsed.scheme != "https" or not parsed.netloc:
            raise ValueError(f"benchmark source {source_id} must use HTTPS")
        source_ids.add(source_id)
        sources.append({str(key): str(value) for key, value in item.items()})

    evidence = payload.get("modelEvidence")
    if not isinstance(evidence, dict) or not evidence:
        raise ValueError("benchmark modelEvidence must be a non-empty object")
    known_ids = set(registry.enabled_model_ids) if registry else None
    available_metrics: set[str] = set()
    normalized_evidence: dict[str, dict[str, Any]] = {}
    for model_id, entry in evidence.items():
        if not isinstance(model_id, str) or known_ids is not None and model_id not in known_ids:
            raise ValueError(f"benchmark evidence references unknown model: {model_id}")
        if not isinstance(entry, dict):
            raise ValueError(f"benchmark evidence for {model_id} must be an object")
        pricing = entry.get("pricingUsdPerMillion")
        metrics = entry.get("metrics")
        if not isinstance(pricing, dict) or not isinstance(metrics, dict) or not metrics:
            raise ValueError(f"benchmark evidence for {model_id} needs pricing and metrics")
        for name in ("input", "output"):
            _require_number(pricing.get(name), f"{model_id} pricing {name}", positive=True)
        for metric, value in metrics.items():
            if not isinstance(metric, str) or not metric:
                raise ValueError(f"benchmark metric name for {model_id} is invalid")
            _require_number(value, f"{model_id} metric {metric}")
            available_metrics.add(metric)
        normalized_evidence[model_id] = dict(entry)

    guidance = payload.get("routingGuidance")
    if not isinstance(guidance, dict) or set(guidance) != REQUIRED_GUIDANCE:
        raise ValueError("benchmark routingGuidance must contain the required signals exactly")
    normalized_guidance: dict[str, dict[str, Any]] = {}
    for signal, rule in guidance.items():
        if not isinstance(rule, dict):
            raise ValueError(f"benchmark guidance {signal} must be an object")
        tier_keys = [key for key in ("minimumTier", "recommendedTier") if key in rule]
        if len(tier_keys) != 1 or rule[tier_keys[0]] not in TIERS:
            raise ValueError(f"benchmark guidance {signal} must declare one valid tier")
        reason = rule.get("reason")
        if not isinstance(reason, str) or not re.fullmatch(r"[a-z0-9_]{1,80}", reason):
            raise ValueError(f"benchmark guidance {signal} reason is invalid")
        metrics = rule.get("evidenceMetrics")
        if not isinstance(metrics, list) or not metrics or any(
            not isinstance(metric, str) or metric not in available_metrics for metric in metrics
        ):
            raise ValueError(f"benchmark guidance {signal} references unavailable metrics")
        normalized_guidance[signal] = dict(rule)

    limitations = payload.get("limitations")
    if not isinstance(limitations, list) or not limitations or any(
        not isinstance(item, str) or not item.strip() for item in limitations
    ):
        raise ValueError("benchmark limitations must be a non-empty string list")
    return BenchmarkPriors(
        version=version,
        as_of=as_of,
        source=source,
        runtime_network_access=False,
        sources=tuple(sources),
        model_evidence=normalized_evidence,
        routing_guidance=normalized_guidance,
        limitations=tuple(limitations),
        raw_payload=dict(payload),
    )


def load_benchmark_priors(
    path: Path | None = None,
    *,
    registry: ModelRegistry | None = None,
) -> BenchmarkPriors:
    source_path = path or DEFAULT_BENCHMARK_PRIORS_PATH
    payload = json.loads(source_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("benchmark priors file must contain an object")
    return benchmark_priors_from_dict(payload, registry=registry, source=str(source_path))


def benchmark_priors_digest(priors: BenchmarkPriors) -> str:
    canonical = json.dumps(
        priors.raw_payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
