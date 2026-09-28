# M4 Query Understanding + Hydrology Topology API / 前端接入

更新日期：2026-09-27

> 状态：Milestone 9 协议与 Milestone 10 本机集成审计已完成。本文在 M3 API 基线上定义最终 create request、M4 typed content 和读模型扩展；未列出的幂等、历史、全文、取消、补写和 SSE 规则继续以 [M3 API 文档](../M3/m3_transcript_history_api.md) 为准，实际证据与仍未运行的外部门禁见 [M4 Implementation TODO](m4_implementation_todo.md)。

## 1. 路由与传输不变

M4 不增加 endpoint，继续使用：

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

JSON 仍严格校验、未知字段拒绝。时间戳均是带 `Z` 的 UTC；用户日期是 `YYYY-MM-DD`。错误继续使用 M3 `AnalysisRunErrorResponse`，所有响应保留 `X-Request-ID`。

## 2. 创建分析

### 2.1 最终 request

`POST /api/analysis/runs` 继续要求 `Content-Type: application/json`，前端主动提交使用新的 UUID v4 `Idempotency-Key`：

```json
{
  "query": "Will dissolved oxygen become unsafe at this reach after tomorrow's heat?",
  "target": {
    "comid": "12345678",
    "coordinates": {
      "latitude": 38.8719,
      "longitude": -77.0479
    }
  },
  "date_range": {
    "start_date": "2026-09-26",
    "end_date": "2026-09-28"
  },
  "time_resolution": "auto",
  "parser_model": {
    "provider": "openai",
    "model": "parser-model-id"
  },
  "waterbody_model": {
    "provider": "openai",
    "model": "waterbody-model-id"
  },
  "memory_mode": "stateless"
}
```

字段规则：

| 字段 | 规则 |
|---|---|
| `query` | 必填非空字符串。参数只从 query 理解；前端没有参数表单。 |
| `target` | 可省略；存在时 `comid`、`coordinates` 至少一项。前端地图正常同时发送；后端交叉校验。 |
| `target.comid` | 正十进制字符串，不接受 number，避免前导零/精度语义混乱。 |
| `coordinates` | `latitude` 为 `[-90,90]`、`longitude` 为 `[-180,180]` 的严格有限数。 |
| `date_range` | 可省略；存在时 start/end 必须同时存在、start ≤ end，且均在 run 当地 `today±7` 闭区间。 |
| `time_resolution` | `auto / 6h / 12h / 24h`；前端显式默认 `auto`。字段省略只为非前端 API client 提供 `12h/default`；显式 `null` 非法。 |
| `parser_model` | 必填；必须是模型目录中可用于 `query_parser` 的 provider/model。 |
| `waterbody_model` | 必填；必须是模型目录中可用于 `waterbody` 的 provider/model。 |
| `memory_mode` | 必填 `stateless / memory_augmented`。最终系统由 M8 实现后者；M4 不加临时拒绝或假行为。 |

前端不提交 timezone、mode、`ParameterKey`、API key、topology limits 或 evidence relation。timezone 由服务端设置在 run 创建时固化；mode、参数和实际 resolution 由后端产生。

### 2.2 可选结构化字段的 fallback

- target 有值：使用前端值；query 中另有实质不同地点则失败 `location_conflict`。
- target 省略：parser 必须从 query 得到唯一地点；否则失败 `location_required` 或 `location_ambiguous`。
- date_range 有值：使用前端值；query 中另有实质不同日期则失败 `date_range_conflict`。
- date_range 省略：parser 必须得到完整日期范围；否则失败 `date_range_required`。
- 参数没有前端 fallback；parser 必须从 query 明确提取或自主推测，并由 capability registry 验证。

结构化输入优先不等于忽略冲突。发生 conflict 时不静默覆盖、合并或任选，当前 run 失败并提示用户修正后创建新 run。

### 2.3 创建响应与幂等

