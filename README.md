# qwen-coder

> 项目针对大模型多轮对话中重复 token 成本高、上下文溢出与跨会话记忆遗忘等问题，以 **Prompt 前缀缓存优化**为核心：将 system prompt 拆分为"绝对不变的稳定前缀"与"随环境/模式/轮次变化的动态后缀"，最大化 provider 前缀缓存命中、显著降低 input token 费用；针对"压缩必然导致前缀缓存失效"这一物理矛盾，设计**缓存对齐的四级压缩**——只在缓存本已过期的时机（空闲超 TTL）清理、并把多次小压缩批量为一次前缀重建，在化解上下文溢出的同时把缓存失效代价摊到最低；辅以跨会话文件记忆保障知识持续积累、远程沙箱确保代码安全执行。

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

压缩遵循「先丢可再生资源，最后才动不可再生内容」原则，四级从轻到重：

1. **预算截断**（`core/tools.py` 内实时发生）：单个工具结果 >10000 字符时直接截断，保留头尾、中间打 `...[truncated]` 标记，文件仍在磁盘可按需重读。
2. **Time-based Micro-compact**（空闲 >5 分钟）：Qwen 计费缓存 TTL 为 5 分钟，超时即视为缓存失效——反正要重写前缀，不如顺手把旧的可再生工具结果内容清空，保留最近 5 条。只改本轮发送的 history 副本，不写回 state（对齐 cc-haha 的 time-based microcompact，触发条件从 Anthropic 的 1h cache TTL 改为 Qwen 的 5min）。
3. **LLM Snip**（每积累 20 条消息触发一次）：不看 token 使用率，而是和 cc-haha 一样**让模型自己判断**哪段历史冗余。LLM 返回要折叠的消息序号区间 `[start, end]` 和该区间的摘要，只把这一段折叠成 1 条摘要消息，其余消息**原封不动**（例如 30 条里 LLM 认为第 5–13 条冗余，就把这 9 条换成 1 条摘要，剩下 21 条保持原样）。最近 6 条永不参与压缩。
4. **LLM 全量摘要**（使用率 >85% 且用户确认）：`warn` 节点 `interrupt()` 询问用户，确认后把全部历史折叠为 1 条结构化摘要（对齐 cc-haha `compactConversation`，9 段式：主要请求/技术要点/文件代码/错误修复/问题解决/全部用户消息/待续事项/当前工作/下一步）。

结合 Prompt Caching（`cache_control: {type: "ephemeral"}` 稳定/动态前缀分离）将缓存命中成本降至 0.1×。

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

压缩遵循"先丢可再生资源，最后才动不可再生内容"的原则，4 级从轻到重依次触发。Snip 和 Micro-compact 都在 `agent_node` 调用 LLM 前执行。

#### 级别 1：预算截断（core/tools.py 内实时发生）

单个工具结果超过 10000 字符时，`execute_tool()` 在返回前直接截断，保留头部 2500 字符 + 尾部 500 字符，中间加 `...[truncated]` 标记。Agent 看到截断标记后可以用 `read_file` 加 `offset` 参数重新读取所需的特定行范围。

- 触发时机：每次工具调用，实时处理
- 信息损失：中间部分丢失，但文件仍在磁盘，可按需重读
- 对 Agent 透明：截断结果直接写入 ToolMessage，Agent 只看到截断版

#### 级别 2：Time-based Micro-compact（空闲 > 5 分钟）

对齐 cc-haha 的 time-based microcompact：Qwen 计费缓存 TTL 是 5 分钟，距上次 API 调用超过 5 分钟时，缓存几乎必然已失效——反正下一轮要重写整个前缀，不如趁机把旧的**可再生工具结果**内容清空，减少要重写的体积。

- 触发条件：`now - last_api_call_time > 300s`
- 操作对象：可再生工具结果（`read_file` / `grep_search` / `list_files` / `run_shell` / `web_fetch`），保留最近 5 条
- 关键差异：**只修改本轮发送给 LLM 的 history 副本，不写回 state**（缓存已失效，无需持久化改动）

