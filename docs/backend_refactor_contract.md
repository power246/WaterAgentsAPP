# WaterAgents 旧后端历史归档

生成日期：2026-07-09

本文档记录旧后端和旧前端之间的历史行为，仅在用户明确要求维护或审计旧系统时使用。

> [!WARNING]
> 本文档、`backend_bk/` 和 `frontend_bk/` 不属于新后端或未来新前端的上下文。不得用它们推导、限制、兼容、排除或测试新系统的任何功能、接口、DTO、事件、错误、组件、字段或内部实现。新系统完成后，本文档应与旧前后端一起丢弃。

## 1. 当前后端概览

技术栈：

- Web 框架：FastAPI，入口为 `backend/main.py`
- 实时推理：WebSocket + LangGraph
- LLM 封装：LangChain provider，包括 Ollama、OpenAI、Google Gemini、Grok/xAI
- 外部数据：USGS NLDI、USGS NWIS、EPA FRS、NOAA weather.gov、Google Maps Geocoding
- 本地持久化：`data/user_keys.json`、`data/memory/<comid>.json`
- 本地缓存：`.cache/nldi_data`、`.cache/nwis_data`、`.cache/weather_data`

启动与生命周期：

- FastAPI 应用标题：`Water-Agent API`
- CORS：允许所有 origin、method、header
- `lifespan` 启动时：
  - 清空 `./data/memory`
  - 清空三类 DiskCache 内容，但保留 cache 目录
- `lifespan` 关闭时：
  - 关闭共享 `httpx.AsyncClient`
  - 关闭 NLDI/NWIS/Weather DiskCache

前端开发代理：

- Vite 开发服务器端口：`3000`
- `/api` 代理到 `http://localhost:8000`
- `/ws` 代理到 `ws://localhost:8000`

## 2. 前端实际依赖的接口

当前前端主要依赖：

- `GET /api/keys/map-key`：地图初始化时获取 Google Maps key，缺失则使用 OpenStreetMap
- `GET /api/settings/defaults`：打开 Settings 面板时加载 provider、模型、设施类型和参数限制
- `GET /api/keys/ollama-models`：打开 Settings 面板时获取本机 Ollama 模型列表
- `GET /api/keys`：打开 Settings 面板时获取已保存 key 的 masked 状态
- `POST /api/keys/{name}`：保存 API key
- `DELETE /api/keys/{name}`：清除 API key
- `WS /ws/hydro-analysis`：运行实时水文分析，是当前主分析入口

当前前端没有调用：

- `POST /analyze`：保留的 REST 分析入口
- `GET /health`：健康检查

未注册接口：

- `backend/app/api/endpoints/analysis.py` 中的 `/run-analysis` 被整体注释，且没有被 `main.py` include，不属于当前外部契约。

## 3. HTTP 接口契约

### 3.1 `GET /health`

用途：健康检查。

响应：

```json
{
  "status": "healthy",
  "engine": "LangGraph"
}
```

### 3.2 `POST /analyze`

用途：REST 方式触发完整分析。当前前端不使用，但后端仍暴露。

请求体：

```json
{
  "query": "Compare the pH at coordinates: -92.4141, 46.6823",
  "model_name": "llama3:8b",
  "start_date": "2026-01-01",
  "end_date": "2026-01-07"
}
```

字段：

- `query`：必填，自然语言查询。代码假设日期来自独立字段，而不是 query 文本
- `model_name`：可选，默认 `llama3:8b`
- `start_date`：必填，格式 `YYYY-MM-DD`
- `end_date`：必填，格式 `YYYY-MM-DD`

成功响应：

```json
{
  "target_id": "string",
  "summary": {},
  "all_agents_data": {
    "COMID": {}
  },
  "params_used": [["pH", "00400"]],
  "time_range": {
    "start": "2026-01-01",
    "end": "2026-01-07"
  },
  "mode": "gap_filling",
  "time_mode": "date_range",
  "segments": [
    {"start": "2026-01-01", "end": "2026-01-07", "mode": "gap_filling"}
  ]
}
```

错误：

- `400`：无法解析查询意图或坐标无效
- `500`：计算图构建失败或推理引擎运行异常

当前实现细节：

- 自动 `resample_auto=true`，`resample_freq_h=6`
- `facility_types` 固定为 `["ICIS", "SEMS", "TRIS", "NCDB", "CWNS", "FFEP"]`
- 不支持前端 Settings 里的 provider、api key、facility selection、weather interval

### 3.3 `GET /api/settings/defaults`

用途：返回 Settings 面板需要的 provider、模型、设施类型、参数限制和后端默认值。

响应顶层字段：