请求形状、structured dates 和模型目录在创建前通过后，成功仍返回 `201` 的原创建回执。示例：

```json
{
  "run_id": "0d415090-860a-4e1c-b843-191968dbcc6e",
  "status": "pending",
  "created_at": "2026-09-25T08:00:00Z",
  "updated_at": "2026-09-25T08:00:00Z",
  "configuration": {
    "analysis_timezone": "America/New_York",
    "time_resolution_preference": "auto",
    "time_resolution_preference_source": "frontend",
    "parser_model": {
      "provider": "openai",
      "model": "parser-model-id"
    },
    "waterbody_model": {
      "provider": "openai",
      "model": "waterbody-model-id"
    },
    "memory_mode": "stateless"
  }
}
```

M3 的幂等规则不变：同 key 只有原始 request bytes 完全相同才返回同一回执；字段顺序、空白、显式/省略默认不同均视为不同 body。服务端先查询 key/body：已有且相同就返回原回执，不因日期已跨日或模型后来退出目录而重新拒绝；已有但 body 不同仍冲突。只有全新 key 才使用当次 Clock 和模型目录做动态校验。M4 直接替换阶段性 create schema，不接受旧 M3 request 作为新建兼容输入。

## 3. 创建前 `422` 校验

示例：

```json
{
  "error": {
    "error_id": "67fd3072-4050-4518-b476-cc895012467d",
    "code": "request_validation_failed",
    "message": "The analysis request is invalid.",
    "failed_stage": "request_validation",
    "retryable": false,
    "request_id": "fae68dfd-c483-4376-ad30-136a9f7e2438",
    "run_id": null,
    "session_id": null
  },
  "run": null,
  "validation": [
    {
      "field": ["date_range", "end_date"],
      "type": "date_range_out_of_bounds",
      "message": "Choose a date from 2026-09-18 through 2026-10-02."
    }
  ],
  "history_url": null
}
```

创建前稳定 violation type：

| type | 条件 |
|---|---|
| `target_value_required` | target 存在但 COMID/coordinates 都没有。 |
| `invalid_comid` | COMID 为空、非十进制字符串或不是正值。 |
| `invalid_coordinates` | 经纬度类型、有限性或范围错误。 |
| `date_range_incomplete` | start/end 只提供一个。 |
| `date_range_reversed` | start > end。 |
| `date_range_out_of_bounds` | structured date 超出当天前后各 7 天。 |
| `invalid_time_resolution` | null、未知值或错误类型。 |
| `model_selection_required` | 任一模型选择缺失。 |
| `model_selection_unsupported` | provider/model 不在对应 role 的可选目录。 |
| `invalid_memory_mode` | 未知或错误类型。 |
| `extra_forbidden` | 出现未定义字段。 |

动态日期消息中的边界来自拟创建 run 使用的服务端 timezone 与同一次 Clock 快照。校验失败无 run_id，不写 transcript；已存在的同 key/同 bytes 重试按 §2.3 返回原回执，不重跑动态校验。

## 4. M4 typed content

公共 envelope、inline/reference、全文读取、scope、correlation、metadata 和 parse status 全部沿用 M3。M4 只增加下面的 body schema。

### 4.1 `analysis_request v1`

run-owned，正文是通过严格校验后的最终 create request，保留字段显式/省略语义所需的信息。M4 所需字段直接加入当前 v1，registry、读写模型、fixture 和调用方同步更新；不另建 M4 请求版本。

### 4.2 `lifecycle_event v2`

run/session lifecycle 的公开事件语义和状态枚举不变，继续使用当前 v2。唯一 run configuration 直接加入 resolution preference、双模型选择和 memory mode，不含 configuration schema version，也不建立 M3/M4 read union。M1–M9 是同一内部实施过程，开发数据和 fixture 与当前模型一起迁移或重建。

### 4.3 `query_parser_input v1`

parser-session-owned、非流式：

