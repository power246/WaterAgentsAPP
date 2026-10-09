import { useCallback, useEffect, useState } from 'react'
import { api, ApiError, type RunHistory } from '../../api/http'
import { originalTimezone } from '../analysis/models'

type HistoryItem = NonNullable<RunHistory['items']>[number]

export function HistoryList({ selectedRunId, onSelect, refreshVersion }: {
  selectedRunId: string | null
  onSelect: (runId: string) => void
  refreshVersion: number
}) {
  const [items, setItems] = useState<HistoryItem[]>([])
  const [cursor, setCursor] = useState<string | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  const load = useCallback(async (next: string | null, signal?: AbortSignal) => {
    setLoading(true)
    setError('')
    try {
      const page = await api.history(next ?? undefined, signal)
      setItems((current) => next === null ? page.items ?? [] : [...current, ...(page.items ?? [])])
      setCursor(page.next_cursor ?? null)
    } catch (cause) {
      if (signal?.aborted) return
      setError(cause instanceof ApiError ? cause.code : 'History unavailable')
    } finally {
      if (!signal?.aborted) setLoading(false)
    }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    void load(null, controller.signal)
    return () => controller.abort()
  }, [refreshVersion, load])

  return <section className="history-list" aria-label="Run history">
    <div className="panel-title"><h2>History</h2><button type="button" onClick={() => void load(null)} disabled={loading}>Refresh</button></div>
    {error && <p className="error" role="alert">{error === 'invalid_history_cursor' ? 'History cursor is no longer valid. Refresh the first page.' : error}</p>}
    <div className="history-items">{items.map((item) => <button key={item.run_id} type="button"
      className={selectedRunId === item.run_id ? 'selected' : ''} onClick={() => onSelect(item.run_id)}>
      <strong>{item.query}</strong>
      <span>{item.status} · {item.created_at}</span>
      <span>COMID {item.context_summary?.target_comid ?? 'unresolved'} · {originalTimezone(item.configuration)}</span>
      {item.context_summary && <span>{item.context_summary.date_range.start_date} – {item.context_summary.date_range.end_date} · {item.context_summary.mode}</span>}
      {item.result_summary && <span>Confidence {item.result_summary.target_result_summary.overall_confidence}</span>}
    </button>)}</div>
    {!items.length && !loading && !error && <p className="muted">No saved runs.</p>}
    {cursor && <button type="button" disabled={loading} onClick={() => void load(cursor)}>Load more</button>}
  </section>
}
