import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, expect, it, vi } from 'vitest'
import { api, ApiError, ApiClient } from '../src/api/http'
import type { GuidelineDocument, GuidelineReceipt } from '../src/api/guidelineContracts'
import { GuidelinesPage } from '../src/features/guidelines/GuidelinesPage'
import { GuidelineViewer, safeSourceUrl } from '../src/features/guidelines/GuidelineViewer'
import { RunGuidelines, selectRunGuidelines } from '../src/features/guidelines/RunGuidelines'
import { useGuidelineEditor } from '../src/features/guidelines/useGuidelineEditor'
import { document, draft, id, knowledgeRecords, operation, receipt, result, runId, sessionId, version } from './guideline-fixtures'

function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>((done) => { resolve = done }); return { promise, resolve } }
function setup(doc = document) {
  vi.spyOn(api, 'guidelines').mockResolvedValue({ items: [], next_cursor: null })
  const get = vi.spyOn(api, 'guideline').mockResolvedValue(doc)
  render(<RouterProvider router={createMemoryRouter([{ path: '/', element: <GuidelinesPage /> }])} />)
  return get
}
function fill() {
  fireEvent.change(screen.getByLabelText('标题'), { target: { value: draft.title } })
  fireEvent.change(screen.getByLabelText('简介'), { target: { value: draft.summary } })
  fireEvent.change(screen.getByLabelText(/适用指标（/), { target: { value: 'usgs:00400' } })
  fireEvent.change(screen.getByLabelText('正文'), { target: { value: draft.body_markdown } })
}
async function createSaved() {
  fill(); fireEvent.click(screen.getByRole('button', { name: '保存草稿' }))
  await screen.findByRole('heading', { name: '编辑说明书' })
}
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers() })

it('queries both libraries with state, text and keyset pagination', async () => {
  setup()
  vi.mocked(api.guidelines).mockResolvedValue({ items: [], next_cursor: 'cursor-1' })
  fireEvent.click(screen.getByRole('button', { name: '我的说明书' }))
  await waitFor(() => expect(api.guidelines).toHaveBeenLastCalledWith({ source: 'mine', q: '', state: '', cursor: '' }, expect.any(AbortSignal)))
  fireEvent.change(screen.getByLabelText('查找说明书'), { target: { value: '酸碱' } })
  fireEvent.click(screen.getByRole('button', { name: '查询' }))
  fireEvent.change(screen.getByLabelText('状态筛选'), { target: { value: 'available' } })
  fireEvent.click(await screen.findByRole('button', { name: '下一页' }))
  await waitFor(() => expect(api.guidelines).toHaveBeenLastCalledWith({ source: 'mine', q: '酸碱', state: 'available', cursor: 'cursor-1' }, expect.any(AbortSignal)))
})

it('saves an authored draft with no sources and requires inspection of the acknowledged version', async () => {
  setup()
  const mutate = vi.spyOn(api, 'mutateGuideline').mockImplementation(async (_path, _method, request) => receipt(request.key))
  fill(); fireEvent.click(screen.getByRole('button', { name: '检查并发布' }))
  const review = await screen.findByRole('dialog', { name: '检查保存版本并发布' })
  expect(mutate).toHaveBeenCalledOnce()
  expect(JSON.parse(mutate.mock.calls[0]![2].bytes).sources).toEqual([])
  expect(within(review).getByText('用户编写，无外部出处')).toBeVisible()
  expect(within(review).getByRole('button', { name: '确认发布此保存版本' })).toBeDisabled()
  fireEvent.change(within(review).getByLabelText('检查人'), { target: { value: '本地检查人' } })
  fireEvent.change(within(review).getByLabelText('检查说明'), { target: { value: '合成测试确认，不是专业批准' } })
  fireEvent.click(within(review).getByRole('checkbox'))
  fireEvent.click(within(review).getByRole('button', { name: '确认发布此保存版本' }))
  await waitFor(() => expect(mutate).toHaveBeenCalledTimes(2))
  expect(mutate.mock.calls[1]![0]).toBe(`/api/guidelines/${id}/publish`)
  expect(JSON.parse(mutate.mock.calls[1]![2].bytes)).toMatchObject({ expected_revision: 1, content_digest: document.draft_digest, review_confirmed: true })
})

