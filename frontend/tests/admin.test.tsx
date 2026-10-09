import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { api, ApiError, type AdminOperation, type AdminRag, type AdminStorage, type CleanupPreview, type RunHistory } from '../src/api/http'
import { AdminPage } from '../src/features/admin/AdminPage'

const runId = '00000000-0000-4000-8000-000000000001'
const dependency = '00000000-0000-4000-8000-000000000002'
const time = '2026-10-08T00:00:00Z'
const storage: AdminStorage = {
  history: { state: 'available' }, rag_index: { state: 'available' },
  evidence_cache: { state: 'not_present' }, credential_store: { state: 'available' },
}
const rag: AdminRag = {
  index: { active_generation_id: null, generations: [] },
  handoff: { desired_profile_digest: 'a'.repeat(64), active_generation_id: null,
    building_generation_id: null, blocking_reader_count: 0, old_credential_cleanup_ready: false,
    phase: 'build_required', retired_generation_ids: [] },
}
const history: RunHistory = { items: [{ run_id: runId, query: 'Saved river', status: 'completed',
  created_at: time, updated_at: time, configuration: null, context_summary: null,
  result_ref: null, result_summary: null, issues: [] }], next_cursor: null }
const preview: CleanupPreview = {
  requested_run_ids: [runId], delete_run_ids: [runId, dependency], delete_order: [dependency, runId],
  dependencies: [dependency], cascade_required: true, cascade_authorized: false,
  estimated: { runs: 2, sessions: 3, transcript_items: 12, retained_idempotency_tombstones: 2 },
  preserved: ['api_keys', 'settings', 'unit_policy_versions'], confirmation: null,
}
const operation: AdminOperation = {
  operation_id: '00000000-0000-4000-8000-000000000003', kind: 'history_cleanup', state: 'pending',
  resource_identity: 'history:selection', progress: { processed_items: 0, total_items: 2,
    succeeded_items: 0, failed_items: 0 }, safe_result: null, safe_error: null,
  created_at: time, started_at: null, finished_at: null,
}

function setup(list: AdminOperation[] = []) {
  vi.spyOn(api, 'adminStorage').mockResolvedValue(storage)
  vi.spyOn(api, 'adminRag').mockResolvedValue(rag)
  vi.spyOn(api, 'operations').mockResolvedValue({ items: list, next_cursor: null })
  vi.spyOn(api, 'history').mockResolvedValue(history)
  return render(<MemoryRouter><AdminPage /></MemoryRouter>)
}

afterEach(() => { cleanup(); vi.restoreAllMocks() })

it('requires second dependency preview and server phrase, then tracks accepted operation', async () => {
  const prepare = vi.spyOn(api, 'previewHistoryCleanup').mockResolvedValueOnce(preview)
    .mockResolvedValueOnce({ ...preview, cascade_authorized: true, confirmation: {
      token: 'server-token', required_phrase: 'DELETE 2 RUNS', expires_at: '2026-10-08T01:00:00Z',
    } })
  const execute = vi.spyOn(api, 'executeAdmin').mockResolvedValue(operation)
  vi.spyOn(api, 'operation').mockResolvedValue({ ...operation, state: 'succeeded', finished_at: time })
  setup()
  await screen.findByText(/Saved river/)
  fireEvent.click(screen.getByRole('checkbox', { name: /Saved river/ }))
  fireEvent.click(screen.getByRole('button', { name: 'Preview history cleanup' }))
  await waitFor(() => expect(prepare).toHaveBeenCalledWith({ kind: 'runs', run_ids: [runId] }, false))
  expect(await screen.findByText(/2 tombstones retained/)).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /evidence cache/i })).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Preview whole-run cascade' }))
  await waitFor(() => expect(prepare).toHaveBeenCalledWith({ kind: 'runs', run_ids: [runId] }, true))
  const confirm = await screen.findByRole('button', { name: 'Execute confirmed operation' })
  expect(confirm).toBeDisabled()
  fireEvent.change(screen.getByRole('textbox', { name: 'Admin confirmation phrase' }), { target: { value: 'DELETE 2 RUNS' } })
  fireEvent.click(confirm)
  await waitFor(() => expect(execute).toHaveBeenCalledWith('history_cleanup', expect.objectContaining({
    key: expect.any(String), bytes: JSON.stringify({ confirmation_token: 'server-token', confirmation_phrase: 'DELETE 2 RUNS' }),
  }), undefined))
  expect(await screen.findByText(/accepted. You may leave this page/)).toBeInTheDocument()
})

