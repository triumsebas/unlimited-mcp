# Example prompts

Copy-paste prompts for use with `unlimited-mcp`. You talk to your
**orchestrator** (Claude Code, Codex); it drives the **workers** (the
delegated agents). See the [README](README.md) for setup.

> Contributions welcome — open a PR adding your prompt with a one-line note
> on what it's good for.

---

## Simple examples

Plain-language prompts you can type as-is. They don't name any specific model
or agent — the orchestrator picks from whatever you've configured.

**Delegate a contained coding change:**

```
Use unlimited-mcp to delegate this to a cheap coding agent: add type hints and
docstrings to every public function in src/utils/, then run the tests. Work in
an isolated branch and show me the diff — don't touch anything outside that
folder.
```

**Long job in the background, keep working:**

```
Kick off a background job through unlimited-mcp that runs the full test suite
with coverage and summarizes the failures. Don't block on it — I'll keep
working; let me know when it finishes.
```

**Read-only analysis, no changes:**

```
Have a cheap worker do a read-only pass over this repo: list every TODO/FIXME
and any obviously dead code, grouped by file, as a prioritized list. It must
not modify anything.
```

---

## Tested generator prompts

These are **generator prompts**: you paste them into a frontier orchestrator
(Claude Code / Codex) and it produces the actual per-session orchestration
prompt. They delegate *all* delegation mechanics to the `unlimited-mcp` skill
as the single source of truth, and only encode the project-specific role map
and workflow. They are deliberately tied to specific agents/models because
that is part of what was validated.

### End-to-end remote build (fully delegated, unattended)

A fully tested, end-to-end prompt for running a real remote project from
start to finish — fully delegated and unattended. It represents what the
system is capable of; simpler use cases are always possible.

> **Codex users:** replace `sonnet` with `codex-medium` and `opus` with
> `codex-high` throughout.

```
Generate an orchestration prompt for a new session to build the unlimited-demo repo (https://github.com/triumsebas/unlimited-demo) through Phase 1 complete. The plan is in `local/PLAN.md` (the session reads it after starting). Remote server: `andrew`.

Hard rule for the generated prompt: it must delegate ALL delegation mechanics to the `unlimited-mcp` skill as the single source of truth (Q&A protocol, timeouts, queues, SSH ban, "never code/fix/review by hand"). Do NOT re-encode the mechanics in the prompt — only encode the project-specific role map and workflow below.

Role map:
- Sonnet = orchestrator + reviewer of pro's work (never writes code)
- `opencode_ssh_pro`   = Opus-level: critical, architectural, ambiguous tasks
- `opencode_ssh_flash` = Sonnet-level: mechanical, fully-specified tasks

Workflow the generated prompt must specify:
- Parallelize PRs with no dependencies; bundle sub-tasks into one delegation only when they share context (per the skill's grouping checklist)
- clarify_rounds=2 for pro's architectural/ambiguous tasks; clarify_rounds=1 for flash's mechanical ones. After any clarify_rounds>=1 delegation, make ONE await_worker_questions(job_id) call and branch on outcome
  (questions / no_questions / job_finished / timed_out / wait_expired).
  Never poll get_worker_questions in a loop. Never hand-add Q&A time to
  timeout_seconds (the server auto-extends it).
- Flash work: flash codes → pro reviews → flash fixes → pro re-reviews → Sonnet merges
- Pro work: pro codes → Sonnet reviews locally (read output/branch, no edits) → pro fixes → Sonnet re-reviews → Sonnet merges
- GitHub is the sync bus; remote workers read local/PLAN.md from the repo cloned on andrew; Sonnet merges to main (the only direct code action)

The generated prompt must be generic (no plan details), concise, and readable as documentation of this orchestration pattern.
```

### Serious security audit of a server fleet

Same generator style, for a read-only security audit across a list of
servers — patch/update status, intrusion indicators, hardening review.
Findings only: remediation is proposed, never applied automatically.

> _Pattern not yet field-tested end-to-end — review the generated prompt
> before running it unattended._

> **Codex users:** replace `sonnet` with `codex-medium` and `opus` with
> `codex-high` throughout.

