"""The round loop: leader drafts, reviewer verdicts, iterate until APPROVE."""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

from . import prompts
from .adapters import Adapter, Reply
from .transcript import RunLog

# Tolerates markdown decoration ("**VERDICT: approve**"), a full-width colon,
# and "APPROVED"; the LAST occurrence in the message wins.
VERDICT_RE = re.compile(r"(?im)^[>\s*_`#-]*verdict[\s*_`]*[:：][\s*_`]*(approve|revise)")
SCORE_RE = re.compile(r"(?im)^[>\s*_`#-]*score[\s*_`]*[:：][\s*_`]*(\d{1,2})")
# Captures everything from the BLOCKING ISSUES marker up to the SCORE/VERDICT line.
BLOCKING_RE = re.compile(
    r"(?ims)^[>\s*_`#-]*blocking\s+issues[\s*_`]*[:：][\s*_`]*"
    r"(.*?)(?=^[>\s*_`#-]*(?:score|verdict)[\s*_`]*[:：]|\Z)"
)

MAX_DIFF_CHARS = 60_000


@dataclass
class RunResult:
    status: str  # "approved" | "max_rounds"
    rounds: int
    final_text: str


def parse_verdict(text: str) -> str | None:
    matches = VERDICT_RE.findall(text)
    return matches[-1].upper() if matches else None


def parse_score(text: str) -> int | None:
    matches = SCORE_RE.findall(text)
    if not matches:
        return None
    score = int(matches[-1])
    return score if 0 <= score <= 10 else None


def parse_blocking_issues(text: str) -> str | None:
    matches = BLOCKING_RE.findall(text)
    return matches[-1].strip() or None if matches else None


def _git(cwd: str, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60,
        )
        return proc.stdout if proc.returncode == 0 else ""
    except OSError:
        return ""


def build_review_material(summary: str, cwd: str) -> str:
    """For build mode: the leader's summary plus the actual working-tree diff."""
    diff = _git(cwd, "diff")
    status = _git(cwd, "status", "--short")
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + "\n... [diff truncated by roundtable]"
    return (
        f"{summary}\n\n--- git status --short ---\n{status or '(clean)'}\n"
        f"--- git diff (unstaged) ---\n{diff or '(no diff)'}"
    )


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
) -> RunResult:
    def step(agent: Adapter, role: str, round_no: int, prompt: str) -> Reply:
        reply = agent.send(prompt)
        log.add(agent.name, role, round_no, reply.text, reply.duration_s, reply.session_id)
        echo(agent.name, role, reply.text)
        return reply

    draft = step(leader, "leader", 0, prompts.leader_kickoff(leader.name, reviewer.name, mode, task))

    approved = False
    rounds = 0
    for rnd in range(1, max_rounds + 1):
        rounds = rnd
        material = draft.text
        if mode == "build":
            material = build_review_material(draft.text, cwd)
        if rnd == 1:
            rprompt = prompts.reviewer_first(reviewer.name, leader.name, mode, task, material, style)
        else:
            rprompt = prompts.reviewer_next(leader.name, material, style)
        review = step(reviewer, "reviewer", rnd, rprompt)

        verdict = parse_verdict(review.text)
        log.verdict(rnd, verdict, parse_score(review.text), parse_blocking_issues(review.text))
        if verdict == "APPROVE":
            approved = True
            break
        if rnd == max_rounds:
            break
        draft = step(leader, "leader", rnd, prompts.leader_revision(reviewer.name, mode, review.text))

    if approved:
        final_text, status = draft.text, "approved"
    else:
        synthesis = step(leader, "leader", rounds, prompts.leader_synthesis(reviewer.name))
        final_text, status = synthesis.text, "max_rounds"

    log.finish(final_text, status)
    return RunResult(status=status, rounds=rounds, final_text=final_text)
