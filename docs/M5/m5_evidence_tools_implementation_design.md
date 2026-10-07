# M5 Evidence Tools + EvidenceBundle 实施设计

更新日期：2026-10-07

> 状态：设计已冻结，当前唯一 V1 已完成 M5 Milestone 0–13 本机实现与验收。真实 USGS/NWS/EPA 请求和 Ubuntu/macOS/Windows 三平台 CI 仍为 `not run`；实施步骤、验证证据和剩余风险见 [Milestone 0–13 Implementation TODO](m5_implementation_todo.md)。

> M7 接力说明：本文 2026-10-03 新增的 dependency manifest、usable upstream启动门禁和stationless 0-model路径已由M7 production workflow承接；它们仍不表示M5 Milestone 0–13自身完成了DAG producer。

> M8 接力说明：当前 `HistoricalTranscriptRecord`/`HistoricalInferencePayload` 是 producer slot占位；M8目标类型与读取协议见 [M8 实施设计](../M8/m8_memory_rag_implementation_design.md)、[M8 API](../M8/m8_memory_rag_api.md) 和全部待办的 [M8 Implementation TODO](../M8/m8_implementation_todo.md)，不属于既有M5完成证据。

本文把 [M5 设计决策](m5_evidence_tools_design.md) 落到模块、类型、provider、执行链、持久化与测试接缝。确定性单位、质量、聚合和 applicability 规则见 [M5 Policy Catalog](m5_evidence_policy_catalog.md)，公开读取与 typed content 示例见 [M5 API 文档](m5_evidence_tools_api.md)。

## 1. 核心结论与范围

M5 建立一条统一的 evidence 执行链：

```text
Agent 或确定性 preflight
  -> EvidenceToolExecutor
  -> scope / budget / dedup 校验
  -> provider adapter 返回 SourceRecord
  -> quality + unit + applicability + aggregation policy
  -> EvidenceFragment / EvidenceCell / EvidenceBundle
  -> M3 transcript writer
```

M5 负责：

- 按当前水体、允许的时间范围和合法 station refs 调用 evidence tools；
- 补齐当前唯一 V1 的水体 LineString 空间事实；
- 标准化 observation、discharge、weather、forecast、facility 等 source records；
- 确定性执行质量分类、单位换算、聚合和窗口分配；
- 形成可供 M6 门禁消费的 reference-complete bundle；
- 为 M7 upstream result 与 M8 transcript RAG 保留同一工具接口和明确的暂不可用语义。

M5 不负责：

- 输出最终水质结论、confidence 或 attribution；
- 判断不同 observation series 是否“明显冲突”；
- 调度多水体 DAG 或创建尚未准备执行的 session；
- 建立 RAG index；
- 向 production frontend 暴露任意 tool invocation API；
- 为 M1–M9 中间阶段建立 V2、兼容 union、feature gate 或迁移分支。

## 2. 执行流程

### 2.1 水体 session 的启动输入

M6/M7 准备执行某个水体时，必须向 M5 提供 `EvidenceSessionScope`：

```text
run_id
session_id
agent_comid
analysis_context_ref
topology_ref
target_node
time_windows[]
requested_parameters[]
supporting_parameters[]       # 首期始终含 usgs:00060
memory_mode
cancel_token
```

scope 从已保存的 `query_understood v1` 与 `topology_ready v1` 装配，调用方不能提交第二份任意 COMID、坐标、station 或窗口。

### 2.2 station-backed 水体

有合格 `station_refs` 的当前水体按以下顺序执行：

