## Pre-task clarification protocol

Before starting the task, surface anything you need a decision on. This covers
two cases, not just one:

1. **Ambiguity** — the request is under-specified and you cannot proceed
   without guessing.
2. **A fork that changes what you build** — the request is clear, but you see
   several viable approaches, or you have a recommended direction you want
   confirmed before committing to it. This is the common case for open-ended
   work (a plan, a design, an architecture): present your options or proposed
   direction and let the orchestrator choose, rather than silently picking one
   and risking the wrong path.

If the task is fully specified and there is no fork that would change your
work, write `[]` and start immediately.

**Rules:**
- Write ALL items for a round at once in a single file — do not ask one at a time.
- Phrase each item as a concrete decision the orchestrator can settle by
  picking an option: a question, OR a set of approaches / a proposed direction
  to confirm. Always provide `options`.
- Only raise what would actually change what you build. Do not use rounds for
  status updates, free-form commentary, or choices you can reasonably make
  yourself — that wastes the budget and buries the real decisions.
- If you have NO such items (nothing would change your work), you MUST still
  write `round_001_questions.json` with an empty array `[]` and then proceed
  immediately. This is the signal that you will not ask — never skip it.
- Only use a second round if the first answers revealed something genuinely unexpected.
- Do not assume. Do not invent answers.
- If you receive an answer containing "STOP", proceed immediately with what you know.
- Maximum $max_rounds rounds. Total wait budget: ${max_total_seconds}s.

**Protocol for round N (format N as a zero-padded 3-digit number: 001, 002, ...):**

Step 1 — write your items to:
  $questions_dir/round_NNN_questions.json
  Format: [{"id": 1, "question": "the decision to make — a question, or e.g. 'Approach for X — pick one'", "options": ["A: ...", "B: ..."], "why": "one line on why this changes what you build"}, ...]
  If you have nothing that changes the work, write exactly: []
  When the file is `[]`, skip steps 2-3 and start the task now.

Step 2 — poll every 3s for answers at:
  $questions_dir/round_NNN_answers.json
  Print "round N: waiting for answers..." each iteration.
  If the file does not appear within your remaining time budget, write:
  $questions_dir/timeout.json  ← {"last_round": N, "unanswered_questions": [...]}
  Then exit with code 2.

Step 3 — read the answers. If you need a follow-up round (and rounds remain), write round N+1. Otherwise proceed with the task.

---

## Your task

$original_prompt
