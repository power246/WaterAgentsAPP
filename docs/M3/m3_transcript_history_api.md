# M3 Transcript、History 与两层 SSE API / 搭建排障指南

更新日期：2026-09-14

**状态：Milestone 0–10 的历史实现与本机验证证据已经保留；针对提交 `452749b` 的审查发现 4 个 P1、5 个 P2，Milestone 11 已完成本机实现与适用验证，Milestone 12–16 尚未完成，当前保持 `partial / request changes`。** 纯字母 Bearer/Basic 凭据与显式 private value tags 已在正文、日志、SSE、live/history 和存储边界统一安全化；取消边界、引用依赖、手动补写竞争及 corrupt 全文读取仍不能视为已验收。三平台实际 CI 与跨系统迁移证据也仍为 partial，中心最终承接暂不登记 `implemented`。

工程原理见 [M3 实施详细设计](m3_transcript_history_implementation_design.md)。跨阶段前端行为集中在父目录 [前端接入约定](../frontend_integration_contracts.md)，本文负责具体 HTTP/SSE 表示和搭建步骤。实现后，普通 HTTP 以同步后的 OpenAPI 为可执行来源，SSE 时序与重连遵守本文。

## 1. 基础约定与路由

本地默认 `http://127.0.0.1:8000`。一个 run 是一次完整分析，session 是某个 Agent 的独立上下文。M3 保持同一数据库只有一个活动服务进程、一个 Uvicorn worker，允许多个 run/Agent 并发。认证、真实问题解析、真实推理和正式前端属于后续阶段。

| 方法 | 路径（均以 /api/analysis 开头） | 用途 |
|---|---|---|
| POST | /runs | 创建 run，可选幂等 key。 |
| GET | /runs | PostgreSQL 历史 run 分页。 |
| GET | /runs/{run_id} | run 详情、session 目录、run 自身完整记录。 |
| GET | /runs/{run_id}/sessions/{session_id} | 所选 Agent 详情。 |
| GET | /runs/{run_id}/sessions/{session_id}/transcript | 所选 Agent 完整过程与显式公共引用。 |
| GET | /runs/{run_id}/transcript/{item_id}/content | 原 item 全文。 |
| GET | /runs/{run_id}/events | run 状态通知 SSE。 |
| GET | /runs/{run_id}/sessions/{session_id}/events | Agent 过程 SSE。 |
| POST | /runs/{run_id}/cancel | 取消整次分析。 |
| POST | /runs/{run_id}/persistence/retry | 手动恢复保存。 |

所有响应带服务端新生成的 `X-Request-ID`，客户端同名 header 不决定它。error_id 标识安全错误，run_id/session_id/operation_id 标识业务归属；正文不携带数据库连接详情或原始异常。日期输出 UTC，ID 使用 UUID 字符串。

详情、transcript、全文支持 `view=auto|history`，省略即 auto，非法值 422。列表只查数据库，不支持 view；两条 SSE 只读 live，不接收 view。Agent transcript 不支持 limit/cursor；首次版本对未声明查询参数返回 422，避免前端误以为截断或筛选生效。

## 2. 创建与幂等

`POST /api/analysis/runs`，`Content-Type: application/json`：

```json
{
  "query": "Analyze water quality near Austin",
  "time_resolution": "12h"
}
```

query 必填、trim 后非空；time_resolution 可省略，允许 6h/12h/24h，省略使用 12h/default，显式值的 source 为 frontend。显式 null、auto、未知字段非法。M3 不提前新增位置/日期/水质参数/provider/key 字段，M4 按真实请求设计扩展。

可选 `Idempotency-Key` 是客户端随机 UUID v4。新的一次主动分析必须生成新 key，即使 query 相同。**重试时原样复用 key 和请求体。** 请求体按原始 HTTP bytes 比较，空白、字段顺序、转义或字段有无变化均可能冲突；前端保存第一次序列化得到的 body，不在重试时重新组装。

成功 `201 Created`，示例原创建回执：

```json
{
  "run_id": "10ef14bd-d0e7-4a70-9728-1fbb4ca7a83c",
  "status": "pending",
  "created_at": "2026-09-13T03:20:00.000000Z",
  "updated_at": "2026-09-13T03:20:00.000000Z",
  "configuration": {
    "analysis_timezone": "America/Chicago",
    "time_resolution": "12h",
    "time_resolution_source": "frontend"
  }
}
```

同 key + 同 bytes 始终返回原 run、201 和原回执，终态后也仍是原 pending；当前状态通过 GET/SSE 读取。不同 bytes 返回 409 `idempotency_key_conflict`；非法 UUID v4 返回 422；不同 key 或省略 key 创建新 run。不按 query 去重，幂等记录随 run 历史保留，不因重启、终态、时间经过或 live 清理而过期。

