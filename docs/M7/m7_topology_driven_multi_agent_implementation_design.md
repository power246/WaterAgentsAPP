# M7 Topology-Driven Multi-Agent Workflow 实施设计

更新日期：2026-10-03

本文把 [M7 总体设计](m7_topology_driven_multi_agent_design.md) 映射到当前后端代码。它是 implementation design，不是完成声明或 TODO 清单。

## 1. 当前接入点

当前 production 已具备：

- `QueryTopologyWorkflow`：解析 query、保存唯一 `AnalysisContext`，构建并保存 `HydrologyGraph`；
- `RunCoordinator`：run/session 生命周期、transcript、取消、两层 SSE、保存补写与 `workflow_quiesced`；
- `M5Runtime`：scope-safe evidence acquisition、`UpstreamResultPort` producer slot、bundle/gate/materializer；
- `M6Runtime`：task validator、`RunSingleWaterbodyAgent`、结果装配与 transcript adapter；
- `HydrologyGraph.execution_layers`：稳定覆盖全部 active nodes，participating edge 上游严格早于下游，最后一层只有 target。

当前缺口是 `QueryTopologyWorkflow` 保存 `topology_ready v1` 后返回，因此 coordinator 会提前完成 run；M5/M6 runtime 虽已装配但没有 production workflow caller。M7 必须接在同一 workflow task 内。

## 2. 模块与依赖方向

建议在现有分层中增加以下职责，不创建按 milestone 命名的业务孤岛：

```text
app/application/analysis_runs/
  topology_driven_workflow.py       # parser -> topology -> DAG -> summary -> run result
  topology_scheduler.py             # ready queue、dependency counters、bounded tasks
  topology_result_models.py         # run-owned result、node outcome、coverage projection
  topology_result_assembler.py      # refs-only run result assembly/validation

app/application/evidence_tools/
  upstream_result_store.py          # run-local outcome index + UpstreamResultPort
  upstream_projection.py            # usable cell -> UpstreamResultRecord

app/application/run_summary/
  models.py                          # report card、prompt input、strict draft/result
  prompt_builder.py                  # simple-language summary policy renderer
  runner.py                          # one provider call + one repair at most
  transcript_store.py               # internal model audit + validated summary publish

app/infrastructure/runtime/
  m7.py                              # M4 stages + M5/M6 + scheduler + summarizer composition
```

依赖只允许：workflow/scheduler -> M6 service、upstream store、summary runner、`WorkflowOperations`。M5/M6/domain 不得反向 import scheduler 或 HTTP 层。API 只读取 coordinator/transcript projection，不直接驱动节点。

## 3. 核心类型

### 3.1 Dependency outcome

```text
WaterbodyDependencyStatus
  usable | insufficient | invalid | cancelled

WaterbodyDependencyOutcome
  run_id
  target_comid
  session_id
  status
  result_ref?                 # usable/insufficient completed session
  result_summary?             # small existing projection
  usable_cells[]              # parameter + window + result/cell refs
  discharge_cells[]           # required-support state per window
  error_ref?                  # invalid only; safe ref, not downstream evidence
  completed_at
```

不变量：

- `usable` 必须来自 completed session、具有 result ref，并至少有一个 quantified/directional eligible cell；
- `insufficient` 必须来自 completed result，eligible cell 集为空，所有相关 cells 为 indeterminate；
- `invalid` 必须来自 failed session，没有 result/evidence projection；
- `cancelled` 没有可传播 result，run cancel 后不得被下游消费；
- 同一 run + COMID 只能原子提交一个 outcome，重复同 bytes 可幂等返回，不同内容冲突。

### 3.2 Dependency manifest

每个实际执行的水体保存一个 session-owned `waterbody_dependency_manifest v1`：

```text
WaterbodyDependencyManifestV1
  schema_version = 1
  run_id
  session_id
  target_comid
  topology_ref
  dependencies[]
    upstream_comid
    upstream_session_id?
    dependency_status
    result_ref?               # only usable; insufficient ref stays audit-only
    usable_cell_refs[]
    discharge_states[]
  manifest_digest
```

