# CodeWhale 记忆系统深度分析

> 本文档完整分析 CodeWhale 的五层记忆架构，包括各记忆层级的职责、触发时机、注入位置，以及核心设计哲学。

---

## 📋 记忆系统总览

CodeWhale 采用 **五层独立记忆架构**，每一层职责明确，互不混淆：

| 层级 | 模块 | 持久化 | 作用范围 | 更新方式 | 核心用途 |
|------|------|--------|---------|---------|---------|
| 1. 用户记忆 | `memory.rs` | ✅ 全局 | 跨所有会话 | **完全手动** | 个人偏好、永久规则 |
| 2. 锚点 | `anchor.rs` | ✅ 项目级 | 项目生命周期 | **完全手动** | 关键事实、项目约束 |
| 3. 工作集 | `working_set.rs` | ❌ 会话级 | 当前会话 | 自动追踪 | 活跃路径、上下文聚焦 |
| 4. 对话摘要 | `compaction.rs` | ⚠️ 半持久化 | 压缩周期 | LLM自动生成 | 历史消息摘要 |
| 5. 规范状态 | `capacity_memory.rs` | ✅ 会话级 | 干预触发时 | **自动生成** | 关键时刻快照 |

---

## 🎯 核心设计原则

> **记忆的可靠性 > 记忆的便利性**

CodeWhale 的记忆系统故意设计成 **半自动**，而不是像其他 AI 助手那样全自动提取。

| 记忆类型 | 是否自动提取 | 原因 |
|---------|------------|------|
| 用户记忆 | ❌ 完全手动 | 自动提取分不清"这次"和"永远" |
| 锚点 | ❌ 完全手动 | 锚点是最高优先级事实，必须用户确认 |
| 工作集 | ✅ 自动追踪 | 路径提取是确定性规则，没有歧义 |
| 对话摘要 | ✅ 自动生成 | 即使摘要错了，固定消息还在 |
| 规范状态 | ✅ 自动生成 | 从工具输出等客观事实提取 |

---

## 📚 第一层：用户记忆 (User Memory)

**文件**: `crates/tui/src/memory.rs`

### 功能
全局跨会话的个人记忆，存储在 `~/.codewhale/memory.md`（可配置路径）。

### 典型内容
```markdown
- (2024-06-13 10:30 UTC) 我偏好使用 Python 而不是 Bash/JS
- (2024-06-13 11:00 UTC) 不要用驼峰命名，统一用 snake_case
- (2024-06-13 14:00 UTC) 我用 neovim，不要推荐 VSCode 插件
- (2024-06-13 15:00 UTC) 测试用 pytest，不要用 unittest
```

### 提取时机
✅ **会话启动时读取一次**，之后不会重新读取磁盘。

### 注入时机
✅ **会话启动后立即注入一次**，之后整个会话期间不再变动。

### 注入位置
```xml
<system_prompt>
  <base_identity>你是 DeepSeek Coder...</base_identity>
  
  <!-- ✅ 用户记忆在这里：仅次于基础身份，优先级最高 -->
  <user_memory source="/home/user/.codewhale/memory.md">
  - (2024-06-13 10:30 UTC) 我偏好使用 Python...
  </user_memory>
  
  <tool_definitions>...</tool_definitions>
  ...
</system_prompt>
```

### 添加方式
```bash
# 快速添加（在输入框直接打）
# 不要修改 .ssh 目录

# 或者用命令
/memory
/memory edit
```

### 技术细节
- 大小限制：100KB，超出时截断并标记
- 默认关闭：需要 `config.toml` 中 `[memory] enabled = true` 才会启用
- 注入格式：XML 包裹，清晰标记来源

---

## ⚓ 第二层：锚点 (Anchors)

**文件**: `crates/tui/src/commands/anchor.rs`

### 功能
项目级被动记忆，存储在项目目录 `.codewhale/anchors.md`（或 `.deepseek/anchors.md` 兼容旧版）。

### 典型内容
```
这个项目的 status 字段不可靠，要检查 error_code
---
.ssh/ 目录永远不要修改
---
提交信息格式：类型(范围): 描述
```

