# WaterAgents 跨 Milestone 延后契约与 MR 门禁（M2+）

更新日期：2026-09-08

本文档集中记录已经确认、但需由后续 Milestone 实现、细化或持续保持的功能约定。内容来自 M1/M2 详细设计、后端架构骨架和总 milestones 文档，是跨 Milestone 延后事项的统一登记处。

各 Milestone 的详细设计可以补充实现类型、API schema 和验收用例，但不得静默弱化本文档的硬约束。约定变更时，必须在同一个 MR 中同步更新本文档、所属 Milestone 设计/TODO、相关 API 文档和测试。

相关入口：

- [后端架构骨架](backend_architecture_skeleton.md)
- [总 Milestones](backend_refactor_milestones.md)
- [M2 详细设计](M2/m2_analysis_session_sse_design.md)
- [M2 Implementation TODO](M2/m2_analysis_session_sse_implementation_todo.md)

## 1. 使用方式与权威性

- 每条硬约束使用稳定 contract ID。后续补充新条目时只能增加新 ID，不得因排序变化重编号。
- MR 必须在描述中列出适用的 contract ID，并给出实现位置、测试证据和文档状态。
- 某条约束完成实现后不删除原条目；在“承接记录”中登记 MR、代码和测试，保留它作为后续回归约束。
- 如果约束改期、拆分、替换或取消，保留原 ID，在条目或承接记录中注明新归属、原因和替代 ID。
- 本文档与某个详细设计冲突时，MR 不能自行选择其中一个；必须先把冲突作为设计变更在同一 MR 中消除。

## 2. 跨 Milestone 硬约束

- **GLOBAL-C01 — 新旧边界**：新系统只以当前架构、Milestone 和功能设计为依据。`backend_bk/`、`frontend_bk/` 及旧契约文档不是需求、行为、数据源、兼容或测试依据。
- **GLOBAL-C02 — 内部演进**：M1–M9 是内部演进阶段。阶段性 request、DTO、fake adapter 和测试入口可在后续 Milestone 中直接收敛，不为中间状态建立兼容层。
- **GLOBAL-C03 — COMID 身份**：第一版业务身份只使用 COMID；只有出现非 COMID 水体的真实需求时才设计通用 `Waterbody`。
- **GLOBAL-C04 — 时间身份**：时间事实统一存 UTC。用户时间语义按 session 固化的 IANA timezone 解释，全局设置修改不得改变已有 session。
- **GLOBAL-C05 — 墙上时钟窗口**：窗口按当地墙上时钟推进。真实 DST transition 导致的 elapsed duration 变化合法；重复当地时间使用不同 UTC instant 作为唯一身份，不得互相覆盖。
- **GLOBAL-C06 — Evidence 闭包**：结果、归因和进入推理的事实必须能回溯到 evidence references；不得只把事实散落在 prompt 或日志中。
- **GLOBAL-C07 — Secret 边界**：API key、Authorization、Cookie 和其他敏感凭据不得进入 transcript、普通日志、公开错误或前端事件。
- **GLOBAL-C08 — 最终前端**：最终前端以当时确认的产品设计、OpenAPI 和 SSE 事件契约独立重构，不迁移旧前端字段、事件或交互契约。
- **GLOBAL-C09 — 单一事实模型**：同一 request/session/event/error/result 事实不得在 API、日志、transcript 和 workflow 中维护语义不同的重复模型；边界映射只负责可见性和序列化。
- **GLOBAL-C10 — 可验收延后**：延后事项必须同时写明归属、必须保留的约束和可观察验收方式，不能只写“以后完善”。

## 3. M2 已建立、后续 MR 不得破坏的基线

