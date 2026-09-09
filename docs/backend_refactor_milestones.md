# WaterAgents 后端重构 Milestones

生成日期：2026-09-06

本文档是给后续设计/实现 agent 阅读的 milestone 引入文档。它基于 `docs/backend_architecture_skeleton.md`，说明第一版 demo 应该按什么顺序推进，以及每个 milestone 完成后如何作为开发接力检查点独立自测验收。

本文档只做中等粒度拆分，不展开每个功能的内部设计。每个 milestone 都应该在完成后有可运行、可观察、可测试的结果；这些结果用于后续 agent 接力和问题定位，不构成用户正式测试入口，也不要求支持任何中间态兼容。用户正式上手测试发生在 M1-M9 全部完成之后。

如果某个 milestone 依赖后续能力，可以先使用 fake/mock adapter。fake/mock 只需要帮助验证本阶段边界和数据形态，不代表最终外部契约；后续 milestone 可以在保持总体架构方向的前提下继续收敛和调整。

新后端只实现 session + SSE 协议。新后端的需求和契约只以架构文档、milestone 文档和后续新功能设计为准。`backend_bk/`、`frontend_bk/` 和 `docs/backend_refactor_contract.md` 完全不属于新后端上下文，不能成为需求来源、行为参考、数据源清单、兼容目标或测试 oracle。

## 0. 仓库目录约定

- `backend/` 是新后端实现目录，也是所有 milestone 的开发和验收目标。
- `backend_bk/` 是与新后端完全独立的旧后端归档，新后端完成后整体丢弃。
- `frontend_bk/` 是待重构前端的历史归档，不是新后端或未来新前端的设计输入。
- 后续 Agent 设计、实现、测试、评审或验收 `backend/` 时，不得读取、导入、复制、适配、枚举或测试 `backend_bk/` 与 `frontend_bk/` 的代码和契约。未来前端重构基于当时确认的产品设计和新后端契约独立进行。

## 1. Milestone 原则

1. 每个 milestone 都要能独立自测验收。
   - 可以通过 API、SSE event、数据库记录、单元测试或最小 demo 验收。
   - 不要求最终推理准确，但要求该阶段的架构边界和数据形态成立。
   - 这是开发过程中的接力检查点，不是用户正式测试版本。
   - 中间结果不需要面向前端、用户或其他 milestone 保持长期兼容。

2. 每个 milestone 都要留下可继续接力的开发边界。
   - 后续 agent 应该能基于上一阶段的产物继续实现，不需要重推架构。
   - 如果后续实现发现阶段性 DTO、fake adapter 或测试入口需要调整，可以直接更新，但必须同步更新相关文档和测试。

3. MR 范围不必严格等同于 milestone 边界。
   - Milestone 是工作组织和接力参考，不是 MR 的硬性改动范围。
   - 如果设计、实现或测试时发现必须跨 milestone 才能让功能闭环或验证成立，可以在同一个 MR 中超出原 milestone 范围。
   - 跨范围改动需要在 MR 说明或相关文档中记录原因、影响范围和验证方式，方便后续 agent 接力。
   - 这个规则只允许在新后端设计范围内跨 milestone 工作，不允许把归档目录中的代码或契约引入新后端。

4. 第一版优先端到端跑通。
   - 复杂 replay、accuracy benchmark、暂停/恢复、大对象外置存储先后置。

5. 可以使用 fake/mock 实现通过验收。
   - 例如 LLM、NLDI、NWIS、NOAA、EPA、RAG 都可以先用 fake adapter。
   - 但 fake adapter 产出的结构必须和本阶段已经定义的 port 一致；若后续 milestone 收敛 port，必须同步更新相关 fake adapter、文档和测试。

6. 延后设计必须显式登记。
   - 如果某个 port DTO、policy 或产品接口缺少真实上下文，不在早期 milestone 用 `Any` 或猜测结构强行定型。
   - 延后项必须记录所属 milestone、必须保留的约束和阶段自测入口。

