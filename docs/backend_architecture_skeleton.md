# WaterAgents 后端架构骨架设计

生成日期：2026-09-06

本文档是当前后端架构骨架的最终版，也是给后续设计/实现 agent 阅读的架构引入文档。它只回答最高层问题：WaterAgents 后端要解决什么问题、第一版需要哪些大功能、系统分成哪些层、层与层之间如何协作。

本文档不写 milestone，不分配具体 agent 任务，不选择具体数据库 schema，不展开每个功能的实现细节。后续 milestone 文档会在本文基础上说明需要哪些 agent 完成哪些功能；具体功能设计再由功能实现 agent 逐项细化。

M3 及之后必须承接的已确认功能，统一登记在 [deferred_milestone_contracts.md](deferred_milestone_contracts.md)。

第一版新后端只实现 session + SSE 主协议。新后端的需求和契约只以本文档与后续新架构设计为准。`backend_bk/`、`frontend_bk/` 和 `docs/backend_refactor_contract.md` 完全不属于新后端上下文，不能成为需求来源、行为参考、数据源清单、兼容目标或测试 oracle。

## 0. 仓库目录约定

后端重构采用新旧目录分离：

- `backend/`：新后端实现目录。后续所有新架构代码都写在这里。
- `backend_bk/`：与新后端完全独立的旧后端归档，新后端完成后整体丢弃。
- `frontend_bk/`：待重构前端的历史归档，不是新后端或未来新前端的设计输入。

设计、实现、测试、评审和验收 `backend/` 时，Agent 不得读取 `backend_bk/` 或 `frontend_bk/` 来推导功能，也不得导入、复制、适配、枚举或测试归档中的代码与契约。未来前端重构应基于当时确认的产品设计和新后端契约独立完成。只有用户明确要求维护或审计旧系统时才可以单独处理这些归档，且结论不得自动进入新设计。

## 1. 项目背景

WaterAgents 要解决的问题是：在美国，并不是每条河流或水体都有对应检测站。即使附近有检测站，也会遇到时间覆盖不足、参数缺失、冬季冰冻、设备异常或局部数据不稳定等情况。因此用户很难直接获得任意水体在任意时间窗口内的水质状态。

系统目标不是简单查询数据库，而是基于有限证据生成一个可解释判断：

```text
目标水体 + 时间窗口 + 目标参数
  -> 读数或趋势/风险判断
  -> 证据链
  -> 置信度
  -> 归因总结
  -> 可展开的具体分析过程
```

第一版核心业务能力是“推测”，其中包含两种模式：

- 补全：历史时间窗口内，当目标水体或目标参数缺失时，基于检测站、上游结果、流量、天气、历史记忆等证据补全。
- 预测：未来时间窗口内，基于最近观测、上游结果、流量、天气预报、历史记忆等证据预测。

补全和预测不是两套系统，而是同一条推理流程在不同时间段上的两种模式。跨越当前日期的请求可以拆成历史补全段和未来预测段。

归因不是第一版独立前端功能，而是每次推测结果必须附带的能力。结果中需要区分：

- `analysis_detail`：具体分析过程，适合用户点进详情页后查看。
- `attribution_summary`：归因总结，适合前端结果卡片直接展示。
- `evidence_chain`：支撑分析和归因的证据引用。

## 2. 第一版大功能范围

第一版 demo 需要跑通这 8 个大功能：

1. Analysis Session
   - 创建分析任务。
   - 用 SSE 向前端推送实时事件。
   - 支持 session cancel。
   - 支持历史 session 列表、详情和 transcript 查看。

2. Query Understanding
   - 从用户 query 和前端参数中解析位置、目标 COMID、目标参数、时间范围、时间粒度、推理模式。
   - 参数解析使用语义匹配方式，将用户描述映射到最接近的 USGS parameter codes。

3. Hydrology Topology
   - 基于目标 COMID 构建水流拓扑。
   - 找到可用检测站和上游 agent。
   - 确定拓扑驱动的 agent 执行顺序。

