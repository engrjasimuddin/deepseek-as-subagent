---
name: delegate-to-deepseek
description: By default delegate medium-or-below, batch, repetitive, or mechanical tasks as complete logical units to DeepSeek (mode="execute"), with independent verification by the main Agent. Suitable for batch file edits, log scanning, translation, ETL, scripting, testing, documentation, CRUD, single-domain refactoring, single component or single endpoint. For read-heavy multi-file analysis/audits, delegate with mode="audit" instead — DeepSeek is read-only there and returns structured findings for the main Agent to triage and act on, rather than doing the whole task itself. Delegation timing, source-reading restrictions, task granularity, and self-handling lists are default heuristics; the main Agent may override these defaults when confident based on current context. User explicit instructions, security, permissions, privacy boundaries, and post-delegation verification cannot be overridden. Skip when DEEPSEEK_MODE=off.
---

# delegate-to-deepseek — Guidelines for Main Agent Delegating to DeepSeek

> "Main Agent" refers to the upper-level agent responsible for decision-making, integration, and verification.

## 🧭 Highest Priority: Main Agent High-Confidence Discretion

Except for the "Non-overridable Items" below, the "delegate by default / do it yourself / no reading before delegation / complete logical unit" guidelines in this document are cost-optimization heuristics, not absolute restrictions. The Main Agent can override them when confident based on already-grasped context, change scope, failure cost, and verification methods, including:

- Handling tasks itself that would otherwise be delegated by default, or delegating tasks it would normally handle
- Delegating after reading source code, or taking over after delegating
- Adjusting granularity based on actual dependencies, unconstrained by fixed file counts, line counts, or task types

When exercising discretion, briefly state the specific rationale; "it's convenient" or "it feels right" do not count as rationale.

**Non-overridable Items**:

- User explicitly requests delegation / no delegation, specifies executor or execution method
- Permission, security, privacy, sensitive information, and unauthorized write boundaries
- Delegation results must be independently verified by the Main Agent; the Main Agent is responsible for recovery on failure
- When env var `DEEPSEEK_MODE=off`, this skill is immediately disabled

## 🚀 Core Philosophy: Delegate When Possible

DeepSeek v4-pro is already very capable and, under current configuration, is typically cheaper than the Main Agent model. The Main Agent's scarce resource is high-cost model quota, while DeepSeek's scarce resource is mainly API call costs. **Delegate by default**, except for the following scenarios which the Main Agent handles itself by default:

