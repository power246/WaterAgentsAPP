# M6 Single-Agent Reasoning 实施设计

更新日期：2026-10-03

> 状态：当前唯一 V1 已完成 M6 Milestone 0–13 本机实现与集成审计。本文描述已落地的代码结构与可执行契约；分阶段提交、验证证据、外部 `not run` 项和剩余风险见 [M6 Implementation TODO](m6_implementation_todo.md)。

> M7 接力说明：本文 2026-10-03 只在第 16 节登记 dependency manifest、stationless 0-model 调度和 `run_summarizer` 接缝；前文仍描述已实现的 M6 单水体 runtime，M7 目标尚未实现或验证。

## 1. 核心结论与范围

M6 在一个已经存在的 waterbody session 中运行一个受约束 Agent。它复用 M5 evidence runtime、M3 transcript/session SSE 和 M4 固化的 `AnalysisContext.waterbody_model`，不创建新产品入口。

唯一主路径为：

```text
WaterbodyAgentTask
  -> deterministic initial acquisition
  -> bounded evidence-planning Agent loop
  -> sealed EvidenceBundle + final cell gates
  -> deterministic result skeleton + batch plan
  -> reasoning batches (supported cells only)
  -> overall synthesis
  -> deterministic validation + assembly
  -> inference_result transcript item
  -> session_completed(result_ref + lightweight summary)
```

M6 只执行一个 waterbody session。M7 后续负责 DAG、上游等待、session 创建和 run-level aggregation；M8 后续负责 RAG producer；M9 后续负责最终前端与 settings/admin。

## 2. Application 输入与阶段状态

### 2.1 WaterbodyAgentTask

```text
WaterbodyAgentTask
  run_id: UUID
  session_id: UUID
  agent_role: waterbody
  target_comid: str
  analysis_context_ref: ItemReference
  topology_ref: ItemReference
  evidence_scope: EvidenceSessionScope
  waterbody_model: ModelSelection
  memory_mode: stateless | memory_augmented
  cancellation_token
```

validator 必须证明：

- session 已存在、属于 run、角色是 waterbody、COMID 一致且处于可启动状态；
- context/topology/scope 均属于同一 run，requested/supporting parameters、time windows、station refs 和 geometry 一致；
- `waterbody_model` 等于 run 创建时固化的选择且 model catalog 允许 `waterbody` role；
- M8 未实现时 `memory_augmented` 只保留配置事实，`search_transcript_evidence` producer 显式 unavailable，不能 hidden fake；
- cancellation 已接受时不启动任何新 tool/model operation。

### 2.2 内部阶段

不维护第二套 reasoning stage 枚举。模型交互阶段使用 `WaterbodyModelPhase`，失败位置使用 `PublicError.failed_stage`，session lifecycle 仍只有 pending/running/completed/failed/cancelled；过程记录继续通过既有 session SSE `transcript_item` 通知，不增加 lifecycle event type。

## 3. 模块与依赖方向

建议新增：

```text
backend/app/
  application/
    reasoning/
      __init__.py
      task.py                    # WaterbodyAgentTask / validated task
      runner.py                  # RunSingleWaterbodyAgent
      acquisition_loop.py        # planning turn + M5 executor orchestration
      rule_appender.py           # append-only EvidenceRulePlan validation
      sufficiency.py             # sealed gate snapshot
      batch_planner.py           # deterministic supported-cell batches
      prompt_builder.py          # planning/batch/synthesis/repair prompts
      model_models.py            # provider-neutral request/event/response DTO
      result_models.py           # draft/skeleton/composite DTO
      validator.py               # mechanical output validation
      assembler.py               # final InferenceResult assembly
      transcript_store.py        # M6 typed contents + streaming message writer
      errors.py
  ports/
    waterbody_model.py           # streaming waterbody model port
  infrastructure/
    adapters/
      openai_responses_waterbody.py
    runtime/
      m6.py
```

