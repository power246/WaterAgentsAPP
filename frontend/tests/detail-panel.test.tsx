import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { api, type RunSnapshot, type TranscriptItem } from '../src/api/http'
import { DetailPanel } from '../src/features/analysis/DetailPanel'
import type { Graph } from '../src/features/analysis/models'

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

afterEach(() => { cleanup(); vi.restoreAllMocks() })

it('keeps persistence retry separate from analysis status and shows three tabs', async () => {
  const retry = vi.spyOn(api, 'retryPersistence').mockResolvedValue({ run_id: runId,
    persistence: { state: 'pending', warning: null } })
  const refresh = vi.fn()
  render(<DetailPanel runId={runId} snapshot={snapshot} detail={null} graph={null} sessionId={sessionId}
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
  render(<DetailPanel runId={runId} snapshot={snapshot} detail={null} graph={null} sessionId={sessionId}
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
  render(<DetailPanel runId={runId} snapshot={snapshot} detail={null} graph={null} sessionId={sessionId}
    processItems={[{ itemId: item.item_id, header: item, finalized: item, delta: '', generationStatus: null }]}
    referencedItems={[]} result={null} summary={null} readView="history"
    tab="evidence" onTab={() => undefined} onRefresh={() => undefined} />)
  fireEvent.click(screen.getByRole('button', { name: 'Read saved content' }))
  await waitFor(() => expect(content).toHaveBeenCalledWith(runId, item.item_id, 'history', expect.any(AbortSignal)))
  expect(await screen.findByText('Saved full evidence')).toBeInTheDocument()
})

it.each(['completed', 'incomplete'])('shows the saved agent message status %s', (status) => {
  const item: TranscriptItem = { ...historical, content_type: 'waterbody_agent_message',
    content: { kind: 'inline', body: { text: 'Saved agent message', generation_status: status } } }
  render(<DetailPanel runId={runId} snapshot={snapshot} detail={null} graph={null} sessionId={sessionId}
    processItems={[{ itemId: item.item_id, header: item, finalized: item, delta: '', generationStatus: null }]}
    referencedItems={[]} result={null} summary={null} readView="history"
    tab="agent" onTab={() => undefined} onRefresh={() => undefined} />)
  expect(screen.getByRole('heading', { name: `Agent message · ${status}` })).toBeInTheDocument()
  expect(screen.queryByRole('heading', { name: 'Agent message · generating' })).not.toBeInTheDocument()
})

it('does not label a saved reference message as still generating', () => {
  const item: TranscriptItem = { ...historical, content_type: 'waterbody_agent_message',
    content: { kind: 'reference', preview_head: 'Saved', preview_tail: 'message', truncated: true } }
  render(<DetailPanel runId={runId} snapshot={snapshot} detail={null} graph={null} sessionId={sessionId}
    processItems={[{ itemId: item.item_id, header: item, finalized: item, delta: '', generationStatus: null }]}
    referencedItems={[]} result={null} summary={null} readView="history"
    tab="agent" onTab={() => undefined} onRefresh={() => undefined} />)
  expect(screen.getByRole('heading', { name: 'Agent message · saved' })).toBeInTheDocument()
})

it('expands long inline content locally without requesting another read', () => {
  const suffix = 'end-of-saved-message'
  const longItem: TranscriptItem = { ...historical, content: { kind: 'inline', body: `${'a'.repeat(900)}${suffix}` } }
  const content = vi.spyOn(api, 'content')
  render(<DetailPanel runId={runId} snapshot={snapshot} detail={null} graph={null} sessionId={sessionId}
    processItems={[{ itemId: longItem.item_id, header: longItem, finalized: longItem,
      delta: '', generationStatus: null }]}
    referencedItems={[]} result={null} summary={null} readView="history"
    tab="evidence" onTab={() => undefined} onRefresh={() => undefined} />)
  expect(screen.queryByText(new RegExp(suffix))).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Read full inline content' }))
  expect(screen.getByText(new RegExp(suffix))).toBeInTheDocument()
  expect(content).not.toHaveBeenCalled()
})

it('shows a saved numeric method and its input evidence in the evidence detail', () => {
  const fragment: TranscriptItem = {
    ...historical,
    content: { kind: 'inline', body: {
      parameter_refs: [{ code_system: 'usgs', code: '00011' }],
      payload: {
        payload_type: 'numeric_derived', value: '8.2', unit: 'standard units',
        method_id: 'registered_method', method_version: 1,
        input_evidence_refs: [{ item_id: 'saved-input', evidence_id: 'aggregate-a' }],
      },
    } },
  }
  const plan: TranscriptItem = {
    ...historical, item_id: '00000000-0000-4000-8000-000000000004',
    transcript_sequence: 2, content_type: 'numeric_dependency_plan',
    content: { kind: 'inline', body: { plan_digest: 'saved-plan' } },
  }
  const view = render(<DetailPanel runId={runId} snapshot={snapshot} detail={null} graph={null} sessionId={sessionId}
    processItems={[
      { itemId: fragment.item_id, header: fragment, finalized: fragment, delta: '', generationStatus: null },
      { itemId: plan.item_id, header: plan, finalized: plan, delta: '', generationStatus: null },
    ]} referencedItems={[]} result={null} summary={null}
    readView="history" tab="evidence" onTab={() => undefined} onRefresh={() => undefined} />)
  expect(within(view.container).getByText('numeric dependency plan · 2')).toBeInTheDocument()
  fireEvent.click(within(view.container).getByText('evidence fragment · 1'))
  expect(within(view.container).getByText(/Calculated value \(00011\): 8\.2 standard units/)).toHaveTextContent(
    'method registered_method v1; input evidence: aggregate-a',
  )
})

it('separates usable waterbody results from missing upstream branch coverage', () => {
  const branchOutcomes: Graph['branch_outcomes'] = ['101', '201'].flatMap((comid) =>
    Array.from({ length: 8 }, (_, index) => ({
      branch_root_comid: comid, stop_comid: comid, path_to_target: [comid, '123'],
      blocked_upstream_comid: null, outcome: 'no_qualifying_source' as const,
      reason: 'natural_headwater' as const,
      coverage_unit: { parameter: { code_system: 'usgs', code: index < 4 ? '00400' : '00060' },
        segment_index: 0, window_index: index % 4 },
    })))
  branchOutcomes.push({
    branch_root_comid: '301', stop_comid: '301', path_to_target: ['301', '123'],
    blocked_upstream_comid: '302', outcome: 'backup_search_ended', reason: 'max_nodes_reached',
    coverage_unit: { parameter: { code_system: 'usgs', code: '00400' }, segment_index: 0, window_index: 0 },
  })
  const graph = { branch_outcomes: branchOutcomes, coverage: 'partial' } as Graph
  const completed = { ...snapshot, graph_outcome: { coverage: 'partial' as const,
    usable_nodes: 7, insufficient_nodes: 0, invalid_nodes: 0, cancelled_nodes: 0 } }
  render(<DetailPanel runId={runId} snapshot={completed} detail={null} graph={graph} sessionId={sessionId}
    processItems={[]} referencedItems={[]} result={null} summary={null}
    readView="history" tab="overview" onTab={() => undefined} onRefresh={() => undefined} />)
  expect(screen.getByText('Waterbody results').closest('p')).toHaveTextContent('7/7 usable')
  expect(screen.getByText('Graph coverage').closest('p')).toHaveTextContent('partial')
  const notes = screen.getByRole('list', { name: 'Graph coverage details' })
  expect(within(notes).getByText(/^2 upstream branch paths have no qualifying source \(headwater\)/)).toBeInTheDocument()
  expect(within(notes).getByText(/^Backup search ended \(node limit\)/)).toBeInTheDocument()
  expect(within(notes).queryByText(/16 upstream branch paths/)).not.toBeInTheDocument()
})

it('explains a required search limit and invalid waterbody result separately', () => {
  const graph = { coverage: 'partial', branch_outcomes: [{
    branch_root_comid: '301', stop_comid: '301', path_to_target: ['301', '123'],
    blocked_upstream_comid: '302', outcome: 'search_limit_reached', reason: 'max_nodes_reached',
    coverage_unit: { parameter: { code_system: 'usgs', code: '00400' }, segment_index: 0, window_index: 0 },
  }] } as Graph
  const completed = { ...snapshot, graph_outcome: { coverage: 'partial' as const,
    usable_nodes: 6, insufficient_nodes: 0, invalid_nodes: 1, cancelled_nodes: 0 } }
  render(<DetailPanel runId={runId} snapshot={completed} detail={null} graph={graph} sessionId={sessionId}
    processItems={[]} referencedItems={[]} result={null} summary={null}
    readView="history" tab="overview" onTab={() => undefined} onRefresh={() => undefined} />)
  const notes = screen.getByRole('list', { name: 'Graph coverage details' })
  expect(within(notes).getByText(/^1 required branch path reached a search limit \(node limit\)/)).toBeInTheDocument()
  expect(within(notes).getByText('Invalid waterbody results: 1.')).toBeInTheDocument()
  expect(screen.getByText('Waterbody results').closest('p')).toHaveTextContent('6/7 usable')
})