- **M2-C01 — 显式取消**：只有 cancel API 可以取消 session。SSE disconnect、页面刷新、网络抖动、代理超时或订阅者数量变为零都不改变 session 状态。
- **M2-C02 — 事件续传**：公开 lifecycle event 使用 session 内严格递增 sequence；首次订阅回放已有事件，重连通过 `Last-Event-ID` 只续传更大的 sequence。
- **M2-C03 — 唯一终态事件**：每个 session 最多提交一个 completed/failed/cancelled terminal event。`pending -> running` 只记录结构化日志，不新增公开 `session_started`。
- **M2-C04 — 原子转换**：公开状态转换与对应 lifecycle event 必须在同一 session 临界区原子提交；日志或外部 event sink 失败不得回滚权威状态/event。
- **M2-C05 — Cancel 收敛**：cancel 在提交 cancelled 后立即返回，通过 cancellation token 让 workflow 在阶段边界协作式收敛。cancel 后不得再启动或传播下游工作。
- **M2-C06 — 晚到结果**：cancel 后已经产生或无法及时中止的在途结果不得发往前端或下游；后续可以写入 cancelled session transcript，但不增加 `produced_after_cancel` 一类冗余字段。
- **M2-C07 — Keepalive**：SSE keepalive 是无 ID 的 comment，不占 sequence、不回放、不写 transcript，也不逐条写业务日志。
- **M2-C08 — 事件复用**：SSE、结构化 lifecycle log 和未来 transcript projection 复用同一个版本化、类型化 `LifecycleEvent`；不得新增第二套生命周期事实。
- **M2-C09 — 可观察性关联**：`session_id` 串联分析生命周期，`request_id` 定位 HTTP 请求，`error_id` 从公开错误回溯后端异常；公开错误不得包含 traceback 或原始异常文本。
- **M2-C10 — Demo 运行形态**：第一版 demo 只支持单进程、单 worker，但单 worker 内允许多个 session 并发且互相隔离。
- **M2-C11 — M2 内存态**：M2 的 session/event 保留到进程退出，不实现 TTL、LRU、容量上限或后台清理；持久化确认后的清理由 M3 承接。
- **M2-C12 — 无 fake 业务契约**：M2 fake workflow 只驱动生命周期，不生成临时水质、evidence 或 final-result schema；真实 completed payload 由 M6/M7 定型。
- **M2-C13 — Shutdown 区分**：服务 shutdown 不是用户 cancel，不发布伪造的 `session_cancelled`。持久化后的遗留 active session 由 M3 按 `service_interrupted` 处理。

## 4. 后续 MR 合规门禁

- **MR-G01 — 适用契约声明**：MR 描述必须列出 `Applicable contracts`。若判断无适用条目，写 `None` 并说明检索过的 Milestone/功能域，不能省略。
- **MR-G02 — 承接矩阵**：每个适用 contract ID 都要列出实现位置、测试名称/命令和文档变更；不能只引用提交或笼统写“已完成”。
- **MR-G03 — 设计先行**：功能级设计尚未定型时，MR 先更新详细设计和 TODO，再实现代码。不得用 `Any`、任意 `dict`、test-only production switch 或旧系统字段绕过未知契约。
- **MR-G04 — 单一 MR 同步**：如果代码改变 request、response、event、状态机、持久化、日志或 cleanup 语义，必须在同一个 MR 中同步 OpenAPI/schema、Markdown 文档、fake/stub、smoke 和测试。
- **MR-G05 — 禁止静默弱化**：无法满足硬约束时，MR 必须明确提出设计变更，更新本文档并解释取舍；不得仅放宽断言、删除测试或把失败路径改成静默降级。
- **MR-G06 — 端到端 contract sweep**：完成前从输入沿 validation、normalization、state transition、persistence、replay、logging、routing 和 downstream assembly 追踪一次，并搜索过时字段/事件/错误码。
- **MR-G07 — 真实生产路径**：验收必须调用真实 production entrypoint；测试 fake 只替换外部能力或时序，不得要求生产代码暴露 fake outcome、特殊 header 或隐藏 query 参数。
- **MR-G08 — 竞争与失败证据**：涉及 async、cancel、双写或 cleanup 的 MR 必须覆盖成功、失败、重复调用和关键竞态，使用可控 clock/gate，不依赖真实 sleep 证明正确性。
- **MR-G09 — Secret 与诊断**：新增数据路径必须同时检查公开错误、结构化日志、transcript 和事件。既要保留 session/request/error 等回溯 ID，也要证明 secret 和内部 traceback 不会越界。
- **MR-G10 — 边界与范围**：不得顺手修改旧归档、现有旧前端或无关代码。必须保留工作区中不属于本 MR 的已有改动，并在交付时说明未处理的无关变化。
- **MR-G11 — 跨平台**：新增运行入口、路径、时间、临时文件或进程控制时，必须验证 macOS、Windows、Linux 约束；不能把本机 POSIX 行为写成应用契约。
- **MR-G12 — 完成定义**：docs-only 设计 MR 只能标记“设计已确认”；只有实现、focused tests、相关 regression、smoke/CI 和承接记录齐全后，才能把 contract 标记为“已承接”。
- **MR-G13 — 保持简单**：不得为单一内存实现预造 repository/event-bus/DI framework，也不得为未进入当前 Milestone 的生产形态提前引入数据库、队列或兼容层。
- **MR-G14 — Deferred 更新**：发现新延后事项时，在 MR 合入前为其分配新 contract ID、明确目标 Milestone 和验收方式；不能只留在代码 TODO、review comment 或聊天记录中。