1. 创建实际 waterbody session 后写入 `evidence_tool_call v1`；
2. 确定性调用 `collect_water_observations`，查询当前水体已登记的目标参数通道和 discharge 通道；
3. adapter 按 provider/range 批量取数，并逐页产生标准化 SourceRecord；
4. 每一批完整记录只写一次 `evidence_source_records v1`；
5. policy layer 形成 direct fragments、derived fragments 与逐窗口 collection facts；
6. 写入 `evidence_fragment v1` 和 `evidence_tool_result v1`；
7. 读取 M7 已收敛的直接上游 dependency manifest/result；若本地目标参数无 usable direct evidence，但 upstream 仍有与目标参数相关的 usable result，不提前停止；
8. 只要至少一个目标参数有 usable local direct cell，或有与目标参数相关的 usable upstream result，Agent 可继续按需调用天气、设施、inspection、conversion 或 aggregation 工具；只有本地 discharge 等 supporting cell 不能单独启动模型。没有目标 evidence 起点时形成完整 bundle 后交给 M6 以 0 model calls 产生全 indeterminate；
9. 完成取证后写入唯一 `evidence_bundle v1`，M6 只从该 bundle 读取门禁输入。

provider unavailable、认证失败、超时、分页不完整或协议损坏不走自然 empty 分支，而是保留已确认事实并产生技术失败。

### 2.3 stationless 水体

无合格本地站点的当前水体不调用 `collect_water_observations`。执行顺序为：

1. 由 M7 注入已收敛的直接上游 dependency manifest，并只为 usable quantified/directional points 提供结果引用；M7 尚未接入时 `get_upstream_results` 明确返回 producer unavailable；
2. 至少一个直接上游 usable 时，Agent 根据任务和缺口决定是否调用天气、设施等 optional tools；全部上游 insufficient/invalid 时不进入 Agent planning，直接走 M6 零模型不足结果；
3. 逐窗口建立 target 与 discharge cells；
4. discharge 必须来自直接上游传递或 capability registry 允许的估算关系；
5. contextual-only evidence 可以进入 bundle 和说明，但不能让 cell 通过门禁；
6. 最终没有 target 或 discharge allowed evidence 的窗口保持 indeterminate input。

### 2.4 时间边界

provider batch 可以覆盖多个最终窗口，但请求边界只能是：

```text
[analysis_start - 1 local calendar day,
 analysis_end   + 1 local calendar day]
```

“一天”按 session IANA timezone 的当地日期计算后转换为 UTC，不能简单减加 24 小时。`same_window`、`overlapping_window` 与 `adjacent_local_day` 由唯一 timezone-aware policy 判断；其中 adjacent 要求 source 与 target 分别落在相差恰好一天的当地自然日，同一当地日期内的其他窗口不算 adjacent。扩展记录只用于已登记的 adjacent-time、baseline、aggregation boundary 或 provider statistic 解释；不能因此创建额外结果窗口。

## 3. 模块与依赖方向

M5 继续扩展当前分层，不建立 `m5/` 技术孤岛：

```text
backend/app/
  application/
    evidence_tools/
      executor.py                 # 唯一执行入口
      registry.py                 # agent-facing tool registry
      models.py                   # request/result/scope/budget
      dedup.py                    # run-local single-flight
      collection_service.py       # source -> fragment -> bundle
      bundle_builder.py
      inspection_service.py
      transcript_writer.py
    evidence_policy/
      quality.py
      units.py
      aggregation.py
      applicability.py
      coverage.py
    hydrology_topology/
      geometry.py                 # M5 LineString 补齐与校验
  domain/
    evidence/
      source_records.py
      evidence_payload.py
      evidence_fragment.py
      evidence_cell.py
      evidence_bundle.py
      collection_manifest.py
      provenance.py
    hydrology/
      waterbody_geometry.py
      parameter_capability.py
  ports/
    water_observations.py
    weather_evidence.py
    facility_evidence.py
    upstream_results.py
    transcript_evidence.py
  infrastructure/
    adapters/
      usgs_observations.py
      nws_weather.py
      epa_echo_facilities.py
      nldi.py                    # 复用 M4 adapter，保留 LineString
    runtime/
      m5.py                      # production composition
      m5_test.py                 # 仅测试 composition
```

`domain` 不依赖 provider、FastAPI、SQLAlchemy 或 Agent SDK；adapter 只解析远端协议并返回 source DTO；application 负责授权、顺序、状态、写入与错误语义；tool registry 不泄露 provider URL 或 secret。

