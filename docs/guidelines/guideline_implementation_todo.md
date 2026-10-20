# Guideline Knowledge Implementation TODO

日期：2026-10-20（Asia/Shanghai）

状态：**Milestone 0–3 已完成，其余阶段待实施**。本文是后续 coding agent 的唯一实施进度清单。已完成源码接缝盘点、领域/ports、PostgreSQL 事务、内置资源导入与 family 覆盖基础，不代表 guideline 集成功能、内容审核、真实供应商调用或实测验收已经完成。

规格依据：[技术设计](guideline_knowledge_design.md)；产品决定与调查证据：[讨论记录 D01–D14](guideline_design_discussion.md)。阅读顺序为产品契约 → 当前源码 → 本清单对应阶段。本文定义施工、测试与交付要求，字段和状态语义以技术设计为准；发现冲突先在同一变更中修正文档一致性，不能静默选择更省事的实现。

## 执行规则

1. 先执行 Milestone 0，重新核对当前 HEAD、工作树和调用链。设计调查基线为 `codex/runtime-smoke-fixes-20261014` / `0f2bfd4d79b80d80dfcaaec03d8c8c6ea6879b41`，不能假定后续实现时仍未变化。
2. Milestone 是同一最终功能的内部实施切片，阶段提交用于审查，不形成多套并行生产路径。用户只授权某阶段时完成该阶段；授权完整实现时继续完成全部阶段。
3. 自主处理内部函数拆分、fixture 命名等常规选择；不重新询问已确认的产品边界。若需调整字段、不变量或产品行为，先写明具体证据并同步主设计。
4. 新生产代码只接入当前 `backend/app`、`frontend/src` 的既有 workflow、writer/reader、模型供应商配置和两层 SSE。不得从 `backend_bk`、`frontend_bk` 或运行归档复制另一套业务实现。
5. 实现与必要测试同步完成；每阶段先做针对性验证，最终再做整体回归。不要反复跑已通过的大套件，除非后续改动影响它或仍有未解释失败。
6. 勾选表示对应代码、测试和证据均已具备；列出命令、写完接口或使用 fake 不代表真实运行完成。未运行项保留未勾选，并写 `not run` 及原因。
7. 在本文件相应阶段更新完成记录与实际 commit，不另建阶段状态 Markdown。规格变更更新主设计；产品决定变更更新讨论记录；不要把三个文档当作重复 TODO。
8. 遵守根目录 [AGENT.md](../../AGENT.md)：新测试源码与必要输入 fixture 可入 Git，新日志、模型输出、截图和临时结果留在 task-owned 未跟踪目录。保留已有 `runtime-test-1019/`，不向其中追加本功能结果，不清理用户缓存或已有临时目录。
9. 内容审核是已确认的产品要求：agent 可准备真实可审查草稿，不能替制作者伪造批准。内容待审核时可继续不依赖批准内容的实现和测试；未批准草稿不能进入生产检索库。
10. 不修改业务库来跑测试，不把 `WATERAGENTS_TEST_DB_NAME` 写进 `.env`，不打印密钥。测试、真实 embedding、真实分析模型、浏览器、hosted CI 分别记证据。

## 阶段导航与依赖

| 阶段 | 主要产物 | 前置 | 技术设计章节 |
| --- | --- | --- | --- |
| Milestone 0 | 当前基线、消费者清单、回归与边界 | 无 | §1–3 |
| Milestone 1 | 内容、来源、canonical、profile、citation 与 ports | 0 | §4–5、§7–9 |
| Milestone 2 | 数据库迁移、repository、CAS 与 operation receipts | 1 | §6 |
| Milestone 3 | 内置资源导入、有效目录与本地覆盖 | 2 | §4、§6 |
| Milestone 4 | 草稿、人工发布与本地管理 API | 2–3 | §5、§10 |
| Milestone 5 | 独立 embedding 契约与公共传输复用 | 1–2 | §7、§12 |
| Milestone 6 | 持久化准备任务、激活与 generation 切换 | 3–5 | §6–7、§12 |
| Milestone 7 | Run pin、session 检索、稳定排序与缓存 | 6 | §6–7 |
| Milestone 8 | 模型上下文、数值路径与引用解析 | 7 | §8 |
| Milestone 9 | Transcript、结果引用、历史闭包与 M8 隔离 | 8 | §8–9、§12 |
| Milestone 10 | 前端管理、引用查看与浏览器链路 | 4、9 | §10–11 |
| Milestone 11 | pH 内容实际审核、检索质量与实测配对 | 6–10 | §13–14 |
| Milestone 12 | 完整回归、契约核验与交付 | 0–11 | §14–16 |

Milestone 8 的端到端接入必须包含 Milestone 9 的合法记录链；不得先上线只注入知识、无法记录实际提供内容的临时版本。Milestone 11 的资料可提前撰写和送审，但正式检索与对照运行依赖上述链路完成。

## 必守契约与覆盖责任

| ID | 不变量 | 主要负责阶段 | 最终证据 |
| --- | --- | --- | --- |
| GK-C01 | 说明书是可选参考；缺资料、未命中或不可用仍依原契约分析 | 7–8 | 未采用、失败、缺资料路径 |
| GK-C02 | 不替代观测、measurement anchors、scope、数值资格或 risk 判断 | 8–9 | 数值/证据回归及 lineage 检查 |
| GK-C03 | bundled/custom/override 权威分离；本地内容不自动公开 | 3、10 | 包资源与本地编辑/更新测试 |
| GK-C04 | 真实人工检查绑定已保存 digest；agent 草稿不自动批准 | 1、4、11 | 发布测试与实际批准版本 |
| GK-C05 | 完整解释主题、来源性质、字段限制及 canonical digest 一致 | 1 | domain 边界与跨换行测试 |
| GK-C06 | 原文与安全 model view 分离；URLs/凭据不进入受限输入 | 1、8、10 | projection 与渲染安全测试 |
| GK-C07 | 每 family/revision 至多一有效版；停用不暗中回退 | 2–3、6 | 真实 PostgreSQL 目录约束 |
| GK-C08 | mutation receipt 先于 CAS；相同 key 重放、不同 body 冲突 | 2、4、10 | 丢响应与重复请求测试 |
| GK-C09 | 编辑 revision 与激活 epoch 分离；旧 worker 不逆转后续操作 | 2、6 | 屏障控制的真实竞态 |
| GK-C10 | guideline profile/canonical 独立，保留历史 case 输入校验 | 1、5 | adapter/port/M8 回归 |
| GK-C11 | generation 完整后原子切换；不混 profile/维度 | 5–6 | 重建与发布并发测试 |
| GK-C12 | 全 run 固定 catalog revision/generation；更新只影响新 run | 7 | 跨 session pin 测试 |
| GK-C13 | metadata + exact cosine，去重、稳定排序、完整主题和有界输入 | 7 | 检索标注集与预算测试 |
| GK-C14 | 首次实际模型路径前加载；零模型/取消无新增 provider 调用 | 7–8 | 实际 dispatch 计数 |
| GK-C15 | advisory block 不提升指令权威；最终序列化请求统一 fit | 8 | 实际 payload、repair 与裁剪测试 |
| GK-C16 | 仅有效 alias 成为 usage；正式结果只映射实际采用的 lineage | 8–9 | 候选丢弃与目标绑定测试 |
| GK-C17 | provided/used 分离；历史依已保存快照，引用闭包无循环 | 9–10 | 实时/读回/更新后查看 |
| GK-C18 | 本地管理 API 延续 Origin/Host/JSON、safe error 与 no-store | 4 | API/OpenAPI/边界测试 |
| GK-C19 | 不确定响应保留原 key 与脏编辑；旧 ACK 不清新编辑 | 10 | editor 实际 interleaving |
| GK-C20 | M8/stateless 独立；guideline 不自动变成 historical evidence | 5、9 | M8 eligibility/source closure 回归 |
| GK-C21 | worker 有限重试/租约；run 与后台分别持有正确凭据生命周期 | 5–7 | 重启/取消/密钥替换测试 |
| GK-C22 | 两层 SSE、单一 workflow/registry；旧记录 digest/读语义保持 | 0、9 | 架构与历史 fixture 回归 |
| GK-C23 | 小样本对照隔离实测答案；不虚构数据或精度提升 | 11 | 3–5 例输入核验与成对结果 |
| GK-C24 | 结果、fixture、批准、真实供应商、CI 的验证等级分别报告 | 0、11–12 | 本文完成记录与交付说明 |

## Milestone 0：基线与完整接缝盘点

### Work

- [x] 记录实际 branch、HEAD、merge-base、已有修改、Python/Node/npm/uv 版本及当前 migration head；明确本次拥有的文件，保留无关修改。
- [x] 复读 `AGENT.md`、主设计与讨论 D01–D14；确认本文与当前源码一致，记录真实偏差及修正，不能把旧设计中的禁止新路由扩大解释为禁止本次已授权管理 API。
- [x] 从 run/query/topology → session/planning → numeric estimation → batch/synthesis/repair → result assembler，列出全部请求入口、零模型出口、取消检查和上下文传参点。
- [x] 盘点 typed registry、reference extractors、public projection、SSE、history reader、M8 source closure、前端 `visibleRecords`/`referencedItems` 的全部消费者。
- [x] 盘点现有 embedding canonical 校验、model catalog、RunProviderBundle、credential lease、数据库迁移及 LocalMutationGuard 接缝；区分可复用传输与 case 专用语义。
- [x] 在本节完成记录补充影响表和已有测试基线；确认生产路径不依赖归档、测试 router 或 fake-provider composition。

