# M4 Milestone 0 基线、边界与接力盘点

记录日期：2026-09-25

## 1. 范围与结论

本记录只完成 M4 Milestone 0：固定可复现基线、确认 M3 的真实扩展接缝、登记 M4 影响范围，并增加归档隔离门禁。本阶段没有修改 create schema、configuration、workflow、数据库 schema 或任何 M4 业务行为。

M4 继续复用 M3 的一个 `RunCoordinator`、run/session runtime、`TranscriptWriter`、typed-content registry、`TranscriptReader`、两层 SSE 和同一个 transcript store。M4 不新增数据库业务表；后续 typed contents 继续写入现有四表。

## 2. Git 与环境基线

| 项目 | 基线 |
|---|---|
| 实现分支 | `codex/m4-milestone-0` |
| 起始 HEAD | `a75d604c6397ab0e8c8d7b11d2c7fa9d501002cc` |
| 刷新后的 `origin/main` | `a75d604c6397ab0e8c8d7b11d2c7fa9d501002cc` |
| 起始 merge-base | `a75d604c6397ab0e8c8d7b11d2c7fa9d501002cc` |
| Python | 3.12.14，复用已有 `backend/.venv` 依赖环境 |
| 关键测试依赖 | pytest 9.1.1、FastAPI 0.141.1、SQLAlchemy 2.0.54、psycopg 3.3.6 |

开始工作时，主工作区位于 `codex/m4-document-design`，包含四个已修改的项目级文档和未跟踪的 `docs/M4/`。实现工作在独立 worktree 中从最新 `origin/main` 创建；这些既有文档改动没有被移动、覆盖、暂存或提交。M4 设计文档从保留的文档工作区只读使用，本分支只新增当前独立基线记录。

测试使用显式专用数据库名 `wateragents_test`，与业务数据库名分离；测试和 smoke 的数据库 helper 仍会为每次运行创建随机临时 schema，并只清理该 schema。测试输出和本记录均未保存凭据值。

## 3. 当前真实执行路径

1. `backend/app/main.py:create_app()` 是 composition root，装配 settings、clock、PostgreSQL store、production registry、coordinator、reader、HTTP routes 和 lifespan cleanup。
2. `backend/app/api/http/analysis_runs.py:create_run()` 读取原始 JSON bytes 和 `Idempotency-Key`，调用 `RunCoordinator.create_run()`。
3. `RunCoordinator` 独占 `RunRuntime` 的创建、状态、task、cancel、session 和 live subscription ownership；creation path 在启动 workflow 前完成幂等持久化确认。
4. `AnalysisWorkflow.run(WorkflowContext)` 是 M4 workflow 的替换接缝。当前 `DefaultFakeWorkflow` 只驱动 M3 生命周期，M4 不需要第二个 coordinator 或 runtime。
5. `TranscriptWriter` 通过 `WriterRuntime` protocol 负责批量保存、未知提交核实、重试、交付宽限、手动补写和 shutdown drain。
6. `TranscriptRegistry` 是版本化正文解析、引用和 visibility 的单一注册入口；`TranscriptReader` 将 live state 与持久化读取装配为 history/detail/full-content 读模型。
7. run SSE 由 `RunCoordinator.subscribe_run()` 提供完整轻量快照；session SSE 由 `subscribe_session()` 提供可按游标读取的过程记录。HTTP 层不读取 coordinator 私有状态。
8. `PostgresTranscriptStore` 与 `InMemoryTranscriptStore` 实现同一 creation/write/read contract；production lifespan 只装配 PostgreSQL 实现，fake 由测试显式注入。

## 4. M4 改动影响表

