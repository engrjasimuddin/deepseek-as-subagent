"""Sandbox: path restrictions + command blacklist.

Design goal: DeepSeek is an "obedient assistant but not guaranteed reliable" —
it may misread paths or run wrong commands. No docker (slow startup, heavy dependencies),
use lightweight in-process checks to catch 95% of accidents.

Note: Blacklist is not a security boundary. Real adversarial attacks should use
docker / bubblewrap / sandbox-exec or similar true sandboxes. These checks are only
guardrails to prevent DeepSeek from "going off-script."
"""
from __future__ import annotations

import shlex
from pathlib import Path

# Dangerous command detection at two granularities:
#   1) DANGEROUS_TOKENS: first token (program name) exact match, hard to bypass with \ encoding
#   2) DANGEROUS_PHRASES: full phrase substring match (combinations like rm -rf / that are "never legitimate")
DANGEROUS_TOKENS = {
    "sudo",
    "su",
    "nc", "ncat", "netcat",
    "ssh", "scp", "sftp", "rsync",
    "curl", "wget",
    "telnet",
    "socat",
}

# Companion program names (rejected if they appear anywhere in the token stream)
DANGEROUS_ANYWHERE_TOKENS = {
    "sudo", "su",
}

# Full phrase match (preserves old style, specifically catches "morphologically unique" dangerous combos)
DANGEROUS_PHRASES = [
    "rm -rf /",
    "rm -rf ~",
    "rm -rf $HOME",
    "rm -rf *",
    "rm -rf .",
    ":(){:|:&};:",  # fork bomb
    "mkfs.",
    "> /dev/sd",
    "chmod -R 777 /",
    "dd if=/dev/zero",
    "dd if=/dev/random",
    "/dev/tcp/",
    "/dev/udp/",
]

# Python / shell / interpreter -c inline code: easily hides malicious commands, reject uniformly
DANGEROUS_INLINE_INTERPRETERS = {
    ("python", "-c"), ("python3", "-c"),
    ("perl", "-e"), ("ruby", "-e"),
    ("node", "-e"), ("php", "-r"),
    ("awk", "-e"),
    ("sh", "-c"), ("bash", "-c"), ("zsh", "-c"), ("ksh", "-c"), ("dash", "-c"),
}

# Package manager "install" actions: easily abused to install malicious packages
PACKAGE_INSTALL_PREFIXES = [
    ("pip", "install"), ("pip3", "install"),
    ("pipx", "install"),
    ("npm", "install"), ("npm", "i"),
    ("yarn", "add"),
    ("pnpm", "install"), ("pnpm", "add"),
    ("uv", "pip"),
    ("uv", "add"),
    ("gem", "install"),
    ("cargo", "install"),
    ("brew", "install"),
    ("apt", "install"), ("apt-get", "install"),
    ("dnf", "install"), ("yum", "install"),
]

# Publish / push actions: "exit points" that write to the outside world
PUBLISH_PREFIXES = [
    ("git", "push"),
    ("npm", "publish"),
    ("twine", "upload"),
    ("cargo", "publish"),
    ("gh", "release"),
]


class SandboxViolation(Exception):
    """Tool call violated sandbox rules. Returned to DeepSeek so it knows why it failed."""


def resolve_safe_path(rel_or_abs: str, workspace: Path) -> Path:
    """Resolve a path from DeepSeek to an absolute path and verify it's inside the workspace.

    Returns: resolved absolute path.
    Raises: SandboxViolation if the path escapes the workspace.
    """
    if not rel_or_abs:
        raise SandboxViolation("empty path is not allowed")
    if "\x00" in rel_or_abs:
        raise SandboxViolation("null byte in path is not allowed")

    p = Path(rel_or_abs).expanduser()
    if not p.is_absolute():
        p = workspace / p
    abs_path = p.resolve()
    ws_resolved = workspace.resolve()

    try:
        abs_path.relative_to(ws_resolved)
    except ValueError as e:
        raise SandboxViolation(
            f"Path {abs_path} is outside workspace {ws_resolved}. "
            f"Tools can only access files within the configured workspace."
        ) from e

    return abs_path