创建先确认事务再执行。同 key 并发等待默认最多 5 秒，超时 503 `idempotency_wait_timeout`；创建提交结果尚不确定时 503 `creation_commit_unconfirmed`，两者均不能推断没有创建。前端需要重试时沿用原 key/body。没有 key 的请求无法可靠去重网络重发，客户端不应静默重发它。

HTTP 断开不取消已受理创建。数据库已提交后 workflow 启动失败，仍返回原 201，该 run 随后呈现 failed/workflow_start_failed；重试不会再执行。

```js
// 一次新的主动提交建立一份上下文；仅这次提交的网络重试复用它。
const submission = {
  key: crypto.randomUUID(),
  body: JSON.stringify({ query, time_resolution: "12h" }),
};
const response = await fetch("/api/analysis/runs", {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    "Idempotency-Key": submission.key,
  },
  body: submission.body,
});
```

## 3. 当前与历史的共同返回模型

### 3.1 来源与保存情况

除创建回执和历史分页外，GET 读取响应包含 `source`、`persistence`、`issues`。source 为 live 或 postgresql；persistence 只有 live 能提供，数据库读取为 null。issues 是已知记录解析/依赖装配问题的安全列表，正常为空；它不把数据库整体故障转成成功空响应。

```json
{
  "state": "pending",
  "warning": null
}
```

上面是 PersistenceInfo 的完整形状。state 为 up_to_date/pending/retry_exhausted。生成中必需正文也会使状态 pending，正常等待时没有 warning；发生保存错误才填 PublicError。up_to_date 只保证目前已经产生的必需内容均已确认，不预告未来内容。retry_exhausted 优先，只有用户手动恢复才重开一轮。

postgresql + persistence=null 不表示历史必然完整；只表示这次读取没有 live 保存进度。completed_with_persistence_error 的原终态 warning 永久保留，后续保存恢复只改变当前 persistence。

### 3.2 RunSummary 与 SessionSummary

| DTO | 字段 |
|---|---|
| RunSummary | run_id、status、created_at、updated_at、query、configuration。配置来自原创建 item；损坏时为 null 并报告对应 issue。 |
| SessionSummary | session_id、agent_role（query_parser/waterbody）、target_comid（parser 为 null）、status、created_at、updated_at。 |
| ContentRef | run_id、item_id；URL 可由固定全文路径组装，不需要 content_id。 |
| ReadIssue | item_id、PublicError；正常无记录。 |

run 状态为 pending/running/completed/failed/cancelled/completed_with_persistence_error，session 只有 pending/running/completed/failed/cancelled。pending session 表示已经准备执行；没有创建 session 的水体不能开一条空过程流。

### 3.3 TranscriptItemView

| 字段 | 表示 |
|---|---|
| item_id、run_id | 原身份。 |
| transcript_sequence、occurred_at | 原 run 内序号与原创建时间；不随保存刷新。 |
| content_type、content_schema_version | 注册类型及版本。 |
| scope | `{session_id, agent_role, target_comid}`；run-owned 三项均 null。 |
| correlation | `{request_id, operation_id, error_id}`，不适用为 null，从权威字段/正文提取。 |
| tags、rag_eligible | 默认 []、false。 |
| confidence、provenance | 正文对应层级的真实值/来源；无适用值为 null。 |
| content_bytes | 完整安全序列化正文的 UTF-8 大小。 |
| content_ref | 原 ContentRef；内联 item 也可以走统一全文入口。 |
| content | 下列内联或引用表示。 |
| parse_status、parse_error | ok/unsupported/corrupt；后两种有安全错误，ok 为 null。 |

`TranscriptItemHeader` 是以上从 item_id 到 tags/rag_eligible 的原元信息，不含正文、字节数、正文派生置信度或 parse 状态，用于 message_started。

已解析小正文：`content={"kind":"inline","body":<该类型的严格正文>}`。大正文：

```json
{
  "kind": "reference",
  "preview_head": "安全原文的开头",
  "preview_tail": "安全原文的末尾",
  "truncated": true
}
```

未知/损坏正文使用 `content={"kind":"unparsed"}` 及原 content_ref，不假装有已解析 body。完整过程仍保留该记录和安全错误；其他正常记录继续返回。新写入不允许以 unparsed 绕过类型校验。

大小阈值默认 64 KiB；外置预览 head/tail 各最多 1 KiB，保留完整字符。preview 不是完整 JSON，也不是推理概述/总结；前端不能直接把它解析成预测值或覆盖已经收到的完整消息。skill/tool/hook 的折叠按语义，不按字节大小决定。

### 3.4 完整 item 示例

下面是 run 创建记录的完整内联表示：它的 transcript_sequence=2，而原 lifecycle sequence=1。字节数按紧凑 UTF-8 编码的 content.body 计算，示例排版缩进不属于原存储正文。

