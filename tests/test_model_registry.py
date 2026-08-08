from __future__ import annotations

import json
import pathlib
import sys
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "skills" / "agent-auto-router" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from model_registry import (  # noqa: E402
    DEFAULT_REGISTRY_PATH,
    load_model_registry,
    registry_from_dict,
)
from orchestration_profiles import (  # noqa: E402
    load_orchestration_profiles,
    profiles_from_dict,
)
from routing_policy import select_model  # noqa: E402
from validate_model_registry import validate_registry_and_profiles  # noqa: E402


def registry_payload() -> dict[str, object]:
    return json.loads(DEFAULT_REGISTRY_PATH.read_text(encoding="utf-8"))


def additional_fast_model() -> dict[str, object]:
    return {
        "id": "gpt-example-fast",
        "aliases": ["example-fast"],
        "tier": "fast",
        "priority": 5,
        "qualityRank": 1,
        "costRank": 1,
        "latencyRank": 1,
        "defaultEffort": "low",
        "capabilities": ["coding", "bounded-execution"],
        "allowedRoles": ["direct", "worker"],
        "enabled": True,
        "autoEligible": True,
    }


class ModelRegistryTests(unittest.TestCase):
    def test_default_registry_preserves_existing_aliases_and_tiers(self) -> None:
        registry = load_model_registry()
        self.assertEqual(registry.get("sol").model_id, "gpt-5.6-sol")
        self.assertEqual(registry.get("terra").tier, "balanced")
        self.assertEqual(registry.get("luna").tier, "fast")

    def test_new_enabled_model_can_win_tier_resolution_without_policy_code_change(self) -> None:
        payload = registry_payload()
        payload["models"].append(additional_fast_model())
        registry = registry_from_dict(payload, "unit-test")
        decision = select_model("Rename this field", "balance", registry=registry)
        self.assertEqual(decision.target_tier, "fast")
        self.assertEqual(decision.model, "gpt-example-fast")

    def test_disabled_model_cannot_be_selected_explicitly(self) -> None:
        payload = registry_payload()
        model = additional_fast_model()
        model["enabled"] = False
        model["autoEligible"] = False
        payload["models"].append(model)
        registry = registry_from_dict(payload, "unit-test")
        with self.assertRaisesRegex(ValueError, "not enabled"):
            registry.get("example-fast", role="direct")

    def test_explicit_only_model_does_not_enter_auto_resolution(self) -> None:
        payload = registry_payload()
        model = additional_fast_model()
        model["autoEligible"] = False
        payload["models"].append(model)
        registry = registry_from_dict(payload, "unit-test")
        self.assertEqual(registry.get("example-fast").model_id, "gpt-example-fast")
        self.assertEqual(
            select_model("Rename this field", "balance", registry=registry).model,
            "gpt-5.6-luna",
        )

    def test_registry_rejects_alias_collision(self) -> None:
        payload = registry_payload()
        model = additional_fast_model()
        model["aliases"] = ["luna"]
        payload["models"].append(model)
        with self.assertRaisesRegex(ValueError, "duplicate model alias"):
            registry_from_dict(payload, "unit-test")

    def test_registry_requires_high_risk_primary_model(self) -> None:
        payload = registry_payload()
        payload["models"][0]["capabilities"].remove("high-risk-primary")
        with self.assertRaisesRegex(ValueError, "high-risk-primary"):
            registry_from_dict(payload, "unit-test")

    def test_profiles_resolve_existing_roles_through_registry(self) -> None:
        registry = load_model_registry()
        profiles = load_orchestration_profiles()
        planner = profiles.assignment("B", "planner").resolve(registry, "planner")
        worker = profiles.assignment("B", "worker").resolve(registry, "worker")
        self.assertEqual(planner.model_id, "gpt-5.6-sol")
        self.assertEqual(worker.model_id, "gpt-5.6-luna")

    def test_profile_tier_automatically_uses_new_higher_priority_model(self) -> None:
        payload = registry_payload()
        payload["models"].append(additional_fast_model())
        registry = registry_from_dict(payload, "unit-test")
        worker = load_orchestration_profiles().assignment("B", "worker").resolve(
            registry, "worker"
        )
        self.assertEqual(worker.model_id, "gpt-example-fast")

    def test_validator_resolves_every_profile_without_model_calls(self) -> None:
        report = validate_registry_and_profiles(
            load_model_registry(), load_orchestration_profiles()
        )
        self.assertTrue(report["valid"])
        self.assertEqual(report["modelCalls"], 0)
        self.assertEqual(report["resolvedProfiles"]["C"]["dispatcher"]["tier"], "balanced")
        self.assertEqual(report["highRiskFinalRoles"]["A"]["model"], "gpt-5.6-sol")

    def test_high_risk_profile_resolution_keeps_required_capability(self) -> None:
        payload = registry_payload()
        payload["models"].append({
            "id": "gpt-frontier-lite",
            "aliases": ["frontier-lite"],
            "tier": "frontier",
            "priority": 1,
            "qualityRank": 2,
            "costRank": 2,
            "latencyRank": 1,
            "defaultEffort": "medium",
            "capabilities": ["coding"],
            "allowedRoles": ["direct", "reviewer"],
            "enabled": True,
            "autoEligible": True,
        })
        registry = registry_from_dict(payload, "unit-test")
        assignment = load_orchestration_profiles().assignment("A", "direct")
        resolved = assignment.resolve(
            registry,
            "direct",
            required_capabilities=("high-risk-primary",),
            required_tier="frontier",
        )
        self.assertEqual(resolved.model_id, "gpt-5.6-sol")

    def test_explicit_only_profile_model_is_rejected_from_auto(self) -> None:
        payload = registry_payload()
        model = additional_fast_model()
        model["autoEligible"] = False
        payload["models"].append(model)
        registry = registry_from_dict(payload, "unit-test")
        profile_payload = json.loads(
            (SCRIPTS / "orchestration_profiles.json").read_text(encoding="utf-8")
        )
        profile_payload["profiles"]["F"]["direct"] = {
            "model": "gpt-example-fast",
            "effort": "low",
        }
        profiles = profiles_from_dict(profile_payload, "unit-test")
        with self.assertRaisesRegex(ValueError, "not eligible for Auto"):
            validate_registry_and_profiles(registry, profiles)


if __name__ == "__main__":
    unittest.main()
