# M3 Transcript Store + History APIs Implementation TODO

更新日期：2026-09-13

本清单执行 [M3 实施详细设计](m3_transcript_history_implementation_design.md)，同时维护 [API 与搭建排障指南](m3_transcript_history_api.md)、父目录 [前端接入约定](../frontend_integration_contracts.md) 和 [中心验收契约](../deferred_milestone_contracts.md#5-m3transcript-store--history-apis)。[D01–D86 决策记录](m3_transcript_history_design.md) 保留已确认依据。

**当前状态：Milestone 0–10 均待实施与验证。** 本轮完成工程文档整理，没有实现数据库、路由、迁移、配置文件或测试。下列命令是后续实施时的 Verify 目标，不是本轮运行证据。M2 原验收记录保留原义，不能当作 M3 基线或三平台通过结果。

## 阶段与设计导航

执行某个阶段时，先从下表进入对应设计，再按复选项末尾的链接定位具体规则。`设计 §…` 指实施详细设计，`API §…` 指接口与搭建指南；原 D01–D86 用于追溯决策，不要求每项任务重新通读。工作、Tests 和 Verify 均有依据链接，命令是否通过仍须实际验证。

| 实施阶段 | 设计定位 | 接口、配置或接力依据 |
|---|---|---|
| [Milestone 0：基线、适用契约与工作区](#milestone-0基线适用契约与工作区) | [设计 §1.2](m3_transcript_history_implementation_design.md#12-后续阶段承接)；[设计 §2](m3_transcript_history_implementation_design.md#2-当前代码与目标模块) | [M2 接力与验收](../M2/m2_analysis_session_sse_implementation_todo.md#后续接力入口与验收)；[契约 §2](../deferred_milestone_contracts.md#2-跨-milestone-硬约束) |
| [Milestone 1：Run / Agent session 模型和统一状态入口](#milestone-1run--agent-session-模型和统一状态入口) | [设计 §3.1](m3_transcript_history_implementation_design.md#31-身份与顺序)；[设计 §3.2](m3_transcript_history_implementation_design.md#32-当前状态)；[设计 §3.3](m3_transcript_history_implementation_design.md#33-时间) | [API §3.2](m3_transcript_history_api.md#32-runsummary-与-sessionsummary)；[设计 §4.2](m3_transcript_history_implementation_design.md#42-analysis_sessions) |
| [Milestone 2：PostgreSQL、四表 schema 与可复现环境](#milestone-2postgresql四表-schema-与可复现环境) | [设计 §4](m3_transcript_history_implementation_design.md#4-postgresql-schema)；[设计 §10.1](m3_transcript_history_implementation_design.md#101-技术选择)；[设计 §10.2](m3_transcript_history_implementation_design.md#102-配置与命令) | [API §8.1](m3_transcript_history_api.md#81-环境变量)；[API §8.2](m3_transcript_history_api.md#82-本地-compose)；[API §8.3](m3_transcript_history_api.md#83-已有数据库与升级) |
| [Milestone 3：Typed content、单份正文、引用与安全入口](#milestone-3typed-content单份正文引用与安全入口) | [设计 §5](m3_transcript_history_implementation_design.md#5-typed-content-与读取表示)；[设计 §9.2](m3_transcript_history_implementation_design.md#92-凭据边界) | [API §3.3](m3_transcript_history_api.md#33-transcriptitemview)；[API §4.4](m3_transcript_history_api.md#44-全文) |
| [Milestone 4：创建原子事务、幂等和所有权收敛](#milestone-4创建原子事务幂等和所有权收敛) | [设计 §6.1](m3_transcript_history_implementation_design.md#61-创建与所有权)；[设计 §6.3](m3_transcript_history_implementation_design.md#63-原子提交超时与核实) | [API §2](m3_transcript_history_api.md#2-创建与幂等) |
| [Milestone 5：串行写入、确认集合与有限补写](#milestone-5串行写入确认集合与有限补写) | [设计 §6.2](m3_transcript_history_implementation_design.md#62-runwriter)；[设计 §6.4](m3_transcript_history_implementation_design.md#64-重试和交付窗口)；[设计 §6.5](m3_transcript_history_implementation_design.md#65-保存状态手动恢复与清理) | [API §3.1](m3_transcript_history_api.md#31-来源与保存情况)；[API §6.2](m3_transcript_history_api.md#62-persistenceretry) |
| [Milestone 6：统一读取、历史分页与 HTTP 契约](#milestone-6统一读取历史分页与-http-契约) | [设计 §7](m3_transcript_history_implementation_design.md#7-统一读取与历史查询) | [API §3](m3_transcript_history_api.md#3-当前与历史的共同返回模型)；[API §4](m3_transcript_history_api.md#4-get-读取接口)；[API §7](m3_transcript_history_api.md#7-错误与排查字段) |
| [Milestone 7：两层 SSE、消息生命周期与续传](#milestone-7两层-sse消息生命周期与续传) | [设计 §8](m3_transcript_history_implementation_design.md#8-两层-sse-与消息装配) | [API §5](m3_transcript_history_api.md#5-两层-sse) |
| [Milestone 8：取消、遗留修复、关闭与 live 清理](#milestone-8取消遗留修复关闭与-live-清理) | [设计 §9.1](m3_transcript_history_implementation_design.md#91-原消息与晚到审计)；[设计 §10.3](m3_transcript_history_implementation_design.md#103-启动恢复和关闭)；[设计 §6.5](m3_transcript_history_implementation_design.md#65-保存状态手动恢复与清理) | [API §6.1](m3_transcript_history_api.md#61-cancel)；[API §5.4](m3_transcript_history_api.md#54-游标与结束) |
| [Milestone 9：完整集成、三平台 smoke 与运行手册](#milestone-9完整集成三平台-smoke-与运行手册) | [设计 §11](m3_transcript_history_implementation_design.md#11-测试与验收设计)；[设计 §10.4](m3_transcript_history_implementation_design.md#104-跨操作系统可迁移性) | [API §8.5](m3_transcript_history_api.md#85-更换操作系统或机器的最短流程)；[API §9](m3_transcript_history_api.md#9-排障与验证) |
| [Milestone 10：契约验收与后续交接](#milestone-10契约验收与后续交接) | [设计 §12](m3_transcript_history_implementation_design.md#12-完成标准与接力)；[设计 §1.2](m3_transcript_history_implementation_design.md#12-后续阶段承接) | [契约 §13](../deferred_milestone_contracts.md#13-承接记录)；[前端约定 §5](../frontend_integration_contracts.md#5-m4m9-的前端接力边界) |

以下原任务内容与未完成状态保持不变，链接仅提供定位；修改设计章节标题时须同步校验这些锚点。

## 执行原则

- [ ] 开始时记录已有用户改动；只修改新 backend、适用 CI、M3 与必要上层文档，不覆盖无关文件。（[设计 §1.1](m3_transcript_history_implementation_design.md#11-本阶段实现)；[设计 §1.2](m3_transcript_history_implementation_design.md#12-后续阶段承接)）
- [ ] 不读取或复用归档后端、归档前端与旧契约；以 M1/M2 当前新代码和中心契约为基线。（[契约 §2](../deferred_milestone_contracts.md#2-跨-milestone-硬约束)）
- [ ] 直接将旧整体 session 职责迁到 run，真实 Agent session 使用独立上下文；旧 HTTP 路径不保留兼容别名。（[设计 §2](m3_transcript_history_implementation_design.md#2-当前代码与目标模块)；[API §1](m3_transcript_history_api.md#1-基础约定与路由)）
- [ ] 坚持四张核心业务表、原正文一份、公共上下文原引用；不新增创建 key 表、关联索引表、状态回放系统或地图专用存储。（[设计 §4](m3_transcript_history_implementation_design.md#4-postgresql-schema)）
- [ ] 细节由实现者在已确认保证内完成，不逐字段等待用户审批；实质改变行为/数据保证/范围才重新对齐。（[决策记录 §4](m3_transcript_history_design.md#4-工程定型与后续阶段边界)）
- [ ] 每个阶段先完成相关工作与 focused tests；达到阶段出口再继续。最后运行适用完整回归和平台 smoke，不重复堆叠无关测试。（[设计 §11](m3_transcript_history_implementation_design.md#11-测试与验收设计)）
- [ ] 数据库约束与事务使用真实 PostgreSQL 验证；fakes 负责确定性时序和故障，不代替真实提交证据。（[设计 §11](m3_transcript_history_implementation_design.md#11-测试与验收设计)）
- [ ] 时间使用 ClockPort，时序测试用 gate/可控计时，不真实等待整轮退避或 60 秒清理。（[设计 §3.3](m3_transcript_history_implementation_design.md#33-时间)；[设计 §11](m3_transcript_history_implementation_design.md#11-测试与验收设计)）
- [ ] 依赖、uv.lock、README、OpenAPI、API/SSE fixture 和 smoke 与功能同批维护。（[API §9](m3_transcript_history_api.md#9-排障与验证)）
- [ ] 按 GLOBAL-C11 保持 macOS/Windows/Linux 同一份业务代码、依赖锁、配置格式与 schema；换系统通过配置和重建运行环境完成，不要求手工改源码。（[设计 §10.4](m3_transcript_history_implementation_design.md#104-跨操作系统可迁移性)）
- [ ] 文档不出现内部参考项目名称、私有路径或专有实现标识；使用本项目定义及公开技术说明。（[决策记录 §1](m3_transcript_history_design.md#1-依据与当前基线)）
- [ ] 每阶段登记实际改动位置、命令、环境和结果；缺少的证据保持待办或 partial。（[证据记录格式](#实际证据记录格式)）

## 成功标准

- [ ] 一个 run 可拥有问题解析与多个 COMID sessions；状态、身份、时间与独立上下文正确，未执行节点不预建 session。（[设计 §3.2](m3_transcript_history_implementation_design.md#32-当前状态)）
- [ ] 全新 PostgreSQL 环境能独立初始化/升级，三个平台按文档搭建；启动严格检查并修复遗留后才接收请求。（[设计 §10.2](m3_transcript_history_implementation_design.md#102-配置与命令)；[设计 §10.3](m3_transcript_history_implementation_design.md#103-启动恢复和关闭)）
- [ ] 三系统分别作为迁移目标，用同版代码和锁安装运行；接回同一测试数据库后原身份、历史游标、回执与全文引用保持有效，不转移未保存内存或自动恢复 Agent。（[设计 §10.4](m3_transcript_history_implementation_design.md#104-跨操作系统可迁移性)；[API §8.5](m3_transcript_history_api.md#85-更换操作系统或机器的最短流程)）
- [ ] 创建事务提交后才执行，同 key + 原 body 返回原 201 回执；响应丢失、并发和未知提交都不会重复执行。（[设计 §6.1](m3_transcript_history_implementation_design.md#61-创建与所有权)）
- [ ] 过程增量保存、同批原子、稳定身份及明确提交确认成立；生成中草稿不能被最大序号误判已保存。（[设计 §6.2](m3_transcript_history_implementation_design.md#62-runwriter)；[设计 §6.3](m3_transcript_history_implementation_design.md#63-原子提交超时与核实)）
- [ ] 有限补写、第三次失败后两秒的完成交付窗口、手动恢复和唯一业务终态相互独立。（[设计 §6.4](m3_transcript_history_implementation_design.md#64-重试和交付窗口)；[设计 §6.5](m3_transcript_history_implementation_design.md#65-保存状态手动恢复与清理)）
- [ ] 当前读取与严格历史共享 DTO/解析/全文实现；PG 故障时 live 可读，history 不混入内存。（[设计 §7.1](m3_transcript_history_implementation_design.md#71-一次选择来源)）
- [ ] 只有 run 历史列表分页，任一实际 session 的完整过程与显式公共全文都可读取。（[设计 §7.2](m3_transcript_history_implementation_design.md#72-run-详情与-session-完整过程)；[设计 §7.4](m3_transcript_history_implementation_design.md#74-run-历史分页)）
- [ ] 两层 SSE、首次完整过程、整数续传、三阶段消息和明确 stream_end/410 成立；正文不逐帧重复落库。（[设计 §8.2](m3_transcript_history_implementation_design.md#82-session-过程)；[设计 §8.3](m3_transcript_history_implementation_design.md#83-续传握手与结束)）
- [ ] 取消即固定原生成中正文，晚到审计仅历史可见；凭据跨片段、取消边界和日志均不泄漏。（[设计 §9.1](m3_transcript_history_implementation_design.md#91-原消息与晚到审计)；[设计 §9.2](m3_transcript_history_implementation_design.md#92-凭据边界)）
- [ ] 只有终态、执行收敛、全部实际保存和完整 grace 后清理；重启不自动恢复 Agent 或补造未保存内容。（[设计 §6.5](m3_transcript_history_implementation_design.md#65-保存状态手动恢复与清理)；[设计 §10.3](m3_transcript_history_implementation_design.md#103-启动恢复和关闭)）
- [ ] 中心全部适用契约有实现位置和验证证据，未完成的跨平台验证或业务后续项准确登记。（[设计 §12](m3_transcript_history_implementation_design.md#12-完成标准与接力)；[契约覆盖矩阵](#契约覆盖矩阵)）

## Milestone 0：基线、适用契约与工作区

### 工作

- [ ] 记录分支/HEAD、git status、已有改动和 Python/依赖版本；保留 M2 原有文档改动。（[证据记录格式](#实际证据记录格式)）
- [ ] 阅读 M1/M2 设计、实现、API 与当前测试，确认整体协调器、ClockPort、PublicError、CorrelationContext、SSE encoder/response、redaction 和 factory/lifespan 入口。（[M1 详细设计](../M1/m1_project_shell_domain_design.md)；[M2 接力与验收](../M2/m2_analysis_session_sse_implementation_todo.md#后续接力入口与验收)）
- [ ] 核实现有 TranscriptStorePort 及架构导入约束，列出所有调用方、测试替身与将迁移的旧名称。（[设计 §2](m3_transcript_history_implementation_design.md#2-当前代码与目标模块)）
- [ ] 核对中心 GLOBAL、M3-C01–C20、M3-A01–A12 与 M2 接力；记录仍未取得的 M2 三平台证据，不猜测通过。（[契约 §2](../deferred_milestone_contracts.md#2-跨-milestone-硬约束)；[M2 接力与验收](../M2/m2_analysis_session_sse_implementation_todo.md#后续接力入口与验收)）
- [ ] 列出目标目录和依赖改动；保持前端实现、真实业务 Agents 与无关 refactor 在范围外。（[设计 §1.2](m3_transcript_history_implementation_design.md#12-后续阶段承接)；[设计 §2](m3_transcript_history_implementation_design.md#2-当前代码与目标模块)）
- [ ] 确认参考代码仅作为只读设计依据，交付文档不包含内部名字。（[决策记录 §1](m3_transcript_history_design.md#1-依据与当前基线)）

### Tests / Verify

- [ ] 在现有可用环境运行基线：`python -m pytest backend/tests -q`。（[设计 §11](m3_transcript_history_implementation_design.md#11-测试与验收设计)）
- [ ] `git diff --check`；记录本机基线结果及确实存在的失败。（[证据记录格式](#实际证据记录格式)）
- [ ] 对照架构测试确认 ports 不反向依赖 application/infrastructure。（[设计 §2](m3_transcript_history_implementation_design.md#2-当前代码与目标模块)）

### 阶段出口与记录

待填写：基线命令、版本、结果、已有改动保护范围及适用契约。基线失败先区分已有问题与本阶段修改，不将未知 CI 状态标记已完成。

## Milestone 1：Run / Agent session 模型和统一状态入口

### 工作

- [ ] 将旧整体 AnalysisSession/SessionCoordinator/Workflow 的职责迁为 run，迁移关联 ID 与调用方。（[设计 §2](m3_transcript_history_implementation_design.md#2-当前代码与目标模块)）
- [ ] 新增 AgentSessionRuntime 与 SessionSummary，明确 query_parser/null COMID 和 waterbody/有效 COMID。（[设计 §3.2](m3_transcript_history_implementation_design.md#32-当前状态)；[API §3.2](m3_transcript_history_api.md#32-runsummary-与-sessionsummary)）
- [ ] run 六态、session 五态、不可变终态及取消优先竞争沿用统一状态机，禁止第二套状态推演逻辑。（[设计 §3.2](m3_transcript_history_implementation_design.md#32-当前状态)）
- [ ] 实现准备执行时创建 session；parser 每 run 一次、水体每 run/COMID 一次，不为等待/排除节点创建上下文。（[设计 §3.2](m3_transcript_history_implementation_design.md#32-当前状态)；[设计 §4.2](m3_transcript_history_implementation_design.md#42-analysis_sessions)）
- [ ] 原请求/创建/BFS 等归 run，Agent 消息归具体 session；operation_id 对一次调用稳定。（[设计 §3.1](m3_transcript_history_implementation_design.md#31-身份与顺序)；[设计 §4.3](m3_transcript_history_implementation_design.md#43-transcript_items)）
- [ ] 统一分配原 item_id、run transcript_sequence 和 created_at；生成开始时可先分配草稿身份。（[设计 §3.1](m3_transcript_history_implementation_design.md#31-身份与顺序)；[设计 §3.3](m3_transcript_history_implementation_design.md#33-时间)）
- [ ] 建立短临界区内的事实/状态 mutation 入口，DB/网络等待留在锁外；running 无公开事件也有必需状态变更。（[设计 §3.2](m3_transcript_history_implementation_design.md#32-当前状态)；[设计 §6.2](m3_transcript_history_implementation_design.md#62-runwriter)）
- [ ] 明确 mutable created_at/updated_at 与 immutable created_at，API occurred_at 同源；真实业务时间保留。（[设计 §3.3](m3_transcript_history_implementation_design.md#33-时间)）

### Tests

- [ ] 两层状态转换、准备失败、取消、终态不可改及已完成子 session 保持完成。（[设计 §3.2](m3_transcript_history_implementation_design.md#32-当前状态)）
- [ ] 一个 run 多 session 的上下文隔离、所有上游 gate 后只创建/执行一次。（[设计 §3.2](m3_transcript_history_implementation_design.md#32-当前状态)）
- [ ] run-root/session 归属、UUID/COMID、UTC、并发序号与过滤后的缺口。（[设计 §3.1](m3_transcript_history_implementation_design.md#31-身份与顺序)；[设计 §4.2](m3_transcript_history_implementation_design.md#42-analysis_sessions)）
- [ ] 读取/重试/确认不刷新时间；生成完成不重新分配 ID/序号/时间。（[设计 §3.1](m3_transcript_history_implementation_design.md#31-身份与顺序)；[设计 §3.3](m3_transcript_history_implementation_design.md#33-时间)）
- [ ] 新 factory 与 fakes 的导入/依赖方向检查。（[设计 §2](m3_transcript_history_implementation_design.md#2-当前代码与目标模块)）

### Verify

- [ ] `python -m pytest backend/tests/domain backend/tests/application backend/tests/architecture -q`。（[设计 §11](m3_transcript_history_implementation_design.md#11-测试与验收设计)）

阶段出口：模型和统一状态入口可独立测试，M1 领域不变；本阶段不宣称具备数据库持久化。

## Milestone 2：PostgreSQL、四表 schema 与可复现环境

### 工作

- [ ] 新增 SQLAlchemy Core asyncio、psycopg binary、Alembic 依赖和锁文件；版本范围与实际解析版本记录一致。（[设计 §10.1](m3_transcript_history_implementation_design.md#101-技术选择)）
- [ ] 创建 analysis_runs、analysis_sessions、transcript_items、transcript_contents 及 Alembic 单链迁移。（[设计 §4](m3_transcript_history_implementation_design.md#4-postgresql-schema)；[设计 §10.2](m3_transcript_history_implementation_design.md#102-配置与命令)）
- [ ] 实现 PK/FK、同 run 联合归属、run 序号唯一、可空 UUID key 唯一、角色/COMID、终态引用和行正文互斥约束。（[设计 §4](m3_transcript_history_implementation_design.md#4-postgresql-schema)）
- [ ] created/terminal/request 引用使用延迟外键支持原子创建；不添加完整配置、结果、副本正文或独立 key 表。（[设计 §4.1](m3_transcript_history_implementation_design.md#41-analysis_runs)；[设计 §4.2](m3_transcript_history_implementation_design.md#42-analysis_sessions)）
- [ ] 写清跨表全文存在/互斥的 adapter 事务校验与行 CHECK 的区别；非法输入不能通过正常 store 写入口。（[设计 §4.4](m3_transcript_history_implementation_design.md#44-transcript_contents)）
- [ ] 增加实际历史列表/目录/完整过程所需索引，不预建全文、confidence 或关联查询索引。（[设计 §4.5](m3_transcript_history_implementation_design.md#45-索引引用及事务边界)）
- [ ] 新增 backend/compose.yaml、.env.example、忽略真实 .env、命名持久卷和 loopback 端口/healthcheck。（[API §8.2](m3_transcript_history_api.md#82-本地-compose)）
- [ ] AppSettings 按明确优先级加载 backend/.env；SecretStr 密码不 trim、不回显，分列 URL 构造覆盖特殊字符。（[设计 §10.2](m3_transcript_history_implementation_design.md#102-配置与命令)；[API §8.1](m3_transcript_history_api.md#81-环境变量)）
- [ ] 实现 wateragents-db upgrade/check；与 app 共用配置，迁移资源从已安装包/明确项目路径可定位。（[设计 §10.2](m3_transcript_history_implementation_design.md#102-配置与命令)）
- [ ] schema 版本只来自 Alembic 单 head；check 不 DDL，未知/缺失/旧/新/多 head 明确失败。（[设计 §10.2](m3_transcript_history_implementation_design.md#102-配置与命令)；[设计 §10.3](m3_transcript_history_implementation_design.md#103-启动恢复和关闭)）
- [ ] Windows 正式 CLI、测试和 smoke 复用 Selector loop 初始化；不在已经运行的 loop 中切换。（[设计 §10.1](m3_transcript_history_implementation_design.md#101-技术选择)）
- [ ] 更新 README 的本地 Compose、已有 PG、remote TLS、升级/停止/保留卷与单实例限制。（[API §8](m3_transcript_history_api.md#8-跨平台搭建与配置)）
- [ ] 将路径/包资源、UTF-8、进程启动与系统差异集中在基础设施边界；初始化和 smoke 不依赖 Bash/GNU/Unix 专用能力，不硬编码开发机路径或依赖当前 cwd。（[设计 §10.4](m3_transcript_history_implementation_design.md#104-跨操作系统可迁移性)）

### Tests

- [ ] 真实空测试库 upgrade→check→再次 upgrade；确认 revision 与四表约束。（[设计 §4](m3_transcript_history_implementation_design.md#4-postgresql-schema)；[API §8.3](m3_transcript_history_api.md#83-已有数据库与升级)）
- [ ] 重复 key、run 序号、parser/waterbody 身份、跨 run FK 和非法状态的失败。（[设计 §4](m3_transcript_history_implementation_design.md#4-postgresql-schema)）
- [ ] inline/external 批次错误、丢全文、重复全文、字节数/时间不一致被 adapter 拒绝并整批回滚。（[设计 §4.4](m3_transcript_history_implementation_design.md#44-transcript_contents)；[设计 §6.3](m3_transcript_history_implementation_design.md#63-原子提交超时与核实)）
- [ ] 配置 cwd 独立、环境覆盖、含 @:/#%$ 和空白的密码、sslrootcert、缺密码安全错误。（[API §8.1](m3_transcript_history_api.md#81-环境变量)）
- [ ] import/fake 注入不依赖真实数据库；正式启动缺配置明确失败。（[设计 §2](m3_transcript_history_implementation_design.md#2-当前代码与目标模块)；[API §8.1](m3_transcript_history_api.md#81-环境变量)）
- [ ] 三平台目标入口连接真实 PG；不要把仅执行配置解析当成连接验证。（[设计 §10.1](m3_transcript_history_implementation_design.md#101-技术选择)；[设计 §11](m3_transcript_history_implementation_design.md#11-测试与验收设计)）
- [ ] 相同锁文件在目标平台重新安装；含空格/中文路径、不同 cwd、LF/CRLF 配置和平台对应的原生依赖均可正常使用，不复制旧 venv。（[设计 §10.4](m3_transcript_history_implementation_design.md#104-跨操作系统可迁移性)；[API §8.5](m3_transcript_history_api.md#85-更换操作系统或机器的最短流程)）

### Verify

- [ ] `wateragents-db upgrade`、`wateragents-db check`（独立测试环境）。（[API §8.2](m3_transcript_history_api.md#82-本地-compose)；[API §8.3](m3_transcript_history_api.md#83-已有数据库与升级)）
- [ ] `python -m pytest backend/tests/persistence backend/tests/architecture -q`。（[设计 §11](m3_transcript_history_implementation_design.md#11-测试与验收设计)）
- [ ] 记录 PG minor/digest、driver/SQLAlchemy/Alembic 版本、migration revision。（[设计 §10.1](m3_transcript_history_implementation_design.md#101-技术选择)；[证据记录格式](#实际证据记录格式)）

阶段出口：可复现空库 schema、可靠连接及命令，尚不把启动修复或 transcript 写入标记完成。

## Milestone 3：Typed content、单份正文、引用与安全入口

### 工作

- [ ] 完成 immutable transcript、storage batch/read model、严格 registry 和统一 UTF-8 编码。（[设计 §2](m3_transcript_history_implementation_design.md#2-当前代码与目标模块)；[设计 §5.1](m3_transcript_history_implementation_design.md#51-注册器)）
- [ ] 生产注册 analysis_request v1、lifecycle_event v2、error v1、runtime_fact v1；runtime_fact 使用明确 fact 联合。（[设计 §5.1](m3_transcript_history_implementation_design.md#51-注册器)）
- [ ] 生命周期命名区分 run/session，配置只在 run_created，保留 v1 历史解析与原版本/序号/时间。（[设计 §5.1](m3_transcript_history_implementation_design.md#51-注册器)）
- [ ] 已附于原事件的配置或错误不再复制同义 item；tags/RAG 单源，confidence/provenance 按正文适用层级提取。（[设计 §5.1](m3_transcript_history_implementation_design.md#51-注册器)；[设计 §5.2](m3_transcript_history_implementation_design.md#52-消息与元信息)）
- [ ] 为所有已知类型实现唯一引用提取器，验证同 run/跨 session、目标存在或同批；保留 AnalysisDetail/AnalysisStep 原引用。（[设计 §4.5](m3_transcript_history_implementation_design.md#45-索引引用及事务边界)）
- [ ] 完成 64 KiB 默认阈值、首尾总 2 KiB Unicode 安全预览和 run/item 全文定位。（[设计 §5.3](m3_transcript_history_implementation_design.md#53-单份正文与大内容)；[API §4.4](m3_transcript_history_api.md#44-全文)）
- [ ] 新写严格验证；未知历史版本/已知损坏分别处理，不静默删记录或伪装正常业务结果。（[设计 §5.4](m3_transcript_history_implementation_design.md#54-未知版本与损坏)）
- [ ] 脱敏先于可读 live/SSE，再由持久化出口复核；日志保留普通 query。（[设计 §9.2](m3_transcript_history_implementation_design.md#92-凭据边界)）
- [ ] 完成流式凭据过滤器的私有缓冲、遮蔽状态及安全收尾，不把原始 secret 保留成另一个可读副本。（[设计 §9.2](m3_transcript_history_implementation_design.md#92-凭据边界)）
- [ ] 测试专用 fixture_message 注入通用消息适配器，不将假 LLM/水体结果注册进生产。（[设计 §5.1](m3_transcript_history_implementation_design.md#51-注册器)；[设计 §5.2](m3_transcript_history_implementation_design.md#52-消息与元信息)）

### Tests

- [ ] typed round-trip、未知/非法版本、错误归类、正常邻近 item 保留。（[设计 §5.1](m3_transcript_history_implementation_design.md#51-注册器)；[设计 §5.4](m3_transcript_history_implementation_design.md#54-未知版本与损坏)）
- [ ] UTF-8 65535/65536/65537 字节、预览 head/tail 各上限、自定义阈值、多字节字符。（[设计 §5.3](m3_transcript_history_implementation_design.md#53-单份正文与大内容)）
- [ ] 同文独立事实不同 ID，公共内容只存原 item，refs 跨 session 合法/跨 run 拒绝。（[设计 §3.1](m3_transcript_history_implementation_design.md#31-身份与顺序)；[设计 §4.5](m3_transcript_history_implementation_design.md#45-索引引用及事务边界)）
- [ ] 生成中引用依赖等待，已保存/同批目标合法，非法目标整批失败。（[设计 §4.5](m3_transcript_history_implementation_design.md#45-索引引用及事务边界)）
- [ ] 所有合成凭据切分点、普通 query、异常 repr/JSON 转义、URL/Authorization/Cookie、取消边界与最终正文。（[设计 §9.1](m3_transcript_history_implementation_design.md#91-原消息与晚到审计)；[设计 §9.2](m3_transcript_history_implementation_design.md#92-凭据边界)）
- [ ] 正常文本持续增量，不能等全文结束才实现表面“安全流式”。（[设计 §9.2](m3_transcript_history_implementation_design.md#92-凭据边界)）

### Verify

- [ ] `python -m pytest backend/tests/application backend/tests/observability backend/tests/persistence -q`。（[设计 §11](m3_transcript_history_implementation_design.md#11-测试与验收设计)）

阶段出口：一份安全原正文贯穿 live、存储和读取模型；registry 为后续生产方提供明确接入点。

## Milestone 4：创建原子事务、幂等和所有权收敛

### 工作

- [ ] HTTP body 只读一次，SHA-256 在业务解析/默认值/trim 前计算；不保存未脱敏 raw bytes。（[设计 §6.1](m3_transcript_history_implementation_design.md#61-创建与所有权)；[API §2](m3_transcript_history_api.md#2-创建与幂等)）
- [ ] 首次事务包含 run、原请求、run_created 配置、key/fingerprint/小型原回执及引用，确认后才允许执行。（[设计 §4.1](m3_transcript_history_implementation_design.md#41-analysis_runs)；[设计 §6.1](m3_transcript_history_implementation_design.md#61-创建与所有权)）
- [ ] 同 key 先读原指纹/回执；不因当前默认配置或请求 DTO 的后来变化改写已接受响应。（[设计 §6.1](m3_transcript_history_implementation_design.md#61-创建与所有权)）
- [ ] 用 DB UNIQUE 裁决竞争；内存 owner 只负责当前创建任务/注册窗口，不作为幂等权威。（[设计 §6.1](m3_transcript_history_implementation_design.md#61-创建与所有权)）
- [ ] 等待者共享默认 5 秒 deadline，确认回滚才接手，超时不取消 owner 或伪称回滚。（[设计 §6.1](m3_transcript_history_implementation_design.md#61-创建与所有权)）
- [ ] HTTP 断开后 owner 仍完成实际提交核实/注册；读取与 SSE 不把提交后注册前窗口误判 410。（[设计 §6.1](m3_transcript_history_implementation_design.md#61-创建与所有权)；[设计 §8.3](m3_transcript_history_implementation_design.md#83-续传握手与结束)）
- [ ] 未知提交保留原身份，通过原事务收尾和同 run 事务级 advisory lock 后的新查询核实结果；未确认不执行、不重建 run，幂等竞争仍受 DB UNIQUE 约束。（[设计 §6.3](m3_transcript_history_implementation_design.md#63-原子提交超时与核实)）
- [ ] task 启动失败保持原 201，追加一次 workflow_start_failed 终态；取消若先胜出则保持取消。（[设计 §6.1](m3_transcript_history_implementation_design.md#61-创建与所有权)）
- [ ] 原回执与 key 无自动过期；当前保存状态/HTTP request_id 不进入不可变 receipt。（[设计 §4.1](m3_transcript_history_implementation_design.md#41-analysis_runs)；[API §2](m3_transcript_history_api.md#2-创建与幂等)）

### Tests

- [ ] 同 key 同 body 串行/并发均同 run、201、同回执与一次执行。（[设计 §6.1](m3_transcript_history_implementation_design.md#61-创建与所有权)；[API §2](m3_transcript_history_api.md#2-创建与幂等)）
- [ ] JSON 空白、顺序、转义、值及字段有无改变返回 409；非法 key 422。（[API §2](m3_transcript_history_api.md#2-创建与幂等)）
- [ ] 不同 key 相同 body、不带 key 的独立提交各建新 run。（[API §2](m3_transcript_history_api.md#2-创建与幂等)）
- [ ] 终态、默认设置变化、重启和 live 清理后原 receipt 保持，HTTP request_id 每次不同。（[设计 §6.1](m3_transcript_history_implementation_design.md#61-创建与所有权)；[API §2](m3_transcript_history_api.md#2-创建与幂等)）
- [ ] 创建回滚无执行；提交响应丢失可核实；SELECT 暂时无行不能误认回滚。（[设计 §6.3](m3_transcript_history_implementation_design.md#63-原子提交超时与核实)）
- [ ] 等待超时、客户端断开、注册窗口和启动失败/取消竞争。（[设计 §6.1](m3_transcript_history_implementation_design.md#61-创建与所有权)）
- [ ] 缺失 key 的网络重发不被文档误称为可去重。（[API §2](m3_transcript_history_api.md#2-创建与幂等)）

### Verify

- [ ] `python -m pytest backend/tests/api backend/tests/application backend/tests/persistence -q`。（[设计 §11](m3_transcript_history_implementation_design.md#11-测试与验收设计)）

阶段出口：创建路由的持久化保证成立，测试证明数据库唯一性和一次执行，不能仅依赖内存 map。

## Milestone 5：串行写入、确认集合与有限补写

### 工作

- [ ] 每 run 只建一个 writer；待写引用原 buffer，准备/冻结批次在锁内，SQL 在锁外。（[设计 §6.2](m3_transcript_history_implementation_design.md#62-runwriter)）
- [ ] 100 items 目标、空闲立即写、状态单独触发、原子组不可拆、无空 SQL。（[设计 §6.2](m3_transcript_history_implementation_design.md#62-runwriter)）
- [ ] 草稿占据原序号但不提前写库；无依赖可写记录可先保存，按实际 item/mutation 维护确认。（[设计 §3.1](m3_transcript_history_implementation_design.md#31-身份与顺序)；[设计 §6.2](m3_transcript_history_implementation_design.md#62-runwriter)）
- [ ] 运行中失败仍保留 live/分析，重试冻结原批次，不覆写已保存原事实、不重排身份/时间。（[设计 §6.2](m3_transcript_history_implementation_design.md#62-runwriter)；[设计 §6.4](m3_transcript_history_implementation_design.md#64-重试和交付窗口)）
- [ ] 默认 5 秒覆盖 acquire/SQL/commit，旧调用退出与连接失效收尾后才退避。（[设计 §6.3](m3_transcript_history_implementation_design.md#63-原子提交超时与核实)）
- [ ] 实现提交后响应丢失的核实，检查同批身份/正文/适用状态；不能把未知结果当成功或回滚。（[设计 §6.3](m3_transcript_history_implementation_design.md#63-原子提交超时与核实)）
- [ ] 首次 + 最多 5 次重试、1/2/4/8/16 秒；adapter 无隐藏重试，新事实不重置预算。（[设计 §6.4](m3_transcript_history_implementation_design.md#64-重试和交付窗口)）
- [ ] 成功分析冻结此前必需事实集合；第三次重试失败后两秒到期交付警告终态，已过期不重新等待。（[设计 §6.4](m3_transcript_history_implementation_design.md#64-重试和交付窗口)）
- [ ] 终态及收尾另存原子组，保存失败不重选终态；唯一 run_completed 可表达 persistence error。（[设计 §6.4](m3_transcript_history_implementation_design.md#64-重试和交付窗口)）
- [ ] 派生 PersistenceInfo，草稿 pending、实际 warning、耗尽优先；恢复后清当前 warning、保留原事件 warning。（[设计 §6.5](m3_transcript_history_implementation_design.md#65-保存状态手动恢复与清理)；[API §3.1](m3_transcript_history_api.md#31-来源与保存情况)）
- [ ] 实现手动恢复服务：exhausted 新一轮，pending 复用，up_to_date 无操作；HTTP 在 Milestone 6 接入。（[设计 §6.5](m3_transcript_history_implementation_design.md#65-保存状态手动恢复与清理)；[API §6.2](m3_transcript_history_api.md#62-persistenceretry)）

### Tests

- [ ] 两 run 并行、同 run 串行；状态/事件/全文/refs 同批失败全部回滚。（[设计 §4.5](m3_transcript_history_implementation_design.md#45-索引引用及事务边界)；[设计 §6.3](m3_transcript_history_implementation_design.md#63-原子提交超时与核实)）
- [ ] 较大序号已保存而小草稿未定稿时仍 pending；所有 group/mutation 确认后才 up_to_date。（[设计 §6.2](m3_transcript_history_implementation_design.md#62-runwriter)；[设计 §6.5](m3_transcript_history_implementation_design.md#65-保存状态手动恢复与清理)）
- [ ] 冻结失败批次优先、新记录不抢跑、不额外 writer、无空事务。（[设计 §6.2](m3_transcript_history_implementation_design.md#62-runwriter)）
- [ ] 每次超时共享预算、取消收尾、unknown commit 核实及不并行重写。（[设计 §6.3](m3_transcript_history_implementation_design.md#63-原子提交超时与核实)）
- [ ] 各次退避、第三次失败后两秒、窗口已过、窗口中恢复/取消、后续终态写入失败。（[设计 §6.4](m3_transcript_history_implementation_design.md#64-重试和交付窗口)）
- [ ] 耗尽后 DB 恢复/GET/新内容不唤醒；手动重复/并发不重置执行中预算。（[设计 §6.5](m3_transcript_history_implementation_design.md#65-保存状态手动恢复与清理)）
- [ ] 保存恢复事实只追加一次，不因保存自己递归追加。（[设计 §5.1](m3_transcript_history_implementation_design.md#51-注册器)）

### Verify

- [ ] `python -m pytest backend/tests/application backend/tests/persistence -q`。（[设计 §11](m3_transcript_history_implementation_design.md#11-测试与验收设计)）

阶段出口：持久化与业务状态分别正确，故障矩阵证明真实确认和原子性，而非仅测试入队成功。

## Milestone 6：统一读取、历史分页与 HTTP 契约

### 工作

- [ ] 共享 DTO/registry/完整列表/全文装载，固定 auto/history 来源，不维护第二套模型。（[设计 §7.1](m3_transcript_history_implementation_design.md#71-一次选择来源)；[API §3](m3_transcript_history_api.md#3-当前与历史的共同返回模型)）
- [ ] live 短临界区取快照；PG 多语句读取使用一致的只读事务，不混入 live 配置/状态/引用。（[设计 §7.1](m3_transcript_history_implementation_design.md#71-一次选择来源)）
- [ ] run 详情返回真实 session 目录及全部 run-owned 记录；session 详情与 transcript 严格校验 run 归属。（[设计 §7.2](m3_transcript_history_implementation_design.md#72-run-详情与-session-完整过程)；[API §4.2](m3_transcript_history_api.md#42-run-详情)）
- [ ] session transcript 全量有序，沿全部原记录的显式引用装载公共闭包并去重；不只挑最终总结。（[设计 §7.2](m3_transcript_history_implementation_design.md#72-run-详情与-session-完整过程)；[API §4.3](m3_transcript_history_api.md#43-agent-详情与完整-transcript)）
- [ ] 实现统一全文 API，内联/外置可读，未知版本和损坏明确表示；安全出口和 history-only 类型规则统一。（[设计 §5.4](m3_transcript_history_implementation_design.md#54-未知版本与损坏)；[API §4.4](m3_transcript_history_api.md#44-全文)）
- [ ] run 历史列表默认 50、最大 200、created_at/run_id 倒序，base64url 版本游标；无 total/filters/Agent 分页。（[设计 §7.4](m3_transcript_history_implementation_design.md#74-run-历史分页)；[API §4.1](m3_transcript_history_api.md#41-run-历史列表)）
- [ ] 四个读取入口的 view、source、persistence、issues、URL 传递与取消可见性一致。（[设计 §7.1](m3_transcript_history_implementation_design.md#71-一次选择来源)；[设计 §7.3](m3_transcript_history_implementation_design.md#73-可见性)）
- [ ] 补齐 create/cancel/retry 和错误 HTTP 映射，保留 request_id/error_id，参数无效 422。（[API §1](m3_transcript_history_api.md#1-基础约定与路由)；[API §7](m3_transcript_history_api.md#7-错误与排查字段)）
- [ ] retry 的 200/202/409/404/503 正确；创建/手动补写/业务取消与连接断开不同义。（[API §6.2](m3_transcript_history_api.md#62-persistenceretry)）
- [ ] 更新 OpenAPI、API 示例与前端约定；旧路径已删除，M2 文档仍作为旧阶段证据。（[设计 §2](m3_transcript_history_implementation_design.md#2-当前代码与目标模块)；[API §9](m3_transcript_history_api.md#9-排障与验证)）

### Tests

- [ ] live 有而 PG 故障可读；strict history 不出现内存独有 item，auto fallback 保持取消限制。（[设计 §7.1](m3_transcript_history_implementation_design.md#71-一次选择来源)；[设计 §7.3](m3_transcript_history_implementation_design.md#73-可见性)）
- [ ] 公共全文只读原 item、view 传递、跨 run/session/item 404，不用全局 item ID 绕过归属。（[API §4.4](m3_transcript_history_api.md#44-全文)）
- [ ] 大型完整过程不截断、引用闭包有序去重且不扩张到其他 Agent 全部上下文。（[设计 §7.2](m3_transcript_history_implementation_design.md#72-run-详情与-session-完整过程)）
- [ ] 配置损坏保留列表 item/issues，完整 DB 故障 503，不假空/假末页/假 404。（[设计 §5.4](m3_transcript_history_implementation_design.md#54-未知版本与损坏)；[API §7](m3_transcript_history_api.md#7-错误与排查字段)）
- [ ] 同 created_at UUID tie-break、状态变化、页间新插入、最后一页、空库、游标精度及重启。（[设计 §7.4](m3_transcript_history_implementation_design.md#74-run-历史分页)）
- [ ] 非法 view/limit/cursor/未知 query 参数，以及无 Agent 分页参数契约。（[API §1](m3_transcript_history_api.md#1-基础约定与路由)；[API §4.1](m3_transcript_history_api.md#41-run-历史列表)）
- [ ] 取消和手动保存各分支，包括已释放历史的处理。（[API §6](m3_transcript_history_api.md#6-取消与手动保存)）

### Verify

- [ ] `python -m pytest backend/tests/api backend/tests/application backend/tests/persistence -q`。（[设计 §11](m3_transcript_history_implementation_design.md#11-测试与验收设计)）
- [ ] 导出实际 OpenAPI 对照十路由及错误/响应模型，确认旧路由不存在。（[API §1](m3_transcript_history_api.md#1-基础约定与路由)；[API §9](m3_transcript_history_api.md#9-排障与验证)）

阶段出口：历史可诚实读取全部已保存事实，当前与历史只有来源/可见性差异，没有重复实现。

## Milestone 7：两层 SSE、消息生命周期与续传

### 工作

- [ ] run_snapshot 从原状态装配完整轻量通知，不持久化通知或复制完整消息。（[设计 §8.1](m3_transcript_history_implementation_design.md#81-run-通知)；[API §5.2](m3_transcript_history_api.md#52-run_snapshot)）
- [ ] actual session 目录与水体参与状态按原事实生成；M3 无拓扑时不伪造，测试覆盖有拓扑情形。（[设计 §8.1](m3_transcript_history_implementation_design.md#81-run-通知)）
- [ ] session process 先记录稳定整数 ID，再唤醒订阅者；无订阅也保留续传日志。（[设计 §8.2](m3_transcript_history_implementation_design.md#82-session-过程)）
- [ ] 完成 transcript_item、message_started、message_delta、message_finalized 和 stream_end 编码。（[API §5.3](m3_transcript_history_api.md#53-session-过程与三个消息事件)；[API §5.4](m3_transcript_history_api.md#54-游标与结束)）
- [ ] 消息开始固定原身份/顺序/时间，delta 只传新增安全文本，finalized 只更新原消息、从正文派生结束状态。（[设计 §3.1](m3_transcript_history_implementation_design.md#31-身份与顺序)；[设计 §8.2](m3_transcript_history_implementation_design.md#82-session-过程)）
- [ ] 消息定稿不等 DB，不再生成同义 transcript item；大正文统一 preview/ref。（[设计 §8.2](m3_transcript_history_implementation_design.md#82-session-过程)；[设计 §5.3](m3_transcript_history_implementation_design.md#53-单份正文与大内容)）
- [ ] 初订阅先全部已有后新增，重连补大于 cursor 的事件，锁内交接无缺口。（[设计 §8.3](m3_transcript_history_implementation_design.md#83-续传握手与结束)）
- [ ] session Last-Event-ID ASCII 整数/0/缺省、400/409；run 通知不编号也不复用过程 cursor。（[API §5.4](m3_transcript_history_api.md#54-游标与结束)）
- [ ] live 合法握手统一 200，已结束先补齐再 end；移除旧 204 行为与相关 smoke 断言。（[设计 §8.3](m3_transcript_history_implementation_design.md#83-续传握手与结束)；[API §5.4](m3_transcript_history_api.md#54-游标与结束)）
- [ ] session 与 run 各自满足关闭条件后 stream_end；异常断开不发成功结束，keepalive 无编号。（[设计 §8.3](m3_transcript_history_implementation_design.md#83-续传握手与结束)；[API §5.1](m3_transcript_history_api.md#51-公共传输)）
- [ ] live 释放的 410/history_url、完全不存在 404、DB 失败 503 和创建注册窗口收敛正确。（[设计 §6.1](m3_transcript_history_implementation_design.md#61-创建与所有权)；[API §5.4](m3_transcript_history_api.md#54-游标与结束)）

### Tests

- [ ] run 首次/重连只最新完整快照，状态变化不产生第二套 DB 通知。（[设计 §8.1](m3_transcript_history_implementation_design.md#81-run-通知)）
- [ ] 无订阅已有内容、首次发送期间新增、多订阅者、重连及重复帧消费。（[设计 §8.3](m3_transcript_history_implementation_design.md#83-续传握手与结束)）
- [ ] 三阶段顺序、同消息原 ID、跨消息交错、最终累计文本不重复拼接、大小正文表示。（[设计 §8.2](m3_transcript_history_implementation_design.md#82-session-过程)）
- [ ] 较早生成时间保留，SSE sequence、lifecycle sequence、transcript_sequence 不混淆。（[设计 §3.1](m3_transcript_history_implementation_design.md#31-身份与顺序)；[设计 §8.2](m3_transcript_history_implementation_design.md#82-session-过程)）
- [ ] session 结束不等待 run 保存；run 保存中/耗尽继续通知，最终全保存先快照再 end。（[设计 §8.3](m3_transcript_history_implementation_design.md#83-续传握手与结束)）
- [ ] 200 空补齐后 end、400/409、410、意外 EOF、客户端关闭不取消执行。（[API §5.1](m3_transcript_history_api.md#51-公共传输)；[API §5.4](m3_transcript_history_api.md#54-游标与结束)）
- [ ] 逐切分凭据推送安全与正常文本持续输出。（[设计 §9.2](m3_transcript_history_implementation_design.md#92-凭据边界)）

### Verify

- [ ] `python -m pytest backend/tests/api backend/tests/application backend/tests/observability -q`。（[设计 §11](m3_transcript_history_implementation_design.md#11-测试与验收设计)）
- [ ] 可执行 fixture 对照 API 中每个事件名、wrapper、id、结束与错误示例。（[API §5](m3_transcript_history_api.md#5-两层-sse)；[API §7](m3_transcript_history_api.md#7-错误与排查字段)）

阶段出口：原过程一份、live 事件可续传，数据库不按 token/frame 增长，实时查看不改变执行。

## Milestone 8：取消、遗留修复、关闭与 live 清理

### 工作

- [ ] run cancel 在同一边界关闭调度、固定生成中原消息、通知全部未终态 session；不做 HTTP 自调用。（[设计 §9.1](m3_transcript_history_implementation_design.md#91-原消息与晚到审计)；[API §6.1](m3_transcript_history_api.md#61-cancel)）
- [ ] 晚到新增文本聚合为引用原消息的独立 history-only 审计，不复制前缀或另存拼接全文。（[设计 §9.1](m3_transcript_history_implementation_design.md#91-原消息与晚到审计)）
- [ ] 取消跨片段凭据状态延续，原消息安全定稿在 session stream_end 前完成。（[设计 §9.1](m3_transcript_history_implementation_design.md#91-原消息与晚到审计)；[设计 §9.2](m3_transcript_history_implementation_design.md#92-凭据边界)）
- [ ] late audit/清理事实计入必需写入及执行收敛，当前 GET/SSE/全文/引用/下游均不能泄露。（[设计 §7.3](m3_transcript_history_implementation_design.md#73-可见性)；[设计 §6.5](m3_transcript_history_implementation_design.md#65-保存状态手动恢复与清理)）
- [ ] lifespan 连接→schema→遗留修复→接收流量；不自动 DDL，失败安全退出。（[设计 §10.3](m3_transcript_history_implementation_design.md#103-启动恢复和关闭)）
- [ ] 按 run 逐事务修复 PG 非终态 run/session，包含终态 run 下的未终态 sessions。（[设计 §10.3](m3_transcript_history_implementation_design.md#103-启动恢复和关闭)）
- [ ] 修复保持原身份/旧正文/原终态，新状态与 service_interrupted 记录同事务；重复启动不重复修复。（[设计 §10.3](m3_transcript_history_implementation_design.md#103-启动恢复和关闭)；[设计 §6.3](m3_transcript_history_implementation_design.md#63-原子提交超时与核实)）
- [ ] stop 与用户 cancel 分开，关闭有界保存/协作收敛、writer 和 engine 资源退出；无自动 resume。（[设计 §10.3](m3_transcript_history_implementation_design.md#103-启动恢复和关闭)）
- [ ] terminal + quiesced + 全部保存后起算完整 grace，新增待写失效计时，清理前锁内复查。（[设计 §6.5](m3_transcript_history_implementation_design.md#65-保存状态手动恢复与清理)）
- [ ] cleanup 幂等/可重试，失败不改变终态；不增加慢 SSE 排空/ACK 机制。（[设计 §6.5](m3_transcript_history_implementation_design.md#65-保存状态手动恢复与清理)；[设计 §8.3](m3_transcript_history_implementation_design.md#83-续传握手与结束)）

### Tests

- [ ] cancel 与 chunk/finalize/terminal 交错：前缀固定、尾部仅审计、已完成消息不降级。（[设计 §9.1](m3_transcript_history_implementation_design.md#91-原消息与晚到审计)）
- [ ] audit 的同批引用/回滚/补写、auto PG fallback/direct fulltext/ref 不绕过。（[设计 §9.1](m3_transcript_history_implementation_design.md#91-原消息与晚到审计)；[设计 §7.3](m3_transcript_history_implementation_design.md#73-可见性)）
- [ ] pending/running 的 run 与 session 各种组合，包括终态父/非终态子、重复启动和中途修复失败。（[设计 §10.3](m3_transcript_history_implementation_design.md#103-启动恢复和关闭)）
- [ ] 原创建 receipt、旧序号时间、终态保持，新修复时间表达真实修复时刻。（[设计 §10.3](m3_transcript_history_implementation_design.md#103-启动恢复和关闭)；[设计 §3.3](m3_transcript_history_implementation_design.md#33-时间)）
- [ ] 未收敛/未全保存/grace 未到均不清理；补写后完整窗口、cleanup 失败重试。（[设计 §6.5](m3_transcript_history_implementation_design.md#65-保存状态手动恢复与清理)）
- [ ] 活动 SSE、在途 SQL 和未完成模型任务下的实际 shutdown 资源关闭。（[设计 §10.3](m3_transcript_history_implementation_design.md#103-启动恢复和关闭)）

### Verify

- [ ] `python -m pytest backend/tests/application backend/tests/persistence backend/tests/api -q`。（[设计 §11](m3_transcript_history_implementation_design.md#11-测试与验收设计)）
- [ ] 真实进程停止/重启后 history 仍可读，SSE 正确 410，遗留不恢复执行。（[设计 §10.3](m3_transcript_history_implementation_design.md#103-启动恢复和关闭)；[API §5.4](m3_transcript_history_api.md#54-游标与结束)）

阶段出口：中断不会伪造完成、保存或恢复；正常结束后可释放资源而保持全文历史。

## Milestone 9：完整集成、三平台 smoke 与运行手册

### 工作

- [ ] 更新 health smoke 以满足正式 PG 启动门槛；新增/替换 run_history_smoke，覆盖两层流与历史。（[设计 §11](m3_transcript_history_implementation_design.md#11-测试与验收设计)；[API §9](m3_transcript_history_api.md#9-排障与验证)）
- [ ] smoke 使用真实 Uvicorn 与 PG，动态 loopback 端口、禁用环境代理、确定性 gate，不用真实 LLM。（[设计 §11](m3_transcript_history_implementation_design.md#11-测试与验收设计)；[API §9](m3_transcript_history_api.md#9-排障与验证)）
- [ ] 按 API 指南通过 WATERAGENTS_TEST_DB_NAME 指定不同于业务库的测试数据库；fixture/smoke 的临时 schema、Alembic version 表和子进程一致，缺连接明确失败且清理不碰业务库。（[API §9](m3_transcript_history_api.md#9-排障与验证)）
- [ ] 完成 CI 三平台单元/协议与实际 PG smoke 方案；平台不支持 service containers 时使用可用 PG 供应方式。（[设计 §11](m3_transcript_history_implementation_design.md#11-测试与验收设计)）
- [ ] 运行从空环境配置到 upgrade/check/start，再 stop/restart/history 的完整文档步骤。（[API §8.2](m3_transcript_history_api.md#82-本地-compose)；[API §8.4](m3_transcript_history_api.md#84-停止重启与检查)）
- [ ] 验证远程实例配置、SSL、特殊字符密码和版本不匹配错误，不把仅本机 Compose 成功当作跨环境完成。（[API §8.1](m3_transcript_history_api.md#81-环境变量)；[API §8.3](m3_transcript_history_api.md#83-已有数据库与升级)）
- [ ] 按 API §8.5 完成跨系统切换：三个 OS 分别作为目标，复用同版源码/锁及一份已保存测试数据，停止旧实例后再启动目标；记录实际 OS/CPU/Python 组合。（[设计 §10.4](m3_transcript_history_implementation_design.md#104-跨操作系统可迁移性)；[API §8.5](m3_transcript_history_api.md#85-更换操作系统或机器的最短流程)）
- [ ] 验证接回原 PG 无需改表/搬历史；新空 PG 只用现有 Alembic 初始化同一 schema，不另建按 OS 分叉的迁移或数据库复制工具。（[API §8.5](m3_transcript_history_api.md#85-更换操作系统或机器的最短流程)）
- [ ] 完成 OpenAPI、事件文档、README、配置示例、测试命令与真实文件路径一致性检查。（[API §9](m3_transcript_history_api.md#9-排障与验证)；[前端约定 §7](../frontend_integration_contracts.md#7-同步规则)）
- [ ] 普通 query 可用于诊断，日志不暴露 secret；保存 warning 可通过 run/session/request/error ID 追溯。（[设计 §9.2](m3_transcript_history_implementation_design.md#92-凭据边界)；[API §7](m3_transcript_history_api.md#7-错误与排查字段)）

### Verify

- [ ] `python -m pytest backend/tests -q`。（[设计 §11](m3_transcript_history_implementation_design.md#11-测试与验收设计)；[API §9](m3_transcript_history_api.md#9-排障与验证)）
- [ ] `python -m pytest backend/tests/persistence -q`（真实 PG）。（[设计 §11](m3_transcript_history_implementation_design.md#11-测试与验收设计)；[API §9](m3_transcript_history_api.md#9-排障与验证)）
- [ ] `python backend/scripts/health_smoke.py`。（[API §9](m3_transcript_history_api.md#9-排障与验证)）
- [ ] `python backend/scripts/run_history_smoke.py`。（[API §9](m3_transcript_history_api.md#9-排障与验证)）
- [ ] Ubuntu/macOS/Windows + Python 3.11 的实际结果与退出资源证据。（[设计 §11](m3_transcript_history_implementation_design.md#11-测试与验收设计)）
- [ ] 迁移前后对照原 run/session/item ID、UTC 时间、顺序、历史 cursor、同 key 原回执及大正文全文；确认没有重跑分析或依赖旧机器本地路径。（[设计 §10.4](m3_transcript_history_implementation_design.md#104-跨操作系统可迁移性)；[API §8.5](m3_transcript_history_api.md#85-更换操作系统或机器的最短流程)）
- [ ] `git diff --check`、相对文档链接/锚点、配置引用和协议示例检查。（[前端约定 §7](../frontend_integration_contracts.md#7-同步规则)；[证据记录格式](#实际证据记录格式)）

阶段出口：记录真实命令、平台/版本、通过/失败数量及 CI 入口。任何平台缺失保持 partial，不借用 M2 测试数量。

## Milestone 10：契约验收与后续交接

### 工作

- [ ] 为下表每条 contract 填写实现文件、测试位置、实际命令/结果和剩余风险；不只填写“已支持”。（[契约覆盖矩阵](#契约覆盖矩阵)；[证据记录格式](#实际证据记录格式)）
- [ ] 核对 GLOBAL 的模型/时间/事件/日志/跨平台规则，以及当前适用 MR-G01–MR-G14 的交付要求。（[契约 §2](../deferred_milestone_contracts.md#2-跨-milestone-硬约束)；[契约 §4](../deferred_milestone_contracts.md#4-后续-mr-合规门禁)）
- [ ] 更新中心承接登记：全部必要实现和证据满足后才标已承接；未完成项给出明确后续位置。（[契约 §13](../deferred_milestone_contracts.md#13-承接记录)）
- [ ] M4 接入真实 parser session、AnalysisContext/BFS 和原类型注册，不改四表事实原则。（[设计 §1.2](m3_transcript_history_implementation_design.md#12-后续阶段承接)；[契约 §6](../deferred_milestone_contracts.md#6-m4query-understanding--hydrology-topology)）
- [ ] M6 接入真实消息/工具/skill/hook/结果、适用置信度和综合水质指标；M3 fixture 不成为业务 schema。（[设计 §1.2](m3_transcript_history_implementation_design.md#12-后续阶段承接)；[契约 §8](../deferred_milestone_contracts.md#8-m6single-agent-reasoning)）
- [ ] M7 接入 DAG、上游失败策略与水体参与状态；引用索引仅按真实需要评估。（[设计 §1.2](m3_transcript_history_implementation_design.md#12-后续阶段承接)；[契约 §9](../deferred_milestone_contracts.md#9-m7topology-driven-multi-agent-workflow)）
- [ ] M8 接入显式跨 run 来源与 RAG，保持 stateless 和当前上下文边界。（[设计 §1.2](m3_transcript_history_implementation_design.md#12-后续阶段承接)；[契约 §10](../deferred_milestone_contracts.md#10-m8memoryrag-from-transcript)）
- [ ] M9 按父目录契约实现地图/连线、多层完整阅读、两层 SSE、保存按钮与历史管理；取消颜色仍待前端设计。（[前端约定 §5](../frontend_integration_contracts.md#5-m4m9-的前端接力边界)；[契约 §11](../deferred_milestone_contracts.md#11-m9settingskeysadmin--demo-closure)）
- [ ] 在本清单追加最终完成记录，说明改动、验证、限制和可复现入口。（[设计 §12](m3_transcript_history_implementation_design.md#12-完成标准与接力)；[证据记录格式](#实际证据记录格式)）

### 契约覆盖矩阵

| 契约 | 对应阶段 | 必须登记的证据 | 设计定位 |
|---|---|---|---|
| GLOBAL-C11 | 2、9–10 | 同源代码/锁/配置/schema、三系统目标迁移、路径/编码/进程可移植性与同库历史不变。 | [设计 §10.4](m3_transcript_history_implementation_design.md#104-跨操作系统可迁移性)；[API §8.5](m3_transcript_history_api.md#85-更换操作系统或机器的最短流程) |
| M3-C01 / M3-A01 | 1–5、9 | 四表、append-only、原身份、两层归属、草稿缺口。 | [设计 §3.1](m3_transcript_history_implementation_design.md#31-身份与顺序)；[设计 §4](m3_transcript_history_implementation_design.md#4-postgresql-schema) |
| M3-C02 / M3-A02 | 1、3、6 | 严格版本/类型、UTC、未知/损坏、元信息提取。 | [设计 §5.1](m3_transcript_history_implementation_design.md#51-注册器)；[设计 §5.4](m3_transcript_history_implementation_design.md#54-未知版本与损坏) |
| M3-C03 / M3-C07 | 1–4、6 | 原配置快照、列表同源投影、Agent 自身状态/时间。 | [设计 §3.2](m3_transcript_history_implementation_design.md#32-当前状态)；[设计 §4.1](m3_transcript_history_implementation_design.md#41-analysis_runs) |
| M3-C04 | 6–7、9 | 十路由、run 分页、完整 session 及 run-owned 原记录。 | [设计 §7](m3_transcript_history_implementation_design.md#7-统一读取与历史查询)；[API §4](m3_transcript_history_api.md#4-get-读取接口) |
| M3-C05 / M3-C06 / M3-A03 | 2–3、6–8 | 全文单份、字节阈值、凭据跨片段/全文/预览。 | [设计 §5.3](m3_transcript_history_implementation_design.md#53-单份正文与大内容)；[设计 §9.2](m3_transcript_history_implementation_design.md#92-凭据边界) |
| M3-C08 | 1、4–5、8 | 生命周期/错误原事实、启动失败和适用状态原子保存。 | [设计 §5.1](m3_transcript_history_implementation_design.md#51-注册器)；[设计 §6.1](m3_transcript_history_implementation_design.md#61-创建与所有权) |
| M3-C09 / M3-A04 | 2、4、9 | DB 唯一竞争、原 bytes 指纹/回执、未知提交与一次执行。 | [设计 §6.1](m3_transcript_history_implementation_design.md#61-创建与所有权)；[API §2](m3_transcript_history_api.md#2-创建与幂等) |
| M3-C10 / M3-A05 | 3、7–8 | 取消固定前缀、history-only 尾部、可见性不可绕过。 | [设计 §9.1](m3_transcript_history_implementation_design.md#91-原消息与晚到审计)；[设计 §7.3](m3_transcript_history_implementation_design.md#73-可见性) |
| M3-C11 / M3-A07 | 5–6、8 | 交付窗口、唯一终态、当前可读/保存警告。 | [设计 §6.4](m3_transcript_history_implementation_design.md#64-重试和交付窗口)；[API §3.1](m3_transcript_history_api.md#31-来源与保存情况) |
| M3-C12 / M3-C13 / M3-A06 | 5、8–9 | 全部确认+收敛+grace、幂等清理/实际资源退出。 | [设计 §6.5](m3_transcript_history_implementation_design.md#65-保存状态手动恢复与清理) |
| M3-C14 | 2、8–9 | 严格启动、父子遗留修复、原终态保持、不自动执行。 | [设计 §10.3](m3_transcript_history_implementation_design.md#103-启动恢复和关闭) |
| M3-C15 / M3-A08 | 7–9 | 两层流、三消息事件、cursor、stream_end、200/410。 | [设计 §8](m3_transcript_history_implementation_design.md#8-两层-sse-与消息装配)；[API §5](m3_transcript_history_api.md#5-两层-sse) |
| M3-C16 / M3-A09 | 1、3、6–7 | 多 COMID/operation、同 run 跨 session 引用、完整公共上下文。 | [设计 §4.5](m3_transcript_history_implementation_design.md#45-索引引用及事务边界)；[设计 §7.2](m3_transcript_history_implementation_design.md#72-run-详情与-session-完整过程) |
| M3-C17 | 6–8 | 同 reader/DTO、auto/history、来源和审计限制。 | [设计 §7.1](m3_transcript_history_implementation_design.md#71-一次选择来源)；[设计 §7.3](m3_transcript_history_implementation_design.md#73-可见性) |
| M3-C18 / M3-A10 | 2–5、8 | 原子批次/回滚/未知 commit 核实、确认集合与单份正文。 | [设计 §6.2](m3_transcript_history_implementation_design.md#62-runwriter)；[设计 §6.3](m3_transcript_history_implementation_design.md#63-原子提交超时与核实) |
| M3-C19 / M3-A11 | 3、9 | 普通 query 保留，实际日志/异常/driver 不泄漏凭据。 | [设计 §9.2](m3_transcript_history_implementation_design.md#92-凭据边界) |
| M3-C20 / M3-A12 | 5–6、9 | 耗尽停止、手动新一轮、并发复用、HTTP 与实际保存不同义。 | [设计 §6.5](m3_transcript_history_implementation_design.md#65-保存状态手动恢复与清理)；[API §6.2](m3_transcript_history_api.md#62-persistenceretry) |

### 实际证据记录格式

每个阶段完成后补充以下信息，不提前填通过值：

| 字段 | 待记录内容 |
|---|---|
| 日期与代码状态 | 分支/提交或可复核 diff，已有改动保护情况。 |
| 实现位置 | 生产代码、migration、配置、脚本和相应测试。 |
| 环境 | OS、Python、PG minor/digest、依赖版本、schema revision。 |
| 验证 | 实际命令、结果、测试数量及 CI 链接（确实执行后填写）。 |
| 契约结论 | 已满足、partial 或未满足，缺少的具体证据。 |
| 后续接力 | 所属 Milestone/contract、入口、未实现业务边界。 |

当前没有 M3 实现或运行验收完成记录；本清单全部实施复选项保持未勾选。