```json
{
  "item_id": "c5677c98-27ac-4881-b7ba-b06cc2e85482",
  "run_id": "10ef14bd-d0e7-4a70-9728-1fbb4ca7a83c",
  "transcript_sequence": 2,
  "occurred_at": "2026-09-13T03:20:00Z",
  "content_type": "lifecycle_event",
  "content_schema_version": 2,
  "scope": {
    "session_id": null,
    "agent_role": null,
    "target_comid": null
  },
  "correlation": {
    "request_id": "ec011edb-ef06-49f6-abd7-f2d9b587fd76",
    "operation_id": null,
    "error_id": null
  },
  "tags": [],
  "rag_eligible": false,
  "confidence": null,
  "provenance": null,
  "content_bytes": 340,
  "content_ref": {
    "run_id": "10ef14bd-d0e7-4a70-9728-1fbb4ca7a83c",
    "item_id": "c5677c98-27ac-4881-b7ba-b06cc2e85482"
  },
  "content": {
    "kind": "inline",
    "body": {
      "type": "run_created",
      "schema_version": 2,
      "run_id": "10ef14bd-d0e7-4a70-9728-1fbb4ca7a83c",
      "session_id": null,
      "sequence": 1,
      "occurred_at": "2026-09-13T03:20:00Z",
      "payload": {
        "status": "pending",
        "created_at": "2026-09-13T03:20:00Z",
        "configuration": {
          "analysis_timezone": "America/Chicago",
          "time_resolution": "12h",
          "time_resolution_source": "frontend"
        }
      }
    }
  },
  "parse_status": "ok",
  "parse_error": null
}
```