it('requires sources for reference-derived publication and provides source metadata fields', () => {
  setup(); fill()
  fireEvent.change(screen.getByLabelText('来源性质'), { target: { value: 'reference_derived' } })
  expect(screen.getByRole('button', { name: '检查并发布' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: '添加出处' }))
  fireEvent.change(screen.getByLabelText('出处标题'), { target: { value: '第一方资料' } })
  fireEvent.change(screen.getByLabelText('出处网址'), { target: { value: 'javascript:alert(1)' } })
  expect(screen.getByRole('button', { name: '检查并发布' })).toBeDisabled()
  fireEvent.change(screen.getByLabelText('出处网址'), { target: { value: 'https://example.org/source' } })
  expect(screen.getByRole('button', { name: '检查并发布' })).toBeEnabled()
})

it('lets a long draft be saved while preventing publication beyond the UTF-8 body limit', () => {
  setup(); fill()
  fireEvent.change(screen.getByLabelText('正文'), { target: { value: '中'.repeat(2001) } })
  expect(screen.getByRole('button', { name: '保存草稿' })).toBeEnabled()
  expect(screen.getByRole('button', { name: '检查并发布' })).toBeDisabled()
  expect(screen.getByText(/正文超过发布长度上限/)).toBeVisible()
})

it('replays an uncertain publication with the reviewed key and keeps newer text dirty', async () => {
  const get = setup()
  const mutate = vi.spyOn(api, 'mutateGuideline').mockImplementation(async (_path, _method, request) => receipt(request.key))
  await createSaved()
  fireEvent.click(screen.getByRole('button', { name: '检查并发布' }))
  const review = await screen.findByRole('dialog')
  fireEvent.change(within(review).getByLabelText('检查人'), { target: { value: 'fixture' } })
  fireEvent.change(within(review).getByLabelText('检查说明'), { target: { value: 'fixture approval only' } })
  fireEvent.click(within(review).getByRole('checkbox'))
  mutate.mockRejectedValueOnce(new ApiError(202, 'invalid_json', null))
  fireEvent.click(within(review).getByRole('button', { name: '确认发布此保存版本' }))
  await screen.findByText(/请求结果不确定/)
  fireEvent.change(screen.getByLabelText('正文'), { target: { value: 'B after publication with lost ACK' } })
  get.mockResolvedValue({ ...document, revision: 2, state: 'available', active_version: version })
  fireEvent.click(screen.getByRole('button', { name: '使用原请求对账' }))
  await screen.findByText('我的说明书 · 可用于分析')
  expect(mutate.mock.calls[1]![2]).toEqual(mutate.mock.calls[2]![2])
  expect(mutate.mock.calls[2]![0]).toBe(`/api/guidelines/${id}/publish`)
  expect(screen.getByLabelText('正文')).toHaveValue('B after publication with lost ACK')
  expect(screen.getByText('有未保存编辑')).toBeVisible()
})

it('preserves B dirty when save A ACK arrives after typing B', async () => {
  setup()
  const pending = deferred<GuidelineReceipt>()
  const mutate = vi.spyOn(api, 'mutateGuideline').mockReturnValue(pending.promise)
  fill(); fireEvent.click(screen.getByRole('button', { name: '保存草稿' }))
  fireEvent.change(screen.getByLabelText('正文'), { target: { value: 'B newer input' } })
  await act(async () => pending.resolve(receipt(mutate.mock.calls[0]![2].key)))
  await screen.findByRole('heading', { name: '编辑说明书' })
  expect(screen.getByLabelText('正文')).toHaveValue('B newer input')
  expect(screen.getByText('有未保存编辑')).toBeVisible()
  expect(screen.getByRole('button', { name: '保存草稿' })).toBeEnabled()
})