```
清理前：
  [read_file 结果] "def main(): ..."   # 旧
  [grep 结果]      "import os..."      # 旧
  ... 最近 5 条可再生结果保留 ...

清理后：
  [read_file 结果] "[缓存已失效，旧结果已清理]"
  [grep 结果]      "[缓存已失效，旧结果已清理]"
  ... 最近 5 条不动 ...
```

#### 级别 3：LLM Snip（每积累 20 条消息触发一次）

这是和 cc-haha 一致的核心机制：**不看 token 使用率，让模型自己判断哪段历史冗余**。每当 non-system 消息数比上次检查时又多了 20 条，就把整段历史（带序号）发给 LLM，让它判断：

1. 是否需要压缩？不需要则返回 `{"needed": false}`，本轮跳过
2. 需要的话，返回要折叠的消息序号区间 `[start, end]` 和这段的摘要

只把 `[start, end]` 这一段折叠成 **1 条摘要消息**，区间外的消息**原封不动**。最近 6 条消息永不参与压缩（即使 LLM 给了越界序号也会被强制裁回）。

```
压缩前（30 条消息）：
  [0] 用户：做个项目
  [1] AI：读 package.json
  [2] 工具结果：...
  ...
  [5-13] 一堆探索性的读文件 / grep / 架构讨论   ← LLM 判定这段冗余
  ...
  [29] 用户：继续补登录

压缩后（22 条消息）：
  [0] 用户：做个项目
  [1] AI：读 package.json
  [2] 工具结果：...
  [对话历史摘要（原第 5–13 条已折叠）] 读了 X，发现 Y，决定用 Z 方案...
  ...                                              ← 其余 21 条原封不动
  [29] 用户：继续补登录
```

实现：用 `RemoveMessage` 删除被折叠的旧消息，插入 1 条摘要 `HumanMessage`，通过 `messages_at_last_snip` 计数器控制 20 条的检查间隔（不论是否实际压缩都重置计数器，避免无冗余时每轮都问 LLM）。

#### 级别 4：LLM 全量摘要（使用率 > 75%，用户确认）

使用率超过 75% 时，`warn 节点` 通过 `interrupt()` 弹出提示，询问用户是否立即压缩。用户选择"压缩"且使用率 > 85% 时，进入全量摘要——对齐 cc-haha 的 `compactConversation`：

1. 把**全部历史**发给 LLM（不保留任何原始消息）
2. LLM 输出 9 段式结构化摘要（主要请求 / 技术要点 / 文件代码 / 错误修复 / 问题解决 / 全部用户消息 / 待续事项 / 当前工作 / 下一步），先用 `<analysis>` 草稿分析再输出 `<summary>`
3. 用 `RemoveMessage` 删除所有原始消息，只留 1 条摘要消息

> 注：cc-haha 全量压缩后还会把会话期间读过的文件（最多 5 个）重新读一遍注入回来（attachment），本项目暂未实现这一步，压缩后模型如需文件内容自行 `read_file` 重读即可。

#### 压缩流程总览

```
agent_node 调用 LLM 前：
   ① Time-based mic：空闲 >5min → 清空旧工具结果（改本地副本）
   ② LLM snip：消息数比上次 +20 → 问 LLM 哪段冗余 → 局部折叠（写回 state）
        ↓
   调用 LLM → tools 节点执行 → token_router 检查使用率
        ↓
   < 75%  → 继续循环
   ≥ 75%  → warn 节点 interrupt() "使用率 78%，是否压缩？"
        ↓
   用户选"压缩" + 使用率 >85% → compress 节点全量 LLM 摘要
   用户选"跳过" → 继续循环
```

| 级别 | 触发时机 | 操作对象 | 实现机制 |
|---|---|---|---|
| **预算截断** | 单个工具结果 > 10000 字符 | 工具返回值 | 直接截断字符串 |
| **Time-based mic** | 空闲 > 5 分钟（Qwen 缓存 TTL） | 旧的可再生工具结果，保留最近 5 条 | 改本地 history 副本，不写 state |
| **LLM snip** | 每积累 20 条消息 | LLM 判定的冗余区间 | LLM 决定区间 → `RemoveMessage` 局部折叠为 1 条摘要 |
| **LLM 全量摘要** | 使用率 > 85% + 用户确认 | 全部历史 | `RemoveMessage` 删除全部 + 插入 1 条摘要 |

