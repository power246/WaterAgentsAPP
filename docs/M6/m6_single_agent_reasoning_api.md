# M6 Single-Agent Reasoning API / 读取契约

更新日期：2026-10-02

> 状态：M6 typed transcript、session SSE 投影、真实 waterbody session completed payload、history summary 和 public error 已完成本机实现与 PostgreSQL/SSE 验证；production 仍不新增 reasoning HTTP route。真实 provider 与三平台证据边界见 [Implementation TODO](m6_implementation_todo.md)。

## 1. Production 路由不新增 reasoning 入口

M6 继续使用 M3 已实现的 run/session API：

```text
POST /api/analysis/runs
GET  /api/analysis/runs/{run_id}
GET  /api/analysis/runs/{run_id}/events
POST /api/analysis/runs/{run_id}/cancel
GET  /api/analysis/runs/{run_id}/sessions/{session_id}
GET  /api/analysis/runs/{run_id}/sessions/{session_id}/transcript
GET  /api/analysis/runs/{run_id}/sessions/{session_id}/events
GET  /api/analysis/runs/{run_id}/transcript/{item_id}/content
```

production OpenAPI 不得出现：

```text
/reasoning/invoke
/agents/run
/models/complete
/tools/invoke
/evidence/collect
```

前端不提交 prompt、tool arguments、risk/confidence、result skeleton 或 reasoning batch。M6 全部由 run 固化配置与后端 workflow 驱动。

## 2. 统一 transcript envelope 不变

所有 M6 内容继续使用 M3 `TranscriptItemView`：

```json
{
  "item_id": "60000000-0000-4000-8000-000000000001",
  "run_id": "10000000-0000-4000-8000-000000000001",
  "transcript_sequence": 41,
  "occurred_at": "2026-10-02T02:00:00Z",
  "content_type": "reasoning_decision",
  "content_schema_version": 1,
  "scope": {
    "session_id": "20000000-0000-4000-8000-000000000001",
    "agent_role": "waterbody",
    "target_comid": "1234567"
  },
  "correlation": {
    "request_id": null,
    "operation_id": "30000000-0000-4000-8000-000000000001",
    "error_id": null
  },
  "tags": ["reasoning_batch"],
  "rag_eligible": false,
  "confidence": null,
  "provenance": null,
  "content_bytes": 512,
  "content_ref": {
    "run_id": "10000000-0000-4000-8000-000000000001",
    "item_id": "60000000-0000-4000-8000-000000000001"
  },
  "content": {
    "kind": "inline",
    "body": {}
  },
  "parse_status": "ok",
  "parse_error": null
}
```

inline/preview/reference、全文读取、auto/history、unknown schema、corrupt item、公共引用和 persistence warning 沿用 M3。M6 不因 prompt/response 较大改变阈值或另建文件接口。

## 3. `waterbody_agent_input v1`

在 runner 开始时写一次，保存本 session 的权威输入引用，不复制完整 graph/evidence：

```json
{
  "schema_version": 1,
  "run_id": "10000000-0000-4000-8000-000000000001",
  "session_id": "20000000-0000-4000-8000-000000000001",
  "target_comid": "1234567",
  "analysis_context_ref": {
    "run_id": "10000000-0000-4000-8000-000000000001",
    "item_id": "40000000-0000-4000-8000-000000000001"
  },
  "topology_ref": {
    "run_id": "10000000-0000-4000-8000-000000000001",
    "item_id": "40000000-0000-4000-8000-000000000002"
  },
  "initial_evidence_bundle_ref": null,
  "requested_parameters": [
    {"code_system": "usgs", "code": "00010"}
  ],
  "supporting_parameters": [
    {"code_system": "usgs", "code": "00060"}
  ],
  "window_ids": ["window-0001", "window-0002"],
  "waterbody_model": {"provider": "openai", "model": "configured-model"},
  "memory_mode": "stateless"
}
```

公开内容不含 credential、provider URL、station secret、raw topology 或任意 prompt。

## 4. `waterbody_model_request v1`

