# M9 Frontend Experience 设计

本文冻结 M9 `frontend/` 的页面、交互、状态所有权和前后端消费规则。实现和验收状态以 [M9 Implementation TODO](m9_implementation_todo.md) 的实际证据为准。可序列化字段与 HTTP 语义以 [M9 Settings/Admin API](m9_settings_admin_api.md) 为准；跨域产品边界见 [M9 总设计](m9_settings_admin_demo_design.md)。

---

## 1. 产品边界

M9 交付独立的 React + TypeScript + Vite 本地 Web 应用。生产源码只位于 `frontend/`；`frontend_bk/` 不参与 import、build、runtime、test 或部署，只能作为人工查看的视觉参考。

第一版：

- 默认只连接同 origin backend；
- 面向一个本地用户、一个 backend 进程、一个 worker；
- 不提供用户登录、远程访问、多租户、Agent 追加消息或单 Agent 取消；
- 不展示 hidden reasoning、raw prompt、raw provider response、secret 或前端推断的科学结论；
- 不建设 Electron/Tauri、安装器、自动更新或系统托盘。

## 2. 信息架构与 URL

路由只表达可恢复的用户位置，不复制 backend resource hierarchy：

| URL | 页面状态 | 说明 |
|---|---|---|
| `/` | New Analysis | 三栏工作台，新建分析表单 |
| `/history` | History | 左栏显示分页 run 历史，地图与右栏显示当前选择 |
| `/runs/{run_id}` | 当前/历史 run | 直接恢复一个 run；live 存在时接流，否则读取 history |
| `/runs/{run_id}/waterbodies/{session_id}` | 选中水体 | 使用真实 `run_id + session_id`，不以 COMID 代替 session identity |
| `/admin` | Advanced/Admin | 独立管理视图，不与日常 Settings 混排 |

Settings 从左栏 header 的齿轮打开 drawer，不要求独立 URL。drawer 关闭后保留当前 run/地图/右栏位置；未保存输入关闭时需确认丢弃。Admin 从 drawer 中的稳定入口进入 `/admin`。

未知前端 route 显示本地 not-found 页面；未知 `/api/*` 必须保留 backend 404，不能被 SPA fallback 改写为 HTML 200。

## 3. 桌面布局与局部改良

桌面主视图保持三栏空间关系：

```text
┌───────────────┬───────────────────────────┬──────────────────┐
│ Controls      │ Map                       │ Detail           │
│ New/History   │ topology + node status    │ Overview         │
│ query/target  │ selected time series      │ Agent            │
│ models/dates  │                           │ Evidence         │
│ Start/Stop    │                           │                  │
└───────────────┴───────────────────────────┴──────────────────┘
```

- 左栏保持约 320 px 的操作密度，中间地图弹性伸缩，右栏保持约 360 px 的阅读宽度；实现使用 CSS grid 的可收缩范围，不把三个值写成不可响应的固定总宽度。
- logo/header、settings 齿轮、Start/Stop、地图 legend、右侧 tab 和 time-series 底部区域保留熟悉的位置线索。
- 允许改善分组、留白、错误靠近输入、键盘焦点和窄屏布局；不得仅为重构同时移动多个高频入口。
- 窄屏将三栏降级为 Controls / Map / Detail tabs 或 drawers；桌面仍是第一版 demo 的主要验收面。
- M9 建立自己的 screenshot baseline；不以和旧系统逐像素一致作为门禁。

## 4. 前端状态所有权

前端不得维护一个混合的 `analysisStatus`。至少分离：

| 状态 | 权威来源 | 客户端职责 |
|---|---|---|
| run lifecycle/phase | run GET + run SSE，SSE 优先 | 选择最新已收到的完整 snapshot，不反推 Agent 终态 |
| session lifecycle | run snapshot/session GET | 按真实 `session_id` 映射节点与详情 |
| transcript message generation | session SSE / strict history GET | 同一 `item_id` 原位 started → delta → finalized |
| persistence | run snapshot/GET 的 `persistence` | 独立显示 pending/retry_exhausted/up_to_date |
| selected UI identity | URL + local view state | 保存选中 run/session/item/tab，不创造业务 identity |
| form draft | local component state | 提交前校验；提交后以 backend snapshot 为准 |
| settings/key/admin | 对应 M9 REST API | 不写入 run reducer，不进入 transcript |

