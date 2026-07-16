"""The round loop: leader drafts, reviewer verdicts, iterate until APPROVE."""
from __future__ import annotations

import fnmatch
import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import prompts
from .adapters import Adapter, Reply
from .transcript import RunLog

DECORATION = r"[>\s*_`#-]*"
COLON = r"[:\uFF1A]"
VERDICT_RE = re.compile(
    rf"(?im)^{DECORATION}verdict[\s*_`]*{COLON}[\s*_`]*(approve(?:d)?|revise)"
)
SCORE_RE = re.compile(rf"(?im)^{DECORATION}score[\s*_`]*{COLON}[\s*_`]*(\d{{1,2}})")
# Captures everything from the BLOCKING ISSUES marker up to the SCORE/VERDICT line.
BLOCKING_RE = re.compile(
    rf"(?ims)^{DECORATION}blocking\s+issues[\s*_`]*{COLON}[\s*_`]*"
    rf"(.*?)(?=^{DECORATION}(?:score|verdict)[\s*_`]*{COLON}|\Z)"
)
VERDICT_LINE_RE = re.compile(
    rf"^{DECORATION}verdict[\s*_`]*{COLON}[\s*_`]*(approve(?:d)?|revise)[\s*_`]*$",
    re.IGNORECASE,
)
SCORE_LINE_RE = re.compile(
    rf"^{DECORATION}score[\s*_`]*{COLON}[\s*_`]*(\d{{1,2}})(?:\s*/\s*10)?[\s*_`]*$",
    re.IGNORECASE,
)
BLOCKING_LINE_RE = re.compile(
    rf"^{DECORATION}blocking\s+issues[\s*_`]*{COLON}[\s*_`]*(.*)$",
    re.IGNORECASE,
)

MAX_DIFF_CHARS = 60_000
MAX_UNTRACKED_FILE_BYTES = 100_000
MAX_UNTRACKED_TOTAL_BYTES = 300_000
MAX_UNTRACKED_ENTRIES = 200
SENSITIVE_PATTERNS = (
    ".env", ".env.*", "*.pem", "*.key", "id_*", "*credential*", "*secret*",
)


@dataclass
class RunResult:
    status: str  # "approved" | "needs_human_decision"
    rounds: int
    final_text: str


def parse_verdict(text: str) -> str | None:
    matches = VERDICT_RE.findall(text)
    if not matches:
        return None
    return "APPROVE" if matches[-1].lower().startswith("approve") else "REVISE"


def parse_score(text: str) -> int | None:
    matches = SCORE_RE.findall(text)
    if not matches:
        return None
    score = int(matches[-1])
    return score if 0 <= score <= 10 else None


def parse_blocking_issues(text: str) -> str | None:
    matches = BLOCKING_RE.findall(text)
    return matches[-1].strip() or None if matches else None


@dataclass(frozen=True)
class ReviewProtocol:
    verdict: str
    score: int | None
    blocking_issues: str | None
    errors: tuple[str, ...] = ()


