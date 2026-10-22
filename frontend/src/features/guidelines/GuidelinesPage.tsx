import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useBlocker } from 'react-router-dom'
import { api, ApiError } from '../../api/http'
import type { GuidelineDocument, GuidelineDraft, GuidelineList } from '../../api/guidelineContracts'
import { GuidelineViewer, originLabel, safeSourceUrl, stateLabel } from './GuidelineViewer'
import { useGuidelineEditor } from './useGuidelineEditor'

const split = (text: string) => text.split(/[,，\n]/).map((s) => s.trim()).filter(Boolean)

function DraftFields({ draft, onEdit, disabled }: { draft: GuidelineDraft; onEdit: (draft: GuidelineDraft) => void; disabled: boolean }) {
  const change = (patch: Partial<GuidelineDraft>) => onEdit({ ...draft, ...patch })
  return <fieldset disabled={disabled} className="guideline-fields"><legend>说明书内容</legend>
    <label>标题<input maxLength={160} value={draft.title ?? ''} onChange={(e) => change({ title: e.target.value })} /></label>
    <label>简介<textarea maxLength={240} value={draft.summary ?? ''} onChange={(e) => change({ summary: e.target.value })} /></label>
    <label>来源性质<select value={draft.source_kind ?? 'user_authored'} onChange={(e) => change({ source_kind: e.target.value as GuidelineDraft['source_kind'] })}>
      <option value="user_authored">自己编写</option><option value="reference_derived">整理专业资料</option>
    </select></label>
    <label>语言<select value={draft.language ?? 'zh'} onChange={(e) => change({ language: e.target.value as GuidelineDraft['language'] })}>
      <option value="zh">中文</option><option value="en">English</option><option value="mixed">中英混合</option>
    </select></label>
    <label>适用指标（逗号分隔，pH 为 usgs:00400）<input defaultValue={draft.parameter_keys.map((p) => `${p.code_system}:${p.code}`).join(', ')}
      onChange={(e) => change({ parameter_keys: split(e.target.value).map((s) => {
        const [system, code] = s.split(':'); return { code_system: code ? system! : 'usgs', code: code ?? system! }
      }) })} /></label>
    <label>适用场景（逗号分隔）<input defaultValue={draft.scenario_tags.join(', ')} onChange={(e) => change({ scenario_tags: split(e.target.value) })} /></label>
    <label>检索关键词（逗号分隔）<input defaultValue={draft.keywords.join(', ')} onChange={(e) => change({ keywords: split(e.target.value) })} /></label>
    <label>正文<textarea className="guideline-editor-body" value={draft.body_markdown ?? ''} onChange={(e) => change({ body_markdown: e.target.value })} /></label>
    {new TextEncoder().encode(draft.body_markdown ?? '').length > 6000 && <p className="error">正文超过发布长度上限，请拆分后发布；草稿可单独保存。</p>}
    <p className="muted">提示：写清可能机制、适用条件、观察线索、例外与替代解释、估值注意事项。保存草稿不会用于分析。</p>
    <h3>出处{draft.source_kind === 'reference_derived' ? '（整理专业资料必填）' : '（自己编写可不填）'}</h3>
    {draft.sources.map((source, index) => <fieldset key={source.source_id}><legend>出处 {index + 1}</legend>
      <label>出处标题<input value={source.title ?? ''} onChange={(e) => change({ sources: draft.sources.map((s, i) => i === index ? { ...s, title: e.target.value } : s) })} /></label>
      <label>出处网址<input value={source.url ?? ''} onChange={(e) => change({ sources: draft.sources.map((s, i) => i === index ? { ...s, url: e.target.value || null } : s) })} /></label>
      <label>章节或位置<input value={source.locator ?? ''} onChange={(e) => change({ sources: draft.sources.map((s, i) => i === index ? { ...s, locator: e.target.value || null } : s) })} /></label>
      <label>发表日期<input type="date" value={source.published_date ?? ''} onChange={(e) => change({ sources: draft.sources.map((s, i) => i === index ? { ...s, published_date: e.target.value || null } : s) })} /></label>
      <label>访问日期<input type="date" value={source.accessed_date ?? ''} onChange={(e) => change({ sources: draft.sources.map((s, i) => i === index ? { ...s, accessed_date: e.target.value || null } : s) })} /></label>
      <button type="button" onClick={() => change({ sources: draft.sources.filter((_, i) => i !== index) })}>移除此出处</button>
    </fieldset>)}
    <button type="button" disabled={draft.sources.length >= 16} onClick={() => change({ sources: [...draft.sources,
      { source_id: `s_${crypto.randomUUID().replaceAll('-', '')}`, title: '', url: null }] })}>添加出处</button>
  </fieldset>
}