### Tests

- [x] 运行现有 architecture、OpenAPI、numeric estimation、transcript registry/history 的针对性基线，记录已有失败与环境原因。
- [x] 检查现有 PostgreSQL 测试隔离与 migration 启动方式；只用专用测试库，不为通过测试删安全门禁。
- [x] 确认生产 imports、API 路由、SSE 数量的既有守卫能覆盖新入口；仅对缺失的实质边界补守卫，不为盘点文档写镜像测试。

### Verify

先执行附录 A 的测试 shell 初始化；下文 `Invoke-GuidelinePytest` 是文档中的本地命令助手，不是待新增应用接口。

```powershell
Invoke-GuidelinePytest -Stage gk-m0 -Paths @('backend/tests/architecture', 'backend/tests/api/test_openapi_contract.py', 'backend/tests/api/test_m9_target_openapi.py', 'backend/tests/application/reasoning/test_numeric_estimation.py', 'backend/tests/application/test_transcript_registry.py', 'backend/tests/api/test_analysis_history.py')
git diff --check
```

阶段出口：调用链、原子更新面和基线可复现；已知限制明确，后续不靠绕过历史契约或环境门禁开始实现。

提交边界：本清单影响表、必要的边界守卫及对应测试；不提前改业务数值、provider roster 或历史语义。

完成记录（2026-10-20）：`complete`。

- 基线：开始时为 `codex/runtime-smoke-fixes-20261014` / `0f2bfd4d79b80d80dfcaaec03d8c8c6ea6879b41`；fetch 后由 `origin/main` 新建 `codex/guidelines-m0`，起点 HEAD 与 merge-base 均为 `6cedea4f1f21339dcd87ef5a3bbd2d49079ef799`。两基线的 tracked tree 无差异。没有创建 worktree。
- 已有修改：tracked 文件无修改，`docs/guidelines/` 三份文档与 `.serena/` 未跟踪。本阶段仅纳入这三份指南文档；保留 `.serena/`、运行归档和已有临时目录。生产代码、测试源码、模型配置与数值/历史语义未改。
- 环境：项目 venv Python `3.12.14`，Node `v24.21.0`，npm `11.19.0`，`py -m uv --version` 为 `0.13.1`；`uv` 不在 PATH。Alembic 唯一代码 head 为 `20261020_01`（前驱 `20261008_03`）。Docker Compose PostgreSQL healthy；仅将独立 `wateragents_test` 从 `20261008_01` 升级并检查到 `20261020_01`。
- 本阶段 commit：`18a9a2b7e7878d4937b4bf351382cfe5999bd97f`（`docs(guidelines): complete milestone 0 baseline`）；随后仅补记此 hash 与核对措辞。

影响表（以下是后续阶段须一并处理的消费者，不表示已实现新功能；后端路径以 `backend/app/` 为根，前端路径以 `frontend/src/` 为根，同格省略重复目录）：

| 接缝 / 现有路径 | 当前调用、出口与后续改动责任 |
| --- | --- |
| Run 入口：`api/http/analysis_runs.py`、`application/analysis_runs/coordinator.py`、`infrastructure/runtime/provider_bundle.py` | `POST /api/analysis/runs` → coordinator creation owner → run-owned bundle workflow。`main.py` 生产装配使用该 factory，显式注入 workflow 的分支属于测试 composition；不能在 HTTP 另起检索/分析 workflow。 |
| Query / topology：`application/query_understanding/parser_workflow.py`、`parser_prompt.py`、`application/analysis_runs/query_topology_workflow.py` | parser 普通请求与至多一次 schema repair → context resolver → 保存 `query_understood` → topology build/save → execution stage。各边界执行 cancellation checkpoint，外部等待使用 `await_with_cancellation`；parser 不注入 guideline，也不获得新工具权限。run catalog pin 放在拓扑已保存、waterbody 执行前。 |
| Node / session：`application/analysis_runs/topology_driven_workflow.py`、`topology_scheduler.py`、`node_launch.py`、`zero_model_result.py` | execution stage → scheduler → prepare session/manifest/scope/task → acquisition → `NodeLaunchPolicy`。`RUN_AGENT` 才进入 M6；`DETERMINISTIC_INSUFFICIENT` 直接装配零 waterbody-model 调用结果；取消/invalid 分支不启动 runner。这里的零模型指 waterbody session，前面的 parser 和后面的 run summary 是独立调用。 |
| Session / planning：`infrastructure/runtime/m6.py`、`application/reasoning/runner.py`、`planning_loop.py` | `M6RunnerService.build_runner` 共享 session recording port；runner → planning loop → seal → skeleton/context → batch planner。planning 在取消、deadline、tool budget、无合法 action、`stop_before_model` 等条件下停止或跳过调用。首个真实 planning 请求前惰性加载；不可在建 session/runner 时无条件 embedding。 |
| Numeric：`application/reasoning/numeric_estimation.py`、`application/evidence_tools/{acquisition,numeric_estimation,numeric_dependency}.py` | planning 收敛后，只要 `numeric_estimation_allowed` 且未到 deadline，仍会 `resolve_numeric` → `ModelNumericEstimator`；planning 调用数为零不保证 numeric 为零。其请求自行构造，不经过 `PromptBuilder`，须单独传 context、schema/alias、usage 与最终 token fit。dispatch lock 内再检查 cancellation、deadline、调用预算和 circuit；点估计仍要求原始同指标 anchors。 |
| Batch / synthesis / repair：`application/reasoning/batch_planner.py`、`prompt_builder.py`、`validation_repair.py`、`draft_models.py` | 支持的 cells 分批 → `ValidationExchange.run_batch` → `run_synthesis`；各自最多一次同模型 repair。无 batches 跳过这两类模型调用，仍可装配固定/unsupported 结果。现有 size estimator 用完整 serialized request；新增 advisory 必须进入测量，并沿 prompt inputs、working context、batch/synthesis plans 和 repair 同步传递。 |
| Dispatch / 保存：`application/reasoning/model_protocol.py`、`transcript_store.py`、`infrastructure/runtime/m6.py` | planning、numeric、batch、repair 最终经 `consume_waterbody_model_stream` → recording port → router → adapter；consumer 在调用前、流内和结束检查取消。recording port 先保存 request 再进入 delegate；仅构建/保存 request 还不是实际提交，delivery 必须绑定真实 port 调用及最终 payload，保留失败/取消状态。 |
| 水体与 run 结果：`application/reasoning/draft_validator.py`、`result_assembler.py`、`application/analysis_runs/topology_result_assembler.py`、`application/run_summary/{input_builder,prompt_builder,runner,transcript_store,stage}.py` | validator → 正式水体 result → coordinator committed outcome → topology aggregation → run summary 普通/至多一次 repair → complete run。runner 在 batch 间、synthesis 和装配前后 checkpoint；summary consumer 独立检查取消，请求向 adapter 传递 deadline。numeric usage 沿实际 `model_operation_id`/选用 evidence lineage 映射，summary 只沿已保存引用传递，不重新全库检索。 |
| Typed registry / writer：`application/transcripts/{registry,models,buffer}.py`、`application/analysis_runs/{coordinator,writer}.py`、`infrastructure/persistence/{schema,store}.py` | registry 同时负责 encode/parse/recheck、metadata、visibility、message/public projections、references；`_inference_result_references` 提取 detail 及每 step refs，另有 dependency/summary/topology extractors。coordinator 与 writer 校验同 run 引用、等待目标 finalize、按依赖组持久化。五类 guideline records 须一起注册并默认 RAG-ineligible；usage → delivery → retrieval/context/request，result 只指向先写好的 usage，避免循环。旧 schema/digest 不重写。 |
| Read / SSE / API：`application/transcripts/{reader,read_models,stream_models}.py`、`api/http/analysis_runs.py`、`api/sse/{response,encoder}.py`、`api/schemas/analysis_runs.py`、`main.py` | reader 的 run/session/full-content/registered read 共用 registry；`_reference_closure`、`_detail_missing_references` 与 public projection 均是消费者。run snapshot 与 session process 两条 SSE 复用 item projection，兼顾 inline/大正文/history。新 typed 内容须连同 references、公开正文和 OpenAPI source export 更新，保持旧历史只读语义。 |
| 下游 evidence / M8：`application/evidence_tools/upstream_projection.py`、`application/transcript_memory/{source_reader,source_closure,eligibility,projection_builder}.py` | upstream reader 对已保存 result/bundle 与 session closure 有自己的检查，不能假定只改通用 reader 即可。M8 source reader 从 registry refs 建 confirmed references/eligible fragments，再做 source closure/projection；新 usage 不成为 observation、measurement anchor 或 historical case 正文，旧 eligibility 不因有知识引用增强。 |
| Embedding / 凭据：`application/transcript_memory/embedding_contract.py`、`domain/memory_rag/embedding_profile.py`、`domain/model_catalog_snapshot.py`、`infrastructure/config/model_catalog.py`、`infrastructure/adapters/openai_embeddings.py`、`infrastructure/runtime/{provider_bundle,m8}.py`、`application/settings_admin/credential_service.py` | 旧 adapter 先验证 `CanonicalCaseQueryV1` 精确 bytes/versions，之后才发送 HTTP；可复用 HTTP、取消、model/count/order/dimensions/finite/norm 检查，不能复用 case 身份。catalog embedding role 可提供配置，guideline profile 独立。run bundle 在创建前固定 leases，`bind_run/aclose` 绑定/释放 run embedding；后台 `AttemptEmbeddingFactory` 每 attempt 取 lease。新 worker 不依赖 M8 memory/stateless 开关。 |
| SQL / 本地管理：`infrastructure/persistence/{database,cli,migrations,schema}.py`、`api/http/{local_mutation,request_context,errors}.py`、`main.py` | migration 显式 upgrade，startup/check 只验证。新 migration 从实际 head 续写；七表 catalog/document/operation 锁序、receipt-before-CAS、editing revision/activation epoch/generation 原子切换在阶段 2–6 实现。全 `/api/` mutation 受全局 Host/Origin/JSON guard；新管理 API 延用 request ID/safe errors，并显式 no-store。旧“禁止新路由”不禁止本设计授权的 `/api/guidelines`。 |
| 前端：`app/App.tsx`、`features/analysis/{state,DetailPanel,models}.ts*`、`api/{http,sse,generated}.ts` | App 在 live 接 session SSE、history 加载 transcript/referenced_items，reducer 保留 items。`DetailPanel.visibleRecords` 是显式可见清单；`referencedItems` 当前整组显示为 Historical context。须增加 guideline cards/只读快照 viewer、按 content type 分类、结果 usage targets 与管理导航；由 API 重新生成类型，旧结果不补虚构引用。 |

