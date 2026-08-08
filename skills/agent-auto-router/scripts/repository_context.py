"""Deterministic repository inspection and compact context planning."""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
from collections import Counter
from typing import Any


SOURCE_EXTENSIONS = {
    ".c", ".cc", ".cpp", ".cs", ".css", ".go", ".h", ".hpp", ".html",
    ".java", ".js", ".jsx", ".kt", ".kts", ".php", ".py", ".rb", ".rs",
    ".scala", ".sql", ".swift", ".ts", ".tsx", ".vue",
}
MANIFEST_NAMES = {
    "package.json", "pyproject.toml", "pom.xml", "build.gradle", "build.gradle.kts",
    "cargo.toml", "go.mod", "composer.json", "gemfile", "requirements.txt",
}
CONTEXT_NAMES = MANIFEST_NAMES | {"agents.md", "readme.md", "makefile"}
IGNORED_PARTS = {
    ".git", ".idea", ".vscode", "node_modules", "target", "dist", "build",
    "coverage", ".venv", "venv", "__pycache__",
}
GENERIC_TERMS = {
    "add", "and", "change", "code", "create", "fix", "implement", "module", "please",
    "project", "test", "tests", "the", "this", "update", "with", "修改", "实现", "添加",
    "测试", "项目", "代码",
}


def _run(command: list[str], workdir: pathlib.Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=workdir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def collect_repository_files(workdir: pathlib.Path) -> tuple[list[str], bool]:
    git = shutil.which("git")
    if git:
        inside = _run([git, "rev-parse", "--is-inside-work-tree"], workdir)
        if inside.returncode == 0 and inside.stdout.strip() == "true":
            listed = _run(
                [git, "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                workdir,
            )
            if listed.returncode == 0:
                files = [item.replace("\\", "/") for item in listed.stdout.split("\0") if item]
                return sorted(set(files)), True
    rg = shutil.which("rg") or shutil.which("rg.exe")
    if rg:
        listed = _run([rg, "--files", "-0"], workdir)
        if listed.returncode == 0:
            files = [item.replace("\\", "/") for item in listed.stdout.split("\0") if item]
            return sorted(set(files)), False
    files: list[str] = []
    for root, dirs, names in os.walk(workdir):
        dirs[:] = [name for name in dirs if name.lower() not in IGNORED_PARTS]
        root_path = pathlib.Path(root)
        for name in names:
            relative = (root_path / name).relative_to(workdir).as_posix()
            files.append(relative)
    return sorted(set(files)), False


def inspect_repository(workdir: pathlib.Path, task: str = "") -> dict[str, Any]:
    resolved = workdir.resolve()
    files, is_git_repo = collect_repository_files(resolved)
    source_files = [path for path in files if pathlib.PurePosixPath(path).suffix.lower() in SOURCE_EXTENSIONS]
    tests = [
        path for path in files
        if "test" in pathlib.PurePosixPath(path).name.lower()
        or any(part.lower() in {"test", "tests", "spec", "specs"} for part in pathlib.PurePosixPath(path).parts)
    ]
    languages = Counter(pathlib.PurePosixPath(path).suffix.lower() for path in source_files)
    manifests = [path for path in files if pathlib.PurePosixPath(path).name.lower() in MANIFEST_NAMES]
    manifest_roots = {str(pathlib.PurePosixPath(path).parent).lower() for path in manifests}
    dirty = False
    if is_git_repo and shutil.which("git"):
        status = _run(["git", "status", "--porcelain", "--untracked-files=no"], resolved)
        dirty = status.returncode == 0 and bool(status.stdout.strip())
    return {
        "repo_files": len(files),
        "source_files": len(source_files),
        "test_files": len(tests),
        "language_count": len(languages),
        "manifest_count": len(manifests),
        "large_repo": len(source_files) >= 1000 or len(files) >= 2500,
        "monorepo": len({root for root in manifest_roots if root != "."}) >= 2,
        "dirty_worktree": dirty,
        "is_git_repo": is_git_repo,
        "files": files,
        "task_has_path_hint": bool(re.search(r"(?:^|\s)[\w./\\-]+\.[A-Za-z0-9]{1,8}(?:$|\s)", task)),
    }


def _task_terms(task: str) -> set[str]:
    terms = {
        term.lower().strip("./\\-")
        for term in re.findall(r"[\w./\\-]{2,}", task.lower())
    }
    return {term for term in terms if term and term not in GENERIC_TERMS}


def rank_candidate_files(files: list[str], task: str, limit: int) -> list[str]:
    terms = _task_terms(task)
    ranked: list[tuple[int, int, str]] = []
    for path in files:
        pure = pathlib.PurePosixPath(path)
        lower = path.lower()
        score = 0
        for term in terms:
            normalized = term.replace("\\", "/")
            if normalized == lower or normalized == pure.name.lower():
                score += 8
            elif normalized in lower:
                score += 3
        if pure.name.lower() in CONTEXT_NAMES:
            score += 1
        if "test" in task.lower() and "test" in lower:
            score += 2
        if score:
            ranked.append((-score, len(path), path))
    ranked.sort()
    return [path for _, _, path in ranked[:limit]]


def build_repository_context(
    workdir: pathlib.Path,
    task: str,
    *,
    max_candidate_files: int,
    repo_map_tokens: int,
) -> tuple[str, dict[str, Any]]:
    inspected = inspect_repository(workdir, task)
    files = list(inspected.pop("files"))
    candidates = rank_candidate_files(files, task, max_candidate_files)
    top_levels = Counter(pathlib.PurePosixPath(path).parts[0] for path in files if path)
    top_level_text = ", ".join(
        f"{name}({count})" for name, count in top_levels.most_common(12)
    )
    candidate_text = "\n".join(f"- {path}" for path in candidates) or "- none identified"
    context = (
        "DETERMINISTIC LOCAL REPOSITORY CONTEXT (verify before editing; candidates are not exhaustive):\n"
        f"files={inspected['repo_files']} source_files={inspected['source_files']} "
        f"test_files={inspected['test_files']} languages={inspected['language_count']} "
        f"monorepo={str(inspected['monorepo']).lower()}\n"
        f"top-level entries: {top_level_text or 'unknown'}\n"
        f"candidate paths:\n{candidate_text}\n"
        "Start with the candidate paths and targeted search. Expand only when evidence requires it."
    )
    max_chars = max(512, repo_map_tokens * 4)
    if len(context) > max_chars:
        context = context[: max_chars - 16].rstrip() + "\n[context cut]"
    metadata = {
        **inspected,
        "candidate_files": len(candidates),
        "context_chars": len(context),
        "context_useful": bool(candidates) or inspected["repo_files"] >= 50,
    }
    return context, metadata
