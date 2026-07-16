"""CLI entry point: `roundtable "task" [options]` and `roundtable doctor`."""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys

from . import __version__, orchestrator
from .audit import default_audit_root
from .adapters import AdapterError
from .adapters.mock import MockAdapter
from .participants import PROVIDERS, build_roster, create_participant, provider_names
from .project import ProjectRoom
from .transcript import RunLog


def doctor(selected: list[str] | None = None) -> int:
    """Check selected participant CLIs (and git) and answer `--version`."""
    ok = True
    names = selected or list(provider_names())
    checks = [(PROVIDERS[name].command, PROVIDERS[name].install_hint) for name in names]
    checks.append(("git", "https://git-scm.com/downloads (needed for build mode diffs)"))
    for name, hint in checks:
        path = shutil.which(name)
        if not path:
            print(f"[MISSING] {name:7s} not on PATH. Install: {hint}")
            ok = False
            continue
        try:
            proc = subprocess.run(
                [path, "--version"], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=60,
            )
            version = (proc.stdout or proc.stderr).strip().splitlines()[0]
            print(f"[OK]      {name:7s} {version}  ({path})")
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"[BROKEN]  {name:7s} found at {path} but `--version` failed: {exc}")
            ok = False
    print("\nAll good — try:  roundtable \"your question\" --mode discuss" if ok
          else "\nFix the items above, then rerun `roundtable doctor`.")
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="roundtable",
        description="Let Claude Code and Codex collaborate directly: leader drafts, "
                    "reviewer critiques with APPROVE/REVISE verdicts, iterate until consensus.",
        epilog='Also: "roundtable doctor" checks that both CLIs are installed; '
               '"roundtable ui" opens a live web viewer for past and running sessions.',
    )
    p.add_argument("task", help="the task/question, passed verbatim to both agents")
    p.add_argument("--mode", choices=["discuss", "plan", "build"], default="discuss",
                   help="discuss: joint answer; plan: reviewed plan; build: leader edits files, "
                        "reviewer reviews the diff (default: discuss)")
    p.add_argument("--lead", choices=provider_names(), default="claude",
                   help="which agent leads (default: claude)")
    p.add_argument("--reviewer", choices=provider_names(),
                   help="reviewer provider (default: the other provider; may equal --lead)")
    p.add_argument("--lead-name", help="display name for the leader")
    p.add_argument("--reviewer-name", help="display name for the reviewer")
    p.add_argument("--style", choices=["balanced", "adversarial"], default="balanced",
                   help="reviewer style: adversarial requires concrete objections before "
                        "any APPROVE — counters rubber-stamping (default: balanced)")
    p.add_argument("--max-rounds", type=int, default=3, metavar="N",
                   help="max review rounds before forced synthesis (default: 3)")
    p.add_argument("--claude-model", metavar="M", help="model for the claude side")
    p.add_argument("--codex-model", metavar="M", help="model for the codex side")
    p.add_argument("--lead-model", metavar="M", help="model override for the leader role")
    p.add_argument("--reviewer-model", metavar="M", help="model override for the reviewer role")
    p.add_argument("--cwd", default=".", help="working directory for both agents (default: .)")
    p.add_argument("--no-project-context", action="store_true",
                   help="do not include .roundtable/project.json in agent prompts")
    p.add_argument("--timeout", type=int, default=1200, metavar="SEC",
                   help="per-call timeout in seconds (default: 1200)")
    p.add_argument("--quiet", action="store_true", help="only print progress headers and the final result")
    p.add_argument("--dangerous", action="store_true",
                   help="lift sandboxes (claude --dangerously-skip-permissions / codex "
                        "danger-full-access). Off by default; use only in disposable environments")
    p.add_argument("--mock", choices=["leader", "reviewer", "both"],
                   help="replace that side with a scripted mock (testing/smoke runs)")
    p.add_argument("--version", action="version", version=f"roundtable {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["doctor"]:
        dp = argparse.ArgumentParser(prog="roundtable doctor",
                                     description="check only the providers you plan to use")
        dp.add_argument("providers", nargs="*", choices=provider_names())
        return doctor(dp.parse_args(argv[1:]).providers)
    if argv[:1] == ["ui"]:
        from . import webui
        up = argparse.ArgumentParser(prog="roundtable ui",
                                     description="live web viewer for roundtable runs")
        up.add_argument("--cwd", default=".", help="project dir containing .roundtable/ (default: .)")
        up.add_argument("--port", type=int, default=8642, help="port on 127.0.0.1 (default: 8642)")
        uargs = up.parse_args(argv[1:])
        webui.serve(uargs.cwd, uargs.port)
        return 0
    args = build_parser().parse_args(argv)

    provider_models = {"claude": args.claude_model, "codex": args.codex_model}
    lead_spec, reviewer_spec = build_roster(
        args.lead,
        args.reviewer,
        lead_name=args.lead_name,
        reviewer_name=args.reviewer_name,
        lead_model=args.lead_model,
        reviewer_model=args.reviewer_model,
        provider_models=provider_models,
    )

    def make(spec):
        if args.mock in (spec.role, "both"):
            adapter = MockAdapter(cwd=args.cwd)
            adapter.name = spec.name
            return adapter
        return create_participant(
            spec,
            cwd=args.cwd,
            mode=args.mode,
            dangerous=args.dangerous,
            timeout=args.timeout,
        )

    try:
        leader = make(lead_spec)
        reviewer = make(reviewer_spec)
    except AdapterError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    participants = [lead_spec.as_dict(), reviewer_spec.as_dict()]
    project_context = None if args.no_project_context else ProjectRoom(args.cwd).context_text()
    log = RunLog(
        args.cwd, args.task, args.mode, lead_spec.name,
        {"max_rounds": args.max_rounds, "participants": participants,
         "provider_models": provider_models, "dangerous": args.dangerous,
         "mock": args.mock, "style": args.style, "version": __version__,
         "project_context": project_context},
        audit_root=default_audit_root(),
    )
    print(f"roundtable run: {log.dir}")

    def echo(speaker: str, role: str, text: str) -> None:
        print(f"\n--- {speaker} ({role}) " + "-" * 40)
        if not args.quiet:
            print(text)

    try:
        result = orchestrator.run(
            args.task, leader, reviewer, args.mode, args.max_rounds,
            log, args.cwd, echo=echo, style=args.style,
            project_context=project_context,
        )
    except AdapterError as exc:
        log.finish("", "error")
        print(f"\nerror: {exc}\npartial transcript saved to {log.dir}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        log.finish("", "interrupted")
        print(f"\ninterrupted — partial transcript saved to {log.dir}", file=sys.stderr)
        return 130

    print("\n" + "=" * 60)
    print(f"status: {result.status} after {result.rounds} round(s)")
    print(f"artifacts: {log.dir}")
    print("=" * 60 + "\n")
    print(result.final_text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
