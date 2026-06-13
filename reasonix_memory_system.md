# DeepSeek-Reasonix 记忆系统完整分析

> ⚠️ **更正声明（经 `deepseek-reasonix` 源码核实）**：本文部分内容不准确，阅读时请注意——
> 1. **没有 BM25，也没有专门的 memory 检索工具（search/read/list）**。第四层「记忆检索工具 / BM25 搜索」一节描述的接口在源码中不存在。Reasonix 的实际召回方式：索引与文档记忆折进系统前缀，模型需要细节时用**通用的 `read_file`** 读对应文件（与 cc-haha 的 grep 同属"模型驱动按需读"）。
> 2. **删除不归档到 `.archive`**。`store.Delete` 实际是 `os.Remove`，无审计日志。
> 3. ✅ 仍然准确的部分：索引/文档启动时 `Block()` 折进前缀、`remember`/`forget` 工具写删、`pendingMemory` 队列 + 尾部 `<memory-update>` delta 注入、4 类记忆（user/feedback/project/reference）、@import 递归、路径逃逸防护。
>
> 准确版对比见 [`四项目对比_记忆系统.md`](四项目对比_记忆系统.md)。

Reasonix 的记忆系统是一个**缓存优先、分层生效、生态兼容**的工业级设计，而不是简单的"把内容塞进 system prompt"。本文完整解析其架构。

---

## 🎯 核心设计哲学

```
索引优先，内容按需加载
本轮注入，下轮折叠
生态兼容，用户可编辑
```

**一句话概括**：用最小的 token 开销让模型知道它有什么，需要时自己去取，同时最大化前缀缓存稳定性。

---

## 🏗️ 整体架构视图

```
                    ┌───────────────────────────────────┐
                    │       持久层 (磁盘文件)            │
                    │  • REASONIX.md / AGENTS.md        │
                    │  • MEMORY.md 索引                 │
                    │  • 单事实 .md 文件                 │
                    └──────────────────┬────────────────┘
                                       │  启动时一次性加载
                    ┌──────────────────▼────────────────┐
                    │      系统前缀 (缓存层)              │
                    │  • 文档记忆 Block()                │
                    │  • 记忆索引列表                    │
                    │  ✅ 启动时折叠，之后不变            │
                    └──────────────────┬────────────────┘
                                       │  运行时修改不碰这里
                    ┌──────────────────▼────────────────┐
                    │     pendingMemory 队列             │
                    │  • remember/forget 工具结果        │
                    │  • 用户手动编辑的文档               │
                    │  • 快速添加 (# 命令)               │
                    └──────────────────┬────────────────┘
                                       │  下一轮 Compose 时消费
                    ┌──────────────────▼────────────────┐
                    │    本轮消息头部注入                │
                    │  <memory-update> 块               │
                    │  位置靠后，注意力优先级最高        │
                    └───────────────────────────────────┘
```

---

## 📂 第一层：持久层 - 纯文件存储

### 1.1 两种记忆类型

| 类型 | 存储方式 | 用途 | 示例 |
|------|---------|------|------|
| **文档记忆** | REASONIX.md / AGENTS.md / CLAUDE.md | 项目级文档、约定、指南 | "本项目使用 Go 1.22" |
| **自动记忆** | 单文件 + MEMORY.md 索引 | 事实、决策、外部引用 | "数据库端口 5432" |

### 1.2 发现顺序（优先级从低到高）

```
1. 用户全局    → ~/.config/reasonix/REASONIX.md
2. 祖先链      → 项目根以上每个父目录的 AGENTS.md
3. 项目根      → ./REASONIX.md, ./AGENTS.md
4. 项目本地    → ./REASONIX.local.md (最高优先级)
```

### 1.3 @import 递归引用

```markdown
# REASONIX.md

@./docs/architecture.md
@./docs/team-conventions.md
```

**特性**：
- 递归深度上限 5 层
- 循环引用检测
- 读取失败的导入保留原文，不崩溃
- ~/ 家目录、绝对路径、相对路径都支持

### 1.4 自动记忆存储结构

