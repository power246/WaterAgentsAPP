# M1 Project Shell + Domain Skeleton 详细设计

生成日期：2026-09-07

本文把 `backend_refactor_milestones.md` 中的 M1 展开为可直接实现和验收的技术设计。它服从 `backend_architecture_skeleton.md` 的分层与依赖方向；`backend_refactor_contract.md` 只作为旧实现的数据链路参考，不构成兼容目标。

M1 的目的不是恢复旧后端功能，而是固定后续 M2-M9 共同依赖的项目入口、领域语言、时间语义、数据不变量和 port 模块入口。

实现步骤与勾选状态见 [m1_project_shell_domain_implementation_todo.md](m1_project_shell_domain_implementation_todo.md)。

## 1. 核心决策

- 使用 Python 3.11+、FastAPI、Pydantic v2 和 `pyproject.toml`。
- Domain 允许直接依赖 Pydantic v2，但不依赖 FastAPI、数据库、LLM framework 或外部 API SDK。
- 新代码只写入 `backend/`；`backend_bk/` 只作历史参考。
- 第一版业务目标只用 COMID 表达，不引入 `Waterbody`；通用水体抽象留给未来版本。
- Domain model 禁止额外字段并采用浅层不可变；动态 JSON payload 在边界深拷贝，避免共享引用被原地修改。
- 时间戳进入 Domain 时统一转为 UTC，用户日期和时间窗口按可配置 IANA timezone 解释，默认 `America/Chicago`。
- 第一版输入只支持日期范围，结束日期包含整天。
- 时间窗口按当地墙上时钟推进；DST 导致的 5/7 小时或 23/25 小时窗口合法，重复本地时间由 UTC 时间戳区分。
- `TimeResolution` 只允许 `6h / 12h / 24h`，默认 `12h`；同时记录 `time_resolution_source: frontend | default | auto`。
- `TimeWindowResult` 使用 `quantified / directional / indeterminate` 三种状态。quantified 可以附带可选风险/趋势。
- confidence 只限制在 `[0, 1]`，不与结果状态做硬绑定；具体规则和 Agent 自主判断在 M6 设计。
- 没有任何可用证据时仍为每个窗口生成 `indeterminate` 结构化空判断，方便审计和排障。
- evidence ID 只要求 session 内唯一；跨 session 的源记录通过 provenance 识别。
- `InferenceResult` 只保存 evidence 引用和摘要，完整 evidence 与分析上下文由 transcript 保存并按需加载。
- adapter 返回标准化 source record；Evidence Tool 负责构造 `EvidenceFragment` 和 evidence policy 字段。
- 不兼容旧 `/analyze`、`/ws/hydro-analysis`、旧事件或旧前端数据结构，后续前端改用新 session + SSE 协议。

## 2. 范围

### 2.1 M1 包含

- 可安装、可启动、可测试的 `backend/` Python 工程。
- FastAPI application factory 和最小 `GET /health`。
- 基础运行配置，包括默认 `analysis_timezone`。
- M1 核心 Domain 对象和必要的 supporting value objects。
- 统一的本地墙上时钟窗口规划函数。
- ports 的稳定模块入口；只冻结已有足够信息的协议，其他协议使用 generic Protocol 占位。
- Domain 构造、校验、序列化和反序列化测试。
- 时间窗口与 DST 语义测试。
- Domain 依赖边界静态测试。
- 启动与 health smoke test。

### 2.2 M1 不包含

- Analysis session、状态机、SSE 和 cancel；属于 M2。
- Transcript schema、持久化、内容版本、敏感信息过滤和 history API；属于 M3。
- Query understanding、日期请求 mapper、参数语义匹配和拓扑构建；属于 M4。
- 数据 source record DTO、Evidence Tool registry、单位系统、数据拉取、聚合和异常检测；属于 M5。
- LLM prompt、parser、Agent、evidence sufficiency 和 confidence policy；属于 M6。
- 多 Agent DAG 调度；属于 M7。
- RAG 实现；属于 M8。
- Settings/key/admin 产品接口和真实 adapter；属于 M9。
- 旧 HTTP/WebSocket/前端结构的任何兼容 adapter。

## 3. 目录和模块设计

采用一类一文件：