### 提取时机
✅ **每次上下文压缩时重新读取一次**。

⚠️ 关键设计：**不是每回合读取，只在压缩时读取**。

### 注入时机
✅ **仅在上下文压缩完成后注入**。

### 注入位置
锚点是压缩摘要块的 **第一部分**，永远在对话摘要之前：

```xml
<compaction_summary_block>
  <!-- ✅ 锚点注入在这里：压缩摘要块的最顶端 -->
  ## Pinned Facts (User Anchors)
  - 这个项目的 status 字段不可靠
  - .ssh 目录永远不要修改
  
  ---
  
  ## 📋 Conversation Summary (Auto-Generated)
  ...
  
  ---
  
  ## 🔍 Workflow Context
  ...
</compaction_summary_block>
```

### 管理命令
```bash
/anchor 这个项目的 status 字段不可靠    # 添加锚点
/anchor list                               # 列出所有锚点
/anchor remove 1                           # 删除第 1 个锚点
```

### 设计意图
锚点的目标是"压缩后绝对不会丢失的事实"。如果每回合都注入，会增加固定 token 开销，反而更频繁触发压缩。只在压缩后注入，既保证事实不丢失，又只在真正需要时才占用预算。

---

## 🎯 第三层：工作集 (Working Set)

**文件**: `crates/tui/src/working_set.rs`

### 功能
实时追踪会话中提到的文件路径，决定压缩时哪些消息应该被固定保留。

### 追踪来源
```
┌─────────────────┐
│  用户消息文本    │  →  提取路径
└─────────────────┘
┌─────────────────┐
│  工具调用输入    │  →  提取路径
└─────────────────┘
┌─────────────────┐
│  工具输出内容    │  →  提取路径
└─────────────────┘
```

### 数据结构
```rust
pub struct WorkingSetEntry {
    path: String,           // 工作区相对路径
    is_dir: bool,           // 是否是目录
    exists: bool,           // 磁盘上是否存在
    touches: u32,           // 被引用的次数
    last_turn: u64,         // 最后一次出现的回合
    last_source: Source,    // 来源：用户/工具输入/工具输出
}
```

### 注入时机
✅ **每回合动态更新**。

### 注入位置
系统提示的 **最末端**，就在消息历史开始之前：

```markdown
## Repo Working Set
Workspace: /home/user/project/codewhale

Active paths (prioritize these):
- crates/tui/src/memory.rs (file)
- crates/tui/src/commands/anchor.rs (file)
- crates/tui/src/core/engine/ (dir)

<!-- 消息历史从这里开始 -->
```

### 缓存优化关键点
`summary_block()` 的输出在没有新路径时是 **字节级完全稳定** 的，不会破坏前缀缓存。

---

## 📝 第四层：对话摘要 (Compaction Summary)

**文件**: `crates/tui/src/compaction.rs`

### 功能
上下文压力过大时，用 LLM 摘要未固定的历史消息。

### 提取时机
✅ 超过压缩阈值时自动触发。

### 注入时机
✅ 压缩完成后注入，替换掉被摘要的消息。

### 注入格式
```markdown
## 📋 Conversation Summary (Auto-Generated)
用户要求实现 OAuth2 登录功能。我首先检查了项目结构，发现使用 Axum 框架。
然后添加了 oauth2 crate 依赖，实现了 Google 和 GitHub 的认证路由。

遇到了几个问题：
1. 回调 URL 不匹配，已在 Google Cloud Console 修复
2. 状态验证缺少 CSRF 保护，已添加
3. 测试覆盖率只有 62%，需要补充

## 🔍 Workflow Context
Files Modified/Read:
- crates/tui/src/auth/oauth.rs
- Cargo.toml

Tools Used: read_file, edit_file, run_tests, exec_shell
```

### 可靠性说明
摘要是 LLM 生成的，可能有信息丢失或偏差，不是 100% 可靠的事实来源。关键事实应该用锚点固定。

---

## 🏛️ 第五层：规范状态快照 (Canonical State)

