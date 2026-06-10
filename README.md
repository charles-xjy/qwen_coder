# qwen-coder

用 LangGraph 重新实现 [mini_claude](https://github.com/Windy3f3f3f3f/claude-code-from-scratch/tree/main/python) 的全部功能，作为编程 Agent 的学习与生产参考实现。

---

## 项目目标

mini_claude 用纯 asyncio 手写了一个完整的 Claude Code 克隆。本项目目标是：

- 保留原项目的**全部核心功能**（工具系统、权限模式、上下文压缩、记忆、Skills、子 Agent）
- 用 **LangGraph StateGraph** 替换手写 agent loop，获得内置 checkpoint、human-in-the-loop、可视化调试
- 结构更清晰，每个模块职责单一，便于二次开发

---

## 功能特性

### 一、Agent 循环

- LangGraph StateGraph 驱动，节点：`agent → tools → agent`
- 流式输出，token 逐字打印
- 支持 Anthropic 原生 + OpenAI 兼容双后端
- `--max-turns` 限制最大循环次数，`--max-cost` 限制最大花费
- Ctrl+C 单次中断当前操作，双次退出程序
- REPL 交互模式 + 一次性执行模式（`qwen-coder "prompt"`）

### 二、工具系统

10 个核心工具，分三类：

| 工具 | 类型 | 说明 |
|---|---|---|
| `read_file` | 读 | 读文件内容，带行号 |
| `write_file` | 写 | 创建或覆盖文件 |
| `edit_file` | 写 | 精确字符串替换，要求唯一匹配 |
| `list_files` | 读 | 列目录结构 |
| `grep_search` | 读 | 正则内容搜索 |
| `run_shell` | 执行 | 在沙箱中执行 shell 命令 |
| `web_fetch` | 网络 | 抓取网页内容 |
| `enter_plan_mode` | 控制 | 动态切换到只读模式 |
| `exit_plan_mode` | 控制 | 退出只读模式 |
| `agent` | 子 Agent | 派发独立子任务（隔离 context） |

**Deferred 工具**（默认不加载，通过 `tool_search` 激活）：`skill`、`tool_search`

### 三、权限系统（5 种模式）

| 模式 | 参数 | 说明 |
|---|---|---|
| `default` | （默认）| 读自动执行，写/执行需用户确认 |
| `plan` | `--plan` | 只读，写/执行直接拒绝 |
| `acceptEdits` | `--accept-edits` | 文件读写自动批准，shell 执行仍需确认 |
| `bypassPermissions` | `--yolo` | 全部自动执行，无任何弹窗 |
| `dontAsk` | `--dont-ask` | 自动拒绝所有需确认操作（CI 场景）|

工具危险等级由 `settings.json` 中的规则配置，支持按工具名、路径前缀设置白名单。

### 四、上下文压缩（4 级）

压缩遵循"先丢可再生资源，最后才动不可再生内容"的原则，4 级从轻到重依次触发：

#### 级别 1：预算截断（tools.py 内实时发生）

单个工具结果超过 15KB 时，`execute_tool()` 在返回前直接截断，保留头部 25000 字符 + 尾部 5000 字符，中间加 `...[truncated]` 标记。Agent 看到截断标记后可以用 `read_file` 加 `offset` 参数重新读取所需的特定行范围。

- 触发时机：每次工具调用，实时处理
- 信息损失：中间部分丢失，但文件仍在磁盘，可按需重读
- 对 Agent 透明：截断结果直接写入 ToolMessage，Agent 只看到截断版

#### 级别 2：Snipping（使用率 > 60%）

把历史消息中**较早的可再生工具结果**替换为占位符，保留最近 3 条 ToolMessage 不动。

可截断工具（幂等操作，结果可重新获取）：`read_file` / `grep_search` / `list_files` / `run_shell` / `web_fetch`

```
替换前：
  [工具结果①] "def main(): ..."   # read_file，旧
  [工具结果②] "import os..."      # grep_search，旧
  [工具结果③] "def helper()..."   # 最近3条：保留
  [工具结果④] "DEBUG = True..."   # 保留
  [工具结果⑤] "test passed"       # 保留

替换后：
  [工具结果①] "[内容已压缩 — 如需查看请重新调用工具]"
  [工具结果②] "[内容已压缩 — 如需查看请重新调用工具]"
  [工具结果③] "def helper()..."   # 不动
  [工具结果④] "DEBUG = True..."   # 不动
  [工具结果⑤] "test passed"       # 不动
```

实现细节：使用 `msg.model_copy(update={"content": placeholder})` 保留原 message id，LangGraph 的 `add_messages` reducer 识别到相同 id 时执行 update-in-place，不追加新消息。

#### 级别 3：Micro-compact（空闲 > 5 分钟）

Agent 检测到上次 API 调用距今超过 5 分钟时，触发比 Snipping 更激进的清理：**所有旧 ToolMessage 都替换为占位符**，不区分工具类型，只保留最近 3 条。

适合用户暂时离开、Agent 等待输入的场景，趁空闲释放 token。

#### 级别 4：LLM 摘要（使用率 > 75%，用户确认）

使用率超过 75% 时，`warn 节点` 通过 `interrupt()` 弹出提示，询问用户是否立即压缩。用户选择"压缩"且使用率 > 85% 时，进入 LLM 摘要：

1. 把早期对话（除最后 4 条外）发给 LLM
2. LLM 输出结构化摘要（任务背景 / 已完成工作 / 关键决策 / 当前状态 / 待续事项）
3. 用 `RemoveMessage` 删除原始消息，插入摘要消息

```
压缩前（几十条对话）：
  [用户] 帮我重构这个项目
  [AI]   好的，我先分析...（一大段分析）
  [工具] ...（大量工具结果）
  ... 几十条 ...
  [用户] 现在测试一下           ← 最后4条保留
  [AI]   调用 run_shell
  [工具] test passed
  [AI]   测试通过

压缩后：
  [摘要] 任务背景：重构项目，重点 main.py
         已完成：拆分了3个函数，修改了 utils.py
         关键决策：用工厂模式替代 if-else
         当前状态：代码已写完，正在测试
         待续：还需要更新文档

  [用户] 现在测试一下           ← 原样保留
  [AI]   调用 run_shell
  [工具] test passed
  [AI]   测试通过
```

LLM 摘要失败时自动降级为 Snipping。

#### 压缩流程总览

```
每轮 tools 节点执行完
        ↓
   token_router 检查使用率
        ↓
   < 75%  → agent（Snipping/micro-compact 在 compress 节点内按条件触发）
   ≥ 75%  → warn 节点
        ↓
   warn 节点：interrupt() 弹出提示
   "上下文使用率 78%，是否立即压缩？"
        ↓
   用户选"压缩" → compress 节点
     ├─ 使用率 > 85%：LLM 摘要
     ├─ 空闲 > 5 分钟：micro-compact
     └─ 其他：snipping
   用户选"跳过" → agent（继续工作）
```

| 级别 | 触发时机 | 操作对象 | 实现机制 |
|---|---|---|---|
| **预算截断** | 单个工具结果 > 15KB | 工具返回值 | 直接截断字符串 |
| **Snipping** | 使用率 > 60% | 旧的可再生工具结果 | `model_copy` 保留 id，update-in-place |
| **Micro-compact** | 空闲 > 5 分钟 | 所有旧工具结果 | `model_copy` 保留 id，update-in-place |
| **LLM 摘要** | 使用率 > 85% + 用户确认 | 早期对话文本 | `RemoveMessage` 删除 + 插入摘要 |

### 五、文件记忆系统

- 存储路径：`~/.qwen-coder/projects/{project_hash}/memory/`
- 每条记忆是一个 Markdown 文件，YAML frontmatter 含 `name` / `description` / `type`
- `MEMORY.md` 为自动维护的索引（最多 200 行，25KB 上限）
- **检索方式：sideQuery** — 把索引发给 LLM，由 LLM 从中选出最相关的 5 个文件名，异步后台执行
- 4 种记忆类型：`user`（用户偏好）/ `feedback`（行为反馈）/ `project`（项目背景）/ `reference`（外部资源指针）
- 大小限制：单文件 4KB，单会话注入上限 60KB
- 记忆超过 1 天时展示新鲜度警告

### 六、Skills 系统

- 路径：`~/.claude/skills/` 和 `./.claude/skills/`，项目级覆盖用户级
- 每个 skill 是 `SKILL.md`，frontmatter 含 `name` / `description` / `when_to_use` / `allowed-tools` / `user-invocable` / `context`
- 触发方式：用户输入 `/skillname args`，或 Agent 调用 `skill` 工具
- 上下文模式：`inline`（注入当前 Agent system prompt）/ `fork`（新建隔离子 Agent 执行）
- 全局缓存，文件变更后自动重载

### 七、子 Agent 系统

- 通过 `agent` 工具派发，父 Agent 以字符串形式接收子 Agent 结论
- 三种内置类型，工具集按类型静态限制：
  - `explore`：只有 `read_file` / `list_files` / `grep_search`，专为代码探索优化
  - `plan`：只读工具，输出实现方案
  - `general`：全部工具（排除 `agent` 自身，防无限递归）
- 自定义类型：在 `.claude/agents/*.md` 中定义，frontmatter 指定 `allowed-tools`
- 权限继承：父为 `plan` 则子也为 `plan`；否则子用 `bypassPermissions`
- context 完全隔离：子 Agent 有独立消息历史，不污染父 Agent context window

### 八、沙箱隔离（OpenSandbox）

代码执行通过 [OpenSandbox](https://github.com/opensandbox-group/OpenSandbox) 隔离，保护宿主机 conda 环境，防止恶意代码破坏主机。

**隔离范围：** 只有 `run_shell` 工具在沙箱内执行；文件操作工具（`read_file` / `write_file` / `edit_file` / `list_files` / `grep_search`）继续在宿主机 WORKDIR 内运行。

**文件同步：** 每次调用 `run_shell` 前，增量上传 WORKDIR 下自上次同步后 mtime 有变化的文件到沙箱 `/workspace/`，再执行命令，执行结果（stdout + stderr）返回给 Agent。首次调用上传全量，后续只传变更文件。

**生命周期：** 会话级单例——整个 Agent 会话共享一个沙箱实例，已安装的包和环境变量在多次 `run_shell` 调用之间保留，会话结束时销毁。

**降级策略：** OpenSandbox server 不可用时（本地未启动 Docker），向用户报错并询问是否接受在宿主机本地执行；用户确认后降级运行，否则 `run_shell` 调用终止。

```
# 本地启动 OpenSandbox server
uvx opensandbox-server init-config ~/.sandbox.toml --example docker
uvx opensandbox-server
```

### 九、会话管理

- LangGraph 内置 checkpoint 自动持久化（默认 SQLite，可切换 Redis）
- JSON 会话索引文件：`~/.qwen-coder/sessions/{session_id}.json`，存元数据和消息历史
- `--resume` 恢复最近一次会话
- 启动时展示历史会话列表，支持选择恢复

### 九、终端 UI（rich）

- 流式 token 输出，实时打印
- 工具调用展示：图标 + 参数摘要（`📖 read_file  src/main.py:1-50`）
- `edit_file` 结果渲染为 diff（红色删除行，绿色新增行）
- 处理中 spinner 动画
- REPL 内置命令：`/clear` `/cost` `/compact` `/memory` `/skills`

---

## 架构图

```
┌─────────────────────────────────────────────────────────────────┐
│                        CLI 入口 (__main__.py)                    │
│  argparse → 解析 --yolo/--plan/--accept-edits/--model 等参数     │
│  → 确定 permission_mode → 选择 REPL 模式 或 一次性模式           │
└───────────────────────────┬─────────────────────────────────────┘
                            │ 用户输入
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│                     LangGraph StateGraph                         │
│                                                                  │
│   START                                                          │
│     │                                                            │
│     ▼                                                            │
│  ┌──────────────────────────────────────────────────────┐        │
│  │  agent 节点                                          │        │
│  │  - 加载 Memory（sideQuery 检索相关记忆注入 system）   │        │
│  │  - 加载 Skills 目录（热更新）                        │        │
│  │  - 调用 LLM（流式）                                  │        │
│  └──────────────┬───────────────────────────────────────┘        │
│                 │                                                 │
│        有 tool_calls?                                            │
│         ├── 否 ──→ END（输出最终回复）                           │
│         └── 是 ──▼                                               │
│                                                                  │
│  ┌──────────────────────────────────────────────────────┐        │
│  │  tools 节点                                          │        │
│  │  - 逐个检查 permission_mode vs 工具危险等级          │        │
│  │  - deferred 工具检测（未激活则拒绝并提示 tool_search）│        │
│  │  - 并发执行已批准工具                                │        │
│  │  - 大结果（>15KB）截断 / 持久化到磁盘                │        │
│  └──────────┬──────────────┬───────────────────────────┘        │
│             │              │                                     │
│      需要权限确认?      context 使用率?                          │
│             │              │                                     │
│             ▼              ├─ >85% ──▼                           │
│  ┌──────────────────┐  ┌──────────────────────────────┐         │
│  │ permission_      │  │  warn 节点                   │         │
│  │ confirm 节点     │  │  interrupt() 询问用户        │         │
│  │ interrupt()      │  │  → skip / compress           │         │
│  │ 展示工具调用详情  │  └──────────┬───────────────────┘         │
│  │ → approve/deny  │             │                              │
│  └────────┬─────────┘   compress ▼                              │
│           │          ┌──────────────────────────────┐           │
│     approve ──→      │  compress 节点               │           │
│     deny ──→ agent   │  Snipping：清理旧 tool result │           │
│                      │  LLM 摘要：压缩早期对话       │           │
│                      └──────────┬───────────────────┘           │
│                                 │                               │
│                     ◀───────────┘ → agent（循环）               │
└─────────────────────────────────────────────────────────────────┘
         │                  │                   │
         ▼                  ▼                   ▼
  ┌────────────┐    ┌──────────────┐   ┌──────────────────┐
  │ tools.py   │    │  memory.py   │   │  subagent.py     │
  │            │    │              │   │                  │
  │ 10 核心工具 │    │ 文件存储      │   │ Agent 工具：      │
  │ + MCP 扩展 │    │ MEMORY.md 索引│   │ 新建独立 Agent   │
  │ + deferred │    │ sideQuery    │   │ 实例，隔离 ctx   │
  │   工具激活  │    │ 语义选择      │   │ explore/plan/   │
  └────────────┘    └──────────────┘   │ general/custom  │
                                       └──────────────────┘

  ┌────────────┐    ┌──────────────┐   ┌──────────────────┐
  │ skills.py  │    │  session.py  │   │  ui.py           │
  │            │    │              │   │                  │
  │ SKILL.md   │    │ JSON 会话索引 │   │ rich 终端        │
  │ frontmatter│    │ LangGraph    │   │ 流式输出         │
  │ 工具白名单  │    │ checkpoint   │   │ diff 高亮        │
  │ /cmd 触发  │    │ 自动持久化    │   │ spinner          │
  └────────────┘    └──────────────┘   └──────────────────┘
```

---

## 模块说明

```
qwen_coder/
├── __main__.py      # CLI 入口，argparse，REPL / 一次性模式
├── agent.py         # LangGraph StateGraph 定义，节点逻辑，路由函数
├── tools.py         # 10 工具定义 + 权限检查 + deferred 机制 + execute_tool()
├── sandbox.py       # OpenSandbox 会话级单例，文件上传同步，降级策略
├── subagent.py      # agent 工具，子 Agent 实例化，类型配置
├── memory.py        # 文件记忆读写，MEMORY.md 索引，sideQuery 检索
├── skills.py        # SKILL.md 加载，/cmd 解析，prompt 注入
├── session.py       # 会话 JSON 索引，LangGraph checkpointer 封装
├── compressor.py    # 4 级压缩：预算截断 → snip → micro-compact → LLM 摘要
├── prompt.py        # system prompt 构建，记忆 / todo / skill 注入
├── ui.py            # rich 终端渲染，spinner，diff 高亮，工具调用展示
└── state.py         # AgentState TypedDict 定义
```

---

## 快速开始

```bash
pip install -e .

# 设置 API Key
export ANTHROPIC_API_KEY=sk-...
# 或 OpenAI 兼容接口
export OPENAI_API_KEY=xxx
export OPENAI_BASE_URL=http://localhost:8000/v1

# 交互模式
mini-claude

# 一次性执行
mini-claude "帮我写一个 Python 的快速排序"

# 只读模式（不执行写操作）
mini-claude --plan

# 跳过所有确认
mini-claude --yolo

# 从上次会话继续
mini-claude --resume
```

---

## 权限模式

| 模式 | 参数 | 说明 |
|---|---|---|
| `default` | （默认）| 读操作自动执行，写/执行操作需确认 |
| `plan` | `--plan` | 只读，禁止写文件和执行命令 |
| `acceptEdits` | `--accept-edits` | 自动批准文件编辑，执行命令仍需确认 |
| `bypassPermissions` | `--yolo` | 全部自动执行，无确认弹窗 |
| `dontAsk` | `--dont-ask` | 自动拒绝所有需确认操作（CI 场景）|

---

## Skills 系统

在 `.claude/skills/` 下创建 Markdown 文件即可扩展 Agent 能力：

```markdown
---
name: react-patterns
description: React 最佳实践和常见模式
when_to_use: 开发 React 组件时
allowed-tools: read_file, write_file, edit_file, run_shell
user-invocable: true
context: inline
---

你是一个 React 专家。编写组件时遵循以下规范：
- 优先使用函数组件 + Hooks
- ...
```

REPL 中用 `/react-patterns` 触发，或 Agent 通过 `skill` 工具自动调用。

---

## 依赖

```
langgraph>=0.2
langchain-anthropic           # Anthropic 后端
langchain-openai              # OpenAI 兼容后端
rich                          # 终端 UI
pyyaml                        # SKILL.md frontmatter 解析
opensandbox                   # 沙箱运行时客户端
opensandbox-code-interpreter  # Python/多语言代码执行
```

OpenSandbox server 需单独启动（依赖 Docker）：

```bash
uvx opensandbox-server init-config ~/.sandbox.toml --example docker
uvx opensandbox-server
```
