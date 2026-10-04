# M7 Topology-Driven Multi-Agent Workflow 设计

更新日期：2026-10-03

本文冻结 M7 的产品与架构决策。实施模块、类型、执行顺序和测试矩阵见 [M7 实施设计](m7_topology_driven_multi_agent_implementation_design.md)，公开读模型与事件投影见 [M7 API](m7_topology_driven_multi_agent_api.md)，dependency 与汇总提示词规则见 [M7 Policy Catalog](m7_workflow_policy_catalog.md)，分阶段施工与验收边界见 [M7 Implementation TODO](m7_implementation_todo.md)。

## 1. 目标与边界

M7 将 M4 保存的 active `HydrologyGraph`、M5 Evidence Tools 和 M6 `RunSingleWaterbodyAgent` 接成一个真实 production workflow：

```text
AnalysisContext + saved HydrologyGraph
  -> dependency-aware ready queue
  -> bounded parallel waterbody sessions
  -> direct-upstream result handoff
  -> target waterbody InferenceResult
  -> one run_summarizer session
  -> one run-owned topology result
```

M7 负责：

- 只调度 active DAG，并在节点真正准备执行时创建唯一 waterbody session；
- 等待全部直接上游 dependency outcomes 收敛，再按后端规则决定执行或零模型收敛；
- 实现真实 `UpstreamResultPort`，严格限制为同一 run 的直接上游；
- 有界并行执行 M6 单水体 runner，捕获每个节点的真实终态；
- 形成 target 主结论、全图覆盖事实和 run-level 汇总；
- 把取消、晚到输出、保存与两层 SSE 接入既有 M3 边界。

M7 不负责：

- 改写 M4 query understanding、BFS 或 capability registry；
- 复制 M5 tool executor、M6 reasoning/gate/result assembler 或 M3 transcript store；
- 建立跨 run RAG（M8）或最终地图/settings/admin UI（M9）；
- 自动 provider/model failover、跨进程共享调度或多 worker 恢复；
- 新增 direct Agent/tool HTTP route、第三条 SSE 或隐藏 reasoning 字段。

## 2. 已确认设计

### D01：同一 production workflow 从 topology 一直运行到 run result

当前 `QueryTopologyWorkflow` 在保存 `topology_ready v1` 后即返回，`RunCoordinator` 随后完成 run。M7 必须把 DAG execution、target result、summary session 和 topology final result 接入这一条 owned workflow；不得从另一个后台入口旁路启动，也不得在 topology ready 时提前完成 run。

workflow 只读取已经保存并重新校验的 `AnalysisContext` 与 `HydrologyGraph`。active nodes/participating edges、execution layers、target、limits 和 coverage 均来自原图；M7 不重跑 BFS、不重新选择 station、不把 excluded node 改回 active。

### D02：ready 由直接依赖收敛决定，session 只在准备执行时创建

每个 active node 在同一 run 内最多执行一次。节点只有同时满足以下条件才可进入 ready queue：

1. 所有 participating direct-upstream edges 都已有终态 dependency outcome；
2. run 尚未取消且 workflow 未进入终态；
3. 节点尚未 claim、尚未创建 waterbody session；
4. D03 的 station/dependency 启动策略已经确定该节点走真实 Agent 或零模型结果路径。

DAG 建成时不预建全部 sessions。等待上游、排队中和取消前尚未执行的节点由 run-owned scheduling facts 表达；一旦节点准备实际取证或确定性生成完整结果，才创建并绑定该 COMID 的 waterbody session。

### D03：后端按 station 与 usable upstream 决定启动路径

启动规则由后端确定，不通过一次模型调用询问 Agent：

| 当前水体 | 本地/上游情况 | 行为 |
|---|---|---|
| 有合格 station refs | 先执行本地 target + required discharge preflight | 本地目标与支持证据足以形成合法起点时进入 M6 Agent loop；上游结果可补充缺失部分或作为辅助推理证据 |
| 有合格 station refs | 本地目标证据缺失，但至少一个直接上游有 usable result | 不截断；允许结合上游结果、本地 discharge/其他 station supporting parameter，以及按需天气/设施等间接证据继续规划 |
| 有合格 station refs | 没有可支持目标判断的本地起点，且 usable upstream result 不存在 | 保存真实 collection facts，零模型生成完整 `indeterminate/insufficient_evidence` result |
| 无合格 station refs | 至少一个直接上游有 usable result | 上游为必须起点，允许进入有界 planning 并按需取得本地间接证据 |
| 无合格 station refs | 全部直接上游均无 usable evidence | 不构建 Agent prompt、不调用 optional tools；按 D08 零模型生成完整不足结果 |

