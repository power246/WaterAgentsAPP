# M8 Memory/RAG from Transcript 设计

更新日期：2026-10-07

本文冻结 M8 的产品与架构决策。它继承 M1–M7 的现有边界，并定型跨 run memory 的 source eligibility、query-to-query retrieval、生命周期、失败语义与 M9 接力。它是设计输入，不是实现完成声明；实施 TODO 本轮不编写。

## 1. 目标与边界

M8 将已经持久化的合格 transcript 内容投影为可检索的跨 run memory，并通过 M5 已预留的 `search_transcript_evidence` producer slot 向 `memory_augmented` 水体 session 提供 historical evidence：

```text
completed + persistence-confirmed transcript item
  -> eligibility and safe-projection validation
  -> RAG projection/index
  -> bounded fixed-strategy retrieval
  -> HistoricalTranscriptRecord
  -> current-session evidence/transcript audit
```

M8 负责：

- 从 `rag_eligible=true` 的 transcript item 生成有界、安全、可版本化的检索投影；
- 支持 `stateless` 与 `memory_augmented` 两种既有 memory mode；
- 实现文档要求的固定检索策略及其确定性 scope、排序、上限和审计字段；
- 将命中包装为显式 `historical_inference`，保留原来源身份、原 confidence、schema version、RAG score 与 `matched_by`；
- 将本次实际检索和采用的历史 evidence 写入当前 session transcript；
- 为 M9 的 index/derived-memory cleanup 留下明确 ownership 和删除边界。

M8 不负责：

- 改写 M4 query understanding、topology/BFS 或 parameter capability registry；
- 复制 M3 transcript store、M5 executor/bundle/materializer、M6 reasoning/gate/result assembler 或 M7 scheduler/upstream store；
- 把 index 当作新的 evidence 事实源，或复制完整 source records、provider envelope、secret、raw model response 或无界 tool payload；
- 新增 direct RAG HTTP route、第三条 SSE、隐藏 reasoning 字段或 milestone feature/read compatibility branch；
- 实现 M9 frontend/settings/keys/admin 与最终 demo closure。

## 2. 已继承的确定契约

### D01：candidate 标记不是最终索引资格

`rag_eligible` 默认 `false`，只由 typed content 的既有生产方和固定 policy 计算。`rag_eligible=true` 仍只是 candidate；M8 upsert 前还必须同时验证：

- 原 item 的 content type/schema version 受当前 projection registry 支持；
- 所属 waterbody session 与 run 均正常 `completed`；
- 原 item 及其引用闭包已确认持久化；
- projection 已脱敏、引用完整、字段有界并通过当前 projection schema；
- operation/content 未 cancelled、failed 或 incomplete。

raw source-record batch、tool call/result、inspection page、empty/unavailable/invalid fact、bundle、error/diagnostic、cancelled/incomplete 内容不得索引。非 eligible 内容仍按 M3 历史读取契约正常可读。

### D02：index 是可重建检索视图，不是事实源

projection 只保存检索所需的有界语义文本和结构化字段，以及稳定的原 `run_id`、适用 `session_id`、`item_id`、COMID、ParameterKey/window、quality/applicability、schema version、时间、confidence 与 provenance。

命中后仍以原 transcript item 为权威；原内容通过 `run_id + item_id` 读取并校验归属。index row、embedding、关键词向量、score 和摘要均不得成为独立 evidence 真相或可编辑业务记录。

### D03：memory mode 只控制跨 run 历史记忆

- `stateless`：不得检索或使用其他 run 的历史 memory；同一 run 内 M7 直接上游结果仍按既有契约传递。
- `memory_augmented`：允许通过显式 historical source 结构关联其他 run 的合格内容。

普通 `transcript_item_refs` 继续只能引用同一 run。跨 run 命中不能伪装成本次 session 原过程，也不能通过复制同义 item 绕过边界。

### D04：历史命中默认只具有 contextual 资格

每个命中必须保持 `historical_inference/contextual_only`，不得：

- 满足本次 target/discharge sufficiency gate；
- 替代当前 observation collection；
- 覆盖当前 session 的合格实际观测；
- 因 RAG score 提升原 confidence、quality、applicability、attribution basis 或 result source；
- 把历史或上游 observation 显示为当前水体的本地 observation。

低 confidence 历史内容可以被检索，但必须保留原 confidence；按 Policy Catalog，它不污染 `rag_score`，而是在同等检索相关候选中降低排序资格，最终仍只能作为 contextual evidence。

