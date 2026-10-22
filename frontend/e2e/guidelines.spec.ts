import { readFileSync } from 'node:fs'
import { expect, test, type Page } from '@playwright/test'
import { document, receipt, version } from '../tests/guideline-fixtures'

async function fillDraft(page: Page, title: string, code = '00400', unsafeFixture = false) {
  await page.getByLabel('标题', { exact: true }).fill(title)
  await page.getByLabel('简介', { exact: true }).fill('Synthetic browser fixture; no scientific approval.')
  await page.getByLabel(/适用指标（/).fill(`usgs:${code}`)
  await page.getByRole('textbox', { name: '正文', exact: true }).fill(`Original mechanism A; conditions and alternatives.${unsafeFixture ? ' <script>window.evil=1</script>' : ''}`)
}
async function publish(page: Page) {
  await page.getByRole('button', { name: '检查并发布', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '检查保存版本并发布' })
  await expect(dialog.getByText('用户编写，无外部出处')).toBeVisible()
  await dialog.getByLabel('检查人', { exact: true }).fill('browser-fixture-reviewer')
  await dialog.getByLabel('检查说明', { exact: true }).fill('Synthetic UI fixture only; not actual professional review.')
  await dialog.getByRole('checkbox').check()
  await dialog.getByRole('button', { name: '确认发布此保存版本' }).click()
  await expect(dialog).not.toBeVisible()
}

test('mocked API UI: uncertain creation reuses its key, inspection and keyboard navigation', async ({ page }) => {
  let current = structuredClone(document), firstKey = '', firstBytes = '', creates = 0
  await page.route('**/api/guidelines**', async (route) => {
    const request = route.request(), path = new URL(request.url()).pathname
    if (request.method() === 'GET') return route.fulfill({ json: path === '/api/guidelines' ? { items: [], next_cursor: null } : current })
    const key = request.headers()['idempotency-key']!, bytes = request.postData()!
    if (path === '/api/guidelines') {
      creates++
      current = { ...current, draft: JSON.parse(bytes), draft_digest: document.draft_digest }
      if (creates === 1) { firstKey = key; firstBytes = bytes; return route.fulfill({ status: 201, contentType: 'application/json', body: 'invalid ACK' }) }
      expect(key).toBe(firstKey); expect(bytes).toBe(firstBytes)
    }
    if (path.endsWith('/publish')) current = { ...current, state: 'available', active_version: version, effective_version_id: version.version_id }
    return route.fulfill({ json: receipt(key, 1, path.endsWith('/publish') ? 'publish' : 'create') })
  })
  await page.goto('/guidelines')
  await expect(page.getByRole('button', { name: '程序自带', exact: true })).toHaveAttribute('aria-pressed', 'true')
  await page.getByRole('button', { name: '我的说明书', exact: true }).click()
  await fillDraft(page, 'Browser authored guide', '00400', true)
  await page.getByRole('button', { name: '保存草稿', exact: true }).click()
  await expect(page.getByText(/请求结果不确定/)).toBeVisible()
  await page.getByRole('button', { name: '使用原请求对账' }).click()
  await expect(page.getByRole('heading', { name: '编辑说明书', exact: true })).toBeVisible()
  expect(creates).toBe(2)
  await page.getByRole('button', { name: '检查并发布', exact: true }).click()
  await expect(page.getByRole('dialog')).toBeVisible()
  await page.keyboard.press('Tab')
  await expect(page.getByRole('dialog').getByLabel('检查人', { exact: true })).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog')).not.toBeVisible()
  await publish(page)
  await expect(page.getByText('我的说明书 · 可用于分析', { exact: true })).toBeVisible()
  expect(await page.evaluate(() => 'evil' in window)).toBe(false)
  await page.getByRole('textbox', { name: '正文', exact: true }).fill('Unsaved B')
  page.once('dialog', (dialog) => dialog.dismiss())
  await page.getByRole('link', { name: '分析', exact: true }).click()
  await expect(page).toHaveURL(/\/guidelines$/)
  await expect(page.getByRole('textbox', { name: '正文', exact: true })).toHaveValue('Unsaved B')
  expect(firstKey).toMatch(/^[\da-f-]{36}$/)
  expect(firstBytes).toContain('Browser authored guide')
})

test('real backend: publish, provided and used snapshots, history after library update', async ({ page }) => {
  test.skip(!process.env.WATERAGENTS_GUIDELINE_BROWSER_FIXTURE, 'Requires isolated PostgreSQL backend and deterministic test composition')
  test.setTimeout(120_000)
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  await page.route(/tile\.openstreetmap\.org/, async (route) => route.fulfill({ status: 200, contentType: 'image/png',
    body: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4////fwAJ+wP9Z9b0WQAAAABJRU5ErkJggg==', 'base64') }))
  await page.goto('/guidelines')
  await fillDraft(page, 'M10 actual API original', '00300')
  await publish(page)
  await expect(page.getByText('我的说明书 · 可用于分析', { exact: true })).toBeVisible({ timeout: 30_000 })
  let fixture: { run_id: string; session_id: string; document_id: string } | null = null
  await expect.poll(() => {
    try { fixture = JSON.parse(readFileSync(process.env.WATERAGENTS_GUIDELINE_BROWSER_FIXTURE!, 'utf8')); return true } catch { return false }
  }, { timeout: 30_000 }).toBe(true)
  const saved = fixture!
  await page.goto(`/runs/${saved.run_id}/waterbodies/${saved.session_id}`)
  await page.getByRole('tab', { name: 'Agent', exact: true }).click()
  const panel = page.getByRole('region', { name: '运行说明书参考' })
  await expect(panel.getByText(/查看提供的说明书：M10 actual API original/).first()).toBeVisible({ timeout: 20_000 })
  await expect(panel.getByText(/查看参考说明书：M10 actual API original/).first()).toBeVisible()
  await page.goto('/guidelines')
  await page.getByRole('button', { name: '我的说明书', exact: true }).click()
  await page.getByRole('button', { name: 'M10 actual API original', exact: true }).click()
  await expect(page.getByLabel('标题', { exact: true })).toHaveValue('M10 actual API original')
  await page.getByLabel('标题', { exact: true }).fill('M10 updated library B')
  await page.getByRole('textbox', { name: '正文', exact: true }).fill('Updated mechanism B; this must never replace the old snapshot.')
  await publish(page)
  await expect(page.getByText('我的说明书 · 可用于分析', { exact: true })).toBeVisible({ timeout: 30_000 })
  await page.goto(`/history?run=${saved.run_id}&session=${saved.session_id}`)
  await page.getByRole('tab', { name: 'Agent', exact: true }).click()
  const historical = page.getByRole('region', { name: '运行说明书参考' })
  const savedReference = historical.getByText(/查看参考说明书：M10 actual API original/).first()
  await savedReference.click()
  const savedViewer = savedReference.locator('..')
  await expect(savedViewer.getByText(/Original mechanism A; conditions and alternatives/)).toBeVisible()
  await expect(savedViewer.getByText('用户编写，无外部出处')).toBeVisible()
  await expect(historical.getByText(/Updated mechanism B/)).toHaveCount(0)
  expect(errors).toEqual([])
})
