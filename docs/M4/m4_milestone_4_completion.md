# M4 Milestone 4 完成记录

记录日期：2026-09-25

实现分支：`codex/m4-query-topology`

## 1. 阶段结论

Milestone 4 已完成 Query Parser 的 application DTO/port、确定性 prompt、一次 schema repair、真实 parser Agent session、流式 message transcript、`query_understood v1` 交接和稳定失败/取消边界。通过注入 fake parser 与 fake context resolver，当前 workflow 可以留下完整的 parser session 过程，并产出严格 `ParsedIntentV1` 和唯一 `AnalysisContext`。

本阶段没有调用真实付费模型，没有接入 provider SDK adapter、geocoder/NLDI、topology BFS、observation/evidence 或 waterbody 模型。

## 2. Parser boundary 与 prompt

- `QueryParserPort.generate()` 只接受 application-owned `QueryParserInputV1`、delta callback，并返回 `QueryParserResponse`；provider SDK 类型不会进入 application/domain。
- provider request 固化 parser `ModelSelection`，不读取或调用 waterbody selection；不存在模型猜测、fallback 或前端 credential 字段。
- `QueryParserInputV1` 固定 prompt/schema version，精确包含 system + user messages，`tools` 必须为空；任何非空 tool roster 在进入 adapter 前即被拒绝。
- system prompt 包含原 `ParsedIntentV1` JSON schema、154 项唯一 capability registry 的规范名称/单位/语义，以及显式参数、自主推测和歧义上报三个 few-shot。
- user prompt 只包含安全 query、structured target/date/resolution、同一 run creation decision instant 推导的 local today 与固化 timezone。
- repair request 不重复原 query、structured selections、capability summary 或 few-shot；其 user message 只含安全原输出和不带 input/context 的 schema 错误摘要。

## 3. 调用、repair 与取消

- 正常仅调用一次。只有原始文本不能通过 `ParsedIntentV1` JSON/schema 时才调用一次 repair；第二次仍非法即 `query_parse_failed`，不会第三次调用。
- schema-valid 的 ambiguity、parameter unsupported 及其他 deterministic resolver 业务失败直接结束，不触发 repair。
- provider error/timeout 映射为 `parser_model_unavailable` 且不切换模型；tool-call、非 stop finish 或 streamed/final text 不一致在 resolver 前拒绝。
- parser timeout 使用已有严格 setting；provider 调用与 resolver 都同 run cancellation token 竞速。取消获胜后 provider task 被取消，已有 message 按 M3 规则定稿为 `incomplete/cancelled`，不启动 repair、resolver 或后续阶段。
- 模型输出先按严格 schema 解析，再对 typed fields 做结构化脱敏，避免把未完成 JSON 当业务事实，也避免凭据进入最终 context。

## 4. Session、transcript 与终态

- workflow 准备调用模型时创建唯一 `query_parser` session，`target_comid=null`，并进入 running。
- session 保存一个 `query_parser_input v1`；每次调用创建独立 operation 和原始 `query_parser_message v1`，repair 不覆盖第一条消息。
- message 沿用 M3 `message_started / message_delta / message_finalized`、统一流式脱敏、取消冻结、late-output 和 writer 持久化路径。
- context resolver 成功后先把唯一 `AnalysisContext` 保存为 parser-session-owned `query_understood v1`，再完成 session；失败保存 session/run lifecycle error，不写部分 context。
- `query_parser_input`、`query_parser_message` 和 `query_understood` 已加入 production registry，并继续由 registry 在 live buffer 与持久化前双重校验/脱敏。
- `WorkflowContext` 只新增最小 `WorkflowOperations` 协议和安全 typed request；`RunCoordinator` 仍持有生命周期、并发、取消和 writer 所有权。

## 5. 验证证据

| 验证 | 结果 |
|---|---|
| M4 parser prompt + fake-provider workflow focused | `15 passed` |
| parser DTO/prompt/registry focused | `24 passed in 0.22s` |
| `python -m pytest backend/tests/application backend/tests/observability -q` | `480 passed in 13.16s` |
| `python -m pytest backend/tests/api/test_analysis_events.py -q` | `38 passed in 2.37s` |
| `python -m pytest backend/tests/architecture -q` | `77 passed in 1.16s` |
| 完整非 PostgreSQL backend 回归 | `1003 passed in 36.41s` |
| `python -m compileall -q backend/app` | passed |

所有 parser 调用验证均使用 fake adapter，实际 provider 请求次数分别断言为 1 或 2，且每次 `tools=[]`；没有发生真实模型请求或费用。真实 PostgreSQL integration 继续不属于本阶段验收，完整回归排除 `backend/tests/persistence/test_postgres_persistence.py`。

## 6. 后续边界

Milestone 5 可以实现真实 geocoding/NLDI、河段和 observation/availability adapters，并消费已持久化的同一 `AnalysisContext`。真实 provider SDK adapter、topology graph/BFS、evidence gate 与 waterbody model 仍不在本提交中；任何 provider adapter 必须实现当前 port、保持空 tools，并从后端 secret 配置获取凭据。
