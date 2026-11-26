import type { GuidelineDraft, GuidelineVersion } from '../../api/guidelineContracts'

export const originLabel = { bundled: '程序自带', local_custom: '我的说明书', local_override: '已在本机修改' }
export const stateLabel = { draft: '草稿', preparing: '正在准备', available: '可用于分析',
  preparation_failed: '准备失败', disabled: '已停用', unavailable: '暂不可用' }
export function safeSourceUrl(value: string | null | undefined): string | null {
  if (!value || /[\s\\]/.test(value) || [...value].some((c) => c.charCodeAt(0) < 32)) return null
  try {
    const url = new URL(value)
    const privateQuery = [...url.searchParams.keys()].some((key) => ['api_key', 'token', 'access_token', 'password'].includes(key.toLowerCase()))
    return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password && url.port !== '0' && !privateQuery ? url.href : null
  } catch { return null }
}
export function GuidelineViewer({ content, version }: { content: GuidelineDraft; version?: GuidelineVersion }) {
  return <article className="guideline-viewer">
    <h3>{content.title || '未命名说明书'}</h3>
    {version && <p>{originLabel[version.identity.origin]} · 版本 {version.version_number} · {version.version_id}</p>}
    <p>{content.source_kind === 'reference_derived' ? '专业资料整理' : '用户编写'}</p>
    <p>{content.summary}</p>
    <p>适用指标：{content.parameter_keys.map((p) => `${p.code_system}:${p.code}`).join('、') || '未填写'}</p>
    <p>场景：{content.scenario_tags.join('、') || '未填写'}</p>
    <h4>正文（Markdown 原文）</h4>
    <pre className="guideline-body">{content.body_markdown}</pre>
    <h4>出处</h4>
    {!content.sources.length && <p>{content.source_kind === 'user_authored' ? '用户编写，无外部出处' : '尚未填写出处'}</p>}
    <ul>{content.sources.map((source) => {
      const url = safeSourceUrl(source.url)
      return <li key={source.source_id}>{url ? <a href={url} target="_blank" rel="noopener noreferrer">{source.title || source.source_id}</a>
        : <span>{source.title || source.source_id}</span>}
        {source.locator && <> · {source.locator}</>}{source.published_date && <> · 发表 {source.published_date}</>}
        {source.accessed_date && <> · 访问 {source.accessed_date}</>}
      </li>
    })}</ul>
  </article>
}