4. Evidence System
   - 获取、组织和追踪证据。
   - 基础证据包括检测站观测、流量、天气/预报、上游 agent 结果、历史 transcript/RAG。
   - EPA facility 等外部信息作为 agent 可按需调用的 evidence tool。

5. Agent Reasoning
   - 按水流拓扑运行 agent。
   - 同一流程支持补全、预测和混合时间段。
   - 每次结果必须带读数或趋势/风险判断、置信度、证据链、具体分析和归因总结。

6. Result Model
   - 支持 `6h / 12h / 24h` 时间粒度。
   - 默认时间粒度是 `12h`。
   - 每个时间窗口标记 `observed / inferred / predicted`。
   - 支持每个时间窗、每个参数和整体结果的置信度。

7. Memory / RAG
   - transcript 是历史事实源。
   - RAG 从 transcript 中可检索的 item 建索引。
   - 历史记忆可以作为 evidence，但不能覆盖当前真实检测站数据。

8. Settings / Keys / Admin
   - 管理 provider、model、API key、运行参数默认值。
   - 提供清理增长型数据的 admin 能力，例如历史 session、transcript、RAG index、cache。
   - 清理接口不应删除 API keys。

第二版再加入 Evaluation / Accuracy Benchmark，用于正确率评估、holdout 测试、误差分析和推理质量优化。第一版只预留架构位置，不要求实现 benchmark。

## 3. 架构原则

### 3.1 输出是判断，不是裸读数

系统最终交付的是带证据链和置信度的判断。判断里必须包含最终读数；但当证据不足以给出可信具体数值时，系统应返回趋势/风险判断，而不是硬编数字。

### 3.2 检测站数据是锚点

如果目标 COMID、目标参数、目标时间窗口有真实检测站读数，agent 不允许随便改该读数。agent 可以补充：

- 置信度说明
- 覆盖率和质量标记
- 异常标记
- 归因总结
- 周边证据解释

如果检测站覆盖率不足，例如只有部分时间窗口有读数，则有真实读数的窗口锚定检测站，缺失窗口再由 agent 补全。每个窗口都必须标记来源。

同一窗口存在多条冲突的有效观测时，Agent 可以基于全部证据判断采用方式，但不得静默丢弃冲突记录。原样采用某条有效观测可标记为 `observed`；任何加权、折中或综合结果必须标记为 `inferred`。

### 3.3 证据不足时保持保守

如果目标 COMID 没有检测站，上游也找不到任何可用检测站链路，即使有天气和 EPA 等环境证据，第一版也不输出具体水质数值。

如果目标参数完全没有观测支撑，例如用户请求 pH，但目标和上游都没有 pH 类观测，则只能输出趋势/风险判断。

### 3.4 Evidence 是 agent 推理的中心

所有进入推理判断的事实都要成为结构化 evidence，不能只作为 prompt 文本散落存在。Reasoning layer 可以动态调用 evidence tools 或 subagents，但最终参与判断的内容都必须落为可追踪的 `EvidenceFragment` 或 `EvidenceBundle`。

### 3.5 Mandatory 和 optional tool 只是 policy 区分

所有外部信息能力本质上都是 evidence tools。mandatory tools 和 optional tools 在技术上没有区别，区别只在 agent policy/prompt：

- mandatory tools：某类 agent 任务开始分析前必须调用。
- optional tools：agent 根据任务和证据缺口按需调用。

例如基础水文观测、流量、天气通常是 mandatory；EPA facility 可以是 optional。

### 3.6 Transcript 是事实源

每次分析的过程要保存为 transcript。Transcript 中记录请求、上下文、tools 结果、agent/subagent 会话、证据、结果和错误状态。

跨会话 memory/RAG 不另建一套神秘记忆系统，而是从 transcript 中可检索的 item 生成索引视图。

### 3.7 第一版优先跑通主流程

第一版 demo 不需要解决所有异常和边界情况。复杂失败处理、精细 replay、数据库性能优化、准确率 benchmark 可以记录设计入口，后续版本再做。

### 3.8 跨平台运行是基础约束