```json
{
  "providers": [],
  "facility_types": [],
  "limits": {},
  "auto_freq_rules": [],
  "defaults": {}
}
```

`providers` 当前包含：

- `ollama`
  - `needs_api_key=false`
  - 默认模型列表：`llama3:8b`、`llama3:70b`、`mistral:7b`、`qwen2:7b`
- `openai`
  - `needs_api_key=true`
  - `store_key=OPENAI_API_KEY`
  - 模型：`gpt-4o`、`gpt-4o-mini`、`gpt-4-turbo`、`gpt-3.5-turbo`
- `gemini`
  - `needs_api_key=true`
  - `store_key=GOOGLE_API_KEY`
  - 模型：`gemini-2.0-flash`、`gemini-1.5-pro`、`gemini-1.5-flash`
- `grok`
  - `needs_api_key=true`
  - `store_key=GROK_API_KEY`
  - 模型：`grok-2`、`grok-beta`

`facility_types` 当前包含：

- `ICIS`
- `SEMS`
- `TRIS`
- `NCDB`
- `CWNS`
- `FFEP`

`limits`：

```json
{
  "max_topology_depth": {"min": 1, "max": 20, "unit": "hops"},
  "resample_freq_h": {"min": 1, "max": 168, "unit": "hours"},
  "weather_freq_h": {"min": 1, "max": 168, "unit": "hours"}
}
```

`auto_freq_rules`：

```json
[
  {"max_days": 7, "resample_h": 6, "weather_h": 12, "label": "<= 7 days"},
  {"max_days": 14, "resample_h": 12, "weather_h": 24, "label": "<= 14 days"},
  {"max_days": 30, "resample_h": 24, "weather_h": 48, "label": "<= 30 days"},
  {"max_days": 90, "resample_h": 48, "weather_h": 72, "label": "<= 90 days"},
  {"max_days": 9999, "resample_h": 72, "weather_h": 168, "label": "> 90 days"}
]
```

`defaults` 来自 `app.core.config.settings`：

```json
{
  "provider": "ollama",
  "model_name": "llama3:8b",
  "facility_types": ["ICIS", "SEMS", "TRIS", "NCDB", "CWNS", "FFEP"],
  "max_topology_depth": 10,
  "resample_freq_h": 6,
  "weather_freq_h": 12,
  "resample_auto": true
}
```

### 3.4 `GET /api/keys`

用途：返回所有已知 key 的保存状态和 masked 值，不返回原始 key。

已知 key：

- `GOOGLE_MAPS_API_KEY`
- `OPENAI_API_KEY`
- `GOOGLE_API_KEY`
- `GROK_API_KEY`

响应：

```json
{
  "GOOGLE_MAPS_API_KEY": {"set": true, "masked": "••••abcd"},
  "OPENAI_API_KEY": {"set": false, "masked": null},
  "GOOGLE_API_KEY": {"set": false, "masked": null},
  "GROK_API_KEY": {"set": false, "masked": null}
}
```

持久化位置：`./data/user_keys.json`

### 3.5 `POST /api/keys/{name}`

用途：保存或覆盖一个用户输入的 API key。

路径参数：

- `name` 必须属于已知 key 列表

请求体：

```json
{
  "value": "actual-api-key"
}
```

成功响应：

```json
{
  "ok": true,
  "name": "OPENAI_API_KEY",
  "message": "Key saved successfully."
}
```

错误：

- `400`：未知 key 名称
- `400`：`value` 为空字符串

### 3.6 `DELETE /api/keys/{name}`

用途：删除已保存 key，使系统回退到 `.env`。

成功响应：

```json
{
  "ok": true,
  "name": "OPENAI_API_KEY",
  "message": "Key cleared."
}
```

错误：

- `400`：未知 key 名称

### 3.7 `GET /api/keys/map-key`

用途：返回 Google Maps JS tile 初始化需要的原始 key。

key 优先级：

1. `data/user_keys.json` 中的 `GOOGLE_MAPS_API_KEY`
2. `.env` 或环境变量中的 `GOOGLE_MAPS_API_KEY`

响应：

```json
{
  "key": "actual-google-maps-key-or-null"
}
```

说明：

- 这是当前唯一会把原始 key 返回给前端的 HTTP 接口
- 如果 `key=null`，前端地图使用 OpenStreetMap

### 3.8 `GET /api/keys/ollama-models`

用途：代理本机 Ollama 的 `/api/tags`，返回 Settings 面板可展示的模型列表。

请求目标：`settings.OLLAMA_BASE_URL.rstrip("/") + "/api/tags"`，默认 `http://localhost:11434/api/tags`

成功响应：

```json
{
  "models": [
    {"id": "llama3:8b", "label": "Llama3 8B"}
  ],
  "error": null
}
```

