#!/usr/bin/env python3
"""Build and validate privacy-safe host execution receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Iterable, Mapping
from typing import Any

from protocol_schemas import DESKTOP_PLAN_SCHEMA, EXECUTION_RECEIPT_SCHEMA


SCHEMA = EXECUTION_RECEIPT_SCHEMA
_TOP_LEVEL_FIELDS = frozenset({
    "schema",
    "receiptId",
    "attemptBindingId",
    "routeId",
    "stageId",
    "instance",
    "attemptId",
    "idempotencyKey",
    "planBinding",
    "identity",
    "terminalEvidence",
    "lifecycle",
    "workspaceEvidence",
    "checks",
    "artifactRefs",
    "completionDecision",
    "acceptanceDecision",
    "privacy",
})
_PLAN_BINDING_FIELDS = frozenset({
    "routeId",
    "stageId",
    "role",
    "requested",
    "resolved",
    "idempotencyKeyTemplate",
    "writer",
    "wouldWrite",
    "minimumInstances",
    "maximumInstances",
    "workspaceEvidencePolicy",
    "digest",
})
_IDENTITY_FIELDS = frozenset({"requested", "resolved", "actual", "decision"})
_REQUESTED_FIELDS = frozenset({"model", "effort"})
_RESOLVED_FIELDS = frozenset({"model", "effort", "modelResolution"})
_ACTUAL_FIELDS = frozenset({"state", "model", "effort", "source"})
_TERMINAL_FIELDS = frozenset({
    "finalStatusNotification",
    "childThreadCompleted",
    "terminalToolResult",
    "finalAnswerReceived",
    "taskCompleteReceived",
    "terminalOutcome",
})
_LIFECYCLE_FIELDS = frozenset({
    "deadlineOutcome",
    "deadlineSequence",
    "terminalSequence",
    "interruptAttempted",
    "interruptGraceExpired",
    "advisoryHostStatus",
})
_WORKSPACE_FIELDS = frozenset({"state", "changedFileCount", "digest", "source"})
_CHECK_FIELDS = frozenset({"name", "status", "required", "evidenceOwner", "artifactRef"})
_ARTIFACT_FIELDS = frozenset({"id", "sha256", "mediaType", "byteLength"})
_COMPLETION_FIELDS = frozenset({
    "outcome",
    "authoritativeTerminal",
    "lateTerminalAfterTimeout",
    "uiReconciliation",
})
_ACCEPTANCE_FIELDS = frozenset({"status", "reason"})
_PRIVACY_FIELDS = frozenset({
    "taskIncluded",
    "agentOutputIncluded",
    "toolOutputIncluded",
    "rawWorkspacePathsIncluded",
})
_TERMINAL_OUTCOMES = frozenset({"succeeded", "failed", "blocked", "cancelled"})
_CHECK_STATUSES = frozenset({"passed", "failed", "not_run"})
_TRUSTED_EVIDENCE_OWNERS = frozenset({
    "host-runtime",
    "deterministic-validator",
    "independent-reviewer",
})
_TRUSTED_WORKSPACE_SOURCES = frozenset({"host-runtime", "deterministic-validator"})
_EVIDENCE_OWNERS = _TRUSTED_EVIDENCE_OWNERS | {"agent-claim"}
_SYMBOLIC_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def build_execution_receipt_contract(*, required_after_attempt: bool) -> dict[str, Any]:
    """Return the closed receipt policy embedded in a Desktop execution plan."""
    return {
        "schema": SCHEMA,
        "producer": "trusted-host-runtime",
        "requiredAfterAttempt": _boolean(
            required_after_attempt, "required_after_attempt"
        ),
        "requestedResolvedActualIdentity": True,
        "actualIdentityPolicy": "observed-or-unresolved-never-inferred",
        "planBindingPolicy": "canonical-agent-binding-digest",
        "attemptBindingPolicy": "stable-plan-attempt-digest",
        "receiptIdPolicy": "complete-content-sha256",
        "eventOrdering": "trusted-monotonic-sequence",
        "writerWorkspaceEvidence": "changed-required",
        "completionAndAcceptanceSeparated": True,
        "selfAuthoredEvidenceCanAccept": False,
        "rawTaskOrOutputAllowed": False,
        "rawWorkspacePathsAllowed": False,
    }


def _closed_mapping(value: Any, fields: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    payload = dict(value)
    if set(payload) != fields:
        missing = sorted(fields - set(payload))
        unknown = sorted(set(payload) - fields)
        raise ValueError(
            f"{label} must use the closed field set; missing={missing}, unknown={unknown}"
        )
    return payload


def _non_empty_string(value: Any, label: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(
            f"{label} must be a non-empty string of at most {maximum} characters"
        )
    if any(character in value for character in ("\r", "\n", "\x00")):
        raise ValueError(f"{label} cannot contain control line breaks")
    return value


def _symbolic_name(value: Any, label: str) -> str:
    text = _non_empty_string(value, label, maximum=64)
    if _SYMBOLIC_NAME.fullmatch(text) is None:
        raise ValueError(f"{label} must be a symbolic identifier")
    return text


def _opaque_id(value: Any, label: str, *, maximum: int = 256) -> str:
    text = _non_empty_string(value, label, maximum=maximum)
    if _OPAQUE_ID.fullmatch(text) is None:
        raise ValueError(f"{label} must be an opaque identifier")
    return text


def _positive_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _optional_sequence(value: Any, label: str) -> int | None:
    if value is None:
        return None
    return _positive_integer(value, label)


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be boolean")
    return value


def _canonical_digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _attempt_binding_id(
    plan_binding_digest: str,
    instance: int,
    attempt_id: str,
    idempotency_key: str,
) -> str:
    return hashlib.sha256(
        f"{plan_binding_digest}\0{instance}\0{attempt_id}\0{idempotency_key}".encode(
            "utf-8"
        )
    ).hexdigest()


def _receipt_id(receipt: Mapping[str, Any]) -> str:
    content = dict(receipt)
    content.pop("receiptId", None)
    return _canonical_digest(content)


def _validate_requested(value: Any, label: str) -> dict[str, Any]:
    requested = _closed_mapping(value, _REQUESTED_FIELDS, label)
    _opaque_id(requested["model"], f"{label}.model")
    _symbolic_name(requested["effort"], f"{label}.effort")
    return requested


def _validate_resolved(value: Any, label: str) -> dict[str, Any]:
    resolved = _closed_mapping(value, _RESOLVED_FIELDS, label)
    _opaque_id(resolved["model"], f"{label}.model")
    _symbolic_name(resolved["effort"], f"{label}.effort")
    _symbolic_name(resolved["modelResolution"], f"{label}.modelResolution")
    return resolved


def build_receipt_plan_binding(
    route_id: str,
    agent: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the immutable receipt subset declared by one Desktop agent template."""
    route_id = _opaque_id(route_id, "routeId")
    stage_id = _symbolic_name(agent.get("id"), "agent.id")
    role = _symbolic_name(agent.get("role"), "agent.role")
    if role != stage_id:
        raise ValueError("agent.role must match agent.id")
    receipt_identity = agent.get("receiptIdentity")
    if not isinstance(receipt_identity, Mapping):
        raise ValueError("agent.receiptIdentity must be an object")
    if set(receipt_identity) != {"requested", "resolved", "actualPolicy"}:
        raise ValueError("agent.receiptIdentity must use the closed field set")
    if receipt_identity.get("actualPolicy") != "trusted-host-observed-or-unresolved":
        raise ValueError("agent.receiptIdentity.actualPolicy is invalid")
    requested = _validate_requested(
        receipt_identity.get("requested"), "agent.receiptIdentity.requested"
    )
    resolved = _validate_resolved(
        receipt_identity.get("resolved"), "agent.receiptIdentity.resolved"
    )
    if requested.get("model") != agent.get("preferredModel"):
        raise ValueError("agent requested model does not match preferredModel")
    if resolved != {
        "model": agent.get("model"),
        "effort": agent.get("reasoningEffort"),
        "modelResolution": agent.get("modelResolution"),
    }:
        raise ValueError("agent resolved identity does not match execution fields")
    if requested.get("effort") != resolved.get("effort"):
        raise ValueError("requested and resolved effort must match")

    template = _non_empty_string(
        agent.get("idempotencyKeyTemplate"), "agent.idempotencyKeyTemplate"
    )
    expected_template = f"{route_id}:{stage_id}:{{instance}}"
    if template != expected_template:
        raise ValueError("agent idempotency key template does not match route and stage")
    writer = _boolean(agent.get("writer"), "agent.writer")
    would_write = _boolean(agent.get("wouldWrite"), "agent.wouldWrite")
    minimum_instances = _positive_integer(
        agent.get("minimumInstances"), "agent.minimumInstances"
    )
    maximum_instances = _positive_integer(
        agent.get("maximumInstances"), "agent.maximumInstances"
    )
    if minimum_instances > maximum_instances:
        raise ValueError("agent instance bounds are invalid")
    body = {
        "routeId": route_id,
        "stageId": stage_id,
        "role": role,
        "requested": requested,
        "resolved": resolved,
        "idempotencyKeyTemplate": template,
        "writer": writer,
        "wouldWrite": would_write,
        "minimumInstances": minimum_instances,
        "maximumInstances": maximum_instances,
        "workspaceEvidencePolicy": "changed-required" if would_write else "not-required",
    }
    return {**body, "digest": _canonical_digest(body)}


