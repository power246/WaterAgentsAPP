# M4 Query Understanding + Hydrology Topology 实施设计

更新日期：2026-09-29

> 状态：Milestone 0–10 已完成本机实现与集成审计。固定 fake workflow、154 项离线审计、运行时协议和 M1–M3 非 PostgreSQL 回归已有本机证据；隔离 PostgreSQL、需凭据 provider/parser smoke 与三平台 CI 仍按实际环境保持 `not run`/`partial`。实际复选项和证据记录见 [M4 Implementation TODO](m4_implementation_todo.md)。

本文把 [M4 设计决策](m4_query_understanding_topology_design.md) 落到模块、类型、workflow、ports、持久化和测试接缝。API 字段与公开错误见 [M4 API 文档](m4_query_understanding_topology_api.md)。M4 继续遵守 M3 的四表、typed-content registry、统一 writer/reader 和两层 SSE，不新增第二套运行记录。

## 1. 核心结论与范围

M4 负责两件事：

1. 将用户 query 和前端结构化选择解析为唯一、已验证的 `AnalysisContext`；
2. 从目标 COMID 向上游构建唯一、可重放的 `HydrologyGraph`，标出候选 evidence inlet、有效路径及排除原因。

M4 不负责：

- 获取并保存完整观测、天气、排放或设施数据；
- 判断真实 evidence 的质量、单位换算和最终充分性；
- 调用水体模拟模型或创建水体 Agent session；
- 实现跨 run memory/RAG；
- 新增独立的 M4 产品开关或单独发布形态。

M5 必须重新验证 inlet 的真实数据，M6 才能在 evidence gate 通过后调用水体模型。M1–M9 是内部实施切片，最终统一做端到端验收。

## 2. 执行流程与状态边界

一次正常执行按以下顺序推进：

1. HTTP 层严格校验请求形状、日期成对/顺序和枚举；
2. M3 create 流程先按 `Idempotency-Key + 原始 body bytes` 查询已有创建：同 body 重试直接返回原回执，不重新应用会漂移的日期窗口或模型目录；
3. 只有全新 key 才从同一次 Clock/模型目录快照校验结构化日期动态范围和模型 capability，再原子保存扩展后的 `analysis_request v1`、run 创建事实和不可变配置；
4. workflow 准备执行解析模型时创建 `query_parser` session；
5. 保存 `query_parser_input v1`，流式生成 `query_parser_message v1`；schema 非法时最多修复一次；
6. deterministic resolver 按优先级解析地点、日期、参数、实际粒度与 mode；
7. 全部校验通过后，保存唯一的 `query_understood v1`，其正文即 `AnalysisContext`，并完成 parser session；
8. run 进入 topology 阶段，确定性 BFS 查询河网与低成本 availability metadata；
9. 保存唯一的 `topology_ready v1`，其正文即 `HydrologyGraph`；
10. 更新 run 的轻量派生状态，交给 M5；本阶段不创建任何 waterbody session。

结构非法或全新创建的动态 date/model capability 非法时返回 `422`，不产生 run。已经存在的同 key/同 bytes 重试返回原回执，不受后来跨日或目录变化影响；同 key/不同 bytes 仍按 M3 冲突。只有模型调用或外部解析后才能发现的语义问题，保留 parser session 和 transcript，随后将 session/run 置为 `failed`。topology 技术故障发生时，已经持久化的 `AnalysisContext` 保留，run 失败，不把故障改写成“无站点”。

取消沿用 M3 临界区：一旦 cancel 胜出，不再开始新的模型调用、resolver 查询、BFS frontier 或下游阶段；已接收的流式正文按 M3 规则定稿为 `incomplete`，已保存的结构化事实不删除。

## 3. 模块与依赖方向

实际实现落在当前骨架的以下模块中，不建立 `m4/` 技术孤岛：