核对结论：设计主契约与当前源码一致。补全主设计 §2 的 upstream closure/run-summary 消费者；当前 numeric 只有 192,000 bytes 输入门禁，不能把它误称为已有的统一 token fit（阶段 8 负责补齐）。未发现需要在 M0 新增的生产边界守卫：现有 archive/import/layer/composition guards 扫描整个生产树，OpenAPI allowlist 与两条 SSE 守卫覆盖新增路由的审查入口。阶段 4 添加已授权管理路由时须同步 allowlist/安全测试；保留直接调用模型/工具、归档和测试路由禁止项。

实际验证（根目录 PowerShell；`$p = (Resolve-Path 'backend/.venv/Scripts/python.exe').Path`，测试进程设置 `WATERAGENTS_TEST_DB_NAME=wateragents_test`；未写 `.env`）：

| 命令 / 范围 | 实际结果 |
| --- | --- |
| 本节 Verify 的六组路径，经 `& $p -m dotenv -f backend/.env run --no-override -- $p -m pytest <paths> -q -p no:cacheprovider --tb=short --basetemp .tmp/guidelines-m0-20261020/baseline-permitted --junitxml .tmp/guidelines-m0-20261020/baseline.xml` | **255 passed**，21.96s；architecture/OpenAPI/numeric/registry/history 指定基线通过。 |
| 同一 runner，路径为 `backend/tests/persistence/` 下的 `test_environment_contract.py`、`test_database_config.py`、`test_schema_contract.py`、`test_rag_migration.py`、`test_postgres_persistence.py`、`test_numeric_dependency_persistence.py`、`test_m6_reasoning_persistence.py`；temp/report 分别为 `postgres-permitted` / `postgres.xml` | **65 passed**，14.01s；包括真实 PostgreSQL fresh/升级 migration、transcript/numeric 持久化与测试库隔离门禁。测试只创建/清理自有随机 schema。 |
| 同一 runner，`backend/tests/application/reasoning/{test_planning_loop,test_runner,test_validation_repair,test_batch_planner}.py`、`backend/tests/application/transcript_memory/test_source_closure.py`、`backend/tests/infrastructure/embeddings`、`backend/tests/infrastructure/test_m9_provider_bundle.py`、`backend/tests/api/test_m9_settings.py`；temp/report 为 `seams-permitted` / `seams.xml` | **137 passed, 1 failed**，26.72s。失败为既有 lease test 等待 mock started 事件 2s 超时；单项重放定位到 dotenv 注入本机自定义 base URL，mock 断言的 `/v1/embeddings` 与实际 `/embeddings` 不同，尚未进入 lease 竞态。 |
| `& $p -m pytest backend/tests/infrastructure/test_m9_provider_bundle.py -q -p no:cacheprovider --tb=short --basetemp .tmp/guidelines-m0-20261020/lease-isolated --junitxml .tmp/guidelines-m0-20261020/lease-isolated.xml`（离线文件不加载部署 dotenv） | **2 passed**，1.13s；原生产 lease 行为通过。保留上行失败证据，不将部署配置耦合误报为 provider 回归或全套通过，不延长超时。 |
| `backend/`：`docker compose --env-file .env -f compose.yaml up -d --wait`；以 venv dotenv runner 执行 `python -m app.infrastructure.persistence.cli upgrade --test-database` / `check --test-database` | healthy；专用测试库 current at `20261020_01`。未对业务库执行 upgrade 或测试。 |
| 三份指南 Markdown 的相对链接、代码围栏、行尾空白与阶段勾选核对；提交前 `git diff --cached --check` | 50 个本地链接存在；M0 共 9 项已勾选，其余阶段无勾选；文档格式检查通过。 |

环境处理：最初 `py -m uv run --project backend --locked ...` 因 uv cache ACL 拒绝启动，改用现有项目 venv；首次 pytest 因 task parent 缺失报 `222 passed, 33 errors`，补父目录后仍遇 basetemp 创建/清理 `WinError 5`。上述通过结果来自获准权限和全新 task-owned temp；不删除历史目录、不关闭隔离门禁、不修改产品代码。附录 A 已补齐父目录创建。XML 等结果只留本机，不纳入提交。

验证等级：既有逻辑/MockTransport/fake composition + **真实 PostgreSQL**。browser、real-embedding、real-analysis、hosted-CI、专业资料批准与 pH 实测配对均为 **not run**（本阶段不涉及）。未完成：Milestone 1–12 与最终功能验收；本阶段出口已满足，无需以新增功能或真实模型调用替代基线。

## Milestone 1：内容、身份、canonical 与 ports

### Work

- [x] 定义 guideline domain：origin、source_kind、family/version identity、draft/published 内容、source refs、review、operation、catalog/profile snapshots 和状态；pH 仅是内容实例。
- [x] 按主设计 §5 实现字段、集合和 UTF-8 上限；draft 允许暂缺必填字段，发布独立校验。来源为 `reference_derived` 必须有出处，`user_authored` 可无出处。
- [x] 实现统一 canonicalization：NFC、LF、稳定 JSON key order、metadata 去重/排序，保留正文语义/大小写；manifest 与数据库使用同一 digest 实现。
- [x] 定义确定性的 `GuidelineModelViewV1`：完整解释正文、来源性质、source IDs；保存原文与 source metadata，将正文 URL/链接安全转成可追溯 source refs，不能放宽旧 summary 校验。
- [x] 定义 repository、bundled source、guideline embedding ports 及必要 DTO；domain/ports 不导入 SQLAlchemy、HTTP、FastAPI 或 infrastructure。
- [x] 定义独立 document/query canonical inputs 与 `GuidelineEmbeddingProfileV1`，包含 provider/model/dimensions/metric、模板/契约版本及角色指令。
- [x] 定义 `GuidelineCitationV1` 与 operation/result usage target 类型；alias 不是持久身份，target 使用已有 task/cell/window、正式 step 零基 index 或固定 `overall`。

### Tests

- [x] 覆盖发布 6,000 bytes、草稿 24,000 bytes、中文多字节、字符上限、空字段、source refs/URL 长度与已登记 parameter keys 的边界。
- [x] 覆盖 metadata 重排去重、NFC 和 CRLF/LF digest 等价；正文内容变化必须改变 digest，不能以过度归一化抹掉条件或大小写含义。
- [x] 覆盖 URL/Markdown 转换后仍保留因果条件与例外；无法安全投影返回字段错误，不悄悄删正文；非 HTTP(S) source URL 拒绝。
- [x] 覆盖无出处自编合法、资料整理无出处拒绝、本地修改 provenance 不自动获得专业审核资格；测试中的批准明确标为 fixture。
- [x] 覆盖 port/layer purity；新 profile ID 不等于历史 case profile ID，旧 canonical 输入约束仍成立。

### Verify

以下测试路径已由本阶段创建；实际执行记录见本节完成记录。

```powershell
Invoke-GuidelinePytest -Stage gk-m1 -Paths @('backend/tests/domain/test_guidelines.py', 'backend/tests/ports/test_guideline_ports.py', 'backend/tests/architecture/test_guideline_boundaries.py')
git diff --check
```

阶段出口：三种 origin、两种 source_kind、未完整草稿、不可变批准版本及独立向量契约可表达；不借用 observation 或 historical case 身份。

提交边界：domain、ports、canonical/model-view 纯逻辑及针对性测试；不包含自动批准内容或新业务算法。

完成记录（2026-10-20）：`complete`。