it.each(['lost', 'invalid_json', 'invalid_response', '503'])('reconciles %s using the exact original key and bytes', async (mode) => {
  setup()
  const failure = mode === 'lost' ? new TypeError('lost response') : new ApiError(mode === '503' ? 503 : 201, mode, null)
  const mutate = vi.spyOn(api, 'mutateGuideline').mockRejectedValueOnce(failure)
    .mockImplementation(async (_path, _method, request) => receipt(request.key))
  fill(); fireEvent.click(screen.getByRole('button', { name: '保存草稿' }))
  await screen.findByText(/请求结果不确定/)
  fireEvent.change(screen.getByLabelText('正文'), { target: { value: 'preserved after uncertainty' } })
  expect(screen.getByRole('button', { name: '保存草稿' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: '使用原请求对账' }))
  await screen.findByRole('heading', { name: '编辑说明书' })
  expect(mutate.mock.calls[1]![2]).toEqual(mutate.mock.calls[0]![2])
  expect(screen.getByLabelText('正文')).toHaveValue('preserved after uncertainty')
})

it('preserves local editing on 409 until an explicit revision decision', async () => {
  const get = setup()
  const mutate = vi.spyOn(api, 'mutateGuideline').mockImplementation(async (_path, _method, request) => receipt(request.key))
  await createSaved()
  fireEvent.change(screen.getByLabelText('正文'), { target: { value: 'local B' } })
  const newer = { ...document, revision: 5, draft: { ...draft, body_markdown: 'remote C' } }
  mutate.mockRejectedValueOnce(new ApiError(409, 'guideline_revision_conflict', null, null, null, newer))
  fireEvent.click(screen.getByRole('button', { name: '保存草稿' }))
  await screen.findByText(/服务器已有新修订 5/)
  expect(screen.getByLabelText('正文')).toHaveValue('local B')
  fireEvent.click(screen.getByRole('button', { name: '保留本地编辑，采用最新修订' }))
  get.mockResolvedValue({ ...newer, revision: 6, draft: { ...draft, body_markdown: 'local B' } })
  fireEvent.click(screen.getByRole('button', { name: '保存草稿' }))
  await waitFor(() => expect(mutate).toHaveBeenCalledTimes(3))
  expect(JSON.parse(mutate.mock.calls[2]![2].bytes).expected_revision).toBe(5)
})

it('edits bundled through local-edit and continues the existing override draft', async () => {
  const bundledVersion = { ...version, identity: { ...version.identity, origin: 'bundled' as const } }
  const bundled: GuidelineDocument = { ...document, identity: bundledVersion.identity, draft: null,
    active_version: bundledVersion, latest_bundled_version: bundledVersion, state: 'available' }
  const get = setup(bundled)
  vi.mocked(api.guidelines).mockResolvedValue({ items: [{ ...bundled, title: '内置机制', summary: '', source_kind: 'user_authored',
    active_version_id: version.version_id, pending_version_id: null, draft_digest: null, effective_version_id: version.version_id,
    latest_bundled_version_id: version.version_id }], next_cursor: null })
  fireEvent.click(screen.getByRole('button', { name: '我的说明书' }))
  fireEvent.click(await screen.findByRole('button', { name: '内置机制' }))
  await screen.findByRole('button', { name: '在本机编辑' })
  const override: GuidelineDocument = { ...document, identity: { ...document.identity, guideline_id: 'override', origin: 'local_override' }, draft: { ...draft, body_markdown: 'existing local content' } }
  get.mockImplementation(async (selected) => selected === 'override' ? override : bundled)
  const mutate = vi.spyOn(api, 'mutateGuideline').mockImplementation(async (_path, _method, request) => receipt(request.key, 1, 'local_edit', 'override'))
  fireEvent.click(screen.getByRole('button', { name: '在本机编辑' }))
  await screen.findByRole('heading', { name: '编辑说明书' })
  expect(screen.getByLabelText('正文')).toHaveValue('existing local content')
  expect(mutate.mock.calls[0]![0]).toBe(`/api/guidelines/${id}/local-edit`)
  expect(JSON.parse(mutate.mock.calls[0]![2].bytes)).toMatchObject({ base_version_id: version.version_id, base_content_digest: version.model_view.content_digest })
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
  fireEvent.change(screen.getByLabelText('正文'), { target: { value: 'still editing override' } })
  mutate.mockImplementation(async (_path, _method, request) => receipt(request.key, 1, 'restore_bundled'))
  fireEvent.click(await screen.findByRole('button', { name: '恢复程序自带版本' }))
  await waitFor(() => expect(mutate).toHaveBeenCalledTimes(2))
  expect(confirm).toHaveBeenCalledWith(expect.stringContaining(version.version_id))
  expect(JSON.parse(mutate.mock.calls[1]![2].bytes)).toEqual({ expected_revision: 1, version_id: version.version_id })
  expect(screen.getByLabelText('正文')).toHaveValue('still editing override')
})

