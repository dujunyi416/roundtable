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
from roundtable.orchestrator import (  # noqa: E402
    parse_blocking_issues,
    parse_score,
    parse_verdict,
    run,
)
from roundtable.transcript import RunLog  # noqa: E402

import tempfile  # noqa: E402


def _run(tmp: str, leader_replies, reviewer_replies, max_rounds=3, mode="discuss",
         style="balanced"):
    leader = MockAdapter(replies=leader_replies)
    leader.name = "claude"
    reviewer = MockAdapter(replies=reviewer_replies)
    reviewer.name = "codex"
    log = RunLog(tmp, task="TASK-TEXT-测试", mode=mode, lead="claude", config={})
    result = run("TASK-TEXT-测试", leader, reviewer, mode, max_rounds, log, cwd=tmp,
                 style=style)
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


class TestScoreAndBlockingParsing(unittest.TestCase):
    REVIEW = (
        "Solid overall but two problems.\n\n"
        "SCORE: 7\n"
        "BLOCKING ISSUES:\n1. missing error handling\n2. off-by-one in loop\n"
        "VERDICT: REVISE"
    )

    def test_score_plain_and_decorated(self):
        self.assertEqual(parse_score(self.REVIEW), 7)
        self.assertEqual(parse_score("**Score**: 9/10"), 9)
        self.assertEqual(parse_score("SCORE：10"), 10)

    def test_score_missing_or_out_of_range(self):
        self.assertIsNone(parse_score("no score here"))
        self.assertIsNone(parse_score("SCORE: 42"))

    def test_blocking_issues_captured_up_to_verdict(self):
        issues = parse_blocking_issues(self.REVIEW)
        self.assertIn("missing error handling", issues)
        self.assertIn("off-by-one", issues)
        self.assertNotIn("VERDICT", issues)

    def test_blocking_issues_none_literal(self):
        self.assertEqual(parse_blocking_issues("BLOCKING ISSUES: none\nVERDICT: APPROVE"),
                         "none")

    def test_blocking_issues_missing(self):
        self.assertIsNone(parse_blocking_issues("just chatter"))


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

    def test_structured_verdict_recorded_in_meta(self):
        _, _, _, log = _run(
            self.tmp, ["DRAFT", "DRAFT-2"],
            ["weak.\nSCORE: 6\nBLOCKING ISSUES:\n1. issue-A\nVERDICT: REVISE",
             "good.\nSCORE: 9\nBLOCKING ISSUES: none\nVERDICT: APPROVE"],
        )
        _, meta, _ = self._artifacts(log)
        self.assertEqual(meta["verdicts"][0]["score"], 6)
        self.assertIn("issue-A", meta["verdicts"][0]["blocking_issues"])
        self.assertEqual(meta["verdicts"][1]["verdict"], "APPROVE")
        self.assertEqual(meta["verdicts"][1]["score"], 9)

    def test_messages_jsonl_mirrors_transcript(self):
        _, _, _, log = _run(self.tmp, ["DRAFT-1"], ["ok\nSCORE: 9\nVERDICT: APPROVE"])
        lines = (log.dir / "messages.jsonl").read_text(encoding="utf-8").splitlines()
        records = [json.loads(line) for line in lines]
        self.assertEqual(len(records), 2)  # leader draft + reviewer approval
        self.assertEqual(records[0]["role"], "leader")
        self.assertEqual(records[0]["text"], "DRAFT-1")
        self.assertEqual(records[1]["role"], "reviewer")

    def test_adversarial_style_reaches_reviewer_prompt(self):
        _, _, reviewer, _ = _run(self.tmp, ["D"], ["ok\nVERDICT: APPROVE"],
                                 style="adversarial")
        self.assertIn("ADVERSARIAL", reviewer.prompts[0])
        _, _, reviewer2, _ = _run(self.tmp, ["D"], ["ok\nVERDICT: APPROVE"])
        self.assertNotIn("ADVERSARIAL", reviewer2.prompts[0])


if __name__ == "__main__":
    unittest.main()
