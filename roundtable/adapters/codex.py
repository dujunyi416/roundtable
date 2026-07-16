"""Codex CLI adapter — wraps `codex exec` non-interactive mode.

First call:   codex exec --json -o <tmpfile> ... -            (prompt on stdin)
Later calls:  codex exec resume ... <session_id> -            (prompt on stdin)

The reply text is read from the --output-last-message file (reliable across
codex versions). The session id must be present in the --json event stream;
without an explicit id the adapter fails closed instead of risking cross-run
session contamination through `resume --last`.
"""
from __future__ import annotations

import json
import os
import tempfile

from . import Adapter, AdapterError, resolve_binary

INSTALL_HINT = "npm install -g @openai/codex  (then run `codex` once to log in)"
SESSION_KEYS = ("session_id", "thread_id", "conversation_id")


def _find_key(obj, keys):
    """Depth-first search for the first string value under any of `keys`."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and isinstance(v, str) and v:
                return v
            found = _find_key(v, keys)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_key(item, keys)
            if found:
                return found
    return None


def _find_usage(obj):
    if isinstance(obj, dict):
        usage = obj.get("usage")
        if isinstance(usage, dict):
            return usage
        for value in obj.values():
            found = _find_usage(value)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_usage(item)
            if found:
                return found
    return None


class CodexAdapter(Adapter):
    name = "codex"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.binary = resolve_binary(["codex"], "ROUNDTABLE_CODEX_BIN")
        if not self.binary:
            raise AdapterError(f"codex CLI not found on PATH. Install: {INSTALL_HINT}")
        self._out_file: str | None = None

    def _before_call(self) -> None:
        fd, self._out_file = tempfile.mkstemp(prefix="roundtable-codex-", suffix=".txt")
        os.close(fd)

    def _after_call(self) -> None:
        if self._out_file:
            try:
                os.unlink(self._out_file)
            except OSError:
                pass
            self._out_file = None

    def _build_command(self, first: bool) -> list[str]:
        cmd = [self.binary, "exec"]
        if self.dangerous:
            cmd += ["--sandbox", "danger-full-access"]
        elif self.writable:
            cmd += ["--sandbox", "workspace-write"]
        else:
            cmd += ["--sandbox", "read-only"]
        if not first:
            cmd.append("resume")
        cmd += ["--json", "--output-last-message", self._out_file, "--skip-git-repo-check"]
        if self.model:
            cmd += ["-m", self.model]
        if not first:
            cmd.append(self.session_id)
        cmd.append("-")  # read the prompt from stdin
        return cmd

    def _parse(self, stdout: str, stderr: str) -> str:
        text = ""
        try:
            with open(self._out_file, encoding="utf-8", errors="replace") as fh:
                text = fh.read().strip()
        except OSError:
            pass
        if not text:
            text = self._last_message_from_events(stdout)
        if not text:
            detail = (stderr or stdout or "").strip()[-1000:]
            raise AdapterError(f"codex returned no reply text. Last output:\n{detail}")

        sid = None
        usage = None
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            sid = sid or _find_key(event, SESSION_KEYS)
            usage = usage or _find_usage(event)
            if sid and usage:
                break
        if sid:
            self.session_id = sid
        elif self.session_id is None:
            raise AdapterError(
                "codex reply did not expose a session id; refusing unsafe resume --last"
            )
        self.last_usage = usage
        return text

    @staticmethod
    def _last_message_from_events(stdout: str) -> str:
        """Fallback: pull the last agent message out of the --json event stream."""
        last = ""
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            item = event.get("item") or event.get("msg") or event
            if isinstance(item, dict) and "agent_message" in str(item.get("type", "")):
                text = item.get("text") or item.get("message")
                if isinstance(text, str) and text.strip():
                    last = text.strip()
        return last
