import type { GuidelineDocument, GuidelineDraft, GuidelineOperation, GuidelineReceipt, GuidelineVersion } from '../src/api/guidelineContracts'
import type { TranscriptItem } from '../src/api/http'
import type { InferenceResult } from '../src/features/analysis/models'

export const id = '00000000-0000-4000-8000-000000000010'
export const runId = '00000000-0000-4000-8000-000000000001'
export const sessionId = '00000000-0000-4000-8000-000000000002'
export const digest = 'a'.repeat(64)
export const draft: GuidelineDraft = { schema_version: 1, title: '机制 A', summary: '合成测试内容，非专业批准。',
  language: 'zh', source_kind: 'user_authored', parameter_keys: [{ code_system: 'usgs', code: '00400' }],
  related_parameter_keys: [], keywords: [], scenario_tags: [], sources: [], body_markdown: '机制 A 原文与适用边界。' }
export const version: GuidelineVersion = { schema_version: 1, version_id: '00000000-0000-4000-8000-000000000011', version_number: 1,
  identity: { guideline_id: id, family_root_id: id, origin: 'local_custom' },
  content: { ...draft, title: draft.title!, summary: draft.summary!, body_markdown: draft.body_markdown!, language: 'zh', source_kind: 'user_authored', sources: [] },
  model_view: { schema_version: 1, title: draft.title!, summary: draft.summary!, body: draft.body_markdown!, source_kind: 'user_authored', source_ids: [], content_digest: digest },
  review: { guideline_id: id, content_digest: digest, review_confirmed: true, reviewer: 'fixture', review_note: 'Synthetic fixture only', reviewed_at: '2026-10-21T00:00:00Z' } }
export const document: GuidelineDocument = { identity: version.identity, revision: 1, activation_epoch: 0,
  disabled: false, draft, draft_digest: digest, state: 'draft', latest_version_number: 0,
  has_bundled_update: false, has_unpublished_changes: true, effective_version_id: null }
export function receipt(key: string, revision = 1, kind: GuidelineReceipt['mutation']['kind'] = 'save_draft', documentId = id): GuidelineReceipt {
  return { document_id: documentId, accepted_revision: revision, content_digest: digest, operation_id: '00000000-0000-4000-8000-000000000012',
    status_code: 200, mutation: { idempotency_key: key, kind, request_fingerprint: digest } }
}
export function operation(state: GuidelineOperation['state']): GuidelineOperation {
  return { receipt: receipt('00000000-0000-4000-8000-000000000099', 1, 'publish'), state, attempts: 1,
    safe_error_code: state === 'failed' ? 'guideline_embedding_unavailable' : null }
}
export function record(itemId: string, type: string, body: unknown, operationId: string | null = 'model-op'): TranscriptItem {
  return { item_id: itemId, run_id: runId, transcript_sequence: 1, occurred_at: '2026-10-21T00:00:00Z',
    content_type: type, content_schema_version: 1, scope: { session_id: sessionId, agent_role: 'waterbody', target_comid: '100' },
    correlation: { request_id: null, operation_id: operationId, error_id: null }, tags: [], rag_eligible: false, content_bytes: 100,
    content_ref: { run_id: runId, item_id: itemId }, content: { kind: 'inline', body: body as never }, parse_status: 'ok',
    confidence: null, provenance: null, parse_error: null }
}
export function knowledgeRecords(phase = 'reasoning_batch') {
  const base = { schema_version: 1, run_id: runId, session_id: sessionId }
  const target = { kind: 'overall', key: 'overall' }
  return [record('delivery', 'guideline_delivery', { ...base, operation_id: 'model-op', phase, submitted: true,
    selection_status: 'retrieved', entries: [{ alias: 'G1_modelop', version }] }),
  record('usage', 'guideline_usage', { ...base, operation_id: 'model-op', phase, target,
    citations: [{ schema_version: 1, delivery_ref: { run_id: runId, session_id: sessionId, item_id: 'delivery' },
      guideline_id: id, family_root_id: id, version_id: version.version_id, origin: 'local_custom', source_kind: 'user_authored', operation_id: 'model-op', phase }] }),
  record('result-usage', 'result_guideline_usage', { ...base, result_id: 'result', bindings: [{ result_id: 'result', target, usage_item_ids: ['usage'] }] }, null)]
}
export const result = { run_id: runId, session_id: sessionId, result_id: 'result', series: [],
  analysis_detail: { steps: [], transcript_item_refs: ['result-usage'] } } as unknown as InferenceResult