manifest 的 dependency COMIDs 必须与 participating direct-upstream set 精确相等且稳定排序。`invalid`/`insufficient` 不携带 evidence refs；内部错误不进入 manifest。该 manifest 是 prompt 中 `invalid`/不足状态的唯一来源，不能从日志文本猜测。

### 3.3 Run result

新增 run-owned `topology_run_result v1`：

```text
TopologyRunResultV1
  schema_version = 1
  run_id
  analysis_context_ref
  topology_ref
  target_comid
  target_result_ref
  target_result_summary
  graph_coverage
  node_outcomes[]
    target_comid
    session_id
    dependency_status
    result_ref?
    result_summary?
  outcome_counts
  summary
    status: completed | failed
    session_id
    result_ref?
    public_error?             # failed only
  generated_at
```

`node_outcomes` 精确覆盖 active COMIDs，并按 graph stable order 排列。它只保存小型 projection/ref，不复制 `InferenceResult`。target 必须有 completed result；target technical failed 时不生成正常 topology result，由 workflow 失败路径处理。

### 3.4 Summary report card 与 result

```text
WaterbodyReportCard
  target_comid
  session_id
  graph_position
  direct_upstream_comids[]
  dependency_status
  result_ref?
  mode
  overall_risk
  availability
  confidence
  key_reasons[]
  window_summaries[]
  notable_findings[]
  model_execution_summary

RunSummaryInputV1
  run_id
  query
  target_comid
  mode/time_range/timezone
  graph_coverage/limit_facts
  target_result_projection
  report_cards[]
  allowed_source_refs[]
  input_digest

RunSummaryDraftV1
  plain_language_summary
  highlights[]
    kind
    text
    source_ref
    display_value?
    display_unit?
    window_id?

RunSummaryResultV1
  run_id/session_id
  input_digest
  model
  plain_language_summary
  highlights[]
  usage?
  generated_at
```

report cards 精确覆盖全部非 target active nodes，target 由完整安全业务投影单独代表；两者合并后每个 active node 恰好出现一次。字符串、数组和 window summaries 均有硬上限；builder 必须先用所选 model capability 验证完整输入 fit。若完整输入无法在不丢节点的前提下装入，summary session failed=`summary_input_too_large`，不能静默删除上游。

highlight 的数值必须精确匹配 source result 中同 ref 的 decimal/unit/window；后端不接受 Agent 改写显示值。非数值 highlight 也必须引用一张输入 card 或 graph coverage fact。

## 4. Workflow 组合

最终 workflow 使用一个 `TopologyDrivenAnalysisWorkflow`：

```text
understand query
  -> save AnalysisContext
  -> build/save HydrologyGraph
  -> execute active DAG
  -> build report cards
  -> run run_summarizer once
  -> save topology_run_result
  -> return to RunCoordinator
```

不得让 coordinator 在 `topology_ready` 后完成 run，也不得由 HTTP request、startup hook 或 `app.state` 上的隐藏 background task再次启动 DAG。

当前 composition 存在 workflow 先于 coordinator、M5/M6 创建的顺序。M7 应使用 startup-only deferred binding：

1. 创建 query/topology stages 与一个尚未接流量的 workflow facade；
2. 创建 coordinator/transcript reader；
3. 创建 run-local upstream store 并注入 M5 runtime；
4. 创建 M6 runtime、summary runtime 与 scheduler；
5. 将完整 execution stage 一次绑定到 facade；
6. 绑定成功后应用才开始接收请求，缺任一依赖则 startup fail closed。

facade 不允许运行中替换依赖，也不从全局 mutable service locator 查询 runtime。

## 5. Scheduler 算法

### 5.1 初始化

从保存后的 graph 构造只含 active nodes/participating edges 的不可变 scheduler view：

- `remaining_dependencies[comid]`；
- `direct_upstreams[comid]`、`direct_downstreams[comid]`；
- stable graph order 与 layer index；
- node state：`waiting | ready | claimed | terminal`；
- terminal `WaterbodyDependencyOutcome` map。

重新验证：active node 精确出现一次、target 最后一层、edge endpoint 存在、无环、每条 participating edge 上游 layer 更早。任何 mismatch 是 `topology_execution_invalid` run-level fatal error，不删边继续。

### 5.2 Ready queue