依赖必须保持：

```text
domain <- application <- ports <- infrastructure/api composition
```

`reasoning` 可以依赖 `application.evidence_tools` 的公开 handoff/reader/executor DTO，不得导入 USGS/NWS/EPA adapters。M5 不反向依赖 M6。Domain 不依赖 prompt、provider response 或 transcript application models。

## 4. 当前唯一 V1 Domain 收敛

### 4.1 RiskLevel 与 availability

`RiskLevel` 原子收敛为：

```text
NONE
LOW
MEDIUM
HIGH
INDETERMINATE
```

新增：

```text
AssessmentAvailability
  complete
  partial
  indeterminate
```

availability 由后端依据计划 cells、validated cells、fixed indeterminate cells 和 blockers 机械装配；risk level 由 Agent 判断。二者是独立字段。

### 4.2 TimeWindowResult

当前模型原子增加并收敛为：

```text
TimeWindowResult
  cell_id: str
  start_time: UTC datetime
  end_time: UTC datetime
  outcome_status: quantified | directional | indeterminate
  representative_value: Decimal-string | null
  unit_id: str | null
  assessment: RiskOrTrendAssessment
  source: observed | inferred | predicted | null
  confidence: float [0,1]
  confidence_rationale: str
  aggregation_method: method | null
  evidence_refs[]
  quality_flags[]
```

使用规范 decimal string 保存模型/业务值，避免模型 JSON float 在后续组装中重复舍入。Interface 可以按 schema 输出 JSON string；如果最终产品必须输出 number，需要在 M9 明确精度和序列化边界，不能在 M6 隐式转二进制 float。

机械不变量：

- quantified 要求 value、unit、非空 evidence refs 和非空 confidence rationale；
- directional 禁止 value，要求明确 trend 或 risk；
- indeterminate 禁止 value/unit/aggregation，要求 reason codes；
- indeterminate 的 `source=null`；
- fixed insufficient result confidence=0；
- observed 资格按 M5 direct record 一对一闭包验证；
- prediction quantified/directional source=predicted；gap-filling 不允许 predicted；
- cell identity 唯一并与 ParameterKey/window plan 对应。

### 4.3 RiskOrTrendAssessment

```text
RiskOrTrendAssessment
  summary: str
  trend: rising | falling | stable | uncertain | null
  risk_level: none | low | medium | high | indeterminate
  risk_rationale: str
  reason_codes[]
  supporting_refs[]
```

Agent 产生的风险要求 `risk_rationale` 和 refs。后端 fixed indeterminate 使用稳定 reason/rationale，不制造 Agent 文本。

### 4.4 ParameterTimeSeries

```text
ParameterTimeSeries
  parameter
  time_resolution
  time_resolution_source
  points[]
  confidence
  confidence_rationale
  contributing_cell_refs[]
```

points 必须精确覆盖 window plan，按 start_time 排序且不重叠。series confidence/rationale 来自 overall synthesis；如果整条 series 都是 fixed indeterminate，则后端设 confidence=0 并使用固定 rationale。

### 4.5 CompositeRiskAssessment

```text
CompositeRiskAssessment
  assessment_id: str
  start_time
  end_time
  risk_level
  risk_rationale
  confidence
  confidence_rationale
  availability
  assessed_cell_count
  total_cell_count
  contributing_cell_refs[]
  missing_cell_refs[]
  blocker_refs[]
  evidence_refs[]
```

每个 final window 一条，start/end 必须和 window plan 完全一致。counts、missing/blocker refs、availability 由 assembler 计算；risk/confidence/rationales 来自 validated synthesis。全部 cells indeterminate 时由后端固定生成。

### 4.6 OverallWaterbodyAssessment

```text
OverallWaterbodyAssessment
  assessment_id: str
  time_range
  risk_level
  risk_rationale
  confidence
  confidence_rationale
  availability
  assessed_cell_count
  total_cell_count
  contributing_window_refs[]
  contributing_cell_refs[]
  missing_cell_refs[]
  blocker_refs[]
  evidence_refs[]
```

