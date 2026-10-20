import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import type { InferenceResult } from './models'

type Point = InferenceResult['series'][number]['points'][number]

export function isPhParameter(key: { code_system: string; code: string }): boolean {
  return key.code_system === 'usgs' && (key.code === '00400' || key.code === '00403')
}

export function phAxisDomain(values: (number | null)[]): [number, number] | undefined {
  const finite = values.filter((value): value is number => value !== null && Number.isFinite(value))
  if (!finite.length) return undefined
  const low = Math.min(...finite)
  const high = Math.max(...finite)
  const padding = Math.max((high - low) * 0.2, 0.25)
  return [low >= 0 ? Math.max(0, low - padding) : low - padding,
    high <= 14 ? Math.min(14, high + padding) : high + padding]
}

export function windowTickFormatter(starts: string[], timeZone: string): (value: string) => string {
  const day = new Intl.DateTimeFormat('en-US', { timeZone, year: 'numeric', month: '2-digit', day: '2-digit' })
  const shortDay = new Intl.DateTimeFormat('en-US', { timeZone, month: 'numeric', day: 'numeric' })
  const time = new Intl.DateTimeFormat('en-GB', { timeZone, hour: '2-digit', minute: '2-digit', hourCycle: 'h23' })
  const showDay = new Set(starts.map((start) => day.format(new Date(start)))).size > 1
  return (value) => {
    const instant = new Date(value)
    return `${showDay ? `${shortDay.format(instant)} ` : ''}${time.format(instant)}`
  }
}

function temporalEstimateMethod(point: Point): string | null {
  return point.temporal_estimate_method ?? null
}

function isTemporalEstimate(point: Point): boolean {
  return temporalEstimateMethod(point) !== null
}

function isWithinWindowValue(point: Point): boolean {
  return point.inference_method !== 'model_estimate' && !isTemporalEstimate(point) &&
    (point.source === 'observed' || (point.source === 'inferred' && point.aggregation_method !== null))
}

function basisLabel(point: Point): string {
  if (point.outcome_status === 'range_estimate') return point.inference_basis === 'same_parameter_measurement'
    ? 'Range prediction' : 'Low-basis range prediction'
  if (point.inference_method === 'model_estimate') return 'Model estimate'
  const method = temporalEstimateMethod(point)
  if (method) return `Estimated empty window (${method.replaceAll('_', ' ')})`
  if (point.source === 'observed') return 'Reading in window'
  if (point.aggregation_method) return `Within-window ${point.aggregation_method} of available readings`
  if (point.source === 'predicted') return 'Predicted value'
  return point.source === 'inferred' ? 'Other inferred value' : 'No value'
}

function rangeLabel(point: Point): string | null {
  return point.value_lower != null && point.value_upper != null
    ? `${point.value_lower} – ${point.value_upper}` : null
}

function supportLabel(point: Point): string | null {
  if (point.inference_basis === 'related_evidence') return 'Related readings or local information'
  if (point.inference_basis === 'general_knowledge') return 'General knowledge; local evidence unavailable'
  if (point.inference_basis === 'same_parameter_measurement') return 'Traceable same-parameter measurements'
  return null
}

function anchorLabel(point: Point): string | null {
  type Anchor = NonNullable<Point['measured_anchors']>[number] & { distance_basis?: 'mapped_channel_estimate' | null }
  const anchors: Anchor[] = point.measured_anchors ?? []
  if (!anchors.length) return point.inference_method === 'model_estimate' || point.outcome_status === 'range_estimate'
    ? 'No same-parameter measured anchor' : null
  const nearest = [...anchors].sort((left, right) =>
    (left.distance_km ?? Infinity) - (right.distance_km ?? Infinity) || left.path_comids.length - right.path_comids.length)[0]
  if (!nearest) return null
  const routes = anchors.filter((anchor) => anchor.station_id === nearest.station_id &&
    anchor.origin_comid === nearest.origin_comid).sort((left, right) =>
    (left.distance_km ?? Infinity) - (right.distance_km ?? Infinity) || left.path_comids.length - right.path_comids.length)
  return [...new Set(routes.map((anchor) => {
    const distance = anchor.distance_km == null ? 'Distance unknown'
      : anchor.distance_basis === 'mapped_channel_estimate'
        ? `Approx. river distance ${Number(anchor.distance_km.toFixed(2))} km (saved channel geometry)`
        : `Recorded distance ${anchor.distance_km} km (method unspecified)`
    return `${distance} · ${anchor.path_comids.length - 1} waterbody steps · Measured at ${anchor.station_id ?? `COMID ${anchor.origin_comid}`} · route ${anchor.path_comids.join(' → ')}`
  }))].join('; ')
}

function qualityLabel(point: Point): string {
  return point.quality_flags.map((flag) =>
    flag === 'temporal_estimate' && !isTemporalEstimate(point)
      ? 'temporal_estimate (supporting evidence)' : flag).join(', ') || 'none'
}

