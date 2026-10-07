# M8 Memory/RAG from Transcript 实施设计

更新日期：2026-10-07

本文把 [M8 总体设计](m8_memory_rag_design.md) 与 [RAG Policy Catalog](m8_rag_policy_catalog.md) 映射到当前后端。它是 implementation design，不是完成声明或 TODO 清单。

## 1. 当前接入点与缺口

当前 production 已具备：

- M3 PostgreSQL transcript/history、`rag_eligible` candidate flag、run/session/item identity 与全文读取；
- M4 保存后的 `AnalysisContext`、`HydrologyGraph`、ParameterKey/window/timezone/topology hard limits；
- M5 `TranscriptEvidencePort`、`search_transcript_evidence` producer slot、scope preflight、tool budget、source/fragment/bundle/transcript materialization；
- M6 bounded planning loop、target/discharge gate、result validator/assembler；
- M7 production DAG、direct-upstream store、waterbody result 与 run summary authority；
- generic `RAGIndexPort.upsert/search` 占位和 `HistoricalTranscriptRecord`/`HistoricalInferencePayload` 基础类型。

当前 production 没有：typed historical query、source final-eligibility projector、embedding adapter、pgvector schema、generation/job lifecycle、search/hydration service，也没有向 M5 注入 transcript producer。因此 M8 必须完成这条既有接缝，不能新增 HTTP/search 旁路。

## 2. 模块与依赖方向

建议在现有分层中增加以下职责，不创建按 milestone 命名的业务孤岛：

```text
app/application/transcript_memory/
  models.py                    # profile/generation/job/case/query/search typed models
  eligibility.py               # source final-eligibility and closure validation
  canonical_query.py           # CanonicalCaseQueryV1 + stable text/digest
  projection.py                # result/fragment bounded projections
  discovery.py                 # completed+persistence-confirmed source discovery
  index_worker.py              # lease/batch/retry/backfill/activation
  search_policy.py             # five strategies, score, stable limits
  search_service.py            # candidate search -> hydrate -> Historical records
  consumption_links.py         # cross-run source/consumer ownership registration

app/ports/
  embedding.py                 # EmbeddingPort
  rag_index.py                 # typed projection/generation/search port
  rag_jobs.py                  # durable discovery/lease/job port
  historical_sources.py       # authoritative result/fragment hydration reader

app/infrastructure/adapters/
  supplier_embeddings.py       # first remote embedding adapter

app/infrastructure/persistence/
  rag_store.py                 # generation/job/projection/search implementation

app/infrastructure/runtime/
  memory.py                    # profile + ports + worker + TranscriptEvidencePort composition
```

依赖只允许：M5 `TranscriptEvidencePort` -> application search service -> ports；worker -> eligibility/projection/embedding/job/index ports。domain/application 不 import SQLAlchemy、pgvector、HTTP 或 FastAPI。M6/M7 不反向 import memory worker；HTTP 只读取既有 transcript projections。

## 3. 核心类型

### 3.1 Embedding profile

```text
EmbeddingProfile
  profile_id
  provider
  model
  output_dimensions
  distance_metric = cosine
  semantic_min_similarity
  canonical_query_schema_version = 1
  canonical_template_version = 1
  projection_schema_version = 1
  profile_digest
```

`profile_id` 是上述字段 canonical serialization 的稳定 digest，不是显示名称。首版只允许 cosine。运行时唯一 embedding catalog entry 必须与 building/active generation 的 identity 精确匹配；query/projection 不得只按 model string 判断兼容。

### 3.2 Generation 与 job

```text
RAGIndexGeneration
  generation_id
  profile
  status: building | active | retired | failed
  source_cutoff_created_at
  source_cutoff_item_id
  created_at/activated_at?/retired_at?/failed_at?

RAGIndexJob
  job_id
  generation_id
  source_run_id/source_session_id/source_result_item_id
  projection_schema_version
  status: pending | leased | retry_wait | completed | excluded | failed
  attempts
  next_attempt_at
  lease_owner?/lease_until?
  last_safe_error_code?
  created_at/updated_at/completed_at?
```

job unique identity 是 `(generation_id, source_result_item_id, projection_schema_version)`。`excluded` 表示 candidate 在最终 eligibility 校验后按稳定 policy不合格，不写 projection，也不阻塞 generation；`failed` 表示本应建立但技术/协议尝试无法完成。attempt 只记录安全 code、时间和计数，不保存 canonical text、credential、vector 或供应商 raw body。

### 3.3 Projection 与 source ref

