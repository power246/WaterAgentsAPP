import { afterEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { api, ApiError, type KeyStatus, type Settings, type UnitPreview } from '../src/api/http'
import { SettingsDrawer } from '../src/features/settings/SettingsDrawer'

const profile = `sha256:${'a'.repeat(64)}`
const settings: Settings = {
  schema_version: 1, revision: 0, updated_at: null,
  defaults: { analysis_timezone: 'UTC', parser_model: { provider: 'openai', model: 'parser' },
    waterbody_model: { provider: 'openai', model: 'waterbody' }, memory_mode: 'stateless',
    time_resolution: '12h', desired_embedding_profile_id: profile },
  catalog: { models: [
    { selection: { provider: 'openai', model: 'parser' }, label: 'Parser', roles: ['query_parser'], available: true, unavailable_reason: null },
    { selection: { provider: 'openai', model: 'waterbody' }, label: 'Waterbody', roles: ['waterbody'], available: true, unavailable_reason: null },
  ], embedding_profiles: [{ profile_id: profile, label: 'Embedding', available: true, unavailable_reason: null }],
  memory_modes: ['stateless'], time_resolutions: ['12h'], iana_timezones: ['UTC', 'America/Denver'],
  unit_options: [{ parameter: { code_system: 'usgs', code: '00060' }, name: 'Discharge', source_unit_id: 'ft3_s',
    target_units: [{ unit_id: 'ft3_s', symbol: 'ft³/s', dimension_id: 'discharge' },
      { unit_id: 'm3_s', symbol: 'm³/s', dimension_id: 'discharge' }] }],
  },
  unit_policy: { version: 1, registry_digest: 'a'.repeat(64),
    entries: [{ parameter: { code_system: 'usgs', code: '00060' }, target_unit_id: 'ft3_s' }] },
}
const key: KeyStatus = {
  key_name: 'openai', label: 'OpenAI', purpose: ['query_parser'], configured: true,
  source: 'user_store', masked: '••••1234', updated_at: null, store_available: true,
  can_save: true, can_delete_user_override: true, can_test: true,
}
const unitPreview: UnitPreview = { base_version: 1, proposed: { ...settings.unit_policy, version: 2,
  entries: [{ parameter: { code_system: 'usgs', code: '00060' }, target_unit_id: 'm3_s' }] },
  changes: [{ parameter: { code_system: 'usgs', code: '00060' }, previous_unit_id: 'ft3_s', target_unit_id: 'm3_s',
    dimension_id: 'discharge' }], affected_dimensions: ['discharge'], registry_digest: settings.unit_policy.registry_digest,
  confirmation: { token: 'server-token', required_phrase: 'APPLY UNIT POLICY 2', expires_at: '2026-10-08T01:00:00Z' } }

function setup() {
  vi.spyOn(api, 'keys').mockResolvedValue({ keys: [key] })
  return render(<MemoryRouter><SettingsDrawer settings={settings} onSettings={() => undefined} onClose={() => undefined} /></MemoryRouter>)
}

afterEach(() => { cleanup(); vi.restoreAllMocks(); localStorage.clear() })

it('shows the reserved embedding profile as disabled when historical memory is off', () => {
  const disabled: Settings = { ...settings, catalog: { ...settings.catalog,
    embedding_profiles: [{ ...settings.catalog.embedding_profiles[0]!, available: false,
      unavailable_reason: 'Historical memory is disabled.' }],
  } }
  vi.spyOn(api, 'keys').mockResolvedValue({ keys: [key] })
  vi.spyOn(api, 'settings').mockResolvedValue(disabled)
  render(<MemoryRouter><SettingsDrawer settings={disabled} onSettings={() => undefined} onClose={() => undefined} /></MemoryRouter>)
  expect(screen.getByLabelText('Desired embedding profile')).toBeDisabled()
  expect(screen.getByLabelText('Desired embedding profile')).toHaveValue(profile)
  expect(screen.getByLabelText('Memory mode')).toHaveValue('stateless')
})

it('preserves edits made before the initial settings refresh completes', async () => {
  let resolve!: (value: Settings) => void
  vi.spyOn(api, 'settings').mockReturnValue(new Promise<Settings>((done) => { resolve = done }))
  setup()
  fireEvent.change(screen.getByLabelText('Analysis timezone'), { target: { value: 'America/Denver' } })
  await act(async () => { resolve({ ...settings, revision: 1 }) })
  expect(screen.getByLabelText('Analysis timezone')).toHaveValue('America/Denver')
  expect(screen.getByRole('button', { name: 'Save full defaults' })).toBeEnabled()
})

it('does not replace edits made while a defaults save is pending', async () => {
  vi.spyOn(api, 'settings').mockResolvedValue(settings)
  let resolve!: (value: Settings) => void
  vi.spyOn(api, 'replaceSettings').mockReturnValue(new Promise<Settings>((done) => { resolve = done }))
  setup()
  await screen.findByLabelText('OpenAI credential')
  fireEvent.change(screen.getByLabelText('Analysis timezone'), { target: { value: 'America/Denver' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save full defaults' }))
  fireEvent.change(screen.getByLabelText('Analysis timezone'), { target: { value: 'UTC' } })
  await act(async () => { resolve({ ...settings, revision: 1, defaults: {
    ...settings.defaults, analysis_timezone: 'America/Denver',
  } }) })
  expect(screen.getByLabelText('Analysis timezone')).toHaveValue('UTC')
  expect(screen.getByRole('button', { name: 'Save full defaults' })).toBeEnabled()
  expect(screen.getByText('Revision 1. Changes affect future runs.')).toBeInTheDocument()
})

it('preserves edits when the initial GET sees a pending save before its response arrives', async () => {
  let resolveGet!: (value: Settings) => void
  let resolveSave!: (value: Settings) => void
  const committed = { ...settings, revision: 1, defaults: {
    ...settings.defaults, analysis_timezone: 'America/Denver',
  } }
  vi.spyOn(api, 'settings').mockReturnValue(new Promise<Settings>((done) => { resolveGet = done }))
  const save = vi.spyOn(api, 'replaceSettings')
    .mockReturnValueOnce(new Promise<Settings>((done) => { resolveSave = done }))
    .mockResolvedValueOnce({ ...settings, revision: 2 })
  setup()

  fireEvent.change(screen.getByLabelText('Analysis timezone'), { target: { value: 'America/Denver' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save full defaults' }))
  expect(save).toHaveBeenCalledWith(0, expect.objectContaining({ analysis_timezone: 'America/Denver' }))
  fireEvent.change(screen.getByLabelText('Analysis timezone'), { target: { value: 'UTC' } })

  await act(async () => { resolveGet(committed) })
  expect(screen.getByLabelText('Analysis timezone')).toHaveValue('UTC')

  await act(async () => { resolveSave(committed) })
  expect(screen.getByLabelText('Analysis timezone')).toHaveValue('UTC')
  expect(screen.getByRole('button', { name: 'Save full defaults' })).toBeEnabled()
  fireEvent.click(screen.getByRole('button', { name: 'Save full defaults' }))
  await waitFor(() => expect(save).toHaveBeenCalledWith(1, expect.objectContaining({ analysis_timezone: 'UTC' })))
})

it.each([
  ['network error', new Error('response lost')],
  ['service unavailable', new ApiError(503, 'service_unavailable', 'request')],
  ['invalid JSON', new ApiError(200, 'invalid_json', 'request')],
  ['invalid response', new ApiError(200, 'invalid_response', 'request')],
])('preserves edits after an uncertain save %s and a late GET', async (_name, failure) => {
  let resolveGet!: (value: Settings) => void
  let rejectSave!: (cause: unknown) => void
  const committed = { ...settings, revision: 1, defaults: {
    ...settings.defaults, analysis_timezone: 'America/Denver',
  } }
  vi.spyOn(api, 'settings').mockReturnValue(new Promise<Settings>((done) => { resolveGet = done }))
  const save = vi.spyOn(api, 'replaceSettings')
    .mockReturnValueOnce(new Promise<Settings>((_done, reject) => { rejectSave = reject }))
    .mockResolvedValueOnce({ ...settings, revision: 2 })
  setup()

  fireEvent.change(screen.getByLabelText('Analysis timezone'), { target: { value: 'America/Denver' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save full defaults' }))
  fireEvent.change(screen.getByLabelText('Analysis timezone'), { target: { value: 'UTC' } })
  await act(async () => { rejectSave(failure) })
  await act(async () => { resolveGet(committed) })

  expect(screen.getByLabelText('Analysis timezone')).toHaveValue('UTC')
  expect(screen.getByRole('button', { name: /Reconfirm this draft against revision 1/ })).toBeInTheDocument()
  expect(save).toHaveBeenCalledTimes(1)
  fireEvent.click(screen.getByRole('button', { name: /Reconfirm this draft against revision 1/ }))
  fireEvent.click(screen.getByRole('button', { name: 'Save full defaults' }))
  await waitFor(() => expect(save).toHaveBeenCalledWith(1, expect.objectContaining({ analysis_timezone: 'UTC' })))
})

it('asks before leaving when an unconfirmed save is followed by edits back to the old value', async () => {
  let rejectSave!: (cause: unknown) => void
  vi.spyOn(api, 'settings').mockReturnValue(new Promise<Settings>(() => undefined))
  vi.spyOn(api, 'replaceSettings').mockReturnValue(new Promise<Settings>((_done, reject) => { rejectSave = reject }))
  vi.spyOn(api, 'keys').mockResolvedValue({ keys: [key] })
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
  const onClose = vi.fn()
  render(<MemoryRouter><SettingsDrawer settings={settings} onSettings={() => undefined} onClose={onClose} /></MemoryRouter>)

  fireEvent.change(screen.getByLabelText('Analysis timezone'), { target: { value: 'America/Denver' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save full defaults' }))
  fireEvent.change(screen.getByLabelText('Analysis timezone'), { target: { value: 'UTC' } })
  await act(async () => { rejectSave(new Error('response lost')) })
  expect(screen.getByLabelText('Analysis timezone')).toHaveValue('UTC')

  fireEvent.click(screen.getByRole('button', { name: 'Close' }))
  expect(onClose).not.toHaveBeenCalled()
  expect(confirm).toHaveBeenCalledTimes(1)
  expect(fireEvent.click(screen.getByRole('link', { name: 'Advanced / Admin' }))).toBe(false)
  expect(confirm).toHaveBeenCalledTimes(2)
})

it('closes without a discard prompt after a confirmed save with no later edits', async () => {
  const committed = { ...settings, revision: 1, defaults: {
    ...settings.defaults, analysis_timezone: 'America/Denver',
  } }
  vi.spyOn(api, 'settings').mockResolvedValue(settings)
  vi.spyOn(api, 'replaceSettings').mockResolvedValue(committed)
  vi.spyOn(api, 'keys').mockResolvedValue({ keys: [key] })
  const confirm = vi.spyOn(window, 'confirm')
  const onClose = vi.fn()
  render(<MemoryRouter><SettingsDrawer settings={settings} onSettings={() => undefined} onClose={onClose} /></MemoryRouter>)

  fireEvent.change(screen.getByLabelText('Analysis timezone'), { target: { value: 'America/Denver' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save full defaults' }))
  await screen.findByText('Revision 1. Changes affect future runs.')
  fireEvent.click(screen.getByRole('button', { name: 'Close' }))
  expect(confirm).not.toHaveBeenCalled()
  expect(onClose).toHaveBeenCalledOnce()
})

it('ignores an initial settings GET that arrives after a successful save', async () => {
  let resolveGet!: (value: Settings) => void
  vi.spyOn(api, 'settings').mockReturnValue(new Promise<Settings>((done) => { resolveGet = done }))
  vi.spyOn(api, 'replaceSettings').mockResolvedValue({ ...settings, revision: 1, defaults: {
    ...settings.defaults, analysis_timezone: 'America/Denver',
  } })
  vi.spyOn(api, 'keys').mockResolvedValue({ keys: [key] })
  const onSettings = vi.fn()
  render(<MemoryRouter><SettingsDrawer settings={settings} onSettings={onSettings}
    onClose={() => undefined} /></MemoryRouter>)
  fireEvent.change(screen.getByLabelText('Analysis timezone'), { target: { value: 'America/Denver' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save full defaults' }))
  expect(await screen.findByText('Revision 1. Changes affect future runs.')).toBeInTheDocument()
  await act(async () => { resolveGet(settings) })
  expect(screen.getByText('Revision 1. Changes affect future runs.')).toBeInTheDocument()
  expect(screen.getByLabelText('Analysis timezone')).toHaveValue('America/Denver')
  expect(onSettings).toHaveBeenCalledTimes(1)
  expect(onSettings).toHaveBeenCalledWith(expect.objectContaining({ revision: 1 }))
})

it('discards a unit preview response after the selection changes', async () => {
  vi.spyOn(api, 'settings').mockResolvedValue(settings)
  let resolve!: (value: UnitPreview) => void
  vi.spyOn(api, 'previewUnitPolicy').mockReturnValue(new Promise<UnitPreview>((done) => { resolve = done }))
  setup()
  await screen.findByLabelText('OpenAI credential')
  const unit = screen.getByLabelText(/Discharge \(usgs:00060\)/)
  fireEvent.change(unit, { target: { value: 'm3_s' } })
  fireEvent.click(screen.getByRole('button', { name: 'Preview unit policy' }))
  fireEvent.change(unit, { target: { value: 'ft3_s' } })
  await act(async () => { resolve(unitPreview) })
  expect(screen.queryByText('Server unit preview')).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Confirm unit policy' })).not.toBeInTheDocument()
})

it('preserves an unsaved defaults draft after a unit policy refresh', async () => {
  const refreshed = { ...settings,
    revision: 1, unit_policy: { ...settings.unit_policy, version: 2,
      entries: [{ parameter: { code_system: 'usgs', code: '00060' }, target_unit_id: 'm3_s' }] } }
  vi.spyOn(api, 'settings').mockResolvedValueOnce(settings).mockResolvedValueOnce(refreshed)
  vi.spyOn(api, 'previewUnitPolicy').mockResolvedValue(unitPreview)
  vi.spyOn(api, 'confirmUnitPolicy').mockResolvedValue({ ...unitPreview.proposed, created_at: '2026-10-09T00:00:00Z' })
  const save = vi.spyOn(api, 'replaceSettings').mockResolvedValue({ ...refreshed, revision: 2, defaults: {
    ...settings.defaults, analysis_timezone: 'America/Denver',
  } })
  setup()
  await screen.findByLabelText('OpenAI credential')
  fireEvent.change(screen.getByLabelText('Analysis timezone'), { target: { value: 'America/Denver' } })
  fireEvent.change(screen.getByLabelText(/Discharge \(usgs:00060\)/), { target: { value: 'm3_s' } })
  fireEvent.click(screen.getByRole('button', { name: 'Preview unit policy' }))
  await screen.findByRole('button', { name: 'Confirm unit policy' })
  fireEvent.change(screen.getByRole('textbox', { name: 'Unit confirmation phrase' }),
    { target: { value: 'APPLY UNIT POLICY 2' } })
  fireEvent.click(screen.getByRole('button', { name: 'Confirm unit policy' }))
  await screen.findByText('Unit policy saved for future runs.')
  await screen.findByText('Revision 1. Changes affect future runs.')
  expect(screen.getByLabelText('Analysis timezone')).toHaveValue('America/Denver')
  expect(screen.getByRole('button', { name: 'Save full defaults' })).toBeEnabled()
  expect(save).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Save full defaults' }))
  await waitFor(() => expect(save).toHaveBeenCalledWith(1, expect.objectContaining({
    analysis_timezone: 'America/Denver',
  })))
})

it('keeps a full draft through revision conflict and requires explicit reconfirmation', async () => {
  const current = { ...settings, revision: 1, updated_at: '2026-10-08T00:00:00Z' }
  vi.spyOn(api, 'settings').mockResolvedValue(settings)
  const save = vi.spyOn(api, 'replaceSettings')
    .mockRejectedValueOnce(new ApiError(409, 'settings_revision_conflict', 'request', null, current))
    .mockResolvedValueOnce({ ...current, revision: 2, defaults: { ...settings.defaults, analysis_timezone: 'America/Denver' } })
  setup()
  await screen.findByLabelText('OpenAI credential')
  fireEvent.change(screen.getByLabelText('Analysis timezone'), { target: { value: 'America/Denver' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save full defaults' }))
  expect(await screen.findByText(/Settings changed on the server/)).toBeInTheDocument()
  expect(screen.getByLabelText('Analysis timezone')).toHaveValue('America/Denver')
  expect(save).toHaveBeenCalledWith(0, expect.objectContaining({ analysis_timezone: 'America/Denver',
    desired_embedding_profile_id: profile }))
  fireEvent.click(screen.getByRole('button', { name: /Reconfirm this draft against revision 1/ }))
  fireEvent.click(screen.getByRole('button', { name: 'Save full defaults' }))
  await waitFor(() => expect(save).toHaveBeenCalledWith(1, expect.objectContaining({ analysis_timezone: 'America/Denver' })))
  expect(await screen.findByText(/New runs use these values/)).toBeInTheDocument()
})

it('requires another review when a late settings GET follows reconfirmation', async () => {
  let resolveGet!: (value: Settings) => void
  vi.spyOn(api, 'settings').mockReturnValue(new Promise<Settings>((done) => { resolveGet = done }))
  const first = { ...settings, revision: 1 }
  const newer: Settings = { ...first, revision: 2, defaults: {
    ...settings.defaults, memory_mode: 'memory_augmented',
  }, catalog: { ...settings.catalog, memory_modes: ['stateless', 'memory_augmented'] } }
  const save = vi.spyOn(api, 'replaceSettings')
    .mockRejectedValueOnce(new ApiError(409, 'settings_revision_conflict', 'request', null, first))
    .mockResolvedValueOnce({ ...newer, revision: 3, defaults: {
      ...newer.defaults, analysis_timezone: 'America/Denver', memory_mode: 'stateless',
    } })
  setup()
  fireEvent.change(screen.getByLabelText('Analysis timezone'), { target: { value: 'America/Denver' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save full defaults' }))
  await screen.findByRole('button', { name: /Reconfirm this draft against revision 1/ })
  fireEvent.click(screen.getByRole('button', { name: /Reconfirm this draft against revision 1/ }))
  expect(screen.getByRole('button', { name: 'Save full defaults' })).toBeEnabled()

  await act(async () => { resolveGet(newer) })
  expect(await screen.findByRole('button', { name: /Reconfirm this draft against revision 2/ })).toBeInTheDocument()
  expect(screen.getByText(/memory memory_augmented/)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Save full defaults' })).toBeDisabled()
  expect(save).toHaveBeenCalledTimes(1)

  fireEvent.click(screen.getByRole('button', { name: /Reconfirm this draft against revision 2/ }))
  fireEvent.click(screen.getByRole('button', { name: 'Save full defaults' }))
  await waitFor(() => expect(save).toHaveBeenCalledWith(2, expect.objectContaining({
    analysis_timezone: 'America/Denver', memory_mode: 'stateless',
  })))
})

it('requires a server unit preview and phrase before confirming a version', async () => {
  vi.spyOn(api, 'settings').mockResolvedValueOnce(settings).mockResolvedValueOnce({ ...settings,
    revision: 1, updated_at: '2026-10-08T00:00:00Z', unit_policy: { ...settings.unit_policy, version: 2,
      entries: [{ parameter: { code_system: 'usgs', code: '00060' }, target_unit_id: 'm3_s' }] } })
  const preview = vi.spyOn(api, 'previewUnitPolicy').mockResolvedValue(unitPreview)
  const confirm = vi.spyOn(api, 'confirmUnitPolicy').mockResolvedValue({ ...unitPreview.proposed, created_at: '2026-10-08T00:01:00Z' })
  const defaults = vi.spyOn(api, 'replaceSettings')
  setup()
  await screen.findByLabelText('OpenAI credential')
  fireEvent.change(screen.getByLabelText(/Discharge \(usgs:00060\)/), { target: { value: 'm3_s' } })
  fireEvent.click(screen.getByRole('button', { name: 'Preview unit policy' }))
  await waitFor(() => expect(preview).toHaveBeenCalledWith(1, [{ parameter: { code_system: 'usgs', code: '00060' }, target_unit_id: 'm3_s' }]))
  expect(screen.getByText(/Version 1 → 2/)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Confirm unit policy' })).toBeDisabled()
  fireEvent.change(screen.getByRole('textbox', { name: 'Unit confirmation phrase' }), { target: { value: 'APPLY UNIT POLICY 2' } })
  fireEvent.click(screen.getByRole('button', { name: 'Confirm unit policy' }))
  await waitFor(() => expect(confirm).toHaveBeenCalledWith(expect.objectContaining({
    key: expect.any(String), bytes: JSON.stringify({ confirmation_token: 'server-token', confirmation_phrase: 'APPLY UNIT POLICY 2' }),
  })))
  expect(defaults).not.toHaveBeenCalled()
})

it('retries an uncertain unit confirmation with the same key and body', async () => {
  vi.spyOn(api, 'settings').mockResolvedValue(settings)
  vi.spyOn(api, 'previewUnitPolicy').mockResolvedValue(unitPreview)
  const confirm = vi.spyOn(api, 'confirmUnitPolicy').mockRejectedValueOnce(new Error('response lost'))
    .mockResolvedValueOnce({ ...unitPreview.proposed, created_at: '2026-10-08T00:01:00Z' })
  setup()
  await screen.findByLabelText('OpenAI credential')
  fireEvent.change(screen.getByLabelText(/Discharge \(usgs:00060\)/), { target: { value: 'm3_s' } })
  fireEvent.click(screen.getByRole('button', { name: 'Preview unit policy' }))
  await screen.findByRole('button', { name: 'Confirm unit policy' })
  fireEvent.change(screen.getByRole('textbox', { name: 'Unit confirmation phrase' }), { target: { value: 'APPLY UNIT POLICY 2' } })
  fireEvent.click(screen.getByRole('button', { name: 'Confirm unit policy' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Response unknown')
  fireEvent.click(screen.getByRole('button', { name: 'Retry unit confirmation' }))
  await waitFor(() => expect(confirm).toHaveBeenCalledTimes(2))
  expect(confirm.mock.calls[1]).toEqual(confirm.mock.calls[0])
  expect(await screen.findByText('Unit policy saved for future runs.')).toBeInTheDocument()
})

it('keeps the unit retry available after a settings conflict invalidates its preview', async () => {
  const committed = { ...settings, revision: 1, unit_policy: unitPreview.proposed }
  vi.spyOn(api, 'settings').mockResolvedValueOnce(settings).mockResolvedValueOnce(committed)
  vi.spyOn(api, 'previewUnitPolicy').mockResolvedValue(unitPreview)
  const confirm = vi.spyOn(api, 'confirmUnitPolicy').mockRejectedValueOnce(new Error('response lost'))
    .mockResolvedValueOnce({ ...unitPreview.proposed, created_at: '2026-10-08T00:01:00Z' })
  vi.spyOn(api, 'replaceSettings').mockRejectedValueOnce(
    new ApiError(409, 'settings_revision_conflict', 'request', null, committed))
  setup()
  await screen.findByLabelText('OpenAI credential')
  fireEvent.change(screen.getByLabelText(/Discharge \(usgs:00060\)/), { target: { value: 'm3_s' } })
  fireEvent.click(screen.getByRole('button', { name: 'Preview unit policy' }))
  await screen.findByRole('button', { name: 'Confirm unit policy' })
  fireEvent.change(screen.getByRole('textbox', { name: 'Unit confirmation phrase' }),
    { target: { value: 'APPLY UNIT POLICY 2' } })
  fireEvent.click(screen.getByRole('button', { name: 'Confirm unit policy' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Response unknown')

  fireEvent.change(screen.getByLabelText('Analysis timezone'), { target: { value: 'America/Denver' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save full defaults' }))
  expect(await screen.findByText('Revision 1. Changes affect future runs.')).toBeInTheDocument()
  expect(screen.queryByText('Server unit preview')).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Retry unit confirmation' })).toBeEnabled()
  expect(screen.getByLabelText(/Discharge \(usgs:00060\)/)).toBeDisabled()

  fireEvent.click(screen.getByRole('button', { name: 'Retry unit confirmation' }))
  await waitFor(() => expect(confirm).toHaveBeenCalledTimes(2))
  expect(confirm.mock.calls[1]).toEqual(confirm.mock.calls[0])
  expect(await screen.findByText('Unit policy saved for future runs.')).toBeInTheDocument()
  expect(screen.getByLabelText(/Discharge \(usgs:00060\)/)).toBeEnabled()
})

it('requires review of the latest revision after a unit retry refreshes settings', async () => {
  const conflicted = { ...settings, revision: 1, unit_policy: unitPreview.proposed }
  const latest = { ...conflicted, revision: 2, defaults: {
    ...settings.defaults, memory_mode: 'memory_augmented' as const,
  }, catalog: { ...settings.catalog, memory_modes: ['stateless', 'memory_augmented'] as Settings['catalog']['memory_modes'] } }
  vi.spyOn(api, 'settings').mockResolvedValueOnce(settings).mockResolvedValueOnce(latest)
  vi.spyOn(api, 'previewUnitPolicy').mockResolvedValue(unitPreview)
  vi.spyOn(api, 'confirmUnitPolicy').mockRejectedValueOnce(new Error('response lost'))
    .mockResolvedValueOnce({ ...unitPreview.proposed, created_at: '2026-10-08T00:01:00Z' })
  const save = vi.spyOn(api, 'replaceSettings').mockRejectedValueOnce(
    new ApiError(409, 'settings_revision_conflict', 'request', null, conflicted))
    .mockResolvedValueOnce({ ...latest, revision: 3, defaults: {
      ...settings.defaults, analysis_timezone: 'America/Denver',
    } })
  setup()
  await screen.findByLabelText('OpenAI credential')
  fireEvent.change(screen.getByLabelText(/Discharge \(usgs:00060\)/), { target: { value: 'm3_s' } })
  fireEvent.click(screen.getByRole('button', { name: 'Preview unit policy' }))
  await screen.findByRole('button', { name: 'Confirm unit policy' })
  fireEvent.change(screen.getByRole('textbox', { name: 'Unit confirmation phrase' }),
    { target: { value: 'APPLY UNIT POLICY 2' } })
  fireEvent.click(screen.getByRole('button', { name: 'Confirm unit policy' }))
  await screen.findByRole('button', { name: 'Retry unit confirmation' })
  fireEvent.change(screen.getByLabelText('Analysis timezone'), { target: { value: 'America/Denver' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save full defaults' }))
  await screen.findByRole('button', { name: /Reconfirm this draft against revision 1/ })

  fireEvent.click(screen.getByRole('button', { name: 'Retry unit confirmation' }))
  expect(await screen.findByRole('button', { name: /Reconfirm this draft against revision 2/ })).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /Reconfirm this draft against revision 1/ })).not.toBeInTheDocument()
  expect(screen.getByText(/memory memory_augmented/)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Save full defaults' })).toBeDisabled()
  expect(save).toHaveBeenCalledTimes(1)

  fireEvent.click(screen.getByRole('button', { name: /Reconfirm this draft against revision 2/ }))
  fireEvent.click(screen.getByRole('button', { name: 'Save full defaults' }))
  await waitFor(() => expect(save).toHaveBeenCalledWith(2, expect.objectContaining({
    analysis_timezone: 'America/Denver', memory_mode: 'stateless',
  })))
})

it('does not resubmit an accepted unit policy when refreshing settings fails', async () => {
  vi.spyOn(api, 'settings').mockResolvedValueOnce(settings).mockRejectedValueOnce(new Error('offline'))
  vi.spyOn(api, 'previewUnitPolicy').mockResolvedValue(unitPreview)
  const confirm = vi.spyOn(api, 'confirmUnitPolicy').mockResolvedValue({ ...unitPreview.proposed,
    created_at: '2026-10-08T00:01:00Z' })
  setup()
  await screen.findByLabelText('OpenAI credential')
  fireEvent.change(screen.getByLabelText(/Discharge \(usgs:00060\)/), { target: { value: 'm3_s' } })
  fireEvent.click(screen.getByRole('button', { name: 'Preview unit policy' }))
  await screen.findByRole('button', { name: 'Confirm unit policy' })
  fireEvent.change(screen.getByRole('textbox', { name: 'Unit confirmation phrase' }), { target: { value: 'APPLY UNIT POLICY 2' } })
  fireEvent.click(screen.getByRole('button', { name: 'Confirm unit policy' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Unit policy was accepted')
  expect(screen.queryByRole('button', { name: 'Retry unit confirmation' })).not.toBeInTheDocument()
  expect(confirm).toHaveBeenCalledTimes(1)
})

it('clears submitted credential from DOM and state; save never runs connection test', async () => {
  vi.spyOn(api, 'settings').mockResolvedValue(settings)
  const save = vi.spyOn(api, 'saveKey').mockResolvedValue(key)
  const remove = vi.spyOn(api, 'deleteKey').mockResolvedValue({ ...key, source: 'deployment',
    can_delete_user_override: false })
  const test = vi.spyOn(api, 'testKey')
  const logs = vi.spyOn(console, 'log')
  const view = setup()
  const input = await screen.findByLabelText('OpenAI credential')
  fireEvent.change(input, { target: { value: 'test-only-private-value' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save OpenAI' }))
  await waitFor(() => expect(save).toHaveBeenCalledWith('openai', 'test-only-private-value'))
  expect(input).toHaveValue('')
  expect(view.container.innerHTML).not.toContain('test-only-private-value')
  expect(localStorage.length).toBe(0)
  expect(test).not.toHaveBeenCalled()
  expect(logs.mock.calls.flat().join(' ')).not.toContain('test-only-private-value')
  fireEvent.click(screen.getByRole('button', { name: 'Remove user override' }))
  await waitFor(() => expect(remove).toHaveBeenCalledWith('openai'))
  expect(await screen.findByText(/deployment/)).toBeInTheDocument()
})

it('keeps an invalid draft and reports a settings service failure safely', async () => {
  vi.spyOn(api, 'settings').mockResolvedValue(settings)
  const save = vi.spyOn(api, 'replaceSettings')
    .mockRejectedValue(new ApiError(422, 'request_validation_failed', 'safe-request-id'))
  setup()
  await screen.findByLabelText('OpenAI credential')
  fireEvent.change(screen.getByLabelText('Analysis timezone'), { target: { value: 'America/Denver' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save full defaults' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('request_validation_failed (safe-request-id)')
  expect(screen.getByLabelText('Analysis timezone')).toHaveValue('America/Denver')
  expect(save).toHaveBeenCalledTimes(1)
})

it('shows unavailable services and disables credential actions the server does not allow', async () => {
  vi.spyOn(api, 'settings').mockRejectedValue(new Error('private transport detail'))
  vi.spyOn(api, 'keys').mockResolvedValue({ keys: [{ ...key, store_available: false,
    can_save: false, can_delete_user_override: false, can_test: false }] })
  render(<MemoryRouter><SettingsDrawer settings={settings} onSettings={() => undefined} onClose={() => undefined} /></MemoryRouter>)
  expect(await screen.findByText('Credential store unavailable')).toBeInTheDocument()
  expect(await screen.findByRole('alert')).toHaveTextContent('Service unavailable')
  expect(screen.queryByLabelText('OpenAI credential')).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Test connection' })).not.toBeInTheDocument()
  expect(screen.queryByText('private transport detail')).not.toBeInTheDocument()
})
