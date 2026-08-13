from __future__ import annotations

import pathlib
import sys
import unittest
from datetime import datetime, timedelta, timezone

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "skills" / "agent-auto-router" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from model_affinity import (  # noqa: E402
    ROLE_MODEL_POLICY_AFFINITY,
    ROLE_MODEL_POLICY_PROFILE,
    resolve_model_affinity,
    workspace_identity,
)
from model_registry import load_model_registry  # noqa: E402


def event(
    *,
    workspace_key: str,
    model: str,
    recorded_at: datetime,
    cached: int,
    cache_write: int,
    input_tokens: int = 100,
    strategy: str = "balance",
) -> dict[str, object]:
    return {
        "eventType": "route_outcome",
        "recordedAt": recorded_at.isoformat(),
        "workspaceKey": workspace_key,
        "strategy": strategy,
        "selectedModel": model,
        "executionSucceeded": True,
        "explicitOverride": False,
        "observedTokens": {
            "input": input_tokens,
            "cached_input": cached,
            "cache_write": cache_write,
        },
        "selectedModelObservedTokens": {
            "input": input_tokens,
            "cached_input": cached,
            "cache_write": cache_write,
        },
    }


class ModelAffinityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = load_model_registry()
        self.now = datetime(2026, 8, 11, 8, 0, tzinfo=timezone.utc)
        self.workspace_key = workspace_identity(pathlib.Path("D:/private/workspace"))
        assert self.workspace_key is not None

    def resolve(self, events: list[dict[str, object]], selector: str, tier: str):
        return resolve_model_affinity(
            events,
            workspace_key=self.workspace_key,
            strategy="balance",
            selector_model=selector,
            target_tier=tier,
            registry=self.registry,
            available_backends=("codex",),
            mode="auto",
            now=self.now,
        )

    def test_session_mode_reuses_selected_model_within_run_without_evidence(self) -> None:
        result = resolve_model_affinity(
            (),
            workspace_key=self.workspace_key,
            strategy="balance",
            selector_model="codex:gpt-5.6-terra",
            target_tier="balanced",
            registry=self.registry,
            available_backends=("codex",),
            mode="session",
            now=self.now,
        )
        self.assertEqual(result["mode"], "session")
        self.assertEqual(result["selectedModel"], "codex:gpt-5.6-terra")
        self.assertEqual(result["roleModelPolicy"], ROLE_MODEL_POLICY_AFFINITY)
        self.assertEqual(result["reason"], "session-role-reuse")
        self.assertFalse(result["applied"])
        self.assertEqual(result["evidence"]["samples"], 0)

    def test_workspace_identity_never_contains_the_path(self) -> None:
        self.assertRegex(self.workspace_key, r"^[0-9a-f]{64}$")
        self.assertNotIn("private", self.workspace_key)

    def test_cache_signal_can_retain_one_adjacent_stronger_tier(self) -> None:
        result = self.resolve(
            [
                event(
                    workspace_key=self.workspace_key,
                    model="codex:gpt-5.6-sol",
                    recorded_at=self.now - timedelta(minutes=2),
                    cached=20,
                    cache_write=10,
                )
            ],
            "codex:gpt-5.6-terra",
            "balanced",
        )
        self.assertTrue(result["applied"])
        self.assertEqual(result["selectedModel"], "codex:gpt-5.6-sol")
        self.assertTrue(result["retainedStrongerTier"])
        self.assertEqual(result["roleModelPolicy"], ROLE_MODEL_POLICY_AFFINITY)
        self.assertFalse(result["storesWorkspacePath"])
        self.assertEqual(result["modelCalls"], 0)

    def test_stronger_tier_is_rejected_without_cache_evidence(self) -> None:
        result = self.resolve(
            [
                event(
                    workspace_key=self.workspace_key,
                    model="codex:gpt-5.6-sol",
                    recorded_at=self.now - timedelta(minutes=2),
                    cached=0,
                    cache_write=0,
                )
            ],
            "codex:gpt-5.6-terra",
            "balanced",
        )
        self.assertFalse(result["applied"])
        self.assertEqual(result["selectedModel"], "codex:gpt-5.6-terra")
        self.assertEqual(result["reason"], "stronger-model-cache-signal-insufficient")

    def test_aggregate_multi_model_tokens_do_not_support_stronger_affinity(self) -> None:
        prior = event(
            workspace_key=self.workspace_key,
            model="codex:gpt-5.6-sol",
            recorded_at=self.now - timedelta(minutes=2),
            cached=20,
            cache_write=0,
        )
        prior.pop("selectedModelObservedTokens")
        prior["roleModelPolicy"] = ROLE_MODEL_POLICY_PROFILE
        result = self.resolve([prior], "codex:gpt-5.6-terra", "balanced")
        self.assertFalse(result["applied"])
        self.assertEqual(result["reason"], "stronger-model-cache-signal-insufficient")

    def test_weaker_previous_model_never_overrides_current_requirement(self) -> None:
        result = self.resolve(
            [
                event(
                    workspace_key=self.workspace_key,
                    model="codex:gpt-5.6-luna",
                    recorded_at=self.now - timedelta(minutes=2),
                    cached=80,
                    cache_write=10,
                )
            ],
            "codex:gpt-5.6-sol",
            "frontier",
        )
        self.assertFalse(result["applied"])
        self.assertEqual(result["selectedModel"], "codex:gpt-5.6-sol")
        self.assertEqual(result["reason"], "previous-model-weaker-than-current-requirement")

    def test_repeated_low_cache_signal_prefers_profile_role_models(self) -> None:
        events = [
            event(
                workspace_key=self.workspace_key,
                model="codex:gpt-5.6-terra",
                recorded_at=self.now - timedelta(minutes=index + 1),
                cached=0,
                cache_write=0,
            )
            for index in range(3)
        ]
        result = self.resolve(events, "codex:gpt-5.6-terra", "balanced")
        self.assertEqual(result["roleModelPolicy"], ROLE_MODEL_POLICY_PROFILE)
        self.assertEqual(result["evidence"]["samples"], 3)
        self.assertEqual(result["evidence"]["cacheSignalRatio"], 0.0)


if __name__ == "__main__":
    unittest.main()