```text
HistoricalCaseRef
  generation_id/case_id
  run_id/session_id/target_comid
  analysis_context_item_id
  result_item_id

RAGCaseProjectionV1
  case_ref
  mode
  parameter_keys[]
  windows[]
  source_completed_at
  canonical_query
  canonical_text_digest
  embedding
  safe_result_index_fields
  projection_digest

RAGFragmentProjectionV1
  case_id
  source_fragment_item_id
  parameter_key/window_id
  quality/applicability/valid_time/provenance identity
  bounded safe projection
  projection_digest
```

case projection 不复制完整 `InferenceResult`；fragment projection 不复制 raw source record。搜索 row 只能产生 `HistoricalCaseRef`，最终 record 必须从 authoritative transcript hydration 后构建。

### 3.4 Typed search/result

```text
RAGSearchStrategy
  exact_comid_parameter_window
  nearby_comid_parameter
  parameter_season
  semantic_case_query
  topology_neighborhood_parameter

HistoricalSearchIntentV1
  strategy
  parameter_keys[]
  window_ids[]
  limit

RAGScoreComponentsV1
  strategy + applicable structural/lexical/vector components

HistoricalCaseRecordV1
  case_ref
  matched_by
  rag_score/score_components
  source schema/time/quality/confidence
  authorized result cells
  bounded supporting fragments
```

当前 `TranscriptEvidenceRequest.query` 必须由 typed intent 替换；当前 `HistoricalTranscriptRecord.matched_by: str` 必须收紧为 enum + typed components，并增加完整 `HistoricalCaseRef` 与有界 result projection。普通 `ItemReference` 继续只用于同 run；跨 run 使用专门的 historical source ref。

## 4. Port contracts

### 4.1 `EmbeddingPort`

```text
embed(profile, texts, deadline, cancel_token?) -> EmbeddingBatch
```

- 输入必须已 canonicalize、脱敏并通过 byte/token/batch fit；
- 输出数量、顺序、profile、dimension、有限数值全部验证；
- index worker 的调用不绑定 run cancellation；live semantic search 绑定当前 session cancellation/deadline；
- adapter 不重试 policy、不换模型、不写 transcript；重试由 worker或 M5 executor ownership 执行；
- tests 使用 deterministic fake，不能发真实付费请求。

### 4.2 Typed `RAGIndexPort`

现有 generic port 演进为 typed methods：

```text
write_ready_projection(job, case, fragments)
search_candidates(authorized_query) -> CandidateRows
read_active_generation() -> generation | None
create_building_generation(profile, cutoff) -> generation
activate_generation(generation_id)
retire/delete_generation(generation_id)
```

`write_ready_projection` 与 job completed 原子提交。`search_candidates` 只读取 active generation/ready cases并在 SQL 前获得后端已推导的 authorized COMID/ParameterKey/window set。empty tuple 只表示正常无命中；数据库/profile/schema 错误使用 typed exception。

### 4.3 `HistoricalSourceReaderPort`

按 `run_id + session_id + item_id` 一次读取指定 registry item，并验证 run/session lifecycle、归属、content/schema 与 digest。它必须复用 M3/M7 已有 single-item read 路径，只解析/脱敏所选 result/fragment，不加载整个 run transcript。

### 4.4 `RAGJobStorePort`

提供 idempotent discovery enqueue、`FOR UPDATE SKIP LOCKED` claim、lease recovery、retry/failed transition 和 generation counts。应用层决定 retryable/non-retryable；persistence 层不解析 provider error text。

## 5. PostgreSQL schema

M8 migration 在受控部署阶段执行 `CREATE EXTENSION IF NOT EXISTS vector`；正式 application startup 只验证 extension/version、Alembic head、table/index/constraint，不临时安装扩展。Compose 使用显式带受控 pgvector extension 的 PostgreSQL image/version，不能继续假设 vanilla image 已有 extension。

### 5.1 `rag_index_generations`

关键列：generation UUID、profile fields/digest、status、source cutoff、timestamps、安全失败 code。约束：全库最多一个 `active`、最多一个 `building`、status/timestamp 配对；同一 profile允许重新建立新 generation，不能用 profile digest充当 generation identity。active switch 在一个 transaction 中将旧 active -> retired、新 building -> active。

### 5.2 `rag_index_jobs`

FK 指向 generation、source run/session/result item；source result 删除时 job cascade 删除。唯一约束覆盖 job identity；索引覆盖 `(generation_id,status,next_attempt_at,lease_until,job_id)`。`leased` 但 lease 到期可重新 claim，attempt 不因进程重启归零。