初始 `remaining_dependencies=0` 的 active nodes 进入 priority queue，key 为 `(layer_index, graph_order, comid)`。每次 outcome 原子提交后，仅对其 participating direct downstream 递减一次 counter；counter 到 0 时构建完整 dependency manifest，再决定真实 Agent 或 deterministic-zero-model path。

queue pop 与 session creation 之间必须在 run cancellation lock 内 claim node。取消先胜出时不得创建 session；claim 先胜出则 session 成为已创建未终态对象，由 run cancel 通知。

### 5.3 有界并行

使用 workflow-owned semaphore，配置建议：

```text
WATERBODY_WORKFLOW_MAX_CONCURRENCY
  strict int
  default 4
  min 1
  max 32
```

并发上限涵盖一个 waterbody session 的 M5+M6 完整执行，不只包围单次 model call。首版单 worker；多 run 共享同一进程 limiter，避免每个 run 各自启动上限数量。公平排队使用稳定 run created order + node priority；不得承诺跨 worker 公平性。

完成顺序只影响真实 transcript sequence，不影响结果数组顺序、digest 或 dependency scope。

### 5.4 节点执行

```text
claim node
  -> cancellation checkpoint
  -> create WATERBODY session
  -> save dependency manifest
  -> build/validate EvidenceSessionScope + WaterbodyAgentTask
  -> mark session running
  -> call the same M6 runner
  -> read coordinator-returned canonical SessionSummary
  -> classify dependency outcome
  -> publish outcome to upstream store
  -> unlock direct downstreams
```

`WaterbodyAgentRunOutcome` 不是最终生命周期权威；必须使用 coordinator 已提交的 session summary/result ref，防止 cancel 或保存竞争后传播未持久化结果。

节点 failed/cancelled 作为值返回 scheduler，不能直接抛出到 `RunCoordinator._run_workflow`。只有 graph invariant、outcome store corruption、target technical failure 或无法形成 run result 等 run-level fatal condition 才抛 `WorkflowFailed`。

## 6. M5 acquisition 接力修订

当前 M5 acquisition 对 station-backed 只执行本地 preflight并在全空时停止；对 stationless upstream 完成后总是允许 planning。M7 接入需原子收敛为 D03：

1. 有 station refs 时确定性执行本地 target + discharge preflight；
2. 有 participating direct upstream 时确定性执行真实 `get_upstream_results`；
3. 构造 dependency manifest，并只把 `usable` upstream records 加入 evidence fragments；
4. station-backed 仅在本地 target/support 能形成合法目标起点，或 usable upstream 能与本地 discharge 等 supporting evidence 形成登记关系时，才允许 required-rule attempts/Agent planning；stationless 必须 `has_usable_upstream`；
5. 没有可支持目标判断的起点时 `stop_before_model=true`，生成明确 stop fact；
6. station-backed 本地目标缺失但 local discharge/upstream 可支持 relation 时不得提前短路。

应扩展既有 acquisition/handoff 逻辑，不复制一个 M7-only bundle builder。`stop_before_model` 仍由 M6 现有 skeleton/assembler 消费，因此零模型路径不复制结果装配。

建议 stop facts：

```text
no_usable_local_or_upstream_evidence
upstream_dependencies_insufficient
```

它们是业务覆盖事实，不是 provider technical blocker。

## 7. 真实 UpstreamResultPort

### 7.1 Store 与发布

`RunLocalUpstreamResultStore` 同时承担：

- scheduler 写入/读取 dependency outcome 的内部 repository；
- M5 `UpstreamResultPort.collect()` adapter；
- run cleanup 时清理 live index。

outcome 只有在 coordinator 完成/失败/cancel session 原子提交后才能 publish。completed result 的详细内容按原 `result_ref` 经 trusted reader读取并验证 run/session/COMID；不得信任 scheduler 临时对象中的任意 result。

### 7.2 Collect authorization

`collect(request)` 必须验证：

- request run/session/agent COMID 与当前 sealed evidence scope 一致；
- `direct_upstream_comids` 与 graph participating direct upstream set 精确一致；
- 每个 upstream outcome 已 terminal；
- 参数/窗口是本次 scope 的子集且非空；
- result ref 属于同 run、对应 upstream session，session completed；
- closure refs 均属于该 result 的验证闭包。

