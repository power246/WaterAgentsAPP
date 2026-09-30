# M5 Evidence Policy Catalog

更新日期：2026-09-30

> 状态：设计已冻结，Milestone 2 的 manifest schema、不可变 registry、规范 digest 与离线 audit 已实现；数值转换、质量判定、coverage 计算和聚合执行仍属于 Milestone 3。本文定义后续实现必须落入同一 capability manifest 的单位、质量、聚合、coverage 与 evidence relation policy；不为每条观测值维护单独规则表。分阶段施工、证据门禁和当前进度见 [M5 Milestone 0–13 Implementation TODO](m5_implementation_todo.md)。

## 1. 为什么不是“每个数值一张表”

规则分成三层：

1. `UnitDefinition`：描述“单位本身如何理解和换算”，全项目共享；
2. `PolicyProfile`：描述一类 series 如何判质量、coverage 和允许何种聚合；多个参数复用；
3. `ParameterCapability`：154 项参数各自引用 unit/profile/relation ID，并可做极少量 override。

因此观测记录只携带 provider 给出的值、单位和语义；它不会复制一份“可用单位 × 可用聚合算法 × 换算公式”。修改规则时只改 registry/manifest，并由 digest 固化到本次 evidence provenance。

## 2. Manifest 原子扩展

当前 `ParameterCapabilityManifest(schema_version=1)` 原子增加：

```text
unit_definitions[]
quality_profiles[]
aggregation_profiles[]
coverage_profiles[]
evidence_relations[]
capabilities[]
```

每个 `ParameterCapability` 在既有字段上增加：

```text
accepted_source_units[]
unit_policy_id
channel_policies[]
default_aggregation_by_resolution
quality_profile_id
allowed_indirect_evidence_rules[]    # 既有字段继续使用
```

每个 `ChannelPolicy`：

```text
provider
channel
value_semantics
series_identity_fields[]
accepted_statistics[]
aggregation_profile_id
coverage_profile_id
quality_profile_id
medium/fraction constraints
direct_observation
```

`default_aggregation_by_resolution` 以 provider/channel/resolution 为键登记默认方法，避免 continuous、daily 与 discrete channel 共用一个错误默认值。quality、aggregation、coverage profile 均允许可选 `extends_profile_id`，loader 对未知 parent 和循环引用 fail closed；当前初始 profiles 均为完整定义，不依赖继承补字段。`evidence_relations[]` 保存 relation ID、same-parameter 语义或固定 input/output ParameterKey，所有固定输入输出必须属于同一 154 项 catalog。

manifest loader 必须验证：

- 154 项 key 集合和现有 catalog hash 不变；
- 所有引用 ID 存在且无环；
- canonical unit 的 dimension 与 accepted source units 一致；
- 每个 provider/channel 都有明确 value semantics；
- 每个允许方法都能由该 value semantics 支持；
- required-support `usgs:00060` 具有 direct channel 和 discharge relation；
- 每项至少有一个 observation channel，不允许只登记名称；
- 规范序列化后的 manifest digest 可重现。

当前离线 audit 从唯一 manifest 生成 canonical JSON，报告 `154/154`、唯一 key、required support、unit/profile/relation/channel 数量、引用闭包状态和完整 manifest SHA-256。可选 official refresh 只报告 drift，不修改 manifest。

## 3. 单位定义与换算

### 3.1 UnitDefinition

```text
unit_id
symbol
aliases[]
dimension_id
scale_to_base          # 十进制字符串
offset_to_base         # 十进制字符串
kind: linear | affine | identity_only
precision
```

线性/仿射换算统一为：

```text
base = source * scale_to_base + offset_to_base
target = (base - target.offset_to_base) / target.scale_to_base
```

实现使用 `Decimal` 和 manifest 中的十进制常量，不使用二进制 float 串联换算。中间值至少保留 28 位 Decimal context；只在输出边界按 target precision 使用 round-half-even。原值、原单位、公式 ID、registry digest、未舍入结果的规范十进制表示与最终值进入 conversion provenance。

### 3.2 Unit identity 和 alias

alias 先做 Unicode NFKC、trim、casefold 和受控空白规范化，再查精确 registry。不得使用模糊匹配猜单位。

示例定义：