### 5.3 `rag_case_projections`

关键列：generation/case UUID、source identities、target COMID、mode、ParameterKey array、canonical text/digest、`tsvector`、`vector`、result safe index fields JSONB、projection digest、ready timestamp。FK 指向 source result item并 cascade 删除；唯一 `(generation_id,source_result_item_id)`。

向量列首版使用 unconstrained `vector`，因为不同 inactive generation 可以有不同 dimensions；每次写入/查询都按 generation profile 验证 dimension。首版 exact scan，不建 HNSW/IVFFlat。ParameterKey 使用 GIN，COMID/mode/generation 使用 B-tree，canonical text 使用 GIN `tsvector`。

### 5.4 `rag_case_windows`

一行对应一个 case window：generation/case/window identity、`tstzrange`、local months、season bucket/year、duration、resolution、ParameterKeys。GiST 支持 interval overlap，B-tree/GIN 支持 generation/case/season/parameter filters。SQL 先执行 hard filters，再 exact vector/full-text/order/limit。

### 5.5 `rag_fragment_projections`

FK 指向 case projection与原 fragment item；保存 parameter/window、quality/applicability/time/provenance identity 和有界 JSONB projection。唯一 `(generation_id,case_id,source_fragment_item_id)`；root case 或 source fragment 删除时 cascade。

### 5.6 `rag_consumption_links`

当当前 session 的 historical source record 成功持久化时，登记 source case/result item与 consumer run/session/item 的关系。它不进入搜索排名，只用于删除冲突、引用审计和 M9 cleanup。

consumer 删除时 link cascade；source 删除默认 restrict。选择性删除一个已被保留 run 消费的 source 时，M9 必须返回 conflict，或在用户明确授权下删除整个 dependent consumer run；不得改写 immutable transcript、只删 source 留下悬空引用或静默改成 tombstone。全量 history cleanup 按 consumer -> source -> index/link 顺序执行。

## 6. Discovery、backfill 与 active generation

### 6.1 Candidate discovery

worker 启动后及收到 run-terminal post-commit wake signal 时执行同一个 idempotent discovery query：只扫描 run/session completed、persistence-confirmed、`rag_eligible=true`、allowlisted result items。wake signal 只减少延迟；丢失信号不丢数据，周期性 bounded reconciliation 仍会发现遗漏。

discovery 只创建 job，不读取全文、不调用 embedding。最终 eligibility 在 job 执行时重新验证，因此 candidate flag 或查询结果不能跳过 D01。

### 6.2 First generation

1. 从唯一 embedding catalog entry构建 profile；
2. 建立 building generation 与稳定 `(created_at,item_id)` cutoff；
3. 为 cutoff 内 eligible roots 创建 jobs；
4. worker 完成 projection；稳定 final-ineligible candidate进入 `excluded`，non-retryable technical/protocol failed job必须显式处理，不能把不完整 generation激活；
5. 验证 source counts、projection digests、dimension/profile、FK 和 sample ranking fixtures；
6. 原子激活；
7. cutoff 后 sources继续进入当前 active generation jobs。

首代 building 期间没有 active generation，search 返回 typed unavailable；原 history/current analysis 不受影响。

### 6.3 Profile change

旧 active generation继续服务；新 profile 创建 building generation并从 source transcript 重建，不从旧 projection复制。新 generation 完整验证后原子切换。切换后新 query只读新 active；旧 generation变 retired，等待 reader lease 归零后按 retention policy 删除。

新 source 在 build cutoff 后出现时，先进入当前 active old generation以保持可见，并在新 generation 激活前/后由 delta reconciliation补入新 generation。activation gate 必须保证 cutoff 内无 pending/retry/failed缺口；cutoff 后 lag 按 D11 最终一致语义处理。

## 7. Worker lifecycle

- 单进程默认启动一个 bounded worker；schema支持 lease 恢复，但不宣称多进程整体 runtime/cancel 已获支持；
- claim batch 与 embedding batch 分开限制；worker最大并发、deadline、attempt、backoff由 validated settings 一次注入；
- retryable attempt 最多 5 次，指数退避有上限；非 retryable 立即 failed；
- 每批 embedding 返回后逐 row 验证，整批任一缺失/错序/非法使 attempt失败，不提交部分 projection；
- source 在 job 执行期间被删除则job随FK清理；仍存在但按final policy失去资格则转 `excluded/source_no_longer_eligible`，不写projection、不恢复旧内容；
- shutdown 停止 claim，等待在途 DB transaction有界收敛；超时释放/到期 lease由下次启动恢复；
- worker crash、供应商失败、数据库暂时不可用不能修改 run/session终态。

