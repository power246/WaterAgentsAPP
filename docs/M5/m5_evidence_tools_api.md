# M5 Evidence Tools + EvidenceBundle API / 读取契约

更新日期：2026-09-29

> 状态：设计已冻结。M5 不增加 production tool invocation endpoint；本文定义现有 run/session/transcript 路由如何读取 M5 typed content，以及只供自动化测试的独立调用面。分阶段实施、验证命令、完成记录和当前进度见 [M5 Milestone 0–13 Implementation TODO](m5_implementation_todo.md)。

## 1. Production 路由不变

M5 继续使用 M3/M4 已有路由：

```text
POST /api/analysis/runs
GET  /api/analysis/runs/{run_id}
GET  /api/analysis/runs/{run_id}/status
GET  /api/analysis/runs
GET  /api/analysis/runs/{run_id}/sessions/{session_id}
GET  /api/analysis/runs/{run_id}/sessions/{session_id}/transcript
GET  /api/analysis/runs/{run_id}/transcript/{item_id}/content
GET  /api/analysis/runs/{run_id}/events
GET  /api/analysis/runs/{run_id}/sessions/{session_id}/events
POST /api/analysis/runs/{run_id}/cancel
POST /api/analysis/runs/{run_id}/persistence/retry
```

production OpenAPI 中不得出现 `/tools`、`/evidence/collect` 或等价的直接调用路由。frontend 只读取已发生的 transcript/evidence/result，不提交 provider、station、半径、geometry、URL 或 tool policy。

M5 不改变 create request。所有新内容继续使用当前唯一 `schema_version: 1`；没有 `EvidenceFragmentV2`、`EvidenceBundleV2`、旧/新 union 或 milestone compatibility 字段。

## 2. 统一 transcript envelope

M5 typed content 仍装在 M3 的 transcript item 中：

```json
{
  "item_id": "019d1c65-1a23-7f2b-8c99-fcb5dc452003",
  "run_id": "019d1c64-fc79-7a9f-a011-e6910f29f001",
  "session_id": "019d1c65-0828-72cb-8b33-f1fc76628002",
  "sequence": 12,
  "content_type": "evidence_tool_result",
  "content_schema_version": 1,
  "occurred_at": "2026-09-29T02:12:42.031Z",
  "visibility": "public",
  "rag_eligible": false,
  "operation_id": "019d1c65-145e-77ef-a85e-608e489d5004",
  "content": {
    "load_state": "inline",
    "value": {}
  }
}
```

超过 M3 inline 阈值时 `content.load_state=referenced`，timeline 只返回 size、preview 与原全文引用；客户端通过既有 content route 获取完整内容。preview 不是完整事实，不得用于重新计算结果。

## 3. `evidence_tool_call v1`

示例：

```json
{
  "schema_version": 1,
  "tool_call_id": "call_01J8ZX98X10NV7XB5HCTQ5R6VA",
  "session_id": "019d1c65-0828-72cb-8b33-f1fc76628002",
  "tool_name": "collect_water_observations",
  "purpose": "local_station_preflight",
  "window_ids": ["w000", "w001"],
  "parameter_keys": [
    {"code_system": "usgs", "code": "00300"},
    {"code_system": "usgs", "code": "00060"}
  ],
  "options": {},
  "policy": "conditional_mandatory",
  "requested_at": "2026-09-29T02:12:40.000Z"
}
```

公开内容不包含 provider URL、API key、Authorization、任意 station list 或 canonical request hash。station/provider plan 属于 server-resolved provenance，可在有权限的完整内容中以脱敏 identity 表示。

## 4. `evidence_source_records v1`

一个 item 保存同一 operation 下的一批标准化记录：