#### 附：cc-haha 完整压缩流程梳理与对比

本项目的 4 级压缩参考了 cc-haha（Claude Code 同源实现）的设计。为便于理解取舍，下面先完整梳理 cc-haha 的压缩流水线，再逐项对比。

**cc-haha 的压缩在主循环里每轮 API 调用前按固定顺序执行（`query.ts`）：**

```
snip → microcompact → (context-collapse 投影) → autocompact ┐
                                                  ├─ 先试 session-memory 压缩
                                                  └─ 再走 完整 compact
                          partial compact = 不在循环里，由用户在 UI 选消息触发
```

cc-haha 共有 **5 条**压缩路径（其中 3 条在外部 build 里是 `@generated stub`，经 DCE 不执行）：

| cc-haha 路径 | 触发时机 | 压缩策略 | 是否调 LLM | 外部 build |
|---|---|---|---|---|
| **snip**（`HISTORY_SNIP`） | 每轮最早，先于 microcompact | 裁剪历史消息腾 token，节省量 `tokensFreed` 单独 plumb 给后续阈值判断（尾部 usage 看不见） | 否 | stub（不执行） |
| **microcompact / time-based** | 距上条 assistant 消息 > **60min**（服务端 cache TTL=1h 必失效） | 清空旧的可压缩工具结果内容，保留最近 N 条，**改本地消息** | 否 | ✅ 但默认 flag 关 |
| **microcompact / cached** | **计数触发**：活跃可压缩工具结果**个数** > `triggerThreshold` | 按 `tool_use_id` 走 **cache-editing API** 在服务端删结果，**不改本地**，靠 `pinnedEdits` 复发位置 | 否 | stub（不执行） |
| **session-memory 压缩**（实验） | 达 autocompact 阈值时**优先**尝试 | **复用后台早已抽取好的 memory 文件当摘要**，保留 `lastSummarizedMessageId` 之后的消息（向前扩到 minTokens=10K/5 条文本，封顶 40K） | 否（摘要平时后台攒） | 需 `tengu_session_memory`+`tengu_sm_compact` 双 flag，默认关 |
| **完整 compact**（`compactConversation`） | 自动达阈值（有效窗口 − 13K）**或** 手动 `/compact` | forked-agent 共享 prompt cache 做总结 → 9 段式摘要 → **全替换** → 重注入最近读过的文件(≤5)/plan/skill/tools/MCP delta；带 PTL 重试 + 连续失败 3 次熔断 | **是** | ✅ 默认主路径 |
| **partial compact** | **用户手动**选一条枢轴消息 | `from`=总结枢轴之后保留之前；`up_to`=总结之前保留之后。只压一半，另一半逐字保留，用 `preservedSegment{head,anchor,tail}` 修补磁盘链路 | **是** | ✅ |

**关于 session-memory 的关键点（容易误解）**：它的摘要**不是压缩那一刻生成的**，而是会话过程中由 post-sampling hook（`extractSessionMemory`）**后台分期抽取**写进磁盘文件——满足"token 增量 + 工具调用数"双门槛时，开一个 forked subagent 增量更新 memory 文件并记下 `lastSummarizedMessageId`。压缩时直接把这份现成笔记读出来顶替，省掉一次总结 API 调用。但它是**优先级**而非替代关系：memory 没攒够、是空模板、边界对不上、或压完仍超阈值，都会 `return null` **回落到完整 compact**——所以完整 compact 永远是兜底，不会变成死代码。

**易混淆点澄清（速记版）**：下面是 cc-haha 5 层的精简心智模型，特别标注三个最常见的误解：

| # | 机制 | 干什么 | 调 LLM？ |
|---|---|---|---|
| 1 | snip | 裁历史消息腾 token | 否 |
| 2a | mic time-based（空闲 > 1h） | **清空**旧工具结果内容、保留最近 5（非删消息） | 否 |
| 2b | mic cached（活跃工具数超阈值） | **cache-editing API 删旧工具结果**（⚠️**不是总结对话**） | 否 |
| 3 | session-memory（达阈值优先） | **复用后台攒好的笔记当摘要**（与 2b 无关，是另一条独立路径） | 否（后台早已攒） |
| 4 | 完整 compact（兜底 / 手动 `/compact`） | 现场全量总结 + 全替换 + 重注入 | 是 |
| 5 | partial compact（**用户手动**选枢轴） | 总结一半、保留一半 | 是 |

