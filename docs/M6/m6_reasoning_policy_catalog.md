# M6 Reasoning Policy Catalog

更新日期：2026-10-02

> 状态：设计已确认，尚未实现。本文件是 M6 system prompt、确定性 sufficiency/validation 和结果 assembly 的共同政策源；实现时应由 typed policy/config 生成 prompt 片段和测试审计，不能在多个模块复制不同规则。

## 1. Policy 分层

M6 policy 分为三层：

1. **Backend hard policy**：权限、scope、gate、shape、单位、引用、身份、预算和 lifecycle，不允许模型覆盖；
2. **Agent reasoning policy**：风险、confidence、series 解释、归因和简述，由 system prompt 约束并由 Agent判断；
3. **Presentation projection**：SSE/history/frontend 的轻量展示，不改变原事实。

同一规则只能有一个运行时来源。PromptBuilder 从 typed policy 和当前 sealed input 生成内容；validator 只实现 hard policy。禁止把 prompt 文字再解析回权限或 gate，也禁止为测试维护第二份手工 allowlist。

## 2. Evidence acquisition policy

### 2.1 起点

| 水体类型 | 确定性动作 | Agent planning |
|---|---|---|
| station-backed | 先执行当前水体 observation + discharge preflight | 至少一个 usable direct evidence 或有效 required rule 路径存在时允许；成功全空则停止 |
| stationless | 不注册 local observation；先读直接上游 producer slot | 始终允许有界 planning，即使初始没有水质 evidence |

`station_refs` 来自保存的 M4/M5 scope，Agent 不能自报是否有站。

### 2.2 自动动作

后端只自动执行：

- station-backed preflight；
- stationless upstream-result read；
- baseline/appended rule 中 `required_collection_attempt=true` 的合法请求；
- relation materialization；
- bundle/gate rebuild。

天气不会仅因 prediction mode 自动 mandatory。facility 永远不是自动 mandatory。Agent 未选择的 optional tool 不是 failure，也不能被记录为 empty。

### 2.3 Optional tools

Agent-facing M5 roster 保持：

```text
collect_water_observations
collect_weather_evidence
collect_facility_evidence
get_upstream_results
search_transcript_evidence
convert_evidence_unit
aggregate_evidence
inspect_evidence_records
```

实际 roster 由 scope/policy/producer status 裁剪：

- stationless 不含 `collect_water_observations`；
- M7 未接入时 upstream producer 显式 unavailable；
- M8 未接入或 stateless 时 transcript search 不注册；
- provider routing、COMID、station、geometry、URL、credential 不作为自由参数；
- deterministic tools 仍受 parameter/unit/series/pagination constraints。

### 2.4 Rule append

Agent rule proposal 必须包含：

```text
output_parameter
input_parameters[]
temporal_relation
spatial_relation
max_strength=directional
required_collection_attempt
rationale
```

后端要求 ParameterKey 已登记、input/output 完整、关系类型受支持、scope 不越权、规则不重复、总数在预算内。append-only；重复同一规范规则为 no-op；没有 delete/replace。rule proposal 不是 evidence，只有 materializer 生成的 `RelationSupportPayload` 才能成为 allowed-indirect。

## 3. Sufficiency gate policy

### 3.1 Cell gate

每个 target ParameterKey/window 同时检查：

```text
target cell + same-window usgs:00060 discharge cell
```

| Target | Discharge | Gate | Model |
|---|---|---|---|
| supported | supported | supported | 允许进入 reasoning batch |
| unsupported | 任意非 blocked | unsupported | fixed indeterminate |
| 任意非 blocked | unsupported | unsupported | fixed indeterminate |
| blocked | 任意 | blocked | 不进入 reasoning |
| 任意 | blocked | blocked | 不进入 reasoning |

`supported` 只接受 direct_measurement 或 materialized allowed_indirect。contextual_only、M4 metadata、未物化 rule、facility proximity、一般天气事实和历史推理不能单独支持 gate。

### 3.2 Technical blockers

以下不等于 empty：

- authentication/rate limit/timeout/provider unavailable；
- invalid protocol/schema；
- pagination/shard incomplete；
- unit unresolved for required numeric use；
- cancelled operation；
- required producer unavailable。

部分完整窗口可继续，受影响窗口 blocked。全部 cells 因 required technical blocker 无法执行时 session failed，不用固定 business indeterminate 掩盖服务故障。

### 3.3 Fixed indeterminate

自然 unsupported cell 的固定结果：

```text
outcome_status=indeterminate
representative_value=null
unit_id=null
source=null
risk_level=indeterminate
confidence=0
reason_codes=[insufficient_evidence]
```

