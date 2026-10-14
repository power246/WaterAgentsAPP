import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { api, ApiError, type CleanupPreview, type RunDetail, type RunSnapshot, type SessionProcess, type TranscriptItem } from '../src/api/http'
import { subscribe } from '../src/api/sse'
import { App } from '../src/app/App'

vi.mock('../src/api/sse', () => ({ subscribe: vi.fn() }))
vi.mock('../src/features/analysis/TopologyMap', () => ({ TopologyMap: ({ onSelect }:
  { onSelect: (comid: string, sessionId: string | null) => void }) =>
  <button onClick={() => onSelect('999', null)}>Select excluded waterbody</button> }))
vi.mock('../src/features/analysis/NewAnalysisForm', () => ({ NewAnalysisForm: ({ onCreated }:
  { onCreated: (id: string) => void }) => <button onClick={() => onCreated('00000000-0000-4000-8000-000000000004')}>
    Go to run B</button> }))

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

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.mocked(subscribe).mockReset(); window.history.replaceState({}, '', '/') })

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

it('does not apply run A history fallback to a newly selected run B', async () => {
  const runB = '00000000-0000-4000-8000-000000000004'
  const historyUrl = `/api/analysis/runs/${runId}?view=history`
  window.history.replaceState({}, '', `/runs/${runId}`)
  vi.spyOn(api, 'settings').mockResolvedValue({} as never)
  const history = vi.spyOn(api, 'historyAt').mockResolvedValue({
    run: { run_id: runId, status: 'completed', query: 'Saved run A', created_at: time, updated_at: time,
      context_summary: null, result_ref: null, result_summary: null },
    source: 'postgresql', sessions: [], items: [], referenced_items: [], issues: [],
    persistence: { state: 'up_to_date', warning: null },
  } as RunDetail)
  const bSignals: AbortSignal[] = []
  vi.mocked(subscribe).mockImplementation(async (kind, id, _session, onEvent, signal) => {
    if (kind !== 'run') return
    if (id === runId) throw new ApiError(410, 'live_state_gone', null, historyUrl)
    bSignals.push(signal)
    onEvent({ name: 'run_snapshot', data: { ...snapshot, run_id: runB } })
    await new Promise<void>((resolve) => signal.addEventListener('abort', () => resolve(), { once: true }))
  })
  render(<App />)
  expect(await screen.findByText('Saved run A')).toBeInTheDocument()
  expect(history).toHaveBeenCalledWith(historyUrl, expect.any(AbortSignal))
  fireEvent.click(screen.getByRole('button', { name: 'Go to run B' }))
  await waitFor(() => expect(bSignals).toHaveLength(1))
  expect(window.location.pathname).toBe(`/runs/${runB}`)
  expect(bSignals[0]?.aborted).toBe(false)
  expect(history).toHaveBeenCalledTimes(1)
})

it('uses run history and historical transcript after a session stream returns 410', async () => {
  window.history.replaceState({}, '', `/runs/${runId}/waterbodies/${sessionId}`)
  vi.spyOn(api, 'settings').mockResolvedValue({} as never)
  const historyAt = vi.spyOn(api, 'historyAt').mockResolvedValue({
    run: { run_id: runId, status: 'completed', query: 'Saved session run', created_at: time,
      updated_at: time, context_summary: null, result_ref: null, result_summary: null },
    source: 'postgresql', sessions: [{ ...snapshot.sessions[0]!, status: 'completed' }],
    items: [], referenced_items: [], issues: [], persistence: { state: 'up_to_date', warning: null },
  } as RunDetail)
  const transcript = vi.spyOn(api, 'transcript').mockResolvedValue({ items: [], referenced_items: [] } as never)
  vi.mocked(subscribe).mockImplementation(async (kind, _id, _session, onEvent, signal) => {
    if (kind === 'session') throw new ApiError(410, 'live_state_gone', null,
      `/api/analysis/runs/${runId}/sessions/${sessionId}/transcript?view=history`)
    onEvent({ name: 'run_snapshot', data: snapshot })
    await new Promise<void>((resolve) => signal.addEventListener('abort', () => resolve(), { once: true }))
  })
  render(<App />)
  fireEvent.click(screen.getByRole('tab', { name: 'Agent' }))
  await waitFor(() => expect(historyAt).toHaveBeenCalledWith(
    `/api/analysis/runs/${runId}?view=history`, expect.any(AbortSignal)))
  await waitFor(() => expect(transcript).toHaveBeenCalledWith(runId, sessionId, 'history', expect.any(AbortSignal)))
  fireEvent.click(screen.getByRole('tab', { name: 'Overview' }))
  expect(await screen.findByText('Saved session run')).toBeInTheDocument()
})

it('clears the previous session when selecting a waterbody without a session', async () => {
  window.history.replaceState({}, '', `/runs/${runId}/waterbodies/${sessionId}`)
  vi.spyOn(api, 'settings').mockResolvedValue({} as never)
  vi.mocked(subscribe).mockImplementation(async (kind, _id, _session, onEvent, signal) => {
    if (kind === 'run') onEvent({ name: 'run_snapshot', data: snapshot })
    await new Promise<void>((resolve) => signal.addEventListener('abort', () => resolve(), { once: true }))
  })
  render(<App />)
  expect(screen.getByText('Saved time series')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Select excluded waterbody' }))
  await waitFor(() => expect(window.location.pathname).toBe(`/runs/${runId}`))
  expect(screen.queryByText('Saved time series')).not.toBeInTheDocument()
  expect(screen.getByText(/COMID 999: session not started/)).toBeInTheDocument()
})

it('closes a run deletion dialog when History selects another run', async () => {
  const runB = '00000000-0000-4000-8000-000000000008'
  window.history.replaceState({}, '', `/history?run=${runId}`)
  vi.spyOn(api, 'settings').mockResolvedValue({} as never)
  vi.spyOn(api, 'history').mockResolvedValue({ items: [runId, runB].map((id) => ({
    run_id: id, query: id === runId ? 'Saved A' : 'Saved B', status: 'completed',
    created_at: time, updated_at: time, configuration: null, context_summary: null,
    result_ref: null, result_summary: null, issues: [],
  })), next_cursor: null })
  vi.spyOn(api, 'run').mockImplementation(async (id) => ({
    run: { run_id: id, status: 'completed', query: `Run ${id}`, created_at: time,
      updated_at: time, context_summary: null, result_ref: null, result_summary: null },
    source: 'postgresql', sessions: [], items: [], referenced_items: [], issues: [],
    persistence: { state: 'up_to_date', warning: null },
  } as RunDetail))
  const preview: CleanupPreview = { requested_run_ids: [runId], delete_run_ids: [runId],
    delete_order: [runId], dependencies: [], cascade_required: false, cascade_authorized: false,
    estimated: { runs: 1, sessions: 1, transcript_items: 2, retained_idempotency_tombstones: 1 },
    preserved: [], confirmation: { token: 'token-for-A', required_phrase: 'DELETE 1 RUN', expires_at: time } }
  vi.spyOn(api, 'previewRunCleanup').mockResolvedValue(preview)
  const execute = vi.spyOn(api, 'executeRunCleanup')
  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: 'Delete this saved run' }))
  await screen.findByRole('button', { name: 'Delete these runs' })
  fireEvent.click(screen.getByText('Saved B'))
  await waitFor(() => expect(window.location.search).toBe(`?run=${runB}`))
  expect(screen.queryByRole('dialog', { name: 'Delete saved run' })).not.toBeInTheDocument()
  expect(execute).not.toHaveBeenCalled()
})