it('keeps later edits dirty while an acknowledged publication is preparing and polls safely', async () => {
  const get = setup()
  vi.spyOn(api, 'mutateGuideline').mockImplementation(async (_path, _method, request) => receipt(request.key))
  await createSaved()
  get.mockResolvedValue({ ...document, revision: 2, state: 'preparing', preparation: operation('pending'), pending_version: version })
  fireEvent.click(screen.getByRole('button', { name: '检查并发布' }))
  const review = await screen.findByRole('dialog')
  fireEvent.change(within(review).getByLabelText('检查人'), { target: { value: 'fixture' } })
  fireEvent.change(within(review).getByLabelText('检查说明'), { target: { value: 'fixture approval' } })
  fireEvent.click(within(review).getByRole('checkbox'))
  fireEvent.click(within(review).getByRole('button', { name: '确认发布此保存版本' }))
  await screen.findByText(/正在准备已批准版本/)
  fireEvent.change(screen.getByLabelText('正文'), { target: { value: 'later draft B' } })
  vi.spyOn(api, 'guidelineOperation').mockResolvedValue(operation('succeeded'))
  get.mockResolvedValue({ ...document, revision: 2, state: 'available', preparation: operation('succeeded'), active_version: version, has_unpublished_changes: true })
  await screen.findByText('我的说明书 · 可用于分析', {}, { timeout: 4000 })
  expect(screen.getByLabelText('正文')).toHaveValue('later draft B')
  expect(screen.getByText('有未保存编辑')).toBeVisible()
})

it('renders Markdown as inert source and allows only credential-free HTTP(S) source links', () => {
  render(<GuidelineViewer content={{ ...draft, body_markdown: '<script>window.evil=1</script>\n[x](javascript:alert(1))', sources: [
    { source_id: 'bad', title: 'bad link', url: 'javascript:alert(1)' }, { source_id: 'good', title: 'safe link', url: 'https://example.org/source' }] }} />)
  expect(globalThis.document.querySelector('script')).toBeNull()
  expect(screen.getByText(/<script>/)).toBeVisible()
  expect(screen.queryByRole('link', { name: 'bad link' })).not.toBeInTheDocument()
  expect(screen.getByRole('link', { name: 'safe link' })).toHaveAttribute('rel', 'noopener noreferrer')
  expect(safeSourceUrl('https://user:secret@example.org')).toBeNull()
  expect(safeSourceUrl('data:text/html,test')).toBeNull()
  expect(safeSourceUrl('https://example.org/?access_token=private')).toBeNull()
  expect(safeSourceUrl(' https://example.org')).toBeNull()
})

it('shows only provided saved snapshots and follows explicit result usage in history', async () => {
  const items = knowledgeRecords()
  const candidate = { ...items[0]!, item_id: 'candidate', content_type: 'guideline_retrieval' }
  render(<RunGuidelines items={[...items, candidate]} runId={runId} sessionId={sessionId} result={result} readView="history" />)
  expect(screen.getAllByText(/查看提供的说明书/)).toHaveLength(1)
  fireEvent.click(screen.getByText(/查看参考说明书/))
  expect(screen.getAllByText('机制 A 原文与适用边界。').length).toBeGreaterThan(0)
  const mutated = structuredClone(items)
  if (mutated[1]?.content.kind === 'inline') (mutated[1].content.body as { citations: { version_id: string }[] }).citations[0]!.version_id = 'fabricated'
  expect(selectRunGuidelines(mutated, new Map(), runId, sessionId, result).used).toEqual([])
  expect(selectRunGuidelines(items.slice(0, 1), new Map(), runId, sessionId, result).used).toEqual([])
})

