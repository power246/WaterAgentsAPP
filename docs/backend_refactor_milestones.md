# WaterAgents 后端重构 Milestones

生成日期：2026-09-06

更新日期：2026-09-15

本文档是给后续设计/实现 agent 阅读的 milestone 引入文档。它基于 `docs/backend_architecture_skeleton.md`，说明第一版 demo 应该按什么顺序推进，以及每个 milestone 完成后如何作为开发接力检查点独立自测验收。

本文档只做中等粒度拆分，不展开每个功能的内部设计。每个 milestone 都应该在完成后有可运行、可观察、可测试的结果；这些结果用于后续 agent 接力和问题定位，不构成用户正式测试入口，也不要求支持任何中间态兼容。用户正式上手测试发生在 M1-M9 全部完成之后。

如果某个 milestone 依赖后续能力，可以先使用 fake/mock adapter。fake/mock 只需要帮助验证本阶段边界和数据形态，不代表最终外部契约；后续 milestone 可以在保持总体架构方向的前提下继续收敛和调整。

领域边界按 [M3 D50](M3/m3_transcript_history_design.md#d50run-覆盖整次分析session-对应独立-agent-上下文) 和 [D51](M3/m3_transcript_history_design.md#d51问题解析-agent-也属于-run-下的独立-session)：run 是一次用户提交的完整分析和历史分页对象；session 是单个 Agent 的独立上下文与完整 loop，包括问题解析 session 和各水体 session。解析 session 不绑定 COMID，水体 session 绑定对应 COMID 并等待所有上游结束后只触发一次。完整流程见 [总体设计第 1.2 节](backend_architecture_skeleton.md#12-从用户提问到历史记录阅读的完整流程)。旧设计中整次分析的创建幂等、整体状态、SSE、取消、保存重试和清理职责归到 run；M3 按 D53 四表职责集中修订模型、存储、接口及引用校验，D33 去重原则与既有持久化保证保留；所有表的时间要求按 D54/D55 落实。

M2 的旧整体 session 模型只保留为历史实现证据；M3 已在 `backend/` 实现 run 根协议，Agent session 是 run 下的独立上下文，旧 `/api/analysis/sessions` 不提供兼容别名。新后端的需求和契约只以架构文档、milestone 文档和后续新功能设计为准。`backend_bk/`、`frontend_bk/` 和 `docs/backend_refactor_contract.md` 完全不属于新后端上下文，不能成为需求来源、行为参考、数据源清单、兼容目标或测试 oracle。

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

跨平台交付必须满足 GLOBAL-C11：三系统共用代码、配置、依赖锁和 schema；[跨系统迁移](M3/m3_transcript_history_api.md#85-更换操作系统或机器的最短流程) 只调整环境并重建依赖。Milestone 2/9 验证目标平台安装与同库历史切换，不能只证明各自能启动；不增加数据库内容搬迁或在途 Agent 接管范围。

关键用户行为与持久化边界已完成对齐，工程方案参照 M1/M2 展开为 [M3 实施详细设计](M3/m3_transcript_history_implementation_design.md)、[API 与搭建排障指南](M3/m3_transcript_history_api.md) 和 [Milestone 0–16 实施清单](M3/m3_implementation_todo.md)。M3 已在 `backend/` 实现四表 PostgreSQL 持久化、原子提交、类型/读取模型、history、十路由与两层 SSE，并完成本机实现与 9 个 review finding 的复审闭环。Ubuntu、macOS、Windows 的实际 CI 与同一 PostgreSQL 跨系统迁移仍无完整外部结果，因此 GLOBAL-C11、Milestone 2/9 和中心最终承接保持 `partial`；这不再与本机代码缺陷状态混写。

当前公开路由以实际 OpenAPI 为准：

```text
POST /api/analysis/runs
GET  /api/analysis/runs
GET  /api/analysis/runs/{run_id}
GET  /api/analysis/runs/{run_id}/sessions/{session_id}
GET  /api/analysis/runs/{run_id}/sessions/{session_id}/transcript
GET  /api/analysis/runs/{run_id}/transcript/{item_id}/content
GET  /api/analysis/runs/{run_id}/events
GET  /api/analysis/runs/{run_id}/sessions/{session_id}/events
POST /api/analysis/runs/{run_id}/cancel
POST /api/analysis/runs/{run_id}/persistence/retry
```

消息流按 [D86](M3/m3_transcript_history_design.md#d86消息流划分为开始增量和定稿事件) 使用 message_started / message_delta / message_finalized，分别提供原消息元信息、本次新增文本及定稿表示。三者沿原 session SSE 编号续传，数据库不逐帧保存，其他过程事实仍完整记录；字段、组装和解析由实现者直接落实。

交互按 [D69](M3/m3_transcript_history_design.md#d69run-通知与-session-过程使用两层-sse) 采用两层 SSE：当前 run 页面建立轻量 run 通知流，上报整体状态、新 session、Agent 状态和保存情况；水体实时过程按 D68 连接对应 session 流。D65 路由表已同步两条 GET 流，GET 详情用于首次加载、断线核对与历史，不持续轮询；正常关闭条件按 D70，恢复语义按 D73，明确结束通知按 D76 使用 stream_end；其他事件、消费身份、控制通知 payload 及握手见 M3 API 指南。用户不能追加消息，停止整次分析只调用 D67 的 run cancel，具体内部取消边界和当前实现证据保持原义。

订阅时机按 [D68](M3/m3_transcript_history_design.md#d68按查看需要订阅水体-session已结束与历史使用统一读取)：打开所选水体的实时过程时订阅对应 session，切换或关闭时释放旧连接；未查看的 Agent 照常执行并保存。已结束 Agent 和历史通过统一读取接口取得完整记录，保留 auto/history 的来源语义；首次过程衔接按 D75，无游标先发送全部已有可见记录再持续推送新增，无需先 GET transcript；消费身份和游标按 D78、成功握手按 D77，具体事件 DTO 见 M3 API 指南，前端由 M9 实现和验收。

关闭时机按 [D70](M3/m3_transcript_history_design.md#d70两层-sse-分别结束run-流等待收敛与保存完成)：session 结束并发完可见过程后关闭其流；run 已终态、执行已收敛且全部必需写入确认后关闭通知流，不额外等待 60 秒。保存中或耗尽时继续通知补写变化，结果按原窗口交付，用户断开不影响后台工作；不新增关闭状态存储，D19 清理门槛保持原义。

快照按 [D71](M3/m3_transcript_history_design.md#d71run-通知使用完整轻量快照水体区分五种展示状态) 使用完整轻量状态，除 run、真实 session 和保存情况外，还覆盖 BFS 范围内水体的不参与、参与未开始、推理中、完成和出错。前端分别映射半透明灰/灰/蓝/绿/红，前两种不要求已有 session。M4/M7 产生原拓扑、调度与执行事实，M3 提供统一装配/存储能力，M9 验收颜色与交互；不增加地图状态副本，取消后的呈现按 [D72](M3/m3_transcript_history_design.md#d72取消后的水体展示延后到前端设计) 留到 M9 前端设计，不在本轮继续对齐。

断线恢复按 [D73](M3/m3_transcript_history_design.md#d73run-重连同步最新快照session-按游标补齐过程)：live state 保留时，run 重连同步最新完整轻量快照，不逐条补播旧状态通知；session 按游标补齐可见过程，完整过程与原全文仍可读取。无需新建通知日志或重复快照存储，重启或清理后仍转 PostgreSQL 历史；GET 与推送的状态优先级按 D74，成功握手按 D77，消费游标按 D78，具体错误正文见 M3 API 指南。

当前状态按 [D74](M3/m3_transcript_history_design.md#d74当前状态以-sse-为准不引入快照版本号) 以 run SSE 为准，迟到的 GET 不覆盖已收到的 SSE 状态；GET 继续用于加载、断线核对及完整内容/历史读取。不增加 state_revision 或数据库快照版本列，M9 验收来源优先级，后端状态机与持久化保证保持原义。

首次过程订阅按 [D75](M3/m3_transcript_history_design.md#d75首次订阅-session-流先发送已有过程再持续推送新增记录) 由 session SSE 连续发送已有全部可见记录与新增记录，重连按原游标补齐；共享原记录、解析和可见性，大正文与公共引用沿原全文读取能力处理。已结束 Agent/历史继续用统一 GET，不新增 transcript 副本或重复写入。

两类流按 [D76](M3/m3_transcript_history_design.md#d76两类-sse-用-stream_end-明确通知正常结束) 在满足 D70 条件后先发送 stream_end 再关闭；客户端据此停止重连，不以意外 EOF 判断业务结束。通知只控制连接结束，不新增业务终态、数据库记录或补写任务；session 流结束不能代替 run 的全部保存确认，原持久化保证与完整过程读取保持不变。

成功握手按 [D77](M3/m3_transcript_history_design.md#d77live-sse-统一返回-200通过-stream_end-结束) 统一为合法 live 请求返回 200 SSE；已结束 session 先补齐未消费记录再发送 stream_end，无待补记录时直接发送它；run 先发送最新快照，再按 D70 结束或继续。取消 M3 目标路径的旧 204 分支，同步实现、OpenAPI、raw frame、smoke 与前端验收；已清理实时状态仍按 410 转历史。

过程游标按 [D78](M3/m3_transcript_history_design.md#d78session-过程使用整数游标控制通知不编号) 复用整数编号及原事件续传：每个实际 session 从 1 递增，重发保留原 SSE id；Last-Event-ID 回传最后已消费编号，非法格式 400、超过已有末尾 409。run 通知、stream_end 和心跳不编号，不影响过程游标；保留 D56 的 run 内 transcript_sequence，不新增同义记录顺序或快照版本。

流式文本按 [D79](M3/m3_transcript_history_design.md#d79实时增量推送数据库按消息保存一份正文) 实时推送小片段、数据库按消息保存组装后的一份正文；稳定消息身份关联当前增量与完成消息，live 续传保持原号，严格历史只读已保存正文。失败/取消保留实际内容和未完成语义，推送前须支持跨片段凭据脱敏。M3 验证通用能力，M4/M6 接入真实模型输出，M9 验收逐步显示；四表、全文去重和完整过程保证不变。按 [D80](M3/m3_transcript_history_design.md#d80未定稿正文计入-pending整理后再触发写入)，未定稿必需正文计入 pending，无实际错误则 warning=null；结束或中断后整理写入，等待生成不消耗重试且不阻塞其他可写内容，已有耗尽及错误优先保留，不新增保存状态列。

消息身份按 [D81](M3/m3_transcript_history_design.md#d81流式片段与最终消息复用-item_id) 在开始生成时分配 item_id，所有片段与最终正文复用，读取和补写保持不变；前端按消息更新、按 SSE id 去重重发片段。分配身份不提前写库，不新增同义消息身份或映射存储；消息入序和原创建时间按 [D82](M3/m3_transcript_history_design.md#d82消息开始时固定过程序号和创建时间) 在开始生成时同时固定，定稿及补写不改位置或时间。其他可写记录照常保存，较大已保存序号不代替全部保存确认；通用消息状态与装载表示见 M3 API 指南，实际业务消息类型由生产阶段注册。

消息正文按 [D83](M3/m3_transcript_history_design.md#d83消息正文使用三种-generation_status) 使用 generation_status=generating/completed/incomplete。generating 仅存在 live，结束或中断后保存 completed/incomplete 及实际正文，原身份、位置和时间不变；不新增同义数据库列或另一份公共状态。当前与历史复用解析，消息生成状态与 Agent/run 执行状态、persistence 分开。

取消正文按 [D84](M3/m3_transcript_history_design.md#d84取消时固定原消息晚到正文另存审计) 在服务端受理 run cancel 时固定为 incomplete；晚到新增正文合并为引用原消息的独立审计，不复制原前缀，沿用原 transcript 表。当前 GET/SSE、全文及引用读取保持边界，已保存历史可读关联审计；原身份/时间、引用校验、补写预算和全部保存确认要求保持。

定稿通知按 [D85](M3/m3_transcript_history_design.md#d85消息定稿时发送-message_finalized) 使用 message_finalized：消息正常结束或中断、整理正文后产生一次，不等待数据库，复用原消息表示与既有正文装载。事件按原 SSE id 续传，不额外保存同义 transcript 或正文；取消只带固定原文，先于 session stream_end。M9 结束该消息打字状态，Agent 可以继续，定稿不等于已保存。

目标：

- 把 transcript 作为历史事实源跑通。
- 支持历史 run 列表、详情和所属水体 session 的完整过程查询。

主要产物：

- `LiveTranscriptBuffer`
- `TranscriptStore`
- append-only transcript item 写入能力
- PostgreSQL 按 [M3 D53](M3/m3_transcript_history_design.md#d53四张核心表会话状态与两层记录归属) 采用 analysis_runs、analysis_sessions、transcript_items、transcript_contents 四张核心表。run 保存整体当前信息及创建幂等信息，session 保存自身身份、角色、水体与必要状态/时间；原请求、run 创建事件、BFS 属于 run，Agent 消息属于各自 session。角色和水体归属优先来自 session，完整请求/配置/过程/结果保留原记录，run 汇总表达整体结论并引用水体结果，大正文按阈值只保存一份。session 状态枚举按 D60，具体列和约束见 M3 实施详细设计。
- 按 [D54](M3/m3_transcript_history_design.md#d54所有业务表的每条记录都保留时间信息) 和 [D55](M3/m3_transcript_history_design.md#d55通用时间最多两列occurred_at-复用创建时间)，每条业务表记录保留时间：analysis_runs/analysis_sessions 使用 created_at、updated_at，transcript_items/transcript_contents 仅使用 created_at。API occurred_at 从原记录 created_at 装配，数据库不另存同义列；时间在原创建/真实变更时确定，读取和补写不刷新。真实业务正文中的观测时间和预测窗口继续保留。
- session、request、LifecycleEvent、error 和 final result 的持久化
- 持久化 `Idempotency-Key`
- persistence-confirmed/workflow-quiesced/grace-period 驱动的 live-state cleanup
- 路径基线按 [D65](M3/m3_transcript_history_design.md#d65接口以-runs-为根嵌套读取独立-session)：创建、历史列表、run/Agent 详情、完整过程、原全文、SSE、取消和补写统一以 /api/analysis/runs 为根。M3 已替换 M2 旧整体 session 路径，并同步实现、OpenAPI、文档、测试与 smoke；M2 实现证据只作历史保留，不维护旧路径兼容别名。
- D69 的两流分别为 GET /api/analysis/runs/{run_id}/events 与 GET /api/analysis/runs/{run_id}/sessions/{session_id}/events。run 通知从既有状态装配，不重复保存完整 Agent 消息或为通知生成同义必需写入；保存变化与 GET 的 D40 语义一致，原唯一业务终态不变。
- `GET /api/analysis/runs`
- query 历史列表分页按 [M3 D47](M3/m3_transcript_history_design.md#d47query-历史列表按创建顺序游标分页)：limit 默认 50、允许整数 1–200，非法值 422；按 created_at DESC、run_id DESC，以 cursor 翻页，返回 items/next_cursor，不返回总条数。状态/updated_at 更新不改变列表位置，分页只读 PostgreSQL，Agent 过程沿用 D45 完整列表读取。
- 历史游标按 [D48](M3/m3_transcript_history_design.md#d48历史游标编码位置不保存分页状态) 编码版本与上一页实际末条记录的 created_at/run_id，前端原样传回；不增加游标表、分页会话或页面快照，普通重启后可继续读取。非法编码/字段或不支持版本返回 422，不静默退回首页。
- 首版历史列表范围按 [D49](M3/m3_transcript_history_design.md#d49首版历史列表只提供分页参数)，只提供 limit/cursor，query/status 等既有字段仍返回；不增加关键词、状态或时间范围筛选，后续按明确需求扩展。水体 scope 的完整过程、公共引用及全文读取保持原约定。
- 完整过程展示必须先选中水体。按 [D64](M3/m3_transcript_history_design.md#d64run-详情提供会话信息按-run-与-session-定位水体过程)，run 详情从原 session 装配已创建会话的 session_id、角色、适用 COMID 和状态；前端取得对应 session_id 后，以 run_id + session_id 读取水体结果和完整过程，后端校验归属。不另建映射表或按 COMID 定位同一 Agent 过程的第二套接口。具体 URL 按 D65，完整 DTO 见 M3 API 指南；M2 当前接口不能当作独立水体 session 接口。
- 按 [M3 D44](M3/m3_transcript_history_design.md#d44query-历史列表分页按当次地图进入连续会话记录)，产品分页针对 query 历史列表；选定某次分析后，前端按该次消息组织地图和水体连续多层会话。按 [D46](M3/m3_transcript_history_design.md#d46地图连线由前端设计后端保存原消息)，M3 保存并提供原消息及已有业务字段/引用，地图连线由前端设计，不增加地图专用存储要求。按 [D45](M3/m3_transcript_history_design.md#d45水体过程一次返回完整记录列表大正文按需读取)，timeline 一次返回所选水体 scope 的完整有序记录列表，小正文随列表返回、大正文按需全文读取，保留公共上下文关联；Agent 记录不提供分页参数。
- 路径按 [D65](M3/m3_transcript_history_design.md#d65接口以-runs-为根嵌套读取独立-session)：GET /api/analysis/runs/{run_id} 为 run 详情，GET /api/analysis/runs/{run_id}/sessions/{session_id} 为 Agent 详情，其 /transcript 返回完整过程；原全文使用 GET /api/analysis/runs/{run_id}/transcript/{item_id}/content。D42 默认 live state 存在时读内存、释放后读 PostgreSQL，不建设独立 /current 路由族。按 [D43](M3/m3_transcript_history_design.md#d43同一路径通过可选-view-选择严格历史读取)，省略 view 或 view=auto 沿用默认行为，view=history 只读已保存事实并返回 persistence=null，非法 view 返回 422；详情、timeline、全文和公共引用保持同一模式，SSE 410 历史入口显式使用 view=history。
- transcript item 基础字段（公共命名与职责按 [M3 D29](M3/m3_transcript_history_design.md#d29公共-envelope-的字段命名与职责)）：
  - `session_id`
  - `item_id`
  - `transcript_sequence`：按 [D56](M3/m3_transcript_history_design.md#d56每个-run-统一分配-transcript_sequence) 在 run 内统一编号，首次追加分配，session 筛选和补写保留原序号，不另维护 session 局部计数器。
  - `occurred_at`
  - `content_type`
  - `content_schema_version`
  - `scope`：适用时的 `target_comid`、`operation_id`；按 D52，Agent 执行统一关联独立 `session_id`，不再包含同义 `agent_id`；M3 的 run/session DTO 层级以当前 OpenAPI 和 API 指南为准。
  - `correlation`：适用时的 `request_id`、`error_id`
  - 内联 `content` 或 `content_ref`，大内容附 preview 和完整字节数
  - `tags`：默认空列表，仅辅助筛选
  - `rag_eligible`：默认 false，由业务生产方显式标记资格
  - `provenance`：保留真实来源，无适用来源时为 null
  - `confidence`：保留真实置信度，不适用或未知时为 null；元信息规则见 [M3 D30](M3/m3_transcript_history_design.md#d30元信息采用明确默认值保留真实业务依据)

上述字段描述领域/返回语义，不要求原样重复落表：content_ref 按 [D57](M3/m3_transcript_history_design.md#d57全文引用统一使用-run_id-与-item_id) 从原 run_id + item_id 组装，读取时核对 item 所属 run；session_id 继续表达 Agent 归属，不作为全文引用的另一组必需参数。scope/correlation 从权威归属和查询列组装，正文已有的 confidence/provenance 按类型和层级提取；只有明确查询需求才增加派生列。当前与历史读取共享 DTO、类型解析、全文装载和 D45 的完整有序记录装配，来源与取消可见性仍分别遵守契约。

M3 不建设 transcript_item_links；先按 scope 读取水体全部过程，再读取原 typed content 和 AnalysisDetail/AnalysisStep 显式引用的公共上下文。引用保存、校验和完整读取继续在 M3 验收；M6/M7 出现真实查询/反向查询/性能需求时再按 M6-C10/M7-C08 评估索引，建成后也只从原引用生成并与对应记录同批保存。

引用范围按 [D58](M3/m3_transcript_history_design.md#d58普通过程引用限于同一-run历史证据显式关联)：普通 transcript_item_refs 限于相同 run，可指向 run 公共记录或其他 session 的原记录；目标须已保存或同批保存。跨 run 的信息由 M8 的历史来源结构显式关联，保留原身份、置信度与检索信息，不复制成当前普通过程。

会话创建按 [D59](M3/m3_transcript_history_design.md#d59agent-准备执行时创建-session)：解析 Agent 在准备解析时创建 session，水体 Agent 在所有上游完成且准备执行自身 loop 时创建 session。DAG 不预建全部水体会话，未执行节点由 run 拓扑与实际过程表示；已创建会话失败或取消仍保留自身状态与记录。M3 提供存储能力，实际解析、水体推理和 DAG 生产流程由对应里程碑接入；运行中 session 创建沿用增量写入，不新增每 Agent 调用前的同步提交等待。

状态按 [D60](M3/m3_transcript_history_design.md#d60agent-session-使用五种执行状态)：Agent session 使用 pending/running/completed/failed/cancelled，pending 从 D59 的实际创建开始，不覆盖等待上游的节点。准备失败或取消可直接进入终态，所有终态确定后不再改写；run 后续取消不改变已完成 session。保存进度与警告按 run 管理，session 不增加 completed_with_persistence_error，不因补写而重新运行，也不通过新增公开事件或回放机制重建状态。

阶段自测验收：

- fake run 及其 Agent session 的请求、事件、结果能写入 transcript。
- 历史列表能返回：
  - `run_id`
  - `status`
  - `created_at`
  - `updated_at`
  - `query`
  - `configuration` snapshot：从已保存的原创建事件装配，analysis_sessions 不再存完整配置副本
- `target_comid/location/time_range/mode/parameters` 由 M4 基于持久化 AnalysisContext 扩展；`final_confidence` 由 M6 基于持久化 result 扩展。M3 不为尚未产生的字段伪造 placeholder。
- 历史详情返回 run、所属 Agent session、可展开的 transcript timeline，以及存在时的已持久化 final-result projection；M6 定型 final-result schema 前不生成 fake 业务摘要。
- transcript 写入保持 append-only，不修改旧 item。
- 创建 key/指纹/回执随 run 同事务保存并受数据库唯一约束保护；同一 item 的全文按阈值只保存于内联或独立内容表之一。
- session 状态由已有状态机决定，存在对应事件/记录时同事务保存；running 没有公开事件也须保存状态并参与持久化确认，不实现通用事件回放重建。
- 在没有关联索引表时，D45 按 scope 一次返回完整有序记录列表与原显式引用，仍能读取水体全部过程、共享全文和大正文中的引用；不依赖 Agent 记录分页或固定条数截断，写入失败不留下半批事实。当前与历史共用读取实现仍保留来源差异、持久化警告和取消后的可见性。
- API keys 不进入 transcript 内容。
- transcript content 使用 `content_type + content_schema_version` 版本化 envelope。
- run 创建记录固化分析时区，后续全局设置变化不改变历史窗口语义。
- transcript 写入前强制移除 API key、Authorization、Cookie 等 secret。
- analysis detail 可以引用完整 tool/prompt/raw output timeline；大 payload 采用预览加引用，按需加载。
- 相同幂等 key 和完全相同的原始请求体在服务重启后仍返回原 run；相同 key 搭配不同请求体返回 409。前端每次主动分析生成新 key，即使 query/请求体相同；只有同一次提交的重试才原样复用 key 和请求体，不额外做字段规范化。幂等记录不自动过期，随 run 历史保留；详见 [前端接入约定](frontend_integration_contracts.md)。
- cancelled session 的晚到内容可以写 transcript，但不再进入 SSE 或下游，也不增加 after-cancel 冗余字段。
- 按 M3 D38，成功分析结束时固定此前已产生的必需写入范围，交付窗口到期仍未保存完整则进入 `completed_with_persistence_error`，发布带安全 warning/error_id 的唯一 `run_completed`。选定终态后的完成事件/收尾写入失败通过当前读取返回警告，不改写业务终态；全部必需写入确认前不清理 live state。
- 按 M3 D39–D41/M3-C20，自动补写耗尽后提供 POST /api/analysis/runs/{run_id}/persistence/retry，无需请求体；由原串行写入器受理新一轮或复用已有补写，202 只表示受理，200 表示当前无需补写。无 live 但历史仍在时返回 409 / persistence_retry_unavailable；重复触发不增加并发或重置运行中的预算，不重新分析，HTTP 断开不取消已受理补写。M9-C11 承接按钮与响应丢失后先查询、不自动重发 POST 的规则。
- live state 只有在持久化确认、workflow quiesced 和 grace period 全部满足后才清理；grace 默认 60 秒、可配置，从终态、quiesced 和全部必需数据落库确认后起算，不延迟结果交付。cleanup 幂等且失败不修改业务终态。
- 启动遗留处理按 [D61](M3/m3_transcript_history_design.md#d61重启时分别处理-run-与-session-的遗留状态)：分别将 PostgreSQL 中 pending/running 的 run 与 Agent session 标记为 failed/service_interrupted，已终态 run 下的遗留 session 也检查。各层原终态保持不变，状态修正与适用中断记录同事务保存；保留原身份、时间事实和已保存过程，不补造未保存会话、不自动恢复执行，重复启动不重复修正已处理记录。
- 启动门槛按 [D62](M3/m3_transcript_history_design.md#d62启动检查全部成功后才接收请求)：依次完成 PostgreSQL 连接、schema 版本检查及 D61 遗留处理，全部成功后才接收请求；任一步失败则启动失败退出，输出脱敏诊断及修复提示。初始化/升级使用独立命令，启动不自动改表；交付跨环境搭建和排障说明。运行中持久化故障仍走原补写规则，新 run 创建仍先确认事务提交。
- 结构版本按 [D63](M3/m3_transcript_history_design.md#d63schema-版本严格匹配统一由-alembic-管理) 使用 Alembic 单一管理，只接受与当前代码要求完全匹配的版本，不另维护同义版本号或跨 schema 兼容。未初始化/需升级、版本较新/无法识别均在启动时明确失败，分别提示独立命令或核对配套版本，不自动降级；历史 content/event 版本保留原职责。
- live state 保留时继续 SSE 续传；服务重启后的旧 session 或终态内存清理后，SSE 返回 `410` 并提供历史查询入口，前端停止重连、改查已保存历史。内存和数据库都不存在才返回 `404`，读取失败不伪装成不存在。

## 6. M4: Query Understanding + Hydrology Topology

按 D51，问题解析 Agent 在本次 run 下执行独立 session，不绑定水体 COMID；输入、实际调用过程及结构化输出复用 M3 的存储和完整读取能力。解析结果可以包含目标 COMID 和坐标，后端以此向上游 BFS 并组织水文 DAG。真实解析 session 及输出由本阶段接入，验收见 `M4-C01`/`M4-A01`。

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
- 按 D58，`stateless` 模式不使用跨 run 历史记忆，但保留当前 run 内上游 session 的结果传递；`memory_augmented` 的历史证据使用明确来源关联。

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
- 按 M3 D66，执行中选中已有 session 的水体即可观看真实上报过程，不必等整次 run 完成或最终预测出现；不提供向 Agent 追加消息的交互，保留停止整个 run 的操作。关闭查看连接不取消分析，具体 session SSE 协议按后续确认结果实现并验收。
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