Ollama 不可达时响应仍为 `200`：

```json
{
  "models": [],
  "error": "Ollama not reachable at http://localhost:11434. Is it running?  Falling back to default model list."
}
```

## 4. WebSocket 契约：`/ws/hydro-analysis`

用途：当前主分析入口。前端点击 Start Analysis 后建立 WebSocket，然后立即发送一次参数 JSON。后端流式返回拓扑、agent 运行状态和推理结果。

连接地址：

- 本地开发：`ws://localhost:3000/ws/hydro-analysis`，经 Vite 代理到后端
- 后端真实地址：`ws://localhost:8000/ws/hydro-analysis`

### 4.1 客户端首条消息

前端建立连接后发送：

```json
{
  "query": "Compute the pH at coordinates: -92.410000, 46.680000",
  "model_name": "llama3:8b",
  "start_date": "2026-01-01",
  "end_date": "2026-01-07",
  "provider": "ollama",
  "api_key": null,
  "facility_types": ["ICIS", "SEMS", "TRIS", "NCDB", "CWNS", "FFEP"],
  "max_topology_depth": 10,
  "resample_auto": true,
  "resample_freq_h": 6,
  "weather_freq_h": 12
}
```

当前后端实际使用：

- `query`
- `model_name`
- `start_date`
- `end_date`
- `max_topology_depth`
- `resample_auto`
- `resample_freq_h`

当前后端收到但未真正使用：

- `provider`：LLM provider 由 `model_name` 字符串自动判断
- `api_key`：没有从 WebSocket 参数传入 `LLMService.call_llm`
- `facility_types`：后端在 LangGraph 初始状态中硬编码为全量设施类型
- `weather_freq_h`：WeatherService 使用 `settings.WEATHER_FREQ`

重构决策点：新后端如果要保持“当前实际行为”，这些字段可以继续忽略；如果要符合 Settings 面板语义，应让这些字段生效，但这属于外部行为修正。

### 4.2 客户端停止消息

用户点击 Stop 时，前端发送：

```json
{
  "type": "stop"
}
```

当前后端行为：

- WebSocket 后台 listener 收到后设置 `stop_event`
- 正在运行的 inference task 会被取消
- 后端最终尝试发送 `{"type": "backend_idle"}`
- 前端收到 `backend_idle` 后解锁 Start 按钮，并把状态设置为 `interrupted`

### 4.3 服务端事件：`ping`

用途：20 秒心跳，避免浏览器或代理空闲断连。

```json
{
  "type": "ping"
}
```

前端行为：静默忽略，不写入日志。

### 4.4 服务端事件：`topology`

用途：在任何 agent 推理前，先把完整拓扑节点和边发给前端地图。

```json
{
  "type": "topology",
  "nodes": [
    {
      "comid": "123456",
      "lat": 46.68,
      "lon": -92.41,
      "has_usgs_site": true,
      "site_id": "04024000",
      "is_target": true,
      "is_inlet": false,
      "is_excluded": false
    }
  ],
  "edges": [
    {
      "from": "111111",
      "to": "123456",
      "is_excluded": false
    }
  ],
  "target_id": "123456",
  "inlet_comids": ["111111"],
  "params_used": [["pH", "00400"]],
  "time_range": {
    "start": "2026-01-01",
    "end": "2026-01-07"
  },
  "segments": [
    {"start": "2026-01-01", "end": "2026-01-07", "mode": "gap_filling"}
  ]
}
```

前端依赖：

- `nodes`、`edges` 用于地图展示
- `is_excluded` 用于灰化不参与推理链路的节点/边
- `is_target`、`is_inlet`、`has_usgs_site` 用于节点颜色和图例
- `segments` 传给 TimeSeriesChart，用于区分 historical/prediction 日期

### 4.5 服务端事件：`agent_start`

用途：某个 active COMID 开始推理。

```json
{
  "type": "agent_start",
  "comid": "123456"
}
```

前端行为：

- 设置 `activeComid`
- 地图上对应节点出现 running 状态
- 右侧日志显示开始推理

### 4.6 服务端事件：`agent_done`

用途：某个 active COMID 推理完成，携带该节点推理结果、坐标、LLM 输入输出和异常窗口。

