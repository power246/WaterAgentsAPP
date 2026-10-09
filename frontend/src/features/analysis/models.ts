import type { components } from '../../api/generated'
import type { RunDetail } from '../../api/http'

export type Graph = components['schemas']['HydrologyGraph']
export type InferenceResult = components['schemas']['InferenceResult']
export type RunSummaryResult = components['schemas']['RunSummaryResult']

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function graph(value: unknown): value is Graph {
  return record(value) && Array.isArray(value.nodes) && value.nodes.every((node: unknown) =>
    record(node) && typeof node.comid === 'string' && typeof node.participation === 'string' &&
    (node.point === null || (record(node.point) && typeof node.point.latitude === 'number' &&
      typeof node.point.longitude === 'number'))) && record(value.geometries)
}

function inference(value: unknown, runId: string, sessionId: string): value is InferenceResult {
  return record(value) && value.run_id === runId && value.session_id === sessionId &&
    record(value.time_range) && typeof value.analysis_timezone === 'string' &&
    Array.isArray(value.window_assessments) && value.window_assessments.every(record) &&
    Array.isArray(value.series) && value.series.every((series: unknown) =>
      record(series) && record(series.parameter) && Array.isArray(series.points) && series.points.every(record))
}

function summary(value: unknown, runId: string): value is RunSummaryResult {
  return record(value) && value.run_id === runId && typeof value.plain_language_summary === 'string' &&
    Array.isArray(value.highlights) && value.highlights.every(record)
}

export function graphFromDetail(detail: RunDetail | null): Graph | null {
  const item = detail?.items?.find((entry) => entry.content_type === 'topology_ready')
  if (!item || item.content.kind !== 'inline') return null
  const body = item.content.body
  return graph(body) ? body : null
}

export function graphFromText(text: string): Graph | null {
  let value: unknown
  try { value = JSON.parse(text) } catch { return null }
  return graph(value) ? value : null
}

export function inferenceFromText(text: string, runId: string, sessionId: string): InferenceResult | null {
  let value: unknown
  try { value = JSON.parse(text) } catch { return null }
  return inference(value, runId, sessionId) ? value : null
}

export function summaryFromText(text: string, runId: string): RunSummaryResult | null {
  let value: unknown
  try { value = JSON.parse(text) } catch { return null }
  return summary(value, runId) ? value : null
}

export type NodeVisual = 'excluded' | 'pending' | 'running' | 'completed' | 'failed' | 'cancelled' | 'cancelled-before-start'

export function nodeVisual(
  participating: boolean,
  sessionStatus: string | null | undefined,
  runStatus: string,
): NodeVisual {
  if (!participating) return 'excluded'
  if (!sessionStatus) return runStatus === 'cancelled' ? 'cancelled-before-start' : 'pending'
  if (sessionStatus === 'running') return 'running'
  if (sessionStatus === 'completed') return 'completed'
  if (sessionStatus === 'failed') return 'failed'
  if (sessionStatus === 'cancelled') return 'cancelled'
  return 'pending'
}

export const visualLabels: Record<NodeVisual, string> = {
  excluded: 'Outside active analysis',
  pending: 'Waiting to start',
  running: 'Running',
  completed: 'Completed',
  failed: 'Failed',
  cancelled: 'Cancelled',
  'cancelled-before-start': 'Stopped before start',
}

export function originalTimezone(configuration: { analysis_timezone?: string | null } | null | undefined): string {
  return configuration?.analysis_timezone || 'legacy/unknown'
}
