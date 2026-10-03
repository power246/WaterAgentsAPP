# M7 Topology-Driven Multi-Agent Workflow API

更新日期：2026-10-03

本文冻结 M7 对既有 HTTP、SSE、read model 和 typed transcript content 的扩展。M7 不新增公开 route；实现仍以 OpenAPI、serializer 和 raw SSE fixtures 为准。

## 1. 路由保持不变

```text
POST /api/analysis/runs
GET  /api/analysis/runs
GET  /api/analysis/runs/{run_id}
GET  /api/analysis/runs/{run_id}/status
GET  /api/analysis/runs/{run_id}/events
POST /api/analysis/runs/{run_id}/cancel
POST /api/analysis/runs/{run_id}/persistence/retry
GET  /api/analysis/runs/{run_id}/sessions/{session_id}
GET  /api/analysis/runs/{run_id}/sessions/{session_id}/transcript
GET  /api/analysis/runs/{run_id}/sessions/{session_id}/events
GET  /api/analysis/runs/{run_id}/transcript/{item_id}/content
```

禁止新增：

- direct DAG start/retry route；
- direct upstream-result/tool/reasoning route；
- 单个 waterbody/summary session cancel route；
- summary 专用 endpoint 或 SSE；
- 按 COMID 定位同一 session 的第二套 route。

## 2. AgentRole 与 session summary

`AgentRole` 当前唯一枚举增加：

```text
query_parser | waterbody | run_summarizer
```

角色约束：

| role | target_comid | completed result |
|---|---|---|
| query_parser | null | 不适用 |
| waterbody | 必填 | `waterbody` result summary |
| run_summarizer | null | `run_summary` result summary |

`SessionResultSummary` 使用严格 discriminator：

```json
{
  "result_kind": "waterbody",
  "result_id": "...",
  "target_comid": "123",
  "risk_level": "medium",
  "availability": "partial",
  "overall_confidence": 0.63
}
```

```json
{
  "result_kind": "run_summary",
  "highlight_count": 3,
  "language": "zh-CN"
}
```

summary failed/cancelled session 不得携带 completed result ref/summary。waterbody completed 仍必须同时携带原 `InferenceResult` ref/summary。

## 3. Analysis phase

`AnalysisPhase` 增加：

```text
queued
query_understanding
topology_building
topology_ready
waterbody_execution
run_summarization
terminal
```

phase 不替代 run status，也不表示所有同类 sessions 同时处于该状态。`run_summarization` 时 waterbody results 已收敛但 topology run result 尚未最终提交。

## 4. Run snapshot

`RunSnapshotEvent` 继续使用当前唯一 schema，原子扩展：

```text
result_ref: ContentRef | null
summary:
  status: not_started | running | completed | failed | cancelled
  session_id: UUID | null
  result_ref: ContentRef | null
  error: PublicError | null
graph_outcome:
  coverage: complete | partial | none | null
  usable_nodes: int
  insufficient_nodes: int
  invalid_nodes: int
waterbodies[]:
  target_comid
  participating
  scheduling_status
  dependency_status
  session_id
  session_status
  result_ref
```

`scheduling_status`：

```text
excluded | waiting | ready | running | terminal
```

约束：

- excluded 水体无 dependency/session/result；
- waiting/ready 可无 session；
- running/terminal 已创建 session；
- `dependency_status` 只对 terminal active node存在；
- result ref 只对 completed waterbody/session存在；
- summary failure 的 `PublicError` 是安全公开错误，不含内部诊断；
- summary cancelled 只在 run cancel 胜出时出现，不带 result_ref 或 completed result summary；
- snapshot 不内联完整 summary、InferenceResult、report cards 或 transcripts。

示例：

```json
{
  "schema_version": 1,
  "run_id": "5c40b92c-fffb-43a7-9ee4-28a0c73f9d31",
  "status": "running",
  "analysis_phase": "run_summarization",
  "created_at": "2026-10-03T01:00:00Z",
  "updated_at": "2026-10-03T01:03:00Z",
  "context_ref": {"run_id": "5c40b92c-fffb-43a7-9ee4-28a0c73f9d31", "item_id": "..."},
  "topology_ref": {"run_id": "5c40b92c-fffb-43a7-9ee4-28a0c73f9d31", "item_id": "..."},
  "result_ref": null,
  "summary": {
    "status": "running",
    "session_id": "7d1e9c4a-0a8f-44f7-9787-a49006ac7f0e",
    "result_ref": null,
    "error": null
  },
  "graph_outcome": {
    "coverage": "partial",
    "usable_nodes": 2,
    "insufficient_nodes": 1,
    "invalid_nodes": 1
  },
  "sessions": [],
  "waterbodies": [
    {
      "target_comid": "101",
      "participating": true,
      "scheduling_status": "terminal",
      "dependency_status": "usable",
      "session_id": "...",
      "session_status": "completed",
      "result_ref": {"run_id": "5c40b92c-fffb-43a7-9ee4-28a0c73f9d31", "item_id": "..."}
    }
  ],
  "persistence": {"state": "up_to_date", "warning": null}
}
```

