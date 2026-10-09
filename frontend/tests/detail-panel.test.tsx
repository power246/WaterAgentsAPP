import { afterEach, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { api, type RunSnapshot, type TranscriptItem } from '../src/api/http'
import { DetailPanel } from '../src/features/analysis/DetailPanel'

const runId = '00000000-0000-4000-8000-000000000001'
const sessionId = '00000000-0000-4000-8000-000000000002'
const time = '2026-10-08T00:00:00Z'
const snapshot: RunSnapshot = {
  schema_version: 1, run_id: runId, status: 'completed_with_persistence_error', analysis_phase: 'terminal',
  created_at: time, updated_at: time, context_ref: null, topology_ref: null, result_ref: null,
  summary: { status: 'not_started', session_id: null, result_ref: null, error: null },
  graph_outcome: { coverage: null, usable_nodes: 0, insufficient_nodes: 0, invalid_nodes: 0, cancelled_nodes: 0 },
  sessions: [{ session_id: sessionId, target_comid: '123', agent_role: 'waterbody', status: 'completed',
    created_at: time, updated_at: time, result_ref: null, result_summary: null }],
  waterbodies: [], persistence: { state: 'retry_exhausted', warning: null },
}
const historical: TranscriptItem = {
  item_id: '00000000-0000-4000-8000-000000000003', run_id: runId, transcript_sequence: 1,
  occurred_at: time, content_type: 'evidence_fragment', content_schema_version: 1,
  scope: { session_id: sessionId, agent_role: 'waterbody', target_comid: '123' },
  correlation: { request_id: null, operation_id: null, error_id: null }, tags: [], rag_eligible: false,
  content_bytes: 25, content_ref: { run_id: runId, item_id: '00000000-0000-4000-8000-000000000003' },
  content: { kind: 'inline', body: { text: 'Saved historical evidence' } }, parse_status: 'ok',
  confidence: null, provenance: null, parse_error: null,
}

afterEach(() => vi.restoreAllMocks())

it('keeps persistence retry separate from analysis status and shows three tabs', async () => {
  const retry = vi.spyOn(api, 'retryPersistence').mockResolvedValue({ run_id: runId,
    persistence: { state: 'pending', warning: null } })
  const refresh = vi.fn()
  render(<DetailPanel runId={runId} snapshot={snapshot} detail={null} sessionId={sessionId}
    processItems={[]} referencedItems={[]} result={null} summary={null}
    readView="auto" tab="overview" onTab={() => undefined} onRefresh={refresh} />)
  expect(screen.getAllByRole('tab')).toHaveLength(3)
  expect(screen.getByText('completed_with_persistence_error', { exact: false })).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Retry saving' }))
  await waitFor(() => expect(retry).toHaveBeenCalledWith(runId))
  expect(refresh).toHaveBeenCalledOnce()
  expect(screen.getByRole('status')).toHaveTextContent('Retry accepted')
})

it('labels saved historical evidence as context and excludes unrelated records', () => {
  render(<DetailPanel runId={runId} snapshot={snapshot} detail={null} sessionId={sessionId}
    processItems={[]} referencedItems={[historical]} result={null} summary={null}
    readView="history" tab="evidence" onTab={() => undefined} onRefresh={() => undefined} />)
  expect(screen.getByText('Historical context')).toBeInTheDocument()
  expect(screen.getByText(/do not replace current observations/)).toBeInTheDocument()
  expect(screen.getByText('evidence fragment · 1')).toBeInTheDocument()
  expect(screen.queryByText(/raw prompt|provider response/i)).not.toBeInTheDocument()
})

it('reads a process record using the selected history view', async () => {
  const content = vi.spyOn(api, 'content').mockResolvedValue({ run_id: runId,
    item_id: historical.item_id, text: 'Saved full evidence', content_type: 'evidence_fragment',
    content_bytes: 19, content_schema_version: 1, issues: [], source: 'postgresql' })
  const item: TranscriptItem = { ...historical, content: { kind: 'reference',
    preview_head: 'Saved', preview_tail: 'evidence', truncated: true } }
  render(<DetailPanel runId={runId} snapshot={snapshot} detail={null} sessionId={sessionId}
    processItems={[{ itemId: item.item_id, header: item, finalized: item, delta: '', generationStatus: null }]}
    referencedItems={[]} result={null} summary={null} readView="history"
    tab="evidence" onTab={() => undefined} onRefresh={() => undefined} />)
  fireEvent.click(screen.getByRole('button', { name: 'Read saved content' }))
  await waitFor(() => expect(content).toHaveBeenCalledWith(runId, item.item_id, 'history', expect.any(AbortSignal)))
  expect(await screen.findByText('Saved full evidence')).toBeInTheDocument()
})