def parse_review_protocol(text: str) -> ReviewProtocol:
    """Atomically parse the protocol block at the very end of a review."""
    lines = text.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    while lines and lines[-1].strip() in ("```", "~~~"):
        lines.pop()
        while lines and not lines[-1].strip():
            lines.pop()
    if not lines:
        return ReviewProtocol("REVISE", None, None, ("missing protocol block",))

    verdict_match = VERDICT_LINE_RE.fullmatch(lines[-1])
    if not verdict_match:
        return ReviewProtocol(
            "REVISE", None, None,
            ("VERDICT must be the last non-empty line in a complete protocol block",),
        )
    verdict = "APPROVE" if verdict_match.group(1).lower().startswith("approve") else "REVISE"

    blocking_index = None
    blocking_match = None
    for index in range(len(lines) - 2, -1, -1):
        match = BLOCKING_LINE_RE.fullmatch(lines[index])
        if match:
            blocking_index, blocking_match = index, match
            break
    if blocking_index is None:
        return ReviewProtocol("REVISE", None, None, ("missing BLOCKING ISSUES line",))

    errors: list[str] = []
    score_index = blocking_index - 1
    while score_index >= 0 and not lines[score_index].strip():
        score_index -= 1
    score_match = SCORE_LINE_RE.fullmatch(lines[score_index]) if score_index >= 0 else None
    score = int(score_match.group(1)) if score_match else None
    if score is None:
        errors.append("SCORE must immediately precede BLOCKING ISSUES")
    elif not 1 <= score <= 10:
        errors.append("SCORE must be between 1 and 10")
        score = None

    blocking_lines = [blocking_match.group(1)] + lines[blocking_index + 1:-1]
    blocking = "\n".join(blocking_lines).strip()
    if not blocking:
        errors.append("BLOCKING ISSUES must be 'none' or a concrete list")
        blocking = None
    normalized = re.sub(r"[\s.;。；]+", "", blocking or "").lower()
    no_blockers = normalized in {"none", "无", "沒有", "没有"}
    if verdict == "APPROVE" and not no_blockers:
        errors.append("APPROVE is invalid while blocking issues remain")
    if errors:
        verdict = "REVISE"
    return ReviewProtocol(verdict, score, blocking, tuple(errors))


def _git(cwd: str, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60,
        )
        return proc.stdout if proc.returncode == 0 else ""
    except OSError:
        return ""


def build_review_material(summary: str, cwd: str, baseline: str | None = None) -> str:
    """Collect staged, unstaged, and safe untracked review material."""
    unstaged = _truncate_diff(_git(cwd, "diff"))
    staged = _truncate_diff(_git(cwd, "diff", "--cached"))
    status = _git(cwd, "status", "--short", "--untracked-files=all")
    untracked = _untracked_material(cwd, status)
    baseline_section = f"\n--- run-start content baseline ---\n{baseline}\n" if baseline else ""
    return (
        f"{summary}\n\n--- git status --short ---\n{status or '(clean)'}\n"
        f"--- git diff (staged) ---\n{staged or '(no staged diff)'}\n"
        f"--- git diff (unstaged) ---\n{unstaged or '(no unstaged diff)'}\n"
        f"--- safe untracked contents ---\n{untracked or '(no untracked files)'}"
        f"{baseline_section}"
    )


def _truncate_diff(diff: str) -> str:
    if len(diff) <= MAX_DIFF_CHARS:
        return diff
    sections = re.split(r"(?=^diff --git )", diff, flags=re.MULTILINE)
    kept: list[str] = []
    size = 0
    skipped = 0
    for section in sections:
        if not section:
            continue
        if size + len(section) > MAX_DIFF_CHARS:
            skipped += 1
            continue
        kept.append(section)
        size += len(section)
    kept.append(f"\n... [{skipped} complete file diff(s) omitted by roundtable]\n")
    return "".join(kept)


