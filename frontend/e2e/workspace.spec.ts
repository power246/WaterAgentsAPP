import { expect, test } from '@playwright/test'

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
    memory_modes: ['stateless'], time_resolutions: ['12h'], embedding_profiles: [], iana_timezones: ['America/Denver'],
  },
  unit_policy: { version: 0, entries: [] }, updated_at: null,
}

test('workspace routes and form use the current API surface', async ({ page }) => {
  await page.route(/\/api\/settings$/, async (route) => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify(settings),
  }))
  await page.route('**/api/analysis/runs?limit=50', async (route) => route.fulfill({ json: { items: [], next_cursor: null } }))
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'New analysis' })).toBeVisible()
  await expect(page.getByRole('tab', { name: 'Overview' })).toBeVisible()
  await page.getByRole('link', { name: 'History' }).click()
  await expect(page.getByRole('heading', { name: 'History' })).toBeVisible()
  await page.setViewportSize({ width: 390, height: 844 })
  await page.getByRole('tab', { name: 'map' }).click()
  await expect(page.getByRole('region', { name: 'Topology map' })).toBeVisible()
  await page.getByRole('tab', { name: 'detail' }).click()
  await expect(page.getByRole('region', { name: 'Analysis detail' })).toBeVisible()
  await page.goto('/admin')
  await expect(page.getByRole('heading', { name: 'Advanced / Admin' })).toBeVisible()
  await page.goto('/not-a-page')
  await expect(page.getByRole('heading', { name: 'Page not found' })).toBeVisible()
})

test('map renders saved topology status, labels and a frozen running halo', async ({ page }) => {
  const runId = '00000000-0000-4000-8000-000000000001'
  const time = '2026-10-08T00:00:00Z'
  let runStatus: 'running' | 'cancelled' = 'running'
  const nodes = Array.from({ length: 7 }, (_, index) => ({
    comid: String(index + 1), point: { latitude: 39 + index * 0.07, longitude: -97 + index * 0.08 },
    participation: index === 0 ? 'excluded' : 'active',
  }))
  const statuses = [null, null, 'running', 'completed', 'failed', 'cancelled', null]
  const sessions = statuses.flatMap((status, index) => status ? [{
    session_id: `00000000-0000-4000-8000-${String(index + 1).padStart(12, '0')}`,
    target_comid: String(index + 1), agent_role: 'waterbody', status,
    created_at: time, updated_at: time, result_ref: null, result_summary: null,
  }] : [])
  const waterbodies = nodes.map((node, index) => ({
    target_comid: node.comid, participating: index !== 0, scheduling_status: 'waiting',
    dependency_status: null,
    session_id: sessions.find((session) => session.target_comid === node.comid)?.session_id ?? null,
    session_status: statuses[index], result_ref: null,
  }))
  const snapshot = () => ({
    schema_version: 1, run_id: runId, status: runStatus, analysis_phase: 'waterbody_execution',
    created_at: time, updated_at: time,
    context_ref: null, topology_ref: { run_id: runId, item_id: 'graph' }, result_ref: null,
    summary: { status: 'not_started', session_id: null, result_ref: null, error: null },
    graph_outcome: { coverage: null, usable_nodes: 0, insufficient_nodes: 0, invalid_nodes: 0, cancelled_nodes: 0 },
    sessions, waterbodies, persistence: { state: 'pending', warning: null },
  })
  await page.route(/\/api\/settings$/, async (route) => route.fulfill({ json: settings }))
  await page.route(/\/api\/analysis\/runs\/[^/]+\/events$/, async (route) => route.fulfill({
    status: 200, contentType: 'text/event-stream',
    body: `event: run_snapshot\ndata: ${JSON.stringify(snapshot())}\n\nevent: stream_end\ndata: ${JSON.stringify({ run_id: runId, stream: 'run' })}\n\n`,
  }))
  await page.route(/\/api\/analysis\/runs\/[^/?]+\?view=auto$/, async (route) => route.fulfill({ json: {
    run: { run_id: runId, status: runStatus, query: 'Saved topology', created_at: time, updated_at: time,
      context_summary: null, result_ref: null, result_summary: null },
    source: 'live', sessions, items: [{ content_type: 'topology_ready', content: {
      kind: 'inline', body: { nodes, geometries: {} },
    } }], referenced_items: [], issues: [], persistence: { state: 'pending', warning: null },
  } }))
  await page.route(/tile\.openstreetmap\.org/, async (route) => route.fulfill({
    status: 200, contentType: 'image/png',
    body: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4////fwAJ+wP9Z9b0WQAAAABJRU5ErkJggg==', 'base64'),
  }))
  await page.goto(`/runs/${runId}`)
  const map = page.getByRole('region', { name: 'Topology map' })
  await expect(map.getByRole('button', { name: /COMID 3 — Running/ })).toBeVisible()
  await expect(map.locator('.running-halo')).toHaveCount(1)
  await page.addStyleTag({ content: '.running-halo { animation: none !important; }' })
  await expect(map).toHaveScreenshot('running-topology.png', { animations: 'disabled' })
  runStatus = 'cancelled'
  await page.reload()
  await expect(map.getByRole('button', { name: /COMID 7 — Stopped before start/ })).toBeVisible()
  await page.addStyleTag({ content: '.running-halo { animation: none !important; }' })
  await expect(map).toHaveScreenshot('cancelled-topology.png', { animations: 'disabled' })
})

