# M8 Memory/RAG API / 读取契约

更新日期：2026-10-07

> 状态：本文定义 M8 的目标读取协议，不表示代码已经实现。M8 不增加 direct RAG route；frontend 与调用方继续使用既有 run/session/transcript/history/SSE API。

## 1. Production routes 不变

```text
POST /api/analysis/runs
GET  /api/analysis/runs
GET  /api/analysis/runs/{run_id}
GET  /api/analysis/runs/{run_id}/events
POST /api/analysis/runs/{run_id}/cancel
POST /api/analysis/runs/{run_id}/persistence/retry
GET  /api/analysis/runs/{run_id}/sessions/{session_id}
GET  /api/analysis/runs/{run_id}/sessions/{session_id}/transcript
GET  /api/analysis/runs/{run_id}/sessions/{session_id}/events
GET  /api/analysis/runs/{run_id}/transcript/{item_id}/content
```

production OpenAPI 不得出现 `/rag/search`、`/memory/search`、`/embeddings`、`/index/jobs`、`/reindex` 或等价 route。客户端不能直接传 COMID、ParameterKey、window、topology 范围、source run/item、embedding text/profile 或 SQL 来搜索历史。

index generation/job/lag/供应商 response 属于内部基础设施；M9 若公开 admin/settings/cleanup surface，必须另行定义授权和状态协议。

## 2. Create request 与 memory mode

M8 不修改 M4 已冻结的 create schema。调用方继续显式选择：

```json
{
  "query": "Predict dissolved oxygen near the selected river next week",
  "location": "Austin, Texas",
  "date_range": {"start": "2026-10-08", "end": "2026-10-14"},
  "time_resolution": "12h",
  "parser_model": {"provider": "openai", "model": "configured-parser"},
  "waterbody_model": {"provider": "openai", "model": "configured-waterbody"},
  "memory_mode": "memory_augmented"
}
```

- `stateless`：waterbody session 的 tool roster 中没有 `search_transcript_evidence`；同 run 的 M7 direct-upstream evidence 不受影响。
- `memory_augmented`：允许 waterbody Agent 在 bounded loop 中显式调用该 tool；不保证一定调用或命中。

客户端不能选择 embedding model。create receipt、run detail、session input按既有 configuration/read model回显固定的 `memory_mode`，不回显 active embedding profile、vector或 canonical text。

## 3. `search_transcript_evidence` call

M8 复用 `evidence_tool_call v1`，给 `EvidenceToolOptions` 增加唯一历史字段 `historical_search_strategy`；参数、window和limit复用已有 typed fields：

```json
{
  "schema_version": 1,
  "tool_call_id": "call_01M8HISTORY0000000000000001",
  "session_id": "20000000-0000-4000-8000-000000000001",
  "tool_name": "search_transcript_evidence",
  "purpose": "compare_similar_historical_case",
  "window_ids": ["window-0001"],
  "parameter_keys": [
    {"code_system": "usgs", "code": "00300"}
  ],
  "options": {
    "record_limit": 5,
    "historical_search_strategy": "semantic_case_query"
  },
  "policy": "producer_slot",
  "requested_at": "2026-10-07T09:00:00Z"
}
```

允许的 strategy：

```text
exact_comid_parameter_window
nearby_comid_parameter
parameter_season
semantic_case_query
topology_neighborhood_parameter
```

Agent 只提交 strategy、当前 scope 内 ParameterKeys/window IDs和 `record_limit=1..5`。后端从 sealed scope填写/验证 current COMID、时间、season、nearby/topology set、profile和 canonical query。options 中其他工具专用字段、source refs、自由 query、半径、hop或 COMID 与 historical search互斥；非法组合在 preflight拒绝，不写合法 call/result/source item。

## 4. Historical source DTO

`evidence_source_records v1.records[]` 继续使用 `record_type="historical_transcript"`，但 M8 将占位字段收敛为完整 case identity与有界 result projection：