## 4. 当前唯一 V1 Domain 模型

### 4.1 SourceRecord 判别联合

`SourceRecord` 是只存在于当前 V1 的判别联合，所有分支共享：

```text
record_type
record_id                    # provider 保证的 identity；没有则使用规范字段 hash
provider
channel
source_uri                   # 脱敏后的公开 URI
retrieved_at
observed_time | valid_time_range
location
provider_quality
raw_unit
provenance
```

分支如下：

| record_type | 关键字段 |
|---|---|
| `water_observation` | station_id、ParameterKey、value、medium、fraction、method、statistic、approval、qualifiers |
| `discharge_observation` | 与 water observation 同构，但参数必须是已登记 discharge 通道 |
| `weather_observation` | station/grid identity、weather_variable、value、duration/statistic |
| `weather_forecast` | grid identity、issued_at、valid range、weather_variable、value |
| `facility` | registry/facility/permit identity、point、program/status、公开属性 |
| `upstream_result` | upstream session/result refs、ParameterKey/window、result kind/source/confidence/evidence closure refs |
| `historical_transcript` | original run/session/item refs、schema、RAG score、matched_by、原 quality/confidence |

provider envelope 不进入 DTO。原始响应只在 adapter contract fixture 中保留；生产 transcript 保存标准化 records 和足以重放的 provenance。

M8实现时将上述占位收敛为typed `HistoricalCaseRef`（原run/session/COMID/context item/result item）、typed score components与有界result/fragment projection；跨run ref使用专门validator，不能进入普通同run `transcript_item_refs`。

### 4.2 EvidencePayload

`EvidenceFragment.payload` 是当前唯一 V1 的判别联合：

```text
DirectRecordPayload(record_ref, displayed_value, displayed_unit)
AggregatePayload(value, unit, method, input_record_refs, input quality counts)
ConversionPayload(value, unit, input_evidence_ref, conversion_id)
ContextPayload(summary_fields, record_refs)
CollectionFactPayload(status, reason, affected_window_ids, record_refs)
UpstreamResultPayload(upstream_session_id, result_ref, closure_refs)
RelationSupportPayload(rule_id, plan_digest, input_evidence_refs, inference_strength, rationale)
HistoricalInferencePayload(original_item_ref, projection)
```

`EvidenceFragment` 保留既有 identity/location/time/provenance 概念，同时增加：

```text
payload
applicability: direct_measurement | allowed_indirect | contextual_only
relation_id: NonEmptyString | null       # 动态规则的稳定 rule_id
series_identity
coverage
quality_class: valid | suspicious | invalid | null  # collection fact 为 null
quality_flags[]
source_record_refs[]
derivation
```

`observed` 不是 EvidenceFragment 的类别，而是 M6 最终结果 source；M5 只提供足以验证一对一 direct record 的闭包。

### 4.3 EvidenceCell 与 Bundle

每个 `EvidenceCell` 唯一对应：

```text
parameter
window_id
role: target | required_support
direct_fragment_refs[]
allowed_indirect_fragment_refs[]
contextual_fragment_refs[]
collection_fact_refs[]
status: supported | unsupported | blocked
reasons[]
```

含义：

- `supported`：至少一个 valid/suspicious direct 或 allowed-indirect fragment 可进入门禁；
- `unsupported`：已完整查询但没有可用证据，或只存在 contextual/invalid；
- `blocked`：provider unavailable、technical incomplete、单位未解析或其他技术事实使完整性无法证明。

`EvidenceBundle` 不再内嵌全部 fragments，只保存：

```text
bundle_id
agent_comid
context_ref
topology_ref
window_refs[]
requested_parameters[]
supporting_parameters[]
cells[]
fragment_refs[]
source_record_item_refs[]
tool_result_refs[]
quality_summary
collection_summary
created_at
```

