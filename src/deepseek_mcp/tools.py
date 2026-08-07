"""Implementations for 7 local tools + OpenAI-style function schema definitions.

When DeepSeek calls:
1. agent_loop.py receives tool_call(name, arguments)
2. Dispatches to the corresponding _execute_xxx function here
3. Function returns a string result (success or error description)
4. String is fed back into messages for DeepSeek to continue

Design principles:
- Failures don't raise exceptions; return "ERROR: ..." strings so DeepSeek can see and decide next steps
- Output truncation: single tool result > 50K chars gets truncated to avoid blowing up DeepSeek's context
- All paths must go through safety.resolve_safe_path; raw path operations are forbidden
- Text read/write explicitly uses utf-8 to avoid Windows default cp1252 encoding issues
"""
from __future__ import annotations

import glob as _glob
import json
import re
import subprocess
import uuid
from pathlib import Path

from .safety import SandboxViolation, check_command, resolve_safe_path

MAX_TOOL_OUTPUT = 50_000  # max chars per single tool result
MAX_WRITE_BYTES = 5_000_000  # max bytes per single Write (5MB, prevent DeepSeek from filling disk)
MAX_BASH_TIMEOUT = 600  # max Bash command timeout (seconds)
DEFAULT_BASH_TIMEOUT = 60


def _truncate(text: str) -> str:
    if len(text) > MAX_TOOL_OUTPUT:
        return (
            text[:MAX_TOOL_OUTPUT]
            + f"\n... [truncated, total {len(text)} chars, showing first {MAX_TOOL_OUTPUT}]"
        )
    return text


def _is_binary(path: Path, sniff_bytes: int = 8192) -> bool:
    """Simple binary sniffing: if the first N bytes contain a null byte, treat as binary."""
    try:
        with path.open("rb") as f:
            chunk = f.read(sniff_bytes)
        return b"\x00" in chunk
    except Exception:
        return False


# ===== Tool implementations =====


def _execute_read(args: dict, workspace: Path) -> str:
    """Read a file. args: {path: str, offset?: int, limit?: int}"""
    path = args.get("path", "")
    if not path:
        return "ERROR: missing required 'path' argument"
    try:
        abs_path = resolve_safe_path(path, workspace)
    except SandboxViolation as e:
        return f"ERROR: {e}"
    if not abs_path.exists():
        return f"ERROR: file not found: {path}"
    if not abs_path.is_file():
        return f"ERROR: not a file: {path}"
    if _is_binary(abs_path):
        return f"ERROR: {path} appears to be binary; refusing to read as text."
    try:
        text = abs_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"ERROR: failed to read {path}: {e}"

    offset = int(args.get("offset", 0))
    limit = args.get("limit")
    if offset or limit:
        lines = text.splitlines()
        end = offset + int(limit) if limit else len(lines)
        text = "\n".join(lines[offset:end])

    return _truncate(text)


def _execute_write(args: dict, workspace: Path) -> str:
    """Write a file (overwrites). args: {path: str, content: str}"""
    path = args.get("path", "")
    content = args.get("content", "")
    if not path:
        return "ERROR: missing required 'path' argument"
    if not isinstance(content, str):
        return "ERROR: 'content' must be a string"
    if len(content.encode("utf-8", errors="replace")) > MAX_WRITE_BYTES:
        return f"ERROR: content exceeds {MAX_WRITE_BYTES} bytes; split into smaller writes."
    try:
        abs_path = resolve_safe_path(path, workspace)
    except SandboxViolation as e:
        return f"ERROR: {e}"
    try:
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(content, encoding="utf-8")
    except Exception as e:
        return f"ERROR: failed to write {path}: {e}"
    return f"OK: wrote {len(content)} chars to {path}"


def _execute_edit(args: dict, workspace: Path) -> str:
    """Exact string replacement. args: {path: str, old_string: str, new_string: str, replace_all?: bool}"""
    path = args.get("path", "")
    old = args.get("old_string", "")
    new = args.get("new_string", "")
    replace_all = bool(args.get("replace_all", False))
    if not path or old == "":
        return "ERROR: missing required 'path' or 'old_string'"
    try:
        abs_path = resolve_safe_path(path, workspace)
    except SandboxViolation as e:
        return f"ERROR: {e}"
    if not abs_path.exists():
        return f"ERROR: file not found: {path}"
    if _is_binary(abs_path):
        return f"ERROR: {path} appears to be binary; refusing to edit as text."
    try:
        text = abs_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"ERROR: failed to read {path}: {e}"

    count = text.count(old)
    if count == 0:
        return f"ERROR: old_string not found in {path}"
    if count > 1 and not replace_all:
        return (
            f"ERROR: old_string appears {count} times in {path}. "
            f"Use replace_all=true or provide more context to make it unique."
        )

    new_text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    try:
        abs_path.write_text(new_text, encoding="utf-8")
    except Exception as e:
        return f"ERROR: failed to write {path}: {e}"
    return f"OK: replaced {count if replace_all else 1} occurrence(s) in {path}"


