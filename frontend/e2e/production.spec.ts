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
})
