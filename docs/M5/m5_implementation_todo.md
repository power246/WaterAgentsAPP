# M5 Evidence Tools + EvidenceBundle Implementation TODO

更新日期：2026-10-01

> 当前状态：Milestone 0–5 已完成，Milestone 6–13 尚未开始。本文中的 checkbox、验证命令、contract 状态和提交位置只有在实际证据支持时才表示完成。M5 是 M1–M9 同一最终系统中的内部实施切片，不形成独立发布版本；没有实际运行证据的项目必须保持未勾选或记录为 `not run` / `partial`。

本清单执行以下已确认文档：

- [M5 设计决策 D01–D19](m5_evidence_tools_design.md)
- [M5 实施设计](m5_evidence_tools_implementation_design.md)
- [M5 Evidence Policy Catalog](m5_evidence_policy_catalog.md)
- [M5 API / 读取契约](m5_evidence_tools_api.md)
- [跨 Milestone M5 契约](../deferred_milestone_contracts.md#7-m5evidence-tools--evidencebundle)
- [M4 Implementation TODO 的 M5 接力出口](../M4/m4_implementation_todo.md#m5-接力出口)

若本清单与上述设计冲突，coding agent 不得自行选择实现成本更低的一项，也不得用兼容层同时保留两套行为；应先在同一变更中把当前唯一 V1 文档和契约修正为一致，再继续代码实现。内部函数拆分、文件粒度和 fixture 组织可由实现者决定，但不能改变已冻结的 scope、provider、类型、状态、写入顺序、门禁输入、失败语义或 M6–M8 接力边界。

## 阶段与设计导航

| 实施阶段 | 主要设计依据 | 阶段产物 |
|---|---|---|
| [Milestone 0：基线、边界与接缝盘点](#milestone-0基线边界与接缝盘点) | [实施设计 §1–3](m5_evidence_tools_implementation_design.md#1-核心结论与范围) | 可复现基线、影响表、归档隔离和真实扩展接缝 |
| [Milestone 1：当前唯一 V1 evidence domain](#milestone-1当前唯一-v1-evidence-domain) | [实施设计 §4](m5_evidence_tools_implementation_design.md#4-当前唯一-v1-domain-模型)；[API §2–7](m5_evidence_tools_api.md#2-统一-transcript-envelope) | SourceRecord、typed payload、cell、bundle、collection manifest |
| [Milestone 2：154 项 manifest 与 policy registry](#milestone-2154-项-manifest-与-policy-registry) | [Policy §2](m5_evidence_policy_catalog.md#2-manifest-原子扩展)；[Policy §11](m5_evidence_policy_catalog.md#11-154-项配置验收) | 原子扩展 manifest、稳定 digest、完整离线审计 |
| [Milestone 3：确定性 evidence policy 引擎](#milestone-3确定性-evidence-policy-引擎) | [Policy §3–10](m5_evidence_policy_catalog.md#3-单位定义与换算) | 单位、质量、coverage、聚合、applicability、RAG candidate |
| [Milestone 4：LineString geometry 与 corridor planner](#milestone-4linestring-geometry-与-corridor-planner) | [实施设计 §7](m5_evidence_tools_implementation_design.md#7-geometry-扩展)；[API §8](m5_evidence_tools_api.md#8-geometry-对-topology_ready-v1-的原子扩展) | 当前 V1 geometry、简化、空间校验、设施 corridor 计划 |
| [Milestone 5：provider-neutral ports 与 collection completeness](#milestone-5provider-neutral-ports-与-collection-completeness) | [实施设计 §5/§9/§11](m5_evidence_tools_implementation_design.md#5-tool-registry-与执行契约) | typed ports、scope/request plan、completion proof、统一错误 |
| [Milestone 6：USGS observation 与 discharge adapters](#milestone-6usgs-observation-与-discharge-adapters) | [实施设计 §6.1–6.3](m5_evidence_tools_implementation_design.md#61-usgs-continuousdaily) | OGC continuous/daily、Samples CSV、discharge 实际记录 |
| [Milestone 7：NWS weather 与 EPA facility adapters](#milestone-7nws-weather-与-epa-facility-adapters) | [实施设计 §6.4–6.6](m5_evidence_tools_implementation_design.md#64-nws-weather) | NWS discovery/observation/forecast、ECHO corridor collection |
| [Milestone 8：tool registry、executor 与 run-local single-flight](#milestone-8tool-registryexecutor-与-run-local-single-flight) | [实施设计 §5/§9.2/§11](m5_evidence_tools_implementation_design.md#5-tool-registry-与执行契约) | 八工具 roster、统一 executor、预算、取消、dedup、inspection |
| [Milestone 9：source-to-bundle 构造链](#milestone-9source-to-bundle-构造链) | [实施设计 §2/§8–10](m5_evidence_tools_implementation_design.md#2-执行流程) | source record 到 fragment/cell/reference-only bundle 的完整闭包 |
| [Milestone 10：typed content、持久化、读取与两层 SSE](#milestone-10typed-content持久化读取与两层-sse) | [实施设计 §10](m5_evidence_tools_implementation_design.md#10-transcript-与写入顺序)；[API §2–12](m5_evidence_tools_api.md#2-统一-transcript-envelope) | 五类内容、writer/reader、preview/reference、session SSE |
| [Milestone 11：station-backed / stationless acquisition contract](#milestone-11station-backed--stationless-acquisition-contract) | [设计 D15–D16](m5_evidence_tools_design.md#d15有站水体先验证真实观测无站水体消费上游并按需取证) | preflight、全空停止、部分继续、逐窗 discharge、延后 producer 槽位 |
| [Milestone 12：production composition 与独立 test app](#milestone-12production-composition-与独立-test-app) | [实施设计 §12](m5_evidence_tools_implementation_design.md#12-测试接口与验证策略)；[API §13](m5_evidence_tools_api.md#13-仅测试-http-调用面) | production runtime、无公开 tool route、独立可测真实执行链 |
| [Milestone 13：集成验收、契约审计与 M6–M8 接力](#milestone-13集成验收契约审计与-m6m8-接力) | [实施设计 §13–14](m5_evidence_tools_implementation_design.md#13-m6m7m8-接力) | fake 端到端、可选 provider smoke、契约矩阵、接力证据 |

## Coding agent 执行原则

- [ ] 只以当前 `backend/`、`docs/M1`–`docs/M5` 和项目级当前契约为实现输入。`backend_bk/`、`frontend_bk/`、`docs/backend_refactor_contract.md` 是完全不相关、最终丢弃的归档；不得读取、导入、复制、适配、枚举、运行或把其中行为当测试 oracle。
- [ ] 开始每个 Milestone 前记录当前 branch、HEAD、`origin/main`、merge-base、`git status` 和已有用户改动。不得清理、覆盖、回退或顺手提交不属于本阶段的文件。
- [ ] 一个 Milestone 只交付表中一个可观察目标。先写或更新会失败的测试，再做最小 production 实现，再跑 focused gate；阶段出口未满足时不得进入下一阶段，也不得勾选项目。
- [ ] 每个 Milestone 原则上形成一个可独立审查的提交。提交前运行适用 focused tests、architecture tests、`compileall` 和 `git diff --check`；未经用户要求不 push、不创建 PR、不改写历史。
- [ ] 直接原子更新当前唯一 V1 的 domain、manifest、typed content、fixture、reader 和调用方。不得新增 `EvidenceFragmentV2`、`EvidenceBundleV2`、旧新 read union、migration branch、milestone feature flag、双写或临时兼容入口。
- [ ] 继续复用 M3 的 run/session identity、四表、writer/reader、typed-content registry、全文引用、两层 SSE、取消、补写、启动修复和 live cleanup。不得建立第二套事实库、历史、重试队列、session 状态或第三条 SSE。
- [ ] M5 扩展 M4 的唯一 capability manifest、`AnalysisContext`、`HydrologyGraph`、station/series refs 和 `topology_ready v1`；M4 availability 只是候选资格，不能直接变成 SourceRecord 或 EvidenceFragment。
- [ ] M5 只构造 evidence 和 M6 gate 输入，不输出最终水质结论、confidence、attribution 或 `InferenceResult`，不调度 DAG，也不为尚未准备执行的水体预建 session。
- [ ] production 不公开直接 tool invocation route，当前 frontend 不改动。独立 test app 必须通过单独 composition 显式创建，不能由 production runtime flag 开启，也不能绕过 scope、writer 或授权。
- [ ] Agent-facing 请求不接受 provider、base URL、secret、任意 COMID、station、geometry、中心点或自由时间范围。server 必须从已保存的 context/topology/capability/station facts 生成 provider plan。
- [ ] 当前 Agent 只主动查询自身 `agent_comid`。上游 station 数据只能由上游 session 的结果引用传递；不得让下游 Agent 直接查询上游站点。provider 查询范围最多扩展到目标范围前后各一个当地日，且不新增结果窗口。
- [ ] discharge 对每个实际执行水体、每个最终窗口都是 required support。有站先查询直接 discharge；无站或本地缺失时只接受上游结果或 registry 登记的估算关系。未知 discharge 不能写成 0。
- [ ] weather observation/forecast 始终按需；prediction mode 不自动使天气必查。设施邻近与历史 RAG 默认 `contextual_only`，不能解锁 target/discharge gate。
- [ ] adapter 只返回强类型 SourceRecord 和 completion facts；quality、unit、coverage、aggregation、applicability、RAG candidate、fragment/cell/bundle 构造权统一属于 application policy/tool 层。
- [ ] provider success-empty 与 timeout、auth、rate limit、invalid response、超限、分页/stream 不完整必须分开。技术失败不得变成 `empty`；已解析事实不删除，但受影响窗口必须 blocked/`incomplete_source`。
- [ ] 单位与数值算法只使用 manifest 驱动的 `Decimal` 确定性实现。不得让 Agent 提供换算系数；不得按参数名称散落 if/else；不得插值、补值、替换异常值、跨 series 合并或在 M5 预测数值。
- [ ] `suspicious` 可按 capability policy 参与聚合并传播 flags/counts；`invalid` 只保留审计。M5 不自动检测跨 series 冲突，也不保留 `CONFLICTING_OBSERVATIONS` 兼容枚举。
- [ ] 只有一对一 local direct record 加可逆展示换算具备后续 `observed` 资格；任何多记录 aggregate、跨 series 选择、上游、天气、设施或历史支持均只能成为 inferred 候选。
- [ ] 外部真实 provider smoke 不是单元测试前置条件。先用 checked-in fixtures 和 fakes 完成确定性覆盖；需凭据、联网或可能计费的 smoke 必须先 preflight，0 请求、缺凭据或网络不满足一律记录 `not run`，不能写成通过或零结果。
- [ ] 实际完成记录必须写清改动路径、命令、环境、pass/fail/skip/not run 数、失败后的修复、剩余风险和 commit SHA。不得复制旧阶段 pass count，不得用 exit code 0 代替业务断言。

## M5 完成标准

- [ ] 当前唯一 V1 已包含完整 SourceRecord 判别联合、typed EvidencePayload、EvidenceCell、CollectionManifest 和 reference-only EvidenceBundle；不存在旧 `value_or_summary` 兼容读取。
- [ ] 154 项 capability 的 unit/channel/value-semantics/quality/coverage/aggregation/relation policy 全部来自同一 manifest，集合与 M4 key/hash 不漂移，规范 digest 字节稳定。
- [ ] Decimal 单位换算、八种固定聚合算法、quality、coverage、series identity、applicability 和 RAG candidate policy 均有正反 fixture；明显错误组合被拒绝。
- [ ] NLDI LineString 在当前 V1 中保存一次并由 node 摘要引用；validator、geodesic length、bbox、representative point、简化和完整 corridor 规划均确定性成立。
- [ ] USGS OGC continuous/daily、USGS Samples CSV、direct discharge、NWS weather 和 EPA ECHO facility adapters 都有 checked-in 官方 contract fixtures；无 silent fallback。
- [ ] 八个 evidence tools 共用同一 registry、executor、scope、budget、dedup、writer、error mapping 和 result schema；M7/M8 producer 未接入时不向 production Agent 暴露假可用工具。
- [ ] 五类 M5 typed content 可写入、读取、补写、全文引用并由既有 session SSE 交付；公开投影不泄露 secret、raw provider envelope、无界 records 或内部异常。
- [ ] station-backed 全空、部分 evidence、provider failure 与 stationless upstream/optional 四条主路径均有 fixture；逐 ParameterKey × window 的 target/discharge cells 稳定区分 supported/unsupported/blocked。
- [ ] 全部 usable direct evidence 为空的 station-backed preflight 形成真实 call/result/facts/bundle 和停止指令，不调用水体模型；技术失败不走自然全空完成路径。
- [ ] run-local exact-request single-flight、success reuse、failed/cancelled 不缓存、跨 run 不复用、bounded retry/cancel/budget 和 partial-provider failure 均有并发测试。
- [ ] production OpenAPI 不含 tool/test route；独立 `create_m5_test_app()` 可通过 fixture scope 复用真实 executor，frontend 没有接入。
- [ ] M1–M4 regression、M5 focused tests、architecture guards、离线 manifest/doc/fixture audit、`compileall` 和 `git diff --check` 有实际通过证据；外部、PostgreSQL和三平台未执行项如实记录。

## Milestone 0：基线、边界与接缝盘点

### 工作

- [x] 记录 branch、HEAD、`origin/main`、merge-base、工作区已有改动、Python/依赖版本和可用测试入口；确认不覆盖用户改动。
- [x] 只盘点当前新后端接缝：evidence domain、M4 capability registry、HydrologyGraph/NLDI adapter、M3 writer/reader/registry/SSE、settings、composition root、ports 和现有 fakes。
- [x] 形成 M5 影响表，至少覆盖 domain、manifest/config、application policy、ports、provider adapters、runtime、transcript registry、persistence/API projection、test composition、scripts 和 docs；每类事实只指定一个 source of truth。
- [x] 记录当前 M1–M4 full regression、architecture、PostgreSQL 和 smoke 基线；环境缺失时写 `not run`，不修改测试绕过基础设施。
- [x] 增加或确认 architecture guard：production/test 不依赖归档；domain/ports 不导入 infrastructure/FastAPI/SQLAlchemy；production import graph 不含 test router/composition。
- [x] 扫描当前 `EvidenceFragment`、`EvidenceBundle`、`DataQualityFlag`、`JsonValue/value_or_summary` 和 capability manifest 的所有 production/test/docs 消费者，形成原子更新清单。
- [x] 确认 M5 不新增业务表。若 typed content 或大正文需要 schema 变更，先证明现有通用存储不能承载；默认只扩展内容模型、registry 和 fixture。

### Tests

- [x] 当前 `backend/tests` 基线无未解释失败；PostgreSQL 用例仅使用 `wateragents_test` 安全门禁。
- [x] architecture tests 锁定 layer dependency、port purity、archive isolation、单一 writer/reader、两层 SSE 和 production/test import isolation。
- [x] current OpenAPI、typed registry、M4 manifest audit 和 M4 workflow smoke 的基线结果被记录。

### Verify

- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests -q`（无法运行的 PostgreSQL/外部项明确拆出并记录）
- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/architecture -q`
- [x] `backend/.venv/Scripts/python.exe -B -m compileall -q backend/app backend/scripts`
- [x] `git diff --check`

阶段出口：真实扩展接缝、原子更新面和基线证据可复现；归档完全不在实现输入中；本阶段不新增 M5 业务行为。

提交边界：只允许 architecture guard、必要的基线测试修正、影响表和本清单完成记录；不得提前修改 evidence model、manifest 或 provider。

#### Milestone 0 影响表与原子更新清单

| 影响面 | 当前唯一 source of truth | M5 后续原子更新面 |
|---|---|---|
| Evidence domain | `app/domain/evidence/` 与 `app/domain/__init__.py` | 同步替换 `EvidenceFragment`、`EvidenceBundle`、`DataQualityFlag`、exports、domain/result tests；不保留 `value_or_summary` 或旧 bundle 读取分支。 |
| Capability manifest/config | `app/infrastructure/config/parameter_capabilities.v1.json`，由 `parameter_capabilities.py` 装载为 `ParameterCapabilityRegistry` | 在同一 manifest/model 中扩展 unit/channel/quality/coverage/aggregation/relation policy；保持 154 key 集合与 digest 基线。 |
| Topology/geometry | `app/domain/hydrology/`、`application/hydrology_topology/` 与 `infrastructure/adapters/nldi.py` | 原子更新 `RiverSegment`、`HydrologyNode`、`HydrologyGraph`、`topology_ready v1`、NLDI fixtures/consumers；完整 LineString 只保存一次。 |
| Application policy/tools | 当前尚无 M5 实现；既有流程入口为 `application/analysis_runs/` | 新增唯一 evidence policy/tool executor、collection/bundle/inspection 链；不建立第二套 workflow、writer 或事实库。 |
| Ports/provider adapters | `app/ports/` 的 Protocol 与 `app/infrastructure/adapters/` | ports 只使用 domain DTO；USGS/NWS/EPA adapters 只解析官方协议并返回 SourceRecord/completion facts，失败不改写为 empty。 |
| Runtime/settings | `infrastructure/config/settings.py`、`infrastructure/runtime/` 与 `app/main.py` | production composition 只装配内部 M5 能力，不注册 tool/test route；预算默认值只在 Settings 定义一次。 |
| Transcript/read/write/SSE | `application/transcripts/registry.py`、`application/analysis_runs/writer.py`、`application/transcripts/reader.py` 与现有 run/session SSE | 五类 M5 typed content 复用单一 registry/writer/reader、全文引用和两层 SSE，不增加第三条流。 |
| Persistence/API projection | 四表 `infrastructure/persistence/schema.py` 与现有 transcript/API projection | 通用 `content_type + schema_version + inline/reference body` 已能承载 M5；Milestone 0 确认不新增业务表或 migration。 |
| Test composition/fakes | `backend/tests` 当前 fixtures/fakes；production app 为 `app.main:create_app` | 独立 M5 test composition 复用真实 executor；`test_production_test_isolation.py` 阻止 production 导入 test-only module，并锁定单一 reader/writer 与两条 SSE。 |
| Scripts/docs | `backend/scripts/`、`docs/M1`–`docs/M5` 与项目级当前契约 | 离线 audit/fixture smoke 为默认门禁；真实 provider/PostgreSQL 仅 opt-in 并记录 `not run`；完成证据只回写本 TODO。 |

现有消费者扫描覆盖 production、tests 与 `docs/M1`–`docs/M5`：`EvidenceFragment`/`EvidenceBundle` 主要由 domain exports、evidence/result tests 和边界文档消费；`DataQualityFlag` 还被 `TimeWindowResult` 消费；`JsonValue` 是通用 transcript/persistence JSON 类型，M5 只移除 evidence payload 对它的依赖；capability manifest 由 loader/registry、resolver/runtime、OpenAPI、audit 与 fixtures 共同消费。后续 Milestone 必须一次同步上述消费者，不能留下旧字段兼容分支。

#### Milestone 0 完成记录（2026-09-30）

- 实现：从 `origin/main` 的 `628518e` 创建 `codex/m5-evidence-tools-m0`，初始 tracked worktree/index 均无改动；盘点当前 backend 接缝并新增 production/test import isolation、单一 transcript writer/reader 和两层 SSE 架构守卫。Python `3.12.14`、pip `26.2.1`、FastAPI `0.141.1`、Pydantic `2.13.5`、SQLAlchemy `2.0.54`、pytest `9.1.1`；未修改 evidence model、manifest、provider、数据库 schema 或 production runtime。
- 验证：首次原始 full command 为 `1150 passed, 2 failed, 45 errors`，失败均来自默认 pytest 临时目录 ACL 或当时未设置 `WATERAGENTS_TEST_DB_NAME`；沙箱外非 PostgreSQL回归为 `1185 passed`、architecture 为 `97 passed`、OpenAPI/typed-registry/manifest focused 为 `28 passed`。复查发现 Docker Desktop 的 `run/dockerInference` reparse point 损坏；确认进程/服务停止后将原 `run` 保留为 `run.codex-stale-20260930-m5-m0` 并重建空运行目录，未触碰 volume。随后通过项目 dotenv 与进程级 `wateragents_test` 验证 persistence `57 passed`、含 PostgreSQL 的完整 backend `1202 passed`、health `healthy`、history/restart/startup-repair 全部通过；`compileall`、154 项离线 audit、M4 workflow smoke、run SSE smoke 与 `git diff --check` 通过。真实 provider 和最新三平台 CI 为 `not run`。
- 风险：Milestone 3–13 尚未开始；Milestone 1–2 已原子替换 evidence domain 并扩展唯一 capability manifest，NLDI LineString 仍只提取代表点，policy executor、五类 M5 typed content 与 provider execution 均未实现。
- 提交：本地实现提交 `3d9235c`（`feat(m5): complete milestones 0-2`）；未 push。

## Milestone 1：当前唯一 V1 evidence domain

### 工作

- [x] 定义 `WaterObservationRecord`、`DischargeObservationRecord`、`WeatherObservationRecord`、`WeatherForecastRecord`、`FacilityRecord`、`UpstreamResultRecord`、`HistoricalTranscriptRecord` 的判别联合和共享 provenance/location/time 字段。
- [x] 对无 provider stable ID 的记录使用规范字段 hash 生成稳定 identity；禁止仅按时间和值猜重复，保留 page/channel provenance。
- [x] 用 `DirectRecordPayload`、`AggregatePayload`、`ConversionPayload`、`ContextPayload`、`CollectionFactPayload`、`UpstreamResultPayload`、`HistoricalInferencePayload` 原子替换 `value_or_summary: JsonValue`。
- [x] 扩展唯一 `EvidenceFragment`：typed payload、applicability、relation、series identity、coverage、quality class/flags、source refs 和 derivation；删除当前 V1 中被替换字段与 `CONFLICTING_OBSERVATIONS`。
- [x] 实现 `EvidenceCell` 的 parameter/window/role、三类 fragment refs、collection facts 和 supported/unsupported/blocked 语义。
- [x] 实现 reference-only `EvidenceBundle`、`CollectionManifest` 和 tool request/result/safe error models；tool execution 与 collection status 使用两组独立 enum。
- [x] 为全部模型实现严格 invariant、稳定序列化、reference extraction、canonical decimal/time 表示和未知/损坏内容错误，不以 raw dict 作为跨层主契约。
- [x] 同步当前 production/test/docs 消费者，删除旧字段、旧 enum 和旧 fixture；不保留 V1/V2 union 或 fallback deserializer。

### Tests

- [x] 每个 SourceRecord 分支有最小正例、未知 discriminator、缺字段、额外字段、非法时间/location/unit/identity 反例。
- [x] fragment payload 与 applicability/relation/quality/coverage 组合 invariant 完整；collection fact 不伪装 quantified evidence。
- [x] bundle 校验每个 parameter × window × role 恰有一个 cell、引用去重/scope 一致、closure 完整，并要求每个 target cell 存在同窗 discharge cell。
- [x] execution `completed/failed/cancelled` 与 collection `available/empty/unavailable/invalid/null` 的合法组合有矩阵测试。
- [x] 不存在 `EvidenceFragmentV2`、旧字段 read union、`CONFLICTING_OBSERVATIONS` 或 milestone feature flag。

### Verify

- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/domain/test_evidence.py backend/tests/domain/test_serialization.py -q`
- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/architecture -q`
- [x] `rg -n "value_or_summary|EvidenceFragmentV2|EvidenceBundleV2|CONFLICTING_OBSERVATIONS" backend/app backend/tests`

阶段出口：所有后续 M5 组件只能消费当前唯一的 typed V1 models；尚未扩展 manifest、调用 provider 或写 transcript。

提交边界：只包含 domain model、serialization/reference contracts、原子调用方迁移和 focused tests；不得夹带 adapter、executor、runtime 或 HTTP。

#### Milestone 1 完成记录（2026-09-30）

- 实现：在当前唯一 V1 中加入七类 `SourceRecord` 与稳定 identity、七类 typed payload、严格 `EvidenceFragment`、`EvidenceCell`、reference-only `EvidenceBundle`、`CollectionManifest`、tool request/result/safe error、规范 decimal/time 序列化和稳定内容错误；同步 domain exports、当前 result 消费者及 M1/M3/M5 契约文档。未新增 V2/read fallback，也未实现 manifest policy、provider adapter、executor、runtime、HTTP 或 transcript registry。
- 验证：Python `3.12.14`；focused evidence/serialization 为 `119 passed`，architecture 为 `98 passed`，使用进程级 `WATERAGENTS_TEST_DB_NAME=wateragents_test` 与 Docker PostgreSQL 的完整 backend 为 `1268 passed`；legacy 名称扫描无 production/test 命中，`compileall`、文档 fence/JSON audit 与 `git diff --check` 通过。真实 provider 与 Windows/Linux/macOS 三平台 CI 为 `not run`。
- 修复与风险：稳定 identity 初测暴露浮点坐标缺少规范表示，已改为有限值规范化并增加回归；受限 pytest 临时目录 ACL 通过显式独立 `--basetemp` 在沙箱外复核。Milestone 3–13 尚未开始，policy executor、真实 provider、geometry、执行与持久化链仍按后续阶段实现。
- 提交：本地实现提交 `3d9235c`（`feat(m5): complete milestones 0-2`）；未 push。

## Milestone 2：154 项 manifest 与 policy registry

### 工作

- [x] 在当前 `ParameterCapabilityManifest(schema_version=1)` 原子加入 unit definitions、quality/aggregation/coverage profiles、evidence relations 和 capability/channel policy refs；保持 154 个 ParameterKey 集合和 M4 catalog identity 不变。
- [x] 定义全局 `UnitDefinition` registry：dimension、alias、linear/affine/identity-only、十进制 scale/offset、precision；pH 使用 identity-only 语义。
- [x] 定义共享 value-semantics、quality、coverage 和 aggregation profiles；参数只引用命名 profile，极少 override 必须显式且有 fixture。
- [x] 为每个 capability/channel 登记 provider、channel、series identity fields、accepted source units/statistics、value semantics、默认/允许聚合、quality/coverage profile 和 indirect relations。
- [x] 保证 `usgs:00060` 仍是唯一自动 required support，拥有 direct discharge channel 和允许关系；每项至少有一个 actual observation channel。
- [x] 扩展不可变 registry loader：所有引用存在且无环、unit dimension 兼容、value semantics 支持声明的方法、relation 输入已登记、alias 无非法歧义。
- [x] 规范序列化 manifest 并产生可重现 digest；package installation、不同 cwd 和 Windows/Linux/macOS 路径不改变加载或 digest。
- [x] 扩展离线 audit 和只报告 drift 的 optional refresh；运行时不得自动接受官方新增字段/参数或修改 manifest。

### Tests

- [x] 154 项 key/hash 与 M4 manifest 双向一致；required support 恰好一项且为 `usgs:00060`。
- [x] 每项 canonical/accepted unit、channel、value semantics、quality/coverage/aggregation profile、default method 和 relation refs 完整。
- [x] 未知/循环 profile、dimension mismatch、缺 channel、非法 method/value semantics、重复 alias、未知 relation 和不稳定 digest 启动失败。
- [x] pH、discharge、interval/cumulative precipitation、daily statistic、concentration、count density 各有正反配置 fixture。
- [x] 生成 audit report 在重复运行、不同 cwd 和支持平台上字节稳定。

### Verify

- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/domain/test_parameter_capabilities.py backend/tests/infrastructure/test_parameter_capability_audit.py -q`
- [x] 运行扩展后的离线 manifest audit，输出 `154/154`、唯一 key、required support、profile/relation 引用和稳定 digest。
- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/architecture -q`

阶段出口：所有 M5 policy 都能从唯一 manifest/registry 查询，154 项没有模板空壳或第二份 Markdown allowlist；尚未执行数值转换或 provider collection。

提交边界：manifest schema/data、loader/registry、audit 和 tests；不得夹带 policy executor、provider、workflow 或 API。

#### Milestone 2 完成记录（2026-09-30）

- 实现：原子扩展 `parameter_capabilities.v1.json` 和当前不可变 registry，154 项 capability 全部引用 45 个 unit definitions、2 个 quality profiles、10 个 aggregation profiles、5 个 coverage profiles、5 个 evidence relations 与 211 个 provider/channel policies；`usgs:00060` 仍是唯一 required support。manifest canonical SHA-256 为 `52b840a0908c73c2b1059d363f95beb402686167822f56010befb788274733f7`，optional official refresh 保持只报告 drift。
- 验证：focused manifest/audit 为 `30 passed`，domain 与 M4 parameter/context/station 回归为 `319 passed`，architecture 为 `98 passed`，使用 Docker PostgreSQL `wateragents_test` 的完整 backend 为 `1282 passed`；离线 audit 输出 `154/154`、154 unique/complete、引用状态 `valid`，checksum mismatch 启动失败，zip package 从不同 cwd 读取同一 resource 后得到相同 digest；`compileall`、文档 audit 与 `git diff --check` 通过。
- 修复与风险：首次 domain 回归为 `318 passed, 1 failed`，原因是内部 `counter_delta_sum` 误扩展既有 result `AggregationMethod`；已拆为 M5 专用 `EvidenceAggregationMethod` 并恢复旧枚举稳定值。首次 wheel 命令因路径歧义误解析同名 PyPI 包，显式本地路径后又发现环境缺少 `setuptools.build_meta`，因此 wheel build 为 `not run`；改用标准库 zip package 完成资源隔离验证。Linux/macOS、真实 official refresh 和真实 provider 为 `not run`；数值转换/聚合执行仍属于 Milestone 3。
- 提交：本地实现提交 `3d9235c`（`feat(m5): complete milestones 0-2`）；未 push。

## Milestone 3：确定性 evidence policy 引擎

### 工作

- [x] 实现 unit alias 的 NFKC/trim/casefold/受控空白规范化和精确查表；未知/不同 dimension/不同化学或温度基准返回 `evidence_unit_unresolved`，不模糊猜测。
- [x] 用 `Decimal` 实现 linear/affine/identity-only 换算，至少 28 位中间精度并在输出边界 round-half-even；provenance 保留原值/单位、公式、digest、未舍入值和最终值。
- [x] 实现 provider/channel quality mapping：valid/suspicious/invalid、原 qualifier、unknown qualifier 保守 suspicious、协议结构损坏 invalid；删除跨 series 冲突 detector。
- [x] 实现 coverage profiles，明确 complete/partial/insufficient/unknown、count、duration、ratio 和 largest gap；coverage 不从 quality 或记录数量猜 cadence。
- [x] 实现 mean/median/min/max/sum/p90/latest 和内部 `counter_delta_sum` 的稳定 Decimal 算法、排序、reset 和 overlap 规则。
- [x] 聚合前严格校验同 series identity、parameter/provider/channel/station/medium/fraction/method/statistic/value semantics、单位、窗口相交、collection completeness 和 profile threshold。
- [x] suspicious 输入允许聚合并把全部 flags、valid/suspicious/invalid counts 和真实 coverage 传播到 derived fragment；invalid 输入不参与。
- [x] 实现 direct/allowed-indirect/contextual relation resolver；未登记 relation 和 facility/history 默认 contextual，Agent 不能覆盖 applicability。
- [x] 实现内容层 `rag_eligible` 固定 policy；raw/error/empty/unavailable/invalid/cancelled/incomplete/bundle/inspection false，合格 reference-complete fragment 仅标 candidate。

### Tests

- [x] 温度/流量/浓度/长度/降水等正向换算、双向 round-trip、alias 和 Decimal 边界确定；pH 不与普通 dimensionless 互换。
- [x] pH sum、流量读数 sum、浓度 sum、累计降水直接 sum、重叠 interval total、跨 station/method/fraction/statistic series 聚合均拒绝；合法 interval-total sum 保持可用。
- [x] mean/median/p90 nearest-rank/latest tie/counter reset/partial coverage 的排序与输出字节稳定。
- [x] suspicious aggregate 传播 flags/counts；invalid 仅审计；M5 不产生 conflict flag、不自动选 winner。
- [x] direct 判定检查 current COMID/station ref、parameter/window、method/medium/fraction/statistic、quality、collection completion 和 closure。
- [x] RAG candidate 正反矩阵不改变 applicability，历史命中不能满足当前 target/discharge gate。

### Verify

- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/application/evidence_policy backend/tests/domain/test_evidence.py -q`
- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/infrastructure/test_parameter_capability_audit.py -q`
- [x] `rg -n "float\(|CONFLICTING_OBSERVATIONS|conflicting_observations" backend/app/application/evidence_policy backend/app/domain/evidence`

阶段出口：给定 typed records、manifest 和窗口，可纯确定性得到单位、质量、coverage、聚合、applicability 与 candidate 结果；尚未访问网络或写 transcript。

提交边界：policy application/domain、fixtures 和 tests；不得夹带 HTTP adapters、executor orchestration、runtime 或 routes。

#### Milestone 3 完成记录（2026-09-30）

- 实现：新增纯 application policy 包，完成精确 unit alias/Decimal 换算、provider quality、五类 coverage、八种稳定聚合、direct/allowed-indirect/contextual resolver 与固定 RAG candidate policy；manifest 为 154 项 capability 的 211 个 channel policy 补齐 accepted methods，并显式登记 valid quality tokens 与 partial-coverage aggregation policy。当前 canonical manifest SHA-256 为 `657ca1d3c59780090e1ebf8af3d5b0cdea919f2241e49c871d1d306cf4842d66`。
- 验证：M3 policy + evidence focused 为 `148 passed`，manifest audit focused 为 `3 passed`，domain/application 回归为 `774 passed`，architecture 为 `98 passed`；使用 Docker PostgreSQL 与进程级 `WATERAGENTS_TEST_DB_NAME=wateragents_test` 的最终完整 backend 为 `1332 passed`。离线 audit 为 `154/154`、211 channel policies、引用闭包 `valid`；`compileall`、禁用名称扫描和 `git diff --check` 通过。
- 修复与风险：首轮 focused 为 `174 passed, 1 failed`，失败来自 interval `TimeRange` 无排序运算，已改为显式 `(start, end)` 排序并增加非重叠/重叠回归。真实 provider、Linux/macOS CI 为 `not run`；Milestone 4–13 尚未开始，网络 adapter、executor、runtime、HTTP 与 transcript 写入均未夹带。
- 提交：本地实现提交 `a71a210`（`feat(m5): complete milestone 3 evidence policy`）；未 push。

## Milestone 4：LineString geometry 与 corridor planner

### 工作

- [x] 新增当前 V1 `WaterbodyGeometry`，包含 full/simplified coordinates、representative point、bbox、geodesic length、source COMID 和 simplification tolerance。
- [x] 原子扩展 `RiverSegment`、`HydrologyNode`、`HydrologyGraph`、NLDI adapter 和 `topology_ready v1`；完整 geometry 在原 typed content 的 map 中保存一次，node 只保存 ref 与有界摘要。
- [x] 校验 WGS84 lon/lat、至少两个不同有限点、LineString-only；拒绝 empty、全重复、MultiLineString 和不支持 geometry。
- [x] 确定性计算 geodesic length、bbox、representative point；Douglas-Peucker 从 25 m 开始，最多 256 点且 tolerance 不超过 250 m，不能满足时明确失败。
- [x] 所有实际空间判定使用 full LineString；simplified polyline 只用于 Agent 摘要和 point-query planning。
- [x] 实现 `[1,20] mile` Agent facility radius 校验、provider 支持半径向上取整、完整 corridor geodesic sampling 和最多 32 centers 的可行性计算。
- [x] facility tool descriptor 向 Agent 提供当前水体 length/bbox/有界 simplified polyline、半径上下限和各候选半径的预计 provider request 数；Agent 据此选择半径，但不能提交中心点或替换 geometry。
- [x] 所选半径超过 center budget 时在请求前返回 `facility_radius_too_small_for_budget` 与确定性 minimum feasible radius；不得截断河段或退化到 representative point。
- [x] 实现 facility point 到 full LineString 最短距离、按 Agent 精确半径最终过滤和 facility/permit identity 去重。
- [x] geometry 缺失不阻止已授权 station observation；facility tool 返回 `waterbody_geometry_unavailable`。

### Tests

- [x] 短线、长线、弯曲线、跨经线、高纬度、重复点、非法坐标、空线和 MultiLineString fixture。
- [x] full geometry 只保存一次，所有 node ref/summary 可解析，M4 topology 排序和稳定序列化不回退。
- [x] 简化满足点数/tolerance/确定性边界且不被用于最终距离判定。
- [x] radius 1/20/越界、provider radius round-up、32 center 临界值、minimum feasible radius 和无截断覆盖可重现。
- [x] 多圆重复 facility 去重、边界内外 exact line distance、缺坐标和缺 geometry 行为正确。

### Verify

- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/domain/test_hydrology_topology_models.py backend/tests/application/hydrology_topology backend/tests/infrastructure/test_nldi_adapter.py -q`
- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/smoke/test_m4_workflow_smoke.py backend/tests/architecture -q`
- [x] 导出/读取 `topology_ready v1` fixture，核对 full geometry 单份保存和 summary refs。

阶段出口：当前唯一 topology V1 已提供可验证的水体形状与有界 Agent 摘要，corridor plan 可离线确定；尚未调用 ECHO 或其他 evidence provider。

提交边界：geometry domain、NLDI parsing、topology V1 原子迁移、corridor planner 和 tests；不得夹带 facility HTTP、tool executor 或 frontend。

完成记录（2026-09-30）：

- 实现：新增 WGS84 LineString geometry、geodesic 派生值、支持跨日期变更线的 bbox、25–250 m/256 点确定性简化；NLDI、segment/node/graph 和当前唯一 `topology_ready v1` 已原子贯通，完整 geometry 仅在 graph map 保存一次。新增由已保存 graph 派生的 facility descriptor/plan、`[1,20] mile` 精确半径、provider round-up、32-center 完整 corridor、预算前置错误、full-line 距离过滤与 identity dedup；未加入 facility HTTP、tool executor 或 frontend。
- 验证：清单 focused 为 `28 passed`，smoke + architecture 为 `100 passed`，geometry/corridor/API 扩展 focused 为 `62 passed`；Docker PostgreSQL healthy，使用进程级 `WATERAGENTS_TEST_DB_NAME=wateragents_test` 的最终完整 backend 为 `1363 passed`，health 与 run-history smoke 均通过。受版本控制的 `topology_ready_v1_geometry.json` 已完成读取、导出、再读取，并校验完整 geometry 单份保存和 node 引用闭包；`compileall` 与 `git diff --check` 通过。
- 修复与风险：首轮完整 backend 的唯一失败来自 M4 文档中的旧 `topology_ready v1` JSON 未包含原子新增字段，已同步更新现行文档 fixture 后全绿。真实 NLDI/ECHO provider、Linux/macOS CI 为 `not run`；本项只是 M1–M9 原子任务中的实现切片，正式整体验收仍在 Milestone 9 完成后开始。
- 提交：本地实现提交 `fcd2653`（`feat(m5): complete milestone 4 geometry`）；未 push。

## Milestone 5：provider-neutral ports 与 collection completeness

### 工作

- [x] 定义 `WaterObservationPort`、`WeatherEvidencePort`、`FacilityEvidencePort`、`UpstreamResultPort`、`TranscriptEvidencePort`，只使用 stdlib/domain DTO 和类型化 port errors。
- [x] 定义不可伪造的 `EvidenceSessionScope`，只从已保存 context/topology/session facts 装配 agent COMID、窗口、参数、station/series refs、geometry refs、memory mode 和 cancel token。
- [x] 实现 scope authorizer 和 provider plan builder：拒绝任意 COMID/station/geometry/URL、非当前水体站点、未登记 parameter/channel 和超出 ±1 当地日的时间。
- [x] 实现 DST-aware local-calendar-day query envelope，转换为 UTC `[start,end)`；扩展记录不生成额外 result windows。
- [x] 定义 provider-neutral page/chunk response、request/page/record/byte counts、completed ranges、affected windows 和 completion proof。
- [x] collection completeness 与 observation coverage 分离；只有官方 next-chain/完整 CSV/single-response contract 正常结束才能证明 complete。
- [x] 建立统一 error mapping：empty、unavailable、invalid、auth、rate limit、timeout、response too large、incomplete、scope、unit、aggregation、geometry、producer unavailable。
- [x] 在 settings 中一次解析默认 limits：20 s/request、2 attempts、90 s/waterbody、12 collection calls、24 transforms、10 pages、10,000 records、8 MiB/request、inspection 50/200、preview 32 records/16 KiB、facility 32 centers。
- [x] 非法/未知/越界配置启动失败；Agent 不能提高 limit 或用同义拆分绕过 canonical accounting。

### Tests

- [x] scope 只能来自真实 run/session/context/topology，跨 run/session/COMID/station/geometry/window 请求均拒绝。
- [x] DST spring/fall、当地日期 ±1 日、UTC 边界和不新增窗口可由 fake clock/timezone 重现。
- [x] normal complete、successful empty、auth/rate limit/timeout/invalid、page failure 和 cancel 的 port/error 语义不同。
- [x] 失败页只阻断无法证明完整的窗口；独立 complete 窗口继续，已解析 record refs 保留。
- [x] settings 默认值、收紧值、非法值、部署上限与 Agent schema 不泄露 server limits/secret 的边界完整。

### Verify

- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/ports backend/tests/application/evidence_tools/test_scope.py backend/tests/application/evidence_tools/test_collection_manifest.py -q`
- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/architecture/test_port_imports.py backend/tests/architecture/test_application_dependencies.py -q`
- [x] `backend/.venv/Scripts/python.exe -B -m compileall -q backend/app`

阶段出口：fake ports 已能表达合法 scope、provider plan、完整性和全部技术结果；尚未实现真实 provider 或统一 tool executor。

提交边界：ports、scope/plan/completeness models、settings/error mapping 和 tests；不得夹带真实 adapters、transcript writer 或 HTTP test app。

完成记录（2026-10-01）：

- 实现：以五个强类型 provider-neutral port 原子替换 M4 的 observation/weather/facility 占位泛型；新增由已保存 run/session/context/topology facts 封装的 sealed `EvidenceSessionScope`、DST-aware 当地自然日 envelope、scope/provider request 授权、water observation plan、canonical request/budget accounting、三种官方 completion contract、provider-neutral batch/page/count/completed-range 模型、逐窗口 collection completeness 与固定安全错误映射。Settings 一次解析且只允许部署收紧全部 M5 limits；未加入真实 provider adapter、统一 executor、transcript writer 或 HTTP test app。
- 验证：清单 focused 为 `32 passed`，两项 architecture gate 为 `63 passed`，Evidence/Settings 扩圈回归为 `169 passed`；Docker Engine `29.8.1` 与项目 PostgreSQL 17 容器 healthy，使用进程级 `WATERAGENTS_TEST_DB_NAME=wateragents_test` 的最终完整 backend 为 `1399 passed`。health 返回 `{"status":"healthy"}`，run-history 的 history、独立进程重启与启动修复均通过；`compileall` 与 `git diff --check` 通过。
- 修复与风险：架构 gate 首轮唯一失败是新 port docstring 未显式写出 unavailable 语义，已同步契约文字后通过；沙箱内 pytest 临时目录 ACL 拒绝访问，最终 gates 均在沙箱外独立 `--basetemp` 复核。真实 USGS/NWS/EPA provider、Linux/macOS CI 为 `not run`；本项只是 M1–M9 原子任务中的实现切片，正式整体验收仍在 Milestone 9 完成后开始。
- 提交：本地实现提交 `73f8f71`（`feat(m5): complete milestone 5 provider contracts`）；未 push。

## Milestone 6：USGS observation 与 discharge adapters

### 工作

- [ ] 实现 USGS OGC v1 continuous/daily adapter，使用 M4 monitoring-location/time-series identity、ParameterKey 和 UTC interval；只跟随官方 `rel=next` HTTPS link，不猜 offset。
- [ ] `X-Api-Key` 只来自后端配置/header；公开 provenance 使用安全 source URI，不保存/输出 credential 或未脱敏 query。
- [ ] continuous 为 6h/12h/24h 主要通道；daily 只在 channel policy 接受 provider interval statistic 时使用，不把 daily mean/max 当原始瞬时值再次任意聚合。
- [ ] 实现 Samples `/results/narrow` 流式 CSV：固定官方参数与 `mimeType=text/csv`，unknown column 忽略，缺 required column/非法 row/超 byte 或 record limit 为明确 invalid/too-large。
- [ ] direct discharge 复用 observation adapter，但保持 `usgs:00060` 或其他已登记 discharge ParameterKey/statistic；adapter 不自行制造 gage-height/basin estimate。
- [ ] 每页/stream 产生标准化 SourceRecord 与 completion facts；provider envelope 只存在于 checked-in contract fixture，不写 transcript。
- [ ] 实现 bounded retry/cancel：幂等 GET 最多 2 attempts，下一页/retry 前检查 deadline/cancel；技术失败不 fallback、不 stale-cache、不变 empty。

### Tests

- [ ] OGC continuous/daily normal、empty、approved/working、qualifier、unit/time/statistic、next-page、重复 identity 和 malformed fixture。
- [ ] Samples normal/empty/quoted field/unknown column/缺列/非法数值/detection qualifier/byte/record limit/stream failure fixture。
- [ ] 00060 和其他登记 discharge 通道保持参数与统计身份；不存在把估算值伪装 direct 的路径。
- [ ] page 2 timeout/rate limit/invalid 时保留 page 1 records，complete 与 affected windows 判定不靠“少于 page size”等猜测。
- [ ] request URI/header/log/error/transcript projection 的 secret 与 raw body 扫描通过。

### Verify

- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/infrastructure/test_usgs_observations.py backend/tests/application/evidence_tools/test_usgs_collection.py -q`
- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/infrastructure/test_provider_http.py backend/tests/observability -q`
- [ ] 可选 USGS contract smoke 先 preflight；无真实请求时记录 `not run`。

阶段出口：checked-in fixtures 下能从 USGS 官方契约稳定得到 actual water/discharge SourceRecords 和 completeness；尚未构造 fragments/bundle 或接入 Agent。

提交边界：USGS adapters、official contract fixtures、配置和 focused tests；不得夹带 NWS/EPA、tool registry、workflow 或 public route。

## Milestone 7：NWS weather 与 EPA facility adapters

### 工作

- [ ] 实现 NWS `/points/{lat},{lon}` discovery，只跟随返回的同源 `api.weather.gov` HTTPS forecast/grid/station links；User-Agent 使用配置的项目和联系信息。
- [ ] 历史观测从 discovered station observations 读取，forecast 优先 grid data，保留 issued_at、valid range、variable、unit、duration/statistic 和 provider quality。
- [ ] weather adapter 只返回 records/completeness，不声明对某水质参数的 relation；applicability 由 policy registry 决定。
- [ ] 实现 EPA ECHO CWA `get_facilities` query-only GET：固定 JSON、CWA/NPDES 所需公开字段、responseset/pagination 上限和安全 URI。
- [ ] facility adapter 逐 corridor center 使用 planner 的 provider radius，请求完成后按 facility/permit identity 去重，再以 full LineString + Agent exact radius 最终过滤。
- [ ] provenance 保存 Agent radius、provider radius、sample centers、request/page identity、dedup 和 final distance；Agent 不能提交中心点或 URL。
- [ ] facility 默认始终 `contextual_only`；缺坐标记录保留质量/审计事实但不能伪造距离或通过空间过滤。
- [ ] 两类 adapter 都遵守统一 timeout/retry/cancel/size/completeness/error 语义，无 fallback provider。

### Tests

- [ ] NWS point discovery、同源校验、observation、grid forecast、valid interval、单位、missing/control fields 和 malformed fixture。
- [ ] weather optional：prediction mode 本身不触发请求；未登记 relation 不提升 applicability。
- [ ] ECHO single/multi-center、pagination、重复 facility/permit、边界内外、缺坐标、radius round-up 和 provider failure fixture。
- [ ] long/curved geometry 的所有中心都被查询或在请求前因预算失败；绝不静默截断或 representative-point fallback。
- [ ] NWS/EPA URI、headers、logs、typed content 和 errors 不含 secret、任意 Agent URL 或 raw provider envelope。

### Verify

- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/infrastructure/test_nws_weather.py backend/tests/infrastructure/test_epa_echo_facilities.py -q`
- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/application/evidence_tools/test_weather_collection.py backend/tests/application/evidence_tools/test_facility_collection.py -q`
- [ ] 可选 NWS/EPA contract smoke 分开 preflight；0 请求或网络不满足记录 `not run`。

阶段出口：NWS/EPA fixture contract 可产生 typed records 与可信 completeness，空间和 relation 边界由 server 固定；尚未注册 Agent tools 或写 bundle。

提交边界：NWS/EPA adapters、fixtures 和 focused tests；不得夹带 executor、transcript/API、M6 prompt 或 frontend。

## Milestone 8：tool registry、executor 与 run-local single-flight

### 工作

- [ ] 实现八个固定 tool descriptors：collection 三项、M7/M8 producer slots、conversion、aggregation、inspection；descriptor 包含 typed request/result、policy class、scope requirements、limits 和 producer status。
- [ ] 按 session scope 生成 roster：stationless 不注册 local observation；stateless 不注册 transcript search；M7/M8 producer unavailable 时 production 不暴露不可执行工具，contract test 仍可验证 slot schema。
- [ ] 实现唯一 `EvidenceToolExecutor.execute(scope, request)`：authorize → budget → canonical key/single-flight → port/policy → writer → typed result。
- [ ] canonical request key 使用 run_id、agent_comid、tool、normalized options、sorted windows 和 manifest/policy digest；相同 run/scope/key 共享 in-flight operation。
- [ ] 每次调用仍生成自己的 call/result；success reuse 标记 `deduplicated=true` 并引用原 operation/result，不复制 source/fragment；failed/cancelled 不进入 success cache，不跨 run 复用。
- [ ] budget 分开计算 external collection 与 deterministic transforms；同义拆分仍归入 canonical accounting，deadline/cancel 在新请求、下一页、retry、transform、bundle 和 downstream handoff 前检查。
- [ ] 实现 conversion/aggregation tools，严格复用 Milestone 3 policy；Agent 只能选择 descriptor 暴露的方法/target unit。
- [ ] 实现 inspection：只读取已保存 records，按 `(observed_time, record_id)` 稳定分页，default 50/max 200，不访问 provider、不改变原记录、不标 RAG eligible。
- [ ] producer slot 返回结构化 `evidence_producer_unavailable` 或不注册；不得用 hidden fake 使 production 看似已接入 M7/M8。

### Tests

- [ ] roster 对 station-backed/stationless、stateful/stateless、producer ready/unavailable 和 allowed modes 的矩阵稳定。
- [ ] 同 run 并发 exact request 只有一次 provider operation；两个 call/result 都存在且 refs 正确；不同 scope/options/digest/run 不合并。
- [ ] success reuse 到 workflow quiesced，failed/cancelled 可重试且不缓存；cleanup 移除 live dedup state。
- [ ] collection/transform call budget、deadline、pages/records/bytes/preview/inspection limits 的边界与错误确定。
- [ ] cancel 胜出后不发新页/retry/transform、不写 final bundle、不向下游交付晚到响应。
- [ ] inspection 的 ownership、offset/limit、稳定排序、full-content read 和无 provider I/O 可证明。

### Verify

- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/application/evidence_tools/test_registry.py backend/tests/application/evidence_tools/test_executor.py -q`
- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/application/evidence_tools/test_dedup.py backend/tests/application/evidence_tools/test_inspection.py -q`
- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/architecture -q`

阶段出口：八工具通过同一 application interface 在 fake ports 下执行，scope/budget/dedup/cancel/producer 状态可验证；尚未完成完整 source-to-bundle 和 production composition。

提交边界：registry/executor/dedup/inspection、application tests 和必要 fakes；不得夹带 public HTTP、M6/M7 workflow 或 frontend。

## Milestone 9：source-to-bundle 构造链

### 工作

- [ ] 实现 collection service：adapter records → quality/unit/series/completeness → direct/context/collection-fact fragments，不保存第二份 provider envelope。
- [ ] 对 provider batch 生成唯一 CollectionManifest，保存 request range、series/pages/chunks、completed ranges、affected windows、counts 和 completion proof。
- [ ] 按最终 windows 分配 fragments 与 collection facts；技术性 incomplete 窗口 blocked/`incomplete_source`，独立 complete 窗口可继续，数据 coverage 另算。
- [ ] 建立每个 requested target 和 required discharge 的 `EvidenceCell`；只允许 direct/allowed-indirect 解锁，contextual/invalid 只能保留说明。
- [ ] 同一 series 内按 manifest policy 聚合；跨 series 保持独立供 Agent inspection/解释，不自动冲突、选优或合并。
- [ ] 保留 observed 资格闭包：只有一条 local direct record 加可逆展示 conversion；所有 aggregate/跨 series/upstream/weather/facility/history 标记 inferred candidate metadata。
- [ ] 实现 bundle builder/validator：reference-only、scope/closure/role 唯一、fragment/source/result refs 存在、每个 target 同窗 discharge cell 齐全。
- [ ] tool model preview 限于 32 records 或 16 KiB；完整 records 只保存一次并通过 inspection/full-content read 取得。
- [ ] bundle 写入前校验必需 refs 已持久化或同批原子提交；写入失败沿 M3 pending/retry，不把未确认事实宣称完成。

### Tests

- [ ] direct valid/suspicious/invalid、successful empty、unit unresolved、partial coverage 和 technical incomplete 的逐窗 cell 状态正确。
- [ ] target supported 但 discharge missing、discharge supported 但 target missing、部分窗口 supported 和全部 blocked 的 bundle 均确定。
- [ ] 同 parameter 不同 station/method/fraction/statistic/series 分开；去重只接受稳定 identity 并合并 provenance。
- [ ] aggregate/conversion closure、suspicious counts、applicability/relation、RAG candidate 和 observed/inferred eligibility 不丢失。
- [ ] reference cycle、missing ref、cross-scope ref、duplicate cell/ref、inline raw arrays 和未持久化 ref 被拒绝。
- [ ] 相同输入在并发/分页完成顺序变化下产生稳定 manifest、fragments、cells 和 bundle serialization。

### Verify

- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/application/evidence_tools/test_collection_service.py backend/tests/application/evidence_tools/test_bundle_builder.py -q`
- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/domain/test_evidence.py -q`
- [ ] 运行 deterministic fixture audit，重复执行比对 canonical bytes/digests。

阶段出口：fake/real adapter records 能形成 reference-complete、逐窗口、含 discharge 的唯一 bundle；尚未注册五类 transcript content 或接入生产 runtime。

提交边界：collection/bundle services、validators、fixtures 和 focused tests；不得夹带 API route、M6 result 或 DAG scheduler。

## Milestone 10：typed content、持久化、读取与两层 SSE

### 工作

- [ ] 注册 `evidence_tool_call v1`、`evidence_source_records v1`、`evidence_fragment v1`、`evidence_tool_result v1`、`evidence_bundle v1` 的 model/deserializer/reference extractor/public projection/redaction/visibility/size/corruption handler。
- [ ] 正常写入顺序为 call → zero/more source batches → fragments/facts → result → final bundle；失败/cancel 路径保存实际存在的 immutable facts，不补假成功。
- [ ] source records 完整正文只存一次；fragment/result/bundle 使用 item/evidence refs，禁止复制 raw arrays 或相同摘要成为第二事实源。
- [ ] 复用 M3 inline/reference/preview/full-content 和 pending/retry 语义；大内容公开 preview 有界，全文读取验证 run/session/item ownership。
- [ ] session SSE 继续只发送 `transcript_item`，使用原 session-local cursor/replay/stream_end；不增加 evidence SSE event 或第三条流。
- [ ] run SSE 只投影轻量 session/run/persistence 状态，不复制 tool、records、fragments 或 bundle 正文。
- [ ] 当前/历史 reader、strict `view=history`、补写、重启修复、取消可见性和 corruption issues 支持五类内容。
- [ ] public projection 保留安全 provider/source URI、时间、质量、coverage、单位、applicability、provenance/ref；删除 secret、raw body、内部 exception、任意 URL 和 test controls。

### Tests

- [ ] 五类 content 的 schema round-trip、unknown schema、corrupt body、reference extraction、inline/reference 和 history reload 完整。
- [ ] writer partial failure、pending/retry exhaustion/manual retry、同批 refs、PostgreSQL reload 和 startup repair 不产生悬空 bundle。
- [ ] session SSE 首次补齐、重连 cursor、已结束 stream_end、cancel late output 和大正文 reference 行为沿用 M3。
- [ ] source batch/result/bundle 不重复正文；公开 preview/全文/API/SSE/日志全出口脱敏扫描通过。
- [ ] production registry/OpenAPI/schema fixtures 原子同步，不保留旧 evidence field union。

### Verify

- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/application/test_transcript_registry.py backend/tests/application/test_run_writer.py backend/tests/api backend/tests/persistence -q`（PostgreSQL 无安全配置时单独记 `not run`）
- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/smoke/test_run_sse_smoke.py backend/tests/architecture -q`
- [ ] 导出实际 OpenAPI 和 production registry，核对五类 content、旧字段清除和无新 SSE event。

阶段出口：五类 M5 原事实能通过当前 M3 storage/read/SSE 路径完整交付且引用闭合；尚未接入 station-backed/stationless acquisition 主路径。

提交边界：typed registry、writer/reader/projection/SSE/persistence integration 和 tests；不得新增 production tool route、frontend 或最终推理。

## Milestone 11：station-backed / stationless acquisition contract

### 工作

- [ ] 实现 session-scoped acquisition service，要求调用方提供已存在且准备执行的真实 waterbody session；M5 不因 topology 节点存在而预建 session。
- [ ] station-backed 在首次水体模型调用前确定性执行 `collect_water_observations`，查询当前水体全部适用目标通道并始终包含 local discharge direct channel。
- [ ] station-backed 成功查询后若所有适用窗口均无 usable target/discharge direct evidence，写完真实 calls/results/facts/bundle 并返回 `stop_before_model`；不调用模型、不造 fake message/confidence/value。
- [ ] station-backed 只要任一 target 或 discharge cell 有 usable direct evidence即可继续 optional acquisition；其余缺口保持 unsupported/blocked，不要求整分支全有或全无。
- [ ] provider unavailable/invalid/incomplete/cancelled 不走自然全空 completed 路径；返回技术结果并保留已确认事实。
- [ ] stationless roster 不含 local observation tool，也不发送注定 empty 的 USGS 请求；初始输入只消费 M7 direct-upstream slot 并允许 bounded optional tools。
- [ ] `get_upstream_results` 未接 M7 时明确 unavailable/not registered；正常上游全 indeterminate 只传递 result/bundle/缺口 refs，不构造 `same_parameter_upstream` allowed evidence。
- [ ] 每个 target/window 同时要求同窗 discharge direct/allowed cell；本地缺失时可消费 upstream 或已登记 discharge estimate relation，仍缺失时保持 indeterminate input。
- [ ] weather 始终 optional；只有 Agent 选择的已登记 relation 需要时才调用，前置证据不完整则 relation 不成立。
- [ ] 输出给 M6 的 contract 只包含 EvidenceBundleRef、cells、read ports、tool roster/executor 和 stop/block facts，不输出最终 InferenceResult。

### Tests

- [ ] station-backed full usable、完整查询但 empty/仅有 row-level quality-invalid records、部分 evidence、provider protocol/transport technical failure、cancel 五条路径。
- [ ] 全空停止路径有真实 session/call/result/facts/bundle，downstream fake model call count 为 0；session 最终 completed/indeterminate 由 M6 接力，不由 M5造结果。
- [ ] stationless 不调用 local observation；direct upstream usable、upstream indeterminate、upstream unavailable、optional weather/facility 的组合稳定。
- [ ] 每个窗口 target + discharge 的 supported/unsupported/blocked 组合和 `stop_before_model` 判断确定；未知 discharge 从不变 0。
- [ ] M4 availability 只影响 plan，不成为 actual record/fragment；下游不能直接查询上游 station。
- [ ] weather 未调用不算技术失败；选用天气 relation 后缺天气/流量前置时不能通过。

### Verify

- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/application/evidence_tools/test_acquisition_workflow.py -q`
- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/smoke/test_m5_evidence_workflow_smoke.py -q`
- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/application/test_query_topology_workflow.py backend/tests/smoke/test_m4_workflow_smoke.py -q`

阶段出口：station-backed/stationless 在 fake runtime 中产生正确 M6 输入和停止/继续事实，且未实现 M6 最终结果、M7 DAG 或水体模型 loop。

提交边界：acquisition service、M6 handoff DTO、fake integration/smoke tests；不得实现 final inference、DAG scheduling、RAG producer 或 frontend。

## Milestone 12：production composition 与独立 test app

### 工作

- [ ] 在 production `m5` composition 一次装配 settings、registry/policy、ports/adapters、executor、dedup、writer/reader 和 acquisition service；缺必需组件启动失败。
- [ ] 保持 production FastAPI/OpenAPI 的既有 run/session routes，不 import/register test router，不新增 `/tools`、`/evidence/collect` 或等价入口。
- [ ] 创建独立 `create_m5_test_app()`/test composition，显式挂载 `POST /__test__/m5/evidence-tools:execute` 和 test transcript read；production module import graph不可达。
- [ ] test request 只接受 checked-in `fixture_scope_id`、tool name、window/parameter IDs 和有界 options；不接受 provider/base URL/secret/任意 COMID/station/geometry 或绕过 writer/scope 的开关。
- [ ] fixture scope 创建合法合成 run/session/context/graph 并复用 production executor、registry、policy、dedup、writer、deserializer 和 error mapping；fake 只替换外部 ports。
- [ ] test app 不通过环境变量在 production 开启，不进入发布入口/依赖，不由 frontend 调用。
- [ ] 增加 opt-in provider smoke scripts：分 provider preflight、实际 request/page/record count、安全摘要和 `not run` 原因；脚本不默认执行、不打印 secret。

### Tests

- [ ] production OpenAPI 精确不含 test/tool invocation path，production app import test modules 的 architecture guard 失败。
- [ ] test app happy/empty/unavailable/invalid/cancel/dedup/inspection 可调用并能从真实 transcript path 读回 refs。
- [ ] test request 对未知 fixture/tool、越权 window/parameter、任意 extra URL/secret/control 字段 strict 拒绝。
- [ ] application direct test 与 test HTTP app 对同 fixture 产生同一 contract semantics，不维护简化第二实现。
- [ ] production startup 配置缺失/非法与 test fake composition 的边界明确；frontend tree 无 test route 消费。

### Verify

- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/api/test_m5_test_app.py backend/tests/infrastructure/test_m5_runtime.py -q`
- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/architecture -q`
- [ ] 导出 production OpenAPI 并断言不含 `/__test__`、`/tools`、`/evidence/collect`。

阶段出口：production runtime 可供后续 waterbody Agent application 调用且无公开 invocation surface；独立 test app 能自动化验证同一真实执行链。

提交边界：production/test composition、test-only router、smoke scripts、OpenAPI/architecture tests；不得修改 frontend 或接入 M6/M7/M8 production producer。

## Milestone 13：集成验收、契约审计与 M6–M8 接力

### 工作

- [ ] 建立固定 fake providers 的完整 M5 workflow smoke：station-backed usable、全空停止、部分 evidence、provider failure、stationless upstream/optional、逐窗 discharge 缺失。
- [ ] 建立完整 provider fixture suite 和离线 contract audit：USGS next-chain/Samples CSV、NWS discovery/grid、EPA corridor、geometry、policy、typed content/reference closure。
- [ ] 完整回归 M1–M4：创建幂等、四表、writer/reader、history、两层 SSE、取消、补写、重启修复、parser/context/topology/availability 和 OpenAPI。
- [ ] 在专用 PostgreSQL `wateragents_test` 执行 persistence、补写、history reload 和启动修复；仅用进程级 `WATERAGENTS_TEST_DB_NAME`，不接触业务库。
- [ ] 分 provider 执行可选真实 contract smoke；预检网络/凭据并记录实际请求数、endpoint contract、pages/records 和安全结果，未执行保持 `not run`。
- [ ] 审计 `GLOBAL-C01`–`GLOBAL-C11`、`M5-C01`–`M5-C20`、`M5-A01`–`M5-A08`，逐项填写实现位置、测试、实际状态和剩余风险。
- [ ] 搜索并清除旧 evidence fields、重复 policy/allowlist、orphan adapters/methods、M5 feature flags、production test route import 和无 caller 代码；不清理无关历史代码。
- [ ] 同步 M5 设计/实施/Policy/API/TODO、架构骨架、总 Milestones、延后契约和前端接入边界；只勾选有实际证据的任务。
- [ ] 写明 M6 接力：消费 bundle/cells/read ports/executor，执行 sufficiency/final result validator；system prompt 负责跨 series 差异的检查、引用、解释或保持不确定。
- [ ] 写明 M7 接力：实现 direct-upstream producer、依赖收敛和 session 调度；M5 不把 slot/fake 当 production producer。
- [ ] 写明 M8 接力：实现 transcript index/search，且在 candidate 外继续检查 run/session completed、persistence、memory mode 和 safe projection；历史不能解锁当前 gate。

### Tests

- [ ] fake end-to-end 覆盖所有主路径、每类 technical failure、cancel race、single-flight、budget、persistence failure 和 deterministic serialization。
- [ ] 154 项 manifest/policy、geometry/corridor、八工具 roster、五类 typed content、逐窗 target/discharge cell 和 RAG candidate 均有可执行 gate。
- [ ] secrets、raw provider envelope、URL query、Authorization、内部异常、无界 records 和 test controls 的 logs/API/SSE/history 全出口扫描通过。
- [ ] 当前唯一 V1 producer/consumer/fixture/OpenAPI 同步，无 V2/read union；production OpenAPI 无 test/tool route。
- [ ] contract ID 集合与覆盖矩阵双向一致；Markdown links、JSON examples、code fences、trailing whitespace 和 `git diff --check` 通过。

### Verify

- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests -q`
- [ ] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/persistence -q`（仅专用 `wateragents_test`；未配置则 `not run`）
- [ ] `backend/.venv/Scripts/python.exe -B -m compileall -q backend/app backend/scripts`
- [ ] `backend/.venv/Scripts/python.exe -B backend/scripts/m5_evidence_workflow_smoke.py`
- [ ] `backend/.venv/Scripts/python.exe -B backend/scripts/run_sse_smoke.py`
- [ ] `backend/.venv/Scripts/python.exe -B backend/scripts/run_history_smoke.py`（满足专用 PostgreSQL 安全门禁时）
- [ ] 运行 manifest、provider fixture、typed registry、OpenAPI、docs/link/JSON 离线 audits。
- [ ] `git diff --check`

阶段出口：M5 内部切片的代码、测试、协议和接力证据完整；没有把未运行的 provider/PostgreSQL/三平台项写成通过，也没有实现 M6–M9 或开始正式前端联调。

提交边界：只包含集成修复、审计、smoke、文档同步和完成记录；发现实质功能缺口时必须回到对应 Milestone 修复并重跑其 focused gate，不能把大功能补丁藏在最终审计提交。

## 契约覆盖矩阵（实施时填写）

| 契约 | 状态 | 实现位置 | 重点测试 | 证据与剩余风险 |
|---|---|---|---|---|
| M5-C01 / M5-C02 / M5-A01 | pending | — | typed models、reference closure、corruption | — |
| M5-C03 / M5-C04 / M5-C15 | pending | — | execution/collection、quality/coverage、partial failure | — |
| M5-C05 / M5-C06 / M5-A05 | pending | — | scope/time、geometry/corridor、facility filtering | — |
| M5-C07 / M5-C17 / M5-A06 | pending | — | roster/executor、single-flight、budget/cancel | — |
| M5-C08 / M5-C09 / M5-C10 / M5-A03 | pending | — | station-backed/stationless、discharge、optional weather | — |
| M5-C11 / M5-C12 / M5-C13 / M5-A02 | pending | — | manifest、unit/aggregation、series、applicability | — |
| M5-C14 | pending | — | provider fixtures、single-source、official contract | — |
| M5-C16 / M5-A04 | pending | — | five typed contents、writer/reader/SSE/OpenAPI | — |
| M5-C18 | pending | — | production/test import and route isolation | — |
| M5-C19 / M5-C20 / M5-A07 | pending | — | RAG candidate、M7/M8 producer unavailable | — |
| M5-A08 / GLOBAL-C01–C11 | pending | — | M1–M4 regression、architecture、current V1、platform | — |

状态只允许：`pending`、`partial`、`本机已验证`、`外部已验证`。不能只因代码或 fixture 存在填写已验证；本机、PostgreSQL、外部 provider 和三平台 CI 证据分开记录。

## 实际完成记录格式

每个 Milestone 完成时，在该节“提交边界”后追加记录；不要新建独立 completion Markdown：

```text
#### Milestone N 完成记录（YYYY-MM-DD）

- 实现：关键 production 契约、改动路径与明确未实现边界。
- 验证：实际命令、环境和 pass/fail/skip/not run 结果；失败后的修复与重跑结果。
- 风险：外部未运行项、partial 项和后续接力限制。
- 提交：commit SHA；如未提交则写未提交，不能填计划 SHA。
```

不得把计划命令写成已执行结果，不得复制 M4 或其他阶段的 pass count，不得把 `skipped`、0 requests、HTTP 200、进程启动或 exit code 0 单独当作业务通过证据。

## M6 / M7 / M8 接力出口

M5 完成后只交付以下稳定能力：

- M6：`EvidenceBundleRef`、逐 ParameterKey × window 的 target/discharge cells、fragment/source read ports、八工具 registry/executor、stop/block facts；M6 自己执行 sufficiency、prompt、模型调用和最终 result validation。
- M7：稳定的 `UpstreamResultPort` request/result 槽位和 producer-unavailable 语义；M7 自己实现 DAG dependency convergence、真实 waterbody session 调度和 direct-upstream producer。
- M8：固定 `TranscriptEvidencePort` 槽位和 fragment candidate policy；M8 自己实现 index/search、生命周期/持久化/memory-mode gate 和安全 projection。
- M9：现有 run/session/transcript/API/SSE 中的安全 M5 projections；M9 frontend 不调用 test route，也不提交 provider、station、radius、geometry、URL 或 tool policy。

M5 不因上述阶段尚未实现而增加 fake production producer、临时 unsupported product response、feature flag、V2 或兼容层。后续阶段应直接扩展同一最终系统和当前唯一 V1。
