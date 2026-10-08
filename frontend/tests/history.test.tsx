import { afterEach, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { api, ApiError, type RunHistory } from '../src/api/http'
import { HistoryList } from '../src/features/history/HistoryList'

const time = '2026-10-08T00:00:00Z'
const page: RunHistory = {
  items: [{
    run_id: '00000000-0000-4000-8000-000000000001', query: 'Same query',
    status: 'completed', created_at: time, updated_at: time,
    context_summary: null, result_ref: null, result_summary: null,
    configuration: null, issues: [],
  }],
  next_cursor: 'opaque:do-not-parse',
}

afterEach(() => vi.restoreAllMocks())

it('passes the opaque cursor unchanged and keeps saved identity/timezone', async () => {
  const history = vi.spyOn(api, 'history')
    .mockResolvedValueOnce(page)
    .mockResolvedValueOnce({ items: [{ ...page.items![0]!, run_id: '00000000-0000-4000-8000-000000000002', configuration: null }], next_cursor: null })
  const select = vi.fn()
  render(<HistoryList selectedRunId={null} onSelect={select} refreshVersion={0} />)
  await screen.findByText(/COMID unresolved · legacy\/unknown/)
  fireEvent.click(screen.getByRole('button', { name: 'Load more' }))
  await waitFor(() => expect(history).toHaveBeenCalledWith('opaque:do-not-parse', undefined))
  expect(screen.getAllByText('Same query')).toHaveLength(2)
  fireEvent.click(screen.getAllByText('Same query')[1]!)
  expect(select).toHaveBeenCalledWith('00000000-0000-4000-8000-000000000002')
  expect(screen.queryByText(/total runs/i)).not.toBeInTheDocument()
})

it('shows an invalid cursor as an error rather than an empty page', async () => {
  vi.spyOn(api, 'history').mockRejectedValue(new ApiError(422, 'invalid_history_cursor', 'request'))
  render(<HistoryList selectedRunId={null} onSelect={() => undefined} refreshVersion={0} />)
  expect(await screen.findByRole('alert')).toHaveTextContent('History cursor is no longer valid')
  expect(screen.queryByText('No saved runs.')).not.toBeInTheDocument()
})
