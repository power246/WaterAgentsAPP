import { describe, expect, it, vi } from 'vitest'
import { ApiClient, ApiError, prepareConfirmation, prepareSubmission, type CreateRequest } from '../src/api/http'

const request: CreateRequest = {
  query: 'Assess the river', parser_model: { provider: 'openai', model: 'parser' },
  waterbody_model: { provider: 'openai', model: 'waterbody' },
  memory_mode: 'stateless', time_resolution: '12h',
}

function json(status: number, body: unknown, contentType = 'application/json'): Response {
  return new Response(JSON.stringify(body), {
    status, headers: { 'Content-Type': contentType, 'X-Request-ID': 'safe-request-id' },
  })
}

describe('typed HTTP policy', () => {
  it('uses fixed confirmation bytes and key for unit and Admin retries without automatic retry', async () => {
    const seen: { path: string; body: BodyInit | null | undefined; key: string | null }[] = []
    let loseResponse = true
    const client = new ApiClient(async (path, init) => {
      seen.push({ path: String(path), body: init?.body, key: new Headers(init?.headers).get('Idempotency-Key') })
      if (loseResponse) { loseResponse = false; throw new TypeError('response lost') }
      return String(path).includes('unit-policies')
        ? json(201, { version: 2, entries: [], registry_digest: 'a'.repeat(64) })
        : json(202, { operation_id: 'operation', state: 'pending' })
    })
    const unit = prepareConfirmation('unit-token', 'APPLY UNIT POLICY 2')
    await expect(client.confirmUnitPolicy(unit)).rejects.toThrow('response lost')
    expect(seen).toHaveLength(1)
    await client.confirmUnitPolicy(unit)
    expect(seen[1]).toEqual(seen[0])
    expect(seen[0]?.body).toBe(unit.bytes)
    const admin = prepareConfirmation('admin-token', 'DELETE 2 RUNS')
    await client.executeRunCleanup(admin)
    await client.executeRunCleanup(admin)
    expect(seen[2]).toEqual(seen[3])
    expect(seen[2]?.body).toBe(admin.bytes)
    expect(seen[2]?.key).toBe(admin.key)
  })

  it('keeps exact bytes and UUID v4 for a retry of one submission', async () => {
    const seen: { path: string; body: BodyInit | null | undefined; key: string | null }[] = []
    const client = new ApiClient(async (path, init) => {
      seen.push({ path: String(path), body: init?.body, key: new Headers(init?.headers).get('Idempotency-Key') })
      return json(201, { run_id: 'run', status: 'pending', created_at: '', updated_at: '', configuration: {} })
    })
    const submission = prepareSubmission(request)
    await client.create(submission)
    await client.create(submission)
    expect(seen).toHaveLength(2)
    expect(seen[0]).toEqual(seen[1])
    expect(seen[0]?.path).toBe('/api/analysis/runs')
    expect(seen[0]?.body).toBe(JSON.stringify(request))
    expect(seen[0]?.key).toMatch(/^[0-9a-f-]{36}$/)
    expect(prepareSubmission(request).key).not.toBe(submission.key)
  })

  it('never automatically retries mutation; retries a GET network failure once', async () => {
    const mutation = vi.fn(async () => { throw new TypeError('offline') })
    await expect(new ApiClient(mutation).create(prepareSubmission(request))).rejects.toThrow('offline')
    expect(mutation).toHaveBeenCalledTimes(1)
    const get = vi.fn().mockRejectedValueOnce(new TypeError('offline')).mockResolvedValueOnce(json(200, { items: [], next_cursor: null }))
    await expect(new ApiClient(get).history()).resolves.toMatchObject({ items: [] })
    expect(get).toHaveBeenCalledTimes(2)
  })

  it('reports safe code, status and request ID, including a tombstone history URL', async () => {
    const fetcher = vi.fn(async () => json(410, {
      error: { code: 'idempotency_resource_deleted', message: 'safe' },
      history_url: '/api/analysis/runs/123?view=history',
    }))
    const client = new ApiClient(fetcher)
    await expect(client.create(prepareSubmission(request))).rejects.toMatchObject({
      status: 410, code: 'idempotency_resource_deleted', requestId: 'safe-request-id',
      historyUrl: '/api/analysis/runs/123?view=history',
    })
    expect(fetcher).toHaveBeenCalledTimes(1)
  })

  it('rejects an HTML success body before projecting it as an API response', async () => {
    const client = new ApiClient(async () => json(200, { items: [] }, 'text/html'))
    await expect(client.history()).rejects.toEqual(new ApiError(200, 'invalid_content_type', 'safe-request-id'))
  })
})
