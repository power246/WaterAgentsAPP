import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, ApiError, confirmedRequestRejected, prepareConfirmation, type Defaults,
  type KeyStatus, type Settings, type Submission, type UnitEntry, type UnitPreview } from '../../api/http'
import { safeError } from '../../api/safeError'

function CredentialRow({ item, onStatus }: { item: KeyStatus; onStatus: (item: KeyStatus) => void }) {
  const input = useRef<HTMLInputElement>(null)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')

  async function save() {
    const value = input.current?.value ?? ''
    if (input.current) input.current.value = ''
    if (!value) { setMessage('Enter a credential value.'); return }
    setBusy(true)
    setMessage('')
    try { onStatus(await api.saveKey(item.key_name, value)); setMessage('Saved. Future runs use the updated credential.') }
    catch (cause) { setMessage(safeError(cause)) }
    finally { setBusy(false) }
  }

  async function remove() {
    setBusy(true)
    setMessage('')
    try { onStatus(await api.deleteKey(item.key_name)); setMessage('User override removed. Effective source updated.') }
    catch (cause) { setMessage(safeError(cause)) }
    finally { setBusy(false) }
  }

  async function testConnection() {
    setBusy(true)
    setMessage('')
    try {
      const result = await api.testKey(item.key_name)
      setMessage(result.status === 'succeeded' ? 'Connection succeeded.' : result.safe_error?.code ?? 'Connection failed.')
    } catch (cause) { setMessage(safeError(cause)) }
    finally { setBusy(false) }
  }

  return <div className="credential-row">
    <h4>{item.label}</h4>
    <p>{item.purpose.join(', ')} · {item.configured ? 'Configured' : 'Not configured'} · {item.source}
      {item.masked ? ` · ${item.masked}` : ''}</p>
    <p className="muted">{item.store_available ? 'Credential store available' : 'Credential store unavailable'}
      {item.updated_at ? ` · Updated ${item.updated_at}` : ''}</p>
    {item.can_save && <><input ref={input} type="password" autoComplete="new-password"
      aria-label={`${item.label} credential`} maxLength={4096} />
      <button type="button" disabled={busy} onClick={() => void save()}>Save {item.label}</button></>}
    {item.can_delete_user_override && <button type="button" disabled={busy} onClick={() => void remove()}>Remove user override</button>}
    {item.can_test && <button type="button" disabled={busy} onClick={() => void testConnection()}>Test connection</button>}
    <p className="muted">Testing makes an external provider request and may incur usage.</p>
    {message && <p role="status">{message}</p>}
  </div>
}