```json
{
  "prompt_template_version": 1,
  "response_schema_version": 1,
  "model": {
    "provider": "openai",
    "model": "parser-model-id"
  },
  "messages": [
    {
      "role": "system",
      "text": "<safe rendered parser instructions>"
    },
    {
      "role": "user",
      "text": "<safe rendered request and structured selections>"
    }
  ],
  "tools": []
}
```

这是实际送给 provider 的安全输入表示。任何凭据在进入 live buffer 前已脱敏；`tools` 必须为空。若触发一次 schema repair，repair 使用新的 operation，并额外保存一份与第二次真实调用逐字段一致的 `query_parser_input v1`；不能只保存首轮输入。

### 4.4 `query_parser_message v1`

parser-session-owned、可流式：

```json
{
  "text": "{\"location_mention\":null,\"coordinate_mention\":null,\"start_date\":null,\"end_date\":null,\"parameters\":[{\"concept\":\"dissolved oxygen\",\"rationale\":\"Dissolved oxygen is explicitly requested.\",\"explicitly_named\":true}],\"ambiguities\":[]}",
  "generation_status": "completed",
  "end_reason": "completed"
}
```

该正文是 provider 实际生成的安全文本，结构化调用通常表现为 JSON，不是额外再调用一次模型生成的用户说明。实时传输使用 M3 `message_started / message_delta / message_finalized`，正文最终只保存一次；前端不能从未完成 JSON 提前取业务值。schema repair 若发生，使用新的 message item，不覆盖第一次实际输出。

### 4.5 `query_understood v1`

parser-session-owned，正文是唯一 `AnalysisContext`：

```json
{
  "schema_version": 1,
  "query": "Will dissolved oxygen become unsafe at this reach after tomorrow's heat?",
  "decision_instant": "2026-09-25T08:00:00Z",
  "analysis_timezone": "America/New_York",
  "target": {
    "comid": "12345678",
    "point": {
      "latitude": 38.8719,
      "longitude": -77.0479
    },
    "display_name": "Selected river reach",
    "country_code": "US",
    "subdivision_code": "US-DC",
    "source": "frontend"
  },
  "requested_date_range": {
    "start_date": "2026-09-26",
    "end_date": "2026-09-28"
  },
  "time_range": {
    "start": "2026-09-26T04:00:00Z",
    "end": "2026-09-29T04:00:00Z"
  },
  "time_segments": [
    {
      "segment_index": 0,
      "mode": "prediction",
      "time_range": {
        "start": "2026-09-26T04:00:00Z",
        "end": "2026-09-29T04:00:00Z"
      }
    }
  ],
  "mode": "prediction",
  "time_resolution": "6h",
  "time_resolution_source": "auto",
  "params_used": [
    {
      "key": {
        "code_system": "usgs",
        "code": "00300"
      },
      "canonical_name": "dissolved oxygen",
      "canonical_unit": "mg/L",
      "source": "explicit",
      "rationale": "Dissolved oxygen is explicitly requested and is the direct indicator for the stated oxygen-safety risk."
    }
  ],
  "supporting_params": [
    {
      "key": {
        "code_system": "usgs",
        "code": "00060"
      },
      "canonical_name": "discharge",
      "canonical_unit": "ft3/s",
      "reason": "system_required"
    }
  ],
  "parser_model": {
    "provider": "openai",
    "model": "parser-model-id"
  },
  "waterbody_model": {
    "provider": "openai",
    "model": "waterbody-model-id"
  },
  "memory_mode": "stateless"
}
```

前端可以在收到该 `transcript_item` 后立即显示“系统理解”：目标、日期、实际粒度、`params_used`、每项目标的简短 rationale，以及系统自动加入的 `supporting_params`。discharge 已经是用户目标时不在 supporting 列表复制。解析成功后自动继续 topology，不等待用户确认。

### 4.6 `topology_ready v1`