根据事实可以追加更具体 reason：

```text
no_station_path
parameter_unobserved
invalid_observations
no_applicable_evidence
required_discharge_missing
```

reason 不得把 unavailable 写成 unobserved。fixed rationale 由后端稳定模板生成，只陈述事实，不伪装 Agent 推理。

## 4. Outcome policy

### 4.1 Quantified

要求：

- gate supported；
- 有数值资格的 direct/derived evidence；
- target unit 已登记且 dimension/semantics 兼容；
- evidence refs 足以重放数值来源；
- confidence/rationale 完整。

只有 allowed-indirect 时禁止 quantified。contextual evidence 可以解释，但不能成为数值的唯一依据。

### 4.2 Directional

要求：

- gate supported；
- `representative_value=null`；
- 至少一个 definite trend 或非-indeterminate risk；
- allowed-indirect 时必须引用 relation support 及全部 input closure；
- 明确说明限制与 confidence。

### 4.3 Agent-selected indeterminate

即使 gate supported，Agent 仍可因 series 差异、coverage、方法差异或无法合理选择而输出 indeterminate。此时必须引用导致不确定的 evidence，提供 reason/rationale；不能简单省略 cell。

Agent-selected indeterminate confidence 由 Agent 给出，但应反映无法形成可靠水质判断；后端固定 unsupported cell 才强制 confidence=0。

## 5. Source policy

| 情况 | ResultSource |
|---|---|
| 当前水体/窗口一条具体 direct record 原样采用，可逆展示换算 | observed |
| 任意 aggregate、多记录、跨 series 选择、上游/天气/关系、模型 gap fill | inferred |
| prediction segment 的模型判断 | predicted |
| indeterminate | null |

`latest` 只有在唯一 direct record 原样采用且没有跨 series 选择时可 observed。Agent 声明 source 不能改变资格；validator 从 refs/derivation 校验。

## 6. Series comparison policy

System prompt 必须要求 Agent：

1. 先按 series identity 分开阅读；
2. 比较 station/site、time、medium/fraction、method、statistic、value semantics、quality、coverage；
3. 检查 suspicious flags，不把 suspicious 当 invalid；
4. 引用所有影响判断的相反或重要记录；
5. 说明采用、分别保留、降低 confidence 或 indeterminate 的理由；
6. 不静默删除、不平均不兼容 series、不声称后端已经检测“冲突”。

validator 不实现“差多少算冲突”的阈值，不替 Agent 选择赢家。

## 7. Unit 与 aggregation policy

- Agent 只选择 registry 允许的 unit/method；
- 换算、聚合必须调用 M5 deterministic tools；
- 不接受 Agent 自报 conversion factor、计算后的未引用数字或跨 series aggregate；
- unknown unit、dimension mismatch、chemical basis mismatch、incomplete collection 均不能 quantified；
- suspicious 可在 capability 允许时参与 aggregation，结果传播 flags/counts；invalid 不参与；
- tool result/derived fragment 必须进入 final refs，不能只在 Agent 文本中描述。

## 8. Risk policy

### 8.1 枚举

```text
none | low | medium | high | indeterminate
```

### 8.2 Prompt semantics

| Level | Prompt 约束 |
|---|---|
| none | evidence 足以支持当前 scope 内未识别到风险；不能由缺失、未查询或低 confidence 推出 |
| low | 存在可解释的低程度风险/异常/趋势，或 evidence 支持总体风险较低，但不得表述为绝对安全 |
| medium | 存在需要关注的风险、趋势或不利因素，依据与限制明确 |
| high | 存在显著风险信号；不得被其他低风险参数通过无依据平均抵消 |
| indeterminate | evidence、coverage、blocker 或 series 差异使等级无法合理判断 |

这些是 reasoning 指令，不是后端阈值表。具体环境标准、法规阈值或健康结论只有在 capability/evidence 明确提供并被引用时才能使用；Agent 不得凭常识编造阈值。

### 8.3 Rationale

每个 Agent risk 必须提供：

```text
risk_level
risk_rationale
supporting_refs[]
```

rationale 说明 evidence -> interpretation -> level 的简短链路，包含重要缺失/相反事实。只重复等级名称不合格。validator 只检查非空、refs 和 scope，不评分内容。

## 9. Confidence policy

### 9.1 定义

confidence 表达对当前水质判断的把握，不表达风险严重程度，也不是 collection completion 的替代字段。

Agent 应考虑：

- direct vs allowed-indirect；
- quality valid/suspicious；
- coverage complete/partial；
- station/time/method 匹配；
- series 一致性；
- 推理跨度和 prediction horizon；
- missing/blocker facts。

### 9.2 输出

