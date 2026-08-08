from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from model_registry import EFFORTS, ROLES, TIERS, ModelRegistry, ModelSpec

PROFILE_SCHEMA_VERSION = 1
DEFAULT_PROFILES_PATH = Path(__file__).resolve().with_name("orchestration_profiles.json")
REQUIRED_ROLES = {
    "A": {"direct", "grader"},
    "B": {"planner", "worker", "reviewer", "grader"},
    "C": {"planner", "dispatcher", "worker", "reviewer", "grader"},
    "D": {"planner", "worker", "reviewer", "grader"},
    "E": {"direct", "grader"},
    "F": {"direct", "grader"},
}


@dataclass(frozen=True)
class RoleAssignment:
    tier: str | None
    model: str | None
    effort: str

    def resolve(
        self,
        registry: ModelRegistry,
        role: str,
        *,
        required_capabilities: Iterable[str] = (),
        required_tier: str | None = None,
        backends: Iterable[str] | None = None,
        allow_explicit_only: bool = False,
    ) -> ModelSpec:
        required = frozenset(required_capabilities)
        if self.model:
            resolved = registry.get(self.model, role=role)
            if backends is not None and resolved.backend not in backends:
                raise ValueError(
                    f"profile model {resolved.model_id} not in requested backends"
                )
            if not resolved.auto_eligible:
                raise ValueError(
                    f"profile model {resolved.model_id} is not eligible for Auto role={role}"
                )
            if not required.issubset(resolved.capabilities):
                capability_text = ",".join(sorted(required)) or "none"
                raise ValueError(
                    f"profile model {resolved.model_id} lacks role={role} "
                    f"capabilities={capability_text}"
                )
        elif self.tier:
            resolved = registry.resolve_tier(
                self.tier,
                role=role,
                required_capabilities=required,
                backends=backends,
                allow_explicit_only=allow_explicit_only,
            )
        else:
            raise ValueError(f"role assignment for {role} has neither model nor tier")
        if required_tier is not None and resolved.tier != required_tier:
            raise ValueError(
                f"profile model {resolved.model_id} must use tier={required_tier} "
                f"for role={role}"
            )
        return resolved


class OrchestrationProfiles:
    def __init__(self, profiles: dict[str, dict[str, RoleAssignment]], source: str) -> None:
        self.profiles = profiles
        self.source = source
        if set(profiles) != set(REQUIRED_ROLES):
            raise ValueError("orchestration profiles must define variants A-F exactly")
        for variant, required in REQUIRED_ROLES.items():
            actual = set(profiles[variant])
            if actual != required:
                raise ValueError(
                    f"variant {variant} roles must be {sorted(required)}; found {sorted(actual)}"
                )

    def assignment(self, variant: str, role: str) -> RoleAssignment:
        try:
            return self.profiles[variant][role]
        except KeyError as exc:
            raise ValueError(f"variant {variant} has no assignment for role {role}") from exc


def assignment_from_dict(role: str, payload: dict[str, Any]) -> RoleAssignment:
    if role not in ROLES:
        raise ValueError(f"unknown orchestration role: {role}")
    tier = payload.get("tier")
    model = payload.get("model")
    effort = payload.get("effort")
    if (tier is None) == (model is None):
        raise ValueError(f"role {role} must define exactly one of tier or model")
    if tier is not None and tier not in TIERS:
        raise ValueError(f"role {role} has invalid tier")
    if model is not None and not isinstance(model, str):
        raise ValueError(f"role {role} model must be a string")
    if effort not in EFFORTS:
        raise ValueError(f"role {role} has invalid effort")
    return RoleAssignment(tier=tier, model=model, effort=effort)


def profiles_from_dict(payload: dict[str, Any], source: str = "memory") -> OrchestrationProfiles:
    if payload.get("schemaVersion") != PROFILE_SCHEMA_VERSION:
        raise ValueError("unsupported orchestration profile schemaVersion")
    raw_profiles = payload.get("profiles")
    if not isinstance(raw_profiles, dict):
        raise ValueError("orchestration profiles must contain an object")
    profiles: dict[str, dict[str, RoleAssignment]] = {}
    for variant, raw_roles in raw_profiles.items():
        if not isinstance(raw_roles, dict):
            raise ValueError(f"variant {variant} roles must be an object")
        profiles[variant] = {
            role: assignment_from_dict(role, assignment)
            for role, assignment in raw_roles.items()
            if isinstance(assignment, dict)
        }
        if len(profiles[variant]) != len(raw_roles):
            raise ValueError(f"variant {variant} contains an invalid role assignment")
    return OrchestrationProfiles(profiles, source)


def load_orchestration_profiles(path: Path | None = None) -> OrchestrationProfiles:
    profiles_path = path or DEFAULT_PROFILES_PATH
    payload = json.loads(profiles_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("orchestration profiles must contain an object")
    return profiles_from_dict(payload, str(profiles_path))