def _validate_plan_binding(value: Any) -> dict[str, Any]:
    binding = _closed_mapping(value, _PLAN_BINDING_FIELDS, "planBinding")
    route_id = _opaque_id(binding["routeId"], "planBinding.routeId")
    stage_id = _symbolic_name(binding["stageId"], "planBinding.stageId")
    role = _symbolic_name(binding["role"], "planBinding.role")
    if role != stage_id:
        raise ValueError("planBinding.role must match stageId")
    requested = _validate_requested(binding["requested"], "planBinding.requested")
    resolved = _validate_resolved(binding["resolved"], "planBinding.resolved")
    if requested["effort"] != resolved["effort"]:
        raise ValueError("planBinding requested and resolved effort must match")
    expected_template = f"{route_id}:{stage_id}:{{instance}}"
    if binding["idempotencyKeyTemplate"] != expected_template:
        raise ValueError("planBinding idempotency template does not match route and stage")
    writer = _boolean(binding["writer"], "planBinding.writer")
    would_write = _boolean(binding["wouldWrite"], "planBinding.wouldWrite")
    minimum_instances = _positive_integer(
        binding["minimumInstances"], "planBinding.minimumInstances"
    )
    maximum_instances = _positive_integer(
        binding["maximumInstances"], "planBinding.maximumInstances"
    )
    if minimum_instances > maximum_instances:
        raise ValueError("planBinding instance bounds are invalid")
    expected_policy = "changed-required" if would_write else "not-required"
    if binding["workspaceEvidencePolicy"] != expected_policy:
        raise ValueError(f"planBinding.workspaceEvidencePolicy must be {expected_policy}")
    body = {key: binding[key] for key in _PLAN_BINDING_FIELDS if key != "digest"}
    if not isinstance(binding["digest"], str) or binding["digest"] != _canonical_digest(body):
        raise ValueError("planBinding.digest does not bind the declared plan fields")
    return binding