run-owned，正文是唯一 `HydrologyGraph`。以下是 target 本身已有合格 station 的最小完整示例：

```json
{
  "schema_version": 1,
  "target_comid": "12345678",
  "coverage": "complete",
  "nodes": [
    {
      "comid": "12345678",
      "point": {
        "latitude": 38.8719,
        "longitude": -77.0479
      },
      "depth": 0,
      "cumulative_distance_km": 0.0,
      "is_target": true,
      "is_inlet": true,
      "participation": "active",
      "qualified_units": [
        {
          "parameter": {
            "code_system": "usgs",
            "code": "00060"
          },
          "segment_index": 0
        },
        {
          "parameter": {
            "code_system": "usgs",
            "code": "00300"
          },
          "segment_index": 0
        }
      ],
      "station_refs": [
        {
          "provider": "wdfn_time_series",
          "station_id": "USGS-01234567",
          "series_refs": ["series-safe-identifier"]
        }
      ],
      "excluded_reasons": []
    }
  ],
  "edges": [],
  "branch_outcomes": [
    {
      "branch_root_comid": "12345678",
      "coverage_unit": {
        "parameter": {
          "code_system": "usgs",
          "code": "00060"
        },
        "segment_index": 0
      },
      "outcome": "qualified",
      "stop_comid": "12345678",
      "path_to_target": ["12345678"],
      "reason": "target_station_qualified",
      "blocked_upstream_comid": null
    },
    {
      "branch_root_comid": "12345678",
      "coverage_unit": {
        "parameter": {
          "code_system": "usgs",
          "code": "00300"
        },
        "segment_index": 0
      },
      "outcome": "qualified",
      "stop_comid": "12345678",
      "path_to_target": ["12345678"],
      "reason": "target_station_qualified",
      "blocked_upstream_comid": null
    }
  ],
  "execution_layers": [
    ["12345678"]
  ],
  "limits": {
    "max_distance_km": 100.0,
    "max_hops": 12,
    "max_nodes": 128
  },
  "stats": {
    "discovered_nodes": 1,
    "queried_nodes": 1,
    "max_depth": 0,
    "max_distance_km": 0.0,
    "qualified_inlets": 1,
    "excluded_nodes": 0
  }
}
```

大图按 M3 64 KiB 规则返回 preview/reference，前端再用全文 endpoint 获取。地图只依据 graph 原事实绘制；不能从 session 数量反推节点或排除原因。距离或全图节点上限在 child 进入图前阻止扩展时，`stop_comid/path_to_target` 仍描述已发现的 parent 路径，`blocked_upstream_comid` 单独记录该 child；因此多个同时受限的支流不会被集合去重合并。

## 5. History 与 detail 投影

`RunSummary` 增加 nullable `context_summary`：

```json
{
  "target_comid": "12345678",
  "location": {
    "latitude": 38.8719,
    "longitude": -77.0479,
    "display_name": "Selected river reach"
  },
  "date_range": {
    "start_date": "2026-09-26",
    "end_date": "2026-09-28"
  },
  "mode": "prediction",
  "time_resolution": "6h",
  "params_used": [
    {
      "code_system": "usgs",
      "code": "00300",
      "canonical_name": "dissolved oxygen",
      "source": "explicit",
      "rationale": "Dissolved oxygen is explicitly requested and is the direct indicator for the stated oxygen-safety risk."
    }
  ],
  "supporting_params": [
    {
      "code_system": "usgs",
      "code": "00060",
      "canonical_name": "discharge"
    }
  ]
}
```

在 `query_understood` 尚未产生或解析失败时为 `null`。一旦 context 存在，auto/history 都从同一 typed item 投影；不从 query、configuration 或 topology 拼凑。损坏/未知版本沿 M3 `issues` 显式呈现，不用 null 假装“尚未解析”。

`RunSummary.configuration` 使用本文件 §2.3 的唯一当前形状；create receipt、history/detail 和 lifecycle 从同一配置事实读取。M1–M9 内不按 Milestone 返回不同 configuration 版本，也不保留阶段 union。