```text
backend/
  pyproject.toml
  README.md
  app/
    __init__.py
    main.py
    api/
      __init__.py
      http/
        __init__.py
        health.py
    domain/
      __init__.py
      common.py
      hydrology/
        __init__.py
        location.py
        river_segment.py
        monitoring_station.py
        parameter_key.py
        water_quality_parameter.py
      time/
        __init__.py
        time_range.py
        time_resolution.py
        time_resolution_source.py
        time_segment.py
        window_builder.py
      evidence/
        __init__.py
        collection_status.py
        data_quality.py
        evidence_location.py
        evidence_fragment.py
        evidence_bundle.py
        evidence_reference.py
        evidence_chain.py
        provenance.py
      inference/
        __init__.py
        mode.py
        outcome_status.py
        assessment.py
        time_window_result.py
        parameter_time_series.py
        inference_result.py
      attribution/
        __init__.py
        attribution_basis.py
        attribution_factor.py
        attribution_summary.py
        analysis_step.py
        analysis_detail.py
    ports/
      __init__.py
      errors.py
      geocoding.py
      hydrology_data.py
      weather_data.py
      facility_data.py
      llm.py
      transcript_store.py
      rag_index.py
      key_store.py
      cache.py
      clock.py
      event_publisher.py
    infrastructure/
      __init__.py
      config/
        __init__.py
        settings.py
  tests/
    api/
      test_health.py
    architecture/
      test_domain_dependencies.py
      test_port_imports.py
    domain/
      test_hydrology.py
      test_time.py
      test_time_windows.py
      test_evidence.py
      test_results.py
      test_serialization.py
```

依赖规则：

- `app.main` 是 composition root，允许依赖 API 和 infrastructure config。
- `domain` 只能依赖标准库、Pydantic 和 `app.domain`。
- `ports` 可以依赖 `domain` 和 Python typing，不能依赖具体 adapter。
- `infrastructure` 后续实现 ports；M1 除 settings 外不创建空 adapter。
- application/reasoning/transcript 包由对应 milestone 在出现真实职责时创建。

## 4. 通用建模规则

### 4.1 `DomainModel`

所有领域模型继承内部基类 `DomainModel`：

```text
extra = forbid
frozen = true
str_strip_whitespace = true
allow_inf_nan = false
```

约定：

- 必填字符串 trim 后不能为空。
- COMID、station ID 和 parameter code 使用字符串，保留前导零。
- 不用裸 `dict[str, Any]` 表示核心业务结构。
- 外部源尚未定型的 payload 使用递归 `JsonValue`，不能保存 Python 任意对象。
- Pydantic frozen 是浅层不可变。动态 JSON payload 在构造时深拷贝，对外序列化返回新对象；M1 不引入 FrozenDict。
- 枚举继承 `str, Enum`，JSON 使用稳定小写值。
- round-trip 标准为 `model_dump_json()` 后可由同类型 `model_validate_json()` 恢复为相等对象。

### 4.2 标识与引用

- M1 不规定 UUID/ULID 生成方式，只要求 ID 非空。
- `evidence_id` 是 session 内采集实例 ID，不要求跨 session 稳定。
- 跨 session 原始记录身份由 `provenance.provider + source_record_ids` 表达。
- 同一个 bundle、chain 或 result 内的 ID/ParameterKey 不得重复。
- evidence 引用保持顺序，不复制完整 EvidenceFragment。

### 4.3 数值

- 纬度范围 `[-90, 90]`，经度范围 `[-180, 180]`。
- 距离、覆盖率等不能为负。
- confidence 范围为 `[0, 1]`，M1 不添加状态相关硬约束。
- NaN/Infinity 不代表缺失；缺失使用 `None` 和明确状态/质量标记。

## 5. 时间契约

### 5.1 存储和配置

- 所有输入 `datetime` 必须带 timezone，naive datetime 直接拒绝。
- Domain 内部和 JSON 中的事实时间统一为 UTC ISO 8601。
- `analysis_timezone` 使用 IANA timezone 名称，默认 `America/Chicago`；无效 timezone 直接拒绝，不静默回退。
- session 创建时固化实际使用的 `analysis_timezone`。修改全局设置不能改变历史 session。
- 外部 evidence 时间戳转为 UTC，同时在 provenance 中保留源 timezone/offset 信息。
- 时间区间采用 `[start, end)`。

### 5.2 第一版日期范围

第一版 API 只接受日期。用户选择 `2026-01-01` 至 `2026-01-07` 时解释为：

```text
configured timezone: 2026-01-01 00:00 inclusive
configured timezone: 2026-01-08 00:00 exclusive
```