新后端必须支持 macOS、Windows 和 Linux，不能把当前开发机行为当作运行时契约。生产代码不硬编码 POSIX 路径、`/tmp`、shell 命令或 macOS 专用能力；文件路径使用 Python 标准库抽象，启动和测试入口通过 Python 模块调用保持一致。依赖需要在三个目标平台的受支持 Python 版本上可安装，其中 IANA timezone 数据不能假设由操作系统提供。

## 4. 总体分层

建议采用以领域和应用为中心的分层架构。外部 API、LLM provider、数据库、缓存、第三方数据源都放在边缘，通过 ports/adapters 接入。

```text
Frontend
  |
  v
[1] Interface / API Layer
  |
  v
[2] Application / Workflow Layer
  |
  +--> [3] Domain Layer
  |
  +--> [4] Evidence & Tool Layer
  |
  +--> [5] Reasoning & Agent Layer
  |
  +--> [6] Transcript / Memory / RAG Layer
  |
  +--> [7] Ports
             ^
             |
[8] Infrastructure Adapters & Persistence
```

依赖方向：

- Interface 依赖 Application。
- Application 编排 Domain、Evidence、Reasoning、Transcript 和 Ports。
- Domain 不依赖 Web 框架、数据库、LLM 框架或第三方 API。
- Evidence 和 Reasoning 依赖 Domain 与 Ports，不直接依赖具体外部 API。
- Infrastructure 只实现 Ports，不反向调用 Application。

## 5. Interface / API Layer

Interface 层负责对外协议，不负责水文业务推理。

### 5.1 新主协议：session + SSE

第一版主协议采用 session + SSE。

建议主入口：

```text
POST /api/analysis/sessions
GET  /api/analysis/sessions/{session_id}/events
POST /api/analysis/sessions/{session_id}/cancel
GET  /api/analysis/sessions
GET  /api/analysis/sessions/{session_id}
```

语义：

- `POST /api/analysis/sessions`：创建 session 并立即开始分析。
- `GET /api/analysis/sessions/{id}/events`：通过 SSE 订阅实时事件。
- `POST /api/analysis/sessions/{id}/cancel`：取消 session。
- `GET /api/analysis/sessions`：返回历史 session 列表。
- `GET /api/analysis/sessions/{id}`：返回历史详情，默认包含最终结果，可展开 transcript timeline。

历史列表最小字段：

```text
session_id
status
created_at
updated_at
query
target_comid
location
time_range
mode
parameters
final_confidence
```

历史详情的产品形态：

- 默认展示最终推理结果卡片。
- 用户点进去后展示总体推理流程和完整 transcript timeline。
- 数据库内容是历史会话的权威来源。

### 5.2 SSE 事件

第一版 SSE 至少需要表达：

```text
session_created
query_understood
topology_ready
agent_started
evidence_collected
agent_completed
session_completed
session_failed
session_cancelled
```

具体字段由后续接口设计 agent 决定。Interface 层要保证事件面向前端稳定，内部事件可以更细。

### 5.3 Cancel 语义

第一版 cancel 是 session 级能力。只有显式调用 cancel API 才取消 session；前端断开 SSE、页面刷新、网络抖动或代理超时都不改变 session 状态。客户端可以使用 `Last-Event-ID` 重连并续传事件。

高层语义：

- session 标记为 `cancelled`。
- 已产生的过程和结果仍写入 transcript。
- cancel 后停止向下游 agent 或后续 reasoning 传播结果。
- cancel endpoint 在终态提交后立即返回，不等待在途 workflow 完全停止；workflow 在阶段边界协作式收敛。
- 不要求第一版实现复杂暂停/恢复。

具体到 tool/LLM 调用是否强杀、如何标记 partial item，属于后续功能级设计。

### 5.4 Settings / Keys / Admin

Interface 层继续提供：

- provider/model/default settings 查询
- API key 保存、清除和 masked status
- map key 获取
- Ollama model list 查询
- 清理增长型数据的 admin API

Admin 清理对象可以包括：

- session history
- transcript items
- RAG index
- derived cross-session memory
- evidence cache

Admin 清理不应影响：

- API keys
- provider settings
- 用户显式保存的配置

## 6. Application / Workflow Layer

Application 层表达用例和流程。它不做水文数学判断，不调用具体外部 URL，不拼具体 prompt。

