"""Local run control shared by the Web workbench and background workers."""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path

from . import __version__, orchestrator
from .audit import AuditMirror, default_audit_root
from .lease import RunLease, owner_is_dead
from .adapters import AdapterError
from .adapters.mock import MockAdapter
from .participants import build_roster, create_participant, provider_names
from .project import ProjectCatalog, ProjectError
from .transcript import RunLog


class RequestError(ValueError):
    pass


class RunCancelled(RuntimeError):
    pass


class SessionControl:
    def __init__(self, log: RunLog, human_gate: bool = False,
                 lease: RunLease | None = None):
        self.log = log
        self.human_gate = human_gate
        self._condition = threading.Condition()
        self._paused = False
        self._waiting = False
        self._cancelled = False
        self._finished = False
        self._pending: list[str] = []
        self._lease = lease
        self.phase: str | None = None
        self.round_no: int | None = None

    def checkpoint(self, phase: str, round_no: int) -> str | None:
        with self._condition:
            self.phase, self.round_no = phase, round_no
            self._waiting = self.human_gate or self._paused
            self._publish("waiting" if self._waiting else "running")
            while self._waiting and not self._cancelled:
                self._condition.wait()
            if self._cancelled:
                raise RunCancelled("cancelled by human")
            notes = self._pending
            self._pending = []
            self._publish("running")
            return "\n\n".join(notes) if notes else None

    def command(self, action: str, message: str | None = None) -> dict:
        with self._condition:
            if self._finished or self.log.meta.get("status") != "running":
                raise RequestError("run has already finished")
            if action == "pause":
                self._paused = True
                state = "pause_requested" if not self._waiting else "waiting"
            elif action == "resume":
                self._paused = False
                self._waiting = False
                state = "running"
            elif action == "intervene":
                if not message or not message.strip():
                    raise RequestError("intervene requires a non-empty message")
                self._pending.append(message.strip())
                self._paused = False
                self._waiting = False
                state = "running"
            elif action == "cancel":
                self._cancelled = True
                self._waiting = False
                state = "cancelling"
            else:
                raise RequestError(f"unknown control action: {action}")
            self._publish(state)
            self._condition.notify_all()
            return self.snapshot(state)

    def finish(self) -> None:
        self.prepare_finish()

    def prepare_finish(self) -> None:
        with self._condition:
            if self._finished:
                return
            self._finished = True
            self._waiting = False
            if self._lease:
                self._lease.close()
                self._lease = None
            self._condition.notify_all()

    def snapshot(self, state: str | None = None) -> dict:
        return {
            "state": state or ("waiting" if self._waiting else "running"),
            "phase": self.phase,
            "round": self.round_no,
            "pending_messages": len(self._pending),
        }

    def _publish(self, state: str) -> None:
        self.log.control(state, self.phase, self.round_no)


@dataclass(frozen=True)
class RunRequest:
    task: str
    mode: str = "discuss"
    lead: str = "codex"
    reviewer: str = "codex"
    style: str = "balanced"
    max_rounds: int = 3
    timeout: int = 1200
    human_gate: bool = False
    lead_name: str | None = None
    reviewer_name: str | None = None
    lead_model: str | None = None
    reviewer_model: str | None = None
    project_id: str | None = None
    parent_run_id: str | None = None
    workflow_confirmation: dict | None = None
    mock: str | None = None

    @classmethod
    def from_dict(cls, data: dict) -> "RunRequest":
        if not isinstance(data, dict):
            raise RequestError("request body must be a JSON object")
        task = data.get("task")
        if not isinstance(task, str) or not task.strip():
            raise RequestError("task is required")
        mode = data.get("mode", "discuss")
        lead = data.get("lead", "codex")
        reviewer = data.get("reviewer", "codex")
        style = data.get("style", "balanced")
        if mode not in ("discuss", "plan", "build"):
            raise RequestError("mode must be discuss, plan, or build")
        if lead not in provider_names() or reviewer not in provider_names():
            raise RequestError("unknown participant provider")
        if style not in ("balanced", "adversarial"):
            raise RequestError("style must be balanced or adversarial")
        try:
            max_rounds = int(data.get("max_rounds", 3))
            timeout = int(data.get("timeout", 1200))
        except (TypeError, ValueError) as exc:
            raise RequestError("max_rounds and timeout must be integers") from exc
        if not 1 <= max_rounds <= 20:
            raise RequestError("max_rounds must be between 1 and 20")
        if not 1 <= timeout <= 7200:
            raise RequestError("timeout must be between 1 and 7200")
        mock = data.get("mock")
        if mock not in (None, "leader", "reviewer", "both"):
            raise RequestError("invalid mock setting")
        workflow_confirmation = data.get("workflow_confirmation")
        if workflow_confirmation is not None and not isinstance(workflow_confirmation, dict):
            raise RequestError("workflow_confirmation must be an object")
        return cls(
            task=task.strip(), mode=mode, lead=lead, reviewer=reviewer, style=style,
            max_rounds=max_rounds, timeout=timeout,
            human_gate=bool(data.get("human_gate", False)),
            lead_name=_optional_text(data.get("lead_name")),
            reviewer_name=_optional_text(data.get("reviewer_name")),
            lead_model=_optional_text(data.get("lead_model")),
            reviewer_model=_optional_text(data.get("reviewer_model")),
            project_id=_optional_text(data.get("project_id")),
            parent_run_id=_optional_text(data.get("parent_run_id")),
            workflow_confirmation=workflow_confirmation,
            mock=mock,
        )