它只引用决定整体判断的窗口/cells/evidence，不复制 series。range 与 `InferenceResult.time_range` 一致。

### 4.7 InferenceResult

```text
InferenceResult
  result_id
  run_id
  session_id
  target_comid
  mode
  time_range
  time_segments[]
  analysis_timezone
  time_resolution
  time_resolution_source
  series[]
  window_assessments[]
  overall_assessment
  overall_confidence
  overall_confidence_rationale
  attribution_summary
  analysis_detail
  evidence_chain
  model_execution_summary
  generated_at
```

`model_execution_summary` 只保存安全计数和 model identity：planning calls、tool calls、reasoning batches、repair calls、synthesis calls、input/output usage（provider 提供时）和 skipped cells。它不保存 secret、provider raw envelope 或 hidden reasoning。

## 5. Provider-neutral waterbody model contract

### 5.1 Port

```text
WaterbodyModelPort.stream(
  request: WaterbodyModelRequest
) -> AsyncIterator[WaterbodyModelEvent]
```

不用 query parser 的 `generate()` adapter 承担 M6；parser 禁止 tools 的权限边界保持不变。M6 adapter 独立支持 visible text streaming、tool calls 和 structured terminal output。

### 5.2 Request

```text
WaterbodyModelRequest
  operation_id: UUID
  phase: evidence_planning | reasoning_batch | overall_synthesis | schema_repair
  model: ModelSelection
  system_prompt: str
  messages[]
  tool_descriptors[]
  response_schema
  scope_digest
  evidence_bundle_ref
  batch_id: str | null
  deadline
```

request 只能携带 safe/normalized application data。API keys 由 adapter config/header 提供，不进入 DTO、transcript body 或错误。`scope_digest` 绑定 run/session/COMID/parameters/windows/rule plan/registry digest，response validation 必须核对。

### 5.3 Events

```text
WaterbodyModelEvent =
  VisibleTextDelta(text)
  ToolCallCompleted(provider_call_id, name, arguments)
  StructuredOutputCompleted(value)
  RefusalCompleted(text)
  UsageReported(input_tokens?, output_tokens?)
  Terminal(finish_reason)
```

adapter 必须拒绝未知 item/content、重复 terminal、terminal 后输出、空/损坏 tool arguments 和 provider identity 变化。tool arguments 在进入 executor 前再次由 application strict DTO 验证。

`VisibleTextDelta` 先经过跨 chunk credential redaction，再写 session streaming message。provider tool arguments 不直接进入可见文本；安全 typed call 由 M5 writer 写入。

### 5.4 OpenAI Responses adapter

首个真实 adapter 与 parser adapter 共用安全 HTTP/config/finish-reason 原则，但代码职责独立：

- endpoint 和 credential 只来自 composition；
- waterbody request 允许本次 roster 的 function tools；
- output item 顺序和 provider call id 保留；
- incomplete/content filter/timeout 映射为 typed port error；
- 不接受 provider 内置 web/file/MCP/computer/custom tools；
- 不读取 encrypted/hidden reasoning 作为业务内容；
- 没有 provider/model failover。

## 6. Prompt 与 working context

### 6.1 PromptBuilder 输入

PromptBuilder 只接收已验证类型：

```text
AnalysisContext summary
current HydrologyNode/geometry summary
time window plan
requested/supporting ParameterKeys
EvidenceRulePlan
current EvidenceBundle manifest
bounded fragment summaries/references
cell gates and missing/blocker facts
allowed tool descriptors
phase-specific output schema
```

它不得读取 provider SDK objects、raw HTTP envelopes、arbitrary dict、archive code 或前端文本拼接模板。

### 6.2 Working context

session transcript 是事实源，但模型 working context 是有界投影，不是每轮把整个 transcript 全量重发。投影包含：