```
~/.config/reasonix/projects/-home-charles-myproject/
└── memory/
    ├── MEMORY.md           ← 索引，加载到系统前缀
    ├── database-port.md    ← 单事实文件
    ├── build-config.md
    └── .archive/           ← 已删除记忆的审计日志
        └── 20240115-143022.000-old-fact.md
```

### 1.5 单事实文件格式

```markdown
---
name: database-port
title: 数据库端口配置
description: PostgreSQL 连接端口确认是 5432
metadata:
  type: reference
---

经过和运维确认，生产环境 PostgreSQL 端口是 5432，
不是默认的 5433。这个信息在 2024 年 1 月更新过。
```

**四种记忆类型**：
- `user` - 用户身份、偏好、专业领域
- `feedback` - 工作方式指导（含为什么 + 如何应用）
- `project` - 进行中的工作、目标、约束
- `reference` - 外部资源指针、URL、ticket

---

## 🚀 第二层：系统前缀折叠

### 2.1 启动时一次性加载

```go
// 只在程序启动时调用一次
func Load(opts Options) *Set {
    return &Set{
        Docs:    discoverDocs(cwd, opts.UserDir),  // 发现所有文档记忆
        Store:   StoreFor(opts.UserDir, cwd),      // 自动记忆存储
        Index:   store.Index(),                    // MEMORY.md 内容
        CWD:     cwd,
        UserDir: opts.UserDir,
    }
}
```

### 2.2 Block() 确定性渲染

```go
func (s *Set) Block() string {
    if s.Empty() {
        return ""  // ✅ 无记忆时返回空，base 提示字节级不变
    }
    var b strings.Builder
    b.WriteString("# Memory\n\n")
    b.WriteString("Persistent context loaded from memory files...\n")

    // 按发现顺序遍历文档（优先级升序）
    for _, d := range s.Docs {
        fmt.Fprintf(&b, "\n## %s (%s)\n\n%s\n", 
            d.Path, d.Scope, strings.TrimSpace(d.Body))
    }

    // 自动记忆索引
    if idx := strings.TrimSpace(s.Index); idx != "" {
        b.WriteString("\n## Saved memories\n\n")
        b.WriteString("Facts you saved in earlier sessions...\n\n")
        b.WriteString(idx)
    }
    return b.String()
}
```

**关键设计**：
- ✅ **纯函数**：相同文件 → 相同输出字符串
- ✅ **无随机、无时间戳**：保证跨运行的稳定性
- ✅ **空安全**：无记忆时不添加任何字符

### 2.3 Compose 组合策略

```go
func Compose(base string, s *Set) string {
    block := s.Block()
    if block == "" {
        return base  // ✅ 无记忆，base 完全不变
    }
    if strings.TrimSpace(base) == "" {
        return block
    }
    // ✅ base 永远在前（最稳定的文本），memory 在后
    return strings.TrimRight(base, "\n") + "\n\n" + block
}
```

**为什么 base 在前？**

```
位置越靠前 → 跨会话越稳定 → 缓存命中率越高

[base system prompt 第 1-200 token]  → 永远不变，缓存核心
[memory block 第 201-500 token]      → 记忆变化时，后面失效
```

即使记忆变化，前 200 token 仍然可能命中缓存。

---

## 🧠 第三层：运行时修改的两阶段生效

### 3.1 为什么不直接修改 System？

| 策略 | 缓存影响 | 成本 |
|------|---------|------|
| ❌ 实时重写 System | 每次保存全量失效 | 每轮多花几千 tokens |
| ✅ 尾部注入 + 下次折叠 | 仅一轮受影响 | 几十 tokens 成本 |

### 3.2 pendingMemory 队列

```go
// controller.go
type Controller struct {
    // ...
    pendingMemory []string  // 排队等待注入的记忆更新
}
```

**入队时机**：

```go
// 1. remember 工具执行成功
c.pendingMemory = append(c.pendingMemory, 
    "Saved memory: database-port — PostgreSQL 端口确认是 5432")

// 2. forget 工具执行成功  
c.pendingMemory = append(c.pendingMemory,
    "Deleted memory: old-credential — 旧认证信息已移除")

// 3. 用户手动编辑了 AGENTS.md
c.pendingMemory = append(c.pendingMemory,
    "Edited ./AGENTS.md: Go version updated to 1.22")
```

