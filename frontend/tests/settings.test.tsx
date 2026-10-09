import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
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
