"""Prompt frames for each role and mode.

The orchestrator NEVER rewrites what either agent said — counterpart messages
are always embedded verbatim between BEGIN/END markers. These templates only
add the fixed role framing around them.
"""
from __future__ import annotations


def _block(label: str, content: str) -> str:
    return f"=== BEGIN {label} ===\n{content}\n=== END {label} ==="


VERDICT_RULE = (
    "Give specific, actionable critique first, then end your reply with this "
    "exact protocol block:\n"
    "SCORE: <integer 1-10, overall quality of the work>\n"
    'BLOCKING ISSUES: <numbered list of problems that MUST be fixed before approval, or "none">\n'
    'VERDICT: <"APPROVE" if good enough to ship as the final result, "REVISE" otherwise>\n'
    "The VERDICT line must come last. You must not answer APPROVE while any "
    "blocking issue remains."
)

ADVERSARIAL_RULE = (
    "Style: ADVERSARIAL. Your job is to find what is wrong, not to be agreeable. "
    "Actively challenge assumptions, hunt for edge cases, gaps, and errors. Before "
    "you may answer APPROVE, you must have identified at least 2 concrete problems "
    "and confirmed they are non-blocking — or explicitly explain why serious "
    "scrutiny turned up nothing. A first-round APPROVE should be rare."
)


def _style_rule(style: str) -> str:
    return f"{ADVERSARIAL_RULE}\n" if style == "adversarial" else ""

_LEADER_TASK = {
    "discuss": (
        "Give your best, complete answer or position on the task. Be concrete, "
        "state your reasoning and any assumptions you are making."
    ),
    "plan": (
        "Draft an execution-ready implementation plan. Use task IDs and ordered, "
        "independently verifiable batches. For every task name the builder and "
        "reviewer roles, files to touch, dependencies, acceptance criteria, exact "
        "verification commands, risks, and rollback. End with a human approval "
        "checkpoint that states the proposed first build batch."
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


def leader_kickoff(agent: str, other: str, mode: str, task: str,
                   project_context: str | None = None) -> str:
    context = (
        f"{_block('PROJECT ROOM CONTEXT', project_context)}\n\n"
        if project_context else ""
    )
    return (
        f"You are {agent}, acting as the LEADER in a two-AI collaboration run by "
        f"Roundtable. Your counterpart, {other}, will review your work and reply "
        f'with critique plus a verdict ("APPROVE" or "REVISE"). Messages between '
        f"you are relayed verbatim, so write as if speaking directly to {other} "
        f"and to the human who posed the task.\n\n"
        f"{_LEADER_TASK[mode]}\n"
        f"Respond in the same language as the task.\n\n"
        f"{context}"
        f"{_block('TASK (verbatim from the human)', task)}"
    )


def reviewer_first(agent: str, other: str, mode: str, task: str, material: str,
                   style: str = "balanced", project_context: str | None = None) -> str:
    context = (
        f"{_block('PROJECT ROOM CONTEXT', project_context)}\n\n"
        if project_context else ""
    )
    return (
        f"You are {agent}, acting as the REVIEWER in a two-AI collaboration run by "
        f"Roundtable. {other} (the leader) has produced a first attempt at the "
        f"human's task; both are below, verbatim.\n\n"
        f"{_REVIEWER_TASK[mode]}\n"
        f"{_style_rule(style)}"
        f"Respond in the same language as the task.\n{VERDICT_RULE}\n\n"
        f"{context}"
        f"{_block('TASK (verbatim from the human)', task)}\n\n"
        f"{_block(f'{other.upper()} DRAFT (verbatim)', material)}"
    )


def reviewer_next(other: str, material: str, style: str = "balanced") -> str:
    return (
        f"{other} revised the work in response to your review; the new version is "
        f"below, verbatim. Re-review it: check whether each of your blocking issues "
        f"was resolved and whether the revision introduced new problems.\n"
        f"{_style_rule(style)}"
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
        f"{other} reviewed your work; the review is below, verbatim. Respond to each "
        f"numbered BLOCKING ISSUE point by point: fix the ones you agree with, push "
        f"back explicitly on the ones you disagree with (and say why). Then {action}.\n\n"
        f"{_block(f'{other.upper()} REVIEW (verbatim)', review)}"
    )


def human_intervention(text: str) -> str:
    return _block("HUMAN INTERVENTION (verbatim)", text)


def leader_synthesis(other: str, final_review: str | None = None) -> str:
    prompt = (
        f"The round limit was reached without {other}'s approval. Produce the final "
        f"deliverable now: your best self-contained version incorporating the valid "
        f'critique, ending with a short "Open disagreements" section that lists any '
        f"unresolved points of disagreement (or states there are none)."
    )
    if final_review:
        prompt += f"\n\n{_block(f'{other.upper()} FINAL REVIEW (verbatim)', final_review)}"
    return prompt
