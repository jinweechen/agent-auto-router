"""Privacy-safe model affinity and cache-pressure decisions."""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from model_registry import EFFORTS, TIER_RANK, ModelRegistry
from protocol_schemas import MODEL_AFFINITY_SCHEMA


MODEL_AFFINITY_MODES = ("session", "sticky", "auto", "off")
DEFAULT_MODEL_AFFINITY_MODE = "session"
ROLE_MODEL_POLICY_AFFINITY = "selected-model-preferred"
ROLE_MODEL_POLICY_PROFILE = "profile"
AFFINITY_TTL_SECONDS = 30 * 60
MINIMUM_STRONGER_TIER_CACHE_READ_RATIO = 0.15
PROFILE_PREFERRED_MAXIMUM_CACHE_READ_RATIO = 0.05
PROFILE_PREFERRED_MINIMUM_SAMPLES = 3
CONVERSATION_KEY_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
EFFORT_RANK = {effort: index for index, effort in enumerate(EFFORTS)}
MINIMUM_PIN_RESIDENCY_TURNS = 3
MINIMUM_SWITCH_COOLDOWN_SECONDS = 10 * 60
PIN_SWITCH_ACTIONS = frozenset({
    "none",
    "keep",
    "upgrade",
    "upgrade-effort",
    "downgrade",
    "replace-unavailable",
})


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