def _agent_binding_from_plan(
    plan: Mapping[str, Any],
    stage_id: str,
    instance: int,
) -> dict[str, Any]:
    if plan.get("schema") != DESKTOP_PLAN_SCHEMA:
        raise ValueError(f"plan schema must be {DESKTOP_PLAN_SCHEMA}")
    if plan.get("status") != "ready" or plan.get("executionRequested") is not True:
        raise ValueError("execution receipts require a ready executable Desktop plan")
    route_id = _opaque_id(plan.get("routeId"), "plan.routeId")
    coordination = plan.get("coordination")
    receipt_contract = (
        coordination.get("executionReceipt") if isinstance(coordination, Mapping) else None
    )
    if not isinstance(receipt_contract, Mapping) or dict(
        receipt_contract
    ) != build_execution_receipt_contract(required_after_attempt=True):
        raise ValueError("plan execution receipt contract is incomplete or changed")

    agents = plan.get("agents")
    if not isinstance(agents, list):
        raise ValueError("plan.agents must be an array")
    matching_agents = [
        item for item in agents if isinstance(item, Mapping) and item.get("id") == stage_id
    ]
    if len(matching_agents) != 1:
        raise ValueError("stage must resolve to exactly one plan agent")
    agent = matching_agents[0]
    stages = plan.get("stages")
    if not isinstance(stages, list):
        raise ValueError("plan.stages must be an array")
    matching_stages = [
        item for item in stages if isinstance(item, Mapping) and item.get("id") == stage_id
    ]
    if len(matching_stages) != 1 or matching_stages[0].get("agent") != stage_id:
        raise ValueError("stage must bind to the matching plan agent")
    stage = matching_stages[0]
    if stage.get("minimumInstances") != agent.get("minimumInstances") or stage.get(
        "maximumInstances"
    ) != agent.get("maximumInstances"):
        raise ValueError("stage and agent instance bounds must match")

    binding = build_receipt_plan_binding(route_id, agent)
    declared_binding = _validate_plan_binding(agent.get("receiptBinding"))
    if declared_binding != binding:
        raise ValueError("agent receipt binding changed after Desktop planning")
    if instance < binding["minimumInstances"] or instance > binding["maximumInstances"]:
        raise ValueError("instance is outside the stage bounds")
    return binding


