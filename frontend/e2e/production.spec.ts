import { expect, test } from '@playwright/test'

test('backend-served build uses same-origin API and safe assets', async ({ page }) => {
  test.skip(!process.env.WATERAGENTS_E2E_BASE_URL, 'Requires a running backend with built frontend')
  const requests: string[] = []
  const errors: string[] = []
  await page.route(/tile\.openstreetmap\.org/, async (route) => route.fulfill({ status: 200, contentType: 'image/png',
    body: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4////fwAJ+wP9Z9b0WQAAAABJRU5ErkJggg==', 'base64') }))
  page.on('request', (request) => requests.push(request.url()))
  page.on('console', (message) => { if (message.type() === 'error') errors.push(message.text()) })
  const response = await page.goto('/')
  expect(response?.status()).toBe(200)
  expect(response?.headers()['cache-control']).toBe('no-cache')
  await expect(page.getByRole('heading', { name: 'New analysis' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Start new analysis' })).toBeVisible()
  const origin = new URL(page.url()).origin
  const settings = await page.evaluate(async () => {
    const response = await fetch('/api/settings')
    return { status: response.status, type: response.headers.get('Content-Type'), body: await response.json() }
  })
  expect(settings.status).toBe(200)
  expect(settings.type).toContain('application/json')
  expect(settings.body.catalog.unit_options.length).toBe(154)
  expect(requests.filter((url) => !url.includes('tile.openstreetmap.org')).every((url) => url.startsWith(origin))).toBe(true)
  expect(requests.filter((url) => url.includes('/api/')).every((url) => url.startsWith(origin))).toBe(true)
  expect(requests.some((url) => url.includes('/api/settings'))).toBe(true)
  expect(requests.some((url) => url.includes('5173'))).toBe(false)
  expect(errors).toEqual([])
  const health = await page.request.get('/health')
  expect(health.headers()['x-frontend-available']).toBe('true')
  const missing = await page.request.get('/api/not-found', { headers: { Accept: 'text/html' } })
  expect(missing.status()).toBe(404)
  expect(missing.headers()['content-type']).toContain('application/json')
  const realRunId = process.env.WATERAGENTS_E2E_RUN_ID
  if (realRunId) {
    const saved = await page.request.get(`/api/analysis/runs/${realRunId}?view=history`)
    expect(saved.status()).toBe(200)
    const savedBody = await saved.json()
    expect(savedBody.run.status).toBe('completed')
    expect(savedBody.run.configuration.analysis_timezone).toBe('America/New_York')
    expect(savedBody.run.result_ref).not.toBeNull()
    const waterbody = savedBody.sessions.find((session: { agent_role: string }) => session.agent_role === 'waterbody')
    expect(waterbody?.status).toBe('completed')
    const transcript = await page.request.get(`/api/analysis/runs/${realRunId}/sessions/${waterbody.session_id}/transcript`)
    expect(transcript.status()).toBe(200)
    const items = (await transcript.json()).items as { content_type: string }[]
    expect(items.some((item) => item.content_type === 'evidence_bundle')).toBe(true)
    expect(items.some((item) => item.content_type === 'inference_result')).toBe(true)
    const runEvents = await page.request.get(`/api/analysis/runs/${realRunId}/events`)
    const sessionEvents = await page.request.get(`/api/analysis/runs/${realRunId}/sessions/${waterbody.session_id}/events`)
    if (process.env.WATERAGENTS_E2E_OPERATION_ID) {
      expect(runEvents.status()).toBe(410)
      expect((await runEvents.json()).history_url).toContain(`runs/${realRunId}?view=history`)
      expect(sessionEvents.status()).toBe(410)
    } else {
      expect(await runEvents.text()).toContain('event: run_snapshot')
      expect(await sessionEvents.text()).toContain('event: transcript_item')
    }
    expect(settings.body.revision).toBeGreaterThanOrEqual(1)
    await page.goto(`/runs/${realRunId}`)
    await expect(page.getByText(`Run ${realRunId}`).first()).toBeVisible()
    await expect(page.getByRole('button', { name: 'COMID 100 — Completed' })).toBeVisible()
    await expect(page.getByText('The target has a supported water-quality assessment.')).toBeVisible()
    await page.getByRole('button', { name: 'COMID 100 — Completed' }).click()
    await expect(page.getByRole('tab', { name: 'Agent' })).toBeVisible()
    await expect(page.getByRole('tab', { name: 'Evidence' })).toBeVisible()
  }
  const cancelledRunId = process.env.WATERAGENTS_E2E_CANCEL_RUN_ID
  if (cancelledRunId) {
    await page.goto(`/runs/${cancelledRunId}`)
    await expect(page.getByText(`Run ${cancelledRunId}`).first()).toBeVisible()
    await expect(page.getByRole('button', { name: 'COMID 100 — Cancelled' })).toBeVisible()
  }
  const operationId = process.env.WATERAGENTS_E2E_OPERATION_ID
  if (operationId) {
    const operation = await page.request.get(`/api/admin/operations/${operationId}`)
    expect(operation.status()).toBe(200)
    expect((await operation.json()).state).toBe('succeeded')
    const deletedRunId = process.env.WATERAGENTS_E2E_DELETED_RUN_ID
    expect((await page.request.get(`/api/analysis/runs/${deletedRunId}?view=history`)).status()).toBe(404)
    expect((await page.request.get(`/api/analysis/runs/${realRunId}?view=history`)).status()).toBe(200)
  }
})