scope/shape 非法在 port 前失败，保持 M5 `0 call / 0 result` preflight；真实读取后的错误保留 typed call/result audit。

### 7.3 Record 投影

只为 upstream result 中 quantified/directional 且命中请求参数/窗口的 points 产生 `UpstreamResultRecord`。全 indeterminate、failed、cancelled 不产生 records。

记录继续携带 upstream session/result refs、confidence、result kind/source 与 closure refs。进入当前水体后，materializer 将跨水体适用性限制为 capability relation 的最高强度；原 upstream `observed` 不使 downstream 结果成为 `observed`。

port 返回完整 `ProviderCollectionBatch` 和 completion proof。空 records 只表示所请求的已收敛 dependencies 没有 usable points；producer unavailable、scope mismatch、corrupt result 或不完整读取不得伪装为空批次。

## 8. Mode、window 与 gate

每个 node scope 从同一个 `AnalysisContext` 构造：

- `mode`、time segments、analysis timezone；
- final resolution/windows；
- requested parameters 与 required-support discharge；
- baseline + appended rule plan；
- memory mode 与 model selection。

dependency usability 按 parameter × window 计算，branch-level `usable` 只是“至少一个 eligible point”的调度投影。最终每个 cell 是否 supported 仍由 M6 sealed gate 判定：target 与同窗 discharge 都必须有 direct 或 materialized allowed-indirect evidence。

`gap_filling/prediction/mixed` 不在节点间重新推导。prediction upstream point 保持 predicted；跨水体 materialized result 至少 inferred，不能降格为 observed。

## 9. Summary runner

### 9.1 Session lifecycle

当 scheduler 已获得全部 active node outcomes、target completed 且 cancellation 未胜出：

1. 创建唯一 `RUN_SUMMARIZER` session；
2. 保存 `run_summary_input v1`；
3. mark running；
4. 用 run 固化 `waterbody_model` 发出一次 strict structured request；
5. schema/refs validation 失败时最多使用一次同模型、同 scope repair；
6. 成功保存 `run_summary_result v1` 并 completed；
7. provider/timeout/ref/schema repair failure 时保存 error 并 failed，不生成替代 summary。

summary 不注册 tools。最大模型调用数为 2（一次主调用 + 至多一次结构修正），不是多轮对话或分批总结。

### 9.2 Prompt 输入

builder 先从已保存 facts 生成 canonical report cards，不从 UI preview 或自然语言 transcript 抽取。target 使用完整安全业务投影；其他水体各使用一张 card。cards 按 graph execution order 排列，target projection 固定放在输入末尾；合并后每个 active node 恰好出现一次。

输入允许：query、mode/time、topology edges/coverage、results、availability/risk/confidence、safe reasons、notable findings 与 refs。禁止：credentials、provider envelope、internal error、traceback、raw tool payload、完整 transcript、hidden reasoning。

### 9.3 输出验证

validator 必须：

- strict reject unknown/missing fields；
- 验证每个 highlight ref 在 `allowed_source_refs`；
- 数值/unit/window 与 ref 对应原 result 完全一致；
- 禁止出现输入不存在的 COMID、风险、置信度或结论；
- 禁止内部 error/provider/secret 文本；
- summary language 跟随用户 query 的主要语言，并使用面向普通用户的简洁表达；
- 不要求或保存 hidden chain-of-thought。

validator 不评价自然语言写得是否“足够好”，只守结构、引用、安全和不伪造事实边界。

## 10. AgentRole 与 session result 扩展

当前 `AgentRole` 只有 `query_parser/waterbody`，`SessionSummary` 假设只有 waterbody completed 才有 result。M7 在当前唯一模型中原子增加：

```text
AgentRole.RUN_SUMMARIZER = "run_summarizer"
```

角色约束：

- query_parser：`target_comid=null`，无 session result summary；
- waterbody：`target_comid!=null`，completed 必须有 waterbody result ref/summary；
- run_summarizer：`target_comid=null`，completed 必须有 summary result ref/summary，failed/cancelled 不得有 completed result。

`SessionResultSummary` 应改为带 discriminator 的窄 union，而不是给现有 waterbody fields 加大量 nullable 字段。相关 domain、schema、persistence、reader、SSE、fixtures 和 OpenAPI 必须同一变更更新；不建立 V2/read union。

