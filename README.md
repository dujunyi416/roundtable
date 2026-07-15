# Roundtable

**Let Claude Code and Codex talk to each other directly — no more copy-pasting between two AI chats.**

[中文文档 →](README.zh.md)

If you use both Claude Code and OpenAI Codex, you have probably played telephone between them: copy one AI's answer, paste it to the other, paraphrase, lose nuance, repeat. Roundtable removes you from the loop. It is a small zero-dependency Python CLI that relays messages between the two agents **verbatim**, runs a structured review loop until they agree, and leaves a full auditable transcript on disk.

```
you ──task──▶ roundtable
                 │
                 ▼
      ┌─── leader drafts ◀────────────┐
      │        │                      │
      │        ▼ (verbatim)           │ (verbatim)
      │   reviewer critiques ──REVISE─┘
      │        │
      │     APPROVE
      ▼        ▼
   transcript.md + result.md
```

## How it works

- The **leader** (configurable: `claude` or `codex`) produces a first draft of the task.
- The **reviewer** receives the draft *verbatim* and must end its critique with a machine-parseable line: `VERDICT: APPROVE` or `VERDICT: REVISE`.
- On `REVISE`, the leader gets the review verbatim, revises, and resubmits — until approval or `--max-rounds` (default 3), after which the leader writes a final synthesis that explicitly lists unresolved disagreements.
- Each agent keeps **one continuous session** for the whole run (`claude -p --resume` / `codex exec resume`), so both remember the entire discussion.
- Every message is appended to `.roundtable/runs/<run-id>/transcript.md` as it happens; `meta.json` records models, session ids, verdicts, and timings; `result.md` holds the final deliverable.

## Install

Prerequisites: Python 3.10+, plus both CLIs installed and logged in:

```bash
npm install -g @anthropic-ai/claude-code   # then run `claude` once to log in
npm install -g @openai/codex               # then run `codex` once to log in
```

Then:

```bash
git clone https://github.com/dujunyi416/roundtable
cd roundtable
pip install .        # or: pipx install .
roundtable doctor    # verifies claude, codex, and git are ready
```

(Or skip installing and run `python -m roundtable ...` from the repo.)

## Usage

```bash
# Discuss an open question, converge on a joint answer
roundtable "Should we use SQLite or Postgres for this project? Context: ..." --mode discuss

# Draft an implementation plan, cross-reviewed until approved
roundtable "Plan the migration of data/fetcher.py to async IO" --mode plan --cwd path/to/repo

# Leader actually edits code; reviewer reviews the real git diff each round
roundtable "Fix the failing tests in tests/test_api.py" --mode build --lead codex --cwd path/to/repo
```

| Option | Default | Meaning |
|---|---|---|
| `--mode discuss\|plan\|build` | `discuss` | see table below |
| `--lead claude\|codex` | `claude` | which agent leads; the other reviews |
| `--max-rounds N` | `3` | review rounds before forced synthesis |
| `--claude-model` / `--codex-model` | CLI defaults | per-side model override |
| `--cwd DIR` | `.` | working directory for both agents and artifacts |
| `--timeout SEC` | `1200` | per-call timeout |
| `--quiet` | off | print only headers and the final result |
| `--dangerous` | off | lift sandboxes (see Safety) |

### Modes

| Mode | Deliverable | File access |
|---|---|---|
| `discuss` | joint answer / position | both read-only |
| `plan` | reviewed implementation plan | both read-only |
| `build` | actual code changes in `--cwd` | leader may edit files; reviewer reviews the `git diff` read-only |

## Safety defaults

- `discuss` / `plan`: Claude runs with tools limited to `Read,Grep,Glob`; Codex runs with `--sandbox read-only`.
- `build`: only the leader can write (Claude `--permission-mode acceptEdits` / Codex `--sandbox workspace-write`), the leader is told not to commit, and the reviewer stays read-only.
- `--dangerous` maps to `claude --dangerously-skip-permissions` / `codex --sandbox danger-full-access`. Use only in disposable environments.

Note: in `build` mode, newly created files appear in the reviewer's material as untracked entries in `git status` (content shown only for tracked-file diffs).

## Roadmap

- More than two seats at the table (the adapter interface is already agent-agnostic).
- Optional PyPI release.

## License

MIT