export function SettingsDrawer({ settings, onSettings, onClose }: {
  settings: Settings
  onSettings: (settings: Settings) => void
  onClose: () => void
}) {
  const [server, setServer] = useState(settings)
  const serverRevision = useRef(settings.revision)
  const latestSettings = useRef(settings)
  const reviewedSettings = useRef(settings)
  const unitPolicyVersion = useRef(settings.unit_policy.version)
  const initialUnitEntries = useRef(settings.unit_policy.entries)
  const draftEditVersion = useRef(0)
  const acceptedDraftEditVersion = useRef(0)
  const previewRequest = useRef(0)
  const previewInput = useRef<{ request: number; version: number; entries: string } | null>(null)
  const [draft, setDraft] = useState<Defaults>(settings.defaults)
  const draftRef = useRef(settings.defaults)
  const [unitEntries, setUnitEntries] = useState<UnitEntry[]>(settings.unit_policy.entries)
  const [unitPreview, setUnitPreview] = useState<UnitPreview | null>(null)
  const [unitPhrase, setUnitPhrase] = useState('')
  const [unitAttempt, setUnitAttempt] = useState<{ submission: Submission; proposedEntries: UnitEntry[] } | null>(null)
  const [conflict, setConflict] = useState<Settings | null>(null)
  const conflictRef = useRef<Settings | null>(null)
  const [keys, setKeys] = useState<KeyStatus[]>([])
  const [keysError, setKeysError] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const defaultsDirty = JSON.stringify(draft) !== JSON.stringify(server.defaults)
  const unitsDirty = JSON.stringify(unitEntries) !== JSON.stringify(server.unit_policy.entries)
  const dirty = defaultsDirty || unitsDirty

  const acceptSettings = useCallback((current: Settings, source: 'read' | 'conflict' | 'save' = 'read'): boolean => {
    if (current.revision < serverRevision.current) return false
    const reviewed = reviewedSettings.current
    const draftDirty = JSON.stringify(draftRef.current) !== JSON.stringify(reviewed.defaults)
      || draftEditVersion.current !== acceptedDraftEditVersion.current
    serverRevision.current = current.revision
    latestSettings.current = current
    if (current.unit_policy.version !== unitPolicyVersion.current) {
      unitPolicyVersion.current = current.unit_policy.version
      previewRequest.current += 1
      previewInput.current = null
      setUnitPreview(null)
      setUnitPhrase('')
    }
    setServer(current)
    if (source === 'save') {
      reviewedSettings.current = current
      conflictRef.current = null
      setConflict(null)
    } else if (source === 'conflict' || conflictRef.current || (
      draftDirty && JSON.stringify(current.defaults) !== JSON.stringify(reviewed.defaults)
    )) {
      conflictRef.current = current
      setConflict(current)
      if (source === 'read') setError('Settings changed on the server. Review current values and explicitly reconfirm this full draft.')
    } else {
      reviewedSettings.current = current
      if (!draftDirty) {
        draftRef.current = current.defaults
        setDraft(current.defaults)
        acceptedDraftEditVersion.current = draftEditVersion.current
      }
    }
    onSettings(current)
    return true
  }, [onSettings])

  useEffect(() => {
    const controller = new AbortController()
    void api.settings(controller.signal).then((current) => {
      if (controller.signal.aborted) return
      if (!acceptSettings(current)) return
      setUnitEntries((value) => JSON.stringify(value) === JSON.stringify(initialUnitEntries.current)
        ? current.unit_policy.entries : value)
    }).catch((cause: unknown) => { if (!controller.signal.aborted) setError(safeError(cause)) })
    void api.keys(controller.signal).then((result) => { if (!controller.signal.aborted) setKeys(result.keys) })
      .catch((cause: unknown) => { if (!controller.signal.aborted) setKeysError(safeError(cause)) })
    return () => controller.abort()
  }, [acceptSettings])

  function close() {
    if (dirty && !window.confirm('Discard unsaved settings and unit changes?')) return
    onClose()
  }

  function change<K extends keyof Defaults>(field: K, value: Defaults[K]) {
    draftEditVersion.current += 1
    draftRef.current = { ...draftRef.current, [field]: value }
    setDraft(draftRef.current)
  }

  const modelOptions = (role: 'query_parser' | 'waterbody') => server.catalog.models
    .filter((option) => option.available && option.roles.includes(role))
  function modelSelect(role: 'query_parser' | 'waterbody', field: 'parser_model' | 'waterbody_model') {
    return <select aria-label={field} value={JSON.stringify(draft[field])} onChange={(event) => {
      const option = modelOptions(role).find((value) => JSON.stringify(value.selection) === event.target.value)
      if (option) change(field, option.selection)
    }}>
      {modelOptions(role).map((option) => <option key={JSON.stringify(option.selection)} value={JSON.stringify(option.selection)}>{option.label}</option>)}
    </select>
  }

  async function saveDefaults() {
    if (conflictRef.current || reviewedSettings.current.revision !== latestSettings.current.revision) {
      conflictRef.current = latestSettings.current
      setConflict(latestSettings.current)
      return
    }
    setBusy(true); setError(''); setNotice('')
    const submittedEditVersion = draftEditVersion.current
    try {
      const current = await api.replaceSettings(reviewedSettings.current.revision, draftRef.current)
      if (acceptSettings(current, 'save')) {
        if (draftEditVersion.current === submittedEditVersion) {
          draftRef.current = current.defaults
          setDraft(current.defaults)
          acceptedDraftEditVersion.current = draftEditVersion.current
        }
        setError('')
      }
      setNotice('Defaults saved. New runs use these values; running runs keep their snapshot.')
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 409 && cause.currentSettings) {
        const current = cause.currentSettings.revision < serverRevision.current
          ? latestSettings.current : cause.currentSettings
        acceptSettings(current, 'conflict')
        setError('Settings changed on the server. Review current values and explicitly reconfirm this full draft.')
      } else setError(safeError(cause))
    } finally { setBusy(false) }
  }

  async function previewUnits() {
    if (unitAttempt) return
    setBusy(true); setError(''); setUnitPreview(null); setUnitPhrase('')
    const request = ++previewRequest.current
    const version = server.unit_policy.version
    const entries = JSON.stringify(unitEntries)
    previewInput.current = null
    try {
      const result = await api.previewUnitPolicy(version, unitEntries)
      if (request === previewRequest.current && version === unitPolicyVersion.current) {
        previewInput.current = { request, version, entries }
        setUnitPreview(result)
      }
    } catch (cause) {
      if (request === previewRequest.current) setError(safeError(cause))
    }
    finally { setBusy(false) }
  }

  async function confirmUnits() {
    let attempt = unitAttempt
    if (!attempt) {
      if (!unitPreview?.confirmation || unitPhrase !== unitPreview.confirmation.required_phrase) return
      const preview = previewInput.current
      if (!preview || preview.request !== previewRequest.current
        || preview.version !== server.unit_policy.version
        || preview.entries !== JSON.stringify(unitEntries)) return
      attempt = {
        submission: prepareConfirmation(unitPreview.confirmation.token, unitPhrase),
        proposedEntries: unitPreview.proposed.entries,
      }
      setUnitAttempt(attempt)
    }
    setBusy(true); setError('')
    try {
      await api.confirmUnitPolicy(attempt.submission)
      setUnitAttempt(null); setUnitPreview(null); setUnitPhrase('')
      previewInput.current = null
      setNotice('Unit policy saved for future runs.')
    } catch (cause) {
      if (confirmedRequestRejected(cause)) {
        setUnitAttempt(null); setUnitPreview(null); setUnitPhrase('')
        setError(`${safeError(cause)}. Request a new unit preview before confirming again.`)
      } else setError(`${safeError(cause)}. Response unknown; retry this confirmation with the same request.`)
      setBusy(false)
      return
    }
    try {
      const current = await api.settings()
      if (acceptSettings(current)) {
        setUnitEntries((value) => JSON.stringify(value) === JSON.stringify(attempt.proposedEntries)
          ? current.unit_policy.entries : value)
      }
    } catch (cause) {
      setError(`${safeError(cause)}. Unit policy was accepted; reopen Settings to refresh current values.`)
    } finally { setBusy(false) }
  }

  function updateUnit(parameter: UnitEntry['parameter'], target: string) {
    if (unitAttempt) return
    previewRequest.current += 1
    previewInput.current = null
    setUnitEntries((current) => current.map((entry) => entry.parameter.code_system === parameter.code_system &&
      entry.parameter.code === parameter.code ? { ...entry, target_unit_id: target } : entry))
    setUnitPreview(null); setUnitPhrase('')
  }

  return <div className="dialog-backdrop" role="presentation" onClick={close}>
    <aside className="settings-drawer" aria-label="Settings" onClick={(event) => event.stopPropagation()}>
      <div className="panel-title"><h2>Settings</h2><button type="button" onClick={close}>Close</button></div>
      <p className="muted">Revision {server.revision}. Changes affect future runs.</p>
      <section><h3>Analysis defaults</h3>
        <label>Analysis timezone<select value={draft.analysis_timezone} onChange={(event) => change('analysis_timezone', event.target.value)}>
          {server.catalog.iana_timezones.map((zone) => <option key={zone}>{zone}</option>)}
        </select></label>
        <label>Parser model{modelSelect('query_parser', 'parser_model')}</label>
        <label>Waterbody model{modelSelect('waterbody', 'waterbody_model')}</label>
        <label>Memory mode<select value={draft.memory_mode} onChange={(event) => {
          const mode = server.catalog.memory_modes.find((value) => value === event.target.value)
          if (mode) change('memory_mode', mode)
        }}>{server.catalog.memory_modes.map((mode) => <option key={mode}>{mode}</option>)}</select></label>
        <label>Time resolution<select value={draft.time_resolution} onChange={(event) => {
          const resolution = server.catalog.time_resolutions.find((value) => value === event.target.value)
          if (resolution) change('time_resolution', resolution)
        }}>{server.catalog.time_resolutions.map((resolution) => <option key={resolution}>{resolution}</option>)}</select></label>
        <label>Desired embedding profile<select value={draft.desired_embedding_profile_id} onChange={(event) =>
          change('desired_embedding_profile_id', event.target.value)}>
          {server.catalog.embedding_profiles.filter((profile) => profile.available).map((profile) =>
            <option key={profile.profile_id} value={profile.profile_id}>{profile.label}</option>)}
        </select></label>
        <button type="button" disabled={busy || !defaultsDirty || Boolean(conflict)} onClick={() => void saveDefaults()}>Save full defaults</button>
        {conflict && <div className="conflict" role="alert">
          <strong>Server revision {conflict.revision}</strong>
          <p>Timezone {conflict.defaults.analysis_timezone}; memory {conflict.defaults.memory_mode}; resolution {conflict.defaults.time_resolution}</p>
          <p>Parser {conflict.defaults.parser_model.provider}/{conflict.defaults.parser_model.model}; waterbody {conflict.defaults.waterbody_model.provider}/{conflict.defaults.waterbody_model.model}</p>
          <p>Desired profile {conflict.defaults.desired_embedding_profile_id}</p>
          <button type="button" onClick={() => {
            if (conflict.revision !== latestSettings.current.revision) {
              conflictRef.current = latestSettings.current
              setConflict(latestSettings.current)
              return
            }
            reviewedSettings.current = latestSettings.current
            acceptedDraftEditVersion.current = draftEditVersion.current
            conflictRef.current = null
            setConflict(null); setError('')
          }}>
            Reconfirm this draft against revision {conflict.revision}
          </button>
        </div>}
      </section>
      <section><h3>Global units</h3>
        <p className="muted">Version {server.unit_policy.version}; registry {server.unit_policy.registry_digest}. A separate preview is required.</p>
        <div className="unit-options">{(server.catalog.unit_options ?? []).map((option) => {
          const selected = unitEntries.find((entry) => entry.parameter.code_system === option.parameter.code_system &&
            entry.parameter.code === option.parameter.code)
          return <label key={`${option.parameter.code_system}:${option.parameter.code}`}>
            {option.name} ({option.parameter.code_system}:{option.parameter.code})
            <select value={selected?.target_unit_id ?? option.source_unit_id} disabled={Boolean(unitAttempt)}
              onChange={(event) => updateUnit(option.parameter, event.target.value)}>
              {option.target_units.map((unit) => <option key={unit.unit_id} value={unit.unit_id}>{unit.symbol} · {unit.unit_id}</option>)}
            </select>
          </label>
        })}</div>
        <button type="button" disabled={busy || Boolean(unitAttempt) || !server.catalog.unit_options?.length || !unitsDirty}
          onClick={() => void previewUnits()}>Preview unit policy</button>
        {unitPreview && <div className="unit-preview">
          <h4>Server unit preview</h4>
          <p>Version {unitPreview.base_version} → {unitPreview.proposed.version}; registry {unitPreview.registry_digest}</p>
          <p>Dimensions: {unitPreview.affected_dimensions.join(', ') || 'none'}</p>
          <ul>{unitPreview.changes.map((change) => <li key={`${change.parameter.code_system}:${change.parameter.code}`}>
            {change.parameter.code_system}:{change.parameter.code} · {change.previous_unit_id ?? 'none'} → {change.target_unit_id}
          </li>)}</ul>
          {unitPreview.confirmation && !unitAttempt && <><p>Enter <strong>{unitPreview.confirmation.required_phrase}</strong> to confirm.</p>
            <input aria-label="Unit confirmation phrase" value={unitPhrase} disabled={Boolean(unitAttempt)} onChange={(event) => setUnitPhrase(event.target.value)} />
            <button type="button" disabled={busy || unitPhrase !== unitPreview.confirmation.required_phrase}
              onClick={() => void confirmUnits()}>Confirm unit policy</button></>}
        </div>}
        {unitAttempt && <button type="button" disabled={busy} onClick={() => void confirmUnits()}>Retry unit confirmation</button>}
      </section>
      <section><h3>Memory / RAG</h3>
        <p>Desired profile: {draft.desired_embedding_profile_id}</p>
        <p className="muted">Saving the desired profile does not start a handoff. Review it in Admin.</p>
      </section>
      <section><h3>Credentials</h3>
        {keysError && <p className="error" role="alert">{keysError}</p>}
        {keys.filter((item) => item.key_name === 'openai' || item.key_name === 'google_geocoding').map((item) =>
          <CredentialRow key={item.key_name} item={item} onStatus={(status) => setKeys((current) => current.map((row) =>
            row.key_name === status.key_name ? status : row))} />)}
      </section>
      {error && <p className="error" role="alert">{error}</p>}
      {notice && <p role="status">{notice}</p>}
      <Link to="/admin" onClick={(event) => { if (dirty && !window.confirm('Discard unsaved settings and unit changes?')) event.preventDefault() }}>
        Advanced / Admin
      </Link>
    </aside>
  </div>
}
