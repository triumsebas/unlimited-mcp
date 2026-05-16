# Skill: delegate

Opinionated wrappers around the `unlimited-mcp` MCP tools for common
delegation patterns.  Requires the `unlimited-mcp` server to be connected.

---

## Queue selection: `local` · `ts` · `ts_serial`

| Scenario | `queue=` | Why |
|---|---|---|
| Quick command, analysis, < 30s | `"local"` (default) | In-process thread, no ts dependency |
| Independent tasks, run in parallel | `"ts"` | Up to 4 simultaneous jobs; durable across restarts |
| Ordered pipeline (step 2 needs step 1 done) | `"ts_serial"` | Strictly 1 job at a time; enqueue all upfront |
| Any task expected to take > 1 min | `"ts"` or `"ts_serial"` | Don't block context; recoverable from inbox |

**Default rule: if the task is expected to take more than 2 minutes, use `"ts"` or `"ts_serial"`. No exceptions.**

- Multiple independent tasks, or single long task → `"ts"`.
- Ordered pipeline where steps don't depend on each other's output → `"ts_serial"`.
- Steps where you must read the result before deciding the next step → poll with `"local"`.
- Quick commands (< 2 min) you'll poll in the same turn → `"local"`.

When in doubt about duration, default to `"ts"`. The cost of going background unnecessarily is low; blocking the context window on a slow task is not.

---

## delegate.now — sync delegation (small tasks)

For quick tasks (< 30s) where you want to wait for the result in the same turn.

```
1. add_allowed_root(cwd)
2. result = delegate_to_agent(agent_name, prompt=prompt, cwd=cwd,
                              workspace='safe_dev', queue='local')
3. Poll: while result.status == 'running': result = get_job_result(result.job_id)
4. Return result.summary and result.branch (if applicable)
```

Use when: task is < 30s, you need the diff inline to review.
Do NOT use for agent delegation (coding tasks always use `queue="ts"`).

---

## delegate.fire_and_forget — background delegation (long tasks)

For tasks expected to take minutes or when you want to continue working.
Uses `queue="ts"` so the job survives MCP restarts and appears in the inbox.

```
1. add_allowed_root(cwd)
2. job = submit_task(agent_name=agent_name, prompt=prompt, cwd=cwd,
                     queue='ts', tag=session_tag)
3. Tell user: "Job submitted: {job.job_id}. I'll check back when it's done."
4. Return immediately — check list_jobs() inbox in future turns.
```

