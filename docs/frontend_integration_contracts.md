# 前端接入约定

更新日期：2026-10-03

本文集中记录新前端必须遵守的跨 Milestone 约定，不复制所有 API 字段和 SSE raw frame。

M3 的具体字段、十条目标路由、两层 SSE frame、错误和 PostgreSQL 搭建说明已整理在 [M3 API 与搭建排障指南](M3/m3_transcript_history_api.md)，工程保证见 [实施详细设计](M3/m3_transcript_history_implementation_design.md)。Milestone 17–18 已完成 Windows 正式入口和统一脱敏/性能功能包，[Milestone 19](M3/m3_implementation_todo.md#milestone-19追加修复集成验收与契约复审) 已完成隔离 PostgreSQL、真实 socket、正式服务 smoke 与契约复审；Milestone 0–16 保留历史证据，HEAD `def435a` 追加审查的 4 个 P1、1 个 P2 均已本机复审通过，`request changes` 已清除。公共 OpenAPI/SSE 契约不变；正式前端消费仍由产品阶段 M9 实现。实际 Windows/Linux runner、三平台完整 CI 与同库跨系统迁移另保持 `partial`，契约可用于接入设计但不代表中心最终承接已登记 `implemented`。

**领域基线修订（D50/D51）**：run 是一次用户提交到全部 Agent 输出结束的完整分析，历史列表分页展示 run；session 是单个 Agent 的独立上下文和一次完整 loop，包括问题解析 session、各水体 session 和 M7 的最终汇总 session。前文设计曾将整次分析称为 session，现按 [D50](M3/m3_transcript_history_design.md#d50run-覆盖整次分析session-对应独立-agent-上下文) 和 [D51](M3/m3_transcript_history_design.md#d51问题解析-agent-也属于-run-下的独立-session) 修正。完整流程见 [总体设计第 1.2 节](backend_architecture_skeleton.md#12-从用户提问到历史记录阅读的完整流程)。M2 文档中的 session_id 仍是整次分析的旧命名；当前 HTTP 已迁移到 D65 的 run 路径，不能用旧整体 ID 充当独立 Agent session ID。

| 范围 | 状态 | 前端使用方式 |
|---|---|---|
| M2 session/SSE/cancel/error | 历史阶段证据 | 仅用于追溯旧整体 session 协议，不作为当前 HTTP 路径。 |
| M3 transcript/history/idempotency/SSE | Milestone 0–19 本机实现与复审完成，追加 findings 已关闭；实际 Windows/Linux runner、三平台完整 CI 与同库迁移仍为 `partial` | 以当前 OpenAPI、API 指南和 SSE frame 设计接入；Windows 入口、短前缀取消、private URL、性能与正文一致性已完成本机实现和复审，公共契约不变。正式消费由产品阶段 M9 接入。 |
| M4 query/context/topology | 后端协议与本机集成审计已完成；外部 PostgreSQL/provider/三平台证据仍按实际结果保持 `not run`/`partial` | 以 M4 API、运行时 OpenAPI 和既有两层 SSE 作为 M9 前端实现输入；当前不开始正式联调，也不增加 M4 产品兼容分支。 |
| M5 evidence | Milestone 0–13 后端本机实现与集成审计已完成；真实 provider 请求和三平台 CI 仍为 `not run`/`partial` | production 不新增 direct tool route，前端只沿现有 transcript/reference/SSE 读取 evidence；正式消费仍由 M9 接入。 |
| M6 reasoning/result | Milestone 0–13 后端本机实现与集成审计已完成；真实 provider 请求和三平台 CI 仍为 `not run` / `partial` | 以现有 result summary/ref、session transcript/SSE、history 与 [M6 API/读取契约](M6/m6_single_agent_reasoning_api.md) 作为 M9 接入输入；正式前端仍由 M9 实现，不调用 direct prompt/model/tool surface。 |
| M7 workflow/summary | 设计与跨 Milestone 契约已冻结，尚未实现 | 以 M7 设计/API 文档作为 M9 接入输入；当前不宣称运行时已有 DAG 调度、依赖状态或最终汇总。 |
| M8–M9 | 只有跨 Milestone 硬约束，最终 DTO 和路由尚未全部冻结 | 等所属 Milestone 的 OpenAPI、事件文档和验收完成后接入。 |

正式前端在 M9 依据届时的产品设计、运行时 OpenAPI 和最终 SSE 文档独立重构。`frontend_bk/` 是历史归档，不得复制其字段、事件、组件或 WebSocket 交互，也不要求兼容 M1–M8 的阶段性接口。

**已确认的产品交互目标（M9 实现）**

按 [M3 D66](M3/m3_transcript_history_design.md#d66分析中按水体实时观察用户可取消整个-run)，整次分析尚未结束时，用户也可以选中水体，实时观看对应 Agent session 的上报过程，不必等待最终预测出现。只展示已经产生的可见内容，沿用完整过程与语义折叠规则；未创建 session 的水体不补造空会话。

用户不能给 Agent 追加消息，但可以通过 POST /api/analysis/runs/{run_id}/cancel 停止整个 run；停止范围不取决于当前查看或订阅了哪些水体。关闭或切换查看连接不会取消分析。按 [D67](M3/m3_transcript_history_design.md#d67只公开-run-取消由后端传递到各-session)，前端只调用一次 run cancel，由后端阻止后续调度并通过内部能力取消所有已创建的未终态 session；首版不公开单独 Agent cancel HTTP 接口，前端不遍历会话取消。受理取消不代表在途调用已立即停止，各 session 原终态保留，晚到输出遵守既有可见性限制。D69 两层 SSE 的路径、事件、续传、握手与关闭协议已由 Milestone 7 实现，见 M3 API 指南；下文 M2 契约只作为历史基线。

按 [D68](M3/m3_transcript_history_design.md#d68按查看需要订阅水体-session已结束与历史使用统一读取)，打开水体实时过程时才订阅对应 session，切换水体时关闭旧订阅，关闭过程视图也关闭连接；进入 run 页面不自动订阅所有 Agent。后台执行及保存不依赖查看连接。Agent 已结束或正在查阅历史时，使用统一读取接口取得完整记录；当前读取保留 auto 和持久化警告，严格历史保持 view=history。打开前已经产生的全部可见过程也必须可读，不能仅展示连接后的消息；已有过程与实时上报按 M3 API §5.3 的首次订阅和整数游标协议衔接。

按 [D69](M3/m3_transcript_history_design.md#d69run-通知与-session-过程使用两层-sse)，打开当前 run 页面还要连接一条轻量 run SSE，接收 run 状态、新 session、各 Agent 状态和保存变化；打开水体实时过程时再加对应 session SSE。通常是一条整体通知流加一条所选 Agent 过程流，切换水体保持 run 连接，离开 run 页面才关闭页面持有的两类连接。普通 GET 用于首次加载、断线状态核对与历史读取，不持续轮询 run 详情。run 通知从已有状态生成，不携带完整 Agent 消息；关闭时机、事件、续传和 GET 衔接以 D70 及当前 M3 API 指南为准。

分析完成后，地图显示每个实际参与推理的水体。点击水体后展示该水体的分时段结果、逐窗口综合风险、日期范围总体风险、availability 和理由；从结果入口进入对应 waterbody Agent session，以类似 Codex 的方式阅读真实消息、tool、evidence、decision 和 result 上下文。

M7 还会在全部 active 水体收敛后创建一个不绑定 COMID 的 `run_summarizer` session，用普通用户容易理解的语言总结整条上游到目标流程。run 页面展示该总结、目标结论、覆盖不足或失败分支及特殊值；数字保留单位、时间窗和稳定来源引用。汇总失败时显示后端提供的安全失败信息，不能由前端固定模板补出一份看似成功的总结；目标结果有效时，run 仍可完成，水体结果照常查看。特殊值引用在首版保持稳定身份即可，M9 可进一步把它渲染成跳往对应 Agent 上下文的内部链接。

按 [D71](M3/m3_transcript_history_design.md#d71run-通知使用完整轻量快照水体区分五种展示状态)，run SSE 每次通知发送完整轻量状态快照，前端更新状态区。快照除了 run、实际 session 和保存情况，还须覆盖 BFS 范围内水体的参与/执行情况；地图不只显示已经创建 session 的水体。

| 水体状态 | 前端颜色 |
|---|---|
| BFS 范围内但不参与推理 | 半透明灰色 |
| 参与推理但尚未开始 | 灰色 |
| 正在推理 | 蓝色 |
| 推理完成 | 绿色 |
| 推理出错 | 红色 |

颜色由前端映射，状态来自本次 BFS/推理范围、调度和真实 session 事实；前两种可能没有 session，不能补造空会话或因此建立过程 SSE。真实 session 的 pending/running/completed/failed/cancelled 仍各有原义，水体状态不等同于 session 状态枚举。用户举例的排除原因是上游没有带检测站点的水体，具体判断由 M4 定型。取消后的颜色和标签按 [D72](M3/m3_transcript_history_design.md#d72取消后的水体展示延后到前端设计) 留到 M9 前端设计，后端保留真实 cancelled 状态，本轮不确定其具体呈现。历史只使用本次已保存事实，完整请求/配置/Agent 正文不进入 run 通知，也不因颜色增加后端存储字段。

按 [M3 D44](M3/m3_transcript_history_design.md#d44query-历史列表分页按当次地图进入连续会话记录) 及 D50，前端提供按 run 分页的 query 历史列表。用户选择某次 run 后，显示当时地图上的标点和连线，点击水体标点再进入该 run 下对应 session 的多层会话记录。Agent 过程连续展示，不设置记录的上一页/下一页；同一 query 的不同 run 不合并，同一水体在不同 run 的记录不混合。历史读取使用已保存的当次消息，不重新分析 query 或用当前结果替换旧结果。

水体完整分析记录必须在用户选中该水体后展示；未选水体时不展示混合的全 run 过程。最终汇总从 run 汇总入口读取独立 `run_summarizer` session，也不能把所有水体 transcript 混入汇总过程。按 [D64](M3/m3_transcript_history_design.md#d64run-详情提供会话信息按-run-与-session-定位水体过程)，run 详情提供已创建 session 的 session_id、角色、适用 COMID 和自身状态；前端选中水体或汇总后取得对应 session_id，以 run_id + session_id 读取结果和完整过程，后端校验归属。首版不另提供按 COMID 读取同一 Agent 过程的第二套入口，具体 URL 按 D65。

该对应信息来自原 session，后端不另存映射表或重复目录。历史只显示 PostgreSQL 已保存的会话信息，未创建节点不补造空会话；解析和汇总 session 以角色区分且不绑定 COMID，不显示为地图水体。定位方式不改变完整过程及公共引用读取，也不要求在 run 详情提前加载所有 Agent 消息。

按 D51，问题解析 Agent 也拥有属于该 run 的独立 session，保存输入、调用过程与结构化输出。它不绑定水体 COMID，不作为地图上的水体节点；其输出可以包含目标位置和 COMID。该归属确认不新增独立前端入口，也不改变先选水体再阅读完整分析过程的交互。

按 [D53](M3/m3_transcript_history_design.md#d53四张核心表会话状态与两层记录归属)，run 与各 session 保留各自必要的状态和时间，前端不能从 run 状态直接推断所有 Agent 都已开始、完成或失败；状态枚举和 M3 返回字段以当前 OpenAPI 为准。原请求、run 创建事件、BFS 等公共过程属于 run，解析与水体 Agent 的消息属于各自 session。角色和水体归属由后端从 session 等权威事实装配，前端按原引用读取完整过程，不将全部公共内容归到解析 session，也不复制成每个水体的过程。

按 [D54](M3/m3_transcript_history_design.md#d54所有业务表的每条记录都保留时间信息) 和 [D55](M3/m3_transcript_history_design.md#d55通用时间最多两列occurred_at-复用创建时间)，每条数据库记录保留时间。run/session 表使用 created_at、updated_at，不可变原记录/大正文表仅使用 created_at；transcript 的 API occurred_at 从原创建时间装配，仍表达原过程发生时间。前端不因数据库列精简更换字段或排序规则：历史继续按 run 创建顺序分页，过程按 transcript_sequence 展示。读取和补写不更新时间，不能把 updated_at 一律当作完成时间。

流式消息按 [D82](M3/m3_transcript_history_design.md#d82消息开始时固定过程序号和创建时间) 在开始生成时同时确定 item_id、transcript_sequence 和 created_at，occurred_at 复用该时间。消息结束后保持原位置，保存或补写不改时间；其他记录可能先保存，历史只显示实际保存内容并按原序号排列，不因较大序号已存在而推断整个过程已保存。

按 [D59](M3/m3_transcript_history_design.md#d59agent-准备执行时创建-session)，水体等所有上游推理完成、准备开始自己的 loop 时才创建 session；问题解析 session 同样在准备解析时创建。拓扑中尚未执行的水体只有 run 的相关事实，没有 Agent 会话可读，不应展示成空会话；已经创建的会话失败或取消仍可按可见性规则读取已有记录。严格历史读取可能落后于 live state，不能把数据库暂未返回 session 当作从未执行的证明；具体响应表示随 DTO 定型。地图的组织和画法仍由前端设计。

按 [D60](M3/m3_transcript_history_design.md#d60agent-session-使用五种执行状态)，解析与水体 session 统一使用 pending（已创建、准备执行）、running（执行中）、completed（执行完成）、failed（准备或执行失败）、cancelled（执行取消）。pending 不表示等待上游。前端保留每个 session 的原终态，不因 run 后续取消而把已完成 Agent 改为取消。保存进度和警告按 run 统一展示，session 没有 completed_with_persistence_error，completed 也不表示整个 run 完成或全部过程已保存。

按 [M3 D46](M3/m3_transcript_history_design.md#d46地图连线由前端设计后端保存原消息)，地图标点和连线的含义、方向与画法由前端设计。后端保存并提供原消息、其中已有的真实业务内容及引用；本轮不增加地图专用字段、快照或绘图接口，不把上下游箭头作为已确认规则。前端基于这些历史消息组织展示，既有水体/操作归属和公共上下文引用继续保留。

skill、tool、hook 等按语义组织为默认折叠的块，用户点击后展开具体内容。**折叠状态由内容类型和用户操作决定，与内容大小、是否已下载或是否在 API 中内联无关。** transcript 大内容存储和传输策略负责按需取数据，不决定页面结构。

“总体预测值”已由 M6 收敛为**跨多个参数的分类综合风险判断**，等级为 `none / low / medium / high / indeterminate`，并附 Agent 理由、confidence 和独立的 availability。它不是 0–100 分，不由前端或后端按参数最大值/均值计算，也不能用总体置信度、分时段均值或存储 preview 临时代替。具体设计见 [M6 设计决策](M6/m6_single_agent_reasoning_design.md) 和 [M3 D21](M3/m3_transcript_history_design.md#d21按水体进入预测与推理上下文展示规则独立于存储大小)。

按 [M3 D22](M3/m3_transcript_history_design.md#d22单份-session-transcript按水体与调用关系关联读取)，一次分析保留一份完整 transcript，水体记录明确关联 COMID，工具开始/结束及相关 hook 通过稳定调用 ID 配对。水体结果关联其完整过程，公共上下文通过引用读取；前端按这些关联组织上下文，不靠日志文本或记录相邻关系猜测归属，不将每个水体的过程视为独立复制的全文。M3 DTO 以当前 OpenAPI 与 API 指南为准；M4–M9 只扩展所属真实业务字段。

## 1. 事实来源与版本边界

1. 普通 HTTP 的路径、方法、request/response schema 和 status code 以运行时 `/openapi.json` 为可执行事实源。
2. SSE framing、回放、keepalive、断线和关闭语义以 [M3 API 与搭建排障指南](M3/m3_transcript_history_api.md#5-两层-sse) 为准；OpenAPI 只能描述握手响应，不能完整表达流行为。M2 指南仅保留历史证据。
3. 本文规定客户端行为和跨接口不变量。标为“尚未实现”或“待定型”的内容不得出现在生产前端调用中。
4. M4 会在当前 M3 create request 上增加真实 AnalysisContext 输入，M6 会扩展真实 reasoning/result payload；前端不得为历史 M2 结构保留兼容分支。
5. 发送请求时只包含 OpenAPI 声明的字段。消费事件时按 `schema_version` 和 `type` 解析；不支持的版本或类型不得被解释成其他业务状态，应保留关联 ID 并产生可排查的客户端诊断。

## 2. 跨接口必须保留的身份与安全边界

| 字段 | 身份范围 | 前端要求 |
|---|---|---|
| M2 接口中的 `session_id` | 整次分析的旧命名 | 仅解释历史 M2 协议；在 D50 新模型中对应 run 层，当前字段和路径以 M3 OpenAPI 为准。 |
| `run_id`（D50/D51 新模型） | 一次用户提交的完整分析 | 关联历史列表、整次分析状态、问题解析 session 与水体 sessions。 |
| `session_id`（D50/D51 新模型） | 单个 Agent 的独立上下文和完整 loop | 问题解析与水体推理各有独立 session；选中水体后按 D64 定位其结果与完整消息，路径按 D65。 |
| `agent_role`（D52） | 问题解析或水体推理职责 | 用于识别职责，不作为唯一执行身份；角色的具体枚举编码随最终 DTO 定型。 |
| `operation_id`（D32/D52） | 一次模型、工具等调用 | 开始、结束及直接关联 hook 使用同一 ID；同名调用分别保留自己的 ID，读取与补写不改变身份。 |
| `request_id` | 一次 HTTP 请求 | 从 response 的 `X-Request-ID` 或 error body 读取并保留；由服务端生成，客户端提供同名 header 也会被忽略。 |
| `error_id` | 一次公开错误 | 与错误提示一起保留，供后端回溯异常日志；不能用公开 message 代替它排查。 |
| `sequence` | 单个 session 的公开 SSE 事件 | 与 `session_id` 组成事件身份，用于排序、去重和续传；只接受正整数。 |
| `item_id` | 单条 transcript 记录；也是流式消息身份 | 按 D81 在消息开始生成时分配，片段和最终正文复用；前端持续更新同一消息，内存读取、保存和补写保留身份；不按 UUID 排序，也不据此判断已保存。 |
| `transcript_sequence` | run 内统一的过程追加顺序 | 按 D56，查询 session 保留筛选后的原序号，缺口正常，不重新编号；不是 SSE cursor，不得写入 `Last-Event-ID`，Agent 记录不分页。 |
| `content_ref` | 原 transcript item 的全文 | 按 M3 D57 统一使用 run_id + item_id，公共记录与 session 记录共用；沿用后端给出的引用读取，不另行拼接公开 content_id 或底层存储路径。 |
| `scope` | transcript 的水体与操作归属 | 配合所属 run/session 及适用的 target_comid/operation_id 组织过程，Agent 执行不再另用 agent_id，公共记录不强行归到某个水体；M3 DTO 层级以 OpenAPI 与 API 指南为准。 |
| `correlation` | transcript 的 HTTP 请求与错误关联 | 通过适用的 request_id/error_id 排障，不代替水体或操作归属。 |

前端错误上报至少应包含 HTTP status、route、`request_id`、适用时的 `session_id`、`error_id` 和最后处理的 event `sequence`。按 [M3 D26](M3/m3_transcript_history_design.md#d26日志保留-query-正文仅对其中凭据脱敏)，日志允许保留业务 query 正文，但须移除其中凭据；不能因字段名为 query 就整段隐藏。不得为了排障输出完整 request body、Authorization、Cookie、API key 或未脱敏的服务端响应。

所有时间戳作为 UTC instant 解析；用户时间语义和窗口标签按 session 固化的 `analysis_timezone` 展示。全局设置变化不得改写已有 session snapshot。DST 可能产生 5/7 小时或 23/25 小时的实际时长，前端不得用固定毫秒数推导“当地 6h/24h”窗口。

## 3. 历史基线：M2 session 生命周期

### 3.1 创建分析

历史接口为 `POST /api/analysis/sessions`，成功返回 `201`。M2 request 只有：

- `query`：必填，trim 后必须非空。
- `time_resolution`：可省略，只允许 `6h`、`12h`、`24h`；显式 `null` 非法。
- 未知字段非法；当前不接受 `auto`、timezone、日期、位置、参数、provider、API key、mode 或 memory mode。

创建成功后立即保存返回的 `session_id` 并建立 SSE 订阅。POST 固定返回创建时的 `pending` snapshot，workflow 可能已在后台进入 running 或终态，因此不得把 POST response 当作后续状态事实。

configuration 是 session 创建时固化的事实：显式传 `time_resolution` 时 source 为 `frontend`，省略时由服务端使用默认值并返回 source `default`；`analysis_timezone` 来自服务端配置。前端不能用浏览器本地时区或自己的默认粒度覆盖 response。

历史 M2 没有实现持久化幂等创建。当前 M3 已提供 `Idempotency-Key`，新前端必须按 §4.1 管理主动提交与同次重试；本段只解释旧协议风险。

### 3.2 状态与展示

M2 不公开 `session_started`。前端不得依赖该事件，也不需要区分 pending 与内部 running：

| snapshot / event | UI 语义 |
|---|---|
| `pending`、`running`、`session_created` | 分析中 |
| `completed`、`session_completed` | 已完成 |
| `failed`、`session_failed` | 失败 |
| `cancelled`、`session_cancelled` | 已取消 |
| `completed_with_persistence_error` | M3 语义：结果已完成，同时展示持久化 warning；不能显示为分析失败。 |

每个 session 最多有一个 terminal event：`session_completed`、`session_failed`、`session_cancelled` 三者之一。终态只能由 snapshot 或 terminal event 确认，不能根据 POST 返回、SSE 暂时断开、EventSource `error`、流 EOF 或页面卸载推断。

### 3.3 SSE 订阅、回放和关闭

接口为 `GET /api/analysis/sessions/{session_id}/events`。

- 首次订阅不发送 `Last-Event-ID`；服务端先回放已有事件，再等待新事件。
- lifecycle frame 的 SSE `id` 等于 JSON envelope 的 `sequence`，SSE `event` 等于 envelope 的 `type`。
- event envelope 的 `schema_version` 当前为整数 `1`；`payload` 由 `type` 决定，不是任意 map。
- 15 秒无业务事件时可能收到 `: keepalive` comment。它没有 ID 或 JSON data，不更新 cursor、不进入 UI timeline，也不表示分析进度。
- 连接意外断开不会取消 session。浏览器 `EventSource` 可以自动续传；自定义客户端必须用本 session 最后确认的 sequence 发送 `Last-Event-ID`。
- 服务端只补发 cursor 之后的事件。前端仍应按 `session_id + sequence` 幂等消费，重复事件不能重复改变 UI 或追加结果。
- 收到 terminal event 后关闭本地 EventSource。终态已被 cursor 消费完时服务端返回无 body 的 `204`；终态 cursor 落后时先补发缺失事件再关闭。
- `400 invalid_event_cursor` 应清除损坏的本地 cursor 后重新订阅；`409 event_cursor_ahead` 表示很可能混用了 session/cursor，不能继续增加 cursor 或盲目重试。
- SSE headers 发出后的传输失败只表现为流中断，不会在流尾追加 HTTP error JSON，也不会把业务 session 自动改为 failed。恢复后从最后成功处理的 sequence 续传。

同一 session 可以有多个订阅者，任一订阅断开都不得触发自动 cancel。页面刷新后恢复订阅，而不是创建新 session 来“恢复”旧任务。

### 3.4 显式取消

取消必须调用无 request body 的 `POST /api/analysis/sessions/{session_id}/cancel`。调用 `EventSource.close()`、路由跳转、页面刷新或网络断开都不会取消分析。

- pending/running：返回 `200` 和 cancelled snapshot。
- 已 cancelled：幂等返回 `200`，不会产生第二个 cancelled event。
- completed/failed/completed_with_persistence_error：返回 `409 session_already_terminal`；前端使用 response 中的 current snapshot 收敛 UI。
- unknown session：返回 `404 session_not_found`。

取消 response 表示取消已被接受，不表示所有在途外部调用都已停止。cancel 后可能继续出现后端收敛日志，但晚到输出不会再进入前端事件或下游 workflow；前端不得等待 `workflow_quiesced`，该事实不是公开 SSE 事件。

### 3.5 HTTP error 与重试

所有 HTTP error 使用固定 envelope：`error`、`session`、`validation` 三个顶层字段始终存在。客户端必须：

- 按 HTTP status 与 `error.code` 分支，不解析或匹配英文 `message`。
- 只把安全 `message` 用作用户提示；不期待公开响应包含 traceback、内部路径或原始异常。
- 字段错误按 `validation[].field` 路径定位；路径元素可能是字符串或整数。
- 如果 error 带 `session`，用该 snapshot 收敛本地状态。
- 只有 `error.retryable=true` 才能进入重试判断；同时还必须确认该操作自身幂等。M2 create 即使遇到网络错误也不满足此条件。
- 保留 response header 的 `X-Request-ID`。公开错误中的 `error.request_id` 是相同请求的排障身份。

当前 M3 关键 error code 和处理方式以 [M3 错误与排查字段](M3/m3_transcript_history_api.md#7-错误与排查字段) 为准；M2 错误码只解释本节历史协议。接口不自动重定向尾斜杠，前端必须使用 OpenAPI 中的准确路径和方法。

## 4. M3 接入约定与分阶段状态

本节来自 [M3 设计决策](M3/m3_transcript_history_design.md) 和 `M3-C01`–`M3-C20`。创建、统一 HTTP 读取、历史分页、取消/晚到审计、手动补写、启动修复、live 清理与两层 SSE 的既有证据保留；Milestone 17–18 已本机修复 Windows 正式入口与统一脱敏/性能缺陷，Milestone 19 已完成最终集成复审。本节保留已确认的产品行为和公共契约，不因外部三平台证据仍为 `partial` 而弱化安全或取消保证。后续产品能力仍按各自 Milestone 状态接入；已确认行为的变更须同步运行时 schema、对应设计与契约。

### 4.1 创建分析：Idempotency-Key

**每次主动分析生成新 key；只有同一次提交的重试才原样复用 key 和请求体。**

设计来源：[M3 设计草稿 D11–D16](M3/m3_transcript_history_design.md#d11可选幂等键前端按一次主动提交管理)。对应契约：[M3-C09](deferred_milestone_contracts.md)。

接口：`POST /api/analysis/runs`

请求头：`Idempotency-Key: <本次主动提交生成的随机 UUID v4>`

后端允许省略该请求头；前端按上面一句话管理 key，不需要掌握额外的字段规范化规则。用户再次提交相同 query，甚至整个请求体完全相同，也必须生成新 key。不得按 query 缓存或复用 key。

格式与范围按 [M3 D34](M3/m3_transcript_history_design.md#d34创建-key-使用-uuid-v4在同一数据库的创建接口内唯一)：后端按 UUID 值识别，在同一套数据库的创建分析接口内唯一。提供了请求头却不是合法 UUID v4 时返回 `422`，不会按省略 key 处理；前端每次主动提交生成新值，不使用固定样例值或 query 计算出的 key。

| 前端动作/请求 | 后端设计行为 |
|---|---|
| 首次提交，携带新 key | 创建一个新 run。 |
| 请求超时或连接中断后，原样复用 key 和请求体 | 若原请求已提交，返回原 run，不重复执行；若原请求尚未创建成功，则按创建流程处理。 |
| 分析已经结束或服务已重启，原样复用 key 和请求体 | 返回原 run，并沿用原创建配置，不再次执行分析。 |
| 原 key 搭配不同请求体 | 返回 `409 Conflict`，不创建新 run。 |
| 用户主动再次分析，即使 query 和请求体完全相同，也生成新 key | 创建新 run，执行新一轮分析。 |
| 未携带 key | 每次请求都创建新 run，重试可能重复创建。 |
| 携带的 key 不是合法 UUID v4 | 返回 `422`，本次校验失败不创建 run。 |

首次创建成功与幂等重试成功都返回 **`201` 和首次创建的回执**：相同的 `run_id`、创建时间、创建时的 `updated_at`、configuration 和 `pending` 状态。这个 `pending` 只属于创建回执，不能用它覆盖已经收到的后续状态；当前进度通过后续状态读取获取。幂等命中不会再次启动分析或追加第二个 `run_created`。`X-Request-ID` 仍由服务端为每次 HTTP 请求重新生成。

同 key、同请求体同时到达时，后端协调出一次创建，其余请求按 [M3 D35](M3/m3_transcript_history_design.md#d35同-key-创建等待默认最多-5-秒) 默认最多等待 5 秒，可配置。确认创建成功立即返回上述回执；确认回滚后允许一个等待者接手创建。等待超时返回 `503`、`code=idempotency_wait_timeout`、`retryable=true`，前端仍原样复用 key 和请求体。这个响应只结束等待请求，不取消原创建或分析，也不代表原请求一定未创建；不能据此换 key 或把已有 run 显示为分析失败。前端依据响应处理，不把 5 秒当作整个创建请求或分析的时长上限。

若 run 已创建，但后台分析任务启动失败，POST 仍返回 `201` 与原创建回执；后续状态读取呈现 `failed / workflow_start_failed`，并保留 `error_id`。同 key 重试不会重新启动分析，用户主动再分析需生成新 key。若进程直接崩溃，下次启动按 D61 分别处理数据库中遗留 pending/running 的 run 和 Agent session，原因记录为 service_interrupted。

### 4.2 前端接入提示

M3 的幂等记录不自动过期，随 run 历史保留。分析结束、服务重启和后端内存清理不会使旧 key 变成可用于新分析的 key；用户主动重新分析仍须生成新 key。将来删除历史后的 key 行为在 M9 admin cleanup 中定型。

保存首次提交的 key 和请求体供重试使用；用户主动发起另一轮分析时再生成新 key。收到 `409` 时停止自动重试并提示请求不一致，不自动换 key 后静默提交。

例如：第一次提交生成一个 UUID v4，响应超时后原样复用它和请求体；用户稍后主动点击“再次分析”，另生成一个 UUID v4。

`Idempotency-Key` 与现有 `X-Request-ID` 用途不同：前者由客户端管理，关联同一次提交的多次 HTTP 尝试；后者由服务端为每次 HTTP 请求生成，用于排障，不能作为幂等键的替代品。

### 4.3 后续前端与 SSE 细节

- 页面刷新后的客户端 key 保存方式；格式与唯一范围已按 D34 确认。
- 创建提交结果不确定、HTTP 中断的处理，以及前端 HTTP 重试次数和退避策略；同 key 等待上限及错误码已按 D35 确认。后端数据库补写的重试预算不等于前端 HTTP 重试预算。
- M3 历史与实时 HTTP 读取 DTO 已在 Milestone 6 冻结；水体按 D64 定位、路径按 D65，列表仅支持 limit/cursor。M3 Milestone 7 实现 `410 live_state_gone` 与 history_url，Milestone 8 已补齐正常 live grace 清理，见 4.6。基于 fetch 的统一客户端由产品阶段 M9 实现，见 4.7。

后续设计确认应同时更新本文与 M3 详细设计。已实现 HTTP 行为以运行时 OpenAPI 为准，两层 SSE 的帧与时序另以 M3 API 指南和可执行 fixture 为准。

### 4.4 History 与 transcript

M3 提供 run 历史列表、run 详情、Agent 详情与结果、Agent 完整过程及原 item 全文读取。产品入口仍按“选择历史 run → 地图水体 → 预测与总结 → 推理上下文”组织；当前与历史按 D42/D43 共用路径，不增加 /current 路由。

以下路径按 [D65](M3/m3_transcript_history_design.md#d65接口以-runs-为根嵌套读取独立-session) 已实现，并已直接替换旧整体 session 路径。完整 HTTP DTO 与两层 SSE 事件见 M3 API 指南、运行时 OpenAPI 和事件 fixture。

| GET 路径 | 行为 |
|---|---|
| /api/analysis/runs | PostgreSQL 历史列表，按 created_at/run_id 倒序游标分页，返回 items/next_cursor。 |
| /api/analysis/runs/{run_id} | run 详情、存在时的原结果及 D64 会话简要信息。 |
| /api/analysis/runs/{run_id}/sessions/{session_id} | 所选 Agent 的详情与实际结果，校验所属 run。 |
| /api/analysis/runs/{run_id}/sessions/{session_id}/transcript | 一次返回所选 session 的完整有序过程及显式公共引用，不分页。 |
| /api/analysis/runs/{run_id}/transcript/{item_id}/content | 原 item 全文，覆盖内联/独立大正文及 run/session 两类归属。 |

创建、取消和手动补写已分别使用 POST /api/analysis/runs、POST /api/analysis/runs/{run_id}/cancel 与 POST /api/analysis/runs/{run_id}/persistence/retry。D69 的两层 SSE 已实现为 run 完整轻量快照与 session 可续传过程流；此前整次分析的 session_id 已映射为 run_id，独立 Agent 的 session_id 保留，不维护旧 HTTP 路径兼容别名。

query 历史列表按 [M3 D47](M3/m3_transcript_history_design.md#d47query-历史列表按创建顺序游标分页)：limit 省略为 50，允许整数 1–200，非法值返回 422。首次不传 cursor，后续原样传回 next_cursor；返回 items 与 next_cursor，不返回总条数。next_cursor=null 表示本次已到末尾，无记录时 items=[]；数据库失败显示错误，不能显示成正常末页。状态/updated_at 更新不改变创建顺序，新分析通过刷新首页查看。

按 [M3 D49](M3/m3_transcript_history_design.md#d49首版历史列表只提供分页参数)，首版 query 历史列表只提供 limit/cursor，前端按创建顺序浏览，不调用尚未提供的关键词、状态或日期筛选接口。query、status 等字段仍正常展示；水体完整过程的独立读取能力保持原约定。

游标按 [M3 D48](M3/m3_transcript_history_design.md#d48历史游标编码位置不保存分页状态) 由后端将版本与上一页末条记录的 created_at/run_id 编码成字符串。前端原样传回，不解析或按展示时间重建；服务端不保存分页状态或页面快照，普通重启后仍可使用支持版本的游标。无法解析、字段格式非法或版本不支持时返回 422，不静默返回首页；前端应明确处理错误，需要重新浏览时移除 cursor 从首页开始。该游标不与 SSE cursor 或 Agent 记录混用。

上述读取路径已经实现。详情、timeline 和全文按 [M3 D43](M3/m3_transcript_history_design.md#d43同一路径通过可选-view-选择严格历史读取) 使用可选 view 参数，普通请求不需要判断数据存放位置：

| view | 前端约定 |
|---|---|
| 省略或 auto | 默认自动选择来源，普通结果页与手动补写后的读取无需传参。 |
| history | 只读取 PostgreSQL 已保存事实，persistence=null，保留记录中的原错误和警告。 |

非法 view 返回 422。历史查阅从详情到 timeline、单条全文和公共引用读取均携带 view=history；SSE 410 的历史入口也使用该模式。原 run/item 全文身份及 content_ref 不变，前端在后续请求中保持所选 view，不期待引用自身另存一份模式专属身份。前端必须遵守：

- view=history 只展示 PostgreSQL 已持久化事实，不从内存补齐正文或实时保存状态；默认自动读取可能返回领先数据库的内存内容，不能把它伪装成已保存历史。历史模式保留既有审计可见性，自动模式在转读 PostgreSQL 后仍遵守当前读取的取消后传播限制。
- 按 [M3 D25](M3/m3_transcript_history_design.md#d25每批-transcript完整内容与关联原子提交)，已保存记录与同批全文、正文引用及适用状态更新一起提交；写入不会留下只有预览却缺少对应全文的半条历史。读取接口本身发生错误时仍按错误契约处理，不能用 preview 冒充全文。
- 后端按 D53 采用四张核心表并保留 D33 去重原则；前端沿用结果和原 item 引用，地图、结果与上下文阅读流程保持原义。run 汇总可以表达整个分析的结论，并引用各水体原结果。按水体取得独立 session 的完整过程，再读取显式关联的公共上下文，不能只显示最终总结引用的几条记录。后端按需生成 content_ref/scope/correlation 返回对象，前端不依赖它们在数据库中的保存形式。
- 当前分析的结果/上下文按 [M3 D24](M3/m3_transcript_history_design.md#d24当前结果与上下文统一读取后端选择内存或数据库来源) 使用统一读取路径：后端在 live state 存在时读内存，内存释放后读 PostgreSQL。前端沿用 D57 的原 run/item 全文引用，不自行判断或切换存储来源；默认读取与只返回已保存事实的历史读取在同一路径承接不同语义，DTO 以运行时 OpenAPI 为准。
- 当前与历史读取共享 DTO、类型解析和内容装载语义，前端复用对应解析/展示；来源进度、持久化警告和取消后的可见性仍按各自入口处理。当前状态由后端状态机提供，前端不通过回放公开事件推导所有状态，也不等待新增 session_started 事件。历史列表既有 configuration 仍由后端从已保存的创建事件装配。
- 按 [M3 D27](M3/m3_transcript_history_design.md#d27进入可读取的-live-buffer-前脱敏持久化边界再次执行)，内存内容在可读取前就完成凭据脱敏，数据库写入再次执行同一规则。前端取得的正文、preview 和全文都是脱敏版本，普通 query/业务正文仍保留；展开完整内容不会恢复被替换的凭据。
- 列表和结果详情不自动下载完整 prompt、tool result 或原始输出。timeline 接口可以返回 preview/reference 以减少传输，展开时再加载尚未取得的完整内容；已返回的 inline 内容仍按语义折叠。
- skill、tool、hook 等操作块无论大小都默认折叠，由用户主动展开；存储 preview 不代替面向用户的推理概述或总结。
- 按 [M3 D23](M3/m3_transcript_history_design.md#d23默认-64-kib-内联超出后完整保存并返回-2-kib-预览)，默认单条脱敏后内容不超过 64 KiB 时内联，超出后返回完整内容引用、完整字节数和最多 2 KiB 的首尾预览正文（首尾各最多 1 KiB）。阈值可由后端配置，前端依据实际 inline/reference 读取，不硬编码阈值判断展开方式。预览明确标注省略、保留完整字符，不当作完整 JSON 或总结解析；全文仍可按引用读取。
- 按 [M3 D45](M3/m3_transcript_history_design.md#d45水体过程一次返回完整记录列表大正文按需读取)，进入水体推理上下文时，一次读取该次分析中该水体的完整有序记录列表，按 transcript_sequence 升序排列；Agent 记录不使用 cursor、limit 或下一页字段。小正文随列表返回，大正文保留 preview/content_ref，展开时读取全文，公共上下文仍完整关联。query 历史列表独立按 D47 分页。
  - 按 [D56](M3/m3_transcript_history_design.md#d56每个-run-统一分配-transcript_sequence)，run 和各 session 的原记录共用一个过程序列；水体读取可能得到 `4、7、9` 等不连续序号，前端保留原值，不把缺口直接显示为消息丢失或要求补齐其他水体记录。公共引用保持原 item/序号，SSE 独立编号。
- 完整记录列表表示本次所选来源及可见性规则下可读的全部匹配记录，不表示分析已经结束或全部数据已持久化；仍依据业务 status 与 persistence 展示状态。刷新读取当前列表，按原 item 身份与顺序组织，不混用 SSE sequence，不按数量裁掉过程或仅保留总结引用。
- 公共字段按 [M3 D29](M3/m3_transcript_history_design.md#d29公共-envelope-的字段命名与职责)：发生时间为 occurred_at，按 D55 由原记录 created_at 装配，仍保留原生命周期时间。按 content_type/content_schema_version 读取内容；外层不再提供同义 type，内部原 LifecycleEvent.type 保留。内联、引用和不可解析 content 的 DTO 已在 Milestone 6 冻结。
- 身份与全文引用按 M3 D31/D57：同一 item 在内存和数据库中沿用 UUID，通过原 run_id + item_id 引用读取全文，后端核对 item 所属 run；相同正文的不同记录仍是不同 item，前端不能按文本合并过程。session_id 继续用于取得某个 Agent 的完整过程，公共记录不用借用解析 session 身份。
  - 按 [D58](M3/m3_transcript_history_design.md#d58普通过程引用限于同一-run历史证据显式关联)，普通过程引用允许同 run 内跨 session，前端保留原归属；来自其他 run 的证据通过历史来源结构关联，保留原身份与置信度，不展示为本次 session 产生的过程。仍先读取所选 session 全部记录，再读取显式公共引用；允许关联不代表合并所有 session 的消息。
- 执行身份按 [M3 D52](M3/m3_transcript_history_design.md#d52session_id-统一标识-agent-执行取消重复-agent_id)：run_id 标识整次分析，session_id 统一标识一次 Agent 上下文与完整执行，取消同义执行 agent_id；agent_role 表达 `query_parser / waterbody / run_summarizer` 职责，target_comid 仅对水体推理有值。operation_id 继续标识一次模型/工具等调用，同一调用的开始/结束及直接关联 hook 按它配对。前端使用后端 ID 组织过程，不按 COMID、角色、名称或正文合并执行；读取和数据库补写不会改变原 ID。
- 元信息按 [M3 D30](M3/m3_transcript_history_design.md#d30元信息采用明确默认值保留真实业务依据)：tags 默认空，rag_eligible 默认 false；confidence/provenance 没有适用值时为 null。前端不能将未知置信度显示为 0%/100%，也不能把缺少来源解释成某个默认来源；RAG 资格不影响普通历史和水体过程的可读性。
- 正文已有的置信度/来源由后端按类型和对应层级提取，前端不在公共元信息中另行计算或维护同义值；整体、参数、时间窗和证据置信度分别按其业务含义展示，多来源信息保留原结构。
- 按 [M3 D28](M3/m3_transcript_history_design.md#d28m3-首批注册四类明确-schema-的内容)，M3 首批内容为 analysis_request、lifecycle_event、error、runtime_fact；配置及事件已经携带的错误通过原记录查看，不期待重复 item。runtime_fact 属于内部过程记录，不作为新的 SSE 事件或业务终态处理。LLM/tool/skill/hook 和真实预测结果在后续阶段接入，前端以届时注册的类型与版本读取。
- lifecycle transcript item 复用原 LifecycleEvent，保留原 event version、sequence 和 occurred time；不得重新编号或按写入完成时间排序。
- 未知 content type/version 应保留 item identity、时间、类型和版本并显示“不支持”，不能当作正常结果；已知版本损坏时显示安全错误和 `error_id`，其余 timeline 继续可读。
- 数据库整体读取失败是错误，不能渲染成“暂无历史”。M3 尚未产生的业务字段也不能由前端从 query 猜测或填充 placeholder。

统一路径及默认自动行为按 D42，严格历史模式按 D43，过程完整列表按 D45，query 历史列表分页与参数范围按 D47–D49，均已在 Milestone 6 实现并由 OpenAPI/测试验证。Milestone 7 已提供 session SSE 后端契约，正式前端客户端仍按计划由 M9 接入。

### 4.5 持久化异常与取消后的内容

- 当前 OpenAPI 中只有 run 可以进入 `completed_with_persistence_error`；Agent session 始终只使用五态 pending/running/completed/failed/cancelled。run 的保存进度和 warning 不新增或改写 session 状态，session terminal event 不携带 run 级保存 warning。
- 按 [M3 D36](M3/m3_transcript_history_design.md#d36单次数据库写入共用-5-秒执行预算)，后端单次数据库写入默认采用 5 秒执行预算，可配置；该预算不表示整个分析应在 5 秒完成，也不替代后台重试与终态交付窗口。前端仍依据实际状态、warning 和 HTTP 错误处理，不自行倒计时将分析标为失败。
- 按 [M3 D38](M3/m3_transcript_history_design.md#d38终态选定前检查既有写入后续保存失败保留终态)，后端在成功分析结束时固定此前已产生的必需结果、过程和状态的保存范围；该范围在既定交付窗口到期时仍未保存完整，则 run 为 `completed_with_persistence_error`，仍通过唯一 `run_completed` 交付当前结果，并带安全 warning/`error_id`。前端应显示结果和持久化警告，不能归类为 `run_failed`；各 Agent session 保留自己的原五态和 terminal event。
- run 的 completed 表示业务分析完成，不保证完成事件自身和所有后续收尾记录均已保存。它们的保存随后失败时，后端保持原 run 终态，通过当前读取接口及 D69 的 run SSE 通知保存情况和警告/`error_id`；前端保留完成结果并展示该警告，不能自行改写业务终态。保存变化不产生第二个 run terminal event，也不产生 session terminal event，不能在业务终态到达时直接认定 run 通知流结束；关闭时机和通知 DTO 以 D70 及当前 M3 API 指南为准。
- 内存仍保留时，统一读取入口可读取当前结果及尚未保存的完整上下文；展开大内容不要求 PostgreSQL 已恢复。成功读取不代表保存成功，仍展示后端提供的持久化警告。进程退出后未保存内容可能丢失，不能把本地读过的内容标成后端已保存。
- 后台补写恢复后不改变原 run 终态，也不再发送第二个 `run_completed`。前端不得等待 run 状态从 `completed_with_persistence_error` 自动变成 `completed`；session 状态和 terminal event 同样不因 run 保存恢复而变化。
- 按 [M3 D39](M3/m3_transcript_history_design.md#d39自动补写耗尽后允许用户手动开启新一轮)，每轮 5 次自动重试耗尽后停止。M3 提供“重试保存”接口，M9 在保存警告处接入按钮：仅用户主动操作可在后端仍保留待写内容时开启新一轮，首次立即尝试，失败后最多自动重试 5 次。数据库恢复、页面刷新或新内容到来不自动重置耗尽预算。
- “重试保存”只补写原 run 及其 sessions 的原记录，不重新分析、不改变任一层终态。执行或退避中的重复点击不启动额外任务、不重置预算；触发成功不等于已经保存，前端继续依据实际读取更新保存情况。内存已丢失时不能恢复未保存内容，没有待写内容时不做空写入；按钮依据 D40 的保存状态，路由和 HTTP 响应按 D41。
- 完成前等待保存期间仍可立即取消；取消先被后端接受就保持 cancelled，完成先被接受则沿用终态冲突响应，前端以服务器返回的状态为准。
- cancelled session 的已产生在途内容可以进入历史审计，但不得出现在 live SSE 或继续触发下游结果。不会增加 `produced_after_cancel` 一类字段，前端不得依赖该标记。
- 服务重启按 [D61](M3/m3_transcript_history_design.md#d61重启时分别处理-run-与-session-的遗留状态)，数据库中遗留 pending/running 的 run 和 Agent session 分别标记为 failed，错误码为 service_interrupted；各层已保存的终态保持不变。run 已终态时，其下遗留非终态 session 仍需处理，所以前端不能从 run 终态推导所有 Agent 的状态。保留已保存过程，不补造未保存会话；第一版不自动恢复 workflow，也不得伪造可继续执行的 active SSE stream。
- 启动按 [D62](M3/m3_transcript_history_design.md#d62启动检查全部成功后才接收请求)，只有连接检查、schema 检查及遗留处理全部成功，后端才接收请求；失败时本次启动退出，由后端诊断说明修复方式，前端不能将服务不可用解释为空历史。服务已启动后的运行中持久化故障继续按原保存警告和补写约定处理，新的创建请求仍以原创建回执及明确响应为准。
  - schema 按 [D63](M3/m3_transcript_history_design.md#d63schema-版本严格匹配统一由-alembic-管理) 要求与后端代码版本配套，由 Alembic 统一管理；不匹配时后端启动失败，修复由部署配置及独立命令完成。前端仍按原 content/event 版本解析历史内容，不因数据库结构版本检查而假定所有历史正文都是最新格式。

按 [M3 D40](M3/m3_transcript_history_design.md#d40实时保存信息按读取来源生成不另存重试状态)，当前读取选择 live state 时，响应附带 run 级 persistence 运行信息；D69 的 run SSE 从相同写入器事实通知其变化。两种表示共用语义，不修改业务 status 或原创建回执，不维护另一套保存状态，也不因通知再追加同义持久化记录。

| persistence.state | 前端行为 |
|---|---|
| up_to_date | 显示目前已产生的必需内容及状态均已保存；不据此保证后续内容或判断内存应当清理。 |
| pending | 表示尚未全部保存，包括等待消息定稿、待写入、写入和退避；不表示数据库正在执行写入，不通过按钮重置这一轮。 |
| retry_exhausted | 显示自动补写已停止，并提供“重试保存”；按钮不单靠 error.retryable 决定。 |

persistence.warning 使用已有安全 PublicError 结构；正常等待时为 null，不伪造失败。补写恢复后当前 warning 可以消除，原终态事件中的警告仍保留，前端分别表达当前保存情况与当时发生的事件。 按 [D80](M3/m3_transcript_history_design.md#d80未定稿正文计入-pending整理后再触发写入)，生成中已有但未保存的正文也计入 pending；已有 retry_exhausted 及错误不会被新片段清除。消息结束或中断后再整理写入，等待生成不消耗数据库重试，其他可写内容照常保存。

历史读取及当前入口回退 PostgreSQL 时，persistence 为 null，表示本次读取不提供 live 保存运行信息；返回内容是已保存事实，不能据此推断整个分析都已保存完整或当前一定不存在 live state。原记录中的错误/警告照常展示，不显示手动恢复按钮。当前与历史复用 DTO，不另维护 can_retry 或一套前端重试计数。

手动补写接口已按 [M3 D41](M3/m3_transcript_history_design.md#d41手动补写接口只受理恢复不等待保存完成) 实现：

`POST /api/analysis/runs/{run_id}/persistence/retry`

不需要请求体或创建分析的幂等 key，不重传原 query、请求或 transcript 正文。

| 响应 | 前端处理 |
|---|---|
| 202，正文含 run_id 与 persistence | 新一轮已受理或原写入器继续负责，可能仍在等消息定稿；不表示数据库已开始执行或已经保存，保留原业务终态并关注实际保存情况。 |
| 200，相同正文结构 | 当前为 up_to_date，无需补写；按返回状态更新展示。 |
| 409 / persistence_retry_unavailable | 历史仍在，但当前没有可补写的 live state；停止手动恢复，继续查看已保存历史，不能直接推断内容全部已保存或一定丢失。 |
| 404 / 503 / 422 | 分别按 run 确认不存在、数据库读取失败、UUID 校验错误处理；沿用安全错误与 X-Request-ID。 |

此 POST 不自动重发，即使某个网络/服务错误可重试，也不能据此自动开启另一轮。超时或响应丢失后先查询当前 persistence，需要再次开启新一轮时由用户主动点击。已经受理的补写不随 HTTP 断开取消；前端不发送取消请求、不重新创建分析。执行中重复请求可复用，不等于跨轮次自动重发也始终无副作用。 若 pending 仅因消息尚未定稿，调用不会强制结束生成或提前保存半条消息；前端仍只在 retry_exhausted 提供手动入口。

### 4.6 重启或内存清理后的 SSE

按 [D70](M3/m3_transcript_history_design.md#d70两层-sse-分别结束run-流等待收敛与保存完成)，session 流在 Agent 结束并发完可见过程后关闭；run 流在业务已终态、执行已收敛、必需内容及状态全部保存后关闭，不额外等待 60 秒保留期。业务结果按原交付窗口展示，不等通知流结束。保存为 pending 或 retry_exhausted 时继续接收 run 通知，后续补写只更新保存情况，不改写业务终态；用户离开页面可关闭连接，后台执行和保存不受影响。

已确认设计来源：[M3 D17](M3/m3_transcript_history_design.md#d17live-sse-保持进程内续传重启或清理后转历史) 及 D69 两层 SSE，并已由 Milestone 7 实现。正常关闭时机按 D70，成功握手按 D77 统一 200 SSE，不保留原单流 204 分支，以下不存在、数据库故障及转历史的边界保留。

| 场景 | 响应与前端动作 |
|---|---|
| 当前进程仍保留对应 live state | 按 D73，run 重连发送最新完整轻量快照、不补播旧通知，session 按游标补齐未消费的可见过程；run 业务终态不直接代表通知流结束，合法请求按 D77 返回 200 SSE，正常关闭按 D70/D76 发送 stream_end，不返回 204。 |
| 服务重启后的旧 session，或终态内存已完成清理，数据库中仍有历史 | SSE 握手返回 `410 Gone` 并提供历史查询入口；停止 SSE 重连，转到历史详情读取状态和已保存内容。 |
| 内存与数据库均确认不存在该 session | `404`，展示记录不存在；不自动创建替代 session。 |

`410` 不代表分析失败，也不代表历史被删除。不要把它归类为临时网络故障不断重连；数据库读取失败也不能显示为“记录不存在”。已建立 SSE 流发生故障仍表现为中断，握手错误不会追加在流尾。

live state 默认在“终态已确定、workflow 已完全收尾、全部必需数据已确认落库”之后再保留 `60` 秒，时长可由后端配置。持久化失败时不开始计时，补写完整后再获得完整保留窗口。这个窗口不延迟结果交付；前端按实际 HTTP 响应处理，不用本地倒计时推断何时会返回 `410`。详见 [M3 D19](M3/m3_transcript_history_design.md#d19全部完成条件满足后默认保留-live-state-60-秒)。

两层流的正常结束条件按 D70 接入；D19 的清理门槛保留，M3 不增加浏览器已读确认或清理前等待极少数慢连接排空的协议，详见 M3 D20。

页面刷新、网络重连和读取历史都不会重新执行分析，也不触发 cancel。具体历史入口字段、错误码仍需定型；前端按 4.7 的统一客户端读取 HTTP status/body。

### 4.7 统一 fetch SSE 客户端（由 M9 实现）

已确认采用基于 fetch 的统一 SSE 客户端，依据 [M3 D18](M3/m3_transcript_history_design.md#d18前端统一使用基于-fetch-的-sse-客户端由-m9-实现)。M3 提供后端契约与文档，M9 完成前端实现和验收；具体解析库届时选择。

客户端先检查 HTTP status：按 D77，`200` 且 content type 为 `text/event-stream` 时读取事件，并由 `stream_end` 明确正常结束；`410` 转历史，`404` 展示不存在，其余错误遵守统一处理约定。事件应按 SSE 格式解析，不能把一个网络分块当作一个完整事件。

由同一客户端实现管理 run 通知流与 session 过程流。按 [D73](M3/m3_transcript_history_design.md#d73run-重连同步最新快照session-按游标补齐过程)，run 重连同步最新完整轻量快照，不逐条补播旧状态通知；session 使用自身消费游标补齐可见过程并去重，`Last-Event-ID` 按 D78 使用该 session 最后已消费的整数事件编号。run 的状态同步与 session 的过程续传分别处理，页面不各自实现一套连接逻辑。run 业务终态与两类流的结束条件分别处理，不能因业务完成提前漏掉保存通知；关闭时机按 D70，结束通知按 D76 使用 stream_end；编号按 D78、成功握手按 D77，具体控制通知 payload 与错误正文见 M3 API 指南。关闭页面订阅只中止网络连接，不取消分析；用户明确停止时调用 run cancel。HTTP/SSE 重试策略后续单独定义，不与数据库补写预算混用。

当前状态按 [D74](M3/m3_transcript_history_design.md#d74当前状态以-sse-为准不引入快照版本号) 优先采用 run SSE：已收到 SSE 状态后，迟到的 GET 不覆盖它；尚未收到 SSE 时可由 GET 初始加载。GET 继续承担断线核对、结果/完整过程/全文与历史读取，重连按 D73 接收最新快照。不引入 state_revision，也不比较时间戳裁决新旧；严格历史仍只展示 PostgreSQL 已保存事实，不混入 SSE 内容。

首次打开尚未结束的 Agent 过程按 [D75](M3/m3_transcript_history_design.md#d75首次订阅-session-流先发送已有过程再持续推送新增记录) 直接订阅 session SSE，不带游标：后端先按序发送此前全部可见记录，再持续发送新增记录，无需先 GET transcript。断线按 D73 携带已消费游标补齐。大正文仍使用预览/全文引用，公共上下文沿原显式引用读取；已结束 Agent 和历史继续通过统一 GET 取得完整过程及全文。订阅本身不启动或重跑分析。

按 [D76](M3/m3_transcript_history_design.md#d76两类-sse-用-stream_end-明确通知正常结束)，两类流满足各自 D70 条件后都先发送 `stream_end` 再关闭，收到后停止该订阅的重连；未收到即意外断开，按中断处理。用户主动关闭查看仍释放连接，不自动恢复该订阅。`stream_end` 表示流正常结束，不表示分析成功，也不以 session 流结束推断整次 run 已保存完整；业务状态、结果和保存情况仍读原事实。该通知不新增数据库记录或业务终态，已结束时的成功握手按 D77，具体 payload 见 M3 API 指南。

成功握手按 [D77](M3/m3_transcript_history_design.md#d77live-sse-统一返回-200通过-stream_end-结束)：请求合法且 live state 保留时，两类流统一返回 200 SSE。session 已结束仍先补齐未消费的可见记录，再发 stream_end；没有待补记录则直接发 stream_end。run 先发最新完整轻量快照，满足 D70 全部条件时再发 stream_end，否则继续通知。M3 目标路径不保留旧 204 分支；第 3 节中的 204 仅说明历史 M2 基线。清理后仍按 410 转严格历史，错误响应不能当作正常结束。

过程游标按 [D78](M3/m3_transcript_history_design.md#d78session-过程使用整数游标控制通知不编号)：每个 session 的过程事件从 1 递增作为 SSE id，重发保留原号，重连原样回传最后已消费编号到 Last-Event-ID。首次省略请求头，0 也表示起点；格式非法返回 400 / invalid_event_cursor，超过该 session 已产生的末条事件返回 409 / event_cursor_ahead，服务端不静默重置或跳过。按 run/session 分别管理游标；run 通知、stream_end 和心跳没有 SSE id，不更新过程游标，也不引入快照版本号。历史列表游标和 transcript_sequence 不用于此请求头。

流式显示按 [D79](M3/m3_transcript_history_design.md#d79实时增量推送数据库按消息保存一份正文)：session SSE 在模型生成过程中持续推送小片段，按稳定消息身份更新同一条消息，完成消息不能再次追加成重复内容。live state 保留期间，片段按 D78 游标补齐并去重；严格历史读取组装后已保存的正文，不混入未保存片段。数据库按消息保留一份正文，失败/取消保留实际已产生内容及未完成语义，取消后晚到内容不恢复展示。片段推送前须支持跨片段凭据脱敏；生成中的保存状态按 D80，消息身份按 [D81](M3/m3_transcript_history_design.md#d81流式片段与最终消息复用-item_id) 复用 item_id：开始生成时分配，片段与最终正文持续更新同一消息；片段重发按 SSE id 去重，不能因 item_id 相同而丢弃后续片段。提前分配 ID 不触发落库，消息入序与原创建时间按 D82 在开始生成时固定，通用消息状态与装载表示见 M3 API 指南，实际业务消息类型由生产阶段注册。

消息生成情况按 [D83](M3/m3_transcript_history_design.md#d83消息正文使用三种-generation_status) 读取正文中的 generation_status：generating 表示正在生成，仅存在 live；completed 表示该消息正常生成结束；incomplete 表示取消、错误或截断导致未完整生成，实际正文仍保留。后两者随整理后的正文保存。前端不能据此推断 Agent 已结束、run 已成功或内容已保存，也不从 SSE 断线或 stream_end 推断消息生成成功；已经正常生成完的消息不因后续 Agent 失败或 run 取消改为 incomplete。通用消息状态见 M3 API 指南，实际业务消息类型由生产阶段注册。

取消中的正文按 [D84](M3/m3_transcript_history_design.md#d84取消时固定原消息晚到正文另存审计)：服务端接受 run cancel 时，把正在生成的消息固定在当时已接收内容并标为 incomplete，原 item_id、顺序和时间不变。晚到片段合并为引用原消息的独立审计，仅保存新增部分。当前 GET、SSE、全文及引用读取不包含晚到审计，auto 回退数据库也不放宽；view=history 可读取实际已保存的原消息与关联审计，不把晚到正文拼回当前消息。定稿通知按 [D85](M3/m3_transcript_history_design.md#d85消息定稿时发送-message_finalized)，具体字段层级见 M3 API 指南。

消息定稿按 [D85](M3/m3_transcript_history_design.md#d85消息定稿时发送-message_finalized)：session SSE 在一条消息正常结束或中断、正文整理后产生一次 message_finalized，带原 item_id 和定稿消息表示，generation_status 来自原正文的 completed/incomplete；不等数据库保存。前端更新原消息并结束其打字状态，不能据此关闭整个 session 流、判断 run 结束或显示已保存。大正文仍提供预览/全文引用，预览不作为完整正文覆盖已有内容。事件按原 SSE id 补发去重，不另加同义消息；取消定稿只包含边界前原正文，应先于该 session 的 stream_end。

原生 EventSource 不直接暴露握手的 HTTP status/body，参见 [浏览器标准](https://html.spec.whatwg.org/multipage/server-sent-events.html#the-eventsource-interface)；Fetch 可读取状态并流式消费正文，参见 [Fetch 文档](https://developer.mozilla.org/en-US/docs/Web/API/Fetch_API/Using_Fetch)。

消息流按 [D86](M3/m3_transcript_history_design.md#d86消息流划分为开始增量和定稿事件) 分为 message_started、message_delta、message_finalized：开始事件先提供原消息元信息，增量按 item_id 追加本次新增文本，定稿更新同一消息并结束打字状态。三者共用 session SSE 编号续传；增量不反复携带完整元信息或累计全文，不能将每帧理解成独立数据库记录。字段层级已整理至 M3 API 指南，实施时与 OpenAPI 和事件 fixture 同步。

## 5. M4–M9 的前端接力边界

M4 已冻结并实现后端语义、运行时 schema 和事件示例；M5 已完成 evidence typed content、引用、读取、policy、provider adapter 与 production/test composition 的本机实现和集成审计。M6 已完成单水体 reasoning、typed content、result summary/ref、session transcript/SSE、history 和 production composition 的本机实现与集成审计。新前端仍由 M9 统一实现并开始正式联调；M6 不增加 direct prompt/model/tool route 或第三条流。

| Milestone | 前端必须遵守的语义 |
|---|---|
| M4 query/context | 最终 create request 由 query、可选地图 target（COMID/坐标）、可选 date range、`auto/6h/12h/24h`、parser/waterbody 两个模型选择和 memory mode 组成；前端不提供参数表单，也不提交 mode/ParameterKey/key/topology limits。target/date 表单值优先，缺失才使用 query，实质冲突停止。首期日期只允许当地今天前后各 7 天，date-only inclusive end 由后端按固化 IANA timezone 转 UTC；浏览器不自行转换或截短。参数由 LLM 自主推测后经 capability registry 验证，并以 ParameterKey+rationale 展示；第一版正式联调能力范围见 [M4 的 154 项目录](M4/m4_parameter_capability_catalog.md)，单次最多 6 个用户目标，系统必需 discharge 不占上限。parser 过程走 session 流；context 是 session typed item，graph 是 run typed item，run snapshot 只给引用；没有第三条流或独立 topology_ready event。完整协议见 [M4 API](M4/m4_query_understanding_topology_api.md)。 |
| M5 evidence | production 不提供 direct tool invocation，test-only 接口不接入前端；前端只读取现有 session transcript/SSE/reference。区分 tool execution 与 `available / empty / unavailable / invalid` collection，不能把无数据、失败和技术性分页不完整合并。direct/allowed-indirect/contextual 分别展示，M4 availability metadata 不是 evidence；suspicious 可用但带警告，invalid 只审计。大 source records 只提供 preview/reference，按需读取完整内容，不要求无界 payload 进入 SSE。单位、换算、聚合、coverage 和 provenance 由后端确定性逻辑提供，前端不自行猜测。任何多记录 aggregate/上游/天气/设施/历史结论不展示成原始 observed；未知或 indeterminate discharge 不显示为 0。完整契约见 [M5 API](M5/m5_evidence_tools_api.md)。 |
| M6 reasoning/result | 每个计划窗口都有 quantified、directional 或 indeterminate；未通过最终 gate 的 cell 由确定性逻辑生成 `insufficient_evidence` 且不进入 final reasoning。前端不能丢弃 indeterminate、根据 confidence 改写结果，或把 source=null 显示成 observed。风险等级 `none/low/medium/high/indeterminate` 由 Agent 给出并附 `risk_rationale`；confidence 也由 Agent 给出并附理由，fixed insufficient confidence=0。每个窗口同时展示跨参数综合风险，日期范围另展示总体风险；`complete/partial/indeterminate` availability 必须独立显示，`none/low + partial` 不得称为整体安全。值、单位、refs、attribution 和 `basis: observed/correlated/inferred/hypothesized` 保留后端 identity。水体 completed payload 在现有 typed lifecycle item 上增加 result ref/summary，不新增 completion SSE event。Agent 的实时输出和工具调用就是 session 会话上下文：文字沿用 `message_started/message_delta/message_finalized`，其他过程沿用 `transcript_item`；不接入第三条 SSE、`reasoning_delta` 或后端合成推理摘要。完整设计见 [M6 API/读取契约](M6/m6_single_agent_reasoning_api.md)。 |
| M7 workflow/summary | 水体等待全部直接上游收敛后只执行一次；依赖状态区分 `usable / insufficient / invalid / cancelled`。invalid 表示技术失败且没有证据正文，insufficient 不能显示为 0；只要另一直接上游 usable，下游仍可继续。stationless 全部上游不足/invalid 时得到正常的确定性 indeterminate，0 次模型调用。目标水体结果是主结论；非目标失败时可 `completed + partial`，目标技术失败才使 run failed。全部 active 水体收敛后，`run_summarizer` 用目标完整投影与其他水体报告卡生成一次浅显总结；突出特殊值，数字带单位/窗口/ref。汇总失败只显示安全失败和原水体结果，不生成 fallback。Agent/tool 事件仍以 session 为根关联，汇总沿用同一 session SSE；cancel 后不得展示晚到 result 或启动新的前端下游动作，`workflow_quiesced` 保持内部审计事实。完整契约见 [M7 API](M7/m7_topology_driven_multi_agent_api.md)。 |
| M8 RAG | 历史推理必须显示原 session、原 confidence、schema version、RAG score 和 provenance；当前有效观测优先，前端不得把历史推理伪装成当前 observation。 |
| M9 settings/admin | 修改默认 timezone 只影响新 session。API key 只能通过 secret endpoint 保存/清除并以 masked status 展示，不能回显或进入普通日志。全局单位变更必须经过用户显式确认。Admin 清理 history/transcript/index/memory/cache 时不得暗示 API key 也被删除。最终 demo 仍是单进程、单 worker。 |

## 6. 前端验收清单

- [ ] 按 D79，在一条消息尚未生成完时即可看到安全文本逐步增长，按同一稳定身份更新；完整消息及断线补发不会重复追加正文。历史显示已保存的组装正文，失败/取消不假装消息完整，晚到内容遵守原可见性；大正文仍可读全，流式效果不取决于折叠或存储阈值。

- [ ] 按 D78，session 过程 SSE id 从 1 递增，重发同号不重复展示；Last-Event-ID 使用最后已消费编号并按 session 隔离。run 通知、stream_end 和心跳不改变过程游标；非法格式 400、超过末尾 409 可明确处理，不混用 transcript_sequence 或历史分页游标。

- [ ] 按 D77，合法 live SSE 请求统一以 200 建流；已结束 session 有缺失记录时先补齐，无缺失时直接 stream_end，均不返回 204。run 先收到最新快照，再按真实关闭条件结束或继续；清理后的 410 仍转历史，不保留 M2 的 204 兼容分支。

- [ ] 按 D76，两流收到 stream_end 后停止重连；未收到而意外断开时不误报 Agent 完成或分析成功。session 流结束不等于整次 run 已保存，run 流仍遵守 D70 保存门槛；控制通知不显示为新的业务终态或过程正文。用户主动关闭查看不触发自动重连。

- [ ] 按 D75，首次打开运行中 Agent 时，无游标 session SSE 能取得此前全部可见过程并接上新增记录，不依赖先 GET transcript，也不遗漏发送交接期间产生的消息。重连用原游标补齐并去重；已结束 Agent/历史仍用完整 GET，大正文与公共引用可继续读取。

- [ ] 按 D74，SSE 已通知完成后，迟到且仍为推理中的 GET 不使当前状态倒退；未收到 SSE 时 GET 可用于初始加载，断线核对与历史读取保留。不要求 state_revision 或时间戳比较，也不把 SSE 内容混入严格历史。

- [ ] 按 D73，断线期间 Agent 从推理中变为完成时，run 重连直接取得最新完整轻量快照；session 按游标补齐过程，完整 GET 仍能读取此前全部可见记录和全文。不要求重播 run 中间状态通知，不将已清理的历史当作实时流恢复。

- [ ] 按 D71 消费完整轻量 run 快照，BFS 范围内被排除和未开始的水体也有对应表示；半透明灰/灰/蓝/绿/红分别映射不参与/未开始/推理中/完成/出错，不以 session 是否存在代替参与判定。未创建 session 不出现空过程流，取消后展示按 D72 留到 M9 设计与验收，不阻塞 M3 后端契约。
- [ ] M4 表单只收地图 target、日期、粒度、parser model、waterbody model 和 memory mode；没有参数选择器。地图正常提交 COMID+坐标，日期 UI 限制当地今天前后各 7 天，粒度默认 auto；前端限制只改善体验，后端错误仍完整处理。
- [ ] 201 后显示异步解析进度；从 `query_understood` item 分别展示后端最终用户目标 `params_used`、explicit/inferred、简短 rationale 和系统自动加入的 `supporting_params`，不得把必需 discharge 显示成用户选择，也不从 parser 流式草稿提前确定参数。用户目标最多 6 项；解析出第 7 项时显示后端 `too_many_parameters` 提示并要求用户缩小问题，不能在前端截断。理解明确自动继续；出现冲突/歧义/unsupported/超限则提示用户修改后新建 run，不在原 run 内对话修改。
- [ ] parser/waterbody model 分别选择并只发送 provider/model identity；API key 不进入普通 create body、日志或历史。双模型、粒度 preference 和 memory mode 直接加入唯一 run configuration，不增加 configuration 版本或旧/新 union。`memory_augmented` 作为最终系统选项由 M8 实现，前端不为内部 M4 单独做 feature gate 或“暂不可用”分支。
- [ ] M1–M9 期间前端只按同一套最终产品契约协同设计，不接入或兼容“M4 版”等阶段产品；M9 完成后才开始正式前后端联调和第一版整体验收。M9 前后端字段、OpenAPI fixture 和 SSE 消费代码随当前唯一契约一起更新，不保留 milestone-specific 分支。
- [ ] run snapshot 的 `context_ref/topology_ref` 指向原 item；收到 topology reference 后按需读取完整 graph。`query_understood/topology_ready` 是 content type，不当作新的 SSE event；run 重连仍只取最新完整轻量快照。
- [ ] 按 D70 分别处理两流正常结束：Agent 结束并发完过程后关闭 session 流；run 业务终态不延迟展示，收敛且全部必需内容保存后关闭 run 流，不等待 60 秒。保存中或耗尽时保留 run 通知，手动补写变化可见，不新增业务终态；用户关闭连接不取消后台执行或保存。
- [ ] 按 D69，打开当前 run 页面建立 run 通知流，查看水体过程才增加对应 session 流；切换水体保留 run 连接，离开页面关闭两流且不取消分析。整体状态和保存变化主动更新，GET 用于加载/断线核对/历史，不持续轮询，也不将完整 Agent 消息塞入 run 通知。关闭时机按 D70、结束通知按 D76，具体序号、payload 和握手按最终两层协议验收。
- [ ] 按 D66，在 run 执行中选中已有 session 的水体即可查看其真实上报过程，不等待最终预测、不提供给 Agent 追加消息的输入；保留 run 级停止操作，取消覆盖未查看/未订阅的执行与后续调度。关闭查看连接不取消分析。SSE 连接时机按 D68，具体路径及事件按后续冻结协议验收。
- [ ] 按 D67，停止操作只请求 run cancel，不逐个请求 session cancel；已完成或失败的 Agent 保留原终态，不将取消受理显示为所有在途外部调用都已停止。
- [ ] 按 D68，打开实时过程才订阅对应 session，切换或关闭时释放旧订阅，未查看的 Agent 照常执行并保存。打开前的完整可见过程不会遗漏；已结束 Agent 及历史通过统一读取展示，当前内容不冒充已保存历史。
- [ ] HTTP client 来自最终 OpenAPI 或与其逐字段校对；没有旧前端兼容 DTO。
- [ ] 创建成功后保存 `run_id`，当前 run 页面建立 run 通知流；Agent 过程按需订阅，不把 pending 创建回执当作后续状态。
- [ ] 所有非终态显示为“分析中”，不等待 `session_started`。
- [ ] keepalive 被忽略；按 D69 最终协议区分 run/session 流的事件身份、去重与续传，不能将一条流的消费位置当作另一条流或 transcript 的游标。
- [ ] SSE 断线、页面刷新和组件卸载不触发 cancel；停止按钮调用 cancel API。
- [ ] 各执行的业务终态只确定一次，流按 D70 的正常关闭条件结束；run 业务终态不直接关闭仍需通知保存变化的流，传输 EOF 也不被误判成业务终态。
- [ ] HTTP error 按 status/code 处理，并保存 request/session/error ID；客户端日志可保留业务 query，凭据仍须脱敏。
- [ ] M3 上线前 create 不自动重试；M3 上线后一次主动提交的所有重试原样复用同一 idempotency key 与请求体。
- [ ] 用户连续两次主动提交完全相同的 query/请求体，前端生成两个不同 key，后端创建两个不同 run；只有对其中某次提交的重试才复用它自己的 key 和请求体。不得按 query 复用 key。
- [ ] 主动提交生成合法 UUID v4；非法 key 的 `422` 按校验错误处理，不将其自动当作无 key 请求再次提交。
- [ ] 同 key 等待超时按 `503 / idempotency_wait_timeout / retryable=true` 处理，重试不换 key、不重复启动分析，不用创建回执的 pending 覆盖后续状态，也不把等待超时当作原分析失败。
- [ ] 创建后启动失败显示 `workflow_start_failed` 和 error ID；原 key 重试只取得原创建回执，不触发重新执行。用户主动再分析使用新 key。
- [ ] SSE 握手 `410` 后停止重连并查询历史；`404` 展示不存在；数据库读取故障不伪装成不存在。重启或内存清理不会触发前端重新提交分析。
- [ ] M9 使用统一 fetch SSE 客户端，覆盖 HTTP 状态分支、跨网络分块解析、Last-Event-ID、去重、terminal 停止和主动关闭订阅不取消分析；具体解析库不改变这些契约。
- [ ] query 历史列表按 D47 默认 50、最多 200 条，以 created_at/run_id 倒序及 items/next_cursor 翻页，不依赖总条数；末页、空列表、非法 limit 和数据库错误正确区分。选定历史后，前端按该次已保存消息及最终前端设计展示地图标点和连线，不要求后端另存地图快照；按 D45 一次取得水体完整有序记录列表，采用连续多层展示，不传 Agent 记录分页参数、不出现记录翻页交互。小正文随列表返回、大正文按需全文读取，公共上下文完整关联，SSE sequence 与 transcript sequence 不混用。
- [ ] D48 游标由后端生成并原样传回，不从 UI 时间重建；重启后可继续读取，无服务端分页状态依赖。非法/不支持游标的 422 显式处理，不将错误响应当成首页或空列表，不与 SSE cursor 混用。
- [ ] D49 首版历史列表只传 limit/cursor，按创建顺序浏览；query/status 等正常展示，不依赖未实现的关键词、状态或日期筛选，水体过程的读取保持独立。
- [ ] 持久化失败且内存仍在时，当前结果与大内容可读取并保留警告；内存按条件清理后，同一内容引用可读取已保存全文。前端不切换存储来源，也不把内存内容混入已保存历史。
- [ ] completed 之后仅完成事件或收尾记录保存失败时，按当前读取返回的信息展示持久化警告并保留业务结果；不自行改写终态、不期待第二个 SSE 终态，也不把 completed 当作全部保存成功。完成等待期间取消的竞争仍按后端响应处理。
- [ ] 自动补写耗尽后可按后端能力手动“重试保存”；一个主动操作开启新一轮，执行/退避中重复点击不重置预算，不因页面刷新自动开启新一轮。按钮不调用创建分析接口，触发成功、实际保存成功和内存已丢失分别展示。
- [ ] persistence 的 up_to_date/pending/retry_exhausted 与当前 warning 按后端响应展示；正常等待不显示伪造错误，只有 retry_exhausted 提供手动按钮。persistence=null 不代表整个分析保存完整，不隐藏历史记录中的原警告，不据此启动补写或另建前端重试状态。 覆盖 D80 的生成中未保存正文：显示 pending 且无实际错误则 warning=null，不误称数据库正在写入，已有耗尽及错误不被新片段覆盖。
- [ ] 按 D81 使用 item_id 关联流式片段与最终正文，SSE id 用于片段重发去重；连续片段不因消息身份相同而被丢弃，完成、补写和历史读取不换消息身份或展示重复正文。
- [ ] 按 D82 保持消息开始时的过程序号与时间，完成、补写后仍在原位；历史按实际已保存内容及原序号展示，不从最大已保存序号推断保存完整。
- [ ] 按 D83 展示消息正文的 generating/completed/incomplete；历史不出现内存中的 generating，未完整内容不被伪装成完整生成。消息生成状态、Agent/run 执行状态及 persistence 分别处理。
- [ ] 按 D86 处理开始、增量、定稿，保持同一消息顺序与身份；多消息交错不串正文，续传不重复追加，增量仅包含本次新增文本。
- [ ] 按 D84 保留取消时固定的 incomplete 消息，后续片段不再追加；当前全文或引用读取也不带回晚到审计。严格历史能读取已保存的原消息及关联审计，各自正文不重复。
- [ ] 按 D85 处理 message_finalized：正常或未完整生成更新原消息、结束其打字状态，重复投递按原 SSE id 去重；数据库未保存也可收到，不据此关闭 Agent 流或显示已保存。大正文预览不代替全文，取消定稿不混入晚到审计，定稿先于 session stream_end。
- [ ] 手动补写使用 D41 的无请求体 POST；202 与实际保存完成区分，200/409 按当前情况展示，404/503 不混淆。响应丢失后先查询保存状态、不自动重发 POST，不取消已经受理的补写或自动开启另一轮。
- [ ] 地图展示所有实际参与推理的水体，点击能查看对应水体的分时段/总体结果和真实 Agent session 上下文；文字实时增量与 tool/evidence/decision/result 共同构成过程，不展示后端另编的“推理过程”或隐藏思维链。总体风险按 M6 定型语义展示。
- [ ] M7 依赖状态不会被 UI 混成同一种“没数据”：usable 可进入下游证据，insufficient 显示证据不足但不显示成 0，invalid 显示安全技术失败且不展示为证据；某分支 invalid、另一分支 usable 时，下游仍只出现一次真实 session/result。
- [ ] stationless 全部直接上游 insufficient/invalid 时，前端显示 completed 的确定性 indeterminate，而不是永远 loading 或失败；0 模型调用不影响结果可读。目标技术失败显示 run failed；非目标失败且目标有效时显示 completed + partial/覆盖不完整。
- [ ] run 页面展示 `run_summarizer` 的浅显全流程总结、特殊值、证据限制和稳定 refs；数字必须连同单位和时间窗展示。汇总 session failed 时只显示后端安全失败信息与仍可用的水体结果，不由前端拼固定模板或重复触发模型。首版 refs 可不跳转；如 M9 实现内部链接，只能由后端稳定引用装配 URL，不能采信 Agent 生成的 URL。
- [ ] `run_summarizer` 使用既有 session 身份、状态、transcript 读取和 SSE；target_comid 为 null，不显示成地图水体，也不建立第三条事件流。用户进入其上下文时按 session_id 读取，不按角色名称合并历史。
- [ ] 历史分页以 run 为单位，完整过程只在用户选中水体后展示其独立 session；未选水体时不展示混合的全分析过程。D50 的 run/session 身份不能与旧 M2 整体 session_id 混用，查询路径按当前 M3 OpenAPI 接入。
- [ ] 同时展示逐窗口综合风险和日期范围总体风险，保留 Agent 风险理由、confidence 理由、证据引用和独立 availability；不显示任意 0–100 分，不把总体置信度或参数均值显示成总体风险，`none/low + partial` 不宣称整体安全。
- [ ] skill/tool/hook 操作块按语义默认折叠，小内容 inline 不导致自动展开，大内容按需读取；摘要/总结来自真实结果，不使用存储 preview 替代。
- [ ] 按 COMID 进入的上下文包含对应水体完整过程及适用的公共引用，包括未被最终总结挑选的记录和大正文关联的上下文；工具开始/结束/hook 按调用 ID 正确配对，同名工具多次调用不会混合，公共内容沿用原 item 读取。
- [ ] 当前与历史内容复用 DTO/解析/展示；相同事实的身份、正文和顺序一致，持久化进度差异、警告和取消可见性仍明确。配置从原创建事实读取，置信度按其层级展示，不因减少存储副本而改变用户可见语义。
- [ ] D42/D43 的详情、timeline 和全文各使用一个统一路径，省略 view 或 view=auto 时自动选择来源，内存释放不更换 URL 或原 item 引用；不调用独立 /current 路由，不把默认内存内容标成已保存历史。历史查阅及 SSE 410 历史入口使用 view=history，timeline、全文及公共引用保持同一模式，数据库错误不从内存补齐，非法 view 按 422 处理。
- [ ] UTC/IANA timezone、DST、resolution source、三态结果、persistence warning 和 unknown schema/version 均有前端测试。
- [ ] 最终前端、OpenAPI、SSE raw frame、错误码、端到端 smoke 和本文件的字段/事件名称一致。

## 7. 同步规则

文档中的参考说明使用通用技术描述，不包含内部项目名称、仓库路径或专有源码标识。

后续任何 MR 如果改变前端可见的 request、response、status、event、error、重连、历史分页或 secret 语义，必须在同一个 MR 中同步运行时 schema/OpenAPI、对应 API 文档、raw frame/smoke、测试和本文。设计讨论中新增的前端约定先标记为“尚未实现”；只有真实 route、存储和验收证据完成后才能改为“已实现”。
