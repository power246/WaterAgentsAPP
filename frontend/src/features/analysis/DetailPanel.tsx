import { useEffect, useState } from 'react'
import { api, ApiError, type RunDetail, type RunSnapshot, type TranscriptItem } from '../../api/http'
import type { ProcessItem } from './state'
import type { InferenceResult, RunSummaryResult } from './models'

export type DetailTab = 'overview' | 'agent' | 'evidence'

const visibleRecords = new Set([
  'waterbody_agent_message', 'evidence_bundle', 'evidence_fragment',
  'evidence_tool_result', 'evidence_source_records', 'reasoning_decision',
  'inference_result', 'waterbody_dependency_manifest',
])

function safeContent(item: TranscriptItem): string {
  if (item.content.kind === 'reference') return `${item.content.preview_head} … ${item.content.preview_tail}`
  if (item.content.kind !== 'inline') return 'Saved content unavailable'
  const body = item.content.body
  if (typeof body === 'string') return body
  if (typeof body === 'object' && body !== null && !Array.isArray(body)) {
    const message = 'text' in body ? body.text : 'message' in body ? body.message : null
    if (typeof message === 'string') return message
  }
  return JSON.stringify(body, null, 2)
}

function RecordView({ entry, runId, readView }: { entry: ProcessItem; runId: string; readView: 'auto' | 'history' }) {
  const [full, setFull] = useState<string | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [controller, setController] = useState<AbortController | null>(null)
  useEffect(() => () => controller?.abort(), [controller])
  const item = entry.finalized
  const message = entry.header.content_type === 'waterbody_agent_message'
  if (!visibleRecords.has(entry.header.content_type)) return null
  const text = item ? safeContent(item) : entry.delta
  const preview = text.length > 700 ? `${text.slice(0, 700)}…` : text
  const body = <>
    <p className="record-meta">{entry.header.occurred_at} · {item?.parse_status ?? (entry.generationStatus || 'generating')}</p>
    <pre>{(full ?? preview) || 'Generating…'}</pre>
    {item?.content.kind === 'reference' && <button type="button" disabled={loading} onClick={() => {
      const request = new AbortController()
      setController(request); setLoading(true); setError('')
      void api.content(runId, item.item_id, readView, request.signal)
        .then((response) => { if (!request.signal.aborted) setFull(response.text) })
        .catch((cause: unknown) => { if (!request.signal.aborted) setError(cause instanceof ApiError ? cause.code : 'Content unavailable') })
        .finally(() => { if (!request.signal.aborted) setLoading(false) })
    }}>Read saved content</button>}
    {error && <p className="error">{error}</p>}
  </>
  return <article className="process-record" data-item-id={entry.itemId}>
    {message ? <><h4>Agent message {entry.generationStatus ? `· ${entry.generationStatus}` : '· generating'}</h4>{body}</>
      : <details><summary>{entry.header.content_type.replaceAll('_', ' ')} · {entry.header.transcript_sequence}</summary>{body}</details>}
  </article>
}

function ReadItem({ item, runId }: { item: TranscriptItem; runId: string }) {
  if (!visibleRecords.has(item.content_type)) return null
  const entry: ProcessItem = { itemId: item.item_id, header: item, finalized: item, delta: '', generationStatus: null }
  return <RecordView entry={entry} runId={runId} readView="history" />
}

