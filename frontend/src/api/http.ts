import type { components } from './generated'

type Schema<Name extends keyof components['schemas']> = components['schemas'][Name]
export type CreateRequest = Schema<'CreateAnalysisRunRequest'>
export type CreatedRun = Schema<'AnalysisRunResponse'>
export type RunDetail = Schema<'RunDetailResponse'>
export type RunHistory = Schema<'RunHistoryResponse'>
export type SessionTranscript = Schema<'SessionTranscriptResponse'>
export type ContentResponse = Schema<'TranscriptContentResponse'>
export type Settings = Schema<'SettingsResponse'>
export type CleanupPreview = Schema<'HistoryCleanupPreviewResponse'>
export type AdminOperation = Schema<'AdminOperationSnapshot'>
export type RunSnapshot = Schema<'RunSnapshotEvent'>
export type SessionProcess = Schema<'SessionProcessEvent'>
export type TranscriptItem = Schema<'TranscriptItemView'>

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    readonly requestId: string | null,
    readonly historyUrl: string | null = null,
  ) {
    super(code)
  }
}

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function settingsResponse(value: unknown): value is Settings {
  return record(value) && Number.isSafeInteger(value.revision) && record(value.defaults) &&
    record(value.catalog) && record(value.unit_policy) &&
    record(value.defaults.parser_model) && record(value.defaults.waterbody_model) &&
    Array.isArray(value.catalog.models) && Array.isArray(value.catalog.time_resolutions) &&
    Array.isArray(value.catalog.memory_modes)
}

function createdRun(value: unknown): value is CreatedRun {
  return record(value) && typeof value.run_id === 'string' && typeof value.status === 'string'
}

function runDetail(value: unknown): value is RunDetail {
  return record(value) && record(value.run) && typeof value.run.run_id === 'string' &&
    typeof value.run.status === 'string' && typeof value.source === 'string' &&
    Array.isArray(value.sessions) && Array.isArray(value.items) && Array.isArray(value.referenced_items)
}

function runHistory(value: unknown): value is RunHistory {
  return record(value) && Array.isArray(value.items) && value.items.every(record) &&
    (value.next_cursor === undefined || value.next_cursor === null || typeof value.next_cursor === 'string')
}

function sessionTranscript(value: unknown): value is SessionTranscript {
  return record(value) && typeof value.run_id === 'string' && typeof value.session_id === 'string' &&
    Array.isArray(value.items) && Array.isArray(value.referenced_items)
}

function transcriptContent(value: unknown): value is ContentResponse {
  return record(value) && typeof value.run_id === 'string' && typeof value.item_id === 'string' &&
    typeof value.text === 'string'
}

function runStatus(value: unknown): value is Schema<'RunStatusResponse'> {
  return record(value) && typeof value.run_id === 'string' && typeof value.status === 'string'
}

function persistenceRetry(value: unknown): value is Schema<'PersistenceRetryResponse'> {
  return record(value) && typeof value.run_id === 'string' && record(value.persistence)
}

function cleanupPreview(value: unknown): value is CleanupPreview {
  return record(value) && Array.isArray(value.delete_run_ids) && Array.isArray(value.dependencies) &&
    Array.isArray(value.preserved) && record(value.estimated) && typeof value.cascade_required === 'boolean'
}

function adminOperation(value: unknown): value is AdminOperation {
  return record(value) && typeof value.operation_id === 'string' && typeof value.state === 'string'
}

function errorEnvelope(value: unknown): { code: string; historyUrl: string | null } {
  if (!record(value)) return { code: 'invalid_error_response', historyUrl: null }
  const detail = value.error
  return {
    code: record(detail) && typeof detail.code === 'string' ? detail.code : 'invalid_error_response',
    historyUrl: typeof value.history_url === 'string' && value.history_url.startsWith('/api/analysis/runs/')
      ? value.history_url : null,
  }
}

export type Submission = Readonly<{ key: string; bytes: string }>

export function prepareSubmission(body: CreateRequest): Submission {
  return Object.freeze({ key: crypto.randomUUID(), bytes: JSON.stringify(body) })
}

export class ApiClient {
  constructor(private readonly fetcher: typeof fetch = (...args) => fetch(...args)) {}

