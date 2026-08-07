"""DeepSeek agent loop.

Receives a task description → lets DeepSeek run its own Read/Edit/Bash tool cycle → returns final message.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from openai import APIConnectionError, APIError, OpenAI, RateLimitError

from .config import Config
from .tools import build_tool_schemas, execute_tool

logger = logging.getLogger(__name__)

# Max retry attempts per API call (excluding first attempt). Only for network/rate-limit transient errors.
API_RETRY_ATTEMPTS = 2
API_RETRY_BACKOFF_SECONDS = 2.0

# Tool argument logging: fields containing sensitive content (avoid writing to server.log)
SENSITIVE_TOOL_ARG_KEYS = {"content", "new_string"}


SYSTEM_PROMPT_TEMPLATE = """You are DeepSeek working as a sub-agent for Claude.

You're given a focused task to complete autonomously within a workspace.
You have local tools: {tools}

Rules:
1. Stay strictly within the workspace: {workspace}
2. Read before editing. Don't guess file contents.
3. For batch tasks (translating, extracting, refactoring many files), iterate file-by-file.
4. When done, return a final message summarizing:
   - What you did (file paths affected)
   - Any issues / files you couldn't process
   - A brief summary the parent (Claude) can use without re-reading everything
5. Don't ask clarifying questions back to the parent. Make reasonable assumptions
   and document them in your final message.
6. If a tool returns "ERROR: ...", read the error and decide: retry with fixed input,
   skip the file, or report and stop. Don't blindly loop on the same error.
"""

# Audit mode: DeepSeek can only read (Read/Glob/Grep) — no Bash either. Bash's
# blacklist only blocks obviously dangerous commands (see the note at the top
# of safety.py: "blacklist is not a security boundary") — things like
# `rm foo.php` / `sed -i` / `echo x > file` all sail right through it. So audit
# mode simply doesn't offer Bash at all; the tool set itself is the read-only
# guarantee, not a sentence in the prompt.
AUDIT_ALLOWED_TOOLS = ["Read", "Glob", "Grep"]

# Structured findings output — lets Claude triage programmatically instead of
# re-reading a wall of prose. The "confidence" field deliberately echoes the
# CONFIRMED/PLAUSIBLE vocabulary Claude's own code-review tooling uses — same
# mental model on both sides of the delegation.
AUDIT_SYSTEM_PROMPT_TEMPLATE = """You are DeepSeek working as a read-only recon sub-agent for Claude.

You're given an analysis/audit task. You have READ-ONLY tools: {tools}
You cannot write, edit, or run shell commands — this session is enforced read-only
at the tool level, not just by instruction. If you want to "verify by running
something," you can't; reason from what Read/Grep/Glob show you instead.

Rules:
1. Stay strictly within the workspace: {workspace}
2. Explore broadly first (Glob/Grep to find candidate files), then Read the files
   that matter. Don't Read every file in the workspace if the task doesn't need it.
3. Your final message MUST be a single JSON object (no markdown fences, no prose
   outside the JSON) matching this shape:
   {{
     "summary": "one or two sentences on what you looked at and the overall verdict",
     "files_examined": <int>,
     "findings": [
       {{
         "file": "path/relative/to/workspace.php",
         "line": <int or null>,
         "summary": "one-sentence statement of the issue",
         "severity": "critical" | "high" | "medium" | "low",
         "confidence": "confirmed" | "plausible"
       }}
     ]
   }}
4. "confirmed" = you read the exact code and are sure. "plausible" = pattern looks
   wrong but you didn't fully trace all call sites / couldn't verify runtime behavior.
   Don't mark everything "confirmed" to sound authoritative — Claude will spot-check
   a sample, and honest confidence levels make that spot-check more useful, not less.
5. Order findings most-severe-first. If you find more than ~30 real issues, report
   the 30 most severe and say so in "summary" — don't pad the list with trivia.