it('hydrates a saved reference through history content API and never reads the current library', async () => {
  const items = knowledgeRecords('numeric_estimation'), saved = items[0]!.content
  items[0] = { ...items[0]!, content: { kind: 'reference', preview_head: 'not JSON', preview_tail: '', truncated: true } }
  const get = vi.spyOn(api, 'guideline')
  const content = vi.spyOn(api, 'content').mockResolvedValue({ run_id: runId, item_id: 'delivery', text: JSON.stringify(saved.kind === 'inline' ? saved.body : null),
    content_type: 'guideline_delivery', content_schema_version: 1, content_bytes: 100, issues: [], source: 'postgresql' })
  render(<RunGuidelines items={items} runId={runId} sessionId={sessionId} result={result} readView="history" />)
  await screen.findByText(/查看参考说明书/)
  expect(content).toHaveBeenCalledWith(runId, 'delivery', 'history', expect.any(AbortSignal))
  expect(get).not.toHaveBeenCalled()
  expect(screen.getByText(/查看参考说明书/)).toHaveTextContent('数值估值')
})

it('validates management responses and retains a conflict snapshot on ApiError', async () => {
  const client = new ApiClient(vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: { code: 'guideline_revision_conflict' }, current_document: document }),
    { status: 409, headers: { 'Content-Type': 'application/json' } })))
  await expect(client.guideline(id)).rejects.toMatchObject({ currentDocument: document })
  const bad = new ApiClient(vi.fn().mockResolvedValue(new Response('{"operation_id":"fake"}', { headers: { 'Content-Type': 'application/json' } })))
  await expect(bad.mutateGuideline('/api/guidelines', 'POST', { key: 'same', bytes: '{}' })).rejects.toMatchObject({ code: 'invalid_response' })
  const foreign = new ApiClient(vi.fn().mockResolvedValue(new Response(JSON.stringify(document), { headers: { 'Content-Type': 'application/json' } })))
  await expect(foreign.guideline('different-document')).rejects.toMatchObject({ code: 'invalid_response' })
  const wrongOperation = new ApiClient(vi.fn().mockResolvedValue(new Response(JSON.stringify(operation('succeeded')), { headers: { 'Content-Type': 'application/json' } })))
  await expect(wrongOperation.guidelineOperation('different-operation')).rejects.toMatchObject({ code: 'invalid_response' })
  const malformed = new ApiClient(vi.fn().mockResolvedValue(new Response(JSON.stringify({ ...document,
    identity: { ...document.identity, origin: ['local_custom'] } }), { headers: { 'Content-Type': 'application/json' } })))
  await expect(malformed.guideline(id)).rejects.toMatchObject({ code: 'invalid_response' })
})

it('retries a failed approved version without another review or publication', async () => {
  const get = setup({ ...document, state: 'preparation_failed', preparation: operation('failed'), pending_version: version })
  const mutate = vi.spyOn(api, 'mutateGuideline').mockImplementation(async (_path, _method, request) => receipt(request.key))
  await createSaved()
  expect(screen.getByText(/guideline_embedding_unavailable/)).toBeVisible()
  get.mockResolvedValue({ ...document, state: 'available', active_version: version, preparation: operation('succeeded') })
  fireEvent.click(screen.getByRole('button', { name: '重试准备' }))
  await screen.findByText('我的说明书 · 可用于分析')
  expect(mutate.mock.calls[1]![0]).toBe(`/api/guidelines/operations/${operation('failed').receipt.operation_id}/retry`)
  expect(JSON.parse(mutate.mock.calls[1]![2].bytes)).toEqual({ expected_revision: 1 })
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
})

function EditorHarness() {
  const e = useGuidelineEditor(() => undefined)
  return <><button onClick={() => void e.load(id)}>load</button>
    <button onClick={() => void e.save()}>save</button>
    <input aria-label="harness body" value={e.state.draft.body_markdown ?? ''} onChange={(event) => e.edit({ ...e.state.draft, body_markdown: event.target.value })} />
    <button onClick={() => void e.mutate('/api/guidelines/bundled-root/set-enabled', { expected_revision: 3, enabled: false })}>disable</button>
    <button onClick={() => void e.mutate('/api/guidelines/bundled-root/restore-bundled', { expected_revision: 3, version_id: version.version_id })}>restore</button>
    <output aria-label="harness state">{JSON.stringify({ id: e.state.document?.identity.guideline_id, state: e.state.document?.state, dirty: e.dirty })}</output></>
}