def _execute_bash(args: dict, workspace: Path) -> str:
    """Run a shell command. args: {command: str, timeout?: int(seconds)}"""
    command = args.get("command", "")
    if not command:
        return "ERROR: missing required 'command' argument"
    try:
        check_command(command)
    except SandboxViolation as e:
        return f"ERROR: {e}"

    # timeout clamped to [1, MAX_BASH_TIMEOUT], prevents DeepSeek from stalling with huge values
    try:
        timeout = int(args.get("timeout", DEFAULT_BASH_TIMEOUT))
    except (TypeError, ValueError):
        timeout = DEFAULT_BASH_TIMEOUT
    timeout = max(1, min(timeout, MAX_BASH_TIMEOUT))

    # text=False + manual utf-8 decode: cross-platform stability (Windows cmd may output GBK;
    # replace fallback preserves readability instead of throwing UnicodeDecodeError and "failing" the command)
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=False,
            timeout=timeout,
            cwd=str(workspace),
        )
    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out after {timeout}s"
    except Exception as e:
        return f"ERROR: failed to execute: {e}"

    out = (result.stdout or b"").decode("utf-8", errors="replace")
    err = (result.stderr or b"").decode("utf-8", errors="replace")
    combined = f"[exit {result.returncode}]\n--- stdout ---\n{out}"
    if err:
        combined += f"\n--- stderr ---\n{err}"
    return _truncate(combined)


def _safe_match(path_str: str, workspace: Path, ws_resolved: Path) -> Path | None:
    """Check that a glob-returned path is still inside the workspace (prevents symlink escape)."""
    try:
        p = Path(path_str).resolve()
        p.relative_to(ws_resolved)
        return p
    except (ValueError, OSError):
        return None


def _execute_glob(args: dict, workspace: Path) -> str:
    """File name pattern matching. args: {pattern: str, path?: str}"""
    pattern = args.get("pattern", "")
    if not pattern:
        return "ERROR: missing required 'pattern' argument"
    base = args.get("path", "")
    try:
        base_path = resolve_safe_path(base, workspace) if base else workspace
    except SandboxViolation as e:
        return f"ERROR: {e}"

    ws_resolved = workspace.resolve()
    raw_matches = sorted(_glob.glob(str(base_path / pattern), recursive=True))

    # Filter out symlinks that escape the sandbox
    safe_matches: list[Path] = []
    rejected = 0
    for m in raw_matches:
        p = _safe_match(m, workspace, ws_resolved)
        if p is None:
            rejected += 1
            continue
        safe_matches.append(p)

    rel_matches = []
    for p in safe_matches[:500]:
        try:
            rel_matches.append(str(p.relative_to(ws_resolved)))
        except ValueError:
            rel_matches.append(str(p))

    summary = f"Found {len(safe_matches)} match(es)"
    if len(safe_matches) > 500:
        summary += " (showing first 500)"
    if rejected:
        summary += f" [{rejected} hidden: outside workspace]"
    return summary + ":\n" + "\n".join(rel_matches)


