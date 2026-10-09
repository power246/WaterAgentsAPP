import { describe, expect, it } from 'vitest'
import type { RunDetail, RunSnapshot, SessionProcess } from '../src/api/http'
import type { components } from '../src/api/generated'
import { emptyRun, emptyTranscript, runReducer, transcriptReducer } from '../src/features/analysis/state'
import { nodeVisual, originalTimezone, visualLabels } from '../src/features/analysis/models'

const runId = '00000000-0000-4000-8000-000000000001'
const sessionId = '00000000-0000-4000-8000-000000000002'
const itemId = '00000000-0000-4000-8000-000000000003'
const time = '2026-10-08T00:00:00Z'

const snapshot: RunSnapshot = {
  schema_version: 1, run_id: runId, status: 'running', analysis_phase: 'waterbody_execution', created_at: time, updated_at: time,
  context_ref: null, topology_ref: null, result_ref: null,
  summary: { status: 'not_started', session_id: null, result_ref: null, error: null },
  graph_outcome: { coverage: null, usable_nodes: 0, insufficient_nodes: 0, invalid_nodes: 0, cancelled_nodes: 0 },
  sessions: [], waterbodies: [], persistence: { state: 'pending', warning: null },
}

const detail: RunDetail = {
  run: { run_id: runId, status: 'pending', created_at: time, updated_at: time,
    query: 'Original question', context_summary: null, result_ref: null, result_summary: null },
  source: 'live', sessions: [], items: [], referenced_items: [], issues: [], persistence: { state: 'pending', warning: null },
}

describe('feature state ownership', () => {
  it('keeps a received SSE replacement when an older GET returns late', () => {
    const opened = runReducer(emptyRun, { type: 'open', runId })
    const streamed = runReducer(opened, { type: 'sse', snapshot })
    const late = runReducer(streamed, { type: 'get', detail,
      snapshot: { ...snapshot, status: 'pending' } })
    expect(late.snapshot?.status).toBe('running')
    expect(late.detail?.run.query).toBe('Original question')
    expect(runReducer(late, { type: 'sse', snapshot: { ...snapshot, status: 'completed', persistence: { state: 'retry_exhausted', warning: null } } }).snapshot?.persistence.state)
      .toBe('retry_exhausted')
  })

  it('assembles started, distinct deltas, and finalized in the first message position', () => {
    const header: components['schemas']['TranscriptItemHeader'] = { item_id: itemId, run_id: runId, transcript_sequence: 4, occurred_at: time,
      content_type: 'waterbody_agent_message', content_schema_version: 1,
      scope: { session_id: sessionId, agent_role: 'waterbody', target_comid: '123' },
      correlation: { request_id: null, operation_id: null, error_id: null }, tags: [], rag_eligible: false }
    const event = (sequence: number, payload: SessionProcess['payload']): SessionProcess => ({
      schema_version: 1, run_id: runId, session_id: sessionId, sequence, occurred_at: time, payload,
    })
    let state = transcriptReducer(emptyTranscript, { type: 'open', sessionId })
    state = transcriptReducer(state, { type: 'event', name: 'message_started', data: event(1, { item: header }) })
    state = transcriptReducer(state, { type: 'event', name: 'message_delta', data: event(2, { item_id: itemId, text: 'Water ' }) })
    state = transcriptReducer(state, { type: 'event', name: 'message_delta', data: event(3, { item_id: itemId, text: 'quality' }) })
    const finalized: components['schemas']['TranscriptItemView'] = {
      ...header, content_bytes: 12, content_ref: { run_id: runId, item_id: itemId },
      content: { kind: 'inline', body: { text: 'Water quality' } }, parse_status: 'ok',
      confidence: null, provenance: null, parse_error: null,
    }
    state = transcriptReducer(state, { type: 'event', name: 'message_finalized', data: event(4, { item: finalized, generation_status: 'completed' }) })
    expect(state.items).toHaveLength(1)
    expect(state.items[0]?.delta).toBe('Water quality')
    expect(state.items[0]?.finalized?.item_id).toBe(itemId)
    expect(transcriptReducer(state, { type: 'event', name: 'message_delta', data: event(3, { item_id: itemId, text: 'duplicate' }) })).toBe(state)
    expect(transcriptReducer(state, { type: 'open', sessionId: 'another' }).items).toEqual([])
  })

  it('maps all seven backend node states to labels and preserves legacy timezone', () => {
    const states = [
      nodeVisual(false, null, 'running'), nodeVisual(true, null, 'running'),
      nodeVisual(true, 'running', 'running'), nodeVisual(true, 'completed', 'completed'),
      nodeVisual(true, 'failed', 'failed'), nodeVisual(true, 'cancelled', 'cancelled'),
      nodeVisual(true, null, 'cancelled'),
    ]
    expect(new Set(states).size).toBe(7)
    expect(states.every((state) => Boolean(visualLabels[state]))).toBe(true)
    expect(originalTimezone(null)).toBe('legacy/unknown')
    expect(originalTimezone({ analysis_timezone: 'America/Denver' })).toBe('America/Denver')
  })
})
