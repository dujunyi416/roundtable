"""Web viewer tests: run scanning, run loading, and an HTTP smoke test."""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from roundtable.transcript import RunLog  # noqa: E402
from roundtable.webui import _Handler, load_run, scan_runs  # noqa: E402


def make_run(base: str, status: str = "approved") -> RunLog:
    log = RunLog(base, task="示例任务 task", mode="discuss", lead="claude", config={})
    log.add("claude", "leader", 0, "draft text", 1.2, "sid-1")
    log.add("codex", "reviewer", 1, "ok\nSCORE: 9\nVERDICT: APPROVE", 0.8, "sid-2")
    log.verdict(1, "APPROVE", 9, "none")
    log.finish("final answer", status)
    return log


class TestScanAndLoad(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def test_scan_empty(self):
        self.assertEqual(scan_runs(self.tmp), [])

    def test_scan_lists_runs_with_status(self):
        make_run(self.tmp)
        runs = scan_runs(self.tmp)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "approved")
        self.assertEqual(runs[0]["rounds"], 1)
        self.assertIn("示例任务", runs[0]["task"])

    def test_load_run_messages_and_result(self):
        log = make_run(self.tmp)
        run = load_run(self.tmp, log.meta["run_id"])
        self.assertEqual(len(run["messages"]), 2)
        self.assertEqual(run["messages"][0]["text"], "draft text")
        self.assertEqual(run["meta"]["verdicts"][0]["score"], 9)
        self.assertIn("final answer", run["result"])

    def test_load_run_fallback_to_transcript_for_old_runs(self):
        log = make_run(self.tmp)
        (log.dir / "messages.jsonl").unlink()  # simulate a pre-v0.2 run
        run = load_run(self.tmp, log.meta["run_id"])
        self.assertEqual(run["messages"], [])
        self.assertIn("draft text", run["transcript"])

    def test_load_run_rejects_bad_ids(self):
        self.assertIsNone(load_run(self.tmp, "../../etc"))
        self.assertIsNone(load_run(self.tmp, "no-such-run"))
        self.assertIsNone(load_run(self.tmp, "20260101-000000-zzzzzz"))


class TestHttp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self.log = make_run(self.tmp)
        handler = type("H", (_Handler,), {"base": self.tmp})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def _get(self, path):
        with urllib.request.urlopen(self.url + path, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8")

    def test_index_serves_page(self):
        status, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("Roundtable", body)

    def test_api_runs_and_detail(self):
        status, body = self._get("/api/runs")
        self.assertEqual(status, 200)
        runs = json.loads(body)
        self.assertEqual(runs[0]["run_id"], self.log.meta["run_id"])
        status, body = self._get("/api/runs/" + self.log.meta["run_id"])
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(body)["messages"]), 2)

    def test_unknown_paths_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/api/runs/../../secrets")
        self.assertEqual(ctx.exception.code, 404)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/nope")
        self.assertEqual(ctx.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
