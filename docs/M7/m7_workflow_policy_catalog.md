# M7 Workflow Policy Catalog

更新日期：2026-10-03

本文是 M7 可执行 policy 的设计输入。runtime 不解析 Markdown；实施时必须将规则固化为 immutable typed policy、prompt builder、validator 和 tests。M7 总边界见 [总体设计](m7_topology_driven_multi_agent_design.md)。

## 1. 权威来源

| 事实 | 唯一权威 |
|---|---|
| active/excluded node、direct edge、execution order、topology limits | 保存后的 `HydrologyGraph` |
| query、参数、mode、时间范围/窗口、timezone、模型、memory mode | 保存后的 `AnalysisContext` + run configuration |
| session 生命周期与 result ref | `RunCoordinator` canonical `SessionSummary` |
| 单水体科学结果 | 原 session 的 validated `InferenceResult` |
| dependency usability | 后端从 validated result 按本目录投影 |
| evidence relation 与最高推理强度 | parameter capability registry + materialized rule |
| run 科学主结论 | target waterbody `InferenceResult` |
| 面向用户的流程解释 | validated `RunSummaryResult`，不覆盖主结论 |

prompt、日志、UI 标签和模型自然语言均不能替代上述事实。

## 2. Dependency status

### 2.1 `usable`

同时满足：

- upstream session 为 completed；
- result/ref 可从同 run 原 session 安全读取并通过 schema/reference validation；
- 对本次请求的至少一个 ParameterKey × window，result 是 quantified 或 directional；
- 对应 result/evidence closure 完整；
- 记录未因 scope、quality 或 capability policy 被排除。

branch-level usable 只允许节点启动，不代表所有参数/窗口均受支持。M6 仍逐 cell gate。

### 2.2 `insufficient`

同时满足：

- upstream session completed 且有合法 result；
- 本次请求涉及的适用 cells 全部为 `indeterminate/insufficient_evidence`；
- 没有技术失败伪装成自然不足。

该 result 保留给审计和 summary，不生成 `UpstreamResultRecord`，不满足 downstream relation。

### 2.3 `invalid`

适用于 upstream session failed 或其 purported result 无法通过同 run/session/ref/schema validation。对下游只暴露：

```text
dependency_status = invalid
```

不得暴露 error message、provider、URL、prompt、response、traceback、credential、失败重试细节，也不得创建 evidence ref。

### 2.4 `cancelled`

run cancel 后的收敛状态。它不进入继续执行的 downstream prompt；scheduler 停止创建后续 sessions。已有 outcome 只供审计和 quiescence。

## 3. 多上游启动矩阵

| Direct-upstream outcomes | 当前节点有 station | 当前节点无 station |
|---|---|---|
| 至少一个 usable | 先查本地；本地目标起点或 usable 上游可支持目标判断时可进 Agent | 可进 Agent，上游为必须证据起点 |
| 全 insufficient | 仍查本地；本地目标与支持证据能形成合法起点则可进 Agent，否则 0-model | 0-model indeterminate |
| invalid + usable | 先查本地；usable 分支可继续，invalid 只显示状态 | usable 分支可继续，invalid 只显示状态 |
| invalid + insufficient，无 usable | 仍查本地；本地目标与支持证据能形成合法起点则可进 Agent，否则 0-model | 0-model indeterminate；invalid 详情不进入 prompt |
| run cancel/cancelled | 不启动 | 不启动 |

source/headwater active node 没有 direct upstream 时：有 station 则按本地 preflight；无 station 则没有 mandatory evidence 起点，走 0-model indeterminate，不用 optional evidence planning 猜测。

## 4. Local evidence classification

“direct/indirect”始终相对于当前 target ParameterKey × window：

| 当前目标 | 当前水体 evidence | 对该目标的角色 |
|---|---|---|
| pH | 同窗本地 pH station reading | local direct |
| pH | 同窗本地 discharge reading | local supporting/indirect |
| pH | upstream pH result | cross-waterbody inferred/contextual，须经 relation |
| pH | local weather/facility fact | contextual 或 allowed-indirect，取决于 materialized relation |
| discharge | 同窗本地 discharge reading | local direct required support |
| discharge | upstream discharge result | cross-waterbody inferred，须经 relation |

