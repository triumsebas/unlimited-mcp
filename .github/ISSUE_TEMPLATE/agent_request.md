---
name: New agent CLI
about: Request support for a new agent CLI (or submit a knowledge.yaml entry)
labels: agent-support
---

**Agent name and URL**
e.g. [aider](https://aider.chat)

**Install command**
```
pip install <agent>
```

**Typical invocation**
How does the CLI accept a prompt? e.g.:
```bash
agent-cli --model gpt-4o "refactor this function: ..."
# or via stdin:
echo "prompt" | agent-cli
```

**Key parameters**
List any useful flags (model, provider, context window, etc.)

**Why it's useful**
What makes this agent worth adding? Any specific use case or cost advantage?
