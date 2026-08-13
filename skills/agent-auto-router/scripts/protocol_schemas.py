#!/usr/bin/env python3
"""Stable, unversioned schema identifiers for router-owned runtime protocols."""

ROUTE_DECISION_SCHEMA = "agent-auto-router.route-decision"
TASK_BINDING_SCHEMA = "agent-auto-router.task-binding"
EXECUTION_ENVELOPE_SCHEMA = "agent-auto-router.execution-envelope"
HOST_REQUEST_SCHEMA = "agent-auto-router.host-request"
HOST_PLAN_SCHEMA = "agent-auto-router.host-plan"
HOST_PERMISSIONS_SCHEMA = "agent-auto-router.host-permissions"
DESKTOP_SPAWN_CAPABILITIES_SCHEMA = "agent-auto-router.desktop-spawn-capabilities"
DESKTOP_PLAN_SCHEMA = "agent-auto-router.desktop-plan"
EXECUTION_REPORT_SCHEMA = "agent-auto-router.execution-report"
RUNNER_INPUT_SCHEMA = "agent-auto-router.runner-input"
MODEL_AFFINITY_SCHEMA = "agent-auto-router.model-affinity"
QUICK_PROFILES_SCHEMA = "agent-auto-router.quick-profiles"
DOCTOR_SCHEMA = "agent-auto-router.doctor"
POLICY_SHADOW_SCHEMA = "agent-auto-router.policy-shadow"
WORKSPACE_SNAPSHOT_SCHEMA = "agent-auto-router.workspace-snapshot"
WORKSPACE_COMPARISON_SCHEMA = "agent-auto-router.workspace-comparison"

RUNTIME_PROTOCOL_SCHEMAS = frozenset({
    ROUTE_DECISION_SCHEMA,
    TASK_BINDING_SCHEMA,
    EXECUTION_ENVELOPE_SCHEMA,
    HOST_REQUEST_SCHEMA,
    HOST_PLAN_SCHEMA,
    HOST_PERMISSIONS_SCHEMA,
    DESKTOP_SPAWN_CAPABILITIES_SCHEMA,
    DESKTOP_PLAN_SCHEMA,
    EXECUTION_REPORT_SCHEMA,
    RUNNER_INPUT_SCHEMA,
    MODEL_AFFINITY_SCHEMA,
    QUICK_PROFILES_SCHEMA,
    DOCTOR_SCHEMA,
    POLICY_SHADOW_SCHEMA,
    WORKSPACE_SNAPSHOT_SCHEMA,
    WORKSPACE_COMPARISON_SCHEMA,
})