  private async send<T>(
    path: `/api/${string}`,
    method: 'GET' | 'POST',
    valid: (value: unknown) => value is T,
    options: { body?: string; headers?: HeadersInit; signal?: AbortSignal } = {},
  ): Promise<T> {
    const headers = new Headers(options.headers)
    if (options.body !== undefined) headers.set('Content-Type', 'application/json')
    let response: Response
    for (let attempt = 0; ; attempt++) {
      try {
        response = await this.fetcher(path, { method, headers, body: options.body, signal: options.signal })
        break
      } catch (error) {
        if (method !== 'GET' || attempt >= 1 || options.signal?.aborted) throw error
      }
    }
    const requestId = response.headers.get('X-Request-ID')
    const contentType = (response.headers.get('Content-Type')?.split(';', 1)[0] ?? '').trim().toLowerCase()
    if (contentType !== 'application/json') {
      throw new ApiError(response.status, 'invalid_content_type', requestId)
    }
    let value: unknown
    try {
      value = await response.json()
    } catch {
      throw new ApiError(response.status, 'invalid_json', requestId)
    }
    if (!response.ok) {
      const detail = errorEnvelope(value)
      throw new ApiError(response.status, detail.code, requestId, detail.historyUrl)
    }
    if (!valid(value)) throw new ApiError(response.status, 'invalid_response', requestId)
    return value
  }

  settings(signal?: AbortSignal): Promise<Settings> {
    return this.send('/api/settings', 'GET', settingsResponse, { signal })
  }

  create(submission: Submission): Promise<CreatedRun> {
    return this.send('/api/analysis/runs', 'POST', createdRun, {
      body: submission.bytes, headers: { 'Idempotency-Key': submission.key },
    })
  }

  run(runId: string, view: 'auto' | 'history' = 'auto', signal?: AbortSignal): Promise<RunDetail> {
    return this.send(`/api/analysis/runs/${encodeURIComponent(runId)}?view=${view}`, 'GET', runDetail, { signal })
  }

  historyAt(historyUrl: string, signal?: AbortSignal): Promise<RunDetail> {
    if (!/^\/api\/analysis\/runs\/[0-9a-f-]+\?view=history$/i.test(historyUrl)) {
      throw new Error('Invalid backend history URL')
    }
    return this.send(historyUrl as `/api/${string}`, 'GET', runDetail, { signal })
  }

  history(cursor?: string, signal?: AbortSignal): Promise<RunHistory> {
    const query = new URLSearchParams({ limit: '50' })
    if (cursor) query.set('cursor', cursor)
    return this.send(`/api/analysis/runs?${query}`, 'GET', runHistory, { signal })
  }

  transcript(runId: string, sessionId: string, view: 'auto' | 'history' = 'auto', signal?: AbortSignal): Promise<SessionTranscript> {
    return this.send(`/api/analysis/runs/${encodeURIComponent(runId)}/sessions/${encodeURIComponent(sessionId)}/transcript?view=${view}`, 'GET', sessionTranscript, { signal })
  }

  content(runId: string, itemId: string, view: 'auto' | 'history' = 'auto'): Promise<ContentResponse> {
    return this.send(`/api/analysis/runs/${encodeURIComponent(runId)}/transcript/${encodeURIComponent(itemId)}/content?view=${view}`, 'GET', transcriptContent)
  }

  cancel(runId: string): Promise<Schema<'RunStatusResponse'>> {
    return this.send(`/api/analysis/runs/${encodeURIComponent(runId)}/cancel`, 'POST', runStatus)
  }

  retryPersistence(runId: string): Promise<Schema<'PersistenceRetryResponse'>> {
    return this.send(`/api/analysis/runs/${encodeURIComponent(runId)}/persistence/retry`, 'POST', persistenceRetry)
  }

  previewRunCleanup(runId: string, cascadeConsumers: boolean): Promise<CleanupPreview> {
    return this.send('/api/admin/history-cleanups/preview', 'POST', cleanupPreview, {
      body: JSON.stringify({ selection: { kind: 'runs', run_ids: [runId] }, cascade_consumers: cascadeConsumers }),
    })
  }

  executeRunCleanup(token: string, phrase: string): Promise<AdminOperation> {
    return this.send('/api/admin/history-cleanups', 'POST', adminOperation, {
      body: JSON.stringify({ confirmation_token: token, confirmation_phrase: phrase }),
      headers: { 'Idempotency-Key': crypto.randomUUID() },
    })
  }

  operation(operationId: string): Promise<AdminOperation> {
    return this.send(`/api/admin/operations/${encodeURIComponent(operationId)}`, 'GET', adminOperation)
  }
}

export const api = new ApiClient()
