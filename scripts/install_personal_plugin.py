#!/usr/bin/env python3
"""Install this repository as a local plugin in Codex's personal marketplace."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from validate_plugin import validate as validate_plugin


PLUGIN_NAME = "agent-auto-router"
MARKETPLACE_ENTRY = {
    "name": PLUGIN_NAME,
    "source": {"source": "local", "path": f"./plugins/{PLUGIN_NAME}"},
    "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
    "category": "Productivity",
}


class InstallError(RuntimeError):
    """A safe, user-actionable installation failure."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--home",
        type=Path,
        default=Path.home(),
        help="Home directory used for ~/plugins and ~/.agents (default: current user home).",
    )
    parser.add_argument(
        "--source-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Repository plugin root (default: parent of this script directory).",
    )
    parser.add_argument(
        "--force-marketplace-entry",
        action="store_true",
        help="Replace a conflicting personal marketplace entry for this plugin.",
    )
    parser.add_argument(
        "--skip-codex-install",
        action="store_true",
        help="Prepare the local package and marketplace without running `codex plugin add`.",
    )
    return parser.parse_args()


def load_marketplace(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "name": "personal",
            "interface": {"displayName": "Personal"},
            "plugins": [],
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstallError(f"cannot read personal marketplace {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise InstallError(f"personal marketplace must contain a JSON object: {path}")
    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        raise InstallError("personal marketplace name must be a non-empty string")
    plugins = payload.get("plugins")
    if not isinstance(plugins, list) or not all(isinstance(item, dict) for item in plugins):
        raise InstallError("personal marketplace plugins must be an array of objects")
    interface = payload.get("interface")
    if interface is not None and not isinstance(interface, dict):
        raise InstallError("personal marketplace interface must be an object")
    return payload


def update_marketplace(
    marketplace: dict[str, Any], *, force: bool
) -> tuple[dict[str, Any], bool]:
    updated = dict(marketplace)
    plugins = list(marketplace["plugins"])
    matching = [index for index, entry in enumerate(plugins) if entry.get("name") == PLUGIN_NAME]
    if len(matching) > 1:
        raise InstallError(f"personal marketplace contains duplicate {PLUGIN_NAME!r} entries")
    if matching:
        index = matching[0]
        if plugins[index] == MARKETPLACE_ENTRY:
            return updated, False
        if not force:
            raise InstallError(
                f"personal marketplace already contains a conflicting {PLUGIN_NAME!r} entry; "
                "inspect it or rerun with --force-marketplace-entry"
            )
        plugins[index] = MARKETPLACE_ENTRY
    else:
        plugins.append(MARKETPLACE_ENTRY)
    updated["plugins"] = plugins
    return updated, True


def copy_distribution(source_root: Path, destination: Path) -> None:
    shutil.copytree(source_root / ".codex-plugin", destination / ".codex-plugin")
    shutil.copytree(
        source_root / "skills" / PLUGIN_NAME,
        destination / "skills" / PLUGIN_NAME,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )


def file_snapshot(root: Path) -> dict[str, bytes]:
    if not root.is_dir():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def ensure_windows_codex_read_access(target: Path) -> bool:
    """Grant the local Codex sandbox group recursive read access when it exists."""
    if os.name != "nt":
        return False
    group = "CodexSandboxUsers"
    try:
        group_check = subprocess.run(
            ["net", "localgroup", group],
            check=False,
            text=True,
            capture_output=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired as exc:
        raise InstallError("timed out while detecting the Codex sandbox group") from exc
    if group_check.returncode != 0:
        return False
    try:
        grant = subprocess.run(
            [
                "icacls",
                str(target),
                "/grant:r",
                f"{group}:(OI)(CI)(RX)",
                "/T",
                "/Q",
            ],
            check=False,
            text=True,
            capture_output=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        raise InstallError(f"timed out while updating plugin permissions: {target}") from exc
    if grant.returncode != 0:
        detail = (grant.stderr or grant.stdout).strip()
        suffix = f": {detail}" if detail else ""
        raise InstallError(f"cannot grant Codex sandbox read access to {target}{suffix}")
    return True


def install_package(
    source_root: Path, home: Path
) -> tuple[Path, Path | None, bool, bool]:
    target = home / "plugins" / PLUGIN_NAME
    if target.exists() and not target.is_dir():
        raise InstallError(f"plugin target exists but is not a directory: {target}")
    sandbox_access_updated = False
    if target.exists():
        sandbox_access_updated = ensure_windows_codex_read_access(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    stage_parent = Path(tempfile.mkdtemp(prefix=f".{PLUGIN_NAME}-stage-", dir=target.parent))
    staged_plugin = stage_parent / PLUGIN_NAME
    backup_path: Path | None = None
    try:
        copy_distribution(source_root, staged_plugin)
        errors = validate_plugin(staged_plugin)
        if errors:
            raise InstallError("staged plugin failed validation: " + "; ".join(errors))
        if file_snapshot(target) == file_snapshot(staged_plugin):
            return target, None, False, sandbox_access_updated

        if target.exists():
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup_path = home / ".codex" / "plugin-backups" / f"{PLUGIN_NAME}-{timestamp}"
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(target, backup_path)
        os.replace(staged_plugin, target)
        sandbox_access_updated = (
            ensure_windows_codex_read_access(target) or sandbox_access_updated
        )
        return target, backup_path, True, sandbox_access_updated
    except Exception:
        if backup_path is not None and backup_path.exists() and not target.exists():
            os.replace(backup_path, target)
        raise
    finally:
        shutil.rmtree(stage_parent, ignore_errors=True)


def run_install(args: argparse.Namespace) -> dict[str, Any]:
    home = args.home.expanduser().resolve()
    source_root = args.source_root.expanduser().resolve()
    if not args.skip_codex_install and home != Path.home().resolve():
        raise InstallError(
            "--home may differ from the current user home only with --skip-codex-install; "
            "otherwise Codex would read a different personal marketplace"
        )
    source_errors = validate_plugin(source_root)
    if source_errors:
        raise InstallError("source plugin failed validation: " + "; ".join(source_errors))

    legacy_skill = home / ".codex" / "skills" / PLUGIN_NAME
    if legacy_skill.exists():
        raise InstallError(
            f"legacy standalone Skill exists at {legacy_skill}; back it up and remove only that "
            "installed copy before installing the plugin (keep ~/.codex/auto-router)"
        )

    marketplace_path = home / ".agents" / "plugins" / "marketplace.json"
    marketplace = load_marketplace(marketplace_path)
    updated_marketplace, marketplace_changed = update_marketplace(
        marketplace, force=args.force_marketplace_entry
    )
    target, backup_path, package_changed, sandbox_access_updated = install_package(
        source_root, home
    )
    try:
        if marketplace_changed:
            atomic_write_json(marketplace_path, updated_marketplace)
    except Exception:
        if package_changed and target.exists():
            shutil.rmtree(target)
        if backup_path is not None and backup_path.exists():
            os.replace(backup_path, target)
        raise

    marketplace_name = updated_marketplace["name"]
    codex_installed = False
    if not args.skip_codex_install:
        codex = shutil.which("codex")
        if codex is None:
            raise InstallError(
                "Codex CLI is not on PATH; the plugin package and personal marketplace are ready, "
                f"then run `codex plugin add {PLUGIN_NAME}@{marketplace_name}`"
            )
        try:
            completed = subprocess.run(
                [codex, "plugin", "add", f"{PLUGIN_NAME}@{marketplace_name}", "--json"],
                check=False,
                text=True,
                capture_output=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired as exc:
            raise InstallError(
                "Codex plugin installation timed out; the local package and marketplace entry "
                f"were preserved for retry: codex plugin add {PLUGIN_NAME}@{marketplace_name}"
            ) from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            suffix = f" ({detail})" if detail else ""
            raise InstallError(
                "Codex plugin installation failed; the local package and marketplace entry were "
                f"preserved for retry: codex plugin add {PLUGIN_NAME}@{marketplace_name}{suffix}"
            )
        codex_installed = True

    return {
        "pluginRoot": str(target),
        "marketplacePath": str(marketplace_path),
        "marketplaceName": marketplace_name,
        "packageChanged": package_changed,
        "marketplaceChanged": marketplace_changed,
        "sandboxReadAccessUpdated": sandbox_access_updated,
        "codexInstalled": codex_installed,
        "backupPath": str(backup_path) if backup_path is not None else None,
    }


def main() -> int:
    args = parse_args()
    try:
        result = run_install(args)
    except (InstallError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
