"""Participant roster and same-provider collaboration tests."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from roundtable.__main__ import build_parser, main  # noqa: E402
from roundtable.participants import build_roster  # noqa: E402


class TestRoster(unittest.TestCase):
    def test_preserves_original_default_pair(self):
        leader, reviewer = build_roster("claude")
        self.assertEqual((leader.provider, leader.name), ("claude", "claude"))
        self.assertEqual((reviewer.provider, reviewer.name), ("codex", "codex"))

    def test_same_provider_gets_distinct_participants(self):
        leader, reviewer = build_roster("codex", "codex")
        self.assertEqual(leader.name, "codex-leader")
        self.assertEqual(reviewer.name, "codex-reviewer")
        self.assertEqual(leader.provider, reviewer.provider)

    def test_role_model_overrides_provider_default(self):
        leader, reviewer = build_roster(
            "codex", "codex", lead_model="leader-model",
            provider_models={"codex": "provider-model"},
        )
        self.assertEqual(leader.model, "leader-model")
        self.assertEqual(reviewer.model, "provider-model")

    def test_custom_names(self):
        leader, reviewer = build_roster(
            "codex", "codex", lead_name="builder", reviewer_name="critic",
        )
        self.assertEqual((leader.name, reviewer.name), ("builder", "critic"))


class TestCli(unittest.TestCase):
    def test_parser_accepts_same_provider_roles(self):
        args = build_parser().parse_args([
            "task", "--lead", "codex", "--reviewer", "codex",
            "--lead-model", "m1", "--reviewer-model", "m2",
        ])
        self.assertEqual((args.lead, args.reviewer), ("codex", "codex"))
        self.assertEqual((args.lead_model, args.reviewer_model), ("m1", "m2"))

    def test_mock_run_records_distinct_codex_participants(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()), \
                patch.dict(os.environ, {"ROUNDTABLE_AUDIT_DIR": str(Path(tmp) / "audit")}):
            code = main([
                "same-provider smoke", "--lead", "codex", "--reviewer", "codex",
                "--mock", "both", "--max-rounds", "1", "--cwd", tmp,
            ])
            self.assertEqual(code, 0)
            meta_file = next((Path(tmp) / ".roundtable" / "runs").glob("*/meta.json"))
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        self.assertEqual(meta["lead"], "codex-leader")
        self.assertEqual(
            [p["name"] for p in meta["config"]["participants"]],
            ["codex-leader", "codex-reviewer"],
        )
        self.assertEqual(
            [m["speaker"] for m in meta["messages"]],
            ["codex-leader", "codex-reviewer"],
        )


if __name__ == "__main__":
    unittest.main()