```json
{
  "type": "agent_done",
  "comid": "123456",
  "result": {
    "parameters": [
      {
        "name": "pH",
        "code": "00400",
        "estimate_value": 7.2,
        "unit": "std units"
      }
    ],
    "estimated_flow": 120.5,
    "confidence": 0.82,
    "analysis": "text",
    "detailed_results": [
      {
        "name": "pH",
        "code": "00400",
        "unit": "std units",
        "daily_results": [
          {
            "date": "2026-01-01",
            "representative_value": 7.2,
            "unit": "std units",
            "confidence": 0.75,
            "anomalies": []
          }
        ]
      }
    ],
    "explanation": {
      "primary_drivers": [
        {
          "factor": "Upstream COMID 111111",
          "direction": "increasing",
          "magnitude": "medium",
          "evidence": "text"
        }
      ],
      "reasoning_chain": "text",
      "uncertainty_sources": "text"
    },
    "flow_results": {
      "name": "Discharge",
      "code": "00060",
      "unit": "ft3/s",
      "daily_results": []
    }
  },
  "lat": 46.68,
  "lon": -92.41,
  "has_usgs_site": true,
  "site_id": "04024000",
  "prompt_used": "full LLM prompt",
  "llm_raw_output": "raw LLM response",
  "explanation": {},
  "detailed_results": [],
  "flow_results": {},
  "anomalies": [
    {
      "comid": "123456",
      "time_window": "2026-01-01 06:00",
      "parameter": "pH",
      "observed_value": 8.4,
      "daily_mean": 7.2,
      "deviation_pct": 16.67,
      "possible_cause": "text"
    }
  ]
}
```

前端依赖：

- `result.parameters` 用于日志、Popup、Inspector 参数表
- `result.estimated_flow` 用于 Popup 和 Inspector
- `result.confidence` 用于日志和 Inspector 置信度条
- `result.analysis` 用于日志 details 和 Inspector
- `result.detailed_results` 用于 TimeSeriesChart
- `result.flow_results` 用于 TimeSeriesChart 中的 Discharge 曲线
- `prompt_used` 和 `llm_raw_output` 用于 Inspector 展示完整 LLM 输入输出
- `anomalies` 用于右侧 Anomalies tab
- `lat`、`lon` 用于更新拓扑节点坐标

注意：`agent_done` 顶层同时传了 `explanation`、`detailed_results`、`flow_results`，它们与 `result` 内部字段重复。当前前端主要从 `result` 取图表数据，从顶层取 anomalies。

### 4.7 服务端事件：`done`

用途：所有 active agent 推理完成。

```json
{
  "type": "done",
  "target_id": "123456",
  "message": "All agent inference complete."
}
```

前端行为：

- 状态设为 `done`
- 清空 `activeComid`
- 日志显示目标节点

### 4.8 服务端事件：`interrupted`

用途：用户停止后，后端在 agent 边界或取消路径上通知前端推理中断。

```json
{
  "type": "interrupted",
  "message": "Inference stopped by user. Completed agents are shown on the map."
}
```

前端行为：

- 状态设为 `interrupted`
- 清空 `activeComid`
- 已完成节点继续显示

### 4.9 服务端事件：`error`

用途：意图解析、拓扑构建、推理图运行或 WebSocket 外层处理失败。

```json
{
  "type": "error",
  "message": "Inference engine error: ..."
}
```

前端行为：

- 状态设为 `error`
- 清空 `activeComid`
- 日志显示错误

### 4.10 服务端事件：`backend_idle`

用途：后端告知前端所有后端任务已经停止，可解锁 Start 按钮。

```json
{
  "type": "backend_idle"
}
```

当前实现注意点：

- WebSocket handler 的 `finally` 会在 inference task 结束后尝试发送 `backend_idle`
- 这不只发生在 Stop 场景，正常 `done` 或 `error` 后也可能发送
- 当前前端收到 `backend_idle` 会调用 stop finalizer，并把状态设为 `interrupted`
- 重构时需要明确：`backend_idle` 是只用于 stop，还是任何运行结束都发送。如果保留当前前端逻辑，建议只在 Stop/Cancel 路径发送

### 4.11 前端识别但当前后端未发送的事件

`preparing`：

```json
{
  "type": "preparing",
  "step": "string",
  "message": "string"
}
```

当前 `RightPanel` 和 `App` 能处理该事件，但后端 `run_hydro_graph` 没有实际 emit。

## 5. 核心数据模型

### 5.1 `RiverAgentData`

用于表示每个 COMID agent 的静态信息、拓扑关系、记忆和结果。

```json
{
  "comid": "string",
  "upstream_ids": ["string"],
  "downstream_ids": ["string"],
  "has_usgs_site": false,
  "site_id": null,
  "lat": 0.0,
  "lon": 0.0,
  "memory": {},
  "output": null,
  "prompt_used": null,
  "llm_raw_output": null
}
```

### 5.2 `HydrologyInference`

LLM 最终输出的规范结构，也是 `agent_done.result` 的核心结构。

字段：

- `parameters: ParameterInference[]`
- `estimated_flow: float | null`
- `confidence: float`
- `analysis: string`
- `detailed_results: ParameterResult[]`
- `explanation: HydrologyExplanation | null`
- `flow_results: ParameterResult | null`

