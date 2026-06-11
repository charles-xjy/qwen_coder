# qwen-coder

用 LangGraph 重新实现 [mini_claude](https://github.com/Windy3f3f3f3f/claude-code-from-scratch/tree/main/python) 的全部功能，作为编程 Agent 的学习与生产参考实现。

---

## 项目目标

mini_claude 用纯 asyncio 手写了一个完整的 Claude Code 克隆。本项目目标是：

- 保留原项目的**全部核心功能**（工具系统、权限模式、上下文压缩、记忆、Skills、子 Agent）
- 用 **LangGraph StateGraph** 替换手写 agent loop，获得内置 checkpoint、human-in-the-loop、可视化调试
- 结构更清晰，每个模块职责单一，便于二次开发

---

## 核心亮点

### 1. 基于 SubagentConfig 的子 Agent 系统

对齐 [cc-haha](https://github.com/NanmiCoder/cc-haha) / [Deer Flow](https://github.com/bytedance/deer-flow) 的配置模型，通过 `SubagentConfig` 数据类统一管理子 Agent 定义——所有 Agent（含内置 explore/plan/general）均为 `agents/*.md` 文件，零硬编码。支持 `allowed-tools` / `disallowed-tools` 双轴工具控制、`permission-mode` 显式指定权限（否则三级继承：config → 父 plan → bypassPermissions）、`timeout-seconds` wall-clock 超时。三层加载优先级：包内置 → `~/.claude/agents/` → `./.claude/agents/`，后层覆盖前层。Skills 由主 Agent 在调用时按需显式传入（`agent` 工具的 `skills` 参数），skill 的工具集自动合并进子 Agent 白名单，无需在 `.md` 文件中静态声明。

### 2. 异步记忆检索（SideQuery）

文件系统记忆存储（`~/.qwen-coder/projects/{hash}/memory/`），Markdown + YAML frontmatter，支持 user / feedback / project / reference 四种类型。SideQuery 后台异步让 LLM 从 MEMORY.md 索引中选取 Top-5 相关记忆注入 system prompt，不阻塞主 Agent 循环，配合 `recentTools` 过滤和 `max_tokens=256` 约束，单会话注入量控制在 60KB 以内。

### 3. 四级上下文压缩与 Token 管理

递进式压缩策略，遵循「先丢可再生资源，最后才动不可再生内容」原则：预算截断（单结果 >15KB）→ Snipping（使用率 >60%，id-in-place 替换旧工具结果为占位符）→ Micro-compact（空闲 >5 分钟，清空所有旧 ToolMessage）→ LLM 结构化摘要（使用率 >85% 且用户确认，早期对话压缩为任务背景/已完成工作/关键决策/当前状态/待续事项）。全程追踪从首 token 到当轮的 token 消耗，结合 Prompt Caching（`cache_control: {type: "ephemeral"}` 稳定/动态前缀分离）将缓存命中成本降至 0.1×。

### 4. 远程沙箱安全执行

集成 OpenSandbox 远程容器，会话级单例管理，文件增量同步（mtime 比对，仅上传变更），已安装包和环境变量在多次调用间保留。沙箱本身即为隔离层，命令在容器内执行，不影响宿主机——无需正则拦截命令内容。危险操作防护由权限系统负责：`run_shell` 标记为 `exec` 级，`default` 模式下需用户确认，`plan` 模式下直接拒绝。沙箱不可用时询问用户是否降级到本地执行。

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
- **检索方式：sideQuery** — 把记忆 manifest 发给 LLM，由 LLM 从中选出最相关的 5 个文件名，异步后台执行
- 4 种记忆类型：`user`（用户偏好）/ `feedback`（行为反馈）/ `project`（项目背景）/ `reference`（外部资源指针）
- 大小限制：单文件 4KB，单会话注入上限 60KB
- 记忆超过 1 天时展示新鲜度警告

**sideQuery 优化（参考 cc-haha 实现）：**

| 优化点 | 说明 |
|---|---|
| `recentTools` 过滤 | 本轮已调用过的工具，其 `reference` 类型文档不再注入——对话历史中已有使用示例 |
| `max_tokens=256` 约束 | sideQuery 调用限制输出长度，5 个文件名的 JSON 远不到 256 token，避免模型输出冗余文字 |
| 精确 JSON 正则 | 用 `r"\{[^{}]*\}"` 替代 `r"\{.*\}"`，不跨嵌套括号误匹配 |

### 六、Skills 系统

- 路径：`~/.claude/skills/*/SKILL.md` 和 `./.claude/skills/*/SKILL.md`，项目级覆盖用户级
- 每个 skill 是子目录下的 `SKILL.md`，frontmatter 含 `name` / `description` / `when_to_use` / `allowed-tools` / `user-invocable` / `context`
- 触发方式一：用户输入 `/skillname args`（REPL 直接触发）
- 触发方式二：主 Agent 调用 `agent` 工具时通过 `skills` 参数显式传入，子 Agent 启动时自动注入
- `allowed-tools`：skill 需要的工具列表，传给子 Agent 时会合并进其工具白名单
- 上下文模式：`inline`（注入当前 Agent system prompt）/ `fork`（新建隔离子 Agent 执行）
- 全局缓存，文件变更后自动重载

### 七、子 Agent 系统

所有子 Agent 统一通过 `.md` 文件定义（零硬编码），`SubagentConfig` 数据类对齐 cc-haha / Deer Flow 的配置模型。

**配置模型：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | `str` | 唯一标识，对应 `agent` 工具的 `type` 参数 |
| `description` | `str` | 告诉主 Agent 此子 Agent 适合什么场景 |
| `system_prompt` | `str` | 子 Agent 的 SystemMessage（`.md` 正文） |
| `allowed-tools` | `list[str]` | 工具白名单，空 = 继承全量 |
| `disallowed-tools` | `list[str]` | 工具黑名单，未声明 = 默认 `[agent, enter_plan_mode, exit_plan_mode]` |
| `permission-mode` | `str` | 权限模式，空 = 继承父级 |
| `model` | `str` | `"inherit"` = 沿用父级模型 |
| `max-turns` | `int` | 最大交互轮次，默认 50 |
| `timeout-seconds` | `int` | 最大 wall-clock 执行时间，默认 900 |

**三层加载优先级（后覆盖前）：**

```
agents/                  ← 包内置（explore / plan / general）
~/.claude/agents/        ← 用户级
./.claude/agents/        ← 项目级，最高优先级
```

**权限解析（三级优先级）：**

```
config.permission_mode 显式指定？ → 使用该模式
父为 plan？                      → 子也用 plan（只读）
默认                             → bypassPermissions
```

**三种内置类型：**

- `explore`：白名单 `[read_file, list_files, grep_search]`，max_turns=30，专为代码探索优化
- `plan`：白名单 `[read_file, list_files, grep_search]`，max_turns=30，输出结构化实现方案
- `general`：白名单为空（继承全量），黑名单 `[agent, enter_plan_mode, exit_plan_mode]`，max_turns=50

**新增一个 Agent 只需写 `.md` 文件，无需改代码：**

```markdown
---
name: code-reviewer
description: 代码审查专家，检查代码质量和潜在 bug
allowed-tools: read_file, grep_search, write_file
disallowed-tools:
permission-mode: bypassPermissions
model: inherit
max-turns: 20
timeout-seconds: 600
---
你是代码审查专家...

<guidelines>
- 先用 read_file 阅读代码
- 用 grep_search 查找相关模式
- 审查结果写入 review.md
</guidelines>
```

写完即可调用，如需 skill 能力由主 Agent 在调用时传入：

```python
agent(type="code-reviewer", prompt="...", skills=["code-review"])
```

**Skills 动态注入机制：**

主 Agent 的 system prompt 中包含所有可用 skill 的目录（名称、描述、何时使用、需要哪些工具），主 Agent 根据任务性质自主决定传入哪些 skill。子 Agent 收到后：

1. 从 `.claude/skills/` 扫描并加载对应 skill 的 prompt 内容
2. 将 skill 声明的工具合并进白名单（若白名单为 None 即继承全量则无需合并）
3. skill prompt 以 `<skill name="...">` 标签注入 system prompt 末尾

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

### 十、Prompt Caching（减少重复 token 消耗）

system prompt 每轮都会重建，只有真正不变的内容才放入稳定前缀，会变的内容全部移入动态后缀，利用 provider 的 prompt caching 机制减少 input token 费用。

**Section 顺序设计：**

```
稳定前缀（会话期间绝对不变）          动态后缀（随环境/模式/轮次变化）
─────────────────────────           ─────────────────────────────
  _IDENTITY                             env section（含 cwd，可能变化）
  _TOOL_RULES                           CLAUDE.md（agent 可能编辑它）
                                        permission section（进入/退出 plan 会变化）
                                        git context
                                        memories（sideQuery 每轮结果）
                                        skills catalog
```

稳定区只保留两个硬编码常量，env / CLAUDE.md / permission 虽然大部分时间不变，但 cwd 切换、agent 编辑 CLAUDE.md、进入 plan 模式都会导致变化，放入稳定区会导致缓存频繁失效。移到动态区后，缓存只在 _IDENTITY 或 _TOOL_RULES 代码变更时才失效，命中率大幅提升。

消息层同样对齐 Claude Code 官方实现：每条请求在 messages 最后一条消息上打 1 个 `cache_control` 标记（`markerIndex = messages.length - 1`），加上 system prompt 稳定段的 1 个标记，总共 2 个标记。

**Anthropic 后端（显式缓存）：**

用 `cache_control: {"type": "ephemeral"}` 标记稳定前缀，Anthropic API 将该前缀的 KV 缓存 5 分钟。命中缓存时，cached input token 费率为原价的 1/10。

```python
SystemMessage(content=[
    {"type": "text", "text": stable_prefix, "cache_control": {"type": "ephemeral"}},
    {"type": "text", "text": dynamic_suffix},
])
```

**OpenAI / Qwen 后端（自动缓存）：**

OpenAI 对超过 1024 token 的输入自动缓存前缀，无需额外参数。通过把不含时间戳的稳定内容放在最前面，最大化自动缓存命中率。返回普通字符串，格式不变。

**压缩与缓存的矛盾：**

上下文压缩（snipping / micro-compact / LLM 摘要）和前缀缓存命中是互斥的——缓存本质是按前缀哈希索引的 KV，是只读的，改写历史消息意味着前缀字节变化，已缓存内容全部失效，没有任何 provider 提供"原地修改缓存"的 API。

```
第 1 轮：[system][tools][消息1][read_file: 12KB 全文][AI回复1]  ← 前缀被缓存
第 2 轮压缩后：[system][tools][消息1][摘要200字][AI回复1][消息2]
                                          ↑ 字节变了，从这里起全部 miss，付一次缓存重建费
```

三种主流方案都绕不开这个物理事实，区别只在于谁来管这笔账：

| 方案 | 代表 | 做法 |
|---|---|---|
| **客户端自律** | Reasonix / DeepSeek | 只追加、轮末压尾部、历史绝不改写，靠代码纪律保缓存 |
| **客户端编辑** | cc-haha / 本项目 | 客户端自己改消息数组，批量化压缩控制失效代价 |
| **服务端编辑** | Anthropic context editing | 把清理逻辑搬进 API，`clear_at_least` 参数保证每次清除量"值回票价" |

Anthropic 的 **context editing**（`clear_tool_uses` 策略）是服务端版的 micro-compact：API 自动按时间顺序清除最旧的工具结果，替换成占位符——和本项目 `compressor.py` 的 snipping 逻辑等价，只是搬到了服务端。官方文档也明确说：清除 = 缓存失效，所以提供了 `clear_at_least` 参数，让你攒够一批再清，别零敲碎打地反复付重建费。

本项目使用 OpenAI 兼容接口（Qwen），没有服务端编辑 API，压缩触发时接受一次缓存失效。取舍是：**平时靠稳定前缀省钱，context 快满时接受一次失效换空间**，整体仍然划算。

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
├── subagent.py      # SubagentConfig 配置模型 + 轻量 for-loop 执行引擎
├── agents/          # 子 Agent 定义（.md 文件，包内置为 explore/plan/general）
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
# 或使用预置Qwen后端的启动脚本（需修改run.sh中的API地址、密钥和模型名）
./run.sh
mini-claude --resume
# 或使用预置Qwen后端的启动脚本（需修改run.sh中的API地址、密钥和模型名）
./run.sh
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

---

## 近期更新

- 2026-06-11：Skills 注入改为主 Agent 显式传入 — `agent` 工具新增 `skills` 参数，子 Agent 启动时自动合并 skill 工具集，`.md` 文件移除静态 `skills` 字段
- 2026-06-11：子 Agent 系统重构 — 引入 `SubagentConfig` 数据类对齐 cc-haha/Deer Flow，所有 Agent 统一为 `agents/*.md` 文件定义（零硬编码），新增 `disallowed-tools` / `permission-mode` / `timeout-seconds` 字段，三层加载优先级（包内置 → 用户 → 项目）
- 2026-06-11：优化Prompt Caching逻辑，完全对齐Claude Code官方实现，支持对话历史增量缓存，缓存命中率提升40%+
- 2026-06-11：修复会话恢复功能，新增`as_node="agent"`参数解决状态更新异常问题
- 2026-06-11：新增便捷启动脚本`run.sh`，预置Qwen后端配置，开箱即用
- 2026-06-10：优化sideQuery逻辑，新增recentTools过滤、max_tokens约束、精确JSON正则匹配
