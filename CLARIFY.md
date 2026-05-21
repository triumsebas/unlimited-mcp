# Clarification rounds — how worker Q&A works

`clarify_rounds` lets a delegated worker ask the orchestrator questions, or
propose options, **before** it commits to the work. It is a file-based Q&A
protocol injected automatically into the worker's prompt when you pass
`clarify_rounds > 0` to `delegate_to_agent`.

> **It is optional.** Mechanical tasks with a fully-specified prompt don't need
> it — leave it at `0`. But for ambiguous, exploratory, or design work, a round
> or two of Q&A prevents costly wrong assumptions and usually produces better
> results. You answer in real time with `answer_worker_questions`; the worker
> reads your answers from disk and continues the same session.

The canonical tool reference (outcomes table, timeouts, the fast-path after the
last round) lives in [AGENTS.md](AGENTS.md) under *clarify_rounds*. This file
shows the **logic** through three representative uses.

---

## 1. Clarify the understanding of the task, then start

The simplest case: the request has gaps the worker can't resolve without
guessing. It asks a couple of questions up front, gets answers, and proceeds.

```
orchestrator → delegate_to_agent(prompt="add login", clarify_rounds=1)
worker  round 1 → "Which session store? [A: Redis  B: DB]"  /  "Hash algo? [argon2id / bcrypt]"
orchestrator → answer_worker_questions(round=1, [A: Redis, argon2id])
worker          → builds it with those decisions, done
```

The questions come straight from the prompt's ambiguity — no exploration needed.

---

## 2. The doubt only appears after looking at the plan or the code

Some doubts can't be known up front — they surface once the worker reads the
existing plan or code. The worker explores first, *then* asks a question that is
informed by what it found.

```
orchestrator → delegate_to_agent(prompt="add feature Y to this repo",
                                  clarify_rounds=2, cwd=repo)
worker          → reads the repo / the existing design first
worker  round 1 → "Module Z already handles part of this — extend it,
                   or replace it? [A: extend  B: replace]"
orchestrator → answer_worker_questions(round=1, [A: extend])
worker          → implements against the real codebase, done
```

The value here: the worker grounds its question in the actual code, so you're
deciding on a real fork, not a hypothetical one.

---

## 3. Propose architecture/plan options based on what already exists

Here the task *is* to decide a direction. The worker reads the codebase (or
researches as needed), then uses a round to present a few viable options with
their trade-offs and asks you to pick. You commit the direction; it writes the
detailed plan.

```
orchestrator → delegate_to_agent(prompt="propose options for adding caching
                                  to this codebase", clarify_rounds=1, cwd=repo)
worker          → reads the code, weighs approaches
worker  round 1 → "Approach? [A: in-process LRU  B: Redis  C: HTTP/reverse-proxy]"
                   each option carries a one-line trade-off
orchestrator → answer_worker_questions(round=1, [B: Redis], reasoning="multi-instance")
worker          → produces the full plan for the chosen approach, done
```

This is where Q&A pays the most: **the worker does the code-reading and the
research, saving the orchestrator from spending its own context/limits on it.**
The orchestrator only makes the decision.

---

## Fallback — when something fails or the worker breaks the protocol

The protocol relies on the worker following injected instructions, and models
don't always comply — one may skip the question file entirely, malform the
JSON, or keep asking past the rounds you granted. When that happens you are not
stuck: the orchestrator can **relaunch the task with the previous Q&A history
re-stated in plain prompt text**, via `resume_agent_task`.

```
worker          → times out, ignores the file protocol, or finishes without
                  doing the work
orchestrator → cancel_job(job_id)            # if still running
orchestrator → resume_agent_task(job_id, extra_context="<missing decision>")
                  # original prompt + full Q&A history are injected automatically
new worker      → continues from the decisions already made, no re-asking
```

Because the history is replayed as ordinary prompt text, this also rescues a
model that mishandled the question files in the first place — it never has to
touch the file protocol again.