- 固定 system prompt 和 task identity；
- 当前有效 rule/bundle/gate digest；
- 最近 Agent 可见 messages；
- tool result 的 bounded preview + refs；
- 已验证 batch outputs 的 summary/ref；
- 尚未解决的 cells/budget。

完整 source records 仍通过 inspection tool 按需读取。working context projection 必须可由 transcript refs 和当前 bundle 重建，并在 model request typed item 中保存实际发送内容或 content reference；不能从 UI preview 反推模型输入。

### 6.3 Prompt 分层

四类 prompt 使用共享 immutable base policy，再追加 phase instruction：

- evidence planning：选择合法 tools/append rules 或宣布 ready；
- reasoning batch：只回答给定 supported cells；
- overall synthesis：只消费 validated cell results 与 coverage facts；
- repair：只修复列出的机械错误。

完整 policy 见 [M6 Reasoning Policy Catalog](m6_reasoning_policy_catalog.md)。

## 7. Evidence-planning Agent loop

### 7.1 初始 acquisition

runner 调用 M5 acquisition service 取得初始 handoff。现有 `acquire(optional_requests=...)` 需要收敛为可迭代 API，例如：

```text
start(scope) -> EvidenceAcquisitionHandoff
execute_optional(scope, requests, prior_bundle_ref) -> handoff
append_rules(scope, proposals, prior_bundle_ref) -> handoff
seal(scope, bundle_ref) -> SealedEvidenceInput
```

这不是复制 M5 executor；每个方法仍使用相同 registry/executor/writer/materializer/bundle builder。

### 7.2 Planning turn

每轮请求只暴露当前合法 roster。response 可以：

1. 发出至多 `max_tool_calls_per_turn` 个 M5 tool calls；
2. 返回 append-only rule proposals；
3. 返回 `evidence_ready`；
4. 返回可见说明但没有合法 action，此时视为 invalid planning response。

tool calls 按 provider 输出顺序逐个授权与执行。任一 cancel 后停止后续 calls。一个 call 失败不会自动停止 loop；下一轮看到安全错误和 blocker，由 policy 判断是否还有合法路径。重复 exact request 继续由 M5 single-flight/dedup 处理，但仍受预算控制。

### 7.3 收敛

以下任一进入 final seal：

- Agent 返回 valid `evidence_ready`；
- 没有 remaining legal tools/rules；
- planning turn 上限达到；
- M5 tool budget 已用尽。

达到预算不是 permission to guess。seal 后按现有 evidence 形成 gates；无支持的 cells 走 indeterminate。若预算到达时仍存在应该执行但未执行的 conditional-mandatory action，则是 technical blocker，不得标 empty。

## 8. Sufficiency 与 batch plan

### 8.1 SealedEvidenceInput

```text
SealedEvidenceInput
  bundle_ref
  rule_plan_digest
  capability_registry_digest
  cells[]
  gates[]
  fragment_allowlist[]
  source_record_allowlist[]
  tool_result_refs[]
  planning_summary
```

seal 后禁止再追加 evidence 或 rule。reasoning batch 若请求 tool call，adapter/application 以 `reasoning_tools_not_allowed` 拒绝并进入结构错误处理；不能重新打开 acquisition。

### 8.2 Gate 结果

```text
ReasoningCellGate
  cell_id
  parameter
  window_id
  target_status
  discharge_status
  decision: supported | unsupported | blocked
  allowed_evidence_refs[]
  contextual_refs[]
  reasons[]
```

supported 不强制 quantified，只授予 Agent 推理权限。unsupported 产生 business indeterminate。blocked 的 session 级收敛按 §12。

### 8.3 Batch planner

排序键：

```text
(parameter order in AnalysisContext, segment order, window start, cell_id)
```

默认限额：

```text
REASONING_BATCH_MAX_CELLS=32
REASONING_MAX_BATCHES=24
```

