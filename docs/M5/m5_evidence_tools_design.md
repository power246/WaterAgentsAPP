# M5 Evidence Tools + EvidenceBundle 设计决策记录

更新日期：2026-10-03

> 状态：设计方向与实施模式已完成对齐，当前唯一 V1 已完成 M5 Milestone 0–13 本机实现与验收。本文记录已确认的业务与架构决策；精确类型、provider、policy、API、验收证据和未运行边界见同目录实施设计、Policy Catalog、API 文档与 [Milestone 0–13 Implementation TODO](m5_implementation_todo.md)。

> M7 接力说明：本文 2026-10-03 同步的 dependency manifest、stationless 零模型截断及本地/上游组合规则是最终产品目标，须由 M7 接入实现；不属于既有 M5 Milestone 0–13 的完成证据。

本文承接：

- [后端架构骨架](../backend_architecture_skeleton.md)
- [总 Milestones 的 M5 范围](../backend_refactor_milestones.md#7-m5-evidence-tools--evidencebundle)
- [跨 Milestone M5 契约](../deferred_milestone_contracts.md#7-m5evidence-tools--evidencebundle)
- [M4 设计决策](../M4/m4_query_understanding_topology_design.md)
- [M4 实施设计的后续阶段接力](../M4/m4_query_understanding_topology_implementation_design.md#17-后续阶段接力)
- [M4 Implementation TODO 的 M5 接力出口](../M4/m4_implementation_todo.md#m5-接力出口)
- [M5 实施设计](m5_evidence_tools_implementation_design.md)
- [M5 Evidence Policy Catalog](m5_evidence_policy_catalog.md)
- [M5 API / 读取契约](m5_evidence_tools_api.md)
- [M5 Milestone 0–13 Implementation TODO](m5_implementation_todo.md)

## 1. 当前边界

M5 负责把外部和内部来源的原始记录转换为可追踪、可比较、可被后续推理消费的 evidence。adapter 只返回强类型 source record；Evidence Tool 负责 collection status、质量判断、单位换算、聚合、provenance、evidence relation 和 `EvidenceFragment` 构造。

M5 直接消费 M4 已保存的唯一 `AnalysisContext`、唯一 `HydrologyGraph`、station/series references 和唯一 parameter capability registry。M4 的 availability qualification 只是候选资格，不是 observation，也不能直接成为 `EvidenceFragment`。M5 必须重新读取并验证真实记录的时间覆盖、质量、单位和完整性。

M5 不负责最终水质判断、模型 confidence、归因或多 Agent DAG 调度。M6 负责单水体 reasoning 和模型调用前的确定性 sufficiency gate；M7 负责按 active DAG 创建并调度水体 sessions；M8 才接入真实跨 run transcript RAG producer。

## 2. 已确认决策

### D01：Evidence 作为 Agent tools 按需获取，不在 topology 后全图预采集

M5 建立统一的 Evidence Tool 层。Evidence Tool 是后续水体 Agent 可调用的能力，不是在 M4 topology 完成后立即遍历整张 active graph 的 run-wide 预采集阶段。

最终系统中的调用关系为：

```text
M4 AnalysisContext + HydrologyGraph
  -> M6/M7 为真正准备执行的水体建立 Agent 执行上下文
  -> station-backed 水体由确定性 policy 先调用 M5 本地 observation/discharge preflight
  -> M7 注入已收敛的直接上游结果
  -> Agent 根据证据缺口按需调用 M5 optional evidence tools
  -> M5 返回并记录结构化 EvidenceFragment / EvidenceBundle
  -> M6 deterministic sufficiency gate 决定哪些参数 × 时间窗口可进入水体模型
```

因此：

- M5 不因 graph 中存在 active node 就提前发出该节点的全部 provider 请求；
- 未准备执行、被取消或最终未调度的水体不会仅因 topology 存在而产生 evidence 调用；
- conditional-mandatory、mandatory evidence requirement 与 optional 共用 tool registry/executor，不建立两套执行框架；
- 每次真实 tool 调用沿用 M3 的 run/session/operation identity、统一 transcript writer、脱敏和大正文读取能力；
- M5 的独立阶段验收通过 fake adapter、provider contract test 和最小 tool execution harness 验证，不在 production path 中创建隐藏水体 Agent、预建水体 session 或用 fake producer 冒充后续能力；
- M6 负责接入单水体 Agent，M7 再将同一 tool 层扩展到 DAG 中实际准备执行的多个水体。

不采用 topology 后全图预采集，原因是该模式会在水体 session 和真实执行需求尚未成立时提前放大外部请求，同时造成 evidence 的 session/operation 归属含糊，并与后续 Agent 的 mandatory/optional tool 调用重复。

### D02：Provider 按时间段批量获取，EvidenceBundle 按最终时间窗口组织

Evidence 的外部获取粒度与推理消费粒度分开：

- `TimeSegment`（`gap_filling` / `prediction`）决定 provider 路由、查询范围和历史/预测语义；
- provider adapter 可按完整 segment 或合并后的连续时间范围批量读取记录，避免为每个窗口单独发起远端请求；
- M5 对批量记录执行标准化、去重、质量分类、确定性单位换算和确定性聚合；
- EvidenceBundle 按 `build_time_windows()` 生成的最终 `6h / 12h / 24h` 窗口组织 evidence；
- M6 的 sufficiency gate 按“目标 COMID × 目标 ParameterKey × 最终时间窗口”读取 M5 的确定性分类结果，而不是因为一个 segment 中曾出现过一条记录就认定整段充分。

每个目标参数窗口必须能够分别表达：

- direct evidence references；
- capability registry 明确允许的 indirect evidence references 及 relation identity；
- 同一窗口必需 discharge 的 direct/allowed-indirect evidence references；
- `empty`、`unavailable`、`invalid` 和质量不足事实；
- 从 raw source records 到 normalized/aggregated evidence 的 provenance。

`available / empty / unavailable / invalid` 继续保持不同语义。`empty` 可以参与“完全无证据”的确定性判断；provider unavailable、timeout、协议损坏或 invalid record 不能伪装成 empty，也不能因为同一 segment 的其他窗口有记录而被掩盖。

该决策不要求 provider 每次只返回一个 segment，也不要求 transcript 为每个窗口复制整份 raw payload。实际请求可以在不改变语义的前提下合并；窗口级索引、引用和分类必须保持可重现，完整原始内容继续只保存一份并通过引用读取。

### D03：第一版使用官方单一来源，失败不静默切换 provider

第一版为每类外部 evidence 选择一个权威来源，保持来源、单位、质量语义和失败行为可预测：

| Evidence 类别 | 第一版来源 | 角色 |
|---|---|---|
| COMID、location、topology | M4 已保存的 `AnalysisContext` 与 `HydrologyGraph` 原事实 | mandatory；内部事实封装，不再次调用外部 topology provider。 |
| 水文/水质连续与日值观测 | USGS Water Data OGC API v1 的 continuous/daily 数据 | 当前水体存在合格本地 station/series ref 时 conditional-mandatory；实际通道由 capability 和 M4 reference 决定。 |
| 离散水质样品 | USGS Samples Data API | conditional-mandatory water observation 的离散样品通道；不得伪装成连续序列。 |
| discharge/flow | USGS Water Data OGC API v1 与 capability 允许的 USGS observation 通道 | 每个水体/窗口都必须查询或推理的 required support；有本地站时先查询，无直接读数时只走登记的推理关系。 |
| 历史天气观测 | National Weather Service API | optional；由 Agent 根据任务、证据缺口和允许关系按需调用。 |
| 未来天气 | National Weather Service API | optional；由 Agent 根据任务、证据缺口和允许关系按需调用。 |
| 设施/排放相关事实 | EPA ECHO web services | optional facility evidence。 |
| 当前 run 上游结果 | M7 接入的 upstream-result producer | M5 定型工具槽位与 unavailable 语义，不在本阶段伪造 producer。 |
| 跨 run 历史证据 | M8 接入的 transcript-RAG producer | 仅 `memory_augmented` 时适用；M5 不在本阶段伪造 producer。 |

第一版不在某个 provider timeout、限流、认证失败、协议损坏或返回 invalid record 时自动切换到另一个商业或公共 provider。上述情况必须保留为 `unavailable` 或 `invalid` 事实，并由后续确定性策略决定当前执行是否失败；不能换源后把 provenance 写成正常首选来源，也不能把失败改写成 `empty`。

“单一来源”是第一版执行策略，不要求把 port 或 capability 写死成永远只有一个 provider。以后增加来源时，必须显式更新 capability、优先级/冲突规则、单位与质量映射、provenance、测试和用户可见说明；不得只在 adapter 内增加隐式 fallback。

M4 的 station/series reference 可以帮助 M5 选择 USGS 通道和缩小查询，但 M5 仍以 observation endpoint 的真实返回为准。内部 topology evidence 引用 M4 原 item，不复制或重算 graph；weather/facility evidence 必须使用本次 tool 调用取得的 source record，不能从 M4 metadata 推断。

### D04：Evidence 模型按 M1–M9 原子更新，当前形态始终是唯一 V1

M1–M9 是同一套系统的原子演进过程，不存在可运行、可联调或需要兼容的 Milestone 中间态。M5 对 M1 `EvidenceFragment`、`EvidenceBundle` 和相关 source port 的收敛直接替换当前内部模型；不新增 `EvidenceFragmentV2`、`EvidenceBundleV2`、旧/新 read union、兼容 wrapper、迁移 adapter 或 milestone feature gate。不管字段如何调整，当前实现、文档、fixture 和测试共同表示唯一 V1。

M5 采用以下单一数据流：

```text
provider wire response
  -> adapter 私有解析模型
  -> provider-neutral SourceRecord
  -> Evidence Tool
  -> 唯一 EvidenceFragment
  -> EvidenceBundle 中的窗口级 EvidenceCell 只保存 evidence references
```

具体约束：

- provider wire model 与 SDK/HTTP 字段留在对应 infrastructure adapter 内，不进入 port、domain、transcript 或 reasoning；
- observation、weather、forecast、facility、upstream result 和 historical inference 分别使用强类型 Request/Record DTO，port 不返回裸 `dict`、`Any` 或 provider SDK 对象；
- `EvidenceFragment.payload` 是带稳定 `payload_type` discriminator 的强类型 evidence payload union，不存在宽松 JSON payload 或并行读取分支；
- 每个 `EvidenceFragment` 保存为唯一 typed transcript item；`EvidenceBundle.fragment_refs` 保存去重后的原 item references，不在 bundle 中复制 fragment payload；
- 窗口级 `EvidenceCell` 使用“agent COMID × 目标 ParameterKey × 最终 TimeWindow”身份，并分别引用 direct evidence、capability 允许的 indirect evidence、必需 discharge evidence 和 collection/quality blockers；
- indirect reference 必须同时保存 capability registry 中的 relation identity，不能只保存一个无语义 evidence ID；
- bundle 校验 fragment ID 唯一、cell identity 唯一、所有引用闭包、window plan 完整、目标参数完整以及 required-support discharge 角色完整；
- source record、fragment、cell 和 bundle 都使用 frozen/strict 模型，未知字段拒绝；Evidence Tool 是 source record 到 fragment/cell 的唯一业务映射层。

实施时必须在同一个 M5 原子变更中同步 Domain、ports、tool registry、transcript registry、runtime composition、fixtures、文档和测试。开发数据按当前 HEAD 迁移或重建，不为尚未正式发布的内部旧 Evidence 形态增加读取兼容。

### D05：Tool execution 与 collection status 分层，必需路径失败阻断、optional 失败条件降级

M5 不用一个 `status` 同时表达工具是否正常执行和数据是否存在。两层状态分别负责：

```text
EvidenceToolExecutionStatus
  completed | failed | cancelled

CollectionStatus
  available | empty | unavailable | invalid
```

`EvidenceToolExecutionStatus` 描述本次 operation 是否按工具契约完成；`CollectionStatus` 描述某个来源、参数和时间范围内 evidence 的采集事实。二者不能互相推导或压缩成一个布尔值。

规则如下：

- adapter 成功完成查询且没有匹配记录时，tool execution 是 `completed`，对应 collection status 是 `empty`；这才可以参与“完全无证据”的确定性判断；
- provider timeout、限流、认证失败、服务不可用或完整响应不符合协议/schema 时，tool execution 是 `failed`，并保存安全错误与 `unavailable` 事实；不得返回 `empty`；
- provider 响应整体合法但其中某条观测缺值、质量明确无效、单位不兼容或不能形成有效观测时，保留该 source record 和 `invalid` fragment；不能把单条无效记录升级成整个响应协议失败，也不能把它用作 observed quantified evidence；
- 同批记录部分有效、部分无效或只覆盖部分窗口时，tool execution 可以是 `completed`；有效记录只覆盖实际命中的窗口，其余窗口保留 `invalid`、`empty` 或 `partial_coverage` 事实，不能互相掩盖；
- cancellation 独立于 provider failure。取消胜出后不启动新 provider 请求、聚合或下游 tool；在途返回遵守 M3/M7 的晚到结果边界，不能继续进入 bundle 或模型输入。

conditional-mandatory preflight 或 discharge 必需取证路径的 `failed` 会阻断受影响的水体/参数/窗口推理，并保证该范围不调用水体模型。M5 输出明确 blocker 和安全错误；该 blocker 如何收敛为单 Agent session 或多 Agent run 的最终状态，由 M6/M7 在不弱化本规则的前提下定型。

optional evidence tool 的 `failed` 仍保存为 `unavailable`，但只要 target + discharge gate 已由其他 direct/allowed-indirect evidence 满足，可以继续当前推理；最终 evidence chain 必须保留该补充来源不可用的事实。若该 optional tool 是某个 allowed-indirect relation 成立所必需的唯一来源，则对应窗口不能通过 gate，不能把调用失败解释成“没有侧面证据”。

Port 层继续以类型化 exception 表达 timeout、rate limit、authentication、unavailable 和 invalid response；Evidence Tool 捕获预期 port failure，将其转换为统一、可序列化、可写 transcript 的 tool failure result。未分类异常在 application 边界转换为安全内部失败，原异常正文、provider body、URL secret 和 traceback 不进入公开 tool result。

### D06：单位与聚合采用三层注册，不为每个观测值维护规则

M5 必须提供确定性单位换算及聚合能力，但不为每个 observation value 建立独立规则，也不维护任意单位两两换算的 N×N 表。规则分成三层：

1. parameter capability 为每个 `ParameterKey` 固定 canonical unit、measurement semantics 和 aggregation policy identity；
2. 共享 unit registry 维护内部 unit ID、量纲、provider aliases 和到规范基准的确定性转换；
3. 可复用 aggregation policy 维护适用 value semantics、默认方法、允许方法和覆盖要求，由多个 capability 显式引用。

建议的逻辑结构为：

```text
ParameterCapability
  canonical_unit_id
  measurement_semantics
  aggregation_policy_ids

UnitDefinition
  unit_id
  dimension
  aliases
  conversion_rule

AggregationPolicy
  policy_id
  accepted_value_semantics
  default_method
  allowed_methods
  coverage_rule
```

同一单位字符串不足以决定聚合方式。normalized SourceRecord 必须同时保留 value semantics，例如 `instantaneous`、`interval_total`、`cumulative_counter`、`daily_statistic` 或 `discrete_sample`；相同的 `mm` 或 `in` 在区间降水量、累计计数器和水位中具有不同聚合规则。provider statistic/method、采样 fraction 和介质语义不能在单位标准化时丢失。

单位处理规则：

- SourceRecord 同时保留原值/原单位，以及成功换算后的标准值/canonical unit；
- provider 的 `deg C`、`degC`、`°C` 等字符串先通过显式 alias 映射到同一内部 unit ID；
- 线性、仿射或其他允许的转换只通过已登记的确定性算法执行；不得由 Agent 或 LLM 生成公式；
- pH 等对数尺度、method-specific unit、样品 fraction 或需要额外上下文的转换作为独立 measurement semantics 处理，不能套用普通线性换算；pH 的 mean/min/max 等只在 `continuous_ph` capability policy 明确登记时允许，不能由“无量纲”自动推导；
- 未知单位、量纲不兼容、缺少必要上下文或 conversion rule 不存在时，保留原记录并标记 `unit_unresolved/invalid`，不能猜测或只改单位标签；
- 每次换算记录 conversion rule identity/version、原值/原单位、标准值/canonical unit 和来源 record identity，形成完整 provenance。

聚合处理规则：

- capability 只允许引用已登记、适用于其 measurement/value semantics 的 aggregation policy；
- Agent 可以在 policy 的 `allowed_methods` 内选择；省略时使用 `default_method`，越权方法在执行前被拒绝；
- 首期方法仍限制为 `mean / median / min / max / sum / p90 / latest`，但并非每个参数都允许全部方法；
- 先完成单位校验与标准化，再对同一窗口内的兼容记录聚合；不得混合不同量纲、介质、fraction 或不可比较 method；
- raw observation fragment 原样保留；聚合产生新的 derived fragment，引用全部输入 evidence IDs，并记录 policy/method、覆盖情况和算法版本；
- M5 aggregation 不执行插值、gap filling、预测或跨无数据窗口补值；这些不能伪装成普通聚合。

现有 M4 唯一 capability manifest 继续是参数执行权威；M5 在同一 manifest/model 上原子增加 measurement 与 aggregation fields，不建立第二份 `ParameterKey -> unit/method` 表。共享 unit/aggregation definitions 可以被 154 项 capability 重用，但 manifest 加载后必须逐项解析为完整、无悬空引用的可执行策略。

### D07：SourceRecord 与 Fragment 各保存一次，tool result 和 bundle 只引用原事实

M5 沿用 M3 transcript 作为唯一事实源，但不把 provider 的完整 HTTP envelope、headers 或原始 response body 直接保存为业务事实。adapter 输出的完整、强类型、provider-neutral SourceRecord 是系统保留的来源事实；它必须保留审计所需的原值、原单位、观测/有效时间、provider quality/statistic/method、provider record identity、location 和不含凭据的 source URI。

M5 使用以下 typed transcript 内容：

```text
evidence_tool_call v1
  -> 本次安全、强类型 tool request

evidence_source_records v1
  -> 本次 operation 返回的唯一标准化 SourceRecord 集合

evidence_fragment v1
  -> 单份唯一 EvidenceFragment，引用其 source-record item/record identities

evidence_tool_result v1
  -> execution status、safe error、source-record refs、fragment refs

evidence_bundle v1
  -> window cells、tool-result refs、fragment refs；不复制 fragment/source payload
```

约束如下：

- `evidence_source_records` 保存 adapter 已验证的标准化记录，不保存 SDK object、HTTP headers、API key、Authorization、未脱敏 URL query 或完整 provider envelope；
- 每个 SourceRecord 只在所属 source-record item 中出现一次；Fragment 通过 item reference + source record identity 建立 provenance，不内嵌第二份 source record；
- 每个 EvidenceFragment 只保存一次；tool result、bundle、EvidenceCell 和后续 EvidenceChain 都引用该 fragment 原 item/evidence identity；
- tool result 只有在其引用的 source-record/fragment item 已存在后才能声明 `completed`，不能先发布悬空引用；
- bundle 的持久化形态是 reference-only assembly；M6 通过 M3 统一 reader 解析完整引用闭包，不建立第二套 evidence repository 或旁路加载器；
- bundle validator 校验 tool-result、fragment、source-record 引用都属于同一 run，适用时属于当前 waterbody session，并验证 evidence ID、source record identity、window cell 与 required-support closure；
- 超过 M3 正文阈值的 source series 或 fragment payload 继续由现有大正文机制保存一次，timeline/SSE 只返回安全 preview/reference；内容大小不改变业务 schema；
- provider 原始响应只允许作为不含真实 secret 的 adapter contract fixture，或进入受控且脱敏的临时诊断；它不成为 transcript、普通日志、SSE 或最终 bundle 的事实源。

这一持久化模型直接替换 M1 `EvidenceBundle.fragments` 的内嵌形态，仍属于当前唯一 V1；不保留 inline-fragment 兼容读取，也不增加 Evidence V2。

### D08：质量策略按 capability/source channel 定义，suspicious 可参与聚合

M5 不使用一个全局采样数或缺失率阈值判断全部参数。quality policy 由 capability、source channel、measurement semantics 和 value semantics 共同选择，并在确定性代码中执行。provider 原始 approval、qualifier、quality flag、method、statistic、medium 和 fraction 必须保留，随后再映射为项目领域质量事实。

观测质量分为三个决策层级：

```text
valid
  -> 可作为直接 evidence，并可参与允许的聚合

suspicious
  -> 保留原值和原因；可以参与允许的聚合，但必须传播可疑事实

invalid
  -> 保留用于审计；不得参与 observed/quantified aggregation
```

`suspicious` 与 `invalid` 不得合并。suspicious 输入参与聚合时：

- 聚合结果必须引用全部 valid/suspicious 输入，不能静默删除可疑点；
- derived fragment 继承适用的 `provider_flagged`、`anomalous`、`possible_freezing`、`estimated`、`stale` 或其他已登记质量标志；
- 记录 valid/suspicious/invalid/missing 输入数量、实际覆盖率及可疑输入占比；
- 按 quality policy 产生 deterministic `quality_class`、flags 与 coverage，不在 M5 生成另一套 `confidence_hint` 或 M6 最终 confidence；
- 如果 suspicious 比例或关键覆盖条件超出 policy 上限，聚合结果可以转为 `invalid` 或不足，而不是删除输入后伪装为高质量 available。

窗口覆盖规则：

- continuous series 根据已知 cadence、窗口边界、有效/可疑点数、最大连续缺口和 policy 阈值判断 `complete / partial / insufficient / unknown`；
- cadence 未知时不能假定完整覆盖，必须标记 `unknown` 或 `partial`；
- discrete sample 默认只作为样品实际落入窗口的直接 evidence，不代表整个连续窗口均有观测；
- 离散样品或相邻时间记录若要支持其他窗口，必须命中 capability 已登记的 `same_location_adjacent_time` 等 indirect relation，并保留 relation identity；
- daily statistic、interval total 和 cumulative counter 按各自 value semantics 分窗，不能按瞬时点规则处理；
- 窗口边界继续使用 UTC instant 的半开区间，并保留 analysis timezone/DST 语义，不用固定毫秒数重建当地窗口。

M5 不执行插值、缺口补全、异常值替换、winsorization、平滑或预测，也不因 provider flag 自动删除原记录。任何自动范围检查或异常检测必须来自已登记、可测试的 quality policy；未知规则只保留 provider 事实，不由 Agent 发明阈值。

quality assessment 与 `CollectionStatus` 分工不同：`available` collection 可以包含 valid 或 suspicious 记录；全部记录明确无效时保留 `invalid`；成功无记录仍是 `empty`；provider/协议失败仍是 `unavailable`。

### D09：M5 首版仅提供 run-local exact-request deduplication / single-flight

同一 analysis run 内，相同 canonical request key 只允许一个 in-flight provider operation；并发调用等待该 operation，后续命中复用已生成的 source-record 与 fragment 引用。deduplication 不合并 transcript tool calls：每次调用仍各自持久化，并在 result 中记录实际获取或 `deduplicated`；命中结果不得复制事实正文。

只有下列字段都一致时才视为同一个请求：

- provider、route/endpoint identity 和 API contract version；
- 查询位置、station/site identity、参数集合及其规范化顺序；
- 精确 UTC 起止时间、窗口语义、分页/查询选项；
- capability registry、unit/quality/aggregation policy 与 normalization schema version；
- 其他会改变返回事实或解释方式的 provider-specific 安全选项。

若这些字段任一不同，就必须作为新请求执行。请求键由确定性 canonical serialization 生成，不包含 API key、Authorization 或其他 secret。

M5 首版不提供跨 analysis run 的 observation/forecast cache，不把历史 transcript 当作透明缓存。新 run 必须重新获取；provider 失败时禁止使用上一 run 的数据作为 stale-on-error fallback，应按 D05 返回 `unavailable`，由后续 sufficiency gate 决定是否继续。

有界重试、退避和 provider 限流属于调用可靠性策略，不属于缓存。一次实际 provider operation 内的分页与有界重试仍归属于同一 tool call；只有 operation 成功形成受验证的 source records 后，结果才可被本次运行内的相同请求复用。失败结果可以在短暂的同一 in-flight operation 中共享，但不得在本次运行后续阶段被永久记忆成“已经没有数据”。

未来若引入跨 run cache，必须原子定义 evidence class TTL、新鲜度投影、原始 `retrieved_at`、cache provenance、失效规则和 transcript 引用边界；当前 M5 契约不预留跨 run cache 行为。

### D10：Agent-facing roster 按 evidence purpose 划分，provider 路由不对 Agent 暴露

M5 不提供单一万能 evidence tool，也不按 USGS/NWS/EPA 等 provider 建立 Agent-facing tool。稳定 roster 按业务用途与确定性变换职责划分：

| Tool | 职责 | 初始 policy |
|---|---|---|
| `collect_water_observations` | 获取水质、流量的 continuous/daily/discrete source records，并构造对应 evidence | 当前水体存在合格本地 station/series refs 时 conditional-mandatory；实际 route 由 ParameterCapability 决定 |
| `collect_weather_evidence` | 获取历史天气或预报 source records，并构造允许的环境 evidence | optional；由 Agent 根据任务、证据缺口和 capability relation 决定 |
| `collect_facility_evidence` | 获取目标范围内的 facility context | optional |
| `get_upstream_results` | 读取同一 run 中已完成且允许传递的上游推理结果 | M5 定型 schema/slot；M7 接入 producer 后进入可用 roster |
| `search_transcript_evidence` | 按固化的 memory mode 检索允许的历史 transcript evidence | M5 定型 schema/slot；M8 接入 producer 后进入可用 roster |
| `convert_evidence_unit` | 对已存在 evidence/source-record refs 执行登记且量纲兼容的确定性换算 | deterministic transformation tool |
| `aggregate_evidence` | 对已存在 evidence refs 执行 capability policy 允许的确定性聚合 | deterministic transformation tool |
| `inspect_evidence_records` | 对已保存 source records 生成有界、稳定分页的只读投影 | deterministic inspection tool；不访问 provider |

`flow` 使用独立 SourceRecord DTO/semantics，但在 Agent-facing roster 中仍由 `collect_water_observations` 获取；`weather observation` 与 `forecast` 保持不同 SourceRecord DTO/channel，由 `collect_weather_evidence` 的判别式 request 明确选择，不在返回后猜测。

source tool request 只表达业务目标与稳定身份，例如 run/session/context reference、ParameterKey、window IDs 和所需 evidence channel；适用 COMID、location 与可用 station refs 必须来自受信执行上下文，不允许 Agent 任意指定。request 不得接受 provider 名称、任意 endpoint、原始 URL、API key、provider query string 或未登记单位/聚合表达式。tool service 根据 capability registry、provider policy 和 composition-root 配置选择唯一 route，并在 provenance 中记录实际 provider/endpoint contract identity。

mandatory/optional 仅是 registry/prompt policy metadata，不产生两套 tool protocol、base class、执行器或 transcript schema。每次真实模型请求的 tool roster 必须由当前阶段已接通的 producer 与 policy 装配；M5 fake/contract harness 可以验证延后 slot，但 production roster 不得注册隐藏 fake producer，也不得把恒定占位结果冒充真实能力。

所有 tool request/result 使用严格判别式 schema，拒绝额外字段。collection、conversion 与 aggregation 各自保留 operation identity 和 transcript call/result；conversion/aggregation 只能引用已有事实，不覆盖 source fragment，也不能把 inferred/derived 值重新标成 observed。

### D11：每个水体 Agent 只主动收集本水体 evidence；M5 补齐空间形状与有界查询

水体 session 的 `target_comid` 是 collection tool 的强制空间主体。M5 source tools 只能主动读取该 COMID 自身关联的 station/site、天气和设施数据；Agent request 不接收其他 COMID、任意 station ID、任意中心点或任意 geometry。`collect_water_observations` 只能使用当前 HydrologyNode 已关联且通过 provider/capability 校验的本地 station refs。

上游水体的观测和已确认站点不得由下游 Agent 越权重新查询。M7 接入后，下游只能通过 `get_upstream_results` 读取同一 run、DAG 直接依赖已完成 session 的允许结果与 evidence closure；更远上游信息只能作为该直接上游结果已经引用并传递的 provenance 出现。该边界必须由 application authorization 校验，不能只依赖 prompt。

所有 source tool 的时间请求必须落入同一个 hard support envelope：以 `AnalysisContext` 的 analysis timezone 计算，从请求日期范围前一当地自然日的 00:00，到后一当地自然日结束后的半开边界。该 envelope 在 DST 日可不是固定 24 小时倍数；转换为 UTC 后执行。Agent 不得扩大 envelope，capability 的 adjacent-time relation、provider pagination、重试或批次拆分也不得越界。

M5 负责在现有唯一模型中新增并实现水体空间形状。NLDI adapter 当前已解析但丢弃的 LineString 必须进入受验证的 domain 模型，并沿 graph builder、`HydrologyNode`、`HydrologyGraph`、`topology_ready v1` registry/serializer、持久化读取、fixtures 和消费者原子贯通。该工作属于 M5 实现范围，不回填为 M4 实施任务，也不新增 schema V2、兼容 union 或 milestone feature gate。

每个 HydrologyNode 的空间事实至少包含：

- WGS84/EPSG:4326 LineString，固定 `[longitude, latitude]` 顺序；
- 确定性计算的 segment length、bounding box 和 representative point；
- 原始 coordinate count、geometry source/provenance 与适用 COMID；
- 完整 geometry 的原事实引用，以及按固定算法/误差与点数上限生成的 prompt-safe simplified polyline。

完整 geometry 在 run 原事实中只保存一次，并继续使用 M3 大正文/reference 机制；水体 session 输入引用该事实，只内联有界 spatial summary 与 simplified polyline。简化形状不得替代精确 geometry 执行空间过滤、距离计算或审计。

`collect_facility_evidence` 允许 Agent 提交 `radius_miles`，但不允许提交中心点或替代 geometry。第一版闭区间为 `[1,20]` mile，越界拒绝且不得静默 clamp。工具语义是对当前水体完整 LineString 建立 Agent 所选精确半径的 corridor；ECHO point-radius 查询使用不小于该值的最小官方支持半径 `{1,2,3,4,5,10,20}` mile 和有界采样圆覆盖整段，再用完整 LineString 距离按 Agent 原半径最终过滤。所选半径需要超过采样点预算时在发请求前返回确定性最小可行半径，不得截断河段。result/provenance 必须记录 Agent 半径、provider 半径、采样中心、去重结果和每个 facility 到河段的确定性距离。

天气等其他需要坐标的工具同样只能消费当前水体受信 geometry，并由登记的 location policy 生成查询点；Agent 不能用自由坐标把本地 evidence tool 改成跨水体搜索。所有空间查询继续受 tool-call、provider request、采样点、分页、记录数、并发和 deadline 硬预算约束。

### D12：完整记录只保存一次，Agent 使用有界摘要、确定性聚合与按需检查

collection tool 不得将无界 source-record array 直接返回给 Agent/model。adapter 产生的完整标准化 SourceRecord 集合先按 D07 保存为唯一 `evidence_source_records v1` 原事实；tool result 返回有界 collection manifest、事实/派生 fragment refs、source-record item ref 和可检查范围。

collection manifest 至少提供 status、channel/value semantics、请求与实际时间覆盖、记录总数、valid/suspicious/invalid/missing 计数、单位集合、cadence/最大缺口、分页完整性和相关 evidence refs。任何会参与推理的数值摘要，例如 latest/min/max 或 capability 默认聚合，不得作为未追踪的展示字段出现；它必须由登记的确定性 policy 计算并保存为引用输入记录的 derived EvidenceFragment。对不允许该聚合或非数值 semantics 的 capability 不生成对应摘要。

`aggregate_evidence` 接收当前 session 可访问的 evidence/source-record refs、window IDs 和允许的 aggregation policy/method identity。工具负责选择输入、单位标准化、质量传播、覆盖计算和派生 fragment 持久化；Agent 不接收无界数组后自行计算均值、分位数或累计值。重复聚合命中 D09 的 run-local deduplication，仍保留独立 tool call/result。

`inspect_evidence_records` 只读取已经持久化且位于当前授权 evidence closure 内的 SourceRecord，不调用 provider、不创建新的 source fact，也不修改 quality。request 只允许 source-record item/ref、UTC 子区间、稳定 cursor 和有界 page size；不接受任意 SQL/filter expression、provider URL、跨 run identity 或范围外时间。

inspection 使用稳定的 canonical record order 和 opaque cursor。result 返回本页 record identities、必要的观测时间、原值/单位、canonical value/unit、value semantics、quality flags 与 provenance refs，并包含总范围/是否还有下一页；单页 record/byte/token 上限由后端 policy 强制执行，不能由 Agent 提高。检查下一页必须显式再次调用工具。

inspection tool result 的持久化形态保持 reference + projection specification + record identities + content hash；Agent context materializer 可从不可变 source-record 原事实构造本次有界可见投影，不将同一 SourceRecord 保存为第二份业务事实。若 inspected record 影响最终推理，结构化决策与结果必须引用其原 source-record/fragment identity，不能只引用 inspection 页码或模型文本。

公开 transcript/SSE 沿用相同有界 manifest/preview/reference，不传输完整 series。全文读取只从唯一 source-record 原事实按权限与 M3 大正文机制取得；preview 截断、模型上下文裁剪或 inspection 分页都不得改变 EvidenceBundle 引用闭包。

### D13：事实来源与对目标水体的证明力分开；contextual evidence 不解锁门禁

每个 EvidenceFragment 除 typed payload、quality 和 provenance 外，还必须具有受验证的 applicability：

```text
direct_measurement
  -> 当前水体、目标 ParameterKey、适用时间/介质/方法的直接观测

allowed_indirect
  -> 当前 append-only EvidenceRulePlan 中的动态 rule identity，且其全部前置证据、时空关系与引用闭包成立

contextual_only
  -> 可用于解释或提出假设，但不能满足目标参数或 required discharge 的 sufficiency cell
```

Agent、prompt、tool caller 和 provider adapter 都不能直接提升 applicability。问题解析 Agent 负责基线规则，水体 Agent 只能追加规则；通用 materializer 是唯一可以据此生成新 `allowed_indirect` fragment 的组件。EvidenceCell 只把 `direct_measurement` 或命中当前 plan 的 `allowed_indirect` 计入 gate；`contextual_only` refs 可以随 bundle 提供给后续归因，但不得把 `insufficient` 改为 sufficient。

同一 series 的本地 direct records 经过允许的确定性聚合后，derived fragment 可以继续以 `direct_measurement` applicability 进入 gate，因为其依据仍是当前水体直接观测；但它不再是一条原始读数。M6 最终结果只要引用多条 records 或 aggregate，就必须标 `inferred`，不能利用 applicability 名称绕过 D14 的一对一 observed 规则。

facility 事实默认是 `contextual_only`。设施存在、permit/compliance 状态、行业类别或与 LineString 的空间距离只证明该事实本身，不证明设施与当前河段水文连通、向当前窗口排放或导致目标参数变化。仅凭 proximity 的 attribution basis 上限是 `hypothesized`，不得输出确定因果措辞。

未来若 provider 返回真实 facility discharge/effluent measurement，该记录可以是“设施处的 observed source fact”，但它对目标水体的影响仍不是 `direct_measurement`。只有当前 `EvidenceRulePlan` 明确参数对应、时间关系、水文关系和最大结果强度，且 materializer 验证输入闭包后，才能另行生成对应 `allowed_indirect`；否则仍为 contextual。不得因名称或单位相似自动关联。

天气观测/预报同样先保留其自身事实身份，不自动成为水质观测。例如问题解析 Agent 可声明 precipitation + discharge 对 turbidity 的同窗方向性支持；该规则必须同时引用适用窗口的 precipitation 与 discharge，并最多支持 directional/inferred 判断，不能直接生成目标参数 quantified observed value。当前 plan 没有声明或输入不完整的天气—参数关系保持 contextual。

上游 Agent result 先作为 contextual raw input 保存。只有当前 plan 存在相应 `upstream_to_downstream` 规则，且它引用直接上游 session 的原结果及完整 evidence closure 时，materializer 才生成 `allowed_indirect` relation support；它仍不是下游本地 observed。下游的加权、传播或综合值继续标记 inferred/predicted，不得因上游链包含观测而提升为下游 observed。

`search_transcript_evidence` 返回的历史推理必须保留 `historical_inference` 事实和原 run/session provenance。M5 slot 默认将其标为 contextual，不能替代本次 collection、required discharge 或 M6 gate；M8 若扩大用途，必须通过同一 append-only rule plan 明确关系、时效和优先级，并经过确定性物化，且当前 session 的合格实际观测始终优先。

现有 `AttributionBasis(observed/correlated/inferred/hypothesized)` 表达结论的归因依据，不替代 evidence applicability。M6 validator 必须交叉校验 result source、attribution basis、applicability、relation identity 和 evidence refs：目标水体 observed 只能来自适用且能够一对一追溯的本地具体直接观测；相关、聚合、综合、推断或假设不得通过措辞或 confidence 值伪装成 observed/causal。

### D14：M5 分离 observation series，不自动判冲突；只有具体直接读数可标 observed

同一 COMID/ParameterKey 下的记录必须按稳定 series identity 分组。identity 至少包含 provider/channel、station/site、ParameterKey、medium/fraction、method/statistic、value semantics 和其他会改变读数含义的 qualifier。不同 identity 的记录不得在 M5 collection 阶段静默合并、选优或跨组聚合。

同一 series 内可按 capability aggregation policy 对适用窗口执行确定性聚合；聚合必须产生引用全部输入记录的 derived EvidenceFragment。跨 station、method、medium/fraction、parameter code 或不兼容 statistic 的合并默认禁止。完全相同的 provider record identity 可以确定性去重，但 provenance 必须保留重复出现的 channel/page 信息；没有受 provider contract 保证的稳定 identity 时不得仅按数值和时间猜测重复。

M5/backend 不实现跨 series 的“明显冲突”检测阈值，不自动产生冲突结论、选择赢家、降权或特殊门禁状态。它只向 Agent 暴露区分充分的 series metadata、quality、窗口摘要与 inspection 能力。水体 Agent system prompt 必须要求：

- 发现读数存在明显差异时检查相关 station/time/method/quality；
- 保留并引用所有影响判断的 evidence，不静默删除相反记录；
- 说明选择某条记录、保持不确定或形成综合判断的理由；
- 无法合理解释或选择时输出受限判断或 indeterminate，而不是伪造一致值。

`ResultSource.OBSERVED` 只适用于最终点值能够一对一追溯到当前水体适用窗口内的一条具体 `direct_measurement` SourceRecord；允许仅做登记且可逆的确定性单位表示转换，同时保留原值/单位与 conversion provenance。下列结果一律使用 `ResultSource.INFERRED`：

- 即使方法获准，任何引用多条记录形成的聚合值，包括 mean/median/min/max/sum/p90；
- 跨 station/series 的选择、加权、折中、综合或冲突解释；
- 相邻时间、上游结果、天气/流量关系、设施或历史 transcript 支持的判断；
- 趋势、方向、风险或从观测推导出的其他非原始结论。

`latest` 只有在结果原样采用其唯一对应的具体 direct record 时可以保持 observed；若 latest selection 还包含跨 series 选择、冲突裁决或其他推理则仍是 inferred。validator 只校验 observed 的一对一引用/转换闭包及 inferred 的完整引用，不尝试替 Agent 判定数值是否“冲突明显”。

### D15：有站水体先验证真实观测，无站水体消费上游并按需取证；discharge 始终必需

M5/M6/M7 必须按当前 HydrologyNode 是否具有合格本地 station/series refs 区分 evidence acquisition 起点，不能为全部水体生成同一固定 mandatory tool list。

对 station-backed waterbody，在首次水体推理调用前，由该 session 的确定性 tool policy 执行 `collect_water_observations`：

- 只查询当前节点关联的 station/series refs 和其 qualified coverage units；
- 查询所有适用的用户目标 ParameterKey，并始终包含 required-support `usgs:00060` discharge 的本地可执行通道；
- M4 metadata 只决定候选与 conditional-mandatory plan，M5 actual response 决定真实 evidence；
- 本地 preflight 后仍须读取 M7 已收敛的直接上游 outcome；如果没有任何本地目标参数 direct evidence，且上游也没有与目标参数相关的 usable result，则即使本地只有 discharge 等 supporting evidence，也只生成逐 cell 的 empty/invalid/quality facts 并停止，不进入该水体的推理模型；
- provider unavailable/timeout/invalid protocol 仍按 D05 作为技术失败处理，不得冒充“完全没有证据”后自然停止；
- 只要至少一个目标参数存在 usable 本地 direct evidence，或 M7 注入至少一个与目标参数相关的 usable direct-upstream result，分支可以继续；本地 discharge 等 supporting evidence 可参与已登记关系，但不能单独充当目标参数起点。缺失参数/窗口保留 blocker，由后续允许的间接关系和逐 cell gate 处理，不要求整分支全有或全无。

对 stationless waterbody，不调用 `collect_water_observations` 制造注定为空的本地查询。其 mandatory evidence 起点来自同一 run 的直接上游 Agent results；至少一个直接上游具有 usable result 时，Agent 可根据任务和缺口按需调用天气、设施、inspection、aggregation 等 optional tools。全部直接上游均为 `indeterminate/insufficient_evidence` 或 invalid 时，M7 后端不启动 optional planning，而由 M6 确定性生成完整不足结果；contextual-only evidence 不能单独解锁水质判断。

discharge 是每个实际执行水体、每个最终时间窗口的 mandatory supporting parameter，不因为用户未显式请求或当前节点无监测站而省略：

- station-backed waterbody 必须先尝试取得本地 discharge direct records；
- stationless 或本地 discharge 缺失时，必须读取直接上游传递的 discharge result/evidence；只有当前 plan 的 discharge 规则输入完整时才能形成方向性 allowed-indirect support；
- 任一 discharge 聚合、传播、换算为估算流量或跨水体结果均标记 inferred，只有一对一采用当前水体具体 discharge direct record 才可 observed；
- 每个目标 ParameterKey/window 的 gate 同时要求该目标与 discharge 各自具有 direct 或 allowed-indirect evidence；最终仍无法得到 discharge 的窗口必须是 `indeterminate/insufficient_evidence`，不得生成该窗口的 quantified/directional 目标水质判断。

天气 observation/forecast 不因 prediction mode 自动执行；是否调用由当前任务、上游结果、证据缺口和 `EvidenceRulePlan` 决定。若 parser 基线规则把天气输入标为 `required_collection_attempt`，确定性 acquisition 必须先尝试该有界调用；Agent 后加的可选规则可由 Agent 触发。无论哪种来源，所需前置证据未完成时 relation 都不成立。

mandatory/conditional-mandatory/optional 仍由同一 registry、executor、transcript schema 和 error mapping 实现。确定性 tool policy 只负责 station-backed preflight 与直接上游结果注入；水体 Agent 负责 optional 调用。任何阶段都不得退化为 topology 完成后对整图预取。

### D16：无实际观测导致的分支停止是可审计业务结果，不是技术失败或假推理

station-backed waterbody 在准备执行时创建真实 waterbody session，随后执行 D15 的 conditional-mandatory observation tools 并读取 M7 direct-upstream outcomes。若成功查询后全部适用窗口均无 usable local target direct evidence，且全部直接上游也无与目标参数相关的 usable result，session 不调用水体模型；本地仅有 discharge 等 supporting evidence 不改变该结论，但真实过程不得删除、跳过或伪造。

该路径必须完成：

- 持久化实际 tool calls/results、source-record/fragment refs、empty/invalid/quality facts 和 reference-complete EvidenceBundle；
- 由 M6 确定性生成覆盖全部目标 ParameterKey × TimeWindow 的 `indeterminate/insufficient_evidence` 结果，waterbody model call count 保持 0；
- 使用当前 session/result 模型进入正常 completed 终态，并明确记录 inference model 未执行；
- 不新增 `branch_stopped` run/session lifecycle 状态，不创建 fake model message、fake confidence 或 placeholder quantified value。

M7 必须将每个直接上游节点的终态视为 DAG 依赖已收敛，包括正常有结果、正常全 indeterminate、failed 和 cancelled；汇流节点等待全部直接依赖收敛后再决定执行，不能因一条分支无 evidence 就忽略其存在或提前启动。

`get_upstream_results` 对正常无 evidence 分支保留结构化 `insufficient` dependency fact，但不将其 result/bundle refs 作为下游 evidence 返回，也不满足任何 `upstream_to_downstream` 动态规则。若其他 usable 分支使下游继续，Agent/context 只知道该分支无 usable evidence；只允许其余 direct/allowed-indirect evidence 进入 gate，并在适用结果中保留 branch coverage 不完整事实。

汇流处只要其他上游分支或本地/允许侧面 evidence 仍能支持部分 cell，就可以继续；缺失分支不使其他 cell 自动失败。单链或全部分支均无 usable evidence 时，下游无站节点不启动 optional planning，直接以 0 model calls 确定性收敛为完整 `indeterminate/insufficient_evidence`；后续有站节点仍须执行自己的本地 preflight。

上游 result handoff 必须为 required-support discharge 保留每个窗口的 observed/inferred/indeterminate 状态及 evidence refs，即使 discharge 不是用户目标。下游不得因字段缺省猜测 discharge，也不得把 upstream indeterminate 当作流量为零或正常基线。

provider timeout、rate limit、认证失败、协议损坏等仍是 D05 的 `failed + unavailable` 技术路径，不进入上述正常无 evidence 分支。M7 将 failed upstream 对继续执行的下游表示为 `invalid` 且不传 evidence；只要 target 仍能正常产出，非 target failure 形成 partial coverage 而不强制整个 run failed。cancelled outcome 不启动下游，且不得降级成 completed empty。

### D17：production 不公开直接 tool invocation；提供复用真实执行链的测试接口

Evidence Tools 是 waterbody Agent/application 内部能力。production public API 和 M9 frontend 不提供任意 tool invocation endpoint，不接受前端指定 provider、station、COMID、geometry、URL、任意时间范围或 tool policy；用户不能绕过 session scope、D11 空间/时间边界、调用预算和 evidence gate 将后端作为通用数据代理。

当前/历史前端只通过 M3/M6 的 run/session detail、transcript item/reference、全文读取与两层 SSE 查看已发生的 tool/evidence 事实。实时事件只含有界 call/result/manifest preview 和原 item reference；详情读取已保存的标准化事实，不重新访问 provider。历史 run 永远保留当时 evidence，不以当前数据覆盖。

M5 必须提供稳定、可自动化的测试调用面：

- application-level `EvidenceToolExecutor`/registry contract，供 unit、contract 和 integration tests 直接调用；
- 使用同一 composition、request/result schema、scope authorization、dedup、writer、deserializer、error mapping 和 fake/real adapter ports，不维护简化的第二套测试实现；
- checked-in fake/contract fixtures 覆盖正常、empty、unavailable、invalid、partial、unit/quality/aggregation、空间/时间越界和取消；
- opt-in smoke script 可在安全 preflight 后调用受控真实 provider，并报告实际请求数、endpoint contract、成功/失败与 `not run` 原因。

若 HTTP 层测试确需路由，使用独立 test application/composition 显式挂载 internal test router；该 router 不在 production application 注册，不进入 production OpenAPI，也不通过运行时 feature flag 临时开启。test route 仍必须创建合法的合成 run/session/context/graph scope 或引用测试 fixture scope，不能关闭授权校验、注入任意 URL/secret 或绕过 transcript 写入。

production 可以保留相同的 application interface 供 waterbody runtime 调用，但不得暴露对应 transport route。真实 provider smoke 必须显式 opt-in；缺凭据、网络条件不满足或未发出请求均记录 `not run`，不得记为通过或零结果。

用户可见 evidence projection 可以展示安全 provider 名称、公开 source URI、观测/获取时间、原/规范单位、质量事实、适用关系和内容引用；API key、Authorization、未脱敏 query、内部异常、完整 provider envelope 和测试控制字段不得进入公开内容。

### D18：M5 确定 RAG candidate eligibility，M8 只索引已完成且安全的语义投影

M1 已有的 `rag_eligible` 默认保持 `false`。M5 Evidence Tool 按固定 policy 为 evidence content 计算 candidate eligibility，Agent、provider response 和调用方不能自行将内容提升为可索引。M5 不实现 index upsert/search；M8 才在 candidate eligibility 之外继续校验 run/session 终态、持久化确认、memory policy 和 projection schema。

初始 eligibility 规则：

- `evidence_source_records v1` 的完整/分页原始 series 不直接索引，`rag_eligible=false`；
- tool call/result、inspection page、empty/unavailable/invalid fact、公开/内部 error 和诊断内容不索引；
- collection status 为 available、quality 为 valid 或 suspicious、已完成验证且通过脱敏的 EvidenceFragment 可以成为 candidate；
- suspicious fragment 的全部 quality flags、原因、coverage 和 provenance 必须进入索引投影，不能只索引数值/摘要；
- derived fragment 只有引用闭包完整、aggregation/conversion policy 可重放且输入均允许时才可成为 candidate；
- evidence bundle 本身是 reference assembly，不重复索引其全部子内容；M8 从 eligible fragment refs 和后续最终结果构建去重 projection；
- cancelled/incomplete operation、session 或 run 下的内容即使局部验证成功也不得进入最终索引；failed persistence 未确认前同样不得索引。

M8 projection 只保存检索所需的有界语义文本/字段、稳定原 item refs、原 run/session/COMID/ParameterKey/window、quality/applicability、schema version、时间和 provenance；不复制完整 source-record array、provider envelope、secret 或无界 tool payload。命中后按引用和权限读取原事实，索引内容不成为新的 evidence 真相源。

facility/weather 等 contextual evidence 在自身事实有效、安全时可以成为内容层 candidate，但仍保留 `contextual_only`；M8 只有在 session/run 完成且持久化确认后才允许实际索引，不得因被检索命中提升 applicability。历史推理继续标记 `historical_inference`；M8 retrieval score 不改写原 confidence、quality 或 attribution basis。

任何历史命中都不能满足当前 run 的 target/discharge gate、替代当前 observation collection，或覆盖当前 session 的合格实际观测。索引缺失、尚未构建或检索失败不改变 M5 原 evidence、transcript 和最终历史可读性。

### D19：provider 中途失败保留已确认事实，但技术性不完整窗口不得冒充完整 collection

M5 必须分别建模 provider operation 完整性与数据自身 coverage。provider 正常遍历全部约定分页/分片后，记录稀疏、时间缺口或部分质量无效属于 D08 的 observation coverage/quality；provider pagination、分片或响应流未完成则属于 technical incompleteness，不能仅用 `partial_coverage` 掩盖。

adapter/tool 必须为每个 provider batch 保存请求 UTC 范围、分页/分片 identity、完成证明和受影响 window IDs。只有 provider contract 能确定某个窗口的全部记录均已取得时，该窗口才可被标记为 collection-complete；如果 provider 排序/分页语义不能证明边界，失败页影响的全部候选窗口都必须视为技术性不完整。

中途 timeout、rate limit、transport failure 或 invalid page 后：

- 已成功解析、验证并持久化的 SourceRecord 与 provenance 不删除，也不改写；
- tool execution 记为 `failed`，保留安全错误、实际请求/页数和已完成范围；
- 已独立证明完整的窗口继续保留其 available/empty/invalid collection facts，并可按正常 gate 使用；
- 失败影响的窗口保存已取得 record refs 供审计，但增加 `unavailable/incomplete_source` blocker，不得仅凭这些 partial pages 通过 gate；
- 尚未尝试或无法证明查询完成的窗口是 unavailable，不是 empty；
- 不从记录数、最后时间戳或“本页少于 page size”等未写入 provider contract 的现象猜测已经结束。

provider retry 由 tool executor 执行有界 attempt/backoff/deadline policy；Agent 不能提高上限或制造无限 retry loop。一次 retry 若补齐所有缺页，可由新的成功 operation/result 证明相应窗口完整；既有失败 call/result 保持不可变，bundle 引用后续成功事实并保留此前失败审计链。failed result 不进入 D09 的后续成功缓存。

同一水体只要仍有至少一个满足 target + discharge gate 的窗口即可按 D15 继续，技术性不完整影响的窗口确定性输出 indeterminate。所有窗口均受 blocker 时不调用最终水体推理；不得为了生成说明文字额外消耗模型调用。

用户取消继续遵守 D05：取消胜出后停止新分页、retry、aggregation 和 downstream work，并使用 `cancelled` 而不是上述 `failed`；晚到响应不能补入 bundle 或触发模型。

## 3. 已冻结的阶段责任

| 阶段 | 责任 |
|---|---|
| M4 | 提供已验证 context、topology、station/series candidates 和 capability rules；metadata qualification 不是 evidence。 |
| M5 | 补齐当前 V1 水体空间事实；按需调用本地来源、返回 source records、构造 evidence、执行质量/单位/聚合并形成窗口级 bundle 输入；不作最终推理。 |
| M6 | 为单水体调用 tools，执行确定性 sufficiency gate，并只对通过门禁的窗口调用水体模型。 |
| M7 | 按 active DAG 在真正准备执行时创建水体 session，并复用同一 M5 tool 层；接入真实 upstream-result producer。 |
| M8 | 接入真实 transcript-RAG producer；M5 只保留 tool 槽位和 unavailable 语义。 |

## 4. 实施文档索引

已确认方向不在本文重复展开为字段清单：

| 主题 | 权威位置 |
|---|---|
| 模块、类型、执行链、provider、geometry、预算、dedup、测试与 M6–M8 接力 | [M5 实施设计](m5_evidence_tools_implementation_design.md) |
| 单位定义、Decimal 换算、value semantics、聚合、coverage、质量、applicability 与 154 项 manifest 扩展 | [M5 Evidence Policy Catalog](m5_evidence_policy_catalog.md) |
| 五类 typed content、EvidenceFragment/Bundle 示例、SSE/读取、public error 与 test-only HTTP composition | [M5 API / 读取契约](m5_evidence_tools_api.md) |

上述文档与本文共同构成 [M5 Implementation TODO](m5_implementation_todo.md) 的执行输入。实现阶段如果 provider 官方契约发生非兼容变化，应先更新 adapter contract fixture 和当前唯一 V1 设计，再实施；不得临时增加 fallback provider、兼容 union 或让 Agent 猜测缺失语义。