建议 MR 描述使用：

```text
Applicable contracts:
- M3-C09
- M3-C12

Contract evidence:
| Contract | Status | Implementation | Tests | Docs |
|---|---|---|---|---|
| M3-C09 | implemented | path:symbol | test name / command | doc section |
| M3-C12 | partial | path:symbol | test name / command | remaining item + target MR |

Contract changes:
- None
```

`partial` 不能被当作 Milestone 完成；必须写清剩余内容、风险和新的承接位置。

## 5. M3：Transcript Store + History APIs

### 必须实现

- **M3-C01 — Transcript 事实源**：提供 `LiveTranscriptBuffer`、持久化 `TranscriptStore` 和 append-only item 写入；数据库中的 transcript 是历史权威来源。
- **M3-C02 — Content 版本**：transcript content 使用 `content_type + content_schema_version` 版本化 envelope。
- **M3-C03 — Session snapshot 持久化**：保存 session 创建时的 analysis timezone、time resolution 及其 source，确保历史窗口语义不受全局设置变化影响。
- **M3-C04 — History API**：提供 session 历史列表和详情；详情返回 session、可展开的完整 transcript timeline，以及“存在时”的已持久化 final-result projection。M6 定型 final-result schema 前不得生成 fake 业务摘要。
- **M3-C05 — 大内容**：`analysis_detail` 支持完整可折叠上下文；大 payload 只内联 preview，完整内容通过 reference 按需加载。
- **M3-C06 — 写前脱敏**：transcript 写入前移除 API key、Authorization、Cookie 等 secret；redaction 必须在持久化边界执行，不能只依赖调用方自律。
- **M3-C07 — History 核心字段**：M3 历史列表先返回当前阶段真实拥有的 session_id、status、created_at、updated_at、query 和 configuration snapshot。不得为尚未产生的 target/mode/parameters/final confidence 伪造值或自由形态 placeholder；这些字段分别由 M4/M6 扩展。
- **M3-C08 — 生命周期持久化**：持久化 session、request、LifecycleEvent、errors，以及存在时使用 versioned content envelope 保存的 final result；历史 timeline 保留 event schema version、sequence 和原始发生时间。M3 不猜测 M6 final-result payload。
- **M3-C09 — 幂等创建**：实现持久化 `Idempotency-Key`。相同 key + 相同规范化请求返回原 session；相同 key + 不同请求返回 409。不能只用内存 map 声称幂等。
- **M3-C10 — Cancelled transcript**：cancelled session 可以继续追加已经产生的在途结果和清理事实，但不得向前端/下游传播，也不增加 after-cancel 布尔字段。
- **M3-C11 — Persistence-error 终态**：分析成功但最终持久化失败时进入 `completed_with_persistence_error`，发布 `session_completed` 并携带安全 warning/error_id；不得误报 `session_failed`。
- **M3-C12 — Live-state 清理门槛**：只有 `persistence_confirmed + workflow_quiesced + grace period elapsed` 同时满足时才异步清理 live state；`persistence_confirmed` 必须来自本 session 所有必需且适用的 session/transcript/result durable write 成功回调，不能在仅入队、创建 task 或写入 live buffer 后提前标记；持久化失败不得清理。
- **M3-C13 — 清理幂等**：cleanup callback 必须幂等、可重试；cleanup 失败不改变业务终态，并通过 session/error ID 可排查。
- **M3-C14 — 启动遗留处理**：启动时发现持久化的 pending/running session，归档为 failed、错误码 `service_interrupted`，第一版不自动恢复执行。
- **M3-C15 — Event replay 边界**：明确 live SSE replay 与历史 timeline 的映射。进程内 active stream 继续按 sequence；重启后不得伪造可继续执行的 active stream。