```json
{
  "schema_version": 1,
  "operation_id": "019d1c65-145e-77ef-a85e-608e489d5004",
  "batch_id": "batch-0001",
  "provider": "usgs_waterdata",
  "channel": "continuous",
  "request_range": {
    "start": "2026-09-28T04:00:00Z",
    "end": "2026-09-30T04:00:00Z"
  },
  "manifest": {
    "page_identity": "page-0001",
    "record_count": 2,
    "response_bytes": 2187,
    "page_complete": true,
    "next_page": false
  },
  "records": [
    {
      "record_type": "water_observation",
      "record_id": "usgs-feature-id-1",
      "provider": "usgs_waterdata",
      "channel": "continuous",
      "source_uri": "https://api.waterdata.usgs.gov/ogcapi/v1/collections/continuous/items/usgs-feature-id-1",
      "retrieved_at": "2026-09-29T02:12:41.000Z",
      "observed_time": "2026-09-29T00:00:00Z",
      "location": {
        "point": {"latitude": 38.9497778, "longitude": -77.1276389},
        "comid": "12345678",
        "station_id": "USGS-01646500",
        "distance_to_target_km": 0.0
      },
      "parameter": {"code_system": "usgs", "code": "00300"},
      "value": "8.2",
      "raw_unit": "mg/L",
      "medium": "surface water",
      "fraction": "unfiltered",
      "method": null,
      "statistic": "instantaneous",
      "provider_quality": {
        "approval": "Approved",
        "qualifiers": []
      },
      "provenance": {
        "provider_series_id": "series-identity",
        "provider_record_id": "usgs-feature-id-1"
      }
    }
  ]
}
```

JSON numbers representing measured values are serialized as normalized decimal strings to avoid cross-language precision drift. Coordinates and confidence-like bounded display fields may remain JSON numbers under their domain rules.

`rag_eligible` for this content is always false。完整 provider envelope、response headers 与 secret 不写入公共正文。

## 5. `evidence_fragment v1`

### 5.1 Direct record

```json
{
  "schema_version": 1,
  "evidence_id": "ev_01J8ZXB3VDD1QJJPDHVA3G3G8R",
  "source": "usgs_waterdata",
  "collection_status": "available",
  "target_comid": "12345678",
  "location": {
    "point": {"latitude": 38.9497778, "longitude": -77.1276389},
    "comid": "12345678",
    "station_id": "USGS-01646500",
    "distance_to_target_km": 0.0
  },
  "time_range": {
    "start": "2026-09-29T00:00:00Z",
    "end": "2026-09-29T06:00:00Z"
  },
  "parameter_refs": [{"code_system": "usgs", "code": "00300"}],
  "data_type": "water_observation",
  "applicability": "direct_measurement",
  "relation_id": null,
  "series_identity": "sha256:series-digest",
  "quality_class": "valid",
  "quality_flags": [],
  "coverage": {
    "status": "partial",
    "observed_count": 1,
    "expected_count": null,
    "ratio": null,
    "largest_gap_seconds": null
  },
  "payload": {
    "payload_type": "direct_record",
    "record_ref": {
      "item_id": "019d1c65-17ab-78d0-a807-972e81864005",
      "record_id": "usgs-feature-id-1"
    },
    "displayed_value": "8.2",
    "displayed_unit": "mg/L"
  },
  "source_record_refs": [
    {
      "item_id": "019d1c65-17ab-78d0-a807-972e81864005",
      "record_id": "usgs-feature-id-1"
    }
  ],
  "derivation": null,
  "provenance": {
    "registry_digest": "sha256:manifest-digest",
    "operation_id": "019d1c65-145e-77ef-a85e-608e489d5004"
  },
  "rag_eligible": true
}
```

### 5.2 Aggregate with suspicious input

