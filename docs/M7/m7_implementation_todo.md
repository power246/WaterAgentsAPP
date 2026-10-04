# M7 Topology-Driven Multi-Agent Workflow Implementation TODO

更新日期：2026-10-04

> 当前状态：Milestone 0–9 已完成 Windows 本机实现、专用 PostgreSQL 验证与离线契约审计，M7 已具备进入正式验收的分支证据；真实 provider 请求和 Ubuntu/macOS/Windows 同提交 CI 仍分别为 `not run` / `partial`。M7 是 M1–M9 同一最终系统中的内部实施切片，不形成独立发布版本；M8 transcript RAG 与 M9 frontend/settings/admin 尚未实现，也未开始正式前端联调。

本清单执行以下已确认文档：

- [M7 设计决策 D01–D20](m7_topology_driven_multi_agent_design.md)
- [M7 实施设计](m7_topology_driven_multi_agent_implementation_design.md)
- [M7 Workflow Policy Catalog](m7_workflow_policy_catalog.md)
- [M7 API / 读取契约](m7_topology_driven_multi_agent_api.md)
- [跨 Milestone M7 契约](../deferred_milestone_contracts.md#9-m7topology-driven-multi-agent-workflow)
- [M6 Implementation TODO 的 M7 接力出口](../M6/m6_implementation_todo.md#m7--m8--m9-接力出口)
- [Docker PostgreSQL 快速指南](../docker_postgresql_quickstart.md)

若本清单与上述设计冲突，coding agent 不得自行选择实现成本更低的一项，也不得用兼容层同时保留两套行为。应先暂停当前阶段，在同一变更中把当前唯一 V1 设计、契约与清单修正一致，再继续实现。内部函数拆分、文件粒度和 fixture 组织可由实现者决定，但不能改变已冻结的 workflow 所有权、ready 条件、session 创建时机、dependency 分类、station 启动门禁、并发/取消、汇总 Agent、持久化、SSE 或 M8/M9 接力语义。

## 阶段与设计导航

| 实施阶段 | 主要设计依据 | 阶段产物 |
|---|---|---|
| [Milestone 0：基线、真实接缝与原子更新清单](#milestone-0基线真实接缝与原子更新清单) | [实施设计 §1–2/§17.6](m7_topology_driven_multi_agent_implementation_design.md#1-当前接入点) | 可复现基线、production caller/consumer 影响表、架构红线 |
| [Milestone 1：当前唯一 V1 类型与投影骨架](#milestone-1当前唯一-v1-类型与投影骨架) | [实施设计 §3/§10–12](m7_topology_driven_multi_agent_implementation_design.md#3-核心类型)；[API §2–8](m7_topology_driven_multi_agent_api.md#2-agentrole-与-session-summary) | dependency/run/summary 类型、role、phase、discriminated summary、稳定引用 |
| [Milestone 2：run-local outcome store 与真实上游证据端口](#milestone-2run-local-outcome-store-与真实上游证据端口) | [实施设计 §6–7](m7_topology_driven_multi_agent_implementation_design.md#6-m5-acquisition-接力修订)；[Policy §2–6](m7_workflow_policy_catalog.md#2-dependency-status) | direct-upstream 授权、manifest、usable-only records、同 run 引用闭包 |
| [Milestone 3：水体启动门禁与零模型正式结果](#milestone-3水体启动门禁与零模型正式结果) | [设计 D03/D08](m7_topology_driven_multi_agent_design.md#d03后端按-station-与-usable-upstream-决定启动路径)；[实施设计 §8](m7_topology_driven_multi_agent_implementation_design.md#8-modewindow-与-gate) | station 分流、本地 preflight、0-model insufficient、source role prompt |
| [Milestone 4：依赖感知 scheduler 与有界并行](#milestone-4依赖感知-scheduler-与有界并行) | [设计 D02/D09/D10](m7_topology_driven_multi_agent_design.md#d02ready-由直接依赖收敛决定session-只在准备执行时创建)；[实施设计 §5](m7_topology_driven_multi_agent_implementation_design.md#5-scheduler-算法) | stable ready queue、exact-once claim、全局并发上限、topology-only 范围 |
| [Milestone 5：同一 production workflow 执行全部水体](#milestone-5同一-production-workflow-执行全部水体) | [设计 D01/D11/D16/D19](m7_topology_driven_multi_agent_design.md#d01同一-production-workflow-从-topology-一直运行到-run-result)；[实施设计 §4/§14](m7_topology_driven_multi_agent_implementation_design.md#4-workflow-组合) | `QueryTopologyWorkflow` 延伸、M6 runner 接入、target/partial/failure 收敛 |
| [Milestone 6：独立 run summarizer Agent](#milestone-6独立-run-summarizer-agent) | [设计 D12–D15](m7_topology_driven_multi_agent_design.md#d12增加一个独立-run_summarizer-session)；[实施设计 §9](m7_topology_driven_multi_agent_implementation_design.md#9-summary-runner) | 同模型汇总、target full + cards、严格验证/一次 repair、特殊值 refs |
| [Milestone 7：typed transcript、持久化、SSE 与 history](#milestone-7typed-transcript持久化sse-与-history) | [设计 D18](m7_topology_driven_multi_agent_design.md#d18只扩展既有两层-sse-与历史读取)；[实施设计 §10–12/§15](m7_topology_driven_multi_agent_implementation_design.md#10-agentrole-与-session-result-扩展) | 七类内容、原子结果闭包、snapshot/detail/history、既有两层 SSE |
| [Milestone 8：production composition、取消与竞争收敛](#milestone-8production-composition取消与竞争收敛) | [设计 D17](m7_topology_driven_multi_agent_design.md#d17取消仍只有-run-cancel先封调度再通知-sessions)；[实施设计 §13/§16](m7_topology_driven_multi_agent_implementation_design.md#13-cancellation-与并发竞争) | M7 runtime、单 caller、cancel/quiesced/late audit、配置边界 |
| [Milestone 9：端到端验收、契约审计与 M8/M9 接力](#milestone-9端到端验收契约审计与-m8m9-接力) | [实施设计 §17–18](m7_topology_driven_multi_agent_implementation_design.md#17-测试策略) | chain/diamond/大图 E2E、PostgreSQL/SSE/history、全回归、接力证据 |

## Coding agent 持续执行原则（非完成项）

以下条目是后续维护仍须遵守的规范，不作为完成状态复选框：

- 只以当前 `backend/`、`docs/M1`–`docs/M7` 和项目级当前契约为实现输入。`backend_bk/`、`frontend_bk/`、`docs/backend_refactor_contract.md` 是最终丢弃的归档；不得读取、导入、复制、适配、枚举、运行或把其中行为当测试 oracle。
- 开始每个 Milestone 前记录 branch、HEAD、`origin/main`、merge-base、`git status`、已有用户改动和 permission-limited 路径。不得清理、覆盖、回退或顺手提交不属于本阶段的文件。
- 一个 Milestone 只交付表中一个可观察目标。先写或更新会失败的测试，再做最小 production 实现，再跑 focused gate；阶段出口未满足时不得进入下一阶段，也不得勾选项目。
- 每个 Milestone 原则上形成一个可独立审查的提交。提交前运行适用 focused tests、architecture tests、`compileall` 和 `git diff --check`；未经用户要求不 push、不创建 PR、不改写历史。
- 直接原子更新当前唯一 V1 的 role、typed content、lifecycle、snapshot、history、OpenAPI、fixtures 与全部消费者。不得新增 V2/read union、milestone feature flag、双写、临时兼容入口或第二套 run/session 状态。
- 继续复用 M3 的 run/session identity、四表、writer/reader、typed-content registry、全文引用、两层 SSE、取消、补写、启动修复和 live cleanup；继续复用 M4 的 topology/范围边界、M5 的 evidence executor、M6 的 waterbody runner/model adapter。不得复制这些能力。
- M7 必须扩展 coordinator 已拥有的 `QueryTopologyWorkflow`。不得另起 background workflow、在 route 内直接跑 DAG、让 frontend 编排 Agent，或保留 topology-only 与 M7 两条 production 路径。
- node 只有在全部 participating direct-upstream outcomes 终结后才 ready，并只 claim/执行一次。创建水体 session 的时间是“准备执行 Agent loop 或生成确定性正式结果”，不是 DAG 建好时。
- dependency 状态只有 `usable / insufficient / invalid / cancelled`。正常全 `indeterminate` 是 `insufficient`；技术失败是 `invalid`。两者都不产生 `UpstreamResultRecord`，也不向下游暴露 evidence、result 或错误正文。
- 汇流节点必须等待全部直接上游。只要至少一支 usable，其他 invalid/insufficient 不阻止本地 preflight 和后续判断；不得把“某个上游失败”误写成“整个下游无条件继续”或“整个下游无条件停止”。
- station-backed 永远先查本地目标参数和 discharge 等允许的本地站点证据。目标参数缺失时，本地 discharge/其他允许的间接证据加 usable upstream 可进入 planning；本地同参数读数是当前水体直接观测，上游值只是上游水体推理结果。
- stationless 以 usable upstream 为强制起点。全部直接上游 insufficient/invalid 时创建完整 `indeterminate / insufficient_evidence` 结果和水体 session，且必须是 0 planning、0 final reasoning、0 optional tool；该结果只传不足状态，不作为证据继续传播。
- 后端不为上游结果发明评分、权重、冲突裁决或内容改写。跨水体推理和普通本地间接证据默认只能支持方向、趋势或风险；只有 registry 中可执行的确定性公式/工具可以产出具体推断数值，Agent 不能自造公式或系数。
- 同层无依赖节点可并行，但全局并发必须受 `WATERBODY_WORKFLOW_MAX_CONCURRENCY` 约束并有竞争测试。整次运行只受 M4 已冻结的最大上游距离、hop 和 node 范围限制；不得新增 run-wide token/model-call quota。M5/M6 每 session 的 budget、repair、timeout/deadline 继续有效。
- target 水体的 M6 结果是主科学结论。非 target 技术失败但 target 有效时 run 仍可 completed 并标记 partial coverage；target 技术失败才使 run failed；target 确定性 indeterminate 是正常 completed 业务结果。
- summarizer 是独立 `agent_role=run_summarizer`、`target_comid=null` session，只在全部 active 水体收敛后创建。它复用本 run 精确 `waterbody_model`，没有第三模型设置、模型 fallback、tools 或隐藏总结路径。
- summary 输入恰好包含一份 target 完整安全业务投影和每个其他 active 水体各一份紧凑报告卡。数字 highlight 必须有 value、unit、window 和稳定 source refs；Agent 不生成 URL，M9 才可把 refs 渲染成可点击内部链接。
- summary 只有一次主调用和至多一次同模型、同 scope 的 schema repair。失败时 session failed、后端保留详细日志、前端只见安全失败信息；不得生成固定模板、字符串拼接 fallback 或复用旧 summary。target 有效时 run 仍 completed，各水体结果仍可读。
- 用户仍只调用既有 run cancel。只有 run cancellation token 已置位才可把 summary 标成 cancelled；provider 自身的 cancelled terminal 按 summary technical failure 处理。后端先封住 dequeue/new session/tool/model，再通知所有已创建未终态 sessions；已完成 session 不改写。无法强杀的晚到内容只走原审计路径，不进入当前正文、frontend result、summary 或下游传播。
- 只使用现有 run SSE 与 session SSE；summarizer 走 session SSE。不得新增 route、第三条 SSE、per-session cancel HTTP、direct scheduler/model/tool/test HTTP surface。默认不得新增业务表或 migration；确有必要时先用失败 fixture 证明现有四表无法承载并同步修订设计。
- 外部 provider smoke 不是单元测试前置条件。先用 checked-in fixtures/fakes 完成确定性覆盖；需凭据、联网或可能计费的请求必须先 preflight，0 requests、缺凭据或网络不满足一律记录 `not run`，不能写成通过或空结果。
- 实际完成记录必须写清改动路径、命令、环境、pass/fail/skip/not run 数、失败后的修复、剩余风险和 commit SHA。不得复制旧阶段 pass count，不得用 exit code 0、HTTP 200、收到 `stream_end` 或进程启动代替业务断言。

## M7 完成标准

- [x] 当前唯一 V1 能表达 dependency manifest、waterbody/summary discriminated session result、run summary、topology run result、partial coverage 和稳定引用；全部 producer/consumer/fixture/OpenAPI 原子一致。
- [x] fake chain、diamond、source、station-backed、stationless、mixed mode 和 bounded large graph 均由同一 production workflow 执行，node ready/claim/session 数量和顺序满足契约。
- [x] 每个 dependency status 的 evidence 资格、错误隔离和 direct-upstream authorization 有正反测试；invalid/insufficient 为 0 upstream records，usable refs 闭合且不会覆盖本地直接观测。
- [x] station-backed 目标缺失但有本地 discharge + usable upstream 可进入一次 loop；stationless 全不可用严格 0 planning/0 final/0 optional tool，并保存完整正式结果/session。
- [x] ready queue 在完成顺序扰动和并发 cancel 下仍 exact once，不超过配置并发；只消费 M4 topology 范围，不存在第二套运行规模或 run-wide token/model-call quota。
- [x] target、非 target failure、deterministic indeterminate、partial coverage 和 fatal target failure 的 run/session/node/result 状态均与终态矩阵一致。
- [x] summarizer 在全部 active nodes 收敛后创建，精确复用 waterbody model；target full + all other cards 无漏无重，plain-language summary、限制与特殊值 refs 通过机械验证。
- [x] summary failure 没有 fallback 文本，不抹掉 waterbody results；安全 public error、详细 backend log、summary session 与 run completion 语义彼此一致；provider cancelled 且 run token 未取消的完整 workflow 回归已覆盖。
- [x] 七类 M7 typed contents、result/completed 原子闭包、run snapshot/detail/history、PostgreSQL reload/startup repair 与两层 SSE 回放均有可执行证据。
- [x] cancel 在 dequeue、session create、local preflight、tool/model、publish、summary 和 final commit 边界有竞争测试；cancel_accepted 后无新业务动作，最终可观察 `workflow_quiesced`。
- [x] production composition 只有一个 M7 caller，单进程/单 worker 首版边界明确；OpenAPI 无新 route，frontend 未实现，M8 transcript producer 与 M9 clickable URL 未提前实现。
- [x] M1–M6 regression、M7 focused、architecture、OpenAPI、offline fixtures/docs audits、专用 PostgreSQL、smoke、`compileall` 和 `git diff --check` 有实际证据；真实 provider 与三平台未运行项如实记录。

## Milestone 0：基线、真实接缝与原子更新清单

### 工作

- [x] 记录 branch、HEAD、`origin/main`、merge-base、tracked/untracked/permission-limited 状态、Python/依赖版本和测试入口；明确保护本分支已有文档及用户改动。
- [x] 追踪真实 production call chain：route/coordinator → `QueryTopologyWorkflow` → topology save/phase → run completion，定位唯一延伸点、当前状态提交顺序和 cancel token 来源。
- [x] 盘点 M3 coordinator/writer/reader/registry/SSE、M4 `HydrologyGraph` 与 topology limits、M5 `UpstreamResultPort`/acquisition、M6 `RunSingleWaterbodyAgent`/runtime/model/transcript 的真实接口；形成 M7 影响表和单一 source-of-truth 表。
- [x] 扫描 `AgentRole`、`AnalysisPhase`、`SessionResultSummary`、session completed payload、run snapshot/detail/history、production typed-content registry、OpenAPI fixtures、runtime composition 和 test builders 的全部生产/测试消费者，形成原子更新清单。
- [x] 记录当前 M1–M6 full regression、architecture、PostgreSQL、SSE/history smoke 基线；环境缺失或 ACL 限制写 `not run`/permission-limited，不修改测试或设置绕过基础设施。
- [x] 增加 architecture red gates：M7 不依赖归档/前端/M8；Domain/ports 不导入 infrastructure/provider SDK；production 只有一个 topology workflow caller、两条 SSE、无新 public route；`backend/tests/application/reasoning/m7_support.py` 仍是 M6 内部测试 helper，不能误当本里程碑实现。
- [x] 确认默认不新增业务表/Alembic migration。若四表、通用 typed contents 和现有索引不足，必须在本阶段给出失败 fixture 与设计修订，不能直接建表。

### Tests

- [x] 当前 backend 基线无未解释失败；PostgreSQL 用例只使用进程级 `WATERAGENTS_TEST_DB_NAME=wateragents_test`，且与业务库不同。
- [x] architecture tests 锁定 archive isolation、layer dependency、production/test import isolation、单 workflow owner、无 direct M7 route 与仅两条 SSE。
- [x] 现有 M4 topology、M5 upstream port、M6 runner、lifecycle/registry/OpenAPI/SSE/history 基线结果被记录。

### Verify

- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests -q`（无法运行的 PostgreSQL/外部项明确拆出并记录）
- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/architecture backend/tests/application -q`
- [x] `backend/.venv/Scripts/python.exe -B -m compileall -q backend/app backend/scripts`
- [x] `git diff --check`

阶段出口：唯一 production 接入点、原子更新面、基线证据和默认无 migration 结论可复现；本阶段不新增 M7 业务行为。

提交边界：只允许 architecture guard、必要的基线测试修正、影响表和本清单完成记录；不得提前实现 scheduler、store、summary 或 runtime composition。

#### Milestone 0 完成记录（2026-10-03）

- 基线：分支 `codex/m7-milestone-0` 从 `origin/main`/merge-base `59efea071f9f46e3949ca3bd14ee403c771a5151` 创建；既有 M7 文档输入受保护并独立提交为 `fe95b293d863d2c0fc92643f61fa8693a99b69f9`。Python 3.12.14，pytest 9.1.1，FastAPI 0.141.1，Pydantic 2.13.5，SQLAlchemy 2.0.54，psycopg 3.3.6，httpx 0.28.1。仓库根部历史 pytest 目录存在 ACL permission-limited 警告，因此状态结论限于 tracked 文件及 `docs`、`backend/app`、`backend/tests`、`backend/scripts` 等本阶段路径。
- 调用链：`POST /api/analysis/runs` → `RunCoordinator.create_run` → 唯一 `_run_workflow` task/cancellation token → `QueryTopologyWorkflow` → 保存 `query_understood`/`topology_ready` 并推进 phase → workflow 返回后 `complete_run`。M7 唯一延伸点是同一 workflow 在 `topology_ready` 后继续执行 DAG；不得增加 route/background caller。
- 影响与事实源：`analysis_runs/models.py` 定义 role/phase/session/result；coordinator/runtime/writer 与四表保存 lifecycle、cancel、quiescence 和原子结果；transcript registry/reader/stream models 负责 typed content、history 与两层 SSE；保存后的 `AnalysisContext`/`HydrologyGraph` 是 M4 scope/limits 权威；M5 `UpstreamResultPort`/acquisition 是唯一上游证据接缝；M6 `RunSingleWaterbodyAgent`、runtime/model/transcript 是唯一单水体执行链。原子消费者包括 API schemas/routes、persistence records/store、startup repair、OpenAPI fixtures，以及 application/API/persistence/smoke test builders。
- 实现：新增 `test_m7_boundaries.py`，锁定单一 topology workflow composition/执行 owner、M7 不导入 M8 transcript/RAG port、`m7_support.py` 仅为测试 helper；扩展现有 public-surface guard 拒绝 direct M7/scheduler/summary route。修正 M7 文档同步触发的 M6 状态措辞基线；未新增 M7 业务行为。
- 数据库：Docker PostgreSQL 17 healthy；仅在测试进程设置 `WATERAGENTS_TEST_DB_NAME=wateragents_test`，与业务库分离。现有四表和唯一初始 migration 未改动，M0 没有新增表、索引或 Alembic revision。
- 验证：focused guard/document gate `18 passed`；architecture + application `866 passed`；完整 `backend/tests`（含专用 PostgreSQL、M4/M5/M6、registry/OpenAPI/SSE/smoke）`1842 passed, 1 warning`。`run_sse_smoke.py` 的 completed/cancelled 通过；`run_history_smoke.py` 的 history、独立进程重启和 startup repair 通过；`compileall` 与 `git diff --check` 通过。首次受限运行的 PostgreSQL/pytest 临时目录错误记为 permission-limited，授权上下文重跑后无遗留失败。
- 风险：真实外部 provider 请求和本次提交后的 Ubuntu/macOS/Windows CI 均 `not run`；Milestone 1–9、frontend、M8 producer 与 M9 接力未开始。
- 提交：架构红线与必要基线修正 `258e69149b6ce352ab1f06fdd3631e73085b5410`；本完成记录由后续本地文档提交保存。

## Milestone 1：当前唯一 V1 类型与投影骨架

### 工作

- [x] 在 `application/analysis_runs`/`application/run_summary` 对应模块新增并验证 `DependencyOutcome`、`WaterbodyDependencyManifest`、`WaterbodyRunEntry`、`TopologyRunResult`、`WaterbodyReportCard`、`RunSummaryInput`、`RunSummaryResult` 与稳定 canonical order/digest；Domain/Application 不导入 transport/provider 类型。
- [x] 将 `AgentRole` 原子扩展为 `parser / waterbody / run_summarizer`，其中 summarizer `target_comid=null`；把 `SessionResultSummary` 收敛为按 role 判别的唯一 V1 union，非法 role/result/COMID 组合构造即失败。
- [x] 扩展 `AnalysisPhase` 为 `waterbody_execution`、`run_summarization`，并定义 run snapshot/result projection 的最小结构；phase 不代替 run/session/node status。
- [x] 固化 dependency、card、summary 和 run-result ref/version 规则：每个 active COMID 恰好一个 entry，target 恰好一个 full projection，其他 active node 恰好一张 card；refs 必须属于同 run/session/content。
- [x] 为七类 M7 typed content 定义 application payload/schema，但本阶段不注册 writer，也不做模型调用或调度。
- [x] 原子更新 serializers、builders、fixtures、OpenAPI schema inputs 和 exhaustive match；删除旧封闭枚举假设，不建立 V2/read union 或兼容分支。

### Tests

- [x] dependency status/manifest、partial coverage、target failure、summary availability 和 cross-reference closure 的构造正反例。
- [x] `AgentRole` × COMID × result summary 判别矩阵；旧 parser/waterbody 数据仍按当前唯一 V1 正常解析。
- [x] canonical ordering/digest 在输入乱序、重复 COMID/ref、缺 target/card、跨 run ref 时稳定或拒绝。

### Verify

- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/domain backend/tests/application/analysis_runs/test_topology_result_models.py backend/tests/application/run_summary/test_models.py -q`
- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/architecture/test_domain_dependencies.py backend/tests/architecture/test_current_v1_contracts.py -q`
- [x] `backend/.venv/Scripts/python.exe -B -m compileall -q backend/app/domain backend/app/application`
- [x] `git diff --check`

阶段出口：M7 需要的当前唯一 V1 类型能独立表达完整业务事实并机械拒绝非法组合；尚无 runtime、writer、scheduler 或模型调用。

提交边界：仅类型、validation、exports、直接 fixtures/consumers 与 focused tests；不得实现 store、evidence collect、workflow 或 summary runner。

#### Milestone 1 完成记录（2026-10-03）

- 实现：新增 dependency/manifest/topology result 与 target/report-card/summary 的严格 V1 application models、稳定 canonical JSON/digest 和同 run/session/content 引用闭包；扩展唯一 `AgentRole`、`AnalysisPhase`、role-discriminated session result、run snapshot/detail/history/OpenAPI 投影及全部直接消费者。七类 M7 payload 只有 schema，production registry 未注册，未接入 writer、模型、scheduler 或 workflow。
- 数据库：PostgreSQL 红灯 fixture 证明原 M3 `analysis_sessions` CHECK 拒绝 `run_summarizer`；新增窄 revision `20261003_01`，只扩展现有 CHECK 并增加每 run 一个 summarizer 的 partial unique index，不新增表/结果列。现有 `wateragents` 与专用 `wateragents_test` 均已 upgrade/check 到当前 head。
- 验证：M1 domain/model gate `361 passed`；指定 architecture gate `8 passed`；完整 architecture `120 passed`；最终完整 `backend/tests`（含 Docker PostgreSQL、OpenAPI/SSE/history/M1–M6 回归）`1866 passed, 1 warning`；`compileall` 与 `git diff --check` 通过。首次普通权限的 pytest 临时目录错误为 Windows ACL permission-limited；一次隔离进程流式脱敏性能阈值受负载抖动，原用例连续 3 次及随后完整回归均通过。
- 风险：真实外部 provider 请求和 Ubuntu/macOS/Windows CI `not run`；Milestone 2–9 尚未开始，当前无 M7 runtime、writer registration、DAG scheduler、summary runner 或 frontend 行为。
- 提交：本完成记录与 M1 类型/直接消费者由当前本地提交保存。

## Milestone 2：run-local outcome store 与真实上游证据端口

### 工作

- [x] 实现 run-local immutable outcome store：每个 active COMID 只能发布一次 canonical outcome，记录 session/result/ref/availability/dependency status；重复或身份不一致发布失败。
- [x] 实现真实 `UpstreamResultPort`，只允许当前 node 的 participating direct-upstream COMIDs、同 run、已收敛 outcome；拒绝祖先越级、兄弟、下游、任意 station/COMID 和跨 run 读取。
- [x] usable outcome 投影为现有 `ProviderCollectionBatch[UpstreamResultRecord]`，包含 result/confidence/evidence summary/uncertainty、逐窗口 discharge 状态和闭合 refs；不重算、不加权、不覆盖本地 evidence。
- [x] insufficient/invalid/cancelled 为 0 `UpstreamResultRecord`，但全部 direct dependency 状态进入稳定排序的 `waterbody_dependency_manifest v1`；invalid 不含内部错误、结果或 evidence refs。
- [x] 将 manifest 作为 downstream prompt 中 dependency status 的唯一事实源；M5 descriptor/scope/options/request-shape 的 pure preflight 仍在 scope preparation/call audit 之前，合法调用后的失败仍保留原 typed audit。
- [x] producer 发布与 collect authorization 复用 topology snapshot，不另建 Agent-to-Agent channel、共享 mutable prompt 或跨 run cache。

### Tests

- [x] chain/diamond direct-upstream allow/deny、同 run identity、发布 exact once、乱序输入稳定 manifest。
- [x] usable 记录字段/ref 闭包；insufficient/invalid/cancelled 均 0 records，且 invalid 不泄露错误正文。
- [x] M5 pure preflight 保持 invalid input `0 scope prepare / 0 call / 0 result`；合法 collect failure 仍有 typed call/result audit。

### Verify

- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/application/evidence_tools/test_upstream_result_store.py backend/tests/application/evidence_tools/test_upstream_result_provider.py -q`
- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/application/evidence_tools backend/tests/domain/evidence -q`
- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/architecture/test_evidence_tool_boundaries.py -q`
- [x] `git diff --check`

阶段出口：下游可通过原 M5 evidence seam 安全读取“仅直接上游、仅 usable”的真实记录，同时完整看到所有直接依赖状态；无 scheduler 或水体启动决策。

提交边界：仅 outcome store/upstream projection/M5 collect adapter、manifest builder 与对应测试；不得创建 sessions、运行 M6 runner 或汇总 Agent。

#### Milestone 2 完成记录（2026-10-03）

- 实现：新增 run-local immutable outcome store、可信 transcript result/closure reader、usable-only `UpstreamResultRecord` projection 与稳定 dependency manifest builder；授权完全复用保存后的 topology，只接受同 run、已收敛、participating direct-upstream outcomes，并提供 run cleanup，不引入跨 run cache 或 Agent-to-Agent channel。
- 安全：usable result/session/cell/closure identity 在发布时机械复核；insufficient/invalid/cancelled 均返回 0 records，manifest 仍保留全部 direct dependency statuses，invalid 的内部 error/result/evidence refs 不下传。M5 invalid request 保持 `0 scope prepare / 0 call / 0 result`，合法 producer failure 保留 typed call/result audit。
- 验证：M2 focused `11 passed`；evidence-tools/domain gate `86 passed`；完整 domain/application gate `432 passed`；M2 architecture gate `2 passed`，完整 architecture gate `122 passed`；最终完整 `backend/tests`（含 Docker PostgreSQL）`1880 passed, 1 warning`，warning 仅为既有 Starlette/httpx 弃用提示。普通权限完整 architecture 首次因 Windows pytest 临时目录 ACL 拒绝，提升权限使用独立目录后通过；真实外部 provider 与三平台 CI `not run`。
- 边界：未创建 waterbody session、未运行 M6 runner、未接 DAG scheduler/workflow、未注册 M7 typed contents，也未实现 run summarizer 或 frontend；Milestone 3–9 尚未开始。
- 提交：本完成记录与 M2 store/provider/manifest/tests 由当前本地提交保存。

## Milestone 3：水体启动门禁与零模型正式结果

### 工作

- [x] 新增 backend-owned node launch decision。输入只来自 topology/station facts、dependency manifest、M5 capability/collection facts和 cancel token；输出明确 `run_agent / deterministic_insufficient / invalid / cancelled` 及可审计理由。
- [x] station-backed 总是先执行本地目标参数与 discharge preflight；目标值缺失但本地 discharge/允许的支持证据加至少一个 usable upstream 时允许进入同一个 M6 planning loop。
- [x] stationless 不制造本地空查询；至少一个 usable direct upstream 才可 planning。全部 direct upstream insufficient/invalid 时，先创建水体 session，再复用 M6 skeleton/assembler 生成完整 `indeterminate / insufficient_evidence`，调用数严格为 0/0/0。
- [x] source/root node 没有 direct upstream 时按 topology/station facts 进入 station-backed 路径，不把空依赖集合误判为“上游证据不足”。
- [x] 构造 M6 waterbody prompt dependency block：明确本地同参数站点值是直接观测，上游值是上游水体推理；可按需使用本地其他站点参数（例如 discharge）、天气、设施等允许的间接证据。
- [x] 保持数值边界：普通上游 + 本地间接证据只可支持 directional/trend/risk；registry 可执行确定性公式/工具是具体 inferred value 的唯一入口。
- [x] deterministic insufficient outcome 发布为 `insufficient`，保留完整 result/session 供 run 汇总，但不给下游 `UpstreamResultRecord`。

### Tests

- [x] station-backed：target+discharge、target missing+discharge+usable upstream、target missing 无任何种子、source node 四条路径及调用计数。
- [x] stationless：usable+invalid 可运行；all insufficient/invalid 为 0 planning/0 final/0 optional tool；完整 session/result/ref 存在但下游记录为 0。
- [x] prompt fixture 锁定 source roles、local discharge 可用、invalid 无详情、禁止自造公式/权重/具体数值；gap_filling/prediction/mixed 与 window 原样传入。

### Verify

- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/application/analysis_runs/test_node_launch_policy.py backend/tests/application/analysis_runs/test_zero_model_result.py -q`
- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/application/reasoning/test_prompt_policy.py backend/tests/application/reasoning/test_runner.py -q`
- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/application/evidence_tools/test_acquisition_workflow.py -q`
- [x] `git diff --check`

阶段出口：backend 可在不依赖 scheduler 的单节点 harness 中正确决定是否运行 Agent，并为 stationless 全不可用路径保存可审计的零模型正式结果。

提交边界：仅 launch policy、dependency prompt block、M6 handoff 的最小接缝、zero-model assembler 与 tests；不得接通完整 DAG 或 run summarizer。

#### Milestone 3 完成记录（2026-10-03）

- 实现：新增 backend-owned node launch policy、station/local + direct-upstream deterministic preflight、M6 initial handoff 复用、canonical dependency prompt block 与 zero-model insufficient assembler；usable dependency 才暴露 result ref，invalid/insufficient 不暴露 result/error/evidence，未接 scheduler、完整 DAG、run summarizer 或 frontend。
- 验证：三组 M3 focused 分别 `9 passed`、`28 passed`、`17 passed`；domain/application `1139 passed`；architecture `122 passed`；最终完整 `backend/tests` 使用 Docker 中健康的 PostgreSQL 与进程级 `wateragents_test` 得到 `1892 passed, 1 warning`，warning 仅为既有 Starlette/httpx 弃用提示；`compileall`、应用导入与 `git diff --check` 通过。architecture 首次受 Windows pytest 临时目录 ACL/目录准备影响，改用 `D:\DockerData\pytest-temp` 独立目录后通过；真实外部 provider 与三平台 CI `not run`。
- 边界：Milestone 4–9 尚未开始；当前只提供 scheduler 无关的单节点 launch/result harness 与 M6 接缝，M1–M9 仍作为一个原子任务等待最终验收。
- 提交：本完成记录与 M3 launch/preflight/prompt/zero-model/tests 由当前本地提交保存。

## Milestone 4：依赖感知 scheduler 与有界并行

### 工作

- [x] 从 immutable `HydrologyGraph`/active topology view 构造 direct dependency counters、reverse edges 和稳定 ready priority；不重新 BFS、不扩展范围、不让 Agent 修改 graph。
- [x] node 只有在全部 participating direct-upstream outcomes settle 后入队；claim 使用单一原子状态，保证并发完成通知、重复回调和异常收敛下只执行一次。
- [x] 用全局 limiter 执行互不依赖的 ready nodes；`WATERBODY_WORKFLOW_MAX_CONCURRENCY` 默认 4、允许 1–32，配置非法时启动失败，不在运行中静默 clamp。
- [x] 稳定 tie-break 只决定同等 ready 节点的启动优先级，不把并发完成顺序冒充科学语义；测试不得依赖 wall-clock 完成顺序。
- [x] scheduler 只依赖 `NodeRunner`/outcome port，可用 fake runner 独立测试。node error 必须 settle 为 canonical invalid outcome，再解锁可能仍可运行的汇流节点。
- [x] 整次工作量仅使用 M4 snapshot 中已冻结的 distance/hop/node 边界；禁止添加 run token budget、run model-call counter 或第二套 node cap。

### Tests

- [x] chain、fork、diamond、invalid+usable merge、all insufficient、runner exception、duplicate completion、out-of-order completion 的 exact-once/ready assertions。
- [x] 并发上限 1、默认值、最大值和非法 0/33；峰值 in-flight 不超配置且独立节点确实可重叠。
- [x] 最大允许 topology fixture 全覆盖且无范围外 COMID；per-session budget 耗尽只影响对应 outcome，不变成 run-wide quota。

### Verify

- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/application/analysis_runs/test_topology_scheduler.py -q`
- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/domain/test_hydrology.py backend/tests/domain/test_hydrology_topology_models.py backend/tests/application/test_analysis_context.py backend/tests/application/test_analysis_context_resolver.py backend/tests/application/test_date_resolver.py backend/tests/application/test_parameter_resolver.py backend/tests/application/test_location_resolver.py backend/tests/application/test_query_parser_prompt.py backend/tests/application/test_query_parser_workflow.py backend/tests/application/test_station_availability.py backend/tests/application/test_hydrology_topology_builder.py backend/tests/application/test_query_topology_workflow.py -q`
- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/application/analysis_runs/test_topology_scheduler_races.py -q`
- [x] `git diff --check`

阶段出口：scheduler 可用 fake runner 稳定执行任意已冻结 active DAG，ready/claim/并发/范围行为均可机械验证；尚未替换 production workflow。

提交边界：仅 scheduler、limiter setting、fake runner protocol 与 tests；不得在 route/coordinator 中建立第二个临时 caller，也不得接 summary/persistence。

#### Milestone 4 完成记录（2026-10-03）

- 实现：新增从 canonical `HydrologyGraph` 重验证并构造的 immutable active execution view、direct dependency counters/reverse edges、稳定 ready queue 与原子 claim/settle 状态机；新增只依赖 `NodeRunner`/outcome port 的 scheduler 及可在多个 run 之间共享的 `WaterbodyWorkflowLimiter`，配置 `WATERBODY_WORKFLOW_MAX_CONCURRENCY` 默认 4、严格限定 1–32；未重做 BFS、未扩展 topology 范围、未新增 run-wide quota/node cap。
- 验证：scheduler focused `10 passed`，当前 topology/query-understanding 实际回归文件 `171 passed`，race/limiter focused `7 passed`，analysis-runs + API health `76 passed`，architecture `122 passed`；最终完整 `backend/tests` 使用 Docker 中健康的 PostgreSQL 与进程级 `wateragents_test` 得到 `1909 passed, 1 warning`，warning 仅为既有 Starlette/httpx 弃用提示；应用构造与 `git diff --check` 通过。首次全量仅有既有 M6 SSE smoke 在 45 秒总时限下超时（`1908 passed, 1 failed`），该用例随后单独复跑 `1 passed`，再次全量全绿；真实外部 provider 与三平台 CI `not run`。
- 边界：Milestone 5–9 尚未开始；scheduler 尚未替换 production `QueryTopologyWorkflow`，也未接 run summarizer、持久化或 frontend，M1–M9 仍作为一个原子任务等待最终验收。
- 提交：本完成记录与 M4 scheduler/limiter setting/fake runner/tests 由当前本地提交保存。

## Milestone 5：同一 production workflow 执行全部水体

### 工作

- [x] 延伸现有 `QueryTopologyWorkflow`：保存 topology 后进入 `waterbody_execution`，使用 Milestone 4 scheduler、Milestone 3 launch gate、Milestone 2 store 与同一个 M6 `RunSingleWaterbodyAgent`；删除 topology 后直接结束 run 的旧 terminal path。
- [x] 每个 ready node 在准备运行 Agent 或生成 deterministic result 时创建一个 waterbody session；DAG 初始化不预建，未启动节点没有假 session，创建后失败/取消则保留真实 session/process。
- [x] 将 M6 canonical `SessionSummary`/持久化 result 作为 outcome 权威，不传播 runner 返回但 coordinator 未提交的晚到/失配结果。
- [x] 汇聚所有 active node outcomes，确定 target main conclusion、non-target failures、coverage/partial 状态和 fatal target failure；target deterministic indeterminate 保持 completed。
- [x] 保持 mode、timezone、segments、ParameterKey、window 和 model selection 从 run context 原样贯穿；不得为各 node 猜新模式或自行扩大参数范围。
- [x] 本阶段以 fake summary boundary 暂停在“waterbody outcomes settled”，不得伪造最终 `TopologyRunResult`、提前将 run completed，或保留一个旁路 production workflow。

### Tests

- [x] production workflow fake chain/diamond：session 创建时机、每 node 一次、upstream publish、M6 runner 调用和 phase 顺序。
- [x] invalid+usable merge 继续一次、all insufficient 零模型、non-target failed partial、target failed fatal、target indeterminate completed-ready-for-summary。
- [x] coordinator 权威竞态：runner late completed、persistence/session failed、cancel 胜出时均不错误传播。

### Verify

- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/application/analysis_runs/test_topology_driven_workflow.py backend/tests/application/analysis_runs/test_topology_result_assembler.py -q`
- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/application/reasoning backend/tests/application/evidence_tools -q`
- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/architecture/test_analysis_workflow_composition.py -q`
- [x] `git diff --check`

阶段出口：唯一 production workflow 已能从 topology 执行并收敛全部水体，但尚未调用 summarizer 或提交最终 run result/run completed。

提交边界：只包含 workflow 延伸、node runner adapter、waterbody outcome aggregation 与 tests；不得用临时文本完成 summary，也不得提前注册最终 typed contents。

#### Milestone 5 完成记录（2026-10-03）

- 实现：延伸唯一 `QueryTopologyWorkflow`，由 coordinator phase gate 进入 `waterbody_execution`，用同一 M4 scheduler、M2 run-local store/M5 upstream provider、M3 launch policy/zero-model assembler 和 M6 runner 执行全部 active nodes；session 只在 node claim 后创建。新增 committed-session result assembler、target/partial/failure 收敛、按 run provider routing/cleanup、一次性 production composition binding；全部 outcomes settle 后进入 `run_summarization` 并停在 fake boundary，没有创建最终 result 或完成 run。
- 验证：M5 workflow/result focused `11 passed`；reasoning + evidence tools `234 passed`；production composition `2 passed`；analysis-runs/M4–M6 runtime/architecture 宽回归在 D 盘 basetemp 为 `228 passed`；fresh-import/composition 最终门禁 `6 passed`；`compileall` 与 `git diff --check` 通过。Docker PostgreSQL 17 healthy，测试进程只设置 `WATERAGENTS_TEST_DB_NAME=wateragents_test`；最终完整 `backend/tests` 为 `1922 passed, 1 failed, 1 warning`，唯一失败是既有 M6 session SSE smoke 的 45 秒外层超时，该用例单独复跑 `1 passed in 29.73s`，其业务断言无失败；warning 为既有 Starlette/httpx 弃用提示。
- 修复与边界：宽回归发现并修复 `WorkflowOperations` 类型导入环、旧 test runtime 可选参数兼容和 M4 smoke 的显式 test-only terminal stage。真实外部 provider 与三平台 CI `not run`；Milestone 6–9、summary Agent、最终 typed contents/persistence/run completion、完整 cancel races 与 frontend 均未完成，M1–M9 仍等待 M9 后统一验收。
- 提交：本完成记录与 M5 workflow/node adapter/outcome aggregation/composition/tests 由当前本地提交保存。

## Milestone 6：独立 run summarizer Agent

### 工作

- [x] 实现 `application/run_summary` prompt builder、runner、validator 与 transcript adapter；全部 active waterbody outcomes settle 后才创建 `run_summarizer` session，`target_comid=null`。
- [x] 复用本 run 精确 `waterbody_model` selection 与现有 provider adapter；provider visible output 仅作内部候选/审计，summary 无 tools、无第三模型设置、无 fallback，主调用一次，schema repair 至多一次且同模型/同 scope。
- [x] input builder 只读持久化安全业务投影：target 一份 full projection，其他每个 active node 恰好一张 compact card，稳定 topology 顺序；technical details、secrets、原始 provider envelope 和隐藏 reasoning 不进入 prompt。
- [x] prompt 要求用浅显语言说明全流程、目标结论、分支覆盖/限制和特殊值；不得改变水体科学结论、重新评分、补造缺失数字或把 insufficient 猜成 0。
- [x] validator 对任何数字 highlight 强制 exact value、unit、window、source refs，并验证文本中的数字只等于 display value；plain summary 除 scope 内 COMID 外不允许数字，非数字 highlight 不允许数字。拒绝 Agent URL、scope 外 COMID/ref、遗漏 target/coverage 和不支持的断言。
- [x] summary 失败保存 failed session 和安全 public failure，详细异常只进 backend log/audit；只有 run cancellation token 已置位才返回 cancelled，provider 自身的 cancelled terminal 按可重试技术失败处理；不生成固定模板、拼接 fallback 或旧 summary。target 有效时仍允许后续 run completed。

### Tests

- [x] input 恰好 target full + N-1 cards，输入乱序后稳定、每 active node 无漏无重；large bounded topology 仍满足 model input capability，否则确定性 safe failure。
- [x] same-model/no-tools/one-repair 调用计数；invalid JSON、bad ref、数字缺 unit/window、URL、第二次 repair 失败均按契约终止。
- [x] plain-language output 保留 target science、partial coverage、invalid/insufficient 限制和特殊值；summary failed 无任何 fallback content；provider cancelled/run token 未取消有 runner 与完整 workflow 回归。

### Verify

- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/application/run_summary/test_prompt_builder.py backend/tests/application/run_summary/test_runner.py backend/tests/application/run_summary/test_validator.py -q`
- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/infrastructure/test_openai_responses_waterbody.py -q`
- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/application/run_summary/test_transcript_store.py -q`
- [x] `git diff --check`

阶段出口：summary runner 可在 direct application harness 中产出严格可引用的浅显汇总，或真实失败且无 fallback；尚未完成 final persistence/API/SSE 集成。

提交边界：仅 run_summary application、复用现有 provider/transcript 的最小 adapter、fixtures 与 tests；不得新增 provider、route、SSE 或 frontend URL。

- 实现：新增 canonical summary input/prompt、严格 draft validator、一次同模型 repair runner、provider response 审计 adapter 与 direct application stage。输入固定为稳定顺序的非 target cards + 末尾 target full safe result；highlight 只接受允许 kind/ref，数值必须逐项匹配原 value/unit/window。OpenAI Responses adapter 仅扩展接受同一 provider-neutral summary request，tools 始终为空；未新增 provider、route、SSE、frontend URL 或 fallback。
- 验证：指定 summary prompt/runner/validator `15 passed`，provider adapter `32 passed`，summary transcript `2 passed`；M4–M7 application/provider 宽回归 `344 passed`，port/M7 architecture + summary 回归 `81 passed`，`compileall` 与 `git diff --check` 通过。专用 `wateragents_test` 下除既有 45 秒 session SSE 超时用例外的完整套件为 `1940 passed, 1 deselected, 1 warning`；该 SSE 用例单独复跑仍在打开首个 stream 时 `54.78s` 外层超时、尚未进入业务断言。warning 为既有 Starlette/httpx 弃用提示；Docker PostgreSQL 17 healthy。真实外部 provider 与三平台 CI `not run`。
- 修复与边界：完整收集发现并修复测试模块同名冲突及多余 summary port 的架构违规。Milestone 7 的 production typed-content registry、summary completed/result 原子提交、API/SSE/history 与最终 run completion 尚未接线；当前阶段出口仅为 direct application harness，M1–M9 仍等待 M9 后统一验收。
- 提交：本完成记录与 M6 summary application/provider adapter/tests 由当前本地提交保存。

## Milestone 7：typed transcript、持久化、SSE 与 history

### 工作

- [x] 在 production registry 注册且只注册 M7 七类当前 V1 内容：`waterbody_dependency_manifest`、`run_summary_input`、`run_summary_model_request`、`run_summary_agent_message`、`run_summary_model_response`、`run_summary_result`、`topology_run_result`。
- [x] summarizer 的 provider delta/structured candidate 只保存为 history-only 审计内容；validator 通过后，以既有 `transcript_item` 一次性发布 completed `run_summary_agent_message` 与 result。item identity、Last-Event-ID、late audit 和两层 SSE 全部复用原能力，不增加第三条流。
- [x] summary result 与 summary `session_completed(result_ref + discriminated result_summary)` 同一必需提交批次；`topology_run_result`、run result ref/summary projection 与 run completed lifecycle 同一等价原子边界。
- [x] 扩展 run snapshot/detail/history：waterbody node 五态、phase、partial coverage、summary status/ref/safe failure、topology result ref；读取只投影持久化事实，不重新调度、推理或重算。
- [x] 保持现有 run SSE 与 session SSE。run stream 只发轻量快照；waterbody/summarizer 真实过程在对应 session stream；不复制完整 result/prompt/tool 内容到 run stream。
- [x] PostgreSQL inline/reference、补写、reload、startup repair、history pagination 和 scope+explicit refs 继续使用四表；若需要 D33 关联索引，只能按已有设计增加同一索引，不建立第二套关联存储。
- [x] 同步 current OpenAPI/serializers/fixtures，确认 production 路由集合不变且没有 direct M7/scheduler/summary/test endpoint。

### Tests

- [x] 七类 registry/serialization/reference closure/unknown version；role-discriminated completed payload 与 result identity 原子性。
- [x] run/session SSE 首次加载、live handoff、resume、stream_end、cancel incomplete、summary failed 和 run completed partial 快照。
- [x] PostgreSQL result/content/reference reload、startup interruption、pending write retry、history detail 按 waterbody/summarizer session 读取。

### Verify

- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/application/transcripts backend/tests/application/analysis_runs/test_m7_projections.py -q`
- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/api/test_analysis_runs.py backend/tests/api/test_analysis_events.py backend/tests/api/test_openapi_contract.py -q`
- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/persistence/test_m7_topology_workflow_persistence.py -q`（仅专用 `wateragents_test`；未配置则 `not run`）
- [x] `backend/.venv/Scripts/python.exe -B -m compileall -q backend/app backend/scripts`
- [x] `git diff --check`

阶段出口：M7 全部内容、result refs、session/run lifecycle 和读取投影可以经真实 writer/reader、PostgreSQL 与既有两层 SSE 闭环；无新 public surface。

提交边界：仅 registry/writer-reader/lifecycle/projection/OpenAPI fixture 与 tests；不得实现 frontend、M8 producer、点击 URL 或额外表。

#### Milestone 7 完成记录（2026-10-03）

- 实现：production registry 接入七类 M7 V1 内容；summary/result lifecycle 按必需提交组原子保存；run snapshot/detail/history 增加五态 waterbody、phase、partial coverage、summary ref/安全失败和 topology result ref 投影。继续复用既有四表、run/session 两层 SSE、typed writer/reader 与补写/启动修复能力；未新增 route、表、SSE、frontend、M8 producer 或点击 URL。
- 验证：指定 application `5 passed`，API/SSE/OpenAPI `119 passed`；Docker PostgreSQL 17 healthy，专用 `wateragents_test` 持久化闭环 `1 passed`；完整 backend `1948 passed, 1 warning`，warning 为既有 Starlette/httpx 弃用提示；`compileall` 与 `git diff --check` 通过。
- 风险：真实外部 provider 与三平台 CI `not run`；M8 production composition/cancel races 和 M9 端到端统一验收仍待后续 Milestone，不把当前内部切片声明为最终验收完成。
- 提交：本完成记录与 M7 registry/persistence/SSE/history 实现由当前本地提交保存。

## Milestone 8：production composition、取消与竞争收敛

### 工作

- [x] 新增 `app/infrastructure/runtime/m7.py` 或等价单一 composition root，把 scheduler/store/node runner/summarizer 接到 coordinator 已拥有 workflow；production 只有一个 caller，test harness 不进入 production import graph。
- [x] 配置 `WATERBODY_WORKFLOW_MAX_CONCURRENCY`、`RUN_SUMMARY_TIMEOUT_SECONDS` 和基于精确 model capability 的 summary output/input limits；不增加 summary model setting、run-wide token/model-call quota 或自动 retry expansion。
- [x] 在 dequeue、claim 后 session create 前、本地 preflight、tool/model 前、publish、summary create/call 和 final commit 设置真实 cancel checkpoint。cancel 接受后先封调度，再通知全部已创建未终态 sessions。
- [x] 处理 cancel/create、cancel/node completion、cancel/summary completion、target failure/other completion 和 duplicate callback races；已完成 sessions 保持终态，未创建下游不补 session。
- [x] 无法强杀的 provider/tool late output 只进入 cancelled transcript/日志审计，不追加当前正文、不发布 upstream/result、不触发 summary；必要保存补写仍完成。
- [x] `cancel_accepted` 与 `workflow_quiesced` 分开；live cleanup、run stream 正常关闭与后台执行生命周期必须等待 quiesced，不因客户端断开提前终止。
- [x] 首版明确单进程/单 worker in-memory scheduler/store 边界；启动文档/部署配置不得暗示共享状态、多 worker 或跨进程 cancel 已支持。

### Tests

- [x] 每个 checkpoint 的 cancel race；接受后 new session/tool/model/upstream publish/summary/final result 调用数均为 0。
- [x] cancel 前 completed session 保持 completed，in-flight session cancelled/incomplete，late content 可审计但不可从正常读取/下游看到，最终 workflow_quiesced。
- [x] production dependency graph 唯一、invalid settings fail fast、无 provider 时安全失败、same model selection、单 worker documentation/config guard。

### Verify

- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/infrastructure/test_m7_runtime.py backend/tests/application/analysis_runs/test_topology_cancellation.py -q`
- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/application/analysis_runs/test_topology_races.py backend/tests/application/analysis_runs/test_workflow_quiescence.py -q`
- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/architecture/test_production_test_isolation.py backend/tests/architecture/test_analysis_workflow_composition.py -q`
- [x] `backend/.venv/Scripts/python.exe -B -m compileall -q backend/app backend/scripts`
- [x] `git diff --check`

阶段出口：M7 已进入唯一 production composition，取消与竞争收敛可重复验证，且运行配置/部署边界诚实；尚未宣称完整端到端验收完成。

提交边界：仅 runtime/settings/coordinator cancel integration、race fixes、deployment wording 与 tests；发现前序契约缺口必须回到对应 Milestone 修复并重跑，不在本阶段堆叠大补丁。

#### Milestone 8 完成记录（2026-10-03）

- 实现：唯一 M7 production composition 已串联 scheduler、run-local outcome store、waterbody runner、同模型 summarizer 与最终 topology result；新增 summary timeout 严格配置并复用精确 model capability。dequeue/session/preflight/tool-model/publish/summary/final commit 均接入取消检查，提交门保证 cancel acceptance 与 outcome publish 互斥，既有 late-output 审计、必要补写和 quiesced cleanup 语义保持不变；README 明确单进程/单 worker 边界。
- 验证：指定 runtime/cancel `13 passed`，race/quiescence `3 passed`，production isolation/composition `11 passed`；Docker PostgreSQL 17 healthy，专用 `wateragents_test` 完整 backend `1966 passed, 1 warning`，warning 为既有 Starlette/httpx 弃用提示；`compileall` 与 `git diff --check` 通过。
- 风险：真实外部 provider 与三平台 CI `not run`；M9 fake E2E、最大 topology、离线出口扫描与统一验收仍待后续 Milestone，不把 M8 内部切片声明为最终验收完成。
- 提交：本完成记录与 M8 production composition、取消/竞态收敛、配置/部署边界由当前本地提交保存。

## Milestone 9：端到端验收、契约审计与 M8/M9 接力

### 工作

- [x] 新增固定 fake M7 smoke，至少覆盖 3-node chain、diamond merge、stationless all-insufficient、invalid+usable、non-target failure partial、target failure、summary success/failure、provider cancelled 但 run 未取消、cancel/quiesced。
- [x] 使用最大允许 topology 边界 fixture 验证范围、峰值并发、每 node/session/card 恰好一次、summary model fit 和无 run-wide quota；不得用缩小 topology 设置掩盖问题。
- [x] 在专用 PostgreSQL 验证完整 workflow、result/session/content refs、run/session SSE、history/restart/startup repair、补写与 cleanup；数据库不可用时明确 `not run`，不以 SQLite/fake 替代。
- [x] 执行完整 backend regression、architecture、OpenAPI、typed registry、policy fixture、secrets/log/SSE/history 出口扫描、Markdown links/JSON/code fences/trailing whitespace 和 contract ID 双向审计。
- [x] 可选真实 provider smoke 必须得到显式计费/联网授权并先 preflight；否则记录 requests/events/responses 均为 0 的 `not run`，不得影响 fake/fixture 验收结论。
- [x] 更新本清单矩阵和各 Milestone 完成记录；项目级文档只写有实际证据的状态，未跑 PostgreSQL/provider/三平台 CI 不得写 completed。
- [x] 确认 M8 只消费稳定 transcript/result refs，M9 只消费 snapshot/history/summary refs 并负责 clickable URL 与 frontend；M7 不提前实现 RAG producer、页面、地图连线或 admin/settings UI。

### Tests

- [x] fake E2E 对 session 创建时机、topology start/completed、dependency manifest、model/tool counts、target/partial/run status、provider cancellation ownership 和 summary refs 做业务断言。
- [x] M7 policy、scheduler、store、gate、summary、七类 typed content、lifecycle/history/runtime/cancel 均有独立可执行 gate。
- [x] secrets、provider envelope、hidden reasoning、raw arguments/URL、内部错误和 unbounded arrays 的 logs/API/SSE/history 全出口扫描通过；request/response 的 preview 与 full content 都使用安全投影，raw provider text 不先于 validation 公开。
- [x] contract ID 集合与覆盖矩阵双向一致；所有勾选项均有本分支实际证据，未执行项保持未勾选或 `not run`。

### Verify

- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests -q`
- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/persistence -q`（仅专用 `wateragents_test`；未配置则 `not run`）
- [x] `backend/.venv/Scripts/python.exe -B -m compileall -q backend/app backend/scripts`
- [x] `backend/.venv/Scripts/python.exe -B backend/scripts/m7_topology_workflow_smoke.py`
- [x] `backend/.venv/Scripts/python.exe -B backend/scripts/run_sse_smoke.py`
- [x] `backend/.venv/Scripts/python.exe -B backend/scripts/run_history_smoke.py`（满足专用 PostgreSQL 安全门禁时）
- [x] 运行 M7 prompt/policy、typed registry、OpenAPI、contract matrix、docs/link/JSON 离线 audits。
- [x] `git diff --check`

阶段出口：M7 内部切片的代码、测试、协议和接力证据完整；没有把未运行的 provider/PostgreSQL/三平台项写成通过，也没有实现 M8/M9 或开始正式前端联调。

提交边界：只包含集成修复、审计、smoke、文档同步和完成记录；发现实质功能缺口时必须回到对应 Milestone 修复并重跑其 focused gate，不能把大功能补丁藏在最终审计提交。

#### Milestone 9 完成记录（2026-10-04）

- 实现：新增固定 M7 production-workflow smoke、最大 128-node 边界和专用 PostgreSQL restart 用例；审计时修复 dependency manifest 未写入 transcript、fake result closure 未引用本 run topology，以及合法大汇流的直接上游上限过小/部分 M7 collection 未显式有界三个缺口。最终审查又收紧 summary 发布边界：raw provider candidate 只作 history-only 安全投影，strict structured event 与数字事实校验通过后才原子提交 completed message/result。复审修复 provider cancelled 被误判为 run cancel 的问题：只有 run token 已置位才取消 summary；否则 summary session 以安全 `run_summary_failed` 终结，target 已完成时 run 仍 completed。summary request 的 history-only 副本先脱敏并基于安全内容重算 digest，实际发送 provider 的原请求保持不变；完整 workflow 回归保留真实非空 usage。同步移除未使用的 summary wait 占位类与 zero-model test-only publisher 接缝，由 scheduler 保持唯一 outcome 发布所有权。同步当前 OpenAPI、七类 typed registry、CI smoke 入口、README 与项目级状态；M8/M9 只获得稳定 handoff refs，没有加入 RAG、页面、URL、settings/admin、V2、feature flag、第三条 SSE 或 run-wide quota。
- 验证：Docker PostgreSQL 17 healthy；本次复审先以两个 red regression 复现 runner `CANCELLED` 与完整 workflow `workflow_failed`，修复后相关 focused/broad/final core gate 分别为 `27 passed` / `78 passed` / `31 passed`。发布前复查又以 direct transcript + 完整 workflow 两个 red regression 复现非空 usage 导致的 digest 失效，修复后 `2 passed`，相关 summary/workflow/registry 回归 `60 passed`；专用 `wateragents_test` 完整 backend `1988 passed, 1 warning`、persistence 独立门 `63 passed`、M7 workflow smoke `56 passed`、文档/唯一 composition gate `8 passed`，`compileall` 与 `git diff --check` 通过。原 M9 验收的 M7 PostgreSQL workflow/restart focused gate `2 passed`、离线出口/协议审计 `200 passed`、run SSE `completed/cancelled passed`、history smoke `history/independent_process_restart/restart_repair passed` 和 154 项 capability 审计仍保留。warning 为既有 Starlette/httpx 弃用提示；smoke 另有 permission-limited pytest cache warning，不影响测试断言。
- 风险：未获得真实 provider 计费/联网授权，因此 `requests=0 / events=0 / responses=0`，明确记为 `not run`；Ubuntu/macOS/Windows workflow 已包含 M7 smoke，但本提交的远程三平台作业未执行，GLOBAL-C08/C11 仍为 `partial`。M8 transcript RAG 与 M9 frontend/settings/admin 仍由后续阶段实现，M7 本机施工完成不等于中心外部验收完成。
- 提交：本完成记录与 M9 集成修复、E2E/审计/持久化测试和文档同步由当前本地提交保存。

## 契约覆盖矩阵（实施时填写）

| 契约 | 状态 | 计划实现位置 | 重点测试 | 证据与剩余风险 |
|---|---|---|---|---|
| GLOBAL-C01 | 本机已验证 | 当前 `backend/`、M0 archive guards | archive isolation、production import scan | M9 文档/registry/import 审计仅覆盖当前 `backend/`，未读取、导入或复用归档实现。 |
| GLOBAL-C02 | 本机已验证 | 当前唯一 V1 role/content/lifecycle/OpenAPI | no V2/read union/feature flag/new route | M7 production registry、runtime lifecycle、typed reader 与 OpenAPI 已本机验证，未新增 V2、feature flag 或 route。 |
| GLOBAL-C03 | 本机已验证 | topology、scheduler、dependency/result models | COMID identity、scope/ref closure | topology node、session target、dependency 和 final result 统一以 COMID 作为业务身份，无第二套水体 ID。 |
| GLOBAL-C04 | 本机已验证 | run context、M6 handoff、summary input | UTC instant + IANA timezone roundtrip | run 固化 IANA timezone，窗口 instant 继续使用 UTC，workflow/summary 不重算已有 session 时间语义。 |
| GLOBAL-C05 | 本机已验证 | M4 time windows、M6/M7 typed handoff | DST/wall-clock fixtures、window identity | M7 只传递 M4 已冻结的墙上时钟窗口及其 UTC identity，不按 elapsed duration 重建窗口。 |
| GLOBAL-C06 | 本机已验证 | outcome store、upstream projection、summary validator/result assembler | ref closure、scope/identity negative tests | outcome、summary 与 topology result 的 scope/identity/reference closure 已通过 application 与 PostgreSQL 回归。 |
| GLOBAL-C07 | 本机已验证 | logs、transcript、public projection、provider adapter | secret/error/provider envelope scans | 200 项离线出口门与完整回归验证 credential、private URL、raw provider/error 不进入公开 logs/API/SSE/history。 |
| GLOBAL-C08 | partial | M9 handoff、current OpenAPI/SSE | no frontend/direct route guard | M7 只留稳定读取接缝，frontend 由 M9 实现。 |
| GLOBAL-C09 | 本机已验证 | typed models、writer/reader、snapshot/history | serialization/persistence/reload | 七类 M7 内容及 lifecycle/result refs 已通过 serialization、真实 PostgreSQL reload/repair 与 snapshot/history 投影回归。 |
| GLOBAL-C10 | 本机已验证 | 本清单接力出口、项目级 docs | documented protocol audit | contract ID 双向覆盖、项目级状态与 M8/M9 可观察接力边界已通过离线审计，未提前实现后续 producer/UI。 |
| GLOBAL-C11 | partial | settings、event loop、paths、PostgreSQL、CI | Windows/local gates + three-platform CI | 并发/summary timeout 严格配置、单 worker guard、Windows + Docker PostgreSQL 全量回归与 CI M7 smoke 配置已验证；本提交的实际三平台作业仍未运行。 |
| M7-C01 | 本机已验证 | scheduler、topology workflow、session factory | chain/diamond/session timing/exact once | 同一 workflow 的 chain/diamond 已验证全部直接上游 settle 后 exact-once claim，DAG 初始化不预建 session。 |
| M7-C02 | 本机已验证 | outcome store、manifest、upstream provider | status matrix、usable-only records、invalid redaction | chain/diamond、同 run/direct-only、完整状态 manifest、usable-only records 与 invalid redaction 已通过 focused/full regression。 |
| M7-C03 | 本机已验证 | run context/node task handoff | gap_filling/prediction/mixed fixtures | M3 mode/window fixtures 与 M5 production node adapter 已验证 mode、timezone、segments、ParameterKey、window、memory/model selection 原样传递。 |
| M7-C04 | 本机已验证 | coordinator cancel、scheduler checkpoints | cancel-at-every-boundary races | cancel acceptance 先封 outcome publish/调度；dequeue、session、preflight、tool/model、summary 与 final commit races 已验证不启动下游；provider cancelled 不冒充 run cancel。 |
| M7-C05 | 本机已验证 | provider/tool late-output audit | cancelled stream/late callback tests | 晚到 completed/result/chunk 不能覆盖 committed cancelled、进入正常正文、发布 upstream/result 或触发 summary；仅保留脱敏 audit 与必要补写。 |
| M7-C06 | 本机已验证 | workflow quiescence、live cleanup | cancel_accepted vs quiesced tests | cancel_accepted 与 workflow_quiesced 独立；run stream、cleanup 与后台生命周期等待实际 quiesced。 |
| M7-C07 | 本机已验证 | ready queue、limiter、M4 topology snapshot | concurrency/range/per-session budget fixtures | 全局峰值 1/4/32、两个 run 共享 limiter、128-node 范围与 per-session failure 已验证，无第二套 run-wide quota。 |
| M7-C08 | 本机已验证 | topology result assembler、run lifecycle/history | target/partial/failure/ref tests | topology result、run completed lifecycle 与轻量 summary/ref 在同一必需提交组保存，partial/failure/history/reload 已验证。 |
| M7-C09 | 本机已验证 | node launch policy、local preflight、zero-model assembler | station-backed/stationless call counts | stationless 全不可用形成正式 insufficient result，0 planning/final/optional 且不传播 evidence。 |
| M7-C10 | 本机已验证 | dependency prompt block、M6 validator/registry | source roles、directional/numeric boundary | source role、usable-only result ref、invalid redaction、mode/window 与 deterministic numeric boundary 已通过 focused/full regression。 |
| M7-C11 | 本机已验证 | run summary input/prompt/runner/transcript | all-nodes input、same model、highlights refs | M6 runner 与 M7 transcript/lifecycle 集成已验证 all-node input、same-model/no-tools、strict structured event、exact numeric text/refs 与 result reload。 |
| M7-C12 | 本机已验证 | summary failure mapping、public projection/logging | no-fallback、provider cancelled、non-null usage/digest、safe error、run completed fixture | summary request/response 为 history-only 安全投影；安全 request 副本脱敏后重算 digest，provider 原请求不变；validation 前无公开 summary message，失败无 fallback；provider cancelled 且 run token 未取消时 summary failed、target/waterbody results 保持且 run completed。 |
| M7-A01 | 本机已验证 | production workflow fake topology | 3+ nodes、event/session order、exact once | chain/diamond production workflow harness 已验证 phase 顺序、session 延迟创建、每 node 一次与 outcome 全收敛。 |
| M7-A02 | 本机已验证 | manifest/upstream bundle | status/evidence/ref/merge fixtures | M2 provider/M3 launch 与 M5 完整 workflow orchestration 已验证 direct-upstream publish、invalid+usable merge、all-insufficient 零模型。 |
| M7-A03 | 本机已验证 | cancel coordinator/scheduler/quiescence | all checkpoints、one run cancel | 单 run 取消后不新建 session、不调用 tool/model、不发布 outcome/result/summary，并最终 quiesced。 |
| M7-A04 | 本机已验证 | transcript late audit/session lifecycle | provider late chunks、completed-session preservation | cancel 前 completed session 保持 completed，in-flight session 取消；迟到内容仅可经脱敏 audit 读取。 |
| M7-A05 | 本机已验证 | launch gate/zero-model/M6 handoff | stationless 0/0/0、station-backed indirect seed | 两条路径复用 canonical M6 result/session 提交接缝，stationless 发布 insufficient 且无 usable records。 |
| M7-A06 | 本机已验证 | limiter/topology snapshot/settings | peak in-flight、max topology、no run quota | 独立节点实际重叠且峰值不超设置，最大 topology 仅执行 snapshot 内 128 个 active COMID，配置 0/33 失败。 |
| M7-A07 | 本机已验证 | summarizer/input/validator/failure projection | same model、all cards、special values、non-null usage、provider cancelled | M6 summary 校验与 M7 safe failure/result projection 已通过 focused、完整 workflow 与 backend 回归；真实 usage 可安全持久化且不改变 provider request，provider cancelled 不改变 run cancellation authority，且无 fallback。 |

状态只允许：`pending`、`partial`、`本机已验证`、`外部已验证`。不能只因代码、fixture 或文档存在填写已验证；本机、专用 PostgreSQL、真实 provider 和三平台 CI 证据必须分开记录。

## 实际完成记录格式

每个 Milestone 完成时，在该节“提交边界”后追加记录；不要新建独立 completion Markdown：

```text
#### Milestone N 完成记录（YYYY-MM-DD）

- 实现：关键 production 契约、改动路径与明确未实现边界。
- 验证：实际命令、环境和 pass/fail/skip/not run 结果；失败后的修复与重跑结果。
- 风险：外部未运行项、partial 项和后续接力限制。
- 提交：commit SHA；如未提交则写未提交，不能填计划 SHA。
```

不得把计划命令写成已执行结果，不得复制 M6 或其他阶段的 pass count，不得把 `skipped`、0 requests、HTTP 200、收到 `stream_end`、进程启动或 exit code 0 单独当作业务通过证据。

## M8 / M9 接力出口

M7 完成后只交付以下稳定能力：

- M8：waterbody/summarizer sessions 的现有 typed transcript、持久化 result refs、明确 role/COMID scope 和 `TranscriptEvidencePort` 消费接缝。M8 自己建立 index/search producer，并继续检查 completed/persistence/memory-mode/safe projection；M7 不把历史 transcript 注入当前 dependency evidence。
- M9：run snapshot/detail/history 中的 topology node 状态、waterbody/session/result refs、target main conclusion、partial coverage、summary result/highlights/source refs 和安全 failure。M9 自己实现页面、地图连线、设置/admin、特殊值点击跳转及内部 URL；URL 从稳定 refs 确定性生成，不让 Agent 输出。

M7 不因后续阶段尚未实现而增加 fake production producer、临时 unsupported product response、feature flag、V2、兼容层、第三条 SSE、独立 Agent cancel route 或前端专用总结。后续阶段应直接扩展同一最终系统和当前唯一 V1。
