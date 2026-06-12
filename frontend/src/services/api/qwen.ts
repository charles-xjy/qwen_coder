/**
 * qwen.ts - Python 后端 SSE 适配器
 *
 * 替换 claude.ts 的 queryModelWithStreaming，将 Python server（localhost:8010）
 * 的 SSE 事件流翻译成 cc-haha UI 期望的 Anthropic 格式 StreamEvent。
 *
 * Python SSE 事件 → Anthropic StreamEvent 映射：
 *   token       → content_block_start(text) + content_block_delta(text_delta)
 *   tool_start  → content_block_stop(前一块) + content_block_start(tool_use)
 *   tool_end    → content_block_stop
 *   interrupt   → 自动回复 yes（权限）/ compress（压缩）
 *   done        → message_delta + message_stop + AssistantMessage(纯文本)
 *   error       → SystemAPIErrorMessage
 */

import { randomUUID } from 'crypto'
import type {
  AssistantMessage,
  Message,
  StreamEvent,
  SystemAPIErrorMessage,
} from '../../types/message.js'
import type { SystemPrompt } from '../../utils/systemPromptType.js'
import type { Tools } from '../../Tool.js'

// ── 常量 ──────────────────────────────────────────────────────────────────────

const PYTHON_SERVER = 'http://localhost:8010'

// ── 会话管理（单例，整个 CLI 生命周期内复用同一个 session）──────────────────

let _sessionId: string | null = null

async function getOrCreateSession(): Promise<string> {
  if (_sessionId) return _sessionId
  const res = await fetch(`${PYTHON_SERVER}/sessions/new`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ permission_mode: 'bypassPermissions' }),
  })
  if (!res.ok) throw new Error(`创建会话失败: ${res.status}`)
  const data = (await res.json()) as { session_id: string }
  _sessionId = data.session_id
  return _sessionId
}

async function sendInterruptAnswer(sessionId: string, answer: string): Promise<void> {
  await fetch(`${PYTHON_SERVER}/interrupt`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId, answer }),
  }).catch(() => {})
}

// ── ThinkingConfig 类型（局部定义，避免从 claude.ts 导入）──────────────────

type ThinkingConfig =
  | { type: 'enabled'; budgetTokens?: number }
  | { type: 'disabled' }
  | { type: 'adaptive' }

type Options = {
  model: string
  [key: string]: unknown
}

// ── SSE 解析 ──────────────────────────────────────────────────────────────────

interface PythonEvent {
  type: 'token' | 'tool_start' | 'tool_end' | 'interrupt' | 'done' | 'error'
  text?: string
  name?: string
  args?: Record<string, unknown>
  result?: string
  diff?: { path: string; old: string; new: string } | null
  message?: string
  kind?: 'permission' | 'compress'
  input_tokens?: number
  output_tokens?: number
}

async function* parsePythonSSE(
  response: Response,
): AsyncGenerator<PythonEvent> {
  const reader = response.body!.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })

      const lines = buffer.split('\n')
      buffer = lines.pop() ?? ''

      for (const line of lines) {
        if (line.startsWith('data: ')) {
          const raw = line.slice(6).trim()
          if (!raw) continue
          try {
            yield JSON.parse(raw) as PythonEvent
          } catch {
            // 忽略解析错误
          }
        }
      }
    }
  } finally {
    reader.releaseLock()
  }
}

// ── StreamEvent 构造帮助函数 ──────────────────────────────────────────────────

function makeStreamEvent(event: unknown): StreamEvent {
  return { type: 'stream_event', event } as StreamEvent
}

function makeMessageStart(messageId: string, model: string): StreamEvent {
  return makeStreamEvent({
    type: 'message_start',
    message: {
      id: messageId,
      type: 'message',
      role: 'assistant',
      model,
      content: [],
      stop_reason: null,
      stop_sequence: null,
      usage: { input_tokens: 0, output_tokens: 0, cache_creation_input_tokens: 0, cache_read_input_tokens: 0 },
    },
  })
}

