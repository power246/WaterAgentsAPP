# WaterAgents 跨 Milestone 延后契约与 MR 门禁（M2+）

更新日期：2026-09-17

本文档集中记录已经确认、但需由后续 Milestone 实现、细化或持续保持的功能约定。内容来自 M1/M2 详细设计、后端架构骨架和总 milestones 文档，是跨 Milestone 延后事项的统一登记处。

各 Milestone 的详细设计可以补充实现类型、API schema 和验收用例，但不得静默弱化本文档的硬约束。约定变更时，必须在同一个 MR 中同步更新本文档、所属 Milestone 设计/TODO、相关 API 文档和测试。

**2026-09-10 领域基线修订，2026-09-17 实现状态同步**：按 [M3 D50](M3/m3_transcript_history_design.md#d50run-覆盖整次分析session-对应独立-agent-上下文) 和 [D51](M3/m3_transcript_history_design.md#d51问题解析-agent-也属于-run-下的独立-session)，run 覆盖整次用户分析，session 是单个 Agent 的独立上下文与完整 loop，包括不绑定水体的解析 session 和绑定 COMID 的水体 sessions，历史分页对象为 run。完整主流程见 [总体设计第 1.2 节](backend_architecture_skeleton.md#12-从用户提问到历史记录阅读的完整流程)。M3–M9 条目中此前用于整次分析的 session/旧路径按 run 层职责理解；创建幂等、整体状态、SSE、取消、保存重试及清理的已确认行为保留。四表职责、两层记录归属、路径和引用校验已由 M3 当前 schema/OpenAPI 实现；M2 旧代码与接口只保留历史证据，不再表示当前 `backend/` 状态。

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
  - 按 [M3 D54](M3/m3_transcript_history_design.md#d54所有业务表的每条记录都保留时间信息) 和 [D55](M3/m3_transcript_history_design.md#d55通用时间最多两列occurred_at-复用创建时间)，每条业务表记录保存时间，通用列按生命周期使用 created_at 与按需的 updated_at。M3 的 run/session 表使用两列，原记录/大正文表仅使用 created_at；transcript API occurred_at 从原创建时间装配，不另存同义列。原创建/真实变更时确定时间，保存重试与读取不刷新，观测时间和预测窗口等正文业务事实保留。
- **GLOBAL-C05 — 墙上时钟窗口**：窗口按当地墙上时钟推进。真实 DST transition 导致的 elapsed duration 变化合法；重复当地时间使用不同 UTC instant 作为唯一身份，不得互相覆盖。
- **GLOBAL-C06 — Evidence 闭包**：结果、归因和进入推理的事实必须能回溯到 evidence references；不得只把事实散落在 prompt 或日志中。
- **GLOBAL-C07 — Secret 边界**：API key、Authorization、Cookie 和其他敏感凭据不得进入 transcript、普通日志、公开错误或前端事件。业务分析 query 本身不自动视为 secret；按 M3 D26，日志允许保留其正文，只对其中凭据脱敏，不因字段名为 query 而整段隐藏。
- **GLOBAL-C08 — 最终前端**：最终前端以当时确认的产品设计、OpenAPI 和 SSE 事件契约独立重构，不迁移旧前端字段、事件或交互契约。
- **GLOBAL-C09 — 单一事实模型**：同一 request/session/event/error/result 事实不得在 API、日志、transcript 和 workflow 中维护语义不同的重复模型；边界映射只负责可见性和序列化。
- **GLOBAL-C10 — 可验收延后**：延后事项必须同时写明归属、必须保留的约束和可观察验收方式，不能只写“以后完善”。
- **GLOBAL-C11 — 跨系统可迁移性与简单迁移**：macOS、Windows、Linux 共用业务代码、配置格式、依赖锁和 schema 迁移链；迁移只需调整环境配置、在目标机器重建依赖并执行统一检查/启动入口，不要求改业务代码或按 OS 转换历史格式。路径、编码、进程与事件循环差异集中在基础设施边界，不依赖原开发机、特定 shell 或复制 venv/数据库原数据目录。M3 按 [实施设计 §10.4](M3/m3_transcript_history_implementation_design.md#104-跨操作系统可迁移性) 和 [迁移步骤](M3/m3_transcript_history_api.md#85-更换操作系统或机器的最短流程) 首次交付，Milestone 2/9 验证三个系统分别作为目标的锁定安装与同库切换、路径/编码兼容、原历史身份/时间/顺序/游标/回执/全文不变；缺少实际平台证据保持 partial。继续使用原 PostgreSQL 不搬历史，新空库通过同一 Alembic 初始化 schema；不额外要求内容搬迁、live state 转移或 Agent 续跑。后续 Milestone 新增依赖、脚本和存储时继续遵守。

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

当前工程规范见 [实施详细设计](M3/m3_transcript_history_implementation_design.md)，对外表示与搭建命令见 [API 指南](M3/m3_transcript_history_api.md)，实施与证据分解见 [Milestone 0–19 清单](M3/m3_implementation_todo.md)。D01–D86 保留确认依据；其早期整体 session 术语映射为 run，真实 Agent session 保留独立语义。Milestone 17–18 已完成 Windows Uvicorn factory 和统一脱敏/性能功能包的本机实现与适用验证，Milestone 0–16 保留历史记录；HEAD `def435a` 追加审查的 4 个 P1、1 个 P2 均已有本机修复与回归，Milestone 19 最终复审仍待完成，当前仍为 `request changes`。实际 Windows/三平台 CI 与同库跨系统迁移证据另缺，因此 M3 保持 `partial / request changes`，不进入最终 `implemented` 表；既有 contract ID 和硬约束不变。

工程定型补充：四表采用原身份/联合归属约束、单份正文及 Alembic 单 head；当前状态不通过事件回放重建。新生命周期正文版本 2 区分 run_* 与 session_*，历史版本 1 保留解析；run_snapshot 与 stream_end 是控制通知，session 的 transcript_item/message_started/message_delta/message_finalized 使用独立过程整数序号，不逐帧落库。run 详情提供全部 run-owned 原记录，所选 session 的完整过程另读；未知/损坏内容显式表示。配置、严格来源、取消可见性、创建未知提交核实和逐项保存确认按实施详细设计执行，不能用最大已保存序号推断草稿已保存。

实时交互按 [D69](M3/m3_transcript_history_design.md#d69run-通知与-session-过程使用两层-sse) 采用两层 SSE：当前 run 页面连接轻量 run 通知流，打开水体实时过程时按 D68 连接对应 session 过程流。run 流通知整体状态、新 session、Agent 状态和保存变化，GET 用于加载、断线核对及历史，不持续轮询。D65 已同步十条目标路由；正常关闭条件按 D70、结束通知按 D76，成功握手按 D77 统一 200 SSE 并取消旧 204 分支；具体事件 DTO、消费游标与错误正文按实施详细设计与 API 指南落实，唯一业务终态与持久化保证保留，当前实现证据不变。

### 必须实现

- **M3-C01 — Transcript 事实源**：提供 `LiveTranscriptBuffer`、持久化 `TranscriptStore` 和 append-only item 写入；数据库中的 transcript 是历史权威来源。
  - 按 [M3 D53](M3/m3_transcript_history_design.md#d53四张核心表会话状态与两层记录归属) 采用四张核心表：analysis_runs 保存 run 当前信息、列表字段、创建 key/指纹/单份原回执及引用；analysis_sessions 保存身份、所属 run、角色、水体与自身必要状态和时间；transcript_items 保存 run 或 session 的原 typed 记录；transcript_contents 保存独立大正文。不得复制同义完整请求/配置/结果/transcript，少量查询投影从同一事实生成，四表均按 D54 保留时间。独立创建 key 表取消，关联索引表延后至 M6-C10/M7-C08 按真实需求评估；引用校验、公共全文复用与完整读取仍须交付。具体列与约束已按实施详细设计实现，证据见 [M3 契约覆盖矩阵](M3/m3_implementation_todo.md#契约覆盖矩阵)。
  - 按 M3 D31/D57，item 首次创建时由后端生成全局唯一 UUID，补写/读取保留身份；content_ref 统一由原 run_id + item_id 组装，读取时校验该 item 属于指定 run，不另设公开 content_id。session_id 继续表达适用 Agent 归属。按 D56，run 自身与各 Agent session 的记录共用 run 内 transcript_sequence，首次追加分配，session 筛选保留原序号；相同内容的独立发生记录不按文本去重，公共上下文通过原 item 引用复用。
  - 按 [D81](M3/m3_transcript_history_design.md#d81流式片段与最终消息复用-item_id)，流式消息开始生成时分配一次 item_id，所有片段与最终正文复用；提前分配身份不写数据库占位或部分正文，结束或中断后仍按 D79/D80 整理保存。读取、补写及全文引用保留原身份，不增加同义 message_id 或身份映射表。按 [D82](M3/m3_transcript_history_design.md#d82消息开始时固定过程序号和创建时间)，消息开始生成时同时固定 run 内 transcript_sequence 和 created_at，occurred_at 同源，先保留在内存；定稿、保存和补写不重新编号或刷新时间。其他可写记录照常保存，不能按最大已保存序号确认此前所有内容。
- **M3-C02 — Content 版本**：transcript content 使用 `content_type + content_schema_version` 版本化 envelope。按 M3 D28，首批生产注册 analysis_request、lifecycle_event、error、runtime_fact 四类并严格校验；runtime_fact 内部也须有明确 fact 类型和字段。创建配置及生命周期错误通过原事件保留，不额外复制同义 item；LLM/tool/skill/hook、真实 context/result 类型由后续生产它们的 milestone 扩展注册，M3 不预造业务 payload。
  - 按 [D83](M3/m3_transcript_history_design.md#d83消息正文使用三种-generation_status)，适用的消息正文使用 generation_status=generating/completed/incomplete：generating 只用于 live，正常结束保存 completed，取消、错误或输出截断而未完整生成时保存 incomplete 及实际正文。状态仅在消息正文维护一份，不新增同义查询列或公共元信息，也不强制所有 transcript 类型都具备该字段；具体原因和完整 DTO 随消息类型定型。
  - 公共字段按 M3 D29，API 使用 occurred_at、明确的 scope/correlation 和 content/content_ref 装载表示；按 D55，occurred_at 从原记录 created_at 装配，不独立落列。外层不另设重复的 type，内部原 LifecycleEvent 的 type/schema version/sequence/发生时间保留原义。
  - 按 D33/D53/D57 区分存储与 API 表示：content_ref 从原 run_id + item_id 组装，路径按 D65，具体 DTO 按实施详细设计与 API 指南落实，不重复保存引用对象/URL；角色和水体归属优先来自 session，scope/correlation 已有权威归属或查询列时不再存同义 JSON，返回时复用模型组装。typed 正文里的真实目标位置、结果水体和证据来源正常保留，不由调用方独立维护同义副本。
  - 元信息按 M3 D30：tags 默认空列表，rag_eligible 默认 false；confidence 有真实值时保留并校验 `[0,1]`，不适用或未知时为 null；provenance 无适用来源时为 null，不编造业务依据。
  - confidence/provenance 已在正文中时按对应类型和层级提取，不由调用方在正文与元信息分别填写；仅明确筛选/排序/检索需求允许增加由同一事实生成的派生列。不同层级置信度不得混用，多来源信息仍完整保留。tags/RAG 资格也须保持唯一维护来源及上述默认语义。
- **M3-C03 — Run/session snapshot 持久化**：按 D50/D53，保存 run 创建时的 analysis timezone、time resolution 及其 source，确保历史窗口语义不受全局设置变化影响；完整配置位于原 run 创建记录中，M3 使用 run_created，旧 M2 的 session_created 保留为版本 1 历史语义。run/session DTO 按需装配，当前信息表不另存完整配置；原创建响应仅按 M3-C09 保留一份小型不可变回执。各 Agent session 另存自身必要状态与时间，能够判断是否开始、结束或失败，不能以 run 整体状态代替；枚举按 D60，通用时间列按 D55。
  - 按 [D59](M3/m3_transcript_history_design.md#d59agent-准备执行时创建-session)，解析 Agent 在准备解析时创建 session，水体 Agent 等所有上游推理完成、满足执行条件并准备开始自身 loop 时创建 session。DAG 确定时不预建全部水体会话；此前节点由 run 拓扑及实际过程记录表示。已创建会话失败或取消仍保留身份、自身状态和已有记录。created_at 取该 session 原逻辑创建时间，运行中创建沿用增量保存与 M3-C18，不新增每 Agent 调用前的同步提交等待。
  - 按 [D60](M3/m3_transcript_history_design.md#d60agent-session-使用五种执行状态)，session 使用 pending/running/completed/failed/cancelled。pending 表示已创建并准备执行，不包含等待上游；可因准备失败或取消直接进入终态，running 可进入完成、失败或取消。终态不改写，run 后续取消也不把已 completed 的 session 改为 cancelled。保存进度和警告仍按 run 统一管理，session 不增加 completed_with_persistence_error；具体状态更新入口和完整 DTO 按实施详细设计执行。
- **M3-C04 — History API**：按 D50 提供 run 历史列表和详情，以及所属水体 session 的完整过程读取；详情返回整次分析的信息和存在时的已持久化真实结果。完整过程需在前端选中水体后展示其 session。M6 定型 final-result schema 前不得生成 fake 业务摘要；目标路径按 D65，完整 DTO 与事件名称按实施详细设计与 API 指南落实。
  - 按 D69 修订后的 [D65](M3/m3_transcript_history_design.md#d65接口以-runs-为根嵌套读取独立-session) 路由表：创建和历史列表位于 /api/analysis/runs，run 详情/通知 SSE/取消/补写归于 run；Agent 详情、完整过程与过程 SSE 嵌套 session_id，原全文只用 run_id + item_id。两流分别为 GET /api/analysis/runs/{run_id}/events 和 GET /api/analysis/runs/{run_id}/sessions/{session_id}/events。M3 直接替换旧整体 session 路径，不维护兼容别名，同步 OpenAPI、文档、测试和 smoke；当前 M2 实现证据保持原义。
  - 按 [D64](M3/m3_transcript_history_design.md#d64run-详情提供会话信息按-run-与-session-定位水体过程)，run 详情提供已创建 session 的 session_id、角色、适用 COMID 和自身状态；前端选中水体后，以 run_id + session_id 读取其结果和完整过程，后端校验该 session 属于指定 run。会话信息从原 session 装配，不另存目录/映射表或新增按 COMID 定位同一 Agent 过程的第二套接口。历史目录仍只读 PostgreSQL，未创建节点不补造会话，解析 session 不绑定 COMID；路径按 D65，完整 DTO 按实施详细设计与 API 指南落实。
  - 按 D44/D45/D46/D50，run 历史列表单独分页；选定 run 后读取当次已保存的消息与结果，选中水体后展示其独立 session 的完整过程，不重新推理。地图连线由前端设计，M3 不增加地图专用存储或绘图语义。transcript 接口一次返回所选 session 的完整有序记录列表，按 transcript_sequence 升序，不增加 Agent 记录 cursor/limit/下一页字段或固定条数截断。小正文随列表返回，大正文按需全文读取，公共上下文保持完整关联；列表分页保留 D47 行为，按 D65 使用 run 路径与身份。
  - 按 [D47](M3/m3_transcript_history_design.md#d47query-历史列表按创建顺序游标分页)，GET /api/analysis/runs 的 limit 默认 50、允许整数 1–200，非法值返回 422；按 created_at DESC、run_id DESC 排序。首次不传 cursor，后续原样使用 next_cursor；响应为 items 与 next_cursor，不返回总条数，末页 next_cursor=null，无匹配记录时 items=[]。状态/updated_at 更新不改变列表位置，新分析通过刷新首页查看；只读已保存事实，数据库错误不返回假空列表或末页。
  - 按 [D48](M3/m3_transcript_history_design.md#d48历史游标编码位置不保存分页状态)，游标由版本与上一页最后一条实际返回记录的 created_at/run_id 编码，保留原数据库排序精度；前端原样传回，后端解码后按 D47 边界读取下一页。不另存游标、分页会话或页面快照，普通重启后仍可使用支持版本的游标；无法解析、字段格式非法或版本不支持返回 422，不静默回到首页。该游标不用于 SSE 或 Agent 记录。
  - 按 [D49](M3/m3_transcript_history_design.md#d49首版历史列表只提供分页参数)，M3 首版历史列表只提供 limit/cursor，不增加关键词、状态或时间范围筛选；query/status 等既有列表字段仍正常返回。水体完整过程的 scope 读取、公共引用及全文能力不受影响；后续有明确检索需求时再扩展列表和游标查询约束。
  - 按 D42/D43，详情、timeline、全文分别只保留一个统一路径，不建设独立 /current 路由族。view 省略或为 auto 时默认自动选择来源，view=history 只返回 PostgreSQL 已保存事实，非法 view 返回 422；历史列表继续只查数据库。不能把默认内存响应当作历史持久化证明，全文及公共引用保持所选模式。
- **M3-C05 — 大内容**：`analysis_detail` 支持完整可折叠上下文。按 M3 D23，单条脱敏后内容默认不超过 64 KiB 时内联，超出后完整保存在 PostgreSQL 独立内容记录中，timeline 返回引用、完整字节数及最多 2 KiB 的首尾预览正文（首尾各最多 1 KiB）；上限可配置，预览保留完整字符并明确标注省略，全文可按需读取。存储/传输策略不改变 Agent 输入或前端展开状态；skill/tool/hook 等按语义默认折叠，无论大小，用户点击后展开。具体产品流程由 M3 D21、M9-C10 承接，存储 preview 不代替推理概述或总结。
- **M3-C06 — 写前脱敏**：按 M3 D27，统一记录入口在内容进入可读取的 live buffer 前移除 API key、Authorization、Cookie 等 secret，持久化边界再次强制执行同一规则，不能只依赖调用方自律。普通业务 query/正文保留，preview、字节数和完整内容读取都基于脱敏后的记录；重复执行不改变安全正文的业务含义，完整内容引用不能绕过脱敏。
  - 按 [D79](M3/m3_transcript_history_design.md#d79实时增量推送数据库按消息保存一份正文)，实时文本也须在推送和进入可读取内存前处理凭据，支持凭据跨多个片段的识别，不能先泄露后通过最终消息覆盖或落库脱敏补救。持久化边界仍执行同一规则；具体缓冲和识别实现随消息协议定型，不扩展为通用 DLP 框架。
- **M3-C07 — History 核心字段**：M3 历史列表先返回当前阶段真实拥有的 run_id、status、created_at、updated_at、query 和 configuration snapshot。configuration 从已持久化的创建事件读取，API 字段不强制对应 analysis_runs 的冗余列；列表 query 等少量投影从同一业务请求生成。不得为尚未产生的 target/mode/parameters/final confidence 伪造值或自由形态 placeholder；这些字段分别由 M4/M6 扩展。
- **M3-C08 — 生命周期持久化**：持久化 run/session 状态、request、LifecycleEvent、errors，以及存在时使用 versioned content envelope 保存的 final result；历史 timeline 保留 event schema version、sequence 和原始发生时间。创建事务已提交但 workflow 启动失败时保留 run，按统一终态规则记录 `failed / workflow_start_failed` 和安全 error ID；HTTP 仍返回 `201` 原创建回执，同 key 重试不重新执行，详见 [M3 D16](M3/m3_transcript_history_design.md#d16创建提交后启动失败保留-session-并报告失败终态)。M3 不猜测 M6 final-result payload。
- **M3-C09 — 幂等创建**：实现持久化、可选的 `Idempotency-Key`。相同 key + 完全相同的原始请求体返回原 run；相同 key + 不同请求体返回 409，不额外做字段规范化或默认值合并。首次创建与幂等重试成功均返回 `201` 和原创建时的 `pending` 回执，不替换为当前状态。前端每次主动分析生成新 key，即使 query/请求体完全相同；只有同一次提交的重试才原样复用 key 和请求体。新 key 或不传 key 时创建新 run，不按 query 自动合并。幂等记录不自动过期，随 run 历史保留，终态、重启和 live-state 清理不删除它。同 key 同 body 并发时只创建一次，等待者在创建事务成功后取得原回执；确认回滚后允许等待者接手创建，等待超时返回可重试的 `503`，不把超时等同于回滚。不能只用内存 map 声称幂等。详见 [M3 设计 D11–D15](M3/m3_transcript_history_design.md#d11可选幂等键前端按一次主动提交管理) 与 [前端接入约定](frontend_integration_contracts.md)。
  - 按 D33，run 最多对应一个创建 key，key、原请求体指纹和小型不可变原回执保存在 analysis_runs 的创建信息中。按 D34，客户端生成随机 UUID v4，后端按 UUID 值识别，在同一套数据库的创建分析接口内唯一；analysis_runs.idempotency_key 可空且带唯一约束，省略请求头保存 null，提供非法 UUID v4 返回 `422`。取消独立 analysis_creation_keys 表，原回执一次捕获，不从当前状态或后来改变的响应结构重建，也不再复制其他同义创建快照。
  - 按 D35，同 key 等待默认最多 5 秒、可配置，锁等待与必要重读共用截止时间；确认成功立即返回原 `201`，超时返回 `503 / idempotency_wait_timeout / retryable=true`。超时仅结束该等待请求，不取消创建所有者或分析，也不证明回滚；不消耗 D04 后台补写预算。
- **M3-C10 — Cancelled transcript**：cancelled session 可以继续追加已经产生的在途结果和清理事实，但不得向前端/下游传播，也不增加 after-cancel 布尔字段。
  - 按 [D84](M3/m3_transcript_history_design.md#d84取消时固定原消息晚到正文另存审计)，后端接受 run cancel 时固定正在生成消息的已接收正文，标为 incomplete，原 ID、序号及创建时间不变。晚到片段合并为引用原消息的独立审计，只保存新增内容，不复制原前缀或另存拼接全文。当前 GET/SSE、全文、引用及下游不得读取晚到审计，auto 回退数据库不放宽；严格历史可读取已保存的原消息和关联审计。沿用四表、凭据脱敏、原子提交、引用目标已存在或同批保存及有限补写规则，定稿通知按 D85 使用 message_finalized 且仅含取消边界前原文，具体审计及事件 DTO 按实施详细设计与 API 指南落实。
- **M3-C11 — Persistence-error 终态**：按 [M3 D38](M3/m3_transcript_history_design.md#d38终态选定前检查既有写入后续保存失败保留终态)，成功分析结束时固定此前已产生的必需结果、过程和状态的写入范围；在 D04 的第 3 次重试失败后 2 秒窗口到期时该范围仍未保存完整，则进入 `completed_with_persistence_error`，发布唯一 `run_completed` 并携带安全 warning/error_id，不得误报 `run_failed`。该范围全部确认保存则正常完成。终态自身及后续适用收尾写入失败时保留已选定终态，由当前统一读取返回持久化警告，继续既定补写且禁止提前清理；不改写原事件或另发终态。等待期间取消仍立即响应，由现有状态机一次决定胜出终态，不在状态锁内等待数据库。
  - 按 D50/D60，本条 completed_with_persistence_error 属于整次 run；Agent session 保留自己的五态，不增加此状态。原整体生命周期事件按实施详细设计映射为 run_*，不用旧 session 命名混淆两层状态。
- **M3-C12 — Live-state 清理门槛**：只有 `persistence_confirmed + workflow_quiesced + grace period elapsed` 同时满足时才异步清理 live state；`persistence_confirmed` 必须来自本 run 所有必需且适用的 run/session/transcript/result durable write 成功回调，不能在仅入队、创建 task 或写入 live buffer 后提前标记。grace 默认 60 秒、可配置，在 run 终态、workflow quiesced、全部必需写入确认后才起算；持久化失败时不开始计时也不清理，补写完整且其余条件满足后再获得完整保留窗口。实际清理前复查资格，保留窗口不延迟终态/结果交付，详见 [M3 D19](M3/m3_transcript_history_design.md#d19全部完成条件满足后默认保留-live-state-60-秒)。
  - 按 D38，全部必需写入包含终态状态、终态事件及适用收尾记录；终态选定前的固定范围保存成功或业务状态为 completed，均不替代这一门槛。
- **M3-C13 — 清理幂等**：cleanup callback 必须幂等、可重试；cleanup 失败不改变业务终态，并通过 run/session/error ID 可排查。
- **M3-C14 — 启动遗留处理**：按 [D61](M3/m3_transcript_history_design.md#d61重启时分别处理-run-与-session-的遗留状态)，启动时分别检查 PostgreSQL 中的 run 和 Agent session；pending/running 转为 failed，原因记录为 service_interrupted，各层已保存的终态保持不变。检查 session 不以所属 run 非终态为前提，已终态 run 下的遗留非终态 session 同样处理。保留原身份、创建信息与已保存过程，不补造未保存会话或内容，第一版不自动恢复执行。
  - 修正状态与适用中断记录按 M3-C18 同事务保存，原创建回执、旧正文、时间和序号不变；新的 updated_at 与中断记录时间表达本次实际处理时刻，不假定真实进程中断时间，新增记录延续原 run 序号。重复启动不再修正已确定终态或追加同义中断记录。启动顺序与失败退出按 D62，事务分批按实施详细设计与 API 指南落实。
  - 按 [D62](M3/m3_transcript_history_design.md#d62启动检查全部成功后才接收请求)，依次通过 PostgreSQL 连接检查、schema 版本检查及上述遗留处理后才对外接收请求；任一步失败则本次启动失败退出，不跳过检查或继续执行后续步骤。输出脱敏原因与修复提示，需初始化/升级时提示独立命令，启动不自动改表。跨平台配置、实际命令和诊断文档必须配套交付，版本接受规则按 D63，检查超时按实施详细设计与 API 指南落实。
  - 按 [D63](M3/m3_transcript_history_design.md#d63schema-版本严格匹配统一由-alembic-管理)，schema 版本由 Alembic 单一管理，仅与当前代码要求完全匹配时继续启动。未初始化/需升级时提示独立命令，版本较新/无法识别时提示检查代码与数据库是否配套；均启动失败且不自动降级。不另建同义版本记录、手动期望版本配置或跨 schema 兼容逻辑；transcript/SSE 的原内容与事件版本继续按各自契约处理。
  - D62 不改变服务已成功启动后的运行中持久化故障规则：已有分析保留内存并按既定规则补写，新 run 仍须确认创建事务提交后才能开始分析；历史读取失败不返回假空结果。
- **M3-C15 — Event replay 边界**：按 D69 承接 run 通知与 session 过程两层 SSE，正常关闭条件按 D70，恢复语义按 D73：live state 保留时，run 重连同步最新完整轻量快照、不补播旧通知，session 按游标补齐未消费的可见过程。结束通知按 D76 使用 stream_end；合法请求且 live state 保留时，成功握手按 D77 统一 200 SSE，不保留旧 204 分支；消费身份、Last-Event-ID 及 400/409 校验按 D78；具体控制通知 payload 与错误正文按实施详细设计与 API 指南落实。重启或 live-state 清理后，对应历史仍在则握手返回 `410 Gone` 并提供历史入口，前端停止重连并读取已保存事实；两处均确认不存在返回 `404`，数据库读取失败不得伪装成不存在。不得通过历史读取重新执行分析或伪造可继续执行的 active stream。
  - 按 [D86](M3/m3_transcript_history_design.md#d86消息流划分为开始增量和定稿事件)，消息 SSE 使用 message_started / message_delta / message_finalized：原元信息先于本消息增量，delta 携带 item_id 与本次新增文本，定稿在最后。三者共用 session SSE 编号，重发保留原号；不反复传完整元信息或累计全文，不把每帧保存为数据库行。不同消息交错按原身份关联，取消后的审计不进入可见 delta；具体 DTO 和装配由实现者直接定型。
  - 按 D79，session SSE 支持生成过程中的文本片段，使用稳定消息身份关联片段和整理后的消息，片段按 D78 过程编号参与 live 续传。片段不逐条写入 transcript；PostgreSQL 保存组装后的消息正文一份，其他独立过程事实仍完整保存。不能将 run 通知、控制帧或每个 token 都强制映射为数据库行；run 流保持轻量。
  - 按 [D85](M3/m3_transcript_history_design.md#d85消息定稿时发送-message_finalized)，消息正常结束或中断并完成正文整理时产生一次 message_finalized，携带原消息定稿表示及正文同源的 completed/incomplete，不等数据库保存。正文沿原内联或预览/全文引用装载；事件不另加同义 transcript 或正文，按原 SSE id 保留并续传，不因补写或重连另产生一次定稿。取消仅带固定原正文，应先于 session stream_end；单条消息定稿不结束 Agent 流或表示已保存。
  - 按 [D78](M3/m3_transcript_history_design.md#d78session-过程使用整数游标控制通知不编号)，实际 session 的过程事件从 1 递增作为 SSE id，重发保留原号；Last-Event-ID 使用最后已消费编号，首次省略或 0 从起点发送。不符合非负十进制整数格式时返回 400 / invalid_event_cursor，超过该 session 已产生末条序号返回 409 / event_cursor_ahead，不静默回起点或跳过。run 通知、stream_end 和心跳不分配 SSE id，不更新过程游标；与 transcript_sequence 和历史分页游标分开。
  - 按 [D77](M3/m3_transcript_history_design.md#d77live-sse-统一返回-200通过-stream_end-结束)，已结束 session 先补齐未消费的可见过程再发 stream_end；无待补过程则在 200 SSE 中直接发 stream_end。run 每次成功连接先发最新完整轻量快照，满足 D70 全部关闭条件才接着发 stream_end，否则继续通知。正常结束不走 204；非法请求、归属错误与读取故障继续明确报错，不因统一成功握手改成假 200。
  - 按 [D76](M3/m3_transcript_history_design.md#d76两类-sse-用-stream_end-明确通知正常结束)，两流分别满足 D70 条件并发送完应发送内容后，先发送 stream_end 再关闭连接。该通知只表达流正常结束，不新增业务终态、transcript 或其他数据库记录，不触发补写；不能把 session 流结束当作整个 run 的保存完成。具体控制通知 payload、消费游标配合及已结束时的握手按实施详细设计与 API 指南落实。
  - 按 [D75](M3/m3_transcript_history_design.md#d75首次订阅-session-流先发送已有过程再持续推送新增记录)，未结束 Agent 的 session SSE 首次不带游标，先按原顺序发送已有全部可见过程，再持续发送新增记录，无需先 GET transcript。发送期间新增记录必须完整衔接；重连按 D73 补齐，已结束 Agent/历史仍用统一 GET。复用原记录、类型解析与可见性，大正文与公共引用沿原全文读取能力处理，不重复保存或重新执行。
  - run 流在当前 run 页面打开时连接，推送 run 状态、新 session、各 Agent 状态与保存变化；session 流按 D68 连接并承载所选 Agent 具体过程。通知从既有状态生成，不额外保存一份完整 Agent 消息或同义通知事实。GET 负责 run 首次加载、断线核对和历史；未结束 Agent 的首次过程按 D75 由 session SSE 从头发送，具体消费身份与去重编码按最终协议实现。
  - 按 [D71](M3/m3_transcript_history_design.md#d71run-通知使用完整轻量快照水体区分五种展示状态)，run 通知发送完整轻量状态快照，覆盖 run、实际 session、persistence 及 BFS 范围内水体参与/执行情况；不能仅列已创建会话而漏掉不参与和未开始水体。GET 与通知共用原事实装配，具体对象与引用组织遵守去重原则，不复制正文、颜色字段或另存可独立修改的状态快照。
  - 按 [D70](M3/m3_transcript_history_design.md#d70两层-sse-分别结束run-流等待收敛与保存完成)，Agent 结束并发完可见过程后关闭 session 流；run 已终态、执行已收敛且全部必需内容及状态均确认保存后，通知最终保存情况并关闭 run 流，不等 60 秒 grace。pending/retry_exhausted 时 run 流继续通知自动或手动补写变化，不重置预算，不改变原业务终态；用户关闭连接不取消后台执行或保存。

- **M3-C16 — 水体与操作关联**：按 M3 D50，一个 run 下的每个水体 Agent 具有独立 session，其消息与真实 COMID、操作记录明确关联；session 是独立上下文，不能仅作为统一上下文的 UI 筛选。整个 run 的记录可由统一存储保存，公共内容按引用复用；每个 session 的完整过程和引用都须可读，不能只保存总结挑选的步骤。原 D22/D33 的同 session 引用约束须区分所属 run 与所属 Agent session，具体校验结构按实施详细设计与 API 指南落实；真实业务关联由 M6/M7 产生。
  - 按 D51，问题解析 Agent 也属于 run 下的独立 session，会话不绑定 COMID，但结构化输出可以包含目标 COMID、坐标等业务事实。两类 session 共用记录模型、类型解析和全文读取能力；BFS 等确定性后端过程归属于 run，不另建 Agent session。M3 提供通用归属与读取能力，真实解析记录由 M4 产生。
  - 按 D53，用户原始请求、run 创建事件、BFS 等记录直接属于 run，问题解析和水体 Agent 的输入、消息、调用与输出属于各自 session；公共原记录按实际来源保留，不集中放入解析 session，也不复制到每个水体 session。记录角色和水体归属优先从所属 session 装配，具体列与引用约束按实施详细设计与 API 指南落实。
  - 按 [D58](M3/m3_transcript_history_design.md#d58普通过程引用限于同一-run历史证据显式关联)，普通 transcript_item_refs 限于同 run，可引用公共记录或其他 session 的原记录；目标须已保存或与源记录同批保存。其他 run 的证据通过 M8 的历史来源结构关联，保留原身份和置信度，不能作为本次普通过程引用静默接入。全文保留在原 item，完整读取仍先取所选 session 全部记录，再读取显式公共引用。
  - 按 M3 D32/D52，run_id 关联整次分析，session_id 统一标识独立 Agent 上下文与一次完整执行，取消同义执行 agent_id；agent_role 表达问题解析或水体推理职责，COMID 表达适用水体。session_id、operation_id 分别是后端为独立 Agent 会话、一次模型/工具等调用生成的 UUID，新的执行/调用使用新 ID，读取与数据库补写保留原 ID。调用开始/结束及直接关联 hook 共用 operation_id；模型原 tool_call_id 保留在对应 payload，不被系统 ID 改写。
  - 按 D33，M3 使用 scope 取得水体完整过程，再解析原 typed content 及 AnalysisDetail/AnalysisStep.transcript_item_refs 的显式引用，读取公共上下文；大正文从全文解析引用，不从 preview 猜测。引用身份、归属及目标存在性仍在写入边界校验，目标已保存或同批保存；M3 不以缺少 transcript_item_links 为由跳过关联校验、漏掉记录或复制公共全文。
- **M3-C17 — 当前内容统一读取**：按 M3 D24/D57，当前分析的结果和完整上下文通过统一入口读取；live state 存在时从内存读取，内存释放后由同一入口读取 PostgreSQL 已保存内容，前端沿用原 run/item 全文引用，读取校验 item 所属 run。持久化失败不阻止读取仍在内存中的结果和大内容，但必须保留持久化警告；内存读取不代表已保存。历史查询仍只返回已持久化事实，取消后的实时传播限制和 SSE 的 `410` 边界保持原义。
  - 按 D79，当前显示支持逐步生成的消息，历史只读实际已保存的组装正文。保存状态与写入触发按 D80，稳定消息身份按 D81 复用 item_id；消息入序和原创建时间按 D82 在开始生成时固定；消息 generation_status 按 D83 使用三态，generating 只存在 live，历史只读实际保存的 completed/incomplete 正文；完整 DTO、原因及具体未完成内容整理按实施详细设计与 API 指南落实；不把实时片段的可见性当作已持久化，不删除原完整 GET、全文或公共引用读取能力。
  - 按 [D74](M3/m3_transcript_history_design.md#d74当前状态以-sse-为准不引入快照版本号)，当前页面状态以 run SSE 为准，迟到的 GET 不覆盖已收到的 SSE 状态。GET 的初始加载、断线核对及结果/过程/全文读取保持，不增加运行期 state_revision、快照计数器或数据库版本列；后端状态与持久化事实仍由原入口统一维护，历史不混入 SSE。
  - 按 [M3 D42](M3/m3_transcript_history_design.md#d42合并读取路径默认自动选择来源)，D65 的 run 详情、所属 Agent 详情、Agent /transcript 及 run 下原 item 全文路径分别提供对应读取，默认自动选择 live state 或 PostgreSQL。前端不使用 /current 路径、不随内存释放更换路径或引用；统一路径共用模型/解析/全文装载/完整记录装配，取消可见性随读取语义保留。严格历史读取按 [D43](M3/m3_transcript_history_design.md#d43同一路径通过可选-view-选择严格历史读取) 使用 view=history，详情、timeline、全文及公共引用均只读已保存事实并返回 persistence=null，不从 live state 补齐。
  - 按 D38/D69，即使业务状态已为 completed，终态自身或后续收尾写入失败仍须通过当前读取及 run 通知流表达保存情况和警告/error_id；原终态保持不变，不发布第二个业务终态。run 流按 D70 的业务终态、执行收敛与全部必需写入确认条件结束，前端不能从业务终态或成功读到全文推断全部保存成功。
  - 按 [M3 D40](M3/m3_transcript_history_design.md#d40实时保存信息按读取来源生成不另存重试状态)，当前读取选择 live state 时返回 run 级 persistence：state 为 up_to_date（当前已产生的必需内容及状态均确认保存）、pending（等待消息定稿/待写/写入/退避中）或 retry_exhausted（耗尽停止且可手动恢复），warning 复用可空 PublicError。正常等待不伪造失败/error_id，恢复后当前 warning 可消除，原事件警告不改写。历史及当前回退 PostgreSQL 时 persistence 为 null，仅返回已保存事实，不读取内存重试状态或隐藏原记录警告。D69 的 run SSE 由同一写入器事实通知保存变化，复用相同语义；不另存重试状态列或重复 can_retry，不修改创建回执，也不因通知另生成必需写入。 [D80](M3/m3_transcript_history_design.md#d80未定稿正文计入-pending整理后再触发写入) 将已产生但未定稿的必需正文计入 pending，没有实际保存错误则 warning=null；结束或中断后整理写入，等待生成不执行 SQL 或消耗重试，其他可写记录照常保存。已有 retry_exhausted 及错误优先保留，新片段不重置预算。状态计算同时使用原 live 消息事实，不新增数据库列。
  - 按 D33/D45，两个读取职责共享 DTO、内容类型/版本解析、全文装载和完整有序记录装配，不维护两套 transcript 模型。来源选择、可见性和错误处理明确区分；引用读取也遵守所选来源，历史不回退到内存补齐，共用解析器不能泄漏取消后的晚到内容。D45 替换原 transcript 分页要求，query 历史列表独立分页。
- **M3-C18 — 追加原子性**：按 M3 D25/D33/D53，同一 run 每批原记录元信息、新增完整内容及正文中的显式引用在一个短 PostgreSQL 事务中提交，整批生效或整批不生效，避免已保存记录缺少同批全文或引用目标。run/session 的状态、必要时间及少量查询字段更新存在对应记录时，与该记录同事务保存；独立必要状态更新也参与确认。实际提交成功后才确认该批已保存，单批成功不替代整个 run 的全部写入确认。补写沿用原身份、时间事实与统一重试预算；未收到提交结果不等于已回滚，须按稳定身份核实。
  - 按 D36，一次创建事务或 session 写入批次默认共用 5 秒执行预算，可配置，覆盖获取连接、整批 SQL 和提交；提前失败立即结束，到期按超时处理并完成取消/连接收尾。未确认提交不算保存成功，也不证明已回滚；后台在本次尝试结束后按 D04 退避，不并行写同一 session、不叠加隐藏重试。与 D35 截止时间同时适用时取更早者；创建仍确认提交后才启动，不套用后台补写预算。
  - 按 D37，空闲写入器收到待保存 item 或 run/session 状态就开始，每 run 串行，不另设合批等待；默认以 100 条 item 为组批上限目标、可配置，不足也立即写。必须同事务的完整一组不拆，单组超目标时独立提交整组；全文不截断。写入中新内容留下一批，原批次失败后先按 D04 补写，新内容不抢跑、不打断退避或重置预算；不复制第二套正文队列。成功仅确认该批覆盖的写入，创建仍单独确认；终态按 D38 一次选定，状态和事件作为同一原子组保存，失败补写不改变原事实。
  - 当前状态由现有状态机统一维护，数据库接收状态快照，不引入事件回放重建或第二套状态推导。running 没有公开事件，照常持久化状态快照，不为存储增加事件。无对应记录的状态写入也纳入持久化确认；异步数据库进度可落后于 live state，但不得倒退/覆盖已确定终态，取消即时响应及既有补写门槛保留。
  - 此条替换 D33 原先要求 M3 同时维护关联索引的部分：M3 引用随原正文保存和校验；若 M6-C10/M7-C08 按真实需求建设索引，须从原引用生成并与对应记录同批提交，不接受独立业务编辑。
- **M3-C19 — 日志保留 query**：按 M3 D26，调整 M2 的 query 整段隐藏策略，使日志可保留正常业务 query，仍移除其中凭据。调整应覆盖结构化字段、异常诊断的 query 私有值、实际记录入口及回归测试；仅改变一个正则不算完成。日志继续保留 request/session/error 关联，transcript 仍是历史事实源。
- **M3-C20 — 手动恢复保存**：按 [M3 D39](M3/m3_transcript_history_design.md#d39自动补写耗尽后允许用户手动开启新一轮)，提供“重试保存”接口。每轮 5 次自动重试耗尽后停止；仅用户主动操作可在 live state 仍在、待写内容仍在且上一轮耗尽并停止时，复用原串行写入器开启新一轮首次尝试及最多 5 次自动重试，沿用 D04/D36/D37。执行或退避中的重复触发只返回当前补写情况，不另建任务或重置预算。保留原 run/session/item/正文和业务终态，不重新分析；内存丢失不能恢复未保存内容，无待写内容不执行空事务，触发成功不等于保存成功。M9-C11 承接前端按钮，路由与 HTTP 语义按 D41。
  - 按 D40，前端依据当前读取的 persistence.state=retry_exhausted 提供按钮，不仅依据 error.retryable；pending 不重置运行中的预算，up_to_date 不执行空写入，persistence=null 不伪装成可手动恢复。读取本身不触发新一轮补写。
  - 按 [M3 D41](M3/m3_transcript_history_design.md#d41手动补写接口只受理恢复不等待保存完成)，POST /api/analysis/runs/{run_id}/persistence/retry 无需请求体或创建幂等 key。新受理一轮或复用 pending 返回 202，up_to_date 返回 200，成功正文仅含 run_id 与 D40 persistence。无 live 但历史存在返回 409 / persistence_retry_unavailable / retryable=false，均不存在返回 404，数据库读取失败/非法 UUID 分别沿用安全 503/422。不能返回无人负责处理的假受理；受理后 HTTP 断开不取消补写。前端不自动重发这个 POST，响应丢失先查当前保存状态，不增加操作 key、回执表或正文副本。 D80 中仅等待消息定稿的 pending 也由原写入器负责并返回 202，不强制结束生成或提前写入部分正文，不执行空事务、新增写入器或启动重试。

### 验收重点

- **M3-A01**：测试 run 与 Agent sessions 的请求、事件、错误及测试专用注册结果写入 transcript，旧 item 不被覆盖。
  - 验证 D31/D57/D58 的 UUID 身份、全文所属 run 校验、普通过程引用的同 run 边界与适用 session 归属，以及补写后引用稳定、相同内容独立记录拥有不同 ID；通过原 item 引用可读取内联或独立保存的完整内容，排序不依赖 UUID。
  - 创建提交后 task 启动失败，原 run 和创建幂等信息保留，失败终态唯一且错误码为 `workflow_start_failed`；首次/重试均返回 `201` 原回执，重试不启动任务。无 task 时仍完成适用的 quiesced 收尾；cancel 已先提交时不覆盖 cancelled。
- **M3-A02**：历史记录稳定反序列化，content/event 版本、时区、sequence、置信度和 provenance 不丢失。
  - 验证 D30 元信息默认值、真实值往返及无依据时的 null；不将 null confidence 变成 0/1，不因写入成功自动标记 rag_eligible，tags 不覆盖 scope/type/status。
  - 验证 D33 正文元信息按类型/层级提取，结果整体、参数、窗口和证据置信度不混用，多来源信息不丢失；scope/correlation/content_ref 从单一存储表示组装，不存在独立填写的同义值。run 当前配置及列表 configuration 读取原创建事件，修改全局设置不改写历史。
  - 校对 D29 公共字段与完整读取表示：外层 content_type 正确分发，原生命周期 type/schema version/sequence/occurred_at 保留，scope 与 correlation 不混用，缺少适用业务归属时不伪造值。
  - 按 D55 验证四表的时间列组合、API occurred_at 与原记录 created_at 的同源映射，以及大正文沿用原 item 时间；生命周期原时间、正文观测时间和预测窗口不会被数据库保存时间替换。
  - 按 D56 验证同一 run 的公共记录、解析 session 与多个水体 session 共用唯一递增序号，首次追加确定、交错记录可排序，读取与补写不重新编号；SSE sequence 与 transcript_sequence 保持独立。
  - 验证 D28 四类内容与明确 runtime fact schema 的写入/读取；未知类型或版本不能作为新写入通过校验。配置/生命周期错误不会额外复制同义 item；版本化结果存储通过测试专用注册类型验收，生产不注册 fake result。
- **M3-A03**：transcript 中无 secret，大内容可通过 reference 追溯。验证脱敏后 UTF-8 内容的 64 KiB 边界及可配置上限，预览正文默认不超过 2 KiB、首尾各不超过 1 KiB，多字节字符不被切坏且明确标注省略；全文读取与脱敏后原内容一致，预览不冒充完整 JSON 或业务总结。
  - 按 D79 使用跨多个文本片段拆开的已知凭据及普通业务文本，验证原凭据不会进入 SSE、可读取内存、日志或持久化正文，普通文本可正确组装。不能仅验证最终消息已脱敏而放过此前片段泄露；全文与预览保持同一安全事实，具体用例随脱敏实现定型。
  - 验证进入 live buffer 前已脱敏、绕过普通记录调用方直接写 store 时仍执行边界脱敏，以及重复处理稳定；当前读取、历史读取、preview 和全文均无合成凭据，普通 query/正文保留。
  - 按 [D57](M3/m3_transcript_history_design.md#d57全文引用统一使用-run_id-与-item_id)，验证正文按阈值只在 transcript_items 或 transcript_contents 中保存一份全文；由原 run_id + item_id 组装的引用在两种装载方式下均可读取，run 公共记录与 session 记录共用，错误的 run/item 组合不能读出其他 run 的内容。不依赖数据库中另存的引用对象、URL 或额外公开 content_id。
- **M3-A04**：相同幂等 key 和完全相同的原始请求体返回同 run、`201` 和相同的原创建回执，终态后重试不重新执行、不发布第二个 created event；每次 HTTP 的 request ID 仍独立生成。同 key 请求体变化稳定返回 409，包括字段值、字段有无及 JSON 字节表示变化；服务重启或默认配置变化后仍成立。历史仍在时，经过时间和 live-state 清理不使 key 过期。两个不同 key 携带完全相同的 query/请求体必须创建两个不同 run；不传 key 时也不按 query 去重。
  - 同 key 同 body 并发成功只留下一个 run，workflow 仅由创建所有者启动一次；确认回滚后等待者可接手。按 D35 验证默认 5 秒及可配置时限、锁等待/重读不重置截止时间；超时返回 `503 / idempotency_wait_timeout / retryable=true`，使用原 key/body 重试不重复创建。等待超时不取消原创建/分析、不消耗后台补写预算；创建等待不等待 workflow 完成，也不阻塞其他 key/run。
  - 按 D53，在真实 PostgreSQL 中验证 analysis_runs 行上的可选 key 唯一约束、指纹和单份原回执；创建失败时 run/请求/创建事件整批回滚，无 key 的独立创建不互相冲突。状态变化、配置默认值变化及响应模型后续演进不导致原回执被重建或覆盖。
  - 验证 D34 UUID v4 格式及按 UUID 值匹配，非法值返回 `422` 且不创建；同一数据库的创建接口对同 key 只保留一个 run，多个省略 key 的请求仍可独立创建。key 解析不改变原始请求体指纹语义。
- **M3-A05**：cancelled 晚到内容可审计但不出现在 SSE/downstream；没有 after-cancel 冗余字段。
  - 按 D84 验证生成中取消以服务端受理为边界，原正文固定为 incomplete 并保留原身份、位置和时间；晚到片段不改写原消息，合并为独立审计并显式引用原消息，仅新增部分保存一次。覆盖当前内存读取、auto 回退数据库、全文及引用均不带回晚到内容，严格历史可以读取已保存关联；取消前已 completed 的消息保持原状态。无新增审计表或 after-cancel 布尔字段，具体 DTO 按最终协议验收。
- **M3-A06**：持久化成功/失败、workflow 未收敛、grace 未到和 cleanup 重试均有独立测试。验证默认 60 秒及可配置时长、三个条件全部满足后起算、补写成功后完整保留窗口、清理前资格复查，以及 grace 不延迟结果交付。
- **M3-A07**：`completed_with_persistence_error` 仍可交付当前结果，并可用 error_id 回溯持久化失败。按 M3-C17 验证数据库不可用但 live state 存在时，统一入口可读结果和完整大内容且保留警告；补写完整并按条件清理内存后，同一引用读取已保存内容；历史查询不混入未保存事实，读取失败不返回假空内容。
  - 按 D42/D43/D57 验证详情、timeline、全文的同一路径在省略 view 或 view=auto 时自动选择来源，live 释放后原全文 URL 与 run/item 身份仍有效，没有独立 /current 路由或另一套 DTO。view=history 在详情、timeline、全文与公共引用中只读 PostgreSQL，保留历史审计可见性与 persistence=null，不从内存补齐；非法 view 返回 422，数据库错误不返回空结果。模式切换不能改写原内容身份、复制全文或绕过当前读取的取消后传播限制。
  - 验证 D40 三种实时保存状态随待写内容、实际提交和耗尽情况准确生成；仅状态更新待保存时也不能返回 up_to_date，单批成功不确认剩余内容，up_to_date 不绕过 quiesced/终态/grace 清理门槛。正常 pending 的 warning 为 null，适用失败使用原安全错误，恢复后的当前警告与不可变终态警告分别处理。相同 DTO 在历史/当前回退数据库时返回 persistence=null，不查询内存补齐状态，不隐藏已保存警告，不用 null 掩盖读取失败。D69 的 run 保存通知与 GET 来自相同事实，不存在重复重试状态存储、can_retry 或因通知导致的新必需写入。 按 D80 覆盖只有未定稿正文、写入队列暂空的情况，仍返回 pending 且无实际错误则 warning=null；等待生成不消耗重试或执行空事务，其他可写记录照常保存。消息结束/中断后整理的正文按原规则保存；新片段或定稿均不覆盖已有耗尽及错误、不重置预算，不因单批成功将剩余未定稿正文误报为已保存。
  - 验证 D38：终态选定前固定范围全部保存则正常完成，窗口到期仍不完整则发布带警告的唯一完成事件；部分批次成功不冒充范围全部保存，窗口提前到期不重新等待。等待中 cancel 先胜出则保持 cancelled，完成先胜出则沿用取消冲突，数据库等待不占状态锁。另覆盖 completed 已选定后仅终态原子组或后续收尾写入失败：原终态、事件身份及内容不变，当前读取返回保存警告，历史只见实际提交内容，SSE 不增发终态，补写成功前不启动清理窗口。
  - 使用同组内容验证两种来源复用 DTO/解析/全文装载和 D45 完整记录装配：在事实相同时身份、正文、版本处理和顺序一致；来源进度差异、警告、未知/损坏内容及取消后可见性仍按各自契约表达，不靠混合来源消除差异。完整列表不代表未来无追加或整个分析已保存完整。
  - 验证 D47 query 历史列表默认/边界 limit、非法值 422、跨页顺序与同创建时间的 run_id 排序；同一组已保存记录无遗漏/重复，更新 status/updated_at 不改变位置，新分析可从首页读取。响应只提供 items/next_cursor 分页字段，空列表、末页和数据库故障明确区分，不将分页参数加到 Agent 记录接口。
  - 验证 D48 游标取自实际末条记录及原数据库时间精度，普通服务重启后用原游标继续下一页，不依赖内存分页状态、游标表或页面快照；非法编码、非法字段和不支持版本返回 422，不返回伪造首页/空列表。排序边界与 D47 一致，SSE cursor 不能被当作历史列表游标接受。
  - 按 D49 校对首版历史列表 OpenAPI/DTO 只声明 limit/cursor，未承诺关键词、状态或时间范围筛选；query/status 等既有字段仍正常返回，水体 scope 与全文读取不因该范围收紧而缺失。
- **M3-A08**：live state 保留时按 D69/D77 验证合法请求统一 `200` SSE、续传与 `stream_end` 正常关闭，不能由 run 业务终态直接推断通知流结束；重启或内存清理后握手返回 `410` 和可用历史入口，原已保存历史可读；完全不存在返回 `404`，数据库读取失败不返回假 `404`。前端收到 `410` 停止重连、不创建新分析；创建注册窗口不能误判为历史。保留 D19 清理门槛与 D20 控制范围原则，不为超过保留期仍未发送完的极少数慢连接增加专项机制或验收。
  - 按 D86 验证 message_started 先于同消息的 delta、message_finalized 位于最后；交错消息按 item_id 正确关联，首次订阅及续传使用原 SSE 编号。增量只提供本次新增文本，不复制全量元信息/累计全文，三类传输事件不产生同义数据库行或改变原消息保存规则。
  - 按 D79/D81 用测试消息验证开始生成时分配 item_id，连续片段、最终正文、补写和历史读取保留原身份，首次读取及断线续传完整且不重复生成正文。片段按原 session SSE 整数编号去重，不能仅凭相同 item_id 丢弃不同片段；多条消息使用独立 item_id，不用 session_id/operation_id 或 SSE id 替代。分配身份不写数据库占位或部分正文，stream_end 和心跳仍不编号。历史只出现实际已保存消息及其他真实过程，不从 live 片段补齐或重新执行 Agent。
  - 按 D85 验证正常结束、取消、错误和截断后的 message_finalized 复用原 item/顺序/时间和适用生成状态，数据库不可用仍可发送，补写不重复产生事件；无订阅者时也保留，首次订阅与重连按原 SSE id 补齐去重。定稿事件先于 session stream_end，定稿后 Agent 可继续其他消息；正文内联/预览引用沿原装载且全文可读，取消不带晚到审计，不额外写同义记录或正文副本。
  - 按 D78 验证多 session 独立公开编号、同一事件多次订阅及补发保留原号、Last-Event-ID 只补齐更大序号；首次省略或 0 可从头读取。非法格式 400、超过已有末尾 409，不建立伪正常流、不静默改游标。检查 run 通知、stream_end 和心跳无 SSE id，结束控制不吞掉末条过程游标；不新增 session 局部 transcript_sequence 或持久化通知副本。
  - 按 D77 验证已结束 session 游标落后时先发缺失可见记录、再发 stream_end；全部消费完时 200 SSE 只发 stream_end，不返回 204。分别验证 run 已满足关闭条件时最新快照后结束、未满足时继续通知，不能因 run 已终态跳过最新保存状态。旧 M2 的 204 实现及文档作为基线，M3 替换路径的 OpenAPI、raw frame、smoke 和实现须一致。
  - 按 D76 验证应发内容先于 stream_end，随后连接关闭；session 不等待全 run 保存，run 不在 pending/retry_exhausted 时提前结束。成功、失败、取消仍按各自真实状态表达，控制通知不产生额外业务终态或数据库写入；传输失败不得伪装为已发送正常结束。
  - 按 D75 构造订阅前已有记录及首次发送期间新增记录，验证无游标先按序取得全部可见过程并接上新增内容，无缺口且不要求先 GET transcript。断线用已消费游标只补齐未消费记录，保留稳定身份；与完整 GET 使用同一内容解析/可见性，公共引用与大正文按 M3-A09 校验，不因发送已有内容重复写入或启动 Agent。
  - 按 [D73](M3/m3_transcript_history_design.md#d73run-重连同步最新快照session-按游标补齐过程) 验证断线期间水体从运行变为完成后，run 重连只需发送最新完整轻量快照，不要求旧通知重播；同一 Agent 的 session 重连能按游标补齐未消费的可见过程，完整 GET 和原全文仍可读取。不为 run 通知新增持久化日志或事件回放状态机；历史与实时边界、取消可见性保持原义。
  - 按 D43 校对 410 引导的历史读取使用统一路径的 view=history，后续 transcript、全文和公共引用保持历史语义，不意外进入默认内存读取。
  - 按 D70 验证 session 流不等待其他 Agent 或全 run 保存；run 流则须同时满足业务终态、执行收敛与必需写入全部确认，单独 up_to_date 或业务终态不足以关闭。保存中/耗尽时保留通知能力，全部满足后通知最终保存情况并关闭，不等待 60 秒，也不延迟 D38 原结果交付。不得以关闭通知新增必需写入或改变 D19 清理资格，结束通知按 D76 验证，成功握手按 D77 验证，具体 payload 按 M3 API 指南验收。
  - 按 D61 验证 pending/running 的 run 与 session 均转为 failed/service_interrupted；分别覆盖父 run 非终态及父 run 已终态的遗留 session。各层原终态、创建回执、身份与已保存正文保持不变，不从丢失内存补造记录、不重跑 Agent。状态修正与适用中断记录同事务提交/回滚；处理时间如实保存，原时间和序号不变，重复启动不重复追加已处理的中断事实。
  - 按 D62 验证连接、schema、遗留处理的顺序及全部成功后才接收请求；分别注入各阶段失败，确认后续步骤不执行、本次启动失败退出且诊断已脱敏。未初始化或需要升级时提供独立命令提示，启动不执行 schema 变更；运行中数据库故障仍沿用补写行为，新创建未确认提交不启动分析。
  - 按 D63 验证版本完全匹配才继续，未初始化/需升级及较新/无法识别分别失败并给出对应修复提示，且不会执行遗留修正或自动升级/降级。版本检查与独立结构管理命令使用同一迁移版本来源，不存在可分别修改的第二套版本号；历史内容原版本不因检查成功而被覆盖。

- **M3-A09**：使用多 COMID、同名工具多次调用和公共上下文的测试记录，验证水体归属、调用开始/结束/hook 配对、引用可读取及原 transcript 顺序；按水体查阅能取得完整关联过程，公共全文只保存一份。真实推理产物接入在 M6/M7 验收。
  - 按 D51，以测试专用记录验证同一 run 可容纳不绑定 COMID 的解析 session 和绑定 COMID 的水体 sessions，消息归属与上下文不会混合；两类记录复用解析和全文读取。真实问题解析过程在 M4 验收，不在 M3 production path 中伪造解析输出。
  - 按 D64 验证 run 详情会话信息直接取自原 session，包含身份、角色、适用 COMID 和自身状态，未创建节点不出现空会话。使用两个 run 下同 COMID 的 session 验证 run_id + session_id 定位与归属校验，不能读取成另一轮结果；所选 session 过程完整且保留公共引用，不依赖 COMID 第二套入口或映射表。严格历史目录不混入尚未保存的 live session。
  - 按 D71 使用测试专用拓扑/调度/session 记录验证完整轻量快照可表达 BFS 范围内不参与、参与未开始、推理中、完成及出错；前两种无需预建 session，实际 session 目录仍只含真实会话。状态由同一事实装配，历史不混入 live 事实，不新增地图状态副本；后端保留真实取消状态，DTO 按后续契约验收；取消后的颜色/标签按 D72 留到 M9，不作为 M3 验收阻塞。
  - 按 D65/D69 校对十条目标路由的 HTTP 方法与归属参数，验证两层 SSE 分工、run/session 详情、完整过程和全文的 auto/history 语义。run 通知更新整体状态、新 session、Agent 状态和保存情况，session 流只承载所选 Agent 过程；不复制完整消息或形成重复持久化事实。原创建幂等、取消及补写按既定用例验收；SSE 事件、续传、关闭和 GET 衔接按最终两层协议验收，旧整体 session 路径不作为兼容入口。
  - 按 D53 验证原始请求、run 创建记录和后端过程直接归属 run，Agent 消息归属各自 session；没有把全部公共记录塞入解析 session。角色和水体归属从 session 等权威事实装配，不存在可被调用方分别修改的同义副本，真实业务正文中的目标与来源信息保留。
  - 按 [D56](M3/m3_transcript_history_design.md#d56每个-run-统一分配-transcript_sequence) 验证 session 全过程筛选保留不连续的原序号，公共引用保留原身份与 run 内顺序，不维护第二套局部编号，不把正常缺口判为丢失，也不因此补入无关水体消息。
  - 按 D58 验证同 run 内跨 session 的上游结果与公共上下文引用可保存、读取，跨 run 的普通 transcript_item_refs 被明确拒绝；目标已保存或同批保存时校验通过，缺失目标不静默跳过。M8 历史证据的生产与原来源读取由其阶段验收，不因 M3 拒绝普通跨 run 引用而取消历史来源能力。
  - 按 D32/D52 验证同一 COMID 在不同 run 中对应不同 session_id，同一 session 的全部 Agent 过程和结果保持该身份，不再生成、保存或返回第二个同义执行 agent_id。同名工具的不同调用拥有不同 operation_id；读取和补写保持身份，模型原 tool_call_id 往返保留，前端配对不依赖角色、名称或正文。
  - 按 D59 验证实际创建的 session 原身份、创建时间和已有过程可保存与读取，随后失败或取消不删除会话，补写不重新创建或执行 Agent。M3 使用测试流程验证存储能力；真实解析和 DAG 生产方在对应阶段验证准备执行才创建，尚未执行节点不预建空会话。历史未读到尚未保存的 session 时，不从 live state 补齐或据此宣称从未执行。
  - 无关联索引表时验证 D45 按所属水体 session 一次返回完整有序过程，包含未被最终总结引用的记录；用超过旧候选页大小的记录集验证没有固定条数截断、末尾截取或下一页依赖。小正文随列表返回，大正文 preview/content_ref 可读取原全文；共享上下文引用和大正文里的引用完整可追溯。按 D50 定型后的规则校验所属 run、session 及允许的跨 session 引用，缺失目标或非法归属不得静默通过，不沿用旧模型的一律同 session 限制；公共内容按 ID 复用，历史不能从 live buffer 补齐。使用测试专用注册类型验收 M3 能力，不预注册 M6/M7 业务 payload。
- **M3-A10**：真实 PostgreSQL 集成验收覆盖一批元信息、完整内容、正文引用及适用 run/session 更新的成功提交，以及其中写入失败后的整批回滚；历史读取不暴露半批记录。补写后原 ID、顺序、引用及全文不重复、不被覆盖；实际提交确认前不提前确认该批已保存，也不触发 run 清理。
  - 按 D79 验证多个实时片段组装为一份消息正文，按 D23 仅内联或独立大正文一处保存；不存在逐 token 记录与同义全文两套持久化副本。消息及适用状态/引用遵守原子提交和原身份补写；失败/取消保存实际内容及未完成语义；按 D84 验证取消前原正文与晚到新增审计分别保存，原消息未保存时审计与其同批提交或等待目标保存，失败补写不复制前缀、覆盖原文或绕过引用校验，全部适用审计参与保存确认。具体 DTO 按最终消息协议验收。
  - 按 D83 验证 generating 只存在 live，正常结束、取消、错误及输出截断分别得到适用的 completed/incomplete 正文并保留实际内容；数据库中不写 generating 或同义状态列。读取和补写保留正文中的原生成结果，身份、序号及创建时间不变；已经正常生成完的消息不因后续工具失败、run 取消或保存失败而改写。历史不从 live 补入 generating，消息状态不能代替执行终态或保存确认。
  - 按 D55 验证延迟保存、读取、失败补写与提交核实不刷新原 created_at/updated_at；可变记录的真实状态/内容变化更新原 updated_at，并与适用记录同事务保存。不可变记录不增加 updated_at 或独立 occurred_at 通用列。
  - 按 D82 验证一条消息开始时同时固定 item_id/transcript_sequence/created_at，并先仅存在内存；较大序号的独立可写记录可以先提交，较小序号消息随后定稿及补写仍保留原值。历史按原序号读取，不能按最大已保存序号确认未定稿消息，也不因空写入队列误报全部保存或提前关闭 run 流、清理 live state；引用及原子组约束保持。
  - 验证 D36 默认 5 秒和可配置预算，连接等待与多条 SQL/提交共用截止时间、提前失败不等满时限、取消/收尾完成前不启动同 run 的下一次写入；超时不提前确认持久化、不丢弃未确认内容。提交结果不确定仍按原身份核实，D04 退避计数和第 3 次重试后的交付窗口保持原义；创建与同 key 等待同时受限时不叠加预算、不误启动 workflow。
  - 验证 D37 空闲即写、仅状态更新也触发、无内容不执行空事务；默认 100 条及配置生效，不足目标不等待，原子组跨目标仍完整提交。写入期间新增内容进入后续批次，失败保留原批次且不会被新内容触发额外尝试；部分批次成功不把剩余内容标为已保存、不提前清理。核对同一 run 串行和 D04 退避/次数不被合批逻辑重置。
  - 按 D33/D53 四表职责验收：引用保存在原正文，目标已存在或同批保存；run 与 session 的相关状态、时间和原记录同事务生效/回滚，独立状态更新也参与确认。验证同一 run 下 Agent 的状态/时间可各不相同且不会被整体状态替代，枚举按 D60 验收；不要求添加公开事件或回放重建。不复制同义完整请求、配置、结果或 transcript，保留同源必要列表字段及 run 创建的一份原回执。所有表每条记录按 D54 具备必要时间，补写不覆盖原过程时间。索引维护验收延后至实际建设它的 M6-C10/M7-C08。
  - 按 D60 验证 pending 准备失败/取消与 running 完成/失败/取消的原状态可保存并还原；已确定的终态不因 run 后续取消、晚到输出或补写而改变。保存失败时保留 Agent 执行状态，通过 run 的 persistence 表达保存问题；不生成 session 级 completed_with_persistence_error 或第二套重试状态。
- **M3-A11**：普通业务 query 在适用日志中保留；query 含合成凭据时，正常正文可辨认而凭据在结构化字段、原文/repr/JSON 转义诊断中不泄漏。HTTP/workflow 记录不再因 query 身份整段抹除；Authorization、Cookie、URL 凭据等原有保护与日志失败不影响业务的契约继续通过。
- **M3-A12**：验证自动补写耗尽后不被数据库恢复、读取或新内容重新唤起；用户手动操作启动原写入器的新一轮，首次立即尝试及最多 5 次重试的退避/预算正确。并发或执行/退避中重复触发不增加写入器、不重置计数。原批次恢复后继续后续内容，原 ID、全文和终态保持不变且 workflow 不重跑；无待写内容无空事务，内存已丢失不伪造恢复或成功。触发响应与实际持久化确认分开，保存完整后仍满足 D19 清理门槛。
  - 验证 D41 的真实 route、无请求体调用、202/200 成功正文以及 409/404/503/422 的安全错误；202 不等待数据库保存完成但必须有已受理的写入器负责处理。HTTP 断开不取消已受理补写，执行/退避中重复请求不重置计数，无法受理仍保留原待写内容；成功响应不复制完整 session/configuration/transcript，错误保留 X-Request-ID 关联。 按 D80 覆盖 pending 仅因正文尚未定稿的请求：202 继续由原写入器负责，等待生成不提前保存半条消息或重置预算，也不执行空事务或开启重试。

## 6. M4：Query Understanding + Hydrology Topology

### 必须实现

- **M4-C01 — Final request/context**：定型 analysis request 和 `AnalysisContext`，包含 query、位置/COMID、参数、日期范围、mode、memory mode 和时间粒度；直接替换阶段性 M2 request，不建兼容层。
  - 按 D51，问题解析 Agent 在本次 run 下执行独立 session，保存自身输入、实际调用过程和结构化输出，复用 M3 transcript 能力。session 本身不绑定水体 COMID，解析输出正常包含真实目标信息；后续 BFS 和水体推理使用该结果，不另造同义解析快照。
- **M4-C02 — 日期语义**：第一版只接受日期范围，结束日期包含整天；按 session 固化的 IANA timezone 解释后转换为 UTC。
- **M4-C03 — 自动粒度**：定义可测试的自动时间粒度规则；只有该规则实际选择粒度时才使用 `time_resolution_source=auto`。
- **M4-C04 — 参数身份**：参数使用 `ParameterKey(code_system, code)`，第一版默认 `code_system=usgs`。
- **M4-C05 — 位置与模式**：解析位置到经纬度和目标 COMID，并根据时间范围判断 `gap_filling / prediction / mixed`。
- **M4-C06 — Topology**：按 D50，以用户目标水体为基准向上游 BFS，寻找有水质数据的水体作为推理源头；构建 `HydrologyGraph`，标记 target、inlet、active/excluded，确定由上游向目标推进的 DAG 顺序。数据可用标准和搜索边界在 M4 定型。
  - 按 D71 保留本次 BFS 范围、实际参与与排除等原业务事实，以区分范围内不参与和参与未开始的水体；这些是 run 原记录，不是额外地图快照。上游没有带检测站点的水体是用户给出的排除示例，具体规则结合数据可用标准定型，不将示例当作唯一判定条件。
  - 按 D59，发现节点或确定 DAG 不预建对应 Agent session；尚未执行节点由 run 的拓扑及实际过程记录表示，session 在后续满足依赖并准备执行时创建。
- **M4-C07 — Typed events**：定义 `query_understood` 和 `topology_ready` 的实际 typed 内容，保持解析 session 与 run 拓扑事实的原归属。按 D69 的最终两层协议确定公开通知/过程的映射及引用，不再把所有 payload 强制放入 M2 单条整体流；run 通知保持轻量，具体事件名称、envelope 和续传语义随后定型。
  - 问题解析 Agent 的实际流式文本按 D79 接入，片段用于实时过程，正文按消息保存；解析成功的结构化内容仍须通过原模型校验，不能将尚未完成的文本直接当作 query_understood。M3 使用测试类型验证通用能力，不提前伪造真实解析输出。
- **M4-C08 — History query 扩展**：在 M3 history DTO 上增加真实解析得到的 target COMID/location、time range、mode 和 parameters，并从持久化的版本化 AnalysisContext 投影，不从 query 文本临时重算。

### 验收重点

- **M4-A01**：固定 query 在 fake geocoding/NLDI adapter 下稳定生成 `AnalysisContext` 和 topology。
  - 按 D71 验证 BFS 范围内被排除与参与的水体事实可保存并用于当前/历史状态装配，不因没有 Agent session 而丢弃被排除节点，也不为其预建会话。
  - 验证问题解析 session 归属于原 run，输入、实际过程与结构化输出可持久化和完整读取，目标 COMID 属于解析结果而非解析 session 的水体身份；后续流程使用同一解析事实。
- **M4-A02**：日期边界、DST、resolution source 和 mixed mode 拆分可通过测试重现。
- **M4-A03**：结果显示实际解析的 ParameterKey，不退回仅按裸 code 识别参数。
- **M4-A04**：新增内容按原归属保存，公开表示遵守 D69/D73 两层流规则：run 重连同步最新完整轻量状态，session 按游标补齐过程且不重复展示；不能把无需补播 run 中间通知误判为过程记录缺失；不能把所有事件强制共用旧 M2 单流序号或把 transcript_sequence 直接作为两流通用游标。

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
- **M6-C10 — Transcript**：prompt、raw LLM output、structured intermediate decision 和 final result 可写入 transcript。承接 M3 D22/M3-C16，为真实水体推理记录填入 COMID 和操作关联，结果关联完整过程及适用的公共上下文；不只保存最终总结引用的少量步骤。
  - 按 D79 接入真实模型文本片段推送及消息正文整理，SSE 与数据库使用不同粒度；正文只保存一份，片段保持 live 续传身份，失败/取消保留实际输出和未完成语义。raw output、结构化决策和有效业务结果仍按各自类型处理，流式文本不能绕过结果校验，推送前跨片段脱敏及原可见性限制必须生效。
  - 承接 D33 的按需索引评估：基于真实关联/反向查询或性能需求决定是否建设 transcript_item_links，无需求继续使用 scope + 原显式引用读取；不得为预想需求强制加表。若建设，索引仅从原记录生成，与对应记录同批提交；用真实过程验证索引前后读取结果一致及写入失败整批回滚。该延后不豁免 M3-C16 的既有能力验收。
- **M6-C11 — Final event**：定型 `session_completed` 的真实 inference-result payload，替换 M2 空 lifecycle payload，不保留 fake result 兼容结构。
- **M6-C12 — Correlation**：按 M3 D50–D52，以 run_id 关联整次分析的 SSE、日志与 transcript，适用的 Agent 记录使用独立 session_id；session_id 统一标识 Agent 上下文与本次完整执行，不另生成同义 agent_id。agent_role 表达职责，stage name 表达阶段，真实模型/工具等调用生成 operation_id UUID；开始/结束及直接关联 hook 保留操作身份，模型原 tool_call_id 保留在类型 payload 中。COMID、角色、Agent 名称和工具名称不代替执行身份。
- **M6-C13 — History result 扩展**：在 M3 history/detail DTO 上增加真实 final result summary 和 final confidence；值必须来自持久化 result，不在 history 查询时重新推理。
- **M6-C14 — 综合水质指标**：承接 M3 D21，用户所称“总体预测值”是跨多个参数的综合水质指标。定型所用指标、计算口径、单位/尺度、所需参数与缺失参数处理，提供有可追溯依据的结果；不能用 overall_confidence 或未经定义的分时段均值替代。对应水体的分时段预测、综合指标及推理概述/总结须能关联到真实 AnalysisDetail/transcript。

### 验收重点

- **M6-A01**：fake EvidenceBundle 跑出完整 InferenceResult，三种状态都有正反例。
- **M6-A02**：每个窗口有 source、confidence 和 evidence refs，结果与归因引用闭包成立。
- **M6-A03**：冲突观测、无证据和未知单位不会被静默猜测或丢弃。
- **M6-A04**：completed event、日志和 transcript 中的 result identity/版本一致。
- **M6-A05**：综合水质指标按 M6-C14 定型的规则产出，已知输入可验证，缺失参数时按明确规则表达可用性，指标与置信度不混淆；持久化后能读取原结果和推理依据。

## 9. M7：Topology-Driven Multi-Agent Workflow

### 必须实现

- **M7-C01 — DAG 调度**：按 D50/`HydrologyGraph` DAG 从推理源头向目标水体推进；每个水体 Agent 等待所有上游推理完成后只触发一次，从接收输入、获取证据到输出结果形成一个独立 session，全部归属于本次 run。按 D59，满足执行条件并准备开始该 loop 时才创建 session，DAG 确定时不预建全部会话；未执行节点由 run 过程表示，已创建会话失败或取消仍保留。上游结果沿 DAG 传递，禁止无约束 Agent 互聊；上游失败或不足时的调度语义在 M7 定型。
- **M7-C02 — 上游证据**：下游接收 upstream result、confidence、evidence summary 和 uncertainty，并将其作为 current-session evidence。
- **M7-C03 — 模式传播**：支持 `gap_filling / prediction / mixed` 的拓扑传播。
- **M7-C04 — Cancel 边界**：在每个真实阶段边界检查 cancellation token；cancel 后不得启动新 agent/tool，也不得传播晚到结果。
  - 流式正文按 D84 以服务端受理 run cancel 为界固定原消息；实际回调中的晚到新增内容交给原 transcript 审计路径，不继续拼接原消息或传给下游，已完成消息和 session 终态保持。
  - 按 D66/D67，用户只调用一次 POST /api/analysis/runs/{run_id}/cancel；后端先阻止解析、BFS 和 DAG 后续调度，再通过同一内部取消能力通知所有已创建的未终态 session，不限于正在查看或订阅的 Agent。首版不公开单独 Agent cancel HTTP 接口，不由前端逐个取消或由后端通过 HTTP 调用自身。run 取消先被接受后不得遗漏并发准备的 Agent，尚未创建会话的下游节点不再启动，也不为取消补造会话。
  - 按 D60，各 Agent session 终态不因 run 后续取消而改写，已经完成的 session 保持 completed；保存重试不改变执行终态。活跃 session 的取消传递按最终执行流程接入，不能用 run 状态直接覆盖所有会话。
- **M7-C05 — 在途结果**：无法强杀的 LLM/HTTP 调用可自然返回；其结果只进入 cancelled transcript/日志，不触发前端 result 或下游传播。
- **M7-C06 — Workflow 收敛**：记录 `cancel_accepted` 和最终 `workflow_quiesced`；M3 live-state cleanup 必须等待 quiesced。
- **M7-C07 — 并发策略**：基于真实 topology/workload 定义并发上限、排队、资源配额或明确保持无限制；不能让 M2 的“未预设”变成无意识生产默认。
- **M7-C08 — Topology final result**：按 D50/D53，聚合本次 run 下各水体 session 的真实结果，形成属于 run 的原最终结果/摘要记录；允许汇总表达整个分析的业务结论，并引用各水体原结果。run 汇总与单水体结果表达不同层级，不完整复制同义水体结果或 transcript。保留每个参与水体的 COMID、所属 session、结果或原内容引用，以及 AnalysisDetail/transcript 关联；历史先选择 run，再按水体查阅 session。
  - 按 D46 保存实际推理产生的原业务消息和已有引用；原消息中的位置/拓扑等内容正常保留，不因地图展示增加额外采集、连线字段或独立地图快照。地图连线由前端设计，实际推理所需的水文拓扑职责保持不变。
  - 若多水体实际查询首次需要 D33 的关联索引，按 M6-C10 同一规则建设并验收；已有索引则复用，不另建一套。是否建索引不改变按 scope + 显式引用读取全部过程、原全文复用及事务一致性要求。

### 验收重点

- **M7-A01**：fake topology 稳定执行至少 3 个节点，Agent start/completed 事件顺序符合拓扑。
  - 按 D59 验证 DAG 确定时未预建全部水体 session；汇流节点等待全部上游完成、准备执行时才创建自己的 session，实际执行一次且保持原身份。执行准备前被停止的节点不补造空会话，已经创建后失败或取消的 session 仍保留状态与已有过程。
- **M7-A02**：下游 EvidenceBundle 可见上游证据，但不覆盖下游当前有效观测。
- **M7-A03**：在各关键阶段触发 cancel 后没有新下游调用或前端 result，且最终出现 workflow_quiesced。
  - 按 D66/D67 验证一次 run cancel 覆盖未查看或未订阅的 Agent，后端先阻止后续调度再通知已有未终态 session；run 自身过程和尚未创建 session 的下游节点不再继续推进。验证取消先被接受时，并发准备的新 Agent 也不能启动；前端不需要遍历 session 取消，首版无独立 Agent cancel HTTP 路由。受理取消与实际收敛区分，取消不丢弃已有过程或停止必要的保存补写。
- **M7-A04**：已完成 Agent 的结果和 transcript item 保留，晚到结果只在 cancelled transcript 可审计。
  - 按 D84 覆盖真实模型生成中取消与取消后继续返回片段：原消息保留边界前正文及 incomplete，晚到部分进入关联审计；两部分不重复、不遗漏实际收到的内容，晚到正文不回流当前读取或下游，补写沿用原规则。
  - 按 D60 验证 run 后续取消时已完成 session 仍为 completed，失败/取消等原终态也不被晚到输出覆盖；各 session 自身状态、结果和过程保持一致。

## 10. M8：Memory/RAG from Transcript

### 必须实现

- **M8-C01 — Projection**：从 `rag_eligible=true` 的 transcript item 生成 RAG projection/index。承接 M3 D30，字段默认 false，消费业务生产方明确给出的资格标记；保存成功本身不产生 RAG 资格，非 eligible 记录仍可正常查询历史。
- **M8-C02 — Memory mode**：支持 stateless 和 memory_augmented；按 D50/D58，stateless 不使用跨 run 历史记忆，但保留当前 run 内上游 session 的结果。memory_augmented 可通过明确历史来源结构关联其他 run 的证据，不能把它伪装为当前 session 产生的过程。
- **M8-C03 — 固定检索策略**：至少支持 COMID + parameter + time window、nearby COMID + parameter、parameter + season、semantic query 和 topology neighborhood + parameter。
- **M8-C04 — Historical provenance**：按 D58，historical inference 进入当前 transcript/evidence 时使用明确的历史来源结构，保留原 run/item、适用 session、原 confidence、content schema version、RAG score 和 matched_by。原内容可按 D57 的 run_id + item_id 读取；普通 transcript_item_refs 不跨 run，不复制同义原过程以规避校验，具体历史来源 DTO 在本阶段定型。
- **M8-C05 — 当前观测优先**：历史 inference 不得覆盖当前有效观测；低置信度历史结果可以使用，但必须保留原置信度并降权。

### 验收重点

- **M8-A01**：eligible item 进入 index，非 eligible item 不会被检索。
- **M8-A02**：检索结果的身份、版本、分数、置信度和 provenance 可审计。
  - 按 D58 验证跨 run 历史证据保留原来源身份与置信度，原内容可通过 run/item 引用读取；不会混入当前普通 transcript_item_refs 或被重标为本次 session 的原过程。stateless 仍可使用当前 run 内上游 session 的结果。
- **M8-A03**：当前观测与历史记忆冲突时，优先级规则由测试证明。

## 11. M9：Settings/Keys/Admin + Demo Closure

### 必须实现

- **M9-C01 — Settings/keys/admin**：定型 settings、provider/model、key store 和 admin cleanup API，并将 demo 所需 fake adapter 替换为真实 adapter。
- **M9-C02 — 动态时区**：动态修改默认 IANA analysis timezone 只影响新 session，不改变已有 snapshot。
- **M9-C03 — Secret DTO**：定型 secret 与 masked status DTO，支持 API key 保存、清除和 masked 展示。
- **M9-C04 — 自定义单位**：确定性 unit tool 解析和校验量纲，Agent 判断参数适用性；默认只生成 session override。
- **M9-C05 — 全局单位确认**：写入全局目标单位表必须由用户显式确认，并保存版本记录。
- **M9-C06 — Admin cleanup**：可以清理 session history、transcript、RAG index、derived memory 和 evidence cache，但不得删除 API keys；与 M3 live-state cleanup 使用一致的 ownership/幂等规则。承接 M3 D14：明确删除 session 历史时对应幂等记录的处置，以及之后使用旧 key 的响应，并同步前端文档；不能将内存清理等同于删除历史或释放幂等 key。
- **M9-C07 — 最终契约校对**：完成最终 OpenAPI、SSE 文档、前端消费代码、端到端 smoke 和启动说明的一致性 sweep。
- **M9-C08 — 前端重构**：前端以最终契约独立重构，不读取或迁移旧前端组件、字段或事件。承接 D18/D69，由同一 fetch SSE 客户端实现管理 run 通知与 session 过程两类流，读取 HTTP status/body，按 D77 处理 `200` SSE、`stream_end` 与 `410` 等分支，不保留 M3 路径的旧 `204` 兼容分支，按 D73 区分 run 重连最新快照同步与 session 游标补齐过程，session 消费身份、Last-Event-ID 整数编码、400/409 校验及去重按 D78 实现，不要求 run 补播旧状态通知。页面关闭订阅不取消分析，GET 用于加载/断线核对/历史，不持续轮询；业务终态与流结束分别处理，正常关闭按 D70、明确结束通知按 D76，具体控制通知 payload、握手协议及解析库随后定型。
  - 按 D86 用同一客户端消费 message_started/message_delta/message_finalized，按原身份建立、追加和定稿同一消息，不为三阶段维护独立 transcript 模型；具体字段和解析由实现者同步协议后落实。
  - 按 D85 消费 message_finalized，按原 item_id 更新消息并结束其打字状态，按 SSE id 去重重发；正常完成与 incomplete 分别展示。该事件不表示 Agent/run 结束或已保存，大正文预览不替代完整正文；取消仅使用固定原文，消息定稿应先于 session stream_end。
  - 按 D79/D81 实现逐步输出：依据后端在消息开始时分配的 item_id 持续更新同一条消息，片段与整理后的正文不重复展示；按 SSE id 去重重发片段，相同 item_id 的不同片段正常追加。断线按原游标补齐，历史展示已保存的组装正文并保留原身份；item_id 不表示已保存。按 D82 保持开始生成时的消息位置和原创建时间，完成或补写后不移到末尾。按 D83 展示消息正文的 generating/completed/incomplete，正常结束和未完整生成分别表达；generating 只来自 live，历史读取已保存的后两态。消息 DTO 和具体原因继续定型，不因消息状态或打字效果改写业务终态或保存状态。
  - 按 D76 识别两类流的 stream_end，收到后停止该订阅的重连；未收到就意外断开按中断处理，不能仅凭 EOF 宣称业务成功或保存完整。用户主动关闭查看不自动重连；控制通知不加入业务 transcript 或生成新的完成状态。
  - 按 D75，首次查看未结束 Agent 直接无游标订阅 session SSE，消费已有全部可见记录后继续接收新增；无需先 GET transcript。重连按 D73 携带原消费游标，已结束 Agent/历史仍用统一 GET；共享客户端与内容模型，不新增第二套过程读取模型。
  - 按 D74 实现当前状态来源优先级：收到 run SSE 状态后，迟到的 GET 不覆盖；首次尚无 SSE 状态可使用 GET，断线核对与历史读取保留。不为此设计快照版本协议或时间戳比较。session 续传游标与内容 schema version 保持原义。
- **M9-C09 — Demo 单 worker**：启动说明明确 demo 单进程、单 worker；不得通过未实现的共享状态暗示支持多 worker。
- **M9-C10 — 地图结果与上下文阅读**：承接 M3 D21，分析完成后地图展示所有实际参与推理的水体；点击水体展示分时段预测、跨参数综合水质指标和推理概述/总结。从预测结果入口进入对应的具体推理上下文，skill/tool/hook 等按语义默认折叠、用户点击展开，与内容大小和是否已下载无关。前端依据真实结果与 transcript 引用关联，不把后端四种查询职责直接变成四层页面导航。
  - 按 D66，run 执行中也允许选中已有 session 的水体观看实时上报，不要求先有最终结果；用户不能给 Agent 追加消息，但保留取消整个 run 的操作。SSE 连接时机按 D68，独立 session 的具体订阅协议继续定型，关闭查看连接不取消分析，历史读取不重新执行。
  - 按 [D68](M3/m3_transcript_history_design.md#d68按查看需要订阅水体-session已结束与历史使用统一读取)，前端打开水体实时过程时才订阅对应 session，切换水体或关闭过程视图时关闭旧订阅，不在进入 run 页面时连接所有 Agent。未被查看的 Agent 正常执行和保存，打开前的全部可见过程也必须能读全。已结束 Agent 和历史通过统一接口读取，auto 与严格 history 语义保留；完整记录与实时流的衔接协议继续定型。
  - 按 D69，当前 run 页面同时建立一条轻量 run 通知流，更新 run 状态、新 session、各 Agent 状态和保存变化。切换水体保持 run 连接，通常一页为一条 run 流加一条当前 session 流，离开页面才关闭两类连接；run 通知不承载完整 Agent 消息，历史不订阅实时流。
  - 按 D71 消费完整轻量快照并展示五种水体状态：BFS 范围内不参与为半透明灰色，参与未开始为灰色，推理中为蓝色，完成为绿色，出错为红色。水体状态来自拓扑/调度与 session 原事实，前两种可没有 session，颜色仅由前端映射；取消后呈现按 [D72](M3/m3_transcript_history_design.md#d72取消后的水体展示延后到前端设计) 在 M9 前端设计阶段确定，本轮不冻结；session 的真实 cancelled 状态不改写。
  - 按 [D67](M3/m3_transcript_history_design.md#d67只公开-run-取消由后端传递到各-session)，前端停止操作只调用 run cancel，后端负责停止后续调度并通过内部能力取消各未终态 session；不增加单独停止某个 Agent 的 HTTP 入口或前端逐会话取消流程。
  - 按 D44/D50，历史分页对象为 run，完整分析记录必须先选中水体后展示其真实独立 session；未选水体时不展示混合的全 run 过程。按 D64，从 run 详情会话信息取得所选水体的 session_id，以 run_id + session_id 定位结果和完整过程；不另走按 COMID 定位同一 Agent 过程的第二套入口，具体 URL 按 D65。
  - 按 D59，拓扑中的未执行水体不保证存在 session，不能展示为预建的空会话；已创建会话失败或取消仍按所选来源及可见性规则读取已有过程。严格历史不以 live state 补齐，也不把暂未保存误判为从未执行。
  - 按 D60 展示各 Agent 自身的 pending/running/completed/failed/cancelled，pending 不包含等待上游；run 后续取消不覆盖已经完成的 session 终态。保存进度和警告仍按 run 管理，session 没有 completed_with_persistence_error，也不能将 completed 当作全部保存证明。
  - 按 D61，重启后依据各 run/session 的已保存状态展示 service_interrupted，原已完成结果继续可读；run 已终态也不能据此把遗留 Agent session 显示为完成，不提供自动恢复执行或重建未保存过程的入口。
  - 承接 D44/D45/D46，query 历史列表提供分页；选择某次 session 后，前端根据当次已保存消息组织地图标点、连线及水体连续多层会话，连线语义和画法由前端设计。后端提供原消息及已有业务内容/引用，不为前端画法另建存储。进入推理上下文时一次取得该水体完整有序记录列表，小正文随列表返回、大正文展开时读取，保留公共关联。Agent 记录不提供分页参数或分页展示。历史全程保持 view=history，同 query 的不同 session 不合并，水体上下文以原 session + COMID 关联。
- **M9-C11 — 重试保存入口**：承接 M3 D39/M3-C20，在自动补写耗尽且后端仍能补写时，于保存警告处提供“重试保存”按钮，用户主动操作才开启新一轮。执行中展示当前情况，不通过刷新页面、自动轮询或重复点击重置预算，不把此操作变成重新分析。内存丢失时明确不能恢复未保存内容，触发成功不显示成已经保存；沿用原终态并依据实际读取更新保存信息。
  - 按 D41 调用无请求体 POST /api/analysis/runs/{run_id}/persistence/retry；202 仅表示受理/已有补写，200 表示当前无需补写，409 表示当前不能手动恢复。此 POST 不自动重发，超时或响应丢失先查询 persistence，再由用户决定是否开启新一轮；已受理补写不随 HTTP 断开取消。
  - 按 D40 使用 persistence.state 区分 up_to_date/pending/retry_exhausted，只有 retry_exhausted 提供手动入口；persistence=null 表示本次读取无 live 保存运行信息，不能据此宣称整个分析已经保存完整或隐藏原记录中的警告。不另造前端重试计数或从业务 status/error.retryable 推断按钮条件。 按 D80，pending 可包括等待消息定稿，正常生成不展示保存失败；pending 或 202 不表明数据库正在执行 SQL，已有耗尽和错误不被新片段覆盖。

### 验收重点

- **M9-A01**：settings/key/admin API 独立测试通过，secret 不出现在公开响应、日志、transcript 或事件中。
- **M9-A02**：清理增长型数据后 API keys 仍存在，已固化 session 语义不变。删除历史后的旧幂等 key 请求按 M9-C06 定型的规则响应，前端文档、数据库记录处置与实际接口行为一致。
- **M9-A03**：端到端 session 从创建跑到完成或 cancel，并可查询历史详情。
- **M9-A04**：demo 输出三态结果之一、EvidenceChain、confidence 和 AttributionSummary。
- **M9-A05**：最终前端、OpenAPI、SSE raw frame、错误码、smoke 和后端实现使用同一字段/事件名称。统一 fetch SSE 客户端按 D69 最终两层协议覆盖 HTTP 状态分支、跨网络分块解析、D73 的 run 最新快照同步与 session 游标补齐/去重、GET 与通知衔接及关闭条件；run 业务终态不直接关闭仍需通知保存变化的流，关闭订阅不取消分析，`410` 能转到真实历史入口。
  - 按 D79/D81 验证消息生成过程中可持续看到新增安全文本，无需等整条生成结束；连续片段与最终正文使用相同 item_id，完成和重连后仍为同一消息，不出现重复正文。按 SSE id 去重重发，相同 item_id 的不同片段不能被丢弃，不同消息也不能混在同一消息中。严格历史不混入未保存片段，失败/取消的未完成内容与后续晚到可见性按最终协议验收；不得把打字效果或提前分配身份绑定为逐 token 写库或数据库占位写入。
  - 按 D82 验证消息开始时的位置和时间在持续更新、结束与历史读取中保持，后来保存的较早消息按原序号出现；不因完成或补写移动消息，不把后续序号已保存当作全程已保存。
  - 按 D83 验证生成中、正常结束和取消/错误/截断后的未完整正文分别展示；已正常生成的消息不被后续执行失败改成 incomplete。SSE 断线或 stream_end 不伪装消息成功，completed 不伪装 Agent/run 已结束或已经保存；历史不出现来自 live 的 generating 状态。
  - 按 D84 验证取消后原消息不再增长或变为 completed，当前全文和引用读取也不能拼回晚到正文；严格历史可读取已保存的原消息和关联审计，展示不复制前缀或伪装重新完成。incomplete 定稿按 D85 的 message_finalized 验收，具体 DTO 层级随最终事件协议落实。
  - 按 D85 验证 message_finalized 结束该消息打字状态、复用原消息且不重复追加正文；补发按 SSE id 去重，不能将定稿视为 Agent/run 结束或数据库已保存。覆盖未保存定稿、后续新消息、大正文预览与全文读取，以及取消只含固定原文并先于 session stream_end 的顺序。
  - 按 D78 覆盖 session SSE id 与 Last-Event-ID 原样往返、重发去重、多 session 游标隔离以及 400/409 响应；run 通知、stream_end 和心跳不能覆盖最后已消费的过程编号。不把 SSE 编号当作数据库记录序号或状态快照版本。
  - 按 D77 验证 session 无待补记录时仍通过 200 SSE 中的 stream_end 结束，有待补记录先完整消费；run 最新快照先于正常结束通知。M3 目标接口、统一客户端及示例不再将 204 声明为正常结束分支；410 及其他握手错误保留各自语义。
  - 按 D76 覆盖 stream_end 后停止重连、未收到即意外断开的恢复分支以及用户主动关闭查看；不将 session 流结束解释为 run 保存完成，不将 stream_end 当作新的业务终态。run 最终保存通知应先于其 stream_end，具体握手响应按随后冻结协议验收。
  - 按 D75 验证首次 session 订阅能显示打开前的全部可见记录并连续显示新增记录，未先 GET transcript 也可完整读取；大正文及公共引用仍可打开。重连不重复展示，已结束 Agent/历史遵守 GET 与 auto/history 语义。
  - 按 D74 覆盖 SSE 已报告完成、旧 GET 随后报告运行中的响应交叠，当前展示不倒退；另验证无 SSE 状态时 GET 初始加载及严格历史读取。验收来源优先级，不要求新增 state_revision，也不改变实际后端状态或已保存事实。
- **M9-A06**：最终 history 列表包含架构要求的 session identity/status/time、query、target COMID/location、time range/mode/parameters 和 final confidence，所有字段来自已持久化的阶段产物。
  - 按 D47 验证 query 历史列表默认 50、最多 200 条，遵守创建时间/session_id 倒序及原样传回 next_cursor 的规则，不依赖总条数；状态更新不重新排序，刷新首页可见新分析，错误不能显示成正常空列表。
  - 按 D48 验证前端不解析或重建历史游标，服务重启后可继续读取；422 游标错误被明确展示/处理，不把错误响应当成首页或空列表。
  - 按 D49，首版前端历史列表使用 limit/cursor 浏览并展示既有字段，不依赖未提供的关键词、状态或日期筛选 API。
- **M9-A07**：从地图上的每个参与水体可查看该水体的分时段预测、综合水质指标、概述/总结及对应推理流程；skill/tool/hook 小内容即使 inline 也默认折叠，大内容展开时能取得完整已保存内容。存储 preview、综合指标和置信度不混用。
  - 按 D66 验证 run 未结束时即可选中水体查看已有 session 的上报过程，不预造最终结果或未创建会话，不提供追加用户消息入口；run 停止操作与查看连接的关闭分离，具体 SSE 协议按最终定型结果验收。
  - 按 D67 验证前端仅请求 run cancel，不因当前只查看一个水体而缩小停止范围，也不将取消受理当作所有在途调用已停止；各 Agent 原终态保留。
  - 按 D68 验证查看才订阅、切换或关闭即释放旧连接，未订阅 Agent 的执行与保存不受影响；首次打开和切回均可取得此前完整可见过程，已结束 Agent 和历史使用统一读取，已结束但未保存的当前内容仍保留警告且不混入严格历史。首次衔接按 D75、续传按 D73 验收，消费身份与游标按 D78 验收，具体事件 DTO 继续定型。
  - 按 D69 验证未打开水体过程时，run 通知也能更新整体及各 Agent 状态；上游完成、下游 session 创建时更新对应状态，打开下游过程再建立其 session 连接。切换水体不关闭 run 流，页面不持续轮询 run 详情，通知不包含完整 Agent 正文。
  - 按 D71 验证上述五种水体颜色及业务含义，范围内被排除和未开始水体可见但不显示伪造的 session；各次 run 的历史使用原已保存事实，不能用当前数据重跑 BFS。取消后的颜色/标签按 D72 留到 M9 前端设计后验收，不阻塞 M3，后端仍返回真实状态。
  - 按 D50 验证分页列表选择 run、选中水体后读取对应独立 session 的完整过程；未选择水体时不展示混合的全分析过程。最终身份映射、路径及水体读取参数按后续冻结契约验收。
  - 按 D44/D45/D46，从分页 query 历史列表选择不同 session，前端按各自的已保存消息和最终前端设计展示地图与水体会话，不重新执行分析；同 query 或同 COMID 不串历史。一次读取水体完整有序记录列表、连续展示，公共上下文及大正文均可读，没有 Agent 记录分页参数或翻页交互，按需全文装载不改变语义折叠。地图画法不转化为后端独立快照或连线存储验收。
- **M9-A08**：自动补写耗尽后展示后端允许的手动入口，点击只调用保存恢复接口，不调用创建分析接口；执行中与重复点击不会开启并发补写或重置预算。根据读取情况展示保存进度、再次耗尽或恢复成功，业务终态不变；内存丢失与触发成功但尚未落库均不被显示为保存成功。
  - 按 D69 验证 run 通知流从同一写入器事实推送保存变化，普通 GET 可核对相同状态；业务结束后的补写通知不改写原终态或原 warning，也不因每次通知再产生同义必需写入。流关闭条件按 D70 验收。
  - 按 D70 验证业务结果先按原窗口显示，保存中及耗尽时仍可接收 run 通知，手动补写完成且执行已收敛后正常结束；不等待通知流关闭才展示结果，不自动开启新一轮，也不因为通知流关闭改写原持久化警告记录。
  - 验证 D41 的 202/200/409 处理，POST 超时或丢失响应后先读取保存状态、不会自动重发；后端已受理任务继续运行，客户端既不取消它，也不把响应不确定当作保存失败或成功。
  - 验证 D40 三种保存状态、正常 pending 无失败警告、恢复后当前 warning 消除而原事件警告保留，以及历史/数据库回退时 persistence=null 的展示；按钮不依赖独立计数、额外 can_retry 或单独的业务终态推断。 覆盖 D80 的生成中待定稿显示、pending/202 不误报数据库已开始执行，以及新片段不清除已有 retry_exhausted 和错误。

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
| — | — | — | — | — | 暂无满足全部验收条件的记录；M2/M3 本机实现证据见下方，三平台 CI 与同库跨系统迁移待通过。 |

允许的 Status：

```text
implemented
superseded
cancelled
```

`partial` 只能出现在 MR 描述中，不能进入最终承接记录。若一个 MR 只实现部分内容，应保持 contract 未承接，或先用新的稳定 ID 拆分约束。

### M2 待验收索引

本节只索引本地实现证据，不属于上表的最终承接记录。当前分支为 `codex/m2-analysis-session-sse`；Milestone 8 基于本地 Milestone 7 提交 `b3cd6d1`，正式启动审查修复基于 `48decb0`，日志边界修复基于 `b9cd8f0`，跨平台 smoke 修复基于已推送提交 `73ad133`。已收到 Windows + Python 3.11 CI 的关闭探测断言失败日志；修复后的 Ubuntu/macOS/Windows 完整 tests 与两项 smoke 仍待远端通过，因此 `M2-C01`–`M2-C13` 保持未最终承接。

| 范围 | 实现、测试与文档证据 | 剩余验收 |
|---|---|---|
| `GLOBAL-C01`、`GLOBAL-C02`、`GLOBAL-C04`、`GLOBAL-C07`、`GLOBAL-C09`、`GLOBAL-C10`；`M2-C01`–`M2-C13` | [M2 最终本地 contract evidence](M2/m2_analysis_session_sse_implementation_todo.md#m2-contract-evidence)、[正式启动审查修复](M2/m2_analysis_session_sse_implementation_todo.md#milestone-8-审查修复记录2026-09-08)、[日志边界修复](M2/m2_analysis_session_sse_implementation_todo.md#milestone-8-日志边界修复记录2026-09-09)、[跨平台 smoke 修复](M2/m2_analysis_session_sse_implementation_todo.md#milestone-8-跨平台-smoke-修复记录2026-09-09)、[详细设计](M2/m2_analysis_session_sse_design.md)、[API/runbook](M2/m2_analysis_session_sse_api.md)。602 项回归、health 和 completed/cancel 真实 Uvicorn smoke 在 macOS 本机通过；包含真实资源关闭检查、连接超时/重置注入、架构门禁、单 worker、活动 SSE 关闭、info/trace 下完整服务输出脱敏及流中断后 replay 回归。 | 修复后三平台完整 tests 和两项 smoke 的实际 CI 结果；通过后在最终承接表追加实现位置、测试结果和提交/MR 引用。 |
| M3–M9 / Future 接力 | [后续接力入口与验收](M2/m2_analysis_session_sse_implementation_todo.md#后续接力入口与验收)给出当前代码入口、保留约束与可观察验收。 | M3 的后续状态见下方独立索引；M4–M9/Future 仍按原 ID 实现，本行不将未来能力计为已实现。 |

### M3 待验收索引

本节只索引本机实现与契约审计证据，不属于上表的最终承接记录。当前分支为 `codex/m3-milestone`；Milestone 2–9 审查回补对应本地提交 `6c3a515`–`01e8f07`，Milestone 10 历史证据收敛于 `452749b`，Milestone 11–16 保留随后本机修复与契约审计记录。Milestone 17–18 已完成 Windows 正式入口和统一脱敏/性能功能包的本机实现与适用验证；Milestone 19 继续承接最终复审，当前仍为 `request changes`。GLOBAL-C11 的三平台实际结果（包括实际 Windows runner）另缺，M3 保持未最终承接；模拟 Windows 验证和本机性能/socket 结果不替代实际平台证据。

| 范围 | 实现、测试与文档证据 | 剩余验收 |
|---|---|---|
| `GLOBAL-C01`–`GLOBAL-C11`、M2 保留基线、`M3-C01`–`M3-C20`、`M3-A01`–`M3-A12` | [M3 契约覆盖矩阵](M3/m3_implementation_todo.md#契约覆盖矩阵)、[GLOBAL / MR 门禁审计](M3/m3_implementation_todo.md#global--mr-门禁审计)、[实施详细设计](M3/m3_transcript_history_implementation_design.md)、[API/runbook](M3/m3_transcript_history_api.md)与[前端交接](frontend_integration_contracts.md)。[Milestone 11–16](M3/m3_implementation_todo.md#milestone-11认证凭据与公开-metadata-脱敏) 保留既有证据；[Milestone 17–18](M3/m3_implementation_todo.md#milestone-17windows-正式入口与事件循环-factory) 已本机完成，Milestone 19 尚未验收。 | 先完成最终复审，再补齐 Ubuntu/macOS/Windows 完整测试、两项 smoke 及同一已保存 PostgreSQL 的跨系统核对，才可在最终承接表登记 `implemented`。 |

未新增、拆分、替换或取消 contract；Milestone 11–16 是既有 M3/Global/MR 约束的审查修复分解。M2 CI 待验收仍属于 M2 Milestone 7/8；M3 三平台与迁移待验收仍属于 M3 Milestone 2/9，不转移为新的 deferred contract。

## 14. 维护规则

- 文档使用本项目定义、通用技术说明及公开资料链接；参考实现不写内部项目名称、仓库路径或专有源码标识，保留适用于本项目的设计结论与边界。
- 新增条目时选择所属 section 的下一个未使用 ID，并补充对应验收项。
- 所属 Milestone 设计阶段必须把适用 ID 写入详细设计和 Implementation TODO。
- 所属 Milestone 实现阶段必须在同一 MR 更新承接记录，并提供可复现测试证据。
- contract 发生语义变化时，同步更新架构骨架、总 milestones、所属详细设计、API 文档和测试；只改其中一处视为未完成。
- 不用删除测试、放宽 schema 或添加兼容 fallback 的方式“解决”契约冲突。
- 本文档不引用旧前后端归档来补全需求。