- ❌ Tasks depending on CLAUDE.md / project internal convention docs (DS can't access Main Agent's memory)
- ❌ Cross-domain architecture design / tech selection / ADR (requires Main Agent's comprehensive reasoning)
- ❌ Single-bug root cause analysis on 1-2 known files (reasoning-intensive, Main Agent handles by default) — **but** a multi-file *sweep* ("check all N plugins for this pattern") is a different shape: delegate with `mode="audit"` instead of doing it yourself (see the dedicated section below)
- ❌ Single-file < 200 line tweaks (DS reasoning startup cost > saved Main Agent tokens)
- ❌ User explicitly says "do it yourself / don't delegate"

**All other tasks delegate by default**. Including but not limited to: "write X", "add tests", "fix this lint", "rename Y to Z", "scan logs", "translate this", "implement this endpoint".

## Default Timing: Decide Whether to Delegate Before Main Agent Reads Source Code

Delegation is to **save Main Agent tokens**. If the Main Agent has already Read source files, the source is in the main conversation context and the tokens are already burned. Delegating to DeepSeek afterward means DS has to **read everything again** (it can't access the Main Agent's memory), resulting in **double consumption**:

```
Wrong timing (double cost)              Right timing (net savings)
─────────────────                ──────────────
User proposes task                 User proposes task
    │                                │
    ▼                                ▼
Main Agent Read 50 files — burn 100k  Main Agent Glob scope — burn 500
    │                                │
    ▼                                ▼
"OK, read, this is a DS task"       "Scope clear" → delegate immediately
    │                                │
    ▼                                ▼
Delegate to DS (DS reads 100k)      DS takes over all Read + processing
    │                                │
  ❌ Total = Main 100k +            ✅ Total = Main 500 +
            DS 100k + verify 20k             DS 100k + verify 20k
                                              (save 100k Main Agent tokens)
```

### Tools Available by Default Before Delegation Decision

✅ `Glob` — See how many files, what extensions
✅ `LS` — See directory structure
✅ `Bash` read-only commands — `ls`, `wc -l`, `find . -name`, `du -sh`, `git status`
✅ `WebSearch` / `WebFetch` — Look up external docs / new APIs / error codes (to supplement DS context, billed by Anthropic)

### Tools to Avoid by Default Before Delegation Decision

❌ `Read` — once read, it pollutes context; sunk cost makes delegation uneconomical
❌ `Grep` — same as above, brings matching lines into context

**Decision heuristic**: **Can't decide "should I delegate?"? Default to delegate — DS burning a few thousand extra tokens is minor; Main Agent burning 100k extra is major.**

---

## Difficulty Tiers + Delegation Decision

| Difficulty | Examples | Default |
|---|---|---|
| 🟢 **Simple** | Write hello world / single script, write test cases, supplement docs, single endpoint CRUD, single component | ✅ **Delegate** |
| 🟡 **Medium** | 3-10 file batch changes, single feature implementation (clear spec), complete tests, generate boilerplate, simple refactor, scan logs / ETL | ✅ **Delegate** |
| 🟠 **Upper-Medium** | 10+ file batch, single-domain refactor, performance optimization (data given), i18n extraction, protocol conversion | ✅ **Delegate** (split into batches if needed) |
| 🔴 **Hard** | Cross-domain architecture design, tech selection, ADR, bug root cause analysis, needs deep project conventions | ❌ **Do it yourself** |
| 🌶️ **Tiny** | Single file < 200 lines typo / rename / add comments | ❌ **Do it yourself** (DS overhead not worth it) |

**Simple / Medium / Upper-Medium all delegate by default**. Don't skip delegation because "it sounds simple, I'll just do it" — that saves 5 minutes but burns tens of thousands of Main Agent tokens. (Main Agent can handle itself when highly confident.)

### Decision Fast Track

| User says / sees | Main Agent action |
|---|---|
| "Write X" / "Implement Y" / "Build Z" | Delegate (unless 🔴/🌶️) |
| "Rename / batch change / translate / extract" | Delegate |
| "Tests / docs / boilerplate / lint fix" | Delegate |
| "Why this bug" / "Why did this break" | Do it yourself (reasoning task) |
| "Should I use A or B" | Do it yourself (selection) |
| "Fix a typo / rename a variable" | Do it yourself (tiny, DS overhead not worth it) |
| "Delegate to DS" / `/ds <task>` | Force delegate |
| "Do it yourself" / "Don't delegate" | Force no delegate |

---

## 🔎 `mode="audit"` — Read-Only Recon for Multi-File Analysis

This is a **different shape of delegation** from everything above. `mode="execute"` (the default) hands DeepSeek a task it completes end-to-end, including any edits — Main Agent gets a summary back, not the raw material. `mode="audit"` is the opposite: DeepSeek is **read-only** (Read/Glob/Grep only — no Write/Edit/NotebookEdit/Bash, enforced at the tool-dispatch level, not just a prompt instruction) and returns **structured JSON findings** (`file`, `line`, `summary`, `severity`, `confidence`) instead of a change. Main Agent stays the decision-maker and does the actual editing.

**Why this exists**: some tasks are expensive to *investigate* but cheap to *fix* — a security/consistency sweep across 10-15 files where each individual fix is a few lines, but finding all the occurrences means reading everything. In `mode="execute"`, DeepSeek would either need to also make the edits itself (fine for mechanical batch fixes, risky for anything needing judgment) or Main Agent would have to Read everything itself first anyway to know what to delegate — defeating the point. `mode="audit"` splits it cleanly: DeepSeek does the expensive reading, Main Agent does the cheap judgment-requiring edits.

### When to use `mode="audit"` vs `mode="execute"` vs do-it-yourself

| Task shape | Mode |
|---|---|
| "Scan these 15 plugins for pattern X" / "find every place Y is called without Z" | `mode="audit"` |
| "Fix pattern X in these 15 files" (fix itself is mechanical, no judgment needed) | `mode="execute"` |
| "Why is this one specific bug happening" (already know the file) | Do it yourself |
| "Is this plugin's auth flow secure" (1-2 files you can just Read) | Do it yourself — audit-mode overhead not worth it for a handful of files |

### Using it

```
mcp__deepseek__delegate_to_deepseek(
  task="<what to look for + which files/directories + what counts as a finding>",
  context="<project conventions the audit should check against, known false-positive patterns to skip>",
  mode="audit"
)
```

Findings are capped at 30 (most-severe-first, both by prompt instruction and by a server-side hard truncation — don't assume "no findings" means "clean" if `findings_truncated: true` is set, it means there were more than 30). Each finding has a `confidence` of `"confirmed"` or `"plausible"` — DeepSeek's own self-assessment, not a guarantee.

### After an audit — this replaces the normal "Must Do After Delegation" verification

Since audit mode never touches files, the usual "spot-check the written output" step doesn't apply the same way. Instead:

1. **Triage, don't blindly act.** Treat `"confidence": "confirmed"` findings as high-trust but not infallible; treat `"plausible"` findings as needing your own quick look before you act on them.
2. **Spot-check at least 1-2 "confirmed" findings** by reading the actual file yourself — DeepSeek can be wrong about what "confirmed" means even when instructed not to overclaim.
3. **You make the actual fix.** Audit mode's whole point is that Main Agent does the edit (with full project-context judgment), not DeepSeek. Don't re-delegate the fix in `mode="execute"` unless the fix itself is genuinely mechanical.
4. **If the response has a `NOTE: ... was not valid JSON` suffix**, DeepSeek didn't follow the structured-output format — treat the whole thing as unverified prose and read the raw text yourself before trusting any of it.

---

## 🧩 Delegation Granularity (Default): Complete Logical Units > Fine-Grained Steps

> 💡 This granularity guideline is a default heuristic. Main Agent can adjust granularity (merge/split) based on task understanding; see top-level discretion rules.

**Core counter-intuition**: More granular ≠ cheaper. Over-splitting can be more expensive than not delegating at all.

### The 5 "Anti-Delegation Taxes" (the more you split, the more you lose)

| Tax | Mechanism |
|---|---|
| **Split-planning tax** | Main Agent thinking "how to split / what context / what task" itself burns Main Agent tokens |
| **Context re-read tax** | Files Main Agent reads in one conversation can be referenced later; DS each delegate is a separate process, **same files get re-read N times** |
| **Verification tax** | Each DS completion requires Main Agent to Read samples for verification; more splits → more verification |
| **DS startup fee** | v4-pro thinking mode ~5-10k reasoning tokens per startup; split 10 times = 50-100k startup fee |
| **Fragment rework tax** | Sub-tasks lack global perspective, outputs are inconsistent; rework means re-split + re-do + re-verify all over again |

### Math Intuition (in Main Agent equivalent cost)

| Strategy | Total Cost |
|---|---|
| Main Agent does it all | 1.0X |
| Delegate 1 complete logical unit to DS | ~0.13X ✅ **Save 87%** |
| Delegate 5 sub-tasks (split by steps) | ~0.50X Save 50% |
| Delegate 10 micro-tasks | ~0.95X ❌ Almost no savings |
| Delegate 20 fine tasks | ~1.88X ❌ **More expensive than not delegating** |

### The Truly Cost-Saving Pattern

✅ **Delegate "complete logical units"**, DS internal loop runs 10-30 turns in one shot:
- "Implement this feature end-to-end" → 1 delegate, DS runs its own Read/Write/Test cycle
- "Batch edit these 50 files" → 1 delegate, DS runs file loop internally
- "Scan entire logs/ directory and extract error stacks" → 1 delegate, DS traverses all log files

❌ **Don't** split "feature step 1, step 2, step 3" into separate delegations:
- Each step requires Main Agent split + verify + DS re-read context, hitting all 5 taxes
- Better to let DS take over the entire feature in one shot

### Splitting Principles (Main Agent handles this part)

Main Agent's job is **"identify logical units + design interfaces + integrate"**, DS's job is **"complete implementation of units"**:

1. **Identify**: What is a "complete logical unit"? Clear interface, independently verifiable, self-contained (doesn't depend on another DS task's output)
2. **Design**: Input/output format between units (schema / file paths), Main Agent defines, DS implements
3. **Integrate**: After DS finishes, Main Agent strings them together, with final glue code / verification if needed

### One Test: Should I Split Further?

Before delegating, ask yourself: **"Could I give this sub-task to a 1-week newcomer with all context at once and have them complete it independently?"**
- Yes → Safe to delegate to DS
- No (needs to come back to ask questions / check prior results) → **Don't split it out**, merge it with the preceding unit into a larger unit

## 💰 Token Economics (Keeping the Main Agent Accountable)

### The Formula for Genuinely Saving Money via Delegation

```
Net savings = (Tokens Main Agent would burn if not delegating)
            - (Tokens Main Agent burns preparing task + verifying output)
            - (Tokens DeepSeek burns × price conversion factor ≈ 0.1x)
```

The 0.1x conversion factor means **DS burning 10k tokens costs the same as Main Agent burning 1k tokens**. So even for not-so-large tasks, delegation is often worthwhile.

### Counter-Intuitive but Common "Should Delegate" Signals

- "I can finish this in 5 minutes myself" → **If it requires reading files / writing 50+ lines**, those 5 minutes still burn 10-20k Main Agent tokens; delegating to DS is cheaper
- "DeepSeek will probably iterate a few rounds" → Let it iterate, it's cheap
- "The code is small, no need to delegate" → Check if it's < 200 lines **and no dependency reads**. Need to read several files before starting? Delegate

### The Only "Should NOT Delegate" Signal to Watch For

- DS reasoning tokens have a large startup cost (v4-pro thinking mode): **even just writing hello world burns ~8k tokens**
- So "almost no Read, changes < 200 lines" → Main Agent can do it in 5 lines, cheaper than DS 8k tokens

---

## Default Preparation Before Delegation (Avoid Context Loss)

DeepSeek as a sub-agent **cannot see** the main conversation history, CLAUDE.md, project internal convention docs, Main Agent memory, **and cannot access the internet**. All the context it needs (including external materials) **must** be passed via `task` and `context` parameters.

Before calling, **by default only use Glob / LS / read-only Bash** (avoid Read as much as possible) to collect:

```
1. Use Glob to list involved file paths (if any), pass to DeepSeek
2. Summarize project conventions (from Main Agent's own memory, don't Read CLAUDE.md):
   - Naming rules, output schema, boundaries
   - Tech stack (language version, framework, key dependencies)
3. Clarify success criteria:
   - What should be generated / modified
   - Verifiable signal of completion ("write a fastapi endpoint, curl localhost/x returns 200")
```

## 🌐 Supplement DS with External Knowledge via Main Agent's Own WebSearch / WebFetch

**Key insight**: DeepSeek sub-agent **cannot access the internet** (sandbox blocks curl/wget, and no web tools are exposed). Main Agent can use the current environment's `WebSearch` / `WebFetch` or equivalent external resource tools.

**Pre-delegation rule**: If the task requires external knowledge the Main Agent isn't familiar with, **Main Agent should use WebSearch / WebFetch or equivalent tools to look it up, and put the result summary into `context`**. This rule doesn't conflict (external resource tools fetch non-project code, so they don't count as project code sunk cost).

### When to pre-flight search

| Signal in task | What Main Agent should search |
|---|---|
| Using new version / new framework API ("FastAPI 0.115", "Tailwind v4") | Latest docs / changelog / breaking changes |
| Using a library Main Agent is unsure about (niche / lesser-known) | Library README + main API examples |
| Implementing a protocol / spec ("OIDC", "WebRTC SDP") | Key spec section summaries |
| Fixing a bug with an error code | Official docs for the error code / known issues |
| Using a SaaS API (DeepSeek API, Stripe API) | Official endpoint + parameter schema summary |
| Performance optimizing an algorithm | Known best implementations / benchmark data |

### Pre-flight search template

```
1. Use WebSearch for 1-3 queries (don't over-search, save Anthropic quota)
2. Summarize key info:
   - API signatures / parameter tables
   - Required imports / setup
   - Common pitfalls / breaking changes
3. Put the summary at the beginning of delegate_to_deepseek(context=...)
4. Delegate
```

### Example: DS implementing a fastapi SSE endpoint

**❌ Delegation without pre-flight (DS can't get latest docs, may write using old 0.95-era API)**:
```
task="Implement a fastapi SSE endpoint /events for streaming."
context="Project uses fastapi 0.115."
```

**✅ Delegation after pre-flight**:
```
(First, Main Agent calls WebSearch or equivalent: "fastapi SSE EventSourceResponse 0.115 example")
(Get key code snippets, summarize into context)

task="Implement fastapi SSE endpoint /events for streaming."
context="Project fastapi 0.115, reference API usage:
- from sse_starlette.sse import EventSourceResponse
- Return EventSourceResponse(generator())
- generator is async def, yield dict {'event': 'msg', 'data': '...'}
- Client receives via EventSource API

Boundaries: place in api/events.py, reuse db session = Depends(get_session)
Success criteria: curl -N localhost:8000/events returns SSE stream."
```

The second approach significantly increases DS's first-try success rate.

### When pre-flight is not needed

- Common knowledge DS should know (Python stdlib, shell commands, SQL basics)
- Project-internal idioms (collected via Glob/LS, not web search)
- The task itself is search ("scan these logs for X") — nothing external needed

## Delegation Template

```
mcp__deepseek__delegate_to_deepseek(
  task="<Clear description of what to do + success criteria + involved paths>
        (Paths relative to cwd are fine — DeepSeek sandbox root = Main Agent launch directory)",

  context="<Project conventions / framework version / schema / boundaries / known pitfalls>
  - After completion, spot-verify N outputs"
)
```

### Examples

**🟢 Simple (write script)**:
```
task="Write batch_rename.py in scripts/ that renames all *.JPG to *.jpg in the current directory.
      Use pathlib, not os.system. Print count of renamed files on success."
context="Python 3.10+, no third-party dependencies."
```

**🟡 Medium (implement endpoint)**:
```
task="Add a GET /users/:id endpoint in api/users.py returning user detail JSON.
      Table is already in db/schema.sql (users table). Use FastAPI + SQLAlchemy async.
      Success criteria: curl localhost:8000/users/1 returns {id, name, email}."
context="Project uses FastAPI 0.115, DB session injection via Depends(get_session).
        Router module convention: each file has one router instance named 'router'.
        After completion, spin up the server with Bash + curl to self-verify."
```

**🟠 Upper-Medium (batch extraction)**:
```
task="Extract all keys from Resources/*.lproj/Localizable.strings into
      keys.json, schema: { 'file': str, 'keys': [str] }.
      Process file by file, write to ./keys.json."
context="Key naming is lowerCamelCase; .strings format: \"key\" = \"value\";
        Comment lines (// prefix) are ignored. Spot-verify 3 files after completion."
```

**🔎 Audit mode (multi-file read-only sweep)**:
```
task="Check every plugin under plugins-source/pk-*/ for REST route handlers
      that read $_GET/$_POST directly instead of using WP's sanitized
      request->get_param(). Report each occurrence."
context="Project convention: all REST handlers should go through
        WP_REST_Request::get_param(), never superglobals directly.
        False positive to skip: admin-ajax.php legacy handlers (different
        pattern intentionally, not a bug)."
mode="audit"
```

## Must Do After Delegation (Avoid Blind Trust)

DeepSeek self-reporting "done" doesn't mean it's actually done. **Main Agent must verify**:

```
1. Use Read to spot-check 1-2 output files (don't need all) — Read is allowed here, it's new output
2. Check schema compliance
3. Quantity sanity check ("50 files should produce ≥50 keys")
4. If quality issues found:
   a. Minor (a few missing) → Main Agent fixes it
   b. Serious (wrong schema / large gaps) → Edit fix then delegate once more
   c. Disastrous (DeepSeek barely completed anything) → Take over + tell user delegation failed
```

## Fallback Strategy

| Symptom | Handling |
|---|---|
| `ERROR: deepseek-mcp not configured` | Tell user: "DeepSeek key not configured, I'll do it myself" + Main Agent takes over |
| `ERROR: DeepSeek API error` | MCP already auto-retried 2 times; if still fails → take over yourself |
| Agent loop exceeds max_turns | Task too large; split smaller and re-delegate ("do the first 25 files first") |
| Output quality is poor | Fix after verification; 2 consecutive poor results → proactively skip delegation (this session) |
| User launches with `pure` 2 consecutive times | Don't delegate by default, wait for explicit `/ds` |

## General Engineering Discipline (Delegation Does Not Exempt)

- Collect sufficient context before delegation, don't leave half-finished work for DS
- Don't put API keys / sensitive data into task / context parameters
- Don't sleep waiting for DeepSeek — tool calls return synchronously
- DS outputs still need spot-checking per SOLID / project code standards; delegation doesn't exempt code quality responsibility

## User Explicit Control

| User says | Main Agent action |
|---|---|
| "Delegate to DS" / "Outsource to deepseek" | Force invoke this tool, no self-judgment |
| "Do it yourself" / "Don't delegate" | Forbid this tool, proactively fallback in this conversation |
| `/ds <task>` (slash command) | Same as "Delegate to DS" |
| Launch with `pure` command | DEEPSEEK_MODE=off, this tool returns disabled immediately |