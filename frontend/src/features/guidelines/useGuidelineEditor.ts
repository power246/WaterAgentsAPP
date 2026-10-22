import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError, confirmedRequestRejected, type Submission } from '../../api/http'
import type { GuidelineDocument, GuidelineDraft } from '../../api/guidelineContracts'

export const blankDraft = (): GuidelineDraft => ({ schema_version: 1, title: '', summary: '', language: 'zh',
  source_kind: 'user_authored', parameter_keys: [], related_parameter_keys: [], keywords: [], scenario_tags: [], sources: [],
  body_markdown: '## 可能机制\n\n## 适用条件\n\n## 可观察线索\n\n## 例外与替代解释\n\n## 估值注意事项\n' })
export function documentDraft(doc: GuidelineDocument): GuidelineDraft {
  return structuredClone(doc.draft ?? doc.active_version?.content ?? doc.latest_bundled_version?.content ?? blankDraft())
}
type Pending = { path: `/api/guidelines${string}`; method: 'POST' | 'PUT'; submission: Submission;
  editVersion: number; selectedId: string | null; kind: 'save' | 'action' | 'local_edit' }
type Review = { document: GuidelineDocument; draft: GuidelineDraft; editVersion: number }
type Editor = { document: GuidelineDocument | null; draft: GuidelineDraft; editVersion: number; acceptedEditVersion: number;
  busy: boolean; uncertain: boolean; pending: Pending | null; review: Review | null;
  conflict: GuidelineDocument | null; error: string; epoch: number }
const initial = (): Editor => ({ document: null, draft: blankDraft(), editVersion: 0, acceptedEditVersion: 0,
  busy: false, uncertain: false, pending: null, review: null, conflict: null, error: '', epoch: 0 })
const message = (cause: unknown) => cause instanceof ApiError ? cause.code : '网络不可用'

