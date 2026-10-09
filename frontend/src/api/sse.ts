import { ApiError, type RunSnapshot, type SessionProcess } from './http'

export type SseFrame = Readonly<{ event: string; id: string | null; data: string }>

export class SseParser {
  private readonly decoder = new TextDecoder('utf-8', { fatal: true })
  private buffer = ''
  private data: string[] = []
  private event = ''
  private id: string | null = null

  feed(chunk: Uint8Array): SseFrame[] {
    try { this.buffer += this.decoder.decode(chunk, { stream: true }) }
    catch { throw new ApiError(200, 'malformed_stream_frame', null) }
    return this.lines(false)
  }

  finish(): SseFrame[] {
    try { this.buffer += this.decoder.decode() }
    catch { throw new ApiError(200, 'malformed_stream_frame', null) }
    const frames = this.lines(true)
    if (this.buffer) {
      this.line(this.buffer, frames)
      this.buffer = ''
    }
    this.dispatch(frames)
    return frames
  }

  private lines(final: boolean): SseFrame[] {
    const frames: SseFrame[] = []
    for (;;) {
      const index = this.buffer.search(/[\r\n]/)
      if (index < 0) break
      const delimiter = this.buffer[index]
      if (delimiter === '\r' && index === this.buffer.length - 1 && !final) break
      const consume = delimiter === '\r' && this.buffer[index + 1] === '\n' ? 2 : 1
      const line = this.buffer.slice(0, index)
      this.buffer = this.buffer.slice(index + consume)
      this.line(line, frames)
    }
    return frames
  }

  private line(line: string, frames: SseFrame[]): void {
    if (!line) {
      this.dispatch(frames)
      return
    }
    if (line.startsWith(':')) return
    const colon = line.indexOf(':')
    const field = colon < 0 ? line : line.slice(0, colon)
    let value = colon < 0 ? '' : line.slice(colon + 1)
    if (value.startsWith(' ')) value = value.slice(1)
    if (field === 'data') this.data.push(value)
    if (field === 'event') this.event = value
    if (field === 'id' && !value.includes('\0')) this.id = value
  }

  private dispatch(frames: SseFrame[]): void {
    if (this.data.length) {
      frames.push({ event: this.event || 'message', id: this.id, data: this.data.join('\n') })
    }
    this.data = []
    this.event = ''
  }
}

export type StreamEvent =
  | { name: 'run_snapshot'; data: RunSnapshot }
  | { name: 'transcript_item' | 'message_started' | 'message_delta' | 'message_finalized'; data: SessionProcess }
  | { name: 'stream_end' }

function object(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function runSnapshot(value: unknown, runId: string): value is RunSnapshot {
  return object(value) && value.run_id === runId && typeof value.status === 'string' &&
    typeof value.analysis_phase === 'string' && object(value.summary) && object(value.graph_outcome) &&
    object(value.persistence) && Array.isArray(value.sessions) && value.sessions.every(object) &&
    Array.isArray(value.waterbodies) && value.waterbodies.every(object)
}

function sessionProcess(value: unknown, runId: string, sessionId: string | null): value is SessionProcess {
  return object(value) && value.run_id === runId && value.session_id === sessionId &&
    Number.isSafeInteger(value.sequence) && object(value.payload)
}

async function streamError(response: Response): Promise<ApiError> {
  let value: unknown
  try { value = await response.json() } catch { value = null }
  const detail = object(value) && object(value.error) ? value.error : null
  const code = detail && typeof detail.code === 'string' ? detail.code : 'stream_failed'
  const historyUrl = object(value) && typeof value.history_url === 'string' && value.history_url.startsWith('/api/analysis/runs/')
    ? value.history_url : null
  return new ApiError(response.status, code, response.headers.get('X-Request-ID'), historyUrl)
}

export async function subscribe(
  kind: 'run' | 'session', runId: string, sessionId: string | null,
  onEvent: (event: StreamEvent) => void, signal: AbortSignal,
  fetcher: typeof fetch = (...args) => fetch(...args),
): Promise<void> {
  let lastSequence: number | null = null
  const path = `/api/analysis/runs/${encodeURIComponent(runId)}` +
    (kind === 'run' ? '/events' : `/sessions/${encodeURIComponent(sessionId ?? '')}/events`)
  while (!signal.aborted) {
    const headers = new Headers({ Accept: 'text/event-stream' })
    if (kind === 'session' && lastSequence !== null) headers.set('Last-Event-ID', String(lastSequence))
    let response: Response
    try {
      response = await fetcher(path, { headers, signal })
    } catch {
      if (signal.aborted) return
      await reconnectDelay(signal)
      continue
    }
    if (!response.ok) throw await streamError(response)
    if ((response.headers.get('Content-Type')?.split(';', 1)[0] ?? '').trim().toLowerCase() !== 'text/event-stream' || !response.body) {
      throw new ApiError(response.status, 'invalid_stream_content_type', response.headers.get('X-Request-ID'))
    }
    const parser = new SseParser()
    const reader = response.body.getReader()
    let ended = false
    const consume = (frame: SseFrame) => {
      let data: unknown
      try { data = JSON.parse(frame.data) } catch { throw new ApiError(200, 'malformed_stream_frame', response.headers.get('X-Request-ID')) }
      if (frame.event === 'stream_end') {
        ended = true
        onEvent({ name: 'stream_end' })
        return
      }
      if (kind === 'run') {
        if (frame.event !== 'run_snapshot' || !runSnapshot(data, runId)) throw new ApiError(200, 'malformed_stream_frame', response.headers.get('X-Request-ID'))
        onEvent({ name: 'run_snapshot', data })
        return
      }
      if (!sessionProcess(data, runId, sessionId) ||
          !['transcript_item', 'message_started', 'message_delta', 'message_finalized'].includes(frame.event)) {
        throw new ApiError(200, 'malformed_stream_frame', response.headers.get('X-Request-ID'))
      }
      const sequence = data.sequence
      if (frame.id !== String(sequence)) throw new ApiError(200, 'malformed_stream_frame', response.headers.get('X-Request-ID'))
      if (lastSequence !== null && sequence <= lastSequence) return
      lastSequence = sequence
      onEvent({ name: frame.event as StreamEvent['name'] & ('transcript_item' | 'message_started' | 'message_delta' | 'message_finalized'), data })
    }
    try {
      while (!signal.aborted && !ended) {
        const { done, value } = await reader.read()
        if (done) break
        for (const frame of parser.feed(value)) consume(frame)
      }
      if (!signal.aborted && !ended) for (const frame of parser.finish()) consume(frame)
    } catch (error) {
      if (error instanceof ApiError || signal.aborted) throw error
    } finally {
      await reader.cancel().catch(() => undefined)
      reader.releaseLock()
    }
    if (ended || signal.aborted) return
    await reconnectDelay(signal)
  }
}

function reconnectDelay(signal: AbortSignal): Promise<void> {
  return new Promise<void>((resolve) => {
    const timer = setTimeout(resolve, 500)
    signal.addEventListener('abort', () => { clearTimeout(timer); resolve() }, { once: true })
  })
}
