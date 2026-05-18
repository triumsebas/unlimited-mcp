# Copyright 2026 Sebastian Fernandez Alberdi
# SPDX-License-Identifier: Apache-2.0
# Part of unlimited-mcp — https://github.com/triumsebas/unlimited-mcp

"""CLIAgent abstraction and ``render_argv`` — the param-to-argv pipeline.

A :class:`CLIAgent` is the resolved view of one agent in ``config.yaml``:
the CLI binary it wraps (from ``knowledge.yaml.clis``) plus the merged
parameter values that should drive its invocation.  The single public
operation is :meth:`CLIAgent.render_argv`, which produces a
:class:`RenderResult` ready for submission to a runner.

Merge precedence (lowest → highest)
-----------------------------------
1. ``ParamSpec.default``                — knowledge.yaml
2. ``AgentConfig.params``               — config.yaml (per-agent default)
3. ``render_argv(... params_override=)``— per-call override

Template format (``CliKnowledge.command_template``)
---------------------------------------------------
The template is :func:`shlex.split` into tokens.  Whole-token placeholders:

* ``{prompt}``      — inline prompt token (``prompt_via="arg"`` only).
* ``{prompt_file}`` — replaced with the path of ``job_dir/prompt.txt``
                      by the runner (``prompt_via="file"`` only).
* ``{files}``       — expanded to ``len(files)`` tokens, one per file.
* ``{cwd}``         — working directory; collapses to zero tokens when None.

Prompt delivery modes (``CliKnowledge.prompt_via``)
---------------------------------------------------
* ``"arg"``                    — ``{prompt}`` in argv (default).
* ``"stdin"``                  — written to ``job_dir/stdin.txt``, piped
                                 as stdin; template has no ``{prompt}``.
* ``"file"``                   — written to ``job_dir/prompt.txt``;
                                 template uses ``{prompt_file}``.
* ``"arg_with_stdin_fallback"``— arg up to 64 KB, stdin beyond that.

In-token interpolation is intentionally **not** supported.

Param render rules (``ParamSpec.render``)
-----------------------------------------
* ``""`` or ``None`` → metadata-only param, never appears in argv.
* ``ParamSpec.type == "bool"``       → ``render`` must be a ``dict`` like
  ``{"true": "--git", "false": "--no-git"}``.
* ``ParamSpec.type in {"str", "int"}`` → ``render`` is a string template;
  ``{value}`` is replaced inside each token.
* ``ParamSpec.type == "list[str]"``  → same template, applied per element.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from typing import Any

from unlimited_mcp.config.schema import Config, Knowledge, ParamSpec

_STDIN_THRESHOLD = 64 * 1024  # bytes; above this, arg_with_stdin_fallback uses stdin


class AgentRenderError(ValueError):
    """Raised when an agent invocation cannot be rendered to argv."""


@dataclass(frozen=True)
class RenderResult:
    """Output of :meth:`CLIAgent.render_argv`.

    Attributes
    ----------
    argv:
        Final command + flags.  For ``prompt_via="file"`` the literal token
        ``{prompt_file}`` is left in place; the runner substitutes the real
        path after writing ``job_dir/prompt.txt``.
    stdin_content:
        Text to pipe as stdin (``"stdin"`` / ``"arg_with_stdin_fallback"``
        modes).  ``None`` when not applicable.
    prompt_file_content:
        Text to write to ``job_dir/prompt.txt`` (``"file"`` mode).
        ``None`` when not applicable.
    prompt_via:
        The effective delivery mode used (resolved from
        ``arg_with_stdin_fallback`` to either ``"arg"`` or ``"stdin"``).
    """

    argv: list[str]
    stdin_content: str | None = None
    prompt_file_content: str | None = None
    prompt_via: str = "arg"


@dataclass(frozen=True)
class CLIAgent:
    """Resolved agent definition: CLI binary plus merged parameter values.

    Build via :meth:`from_config`; the constructor is intended for tests
    that need a fully formed instance without going through merge logic.
    """

    name: str
    cli: str
    command_template: str
    stdin_command_template: str | None
    prompt_via: str
    params_catalog: dict[str, ParamSpec]
    params_active: dict[str, Any]

    # ------------------------------------------------------------------

    @classmethod
    def from_config(
        cls,
        name: str,
        config: Config,
        knowledge: Knowledge,
    ) -> CLIAgent:
        """Build a :class:`CLIAgent` by merging config + knowledge.

        Raises
        ------
        AgentRenderError
            When *name* is not in ``config.agents`` or its ``cli`` is not in
            ``knowledge.clis``.
        """
        agent_cfg = config.agents.get(name)
        if agent_cfg is None:
            raise AgentRenderError(f"Unknown agent {name!r}. Available: {sorted(config.agents)}")
        cli_kn = knowledge.clis.get(agent_cfg.cli)
        if cli_kn is None:
            raise AgentRenderError(
                f"Agent {name!r} references unknown CLI {agent_cfg.cli!r}. "
                f"Available CLIs: {sorted(knowledge.clis)}"
            )

        merged: dict[str, Any] = {n: spec.default for n, spec in cli_kn.params.items()}
        merged.update(agent_cfg.params)

        return cls(
            name=name,
            cli=agent_cfg.cli,
            command_template=cli_kn.command_template,
            stdin_command_template=cli_kn.stdin_command_template,
            prompt_via=cli_kn.prompt_via,
            params_catalog=dict(cli_kn.params),
            params_active=merged,
        )

    # ------------------------------------------------------------------

    def render_argv(
        self,
        *,
        prompt: str | None = None,
        files: list[str] | None = None,
        params_override: dict[str, Any] | None = None,
        cwd: str | None = None,
    ) -> RenderResult:
        """Render this agent's invocation to a :class:`RenderResult`.

        Param order in the produced argv is: tokens from ``command_template``
        first (in source order), followed by rendered params in the order
        they appear in ``params_catalog`` (i.e. knowledge declaration order).
        """
        params: dict[str, Any] = dict(self.params_active)
        if params_override:
            params.update(params_override)

        for pname, spec in self.params_catalog.items():
            if spec.required and params.get(pname) is None:
                raise AgentRenderError(f"Required param {pname!r} missing for agent {self.name!r}.")

        # Resolve effective delivery mode
        effective_via = self.prompt_via
        if effective_via == "arg_with_stdin_fallback":
            if prompt and len(prompt.encode()) > _STDIN_THRESHOLD:
                effective_via = "stdin"
            else:
                effective_via = "arg"

        # Select template: stdin_command_template takes precedence for stdin/file
        # modes when the CLI syntax changes (e.g. "codex exec -" vs "codex exec {prompt}").
        use_stdin_template = (
            effective_via in ("stdin", "file")
            and self.stdin_command_template is not None
        )
        template = self.stdin_command_template if use_stdin_template else self.command_template

        argv = _expand_template(
            template,
            prompt=prompt if effective_via == "arg" else None,
            include_prompt_token=effective_via == "arg",
            files=files or [],
            cwd=cwd,
        )
        for pname, spec in self.params_catalog.items():
            value = params.get(pname)
            if value is None:
                continue
            argv.extend(_render_param(spec, value, agent=self.name, param=pname))

        stdin_content: str | None = None
        prompt_file_content: str | None = None
        if effective_via == "stdin":
            stdin_content = prompt
        elif effective_via == "file":
            prompt_file_content = prompt

        return RenderResult(
            argv=argv,
            stdin_content=stdin_content,
            prompt_file_content=prompt_file_content,
            prompt_via=effective_via,
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _expand_template(
    template: str,
    *,
    prompt: str | None,
    include_prompt_token: bool,
    files: list[str],
    cwd: str | None,
) -> list[str]:
    tokens = shlex.split(template)
    out: list[str] = []
    for tok in tokens:
        if tok == "{prompt}":
            if not include_prompt_token:
                # CLI uses stdin or file mode — {prompt} should not be in template.
                raise AgentRenderError(
                    f"Template contains {{prompt}} but prompt_via is not 'arg'. "
                    f"Remove {{prompt}} from the command_template: {template!r}."
                )
            if prompt is None:
                raise AgentRenderError(
                    f"Template references {{prompt}} but no prompt was provided "
                    f"(template: {template!r})."
                )
            out.append(prompt)
        elif tok == "{prompt_file}":
            # Kept as literal placeholder; the runner substitutes the real path.
            out.append("{prompt_file}")
        elif tok == "{files}":
            out.extend(files)
        elif tok == "{cwd}":
            if cwd is not None:
                out.append(cwd)
        else:
            out.append(tok)
    return out


def _render_param(
    spec: ParamSpec,
    value: Any,
    *,
    agent: str,
    param: str,
) -> list[str]:
    render = spec.render
    if render == "" or render is None:
        return []

    if spec.type == "bool":
        return _render_bool(render, value, agent=agent, param=param)
    if spec.type == "list[str]":
        return _render_list(render, value, agent=agent, param=param)
    return _render_scalar(render, value, agent=agent, param=param)


def _render_bool(render: Any, value: Any, *, agent: str, param: str) -> list[str]:
    if not isinstance(render, dict):
        raise AgentRenderError(
            f"Param {param!r} of agent {agent!r}: bool type requires dict render, "
            f"got {type(render).__name__}."
        )
    key = "true" if bool(value) else "false"
    flag = render.get(key, "")
    if not flag:
        return []
    if not isinstance(flag, str):
        raise AgentRenderError(
            f"Param {param!r} of agent {agent!r}: render values must be strings, "
            f"got {type(flag).__name__} for key {key!r}."
        )
    return shlex.split(flag)


def _render_list(render: Any, value: Any, *, agent: str, param: str) -> list[str]:
    if not isinstance(render, str):
        raise AgentRenderError(
            f"Param {param!r} of agent {agent!r}: list[str] type requires str "
            f"render template, got {type(render).__name__}."
        )
    if not isinstance(value, list):
        raise AgentRenderError(
            f"Param {param!r} of agent {agent!r}: expected list, got {type(value).__name__}."
        )
    out: list[str] = []
    for v in value:
        out.extend(_substitute_value(render, v))
    return out


def _render_scalar(render: Any, value: Any, *, agent: str, param: str) -> list[str]:
    if not isinstance(render, str):
        raise AgentRenderError(
            f"Param {param!r} of agent {agent!r}: scalar type requires str render "
            f"template, got {type(render).__name__}."
        )
    return _substitute_value(render, value)


def _substitute_value(template: str, value: Any) -> list[str]:
    """``shlex.split`` *template*, then replace ``{value}`` inside each token."""
    tokens = shlex.split(template)
    return [tok.replace("{value}", str(value)) for tok in tokens]