核心职责：

- 创建和管理 analysis session。
- 初始化 live transcript。
- 编排 query understanding、topology、evidence、reasoning、结果聚合。
- 处理 SSE 事件推送。
- 处理 session cancel。
- 处理历史 session 查询。
- 保证每次运行的状态转换可记录。

核心 workflow：

```text
CreateAnalysisSession
  -> PrepareAnalysisContext
  -> BuildHydrologyTopology
  -> RunTopologyDrivenAgents
  -> PersistTranscriptAndResult
  -> CompleteSession
```

Session 状态建议：

```text
pending
running
completed
failed
cancelled
completed_with_persistence_error
```

Application 层只负责流程推进。证据是否足够继续推理，由 Reasoning & Agent 层判断。

## 7. Domain Layer

Domain 层定义稳定业务概念。它应该是最少依赖、最容易测试、最不受框架影响的一层。

核心领域对象：

- `RiverSegment`
  - COMID、坐标、上下游关系。
  - 第一版业务身份只使用 COMID；`Waterbody` 抽象延后到真正出现非 COMID 水体需求时设计。

- `MonitoringStation`
  - 检测站，例如 USGS station。
  - 包含 station id、位置、关联 COMID、可用参数。

- `WaterQualityParameter`
  - `ParameterKey(code_system, code)`、参数名、canonical unit、参数类别。

- `TimeRange`
  - 用户请求的起止时间。

- `TimeResolution`
  - `6h / 12h / 24h`。
  - 默认 `12h`。
  - 另存 `time_resolution_source: frontend | default | auto`。
  - 分析时区使用可配置 IANA timezone，默认 `America/Chicago`；session 固化当时使用的时区。

- `TimeSegment`
  - `{start, end, mode}`。
  - mixed 请求会拆成多个 segment。

- `InferenceMode`
  - `gap_filling`
  - `prediction`
  - `mixed`

- `EvidenceFragment`
  - 单条证据。

- `EvidenceBundle`
  - 某个 agent 在某个时间上下文中可用的证据集合。

- `ParameterTimeSeries`
  - 某参数按时间窗口组织的结果序列。

- `TimeWindowResult`
  - 单个时间窗口的值或趋势/风险判断。

- `InferenceResult`
  - 每个 agent 或目标水体的推测结果。

- `AttributionSummary`
  - 归因总结，面向前端卡片。

- `AnalysisDetail`
  - 具体分析过程，面向详情页。

- `EvidenceChain`
  - 结果引用的证据集合。

- `DataQualityFlag`
  - 缺测、覆盖不足、可能冰冻、异常、估计值、历史推理等结构化标记。

### 7.1 Time-window 结果模型

Domain 层的核心结果模型是 time-window result：

```text
ParameterTimeSeries
  parameter
  resolution
  points:
    - start_time
    - end_time
    - outcome_status: quantified | directional | indeterminate
    - representative_value，可空
    - risk_or_trend，可选；quantified 也可附带
    - source: observed | inferred | predicted
    - confidence
    - aggregation_method
    - evidence_refs
```

前端如果需要 daily view，可以由 Interface mapper 从 time-window result 汇总生成。Domain 层保持时间窗口语义，不绑定单一展示粒度。

时间戳统一存 UTC；窗口按 session IANA timezone 的当地墙上时钟推进。DST 切换导致的 5/7 小时或 23/25 小时 elapsed duration 合法，重复本地时间依靠不同 UTC instant 区分。完全缺乏证据时也要为计划窗口返回带原因和 evidence refs 的 `indeterminate` 结果。

### 7.2 置信度粒度

后端至少支持：

- 整体结果置信度
- 每个参数置信度
- 每个参数每个时间窗口置信度

### 7.3 归因与证据引用

归因 factor 必须能追溯到具体 evidence：

```text
AttributionFactor
  factor
  direction
  magnitude
  explanation
  evidence_refs[]
```

前端展示可以是“主要因素 + 来源引用”的摘要；后端存储要能展开到原始证据、位置、质量标记和 provenance。

## 8. Evidence & Tool Layer