三个易错点：
1. **2b 不总结**：cached microcompact 只是用 cache-editing 删旧工具结果以保护缓存前缀，全程不调 LLM、不产生摘要。
2. **完整压缩的快通道是 session-memory（#3），不是 2b**：两者毫无关系——2b 删工具结果，session-memory 复用后台笔记当摘要。
3. **cc-haha 没有"自动判冗余折叠"**：能"折叠一段保留其余"的 partial compact 是**用户手动**选区间。本项目的级别 3（自动 LLM snip：自动触发 + LLM 自主决定冗余区间）是 qwen-coder 在这一层比 cc-haha 多做的。

**逐项对比：**

| 维度 | 本项目（qwen-coder） | cc-haha |
|---|---|---|
| 级别 1 / 预算截断 | 单工具结果 > 10000 字符头尾截断，对 Agent 透明 | 无单独"截断级"，靠 FileRead token 上限 + 工具结果存储 |
| 级别 2 / time-based mic | 空闲 > **5min**（Qwen 缓存 TTL=5min），清旧工具结果，保留 5 条，改本地副本 | 空闲 > **60min**（服务端 TTL=1h），逻辑基本一致；阈值差异源于两边缓存 TTL 不同 |
| 级别 3 / LLM snip | **每积累 20 条消息**自动触发，**让 LLM 自己判定冗余区间 [start,end]** 折叠为 1 条摘要，其余原封不动，最近 6 条永不压 | snip 仅腾 token（stub）；"折叠一段保留其余"对应的是 **partial compact，但它是用户手动选区间**。本项目把它做成了**自动 + LLM 决策区间**，更激进 |
| 级别 4 / 全量摘要 | 使用率 **> 75% interrupt 问用户**，> 85% + 确认才全量摘要；9 段式 prompt 对齐 cc-haha | 达阈值（有效窗口 − 13K）**自动压缩、不询问**；同样 9 段式、含 `<analysis>` 草稿 |
| 触发度量 | 上下文**使用率比例**（60/75/85%）+ 消息计数 | 多为**绝对 token 阈值**（窗口 − 固定 buffer） |
| Prompt cache 保护 | time-based mic 减少重写；未做 forked-agent 缓存共享 / cache-editing | 重度优化：forked-agent 共享前缀缓存、cache-editing API、缓存断裂检测 |
| 后台增量摘要 | **无**：`memory.py` 是跨会话**文件记忆 + sideQuery 检索**，不用作压缩摘要 | session-memory 把"后台攒摘要"直接用作压缩快通道 |
| 压缩后重注入 | 无，模型按需 `read_file` 重读 | 重注入文件/plan/skill/tools/MCP |
| 健壮性 | snip/摘要失败则原样返回 | PTL 自动截头重试、连续失败熔断、递归防护 |

**总体差异**：本项目走"**比例阈值 + 用户可介入 + LLM 自主决定折叠区间**"的轻量路线，把 cc-haha 里"手动 partial"升级成了"自动 LLM snip"，但省略了 cc-haha 围绕 prompt cache 的大量工程（forked-agent 缓存共享、cache-editing、后台增量 session-memory、压缩后重注入、PTL 重试与熔断）。cc-haha 更偏"省钱省 cache、全自动、强兜底"，本项目更偏"简单可控、关键处让用户确认"。

### 五、文件记忆系统

- 存储路径：`.memory/`（项目级，随代码库版本控制）
- 每条记忆是一个 Markdown 文件，YAML frontmatter 含 `name` / `description` / `type`
- `MEMORY.md` 为自动维护的索引（最多 200 行，25KB 上限）
- 4 种记忆类型：`user`（用户偏好）/ `feedback`（行为反馈）/ `project`（项目背景）/ `reference`（外部资源指针）
- 大小限制：单文件 4KB，单会话注入上限 60KB
- 记忆超过 1 天时展示新鲜度警告

**每轮对话涉及两次独立 LLM 调用：**

