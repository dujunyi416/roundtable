"""Discover model choices exposed by the installed provider CLIs."""
from __future__ import annotations

import json
import queue
import re
import subprocess
import threading
import time

from .adapters import resolve_binary


def _parse_claude_help(help_text: str) -> list[dict]:
    match = re.search(
        r"--model <model>(.*?)(?=\n\s+(?:-\w,\s+)?--[a-z])",
        help_text,
        re.DOTALL,
    )
    if not match:
        return []
    values = list(dict.fromkeys(re.findall(r"'([a-z][a-z0-9.-]+)'", match.group(1))))
    return [{
        "value": value,
        "label": value,
        "description": "Claude CLI documented alias or model name",
        "default": False,
    } for value in values]


def _parse_codex_response(response: dict) -> list[dict]:
    data = response.get("result", {}).get("data", [])
    return [{
        "value": item["model"],
        "label": item.get("displayName") or item["model"],
        "description": item.get("description", ""),
        "default": bool(item.get("isDefault")),
    } for item in data
        if isinstance(item, dict) and item.get("model") and not item.get("hidden")]


def _claude_models(timeout: float) -> dict:
    binary = resolve_binary(["claude"], "ROUNDTABLE_CLAUDE_BIN")
    if not binary:
        raise RuntimeError("Claude CLI not found")
    proc = subprocess.run(
        [binary, "--help"], capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout,
    )
    models = _parse_claude_help(proc.stdout)
    if proc.returncode != 0 or not models:
        raise RuntimeError("Claude CLI did not publish model aliases")
    return {
        "source": "Claude CLI 公布的别名（非账户枚举）",
        "models": models,
        "warning": "Claude.ai 登录没有公开模型列表接口；可手动输入完整模型名。",
    }


def _codex_models(timeout: float) -> dict:
    binary = resolve_binary(["codex"], "ROUNDTABLE_CODEX_BIN")
    if not binary:
        raise RuntimeError("Codex CLI not found")
    proc = subprocess.Popen(
        [binary, "app-server", "--stdio"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    responses: queue.Queue[str | None] = queue.Queue()

    def read_stdout() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            responses.put(line)
        responses.put(None)

    threading.Thread(target=read_stdout, daemon=True).start()

    def send(message: dict) -> None:
        if proc.stdin is None:
            raise RuntimeError("Codex app-server stdin unavailable")
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()

    def wait_for(request_id: int) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Codex model discovery timed out")
            try:
                line = responses.get(timeout=remaining)
            except queue.Empty as exc:
                raise TimeoutError("Codex model discovery timed out") from exc
            if line is None:
                raise RuntimeError("Codex app-server stopped before replying")
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") == request_id:
                if "error" in message:
                    raise RuntimeError("Codex app-server rejected model discovery")
                return message

    try:
        send({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "clientInfo": {"name": "roundtable", "version": "0"},
                "capabilities": {"experimentalApi": True},
            },
        })
        wait_for(1)
        send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        send({
            "jsonrpc": "2.0", "id": 2, "method": "model/list",
            "params": {"includeHidden": False, "limit": 100},
        })
        models = _parse_codex_response(wait_for(2))
        if not models:
            raise RuntimeError("Codex account returned no models")
        return {"source": "Codex 当前账户", "models": models}
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)


def discover_models(timeout: float = 10) -> dict:
    catalog = {}
    for provider, discover in (("claude", _claude_models), ("codex", _codex_models)):
        try:
            catalog[provider] = discover(timeout)
        except (OSError, RuntimeError, TimeoutError, subprocess.SubprocessError) as exc:
            catalog[provider] = {
                "source": "unavailable", "models": [], "warning": str(exc),
            }
    return catalog