bundle validator 校验引用去重、每个 parameter/window/role 恰有一个 cell、fragment/reference closure 完整，以及每个目标 cell 都能找到同窗 discharge cell；`RelationSupportPayload` 的每个 input ref 必须精确指向 bundle 内相应 ParameterKey 的 fragment，依赖图不得成环，且 `plan_digest/rule_id` 必须命中 sealed scope 的 `EvidenceRulePlan`。规则输入片段作为目标 cell 的 contextual 辅助引用，不因输入缺失把已经成立的 direct 证据降级。item 由统一 reader 解析后，再用 `ReferenceScope` 校验同 run/current session（context/topology 可为同 run 的 run-global item）。

## 5. Tool registry 与执行契约

### 5.1 Agent-facing tools

| tool | policy | 用途 |
|---|---|---|
| `collect_water_observations` | station-backed conditional-mandatory | 当前水体 station refs 的目标参数与直接 discharge |
| `collect_weather_evidence` | optional | 当前水体附近历史天气或预报 |
| `collect_facility_evidence` | optional | 当前水体整段 corridor 附近设施 |
| `get_upstream_results` | M7 producer slot | 读取直接上游已收敛结果，不接受任意 COMID |
| `search_transcript_evidence` | M8 producer slot | 在当前 scope 和 memory policy 内检索历史 |
| `convert_evidence_unit` | deterministic | 按 unit definition 转换现有 fragment |
| `aggregate_evidence` | deterministic | 按 capability policy 聚合同 series records |
| `inspect_evidence_records` | deterministic | 分页查看已保存 records，不重新请求 provider |

collection tools 的 public schema 不接受 provider、URL、任意 station、任意 COMID 或任意 geometry。server 从 scope 和 capability registry 生成 provider plan。

### 5.2 请求与结果

所有工具请求共享：

```text
tool_call_id
session_id
purpose
window_ids[]
parameter_keys[]
options                 # 仅该工具公开的有界选项
```

所有工具结果共享：

```text
tool_call_id
operation_id
execution_status: completed | failed | cancelled
collection_status: available | empty | unavailable | invalid | null
manifest_refs[]
fragment_refs[]
collection_fact_refs[]
source_record_refs[]
counts: {requests, pages, records, fragments}
deduplicated
deduplicated_from_operation_id
deduplicated_from_result_ref
safe_error: null | {code, message, retryable}
```

`collection_status` 是本次 collection 的摘要；逐窗口精确状态在 manifest/cells 中。deterministic transform tools 不伪造 collection status，使用 `null`。

### 5.3 Registry metadata

每个 tool descriptor 固定：

```text
name
purpose
request_model
result_model
policy_class
allowed_modes
scope_requirements
max_calls_per_session
max_records_per_call
max_result_preview_bytes
producer_status
```

tool roster 由 server 按 session scope 生成：stationless 节点不注册本地 observation tool；`memory_mode=stateless` 不注册 transcript search；M7/M8 producer 未接入时保留 descriptor 供 contract test，但 production roster 不向 Agent 暴露不可执行工具。roster 与 executor 共用同一个 scope authorization predicate；producer readiness 与 scope authorization 分离，合法内部调用仍可得到 producer-unavailable typed result。

## 6. Provider 实现

