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


# ---------------------------------------------------------------------------
# Hosts
# ---------------------------------------------------------------------------


class _HostBase(_Strict):
    allowed_roots: list[str] | None = None
    deny_paths: list[str] | None = None
    default_safety_policy: DefaultSafetyPolicy | None = None
    allow_shell_like_argv: bool | None = None


class LocalHostConfig(_HostBase):
    type: Literal["local"] = "local"


class SshHostConfig(_HostBase):
    type: Literal["ssh"]
    user: str
    host: str
    port: int = 22


HostConfig = Annotated[
    LocalHostConfig | SshHostConfig,
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


class ProviderConfig(_Strict):
    type: Literal["openai_compat", "ollama", "anthropic"]
    model: str
    base_url: str | None = None
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
    workspace: WorkspacePresetName | WorkspaceSpec | None = None


# ---------------------------------------------------------------------------
# Queues — phase 1 ships only an in-process backend (``socket: null``);
# phase 2 wires the ``ts`` (task-spooler) backend with a real socket path.
# ---------------------------------------------------------------------------


class QueueConfig(_Strict):
    socket: str | None = None
    slots: int = 1


# ---------------------------------------------------------------------------
# Top-level Config
# ---------------------------------------------------------------------------


class Config(_Strict):
    schema_version: int = 1
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
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
    type: Literal["openai_compat", "ollama", "anthropic"]
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
