import type { components } from './generated'
import { guidelineDocument, guidelineList, guidelineOperation, guidelineReceipt,
  type GuidelineDocument, type GuidelineList, type GuidelineOperation, type GuidelineReceipt } from './guidelineContracts'

type Schema<Name extends keyof components['schemas']> = components['schemas'][Name]
export type CreateRequest = Schema<'CreateAnalysisRunRequest'>
export type CreatedRun = Schema<'AnalysisRunResponse'>
export type RunDetail = Schema<'RunDetailResponse'>
export type RunHistory = Schema<'RunHistoryResponse'>
export type SessionTranscript = Schema<'SessionTranscriptResponse'>
export type ContentResponse = Schema<'TranscriptContentResponse'>
export type AnchorDistanceEstimate = Schema<'AnchorDistanceEstimate'>
export type AnchorDistanceEstimatesResponse = Schema<'AnchorDistanceEstimatesResponse'>
export type Settings = Schema<'SettingsResponse'>
export type Defaults = Schema<'MutableDefaults'>
export type UnitEntry = Schema<'UnitPolicyEntry'>
export type UnitPreview = Schema<'UnitPolicyPreviewResponse'>
export type UnitPolicy = Schema<'UnitPolicyResponse'>
export type KeyStatus = Schema<'KeyStatusItem'>
export type KeyList = Schema<'KeyStatusResponse'>
export type KeyTest = Schema<'CredentialTestResponse'>
export type AdminStorage = Schema<'AdminStorageStatusResponse'>
export type AdminRag = Schema<'AdminRAGStatusResponse'>
export type AdminOperationList = Schema<'AdminOperationListResponse'>
export type ProfilePreview = Schema<'ProfileHandoffPreviewResponse'>
export type GenerationPreview = Schema<'GenerationPreviewResponse'>
export type AdminKind = Schema<'AdminActionKind'>
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
    readonly currentSettings: Settings | null = null,
    readonly currentDocument: GuidelineDocument | null = null,
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