Provider contract 只以官方文档和 checked-in response fixture 为准：[USGS OGC API v1](https://api.waterdata.usgs.gov/docs/ogcapi/)、[USGS Samples Swagger](https://api.waterdata.usgs.gov/samples-data/docs)、[NWS API](https://www.weather.gov/documentation/services-web-api)、[EPA ECHO Web Services](https://echo.epa.gov/tools/web-services)和 [USGS NLDI](https://api.water.usgs.gov/docs/nldi/)。远端文档变化不能直接改变运行语义；必须先更新 adapter fixture、policy 和当前唯一 V1 契约。

### 6.1 USGS continuous/daily

使用官方 OGC API v1：

```text
GET https://api.waterdata.usgs.gov/ogcapi/v1/collections/continuous/items
GET https://api.waterdata.usgs.gov/ogcapi/v1/collections/daily/items
```

查询由 M4 保存的 monitoring location / time-series identity、ParameterKey 与 UTC interval 组成；响应使用 `rel=next` 链分页，不能自行拼 offset 猜测下一页。API key 只放 `X-Api-Key` header。`continuous` 优先用于 6h/12h/24h 窗口；`daily` 只在 capability/channel policy 明确接受 provider daily statistic 时使用，daily value 不再当原始瞬时记录求和或平均。

### 6.2 USGS discrete samples

使用官方 Samples API：

```text
GET https://api.waterdata.usgs.gov/samples-data/results/narrow
```

固定 `mimeType=text/csv`，使用 `monitoringLocationIdentifier`、`usgsPCode`、`activityStartDateLower`、`activityStartDateUpper` 等官方参数。adapter 以流式 CSV reader 解析并受 response byte/record 上限约束；列名 contract 固化为 checked-in fixture，未知新增列忽略，缺少必需列或行无法解析时为 invalid。`/summary/{site}` 仍只用于 M4 availability，不进入 M5 actual evidence。

### 6.3 Discharge

直接 discharge 使用与 water observation 相同 USGS adapter，并固定 ParameterKey `usgs:00060`；若 capability 为其他已登记 discharge 通道，必须保留原 parameter/statistic，不能改名冒充 00060。站点缺失后的 upstream/估算先保存为 contextual 输入，只有当前 `EvidenceRulePlan` 的规则被 materializer 满足后才产生方向性 support；USGS adapter 不生成推理值。

### 6.4 NWS weather

从当前水体 representative point 调用：

```text
GET https://api.weather.gov/points/{lat},{lon}
```

读取返回的 `forecast`、`forecastHourly`、`forecastGridData` 与 `observationStations` link；随后只访问同源 `api.weather.gov` HTTPS link。历史观测从已发现 station 的 observations resource 获取，预报优先使用 grid data 以保留有效时间和单位。`User-Agent` 使用项目名与可配置联系地址；provider URL 不能由 Agent 提供。

### 6.5 EPA ECHO facility

使用 EPA ECHO CWA facility search 的官方 query-only GET service：

```text
GET https://echodata.epa.gov/echo/cwa_rest_services.get_facilities
```

固定 `output=JSON`，point query 使用 `p_lat`、`p_long`、`p_radius`，`responseset` 受本地上限约束；分页/query identity 必须按官方 response contract 处理。实现时从官方 Swagger 生成并 checked-in 最小 request/response fixture；只请求 CWA/NPDES facility、permit、status 和公开坐标字段，不下载全国批量数据。

ECHO point-radius 能力由 corridor planner 调用：

1. Agent 在 registry 给出的 `[1, 20] mile` 范围内选择半径；
2. tool description 同时给出当前水体 length/bbox、半径上下界和按该半径预计的 provider request 数，Agent 据此选择，不接收自由中心点；
3. server 将 Agent 半径向上取整到 provider 支持的 `{1,2,3,4,5,10,20}` mile 查询半径，并沿完整 LineString 以不大于查询半径的 geodesic 间距生成采样中心，确保覆盖整段而不是只查代表点；
4. 每次调用最多 32 个采样中心；所选半径会超限时，在发出 provider 请求前返回 `facility_radius_too_small_for_budget` 和确定性 `minimum_feasible_radius_miles`，不得截断河段；
5. 对各圆执行有界查询并按 facility/permit identity 去重；
6. 用原始 facility point 对完整 LineString 计算最终最短距离，并按 Agent 选择的精确半径过滤；provider 查询使用的向上取整半径不能扩大最终结果；
7. 保留 Agent radius、provider radius、采样中心、去重和最终 distance provenance。

设施位置始终是 `contextual_only`；只有问题解析 Agent 或后续水体 Agent 明确追加关系，且真实排放/受纳水、时间与引用输入全部满足时，materializer 才能另行生成 allowed-indirect support。

### 6.6 单一来源与失败

第一版每类证据只有上述 official provider。任一 adapter 失败都不静默切换来源、不使用旧 cache、不把失败改写为空。真实 provider smoke 必须显式 opt-in；没有请求或缺少必要配置时报告 `not run`。

provider output 在写 source records 前统一经过 application validator。M7 upstream output 必须复核 record COMID 属于服务端 topology 的直接上游、waterbody session 属于同一 run/COMID 且已 completed、参数与窗口完全受请求授权，并且 result/evidence closure refs 全部存在且属于该 upstream session；失败统一为安全的 invalid provider response，不能进入 materializer 或解锁 cell。

## 7. Geometry 扩展

M5 原子扩展当前 V1：

```text
WaterbodyGeometry
  geometry_ref
  geometry_type = linestring
  full_coordinates[]
  simplified_coordinates[]
  representative_point
  bbox
  length_km
  source = nldi
  source_comid
  simplification_tolerance_m
```

`RiverSegment`、`HydrologyNode`、`HydrologyGraph` 与 `topology_ready v1` 同步增加 `geometry_ref` 及有界摘要；完整 geometry 只在原 typed content 保存一次。NLDI adapter 已请求 `excludeGeometry=false`，M5 保留返回 LineString 而不是丢弃。

校验规则：

- CRS 固定 WGS84 longitude/latitude；
- 至少两个不同的有限点，坐标在合法范围；
- 拒绝 MultiLineString、空线、全重复点和不支持 geometry；
- length 使用 geodesic 分段和，bbox/representative point 从完整线确定性计算；
- 简化使用 Douglas-Peucker，默认 tolerance 25 m、最多 256 点；若误差或点数不能同时满足，增加 tolerance 直到满足，但不得超过 250 m；
- 全部空间判定使用完整线，简化线只供 Agent 摘要和 point-query planning。

geometry 缺失时，observation station 查询仍可按 M4 refs 运行；facility corridor tool 返回 unavailable/`waterbody_geometry_unavailable`，不得退化为任意 target point 搜索。

## 8. 单位、质量、聚合与 applicability

前三类确定性执行 policy 由 [M5 Policy Catalog](m5_evidence_policy_catalog.md) 的 schema 驱动并与 154 项 capability 位于同一 manifest；第四类 relation policy 位于每次分析的 append-only `EvidenceRulePlan`：

- unit definition 表只登记单位维度、别名和确定性变换；
- capability-channel policy 决定某参数可接受哪些 source unit、value semantics 与 aggregation；
- provider quality mapping 把 approval/qualifier 映射为 valid/suspicious/invalid；
- `EvidenceRulePlan` 与通用 materializer 决定 contextual 输入何时产生新的 allowed-indirect support；capability manifest 不保存人类写死的因果规则。

Agent 只能从 registry 给出的 aggregation methods 中选择；executor 再校验 series identity、value semantics、coverage 和单位。`suspicious` 可参与聚合，但 derived fragment 必须继承 flags、输入计数和 suspicious 计数；`invalid` 只供审计。

M5 不进行插值、缺失值填补、异常值替换、跨 series 合并或预测。多记录聚合产生 inferred 候选；只有一对一具体 local direct record（允许可逆单位展示转换）具备 M6 标为 observed 的资格。

## 9. 完整性、分页与 dedup

### 9.1 CollectionManifest

manifest 按 provider batch 保存：

```text
operation_id
provider/channel
request_range
requested_series[]
page_or_chunk_manifests[]
completed_ranges[]
affected_window_ids[]
window_completeness[]
request/page/record/byte counts
completion_proof
```

只有正常走完 provider 的官方 `next` 链、完整 CSV stream 或单响应 contract，才可证明相关窗口 collection-complete。中途失败时，已完整覆盖窗口可继续；无法证明完整的窗口为 `blocked/incomplete_source`，不是 partial coverage 或 empty。

### 9.2 run-local single-flight

canonical request key 为以下规范 JSON 的 SHA-256：

```text
run_id + agent_comid + tool_name + normalized parameters
+ sorted window IDs + policy/manifest digest
```

同 run 相同 key 的并发调用共享一个 in-flight future；成功完成后保留到 run workflow quiesced，再随 run live cleanup 移除。production composition 必须在首个 run 创建前把 `M5Runtime.cleanup_run` 注册为 coordinator 的唯一 run-resource cleanup hook；live cleanup 只有在 hook 成功后才移除 run，失败沿既有 cleanup monitor 重试。应用 shutdown 也必须在 M5 provider context 关闭前对剩余 run 执行同一幂等 hook。failed/cancelled 不进入成功复用。不同 run 永不复用，避免当前数据被旧 cache 替代。

每个调用仍写自己的 `evidence_tool_call v1`；复用者的 `evidence_tool_result v1` 写 `deduplicated=true` 并引用原 operation/result，不复制 records/fragments。

## 10. Transcript 与写入顺序

注册五种 M5 typed content，均保持 schema version `1`：

1. `evidence_tool_call v1`
2. `evidence_source_records v1`
3. `evidence_fragment v1`
4. `evidence_tool_result v1`
5. `evidence_bundle v1`

正常顺序是 call → zero or more source-record batches → fragments/facts → result → final bundle。未知工具，以及 roster、session/window/parameter、跨工具 options 或缺少 descriptor 必需 request shape，属于尚未形成合法调用的 preflight rejection；它们不得触发 scope preparer，也不写 call/result。合法 call 持久化成功后，producer unavailable、budget、provider failure 和已请求取消等运行期结局才写 completed/failed/cancelled result。写入失败沿 M3 pending/retry 语义处理；未确认必需事实不能被 bundle 引用为已持久化完成。

dispatch 已确认写入 manifest/source/fragment 后，即使紧接着发生 deadline 或取消，失败/取消 result 也必须携带这些已确认 references；不得回退为空 value，也不得把该 operation 放入成功 dedup cache。

每类内容提供：model、deserializer、reference extractor、public projection、redaction、visibility、size policy 和 corruption issue。API/SSE 不新造 `evidence_collected` 事件名；仍通过 session 的 `transcript_item` 交付 typed content，生命周期事件只表达执行状态。

## 11. 预算、取消与错误

默认值进入 Settings，并允许部署配置收紧：

| 限制 | 默认值 |
|---|---:|
| provider timeout / request | 20 s |
| provider attempts | 2 |
| total evidence deadline / waterbody | 90 s |
| external collection calls / session | 12 |
| deterministic transform calls / session | 24 |
| pages / operation | 10 |
| records / operation | 10,000 |
| response bytes / request | 8 MiB |
| inspection page size | default 50, max 200 |
| tool result model preview | max 32 records or 16 KiB |
| facility sample centers / call | 32 |

如果官方 provider 的安全上限更低则使用更低值。budget exceeded 是技术 blocker，不是 empty；Agent 不能通过拆分同义调用绕过 canonical budget accounting。

`pages / operation` 是跨全部 facility sample centers 的总预算，不是每中心预算。facility corridor 在 provider I/O 前使用 `min(facility sample centers, pages / operation)` 作为有效中心上限；EPA adapter 在每次逻辑页请求前再次检查同一个 operation 总页数，executor 仍保留聚合后的防御性校验。

取消在新请求、下一页、retry、policy transform、写 bundle 和向下游交付前检查。cancel 胜出后不开始新工作；在途响应只进入 M3 取消审计路径，不补入当前 bundle。

公开错误码至少包括：

```text
evidence_scope_violation
evidence_time_out_of_range
evidence_station_not_authorized
evidence_tool_budget_exceeded
evidence_provider_unavailable
evidence_provider_authentication_failed
evidence_provider_rate_limited
evidence_provider_response_too_large
evidence_provider_response_invalid
evidence_collection_incomplete
evidence_unit_unresolved
evidence_aggregation_not_allowed
waterbody_geometry_unavailable
evidence_producer_unavailable
```

错误正文不包含 URL query、secret、Authorization、provider raw body 或内部异常。

## 12. 测试接口与验证策略

### 12.1 application contract

测试直接调用 production 使用的 `EvidenceToolExecutor.execute(scope, request)`。fake ports 只替代外部响应，不替代 executor、registry、policy、writer、dedup、deserializer 或 bundle validator。

### 12.2 可选 HTTP test app

`create_m5_test_app()` 可以挂载 internal test router，用于跨层 contract test；它：

- 不被 production app import 或注册；
- 不进入 production OpenAPI；
- 不是环境变量开启的隐藏生产功能；
- 只接受 fixture scope identity，不接受任意 provider URL/secret；
- 仍创建合法合成 run/session 并写真实 transcript path。

前端永远不接入该接口。

### 12.3 必需 fixture

checked-in fixtures 至少覆盖：

- OGC continuous/daily 正常、empty、working/approved、qualifier、next-page、中途失败；
- Samples CSV 正常、empty、quoted field、未知列、缺列、非法数值、byte/record limit；
- NWS point discovery、observation、grid forecast、单位与时间段；
- EPA CWA facility 单点、多采样圆、重复 facility、边界内外和缺坐标；
- geometry 空/非法/长河段/弯曲河段/简化误差；
- direct/suspicious/invalid、unit conversion、禁止聚合、coverage 边界；
- station-backed 全空停止、部分继续、stationless upstream、逐窗口 discharge 缺失；
- single-flight、取消、budget、持久化失败、unknown typed content。

### 12.4 opt-in smoke

真实 smoke 分 provider 独立运行并先做配置/网络 preflight。报告 endpoint contract、实际 request/page/record 数和安全摘要；无凭据、网络不可达或 0 请求统一为 `not run`。不得把一次启动成功或 HTTP 200 宣称为质量/有效性验收。

## 13. M6/M7/M8 接力

M6 使用：

```text
EvidenceBundleRef
EvidenceCell[]
fragment/source-record read ports
tool roster + EvidenceToolExecutor
```

M6 负责调用前门禁和最终 result validator；M5 不输出最终结果。

M6 waterbody system prompt 至少固定以下行为，不由后端阈值 detector 代替：发现不同 series 的读数明显不一致时，先检查 station、time、method、medium/fraction、statistic、quality 与 coverage；引用所有影响判断的证据；说明采用某条、分别保留或保持不确定的理由；无法合理解释时输出受限判断或 indeterminate；不得静默删除相反记录。validator 只校验 observed 的一对一 direct-record 闭包和 inferred 的完整引用，不判断数值是否“足够冲突”。

M7 实现 `UpstreamResultPort`，只返回当前水体直接上游依赖中 usable 的已收敛结果；正常无 evidence、failed、cancelled 通过独立的 session-owned dependency manifest 表达，其中 failed 对继续执行的下游只显示 `invalid` 且不传 evidence。M5 的公共 tool request schema 不变。

M8 实现 `TranscriptEvidencePort` 与 index/search；只索引 M5 已标为 candidate、且 session/run/persistence 条件均满足的安全投影。历史命中保持 contextual/historical provenance，不满足本次 target/discharge gate。

## 14. 完成标准

M5 后续实现只有同时满足以下条件才可标记完成：

- 当前唯一 V1 类型、154 项 capability policy 与 geometry 原子更新完成，无 V2/read union；
- station-backed/stationless 两条流程和逐窗口 discharge 规则均有确定性测试；
- official provider adapters 具有 checked-in contract fixtures，技术失败不冒充 empty；
- unit/quality/aggregation/applicability 全部由可审计 policy 驱动；
- 五类 typed content、引用闭包、读取、SSE preview、脱敏与补写通过回归；
- production OpenAPI 不存在 tool invocation 路由，test app 可独立验证；
- M1–M4 回归、M5 focused tests、架构依赖检查和 `git diff --check` 通过；
- 外部 provider、PostgreSQL 和三平台未执行项按实际写 `not run`/`partial`，不虚报。