### 5.3 `ParameterInference`

```json
{
  "name": "pH",
  "code": "00400",
  "estimate_value": 7.2,
  "unit": "std units"
}
```

### 5.4 `ParameterResult`

用于每日时间序列。

```json
{
  "name": "pH",
  "code": "00400",
  "unit": "std units",
  "daily_results": [
    {
      "date": "2026-01-01",
      "representative_value": 7.2,
      "unit": "std units",
      "confidence": 0.75,
      "anomalies": []
    }
  ]
}
```

### 5.5 `AnomalyWindow`

```json
{
  "time_window": "2026-01-01 06:00",
  "parameter": "pH",
  "observed_value": 8.4,
  "daily_mean": 7.2,
  "deviation_pct": 16.67,
  "possible_cause": "text"
}
```

`agent_done.anomalies` 会在此基础上额外加 `comid` 字段。

### 5.6 `HydrologyExplanation`

```json
{
  "primary_drivers": [
    {
      "factor": "Upstream COMID 111111",
      "direction": "increasing",
      "magnitude": "medium",
      "evidence": "text"
    }
  ],
  "reasoning_chain": "text",
  "uncertainty_sources": "text"
}
```

## 6. 当前内部功能链路

### 6.1 查询编排：`AskerService.prepare_inference_context`

输入：

- `user_query`
- `selected_model`
- `max_topology_depth`
- `start_date`
- `end_date`

步骤：

1. 调用 LLM 解析查询意图，目标结构包含：
   - `location`
   - `characteristic_names`
   - `time_range`
2. 解析 location：
   - 如果是长度为 2 的 list，转成 `(lat, lon)`
   - 如果是字符串，调用 Google Maps Geocoding
3. 调用 NLDI 根据坐标查 `start_comid`
4. 使用 SentenceTransformer `all-MiniLM-L6-v2` 对用户参数名做语义匹配，映射到 USGS parameter code
5. 日期优先使用前端传入的 `start_date`、`end_date`
6. 根据服务端 `date.today()` 判断：
   - `mode=gap_filling`：结束日期小于等于今天
   - `mode=prediction`：开始日期大于今天
   - `mode=mixed`：横跨今天
7. 判断 `time_mode`：
   - `single_day`
   - `date_range`
8. 生成 `segments`
   - historical segment 使用 `gap_filling`
   - future segment 使用 `prediction`
9. 预测日期上限：
   - `settings.PREDICTION_MAX_DAYS = 7`
   - 纯预测或 mixed 的预测段都会截断到最多 7 天
10. 调用 `TopologyService.build_topology`
11. 找出所有 `has_usgs_site=true` 的 COMID 作为 `inlet_comids`
12. 如果没有 inlet，则把 `start_comid` 当作孤立推理点

返回 context：

```json
{
  "inlet_comids": ["string"],
  "start_comid": "string",
  "start_date": "YYYY-MM-DD",
  "end_date": "YYYY-MM-DD",
  "current_params": [["parameter name", "parameter code"]],
  "global_data": {"COMID": {}},
  "task": "original query",
  "selected_model": "llama3:8b",
  "mode": "gap_filling",
  "time_mode": "date_range",
  "segments": []
}
```

当前实现注意点：

- LLM 解析失败时有默认 time range，但如果 location 缺失，整个 context 返回 `None`
- USGS 参数 CSV 路径写死为相对路径 `../../dataset/public_srsnames_December_2023.csv`，重构时建议改成基于项目根或配置的稳定路径
- 日期分类使用后端本地日期，前端 TimeSeriesChart 则优先使用后端返回的 `segments`

### 6.2 拓扑构建：`TopologyService.build_topology`

输入：

- `start_comid`
- `max_depth`

逻辑：

1. 从目标 COMID 向上游 BFS
2. 每个 COMID 先读取 `data/memory/<comid>.json` 中的 `static_metadata.site_id`
3. 如果无缓存，调用 NLDI 查关联 USGS site
4. 如果当前 COMID 有 USGS site：
   - 标记 `has_usgs_site=true`
   - 停止继续追溯这一支上游
5. 如果没有 USGS site：
   - 调用 NLDI 查 direct upstream flowlines
   - 去掉“祖先节点同时出现在直接上游列表”的重复项
   - 建立 `upstream_ids` 和反向 `downstream_ids`
6. 返回 `Dict[comid, RiverAgentData]`

### 6.3 Active/excluded 节点计算

在 `app.agents.graph._compute_active_nodes` 中：

