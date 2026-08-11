from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import re
import secrets
from collections.abc import Mapping
from typing import Any


SCHEMA = "agent-auto-router.evaluation-run.v1"
ARTIFACTS_ENV = "AGENT_AUTO_ROUTER_EVALUATIONS_DIR"
_SAFE_KIND = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def default_evaluations_root(
    environ: Mapping[str, str] | None = None,
    *,
    home: pathlib.Path | None = None,
) -> pathlib.Path:
    env = os.environ if environ is None else environ
    configured = env.get(ARTIFACTS_ENV)
    if configured:
        return pathlib.Path(configured).expanduser().resolve()

    local_app_data = env.get("LOCALAPPDATA")
    if local_app_data:
        return (
            pathlib.Path(local_app_data)
            / "agent-auto-router"
            / "evaluations"
        ).resolve()

    xdg_state_home = env.get("XDG_STATE_HOME")
    if xdg_state_home:
        return (pathlib.Path(xdg_state_home) / "agent-auto-router" / "evaluations").resolve()

    resolved_home = pathlib.Path.home() if home is None else home
    return (resolved_home / ".local" / "state" / "agent-auto-router" / "evaluations").resolve()


def create_run_directory(
    kind: str,
    *,
    root: pathlib.Path | None = None,
    timestamp: str | None = None,
    nonce: str | None = None,
) -> tuple[str, pathlib.Path]:
    if not _SAFE_KIND.fullmatch(kind):
        raise ValueError("Evaluation kind must use lowercase letters, digits, and hyphens")
    created_at = timestamp or dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = nonce or secrets.token_hex(4)
    run_id = f"{created_at}-{suffix}"
    evaluations_root = (root or default_evaluations_root()).expanduser().resolve()
    run_directory = evaluations_root / kind / run_id
    run_directory.mkdir(parents=True, exist_ok=False)
    return run_id, run_directory


def prepare_explicit_run_directory(path: pathlib.Path) -> tuple[str, pathlib.Path]:
    run_directory = path.expanduser().resolve()
    run_directory.mkdir(parents=True, exist_ok=True)
    if any(run_directory.iterdir()):
        raise FileExistsError(
            f"Explicit results directory must be empty: {run_directory}"
        )
    return run_directory.name, run_directory


def write_manifest(run_directory: pathlib.Path, payload: Mapping[str, Any]) -> pathlib.Path:
    manifest = {
        "schema": SCHEMA,
        **payload,
    }
    target = run_directory / "manifest.json"
    temporary = run_directory / ".manifest.json.tmp"
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, target)
    return target
