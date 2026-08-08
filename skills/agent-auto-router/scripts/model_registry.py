from __future__ import annotations

import json
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

REGISTRY_SCHEMA_VERSION = 2
TIERS = ("fast", "balanced", "frontier")
TIER_RANK = {tier: index for index, tier in enumerate(TIERS)}
ROLES = ("direct", "planner", "dispatcher", "worker", "reviewer", "grader")
EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")
DEFAULT_REGISTRY_PATH = Path(__file__).resolve().with_name("model_registry.json")
SAFE_NAME = re.compile(r"[A-Za-z0-9._:/+-]{1,160}")
SAFE_CAPABILITY = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
SAFE_BACKEND = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")


def backend_for_model(model_id: str) -> str:
    """Return the backend prefix of a model id ('codex:gpt-5.6-sol' -> 'codex').

    Unprefixed ids return the default backend 'codex'.
    """
    if ":" in model_id:
        return model_id.split(":", 1)[0]
    return "codex"


def strip_backend_prefix(model_id: str, backend: str | None = None) -> str:
    """Return the model name without its backend prefix.

    When backend is given and the id has a DIFFERENT prefix (or an unprefixed
    id is passed with backend != 'codex'), raise ValueError.
    """
    if ":" in model_id:
        prefix, name = model_id.split(":", 1)
        if backend is not None and prefix != backend:
            raise ValueError(
                f"model {model_id} does not belong to backend {backend}"
            )
        return name
    # No colon in id
    if backend is None:
        return model_id
    if backend == "codex":
        return model_id
    raise ValueError(f"model {model_id} does not belong to backend {backend}")


@dataclass(frozen=True)
class ModelSpec:
    model_id: str
    backend: str = "codex"
    aliases: tuple[str, ...] = ()
    tier: str = "balanced"
    priority: int = 100
    quality_rank: int = 1
    cost_rank: int = 1
    latency_rank: int = 1
    default_effort: str = "medium"
    capabilities: frozenset[str] = frozenset()
    allowed_roles: frozenset[str] = frozenset()
    enabled: bool = True
    auto_eligible: bool = True


class ModelRegistry:
    def __init__(
        self,
        models: Iterable[ModelSpec],
        backends=None,
        source: str = "memory",
    ) -> None:
        self.source = source
        self.models = tuple(models)
        if not self.models:
            raise ValueError("model registry must contain at least one model")
        if backends is None:
            backends = {
                "codex": {"kind": "cli", "adapter": "CodexCliAdapter", "default": True}
            }
        self.backends = dict(backends)
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
        # Validate every model's backend is declared
        for model in self.models:
            if model.backend not in self.backends:
                raise ValueError(
                    f"model {model.model_id} references unknown backend: {model.backend}"
                )
        # Require at least one default backend
        defaults = [k for k, v in self.backends.items() if v.get("default")]
        if not defaults:
            raise ValueError("model registry must declare at least one default backend")
        # Existing validation
        enabled = [model for model in self.models if model.enabled]
        if not enabled:
            raise ValueError("model registry must enable at least one model")
        self.resolve_tier(
            "frontier", role="direct", required_capabilities=("high-risk-primary",)
        )

    @property
    def default_backend(self) -> str:
        for name, info in self.backends.items():
            if info.get("default"):
                return name
        return next(iter(self.backends))

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

    def get(
        self,
        value: str,
        *,
        role: str | None = None,
        backend: str | None = None,
    ) -> ModelSpec:
        model = self._by_id.get(value) or self._by_alias.get(value.lower())
        if model is None or not model.enabled:
            raise ValueError(f"model is not enabled in the trusted registry: {value}")
        base_role = role.split(":", 1)[0] if role else None
        if base_role and base_role not in model.allowed_roles:
            raise ValueError(f"model {model.model_id} is not allowed for role {base_role}")
        if backend is not None and model.backend != backend:
            raise ValueError(f"model {model.model_id} does not belong to backend {backend}")
        return model

    def resolve_tier(
        self,
        tier: str,
        *,
        role: str,
        required_capabilities: Iterable[str] = (),
        backends: Iterable[str] | None = None,
        allow_explicit_only: bool = False,
    ) -> ModelSpec:
        if tier not in TIERS:
            raise ValueError(f"unknown model tier: {tier}")
        base_role = role.split(":", 1)[0]
        required = frozenset(required_capabilities)
        candidates = [
            model
            for model in self.models
            if model.enabled
            and (model.auto_eligible or allow_explicit_only)
            and model.tier == tier
            and base_role in model.allowed_roles
            and required.issubset(model.capabilities)
        ]
        if backends is not None:
            backends_set = frozenset(backends)
            candidates = [m for m in candidates if m.backend in backends_set]
        if not candidates:
            capability_text = ",".join(sorted(required)) or "none"
            msg = (
                f"no enabled {tier} model supports role={base_role}"
                f" capabilities={capability_text}"
            )
            if backends is not None:
                msg += f" backends={sorted(backends)}"
            raise ValueError(msg)
        return sorted(
            candidates,
            key=lambda model: (
                model.priority,
                0 if model.backend == self.default_backend else 1,
                model.model_id,
            ),
        )[0]

    def tier_for_model(self, model_id_or_alias: str) -> str:
        return self.get(model_id_or_alias).tier