1. 从 `inlet_comids` 开始
2. 沿 `downstream_ids` BFS 到 target 方向
3. BFS 到的节点是 active
4. 拓扑中存在但不在 active 集合里的节点是 excluded

LangGraph 只为 active 节点创建 node。WebSocket `topology` 事件仍会发送所有节点，并用 `is_excluded` 标记 excluded 节点。

### 6.4 LangGraph 推理图

`create_hydrological_graph`：

- 每个 active COMID 是一个 LangGraph node
- 边方向：upstream COMID 指向 downstream COMID
- entry point：所有 `inlet_comids`
- 每个 node 运行 `inference_node`
- node 输入包含：
  - 当前 task
  - 参数列表
  - 设施类型
  - 日期
  - LLM model
  - upstream_results
  - resample 配置
  - mode/time_mode/segments

### 6.5 单个 COMID 推理：`inference_node`

核心步骤：

1. 初始化 `RiverAgentData`
2. 读取 `data/memory/<comid>.json`
3. 取最近 2 条历史推理作为 LLM 上下文
4. 读取或获取静态 metadata：
   - lat
   - lon
   - site_id
   - has_usgs_site
5. 并发获取外部数据：
   - EPA nearby facilities
   - NOAA historical weather and alerts
   - NWIS parameter observations
   - NWIS flow data
6. prediction/mixed 模式额外获取：
   - NOAA forecast
   - 最近 48 小时 NWIS observations 作为预测 baseline
7. gap_filling/mixed 且有观测数据时：
   - 计算每日 representative value
   - 检测 sub-daily anomaly window
8. 根据 `mode` 和是否有 USGS site 选择 prompt：
   - `HYDRO_USGS_PROMPT`
   - `HYDRO_INFER_PROMPT`
   - `HYDRO_USGS_PREDICTION_PROMPT`
   - `HYDRO_INFER_PREDICTION_PROMPT`
9. mixed 模式执行两次 LLM：
   - historical gap_filling pass
   - future prediction pass
   - 合并 detailed_results、flow_results、explanation、analysis 和 confidence
10. 调用 LLM
11. 正则抽取 JSON，归一化字段，Pydantic 校验为 `HydrologyInference`
12. 写入：
   - `river.output`
   - `river.prompt_used`
   - `river.llm_raw_output`
13. 将本次推理追加到 memory，只保留最近 10 条
14. 返回 `{"global_data": {comid: river.model_dump()}}`

LLM 输出兜底：

- `parameters` 如果是 dict，会转 list
- 缺失的 requested parameter 会注入 `estimate_value=null`
- `confidence` 缺失时：
  - 有 USGS site 默认 `0.75`
  - 无 USGS site 默认 `0.50`
- `analysis` 缺失时填 `"Analysis not provided by model."`
- `explanation` 缺失时插入 Data limitation placeholder
- `flow_results` 缺失只 warning，不阻断
- JSON 解析失败时返回 `{"error": "JSON parsing failed", "analysis": "Raw output: ..."}`

## 7. 外部服务能力

### 7.1 `NLDIService`

缓存目录：`.cache/nldi_data`

能力：

- `get_coords_by_comid(comid)`
  - 调用 `https://api.water.usgs.gov/nldi/linked-data/comid/{comid}`
  - 返回 `{"lat": float, "lon": float}`
- `get_associated_usgs_site(comid, navigation_mode="UT", distance_km=settings.USGS_SEARCH_DISTANCE)`
  - 调用 NLDI navigation 到 `nwissite`
  - 返回清洗后的 USGS station id
- `get_main_comid(lat, lon)`
  - 调用 NLDI position API
  - 返回坐标对应 COMID
- `get_direct_upstreams(comid, distance=5)`
  - 调用 upstream tributary flowlines
  - 返回直接上游 COMID 列表

### 7.2 `NWISService`

缓存目录：`.cache/nwis_data`

能力：

- `get_daily_averages(site_id, start_date, end_date, parm_codes, resample_freq=None)`
  - 调用 `https://waterservices.usgs.gov/nwis/iv/`
  - 拉取 instantaneous values
  - 按 resample frequency 聚合
  - 返回每个参数的 `data_6h` 风格列表，字段为 `date` 和 `avg_value`
- `get_flow_data(site_id, start_date, end_date, resample_freq=None)`
  - 固定参数 `Discharge / 00060`
- `compute_daily_summary(raw_data, anomaly_threshold_pct)`
  - 按自然日聚合
  - 计算 daily representative value
  - 根据 `settings.ANOMALY_THRESHOLD_PCT` 标记 anomaly windows
  - 返回 `ParameterResult[]`

### 7.3 `EPAService`

能力：

