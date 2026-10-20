import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { api, type RunDetail } from '../src/api/http'
import { App } from '../src/app/App'

vi.mock('../src/features/analysis/TopologyMap', () => ({ TopologyMap: () => <div>Topology</div> }))
vi.mock('../src/features/history/HistoryList', () => ({ HistoryList: () => <div>History</div> }))

const runId = '00000000-0000-4000-8000-000000000001'
const sessionId = '00000000-0000-4000-8000-000000000002'
const resultItemId = '00000000-0000-4000-8000-000000000003'
const start = '2026-10-13T05:00:00Z'
const end = '2026-10-13T11:00:00Z'

afterEach(() => { cleanup(); vi.restoreAllMocks(); window.history.replaceState({}, '', '/') })

it('shows historical route estimates from a read-only overlay without rewriting saved result content', async () => {
  window.history.replaceState({}, '', `/history?run=${runId}&session=${sessionId}`)
  vi.spyOn(api, 'settings').mockResolvedValue({} as never)
  vi.spyOn(api, 'run').mockResolvedValue({
    run: { run_id: runId, status: 'completed', query: 'Saved pH run',
      created_at: start, updated_at: end, context_summary: null, result_ref: null,
      result_summary: null },
    source: 'postgresql', sessions: [{ session_id: sessionId, target_comid: '3',
      agent_role: 'waterbody', status: 'completed', created_at: start, updated_at: end,
      result_ref: { run_id: runId, item_id: resultItemId }, result_summary: null }],
    items: [], referenced_items: [], issues: [],
    persistence: { state: 'up_to_date', warning: null },
  } as RunDetail)
  vi.spyOn(api, 'transcript').mockResolvedValue({ items: [], referenced_items: [] } as never)
  const savedText = JSON.stringify({
    run_id: runId, session_id: sessionId, time_range: { start, end },
    analysis_timezone: 'America/Chicago', time_resolution: '6h', mode: 'gap_filling',
    window_assessments: [], series: [{ parameter: {
      key: { code_system: 'usgs', code: '00400' }, name: 'pH',
    }, points: [{ cell_id: 'cell:ph:1', start_time: start, end_time: end,
      representative_value: '8.2', value_lower: '7.9', value_upper: '8.5',
      unit_id: 'standard units', source: 'inferred', aggregation_method: null,
      outcome_status: 'quantified', inference_method: 'model_estimate',
      inference_basis: 'same_parameter_measurement', quality_flags: [],
      confidence: 0.22, confidence_rationale: 'Remote measured anchor.', evidence_refs: [],
      measured_anchors: [
        { origin_comid: '1', station_id: 'USGS-123', path_comids: ['1', '2', '3'],
          distance_km: null },
        { origin_comid: '1', station_id: 'USGS-123', path_comids: ['1', '4', '3'],
          distance_km: null },
      ],
    }] }],
  })
  vi.spyOn(api, 'content').mockResolvedValue({ run_id: runId, item_id: resultItemId,
    text: savedText, content_type: 'inference_result', content_bytes: savedText.length,
    content_schema_version: 1, issues: [], source: 'postgresql' })
  const estimateRequest = vi.spyOn(api, 'anchorDistanceEstimates').mockResolvedValue({
    result_item_id: resultItemId, estimates: [
      { cell_id: 'cell:ph:1', anchor_index: 0, distance_km: 2.94,
        distance_basis: 'mapped_channel_estimate' },
      { cell_id: 'cell:ph:1', anchor_index: 1, distance_km: 3.78,
        distance_basis: 'mapped_channel_estimate' },
    ],
  })

  render(<App />)
  await waitFor(() => expect(estimateRequest).toHaveBeenCalledWith(runId, resultItemId,
    expect.any(AbortSignal)))
  expect(document.querySelector('.chart-frame')).toBeNull()
  fireEvent.click(await screen.findByRole('button', { name: 'View selected waterbody time series' }))
  await waitFor(() => expect(screen.getByText('Saved time series').closest('details')).toHaveAttribute('open'))
  expect(document.querySelector('.chart-frame')).not.toBeNull()
  const notes = await screen.findByRole('list', { name: 'pH chart source and quality' })
  await waitFor(() => expect(within(notes).getByRole('listitem')).toHaveTextContent(
    'Approx. river distance 2.94 km (saved channel geometry)'))
  expect(within(notes).getByRole('listitem')).toHaveTextContent(
    'Approx. river distance 3.78 km (saved channel geometry)')
  expect(JSON.parse(savedText).series[0].points[0].measured_anchors[0].distance_km).toBeNull()
  fireEvent.click(screen.getByText('Saved time series'))
  await waitFor(() => expect(document.querySelector('.chart-frame')).toBeNull())
})
