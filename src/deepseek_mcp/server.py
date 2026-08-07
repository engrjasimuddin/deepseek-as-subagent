"""MCP server entry point.

Exposes two tools to Claude Code:
  - ping: health check
  - delegate_to_deepseek: true sub-agent, outsources tasks to DeepSeek running a full agent loop

Environment variables:
  - DEEPSEEK_MODE=off: delegate tool returns disabled notice immediately, Claude won't call again
  - DEEPSEEK_API_KEY: overrides api_key in config file
  - DEEPSEEK_WORKSPACE: overrides workspace in config file
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import re
import sys
from pathlib import Path

# On Windows, asyncio defaults to ProactorEventLoop, which is incompatible with stdio subprocesses and hangs.
# Must switch to SelectorEventLoopPolicy before importing FastMCP / starting the event loop.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from mcp.server.fastmcp import FastMCP

from . import __version__
from .agent_loop import AUDIT_ALLOWED_TOOLS, AUDIT_SYSTEM_PROMPT_TEMPLATE, AgentLoopError, run_agent
from .config import Config

MAX_AUDIT_FINDINGS = 30
_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}

# Log to ~/.deepseek-mcp/ (don't pollute stdout, stdout is the MCP protocol channel)
# log dir 700, file 600 — contains paths / task summaries, shouldn't be world-readable on multi-user machines
_LOG_DIR = Path.home() / ".deepseek-mcp"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
_SERVER_LOG = _LOG_DIR / "server.log"
_USAGE_LOG = _LOG_DIR / "usage.log"

# Windows doesn't support POSIX permission bits, os.chmod is a no-op; failure is non-fatal
try:
    os.chmod(_LOG_DIR, 0o700)
except OSError:
    pass

# chmod immediately after creating files (basicConfig creates with default umask, may be 644)
for _p in (_SERVER_LOG, _USAGE_LOG):
    if not _p.exists():
        try:
            _p.touch(mode=0o600)
        except OSError:
            pass
    try:
        os.chmod(_p, 0o600)
    except OSError:
        pass

logging.basicConfig(
    filename=str(_SERVER_LOG),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


mcp = FastMCP("deepseek-mcp")


@mcp.tool()
def ping() -> str:
    """Health check. Confirms the deepseek-mcp server is alive.

    Use this before delegate_to_deepseek if you're not sure whether DeepSeek is configured.
    Returns version, mode (auto/off), and whether config is loadable.
    """
    mode = os.getenv("DEEPSEEK_MODE", "auto")
    try:
        cfg = Config.load()
        ws_short = _shorten_path(cfg.workspace)
        config_status = f"workspace={ws_short} (sandbox), model={cfg.model}"
    except Exception as e:
        config_status = f"NOT_CONFIGURED ({e})"
    return f"pong from deepseek-mcp v{__version__} | mode={mode} | {config_status}"


def _shorten_path(p: Path) -> str:
    """Compress long paths to ~ + last few segments, avoiding ping output overflow."""
    s = str(p)
    home = str(Path.home())
    if s.startswith(home):
        s = "~" + s[len(home):]
    if len(s) > 60:
        parts = s.split("/")
        if len(parts) > 4:
            s = "/".join(parts[:2] + ["..."] + parts[-2:])
    return s


@mcp.tool()
def delegate_to_deepseek(task: str, context: str = "", mode: str = "execute") -> str:
    """Delegate a focused task to DeepSeek as a real sub-agent.

    Two modes:

    mode="execute" (default) — DeepSeek runs its own agent loop with
    Read/Write/Edit/Bash/Glob/Grep/NotebookEdit tools inside the configured
    workspace and does the ENTIRE task end-to-end (including any file changes),
    returning only a final summary. Use this for batch / repetitive / mechanical
    tasks where you want DeepSeek to do the heavy lifting and hand you back a
    finished result.

      Good fits: extract i18n keys from N files into JSON, translate large
      chunks of text, scan logs for patterns, bulk refactors with a clear
      pattern, one-off ETL scripts.

    mode="audit" — DeepSeek is READ-ONLY (Read/Glob/Grep only — no Write, Edit,
    NotebookEdit, or Bash; enforced at the tool-dispatch level, not just by
    instruction). It investigates and returns structured JSON findings
    (file/line/summary/severity/confidence) instead of making changes. Use this
    for read-heavy analysis across many files where finding the issue is
    expensive but fixing it is cheap — YOU (Claude) then decide what to do
    with each finding and make the actual edits yourself. Findings are capped
    at 30, most-severe-first.

      Good fits: "scan these 15 plugins for X pattern", "find every place Y is
      called without Z", multi-file security/consistency sweeps.
      Bad fits for audit mode: single-file bug hunts (just Read it yourself),
      tasks where you already know which 1-2 files to check.

    Neither mode is a fit for: architectural design / cross-file judgment calls,
    deep single-bug root-cause reasoning, or tasks requiring project-specific
    idioms from CLAUDE.md (DeepSeek can't see your CLAUDE.md or conversation
    history — put anything it needs to know into `context`).

    Args:
        task: Clear description of what DeepSeek should accomplish, including
              success criteria (execute mode) or what to look for (audit mode),
              and file paths/scope involved.
        context: Optional additional context — project conventions, related
                 files DeepSeek should consider, output format requirements.
                 Include this when project-specific knowledge matters.
        mode: "execute" (default) or "audit". Invalid values fall back to
              "execute" with a warning in the response.

    Returns:
        execute mode: a summary of what DeepSeek did, including files affected,
        turns used, tokens consumed, and any issues. Always verify by reading a
        sample of the affected files before declaring success to the user.

        audit mode: a JSON string with "summary", "files_examined", and a
        "findings" array. Each finding has a "confidence" of "confirmed" or
        "plausible" — spot-check at least the "plausible" ones yourself before
        acting on them (DeepSeek's self-reported confidence is a hint, not a
        guarantee).
    """
    env_mode = os.getenv("DEEPSEEK_MODE", "auto")
    if env_mode == "off":
        return (
            "DeepSeek delegation is disabled (DEEPSEEK_MODE=off). "
            "Continue the task yourself in the main conversation."
        )

    mode_warning = ""
    if mode not in ("execute", "audit"):
        mode_warning = f"[deepseek-mcp] WARNING: unknown mode '{mode}', falling back to 'execute'.\n\n"
        mode = "execute"

    try:
        config = Config.load()
    except Exception as e:
        return f"ERROR: deepseek-mcp not configured: {e}"

    if mode == "audit":
        config = dataclasses.replace(config, allowed_tools=list(AUDIT_ALLOWED_TOOLS))
        system_prompt_template = AUDIT_SYSTEM_PROMPT_TEMPLATE
    else:
        system_prompt_template = None

    full_task = task
    if context:
        full_task = f"{task}\n\n# Additional context\n{context}"

    logger.info(
        "delegate_to_deepseek invoked. mode=%s Task length=%d, context length=%d",
        mode, len(task), len(context),
    )

    try:
        result = run_agent(full_task, config, system_prompt_template=system_prompt_template)
    except AgentLoopError as e:
        logger.exception("Agent loop failed")
        return f"ERROR: DeepSeek agent loop failed: {e}"
    except Exception as e:
        logger.exception("Unexpected error during delegation")
        return f"ERROR: unexpected failure: {e}"

    logger.info(
        "delegate_to_deepseek done. mode=%s turns=%d tool_calls=%d tokens=%d duration=%.2fs",
        mode,
        result["turns_used"],
        result["tool_calls"],
        result["tokens"]["total"],
        result["duration_seconds"],
    )

    # Usage log (human-readable append to usage.log)
    # Note: only log first 60 chars of task as summary, don't log context (may contain project-sensitive details)
    try:
        # Simple size control: rotate when >10MB (rename to .1)
        if _USAGE_LOG.exists() and _USAGE_LOG.stat().st_size > 10 * 1024 * 1024:
            try:
                _USAGE_LOG.replace(_USAGE_LOG.with_suffix(".log.1"))
            except OSError:
                pass
        with open(_USAGE_LOG, "a", encoding="utf-8") as f:
            f.write(
                f"{result['duration_seconds']:.1f}s  "
                f"mode={mode:<7}  "
                f"turns={result['turns_used']:>2}  "
                f"tools={result['tool_calls']:>2}  "
                f"tokens={result['tokens']['total']:>6}  "
                f"task={task[:60]!r}\n"
            )
        try:
            os.chmod(_USAGE_LOG, 0o600)
        except OSError:
            pass
    except Exception:
        pass  # Log failure doesn't affect main flow

    final_message = result["final_message"]
    if mode == "audit":
        final_message = _cap_audit_findings(final_message)

    return (
        f"{mode_warning}"
        f"{final_message}\n\n"
        f"---\n"
        f"[deepseek-mcp] mode={mode}, {result['turns_used']} turns, "
        f"{result['tool_calls']} tool calls, "
        f"{result['tokens']['total']} tokens, "
        f"{result['duration_seconds']}s"
    )


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)


def _try_extract_json_object(raw: str) -> dict | None:
    """Best-effort JSON extraction from a model response.

    Tried in order:
      1. The whole trimmed string parses as JSON directly.
      2. A ```json fenced block ANYWHERE in the text (not just at the start —
         DeepSeek sometimes writes a sentence like "Let me compile the
         findings." before the fence, despite being told not to).
      3. The substring from the first '{' to the last '}' in the text.
    Returns the parsed dict, or None if nothing works.
    """
    text = raw.strip()

    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    fence_match = _JSON_FENCE_RE.search(text)
    if fence_match:
        try:
            data = json.loads(fence_match.group(1).strip())
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

    first = text.find("{")
    last = text.rfind("}")
    if first != -1 and last != -1 and last > first:
        try:
            data = json.loads(text[first : last + 1])
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

    return None


def _cap_audit_findings(raw: str) -> str:
    """Server-side enforcement of the findings cap — don't just trust the prompt.

    DeepSeek is asked to self-limit to 30 findings, sorted most-severe-first,
    but a prompt instruction isn't a guarantee. If the response parses as the
    expected JSON shape, re-sort by severity and hard-truncate here too. If it
    doesn't parse (DeepSeek didn't follow the JSON format), return it unchanged
    with a note — Claude should treat unparseable audit output as needing extra
    scrutiny, not silently trust it.
    """
    data = _try_extract_json_object(raw)
    if data is None:
        return raw + "\n\n[deepseek-mcp] NOTE: audit output was not valid JSON — treat as unverified prose, not structured findings."

    if not isinstance(data, dict) or "findings" not in data:
        return raw + "\n\n[deepseek-mcp] NOTE: audit output was valid JSON but missing 'findings' — treat as unverified."

    findings = data.get("findings")
    if not isinstance(findings, list):
        return raw

    findings_sorted = sorted(
        findings,
        key=lambda f: _SEVERITY_RANK.get((f.get("severity") or "").lower(), 99) if isinstance(f, dict) else 99,
    )
    if len(findings_sorted) > MAX_AUDIT_FINDINGS:
        data["findings_truncated"] = True
        data["findings_total_before_truncation"] = len(findings_sorted)
        findings_sorted = findings_sorted[:MAX_AUDIT_FINDINGS]
    data["findings"] = findings_sorted

    return json.dumps(data, ensure_ascii=False, indent=2)


def main() -> None:
    """CLI entry point."""
    logger.info("deepseek-mcp v%s starting (mode=%s)", __version__, os.getenv("DEEPSEEK_MODE", "auto"))
    try:
        mcp.run()
    except Exception as e:
        logger.exception("MCP server crashed: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