7. macOS、Windows 和 Linux 是同等支持的运行平台。
   - 生产代码和默认配置不得依赖 POSIX 路径、shell、`/tmp` 或 macOS 专用能力。
   - 每个 milestone 的安装、启动和测试入口应保持平台无关；平台专用 adapter 必须隔离并提供等价实现或明确能力检测。

## 2. 第一版 Milestone 总览

```text
M1  Project shell + domain skeleton
M2  Analysis session + SSE shell
M3  Transcript store + history APIs
M4  Query understanding + topology
M5  Evidence tools + EvidenceBundle
M6  Single-agent reasoning
M7  Topology-driven multi-agent workflow
M8  Memory/RAG from transcript
M9  Settings/keys/admin + demo closure
```

每个 milestone 都可以独立自测验收；后面的 milestone 可以替换前面 fake adapter 为真实 adapter，也可以收敛前面为接力而保留的阶段性接口。第一版真正面向用户测试的状态是 M1-M9 全部完成之后。

## 3. M1: Project Shell + Domain Skeleton

详细设计与执行清单：

- [M1 详细设计](M1/m1_project_shell_domain_design.md)
- [M1 Implementation TODO](M1/m1_project_shell_domain_implementation_todo.md)

目标：

- 在 `backend/` 中建立新后端目录骨架。
- 固定核心领域对象和层间依赖方向。
- 让后续 agent 有稳定的类型和模块入口。

主要产物：

- `domain` 层基础对象：
  - `RiverSegment`
  - `MonitoringStation`
  - `ParameterKey`
  - `WaterQualityParameter`
  - `TimeRange`
  - `TimeResolution`
  - `TimeResolutionSource`
  - `TimeSegment`
  - `InferenceMode`
  - `CollectionStatus`
  - `EvidenceFragment`
  - `EvidenceBundle`
  - `EvidenceReference`
  - `ParameterTimeSeries`
  - `TimeWindowResult`
  - `OutcomeStatus`
  - `InferenceResult`
  - `AttributionSummary`
  - `AttributionBasis`
  - `AnalysisDetail`
  - `EvidenceChain`
  - `DataQualityFlag`
- 按 IANA timezone 和本地墙上时钟切分窗口的 `build_time_windows`。
- `ports` 层接口占位。
- 基础配置和应用启动入口。
- macOS、Windows、Linux 通用的安装、启动和测试入口。

阶段自测验收：

- 后端能启动。
- Domain 对象能通过单元测试构造、序列化、反序列化。
- `TimeResolution` 只接受 `6h / 12h / 24h`，默认 `12h`。
- `TimeWindowResult` 能表达 `quantified / directional / indeterminate`，其中 quantified 可附带风险/趋势。
- 默认 `analysis_timezone=America/Chicago`，DST 窗口、UTC 身份和重复本地时间测试通过。
- 每个计划窗口都有结果；完全缺证据时使用带 reason code 和 evidence refs 的 indeterminate。
- 测试能证明 Domain 层不依赖 FastAPI、数据库、LLM provider 或具体外部 API。
- project shell 不依赖操作系统自带 IANA timezone 数据，并且不包含平台专用路径或启动逻辑。
- macOS、Windows、Linux 的 CI 或等效干净环境均能完成安装、测试和 health smoke test。

## 4. M2: Analysis Session + SSE Shell

详细设计、API 文档与执行清单：

- [M2 详细设计](M2/m2_analysis_session_sse_design.md)
- [M2 API 与排障指南](M2/m2_analysis_session_sse_api.md)
- [M2 Implementation TODO](M2/m2_analysis_session_sse_implementation_todo.md)

目标：

- 跑通 session + SSE 的外部协议骨架。
- 即使没有真实水文推理，也能创建 session、订阅事件、cancel session。

主要产物：

- `POST /api/analysis/sessions`
- `GET /api/analysis/sessions/{session_id}/events`
- `POST /api/analysis/sessions/{session_id}/cancel`
- session 状态机：
  - `pending`
  - `running`
  - `completed`
  - `failed`
  - `cancelled`
  - `completed_with_persistence_error`