API/M4 mapper 再把两个边界转换为 UTC `TimeRange`。结束日期包含整天。

### 5.3 窗口语义

- `6h / 12h / 24h` 表示当地墙上时钟跨度，不表示严格 elapsed seconds。
- 日期请求从当地 `00:00` 开始推进。
- 典型 6h 窗口为 `00-06 / 06-12 / 12-18 / 18-24`。
- DST 切换时，`00-06` 实际可能经过 5 或 7 小时；`00-24` 实际可能经过 23 或 25 小时，仍是合法完整窗口。
- 容忍时长变化的依据必须是 IANA timezone 中真实 offset transition，不能把任意少一小时的数据错误当作合法窗口。
- UTC start/end 是窗口唯一身份。即使本地墙上时钟出现两次相同时间，也不能用本地 label 做 map key，更不能让后一条覆盖前一条。

### 5.4 `build_time_windows`

M1 实现纯函数：

```text
build_time_windows(
  time_range,
  time_resolution,
  analysis_timezone,
) -> tuple[TimeRange, ...]
```

规则：

- 从请求起点对应的本地墙上时间开始滚动 resolution。
- 每次使用当地墙上时钟加 `6/12/24` 小时，再转换为 UTC 边界。
- 不超过请求 end；最后不足一个完整墙上时钟窗口时返回截断窗口，由后续阶段标记 `partial_coverage`。
- 第一版日期输入从 00:00 开始，因此自然得到固定本地网格。
- 未来自定义时间例如 `03:20-17:40`，按 `03:20-09:20 / 09:20-15:20 / 15:20-17:40` 滚动；DST 仍按墙上时钟语义处理。

未来自定义时间 API 不在 M1/M4 第一版范围内，但窗口函数保留该能力。

## 6. Domain 对象

### 6.1 Hydrology

#### `GeoPoint`

```text
latitude: float
longitude: float
```

#### `RiverSegment`

```text
comid: str
location: GeoPoint | None
upstream_comids: tuple[str, ...] = ()
downstream_comids: tuple[str, ...] = ()
```

校验：

- 上下游 COMID 各自唯一。
- 当前 COMID 不能出现在自己的上下游集合。
- location 缺失使用 `None`，不能使用 `0,0` 哨兵。
- active/excluded、target/inlet 属于 M4 `HydrologyGraph`。

第一版不定义 `Waterbody`。

#### `ParameterKey`

```text
code_system: str
code: str
```

例如 `code_system="usgs", code="00400"`。所有参数引用和去重统一使用 ParameterKey，不能只使用裸 code。

#### `WaterQualityParameter`

```text
key: ParameterKey
name: str
canonical_unit: str | None
is_dimensionless: bool = false
category: str | None
```

除明确的无量纲参数外，canonical_unit 必填。单位表、别名和转换规则属于 M5。

#### `MonitoringStation`

```text
station_id: str
source: str
name: str | None
location: GeoPoint | None
associated_comid: str | None
available_parameters: tuple[ParameterKey, ...] = ()
```

station location 可缺失，不能用 `0,0` 代替；ParameterKey 必须唯一。

### 6.2 Time

#### `TimeResolution`

```text
SIX_HOURS = "6h"
TWELVE_HOURS = "12h"
TWENTY_FOUR_HOURS = "24h"
```

含 resolution 的模型默认 `12h`。

#### `TimeResolutionSource`

```text
FRONTEND = "frontend"
DEFAULT = "default"
AUTO = "auto"
```

字段采用：

```text
time_resolution: 6h | 12h | 24h
time_resolution_source: frontend | default | auto
```

#### `TimeRange`

```text
start: UTC datetime
end: UTC datetime
```

必须满足 `start < end`，并提供无副作用的 `contains` 和 `overlaps`。

#### `InferenceMode`

```text
GAP_FILLING = "gap_filling"
PREDICTION = "prediction"
MIXED = "mixed"
```

#### `TimeSegment`

```text
start: UTC datetime
end: UTC datetime
mode: gap_filling | prediction
```

segment 不允许 `mixed`；mixed 是整个请求的模式。

### 6.3 Evidence

#### `CollectionStatus`

```text
AVAILABLE = "available"
EMPTY = "empty"
UNAVAILABLE = "unavailable"
INVALID = "invalid"
```

