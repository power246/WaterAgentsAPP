import { useState } from 'react'
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { api, type Settings } from '../src/api/http'
import { NewAnalysisForm } from '../src/features/analysis/NewAnalysisForm'

const settings = {
  revision: 0,
  defaults: {
    analysis_timezone: 'America/Denver',
    parser_model: { provider: 'fake', model: 'parser' },
    waterbody_model: { provider: 'fake', model: 'waterbody' },
    memory_mode: 'stateless', time_resolution: '12h',
    desired_embedding_profile_id: `sha256:${'a'.repeat(64)}`,
  },
  catalog: {
    models: [
      { selection: { provider: 'fake', model: 'parser' }, label: 'Parser', roles: ['query_parser'], available: true },
      { selection: { provider: 'fake', model: 'waterbody' }, label: 'Waterbody', roles: ['waterbody'], available: true },
    ],
    memory_modes: ['stateless'], time_resolutions: ['12h'],
  },
} as Settings

function TargetForm({ initialPoint }: { initialPoint: { latitude: number; longitude: number } | null }) {
  const [mapPoint, setMapPoint] = useState(initialPoint)
  return <>
    <button type="button" onClick={() => setMapPoint({ latitude: 40, longitude: -98 })}>Pick map point</button>
    <NewAnalysisForm settings={settings} mapPoint={mapPoint} onClearMapPoint={() => setMapPoint(null)}
      onCreated={() => undefined} />
  </>
}

afterEach(() => { cleanup(); vi.restoreAllMocks() })

it('replaces a map point with a COMID before creating a run', async () => {
  const create = vi.spyOn(api, 'create').mockResolvedValue({ run_id: 'run', status: 'pending' } as Awaited<ReturnType<typeof api.create>>)
  render(<TargetForm initialPoint={{ latitude: 39, longitude: -97 }} />)
  expect(screen.getByRole('button', { name: 'Clear map point' })).toBeInTheDocument()
  fireEvent.change(screen.getByRole('textbox', { name: 'Target COMID' }), { target: { value: '123' } })
  expect(screen.queryByRole('button', { name: 'Clear map point' })).not.toBeInTheDocument()
  fireEvent.change(screen.getByRole('textbox', { name: 'Question' }), { target: { value: 'Assess the river' } })
  fireEvent.click(screen.getByRole('button', { name: 'Start new analysis' }))
  await waitFor(() => expect(create).toHaveBeenCalledOnce())
  expect(JSON.parse(create.mock.calls[0]?.[0]?.bytes ?? '{}')).toMatchObject({ target: { comid: '123' } })
})

it('replaces typed coordinates with a map point and permits clearing it', async () => {
  const create = vi.spyOn(api, 'create').mockResolvedValue({ run_id: 'run', status: 'pending' } as Awaited<ReturnType<typeof api.create>>)
  render(<TargetForm initialPoint={null} />)
  fireEvent.change(screen.getByRole('spinbutton', { name: 'Latitude' }), { target: { value: '39' } })
  fireEvent.change(screen.getByRole('spinbutton', { name: 'Longitude' }), { target: { value: '-97' } })
  fireEvent.click(screen.getByRole('button', { name: 'Pick map point' }))
  expect(screen.getByRole('spinbutton', { name: 'Latitude' })).toHaveValue(null)
  expect(screen.getByRole('spinbutton', { name: 'Longitude' })).toHaveValue(null)
  expect(screen.getByRole('button', { name: 'Clear map point' })).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Clear map point' }))
  fireEvent.change(screen.getByRole('spinbutton', { name: 'Latitude' }), { target: { value: '38' } })
  fireEvent.change(screen.getByRole('spinbutton', { name: 'Longitude' }), { target: { value: '-96' } })
  fireEvent.change(screen.getByRole('textbox', { name: 'Question' }), { target: { value: 'Assess the river' } })
  fireEvent.click(screen.getByRole('button', { name: 'Start new analysis' }))
  await waitFor(() => expect(create).toHaveBeenCalledOnce())
  expect(JSON.parse(create.mock.calls[0]?.[0]?.bytes ?? '{}')).toMatchObject({
    target: { coordinates: { latitude: 38, longitude: -96 } },
  })
})