def _tokenize(command: str) -> list[str]:
    """Safe tokenization. Falls back to split when shlex errors on unclosed quotes."""
    try:
        return shlex.split(command, comments=False, posix=True)
    except ValueError:
        return command.split()


def check_command(command: str) -> None:
    """Check if a Bash command is on the blacklist. Raises SandboxViolation to reject.

    Multi-layer checks (any hit rejects):
      1) DANGEROUS_PHRASES: coarse-grained substring
      2) DANGEROUS_TOKENS: tokenized program name (first token or first token after pipe)
      3) DANGEROUS_ANYWHERE_TOKENS: sudo / su appearing anywhere
      4) DANGEROUS_INLINE_INTERPRETERS: python -c / perl -e etc.
      5) PACKAGE_INSTALL_PREFIXES: package install actions
      6) PUBLISH_PREFIXES: publish actions (git push / npm publish etc.)
    """
    if not command or not command.strip():
        raise SandboxViolation("empty command")

    lower = command.lower()

    # 1) Phrase match (no tokenization, specifically catches special combinations)
    for phrase in DANGEROUS_PHRASES:
        if phrase.lower() in lower:
            raise SandboxViolation(
                f"Command blocked by sandbox: contains dangerous phrase '{phrase}'."
            )

    # 2-6) Tokenize then check per-clause (split by ; && || |)
    # Simple split; doesn't aim for 100% bash parsing, just prevents 'a; rm -rf /' from slipping through
    clauses = _split_clauses(command)
    for clause in clauses:
        tokens = _tokenize(clause)
        if not tokens:
            continue

        first = _strip_cmd_prefix(tokens[0])

        # sudo / su appearing anywhere
        for tok in tokens:
            if _strip_cmd_prefix(tok) in DANGEROUS_ANYWHERE_TOKENS:
                raise SandboxViolation(
                    f"Command blocked by sandbox: '{tok}' not allowed."
                )

        # Program name blacklist
        if first in DANGEROUS_TOKENS:
            raise SandboxViolation(
                f"Command blocked by sandbox: program '{first}' not allowed "
                f"(network / privilege escalation tools are disabled)."
            )

        # Inline interpreter
        if len(tokens) >= 2:
            sig = (first, tokens[1])
            if sig in DANGEROUS_INLINE_INTERPRETERS:
                raise SandboxViolation(
                    f"Command blocked by sandbox: inline code via '{first} {tokens[1]}' "
                    f"is not allowed (write a file then run it instead)."
                )

        # Package install / publish
        if len(tokens) >= 2:
            sig = (first, tokens[1])
            if sig in PACKAGE_INSTALL_PREFIXES:
                raise SandboxViolation(
                    f"Command blocked by sandbox: package install '{first} {tokens[1]}' "
                    f"is not allowed."
                )
            if sig in PUBLISH_PREFIXES:
                raise SandboxViolation(
                    f"Command blocked by sandbox: publish action '{first} {tokens[1]}' "
                    f"is not allowed."
                )


def _split_clauses(command: str) -> list[str]:
    """Split into clauses by ; && || | (coarse-grained, ignores separators inside quotes — good enough)."""
    out: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(command)
    in_single = False
    in_double = False
    while i < n:
        c = command[i]
        # Simple quote tracking, prevents ';' inside quotes from being treated as separator
        if c == "'" and not in_double:
            in_single = not in_single
            buf.append(c)
            i += 1
            continue
        if c == '"' and not in_single:
            in_double = not in_double
            buf.append(c)
            i += 1
            continue
        if not in_single and not in_double:
            two = command[i:i+2]
            if c in ";|&" or two in ("&&", "||"):
                if buf:
                    out.append("".join(buf).strip())
                    buf = []
                i += 2 if two in ("&&", "||") else 1
                continue
        buf.append(c)
        i += 1
    if buf:
        out.append("".join(buf).strip())
    return [c for c in out if c]


def _strip_cmd_prefix(tok: str) -> str:
    """Strip 'command'/'\\'/'/path/to/' etc. program name prefixes for normalized judgment."""
    # 'command curl' / '\curl' / '/usr/bin/curl' → 'curl'
    if tok.startswith("\\"):
        tok = tok[1:]
    if "/" in tok:
        tok = tok.rsplit("/", 1)[-1]
    return tok.lower()
