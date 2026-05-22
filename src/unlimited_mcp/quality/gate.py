# Copyright 2026 Sebastian Fernandez Alberdi
# SPDX-License-Identifier: Apache-2.0
# Part of unlimited-mcp — https://github.com/triumsebas/unlimited-mcp

"""Language-aware lint + type-check on a worker's changed files.

The gate runs *inside* the job runner (local watcher thread or the ts worker
subprocess) right after the worker exits, while the git worktree still exists.
It is deliberately self-contained — no MCP/server state — so it works equally
from a separate process.

Flow per detected language:

1. Detect the dominant language from the changed-file extensions.
2. If the core linter binary is absent from PATH → ``MISSINGDEP`` (we can't
   give a verdict). Optional tools (type-checker, formatter) that are absent
   are recorded in ``missing_deps`` but do not block a verdict.
3. Auto-fix formatting in place (left uncommitted in the worktree so the
   orchestrator sees one combined diff).
4. Run the linter(s); collect issues → ``PASS`` (clean) or ``NOPASS``.

The verdict only covers the *changed* files, not the whole repo.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from unlimited_mcp.jobs.result import QualityGateIssue, QualityGateResult, QualityGateStatus

_TOOL_TIMEOUT = 120  # seconds per tool invocation


@dataclass(frozen=True)
class _Linter:
    """A verification tool (linter or type-checker) for a language."""

    tool: str  # binary name (also used for shutil.which and the issue label)
    argv: list[str]  # base argv, tool first; changed files appended if takes_files
    takes_files: bool  # pass the changed-file list, or run project-scoped
    parser: str  # "ruff_json" | "eslint_json" | "regex" | "tsc"
    optional: bool = False  # absence does not force MISSINGDEP


@dataclass(frozen=True)
class _Language:
    name: str
    extensions: tuple[str, ...]
    formatter: list[str] | None  # base argv (tool first); files appended
    formatter_takes_files: bool
    linters: tuple[_Linter, ...]


_LANGUAGES: tuple[_Language, ...] = (
    _Language(
        name="python",
        extensions=(".py", ".pyi"),
        formatter=["ruff", "format"],
        formatter_takes_files=True,
        linters=(
            _Linter("ruff", ["ruff", "check", "--output-format", "json"], True, "ruff_json"),
            _Linter(
                "mypy",
                ["mypy", "--no-error-summary", "--hide-error-context"],
                True,
                "regex",
                optional=True,
            ),
        ),
    ),
    _Language(
        name="javascript",
        extensions=(".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"),
        formatter=["prettier", "--write"],
        formatter_takes_files=True,
        linters=(
            _Linter("eslint", ["eslint", "--format", "json"], True, "eslint_json"),
            _Linter("tsc", ["tsc", "--noEmit"], False, "tsc", optional=True),
        ),
    ),
    _Language(
        name="go",
        extensions=(".go",),
        formatter=["gofmt", "-w"],
        formatter_takes_files=True,
        linters=(_Linter("go", ["go", "vet", "./..."], False, "regex"),),
    ),
    _Language(
        name="rust",
        extensions=(".rs",),
        formatter=["cargo", "fmt"],
        formatter_takes_files=False,
        linters=(
            _Linter("cargo", ["cargo", "clippy", "--message-format", "short"], False, "regex"),
        ),
    ),
)


@dataclass
class _Run:
    exit_code: int
    stdout: str
    stderr: str


def _run(argv: list[str], cwd: Path) -> _Run:
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_TOOL_TIMEOUT,
            check=False,
        )
        return _Run(proc.returncode, proc.stdout, proc.stderr)
    except subprocess.TimeoutExpired:
        return _Run(-1, "", f"{argv[0]}: timed out after {_TOOL_TIMEOUT}s")
    except FileNotFoundError:
        return _Run(127, "", f"{argv[0]}: not found")


def _detect_language(changed_files: list[str]) -> _Language | None:
    """Pick the language with the most matching changed files."""
    best: _Language | None = None
    best_count = 0
    for lang in _LANGUAGES:
        count = sum(1 for f in changed_files if f.endswith(lang.extensions))
        if count > best_count:
            best, best_count = lang, count
    return best


def _hash_files(worktree: Path, files: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for rel in files:
        p = worktree / rel
        if p.is_file():
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


# --- issue parsers ---------------------------------------------------------


def _parse_ruff_json(run: _Run, tool: str) -> list[QualityGateIssue]:
    issues: list[QualityGateIssue] = []
    try:
        data = json.loads(run.stdout or "[]")
    except json.JSONDecodeError:
        return issues
    for item in data if isinstance(data, list) else []:
        loc = item.get("location") or {}
        issues.append(
            QualityGateIssue(
                file=item.get("filename", "?"),
                line=loc.get("row"),
                col=loc.get("column"),
                rule=item.get("code"),
                message=item.get("message", ""),
                tool=tool,
            )
        )
    return issues


def _parse_eslint_json(run: _Run, tool: str) -> list[QualityGateIssue]:
    issues: list[QualityGateIssue] = []
    try:
        data = json.loads(run.stdout or "[]")
    except json.JSONDecodeError:
        return issues
    for entry in data if isinstance(data, list) else []:
        path = entry.get("filePath", "?")
        for msg in entry.get("messages", []):
            if msg.get("severity", 0) < 1:
                continue
            issues.append(
                QualityGateIssue(
                    file=path,
                    line=msg.get("line"),
                    col=msg.get("column"),
                    rule=msg.get("ruleId"),
                    message=msg.get("message", ""),
                    tool=tool,
                )
            )
    return issues


def _parse_regex(run: _Run, tool: str) -> list[QualityGateIssue]:
    """Best-effort ``file:line[:col]: message`` and ``file(line,col): message``."""
    import re

    issues: list[QualityGateIssue] = []
    colon = re.compile(r"^(?P<file>[^:\n]+):(?P<line>\d+):(?:(?P<col>\d+):)?\s*(?P<msg>.+)$")
    paren = re.compile(r"^(?P<file>[^(\n]+)\((?P<line>\d+),(?P<col>\d+)\):\s*(?P<msg>.+)$")
    for raw in (run.stdout + "\n" + run.stderr).splitlines():
        line = raw.rstrip()
        m = colon.match(line) or paren.match(line)
        if not m:
            continue
        g = m.groupdict()
        issues.append(
            QualityGateIssue(
                file=g["file"],
                line=int(g["line"]) if g.get("line") else None,
                col=int(g["col"]) if g.get("col") else None,
                rule=None,
                message=g["msg"][:500],
                tool=tool,
            )
        )
    return issues


_PARSERS = {
    "ruff_json": _parse_ruff_json,
    "eslint_json": _parse_eslint_json,
    "tsc": _parse_regex,
    "regex": _parse_regex,
}


def run_quality_gate(
    worktree_path: str | Path,
    changed_files: list[str],
    *,
    report_dir: Path | None = None,
) -> QualityGateResult:
    """Lint + type-check the changed files in *worktree_path*.

    *changed_files* are repo-relative paths (as ``git diff --name-only``
    reports them). *report_dir*, when given, receives a ``quality_report.txt``
    with the raw tool output, referenced by ``report_ref``.
    """
    worktree = Path(worktree_path)

    lang = _detect_language(changed_files)
    if lang is None:
        return QualityGateResult(
            status="NOTDETECTED",
            hint="No recognized language marker among the changed files; quality gate skipped.",
        )

    lang_files = [f for f in changed_files if f.endswith(lang.extensions)]
    existing = [f for f in lang_files if (worktree / f).is_file()]

    # --- dependency check --------------------------------------------------
    missing: list[str] = []
    if lang.formatter and shutil.which(lang.formatter[0]) is None:
        missing.append(lang.formatter[0])
    core_linter_missing = False
    for linter in lang.linters:
        if shutil.which(linter.tool) is None:
            missing.append(linter.tool)
            if not linter.optional:
                core_linter_missing = True

    if core_linter_missing:
        return QualityGateResult(
            status="MISSINGDEP",
            language=lang.name,
            missing_deps=sorted(set(missing)),
            hint=(
                f"{', '.join(sorted(set(missing)))} not on the worker PATH; cannot verify "
                f"{lang.name} changes. Install them or disable the gate "
                "(quality_gate.enabled=false)."
            ),
        )

    raw_chunks: list[str] = []
    tools_run: list[str] = []

    # --- auto-fix formatting (left uncommitted) ----------------------------
    auto_fixed: list[str] = []
    if lang.formatter and shutil.which(lang.formatter[0]) is not None and existing:
        before = _hash_files(worktree, existing)
        fmt_argv = list(lang.formatter)
        if lang.formatter_takes_files:
            fmt_argv += existing
        fmt_run = _run(fmt_argv, worktree)
        tools_run.append(lang.formatter[0])
        if fmt_run.stdout or fmt_run.stderr:
            raw_chunks.append(f"$ {' '.join(fmt_argv)}\n{fmt_run.stdout}{fmt_run.stderr}")
        after = _hash_files(worktree, existing)
        auto_fixed = sorted(f for f in existing if before.get(f) != after.get(f))

    # --- run linters / type-checkers ---------------------------------------
    issues: list[QualityGateIssue] = []
    for linter in lang.linters:
        if shutil.which(linter.tool) is None:
            continue  # optional + absent; already noted in missing
        argv = list(linter.argv)
        if linter.takes_files:
            if not existing:
                continue
            argv += existing
        run = _run(argv, worktree)
        tools_run.append(linter.tool)
        if run.stdout or run.stderr:
            raw_chunks.append(f"$ {' '.join(argv)}\n{run.stdout}{run.stderr}")
        parser = _PARSERS.get(linter.parser, _parse_regex)
        issues.extend(parser(run, linter.tool))

    report_ref: str | None = None
    if report_dir is not None and raw_chunks:
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / "quality_report.txt"
        report_path.write_text("\n\n".join(raw_chunks), encoding="utf-8")
        report_ref = str(report_path)

    optional_missing = sorted(set(missing))
    status: QualityGateStatus
    if issues:
        tools = ", ".join(sorted({i.tool for i in issues}))
        hint = (
            f"{len(issues)} issue(s) from {tools}. Fix the files in issues[] yourself, "
            "or resume_agent_task(job_id, extra_context=<issues>) to hand them back to the worker."
        )
        status = "NOPASS"
    else:
        hint = None
        if optional_missing:
            hint = f"Lint passed. Optional tools not run (absent): {', '.join(optional_missing)}."
        status = "PASS"

    return QualityGateResult(
        status=status,
        language=lang.name,
        tools_run=tools_run,
        auto_fixed=auto_fixed,
        issues=issues,
        missing_deps=optional_missing,
        report_ref=report_ref,
        hint=hint,
    )
