import { useEffect, useState, type FormEvent } from 'react'
import { api, ApiError, prepareSubmission, type CreateRequest, type Settings, type Submission } from '../../api/http'

type Model = CreateRequest['parser_model']

export function NewAnalysisForm({ settings, mapPoint, onClearMapPoint, onCreated }: {
  settings: Settings
  mapPoint: { latitude: number; longitude: number } | null
  onClearMapPoint: () => void
  onCreated: (runId: string) => void
}) {
  const [query, setQuery] = useState('')
  const [comid, setComid] = useState('')
  const [latitude, setLatitude] = useState('')
  const [longitude, setLongitude] = useState('')
  const [startDate, setStartDate] = useState('')
  const [endDate, setEndDate] = useState('')
  const [parserModel, setParserModel] = useState<Model>(settings.defaults.parser_model)
  const [waterbodyModel, setWaterbodyModel] = useState<Model>(settings.defaults.waterbody_model)
  const [memoryMode, setMemoryMode] = useState(settings.defaults.memory_mode)
  const [resolution, setResolution] = useState(settings.defaults.time_resolution)
  const [pending, setPending] = useState(false)
  const [retry, setRetry] = useState<Submission | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    if (mapPoint) {
      setComid('')
      setLatitude('')
      setLongitude('')
      setError('')
    }
  }, [mapPoint])

  const modelOptions = (role: 'query_parser' | 'waterbody') => settings.catalog.models
    .filter((option) => option.roles.includes(role) && option.available)
  const selectionKey = (model: Model) => `${model.provider}\u0000${model.model}`
  const modelSelect = (role: 'query_parser' | 'waterbody', selected: Model, set: (value: Model) => void) => (
    <select value={selectionKey(selected)} onChange={(event) => {
      const model = modelOptions(role).find((option) => selectionKey(option.selection) === event.target.value)
      if (model) set(model.selection)
    }}>
      {modelOptions(role).map((option) => <option key={selectionKey(option.selection)} value={selectionKey(option.selection)}>{option.label}</option>)}
    </select>
  )

  async function send(submission: Submission) {
    setPending(true)
    setError('')
    try {
      const created = await api.create(submission)
      setRetry(null)
      onCreated(created.run_id)
    } catch (cause) {
      setRetry(submission)
      setError(cause instanceof ApiError
        ? cause.code === 'idempotency_resource_deleted'
          ? 'This submission was deleted. Start a new analysis to create a new run.'
          : `Create failed: ${cause.code}${cause.requestId ? ` (${cause.requestId})` : ''}`
        : 'Connection interrupted. Retry this submission with the same request. ')
    } finally {
      setPending(false)
    }
  }

  function start(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!query.trim()) { setError('Enter a query.'); return }
    if (Boolean(startDate) !== Boolean(endDate)) { setError('Enter both dates or leave both blank.'); return }
    if (Boolean(latitude) !== Boolean(longitude)) { setError('Enter both coordinates or leave both blank.'); return }
    if (comid && (latitude || longitude || mapPoint)) { setError('Choose a COMID or coordinates.'); return }
    if (mapPoint && (latitude || longitude)) { setError('Use the map point or enter coordinates.'); return }
    const body: CreateRequest = {
      query: query.trim(), parser_model: parserModel, waterbody_model: waterbodyModel,
      memory_mode: memoryMode, time_resolution: resolution,
      ...(comid ? { target: { comid: comid.trim() } } : latitude && longitude
        ? { target: { coordinates: { latitude: Number(latitude), longitude: Number(longitude) } } }
        : mapPoint ? { target: { coordinates: mapPoint } } : {}),
      ...(startDate && endDate ? { date_range: { start_date: startDate, end_date: endDate } } : {}),
    }
    const submission = prepareSubmission(body)
    setRetry(null)
    void send(submission)
  }

  return <form className="new-analysis" onSubmit={start}>
    <h2>New analysis</h2>
    <label>Target COMID<input value={comid} onChange={(event) => {
      setComid(event.target.value)
      setLatitude(''); setLongitude(''); onClearMapPoint()
    }} inputMode="numeric" placeholder="Optional" /></label>
    <div className="form-row">
      <label>Latitude<input value={latitude} onChange={(event) => {
        setLatitude(event.target.value); setComid(''); onClearMapPoint()
      }} type="number" step="any" min="-90" max="90" /></label>
      <label>Longitude<input value={longitude} onChange={(event) => {
        setLongitude(event.target.value); setComid(''); onClearMapPoint()
      }} type="number" step="any" min="-180" max="180" /></label>
    </div>
    {mapPoint && <p className="muted">Map point: {mapPoint.latitude.toFixed(4)}, {mapPoint.longitude.toFixed(4)}
      <button type="button" onClick={onClearMapPoint}>Clear map point</button></p>}
    <label>Question<textarea value={query} onChange={(event) => setQuery(event.target.value)} required rows={4} placeholder="What should we examine?" /></label>
    <label>Parser model{modelSelect('query_parser', parserModel, setParserModel)}</label>
    <label>Waterbody model{modelSelect('waterbody', waterbodyModel, setWaterbodyModel)}</label>
    <div className="form-row">
      <label>Start date<input value={startDate} onChange={(event) => setStartDate(event.target.value)} type="date" /></label>
      <label>End date<input value={endDate} onChange={(event) => setEndDate(event.target.value)} type="date" /></label>
    </div>
    <label>Time resolution<select value={resolution} onChange={(event) => {
      const value = settings.catalog.time_resolutions.find((item) => item === event.target.value)
      if (value) setResolution(value)
    }}>
      {settings.catalog.time_resolutions.map((value) => <option key={value} value={value}>{value}</option>)}
    </select></label>
    <label>Memory mode<select value={memoryMode} onChange={(event) => {
      const value = settings.catalog.memory_modes.find((item) => item === event.target.value)
      if (value) setMemoryMode(value)
    }}>
      {settings.catalog.memory_modes.map((value) => <option key={value} value={value}>{value}</option>)}
    </select></label>
    {error && <p className="error" role="alert">{error}</p>}
    <button type="submit" disabled={pending}>Start new analysis</button>
    {retry && <button type="button" disabled={pending} onClick={() => void send(retry)}>Retry same submission</button>}
  </form>
}