- 按 EPA facility type 并发查询附近设施
- 查询接口：`https://ofmpub.epa.gov/frs_public2/frs_rest_services.get_facilities`
- 半径单位从 km 转 mile
- 按 `RegistryId` 去重
- 返回字段：
  - `registryId`
  - `name`
  - `lat`
  - `lon`
  - `address`
  - `program_types`

### 7.4 `WeatherService`

缓存目录：`.cache/weather_data`

历史天气：

- `get_weather_and_alerts(lat, lon, start_date, end_date)`
- 调用 `api.weather.gov/points/{lat},{lon}` 找 observation station
- 获取 observations 和 alerts
- observations 聚合字段：
  - `time`
  - `temp_c`
  - `precip_mm`
- alerts 字段：
  - `window`
  - `active_alerts: [{event, severity}]`

未来预报：

- `get_weather_forecast(lat, lon, start_date, end_date)`
- 使用 NOAA hourly forecast 和 gridpoints QPF
- 输出 observations 字段：
  - `time`
  - `temp_c`
  - `precip_prob_pct`
  - `precip_mm`

当前实现注意点：

- Weather resample 频率使用 `settings.WEATHER_FREQ`
- WebSocket 传入的 `weather_freq_h` 尚未接入

### 7.5 `LLMService` 与 `LLMFactory`

Provider 自动判断逻辑：

- model name 包含 `gpt`：`openai`
- model name 包含 `gemini`：`gemini`
- model name 包含 `grok`：`grok`
- 其他：`ollama`

Key 优先级：

1. `LLMService.call_llm(api_key=...)` 显式传入
2. `data/user_keys.json`
3. `.env` 或环境变量

当前 WebSocket 链路注意点：

- 前端发送的 `api_key` 没有传入 `LLMService.call_llm`
- 因此实际优先级在 WebSocket 分析中通常是 user key store，然后 `.env`

Provider 实例：

- `ollama`：`ChatOllama`，base url 默认 `http://localhost:11434`
- `openai`：`ChatOpenAI`
- `gemini`：`ChatGoogleGenerativeAI`
- `grok`：`ChatOpenAI` + `settings.GROK_BASE_URL`

JSON mode：

- Ollama：`llm.bind(format="json")`
- OpenAI：`llm.bind(response_format={"type": "json_object"})`
- Gemini/Grok：当前未设置 provider-specific JSON mode

## 8. 本地状态与持久化

### 8.1 API key store

文件：`./data/user_keys.json`

特点：

- 异步锁保护 read-modify-write
- `GET /api/keys` 只返回 masked 状态
- `GET /api/keys/map-key` 会返回原始 Google Maps key

### 8.2 Agent memory

文件：`./data/memory/<comid>.json`

内容：

- `static_metadata`
  - lat
  - lon
  - site_id
  - has_usgs_site
- `past_inferences`
  - timestamp
  - start_date
  - end_date
  - task
  - structured_inference
  - llm_result

当前生命周期：

- 服务启动时 `reset_memory_only()` 会清空 `./data/memory`
- 因此 memory 不跨服务重启保留
- 同一次运行内，每个 COMID 最多保留最近 10 条推理

### 8.3 DiskCache

目录：

- `.cache/nldi_data`
- `.cache/nwis_data`
- `.cache/weather_data`

当前生命周期：

- 服务启动时清空 cache entries
- 服务关闭时 close cache handle

## 9. 配置项

`app.core.config.Settings` 主要字段：

- `PROJECT_NAME = "Water Agent System"`
- `API_V1_STR = "/api/v1"`，当前未用于路由
- `OLLAMA_BASE_URL = "http://localhost:11434"`
- `OPENAI_API_KEY`
- `GOOGLE_API_KEY`
- `GROK_API_KEY`
- `GROK_BASE_URL = "https://api.x.ai/v1"`
- `GOOGLE_MAPS_API_KEY`
- `DEFAULT_LLM_MODEL = "llama3:8b"`
- `FACILITY_TYPES = ["ICIS", "SEMS", "TRIS", "NCDB", "CWNS", "FFEP"]`
- `USGS_SEARCH_DISTANCE = 0.1`
- `MAX_TOPOLOGY_DEPTH = 10`
- `ANOMALY_THRESHOLD_PCT = 25.0`
- `PREDICTION_MAX_DAYS = 7`
- `WATER_DATA_RESAMPLE_FREQ = "6h"`
- `WEATHER_FREQ = "12h"`
- `HTTP_HEADERS = {"User-Agent": "WaterAgentSystem/1.0"}`
- `REQUEST_TIMEOUT = 60.0`

配置来源：

- `.env`
- 环境变量

## 10. 前端字段依赖矩阵

### 10.1 `App.jsx`

依赖：

- `/api/keys/map-key`
- `WS /ws/hydro-analysis`