```json
{
  "record_type": "historical_transcript",
  "record_id": "sha256:historical-record-identity",
  "provider": "wateragents_transcript",
  "channel": "historical_memory",
  "source_uri": null,
  "retrieved_at": "2026-10-07T09:00:01Z",
  "observed_time": null,
  "valid_time_range": {
    "start": "2026-07-10T00:00:00Z",
    "end": "2026-07-17T00:00:00Z"
  },
  "location": {
    "point": null,
    "comid": "12345678",
    "station_id": null,
    "distance_to_target_km": 0.0
  },
  "provider_quality": {"approval": "validated_transcript", "qualifiers": []},
  "raw_unit": null,
  "provenance": {
    "operation_id": "30000000-0000-4000-8000-000000000001",
    "registry_digest": "sha256:rag-policy-digest",
    "request_identity": "sha256:authorized-search-identity"
  },
  "case_ref": {
    "original_run_id": "10000000-0000-4000-8000-000000000010",
    "original_session_id": "20000000-0000-4000-8000-000000000010",
    "target_comid": "12345678",
    "analysis_context_item_id": "40000000-0000-4000-8000-000000000010",
    "result_item_id": "60000000-0000-4000-8000-000000000010"
  },
  "content_type": "inference_result",
  "content_schema_version": 1,
  "matched_by": {
    "strategy": "semantic_case_query",
    "parameter_overlap_ratio": 1.0,
    "vector_similarity": 0.92,
    "lexical_similarity": 0.61
  },
  "rag_score": 0.8735,
  "original_quality": "valid",
  "original_confidence": 0.72,
  "result_projection": {
    "mode": "prediction",
    "source_completed_at": "2026-07-09T18:20:00Z",
    "overall_risk": "medium",
    "availability": "partial",
    "cells": [
      {
        "parameter": {"code_system": "usgs", "code": "00300"},
        "window_id": "historical-window-0001",
        "time_range": {
          "start": "2026-07-10T00:00:00Z",
          "end": "2026-07-17T00:00:00Z"
        },
        "outcome_status": "directional",
        "trend": "falling",
        "risk_level": "medium",
        "confidence": 0.72,
        "source": "predicted"
      }
    ]
  },
  "supporting_fragments": [
    {
      "original_item_id": "50000000-0000-4000-8000-000000000010",
      "evidence_id": "ev_historical_001",
      "parameter": {"code_system": "usgs", "code": "00300"},
      "valid_time_range": {
        "start": "2026-07-10T00:00:00Z",
        "end": "2026-07-17T00:00:00Z"
      },
      "quality_class": "valid",
      "applicability": "contextual_only"
    }
  ]
}
```

示例只展示结构，不授权新的风险/结果枚举；`result_projection.cells[]` 必须复用 M6 当前 `InferenceResult` 的真实 cell DTO/enum与 decimal规则。每个 record最多两个 supporting fragments，且只覆盖 call请求的 ParameterKey/window。不得返回完整历史 query、完整 run/transcript、其他 waterbody结果、prompt、reasoning或无关 cells。

`case_ref` 是特殊跨 run reference，不属于普通同 run `transcript_item_refs`。reader/hydrator使用五元身份校验，不允许只凭 `original_run_id` 选择一个答案。

## 5. Historical evidence fragment

M5 materializer从每个返回 cell生成 `evidence_fragment v1`，保持：

```json
{
  "schema_version": 1,
  "evidence_id": "ev_01M8HISTORY0000000000000001",
  "source": "wateragents_transcript",
  "collection_status": "available",
  "target_comid": "12345678",
  "time_range": {
    "start": "2026-07-10T00:00:00Z",
    "end": "2026-07-17T00:00:00Z"
  },
  "parameter_refs": [{"code_system": "usgs", "code": "00300"}],
  "data_type": "historical_inference",
  "applicability": "contextual_only",
  "quality_class": "valid",
  "quality_flags": [],
  "payload": {
    "payload_type": "historical_inference",
    "case_ref": {
      "original_run_id": "10000000-0000-4000-8000-000000000010",
      "original_session_id": "20000000-0000-4000-8000-000000000010",
      "target_comid": "12345678",
      "analysis_context_item_id": "40000000-0000-4000-8000-000000000010",
      "result_item_id": "60000000-0000-4000-8000-000000000010"
    },
    "historical_window_id": "historical-window-0001",
    "cell_projection": {
      "outcome_status": "directional",
      "trend": "falling",
      "risk_level": "medium",
      "confidence": 0.72,
      "source": "predicted"
    },
    "supporting_fragment_refs": [
      {"item_id": "50000000-0000-4000-8000-000000000010", "evidence_id": "ev_historical_001"}
    ]
  },
  "source_record_refs": [],
  "derivation": null,
  "provenance": {
    "operation_id": "30000000-0000-4000-8000-000000000001",
    "registry_digest": "sha256:rag-policy-digest"
  },
  "result_eligibility": "inferred_only",
  "rag_eligible": false
}
```

当前 session中由历史命中生成的 wrapper不得再次成为下一代 memory source，避免递归放大；其 `rag_eligible=false`。原 historical result/eligible fragment仍在原 run独立作为 source。历史 wrapper不能进入 direct/allowed-indirect gate，也不能使 final source变成 observed。

`HistoricalInferencePayload.original_item_ref + projection:string` 占位应替换为 typed `case_ref/window/cell/supporting refs`。跨 run refs使用专门 validator；普通 `referenced_item_ids()` 不得把它们当作当前 run item校验。

## 6. Tool result 与安全失败

成功有命中沿用 M5 `evidence_tool_result v1`：