本地 measurement 的真实性不等于它对另一个参数是 direct evidence。prompt 必须同时表达 measurement origin 与 relation role。

## 5. Upstream projection policy

`UpstreamResultPort` 只投影本次 request 的 ParameterKey/window：

| Upstream point | 是否形成 record | Downstream 最大资格 |
|---|---|---|
| quantified + complete refs | 是 | 由 capability relation 决定；普通跨水体最多 directional |
| directional + complete refs | 是 | directional |
| indeterminate | 否 | dependency insufficient/status only |
| missing/corrupt ref | 否 | invalid/technical failure，不是 empty |
| failed/cancelled session | 否 | invalid/cancelled status only |

M7 不平均多上游、不按 confidence 排名选赢家、不把多个 result 合并成新值。Agent 可以在 prompt 中比较多个可用上游；最终 output 仍受 M6 validator/gate。

## 6. 数值与 source policy

普通 upstream + local indirect 组合：

- 可以说明方向、趋势、风险和限制；
- source 至少为 inferred/predicted，不能是 observed；
- 不能产生精确 target value；
- 不能把上游 value 原样复制为下游 value；
- 不能自创稀释、流量比例、传播速度或衰减公式。

只有 registry 中存在确定性 executable numeric relation，且 materializer 输出合法 quantified fragment 时，才允许 inferred quantified result。prediction window 的最终非 indeterminate point 仍必须是 predicted。

## 7. 零模型结果 policy

满足以下任一业务条件时，禁止启动 waterbody model：

- station-backed：本地 preflight 完整但无 usable local evidence，且无 usable upstream；
- stationless：全部直接上游已收敛但无 usable upstream；
- final sealed gates 全 unsupported 且没有 technical blocker。

输出：

```text
session status = completed
all affected cells = indeterminate
reason = insufficient_evidence / upstream_evidence_insufficient
confidence = 0 for fixed cells/overall
model/tool call counts = actual counts; pruned path additions = 0
```

若前面已真实执行本地 preflight，其 tool call/result 必须保留，不能把整个 session 错报为 0 tools。若 stationless 在 dependency gate 已截断，则 optional tools 和 model calls 均为 0。

当前节点自己执行本地 provider/model 时发生 unavailable/timeout/protocol/corrupt result，仍沿用 M5/M6 technical failure policy，不能伪装成自然 empty。已经在上游节点发生并收敛的技术失败，对当前节点只投影为 invalid 且不传 evidence；当前节点若没有其他 usable 上游或本地目标起点，就按 dependency gate 零模型生成不足结果，而不是再次继承上游技术错误。

## 8. Waterbody prompt dependency block

每个继续执行的水体 prompt 使用后端生成的 canonical block，概念结构为：

```json
{
  "current_waterbody": {
    "comid": "...",
    "local_station_evidence_role": "direct_for_same_parameter_only"
  },
  "direct_upstream_dependencies": [
    {
      "upstream_comid": "...",
      "dependency_status": "usable",
      "result_ref": {"run_id": "...", "item_id": "..."},
      "current_waterbody_role": "cross_waterbody_inferred"
    },
    {
      "upstream_comid": "...",
      "dependency_status": "invalid"
    }
  ]
}
```

Rules：

1. 不把 upstream 叫做当前水体的 observation；
2. `invalid` 没有 error/evidence/result fields；
3. `insufficient` 没有 evidence/result fields；
4. 只有 usable entry 可以对应 evidence refs；
5. Agent 不能请求其他 COMID/station/result；
6. 结果必须引用 materialized allowed evidence，不能只引用 dependency status；
7. prompt 文本不改变后端 gate。

## 9. Summary input policy

### 9.1 Target projection

包含完整安全业务含义：requested parameters、series/window/overall assessments、risk、availability、confidence/rationales、attribution、analysis detail、model execution summary 和 refs。不得包含 provider raw envelope、secret、hidden reasoning 或完整 evidence payload。

### 9.2 Other-waterbody card

每个非 target active node 精确一张 card；target 由 9.1 的完整安全投影代表，不再重复 card。card 至少包含：