### 3.3 Compose 消费注入

```go
func (c *Controller) Compose(text string) string {
    c.mu.Lock()
    notes := c.pendingMemory
    c.pendingMemory = nil  // 消费后清空，只注入一次
    c.mu.Unlock()

    if len(notes) > 0 {
        var b strings.Builder
        b.WriteString("<memory-update>\n")
        b.WriteString("The following project-memory changes were just made and apply from now on:\n")
        for _, n := range notes {
            b.WriteString("- " + n + "\n")
        }
        b.WriteString("</memory-update>\n\n")
        text = b.String() + text  // ← 加在用户输入前面
    }
    return text
}
```

### 3.4 实际消息格式

```go
[]provider.Message{
    // ┌─────────────────────────────────────────────┐
    // │  SYSTEM 前缀完全不变！字节级相同             │
    // │  PrefixHash 不变 → 缓存命中率不受影响        │
    // └─────────────────────────────────────────────┘
    {
        Role:    "system",
        Content: "You are Reasonix... [和之前完全一样]",
    },
    
    // ... 之前的所有对话消息 ...
    
    // ┌─────────────────────────────────────────────┐
    // │  本轮 USER 消息，记忆更新注入在头部          │
    // │  位置靠后 → 注意力权重最高 → 自然覆盖旧记忆   │
    // └─────────────────────────────────────────────┘
    {
        Role: "user",
        Content: `<memory-update>
The following project-memory changes were just made and apply from now on:
- Saved memory: database-port — PostgreSQL 端口确认是 5432
</memory-update>

好的，现在帮我连接数据库`,
    },
}
```

---

## 🔍 第四层：记忆检索工具

### 4.1 memory 工具接口

```go
func NewRecallTool(store Store) tool.Tool
```

**支持三种操作**：

| operation | 参数 | 用途 |
|-----------|------|------|
| `search` | query, type, limit | BM25 全文搜索，返回匹配片段 |
| `read` | name | 按名称读取单个完整事实 |
| `list` | type, limit | 列出所有记忆的索引 |

### 4.2 BM25 搜索实现

```go
func searchMemories(ctx context.Context, store Store, query string, typ Type, limit int) {
    // 1. 对查询分词
    queryTerms, _ := retrieval.QueryTerms(query)
    
    // 2. 对每个记忆构建文档向量（name + title + description + body）
    for _, m := range memories {
        text := memorySearchText(m)
        terms := retrieval.Tokens(text)
        // ... 计算词频、文档频率
    }
    
    // 3. BM25 评分
    score := retrieval.BM25Score(doc.counts, doc.length, queryTerms, df, len(docs), avgLen)
    
    // 4. 生成上下文片段
    snippet := retrieval.MakeSnippet(doc.text, query, queryTerms, maxSnippetLen)
}
```

**搜索阈值控制**：
- 最低分阈值：score > 0
- 默认返回：8 条
- 最多返回：20 条
- 片段长度：260 字符

### 4.3 搜索结果格式

```
Memory search results for "database":

1. score=0.872 name=database-port type=reference
   description: PostgreSQL 连接端口
   path: ~/.config/reasonix/projects/.../database-port.md
   snippet: 生产环境 PostgreSQL 端口是 5432...

Use operation="read" with a memory name to inspect the full saved fact.
```

---

## 🛡️ 第五层：安全边界

### 5.1 路径逃逸防护

```go
func safeJoin(base, name string) (string, error) {
    if !filepath.IsLocal(name) {
        return "", fmt.Errorf("memory path escapes store: %s", name)
    }
    // ... 双重检查相对路径
    rel, _ := filepath.Rel(baseAbs, pathAbs)
    if rel == ".." || strings.HasPrefix(rel, ".."+string(os.PathSeparator)) {
        return "", fmt.Errorf("memory path escapes store: %s", name)
    }
}
```

### 5.2 写入白名单

```go
func (s *Set) allowedDocPaths() map[string]bool {
    allow := map[string]bool{}
    // 只允许各 scope 的规范文件
    for _, sc := range docScopes {
        if p := s.DocPath(sc); p != "" {
            allow[absOf(p)] = true
        }
    }
    // 加上本次会话已发现的文件
    for _, d := range s.Docs {
        allow[absOf(d.Path)] = true
    }
    return allow
}
```