export function TimeSeries({ result }: { result: InferenceResult | null }) {
  if (!result) return <p className="muted">No saved time series for this waterbody.</p>
  return <section className="time-series" aria-label="Saved time series">
    <h3>Time series</h3>
    <p className="muted">{result.analysis_timezone} · {result.time_resolution} · {result.mode}</p>
    <p className="muted">A value from readings within a window describes the available samples. It does not prove measurements covered the entire window. An estimated empty window has no reading in that window. Quality flags may also describe supporting data such as flow.</p>
    <p className="muted">Possible ranges describe the estimate's uncertainty. They are not calibrated confidence intervals, and confidence scores are not probabilities of accuracy.</p>
    {result.series.map((series) => {
      const data = series.points.map((point) => ({
        window: point.start_time,
        withinWindowValue: point.representative_value !== null && isWithinWindowValue(point)
          ? Number(point.representative_value) : null,
        estimatedValue: point.representative_value !== null && isTemporalEstimate(point)
          ? Number(point.representative_value) : null,
        otherValue: point.representative_value !== null && !isTemporalEstimate(point) && !isWithinWindowValue(point)
          ? Number(point.representative_value) : null,
        rangeLower: point.value_lower != null ? Number(point.value_lower) : null,
        rangeUpper: point.value_upper != null ? Number(point.value_upper) : null,
        unit: point.unit_id,
      }))
      const pHDomain = isPhParameter(series.parameter.key)
        ? phAxisDomain(data.flatMap(({ withinWindowValue, estimatedValue, otherValue, rangeLower, rangeUpper }) =>
          [withinWindowValue, estimatedValue, otherValue, rangeLower, rangeUpper])) : undefined
      const formatWindow = windowTickFormatter(series.points.map((point) => point.start_time), result.analysis_timezone)
      const annotatedPoints = series.points.filter((point) => (point.representative_value !== null || rangeLabel(point) !== null) &&
        (point.source !== 'observed' || point.quality_flags.length > 0))
      return <div key={`${series.parameter.key.code_system}:${series.parameter.key.code}`}>
        <h4>{series.parameter.name}</h4>
        <p className="time-series-key"><span className="within-window-key">● Reading or sample within window</span><span className="estimated-window-key">○ Estimated empty window</span><span className="other-value-key">● Other inferred or predicted value</span><span className="range-key">◆ Possible range limits</span></p>
        <div className="chart-frame" role="img" aria-label={`${series.parameter.name} saved values by window`}>
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={data} margin={{ top: 8, right: 16, bottom: 8, left: 0 }}>
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis dataKey="window" tickFormatter={formatWindow} />
              <YAxis domain={pHDomain} />
              <Tooltip formatter={(value, name, item) => [`${String(value)} ${item.payload?.unit ?? ''}`, name]} />
              <Line type="linear" name="Reading or sample within window" dataKey="withinWindowValue" stroke="#1234e1" dot connectNulls={false} isAnimationActive={false} />
              <Line type="linear" name="Estimated empty window" dataKey="estimatedValue" stroke="#b45b13" strokeDasharray="5 4" dot={{ r: 5, fill: '#fff', stroke: '#b45b13', strokeWidth: 2 }} connectNulls={false} isAnimationActive={false} />
              <Line type="linear" name="Other inferred or predicted value" dataKey="otherValue" stroke="#6d28d9" strokeWidth={2.5} dot={{ r: 4, fill: '#6d28d9', stroke: '#fff', strokeWidth: 1.5 }} connectNulls={false} isAnimationActive={false} />
              <Line type="linear" name="Possible range lower limit" dataKey="rangeLower" stroke="#0f766e" strokeWidth={2} strokeDasharray="5 3" dot={{ r: 3, fill: '#0f766e' }} connectNulls={false} isAnimationActive={false} />
              <Line type="linear" name="Possible range upper limit" dataKey="rangeUpper" stroke="#0f766e" strokeWidth={2} strokeDasharray="5 3" dot={{ r: 3, fill: '#0f766e' }} connectNulls={false} isAnimationActive={false} />
            </LineChart>
          </ResponsiveContainer>
        </div>
        {annotatedPoints.length > 0 &&
          <ul className="time-series-chart-notes" aria-label={`${series.parameter.name} chart source and quality`}>
            {annotatedPoints.map((point) =>
              <li key={point.cell_id} className={isTemporalEstimate(point) ? 'estimated-window-note' : undefined}>
                {point.start_time} · {point.representative_value ?? rangeLabel(point)} {point.unit_id} · {basisLabel(point)} · Confidence: {point.confidence}
                {point.representative_value !== null && rangeLabel(point) && ` · Possible range: ${rangeLabel(point)} ${point.unit_id}`}
                {(isTemporalEstimate(point) || point.inference_method === 'model_estimate') && ` · ${point.confidence_rationale}`}
                {supportLabel(point) && ` · ${supportLabel(point)}`}
                {anchorLabel(point) && ` · ${anchorLabel(point)}`} · Quality: {qualityLabel(point)}
              </li>)}
          </ul>}
        <details><summary>Windows and quality</summary>
          <table><thead><tr><th>Window</th><th>Value</th><th>Possible range</th><th>Unit</th><th>Basis</th><th>Measured anchor</th><th>Availability</th><th>Confidence</th><th>Reason</th><th>Quality</th><th>Evidence</th></tr></thead><tbody>
            {series.points.map((point) => <tr key={point.cell_id}>
              <td>{point.start_time} – {point.end_time}</td>
              <td>{point.representative_value ?? (rangeLabel(point) ? 'Range only' : 'unavailable')}</td>
              <td>{rangeLabel(point) ?? '—'}</td>
              <td>{point.unit_id ?? '—'}</td>
              <td>{basisLabel(point)}{supportLabel(point) && ` · ${supportLabel(point)}`}</td>
              <td>{anchorLabel(point) ?? '—'}</td>
              <td>{point.outcome_status}</td>
              <td>{point.confidence}</td>
              <td>{point.confidence_rationale}</td>
              <td>{qualityLabel(point)}</td>
              <td>{point.evidence_refs.join(', ')}</td>
            </tr>)}
          </tbody></table>
        </details>
      </div>
    })}
  </section>
}