现有四表足以承载该 session，但 M3 的 `analysis_sessions` role/COMID CHECK 只允许 parser/waterbody，且缺少 summarizer 的 run 内唯一约束。M7 因此只增加一个窄 Alembic revision：扩展原 CHECK，并为 `agent_role='run_summarizer'` 增加按 `run_id` 的 partial unique index；不新增业务表或结果列。

## 11. AnalysisPhase 与 run snapshot

当前 phase 在 `topology_ready` 后直接 terminal。增加：

```text
waterbody_execution
run_summarization
terminal
```

phase 只表示当前 run 主阶段，不替代 session/node 状态。run snapshot 继续是完整轻量快照，增加：

- `result_ref?`；
- `summary_status: not_started | running | completed | failed | cancelled`；
- `summary_session_id?`；
- 每个 active waterbody 的 scheduling state、dependency status、session/result ref；
- compact outcome counts/coverage。

run SSE 不传完整 report card、summary 正文或 waterbody result。完整内容通过原 item ref/GET 读取。

## 12. 原子提交与终态顺序

正常完成顺序：

1. waterbody result 与 session completed 原子提交；
2. scheduler 发布 canonical outcome；
3. 全图收敛后 summary session terminal；
4. `topology_run_result v1` 作为 run-owned item 保存；
5. run completed lifecycle 与 result ref/summary projection 同一状态提交；
6. workflow 返回；
7. 等待 pending persistence 和 `workflow_quiesced` 后按 M3 关闭 run stream。

summary failed 也必须先保存 summary session failed，再保存带 `summary.status=failed` 的 topology result，最后 complete run。target failed 时不写正常 topology result，workflow 抛 typed fatal error，coordinator 失败 run。

若业务结果已确定但持久化暂时失败，沿用 M3 live +补写/`completed_with_persistence_error`；不得重新执行 Agent 作为保存重试。

## 13. Cancellation 与并发竞争

每个以下边界执行 cancellation checkpoint：queue claim 前、session create 前、M5/M6 调用前、outcome publish 前、summary session create/call 前、topology result commit 前。

规则：

- cancel 先胜出：不创建/不启动后续工作；
- session completed 先原子提交：保留 completed，但 cancel 后不向下游发布新 evidence；
- 无法强杀 model/HTTP：允许返回，新增正文进入既有晚到审计，不形成 result；
- summary 生成中 cancel：summary session cancelled，run cancelled，不写 topology result；已有 waterbody results 保留可读；
- semaphore/queue 等待者必须可被 cancellation 唤醒，不靠轮询 sleep。

## 14. Failure mapping

| 条件 | node/session | scheduler/run |
|---|---|---|
| 本地/上游自然无 usable evidence | completed + deterministic indeterminate | outcome insufficient，继续依赖收敛 |
| non-target provider/model technical failure | failed + PublicError | outcome invalid；其他分支继续 |
| target provider/model technical failure | failed | run failed |
| dependency result corrupt/scope mismatch | 当前 node failed | target 可达则 partial；target 失败则 run failed |
| summary provider/timeout/repair failure | summarizer failed | topology result 保存 summary failed；run completed |
| summary input 超出所选 model capacity | summarizer failed/non-retryable | run completed，无 summary text |
| graph/scheduler invariant 破坏 | 未启动新 node | run failed，停止调度 |
| cancel | 已创建非终态 session cancelled | run cancelled，等待 quiesced |

公开 `PublicError` 只含安全 code/message/stage/retryable。内部 URL、provider body、prompt、credential、response dump 和 traceback 仅进脱敏日志。

## 15. Typed transcript contents

新增注册：

| content_type | scope | 含义 |
|---|---|---|
| `waterbody_dependency_manifest v1` | waterbody session | 直接上游终态与可传证据边界 |
| `run_summary_input v1` | summarizer session | 实际发送前的 canonical summary 输入 |
| `run_summary_model_request v1` | summarizer session | history-only provider-neutral request；公开读取只返回安全投影 |
| `run_summary_agent_message v1` | summarizer session | validator 通过后一次性提交的用户可见浅显总结 |
| `run_summary_model_response v1` | summarizer session | history-only provider 审计记录；raw visible/structured output 不进入公开投影 |
| `run_summary_result v1` | summarizer session | validated summary + highlights + refs |
| `topology_run_result v1` | run | target/ref、node outcomes、coverage、summary status/ref |

