from __future__ import annotations

import json
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

REGISTRY_SCHEMA_VERSION = 1
TIERS = ("fast", "balanced", "frontier")
TIER_RANK = {tier: index for index, tier in enumerate(TIERS)}
ROLES = ("direct", "planner", "dispatcher", "worker", "reviewer", "grader")
EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")
DEFAULT_REGISTRY_PATH = Path(__file__).resolve().with_name("model_registry.json")
SAFE_NAME = re.compile(r"[A-Za-z0-9._:/+-]{1,160}")
SAFE_CAPABILITY = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


@dataclass(frozen=True)
class ModelSpec:
    model_id: str
    aliases: tuple[str, ...]
    tier: str
    priority: int
    quality_rank: int
    cost_rank: int
    latency_rank: int
    default_effort: str
    capabilities: frozenset[str]
    allowed_roles: frozenset[str]
    enabled: bool
    auto_eligible: bool


class ModelRegistry:
    def __init__(self, models: Iterable[ModelSpec], source: str = "memory") -> None:
        self.source = source
        self.models = tuple(models)
        if not self.models:
            raise ValueError("model registry must contain at least one model")
        self._by_id: dict[str, ModelSpec] = {}
        self._by_alias: dict[str, ModelSpec] = {}
        seen_names: set[str] = set()
        for model in self.models:
            model_key = model.model_id.lower()
            if model_key in seen_names:
                raise ValueError(f"duplicate model id or alias: {model.model_id}")
            seen_names.add(model_key)
            self._by_id[model.model_id] = model
            for alias in model.aliases:
                key = alias.lower()
                if key in seen_names:
                    raise ValueError(f"duplicate model alias: {alias}")
                seen_names.add(key)
                self._by_alias[key] = model
        enabled = [model for model in self.models if model.enabled]
        if not enabled:
            raise ValueError("model registry must enable at least one model")
        self.resolve_tier(
            "frontier", role="direct", required_capabilities=("high-risk-primary",)
        )

    @property
    def enabled_model_ids(self) -> tuple[str, ...]:
        return tuple(model.model_id for model in self.models if model.enabled)

    @property
    def accepted_model_choices(self) -> tuple[str, ...]:
        aliases = [alias for model in self.models if model.enabled for alias in model.aliases]
        return tuple((*aliases, *self.enabled_model_ids))

    @property
    def auto_model_ids(self) -> tuple[str, ...]:
        return tuple(
            model.model_id for model in self.models if model.enabled and model.auto_eligible
        )

    def get(self, value: str, *, role: str | None = None) -> ModelSpec:
        model = self._by_id.get(value) or self._by_alias.get(value.lower())
        if model is None or not model.enabled:
            raise ValueError(f"model is not enabled in the trusted registry: {value}")
        base_role = role.split(":", 1)[0] if role else None
        if base_role and base_role not in model.allowed_roles:
            raise ValueError(f"model {model.model_id} is not allowed for role {base_role}")
        return model

    def resolve_tier(
        self,
        tier: str,
        *,
        role: str,
        required_capabilities: Iterable[str] = (),
    ) -> ModelSpec:
        if tier not in TIERS:
            raise ValueError(f"unknown model tier: {tier}")
        base_role = role.split(":", 1)[0]
        required = frozenset(required_capabilities)
        candidates = [
            model
            for model in self.models
            if model.enabled
            and model.auto_eligible
            and model.tier == tier
            and base_role in model.allowed_roles
            and required.issubset(model.capabilities)
        ]
        if not candidates:
            capability_text = ",".join(sorted(required)) or "none"
            raise ValueError(
                f"no enabled {tier} model supports role={base_role} capabilities={capability_text}"
            )
        return sorted(candidates, key=lambda model: (model.priority, model.model_id))[0]

    def tier_for_model(self, model_id_or_alias: str) -> str:
        return self.get(model_id_or_alias).tier