```text
backend/app/
  api/schemas/
    analysis_runs.py                 # 最终 create DTO 与公开投影
  application/
    analysis_runs/
      models.py                      # run 配置/快照扩展
      cancellation.py                # workflow 共用取消/timeout 等待
      query_topology_workflow.py     # query + topology 单一执行链
    query_understanding/
      context_models.py              # ParsedIntent、AnalysisContext
      parser_workflow.py             # parser session 与一次 schema repair
      context_resolver.py            # deterministic resolution orchestration
      location_resolver.py
      date_resolver.py
      parameter_resolver.py
    hydrology_topology/
      builder.py                     # bounded deterministic BFS
    station_availability/
      service.py                     # 参数/时间 qualification
    transcripts/
      registry.py                    # M4 typed-content 注册
  domain/
    hydrology/
      hydrology_graph.py             # HydrologyGraph invariant
      parameter_capability.py        # 可执行能力注册项
  ports/
    geocoding.py                     # 地点解析与行政区事实
    hydrology_data.py                # COMID、direct upstream、station association
    station_inventory.py             # 参数/时间 availability metadata
    model_catalog.py                 # 可公开选择的 provider/model
    parameter_catalog.py             # 完整目录候选发现
  infrastructure/adapters/
    openai_responses_parser.py       # Responses API parser adapter
    google_geocoding.py
    nldi.py
    usgs_waterdata.py                # WDFN OGC v1 metadata
  infrastructure/runtime/
    m4.py                            # 唯一 production composition factory
```

`domain` 只保存可独立验证的值对象和图 invariant；`application` 决定优先级、失败语义和阶段；`ports` 不导入 application/FastAPI/SQLAlchemy；adapter 负责远端协议与错误归类。现有 `MonitoringStationPort.find_for_segment()` 无法表达时间覆盖，实施时由新的 `StationInventoryPort.check_availability()` 替代并删除孤立生产入口，而不是保留两套近义主路径。

正式 `create_app()` 的 lifespan 只通过 `build_m4_runtime(settings)` 装配 OpenAI parser、Google geocoding、NLDI、USGS parameter/metadata、resolver、availability 与 topology builder；组件缺失、parser provider 不支持或凭据缺失时在数据库启动前失败。application factory 的显式 `workflow=` 只作为测试/固定 smoke 注入接缝，生产入口不回退 fake 或空 workflow。parser/geocoding/metadata timeout 与 topology limits 均从同一 `AppSettings` 进入该 factory。

## 4. 最终请求与不可变配置

最终 create body 由以下字段组成：

| 字段 | 必填 | 含义 |
|---|---:|---|
| `query` | 是 | 用户原始业务问题；不得为空。 |
| `target` | 否 | 地图选择；至少含 `comid` 或 `coordinates`，正常前端同时发送。 |
| `date_range` | 否 | `start_date` 与 `end_date` 必须成对出现，均为 date-only。 |
| `time_resolution` | 否 | `auto / 6h / 12h / 24h`；前端默认显式发送 `auto`，API 省略时为 `12h`。 |
| `parser_model` | 是 | `{provider, model}`，来自后端发布的模型目录。 |
| `waterbody_model` | 是 | `{provider, model}`，与 parser 可不同。 |
| `memory_mode` | 是 | `stateless / memory_augmented`；M4 只固化并传递。 |

M4 直接扩展当前唯一的 `AnalysisConfigurationSnapshot`，避免在 `auto` 尚未解析时伪造实际粒度：

```text
AnalysisConfigurationSnapshot
  analysis_timezone: IANA timezone
  time_resolution_preference: auto | 6h | 12h | 24h
  time_resolution_preference_source: frontend | default
  parser_model: ModelSelection
  waterbody_model: ModelSelection
  memory_mode: stateless | memory_augmented
```

实际 `time_resolution` 与 `time_resolution_source` 只在 `AnalysisContext` 中出现。省略粒度时配置写入 preference=`12h`、source=`default`；显式 `auto` 时配置写入 preference=`auto`、source=`frontend`，解析后 context 的 source 才是 `auto`。

配置在 run 创建事务中固化，后续模型目录、默认 timezone 或系统设置变化不改写已有 run。两个模型选择只保存安全身份，不保存 endpoint 凭据、API key 或 Authorization。

`time_resolution_preference` 直接替换阶段性 `time_resolution` 配置语义，并加入双模型选择和 memory mode；create receipt、history/read DTO、生命周期正文、测试 fixture 和所有调用方在同一实现变更中一起更新。configuration 不增加 `schema_version`，也不保留 M3/M4 union。M1–M9 尚未进入正式联调，开发数据库中的阶段数据按当前 HEAD 迁移或重建；启动遗留修复只处理当前模型可读的未终态 run，不为内部旧形态增加恢复分支。

## 5. Query Parser 输入与输出

### 5.1 `ParsedIntentV1`

解析模型只能输出以下候选结构：