batch 不跨 ParameterKey 或 `gap_filling/prediction` segment。达到 32 cells、预计 serialized bytes/output budget 或 segment 末尾时封口。若当前合法输入在最多 24 batches 内无法容纳，启动前以 `reasoning_plan_too_large` 失败；不能静默丢 cells 或增大 provider context。

## 9. Draft validation 与 assembly

### 9.1 Cell draft

```text
ReasoningCellDraft
  cell_id
  outcome_status
  representative_value
  target_unit_id
  source
  trend
  risk_level
  risk_rationale
  confidence
  confidence_rationale
  aggregation_method
  evidence_refs[]
  explanation
```

validator 验证：

- cell 属于 batch 且恰好出现一次；
- refs 是 gate allowlist 子集；contextual refs 不能单独满足输出；
- quantified/directional/indeterminate shape；
- unit、value、aggregation/source eligibility；
- observed 一对一 direct record closure；
- allowed-indirect-only 不 quantified；
- risk/confidence enum/range/rationale；
- 所有 required fields 严格存在，未知字段拒绝。

validator 不判断自然语言是否“科学正确”，不重算 risk/confidence，也不实现跨 series 冲突阈值。

### 9.2 Repair

首次 validation issues 规范化为稳定 codes/path，不包含 provider body、secret 或内部 traceback。repair request 固定原 batch/scope/schema，无 tools，最多一次。修复结果必须重新通过全部验证，不只检查旧 issues。

### 9.3 Skeleton assembler

assembler 合并：

- validated supported-cell drafts；
- deterministic unsupported/blocked indeterminate cells；
- backend-computed availability/counts/refs；
- validated synthesis risk/confidence/rationales；
- AnalysisDetail/AttributionSummary；
- reference-complete EvidenceChain；
- model execution summary。

最终 `InferenceResult` 使用 domain constructor 做第二层完整性验证。任何不一致是 `inference_assembly_failed`，session failed，不通过 serializer 补字段。

## 10. Risk、confidence 与 attribution

### 10.1 Agent 责任

Agent 判断：

- cell risk/confidence；
- parameter-series confidence；
- per-window composite risk/confidence；
- range overall risk/confidence；
- risk/confidence rationales；
- analysis steps 和 attribution factors。

### 10.2 后端责任

后端确定：

- availability、counts、missing/blocker refs；
- fixed indeterminate risk/confidence/reasons；
- evidence/result reference closure；
- source/observed eligibility；
- window/parameter completeness；
- identity、timestamps 和 lifecycle linkage。

### 10.3 Attribution

每个 `AttributionFactor` 保持：

```text
factor
direction
magnitude
basis: observed | correlated | inferred | hypothesized
explanation
evidence_refs[]
result_cell_refs[]
```

`hypothesized` 因素可以出现在 explanation/attribution，但不能单独解锁 gate 或 quantified result。所有 factor refs 必须进入 final EvidenceChain/result closure。

## 11. Transcript、session SSE 与持久化

### 11.1 Typed contents

M6 注册：

```text
waterbody_agent_input v1
waterbody_model_request v1
waterbody_agent_message v1
waterbody_model_response v1
reasoning_decision v1
inference_result v1
```

M5 evidence content types 继续复用，不复制。精确 public projection 见 [M6 API](m6_single_agent_reasoning_api.md)。

### 11.2 Streaming messages

每次 Agent 可见 message 在第一个 delta 前 reserve item identity，并使用 coordinator 已有增量 writer：

```text
message_started
message_delta*
message_finalized(completed | incomplete)
```

正常完成、refusal、content filter、timeout、error、cancel 和 truncated 都必须 finalize 同一个 message item。取消后 late chunks 只进入 M3 `late_message_output` audit，不能继续修改正文、working context 或 result。

### 11.3 Write order

关键提交顺序：