def model_spec_from_dict(payload: dict[str, Any]) -> ModelSpec:
    model_id = str(payload.get("id", ""))
    if not SAFE_NAME.fullmatch(model_id):
        raise ValueError("model id contains unsupported characters")
    aliases_raw = payload.get("aliases", [])
    capabilities_raw = payload.get("capabilities", [])
    roles_raw = payload.get("allowedRoles", [])
    if not isinstance(aliases_raw, list) or not all(
        isinstance(alias, str) and SAFE_NAME.fullmatch(alias) for alias in aliases_raw
    ):
        raise ValueError(f"invalid aliases for model {model_id}")
    if not isinstance(capabilities_raw, list) or not all(
        isinstance(item, str) and SAFE_CAPABILITY.fullmatch(item)
        for item in capabilities_raw
    ):
        raise ValueError(f"invalid capabilities for model {model_id}")
    if not isinstance(roles_raw, list) or not roles_raw or not set(roles_raw).issubset(ROLES):
        raise ValueError(f"invalid allowedRoles for model {model_id}")
    tier = str(payload.get("tier", ""))
    effort = str(payload.get("defaultEffort", ""))
    if tier not in TIERS:
        raise ValueError(f"invalid tier for model {model_id}")
    if effort not in EFFORTS:
        raise ValueError(f"invalid defaultEffort for model {model_id}")

    def bounded_rank(key: str) -> int:
        value = payload.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 10:
            raise ValueError(f"{key} for model {model_id} must be an integer from 1 to 10")
        return value

    priority = payload.get("priority", 100)
    if isinstance(priority, bool) or not isinstance(priority, int) or priority < 0:
        raise ValueError(f"priority for model {model_id} must be a non-negative integer")
    enabled = payload.get("enabled")
    auto_eligible = payload.get("autoEligible")
    if not isinstance(enabled, bool) or not isinstance(auto_eligible, bool):
        raise ValueError(f"enabled and autoEligible for model {model_id} must be booleans")
    if auto_eligible and not enabled:
        raise ValueError(f"model {model_id} cannot be autoEligible while disabled")
    return ModelSpec(
        model_id=model_id,
        aliases=tuple(aliases_raw),
        tier=tier,
        priority=priority,
        quality_rank=bounded_rank("qualityRank"),
        cost_rank=bounded_rank("costRank"),
        latency_rank=bounded_rank("latencyRank"),
        default_effort=effort,
        capabilities=frozenset(capabilities_raw),
        allowed_roles=frozenset(roles_raw),
        enabled=enabled,
        auto_eligible=auto_eligible,
    )


def model_spec_to_dict(model: ModelSpec) -> dict[str, Any]:
    return {
        "id": model.model_id,
        "aliases": list(model.aliases),
        "tier": model.tier,
        "priority": model.priority,
        "qualityRank": model.quality_rank,
        "costRank": model.cost_rank,
        "latencyRank": model.latency_rank,
        "defaultEffort": model.default_effort,
        "capabilities": sorted(model.capabilities),
        "allowedRoles": sorted(model.allowed_roles),
        "enabled": model.enabled,
        "autoEligible": model.auto_eligible,
    }


def registry_to_dict(registry: ModelRegistry) -> dict[str, Any]:
    return {
        "schemaVersion": REGISTRY_SCHEMA_VERSION,
        "models": [model_spec_to_dict(model) for model in registry.models],
    }


def registry_digest(registry: ModelRegistry) -> str:
    payload = json.dumps(
        registry_to_dict(registry), ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def registry_from_dict(payload: dict[str, Any], source: str = "memory") -> ModelRegistry:
    if payload.get("schemaVersion") != REGISTRY_SCHEMA_VERSION:
        raise ValueError("unsupported model registry schemaVersion")
    raw_models = payload.get("models")
    if not isinstance(raw_models, list) or not all(isinstance(item, dict) for item in raw_models):
        raise ValueError("model registry models must be an array of objects")
    return ModelRegistry((model_spec_from_dict(item) for item in raw_models), source)


def load_model_registry(path: Path | None = None) -> ModelRegistry:
    registry_path = path or DEFAULT_REGISTRY_PATH
    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("model registry must contain an object")
    return registry_from_dict(payload, str(registry_path))
