import { useCallback, useEffect, useState } from 'react'
import { api, ApiError, type CleanupPreview } from '../../api/http'

export function DeleteRun({ runId, onClose, onAccepted }: {
  runId: string
  onClose: () => void
  onAccepted: () => void
}) {
  const [preview, setPreview] = useState<CleanupPreview | null>(null)
  const [phrase, setPhrase] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')

  const loadPreview = useCallback(async (cascade: boolean) => {
    setError('')
    setPhrase('')
    try { setPreview(await api.previewRunCleanup(runId, cascade)) }
    catch (cause) { setError(cause instanceof ApiError ? cause.code : 'Preview unavailable') }
  }, [runId])

  useEffect(() => { void loadPreview(false) }, [loadPreview])

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
        {preview.confirmation && <>
          <p>Enter <strong>{preview.confirmation.required_phrase}</strong> to confirm.</p>
          <input aria-label="Confirmation phrase" value={phrase} onChange={(event) => setPhrase(event.target.value)} />
          <button type="button" disabled={submitting || phrase !== preview.confirmation.required_phrase} onClick={() => {
            setSubmitting(true)
            void api.executeRunCleanup(preview.confirmation!.token, phrase)
              .then(() => onAccepted())
              .catch((cause: unknown) => { setError(cause instanceof ApiError ? cause.code : 'Delete unavailable'); setPreview(null) })
              .finally(() => setSubmitting(false))
          }}>Delete these runs</button>
        </>}
      </>}
      <p className="muted">Accepted operations continue and are tracked in Admin.</p>
      {error && <p className="error" role="alert">{error}</p>}
      {error && <button type="button" onClick={() => void loadPreview(false)}>Preview again</button>}
      <button type="button" onClick={onClose}>Close</button>
    </section>
  </div>
}
