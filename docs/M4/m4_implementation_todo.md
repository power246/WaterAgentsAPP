# M4 Query Understanding + Hydrology Topology Implementation TODO

更新日期：2026-09-27

> 当前状态：Milestone 0–10 已完成本机实现与集成审计；专用 PostgreSQL、需凭据 provider/parser contract smoke 和三平台 CI 因当前环境缺失保持 `not run`/`partial`，没有冒充通过。M4 是 M1–M9 同一项目进程中的内部实施切片，不形成可单独使用、正式联调或需要兼容的“M4 版本”；M9 完成后才开始正式前后端联调和第一版整体验收。

本清单执行以下已确认文档：

- [M4 设计决策 D01–D27](m4_query_understanding_topology_design.md)
- [M4 实施设计](m4_query_understanding_topology_implementation_design.md)
- [M4 API / 前端接入](m4_query_understanding_topology_api.md)
- [M4 参数能力目录](m4_parameter_capability_catalog.md)
- [跨 Milestone M4 契约](../deferred_milestone_contracts.md#6-m4query-understanding--hydrology-topology)
- [M3 后续接力](../M3/m3_transcript_history_implementation_design.md#12-完成标准与接力)

若本清单与上述设计发生冲突，coding agent 不得自行挑选较容易的一项实现；应先在同一变更中消除文档冲突。字段细节、内部函数拆分和测试文件组织可由实现者决定，但不能改变已确认的输入优先级、错误语义、范围、身份、持久化、SSE、模型调用或 evidence 边界。

## 阶段与设计导航

| 实施阶段 | 主要设计依据 | 阶段产物 |
|---|---|---|
| [Milestone 0：基线、边界与接力盘点](#milestone-0基线边界与接力盘点) | [实施设计 §1–3](m4_query_understanding_topology_implementation_design.md#1-核心结论与范围)；[GLOBAL-C01/C02](../deferred_milestone_contracts.md#2-跨-milestone-硬约束) | 可复现基线、真实扩展接缝、无归档依赖证明 |
| [Milestone 1：最终请求、唯一配置与模型目录](#milestone-1最终请求唯一配置与模型目录) | [实施设计 §4](m4_query_understanding_topology_implementation_design.md#4-最终请求与不可变配置)；[API §2–3](m4_query_understanding_topology_api.md#2-创建分析) | 最终 create schema、配置快照、模型选择门禁 |
| [Milestone 2：154 项能力注册表与参数解析](#milestone-2154-项能力注册表与参数解析) | [实施设计 §7.4/§8](m4_query_understanding_topology_implementation_design.md#74-参数解析)；[参数目录](m4_parameter_capability_catalog.md) | 单一 manifest、capability registry、确定性 parameter resolver |
| [Milestone 3：AnalysisContext 与日期/位置 resolver](#milestone-3analysiscontext-与日期位置-resolver) | [实施设计 §6–7](m4_query_understanding_topology_implementation_design.md#6-analysiscontext) | 纯确定性的 target/date/mode/resolution/context 管道 |
| [Milestone 4：Query Parser session 与模型调用边界](#milestone-4query-parser-session-与模型调用边界) | [实施设计 §5](m4_query_understanding_topology_implementation_design.md#5-query-parser-输入与输出)；[API §4.3–4.5](m4_query_understanding_topology_api.md#43-query_parser_input-v1) | 无工具 parser、一次修复、真实 session 过程 |
| [Milestone 5：Geocoding、NLDI 与 USGS metadata adapters](#milestone-5geocodingnldi-与-usgs-metadata-adapters) | [实施设计 §13](m4_query_understanding_topology_implementation_design.md#13-ports-与外部失败)；[设计 D23–D24](m4_query_understanding_topology_design.md#d23conus-校验使用行政区事实与-comid-覆盖双门禁) | 新系统独立 adapters、类型化失败、contract fixtures |
| [Milestone 6：Station availability qualification](#milestone-6station-availability-qualification) | [实施设计 §9](m4_query_understanding_topology_implementation_design.md#9-availability-qualification) | 参数 × segment 覆盖单元、历史/预测候选资格 |
| [Milestone 7：HydrologyGraph 与稳定有界 BFS](#milestone-7hydrologygraph-与稳定有界-bfs) | [实施设计 §10](m4_query_understanding_topology_implementation_design.md#10-hydrologygraph) | 可验证图模型、每支流最近充分搜索、稳定序列化 |
| [Milestone 8：M3 workflow、typed content 与持久化接入](#milestone-8m3-workflowtyped-content-与持久化接入) | [实施设计 §2/§11](m4_query_understanding_topology_implementation_design.md#2-执行流程与状态边界) | 完整 M4 workflow、唯一 context/graph 原记录、取消收敛 |
| [Milestone 9：HTTP、history、两层 SSE 与公开错误](#milestone-9httphistory两层-sse-与公开错误) | [实施设计 §12/§14](m4_query_understanding_topology_implementation_design.md#12-两层-sse)；[API §5–9](m4_query_understanding_topology_api.md#5-history-与-detail-投影) | 最终 OpenAPI、读模型、SSE reference、错误矩阵 |
| [Milestone 10：集成验收、契约审计与 M5 接力](#milestone-10集成验收契约审计与-m5-接力) | [实施设计 §16–17](m4_query_understanding_topology_implementation_design.md#16-测试与验收设计) | fake 端到端、受控真实 smoke、契约矩阵、接力记录 |

## Coding agent 执行原则

- [ ] 只以 `backend/`、`docs/M1`–`docs/M4` 和项目级当前契约为实现输入。`backend_bk/`、`frontend_bk/` 与旧契约是完全独立、最终丢弃的归档；不得读取、导入、复制、适配、枚举、运行或测试其中的代码、服务、接口、模型、依赖、配置、缓存和行为。
- [ ] 新前端不在 M4 实现，也不以 `frontend_bk/` 为模板。M4 只交付最终新后端协议与 M9 接入依据；M9 再依据最终产品设计、运行时 OpenAPI 和 SSE 契约独立实现前端。
- [ ] 开始每个 Milestone 前检查 `git status`、当前分支和基线 SHA，保留用户已有改动；不得清理、覆盖或顺手重构不属于该阶段的文件。
- [ ] 一个 Milestone 只完成表中一个可观察目标。先写/更新失败测试，再做最小实现，再跑 focused tests；阶段出口未满足时不进入下一阶段，也不勾选任务。
- [ ] 每个 Milestone 原则上形成一个可审查提交；提交前核对 `git diff --check` 与 `git show --stat --oneline HEAD`。未经用户要求不 push、不创建 PR、不改写历史。
- [ ] 直接更新当前唯一 request、configuration、DTO、lifecycle、snapshot、fixture 和调用方；不得增加 M4 schema/version、旧新 union、兼容路由、临时 feature flag、双写或“当前 M4 尚未启用”的产品分支。
- [ ] 复用 M3 的 run/session runtime、四表、writer/reader、typed-content registry、两层 SSE、取消、补写、启动修复和 live cleanup；不得建立第二套记录、事件、重试、历史或 session 系统。
- [ ] domain/application/ports/infrastructure/API 的依赖方向继续由 architecture tests 强制。Ports 只保留 stdlib/domain 类型的 Protocol 和错误；application 不导入 infrastructure/FastAPI；API 不访问 coordinator 私有状态。
- [ ] 外部成功空结果与 timeout/rate-limit/auth/invalid response 必须不同。只对幂等 metadata GET 做已设计的有界重试；不得把技术故障变成“无数据”后继续。
- [ ] 所有时间来自 `ClockPort`；测试使用 fake clock、gate 和确定性 scheduler，不用真实 sleep 等待 45/120 秒预算。所有输出排序必须独立于并发完成顺序。
- [ ] parser 及水体模型只接受后端模型目录中的显式 provider/model，不从模型名称猜 provider、不自动 failover。前端不提交 API key；secret 只来自后端配置且不得进入 transcript、SSE、错误或普通日志。
- [ ] 参数由 LLM 自主推测，few-shot 只教 schema/行为；不得退化成意图模板分支表。最终代码只能由确定性 capability registry 选择；相似度分数不能直接授予执行权。
- [ ] 单次最多 6 个用户目标；第 7 个整体失败，不截断。`usgs:00060` discharge 必带且不占上限，若本身是目标则不重复。
- [ ] M4 availability 只是候选资格，不是实际 evidence。不得在 M4 调用水体模型、生成推理结果、创建 waterbody session，或把 metadata 宣称为观测。
- [ ] 真实 LLM/provider smoke 不是单元测试前置条件。先用 fakes 完成确定性覆盖；需要凭据、可能计费或依赖公网的 smoke 先做配置预检，未配置/零请求记录为 `not run`，不能写成通过或零成本结论。
- [ ] 每阶段追加实际完成记录：改动路径、命令、环境、结果、失败后修复、剩余风险和 commit SHA。历史结果不覆盖新结果；没有实际证据就保持未勾选或 `partial`。

## M4 完成标准

- [x] 最终 create request 只包含 query、可选 target、可选 date range、resolution preference、两个模型选择和 memory mode；strict extra/nullable/成对字段符合 API 文档。
- [x] 同一 `Idempotency-Key` + 原始 body 重试返回原回执，不因日期跨日或模型目录变化重新解释；新 key 才读取同一次 clock/catalog snapshot。
- [x] 前端 target/date 高于 query，缺失才 fallback；实质冲突、缺失、歧义、CONUS/COMID 不支持或日期越界均在 topology 前停止。
- [x] parser 是无 COMID 的真实 session，正常一次模型调用；仅 schema invalid 修复一次，第二次失败终止，语义错误不消耗 repair 调用。
- [x] parser 的实际工具列表为空；prompt、输出、模型身份和调用次数按 M3 规则保存且全出口脱敏。
- [x] capability manifest 精确包含目录中的 154 个唯一 USGS key；代码、单位、aliases、provider route 和 evidence relation 可校验，discharge 是唯一 required support。
- [x] `AnalysisContext` 对 target、当地日期、UTC `[start,end)`、segments、mode、resolution、最多 6 个目标、supporting discharge、双模型和 memory mode 执行完整 invariant。
- [x] CONUS 48 州与 DC、AK/HI/领地/外国、point 无 COMID、COMID/point mismatch 各有独立结果；不得把 region bias 或附近吸附当地域/河段证明。
- [x] WDFN v1 time-series metadata 与 Samples summary 能分别支持连续/每日和离散样品的 period-of-record qualification；旧 WaterServices 不进入 production path。
- [x] availability 按 ParameterKey × time segment 判断，历史 envelope 和预测前 48 小时基线规则明确；empty/unavailable/invalid 不混淆。
- [x] BFS 按每条支流最近充分规则运行，稳定处理汇流、重复和环；100 km/12 hop/128 unique nodes/8 concurrency 分别受控，技术失败不产生 partial success。
- [x] `HydrologyGraph` 保存 target、完整 explored scope、inlet、active path union、excluded facts、branch outcomes、coverage 和稳定 layers；topology 阶段不创建 waterbody session。
- [x] `query_understood` 是 parser session 原 item，`topology_ready` 是 run 原 item；history/detail/SSE 只投影或引用，不复制第二份 context/graph。
- [x] run/session 取消、provider timeout、schema repair、topology 失败、writer 补写、重启修复和 live cleanup 保持 M3 invariant；context 成功后 topology 失败时 context 仍可读。
- [x] 最终 OpenAPI、文档 JSON、production registry、SSE encoder 和可执行 fixture 同源；没有旧 M3 request 兼容输入、第三条 SSE 或 M4 专用路由。
- [x] fake adapters 的完整 M4 workflow、完整 backend regression、适用本机 SSE/contract audit 已有实际证据；PostgreSQL/health/history 与需凭据外部项明确记录为 `not run`。

## Milestone 0：基线、边界与接力盘点

### 工作

- [x] 记录当前 branch、HEAD、`origin/main`、merge-base、工作区已有改动和 Python/依赖环境；不移动或覆盖现有 M4 文档改动。
- [x] 只检查新后端真实入口：`analysis_runs` coordinator/creation/runtime/workflow、transcript registry/reader/streaming、create/history/SSE route、settings、composition root、PostgreSQL store 和现有 fakes。
- [x] 形成 M4 改动影响表：请求/配置、domain、application、ports、adapters、registry、reader、API、OpenAPI、smoke、docs；每个事实只指定一个 runtime source of truth。
- [x] 确认 M3 当前完整测试、真实 PostgreSQL 测试和 smoke 的可运行入口；环境缺失时记录 `not run`，不修改测试绕过基础设施。
- [x] 增加或确认 architecture guard：production `backend/` 和测试不得 import、打开、枚举或依赖归档目录；CI/test collection 不把归档文件纳入路径。
- [x] 明确 M4 不新增数据库业务表；如当前唯一配置/正文模型需要迁移，只修改现有 schema/fixture，不建立阶段兼容层。

### Tests

- [x] 当前 `backend/tests` 完整基线，无未解释失败。
- [x] architecture tests 锁定 layer dependency、port purity、单一 coordinator runtime ownership 和归档隔离。
- [x] 当前 health/run SSE/history smoke 的适用基线被记录；真实 PostgreSQL 使用专用测试库安全门禁。

### Verify

- [x] `python -m pytest backend/tests -q`（非 PostgreSQL `858 passed`；真实 PostgreSQL endpoint 超时，按环境门禁记为 `not run`）
- [x] `python -m pytest backend/tests/architecture -q`
- [x] `python -m compileall -q backend/app backend/scripts`
- [x] `git diff --check`

阶段出口：当前新后端接缝和基线证据可复现，归档系统完全不在实现输入中；本阶段不新增 M4 业务行为。

提交边界：只允许 architecture guard、必要的基线测试修正和本清单完成记录；不提前修改 create schema 或 workflow。

#### Milestone 0 完成记录（2026-09-25）

- 实现：固定 `origin/main` 基线与 M3 扩展接缝，新增归档隔离 architecture guard；未引入 M4 业务行为或数据库表。
- 验证：非 PostgreSQL 回归 `858 passed`、architecture `75 passed`、socket smoke `20 passed`、compile/diff 通过；真实 PostgreSQL 与正式脚本因 endpoint 超时记为 `not run`。
- 提交：`4a9e7cf`；原始文档工作区改动保持独立。

## Milestone 1：最终请求、唯一配置与模型目录

### 工作

- [x] 在 API 层实现最终 strict create body：必填 query/parser_model/waterbody_model/memory_mode，可选 target/date_range/time_resolution；target 至少含 COMID 或 coordinates，日期必须成对，显式 null 和未知字段按文档拒绝。
- [x] 将 `AnalysisConfigurationSnapshot` 直接收敛为唯一模型：timezone、resolution preference/source、parser model、waterbody model、memory mode；删除被替换的阶段字段与测试预期，不增加 configuration version/union。
- [x] 建立后端单一 `ModelCatalog` runtime source，按 `query_parser`/`waterbody` role 校验 provider/model；请求和历史只保存安全 identity，不保存 endpoint credential。
- [x] 保持创建幂等顺序：先查 key + raw body；已有相同 body 直接返回原回执，已有不同 body 冲突；只有全新 key 才用同一次 clock/catalog snapshot 做动态日期与模型校验。
- [x] structured date 的形状、顺序和 ±7 日动态边界可在 create 前返回 422；query-derived date 留给 parser 后 resolver，不在创建前猜测。
- [x] 直接扩展当前 `analysis_request v1`、`lifecycle_event v2`、run configuration 和相关 fixture/reader；同步所有调用方，不新增旧请求兼容入口。
- [x] 在 composition root 一次解析 M4 hard limits 和 timeout；非法、未知或越界设置启动失败，前端不能覆盖 topology limits。

### Tests

- [x] strict body、extra forbid、null、target 组合、日期配对/顺序、enum 和模型 role 的 API/schema 测试。
- [x] 幂等重试跨 fake-clock 日期变化和 catalog 变化仍返回原回执；新 key 使用新 snapshot；同 key 不同 bytes 冲突。
- [x] parser/waterbody 可选择不同模型，unsupported role/组合拒绝；请求、OpenAPI、日志、正文均不出现 secret 字段。
- [x] 默认/auto/显式 resolution preference 与 source 固化正确，尚未在 configuration 伪造实际 resolution。
- [x] M3 创建事务、未知提交核实、并发接管和原回执 regression 不回退。

### Verify

- [x] `python -m pytest backend/tests/api backend/tests/application/test_run_creation.py backend/tests/persistence -q`（适用集合通过；真实 PostgreSQL 因无测试凭据记为 `not run`）
- [x] `python -m pytest backend/tests/architecture -q`
- [x] 导出实际 OpenAPI，核对 create body 与 API 文档字段/required/nullable/enum。

阶段出口：所有新 run 使用最终请求和唯一配置，模型身份已固化；workflow 尚不调用真实 parser 或构建 topology。

提交边界：request/config/catalog/creation 的完整纵切及其测试；不得夹带 parameter resolver、NLDI 或 BFS。

#### Milestone 1 完成记录（2026-09-25）

- 实现：完成最终 strict create request、唯一 configuration、模型目录和创建幂等顺序；未加入 parameter resolver、NLDI 或 BFS。
- 验证：focused `142 passed`、组合回归 `321 passed`、完整非 PostgreSQL 回归 `891 passed`，architecture/smoke/compile/diff 通过；真实 PostgreSQL 因无测试凭据记为 `not run`。
- 提交：`9b1f60c`。

## Milestone 2：154 项能力注册表与参数解析

### 工作

- [x] 新增唯一 checked-in capability manifest，精确覆盖 [参数目录](m4_parameter_capability_catalog.md) 的 154 个 `usgs:<code>`；runtime、resolver、availability 和后续 M5 共用，不在 prompt/API/adapter 复制 allowlist。
- [x] 每项登记 canonical name/unit、官方 group/medium/fraction 语义、中文与英文 aliases、target/support role、TS/Samples provider route、允许的 evidence-rule IDs 和测试 fixture identity。
- [x] 实现不可变 `ParameterCapabilityRegistry`，启动时一次验证 key/alias/role/provider/unit/evidence 引用、目录集合与唯一 discharge；错误配置启动失败。
- [x] 实现 `ParameterCatalogPort` 作为完整官方目录候选发现边界；候选分数只用于诊断，不直接决定可执行 key。
- [x] 实现确定性 parameter resolver：规范名称/显式 alias 优先；唯一 capability 才成功；多义、目录有但 manifest 无、无参数分别返回稳定业务结果。
- [x] 保留 LLM 自主推测的 `ParameterConcept` 与 rationale；resolver 不把未支持概念换成较弱近义参数，也不删除失败项继续。
- [x] 去重后最多 6 个用户目标；超过整体失败。自动加入 `usgs:00060`，目标已含 discharge 时不在 supporting list 重复。
- [x] 明确 M1–M9 集成边界：M4 交付完整 manifest、解析和 availability route；M5 接入真实 observation/evidence。不得把 M4 内部切片宣称为已完成最终 154 项 evidence 能力，也不得因 M5 尚未实施缩减 manifest。

### Tests

- [x] 154 个 key 唯一且与文档双向一致；官方 group 数量为 `28+10+25+6+7+22+12+9+18+10+5+2`。
- [x] 每项 unit/provider/aliases/evidence refs 完整；discharge 唯一 required support；未知 provider/rule、重复 alias 的歧义策略可验证。
- [x] 明确参数、中文/英文 alias、大小写/空白、filtered/unfiltered、field/lab、FNU/NTU 和多种微生物方法不被错误合并。
- [x] few-shot 未覆盖概念仍可被 parser candidate 表达；resolver 的 unique/ambiguous/unsupported/required/too_many 分支完整。
- [x] 1、6 个目标成功，7 个目标整体失败；discharge 自动加入、不占上限、不重复。
- [x] manifest/file loading 与 package installation、不同 cwd、Windows 路径无关。

### Verify

- [x] `python -m pytest backend/tests/domain backend/tests/application -q`（参数 capability/resolver focused 集合）
- [x] `python -m pytest backend/tests/architecture -q`
- [x] 运行离线 manifest audit，输出 154/154、唯一 key、分组计数和 required support 数量。

阶段出口：参数候选可以确定性映射为有限能力和 supporting discharge；尚未创建 parser session 或查询远端数据。

提交边界：manifest、registry、parameter catalog port/resolver 和测试；不得提前接入 workflow、HTTP adapter 或 graph。

#### Milestone 2 完成记录（2026-09-25）

- 实现：完成 154 项唯一 capability manifest、不可变 registry、catalog port 与确定性 parameter resolver；未接入 workflow、HTTP adapter 或 graph。
- 验证：domain/application `522 passed`、architecture `76 passed`、完整非 PostgreSQL 回归 `927 passed`；manifest 审计为 `154/154` 且 required support 为 1，wheel 异地安装加载通过。
- 提交：`c63cb35`。

## Milestone 3：AnalysisContext 与日期/位置 resolver

### 工作

- [x] 实现 `ParsedIntentV1`、`ResolvedTarget`、`ResolvedParameterIntent`、`SupportingParameterIntent`、`LocalDateRange` 和唯一 `AnalysisContext` typed models；禁止跨层 raw dict 主契约。
- [x] 实现输入优先级：前端 target/date 有值时优先，缺失才采用 parser candidate；比较解析后的业务身份，同义地点/日期不算冲突，实质冲突失败。
- [x] 扩展 geocoding/comid ports 的新契约：文字正向、坐标反向行政区、COMID segment/代表点和 position point-in-polygon；`None` 只表示成功无匹配。
- [x] 实现 CONUS 48 州 + DC allowlist 与行政区/COMID 双门禁；AK、HI、领地、外国、无 COMID 和 target mismatch 分开。
- [x] 用单次 `ClockPort.now()` + 固化 timezone 计算 today，验证闭区间 `[today-7,today+7]`；date-only inclusive end 转 UTC `[start,end)`，覆盖 DST。
- [x] 确定性推导 gap_filling/prediction/mixed，在当地今天 00:00 切段；执行跨午夜不重算。
- [x] 实现 auto 规则：1–7 日 6h、8–15 日 12h；24h 只显式选择，API 省略为 12h/default；复用 M1 `build_time_windows()`，最多 60 窗口。
- [x] 组合 Milestone 2 参数结果、双模型选择和 memory mode，执行 AnalysisContext 全部 invariant；失败不填默认地点、日期、参数、mode 或 COMID。

### Tests

- [x] frontend-only、query-only、相同值、实质冲突和半缺失输入矩阵。
- [x] 坐标、COMID、二者一致/不一致、文字唯一/多候选；CONUS/DC/AK/HI/territory/foreign/no-COMID。
- [x] fake clock 下 ±7 日闭边界、跨年/月、start>end、inclusive end、spring-forward/fall-back 和跨午夜不漂移。
- [x] gap/prediction/mixed 分段与 UTC 边界；auto/default/frontend source 和 60-window 上限。
- [x] context invariant 对 target、dates、segments、params、supporting discharge、models 和 memory mode 的拒绝路径。

### Verify

- [x] `python -m pytest backend/tests/domain backend/tests/application -q`（context/resolver focused 集合）
- [x] `python -m pytest backend/tests/domain/test_time.py backend/tests/domain/test_time_windows.py -q`
- [x] `python -m pytest backend/tests/architecture -q`

阶段出口：给定 parser 候选和 fake ports，可纯确定性产出唯一 AnalysisContext 或明确失败；尚未执行 LLM/network/BFS。

提交边界：typed context、纯 resolver 与 ports contract；不得夹带 provider adapter 或 workflow side effects。

#### Milestone 3 完成记录（2026-09-25）

- 实现：完成强类型 `AnalysisContext`、parser 候选模型、地点/日期 resolver 与 geocoding/COMID ports；未调用真实 provider、BFS 或 workflow side effect。
- 验证：新增 focused `60 passed`、domain/application `582 passed`、时间边界 `33 passed`、architecture `77 passed`、完整非 PostgreSQL 回归 `988 passed`。
- 提交：`22a322b`。

## Milestone 4：Query Parser session 与模型调用边界

### 工作

- [x] 定义 parser 专用 request/response DTO 和 `QueryParserPort`/现有泛型 `LLMPort` 的具体 application boundary；provider adapter 不把 SDK 类型泄露进 application/domain。
- [x] 构建 parser prompt：原 query、structured target/date/resolution 安全表示、固化 today/timezone、能力摘要、JSON schema 和少量 few-shot；few-shot 只覆盖显式参数、自主推测与歧义上报。
- [x] 模型输出只含地点/坐标/日期候选、`ParameterConcept[]` 和 ambiguities；禁止输出最终 COMID/ParameterKey/UTC/mode/resolution/graph/evidence sufficiency。
- [x] parser 调用环境不注册任何 geocoding、NLDI、parameter search 或其他 tool，真实请求的 tools 数组/工具 roster 为空。
- [x] 正常只调用一次；仅 JSON/schema invalid 时保存第一次实际输出并进行一次 repair。语义歧义、unsupported、地点/日期业务失败不 repair；第二次 schema invalid 失败。
- [x] 在准备模型调用时创建唯一 query_parser session，不绑定 COMID；保存 `query_parser_input v1`，每次模型调用各自产生原 `query_parser_message v1`，不覆盖第一次消息。
- [x] 流式 delta 继续使用 M3 message lifecycle 和统一脱敏；未完成 JSON 不能成为业务事实。取消后不启动新调用、repair 或 resolver。
- [x] provider/model 从已固化 parser selection 精确路由；凭据只从后端 secret 配置解析，无自动 provider 猜测、fallback 或前端 key。

### Tests

- [x] 一次成功、首轮 schema invalid 后 repair 成功、两次 invalid、provider error、timeout、取消各自的调用计数与终态。
- [x] semantic ambiguity/unsupported 不触发 repair；schema repair 输入只含安全原输出和校验摘要。
- [x] 实际 fake/provider request 的 tools 为空；模型尝试返回 tool call 被拒绝，不进入 resolver。
- [x] parser session 身份、operation identity、message item、delta/finalize、顺序和 transcript 归属正确。
- [x] query 中疑似 key、provider 错误、prompt/response、日志和 SSE 经过统一静态/流式脱敏。
- [x] 不同 parser/waterbody selection 固化，M4 只调用 parser selection。

### Verify

- [x] `python -m pytest backend/tests/application backend/tests/observability -q`（parser/session/stream focused 集合）
- [x] `python -m pytest backend/tests/api/test_analysis_events.py -q`
- [x] 通过 fake adapter 断言模型调用次数和空 tool roster，不调用真实付费模型。

阶段出口：fake parser 能留下真实 session 过程并产出严格 ParsedIntentV1；尚未调用真实 geocoder/NLDI 或构建 topology。

提交边界：parser/model boundary、prompt、repair、session transcript；不得夹带 graph 或 external hydrology adapter。

#### Milestone 4 完成记录（2026-09-25）

- 实现：完成 parser DTO/port、确定性 prompt、一次 schema repair、真实 parser session/message lifecycle 与 `query_understood v1` 交接；未加入 provider adapter、graph 或 hydrology adapter。
- 验证：M4 focused `15 passed`、application/observability `480 passed`、API events `38 passed`、architecture `77 passed`、完整非 PostgreSQL 回归 `1003 passed`；fake adapter 证明每次 `tools=[]`，真实模型请求为 0。
- 提交：`042ec13`；真实 PostgreSQL 不属于本阶段变更范围，未运行。

## Milestone 5：Geocoding、NLDI 与 USGS metadata adapters

### 工作

- [x] 在新系统独立实现共享异步 HTTP 生命周期与 adapter；若引入 runtime dependency，同批更新 `pyproject.toml`、lockfile、README 和跨平台安装测试，不引入仅为单个 adapter 的框架。
- [x] 实现 geocoding 正向/反向 adapter，返回国家、一级行政区、display name、point 和 provider identity；US bias 只是查询约束，application 仍执行 CONUS 校验。
- [x] 实现基于 NHDPlusV2 网络的当前 NLDI position、COMID segment、direct-upstream navigation 和 monitoring-site association；坐标编码使用标准 `POINT(lon lat)`，不进行近邻吸附。
- [x] adapter 必须把原始 navigation 结果规范为直接边；不能让 application 通过重复全量 upstream 查询猜直接关系。
- [x] 实现 USGS WDFN OGC API v1 parameter codes、monitoring locations、time-series metadata 客户端，以及 Samples `/summary` period-of-record 客户端；生产代码不访问旧 WaterServices。
- [x] 统一 provider error taxonomy：successful empty、timeout、rate limit、auth/config、HTTP failure、invalid JSON/schema 和 unsupported API version 分开。
- [x] 只对幂等 GET/metadata 首试加一次有界退避重试；接受阶段 deadline/cancellation，响应大小和分页有界，不把无限目录载入每个请求。
- [x] API key、Authorization、query secret 只存在 infrastructure 边界；错误与日志保存 provider/error identity 和安全 request metadata，不保存 secret 或无界 body。
- [x] 建立 checked-in contract fixtures；网络 contract smoke 单独受环境开关控制，不成为离线测试唯一依据。

### Tests

- [x] geocoder 唯一/多候选/无结果、reverse 行政区、malformed、quota/auth/timeout fixture。
- [x] NLDI position/segment/direct-upstreams/site association 的正常、空、重复、环线输入、无 geometry、字段漂移和非 2xx fixture。
- [x] 明确断言 point 查询为 lon/lat；不得仅用对称坐标 fixture 掩盖顺序错误。
- [x] WDFN continuous/daily metadata 的 begin/end/parameter/station/unit 解析；Samples summary 的离散 period-of-record 解析。
- [x] pagination/limit、响应大小、一次重试、deadline、cancel 和 empty/unavailable/invalid 分类。
- [x] production URL/config 扫描确认无 `waterservices.usgs.gov`，测试 fixture 之外无旧 API。

### Verify

- [x] `python -m pytest backend/tests/infrastructure backend/tests/architecture -q`（按实际新增目录调整）
- [x] `python -m pytest backend/tests -q` 中所有 adapter fixture 离线通过。
- [x] 可选真实 adapter smoke：先打印安全 preflight 状态；实际请求数、API version、成功/失败记录明确，未运行记 `not run`。

阶段出口：新后端拥有独立、类型化、可取消的外部 metadata adapters；尚未把 metadata 当 evidence 或执行 BFS。

提交边界：HTTP/adapters/config/fixtures；不得夹带 topology workflow、M5 evidence bundle 或前端代码。

#### Milestone 5 完成记录（2026-09-25）

- 实现：完成共享有界异步 HTTP client、Google geocoding、当前 NLDI 与 USGS WDFN OGC v1/Samples metadata adapters、typed ports/models、secret settings 和 checked-in contract fixtures；未加入 topology workflow、M5 evidence bundle、BFS 或前端代码。
- 验证：infrastructure/architecture `120 passed`，完整非 PostgreSQL 后端回归 `1055 passed`，`compileall` 与隔离 wheel 构建通过；生产源码旧 WaterServices 扫描为 0。真实 PostgreSQL 未运行（无 schema/persistence 变更）。
- 真实 smoke：匿名调用 Samples v1 `/summary/USGS-06137570` 共 2 次；JSON 请求 HTTP 500，CSV 请求 HTTP 200（12479 bytes），因此只将 CSV contract 记为已验证，不宣称 JSON 成功。
- 提交：`e1a3551`。

## Milestone 6：Station availability qualification

### 工作

- [x] 用 `StationInventoryPort.check_availability(request)` 替换无法表达时间覆盖的旧 `MonitoringStationPort.find_for_segment()` 生产入口；同步导出、fakes、architecture tests 并删除孤立主路径。
- [x] 定义 `StationAvailabilityRequest/StationAvailability/SeriesRef` typed DTO；coverage unit 是 `(ParameterKey, segment_index)`，由目标参数与 supporting discharge 的去重并集生成。
- [x] 按 capability provider route 查询 WDFN metadata 和/或 Samples summary；TS+S 表示两种合法数据形态，不表示无条件 fallback 或两边必有数据。
- [x] 历史 segment 需要 period envelope 覆盖或 capability 指定的段内 existence probe；预测 segment 需要相同参数在起点前 48 小时内有近期基线可能。
- [x] 一站只覆盖部分 unit 时只标记这些 unit；其他参数/segment/discharge 保持未覆盖，供 BFS 继续该支流。
- [x] station 必须有 NLDI 或同等级水文关联事实；不得仅按坐标最近距离选站。
- [x] qualification 保留 provider、station、series refs、checked_at 和覆盖来源；它不是实际观测或 evidence，不进入结果数值。
- [x] provider 技术错误直接失败，不返回空 tuple；阶段 deadline 耗尽后停止新查询。

### Tests

- [x] 历史完整/部分/不覆盖、预测 48h 边界内外、mixed 两段、TS/S/TS+S route。
- [x] 多参数、多站点、同站部分覆盖、discharge 缺失/重复和 filtered/unfiltered 精确身份。
- [x] success-empty、timeout、rate-limit、invalid metadata、目录与 series identity 不一致分开。
- [x] metadata 只能产生 qualification，不能构造 EvidenceFragment、观测值、置信度或推理结果。
- [x] 随机 provider 完成顺序下返回稳定排序和相同序列化。

### Verify

- [x] `python -m pytest backend/tests/application backend/tests/architecture -q`（availability focused 集合）
- [x] 端口签名、旧入口移除和调用方/fixture 全量搜索无残留。

阶段出口：任意河段可得到按参数 × segment 的确定性候选覆盖或明确技术失败；尚未决定完整 graph。

提交边界：availability DTO/port/service/fakes/tests；不得夹带 BFS、evidence 获取或模型推理。

#### Milestone 6 完成记录（2026-09-27）

- 实现：完成 availability request/qualification/series-ref typed DTO、`StationInventoryPort`、capability-driven WDFN/Samples qualification、NLDI 关联门禁、48 小时预测基线、deadline 与稳定并发归并；删除旧 `MonitoringStationPort.find_for_segment()`，未加入 BFS、evidence 获取或模型推理。
- 验证：application/architecture `515 passed`，完整非 PostgreSQL 后端回归 `1068 passed`，`compileall` 与旧入口/禁用 evidence 类型扫描通过；真实 PostgreSQL 未运行（无 schema/persistence 变更）。
- 提交：`c42037f`。

## Milestone 7：HydrologyGraph 与稳定有界 BFS

### 工作

- [x] 实现 `HydrologyGraph`、node、edge、coverage、branch outcome、exclusion reason 和 layer typed models；domain invariant 验证 target、引用闭包、边方向、acyclic active graph、稳定唯一身份。
- [x] builder 从 target 开始逐层向上游，每条支流分别维护未覆盖 coverage units；找到最近合格 source 后只停止已覆盖 unit，未覆盖 unit 继续。
- [x] 始终保留 target 供后续本地/非站点 evidence 检查；没有 qualifying inlet 时不得把 target 伪装成 inlet。
- [x] 实现 100 km/路径、12 hop/路径、128 unique COMID/整图和 8 外部请求并发；限制来自 composition root，不由 request/前端覆盖。
- [x] 以 `(depth, cumulative_distance, comid)` 等稳定业务键合并同层结果；并发完成顺序、set/dict 顺序不得改变 nodes/edges/layers/JSON。
- [x] 正确处理多支流、汇流、共享上游、重复响应、self-edge、cycle、自然无上游和 limit hit；visited 不得让一条路径错误吞掉另一支流 coverage。
- [x] active graph 是选中 inlet 到 target 的路径并集；所有 explored 但未参与节点保留结构化 excluded reason。
- [x] `no_qualifying_source` 与 `search_limit_reached` 分开；任何必要外部技术失败、deadline 或 graph invariant 失败使 topology 阶段失败，不发布 partial graph。
- [x] builder 是 run 确定性过程，不创建、启动或完成任何 Agent session。

### Tests

- [x] target 有完整 coverage、单链、多支流分别命中、同站部分覆盖、汇流、共享节点和无上游。
- [x] cycle/self-edge/duplicate/malformed distance 的防护；active union、excluded 和 layers invariant。
- [x] 100 km、12 hop、128 node 独立边界及同时命中；第 129 个节点不突破整图限制。
- [x] 并发峰值不超过 8；随机延迟/完成顺序运行多次输出字节一致。
- [x] natural empty/limit hit/timeout/rate-limit/invalid response 语义不混淆。
- [x] 每个 segment 的每个 coverage set 都包含去重后的 discharge；partial coverage 不错误提前停止整条支流。
- [x] builder 全程没有 waterbody session 创建调用。

### Verify

- [x] `python -m pytest backend/tests/domain backend/tests/application -q`（hydrology topology focused 集合）
- [x] property/parameterized fixture 重复不同调度顺序，graph canonical JSON hash 一致。
- [x] `python -m pytest backend/tests/architecture -q`

阶段出口：给定 fake topology/availability ports，能稳定产生唯一 HydrologyGraph 或明确失败；尚未接入公开 API/SSE。

提交边界：graph domain、builder、deterministic tests；不得夹带 transport DTO、M5 evidence 或水体 Agent。

完成记录（2026-09-27）：

- 实现：完成 graph/node/edge/outcome/exclusion/layer typed domain、稳定有界逐支流 BFS、active path union、NLDI edge distance 与 deadline/技术失败收敛；未接入 transport、M5 evidence 或水体 Agent。
- 验证：domain/application/architecture `720 passed`，完整非 PostgreSQL 后端回归 `1089 passed`，`compileall` 与稳定 canonical JSON hash 检查通过；真实 PostgreSQL 未运行（未设置 `WATERAGENTS_TEST_DB_NAME`，且本阶段无 schema/persistence 变更）。
- 提交：`6622b00`。

## Milestone 8：M3 workflow、typed content 与持久化接入

### 工作

- [x] 将 create→parser session→resolver→context→topology 的流程接入当前唯一 `AnalysisWorkflow/RunCoordinator`，保持 task ownership、短临界区和外部 I/O 在锁外。
- [x] 注册 `query_parser_input v1`、`query_parser_message v1`、`query_understood v1` 和 `topology_ready v1`；使用 production registry 规范序列化，不手写旁路 JSON。
- [x] `query_understood` 只在 parser schema 与全部 resolver 门禁通过后写入 parser session；成功后自动进入 topology，不增加人工确认状态。
- [x] `topology_ready` 是 run scope 原 item；context/graph 各保存一份完整正文，大正文复用 M3 external-content/reference，不在 snapshot/history 复制。
- [x] phase 只使用 `queued/query_understanding/topology_building/topology_ready/terminal`；它不是 run lifecycle 状态，状态仍沿用 M3 六态/session 五态。
- [x] 取消可在模型、repair、resolver、metadata 查询和 BFS 边界停止新工作；已产生的原消息/context 按 M3 规则保存，取消后不发布 topology_ready。
- [x] parser/resolver 业务失败、parser 技术失败、topology 技术失败分别映射 session/run 终态；context 已成功而 topology 失败时 context 仍可读取。
- [x] 保持 writer batch/补写/终态原子组、startup repair、quiesced、cleanup grace 和 persistence warning；不建立 M4 专用 writer/queue/retry。
- [x] workflow 完成 topology 后只留下 M5 接力事实，不创建 waterbody sessions、不调用 waterbody model，也不生成 placeholder result。

### Tests

- [x] 完整 fake workflow 的 item/session/run 顺序、scope、identity、phase 和最终 reference。
- [x] 业务失败在 topology 前停止；parser/provider/topology 技术失败的 failed_stage/retryable 与调用计数正确。
- [x] cancel 在首次模型、repair 前、resolver、availability、BFS 各 gate 的竞态；没有晚启动调用或 orphan task/session。
- [x] context committed 后 topology 失败/取消仍可读；topology item 与 phase/reference 原子可见。
- [x] writer 失败/补写、未知提交、终态交付窗口、startup interruption 和 live cleanup regression。
- [x] 同 key 并发创建只执行一次 M4 workflow；响应丢失重试不重复调用 parser/provider。

### Verify

- [x] `python -m pytest backend/tests/application backend/tests/persistence -q`（本机适用集合通过；真实 PostgreSQL 因未设置专用测试库变量记为 `not run`）
- [x] `python -m pytest backend/tests/observability backend/tests/architecture -q`
- [x] 使用 fake ports 记录完整调用图，证明 0 个 waterbody session/model 调用。

阶段出口：完整 M4 workflow 在内存与持久化边界成立，所有原事实可读取；公开 DTO/SSE 投影尚由下一阶段完成。

提交边界：workflow/registry/persistence integration 纵切；不得新增路由或复制 context/graph 读模型。

#### Milestone 8 完成记录（2026-09-27）

- 实现：单一 workflow 串联 parser/resolver/context/有界 topology，四类 M4 typed content 统一走 production registry 与 M3 writer；phase/reference 同锁更新，未新增路由、writer、waterbody session/model 或结果占位。
- 验证：focused `114 passed`、application/非 PostgreSQL persistence `491 passed`、observability/architecture `158 passed`、完整非 PostgreSQL 后端回归 `1100 passed`，`compileall` 与 diff 检查通过；真实 PostgreSQL `16` 个用例因未设置 `WATERAGENTS_TEST_DB_NAME` 记为 `not run`。
- 提交：`8786a8e`。

## Milestone 9：HTTP、history、两层 SSE 与公开错误

### 工作

- [x] 更新实际 OpenAPI 和 create route 为最终 M4 request；保留 M3 路由集合，不新增 M4 专用 create/history/topology 或第三条流。
- [x] 扩展唯一 `run_snapshot v1`：phase、context_ref、topology_ref 和轻量 waterbody summaries；snapshot 只引用完整 graph，不内联节点/边/excluded。
- [x] history/detail 从持久化 `query_understood` 投影 nullable `context_summary`；未产生为 null，未知/损坏内容进入 issues，不从 query 临时重算。
- [x] session SSE 继续发送 parser message 生命周期和 context transcript item；run SSE 在原事实提交后发布下一份完整 snapshot，不新增独立 `topology_ready` event。
- [x] run 重连首发最新完整状态且无 SSE id；session 按 cursor 补过程。断线不取消 run，释放后 410/历史入口和 404 边界不回退。
- [x] 实现 API 文档中的 422 与异步 `PublicError` code/failed_stage/retryable 矩阵；安全 message 不包含 provider body、URL key、prompt secret、路径或 stack。
- [x] frontend-visible `params_used` 展示 ParameterKey、explicit/inferred 和 rationale，`supporting_params` 单独展示；discharge 不伪装为用户选择。
- [x] 文档 JSON fixture 必须从 production registry/reader/SSE encoder/OpenAPI 生成或验证，不能维护漂移的第二份手写 schema。

### Tests

- [x] create 422 matrix、201 receipt、异步业务/技术失败、错误安全字段和 request/error correlation。
- [x] parser 两次 message、query_understood、topology_ready 的 registry round-trip、inline/external content 和 PostgreSQL reload。
- [x] history summary 未解析/context 完整/corrupt/unknown；详情全文引用与 item ownership。
- [x] session cursor replay、run reconnect、snapshot 合并、reference 原子可见、stream_end、410/404 和客户端断开。
- [x] OpenAPI required/nullable/enum/extra、六项上限描述、双模型和无 API key 字段。
- [x] M3 pagination、cancel、persistence retry、全文、SSE redaction 与旧历史读取 regression。

### Verify

- [x] `python -m pytest backend/tests/api backend/tests/application/test_transcript_registry.py -q`
- [x] `python -m pytest backend/tests/persistence -q`（非 PostgreSQL contract 集合通过；真实 PostgreSQL 因未配置专用测试库记为 `not run`）
- [x] 可执行 documented-protocol test 校验 API 文档所有 JSON/OpenAPI/SSE fixture。

阶段出口：最终 M4 协议可从新后端真实运行时读取，当前与历史一致；仍不代表产品已经完成 M5–M9 或可开始正式联调。

提交边界：API/read projection/SSE/errors/docs fixture；不得实现新前端或后续推理行为。

#### Milestone 9 完成记录（2026-09-27）

- 实现：最终 M4 create/OpenAPI、`run_snapshot v1` phase/reference、typed context history/detail 投影、既有两层 SSE 与安全公开错误已接入；未新增路由、第三条流、前端或后续推理行为。
- 验证：API/registry `236 passed`、非 PostgreSQL persistence contract `40 passed`、documented protocol `2 passed`、完整非 PostgreSQL 后端回归 `1105 passed`，`compileall` 与 diff 检查通过；真实 PostgreSQL `17` 个用例（含新增 context reload）因未设置 `WATERAGENTS_TEST_DB_NAME` 记为 `not run`。
- 提交：`e4d7aa7`。

## Milestone 10：集成验收、契约审计与 M5 接力

### 工作

- [x] 建立固定 fake model/geocoder/NLDI/WDFN/Samples fixtures 的完整 M4 workflow smoke：frontend target/date、query fallback、mixed、部分 coverage、无 source 和技术失败。
- [x] 建立 154 项 manifest 离线审计和可选官方 refresh 工具；refresh 只能报告 drift，不能在运行时自动改 manifest 或把新官方条目自动启用。
- [x] 完整回归 M1–M3：创建幂等、四表、writer、读取、分页、两层 SSE、取消、重启修复、补写、脱敏、跨平台入口和 smoke。
- [ ] 在专用 PostgreSQL 测试库执行真实 persistence/重启验证；保留数据库安全门禁，不接触业务库。
- [ ] 对 geocoding/NLDI/WDFN/Samples 和至少一个已配置 parser provider 分别做可选 contract smoke；先预检网络/凭据，记录请求数、版本和结果，未运行不冒充通过。
- [x] 审计 `GLOBAL-C01`–`GLOBAL-C11`、`M4-C01`–`M4-C13`、`M4-A01`–`M4-A07`，逐项填写实现位置、测试、实际结果与剩余风险。
- [x] 搜索并清除被替换的 production 入口、旧字段、重复 allowlist、孤立 method、M4 兼容分支和测试专用生产开关；不清理无关历史代码。
- [x] 同步本 TODO、实施设计、API、参数目录、架构骨架、总 Milestones、延后契约和前端接入边界；只把有证据的项目勾选。
- [x] 写明 M5 接力：消费同一 capability registry、AnalysisContext、HydrologyGraph、station/series refs；重新读取真实 observation，区分 available/empty/unavailable/invalid，不把 qualification 当 evidence。
- [x] 写明 M6/M7/M8/M9 接力：evidence gate、waterbody session 创建、memory mode 实现和新前端仍未由 M4 完成；不得把 M4 smoke 称作第一版正式端到端验收。

### Tests

- [x] full fake workflow 覆盖 happy path、所有业务停止点、所有技术失败阶段、取消竞态、幂等与持久化 reload。
- [x] 154 项 manifest、6 项上限、discharge、CONUS、±7 日、48h、四个 topology hard limits 和稳定排序均有可执行门禁。
- [x] secrets、普通 query、provider URL、流式跨 chunk 和公开错误全出口扫描回归。
- [x] 安装包资源、不同 cwd、Windows/Linux/macOS 路径与事件循环的适用 architecture/smoke 不回退；三平台真实 CI 仍为 `partial`。
- [x] 合同 ID 集合与契约矩阵双向一致；文档链接、JSON、表格和 OpenAPI fixture 校验通过。

### Verify

- [ ] `python -m pytest backend/tests -q`
- [ ] `python -m pytest backend/tests/persistence -q`（隔离 PostgreSQL）
- [x] `python -m compileall -q backend/app backend/scripts`
- [ ] `python backend/scripts/health_smoke.py`
- [x] `python backend/scripts/run_sse_smoke.py`
- [ ] `python backend/scripts/run_history_smoke.py`
- [x] 新增 M4 fake-workflow smoke 与适用离线 adapter contract smoke；官方 WDFN parameter refresh 以 1 个请求通过，其余需凭据/外部项保持 `not run`。
- [x] `git diff --check`

阶段出口：M4 内部切片的代码、测试、协议和接力证据完整；没有把外部未运行项写成通过，也没有开始正式前后端联调。下一步是 M5，不是发布“M4 版本”。

提交边界：只包含集成修复、审计、smoke、文档同步和完成记录；发现实质功能缺口时回到对应 Milestone 修复并重跑其 focused gate，不能把大补丁藏进最终审计提交。

#### Milestone 10 完成记录（2026-09-27）

- 实现：新增固定 fake model/geocoder/NLDI/WDFN/Samples 的完整 M4 workflow smoke、154 项离线审计与只报告 drift 的官方 refresh；修正 availability 内层失败的公开分类和 standalone run SSE 的自包含配置，同步契约矩阵、CI、API/设计/参数/架构/前端接力文档；未实现 M5–M9 行为或新前端。
- 验证：focused contract/smoke `6 passed`、standalone SSE focused `21 passed`、完整非 PostgreSQL backend regression `1109 passed`，`compileall`、offline audit、run SSE、M4 workflow 和 `git diff --check` 通过；USGS OGC v1 refresh 为 `1` 个实际请求、`154/154`、无 key/name/unit drift且未写 manifest。真实 PostgreSQL `17` 个用例、health/history smoke、Google/NLDI/Samples 与真实 parser provider contract smoke 因未配置专用测试库/凭据而为 `not run`，三平台 CI 保持 `partial`。
- 提交：`91ba2a6`。

#### Milestone 10 审查修复记录（2026-09-27）

- 实现：生产 lifespan 统一通过 `build_m4_runtime(settings)` 装配 OpenAI parser、Google/NLDI/USGS resolver、availability 与 topology，缺组件在数据库启动前失败；逐 child 保存 topology 上限身份，按 location/parameter 区分 resolver 依赖错误，并为每次 repair 调用保存同 operation 的实际 input。同步收束共用取消等待和 availability 预编译索引，未新建完成说明文档。
- 验证：新增 adapter/runtime/错误语义/双支流/repair/partial 回归；完整非 PostgreSQL backend `1091 passed`，入口相关 focused `130 passed`，`compileall`、M4 workflow smoke（mixed=`partial`）、standalone SSE、154 项离线 audit 与 `git diff --check` 通过。专用 PostgreSQL、真实 Google/NLDI/WDFN/Samples/OpenAI 请求和三平台 CI 因本机未配置继续为 `not run`/`partial`。
- 提交：`f1d57ca`。

#### Milestone 10 production parser streaming 补充记录（2026-09-27）

- 实现：OpenAI Responses parser 使用单次 POST SSE；正常 completed 文本必须由 delta 拼接、`response.output_text.done.text` 与 terminal `response.output` 三方一致，重复/缺失终态与字段错误均作为 invalid provider response。refusal/incomplete/failed/cancelled/error、取消、超时、大小上限和脱敏语义不变；删除无 production caller 的 `post_json`，metadata GET 仍有界重试，streaming POST 仍不重试。未修改 resolver、topology、availability、数据库 schema 或其他 provider 行为。
- 环境：原 `backend/.venv/Scripts/python.exe` 缺失；在 `backend/` 使用 `py -3.13 -m venv --clear .venv` 和 `.venv/Scripts/python.exe -m pip install -e ".[dev]"` 按仓库 README 重建，解释器为 Python `3.13.2`，`python -m pip check` 报告 `No broken requirements found`，未复制 Python executable。
- 验证：仓库根执行 `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/infrastructure/test_openai_responses_parser.py backend/tests/infrastructure/test_provider_http.py backend/tests/application/test_query_parser_workflow.py -q` 为 `52 passed`；`backend/.venv/Scripts/python.exe -B -m pytest backend/tests -q --ignore=backend/tests/persistence/test_postgres_persistence.py` 为 `1158 passed`；`backend/.venv/Scripts/python.exe -B backend/scripts/m4_workflow_smoke.py` 通过且 mixed coverage 为 `partial`；`backend/.venv/Scripts/python.exe -B backend/scripts/run_sse_smoke.py` 输出 `{"completed":"passed","cancelled":"passed"}`；`git diff --check` 通过。
- 未运行：未设置 `WATERAGENTS_TEST_DB_NAME`、OpenAI/Google provider 凭据且无 `backend/.env`，专用 PostgreSQL 与真实 provider smoke 为 `not run`；本轮仅是 M4 parser streaming 的本机离线/fixture 回归，不代表 M4 最终验收全部完成。

## 契约覆盖矩阵（实施时填写）

| 契约 | 状态 | 实现位置 | 重点测试 | 证据与剩余风险 |
|---|---|---|---|---|
| GLOBAL-C01 / GLOBAL-C02 / GLOBAL-C03 | 本机已验证 | `tests/architecture`；当前唯一 request/domain/workflow | archive/dependency guards；完整非 PostgreSQL回归 | 归档隔离、单一首版演进与 COMID 身份成立；中心/三平台证据另缺。 |
| M4-C01 / M4-C02 / M4-C03 | 本机已验证 | `transcripts/models.py`；`query_understanding/context_*`、`date_resolver.py` | request/context/date/OpenAPI tests | 最终 request/context、±7 日期、inclusive end、mixed 与 auto/default resolution 本机通过。 |
| M4-C04 / M4-A03 | 本机已验证 | `parameter_capabilities.v1.json`；`ParameterResolver` | manifest/resolver/audit tests | 154/154、最多 6 项、必需 discharge；官方 OGC v1 refresh 1 请求无 drift，真实 parser provider 未配置。 |
| M4-C05 / M4-C09 / M4-A02 / M4-A05 | 本机已验证 | `LocationResolver`；`QueryParserWorkflow`；模型目录 | location/date/parser/workflow tests | target/date 优先级、CONUS/COMID、双模型、空 tools 与一次 repair 本机通过；Google/parser 外部 smoke 未运行。 |
| M4-C06 / M4-C10 / M4-C11 / M4-A01 / M4-A06 / M4-A07 | 本机已验证 | `StationAvailabilityService`；`HydrologyGraphBuilder`；NLDI/WDFN/Samples adapters | adapter fixtures、availability/topology tests、`m4_workflow_smoke.py` | 固定 fixtures 覆盖 partial/no-source/技术失败与稳定 hard limits；NLDI/Samples 外部 smoke 未运行，qualification 仍非 evidence。 |
| M4-C07 / M4-C08 / M4-A04 | 本机已验证 | production registry、writer/reader、run snapshot、既有两层 SSE | workflow/registry/API/history/SSE/documented protocol | 原 item 归属、history 投影、reference 与重连本机通过；真实 PostgreSQL reload 未运行。 |
| M4-C12 | partial | `AnalysisContext`、`HydrologyGraph`、capability evidence rules | M4 boundary/architecture tests | M5/M6 evidence gate 输入接缝已冻结；真实 observation 与 gate 尚属 M5/M6。 |
| M4-C13 | partial | request/config/context 的 `memory_mode`；M5–M9 handoff | request/context tests | M4 接缝本机通过；M8 memory、M9 新前端和统一端到端尚未实现。 |
| GLOBAL-C04–C11 | partial | UTC/time models、registry/redaction、docs/CI workflow | time/redaction/resource/cwd/event-loop/architecture tests | 适用本机门禁通过；GLOBAL-C06 由 M5/M6 完成，C08 由 M9 完成，C11 缺三平台实际 CI/同库迁移。 |

状态只允许：`pending`、`partial`、`本机已验证`、`外部已验证`。不能仅凭代码存在填写 `implemented`；中心/远端证据与本机证据分开。

## 实际完成记录格式

每个 Milestone 完成时，直接勾选对应的工作、Tests 和 Verify 项，并在该节“提交边界”之后追加简短记录；不再新建单独的 Milestone completion Markdown：

```text
#### Milestone N 完成记录（YYYY-MM-DD）

- 实现：关键 production 契约与明确未实现边界。
- 验证：实际 pass/fail/skip/not run 结果及必要的环境说明。
- 提交：commit SHA。
```

不得把计划命令写成已执行结果，不得复制旧 Milestone 的 pass count，不得用 exit 0 代替业务断言，也不得因预算、凭据或网络缺失把 `not run` 写成通过。

## M5 接力出口

M4 完成后，M5 只从以下稳定事实继续：

- 唯一 `AnalysisContext`：目标 COMID/point、日期/UTC segments、mode、实际 resolution、最多 6 个目标、supporting discharge、模型选择和 memory mode；
- 唯一 `HydrologyGraph`：target、explored/active/excluded、inlets、coverage、station/series refs、branch outcomes 和稳定 layers；
- 唯一 capability registry：154 个目标能力的单位、provider route、aliases 和允许 evidence relation；
- M3 原记录与读取路径：context 属于 parser session，graph 属于 run，history/SSE 只投影/引用。

M5 必须读取真实 observation 并重新验证时间覆盖、质量、单位和完整性；M4 metadata qualification 不能直接成为 EvidenceFragment。M6 才执行确定性 sufficiency gate 并决定是否调用水体模型，M7 才按 active DAG 在准备执行时创建 waterbody session，M8 才实现 memory mode，M9 才实现新前端并开始统一端到端验收。