| dimension | 允许的 unit family | 说明 |
|---|---|---|
| length | m、cm、mm、ft、in | 线性 |
| velocity | m/s、ft/s | 线性 |
| volumetric flow | m3/s、ft3/s | 线性 |
| temperature | degC、degF、K | 仿射 |
| mass concentration | mg/L、ug/L | 线性；带“as N/as P/as CaCO3”等不同化学基准不视为普通单位 alias |
| conductivity | uS/cm、mS/cm | 线性；`@25C` 是 measurement semantics，不只是一段单位文字 |
| percentage | % | identity/scale；不得和任意 dimensionless 自动互换 |
| pH | standard units | `identity_only`；不是可与普通 dimensionless 任意换算的量 |
| count density | count/100mL 等 | 分母体积是单位的一部分 |
| precipitation depth | mm、in | 线性；累计/区间含义由 value semantics 决定 |

未知单位、化学基准不一致、温度基准不一致或 dimension 不同均返回 `evidence_unit_unresolved`，保留原记录但不能进入 quantified aggregate。Agent 不能自行提供换算系数。

### 3.3 自定义目标单位接力

M9 的 session override 只能选择已登记、dimension 兼容的 target unit；新增 unit definition 或修改全局 canonical unit 仍需 M9 明确确认和版本记录。M5 unit tool 不接收任意公式。

## 4. Value semantics

每个 series 必须属于以下一种语义：

| semantics | 含义 | 典型来源 |
|---|---|---|
| `instantaneous` | 某个时刻的读数 | continuous sensor、discrete sample |
| `interval_mean` | provider 对一个明确区间的均值 | daily mean |
| `interval_min` | provider 对区间的最小值 | daily minimum |
| `interval_max` | provider 对区间的最大值 | daily maximum |
| `interval_total` | 一个明确区间内的增量总量 | interval precipitation |
| `cumulative_counter` | 自某个起点累计、可能 reset | cumulative precipitation |
| `categorical_state` | 非数值状态 | weather/facility status |
| `forecast_interval` | 预报在有效区间的值 | NWS grid forecast |

provider statistic 是 series identity 的一部分。`daily mean` 已经是 interval_mean，不能当作瞬时值后再与 daily max 混合，也不能再次对“累计总量”无条件求和。

## 5. Aggregation policy

### 5.1 固定算法

M5 只实现：

```text
mean
median
min
max
sum
p90
latest
counter_delta_sum
```

`counter_delta_sum` 是累计计数器的专用确定性算法，不对 Agent 作为自由选择项展示。稳定算法：按 `(observed_time, record_id)` 排序；相邻非负差计入；负差视为 reset 并从新值开始新段，同时标记 suspicious/reset；无法确认排序或重复时间含不同值时不计算该段。

### 5.2 Profile，而非每个观测值配置

初始 profile：

| profile | 允许方法 | 默认 | 禁止/说明 |
|---|---|---|---|
| `continuous_level` | mean、median、min、max、p90、latest | mean | 禁止 sum |
| `continuous_ph` | mean、median、min、max、p90、latest | mean | pH 只接受 identity unit；禁止 sum |
| `continuous_discharge` | mean、median、min、max、p90、latest | mean | 禁止对流量读数 sum；总过水量不是同一参数 |
| `discrete_concentration` | mean、median、min、max、p90、latest | latest | 多样本 aggregate 一律是 inferred；禁止 sum |
| `discrete_count_density` | mean、median、min、max、p90、latest | latest | 禁止 sum，除非未来登记不同“总数”参数 |
| `interval_total` | sum、min、max、latest | sum | 只允许无重叠区间；重叠时 blocked |
| `cumulative_counter` | counter_delta_sum、latest | counter_delta_sum | 禁止直接 sum 累计值 |
| `provider_interval_statistic` | latest | latest | 保留 provider statistic；多个同类区间的再聚合需专门 capability override |
| `forecast_numeric` | mean、min、max、sum、latest | 由变量 profile 定义 | precipitation amount 可 sum；temperature 不可 sum |
| `categorical_context` | latest | latest | 不产生 quantified target aggregate |

154 项 capability 只引用上述或新增的共享 profile。确需例外时新增命名 profile 或显式 override，并提供 fixture；不得在代码中按参数名称写 if/else。

### 5.3 聚合前置条件

`aggregate_evidence` 必须同时满足：

- 所有输入属于同一 `series_identity`；
- ParameterKey、provider/channel、station/site、medium、fraction、method、statistic 和 value semantics 兼容；
- 全部单位可确定性转为 capability canonical unit；
- record 时间与目标 window 的相交规则由 profile 允许；
- 方法存在于 capability 的 allowed methods；
- 没有 invalid input；
- technical collection completeness 已证明；
- count/coverage 达到 profile 阈值。