1. model request item；
2. streaming visible message（如有）；
3. raw visible model response/tool call item；
4. tool call/result/evidence items 或 reasoning decision；
5. validated draft decision；
6. final inference result；
7. session completed lifecycle item，引用 result。

result 和 completed event 使用统一 writer 的同一必需提交批次或等价原子边界，不能完成事件指向不存在的 result。保存失败继续走 M3 run persistence 补写；session business status 不增加 persistence 状态。

## 12. 失败与取消矩阵

| 情况 | Cell/result | Session |
|---|---|---|
| station-backed preflight completed but all empty/invalid-for-use | 全 fixed indeterminate | completed |
| stationless planning 后自然无 direct/allowed-indirect | 全 fixed indeterminate | completed |
| optional tool failed，但其他 gate supported | 受影响 refs/blocker 保留；supported cells 继续 | completed（availability 可 partial） |
| 部分 windows technical blocked，部分 supported | blocked windows fixed indeterminate；其余推理 | completed（availability partial） |
| required acquisition failure 导致全部 cells blocked | 不发布 final result | failed |
| waterbody model unavailable/timeout/content filter | 不发布 final result | failed |
| structured response repair 后仍无效 | 不发布残缺 result | failed |
| cancel 在任一阶段胜出 | 不启动后续操作，不发布 result | cancelled |
| persistence 暂时失败但业务 result 已确定 | live result 可见并带 run persistence warning | completed；run 继续补写 |

失败后已产生 transcript 不删除。M7 将 failed/cancelled 作为 dependency outcome；M6 不定义下游继续策略。

## 13. Budget 与配置

建议增加严格配置：

```text
WATERAGENTS_REASONING_MAX_PLANNING_TURNS=6
WATERAGENTS_REASONING_MAX_TOOL_CALLS_PER_TURN=4
WATERAGENTS_REASONING_BATCH_MAX_CELLS=32
WATERAGENTS_REASONING_MAX_BATCHES=24
WATERAGENTS_REASONING_MODEL_TIMEOUT_SECONDS=90
WATERAGENTS_REASONING_SESSION_DEADLINE_SECONDS=600
```

约束：

- integer/float 使用 strict validation，未知 env 继续 fail startup；
- planning turns 不含 reasoning/repair/synthesis；
- M5 collection/transform budgets 仍是 tool 权威上限，M6 不复制或提高；
- 每个 structured exchange 最多一个 repair，硬约束不可调大；
- 最大模型调用数可由 `planning + 2*batches + 2` 推导，默认最坏为 56；实际 usage 写 transcript；
- deadline 到达后不启动新 operation，在当前 operation 返回/取消边界收敛；
- token limits 从 model catalog 中精确匹配的 `ModelSelection(provider, model)` 能力取得，adapter 只提供 transport 与 size estimator；batch planner 必须在调用前验证预计 request/response fit。

M9 settings API 可以公开安全 metadata，但不能让普通请求提高 hard limits 或覆盖本次 run 已固化模型。

## 14. Runtime composition

`build_m6_runtime(settings, m3_runtime, m4_runtime, m5_runtime)` 返回：

```text
M6Runtime
  runner
  model_port_router
  limits
```

router 按完整 `ModelSelection(provider, model)` 读取 model catalog 中的 role 与 token limits，再选择 provider adapter；同 provider 的不同模型可以具有不同容量。无 adapter、无 credential、缺少容量或 model 不可选均在真正 session 启动前安全失败。错误只能包含 provider/model identity 和安全 code，不能回显 credential。

production app 不增加 M6 direct route。test composition 可以直接注入 fake ports 调用 runner，不通过 query/header 开关改变 production behavior。

## 15. 测试策略

### 15.1 Domain

- RiskLevel `none` 与 assessment availability；
- quantified/directional/indeterminate 全 shape；
- indeterminate source null/confidence 0；
- observed 一对一、aggregate inferred、prediction predicted；
- planned windows/parameters 完整；
- composite/overall counts/refs/availability；
- evidence/result/analysis/attribution closure。