def _optional_text(value) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


RUN_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")


class RunController:
    def __init__(self, base: str, audit_root: str | Path | None = None):
        self.base = base
        self.audit_root = Path(audit_root) if audit_root else default_audit_root()
        self.projects = ProjectCatalog(base)
        self._controls: dict[str, SessionControl] = {}
        self._lock = threading.Lock()
        self._recover_stale_runs()

    def start(self, data: dict) -> dict:
        request = RunRequest.from_dict(data)
        lead_spec, reviewer_spec = build_roster(
            request.lead, request.reviewer,
            lead_name=request.lead_name, reviewer_name=request.reviewer_name,
            lead_model=request.lead_model, reviewer_model=request.reviewer_model,
        )
        project = self.projects.get(request.project_id)
        workspace = str(Path(project["project_path"]).expanduser().resolve())
        if not Path(workspace).is_dir():
            raise ProjectError(f"project path is not a directory: {project['project_path']}")
        project_context = self.projects.context_text(project["id"])
        config = {
            "max_rounds": request.max_rounds,
            "participants": [lead_spec.as_dict(), reviewer_spec.as_dict()],
            "human_gate": request.human_gate,
            "style": request.style,
            "version": __version__,
            "project_context": project_context,
            "project": {
                key: project[key] for key in ("id", "name", "project_path", "git_path")
            },
        }
        if request.parent_run_id:
            config["parent_run_id"] = request.parent_run_id
        if request.workflow_confirmation:
            config["workflow_confirmation"] = request.workflow_confirmation
        log = RunLog(
            self.base, request.task, request.mode, lead_spec.name, config,
            audit_root=self.audit_root,
        )
        lease = RunLease(log.dir / "owner.lock")
        log.before_finish = lease.close
        control = SessionControl(log, request.human_gate, lease)
        with self._lock:
            self._controls[log.meta["run_id"]] = control
        thread = threading.Thread(
            target=self._run,
            args=(request, lead_spec, reviewer_spec, log, control, project_context, workspace),
            name=f"roundtable-{log.meta['run_id']}", daemon=True,
        )
        thread.start()
        return {"run_id": log.meta["run_id"], "status": "running"}

    def follow_up(self, parent_run_id: str, data: dict) -> dict:
        """Start the next explicit workflow stage and preserve its lineage."""
        if not isinstance(data, dict):
            raise RequestError("request body must be a JSON object")
        parent_dir, meta = self._load_parent(parent_run_id)
        mode = data.get("mode")
        if mode not in ("plan", "build"):
            raise RequestError("follow-up mode must be plan or build")
        expected_parent_mode = "discuss" if mode == "plan" else "plan"
        if meta.get("mode") != expected_parent_mode:
            raise RequestError(f"{mode} must follow a {expected_parent_mode} run")

        parent_status = meta.get("status")
        confirmed = data.get("confirmed") is True
        if mode == "build":
            if not confirmed:
                raise RequestError("build requires explicit human confirmation")
            if "lead" not in data or "reviewer" not in data:
                raise RequestError("build requires an explicit builder and reviewer")
            if parent_status != "approved" and data.get("accept_unapproved") is not True:
                raise RequestError("the plan is not approved; explicit override is required")
            source_file = parent_dir / "plan.md"
            scope = _optional_text(data.get("scope"))
            if not scope:
                raise RequestError("build requires a concrete approved scope")
            prefix = (
                "Implement only the human-approved scope below. Complete its acceptance "
                "criteria and verification, and do not start later batches.\n\n"
                f"APPROVED SCOPE:\n{scope}\n\nEXECUTION PLAN:\n"
            )
        else:
            if parent_status not in ("approved", "needs_human_decision", "max_rounds"):
                raise RequestError("discussion has no usable final result")
            if parent_status != "approved" and not confirmed:
                raise RequestError("using an unapproved synthesis requires human confirmation")
            source_file = parent_dir / "result.md"
            prefix = (
                "Turn the following reviewed discussion into an execution-ready plan. "
                "Preserve unresolved risks and propose only the first safe build batch.\n\n"
                "SOURCE RESULT:\n"
            )
        try:
            source = source_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise RequestError(f"parent artifact is unavailable: {source_file.name}") from exc

        participants = meta.get("config", {}).get("participants", [])
        defaults = {
            "mode": mode,
            "task": prefix + source,
            "parent_run_id": parent_run_id,
            "project_id": meta.get("config", {}).get("project", {}).get("id"),
            "style": meta.get("config", {}).get("style", "balanced"),
        }
        if len(participants) >= 2:
            defaults.update({
                "lead": participants[0].get("provider"),
                "reviewer": participants[1].get("provider"),
                "lead_name": participants[0].get("name"),
                "reviewer_name": participants[1].get("name"),
                "lead_model": participants[0].get("model"),
                "reviewer_model": participants[1].get("model"),
            })
        defaults.update(data)
        defaults["mode"] = mode
        defaults["task"] = prefix + source
        defaults["parent_run_id"] = parent_run_id
        defaults["project_id"] = meta.get("config", {}).get("project", {}).get("id")
        if data.get("lead") != (participants[0].get("provider") if participants else None):
            if "lead_name" not in data:
                defaults["lead_name"] = None
            if "lead_model" not in data:
                defaults["lead_model"] = None
        if data.get("reviewer") != (participants[1].get("provider") if len(participants) > 1 else None):
            if "reviewer_name" not in data:
                defaults["reviewer_name"] = None
            if "reviewer_model" not in data:
                defaults["reviewer_model"] = None
        defaults["workflow_confirmation"] = {
            "confirmed": confirmed,
            "accepted_unapproved": data.get("accept_unapproved") is True,
            "scope": _optional_text(data.get("scope")),
        }
        started = self.start(defaults)
        meta["next_action"] = {
            "status": f"{mode}_started",
            "mode": mode,
            "child_run_id": started["run_id"],
        }
        _write_json(parent_dir / "meta.json", meta)
        self._mirror_meta(parent_run_id, meta)
        return started

    def command(self, run_id: str, data: dict) -> dict:
        with self._lock:
            control = self._controls.get(run_id)
        if control is None:
            raise RequestError("run is not controllable in this server process")
        if not isinstance(data, dict):
            raise RequestError("request body must be a JSON object")
        return control.command(data.get("action", ""), data.get("message"))

    def _load_parent(self, run_id: str) -> tuple[Path, dict]:
        if not RUN_ID_RE.fullmatch(run_id):
            raise RequestError("invalid parent run id")
        run_dir = Path(self.base) / ".roundtable" / "runs" / run_id
        try:
            meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RequestError("parent run not found") from exc
        return run_dir, meta

    def _recover_stale_runs(self) -> None:
        root = Path(self.base) / ".roundtable" / "runs"
        if not root.is_dir():
            return
        for run_dir in root.iterdir():
            meta_file = run_dir / "meta.json"
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if meta.get("status") != "running":
                continue
            death = owner_is_dead(run_dir / "owner.lock")
            status = "interrupted" if death is True else "orphaned"
            if death is False:
                continue
            meta["status"] = status
            meta["control"] = {"state": status, "phase": None, "round": None}
            meta["next_action"] = {"status": "needs_human_decision"}
            meta.setdefault("warnings", []).append(
                "server recovery found no live controller"
                + (" and acquired the previous owner lock" if death else "; death unproven")
            )
            _write_json(meta_file, meta)
            self._mirror_meta(run_dir.name, meta)

    def _mirror_meta(self, run_id: str, meta: dict) -> None:
        AuditMirror(self.audit_root, run_id).write(
            "meta.json", json.dumps(meta, ensure_ascii=False, indent=2)
        )

    def _run(self, request, lead_spec, reviewer_spec, log, control,
             project_context: str | None, workspace: str) -> None:
        heartbeat_stop = threading.Event()

        def prepare_finish() -> None:
            heartbeat_stop.set()
            control.prepare_finish()

        log.before_finish = prepare_finish

        def heartbeat() -> None:
            while not heartbeat_stop.wait(5):
                log.heartbeat()

        heartbeat_thread = threading.Thread(
            target=heartbeat, name=f"heartbeat-{log.meta['run_id']}", daemon=True,
        )
        heartbeat_thread.start()
        def make(spec):
            if request.mock in (spec.role, "both"):
                adapter = MockAdapter(cwd=workspace)
                adapter.name = spec.name
                return adapter
            return create_participant(
                spec, cwd=workspace, mode=request.mode,
                dangerous=False, timeout=request.timeout,
            )

        try:
            result = orchestrator.run(
                request.task, make(lead_spec), make(reviewer_spec), request.mode,
                request.max_rounds, log, workspace, style=request.style,
                checkpoint=control.checkpoint, project_context=project_context,
            )
            return result
        except RunCancelled:
            log.finish("", "cancelled")
        except AdapterError as exc:
            log.warn(str(exc))
            log.finish("", "error")
        except Exception as exc:  # keep the background worker auditable
            log.warn(f"unexpected worker error: {type(exc).__name__}: {exc}")
            log.finish("", "error")
        finally:
            heartbeat_stop.set()
            control.finish()


def _write_json(path: Path, value: dict) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)
