import { afterEach, expect, it } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { isPhParameter, phAxisDomain, TimeSeries, windowTickFormatter } from '../src/features/analysis/TimeSeries'
import { hasMissingAnchorDistances, withAnchorDistanceEstimates,
  type InferenceResult } from '../src/features/analysis/models'

afterEach(cleanup)

it('separates a pH estimate from its range and labels six-hour windows in the analysis timezone', () => {
  expect(isPhParameter({ code_system: 'usgs', code: '00400' })).toBe(true)
  expect(isPhParameter({ code_system: 'usgs', code: '00403' })).toBe(true)
  expect(isPhParameter({ code_system: 'usgs', code: '00060' })).toBe(false)
  expect(isPhParameter({ code_system: 'other', code: '00400' })).toBe(false)
  expect(phAxisDomain([7.8, 8.2, 8.6])).toEqual([7.55, 8.85])
  const singleValueDomain = phAxisDomain([8.2])
  expect(singleValueDomain?.[0]).toBeCloseTo(7.95)
  expect(singleValueDomain?.[1]).toBeCloseTo(8.45)
  expect(phAxisDomain([])).toBeUndefined()
  expect(phAxisDomain([null, Number.NaN])).toBeUndefined()
  expect(phAxisDomain([0, 14])).toEqual([0, 14])
  expect(phAxisDomain([-0.5, 14.5])).toEqual([-3.5, 17.5])

  const starts = ['2026-10-15T05:00:00Z', '2026-10-15T11:00:00Z',
    '2026-10-15T17:00:00Z', '2026-10-15T23:00:00Z']
  const formatSameDay = windowTickFormatter(starts, 'America/Chicago')
  expect(starts.map(formatSameDay)).toEqual(['00:00', '06:00', '12:00', '18:00'])
  const formatAcrossDays = windowTickFormatter([starts[0]!, '2026-10-16T05:00:00Z'], 'America/Chicago')
  expect(formatAcrossDays(starts[0]!)).toBe('10/15 00:00')
  expect(formatAcrossDays('2026-10-16T05:00:00Z')).toBe('10/16 00:00')
})

it('distinguishes a window sample mean from an estimated empty window and shows its support', () => {
  const result = {
    analysis_timezone: 'America/Chicago', time_resolution: '6h', mode: 'gap_filling',
    series: [{
      parameter: { key: { code_system: 'usgs', code: '00400' }, name: 'pH' },
      points: [
        {
          cell_id: 'cell:ph:1', start_time: '2026-10-13T05:00:00Z',
          end_time: '2026-10-13T11:00:00Z', representative_value: '8.2', unit_id: 'standard units',
          source: 'inferred', aggregation_method: 'mean', outcome_status: 'quantified',
          temporal_estimate_method: null, quality_flags: ['provider_flagged', 'temporal_estimate'], confidence: 0.76,
          confidence_rationale: 'Mean of available readings in this window.', evidence_refs: ['fragment:mean'],
        },
        {
          cell_id: 'cell:ph:2', start_time: '2026-10-13T11:00:00Z',
          end_time: '2026-10-13T17:00:00Z', representative_value: '8.3', unit_id: 'standard units',
          source: 'inferred', aggregation_method: null, outcome_status: 'quantified',
          temporal_estimate_method: 'linear_interpolation', quality_flags: ['temporal_estimate'], confidence: 0.41,
          confidence_rationale: 'Linear interpolation from readings 6 hours away.',
          evidence_refs: ['fragment:temporal:2'],
        },
      ],
    }],
  } as unknown as InferenceResult

  render(<TimeSeries result={result} />)
  expect(screen.getByText('○ Estimated empty window')).toBeVisible()
  const chartNote = screen.getByRole('list', { name: 'pH chart source and quality' })
  expect(within(chartNote).getByText(/8\.2 standard units · Within-window mean of available readings · Confidence: 0.76/)).toBeVisible()
  expect(within(chartNote).getByText(/temporal_estimate \(supporting evidence\)/)).toBeVisible()
  expect(within(chartNote).getByText(/8\.3 standard units · Estimated empty window \(linear interpolation\) · Confidence: 0.41 · Linear interpolation from readings 6 hours away/)).toBeVisible()
  expect(screen.getByText('Windows and quality').closest('details')).not.toHaveAttribute('open')
  fireEvent.click(screen.getByText('Windows and quality'))

  const sampledRow = screen.getByText('Within-window mean of available readings').closest('tr')
  expect(sampledRow).not.toBeNull()
  expect(within(sampledRow!).getByText('provider_flagged, temporal_estimate (supporting evidence)')).toBeInTheDocument()
  const estimatedRow = screen.getByText('Estimated empty window (linear interpolation)').closest('tr')
  expect(estimatedRow).not.toBeNull()
  expect(within(estimatedRow!).getByText('temporal_estimate')).toBeInTheDocument()
  expect(within(estimatedRow!).getByText('fragment:temporal:2')).toBeInTheDocument()
  expect(within(estimatedRow!).getByText('Linear interpolation from readings 6 hours away.')).toBeInTheDocument()
})