同一 `run_id` 的 run SSE 状态一旦到达，随后返回的旧 GET 不得让页面倒退。首次尚未收到 SSE 时可使用 GET；断线核对和 history 读取仍保留。不得为此前端规则发明 state revision 或比较客户端时间戳。

session 消息位置由首次 `message_started` 的 sequence/occurred_at 决定。`message_delta` 只追加同一 `item_id`；`message_finalized` 原位结束 generating。完成、补写或历史读取不能把消息移动到列表末尾。

## 5. 统一 HTTP 与 SSE client

### 5.1 HTTP

所有请求经一个 typed client：

- 使用相对 `/api/...` URL；
- 读取 HTTP status、`Content-Type`、`X-Request-ID` 和结构化 error envelope；
- mutation 不自动重试；GET 只可对明确 retryable 的网络失败做有界重试；
- create run 与单位策略/Admin 确认的同一次提交重试复用相同 UUID v4 `Idempotency-Key` 和完全相同 body bytes；确认响应不确定时保留原请求供手动重试，明确拒绝后重新预览；
- 不把 secret 放入 URL、日志、analytics、localStorage 或错误 toast；
- route/DTO 类型由 backend OpenAPI 生成或校验，禁止在组件中维护另一套字段名。

### 5.2 两层 SSE

统一 fetch-stream client 只消费：

1. `GET /api/analysis/runs/{run_id}/events`：一页一条轻量 run snapshot stream；
2. `GET /api/analysis/runs/{run_id}/sessions/{session_id}/events`：当前未结束 session 的 Agent/Evidence 视图共用一条 process stream。

必须正确处理跨 network chunk 的 UTF-8、CRLF/LF、multi-line `data:`、comment keepalive、空行分帧、event name、id 和 JSON parse failure。不能假设一个 fetch chunk 就是一帧。

run stream：

- 首帧是当前完整 snapshot，后续是 replacement snapshots；
- 不发送 `Last-Event-ID`，不要求回放旧 run notifications；
- 切换水体保持 run stream；离开 run 页面才关闭；
- run 业务终态不等于立即关流，仍需接收 persistence 变化；
- 收到明确 `stream_end` 后停止重连；无 `stream_end` 的断开按中断恢复。

session stream：

- 首次打开未结束 session 时不先 GET transcript，无游标订阅即可收到此前全部可见过程并继续 live；
- 重连原样发送最后已消费的 session SSE integer id；按 SSE id 去重，不能按 `item_id` 丢弃合法的不同 delta；
- 切换水体或离开 Agent/Evidence 视图时关闭旧 session stream；两标签之间切换保持连接，不取消 run/session；
- 已结束 session 和严格历史使用 GET，不建立 live stream；
- `400 invalid_event_cursor`、`409 event_cursor_ahead`、`410 live_state_gone` 分别处理；`410` 使用后端 `history_url` 转入真实历史读取；
- 收到 `stream_end` 只表示该流明确结束，不表示 run 保存完成。

不新增第三条 Admin/reasoning/model/tool stream，不恢复旧 WebSocket，也不保留旧 `204` 正常结束分支。

## 6. New Analysis

左栏顺序保持：target/map point、query、model summary、date range、status、Start、Stop、帮助。

表单字段只包含当前 `CreateAnalysisRunRequest` 允许的内容：

- query；
- 可选 COMID 或地图坐标 target；
- 可选完整 start/end date range；
- time resolution preference；
- parser model；
- waterbody model；
- memory mode。

默认值来自 `GET /api/settings`；model 选择来自 backend role-filtered catalog。前端不提交 timezone、API key、provider base URL、parameter list、facility types、topology depth、sampling plan 或 tool 权限。timezone 和全局 unit policy 由 backend 在创建时固化进 configuration snapshot。

Start：

1. 在本地检查必填和成对字段；
2. 生成一次提交专用 UUID v4 idempotency key；
3. 固定 body bytes 并发送 create；
4. `201` 后导航到 `/runs/{run_id}` 并建立 run stream；
5. 超时/响应丢失时先用同 key + 同 bytes 重试，不生成新分析；
6. `410 idempotency_resource_deleted` 提示旧提交已删除，并允许用户明确发起一次新提交。