WebSocket 首包发送字段：

- `query`
- `model_name`
- `start_date`
- `end_date`
- `provider`
- `api_key`
- `facility_types`
- `max_topology_depth`
- `resample_auto`
- `resample_freq_h`
- `weather_freq_h`

消费事件：

- `ping`
- `backend_idle`
- `preparing`
- `topology`
- `agent_start`
- `agent_done`
- `done`
- `interrupted`
- `error`

### 10.2 `SettingsPanel.jsx`

依赖：

- `GET /api/settings/defaults`
- `GET /api/keys/ollama-models`
- `GET /api/keys`
- `POST /api/keys/{name}`
- `DELETE /api/keys/{name}`

关键字段：

- `providers[].id`
- `providers[].label`
- `providers[].needs_api_key`
- `providers[].api_key_hint`
- `providers[].store_key`
- `providers[].models[].id`
- `providers[].models[].label`
- `facility_types[].id`
- `facility_types[].label`
- `facility_types[].description`
- `limits`
- `auto_freq_rules`
- `defaults`

### 10.3 `MapView.jsx`

依赖 `topology.nodes[]`：

- `comid`
- `lat`
- `lon`
- `has_usgs_site`
- `site_id`
- `is_target`
- `is_inlet`
- `is_excluded`
- `done`
- `result`
- `prompt_used`
- `llm_raw_output`

依赖 `topology.edges[]`：

- `from`
- `to`
- `is_excluded`

### 10.4 `RightPanel.jsx`

日志和 Inspector 依赖：

- `agent_done.result.parameters`
- `agent_done.result.estimated_flow`
- `agent_done.result.confidence`
- `agent_done.result.analysis`
- `agent_done.prompt_used`
- `agent_done.llm_raw_output`
- `agent_done.anomalies`

### 10.5 `TimeSeriesChart.jsx`

依赖：

- `result.detailed_results[].daily_results[]`
- `result.flow_results.daily_results[]`
- `topology.segments[]` 中第一个 `mode=prediction` 的 `start`

每日数据字段：

- `date`
- `representative_value`
- `confidence`
- `unit`
- `anomalies`

## 11. 当前实现中的兼容性风险与重构决策点

这些点不一定要在重构中保持原样，但需要显式决定。

1. WebSocket Settings 参数未完全生效
   - `provider`、`api_key`、`facility_types`、`weather_freq_h` 当前被忽略或未完整接入
   - 用户界面暗示这些设置会影响分析

2. `backend_idle` 语义不清
   - 当前可能在正常 done/error 后也发送
   - 前端把它当 Stop completion，会把状态设为 `interrupted`

3. `/analyze` 与 WebSocket 能力不一致
   - REST 入口不支持 Settings 面板所有参数
   - WebSocket 是当前主路径

4. Memory 启动即清空
   - 代码设计了历史推理上下文
   - 但服务重启会清空 `data/memory`

5. Google Maps raw key 暴露给前端
   - 当前地图加载需要
   - 重构时需要确认安全边界是否接受

6. 参数 CSV 路径相对当前工作目录
   - 当前路径写法容易受启动目录影响
   - 重构应改成稳定 resource path

7. 天气自动频率只展示在前端
   - Settings 返回 `auto_freq_rules`
   - 后端对 USGS resample 有 auto 逻辑
   - WeatherService 当前仍使用全局 `settings.WEATHER_FREQ`

8. LLM JSON 解析容错依赖正则抽取
   - 当前契约接受容错后的 `HydrologyInference`
   - 重构可换实现，但需要保持 `agent_done.result` 结构稳定

## 12. 旧实现迁移参考清单（非兼容要求）

以下内容用于迁移前端或核对旧数据链路，不是新后端的验收要求：

- HTTP endpoints 路径、method、状态码和响应字段兼容
- `GET /api/settings/defaults` 能支撑 SettingsPanel 初始化
- API key 的保存、删除、masked 展示和 map-key 获取语义兼容
- WebSocket 首包字段可接收，不因多余字段失败
- WebSocket 至少发送 `topology -> agent_start/agent_done... -> done/error/interrupted`
- `topology.nodes` 和 `topology.edges` 字段满足 MapView
- `agent_done.result` 满足 Inspector、Popup、Log、TimeSeriesChart
- `agent_done.anomalies` 满足 Anomalies tab
- Stop 消息能让后端停止工作并让前端解锁
- `segments` 能准确表达 historical/prediction/mixed 日期段
- Prediction 范围最多 7 天，或显式修改前后端一致规则
- `params_used` 保持 `[[name, code], ...]` 结构
- COMID、USGS site、lat/lon 字段保持字符串/数字语义稳定