it('shows range-only and distant model predictions without inventing a midpoint or hiding low confidence', () => {
  const common = {
    source: 'inferred', aggregation_method: null, quality_flags: [],
    unit_id: 'standard units', inference_method: 'model_estimate', confidence: 0.08,
    confidence_rationale: 'Long transfer path with an unquantified tributary.',
    evidence_refs: ['fragment:estimate'],
  }
  const result = {
    analysis_timezone: 'America/Chicago', time_resolution: '6h', mode: 'gap_filling',
    series: [{
      parameter: { key: { code_system: 'usgs', code: '00400' }, name: 'pH' },
      points: [
        {
          ...common, cell_id: 'cell:ph:range', start_time: '2026-10-13T05:00:00Z',
          end_time: '2026-10-13T11:00:00Z', outcome_status: 'range_estimate',
          representative_value: null, value_lower: '6.5', value_upper: '8.5',
          inference_basis: 'general_knowledge', measured_anchors: [],
        },
        {
          ...common, cell_id: 'cell:ph:point', start_time: '2026-10-13T11:00:00Z',
          end_time: '2026-10-13T17:00:00Z', outcome_status: 'quantified',
          representative_value: '8.1', value_lower: '7.3', value_upper: '8.8',
          inference_basis: 'same_parameter_measurement', measured_anchors: [{
            origin_comid: '1', station_id: 'USGS-123', path_comids: ['1', '2', '3'], distance_km: 145.6,
          }],
        },
        {
          ...common, cell_id: 'cell:ph:anchored-range', start_time: '2026-10-13T17:00:00Z',
          end_time: '2026-10-13T23:00:00Z', outcome_status: 'range_estimate',
          representative_value: null, value_lower: '6.8', value_upper: '8.9',
          inference_basis: 'same_parameter_measurement', measured_anchors: [{
            origin_comid: '1', station_id: 'USGS-123', path_comids: ['1', '2', '3'], distance_km: 145.6,
          }],
        },
      ],
    }],
  } as unknown as InferenceResult

  render(<TimeSeries result={result} />)
  const notes = screen.getByRole('list', { name: 'pH chart source and quality' })
  expect(screen.getByText('◆ Possible range limits')).toBeVisible()
  expect(within(notes).getByText(/6\.5 – 8\.5 standard units · Low-basis range prediction · Confidence: 0.08/)).toBeVisible()
  expect(within(notes).getByText(/General knowledge; local evidence unavailable · No same-parameter measured anchor/)).toBeVisible()
  expect(within(notes).getByText(/8\.1 standard units · Model estimate · Confidence: 0.08 · Possible range: 7.3 – 8.8/)).toBeVisible()
  expect(within(notes).getByText(/6\.8 – 8\.9 standard units · Range prediction · Confidence: 0.08/)).toBeVisible()
  expect(within(notes).getAllByText(/Recorded distance 145.6 km \(method unspecified\) · 2 waterbody steps · Measured at USGS-123/)).toHaveLength(2)
  fireEvent.click(screen.getByText('Windows and quality'))
  const rangeRow = screen.getAllByText('Range only')[0]!.closest('tr')!
  expect(within(rangeRow).getByText('6.5 – 8.5')).toBeInTheDocument()
  expect(within(rangeRow).queryByText('7.5')).not.toBeInTheDocument()
  expect(within(rangeRow).queryByText('unavailable')).not.toBeInTheDocument()
})

