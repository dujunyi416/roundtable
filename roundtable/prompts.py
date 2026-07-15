"""Prompt frames for each role and mode.

The orchestrator NEVER rewrites what either agent said — counterpart messages
are always embedded verbatim between BEGIN/END markers. These templates only
add the fixed role framing around them.
"""
from __future__ import annotations


def _block(label: str, content: str) -> str:
    return f"=== BEGIN {label} ===\n{content}\n=== END {label} ==="


VERDICT_RULE = (
    'End your reply with a line containing exactly "VERDICT: APPROVE" if the work '
    'is good enough to ship as the final result, or "VERDICT: REVISE" if it needs '
    "another iteration. Give specific, actionable critique before the verdict."
)

_LEADER_TASK = {
    "discuss": (
        "Give your best, complete answer or position on the task. Be concrete, "
        "state your reasoning and any assumptions you are making."
    ),
    "plan": (
        "Draft a concrete implementation plan for the task: context, chosen "
        "approach, files to touch, ordered steps, risks, and how to verify."
    ),
    "build": (
        "Implement the task by editing files in the current working directory. "
        "Do NOT commit anything. When done, reply with a concise summary of what "
        "you changed and why (the diff itself is collected separately)."
    ),
}

_REVIEWER_TASK = {
    "discuss": "Challenge weak points, add what is missing, and correct anything wrong.",
    "plan": "Review the plan for gaps, wrong assumptions, missing steps, and risks.",
    "build": (
        "Review the summary and the attached diff for bugs, unhandled cases, and "
        "deviations from the task."
    ),
}


def leader_kickoff(agent: str, other: str, mode: str, task: str) -> str:
    return (
        f"You are {agent}, acting as the LEADER in a two-AI collaboration run by "
        f"Roundtable. Your counterpart, {other}, will review your work and reply "
        f'with critique plus a verdict ("APPROVE" or "REVISE"). Messages between '
        f"you are relayed verbatim, so write as if speaking directly to {other} "
        f"and to the human who posed the task.\n\n"
        f"{_LEADER_TASK[mode]}\n"
        f"Respond in the same language as the task.\n\n"
        f"{_block('TASK (verbatim from the human)', task)}"
    )


def reviewer_first(agent: str, other: str, mode: str, task: str, material: str) -> str:
    return (
        f"You are {agent}, acting as the REVIEWER in a two-AI collaboration run by "
        f"Roundtable. {other} (the leader) has produced a first attempt at the "
        f"human's task; both are below, verbatim.\n\n"
        f"{_REVIEWER_TASK[mode]}\n"
        f"Respond in the same language as the task. {VERDICT_RULE}\n\n"
        f"{_block('TASK (verbatim from the human)', task)}\n\n"
        f"{_block(f'{other.upper()} DRAFT (verbatim)', material)}"
    )


def reviewer_next(other: str, material: str) -> str:
    return (
        f"{other} revised the work in response to your review; the new version is "
        f"below, verbatim. Re-review it: check whether your earlier points were "
        f"addressed and whether the revision introduced new problems.\n"
        f"{VERDICT_RULE}\n\n"
        f"{_block(f'{other.upper()} REVISION (verbatim)', material)}"
    )


def leader_revision(other: str, mode: str, review: str) -> str:
    action = (
        "revise the files accordingly and reply with an updated summary of the changes"
        if mode == "build"
        else "reply with your full revised version (self-contained, not a diff of your answer)"
    )
    return (
        f"{other} reviewed your work; the review is below, verbatim. Address every "
        f"point you agree with, push back explicitly on any you disagree with, "
        f"then {action}.\n\n"
        f"{_block(f'{other.upper()} REVIEW (verbatim)', review)}"
    )


def leader_synthesis(other: str) -> str:
    return (
        f"The round limit was reached without {other}'s approval. Produce the final "
        f"deliverable now: your best self-contained version incorporating the valid "
        f'critique, ending with a short "Open disagreements" section that lists any '
        f"unresolved points of disagreement (or states there are none)."
    )