`sessions` 示例省略不表示实际响应为空；真实 snapshot 继续返回该 run 已创建 session 的轻量目录。

## 5. Run detail/history

`GET /runs/{run_id}` 增加：

```text
result_ref: ContentRef | null
result_summary: TopologyRunResultSummary | null
```

建议 summary：

```text
target_comid
target_result_summary
graph_coverage
outcome_counts
summary_status
summary_result_ref?
```

完整 `topology_run_result v1` 仍从原 item/ref 读取，不在 detail、history list 或 lifecycle event 重复。历史 list 只增加必要的小型 target/coverage/summary-status 投影；损坏/未知类型通过现有 `issues` 返回，不用当前 live 数据补齐。

## 6. `waterbody_dependency_manifest v1`

session-owned，概念 JSON：

```json
{
  "schema_version": 1,
  "run_id": "...",
  "session_id": "...",
  "target_comid": "300",
  "topology_ref": {"run_id": "...", "item_id": "..."},
  "dependencies": [
    {
      "upstream_comid": "100",
      "upstream_session_id": "...",
      "dependency_status": "usable",
      "result_ref": {"run_id": "...", "item_id": "..."},
      "usable_cell_refs": ["cell:..."],
      "discharge_states": [
        {"window_id": "window:...", "outcome": "directional"}
      ]
    },
    {
      "upstream_comid": "200",
      "upstream_session_id": "...",
      "dependency_status": "invalid",
      "result_ref": null,
      "usable_cell_refs": [],
      "discharge_states": []
    }
  ],
  "manifest_digest": "sha256:..."
}
```

`invalid` 和 `insufficient` entries 不得带 evidence refs、confidence、value、internal error 或 closure。若当前节点无 direct upstream，dependencies 为空是合法的 source-node manifest。

## 7. `run_summary_* v1`

### 7.1 `run_summary_input`

session-owned，保存实际 canonical model input 或其原 content ref：

```text
run_id/session_id
query
target_comid
mode/time_range/timezone
graph projection
target result projection
report_cards[]
allowed_source_refs[]
input_digest
```

### 7.2 Request/message/response

沿用 M6 provider-neutral 模式但使用独立 content types：

- `run_summary_model_request v1`：model、messages、strict response schema、deadline、input digest；
- `run_summary_agent_message v1`：`generating/completed/incomplete` 可见浅显文本；
- `run_summary_model_response v1`：安全 visible raw response、finish status、usage；
- repair 仍保存第二次真实 request/response，不覆盖第一次。

summary session 不含 tool descriptors/tool calls。

### 7.3 `run_summary_result`

```json
{
  "schema_version": 1,
  "run_id": "...",
  "session_id": "...",
  "input_digest": "sha256:...",
  "model": {"provider": "openai", "model": "..."},
  "language": "zh-CN",
  "plain_language_summary": "……",
  "highlights": [
    {
      "kind": "notable_value",
      "text": "目标水体在该时段记录到一个需要注意的 pH 值。",
      "source_ref": {
        "run_id": "...",
        "session_id": "...",
        "item_id": "...",
        "result_id": "...",
        "cell_id": "..."
      },
      "display_value": "6.20",
      "display_unit": "pH",
      "window_id": "window:..."
    }
  ],
  "usage": {"input_tokens": 4200, "output_tokens": 620},
  "generated_at": "2026-10-03T01:03:30Z"
}
```

数值字段只允许精确复制原 result；没有 notable value 时 `highlights` 可为空，不能为了填数组发明异常。

## 8. `topology_run_result v1`

run-owned，概念 JSON：