### 验收重点

- **M3-A01**：fake session 的请求、事件、错误和结果写入 transcript，旧 item 不被覆盖。
- **M3-A02**：历史记录稳定反序列化，content/event 版本、时区、sequence、置信度和 provenance 不丢失。
- **M3-A03**：transcript 中无 secret，大内容可通过 reference 追溯。
- **M3-A04**：幂等 key 同请求返回同 session，不同请求稳定返回 409，服务重启后仍成立。
- **M3-A05**：cancelled 晚到内容可审计但不出现在 SSE/downstream；没有 after-cancel 冗余字段。
- **M3-A06**：持久化成功/失败、workflow 未收敛、grace 未到和 cleanup 重试均有独立测试。
- **M3-A07**：`completed_with_persistence_error` 仍可交付当前结果，并可用 error_id 回溯持久化失败。

## 6. M4：Query Understanding + Hydrology Topology

### 必须实现

- **M4-C01 — Final request/context**：定型 analysis request 和 `AnalysisContext`，包含 query、位置/COMID、参数、日期范围、mode、memory mode 和时间粒度；直接替换阶段性 M2 request，不建兼容层。
- **M4-C02 — 日期语义**：第一版只接受日期范围，结束日期包含整天；按 session 固化的 IANA timezone 解释后转换为 UTC。
- **M4-C03 — 自动粒度**：定义可测试的自动时间粒度规则；只有该规则实际选择粒度时才使用 `time_resolution_source=auto`。
- **M4-C04 — 参数身份**：参数使用 `ParameterKey(code_system, code)`，第一版默认 `code_system=usgs`。
- **M4-C05 — 位置与模式**：解析位置到经纬度和目标 COMID，并根据时间范围判断 `gap_filling / prediction / mixed`。
- **M4-C06 — Topology**：构建 `HydrologyGraph`，标记 target、inlet、active/excluded，并确定后续 Agent 的拓扑执行顺序。
- **M4-C07 — Typed events**：定义 `query_understood` 和 `topology_ready` 的 typed payload，继续复用 M2 LifecycleEvent envelope、sequence 和 replay 语义。
- **M4-C08 — History query 扩展**：在 M3 history DTO 上增加真实解析得到的 target COMID/location、time range、mode 和 parameters，并从持久化的版本化 AnalysisContext 投影，不从 query 文本临时重算。

### 验收重点