- 基线：继续使用 `codex/guidelines-m0`，起始 HEAD `afc432a4dd8b4d87f5af42faa303fe925a6c49f3`；tracked 工作树干净。本阶段实现 commit：`6f3a361477da095b685f03ae8df0174e12fcf573`（`feat(guidelines): add milestone 1 domain and ports`），随后仅补记此 hash；保留 `.serena/` 与已有临时产物。
- 实现：`backend/app/domain/guidelines/` 定义三种 origin、两种来源、稳定 bundled family/本地 UUIDv4 身份、不完整草稿与来源、独立发布校验、不可变审核版本、operation/receipt/claim 与 catalog/generation/profile 快照、canonical document/query、delivery citation 和五类 usage targets。遵守每文件一个公开领域类及根包导出约定；`backend/app/ports/guidelines.py` 提供 typed repository、bundled source、embedding 协议，复用中性的 cancellation/vector 类型。
- 不变量：正文按 UTF-8 限长；所有文本 NFC/LF、metadata 稳定去重排序，完整草稿与发布内容 digest 一致；发布入口要求显式传入参数 registry，检查 primary/related keys。model view 仅替换已登记 URL 并保留解释文字，未知/不安全/过长内容返回字段错误。review 绑定本篇 ID/digest，local override 不能复用 base review；保存 receipt 保留原 accepted revision/digest，变化的 worker 状态不改写 receipt。未实现 SQL 事务或发布 worker，未伪造实际审核。
- 规格细化已同步主设计 §5/§7：source/review 字段上限、model-view 12,000 bytes、document input 24,000 bytes、query facts 上限与显式角色指令；超限拒绝，不截断。旧 summary、case embedding、数值与历史语义未改。
- 测试源码：`backend/tests/domain/test_guidelines.py`、`backend/tests/ports/test_guideline_ports.py`、`backend/tests/architecture/test_guideline_boundaries.py`，公共 synthetic fixture 在 `backend/tests/guideline_support.py`。覆盖 ASCII/中文/emoji 精确边界、空草稿/部分来源、NFC/换行/重排、不安全或未登记链接、中英文条件保留、审核/digest/覆盖身份、profile/模板/角色隔离、receipt/claim、citation/target 和旧 summary/case 校验。
- 实际命令（仓库根目录，项目 venv）：`backend/.venv/Scripts/python.exe -m pytest <paths> -q -p no:cacheprovider --tb=short --basetemp .tmp/guidelines-m1-20261020/<stage> --junitxml .tmp/guidelines-m1-20261020/<stage>.xml`。使用 M0 已确认的获准权限处理 Windows 临时目录；结果仅留 task-owned 本地目录。

| Paths / stage | 实际结果 |
| --- | --- |
| 本节 Verify 的三份新增测试；`delivery` | **99 passed**，0.37s；最终代码的完整 M1 针对性检查。 |
| `backend/tests/domain`、`backend/tests/ports`、`backend/tests/architecture`、`backend/tests/infrastructure/embeddings`、`backend/tests/application/transcript_memory/test_source_closure.py`、`backend/tests/application/test_transcript_registry.py`、`backend/tests/application/reasoning/test_numeric_estimation.py`、`backend/tests/api/test_openapi_contract.py`、`backend/tests/api/test_m9_target_openapi.py`；`regression` | **814 passed**，39.50s；随后仅细化新模型的 URL/文本校验与 receipt digest，再以新增测试复核最终代码。 |

- 验证等级：纯领域 / 测试 fake；无真实 embedding 或分析 provider dispatch。PostgreSQL、新 API/浏览器、hosted CI、专业资料批准及 pH 实测：**not run**（本阶段不涉及；数据库迁移/事务从 Milestone 2 开始）。
- 阶段出口：三类内容身份、未完整草稿、不可变批准版本、独立向量契约与可追溯引用类型均可表达；现有全局层级守卫及新 guideline/case/observation 隔离守卫通过。Milestone 2–12 保持未完成。

## Milestone 2：数据库、事务与幂等基础

### Work

- [x] 增加 Alembic migration 和 schema check，创建主设计 §6 的七张 `guideline_*` 表；编号基于实际 Alembic head，不做应用启动 DDL。
- [x] 实现 document/draft、immutable versions、catalog intervals、generation/profile、embedding rows 和 operations repository；索引与版本内容权威分开。
- [x] 用数据库约束与锁保证 override/base 唯一、generation/version 唯一、每 root/revision 有效区间不重叠、guideline namespace 至多一 active/一 building generation。
- [x] 实现 mutation UUIDv4 key、canonical fingerprint、immutable accepted response receipt；receipt lookup 在 revision CAS 之前，同 key/body 返回原结果，同 key 不同 body 返回冲突。
- [x] 明确所有需要多资源锁的事务遵循 catalog → document → operation/version 顺序；不在数据库锁内等待供应商。
- [x] 分离 editing revision 与 root activation_epoch；编辑新草稿不改变批准版本，停用/恢复/新发布令过时激活意图失效。
- [x] 实现 operation claim lease、attempt、safe error、retry/superseded 和启动恢复所需持久状态；失败保留旧有效版，首版不自动清除旧版本/generation。

### Tests

- [x] 在真实 PostgreSQL 验证迁移、schema check、唯一/FK/区间约束及事务 rollback；不以 SQLite/fake repository 代替这些结论。
- [x] 用两连接与确定性屏障重现并发创建 override、同 revision 保存、同 key 重放和 catalog 激活争用；断言只有一个合法结果。
- [x] 模拟事务已提交但响应丢失，重试旧 revision/同 key 仍返回原 accepted response；key/body 改变则冲突且不创建额外版本。
- [x] 模拟 claim 后重启、lease 过期与第二 worker claim，确保旧 attempt 不能重复完成或覆盖新状态。

### Verify

```powershell
Invoke-GuidelinePytest -Stage gk-m2 -Paths @('backend/tests/persistence/test_guidelines.py', 'backend/tests/architecture/test_guideline_boundaries.py')
git diff --check
```

阶段出口：migration、CAS、receipt、锁序和真实并发不变量具备证据；publication 上层可依赖短事务完成原子接受/激活。

提交边界：migration/schema、SQL repository、事务 primitives 和 PostgreSQL 测试；不改写已有业务数据或运行归档。

完成记录（2026-10-20）：

- 分支保持 `codex/guidelines-m0`，起点 `a7f3abd7dc24fc99db7de6a9be4f445d24547491`；本阶段实现 commit：`cc54ca23e761cc1460fbd148248c77a0fa8214fa`（`feat(guidelines): add milestone 2 persistence primitives`）；随后仅补记此 hash。没有新建 worktree 或阶段 Markdown。
- 新 head 为 `20261020_02`（接 `20261020_01`）。七表、部分唯一索引、复合 FK、GiST 区间排斥及不可变 trigger 已落地；schema check 核对表/列、约束、索引、trigger 与扩展。原业务/归档表结构未改，旧 head/表清单测试同步更新。
- `PostgresGuidelineRepository` 提供草稿、批准版本、目录历史、generation/profile、向量和 operation 持久化。所有写事务先锁 catalog，再按 UUID 锁 family documents，最后处理 operation/version；receipt 先于 CAS。fingerprint 取规范化请求，重放保留原 ID/accepted response；保存草稿不改变 activation epoch。
- claim 可跨 repository 重建恢复，最多三次自动尝试；重试原批准 version 时新建 operation，保留旧 receipt。attempt/owner/lease/epoch 共同隔离旧 worker，尤其在取得资源锁后检查 lease，防止等待锁期间过期的请求完成。停用/恢复/新本地发布使旧意图失效；失败保留旧有效版，内置更新保留 override 的批准意图与优先级。具体事务说明已补入主设计 §6.1。
- 最终验证：完整 `backend/tests` **3038 passed，0 failed/errors/skipped**，311.58s；其中新增 `tests/persistence/test_guidelines.py` 的 **31 项真实 PostgreSQL** 用例覆盖 fresh/downgrade/upgrade、metadata 一致性、唯一/FK/区间约束、rollback、丢响应重放、两连接确定性锁屏障、lease 过期/重启/旧 attempt、generation 切换及 canonical/profile 绑定。全部 architecture/ports/domain/API 与既有 persistence 回归包含在该结果中。
- 最终 runner：从根目录用现有 venv 的 `dotenv_values('backend/.env')` **仅将 `WATERAGENTS_DB_*` 注入测试进程**，设置 `WATERAGENTS_TEST_DB_NAME=wateragents_test`，同进程执行 `pytest.main(['backend/tests', '-q', '-p', 'no:cacheprovider', '--tb=short', '--basetemp', '.tmp/guidelines-m2-20261020/full-isolated', '--junitxml', '.tmp/guidelines-m2-20261020/full-isolated.xml'])`。最初整份 dotenv 注入得到 3033 passed / 3 failed，均为离线缺配置/缺凭据断言受部署值干扰；隔离环境后全部通过，没有为这些失败修改产品代码。Windows basetemp ACL 问题通过获准权限与新 task-owned 目录处理；仅有两条既有 Starlette/anyio 弃用 warning。
- `backend/` 下以现有 venv + dotenv runner 执行 `python -m app.infrastructure.persistence.cli upgrade --test-database` 和 `check --test-database`，独立测试库均为 `20261020_02`；测试只清理自有随机 schema，未对业务库执行升级/测试，未修改 `.env`。日志/XML 仅留本地；`git diff --cached --check` 通过，三份指南的 50 个本地链接与代码围栏检查通过。
- 验证等级为本地完整后端 + 真实 PostgreSQL；真实 embedding/分析供应商、浏览器、hosted CI、专业内容实际批准与 pH 实测：**not run**。M3–12 保持未完成，生产资源导入、管理 API、后台 worker 调度与检索接入由后续阶段完成。

## Milestone 3：内置资源、有效目录与本地覆盖

### Work

- [x] 定义 bundled JSON manifest + Markdown package resources；校验 stable key/version、正文/canonical digest、metadata、来源和真实制作者检查记录。
- [x] 实现稳定 bundle identity → UUID 映射和幂等导入；同 key/version 同 digest 不重复导入，同版本不同 digest 拒绝并保留原库。
- [x] 实现 wheel/source 一致的资源读取与 package data 配置；未审核草稿不混入 approved production manifest。
- [x] 实现 family effective selection：停用排除；ready local override 优先其 family；否则 ready bundled；custom 选 ready published；draft/pending/failed 不替换旧有效版。
- [x] 实现 local-edit：建立独立 override draft、绑定所见 base version/digest；已有 override 返回其当前内容，不用新版内置覆盖。
- [x] 实现内置新版本导入与显式 restore：更新保留本地修改和历史，restore 绑定 root revision 与明确目标 builtin version，不静默合并正文。
- [x] 将 approved 内置导入接到持久化准备队列；未完成索引不显示 available，导入错误不清空整个资料库或阻断正常分析。