it('replays the exact Admin request after an unknown response', async () => {
  vi.spyOn(api, 'previewHistoryCleanup').mockResolvedValue({ ...preview, cascade_authorized: true,
    confirmation: { token: 'server-token', required_phrase: 'DELETE 2 RUNS', expires_at: time } })
  const execute = vi.spyOn(api, 'executeAdmin').mockRejectedValueOnce(new Error('response lost'))
    .mockResolvedValueOnce(operation)
  setup()
  await screen.findByText(/Saved river/)
  fireEvent.click(screen.getByRole('checkbox', { name: /Saved river/ }))
  fireEvent.click(screen.getByRole('button', { name: 'Preview history cleanup' }))
  await screen.findByRole('textbox', { name: 'Admin confirmation phrase' })
  fireEvent.change(screen.getByRole('textbox', { name: 'Admin confirmation phrase' }), { target: { value: 'DELETE 2 RUNS' } })
  fireEvent.click(screen.getByRole('button', { name: 'Execute confirmed operation' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Response unknown')
  fireEvent.click(screen.getByRole('button', { name: 'Retry confirmed operation' }))
  await waitFor(() => expect(execute).toHaveBeenCalledTimes(2))
  expect(execute.mock.calls[1]).toEqual(execute.mock.calls[0])
  expect(await screen.findByText(/accepted. You may leave this page/)).toBeInTheDocument()
})

it('resumes an active operation from server list and stops polling at terminal', async () => {
  const get = vi.spyOn(api, 'operation').mockResolvedValue({ ...operation, state: 'succeeded', finished_at: time })
  setup([operation])
  expect(await screen.findByText(/history_cleanup · succeeded/)).toBeInTheDocument()
  expect(get).toHaveBeenCalledWith(operation.operation_id, expect.any(AbortSignal))
  const count = get.mock.calls.length
  await new Promise((resolve) => setTimeout(resolve, 25))
  expect(get).toHaveBeenCalledTimes(count)
})

it.each(['admin_preview_stale', 'admin_preview_expired', 'admin_operation_conflict'])(
  'invalidates %s confirmation instead of repeating the mutation', async (code) => {
  vi.spyOn(api, 'previewHistoryCleanup').mockResolvedValue({ ...preview, cascade_authorized: true,
    confirmation: { token: 'stale-token', required_phrase: 'DELETE 2 RUNS', expires_at: time } })
  const execute = vi.spyOn(api, 'executeAdmin').mockRejectedValue(new ApiError(409, code, 'request'))
  setup()
  await screen.findByText(/Saved river/)
  fireEvent.click(screen.getByRole('checkbox', { name: /Saved river/ }))
  fireEvent.click(screen.getByRole('button', { name: 'Preview history cleanup' }))
  await screen.findByRole('textbox', { name: 'Admin confirmation phrase' })
  fireEvent.change(screen.getByRole('textbox', { name: 'Admin confirmation phrase' }), { target: { value: 'DELETE 2 RUNS' } })
  fireEvent.click(screen.getByRole('button', { name: 'Execute confirmed operation' }))
  expect(await screen.findByRole('alert')).toHaveTextContent(code)
  expect(screen.queryByRole('textbox', { name: 'Admin confirmation phrase' })).not.toBeInTheDocument()
  expect(execute).toHaveBeenCalledTimes(1)
  },
)

it('does not offer a second execute while the same kind of operation is active', async () => {
  vi.spyOn(api, 'previewHistoryCleanup').mockResolvedValue({ ...preview, cascade_authorized: true,
    confirmation: { token: 'server-token', required_phrase: 'DELETE 2 RUNS', expires_at: time } })
  vi.spyOn(api, 'operation').mockResolvedValue(operation)
  const execute = vi.spyOn(api, 'executeAdmin')
  setup([operation])
  await screen.findByText(/Saved river/)
  fireEvent.click(screen.getByRole('checkbox', { name: /Saved river/ }))
  fireEvent.click(screen.getByRole('button', { name: 'Preview history cleanup' }))
  await screen.findByRole('textbox', { name: 'Admin confirmation phrase' })
  fireEvent.change(screen.getByRole('textbox', { name: 'Admin confirmation phrase' }), { target: { value: 'DELETE 2 RUNS' } })
  expect(screen.getByRole('button', { name: 'Execute confirmed operation' })).toBeDisabled()
  expect(execute).not.toHaveBeenCalled()
})