def _identity_decision(resolved: Mapping[str, Any], actual: Mapping[str, Any]) -> str:
    if actual["state"] == "unresolved":
        return "unresolved"
    if actual["model"] == resolved["model"] and actual["effort"] == resolved["effort"]:
        return "matched"
    return "mismatch"


def _authoritative_terminal(terminal: Mapping[str, Any]) -> bool:
    return any(
        terminal[name]
        for name in (
            "finalStatusNotification",
            "childThreadCompleted",
            "terminalToolResult",
        )
    )


def _completion_decision(
    terminal: Mapping[str, Any], lifecycle: Mapping[str, Any]
) -> dict[str, Any]:
    authoritative = _authoritative_terminal(terminal)
    deadline_sequence = lifecycle["deadlineSequence"]
    terminal_sequence = lifecycle["terminalSequence"]
    terminal_preceded_deadline = (
        authoritative
        and deadline_sequence is not None
        and terminal_sequence < deadline_sequence
    )
    late_terminal = (
        authoritative
        and deadline_sequence is not None
        and deadline_sequence < terminal_sequence
    )
    if lifecycle["deadlineOutcome"] == "timed_out" and not terminal_preceded_deadline:
        outcome = "timed_out"
    elif authoritative:
        outcome = terminal["terminalOutcome"]
    elif lifecycle["interruptAttempted"] and lifecycle["interruptGraceExpired"]:
        outcome = "orphaned"
    else:
        outcome = "incomplete"

    advisory = lifecycle["advisoryHostStatus"]
    if authoritative and advisory in {"processing", "waiting"}:
        ui_reconciliation = "stale_host_ui"
    elif authoritative and advisory == "terminal":
        ui_reconciliation = "consistent"
    else:
        ui_reconciliation = "unknown"
    return {
        "outcome": outcome,
        "authoritativeTerminal": authoritative,
        "lateTerminalAfterTimeout": late_terminal,
        "uiReconciliation": ui_reconciliation,
    }