def model_spec_from_dict(payload: dict[str, Any]) -> ModelSpec:
    model_id = str(payload.get("id", ""))
    if not SAFE_NAME.fullmatch(model_id):
        raise ValueError("model id contains unsupported characters")

    # Backend resolution
    backend_raw = payload.get("backend")
    if backend_raw is not None:
        if not isinstance(backend_raw, str) or not SAFE_BACKEND.fullmatch(backend_raw):
            raise ValueError(f"invalid backend for model {model_id}")
        backend = backend_raw
    else:
        backend = None

    if ":" in model_id:
        prefix = model_id.split(":", 1)[0]
        if backend is not None and backend != prefix:
            raise ValueError(
                f"model {model_id} backend field {backend!r} does not match id prefix {prefix!r}"
            )
        backend = prefix
    elif backend is None:
        backend = "codex"

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
        backend=backend,
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
        "backend": model.backend,
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
        "backends": dict(sorted(registry.backends.items())),
        "models": [model_spec_to_dict(model) for model in registry.models],
    }


def registry_digest(registry: ModelRegistry) -> str:
    payload = json.dumps(
        registry_to_dict(registry), ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def registry_from_dict(payload: dict[str, Any], source: str = "memory") -> ModelRegistry:
    schema_version = payload.get("schemaVersion")
    if schema_version == 2:
        backends_raw = payload.get("backends")
        if not isinstance(backends_raw, dict):
            raise ValueError("model registry backends must be an object")
        backends = {}
        for name, info in backends_raw.items():
            if not isinstance(info, dict):
                raise ValueError(f"backend {name} must be an object")
            backends[name] = dict(info)
        defaults = [k for k, v in backends.items() if v.get("default")]
        if not defaults:
            raise ValueError("model registry must declare at least one default backend")
        raw_models = payload.get("models")
        if not isinstance(raw_models, list) or not all(isinstance(item, dict) for item in raw_models):
            raise ValueError("model registry models must be an array of objects")
        return ModelRegistry((model_spec_from_dict(item) for item in raw_models), backends, source)
    elif schema_version == 1 or schema_version is None:
        # v1 migration: wrap everything in a codex backend
        backends = {"codex": {"kind": "cli", "adapter": "CodexCliAdapter", "default": True}}
        raw_models = payload.get("models")
        if not isinstance(raw_models, list) or not all(isinstance(item, dict) for item in raw_models):
            raise ValueError("model registry models must be an array of objects")
        migrated_models = []
        for item in raw_models:
            item = dict(item)
            item["backend"] = "codex"
            model_id = str(item.get("id", ""))
            if ":" not in model_id:
                item["id"] = f"codex:{model_id}"
            migrated_models.append(item)
        return ModelRegistry(
            (model_spec_from_dict(item) for item in migrated_models), backends, source
        )
    else:
        raise ValueError(
            f"unsupported model registry schemaVersion: {schema_version}"
        )


def load_model_registry(path: Path | None = None) -> ModelRegistry:
    registry_path = path or DEFAULT_REGISTRY_PATH
    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("model registry must contain an object")
    return registry_from_dict(payload, str(registry_path))