### D05：复用 M5 producer slot 和 M6 bounded tool loop

M8 实现现有 `TranscriptEvidencePort`，使 `search_transcript_evidence` 只在 `memory_augmented` scope 且 producer ready 时进入既有 tool roster。调用继续经过 M5 的 scope authorization、budget、deadline、transcript writer、provider-result validation、bundle 和 evidence materialization，不增加旁路检索入口。

返回记录沿用 `HistoricalTranscriptRecord` 与 `HistoricalInferencePayload` 的方向，并在 M8 定型 fixed-strategy query、完整 historical source DTO 和 projection schema。M7 dependency ready、direct-upstream scope、当前结果优先级、target result authority 与 run summary authority均不改变。

### D06：审计记录必须闭环

实际检索请求、固定策略、规范化 filters、limit、命中身份、score、`matched_by`、采用/未采用结果及安全失败必须能够从当前 session transcript 审计。进入 evidence 的历史记录必须保留原 run/session/item identity、原 content type/schema version、原 confidence/quality/provenance 和原有效时间范围。

### D07：同一 PostgreSQL 承载结构化、全文与向量检索

M8 不引入独立 vector database。RAG projection 与现有 transcript 事实保存在同一 PostgreSQL database，并组合：

- B-tree/GiST 等适用索引完成 COMID、ParameterKey、时间、season、空间或 topology scope 的结构化候选过滤；
- PostgreSQL full-text search/GIN 完成关键词候选召回；
- `pgvector` 完成 semantic query 的向量候选召回。

固定检索策略先建立授权后的结构化候选范围，再按该策略允许的全文/向量信号排序；不得通过全库向量近邻绕过 COMID、parameter、time、topology 或 memory-mode scope。hybrid ranking 的归一化、固定权重和 tie-break 以 M8 Policy Catalog 为唯一执行输入。

本地 Compose/迁移必须显式提供并启用版本受控的 `vector` extension；正式部署在 startup 前通过 schema/version check 验证扩展和迁移，不允许 application startup 临时安装扩展。索引仍是可删除、可重建的派生视图，原 transcript item 与历史读取不依赖 index row 存活。

首版使用 active generation 内的 exact vector scan，不建立 HNSW/IVFFlat 近似索引。先以正确性、可复现排序和规模基线完成首版；只有容量测试证明 exact scan 已越过明确预算，并且离线 corpus 上通过 recall、scope 和稳定 tie-break 验收后，才允许另行版本化近似索引。`pgvector` 本身不授权静默接受近似结果漂移。

### D08：全系统只有一个 active embedding profile

embedding 是 index 基础设施配置，不是 run configuration，也不由用户在每次分析时选择。现有 model catalog 增加独立 `embedding` role；不得复用或根据本次 `query_parser`/`waterbody` model 隐式推导 embedding model。catalog中的唯一embedding entry表达期望构建的profile，真正服务search的profile由PostgreSQL唯一active generation决定；切换期间可同时存在一个旧active和一个新building，但不能有两个active。

application 只依赖 `EmbeddingPort`。每个 index generation 固定一个 `embedding_profile_id`，其 identity 至少覆盖 provider、model、output dimensions、distance metric、semantic minimum similarity、projection schema/version 与 canonical text template/version。projection row 与 query embedding 必须使用同一 profile；不同 profile 的向量不得比较、融合或共用近邻索引。

修改 active embedding configuration 时，不原地混写旧 generation：先用新 profile 构建并验证完整的新 generation，再原子切换 active generation；失败保持旧 generation 可用。旧 generation 的保留/删除由 M8 ownership 和 M9 cleanup contract 共同约束，不能删除或改写原 transcript item。

M9 可以管理默认 embedding configuration，但变更只影响后续生成和显式 reindex，不改变既有 run/session/transcript 事实。

### D09：首版只交付供应商 embedding adapter

M8 首版通过 `EmbeddingPort` 调用远程供应商 embedding API，不同时交付真实本地 embedding runtime、模型权重下载、GPU/CPU 推理服务或本地模型运维路径。tests 使用 deterministic fake port；未来本地实现必须替换同一 port，不能建立第二套 projection/index/search contract。

