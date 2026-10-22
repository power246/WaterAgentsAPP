import type { components } from './generated'

type Schema<K extends keyof components['schemas']> = components['schemas'][K]
export type GuidelineDraft = Schema<'GuidelineDraftV1'>
export type GuidelineDocument = Schema<'GuidelineDocumentDetails'>
export type GuidelineVersion = Schema<'GuidelineVersionV1'>
export type GuidelineList = Schema<'GuidelineListResponse'>
export type GuidelineOperation = Schema<'GuidelineOperation'>
export type GuidelineReceipt = Schema<'GuidelineMutationReceipt'>

export function object(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}
const strings = (value: unknown) => Array.isArray(value) && value.every((s) => typeof s === 'string')
const oneOf = (value: unknown, values: string[]) => typeof value === 'string' && values.includes(value)
function draft(value: unknown): value is GuidelineDraft {
  return object(value) && value.schema_version === 1 &&
    ['title', 'summary', 'body_markdown'].every((key) => value[key] == null || typeof value[key] === 'string') &&
    (value.source_kind == null || oneOf(value.source_kind, ['user_authored', 'reference_derived'])) &&
    (value.language == null || oneOf(value.language, ['zh', 'en', 'mixed'])) &&
    ['keywords', 'scenario_tags'].every((key) => strings(value[key])) &&
    ['parameter_keys', 'related_parameter_keys'].every((key) => Array.isArray(value[key]) &&
      value[key].every((p: unknown) => object(p) && typeof p.code_system === 'string' && typeof p.code === 'string')) &&
    Array.isArray(value.sources) && value.sources.every((s: unknown) => object(s) && typeof s.source_id === 'string' &&
      ['title', 'url', 'locator', 'published_date', 'accessed_date'].every((key) => s[key] == null || typeof s[key] === 'string'))
}
function identity(value: unknown): boolean {
  return object(value) && typeof value.guideline_id === 'string' && typeof value.family_root_id === 'string' &&
    oneOf(value.origin, ['bundled', 'local_custom', 'local_override'])
}
export function guidelineVersion(value: unknown): value is GuidelineVersion {
  return object(value) && value.schema_version === 1 && typeof value.version_id === 'string' &&
    Number.isSafeInteger(value.version_number) && Number(value.version_number) >= 1 && identity(value.identity) && draft(value.content) &&
    typeof value.content.title === 'string' && typeof value.content.summary === 'string' && typeof value.content.body_markdown === 'string' &&
    oneOf(value.content.language, ['zh', 'en', 'mixed']) && oneOf(value.content.source_kind, ['user_authored', 'reference_derived']) &&
    object(value.model_view) && typeof value.model_view.content_digest === 'string' &&
    typeof value.model_view.body === 'string' && typeof value.model_view.title === 'string' && typeof value.model_view.summary === 'string' &&
    strings(value.model_view.source_ids) && object(value.review) && value.review.review_confirmed === true &&
    typeof value.review.content_digest === 'string'
}
export function guidelineReceipt(value: unknown): value is GuidelineReceipt {
  return object(value) && typeof value.operation_id === 'string' && object(value.mutation) &&
    typeof value.mutation.idempotency_key === 'string' && typeof value.mutation.kind === 'string' &&
    typeof value.status_code === 'number' && [200, 201, 202].includes(value.status_code) &&
    (value.document_id == null || typeof value.document_id === 'string') &&
    (value.content_digest == null || typeof value.content_digest === 'string') &&
    (value.version_id == null || typeof value.version_id === 'string') &&
    (value.accepted_revision == null || Number.isSafeInteger(value.accepted_revision))
}
export function guidelineOperation(value: unknown): value is GuidelineOperation {
  return object(value) && guidelineReceipt(value.receipt) && Number.isSafeInteger(value.attempts) &&
    oneOf(value.state, ['pending', 'running', 'succeeded', 'failed', 'superseded']) &&
    (value.safe_error_code == null || typeof value.safe_error_code === 'string')
}
const states = ['draft', 'preparing', 'available', 'preparation_failed', 'disabled', 'unavailable']
export function guidelineDocument(value: unknown): value is GuidelineDocument {
  return object(value) && identity(value.identity) && Number.isSafeInteger(value.revision) &&
    oneOf(value.state, states) && typeof value.disabled === 'boolean' &&
    typeof value.has_unpublished_changes === 'boolean' &&
    (value.draft == null || draft(value.draft)) &&
    (value.draft_digest == null || typeof value.draft_digest === 'string') &&
    ['active_version', 'pending_version', 'latest_bundled_version'].every((key) => value[key] == null || guidelineVersion(value[key])) &&
    (value.preparation == null || guidelineOperation(value.preparation))
}
export function guidelineList(value: unknown): value is GuidelineList {
  return object(value) && (value.next_cursor === null || typeof value.next_cursor === 'string') &&
    Array.isArray(value.items) && value.items.every((row: unknown) => object(row) && identity(row.identity) &&
      Number.isSafeInteger(row.revision) && oneOf(row.state, states) &&
      (row.title == null || typeof row.title === 'string') && (row.summary == null || typeof row.summary === 'string'))
}
