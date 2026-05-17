## Pre-task clarification protocol

This is the VERY FIRST thing you do — before reading more than the minimum
code needed to know what to ask, before any planning, before any edits.
Resolve ambiguity through files, not assumptions.

**Rules:**
- Write ALL your questions for a round at once in a single file — do not ask one at a time.
- Decide what to ask quickly. Do not deep-dive the whole repo before round 001;
  a short scan to identify the real ambiguities is enough.
- If you have NO questions (the task is unambiguous), you MUST still write
  `round_001_questions.json` with an empty array `[]` and then proceed
  immediately. This is the signal that you will not ask — never skip it.
- Only use a second round if the first answers revealed something genuinely unexpected.
- Do not assume. Do not invent answers.
- If you receive an answer containing "STOP", proceed immediately with what you know.
- Only ask what would actually change your implementation.
- Maximum $max_rounds rounds. Total wait budget: ${max_total_seconds}s.

**Protocol for round N (format N as a zero-padded 3-digit number: 001, 002, ...):**

Step 1 — write your questions to:
  $questions_dir/round_NNN_questions.json
  Format: [{"id": 1, "question": "...", "options": ["A: ...", "B: ..."], "why": "one line on why this changes the implementation"}, ...]
  If you have no questions, write exactly: []
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