| 调用 | 时机 | 方式 | 说明 |
|------|------|------|------|
| sideQuery（检索） | 主 LLM 调用前 | 串行 await | 将 MEMORY.md 索引发给 LLM，选出最相关的 ≤5 条，包装成一条独立消息 **append 进消息历史**（见下「记忆注入：append-only」） |
| auto_save_memory（写入） | 主 LLM 最终回复后 | `asyncio.create_task` fire-and-forget | 分析本轮对话，判断是否有值得长期保存的内容，有则直接写文件 |

**sideQuery 优化：**

| 优化点 | 说明 |
|---|---|
| `recentTools` 过滤 | 本轮已调用过的工具，其 `reference` 类型文档不再注入——对话历史中已有使用示例 |
| `max_tokens=256` 约束 | 5 个文件名的 JSON 远不到 256 token，避免模型输出冗余文字 |
| 精确 JSON 正则 | 用 `r"\{[^{}]*\}"` 替代 `r"\{.*\}"`，不跨嵌套括号误匹配 |

**AutoDream：定期记忆整合（对齐 cc-haha autoDream）**

积累足够多的会话后，在会话结束时自动触发一次深度整合：合并重复条目、删除过时记忆、更新陈旧内容。

双门槛触发条件（两者同时满足）：

| 门槛 | 默认值 | 说明 |
|------|--------|------|
| 时间 | ≥ 24 小时 | 距上次整合的时间 |
| 会话数 | ≥ 5 次 | 上次整合后累计的会话数 |

状态持久化在 `.memory/.dream_state.json`，互斥锁防止并发，整合完成后重置计数器。不满足门槛时立即返回，不阻塞退出。

**记忆注入：append-only（缓存友好）**

召回的记忆**不放进 system prompt**，而是由 `build_memory_message` 包装成一条独立的 `HumanMessage`，**append 到消息历史末尾**。一旦注入即固定位置、之后不再变动/重选——因此除首次召回那一轮外，该记忆消息在后续轮次都能被前缀缓存覆盖。

```
持久化在 state 里的消息历史（system 不入 state，每轮临时前置）：
  human1
  memory1     ← 本轮 sideQuery 召回到的新记忆，紧跟 human1 后
  ai1
  human2      ← 本轮无新记忆，则不插
  ai2
  human3
  memory3     ← 本轮又召回到新的
  ai3

实际发给 LLM：[SystemMessage(prompt)] + 上面这串历史
```

设计要点：

- **为什么不放 system prompt**：旧设计每轮按 query 重选一组记忆拼进 system 动态后缀，内容每轮都变 → 这段永远是全价 input、进不了缓存。改为 append-only 后，记忆消息位置固定，老记忆走缓存，只有"本轮新召回的那条"是全价——与「缓存优化为核心」的主线一致。
- **不会重复注入**：`surfaced_memories` 记录已召回的文件名，下轮不再选；同一用户回合内的多步工具循环也只注入一次。
- **memory 不是每轮都有**：只有 sideQuery 返回了**新**记忆的轮次才插一条；返回 `[]` 的轮次没有 memory 消息。
- **累积量兜底**：append-only 下记忆只进不出（不会因话题切换撤下旧记忆），靠单会话注入上限 `_MAX_SESSION_BYTES=60KB` 封顶。
- **与 cc-haha 的差异**：cc-haha 的召回结果落在 tool_result（模型自己 grep），本项目落在主动注入的 `HumanMessage`；两者都不污染稳定前缀。

#### 三方长期记忆对比（qwen-coder / cc-haha / Reasonix）

三者存储层高度同源（都是 `MEMORY.md` 索引 + 单文件 Markdown + frontmatter，上限多为 200 行/25KB），但**检索、注入、写入**三条链路是三种不同架构。（更完整的四方对比含 CodeWhale，见 [`四项目对比_记忆系统.md`](四项目对比_记忆系统.md)。）

> 注：以下 Reasonix 结论经 `deepseek-reasonix` 源码核实——它**没有 BM25、也没有专门的 memory 检索工具**，召回是"索引在前缀 + 模型用 `read_file` 按需读"，与 cc-haha 的 grep 同属模型驱动。