```json
{
  "schema_version": 1,
  "evidence_id": "ev_01J8ZXDGDJ6S1BEHCMCN9ZE92A",
  "source": "deterministic_aggregation",
  "collection_status": "available",
  "target_comid": "12345678",
  "location": {"point": null, "comid": "12345678", "station_id": "USGS-01646500", "distance_to_target_km": null},
  "time_range": {
    "start": "2026-09-29T00:00:00Z",
    "end": "2026-09-29T06:00:00Z"
  },
  "parameter_refs": [{"code_system": "usgs", "code": "00060"}],
  "data_type": "aggregate",
  "applicability": "direct_measurement",
  "relation_id": null,
  "series_identity": "sha256:series-digest",
  "quality_class": "suspicious",
  "quality_flags": ["estimated", "provider_flagged"],
  "coverage": {
    "status": "complete",
    "observed_count": 20,
    "expected_count": 24,
    "ratio": "0.8333333333333333333333333333",
    "largest_gap_seconds": 1800
  },
  "payload": {
    "payload_type": "aggregate",
    "value": "37.4",
    "unit": "ft3/s",
    "method": "mean",
    "input_record_refs": [
      {"item_id": "019d1c65-17ab-78d0-a807-972e81864005", "record_id": "usgs-feature-id-1"},
      {"item_id": "019d1c65-17ab-78d0-a807-972e81864005", "record_id": "usgs-feature-id-2"}
    ],
    "valid_input_count": 17,
    "suspicious_input_count": 3,
    "invalid_input_count": 0
  },
  "source_record_refs": [
    {"item_id": "019d1c65-17ab-78d0-a807-972e81864005", "record_id": "usgs-feature-id-1"},
    {"item_id": "019d1c65-17ab-78d0-a807-972e81864005", "record_id": "usgs-feature-id-2"}
  ],
  "derivation": {
    "algorithm": "decimal_mean",
    "algorithm_version": 1,
    "policy_id": "continuous_discharge",
    "registry_digest": "sha256:manifest-digest"
  },
  "provenance": {"operation_id": "019d1c65-145e-77ef-a85e-608e489d5004"},
  "rag_eligible": true
}
```

aggregate 仍是 direct-measurement applicability，因为输入是本地直接记录；这不表示 M6 最终结果可以标为 `observed`。任何多记录 aggregate 的最终 source 必须是 `inferred`。

### 5.3 Collection fact

```json
{
  "schema_version": 1,
  "evidence_id": "ev_01J8ZXEQABJ70DWA82HGKY47C3",
  "source": "usgs_waterdata",
  "collection_status": "empty",
  "target_comid": "12345678",
  "location": {"point": null, "comid": "12345678", "station_id": "USGS-01646500", "distance_to_target_km": null},
  "time_range": {
    "start": "2026-09-29T06:00:00Z",
    "end": "2026-09-29T12:00:00Z"
  },
  "parameter_refs": [{"code_system": "usgs", "code": "00300"}],
  "data_type": "collection_fact",
  "applicability": "contextual_only",
  "relation_id": null,
  "series_identity": "sha256:series-digest",
  "quality_class": null,
  "quality_flags": ["missing"],
  "coverage": {"status": "insufficient", "observed_count": 0},
  "payload": {
    "payload_type": "collection_fact",
    "status": "empty",
    "reason": "no_records_in_completed_query",
    "affected_window_ids": ["w001"],
    "record_refs": []
  },
  "source_record_refs": [],
  "derivation": null,
  "provenance": {"operation_id": "019d1c65-145e-77ef-a85e-608e489d5004"},
  "rag_eligible": false
}
```

empty/invalid/unavailable fact 必须可审计，但不能作为 direct/allowed evidence 解锁门禁。

## 6. `evidence_tool_result v1`

```json
{
  "schema_version": 1,
  "tool_call_id": "call_01J8ZX98X10NV7XB5HCTQ5R6VA",
  "operation_id": "019d1c65-145e-77ef-a85e-608e489d5004",
  "tool_name": "collect_water_observations",
  "execution_status": "completed",
  "collection_status": "available",
  "manifest_ref": {
    "item_id": "019d1c65-17ab-78d0-a807-972e81864005",
    "batch_ids": ["batch-0001"]
  },
  "fragment_refs": [
    {"item_id": "019d1c65-1a01-7890-a102-c9bc81c7a006", "evidence_id": "ev_01J8ZXB3VDD1QJJPDHVA3G3G8R"}
  ],
  "collection_fact_refs": [],
  "counts": {"requests": 1, "pages": 1, "records": 20, "fragments": 2},
  "deduplicated": false,
  "deduplicated_from_operation_id": null,
  "preview": {
    "available_windows": 1,
    "empty_windows": 1,
    "blocked_windows": 0,
    "quality": {"valid": 1, "suspicious": 1, "invalid": 0}
  },
  "safe_error": null,
  "completed_at": "2026-09-29T02:12:42.031Z"
}
```

