# M6 Single-Agent Reasoning 设计决策记录

更新日期：2026-10-02

> 状态：设计方向、实施模式和 Milestone 0–13 已完成本机实现与集成审计。固定 fake、official provider fixtures、现有两层 SSE、专用 PostgreSQL 和完整 backend regression 已通过；真实 OpenAI waterbody provider 请求与 Ubuntu/macOS/Windows 同提交 CI 仍为 `not run` / `partial`。精确实现证据和剩余风险见同目录 Implementation TODO。

本文承接：

- [后端架构骨架](../backend_architecture_skeleton.md)
- [总 Milestones 的 M6 范围](../backend_refactor_milestones.md#8-m6-single-agent-reasoning)
- [跨 Milestone M6 契约](../deferred_milestone_contracts.md#8-m6single-agent-reasoning)
- [M3 Transcript / History 设计](../M3/m3_transcript_history_design.md)
- [M4 Query Understanding / Topology 设计](../M4/m4_query_understanding_topology_design.md)
- [M5 Evidence Tools 设计](../M5/m5_evidence_tools_design.md)
- [M5 实施设计的 M6/M7/M8 接力](../M5/m5_evidence_tools_implementation_design.md#13-m6m7m8-接力)
- [M6 实施设计](m6_single_agent_reasoning_implementation_design.md)
- [M6 Reasoning Policy Catalog](m6_reasoning_policy_catalog.md)
- [M6 API / 读取契约](m6_single_agent_reasoning_api.md)

## 1. 当前边界

M6 跑通一个已经存在的 waterbody Agent session 内的完整推理闭环：准备当前水体 scope、收集和检查 evidence、执行受约束的 Agent tool loop、只对通过门禁的 parameter-window cells 调用水体模型、验证结构化草稿、组装完整 `InferenceResult`，并把真实会话过程写入 transcript/session SSE。

M6 不负责：

- 遍历 active topology、等待多个上游或创建多个水体 session；这些由 M7 负责；
- 生产真实 upstream-result producer；M7 接入现有 producer slot；
- 跨 run transcript RAG；M8 接入现有 producer slot；
- 新前端、settings/key/admin 或第一版正式联调；M9 负责；
- 新增独立 reasoning HTTP endpoint、第三条 SSE 或 milestone feature gate。

M1–M9 仍是当前唯一 V1 的原子演进。M6 可以直接收敛现有 inference domain、lifecycle payload、transcript registry 和 runtime composition，不建立 V2、旧/新 read union 或中间产品兼容路径。

## 2. 已确认决策

### D01：M6 提供单水体 runner，M7 只负责调度同一个 runner

M6 的 production boundary 是一个 `RunSingleWaterbodyAgent` application service。调用方必须提供已经创建并准备执行的 waterbody session，以及与 run 固化配置一致的 `AnalysisContext`、当前 `HydrologyNode`、M5 scope/handoff 和 cancellation token。

一次执行从输入、evidence planning、tool calls、reasoning batches、overall synthesis 到 final result 始终属于同一个 `session_id`。模型和工具 operation 各自生成 `operation_id`，但不再生成同义 `agent_id`，也不因为 reasoning 分批而创建多个 session。

M6 的独立验收使用 fake session + fake M5 runtime + fake waterbody model 直接调用该 application service。production workflow 暂不遍历 topology；M7 只需在 active DAG 节点准备执行时创建 session、注入直接上游结果并调用同一 service。

### D02：station-backed 与 stationless 使用不同启动策略

station-backed 水体先由确定性 policy 执行 M5 `collect_water_observations` preflight，并同时查询当前水体适用的 target channels 和 required-support `usgs:00060` discharge。

若 preflight 成功完成，但全部适用窗口都没有 usable local direct evidence：

- 保存真实 tool calls/results、collection facts、fragments 和 bundle；
- 不进入 waterbody model tool loop；
- 后端为全部计划 cells 装配 `indeterminate/insufficient_evidence`；
- 模型调用数严格为 0；
- session 以真实 completed result 收敛，不伪造模型消息或 quantified value。

stationless 水体没有本地观测是正常起点，不得套用上述停止策略。它不注册、不调用本地 observation tool，而是先读取 M7 producer slot 的直接上游结果；无论上游证据是否已经足够，都允许在有界 Agent loop 中按需调用 weather、facility、inspection、aggregation 和 unit tools。M7 尚未接入 producer 时，M6 fake/contract 验收使用显式 unavailable producer，不用 hidden fake 冒充真实上游。

### D03：确定性 required acquisition 与 Agent optional planning 分层

进入 Agent loop 前，后端只自动执行已确认的确定性动作：

1. station-backed local observation/discharge preflight；
2. stationless direct-upstream producer read；
3. `EvidenceRulePlan.required_collection_attempt=true` 对应的有界必需尝试；
4. 已有 evidence 的 relation materialization 和 bundle/gate rebuild。

其余 optional evidence 由 Agent 根据任务、已有 evidence、缺失 cells 和 tool descriptors 决定。Agent 可以：

- 调用当前 scope 允许的 M5 tools；
- 在既有 baseline rules 之后 append 新 evidence rule proposal；
- 请求 inspection、确定性 aggregation 或 unit conversion；
- 声明 evidence acquisition 已收敛。

append rule 只能引用本次 capability registry 中的 ParameterKey，经过 server-side validation 后追加；没有删除或替换 baseline 的接口。Agent 不能直接把 contextual fragment 改成 allowed-indirect，不能传任意 COMID/station/URL，也不能提高 M5 budget。

### D04：每轮 tool 后重建 bundle，最终 gate 封口后才能推理

每轮 Agent tool calls 完成后，M5 重新保存新增 source/fragment/result，materialize 当前有效 relations，并生成新的 reference-only `EvidenceBundle` 与 cell gates。旧 bundle/item 保持不可变，后续 bundle 引用新增事实；不得原地修改已保存 evidence。

Agent 声明 evidence ready、达到 planning 上限或没有合法后续动作时，M6 执行最终 gate：

- identity 为 target `ParameterKey × final TimeWindow`；
- 同时检查 target cell 与同窗 discharge cell；
- direct 或当前 rule plan 允许的 allowed-indirect 才是 supported；
- contextual-only 不能解锁；
- unavailable/invalid/technical incomplete 为 blocked，不得冒充 empty；
- unsupported/blocked cells 不进入 reasoning batch。

如果全部 cells unsupported 且没有 technical blocker，后端以 0 次 final reasoning 调用装配完整 indeterminate result。stationless 在此之前可能发生 evidence-planning 模型调用；这些调用必须如实计数，不能被描述成整个 session 0 次模型调用。

### D05：后端建立结果骨架，Agent 只输出受约束的 reasoning draft

后端从 `AnalysisContext`、window plan 和最终 gates 建立唯一结果骨架，固定：

- run/session/result identity；
- target COMID、mode、timezone、time range/segments/resolution；
- requested ParameterKey 顺序；
- 每个 parameter-window cell identity；
- unsupported/blocked cell 的 deterministic result；
- supported cell 可引用的 evidence allowlist；
- source、unit、aggregation 和 applicability 的机械资格。

Agent 只为 supported cells 输出 `ReasoningDraft`。draft 可以判断 quantified、directional 或 indeterminate，给出风险、confidence、理由、evidence refs、analysis steps 和 attribution proposal，但不能改变 cell identity、创建额外窗口、引用 scope 外 evidence 或覆盖后端固定结果。

validator 通过后由 assembler 生成正式 `InferenceResult`。模型从不直接拥有 result/session identity、generated_at、固定 blockers、引用闭包或 lifecycle terminal event 的构造权。

### D06：一个 Agent session 内分批推理，最后做一次总体汇总

最多 6 个用户参数和最长 15 个当地日期在 6h 粒度下可形成数百个 cells。一次巨大结构化响应容易截断；一个 cell 一次调用又会放大成本。因此 M6 使用确定性 batch plan：

- 按 ParameterKey、time segment 和连续 final windows 排序；
- 单批只包含 supported cells；
- 同时受 cell count、序列化输入和预计输出预算限制；
- 顺序与 batch identity 可重现；
- batch 之间仍属于同一个 Agent session。

每个 batch 产生 validated cell drafts。全部 batches 成功后执行一次 overall synthesis，输入 validated results、固定 indeterminate cells、coverage/blocker facts 和有界引用摘要，不重新内联完整 raw evidence。synthesis 产生逐窗口综合风险、日期范围总体风险、overall/series confidence、`AnalysisDetail` 和 `AttributionSummary` 草稿。

如果没有 supported cell，则不存在 reasoning batch 或 synthesis 调用；全部总体对象由后端按固定 indeterminate 语义装配。

### D07：每个计划窗口完整输出三态结果

每个 requested parameter 的每个 final window 必须有且只有一个 point：

- `quantified`：有 representative value、合法 unit、source、confidence、理由和 evidence refs；
- `directional`：没有 representative value，必须有明确 trend 或 risk、confidence、理由和 evidence refs；
- `indeterminate`：没有 representative value，必须有 reason code、缺失/blocker refs 和固定或 Agent 理由。

仅 allowed-indirect evidence 的 cell 最多输出 directional，不能输出具体目标值。完全没有 direct/allowed-indirect evidence 的 cell 由后端装配 indeterminate。direct evidence 已通过 gate 也不强迫 Agent quantified：跨 series 差异、coverage 或任务语义仍可令 Agent 选择 directional/indeterminate。

`ResultSource` 只描述真实产生的判断来源：gap-filling quantified/directional 为 observed 或 inferred，prediction quantified/directional 为 predicted；indeterminate 不伪造 observed/inferred/predicted source。现有 domain 必须原子收敛该不变量。

### D08：风险等级由 Agent 判断，并必须给出推理简述

第一版风险等级固定为：

```text
none | low | medium | high | indeterminate
```

不建立任意 `0–100` 分数，也不把不同单位参数做算术平均。风险判断属于 Agent；system prompt 固定证据优先级、禁止事项、缺失处理和输出要求，后端不使用阈值、取最大值或平均值替 Agent 判断。

每个 Agent 产生的 parameter-window、window-overall 和 range-overall 风险等级都必须带 `risk_rationale`，说明：

- 使用了哪些 evidence/result refs；
- 这些事实怎样支持判断；
- 为什么选择该等级；
- 高风险、相反 series、缺失或 blocker 如何影响结论。

`none` 只能表达 Agent 在足够 evidence 下没有识别到风险。absence of evidence、collection blocked 或无法解释必须是 `indeterminate`，不能写成 none。validator 校验枚举、rationale 非空和引用闭包，不评价自然语言结论，也不重算等级。

### D09：逐窗口综合风险与日期范围总体风险同时存在

每个 final window 都有跨 requested parameters 的 `CompositeRiskAssessment`，与参数时间序列使用相同 start/end。每个水体另有覆盖整个 request range 的 `OverallWaterbodyAssessment`，用于地图/结果卡片 headline。

两层综合风险都由 Agent 判断并给出 rationale/confidence。后端机械计算独立的 availability：

```text
complete | partial | indeterminate
```

availability 保存 assessed/total counts、missing cell refs 和 blocker refs。风险与 availability 不得合并：`high + partial` 可以表示已经发现可信风险但覆盖不全；`low + partial` 只能描述已判断部分，不能对外宣称整个水体安全；全部 cells indeterminate 时总体风险与 availability 都是 indeterminate。

总体对象引用决定结论的 cell/result refs，不复制完整 parameter series。`overall_confidence` 仍只表达总体判断的 confidence，不能替代综合风险。

### D10：confidence 由 Agent 判断，固定无证据结果为 0

confidence 表达“对水质判断的把握”，与风险高低正交。Agent 为 supported cell、parameter series、window-overall 和 range-overall 分别输出 `[0,1]` confidence，并给出 `confidence_rationale` 与 evidence/result refs。

prompt 要求考虑 evidence directness、quality、coverage、时间匹配、series 一致性、推理跨度和缺失/blocker，但不提供后端打分公式，也不要求跨层级算术平均。validator 只检查范围、rationale、引用与 scope。

由后端固定生成的 `indeterminate/insufficient_evidence` confidence 为 0，表示无法对水质状况形成有把握的判断，而不是对“证据不足事实”的元置信度。

### D11：多 series 判断留给 Agent，observed 资格由后端严格验证

M5 只保留 series identity、quality、coverage、summary 与 inspection，不实现跨 series 冲突 detector。M6 prompt 要求 Agent 检查 station、time、method、medium/fraction、statistic、quality 与 coverage，引用所有影响判断的记录，并解释选择、分别保留、降低 confidence 或 indeterminate 的理由。

后端只强制 source 资格：

- 一对一采用当前水体/当前窗口的一条具体 direct record，可带登记且可逆的展示单位转换，才可 `observed`；
- 任意多记录 aggregate、跨 series 选择/折中、上游/天气/设施/历史关系或模型推理都是 `inferred`；
- prediction segment 的模型产出为 `predicted`；
- contextual-only evidence 只能用于解释，不能成为 quantified/directional 的唯一 gate refs。

### D12：单位、聚合和 scope 权限继续由确定性工具执行

Agent 可以在 capability policy 允许范围内选择目标 unit 和 aggregation method，但不能自行计算换算公式、混合 series、传任意 evidence IDs 或构造 provider request。

所有 convert/aggregate/inspect/collect 调用继续进入 M5 `EvidenceToolExecutor`，复用 server-side scope authorization、registry、budget、dedup、writer、error mapping 和 transcript types。模型返回的 tool arguments 在执行前以 strict DTO 验证；越权调用产生安全 tool error 并占用相应预算，不放宽为自由 JSON。

### D13：结构化输出最多修正一次，仍无效则 session failed

每个 reasoning batch 和 overall synthesis 的首次响应均经过完整 deterministic validation。只有 schema、字段、窗口、枚举、引用、单位/数值资格等机械错误可以触发一次 schema repair。

repair：

- 使用 run 固化的同一 waterbody provider/model；
- 输入原可见响应、精简脱敏 validation issues、原 schema 和相同 scope；
- 不调用 tools、不读取新 evidence、不改变 batch plan；
- 不因后端不同意风险/confidence/rationale 而触发；
- 不自动切换 provider/model。

repair 后仍无效时，整个 waterbody session 进入 failed。已经验证的 batches 和全部 request/raw response/repair/error 保留 transcript，但不发布残缺 `InferenceResult`、overall result 或正常 upstream result。

### D14：推理过程就是 session SSE 中的真实会话上下文

不新增 reasoning/progress SSE、摘要流或第三条事件流。用户看到的推理过程就是 waterbody Agent session 中真实发生的：

- Agent 可见消息实时文本；
- tool calls/results；
- evidence typed items；
- planning/batch/repair/synthesis decisions；
- final result 或安全错误。

Agent 可见消息沿用 M3 `message_started / message_delta / message_finalized`。其余 typed items沿用 `transcript_item`；session stream 的整数 cursor、回放、续传、去重、`stream_end`、取消与晚到输出不变。run SSE 仍只发送轻量完整状态快照，不复制 Agent 正文或完整结果。

raw model output 只指 provider 实际交给应用的可见消息、tool calls 和结构化响应。不请求、不保存、不展示 provider 未返回的隐藏思维链，也不合成第二份虚假“推理过程”。

### D15：技术失败、自然无证据和取消分别收敛

自然无证据是已完整执行 evidence acquisition 后的业务事实，可以完成 session 并返回 indeterminate。技术失败不能伪装为自然无证据：

- optional tool 失败但其他 target + discharge refs 足够时，可以继续；结果保留 unavailable fact；
- 某些 windows blocked、另有 supported windows 时，只推理 supported cells，blocked cells 以明确技术 reason indeterminate，overall availability 为 partial；
- required path 技术失败导致所有 cells blocked 时，不调用 reasoning model，session failed；
- waterbody model unavailable、timeout、content filter、invalid response after repair 均为 session failed；
- cancel 胜出后停止新 tool/model/batch/synthesis，当前可见消息以 incomplete 定稿，晚到输出只进入既有 cancelled audit path，不发布 result。

M6 不新增 lifecycle 状态。session 仍使用 pending/running/completed/failed/cancelled；保存进度仍属于 run persistence，不产生 completed_with_persistence_error session。

### D16：M6 没有独立 production API，扩展现有 final payload 与 history

M6 不公开 direct reasoning、prompt、tool invocation 或 result assembly HTTP route。production 只扩展：

- `session_completed` 的 lightweight real result reference/summary；
- waterbody session transcript typed contents；
- run/session detail 中的 final result reference；
- history summary 中已持久化的 overall risk、availability 和 overall confidence；
- 既有 transcript full-content read。

完整 `InferenceResult` 只保存一份并通过 transcript item/reference 读取；lifecycle、run snapshot 和 history summary 使用轻量 projection，不复制完整 series。

## 3. 已冻结的阶段责任

| 阶段 | 责任 |
|---|---|
| M5 | 提供 scope-safe evidence acquisition、tools、bundle、cell gates、unit/aggregation 和引用闭包；不作最终判断。 |
| M6 | 执行一个 waterbody session 的 Agent loop、最终 gate、分批 reasoning、风险/confidence、结果验证与装配，并复用 session SSE。 |
| M7 | 按 active DAG 创建/调度多个 waterbody sessions，注入真实直接上游结果，处理 dependency outcomes 并形成 run-level result。 |
| M8 | 接入 transcript RAG producer；历史 evidence 保持 contextual，不自动解锁 gate。 |
| M9 | 实现最终前端、settings/keys/admin 和端到端联调；消费 M6 已冻结结果与 session context。 |

## 4. 实施文档索引

| 主题 | 权威位置 |
|---|---|
| 模块、Domain/DTO、执行链、ports/adapters、batch、assembler、预算、错误与测试 | [M6 实施设计](m6_single_agent_reasoning_implementation_design.md) |
| sufficiency、tool、output、risk、confidence、series、repair 和失败规则 | [M6 Reasoning Policy Catalog](m6_reasoning_policy_catalog.md) |
| typed transcript、session SSE、completed payload、history projection、public errors 与 test composition | [M6 API / 读取契约](m6_single_agent_reasoning_api.md) |
| Milestone 0–13 工作项、focused tests、验证命令、阶段出口、提交边界与证据格式 | [M6 Implementation TODO](m6_implementation_todo.md) |
