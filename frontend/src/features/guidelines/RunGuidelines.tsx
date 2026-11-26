import { useEffect, useMemo, useState } from 'react'
import { api, type TranscriptItem } from '../../api/http'
import { guidelineVersion, object, type GuidelineVersion } from '../../api/guidelineContracts'
import type { InferenceResult } from '../analysis/models'
import { GuidelineViewer } from './GuidelineViewer'

export const guidelineRecords = new Set(['guideline_run_context', 'guideline_retrieval', 'guideline_delivery', 'guideline_usage', 'result_guideline_usage'])
const phaseLabels: Record<string, string> = { evidence_planning: '证据规划', numeric_estimation: '数值估值',
  reasoning_batch: '逐项解释', overall_synthesis: '整体解释', schema_repair: '格式修复' }
type Delivery = { itemId: string; operation: string; phase: string; status: string; versions: GuidelineVersion[] }
type Used = { target: string; phase: string; operation: string; version: GuidelineVersion; deliveryId: string }
function body(item: TranscriptItem, loaded: Map<string, unknown>): Record<string, unknown> | null {
  if (item.parse_status !== 'ok') return null
  let value: unknown = item.content.kind === 'inline' ? item.content.body : loaded.get(item.item_id)
  if (typeof value === 'string') { try { value = JSON.parse(value) as unknown } catch { return null } }
  return object(value) && value.schema_version === 1 && value.run_id === item.run_id &&
    value.session_id === item.scope.session_id ? value : null
}
function targetLabel(target: Record<string, unknown>, result: InferenceResult): string | null {
  if (target.kind === 'overall' && target.key === 'overall') return '整体结论'
  if (target.kind === 'analysis_step' && Number.isSafeInteger(target.step_index) && Number(target.step_index) >= 0 &&
      Number(target.step_index) < result.analysis_detail.steps.length) return `解释步骤 ${Number(target.step_index) + 1}`
  const points = result.series.flatMap((s) => {
    const prefix = `cell:${s.parameter.key.code_system}:${s.parameter.key.code}:`
    return s.points.filter((p) => p.cell_id.startsWith(prefix)).map((p) => ({ cell: p.cell_id, window: p.cell_id.slice(prefix.length) }))
  })
  if (target.kind === 'window' && points.some((p) => p.window === target.window_id)) return `时间窗 ${String(target.window_id)}`
  if (target.kind === 'cell' && points.some((p) => p.cell === target.cell_id && p.window === target.window_id))
    return `结果 ${String(target.cell_id)} · 时间窗 ${String(target.window_id)}`
  return null
}
export function selectRunGuidelines(items: TranscriptItem[], loaded: Map<string, unknown>, runId: string, sessionId: string, result: InferenceResult | null) {
  const current = items.filter((item) => item.run_id === runId && item.scope.session_id === sessionId)
  const records = new Map(current.map((item) => [item.item_id, { item, body: body(item, loaded) }]))
  const deliveries = new Map<string, Delivery>()
  for (const { item, body: value } of records.values()) {
    if (item.content_type !== 'guideline_delivery' || !value || value.submitted !== true ||
        typeof value.operation_id !== 'string' || value.operation_id !== item.correlation.operation_id ||
        typeof value.phase !== 'string' || !phaseLabels[value.phase] || !Array.isArray(value.entries) || value.entries.length > 3 ||
        !value.entries.every((e: unknown) => object(e) && typeof e.alias === 'string' && guidelineVersion(e.version))) continue
    const versions = value.entries.map((e) => (e as { version: GuidelineVersion }).version)
    if ((versions.length > 0) !== (value.selection_status === 'retrieved')) continue
    deliveries.set(item.item_id, { itemId: item.item_id, operation: value.operation_id, phase: value.phase,
      status: String(value.selection_status), versions })
  }
  const used: Used[] = []
  if (result?.run_id === runId && result.session_id === sessionId) for (const ref of result.analysis_detail.transcript_item_refs) {
    const root = records.get(ref)
    if (root?.item.content_type !== 'result_guideline_usage' || root.body?.result_id !== result.result_id || !Array.isArray(root.body.bindings)) continue
    for (const binding of root.body.bindings) {
      if (!object(binding) || binding.result_id !== result.result_id || !object(binding.target) || !Array.isArray(binding.usage_item_ids)) continue
      const label = targetLabel(binding.target, result)
      if (!label) continue
      for (const id of binding.usage_item_ids) {
        const usage = typeof id === 'string' ? records.get(id) : null
        const value = usage?.body
        if (usage?.item.content_type !== 'guideline_usage' || !value || value.operation_id !== usage.item.correlation.operation_id ||
            typeof value.phase !== 'string' || !Array.isArray(value.citations) || !object(value.target)) continue
        // Numeric task lineage is selected by the server's result binding; explanation targets must match exactly.
        const numeric = value.target.kind === 'numeric_task' && value.target.window_id === binding.target.window_id &&
          ['cell', 'window'].includes(String(binding.target.kind))
        if (!numeric && targetLabel(value.target, result) !== label) continue
        for (const citation of value.citations) {
          if (!object(citation) || citation.schema_version !== 1 || !object(citation.delivery_ref) ||
              citation.delivery_ref.run_id !== runId || citation.delivery_ref.session_id !== sessionId ||
              citation.phase !== value.phase || citation.operation_id !== value.operation_id) continue
          const delivery = deliveries.get(String(citation.delivery_ref.item_id))
          if (!delivery || delivery.operation !== value.operation_id || delivery.phase !== value.phase) continue
          const version = delivery.versions.find((v) => v.version_id === citation.version_id &&
            v.identity.guideline_id === citation.guideline_id && v.identity.family_root_id === citation.family_root_id &&
            v.identity.origin === citation.origin && v.content.source_kind === citation.source_kind)
          if (version && !used.some((u) => u.target === label && u.deliveryId === delivery.itemId && u.version.version_id === version.version_id))
            used.push({ target: label, phase: delivery.phase, operation: delivery.operation, version, deliveryId: delivery.itemId })
        }
      }
    }
  }
  return { deliveries: [...deliveries.values()], used }
}