生命周期版本 2 的完整联合定义见 [实施详细设计 §5](m3_transcript_history_implementation_design.md#5-typed-content-与读取表示)。配置从这一原记录装配，原创建回执保留同次创建时的一份小型响应，后续 GET 不用回执覆盖当前状态。

## 4. GET 读取接口

### 4.1 run 历史列表

`GET /api/analysis/runs?limit=50&cursor=...`。limit 是 1–200 整数，默认 50；首次不带 cursor，之后原样回传 next_cursor，末页为 null。响应只有：

```json
{
  "items": [],
  "next_cursor": null
}
```

每个 item 为 RunSummary；为兼容局部配置损坏，列表项另带 `issues`（正常 []），不影响其他行和 next_cursor。排序 created_at DESC、run_id DESC，无 total，无关键词/状态/日期筛选。游标是后端编码的版本与位置，客户端不得解析或构造。非法格式或版本 422，不回首页。读取完全来自 PostgreSQL；新 run 刷新首页查看。

### 4.2 run 详情

`GET /api/analysis/runs/{run_id}[?view=history]` 返回：

| 字段 | 内容 |
|---|---|
| run | RunSummary。 |
| sessions | 已实际创建的 SessionSummary 数组，按 created_at/session_id 升序。 |
| items | run 自身全部可见记录，按 transcript_sequence 升序。 |
| referenced_items | items 中显式引用的原记录闭包，去重、有序。 |
| source、persistence、issues | §3 读取上下文。 |

完整原请求、配置创建事件和后端实际 BFS 等属于 items；完整 Agent 消息留在所选 session。实际上下文或结果产生后，从注册类型中的原 item 读取，不另外保存地图快照或虚构结果。查询中途保存可能落后，history 不与 SSE 混合。

### 4.3 Agent 详情与完整 transcript

`GET /api/analysis/runs/{run_id}/sessions/{session_id}` 返回 `{run_id,session,transcript_url,source,persistence,issues}`；session 是 SessionSummary。真实业务结果类型由 M6 扩展详情装配，从原结果引用取得；M3 不创建假 result 字段。transcript_url 保留所选 view。

`GET .../sessions/{session_id}/transcript` 返回 `{run_id,session_id,items,referenced_items,source,persistence,issues}`。items 是该 session **全部**可见过程，referenced_items 是从全部记录显式关联的公共上下文。没有 Agent 记录分页、tail、条数截断；不能只返回最终总结选中的步骤。两数组中的身份与 run 序号都保持原值，引用去重不改归属。

前端先在 run 目录找到所选水体的 session_id，再读详情/过程；后端验证 session 确属 run。未实际创建的水体没有空 session 可读。COMID 不是第二套读取入口。

### 4.4 全文

`GET /api/analysis/runs/{run_id}/transcript/{item_id}/content[?view=history]` 返回 `{run_id,item_id,content_type,content_schema_version,content_bytes,text,source,persistence,issues}`。text 是完整安全 UTF-8 JSON 原文本，经过 JSON HTTP 字符串转义后传输；客户端应根据类型/版本解析它，不把 HTTP wire bytes 当 content_bytes。

内联与外置使用同一入口。当前 live 数据库故障时仍可读内存全文并保留 warning；live 释放后相同身份自动读 PG。history 模式只返回已保存文本。引用与全文请求必须继续传原 view。

auto 的全文和引用同样屏蔽取消后审计，回退 PG 不放宽；history 才能读取已提交的 late_message_output。隐藏或不属于 run 的 item 返回 404；外置正文缺失返回明确 500/transcript_content_unreadable，不返回空字符串。

## 5. 两层 SSE

### 5.1 公共传输

成功始终 `200`、`Content-Type: text/event-stream; charset=utf-8`、`Cache-Control: no-cache`、`X-Request-ID`；沿用禁用代理缓冲的响应设置。每帧空行结束。两流每 15 秒空闲发送 `: keepalive` 注释，无编号、不回放。不保留 M2 的 204 分支。

HTTP 握手错误先返回 JSON。已发 200 后出现流处理异常，记录 request/run/session/error 关联并中断，不能追加伪造成功或 stream_end；客户端按中断处理。用户主动关闭连接不影响执行或保存。

### 5.2 run_snapshot

打开当前 run 页面连接 `GET /api/analysis/runs/{run_id}/events`，首先发送最新快照，之后状态改变继续发送完整快照；重连不补播旧快照。通知不带原请求、完整配置或完整 Agent 消息，不另行入库。

```text
event: run_snapshot
data: {"schema_version":1,"run_id":"10ef14bd-d0e7-4a70-9728-1fbb4ca7a83c","status":"running","created_at":"2026-09-13T03:20:00.000000Z","updated_at":"2026-09-13T03:20:00.010000Z","sessions":[],"waterbodies":[],"persistence":{"state":"pending","warning":null}}

```

sessions 使用 SessionSummary。waterbodies 条目为 `{target_comid,participating,session_id,session_status}`，由实际 BFS/调度/session 事实装配；没有 session 时后两项 null。未产生拓扑时数组空，不伪造水体。waiting/excluded 的区别看 participating，running/completed/failed 看真实 session；取消后的颜色留给前端设计。

run_snapshot 没有 SSE id；客户端不为 run 保存 Last-Event-ID，服务端忽略该头。已经收到 SSE 状态后，迟到 GET 不覆盖它；不引入 state_revision 或时间戳竞争规则。GET 用于首次加载、断线核对与历史，不持续轮询。

### 5.3 session 过程与三个消息事件

只在打开所选 Agent 的实时过程时连接 `GET /api/analysis/runs/{run_id}/sessions/{session_id}/events`。首次不带游标：已有的全部可见过程先发完，再继续新增，不要求先 GET。切换水体关闭旧 session 流；页面通常保留一条 run 流加当前这一条 session 流。

所有过程事件的 data wrapper 为 `{schema_version:1,run_id,session_id,sequence,occurred_at,payload}`。sequence 与 SSE id 相同，属于该 session 的可见事件，不是 transcript_sequence。occurred_at 是这次事件产生时间，payload.item 的 occurred_at 是原 item 创建时间。

| event | payload |
|---|---|
| transcript_item | `{item: TranscriptItemView}`，原非流式可见记录。 |
| message_started | `{item: TranscriptItemHeader}`，原消息一次性元信息。 |
| message_delta | `{item_id,text}`，只含新增安全文本。 |
| message_finalized | `{item: TranscriptItemView,generation_status:"completed"或"incomplete"}`，状态从原正文派生。 |

message_started 的类型表示哪种注册消息正在生成；具体消息正文由生产阶段注册。M3 用严格测试类型验证事件能力，不制造生产 LLM 消息结构。以下只展示与具体业务类型无关的 delta frame：

```text
id: 3
event: message_delta
data: {"schema_version":1,"run_id":"10ef14bd-d0e7-4a70-9728-1fbb4ca7a83c","session_id":"7d0a9cea-d754-4d88-8271-f4a38b8fa71f","sequence":3,"occurred_at":"2026-09-13T03:20:00.200000Z","payload":{"item_id":"6ad82df9-8d67-46ec-9599-2a8d3528d281","text":"已取得当前证据。"}}

```

前端按 item_id 更新同一条消息，按 event sequence 去重；不能因同 item_id 丢掉下一片段，也不能把累计文本再次拼接。started 早于首个 delta，finalized 晚于最后可见 delta；不同消息与过程事件可交错。

generating 只存在 live 消息正文；finalized 的状态派生自最终正文。收到 finalized 结束该消息打字效果，不追加第二条消息，不判断 Agent/run 结束或已保存。大正文仍是 preview/ref，不能用预览覆盖已经显示的完整文本；需要时读取原全文。数据库只保存原定稿正文一次，片段与 finalized 不逐帧存表。

### 5.4 游标与结束

重连 header：`Last-Event-ID: 3`。只接受非负 ASCII 十进制，0 等于起点；非法 400/invalid_event_cursor，超过该 session 末尾 409/event_cursor_ahead，不自动回零。重新打开页面而没有本地原消息状态时，应首次不带游标，不能只持久化一个尾部游标却期待重建此前全文。

两流满足各自关闭条件后发送：

```text
event: stream_end
data: {"schema_version":1,"run_id":"10ef14bd-d0e7-4a70-9728-1fbb4ca7a83c","session_id":"7d0a9cea-d754-4d88-8271-f4a38b8fa71f","stream":"session"}

```

run 流用 `stream="run",session_id=null`。stream_end 没有 id、不入库，前端收到后停止这条连接的重连；它不表达成功。session 在自身终态且全部可见过程发完后结束；run 须业务终态、执行收敛、所有必需写入确认，先发送最终保存快照再结束。保存中/耗尽的 run 通知流仍可继续，结束不等待清理 grace。

已终态但 live 仍在的合法请求也返回 200：session 先补齐再 end，全部消费则直接 end；run 先发快照，满足完整条件才 end。没有 stream_end 的意外 EOF 按中断处理，不猜测业务完成。

live 已释放或重启后历史仍在，握手返回 `410 Gone`，body.history_url 分别指向 run 详情或 session transcript，并显式带 view=history；前端停止重连、转已保存历史。不存在 404，DB 错误 503。历史不能恢复 token 节奏，也不重跑 Agent。

M9 使用基于 fetch 的统一 SSE 客户端，才能读取握手错误状态与 history_url；具体解析库由 M9 选择。

## 6. 取消与手动保存

### 6.1 cancel

`POST /api/analysis/runs/{run_id}/cancel`，无 body。live 成功 `200` 返回 `{run_id,status,created_at,updated_at,persistence}`。首次取消立即选定 cancelled、阻止新调度并内部取消未终态 sessions；已 cancelled 幂等返回 200。completed/failed/completed_with_persistence_error 已胜出时沿用 M2 的终态冲突语义，返回 409 `run_already_terminal`，error body.run 附同形状的当前状态，不改写完成/失败记录。

仅历史存在时按已保存终态使用同样规则：cancelled 返回 200，其他终态返回 409/run_already_terminal，persistence=null。没有可执行 live、历史非终态的异常窗口先收敛创建所有权或返回安全 409 `run_not_live`，不能创建伪造取消结果。不存在 404，必要数据库查询失败 503。

用户只能停止整次 run，不能给 Agent 追加消息；没有单 session cancel HTTP。取消受理不等在途请求完成、数据库写完或 SSE 关闭。受理时原生成中消息固定为 incomplete，晚到新片段只形成 history-only 审计。auto/SSE 不继续显示晚到内容，history 可读已保存审计。

### 6.2 persistence/retry

`POST /api/analysis/runs/{run_id}/persistence/retry`，无 body，不使用创建 key。响应 `{run_id,persistence}`：

| 当前情况 | HTTP / 行为 |
|---|---|
| retry_exhausted，live 待写仍在 | 202，原 writer 开启新一轮首次尝试 + 最多 5 次自动重试。 |
| pending（SQL/退避/等待定稿） | 202，复用原处理，不重置预算、不强制定稿。 |
| up_to_date | 200，无空事务。 |
| live 已丢失、历史仍在 | 409 / persistence_retry_unavailable / retryable=false。 |
| 完全不存在 | 404。 |

202 仅表示仍由原保存过程处理，不能显示“保存成功”。自动重试最多 5 次，间隔 1/2/4/8/16 秒；耗尽后 DB 恢复、刷新和新消息不自动重开。手动 POST 超时先 GET 或观察 run SSE 核对现况，不自动再次发送；用户再次明确点击才能重新操作。网络断开不撤销已受理补写，不重新分析。

## 7. 错误与排查字段

错误统一形状为 `{error,run,validation,history_url}`：error 在原 PublicError 上增加本次 request_id 与可用 run_id/session_id；run 仅在终态取消冲突等需要当前状态时提供 §6.1 的轻量状态对象，否则 null。validation 正常 []，history_url 非 410 时 null。PublicError 包含 error_id、code、message、failed_stage（可空）、retryable。校验项沿用 M2 的 field/type/message，不回显原 input。

| HTTP | code | 含义 / 客户端动作 |
|---|---|---|
| 400 | invalid_request / invalid_event_cursor | 请求编码或 session SSE 游标格式非法，不静默吞错。 |
| 404 | run_not_found / session_not_found / transcript_item_not_found | 不存在、归属不匹配或 auto 不可见。 |
| 405 | method_not_allowed | 路径存在但 HTTP 方法不受支持；按 OpenAPI 使用准确方法。 |
| 409 | idempotency_key_conflict | 同 key 不同 body；回到原提交核对，不能自动新 key 重跑。 |
| 409 | event_cursor_ahead | 游标不属于当前已有过程范围，核对 run/session。 |
| 409 | run_already_terminal | 其他终态先于取消胜出，使用 body.run 核对当前状态。 |
| 409 | persistence_retry_unavailable / run_not_live | 无可恢复 live 所有者，不假装恢复/取消成功。 |
| 410 | live_state_gone | 停止 SSE 重连，读取 history_url。 |
| 422 | validation_error / invalid_history_cursor | 请求字段、UUID、view、limit 或历史 cursor 非法。 |
| 500 | transcript_content_unreadable / internal_error | 已知损坏或未预期错误，保留 error_id。 |
| 503 | database_unavailable / service_unavailable | 必要数据库操作失败或服务正在关闭；不返回假空历史。 |
| 503 | idempotency_wait_timeout / creation_commit_unconfirmed | 创建结果尚未收敛，保留原 key/body。 |

上表只列目标路由的 HTTP error envelope code，覆盖实际 OpenAPI 声明的全部 400/404/405/409/410/422/500/503 错误状态。成功读取中的逐 item `parse_error.code` 为 `transcript_content_unsupported` 或 `transcript_content_corrupt`，不把局部问题冒充整个数据库宕机。run 级 `persistence.warning.code` 为可重试的 `persistence_write_failed` 或不可重试的 `persistence_write_conflict`；它们不是 HTTP 503 code，也不覆盖原事实。run/session lifecycle 中的 workflow/重启错误继续使用原事件内的 `workflow_start_failed`、`workflow_failed`、`service_interrupted`；启动修复自身失败在服务接收 HTTP 前以 `startup_repair_failed` 诊断并退出。公开摘要固定安全，不暴露 SQL、连接串、凭据或原始 validation input。

## 8. 跨平台搭建与配置

以下文件与命令是当前已交付的 **M3 运行入口**；所有命令从仓库根运行。已有 PostgreSQL 可跳过 Compose，设置同一组连接变量即可。

### 8.1 环境变量

| 变量 | 默认值 / 要求 |
|---|---|
| WATERAGENTS_DB_HOST / DB_PORT | 127.0.0.1 / 5432；第二项完整名为 WATERAGENTS_DB_PORT，其余表内同理。 |
| WATERAGENTS_DB_NAME / DB_USER | wateragents / wateragents。 |
| WATERAGENTS_DB_PASSWORD | 正式连接前必填 SecretStr，无默认值；不 trim。 |
| WATERAGENTS_DB_SSLMODE | disable 供本机；远程推荐 verify-full。 |
| WATERAGENTS_DB_SSLROOTCERT | 可空；远程使用服务方 CA 文件路径。 |
| WATERAGENTS_DB_POOL_SIZE / DB_MAX_OVERFLOW | 5 / 5，非负配置且 pool_size>0。 |
| WATERAGENTS_DB_OPERATION_TIMEOUT_SECONDS | 5，>0；单次 DB 尝试总执行预算，读检查也使用有限预算。 |
| WATERAGENTS_IDEMPOTENCY_WAIT_TIMEOUT_SECONDS | 5，>0；同 key 等待总预算。 |
| WATERAGENTS_PERSISTENCE_BATCH_ITEMS | 100，正整数；不能拆原子组。 |
| WATERAGENTS_TRANSCRIPT_INLINE_MAX_BYTES | 65536，正整数。 |
| WATERAGENTS_TRANSCRIPT_PREVIEW_BYTES | 2048，正偶数且小于内联上限，首尾各一半。 |
| WATERAGENTS_LIVE_STATE_GRACE_SECONDS | 60，>=0。 |
| WATERAGENTS_SHUTDOWN_SAVE_GRACE_SECONDS | 5，>=0；关闭时写入收尾窗口，不是运行期重试预算。 |

原 WATERAGENTS_HOST/PORT/ANALYSIS_TIMEZONE/LOG_LEVEL 等继续有效，analysis timezone 默认 America/Chicago。五次重试及 1/2/4/8/16 秒为固定策略，不额外开放另一组配置破坏已确认预算。keepalive 15 秒沿用原测试注入点。

设置优先级为显式 settings 注入 > 进程环境 > backend/.env > 默认；`.env` 的定位相对 backend 根，不随 cwd 改变。缺少密码在生产数据库创建/命令执行时明确失败，不能使纯模型导入或注入 fake store 的测试强制读取真实凭据。

应用与独立迁移 CLI 共享配置。密码通过独立字段传入 URL.create，不对 `@:/#%$` 手动编码；`.env` 使用单引号保留字面量，含单引号或换行等值优先使用部署系统的环境/secret 注入，并测试实际解析。不得打印 `docker compose config` 的完整凭据展开结果作为诊断。

### 8.2 本地 Compose

交付 `backend/compose.yaml` 的最小约定：postgres:17、命名 volume、127.0.0.1 端口绑定、pg_isready healthcheck；Compose 内账号/密码/库名从同一 WATERAGENTS_DB_* 配置映射到 POSTGRES_*，不用另一份凭据文件。卷挂载 `/var/lib/postgresql/data`（本方案 PG17），service 名为 postgres，端口映射到容器 5432。

macOS / Linux：

```bash
cp backend/.env.example backend/.env
```

Windows PowerShell：

```powershell
Copy-Item backend/.env.example backend/.env
```

填写自己的数据库密码后，各平台共用：

```text
docker compose --env-file backend/.env -f backend/compose.yaml up -d --wait postgres
uv sync --project backend --extra dev --locked
uv run --project backend wateragents-db upgrade
uv run --project backend wateragents-db check
uv run --project backend python -m app
```

check 必须确认实际连接及 Alembic 当前 revision 等于代码单 head；仅 Compose healthy 不代表业务表已初始化。`upgrade` 不启动服务、不恢复 Agent；应用启动不自动改表。

标准 venv 替代 uv：macOS/Linux 使用 `python3 -m venv backend/.venv` 后激活 `source backend/.venv/bin/activate`；Windows 使用 `py -3.11 -m venv backend/.venv` 后 `.\backend\.venv\Scripts\Activate.ps1`。激活后共用：

```text
python -m pip install -e "./backend[dev]"
wateragents-db upgrade
wateragents-db check
wateragents
```

每台机器新建 venv，不复制虚拟环境。Windows 正式入口必须在 loop 创建前配置 Selector loop；直接调用自定义 Uvicorn 启动方式必须复用同样初始化，不能把默认 Proactor 配置列为已支持的启动命令。

### 8.3 已有数据库与升级

远程目标为 PostgreSQL 17 的独立业务数据库，配置 host/port/name/user/password 和 sslmode=verify-full，必要时提供 CA。库和连接账号由环境预置；`wateragents-db upgrade` 管理业务 schema，不创建数据库服务器或外部账号。迁移所用账号需有对应 DDL 权限，运行时账号按环境提供业务表读写权限；二者可通过命令环境变量覆盖同一配置，无需在应用里保存两套密码。

部署顺序：停止旧单实例 → 备份/保留数据库 → 同步配套代码与依赖 → 执行 upgrade → check → 启动。没有 schema 变化的重启无需重复 upgrade。应用发现空库、旧版、新版、多个或未知 revision 都拒绝启动；旧/空库提示独立升级，新/未知版本提示核对配套代码，不自动降级或 stamp 掩盖问题。

`upgrade` 基于 Alembic 单一迁移链；未来修订用新 migration，不改已发布 migration。开发验收记录实际 PostgreSQL minor 与镜像 digest、依赖锁及 revision。本文是 PG 首发方案，没有 SQLite schema 转换或历史数据搬迁。

### 8.4 停止、重启与检查

正常退出后端使用 Ctrl-C；关闭不伪造用户 cancel。停止本地数据库并保留数据：

```text
docker compose --env-file backend/.env -f backend/compose.yaml stop postgres
```

重新执行 up 与后端启动即可；`down` 默认保留命名卷，`down -v` 会删除数据，不列作日常停止步骤。更改 .env 密码不会自动更改已有卷中的数据库账号密码，应通过正常账号管理修改，再同步配置。

```text
uv run --project backend python -c "import httpx; r=httpx.get('http://127.0.0.1:8000/health',trust_env=False); r.raise_for_status(); print(r.json())"
```

health 保持现有进程健康语义；启动阶段已通过数据库检查，运行期 `/health` 不能单独证明某个 run 保存成功，应查看该 run.persistence 与严格历史。

### 8.5 更换操作系统或机器的最短流程

目标是 macOS、Windows、Linux 共用同一份代码、配置模板、依赖锁和 schema；迁移只调整目标环境配置并重新安装依赖。下列运行手册与三平台 CI 入口已实现，但没有三个远端目标系统依次接回同一数据库的本次实跑证据，因此跨系统验收仍为 partial。

迁移前停止旧实例接收新分析。需要完整保留当次分析时，先确认 run 已结束、执行已收敛且必需写入全部保存（正常 run SSE 的 stream_end 可作为对应信号），再关闭旧后端；新后端启动前旧实例必须退出。尚未保存的内存不会随源码迁走，已保存非终态沿原 service_interrupted 规则处理。

| PostgreSQL 使用方式 | 目标环境操作 |
|---|---|
| 继续使用同一可达实例 | 配置原数据库连接，执行 check 后启动；已有历史保持原身份，不因换系统执行 schema 转换。原本只监听旧机器 loopback 的本地数据库不能直接当作可达远程实例，连接条件按 §8.3 配置。 |
| 使用全新空实例，不需要原历史 | 按 §8.2 启动本地 Compose，或配置已有空库；执行 upgrade 初始化同一 schema，再 check 和启动。原历史仍在原库，本步骤不搬迁内容。 |

在目标机器取得同一代码版本（包括 backend/pyproject.toml、uv.lock、迁移文件及配置模板），按 §8.1 填写或安全注入环境变量；数据库凭据和证书位置按目标环境调整，不提交真实 .env。不复制旧 .venv、Python 可执行文件、缓存或 PostgreSQL 原数据目录/容器卷作为跨系统迁移方式。

各平台从仓库根使用相同命令，无需激活系统专用虚拟环境脚本：

```text
uv sync --project backend --extra dev --locked
uv run --project backend --locked wateragents-db check
uv run --project backend --locked python -m app
```

若连接全新空库，在 check 前运行下面的初始化命令；同版本原库不需要因为换操作系统再次初始化：

```text
uv run --project backend --locked wateragents-db upgrade
```

依赖由目标平台重新安装，锁文件保持不变。迁移后先检查 health；接回原数据库时，再用 view=history 核对原 run/session、分页游标、全文引用及创建回执，并运行独立测试数据库上的 smoke。旧实时流转历史，不自动续跑 Agent。

跨系统验收必须包含含空格/中文的项目路径、不同启动 cwd、UTF-8 query/大正文、LF/CRLF 配置与正常关闭。三系统分别作为目标环境验证，同库切换期间只有一个活动后端；不能把同一系统重新安装或三个孤立新库的启动成功当成历史可迁移的证明。后续若另有数据库内容搬迁需求，再单独安排 PostgreSQL 数据迁移，本节不增加该交付范围。

## 9. 排障与验证

| 现象 | 检查与处理 |
|---|---|
| 启动 connection refused / authentication failed | 查看脱敏错误码与 DB host/port/name，确认实例可用及密码来源；不输出完整连接串。 |
| schema_missing / schema_outdated | 停止服务，使用配套版本执行 wateragents-db upgrade，再 check。 |
| schema_incompatible | 核对代码与库，不能自动 downgrade/stamp。 |
| startup_repair_failed | 查看 error_id，修复实际数据库故障后重新启动；已经修复的原终态不重复改写。 |
| Windows DB 异步异常 | 检查是否走正式 Selector loop 启动入口，验证实际连接和资源关闭。 |
| 实时有结果，history 暂缺 | 检查 persistence；history 只代表已保存部分，不自动混入内存。 |
| 完成但保存警告 | 业务终态保持；后台有限补写，耗尽后用户可手动恢复。 |
| SSE 返回 410 | 停止重连，读取明确 history URL，不能创建新分析“恢复”。 |
| query 日志消失或凭据出现 | 检查实际日志入口与安全过滤；普通 query 保留，凭据始终过滤。 |

M3 实现后，基于独立测试数据库执行：

```text
uv run --project backend --extra dev python -m pytest backend/tests -q
uv run --project backend --extra dev python -m pytest backend/tests/persistence -q
uv run --project backend --extra dev python -m pytest backend/tests/api/test_m3_documented_protocol.py -q
uv run --project backend --extra dev python backend/scripts/health_smoke.py
uv run --project backend --extra dev python backend/scripts/run_history_smoke.py
```

实现应交付测试数据库的独立配置与创建/清理 fixture：只使用明确指定的测试数据库/临时 schema，禁止清空开发或业务库；PG 集成任务缺少连接时明确失败，不能静默 skip 后宣称通过。smoke 使用动态 loopback 端口并绕过环境代理，关闭后验证连接、writer 和子进程实际退出。

测试环境提前创建独立数据库（例如 wateragents_test），通过 `WATERAGENTS_TEST_DB_NAME` 显式指定；host/port/user/password/SSL 复用测试环境提供的 WATERAGENTS_DB_* 连接参数。fixture 和 smoke 构造测试 AppSettings 时将 db_name 替换成该值，要求它与配置中的业务 DB_NAME 不同；缺少测试库名或连接失败就拒绝执行数据库集成。每组测试使用独立临时 schema，并使表查询、Alembic version 表和子进程使用同一 schema；清理只删除本组创建的 schema，不删除数据库或生产表。该变量不是正式服务的隐式数据库覆盖开关。

OpenAPI 必须覆盖十路由、view/分页参数、create/cancel/retry 成功与错误；SSE 文档及可执行 fixture 覆盖 run_snapshot、transcript_item、三个消息事件、stream_end、400/409/410。`test_m3_documented_protocol.py` 直接读取实际 OpenAPI，并用 production registry 与 SSE encoder 核对本文的错误状态、状态枚举、完整 item 字节数、UTC 时间和 raw frame。每个 Milestone 的实际命令、平台、结果和未完成项记录在 [实施清单](m3_implementation_todo.md)，本文不预填测试通过数量。
