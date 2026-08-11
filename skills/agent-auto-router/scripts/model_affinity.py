"""Privacy-safe model affinity and cache-pressure decisions."""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from model_registry import TIER_RANK, ModelRegistry


MODEL_AFFINITY_SCHEMA = "agent-auto-router.model-affinity.v1"
MODEL_AFFINITY_MODES = ("auto", "off")
DEFAULT_MODEL_AFFINITY_MODE = "auto"
ROLE_MODEL_POLICY_AFFINITY = "selected-model-preferred"
ROLE_MODEL_POLICY_PROFILE = "profile"
AFFINITY_TTL_SECONDS = 30 * 60
MINIMUM_STRONGER_TIER_CACHE_SIGNAL = 0.15
PROFILE_PREFERRED_MAXIMUM_CACHE_SIGNAL = 0.05
PROFILE_PREFERRED_MINIMUM_SAMPLES = 3


def workspace_identity(workdir: Path | str | None) -> str | None:
    """Hash a normalized workdir without retaining the path itself."""
    if workdir is None:
        return None
    normalized = os.path.normcase(str(Path(workdir).resolve(strict=False))).replace("\\", "/")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _event_time(event: dict[str, Any]) -> datetime | None:
    value = event.get("recordedAt")
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _cache_summary(
    events: Iterable[dict[str, Any]],
    *,
    selected_model: str | None = None,
) -> dict[str, Any]:
    samples = 0
    input_tokens = 0
    cached_input = 0
    cache_write = 0
    for event in events:
        observed = (
            event.get("selectedModelObservedTokens")
            if selected_model is not None
            and event.get("selectedModel") == selected_model
            else event.get("observedTokens")
            if selected_model is None
            else None
        )
        if not isinstance(observed, dict):
            continue
        current_input = observed.get("input")
        current_cached = observed.get("cached_input")
        current_write = observed.get("cache_write")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (current_input, current_cached, current_write)
        ):
            continue
        if current_input <= 0:
            continue
        samples += 1
        input_tokens += current_input
        cached_input += current_cached
        cache_write += current_write
    cache_signal = (
        min(1.0, (cached_input + cache_write) / input_tokens)
        if input_tokens else None
    )
    return {
        "samples": samples,
        "inputTokens": input_tokens,
        "cachedInputTokens": cached_input,
        "cacheWriteInputTokens": cache_write,
        "cacheReadRatio": (cached_input / input_tokens) if input_tokens else None,
        "cacheWriteRatio": (cache_write / input_tokens) if input_tokens else None,
        "cacheSignalRatio": cache_signal,
        "billingCostEstimated": False,
    }


def resolve_model_affinity(
    events: Iterable[dict[str, Any]],
    *,
    workspace_key: str | None,
    strategy: str,
    selector_model: str,
    target_tier: str,
    registry: ModelRegistry,
    available_backends: Iterable[str],
    required_capabilities: Iterable[str] = (),
    mode: str = DEFAULT_MODEL_AFFINITY_MODE,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Choose an affinity model without ever retaining a weaker capability tier."""
    if mode not in MODEL_AFFINITY_MODES:
        raise ValueError("model affinity mode must be auto or off")
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    result: dict[str, Any] = {
        "schema": MODEL_AFFINITY_SCHEMA,
        "mode": mode,
        "workspaceKey": workspace_key,
        "storesWorkspacePath": False,
        "selectorModel": selector_model,
        "selectedModel": selector_model,
        "targetTier": target_tier,
        "selectedTier": target_tier,
        "applied": False,
        "reason": "disabled" if mode == "off" else "no-recent-workspace-evidence",
        "retainedStrongerTier": False,
        "previousModel": None,
        "previousModelAgeSeconds": None,
        "roleModelPolicy": (
            ROLE_MODEL_POLICY_PROFILE
            if mode == "off" else ROLE_MODEL_POLICY_AFFINITY
        ),
        "evidence": _cache_summary(()),
        "modelCalls": 0,
    }
    if mode == "off" or workspace_key is None:
        return result

    recent: list[tuple[datetime, dict[str, Any]]] = []
    for event in events:
        if (
            event.get("eventType") != "route_outcome"
            or event.get("workspaceKey") != workspace_key
            or event.get("strategy") != strategy
            or event.get("executionSucceeded") is not True
            or event.get("explicitOverride") is True
        ):
            continue
        recorded_at = _event_time(event)
        if recorded_at is None:
            continue
        age = (current_time - recorded_at.astimezone(timezone.utc)).total_seconds()
        if 0 <= age <= AFFINITY_TTL_SECONDS:
            recent.append((recorded_at, event))
    if not recent:
        return result
    recent.sort(key=lambda item: item[0])
    evidence = _cache_summary(event for _, event in recent[-8:])
    result["evidence"] = evidence
    if (
        evidence["samples"] >= PROFILE_PREFERRED_MINIMUM_SAMPLES
        and evidence["cacheSignalRatio"] is not None
        and evidence["cacheSignalRatio"] < PROFILE_PREFERRED_MAXIMUM_CACHE_SIGNAL
    ):
        result["roleModelPolicy"] = ROLE_MODEL_POLICY_PROFILE

    previous_time, previous_event = recent[-1]
    previous_model = str(previous_event.get("selectedModel") or "")
    result["previousModel"] = previous_model or None
    result["previousModelAgeSeconds"] = int(
        max(0, (current_time - previous_time.astimezone(timezone.utc)).total_seconds())
    )
    try:
        previous = registry.get(previous_model, role="direct")
        selector = registry.get(selector_model, role="direct")
    except ValueError:
        result["reason"] = "previous-model-no-longer-trusted"
        return result
    allowed_backends = frozenset(available_backends)
    required = frozenset(required_capabilities)
    if (
        not previous.auto_eligible
        or previous.backend not in allowed_backends
        or previous.backend != selector.backend
        or not required.issubset(previous.capabilities)
    ):
        result["reason"] = "previous-model-not-eligible"
        return result
    rank_delta = TIER_RANK[previous.tier] - TIER_RANK[target_tier]
    if rank_delta < 0:
        result["reason"] = "previous-model-weaker-than-current-requirement"
        return result
    if rank_delta > 1:
        result["reason"] = "previous-model-more-than-one-tier-stronger"
        return result
    if previous.model_id == selector.model_id:
        result["reason"] = "already-selected"
        result["selectedTier"] = previous.tier
        return result
    previous_evidence = _cache_summary(
        (
            event for _, event in recent[-8:]
            if event.get("selectedModel") == previous.model_id
        ),
        selected_model=previous.model_id,
    )
    if rank_delta == 1 and (
        previous_evidence["cacheSignalRatio"] is None
        or previous_evidence["cacheSignalRatio"] < MINIMUM_STRONGER_TIER_CACHE_SIGNAL
    ):
        result["reason"] = "stronger-model-cache-signal-insufficient"
        return result
    result.update(
        {
            "selectedModel": previous.model_id,
            "selectedTier": previous.tier,
            "applied": True,
            "reason": (
                "same-tier-session-affinity"
                if rank_delta == 0 else "cache-supported-stronger-tier-affinity"
            ),
            "retainedStrongerTier": rank_delta == 1,
            "previousModelEvidence": previous_evidence,
        }
    )
    return result
