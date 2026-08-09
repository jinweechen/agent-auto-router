from __future__ import annotations

import copy
import pathlib
import sys
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "skills" / "agent-auto-router" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from benchmark_priors import (  # noqa: E402
    benchmark_priors_digest,
    benchmark_priors_from_dict,
    load_benchmark_priors,
)
from model_registry import load_model_registry  # noqa: E402


class BenchmarkPriorsTests(unittest.TestCase):
    def test_default_snapshot_is_offline_versioned_and_registry_scoped(self) -> None:
        registry = load_model_registry()
        priors = load_benchmark_priors(registry=registry)
        self.assertFalse(priors.runtime_network_access)
        self.assertEqual(priors.as_of, "2026-08-09")
        self.assertEqual(len(benchmark_priors_digest(priors)), 64)
        self.assertEqual(
            set(priors.model_evidence),
            {
                "codex:gpt-5.6-sol",
                "codex:gpt-5.6-terra",
                "codex:gpt-5.6-luna",
            },
        )
        self.assertFalse(any(model.startswith("claude:") for model in priors.model_evidence))

    def test_runtime_network_access_is_rejected(self) -> None:
        priors = load_benchmark_priors()
        payload = copy.deepcopy(priors.raw_payload)
        payload["runtimeNetworkAccess"] = True
        with self.assertRaisesRegex(ValueError, "disable runtime network"):
            benchmark_priors_from_dict(payload)

    def test_unknown_model_evidence_is_rejected(self) -> None:
        registry = load_model_registry()
        priors = load_benchmark_priors()
        payload = copy.deepcopy(priors.raw_payload)
        payload["modelEvidence"]["claude:unversioned"] = copy.deepcopy(
            payload["modelEvidence"]["codex:gpt-5.6-luna"]
        )
        with self.assertRaisesRegex(ValueError, "unknown model"):
            benchmark_priors_from_dict(payload, registry=registry)

    def test_guidance_cannot_reference_missing_metric(self) -> None:
        priors = load_benchmark_priors()
        payload = copy.deepcopy(priors.raw_payload)
        payload["routingGuidance"]["longContext"]["evidenceMetrics"] = ["invented"]
        with self.assertRaisesRegex(ValueError, "unavailable metrics"):
            benchmark_priors_from_dict(payload)


if __name__ == "__main__":
    unittest.main()
