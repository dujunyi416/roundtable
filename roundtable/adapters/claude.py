"""Claude Code adapter — wraps `claude -p` headless mode.

First call:   claude -p --output-format json          (prompt on stdin)
Later calls:  claude -p --resume <session_id> ...     (prompt on stdin)

Note: every `--resume` returns a NEW session_id for the continued
conversation, so we refresh self.session_id from every reply.
"""
from __future__ import annotations

import json

from . import Adapter, AdapterError, resolve_binary

INSTALL_HINT = "npm install -g @anthropic-ai/claude-code  (then run `claude` once to log in)"
READONLY_TOOLS = "Read,Grep,Glob"


class ClaudeAdapter(Adapter):
    name = "claude"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.binary = resolve_binary(["claude"], "ROUNDTABLE_CLAUDE_BIN")
        if not self.binary:
            raise AdapterError(f"claude CLI not found on PATH. Install: {INSTALL_HINT}")

    def _build_command(self, first: bool) -> list[str]:
        cmd = [self.binary, "-p", "--output-format", "json"]
        if not first:
            cmd += ["--resume", self.session_id]
        if self.model:
            cmd += ["--model", self.model]
        if self.dangerous:
            cmd += ["--dangerously-skip-permissions"]
        elif self.writable:
            cmd += ["--permission-mode", "acceptEdits"]
        else:
            cmd += ["--allowedTools", READONLY_TOOLS]
        return cmd

    def _parse(self, stdout: str, stderr: str) -> str:
        raw = stdout.strip()
        # Tolerate stray non-JSON noise around the result object.
        if not raw.startswith("{"):
            start = raw.find("{")
            end = raw.rfind("}")
            if start == -1 or end <= start:
                raise AdapterError(f"claude: expected JSON output, got:\n{raw[:500]}")
            raw = raw[start : end + 1]
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AdapterError(f"claude: could not parse JSON output: {exc}") from exc
        if data.get("is_error"):
            raise AdapterError(f"claude reported an error: {data.get('result', data)}")
        if data.get("session_id"):
            self.session_id = data["session_id"]
        self.last_usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
        text = data.get("result")
        if not text:
            raise AdapterError(f"claude returned an empty result (subtype={data.get('subtype')})")
        return text