function anchorDistanceEstimatesResponse(value: unknown): value is AnchorDistanceEstimatesResponse {
  return record(value) && typeof value.result_item_id === 'string' && Array.isArray(value.estimates) &&
    value.estimates.every((item: unknown) => record(item) && typeof item.cell_id === 'string' &&
      typeof item.anchor_index === 'number' && Number.isSafeInteger(item.anchor_index) && item.anchor_index >= 0 &&
      typeof item.distance_km === 'number' && Number.isFinite(item.distance_km) && item.distance_km >= 0 &&
      item.distance_basis === 'mapped_channel_estimate')
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

function unitPreview(value: unknown): value is UnitPreview {
  return record(value) && record(value.proposed) && Array.isArray(value.changes) &&
    Array.isArray(value.affected_dimensions) && typeof value.registry_digest === 'string'
}

function unitPolicy(value: unknown): value is UnitPolicy {
  return record(value) && Number.isSafeInteger(value.version) && Array.isArray(value.entries) &&
    typeof value.registry_digest === 'string'
}

function keyStatus(value: unknown): value is KeyStatus {
  return record(value) && (value.key_name === 'openai' || value.key_name === 'google_geocoding') &&
    typeof value.configured === 'boolean' && typeof value.source === 'string' &&
    typeof value.store_available === 'boolean'
}

function keyList(value: unknown): value is KeyList {
  return record(value) && Array.isArray(value.keys) && value.keys.every(keyStatus)
}

function keyTest(value: unknown): value is KeyTest {
  return record(value) && typeof value.key_name === 'string' &&
    (value.status === 'succeeded' || value.status === 'failed')
}

function adminStorage(value: unknown): value is AdminStorage {
  return record(value) && record(value.history) && record(value.rag_index) &&
    record(value.evidence_cache) && record(value.credential_store)
}

function adminRag(value: unknown): value is AdminRag {
  return record(value) && record(value.index) && record(value.handoff)
}

function operationList(value: unknown): value is AdminOperationList {
  return record(value) && Array.isArray(value.items) && value.items.every(adminOperation)
}

function profilePreview(value: unknown): value is ProfilePreview {
  return record(value) && record(value.handoff) && typeof value.estimated_jobs === 'number'
}

function generationPreview(value: unknown): value is GenerationPreview {
  return record(value) && typeof value.generation_id === 'string' && typeof value.affected_jobs === 'number'
}

function errorEnvelope(value: unknown): { code: string; historyUrl: string | null; currentSettings: Settings | null; currentDocument: GuidelineDocument | null } {
  if (!record(value)) return { code: 'invalid_error_response', historyUrl: null, currentSettings: null, currentDocument: null }
  const detail = value.error
  return {
    code: record(detail) && typeof detail.code === 'string' ? detail.code : 'invalid_error_response',
    historyUrl: typeof value.history_url === 'string' && value.history_url.startsWith('/api/analysis/runs/')
      ? value.history_url : null,
    currentSettings: settingsResponse(value.current_settings) ? value.current_settings : null,
    currentDocument: guidelineDocument(value.current_document) ? value.current_document : null,
  }
}

export type Submission = Readonly<{ key: string; bytes: string }>

export function prepareSubmission(body: CreateRequest): Submission {
  return Object.freeze({ key: crypto.randomUUID(), bytes: JSON.stringify(body) })
}

export function prepareConfirmation(token: string, phrase: string): Submission {
  return Object.freeze({ key: crypto.randomUUID(),
    bytes: JSON.stringify({ confirmation_token: token, confirmation_phrase: phrase }) })
}

export function confirmedRequestRejected(cause: unknown): boolean {
  return cause instanceof ApiError && cause.status >= 400 && cause.status < 500 &&
    !['invalid_content_type', 'invalid_json', 'invalid_response'].includes(cause.code)
}

export class ApiClient {
  constructor(private readonly fetcher: typeof fetch = (...args) => fetch(...args)) {}

  private async send<T>(
    path: `/api/${string}`,
    method: 'GET' | 'POST' | 'PUT' | 'DELETE',
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
      throw new ApiError(response.status, detail.code, requestId, detail.historyUrl, detail.currentSettings, detail.currentDocument)
    }
    if (!valid(value)) throw new ApiError(response.status, 'invalid_response', requestId)
    return value
  }

  settings(signal?: AbortSignal): Promise<Settings> {
    return this.send('/api/settings', 'GET', settingsResponse, { signal })
  }

  guidelines(query: { source: 'bundled' | 'mine'; q?: string; state?: string; cursor?: string }, signal?: AbortSignal): Promise<GuidelineList> {
    const params = new URLSearchParams({ source: query.source, limit: '20' })
    for (const key of ['q', 'state', 'cursor'] as const) if (query[key]) params.set(key, query[key])
    return this.send(`/api/guidelines?${params}`, 'GET', guidelineList, { signal })
  }

  async guideline(id: string, signal?: AbortSignal): Promise<GuidelineDocument> {
    const value = await this.send(`/api/guidelines/${encodeURIComponent(id)}`, 'GET', guidelineDocument, { signal })
    if (value.identity.guideline_id !== id) throw new ApiError(200, 'invalid_response', null)
    return value
  }

  mutateGuideline(path: `/api/guidelines${string}`, method: 'POST' | 'PUT', submission: Submission): Promise<GuidelineReceipt> {
    return this.send(path, method, guidelineReceipt, { body: submission.bytes, headers: { 'Idempotency-Key': submission.key }, signal: AbortSignal.timeout(20_000) })
  }

  async guidelineOperation(id: string, signal?: AbortSignal): Promise<GuidelineOperation> {
    const value = await this.send(`/api/guidelines/operations/${encodeURIComponent(id)}`, 'GET', guidelineOperation, { signal })
    if (value.receipt.operation_id !== id) throw new ApiError(200, 'invalid_response', null)
    return value
  }

  replaceSettings(expectedRevision: number, defaults: Defaults): Promise<Settings> {
    return this.send('/api/settings', 'PUT', settingsResponse, {
      body: JSON.stringify({ expected_revision: expectedRevision, defaults }),
    })
  }

  previewUnitPolicy(baseVersion: number, entries: UnitEntry[]): Promise<UnitPreview> {
    return this.send('/api/settings/unit-policies/preview', 'POST', unitPreview, {
      body: JSON.stringify({ base_version: baseVersion, entries }),
    })
  }

  confirmUnitPolicy(submission: Submission): Promise<UnitPolicy> {
    return this.send('/api/settings/unit-policies', 'POST', unitPolicy, {
      body: submission.bytes, headers: { 'Idempotency-Key': submission.key },
    })
  }

  keys(signal?: AbortSignal): Promise<KeyList> {
    return this.send('/api/keys', 'GET', keyList, { signal })
  }

  saveKey(name: KeyStatus['key_name'], value: string): Promise<KeyStatus> {
    return this.send(`/api/keys/${name}`, 'PUT', keyStatus, { body: JSON.stringify({ value }) })
  }

  deleteKey(name: KeyStatus['key_name']): Promise<KeyStatus> {
    return this.send(`/api/keys/${name}`, 'DELETE', keyStatus)
  }

  testKey(name: KeyStatus['key_name']): Promise<KeyTest> {
    return this.send(`/api/keys/${name}/test`, 'POST', keyTest)
  }

  adminStorage(signal?: AbortSignal): Promise<AdminStorage> {
    return this.send('/api/admin/storage', 'GET', adminStorage, { signal })
  }

  adminRag(signal?: AbortSignal): Promise<AdminRag> {
    return this.send('/api/admin/rag', 'GET', adminRag, { signal })
  }

  operations(cursor?: string, signal?: AbortSignal): Promise<AdminOperationList> {
    const query = new URLSearchParams({ limit: '50' })
    if (cursor) query.set('cursor', cursor)
    return this.send(`/api/admin/operations?${query}`, 'GET', operationList, { signal })
  }

  previewHistoryCleanup(selection: Schema<'HistoryCleanupPreviewRequest'>['selection'], cascade: boolean): Promise<CleanupPreview> {
    return this.send('/api/admin/history-cleanups/preview', 'POST', cleanupPreview, {
      body: JSON.stringify({ selection, cascade_consumers: cascade }),
    })
  }

  previewProfileHandoff(): Promise<ProfilePreview> {
    return this.send('/api/admin/rag/profile-handoffs/preview', 'POST', profilePreview)
  }

  previewFailedJobs(generationId: string, safeErrorCodes: string[], limit: number): Promise<GenerationPreview> {
    return this.send(`/api/admin/rag/generations/${encodeURIComponent(generationId)}/failed-jobs/retry-preview`,
      'POST', generationPreview, { body: JSON.stringify({ safe_error_codes: safeErrorCodes, limit }) })
  }

  previewGenerationDeletion(generationId: string): Promise<GenerationPreview> {
    return this.send(`/api/admin/rag/generations/${encodeURIComponent(generationId)}/deletion-preview`, 'POST', generationPreview)
  }

  executeAdmin(kind: AdminKind, submission: Submission, generationId?: string): Promise<AdminOperation> {
    const generation = generationId ? encodeURIComponent(generationId) : ''
    const path: `/api/${string}` = kind === 'history_cleanup' ? '/api/admin/history-cleanups'
      : kind === 'profile_handoff' ? '/api/admin/rag/profile-handoffs'
        : kind === 'retry_failed_jobs' ? `/api/admin/rag/generations/${generation}/failed-jobs/retries`
          : `/api/admin/rag/generations/${generation}/deletions`
    return this.send(path, 'POST', adminOperation, {
      body: submission.bytes, headers: { 'Idempotency-Key': submission.key },
    })
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

  content(runId: string, itemId: string, view: 'auto' | 'history' = 'auto', signal?: AbortSignal): Promise<ContentResponse> {
    return this.send(`/api/analysis/runs/${encodeURIComponent(runId)}/transcript/${encodeURIComponent(itemId)}/content?view=${view}`, 'GET', transcriptContent, { signal })
  }

  anchorDistanceEstimates(runId: string, itemId: string, signal?: AbortSignal): Promise<AnchorDistanceEstimatesResponse> {
    return this.send(`/api/analysis/runs/${encodeURIComponent(runId)}/transcript/${encodeURIComponent(itemId)}/anchor-distance-estimates?view=history`,
      'GET', anchorDistanceEstimatesResponse, { signal })
  }

  cancel(runId: string): Promise<Schema<'RunStatusResponse'>> {
    return this.send(`/api/analysis/runs/${encodeURIComponent(runId)}/cancel`, 'POST', runStatus)
  }

  retryPersistence(runId: string): Promise<Schema<'PersistenceRetryResponse'>> {
    return this.send(`/api/analysis/runs/${encodeURIComponent(runId)}/persistence/retry`, 'POST', persistenceRetry)
  }

  previewRunCleanup(runId: string, cascadeConsumers: boolean): Promise<CleanupPreview> {
    return this.previewHistoryCleanup({ kind: 'runs', run_ids: [runId] }, cascadeConsumers)
  }

  executeRunCleanup(submission: Submission): Promise<AdminOperation> {
    return this.executeAdmin('history_cleanup', submission)
  }

  operation(operationId: string, signal?: AbortSignal): Promise<AdminOperation> {
    return this.send(`/api/admin/operations/${encodeURIComponent(operationId)}`, 'GET', adminOperation, { signal })
  }
}

export const api = new ApiClient()