```text
ParsedIntentV1
  location_mention: string | null
  coordinate_mention: GeoPoint | null
  start_date: date | null
  end_date: date | null
  parameters: 1..N ParameterConcept
  ambiguities: ClarificationIssue[]

ParameterConcept
  concept: non-empty string
  rationale: short user-visible string
  explicitly_named: bool
```

模型不输出最终 COMID、`ParameterKey`、UTC、mode、resolution、graph 或“数据足够”结论。`rationale` 只解释为什么该参数与用户目标有关，不要求或保存隐藏思维链。任何实质歧义进入 `ambiguities`，不能用自由文本藏在 rationale 中继续执行。

地点和日期字段允许同时为空，因为前端可能已经提供；参数不得为空。开始/结束日期必须同时存在或同时为空。模型不得把 structured fields 改写成自己的选择。

### 5.2 prompt 与调用预算

parser 输入包含：

- 原始 query；
- 前端 target/date/resolution 的安全表示，用于识别冲突；
- run 创建时固化的当地日期和 timezone；
- 首批可执行能力的规范名称和简要语义；
- 输出 JSON schema；
- 少量覆盖“显式参数、目标驱动推测、歧义上报”的 few-shot。

few-shot 不是意图模板或分支表。模型必须能够从新目标自主推测参数，也必须原样报告关键但未支持的概念，不能只在已公布列表中挑一个较弱替代项。

正常调用一次。仅当返回不能通过 `ParsedIntentV1` schema 时，允许一次 schema repair；repair 输入只带原安全输出和校验摘要。语义歧义、参数不支持、地点无匹配和日期越界不触发 repair。第二次 schema 仍非法时失败为 `query_parse_failed`。

parser 的 LLM client 不注册 geocoding、NLDI、parameter search 或其他工具；provider 侧也不给对应工具权限。应用层在模型调用结束后自行调用 ports，因此不存在 parser tool loop。

## 6. AnalysisContext

`query_understood v1` 正文使用下列唯一模型：

```text
AnalysisContext
  schema_version: 1
  query: string
  decision_instant: UTC datetime
  analysis_timezone: IANA timezone
  target: ResolvedTarget
  requested_date_range: LocalDateRange
  time_range: UTC [start, end)
  time_segments: TimeSegment[]
  mode: gap_filling | prediction | mixed
  time_resolution: 6h | 12h | 24h
  time_resolution_source: frontend | default | auto
  params_used: ResolvedParameterIntent[1..6]
  supporting_params: SupportingParameterIntent[]
  parser_model: ModelSelection
  waterbody_model: ModelSelection
  memory_mode: stateless | memory_augmented

ResolvedTarget
  comid: string
  point: GeoPoint
  display_name: string | null
  country_code: US
  subdivision_code: US-xx
  source: frontend | query

ResolvedParameterIntent
  key: ParameterKey
  canonical_name: string
  canonical_unit: string
  source: explicit | inferred
  rationale: string

SupportingParameterIntent
  key: ParameterKey
  canonical_name: string
  canonical_unit: string
  reason: system_required | capability_required
```

`decision_instant`、timezone 和当地 `today` 来自同一次 `ClockPort.now()` 快照。`time_segments` 是 one/two element：纯历史、纯预测各一段；mixed 严格在当地今天 `00:00` 转 UTC 后切成历史段和预测段。窗口由既有 `build_time_windows()` 根据实际 resolution 生成，最大 60 个。

`AnalysisContext` 自校验以下 invariant：

- target point、COMID 和行政区已通过一致性检查；
- 本地日期均落在 `[today-7, today+7]`，结束日期包含整天；
- UTC `time_range` 与 local dates/timezone 精确对应，覆盖 DST；
- `mode` 与 segments 顺序一致，mixed 只能是 gap_filling 后 prediction；
- `params_used` 非空、唯一、不超过 6，且每项都存在于当前唯一能力注册表；
- `params_used` 与 `supporting_params` 不重复，其并集必须包含 `usgs:00060` discharge；若 discharge 本身是目标参数，则无需再复制到 supporting 列表；
- 模型和 memory mode 与 run 配置完全一致；
- preference 为 auto 时实际 source 必须为 auto，其他 source 组合合法。

## 7. Deterministic resolvers

### 7.1 输入优先级与冲突

resolver 顺序固定：

1. 前端 `target` 和 `date_range` 是对应业务值的最高优先级；
2. 当前端缺失某字段时，才使用 parser 候选；
3. 前端和 query 同时明确表达且解析为不同目标/日期时，返回 conflict；
4. resolution 使用前端/API preference，不从 query 偷换；query 明确要求不兼容粒度时提示冲突；
5. mode 永远从已验证日期推导，前端和 LLM 均不能直接指定；
6. 参数永远来自 query 推断，前端没有参数表单。