## 8. Search execution

```text
Agent tool call
  -> M5 schema parse
  -> prepare TranscriptEvidenceRequest from sealed EvidenceSessionScope
  -> authorize strategy/parameters/windows/budget
  -> build authorized COMID/time/topology set
  -> read active generation/profile
  -> build current CanonicalCaseQueryV1
  -> semantic strategy only: call EmbeddingPort
  -> SQL hard filters + exact ranking
  -> stable dedupe/diversity/byte fit
  -> hydrate each HistoricalCaseRef from original items
  -> revalidate identity/eligibility/scope/digests
  -> HistoricalTranscriptRecord[]
  -> existing M5 source/fragment/bundle/transcript materialization
```

同一 operation 只读一个 captured active generation；activation 与 search 并发时不能混读两代。hydration 任何 authority/corruption failure使整个 collection invalid，不返回可信与不可信混合 partial set。正常无命中是 completed/empty。

结构化策略不需要供应商在线；`semantic_case_query` 需要一次当前 query embedding。可在当前 session 内按 `(profile_id,canonical_text_digest)` 缓存成功 query vector，生命周期不超过 session，cancel/failed结果不缓存，也不写 transcript/database。

## 9. M5/M6/M7 组合

production startup顺序调整为：

1. 验证 PostgreSQL、Alembic head、`vector` extension与 model catalog；
2. 创建 transcript store/reader、coordinator与 M4 workflow facade；
3. 创建 M8 profile、job/index/source ports、search service与 worker；
4. 将 search service 作为真实 `TranscriptEvidencePort` 注入 M5 runtime；
5. 创建 M6 runtime、M7 upstream store/scheduler/summarizer；
6. 一次绑定完整 M7 execution stage；
7. 启动 bounded index worker后才接收流量。

缺 migration/extension、重复 embedding profile、profile字段非法或 active generation损坏属于 startup configuration error并 fail closed。合法首代 backfill 尚未完成不是 corruption：应用可启动，producer存在但 search报告 `index_building` unavailable。

`stateless` scope仍不把 tool放入 roster；`memory_augmented` 且 M8 producer正常装配时才可调用。M8 不改变 M6 gate/validator/assembler、M7 scheduler/upstream store/summary，也不让 summarizer调用历史工具。

## 10. Model catalog 与 settings

`ModelRole` 增加 `embedding`。model catalog中的唯一embedding entry表示“期望构建并最终激活的profile”；当前真正用于search的active profile来自已激活generation。profile切换期间允许一个active旧generation加一个building新generation，但任何时刻仍只有一个可服务active profile。`ModelCapability` 对 embedding role要求：

- 不与 `query_parser/waterbody` 混用同一 capability entry；
- 明确 `embedding_dimensions`、`embedding_max_input_tokens`、`distance_metric=cosine` 与经版本化离线fixture确定、无默认值的 `semantic_min_similarity`；
- provider/model 必须有已注册 adapter；
- 整个 catalog精确一个期望 embedding entry。

新增 settings 只覆盖基础设施预算：worker batch/concurrency、embedding timeout、job lease/backoff/reconciliation interval、retired generation retention。五种 strategy、25 km、candidate/return/diversity/preview limits属于版本化 policy，不开放为前端/run request自由设置。未知/越界设置 startup失败，不静默回默认。

首版 supplier adapter复用现有受保护 API key/base URL infrastructure，但 embedding model由期望embedding catalog entry选择，不复用 waterbody/query-parser model。search对旧active generation使用其持久化profile identity；M9在新generation切换完成前必须保留旧profile所需adapter/credential，不能先撤销旧配置再声称旧active仍可用。M9可管理下一代profile配置；修改只触发新generation，不改既有run事实。

## 11. Transcript 与审计

不新增 hidden reasoning content。每次真实搜索沿用 M5 typed items：

- `evidence_tool_call v1`：typed strategy、requested ParameterKeys/window IDs/limit，不含 canonical text/vector；
- `evidence_source_records v1`：bounded `HistoricalTranscriptRecord`；
- `evidence_fragment v1`：本次 materialized contextual fragments；
- `evidence_tool_result v1`：completed/failed、empty/unavailable/invalid 与 safe code；
- `evidence_bundle v1`：允许进入 reasoning 的同 run item refs。

`rag_consumption_links` 在 source-record item持久化 transaction确认后登记；link失败属于该历史记录 persistence failure，不能保留正文却缺 ownership registry。index jobs/generation/embedding response不写公共 transcript。