### Tests

- [x] 覆盖重复导入、坏 manifest/hash、同版冲突、缺资源、升级后本地稿/已发布 override/停用均保留。
- [x] 覆盖一个 family 仅一个有效版本，失败 override 保留原有效版，停用 root 不回退 bundled，显式恢复保留修改历史。
- [x] 构建并安装 wheel 到 task-owned 隔离目录，验证资源集合和 digests 与源码一致；不能只验证源码相对路径读取。
- [x] approved fixture 只用于测试；运行时不能把 fixture 或未审核草稿作为真实内置内容。

### Verify

```powershell
Invoke-GuidelinePytest -Stage gk-m3 -Paths @('backend/tests/application/guidelines/test_bundled.py', 'backend/tests/persistence/test_guidelines.py')
uv build --project backend --out-dir "$guidelineCheckRoot/package"
git diff --check
```

wheel 安装/读取验证使用不含业务凭据的隔离环境，记录实际命令和读取结果；仅 `uv build` 成功不足以完成该工作项。

阶段出口：分发资源和用户数据有明确权威；更新、停用、本地编辑和恢复均不会丢失/误激活内容。

提交边界：bundle 格式、reader/importer、effective selection、package 配置和测试；首批真实内容发布依 Milestone 11 的实际批准。

完成记录（2026-10-20）：

- 保持 `codex/guidelines-m0`，起点 `3034eed9dc1367a48fa67396a97f8ef93952c3fd`；本阶段实现 commit：提交后补记。
- 已实现 JSON manifest/Markdown reader、稳定 document/version UUID、digest/metadata/source/review 校验及逐 family safe-error importer；批准版本原子进入持久化 pending 队列，重复导入保留 receipt/重试状态，新内置版本不覆盖或取消本地稿/批准任务。effective selection、首次 local-edit CAS/克隆、已有稿返回与最新已完成版本的显式 restore 已实现；恢复清除 override 激活指针并保留全部修改历史。格式和事务说明见主设计 §4。
- 本节 Verify 的两组测试加 guideline boundary：首轮 **70 passed**，30.21s。补齐重试去重、local-edit/replay、版本跳号/降级及 pending restore 回归后，最终完整 `backend/tests` **3074 passed，0 failed/errors/skipped**，341.97s；其中新增 M3 **36 项**，含 **10 项真实 PostgreSQL**（另保留 M2 的 31 项）。runner 沿用 M2 的 DB-only 环境隔离，仅注入 `WATERAGENTS_DB_*` 与进程级 `WATERAGENTS_TEST_DB_NAME=wateragents_test`，调用 `pytest.main(['backend/tests','-q','-p','no:cacheprovider','--tb=short','--basetemp','.tmp/guidelines-m3-20261020/full','--junitxml','.tmp/guidelines-m3-20261020/full.xml'])`；仅有两条既有弃用 warning。
- 实际打包：`py -m uv build --project backend --out-dir .tmp/guidelines-m3-20261020/package` 成功生成 sdist/wheel；用 `py -m uv pip install --python backend/.venv/Scripts/python.exe --target .tmp/guidelines-m3-20261020/installed --no-deps <wheel>` 安装。另在任务目录复制源码并仅为临时样本包覆盖测试 manifest/Markdown，再 build/install 到独立目录。两个全新 `python -I` probe 均去除业务/provider 环境变量、确认 `app.__file__` 来自目标目录；源码/安装包的资源集合、资源 SHA-256 与全部批准版本快照一致：正式包 **1 个资源/0 个批准版本**，临时样本包 **2 个资源/1 个合成批准版本**。正式 wheel 未包含 tests、样本正文或 `.env`，项目 venv 未被安装修改。
- 正式 manifest 保持空，合成批准内容仅在必要测试 fixture；真实制作者批准与首批资料仍属 M11。`wateragents-db check --test-database` 验证独立测试库 head 为 `20261020_02`，M3 无新增 DDL；未对业务库执行升级/测试。`git diff --cached --check`、50 个本地文档链接与代码围栏通过。构建缓存、wheel、安装目录、日志/XML 仅留本机，没有新增阶段状态 Markdown。
- 验证等级：本地完整后端、真实 PostgreSQL、实际 wheel 安装/读取；真实供应商、浏览器、hosted CI、专业批准与 pH 实测：**not run**。M4–12 保持未完成；M6 接入导入入口及生产 worker 生命周期。


## Milestone 4：草稿、人工检查与本地管理 API

### Work

- [ ] 实现 create/save/get/list、local-edit、publish、set-enabled、restore、operation 查询/retry、immutable version read 的 application services；列表搜索为本地 lexical，不调用 embedding。
- [ ] 发布先检查已保存 draft revision/digest 与 `review_confirmed`，再原子创建 immutable reviewed version、pending pointer、operation/receipt 并返回 `202`；保存不等于发布。
- [ ] 活跃 pending 阻止重复新发布；失败 pending 可重试同一批准版本，也可在重新检查后由新发布 supersede；不能用当前草稿替换旧批准内容。
- [ ] 按主设计 §10 实现 `/api/guidelines` 全部管理路由与 typed schemas，固定 `/operations/...` 的路由匹配顺序，限制 cursor/limit 和 UUID/origin 写入权限。
- [ ] 所有 mutation 复用 LocalMutationGuard 的 Host/Origin/JSON 与 request ID，所有响应 no-store；扩展当前 OpenAPI allowlist 与 guard，不移除既有 API 禁止项。
- [ ] 实现安全 400/422/403/404/409/503、最新 revision/snapshot 冲突反馈；接受后的 embedding 失败通过 operation 报告，不伪装为保存失败。
- [ ] 明确无 public sharing、额外账号/RBAC、运行时网页搜索或模型可调用管理工具；用户不能设置 bundled origin 或伪造 immutable version。

### Tests

- [ ] API 覆盖不完整草稿、自编无出处发布、资料整理缺出处拒绝、digest 不符、未确认审核、编辑 bundled 被拒与 override 成功。
- [ ] 覆盖所有 mutation 幂等重放/不同 body 冲突、revision 冲突、pending/failed/superseded、不存在资源和操作路由不被 UUID 动态路由吞掉。
- [ ] 覆盖恶意 Origin/Host、非 JSON、no-store、request ID、错误脱敏和存储不可用；正常 localhost same-origin 仍可使用。
- [ ] 验证 management list/filter 不发生 provider dispatch；提交失败与提交后准备失败的 HTTP/operation 语义分开。

### Verify

```powershell
Invoke-GuidelinePytest -Stage gk-m4 -Paths @('backend/tests/application/guidelines/test_authoring.py', 'backend/tests/api/test_guidelines.py', 'backend/tests/api/test_openapi_contract.py', 'backend/tests/api/test_m9_target_openapi.py')
npm --prefix frontend run generate:api
npm --prefix frontend run check:api
git diff --check
```

阶段出口：真实数据库支持可对账的管理动作；人工审核绑定具体已保存版本，API 边界与既有工作台一致。

提交边界：authoring services、API、OpenAPI/generated types 和对应测试；generated API 文件由脚本生成，不手改。

完成记录：`pending`。

## Milestone 5：Embedding 契约与供应商生命周期

### Work

- [ ] 从历史 adapter 抽出真正通用的 HTTP/输出校验基础；旧 `EmbeddingPort` 调用继续先验证 `CanonicalCaseQueryV1`，新 adapter 验证 guideline document/query。
- [ ] guideline 使用既有 model catalog embedding role 的期望 provider/model/dimensions/max-input；独立 profile ID 覆盖模板/契约/角色指令，不写死供应商型号。
- [ ] 保证 query/document 模板兼容、输入有界与稳定，模型要求的角色前缀在 profile 中固定；公共输出校验含 count/order/model/dimensions/finite/norm。
- [ ] 在 runtime composition 注入 guideline adapter；run 查询复用 RunProviderBundle 的固定 lease，后台每次 attempt 自取 lease，不把密钥/路由写入版本或 transcript。
- [ ] guideline generation namespace 与 M8 case generation 分离；memory disabled/stateless 不关闭本功能，仍按 guideline 库/readiness 判断。
- [ ] 固定配置默认值：top-k 3、初始 cutoff 0.35、每请求 knowledge budget 3,072 tokens、query 8,192 UTF-8 bytes、在线 3 秒且无重试；阈值必须由 Milestone 11 的真实检索样例核对。
- [ ] 固定后台默认值：并发 2、单 attempt provider timeout 20 秒、最多 3 attempts、退避 2/10 秒；配置校验和错误码有单一来源。

### Tests

- [ ] 旧 case adapter 拒绝 guideline/任意文本，新 adapter 拒绝错模板/错误 profile；公共传输抽取后旧 M8 测试仍过。
- [ ] 覆盖乱序/少条/重复 index、维度不符、非有限值、零范数、超输入、超时/取消和安全 provider error。
- [ ] 用记录 dispatch 的 adapter 验证 run lease 固定、后台新 attempt 取新 lease；密钥切换不混在一个 attempt 或泄露到持久化。
- [ ] 验证 memory disabled/stateless 与 guideline 可用性独立；未配置/不兼容只使知识 unavailable，不改原分析 risk/availability。

