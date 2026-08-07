---
description: Force delegation to DeepSeek sub-agent (bypasses Claude's auto-decision). Usage: /ds <task description> or /ds audit: <task description> for read-only analysis mode.
---

# /ds — Delegate to DeepSeek

Force delegate the following task to DeepSeek for processing, bypassing Claude's auto-decision ("should delegate / should not").

## What You Must Do

1. Determine mode: if `$ARGUMENTS` starts with `audit:` (case-insensitive), strip that prefix and use `mode="audit"` (read-only, structured findings — see the skill's audit-mode section). Otherwise use `mode="execute"` (default).

2. Prepare `task` and `context` following the `delegate-to-deepseek` skill guidelines:
   - Use Glob / LS to collect involved file paths
   - Summarize project conventions (naming rules, output schema, boundaries)
   - Specify success criteria (execute mode) or what counts as a finding (audit mode)

3. Call `mcp__deepseek__delegate_to_deepseek` tool, passing the user's request as task:

```
User input: $ARGUMENTS
```

4. **Must verify** after the tool returns:
   - execute mode: Read sample output files, check count/schema sanity
   - audit mode: spot-check at least one "confirmed" finding by reading the file yourself before acting on it
   - On failure, handle per the skill's fallback strategy

## What NOT to Do

- ❌ Don't ask the user "are you sure?" before calling — typing `/ds` is already an explicit instruction
- ❌ Don't give up immediately when the tool returns ERROR — retry or take over per the skill's fallback strategy
- ❌ Don't put API keys / credentials into task / context