import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, ApiError, confirmedRequestRejected, prepareConfirmation, type AdminKind,
  type AdminOperation, type AdminRag, type AdminStorage, type CleanupPreview,
  type GenerationPreview, type ProfilePreview, type RunHistory, type Submission } from '../../api/http'

type Preview =
  | { kind: 'history_cleanup'; data: CleanupPreview }
  | { kind: 'profile_handoff'; data: ProfilePreview }
  | { kind: 'retry_failed_jobs' | 'generation_deletion'; generationId: string; data: GenerationPreview }
type Attempt = { kind: AdminKind; generationId?: string; submission: Submission }

function safeError(cause: unknown): string {
  return cause instanceof ApiError ? `${cause.code}${cause.requestId ? ` (${cause.requestId})` : ''}` : 'Service unavailable'
}

function active(operation: AdminOperation): boolean {
  return operation.state === 'pending' || operation.state === 'running'
}

export function AdminPage() {
  const [storage, setStorage] = useState<AdminStorage | null>(null)
  const [rag, setRag] = useState<AdminRag | null>(null)
  const [operations, setOperations] = useState<AdminOperation[]>([])
  const [cursor, setCursor] = useState<string | null>(null)
  const [history, setHistory] = useState<RunHistory['items']>([])
  const [historyCursor, setHistoryCursor] = useState<string | null>(null)
  const [selected, setSelected] = useState<string[]>([])
  const [allHistory, setAllHistory] = useState(false)
  const [preview, setPreview] = useState<Preview | null>(null)
  const [phrase, setPhrase] = useState('')
  const [attempt, setAttempt] = useState<Attempt | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [visible, setVisible] = useState(!document.hidden)
  const pollCount = useRef(0)

  const load = useCallback(async (signal?: AbortSignal) => {
    const results = await Promise.allSettled([
      api.adminStorage(signal), api.adminRag(signal), api.operations(undefined, signal), api.history(undefined, signal),
    ])
    if (signal?.aborted) return
    if (results[0].status === 'fulfilled') setStorage(results[0].value)
    if (results[1].status === 'fulfilled') setRag(results[1].value)
    if (results[2].status === 'fulfilled') {
      setOperations(results[2].value.items); setCursor(results[2].value.next_cursor)
    }
    if (results[3].status === 'fulfilled') {
      setHistory(results[3].value.items); setHistoryCursor(results[3].value.next_cursor ?? null)
    }
    const failure = results.find((result) => result.status === 'rejected')
    setError(failure?.status === 'rejected' ? safeError(failure.reason) : '')
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    void load(controller.signal)
    return () => controller.abort()
  }, [load])

  useEffect(() => {
    const changed = () => setVisible(!document.hidden)
    document.addEventListener('visibilitychange', changed)
    return () => document.removeEventListener('visibilitychange', changed)
  }, [])

  useEffect(() => {
    const activeIds = operations.filter(active).map((item) => item.operation_id)
    if (!visible || !activeIds.length) {
      if (!activeIds.length) pollCount.current = 0
      return
    }
    const controller = new AbortController()
    const delay = [0, 1000, 2000, 5000][Math.min(pollCount.current, 3)]
    const timer = setTimeout(() => {
      void Promise.allSettled(activeIds.map((id) => api.operation(id, controller.signal))).then((results) => {
        if (controller.signal.aborted) return
        pollCount.current++
        setOperations((current) => current.map((item) => {
          const index = activeIds.indexOf(item.operation_id)
          const result = results[index]
          return index >= 0 && result?.status === 'fulfilled' ? result.value : item
        }))
        const failure = results.find((result) => result.status === 'rejected')
        if (failure?.status === 'rejected') setError(safeError(failure.reason))
      })
    }, delay)
    return () => { clearTimeout(timer); controller.abort() }
  }, [operations, visible])

  async function prepare(request: () => Promise<Preview>) {
    if (attempt) return
    setBusy(true); setError(''); setNotice(''); setPreview(null); setPhrase('')
    try { setPreview(await request()) }
    catch (cause) { setError(safeError(cause)) }
    finally { setBusy(false) }
  }

  function historySelection(): { kind: 'all_history' } | { kind: 'runs'; run_ids: string[] } {
    return allHistory ? { kind: 'all_history' } : { kind: 'runs', run_ids: selected }
  }

  async function execute() {
    if (!preview?.data.confirmation || (!attempt && phrase !== preview.data.confirmation.required_phrase)) return
    if (!attempt && operations.some((item) => active(item) && item.kind === preview.kind)) return
    const confirmed = attempt ?? { kind: preview.kind,
      generationId: 'generationId' in preview ? preview.generationId : undefined,
      submission: prepareConfirmation(preview.data.confirmation.token, phrase) }
    setAttempt(confirmed)
    setBusy(true); setError('')
    try {
      const operation = await api.executeAdmin(confirmed.kind, confirmed.submission, confirmed.generationId)
      setOperations((current) => [operation, ...current.filter((item) => item.operation_id !== operation.operation_id)])
      setAttempt(null); setPreview(null); setPhrase('')
      setNotice(`Operation ${operation.operation_id} accepted. You may leave this page; it will continue on the server.`)
    } catch (cause) {
      if (confirmedRequestRejected(cause)) {
        setAttempt(null); setPreview(null); setPhrase('')
        setError(`${safeError(cause)}. Refresh facts and request a new preview.`)
      } else setError(`${safeError(cause)}. Response unknown; retry this confirmation with the same request.`)
    } finally { setBusy(false) }
  }

  async function moreOperations() {
    if (!cursor) return
    setBusy(true)
    try {
      const page = await api.operations(cursor)
      setOperations((current) => [...current, ...page.items.filter((item) => !current.some((old) => old.operation_id === item.operation_id))])
      setCursor(page.next_cursor)
    } catch (cause) { setError(safeError(cause)) }
    finally { setBusy(false) }
  }

  async function moreHistory() {
    if (!historyCursor) return
    setBusy(true)
    try {
      const page = await api.history(historyCursor)
      setHistory((current) => [...current, ...page.items.filter((item) => !current.some((old) => old.run_id === item.run_id))])
      setHistoryCursor(page.next_cursor ?? null)
    } catch (cause) { setError(safeError(cause)) }
    finally { setBusy(false) }
  }

  return <main className="admin-page">
    <header className="admin-header"><Link to="/">Water Agents</Link><h1>Advanced / Admin</h1>
      <button type="button" onClick={() => void load()}>Refresh facts</button></header>
    {error && <p className="error" role="alert">{error}</p>}
    {notice && <p role="status">{notice}</p>}
    <div className="admin-grid">
      <section className="admin-card"><h2>Storage</h2>
        {storage ? <ul>
          <li>History: {storage.history.state}</li><li>RAG index: {storage.rag_index.state}</li>
          <li>Credential store: {storage.credential_store.state}</li>
          <li>Evidence cache: {storage.evidence_cache.state}</li>
        </ul> : <p className="muted">Storage status unavailable.</p>}
      </section>
      <section className="admin-card"><h2>History cleanup</h2>
        <p className="muted">Select saved runs or request a bounded all-history preview. Settings, keys and unit versions are preserved.</p>
        <label><input type="checkbox" checked={allHistory} onChange={(event) => { setAllHistory(event.target.checked); setSelected([]) }} /> All saved history</label>
        {!allHistory && <div className="admin-run-list">{history.map((run) => <label key={run.run_id}>
          <input type="checkbox" checked={selected.includes(run.run_id)} onChange={(event) => setSelected((current) =>
            event.target.checked ? [...current, run.run_id] : current.filter((id) => id !== run.run_id))} />
          {run.query} · {run.run_id}
        </label>)}</div>}
        {!allHistory && historyCursor && <button type="button" disabled={busy} onClick={() => void moreHistory()}>Load more saved runs</button>}
        <button type="button" disabled={busy || Boolean(attempt) || (!allHistory && !selected.length)} onClick={() => void prepare(async () => ({
          kind: 'history_cleanup', data: await api.previewHistoryCleanup(historySelection(), false),
        }))}>Preview history cleanup</button>
      </section>
      <section className="admin-card"><h2>RAG profile handoff</h2>
        {rag ? <><p>Desired profile: {rag.handoff.desired_profile_digest}</p>
          <p>Phase: {rag.handoff.phase} · active {rag.handoff.active_generation_id ?? 'none'} · building {rag.handoff.building_generation_id ?? 'none'}</p>
          <p>Blocking readers: {rag.handoff.blocking_reader_count}; credential cleanup ready: {String(rag.handoff.old_credential_cleanup_ready)}</p></>
          : <p className="muted">RAG status unavailable.</p>}
        <button type="button" disabled={busy || Boolean(attempt) || !rag} onClick={() => void prepare(async () => ({
          kind: 'profile_handoff', data: await api.previewProfileHandoff(),
        }))}>Preview profile handoff</button>
      </section>
      <section className="admin-card"><h2>RAG generations</h2>
        {rag?.index.generations.map((generation) => <div className="generation-row" key={generation.generation_id}>
          <h3>{generation.generation_id}</h3>
          <p>{generation.status} · profile {generation.profile_digest} · readers {generation.reader_count}</p>
          <p>Jobs: {generation.job_counts.completed} completed, {generation.job_counts.failed} failed,
            {generation.job_counts.pending} pending, {generation.job_counts.retry_wait} retry wait</p>
          {generation.status === 'building' && generation.job_counts.failed > 0 && generation.last_safe_error_code &&
            <button type="button" disabled={busy || Boolean(attempt)} onClick={() => void prepare(async () => ({
              kind: 'retry_failed_jobs', generationId: generation.generation_id,
              data: await api.previewFailedJobs(generation.generation_id, [generation.last_safe_error_code!], 25),
            }))}>Preview failed jobs retry</button>}
          {(generation.status === 'retired' || generation.status === 'failed') &&
            <button type="button" disabled={busy || Boolean(attempt)} onClick={() => void prepare(async () => ({
              kind: 'generation_deletion', generationId: generation.generation_id,
              data: await api.previewGenerationDeletion(generation.generation_id),
            }))}>Preview inactive generation deletion</button>}
        </div>)}
      </section>
      {preview && <section className="admin-card admin-preview"><h2>Server preview: {preview.kind}</h2>
        {preview.kind === 'history_cleanup' && <>
          <p>{preview.data.estimated.runs} runs, {preview.data.estimated.sessions} sessions,
            {preview.data.estimated.transcript_items} transcript items; {preview.data.estimated.retained_idempotency_tombstones} tombstones retained.</p>
          <p>Dependent links: {preview.data.dependencies.length}; cascade required: {String(preview.data.cascade_required)}</p>
          <p>Delete order: {preview.data.delete_order.join(', ') || 'none'}</p>
          <p>Preserved: {preview.data.preserved.join(', ')}</p>
          {preview.data.cascade_required && !preview.data.cascade_authorized && <button type="button" disabled={busy || Boolean(attempt)}
            onClick={() => void prepare(async () => ({ kind: 'history_cleanup',
              data: await api.previewHistoryCleanup(historySelection(), true) }))}>Preview whole-run cascade</button>}
        </>}
        {preview.kind === 'profile_handoff' && <><p>Estimated jobs: {preview.data.estimated_jobs};
          blocking readers: {preview.data.handoff.blocking_reader_count}</p>
          {preview.data.provider_usage_warning && <p className="error">This action may use an external provider.</p>}</>}
        {(preview.kind === 'retry_failed_jobs' || preview.kind === 'generation_deletion') &&
          <p>Generation {preview.data.generation_id}; affected jobs: {preview.data.affected_jobs}.
            Active readers and retention are checked by the server.</p>}
        {preview.data.confirmation ? <>
          <p>Enter <strong>{preview.data.confirmation.required_phrase}</strong> to confirm.</p>
          <input aria-label="Admin confirmation phrase" value={phrase} disabled={Boolean(attempt)} onChange={(event) => setPhrase(event.target.value)} />
          <button type="button" disabled={busy || phrase !== preview.data.confirmation.required_phrase ||
            (!attempt && operations.some((item) => active(item) && item.kind === preview.kind))}
            onClick={() => void execute()}>{attempt ? 'Retry confirmed operation' : 'Execute confirmed operation'}</button>
        </> : <p className="muted">No executable confirmation for these current facts.</p>}
      </section>}
      <section className="admin-card admin-operations"><h2>Active and recent operations</h2>
        {operations.map((item) => <article key={item.operation_id} className="operation-row">
          <strong>{item.kind} · {item.state}</strong><p>{item.operation_id} · {item.resource_identity}</p>
          <p>{item.progress.processed_items}/{item.progress.total_items ?? '?'} processed;
            {item.progress.succeeded_items} succeeded, {item.progress.failed_items} failed.</p>
          {item.safe_error && <p className="error">{item.safe_error.code}</p>}
          {item.safe_result && <p>Result: {JSON.stringify(item.safe_result)}</p>}
        </article>)}
        {!operations.length && <p className="muted">No recent operations.</p>}
        {cursor && <button type="button" disabled={busy} onClick={() => void moreOperations()}>Load more operations</button>}
      </section>
    </div>
  </main>
}