Stop 只调用 run cancel。关闭页面、切 tab、关闭 session stream 或离开地图均不触发取消。取消受理后继续展示各 session 的真实收敛状态，不立刻把全部节点染成 cancelled。

## 7. 地图、拓扑与 time series

地图只使用 backend topology/result identity 与坐标，不从 transcript 文本解析 COMID。地图默认 OpenStreetMap；tile unavailable 不影响已有结果文本的可读性。

节点视觉：

| backend 事实 | 视觉 |
|---|---|
| scope 内但不参与 | 半透明灰色 |
| 参与、尚未开始 | 实心灰色 |
| session running | 蓝色 + 低频呼吸外圈 |
| session completed | 绿色 |
| session failed | 红色 |
| session cancelled | 橙色 |
| run 取消导致参与节点未创建/未开始 | 灰色 + 虚线外圈/停止标记 |

呼吸只作用于外圈；第一版不增加 reduced-motion 分支。截图测试冻结动画。颜色必须同时配 legend、tooltip/status label；不得仅靠颜色传达状态。

点击节点：

- 有 session 时用 `run_id + session_id` 更新 URL 与右栏；
- 尚无 session 时显示 topology/scheduling 状态，不伪造空 session；
- running session 打开 Agent 或 Evidence tab 时按需接同一条 session stream；
- completed/failed/cancelled 或 history 使用 GET；
- 节点没有 result 时不画空 chart。

time-series 在地图底部可关闭区域展开，只画 backend 结果提供的 series、window、unit 和 quality/availability。不得使用 `value * (1-confidence)` 等前端公式制造 confidence band，也不得自行补点、换单位或生成风险。

## 8. 右侧 Detail

### 8.1 Overview

展示 run status/phase、后端 run summary、graph coverage、partial/失败分支、特殊值与 persistence。run summary unavailable/failed 时显示安全失败信息，不用前端模板拼一个“看似成功”的总结。

### 8.2 Agent

展示当前选中 waterbody session 的：

- pending/running/completed/failed/cancelled；
- 分窗口结果、日期范围总体结果、availability、risk、confidence 和理由；
- message/tool/evidence/decision/result 的有序过程；
- generating/completed/incomplete message 状态。

用户不能向 Agent 追加消息。skill/tool/hook、小型技术记录即使 inline 也默认折叠；大正文显示 preview，点击后按 `content_ref` 读取完整保存内容。live 未保存内容必须和 strict history 区分，不能用 live 补齐 history。

### 8.3 Evidence

展示 source attribution、EvidenceChain、当前 evidence 和 M8 historical context。historical item 必须标识 contextual_only/memory_augmented 等真实 role；historical unavailable、invalid、empty、cleanup 后消失分别展示，不能把旧历史证据升级为当前观测。

异常/限制放在其影响的 summary、result、evidence 或 persistence 附近，不设置独立 Anomalies tab。

## 9. History

History 使用 `GET /api/analysis/runs?limit={1..200}&cursor=...`，默认 50。客户端：

- 不解析 cursor，不依赖 total count；
- 保持 backend 创建时间/run identity 排序，状态更新不重排旧项；
- 刷新首页查看新 run；
- `422 invalid_history_cursor` 是错误，不显示为空列表；
- 第一版不实现未提供的关键词、日期或 status filter。

每项显示 backend 已持久化的 query、status/time、target COMID/location、date range、mode/parameters、final confidence 和原 `analysis_timezone`。时区必须显式显示 IANA identity；缺失时显示 legacy/unknown，不用当前默认值补写。

选择 run 后绘制当次已保存 topology/result；选择水体后读取独立 session。相同 query/COMID 的不同 run 不合并，不重新执行分析。单 run 删除入口先调用 cleanup preview；存在 M8 consumers 时展示整个 cascade，再进行第二次确认。

## 10. Settings 与 Keys

Settings drawer 分组：