def _acceptance_decision(
    *,
    completion: Mapping[str, Any],
    identity_decision: str,
    plan_binding: Mapping[str, Any],
    workspace: Mapping[str, Any],
    checks: Iterable[Mapping[str, Any]],
) -> dict[str, str]:
    outcome = completion["outcome"]
    if outcome == "incomplete":
        return {"status": "pending_evidence", "reason": "terminal-evidence-incomplete"}
    if outcome != "succeeded":
        return {"status": "rejected", "reason": f"completion-{outcome}"}
    if identity_decision == "mismatch":
        return {"status": "rejected", "reason": "actual-identity-mismatch"}
    if identity_decision == "unresolved":
        return {"status": "pending_evidence", "reason": "actual-identity-unresolved"}

    if plan_binding["workspaceEvidencePolicy"] == "changed-required":
        if workspace["state"] in {"not_checked", "unknown"}:
            return {
                "status": "pending_evidence",
                "reason": "workspace-evidence-unresolved",
            }
        if workspace["state"] != "changed":
            return {
                "status": "rejected",
                "reason": "required-workspace-change-missing",
            }

    required = [check for check in checks if check["required"]]
    if any(check["status"] == "failed" for check in required):
        return {"status": "rejected", "reason": "required-check-failed"}
    if not required:
        return {"status": "pending_evidence", "reason": "required-check-missing"}
    if any(
        check["status"] != "passed"
        or check["evidenceOwner"] not in _TRUSTED_EVIDENCE_OWNERS
        for check in required
    ):
        return {"status": "pending_evidence", "reason": "required-check-unverified"}
    return {"status": "accepted", "reason": "trusted-evidence-complete"}


def _validate_identity(value: Any, plan_binding: Mapping[str, Any]) -> dict[str, Any]:
    identity = _closed_mapping(value, _IDENTITY_FIELDS, "identity")
    requested = _validate_requested(identity["requested"], "identity.requested")
    resolved = _validate_resolved(identity["resolved"], "identity.resolved")
    if requested != plan_binding["requested"] or resolved != plan_binding["resolved"]:
        raise ValueError("receipt identity does not match planBinding")
    actual = _closed_mapping(identity["actual"], _ACTUAL_FIELDS, "identity.actual")
    if actual["state"] not in {"observed", "unresolved"}:
        raise ValueError("identity.actual.state must be observed or unresolved")
    _symbolic_name(actual["source"], "identity.actual.source")
    if actual["state"] == "observed":
        _opaque_id(actual["model"], "identity.actual.model")
        _symbolic_name(actual["effort"], "identity.actual.effort")
    elif actual["model"] is not None or actual["effort"] is not None:
        raise ValueError("unresolved actual identity must keep model and effort null")
    expected = _identity_decision(resolved, actual)
    if identity["decision"] != expected:
        raise ValueError(f"identity.decision must be {expected}")
    return identity


def _validate_terminal(value: Any) -> dict[str, Any]:
    terminal = _closed_mapping(value, _TERMINAL_FIELDS, "terminalEvidence")
    for name in _TERMINAL_FIELDS - {"terminalOutcome"}:
        _boolean(terminal[name], f"terminalEvidence.{name}")
    authoritative = _authoritative_terminal(terminal)
    if authoritative and terminal["terminalOutcome"] not in _TERMINAL_OUTCOMES:
        raise ValueError("authoritative terminal evidence requires a terminalOutcome")
    if not authoritative and terminal["terminalOutcome"] is not None:
        raise ValueError("terminalOutcome must be null without authoritative terminal evidence")
    return terminal