冲突检查比较的是解析后的业务身份，而不是字符串：同一 COMID 的两个地点名称不是冲突；相同日期的“tomorrow”和 ISO date 不是冲突。无法完成比较属于无法确认，不得当作相同。

### 7.2 地点与 CONUS

`LocationResolver` 返回 point、国家、一级行政区、显示名称与 provider identity。正向 geocoding 可以对 US 做限制，但 region bias 不算校验；最终只接受 CONUS 48 州和 District of Columbia 的明确行政区结果。

组合规则：

- 只有坐标：反向地理校验行政区，再用 NLDI position point-in-polygon 查 COMID；
- 只有 COMID：读河段和代表点，反向地理校验行政区；
- COMID + 坐标：分别验证，再要求 position 返回相同 COMID；
- 只有文字：正向 geocode 必须得到唯一可接受候选，再按坐标流程；
- 多个实质候选、非 CONUS、无 COMID或不一致均停止。

不得使用 NLDI hydrolocation/raindrop 的近邻吸附来掩盖无覆盖；位置在 CONUS 但没有 NHDPlusV2 COMID 时返回 `location_not_supported`。

### 7.3 日期、粒度与 mode

前端 structured dates 在创建前即可执行形状、顺序和动态 ±7 天门禁；query-derived dates 在 parser session 后执行相同规则。超界不截短。

自动粒度只依据包含的日历日数量：1–7 天为 6h，8–15 天为 12h；24h 只接受显式选择。省略 API 字段为 12h/default。显式 `null` 非法。

### 7.4 参数解析

`ParameterCatalogPort` 用完整目录为 parser 输出提供候选发现；`ParameterCapabilityRegistry` 是是否可执行的唯一来源。流程固定：

1. 标准化业务概念；
2. 先匹配 capability 的规范名称和显式同义词；
3. 必要时从完整目录发现候选，但不按最高相似度直接执行；
4. 只有唯一候选能映射到 capability 时生成 `ParameterKey`；
5. 多个实质候选返回 `parameter_ambiguous`，目录有但能力表无则返回 `parameter_unsupported`；
6. 任一关键参数失败时整次解析失败，不静默删除或替换；
7. 去重后超过 6 个用户目标参数返回 `too_many_parameters`；系统自动加入的支持参数不计入该上限；
8. resolution 成功后把 `usgs:00060` discharge 加入分析计划；若它已是用户目标参数，不生成第二份同义条目。

不使用一个通用 similarity threshold 冒充语义确定性。候选发现的分数只进内部诊断，不进入最终业务裁决。

## 8. 参数能力注册表

第一版正式联调前交付的 production capability manifest 精确覆盖 [M4 参数能力目录](m4_parameter_capability_catalog.md) 的 154 个条目。下面列出跨模块锚点；完整基线、官方代码/单位和数据通道以该目录为准：

| `ParameterKey` | 规范名称 | canonical unit | 角色 | 常见概念示例 |
|---|---|---|---|---|
| `usgs:00060` | discharge | `ft3/s` | `target, required_support` | 流量、streamflow、discharge；每次分析必带 |
| `usgs:00010` | water temperature | `degC` | `target` | 水温、water temperature |
| `usgs:00300` | dissolved oxygen | `mg/L` | `target` | 溶解氧、DO、缺氧风险 |
| `usgs:00400` | pH | `standard_unit` | `target` | 酸碱度、pH |
| `usgs:00095` | specific conductance | `uS/cm@25C` | `target` | 比电导率、conductivity |
| `usgs:63680` | turbidity | `FNU` | `target` | 浊度、turbidity、雨后浑浊 |

除锚点外，manifest 必须覆盖目录规定的全部问题族。目录是交付目标，不是“已实现”声明；任何条目只有接通可查询 provider、canonical unit、availability、evidence 规则和验证 fixture 后才能设为 enabled。完整 USGS 目录仍只负责候选发现。

每个注册项还包含：

```text
ParameterCapability
  key
  canonical_name
  canonical_unit
  roles[]                    # target / required_support
  aliases[]
  availability_providers[]
  observation_providers[]
  allowed_indirect_evidence_rules[]
```