function ReviewDialog({ draft, basedOn, onClose, onPublish }: { draft: GuidelineDraft; basedOn?: string | null;
  onClose: () => void; onPublish: (reviewer: string, note: string) => void }) {
  const [confirmed, setConfirmed] = useState(false), [reviewer, setReviewer] = useState(''), [note, setNote] = useState('')
  const dialog = useRef<HTMLDivElement>(null)
  useEffect(() => { const previous = document.activeElement; dialog.current?.focus();
    return () => { if (previous instanceof HTMLElement) previous.focus() } }, [])
  return <div className="dialog-backdrop"><div ref={dialog} tabIndex={-1} role="dialog" aria-modal="true" aria-label="检查保存版本并发布"
    className="dialog guideline-review" onKeyDown={(e) => {
      if (e.key === 'Escape') { e.preventDefault(); onClose() }
      if (e.key === 'Tab') {
        const elements = [...e.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled), input, textarea, a[href]')]
        const first = elements[0], last = elements.at(-1)
        if (e.shiftKey && (document.activeElement === first || document.activeElement === dialog.current)) { e.preventDefault(); last?.focus() }
        else if (!e.shiftKey && (document.activeElement === last || document.activeElement === dialog.current)) { e.preventDefault(); first?.focus() }
      }
    }}>
    <h2>检查保存版本并发布</h2>
    <p>以下是服务器已保存的确切内容。发布后，文本可能发送给你配置的模型进行准备与分析。</p>
    {basedOn && <p>基于程序自带版本：{basedOn}</p>}
    <GuidelineViewer content={draft} />
    <label>检查人<input value={reviewer} maxLength={160} onChange={(e) => setReviewer(e.target.value)} /></label>
    <label>检查说明<textarea value={note} maxLength={1000} onChange={(e) => setNote(e.target.value)} /></label>
    <label><input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} />我已检查上述内容、适用边界和出处</label>
    <div className="guideline-actions"><button type="button" onClick={onClose}>返回编辑</button>
      <button type="button" disabled={!confirmed || !reviewer.trim() || !note.trim()} onClick={() => onPublish(reviewer.trim(), note.trim())}>确认发布此保存版本</button></div>
  </div></div>
}