function makeContentBlockStart(index: number, block: unknown): StreamEvent {
  return makeStreamEvent({ type: 'content_block_start', index, content_block: block })
}

function makeContentBlockDelta(index: number, delta: unknown): StreamEvent {
  return makeStreamEvent({ type: 'content_block_delta', index, delta })
}

function makeContentBlockStop(index: number): StreamEvent {
  return makeStreamEvent({ type: 'content_block_stop', index })
}

function makeMessageDelta(stopReason: string, outputTokens: number): StreamEvent {
  return makeStreamEvent({
    type: 'message_delta',
    delta: { stop_reason: stopReason, stop_sequence: null },
    usage: { output_tokens: outputTokens },
  })
}

function makeMessageStop(): StreamEvent {
  return makeStreamEvent({ type: 'message_stop' })
}

function makeErrorMessage(content: string): SystemAPIErrorMessage {
  return {
    type: 'system',
    subtype: 'api_error',
    content,
    level: 'error',
    uuid: randomUUID(),
    timestamp: new Date().toISOString(),
  } as SystemAPIErrorMessage
}

// ── 从 Message[] 提取最后一条用户消息文本 ────────────────────────────────────

function extractLastUserText(messages: Message[]): string {
  for (let i = messages.length - 1; i >= 0; i--) {
    const msg = messages[i]
    if (msg.type === 'user') {
      const content = msg.message?.content
      if (typeof content === 'string') return content
      if (Array.isArray(content)) {
        return content
          .filter((b: { type: string; text?: string }) => b.type === 'text')
          .map((b: { type: string; text?: string }) => b.text ?? '')
          .join('')
      }
    }
  }
  return ''
}

// ── 核心：queryModelWithStreaming ─────────────────────────────────────────────