```
Generate an orchestration prompt for a new session that runs a serious security audit across a list of servers. The server list and any site-specific context are in `local/AUDIT_TARGETS.md` (the session reads it after starting).

Hard rule for the generated prompt: it must delegate ALL delegation mechanics to the `unlimited-mcp` skill as the single source of truth (Q&A protocol, timeouts, queues, SSH handling, "never code/fix/review by hand"). Do NOT re-encode the mechanics in the prompt — only encode the role map and workflow below.

ABSOLUTE SAFETY RULE (must appear verbatim and first in the generated prompt): No agent may change the state of ANY target machine in any way. Connect, read, extract, disconnect — nothing else. No writes, no installs, no updates, no config changes, no service restarts, no killing processes, no creating or deleting files, no remediation. Only non-mutating, read-only inspection commands are permitted. Every finding that needs action is written up as a recommendation for a human to review and apply out of band.

Role map:
- Sonnet = orchestrator + consolidator of the final report (never runs remote commands itself)
- `opencode_ssh_flash` = Sonnet-level: mechanical per-server data collection (run the read-only inspection commands and return raw findings)
- `opencode_ssh_pro`   = Opus-level: triage and risk assessment — interpret the collected findings, rate severity, flag likely intrusion or misconfiguration

Audit scope each per-server collection must cover (the generated prompt expands these into concrete read-only commands appropriate to the detected OS):
- Pending OS/package updates and security patch backlog
- Known-vulnerable package versions
- Intrusion indicators: unexpected listening ports, unknown SUID binaries, suspicious cron/systemd units, recent auth failures and successful root logins, modified system binaries
- Hardening posture: SSH config, firewall state, world-writable files, sudo rules, account/password policy
- Unexpected running services and resource anomalies

Workflow the generated prompt must specify:
- One independent collection delegation per server, parallelized (no shared context between servers → separate delegations per the skill's grouping checklist)
- clarify_rounds=1 for flash collection tasks (only to confirm OS/access specifics); clarify_rounds=2 for pro's triage task. After any clarify_rounds>=1 delegation, make ONE await_worker_questions(job_id) call and branch on outcome
  (questions / no_questions / job_finished / timed_out / wait_expired).
  Never poll get_worker_questions in a loop. Never hand-add Q&A time to
  timeout_seconds (the server auto-extends it).
- Per server: flash collects raw findings → pro triages and rates severity → Sonnet aggregates into one consolidated report (per-server section + a fleet-wide risk summary, sorted by severity)
- The only artifact is the written report; no changes are made to any server

The generated prompt must be generic (no real hostnames or site detail), concise, and readable as documentation of this audit pattern.
```

### Intelligence + speed benchmark across agents/models (with vs. without clarify)

Same generator style, for putting several agents/models head-to-head over a
read-only codebase — scored by the orchestrator on quality and timed from
`JobResult`. Shows how `clarify_rounds` lets you measure not just *what* an
agent produces but whether *letting it ask questions* improves the result.
Illustrative example; expand the task battery to taste.

```
Generate an orchestration prompt for a new session that benchmarks several coding agents/models against each other for both intelligence and speed, with you as the judge. The target repo and the task list are in `local/BENCHMARK.md` (read it after starting).

Hard rule: delegate ALL delegation mechanics to the `unlimited-mcp` skill (Q&A protocol, timeouts, queues, "never solve the tasks yourself"). Only encode the benchmark design below.

Read-only rule (first, verbatim): the target repo is read-only; each agent writes only in its own scratch subfolder; git-check the repo before/after each level and revert if touched.

Design:
- agent_1, agent_2, agent_3 = the agents/models under test (same CLI, different models X/Y/Z); the orchestrator only judges.
- A battery of tasks of increasing difficulty (comprehension -> small module+tests -> complex module+tests -> reasoning/bug analysis -> open design with real trade-offs).
- Two conditions per task, same prompt, only clarify_rounds differs: `noask` (0) and `ask` (2). Run each level's cells in parallel; score; advance. Use workspace='none' and a scratch cwd per cell.
- For `ask`, answer questions uniformly and neutrally (same for every agent). To make asking actually matter, at least one task must explicitly require stating doubts + a plan with options before solving.

Score each cell 0-5 on correctness, repo-adherence, completeness, quality, and evidence-of-reading (run tests where possible). Measure work time from JobResult (for `ask`, from the last answer to finish).

Deliverable: a SCOREBOARD per task x agent x condition with scores, times, a ranking, and a short per-agent note on whether being allowed to ask improved its result.

Keep the generated prompt generic, concise, and readable as documentation of this benchmarking pattern.
```
