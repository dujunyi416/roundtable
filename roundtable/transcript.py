"""Run artifacts: .roundtable/runs/<run-id>/{transcript.md,meta.json,result.md}.

transcript.md is appended after every message so a crash or Ctrl-C still
leaves the full conversation so far on disk.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

from .audit import AuditMirror


class RunLog:
    def __init__(self, base_dir: str, task: str, mode: str, lead: str, config: dict,
                 audit_root: str | Path | None = None):
        run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        self.dir = Path(base_dir) / ".roundtable" / "runs" / run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._audit = AuditMirror(audit_root, run_id) if audit_root else None
        self.before_finish = None
        self.meta: dict = {
            "run_id": run_id,
            "started": time.strftime("%Y-%m-%d %H:%M:%S"),
            "task": task,
            "mode": mode,
            "lead": lead,
            "config": config,
            "messages": [],
            "verdicts": [],
            "warnings": [],
            "status": "running",
            "next_action": {"status": "running"},
        }
        self._transcript = self.dir / "transcript.md"
        self._transcript.write_text(
            f"# Roundtable transcript — {run_id}\n\n"
            f"- mode: {mode}\n- lead: {lead}\n\n## Task\n\n{task}\n",
            encoding="utf-8",
        )
        # A returned run id must be immediately discoverable, before its worker
        # produces the first message.
        self._flush_meta()

    def prompt(self, role: str, round_no: int, text: str) -> None:
        """Persist the exact bytes sent to a participant before invoking it."""
        with self._lock:
            relative = f"prompts/r{round_no}-{role}.txt"
            target = self.dir / relative
            target.parent.mkdir(exist_ok=True)
            target.write_text(text, encoding="utf-8")
            if self._audit:
                self._audit.write(relative, text)

    def artifact(self, relative_path: str, text: str) -> None:
        """Persist an additional auditable run artifact."""
        with self._lock:
            relative = Path(relative_path)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("artifact path must stay inside the run directory")
            target = self.dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            if self._audit:
                self._audit.write(relative.as_posix(), text)

    def add(self, speaker: str, role: str, round_no: int, text: str, duration_s: float,
            session_id: str | None = None, usage: dict | None = None) -> None:
        with self._lock:
            message = {
                "round": round_no,
                "speaker": speaker,
                "role": role,
                "duration_s": round(duration_s, 1),
                "session_id": session_id,
                "chars": len(text),
            }
            if usage:
                message["usage"] = usage
            self.meta["messages"].append(message)
            with self._transcript.open("a", encoding="utf-8") as fh:
                fh.write(f"\n## Round {round_no} — {speaker} ({role})\n\n{text}\n")
            # Structured mirror of the transcript, consumed by `roundtable ui`.
            with (self.dir / "messages.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "round": round_no,
                    "speaker": speaker,
                    "role": role,
                    "text": text,
                    "duration_s": round(duration_s, 1),
                    "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                }, ensure_ascii=False) + "\n")
            self._mirror("transcript.md")
            self._mirror("messages.jsonl")
            self._flush_meta()

    def verdict(self, round_no: int, verdict: str | None, score: int | None = None,
                blocking_issues: str | None = None) -> None:
        with self._lock:
            self.meta["verdicts"].append({
                "round": round_no,
                "verdict": verdict,
                "score": score,
                "blocking_issues": blocking_issues,
            })
            if verdict is None:
                self.warn(f"round {round_no}: reviewer gave no VERDICT line; treating as REVISE")
            elif score is None:
                self.warn(f"round {round_no}: reviewer gave no SCORE line")
            self._flush_meta()

    def warn(self, message: str) -> None:
        with self._lock:
            self.meta["warnings"].append(message)
            self._flush_meta()

    def control(self, state: str, phase: str | None = None,
                round_no: int | None = None) -> None:
        with self._lock:
            self.meta["control"] = {
                "state": state,
                "phase": phase,
                "round": round_no,
                "heartbeat": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            self._flush_meta()

    def heartbeat(self) -> None:
        with self._lock:
            control = self.meta.setdefault("control", {})
            control["heartbeat"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self._flush_meta()

    def finish(self, result_text: str, status: str) -> None:
        with self._lock:
            if self.before_finish:
                callback, self.before_finish = self.before_finish, None
                callback()
            self.meta["status"] = status
            self.meta["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self.meta["control"] = {"state": "finished", "phase": None, "round": None}
            (self.dir / "result.md").write_text(result_text + "\n", encoding="utf-8")
            if self.meta["mode"] == "plan":
                (self.dir / "plan.md").write_text(result_text + "\n", encoding="utf-8")
            self.meta["next_action"] = _next_action(self.meta["mode"], status)
            self._mirror("result.md")
            if self.meta["mode"] == "plan":
                self._mirror("plan.md")
            self._flush_meta()

    def _flush_meta(self) -> None:
        meta_file = self.dir / "meta.json"
        temp_file = self.dir / "meta.json.tmp"
        content = json.dumps(self.meta, ensure_ascii=False, indent=2)
        temp_file.write_text(content, encoding="utf-8")
        if self._audit:
            self._audit.write("meta.json", content)
        temp_file.replace(meta_file)

    def _mirror(self, relative_path: str) -> None:
        if not self._audit:
            return
        source = self.dir / relative_path
        if source.is_file():
            self._audit.write(relative_path, source.read_text(encoding="utf-8"))


def _next_action(mode: str, status: str) -> dict:
    if status == "needs_human_decision":
        return {"status": "needs_human_decision"}
    if status != "approved":
        return {"status": "stopped"}
    if mode == "discuss":
        return {"status": "awaiting_plan", "mode": "plan"}
    if mode == "plan":
        return {"status": "ready_to_build", "mode": "build"}
    return {"status": "completed"}