test('create, live run, selected agent, cancel and saved history share the public surface', async ({ page }) => {
  const runId = '00000000-0000-4000-8000-000000000011'
  const sessionId = '00000000-0000-4000-8000-000000000012'
  const itemId = '00000000-0000-4000-8000-000000000013'
  const time = '2026-10-08T00:00:00Z'
  let createKey = ''
  let createdBody: Record<string, unknown> = {}
  let runStreams = 0
  let sessionStreams = 0
  let cancels = 0
  await page.route(/\/api\/settings$/, async (route) => route.fulfill({ json: settings }))
  await page.route(/\/api\/analysis\/runs$/, async (route) => {
    createKey = route.request().headers()['idempotency-key'] ?? ''
    createdBody = route.request().postDataJSON() as Record<string, unknown>
    await route.fulfill({ status: 201, json: { run_id: runId, status: 'pending', created_at: time, updated_at: time, configuration: {} } })
  })
  const session = { session_id: sessionId, target_comid: '123', agent_role: 'waterbody', status: 'running',
    created_at: time, updated_at: time, result_ref: null, result_summary: null }
  const snapshot = {
    schema_version: 1, run_id: runId, status: 'running', analysis_phase: 'waterbody_execution',
    created_at: time, updated_at: time, context_ref: null, topology_ref: { run_id: runId, item_id: 'graph' }, result_ref: null,
    summary: { status: 'not_started', session_id: null, result_ref: null, error: null },
    graph_outcome: { coverage: null, usable_nodes: 0, insufficient_nodes: 0, invalid_nodes: 0, cancelled_nodes: 0 },
    sessions: [session], waterbodies: [{ target_comid: '123', participating: true, scheduling_status: 'running',
      dependency_status: null, session_id: sessionId, session_status: 'running', result_ref: null }],
    persistence: { state: 'pending', warning: null },
  }
  await page.route(/\/api\/analysis\/runs\/[^/]+\/events$/, async (route) => {
    runStreams++
    await route.fulfill({ status: 200, contentType: 'text/event-stream', body:
      `event: run_snapshot\ndata: ${JSON.stringify(snapshot)}\n\nevent: stream_end\ndata: ${JSON.stringify({ run_id: runId, stream: 'run' })}\n\n` })
  })
  await page.route(/\/api\/analysis\/runs\/[^/]+\/sessions\/[^/]+\/events$/, async (route) => {
    sessionStreams++
    const header = { item_id: itemId, run_id: runId, transcript_sequence: 1, occurred_at: time,
      content_type: 'waterbody_agent_message', content_schema_version: 1,
      scope: { session_id: sessionId, agent_role: 'waterbody', target_comid: '123' },
      correlation: { request_id: null, operation_id: null, error_id: null }, tags: [], rag_eligible: false }
    const event = (name: string, sequence: number, payload: object) =>
      `event: ${name}\nid: ${sequence}\ndata: ${JSON.stringify({ schema_version: 1, run_id: runId, session_id: sessionId, sequence, occurred_at: time, payload })}\n\n`
    await route.fulfill({ status: 200, contentType: 'text/event-stream', body:
      event('message_started', 1, { item: header }) + event('message_delta', 2, { item_id: itemId, text: 'Live observation' }) +
      event('message_finalized', 3, { item: { ...header, content_bytes: 16, content_ref: { run_id: runId, item_id: itemId },
        content: { kind: 'inline', body: { text: 'Live observation' } }, parse_status: 'ok', confidence: null, provenance: null,
        parse_error: null }, generation_status: 'completed' }) +
      `event: stream_end\ndata: ${JSON.stringify({ run_id: runId, session_id: sessionId, stream: 'session' })}\n\n` })
  })
  await page.route(/\/api\/analysis\/runs\/[^/?]+\?view=auto$/, async (route) => route.fulfill({ json: {
    run: { run_id: runId, status: 'running', query: 'Assess river flow', created_at: time, updated_at: time,
      context_summary: null, result_ref: null, result_summary: null }, source: 'live', sessions: [session],
    items: [{ content_type: 'topology_ready', content: { kind: 'inline', body: {
      nodes: [{ comid: '123', point: { latitude: 39, longitude: -97 }, participation: 'active' }], geometries: {},
    } } }], referenced_items: [], issues: [], persistence: { state: 'pending', warning: null },
  } }))
  await page.route(/\/api\/analysis\/runs\/[^/]+\/cancel$/, async (route) => {
    cancels++
    await route.fulfill({ json: { run_id: runId, status: 'cancelling' } })
  })
  await page.route('**/api/analysis/runs?limit=50', async (route) => route.fulfill({ json: { items: [{
    run_id: runId, query: 'Assess river flow', status: 'completed', created_at: time, updated_at: time,
    context_summary: null, result_ref: null, result_summary: null,
    configuration: { analysis_timezone: 'America/Denver' }, issues: [],
  }], next_cursor: null } }))
  await page.route(/\/api\/analysis\/runs\/[^/?]+\?view=history$/, async (route) => route.fulfill({ json: {
    run: { run_id: runId, status: 'completed', query: 'Assess river flow', created_at: time, updated_at: time,
      context_summary: null, result_ref: null, result_summary: null }, source: 'postgresql', sessions: [session],
    items: [], referenced_items: [], issues: [], persistence: { state: 'saved', warning: null },
  } }))
  await page.route(/tile\.openstreetmap\.org/, async (route) => route.fulfill({ status: 200, contentType: 'image/png',
    body: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4////fwAJ+wP9Z9b0WQAAAABJRU5ErkJggg==', 'base64') }))

  await page.goto('/')
  await page.getByRole('textbox', { name: 'Question' }).fill('Assess river flow')
  await page.getByRole('button', { name: 'Start new analysis' }).click()
  await expect(page).toHaveURL(new RegExp(`/runs/${runId}$`))
  await expect(page.getByRole('button', { name: /COMID 123 — Running/ })).toBeVisible()
  expect(createKey).toMatch(/^[0-9a-f-]{36}$/)
  expect(createdBody).toMatchObject({ query: 'Assess river flow', memory_mode: 'stateless', time_resolution: '12h' })
  expect(Object.keys(createdBody).sort()).toEqual(['memory_mode', 'parser_model', 'query', 'time_resolution', 'waterbody_model'])
  await page.getByRole('button', { name: /COMID 123 — Running/ }).click()
  await expect(page).toHaveURL(new RegExp(`/waterbodies/${sessionId}$`))
  await expect(page.getByText('Live observation')).toBeVisible()
  await expect(page.locator('[data-item-id]')).toHaveCount(1)
  await page.getByRole('button', { name: 'Stop run' }).click()
  await expect.poll(() => cancels).toBe(1)
  await page.getByRole('link', { name: 'History' }).click()
  await expect(page.getByText('America/Denver')).toBeVisible()
  await page.getByRole('button', { name: /Assess river flow.*completed/ }).click()
  await expect(page.getByRole('button', { name: 'Delete this saved run' })).toBeVisible()
  expect(runStreams).toBe(1)
  expect(sessionStreams).toBe(1)
})
