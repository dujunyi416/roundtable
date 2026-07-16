"""Adapter layer: one class per AI CLI, all speaking the same tiny interface.

An Adapter is a stateful conversation with one AI agent. The first send()
opens a session with the underlying CLI; subsequent send() calls resume that
same session, so each agent keeps its own full memory of the discussion.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass


class AdapterError(RuntimeError):
    """An underlying CLI call failed (missing binary, timeout, non-zero exit)."""


@dataclass
class Reply:
    text: str
    duration_s: float = 0.0
    session_id: str | None = None
    usage: dict | None = None


def resolve_binary(candidates: list[str], env_var: str) -> str | None:
    """Find a CLI binary: env var override first, then PATH lookup.

    On Windows shutil.which() resolves npm shims to their full .cmd path,
    which subprocess needs to launch them without a shell.
    """
    override = os.environ.get(env_var)
    if override:
        return shutil.which(override) or override
    for name in candidates:
        path = shutil.which(name)
        if path:
            return path
    return None


class Adapter:
    """Base class. Subclasses implement _build_command() and _parse()."""

    name = "agent"

    def __init__(
        self,
        cwd: str = ".",
        model: str | None = None,
        writable: bool = False,
        dangerous: bool = False,
        timeout: int = 1200,
    ):
        self.cwd = cwd
        self.model = model
        self.writable = writable
        self.dangerous = dangerous
        self.timeout = timeout
        self.session_id: str | None = None
        self.last_usage: dict | None = None

    def _build_command(self, first: bool) -> list[str]:
        raise NotImplementedError

    def _parse(self, stdout: str, stderr: str) -> str:
        """Extract the reply text; update self.session_id as a side effect."""
        raise NotImplementedError

    def _before_call(self) -> None:
        """Hook for per-call setup (e.g. temp files)."""

    def _after_call(self) -> None:
        """Hook for per-call cleanup. Always runs."""

    def send(self, prompt: str) -> Reply:
        start = time.monotonic()
        try:
            # Per-call resources must exist before the command references them.
            # Codex, for example, passes its temporary output file on the CLI.
            self._before_call()
            cmd = self._build_command(first=self.session_id is None)
            try:
                proc = subprocess.run(
                    cmd,
                    input=prompt,
                    cwd=self.cwd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.timeout,
                )
            except FileNotFoundError as exc:
                raise AdapterError(f"{self.name}: cannot launch {cmd[0]!r}: {exc}") from exc
            except subprocess.TimeoutExpired as exc:
                raise AdapterError(
                    f"{self.name}: no reply within {self.timeout}s (--timeout to raise the limit)"
                ) from exc
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or "").strip()[-2000:]
                raise AdapterError(
                    f"{self.name} exited with code {proc.returncode}:\n{detail}"
                )
            text = self._parse(proc.stdout, proc.stderr)
        finally:
            duration = time.monotonic() - start
            self._after_call()
        return Reply(
            text=text, duration_s=duration, session_id=self.session_id,
            usage=self.last_usage,
        )
