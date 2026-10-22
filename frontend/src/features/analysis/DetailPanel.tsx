import { useEffect, useMemo, useState } from 'react'
import { api, ApiError, type RunDetail, type RunSnapshot, type TranscriptItem } from '../../api/http'
import type { ProcessItem } from './state'
import type { Graph, InferenceResult, RunSummaryResult } from './models'
import { guidelineRecords, RunGuidelines } from '../guidelines/RunGuidelines'

export type DetailTab = 'overview' | 'agent' | 'evidence'
type TranscriptRead = { status: 'loading' | 'ready' | 'error'; error: string | null }

const visibleRecords = new Set([
  'waterbody_agent_message', 'evidence_bundle', 'evidence_fragment',
  'evidence_tool_result', 'evidence_source_records', 'reasoning_decision',
  'inference_result', 'waterbody_dependency_manifest', 'numeric_dependency_plan',
  ...guidelineRecords,
])

function objectValue(value: unknown): Record<string, unknown> | null {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown> : null
}

function numericDerivationSummary(value: unknown): string | null {
  let body: unknown = value
  if (typeof body === 'string') {
    try { body = JSON.parse(body) as unknown } catch { return null }
  }
  const fragment = objectValue(body)
  const payload = objectValue(fragment?.payload)
  if (payload?.payload_type !== 'numeric_derived' || typeof payload.value !== 'string' ||
      typeof payload.unit !== 'string' || typeof payload.method_id !== 'string' ||
      typeof payload.method_version !== 'number') return null
  const parameter = Array.isArray(fragment?.parameter_refs)
    ? objectValue(fragment.parameter_refs[0]) : null
  const key = typeof parameter?.code === 'string' ? ` (${parameter.code})` : ''
  const inputs = Array.isArray(payload.input_evidence_refs)
    ? payload.input_evidence_refs.map((item) => objectValue(item)?.evidence_id)
      .filter((item): item is string => typeof item === 'string') : []
  return `Calculated value${key}: ${payload.value} ${payload.unit}; method ${payload.method_id} v${payload.method_version}; input evidence: ${inputs.join(', ') || 'unavailable'}`
}

function branchPathKey(outcome: Graph['branch_outcomes'][number]): string {
  return [outcome.branch_root_comid, outcome.path_to_target.join('>'),
    outcome.blocked_upstream_comid ?? ''].join(':')
}

function branchCount(graph: Graph, status: Graph['branch_outcomes'][number]['outcome']): number {
  return new Set((graph.branch_outcomes ?? []).filter((item) => item.outcome === status)
    .map(branchPathKey)).size
}

function branchReasons(graph: Graph, status: Graph['branch_outcomes'][number]['outcome']): string {
  const names: Record<string, string> = {
    max_distance_reached: 'distance limit', max_hops_reached: 'hop limit',
    max_nodes_reached: 'node limit', multiple_limits_reached: 'multiple limits',
    natural_headwater: 'headwater',
  }
  return [...new Set((graph.branch_outcomes ?? []).filter((item) => item.outcome === status)
    .map((item) => names[item.reason] ?? item.reason.replaceAll('_', ' ')))].join(', ')
}

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
  const [expanded, setExpanded] = useState(false)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [controller, setController] = useState<AbortController | null>(null)
  useEffect(() => () => controller?.abort(), [controller])
  const item = entry.finalized
  const message = entry.header.content_type === 'waterbody_agent_message'
  if (!visibleRecords.has(entry.header.content_type)) return null
  if (guidelineRecords.has(entry.header.content_type)) return null
  const savedMessageStatus = item?.content.kind === 'inline'
    ? objectValue(item.content.body)?.generation_status : null
  const messageStatus = entry.generationStatus ?? (typeof savedMessageStatus === 'string'
    ? savedMessageStatus : item ? 'saved' : 'generating')
  const text = item ? safeContent(item) : entry.delta
  const summary = entry.header.content_type === 'evidence_fragment'
    ? numericDerivationSummary(full ?? (item?.content.kind === 'inline' ? item.content.body : null))
    : null
  const preview = text.length > 700 ? `${text.slice(0, 700)}…` : text
  const body = <>
    <p className="record-meta">{entry.header.occurred_at} · {item?.parse_status ?? (entry.generationStatus || 'generating')}</p>
    {summary && <p>{summary}</p>}
    <pre>{(full ?? (expanded ? text : preview)) || 'Generating…'}</pre>
    {item?.content.kind === 'inline' && text.length > 700 &&
      <button type="button" onClick={() => setExpanded((value) => !value)}>
        {expanded ? 'Show less' : 'Read full inline content'}
      </button>}
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
    {message ? <><h4>Agent message · {messageStatus}</h4>{body}</>
      : <details><summary>{entry.header.content_type.replaceAll('_', ' ')} · {entry.header.transcript_sequence}</summary>{body}</details>}
  </article>
}