export function DetailPanel({ runId, snapshot, detail, sessionId, processItems, referencedItems,
  result, summary, readView, tab, onTab, onRefresh }: {
  runId: string | null
  snapshot: RunSnapshot | null
  detail: RunDetail | null
  sessionId: string | null
  processItems: ProcessItem[]
  referencedItems: TranscriptItem[]
  result: InferenceResult | null
  summary: RunSummaryResult | null
  readView: 'auto' | 'history'
  tab: DetailTab
  onTab: (tab: DetailTab) => void
  onRefresh: () => void
}) {
  const [retryMessage, setRetryMessage] = useState('')
  const session = snapshot?.sessions.find((item) => item.session_id === sessionId)
    ?? detail?.sessions?.find((item) => item.session_id === sessionId)
  const persistence = snapshot?.persistence ?? detail?.persistence
  const status = snapshot?.status ?? detail?.run.status
  const evidence = processItems.filter((item) => item.header.content_type.startsWith('evidence_'))
  return <section className="detail-panel" aria-label="Analysis detail">
    <div className="detail-tabs" role="tablist" aria-label="Detail tabs">
      {(['overview', 'agent', 'evidence'] as const).map((name) => <button key={name}
        role="tab" aria-selected={tab === name} onClick={() => onTab(name)}>{name.charAt(0).toUpperCase() + name.slice(1)}</button>)}
    </div>
    {tab === 'overview' && <div className="detail-body">
      <h2>Overview</h2>
      {!runId && <p>Select or start an analysis.</p>}
      {runId && <>
        <p><strong>Run</strong> {runId}</p>
        <p><strong>Status</strong> {status ?? 'Loading'}{snapshot ? ` · ${snapshot.analysis_phase}` : ''}</p>
        {detail?.run.query && <p><strong>Question</strong> {detail.run.query}</p>}
        {detail?.run.context_summary && <p><strong>Target</strong> COMID {detail.run.context_summary.target_comid} · {detail.run.context_summary.date_range.start_date} – {detail.run.context_summary.date_range.end_date}</p>}
        {detail?.run.result_summary ? <>
          <p><strong>Summary</strong> {detail.run.result_summary.summary_status}</p>
          {summary && <><p>{summary.plain_language_summary}</p>
            <ul>{summary.highlights?.map((item, index) => <li key={index}>{item.text}</li>)}</ul></>}
          <p><strong>Target risk</strong> {detail.run.result_summary.target_result_summary.risk_level}</p>
          <p><strong>Graph coverage</strong> {JSON.stringify(detail.run.result_summary.graph_coverage)}</p>
          {detail.run.result_summary.summary_error && <p className="error">{detail.run.result_summary.summary_error.code}</p>}
        </> : snapshot?.summary.error ? <p className="error">Summary: {snapshot.summary.error.code}</p>
          : <p className="muted">Summary pending or unavailable.</p>}
        {snapshot?.graph_outcome.coverage && <p><strong>Graph</strong> {snapshot.graph_outcome.usable_nodes} usable · {snapshot.graph_outcome.insufficient_nodes} insufficient · {snapshot.graph_outcome.invalid_nodes} invalid</p>}
        {persistence && <div className="persistence">
          <strong>Saved state</strong> {persistence.state}
          {persistence.warning && <p className="error">{persistence.warning.code}</p>}
          {persistence.state === 'retry_exhausted' && <button type="button" onClick={() => {
            void api.retryPersistence(runId).then(() => { setRetryMessage('Retry accepted.'); onRefresh() })
              .catch((cause: unknown) => setRetryMessage(cause instanceof ApiError ? cause.code : 'Retry unavailable'))
          }}>Retry saving</button>}
          {retryMessage && <p role="status">{retryMessage}</p>}
        </div>}
      </>}
    </div>}
    {tab === 'agent' && <div className="detail-body">
      <h2>Agent</h2>
      {!session ? <p>Select a waterbody with a session.</p> : <>
        <p><strong>Session</strong> {session.session_id}</p>
        <p><strong>Status</strong> {session.status}</p>
        {session.result_summary && 'risk_level' in session.result_summary && <p><strong>Risk</strong> {session.result_summary.risk_level} · {session.result_summary.availability} · confidence {session.result_summary.overall_confidence}</p>}
        {result && <><p>{result.time_range.start} – {result.time_range.end} · {result.analysis_timezone}</p>
          <p>{result.overall_confidence_rationale}</p>
          <h3>Window assessments</h3>
          {result.window_assessments.map((item) => <div key={item.assessment_id} className="window-assessment">
            <strong>{item.start_time} – {item.end_time}</strong>
            <p>{item.availability} · {item.risk_level} · confidence {item.confidence}</p>
            <p>{item.risk_rationale}</p>
          </div>)}
        </>}
        <h3>Process</h3>
        {processItems.length ? processItems.map((item) => <RecordView key={`${readView}:${item.itemId}`} entry={item} runId={runId ?? ''} readView={readView} />)
          : <p className="muted">No visible process records.</p>}
      </>}
    </div>}
    {tab === 'evidence' && <div className="detail-body">
      <h2>Evidence</h2>
      {!session ? <p>Select a waterbody with a session.</p> : <>
        <h3>Current evidence</h3>
        {evidence.length ? evidence.map((item) => <RecordView key={`${readView}:${item.itemId}`} entry={item} runId={runId ?? ''} readView={readView} />)
          : <p className="muted">Current evidence unavailable or empty.</p>}
        <h3>Historical context</h3>
        <p className="muted">Historical records are context only and do not replace current observations.</p>
        {referencedItems.length ? referencedItems.map((item) => <ReadItem key={item.item_id} item={item} runId={runId ?? ''} />)
          : <p className="muted">No saved historical context.</p>}
      </>}
    </div>}
  </section>
}