Evidence 层负责收集、整理、标准化和追踪证据。它可以表现为 pipeline，也可以表现为 agent 可调用的 tools/subagents。

核心原则：

- Evidence 层不做最终判断。
- Evidence 层不输出最终水质读数。
- Evidence 层只提供结构化证据、质量标记、候选聚合结果和 provenance。
- Reasoning 层可以动态调用 Evidence tools，但不能直接依赖 NWIS/NOAA/EPA/NLDI 的 API 细节。

### 8.1 Evidence 最小结构

每条 evidence 至少需要：

```text
evidence_id
source
target_comid
location
time_range
data_type
value_or_summary
quality_flags
confidence_hint
provenance
rag_eligible
```

`location` 至少支持：

```text
lat
lon
comid
station_id
distance_to_target
```

### 8.2 基础 evidence tools

第一版 mandatory evidence tools 候选：

- COMID/location/topology evidence
- station observation evidence
- discharge/flow evidence
- historical weather evidence
- forecast weather evidence
- upstream current-session result evidence
- transcript RAG evidence，当 `memory_mode=memory_augmented`

第一版 optional evidence tools 候选：

- EPA facility evidence
- 更细的历史 transcript 检索
- 参数相关补充证据

### 8.3 Aggregation tool

聚合方法由 agent 在固定 tool/skill 列表中选择，不允许自由发明不可复现的方法。

固定方法候选：

```text
mean
median
min
max
sum
p90
latest
```

不同参数可以有不同默认聚合策略：

- flow/discharge：默认 mean。
- precipitation：默认 sum。
- pH：默认 mean，可辅助 min/max 看异常。
- 污染物浓度：默认 mean，同时可用 max/p90 支持风险判断。

如果 agent 选择了不合适的方法，第一版先接受并记录。这说明 tool/skill instructions 需要后续优化，架构层不强行覆盖。

### 8.4 原始数据

第一版可以完整保存 raw data 到数据库。返回给模型的 tool 结果允许包含完整原始数据，也允许由 agent 指定时间范围、前 N 个或后 N 个读数。

单个 transcript item 或 working memory item 建议有 inline 截断上限，例如 `64 KiB`。超出后的具体存储策略由后续数据库设计决定。

## 9. Reasoning & Agent Layer

Reasoning 层负责让 agent 基于 evidence 生成判断。

第一版不是开放式多 agent 聊天系统，而是：

```text
水流拓扑 DAG
  + 每个节点内可调用 evidence tools
  + 结构化证据追踪
  + 上游结果向下游传递
```

基础交互是严格水流拓扑。未来可以扩展为下游 agent 基于拓扑触发向上游 agent 追问，但第一版不需要实现复杂问答式交互。

### 9.1 Agent 输入

每个 river segment agent 至少需要收到：

```text
agent_id
target_comid
requested_parameters
time_range
time_segments
time_resolution
mode
evidence_policy
upstream_results
memory_mode
```

时间上下文必须在 agent 推理前传入。Agent 生成 time-window result 时要知道每个窗口的 start/end、resolution、mode。

### 9.2 Agent 阶段

建议逻辑阶段：

```text
receive agent task
  -> call mandatory evidence tools
  -> optionally call additional evidence tools
  -> decide evidence sufficiency
  -> generate analysis_detail
  -> generate attribution_summary
  -> emit structured result
```

证据搜寻阶段结束后，agent 再进入分析和归因。这样可以避免边想边乱查，也方便 transcript 记录。

### 9.3 Evidence sufficiency

Evidence sufficiency decision 属于 Reasoning 层。它决定当前证据是否足以输出具体数值。

高层规则：

- 当前真实检测站数据优先。
- 当前真实检测站数据与历史记忆冲突时，当前真实检测站数据绝对优先。
- 无检测站链路时，不给具体数值。
- 目标参数完全无观测支撑时，不给具体数值。
- 未来天气覆盖不完整时，可以继续预测，但降低超出证据覆盖部分的置信度。

### 9.4 上游到下游传递

下游 agent 不应只接收上游裸值，而应接收：

