"""Run artifacts: .roundtable/runs/<run-id>/{transcript.md,meta.json,result.md}.

transcript.md is appended after every message so a crash or Ctrl-C still
leaves the full conversation so far on disk.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path


class RunLog:
    def __init__(self, base_dir: str, task: str, mode: str, lead: str, config: dict):
        run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        self.dir = Path(base_dir) / ".roundtable" / "runs" / run_id
        self.dir.mkdir(parents=True, exist_ok=True)
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
        }
        self._transcript = self.dir / "transcript.md"
        self._transcript.write_text(
            f"# Roundtable transcript — {run_id}\n\n"
            f"- mode: {mode}\n- lead: {lead}\n\n## Task\n\n{task}\n",
            encoding="utf-8",
        )

    def add(self, speaker: str, role: str, round_no: int, text: str, duration_s: float,
            session_id: str | None = None) -> None:
        self.meta["messages"].append({
            "round": round_no,
            "speaker": speaker,
            "role": role,
            "duration_s": round(duration_s, 1),
            "session_id": session_id,
            "chars": len(text),
        })
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
        self._flush_meta()

    def verdict(self, round_no: int, verdict: str | None, score: int | None = None,
                blocking_issues: str | None = None) -> None:
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
        self.meta["warnings"].append(message)
        self._flush_meta()

    def finish(self, result_text: str, status: str) -> None:
        self.meta["status"] = status
        self.meta["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
        (self.dir / "result.md").write_text(result_text + "\n", encoding="utf-8")
        self._flush_meta()

    def _flush_meta(self) -> None:
        (self.dir / "meta.json").write_text(
            json.dumps(self.meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