| 维度 | 本项目（qwen-coder） | cc-haha（AutoMem） | Reasonix（DeepSeek 系） |
|---|---|---|---|
| 索引位置 | **不保留索引**，靠 sideQuery 预选 | `MEMORY.md` 索引常驻 system 前缀 | 文档全文 + `MEMORY.md` 索引折进 system 前缀（启动时一次，`Block()` 纯函数渲染、空安全） |
| 检索方式 | **独立小 LLM**（sideQuery 选 Top-5） | 主模型自己 grep/read | 主模型用通用 `read_file` 读链接文件（**无 BM25/无专用检索工具**） |
| 注入内容 | 选中记忆的**全文**注入消息历史 | grep 结果进 tool_result | 不主动注内容；仅在**变更**时注 `<memory-update>` delta |
| 召回额外成本 | 每轮 1 次小 LLM 调用（决策+选取） | 主模型多一轮往返（仅需要时） | 主模型多一轮往返（仅需要时，与 cc-haha 同结构） |
| 写入/更新生效 | `auto_save` 后台写文件，下轮 sideQuery 才召回 | 模型用 Write 写文件 + 更新索引 | `remember`/`forget` → `pendingMemory` 队列 → 下一轮在 user 消息头注入 `<memory-update>`，系统前缀不动 |
| 缓存代价 | 老记忆固定历史位置可缓存；新召回那轮全价 | 索引常驻前缀稳定；**写记忆那轮破前缀** | **运行时系统前缀永不变**，只有变更轮多 ~50 token，重启才折回前缀 |
| 冲突处理 | append-only，新旧并存（靠 surfaced 去重） | 模型自行判断 | 近因效应：尾部 `<memory-update>` 自然覆盖前缀里的旧索引 |

**一句话定位：**

- **本项目**：小 LLM 预选全文 → append-only 注入历史。召回可靠（强制每轮检索、不靠模型自觉），代价是每轮 1 次小调用 + 注入全文。
- **cc-haha**：索引常驻前缀，主模型自己 grep。省调用，但召回靠模型自觉且占主循环往返。
- **Reasonix**：索引/文档折进确定性前缀 + 模型按需 `read_file` + 变更走尾部 delta。写入路径缓存最稳；召回成本结构与 cc-haha 相同（都靠主模型一轮往返）。

**可借鉴的两点**（与本项目"缓存优化为核心"最契合）：

1. **索引进前缀 + 内容按需取**：Reasonix/cc-haha 只把索引放进可缓存前缀、全文等模型真要用时才 `read_file`/grep（Reasonix 文档：50 条记忆时索引 ~1500 token vs 全文注入 ~7500 token）。本项目把"该知道有哪些记忆"外包给了 sideQuery 小模型，主模型其实看不到完整目录。
2. **写记忆走尾部 delta**（Reasonix）：若未来把索引放进前缀，更新时学 Reasonix 的尾部 `<memory-update>` 注入，避免每次写记忆都破前缀缓存（cc-haha 写记忆那轮就会破前缀）。

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
                                        skills catalog