失败时 `execution_status=failed`，`collection_status=unavailable` 或 `invalid`，并可引用已经安全持久化的 partial records/fragments；受影响窗口必须在 manifest/facts 中是 `blocked/incomplete_source`。取消使用 `cancelled`，不复用 failed。

## 7. `evidence_bundle v1`

```json
{
  "schema_version": 1,
  "bundle_id": "bundle_01J8ZY03M9VTS9B40B67JWTEPK",
  "agent_comid": "12345678",
  "context_ref": {"item_id": "019d1c64-ff01-74a1-a073-ff66ff342010"},
  "topology_ref": {"item_id": "019d1c65-010f-7528-b823-8e3c1c46d011"},
  "time_range": {"start": "2026-09-29T00:00:00Z", "end": "2026-09-29T06:00:00Z"},
  "analysis_timezone": "America/New_York",
  "time_resolution": "6h",
  "time_resolution_source": "frontend",
  "requested_parameters": [{"code_system": "usgs", "code": "00300"}],
  "supporting_parameters": [{"code_system": "usgs", "code": "00060"}],
  "window_refs": ["w000"],
  "cells": [
    {
      "parameter": {"code_system": "usgs", "code": "00300"},
      "window_id": "w000",
      "role": "target",
      "direct_fragment_refs": ["ev_01J8ZXB3VDD1QJJPDHVA3G3G8R"],
      "allowed_indirect_fragment_refs": [],
      "contextual_fragment_refs": [],
      "collection_fact_refs": [],
      "status": "supported",
      "reasons": []
    },
    {
      "parameter": {"code_system": "usgs", "code": "00060"},
      "window_id": "w000",
      "role": "required_support",
      "direct_fragment_refs": ["ev_01J8ZXDGDJ6S1BEHCMCN9ZE92A"],
      "allowed_indirect_fragment_refs": [],
      "contextual_fragment_refs": [],
      "collection_fact_refs": [],
      "status": "supported",
      "reasons": ["suspicious_inputs_present"]
    }
  ],
  "fragment_refs": [
    {"item_id": "019d1c65-1a01-7890-a102-c9bc81c7a006", "evidence_id": "ev_01J8ZXB3VDD1QJJPDHVA3G3G8R"},
    {"item_id": "019d1c65-1b03-7994-8654-810497c14007", "evidence_id": "ev_01J8ZXDGDJ6S1BEHCMCN9ZE92A"}
  ],
  "source_record_item_refs": ["019d1c65-17ab-78d0-a807-972e81864005"],
  "tool_result_refs": [{"item_id": "019d1c65-1d05-7520-a145-f6666cc6d008"}],
  "quality_summary": {"valid": 1, "suspicious": 1, "invalid": 0},
  "collection_summary": {"available": 2, "empty": 0, "unavailable": 0, "invalid": 0},
  "created_at": "2026-09-29T02:12:45.000Z"
}
```

bundle 只引用 source/fragment 原事实，不复制 raw arrays。每个目标 parameter/window 必须存在对应 discharge cell；缺少时 bundle schema 校验失败，而不是由 M6 猜测。

## 8. Geometry 对 `topology_ready v1` 的原子扩展

当前 V1 node 增加：

```json
{
  "comid": "12345678",
  "point": {"latitude": 38.95, "longitude": -77.13},
  "geometry_ref": "geometry:12345678",
  "geometry_summary": {
    "geometry_type": "linestring",
    "length_km": "14.37",
    "bbox": [-77.24, 38.89, -77.02, 39.01],
    "simplified_coordinates": [[-77.24, 38.89], [-77.13, 38.95], [-77.02, 39.01]],
    "simplification_tolerance_m": "25"
  }
}
```

同一 `topology_ready v1` 的 `geometries` map 保存一次完整 coordinates。API 不提供 V1/V2 union；M4–M9 当前 producers、fixtures、deserializers 和消费者一起更新。

## 9. inspection 分页结果

`inspect_evidence_records` 只返回已保存 records 的有界页面：

```json
{
  "tool_call_id": "call_01J8ZY72ZQW89AVVXTM9R25XBN",
  "operation_id": "019d1c66-0800-7eb1-9b01-c7a5893e8012",
  "execution_status": "completed",
  "collection_status": null,
  "page": {
    "source_item_id": "019d1c65-17ab-78d0-a807-972e81864005",
    "series_identity": "sha256:series-digest",
    "offset": 0,
    "limit": 50,
    "returned": 20,
    "next_offset": null,
    "records": ["bounded normalized record projections"]
  }
}
```

