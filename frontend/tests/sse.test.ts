import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '../src/api/http'
import { SseParser, subscribe } from '../src/api/sse'

const runId = '00000000-0000-4000-8000-000000000001'
const sessionId = '00000000-0000-4000-8000-000000000002'

function stream(text: string): Response {
  return new Response(new TextEncoder().encode(text), { headers: { 'Content-Type': 'text/event-stream' } })
}

describe('single SSE parser and client', () => {
  it('assembles every byte split, UTF-8, CRLF, comments and multiline JSON data', () => {
    const source = ': keepalive\r\nevent: message_delta\r\nid: 7\r\ndata: {"text":\r\ndata: "水温"}\r\n\r\n'
    const bytes = new TextEncoder().encode(source)
    const parser = new SseParser()
    const frames = [...bytes].flatMap((byte) => parser.feed(Uint8Array.of(byte)))
    frames.push(...parser.finish())
    expect(frames).toEqual([{ event: 'message_delta', id: '7', data: '{"text":\n"水温"}' }])
    expect(JSON.parse(frames[0]!.data)).toEqual({ text: '水温' })
  })

  it('keeps different deltas for the same item and deduplicates only the integer event ID', async () => {
    const make = (sequence: number, text: string) =>
      `event: message_delta\nid: ${sequence}\ndata: ${JSON.stringify({ run_id: runId, session_id: sessionId, sequence, occurred_at: '2026-10-08T00:00:00Z', payload: { item_id: 'item', text } })}\n\n`
    const events: string[] = []
    await subscribe('session', runId, sessionId, (event) => {
      if (event.name === 'message_delta' && 'text' in event.data.payload) events.push(event.data.payload.text)
    }, new AbortController().signal, async () => stream(make(1, 'A') + make(1, 'A') + make(2, 'B') +
      `event: stream_end\ndata: ${JSON.stringify({ run_id: runId, session_id: sessionId, stream: 'session' })}\n\n`))
    expect(events).toEqual(['A', 'B'])
  })

  it('resumes a session with the last consumed ID after an interrupted stream', async () => {
    const make = (sequence: number) => `event: message_delta\nid: ${sequence}\ndata: ${JSON.stringify({
      run_id: runId, session_id: sessionId, sequence, occurred_at: '2026-10-08T00:00:00Z',
      payload: { item_id: 'item', text: String(sequence) },
    })}\n\n`
    const headers: (string | null)[] = []
    let calls = 0
    const fetcher: typeof fetch = async (_path, init) => {
      headers.push(new Headers(init?.headers).get('Last-Event-ID'))
      calls++
      return stream(calls === 1 ? make(1) : make(2) +
        `event: stream_end\ndata: ${JSON.stringify({ run_id: runId, session_id: sessionId, stream: 'session' })}\n\n`)
    }
    await subscribe('session', runId, sessionId, () => undefined, new AbortController().signal, fetcher)
    expect(headers).toEqual([null, '1'])
  })

  it('releases the selected session reader on abort without affecting the next session', async () => {
    const first = new AbortController()
    let readerCancelled = false
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode(`event: message_delta\nid: 1\ndata: ${JSON.stringify({
          run_id: runId, session_id: sessionId, sequence: 1, payload: { item_id: 'item', text: 'old' },
        })}\n\n`))
      },
      cancel() { readerCancelled = true },
    })
    const oldEvents: string[] = []
    await subscribe('session', runId, sessionId, (event) => {
      if (event.name === 'message_delta') { oldEvents.push('old'); first.abort() }
    }, first.signal, async () => new Response(body, { headers: { 'Content-Type': 'text/event-stream' } }))
    expect(oldEvents).toEqual(['old'])
    expect(readerCancelled).toBe(true)
    const nextSession = '00000000-0000-4000-8000-000000000004'
    const nextEvents: string[] = []
    await subscribe('session', runId, nextSession, (event) => {
      if (event.name === 'message_delta') nextEvents.push('new')
    }, new AbortController().signal, async () => stream(`event: message_delta\nid: 1\ndata: ${JSON.stringify({
      run_id: runId, session_id: nextSession, sequence: 1, payload: { item_id: 'item', text: 'new' },
    })}\n\nevent: stream_end\ndata: ${JSON.stringify({ run_id: runId, session_id: nextSession, stream: 'session' })}\n\n`))
    expect(nextEvents).toEqual(['new'])
  })

  it.each([400, 409, 410])('preserves %i stream errors and safe history location', async (status) => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({
      error: { code: status === 400 ? 'invalid_event_cursor' : status === 409 ? 'event_cursor_ahead' : 'live_state_gone' },
      history_url: status === 410 ? `/api/analysis/runs/${runId}?view=history` : null,
    }), { status, headers: { 'Content-Type': 'application/json' } }))
    await expect(subscribe('session', runId, sessionId, () => undefined,
      new AbortController().signal, fetcher)).rejects.toMatchObject({ status })
    expect(fetcher).toHaveBeenCalledTimes(1)
  })

  it('rejects malformed JSON frames without leaking frame data', async () => {
    await expect(subscribe('run', runId, null, () => undefined, new AbortController().signal,
      async () => stream('event: run_snapshot\ndata: {secret\n\n')))
      .rejects.toEqual(new ApiError(200, 'malformed_stream_frame', null))
  })

  it('rejects malformed UTF-8 and session payloads instead of reconnecting', async () => {
    const invalid = new SseParser()
    expect(() => invalid.feed(Uint8Array.of(0xff))).toThrow(ApiError)
    const malformed = `event: message_delta\nid: 1\ndata: ${JSON.stringify({
      run_id: runId, session_id: sessionId, sequence: 1, payload: 'unexpected',
    })}\n\n`
    const fetcher = vi.fn(async () => stream(malformed))
    await expect(subscribe('session', runId, sessionId, () => undefined, new AbortController().signal, fetcher))
      .rejects.toEqual(new ApiError(200, 'malformed_stream_frame', null))
    expect(fetcher).toHaveBeenCalledTimes(1)
  })
})