```text
upstream_result
upstream_confidence
upstream_evidence_summary
upstream_uncertainty
```

这些内容作为 current-session upstream evidence 进入下游 EvidenceBundle。

## 10. Transcript / Memory / RAG Layer

这一层是第一版架构的一等能力，不只是 debug 日志。

### 10.1 三个概念

```text
LiveTranscriptBuffer
  当前 session 内存态 transcript，给 agent 快速读写。

TranscriptStore
  数据库中的 append-only 历史事实源。

RAGIndex
  从 transcript 中 rag_eligible item 生成的检索视图。
```

内存 transcript 是运行态，不是最终事实源。历史查询以数据库中的 transcript 为权威来源。

### 10.2 Transcript 内容

每次分析应记录：

```text
user request
runtime settings
query understanding result
location / COMID
time range / time resolution / mode
topology
tool calls
tool results
agent and subagent messages
evidence fragments
evidence bundles
RAG retrieval results
LLM prompts
LLM raw outputs
structured intermediate decisions
final result
frontend events
errors / cancellation status
```

Transcript item 至少应支持：

```text
session_id
item_id
type
tags
timestamp
comid
parameter_code
time_range
location
content
rag_eligible
provenance
confidence
```

可以通过 type/tags 控制哪些内容进入 RAG。不是所有 transcript 内容都应被检索，例如心跳、纯 UI 日志、不稳定中间草稿可以 `rag_eligible=false`。

### 10.3 Memory modes

请求参数控制是否使用历史记忆：

```text
memory_mode:
  stateless
  memory_augmented
```

`stateless` 表示不使用过去 session 的 cross-session memory，但仍允许当前 session 内上游 agent 结果传给下游。

`memory_augmented` 表示允许从历史 transcript/RAG 中检索相关历史结果，作为 historical evidence 进入当前推理。

### 10.4 Historical evidence

历史结果可以被 RAG 检索并引用，但必须标记为 historical inference：

```text
source_type: historical_inference
session_id
original_confidence
rag_score
matched_by
```

低置信度历史结果可以使用，但权重应降低，并在证据链里暴露其原始置信度。

RAG 检索出来的历史 item 必须写入当前 transcript，否则未来无法审计这次判断引用了哪些历史记忆。

### 10.5 RAG 检索策略

Agent 可以主动选择 RAG 检索时的侧重点，但数据库层不应支持任意自由查询。第一版可以提供有限的固定检索策略，例如：

```text
by COMID + parameter + time window
by nearby COMID + parameter
by parameter + season
by semantic query
by topology neighborhood + parameter
```

这些策略后续可以对应数据库索引或联合索引。

## 11. Ports

Ports 定义核心层需要的外部能力接口。它们是边界，不是业务实现。

建议 ports：

```text
GeocodingPort
ComidLookupPort
TopologyDataPort
MonitoringStationPort
WaterObservationPort
WeatherObservationPort
WeatherForecastPort
FacilitySearchPort
LLMPort
TranscriptStorePort
RAGIndexPort
KeyStorePort
CachePort
ClockPort
EventPublisherPort
```

核心层只依赖 ports。具体实现放在 Infrastructure。

## 12. Infrastructure Adapters & Persistence

Infrastructure 层实现 ports，负责外部系统接入和持久化。

候选 adapters：

- NLDI adapter：COMID lookup、坐标、上游 flowlines、USGS station。
- NWIS adapter：水质/流量观测。
- NOAA weather adapter：历史天气、预警、未来预报。
- EPA facility adapter：附近设施。
- Google geocoding adapter：地名转坐标。
- LLM provider adapter：Ollama/OpenAI/Gemini/Grok 或其他 provider。
- Transcript database repository。
- RAG index repository。
- Key store repository。
- Cache repository。
- HTTP client adapter。
- Config/lifecycle adapter。

第一版数据库格式还需要单独设计。本文只要求数据库能支撑 transcript、history detail、RAG index 和增长型数据清理。

## 13. 第一版端到端流程

建议第一版主流程：