- empty 表示查询成功但无记录。
- unavailable 表示本次未能取得数据，不能推导“现实中不存在数据”。
- invalid 表示取得记录但不能作为有效观测使用。

#### `DataQualityFlag`

```text
missing
partial_coverage
possible_freezing
anomalous
estimated
historical_inference
stale
provider_flagged
unit_unresolved
conflicting_observations
```

未知 provider 原始 flag 保存在 provenance metadata，M5 决定是否提升为领域 flag。

#### `EvidenceLocation`

```text
point: GeoPoint | None
comid: str | None
station_id: str | None
distance_to_target_km: float | None
```

至少需要 point/comid/station_id 中的一项；point 可以缺失，distance 不能为负。

#### `Provenance`

```text
provider: str
retrieved_at: UTC datetime
source_uri: str | None
source_record_ids: tuple[str, ...] = ()
source_timezone: str | None
source_utc_offset: str | None
metadata: Mapping[str, JsonValue] = empty mapping via default_factory
```

source URI 使用普通非空字符串，因为来源也可能是数据库记录或 session item。

#### `EvidenceFragment`

```text
evidence_id: str
source: str
collection_status: CollectionStatus
target_comid: str | None
location: EvidenceLocation
time_range: TimeRange
parameter_refs: tuple[ParameterKey, ...] = ()
data_type: str
value_or_summary: JsonValue
quality_flags: tuple[DataQualityFlag, ...] = ()
confidence_hint: float | None
provenance: Provenance
rag_eligible: bool = false
```

说明：

- M1 冻结 envelope，M5 再为 observation/weather/flow/upstream result 等定义强类型 payload。
- source 是逻辑来源；provenance.provider 是实际系统。
- collection status 为 empty/unavailable/invalid 时 payload 可以是结构化原因或 null。
- confidence_hint 不是最终 confidence。
- rag_eligible 默认 false。
- Evidence Tool 构造该对象；adapter 不负责 evidence policy。

#### `EvidenceBundle`

```text
bundle_id: str
agent_comid: str
time_range: TimeRange
analysis_timezone: str
time_resolution: TimeResolution = 12h
time_resolution_source: TimeResolutionSource
requested_parameters: tuple[ParameterKey, ...]
fragments: tuple[EvidenceFragment, ...] = ()
```

校验：

- requested parameters 至少一个且唯一。
- evidence ID 唯一。
- fragment 可以来自上游 COMID。
- bundle 可以没有 available evidence；empty/unavailable fragment 用于解释结构化空判断。

#### `EvidenceReference` 与 `EvidenceChain`

```text
EvidenceReference:
  evidence_id: str
  role: str
  summary: str | None

EvidenceChain:
  references: tuple[EvidenceReference, ...]
```

chain 必须非空，evidence ID 唯一。它只保存有序引用和短摘要；完整 fragment 由 transcript/store 保存。

### 6.4 Result 与 Attribution

#### 辅助枚举

```text
OutcomeStatus: quantified | directional | indeterminate
ResultSource: observed | inferred | predicted
AggregationMethod: mean | median | min | max | sum | p90 | latest
TrendDirection: rising | falling | stable | uncertain
RiskLevel: low | medium | high | indeterminate
IndeterminateReason:
  no_station_path
  parameter_unobserved
  invalid_observations
  source_unavailable
  no_applicable_evidence
AttributionDirection: increasing | decreasing | neutral | mixed | unknown
AttributionMagnitude: low | medium | high | unknown
AttributionBasis: observed | correlated | inferred | hypothesized
```

#### `RiskOrTrendAssessment`

```text
summary: str
trend: TrendDirection | None
risk_level: RiskLevel | None
reason_codes: tuple[IndeterminateReason, ...] = ()
```

#### `TimeWindowResult`

```text
start_time: UTC datetime
end_time: UTC datetime
outcome_status: OutcomeStatus
representative_value: float | None
assessment: RiskOrTrendAssessment | None
source: ResultSource
confidence: float
aggregation_method: AggregationMethod | None
evidence_refs: tuple[str, ...]
quality_flags: tuple[DataQualityFlag, ...] = ()
```

结构校验：

- quantified：必须有 finite representative value；assessment 可选。
- directional：不得有 value；assessment 必须包含至少一个确定的 trend 或 risk level，不能只填 uncertain/indeterminate。
- indeterminate：不得有 value；assessment 和至少一个 reason code 必填。
- reason codes 只用于 indeterminate；quantified/directional 不携带 indeterminate reason。
- evidence refs 必须非空且唯一；完全缺证据时引用 empty/unavailable/invalid evidence。
- confidence 只校验 `[0,1]`，不和 outcome status 硬绑定。