每次 planning/batch/synthesis/repair model operation 写一次实际安全请求：

```json
{
  "schema_version": 1,
  "operation_id": "30000000-0000-4000-8000-000000000001",
  "phase": "reasoning_batch",
  "model": {"provider": "openai", "model": "configured-model"},
  "scope_digest": "sha256:scope-digest",
  "prompt_policy_digest": "sha256:prompt-policy-digest",
  "evidence_bundle_ref": {
    "run_id": "10000000-0000-4000-8000-000000000001",
    "item_id": "50000000-0000-4000-8000-000000000001"
  },
  "batch_id": "batch:usgs:00010:prediction:0001",
  "messages": [
    {
      "role": "system",
      "content": "Rendered and redacted M6 system policy."
    },
    {
      "role": "user",
      "content": "Bounded batch input with references."
    }
  ],
  "tools": [],
  "response_schema_id": "reasoning_cell_batch_v1"
}
```

实际发送给 provider 的可见 messages 必须保存或通过本 item 的大正文保存，便于审计。API key/header、HTTP options、provider SDK object 和 hidden reasoning config 不进入正文。tool schema 可保存安全 schema 或 canonical digest；不能只保存工具名称而无法还原实际授权参数。

## 5. `waterbody_agent_message v1`

Agent 可见文字使用一个可流式 finalizable body：

```json
{
  "schema_version": 1,
  "message_id": "70000000-0000-4000-8000-000000000001",
  "phase": "evidence_planning",
  "role": "assistant",
  "text": "I will inspect the available discharge records before deciding whether more evidence is needed.",
  "generation_status": "completed",
  "end_reason": "completed"
}
```

`text` 是 provider 实际返回、经跨 chunk 脱敏后的用户可见正文，不是后端生成的隐藏思维链摘要。生成中由 session SSE 投影为 `message_started/delta/finalized`；历史读取返回 assembled body。

取消、error、content filter 或 truncation 使用：

```text
generation_status=incomplete
end_reason=cancelled | error | truncated
```

后续晚到文字不再拼入该 message；沿用 M3 `late_message_output` history-only audit。

## 6. `waterbody_model_response v1`

保存 adapter 交给 application 的 provider-neutral 可见响应：

```json
{
  "schema_version": 1,
  "operation_id": "30000000-0000-4000-8000-000000000001",
  "phase": "reasoning_batch",
  "finish_reason": "completed",
  "message_refs": [
    {
      "run_id": "10000000-0000-4000-8000-000000000001",
      "item_id": "70000000-0000-4000-8000-000000000001"
    }
  ],
  "tool_calls": [],
  "structured_output_present": true,
  "usage": {"input_tokens": 3200, "output_tokens": 1400},
  "provider_response_id": "safe-provider-response-id"
}
```

不保存完整 provider envelope、headers、request URL query、encrypted/hidden reasoning、secret 或未经验证的 arbitrary fields。structured raw output 作为本 item 的 typed member或独立大正文保存，必须可与后续 `reasoning_decision` 对照；public projection 可以仅显示 presence/usage/ref，完整内容按 content_ref 读取。

## 7. `reasoning_decision v1`

使用 discriminator `decision_type`。

### 7.1 Planning decision

```json
{
  "schema_version": 1,
  "decision_type": "evidence_ready",
  "operation_id": "30000000-0000-4000-8000-000000000002",
  "bundle_ref": {
    "run_id": "10000000-0000-4000-8000-000000000001",
    "item_id": "50000000-0000-4000-8000-000000000001"
  },
  "rationale": "The remaining unsupported cells have no legal evidence path within the current scope."
}
```

其他 planning types：

```text
tool_calls_requested
evidence_rules_proposed
planning_limit_reached
evidence_sealed
```

tool call/result 本体仍使用 M5 typed contents，不复制进 decision。

### 7.2 Validated reasoning batch

