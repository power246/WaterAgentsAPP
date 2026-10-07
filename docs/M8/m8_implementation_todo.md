# M8 Memory/RAG from Transcript Implementation TODO

更新日期：2026-10-07

> 当前状态：M8 产品/架构、实施、policy 与 API 设计已经冻结，代码尚未开始。本文件全部复选框均为待办；任何现有文件名、placeholder port 或历史测试都不能作为 M8 已完成的证据。M8 是 M1–M9 同一最终系统中的内部实施切片，不形成独立发布版本。

本清单执行以下已确认文档：

- [M8 设计决策 D01–D22](m8_memory_rag_design.md)
- [M8 实施设计](m8_memory_rag_implementation_design.md)
- [M8 RAG Policy Catalog](m8_rag_policy_catalog.md)
- [M8 API / 读取契约](m8_memory_rag_api.md)
- [跨 Milestone M8 契约](../deferred_milestone_contracts.md#10-m8memoryrag-from-transcript)
- [M5 Evidence Tools 实施设计](../M5/m5_evidence_tools_implementation_design.md)
- [M6 Single-Agent Reasoning 实施设计](../M6/m6_single_agent_reasoning_implementation_design.md)
- [M7 Implementation TODO 的 M8 接力边界](../M7/m7_implementation_todo.md)
- [Docker PostgreSQL 快速指南](../docker_postgresql_quickstart.md)

若本清单与上述设计冲突，coding agent 不得自行选择实现成本更低的一项，也不得通过兼容层同时保留两套行为。应先暂停当前阶段，在同一变更中把当前唯一 V1 设计、契约与 TODO 修正一致，再继续实现。内部函数拆分、文件粒度、SQLAlchemy 映射方式和 fixture 组织可由实现者决定，但不能改变 source-first ownership、final eligibility、active generation、五种固定策略、query-to-query semantic recall、server-owned scope、历史证据权限、durable eventual indexing、M5/M6/M7 组合或 M9 接力语义。

## 阶段与设计导航

| 实施阶段 | 主要设计依据 | 阶段产物 |
|---|---|---|
| [Milestone 0：基线、真实接缝与红线](#milestone-0基线真实接缝与红线) | [实施设计 §1–2](m8_memory_rag_implementation_design.md#1-当前接入点与缺口) | 可复现基线、production 影响表、失败测试与边界 guards |
| [Milestone 1：当前唯一 V1 类型、ports 与配置契约](#milestone-1当前唯一-v1-类型ports-与配置契约) | [实施设计 §3–4/§10](m8_memory_rag_implementation_design.md#3-核心类型)；[API §3–6](m8_memory_rag_api.md#3-search_transcript_evidence-call) | typed request/result/ref、embedding profile、port 边界、catalog 角色 |
| [Milestone 2：pgvector schema、migration 与 repositories](#milestone-2pgvector-schemamigration-与-repositories) | [实施设计 §5](m8_memory_rag_implementation_design.md#5-postgresql-schema) | 六类 RAG 表、约束/索引、Compose、repository primitives |
| [Milestone 3：final eligibility、canonical query 与 source projection](#milestone-3final-eligibilitycanonical-query-与-source-projection) | [设计 D01/D15–D18](m8_memory_rag_design.md#d01candidate-标记不是最终索引资格)；[Policy §2–3](m8_rag_policy_catalog.md#2-source-eligibility) | 纯函数资格判定、稳定 canonical bytes、typed source hydration |
| [Milestone 4：EmbeddingPort adapter 与 generation 切换](#milestone-4embeddingport-adapter-与-generation-切换) | [设计 D07–D09](m8_memory_rag_design.md#d07同一-postgresql-承载结构化全文与向量检索)；[实施设计 §3.1/§6](m8_memory_rag_implementation_design.md#31-embedding-profile) | supplier adapter、deterministic fake、profile validation、原子 active switch |
| [Milestone 5：durable jobs、worker 与 backfill](#milestone-5durable-jobsworker-与-backfill) | [设计 D11](m8_memory_rag_design.md#d11indexing-最终一致不阻塞原-run-终态)；[实施设计 §6–7](m8_memory_rag_implementation_design.md#6-discoverybackfill-与-active-generation) | durable discovery/lease/retry、首代 backfill、重启恢复、非阻塞 run |
| [Milestone 6：五种 search policy、ranking 与 bounded hydration](#milestone-6五种-search-policyranking-与-bounded-hydration) | [Policy §4–9](m8_rag_policy_catalog.md#4-typed-search-request)；[实施设计 §8](m8_memory_rag_implementation_design.md#8-search-execution) | hard filters、exact retrieval、stable ranking、source revalidation、limits |
| [Milestone 7：M5/M6 historical producer 与当前证据优先级](#milestone-7m5m6-historical-producer-与当前证据优先级) | [设计 D03–D06/D10/D22](m8_memory_rag_design.md#d03memory-mode-只控制跨-run-历史记忆)；[实施设计 §9](m8_memory_rag_implementation_design.md#9-m5m6m7-组合) | tool producer、0/0/0 preflight、contextual-only materialization、consumption links |
| [Milestone 8：production composition、生命周期与可观测性](#milestone-8production-composition生命周期与可观测性) | [实施设计 §7/§10–11](m8_memory_rag_implementation_design.md#7-worker-lifecycle) | M8 runtime、worker startup/shutdown、fail-closed 配置、隐私安全日志指标 |
| [Milestone 9：history/SSE、cleanup 与 M9 handoff](#milestone-9historyssecleanup-与-m9-handoff) | [设计 D20–D21](m8_memory_rag_design.md#d20首版不新增公共-rag-管理或检索-api)；[API §7–10](m8_memory_rag_api.md#7-transcripthistory-与-sse) | typed round-trip、两层 SSE、删除冲突/级联、内部重建操作契约 |
| [Milestone 10：端到端验收、契约审计与交付证据](#milestone-10端到端验收契约审计与交付证据) | [实施设计 §13–14](m8_memory_rag_implementation_design.md#13-测试矩阵) | fake/PG E2E、全回归、smoke、契约矩阵、真实未验证边界 |

## Coding agent 持续执行原则（非完成项）

以下条目是实施和后续维护都必须遵守的规范，不作为完成状态复选框：

- 只以当前 `backend/`、`docs/M1`–`docs/M8` 和项目级当前契约为实现输入。`backend_bk/`、`frontend_bk/`、`docs/backend_refactor_contract.md` 是归档，不得读取、导入、复制、适配、枚举、运行或把其中行为当测试 oracle。
- 开始每个 Milestone 前记录 branch、HEAD、`origin/main`、merge-base、`git status`、已有用户改动和 permission-limited 路径。不得清理、覆盖、回退或顺手提交不属于本阶段的文件。
- 一个 Milestone 只交付表中一个可观察目标。先写或更新会失败的测试，再做最小 production 实现，再跑 focused gate；阶段出口未满足时不得进入下一阶段，也不得勾选项目。
- 每个 Milestone 原则上形成一个可独立审查的提交。提交前运行适用 focused tests、architecture tests、`compileall` 和 `git diff --check`；未经用户要求不 push、不创建 PR、不改写历史。
- 直接原子更新当前唯一 V1 的 types、ports、model catalog、tool schema、typed contents、runtime composition、fixtures 和全部消费者。不得新增 V2/read union、M8 feature flag、双写、临时兼容入口或第二套 historical tool。
- 原 transcript 是权威事实，RAG 表只是可重建检索视图。不得从旧 projection、embedding、tool result、prompt 或 summary 反向恢复/改写事实，也不得让索引记录先于 source 成为可见证据。
- `rag_eligible=true` 只是候选标记。只有 completed、persistence-confirmed、通过 typed final eligibility 的 `inference_result v1` 及其直接关联 eligible `evidence_fragment v1` 才能进入 active generation；未知 content type 必须 fail closed。
- `query_understood` 只派生 canonical navigation key，不是 evidence。semantic recall 比较本次与历史的 query-understanding projection；命中后按完整 `HistoricalCaseRef` 回读原 result，绝不能只凭 `run_id` 或 result body 向量猜答案。
- 首版只允许五种固定 strategy。Agent 只能给 strategy、当前 scope 内 parameter keys/window IDs 和 limit；COMID、时间、season、25 km、topology candidate set、profile 与最终 limit 都由后端填写/验证。
- scope preparation 必须在任何搜索、embedding、hydration、source record 或 transcript write 之前完成。非法请求是严格 `0 search / 0 embedding / 0 hydrate / 0 source record`；合法请求进入 runtime 后的 empty/unavailable/invalid 则按 M5 既有审计链保存。
- 历史证据始终是 `historical_inference/contextual_only`。它不能满足 target parameter/discharge/current-observation gate，不能覆盖当前观测、提高原 confidence/quality、替代 M6 validator，或改变 M7 target/run-summary authority。
- indexing 是 durable eventual work。原 run 完成、失败或取消语义不得等待 worker；search 只读 ready active rows，index lag 时不得扫描 raw transcript 兜底，也不得把 unavailable 伪装成 empty。
- 全系统同时只有一个 active embedding profile/generation。不同 provider/model/dimensions/distance/canonical template 的 vectors 不得混写或互比；新 generation 完整构建和验证后才能原子接管。
- PostgreSQL + pgvector 是首版唯一索引存储；向量候选使用 exact scan。不得引入外部 vector DB、HNSW/IVFFlat、自动摘要/问答模型、result-body vector 二次召回或常驻本地 embedding runtime。
- embedding 适配器只接收 canonical text，不接收原始用户 query、prompt、reasoning、完整 transcript 或 secret。日志/指标不得记录 canonical text、vector、供应商正文、source payload 或高基数 source identity。
- 复用 M3 的 identity/四表/writer/reader/registry/history/SSE/cleanup，M5 的 executor/producer slot/audit，M6 的 bounded loop/gates/result assembler，M7 的 topology scope/scheduler/result authority。不得复制这些能力或增加旁路 caller。
- 首版单次调用最多 5 cases、单 session 最多 2 次历史调用、单 source run 最多 2 cases、每 case 最多 2 fragments、总 preview 最多 16 KiB；所有裁剪与 tie-break 必须稳定、可测试、可审计。
- 不新增 direct search/job/generation/reindex HTTP route，不新增第三条 SSE，不让 frontend、parser、summarizer、zero-model 路径或 route handler 直接检索 RAG。
- source 删除优先处理跨 run consumption links。已被保留 consumer run 引用的 source 必须稳定 conflict，或在明确授权后级联删除整个 dependent consumer run；不得改写 immutable transcript、留下悬空引用或静默 tombstone。
- 外部 provider smoke 不是单元测试前置条件。先用 deterministic fake 完成全部功能验证；需凭据、联网或可能计费的请求必须先 preflight 并获得明确授权，未运行就记录 `not run`。
- 实际完成记录必须写清改动路径、migration revision、命令、环境、pass/fail/skip/not run 数、失败后的修复、剩余风险和 commit SHA。不得复制旧阶段 pass count，不得用 exit code 0、HTTP 200、收到 `stream_end`、表存在或 worker 启动代替业务断言。

## M8 完成标准

- [ ] 当前唯一 V1 能表达 embedding profile/generation/job、完整 `HistoricalCaseRef`、五种 typed strategy、score components、bounded historical case/fragment 和三态安全失败；全部 producer/consumer/fixture 原子一致。
- [ ] 现有四表数据库可升级到 pgvector schema；fresh install、upgrade、约束/索引、repository round-trip、重启恢复和 migration check 使用专用测试库通过。
- [ ] final eligibility、canonical query bytes、projection identity/digest 和 source closure 有正反 fixture；message/reasoning/prompt/summary/tool/raw/error/bundle/cancelled/incomplete/unknown content 永远不进入 ready index。
- [ ] supplier embedding adapter 只收到 canonical text；维度/数量/顺序/finite/timeout/cancel/error 都经验证，deterministic fake 可重复，profile 变化建立新 generation 并原子切换。
- [ ] durable jobs 能发现 completed + persistence-confirmed sources，支持 lease/retry/restart/backfill/delta；原 run 终态不等待 worker，search 不读取 building/failed/incomplete rows。
- [ ] 五种策略先做 server-owned hard filters，再按 catalog 的 exact structured/FTS/vector 公式和 stable tie-break 排序；限额、去重、多样性、session embedding cache 和 prompt 裁剪均有 fixture。
- [ ] semantic query-to-query 在多水体历史 run 中按完整 case ref 回读正确 session/result；hydrator 重验 active projection、source ownership、schema、scope 和参数/window，绝不串答案或返回完整历史。
- [ ] `stateless` 不暴露历史 tool；`memory_augmented` 由 Agent 显式选择调用或不调用。非法输入严格 0/0/0，合法 empty/unavailable/invalid 均沿 M5 审计且不使主分析失败。
- [ ] historical case/fragment 作为 contextual-only source 写入当前 transcript，生成闭合 refs 与 consumption links；当前直接观测和 M6/M7 authority 不被历史内容改变。
- [ ] production composition 先建立 M8 search producer 再注入 M5，worker 生命周期、startup fail-closed、首代 backfill unavailable、graceful shutdown、取消和日志/指标隐私边界都可执行验证。
- [ ] 既有 run/session/item history、两层 SSE 与 result projection 可 round-trip M8 typed contents；OpenAPI route 集合不增加，frontend/M9 功能不被提前实现。
- [ ] selective source deletion 的 conflict/授权级联、index-only cleanup、inactive generation cleanup 和 transcript-source rebuild 均无悬空引用、无事实改写，并给 M9 留下明确内部操作契约。
- [ ] fake embedding E2E、PostgreSQL integration、M1–M8 full regression、architecture、OpenAPI、smoke、`compileall`、docs audits 与 `git diff --check` 有实际证据；真实 supplier、生产规模与三平台状态按事实报告。

## Milestone 0：基线、真实接缝与红线

### 工作

- [ ] 记录 branch、HEAD、`origin/main`、merge-base、tracked/untracked/permission-limited 状态、Python/依赖版本和测试入口；保护当前 M8 文档与所有已有用户改动。
- [ ] 追踪真实 production composition：lifespan → settings/model catalog → M4 runtime → PostgreSQL/store → coordinator/transcript reader → M5 runtime → M6 runtime → M7 execution stage，确认 M8 runtime/worker 与 historical producer 的唯一插入点。
- [ ] 追踪 run 完成、typed item 持久化、result closure、startup repair、run/session/item history、两层 SSE、cleanup 与 cancellation 的真实顺序，形成 source 可见、job discovery、consumer write 的时序图。
- [ ] 盘点当前 `RAGIndexPort`、`TranscriptEvidenceRequest`、`HistoricalTranscriptRecord`、`HistoricalInferencePayload`、`EvidenceToolOptions`、model catalog/capabilities、runtime builders、test builders 与 fake adapters 的全部生产/测试消费者，形成原子更新清单。
- [ ] 审计 `backend/tests/application/test_milestone8_runtime.py` 的真实覆盖范围；明确它当前是通用 lifecycle/cancel/persistence 历史测试，不得因文件名被当作 M8 RAG 完成证据。只有在能保持责任清晰且同步所有引用时才单独重命名。
- [ ] 记录当前 non-PostgreSQL full regression、专用 PostgreSQL、architecture、OpenAPI/SSE/history/smoke 基线；环境或 ACL 限制如实记录为 `not run`/permission-limited，不改测试绕过基础设施。
- [ ] 增加失败的 architecture red gates：M8 不依赖归档/前端/M9；domain/ports 不导入 SQLAlchemy、pgvector 或 provider SDK；无外部 vector DB；production route/SSE 数不增加；只有 M5 waterbody producer 可以调用 search service。
- [ ] 增加失败的 contract tests，先锁定当前 placeholder 与已冻结 V1 的差距：typed strategy、完整 case ref、query-to-query、contextual-only、0/0/0、active generation、五种 strategy 和 limits。

### Tests

- [ ] 基线报告能区分现有通过、现有失败、PostgreSQL、外部 provider、三平台和 permission-limited 项，不把未运行项算作通过。
- [ ] architecture tests 会拒绝 direct RAG route、第三 SSE、route/parser/summarizer 直接调用、archive import、provider SDK 进入 domain/ports、外部 vector DB 与 result-body embedding shortcut。
- [ ] 新 contract tests 在业务实现前因预期能力缺失而失败，失败原因与设计差距一致，不是 import/fixture 拼写错误。

### Verify

- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/architecture backend/tests/application/test_milestone8_runtime.py backend/tests/ports/test_evidence_ports.py backend/tests/infrastructure/test_m5_runtime.py backend/tests/infrastructure/test_m7_runtime.py -q`
- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests -q`（PostgreSQL/外部项按实际环境拆分并记录）
- [ ] `backend/.venv/Scripts/python.exe -B -m compileall -q backend/app backend/scripts`
- [ ] `git diff --check`

阶段出口：唯一 production 插入点、全部原子消费者、source/job/search/consume 时序、数据库依赖缺口和可复现基线已经记录；red tests 精确描述 M8 缺口，本阶段不实现 RAG 业务行为。

提交边界：只允许 architecture guards、失败 contract tests、必要的测试命名澄清和基线记录；不得提前增加 migration、provider adapter、worker、search 或 production composition。

#### Milestone 0 完成记录

- [ ] 完成后填写：branch/base/HEAD、影响表、基线命令与结果、permission-limited 项、red tests、改动路径、commit SHA、剩余风险。

## Milestone 1：当前唯一 V1 类型、ports 与配置契约

### 工作

- [ ] 在当前 V1 中定义五种封闭 `HistoricalSearchStrategy`，以及只含 `strategy`、`parameter_keys`、`window_ids`、`limit` 的 typed request；删除自由 `query` 和 Agent 可控 source identity/COMID/date/radius/hops/profile 字段。
- [ ] 定义 `HistoricalCaseRef`，至少包含 `run_id`、`session_id`、`target_comid`、`analysis_context_item_id`、`result_item_id`；禁止只用 `run_id` 定位历史答案。
- [ ] 定义带 component 明细的 typed score、matched strategy/reasons、source schema/version、projection/generation identity、授权 cells、overall fields 和有界 fragment refs；不要用无界 `dict`/任意 JSON 代替冻结字段。
- [ ] 原子更新 `HistoricalTranscriptRecord`、`HistoricalInferencePayload`、`EvidenceToolOptions`、M5 tool schema、DTO builders、serializers、fixtures 和 fake producers；保留 `historical_inference/contextual_only` 与 `rag_eligible=false`。
- [ ] 将泛化 placeholder `RAGIndexPort` 收敛为 typed search contract，并新增最小 `EmbeddingPort`、`HistoricalSourceReaderPort`、`RAGJobStorePort`/generation repository port；ports 只表达能力，不泄漏 SQLAlchemy/pgvector/provider response。
- [ ] 定义 `EmbeddingProfile`、projection/canonical schema version、distance=`cosine`、dimensions、semantic threshold 与 generation/job 状态；拒绝未知枚举、非法维度、非法阈值和不匹配版本。
- [ ] 扩展当前 model catalog/capability schema，使其恰好能声明一个 desired embedding profile/model；embedding 不是第三个推理 Agent role，也不进入 M6 token-limit/fallback 语义。
- [ ] 保持启动配置只有一个 desired profile，credentials 仍由既有 settings/secret 边界解析；不得在 catalog、profile、transcript 或 exception 中存 secret。

### Tests

- [ ] request 正反测试覆盖五种 strategy 的字段组合、空/重复/scope 外值、limit 上界和禁止字段；序列化/反序列化不会接纳自由 query/source identity。
- [ ] multi-waterbody fixture 证明相同 `run_id` 下不同 session/result 由完整 case ref 区分；缺少任何关键 identity 都 fail closed。
- [ ] profile/catalog 测试覆盖恰好一个 desired profile、provider/model/dimensions/distance/threshold/version 校验和 secret exclusion。
- [ ] architecture tests 锁定 ports/domain 不导入 infrastructure/provider/ORM；M8 types 不新增 V2/read union/feature flag/第二套 tool。
- [ ] M5/M6/M7 原有 fixtures 在 V1 原子更新后仍编译；不存在同时支持旧自由 query 与新 typed request 的兼容分支。

### Verify

- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/domain backend/tests/ports backend/tests/application/evidence_tools backend/tests/application/reasoning -q`
- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/domain/test_model_catalog.py backend/tests/infrastructure/test_m5_runtime.py backend/tests/infrastructure/test_m6_runtime.py backend/tests/infrastructure/test_m7_runtime.py -q`
- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/architecture -q`
- [ ] `backend/.venv/Scripts/python.exe -B -m compileall -q backend/app backend/scripts`
- [ ] `git diff --check`

阶段出口：当前唯一 V1 已能在不接数据库/网络的情况下完整表达 M8 request、case、profile、job 与结果边界；所有旧 placeholder consumer 已原子更新，仍未产生真实搜索或 embedding。

提交边界：types、ports、catalog schema、tool schema、fixtures 和 architecture guards；不得包含 migration、SQL query、provider HTTP、worker 或 production runtime wiring。

#### Milestone 1 完成记录

- [ ] 完成后填写：被替换的 placeholder、原子消费者清单、focused/architecture 结果、改动路径、commit SHA、剩余风险。

## Milestone 2：pgvector schema、migration 与 repositories

### 工作

- [ ] 通过项目依赖管理添加并锁定与当前 SQLAlchemy/PostgreSQL 栈兼容的 pgvector Python 依赖；记录实际解析版本，不在文档或代码中假装验证未安装版本。
- [ ] 将本地 Compose 的 PostgreSQL 服务改为受控、固定 tag 的 pgvector-enabled image，并保持现有数据库名、healthcheck、volume 和快速指南语义；先用 `docker compose config` 验证，不触碰业务库。
- [ ] 新增单个可审查 Alembic revision：启用/检查 vector extension，并创建 `rag_index_generations`、`rag_index_jobs`、`rag_case_projections`、`rag_case_windows`、`rag_fragment_projections`、`rag_consumption_links`。
- [ ] 为 source run/session/item、generation/profile、projection identity、job state/lease/attempt、parameter/window、consumer run/session/item 建立必要 PK/FK/unique/check/index；数据库层拒绝跨 generation 混写、重复 source projection、无 source 的 fragment 和悬空 consumption link。
- [ ] 为 canonical/structured/FTS 建立明确列与 GIN/B-tree 索引；vector 列按设计使用 unconstrained `vector`，以容纳不同 dimensions 的 inactive/building generations，并由每代 profile 在写入/查询时严格验证维度。首版不得创建 HNSW/IVFFlat 或 provider-specific payload 列。
- [ ] 实现最小 repository primitives：generation/job CRUD 与 claim、case/window/fragment 原子写入、active generation read/switch、consumption link append/read/delete-boundary query；此阶段不实现 policy ranking。
- [ ] repository 所有写入接受已验证 typed projections，不负责解析 transcript、调用 embedding 或发明 eligibility；所有读写遵守 transaction 和 deterministic ordering。
- [ ] 更新 database schema contract、migration head/check、测试清理顺序和 Docker PostgreSQL 指南；测试数据库必须通过 `WATERAGENTS_TEST_DB_NAME=wateragents_test` 或等价进程级覆盖与业务库隔离。

### Tests

- [ ] fresh database `upgrade head`、现有四表数据库 upgrade、schema check 和受支持 downgrade/upgrade round-trip 均通过；vector extension 缺失/权限不足时错误清晰且 fail closed。
- [ ] 六类表的 PK/FK/unique/check/index、unconstrained vector 列与 per-generation dimension validation、source ownership、generation isolation 和 cascade/restrict 行为由数据库集成测试直接断言。
- [ ] job claim 在两个 repository/transaction 竞争时 exact once；lease/attempt/status 更新不丢失，稳定顺序不依赖物理 row 顺序。
- [ ] case + windows + fragments 写入是原子的；中途失败后不存在 ready 半成品，重复同 source/generation 写入幂等。
- [ ] active generation switch 是单事务；任一时刻最多一个 active，building/failed generation 不会被普通 read 返回。

### Verify

- [ ] `docker compose config`
- [ ] 使用专用测试数据库运行 `backend/.venv/Scripts/wateragents-db.exe upgrade` 与 `backend/.venv/Scripts/wateragents-db.exe check`
- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/persistence/test_schema_contract.py backend/tests/persistence/test_database_config.py backend/tests/persistence -q`
- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/architecture -q`
- [ ] `backend/.venv/Scripts/python.exe -B -m compileall -q backend/app backend/scripts`
- [ ] `git diff --check`

阶段出口：现有数据库可安全升级，六类 RAG 数据的结构、约束和 repository round-trip 可复现，active generation 能原子表达；尚无 transcript projection、embedding、worker 或 search policy。

提交边界：dependency lock、Compose、一个 migration、ORM/records/repositories、数据库测试和指南；不得包含 provider adapter、projection builder、worker/search service 或 M5 wiring。

#### Milestone 2 完成记录

- [ ] 完成后填写：dependency/image/revision、fresh/upgrade DB 证据、约束与竞争测试、改动路径、commit SHA、剩余风险。

## Milestone 3：final eligibility、canonical query 与 source projection

### 工作

- [ ] 实现纯函数 final eligibility：先验证 run/session completed、persistence-confirmed、typed schema/ownership/ref closure，再只允许 `inference_result v1` root 与直接关联、仍符合 D01 的 `evidence_fragment v1`。
- [ ] 显式拒绝 message、planning/reasoning、prompt、provider request/response、repair/raw output、tool call/result、inspection、error、empty/unavailable/invalid、bundle、dependency manifest、summary/topology aggregate、cancelled/incomplete/unknown content。
- [ ] 实现 typed source reader，按单个已注册 item 精确读取/解析/redact；不得扫描或反序列化整个 transcript，也不得为未知 schema 提供通用 JSON/text fallback。
- [ ] 从已验证 `AnalysisContext`、目标 waterbody session/result 与已保存 graph metadata 构建 case projection、windows、eligible fragments 和完整 case ref；保持原 risk/confidence/availability/quality/provenance，不重算科学结论。
- [ ] 实现 canonical case-query builder，输入只含冻结的 normalized intent、requested/supporting parameter keys、mode、time/season/resolution 与 schema/projection version；排除完整原始 query、rationale、message 和 result body。
- [ ] 冻结 canonical bytes 的排序、大小写/枚举、时间/season、缺省值、UTF-8 和 version 表达；同一输入跨进程产生完全相同 bytes/digest，不依赖 locale、hash seed 或 dict order。
- [ ] 定义 append-only projection identity/digest：同 source item + generation 重放幂等，新历史永远生成新 source projection，不按 COMID/参数/window/topic latest-wins 覆盖。
- [ ] hydration 前置所需的 source closure validator 必须可复用，能重验 source run/session/result ownership、schema、active projection identity 和当前 scope，而不是信任索引 row。

### Tests

- [ ] eligibility 正反矩阵覆盖所有 allowlist/denylist content、run/session 状态、persistence 状态、rag candidate、schema version、source ownership 和 ref closure。
- [ ] property/golden tests 锁定 canonical bytes/digest；字段顺序、进程/hash seed、等价输入顺序不改变结果，版本或语义字段变化必须改变 identity。
- [ ] privacy tests 证明 canonical text/projection 不含 raw user query、prompt、reasoning、provider envelope、secret、错误正文或无界 source arrays。
- [ ] multi-waterbody fixture 为同一 run 产生各自正确 case ref/projection；fragment 只能挂到直接关联 result，不能跨 session/waterbody 串联。
- [ ] typed single-item reader 只解析目标 item；malformed/unknown/non-eligible item fail closed 且不会导致完整 transcript scan。

### Verify

- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/application/transcripts backend/tests/application/transcript_memory backend/tests/domain -q`
- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/architecture -q`
- [ ] `backend/.venv/Scripts/python.exe -B -m compileall -q backend/app backend/scripts`
- [ ] `git diff --check`

阶段出口：给定一个已持久化 source，系统可在无数据库写入和无网络的纯测试中稳定决定“能否索引”和“索引什么”；canonical bytes、case ref、projection/ref closure 可复现且不泄露原始 query/推理文本。

提交边界：eligibility、typed source reader/projection、canonical builder、identity/digest 与纯测试；不得包含 provider HTTP、worker、SQL ranking、M5 producer 或 production composition。

#### Milestone 3 完成记录

- [ ] 完成后填写：allowlist/denylist fixture 数、canonical golden/version、隐私断言、改动路径、commit SHA、剩余风险。

## Milestone 4：EmbeddingPort adapter 与 generation 切换

### 工作

- [ ] 实现首版远程 supplier `EmbeddingPort` adapter，复用当前 provider HTTP/config/credential 边界；application/domain 只依赖 port，不依赖供应商 SDK/response 类型。
- [ ] adapter 只接受 canonical text batch，并验证输出数量、输入顺序、dimensions、finite numeric values 和可接受 norm；任何缺项、重复、NaN/Inf、维度漂移或供应商结构变化都 fail closed。
- [ ] 定义 timeout、调用取消和安全错误映射；adapter 不自行无限 retry、不决定 job attempt/backoff、不记录 request text/vector/secret/provider body。
- [ ] 实现 deterministic fake embedding，显式由测试 composition 注入；相同 canonical bytes/profile 必须跨进程返回相同 vector，不使用 Python 随机 hash。
- [ ] 实现 desired profile validation 与 stable profile identity/digest；provider/model/dimensions/distance/semantic threshold/projection/canonical version 任一变化都产生新 generation identity。
- [ ] 实现 generation lifecycle primitives：`building → active → inactive` 或 `building → failed`；只有通过完整计数/维度/source-closure 验证的 generation 可以在单事务中接管 active。
- [ ] profile 切换期间 search 每次只 pin 一个 active generation；旧 generation/credential 在 reader lease 结束和 M9 可见切换完成前不得清理。
- [ ] startup 能区分损坏/缺失基础 schema/profile（fail closed）与合法首代 generation 尚在 building（应用可启动、historical producer unavailable）。

### Tests

- [ ] adapter contract tests 覆盖 batch ordering、dimensions、finite、timeout、cancel、HTTP/供应商错误、安全异常正文和零输入不发请求。
- [ ] fake 的 determinism/golden 测试跨实例与进程稳定，并能制造相似/不相似 query 用于后续 ranking，不冒充真实模型质量证明。
- [ ] profile identity 测试覆盖每个字段变化与 secret 不参与 identity/持久化；不同 profile vector 绝不混写/互比。
- [ ] generation 竞争测试覆盖两个 builder、激活/失败竞争、reader pin、切换瞬间和 inactive cleanup guard；任一 search 观察到且仅观察到一个 generation。
- [ ] startup 状态测试区分 schema/profile 损坏、无 active 但 building、active ready、active dimensions 不匹配四种情况。

### Verify

- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/infrastructure/embeddings backend/tests/application/transcript_memory/test_generations.py -q`
- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/persistence backend/tests/domain/test_model_catalog.py -q`
- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/architecture -q`
- [ ] `backend/.venv/Scripts/python.exe -B -m compileall -q backend/app backend/scripts`
- [ ] `git diff --check`

阶段出口：supplier adapter 与 deterministic fake 遵守相同严格 port；profile/generation 可验证、可失败、可原子切换，search 可 pin 单一 active generation；尚不运行 durable worker，也不把 historical tool 接入 Agent。

提交边界：embedding adapter/fake、profile identity、generation service 与 focused tests；不得包含真实凭据、付费 smoke 结果、job worker、search ranking 或 M5 wiring。

#### Milestone 4 完成记录

- [ ] 完成后填写：adapter/fake/profile/generation 测试、真实 provider 是否 `not run`、改动路径、commit SHA、剩余风险。

## Milestone 5：durable jobs、worker 与 backfill

### 工作

- [ ] 实现同一个幂等 discovery query：只发现 run/session completed、persistence-confirmed、`rag_eligible=true`、allowlisted result roots；discovery 只创建 job，不读全文、不调用 embedding。
- [ ] 将 run-terminal post-commit wake signal 仅作为降延迟提示；周期性 bounded reconciliation 必须在信号丢失、进程重启和历史 backfill 时仍能发现遗漏，不增加第二套 source truth。
- [ ] 建立 first-generation workflow：创建 building generation 与稳定 `(created_at,item_id)` cutoff、为 cutoff 内 candidates 建 jobs、处理 completed/excluded/failed、验证完整性、原子激活、再追赶 cutoff 后 delta。
- [ ] job 执行时重新读取 source 并执行 Milestone 3 final eligibility；稳定不合格转 `excluded/source_no_longer_eligible`，不得写 projection 或恢复旧内容。
- [ ] 实现有界 claim batch、embedding batch、worker concurrency、attempt deadline、lease、最多 5 次 retry 和有上限指数 backoff；只有 timeout/rate-limit/临时 unavailable 可 retry。
- [ ] source/schema/profile/dimension/non-finite/ref closure 等非 retryable 错误立即 failed；错误码安全可审计，失败 generation 不得被激活或伪装 complete。
- [ ] 每批 embedding 返回后逐 row 验证；case/windows/fragments 与 job completed 必须在同一事务提交，任一 row 失败都不暴露 partial ready projection。
- [ ] 实现 profile-change backfill：旧 active 继续服务，新 building 始终从原 transcript 重建；cutoff 后新 source 先进入旧 active，并由 delta reconciliation 纳入新 generation。
- [ ] activation gate 验证 cutoff 内 candidate/job/source counts、excluded reasons、无 pending/retry/failed 缺口、projection digests、dimensions/profile/FK 与 sample ranking fixtures；不得只检查“队列为空”。
- [ ] worker shutdown 先停止 claim，再让在途事务有界收敛；超时后 lease 可由下次启动恢复。worker/provider/database 故障绝不能改写 run/session terminal。

### Tests

- [ ] discovery 在重复 wake、丢失 wake、周期 reconciliation、重启和并发调用下为同一 generation/source 创建恰好一个 job。
- [ ] cutoff/backfill 测试覆盖 cutoff 前、构建期间、cutoff 后新 source、delta race 和激活边界；激活后没有 source 因切换窗口永久遗漏。
- [ ] lease tests 使用可控 clock/gate 覆盖两个 claimers、worker crash、lease expiry/reclaim、attempt deadline、retry/backoff 上限和 non-retryable immediate failed，不使用真实 sleep 证明竞态。
- [ ] batch 原子性测试覆盖 embedding 缺项/错序/非法 vector、DB commit failure、source 中途删除/失去资格；任何失败都不产生 ready 半成品。
- [ ] run lifecycle 测试证明 run completed 不等待 job，worker/provider 失败不改 run terminal；首代 building 期间当前主链照常运行且 historical search 为 typed unavailable。
- [ ] generation validation 故意制造 pending/retry/failed/count/digest/dimension/FK 缺口时拒绝激活，修复后可重复验证并只激活一次。

### Verify

- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/application/transcript_memory/test_discovery.py backend/tests/application/transcript_memory/test_worker.py backend/tests/application/transcript_memory/test_backfill.py -q`
- [ ] 使用专用测试数据库运行 `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/persistence/test_rag_jobs.py backend/tests/persistence/test_rag_generations.py -q`
- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/application/analysis_runs backend/tests/application/transcripts -q`
- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/architecture -q`
- [ ] `backend/.venv/Scripts/python.exe -B -m compileall -q backend/app backend/scripts`
- [ ] `git diff --check`

阶段出口：first generation 可以从原 transcript durable backfill、严格验证并原子激活；job 能在丢信号、重复、失败、取消 shutdown 和重启后收敛，且任何 indexing 状态都不改变原 run 终态。

提交边界：discovery/backfill/worker、job/generation application services、相关 settings 和 focused tests；不得包含 SQL ranking、M5 producer、public route 或真实 provider 请求。

#### Milestone 5 完成记录

- [ ] 完成后填写：cutoff/source/job/projection 计数、重启/竞态/失败证据、PG 命令、改动路径、commit SHA、剩余风险。

## Milestone 6：五种 search policy、ranking 与 bounded hydration

### 工作

- [ ] 实现统一 search pipeline：capture 一个 active generation → 从 sealed scope 补全授权条件 → SQL hard filters → exact structured/FTS/vector signals → 固定公式 → stable order → dedupe/diversity/byte fit → source hydration/revalidation。
- [ ] 实现 `exact_comid_parameter_window`：同 COMID、ParameterKey overlap、UTC 正长度窗口重叠、只返回相交 cells；不得为未来不重叠窗口偷偷放宽。
- [ ] 实现 `nearby_comid_parameter`：只用当前保存 graph active nodes/geometry、固定 25 km 且不超过 graph `max_distance_km`、排除 current COMID；不得重调 NLDI/地理服务。
- [ ] 实现 `parameter_season`：当前/active-node COMID、同 mode、local `DJF/MAM/JJA/SON` 与稳定 `season_year`、duration/resolution compatibility；首版保持 CONUS/北半球版本边界。
- [ ] 实现 `semantic_case_query`：先按 current/active nodes、mode、ParameterKey、profile/template hard-filter，再在候选集上 exact cosine；只 embedding 当前 canonical query，不使用 result/summary/fragment body vector。
- [ ] 实现 `topology_neighborhood_parameter`：只使用当前保存 graph 的 participating edges、ancestor/descendant、hop/path distance 与原 `max_hops/max_distance_km`；不得重跑 BFS 或扩大 scope。
- [ ] 严格实现 Policy Catalog 的 normalization、五套 score 公式、semantic threshold 与 stable tie-break；NaN/Inf/非法分母/profile mismatch 使整次 search invalid，不静默丢坏 row。
- [ ] 实现最多 50 internal candidates、5 cases/call、1 case/source session-result、2 cases/source run、2 fragments/case、stable cells/fragments order 与 16 KiB 完整-record byte fit；不能截断 JSON 或隐式分页。
- [ ] 只在 `semantic_case_query` 调用 `EmbeddingPort`；四种结构化策略在 supplier unavailable 时仍能运行。当前 session 可按 `(profile_id,canonical_text_digest)` 缓存成功 query vector，cancel/failed 不缓存且不持久化。
- [ ] hydrator 对每个 hit 回读原 result/fragment，重验 run/session/result ownership、target COMID、schema/digest、active projection、current scope/hard filters、cell/ref closure 与 fragment direct relation。
- [ ] 任一 top hit authority/corruption 失败使整个 collection `invalid/historical_source_invalid`，不得返回可信/不可信混合 partial；合法无命中是 completed/empty。

### Tests

- [ ] 五种 strategy 各有 table-driven hard-filter 正反 fixture，并证明过滤发生在 FTS/vector ranking 前；scope 外 COMID/parameter/window/source/profile 永远不会成为候选。
- [ ] ranking golden tests 精确断言每个 component、固定公式、semantic threshold 和完整 stable order；数据库插入顺序变化不改变返回顺序。
- [ ] query-to-query fixture 至少包含两个历史 runs 和一个多 waterbody run，证明命中正确 session/result，answer/result body 改写不影响首版 semantic recall。
- [ ] limits/property tests 覆盖 50/5/2/2/16 KiB、dedupe、多样性、完整-record skip、all-too-large invalid 和同 session 两次调用之外由上层拒绝的边界。
- [ ] supplier unavailable 时四种 structured strategies 仍成功、semantic 为 unavailable；成功 query embedding 同 session 复用一次，失败/cancel 不复用。
- [ ] hydration corruption 矩阵覆盖 source 删除、session/result/COMID/schema/digest/ref/fragment 不一致；全部返回 typed invalid、0 partial records。
- [ ] profile switch 与并发 search 使用可控 gate 证明单次 operation 始终 pin 同一 generation，不混读两代 rows/vectors。

### Verify

- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/application/transcript_memory/test_search_policies.py backend/tests/application/transcript_memory/test_ranking.py backend/tests/application/transcript_memory/test_hydration.py -q`
- [ ] 使用专用测试数据库运行 `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/persistence/test_rag_search.py -q`
- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/architecture -q`
- [ ] `backend/.venv/Scripts/python.exe -B -m compileall -q backend/app backend/scripts`
- [ ] `git diff --check`

阶段出口：五种固定 strategy 在同一 search pipeline 中按 server-owned hard filters 和精确 policy 产生稳定、有界、可审计结果；semantic 正确执行 query-to-query，hydration 只返回经重新授权的原 source 投影。

提交边界：search policies/repository queries/ranking/hydration/session cache 与 focused tests；不得包含 M5 producer、production worker wiring、public API 或 frontend。

#### Milestone 6 完成记录

- [ ] 完成后填写：五策略 fixture、ranking golden、query-to-query 多水体证据、limits/PG 结果、改动路径、commit SHA、剩余风险。

## Milestone 7：M5/M6 historical producer 与当前证据优先级

### 工作

- [ ] 以现有 M5 `TranscriptEvidencePort`/producer slot 实现唯一 production `search_transcript_evidence` producer；不得创建第二个 executor、另一个 tool loop 或 route/direct caller。
- [ ] 在 M5 scope preparation 中从 sealed `EvidenceSessionScope` 填写 operation/run/session/memory mode/current COMID/authorized COMIDs/time/season/topology/profile，只接受 Agent 的 strategy、scope 内 parameters/windows、1–5 limit。
- [ ] `stateless` roster 完全不含 historical tool；`memory_augmented` 且 producer 正常装配时才可见。Agent 可以 0 次、1 次或 2 次调用；server-side budget 最多 2 次且不因 repair/model retry 重置。
- [ ] 非法 strategy/options/parameters/windows/limit 继续复用 M5 preflight：严格 `0 search / 0 embedding / 0 hydrate / 0 source record`，只保留既有安全 rejection audit。
- [ ] 将 search completed/empty、unavailable/index-building/embedding、invalid/index/source/protocol 和 cancel 映射进既有 M5 typed collection/result；不得把 unavailable/invalid 伪装 empty、required evidence failure 或普通异常泄漏。
- [ ] 将每个 hit materialize 为 `HistoricalTranscriptRecord`/`historical_inference` contextual source，保留完整 source identity/schema/original confidence/quality/time/score components/matched_by；普通 `transcript_item_refs` 不跨 run。
- [ ] 通过既有 M5 source → fragment → tool result → bundle 写入闭环，并在 source-record item 持久化的同一事务确认 `rag_consumption_links`；link 失败不能留下无 ownership registry 的正文。
- [ ] 保持每个 historical fragment `rag_eligible=false`，避免“历史命中再次成为新事实源”导致递归索引；原 source result/fragment 仍通过稳定 historical refs 读取。
- [ ] 在 M6 gates/result assembly 加回归保护：历史永远不能满足 target parameter、discharge 或 current observation；不能提高原 confidence/quality/applicability，不能覆盖当前 valid direct observation。
- [ ] 保持 M7 scheduler/upstream result/run summarizer 不变：当前 run 内 direct-upstream 仍按 M7 规则使用，跨 run historical 只存在于 waterbody session optional context，summarizer/zero-model 不获得 tool。

### Tests

- [ ] roster tests 覆盖 stateless、memory_augmented+ready、memory_augmented+building/unavailable；parser、summarizer、zero-model、HTTP handler 均不能直接调用 producer。
- [ ] tool budget tests 覆盖 0/1/2/第 3 次调用、repair/retry、两个 waterbody sessions 的独立预算和 run cancel；descriptor 与 server enforcement 一致。
- [ ] preflight table 覆盖 source identity/COMID/date/radius/hops/profile/raw query 注入、scope 外/空/重复 parameter/window、非法 limit/options，并断言 0 search/embedding/hydrate/source。
- [ ] empty/unavailable/invalid/cancel round-trip 保留 typed 差异和安全错误码；当前已有 supported cells 不因 optional historical failure 变成 failed。
- [ ] provenance tests 证明 historical source 保留原 run/session/item/COMID/confidence/schema/score，当前普通 refs 不跨 run，用户仍可按原 history path 找到 source。
- [ ] priority/gate fixture 同时给出当前 direct observation、上游/current indirect 和冲突历史，证明当前事实优先且历史不解锁 gate、不重算 confidence。
- [ ] persistence failure/transaction test 证明 source record 与 consumption link 同生共灭；materialized historical fragment 不会被下一轮 discovery 索引。

### Verify

- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/application/evidence_tools backend/tests/application/reasoning -q`
- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/application/transcript_memory/test_producer.py backend/tests/application/transcript_memory/test_priority.py -q`
- [ ] 使用专用测试数据库运行 `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/persistence/test_rag_consumption_links.py backend/tests/application/transcripts -q`
- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/infrastructure/test_m5_runtime.py backend/tests/infrastructure/test_m6_runtime.py backend/tests/infrastructure/test_m7_runtime.py backend/tests/architecture -q`
- [ ] `backend/.venv/Scripts/python.exe -B -m compileall -q backend/app backend/scripts`
- [ ] `git diff --check`

阶段出口：`memory_augmented` waterbody Agent 可通过唯一 M5 tool 显式、有限地读取历史；typed provenance/审计/consumption ownership 完整，stateless 与 M6/M7 authority 未变化，optional failure 不污染当前结论。

提交边界：M5 producer/preflight/materializer、M6 gate/priority guards、consumption-link atomic write 与 focused regressions；不得包含 production lifespan wiring、public route 或 M9 admin UI/API。

#### Milestone 7 完成记录

- [ ] 完成后填写：roster/budget/0-0-0/provenance/priority/link 证据、改动路径、commit SHA、剩余风险。

## Milestone 8：production composition、生命周期与可观测性

### 工作

- [ ] 按唯一顺序重组 production startup：DB/Alembic/vector/catalog validation → transcript/coordinator/M4 → M8 ports/profile/search/worker → producer 注入 M5 → M6/M7 → 单次 execution-stage bind → worker ready 后接收流量。
- [ ] M8 runtime 必须在 M5 runtime 前构建并传入真实 producer；删除/替换 placeholder `None` 或泛化 stub 的 production 路径，不保留“有配置走新路径、无配置走旧路径”的双行为。
- [ ] startup 对缺 migration/extension、重复/非法 desired profile、损坏 active generation/profile/dimensions fail closed；合法首代 building 则允许应用启动，producer 稳定返回 `index_building` unavailable。
- [ ] 默认单进程/单 worker；settings 只暴露 worker batch/concurrency、embedding timeout、lease/backoff/reconciliation、retired retention 等基础设施预算，未知/越界值 fail startup，不静默默认。
- [ ] lifespan startup 启动一个 bounded worker，shutdown 先停止新 claim、等待有界事务、释放资源，再按既有顺序停止 coordinator/dispose engine；shutdown 不是用户 cancel，不发布伪 lifecycle event。
- [ ] 将 run-terminal post-commit wake 接到 worker，但保持信号可丢、无业务权威；取消 run 只影响当前 workflow/tool，不能取消或改写已经 durable 的其他历史 indexing jobs。
- [ ] 增加结构化日志/metrics：generation state/age、job pending/lease/retry/failed/lag、embedding latency/error、strategy/empty/failure/latency/candidate-return counts、hydration invalid、active profile digest。
- [ ] 日志/metrics 使用 bounded labels 与安全 correlation/hash；不得记录 run/session/item/COMID 为无界 metric label，也不得输出 raw query、canonical text、vector、credential、provider body、source payload 或 traceback 到公开错误。
- [ ] 更新 runtime builders/fakes/smoke composition，使测试 fake 只替换外部 embedding/时序，不需要 production hidden switch、特殊 header/query 或 direct M8 endpoint。
- [ ] architecture guards 锁定唯一 worker/search owner、唯一 M5 producer injection、无 route/parser/summarizer/zero-model 旁路和 domain/application 依赖方向。

### Tests

- [ ] production composition test 从真实 app factory/lifespan 证明构建顺序、一个 worker、真实 M5 producer、单 M7 execution owner 和完整 shutdown 顺序。
- [ ] startup matrix 覆盖 schema/extension/catalog/profile/active corruption 与 legitimate first-build；前者拒绝服务，后者主链可用且历史 tool 返回 typed unavailable。
- [ ] cancellation/shutdown 竞争使用可控 gate 覆盖 claim 前、embedding 中、DB commit 前、activation 前和 lifespan stop；无新业务动作越过取消/stop 边界，无伪 run terminal。
- [ ] log/metric capture 证明必需状态可定位，同时 raw/canonical query、vector、secret、provider/source body、traceback 和高基数 labels 不出现。
- [ ] production app 的 OpenAPI route 集合与 M7 基线相同；不存在 direct search/job/generation/reindex/embedding route 或第三 SSE。

### Verify

- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/infrastructure/test_m8_runtime.py backend/tests/api/test_openapi_contract.py backend/tests/architecture -q`
- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/application/analysis_runs backend/tests/application/transcript_memory -q`
- [ ] 使用专用测试数据库运行 production lifespan/factory integration tests，并记录实际命令与 basetemp
- [ ] `backend/.venv/Scripts/python.exe -B -m compileall -q backend/app backend/scripts`
- [ ] `git diff --check`

阶段出口：真实 app factory 只装配一个 M8 runtime/worker，并在 M5 之前提供 production historical producer；startup/shutdown/failure/observability 都遵守 M1–M8 边界且不新增公共 surface。

提交边界：settings、runtime composition、lifespan、wake signal、logs/metrics、production integration/architecture tests；不得包含 M9 admin route/settings UI/credential cleanup。

#### Milestone 8 完成记录

- [ ] 完成后填写：factory/lifespan/startup matrix、日志隐私/OpenAPI 证据、改动路径、commit SHA、剩余风险。

## Milestone 9：history/SSE、cleanup 与 M9 handoff

### 工作

- [ ] 在现有 production typed-content registry 注册 M8 扩展后的 call/source/fragment/result/bundle schema，保持未知/不支持 schema 的既有 fail-closed 行为。
- [ ] 更新 session snapshot/detail/item full-content/history readers，使 bounded historical DTO、score components、matched strategy、safe empty/unavailable/invalid 与 stable source refs 可读取；canonical text/vector/job/provider raw response 永不公开。
- [ ] 复用现有 run SSE 与 session SSE 回放同一已持久化 typed facts；不增加第三 stream、M8-specific event、per-search subscription 或 direct cancel。
- [ ] 确认 M8 不把 search hit 或 index job 伪装成 lifecycle terminal；SSE reconnect/sequence/keepalive/cancel/late-content 继续遵守 M2/M3 契约。
- [ ] 实现内部 typed operations：`inspect_index_health`、`build_generation`、安全过滤的 `retry_failed_jobs`、`retire_generation`、`delete_retired_generation`、`inspect_cleanup_dependencies`；M8 不为它们增加 route。
- [ ] generation/index-only cleanup 只删派生 rows；active 只有另一完整 generation 接管后才能退休，inactive 有 reader lease 时不能删，rebuild 只能从原 transcript allowlist 开始。
- [ ] history/source cleanup 在删除前查询 consumption graph：无 consumer 可按既有 owner 流程删除；被保留 consumer 引用时稳定 conflict；只有明确授权才级联删除整个 dependent consumer run。
- [ ] 级联顺序处理 transitive dependencies、consumer/source/index/jobs/links，保证幂等、可重试、无悬空 ref；绝不能改写 retained immutable transcript、只删 source 或静默 tombstone。
- [ ] 固化 M9 handoff：保存 desired profile → build/validate → atomic activate → reader drain/retire → 最后才允许清理旧 credential；API keys 明确不属于 history/RAG cleanup graph。
- [ ] 同步 API、frontend integration、architecture、milestones 和 deferred contracts 的最终 M8 表述；不得提前设计 M9 授权/UI/admin endpoint。

### Tests

- [ ] tool call/source/fragment/result/bundle 通过 persistence reload、session detail、item full-content、history pagination、session SSE live/replay 与 run projection round-trip 一致。
- [ ] OpenAPI snapshot/route enumeration 证明 production surface 未增加；公开 DTO/错误不含 canonical/vector/job/provider internals。
- [ ] source cleanup fixture 覆盖 no consumer、one consumer、transitive consumers、conflict、明确 whole-run cascade、重复/中断重试和并发新 consumption；均无 dangling link/ref。
- [ ] generation cleanup fixture 覆盖 building/active/inactive/failed、reader lease、切换竞争、index-only delete/rebuild；原 transcript/history 始终不变。
- [ ] M9 handoff state-machine test 证明新 profile 激活前旧 adapter/credential 仍可用，激活与 reader drain 后才出现旧 credential cleanup readiness。

### Verify

- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/application/transcripts backend/tests/api/test_analysis_history.py backend/tests/smoke/test_run_sse_smoke.py -q`
- [ ] 使用专用测试数据库运行 `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/persistence/test_rag_cleanup.py backend/tests/persistence/test_rag_consumption_links.py -q`
- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/api/test_openapi_contract.py backend/tests/architecture -q`
- [ ] `backend/.venv/Scripts/python.exe -B -m compileall -q backend/app backend/scripts`
- [ ] `git diff --check`

阶段出口：M8 facts 可通过既有 history/SSE 安全回放，公共 surface 不变；index 与 source cleanup ownership 在 conflict/级联/重建中无悬空引用，并给 M9 留下可调用但尚未公开的内部操作与 credential 顺序。

提交边界：typed registry/read models/SSE-history regressions、internal cleanup services、M9 handoff contracts 与同步文档；不得增加 M9 public admin/settings route 或 UI。

#### Milestone 9 完成记录

- [ ] 完成后填写：round-trip/OpenAPI/cleanup graph/profile handoff 证据、改动路径、commit SHA、剩余风险。

## Milestone 10：端到端验收、契约审计与交付证据

### 工作

- [ ] 新增 `backend/scripts/m8_memory_rag_smoke.py`（或仓库约定的等价路径）和对应 smoke test，只通过真实 app/runtime entrypoint，以 deterministic fake embedding 驱动，不开放 test-only production switch。
- [ ] 构造至少两个历史 runs，其中一个是多 waterbody run；等待 durable jobs/build 激活后创建新的 memory-augmented run，证明 query-to-query 命中正确 session/result、写入 historical source/fragment/bundle/consumption link 并完成当前 reasoning。
- [ ] 同一 E2E 覆盖 stateless 不见 tool、memory-augmented 选择不调用、正常 empty、first-build unavailable、embedding unavailable/protocol invalid、source invalid、cancel 和当前观测冲突优先级。
- [ ] 端到端执行 profile 新 generation build/validate/atomic switch、并发 search pin、worker crash/restart/lease recovery、source conflict/授权级联、index-only rebuild，证明无 run 终态改写或 dangling ref。
- [ ] 从生产 create-run entrypoint 沿 scope preparation、M5 executor、search/ranking/hydration、transcript persistence、M6/M7 assembly、history/SSE 和 cleanup 做一次 contract sweep，搜索旧 placeholder/free-query/第二路径。
- [ ] 运行 M1–M8 focused、完整 non-PG、专用 PostgreSQL、architecture、OpenAPI、smoke、compileall 和 docs audits；逐项记录命令与真实 pass/fail/skip/not-run，不复制旧里程碑数字。
- [ ] 只有在用户明确授权、凭据 preflight 成功且可接受可能计费时才运行 supplier smoke；只验证 adapter contract/维度/安全错误，不发送原始 query/transcript，不把一次成功当作质量或容量证明。
- [ ] 将本文件实际完成项、完成记录、contract matrix、总 milestones、deferred 承接记录、架构/API/frontend handoff 同步为真实状态；未运行的三平台/真实 provider/生产容量保持 `partial` 或 `not run`。
- [ ] 审查 staged/unstaged/untracked 范围、migration head、dependency lock 与生成物；不得提交 secret、数据库 volume、pytest cache、真实 provider response、vector dump 或无关用户文件。

### Tests

- [ ] fake E2E 对 source identity、完整 case ref、strategy/components/score、授权 cells/fragments、current-priority、consumption link、history/SSE 与 final result 做业务断言，而非只断言 HTTP 200/worker exit 0。
- [ ] PostgreSQL E2E 从现有四表 migration 开始，跨至少一次进程 restart 验证 generation/job/projection/read/cleanup，不使用业务数据库。
- [ ] fault E2E 对每种 typed failure 明确断言主链是否继续、写入哪些审计项、不会出现哪些 source/result/fallback；取消后无新 search/source publish。
- [ ] contract matrix 中 GLOBAL-C01–C11、M8-C01–C16、M8-A01–A10 每项都有实现位置、具体测试和文档状态；`partial` 不标记完成。
- [ ] docs link/fence/checkbox/contract-ID audits 通过；本文件不存在被误勾选、重复 ID、缺失 stage exit 或无法执行的虚构命令。

### Verify

- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/smoke/test_m8_memory_rag_smoke.py -q`
- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/architecture backend/tests/application backend/tests/domain backend/tests/ports backend/tests/infrastructure backend/tests/api backend/tests/smoke -q`
- [ ] 使用专用测试数据库运行 `backend/.venv/Scripts/python.exe -B -m pytest backend/tests -q`
- [ ] `backend/.venv/Scripts/python.exe -B backend/scripts/m8_memory_rag_smoke.py`
- [ ] `backend/.venv/Scripts/python.exe -B -m compileall -q backend/app backend/scripts`
- [ ] 执行 docs link/fence/contract-ID/unchecked-count audits，并保存实际输出
- [ ] `git diff --check`
- [ ] `git status --short --branch`

阶段出口：M8 的 production 路径、数据库、worker、五种 strategy、Agent tool、history/SSE、failure/cancel、profile switch 与 cleanup 有可复现的 fake/PG 证据；适用契约全部承接，剩余外部验证边界如实记录，可以交给 M9 而不留下模糊 TODO。

提交边界：E2E/smoke、审计修复、完成记录、contract/deferred/milestone 文档同步；不夹带 M9 public API/frontend/admin 实现。最终提交前列出相对 `origin/main` 的完整文件范围和 commit SHA，未经用户要求不 push。

#### Milestone 10 完成记录

- [ ] 完成后填写：fake/PG/full/smoke/docs 命令和计数、provider/三平台/容量状态、contract audit、文件范围、commit SHA、剩余风险与 M9 接力项。

## Contract 承接矩阵（全部完成后填写）

状态只能使用 `pending / partial / implemented`。只有 production implementation、对应测试、文档和本阶段完成证据齐全时才能改为 `implemented`；`partial` 不能满足 M8 完成标准。

| Contract | 初始状态 | 计划实现阶段 | 最低验收证据 |
|---|---|---|---|
| GLOBAL-C01 | pending | M0/M10 | archive isolation guard + 全链搜索审计 |
| GLOBAL-C02 | pending | M1/M8/M10 | 当前唯一 V1 原子更新；无 V2/flag/兼容路径 |
| GLOBAL-C03 | pending | M1/M6 | COMID-only typed scope 与策略 tests |
| GLOBAL-C04 | pending | M2/M3/M6 | UTC storage、saved IANA timezone/season tests |
| GLOBAL-C05 | pending | M3/M6 | DST/重复当地时间 window identity fixtures |
| GLOBAL-C06 | pending | M3/M7/M9 | result/fragment/historical ref closure round-trip |
| GLOBAL-C07 | pending | M3/M4/M8/M9 | secret/raw/canonical/vector 日志与公开面负测试 |
| GLOBAL-C08 | pending | M0/M9 | 无归档前端依赖；只同步最终 frontend contract |
| GLOBAL-C09 | pending | M1/M7/M9 | 单一 typed fact 贯穿 tool/transcript/history/SSE |
| GLOBAL-C10 | pending | M9/M10 | M9 handoff 有 owner、约束、observable tests |
| GLOBAL-C11 | pending | M2/M8/M10 | 锁定依赖/迁移/路径编码；三平台按实记录 |
| M8-C01 | pending | M3/M5 | eligibility allowlist/denylist + ready projection |
| M8-C02 | pending | M7/M10 | stateless 与 memory-augmented E2E |
| M8-C03 | pending | M6 | 五种 strategy focused/PG fixtures |
| M8-C04 | pending | M1/M7/M9 | historical provenance DTO 与 history round-trip |
| M8-C05 | pending | M7/M10 | current observation conflict/gate priority fixture |
| M8-C06 | pending | M3/M5 | source allowlist、typed reader、source-first rebuild |
| M8-C07 | pending | M3/M6/M10 | canonical query-to-query + 多水体正确 ref E2E |
| M8-C08 | pending | M1/M6/M7 | typed preflight、hard filters、严格 0/0/0 |
| M8-C09 | pending | M2/M4/M6 | pgvector exact search、单 active generation/switch |
| M8-C10 | pending | M4/M8 | port/adapter/fake、privacy 与协议验证 |
| M8-C11 | pending | M5/M8 | durable job/lease/restart/atomic ready；run 非阻塞 |
| M8-C12 | pending | M7/M8 | 唯一 M5 producer、roster、每 session 最多 2 calls |
| M8-C13 | pending | M6/M7/M10 | empty/unavailable/invalid/cancel typed matrix |
| M8-C14 | pending | M6 | source revalidation、授权 cells、5/2/2/16 KiB limits |
| M8-C15 | pending | M7/M9 | 既有 typed audit/history/两层 SSE；无新 route |
| M8-C16 | pending | M7/M9/M10 | consumption links、conflict/cascade/rebuild 无悬空 |
| M8-A01 | pending | M3/M5 | eligible indexed、non-eligible never searchable |
| M8-A02 | pending | M1/M7/M9 | identity/version/score/confidence/provenance 可读 |
| M8-A03 | pending | M7/M10 | 当前观测冲突优先级 production fixture |
| M8-A04 | pending | M6/M7 | 五策略 filters/formula/tie-break/limits/0-0-0 |
| M8-A05 | pending | M3/M6/M10 | 两历史 runs + 多水体 query-to-query/hydration |
| M8-A06 | pending | M2/M4/M5 | migration/backfill/lease/restart/switch PG evidence |
| M8-A07 | pending | M4/M5/M6/M7 | typed failure/cancel 与 current reasoning continuity |
| M8-A08 | pending | M7/M8/M9 | route 不增、roster 分流、typed round-trip |
| M8-A09 | pending | M9/M10 | cleanup conflict/cascade/retired/rebuild/API-key isolation |
| M8-A10 | pending | M10 | fake E2E、focused/PG/full/audit 与外部边界实录 |

## 每个 Milestone 的完成记录模板

```text
Milestone N completed on: YYYY-MM-DD
Branch / base / HEAD:
Applicable contracts:
Production changes:
Tests added or changed:
Verification commands and exact results:
PostgreSQL database isolation / migration state:
Provider / platform / capacity evidence (pass, fail, skipped, not run):
Failure found and repair performed:
Docs synchronized:
Commit SHA:
Remaining risks / next-stage handoff:
```

禁止仅填写“tests pass”“done”或复制上一阶段数字。无法提供 production implementation、focused test 与阶段出口证据的复选框保持未勾选。