it('identifies related readings and reports unavailable anchor distance as unknown', () => {
  const point = {
    cell_id: 'cell:ph:1', start_time: '2026-10-13T05:00:00Z', end_time: '2026-10-13T11:00:00Z',
    representative_value: null, value_lower: '6.2', value_upper: '8.2', unit_id: 'standard units',
    source: 'inferred', aggregation_method: null, outcome_status: 'range_estimate',
    inference_method: 'model_estimate', inference_basis: 'related_evidence',
    quality_flags: [], confidence: 0.3, confidence_rationale: 'Water temperature informs this range.',
    evidence_refs: ['fragment:temperature'], measured_anchors: [],
  }
  const result = {
    analysis_timezone: 'America/Chicago', time_resolution: '6h', mode: 'gap_filling',
    series: [{ parameter: { key: { code_system: 'usgs', code: '00400' }, name: 'pH' }, points: [point] }],
  } as unknown as InferenceResult
  const { rerender } = render(<TimeSeries result={result} />)
  expect(within(screen.getByRole('list')).getByText(/Related readings or local information/)).toBeVisible()

  const unknownDistance = structuredClone(result)
  Object.assign(unknownDistance.series[0]!.points[0]!, {
    measured_anchors: [{ origin_comid: '1', station_id: null, path_comids: ['1', '2'], distance_km: null }],
  })
  rerender(<TimeSeries result={unknownDistance} />)
  expect(within(screen.getByRole('list')).getByText(/Distance unknown · 1 waterbody steps · Measured at COMID 1/)).toBeVisible()
})

it('labels mapped river distances as estimates and shows both routes from the nearest station', () => {
  const result = {
    analysis_timezone: 'America/Chicago', time_resolution: '6h', mode: 'gap_filling',
    series: [{ parameter: { key: { code_system: 'usgs', code: '00400' }, name: 'pH' }, points: [{
      cell_id: 'cell:ph:1', start_time: '2026-10-13T05:00:00Z', end_time: '2026-10-13T11:00:00Z',
      representative_value: '8.2', value_lower: '7.9', value_upper: '8.5', unit_id: 'standard units',
      source: 'inferred', aggregation_method: null, outcome_status: 'quantified',
      inference_method: 'model_estimate', inference_basis: 'same_parameter_measurement',
      quality_flags: [], confidence: 0.22, confidence_rationale: 'Remote measured anchor.',
      evidence_refs: ['fragment:estimate'], measured_anchors: [
        { origin_comid: '1', station_id: 'USGS-123', path_comids: ['1', '2', '3'],
          distance_km: 2.94, distance_basis: 'mapped_channel_estimate' },
        { origin_comid: '1', station_id: 'USGS-123', path_comids: ['1', '4', '3'],
          distance_km: 3.78, distance_basis: 'mapped_channel_estimate' },
        { origin_comid: '5', station_id: 'USGS-456', path_comids: ['5', '3'],
          distance_km: 10.2, distance_basis: 'mapped_channel_estimate' },
      ],
    }] }],
  } as unknown as InferenceResult

  render(<TimeSeries result={result} />)
  const note = within(screen.getByRole('list', { name: 'pH chart source and quality' })).getByRole('listitem')
  expect(note).toHaveTextContent('Approx. river distance 2.94 km (saved channel geometry) · 2 waterbody steps · Measured at USGS-123 · route 1 → 2 → 3')
  expect(note).toHaveTextContent('Approx. river distance 3.78 km (saved channel geometry) · 2 waterbody steps · Measured at USGS-123 · route 1 → 4 → 3')
  expect(note).not.toHaveTextContent('USGS-456')
})

it('applies a historical distance overlay only to matching empty anchors without changing saved content', () => {
  const saved = { series: [{ points: [{
    cell_id: 'cell:ph:1', measured_anchors: [
      { origin_comid: '1', distance_km: null, distance_basis: null },
      { origin_comid: '2', distance_km: 4.2, distance_basis: null },
    ],
  }] }] } as unknown as InferenceResult
  const displayed = withAnchorDistanceEstimates(saved, [
    { cell_id: 'cell:ph:1', anchor_index: 0, distance_km: 2.94,
      distance_basis: 'mapped_channel_estimate' },
    { cell_id: 'cell:ph:1', anchor_index: 1, distance_km: 0.1,
      distance_basis: 'mapped_channel_estimate' },
    { cell_id: 'cell:ph:1', anchor_index: 7, distance_km: 3.78,
      distance_basis: 'mapped_channel_estimate' },
    { cell_id: 'cell:other', anchor_index: 0, distance_km: 5,
      distance_basis: 'mapped_channel_estimate' },
  ])
  expect(hasMissingAnchorDistances(saved)).toBe(true)
  expect(saved.series[0]!.points[0]!.measured_anchors[0]!.distance_km).toBeNull()
  expect(displayed.series[0]!.points[0]!.measured_anchors[0]).toMatchObject({
    distance_km: 2.94, distance_basis: 'mapped_channel_estimate',
  })
  expect(displayed.series[0]!.points[0]!.measured_anchors[1]).toMatchObject({
    distance_km: 4.2, distance_basis: null,
  })
  expect(hasMissingAnchorDistances(displayed)).toBe(false)
})
