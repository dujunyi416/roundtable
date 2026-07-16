"""Project Room persistence and prompt-context tests."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from roundtable.project import ProjectCatalog, ProjectRoom  # noqa: E402


class TestProjectRoom(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self.room = ProjectRoom(self.tmp)

    def test_empty_room_has_no_prompt_context(self):
        self.assertEqual(self.room.load()["schema"], 1)
        self.assertIsNone(self.room.context_text())

    def test_save_load_and_render_context(self):
        saved = self.room.save({
            "name": "Atlas", "mission": "Ship safely",
            "goals": ["first goal", "  "],
            "constraints": ["no cloud"], "decisions": ["use Codex twice"],
        })
        self.assertIsNotNone(saved["updated"])
        self.assertEqual(self.room.load()["goals"], ["first goal"])
        context = self.room.context_text()
        self.assertIn("Project: Atlas", context)
        self.assertIn("- no cloud", context)
        self.assertIn("- use Codex twice", context)


class TestProjectCatalog(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self.catalog = ProjectCatalog(self.tmp)

    def test_seeds_catalog_from_legacy_project_room(self):
        ProjectRoom(self.tmp).save({"name": "Legacy", "mission": "Keep context"})
        project = self.catalog.load()[0]
        self.assertEqual(project["id"], "default")
        self.assertEqual(project["name"], "Legacy")
        self.assertEqual(project["project_path"], str(Path(self.tmp).resolve()))
        self.assertIn("Mission:\nKeep context", self.catalog.context_text("default"))

    def test_saves_multiple_project_profiles_and_updates_by_id(self):
        first = self.catalog.save({
            "id": "default", "name": "Atlas", "project_path": self.tmp,
            "git_path": "https://example.test/atlas.git", "goals": ["ship"],
        })
        second = self.catalog.save({
            "name": "Beacon", "project_path": self.tmp, "git_path": "D:/git/beacon",
        })
        self.catalog.save({**first, "git_path": "https://example.test/new.git"})
        projects = self.catalog.load()
        self.assertEqual([project["name"] for project in projects], ["Atlas", "Beacon"])
        self.assertEqual(self.catalog.get("default")["git_path"], "https://example.test/new.git")
        self.assertEqual(self.catalog.get(second["id"])["name"], "Beacon")


if __name__ == "__main__":
    unittest.main()