1. Analysis defaults：timezone、parser model、waterbody model、memory mode、resolution；
2. Units：backend registry 提供的全局 target units；
3. Memory/RAG：desired embedding profile 与 handoff 状态入口；
4. Credentials：OpenAI、Google Geocoding；
5. Advanced/Admin：导航到 `/admin`。

普通设置整份保存并携带 revision。`409 settings_revision_conflict` 时保留用户草稿，加载服务器新值并要求用户重新确认，不做字段级自动合并。

credential row 只显示 label、purpose、configured、source、masked hint、updated_at、store availability 和允许动作。保存后清空输入；页面状态、DOM 属性、错误和日志均不得保留原值。deployment 环境 secret 不能由 UI 删除；删除 user-store override 后若环境值存在，行应恢复为 deployment configured，而非显示空。

保存 key 不联网。`Test connection` 是独立明确按钮，显示可能产生外部用量；只返回 safe result，不展示 raw supplier response。

## 11. Admin

Admin 只展示真实资源：

- History cleanup：单批 run selection 或 all，preview、dependency/cascade、二次确认；
- RAG profile handoff：desired/active/building/reader-drain/cleanup-ready；
- RAG generations：health、失败 job retry、retired/failed generation 删除；
- Operations：活动与最近终态 operation 状态。

不提供 `Clear evidence cache`，因为当前没有持久化 evidence cache；不提供 `Clear all data`、任意 SQL、任意 generation id 文本执行或 API key cleanup 混合动作。

耗时动作创建 async operation。页面可见时有界查询 GET status；隐藏/离开停止查询但不取消 operation，返回后按 identity 恢复。同类活动 operation 不重复创建。terminal 后停止查询。

preview 显示将删除/影响的 run、dependent consumer、generation、reader、retained tombstone 和不会删除的资源。执行时只提交 backend preview token 与 required confirmation；token 过期或事实变化时重新 preview，不能让前端自行判断“差不多一样”。

## 12. Persistence warning/retry

`persistence.state` 独立于 run status：

- `up_to_date`：不显示失败入口；
- `pending`：显示保存中，不宣称已落库；
- `retry_exhausted`：显示 warning 和手动“重试保存”。

手动按钮只调用 `POST /api/analysis/runs/{run_id}/persistence/retry`。`202` 表示已受理/已有补写，`200` 表示当前无需补写，`409` 表示当前不能恢复。POST 超时不自动重发；先 GET 核对，再由用户决定。业务终态和原 warning 不被前端改写。

## 13. `frontend/` 结构与依赖原则

建议最小边界：

```text
frontend/
  src/
    api/          # generated types, HTTP client, fetch-SSE parser
    app/          # routes, providers, shell
    features/
      analysis/
      history/
      settings/
      admin/
    components/   # 无业务所有权的展示组件
    styles/       # tokens/layout
  tests/
  package.json
  package-lock.json
  vite.config.ts
```

使用 React、TypeScript、Vite、Leaflet/react-leaflet 和 chart library；不引入第二套 domain framework、WebSocket client、全局 event bus 或为单一页面预建插件系统。server state 以 feature reducer/store 隔离，只有证明 React 内建状态无法保持 run/session identity 时才增加通用状态库。

开发 Vite proxy 转发 `/api` 和 SSE；production 输出 `frontend/dist/`，由 backend 同 origin 托管。业务代码只使用相对 URL。

## 14. 前端验收

至少覆盖：

- unit/component：form bytes/idempotency、status mapping、message delta/finalize、settings conflict、masked key、preview confirmation；
- SSE parser：跨 chunk UTF-8、CRLF/LF、multi-line data、keepalive、id、malformed frame、stream_end、400/409/410；
- integration：run SSE + one selected session SSE、切换释放连接、GET/SSE 交叠不倒退、persistence retry；
- map：七种视觉语义、running 呼吸状态冻结截图、选择无 session 节点不伪造过程；
- E2E fake：create → live topology → selected Agent → result/evidence → history；cancel；settings/key fake；Admin preview/operation；
- production build：backend 能提供入口/assets，API 404 不 fallback，SSE 不缓冲；
- secret scan：bundle、fixture、snapshot、console 和测试 artifact 无真实 credential。

真实 provider、三平台和容量证据按实施设计分账；未运行不得写成通过。
