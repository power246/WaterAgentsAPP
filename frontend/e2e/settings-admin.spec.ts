import { expect, test } from '@playwright/test'

test('Settings, keys and Admin use server revisions, previews and resumable operations', async ({ page }) => {
  const consoleMessages: string[] = []
  page.on('console', (message) => consoleMessages.push(message.text()))
  const runId = '00000000-0000-4000-8000-000000000021'
  const operationId = '00000000-0000-4000-8000-000000000022'
  const time = '2026-10-08T00:00:00Z'
  const profile = `sha256:${'a'.repeat(64)}`
  let settings = {
    schema_version: 1, revision: 0, updated_at: null as string | null,
    defaults: { analysis_timezone: 'UTC', parser_model: { provider: 'fake', model: 'parser' },
      waterbody_model: { provider: 'fake', model: 'waterbody' }, memory_mode: 'stateless',
      time_resolution: '12h', desired_embedding_profile_id: profile },
    catalog: { models: [
      { selection: { provider: 'fake', model: 'parser' }, label: 'Parser', roles: ['query_parser'], available: true, unavailable_reason: null },
      { selection: { provider: 'fake', model: 'waterbody' }, label: 'Waterbody', roles: ['waterbody'], available: true, unavailable_reason: null },
    ], embedding_profiles: [{ profile_id: profile, label: 'Embedding', available: true, unavailable_reason: null }],
    memory_modes: ['stateless'], time_resolutions: ['12h'], iana_timezones: ['UTC', 'America/Denver'],
    unit_options: [{ parameter: { code_system: 'usgs', code: '00060' }, name: 'Discharge', source_unit_id: 'ft3_s',
      target_units: [{ unit_id: 'ft3_s', symbol: 'ft³/s', dimension_id: 'discharge' },
        { unit_id: 'm3_s', symbol: 'm³/s', dimension_id: 'discharge' }] }],
    },
    unit_policy: { version: 1, registry_digest: 'a'.repeat(64),
      entries: [{ parameter: { code_system: 'usgs', code: '00060' }, target_unit_id: 'ft3_s' }] },
  }
  const key = { key_name: 'openai', label: 'OpenAI', purpose: ['query_parser'], configured: true,
    source: 'user_store', masked: '••••1234', updated_at: null, store_available: true,
    can_save: true, can_delete_user_override: true, can_test: true }
  let tests = 0
  let saves = 0
  let previewCount = 0
  let executions = 0
  const operation = { operation_id: operationId, kind: 'history_cleanup', state: 'pending',
    resource_identity: 'history:selection', progress: { processed_items: 0, total_items: 1,
      succeeded_items: 0, failed_items: 0 }, safe_error: null, safe_result: null,
    created_at: time, started_at: null, finished_at: null }
  await page.route(/tile\.openstreetmap\.org/, async (route) => route.fulfill({ status: 200, contentType: 'image/png',
    body: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4////fwAJ+wP9Z9b0WQAAAABJRU5ErkJggg==', 'base64') }))
  await page.route(/\/api\/settings$/, async (route) => {
    if (route.request().method() === 'PUT') {
      const body = route.request().postDataJSON()
      expect(body.expected_revision).toBe(settings.revision)
      expect(Object.keys(body).sort()).toEqual(['defaults', 'expected_revision'])
      settings = { ...settings, revision: settings.revision + 1, updated_at: time, defaults: body.defaults }
    }
    await route.fulfill({ json: settings })
  })
  await page.route(/\/api\/settings\/unit-policies\/preview$/, async (route) => {
    const body = route.request().postDataJSON()
    expect(body.base_version).toBe(1)
    expect(body.entries[0].target_unit_id).toBe('m3_s')
    await route.fulfill({ json: { base_version: 1, proposed: { ...settings.unit_policy, version: 2, entries: body.entries },
      changes: [{ parameter: { code_system: 'usgs', code: '00060' }, previous_unit_id: 'ft3_s',
        target_unit_id: 'm3_s', dimension_id: 'discharge' }], affected_dimensions: ['discharge'],
      registry_digest: settings.unit_policy.registry_digest,
      confirmation: { token: 'unit-server-token', required_phrase: 'APPLY UNIT POLICY 2', expires_at: time } } })
  })
  await page.route(/\/api\/settings\/unit-policies$/, async (route) => {
    expect(route.request().headers()['idempotency-key']).toMatch(/^[0-9a-f-]{36}$/)
    expect(route.request().postDataJSON()).toEqual({ confirmation_token: 'unit-server-token', confirmation_phrase: 'APPLY UNIT POLICY 2' })
    settings = { ...settings, revision: settings.revision + 1, unit_policy: { ...settings.unit_policy,
      version: 2, entries: [{ parameter: { code_system: 'usgs', code: '00060' }, target_unit_id: 'm3_s' }] } }
    await route.fulfill({ status: 201, json: { ...settings.unit_policy, created_at: time } })
  })
  await page.route(/\/api\/keys$/, async (route) => route.fulfill({ json: { keys: [key, {
    ...key, key_name: 'google_geocoding', label: 'Google Geocoding', source: 'deployment',
    can_delete_user_override: false,
  }] } }))
  await page.route(/\/api\/keys\/openai$/, async (route) => {
    if (route.request().method() === 'PUT') {
      saves++
      expect(route.request().postDataJSON()).toEqual({ value: 'test-only-ui-value' })
      await route.fulfill({ json: key })
    } else await route.fulfill({ json: { ...key, source: 'deployment', can_delete_user_override: false } })
  })
  await page.route(/\/api\/keys\/openai\/test$/, async (route) => {
    tests++
    await route.fulfill({ json: { key_name: 'openai', status: 'succeeded', safe_error: null, checked_at: time } })
  })
  await page.route(/\/api\/admin\/storage$/, async (route) => route.fulfill({ json: {
    history: { state: 'available' }, rag_index: { state: 'available' },
    evidence_cache: { state: 'not_present' }, credential_store: { state: 'available' },
  } }))
  await page.route(/\/api\/admin\/rag$/, async (route) => route.fulfill({ json: {
    index: { active_generation_id: null, generations: [] }, handoff: {
      desired_profile_digest: 'a'.repeat(64), active_generation_id: null, building_generation_id: null,
      blocking_reader_count: 0, old_credential_cleanup_ready: false, phase: 'build_required', retired_generation_ids: [],
    },
  } }))
  await page.route(/\/api\/admin\/operations\?limit=50$/, async (route) => route.fulfill({ json: {
    items: executions ? [operation] : [], next_cursor: null,
  } }))
  await page.route(/\/api\/admin\/operations\/[^/]+$/, async (route) => route.fulfill({ json: {
    ...operation, state: 'succeeded', finished_at: time,
  } }))
  await page.route('**/api/analysis/runs?limit=50', async (route) => route.fulfill({ json: { items: [{
    run_id: runId, query: 'Saved river', status: 'completed', created_at: time, updated_at: time,
    configuration: null, context_summary: null, result_ref: null, result_summary: null, issues: [],
  }], next_cursor: null } }))
  await page.route(/\/api\/admin\/history-cleanups\/preview$/, async (route) => {
    previewCount++
    const body = route.request().postDataJSON()
    expect(body.selection).toEqual({ kind: 'all_history' })
    expect(body.cascade_consumers).toBe(previewCount === 2)
    await route.fulfill({ json: { requested_run_ids: [runId], delete_run_ids: [runId], delete_order: [runId],
      dependencies: ['00000000-0000-4000-8000-000000000023'], cascade_required: true,
      cascade_authorized: previewCount === 2, estimated: { runs: 1, sessions: 2, transcript_items: 6,
        retained_idempotency_tombstones: 1 }, preserved: ['api_keys', 'settings', 'unit_policy_versions'],
      confirmation: previewCount === 2 ? { token: 'admin-server-token', required_phrase: 'DELETE 1 RUN', expires_at: time } : null } })
  })
  await page.route(/\/api\/admin\/history-cleanups$/, async (route) => {
    executions++
    expect(route.request().headers()['idempotency-key']).toMatch(/^[0-9a-f-]{36}$/)
    expect(route.request().postDataJSON()).toEqual({ confirmation_token: 'admin-server-token', confirmation_phrase: 'DELETE 1 RUN' })
    await route.fulfill({ status: 202, json: operation })
  })

  await page.goto('/')
  await page.getByRole('button', { name: 'Settings' }).click()
  const drawer = page.getByRole('complementary', { name: 'Settings' })
  await expect(drawer.getByRole('heading', { name: 'Google Geocoding' })).toBeVisible()
  await expect(drawer).toHaveScreenshot('settings-drawer.png', { animations: 'disabled' })
  await drawer.getByLabel('Analysis timezone').selectOption('America/Denver')
  await drawer.getByRole('button', { name: 'Save full defaults' }).click()
  await expect(drawer.getByText(/New runs use these values/)).toBeVisible()
  await drawer.getByRole('heading', { name: 'Credentials' }).scrollIntoViewIfNeeded()
  await expect(drawer).toHaveScreenshot('settings-credentials.png', { animations: 'disabled' })
  const secret = drawer.getByLabel('OpenAI credential')
  await secret.fill('test-only-ui-value')
  await drawer.getByRole('button', { name: 'Save OpenAI' }).click()
  await expect(secret).toHaveValue('')
  expect(saves).toBe(1)
  expect(tests).toBe(0)
  await drawer.getByRole('button', { name: 'Test connection' }).first().click()
  await expect.poll(() => tests).toBe(1)
  await drawer.getByRole('button', { name: 'Remove user override' }).click()
  await expect(drawer.getByText(/deployment/).first()).toBeVisible()
  await drawer.getByLabel(/Discharge \(usgs:00060\)/).selectOption('m3_s')
  await drawer.getByRole('button', { name: 'Preview unit policy' }).click()
  await expect(drawer.getByText(/Version 1 → 2/)).toBeVisible()
  await drawer.getByLabel('Unit confirmation phrase').fill('APPLY UNIT POLICY 2')
  await drawer.getByRole('button', { name: 'Confirm unit policy' }).click()
  await expect(drawer.getByText(/Unit policy saved for future runs/)).toBeVisible()
  await drawer.getByRole('link', { name: 'Advanced / Admin' }).click()
  await expect(page.getByRole('heading', { name: 'Advanced / Admin' })).toBeVisible()
  await page.getByRole('checkbox', { name: 'All saved history' }).check()
  await page.getByRole('button', { name: 'Preview history cleanup' }).click()
  await page.getByRole('button', { name: 'Preview whole-run cascade' }).click()
  await expect(page.getByText(/1 tombstone retained/)).toBeVisible()
  await page.getByLabel('Admin confirmation phrase').fill('DELETE 1 RUN')
  await page.getByRole('button', { name: 'Execute confirmed operation' }).click()
  await expect(page.getByText(/history_cleanup · succeeded/)).toBeVisible()
  expect(previewCount).toBe(2)
  expect(executions).toBe(1)
  await expect(page.locator('.admin-operations')).toHaveScreenshot('admin-operation.png', { animations: 'disabled' })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.evaluate(() => window.scrollTo(0, 0))
  await expect(page).toHaveScreenshot('admin-narrow.png', { animations: 'disabled' })
  expect(consoleMessages.join('\n')).not.toContain('test-only-ui-value')
})