### Verify

```powershell
Invoke-GuidelinePytest -Stage gk-m5 -Paths @('backend/tests/application/guidelines/test_embedding.py', 'backend/tests/domain/test_model_catalog.py', 'backend/tests/infrastructure/test_m8_runtime.py', 'backend/tests/architecture/test_m8_boundaries.py')
git diff --check
```

阶段出口：guideline 向量有自己的合法输入与身份，复用通用供应商能力而不取消历史 case 边界；fake 通过尚不代表真实检索质量。

提交边界：embedding DTO/adapter/shared transport、配置/runtime 注入与回归测试；不加外部向量服务、HNSW 或第二个 model catalog。

完成记录：`pending`。

## Milestone 6：持久化准备、激活与索引切换

### Work

- [ ] 实现 publication/index worker 的持久化 claim/lease/restart、有限 concurrency/attempt/退避；embedding 在事务外，完成前校验 attempt ownership。
- [ ] 覆盖首次安装/空库无 active generation 的初始化：按当前合法 profile 创建首个 generation，并使首份批准内容可完成准备；缺凭据或准备失败留在明确状态，不能因等待“已有 active”永久卡住。
- [ ] 批准版本准备向量后，在短事务中校验 pending version、digest、activation epoch、generation/profile，再写向量并原子更新 catalog intervals/revision 和 operation。
- [ ] provider/索引失败保留批准版本与旧 effective content，显式 retry 使用同版本；新的实际审核发布可 supersede 失败旧任务。
- [ ] 配置/model/模板变化建立候选 generation，覆盖有效 catalog；切换前锁内核对 revision，若 publication/disable/restore 改变目录先补齐/reconcile，再原子切换。
- [ ] active generation 改变时，旧 publication 为当前 profile 重新准备后才可激活；旧 profile 的向量不能混入新 generation。
- [ ] 新 builtin base 导入不覆盖/取消用户已批准的 local override；停用/恢复/新发布使旧激活 token 失效，不允许 worker 逆转用户后来的选择。
- [ ] runtime startup/shutdown 接入 worker 生命周期，取消不续派新任务；保留旧 generations/versions，首版不新增自动 purge。

### Tests

- [ ] 屏障重现“批准 A → 开始 embedding → 保存草稿 B → A 返回”：A 仍仅以批准 A 激活，B 留草稿，不误用新 revision 取消合法批准。
- [ ] 屏障重现“批准 → embedding 未返回 → 停用/恢复/新审核发布 → 旧 worker 返回”：旧内容不重新激活，operation 状态准确。
- [ ] 屏障重现“generation 重建读取 catalog → publication/disable/restore 改目录 → 尝试切换”：先补齐再切换，目录每一有效版都有同 profile 向量。
- [ ] 覆盖 publication 计算期间 generation 切换、lease 回收、worker 崩溃/重启、最后 attempt 失败、用户 retry；不产生重复审核/版本。
- [ ] 从新建数据库验证首个 generation/首份批准内容可用，及首次失败后补齐配置/重启/重试可恢复；旧库迁移后不丢原数据。
- [ ] PostgreSQL 验证失败 rollback 后旧可用目录仍完整，at-most-one active/building 不变量与锁序无死锁；不只检查最终 UI 状态。

### Verify

```powershell
Invoke-GuidelinePytest -Stage gk-m6 -Paths @('backend/tests/application/guidelines/test_worker.py', 'backend/tests/persistence/test_guidelines.py', 'backend/tests/infrastructure/test_m8_runtime.py')
git diff --check
```

阶段出口：只有已批准且准备完成的正确版本可激活；重启、失败和指定竞态均不覆盖后续用户意图、不混索引。

提交边界：worker、publication/generation coordinator、runtime 生命周期与真实并发测试；不含无限恢复任务或同步请求中长时间持锁。

完成记录：`pending`。

## Milestone 7：Run pin 与有界自动检索

### Work

- [ ] 在 query/topology 准备后、waterbody 执行前固定 run catalog revision/generation/profile；全 run 各 session 查询同一快照，无可用目录记录 unavailable 并继续原分析。
- [ ] session `get_or_load()` lazy/single-flight 一次加载；缓存绑定 run/profile/query/context，session 关闭释放，不使用跨用户/跨 profile 无界缓存。
- [ ] 从已验证问题、canonical 指标、inference mode 与真实已知情境构造 query；不拼 raw transcript、凭据或无界观测；过长整字段省略可选文本并记录 `input_reduced`。
- [ ] 按主/related parameter metadata 取候选，scenario 仅为排序信息；空库/无候选直接返回不调用 embedding。
- [ ] 实现授权候选内 exact cosine、cutoff、family 去重、top-k；稳定按 similarity DESC、root UUID ASC、version UUID ASC 排序，不比较不同 profile。
- [ ] query embedding 每 session 至多一次并受 3 秒和剩余 session deadline 约束，在线不重试；document vectors 复用预计算结果。
- [ ] 明确 `empty_library`、`no_match`、`unavailable`、`budget_limited`、取消与输入缩减；相似度不是科学权威/预测 confidence，不变成数值或 risk 门槛。

### Tests

- [ ] 同 run 两 session 之间 publish/disable/restore/switch 不改变其 pin，新 run 看到新目录；旧向量/版本未被清理。
- [ ] 覆盖跨指标 related keys、中英文同义 query、无关内容不凑数、同分稳定排序、family 唯一和混 profile 拒绝。
- [ ] 覆盖空库、无 metadata 候选、并发 get_or_load、timeout、cancel/deadline、input_reduced；断言实际 embedding dispatch 数。
- [ ] 保留一组小型人工标注检索 fixture，包括“上下游酸碱变化”→ pH、机制相关和应无命中 query；与 Milestone 11 的真实 provider 结果分开。

### Verify

```powershell
Invoke-GuidelinePytest -Stage gk-m7 -Paths @('backend/tests/application/guidelines/test_retrieval.py', 'backend/tests/persistence/test_guidelines.py', 'backend/tests/infrastructure/test_m7_runtime.py')
git diff --check
```

阶段出口：新运行可自动取相关完整说明书，运行内稳定、预算有界；无知识时分析仍按原路径进行。

提交边界：run context、retrieval/cache/clock-cancellation、SQL ranking 与测试；不新增公开 semantic-search endpoint 或 agent tool。

完成记录：`pending`。

## Milestone 8：可选模型参考与数值链路

### Work

- [ ] 在首次实际 planning 请求前加载；无 planning 但直接 numeric estimation 的路径，在首个 numeric 请求前加载。不要依据单一 handoff 标记跳过其他真实模型入口。
- [ ] planning、numeric、batch 使用同一 session selection 的适用子集；synthesis 仅沿已有有效引用取相关快照，不重新全库检索；repair 保留原 scope/知识子集。
- [ ] 添加独立 `advisory_knowledge`；system 只描述来源角色/可选采用/alias 规则，正文不得改变 tools、scope、role、identity 或既有校验。
- [ ] 对最终序列化请求统一 model fit，计入 knowledge 且保留原 evidence/output 预算；低相关整份移除，不能从机制或例外中切 token，最终 alias 集合只反映实际输入。
- [ ] 给数值/解释内部 response schema 与 draft metadata 增加可空 `guideline_refs`；server 从本 operation delivery 解析 citation，模型不能自造版本、URL 或 transcript IDs。
- [ ] 用独立 usage sidecar 保留 numeric operation/task/window 引用；通过既有 `NumericDerivedPayload.model_operation_id` 关联，不复制/修改其科学事实字段。
- [ ] 仅非法 guideline alias 时丢弃引用并安全告警；空引用合法，不强迫采用全部 provided；原 evidence/scope/数值错误照原 validator/repair 处理。

### Tests

- [ ] 覆盖完整 planning→numeric、无 planning→numeric、batch/synthesis/repair；在实际 model port 检查知识出现时间与 phase，不只断言 helper 被调用。
- [ ] 覆盖原 zero-model、已取消、deadline 耗尽与已满足证据路径，分别断言 embedding 和 reasoning 实际 dispatch，知识不制造新的模型任务。
- [ ] 用临界模型容量检验完整请求 fit：保留 evidence、整份移除、全移除状态、alias 与实际字节一致；正文无强制系统权威。
- [ ] 覆盖模型忽略说明书、缺说明书要求数据仍推理、资料冲突可自主分析；不得新增 guideline sufficiency gate。
- [ ] 覆盖未知/跨 operation alias、安全 warning、不改合法数值；现有 anchors、range-only、observed 资格、单位/窗口/scope 回归不弱化。

### Verify

```powershell
Invoke-GuidelinePytest -Stage gk-m8 -Paths @('backend/tests/application/guidelines/test_model_context.py', 'backend/tests/application/reasoning/test_numeric_estimation.py', 'backend/tests/application/reasoning/test_draft_validator.py', 'backend/tests/application/evidence_tools/test_numeric_estimation_acquisition.py', 'backend/tests/application/evidence_tools/test_numeric_dependency.py', 'backend/tests/infrastructure/test_m6_runtime.py')
git diff --check
```

阶段出口：真正估值前已有可选参考，预算与旧数值规则一致；需与 Milestone 9 一起完成完整可追溯链后才可视为可用功能。

提交边界：reasoning request builders/parser/draft/validator、上下文传参、citation resolver 和对应测试；不新建第二套估值器、科学求解器或观测权威。

完成记录：`pending`。

## Milestone 9：Transcript、结果引用与历史闭包

### Work

