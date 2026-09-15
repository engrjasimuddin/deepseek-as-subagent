"""Load ~/.deepseek-mcp/config.json + env overrides.

Priority: environment variable > config file > defaults (cwd).
workspace defaults to following the MCP server process's cwd — i.e. the directory
where Claude Code was launched, consistent with Claude's main process sandbox;
no manual config required.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_PATH = Path.home() / ".deepseek-mcp" / "config.json"

DEFAULT_MODEL = "deepseek-flash"  # Primary model (cost-efficient); switch to deepseek-v4-pro for stronger reasoning
DEFAULT_MAX_TURNS = 50
DEFAULT_ALLOWED_TOOLS = ["Read", "Write", "Edit", "Bash", "Glob", "Grep", "NotebookEdit"]

logger = logging.getLogger(__name__)


@dataclass
class Config:
    api_key: str
    workspace: Path
    model: str = DEFAULT_MODEL
    max_turns: int = DEFAULT_MAX_TURNS
    allowed_tools: list[str] = field(default_factory=lambda: list(DEFAULT_ALLOWED_TOOLS))
    base_url: str = "https://api.deepseek.com"

    @classmethod
    def load(cls) -> "Config":
        # DEEPSEEK_MODE=off → let server.py decide whether to expose tools
        # Here we only load the real config
        data: dict = {}
        if CONFIG_PATH.exists():
            try:
                data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                raise RuntimeError(
                    f"Invalid JSON in {CONFIG_PATH} (line {e.lineno}, col {e.colno}): {e.msg}"
                ) from e
            if not isinstance(data, dict):
                raise RuntimeError(f"Top-level of {CONFIG_PATH} must be a JSON object")

        # API key: env > config, strip leading/trailing whitespace (paste often includes it)
        api_key = (os.getenv("DEEPSEEK_API_KEY") or data.get("api_key", "")).strip()
        if not api_key or api_key == "PASTE_YOUR_DEEPSEEK_KEY_HERE":
            raise RuntimeError(
                f"DeepSeek API key not configured. "
                f"Set DEEPSEEK_API_KEY env var or edit {CONFIG_PATH}"
            )
        if not api_key.startswith("sk-"):
            logger.warning(
                "API key doesn't start with 'sk-' — DeepSeek may reject it. "
                "Check that you copied the full key from platform.deepseek.com."
            )

        # workspace resolution: env > config > cwd
        # Misconfiguration doesn't hard fail — warning + fallback to cwd, ensures MCP always works
        workspace_str = os.getenv("DEEPSEEK_WORKSPACE") or data.get("workspace", "")
        if workspace_str:
            workspace = Path(os.path.expanduser(workspace_str)).resolve()
            if not workspace.exists():
                logger.warning(
                    "Configured workspace does not exist: %s — falling back to cwd",
                    workspace,
                )
                workspace = Path.cwd()
        else:
            workspace = Path.cwd()  # Follow Claude Code launch directory

        # max_turns must be >= 1, otherwise for-loop won't enter and downstream gets inconsistent state
        try:
            max_turns = int(data.get("max_turns", DEFAULT_MAX_TURNS))
        except (TypeError, ValueError):
            max_turns = DEFAULT_MAX_TURNS
        if max_turns < 1:
            logger.warning("max_turns=%d invalid, using default %d", max_turns, DEFAULT_MAX_TURNS)
            max_turns = DEFAULT_MAX_TURNS

        allowed_tools = data.get("allowed_tools", list(DEFAULT_ALLOWED_TOOLS))
        if not isinstance(allowed_tools, list) or not all(isinstance(t, str) for t in allowed_tools):
            logger.warning("allowed_tools invalid, using default")
            allowed_tools = list(DEFAULT_ALLOWED_TOOLS)

        return cls(
            api_key=api_key,
            workspace=workspace,
            model=data.get("model", DEFAULT_MODEL),
            max_turns=max_turns,
            allowed_tools=allowed_tools,
            base_url=data.get("base_url", "https://api.deepseek.com"),
        )
