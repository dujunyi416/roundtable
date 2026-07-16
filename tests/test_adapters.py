"""Adapter lifecycle tests."""
from __future__ import annotations

import sys
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from roundtable.adapters import Adapter, AdapterError  # noqa: E402
from roundtable.adapters.claude import ClaudeAdapter  # noqa: E402
from roundtable.adapters.codex import CodexAdapter  # noqa: E402


class ResourceBackedAdapter(Adapter):
    name = "resource-backed"

    def __init__(self):
        super().__init__()
        self.resource = None
        self.cleaned = False

    def _before_call(self):
        self.resource = "ready"

    def _build_command(self, first):
        if self.resource is None:
            raise AssertionError("command built before per-call resource exists")
        return [sys.executable, "-c", "print('reply')"]

    def _parse(self, stdout, stderr):
        return stdout.strip()

    def _after_call(self):
        self.cleaned = True


class TestAdapterLifecycle(unittest.TestCase):
    def test_prepares_resources_before_building_command(self):
        adapter = ResourceBackedAdapter()
        reply = adapter.send("prompt")
        self.assertEqual(reply.text, "reply")
        self.assertTrue(adapter.cleaned)


class TestCodexCommand(unittest.TestCase):
    def test_resume_places_sandbox_before_subcommand(self):
        with patch("roundtable.adapters.codex.resolve_binary", return_value="codex"):
            adapter = CodexAdapter(model="test-model")
        adapter._out_file = "reply.txt"
        adapter.session_id = "session-123"

        command = adapter._build_command(first=False)

        self.assertEqual(command[:5], [
            "codex", "exec", "--sandbox", "read-only", "resume",
        ])
        self.assertIn("test-model", command)
        self.assertEqual(command[-2:], ["session-123", "-"])

    def test_missing_session_id_fails_closed(self):
        with patch("roundtable.adapters.codex.resolve_binary", return_value="codex"):
            adapter = CodexAdapter()
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "reply.txt"
            output.write_text("reply", encoding="utf-8")
            adapter._out_file = str(output)
            with self.assertRaisesRegex(AdapterError, "refusing unsafe resume --last"):
                adapter._parse('{"type":"message"}', "")

    def test_codex_usage_is_captured_from_events(self):
        with patch("roundtable.adapters.codex.resolve_binary", return_value="codex"):
            adapter = CodexAdapter()
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "reply.txt"
            output.write_text("reply", encoding="utf-8")
            adapter._out_file = str(output)
            event = {"session_id": "sid", "usage": {"input_tokens": 3, "output_tokens": 2}}
            adapter._parse(json.dumps(event), "")
            self.assertEqual(adapter.last_usage["input_tokens"], 3)


class TestClaudeParsing(unittest.TestCase):
    def test_usage_is_captured(self):
        with patch("roundtable.adapters.claude.resolve_binary", return_value="claude"):
            adapter = ClaudeAdapter()
        adapter._parse(json.dumps({
            "result": "reply", "session_id": "sid", "usage": {"input_tokens": 4},
        }), "")
        self.assertEqual(adapter.last_usage, {"input_tokens": 4})


if __name__ == "__main__":
    unittest.main()
