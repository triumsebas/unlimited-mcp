# Example prompts & delegation flows

Copy-paste prompts and orchestration patterns for use with `unlimited-mcp`.
You talk to your **orchestrator** (Claude Code, Codex); it drives the
**workers** (the delegated agents). See the [README](README.md) for setup.

> More battle-tested prompts will be added over time. Contributions welcome —
> open a PR adding your prompt with a one-line note on what it's good for.

---

## Tested prompts

These are **generator prompts**: you paste them into a frontier orchestrator
(Claude Code / Codex) and it produces the actual per-session orchestration
prompt. They delegate *all* delegation mechanics to the `unlimited-mcp` skill
as the single source of truth, and only encode the project-specific role map
and workflow.

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

Safety rule (must appear in the generated prompt): the audit is READ-ONLY. Workers may only run non-mutating inspection commands. They must NEVER apply updates, change configuration, kill processes, or remediate anything. All remediation is written up as recommendations for the human to review and approve out of band.

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

---

## Pattern: AI-assisted software development

The delegation pattern that motivated this project. You define the strategy in
plain language; the orchestrator executes it.

**Design phase** (expensive model — do it once, do it right):
```
Claude Opus: Review requirements, design architecture, write detailed plan
```

**Implementation phase** (cheap model — runs for hours, burns no subscription):
```
Sonnet orchestrates → opencode(DeepSeek Flash) implements feature by feature
                    → opencode(DeepSeek Pro)  reviews critical sections
                    → delegate_to_agent() for each task, get_job_result() to verify
```

**Review phase** (expensive model — spot checks, final approval):
```
Claude Opus/Sonnet: Review diffs, merge approved branches, tag release
```

You decide which agent handles what, which model reviews, and when the
expensive orchestrator steps back in. Each task runs in an isolated git
worktree, so your main branch is untouched until you approve and merge.

Common things to offload: boilerplate, tests and docstrings; refactors with
clear acceptance criteria; lint/type fixes across large codebases; database
migrations; patch review.

---

## Pattern: systems operations

Automate infrastructure tasks that require many calls or long runtimes:

```python
# Audit multiple servers (Phase 3: SSH)
for server in server_list:
    submit_task(
        agent_name='sysops_agent',
        prompt=f'Audit {server}: check disk, running services, failed systemd units',
        queue='ts'  # background, survives session close
    )

# Parallel updates
submit_task(argv=['apt-get', 'update', '-y'], cwd='/mnt/server1', queue='ts')
submit_task(argv=['apt-get', 'update', '-y'], cwd='/mnt/server2', queue='ts')
```

Good for: auditing many servers for compliance/security, rolling out config
changes across a fleet, long batch jobs (data processing, ML pipelines), or
any automation that would block your terminal for minutes or hours.