run detail 的 `items` 包含 run-owned `analysis_request v1`、`lifecycle_event v2`、`topology_ready v1`；parser session transcript 包含 input/message/`query_understood`。现有 `referenced_items` 和全文 URL 负责跨归属引用，不复制 context/graph 正文。

## 6. 两层 SSE 扩展

### 6.1 session stream

parser session 使用现有事件：

```text
event: transcript_item      # query_parser_input 或 query_understood
event: message_started      # query_parser_message header
event: message_delta        # 安全文本增量
event: message_finalized    # 同一消息最终表示
event: stream_end
```

不存在 `event: query_understood`；`query_understood` 是 `transcript_item.item.content_type`。session cursor、补发、取消定稿和终止规则完全沿用 M3。

### 6.2 run stream

当前 `run_snapshot v1` 直接增加字段：

```json
{
  "schema_version": 1,
  "run_id": "0d415090-860a-4e1c-b843-191968dbcc6e",
  "status": "running",
  "analysis_phase": "topology_ready",
  "created_at": "2026-09-25T08:00:00Z",
  "updated_at": "2026-09-25T08:00:05Z",
  "context_ref": {
    "run_id": "0d415090-860a-4e1c-b843-191968dbcc6e",
    "item_id": "405d943e-c50c-48d7-913f-888ad32cb835"
  },
  "topology_ref": {
    "run_id": "0d415090-860a-4e1c-b843-191968dbcc6e",
    "item_id": "f071e82f-b341-47ed-8b55-6ac4da37b957"
  },
  "sessions": [
    {
      "session_id": "5885bd7f-a1d5-4c51-920d-528cd33f00e9",
      "agent_role": "query_parser",
      "target_comid": null,
      "status": "completed",
      "created_at": "2026-09-25T08:00:01Z",
      "updated_at": "2026-09-25T08:00:03Z"
    }
  ],
  "waterbodies": [
    {
      "target_comid": "12345678",
      "participating": true,
      "session_id": null,
      "session_status": null
    }
  ],
  "persistence": {
    "state": "up_to_date",
    "warning": null
  }
}
```

`analysis_phase` 是进度 code，不是 lifecycle status。M4 已知 `queued / query_understanding / topology_building / topology_ready / terminal`；后续阶段可增加 code。`context_ref`、`topology_ref` 在原 item 尚不存在时为 null，存在后不回退。

不存在独立 `event: topology_ready`。topology 完成通过下一份完整轻量 `run_snapshot` 和 `topology_ref` 通知；完整 graph 从原 item/全文接口读取。run stream 仍无 SSE id，重连首发最新快照。

## 7. 运行中业务错误

下列错误只能在 run 创建后发现，通过 parser/session/run lifecycle 的 `PublicError` 与最新 snapshot/history 读取，不把已经返回 `201` 的异步执行改成另一次 HTTP create 响应：