it.each(['disable', 'restore'])('ignores an old operation after %s and keeps override edits with the selected document', async (action) => {
  vi.useFakeTimers()
  const original: GuidelineDocument = { ...document, identity: { ...document.identity, origin: 'local_override', family_root_id: 'bundled-root' },
    state: 'preparing', preparation: operation('pending') }
  const get = vi.spyOn(api, 'guideline').mockResolvedValue(original)
  const old = deferred<ReturnType<typeof operation>>()
  const poll = vi.spyOn(api, 'guidelineOperation').mockReturnValue(old.promise)
  vi.spyOn(api, 'mutateGuideline').mockImplementation(async (_path, _method, request) => receipt(request.key, 4, 'set_enabled', 'bundled-root'))
  const rendered = render(<EditorHarness />)
  await act(async () => fireEvent.click(screen.getByText('load')))
  await act(async () => vi.advanceTimersByTimeAsync(1200))
  expect(poll).toHaveBeenCalledOnce()
  const signal = poll.mock.calls[0]![1]!
  fireEvent.change(screen.getByLabelText('harness body'), { target: { value: 'B must survive family action' } })
  get.mockImplementation(async (selected) => selected === 'bundled-root'
    ? { ...document, identity: { ...document.identity, guideline_id: 'bundled-root', family_root_id: 'bundled-root', origin: 'bundled' }, revision: 4 }
    : { ...original, state: action === 'disable' ? 'disabled' : 'draft', preparation: operation('superseded') })
  await act(async () => fireEvent.click(screen.getByText(action)))
  expect(signal.aborted).toBe(true)
  get.mockResolvedValue({ ...original, state: 'available', active_version: version, preparation: operation('succeeded') })
  await act(async () => old.resolve(operation('succeeded')))
  expect(screen.getByLabelText('harness body')).toHaveValue('B must survive family action')
  expect(screen.getByLabelText('harness state')).toHaveTextContent(`"id":"${id}"`)
  expect(screen.getByLabelText('harness state')).toHaveTextContent(action === 'disable' ? 'disabled' : 'draft')
  expect(screen.getByLabelText('harness state')).toHaveTextContent('"dirty":true')
  rendered.unmount()
  await act(async () => vi.advanceTimersByTimeAsync(5000))
  expect(poll).toHaveBeenCalledOnce()
})

it('retains B when a post-save GET is stale, then reconciles the original save receipt', async () => {
  const get = setup({ ...document, revision: 1 })
  const ack = deferred<GuidelineReceipt>()
  const mutate = vi.spyOn(api, 'mutateGuideline').mockReturnValueOnce(ack.promise)
    .mockImplementation(async (_path, _method, request) => receipt(request.key, 2))
  fill(); fireEvent.click(screen.getByRole('button', { name: '保存草稿' }))
  fireEvent.change(screen.getByLabelText('正文'), { target: { value: 'new B during stale GET' } })
  await act(async () => ack.resolve(receipt(mutate.mock.calls[0]![2].key, 2)))
  await screen.findByText(/请求结果不确定/)
  get.mockResolvedValue({ ...document, revision: 2 })
  fireEvent.click(screen.getByRole('button', { name: '使用原请求对账' }))
  await screen.findByRole('heading', { name: '编辑说明书' })
  expect(mutate.mock.calls[1]![2]).toEqual(mutate.mock.calls[0]![2])
  expect(screen.getByLabelText('正文')).toHaveValue('new B during stale GET')
  expect(screen.getByText('有未保存编辑')).toBeVisible()
})

