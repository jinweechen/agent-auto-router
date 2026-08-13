#!/usr/bin/env python3
"""Load the small, trusted profile surface used by the beginner CLI wrapper."""

from __future__ import annotations

import argparse
import json
import pathlib
from dataclasses import asdict, dataclass
from typing import Any


SCHEMA = "agent-auto-router.quick-profiles.v1"
DEFAULT_PATH = pathlib.Path(__file__).with_name("quick_profiles.json")
STRATEGIES = frozenset({"intelligence", "balance", "cost"})
SANDBOXES = frozenset({"read-only", "workspace-write"})
CONTEXT_MODES = frozenset({"lean", "full"})
REPOSITORY_CONTEXT_MODES = frozenset({"auto", "off"})
MODEL_AFFINITY_MODES = frozenset({"auto", "off"})
PROFILE_KEYS = frozenset({
    "description", "strategy", "sandbox", "contextMode",
    "repositoryContextMode", "modelAffinity", "noFeedback",
})


@dataclass(frozen=True)
class QuickProfile:
    name: str
    description: str
    strategy: str
    sandbox: str
    contextMode: str
    repositoryContextMode: str
    modelAffinity: str
    noFeedback: bool


@dataclass(frozen=True)
class QuickProfiles:
    default_profile: str
    profiles: dict[str, QuickProfile]
    source: str


def _non_empty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def load_quick_profiles(path: pathlib.Path | None = None) -> QuickProfiles:
    source = (path or DEFAULT_PATH).resolve(strict=True)
    raw = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
        raise ValueError(f"quick profile schema must be {SCHEMA}")
    profile_payloads = raw.get("profiles")
    if not isinstance(profile_payloads, dict) or not profile_payloads:
        raise ValueError("quick profiles must be a non-empty object")
    profiles: dict[str, QuickProfile] = {}
    for raw_name, payload in profile_payloads.items():
        name = _non_empty_string(raw_name, "profile name")
        if not isinstance(payload, dict) or set(payload) != PROFILE_KEYS:
            raise ValueError(f"quick profile {name} has an invalid field set")
        strategy = _non_empty_string(payload.get("strategy"), f"{name}.strategy")
        sandbox = _non_empty_string(payload.get("sandbox"), f"{name}.sandbox")
        context_mode = _non_empty_string(
            payload.get("contextMode"), f"{name}.contextMode"
        )
        repository_mode = _non_empty_string(
            payload.get("repositoryContextMode"),
            f"{name}.repositoryContextMode",
        )
        model_affinity = _non_empty_string(
            payload.get("modelAffinity"), f"{name}.modelAffinity"
        )
        no_feedback = payload.get("noFeedback")
        if strategy not in STRATEGIES:
            raise ValueError(f"unsupported strategy in quick profile {name}")
        if sandbox not in SANDBOXES:
            raise ValueError(f"unsupported sandbox in quick profile {name}")
        if context_mode not in CONTEXT_MODES:
            raise ValueError(f"unsupported context mode in quick profile {name}")
        if repository_mode not in REPOSITORY_CONTEXT_MODES:
            raise ValueError(f"unsupported repository mode in quick profile {name}")
        if model_affinity not in MODEL_AFFINITY_MODES:
            raise ValueError(f"unsupported model affinity in quick profile {name}")
        if not isinstance(no_feedback, bool):
            raise ValueError(f"{name}.noFeedback must be boolean")
        profiles[name] = QuickProfile(
            name=name,
            description=_non_empty_string(
                payload.get("description"), f"{name}.description"
            ),
            strategy=strategy,
            sandbox=sandbox,
            contextMode=context_mode,
            repositoryContextMode=repository_mode,
            modelAffinity=model_affinity,
            noFeedback=no_feedback,
        )
    default_profile = _non_empty_string(
        raw.get("defaultProfile"), "defaultProfile"
    )
    if default_profile not in profiles:
        raise ValueError("defaultProfile must name a declared quick profile")
    safe = profiles.get("safe")
    standard = profiles.get("standard")
    if (
        safe is None
        or safe.sandbox != "read-only"
        or not safe.noFeedback
        or safe.modelAffinity != "off"
        or safe.repositoryContextMode != "off"
    ):
        raise ValueError(
            "safe profile must be read-only with repository inspection, feedback, "
            "and model affinity disabled"
        )
    if (
        standard is None
        or standard.sandbox != "workspace-write"
        or not standard.noFeedback
        or standard.modelAffinity != "off"
        or standard.repositoryContextMode != "off"
    ):
        raise ValueError(
            "standard profile must be workspace-write with repository inspection, "
            "feedback, and model affinity disabled"
        )
    return QuickProfiles(default_profile, profiles, str(source))


def profile_payload(profiles: QuickProfiles, name: str) -> dict[str, Any]:
    try:
        profile = profiles.profiles[name]
    except KeyError as exc:
        raise ValueError(
            f"unknown quick profile {name}; choose from {sorted(profiles.profiles)}"
        ) from exc
    return {
        "schema": SCHEMA,
        "profile": asdict(profile),
        "defaultProfile": profiles.default_profile,
        "availableProfiles": sorted(profiles.profiles),
        "modelCalls": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default=None)
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    try:
        profiles = load_quick_profiles()
        if args.list:
            result = {
                "schema": SCHEMA,
                "defaultProfile": profiles.default_profile,
                "profiles": {
                    name: asdict(profile)
                    for name, profile in profiles.profiles.items()
                },
                "modelCalls": 0,
            }
        else:
            result = profile_payload(
                profiles, args.profile or profiles.default_profile
            )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
