"""CLI entry point: `roundtable "task" [options]` and `roundtable doctor`."""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys

from . import __version__, orchestrator
from .adapters import AdapterError
from .adapters.claude import INSTALL_HINT as CLAUDE_HINT
from .adapters.claude import ClaudeAdapter
from .adapters.codex import INSTALL_HINT as CODEX_HINT
from .adapters.codex import CodexAdapter
from .adapters.mock import MockAdapter
from .transcript import RunLog

AGENTS = {"claude": ClaudeAdapter, "codex": CodexAdapter}


def doctor() -> int:
    """Check that both CLIs (and git) are installed and answer `--version`."""
    ok = True
    checks = [
        ("claude", CLAUDE_HINT),
        ("codex", CODEX_HINT),
        ("git", "https://git-scm.com/downloads (needed for build mode diffs)"),
    ]
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
        epilog='Also: "roundtable doctor" checks that both CLIs are installed.',
    )
    p.add_argument("task", help="the task/question, passed verbatim to both agents")
    p.add_argument("--mode", choices=["discuss", "plan", "build"], default="discuss",
                   help="discuss: joint answer; plan: reviewed plan; build: leader edits files, "
                        "reviewer reviews the diff (default: discuss)")
    p.add_argument("--lead", choices=["claude", "codex"], default="claude",
                   help="which agent leads (default: claude)")
    p.add_argument("--max-rounds", type=int, default=3, metavar="N",
                   help="max review rounds before forced synthesis (default: 3)")
    p.add_argument("--claude-model", metavar="M", help="model for the claude side")
    p.add_argument("--codex-model", metavar="M", help="model for the codex side")
    p.add_argument("--cwd", default=".", help="working directory for both agents (default: .)")
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
        return doctor()
    args = build_parser().parse_args(argv)

    lead, rev = args.lead, ("codex" if args.lead == "claude" else "claude")
    models = {"claude": args.claude_model, "codex": args.codex_model}

    def make(name: str, role: str):
        if args.mock in (role, "both"):
            return MockAdapter(cwd=args.cwd)
        cls = AGENTS[name]
        return cls(
            cwd=args.cwd,
            model=models[name],
            writable=(role == "leader" and args.mode == "build"),
            dangerous=args.dangerous,
            timeout=args.timeout,
        )

    try:
        leader = make(lead, "leader")
        reviewer = make(rev, "reviewer")
    except AdapterError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    log = RunLog(
        args.cwd, args.task, args.mode, lead,
        {"max_rounds": args.max_rounds, "models": models, "dangerous": args.dangerous,
         "mock": args.mock, "version": __version__},
    )
    print(f"roundtable run: {log.dir}")

    def echo(speaker: str, role: str, text: str) -> None:
        print(f"\n--- {speaker} ({role}) " + "-" * 40)
        if not args.quiet:
            print(text)

    try:
        result = orchestrator.run(
            args.task, leader, reviewer, args.mode, args.max_rounds,
            log, args.cwd, echo=echo,
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