| 区域 | 当前扩展接缝 | 唯一 runtime source of truth | M4 后续改动边界 |
|---|---|---|---|
| 请求与配置 | `CreateAnalysisRunRequest`、`AnalysisRequestV1`、`AnalysisConfigurationSnapshot` | 原始请求 bytes 决定幂等身份；通过校验的 application models 决定持久化请求和配置 | 直接收敛为最终字段并同步调用方，不增加兼容 union 或 feature flag |
| Domain | `backend/app/domain/` 中的严格 Pydantic domain models | 每个业务事实对应一个 domain/application typed model | 增加 context、target、parameter intent 和 graph 类型；不跨层传 raw dict |
| Application | `RunCoordinator`、`AnalysisWorkflow`、`WorkflowContext` | `RunCoordinator` 是唯一 live runtime owner；workflow 只通过公开 context/coordinator 能力推进 | 在现有 workflow 接入 parser、resolver、availability 和 topology，不建立第二套 lifecycle |
| Ports | `backend/app/ports/` Protocol 与 `PortError` 分类 | Protocol 只使用 stdlib/domain 类型，成功空结果与 unavailable 分开 | 扩展 geocoding/COMID/topology 并增加 catalog/availability 边界；不导入 infrastructure/API |
| Adapters | composition root 注入 infrastructure 实现 | 每类 provider 的 production adapter 负责远端协议校验，application 只消费 typed port result | 新增独立 geocoding、NLDI、WDFN metadata adapters；技术失败不得伪装为空结果 |
| Registry | `create_production_registry()` | production registry 是可写 content type/version 的唯一目录 | 原位注册 parser/context/topology typed contents；fixture 显式注入测试类型 |
| Reader | `TranscriptReader` | 持久化原 item 是 history/detail/full-content 的事实源 | 只从原 `AnalysisContext`/`HydrologyGraph` 投影 summary/reference，不复制正文 |
| HTTP 与 SSE | analysis run router、SSE response encoder | runtime route/OpenAPI 与两层 SSE encoder 是公开协议来源 | 扩展现有 create/history/snapshot/session process；不新增 M4 route 或第三条流 |
| OpenAPI | `create_app().openapi()` 及现有 documented-protocol tests | 实际 application 生成的 schema 是机器可读事实源 | 每次 schema 改动同步 golden/docs；不手写漂移的第二份 schema |
| Persistence | `analysis_runs`、`analysis_sessions`、`transcript_items`、`transcript_contents` | `schema.py`、Alembic head 与 store contract | M4 不新增业务表；若唯一配置或正文模型确需迁移，只修改现有 schema/fixture |
| Smoke | `health_smoke.py`、`run_sse_smoke.py`、`run_history_smoke.py` 和 `backend/tests/smoke` | 正式 entrypoint/真实 socket/专用 PostgreSQL 测试库组成 production-path 证据 | fake 外部能力保持确定性；公网、provider 或计费 smoke 单独预检并记录 `not run` |
| Docs | M4 decision、implementation design、API、capability catalog、TODO 与中心 deferred contracts | 当前项目文档与 runtime schema 共同约束 MR；冲突必须先消除 | 每个后续 Milestone 记录命令、结果、失败修复、风险和 commit SHA |

## 5. Architecture guard

新增 `backend/tests/architecture/test_archive_isolation.py`，它：

- 只枚举当前 `backend/app`、`backend/scripts`、`backend/tests`、backend package/config 文件和 CI workflows；不会打开或遍历任何归档目录；
- 拒绝 production、测试、依赖/config 或 CI 中出现归档系统引用，因此直接 import、路径 open、目录 enumeration 和 path dependency 都会失败；
- 用独立负例证明上述四类引用都会被门禁发现，并保留不相关名称的正例；
- 锁定 `pyproject.toml` 的 pytest `testpaths = ["tests"]`；
- 锁定所有 CI pytest command 显式收集 `backend/tests`；
- 与既有 layer dependency、domain dependency、port purity，以及“只有 coordinator 可直接导入 `RunRuntime`”门禁共同运行。

## 6. 验证记录

| 验证 | 结果 |
|---|---|
| 未改代码的 `python -m pytest backend/tests -q` | 849 passed；1 failed、15 errors 均来自专用 PostgreSQL endpoint `connection timeout expired`，无其他失败 |
| 不依赖真实 PostgreSQL 的完整 backend 回归 | `858 passed in 30.13s` |
| 新归档隔离 focused tests | `9 passed in 0.13s` |
| `python -m pytest backend/tests/architecture -q` | `75 passed in 0.86s` |
| `python -m pytest backend/tests/smoke -q` | `20 passed in 12.30s`，使用显式 in-memory store 的真实 HTTP/SSE socket 测试 |
| `python -m compileall -q backend/app backend/scripts` | passed |
| `git diff --check` | passed |

真实 PostgreSQL 测试、正式 health smoke、run SSE script 和 run/history/restart smoke 本次为 `not run`：配置文件和专用测试库名存在，但数据库 endpoint 连接超时。没有修改测试来绕过基础设施，也没有把零次成功连接记录成通过。

## 7. 阶段出口与接力

- 当前新后端扩展接缝、单一 ownership 和四表边界已定位。
- production/test/config/CI 的归档隔离由可执行 architecture guard 固定。
- M4 后续只扩展当前 request/configuration、workflow、ports、registry、reader、HTTP/SSE 和四表正文；不引入阶段兼容层或第二套运行系统。
- Milestone 1 可以从最终 request、唯一 configuration 和 model catalog 开始；本分支未提前实现其中任何业务行为。
- Applicable contracts：GLOBAL-C01、GLOBAL-C02、MR-G02、MR-G06、MR-G07、MR-G10、MR-G12、MR-G13，以及 M4-C01 至 M4-C13 的接缝盘点；M4 功能 contracts 仍为未实现。