不同 station、sample fraction、method、statistic 或 provider channel 默认不得合并。Agent 可以分别聚合后在 M6 解释，不可要求 M5 替它选择赢家。

### 5.4 stable math

- mean 使用 Decimal sum/count；
- median 采用排序后中位值，偶数个取中间两值均值；
- p90 使用固定 nearest-rank：`ceil(0.90 * n)`，1-based；
- latest 以 observed time 最大者为候选，同时间只有一条唯一 record 才成立；同时间不同值为不唯一，返回 blocked 供 Agent 检查；
- min/max 以 canonical Decimal 比较；
- sum 只对 `interval_total` 的互不重叠 interval 或 capability 明确允许的增量使用。

输入排序、算法版本、canonical values、record refs、coverage 和 registry digest 均进入 derivation provenance。

## 6. Coverage policy

coverage 与 quality 分开：一个数值可以质量 valid 但窗口覆盖不足。

```text
CoverageSummary
  status: complete | partial | insufficient | unknown
  observed_count
  expected_count
  covered_duration_seconds
  window_duration_seconds
  ratio
  largest_gap_seconds
```

初始共享 profile：

| profile | complete | partial | insufficient |
|---|---|---|---|
| `continuous_regular` | expected cadence 已知，ratio ≥ 0.75 且 largest gap ≤ 2× cadence | ratio ≥ 0.50 | 低于 partial 或 expected cadence 不可确认 |
| `continuous_latest` | 窗口内至少 1 条且距 window end 不超过 2× cadence | 有记录但更旧 | 无记录 |
| `discrete_sample` | 不宣称持续覆盖；单条为 partial | 至少 1 条 usable record | 无 usable record |
| `provider_interval` | provider interval 完整覆盖或按 policy 可精确裁切 | 只覆盖部分窗口 | 无覆盖/边界不明 |
| `forecast_interval` | union 覆盖完整 window | union 覆盖部分 window | 无交集 |

profile threshold 是默认值；capability/channel 可以通过命名 profile 收紧。不能因记录很多就猜 expected cadence，也不能用 partial coverage 掩盖 pagination 技术失败。

aggregation 允许 partial coverage 时，derived fragment 必须标记 `partial_coverage` 和真实 coverage；是否足以进入 M6 directional/quantified gate 由 capability relation 与 M6 policy 决定。`insufficient` 不形成可解锁门禁的 aggregate。

## 7. Quality policy

### 7.1 三类质量

| class | 行为 |
|---|---|
| `valid` | 可按 capability policy 参与聚合和门禁。 |
| `suspicious` | 保留并可参与聚合；derived fragment 继承所有 flags，记录 suspicious/total counts；M6 必须看到降级事实。 |
| `invalid` | 保留审计，不进入聚合、observed 资格或门禁。 |

### 7.2 通用 invalid

以下任一成立即 invalid：

- 值缺失、NaN、Infinity 或非可解析数值；
- 单位缺失且 provider contract/series metadata 不能唯一补全；
- ParameterKey、station、时间或 series identity 与请求不匹配；
- provider 明确标记 rejected/invalid；
- 时间范围倒置或落在允许扩展边界外；
- source row 缺少构造唯一 provenance 所需字段。

### 7.3 通用 suspicious

以下事实默认 suspicious，而不是 invalid：

- provisional/working/unapproved；
- provider qualifier 表示 estimated、ice、equipment/process caveat；
- coverage partial；
- cumulative counter reset；
- provider 明确给出 detection-limit/censoring 语义但数值仍可按 capability 使用；
- capability 登记的物理 plausibility soft bound 外值。

hard bound 只用于不可能值并必须在 capability/profile 明确登记；不能用统计 outlier 算法自动删除记录。M5 不跨 series 判冲突，也不产生单独的 conflict quality flag；当前唯一 V1 不包含该兼容枚举或特殊处理。Agent system prompt 负责检查显著差异、引用相关 series 并解释或保持不确定。

### 7.4 Provider mapping

映射表按 provider/channel profile 固化，至少覆盖：

- USGS OGC `approvals_status` 与 `qualifier`；
- Samples result status、detection/quantitation、remark/qualifier；
- NWS quality/control fields 和缺测值；
- EPA facility 坐标/permit/status 缺失事实。

未知 qualifier 采用保守 `suspicious/provider_flagged`，保留原 token；未知结构或缺必需字段为 invalid protocol。任何 mapping 变化都更新 fixture 和 registry digest。

