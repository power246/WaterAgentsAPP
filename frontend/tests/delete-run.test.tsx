import { afterEach, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { api, type CleanupPreview } from '../src/api/http'
import { DeleteRun } from '../src/features/history/DeleteRun'

const runId = '00000000-0000-4000-8000-000000000001'
const dependency = '00000000-0000-4000-8000-000000000002'
const preview: CleanupPreview = {
  requested_run_ids: [runId], delete_run_ids: [runId, dependency], delete_order: [dependency, runId],
  dependencies: [dependency], cascade_required: true, cascade_authorized: false,
  estimated: { runs: 2, sessions: 3, transcript_items: 12, retained_idempotency_tombstones: 2 },
  preserved: ['api_keys', 'settings', 'unit_policy_versions'], confirmation: null,
}

afterEach(() => vi.restoreAllMocks())

it('requires a fresh whole-run cascade preview and the exact server phrase for one History run', async () => {
  const prepared = vi.spyOn(api, 'previewRunCleanup')
    .mockResolvedValueOnce(preview)
    .mockResolvedValueOnce({ ...preview, cascade_authorized: true, confirmation: {
      token: 'server-token', required_phrase: 'DELETE 2 RUNS', expires_at: '2026-10-08T01:00:00Z',
    } })
  const execute = vi.spyOn(api, 'executeRunCleanup').mockResolvedValue({
    operation_id: '00000000-0000-4000-8000-000000000003', kind: 'history_cleanup',
    state: 'pending', resource_identity: 'history', progress: { processed_items: 0, total_items: 2, succeeded_items: 0, failed_items: 0 },
    safe_result: null, safe_error: null, created_at: '2026-10-08T00:00:00Z', started_at: null, finished_at: null,
  })
  render(<DeleteRun runId={runId} onClose={() => undefined} onAccepted={() => undefined} />)
  expect(await screen.findByText(/2 runs, 3 sessions/)).toBeInTheDocument()
  expect(screen.getByText(dependency)).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Preview whole-run cascade' }))
  await waitFor(() => expect(prepared).toHaveBeenCalledWith(runId, true))
  const button = await screen.findByRole('button', { name: 'Delete these runs' })
  expect(button).toBeDisabled()
  fireEvent.change(screen.getByRole('textbox', { name: 'Confirmation phrase' }), { target: { value: 'DELETE 2 RUNS' } })
  fireEvent.click(button)
  await waitFor(() => expect(execute).toHaveBeenCalledWith('server-token', 'DELETE 2 RUNS'))
  expect(screen.queryByText(/generation deletion/i)).not.toBeInTheDocument()
})