结构化日志/metrics至少覆盖：generation状态/年龄、job pending/lease/retry/failed/lag、embedding latency/error、search strategy/empty/failure/latency/candidate-return counts、hydration invalid、active profile digest。不得以 run/session/item、COMID、query text 或 vector作为无界 metric label；日志只用安全 hash/correlation ID。

## 12. 删除、重建与 M9 handoff

M8 application service提供内部 typed operations，不在 M8公开 route：

```text
inspect_index_health()
build_generation(profile)
retry_failed_jobs(generation, safe_filter)
retire_generation(generation)
delete_retired_generation(generation)
inspect_cleanup_dependencies(run_ids)
```

M9 admin/cleanup调用这些 service前必须增加授权、确认、幂等与异步状态。index-only cleanup只删除 retired/failed generation 的派生 rows；active 必须先由另一完整 generation 原子接管并转为 retired，且 reader drain 与 retention 均满足后才能删除。它不删除 transcript。profile change必须把“保存期望配置、构建、验证、激活、最后再允许清理旧credential”作为一个可观察workflow。history cleanup先检查 `rag_consumption_links`，按 source-first ownership规则拒绝或显式级联，不改写 retained transcript。API keys完全不在清理图中。

实现状态（2026-10-08）：上述 operation 已由 production `M8Runtime.operations` 组合为单一 `RAGMaintenanceOperations`，并与 search 共享同一个 reader-lease owner。generation replacement 通过既有完整性校验与原子 activation 完成；history cleanup 在单事务内重新计算传递 consumption graph，默认稳定 conflict，只有显式参数才按 consumer-first 整 run 级联。该内部 service 另提供 cleanup 执行与 profile handoff readiness 供后续受控调用，但未新增 HTTP route、第三 SSE、frontend/admin surface，也不持有或返回 credential/API key。

## 13. 测试矩阵

### 13.1 Unit/property

- eligibility allowlist、run/session/persistence/reference closure；
- canonical query字节稳定、raw query/secret不进入、token fit；
- 五种 strategy hard filters、score边界、stable tie-break、threshold；
- limit、每 run多样性、fragment/cell/byte裁剪；
- embedding数量/顺序/dimension/finite/profile验证；
- retry classification、lease/backoff、generation state machine；
- source deletion/consumer link conflict与无悬空引用。

### 13.2 PostgreSQL integration

- migration在带 pgvector的全新库和已有四表库升级；extension/version缺失失败；
- GIN/GiST/B-tree过滤与 exact cosine排序；
- active generation唯一、原子切换、并发 search只读一代；
- job claim/lease expiry/restart/idempotent upsert/transaction rollback；
- source item删除cascade projection/job，consumed source默认restrict；
- exact empty与 DB/profile/source invalid错误区分。

### 13.3 Application/contract

- `stateless` roster无历史工具；`memory_augmented`有真实 producer；
- 非法 COMID/parameter/window/strategy保持 `0 search / 0 hydrate / 0 source record`；
- Agent不调用工具仍合法；最多两次、每次单 strategy；
- query-to-query命中正确 `run/session/result item`，多水体 run不串答案；
- hydration只返回当前参数/window与最多两个相关 fragments；
- historical empty/unavailable/invalid不解锁 gate、不覆盖 current evidence、不使已支持 cells失败；
- search/tool/source/fragment/bundle/result/history/SSE可闭环读取；
- run完成不等待 worker，restart后最终可检索。

### 13.4 End-to-end/smoke

使用 deterministic fake embedding与两个以上历史 runs验证：建立首代、active、第三个 memory-augmented run显式搜索、命中正确原 result、transcript审计、stateless隔离、worker失败后当前主链仍完成。另覆盖 profile change/build/switch、source cleanup conflict和M7多水体 source identity。

不得以 fake smoke证明真实供应商、三平台、生产规模或 approximate recall。真实 embedding smoke需要明确联网/计费授权且只发送脱敏 canonical fixtures；首版 exact容量门槛必须用可复现 synthetic/fixture corpus记录，不提前声明HNSW验收通过。

## 14. 实施完成证据

分阶段施工、测试门禁、提交边界、完成记录和 contract matrix 见 [M8 Implementation TODO](m8_implementation_todo.md)。实现 MR 必须给出：适用 contract IDs、migration/extension/Compose证据、typed port与composition路径、policy fixture digest、focused + PostgreSQL + full regression命令/结果、fake E2E、OpenAPI无新增 route审计、git diff/status与未运行项。文档或 exit 0本身不等于真实 provider、三平台或生产容量验收。