```text
1. Frontend POST 创建 analysis session
2. Backend 创建 session，初始化 LiveTranscriptBuffer
3. Frontend 通过 SSE 订阅 session events
4. Query Understanding 解析 query、位置、参数、时间、resolution、mode、memory_mode
5. Hydrology Topology 构建 COMID 图，找到站点和 agent 顺序
6. 每个 agent 根据任务和 topology 决定 mandatory evidence tools
7. Evidence tools 获取基础证据
8. Agent 按需调用 optional evidence tools
9. Agent 判断 evidence sufficiency
10. Agent 基于 time_range/time_segments/time_resolution 生成结果
11. Agent 生成 analysis_detail 和 attribution_summary
12. Application 写 transcript，推 SSE event
13. Session 进入 completed / failed / cancelled 等终态
14. 历史 session 和 transcript 可从数据库查询
```

### 13.1 成功标准

第一版 demo 的核心成功标准：

```text
用户输入位置、参数、时间范围、时间粒度和 memory mode 后，
后端创建 session，
构建水流拓扑，
收集基础证据，
按拓扑运行 agent，
返回带证据链和置信度的 6h/12h/24h 结果，
并允许用户查看历史 transcript。
```

如果证据足够，结果包含具体数值。
如果证据不足，结果返回趋势/风险判断，并说明为什么不能给具体数值。

## 14. 建议目录骨架

这是目录层面的建议，不是强制文件清单。后续 agent 可以调整文件粒度，但不应打破职责边界。

```text
backend/app/
  main.py

  api/
    http/
      analysis_sessions.py
      history.py
      settings.py
      keys.py
      admin.py
      health.py
    sse/
      analysis_events.py
    schemas/
      requests.py
      responses.py
      events.py

  application/
    workflows/
      analysis_session_workflow.py
      cancellation_workflow.py
      history_workflow.py
    use_cases/
      create_analysis_session.py
      run_analysis.py
      get_analysis_history.py
      cancel_analysis_session.py
    mappers/
      event_mapper.py
      result_mapper.py

  domain/
    hydrology/
      river_segment.py
      monitoring_station.py
      parameter_key.py
      water_quality_parameter.py
      topology.py
    time/
      time_range.py
      time_resolution.py
      time_resolution_source.py
      time_segment.py
      window_builder.py
    inference/
      mode.py
      result.py
      confidence.py
    attribution/
      attribution_summary.py
      attribution_factor.py
    evidence/
      evidence_fragment.py
      evidence_bundle.py
      evidence_chain.py
      data_quality.py

  evidence/
    tools/
      location_tools.py
      parameter_tools.py
      topology_tools.py
      observation_tools.py
      flow_tools.py
      weather_tools.py
      facility_tools.py
      rag_tools.py
    processing/
      aggregation.py
      quality_annotation.py
      anomaly_detection.py

  reasoning/
    agents/
      river_segment_agent.py
      graph_runner.py
    policies/
      evidence_policy.py
      sufficiency_policy.py
    prompts/
      builders.py
      templates.py
    parsing/
      structured_output_parser.py
    merging/
      mixed_result_merger.py

  transcript/
    live_buffer.py
    recorder.py
    item_types.py
    rag_projection.py

  ports/
    hydrology_data.py
    weather_data.py
    facility_data.py
    geocoding.py
    llm.py
    transcript_store.py
    rag_index.py
    key_store.py
    cache.py
    clock.py

  infrastructure/
    adapters/
      nldi.py
      nwis.py
      noaa_weather.py
      epa_facility.py
      google_geocoding.py
      llm_provider.py
    persistence/
      transcript_repository.py
      rag_index_repository.py
      key_store_repository.py
      cache_repository.py
    http/
      client.py
    config/
      settings.py
    lifecycle.py
```

## 15. 明确后置的内容

以下内容不属于第一版骨架跑通的必需项：

- Evaluation / Accuracy Benchmark。
- 基于同一 snapshot 的完整 replay/reproduce。
- 复杂暂停/恢复。
- 精细数据库性能优化。
- 大对象存储拆分。
- 下游 agent 主动追问上游 agent 的多轮问答式交互。
- 前端历史页面的完整体验设计。

这些内容可以在第一版架构跑通后作为第二版或后续 milestone 进入。
