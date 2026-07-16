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
   transcript.md + result.md / plan.md
```

## How it works

- The **leader** (configurable: `claude` or `codex`) produces a first draft of the task.
- The **reviewer** receives the draft *verbatim* and must end its critique with a machine-parseable three-part protocol block:

```
SCORE: <integer 1-10, overall quality>
BLOCKING ISSUES: <numbered list of must-fix problems, or "none">
VERDICT: APPROVE | REVISE
```

- On `REVISE`, the leader gets the review verbatim and must respond to each numbered blocking issue point by point (fix it or push back with reasons), then resubmit — until approval or `--max-rounds` (default 3), after which the leader writes a final synthesis that explicitly lists unresolved disagreements.
- Each agent keeps **one continuous session** for the whole run (`claude -p --resume` / `codex exec resume`), so both remember the entire discussion.
- Every message is appended to `.roundtable/runs/<run-id>/transcript.md`; `messages.jsonl` is its structured mirror; `meta.json` records models, session ids, token usage, scores, verdicts, and timings. Plan runs also create `plan.md`, and exact agent inputs are stored under `prompts/`.
- Reaching the round limit is `needs_human_decision`, not success. SCORE / BLOCKING ISSUES / VERDICT must be one complete block at the end of the review; missing, contradictory, or out-of-range fields fail safely to `REVISE`.

## Install

Prerequisites: Python 3.10+, plus whichever CLI provider(s) you plan to use:

```bash
npm install -g @anthropic-ai/claude-code   # then run `claude` once to log in
npm install -g @openai/codex               # then run `codex` once to log in
```

Then:

```bash
git clone https://github.com/dujunyi416/roundtable
cd roundtable
pip install .        # or: pipx install .
roundtable doctor          # verifies all providers and git
roundtable doctor codex    # Codex-only setup; Claude is not required
```

(Or skip installing and run `python -m roundtable ...` from the repo.)

## Usage

```bash
# Discuss an open question, converge on a joint answer
roundtable "Should we use SQLite or Postgres for this project? Context: ..." --mode discuss

# Run two independent Codex conversations when Claude is unavailable
roundtable "Review this architecture" --lead codex --reviewer codex

# Draft an implementation plan, cross-reviewed until approved
roundtable "Plan the migration of data/fetcher.py to async IO" --mode plan --cwd path/to/repo

# Leader actually edits code; reviewer reviews the real git diff each round
roundtable "Fix the failing tests in tests/test_api.py" --mode build --lead codex --cwd path/to/repo
```

| Option | Default | Meaning |
|---|---|---|
| `--mode discuss\|plan\|build` | `discuss` | see table below |
| `--lead claude\|codex` | `claude` | leader provider |
| `--reviewer claude\|codex` | other provider | reviewer provider; may equal `--lead` |
| `--lead-name` / `--reviewer-name` | provider name | human-readable role names |
| `--style balanced\|adversarial` | `balanced` | reviewer attitude (see below) |
| `--max-rounds N` | `3` | review rounds before forced synthesis |
| `--claude-model` / `--codex-model` | CLI defaults | per-side model override |
| `--lead-model` / `--reviewer-model` | provider default | per-role model override |
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

Recommended workflow: `discuss → plan → human confirmation → build → verify`. The web workbench starts linked follow-up runs only after the human confirms the batch scope, builder, and reviewer. `parent_run_id` and `next_action` preserve the lineage.

### Reviewer styles (`--style`)

- `balanced` (default): the reviewer critiques honestly and approves when the work is good enough to ship.
- `adversarial`: the reviewer is instructed to hunt for problems — it must identify at least 2 concrete issues (or explain why serious scrutiny found nothing) before it is allowed to `APPROVE`. Use this to counter rubber-stamping when the two models tend to agree too easily.

```bash
roundtable "Design the auth flow for this API" --mode plan --style adversarial
```

## Human-in-the-loop web workbench

```bash
roundtable ui [--cwd DIR] [--port 8642]
```

Starts a zero-dependency local workbench at `http://127.0.0.1:8642` (loopback only). From the browser you can:

- create `discuss`, `plan`, or `build` runs and choose each participant independently;
- watch the conversation, scores, verdicts, phase, and status live;
- pause, resume, or cancel a running collaboration;
- enable a human gate after every AI turn, then inject guidance verbatim before continuing;
- maintain a Project Room whose mission, goals, constraints, and decisions are supplied to future runs;
- save project profiles with local working directories and Git locations, then choose and browse runs by project.
- generate a dedicated plan from a discussion, explicitly assign the first build batch, and navigate between linked runs.

Runs remain auditable under `<cwd>/.roundtable/runs`. Pre-v0.2 runs fall back to the raw transcript view.

### Project Room

The workbench stores browser project profiles in `.roundtable/projects.json`, seeding the first profile from an existing `.roundtable/project.json`. Web runs use the selected profile's local path as their working directory and receive its context in a separate prompt block. CLI runs continue to read `.roundtable/project.json` from their working directory. Use `--no-project-context` for a one-off isolated CLI run.

## Safety defaults

- `discuss` / `plan`: Claude runs with tools limited to `Read,Grep,Glob`; Codex runs with `--sandbox read-only`.
- `build`: only the leader can write (Claude `--permission-mode acceptEdits` / Codex `--sandbox workspace-write`), the leader is told not to commit, and the reviewer stays read-only.
- Every web request validates the local Host. Writes additionally require same-origin Origin, `application/json`, and a process-random `X-Roundtable-Token`; the page uses a nonce CSP and treats run artifacts as untrusted.
- Codex fails closed when no explicit session id is available; it never falls back to `resume --last`.
- Build reviews include staged, unstaged, and bounded safe untracked text. Environment files, keys, credentials, binaries, and oversized content expose only path, exclusion reason, and SHA-256. A run-start content baseline is retained.
- `--dangerous` maps to `claude --dangerously-skip-permissions` / `codex --sandbox danger-full-access`. Use only in disposable environments.

### Audit and recovery

The workspace keeps a convenient run copy. An authoritative mirror is written to `~/.roundtable/audit/<run-id>/` with an HMAC hash chain rooted in the workspace-external `audit.key`. It may contain prompts and project context: restrict it to the current user and prune it under your retention policy. This isolation does not hold under `--dangerous`.

Running jobs update a heartbeat every five seconds and hold a process lease. Recovery marks a run `interrupted` only when it can acquire the previous owner lock; legacy runs without death evidence become `orphaned` for human review.

## Roadmap

See [ROADMAP.md](ROADMAP.md) for durable resume, richer protocols, project task graphs, cost controls, and remote collaboration.

## License

MIT
