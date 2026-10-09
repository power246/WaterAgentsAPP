import { useCallback, useEffect, useReducer, useRef, useState } from 'react'
import { BrowserRouter, Link, Route, Routes, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { api, ApiError, type Settings, type TranscriptItem } from '../api/http'
import { AdminPage } from '../features/admin/AdminPage'
import { subscribe } from '../api/sse'
import { DetailPanel, type DetailTab } from '../features/analysis/DetailPanel'
import { NewAnalysisForm } from '../features/analysis/NewAnalysisForm'
import { TopologyMap } from '../features/analysis/TopologyMap'
import { TimeSeries } from '../features/analysis/TimeSeries'
import { graphFromDetail, graphFromText, inferenceFromText, summaryFromText,
  type Graph, type InferenceResult, type RunSummaryResult } from '../features/analysis/models'
import { emptyRun, emptyTranscript, runReducer, transcriptReducer } from '../features/analysis/state'
import { DeleteRun } from '../features/history/DeleteRun'
import { HistoryList } from '../features/history/HistoryList'
import { SettingsDrawer } from '../features/settings/SettingsDrawer'

type Mode = 'home' | 'history' | 'run'

function Workspace({ mode }: { mode: Mode }) {
  const params = useParams()
  const navigate = useNavigate()
  const [search, setSearch] = useSearchParams()
  const historyRunId = mode === 'history' ? search.get('run') : null
  const runId = mode === 'run' ? params.runId ?? null : historyRunId
  const sessionId = mode === 'run' ? params.sessionId ?? null : search.get('session')
  const [settings, setSettings] = useState<Settings | null>(null)
  const [settingsError, setSettingsError] = useState('')
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [run, dispatchRun] = useReducer(runReducer, emptyRun)
  const [transcript, dispatchTranscript] = useReducer(transcriptReducer, emptyTranscript)
  const [referencedItems, setReferencedItems] = useState<TranscriptItem[]>([])
  const [result, setResult] = useState<InferenceResult | null>(null)
  const [summary, setSummary] = useState<RunSummaryResult | null>(null)
  const [tab, setTab] = useState<DetailTab>('overview')
  const [mobileTab, setMobileTab] = useState<'controls' | 'map' | 'detail'>('controls')
  const [runError, setRunError] = useState('')
  const [historyFallback, setHistoryFallback] = useState<{ runId: string; url: string } | null>(null)
  const runStream = useRef<AbortController | null>(null)
  const [selectedComid, setSelectedComid] = useState<string | null>(null)
  const [mapPoint, setMapPoint] = useState<{ latitude: number; longitude: number } | null>(null)
  const [loadedTopology, setLoadedTopology] = useState<{ key: string; graph: Graph | null; error: string | null } | null>(null)
  const [deleteForRunId, setDeleteForRunId] = useState<string | null>(null)
  const [historyVersion, setHistoryVersion] = useState(0)
  const historyUrl = historyFallback?.runId === runId ? historyFallback.url : null
  const historical = mode === 'history' || historyUrl !== null
  const sessionTabOpen = tab === 'agent' || tab === 'evidence'
  const currentDetail = run.detail?.run.run_id === runId ? run.detail : null
  const topologyItem = currentDetail?.items.find((item) => item.content_type === 'topology_ready')
  const topologyRef = topologyItem?.content.kind === 'reference' ? topologyItem.content_ref : null
  const topologyRunId = topologyRef?.run_id
  const topologyItemId = topologyRef?.item_id
  const topologyView = historical ? 'history' : 'auto'
  const topologyKey = topologyRunId && topologyItemId ? `${topologyRunId}:${topologyItemId}:${topologyView}` : null

  useEffect(() => { setDeleteForRunId(null) }, [runId])

  useEffect(() => {
    if (!runId || !topologyRunId || !topologyItemId || topologyRunId !== runId || !topologyKey) return
    const controller = new AbortController()
    void api.content(topologyRunId, topologyItemId, topologyView, controller.signal)
      .then((content) => {
        if (controller.signal.aborted) return
        const graph = content.run_id === topologyRunId && content.item_id === topologyItemId
          ? graphFromText(content.text) : null
        setLoadedTopology({ key: topologyKey, graph, error: graph ? null : 'invalid_topology_content' })
      }).catch((cause: unknown) => {
        if (!controller.signal.aborted) setLoadedTopology({ key: topologyKey, graph: null,
          error: cause instanceof ApiError ? cause.code : 'topology_unavailable' })
      })
    return () => controller.abort()
  }, [runId, topologyRunId, topologyItemId, topologyView, topologyKey])

  useEffect(() => {
    const controller = new AbortController()
    void api.settings(controller.signal).then(setSettings).catch((cause: unknown) => {
      if (!controller.signal.aborted) setSettingsError(cause instanceof ApiError ? cause.code : 'Settings unavailable')
    })
    return () => controller.abort()
  }, [])

  const loadRun = useCallback(async (id: string, view: 'auto' | 'history', signal?: AbortSignal, historyUrl?: string) => {
    try {
      const detail = historyUrl ? await api.historyAt(historyUrl, signal) : await api.run(id, view, signal)
      if (signal?.aborted) return
      dispatchRun({ type: 'get', detail })
      setRunError('')
    } catch (cause) {
      if (!signal?.aborted) setRunError(cause instanceof ApiError ? cause.code : 'Run unavailable')
    }
  }, [])

  useEffect(() => {
    dispatchRun({ type: 'open', runId: runId ?? '' })
    setHistoryFallback(null)
    setRunError('')
    setSelectedComid(null)
    if (!runId) return
    const controller = new AbortController()
    if (mode === 'history') {
      void loadRun(runId, 'history', controller.signal)
    } else {
      runStream.current = controller
      void subscribe('run', runId, null, (event) => {
        if (event.name === 'run_snapshot') dispatchRun({ type: 'sse', snapshot: event.data })
      }, controller.signal).catch((cause: unknown) => {
        if (controller.signal.aborted) return
        if (cause instanceof ApiError && cause.status === 410 && cause.historyUrl) {
          setHistoryFallback({ runId, url: cause.historyUrl })
        } else setRunError(cause instanceof ApiError ? cause.code : 'Run stream unavailable')
      })
    }
    return () => { controller.abort(); if (runStream.current === controller) runStream.current = null }
  }, [runId, mode, loadRun])

  useEffect(() => {
    if (!runId || !historyUrl) return
    runStream.current?.abort()
    dispatchRun({ type: 'open', runId })
    const controller = new AbortController()
    void loadRun(runId, 'history', controller.signal, historyUrl)
    return () => controller.abort()
  }, [runId, historyUrl, loadRun])

  const hasRunRefs = Boolean(run.snapshot?.context_ref || run.snapshot?.topology_ref)
  useEffect(() => {
    if (!runId || historical || !hasRunRefs) return
    const controller = new AbortController()
    void loadRun(runId, 'auto', controller.signal)
    return () => controller.abort()
  }, [runId, historical, hasRunRefs, run.snapshot?.context_ref?.item_id,
    run.snapshot?.topology_ref?.item_id, run.snapshot?.result_ref?.item_id, loadRun])

  useEffect(() => {
    dispatchTranscript({ type: 'open', sessionId: sessionId ?? '' })
    setReferencedItems([])
    setResult(null)
  }, [runId, sessionId, historical])

  const sessionStatus = run.snapshot?.sessions.find((item) => item.session_id === sessionId)?.status
    ?? run.detail?.sessions?.find((item) => item.session_id === sessionId)?.status
  useEffect(() => {
    if (!runId || !sessionId) return
    const terminal = ['completed', 'failed', 'cancelled'].includes(sessionStatus ?? '')
    const controller = new AbortController()
    if (historical || terminal) {
      void api.transcript(runId, sessionId, historical ? 'history' : 'auto', controller.signal)
        .then((data) => {
          if (controller.signal.aborted) return
          dispatchTranscript({ type: 'history', sessionId, items: data.items ?? [] })
          setReferencedItems(data.referenced_items ?? [])
        }).catch((cause: unknown) => {
          if (!controller.signal.aborted) setRunError(cause instanceof ApiError ? cause.code : 'Transcript unavailable')
        })
    } else if (sessionStatus && sessionTabOpen) {
      void subscribe('session', runId, sessionId, (event) => {
        if (event.name !== 'stream_end' && event.name !== 'run_snapshot') {
          dispatchTranscript({ type: 'event', name: event.name, data: event.data })
        }
      }, controller.signal).catch((cause: unknown) => {
        if (controller.signal.aborted) return
        if (cause instanceof ApiError && cause.status === 410 && cause.historyUrl) {
          setHistoryFallback({ runId, url: `/api/analysis/runs/${encodeURIComponent(runId)}?view=history` })
        } else setRunError(cause instanceof ApiError ? cause.code : 'Session stream unavailable')
      })
    }
    return () => controller.abort()
  }, [runId, sessionId, historical, sessionTabOpen, sessionStatus, loadRun])

  const selectedSession = run.snapshot?.sessions.find((item) => item.session_id === sessionId)
    ?? run.detail?.sessions?.find((item) => item.session_id === sessionId)
  useEffect(() => {
    if (!runId || !sessionId || !selectedSession?.result_ref?.item_id) return
    let active = true
    void api.content(runId, selectedSession.result_ref.item_id, historical ? 'history' : 'auto')
      .then((content) => { if (active) setResult(inferenceFromText(content.text, runId, sessionId)) })
      .catch(() => { if (active) setResult(null) })
    return () => { active = false }
  }, [runId, sessionId, selectedSession?.result_ref?.item_id, historical])

  const summaryItemId = run.detail?.run.result_summary?.summary_result_ref?.item_id
    ?? run.snapshot?.summary.result_ref?.item_id
  useEffect(() => {
    setSummary(null)
    if (!runId || !summaryItemId) return
    let active = true
    void api.content(runId, summaryItemId, historical ? 'history' : 'auto')
      .then((content) => { if (active) setSummary(summaryFromText(content.text, runId)) })
      .catch(() => undefined)
    return () => { active = false }
  }, [runId, summaryItemId, historical])

  function selectSession(comid: string, id: string | null) {
    setSelectedComid(comid)
    if (!id || !runId) {
      setTab('overview')
      if (runId) {
        if (mode === 'history') setSearch({ run: runId })
        else navigate(`/runs/${encodeURIComponent(runId)}`)
      }
      return
    }
    setTab('agent')
    setMobileTab('detail')
    if (mode === 'history') setSearch({ run: runId, session: id })
    else navigate(`/runs/${encodeURIComponent(runId)}/waterbodies/${encodeURIComponent(id)}`)
  }

  function selectHistory(id: string) {
    setSearch({ run: id })
    setTab('overview')
    setMobileTab('map')
  }

  const loaded = loadedTopology?.key === topologyKey ? loadedTopology : null
  const graph = graphFromDetail(currentDetail) ?? loaded?.graph ?? null
  const topologyMessage = topologyRef && !graph
    ? topologyRunId !== runId ? 'Topology unavailable: invalid content reference'
      : loaded?.error ? `Topology unavailable: ${loaded.error}` : 'Loading topology…'
    : undefined
  const status = run.snapshot?.status ?? run.detail?.run.status ?? 'pending'
  const historyDelete = historical && runId && run.detail?.source === 'postgresql'
  const refresh = () => { if (runId) void loadRun(runId, historical ? 'history' : 'auto') }

  return <div className="app-shell">
    <header className="app-header">
      <Link to="/" className="brand">Water Agents</Link>
      <nav><Link to="/">New</Link><Link to="/history">History</Link></nav>
      <button type="button" aria-label="Settings" onClick={() => setSettingsOpen(true)}>⚙</button>
    </header>
    <div className="mobile-switch" role="tablist" aria-label="Workspace panels">
      {(['controls', 'map', 'detail'] as const).map((name) => <button key={name} role="tab"
        aria-selected={mobileTab === name} onClick={() => setMobileTab(name)}>{name}</button>)}
    </div>
    <main className="workspace">
      <aside className={`controls-panel mobile-${mobileTab}`}>
        {mode === 'history' ? <HistoryList selectedRunId={runId} onSelect={selectHistory} refreshVersion={historyVersion} />
          : settings ? <NewAnalysisForm settings={settings}
            mapPoint={mapPoint} onClearMapPoint={() => setMapPoint(null)}
            onCreated={(id) => navigate(`/runs/${id}`)} />
            : <p>{settingsError || 'Loading settings…'}</p>}
        {mode === 'run' && runId && <div className="run-controls">
          <p><strong>Run</strong> {runId}</p>
          <p>{status}</p>
          {!['completed', 'failed', 'cancelled', 'completed_with_persistence_error'].includes(status) && <button type="button" onClick={() => {
            void api.cancel(runId).catch((cause: unknown) => setRunError(cause instanceof ApiError ? cause.code : 'Cancel unavailable'))
          }}>Stop run</button>}
        </div>}
        {historyDelete && <button type="button" className="danger" onClick={() => setDeleteForRunId(runId)}>Delete this saved run</button>}
        {runError && <p className="error" role="alert">{runError}</p>}
      </aside>
      <div className={`center-panel mobile-${mobileTab}`}>
        <TopologyMap graph={graph} snapshot={run.snapshot} sessions={run.detail?.sessions} runStatus={status}
          topologyMessage={topologyMessage}
          selectedSessionId={sessionId} onSelect={selectSession}
          onPick={mode === 'home' ? (latitude, longitude) => setMapPoint({ latitude, longitude }) : undefined} />
        {selectedComid && !sessionId && <p className="map-note">COMID {selectedComid}: session not started. Topology and scheduling status are shown on the map.</p>}
        {sessionId && <details className="time-series-drawer"><summary>Saved time series</summary><TimeSeries result={result} /></details>}
      </div>
      <div className={`right-panel mobile-${mobileTab}`}>
        <DetailPanel runId={runId} snapshot={run.snapshot} detail={run.detail}
          sessionId={sessionId} processItems={transcript.items} referencedItems={referencedItems}
          result={result} summary={summary} readView={historical ? 'history' : 'auto'}
          tab={tab} onTab={setTab} onRefresh={refresh} />
      </div>
    </main>
    {settingsOpen && (settings
      ? <SettingsDrawer settings={settings} onSettings={setSettings} onClose={() => setSettingsOpen(false)} />
      : <div className="dialog-backdrop"><div className="dialog"><p>{settingsError || 'Loading settings…'}</p>
        <button type="button" onClick={() => setSettingsOpen(false)}>Close</button></div></div>)}
    {deleteForRunId === runId && runId && <DeleteRun key={runId} runId={runId} onClose={() => setDeleteForRunId(null)} onAccepted={() => {
      setDeleteForRunId(null)
      setHistoryVersion((value) => value + 1)
      navigate('/admin')
    }} />}
  </div>
}

function NotFound() {
  return <main className="simple-page"><h1>Page not found</h1><Link to="/">Go to analysis</Link></main>
}

export function App() {
  return <BrowserRouter><Routes>
    <Route path="/" element={<Workspace mode="home" />} />
    <Route path="/history" element={<Workspace mode="history" />} />
    <Route path="/runs/:runId" element={<Workspace mode="run" />} />
    <Route path="/runs/:runId/waterbodies/:sessionId" element={<Workspace mode="run" />} />
    <Route path="/admin" element={<AdminPage />} />
    <Route path="*" element={<NotFound />} />
  </Routes></BrowserRouter>
}
