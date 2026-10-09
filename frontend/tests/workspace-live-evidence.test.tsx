import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { api, type RunSnapshot, type SessionProcess, type TranscriptItem } from '../src/api/http'
import { subscribe } from '../src/api/sse'
import { App } from '../src/app/App'

vi.mock('../src/api/sse', () => ({ subscribe: vi.fn() }))
vi.mock('../src/features/analysis/TopologyMap', () => ({ TopologyMap: () => <div /> }))
vi.mock('../src/features/analysis/NewAnalysisForm', () => ({ NewAnalysisForm: () => <div /> }))

const runId = '00000000-0000-4000-8000-000000000001'
const sessionId = '00000000-0000-4000-8000-000000000002'
const itemId = '00000000-0000-4000-8000-000000000003'
const time = '2026-10-08T00:00:00Z'
const snapshot: RunSnapshot = {
  schema_version: 1, run_id: runId, status: 'running', analysis_phase: 'waterbody_execution',
  created_at: time, updated_at: time, context_ref: null, topology_ref: null, result_ref: null,
  summary: { status: 'not_started', session_id: null, result_ref: null, error: null },
  graph_outcome: { coverage: null, usable_nodes: 0, insufficient_nodes: 0, invalid_nodes: 0, cancelled_nodes: 0 },
  sessions: [{ session_id: sessionId, target_comid: '123', agent_role: 'waterbody', status: 'running',
    created_at: time, updated_at: time, result_ref: null, result_summary: null }],
  waterbodies: [], persistence: { state: 'pending', warning: null },
}
const item: TranscriptItem = {
  item_id: itemId, run_id: runId, transcript_sequence: 1, occurred_at: time,
  content_type: 'evidence_fragment', content_schema_version: 1,
  scope: { session_id: sessionId, agent_role: 'waterbody', target_comid: '123' },
  correlation: { request_id: null, operation_id: null, error_id: null }, tags: [], rag_eligible: false,
  content_bytes: 13, content_ref: { run_id: runId, item_id: itemId },
  content: { kind: 'inline', body: { text: 'New live proof' } }, parse_status: 'ok',
  confidence: null, provenance: null, parse_error: null,
}

afterEach(() => { cleanup(); vi.restoreAllMocks(); window.history.replaceState({}, '', '/') })

it('keeps one selected session stream while switching from Agent to Evidence', async () => {
  window.history.replaceState({}, '', `/runs/${runId}/waterbodies/${sessionId}`)
  vi.spyOn(api, 'settings').mockResolvedValue({} as never)
  const sessionEvents: Array<(event: { name: 'transcript_item'; data: SessionProcess }) => void> = []
  const sessionSignals: AbortSignal[] = []
  vi.mocked(subscribe).mockImplementation(async (kind, _run, _session, onEvent, signal) => {
    if (kind === 'run') { onEvent({ name: 'run_snapshot', data: snapshot }); return }
    sessionEvents.push(onEvent as (event: { name: 'transcript_item'; data: SessionProcess }) => void)
    sessionSignals.push(signal)
    await new Promise<void>((resolve) => signal.addEventListener('abort', () => resolve(), { once: true }))
  })
  render(<App />)
  fireEvent.click(screen.getByRole('tab', { name: 'Agent' }))
  await waitFor(() => expect(sessionEvents).toHaveLength(1))
  fireEvent.click(screen.getByRole('tab', { name: 'Evidence' }))
  expect(sessionSignals[0]?.aborted).toBe(false)
  sessionEvents[0]!({ name: 'transcript_item', data: {
    schema_version: 1, run_id: runId, session_id: sessionId, sequence: 1, occurred_at: time,
    payload: { item },
  } })
  expect(await screen.findByText('evidence fragment · 1')).toBeInTheDocument()
  expect(sessionEvents).toHaveLength(1)
})