初始允许的侧面证据只登记可审计关系，不把它们标成直接测量：同一参数的上游观测、同一位置相邻时间基线，以及对 turbidity 的 precipitation + discharge directional support。discharge 也必须经过 evidence gate：直接观测优先，只有注册表明确允许的流域/水位等估算关系才能作为较弱侧面证据；完全无证据时，受影响的水体 × 时间段不得调用水体模型。其他跨参数关系默认不允许，直到 M5/M6 用明确依据和测试扩展。水体模型不能临时声明新的 evidence relation。

能力注册表由后端唯一的 checked-in manifest 构建一次，并同时驱动 resolver、availability 查询、单位规则和前端公开说明；禁止在不同模块维护多份 code allowlist。manifest 校验必须保证 key 唯一、与目录的 154 个 key 精确一致、上述问题族都有覆盖、所有 enabled 条目都有单位/provider/availability/evidence rule/fixture，且 `usgs:00060` 唯一并标记为 required support。任一目录条目尚未 enabled 时，M9 后的正式联调门禁失败，不能通过减少清单或把目录存在当作能力实现来绕过。

## 9. Availability qualification

M4 只进行低成本资格探测：

```text
StationAvailabilityRequest
  comid
  coverage_units: (ParameterKey, segment_index)[]
  historical_ranges[]
  prediction_baseline_ranges[]

StationAvailability
  station_id
  comid
  provider
  covered_units[]
  series_refs[]
  checked_at
```

`coverage_units` 必须由用户目标参数与自动支持参数的去重并集生成，因此每个 time segment 都包含 discharge。discharge 已是用户目标时仍只查询一次；支持参数和目标参数在图中共用同一个 `ParameterKey` 身份，但结果层保留角色来源。

coverage unit 是“参数 × time segment”，不是只按参数。历史段只有在 metadata 的 earliest/latest envelope 覆盖请求段，或 capability 指定的 existence probe 在段内发现记录时才取得候选资格；预测段要求相同参数最近观测时间不早于预测起点前 48 小时。metadata envelope 只是“可能覆盖”，M5 必须读取真实记录并验证空洞、质量、单位和精度。

站点与河段关联来自 NLDI 或同等级水文关联事实，不能只按坐标最近距离。成功空结果返回空 tuple；限流、超时、无效响应和 provider unavailable 抛出类型化 port error。topology builder 只把前者解释为没有合格站点。

新实现使用：