```json
{
  "schema_version": 1,
  "tool_call_id": "call_01M8HISTORY0000000000000001",
  "operation_id": "30000000-0000-4000-8000-000000000001",
  "tool_name": "search_transcript_evidence",
  "execution_status": "completed",
  "collection_status": "available",
  "manifest_refs": [{"item_id": "51000000-0000-4000-8000-000000000001"}],
  "fragment_refs": [
    {"item_id": "52000000-0000-4000-8000-000000000001", "evidence_id": "ev_01M8HISTORY0000000000000001"}
  ],
  "collection_fact_refs": [],
  "source_record_refs": [
    {"item_id": "51000000-0000-4000-8000-000000000001", "record_id": "sha256:historical-record-identity"}
  ],
  "counts": {"requests": 1, "pages": 1, "records": 1, "fragments": 1},
  "deduplicated": false,
  "deduplicated_from_operation_id": null,
  "deduplicated_from_result_ref": null,
  "safe_error": null,
  "completed_at": "2026-10-07T09:00:01Z"
}
```

状态矩阵：

| 场景 | execution_status | collection_status | safe error code |
|---|---|---|---|
| 正常有命中 | completed | available | null |
| 正常无命中 | completed | empty | null |
| 首代索引构建中 | failed | unavailable | `rag_index_building` |
| search/database暂不可用 | failed | unavailable | `rag_search_unavailable` |
| semantic embedding超时/限流 | failed | unavailable | `embedding_unavailable` |
| profile/dimension/vector协议错误 | failed | invalid | `embedding_protocol_invalid` |
| active generation/schema损坏 | failed | invalid | `rag_index_invalid` |
| source hydration/ref/schema不一致 | failed | invalid | `historical_source_invalid` |
| 当前 run取消 | cancelled | unavailable或null | 既有 cancellation code |

safe error不包含 provider raw body、URL、canonical query、vector、credential、SQL、source全文或 traceback。历史失败是 optional failure；只要 current required evidence gate由其他合法 evidence满足，session可继续。它不能被显示为“没有历史数据”。

## 7. Transcript、history 与 SSE

M8 不增加第三条 SSE或新 event name。实际 call/source/fragment/result/bundle作为既有 `transcript_item` frame出现在：

```text
GET /api/analysis/runs/{run_id}/sessions/{session_id}/events
```

run SSE仍只投影 run/session/persistence状态。session transcript/history与live SSE使用同一 content types/schema；inline/reference threshold、preview、unknown schema、corrupt item、`view=auto/history`和全文读取沿用 M3。

读取当前 historical source item全文：

```text
GET /api/analysis/runs/{current_run_id}/transcript/{historical_source_item_id}/content
```

读取原 result全文时使用 record中后端给出的 original run/result identity：

```text
GET /api/analysis/runs/{original_run_id}/transcript/{result_item_id}/content?view=history
```

客户端必须保持 run+item配对，不能把 source item ID放进当前 run URL。原 source已被合法清理时，不允许 retained consumer留下悬空 ref；M9 cleanup必须事前 conflict或显式级联整个 dependent run。

public timeline/SSE preview可显示：strategy、命中数量、原 run/session/result refs、COMID、参数/window、rag score components、原 confidence/quality、bounded result cells和“historical/contextual”标签。不得显示 canonical text、embedding/vector、full prompt、worker job或供应商诊断。

## 8. Result/read projection

M8 不给 `InferenceResult` 增加“RAG算出的最终值”。历史 fragments仅通过既有 evidence refs、attribution与reasoning decision进入最终可审计链；M6 result validator仍决定哪些 refs可被引用，并禁止 historical evidence解锁 target/discharge gate。

session detail、run detail与`topology_run_result`结构不因M8增加独立 memory result。run summarizer不执行搜索；如果 waterbody最终结果引用了合法 historical fragment，summary只读取已验证 waterbody result projection，不重新展开原历史。

## 9. Frontend/M9 接力

M9 frontend只需：

- 从 run configuration显示 `stateless/memory_augmented`；
- 在 waterbody timeline中把 historical tool call、empty、unavailable/invalid、source record和contextual fragment与当前观测区分；
- 显示原 run/session/result identity、原时间、原 confidence/quality和检索匹配原因；
- 不把 `rag_score`显示为水质置信度或风险分；
- 不提供自由RAG搜索框、embedding选项或direct search请求；
- 原全文跳转只使用后端稳定 refs，并正确处理cleanup conflict/已授权级联后的整体历史消失。

M9 admin/cleanup、embedding profile变更、generation/reindex状态不属于本文已公开route，必须在M9 API设计中另行冻结。

## 10. OpenAPI/contract 验收

M8实现后必须证明：production route集合不增加；create schema仍只暴露既有 `memory_mode`；五种 strategy只存在于Agent tool schema；historical DTO严格拒绝extra/未知enum/NaN/跨run错配；SSE与history round-trip一致；stateless不产生historical items；所有安全失败使用现有tool result路径而不是新增HTTP error surface。