## 8. Applicability 与 relation

### 8.1 三档 applicability

| applicability | 能否解锁 target/discharge gate | 示例 |
|---|---|---|
| `direct_measurement` | 是 | 当前水体、适用窗口、正确参数与 series 的具体观测 |
| `allowed_indirect` | 是，但 M6 必须降级 | 同参数上游结果、登记的相邻时间基线、登记的天气/流量关系 |
| `contextual_only` | 否 | 设施邻近、未登记天气、历史推理、一般背景事实 |

### 8.2 初始 evidence relations

| relation ID | 输入要求 | 输出限制 |
|---|---|---|
| `same_parameter_upstream` | 直接上游已收敛结果、同 ParameterKey/window、引用闭包完整 | 下游只能 inferred；保留 upstream confidence/uncertainty |
| `same_location_adjacent_time` | 当前水体同 series，最多超出目标窗口前/后一个当地日，capability 明确允许 | 只能 allowed-indirect，不伪装当前 observed |
| `turbidity_precipitation_discharge_directional` | 同窗 precipitation 与 discharge 均有 usable evidence | 只支持 directional/inferred，不生成具体 turbidity observed value |
| `discharge_gage_height_estimate` | capability 登记的 station/rating 关系与适用时效 | inferred discharge，保留公式/版本 |
| `discharge_basin_estimate` | 登记的 basin/上游输入与模型版本 | inferred discharge，不能作为 direct |

未登记关系一律 contextual。facility proximity 默认 `contextual_only/hypothesized`；ECHO facility 本身不证明目标水体当前受到排放。历史 RAG 默认 contextual，不能满足当前 target 或 discharge gate。

### 8.3 Direct 判定

direct 必须同时满足：

- `record.location.comid == agent_comid`，或来自 M4 已确认属于该 segment 的 station ref；
- ParameterKey 与目标 cell 相同；
- window/time semantics 相交规则成立；
- medium/fraction/method/statistic 被 capability 接受；
- collection complete；
- quality 为 valid 或 suspicious；
- provenance/reference closure 完整。

Agent 不能修改 applicability；它只能选择是否使用、调用 inspection 或在结果中解释。

## 9. Series identity 与重复

water observation identity 至少由以下组成：

```text
provider + channel + station_id + ParameterKey
+ medium + fraction + method + statistic + value_semantics
+ provider series identity/qualifiers that change meaning
```

只有 provider 提供同一稳定 record ID，或规范字段全部相同形成同一 deterministic identity 时才能去重。去重后保留所有发现 page/channel provenance。不得仅按时间和值相同推断重复，也不得跨 series 选一条“看起来更合理”的记录。

## 10. RAG candidate policy

`rag_eligible` 仍默认 false，由固定 policy 计算：

- raw source record batches、tool calls/results、errors、inspection page、empty/unavailable/invalid facts：false；
- valid/suspicious、已脱敏、reference-complete 的 direct/derived/context fragments：内容层可写 `rag_eligible=true`，该字段只表示 candidate，不表示已经进入索引；
- derived fragment 必须可由 policy/version/input refs 重放；
- bundle 仅组装引用，不重复索引；
- operation/content incomplete 时不成为 candidate；session/run 后续 cancelled/failed 或 persistence 未确认不改写 append-only fragment，但 M8 必须拒绝索引；
- 最终是否索引由 M8 根据 candidate、生命周期、持久化和 projection policy 再校验。

candidate 资格不改变 applicability。历史检索命中也不能把 contextual 提升为 allowed-indirect。

## 11. 154 项配置验收

实现时不手工再维护一份 Markdown 规则表。以唯一 manifest 为源生成审计报告，至少验证：

- 恰好 154 项、key/hash 与 M4 catalog 一致；
- 每项 canonical/accepted units 可解析；
- 每个 channel 有 value semantics、quality/coverage/aggregation profile；
- 每项默认聚合存在于 allowed methods；
- pH、discharge、precipitation cumulative/interval、daily statistic、concentration、count density 各有正反 fixture；
- 不存在 pH/浓度/流量读数使用 `sum`、累计计数器再次直接求和等明显错误组合；interval total 等合法 `sum` profile 不受影响；
- 所有 indirect relation ID 都存在且输入参数已登记；
- manifest/生成报告在不同运行与平台字节稳定。

Markdown catalog 只解释规则；实施验收以 manifest loader、生成审计和 fixture 为准。