#### `ParameterTimeSeries`

```text
parameter: WaterQualityParameter
time_resolution: TimeResolution = 12h
time_resolution_source: TimeResolutionSource
points: tuple[TimeWindowResult, ...]
confidence: float
```

校验：

- points 至少一个、严格按 UTC start 排序且不得重叠。
- 每个预定窗口必须有一个 point；无证据窗口使用 indeterminate，不允许直接缺点。
- point 的 UTC elapsed duration 不要求严格等于 resolution；窗口边界必须与 `build_time_windows` 生成结果一致。
- quantified 输出使用 parameter.canonical_unit；未知单位不得猜测换算。

#### `AttributionFactor`

```text
factor: str
direction: AttributionDirection
magnitude: AttributionMagnitude
basis: AttributionBasis
explanation: str
evidence_refs: tuple[str, ...]
```

每个 factor 至少引用一条 evidence。basis 防止把关联或假设展示成已证实因果。

#### `AttributionSummary`

```text
summary: str
primary_factors: tuple[AttributionFactor, ...] = ()
```

允许 factors 为空，此时 summary 说明没有足够证据支持具体归因。

#### `AnalysisStep` 与 `AnalysisDetail`

```text
AnalysisStep:
  type: str
  title: str
  content: JsonValue
  evidence_refs: tuple[str, ...] = ()
  transcript_item_refs: tuple[str, ...] = ()

AnalysisDetail:
  summary: str
  steps: tuple[AnalysisStep, ...]
  transcript_item_refs: tuple[str, ...] = ()
```

analysis detail 是可折叠 timeline 的领域内容。前端默认折叠，展开后通过 transcript refs 按需加载完整 tool call/result、prompt、raw output 和上下文；大 payload 不重复嵌入 InferenceResult。

#### `InferenceResult`

```text
result_id: str
target_comid: str
mode: InferenceMode
time_range: TimeRange
time_segments: tuple[TimeSegment, ...]
analysis_timezone: str
time_resolution: TimeResolution = 12h
time_resolution_source: TimeResolutionSource
series: tuple[ParameterTimeSeries, ...]
overall_confidence: float
attribution_summary: AttributionSummary
analysis_detail: AnalysisDetail
evidence_chain: EvidenceChain
generated_at: UTC datetime
```

交叉校验：

- ParameterKey 在 series 中唯一。
- series 和 time segments 均至少一个。
- 每条 series 的 resolution/source 与 result 一致。
- 每条 series 的 points 必须完整匹配 window plan。
- time segments 有序、无重叠、无缺口并完整覆盖 time range。
- mixed 必须同时包含 gap filling 和 prediction segment。
- 每个 window 完整落在一个 segment 内。prediction segment 只接受 predicted source；gap filling 接受 observed/inferred source。
- window、attribution 和 analysis step 的 evidence refs 都存在于 EvidenceChain。
- observed 表示原样采用有效站点读数；Agent 对多站冲突值做加权、折中或综合后必须标为 inferred。

## 7. Ports 设计

### 7.1 原则

- port 表达 core 对外部能力的需求，不镜像第三方 API。
- I/O 默认 async，纯时钟读取除外。
- adapter 返回领域对象或显式 source record DTO，不返回 SDK 对象。
- 可预期的没找到使用 None/空 tuple；外部失败使用 port exception。
- adapter 不构造 EvidenceFragment；Evidence Tool 在 M5 负责 evidence envelope、quality、rag eligibility 和 policy。
- M1 不执行网络、文件或数据库 I/O。

### 7.2 M1 可定型协议

```text
ClockPort.now() -> UTC datetime
GeocodingPort.resolve(location_text) -> GeoPoint | None
ComidLookupPort.lookup_comid(point) -> str | None
TopologyDataPort.get_segment(comid) -> RiverSegment | None
TopologyDataPort.get_direct_upstreams(comid, max_distance_km) -> tuple[RiverSegment, ...]
MonitoringStationPort.find_for_segment(comid, parameters) -> tuple[MonitoringStation, ...]
```

### 7.3 延后 DTO 的 generic Protocol

M1 固定模块、职责和操作语义，但不使用 Any 猜测未来 DTO：