“本地 direct”相对于当前目标参数定义。本地 station 的 discharge 是真实本地监测记录，但在判断本地 pH 等其他参数时是 supporting/indirect evidence。它可以和上游 pH result 一起进入已登记的 evidence relation。

### D04：dependency outcome 与 scientific availability 分开

直接上游 outcome 至少区分：

- `usable`：存在与本次参数/窗口相关、可供后续 relation/gate 使用的 quantified 或 directional result；
- `insufficient`：session 正常 completed，但适用结果全部为 `indeterminate/insufficient_evidence`；
- `invalid`：上游因技术故障 failed，没有可传播的 final result；
- `cancelled`：run cancel 已胜出，只用于收敛和审计，不再启动下游。

`completed` 只是 session 生命周期，不自动表示 `usable`。汇流节点必须等待所有直接上游 outcome 收敛；只要还有一个 `usable` 分支，其他 `insufficient`/`invalid` 分支不阻断节点进入 D03 的后续判断。

### D05：失败分支在 prompt 中只显示 `invalid`，不传证据

若其他分支允许下游继续，failed upstream 在 dependency manifest 中只显示 `dependency_status=invalid`。不得增加“技术缺口”等新的科学概念，也不得注入内部错误详情。

`invalid` 分支不携带 result、evidence summary、confidence、discharge value 或 closure refs，不得改写为 completed empty、自然 `indeterminate`、数值 0 或 allowed-indirect evidence。详细 `PublicError`、原 transcript、operation 与脱敏结构化日志留在后端排查边界。

正常 `insufficient` 分支保留原确定性 result 供审计和汇总，但同样不向下游传 evidence；dependency manifest 可说明该分支无 usable evidence。

### D06：上游结果不重算，prompt 明确来源身份

M7 不为 upstream result 增加自定义评分、固定权重、阈值、数值换算或结论改写。真实 producer 传递既有安全结构化 result projection 与引用闭包，并保持：

- 仅同一 run；
- 仅当前节点的 participating direct upstream；
- 仅请求参数与窗口；
- 原 result/session/item identity 不变；
- quantified、directional、indeterminate 与 observed/inferred/predicted 来源不丢失。

prompt 必须说明：当前水体的合格同参数 station reading 才是本地 direct observation；上游结果即使来自上游站点的 observed reading，用于判断当前下游水体时也是跨水体 inferred evidence。Agent 决定如何解释，后端继续校验 scope、source、relation、gate 与引用闭包。

### D07：跨水体与普通间接关系最多支持方向性判断

upstream result 与本地 discharge、天气、设施等 supporting/indirect evidence 的普通组合，最多支持当前水体的 directional trend/risk。Agent 不得自行生成精确的本地目标参数数值。

只有 capability registry 已登记可执行的确定性数值关系，且对应 tool/materializer 已实际执行、单位和窗口一致、输入闭包完整时，才允许产生 inferred quantified value。Agent 不能自创公式、系数、稀释比例或小数值。

### D08：无 usable upstream 的 stationless 节点保存零模型结果

stationless 节点全部直接上游均为 `insufficient`/`invalid` 时，M7 仍在依赖收敛后创建该节点唯一 waterbody session，并复用 M6 的确定性 skeleton/assembler 产生完整 `indeterminate/insufficient_evidence` `InferenceResult`：

- planning、optional tools、reasoning batches、repair 与 synthesis 调用均为 0；
- model/tool usage 如实为 0；
- 不创建 fake Agent message；
- session 正常 `completed`；
- outcome 向下一节点仍为 `insufficient`，不携带可解锁 gate 的 evidence。

因此长 stationless 链可以用零模型结果完整说明证据为何中断；后续若遇到有 station 的节点，仍按 D03 查询其本地数据。target 走本路径时，其确定性 `indeterminate` result 就是 run 的科学主结论。

### D09：ready 节点有界并行

调度器使用 dependency-aware ready queue。同一 execution layer 的独立节点可并行；节点不必等待同层无关任务结束，只需等待自身全部直接上游。