def _nonnegative_integer(value: int | None, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _available_model_set(
    available_model_ids: Iterable[str] | None,
    registry: ModelRegistry,
) -> frozenset[str] | None:
    """Normalize a trusted exact runtime snapshot without exposing it in the route."""
    if available_model_ids is None:
        return None
    if isinstance(available_model_ids, (str, bytes)):
        raise ValueError("available model IDs must be an iterable of model ID strings")
    normalized: set[str] = set()
    for raw_value in available_model_ids:
        if not isinstance(raw_value, str):
            raise ValueError("available model IDs must contain only strings")
        value = raw_value.strip()
        if not value:
            continue
        lowered = value.lower()
        for spec in registry.models:
            accepted = {
                spec.model_id.lower(),
                spec.model_id.split(":", 1)[-1].lower(),
                *(alias.lower() for alias in spec.aliases),
            }
            if lowered in accepted:
                normalized.add(spec.model_id)
                break
    return frozenset(normalized)


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
    # Cache reads are reuse evidence. Cache writes are a rebuild cost and must
    # never make retention of a more expensive model look more attractive.
    cache_signal = (cached_input / input_tokens) if input_tokens else None
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
    conversation_key_hash: str | None = None,
    pinned_model: str | None = None,
    selector_effort: str | None = None,
    pinned_effort: str | None = None,
    pin_turns: int | None = None,
    last_switch_age_seconds: int | None = None,
    checkpoint_reached: bool = False,
    confirm_pin_downgrade: bool = False,
    available_model_ids: Iterable[str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Choose an affinity model without ever retaining a weaker capability tier."""
    if mode not in MODEL_AFFINITY_MODES:
        raise ValueError("model affinity mode must be session, sticky, auto, or off")
    if selector_effort is not None and selector_effort not in EFFORTS:
        raise ValueError("selector effort is invalid")
    if not isinstance(checkpoint_reached, bool):
        raise ValueError("checkpoint reached must be boolean")
    if not isinstance(confirm_pin_downgrade, bool):
        raise ValueError("confirm pin downgrade must be boolean")
    normalized_pin_turns = _nonnegative_integer(pin_turns, "pin turns")
    normalized_switch_age = _nonnegative_integer(
        last_switch_age_seconds, "last switch age seconds"
    )
    if mode == "sticky":
        valid_conversation_key = (
            isinstance(conversation_key_hash, str)
            and CONVERSATION_KEY_HASH_PATTERN.fullmatch(conversation_key_hash)
        )
        if not valid_conversation_key:
            raise ValueError(
                "sticky model affinity requires a lowercase HMAC-SHA256 "
                "conversation key hash"
            )
        if not isinstance(pinned_model, str) or not pinned_model.strip():
            raise ValueError("sticky model affinity requires a pinned model")
        if pinned_effort is not None and pinned_effort not in EFFORTS:
            raise ValueError("sticky model affinity pinned effort is invalid")
    elif any((
        conversation_key_hash is not None,
        pinned_model is not None,
        pinned_effort is not None,
        pin_turns is not None,
        last_switch_age_seconds is not None,
        checkpoint_reached,
        confirm_pin_downgrade,
    )):
        raise ValueError(
            "conversation pin state requires sticky model affinity"
        )
    exact_available_models = _available_model_set(available_model_ids, registry)
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    result: dict[str, Any] = {
        "schema": MODEL_AFFINITY_SCHEMA,
        "mode": mode,
        "workspaceKey": workspace_key,
        "storesWorkspacePath": False,
        "conversationKeyHash": conversation_key_hash,
        "storesConversationKey": False,
        "pinnedModel": pinned_model,
        "pinnedEffort": pinned_effort,
        "pinUpdateRequired": False,
        "pinUpdateModel": None,
        "pinUpdateEffort": None,
        "pinTurns": normalized_pin_turns,
        "lastSwitchAgeSeconds": normalized_switch_age,
        "checkpointReached": bool(checkpoint_reached),
        "downgradeConfirmed": bool(confirm_pin_downgrade),
        "minimumResidencyTurns": MINIMUM_PIN_RESIDENCY_TURNS,
        "minimumSwitchCooldownSeconds": MINIMUM_SWITCH_COOLDOWN_SECONDS,
        "switchAction": "none",
        "switchReason": None,
        "switchBlockedReasons": [],
        "availabilityChecked": exact_available_models is not None,
        "selectorModel": selector_model,
        "selectedModel": selector_model,
        "selectorEffort": selector_effort,
        "selectedEffort": selector_effort,
        "effortApplied": False,
        "targetTier": target_tier,
        "selectedTier": target_tier,
        "applied": False,
        "reason": (
            "disabled" if mode == "off"
            else "session-role-reuse" if mode == "session"
            else "conversation-pin-unresolved" if mode == "sticky"
            else "no-recent-workspace-evidence"
        ),
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
    if mode in {"off", "session"}:
        return result

    if mode == "sticky":
        allowed_backends = frozenset(available_backends)
        required = frozenset(required_capabilities)
        try:
            selector = registry.get(selector_model, role="direct")
        except ValueError:
            result["reason"] = "selector-model-no-longer-trusted"
            return result
        selector_available = (
            exact_available_models is None
            or selector.model_id in exact_available_models
        )
        try:
            pinned = registry.get(str(pinned_model), role="direct")
        except ValueError:
            if not selector_available:
                raise ValueError(
                    "neither pinned nor selector model is available in the exact "
                    "runtime snapshot"
                )
            result["reason"] = "pinned-model-no-longer-trusted"
            result["pinUpdateRequired"] = True
            result["pinUpdateModel"] = selector.model_id
            result["pinUpdateEffort"] = selector_effort
            result["switchAction"] = "replace-unavailable"
            result["switchReason"] = "pinned-model-no-longer-trusted"
            return result
        result["pinnedModel"] = pinned.model_id
        result["previousModel"] = pinned.model_id
        if exact_available_models is not None and pinned.model_id not in exact_available_models:
            if not selector_available:
                raise ValueError(
                    "neither pinned nor selector model is available in the exact "
                    "runtime snapshot"
                )
            result.update({
                "reason": "pinned-model-unavailable",
                "pinUpdateRequired": True,
                "pinUpdateModel": selector.model_id,
                "pinUpdateEffort": selector_effort,
                "switchAction": "replace-unavailable",
                "switchReason": "exact-runtime-model-unavailable",
            })
            return result
        if (
            not pinned.auto_eligible
            or pinned.backend not in allowed_backends
            or pinned.backend != selector.backend
            or not required.issubset(pinned.capabilities)
        ):
            if not selector_available:
                raise ValueError(
                    "pinned model is ineligible and the selector model is unavailable "
                    "in the exact runtime snapshot"
                )
            result["reason"] = "pinned-model-not-eligible"
            result["pinUpdateRequired"] = True
            result["pinUpdateModel"] = selector.model_id
            result["pinUpdateEffort"] = selector_effort
            result["switchAction"] = "upgrade"
            result["switchReason"] = "pinned-model-not-eligible"
            return result
        rank_delta = TIER_RANK[pinned.tier] - TIER_RANK[target_tier]
        if rank_delta < 0:
            if not selector_available:
                raise ValueError(
                    "pinned model is weaker than required and the selector model is "
                    "unavailable in the exact runtime snapshot"
                )
            result["reason"] = "pinned-model-weaker-than-current-requirement"
            result["pinUpdateRequired"] = True
            result["pinUpdateModel"] = selector.model_id
            result["pinUpdateEffort"] = selector_effort
            result["switchAction"] = "upgrade"
            result["switchReason"] = "stronger-tier-required"
            return result

        selected_effort = selector_effort
        if pinned_effort is not None:
            if (
                selector_effort is not None
                and EFFORT_RANK[pinned_effort] < EFFORT_RANK[selector_effort]
            ):
                result.update({
                    "pinUpdateRequired": True,
                    "pinUpdateModel": pinned.model_id,
                    "pinUpdateEffort": selector_effort,
                    "switchAction": "upgrade-effort",
                    "switchReason": "stronger-effort-required",
                })
            else:
                selected_effort = pinned_effort
                result["effortApplied"] = pinned_effort != selector_effort

        if rank_delta > 0 and confirm_pin_downgrade:
            blocked_reasons: list[str] = []
            if not selector_available:
                blocked_reasons.append("selector-model-unavailable")
            if normalized_pin_turns is None or normalized_pin_turns < MINIMUM_PIN_RESIDENCY_TURNS:
                blocked_reasons.append("minimum-residency-not-met")
            if (
                normalized_switch_age is None
                or normalized_switch_age < MINIMUM_SWITCH_COOLDOWN_SECONDS
            ):
                blocked_reasons.append("switch-cooldown-not-met")
            if not checkpoint_reached:
                blocked_reasons.append("checkpoint-required")
            if not blocked_reasons:
                result.update({
                    "reason": "conversation-pin-downgrade-confirmed",
                    "pinUpdateRequired": True,
                    "pinUpdateModel": selector.model_id,
                    "pinUpdateEffort": selector_effort,
                    "selectedEffort": selector_effort,
                    "effortApplied": False,
                    "switchAction": "downgrade",
                    "switchReason": "explicit-checkpoint-downgrade",
                })
                return result
            result["switchBlockedReasons"] = blocked_reasons

        result["selectedTier"] = pinned.tier
        result["selectedEffort"] = selected_effort
        result["retainedStrongerTier"] = rank_delta > 0
        if result["switchAction"] == "none":
            result["switchAction"] = "keep"
            result["switchReason"] = (
                "sticky-continuity" if rank_delta > 0 else "same-tier-continuity"
            )
        if pinned.model_id == selector.model_id:
            result["reason"] = "conversation-pin-already-selected"
            return result
        result.update(
            {
                "selectedModel": pinned.model_id,
                "applied": True,
                "reason": "conversation-sticky-affinity",
            }
        )
        return result

    if workspace_key is None:
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
        and evidence["cacheSignalRatio"] < PROFILE_PREFERRED_MAXIMUM_CACHE_READ_RATIO
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
        or previous_evidence["cacheSignalRatio"] < MINIMUM_STRONGER_TIER_CACHE_READ_RATIO
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
