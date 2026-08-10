#!/usr/bin/env python3
"""Validate the repository's Codex plugin package without external dependencies."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

from validate_skill import validate as validate_skill


NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SEMVER_PATTERN = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
ALLOWED_FIELDS = {
    "id",
    "name",
    "version",
    "description",
    "skills",
    "apps",
    "mcpServers",
    "interface",
    "author",
    "homepage",
    "repository",
    "license",
    "keywords",
}
REQUIRED_INTERFACE_FIELDS = (
    "displayName",
    "shortDescription",
    "longDescription",
    "developerName",
    "category",
)
ALLOWED_INTERFACE_FIELDS = {
    "displayName",
    "shortDescription",
    "longDescription",
    "developerName",
    "category",
    "capabilities",
    "websiteURL",
    "privacyPolicyURL",
    "termsOfServiceURL",
    "brandColor",
    "composerIcon",
    "logo",
    "logoDark",
    "screenshots",
    "defaultPrompt",
    "default_prompt",
}
HEX_COLOR_PATTERN = re.compile(r"^#[0-9A-F]{6}$", re.IGNORECASE)


def non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def resolve_contract_path(plugin_root: Path, raw_path: Any) -> Path | None:
    if not non_empty_string(raw_path) or not raw_path.startswith("./"):
        return None
    relative = PurePosixPath(raw_path[2:].replace("\\", "/"))
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        return None
    resolved = (plugin_root / relative.as_posix()).resolve()
    if not resolved.is_relative_to(plugin_root.resolve()):
        return None
    return resolved


def is_absolute_https_url(value: Any) -> bool:
    if not non_empty_string(value):
        return False
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.netloc)


def validate_asset_path(plugin_root: Path, raw_path: Any, field: str) -> str | None:
    resolved = resolve_contract_path(plugin_root, raw_path)
    if resolved is None:
        return f"plugin interface.{field} must be a relative path inside the plugin"
    if not resolved.is_file():
        return f"plugin interface.{field} points to a missing file"
    return None


def load_companion_object(path: Path, label: str, errors: list[str]) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        errors.append(f"{label} is required when declared")
        return None
    except (OSError, UnicodeError, json.JSONDecodeError):
        errors.append(f"{label} must contain valid JSON")
        return None
    if not isinstance(payload, dict):
        errors.append(f"{label} must contain a JSON object")
        return None
    return payload


def validate_mcp_entries(value: Any, label: str, errors: list[str]) -> None:
    if not isinstance(value, dict):
        errors.append(f"{label} must be an object")
        return
    for name, config in value.items():
        if not non_empty_string(name):
            errors.append(f"{label} server names must be non-empty strings")
        if not isinstance(config, dict):
            errors.append(f"{label} server {name!r} must be an object")


def validate(plugin_root: Path) -> list[str]:
    errors: list[str] = []
    manifest_path = plugin_root / ".codex-plugin" / "plugin.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [f"missing file: {manifest_path}"]
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return [f"invalid plugin manifest: {exc}"]
    if not isinstance(manifest, dict):
        return ["plugin.json must contain a JSON object"]

    unknown = sorted(set(manifest) - ALLOWED_FIELDS)
    if unknown:
        errors.append(f"unsupported plugin fields: {', '.join(unknown)}")

    name = manifest.get("name")
    if not non_empty_string(name) or not NAME_PATTERN.fullmatch(name):
        errors.append("plugin name must use lowercase letters, digits, and hyphens")
    elif plugin_root.name != name:
        errors.append(f"plugin directory {plugin_root.name!r} must match name {name!r}")

    version = manifest.get("version")
    if not non_empty_string(version) or not SEMVER_PATTERN.fullmatch(version):
        errors.append("plugin version must use strict semver")
    if not non_empty_string(manifest.get("description")):
        errors.append("plugin description is required")

    author = manifest.get("author")
    if not isinstance(author, dict) or not non_empty_string(author.get("name")):
        errors.append("plugin author.name is required")
    elif set(author) - {"name", "email", "url"}:
        errors.append("plugin author contains unsupported fields")

    if isinstance(author, dict):
        if "email" in author and not non_empty_string(author["email"]):
            errors.append("plugin author.email must be a non-empty string")
        if "url" in author and not is_absolute_https_url(author["url"]):
            errors.append("plugin author.url must be an absolute https URL")

    if "id" in manifest and not non_empty_string(manifest["id"]):
        errors.append("plugin id must be a non-empty string")
    if "license" in manifest and not non_empty_string(manifest["license"]):
        errors.append("plugin license must be a non-empty string")
    if "keywords" in manifest:
        keywords = manifest["keywords"]
        if not isinstance(keywords, list) or not all(non_empty_string(item) for item in keywords):
            errors.append("plugin keywords must be an array of strings")

    for field in ("homepage", "repository"):
        value = manifest.get(field)
        if value is not None and not is_absolute_https_url(value):
            errors.append(f"plugin {field} must be an absolute https URL")

    skills_value = manifest.get("skills")
    skills_path = resolve_contract_path(plugin_root, skills_value)
    if skills_value != "./skills/" or skills_path is None or not skills_path.is_dir():
        errors.append("plugin skills must resolve to ./skills/")
    else:
        skill_roots = sorted(
            path
            for path in skills_path.iterdir()
            if path.is_dir()
            and not path.name.startswith(".")
        )
        if not skill_roots:
            errors.append("plugin skills directory must contain at least one Skill")
        for skill_root in skill_roots:
            if not (skill_root / "SKILL.md").is_file():
                errors.append(f"skill {skill_root.name!r} is missing SKILL.md")
            else:
                errors.extend(f"{skill_root.name}: {error}" for error in validate_skill(skill_root))

    interface = manifest.get("interface")
    if not isinstance(interface, dict):
        errors.append("plugin interface is required")
    else:
        unknown_interface = sorted(set(interface) - ALLOWED_INTERFACE_FIELDS)
        if unknown_interface:
            errors.append(
                "unsupported plugin interface fields: " + ", ".join(unknown_interface)
            )
        for field in REQUIRED_INTERFACE_FIELDS:
            if not non_empty_string(interface.get(field)):
                errors.append(f"plugin interface.{field} is required")
        capabilities = interface.get("capabilities")
        if not isinstance(capabilities, list) or not all(non_empty_string(item) for item in capabilities):
            errors.append("plugin interface.capabilities must be an array of strings")
        default_prompt = interface.get("defaultPrompt", interface.get("default_prompt"))
        if isinstance(default_prompt, list):
            if not 1 <= len(default_prompt) <= 3 or not all(non_empty_string(item) for item in default_prompt):
                errors.append("plugin interface.defaultPrompt must contain one to three strings")
        elif not non_empty_string(default_prompt):
            errors.append("plugin interface.defaultPrompt is required")

        for field in ("websiteURL", "privacyPolicyURL", "termsOfServiceURL"):
            if field in interface and not is_absolute_https_url(interface[field]):
                errors.append(f"plugin interface.{field} must be an absolute https URL")
        if "brandColor" in interface and (
            not isinstance(interface["brandColor"], str)
            or HEX_COLOR_PATTERN.fullmatch(interface["brandColor"]) is None
        ):
            errors.append("plugin interface.brandColor must use #RRGGBB")
        for field in ("composerIcon", "logo", "logoDark"):
            if field in interface:
                asset_error = validate_asset_path(plugin_root, interface[field], field)
                if asset_error:
                    errors.append(asset_error)
        if "screenshots" in interface:
            screenshots = interface["screenshots"]
            if not isinstance(screenshots, list):
                errors.append("plugin interface.screenshots must be an array")
            else:
                for index, raw_path in enumerate(screenshots):
                    asset_error = validate_asset_path(
                        plugin_root, raw_path, f"screenshots[{index}]"
                    )
                    if asset_error:
                        errors.append(asset_error)

    if "apps" in manifest:
        if manifest["apps"] != "./.app.json":
            errors.append("plugin apps must resolve to .app.json")
        apps = load_companion_object(plugin_root / ".app.json", ".app.json", errors)
        if apps is not None:
            unknown_apps = sorted(set(apps) - {"apps"})
            if unknown_apps:
                errors.append("unsupported .app.json fields: " + ", ".join(unknown_apps))
            if not isinstance(apps.get("apps"), dict):
                errors.append(".app.json field apps must be an object")

    mcp_servers = manifest.get("mcpServers")
    if isinstance(mcp_servers, str):
        if mcp_servers != "./.mcp.json":
            errors.append("plugin mcpServers must resolve to .mcp.json")
        mcp_manifest = load_companion_object(plugin_root / ".mcp.json", ".mcp.json", errors)
        if mcp_manifest is not None:
            unknown_mcp = sorted(set(mcp_manifest) - {"mcpServers"})
            if unknown_mcp:
                errors.append("unsupported .mcp.json fields: " + ", ".join(unknown_mcp))
            validate_mcp_entries(mcp_manifest.get("mcpServers"), ".mcp.json mcpServers", errors)
    elif isinstance(mcp_servers, dict):
        validate_mcp_entries(mcp_servers, "plugin mcpServers", errors)
    elif mcp_servers is not None:
        errors.append("plugin mcpServers must be a string path or object")

    if "[TODO:" in manifest_path.read_text(encoding="utf-8"):
        errors.append("plugin manifest contains a TODO placeholder")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "plugin_root",
        nargs="?",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    args = parser.parse_args()
    plugin_root = args.plugin_root.resolve()
    errors = validate(plugin_root)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"Plugin valid: {plugin_root.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
