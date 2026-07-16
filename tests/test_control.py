"""Background run control and human-gate tests."""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from roundtable.control import RequestError, RunController  # noqa: E402
from roundtable.project import ProjectCatalog, ProjectError  # noqa: E402
from roundtable.lease import RunLease  # noqa: E402
from roundtable.transcript import RunLog  # noqa: E402
from roundtable.webui import load_run  # noqa: E402


class TestRunController(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self.controller = RunController(self.tmp, audit_root=Path(self.tmp) / "audit")

    def wait_for(self, run_id, predicate, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            run = load_run(self.tmp, run_id)
            if run and predicate(run):
                return run
            time.sleep(0.02)
        self.fail(f"run {run_id} did not reach expected state")

    def test_human_gated_run_accepts_intervention_and_resume(self):
        started = self.controller.start({
            "task": "gated", "lead": "codex", "reviewer": "codex",
            "mock": "both", "human_gate": True,
        })
        run_id = started["run_id"]
        self.wait_for(run_id, lambda r: r["meta"].get("control", {}).get("phase") == "after_leader")
        self.controller.command(run_id, {
            "action": "intervene", "message": "human guidance",
        })
        self.wait_for(run_id, lambda r: r["meta"].get("control", {}).get("phase") == "after_reviewer")
        self.controller.command(run_id, {"action": "resume"})
        run = self.wait_for(run_id, lambda r: r["meta"]["status"] == "approved")
        human = [m for m in run["messages"] if m["role"] == "human"]
        self.assertEqual([m["text"] for m in human], ["human guidance"])
        with self.assertRaisesRegex(RequestError, "already finished"):
            self.controller.command(run_id, {"action": "resume"})

    def test_cancel_at_human_gate(self):
        run_id = self.controller.start({
            "task": "cancel", "mock": "both", "human_gate": True,
        })["run_id"]
        self.wait_for(run_id, lambda r: r["meta"].get("control", {}).get("state") == "waiting")
        self.controller.command(run_id, {"action": "cancel"})
        run = self.wait_for(run_id, lambda r: r["meta"]["status"] == "cancelled")
        self.assertEqual(run["meta"]["control"]["state"], "finished")

    def test_selected_project_sets_workspace_metadata(self):
        workspace = Path(self.tmp) / "workspace"
        workspace.mkdir()
        ProjectCatalog(self.tmp).save({
            "id": "default", "name": "Workspace", "project_path": str(workspace),
            "git_path": "https://example.test/workspace.git",
        })
        run_id = self.controller.start({
            "task": "selected project", "project_id": "default",
            "mock": "both", "human_gate": True,
        })["run_id"]
        run = self.wait_for(run_id, lambda r: r["meta"].get("control", {}).get("state") == "waiting")
        project = run["meta"]["config"]["project"]
        self.assertEqual(project["name"], "Workspace")
        self.assertEqual(project["project_path"], str(workspace))
        self.controller.command(run_id, {"action": "cancel"})
        self.wait_for(run_id, lambda r: r["meta"]["status"] == "cancelled")

    def test_rejects_missing_project_directory(self):
        ProjectCatalog(self.tmp).save({
            "id": "default", "name": "Missing",
            "project_path": str(Path(self.tmp) / "missing"),
        })
        with self.assertRaisesRegex(ProjectError, "not a directory"):
            self.controller.start({"task": "bad path", "mock": "both"})

    def test_discuss_plan_build_follow_up_records_lineage_and_confirmation(self):
        discuss_id = self.controller.start({
            "task": "find improvements", "mock": "both",
            "lead": "codex", "reviewer": "claude",
        })["run_id"]
        self.wait_for(discuss_id, lambda r: r["meta"]["status"] == "approved")

        plan_id = self.controller.follow_up(discuss_id, {
            "mode": "plan", "confirmed": True, "mock": "both",
        })["run_id"]
        plan = self.wait_for(plan_id, lambda r: r["meta"]["status"] == "approved")
        self.assertTrue((Path(self.tmp) / ".roundtable" / "runs" / plan_id / "plan.md").is_file())
        self.assertEqual(plan["meta"]["config"]["parent_run_id"], discuss_id)

        build_id = self.controller.follow_up(plan_id, {
            "mode": "build", "confirmed": True, "scope": "first batch",
            "lead": "codex", "reviewer": "claude", "mock": "both",
        })["run_id"]
        build = self.wait_for(build_id, lambda r: r["meta"]["status"] == "approved")
        confirmation = build["meta"]["config"]["workflow_confirmation"]
        self.assertEqual(confirmation["scope"], "first batch")
        self.assertTrue(confirmation["confirmed"])

    def test_build_follow_up_requires_confirmation_assignment_and_scope(self):
        plan_id = self.controller.start({
            "task": "plan", "mode": "plan", "mock": "both",
        })["run_id"]
        self.wait_for(plan_id, lambda r: r["meta"]["status"] == "approved")
        with self.assertRaisesRegex(RequestError, "confirmation"):
            self.controller.follow_up(plan_id, {"mode": "build"})
        with self.assertRaisesRegex(RequestError, "builder and reviewer"):
            self.controller.follow_up(plan_id, {"mode": "build", "confirmed": True})
        with self.assertRaisesRegex(RequestError, "scope"):
            self.controller.follow_up(plan_id, {
                "mode": "build", "confirmed": True,
                "lead": "codex", "reviewer": "claude",
            })

    def test_recovery_distinguishes_legacy_orphan_from_proven_dead_owner(self):
        legacy = RunLog(self.tmp, "legacy", "discuss", "codex", {})
        dead = RunLog(self.tmp, "dead", "discuss", "codex", {})
        (dead.dir / "owner.lock").write_bytes(b"0")

        RunController(self.tmp, audit_root=Path(self.tmp) / "audit")

        legacy_run = load_run(self.tmp, legacy.meta["run_id"])
        dead_run = load_run(self.tmp, dead.meta["run_id"])
        self.assertEqual(legacy_run["meta"]["status"], "orphaned")
        self.assertEqual(dead_run["meta"]["status"], "interrupted")

    def test_recovery_leaves_actively_locked_run_untouched(self):
        log = RunLog(self.tmp, "active", "discuss", "codex", {})
        lease = RunLease(log.dir / "owner.lock")
        self.addCleanup(lease.close)
        RunController(self.tmp, audit_root=Path(self.tmp) / "audit")
        run = load_run(self.tmp, log.meta["run_id"])
        self.assertEqual(run["meta"]["status"], "running")


if __name__ == "__main__":
    unittest.main()