每层 Agent confidence 都要求：

```text
confidence: [0,1]
confidence_rationale
supporting_refs[]
```

后端不平均 cell confidence 生成 series/overall，不设 observed 固定高分，也不将 risk level 绑定到 confidence 区间。

fixed insufficient confidence=0。Agent-selected indeterminate 可使用非零值表达“对无法判断结论的把握”会产生歧义，因此 prompt 要求它仍以对水质判断的把握为准并保持低值；后端只验证 `[0,1]`。

## 10. Composite/overall policy

### 10.1 Window composite

Agent 对同一 window 的全部 requested parameter results、missing/blocker facts做综合判断，输出 risk/confidence/rationales 和 contributing cell refs。

### 10.2 Range overall

Agent 对全部 window composites 和关键 cell results做整体判断。不得：

- 把不同参数数值直接相加/平均；
- 用 `overall_confidence` 代替风险；
- 忽略 high、indeterminate 或 blocked windows；
- 将 partial availability 描述成完整安全结论。

### 10.3 Backend availability

```text
complete: 所有计划 cells 有 validated non-fixed assessment，且没有 blocker
partial: 至少一个 cell 可判断，但存在 fixed/Agent indeterminate 或 blocker
indeterminate: 没有任何可判断 cell
```

counts/refs 由后端计算。Agent 看到这些事实并给风险结论，但不能修改 availability。

## 11. Attribution policy

`basis`：

| Basis | 使用条件 |
|---|---|
| observed | 直接记录本身作为因素，仍受一对一 refs 约束 |
| correlated | capability rule 的 allowed-indirect relation，引用完整 relation/input closure |
| inferred | aggregate、跨记录/上游传播或其他有依据推导 |
| hypothesized | contextual facility/weather/背景假设，只能解释，不能解锁 gate |

factor 必须有 evidence refs 和涉及时的 cell refs。hypothesized 不得写成已证实原因。

## 12. Prompt phase permissions

| Phase | Tools | 可输出 | 禁止 |
|---|---|---|---|
| evidence_planning | 当前合法 M5 roster | tool calls、append rule proposal、evidence_ready、可见简述 | final cell result、任意 provider request |
| reasoning_batch | 无 | batch 内 cell drafts、可见简述 | tool call、scope 外 cell、overall result |
| overall_synthesis | 无 | series/window/range summary、analysis/attribution drafts | 新 evidence、改写 cell value/gate |
| schema_repair | 无 | 同一 scope 的修正结构 | 新 reasoning、tools、新 refs、模型切换 |

任一 phase 越权输出为机械 validation error。

## 13. Repair policy

- 每个 structured exchange 最多一次 repair；
- repair 只处理 schema/机械不变量；
- validation issues 使用稳定 code/path，不泄露内部异常；
- repair 复用同一 provider/model/scope/evidence/batch；
- repair 不增加 planning/tool budget，不允许 tools；
- repair 后完整重验；
- 仍无效则 session failed；
- risk/confidence/rationale 的语义不同意不是 repair 理由。

## 14. Failure 与 cancellation policy

| Failure | 行为 |
|---|---|
| optional evidence unavailable，其他 gate 足够 | 继续，保留 blocker/availability |
| 全部 cells 自然 unsupported | completed + fixed indeterminate，0 final reasoning calls |
| 全部 cells technical blocked | failed，0 final reasoning calls |
| planning/model provider unavailable/timeout/content filter | failed |
| invalid structured output after repair | failed |
| user/run cancellation | cancelled；停止新 operation，固定 incomplete message，晚到只审计 |

失败/取消不删除已产生过程，不传播残缺 final result。

## 15. Budget policy

默认 hard limits：

```text
planning_turns=6
tool_calls_per_turn=4
reasoning_batch_cells=32
reasoning_batches=24
model_timeout_seconds=90
session_deadline_seconds=600
repair_per_exchange=1
```

M5 tool budgets 继续独立生效并且更严格者优先。达到上限后 seal 当前 evidence；不得提高上限、循环 retry 或删 cells。输入无法在 batch 上限内完整覆盖时 fail before model calls with `reasoning_plan_too_large`。

## 16. Prompt audit

实现时提供离线 audit，至少验证：

- shared base policy 与本 catalog 的稳定 rule IDs 一一覆盖；
- 每个 phase permissions 正确；
- 风险五级、risk/confidence rationale、absence-of-evidence、series、observed、units、gate、secret 规则存在；
- tools 从当前 registry 生成，无 provider routing/credential 参数；
- repair prompt 没有 tools；
- prompt canonical digest 在相同输入下可重现；
- 文档不作为运行时解析源，typed policy/config 才是源。