function ReadItem({ item, runId }: { item: TranscriptItem; runId: string }) {
  if (!visibleRecords.has(item.content_type)) return null
  const entry: ProcessItem = { itemId: item.item_id, header: item, finalized: item, delta: '', generationStatus: null }
  return <RecordView entry={entry} runId={runId} readView="history" />
}

export function DetailPanel({ runId, snapshot, detail, graph, sessionId, processItems, referencedItems,
  result, summary, readView, transcriptRead = { status: 'ready', error: null }, onShowTimeSeries,
  tab, onTab, onRefresh }: {
  runId: string | null
  snapshot: RunSnapshot | null
  detail: RunDetail | null
  graph: Graph | null
  sessionId: string | null
  processItems: ProcessItem[]
  referencedItems: TranscriptItem[]
  result: InferenceResult | null
  summary: RunSummaryResult | null
  readView: 'auto' | 'history'
  transcriptRead?: TranscriptRead
  onShowTimeSeries?: () => void
  tab: DetailTab
  onTab: (tab: DetailTab) => void
  onRefresh: () => void
}) {
  const [retryMessage, setRetryMessage] = useState('')
  const guidelineItems = useMemo(() => [...new Map([...processItems.flatMap((p) => p.finalized ? [p.finalized] : []),
    ...referencedItems, ...(detail?.referenced_items ?? []), ...(detail?.items ?? [])]
    .filter((i) => guidelineRecords.has(i.content_type)).map((i) => [i.item_id, i])).values()], [processItems, referencedItems, detail])
  const historicalItems = referencedItems.filter((i) => !guidelineRecords.has(i.content_type))
  const guidelines = runId && sessionId ? <RunGuidelines items={guidelineItems} runId={runId} sessionId={sessionId}
    result={result} readView={readView} /> : null
  const session = snapshot?.sessions.find((item) => item.session_id === sessionId)
    ?? detail?.sessions?.find((item) => item.session_id === sessionId)
  const persistence = snapshot?.persistence ?? detail?.persistence
  const status = snapshot?.status ?? detail?.run.status
  const counts = detail?.run.result_summary?.outcome_counts ?? (snapshot?.graph_outcome ? {
    usable: snapshot.graph_outcome.usable_nodes,
    insufficient: snapshot.graph_outcome.insufficient_nodes,
    invalid: snapshot.graph_outcome.invalid_nodes,
    cancelled: snapshot.graph_outcome.cancelled_nodes,
  } : null)
  const resultCount = counts ? counts.usable + counts.insufficient + counts.invalid + counts.cancelled : 0
  const coverage = detail?.run.result_summary?.graph_coverage ?? snapshot?.graph_outcome.coverage ?? graph?.coverage
  const noSourcePaths = graph ? branchCount(graph, 'no_qualifying_source') : 0
  const limitedPaths = graph ? branchCount(graph, 'search_limit_reached') : 0
  const backupPaths = graph ? branchCount(graph, 'backup_search_ended') : 0
  const hasCoverageDetails = noSourcePaths + limitedPaths + backupPaths + (counts?.invalid ?? 0) + (counts?.cancelled ?? 0) > 0
  const evidence = processItems.filter((item) =>
    item.header.content_type.startsWith('evidence_') || item.header.content_type === 'numeric_dependency_plan')
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
        {session && result && onShowTimeSeries && <button type="button" onClick={onShowTimeSeries}>View selected waterbody time series</button>}
        {detail?.run.result_summary ? <>
          <p><strong>Summary</strong> {detail.run.result_summary.summary_status}</p>
          {summary && <><p>{summary.plain_language_summary}</p>
            <ul>{summary.highlights?.map((item, index) => <li key={index}>{item.text}</li>)}</ul></>}
          <p><strong>Target risk</strong> {detail.run.result_summary.target_result_summary.risk_level}</p>
          {detail.run.result_summary.summary_error && <p className="error">{detail.run.result_summary.summary_error.code}</p>}
        </> : snapshot?.summary.error ? <p className="error">Summary: {snapshot.summary.error.code}</p>
          : <p className="muted">Summary pending or unavailable.</p>}
        {counts && resultCount > 0 && <p><strong>Waterbody results</strong> {counts.usable}/{resultCount} usable · {counts.insufficient} insufficient · {counts.invalid} invalid · {counts.cancelled} cancelled</p>}
        {coverage && <>
          <p><strong>Graph coverage</strong> {coverage} · upstream source search and result validity</p>
          {coverage === 'partial' && !graph && <p className="muted">Branch details unavailable.</p>}
          {hasCoverageDetails &&
            <ul aria-label="Graph coverage details">
              {graph && noSourcePaths > 0 &&
                <li>{noSourcePaths} upstream branch {noSourcePaths === 1 ? 'path has' : 'paths have'} no qualifying source ({branchReasons(graph, 'no_qualifying_source')}) for at least one required window.</li>}
              {graph && limitedPaths > 0 &&
                <li>{limitedPaths} required branch {limitedPaths === 1 ? 'path' : 'paths'} reached a search limit ({branchReasons(graph, 'search_limit_reached')}).</li>}
              {graph && backupPaths > 0 &&
                <li>Backup search ended ({branchReasons(graph, 'backup_search_ended')}); a qualifying source was already found for those windows.</li>}
              {counts && counts.invalid > 0 && <li>Invalid waterbody results: {counts.invalid}.</li>}
              {counts && counts.cancelled > 0 && <li>Cancelled waterbody results: {counts.cancelled}.</li>}
            </ul>}
        </>}
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
        {result && onShowTimeSeries && <button type="button" onClick={onShowTimeSeries}>View saved time series and measured anchors</button>}
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
        {guidelines}
        {transcriptRead.status === 'error' && <p className="error" role="alert">Process records could not be loaded: {transcriptRead.error}</p>}
        {processItems.length ? processItems.map((item) => <RecordView key={`${readView}:${item.itemId}`} entry={item} runId={runId ?? ''} readView={readView} />)
          : transcriptRead.status === 'loading' ? <p className="muted">Loading process records…</p>
            : transcriptRead.status === 'error' ? null
              : <p className="muted">No visible process records.</p>}
      </>}
    </div>}
    {tab === 'evidence' && <div className="detail-body">
      <h2>Evidence</h2>
      {!session ? <p>Select a waterbody with a session.</p> : <>
        <h3>Current evidence</h3>
        {transcriptRead.status === 'error' && <p className="error" role="alert">Current evidence could not be loaded: {transcriptRead.error}</p>}
        {evidence.length ? evidence.map((item) => <RecordView key={`${readView}:${item.itemId}`} entry={item} runId={runId ?? ''} readView={readView} />)
          : transcriptRead.status === 'loading' ? <p className="muted">Loading current evidence…</p>
            : transcriptRead.status === 'error' ? null
              : <p className="muted">No current evidence records.</p>}
        {guidelines}
        <h3>Historical context</h3>
        <p className="muted">Historical records are context only and do not replace current observations.</p>
        {historicalItems.length ? historicalItems.map((item) => <ReadItem key={item.item_id} item={item} runId={runId ?? ''} />)
          : transcriptRead.status === 'loading' ? <p className="muted">Loading historical context…</p>
            : transcriptRead.status === 'error' ? <p className="muted">Historical context could not be loaded.</p>
              : <p className="muted">No saved historical context.</p>}
      </>}
    </div>}
  </section>
}