export function RunGuidelines({ items, runId, sessionId, result, readView }: { items: TranscriptItem[]; runId: string;
  sessionId: string; result: InferenceResult | null; readView: 'auto' | 'history' }) {
  const [loaded, setLoaded] = useState<{ key: string; bodies: Map<string, unknown>; failed: boolean } | null>(null)
  const references = useMemo(() => items.filter((i) => ['guideline_delivery', 'guideline_usage', 'result_guideline_usage'].includes(i.content_type) &&
    i.content.kind === 'reference'), [items])
  const key = `${runId}:${sessionId}:${readView}:${references.map((i) => i.item_id).join(',')}`
  useEffect(() => {
    if (!references.length) return
    const controller = new AbortController()
    void Promise.allSettled(references.map(async (item) => {
      const response = await api.content(item.run_id, item.item_id, readView, controller.signal)
      if (response.run_id !== item.run_id || response.item_id !== item.item_id) throw new Error('invalid_content_ref')
      return [item.item_id, JSON.parse(response.text) as unknown] as const
    })).then((results) => {
      if (controller.signal.aborted) return
      setLoaded({ key, bodies: new Map(results.flatMap((r) => r.status === 'fulfilled' ? [r.value] : [])),
        failed: results.some((r) => r.status === 'rejected') })
    })
    return () => controller.abort()
  }, [key, readView, references])
  const saved = loaded?.key === key ? loaded : null
  const { deliveries, used } = selectRunGuidelines(items, saved?.bodies ?? new Map(), runId, sessionId, result)
  return <section aria-label="运行说明书参考" className="run-guidelines">
    <h3>本次提供的说明书</h3>
    <p className="muted">仅列实际提交给模型的保存版本；提交记录不代表模型采用，也不保证供应商成功接收。</p>
    {references.length > 0 && !saved && <p>正在读取保存快照…</p>}
    {saved?.failed && <p role="alert">部分保存引用不可读，无法确认的内容未列为采用。</p>}
    {!deliveries.length && <p>没有可确认的提供记录。</p>}
    {deliveries.map((delivery) => <article key={delivery.itemId} data-item-id={delivery.itemId} className="process-record">
      <p>{phaseLabels[delivery.phase]} · 已提交 · {delivery.status}</p>
      <p className="record-meta">操作 {delivery.operation}</p>
      {!delivery.versions.length && <p>该请求未附带说明书。</p>}
      {delivery.versions.map((version) => <details key={version.version_id}><summary>查看提供的说明书：{version.content.title} · 版本 {version.version_number}</summary>
        <GuidelineViewer content={version.content} version={version} />
        <details><summary>查看模型收到的参考正文</summary><pre className="guideline-body">{version.model_view.body}</pre></details>
      </details>)}
    </article>)}
    <h3>结果采用的说明书</h3>
    {!used.length && <p>当前结果没有已验证的说明书引用。</p>}
    {used.map((entry) => <details key={`${entry.target}:${entry.deliveryId}:${entry.version.version_id}`}>
      <summary>查看参考说明书：{entry.version.content.title} · {entry.target} · {phaseLabels[entry.phase]}</summary>
      <p>阶段：{phaseLabels[entry.phase]} · 操作 {entry.operation}</p>
      <GuidelineViewer content={entry.version.content} version={entry.version} />
    </details>)}
  </section>
}