- 最小 SSE event：
  - `session_created`
  - `session_completed`
  - `session_failed`
  - `session_cancelled`
- session snapshot 固化：
  - `analysis_timezone`
  - `time_resolution`
  - `time_resolution_source`

阶段自测验收：

- 创建 session 后立即返回 HTTP 201、`session_id` 和 pending snapshot。
- 首次 SSE 订阅能回放 `session_created`；`Last-Event-ID` 重连无遗漏和重复。
- SSE disconnect 不取消 session，多订阅者互不影响。
- cancel API 立即、幂等地把 active session 标记为 `cancelled`，并在终态竞争中只产生一个 terminal event。
- cancel 后 session 不再向前端或下游 workflow 传播，workflow 后续进入 quiesced。
- 可以用 fake workflow 模拟 completed/failed/cancelled 三种终态。
- HTTP/SSE 错误可以通过 request/session/error ID 回溯结构化日志。
- M2 明确使用单进程、单 worker，但允许多个 session 并发且互相隔离。

## 5. M3: Transcript Store + History APIs

目标：

- 把 transcript 作为历史事实源跑通。
- 支持历史 session 列表和详情查询。

主要产物：

- `LiveTranscriptBuffer`
- `TranscriptStore`
- append-only transcript item 写入能力
- session、request、LifecycleEvent、error 和 final result 的持久化
- 持久化 `Idempotency-Key`
- persistence-confirmed/workflow-quiesced/grace-period 驱动的 live-state cleanup
- `GET /api/analysis/sessions`
- `GET /api/analysis/sessions/{session_id}`
- transcript item 基础字段：
  - `session_id`
  - `item_id`
  - `type`
  - `tags`
  - `timestamp`
  - `content`
  - `rag_eligible`
  - `provenance`
  - `confidence`

阶段自测验收：

- fake session 的请求、事件、结果能写入 transcript。
- 历史列表能返回：
  - `session_id`
  - `status`
  - `created_at`
  - `updated_at`
  - `query`
  - `configuration` snapshot
- `target_comid/location/time_range/mode/parameters` 由 M4 基于持久化 AnalysisContext 扩展；`final_confidence` 由 M6 基于持久化 result 扩展。M3 不为尚未产生的字段伪造 placeholder。
- 历史详情返回 session、可展开的 transcript timeline，以及存在时的已持久化 final-result projection；M6 定型 final-result schema 前不生成 fake 业务摘要。
- transcript 写入保持 append-only，不修改旧 item。
- API keys 不进入 transcript 内容。
- transcript content 使用 `content_type + content_schema_version` 版本化 envelope。
- session 历史固化分析时区，后续全局设置变化不改变历史窗口语义。
- transcript 写入前强制移除 API key、Authorization、Cookie 等 secret。
- analysis detail 可以引用完整 tool/prompt/raw output timeline；大 payload 采用预览加引用，按需加载。
- 相同幂等 key 和相同规范化请求在服务重启后仍返回原 session；相同 key 搭配不同请求返回 409。
- cancelled session 的晚到内容可以写 transcript，但不再进入 SSE 或下游，也不增加 after-cancel 冗余字段。
- 分析成功但持久化失败时进入 `completed_with_persistence_error`，发布带安全 warning/error_id 的 `session_completed`。
- live state 只有在持久化确认、workflow quiesced 和 grace period 全部满足后才清理；cleanup 幂等且失败不修改业务终态。
- 启动时发现遗留 pending/running session，将其归档为 failed/service_interrupted，不自动恢复执行。

## 6. M4: Query Understanding + Hydrology Topology

目标：

- 将用户请求转换成标准 `AnalysisContext`。
- 构建目标 COMID 的水流拓扑，并确定 agent 执行顺序。

主要产物：

- `PrepareAnalysisContext`
- `LocationResolver`
- `ParameterResolver`
- `BuildHydrologyTopology`
- `HydrologyGraph`
- `topology_ready` SSE event

