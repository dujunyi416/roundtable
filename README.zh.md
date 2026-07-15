# Roundtable（圆桌）

**让 Claude Code 和 Codex 直接对话——不再需要你在两个 AI 之间复制粘贴传话。**

[English →](README.md)

如果你同时在用 Claude Code 和 OpenAI Codex，大概率经历过"传话游戏"：把一个 AI 的回答复制给另一个，转述、失真、再来一轮。Roundtable 把你从中间解放出来。它是一个零依赖的 Python 命令行工具，在两个 agent 之间**原文**传递消息，运行结构化的评审循环直到双方达成一致，并在磁盘上留下完整可审计的对话记录。

```
你 ──任务──▶ roundtable
                │
                ▼
     ┌─── leader 起草 ◀──────────────┐
     │        │                      │
     │        ▼（原文传递）           │（原文传递）
     │   reviewer 评审 ────REVISE────┘
     │        │
     │     APPROVE
     ▼        ▼
  transcript.md + result.md
```

## 工作原理

- **Leader**（可配置：`claude` 或 `codex`）先就任务产出初稿。
- **Reviewer** 收到初稿*原文*，必须在评审末尾输出机器可解析的裁决行：`VERDICT: APPROVE` 或 `VERDICT: REVISE`。
- 收到 `REVISE` 时，leader 会收到评审原文并修订重交——直到通过，或达到 `--max-rounds`（默认 3 轮）后由 leader 做最终综合，并明确列出未解决的分歧。
- 每个 agent 全程使用**同一个连续会话**（`claude -p --resume` / `codex exec resume`），双方都记得完整的讨论过程。
- 每条消息实时追加写入 `.roundtable/runs/<run-id>/transcript.md`；`meta.json` 记录模型、会话 id、裁决和耗时；`result.md` 保存最终产出。

## 安装

前置条件：Python 3.10+，以及两个 CLI 均已安装并登录：

```bash
npm install -g @anthropic-ai/claude-code   # 装完运行一次 `claude` 登录
npm install -g @openai/codex               # 装完运行一次 `codex` 登录
```

然后：

```bash
git clone https://github.com/dujunyi416/roundtable
cd roundtable
pip install .        # 或者：pipx install .
roundtable doctor    # 自检 claude、codex、git 是否就绪
```

（也可以不安装，直接在仓库目录里 `python -m roundtable ...`）

## 使用

```bash
# 讨论开放问题，收敛出联合结论
roundtable "这个项目该用 SQLite 还是 Postgres？背景：……" --mode discuss

# 起草实现计划，交叉评审直到通过
roundtable "规划 data/fetcher.py 迁移到异步 IO" --mode plan --cwd 你的仓库路径

# leader 实际改代码，reviewer 每轮审查真实的 git diff
roundtable "修复 tests/test_api.py 里失败的测试" --mode build --lead codex --cwd 你的仓库路径
```

| 选项 | 默认值 | 含义 |
|---|---|---|
| `--mode discuss\|plan\|build` | `discuss` | 见下表 |
| `--lead claude\|codex` | `claude` | 谁主导；另一方评审 |
| `--max-rounds N` | `3` | 强制综合前的最大评审轮数 |
| `--claude-model` / `--codex-model` | CLI 默认 | 分侧指定模型 |
| `--cwd DIR` | `.` | 两个 agent 的工作目录（产物也存这里） |
| `--timeout SEC` | `1200` | 单次调用超时 |
| `--quiet` | 关 | 只打印进度头和最终结果 |
| `--dangerous` | 关 | 解除沙箱（见"安全默认值"） |

### 三种模式

| 模式 | 产出 | 文件权限 |
|---|---|---|
| `discuss` | 联合结论 / 观点 | 双方只读 |
| `plan` | 经过评审的实现计划 | 双方只读 |
| `build` | `--cwd` 里的真实代码修改 | leader 可改文件；reviewer 只读审查 `git diff` |

## 安全默认值

- `discuss` / `plan`：Claude 工具限制为 `Read,Grep,Glob`；Codex 使用 `--sandbox read-only`。
- `build`：只有 leader 可写（Claude `--permission-mode acceptEdits` / Codex `--sandbox workspace-write`），leader 被要求不做 commit，reviewer 保持只读。
- `--dangerous` 对应 `claude --dangerously-skip-permissions` / `codex --sandbox danger-full-access`，仅建议在一次性环境中使用。

注意：`build` 模式下，新建的文件在评审材料里以 `git status` 未跟踪条目的形式出现（只有已跟踪文件的改动会展示 diff 内容）。

## 路线图

- 支持两个以上的 AI 同桌（适配器接口已与具体 agent 解耦）。
- 可能发布到 PyPI。

## 许可证

MIT
