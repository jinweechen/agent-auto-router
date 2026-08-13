#!/usr/bin/env python3
"""Capture and compare content-aware workspace state for Desktop orchestration."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import stat
import subprocess
import tempfile
from typing import Any, Iterable

from protocol_schemas import WORKSPACE_COMPARISON_SCHEMA, WORKSPACE_SNAPSHOT_SCHEMA

SNAPSHOT_SCHEMA = WORKSPACE_SNAPSHOT_SCHEMA
COMPARISON_SCHEMA = WORKSPACE_COMPARISON_SCHEMA
MANIFEST_FORMAT = "path-type-mode-size-sha256-plus-git-status"


def _git(workdir: pathlib.Path, *arguments: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(workdir), "-c", "core.quotepath=false", *arguments],
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(message or f"git {' '.join(arguments)} failed")
    return completed.stdout


def _is_git_worktree(workdir: pathlib.Path) -> bool:
    completed = subprocess.run(
        ["git", "-C", str(workdir), "rev-parse", "--is-inside-work-tree"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return completed.returncode == 0 and completed.stdout.strip() == "true"


def _decode_path(value: bytes) -> str:
    return value.decode("utf-8", errors="surrogateescape").replace("\\", "/")


def _git_statuses(workdir: pathlib.Path) -> dict[str, str]:
    raw = _git(workdir, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    records = raw.split(b"\0")
    statuses: dict[str, str] = {}
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        if len(record) < 4 or record[2:3] != b" ":
            raise RuntimeError("Unexpected git porcelain v1 -z record")
        status_code = record[:2].decode("ascii", errors="replace")
        path = _decode_path(record[3:])
        statuses[path] = status_code
        if "R" in status_code or "C" in status_code:
            if index >= len(records) or not records[index]:
                raise RuntimeError("Git rename/copy record is missing its source path")
            source = _decode_path(records[index])
            index += 1
            statuses[source] = f"{status_code}:source"
    return statuses


def _git_paths(workdir: pathlib.Path) -> list[str]:
    raw = _git(
        workdir,
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
    )
    return sorted({_decode_path(item) for item in raw.split(b"\0") if item})


def _non_git_paths(workdir: pathlib.Path) -> list[str]:
    paths: list[str] = []
    for root, directories, files in os.walk(workdir, topdown=True, followlinks=False):
        directories[:] = sorted(name for name in directories if name != ".git")
        relative_root = pathlib.Path(root).relative_to(workdir)
        for name in sorted(files):
            relative = (relative_root / name).as_posix()
            paths.append(relative)
        for name in directories:
            candidate = pathlib.Path(root) / name
            if candidate.is_symlink():
                paths.append((relative_root / name).as_posix())
    return sorted(set(paths))


def _sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _identity(path: pathlib.Path, git_status: str) -> dict[str, Any]:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return {"type": "missing", "mode": None, "size": None, "sha256": None, "gitStatus": git_status}

    mode = stat.S_IMODE(metadata.st_mode)
    if path.is_symlink():
        target = os.readlink(path)
        digest = hashlib.sha256(os.fsencode(target)).hexdigest()
        kind = "symlink"
        size = len(os.fsencode(target))
    elif path.is_file():
        digest = _sha256_file(path)
        kind = "file"
        size = metadata.st_size
    elif path.is_dir():
        digest = None
        kind = "directory"
        size = None
    else:
        digest = None
        kind = "other"
        size = metadata.st_size
    return {"type": kind, "mode": mode, "size": size, "sha256": digest, "gitStatus": git_status}


def capture_snapshot(workdir: str | os.PathLike[str]) -> dict[str, Any]:
    root = pathlib.Path(workdir).resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"Workdir is not a directory: {root}")
    git_backed = _is_git_worktree(root)
    statuses = _git_statuses(root) if git_backed else {}
    relative_paths = _git_paths(root) if git_backed else _non_git_paths(root)
    relative_paths = sorted(set(relative_paths) | set(statuses))
    entries = {
        relative: _identity(root / pathlib.PurePosixPath(relative), statuses.get(relative, ""))
        for relative in relative_paths
    }
    return {
        "schema": SNAPSHOT_SCHEMA,
        "manifestFormat": MANIFEST_FORMAT,
        "workdir": str(root),
        "gitBacked": git_backed,
        "entries": entries,
        "dirtyPaths": sorted(statuses),
    }


def compare_snapshots(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    if before.get("schema") != SNAPSHOT_SCHEMA or after.get("schema") != SNAPSHOT_SCHEMA:
        raise ValueError("Both inputs must use the workspace snapshot schema")
    if before.get("workdir") != after.get("workdir"):
        raise ValueError("Workspace snapshots refer to different workdirs")
    before_entries = before.get("entries")
    after_entries = after.get("entries")
    if not isinstance(before_entries, dict) or not isinstance(after_entries, dict):
        raise ValueError("Workspace snapshot entries must be objects")
    changed_paths = sorted(
        path
        for path in set(before_entries) | set(after_entries)
        if before_entries.get(path) != after_entries.get(path)
    )
    preexisting_dirty = sorted(str(path) for path in before.get("dirtyPaths", []))
    final_dirty = sorted(str(path) for path in after.get("dirtyPaths", []))
    return {
        "schema": COMPARISON_SCHEMA,
        "manifestFormat": MANIFEST_FORMAT,
        "workdir": before["workdir"],
        "runChangedPaths": changed_paths,
        "runChangedFileCount": len(changed_paths),
        "preexistingDirtyPaths": preexisting_dirty,
        "preexistingDirtyFileCount": len(preexisting_dirty),
        "finalDirtyPaths": final_dirty,
        "finalDirtyFileCount": len(final_dirty),
    }


def _write_json(path: pathlib.Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="\n",
        dir=path.parent,
        delete=False,
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        temporary = pathlib.Path(handle.name)
    os.replace(temporary, path)


def _ensure_protected_path(
    path: pathlib.Path,
    forbidden_roots: Iterable[pathlib.Path],
    *,
    label: str,
) -> None:
    resolved_path = path.resolve(strict=False)
    for forbidden_root in forbidden_roots:
        try:
            resolved_path.relative_to(forbidden_root.resolve(strict=True))
        except ValueError:
            continue
        raise ValueError(f"{label} must remain outside every child-writable root")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    capture = subparsers.add_parser("capture")
    capture.add_argument("--workdir", required=True)
    capture.add_argument("--output", required=True)
    capture.add_argument("--forbidden-root", action="append", default=[])
    compare = subparsers.add_parser("compare")
    compare.add_argument("--workdir", required=True)
    compare.add_argument("--baseline", required=True)
    compare.add_argument("--output")
    compare.add_argument("--forbidden-root", action="append", default=[])
    return parser


def main(arguments: Iterable[str] | None = None) -> int:
    options = _parser().parse_args(arguments)
    workdir = pathlib.Path(options.workdir).resolve(strict=True)
    forbidden_roots = [workdir, *(pathlib.Path(value) for value in options.forbidden_root)]
    if options.command == "capture":
        output = pathlib.Path(options.output)
        _ensure_protected_path(output, forbidden_roots, label="Snapshot output")
        _write_json(output, capture_snapshot(workdir))
        return 0

    baseline_path = pathlib.Path(options.baseline).resolve(strict=True)
    _ensure_protected_path(baseline_path, forbidden_roots, label="Snapshot baseline")
    with baseline_path.open("r", encoding="utf-8") as handle:
        baseline = json.load(handle)
    comparison = compare_snapshots(baseline, capture_snapshot(workdir))
    if options.output:
        output = pathlib.Path(options.output)
        _ensure_protected_path(output, forbidden_roots, label="Comparison output")
        _write_json(output, comparison)
    else:
        print(json.dumps(comparison, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