export async function* queryModelWithStreaming({
  messages,
  signal,
  options,
}: {
  messages: Message[]
  systemPrompt: SystemPrompt
  thinkingConfig: ThinkingConfig
  tools: Tools
  signal: AbortSignal
  options: Options
}): AsyncGenerator<StreamEvent | AssistantMessage | SystemAPIErrorMessage, void> {
  const userText = extractLastUserText(messages)
  if (!userText) return

  let sessionId: string
  try {
    sessionId = await getOrCreateSession()
  } catch (err) {
    yield makeErrorMessage(`无法连接 Python 后端 (${PYTHON_SERVER}): ${err}`)
    return
  }

  let response: Response
  try {
    response = await fetch(`${PYTHON_SERVER}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: userText, session_id: sessionId }),
      signal,
    })
  } catch (err) {
    if ((err as Error)?.name === 'AbortError') return
    yield makeErrorMessage(`请求失败: ${err}`)
    return
  }

  if (!response.ok) {
    yield makeErrorMessage(`服务器错误: ${response.status}`)
    return
  }

  // ── 状态机变量 ────────────────────────────────────────────────────────────

  const messageId = randomUUID()
  const model = options.model ?? 'Qwen_agent'
  let blockIndex = 0
  let currentBlockType: 'none' | 'text' | 'tool_use' = 'none'
  let fullText = ''
  let inputTokens = 0
  let outputTokens = 0

  // 发送 message_start
  yield makeMessageStart(messageId, model)

  // ── 消费 SSE 流 ───────────────────────────────────────────────────────────

  for await (const ev of parsePythonSSE(response)) {
    if (signal.aborted) break

    switch (ev.type) {
      case 'token': {
        const text = ev.text ?? ''
        if (!text) break

        if (currentBlockType !== 'text') {
          // 关闭前一个 block（如果有）
          if (currentBlockType !== 'none') {
            yield makeContentBlockStop(blockIndex)
            blockIndex++
          }
          // 开启 text block
          yield makeContentBlockStart(blockIndex, { type: 'text', text: '' })
          currentBlockType = 'text'
        }

        yield makeContentBlockDelta(blockIndex, { type: 'text_delta', text })
        fullText += text
        break
      }

      case 'tool_start': {
        const toolName = ev.name ?? 'unknown_tool'
        const toolArgs = ev.args ?? {}
        const toolId = `toolu_${randomUUID().replace(/-/g, '').slice(0, 24)}`

        // 关闭前一个 block
        if (currentBlockType !== 'none') {
          yield makeContentBlockStop(blockIndex)
          blockIndex++
        }

        // 开启 tool_use block（含完整 input，避免 cc-haha 解析 partial JSON）
        yield makeContentBlockStart(blockIndex, {
          type: 'tool_use',
          id: toolId,
          name: toolName,
          input: toolArgs,
        })
        // 发一个空的 input_json_delta（保持协议完整）
        yield makeContentBlockDelta(blockIndex, {
          type: 'input_json_delta',
          partial_json: '',
        })
        currentBlockType = 'tool_use'
        break
      }

      case 'tool_end': {
        if (currentBlockType === 'tool_use') {
          yield makeContentBlockStop(blockIndex)
          blockIndex++
          currentBlockType = 'none'
        }
        break
      }

      case 'interrupt': {
        // 权限确认：自动回复
        const kind = ev.kind ?? 'permission'
        const answer = kind === 'compress' ? 'compress' : 'yes'
        await sendInterruptAnswer(sessionId, answer)
        break
      }

      case 'done': {
        inputTokens = ev.input_tokens ?? 0
        outputTokens = ev.output_tokens ?? 0
        break
      }

      case 'error': {
        yield makeErrorMessage(ev.message ?? '未知错误')
        return
      }
    }
  }

  // ── 关闭最后一个 block ────────────────────────────────────────────────────

  if (currentBlockType !== 'none') {
    yield makeContentBlockStop(blockIndex)
  }

  // ── 结束帧 ────────────────────────────────────────────────────────────────

  yield makeMessageDelta('end_turn', outputTokens)
  yield makeMessageStop()

  // ── 最终 AssistantMessage（纯文本，不含 tool_use，避免触发 cc-haha 工具循环）

  const assistantMessage: AssistantMessage = {
    type: 'assistant',
    uuid: randomUUID(),
    requestId: undefined,
    timestamp: new Date().toISOString(),
    message: {
      id: messageId,
      type: 'message',
      role: 'assistant',
      model,
      content: fullText ? [{ type: 'text', text: fullText }] : [],
      stop_reason: 'end_turn',
      stop_sequence: null,
      usage: {
        input_tokens: inputTokens,
        output_tokens: outputTokens,
        cache_creation_input_tokens: 0,
        cache_read_input_tokens: 0,
      },
    },
  } as AssistantMessage

  yield assistantMessage
}

// ── queryModelWithoutStreaming（供 generateAgent 等非流式调用使用）───────────

export async function queryModelWithoutStreaming({
  messages,
  signal,
  options,
}: {
  messages: Message[]
  systemPrompt: SystemPrompt
  thinkingConfig: ThinkingConfig
  tools: Tools
  signal: AbortSignal
  options: Options
}): Promise<AssistantMessage> {
  let lastAssistant: AssistantMessage | null = null

  for await (const msg of queryModelWithStreaming({
    messages,
    systemPrompt: [] as SystemPrompt,
    thinkingConfig: { type: 'disabled' },
    tools: {} as Tools,
    signal,
    options,
  })) {
    if (msg.type === 'assistant') lastAssistant = msg
  }

  if (!lastAssistant) {
    return {
      type: 'assistant',
      uuid: randomUUID(),
      requestId: undefined,
      timestamp: new Date().toISOString(),
      message: {
        id: randomUUID(),
        type: 'message',
        role: 'assistant',
        model: options.model ?? 'Qwen_agent',
        content: [],
        stop_reason: 'end_turn',
        stop_sequence: null,
        usage: { input_tokens: 0, output_tokens: 0, cache_creation_input_tokens: 0, cache_read_input_tokens: 0 },
      },
    } as AssistantMessage
  }

  return lastAssistant
}
