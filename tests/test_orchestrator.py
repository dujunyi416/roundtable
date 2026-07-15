"""Round-loop tests driven by the scripted MockAdapter (no real CLIs needed).

Run with either:  python -m unittest discover tests   |   python -m pytest
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from roundtable.adapters.mock import MockAdapter  # noqa: E402
from roundtable.orchestrator import parse_verdict, run  # noqa: E402
from roundtable.transcript import RunLog  # noqa: E402

import tempfile  # noqa: E402


def _run(tmp: str, leader_replies, reviewer_replies, max_rounds=3, mode="discuss"):
    leader = MockAdapter(replies=leader_replies)
    leader.name = "claude"
    reviewer = MockAdapter(replies=reviewer_replies)
    reviewer.name = "codex"
    log = RunLog(tmp, task="TASK-TEXT-测试", mode=mode, lead="claude", config={})
    result = run("TASK-TEXT-测试", leader, reviewer, mode, max_rounds, log, cwd=tmp)
    return result, leader, reviewer, log


class TestVerdictParsing(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(parse_verdict("looks fine\nVERDICT: APPROVE"), "APPROVE")

    def test_markdown_bold_and_lowercase(self):
        self.assertEqual(parse_verdict("bad\n**Verdict: revise**"), "REVISE")

    def test_fullwidth_colon_and_approved(self):
        self.assertEqual(parse_verdict("VERDICT：APPROVED"), "APPROVE")

    def test_last_occurrence_wins(self):
        text = "I would normally say\nVERDICT: APPROVE\nbut actually\nVERDICT: REVISE"
        self.assertEqual(parse_verdict(text), "REVISE")

    def test_missing(self):
        self.assertIsNone(parse_verdict("no verdict anywhere"))

    def test_not_matched_mid_sentence(self):
        self.assertIsNone(parse_verdict("the verdict: approve pattern must start a line x"))


class TestRoundLoop(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def _artifacts(self, log):
        return (
            (log.dir / "transcript.md").read_text(encoding="utf-8"),
            json.loads((log.dir / "meta.json").read_text(encoding="utf-8")),
            (log.dir / "result.md").read_text(encoding="utf-8"),
        )

    def test_approve_first_round(self):
        result, leader, reviewer, log = _run(
            self.tmp, ["DRAFT-1"], ["fine.\nVERDICT: APPROVE"]
        )
        self.assertEqual(result.status, "approved")
        self.assertEqual(result.rounds, 1)
        self.assertEqual(result.final_text, "DRAFT-1")
        self.assertEqual(len(leader.prompts), 1)  # kickoff only, no synthesis call
        transcript, meta, result_md = self._artifacts(log)
        self.assertIn("DRAFT-1", transcript)
        self.assertIn("VERDICT: APPROVE", transcript)
        self.assertEqual(meta["status"], "approved")
        self.assertIn("DRAFT-1", result_md)

    def test_revise_then_approve(self):
        result, leader, reviewer, _ = _run(
            self.tmp,
            ["DRAFT-1", "DRAFT-2"],
            ["wrong.\nVERDICT: REVISE", "better.\nVERDICT: APPROVE"],
        )
        self.assertEqual(result.status, "approved")
        self.assertEqual(result.rounds, 2)
        self.assertEqual(result.final_text, "DRAFT-2")
        # the revision prompt must contain the review verbatim
        self.assertIn("wrong.\nVERDICT: REVISE", leader.prompts[1])

    def test_max_rounds_forces_synthesis(self):
        result, leader, reviewer, log = _run(
            self.tmp,
            ["DRAFT-1", "DRAFT-2", "FINAL-SYNTHESIS"],
            ["no.\nVERDICT: REVISE", "still no.\nVERDICT: REVISE"],
            max_rounds=2,
        )
        self.assertEqual(result.status, "max_rounds")
        self.assertEqual(result.rounds, 2)
        self.assertEqual(result.final_text, "FINAL-SYNTHESIS")
        self.assertEqual(len(leader.prompts), 3)  # kickoff + 1 revision + synthesis
        self.assertEqual(len(reviewer.prompts), 2)

    def test_missing_verdict_treated_as_revise_with_warning(self):
        result, _, _, log = _run(
            self.tmp,
            ["DRAFT-1", "SYNTH"],
            ["I forgot the verdict line entirely."],
            max_rounds=1,
        )
        self.assertEqual(result.status, "max_rounds")
        _, meta, _ = self._artifacts(log)
        self.assertTrue(any("no VERDICT" in w for w in meta["warnings"]))

    def test_task_passed_verbatim_to_both(self):
        _, leader, reviewer, _ = _run(self.tmp, ["D"], ["ok\nVERDICT: APPROVE"])
        self.assertIn("TASK-TEXT-测试", leader.prompts[0])
        self.assertIn("TASK-TEXT-测试", reviewer.prompts[0])
        self.assertIn("D", reviewer.prompts[0])  # draft relayed verbatim


if __name__ == "__main__":
    unittest.main()