```text
WaterObservationPort[RequestT, RecordT].fetch(request) -> tuple[RecordT, ...]
WeatherObservationPort[RequestT, RecordT].fetch(request) -> tuple[RecordT, ...]
WeatherForecastPort[RequestT, RecordT].fetch(request) -> tuple[RecordT, ...]
FacilitySearchPort[RequestT, RecordT].search(request) -> tuple[RecordT, ...]
LLMPort[RequestT, ResponseT].generate(request) -> ResponseT
TranscriptStorePort[ItemT].append/list_for_session
RAGIndexPort[ItemT, QueryT, ResultT].upsert/search
EventPublisherPort[EventT].publish
CachePort[ValueT].get/set/delete/clear_namespace
```

KeyStorePort 的 masked status DTO 在 M9 定型。

### 7.4 Port errors

```text
PortError
PortUnavailableError
PortTimeoutError
InvalidPortResponseError
```

empty 与 unavailable 不混淆；Application 在后续 milestone 决定 retry、降级和事件。

## 8. Project shell

### 8.1 依赖

生产依赖：FastAPI、Pydantic v2、pydantic-settings、Uvicorn。

开发依赖：pytest、httpx。

M1 不安装 LangGraph、LangChain、数据库 driver、HTTP retry/cache 库或 provider SDK。Python 最低版本为 3.11。

### 8.2 Settings

```text
app_name = "WaterAgents API"
environment = "development"
log_level = "INFO"
host = "127.0.0.1"
port = 8000
analysis_timezone = "America/Chicago"
```

环境变量前缀为 `WATERAGENTS_`。analysis timezone 启动时用 `zoneinfo.ZoneInfo` 校验。M9 再提供动态设置接口。

### 8.3 FastAPI entrypoint

```text
create_app(settings: AppSettings | None = None) -> FastAPI
app = create_app()
```

M1 health：

```json
{"status": "healthy"}
```

从仓库根启动：

```bash
uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
```

启动时不清空 memory/cache，不建数据库、不联网、不启动后台任务。

## 9. 测试设计

### 9.1 Domain

- 每个顶层 Domain 对象至少一个合法构造和 JSON round-trip。
- 空字符串、非法坐标、非法 confidence、naive datetime、反向区间被拒绝。
- location=None 合法，`0,0` 不被当成缺失。
- ParameterKey 使用 code system + code 去重。
- TimeResolution 只接受 6h/12h/24h，默认 12h；resolution source 可 round-trip。
- quantified 支持 value + 可选 assessment。
- directional 拒绝 value 并要求方向/风险判断。
- indeterminate 拒绝 value，并要求结构化 reason code 和 evidence refs。
- 不对 outcome status 和 confidence 做硬绑定。
- series 每个计划窗口都有 point，乱序、重叠、缺点或额外点被拒绝。
- EvidenceBundle 拒绝重复 evidence ID/ParameterKey。
- InferenceResult 拒绝 EvidenceChain 外的引用。
- AttributionFactor 必须有 basis 和 evidence refs。
- 未知字段被拒绝，frozen model 不能重新赋值。

### 9.2 时间窗口

使用 `America/Chicago` 固定测试：

- 普通日的 6h/12h/24h 本地窗口。
- spring-forward 日的 00-06 实际 5 小时、00-24 实际 23 小时。
- fall-back 日的 00-06 实际 7 小时、00-24 实际 25 小时。
- 重复本地时间转换后有两个不同 UTC instant，不发生 key 覆盖。
- 没有 timezone transition 时，错误的 5/7 小时完整窗口不被接受。
- 自定义时间预留行为：从 03:20 滚动，而不是对齐 06:00；尾窗可截断。

### 9.3 架构

AST 扫描 `app/domain/**/*.py`：

- 只允许 stdlib、Pydantic 和 `app.domain`。
- 拒绝 FastAPI、SQLAlchemy、httpx、LangChain、LangGraph、provider SDK，以及其他 app layer。

port import test 验证所有协议可从 `app.ports` 导入，且不导入 infrastructure。

### 9.4 Shell

- 无 `.env`、API key、网络和数据库时可构造 app。
- `GET /health` 精确返回 HTTP 200 与 `{"status":"healthy"}`。
- 导入 app.main 无文件清理、网络请求或后台线程副作用。

## 10. M1 验收标准