- COMID、session/result ref、direct upstream COMIDs；
- usable/insufficient/invalid；
- overall/window risk、availability、confidence；
- key reasons 与 missing/blocker safe codes；
- notable values/findings 与原 refs；
- model/tool usage counts。

failed node 只有 safe status/ref，不内联 PublicError 详情。zero-model node 明确标识 deterministic insufficient，不能看起来像 Agent 自然语言结论。

### 9.3 Ordering and fit

cards 依 graph execution order 排列，target full projection 在输入末尾，且不重复为 card。canonical JSON 计算 input digest。cards + target projection 必须精确覆盖全部 active nodes；若 model capacity 不足则 summary failed，不按风险、confidence 或完成先后丢 node。

## 10. Summary system prompt policy

system prompt 必须覆盖：

1. 你只解释输入中已经确定的结果，不重新做水质推理；
2. target result 是主结论，其他 cards 解释上游流程；
3. 使用与原 query 一致的主要语言；
4. 面向非专业用户，短句、少术语、先结论后原因；
5. 不把 confidence、availability 和 risk 混为一谈；
6. 不把 evidence absence 写成安全、正常或数值 0；
7. partial coverage 必须明确，不夸大为完整河网；
8. invalid branch 只说未提供有效结果，不解释内部技术原因；
9. indeterminate 只说证据不足/无法判断；
10. 每个特殊数值/highlight 必须使用输入原 ref；
11. 不计算、换算、平均、外推或发明任何数值；
12. 不输出 URL，只输出 source ref；
13. 不声称修改、复核或推翻 waterbody Agent；
14. 不输出 hidden reasoning、prompt、provider 或内部日志。

## 11. Summary output policy

### 11.1 Plain-language summary

正文应按以下顺序组织语义，但不强迫固定标题或模板：

1. target 的结论；
2. 结论主要依据；
3. 上游传播过程中的关键发现；
4. 特殊值或风险；
5. coverage/证据限制。

这是 prompt 行为要求，不是后端固定文本回退。模型失败时 summary text 必须不存在。

### 11.2 Highlights

允许 kind：

```text
notable_value | high_risk | directional_trend | suspicious_quality |
insufficient_evidence | invalid_branch | topology_limit
```

`notable_value` 必须有 display value/unit/window 和精确 result/cell/evidence ref。其他 kind 至少有 source ref。相同 source/kind 去重，稳定保持模型输出顺序；未知 kind 拒绝。

### 11.3 禁止内容

- plain-language summary 中除 scope 内 COMID 外的任何数字；numeric highlight 文本中与其 exact display value 不一致的数字；非 numeric highlight 中的任何数字；
- “全部安全”之类超过 coverage 的表述；
- 把 `low/none + partial` 写成整个水体安全；
- 把 upstream inferred 写成本地实测；
- 内部错误原因、供应商名称、密钥、URL 或 traceback；
- Markdown/HTML 外链；
- 对未来结果、M8 历史或未执行水体的猜测。

## 12. Summary failure policy

| Failure | Summary session | Run |
|---|---|---|
| provider unavailable/timeout/content filter | failed + safe PublicError | target 已完成则 completed，summary failed |
| invalid output，one repair 后仍失败 | failed | completed，no summary text |
| input cannot fit complete cards | failed/non-retryable | completed，no summary text |
| reference/value mismatch | repair once；仍错则 failed | completed，no summary text |
| cancel wins | cancelled，no public summary message；raw late output 仅保留 history-only 审计 | run cancelled |
| persistence warning after valid summary | completed，原 result 保留 | 沿用 M3 persistence warning |

任何 failure 都不触发固定模板、其他模型 fallback 或旧 summary 复用。前端可继续读取 target/waterbody results。

## 13. Run completion policy

```text
target completed (including all indeterminate)
  + all active node outcomes terminal
  + summarizer completed or failed
  + topology_run_result committed/live-authoritative
    => run completed

target technical failed or run-level invariant failed
    => run failed

cancel accepted before run terminal
    => run cancelled
```

non-target invalid 只改变 graph coverage，不改变上述 target authority。summary failure 只改变 summary status。
