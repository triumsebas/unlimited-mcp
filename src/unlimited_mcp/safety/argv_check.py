"""Top-level safety pipeline: argv → :class:`SafetyDecision`.

The pipeline runs the steps from plan §7 in order:

1. Classify the argv against ``Knowledge.tools`` (with flag patterns).
2. Refuse shell-like argv (``bash -lc``, ``python -c`` ...) unless
   ``safety.allow_shell_like_argv`` is set; if set, escalate to
   ``dangerous``.
3. Verify that every detected path argument and the ``cwd`` are inside
   ``allowed_roots`` and outside ``deny_paths``.
4. If the resulting class is ``dangerous``, require a single-use
   confirmation token (issue one when missing, consume it when given).

A :class:`SafetyDecision` is always returned; the caller never sees an
exception from this module.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from unlimited_mcp.config.knowledge import KnowledgeStore
from unlimited_mcp.config.loader import ConfigStore
from unlimited_mcp.config.schema import Config, Knowledge
from unlimited_mcp.jobs.result import BlastRadius, RiskLevel, SafetyClass

from .allowed_roots import check_paths, find_path_args
from .classes import _higher, classify_argv
from .confirmation import ConfirmationStore

#: Mapping from class to the default risk level of an *allowed* invocation.
_RISK_FROM_CLASS: dict[SafetyClass, RiskLevel] = {
    "read": "low",
    "unknown": "medium",
    "mutating": "medium",
    "dangerous": "high",
}


class SafetyDecision(BaseModel):
    """Outcome of the safety pipeline. Either ``allowed`` is true and the
    caller proceeds, or it carries an ``error_code`` (orchestrator stops
    and surfaces the hint), or it carries a ``confirm_token`` (orchestrator
    asks the user, then re-calls)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: bool
    safety_class: SafetyClass
    risk_level: RiskLevel
    blast_radius: BlastRadius = "local"
    requires_confirmation: bool = False
    confirm_token: str | None = None
    confirm_reason: str | None = None
    error_code: (
        Literal[
            "OUT_OF_ROOT",
            "SHELL_LIKE_BLOCKED",
            "CONFIRMATION_EXPIRED",
        ]
        | None
    ) = None
    error_hint: str | None = None
    detected_paths: list[str] = Field(default_factory=list)


class SafetyChecker:
    """Stateful safety pipeline that holds the confirmation store.

    Lifetime is the lifetime of the MCP server: a single instance is
    shared across all tool calls so confirmation tokens persist between
    invocations.
    """

    def __init__(
        self,
        config: Config | ConfigStore,
        knowledge: Knowledge | KnowledgeStore,
        confirmations: ConfirmationStore | None = None,
    ) -> None:
        self._config_src = config
        self._knowledge_src = knowledge
        # Seed the confirmation TTL from the initial config.
        initial_cfg = config.get() if isinstance(config, ConfigStore) else config
        self.confirmations = confirmations or ConfirmationStore(
            ttl_seconds=initial_cfg.safety.confirm_token_ttl_seconds
        )

    @property
    def config(self) -> Config:
        return self._config_src.get() if isinstance(self._config_src, ConfigStore) else self._config_src

    @property
    def knowledge(self) -> Knowledge:
        return self._knowledge_src.get() if isinstance(self._knowledge_src, KnowledgeStore) else self._knowledge_src

    def check_run_command(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        confirm_token: str | None = None,
    ) -> SafetyDecision:
        """Apply the full pipeline to a ``run_command`` invocation."""
        if not argv:
            return SafetyDecision(
                allowed=False,
                safety_class="unknown",
                risk_level="low",
                error_code="SHELL_LIKE_BLOCKED",
                error_hint="argv is empty.",
            )

        cli_basename = PurePosixPath(argv[0]).name
        cls = classify_argv(argv, self.knowledge)

        # ---- 2. shell-like argv -----------------------------------------
        shell_spec = self.knowledge.shell_like_argv.get(cli_basename)
        is_shell_like = shell_spec is not None and any(
            flag in argv[1:] for flag in shell_spec.inline_flags
        )
        if is_shell_like:
            if not self.config.safety.allow_shell_like_argv:
                return SafetyDecision(
                    allowed=False,
                    safety_class="dangerous",
                    risk_level="high",
                    error_code="SHELL_LIKE_BLOCKED",
                    error_hint=(
                        "Shell-like inline-code invocations are blocked by default. "
                        "Set safety.allow_shell_like_argv: true (still requires "
                        "confirm_token), or wait for run_shell in phase 2."
                    ),
                )
            cls = _higher(cls, "dangerous")

        # ---- 3. allowed-roots -------------------------------------------
        tool = self.knowledge.tools.get(cli_basename)
        paths = find_path_args(argv, tool)
        if cwd:
            paths = [*paths, cwd]

        offender = check_paths(
            paths,
            self.config.allowed_roots,
            self.config.deny_paths,
        )
        if offender is not None:
            return SafetyDecision(
                allowed=False,
                safety_class=cls,
                risk_level=_RISK_FROM_CLASS[cls],
                error_code="OUT_OF_ROOT",
                error_hint=(
                    f"Path {offender!r} is outside allowed_roots. "
                    f"Call add_allowed_root({offender!r}) or use a different cwd."
                ),
                detected_paths=paths,
            )

        # ---- 4. dangerous → confirmation --------------------------------
        if cls == "dangerous":
            if confirm_token is None:
                token = self.confirmations.issue({"argv": list(argv), "cwd": cwd})
                return SafetyDecision(
                    allowed=False,
                    safety_class=cls,
                    risk_level="high",
                    requires_confirmation=True,
                    confirm_token=token,
                    confirm_reason=(
                        f"`{cli_basename}` is classified as dangerous; "
                        f"re-call with confirm_token={token!r} to proceed."
                    ),
                    detected_paths=paths,
                )
            payload = self.confirmations.consume(confirm_token)
            if payload is None:
                return SafetyDecision(
                    allowed=False,
                    safety_class=cls,
                    risk_level="high",
                    error_code="CONFIRMATION_EXPIRED",
                    error_hint=(
                        "Confirmation token is unknown or expired. Re-call "
                        "without confirm_token to obtain a new one."
                    ),
                    detected_paths=paths,
                )

        return SafetyDecision(
            allowed=True,
            safety_class=cls,
            risk_level=_RISK_FROM_CLASS[cls],
            detected_paths=paths,
        )

    def check_run_shell(
        self,
        *,
        cwd: str | None = None,
    ) -> SafetyDecision:
        """Safety check for run_shell: always mutating, cwd must be in allowed_roots."""
        if cwd:
            offender = check_paths(
                [cwd],
                self.config.allowed_roots,
                self.config.deny_paths,
            )
            if offender is not None:
                return SafetyDecision(
                    allowed=False,
                    safety_class="mutating",
                    risk_level="medium",
                    error_code="OUT_OF_ROOT",
                    error_hint=(
                        f"Path {offender!r} is outside allowed_roots. "
                        f"Call add_allowed_root({offender!r}) or use a different cwd."
                    ),
                )
        return SafetyDecision(
            allowed=True,
            safety_class="mutating",
            risk_level="medium",
        )