- **M4-A01**：固定 query 在 fake geocoding/NLDI adapter 下稳定生成 `AnalysisContext` 和 topology。
- **M4-A02**：日期边界、DST、resolution source 和 mixed mode 拆分可通过测试重现。
- **M4-A03**：结果显示实际解析的 ParameterKey，不退回仅按裸 code 识别参数。
- **M4-A04**：新增事件与 M2 created/terminal events 共享同一 sequence，重连无遗漏和重复。

## 7. M5：Evidence Tools + EvidenceBundle

### 必须实现

- **M5-C01 — Source DTO**：为 observation、weather、forecast、flow、facility 和 upstream result 等数据源分别定义标准化 Request/Record DTO。
- **M5-C02 — 构造权**：adapter 只返回 source record；Evidence Tool 负责构造 `EvidenceFragment`、quality、policy、RAG eligibility 和 provenance。
- **M5-C03 — Collection status**：区分 `available / empty / unavailable / invalid`，不得把“查无数据”与“本次取数失败”合并。
- **M5-C04 — 无效与可疑值**：可疑观测保留原值并降权；明确无效观测可保留为 evidence，但不能作为 observed quantified result。
- **M5-C05 — Evidence identity**：evidence ID 只要求 session 内唯一；跨 session 源记录通过 provenance 识别。
- **M5-C06 — Bundle 闭包**：EvidenceBundle 按 COMID、ParameterKey 和时间窗口组织，并保持 evidence reference 闭包。
- **M5-C07 — Tool registry/aggregation**：提供 mandatory/optional evidence tool registry 和确定性 aggregation tool，至少支持 `mean / median / min / max / sum / p90 / latest`。
- **M5-C08 — Unit 系统**：维护 `ParameterKey -> canonical_unit`，保留源单位、转换结果和换算 provenance；量纲校验和换算只能调用确定性库/API，未知换算不得由 Agent 猜测。
- **M5-C09 — Event 容量**：新增 evidence event 继续使用 LifecycleEvent；大 tool/evidence payload 使用 preview + reference，不把无界 raw data 放入 SSE replay buffer。
- **M5-C10 — 延后 producer**：M5 可以定型 upstream-result/RAG evidence tool 的 request/result 槽位和 unavailable 语义，但真实 upstream producer 由 M7 接入，真实 transcript-RAG producer 由 M8 接入；不得在 M5 production path 中用隐藏 fake 冒充已实现能力。

### 验收重点

- **M5-A01**：每个 evidence tool 通过 fake adapter 返回结构完整、可追溯的 EvidenceFragment。
- **M5-A02**：聚合和单位换算完全由确定性代码执行，原值、单位和 provenance 不丢失。
- **M5-A03**：tool call/result 可写入 transcript，公开 event 不泄露 secret 或无界 payload。

## 8. M6：Single-Agent Reasoning

### 必须实现

- **M6-C01 — LLM 契约**：定型 LLM request、raw/structured response DTO、EvidencePolicy、SufficiencyPolicy 和 structured output parser。
- **M6-C02 — Tool policy**：Agent 必须调用 mandatory evidence tools，并可根据任务自行判断是否调用 optional tools。
- **M6-C03 — 窗口完整性**：每个计划时间窗都必须生成结果，不得因证据缺失而省略 point。
- **M6-C04 — 三态输出**：证据充分时输出 quantified；只能判断方向时输出 directional；完全缺证据时输出带 reason code/evidence refs 的 indeterminate。
- **M6-C05 — Confidence**：Domain 只硬校验 `[0,1]`；评分、聚合和最终判断规则属于 reasoning 层，并保留 Agent 自主判断。
- **M6-C06 — 冲突观测**：同窗多个有效站点冲突时引用全部相关观测并解释；加权/折中/综合值为 inferred，只有原样采用的有效读数为 observed。
- **M6-C07 — 单位职责**：Agent 可选择目标单位，但实际换算必须调用确定性 unit tool。
- **M6-C08 — Attribution**：attribution factor 引用 evidence，并标记 `basis: observed | correlated | inferred | hypothesized`。
- **M6-C09 — 当前证据优先**：当前 session 的有效观测优先于历史推理。
- **M6-C10 — Transcript**：prompt、raw LLM output、structured intermediate decision 和 final result 可写入 transcript。
- **M6-C11 — Final event**：定型 `session_completed` 的真实 inference-result payload，替换 M2 空 lifecycle payload，不保留 fake result 兼容结构。
- **M6-C12 — Correlation**：Agent/tool/operation 使用稳定 ID 和 stage name，并以 session_id 为根关联 SSE、日志与 transcript。
- **M6-C13 — History result 扩展**：在 M3 history/detail DTO 上增加真实 final result summary 和 final confidence；值必须来自持久化 result，不在 history 查询时重新推理。