export function GuidelinesPage() {
  const [source, setSource] = useState<'bundled' | 'mine'>('bundled')
  const [query, setQuery] = useState(''), [filter, setFilter] = useState({ q: '', state: '' })
  const [cursor, setCursor] = useState(''), [refresh, setRefresh] = useState(0), [newKey, setNewKey] = useState(0)
  const [list, setList] = useState<GuidelineList | null>(null), [error, setError] = useState('')
  const [root, setRoot] = useState<GuidelineDocument | null>(null)
  const changed = useCallback(() => setRefresh((n) => n + 1), [])
  const editor = useGuidelineEditor(changed), { state: s, dirty } = editor
  const blocker = useBlocker(dirty || s.busy || s.uncertain)
  useEffect(() => {
    if (blocker.state === 'blocked') {
      if (editor.canLeave()) blocker.proceed()
      else blocker.reset()
    }
  }, [blocker, editor])
  const doc = s.document
  useEffect(() => {
    const controller = new AbortController()
    setList(null); setError('')
    void api.guidelines({ source, ...filter, cursor }, controller.signal).then((value) => { if (!controller.signal.aborted) setList(value) })
      .catch((cause: unknown) => { if (!controller.signal.aborted) setError(cause instanceof ApiError ? cause.code : '列表不可用') })
    return () => controller.abort()
  }, [source, filter, cursor, refresh])
  useEffect(() => {
    const controller = new AbortController()
    setRoot(null)
    if (doc?.identity.origin === 'local_override') void api.guideline(doc.identity.family_root_id, controller.signal)
      .then((value) => { if (!controller.signal.aborted) setRoot(value) })
      .catch((cause: unknown) => { if (!controller.signal.aborted) setError(cause instanceof ApiError ? cause.code : '程序自带版本不可用') })
    return () => controller.abort()
  }, [doc?.identity.family_root_id, doc?.identity.origin, doc?.revision, refresh])
  const family = doc?.identity.origin === 'local_override' ? root : doc
  const locked = s.busy || s.uncertain || !!s.conflict
  const bundled = doc?.identity.origin === 'bundled'
  const invalidSources = s.draft.source_kind === 'reference_derived' && !s.draft.sources.length ||
    s.draft.sources.some((ref) => !ref.title?.trim() || !!ref.url && !safeSourceUrl(ref.url))
  const publishable = !!s.draft.title?.trim() && !!s.draft.summary?.trim() && !!s.draft.body_markdown?.trim() &&
    s.draft.parameter_keys.length > 0 && !invalidSources && new TextEncoder().encode(s.draft.body_markdown ?? '').length <= 6000
  return <div className="guidelines-page">
    <header className="admin-header"><h1>说明书</h1><nav>
      <Link to="/">分析</Link>{' · '}<Link to="/history">History</Link>
    </nav></header>
    <p>说明书是模型可选的参考资料。人工检查并准备成功后才可用于分析；文本可能交由你配置的模型服务处理。</p>
    <div className="guidelines-grid"><section className="admin-card" aria-label="说明书列表">
      <div className="guideline-actions" role="group" aria-label="资料库">
        {(['bundled', 'mine'] as const).map((view) => <button key={view} type="button" aria-pressed={source === view}
          onClick={() => { setSource(view); setCursor('') }}>{view === 'bundled' ? '程序自带' : '我的说明书'}</button>)}
      </div>
      <form onSubmit={(e) => { e.preventDefault(); setFilter((v) => ({ ...v, q: query })); setCursor('') }}>
        <label>查找说明书<input maxLength={160} value={query} onChange={(e) => setQuery(e.target.value)} /></label>
        <label>状态筛选<select value={filter.state} onChange={(e) => { setFilter((v) => ({ ...v, state: e.target.value })); setCursor('') }}>
          <option value="">全部状态</option>{Object.entries(stateLabel).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select></label><button type="submit">查询</button>
      </form>
      <button type="button" onClick={() => { editor.create(); setNewKey((n) => n + 1) }}>创建说明书</button>
      {error && <p role="alert" className="error">{error}</p>}
      <button type="button" onClick={changed}>重载列表</button>
      {!list ? <p>正在加载列表…</p> : <>
        {!list.items.length && <p>暂无说明书。</p>}
        <ul className="guideline-list">{list.items.map((item) => <li key={item.identity.guideline_id}>
          <button type="button" onClick={() => void editor.load(item.identity.guideline_id)}>{item.title || '未命名说明书'}</button>
          <p>{originLabel[item.identity.origin]} · {item.source_kind === 'reference_derived' ? '专业资料整理' : '用户编写'} · {stateLabel[item.state]}</p>
          <p>{item.summary}</p><p>{item.effective_version_id ? '当前有效版本已保存' : '当前无有效版本'}{item.has_unpublished_changes ? ' · 有未发布修改' : ''}</p>
          {item.has_bundled_update && <p>程序自带内容有更新，本地修改保留。</p>}
        </li>)}</ul>
        {list.next_cursor && <button type="button" onClick={() => setCursor(list.next_cursor!)}>下一页</button>}
        {cursor && <button type="button" onClick={() => setCursor('')}>返回第一页</button>}
      </>}
    </section>
    <section className="admin-card" aria-label="说明书编辑器">
      <h2>{bundled ? '程序自带说明书' : doc ? '编辑说明书' : '创建说明书'}</h2>
      {doc && <><p>{originLabel[doc.identity.origin]} · {stateLabel[doc.state]}</p>
        <p>{doc.effective_version_id ? '当前有效版本：' + doc.effective_version_id : '当前无有效版本'}{doc.has_unpublished_changes && ' · 有未发布修改'}</p>
        {doc.preparation && <p role="status">准备任务：{doc.preparation.state}{doc.preparation.safe_error_code && ` · ${doc.preparation.safe_error_code}`}</p>}
        {doc.state === 'preparing' && <p>正在准备已批准版本；继续编辑仅改变草稿。</p>}
      </>}
      {s.error && <p className="error" role="alert">{s.error}</p>}
      {s.uncertain && <div role="status"><p>请求结果不确定，编辑内容已保留。先重放原请求对账，期间不会创建新请求。</p>
        <button type="button" disabled={s.busy} onClick={editor.reconcile}>使用原请求对账</button></div>}
      {s.conflict && <div role="alert"><p>服务器已有新修订 {s.conflict.revision}，本地编辑未覆盖。</p>
        {s.conflict.draft && <GuidelineViewer content={s.conflict.draft} />}
        <button type="button" onClick={editor.acceptConflict}>保留本地编辑，采用最新修订</button></div>}
      {bundled ? <>
        {doc.active_version && <GuidelineViewer content={doc.active_version.content} version={doc.active_version} />}
        <button type="button" disabled={locked || !doc.latest_bundled_version} onClick={() => {
          const version = doc.latest_bundled_version!
          void editor.mutate(`/api/guidelines/${doc.identity.guideline_id}/local-edit`, { expected_revision: doc.revision,
            base_version_id: version.version_id, base_content_digest: version.model_view.content_digest }, 'local_edit')
        }}>在本机编辑</button><p>修改仅保存在本机；已有本地稿将继续当前内容。</p>
      </> : <>
        <DraftFields key={`${doc?.identity.guideline_id ?? 'new'}:${doc?.revision ?? 0}:${newKey}`} draft={s.draft} onEdit={editor.edit} disabled={s.busy && !s.pending} />
        <p role="status">{dirty ? '有未保存编辑' : '当前编辑已保存'}{s.busy && ' · 请求处理中'}</p>
        <div className="guideline-actions"><button type="button" disabled={locked || (!dirty && !!doc)} onClick={() => void editor.save()}>保存草稿</button>
          <button type="button" disabled={locked || !publishable || doc?.state === 'preparing'} onClick={() => void editor.review()}>检查并发布</button></div>
        {invalidSources && <p className="error">资料整理需要出处；请填写出处标题和有效 HTTP(S) 网址（如有）。</p>}
      </>}
      {doc && <div className="guideline-actions">
        <button type="button" disabled={s.busy || s.uncertain} onClick={() => void editor.refreshDocument()}>读取最新状态（保留编辑）</button>
        <button type="button" disabled={locked || !family} onClick={() => {
          if (family) void editor.mutate(`/api/guidelines/${family.identity.guideline_id}/set-enabled`, { expected_revision: family.revision, enabled: !family.disabled })
        }}>{family?.disabled ? '启用说明书' : '停用说明书'}</button>
        {doc.preparation?.state === 'failed' && <button type="button" disabled={locked} onClick={() => void editor.mutate(
          `/api/guidelines/operations/${doc.preparation!.receipt.operation_id}/retry`, { expected_revision: doc.revision })}>重试准备</button>}
        {family?.latest_bundled_version && <button type="button" disabled={locked} onClick={() => {
          const version = family.latest_bundled_version!
          if (window.confirm(`恢复程序自带版本 ${version.version_number}（${version.version_id}）？本地修改历史将保留。`)) void editor.mutate(
            `/api/guidelines/${family.identity.guideline_id}/restore-bundled`, { expected_revision: family.revision, version_id: version.version_id })
        }}>恢复程序自带版本</button>}
      </div>}
      {doc?.active_version && !bundled && <details><summary>查看已发布保存版本</summary><GuidelineViewer content={doc.active_version.content} version={doc.active_version} /></details>}
    </section></div>
    {s.review && <ReviewDialog draft={s.review.draft} basedOn={s.review.document.identity.based_on_version_id}
      onClose={editor.closeReview} onPublish={(reviewer, note) => void editor.publish(reviewer, note)} />}
  </div>
}