1. `backend/` 是 Python 3.11+ 独立可安装工程。
2. Uvicorn 可启动，health 返回 200。
3. M1 列出的 Domain 对象和 `build_time_windows` 有真实实现。
4. 所有顶层 Domain 对象可 JSON round-trip。
5. 默认 resolution 为 12h，非法 resolution 被拒绝。
6. 三种 outcome status 的结构约束有测试。
7. DST、本地墙上时钟和重复本地时间测试通过。
8. Domain dependency test 证明无 FastAPI/DB/LLM/外部 API 依赖。
9. 未向 `backend_bk/` 写入代码。
10. 未提前实现 M2-M9 运行时能力或旧接口兼容。

建议命令：

```bash
python -m pip install -e './backend[dev]'
pytest backend/tests -q
uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
curl http://127.0.0.1:8000/health
```

## 11. 实现顺序

```text
1. Project shell + health
   -> verify: app import 无副作用，health smoke test 通过

2. common/time/hydrology + window builder
   -> verify: UTC、DST、重复时间和 round-trip 测试通过

3. evidence/result/attribution models
   -> verify: 三态结果、完整窗口和引用一致性测试通过

4. ports + errors
   -> verify: stable/generic ports 可导入，无反向依赖

5. architecture/full regression
   -> verify: pytest backend/tests -q 全绿
```

## 12. Deferred Contract Register

后续 milestone 必须承接以下事项：

| Milestone | 必须实现或完成的设计 |
|---|---|
| M2 | session 创建时固化 analysis timezone、time resolution 及其 source；只提供新 session + SSE 协议。 |
| M3 | transcript item/content 使用 `content_type + content_schema_version` envelope；保存 session timezone；完整上下文支持折叠 timeline 和 lazy load；保存前强制移除 API key、Authorization、Cookie 等 secret；大 payload 只内联预览，完整内容可外置或引用。 |
| M4 | 第一版输入只接受日期；结束日期包含整天；按 session timezone 转 UTC；输出 `time_resolution` 与 `time_resolution_source`；ParameterResolver 输出 ParameterKey；自动粒度规则在此定义。 |
| M5 | 为 observation/weather/flow/facility/upstream 定义 source record DTO；adapter 返回 record，Evidence Tool 生成 EvidenceFragment；区分 empty/unavailable/invalid；可疑值原样保留，明确无效值不作为 observed result；建立 ParameterKey 到 canonical unit 的表和单位 tool；保留源单位与转换 provenance；确定性库/API 执行换算，未知换算不得猜测。 |
| M6 | 设计 quantified/directional/indeterminate 生成规则；无证据仍输出 indeterminate；制定 confidence 评分和聚合规则但保留 Agent 判断空间；多站冲突由 Agent 判断并引用全部证据，综合结果标 inferred；Agent 选择目标单位但换算调用确定性 tool；归因输出 basis；当前有效观测优先于历史推理。 |
| M8 | historical inference 保留原 session、原 confidence、schema version 和 RAG score；不得覆盖当前观测。 |
| M9 | 动态修改默认 IANA timezone；单位自定义接口先通过 tool 和 Agent 校验，默认仅生成 session override；写入全局单位表必须由用户显式确认并保存版本记录；不提供任何旧 API/WebSocket 兼容层。 |
| Future | 开放任意时分输入；窗口从请求起点按本地墙上时钟滚动，首尾可 partial；根据真实 IANA transition 接受 DST 时长变化。通用 Waterbody 抽象仅在出现非 COMID 水体需求时设计。 |

## 13. 设计取舍

- Domain 直接使用 Pydantic，避免 dataclass 与 API schema 双模型。
- 第一版只建模 COMID；不为未进入范围的 lake/reservoir 提前抽象 Waterbody。
- 一类一文件。
- 时间事实存 UTC，窗口语义由固化的 IANA timezone 决定。
- 结果不是 numeric/risk 二选一，而是三态；quantified 也可带风险/趋势。
- structured empty result 比缺少 point 更易排查，因此 series 必须覆盖所有计划窗口。
- confidence 规则属于 M6 reasoning，不在 Domain 中过度硬编码。
- evidence envelope 在 M1 稳定，具体 payload 在 M5 基于真实 source record 定型。
- adapter 不掌握 evidence policy，Evidence Tool 是 EvidenceFragment 的构造责任层。
- 旧后端结构只作为行为参考，不通过 Domain alias 或兼容 mapper 延续。