主要能力：

- 解析位置到经纬度和目标 COMID。
- 将用户参数描述语义匹配到 USGS parameter code。
- 根据时间范围判断 `gap_filling / prediction / mixed`。
- 接收 `memory_mode: stateless | memory_augmented`。
- 接收 `time_resolution: 6h | 12h | 24h`。
- 输出 `time_resolution_source: frontend | default | auto`。
- 第一版只接受日期范围，结束日期包含整天；按 session 的 IANA timezone 解释后转换为 UTC。
- 参数解析结果使用 `ParameterKey(code_system, code)`，第一版默认 `code_system=usgs`。
- 构建上游拓扑，找到可用检测站和 agent 顺序。

阶段自测验收：

- 使用 fake NLDI/geocoding adapter 时，固定 query 可以稳定产出 `AnalysisContext`。
- `params_used` 能显示实际映射到的参数。
- 日期边界、DST 和 resolution source 能稳定写入 `AnalysisContext`。
- mixed 时间范围能拆出历史段和预测段。
- topology 结果能标记 target、inlet、active/excluded。
- SSE 能发送 `query_understood` 和 `topology_ready`。

## 7. M5: Evidence Tools + EvidenceBundle

目标：

- 建立统一 evidence tool 体系。
- 让 agent 可以通过 mandatory/optional tool 获取结构化证据。

主要产物：

- Evidence tool registry
- `EvidenceBundle`
- 各真实 adapter 的标准化 source record DTO；Evidence Tool 负责把 record 转成 `EvidenceFragment`。
- `collection_status: available | empty | unavailable | invalid`，明确区分“查无数据”和“本次取数失败”。
- mandatory evidence tools：
  - COMID/location/topology evidence
  - station observation evidence
  - discharge/flow evidence
  - historical weather evidence
  - forecast weather evidence
  - upstream current-session result evidence
  - transcript RAG evidence，当 `memory_mode=memory_augmented`
- optional evidence tools：
  - EPA facility evidence
  - 更细的历史 transcript 检索
  - 参数相关补充证据
- aggregation tool：
  - `mean`
  - `median`
  - `min`
  - `max`
  - `sum`
  - `p90`
  - `latest`
- unit registry/tool：
  - `ParameterKey -> canonical_unit` 映射表
  - 源单位、别名和转换 provenance
  - 可调用确定性 Python 库或 API 执行量纲校验与换算
  - 未知换算不得由 Agent 猜测
- M5 可以定型 upstream-result/RAG evidence tool 的槽位和 unavailable 语义；真实 upstream producer 由 M7 接入，真实 transcript-RAG producer 由 M8 接入，不在 M5 production path 中用 hidden fake 冒充完成。

阶段自测验收：

- 每个 evidence tool 都能通过 fake adapter 返回 `EvidenceFragment`。
- 每条 evidence 都包含：
  - `evidence_id`
  - `source`
  - `target_comid`
  - `location`
  - `time_range`
  - `data_type`
  - `value_or_summary`
  - `quality_flags`
  - `confidence_hint`
  - `provenance`
  - `rag_eligible`
- `EvidenceBundle` 能按 COMID、参数、时间窗口组织 evidence。
- 可疑观测原样保留并降权；明确无效观测保留为 evidence，但不能作为 observed quantified result。
- aggregation tool 能对 fake raw series 输出 `6h / 12h / 24h` 聚合结果。
- tool call 和 tool result 能写入 transcript。

## 8. M6: Single-Agent Reasoning

目标：

- 跑通单个 river segment agent 的推理闭环。
- 先不要求多 agent 拓扑传播。

主要产物：

- `RiverSegmentAgent`
- `EvidencePolicy`
- `SufficiencyPolicy`
- `PromptBuilder`
- `StructuredOutputParser`
- `InferenceResult`
- `agent_started` / `evidence_collected` / `agent_completed` SSE event

主要能力：

