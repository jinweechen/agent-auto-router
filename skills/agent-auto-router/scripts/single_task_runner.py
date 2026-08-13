#!/usr/bin/env python3
"""Run one signed-in Codex task and emit privacy-safe execution metrics."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
from typing import Any

from codex_cli_adapter import (
    environment_for_codex_command,
    extract_thread_id,
    extract_usage_details,
    resolve_codex_command,
)
from host_permissions import cli_permission_issue, parse_host_permissions, workdir_is_writable
from model_registry import strip_backend_prefix
from protocol_schemas import RUNNER_INPUT_SCHEMA
from repository_context import build_repository_context


def parse_json_lines(lines: list[str]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def usage_is_available(events: list[dict[str, Any]]) -> bool:
    return any(isinstance(event.get("usage"), dict) for event in events)


def parse_runner_input(
    raw_input: str, input_format: str
) -> tuple[str, str | None, dict[str, Any] | None]:
    if input_format == "task":
        return raw_input, None, None
    try:
        payload = json.loads(raw_input)
    except json.JSONDecodeError as exc:
        raise ValueError(f"runner envelope must be valid JSON: {exc.msg}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != RUNNER_INPUT_SCHEMA:
        raise ValueError(f"runner envelope schema must be {RUNNER_INPUT_SCHEMA}")
    task = payload.get("task")
    repository_context = payload.get("repositoryContext")
    repository_metadata = payload.get("repositoryMetadata")
    if not isinstance(task, str):
        raise ValueError("runner envelope task must be a string")
    if repository_context is not None and not isinstance(repository_context, str):
        raise ValueError("runner envelope repositoryContext must be a string or null")
    if repository_metadata is not None and not isinstance(repository_metadata, dict):
        raise ValueError("runner envelope repositoryMetadata must be an object or null")
    return task, repository_context, repository_metadata


def write_result(path: pathlib.Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort", required=True)
    parser.add_argument(
        "--sandbox",
        choices=("inherit", "read-only", "workspace-write", "danger-full-access"),
        required=True,
    )
    parser.add_argument("--host-permissions-json")
    parser.add_argument("--context-mode", choices=("lean", "full"), default="lean")
    parser.add_argument("--workdir", type=pathlib.Path, required=True)
    parser.add_argument("--result-file", type=pathlib.Path, required=True)
    parser.add_argument(
        "--input-format", choices=("task", "route-envelope"), default="task"
    )
    parser.add_argument("--emit-json", action="store_true")
    parser.add_argument("--repo-map-tokens", type=int, default=0)
    parser.add_argument("--max-candidate-files", type=int, default=0)
    args = parser.parse_args()

    try:
        task, precomputed_context, precomputed_metadata = parse_runner_input(
            sys.stdin.read(), args.input_format
        )
    except ValueError as exc:
        parser.error(str(exc))
    if not task.strip():
        parser.error("task stdin must not be empty")
    workdir = args.workdir.resolve()
    if not workdir.is_dir():
        parser.error(f"workdir not found: {workdir}")
    permissions = None
    if args.host_permissions_json:
        try:
            permissions = parse_host_permissions(args.host_permissions_json)
            args.sandbox = permissions.effective_sandbox(args.sandbox)
        except ValueError as exc:
            parser.error(str(exc))
        if args.sandbox == "external-sandbox":
            parser.error("Codex CLI cannot directly represent external-sandbox inheritance")
        issue = cli_permission_issue(permissions, args.sandbox)
        if issue:
            parser.error(issue)
        if args.sandbox != "read-only" and not workdir_is_writable(workdir, permissions):
            parser.error("workdir is outside the writable roots declared by the host")
    elif args.sandbox == "inherit":
        parser.error("--sandbox inherit requires --host-permissions-json")
    try:
        execution_model = strip_backend_prefix(args.model, "codex")
        codex_command = resolve_codex_command()
    except (RuntimeError, ValueError) as exc:
        parser.error(str(exc))

    repository_metadata: dict[str, Any] | None = None
    effective_task = task
    if precomputed_metadata is not None:
        repository_metadata = precomputed_metadata
        if precomputed_context and repository_metadata.get("context_useful"):
            effective_task = f"{precomputed_context}\n\nUSER TASK:\n{task}"
    elif args.repo_map_tokens > 0 and args.max_candidate_files > 0:
        repository_context, repository_metadata = build_repository_context(
            workdir,
            task,
            max_candidate_files=args.max_candidate_files,
            repo_map_tokens=args.repo_map_tokens,
        )
        if repository_metadata["context_useful"]:
            effective_task = f"{repository_context}\n\nUSER TASK:\n{task}"

    with tempfile.TemporaryDirectory(prefix="codex-auto-single-") as temp_dir:
        output_path = pathlib.Path(temp_dir) / "last-message.txt"
        command = [*codex_command, "exec", "--ephemeral"]
        if args.context_mode == "lean" and args.sandbox == "read-only":
            command.append("--ignore-user-config")
        if permissions is not None:
            command.extend(["-c", f"approval_policy='{permissions.codex_approval_policy}'"])
            if args.sandbox == "workspace-write":
                network = "true" if permissions.network_access is True else "false"
                command.extend(["-c", f"sandbox_workspace_write.network_access={network}"])
                for root in permissions.writable_roots:
                    command.extend(["--add-dir", root])
        command.extend([
            "--skip-git-repo-check",
            "--sandbox",
            args.sandbox,
            "-C",
            str(workdir),
            "-m",
            execution_model,
            "-c",
            f"model_reasoning_effort='{args.effort}'",
            "--json",
            "--output-last-message",
            str(output_path),
            "-",
        ])
        environment = environment_for_codex_command(codex_command)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        started = time.perf_counter()
        completed = subprocess.run(
            command,
            input=effective_task,
            stdout=subprocess.PIPE,
            stderr=None,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            env=environment,
        )
        duration_ms = int((time.perf_counter() - started) * 1000)
        raw_lines = completed.stdout.splitlines()
        events = parse_json_lines(raw_lines)
        usage = extract_usage_details(events)
        usage_available = usage_is_available(events)
        observed_tokens = {
            "input": usage["input_tokens"],
            "cached_input": usage["cached_input_tokens"],
            "cache_write": usage["cache_write_input_tokens"],
            "output": usage["output_tokens"],
            "reasoning_output": usage["reasoning_output_tokens"],
            "total": usage["input_tokens"] + usage["output_tokens"],
        }
        result = {
            "schemaVersion": 1,
            "exitCode": completed.returncode,
            "durationMs": duration_ms,
            "responseId": extract_thread_id(events),
            "observedTokens": observed_tokens,
            "usageAvailable": usage_available,
            "modelCalls": 1,
            "repository": repository_metadata,
        }
        write_result(args.result_file, result)

        if args.emit_json:
            if completed.stdout:
                sys.stdout.write(completed.stdout)
                if not completed.stdout.endswith("\n"):
                    sys.stdout.write("\n")
        elif output_path.is_file():
            final_message = output_path.read_text(encoding="utf-8").strip()
            if final_message:
                print(final_message)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
