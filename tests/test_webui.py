"""Web viewer tests: run scanning, run loading, and an HTTP smoke test."""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from roundtable.transcript import RunLog  # noqa: E402
from roundtable.control import RunController  # noqa: E402
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
        self.assertEqual(runs[0]["project"]["project_path"], str(Path(self.tmp).resolve()))
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

    def test_scan_uses_validated_directory_id_not_untrusted_meta_id(self):
        log = make_run(self.tmp)
        meta_file = log.dir / "meta.json"
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        meta["run_id"] = "x');alert(1);//"
        meta_file.write_text(json.dumps(meta), encoding="utf-8")
        self.assertEqual(scan_runs(self.tmp)[0]["run_id"], log.dir.name)
        self.assertEqual(load_run(self.tmp, log.dir.name)["meta"]["run_id"], log.dir.name)

    def test_scan_ignores_noncanonical_run_directories(self):
        bad = Path(self.tmp) / ".roundtable" / "runs" / "not-a-run"
        bad.mkdir(parents=True)
        (bad / "meta.json").write_text('{"status":"approved"}', encoding="utf-8")
        self.assertEqual(scan_runs(self.tmp), [])


class TestHttp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self.log = make_run(self.tmp)
        handler = type("H", (_Handler,), {
            "base": self.tmp,
            "controller": RunController(self.tmp, audit_root=Path(self.tmp) / "audit"),
            "auth_token": "test-token",
            "csp_nonce": "test-nonce",
        })
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def _get(self, path):
        with urllib.request.urlopen(self.url + path, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8")

    def _post(self, path, data):
        req = urllib.request.Request(
            self.url + path,
            data=json.dumps(data).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Origin": self.url,
                "X-Roundtable-Token": "test-token",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def test_index_serves_page(self):
        status, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("Roundtable", body)
        self.assertIn(r"split(/\n/)", body)
        self.assertIn(r'join("\n")', body)
        self.assertIn("async function controlRun(action)", body)
        self.assertIn('id="leadModel"', body)
        self.assertIn('id="reviewerModel"', body)
        self.assertIn('list="leadModelOptions"', body)
        self.assertIn('list="reviewerModelOptions"', body)
        self.assertIn("async function loadModels()", body)
        self.assertIn('lead_model:document.getElementById("leadModel").value', body)
        self.assertIn('reviewer_model:document.getElementById("reviewerModel").value', body)
        self.assertIn('id="pPath"', body)
        self.assertIn('id="pGit"', body)
        self.assertIn('id="runProject"', body)
        self.assertIn('project_id:document.getElementById("runProject").value', body)
        self.assertIn('id="runFolderButton"', body)
        self.assertIn('id="projectFolderButton"', body)
        self.assertIn('className="project-group"', body)
        self.assertNotIn("onclick=", body)
        self.assertIn('const authToken="test-token"', body)

    def test_api_runs_and_detail(self):
        status, body = self._get("/api/runs")
        self.assertEqual(status, 200)
        runs = json.loads(body)
        self.assertEqual(runs[0]["run_id"], self.log.meta["run_id"])
        status, body = self._get("/api/runs/" + self.log.meta["run_id"])
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(body)["messages"]), 2)

    def test_api_models(self):
        catalog = {
            "codex": {"source": "account", "models": [{"value": "gpt-test"}]},
            "claude": {"source": "cli aliases", "models": [{"value": "sonnet"}]},
        }
        with patch("roundtable.webui.discover_models", return_value=catalog):
            status, body = self._get("/api/models")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), catalog)

    def test_unknown_paths_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/api/runs/../../secrets")
        self.assertEqual(ctx.exception.code, 404)
        ctx.exception.close()

    def test_rejects_bad_host_on_reads(self):
        req = urllib.request.Request(self.url + "/api/runs", headers={"Host": "evil.test"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 403)
        ctx.exception.close()

    def test_write_guard_rejects_missing_token_wrong_origin_and_text_plain(self):
        cases = [
            ({"Content-Type": "application/json", "Origin": self.url}, 403),
            ({"Content-Type": "application/json", "Origin": "https://evil.test",
              "X-Roundtable-Token": "test-token"}, 403),
            ({"Content-Type": "text/plain", "Origin": self.url,
              "X-Roundtable-Token": "test-token"}, 415),
        ]
        for headers, expected in cases:
            with self.subTest(expected=expected, headers=headers):
                req = urllib.request.Request(
                    self.url + "/api/runs", data=b"{}", headers=headers, method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(req, timeout=10)
                self.assertEqual(ctx.exception.code, expected)
                ctx.exception.close()

    def test_security_headers_and_cross_origin_preflight(self):
        with urllib.request.urlopen(self.url + "/", timeout=10) as response:
            self.assertIn("nonce-test-nonce", response.headers["Content-Security-Policy"])
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        req = urllib.request.Request(
            self.url + "/api/runs", headers={"Origin": "https://evil.test"},
            method="OPTIONS",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 403)
        ctx.exception.close()
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/nope")
        self.assertEqual(ctx.exception.code, 404)
        ctx.exception.close()

    def test_create_and_control_run_endpoints(self):
        status, started = self._post("/api/runs", {
            "task": "from web", "lead": "codex", "reviewer": "codex",
            "lead_model": "leader-model", "reviewer_model": "reviewer-model",
            "mock": "both", "human_gate": True,
        })
        self.assertEqual(status, 201)
        run_id = started["run_id"]
        deadline = time.time() + 5
        while time.time() < deadline:
            _, body = self._get("/api/runs/" + run_id)
            run = json.loads(body)
            if run["meta"].get("control", {}).get("state") == "waiting":
                break
            time.sleep(0.02)
        else:
            self.fail("web-created run did not reach human gate")
        participants = run["meta"]["config"]["participants"]
        self.assertEqual(
            [participant["model"] for participant in participants],
            ["leader-model", "reviewer-model"],
        )
        status, control = self._post(f"/api/runs/{run_id}/control", {"action": "cancel"})
        self.assertEqual(status, 200)
        self.assertEqual(control["state"], "cancelling")

    def test_project_room_endpoints(self):
        status, saved = self._post("/api/project", {
            "name": "Web project", "mission": "Collaborate",
            "goals": ["goal A"], "constraints": [], "decisions": ["decision B"],
        })
        self.assertEqual(status, 200)
        self.assertEqual(saved["name"], "Web project")
        status, body = self._get("/api/project")
        self.assertEqual(status, 200)
        loaded = json.loads(body)
        self.assertEqual(loaded["decisions"], ["decision B"])

    def test_project_catalog_endpoints(self):
        status, projects = self._get("/api/projects")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(projects)[0]["project_path"], str(Path(self.tmp).resolve()))
        status, saved = self._post("/api/projects", {
            "id": "default", "name": "Web project", "project_path": self.tmp,
            "git_path": "https://example.test/web.git", "mission": "Collaborate",
        })
        self.assertEqual(status, 200)
        self.assertEqual(saved["git_path"], "https://example.test/web.git")
        _, projects = self._get("/api/projects")
        self.assertEqual(json.loads(projects)[0]["name"], "Web project")

    def test_folder_picker_endpoint(self):
        with patch("roundtable.webui.choose_directory", return_value=self.tmp) as picker:
            status, selected = self._post("/api/folders/select", {
                "initial_path": "C:/starting/place",
            })
        self.assertEqual(status, 200)
        self.assertEqual(selected["path"], self.tmp)
        picker.assert_called_once_with("C:/starting/place")

    def test_cancelled_folder_picker_returns_null(self):
        with patch("roundtable.webui.choose_directory", return_value=None):
            status, selected = self._post("/api/folders/select", {})
        self.assertEqual(status, 200)
        self.assertIsNone(selected["path"])


if __name__ == "__main__":
    unittest.main()