### 验收重点

- **M6-A01**：fake EvidenceBundle 跑出完整 InferenceResult，三种状态都有正反例。
- **M6-A02**：每个窗口有 source、confidence 和 evidence refs，结果与归因引用闭包成立。
- **M6-A03**：冲突观测、无证据和未知单位不会被静默猜测或丢弃。
- **M6-A04**：completed event、日志和 transcript 中的 result identity/版本一致。

## 9. M7：Topology-Driven Multi-Agent Workflow

### 必须实现

- **M7-C01 — DAG 调度**：按 `HydrologyGraph` DAG 调度 upstream -> downstream Agent，禁止无约束 Agent 互聊。
- **M7-C02 — 上游证据**：下游接收 upstream result、confidence、evidence summary 和 uncertainty，并将其作为 current-session evidence。
- **M7-C03 — 模式传播**：支持 `gap_filling / prediction / mixed` 的拓扑传播。
- **M7-C04 — Cancel 边界**：在每个真实阶段边界检查 cancellation token；cancel 后不得启动新 agent/tool，也不得传播晚到结果。
- **M7-C05 — 在途结果**：无法强杀的 LLM/HTTP 调用可自然返回；其结果只进入 cancelled transcript/日志，不触发前端 result 或下游传播。
- **M7-C06 — Workflow 收敛**：记录 `cancel_accepted` 和最终 `workflow_quiesced`；M3 live-state cleanup 必须等待 quiesced。
- **M7-C07 — 并发策略**：基于真实 topology/workload 定义并发上限、排队、资源配额或明确保持无限制；不能让 M2 的“未预设”变成无意识生产默认。
- **M7-C08 — Topology final result**：将 M6 的单 Agent completed payload 收敛为拓扑 workflow 的最终结果/摘要；不得同时保留两套含义不同的 session final result。

### 验收重点

- **M7-A01**：fake topology 稳定执行至少 3 个节点，Agent start/completed 事件顺序符合拓扑。
- **M7-A02**：下游 EvidenceBundle 可见上游证据，但不覆盖下游当前有效观测。
- **M7-A03**：在各关键阶段触发 cancel 后没有新下游调用或前端 result，且最终出现 workflow_quiesced。
- **M7-A04**：已完成 Agent 的结果和 transcript item 保留，晚到结果只在 cancelled transcript 可审计。

## 10. M8：Memory/RAG from Transcript

### 必须实现

- **M8-C01 — Projection**：从 `rag_eligible=true` 的 transcript item 生成 RAG projection/index。
- **M8-C02 — Memory mode**：支持 stateless 和 memory_augmented；stateless 不使用跨 session memory，但保留当前 session 内上游结果。
- **M8-C03 — 固定检索策略**：至少支持 COMID + parameter + time window、nearby COMID + parameter、parameter + season、semantic query 和 topology neighborhood + parameter。
- **M8-C04 — Historical provenance**：historical inference 进入当前 transcript/evidence，保留原 session、原 confidence、content schema version、RAG score 和 matched_by。
- **M8-C05 — 当前观测优先**：历史 inference 不得覆盖当前有效观测；低置信度历史结果可以使用，但必须保留原置信度并降权。

