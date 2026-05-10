"""CLIAgent abstraction and ``render_argv`` — the param-to-argv pipeline.

A :class:`CLIAgent` is the resolved view of one agent in ``config.yaml``:
the CLI binary it wraps (from ``knowledge.yaml.clis``) plus the merged
parameter values that should drive its invocation.  The single public
operation is :meth:`CLIAgent.render_argv`, which produces the final argv
list ready for :class:`~unlimited_mcp.hosts.local.LocalHost.run`.

Merge precedence (lowest → highest)
-----------------------------------
1. ``ParamSpec.default``                — knowledge.yaml
2. ``AgentConfig.params``               — config.yaml (per-agent default)
3. ``render_argv(... params_override=)``— per-call override

Template format (``CliKnowledge.command_template``)
---------------------------------------------------
The template is :func:`shlex.split` into tokens.  Three *whole-token*
placeholders are recognized:

* ``{prompt}`` — replaced verbatim with the prompt (one token).  Raises
  :class:`AgentRenderError` if referenced but ``prompt`` is ``None``.
* ``{files}`` — expanded to ``len(files)`` tokens, one per file.  Empty
  list collapses to zero tokens.
* ``{cwd}``   — replaced with the working directory; collapses to zero
  tokens when ``cwd`` is ``None``.

In-token interpolation is intentionally **not** supported.  Quoting is the
shell's job; we never pass through one.

Param render rules (``ParamSpec.render``)
-----------------------------------------
* ``""`` or ``None`` → metadata-only param, never appears in argv.
* ``ParamSpec.type == "bool"``       → ``render`` must be a ``dict`` like
  ``{"true": "--git", "false": "--no-git"}``.  Missing keys produce zero
  tokens.  The flag string is ``shlex.split`` so multi-token flags
  (``"--option arg"``) work.
* ``ParamSpec.type in {"str", "int"}`` → ``render`` is a string template;
  it is ``shlex.split`` first, then ``{value}`` is replaced inside each
  resulting token.
* ``ParamSpec.type == "list[str]"``  → same template, applied per element.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Any

from unlimited_mcp.config.schema import Config, Knowledge, ParamSpec


class AgentRenderError(ValueError):
    """Raised when an agent invocation cannot be rendered to argv."""


@dataclass(frozen=True)
class CLIAgent:
    """Resolved agent definition: CLI binary plus merged parameter values.

    Build via :meth:`from_config`; the constructor is intended for tests
    that need a fully formed instance without going through merge logic.
    """

    name: str
    cli: str
    command_template: str
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
    ) -> list[str]:
        """Render this agent's invocation to a final argv list.

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

        argv = _expand_template(
            self.command_template,
            prompt=prompt,
            files=files or [],
            cwd=cwd,
        )
        for pname, spec in self.params_catalog.items():
            value = params.get(pname)
            if value is None:
                continue
            argv.extend(_render_param(spec, value, agent=self.name, param=pname))
        return argv


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _expand_template(
    template: str,
    *,
    prompt: str | None,
    files: list[str],
    cwd: str | None,
) -> list[str]:
    tokens = shlex.split(template)
    out: list[str] = []
    for tok in tokens:
        if tok == "{prompt}":
            if prompt is None:
                raise AgentRenderError(
                    f"Template references {{prompt}} but no prompt was provided "
                    f"(template: {template!r})."
                )
            out.append(prompt)
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
