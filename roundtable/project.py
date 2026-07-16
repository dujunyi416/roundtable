"""Persistent Project Room shared across Roundtable runs."""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path


class ProjectError(ValueError):
    pass


DEFAULT_PROJECT = {
    "schema": 1,
    "name": "",
    "mission": "",
    "goals": [],
    "constraints": [],
    "decisions": [],
    "updated": None,
}

DEFAULT_CATALOG_PROJECT = {
    **DEFAULT_PROJECT,
    "id": "",
    "project_path": "",
    "git_path": "",
}

_PROJECT_LOCK = threading.RLock()


class ProjectRoom:
    def __init__(self, base: str):
        self.path = Path(base) / ".roundtable" / "project.json"
        self._lock = _PROJECT_LOCK

    def load(self) -> dict:
        with self._lock:
            if not self.path.is_file():
                return self._normalize({})
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ProjectError(f"cannot read project room: {exc}") from exc
            return self._normalize(raw, preserve_updated=True)

    def save(self, data: dict) -> dict:
        project = self._normalize(data)
        project["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".json.tmp")
            temp.write_text(
                json.dumps(project, ensure_ascii=False, indent=2), encoding="utf-8",
            )
            temp.replace(self.path)
        return project

    def context_text(self) -> str | None:
        return project_context(self.load())

    @staticmethod
    def _normalize(data, preserve_updated: bool = False) -> dict:
        if not isinstance(data, dict):
            raise ProjectError("project room must be a JSON object")
        project = dict(DEFAULT_PROJECT)
        for key in ("name", "mission"):
            value = data.get(key, "")
            if not isinstance(value, str):
                raise ProjectError(f"{key} must be text")
            project[key] = value.strip()[:20_000]
        for key in ("goals", "constraints", "decisions"):
            value = data.get(key, [])
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise ProjectError(f"{key} must be a list of text items")
            project[key] = [item.strip()[:5_000] for item in value if item.strip()][:200]
        if preserve_updated:
            project["updated"] = data.get("updated")
        return project


def project_context(project: dict) -> str | None:
    """Render one project record into the prompt context shared by participants."""
    sections = []
    if project["name"]:
        sections.append(f"Project: {project['name']}")
    if project["mission"]:
        sections.append(f"Mission:\n{project['mission']}")
    for label, key in (
        ("Active goals", "goals"),
        ("Constraints", "constraints"),
        ("Decisions already made", "decisions"),
    ):
        if project[key]:
            sections.append(label + ":\n" + "\n".join(f"- {item}" for item in project[key]))
    return "\n\n".join(sections) if sections else None


class ProjectCatalog:
    """Persistent project profiles used by the browser workbench."""

    def __init__(self, base: str):
        self.base = str(Path(base).resolve())
        self.path = Path(base) / ".roundtable" / "projects.json"
        self._lock = _PROJECT_LOCK

    def load(self) -> list[dict]:
        with self._lock:
            if not self.path.is_file():
                legacy = ProjectRoom(self.base).load()
                return [self._normalize({
                    **legacy,
                    "id": "default",
                    "name": legacy["name"] or Path(self.base).name,
                    "project_path": self.base,
                    "git_path": "",
                }, preserve_updated=True)]
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ProjectError(f"cannot read project catalog: {exc}") from exc
            if not isinstance(raw, dict) or not isinstance(raw.get("projects"), list):
                raise ProjectError("project catalog must contain a projects list")
            return [self._normalize(item, preserve_updated=True) for item in raw["projects"]]

    def get(self, project_id: str | None) -> dict:
        projects = self.load()
        if project_id is None and projects:
            return projects[0]
        for project in projects:
            if project["id"] == project_id:
                return project
        raise ProjectError("project not found")

    def save(self, data: dict) -> dict:
        project = self._normalize(data)
        project["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            projects = self.load()
            for index, current in enumerate(projects):
                if current["id"] == project["id"]:
                    projects[index] = project
                    break
            else:
                projects.append(project)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".json.tmp")
            temp.write_text(json.dumps({
                "schema": 1,
                "projects": projects,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(self.path)
        return project

    def context_text(self, project_id: str | None) -> str | None:
        return project_context(self.get(project_id))

    @staticmethod
    def _normalize(data, preserve_updated: bool = False) -> dict:
        project = ProjectRoom._normalize(data, preserve_updated=preserve_updated)
        project_id = data.get("id")
        if project_id in (None, ""):
            project_id = uuid.uuid4().hex[:12]
        if not isinstance(project_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", project_id):
            raise ProjectError("project id is invalid")
        project.update({"id": project_id})
        for key in ("project_path", "git_path"):
            value = data.get(key, "")
            if not isinstance(value, str):
                raise ProjectError(f"{key} must be text")
            project[key] = value.strip()[:32_000]
        if not project["name"]:
            raise ProjectError("name is required")
        if not project["project_path"]:
            raise ProjectError("project_path is required")
        return {key: project[key] for key in DEFAULT_CATALOG_PROJECT}