全部类型 strict、canonical serialization、unknown field reject，并注册 reference extractor、安全 metadata、全文/preview、secret scan 和 public projection。完整正文只保存一份。

provider 的 visible delta/structured output 在校验前只属于内部候选与审计数据，不创建公开 message。`run_summary_agent_message` 与 `run_summary_result` 在同一 summary terminal writer group 中提交；取消、协议错误或校验失败均不留下公开 summary text。

## 16. 配置

M7 建议只增加：

```text
WATERBODY_WORKFLOW_MAX_CONCURRENCY=4   # 1..32
RUN_SUMMARY_TIMEOUT_SECONDS=120        # >0，且不超过 provider hard cap
RUN_SUMMARY_MAX_OUTPUT_TOKENS           # 从精确 model capability 取较小值
```

不新增 run-wide token budget、summary model、feature flag、per-request concurrency override 或普通用户可提高的 hard limit。topology distance/hops/nodes 继续是总范围权威。

## 17. 测试策略

### 17.1 Domain/assembler

- outcome 四态与字段组合正反例；
- manifest direct-upstream exact set、stable digest、无错误详情；
- topology result active-node exact coverage、target/ref、summary success/failure；
- report card/highlight 数值与 source ref 精确匹配；
- `RUN_SUMMARIZER` role/target/result invariants。

### 17.2 Scheduler

- 3-node chain、diamond、双支流汇合和 128-node bounded fixture；
- 同层 bounded parallel、stable ready tie-break、node exactly once；
- 一个 invalid + 一个 usable 仍执行 downstream；
- 全 insufficient stationless 走 0 model result；
- 全 insufficient 后遇 station-backed 仍执行本地 preflight；
- local target missing + local discharge + usable upstream 可进入 relation/planning；
- target technical failed 失败 run，non-target failed 形成 partial completed run。

### 17.3 Upstream authorization

- 同 run direct upstream positive；
- sibling、ancestor-not-direct、other run/session/COMID/window/parameter negative；
- invalid/insufficient 0 records 且 manifest 状态准确；
- corrupt/missing/incomplete 不是 successful empty；
- upstream observed 到 downstream 不成为 observed。

### 17.4 Summary

- target full projection + every-other-node card，合并后每个 active node exactly once；
- simple-language prompt policy、无 tools、固定 model selection；
- notable numeric value/ref/unit/window closure；
- invented value/ref/COMID/risk rejection + one repair；
- provider failure 无模板 fallback，session failed、run completed；
- oversized complete input 明确失败，不静默裁卡。

### 17.5 Cancellation/SSE/persistence

- queue wait、claim、session create、M5/M6、outcome publish、summary 与 final commit 每个 race；
- cancel 后 0 new session/tool/model/result/downstream propagation；
- completed session 终态不改，晚到消息审计不回流；
- run snapshot/selected session stream、reconnect、stream_end；
- topology result + run completed 原子性、保存补写不重跑；
- startup repair 将遗留 running sessions/run 按既有 service-interrupted 语义失败，不自动续跑 DAG。

### 17.6 Architecture/contract gates

- production 只有一个 topology-driven workflow caller；
- 无 direct reasoning/tool/summary route、无第三 SSE；
- 无 duplicate result/transcript store、无 M7 feature flag/read union；
- no archive imports、no `backend_bk`/`frontend_bk`；
- Windows/Linux/macOS 无硬编码平台路径；
- OpenAPI、typed registry、docs 与 serializer 字段一致。

## 18. 后续阶段接力

M8 只需实现 `TranscriptEvidencePort`；M7 scheduler、upstream store 与 summary cards 不读取其他 run 的 raw transcript。历史 evidence 仍经过现有 relation/gate。

M9 读取 `topology_run_result`、summary session/result、waterbody sessions 和稳定 refs；前端自行构造站内跳转。M9 不新增 summary route/SSE，不把 `run_summarizer.target_comid=null` 当损坏，也不把 partial coverage 显示为完整。