```json
{
  "schema_version": 1,
  "run_id": "...",
  "analysis_context_ref": {"run_id": "...", "item_id": "..."},
  "topology_ref": {"run_id": "...", "item_id": "..."},
  "target_comid": "300",
  "target_result_ref": {"run_id": "...", "item_id": "..."},
  "target_result_summary": {
    "result_id": "...",
    "target_comid": "300",
    "risk_level": "medium",
    "availability": "partial",
    "overall_confidence": 0.63
  },
  "graph_coverage": "partial",
  "node_outcomes": [
    {
      "target_comid": "100",
      "session_id": "...",
      "dependency_status": "usable",
      "result_ref": {"run_id": "...", "item_id": "..."},
      "result_summary": {"result_id": "..."}
    },
    {
      "target_comid": "200",
      "session_id": "...",
      "dependency_status": "invalid",
      "result_ref": null,
      "result_summary": null
    }
  ],
  "outcome_counts": {"usable": 2, "insufficient": 0, "invalid": 1},
  "summary": {
    "status": "completed",
    "session_id": "...",
    "result_ref": {"run_id": "...", "item_id": "..."},
    "public_error": null
  },
  "generated_at": "2026-10-03T01:03:31Z"
}
```

`node_outcomes` 必须精确覆盖 active graph 并保持稳定顺序。summary failed 时：

```json
{
  "status": "failed",
  "session_id": "...",
  "result_ref": null,
  "public_error": {
    "error_id": "...",
    "code": "run_summary_failed",
    "message": "The plain-language summary could not be generated.",
    "failed_stage": "run_summarization",
    "retryable": true
  }
}
```

run 仍可 completed；前端不得把 summary error 当作 target result failure。

## 9. Run SSE

`GET /runs/{run_id}/events` 继续首先发送最新完整 snapshot，之后只在 snapshot 改变时发送新的完整 snapshot。可能观察到：

```text
topology_ready
  -> waterbody_execution / waiting+running nodes
  -> waterbody_execution / terminal outcomes
  -> run_summarization / summarizer running
  -> terminal / topology result ref + summary completed|failed
  -> persistence updates
  -> stream_end
```

并发 node completion 可以使多项水体状态在一个 snapshot 中同时改变。客户端不能依赖“一次只变一个节点”或完成事件顺序来重建 DAG。

## 10. Session SSE

`GET /runs/{run_id}/sessions/{session_id}/events` 对 waterbody 和 run_summarizer 使用同一 envelope、sequence、Last-Event-ID、message started/delta/finalized 与 stream_end 协议。

客户端根据 session `agent_role` 解释 content types。`run_summarizer` 不绑定地图水体，不能因 `target_comid=null` 拒绝订阅或读取。首次订阅仍回放该 session 已有全部可见过程，然后连续接收新增。

## 11. Cancel

`POST /runs/{run_id}/cancel` 无 body，语义不变。M7 扩展内部覆盖：

- waiting/ready 未创建节点不再创建 session；
- running waterbody 和 summarizer session 都收到同一内部 cancellation token；
- completed/failed session 终态不改写；
- cancel 后无新 waterbody/topology/summary result；
- run snapshot 最终显示 cancelled，并在 quiesced/save 收敛后 stream_end。

不公开 retry failed node 或 retry summary API。重新分析仍是新的 run。

## 12. Public errors

M7 至少增加以下安全 code：

| code | failed_stage | retryable | 影响 |
|---|---|---:|---|
| `topology_execution_invalid` | `waterbody_scheduling` | false | run failed |
| `waterbody_dependency_invalid` | `waterbody_scheduling` | false | node failed；target 规则决定 run |
| `upstream_result_unavailable` | `evidence_acquisition` | true | node invalid/partial |
| `run_summary_failed` | `run_summarization` | true | summary failed，target 已完成则 run completed |
| `run_summary_invalid` | `run_summarization` | false | repair 后 summary failed |
| `summary_input_too_large` | `run_summarization` | false | summary failed，无静默裁剪 |
| `topology_result_invalid` | `result_assembly` | false | run failed |

自然 `insufficient_evidence` 仍是 result reason，不是 `PublicError`。failed upstream 对下游只投影 `invalid`，不复制这些 code/message。

## 13. M9 navigation 接缝

首版 M7 highlight 只保存稳定 source ref。M9 可使用现有 run/session/item routes 构造站内链接，例如打开：

1. 对应 run；
2. 对应 waterbody session；
3. result/transcript item 或全文；
4. 可选聚焦 referenced cell/window。

URL 是 UI routing concern，不进入 summary prompt/result。后端继续校验 ref 归属，前端不能把任意 Agent 文本解释成可导航 URL。
