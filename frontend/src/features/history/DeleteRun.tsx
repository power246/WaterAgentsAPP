import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { api, ApiError, confirmedRequestRejected, prepareConfirmation,
  type CleanupPreview, type Submission } from '../../api/http'

export function DeleteRun({ runId, onClose, onAccepted }: {
  runId: string
  onClose: () => void
  onAccepted: () => void
}) {
  const [loadedPreview, setLoadedPreview] = useState<{ runId: string; data: CleanupPreview } | null>(null)
  const [phrase, setPhrase] = useState('')
  const [pendingAttempt, setPendingAttempt] = useState<{ runId: string; submission: Submission } | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  const currentRun = useRef(runId)
  const active = useRef(true)
  const requestSequence = useRef(0)
  useLayoutEffect(() => { currentRun.current = runId }, [runId])
  const preview = loadedPreview?.runId === runId ? loadedPreview.data : null
  const attempt = pendingAttempt?.runId === runId ? pendingAttempt.submission : null

  const loadPreview = useCallback(async (cascade: boolean) => {
    const requestedRun = runId
    const request = ++requestSequence.current
    setError('')
    setPhrase('')
    setLoadedPreview(null)
    try {
      const data = await api.previewRunCleanup(requestedRun, cascade)
      if (!active.current || request !== requestSequence.current || currentRun.current !== requestedRun) return
      if (data.requested_run_ids.length !== 1 || data.requested_run_ids[0] !== requestedRun) {
        setError('Preview target changed'); return
      }
      setLoadedPreview({ runId: requestedRun, data })
    } catch (cause) {
      if (active.current && request === requestSequence.current && currentRun.current === requestedRun) {
        setError(cause instanceof ApiError ? cause.code : 'Preview unavailable')
      }
    }
  }, [runId])

  useEffect(() => {
    const sequence = requestSequence
    active.current = true
    setPendingAttempt(null); setSubmitting(false); setPhrase(''); setLoadedPreview(null)
    void loadPreview(false)
    return () => { active.current = false; sequence.current++ }
  }, [loadPreview])

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
        {preview.cascade_required && !preview.confirmation && <button type="button" disabled={Boolean(attempt)} onClick={() => void loadPreview(true)}>Preview whole-run cascade</button>}
        {preview.confirmation && <>
          <p>Enter <strong>{preview.confirmation.required_phrase}</strong> to confirm.</p>
          <input aria-label="Confirmation phrase" value={phrase} disabled={Boolean(attempt)} onChange={(event) => setPhrase(event.target.value)} />
          <button type="button" disabled={submitting || phrase !== preview.confirmation.required_phrase} onClick={() => {
            const submission = attempt ?? prepareConfirmation(preview.confirmation!.token, phrase)
            const submittedRun = runId
            setPendingAttempt({ runId: submittedRun, submission })
            setSubmitting(true)
            setError('')
            void api.executeRunCleanup(submission)
              .then(() => {
                if (active.current && currentRun.current === submittedRun) {
                  setPendingAttempt(null); onAccepted()
                }
              })
              .catch((cause: unknown) => {
                if (!active.current || currentRun.current !== submittedRun) return
                if (confirmedRequestRejected(cause)) { setPendingAttempt(null); setLoadedPreview(null) }
                setError(cause instanceof ApiError ? cause.code : 'Delete unavailable')
              })
              .finally(() => { if (active.current && currentRun.current === submittedRun) setSubmitting(false) })
          }}>{attempt ? 'Retry deleting these runs' : 'Delete these runs'}</button>
        </>}
      </>}
      <p className="muted">Accepted operations continue and are tracked in Admin.</p>
      {error && <p className="error" role="alert">{error}</p>}
      {error && !attempt && <button type="button" onClick={() => void loadPreview(false)}>Preview again</button>}
      <button type="button" onClick={onClose}>Close</button>
    </section>
  </div>
}