it.each([403, 404, 409, 422])('keeps an accepted save reconcilable when its follow-up read returns %s', async (status) => {
  const get = setup()
  const ack = deferred<GuidelineReceipt>()
  const mutate = vi.spyOn(api, 'mutateGuideline').mockReturnValueOnce(ack.promise)
    .mockImplementation(async (_path, _method, request) => receipt(request.key))
  get.mockRejectedValueOnce(new ApiError(status, 'read_rejected', null))
  fill(); fireEvent.click(screen.getByRole('button', { name: '保存草稿' }))
  fireEvent.change(screen.getByLabelText('正文'), { target: { value: 'B typed before accepted A is read' } })
  await act(async () => ack.resolve(receipt(mutate.mock.calls[0]![2].key)))
  await screen.findByText(/请求结果不确定/)
  expect(screen.getByRole('button', { name: '保存草稿' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: '使用原请求对账' }))
  await screen.findByRole('heading', { name: '编辑说明书' })
  expect(mutate.mock.calls[1]![2]).toEqual(mutate.mock.calls[0]![2])
  expect(screen.getByLabelText('正文')).toHaveValue('B typed before accepted A is read')
  expect(screen.getByText('有未保存编辑')).toBeVisible()
})

it('marks retained local content dirty when an operation poll reads another client\'s saved draft', async () => {
  vi.useFakeTimers()
  const original: GuidelineDocument = { ...document, revision: 2, state: 'preparing', preparation: operation('pending') }
  const get = vi.spyOn(api, 'guideline').mockResolvedValue(original)
  const polled = deferred<GuidelineDocument>()
  vi.spyOn(api, 'guidelineOperation').mockResolvedValue(operation('succeeded'))
  const mutate = vi.spyOn(api, 'mutateGuideline').mockImplementation(async (_path, _method, request) => receipt(request.key, 4))
  const rendered = render(<EditorHarness />)
  await act(async () => fireEvent.click(screen.getByText('load')))
  expect(screen.getByLabelText('harness state')).toHaveTextContent('"dirty":false')
  get.mockReturnValueOnce(polled.promise)
  await act(async () => vi.advanceTimersByTimeAsync(1200))
  expect(get).toHaveBeenCalledTimes(2)
  const remote = { ...original, revision: 3, state: 'available' as const, preparation: operation('succeeded'),
    draft_digest: 'b'.repeat(64), draft: { ...draft, body_markdown: 'C saved in another client' } }
  await act(async () => polled.resolve(remote))
  expect(screen.getByLabelText('harness body')).toHaveValue(draft.body_markdown)
  expect(screen.getByLabelText('harness state')).toHaveTextContent('"dirty":true')
  get.mockResolvedValue({ ...remote, revision: 4, draft, draft_digest: document.draft_digest })
  await act(async () => fireEvent.click(screen.getByText('save')))
  expect(JSON.parse(mutate.mock.calls[0]![2].bytes).expected_revision).toBe(3)
  expect(screen.getByLabelText('harness state')).toHaveTextContent('"dirty":false')
  rendered.unmount()
})

it('keeps a changed server draft separate from the acknowledged save instead of silently marking it clean', async () => {
  setup({ ...document, revision: 3, draft_digest: 'b'.repeat(64), draft: { ...draft, body_markdown: 'external C' } })
  vi.spyOn(api, 'mutateGuideline').mockImplementation(async (_path, _method, request) => receipt(request.key, 2))
  await createSaved()
  expect(screen.getByLabelText('正文')).toHaveValue(draft.body_markdown)
  expect(screen.getByText('有未保存编辑')).toBeVisible()
})

it('never counts cropped candidates, unsubmitted delivery, wrong ownership or missing closure as adoption', () => {
  const records = knowledgeRecords()
  for (const type of ['guideline_retrieval', 'waterbody_model_request']) {
    expect(selectRunGuidelines(records.map((r, i) => i === 0 ? { ...r, content_type: type } : r), new Map(), runId, sessionId, result).deliveries).toEqual([])
  }
  const invalid = structuredClone(records)
  if (invalid[0]?.content.kind === 'inline') (invalid[0].content.body as { submitted: boolean }).submitted = false
  expect(selectRunGuidelines(invalid, new Map(), runId, sessionId, result).used).toEqual([])
  expect(selectRunGuidelines(records.map((r) => ({ ...r, run_id: 'foreign-run' })), new Map(), runId, sessionId, result).deliveries).toEqual([])
  expect(selectRunGuidelines(records, new Map(), runId, sessionId, { ...result, result_id: 'other' }).used).toEqual([])
})