- [ ] 注册 `guideline_run_context`、`guideline_retrieval`、`guideline_delivery`、`guideline_usage`、`result_guideline_usage`，默认 `rag_eligible=false`；补齐 schema、refs、public projection 与 readers。
- [ ] 在真实 model port 提交处记录 delivery，关联 request/retrieval/run context、operation/phase、最终 alias/正文子集和预算状态；仅构建 request 不标为已提供。
- [ ] 保存能重建实际知识输入的 model view、原文/sources/digests 或合法不可变引用；transport failure 只表示随请求提交/请求失败，不能标 used。
- [ ] 保存 operation usage target；result assembler 只沿最终选用的 numeric lineage 与有效 drafts 生成 `result_guideline_usage`，排除未选候选及其他 cell 引用。
- [ ] result usage 引用原 usages，由现有 `analysis_detail.transcript_item_refs` 接入结果；保存 result_id + 合法 target keys，不反指未来 result transcript item 形成循环。
- [ ] 维持单一 registry/writer/reader 和两层 SSE；history snapshot viewer 不依赖管理库当前 active 内容，旧记录不合成不存在的引用。
- [ ] 更新 M8 source closure/projection，识别新引用且不索引整份用户说明书、不增强 inference eligibility/confidence；真正改变旧 content schema 时保留旧 reader/digest，生产只写单一当前 schema。
- [ ] writer 拒绝知识记录时，不注入无法建立合法引用的内容；沿原 persistence failure 契约处理，不掩盖分析本身的存储故障。

### Tests

- [ ] 验证 request constructed/实际提交/transport failure/模型未引用/有效引用五种状态；前端可读的 provided 名单与实际 payload 完全一致。
- [ ] numeric 先产生两个候选、最终只选一个时，结果仅包含被选 lineage 的引用；相同参数不同 cell/window 不能串用 usage。
- [ ] 验证 step 零基 index、overall、task/cell/window 目标合法，未知 alias/跨 run 引用不生成可点击链接，引用图无循环/悬空。
- [ ] live SSE、数据库持久化和 history API 读回一致；发布新版、停用、恢复、内置更新后仍打开运行当时原文/出处。
- [ ] 验证新增引用不令 M8 index/source closure 失败，不把 guideline 作为 historical inference；旧历史 fixture/hash 与 stateless/memory-disabled 回归通过。
- [ ] 验证取消/terminal 后无迟到模型任务或 delivery；安全公开投影不含 credential/raw provider envelope。

### Verify

```powershell
Invoke-GuidelinePytest -Stage gk-m9 -Paths @('backend/tests/application/guidelines/test_transcript.py', 'backend/tests/application/test_transcript_registry.py', 'backend/tests/application/transcripts/test_m8_history_registry.py', 'backend/tests/api/test_analysis_history.py', 'backend/tests/persistence/test_numeric_dependency_persistence.py', 'backend/tests/architecture/test_m8_boundaries.py')
npm --prefix frontend run generate:api
npm --prefix frontend run check:api
git diff --check
```

阶段出口：实际提供与明确采用分开可查，数值阶段引用能到最终结果，当前库变化不改历史；M8 与旧事实权威不变。

提交边界：typed contents/registry、reference closure、assembler sidecars、public/history projections、generated types 与测试；不能散改所有数值 payload 加重复 citation 字段。

完成记录：`pending`。

## Milestone 10：前端创建、发布与引用查看

### Work

- [ ] 新增 `/guidelines` 与“说明书”导航、程序自带/我的说明书、来源标签、列表查询/分页和当前有效状态；本地修改内置项有清楚标记。
- [ ] 实现创建/编辑/保存草稿，模板提示机制、条件、线索、例外；自编出处可空、专业整理出处必填，用户无需填写 profile/digest/vector。
- [ ] 编辑内置先调用 local-edit；已有 override 从当前内容继续，内置升级不覆盖；恢复操作展示明确版本与保留修改历史。
- [ ] 实现检查并发布：先获得保存 ACK 的 revision/digest，再展示该确切版本供确认；人工动作不能由 autosave/索引成功替代。
- [ ] 展示草稿/正在准备/可用于分析/准备失败/已停用及 operation polling/retry/superseded；取消离开页面后的轮询，不制造重复 publication。
- [ ] editor 分开 loaded revision、submitted edit version、current edit version、dirty 与 uncertain mutation；迟到 ACK 不抹掉后续输入，409 不自动覆盖。
- [ ] timeout/丢响应/无效 JSON 保留原 key 与文本，通过 operation/document 或重放同 key 对账；对账前不盲目新建/再发发布。
- [ ] 扩展 `DetailPanel.visibleRecords` 与 `referencedItems` 分类，transcript 展示实际 provided，结果只展示有效 used，保留估值/解释 phase provenance。
- [ ] 使用共享安全 viewer 展示保存快照/原文/出处；无外部出处显示“用户编写，无外部出处”，history 不跳转当前草稿替代引用内容。
- [ ] 安全 Markdown 禁用 HTML/脚本并限制链接 scheme；页面文案允许用户配置模型处理文本，不宣称所有内容绝不离开本机。

### Tests

- [ ] component 覆盖双库、创建、自编无出处、资料整理、编辑内置、真实检查确认、准备失败/重试、停用/恢复及历史 viewer。
- [ ] 重现“保存 A 发出 → 用户输入 B → ACK A 返回”，B 保持 dirty；“提交成功但响应丢失/无效”复用原 key 对账不重建文档/版本。
- [ ] 重现 publish 准备中继续编辑、restore/disable 期间旧 operation 返回、409 snapshot 和迟到 GET/ACK；界面不能误显示旧内容已激活或清除脏编辑。
- [ ] 浏览器覆盖管理→发布→分析提供→引用→history→库更新后旧内容查看；API fake 的 UI 测试与真实后端集成分开标注。
- [ ] 覆盖 HTML/危险链接、无引用不自动列 adopted、候选/移除项不计 provided、原有 detail panel 与键盘/导航交互回归。

### Verify

```powershell
npm --prefix frontend run check:api
npm --prefix frontend run lint
npm --prefix frontend run typecheck
npm --prefix frontend run test -- --run tests/guidelines.test.tsx tests/detail-panel.test.tsx
npm --prefix frontend run build
npm --prefix frontend run test:e2e -- e2e/guidelines.spec.ts
git diff --check
```

浏览器服务前置条件见附录 A；不能把仅 Vite + route mocks 的通过写成真实后端链路通过。

阶段出口：用户可创建自己的说明书、编辑内置并人工发布；实际提供/采用和旧版内容可查看；编辑与网络并发不丢数据。

提交边界：前端页面、client/hooks、shared viewer、generated types 与组件/E2E；不引入公共分享入口或用户不需要的模型内部配置表单。

完成记录：`pending`。

## Milestone 11：首批 pH 资料、真实检索与实测配对

### Work

- [ ] 从主设计 §13 第一方资料重新核实原文与适用边界，准备四个独立草稿：混合/传递限制、酸性输入/缓冲、光合作用/呼吸、原因比较；保留 URL、章节/locator、访问时间。
- [ ] 每稿写清现象→可能机制→条件→观察线索→替代解释/反例→估值注意事项；区分本次事实与一般机制，不提供仅凭上游 pH/流量就保证精确的假公式。
- [ ] 将实际草稿与出处交用户/指定人员检查；记录真实批准的 digest/version/时间，未批准或修改后未重新批准的内容继续为草稿，不纳入生产 bundle。
- [ ] 以真实配置 embedding 执行小型中英文标注检索集，报告期望命中、实际排名、无关误命中、未命中和 profile；依据结果调整初始 cutoff/模板并按需重建 generation。
- [ ] 只读调查可获得的目标点/上游/时间配对观测，选择 3–5 个可比较 pH 案例，至少含上游信息较完整与部分信息缺失两类；不足时明确未达标，不能用预测代实测。
- [ ] 建立 holdout 清单：隔离目标实测及 raw/aggregate/其他水体副本/历史 RAG/缓存/context/说明书例子的等价泄漏；保留符合原资格的上游同指标 anchors。
- [ ] 每例固定证据快照、provider/model/version、预算和时间语义，运行无 guideline/有 guideline 的成对比较；至少一完整配对，成本允许各重复 3 次。
- [ ] 区分单点与时间窗统计的可比性，报告点误差的 pH 差、范围宽度/覆盖、解释质量、实际提供/采用版本和所有失败/不可比较/未运行项；不把单点实测说成整窗连续真值。
- [ ] 保存可复现的公开输入 fixture/脱敏清单和必要评估脚本源码；大输出/截图留 task-local，报告中保留反例，不承诺普遍精度提升。

### Tests

- [ ] 生产 bundle 中的每项都能定位真实人工批准且 digest 一致；测试 fixture 审核信息不能混作真实批准。
- [ ] 对评估脚本做必要的 holdout/leakage 检查，刻意注入重复目标 raw/aggregate/history/cached 值应被拒绝；避免只凭“输入没有 target 字段”断言隔离。
- [ ] 证明 no-guide 对照真正未提供知识、with-guide 提供的是批准版本；两组证据/模型/预算差异明确，query 不含对照答案。
- [ ] 用已知小数据核对评估指标、窗口匹配、无点估计/仅范围/失败等统计；合成数据只检验程序，不计入 3–5 个真实案例。
- [ ] 缺合格同指标 anchors 的案例仍遵守原 range-only/来源规则；不能为了算点误差而放宽资格或强迫模型输出单点。
- [ ] 真实 embedding、真实分析模型及实测核对分别记录，未配置、费用/服务限制或样本不足保留 `not run`，不能以 fake 通过代替。

### Verify