### 验收重点

- **M8-A01**：eligible item 进入 index，非 eligible item 不会被检索。
- **M8-A02**：检索结果的身份、版本、分数、置信度和 provenance 可审计。
- **M8-A03**：当前观测与历史记忆冲突时，优先级规则由测试证明。

## 11. M9：Settings/Keys/Admin + Demo Closure

### 必须实现

- **M9-C01 — Settings/keys/admin**：定型 settings、provider/model、key store 和 admin cleanup API，并将 demo 所需 fake adapter 替换为真实 adapter。
- **M9-C02 — 动态时区**：动态修改默认 IANA analysis timezone 只影响新 session，不改变已有 snapshot。
- **M9-C03 — Secret DTO**：定型 secret 与 masked status DTO，支持 API key 保存、清除和 masked 展示。
- **M9-C04 — 自定义单位**：确定性 unit tool 解析和校验量纲，Agent 判断参数适用性；默认只生成 session override。
- **M9-C05 — 全局单位确认**：写入全局目标单位表必须由用户显式确认，并保存版本记录。
- **M9-C06 — Admin cleanup**：可以清理 session history、transcript、RAG index、derived memory 和 evidence cache，但不得删除 API keys；与 M3 live-state cleanup 使用一致的 ownership/幂等规则。
- **M9-C07 — 最终契约校对**：完成最终 OpenAPI、SSE 文档、前端消费代码、端到端 smoke 和启动说明的一致性 sweep。
- **M9-C08 — 前端重构**：前端以最终契约独立重构，不读取或迁移旧前端组件、字段或事件。
- **M9-C09 — Demo 单 worker**：启动说明明确 demo 单进程、单 worker；不得通过未实现的共享状态暗示支持多 worker。

### 验收重点

- **M9-A01**：settings/key/admin API 独立测试通过，secret 不出现在公开响应、日志、transcript 或事件中。
- **M9-A02**：清理增长型数据后 API keys 仍存在，已固化 session 语义不变。
- **M9-A03**：端到端 session 从创建跑到完成或 cancel，并可查询历史详情。
- **M9-A04**：demo 输出三态结果之一、EvidenceChain、confidence 和 AttributionSummary。
- **M9-A05**：最终前端、OpenAPI、SSE raw frame、错误码、smoke 和后端实现使用同一字段/事件名称。
- **M9-A06**：最终 history 列表包含架构要求的 session identity/status/time、query、target COMID/location、time range/mode/parameters 和 final confidence，所有字段来自已持久化的阶段产物。

## 12. Future / 第二版候选

- **FUTURE-C01 — 任意时分**：开放任意时分输入；窗口从请求起点按当地墙上时钟滚动，首尾允许 partial coverage。
- **FUTURE-C02 — 自定义窗口 DST**：自定义时间窗口仍以真实 IANA transition 解释 elapsed duration，不使用宽泛小时容差。
- **FUTURE-C03 — 非 COMID 水体**：只有出现真实需求时设计通用 Waterbody。
- **FUTURE-C04 — Evaluation**：在有检测站水体上做 holdout，计算推测误差，比较 evidence policy、prompt、模型和聚合策略。
- **FUTURE-C05 — Replay/Reproduce**：根据 transcript snapshot 重放历史分析，对比 replay 与 original result。
- **FUTURE-C06 — Advanced Agent Interaction**：只允许拓扑驱动的上下游补充问答，不开放无约束 Agent 聊天。
- **FUTURE-C07 — Storage Optimization**：大 raw data 拆到 object storage，transcript item 保留 reference、summary 和 hash。
- **FUTURE-C08 — Frontend History**：设计历史 session、结果卡片、transcript timeline 和 EvidenceChain 展开体验。
- **FUTURE-C09 — Multi-worker**：设计共享 session store、pub/sub、跨进程 cancel 和一致性策略；不能只依赖 sticky routing。
- **FUTURE-C10 — Retention/backpressure**：设计 session/event retention、慢订阅者 backpressure、磁盘增长和容量告警。
- **FUTURE-C11 — Recovery**：设计 graceful drain、resume/replay 和 crash recovery。
- **FUTURE-C12 — Security**：设计认证、授权、租户隔离、限流和审计策略。
- **FUTURE-C13 — Observability**：按需要接入 W3C trace context、metrics 和外部 tracing sink，继续保留 session/request/error 关联。