```

> 注：相关记忆**不再放进 system prompt**（旧设计曾置于动态后缀），改为 append-only 注入消息历史，详见「五、文件记忆系统 → 记忆注入：append-only」。

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

上下文压缩（time-based mic / LLM snip / LLM 全量摘要）和前缀缓存命中是互斥的——缓存本质是按前缀哈希索引的 KV，是只读的，改写历史消息意味着前缀字节变化，已缓存内容全部失效，没有任何 provider 提供"原地修改缓存"的 API。本项目的应对：**time-based mic 专挑缓存已失效的时机（空闲 >5min，Qwen 缓存 TTL 过期）执行**——此时前缀本来就要重写，清理工具结果不额外付费；**LLM snip 攒够 20 条才检查一次**，把多次小压缩批量成一次前缀重建，避免零敲碎打反复付重建费。

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

Anthropic 的 **context editing**（`clear_tool_uses` 策略）是服务端版的 micro-compact：API 自动按时间顺序清除最旧的工具结果，替换成占位符——和本项目 `graph/compressor.py` 的 time-based mic 逻辑等价，只是搬到了服务端。官方文档也明确说：清除 = 缓存失效，所以提供了 `clear_at_least` 参数，让你攒够一批再清，别零敲碎打地反复付重建费——本项目的「攒够 20 条才 snip」是同一思路的客户端实现。

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
│  │  - Time-based mic（空闲>5min 清旧工具结果）           │        │
│  │  - LLM snip（每+20条问 LLM 哪段冗余，局部折叠）       │        │
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
│  │  - 大结果（>10000字符）截断 / 持久化到磁盘            │        │
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
│     deny ──→ agent   │  LLM 全量摘要：全部历史       │           │
│                      │  折叠为 1 条结构化摘要        │           │
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
├── __main__.py          # CLI 入口，argparse，REPL / 一次性模式（保留在根）
│
├── core/                # 基础层（被所有人依赖）
│   ├── state.py         # AgentState TypedDict 定义
│   └── tools.py         # 10 工具定义 + 权限检查 + deferred 机制 + execute_tool()
│
├── graph/               # Agent 运行时（LangGraph）
│   ├── agent.py         # StateGraph 定义，节点逻辑，路由函数
│   ├── compressor.py    # 上下文压缩：time-based mic → LLM snip → LLM 全量摘要
│   ├── prompt.py        # system prompt 构建，记忆 / todo / skill 注入
│   └── subagent.py      # SubagentConfig 配置模型 + 轻量 for-loop 执行引擎
│
├── features/            # 子系统
│   ├── memory.py        # 文件记忆读写，MEMORY.md 索引，sideQuery 检索
│   ├── skills.py        # SKILL.md 加载，/cmd 解析，prompt 注入
│   ├── sandbox.py       # OpenSandbox 会话级单例，文件上传同步，降级策略
│   └── session.py       # 会话 JSON 索引，LangGraph checkpointer 封装
│
├── interfaces/          # 接口层
│   ├── ui.py            # rich 终端渲染，spinner，diff 高亮，工具调用展示
│   └── server.py        # FastAPI + SSE 服务，astream_events → 前端事件流
│
└── agents/              # 子 Agent 定义（.md 文件，内置 explore/plan/general）
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

### 全栈模式（Python 后端 + 前端）

使用预置 Qwen 后端的启动脚本（需修改 run.sh / run.ps1 中的 API 地址、密钥和模型名）。
**首次运行前需先安装前端依赖：**

```bash
cd frontend && bun install && cd ..   # 首次运行前执行一次，生成 node_modules
./run.sh                              # Linux/macOS
# Windows: .\run.ps1
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

- 2026-06-12：上下文压缩重构对齐 cc-haha — Snip 改为 **LLM 自主判断**冗余区间并局部折叠（每 20 条消息检查一次，只压 `[start,end]` 区间为 1 条摘要，其余原封不动），不再按 token 阈值机械裁剪；Micro-compact 改为 time-based（空闲 >5min = Qwen 计费缓存 TTL 过期触发，只改本地副本）；LLM 全量摘要对齐 `compactConversation` 9 段式结构，折叠全部历史为 1 条
- 2026-06-11：新增 AutoDream 定期记忆整合 — 双门槛（≥24h + ≥5会话）触发，会话结束时自动合并重复、删除过时记忆；新增 auto_save_memory fire-and-forget，每轮最终回复后后台分析是否需要写入长期记忆
- 2026-06-11：Skills 注入改为主 Agent 显式传入 — `agent` 工具新增 `skills` 参数，子 Agent 启动时自动合并 skill 工具集，`.md` 文件移除静态 `skills` 字段
- 2026-06-11：子 Agent 系统重构 — 引入 `SubagentConfig` 数据类对齐 cc-haha/Deer Flow，所有 Agent 统一为 `agents/*.md` 文件定义（零硬编码），新增 `disallowed-tools` / `permission-mode` / `timeout-seconds` 字段，三层加载优先级（包内置 → 用户 → 项目）
- 2026-06-11：优化Prompt Caching逻辑，完全对齐Claude Code官方实现，支持对话历史增量缓存，缓存命中率提升40%+
- 2026-06-11：修复会话恢复功能，新增`as_node="agent"`参数解决状态更新异常问题
- 2026-06-11：新增便捷启动脚本`run.sh`，预置Qwen后端配置，开箱即用
- 2026-06-10：优化sideQuery逻辑，新增recentTools过滤、max_tokens约束、精确JSON正则匹配