```json
{
  "schema_version": 1,
  "decision_type": "reasoning_batch_validated",
  "operation_id": "30000000-0000-4000-8000-000000000003",
  "batch_id": "batch:usgs:00010:gap_filling:0001",
  "scope_digest": "sha256:scope-digest",
  "cells": [
    {
      "cell_id": "cell:usgs:00010:window-0001",
      "outcome_status": "quantified",
      "representative_value": "18.4",
      "target_unit_id": "degC",
      "source": "observed",
      "trend": "stable",
      "risk_level": "none",
      "risk_rationale": "The cited current-waterbody observation supports no identified temperature risk for this window.",
      "confidence": 0.92,
      "confidence_rationale": "The value is a direct valid observation with complete window coverage.",
      "aggregation_method": null,
      "evidence_refs": ["evidence:sha256:direct-record"]
    }
  ]
}
```

这里只保存 validated draft；原 raw structured output 在 model response 中保留。fixed indeterminate cells 不伪装成 Agent batch output。

### 7.3 Overall synthesis

```json
{
  "schema_version": 1,
  "decision_type": "overall_synthesis_validated",
  "operation_id": "30000000-0000-4000-8000-000000000004",
  "window_assessments": [
    {
      "window_id": "window-0001",
      "risk_level": "low",
      "risk_rationale": "The assessed parameters show only a limited risk signal in this window.",
      "confidence": 0.78,
      "confidence_rationale": "Most parameters are direct, but one is inferred.",
      "contributing_cell_refs": ["cell:usgs:00010:window-0001"]
    }
  ],
  "overall": {
    "risk_level": "low",
    "risk_rationale": "Across the requested range, the supported results indicate limited risk with explicit coverage gaps.",
    "confidence": 0.7,
    "confidence_rationale": "The conclusion is supported by direct observations but availability is partial.",
    "contributing_window_refs": ["overall-window:window-0001"],
    "contributing_cell_refs": ["cell:usgs:00010:window-0001"]
  },
  "analysis_detail": {},
  "attribution_summary": {}
}
```

availability、counts、missing/blocker refs 不由 Agent body 提供；assembler 从 sealed plan 计算并写入 final result。

### 7.4 Repair decision

repair request/response 使用新 operation_id，并引用原 operation/item。validated repair decision 增加：

```json
{
  "repair_of_operation_id": "30000000-0000-4000-8000-000000000003",
  "validation_issue_codes": ["missing_risk_rationale"]
}
```

issue 只包含稳定 code/path，不公开内部异常或 provider body。

## 8. `inference_result v1`

final result 是唯一完整业务结果 typed item。缩略示例：

```json
{
  "result_id": "result:10000000-0000-4000-8000-000000000001:1234567",
  "run_id": "10000000-0000-4000-8000-000000000001",
  "session_id": "20000000-0000-4000-8000-000000000001",
  "target_comid": "1234567",
  "mode": "mixed",
  "time_range": {
    "start": "2026-10-01T05:00:00Z",
    "end": "2026-10-03T05:00:00Z"
  },
  "time_segments": [],
  "analysis_timezone": "America/Chicago",
  "time_resolution": "6h",
  "time_resolution_source": "auto",
  "series": [],
  "window_assessments": [
    {
      "assessment_id": "overall-window:window-0001",
      "start_time": "2026-10-01T05:00:00Z",
      "end_time": "2026-10-01T11:00:00Z",
      "risk_level": "low",
      "risk_rationale": "Supported parameters indicate limited risk in this window.",
      "confidence": 0.78,
      "confidence_rationale": "Most evidence is direct; one parameter is inferred.",
      "availability": "partial",
      "assessed_cell_count": 1,
      "total_cell_count": 2,
      "contributing_cell_refs": ["cell:usgs:00010:window-0001"],
      "missing_cell_refs": ["cell:usgs:00400:window-0001"],
      "blocker_refs": [],
      "evidence_refs": ["evidence:sha256:direct-record"]
    }
  ],
  "overall_assessment": {
    "assessment_id": "overall-range",
    "risk_level": "low",
    "risk_rationale": "Supported results indicate limited risk, with an explicit coverage gap.",
    "confidence": 0.7,
    "confidence_rationale": "Direct evidence supports the assessed portion, but availability is partial.",
    "availability": "partial",
    "assessed_cell_count": 1,
    "total_cell_count": 2,
    "contributing_window_refs": ["overall-window:window-0001"],
    "contributing_cell_refs": ["cell:usgs:00010:window-0001"],
    "missing_cell_refs": ["cell:usgs:00400:window-0001"],
    "blocker_refs": [],
    "evidence_refs": ["evidence:sha256:direct-record"]
  },
  "overall_confidence": 0.7,
  "overall_confidence_rationale": "Direct evidence supports the assessed portion, but availability is partial.",
  "attribution_summary": {},
  "analysis_detail": {},
  "evidence_chain": {},
  "model_execution_summary": {
    "planning_calls": 1,
    "tool_calls": 1,
    "reasoning_batches": 1,
    "repair_calls": 0,
    "synthesis_calls": 1,
    "skipped_cells": 1,
    "usage": {"input_tokens": 5200, "output_tokens": 2100}
  },
  "generated_at": "2026-10-02T02:10:00Z"
}
```