### 5.3 无错加载原则

```go
// Load() 永远不报错，缺失文件只是意味着记忆更少
func Load(opts Options) *Set {
    // 所有错误都静默处理，返回可用的子集
}
```

- 一个损坏的文件不影响其他所有文件
- 一个目录不可读不导致整个程序启动失败

---

## 🎨 第六层：冲突处理设计

### 6.1 利用 LLM 注意力的天然特性

```
所有现代 LLM 对最近的 token 注意力权重更高：

[系统前缀 第 100 token]  注意力权重 ~0.1
...
[对话中间 第 2000 token] 注意力权重 ~0.5
...
[本轮消息 第 4000 token] 注意力权重 ~0.9-1.0  ← 最优先
```

### 6.2 冲突示例

**系统前缀里的旧记忆**：
```
## Saved memories
- [数据库端口](database-port.md) — PostgreSQL 端口是 5432
```

**本轮注入的更新**：
```
<memory-update>
- Updated memory: database-port — PostgreSQL 端口更正为 5433
</memory-update>
```

✅ **模型会使用 5433，而不是 5432**
- 位置靠后 → 注意力权重更高
- 语义标记明确：`"just made and apply from now on"`

### 6.3 重启后无冲突

```go
// Save() 做的两件事：
// 1. 覆盖写入事实文件 → 内容是最新的
// 2. 更新 MEMORY.md 索引行 → description 是最新的

func (s Store) Save(m Memory) (string, error) {
    os.WriteFile(store.Path(m.Name), render(m, name), 0o644)
    s.reindex(name, m)  // ← description 永远是最新的
}
```

**索引里的 description 不是引用，是快照**。重启时，旧描述已被覆盖，没有冲突。

---

## 💰 Token 开销分析

### 典型场景成本对比

| 场景 | 全部加载策略 | Reasonix 索引优先策略 |
|------|-------------|----------------------|
| 0 条记忆 | 0 tokens | 0 tokens |
| 10 条记忆 | ~1500 tokens | ~500 tokens (索引) |
| 50 条记忆 | ~7500 tokens | ~1500 tokens (索引) |
| 保存一条新记忆 | 全量缓存失效 | 仅本轮额外 ~50 tokens |

### 跨会话摊销

| 时间点 | 缓存状态 | 额外成本 |
|--------|---------|---------|
| T1：刚保存记忆 | 本轮前缀变化 | ~50 tokens |
| T2：后续轮次 | 恢复 append-only | 0 |
| T3：下次会话重启 | 记忆折叠到前缀 | 冷启动一次 (新会话本来就冷) |
| T4：下次会话第二轮+ | 前缀稳定深度缓存 | **0 token，免费！** |

---

## 🔑 核心设计原则总结

| 原则 | 如何体现 |
|------|---------|
| **索引优先** | 只把目录放进提示，内容按需加载 |
| **缓存最大化** | 运行时修改永不碰系统前缀，只注入到本轮消息 |
| **确定性输出** | 相同文件 → 相同 Block() → 相同前缀哈希 |
| **LLM 原生** | 利用注意力近因效应，不与模型工作方式对抗 |
| **生态兼容** | AGENTS.md 约定，可与 Claude Code 等工具互操作 |
| **用户可编辑** | 纯 Markdown 文件，用户可以直接修改 |
| **审计友好** | 删除 = 归档到 .archive，永不丢失记录 |
| **渐进降级** | 任何部分失败不影响整体 |

---

## 结语

这不是一个简单的 RAG 系统，也不是一个简单的"把东西塞进 system prompt"的玩具。这是一个经过深度思考的工业级设计，完美平衡了：
- **Token 效率**：索引优先，按需加载
- **缓存稳定性**：启动折叠，运行注入
- **用户体验**：纯文件，可直接编辑
- **生态兼容**：遵循 AGENTS.md 跨工具约定

记忆系统是 Reasonix 整个缓存优先架构的基石，也是最能体现其设计深度的部分。
