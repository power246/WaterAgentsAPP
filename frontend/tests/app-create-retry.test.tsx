import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { api, type Settings } from '../src/api/http'
import { App } from '../src/app/App'

vi.mock('../src/api/sse', () => ({ subscribe: vi.fn() }))
vi.mock('../src/features/analysis/TopologyMap', () => ({ TopologyMap: () => null }))

const profile = `sha256:${'a'.repeat(64)}`
const settings: Settings = {
  schema_version: 1, revision: 0, updated_at: null,
  defaults: {
    analysis_timezone: 'UTC', parser_model: { provider: 'fake', model: 'parser' },
    waterbody_model: { provider: 'fake', model: 'waterbody' },
    memory_mode: 'stateless', time_resolution: '12h',
    desired_embedding_profile_id: profile,
  },
  catalog: {
    models: [
      { selection: { provider: 'fake', model: 'parser' }, label: 'Parser',
        roles: ['query_parser'], available: true, unavailable_reason: null },
      { selection: { provider: 'fake', model: 'waterbody' }, label: 'Waterbody',
        roles: ['waterbody'], available: true, unavailable_reason: null },
    ],
    embedding_profiles: [{ profile_id: profile, label: 'Embedding', available: true,
      unavailable_reason: null }],
    memory_modes: ['stateless'], time_resolutions: ['12h'],
    iana_timezones: ['UTC'], unit_options: [],
  },
  unit_policy: { version: 1, registry_digest: 'a'.repeat(64), entries: [] },
}

afterEach(() => { cleanup(); vi.restoreAllMocks(); window.history.replaceState({}, '', '/') })

it('keeps the exact create request across a Settings revision refresh', async () => {
  vi.spyOn(api, 'settings').mockResolvedValueOnce(settings)
    .mockResolvedValueOnce({ ...settings, revision: 1 })
  vi.spyOn(api, 'keys').mockResolvedValue({ keys: [] })
  const create = vi.spyOn(api, 'create')
    .mockRejectedValueOnce(new Error('response lost'))
    .mockReturnValueOnce(new Promise(() => undefined))
  render(<App />)
  fireEvent.change(await screen.findByRole('textbox', { name: 'Question' }),
    { target: { value: 'Check this river' } })
  fireEvent.click(screen.getByRole('button', { name: 'Start new analysis' }))
  await screen.findByRole('button', { name: 'Retry same submission' })
  fireEvent.click(screen.getByRole('button', { name: 'Settings' }))
  expect(await screen.findByText('Revision 1. Changes affect future runs.')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Close' }))
  expect(screen.getByRole('textbox', { name: 'Question' })).toHaveValue('Check this river')
  fireEvent.click(screen.getByRole('button', { name: 'Retry same submission' }))
  await waitFor(() => expect(create).toHaveBeenCalledTimes(2))
  expect(create.mock.calls[1]?.[0]).toEqual(create.mock.calls[0]?.[0])
})