所有 waterbody Agent 共享后端配置的正整数并发上限。ready queue 按 graph execution order 与稳定 COMID 次序 tie-break；并发完成先后可以变化，但 node identity、依赖 scope、结果引用和 coverage 计算必须可重现。

一条节点失败不会取消同层其他独立节点。释放名额后继续执行其他 ready 节点。首版仍是单进程、单 worker，不宣称跨进程共享 semaphore、队列或恢复。

### D10：总工作量只受 topology 与既有 session 上限约束

M7 不新增 run-wide token 或 waterbody-model call 总额度。总水体规模沿用后端固化的最大上游距离、hop 和 node 数；M7 不扩图、不静默删除 active node。

每个 session 继续遵守 M5/M6 tool budget、planning/batch/repair 上限、provider token capability 与 deadline。topology 因 hard limit 截止时，原 coverage/branch outcome 进入结果与汇总，不得伪装成自然无上游。

### D11：target result 是科学主结论

target waterbody 的 `InferenceResult` 是 run 的唯一科学主结论。M7 不再调用另一个模型重新裁决 evidence gate、risk、confidence 或 attribution，也不从多水体结果计算新的科学分数。

run-level result 引用 target result、各 active node outcome、graph/context 和 summary；各水体完整 `InferenceResult` 只保存于原 session 一份，run 层不得复制完整 series、evidence chain 或 transcript。

### D12：增加一个独立 `run_summarizer` session

全部 active waterbody outcomes 收敛且 run 未取消后，M7 创建至多一个 `agent_role=run_summarizer`、`target_comid=null` 的独立 Agent session。它使用 run 固化的 `waterbody_model`，不增加第三个模型设置，也不切换 parser/default/fallback model。

summary Agent 只负责用用户容易理解的语言解释：

- 本次问题与分析范围；
- 水流从哪些分支汇入目标；
- 主要水体结论与证据覆盖；
- 特殊值、明确趋势或高风险发现；
- 哪些分支不足或 invalid；
- target 最终结论及其限制。

它不调用 evidence tools、不读取 provider raw response/内部日志/hidden reasoning、不改变任何水体结果。

### D13：一次汇总调用使用完整 target 投影与其他水体报告卡

summary 只调用一次。输入包括：

- target `InferenceResult` 的完整安全业务投影；
- active graph 的连接与稳定执行顺序；
- 每个其他 active waterbody 的有界结构化报告卡；
- graph coverage/limit facts 与 dependency outcomes。

报告卡保留 identity、session/result refs、overall/window risk、availability、confidence、关键 reason、usage 小计和 notable findings，但不内联完整 series、evidence、transcript 或 provider response。所有 active node 都必须有一张卡；输入无法装入所选模型上下文时 summary 明确失败，不静默丢水体。

### D14：特殊值必须有原引用，并为 M9 留下可跳转接缝

summary 可输出 `highlights[]`，突出已有结果中的 high risk、directional trend、`suspicious` quality、invalid/insufficient branch 或对结论有影响的 quantified value。

具体数值必须逐字来自已验证 result，保留 decimal string、unit、窗口、source 与 result/cell/evidence ref。没有统一科学阈值时，后端不自创异常值 detector；Agent 不得重算、外推或补值。

首版保存稳定的 run/session/item/result reference 即可，不要求可点击 URL。M9 可以由后端验证后的 ref 生成站内导航，使用户打开对应水体结果或 Agent 上下文；URL 不由 Agent 生成。

### D15：summary 失败是真实子任务失败，不伪造回退文本

summary 调用失败时：

- `run_summarizer` session 进入 `failed`；
- summary status 为 `failed`，summary text/ref 不存在；
- 不使用固定模板、规则拼接、旧 summary 或 fake message 冒充成功；
- 后端记录详细脱敏诊断，前端收到安全失败信息；
- 已完成的 target/水体结果不回滚。

target result 已合法提交时，run 仍可 `completed`；summary 是解释层，不是科学结果权威。

### D16：非 target failure 形成 partial coverage，不否定 target

只要 target session 正常完成并提交合法 `InferenceResult`，非 target 分支 failed 不使整个 run failed。run result 明确 graph coverage 为 incomplete/partial，并保留 completed/insufficient/invalid 数量和引用。

