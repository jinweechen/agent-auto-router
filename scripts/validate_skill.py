#!/usr/bin/env python3
"""Validate the repository's Skill package without external dependencies."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
REQUIRED_INTERFACE_KEYS = ("display_name", "short_description", "default_prompt")


def parse_frontmatter(skill_file: Path) -> dict[str, str]:
    text = skill_file.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("SKILL.md must start with YAML frontmatter")
    try:
        closing = lines.index("---", 1)
    except ValueError as exc:
        raise ValueError("SKILL.md frontmatter is not closed") from exc

    values: dict[str, str] = {}
    for line in lines[1:closing]:
        if not line.strip():
            continue
        match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_-]*):\s*(.+)", line)
        if not match:
            raise ValueError(f"unsupported frontmatter line: {line!r}")
        key, value = match.groups()
        values[key] = value.strip().strip('"\'')
    return values


def validate(skill_dir: Path) -> list[str]:
    errors: list[str] = []
    skill_file = skill_dir / "SKILL.md"
    metadata_file = skill_dir / "agents" / "openai.yaml"

    if not skill_file.is_file():
        return [f"missing file: {skill_file}"]

    try:
        frontmatter = parse_frontmatter(skill_file)
    except (OSError, UnicodeError, ValueError) as exc:
        return [str(exc)]

    unknown = sorted(set(frontmatter) - {"name", "description"})
    if unknown:
        errors.append(f"unsupported frontmatter fields: {', '.join(unknown)}")

    name = frontmatter.get("name", "")
    description = frontmatter.get("description", "")
    if not NAME_PATTERN.fullmatch(name):
        errors.append("frontmatter name must use lowercase letters, digits, and hyphens")
    if name and skill_dir.name != name:
        errors.append(f"skill directory {skill_dir.name!r} must match name {name!r}")
    if not description:
        errors.append("frontmatter description is required")
    if len(description) > 1024:
        errors.append("frontmatter description must be at most 1024 characters")

    if not metadata_file.is_file():
        errors.append(f"missing file: {metadata_file}")
    else:
        metadata = metadata_file.read_text(encoding="utf-8")
        for key in REQUIRED_INTERFACE_KEYS:
            if not re.search(rf"^\s{{2}}{re.escape(key)}:\s*\S", metadata, re.MULTILINE):
                errors.append(f"agents/openai.yaml is missing interface.{key}")
        if f"${name}" not in metadata:
            errors.append("agents/openai.yaml default_prompt must reference the Skill name")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "skill_dir",
        nargs="?",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "skills" / "agent-auto-router",
    )
    args = parser.parse_args()
    skill_dir = args.skill_dir.resolve()
    errors = validate(skill_dir)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"Skill valid: {skill_dir.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