6. If you find nothing wrong, return an empty "findings" array — don't invent issues.
"""


class AgentLoopError(Exception):
    """Agent loop failed (max turns exceeded, API error, etc)."""


def run_agent(task: str, config: Config, system_prompt_template: str | None = None) -> dict:
    """Run the full agent loop.

    system_prompt_template: override the default prompt (pass AUDIT_SYSTEM_PROMPT_TEMPLATE for audit mode).

    Returns dict:
      - final_message: str (DeepSeek's final response)
      - turns_used: int
      - tokens: {prompt, completion, total}
      - tool_calls: int
      - duration_seconds: float
    """
    client = OpenAI(api_key=config.api_key, base_url=config.base_url)
    tools = build_tool_schemas(config.allowed_tools)

    template = system_prompt_template or SYSTEM_PROMPT_TEMPLATE
    system_prompt = template.format(
        tools=", ".join(config.allowed_tools),
        workspace=config.workspace,
    )

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task},
    ]

    total_prompt_tokens = 0
    total_completion_tokens = 0
    tool_call_count = 0
    started = time.time()

    for turn in range(config.max_turns):
        response = _call_with_retry(client, config, messages, tools, turn)

        usage = response.usage
        if usage:
            total_prompt_tokens += usage.prompt_tokens
            total_completion_tokens += usage.completion_tokens

        msg = response.choices[0].message

        # Use raw dict to preserve all fields, including DeepSeek v4-pro thinking mode's reasoning_content
        # — it requires reasoning_content to be sent back in the next turn, otherwise 400 error
        raw = response.model_dump(exclude_none=True)
        msg_dict = raw["choices"][0]["message"]
        messages.append(msg_dict)

        # No tool_calls means DeepSeek decided to stop
        if not msg.tool_calls:
            return {
                "final_message": msg.content or "(empty response)",
                "turns_used": turn + 1,
                "tokens": {
                    "prompt": total_prompt_tokens,
                    "completion": total_completion_tokens,
                    "total": total_prompt_tokens + total_completion_tokens,
                },
                "tool_calls": tool_call_count,
                "duration_seconds": round(time.time() - started, 2),
            }

        # Execute tool calls sequentially
        for tc in msg.tool_calls:
            tool_call_count += 1
            tool_name = tc.function.name

            # Hard re-check here — don't fully trust "not offered in the schema
            # means it can't be called." Previously this was the only execution
            # entry point and never re-verified allowed_tools; it relied solely
            # on build_tool_schemas() not exposing the schema as a "soft" limit.
            if tool_name not in config.allowed_tools:
                logger.warning(
                    "Turn %d blocked tool_call outside allowed_tools: %s (allowed: %s)",
                    turn, tool_name, config.allowed_tools,
                )
                result = (
                    f"ERROR: tool '{tool_name}' is not permitted in this session "
                    f"(allowed: {config.allowed_tools})."
                )
            else:
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError as e:
                    result = f"ERROR: invalid JSON in tool arguments: {e}"
                else:
                    logger.info(
                        "Turn %d tool_call: %s(%s)",
                        turn,
                        tool_name,
                        _redact_args_for_log(args),
                    )
                    result = execute_tool(tool_name, args, config.workspace)

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                }
            )

    # Hit max_turns without converging — only show the last assistant content, not the full tool_calls blob
    last_text = ""
    for m in reversed(messages):
        if m.get("role") == "assistant" and m.get("content"):
            last_text = str(m["content"])[:500]
            break
    raise AgentLoopError(
        f"Agent loop exceeded max_turns ({config.max_turns}). "
        f"Last assistant text: {last_text or '(none)'}"
    )


def _call_with_retry(client, config, messages, tools, turn):
    """Single API call with transient error retry.

    Only retries on transient errors like network / rate-limit / 5xx; permanent errors like 4xx are raised immediately.
    """
    last_exc = None
    for attempt in range(1 + API_RETRY_ATTEMPTS):
        try:
            return client.chat.completions.create(
                model=config.model,
                messages=messages,
                tools=tools,
                tool_choice="auto",
            )
        except (APIConnectionError, RateLimitError) as e:
            last_exc = e
            wait = API_RETRY_BACKOFF_SECONDS * (attempt + 1)
            logger.warning(
                "Turn %d API transient error (attempt %d/%d): %s — retry in %.1fs",
                turn, attempt + 1, 1 + API_RETRY_ATTEMPTS, e, wait,
            )
            time.sleep(wait)
        except APIError as e:
            # 5xx also retries, 4xx does not
            status = getattr(e, "status_code", None)
            if status and 500 <= status < 600:
                last_exc = e
                wait = API_RETRY_BACKOFF_SECONDS * (attempt + 1)
                logger.warning(
                    "Turn %d API 5xx (attempt %d/%d): %s — retry in %.1fs",
                    turn, attempt + 1, 1 + API_RETRY_ATTEMPTS, e, wait,
                )
                time.sleep(wait)
                continue
            raise AgentLoopError(f"DeepSeek API error on turn {turn}: {e}") from e
        except Exception as e:
            raise AgentLoopError(f"DeepSeek API error on turn {turn}: {e}") from e
    raise AgentLoopError(
        f"DeepSeek API unreachable after {1 + API_RETRY_ATTEMPTS} attempts on turn {turn}: {last_exc}"
    ) from last_exc


def _redact_args_for_log(args: dict) -> dict:
    """Redact tool arguments before logging — content/new_string must not go into server.log (may contain secrets)."""
    redacted = {}
    for k, v in args.items():
        if k in SENSITIVE_TOOL_ARG_KEYS and isinstance(v, str):
            redacted[k] = f"<{len(v)} chars, redacted>"
        elif isinstance(v, str) and len(v) >= 100:
            redacted[k] = f"<{len(v)} chars>"
        else:
            redacted[k] = v
    return redacted