| code | failed_stage | retryable | 用户动作 |
|---|---|---:|---|
| `query_parse_failed` | `query_parsing` | false | 改写问题或选择另一解析模型后新建 run。 |
| `query_clarification_required` | `query_resolution` | false | 按提示补充关键信息后新建 run。 |
| `location_required` | `location_resolution` | false | 地图选点或在 query 中说明地点。 |
| `location_ambiguous` | `location_resolution` | false | 使用更具体地点或地图选点。 |
| `location_conflict` | `location_resolution` | false | 统一地图选择与 query 地点。 |
| `location_outside_supported_region` | `location_resolution` | false | 选择 CONUS 范围。 |
| `location_not_supported` | `location_resolution` | false | 选择能够解析到 NHDPlusV2 COMID 的河段。 |
| `target_mismatch` | `location_resolution` | false | 重新地图选点，确保 COMID/坐标一致。 |
| `date_range_required` | `date_resolution` | false | 选择完整日期或在 query 中明确范围。 |
| `date_range_conflict` | `date_resolution` | false | 统一表单和 query 日期。 |
| `date_range_out_of_bounds` | `date_resolution` | false | 选择当前日期前后各 7 天内范围。 |
| `resolution_conflict` | `date_resolution` | false | 统一表单粒度与 query 明确要求。 |
| `parameter_required` | `parameter_resolution` | false | 明确想判断的水质问题。 |
| `parameter_ambiguous` | `parameter_resolution` | false | 使用更具体参数/目标描述。 |
| `parameter_unsupported` | `parameter_resolution` | false | 改为能力表支持的目标，或等待后续扩展。 |
| `too_many_parameters` | `parameter_resolution` | false | 缩小问题至最多 6 个用户目标参数；系统支持参数不占上限。 |
| `parser_model_unavailable` | `query_parsing` | true | 稍后重试；不会自动切模型。 |
| `location_resolution_unavailable` | `location_resolution` | true | 稍后重试。 |
| `parameter_catalog_unavailable` | `parameter_resolution` | true | 稍后重试。 |
| `query_resolution_unavailable` | `query_resolution` | true | 稍后重试并联系维护者确认未分类依赖。 |
| `topology_lookup_failed` | `topology_building` | true | 稍后重试。 |
| `station_availability_failed` | `topology_building` | true | 稍后重试。 |
| `topology_invalid` | `topology_building` | false | 联系维护者并提供 `error_id`。 |

message 必须安全、可操作，不包含 provider 原响应、key、Authorization、内部路径或堆栈。`retryable=true` 只表示新建 run 可能成功；不会自动恢复已 failed 的 run，也不复用原创建 key/body 发起新的业务尝试。

## 8. 前端接入顺序

1. 从最终设置/模型目录读取 parser 与 waterbody 可选项；前端只保存 provider/model identity。
2. 地图选择保存 COMID + coordinates；表单选择 date range、resolution、memory mode；参数不提供表单。
3. 用户主动提交时生成 UUID v4 key；响应丢失才原 bytes 重试。
4. `201` 后订阅 run stream；出现 parser session 后按需订阅该 session stream。
5. 收到 `query_understood` item 后分别展示最终用户目标参数、系统自动加入的支持参数和 rationale；discharge 是必需支持事实，不显示成用户选择；不暂停等待确认。
6. 收到 run snapshot 的 `topology_ref` 后读取 run detail/全文并绘图；excluded reason 来自 graph，不从颜色或 session 缺失猜测。
7. 用户认为理解错误时调用现有 cancel；修改输入后生成新 key 创建新 run。
8. 运行失败显示安全 message 与 `error_id`；用户修正业务错误或对 retryable 技术错误稍后新建 run。

前端不得：发送参数代码作为用户选择、发送 API key、在浏览器自行转 UTC 边界、静默截日期、把 unavailable 当 empty、从参数 rationale 伪造模型置信度，或在 topology 阶段为每个节点预建 UI session。

## 9. OpenAPI 与 fixture 要求

后续实现必须让实际 OpenAPI 覆盖：

- 最终 create request 的 required/nullable/enum/strict-extra 规则；
- 唯一 run configuration 的 resolution preference、两个 model selection 与 memory mode，且没有 milestone schema version/union；
- `context_summary`、扩展后的 run snapshot v1、[154 项 capability 基线](m4_parameter_capability_catalog.md)/必需 discharge 约束和 M4 public errors；
- M3 所有既有 route、view、分页、取消、补写和 SSE error 不回退。

可执行文档 fixture 至少覆盖：完整 create、target/date fallback、`query_understood` transcript item、parser message 三事件、run snapshot topology reference、完整小 graph、422 validation 和异步 lifecycle failure。fixture 通过 production registry 与实际 SSE encoder 生成，禁止手写一个与运行时 schema 漂移的第二份对象。