```powershell
Invoke-GuidelinePytest -Stage gk-m11 -Paths @('backend/tests/application/guidelines', 'backend/tests/persistence/test_guidelines.py')
git diff --check
```

检索/实测 runner 的具体 CLI 在实现后记录本节：输入 fixture、profile、运行次数、输出 task-local 路径和实际命令都要齐全；当前没有可执行 runner，不能把上述 pytest 视作已完成真实核对。

阶段出口：真实批准内容可分发，真实 embedding 的相关性已核对，3–5 个真实 holdout 案例有完整成对结果和限制；未获得人工批准或样本证据时本阶段保持未完成。

提交边界：实际批准的 bundle、必要公开输入 fixture/评估源码、文档证据摘要和针对性修正；不提交新运行结果归档、不虚构批准或选掉更差案例。

完成记录：`pending`。

## Milestone 12：整体回归、契约审计与交付

### Work

- [ ] 逐项审计 GK-C01–GK-C24 和下方 GK-A01–GK-A12，将证据定位到实际测试/记录；修复仅局部通过而完整调用链失败的问题。
- [ ] 复查 domain/ports、schema/迁移、provider/runtime、request/response builders、numeric/assembler、registry/projection/M8、API/generated types、UI/history 全部消费者均已更新。
- [ ] 运行完整后端、PostgreSQL、前端 lint/typecheck/unit/build 和浏览器回归；真实 provider 与 hosted CI 仅在实际执行后填写，环境失败与产品失败分开。
- [ ] 检查旧历史内容仍可读、旧 digests 不变、无新增观察资格旁路/第三 SSE/归档 imports/测试生产入口；库更新保留用户内容。
- [ ] 更新技术设计中的真实实现路径、配置、操作说明和验证限制，本文原位填写各阶段完成记录；清除仅属过渡且不再需要的实现，不清理用户文件。
- [ ] 交付记录实际 branch/HEAD/commits、文件范围、命令/计数、人工批准版本、3–5 例结果摘要、未完成项；按后续用户授权范围执行 commit/push/PR，不对已授权动作重复索取确认，不能把本次写 TODO 视作代码实现授权。

### Tests

- [ ] 全部新增针对性测试与完整后端/架构/专用 PostgreSQL 回归通过，无跳过后伪称全通过。
- [ ] 前端生成契约、lint、typecheck、unit/build、mock UI 与真实后端浏览器链路结果分别可核对。
- [ ] 对本次发现的并发缺陷保留实际操作顺序的回归测试，不能用仅断言最终状态的测试替代。
- [ ] 真实 embedding/分析模型与 pH holdout 的通过范围有限且明确；没有对应证据的 hosted CI/平台检查标 `not run`。

### Verify

```powershell
Invoke-GuidelinePytest -Stage gk-final -Paths @('backend/tests')
npm --prefix frontend run check:api
npm --prefix frontend run lint
npm --prefix frontend run typecheck
npm --prefix frontend run test -- --run
npm --prefix frontend run build
npm --prefix frontend run test:e2e
git diff --check
git diff --stat
git status --short --untracked-files=no
git branch --show-current
git rev-parse HEAD
```

完整 E2E 包含真实后端路径时须满足附录 A；若部分 suite 自动 skip，按实际 skip 原因记录。若后来用户要求 push，还要核对 local HEAD、upstream 与 `git ls-remote`，不能只凭 push 输出断言远端内容。

阶段出口：以下全部功能验收均有证据，代码/内容/实测边界清楚；任一必需项未完成时不能将整个 Milestone 标为全部完成。

提交边界：最终回归修复、必要文档和完成记录；提交前确认无密钥、业务库快照、新日志/模型输出、无关改动或未经审核内容。

完成记录：`pending`。

## 最终验收清单

- [ ] **GK-A01**：用户可在前端创建/保存自编无出处草稿，人工检查后发布；来源性质准确。
- [ ] **GK-A02**：内置说明书可本地编辑，升级保留修改，停用无暗中回退，显式恢复保留历史。
- [ ] **GK-A03**：仅审核且 ready 的有效版被自动检索；empty/no-match/unavailable 分开且分析可继续。
- [ ] **GK-A04**：无 planning 的 numeric 路径在估值前获得参考，模型可以忽略或在信息缺失时自主分析。
- [ ] **GK-A05**：零模型、取消、deadline 路径无新增 provider dispatch，最终请求 fit、整份裁剪和实际 alias 一致。
- [ ] **GK-A06**：transcript 显示实际随请求提供的版本；结果只显示明确有效引用，numeric lineage 与目标绑定正确。
- [ ] **GK-A07**：实时与历史能打开当时正文/出处，库修改或恢复不改变旧结果引用，无跨 run/循环引用。
- [ ] **GK-A08**：发布/编辑/停用/恢复/generation/重启/响应丢失的指定竞态有实测测试，旧 worker/ACK 不覆盖新意图。
- [ ] **GK-A09**：旧 evidence/scope/numeric/observed/risk 与 M8 eligibility 不弱化；memory-disabled/stateless 可独立使用指南。
- [ ] **GK-A10**：本地 API/安全渲染/供应商文本范围正确，用户本地内容不自动进入 Git/共享平台。
- [ ] **GK-A11**：首批资料有真实人工批准，真实 embedding 检索已核对，3–5 个实测 holdout 成对案例如实报告。
- [ ] **GK-A12**：全栈回归和交付证据完整，fake/真实供应商/数据库/浏览器/hosted CI 的结论与限制分开。

## 附录 A：可复用验证环境与命令

从仓库根目录使用 PowerShell。先确认 `backend/.env`、锁定依赖和 PostgreSQL 测试服务可用；按项目现有说明启动服务，不复制、修改或清空业务数据库。以下助手只存在于本次测试 shell；设置测试库名仅作用于子测试进程环境并在完成后恢复。

```powershell
$guidelineCheckRoot = ".tmp/guidelines-$PID-$(Get-Date -Format 'yyyyMMddHHmmss')"
New-Item -ItemType Directory -Force -Path $guidelineCheckRoot | Out-Null
function Invoke-GuidelinePytest {
    param([string]$Stage, [string[]]$Paths)
    $guidelinePreviousTestDb = [Environment]::GetEnvironmentVariable('WATERAGENTS_TEST_DB_NAME', 'Process')
    try {
        $env:WATERAGENTS_TEST_DB_NAME = 'wateragents_test'
        uv run --project backend --locked python -m dotenv -f backend/.env run --no-override -- python -m pytest @Paths -q --basetemp "$guidelineCheckRoot/$Stage"
        if ($LASTEXITCODE -ne 0) { throw "pytest failed: $Stage (exit $LASTEXITCODE)" }
    }
    finally {
        [Environment]::SetEnvironmentVariable('WATERAGENTS_TEST_DB_NAME', $guidelinePreviousTestDb, 'Process')
    }
}
```

若 dotenv runner 不在锁定环境中，按项目当前测试启动约定解决并记录；不能把 runner/Windows ACL 故障报成业务回归，也不能跳过 PostgreSQL 后勾选数据库验收。`--basetemp` 只指向本任务新建路径；禁止复用历史归档/用户临时目录。命令返回非零后先解决或记录原因，再继续依赖它的验证。

本文列出的新路径由对应阶段创建；Milestone 1 的 domain/ports/architecture 测试已存在，application/persistence/frontend 的 guideline 测试仍待后续阶段实施。可按实际拆分调整，但须同步本文命令和证据，不能把尚不存在的测试写成已通过。

浏览器验证分两类：

- UI/mocked API：可用现有 Playwright Vite webServer 与受控 route mocks，验证页面行为，不能证明 SQL/API/model dispatch。
- 集成/production：先按 [AGENT.md](../../AGENT.md) 构建前端、升级专用测试数据库并启动隔离后端，明确端口；设置 `$env:WATERAGENTS_E2E_BASE_URL = 'http://127.0.0.1:<test-port>'` 后运行 E2E。当前 Playwright 仅能自动启动 Vite，**不会启动后端**；验证后恢复该进程环境变量。

API schemas 用 `npm --prefix frontend run generate:api` 生成，以 `check:api` 校验。真实 provider 不通过生产测试后门注入；E2E 采用何种 provider/fixture 必须可见，确定性 fake 仅在测试 composition 中使用。后台服务若用 PowerShell `Start-Process`，沿开发规则使用 `-WindowStyle Hidden`，在本任务记录拥有的 PID 并仅结束本任务进程。

## 附录 B：每阶段完成记录模板

在对应 Milestone 的 `完成记录` 原位填写以下内容；不复制一个新状态文件。任务未通过/未运行时保留对应未勾选项。

```text
状态：pending / partial / complete
基线：branch、HEAD、已有修改；本阶段 commit（如未提交则明确）
实现范围：实际文件/类型/接口；规格调整及依据
新增或回归测试：缺陷/不变量、关键并发顺序、测试路径
实际命令与结果：工作目录、参数、passed/failed/skipped 数、环境
验证等级：fake / PostgreSQL / browser-mocked / browser-integrated / real-embedding / real-analysis / hosted-CI
资料审核：实际批准人或本地操作者标识、时间、内容 digest/version；无则 not run
实测核对：案例数、holdout/时间语义、成对输出摘要、限制；无则 not run
未完成与原因：下一步、环境限制、仍需真实人工审核的具体草稿
阶段出口：逐项证据或尚未满足项；不得以文档完成代替运行验收
```

Milestone 0–3 的实际命令与证据见对应完成记录；Milestone 4–12、内容审核、实测实验与最终功能验收仍待实施。