- agent 接收明确时间上下文：
  - `time_range`
  - `time_segments`
  - `time_resolution`
  - `mode`
- agent 调用 mandatory evidence tools。
- agent 可按需调用 optional evidence tools。
- agent 判断 evidence sufficiency。
- 证据充分时输出 quantified result，并可附带风险/趋势。
- 只能判断方向时输出 directional result。
- 完全缺乏证据时输出 indeterminate result，而不是省略时间窗口。
- 结果包含 `analysis_detail`、`attribution_summary`、`evidence_chain`。

阶段自测验收：

- 用 fake evidence bundle 可以跑出一个完整 `InferenceResult`。
- 当存在目标检测站读数时，最终读数锚定 observed value。
- 当无检测站链路或目标参数无观测支撑时，不输出具体数值，只输出趋势/风险判断。
- 完全无可用证据时仍为每个窗口输出带 reason code 的结构化空判断。
- 每个 time window 都有 source、confidence、evidence refs。
- attribution factor 都能追溯到 evidence refs，并标记 `basis: observed | correlated | inferred | hypothesized`。
- confidence 规则在本 milestone 设计，保留 Agent 自主判断，不与 outcome status 做 Domain 硬绑定。
- 同一窗口多个有效站点冲突时由 Agent 判断，但必须引用全部相关观测并解释；任何加权、折中或综合值标记为 inferred，只有原样采用的有效站点读数可标 observed。
- prompt、raw LLM output、structured result 能写入 transcript。

## 9. M7: Topology-Driven Multi-Agent Workflow

目标：

- 把单 agent 推理扩展成水流拓扑驱动的多 agent workflow。
- 跑通 upstream -> downstream 的结果传递。

主要产物：

- `GraphRunner`
- `RunTopologyDrivenAgents`
- upstream result evidence
- multi-agent session events

主要能力：

- 按水流拓扑 DAG 调度 agent。
- 上游完成后，下游收到：
  - `upstream_result`
  - `upstream_confidence`
  - `upstream_evidence_summary`
  - `upstream_uncertainty`
- 下游把上游结果作为 current-session evidence。
- 支持 `gap_filling / prediction / mixed`。
- 在真实阶段边界检查 cancellation token；cancel 后不启动新 agent/tool，也不传播晚到结果。
- 使用 session 为根的 agent/tool/operation ID 关联 SSE、日志和 transcript。
- 基于真实 topology/workload 明确并发上限、排队或保持无限制的策略。

阶段自测验收：

- 使用 fake topology 可以稳定跑完至少 3 个节点的 upstream -> downstream workflow。
- downstream agent 的 evidence bundle 中能看到 upstream evidence。
- 每个 agent 的 start/completed event 顺序符合拓扑。
- cancel 后 session 标记为 `cancelled`，并停止继续传播后续结果。
- 已完成 agent 的结果和 transcript item 保留。
- 无法强杀的在途调用返回后只进入 cancelled transcript/日志，不触发前端 result；workflow 最终记录 quiesced。

## 10. M8: Memory/RAG from Transcript

目标：

- 让历史 transcript 成为可检索的 cross-session memory。
- 支持 `stateless` 和 `memory_augmented` 两种模式。

主要产物：

- `RAGIndex`
- `rag_projection`
- `rag_tools`
- historical evidence wrapper

主要能力：

- 从 `rag_eligible=true` 的 transcript item 生成 RAG index。
- 支持有限固定检索策略：
  - by COMID + parameter + time window
  - by nearby COMID + parameter
  - by parameter + season
  - by semantic query
  - by topology neighborhood + parameter
- `memory_augmented` 模式允许 agent 主动检索历史 transcript。
- `stateless` 模式不使用跨 session memory，但保留当前 session 内上游结果传递。

阶段自测验收：

- 一个历史 session 的 eligible item 能进入 RAG index。
- 新 session 在 `memory_augmented` 模式下能检索到历史 item。
- 检索结果作为 `historical_inference` evidence 写入当前 transcript。
- 当前真实检测站数据与历史记忆冲突时，真实检测站数据优先。
- 低置信度历史结果可以被使用，但保留原始置信度并降低权重。