示例中的空对象/数组仅缩短文档；runtime schema 继续要求实施设计中的真实非空/闭包约束。`series`、`analysis_detail`、`attribution_summary` 和 `evidence_chain` 不允许在真实 final result 中用占位空对象绕过。

`rag_eligible` 对 final result 默认仍为 candidate-only：M6 可以按 M5/M3 安全 policy 标记 candidate；M8 才根据 run/session/persistence 状态建立索引。

## 9. `session_completed` payload 原子扩展

当前唯一 lifecycle v2 的 `SessionCompletedPayload` 增加可选 waterbody result projection：

```json
{
  "status": "completed",
  "completed_at": "2026-10-02T02:10:01Z",
  "result_ref": {
    "run_id": "10000000-0000-4000-8000-000000000001",
    "item_id": "80000000-0000-4000-8000-000000000001"
  },
  "result_summary": {
    "result_id": "result:10000000-0000-4000-8000-000000000001:1234567",
    "target_comid": "1234567",
    "risk_level": "low",
    "availability": "partial",
    "overall_confidence": 0.7
  }
}
```

约束：

- waterbody session completed 必须同时有 `result_ref` 和 `result_summary`；
- query_parser session completed 两者都为 null/omitted；
- failed/cancelled 没有 result summary；
- completed event 与 result item 的引用在同一必需写入边界确认；
- payload 不内联完整 series/analysis/evidence。

不保留 M2 空 waterbody result 兼容分支。M1–M9 尚未发布，可原子更新当前 lifecycle v2 consumers、fixtures、OpenAPI 和 tests。

## 10. Session summary、run snapshot 与 history

`SessionSummary` 对 waterbody 增加同一 lightweight `result_ref/result_summary`。因此：

- run SSE 的完整轻量 snapshot 能在 waterbody 完成后显示 risk/availability/confidence；
- run snapshot 不含 series、rationale、evidence 或 Agent messages；
- 点击水体后通过既有 session detail/transcript 读取完整过程和 result item；
- history 使用已持久化 result projection，不在查询时重新推理或重算 risk/confidence。

`RunSummary` 的 run-level final result 仍由 M7 定型。M6 不把一个单水体结果冒充整次 run 总结。

## 11. Session SSE

### 11.1 事件不新增

继续使用：

```text
transcript_item
message_started
message_delta
message_finalized
stream_end
```

M6 不新增 `agent_started`、`evidence_collected`、`reasoning_delta` 或 `agent_completed` transport event。生命周期与正文都通过已有 typed item/message projection 表达。

### 11.2 Agent 实时上下文

session SSE 顺序展示真实产生的 context：

```text
waterbody_agent_input
waterbody_model_request
waterbody_agent_message (started/delta/finalized)
evidence_tool_call/result/fragment/bundle
waterbody_model_response
reasoning_decision
...
inference_result
session_completed lifecycle item
stream_end
```

同名 tool 多次调用使用 operation/tool_call identity 配对，不能按相邻文本猜测。首次无 cursor 订阅必须回放此前全部可见过程并接上实时新增；重连使用 session 独立整数 `Last-Event-ID`。