`limit` 默认 50、最大 200；排序固定为 `(observed_time, record_id)`。该工具不重新访问 provider，不改变原 records，也不将 inspection page 设为 RAG eligible。

## 10. session SSE

M5 不新增 SSE event name。可见内容继续用：

```text
event: transcript_item
id: <session-local integer cursor>
data: <TranscriptItemRead>
```

tool call/result/fragment/bundle 在相应 item 完成后发送；source-record 大正文通常发送 reference + bounded preview。run SSE 仍是完整轻量 snapshot，不复制 tool 或 raw evidence。

断线重连、`Last-Event-ID`、`stream_end`、全文读取、保存 warning 与 cancel 边界完全沿用 M3。frontend 不根据 tool item 自己判定分析终态；session/run lifecycle 才是终态来源。

## 11. History 与前端消费

前端在 M9 只需：

- 按 content type 展示 call、result、fragment、bundle 摘要；
- 区分 `available / empty / unavailable / invalid`；
- 展示 direct/allowed-indirect/contextual、quality、coverage、原/规范单位和公开 provenance；
- 通过 item/reference 读取完整内容，不要求 SSE 携带无界 raw data；
- 将 `suspicious` 显示为可用但有警告，不等同 invalid；
- 不把 aggregate 显示成一条原始 observed 读数；
- 不把 facility/weather/history 提升成目标水体直接观测；
- 不把 upstream indeterminate discharge 显示为 0。

M5 阶段不修改当前 frontend，也不接入 test route。

## 12. Public error

错误继续使用 M3 `AnalysisRunErrorResponse` envelope：

```json
{
  "error": {
    "code": "evidence_provider_unavailable",
    "message": "Required evidence could not be retrieved from the configured provider.",
    "request_id": "req_...",
    "run_id": "019d1c64-fc79-7a9f-a011-e6910f29f001",
    "session_id": "019d1c65-0828-72cb-8b33-f1fc76628002",
    "error_id": "err_...",
    "retryable": true
  }
}
```

自然 empty 不产生 HTTP error；它作为成功 tool result + collection fact 保存。provider unavailable/invalid、scope violation、budget exceeded 等产生 typed error/result，并按 workflow 所在阶段决定 session/run 技术终态。

## 13. 仅测试 HTTP 调用面

独立 `create_m5_test_app()` 可以挂载：

```text
POST /__test__/m5/evidence-tools:execute
GET  /__test__/m5/runs/{run_id}/transcript
```

请求只允许：

```json
{
  "fixture_scope_id": "station-backed-two-windows",
  "tool_name": "collect_water_observations",
  "window_ids": ["w000", "w001"],
  "parameter_keys": [
    {"code_system": "usgs", "code": "00300"},
    {"code_system": "usgs", "code": "00060"}
  ],
  "options": {}
}
```

fixture scope 在 test composition 中映射为合法 run/session/context/graph。请求不接受 provider、base URL、secret、任意 COMID/station/geometry 或关闭写入/授权的开关。

这些路由：

- 不在 production module import graph；
- 不进入 production OpenAPI snapshot；
- 不通过环境变量在 production 启用；
- 只用于 contract/integration tests；
- 与 application 直接测试复用同一个 `EvidenceToolExecutor`。

## 14. Contract 验收

实现后至少锁定：

- production OpenAPI 精确不含 test/tool invocation 路径；
- 五类 content 的 schema、正反示例、unknown schema corruption handling；
- inline/referenced 两种读取与 SSE preview 内容一致；
- public projection 无 secret、raw provider body、任意 URL 或内部异常；
- fragment/bundle/reference closure 可从历史 route 完整读取；
- suspicious aggregate、empty、provider partial failure、cancel、dedup 均有序列 fixture；
- geometry 只保存一份，node 摘要引用正确；
- 当前 V1 consumers 同步更新，不存在旧字段兼容分支；
- test app 可调用，production frontend/route 完全不接入。