summary 只用安全措辞说明“部分支流没有提供有效结果”及影响，不展示技术详情。target 的确定性 `indeterminate/insufficient_evidence` 也是正常业务结果，run 仍 completed。只有 target 自身因技术故障不能提交合法结果、run cancel 或 run-level fatal error 才进入 failed/cancelled。

### D17：取消仍只有 run cancel，先封调度再通知 sessions

用户只调用既有 run cancel。取消被接受时必须在同一临界区：

1. 固定 `cancel_accepted`；
2. 阻止 query/topology 后续阶段、ready queue dequeue、新 session/model/tool 启动；
3. 通知所有已创建且未终态的 parser、waterbody 与 summarizer sessions；
4. 等待不可强杀调用自然返回并进入晚到审计；
5. 记录 `workflow_quiesced` 后才允许 live-state cleanup。

已 completed/failed 的 session 终态和已有 transcript 不改写。cancel 后不发布新的水体 result、run result 或下游 evidence；未创建节点不为取消补造 session。

### D18：只扩展既有两层 SSE 与历史读取

M7 不新增公开 route 或事件流：

- run SSE 继续发送轻量完整快照，增加 analysis phase、node scheduling/outcome、run result ref 和 summary status；
- session SSE 同时承载 waterbody 与 `run_summarizer` 的真实过程；summarizer 只在验证成功后发送一次性 completed message/result，不公开 provider token delta；
- GET detail/history 从原 typed content 与引用读取完整结果；
- run cancel 和 persistence retry 路径不变。

run flow 的正常结束仍等待业务终态、workflow quiesced 和适用保存收敛；session flow 在该 session 结束且可见过程发送完后结束。

### D19：模式和时间窗口原样贯穿 DAG

每个 waterbody task 使用同一已保存 `AnalysisContext.mode`、time segments、timezone、resolution、requested/supporting parameters 和 final windows。M7 不按节点重新推导 `gap_filling/prediction/mixed`。

upstream handoff 保留逐窗口 outcome 与 `observed/inferred/predicted`。下游 result source 仍由 M6 校验：gap-filling 只允许 observed/inferred，prediction 只允许 predicted；跨水体关系不能把 upstream observed 复制为 downstream observed。

### D20：M8/M9 只消费稳定接缝

M8 可以在既有 M5 producer slot 中加入合格历史 evidence，但不能改变 M7 dependency ready、direct-upstream scope、当前结果优先级或 summary authority。历史 evidence 仍是 contextual，除非现有 capability relation 明确允许并物化。

M9 必须：

- 识别 `run_summarizer` session role，不能假设所有 session 都有 COMID；
- 展示 summary completed/failed 和安全错误；
- 从稳定 refs 构造站内跳转，不信任 Agent URL；
- 展示 partial graph coverage 与 invalid/insufficient 分支；
- 继续使用 run SSE + 当前所选 session SSE，不为汇总新增通道。

## 3. 终态矩阵

| 情况 | 水体/summary session | run |
|---|---|---|
| target 有 quantified/directional 结果，全部必要步骤完成 | target completed | completed |
| target 确定性全 indeterminate | target completed，0 model path 可适用 | completed |
| 非 target failed，但 target 仍完成 | 原分支 failed，其他终态不改 | completed + partial coverage |
| summary failed，但 target 已完成 | summarizer failed，无 summary text | completed + summary failed |
| target technical failed、无合法 result | target failed | failed |
| run cancel 胜出 | 已完成保持；其余已创建 session cancelled/原终态 | cancelled |
| workflow/persistence 发生既有保存错误 | 按 M3 原 session 终态 | 沿用 completed_with_persistence_error/补写语义 |

## 4. 交付文档

- [M7 实施设计](m7_topology_driven_multi_agent_implementation_design.md)：application/domain/port 设计、调度算法、原子提交、配置与测试。
- [M7 Policy Catalog](m7_workflow_policy_catalog.md)：dependency usability、启动矩阵、summary prompt 与输出校验。
- [M7 API](m7_topology_driven_multi_agent_api.md)：现有 routes 的新增投影、typed contents、SSE 与示例。
- [M7 Implementation TODO](m7_implementation_todo.md)：Milestone 0–9 的施工顺序、测试门禁、阶段出口、契约矩阵与完成记录格式。