**文件**: `crates/tui/src/core/capacity_memory.rs` + `capacity_flow.rs`

这是 CodeWhale 记忆系统最独特、最强大的设计。

### 功能
关键时刻的项目状态检查点，硬重置或会话恢复时的"进度存档"。

### 触发时机
每次容量控制器干预时 **自动静默生成**：
- `TargetedContextRefresh` (上下文刷新)
- `VerifyWithToolReplay` (工具重放验证)
- `VerifyAndReplan` (硬重置并重新规划)

### 数据结构
```rust
pub struct CanonicalState {
    goal: String,              // 当前目标（最近用户请求摘要，≤220字）
    constraints: Vec<String>,  // 约束（模型、工作区、附加说明）
    confirmed_facts: Vec<String>,  // 已验证事实（最近4个非错误工具输出）
    open_loops: Vec<String>,   // 未解决问题（失败的工具调用）
    pending_actions: Vec<String>,  // 下一步计划
    critical_refs: Vec<String>,    // 关键引用（路径 + 工具ID）
}
```

### 完整记录
```rust
pub struct CapacityMemoryRecord {
    id: String,
    ts: String,               // RFC3339 时间戳
    turn_index: u64,          // 回合序号
    action_trigger: String,   // 触发的干预类型
    h_hat: f64, c_hat: f64, slack: f64,  // 容量指标
    risk_band: String,        // 风险等级: low/medium/high/severe
    canonical_state: CanonicalState,
    source_message_ids: Vec<String>,  // 来源消息 ID
    replay_info: Option<ReplayInfo>,  // 重放验证结果
}
```

### 提取时机
两个精确时机：
1. **干预发生时**：构建并写入 JSONL
2. **会话恢复时**：读取最近 1 条记录

### 注入时机
1. ✅ **容量干预发生后立即注入**（当前会话）
2. ✅ **下次恢复会话时注入**（跨会话）

### 注入位置
压缩摘要块的 **最后部分**：

```xml
<compaction_summary_block>
  ## Pinned Facts (User Anchors)
  ...
  
  ## 📋 Conversation Summary
  ...
  
  ## 🔍 Workflow Context
  ...
  
  <!-- ✅ 规范状态注入在这里 -->
  ## Capacity Canonical State [verify_and_replan]
  Goal: 修复 OAuth 回调 URL 不匹配问题
  Constraints: model=deepseek-v4-pro, workspace=/home/user/project
  Confirmed Facts:
  - 回调 URL 已在 Google Cloud Console 更新
  - CSRF 保护已添加
  Open Loops:
  - run_tests: 2 个测试失败
  Pending Actions:
  - 重新评估失败的测试用例
  - 从规范事实重新推导计划
  Critical Refs:
  - crates/tui/src/auth/oauth.rs
  Memory Pointer: memory://session_abc123/cap_xyz789
</compaction_summary_block>
```

### 持久化格式
- 存储格式：**JSONL**（每行一条记录，只追加不修改）
- 位置：`~/.codewhale/memory/{session_id}.jsonl`
- 每个会话一个独立文件

---

## 🔄 完整时序：记忆的生命周期

```
会话启动
    ↓
[1] ✅ 用户记忆提取 → 立即注入系统提示最前端
    ↓
回合 1 开始
    ↓
    用户输入
    ↓
    模型推理
    ↓
    工具调用/执行
    ↓
    工作集自动更新
    ↓
    上下文预算检查
    ↓
[2] ❌ 未触发压缩 → 锚点不注入
    ↓
回合 2 开始
    ↓
    ...重复多轮...
    ↓
[3] ✅ 触发压缩阈值
    ↓
    ├─→ 重新读取 anchors.md → 提取锚点
    ├─→ LLM 生成对话摘要
    ├─→ 生成工作流上下文
    └─→ 三者打包成压缩摘要块，注入系统提示
    ↓
    压缩完成，继续对话
    ↓
[4] ✅ 触发容量干预
    ↓
    ├─→ 扫描消息历史，构建规范状态快照
    ├─→ 持久化到 JSONL 文件
    └─→ 注入规范状态块到系统提示摘要末尾
    ↓
[5] 会话关闭
    ↓
[6] 下次恢复会话
    ↓
    ├─→ 提取用户记忆（同步骤 1）
    ├─→ 读取最近 1 条规范状态快照
    └─→ 两者都注入系统提示
```