def _execute_notebook_edit(args: dict, workspace: Path) -> str:
    """Edit a single cell in a Jupyter notebook (.ipynb).

    Much better than Read+Write of the entire ipynb — DS only sends "what to change",
    the server parses JSON, locates the cell, preserves cell_id / metadata / other cells'
    outputs, and won't corrupt the notebook if DS writes bad JSON.

    args:
        path: .ipynb file path
        edit_mode: "replace" | "insert" | "delete" (default replace)
        cell_id: cell identifier (preferred over cell_index, stable across edits)
        cell_index: 0-indexed position (used if cell_id not given)
        new_source: new source code (for replace / insert)
        cell_type: "code" | "markdown" (default code, only for insert)
    """
    path = args.get("path", "")
    if not path:
        return "ERROR: missing required 'path' argument"
    if not path.endswith(".ipynb"):
        return f"ERROR: not an .ipynb file: {path}"

    edit_mode = args.get("edit_mode", "replace")
    if edit_mode not in ("replace", "insert", "delete"):
        return f"ERROR: invalid edit_mode '{edit_mode}' (must be replace/insert/delete)"

    try:
        abs_path = resolve_safe_path(path, workspace)
    except SandboxViolation as e:
        return f"ERROR: {e}"

    # Read notebook (or create skeleton for insert)
    if abs_path.exists():
        try:
            nb = json.loads(abs_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            return f"ERROR: failed to parse notebook JSON: {e}"
        except Exception as e:
            return f"ERROR: failed to read notebook: {e}"
        if not isinstance(nb, dict) or not isinstance(nb.get("cells"), list):
            return "ERROR: not a valid notebook (missing 'cells' array)"
    elif edit_mode == "insert":
        # Allow insert into a non-existent notebook (auto-create nbformat 4.5 skeleton)
        nb = {
            "cells": [],
            "metadata": {
                "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
                "language_info": {"name": "python"},
            },
            "nbformat": 4,
            "nbformat_minor": 5,
        }
    else:
        return f"ERROR: notebook not found: {path}"

    cells = nb["cells"]

    # Locate target cell
    cell_id = args.get("cell_id")
    cell_index = args.get("cell_index")
    target_index = None

    if cell_id is not None:
        for i, c in enumerate(cells):
            if isinstance(c, dict) and c.get("id") == cell_id:
                target_index = i
                break
        if target_index is None and edit_mode != "insert":
            return f"ERROR: cell_id '{cell_id}' not found in {path}"
    elif cell_index is not None:
        try:
            cell_index = int(cell_index)
        except (TypeError, ValueError):
            return f"ERROR: invalid cell_index '{cell_index}'"
        if 0 <= cell_index < len(cells):
            target_index = cell_index
        elif edit_mode != "insert":
            return f"ERROR: cell_index {cell_index} out of range (0..{len(cells) - 1})"
    elif edit_mode != "insert":
        return "ERROR: replace/delete require cell_id or cell_index"

    # Execute edit
    new_source = args.get("new_source", "")
    if not isinstance(new_source, str):
        return "ERROR: 'new_source' must be a string"

    def _split_source(s: str) -> list[str]:
        # nbformat expects source as list[str], preserving newlines at end of each item (except last line)
        if not s:
            return [""]
        lines = s.splitlines(keepends=True)
        return lines if lines else [""]

    if edit_mode == "replace":
        cell = cells[target_index]
        cell["source"] = _split_source(new_source)
        if cell.get("cell_type") == "code":
            # Source changed; old outputs no longer represent this source — clear them
            cell["outputs"] = []
            cell["execution_count"] = None
        result_msg = f"OK: replaced cell at index {target_index} (id={cell.get('id', 'n/a')})"
    elif edit_mode == "insert":
        cell_type = args.get("cell_type", "code")
        if cell_type not in ("code", "markdown"):
            return f"ERROR: invalid cell_type '{cell_type}' (must be code or markdown)"
        new_cell: dict = {
            "cell_type": cell_type,
            "id": uuid.uuid4().hex[:8],
            "source": _split_source(new_source),
            "metadata": {},
        }
        if cell_type == "code":
            new_cell["outputs"] = []
            new_cell["execution_count"] = None
        # Insert after target_index (or append to end if not specified)
        insert_at = (target_index + 1) if target_index is not None else len(cells)
        cells.insert(insert_at, new_cell)
        result_msg = f"OK: inserted {cell_type} cell at index {insert_at} (id={new_cell['id']})"
    else:  # delete
        removed = cells.pop(target_index)
        result_msg = f"OK: deleted cell at index {target_index} (was id={removed.get('id', 'n/a')})"

    # Write back
    try:
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(
            json.dumps(nb, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8",
        )
    except Exception as e:
        return f"ERROR: failed to write notebook: {e}"

    return result_msg + f" (total cells: {len(cells)})"


def _execute_grep(args: dict, workspace: Path) -> str:
    """Regex search file contents. args: {pattern: str, path?: str, glob?: str, max_matches?: int}"""
    pattern = args.get("pattern", "")
    if not pattern:
        return "ERROR: missing required 'pattern' argument"
    base = args.get("path", "")
    file_glob = args.get("glob", "**/*")
    try:
        max_matches = max(1, min(int(args.get("max_matches", 100)), 1000))
    except (TypeError, ValueError):
        max_matches = 100

    try:
        base_path = resolve_safe_path(base, workspace) if base else workspace
    except SandboxViolation as e:
        return f"ERROR: {e}"
    try:
        regex = re.compile(pattern)
    except re.error as e:
        return f"ERROR: invalid regex: {e}"

    ws_resolved = workspace.resolve()
    results = []
    for filepath in _glob.iglob(str(base_path / file_glob), recursive=True):
        # Critical: re-validate each match in sandbox (prevents symlink escape reading /etc/* etc.)
        p = _safe_match(filepath, workspace, ws_resolved)
        if p is None or not p.is_file():
            continue
        if _is_binary(p):
            continue
        try:
            for lineno, line in enumerate(
                p.read_text(encoding="utf-8", errors="replace").splitlines(), 1
            ):
                if regex.search(line):
                    try:
                        rel = p.relative_to(ws_resolved)
                    except ValueError:
                        rel = p
                    results.append(f"{rel}:{lineno}: {line}")
                    if len(results) >= max_matches:
                        break
        except Exception:
            continue
        if len(results) >= max_matches:
            break

    if not results:
        return f"No matches found for pattern: {pattern}"
    header = f"Found {len(results)} match(es)"
    if len(results) >= max_matches:
        header += f" (limit {max_matches} reached)"
    return header + ":\n" + "\n".join(results)


# ===== Tool dispatch table =====

TOOL_REGISTRY = {
    "Read": _execute_read,
    "Write": _execute_write,
    "Edit": _execute_edit,
    "Bash": _execute_bash,
    "Glob": _execute_glob,
    "Grep": _execute_grep,
    "NotebookEdit": _execute_notebook_edit,
}


def execute_tool(name: str, args: dict, workspace: Path) -> str:
    """Dispatch entry: calls the corresponding implementation by tool name."""
    fn = TOOL_REGISTRY.get(name)
    if fn is None:
        return f"ERROR: unknown tool '{name}'. Available: {list(TOOL_REGISTRY.keys())}"
    return fn(args, workspace)


# ===== OpenAI function calling schema =====


def build_tool_schemas(allowed: list[str]) -> list[dict]:
    """Generate the tools parameter for DeepSeek (OpenAI-compatible)."""
    all_schemas = {
        "Read": {
            "name": "Read",
            "description": "Read a file's contents (UTF-8 text only). Use this before editing.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to workspace (or absolute path within workspace)."},
                    "offset": {"type": "integer", "description": "Starting line number (0-indexed). Optional."},
                    "limit": {"type": "integer", "description": "Number of lines to read. Optional."},
                },
                "required": ["path"],
            },
        },
        "Write": {
            "name": "Write",
            "description": "Write/overwrite a file with given content (UTF-8). Creates parent dirs if needed. Max 5MB per write.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to workspace."},
                    "content": {"type": "string", "description": "Full file content to write."},
                },
                "required": ["path", "content"],
            },
        },
        "Edit": {
            "name": "Edit",
            "description": "Replace exact text in a file. Fails if old_string appears multiple times unless replace_all=true.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string", "description": "Exact text to find (must be unique unless replace_all)."},
                    "new_string": {"type": "string", "description": "Replacement text."},
                    "replace_all": {"type": "boolean", "description": "Replace all occurrences. Default false."},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
        "Bash": {
            "name": "Bash",
            "description": f"Run a shell command in the workspace directory. Dangerous commands are blocked. Timeout clamped to [1, {MAX_BASH_TIMEOUT}]s.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to run."},
                    "timeout": {"type": "integer", "description": f"Timeout in seconds (default {DEFAULT_BASH_TIMEOUT}, max {MAX_BASH_TIMEOUT})."},
                },
                "required": ["command"],
            },
        },
        "Glob": {
            "name": "Glob",
            "description": "Find files matching a glob pattern (e.g. **/*.py, src/**/*.ts). Symlinks pointing outside the workspace are silently filtered.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern."},
                    "path": {"type": "string", "description": "Base directory (relative to workspace). Optional."},
                },
                "required": ["pattern"],
            },
        },
        "Grep": {
            "name": "Grep",
            "description": "Search file contents with a regex pattern. Sandbox re-validates every match (symlinks out of workspace are skipped).",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern."},
                    "path": {"type": "string", "description": "Base directory. Optional."},
                    "glob": {"type": "string", "description": "File glob filter (default **/*)."},
                    "max_matches": {"type": "integer", "description": "Max matches to return (default 100, hard cap 1000)."},
                },
                "required": ["pattern"],
            },
        },
        "NotebookEdit": {
            "name": "NotebookEdit",
            "description": (
                "Edit a Jupyter notebook (.ipynb) cell-by-cell. Modes: 'replace' "
                "(change a cell's source, clears outputs/execution_count), 'insert' "
                "(add new cell after target, auto-generates cell id), 'delete' "
                "(remove cell). Preserves nbformat schema, other cells' outputs, "
                "and notebook metadata. Prefer over Read+Write for .ipynb files."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to .ipynb file relative to workspace."},
                    "edit_mode": {
                        "type": "string",
                        "enum": ["replace", "insert", "delete"],
                        "description": "Edit operation (default: replace).",
                    },
                    "cell_id": {"type": "string", "description": "Cell identifier (preferred over cell_index — stable across edits)."},
                    "cell_index": {"type": "integer", "description": "0-indexed cell position (used if cell_id not provided)."},
                    "new_source": {"type": "string", "description": "New cell source for replace/insert. Multi-line OK."},
                    "cell_type": {
                        "type": "string",
                        "enum": ["code", "markdown"],
                        "description": "Cell type for insert mode (default code).",
                    },
                },
                "required": ["path", "edit_mode"],
            },
        },
    }
    return [
        {"type": "function", "function": all_schemas[name]}
        for name in allowed
        if name in all_schemas
    ]
