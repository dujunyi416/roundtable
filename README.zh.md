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
  transcript.md + result.md / plan.md
```

## 工作原理

- **Leader**（可配置：`claude` 或 `codex`）先就任务产出初稿。
- **Reviewer** 收到初稿*原文*，必须在评审末尾输出机器可解析的三段协议块：

```
SCORE: <1-10 整数，整体质量分>
BLOCKING ISSUES: <必须修复才能通过的问题编号清单，或 "none">
VERDICT: APPROVE | REVISE
```

- 收到 `REVISE` 时，leader 会收到评审原文，并被要求逐条回应每个编号的 blocking issue（修复它，或明确说明为什么不同意），然后重交——直到通过，或达到 `--max-rounds`（默认 3 轮）后由 leader 做最终综合，并明确列出未解决的分歧。
- 每个 agent 全程使用**同一个连续会话**（`claude -p --resume` / `codex exec resume`），双方都记得完整的讨论过程。
- 每条消息实时追加写入 `.roundtable/runs/<run-id>/transcript.md`；`messages.jsonl` 保存结构化镜像；`meta.json` 记录模型、会话 id、token usage、分数、裁决和耗时。`plan` 模式还会生成独立的 `plan.md`，每次真实发送的完整提示保存在 `prompts/`。
- 达到轮次上限会标记为 `needs_human_decision`，不再伪装成成功。SCORE / BLOCKING ISSUES / VERDICT 必须是回复末尾的完整协议块；缺失、越界或自相矛盾的批准会安全降级为 `REVISE`。

## 安装

前置条件：Python 3.10+，以及你计划使用的 CLI provider：

```bash
npm install -g @anthropic-ai/claude-code   # 装完运行一次 `claude` 登录
npm install -g @openai/codex               # 装完运行一次 `codex` 登录
```

然后：

```bash
git clone https://github.com/dujunyi416/roundtable
cd roundtable
pip install .        # 或者：pipx install .
roundtable doctor          # 自检所有 provider 和 git
roundtable doctor codex    # 只使用 Codex，不要求 Claude 可用
```

（也可以不安装，直接在仓库目录里 `python -m roundtable ...`）

## 使用

```bash
# 讨论开放问题，收敛出联合结论
roundtable "这个项目该用 SQLite 还是 Postgres？背景：……" --mode discuss

# Claude 不可用时，用两个独立 Codex 会话分别起草和审查
roundtable "审查这个架构" --lead codex --reviewer codex

# 起草实现计划，交叉评审直到通过
roundtable "规划 data/fetcher.py 迁移到异步 IO" --mode plan --cwd 你的仓库路径

# leader 实际改代码，reviewer 每轮审查真实的 git diff
roundtable "修复 tests/test_api.py 里失败的测试" --mode build --lead codex --cwd 你的仓库路径
```

| 选项 | 默认值 | 含义 |
|---|---|---|
| `--mode discuss\|plan\|build` | `discuss` | 见下表 |
| `--lead claude\|codex` | `claude` | 主导者 provider |
| `--reviewer claude\|codex` | 另一 provider | 审查者 provider；可与 `--lead` 相同 |
| `--lead-name` / `--reviewer-name` | provider 名 | 面向人的角色名称 |
| `--style balanced\|adversarial` | `balanced` | 评审风格（见下文） |
| `--max-rounds N` | `3` | 强制综合前的最大评审轮数 |
| `--claude-model` / `--codex-model` | CLI 默认 | 分侧指定模型 |
| `--lead-model` / `--reviewer-model` | provider 默认 | 按角色指定模型 |
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

推荐工作流：`discuss → plan → 人工确认 → build → verify`。Web 工作台只有在人明确确认本批范围、builder 和 reviewer 后才启动关联的后续运行；`parent_run_id` 和 `next_action` 保存完整链路。

### 评审风格（`--style`）

- `balanced`（默认）：reviewer 诚实评审，认为可以交付就通过。
- `adversarial`（对抗式）：reviewer 被要求以挑毛病为目标——在允许 `APPROVE` 之前，必须先找出至少 2 个具体问题（或明确论证为什么认真审查后确实挑不出毛病）。当两个模型倾向于互相"盖章通过"时，用它来对抗走过场式评审。

```bash
roundtable "为这个 API 设计认证流程" --mode plan --style adversarial
```

## 人类参与的 Web 协作工作台

```bash
roundtable ui [--cwd 目录] [--port 8642]
```

启动零依赖的本地工作台（`http://127.0.0.1:8642`，只绑定回环地址）。你可以直接在浏览器里：

- 创建 `discuss`、`plan`、`build` 运行，并分别选择两个参与者；
- 实时查看完整对话、分数、裁决、当前阶段和状态；
- 暂停、继续或取消运行；
- 开启“每次 AI 发言后等待我确认”，原样插入你的指导后再继续；
- 维护 Project Room，把项目使命、目标、约束和已决定事项自动提供给后续运行；
- 保存多个项目档案（本地项目路径与 Git 路径），新建协作和历史运行都按项目分类。
- 从讨论生成独立计划，明确分派首批实施范围，并在上下游关联运行之间导航。

所有运行仍可在 `<cwd>/.roundtable/runs` 审计。v0.2 之前的旧运行会降级为原始 transcript 视图。

### Project Room

工作台把浏览器项目档案保存在 `.roundtable/projects.json`；首次使用时会把现有 `.roundtable/project.json` 作为默认项目载入。Web 运行使用所选项目的本地路径作为工作目录，并把对应上下文作为独立上下文块传给双方。CLI 运行继续读取当前工作目录下的 `.roundtable/project.json`；一次性隔离运行可使用 `--no-project-context`。

## 安全默认值

- `discuss` / `plan`：Claude 工具限制为 `Read,Grep,Glob`；Codex 使用 `--sandbox read-only`。
- `build`：只有 leader 可写（Claude `--permission-mode acceptEdits` / Codex `--sandbox workspace-write`），leader 被要求不做 commit，reviewer 保持只读。
- Web 所有请求先校验本机 Host；写请求还必须通过同源 Origin、`application/json` 和进程随机 `X-Roundtable-Token`。页面使用 nonce CSP，并把全部运行工件视为不可信数据。
- Codex 无法取得明确 session id 时 fail closed，绝不回退到 `resume --last`。
- build 审查包含 staged、unstaged 和受限的安全 untracked 文本；环境文件、密钥、凭据、二进制和超限内容只暴露路径、排除原因和 SHA-256，并保留运行开始时的内容基线。
- `--dangerous` 对应 `claude --dangerously-skip-permissions` / `codex --sandbox danger-full-access`，仅建议在一次性环境中使用。

### 审计与恢复

工作区内保留便于查看的副本；权威副本写入 `~/.roundtable/audit/<run-id>/`，由工作区外的 `audit.key` 建立 HMAC hash chain。这里可能含提示和项目上下文，应限制为当前用户访问，并按数据保留策略定期清理。`--dangerous` 下 agent 有全盘权限，该隔离边界不成立。

运行每 5 秒更新心跳并持有进程 lease。恢复时，只有能取得旧 owner lock 才标记 `interrupted`；没有死亡证据的旧运行只标记 `orphaned`，等待人工判断。

## 路线图

持久化恢复、更多协作协议、项目任务图、成本控制和远程协作等方向见 [ROADMAP.md](ROADMAP.md)。

## 许可证

MIT