def _validate_lifecycle(
    value: Any,
    terminal: Mapping[str, Any],
) -> dict[str, Any]:
    lifecycle = _closed_mapping(value, _LIFECYCLE_FIELDS, "lifecycle")
    if lifecycle["deadlineOutcome"] not in {"none", "timed_out"}:
        raise ValueError("lifecycle.deadlineOutcome must be none or timed_out")
    for name in ("interruptAttempted", "interruptGraceExpired"):
        _boolean(lifecycle[name], f"lifecycle.{name}")
    if lifecycle["interruptGraceExpired"] and not lifecycle["interruptAttempted"]:
        raise ValueError("interruptGraceExpired requires interruptAttempted")
    if lifecycle["deadlineOutcome"] == "timed_out" and not lifecycle["interruptAttempted"]:
        raise ValueError("timed_out requires interruptAttempted")
    if lifecycle["advisoryHostStatus"] not in {
        "processing",
        "waiting",
        "terminal",
        "unknown",
    }:
        raise ValueError("lifecycle.advisoryHostStatus is invalid")
    deadline_sequence = _optional_sequence(
        lifecycle["deadlineSequence"], "lifecycle.deadlineSequence"
    )
    terminal_sequence = _optional_sequence(
        lifecycle["terminalSequence"], "lifecycle.terminalSequence"
    )
    if (lifecycle["deadlineOutcome"] == "timed_out") != (deadline_sequence is not None):
        raise ValueError("deadlineSequence must exist exactly when the deadline timed out")
    if _authoritative_terminal(terminal) != (terminal_sequence is not None):
        raise ValueError("terminalSequence must exist exactly with authoritative terminal evidence")
    if deadline_sequence is not None and deadline_sequence == terminal_sequence:
        raise ValueError("deadlineSequence and terminalSequence must be distinct")
    return lifecycle


def _validate_workspace(value: Any) -> dict[str, Any]:
    workspace = _closed_mapping(value, _WORKSPACE_FIELDS, "workspaceEvidence")
    state = workspace["state"]
    if state not in {"not_checked", "unchanged", "changed", "unknown"}:
        raise ValueError("workspaceEvidence.state is invalid")
    source = _symbolic_name(workspace["source"], "workspaceEvidence.source")
    if source not in _TRUSTED_WORKSPACE_SOURCES:
        raise ValueError("workspaceEvidence.source must be trusted host evidence")
    count = workspace["changedFileCount"]
    digest = workspace["digest"]
    if state == "unchanged":
        if count != 0:
            raise ValueError("unchanged workspace evidence requires changedFileCount=0")
    elif state == "changed":
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError("changed workspace evidence requires a positive changedFileCount")
    elif count is not None:
        raise ValueError("unchecked or unknown workspace evidence requires changedFileCount=null")
    if digest is not None and (
        not isinstance(digest, str) or _SHA256.fullmatch(digest) is None
    ):
        raise ValueError("workspaceEvidence.digest must be a lowercase SHA-256 or null")
    if state in {"unchanged", "changed"} and digest is None:
        raise ValueError("observed workspace evidence requires a content digest")
    return workspace