---

## 📍 注入位置总览（精确相对顺序）

```
系统提示最顶端
    ↓
1. 基础身份提示 (永远不变)
    ↓
2. 用户记忆 (User Memory)  ✅ 会话启动时注入一次
    ↓
3. 工具定义
    ↓
4. 压缩摘要块 (Compaction Summary)
    │
    ├─→ 4a. 锚点 (Anchors)  ✅ 每次压缩后重新读取注入
    ├─→ 4b. 对话摘要
    ├─→ 4c. 工作流上下文
    └─→ 4d. 规范状态快照  ✅ 干预/恢复时注入
    ↓
5. 工作集 (Working Set)  ✅ 每回合动态更新（不是长期记忆）
    ↓
消息历史开始
```

---

## 💡 设计哲学深入理解

### 为什么用户记忆和锚点必须是手动的？

如果让 LLM 自动提取偏好：
```
用户说："这次用 Python 写吧"
🤖 自动提取 → "用户永远只用 Python"
❌ 错了，用户只是说这次用

用户说："不要用驼峰命名，这个项目我们用下划线"
🤖 自动提取 → "所有项目都不要用驼峰"
❌ 错了，用户说的是这个项目
```

自动提取的根本问题是：**LLM 分不清"这次"和"永远"、"这个项目"和"所有项目"**。

记忆系统最大的问题不是"记不住"，而是"记错了"：
- 记不住 → 用户再说一遍就行，成本低
- 记错了 → 助手持续做出错误假设，用户根本不知道为什么，成本极高

### 为什么规范状态可以自动？

因为规范状态的信息来源是：
- 最近的用户文本消息（客观事实，不是推测）
- 最近的工具输出（客观执行结果，不是推测）

这些是客观存在的事实，不是从用户语气里推断出来的偏好。

---

## 🆚 与其他 AI 助手的对比

| 助手 | 记忆方式 | 主要问题 |
|------|---------|---------|
| **Cursor** | 自动提取偏好 | 经常记错，用户不知道它"以为"自己记住了什么 |
| **Claude Projects** | 自动提取项目上下文 | 注入的内容不透明，污染上下文 |
| **Github Copilot** | 本地 + 云端隐式记忆 | 黑盒，完全不知道模型看到了什么 |
| **CodeWhale** | 完全显式 + 半自动分层 | ✅ 记忆文件就在磁盘上，可以随时编辑、查看、删除 |

---

## 📁 相关源码文件

| 功能 | 文件路径 |
|------|---------|
| 用户记忆 | `crates/tui/src/memory.rs` |
| 锚点命令 | `crates/tui/src/commands/anchor.rs` |
| 工作集 | `crates/tui/src/working_set.rs` |
| 上下文压缩 | `crates/tui/src/compaction.rs` |
| 规范状态 | `crates/tui/src/core/capacity_memory.rs` |
| 容量干预流程 | `crates/tui/src/core/engine/capacity_flow.rs` |

---

## 🎯 总结：各记忆层的正确使用场景

| 记忆类型 | 应该放什么 | 不要放什么 |
|---------|-----------|-----------|
| **用户记忆** | 跨所有项目的个人偏好、工具选择、习惯 | 某个项目特有的约定、当前任务的进度 |
| **锚点** | 当前项目的 API 陷阱、约定、规则 | 个人偏好、临时任务状态 |
| **工作集** | 当前正在编辑的文件 | 任何手动添加的东西 |
| **对话摘要** | 对话历史的梗概 | 绝对不能丢的关键事实 |
| **规范状态** | 当前任务的进度快照、已验证事实 | 长期偏好、个人习惯 |

---

*文档生成时间：2024-06-13*
*基于 CodeWhale v0.8.11 代码分析*