发送给供应商的内容只能是通过 M8 projection policy 的有界、脱敏 canonical text；不得发送完整 transcript、raw source records、provider/model raw response、secret、内部错误或无界 tool payload。调用结果必须校验 exact profile identity、向量维度、有限数值、item 对应关系和 batch 完整性，raw embedding response 不进入公共 transcript。

首版 supplier/model 的精确选择通过唯一 active embedding profile 配置，不由 Agent、run request 或调用方自由填写。供应商不可用、限流、超时、部分 batch 和无效向量均使该 job attempt 失败；worker 只做 policy 允许的有限重试，耗尽后保留可恢复的 failed job，不能写入半成品 projection。

### D10：历史检索只由 waterbody Agent 显式发起

M8 不在 run、topology 或 waterbody session 启动时自动预检索，也不把历史结果隐藏注入 prompt。`memory_augmented` 只使现有 `search_transcript_evidence` 在 M6 bounded planning tool roster 中可用；Agent 根据当前 evidence 和任务决定是否调用，并为每次调用选择一个后端登记的固定检索策略。

后端继续拥有全部授权与执行权：从 sealed scope 校验/补全 COMID、ParameterKey、window、topology neighborhood、limit 和允许的 strategy fields，拒绝任意 SQL、任意 source run/item、scope 外水体/参数/时间及自由数据库过滤。调用结果通过现有 M5 tool result、source record、fragment、bundle 和 transcript 路径进入本次 session。

以下路径不得触发历史检索：

- `stateless` session；
- M7/M6 已确定的 zero-model 收敛路径；
- `run_summarizer`；
- direct HTTP、startup hook、background prompt enrichment 或其他隐藏旁路。

Agent 没有调用历史工具仍是合法执行；`memory_augmented` 表示允许受控检索，不保证每个 waterbody 都产生历史命中。每个 session 最多两次历史检索，每次只能选择一个固定策略；每次最多返回五个 case，具体候选池、去重与 token fit 由 Policy Catalog 固定。

### D11：indexing 最终一致，不阻塞原 run 终态

原 run/session/transcript 按 M3–M7 契约完成并确认持久化后，M8 才为最终合格 candidate 建立幂等、持久化的 indexing job。run completion、waterbody result、history 读取和 persistence 终态不等待 embedding/indexing，也不因 indexing unavailable、timeout、rate limit、invalid response 或 retry exhaustion 被改写。

index worker 使用有界 batch、并发、deadline、attempt 和退避；process restart 后从 PostgreSQL 恢复未完成 job。job identity 至少固定 source run/session/item、content type/schema、projection version 和 embedding profile/generation；重复投递不得产生重复 projection row。projection upsert 与 job completed transition 原子提交。

检索只读取 active generation 中完整 `ready` 的 projection：pending/failed job 不暴露半成品，也不在查询时回退扫描 raw transcript 或临时调用另一模型补 embedding。因此最新 eligible transcript 在 job 完成前可能尚未成为检索命中，但原历史始终可读；该 index lag 是派生视图最终一致语义，不伪装成 source history 缺失或 empty collection。

重试耗尽只保留安全 job 状态和脱敏诊断，后续由显式 retry/reindex 恢复；不得无限自动重试。projection/model schema 变更通过新 generation backfill、验证和原子 active switch 完成，失败保持旧 generation 可用。

### D12：跨 run memory 按当前 sealed analysis scope 授权

当前架构不建立跨 run 永久 Agent identity。每个 run 创建新的 parser、waterbody 与 summarizer sessions；M8 的“自身记忆”指当前 waterbody Agent 在本次 sealed analysis scope 内获准检索的同一 WaterAgents 实例历史，不是由长期 Agent ID 拥有的一组私有记录。

服务端从当前 `AnalysisContext`、`HydrologyGraph`、waterbody session 和固定 strategy 推导允许的 COMID、ParameterKey、window/season、nearby/topology neighborhood 与 source lifecycle 条件。相同 COMID 的历史可在 scope 匹配时检索；nearby/topology strategy 可读取获准的其他水体历史；无关水体、参数、时间、topology 外节点及调用方指定的任意 source run/item 不可见。

当前 session 的 live transcript 仍由既有 session buffer/store 直接读取，不通过 M8 index，因此当前过程不存在 indexing lag。M8 只负责跨 run 历史；新 run 可能在上一 run 的 indexing job ready 前暂时无法命中其最新历史，仍遵守 D11 的完整 row 可见性与最终一致语义。