## 11. M9: Settings/Keys/Admin + Demo Closure

目标：

- 补齐第一版 demo 的配置、key、admin 和端到端验收。
- 将 fake adapter 逐步替换成需要的真实 adapter。

主要产物：

- settings API
- key store API
- admin cleanup API
- provider/model 配置
- 最小真实外部 adapter 接入
- 第一版端到端 demo script 或验收用例

主要能力：

- 管理 provider/model/default settings。
- 动态修改默认 IANA analysis timezone；已有 session 不受影响。
- 保存、清除和 masked 展示 API keys。
- 提供自定义单位验证入口：单位 tool 先做确定性解析，Agent 判断是否适用于参数；默认只生成 session override。
- 全局单位表更新需要用户显式确认并保存版本记录。
- 清理增长型数据：
  - session history
  - transcript items
  - RAG index
  - derived cross-session memory
  - evidence cache
- 清理接口不删除 API keys。
- 端到端 session 能从创建跑到完成或 cancel。
- 前端按最终 OpenAPI/SSE 契约独立重构，不迁移旧前端字段、事件或交互。
- demo 启动说明明确单进程、单 worker，不暗示支持共享状态或跨进程 cancel。

阶段自测验收：

- settings/key/admin API 能独立通过测试。
- 清理增长型数据后，API keys 仍然存在。
- 使用至少一条真实或 fake 的完整链路完成 demo：
  - 创建 session
  - SSE 订阅事件
  - query understanding
  - topology
  - evidence tools
  - agent reasoning
  - transcript 落库
  - 历史详情查询
- demo 输出包含 quantified、directional 或 indeterminate 结果，以及证据链、置信度和归因总结。
- 最终前端、OpenAPI、SSE raw frame、错误码、smoke 和后端实现使用同一字段与事件名称。

## 12. 跨 Milestone 延后契约登记

以下事项不在 M1 强行实现，但后续不得遗漏。M2 及之后的完整硬约束、稳定 contract ID、MR 合规门禁和验收重点统一以 [deferred_milestone_contracts.md](deferred_milestone_contracts.md) 为准；本节只保留归属索引。后续设计或实现 MR 必须列出适用 contract ID、实现位置、测试证据和文档状态。

| 归属 | 功能域 |
|---|---|
| M2 | Session/SSE 生命周期、取消、续传、可观察性和单 worker baseline。 |
| M3 | Transcript、持久化与历史 API。 |
| M4 | Query understanding、日期/时区转换、参数解析与 topology。 |
| M5 | Source record、Evidence Tool、质量/聚合与单位系统。 |
| M6 | 单 Agent reasoning、三态结果、confidence 与 attribution。 |
| M7 | Topology-driven 多 Agent workflow。 |
| M8 | Transcript RAG 与历史证据约束。 |
| M9 | Settings/keys/admin、动态时区、自定义单位与 demo closure。 |
| Future | 自定义时间、非 COMID 扩展和第二版候选功能。 |

## 13. 第二版候选 Milestones

第一版骨架跑通后，再考虑以下 milestone：

1. Evaluation / Accuracy Benchmark
   - 用有检测站的水体做 holdout。
   - 隐藏真实值，让 agent 推测，再计算误差。
   - 比较不同 evidence policy、prompt、模型和聚合策略。

2. Replay / Reproduce
   - 基于 transcript snapshot 重放一次历史分析。
   - 对比 replay result 和 original result。

3. Advanced Agent Interaction
   - 下游 agent 基于拓扑向上游 agent 发起补充问题。
   - 保留拓扑触发原则，不做无约束 agent 聊天。

4. Storage Optimization
   - 大 raw data 从数据库拆到 object storage。
   - transcript item 只保存引用、摘要和 hash。

5. Frontend History Experience
   - 历史 session 列表。
   - 结果卡片。
   - transcript timeline 展开。
   - evidence chain 展开。