## 13. 承接记录

实现 MR 在这里追加记录，不删除上面的 contract：

| Contract ID | Milestone | Status | Implementation/MR | Test evidence | Notes |
|---|---|---|---|---|---|
| — | — | — | — | — | 暂无满足全部验收条件的记录；M2 本地实现证据见下方，三平台 CI 待通过。 |

允许的 Status：

```text
implemented
superseded
cancelled
```

`partial` 只能出现在 MR 描述中，不能进入最终承接记录。若一个 MR 只实现部分内容，应保持 contract 未承接，或先用新的稳定 ID 拆分约束。

### M2 待验收索引

本节只索引本地实现证据，不属于上表的最终承接记录。当前分支为 `codex/m2-analysis-session-sse`；Milestone 8 基于本地 Milestone 7 提交 `b3cd6d1`，正式启动审查修复基于 `48decb0`，日志边界修复基于 `b9cd8f0`。没有发布远端 MR，也未取得 Ubuntu/macOS/Windows + Python 3.11 的实际 CI 结果，因此 `M2-C01`–`M2-C13` 保持未最终承接。

| 范围 | 实现、测试与文档证据 | 剩余验收 |
|---|---|---|
| `GLOBAL-C01`、`GLOBAL-C02`、`GLOBAL-C04`、`GLOBAL-C07`、`GLOBAL-C09`、`GLOBAL-C10`；`M2-C01`–`M2-C13` | [M2 最终本地 contract evidence](M2/m2_analysis_session_sse_implementation_todo.md#m2-contract-evidence)、[正式启动审查修复](M2/m2_analysis_session_sse_implementation_todo.md#milestone-8-审查修复记录2026-09-08)、[日志边界修复](M2/m2_analysis_session_sse_implementation_todo.md#milestone-8-日志边界修复记录2026-09-09)、[详细设计](M2/m2_analysis_session_sse_design.md)、[API/runbook](M2/m2_analysis_session_sse_api.md)。593 项回归、health 和 completed/cancel 真实 Uvicorn smoke 通过；包含架构门禁、单 worker、活动 SSE 关闭及info/trace 下完整服务输出的请求/异常脱敏边界及流中断后 replay 回归。 | 三平台完整 tests 和两项 smoke 的实际 CI 结果；通过后在最终承接表追加实现位置、测试结果和提交/MR 引用。 |
| M3–M9 / Future 接力 | [后续接力入口与验收](M2/m2_analysis_session_sse_implementation_todo.md#后续接力入口与验收)给出当前代码入口、保留约束与可观察验收。 | 各所属 Milestone 按原 ID 实现；本次只登记接力，不将未来能力计为已实现。 |

未新增、拆分、替换或取消 contract。CI 待验收仍属于 M2 的 Milestone 7/8，不转移为新的 deferred contract。

## 14. 维护规则

- 新增条目时选择所属 section 的下一个未使用 ID，并补充对应验收项。
- 所属 Milestone 设计阶段必须把适用 ID 写入详细设计和 Implementation TODO。
- 所属 Milestone 实现阶段必须在同一 MR 更新承接记录，并提供可复现测试证据。
- contract 发生语义变化时，同步更新架构骨架、总 milestones、所属详细设计、API 文档和测试；只改其中一处视为未完成。
- 不用删除测试、放宽 schema 或添加兼容 fallback 的方式“解决”契约冲突。
- 本文档不引用旧前后端归档来补全需求。