首版当前产品仍沿用现有实例/数据 ownership，不在 M8 发明多租户、长期 Agent account 或客户端传入 owner scope。未来若增加用户/租户，tenant ownership 必须成为所有固定策略的服务端前置过滤，不得只依赖向量相似度或 prompt。

### D13：historical retrieval 是 optional evidence，失败不伪装成 empty

M8 继承 M5/M6 optional tool failure policy。active generation 内合法查询没有命中时，tool execution `completed` 且 collection status 为 `empty`；index/search unavailable、timeout、rate limit、协议错误、profile/dimension 不匹配或 source validation 失败时，tool execution `failed` 并保存安全 `unavailable/invalid` fact，不得返回 empty、旧缓存或另一 profile 的结果。

只要当前 target + required discharge gate 已由其他 direct/allowed-indirect evidence 满足，historical retrieval 失败不使已支持 cells 或整个 waterbody session 失败；Agent 可继续使用当前 evidence，最终链保留历史补充来源不可用事实。历史 evidence 本来不能满足 gate，因此 M8 failure 不得把它提升为 required acquisition，也不得为“补齐记忆”增加 fallback model、raw transcript scan 或自动重试循环。

index job 尚未 ready 的最新 source item 按 D11 不进入 active candidate set；这属于 active generation watermark 之后的 index lag，不将单条 pending job伪装成 search error。若 active generation 整体不存在、损坏或无法安全查询，则 producer unavailable，而不是全库 empty。

### D14：科学硬过滤先于全文与向量 ranking

所有固定策略先执行 server-owned authorization、memory scope、source lifecycle、schema/quality 与该策略的 COMID/topology/ParameterKey/time 条件，再对合格候选执行 lexical/vector recall 和 ranking。不得先做全库 semantic top-K，再把高相似但 scope 外或科学条件不匹配的记录交给 Agent 判断。

`rag_score` 只表达当前 query 与 projection 的检索相关性，不是 scientific confidence、evidence quality、applicability 或 attribution strength。高 vector/lexical score 不得覆盖原 confidence、提升 contextual evidence、放宽时间/空间关系或使不匹配参数进入结果。低 confidence 历史可以在其他条件合格时返回，但必须保留原值并按 policy 降低排序资格。

每种 strategy 必须冻结 hard filters、允许的 recall signals、score normalization/fusion、低 confidence/quality 处理、稳定 tie-break 和最大 candidate/return 数。相同输入、active generation 与 exact search mode 应产生字节稳定的命中身份和排序；若后续启用 approximate index，必须单独定义 recall/ordering 验收，不能沿用 exact 的确定性声明。

### D15：长期 memory 只投影 validated result 与 eligible evidence fragment

首版 projection source allowlist 只有：

- `inference_result v1`：后端已验证并提交的 waterbody final result，包括 quantified、directional 或 indeterminate cells，以及原 risk、confidence、availability、attribution 和 evidence refs；
- `evidence_fragment v1`：`rag_eligible=true` 且继续通过 D01 final eligibility 的 direct/derived/context fragment。`suspicious` 必须连同全部 quality flags、reason、coverage 与 provenance 投影，不得只保留数值或摘要。

以下内容即使可在 transcript/history 中正常读取，也不得成为首版长期 memory projection：

- parser/waterbody/run-summarizer message；
- planning、reasoning decision、prompt、model request/response、repair 与 raw output；
- `run_summary_result`、`topology_run_result` 和其他自然语言/聚合总结；
- tool call/result、inspection、error、empty/unavailable/invalid fact；
- `evidence_bundle`、dependency manifest 等引用组装；
- raw source-record array、provider envelope、secret 和无界 payload。

projection builder 按 content type/schema 使用显式 typed adapter，不允许通用 JSON/text fallback 自动索引未来未知类型。检索结果按原 run/session/result 和 source identity 去重；最终 result projection 优先承载结论，返回的 supporting fragments 保持有界并引用原 item，不把多个 fragment 拼成新的历史事实。

`query_understood` 本身保持 `rag_eligible=false`，不成为 historical evidence。为定位上述 eligible result，M8 可以从同一 run 已验证的 `AnalysisContext` 派生有界 case-query key；该 key 只是 index navigation metadata，不能单独返回给 Agent、满足 evidence gate 或被引用为科学事实。

### D16：新历史只追加，不按 COMID/参数覆盖旧 memory