def _untracked_material(cwd: str, status: str) -> str:
    root = Path(cwd).resolve()
    remaining = MAX_UNTRACKED_TOTAL_BYTES
    sections: list[str] = []
    entries = 0
    for line in status.splitlines():
        if not line.startswith("?? "):
            continue
        entries += 1
        if entries > MAX_UNTRACKED_ENTRIES:
            sections.append(
                f"[remaining untracked files not reviewed: entry limit {MAX_UNTRACKED_ENTRIES}]"
            )
            break
        relative = line[3:].strip().strip('"').replace("\\", "/")
        path = (root / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            continue
        if not path.is_file():
            continue
        try:
            content = path.read_bytes()
        except OSError:
            continue
        digest = hashlib.sha256(content).hexdigest()
        lower = relative.lower()
        reason = None
        if lower.startswith(".roundtable/") or any(
            fnmatch.fnmatch(lower, pattern) or fnmatch.fnmatch(path.name.lower(), pattern)
            for pattern in SENSITIVE_PATTERNS
        ):
            reason = "sensitive path policy"
        elif len(content) > MAX_UNTRACKED_FILE_BYTES:
            reason = f"file exceeds {MAX_UNTRACKED_FILE_BYTES} byte limit"
        elif b"\0" in content:
            reason = "binary content"
        elif len(content) > remaining:
            reason = "total untracked content limit reached"
        if reason:
            sections.append(
                f"### {relative}\n[content not reviewed: {reason}; sha256={digest}]"
            )
            continue
        try:
            decoded = content.decode("utf-8")
        except UnicodeDecodeError:
            sections.append(
                f"### {relative}\n[content not reviewed: non-UTF-8 content; sha256={digest}]"
            )
            continue
        remaining -= len(content)
        sections.append(f"### {relative}\n```\n{decoded}\n```")
    return "\n\n".join(sections)


def run(
    task: str,
    leader: Adapter,
    reviewer: Adapter,
    mode: str,
    max_rounds: int,
    log: RunLog,
    cwd: str,
    echo=lambda speaker, role, text: None,
    style: str = "balanced",
    checkpoint: Callable[[str, int], str | None] | None = None,
    project_context: str | None = None,
) -> RunResult:
    def step(agent: Adapter, role: str, round_no: int, prompt: str) -> Reply:
        log.prompt(role, round_no, prompt)
        reply = agent.send(prompt)
        log.add(
            agent.name, role, round_no, reply.text, reply.duration_s,
            reply.session_id, reply.usage,
        )
        echo(agent.name, role, reply.text)
        return reply

    def human(phase: str, round_no: int) -> str | None:
        if checkpoint is None:
            return None
        note = checkpoint(phase, round_no)
        if note:
            log.add("human", "human", round_no, note, 0.0)
        return note

    baseline = None
    if mode == "build":
        baseline = build_review_material("State captured before the leader ran", cwd)
        log.artifact("baseline.md", baseline)

    draft = step(
        leader, "leader", 0,
        prompts.leader_kickoff(leader.name, reviewer.name, mode, task, project_context),
    )
    leader_note = human("after_leader", 0)

    approved = False
    rounds = 0
    final_review = None
    for rnd in range(1, max_rounds + 1):
        rounds = rnd
        material = draft.text
        if mode == "build":
            material = build_review_material(draft.text, cwd, baseline)
        if leader_note:
            material += "\n\n" + prompts.human_intervention(leader_note)
        if rnd == 1:
            rprompt = prompts.reviewer_first(
                reviewer.name, leader.name, mode, task, material, style, project_context,
            )
        else:
            rprompt = prompts.reviewer_next(leader.name, material, style)
        review = step(reviewer, "reviewer", rnd, rprompt)

        protocol = parse_review_protocol(review.text)
        verdict = protocol.verdict
        review_note = human("after_reviewer", rnd)
        review_for_leader = review.text
        if review_note:
            review_for_leader += "\n\n" + prompts.human_intervention(review_note)
            if verdict == "APPROVE":
                verdict = "REVISE"
                log.warn(f"round {rnd}: human intervention overrode reviewer approval")
        final_review = review_for_leader
        for error in protocol.errors:
            log.warn(f"round {rnd}: invalid review protocol: {error}")
        log.verdict(rnd, verdict, protocol.score, protocol.blocking_issues)
        if verdict == "APPROVE":
            approved = True
            break
        if rnd == max_rounds:
            break
        draft = step(
            leader, "leader", rnd,
            prompts.leader_revision(reviewer.name, mode, review_for_leader),
        )
        leader_note = human("after_leader", rnd)

    if approved:
        final_text, status = draft.text, "approved"
    else:
        synthesis = step(
            leader, "leader", rounds,
            prompts.leader_synthesis(reviewer.name, final_review),
        )
        final_text, status = synthesis.text, "needs_human_decision"

    log.finish(final_text, status)
    return RunResult(status=status, rounds=rounds, final_text=final_text)
