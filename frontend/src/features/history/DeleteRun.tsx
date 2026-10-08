import { useCallback, useEffect, useState } from 'react'
import { api, ApiError, type CleanupPreview } from '../../api/http'

export function DeleteRun({ runId, onClose, onDeleted }: {
  runId: string
  onClose: () => void
  onDeleted: () => void
}) {
  const [preview, setPreview] = useState<CleanupPreview | null>(null)
  const [phrase, setPhrase] = useState('')
  const [operationId, setOperationId] = useState<string | null>(null)
  const [error, setError] = useState('')

  const loadPreview = useCallback(async (cascade: boolean) => {
    setError('')
    setPhrase('')
    try { setPreview(await api.previewRunCleanup(runId, cascade)) }
    catch (cause) { setError(cause instanceof ApiError ? cause.code : 'Preview unavailable') }
  }, [runId])

  useEffect(() => { void loadPreview(false) }, [loadPreview])
  useEffect(() => {
    if (!operationId) return
    const controller = new AbortController()
    const timer = setInterval(() => {
      if (controller.signal.aborted) return
      void api.operation(operationId).then((operation) => {
        if (operation.state === 'succeeded') { clearInterval(timer); onDeleted() }
        if (operation.state === 'failed' || operation.state === 'interrupted') {
          clearInterval(timer)
          setError(operation.safe_error?.code ?? 'Operation did not complete')
        }
      }).catch((cause: unknown) => setError(cause instanceof ApiError ? cause.code : 'Operation status unavailable'))
    }, 750)
    return () => { controller.abort(); clearInterval(timer) }
  }, [operationId, onDeleted])

  return <div className="dialog-backdrop" role="presentation">
    <section className="dialog" role="dialog" aria-modal="true" aria-label="Delete saved run">
      <h2>Delete saved run</h2>
      <p>Run {runId}</p>
      {!preview && !error && <p>Preparing dependency preview…</p>}
      {preview && <>
        <p>{preview.estimated.runs} runs, {preview.estimated.sessions} sessions and {preview.estimated.transcript_items} transcript items will be removed.</p>
        <p>{preview.estimated.retained_idempotency_tombstones} idempotency tombstones retained.</p>
        <p>Preserved: {preview.preserved.join(', ')}</p>
        {preview.delete_run_ids.length > 1 && <ul>{preview.delete_run_ids.map((id) => <li key={id}>{id}</li>)}</ul>}
        {preview.dependencies.length > 0 && <p>Dependent links: {preview.dependencies.length}</p>}
        {preview.cascade_required && !preview.confirmation && <button type="button" onClick={() => void loadPreview(true)}>Preview whole-run cascade</button>}
        {preview.confirmation && !operationId && <>
          <p>Enter <strong>{preview.confirmation.required_phrase}</strong> to confirm.</p>
          <input aria-label="Confirmation phrase" value={phrase} onChange={(event) => setPhrase(event.target.value)} />
          <button type="button" disabled={phrase !== preview.confirmation.required_phrase} onClick={() => {
            void api.executeRunCleanup(preview.confirmation!.token, phrase)
              .then((operation) => setOperationId(operation.operation_id))
              .catch((cause: unknown) => setError(cause instanceof ApiError ? cause.code : 'Delete unavailable'))
          }}>Delete these runs</button>
        </>}
      </>}
      {operationId && <p role="status">Deletion operation {operationId} is running.</p>}
      {error && <p className="error" role="alert">{error}</p>}
      <button type="button" onClick={onClose}>Close</button>
    </section>
  </div>
}
