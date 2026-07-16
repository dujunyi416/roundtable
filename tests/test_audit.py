"""Authoritative audit mirror tests."""
from __future__ import annotations

import hashlib
import hmac
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from roundtable.transcript import RunLog  # noqa: E402


class TestAuditMirror(unittest.TestCase):
    def test_mirrors_prompts_messages_result_and_valid_hmac_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = Path(tmp) / "outside" / "audit"
            log = RunLog(
                str(Path(tmp) / "workspace"), "task", "plan", "leader", {},
                audit_root=audit,
            )
            log.prompt("leader", 0, "exact prompt")
            log.add("leader", "leader", 0, "answer", 1.0, "session")
            log.finish("execution plan", "approved")

            mirror = audit / log.meta["run_id"]
            self.assertEqual((mirror / "prompts" / "r0-leader.txt").read_text(encoding="utf-8"),
                             "exact prompt")
            self.assertIn("answer", (mirror / "messages.jsonl").read_text(encoding="utf-8"))
            self.assertEqual((mirror / "plan.md").read_text(encoding="utf-8").strip(),
                             "execution plan")

            key = (audit.parent / "audit.key").read_bytes()
            previous = "0" * 64
            for line in (mirror / "audit.jsonl").read_text(encoding="utf-8").splitlines():
                event = json.loads(line)
                signature = event.pop("hmac")
                self.assertEqual(event["previous"], previous)
                canonical = json.dumps(event, sort_keys=True, separators=(",", ":"))
                expected = hmac.new(key, canonical.encode("utf-8"), hashlib.sha256).hexdigest()
                self.assertEqual(signature, expected)
                previous = signature


if __name__ == "__main__":
    unittest.main()