Use when: any agent delegation, task > 1 min, or multiple parallel tasks.
Pass a consistent `tag` (e.g. today's date or a task name) to recover the
job in a new session via `list_jobs(tag=...)`.

---

## delegate.await — fire-and-forget then await on request

```
1. add_allowed_root(cwd)
2. job = submit_task(agent_name=agent_name, prompt=prompt, cwd=cwd,
                     queue='ts', idempotency_key=key)
3. When user asks for results: result = get_job_result(job.job_id)
4. If result.status == 'running': tell user it's still running.
5. If result.status == 'completed': show result.summary and result.diff_ref.
6. If result.status == 'failed': show result.error and result.summary.
```

---

## Session recovery (new session, jobs still running)

If you resume and need to find in-flight or unread jobs:

```
1. list_jobs()                         # inbox: active + unseen terminal
2. list_jobs(tag='my-session-tag')     # scope to a specific session
3. get_job_result(job_id)              # marks terminal job as seen
```

---

## Workspace selection guide

| Task type | workspace preset | queue |
|---|---|---|
| Write code in a repo (independent tasks) | `safe_dev` | `"ts"` |
| Write code in a repo (ordered pipeline) | `safe_dev` | `"ts_serial"` |
| Quick in-place edit | `quick_edit` | `"local"` |
| Read/analyse only | `read_only` | `"local"` |
| Shell commands (no repo) | `sysops_local` | `"local"` |

---

## run_command vs run_shell — cuándo usar qué

`run_command` recibe un argv (lista). Usa esto para cualquier comando conocido:
`run_command(argv=["git", "log", "--oneline", "-10"])`.

`run_shell` recibe un script string y lo pasa a bash/sh. Úsalo **solo** cuando
necesites características de shell que argv no puede expresar:

| Necesitas | Usa |
|---|---|
| Un comando simple | `run_command` |
| Pipes: `grep ERROR log \| sort \| uniq -c` | `run_shell` |
| Redirecciones: `cmd > out.txt 2>&1` | `run_shell` |
| Loops: `for f in *.log; do ...; done` | `run_shell` |
| Expansiones: `$(date)`, `*.log`, `${VAR:-default}` | `run_shell` |
| Pasos encadenados: `make && ./test.sh \|\| notify` | `run_shell` |

`run_shell` siempre es `safety_class=mutating` — no hay clasificación estática.
El `cwd` debe estar en `allowed_roots` igualmente.
Hay que pasar `i_understand_this_runs_a_shell_script=True` para confirmar la intención.

```python
run_shell(
    script="grep ERROR app.log | sort | uniq -c | sort -rn | head -20",
    i_understand_this_runs_a_shell_script=True,
    cwd="/path/to/project",
)
```

---

## run_and_summarize vs smolagents — cuándo usar qué

Ambos pueden ejecutar algo y devolverte un resumen sin que el output bruto pase por el contexto de Claude. La diferencia es el nivel de complejidad que soportan.

| Criterio | `run_and_summarize` | smolagents |
|---|---|---|
| Un solo comando de shell | ✓ ideal | ✓ (más overhead) |
| Procesar/transformar la salida con Python | ✗ | ✓ |
| Script antes o después del comando | ✗ | ✓ |
| Múltiples pasos encadenados | ✗ | ✓ |
| Operaciones con ficheros | ✗ | ✓ |
| Cálculos o filtros sobre los datos | ✗ | ✓ |

**Regla de decisión:**

- Usa `run_and_summarize` cuando la tarea es literalmente un solo comando conocido y solo necesitas que alguien lo lea y te lo resuma. Es el camino más ligero: sin agente, sin LLM de coding, respuesta inmediata.
- Usa `delegate_to_agent(agent="smolagents_opencode", ...)` cuando necesitas lógica Python, encadenar pasos, transformar datos antes/después, o cuando el "comando" es en realidad un problema que el agente debe resolver eligiendo cómo hacerlo.

smolagents es un superset de `run_and_summarize` para cualquier cosa que requiera computación. El único motivo para preferir `run_and_summarize` es la simplicidad y la latencia: no hay coste de arranque de agente.

---

## clarify_rounds — cuando dejar que el agente pregunte antes de arrancar

Algunos agentes se benefician de hacer preguntas de diseño antes de comenzar en
lugar de asumir cosas que obligarían a reescribir el trabajo.  Pasa
`clarify_rounds=N` a `delegate_to_agent` para habilitarlo.

```python
delegate_to_agent(
    agent_name='opencode_kimi',
    prompt='add notifications to the platform',
    clarify_rounds=1,   # 0 = sin Q&A (default); 1-5 = hasta N rondas batch
    cwd='/path/to/repo',
    workspace='safe_dev',
    queue='ts',
)
```

El agente escribe **todas sus preguntas de golpe** en un fichero por ronda,
espera las respuestas (máx. 300 s en total) y luego trabaja.  Si agota el tiempo
sale con código 2; inspecciona el job dir para ver la pregunta pendiente y usa
`resume_agent_task` para relanzar con el contexto inyectado.

**Usa `clarify_rounds >= 1` solo cuando SE CUMPLAN TODAS estas condiciones:**

- Tarea de diseño o planificación (arquitectura, esquema de BD, superficie de API, elección de tecnología)
- O la tarea es tan larga que unas suposiciones erróneas desperdiciarían tiempo significativo

**Usa `clarify_rounds=0` (default) cuando SE CUMPLA CUALQUIERA de estas:**

- Ejecución de comandos o tarea administrativa (sin decisiones de diseño)
- La tarea es suficientemente corta como para que sea más barato relanzarla que invertir tiempo en Q&A
- El prompt ya nombra ficheros, funciones o criterios de aceptación concretos

Límites: 5 rondas máximo, 300 s de espera total.

---

## Before any task checklist

1. `list_capabilities()` — confirm the target agent is configured.
2. `add_allowed_root(cwd)` — grant the agent access to the repo.
3. If agent not configured: `lookup_agent_cli(cli)` → `add_agent(...)`.

---

## Orchestrator hard constraints

These apply to YOU (the orchestrator) at all times — no exceptions:

- **Never run bash commands on remote hosts** to test, fix, or review code.
- **Never fix code yourself** — always resubmit to the appropriate agent.
- **Never take over a timed-out job** — resubmit with more time, don't do it yourself.
- **Local bash is only for:** git operations on the main branch + `gh` CLI for PRs.
- **"Review" means:** read the agent's text output (`result.summary`, `result.raw_output_ref`), not open source files yourself.

---

## Timeout guide

**`timeout_seconds` is execution time only** — it starts when the worker
actually runs the command, not when the job was submitted or was waiting in
the ts queue. If the MCP server shuts down mid-job, ts keeps the worker alive
and the timeout continues counting normally.

Always overestimate. A generous timeout has no cost if the job finishes early;
a timeout that's too short kills work in progress with no way to resume.

**Always compute `timeout_seconds` from the agent's `speed_tier` before submitting.**

Step 1 — estimate how long the task would take for Claude (baseline).
Step 2 — look up the agent's `speed_tier` from `list_capabilities()`.
Step 3 — apply the multiplier from this table:

| `speed_tier` | Multiplier | Typical backend |
|---|---|---|
| `fast` | 1× | Claude or equivalent |
| `acceptable` | 2–3× | API-backed LLM (deepseek, qwen, gemini…) |
| `slow` | 10–20× | Local GPU (MLX, llama.cpp, consumer card) |
| `unusable` | 50×+ | Local CPU — only for tiny tasks |

Step 4 — add Q&A budget when `clarify_rounds > 0`:
```
timeout_seconds = (claude_estimate × multiplier) + (clarify_rounds × max_total_seconds)
```

Add extra margin for test/retry loops — agents often run tests, hit failures,
and retry before finishing. When in doubt, use the next tier up.

| Task type | Claude | `acceptable` (3×) | `slow` (15×) |
|---|---|---|---|
| Docstrings / quick refactor | 450 s | 1 350 s | 6 750 s |
| New feature / test suite | 900 s | 2 700 s | 13 500 s |
| Complex multi-file task | 3 600 s | 10 800 s | 54 000 s |

**Note on local/remote GPU agents (`slow` / `unusable` tiers):** these can take
hours on tasks that Claude would finish in minutes. That is expected and
intentional — the user may have chosen a local model for cost (totally free),
privacy, or experimentation. A long-running local job is not a problem; do not
escalate just because it is slow.

---

## Error recovery — when a job fails or stalls

**The key metric is progress, not time.** Before taking any action on a failed
or timed-out job, read `raw_output_ref` and ask: is the agent making forward
progress, or is it looping on the same error?

### Timeout with visible progress in logs

The timeout was under-dimensioned. Recalculate using the tier table above,
resubmit with a larger value. Do not escalate.

### Timeout with no progress (or same error repeating)

This is the real problem. Work through this escalation ladder in order:

1. **Sharpen the prompt** — add concrete context: specific files, functions,
   error messages, acceptance criteria. Resubmit to the same agent.
2. **Switch to a more capable model** — if the agent still cannot make
   progress after a specific prompt, resubmit the same task to a stronger model.
3. **Rewrite from scratch** — if even the capable model cannot fix it cleanly,
   have it rewrite the affected code from scratch. Some implementations are so
   poorly structured that patching is more expensive than starting over.

Never skip steps: always try a sharper prompt before escalating the model,
and always try an in-place fix before rewriting.

---

## Background monitoring with ScheduleWakeup

By default, after submitting a job you return control to the user immediately.
The job runs in background and the user can ask for its status at any time.

**Use ScheduleWakeup when the user explicitly asks for autonomous follow-up:**
- "when it finishes, review the diff"
- "when done, continue with the next PR"
- "run phases 1, 2 and 3 in sequence"

In those cases, stay active and poll using ScheduleWakeup:

```
1. Submit job → get job_id
2. Tell user: "Job running ({job_id}). I'll follow up when it finishes."
3. ScheduleWakeup(delaySeconds=120, reason="polling job {job_id}")
   # Use 120s as base; adjust to ~25% of expected task duration, max 270s
   # (keep under 300s to stay in the prompt cache window)
4. On wake-up: get_job_result(job_id)
   - still running → ScheduleWakeup again
   - completed/failed → execute the follow-up the user asked for
```

**Do NOT use ScheduleWakeup when:**
- The user just said "submit this" or "delegate this" with no follow-up instruction
- The follow-up is vague ("let me know when done") — just tell the user to ask
- Multiple independent jobs are running — poll all at once, don't chain wakeups per job

**ScheduleWakeup timing:**
- Poll interval = min(270s, max(60s, expected_duration × 0.25))
- If you poll and the job is still running, double the interval (up to 270s)
- Always stay under 300s to avoid paying a prompt-cache miss on every wake-up

---

## Reviewing results

- `result.summary` — plain-English description of what happened (always set).
- `result.branch` — worktree branch with changes (for `safe_dev`).
- `result.diff_ref` — path to the patch file.
- `result.raw_output_ref` — path to full stdout log (read only if needed).
- `result.risk_level` — `low` / `medium` / `high` / `critical`.

---

## Verifying a new coding agent (agents that handle files and shell commands)

Run these three tests before marking an agent as `verified: true` in knowledge.yaml.
All three use `workspace="none"` and `add_allowed_root("/tmp")` first.

**Test 1 — prompt in params (inline)**
```python
delegate_to_agent(agent_name, prompt='Say exactly: "<agent> ok"',
                  workspace="none", timeout_seconds=60)
```
Pass: job completes, stdout contains the expected string.

**Test 2 — prompt via file (large prompt, triggers stdin/file fallback)**
Generate a prompt > 64 KB and delegate it. Confirms the agent's stdin/file
delivery path works when the prompt is too large for an argv token.
```python
large_prompt = 'Say exactly: "<agent> file-prompt OK"\n' + 'x' * 70000
delegate_to_agent(agent_name, prompt=large_prompt,
                  workspace="none", timeout_seconds=60)
```
Pass: agent responds correctly despite the large input.

**Test 3 — file write + delete in /tmp (confirms no skip-permissions needed)**
```python
delegate_to_agent(agent_name,
    prompt='Create /tmp/<agent>_test.txt with content "<agent>-write OK", '
           'then delete it, then say "<agent> file-write OK"',
    workspace="none", timeout_seconds=90)
```
Pass: agent creates and deletes the file without prompting for confirmation.
Fail: agent hangs waiting for a permission prompt → add the equivalent of
`--dangerously-skip-permissions` / `--yolo` to the `command_template` in
knowledge.yaml, or set it as a bool param defaulting to true.