### 11.3 Run SSE

run SSE 仍发完整轻量快照：run status、analysis phase、context/topology refs、sessions、waterbodies、persistence。M6 只通过 `SessionSummary.result_summary` 增加轻量结果，不放完整 Agent 上下文。

## 12. History 与前端消费

前端必须：

- 展示 parameter-window 的 quantified/directional/indeterminate，不丢 indeterminate；
- 同时展示逐窗口综合风险和日期范围总体风险；
- 风险等级使用 `none/low/medium/high/indeterminate`，并展示 Agent `risk_rationale`；
- confidence 与 risk 分开，并展示 `confidence_rationale`；
- 始终同时展示 availability，`low/none + partial` 不表述为整体安全；
- 从 result refs 进入同一 session 完整上下文；
- 将 tool/structured blocks 按语义折叠，消息正文实时增长；
- 不显示 provider hidden reasoning，也不从 raw provider body自行构造结果；
- 不根据 confidence 改写 outcome/risk；
- 不把未知 discharge 显示为 0。

M9 实现 UI；M6 只冻结数据语义和读取路径。

## 13. Public errors

复用：

```json
{
  "error_id": "90000000-0000-4000-8000-000000000001",
  "code": "waterbody_model_response_invalid",
  "message": "The waterbody model returned an invalid response.",
  "failed_stage": "reasoning_batch",
  "retryable": false
}
```

建议稳定 codes：

| Code | Stage | Retryable | 说明 |
|---|---|---:|---|
| `waterbody_model_unavailable` | planning/batch/synthesis | true | provider/network unavailable |
| `waterbody_model_timeout` | planning/batch/synthesis | true | model deadline |
| `waterbody_model_content_filtered` | planning/batch/synthesis | false | provider content filter/refusal policy |
| `waterbody_model_response_invalid` | planning/batch/synthesis | false | initial invalid，repair 未成功或不适用 |
| `waterbody_model_repair_failed` | repair | false | repair response 仍无效 |
| `reasoning_plan_too_large` | batch_planning | false | 完整 cells 无法放入 hard batch limit |
| `reasoning_budget_exceeded` | evidence_planning | false | required action 未完成即耗尽 budget |
| `reasoning_deadline_exceeded` | current stage | true | session deadline |
| `inference_assembly_failed` | assembling_result | false | validated pieces 无法满足 final invariant |

公开 message 不含 provider body、tool arguments、prompt、URL、credential、traceback 或内部 validation input。详细 issue codes 只进入脱敏 transcript/structured logs，并用 error_id 关联。

## 14. Test-only composition

允许 `build_m6_test_runtime(...)` 或直接 application harness 注入：

- fake waterbody model stream；
- fake M5 ports/producer slots；
- fixed clock/UUID/usage；
- in-memory or PostgreSQL transcript store。

测试入口复用 production runner/validator/assembler/writer，不在 production app 注册 route，不使用 hidden header/query 选择 fake outcome。若提供独立 test app，必须存在于测试包或独立 composition module，production OpenAPI 审计应证明不存在。

## 15. Contract 验收

- 六类 M6 typed contents 注册、strict parse、public projection、large-content read 与 reference closure；
- Agent message 三事件顺序、同 item identity、跨 chunk 脱敏、取消/错误 incomplete 和 late audit；
- reasoning batch/raw response/repair/validated decision 可关联且不重复事实；
- final result 与 completed payload identity/version/ref 一致；
- SessionSummary/run snapshot/history 从同一 persisted result projection 读取；
- run SSE 不含完整 Agent正文，session SSE 可回放全部真实上下文；
- unknown/corrupt schema、数据库错误、410/Last-Event-ID/stream_end 保持 M3 行为；
- production OpenAPI 无 direct reasoning/tool route；
- public errors 和普通日志不泄露 prompt secret/provider body/credential；
- 文档 JSON blocks 可解析，relative links、fences、trailing whitespace 和 `git diff --check` 通过。
