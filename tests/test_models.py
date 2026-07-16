"""Model discovery parsing tests."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from roundtable.models import _parse_claude_help, _parse_codex_response  # noqa: E402


class TestModelDiscoveryParsing(unittest.TestCase):
    def test_claude_help_exposes_documented_aliases(self):
        help_text = """
  --model <model>  Provide an alias (e.g. 'fable', 'opus', or 'sonnet') or a
                   full name (e.g. 'claude-fable-5').
  -n, --name <name>  Session name
"""
        models = _parse_claude_help(help_text)
        self.assertEqual(
            [model["value"] for model in models],
            ["fable", "opus", "sonnet", "claude-fable-5"],
        )

    def test_codex_response_uses_cli_model_value_and_marks_default(self):
        response = {"result": {"data": [
            {
                "id": "catalog-id", "model": "gpt-test", "displayName": "GPT Test",
                "description": "Test model", "hidden": False, "isDefault": True,
            },
            {
                "id": "hidden-id", "model": "gpt-hidden", "displayName": "Hidden",
                "description": "Hidden model", "hidden": True, "isDefault": False,
            },
        ]}}
        self.assertEqual(_parse_codex_response(response), [{
            "value": "gpt-test", "label": "GPT Test", "description": "Test model",
            "default": True,
        }])


if __name__ == "__main__":
    unittest.main()