projection 的业务 identity 来自原 source run/session/item 与 projection/profile generation，不存在按 COMID、ParameterKey、window 或 semantic topic 执行 latest-wins overwrite。新的合格分析创建新的 projection；同一 source item 的 job 重放只允许幂等 upsert 同一派生 identity，不得改写其他历史 source。

ranking 可以将时间接近度作为有界信号，但不得因较新记录自动删除、隐藏或宣告较旧记录失效。返回集按 source run/session/result 做去重与多样性约束，避免全部 slots 被同一次 run 的 result 和 fragments 占满；具体配额与 tie-break 由 policy 冻结。

后端不跨历史 series 平均数值、不按 RAG score 选择科学赢家，也不自动生成“冲突”结论或 consolidated memory。Agent 可以在同时取得多条历史时说明时间、来源和结论差异，但不得用旧历史覆盖当前观测。只有显式授权的 M9 history/index cleanup 可以删除对应派生 rows；profile generation 清理只删除可重建索引，不改写原 transcript。

### D17：semantic recall 比较 query understanding，再按精确引用读取原结果

M8 首版 semantic query 不以 answer/result body embedding 作为主要入口。每个 eligible waterbody result 关联一个由当时 validated `AnalysisContext` 派生的 canonical case-query projection；当前 session 使用同版本 canonical builder 从本次 `AnalysisContext` 生成 query embedding，在 D14 的 hard-filter candidate set 中执行 query-to-query similarity。

case-query projection 至少承载 requested/supporting ParameterKeys、mode、time/season/resolution、normalized intent、query schema/projection version；waterbody-specific COMID 与 topology relation 来自 result/session/graph projection，不把完整原始用户 query、parser message 或 rationale 自由文本复制进 index。

semantic hit 返回 `HistoricalCaseRef`，至少包含：

```text
run_id
session_id
target_comid
analysis_context_item_id
result_item_id
```

`run_id` 不能单独定位答案，因为一个 M7 run 可以包含多个 waterbody sessions/results。hydration 必须重新验证 source run/session completed、result item 归属/schema、当前 memory scope 和 active projection identity，再从原 `inference_result` 读取与本次参数/窗口相关的安全投影，并按 policy 附带少量 eligible fragment refs。

case-query projection 是检索钥匙，不是 evidence；真正进入当前 evidence/transcript 的仍是原 validated result/fragment 的 historical wrapper。首版不增加 result-body vector 二次召回；只有离线评测证明 query-to-query 存在明确、无法由 structured/lexical strategies 覆盖的漏召回时，才另行设计并版本化该能力。

### D18：命中后只向 Agent 投影当前参数/窗口相关内容

hydrator 读取并验证完整原 `InferenceResult`，但 `HistoricalCaseRecord` 只包含本次 authorized ParameterKeys、匹配/相关 windows、必要的 overall risk/availability、原 confidence、原分析时间、source schema 和稳定 refs。不得把整个历史 run、其他 waterbody sessions、无关参数、完整 transcript 或完整 evidence chain 装入当前 prompt。

每个 case 只附 policy 允许数量、与返回 cells 直接关联且仍通过 final eligibility 的 supporting fragment projections/refs。后端按原 result/evidence identity 去重，不根据文本重新拼装答案、生成新摘要或复制原全文。完整历史继续通过既有 run/session/item history routes 对用户可读，但 waterbody Agent 不获得任意展开 scope 外历史的能力。

Agent 若需要另一个当前 scope 内的参数/window/strategy，必须在剩余 tool budget 内发起新的 typed search；不能用一次 case hit 取得整次历史 run。candidate、case、cell、fragment 与 prompt token 上限由 Policy Catalog 固定，超限使用稳定裁剪/拒绝规则并保留审计，不由模型自行丢内容。

### D19：搜索请求是 typed intent，不接收自由 query 或 source identity

M8 将当前 `TranscriptEvidenceRequest.query` 替换为后端可验证的 `strategy`、`parameter_keys`、`window_ids` 和 `limit`。Agent 不能提交 raw SQL、embedding text、source run/session/item、COMID、任意日期、半径、hop 数、profile 或 owner；COMID、时间、season、nearby/topology candidate set、canonical query 和 embedding profile 全部由后端从 sealed `AnalysisContext`、当前 waterbody session 与已保存 `HydrologyGraph` 补全和验证。