def _validate_artifacts(values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        raise ValueError("artifactRefs must be an array")
    artifacts: list[dict[str, Any]] = []
    ids: set[str] = set()
    for index, value in enumerate(values):
        artifact = _closed_mapping(value, _ARTIFACT_FIELDS, f"artifactRefs[{index}]")
        artifact_id = _symbolic_name(artifact["id"], f"artifactRefs[{index}].id")
        if artifact_id in ids:
            raise ValueError(f"duplicate artifact reference: {artifact_id}")
        ids.add(artifact_id)
        if not isinstance(artifact["sha256"], str) or _SHA256.fullmatch(
            artifact["sha256"]
        ) is None:
            raise ValueError(f"artifactRefs[{index}].sha256 must be a lowercase SHA-256")
        _non_empty_string(
            artifact["mediaType"], f"artifactRefs[{index}].mediaType", maximum=128
        )
        size = artifact["byteLength"]
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError(
                f"artifactRefs[{index}].byteLength must be a non-negative integer"
            )
        artifacts.append(artifact)
    return artifacts


def _validate_checks(values: Any, artifact_ids: set[str]) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        raise ValueError("checks must be an array")
    checks: list[dict[str, Any]] = []
    names: set[str] = set()
    for index, value in enumerate(values):
        check = _closed_mapping(value, _CHECK_FIELDS, f"checks[{index}]")
        name = _symbolic_name(check["name"], f"checks[{index}].name")
        if name in names:
            raise ValueError(f"duplicate check name: {name}")
        names.add(name)
        if check["status"] not in _CHECK_STATUSES:
            raise ValueError(f"checks[{index}].status is invalid")
        _boolean(check["required"], f"checks[{index}].required")
        if check["evidenceOwner"] not in _EVIDENCE_OWNERS:
            raise ValueError(f"checks[{index}].evidenceOwner is invalid")
        artifact_ref = check["artifactRef"]
        if artifact_ref is not None and artifact_ref not in artifact_ids:
            raise ValueError(f"checks[{index}].artifactRef does not resolve")
        checks.append(check)
    return checks


def validate_execution_receipt(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a closed receipt and all plan/host-derived decisions."""
    receipt = _closed_mapping(value, _TOP_LEVEL_FIELDS, "execution receipt")
    if receipt["schema"] != SCHEMA:
        raise ValueError(f"execution receipt schema must be {SCHEMA}")
    if not isinstance(receipt["receiptId"], str) or _SHA256.fullmatch(
        receipt["receiptId"]
    ) is None:
        raise ValueError("receiptId must be a lowercase SHA-256")
    if not isinstance(receipt["attemptBindingId"], str) or _SHA256.fullmatch(
        receipt["attemptBindingId"]
    ) is None:
        raise ValueError("attemptBindingId must be a lowercase SHA-256")

    plan_binding = _validate_plan_binding(receipt["planBinding"])
    route_id = _opaque_id(receipt["routeId"], "routeId")
    stage_id = _symbolic_name(receipt["stageId"], "stageId")
    instance = _positive_integer(receipt["instance"], "instance")
    attempt_id = _opaque_id(receipt["attemptId"], "attemptId", maximum=128)
    idempotency_key = _opaque_id(receipt["idempotencyKey"], "idempotencyKey")
    if route_id != plan_binding["routeId"] or stage_id != plan_binding["stageId"]:
        raise ValueError("receipt route and stage do not match planBinding")
    if instance < plan_binding["minimumInstances"] or instance > plan_binding[
        "maximumInstances"
    ]:
        raise ValueError("receipt instance is outside planBinding bounds")
    expected_key = plan_binding["idempotencyKeyTemplate"].replace(
        "{instance}", str(instance)
    )
    if idempotency_key != expected_key:
        raise ValueError("idempotencyKey does not match planBinding")
    expected_attempt_binding = _attempt_binding_id(
        plan_binding["digest"], instance, attempt_id, idempotency_key
    )
    if receipt["attemptBindingId"] != expected_attempt_binding:
        raise ValueError("attemptBindingId does not bind the plan attempt")

    identity = _validate_identity(receipt["identity"], plan_binding)
    terminal = _validate_terminal(receipt["terminalEvidence"])
    lifecycle = _validate_lifecycle(receipt["lifecycle"], terminal)
    workspace = _validate_workspace(receipt["workspaceEvidence"])
    artifacts = _validate_artifacts(receipt["artifactRefs"])
    checks = _validate_checks(
        receipt["checks"], {artifact["id"] for artifact in artifacts}
    )

    completion = _closed_mapping(
        receipt["completionDecision"], _COMPLETION_FIELDS, "completionDecision"
    )
    expected_completion = _completion_decision(terminal, lifecycle)
    if completion != expected_completion:
        raise ValueError("completionDecision does not match ordered lifecycle evidence")
    acceptance = _closed_mapping(
        receipt["acceptanceDecision"], _ACCEPTANCE_FIELDS, "acceptanceDecision"
    )
    expected_acceptance = _acceptance_decision(
        completion=completion,
        identity_decision=identity["decision"],
        plan_binding=plan_binding,
        workspace=workspace,
        checks=checks,
    )
    if acceptance != expected_acceptance:
        raise ValueError("acceptanceDecision does not match receipt evidence")
    privacy = _closed_mapping(receipt["privacy"], _PRIVACY_FIELDS, "privacy")
    if any(privacy.values()) or not all(
        isinstance(item, bool) for item in privacy.values()
    ):
        raise ValueError("execution receipt privacy flags must all be false")
    if receipt["receiptId"] != _receipt_id(receipt):
        raise ValueError("receiptId does not bind the complete receipt content")
    return receipt


def build_execution_receipt(
    plan: Mapping[str, Any],
    *,
    stage_id: str,
    instance: int,
    attempt_id: str,
    actual: Mapping[str, Any],
    terminal_evidence: Mapping[str, Any],
    lifecycle: Mapping[str, Any],
    workspace_evidence: Mapping[str, Any] | None = None,
    checks: Iterable[Mapping[str, Any]] = (),
    artifact_refs: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Reconcile one stage attempt against its canonical executable Desktop plan."""
    stage_id = _symbolic_name(stage_id, "stageId")
    instance = _positive_integer(instance, "instance")
    plan_binding = _agent_binding_from_plan(plan, stage_id, instance)
    route_id = plan_binding["routeId"]
    attempt_id = _opaque_id(attempt_id, "attemptId", maximum=128)
    idempotency_key = plan_binding["idempotencyKeyTemplate"].replace(
        "{instance}", str(instance)
    )

    actual_payload = _closed_mapping(actual, _ACTUAL_FIELDS, "actual")
    identity = {
        "requested": plan_binding["requested"],
        "resolved": plan_binding["resolved"],
        "actual": actual_payload,
        "decision": _identity_decision(plan_binding["resolved"], actual_payload),
    }
    terminal = _validate_terminal(terminal_evidence)
    lifecycle_payload = _validate_lifecycle(lifecycle, terminal)
    workspace = (
        dict(workspace_evidence)
        if workspace_evidence is not None
        else {
            "state": "not_checked",
            "changedFileCount": None,
            "digest": None,
            "source": "host-runtime",
        }
    )
    workspace = _validate_workspace(workspace)
    artifacts = _validate_artifacts([dict(item) for item in artifact_refs])
    checks_payload = _validate_checks(
        [dict(item) for item in checks],
        {artifact["id"] for artifact in artifacts},
    )
    completion = _completion_decision(terminal, lifecycle_payload)
    acceptance = _acceptance_decision(
        completion=completion,
        identity_decision=identity["decision"],
        plan_binding=plan_binding,
        workspace=workspace,
        checks=checks_payload,
    )
    receipt = {
        "schema": SCHEMA,
        "receiptId": "",
        "attemptBindingId": _attempt_binding_id(
            plan_binding["digest"], instance, attempt_id, idempotency_key
        ),
        "routeId": route_id,
        "stageId": stage_id,
        "instance": instance,
        "attemptId": attempt_id,
        "idempotencyKey": idempotency_key,
        "planBinding": plan_binding,
        "identity": identity,
        "terminalEvidence": terminal,
        "lifecycle": lifecycle_payload,
        "workspaceEvidence": workspace,
        "checks": checks_payload,
        "artifactRefs": artifacts,
        "completionDecision": completion,
        "acceptanceDecision": acceptance,
        "privacy": {
            "taskIncluded": False,
            "agentOutputIncluded": False,
            "toolOutputIncluded": False,
            "rawWorkspacePathsIncluded": False,
        },
    }
    receipt["receiptId"] = _receipt_id(receipt)
    return validate_execution_receipt(receipt)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--validate",
        action="store_true",
        help="validate a receipt instead of building one",
    )
    args = parser.parse_args()
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("input must be a JSON object")
        if args.validate:
            receipt = validate_execution_receipt(payload)
        else:
            receipt = build_execution_receipt(
                payload["plan"],
                stage_id=payload["stageId"],
                instance=payload["instance"],
                attempt_id=payload["attemptId"],
                actual=payload["actual"],
                terminal_evidence=payload["terminalEvidence"],
                lifecycle=payload["lifecycle"],
                workspace_evidence=payload.get("workspaceEvidence"),
                checks=payload.get("checks", []),
                artifact_refs=payload.get("artifactRefs", []),
            )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(json.dumps(receipt, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
