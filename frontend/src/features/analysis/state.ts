import type { components } from '../../api/generated'
import type { RunDetail, RunSnapshot, SessionProcess, TranscriptItem } from '../../api/http'

type Header = components['schemas']['TranscriptItemHeader']

export type RunState = {
  runId: string | null
  snapshot: RunSnapshot | null
  detail: RunDetail | null
}

export const emptyRun: RunState = { runId: null, snapshot: null, detail: null }

export type RunAction =
  | { type: 'open'; runId: string }
  | { type: 'sse'; snapshot: RunSnapshot }
  | { type: 'get'; detail: RunDetail }

export function runReducer(state: RunState, action: RunAction): RunState {
  if (action.type === 'open') return { ...emptyRun, runId: action.runId }
  if (action.type === 'sse') {
    if (state.runId !== action.snapshot.run_id) return state
    return { ...state, snapshot: action.snapshot }
  }
  if (state.runId !== action.detail.run.run_id) return state
  return { ...state, detail: action.detail }
}

export type ProcessItem = {
  itemId: string
  header: Header
  finalized: TranscriptItem | null
  delta: string
  generationStatus: string | null
}

export type TranscriptState = {
  sessionId: string | null
  items: ProcessItem[]
  lastSequence: number
}

export const emptyTranscript: TranscriptState = { sessionId: null, items: [], lastSequence: 0 }

export type TranscriptAction =
  | { type: 'open'; sessionId: string }
  | { type: 'history'; sessionId: string; items: TranscriptItem[] }
  | { type: 'event'; name: 'transcript_item' | 'message_started' | 'message_delta' | 'message_finalized'; data: SessionProcess }

function replaceOrAppend(items: ProcessItem[], item: ProcessItem): ProcessItem[] {
  const at = items.findIndex((existing) => existing.itemId === item.itemId)
  if (at < 0) return [...items, item].sort((a, b) =>
    a.header.transcript_sequence - b.header.transcript_sequence)
  return items.map((existing, index) => index === at ? item : existing)
}

export function transcriptReducer(state: TranscriptState, action: TranscriptAction): TranscriptState {
  if (action.type === 'open') return { ...emptyTranscript, sessionId: action.sessionId }
  if (action.type === 'history') {
    if (state.sessionId !== action.sessionId) return state
    return { ...state, items: action.items.map((item) => ({
      itemId: item.item_id, header: item, finalized: item,
      delta: '', generationStatus: null,
    })) }
  }
  if (state.sessionId !== action.data.session_id || action.data.sequence <= state.lastSequence) return state
  const payload = action.data.payload
  let items = state.items
  if (action.name === 'message_delta' && 'item_id' in payload && 'text' in payload) {
    items = items.map((item) => item.itemId === payload.item_id
      ? { ...item, delta: item.delta + payload.text } : item)
  } else if ('item' in payload) {
    const value = payload.item
    const existing = items.find((item) => item.itemId === value.item_id)
    const finalized = 'content' in value ? value : null
    const generationStatus = 'generation_status' in payload ? payload.generation_status : null
    items = replaceOrAppend(items, {
      itemId: value.item_id,
      header: existing?.header ?? value,
      finalized: finalized ?? existing?.finalized ?? null,
      delta: existing?.delta ?? '',
      generationStatus: generationStatus ?? existing?.generationStatus ?? null,
    })
  }
  return { ...state, items, lastSequence: action.data.sequence }
}