### 15.2 Prompt/policy

- system prompt 包含风险 rationale、confidence、series 差异、unit/tool、gate、no-risk 与 absence-of-evidence 规则；
- phase prompt 不能授予多余 tools；repair prompt 不含 tools/new evidence；
- exact prompt/request 脱敏、registry/rule/bundle digest 固定；
- archive 或 provider-private fields 不进入 prompt。

### 15.3 Agent loop

- station-backed full empty 0 model calls；
- station-backed partial evidence 进入 optional planning；
- stationless 无上游时仍可 planning weather/facility；
- required-rule attempt 在 Agent 前执行；
- scope violation、重复 call、budget、append rule、materialization 和 reseal；
- all unsupported 0 final reasoning/synthesis；
- partial supported 只 batch supported cells。

### 15.4 Batch/validation/repair

- 最大 6 参数、15 当地日期、6h windows 的 deterministic batch count/order；
- DST、gap/prediction segment boundary；
- missing/duplicate/extra cell、未知 ref/unit/method、非法 observed、indirect quantified；
- risk/confidence/rationale 缺失；
- 首次 invalid + repair success；repair invalid -> failed；
- semantic disagreement 不触发 repair；
- successful batches 不拼成 partial final result。

### 15.5 Streaming/transcript/persistence

- visible delta 跨 chunk 脱敏；
- message identity/sequence/time 从 started 到 finalized 不变；
- tool/decision/result correlation operation IDs；
- 首次订阅回放 + live handoff、Last-Event-ID、去重、stream_end；
- cancel 固定 incomplete 正文，late output 只审计；
- result + completed reference closure；
- inline/large content、history reload、restart/service_interrupted、补写。

### 15.6 Adapter/集成

- checked-in OpenAI Responses fixtures：message/tool/structured output、usage、refusal、incomplete、content filter、malformed/unknown item；
- fake end-to-end single-waterbody runner 覆盖三态、两个 station 起点、risk/confidence、repair 和 session SSE；
- 专用 PostgreSQL persistence/history/restart；
- M1–M5 domain/application/API/regression 与 architecture imports；
- production OpenAPI 无 direct reasoning/tool route；
- Windows/Linux/macOS CI；缺少 provider credential 的真实 smoke 记录 `not run`，不算 pass。

## 16. M7/M8/M9 接力

M7 只需：

- 为 ready DAG node 创建 waterbody session；
- 实现 `UpstreamResultPort` 与直接上游 dependency manifest；
- 传入相同 `RunSingleWaterbodyAgent`；
- 等待 dependency outcome 并决定下游调度；
- 在 stationless 无 usable upstream 时复用现有 M6 skeleton/assembler 生成 0-model 不足结果；
- 保存 target/run-level result refs，并执行独立 `run_summarizer` session。

M8 实现 `TranscriptEvidencePort`；M6 prompt/policy 不因 memory mode 改写历史 evidence 的 contextual eligibility。

M9 消费最终 result summary/ref 和 session transcript/SSE，不新增对 prompt、raw provider response 或 direct reasoning endpoint 的依赖。

## 17. 完成标准

M6 未来实现只有同时满足以下条件才可标记完成：

- 单 waterbody runner、两种 station 起点、planning loop、sealed gate、batch/synthesis/assembler 全部接入；
- risk/confidence/rationale、composite/overall 与 current V1 Domain 原子收敛；
- model adapter、repair、failure/cancel 和 secret boundary 通过 focused tests；
- 真实 session transcript/SSE 可回放 Agent messages/tools/decisions/result；
- lifecycle completed/history summary/reference closure 与 PostgreSQL reload 通过；
- M1–M5 regression、architecture、OpenAPI、smoke 和 `git diff --check` 通过；
- provider 与三平台未运行项按实际记录 `not run`/`partial`，不虚报；
- 文档存在本身不构成 implementation completion。
