# M6 Single-Agent Reasoning Implementation TODO

更新日期：2026-10-02

> 当前状态：设计、policy、API/读取契约和本施工清单已确认，M6 代码实现与验收尚未开始。所有复选框保持未勾选；coding agent 只能凭本阶段实际实现和验证证据勾选。M6 是 M1–M9 同一最终系统中的内部实施切片，不形成独立发布版本。

本清单执行以下已确认文档：

- [M6 设计决策 D01–D16](m6_single_agent_reasoning_design.md)
- [M6 实施设计](m6_single_agent_reasoning_implementation_design.md)
- [M6 Reasoning Policy Catalog](m6_reasoning_policy_catalog.md)
- [M6 API / 读取契约](m6_single_agent_reasoning_api.md)
- [跨 Milestone M6 契约](../deferred_milestone_contracts.md#8-m6single-agent-reasoning)
- [M5 Implementation TODO 的 M6 接力出口](../M5/m5_implementation_todo.md#m6--m7--m8-接力出口)
- [Docker PostgreSQL 快速指南](../docker_postgresql_quickstart.md)

若本清单与上述设计冲突，coding agent 不得自行选择实现成本更低的一项，也不得用兼容层同时保留两套行为。应先暂停当前阶段，在同一变更中把当前唯一 V1 设计、契约与清单修正一致，再继续实现。内部函数拆分、文件粒度和 fixture 组织可由实现者决定，但不能改变已冻结的 session 边界、station 起点、tool 权限、最终门禁、风险/confidence 责任、分批、repair、失败、SSE、持久化或 M7–M9 接力语义。

## 阶段与设计导航

| 实施阶段 | 主要设计依据 | 阶段产物 |
|---|---|---|
| [Milestone 0：基线、边界与原子更新清单](#milestone-0基线边界与原子更新清单) | [实施设计 §1–3](m6_single_agent_reasoning_implementation_design.md#1-核心结论与范围) | 可复现基线、影响表、archive 隔离、真实扩展接缝 |
| [Milestone 1：当前唯一 V1 inference domain](#milestone-1当前唯一-v1-inference-domain) | [实施设计 §4](m6_single_agent_reasoning_implementation_design.md#4-当前唯一-v1-domain-收敛) | 三态 point、五级风险、四层 confidence、window/range overall、availability |
| [Milestone 2：task、sealed gate 与结果骨架](#milestone-2tasksealed-gate-与结果骨架) | [设计 D01/D04/D05](m6_single_agent_reasoning_design.md#d01m6-提供单水体-runnerm7-只负责调度同一个-runner)；[实施设计 §2/§8](m6_single_agent_reasoning_implementation_design.md#2-application-输入与阶段状态) | validated task、sealed input、final gates、fixed cells、allowlist、skeleton |
| [Milestone 3：provider-neutral waterbody model port](#milestone-3provider-neutral-waterbody-model-port) | [实施设计 §5](m6_single_agent_reasoning_implementation_design.md#5-provider-neutral-waterbody-model-contract) | 独立 streaming port、strict events、fake stream、safe error mapping |
| [Milestone 4：prompt builder 与 policy audit](#milestone-4prompt-builder-与-policy-audit) | [实施设计 §6](m6_single_agent_reasoning_implementation_design.md#6-prompt-与-working-context)；[Policy](m6_reasoning_policy_catalog.md) | immutable base policy、四阶段 prompt、有界 working context、policy audit |
| [Milestone 5：evidence-planning Agent loop](#milestone-5evidence-planning-agent-loop) | [设计 D02–D04](m6_single_agent_reasoning_design.md#d02station-backed-与-stationless-使用不同启动策略)；[实施设计 §7](m6_single_agent_reasoning_implementation_design.md#7-evidence-planning-agent-loop) | station 分流、M5 iterative seam、tool/rule loop、rebuild/seal、调用计数 |
| [Milestone 6：确定性 reasoning batch planner](#milestone-6确定性-reasoning-batch-planner) | [设计 D06](m6_single_agent_reasoning_design.md#d06一个-agent-session-内分批推理最后做一次总体汇总)；[实施设计 §8.3](m6_single_agent_reasoning_implementation_design.md#83-batch-planner) | 稳定 batches、size/token fit、无漏无重、同 session identity |
| [Milestone 7：draft validation 与一次 repair](#milestone-7draft-validation-与一次-repair) | [设计 D12–D13](m6_single_agent_reasoning_design.md#d12单位聚合和-scope-权限继续由确定性工具执行)；[实施设计 §9.1–9.2](m6_single_agent_reasoning_implementation_design.md#91-cell-draft) | strict drafts、机械 validator、一次同 scope repair、whole-session failure |
| [Milestone 8：runner、overall synthesis 与结果装配](#milestone-8runneroverall-synthesis-与结果装配) | [设计 D07–D10/D15](m6_single_agent_reasoning_design.md#d07每个计划窗口完整输出三态结果)；[实施设计 §9.3–§10/§12](m6_single_agent_reasoning_implementation_design.md#93-skeleton-assembler) | 完整 runner、四层判断、deterministic assembler、技术/自然/取消收敛 |
| [Milestone 9：typed transcript、session SSE 与 history](#milestone-9typed-transcriptsession-sse-与-history) | [设计 D14/D16](m6_single_agent_reasoning_design.md#d14推理过程就是-session-sse-中的真实会话上下文)；[API §2–12](m6_single_agent_reasoning_api.md#2-统一-transcript-envelope-不变) | 六类内容、真实消息增量、result/completed 原子边界、summary/history projection |
| [Milestone 10：OpenAI Responses waterbody adapter](#milestone-10openai-responses-waterbody-adapter) | [实施设计 §5.4](m6_single_agent_reasoning_implementation_design.md#54-openai-responses-adapter) | tool-capable streaming adapter、checked-in provider fixtures、无 hidden reasoning/failover |
| [Milestone 11：settings 与 production/test composition](#milestone-11settings-与-productiontest-composition) | [实施设计 §13–14](m6_single_agent_reasoning_implementation_design.md#13-budget-与配置)；[API §14](m6_single_agent_reasoning_api.md#14-test-only-composition) | hard limits、M6 runtime、内部 production service、无新公开 route、direct test harness |
| [Milestone 12：单水体端到端与 PostgreSQL/SSE 验收](#milestone-12单水体端到端与-postgresqlsse-验收) | [实施设计 §15](m6_single_agent_reasoning_implementation_design.md#15-测试策略)；[API §15](m6_single_agent_reasoning_api.md#15-contract-验收) | fixed-fake E2E、cancel/repair/failure、持久化回放、现有两层 SSE |
| [Milestone 13：集成审计与 M7–M9 接力](#milestone-13集成审计与-m7m9-接力) | [实施设计 §16–17](m6_single_agent_reasoning_implementation_design.md#16-m7m8m9-接力) | 完整回归、契约矩阵、可选 provider smoke、orphan 扫描、接力证据 |

## Coding agent 执行原则

- [ ] 只以当前 `backend/`、`docs/M1`–`docs/M6` 和项目级当前契约为实现输入。`backend_bk/`、`frontend_bk/`、`docs/backend_refactor_contract.md` 是完全不相关、最终丢弃的归档；不得读取、导入、复制、适配、枚举、运行或把其中行为当测试 oracle。
- [ ] 开始每个 Milestone 前记录当前 branch、HEAD、`origin/main`、merge-base、`git status` 和已有用户改动。不得清理、覆盖、回退或顺手提交不属于本阶段的文件。
- [ ] 一个 Milestone 只交付表中一个可观察目标。先写或更新会失败的测试，再做最小 production 实现，再跑 focused gate；阶段出口未满足时不得进入下一阶段，也不得勾选项目。
- [ ] 每个 Milestone 原则上形成一个可独立审查的提交。提交前运行适用 focused tests、architecture tests、`compileall` 和 `git diff --check`；未经用户要求不 push、不创建 PR、不改写历史。
- [ ] 直接原子更新当前唯一 V1 的 inference domain、typed content、lifecycle payload、summary/history projection、fixtures 和全部消费者。不得新增 `InferenceResultV2`、旧新 read union、migration branch、milestone feature flag、双写或临时兼容入口。
- [ ] 继续复用 M3 的 run/session identity、四表、writer/reader、typed-content registry、全文引用、两层 SSE、取消、补写、启动修复和 live cleanup。不得建立第二套 transcript、历史、重试队列、session 状态或第三条 SSE。
- [ ] M6 只运行一个已经存在的 waterbody session。不得遍历 DAG、等待多个上游、创建多个水体 session 或形成 run-level result；这些由 M7 实现。M8 RAG producer、M9 frontend/settings/admin 也不得提前实现。
- [ ] query parser 一次性 `generate()` port 继续禁止 tools。waterbody streaming port 独立新增；不得为复用代码而让 parser 获得 tool 权限，也不得把 provider SDK 类型泄漏进 Domain/Application。
- [ ] station-backed 与 stationless 起点必须分开：前者全空成功时 0 planning/0 final reasoning；后者没有本地观测是正常事实，允许有界 planning，但最终全部不足时必须 0 final reasoning。测试和计数不得把两条路径混写成“所有无证据会话 0 模型调用”。
- [ ] 每轮取证后都通过 M5 原 executor/writer/materializer/bundle builder 形成新 handoff；旧 bundle 不原地修改。Agent 不能传任意 COMID/station/URL、提高 budget、删除 baseline rule 或把 contextual evidence 自报为 allowed-indirect。
- [ ] 最终 gate 由后端在 seal 后按 target ParameterKey × window 同时检查 target 与 required discharge。unsupported/blocked cells 不进入 reasoning batches；天气、设施邻近、历史推理或未物化 rule 不能单独解锁。
- [ ] 后端先固定 identity、完整窗口骨架、fixed cells、evidence allowlist 与 source/unit/aggregation 资格。Agent draft 不得新增/删除窗口、覆盖 fixed result、引用 scope 外事实或伪造 observed/单位/计算。
- [ ] 风险等级只允许 `none / low / medium / high / indeterminate`，由 Agent 按 prompt 判断并给出 `risk_rationale` 与 refs。后端不得使用阈值、最大值、平均值或自定义 0–100 分重算或改写风险；没有证据不能成为 `none`。
- [ ] confidence 表示对水质判断的把握，由 Agent 在 cell、series、window overall、range overall 四层给出理由和 refs；后端只校验 `[0,1]`、scope 和引用。fixed `indeterminate/insufficient_evidence` confidence 固定为 0。
- [ ] 风险、confidence、outcome 和 `complete / partial / indeterminate` availability 是独立事实。availability 的 counts/missing/blocker refs 只由后端计算；`none/low + partial` 不得表述为整个水体安全。
- [ ] 一个水体只有一个 Agent/session。supported cells 按参数、segment 和连续窗口做稳定有界 batches；不得一 cell 一模型调用，也不得把数百 cells 塞进一个无界 response。全部 batches 成功后才执行同模型 overall synthesis。
- [ ] 每个 reasoning batch 和 synthesis 只有一次正常调用与至多一次同模型、同 scope schema repair。repair 不得调用 tools、读取新 evidence、改变 batch plan 或 failover；后端不同意风险/confidence/理由不是 repair 条件。
- [ ] repair 仍失败、模型不可用/超时/content filter 或 assembly invariant 失败时，整个 waterbody session failed。已成功 batches 只留审计，不发布 partial `InferenceResult`，不形成正常 upstream result。
- [ ] 用户所见“推理过程”就是本 session 的真实 provider 可见消息、tool calls/results、evidence、decisions 和 result。文字沿用 `message_started / message_delta / message_finalized`，其他内容沿用 `transcript_item`；不得新增 `reasoning_delta`、第三条 SSE、合成思维摘要或隐藏思维链字段。
- [ ] result item 与 waterbody `session_completed(result_ref + result_summary)` 必须在同一必需提交批次或等价原子边界成立。run snapshot/history 只投影同一持久化结果，不在读取时重新推理、重算风险或复制完整 series。
- [ ] 外部 provider smoke 不是单元测试前置条件。先用 checked-in Responses fixtures 和 fakes 完成确定性覆盖；需凭据、联网或可能计费的 smoke 必须先 preflight，0 requests、缺凭据或网络不满足一律记录 `not run`，不能写成通过或零结果。
- [ ] 实际完成记录必须写清改动路径、命令、环境、pass/fail/skip/not run 数、失败后的修复、剩余风险和 commit SHA。不得复制旧阶段 pass count，不得用 exit code 0、HTTP 200 或 stream 正常关闭代替业务断言。

## M6 完成标准

- [ ] 当前唯一 V1 已包含五级 `RiskLevel`、三态 `TimeWindowResult`、nullable indeterminate source、decimal-string value、四层 confidence/rationale、window composite、range overall、availability 和完整 reference closure；不存在旧结果兼容读取。
- [ ] `WaterbodyAgentTask`、sealed evidence input、final gates、result skeleton 和 evidence allowlist 均由后端构造并有越权正反测试；Agent 不能改变 identity 或 fixed cells。
- [ ] 独立 `WaterbodyModelPort.stream()` 支持可见文字、tool call、structured output、usage、refusal 和 terminal；query parser 仍无 tools，未知/损坏/terminal 后 provider events 被拒绝。
- [ ] station-backed full empty、station-backed partial、stationless upstream/optional、stationless final unsupported、technical blocked 和 cancel 六条主路径均有确定性 fixture，planning/final reasoning/tool 调用计数准确。
- [ ] prompt audit 覆盖五级风险、risk/confidence rationale、absence-of-evidence、series、observed、unit、gate、secret 和四阶段 permission；实际发送 messages/schema/roster 可从 transcript 安全还原。
- [ ] batch planner 对最多 6 参数、15 个当地日期、6h windows、mixed segment 与 DST 稳定排序，满足 cell/batch/model-fit 硬上限且无漏、无重、无 scope 漂移。
- [ ] draft validator 覆盖三态 shape、value/unit/source/aggregation 资格、完整窗口、risk/confidence/rationale、evidence/result refs 和 attribution；每个 exchange 至多一次 repair，无 failover 或 semantic-repair。
- [ ] fake end-to-end 可生成完整 `InferenceResult`，包括每个 parameter-window、series、逐窗口综合、日期范围总体、AnalysisDetail、AttributionSummary、EvidenceChain 和 execution summary；全 fixed 路径不调用 final reasoning/synthesis。
- [ ] 六类 M6 typed content、现有 lifecycle v2 扩展、SessionSummary/run snapshot/history projection 可经真实 writer/reader、inline/reference、补写、PostgreSQL reload/startup repair 和 session SSE 回放；result/completed identity 原子闭包成立。
- [ ] Agent 实时消息三事件身份、顺序、跨 chunk 脱敏、首次回放 + live handoff、Last-Event-ID、取消 incomplete、late audit 和 stream_end 全部沿用 M3；run SSE 不含完整正文/tool/result。
- [ ] OpenAI Responses waterbody adapter 和 production runtime 使用 settings/model catalog/secret boundary，缺 provider/credential/model 安全失败；production OpenAPI 无 direct reasoning/model/tool/test route，frontend 未修改。
- [ ] M1–M5 regression、M6 focused、architecture、OpenAPI、offline fixtures/docs audits、专用 PostgreSQL、`compileall` 和 `git diff --check` 有实际证据；真实 provider 与三平台未运行项如实记录。

## Milestone 0：基线、边界与原子更新清单

### 工作

- [x] 记录 branch、HEAD、`origin/main`、merge-base、tracked/untracked/permission-limited 状态、Python/依赖版本和测试入口；明确保护本分支已有 M6 文档改动。
- [x] 只盘点当前新后端接缝：`domain/inference`、attribution/evidence chain、M5 acquisition/registry/executor、M3 coordinator/writer/reader/registry/SSE、lifecycle/session summary/history、settings、model catalog、provider HTTP、runtime composition 和现有 fakes。
- [x] 形成 M6 影响表，至少覆盖 domain、application reasoning、M5 iterative seam、ports、provider adapter、prompt/policy、settings/runtime、transcript/lifecycle/history/API projection、test harness、scripts 和 docs；每类事实只指定一个 source of truth。
- [x] 扫描 `RiskLevel`、`RiskOrTrendAssessment`、`TimeWindowResult`、`ParameterTimeSeries`、`InferenceResult`、`SessionCompletedPayload`、`SessionSummary`、production transcript registry、OpenAPI fixtures 和 serializer 的全部 production/test/docs 消费者，形成原子更新清单。
- [x] 记录当前 M1–M5 full regression、architecture、PostgreSQL、SSE/history smoke 基线；环境缺失时写 `not run`，不得修改测试或设置去绕过基础设施。
- [x] 增加或确认 architecture guards：Domain/ports 不导入 infrastructure/FastAPI/SQLAlchemy/provider SDK；M6 不依赖归档；parser port 无 tools；production import graph 不含 test harness；只有现有两条 SSE。
- [x] 确认 M6 默认不新增业务表或 Alembic migration。若现有四表/通用内容存储不能承载，必须先提供失败 fixture 和契约变更设计，不得先加表再找用途。

### Tests

- [x] 当前 `backend/tests` 基线无未解释失败；PostgreSQL 用例只使用进程级 `WATERAGENTS_TEST_DB_NAME=wateragents_test`，且与业务库不同。
- [x] architecture tests 锁定 layer dependency、archive isolation、parser/waterbody port 分离、单一 writer/reader、两层 SSE 和 production/test import isolation。
- [x] current inference serialization、production registry/OpenAPI、M5 acquisition 与 M3 session streaming 基线结果被记录。

### Verify

- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests -q`（无法运行的 PostgreSQL/外部项明确拆出并记录）
- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/architecture backend/tests/domain/test_results.py backend/tests/application/evidence_tools/test_acquisition_workflow.py -q`
- [x] `backend/.venv/Scripts/python.exe -B -m compileall -q backend/app backend/scripts`
- [x] `git diff --check`

阶段出口：真实扩展接缝、原子更新面和基线证据可复现；归档完全不在实现输入；本阶段不新增 M6 业务行为。

提交边界：只允许 architecture guard、必要的基线测试修正、影响表和本清单完成记录；不得提前修改 inference model、M5 acquisition、provider adapter 或 lifecycle payload。

### Milestone 0 影响表

| 范围 | 唯一运行时事实源 | M6 原子更新面与当前接缝 |
|---|---|---|
| inference domain | `backend/app/domain/inference/` 的当前 V1 constructors | 原子更新 inference exports、attribution/evidence-chain closure、domain tests 与直接 fixtures；不建立 V2/read union。 |
| application reasoning | 后续唯一 `application/reasoning/RunSingleWaterbodyAgent` 链 | 新增 task、sealed input、sufficiency、batch、validator、assembler；不把 M7 DAG 调度或第二套 session 状态放入 M6。 |
| M5 iterative seam | `EvidenceAcquisitionService` + 原 `EvidenceToolRegistry`/`EvidenceToolExecutor`/writer/materializer/bundle builder | 在同一 executor/writer 上增加可迭代 start/optional/append/seal 接缝；bundle 不原地修改，不复制 registry、budget 或 producer slot。 |
| ports | query parser 继续由 `QueryParserPort`/`QueryParserInputV1` 管理；waterbody 后续只有 `ports/waterbody_model.py` | parser request 继续拒绝 tools；waterbody port 只能依赖 provider-neutral reasoning DTO，不导入 parser DTO、infrastructure 或 provider SDK。 |
| provider adapter | `OpenAIResponsesWaterbodyAdapter` 是后续唯一 waterbody provider adapter；共享底层事实源仅为 `ProviderHttpClient` 与安全 parsing/error primitives | parser adapter 保持一次性、无 tools；waterbody adapter 独立处理 visible text/tool/structured/usage/terminal，无 failover。 |
| prompt/policy | `application/reasoning` 内唯一 immutable typed M6 policy，由 `PromptBuilder` 渲染 | catalog 只作设计输入；runtime 不解析 Markdown，不复制 allowlist/gate/risk/confidence 规则。 |
| settings/runtime | `AppSettings`、`ModelCatalogSnapshot` 与后续唯一 `build_m6_runtime` | 六项 M6 hard limits、model role/router、M3/M4/M5 实例复用；production `main.py` 只装配内部 service，不新增 route。 |
| transcript/lifecycle/history/API | `create_production_registry` + 唯一 `TranscriptWriter`/`TranscriptReader` + 已持久化 typed item | 六类 M6 contents、`SessionCompletedPayload`/`SessionSummary` lightweight projection、history/OpenAPI/两层 SSE 必须同批原子更新；完整 result 只保存一次。 |
| persistence | `schema.py` 的四张业务表、通用 transcript content 与现有 Alembic revision | M6 默认不新增表或 migration；`test_schema_contract.py` 继续锁定四表。若不能承载，先失败 fixture 与契约变更设计。 |
| test harness | 后续 direct `build_m6_test_runtime`/application harness | fake 只替换外部 ports、clock、identity、usage；复用真实 runner/validator/assembler/writer，不注册 production test route。 |
| scripts | 后续 `m6_reasoning_workflow_smoke.py`；现有 `run_sse_smoke.py`/`run_history_smoke.py` | 新 smoke 只走 application/runtime；history 继续服从专用 PostgreSQL 门禁，provider smoke opt-in 且先 preflight。 |
| docs | 本目录四份 M6 契约 + 本 TODO；项目级 architecture/milestones/deferred/frontend 文档 | 只记录当前唯一行为和证据；M1–M9 是一个原子产品演进，不增加 milestone feature flag 或中间产品兼容层。 |

### Milestone 0 原子消费者清单与基线

- inference atom：`RiskLevel`、`RiskOrTrendAssessment`、`TimeWindowResult`、`ParameterTimeSeries`、`InferenceResult` 的 production 定义和 exports 只位于 `backend/app/domain/inference/`、`backend/app/domain/__init__.py`；直接测试消费者为 `test_results.py`、`test_serialization.py`，另有 M5 collection/station architecture fixtures 及 M1/M3/M5/M6 文档引用。Milestone 1 必须一次更新这些消费者和 decimal/source/reference invariants。
- lifecycle/history atom：`SessionCompletedPayload` 与 `SessionSummary` 的直接生产/投影链为 `analysis_runs/models.py`、`coordinator.py`、`runtime.py`、`workflow.py`、`startup_repair.py`、`transcripts/read_models.py`、`reader.py`、`stream_models.py`、persistence memory/PostgreSQL stores、API SSE response 和 M5 transcript reader/fakes。Milestone 9 必须让 result item、completed payload、summary、snapshot、history 和 fixtures 同源闭合。
- registry/serializer/OpenAPI atom：唯一生产注册表是 `application/transcripts/registry.py:create_production_registry`，安全 JSON/large-content/reference parsing 继续由该 registry 和 writer/reader 承担；OpenAPI 来自 `app/main.py` 与实际 API schema。更新面包括 transcript registry、persistence records/store、API schema/route/SSE 和 `test_transcript_registry.py`、`test_analysis_runs.py`、`test_analysis_schemas.py`、documented-protocol/production-isolation tests。
- M5/M3 接力：M5 handoff 当前由 `acquisition.py` 返回 bundle ref、cells、reader、roster、executor、gates 和 stop/block facts；M3 只有 `analysis_runs/writer.py` 的 `TranscriptWriter`、`transcripts/reader.py` 的 `TranscriptReader` 与 run/session 两条 SSE。M6 不创建同义 writer、reader、store、event stream 或结果副本。
- 可复现基线：分支从 `origin/main`/merge-base `e225768ce3fb` 创建，开始时 HEAD 同为该提交；已有 4 个 tracked 项目文档修改和 5 个 untracked `docs/M6` 文件均受保护。若干历史 `.tmp`、pytest cache/temp 目录因 ACL 为 permission-limited，未被宣称已完整扫描或清理。
- 环境：Windows/PowerShell，Python `3.12.14`、pip `26.2.1`、FastAPI `0.141.1`、httpx `0.28.1`、Pydantic `2.13.5`、pytest `9.1.1`、SQLAlchemy `2.0.54`、psycopg `3.3.6`、Alembic `1.20.0`；`uv.lock` SHA-256 为 `5391c11327c04a8802ff679a95bcf30c640f651fae61f524525eec553397763c`。pytest 入口由 `backend/pyproject.toml` 固定为 `testpaths=["tests"]`、`pythonpath=["."]`。

#### Milestone 0 完成记录（2026-10-02）

- 实现：从最新 `origin/main` 创建 `codex/m6-milestone-0`，保护并纳入既有 M6 文档输入；完成当前 backend 接缝、影响面和原子消费者盘点。新增窄 architecture guard，锁定 parser 无 tools、parser/waterbody port 分离、waterbody port 仅可依赖 provider-neutral reasoning DTO，并扩展 production OpenAPI 的 M6 禁止路由；未新增 M6 业务行为、表、migration、provider adapter、runtime 或 lifecycle 字段。
- 验证：原始 full command 得到 `1494 passed, 3 failed, 48 errors`，其中 33 个 error 来自默认 pytest 临时目录 ACL，另 18 个 PostgreSQL cases 因缺少进程级测试库分为 3 failed/15 errors；沙箱外新 basetemp 的非 PostgreSQL 全量回归为 `1527 passed`。最终 M0 focused/architecture 为 `176 passed`，serialization/registry/OpenAPI/history/SSE 合约集为 `148 passed`，新增 guard 定向为 `59 passed`，standalone run SSE smoke 输出 completed/cancelled 均 passed。随后启动 Docker Engine `29.8.1` / Compose `v5.5.1` / PostgreSQL `17.11`，仅以进程级 `WATERAGENTS_TEST_DB_NAME=wateragents_test` 补验：18 个 PostgreSQL persistence cases 为 `18 passed`，`run_history_smoke.py` 的 history、independent-process restart、restart repair 均 passed，`health_smoke.py` 为 healthy；首次仓库内 basetemp 全量运行因目录被测试过程移除得到 `1515 passed, 33 errors`，改用新的系统 basetemp 后重跑为 `1548 passed, 1 warning`。`compileall` 与 `git diff --check` 通过，未修改业务数据库或 `backend/.env`。
- 风险：真实 provider 请求、Ubuntu/macOS/Windows 同提交 CI 为 `not run`。M6 Milestone 1–13 尚未开始，本阶段不声称任何 reasoning runtime 已实现。
- 提交：本地架构护栏提交 `f4bc754`；本完成记录与受保护的 M6 设计文档随紧随其后的本地文档提交保存，均未 push。

## Milestone 1：当前唯一 V1 inference domain

### 工作

- [x] 原子增加 `RiskLevel.NONE`；新增 `AssessmentAvailability=complete|partial|indeterminate`，并更新 exports、serialization、fixtures 和所有枚举穷举消费者。
- [x] 将 `RiskOrTrendAssessment` 收敛为带 `risk_level`、非空 `risk_rationale`、`supporting_refs`、trend/reason codes 的当前唯一结构；Agent 产出要求真实 rationale/refs，backend fixed 产出使用稳定 reason/rationale，不新增同义 V2 或伪装 Agent 文本。
- [x] 收敛 `TimeWindowResult`：representative value 使用 canonical decimal string；indeterminate 的 source 为 null；增加 confidence rationale 并复用本层 refs；三态 shape、unit/source/aggregation/evidence 约束原子更新。
- [x] 收敛 `ParameterTimeSeries`：完整计划 windows、series confidence/rationale/result refs；禁止空 series、重复/缺失/额外/重叠 windows。
- [x] 新增 `CompositeRiskAssessment` 和 `OverallWaterbodyAssessment`，包含 risk/confidence rationales、availability、assessed/total counts、missing/blocker/result refs；counts/availability 是后端事实。
- [x] 扩展 `InferenceResult`：run/session identity、requested parameter order、window composites、range overall、execution summary；保留 AnalysisDetail、AttributionSummary、EvidenceChain 和 generated_at 单一事实。
- [x] 修正 prediction/gap-filling source 验证：quantified/directional 才要求 observed/inferred/predicted；indeterminate source=null 不因 segment mode 被拒绝。
- [x] 完整 reference closure 同时覆盖 point、series、composite、overall、analysis steps、attribution、missing/blocker refs；未知、跨 result 或重复引用安全失败。

### Tests

- [x] 五级风险、三态 result、decimal round-trip、indeterminate source null/fixed confidence 0、Agent-selected indeterminate、strict enum/number 均有正反例。
- [x] planned windows 精确覆盖 `6h/12h/24h`、mixed segments、DST 5/7/23/25 elapsed hours；缺失/重复/额外/乱序/跨 segment point 被拒绝。
- [x] observed 一对一、aggregate inferred、prediction predicted、allowed-indirect 不 quantified 等 source/value 资格保持可验证。
- [x] composite/overall availability counts 和 refs 不一致、`none` 无依据、rationale/ref 缺失、overall confidence 冒充 risk 等错误 shape 被拒绝。
- [x] 旧 fixture/serializer/OpenAPI consumer 已原子更新；production/test 搜索无旧非 nullable source、float result value 或四级风险兼容分支。

### Verify

- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/domain/test_results.py backend/tests/domain/test_serialization.py -q`
- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/architecture/test_domain_contracts.py backend/tests/architecture/test_domain_dependencies.py -q`
- [x] `backend/.venv/Scripts/python.exe -B -m compileall -q backend/app/domain`
- [x] `git diff --check`

阶段出口：当前唯一 V1 能独立表达完整 M6 final result，所有机械不变量由 Domain 验证，风险判断内容仍不由 Domain 计算。

提交边界：仅 inference/attribution/evidence-chain domain、exports、fixtures、直接消费者和对应测试；不得开始模型调用、prompt、planning、transcript 或 runtime。

#### Milestone 1 完成记录（2026-10-02）

- 实现：在 `backend/app/domain/inference/` 原子收敛当前唯一 V1，增加五级风险、三态 availability、严格 decimal-string point、nullable indeterminate source、四层 risk/confidence rationales、window/range assessments、safe model execution summary 与 run/session/requested-parameter identity；`InferenceResult` 校验完整窗口、segment/source/unit、counts/availability 和 evidence/result reference closure。同步 `app.domain` exports、domain fixtures 与 serializer 测试；未新增 V2/兼容读取、application reasoning、provider、transcript、runtime、route、表或 migration。
- 验证：旧模型 red gate 因缺少 `AssessmentAvailability` 在 collection 阶段按预期失败；最终 focused 为 `133 passed`，指定 domain architecture 为 `9 passed`，全部 domain 为 `343 passed`，全部 architecture 为 `107 passed`。Windows/Python 3.12 下以进程级 `WATERAGENTS_TEST_DB_NAME=wateragents_test` 和 PostgreSQL 17.11 执行全量后端为 `1607 passed, 1 warning`；`compileall`、JSON schema string/null 审计、旧 shape 搜索与 `git diff --check` 通过。
- 风险：真实 provider 请求与 Ubuntu/macOS/Windows 同提交 CI 为 `not run`；本阶段没有模型调用面。M2–M13 尚未开始，allowed-indirect 的具体 evidence 资格仍由后续 sealed skeleton/draft validator 依据 M5 facts 校验，M1 只锁定该路径使用的 directional/inferred shape 不携带 quantified value。
- 提交：本地实现提交 `c6b670e`；本完成记录随紧随其后的本地文档提交保存，均未 push。

## Milestone 2：task、sealed gate 与结果骨架

### 工作

- [x] 新增 `application/reasoning` 最小包与 `WaterbodyAgentTask` validator；证明 run/session/role/COMID/context/topology/scope/model selection 一致，取消已接受时不启动。
- [x] 定义 `WaterbodyReasoningStage` 诊断枚举，但不新增 lifecycle 状态或 transport event。
- [x] 将 M5 handoff 转为不可变 `SealedEvidenceInput`：bundle/rule/capability digest、cells、gates、collection facts、allowed refs、budget usage 和 producer facts 完整。
- [x] 最终 gate 只从 sealed M5 facts 计算 `supported / unsupported / blocked`；target 与同窗 discharge 都通过才 supported，contextual-only/未物化 rule 不能解锁。
- [x] 构造唯一 `ResultSkeleton`：run/session/result identity、COMID、mode/timezone/range/segments/resolution、parameter/window 顺序、fixed cells、supported scopes、source/unit/aggregation 资格与 evidence allowlist。
- [x] fixed unsupported cell 使用 `indeterminate/insufficient_evidence/source=null/confidence=0`；partial technical blocked 使用明确技术 reason/blocker refs，不伪装自然 empty。
- [x] skeleton/gate serializer 使用稳定 canonical order/digest；Agent request 只获得所需投影，不获得可修改 identity 或 server-owned counts。

### Tests

- [x] session 不存在、跨 run、非 waterbody、COMID/model/context/topology/scope 不一致、已取消均在任何 tool/model call 前失败。
- [x] target/discharge 的 supported/unsupported/blocked 矩阵、direct/allowed-indirect/contextual、technical incomplete 和 mixed windows 均有 fixture。
- [x] 恶意 draft 预备 fixture 不能新增 parameter/window、覆盖 fixed cell、引用 allowlist 外 evidence 或改 server-owned identity/counts。
- [x] same input 的 skeleton/gates/digest 字节稳定；parameter/window 顺序不受 set/dict/hash seed 影响。
- [x] 全 unsupported 无 technical blocker 可构造完整 fixed skeleton；全 blocked 只形成 failure input，不生成 business-completed result。

### Verify

- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/application/reasoning/test_task_and_skeleton.py backend/tests/application/reasoning/test_sufficiency.py -q`
- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/application/evidence_tools/test_acquisition_workflow.py backend/tests/domain/test_results.py -q`
- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/architecture -q`
- [x] `git diff --check`

阶段出口：在不调用水体模型的情况下，可从合法 M5 handoff 得到封口 gates、完整 skeleton、fixed cells 和 supported scopes；越权输入在边界前失败。

提交边界：task、sealed input、sufficiency、skeleton DTO/validator 及 focused tests；不得实现 Agent planning、batch model calls、final assembler 或 M7 调度。

#### Milestone 2 完成记录（2026-10-02）

- 实现：新增 immutable `WaterbodyAgentTask`/validator、诊断阶段枚举、`SealedEvidenceInput`、target + 同窗 discharge 三态 gate、`ResultSkeleton`/fixed cells/supported scopes 和窄 Agent projection；M5 handoff 同步封口 bundle/rule/capability digest、完整 collection/allowlist、预算及 producer facts，并为没有任何采集路径的 cell 写入明确 `no_applicable_evidence` fact。未实现 model port、planning/batch call、final assembler、transcript/runtime 或 M7 调度。
- 验证：M2 focused `18 passed`，M5 acquisition + result domain `127 passed`，全部 architecture `107 passed`；以进程级 `WATERAGENTS_TEST_DB_NAME=wateragents_test` 和 PostgreSQL 17.11 执行完整 backend 为 `1625 passed, 1 warning`。`compileall` 与 `git diff --check` 通过；真实 provider 请求和三平台同提交 CI 为 `not run`。
- 提交：本地实现提交 `164f84d`；本完成记录随紧随其后的本地文档提交保存，均未 push。M3–M13 尚未开始。

## Milestone 3：provider-neutral waterbody model port

### 工作

- [ ] 新增独立 `ports/waterbody_model.py` 和 application DTO：`WaterbodyModelRequest`、phase、safe messages、tool descriptors、response schema、scope digest、bundle ref、batch id 与 deadline。
- [ ] 定义 strict `WaterbodyModelEvent` 判别联合：visible text delta、completed tool call、structured output、refusal、usage、terminal；禁止 provider SDK object、arbitrary dict 和 secret。
- [ ] 定义 provider-neutral completed response/usage/finish reason 与 typed port errors；区分 unavailable、timeout、content filtered/refusal、invalid protocol、truncated/incomplete 和 cancel。
- [ ] 保持 query parser port/adapter 只支持一次性 generate 且 tools 为空；添加 architecture/API test 防止 M6 权限反向扩散。
- [ ] 提供 deterministic fake streaming port，可脚本化多 message、tool call、structured response、错误、取消、usage 和 terminal 顺序；所有 call/operation identity 可断言。
- [ ] event consumer 拒绝未知 event/item、空 delta、损坏 tool arguments、重复 structured output/terminal、terminal 后输出、provider/model identity 漂移。

### Tests

- [ ] 每种合法 event 和组合顺序可 round-trip；多条 message/tool/structured terminal 的顺序与 identity 保持。
- [ ] 未知/重复/乱序/terminal 后/损坏 JSON/空字段/extra secret 字段被 strict 拒绝，不产生 tool 或 result side effect。
- [ ] fake cancellation 在 event 边界停止，晚到 events 不进入 working context 或 result；usage 缺失允许为 null，但伪造负数/布尔值被拒绝。
- [ ] parser fixtures 继续断言 `tools=[]`，waterbody descriptor 不能传入 parser request。
- [ ] public error 只含安全 provider/model identity、code、stage、retryable 和 error_id，不含 prompt/body/arguments/credential/traceback。

### Verify

- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/ports/test_waterbody_model.py backend/tests/application/reasoning/test_model_protocol.py -q`
- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/infrastructure/test_openai_responses_parser.py backend/tests/architecture/test_port_imports.py -q`
- [ ] `backend/.venv/Scripts/python.exe -B -m compileall -q backend/app/ports backend/app/application/reasoning`
- [ ] `git diff --check`

阶段出口：application 可只依赖 provider-neutral streaming port 驱动可见文字、tools 和 structured output；parser 权限不变；尚无真实 provider adapter。

提交边界：waterbody port、application model protocol、fake 与边界测试；不得修改 OpenAI adapter、M5 executor、transcript payload 或 runtime composition。

## Milestone 4：prompt builder 与 policy audit

### 工作

- [ ] 将 Reasoning Policy Catalog 编码为 typed/immutable policy source 和 `PromptBuilder`；共享 base policy 只定义证据资格、风险/confidence、series、unit/source、引用、安全和禁止事项。
- [ ] 分别构造 evidence planning、reasoning batch、overall synthesis、schema repair 四类 phase prompt；每个 phase 只暴露本阶段允许的 tools、schema、facts 和 actions。
- [ ] planning prompt 明确 station 类型、当前 bundle/gates/missing/blockers、剩余预算和 server-authorized roster；不允许任意 provider request、COMID/station/URL 或自由 evidence ID。
- [ ] reasoning batch prompt 只包含本 batch supported cells、bounded evidence summaries/refs、机械资格和 exact output schema；不允许 tools、scope 外 cells 或 server-owned fields。
- [ ] synthesis prompt 只消费 validated cell results、fixed cells、coverage/blocker facts 和有界引用摘要；要求 window/range risk、四层 confidence/rationale、AnalysisDetail 与 AttributionSummary，不重新读取 raw evidence。
- [ ] repair prompt 只含原可见 response、精简脱敏 mechanical issues、原 schema 和相同 scope；不含 tools、新 evidence、替代模型或开放式“重新分析”。
- [ ] working context 由稳定 refs/digests 和实际发送 messages 构成；不每轮重放整个 transcript，不从 UI preview 反推，不内联无界 records。
- [ ] 建立 executable prompt audit，逐项证明五级风险、`none != no evidence`、risk/confidence rationale、series 全量考虑、observed 资格、unit/tool、gate、secret、hidden reasoning 和 phase permission 规则存在且无冲突。

### Tests

- [ ] 四类 prompt golden/semantic fixtures 使用同一 policy source，输入顺序与 canonical digest 稳定；换 dict 顺序不改变逻辑内容。
- [ ] planning roster 仅包含 registry 当前授权 tools；batch/synthesis/repair 的 tool descriptors 精确为空。
- [ ] prompt 不包含 API key/header/raw provider envelope、归档字段、完整无界 source arrays、scope 外 COMID/station/evidence 或 provider hidden-reasoning instruction。
- [ ] risk policy 明确要求 Agent 说明证据、含义和选级理由；后端没有数值阈值、max/average/0–100 评分 prompt。
- [ ] confidence、availability、outcome、risk 定义不互相替代；fixed facts 与 Agent fields 的所有权可由 fixture 审计。

### Verify

- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/application/reasoning/test_prompt_policy.py backend/tests/application/reasoning/test_working_context.py -q`
- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/application/evidence_tools/test_registry.py backend/tests/application/evidence_policy -q`
- [ ] 运行 M6 prompt/policy 离线 audit，并保存规则数、phase roster/schema digest 与失败样例。
- [ ] `git diff --check`

阶段出口：四阶段 model request 可由已验证类型确定性构造，权限只收窄不扩张；policy 规则来自单一 source 并可执行审计。

提交边界：policy models、PromptBuilder、working-context projection、golden/audit tests；不得执行 tools、调用模型、保存 transcript 或组装 final result。

## Milestone 5：evidence-planning Agent loop

### 工作

- [ ] 将 M5 一次性 `acquire(optional_requests=...)` 收敛为可迭代 application seam：`start`、`execute_optional`、`append_rules`、`seal` 或等价最小接口；内部继续复用原 registry/executor/writer/materializer/bundle builder。
- [ ] 保持旧调用方原子迁移到新 seam，不保留两套 production acquisition、兼容 wrapper 链或重复 bundle builder；M5 自身 contracts/regression 必须继续通过。
- [ ] 实现 station-backed 启动：先执行确定性 local observation/discharge preflight；成功但全部 usable local cells 自然为空时保存真实事实并立即 fixed 收敛，0 planning/0 final reasoning。
- [ ] 实现 stationless 启动：禁止 local observation tool，先读取 direct-upstream producer slot；无论初始 evidence 是否充分，都允许在 hard budget 内由 Agent 决定 optional tools/rules 或 ready。
- [ ] 在 Agent 前执行 `required_collection_attempt=true` 的确定性动作和已有 relation materialization；technical failure/invalid/incomplete 保持 blocker，不改写为 empty。
- [ ] 每轮 planning 只执行至多配置上限的合法 calls，按 provider 顺序逐个 authorization；cancel 后停止余下 calls。append rule 只能追加 validated registry relation，不能删除/替换 baseline。
- [ ] 每轮 tools/rules 后写入新 source/fragment/result/bundle，并重建 gates/working context；旧 bundle/item 不修改，引用闭包成立。
- [ ] 收敛条件只允许 valid `evidence_ready`、无 remaining legal actions、planning turn 上限或 M5 budget 耗尽；无合法 action 的纯文字 response 是 invalid planning response。
- [ ] seal 后生成 `SealedEvidenceInput`；预算耗尽不授权猜测。尚有未完成 conditional-mandatory action 时形成 technical blocker，而非 natural unsupported。
- [ ] 分阶段记录 planning model calls、tool calls、rule proposals、bundle refs、budget/deadline/cancel 和 final reasoning eligibility；不得用总调用数掩盖 stationless planning。

### Tests

- [ ] station-backed full empty 精确 0 planning/0 final reasoning；partial/full usable 进入 planning；provider failure 不走 completed-empty。
- [ ] stationless 无上游、上游 usable、上游 indeterminate、producer unavailable、weather/facility optional 的组合稳定，且从不调用 local observation。
- [ ] Agent 未选择 optional tool 不是 failure/empty；选择依赖天气的 relation 后缺 weather 前置不能通过 gate。
- [ ] 多轮 tool/rule/rebuild 的 bundle refs 单调追加且旧内容不可变；重复 exact request 仍由 M5 single-flight 处理并计 budget。
- [ ] 任意 COMID/station/URL、scope 外 parameter/window/ref、非法 radius/unit/method、baseline rule 删除、contextual 升级均在 executor/writer 前拒绝。
- [ ] max turns、per-turn calls、M5 collection/transform budget、session deadline、cancel race 和 late provider output 均有边界 fixture；不出现无限 loop/retry。
- [ ] 全 unsupported seal 后为 0 reasoning batch/0 synthesis；partial supported 只把 supported cells 交给后续阶段。

### Verify

- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/application/reasoning/test_planning_loop.py backend/tests/application/evidence_tools/test_acquisition_workflow.py -q`
- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/application/evidence_tools -q`
- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/smoke/test_m5_evidence_workflow_smoke.py backend/tests/architecture -q`
- [ ] `git diff --check`

阶段出口：两种 station 起点可经真实 M5 execution seam 完成有界 planning、重建和 seal，并产生准确 gates/计数；尚未执行 reasoning batches。

提交边界：M5 iterative seam、planning loop、rule appender、调用计数和 focused regression；不得实现 M7 upstream producer、final batch/synthesis、RAG producer 或 frontend。

## Milestone 6：确定性 reasoning batch planner

### 工作

- [ ] 定义不可变 `ReasoningBatchPlan`/`ReasoningBatch`，只包含 supported cells；identity 绑定 run/session/result、scope digest、bundle/rule/capability digest 和 batch index。
- [ ] 稳定排序为 requested ParameterKey 顺序、time segment 顺序、连续 final windows；不得按 hash/set/provider output order 形成 batch。
- [ ] 同一 batch 不跨 ParameterKey 或 inference segment；连续 windows 尽量合并，但同时受 `reasoning_batch_max_cells=32`、总 batches=24、序列化输入和预计输出/model limit 约束。
- [ ] model-fit 估算使用 model catalog/adapter capability 与实际 schema/context bytes；不能只用字符数或在 provider 截断后再猜测。
- [ ] 每个 supported cell 恰好出现一次；unsupported/blocked/fixed cells 永不进入 batch。无法在硬上限内完整覆盖时，在任何 batch call 前失败为 `reasoning_plan_too_large`。
- [ ] batches 仍属于同一 waterbody session/model，不创建子 session、同义 agent identity 或 per-cell lifecycle。
- [ ] 生成 synthesis input plan：validated results、fixed cells、coverage/blocker facts 和 bounded summaries/refs；不重复内联全部 raw evidence。

### Tests

- [ ] 最大 6 参数 × 15 当地日期 × 6h windows 的 batch count/order 可重现，全部 supported cells 无漏无重。
- [ ] `6h/12h/24h`、mixed segment、DST spring/fall、partial supported、all unsupported 和单 cell 边界均有 fixture。
- [ ] 32/33 cells、23/24/25 batches、request/output/model limit 临界值正确；超过任一硬上限在模型调用前失败。
- [ ] 更换 dict/hash seed、并发完成顺序或 evidence item 保存顺序不改变 batch identity/order/digest。
- [ ] synthesis plan 不含 unbounded raw records、scope 外 refs 或未验证 draft，fixed cells/availability facts 不可被模型修改。

### Verify

- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/application/reasoning/test_batch_planner.py -q`
- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/domain/test_time_windows.py backend/tests/domain/test_results.py -q`
- [ ] 运行 deterministic serialization/hash-seed audit，记录最大 fixture 的 cells/batches/serialized bytes。
- [ ] `git diff --check`

阶段出口：任意合法 sealed input 都能在调用模型前得到完整、稳定、有界且 model-fit 的 batch/synthesis 计划；超限安全失败。

提交边界：batch/synthesis plan DTO、planner、size estimator 和 focused tests；不得解析模型 response、repair、组装 final result 或修改 SSE。

## Milestone 7：draft validation 与一次 repair

### 工作

- [ ] 定义 strict `ReasoningCellDraft`、`ReasoningBatchDraft`、`OverallSynthesisDraft` 和 repair issue DTO；拒绝 extra/arbitrary fields。
- [ ] cell validator 检查 exact batch/cell identity、三态 shape、value decimal、unit/aggregation/source 资格、risk/confidence/rationales、evidence refs、reason codes 和 quality facts。
- [ ] only allowed-indirect cell 最多 directional；observed 只允许一对一当前水体/窗口 direct record 加登记的可逆展示换算；aggregate/跨 series/upstream/weather/facility/history 为 inferred，prediction 为 predicted。
- [ ] validator 检查所有影响判断的 series refs、evidence allowlist、result/reference closure、scope digest 和 duplicate/missing/extra cells；不评价自然语言结论是否“正确”。
- [ ] synthesis validator 检查每个 final window composite、range overall、series confidence/rationale、AnalysisDetail、AttributionSummary proposal 和 contributing refs；availability/counts 不接受 Agent body 提供。
- [ ] 实现 exchange wrapper：initial response mechanical-invalid 时生成一次精简脱敏 repair request；repair 使用同 provider/model/scope/schema，不含 tools/new evidence，不改变 batch plan。
- [ ] semantic disagreement、风险高低、confidence 大小或 rationale 观点不同不触发 repair。repair 仍 invalid 时返回 typed whole-session failure，不进入 assembler。
- [ ] 保存 initial raw visible response、validation issues、repair request/response 和 validated draft 的 identity/correlation；public error 不泄露原 body。

### Tests

- [ ] missing/duplicate/extra/错序 cell、未知 ref/unit/method/source/risk、非法 bool/NaN/Infinity/float drift、空 rationale 和 scope mismatch 均有失败 fixture。
- [ ] quantified/directional/Agent indeterminate、multi-series 选择/保留不确定、observed/inferred/predicted 和 attribution basis 均有正反例。
- [ ] initial invalid + repair valid 成功；initial invalid + repair invalid、repair protocol error、timeout/content filter 均 whole-session failed。
- [ ] 每个 batch/synthesis 最多一次 repair；repair request 无 tools/new refs；fake call count 精确，无 provider/model failover。
- [ ] 后端“不赞同”合法 `none/low/medium/high/indeterminate` 或 confidence 时不 repair、不改写；无证据 none 因 prompt/refs/mechanical invariant 被拒绝。
- [ ] 前面已 validated batches 在后续失败时只留审计，不被 assembler 接受为 partial final result。

### Verify

- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/application/reasoning/test_draft_validator.py backend/tests/application/reasoning/test_validation_repair.py -q`
- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/domain/test_results.py backend/tests/application/reasoning/test_model_protocol.py -q`
- [ ] `backend/.venv/Scripts/python.exe -B -m compileall -q backend/app/application/reasoning`
- [ ] `git diff --check`

阶段出口：所有 model structured outputs 在进入正式结果前经过可机械证明的严格验证；每个 exchange 只允许一次同 scope repair，失败不产生 partial result。

提交边界：draft/synthesis DTO、validator、repair wrapper、typed errors 与 focused tests；不得接入真实 provider、lifecycle completed、history projection 或 M7。

## Milestone 8：runner、overall synthesis 与结果装配

### 工作

- [ ] 完成 `RunSingleWaterbodyAgent` orchestration：validated task → initial/planning → seal/gates/skeleton → batches → synthesis → assembly；所有操作保持同一 session/model，阶段边界检查 cancellation/deadline。
- [ ] all unsupported 且无 technical blocker 时跳过 batch/synthesis，由后端从 skeleton 生成完整 fixed result；stationless 此前 planning calls 如实写 execution summary。
- [ ] partial supported 只执行 planned batches；全部 batches validated 后才发起一次同模型 synthesis。任一 batch/synthesis/repair failure 终止 whole session。
- [ ] assembler 只接受 skeleton、validated drafts/synthesis 和 fixed facts；生成完整 parameter series、window composites、range overall、AnalysisDetail、AttributionSummary、EvidenceChain 和 execution summary。
- [ ] Agent 决定 supported cell/series/window/range 的 risk/confidence/rationales；后端只装配、校验和固定无证据语义，不进行 max/average/阈值/0–100 分重算。
- [ ] availability、assessed/total counts、missing/blocker refs 由 assembler 从完整计划与结果机械计算；`high + partial`、`none/low + partial` 和全 indeterminate 均保持独立字段。
- [ ] attribution basis/refs、analysis step refs、evidence chain 和 result refs 完整闭包；`hypothesized` 可解释但不能单独解锁 gate 或 quantified result。
- [ ] 实现失败矩阵：自然无证据 completed；部分 technical blocked + supported 可 completed/partial；all required blocked、model failure、invalid after repair、assembly failure 为 failed；cancel 胜出为 cancelled 且无 result。
- [ ] 按 API 契约实现稳定 public error codes、failed stage 与 retryable：model unavailable/timeout/content filtered/response invalid/repair failed、plan too large、budget/deadline exceeded、assembly failed；公开消息不含内部 validation input。
- [ ] execution summary 只含安全 model identity、planning/tool/batch/repair/synthesis counts、usage 和 skipped cells；不含 prompt secret、raw envelope 或 hidden reasoning。

### Tests

- [ ] fake E2E 覆盖 quantified、directional、Agent-selected indeterminate、fixed indeterminate、station-backed full empty、stationless final unsupported 和 mixed outcome。
- [ ] 每个 parameter/window 恰好一 point；series/window/range risk/confidence/rationales 与 refs 完整；全 fixed 路径 0 batch/0 synthesis。
- [ ] risk 完全来自 validated Agent output；fixture 证明后端不因数值、其他参数或 availability 改写等级。
- [ ] `high + partial`、`low/none + partial`、complete、全 indeterminate、partial technical blocker 的 counts/refs/summary 正确。
- [ ] 后续 batch/repair/synthesis/assembly 失败均不返回 partial `InferenceResult`；先前 validated items 仍可审计。
- [ ] 每个稳定 error code 的 stage/retryable/public message 与 transcript detail 分层正确；同一 error_id 可关联日志、lifecycle 和安全 transcript。
- [ ] cancel 在 planning/tool/message/batch/repair/synthesis/assembly 边界胜出时，不启动后续 operation、不发布 result；late output 不改变 working context。
- [ ] 同输入、fixed clock/UUID/fake stream 产生 byte-stable domain result，除明确时间/identity 外无非确定性字段。

### Verify

- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/application/reasoning/test_runner.py backend/tests/application/reasoning/test_result_assembler.py -q`
- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/application/reasoning -q`
- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/domain/test_results.py backend/tests/application/evidence_tools -q`
- [ ] `git diff --check`

阶段出口：使用 fake model/M5 runtime 可在 application 层跑通单 waterbody session 并得到完整、验证过的 domain result 或明确失败/cancel；尚未接入持久化 completion/history 或真实 adapter。

提交边界：runner、synthesis orchestration、assembler、failure mapping 和 focused fake E2E；不得修改 production OpenAPI、实现 M7 DAG 或新增前端。

## Milestone 9：typed transcript、session SSE 与 history

### 工作

- [ ] 注册当前唯一 v1 的六类 M6 typed content：`waterbody_agent_input`、`waterbody_model_request`、`waterbody_agent_message`、`waterbody_model_response`、`reasoning_decision`、`inference_result`；复用 M3 envelope/inline/reference/preview。
- [ ] 为六类内容实现 strict parser、public projection、tags、rag-candidate default、confidence/provenance 提取和 reference collector；unknown/corrupt schema 沿用 M3 安全读取。
- [ ] model request 保存实际发送的安全 messages/schema/tool descriptors 或 content ref 与 canonical digest；model response 保存 provider-neutral visible response/usage/ref，不保存完整 provider envelope/header/URL query/secret。
- [ ] Agent 可见 message 在首个 delta 前 reserve item identity/sequence/created_at；沿用 coordinator 的 `message_started`、`message_delta`、`message_finalized` 和跨 chunk credential redaction。
- [ ] refusal/content filter/timeout/error/cancel/truncated 都以同一 message identity finalize 为 incomplete；cancel 后 late chunks 只进入 M3 history-only audit，不再修改正文、working context 或 result。
- [ ] tool call/result/evidence/decision/repair/synthesis/result 使用既有 `transcript_item`；operation/provider call identity 配对，同名 tool 多次调用不能靠相邻顺序猜测。
- [ ] 落实写入顺序：model request → visible message → visible model response/tool facts → decision/validated draft → final result → session completed。中途失败保留实际已产生内容。
- [ ] 原子扩展 lifecycle v2 `SessionCompletedPayload` 与 `SessionSummary`：waterbody completed 必须同时有 `result_ref/result_summary`；parser completed 为空；failed/cancelled 无 result。同步当前全部 serializer/fixture/consumer，不升版本、不保留兼容 union。
- [ ] result item 与 completed lifecycle item 使用同一必需提交批次或等价原子边界；完成事件不得引用不存在/未确认的 result。持久化失败沿用 run persistence 补写，不新增 session persistence 状态。
- [ ] run snapshot/history/session detail 只投影同一 result 的 `result_id/COMID/range risk/availability/overall confidence/ref`；不内联完整 series/rationale/evidence，不在读取时重算。
- [ ] session SSE 只使用 `transcript_item / message_started / message_delta / message_finalized / stream_end`；首次无 cursor 回放此前全部可见过程并接 live，Last-Event-ID/去重/410/history/关闭语义保持 M3。run SSE 不复制完整 Agent context。

### Tests

- [ ] 六类 content 的 strict parse、canonical serialization、unknown/corrupt、large content、reference closure、public projection 和 secret scan 均有正反例。
- [ ] message 三事件保持同 item identity/sequence/time，跨 chunk secret 被完整脱敏，多消息交错不串正文，finalized 不等待数据库保存。
- [ ] 首次订阅前已有消息/tools/decisions/result 能完整回放后无缝接 live；重连只补未消费，重复 SSE id 不重复正文，stream_end 顺序正确。
- [ ] cancel/error/content filter/truncated message 为 incomplete，late audit 不出现在当前正文/working context/result；已完成 message 不被 run 后续取消改写。
- [ ] result + completed 成功/整批回滚、完成事件单独补写、PostgreSQL reload/startup repair、live/history/auto source 和 persistence warning 均保持 reference closure。
- [ ] parser session completed payload 仍无 result；waterbody completed 缺任一 ref/summary、failed/cancelled 带 result 或 summary/ref identity 不一致均被拒绝。
- [ ] run snapshot 只有轻量 summary，session transcript 有完整真实上下文；production registry/OpenAPI 不出现新 transport event 或第三条 SSE。

### Verify

- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/application/reasoning/test_transcript_store.py backend/tests/application/test_transcript_registry.py -q`
- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/application/test_streaming_redaction.py backend/tests/smoke/test_run_sse_smoke.py -q`
- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/api/test_analysis_history.py backend/tests/api/test_analysis_events.py backend/tests/persistence -q`（PostgreSQL 仅按专用测试库门禁执行）
- [ ] `backend/.venv/Scripts/python.exe -B -m compileall -q backend/app`
- [ ] `git diff --check`

阶段出口：完整 M6 会话内容、最终结果和 completion/summary 可经唯一 writer/reader、现有 session SSE 与历史路径一致交付；没有新 route、event stream 或事实副本。

提交边界：typed models/registry/store、streaming integration、lifecycle/summary/history projection、persistence/API fixtures 和 focused tests；不得接入真实 provider 或 M7 workflow。

## Milestone 10：OpenAI Responses waterbody adapter

### 工作

- [ ] 新增 `OpenAIResponsesWaterbodyAdapter`，只实现 `WaterbodyModelPort`；复用安全 HTTP/config/credential/error primitives，但不复用 parser 的无-tool业务实现。
- [ ] request 由 run 固化的 OpenAI waterbody model、system/messages、function tool descriptors、strict response schema 和 deadline 生成；`store=false`，endpoint/key 只来自 composition。
- [ ] 只允许本次 server roster 的 function tools；拒绝 provider built-in web/file/MCP/computer/custom tools 和模型自报的新 tool/schema。
- [ ] 按 provider event 顺序发出 visible text deltas、completed function calls、structured output、refusal、usage 和 terminal；保留安全 provider response/call identity。
- [ ] unknown item/content/event、重复/缺失 terminal、terminal 后 output、损坏/空 arguments、response id/model 漂移和 output/schema 不匹配映射为 invalid port response。
- [ ] completed/incomplete/failed/cancelled、refusal、content filter、timeout、network/protocol error 映射到 M6 typed errors；不 silent fallback、不 provider/model failover。
- [ ] provider reasoning/summary/encrypted reasoning 事件不成为业务 evidence、Agent message 或结果；只处理 provider 实际交给应用的可见 message/tool/structured output。
- [ ] checked-in contract fixtures 覆盖多 message、多 tool、structured terminal、usage 缺失、fragmented SSE/UTF-8、unknown/malformed 和所有 terminal outcomes；fixture 不含真实 secret。

### Tests

- [ ] planning request 能发送授权 tools，batch/synthesis/repair 精确发送空 tools；actual request schema/roster/model 与 application DTO 一致。
- [ ] visible text delta 顺序、function call arguments、structured output、usage/response id 被 provider-neutral 化且无 envelope 泄漏。
- [ ] provider reasoning events 即使出现也不进入 `waterbody_agent_message`、working context、decision 或 result；隐藏内容没有 API/transcript 字段。
- [ ] malformed JSON/SSE、未知 event/item、重复 terminal、terminal 后 output、content filter/refusal/timeout/network/cancel 均有确定性失败 fixture。
- [ ] Authorization/API key/base URL query/prompt/tool args/provider raw body 不出现在 public error、普通日志或不应公开的 projection。
- [ ] parser adapter 全部既有 fixtures 继续通过，且仍拒绝 tool call item；两个 adapter 的权限不会因共享 helper 混合。

### Verify

- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/infrastructure/test_openai_responses_waterbody.py -q`
- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/infrastructure/test_openai_responses_parser.py backend/tests/infrastructure/test_provider_http.py -q`
- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/observability backend/tests/architecture -q`
- [ ] `git diff --check`

阶段出口：OpenAI Responses 可通过独立、严格、可审计的 waterbody streaming adapter 驱动 M6 port；checked-in fixtures 完整，尚不要求真实 provider 请求。

提交边界：waterbody adapter、必要的共享低层安全 helper、official contract fixtures 和 tests；不得改 parser 权限、注册 production route 或添加 failover。

## Milestone 11：settings 与 production/test composition

### 工作

- [ ] 在 `AppSettings` 与 `.env.example` 原子增加六项 M6 limits：planning turns=6、tool calls/turn=4、batch cells=32、batches=24、model timeout=90s、session deadline=600s；strict int/float、bool/NaN/Infinity/extra env 继续 fail startup。
- [ ] repair hard limit 固定为一次，不提供普通 settings 提高；M5 tool budgets 继续独立且更严格者优先，M6 不复制/放宽。
- [ ] 构造 `M6Runtime`：runner、model router、prompt builder、planning loop、sufficiency、batch planner、validator、assembler、transcript store 和 limits；依赖只来自 M3/M4/M5 当前 runtime/ports。
- [ ] router 按 run 固化 `ModelSelection` 选择 role-compatible adapter；selected provider 无 adapter、credential 缺失、model 不在 catalog 或 role 不匹配时，在 session 新操作前安全失败。
- [ ] production lifespan 只装配内部 M6 service，不注册 direct reasoning/model/tool/result/test route，不改变现有十条 run-root API，也不提前让 M4 workflow 创建水体 session。
- [ ] 默认建立 direct application test harness / `build_m6_test_runtime`，用 fake model/M5 ports/fixed clock/UUID/usage 调真实 runner/validator/assembler/writer；不默认增加 test HTTP route。
- [ ] 若自动化确需独立 test app，必须单独 composition、production import graph 不可达、无环境开关启用，并复用同一 production chain；先在清单完成记录中说明必要性。
- [ ] 增加 opt-in `m6_provider_smoke.py`：只验证最小合法 model contract，先检查 credential/model/network/明确授权，输出安全 request/response/usage 计数；默认不运行、不打印 prompt/secret。
- [ ] settings metadata/health 只公开安全 limits 和 adapter availability；不回显 key、base URL credential、prompt 或 provider body。

### Tests

- [ ] 六项默认/边界/非法 env 和最坏模型调用上界 `planning + 2*batches + 2` 有测试；deadline 到达后不启动新 operation。
- [ ] production runtime 的组件 identity、dependency direction、M5/M3 单一实例复用和 shutdown/cancel cleanup 正确；无第二 writer/reader/executor。
- [ ] selected OpenAI waterbody model 正常路由；unsupported provider、缺 key、role/model mismatch 安全失败且错误脱敏，无 fallback。
- [ ] production OpenAPI 精确不含 `/reasoning`、`/models/complete`、`/tools`、`/evidence/collect`、`/__test__` 或等价 path。
- [ ] test harness 与 production composition 对同 fixture 产生相同 contract semantics；fake 只替换外部 ports/clock/identity，不跳过 scope/policy/writer。
- [ ] production import test modules 的 architecture guard、frontend 无 M6 direct route consumer 和 parser/waterbody adapter isolation 通过。

### Verify

- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/infrastructure/test_m6_runtime.py backend/tests/api/test_health.py -q`
- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/architecture backend/tests/api/test_analysis_runs.py -q`
- [ ] 导出 production OpenAPI 并断言路由集合未增加 direct reasoning/tool/test surface。
- [ ] `backend/.venv/Scripts/python.exe -B -m compileall -q backend/app backend/scripts`
- [ ] `git diff --check`

阶段出口：production 可内部构造完整 M6 runner，test harness 可复用同一真实链；limits/secret/model 权限受 settings/catalog 控制，公开 API 未扩张。

提交边界：settings/env example、runtime/router/lifespan、direct test composition、optional smoke script、OpenAPI/architecture tests；不得实现 M7 调度或 frontend。

## Milestone 12：单水体端到端与 PostgreSQL/SSE 验收

### 工作

- [ ] 建立固定 fake model/providers 的完整单水体 smoke，直接准备合法 run/waterbody session/context/topology/M5 scope 后调用 production `RunSingleWaterbodyAgent`；不得新增生产 HTTP invoke route。
- [ ] 覆盖 station-backed usable/partial/full-empty/failure、stationless upstream/optional/final-unsupported、逐窗 discharge、multi-series、quantified/directional/indeterminate 和 window/range overall。
- [ ] 覆盖多 batch、initial invalid + repair success、repair failed、model unavailable/timeout/content filter、plan too large、budget/deadline、cancel 各阶段和 persistence failure。
- [ ] 通过真实 coordinator/writer/reader 验证六类 contents、message 增量、tool/decision/result 顺序、result/completed 原子 identity、SessionSummary/run snapshot/history projection。
- [ ] 使用现有 session SSE HTTP path 验证首次回放 + live handoff、Last-Event-ID、跨网络分块、去重、message finalized、stream_end、410/history 和关闭连接不取消执行。
- [ ] 在专用 PostgreSQL `wateragents_test` 验证 inline/reference、完整内容、补写、history reload、独立进程重启和 startup repair；只用进程级测试库变量，不写入 `backend/.env`。
- [ ] 验证 result summary/headline 在 result 已发布后可见，run SSE 从不包含完整 Agent正文、tool payload、rationale、series 或 evidence。
- [ ] 验证 failed/cancelled 无 result/upstream success，已产生 transcript 按可见性保存；successful batches 不因后续失败成为 partial final result。

### Tests

- [ ] fake smoke 对每条主路径断言业务字段、调用计数、operation/session/result identity、refs/availability/risk/confidence，而不只断言 exit code。
- [ ] session SSE 的 messages/tools/decisions/result 与 GET transcript/history 同一事实、同一顺序和正文；重连不重复、不丢失。
- [ ] result/completed 整批成功/失败/补写、数据库暂时失败、retry exhausted/manual recovery 和 live cleanup 门禁保持 M3 语义。
- [ ] current/history/auto、large-content read、unknown/corrupt schema、公共 refs 和 persistence warning 没有 M6 特例分叉。
- [ ] logs/API/SSE/history 全出口不含 credentials、provider envelope、raw URL query、隐藏 reasoning、scope 外 refs 或未验证 structured output。
- [ ] M1–M5 focused regression 和 production OpenAPI/architecture 在端到端接入后仍通过。

### Verify

- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/smoke/test_m6_reasoning_workflow_smoke.py backend/tests/application/reasoning -q`
- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/smoke/test_run_sse_smoke.py backend/tests/api/test_analysis_history.py -q`
- [ ] 按 [Docker PostgreSQL 快速指南](../docker_postgresql_quickstart.md)设置进程级 `WATERAGENTS_TEST_DB_NAME=wateragents_test` 后运行 `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/persistence -q`。
- [ ] `backend/.venv/Scripts/python.exe -B backend/scripts/m6_reasoning_workflow_smoke.py`
- [ ] `backend/.venv/Scripts/python.exe -B backend/scripts/run_sse_smoke.py`
- [ ] `backend/.venv/Scripts/python.exe -B backend/scripts/run_history_smoke.py`（满足专用 PostgreSQL 安全门禁时）
- [ ] `git diff --check`

阶段出口：在不依赖 M7 DAG 的情况下，单 waterbody session 的真实 application、persistence、session SSE 和 history 链路均由固定 fake 可重复验收；外部 provider 仍可为 `not run`。

提交边界：E2E harness/smoke、PostgreSQL/SSE/history/cancel/persistence tests 和必要的集成修复；发现功能缺口必须回到所属 Milestone 修复，不把大功能补丁藏在 smoke 提交。

## Milestone 13：集成审计与 M7–M9 接力

### 工作

- [ ] 运行完整 M1–M6 regression：domain、application、ports、infrastructure、API、persistence、observability、architecture、smoke、serialization、OpenAPI 和 docs contract。
- [ ] 审计 `GLOBAL-C01`–`GLOBAL-C11`、`M6-C01`–`M6-C19`、`M6-A01`–`M6-A10`，逐项填写实现位置、focused test、集成证据、实际状态与剩余风险。
- [ ] 搜索并清除旧 inference fields、四级风险假设、non-null indeterminate source、重复 policy/allowlist、orphan adapters/methods、M6 feature flags、兼容 read union、production test route import 和无 caller 代码；不清理无关历史代码。
- [ ] 审计 phase/model/tool/result 调用计数、budget/deadline、repair 上限、no failover、secret redaction、scope authorization、reference closure 和 deterministic order；不能只审计 happy path。
- [ ] 同步 M6 设计/实施/Policy/API/TODO、架构骨架、总 Milestones、延后契约和前端接入边界；只勾选有实际证据的任务，不把文档存在写成实现完成。
- [ ] 分 provider 执行可选真实 waterbody model contract smoke；在请求前记录明确授权、credential/model/network preflight，之后记录实际 requests、visible events、tool calls、usage 和安全摘要。未执行保持 `not run`。
- [ ] 在专用 PostgreSQL 执行完整 persistence/history/restart/startup-repair；记录 Docker/PostgreSQL/Python 版本、pass/fail/skip/not run。业务数据库和 `backend/.env` 不得被测试改写。
- [ ] 写明 M7 接力：ready DAG node 创建/提供 session 与 direct upstream producer，调用同一 runner，处理 dependency outcomes 和 run-level result；不得复制 M6 runner 或改其 session 边界。
- [ ] 写明 M8 接力：实现 transcript search producer，并继续把历史 evidence 保持 contextual；不得凭历史结论解锁 current gate。
- [ ] 写明 M9 接力：消费 result summary/ref 和同一 session transcript/SSE；展示真实上下文、risk/confidence/availability，不新增 direct prompt/model/tool route 或第三条流。

### Tests

- [ ] fixed fake E2E 覆盖全部 station/outcome/failure/cancel/repair/budget/persistence 主路径，调用数和终态均有业务断言。
- [ ] M6 policy、batch、validator、assembler、六类 typed content、lifecycle/history、provider fixtures、settings/runtime 和 SSE 均有独立可执行 gate。
- [ ] secrets、provider envelope、hidden reasoning、raw arguments/URL、内部异常、unbounded arrays 和 test controls 的 logs/API/SSE/history 全出口扫描通过。
- [ ] current V1 producer/consumer/fixture/OpenAPI 同步，无 V2/read union/feature flag；production OpenAPI 无 direct reasoning/tool/test route。
- [ ] contract ID 集合与覆盖矩阵双向一致；Markdown links、JSON examples、code fences、trailing whitespace、未勾选/有证据勾选状态和 `git diff --check` 通过。
- [ ] Ubuntu/macOS/Windows 同一提交 CI、真实 provider 与专用 PostgreSQL 证据分别记录；一类通过不能替代另一类。

### Verify

- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests -q`
- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/persistence -q`（仅专用 `wateragents_test`；未配置则 `not run`）
- [ ] `backend/.venv/Scripts/python.exe -B -m compileall -q backend/app backend/scripts`
- [ ] `backend/.venv/Scripts/python.exe -B backend/scripts/m6_reasoning_workflow_smoke.py`
- [ ] `backend/.venv/Scripts/python.exe -B backend/scripts/run_sse_smoke.py`
- [ ] `backend/.venv/Scripts/python.exe -B backend/scripts/run_history_smoke.py`（满足专用 PostgreSQL安全门禁时）
- [ ] 运行 M6 prompt/policy、provider fixture、typed registry、OpenAPI、contract matrix、docs/link/JSON 离线 audits。
- [ ] `git diff --check`

阶段出口：M6 内部切片的代码、测试、协议和接力证据完整；没有把未运行的 provider/PostgreSQL/三平台项写成通过，也没有实现 M7–M9 或开始正式前端联调。

提交边界：只包含集成修复、审计、smoke、文档同步和完成记录；发现实质功能缺口时必须回到对应 Milestone 修复并重跑其 focused gate，不能把大功能补丁藏在最终审计提交。

## 契约覆盖矩阵（实施时填写）

| 契约 | 状态 | 计划实现位置 | 重点测试 | 证据与剩余风险 |
|---|---|---|---|---|
| M6-C01 / M6-C12 | pending | `application/reasoning/model_models.py`；`ports/waterbody_model.py`；provider adapter | model protocol、identity/correlation、adapter fixtures | 尚未实现；parser 权限隔离和 provider-neutral stream 待验证。 |
| M6-C02 / M6-C09 / M6-C15 / M6-A06 | pending | planning loop；M5 iterative acquisition seam；sufficiency | station-backed/stationless、planning/final call counts、target+discharge gates | 尚未实现；必须区分 stationless planning 与 final reasoning 0 calls。 |
| M6-C03 / M6-C04 / M6-C06 / M6-C07 / M6-A01 / M6-A03 | pending | inference domain；draft validator；assembler | 三态、windows、series、unit/source/aggregation | 尚未实现；当前 V1 domain 仍需原子收敛。 |
| M6-C05 / M6-C14 / M6-C18 / M6-A02 / M6-A05 | pending | prompt/policy；domain assessments；synthesis；assembler；session context | risk/confidence rationales、window/range overall、availability、真实 session SSE | 尚未实现；风险由 Agent 判断，后端不得重算。 |
| M6-C08 | pending | synthesis/assembler；attribution domain/validator | basis、factor refs、EvidenceChain closure | 尚未实现；hypothesized 不能解锁 gate。 |
| M6-C10 / M6-C11 / M6-C13 / M6-A04 / M6-A09 | pending | transcript registry/store；coordinator；lifecycle/summary/history projections | typed contents、result/completed atomicity、SSE replay、PostgreSQL reload | 尚未实现；只复用现有事件和两层 SSE。 |
| M6-C16 / M6-C17 / M6-A07 | pending | skeleton、batch planner、validator、assembler | identity/allowlist、batch completeness、恶意 draft | 尚未实现；server-owned facts 不交给模型修改。 |
| M6-C19 / M6-A08 / M6-A10 | pending | repair wrapper、runner failure matrix、settings/runtime | one repair、whole-session failure、budgets/deadline/no failover | 尚未实现；无 partial final result。 |
| GLOBAL-C01–C11 | pending | 当前 M1–M6 backend、docs、CI 与运行环境 | full regression、architecture、PostgreSQL、三平台、secret scan | 各类证据必须分开记录，不因本机通过标 external verified。 |

状态只允许：`pending`、`partial`、`本机已验证`、`外部已验证`。不能只因代码、fixture 或文档存在填写已验证；本机、PostgreSQL、真实 provider 和三平台 CI 证据分开记录。

## 实际完成记录格式

每个 Milestone 完成时，在该节“提交边界”后追加记录；不要新建独立 completion Markdown：

```text
#### Milestone N 完成记录（YYYY-MM-DD）

- 实现：关键 production 契约、改动路径与明确未实现边界。
- 验证：实际命令、环境和 pass/fail/skip/not run 结果；失败后的修复与重跑结果。
- 风险：外部未运行项、partial 项和后续接力限制。
- 提交：commit SHA；如未提交则写未提交，不能填计划 SHA。
```

不得把计划命令写成已执行结果，不得复制 M5 或其他阶段的 pass count，不得把 `skipped`、0 requests、HTTP 200、收到 `stream_end`、进程启动或 exit code 0 单独当作业务通过证据。

## M7 / M8 / M9 接力出口

M6 完成后只交付以下稳定能力：

- M7：`RunSingleWaterbodyAgent`、`WaterbodyAgentTask`、result/upstream-safe projection 和 dependency outcome；M7 自己按 active DAG 创建/调度 session、接入真实 direct-upstream producer、等待全部直接依赖并形成 run-level result。
- M8：现有 transcript typed contents、result refs 和 `TranscriptEvidencePort` slot；M8 自己建立 index/search，并继续检查 completed/persistence/memory-mode/safe projection，历史 evidence 保持 contextual。
- M9：轻量 `result_ref/result_summary`、完整 `InferenceResult` 引用和现有 session transcript/SSE；M9 展示分时段、window/range risk、confidence、availability、rationales 与真实 Agent context，不调用 prompt/model/tool test surface。

M6 不因上述阶段尚未实现而增加 fake production producer、临时 unsupported product response、feature flag、V2、兼容层、第三条 SSE 或前端专用推理摘要。后续阶段应直接扩展同一最终系统和当前唯一 V1。