- [NLDI](https://api.water.usgs.gov/docs/nldi/) 的 NHDPlusV2 网络、position 与站点关联；
- [USGS Water Data OGC API v1](https://api.waterdata.usgs.gov/docs/ogcapi) 的 monitoring/time-series metadata；
- capability 明确指定时才使用 USGS Samples 等 existence probe。

不新增旧 `waterservices.usgs.gov` 依赖。远端 API version、collection 和字段映射集中在 adapter，不泄漏到 domain。

## 10. HydrologyGraph

### 10.1 模型

```text
HydrologyGraph
  schema_version: 1
  target_comid
  coverage: complete | partial | none
  nodes: HydrologyNode[]
  edges: HydrologyEdge[]
  branch_outcomes: BranchOutcome[]
  execution_layers: ComidLayer[]
  limits: TopologyLimits
  stats: TopologySearchStats

HydrologyNode
  comid
  point
  depth
  cumulative_distance_km
  is_target
  is_inlet
  participation: active | excluded
  qualified_units[]
  station_refs[]
  excluded_reasons[]

HydrologyEdge
  upstream_comid
  downstream_comid
  distance_km
  participating

BranchOutcome
  branch_root_comid
  coverage_unit
  outcome: qualified | no_qualifying_source | search_limit_reached
  stop_comid
  path_to_target[]
  reason
  blocked_upstream_comid: comid | null
```

`nodes`/`edges` 保留本次完整 BFS 范围；`participating=true` 的边和 active nodes 是所有 qualified inlet 到 target 路径的并集，并始终保留 target 本身。节点可以只对部分 coverage units 成为 inlet；未覆盖单元继续向上游。`execution_layers` 只从 active DAG 拓扑排序产生，顺序从最上游到 target，但只是潜在依赖顺序，不代表 session 已创建或一定会调用模型。

图 invariant：

- COMID 唯一，恰有一个 target；target 始终 active 且位于最后一个 execution layer；
- edge 两端必须存在，禁止 self-edge 和重复边；
- active 子图无环，所有 active 非 target 节点都能到达 target；
- inlet 至少覆盖一个 unit，qualified outcome 必须引用 inlet；
- excluded node 不得进入 execution layer；
- `coverage=complete` 表示每条自然分支的每个 unit 都找到资格源；`partial` 表示至少一个 qualified 且至少一个自然缺失/上限；`none` 表示没有任何 qualified unit，但 target 仍保留给 M5 查询允许的本地/非站点 evidence；
- `search_limit_reached` 与 `no_qualifying_source` 永不互换。

### 10.2 bounded deterministic BFS

默认限制：100 km/路径、12 hop/路径、128 unique COMID/整图、8 个外部请求并发。限制属于后端配置，前端不可覆盖。

算法：

1. 从 target 建立全部 coverage units，先查询 target 自身 station availability；
2. 对未覆盖 unit 调用 `get_direct_upstreams()`；
3. frontier 按 `(depth, cumulative_distance_km, comid)` 排序；
4. 同一 frontier 可并发读取 segment/availability，但结果按 frontier 顺序合并；
5. 一个节点覆盖某 unit 后，该 unit 不再沿该分支继续；其他 unit 继续；
6. 汇流遇到同一 COMID 时合并待覆盖 unit，不重复计数或查询；
7. 检测到环或不可能的 edge 时以 `topology_invalid` 失败，不删边继续；
8. 自然到达无上游且仍未覆盖，记录 `no_qualifying_source`；
9. 任一硬上限阻止 child 继续时，逐 child 记录 `search_limit_reached` 和 `blocked_upstream_comid`；
10. 搜索完成后回溯 qualified paths，生成 active union 和稳定 execution layers。

因此 `coverage=none` 不等于最终 `insufficient_evidence`：M5 对 stationless target 不制造本地 observation 空查询，而是消费直接上游结果，并由 Agent 根据任务和缺口按需调用天气、设施或 capability 允许的其他来源。discharge 则始终是每个窗口的必需支持参数，必须由直接上游结果或已登记估算关系取得。每个待推理单元既要有目标参数的直接或允许侧面证据，也要有 discharge 的直接或允许侧面证据；任一必需部分完全无证据时，M6 gate 都以 0 次水体模型调用产生该单元的 `indeterminate`。若两者都有合格证据，也只会调度 gate 允许的 target/range。

NLDI navigation 请求必须显式限制 distance；M4 topology port 的语义必须是“直接上游邻居”，adapter 不得把一次 navigation 返回的所有祖先误当 direct edge。M5 在当前唯一 V1 中扩展该 port/model 并保留每个水体的 LineString geometry，供整段 corridor 查询使用；这是 M5 的原子实现责任，不新建 M4/M5 版本或兼容分支。上述行为分别用固定河网 fixture、geometry fixture 和 adapter contract test 证明。

## 11. Typed content、持久化与读取

production registry 增加：

| content type | version | 归属 | 用途 |
|---|---:|---|---|
| `analysis_request` | 1 | run | 当前最终原始请求；直接扩展既有字段。 |
| `lifecycle_event` | 2 | run/session | 当前生命周期正文；run configuration 直接增加字段。 |
| `query_parser_input` | 1 | parser session | 实际安全模型输入、prompt template version 与模型身份。 |
| `query_parser_message` | 1 | parser session | 可流式生成的解析模型文本。 |
| `query_understood` | 1 | parser session | 唯一 `AnalysisContext`。 |
| `topology_ready` | 1 | run | 唯一 `HydrologyGraph`。 |

`analysis_request v1`、`lifecycle_event v2` 和 `run_snapshot v1` 在当前唯一模型中直接增加 M4 所需字段；没有 M3/M4 双注册、旧配置 union 或 milestone compatibility path。实现必须在同一改动中更新 registry、读写模型、持久化、OpenAPI/SSE fixture 和测试。所有新类型都经 registry 在进入 live buffer 前校验/脱敏，并在持久化前 recheck。正文超过 M3 阈值仍只存一份完整内容，读取返回 preview/reference。

parser session 的 input、message、context 与 session completed 使用同一 M3 writer 入口。每次真实 parser 调用各自保存同 operation 的 input/message；repair 不复用首轮 input 充当审计记录。`query_understood` 成功写入 live 原事实后才允许 BFS 消费；不得在 resolver 内另存一个 dict 缓存作为真实上下文。`topology_ready` 同理是当前/历史装配的唯一图事实。

run history 的 `context_summary` 从已解析的 `query_understood` 投影，不从 query 重算。run detail 返回 run-owned `topology_ready` 和引用的 parser context；完整 graph 通过既有全文接口读取。投影失败时沿用 M3 `issues`，不能返回看似完整的空值。

## 12. 两层 SSE

不增加 M4 SSE endpoint：

- parser 文本继续走 session `message_started / message_delta / message_finalized`；
- `query_parser_input`、`query_understood` 使用 session `transcript_item`；
- topology 没有 Agent session，完成时 run 状态变化唤醒下一份完整 `run_snapshot`；
- run snapshot 只增加 `analysis_phase`、`context_ref`、`topology_ref` 和轻量 waterbody summaries，不内联 graph；
- run 重连仍首发最新完整快照且无 SSE id；session 仍按整数 cursor 补过程。

M4 使用的 phase 值为 `queued`、`query_understanding`、`topology_building`、`topology_ready`、`terminal`。字段类型是稳定 code，后续 M5–M9 可增加阶段；前端未知值按通用进行中展示，不能把它当生命周期状态。

状态变更与 item reference 必须来自同一原事实视图：snapshot 不得先宣告 `topology_ready` 再返回不存在的 item。run snapshot 可合并中间通知，但不能丢失最新 reference。

## 13. Ports 与外部失败

主要 port 语义：

```text
GeocodingPort.resolve_text(text) -> resolved candidates
GeocodingPort.reverse(point) -> administrative location | None
ComidLookupPort.lookup_position(point) -> comid | None
TopologyDataPort.get_segment(comid) -> segment | None
TopologyDataPort.get_direct_upstreams(comid, distance_limit) -> segments
StationInventoryPort.check_availability(request) -> station availability[]
ParameterCatalogPort.find_candidates(concept) -> catalog candidates[]
ModelCatalogPort.require_selectable(selection, role) -> selected capability
```

所有 port 统一约定：`None`/空集合只表示远端成功回答“无匹配”；不可用、超时、限流、认证失败、协议损坏、字段缺失分别转成类型化异常。application 再映射稳定 public error 和 `retryable`，domain 不依赖 HTTP status。

只对幂等 GET/metadata 查询做有界重试，默认首试加一次抖动退避；parser schema repair 不算网络重试。建议阶段预算：geocoding 单请求 10 秒、NLDI/WDFN 单请求 15 秒、整个 topology 120 秒、parser 每次 45 秒。总预算耗尽后停止新调用并失败，不能继续产生 partial graph 冒充自然缺失。

## 14. 公开错误与终态

错误分三层：

- HTTP `422`：请求 JSON/枚举/成对字段/structured date 窗口/模型选择在创建前即可确定非法；
- parser/resolver 业务失败：run/session `failed`，例如需要澄清、地点不支持、query-derived 日期越界、参数歧义；
- 技术失败：run/session 或 run `failed`，例如模型不可用、NLDI/WDFN 超时、协议损坏、图 invariant 失败。

错误正文继续使用 M3 `PublicError(error_id, code, message, failed_stage, retryable)`；内部 provider body、URL query 中的 key、prompt secret 和堆栈只进脱敏诊断。失败后不创建 topology/waterbody session。

“请求有效但 M5 实际取数后目标参数或必需 discharge 完全无证据”不是 M4 error：M5/M6 为受影响单元生成确定性 `indeterminate` 并完成 run，不调用水体模型。M4 的 `coverage=none` 只表示没有资格 inlet；后续仍可检查 target/允许的其他 evidence source，但必须经过同一 evidence gate。

## 15. 配置与运行时来源

建议增加以下严格环境配置，均在 composition root 一次解析：

```text
WATERAGENTS_TOPOLOGY_MAX_DISTANCE_KM=100
WATERAGENTS_TOPOLOGY_MAX_HOPS=12
WATERAGENTS_TOPOLOGY_MAX_NODES=128
WATERAGENTS_TOPOLOGY_MAX_CONCURRENCY=8
WATERAGENTS_PREDICTION_BASELINE_HOURS=48
WATERAGENTS_MAX_TARGET_PARAMETERS=6
WATERAGENTS_QUERY_PARSER_TIMEOUT_SECONDS=45
WATERAGENTS_TOPOLOGY_STAGE_TIMEOUT_SECONDS=120
```

数字使用严格类型和合理范围，未知/非法值启动失败，不静默回默认。模型目录、参数 capability、provider API version 和允许 evidence relation 同样只由一个运行时来源构建。参数上限只计算用户目标，required support 不占用；discharge 必带规则不是可由前端或普通运行设置关闭的开关。M9 的设置接口可公开安全 metadata，但不能回显 secrets 或让前端修改 topology hard limits。

## 16. 测试与验收设计

### 16.1 domain / resolver

- request `target` 至少一项、日期成对、strict enum/extra forbid；
- 前端值优先、相同业务值不冲突、实质冲突失败；
- fake clock 下 ±7 天闭区间、date-only inclusive end、DST、跨午夜不漂移；
- auto 1–7/8–15 天、显式 24h、省略 12h/default、最多 60 windows；
- explicit/inferred 参数、few-shot 未覆盖的新目标推测、unsupported/ambiguous/no parameter、去重和 >6；
- manifest 与目录 154 个 key 精确一致、问题族覆盖、key/单位/provider/availability/evidence rule/fixture 完整性；discharge 自动加入、不占目标上限、目标与支持角色不重复；
- CONUS/DC 接受，AK/HI/territory/foreign 拒绝，point 无 COMID、COMID/point mismatch 不吸附。

### 16.2 parser / model

- 正常一调用；仅 schema invalid 修复一次；第二次失败停止；
- 语义错误不触发 repair；provider 不可用不静默 failover；
- parser client 的实际 tool roster 为空；
- 两个模型选择分别验证并固化，日志/transcript/OpenAPI 不含 secret；
- cancel 后不再开始 repair 或 resolver。

### 16.3 topology

- target station、单链、多支流、汇流、重复节点、环和无上游；
- coverage unit 按参数 × segment 独立停止，同站部分覆盖时其他 unit 继续；
- 每个 segment 的 coverage units 包含去重后的 discharge；缺少 discharge 直接/允许侧面证据时，对应推理单元不调用模型；
- 48h prediction baseline 边界；metadata envelope 与 actual evidence 职责不混淆；
- 100 km、12 hop、128 node 分别命中，reason 正确；
- 外部完成顺序随机时 graph JSON、edge order、layers 完全稳定；
- success-empty 与 timeout/invalid/rate-limit 分开；技术失败不生成自然缺失；
- active path union、excluded facts、coverage 和 branch outcomes 满足 invariant。

### 16.4 transcript / API / SSE

- 扩展后的 `analysis_request v1`、`lifecycle_event v2`、唯一 configuration 和 `run_snapshot v1` 贯穿新写与读取；不存在里程碑版本 union 或只读旧形态；其余 M4 content 的归属和 reference 正确；
- parser input/message/context 可完整读取，大正文沿 M3 reference；
- context 后 topology 失败时 context 仍可读；
- history projection 来自 context，损坏 item 进入 issues；
- session 流按 cursor 补齐；run snapshot 只含轻量 reference，重连可恢复 topology ready；
- 取消、启动遗留修复、持久化补写、live 清理继续满足 M3 invariant。

### 16.5 集成边界

`scripts/m4_workflow_smoke.py` 使用固定 fake model/geocoder/NLDI/WDFN/Samples 组合真实 application pipeline，覆盖 frontend target/date、query fallback、mixed、分段部分 qualification、无 source、技术失败、幂等与持久化内容 reload。`scripts/audit_parameter_capabilities.py` 默认离线审计 154 项；显式 `--refresh-official` 才读取 USGS OGC v1，并且只报告 drift、不修改 manifest。其余 adapter/parser contract smoke 继续受凭据和网络条件控制，记录真实 API version、请求量和失败，不把 `not run` 写成通过。

M4 内部测试只证明本切片契约和接缝，不产生“M4 中间态”产品；M9 完成后才开始正式前后端联调并统一验收，包括 M5 实际 evidence、M6 reasoning、M7 调度、M8 memory 和 M9 前端/设置。

## 17. 后续阶段接力

M5 直接消费 `AnalysisContext`、`HydrologyGraph`、station refs 和 capability registry，获取真实 evidence 并区分 `available / empty / unavailable / invalid`。M5 必须重新验证 inlet，不得信任 metadata 就调用模型。

M6 按“参数 × 时间段”执行 sufficiency gate：直接证据或 registry 允许的侧面证据才能进入水体模型；完全无证据的单元生成确定性 `indeterminate`，不消耗推理 token。M7 只对 active DAG 中真正准备执行的水体创建 session。M8 消费已固化的 `memory_mode`，不要求 M4 兼容临时假行为。