同一 tool call 只执行一种 strategy。参数/window 为空、重复、scope 外、数量超限或与 strategy 不兼容时，必须在 scope preparation 阶段拒绝，产生 `0 search / 0 hydrate / 0 source record`；合法请求进入 executor 后的 empty/unavailable/invalid 则继续按 M5 审计。producer 不解析 Agent 自由文本来猜 strategy。

### D20：首版不新增公共 RAG 管理或检索 API

M8 不新增 direct search、job、generation、reindex 或 embedding HTTP route。创建 run 继续只使用既有 `memory_mode`；历史搜索通过 waterbody Agent 的 M5 tool loop 发生，并沿用 session transcript、session SSE、item content 和 history routes 对外可读。index generation/job/lag 是内部运维状态，首版通过结构化日志和指标观察，不把供应商错误、canonical text 或 vector 暴露给客户端。

M9 若需要 settings/admin/cleanup surface，必须另行定义授权、幂等、冲突和异步状态契约；M8 文档不能被当作允许客户端直接检索数据库或触发任意 reindex 的授权。

### D21：删除与重建保持 source-first ownership

删除一个原 run 时，必须在同一受控 cleanup workflow 中处理指向该 run/session/item 的 indexing jobs、case projections、fragment projections、consumption links 和 derived cross-session memory；任何残留 row 都不得继续被 search/hydrator 返回。若该 source 已被仍需保留的 consumer run transcript引用，选择性删除必须稳定 conflict，或在用户明确授权后级联删除整个 dependent consumer run；不得改写 immutable consumer transcript、留下悬空 ref 或静默改成 tombstone。仅删除 index generation 或执行 reindex 不删除原 transcript，不改变历史 API 的 source 事实。

generation cleanup 只能删除 inactive 且没有 reader lease 的 generation；active generation 只能在另一个完整 generation 原子接管后退休。rebuild 从原 transcript allowlist 重新投影，禁止从旧 projection、prompt、tool result 或 embedding 反向恢复事实。M9 的 history cleanup 与 RAG cleanup 必须共享这一 ownership 顺序，API keys 永远不属于该删除图。

### D22：M8 只改变 historical producer readiness，不改主推理 authority

M8 runtime 在 production composition 中先建立 search producer，再将它注入 M5；producer ready 时，`memory_augmented` waterbody session 才看见 `search_transcript_evidence`。active generation 不存在或配置无效时，startup 不伪造一个可用 producer；是否 fail startup 取决于基础 schema/profile 是否损坏：缺少必需 migration/extension/profile 属于配置错误并 fail closed，合法但尚在首代 backfill 则应用可启动、producer 报 unavailable，现有 stateless/current-evidence 主链仍可运行。

M8 不改变 M6 gate、validator、result assembler、M7 scheduler、target result authority 或 run summary authority。历史内容始终是 optional contextual evidence；系统即使完全没有历史 case，也必须能按 M1–M7 既有规则完成当前分析。

## 3. 文档中已要求支持的检索策略

M8 至少覆盖：

1. COMID + parameter + time window；
2. nearby COMID + parameter；
3. parameter + season；
4. semantic query；
5. topology neighborhood + parameter。

这些名称不是自由数据库查询权限。每种策略的输入字段、scope 推导、候选集、空间/拓扑边界、时间重叠、score 组成、tie-break、最大返回数和空结果语义必须在 M8 Policy Catalog 中冻结。

## 4. 与 M9 的接力边界

M9 必须能够按 M8 定型的 ownership 规则清理 RAG index 和 derived cross-session memory，同时不删除 API keys、不改写原 transcript 事实，也不把清理 index 表述为删除原历史。M8 需要为该动作提供可重复执行的删除边界、引用处理和重建语义；是否公开独立 admin route 仍由 M9 定型。

## 5. 设计交付物

- `m8_memory_rag_design.md`：总体产品与架构决策；
- `m8_memory_rag_implementation_design.md`：模块、类型、执行顺序、持久化、组合与测试；
- `m8_rag_policy_catalog.md`：eligibility、五种检索策略、ranking、优先级与失败 policy；
- `m8_memory_rag_api.md`：既有 routes/typed contents/history/SSE 投影及示例；
- `m8_implementation_todo.md`：供 coding agent 执行的 Milestone 0–10 施工顺序、测试门禁、提交边界、完成证据与契约承接矩阵。

[M8 Implementation TODO](m8_implementation_todo.md) 已建立，当前所有实施项保持未勾选；文档存在不表示代码、migration、provider、worker 或验收已经完成。
