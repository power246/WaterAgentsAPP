import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import type { InferenceResult } from './models'

export function TimeSeries({ result }: { result: InferenceResult | null }) {
  if (!result) return <p className="muted">No saved time series for this waterbody.</p>
  return <section className="time-series" aria-label="Saved time series">
    <h3>Time series</h3>
    <p className="muted">{result.analysis_timezone} · {result.time_resolution} · {result.mode}</p>
    {result.series.map((series) => {
      const data = series.points.map((point) => ({
        window: point.start_time,
        value: point.representative_value === null ? null : Number(point.representative_value),
        unit: point.unit_id,
        quality: point.quality_flags.join(', ') || 'none',
        availability: point.outcome_status,
        end: point.end_time,
      }))
      return <div key={`${series.parameter.key.code_system}:${series.parameter.key.code}`}>
        <h4>{series.parameter.name}</h4>
        <div className="chart-frame" role="img" aria-label={`${series.parameter.name} saved values by window`}>
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={data} margin={{ top: 8, right: 16, bottom: 8, left: 0 }}>
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis dataKey="window" tickFormatter={(value: string) => value.slice(0, 10)} />
              <YAxis />
              <Tooltip formatter={(value) => [String(value), data.find((item) => item.value === value)?.unit ?? '']} />
              <Line type="linear" dataKey="value" stroke="#2476b7" dot connectNulls={false} isAnimationActive={false} />
            </LineChart>
          </ResponsiveContainer>
        </div>
        <details><summary>Windows and quality</summary>
          <table><thead><tr><th>Window</th><th>Value</th><th>Unit</th><th>Availability</th><th>Quality</th></tr></thead><tbody>
            {series.points.map((point) => <tr key={point.cell_id}>
              <td>{point.start_time} – {point.end_time}</td>
              <td>{point.representative_value ?? 'unavailable'}</td>
              <td>{point.unit_id ?? '—'}</td>
              <td>{point.outcome_status}</td>
              <td>{point.quality_flags.join(', ') || 'none'}</td>
            </tr>)}
          </tbody></table>
        </details>
      </div>
    })}
  </section>
}
