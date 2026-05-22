# Copyright 2026 Sebastian Fernandez Alberdi
# SPDX-License-Identifier: Apache-2.0
# Part of unlimited-mcp — https://github.com/triumsebas/unlimited-mcp

"""Pydantic schemas for ``config.yaml`` and ``knowledge.yaml``.

Pure schema definitions — validation only. All filesystem and business
logic lives in service classes (``ConfigStore``, ``KnowledgeStore``,
``SafetyChecker``, ``WorkspaceManager``, ``JobStore``). Models forbid
unknown keys (``extra="forbid"``) so misspellings fail loudly instead of
silently dropping data.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Common literal types (kept identical to ``jobs.result`` deliberately; the
# integration test that closes phase 0 asserts they remain in sync).
# ---------------------------------------------------------------------------

CostTier = Literal[0, 1, 2, 3]
SpeedTier = Literal["unusable", "slow", "acceptable", "fast"]
SafetyClass = Literal["read", "mutating", "dangerous", "unknown"]
DefaultSafetyPolicy = Literal["read_only", "standard", "permissive"]


class _Strict(BaseModel):
    """Forbid unknown keys on every schema in this module."""

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Workspace
# ---------------------------------------------------------------------------

WorkspaceMode = Literal["current", "git_worktree", "temp_copy", "remote_cwd", "none"]
ResultMode = Literal["apply_direct", "return_diff", "leave_branch", "report_only"]
DirtyPolicy = Literal["refuse", "allow", "snapshot", "stash_copy"]

WorkspacePresetName = Literal[
    "safe_dev",
    "quick_edit",
    "read_only",
    "sysops_local",
    "sysops_remote",
    "destructive_apply",
]


class WorkspaceSpec(_Strict):
    """Explicit three-axis workspace spec.

    Callers usually pass a preset name (``"safe_dev"``); the runner expands
    that to a ``WorkspaceSpec`` via ``Knowledge.workspace_presets``.
    """

    mode: WorkspaceMode
    result: ResultMode
    dirty: DirtyPolicy | None = None  # n/a when mode is "none"


# ---------------------------------------------------------------------------
# Safety policy (global). Per-host policy mirrors these fields and overrides
# selectively.
# ---------------------------------------------------------------------------


class SafetyConfig(_Strict):
    allow_shell_like_argv: bool = False
    default_safety_policy: DefaultSafetyPolicy = "standard"
    log_full_shell_scripts: bool = False
    confirm_token_ttl_seconds: int = 300


class QualityGateConfig(_Strict):
    """Post-job lint + type-check on a coding worker's changed files.

    When ``enabled`` and the job ran in a git worktree, the runner detects the
    language of the changed files, auto-fixes formatting, runs the linter and
    type-checker, and attaches a :class:`~unlimited_mcp.jobs.result.QualityGateResult`
    to the JobResult. Non-intrusive: unknown languages and missing tools degrade
    to ``NOTDETECTED`` / ``MISSINGDEP`` rather than failing the job.
    """

    enabled: bool = True


class ClarifyConfig(_Strict):
    """Limits for the worker clarification-rounds protocol.

    When ``delegate_to_agent`` is called with ``clarify_rounds > 0`` the
    agent is given a file-based Q&A preamble before it starts work.  These
    two limits cap how long that phase can run regardless of the caller's
    ``clarify_rounds`` value.
    """

    max_rounds: int = 5
    max_total_seconds: int = 600


# ---------------------------------------------------------------------------
# Hosts
# ---------------------------------------------------------------------------


class _HostBase(_Strict):
    allowed_roots: list[str] | None = None
    deny_paths: list[str] | None = None
    default_safety_policy: DefaultSafetyPolicy | None = None
    allow_shell_like_argv: bool | None = None
    repos_root: str | None = None


class LocalHostConfig(_HostBase):
    type: Literal["local"] = "local"


class SshHostConfig(_HostBase):
    type: Literal["ssh"]
    user: str
    host: str
    port: int = 22
    key_file: str | None = None
    key_passphrase_env: str | None = None
    key_passphrase_keyring: str | None = None
    # Keyring account used for the passphrase lookup. The passphrase belongs to
    # the local private key, not to the remote SSH user, so default to the key
    # file's basename (e.g. "id_rsa") and fall back to the SSH user only when no
    # key_file is set. Lets several hosts share one keychain entry.
    key_passphrase_account: str | None = None
    forward_agent: bool = False


HostConfig = Annotated[
    LocalHostConfig | SshHostConfig,
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


class ProviderConfig(_Strict):
    type: Literal["openai_compat", "ollama", "mlx_lm", "gemini", "groq", "openrouter", "anthropic"]
    model: str
    base_url: str | None = None
    api_key: str | None = None
    api_key_env: str | None = None
    api_base_env: str | None = None
    exec_host: str = "local"
    tags: list[str] = Field(default_factory=list)
    suitable_for: list[str] = Field(default_factory=list)
    not_suitable_reason: str | None = None
    speed_tier: SpeedTier = "acceptable"
    cost_tier: CostTier = 1


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


class AgentConfig(_Strict):
    cli: str  # → look up in Knowledge.clis
    cost_tier: CostTier = 1
    tags: list[str] = Field(default_factory=list)
    exec_host: str = "local"
    queue: str | None = None
    suitable_for: list[str] = Field(default_factory=list)
    not_suitable_reason: str | None = None
    speed_tier: SpeedTier = "acceptable"
    params: dict[str, Any] = Field(default_factory=dict)
    # Per-agent env vars injected into the worker subprocess.  Values may use
    # ${VAR} syntax to reference the server's own environment at call time.
    env_extra: dict[str, str] = Field(default_factory=dict)
    workspace: WorkspacePresetName | WorkspaceSpec | None = None
    supports_clarify: bool = True
    """Whether this agent reliably follows the file-based Q&A clarification
    protocol.  Set to ``False`` for local models or CLIs known to ignore the
    preamble.  When ``False`` and ``clarify_rounds > 0`` is requested, the
    runner skips the Q&A phase and adds a warning to the JobResult."""


# ---------------------------------------------------------------------------
# Queues — phase 1 ships only an in-process backend (``socket: null``);
# phase 2 wires the ``ts`` (task-spooler) backend with a real socket path.
# ---------------------------------------------------------------------------


class QueueConfig(_Strict):
    type: str = "local_ts"
    """Queue backend type.

    ``"local_ts"``   — task-spooler running on this machine (default).
    ``"remote_ts"``  — task-spooler running on a remote SSH host.
    """
    socket: str | None = None
    """Override the TS_SOCKET path.

    For ``local_ts``: path on this machine, defaults to ``state_dir/<name>.sock``.
    For ``remote_ts``: path on the remote machine passed as ``TS_SOCKET``.
    """
    slots: int = 1
    """Maximum number of simultaneous jobs for this queue (``ts -S <n>``)."""
    host: str | None = None
    """SSH host name (key in ``hosts:``) — required for ``remote_ts`` queues."""


# ---------------------------------------------------------------------------
# Top-level Config
# ---------------------------------------------------------------------------


class Config(_Strict):
    schema_version: int = 1
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    clarify: ClarifyConfig = Field(default_factory=ClarifyConfig)
    quality_gate: QualityGateConfig = Field(default_factory=QualityGateConfig)
    allowed_roots: list[str] = Field(default_factory=list)
    deny_paths: list[str] = Field(default_factory=list)
    hosts: dict[str, HostConfig] = Field(default_factory=dict)
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    agents: dict[str, AgentConfig] = Field(default_factory=dict)
    queues: dict[str, QueueConfig] = Field(default_factory=dict)


# ===========================================================================
# Knowledge models
# ===========================================================================


class ParamSpec(_Strict):
    """One configurable parameter exposed by a CLI agent.

    ``render`` is intentionally typed as ``Any`` so the same model accepts:
    a string template like ``"--model {value!q}"``; a bool→string mapping
    like ``{"true": "--yes", "false": ""}``; or an empty string when the
    param is consumed by the runner (e.g. ``cwd``) rather than rendered
    into argv.
    """

    type: Literal["bool", "str", "int", "list[str]"]
    default: Any = None
    required: bool = False
    render: Any = ""


class CliKnowledge(_Strict):
    docs_url: str | None = None
    install_hint: str | None = None
    command_template: str
    stdin_command_template: str | None = None
    prompt_via: str = "arg"
    """How the prompt is delivered to this CLI.

    ``"arg"``
        Inline in argv via the ``{prompt}`` token in *command_template*.
        Default for most CLIs.  Subject to OS ``ARG_MAX`` (~1 MB on macOS).

    ``"stdin"``
        Written to ``job_dir/stdin.txt`` and piped as stdin.  The template
        must NOT contain ``{prompt}``.  Use for CLIs like ``opencode run``,
        ``claude``, ``codex exec -`` that read the task from stdin.

    ``"file"``
        Written to ``job_dir/prompt.txt`` and the path is substituted for
        the ``{prompt_file}`` token in *command_template*.  Use for CLIs
        that accept a prompt file via a flag (e.g. ``agent -f {prompt_file}``).

    ``"arg_with_stdin_fallback"``
        Uses ``"arg"`` for prompts ≤ 64 KB; falls back to ``"stdin"`` for
        larger prompts.  The CLI must support stdin in that case.
    """
    params: dict[str, ParamSpec] = Field(default_factory=dict)
    knowledge_required: list[str] = Field(default_factory=list)
    verified: bool = False
    verified_version: str | None = None


class FlagPattern(_Strict):
    match: list[str]
    escalates_to: SafetyClass | None = None
    demotes_to: SafetyClass | None = None


PathValueStyle = Literal["combined", "separate", "separate_or_equals"]


class PathFlag(_Strict):
    flag: str
    style: PathValueStyle = "separate_or_equals"


class ToolKnowledge(_Strict):
    safety_class: SafetyClass = "unknown"
    docs_url: str | None = None
    flag_patterns: list[FlagPattern] = Field(default_factory=list)
    path_flags: list[PathFlag] = Field(default_factory=list)


class ProviderKnowledge(_Strict):
    type: Literal["openai_compat", "ollama", "mlx_lm", "gemini", "groq", "openrouter", "anthropic"]
    docs_url: str | None = None
    default_base_url: str | None = None
    well_known_models: list[str] = Field(default_factory=list)


class OrchestratorKnowledge(_Strict):
    companion_file: str
    tool_description_style: str = "prose-with-example"
    long_lived_session: bool = True
    sandbox: str = "orchestrator-controlled"
    restart_semantics: str = "stdio-reconnects"
    notes: list[str] = Field(default_factory=list)


class ShellLikeArgvSpec(_Strict):
    """Inline-code flags for a given interpreter."""

    inline_flags: list[str] = Field(default_factory=list)


class WorkspacePreset(_Strict):
    mode: WorkspaceMode
    result: ResultMode
    dirty: DirtyPolicy | None = None


class HostTemplate(_Strict):
    description: str | None = None
    default_safety_policy: DefaultSafetyPolicy = "standard"
    suggested_allowed_roots: list[str] = Field(default_factory=list)
    suggested_deny_paths: list[str] = Field(default_factory=list)


class ProbeKnowledge(_Strict):
    type: Literal["file_hash", "pkg_version", "sysctl", "command"]
    description: str | None = None


class Knowledge(_Strict):
    schema_version: int = 1
    orchestrators: dict[str, OrchestratorKnowledge] = Field(default_factory=dict)
    clis: dict[str, CliKnowledge] = Field(default_factory=dict)
    providers: dict[str, ProviderKnowledge] = Field(default_factory=dict)
    runners: dict[str, dict[str, Any]] = Field(default_factory=dict)
    tools: dict[str, ToolKnowledge] = Field(default_factory=dict)
    shell_like_argv: dict[str, ShellLikeArgvSpec] = Field(default_factory=dict)
    workspace_presets: dict[str, WorkspacePreset] = Field(default_factory=dict)
    host_templates: dict[str, HostTemplate] = Field(default_factory=dict)
    probes: dict[str, ProbeKnowledge] = Field(default_factory=dict)