export function useGuidelineEditor(onMutation: () => void) {
  const [state, setState] = useState<Editor>(initial)
  const latest = useRef(state)
  const mounted = useRef(true)
  const loadController = useRef<AbortController | null>(null)
  const update = useCallback((fn: (s: Editor) => Editor) => {
    if (!mounted.current) return
    latest.current = fn(latest.current)
    setState(latest.current)
  }, [])
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; loadController.current?.abort() } }, [])
  const dirty = state.editVersion !== state.acceptedEditVersion

  function canLeave() {
    const s = latest.current
    if (s.busy || s.uncertain) { update((v) => ({ ...v, error: '请先对账当前请求，再离开编辑器。' })); return false }
    return s.editVersion === s.acceptedEditVersion || window.confirm('尚有未保存编辑，放弃这些编辑？')
  }
  useEffect(() => {
    const beforeUnload = (event: BeforeUnloadEvent) => {
      const s = latest.current
      if (s.editVersion !== s.acceptedEditVersion || s.busy || s.uncertain) { event.preventDefault(); event.returnValue = '' }
    }
    window.addEventListener('beforeunload', beforeUnload)
    return () => window.removeEventListener('beforeunload', beforeUnload)
  }, [])

  async function load(id: string) {
    if (!canLeave()) return
    loadController.current?.abort()
    const controller = new AbortController()
    loadController.current = controller
    update((s) => ({ ...s, busy: true, error: '', epoch: s.epoch + 1 }))
    try {
      const doc = await api.guideline(id, controller.signal)
      if (!controller.signal.aborted) update((s) => ({ ...initial(), epoch: s.epoch,
        document: doc, draft: documentDraft(doc), acceptedEditVersion: 0 }))
    } catch (cause) {
      if (!controller.signal.aborted) update((s) => ({ ...s, busy: false, error: message(cause) }))
    }
  }
  function create() {
    if (!canLeave()) return
    loadController.current?.abort()
    update((s) => ({ ...initial(), epoch: s.epoch + 1 }))
  }
  function edit(draft: GuidelineDraft) {
    update((s) => ({ ...s, draft, editVersion: s.editVersion + 1, review: null }))
  }
  async function execute(pending: Pending): Promise<GuidelineDocument | null> {
    update((s) => ({ ...s, pending, busy: true, uncertain: false, error: '', review: null, epoch: s.epoch + 1 }))
    let acknowledged = false
    try {
      const receipt = await api.mutateGuideline(pending.path, pending.method, pending.submission)
      acknowledged = true
      if (!receipt.document_id || receipt.mutation.idempotency_key !== pending.submission.key) throw new Error('invalid_receipt')
      if (pending.kind === 'save' && (!Number.isSafeInteger(receipt.accepted_revision) || !receipt.content_digest)) throw new Error('incomplete_save_receipt')
      const accepted = await api.guideline(receipt.document_id)
      if (accepted.identity.guideline_id !== receipt.document_id || accepted.revision < (receipt.accepted_revision ?? 0)) throw new Error('stale_document')
      const doc = pending.kind === 'action' && pending.selectedId && pending.selectedId !== receipt.document_id
        ? await api.guideline(pending.selectedId) : accepted
      update((s) => {
        const sameSavedDraft = pending.kind !== 'save' || doc.draft_digest === receipt.content_digest
        const replace = pending.kind === 'local_edit' || pending.kind === 'save' && sameSavedDraft && s.editVersion === pending.editVersion
        return { ...s, document: doc, draft: replace ? documentDraft(doc) : s.draft,
          acceptedEditVersion: !sameSavedDraft ? -1 : pending.kind === 'save' || pending.kind === 'local_edit' ? pending.editVersion : s.acceptedEditVersion,
          pending: null, busy: false, uncertain: false, conflict: null }
      })
      onMutation()
      return doc
    } catch (cause) {
      const rejected = !acknowledged && confirmedRequestRejected(cause)
      update((s) => ({ ...s, busy: false, uncertain: !rejected, pending: rejected ? null : pending,
        conflict: cause instanceof ApiError ? cause.currentDocument : null, error: message(cause) }))
      return null
    }
  }
  function mutate(path: Pending['path'], body: unknown, kind: Pending['kind'] = 'action', method: 'POST' | 'PUT' = 'POST') {
    const s = latest.current
    if (s.busy || s.uncertain || s.conflict) return Promise.resolve(null)
    return execute({ path, method, kind, editVersion: s.editVersion, selectedId: s.document?.identity.guideline_id ?? null,
      submission: Object.freeze({ key: crypto.randomUUID(), bytes: JSON.stringify(body) }) })
  }
  function save() {
    const s = latest.current
    return s.document ? mutate(`/api/guidelines/${s.document.identity.guideline_id}/draft`,
      { expected_revision: s.document.revision, draft: s.draft }, 'save', 'PUT')
      : mutate('/api/guidelines', s.draft, 'save')
  }
  async function review() {
    let s = latest.current
    if (s.busy || s.uncertain || s.conflict) return
    if (s.editVersion !== s.acceptedEditVersion || !s.document) {
      const submittedVersion = s.editVersion
      if (!await save() || latest.current.editVersion !== submittedVersion) return
    }
    s = latest.current
    if (s.document?.draft && s.document.draft_digest) update((v) => ({ ...v, review: {
      document: structuredClone(s.document!), draft: structuredClone(s.document!.draft!), editVersion: s.editVersion } }))
  }
  function publish(reviewer: string, note: string) {
    const s = latest.current, reviewed = s.review
    if (!reviewed || reviewed.editVersion !== s.editVersion) return Promise.resolve(null)
    return mutate(`/api/guidelines/${reviewed.document.identity.guideline_id}/publish`, {
      expected_revision: reviewed.document.revision, content_digest: reviewed.document.draft_digest,
      review_confirmed: true, reviewer, review_note: note,
    })
  }
  function acceptConflict() {
    update((s) => s.conflict ? { ...s, document: s.conflict.identity.guideline_id === s.document?.identity.guideline_id ? s.conflict : s.document, conflict: null, review: null, error: '',
      epoch: s.epoch + 1, acceptedEditVersion: -1 } : s)
    onMutation()
  }
  async function refreshDocument() {
    const selected = latest.current.document, epoch = latest.current.epoch
    if (!selected || latest.current.busy || latest.current.uncertain) return
    loadController.current?.abort()
    const controller = new AbortController()
    loadController.current = controller
    try {
      const doc = await api.guideline(selected.identity.guideline_id, controller.signal)
      if (!controller.signal.aborted) update((s) => {
        if (s.epoch !== epoch || s.document?.identity.guideline_id !== doc.identity.guideline_id || doc.revision < s.document.revision) return s
        const changed = doc.draft_digest !== s.document.draft_digest
        return { ...s, document: doc, conflict: null, review: null, acceptedEditVersion: changed ? -1 : s.acceptedEditVersion,
          error: changed ? '服务器草稿已变化，本地编辑已保留；请比较后再保存。' : '' }
      })
    } catch (cause) {
      if (!controller.signal.aborted) update((s) => s.epoch === epoch ? { ...s, error: message(cause) } : s)
    }
  }

  const operationId = state.document?.preparation?.receipt.operation_id
  const operationState = state.document?.preparation?.state
  const documentId = state.document?.identity.guideline_id
  useEffect(() => {
    if (!documentId || !operationId || !['pending', 'running'].includes(operationState ?? '') || state.busy || state.uncertain) return
    const controller = new AbortController(), epoch = state.epoch
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      try {
        await api.guidelineOperation(operationId, controller.signal)
        const doc = await api.guideline(documentId, controller.signal)
        if (!controller.signal.aborted) update((s) => {
          if (s.epoch !== epoch || s.document?.identity.guideline_id !== documentId || doc.revision < s.document.revision) return s
          const changed = doc.draft_digest !== s.document.draft_digest
          return { ...s, document: doc, review: null, acceptedEditVersion: changed ? -1 : s.acceptedEditVersion,
            error: changed ? '服务器草稿已变化，本地编辑已保留；请比较后再保存。' : s.error }
        })
      } catch (cause) {
        if (!controller.signal.aborted) update((s) => s.epoch === epoch ? { ...s, error: message(cause) } : s)
      }
      if (!controller.signal.aborted) timer = setTimeout(() => void poll(), 1200)
    }
    timer = setTimeout(() => void poll(), 1200)
    return () => { controller.abort(); clearTimeout(timer) }
  }, [documentId, operationId, operationState, state.epoch, state.busy, state.uncertain, update])

  return { state, dirty, load, create, edit, save, review, publish, mutate, canLeave, acceptConflict, refreshDocument,
    reconcile: () => { if (latest.current.pending && !latest.current.busy) void execute(latest.current.pending) },
    closeReview: () => update((s) => ({ ...s, review: null })) }
}
