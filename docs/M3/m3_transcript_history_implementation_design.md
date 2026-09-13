# M3 Transcript Store + History APIs 实施详细设计

更新日期：2026-09-14

状态：设计已定稿，Milestone 0–7 已完成本机实现与适用验证，Milestone 8–10 待实施。本文把已经确认的 D01–D86 收敛为可执行的工程方案；逐阶段实际状态与验收见 [Implementation TODO](m3_implementation_todo.md)，对外字段、请求示例和环境命令见 [API 与搭建排障指南](m3_transcript_history_api.md)。

本文是 M3 当前工程设计入口。[设计决策记录](m3_transcript_history_design.md) 保留原编号、取舍和确认依据；其中旧整体 session 术语及“继续定型”应按本文解释，不另形成一套实现方案。用户行为和持久化保证仍受 [中心契约 M3-C01–C20 / M3-A01–A12](../deferred_milestone_contracts.md#5-m3transcript-store--history-apis) 约束。常规工程细节由实现者直接落实；本次不把设计完成标记为功能完成。

## 1. 核心结论与交付边界

1. run 表示一次用户提交的完整分析；session 表示问题解析或某个水体 Agent 的独立上下文及一次执行 loop。run 是历史分页、创建幂等、整体取消、保存进度与清理的单位。
2. PostgreSQL 保存已经提交的事实。运行中先把安全内容放入 live state，再由每个 run 唯一的串行写入器增量保存；创建是例外，必须先确认创建事务提交才能执行。
3. 四张业务表保存当前状态、原过程与单份大正文。配置、请求、结果和公共上下文只维护原事实；API 负责装配，不为每种页面复制存储模型。
4. 当前与历史共用 DTO、类型注册、引用解析和全文装载。auto 有 live 就读 live，否则读 PostgreSQL；history 始终只读 PostgreSQL。
5. run SSE 推送完整轻量状态快照，session SSE 按需推送 Agent 过程及消息片段。SSE 不驱动执行，片段不逐帧入库，历史读取定稿正文。
6. 保留有限补写、未知提交核实、唯一终态、取消可见性、全部保存后清理，以及其他环境可复现搭建的要求。
7. macOS、Windows、Linux 共用业务代码、配置格式、依赖锁与 Alembic 迁移链；换操作系统仅重新部署运行环境，不要求改业务代码、转换业务 schema 或改写已保存记录。跨系统迁移按 §10.4 交付并验收。

### 1.1 本阶段实现

- run/session 职责拆分、四表 schema、Alembic、数据库配置、启动检查与遗留修复。
- 四类首批 typed content、引用验证、凭据脱敏、大正文保存与统一读取。
- 创建幂等、串行原子提交、自动与手动补写、保存状态、取消及 live 清理。
- 十条目标路由、两层 SSE、完整过程与全文读取、历史 run 游标分页。
- 通用流式消息装配能力及可注入测试生产方；真实 PostgreSQL 集成和跨平台 smoke。

### 1.2 后续阶段承接

M4 产生真实结构化问题与 BFS；M6 产生真实模型/工具/技能/钩子记录、水体结果和跨参数综合水质指标；M7 产生 DAG 调度与失败传播策略；M8 产生跨 run 历史证据；M9 实现地图、连线、折叠、打字效果、保存入口和历史管理。M3 不定义综合指标公式，不实现真实 Agent，不增加地图截图、颜色表、事件回放引擎、多 worker 调度、SQLite 转换或 transcript 关联索引表。

完整用户流程已经写入 [总体设计 §1.2](../backend_architecture_skeleton.md#12-从用户提问到历史记录阅读的完整流程)。后端保留原消息和实际拓扑事实，前端依据这些事实组织地图及多层阅读。

## 2. 当前代码与目标模块

当前代码已迁移到 `application/analysis_runs/`，具备 run/Agent session 身份、typed transcript、PostgreSQL 四表存储、幂等创建、串行补写、统一 history 读取及两层 SSE。HTTP 已提供创建、历史列表、run/session/transcript/全文读取、取消、手动补写、完整轻量 run 快照流和可续传 session 过程流；取消定稿/晚到审计、启动遗留修复与 live 清理留给 Milestone 8。M2 文档及其测试数量只作为旧阶段证据。

建议目录如下；每个模块包含相关小类型，不为每个 DTO 单独建文件。

```text
backend/
  compose.yaml
  .env.example
  alembic.ini
  migrations/env.py
  migrations/versions/
  app/
    api/http/analysis_runs.py
    api/schemas/analysis_runs.py
    api/sse/encoder.py
    api/sse/response.py
    application/analysis_runs/
      models.py
      coordinator.py
      runtime.py
      workflow.py
    application/transcripts/
      models.py
      registry.py
      buffer.py
      writer.py
      reader.py
      streaming.py
    ports/transcript_store.py
    infrastructure/persistence/
      schema.py
      postgres_store.py
      database.py
      cli.py
    infrastructure/config/settings.py
    infrastructure/observability/redaction.py
  tests/persistence/
  scripts/run_history_smoke.py
```

| 边界 | 职责 |
|---|---|
| RunCoordinator / RunRuntime | 复用现有整体状态机，持有 sessions、任务、取消、通知及写入器；原子修改 live 状态，不等待数据库。 |
| AgentSessionRuntime | 单 Agent 上下文、五态、可见事件日志及独立 SSE 计数；不另起数据库重试管理器。 |
| TranscriptBuffer / streaming | 原事实唯一入口、身份/顺序/时间、脱敏、生成中正文、定稿和晚到审计。 |
| TranscriptWriter | 每个 run 串行组批、确认集合、有限重试及补写，管理待写引用依赖。 |
| TranscriptReader / registry | 来源选择、统一模型、完整过程、正文解析、引用与可见性；不在 HTTP 层复制。 |
| TranscriptStorePort / PostgreSQL adapter | 原子 create/append、提交核实、持久化只读查询、启动修复；不决定业务终态。 |
| HTTP / SSE adapter | 参数、状态码、错误封装、DTO 与 frame 编码；不写 SQL 或直接取消 task。 |
| composition / lifespan / CLI | 注入 store、clock、workflow；创建和关闭连接池；独立迁移与启动检查。 |

Port 的 generic 参数使用有界、已校验的记录/批次模型；不能继续用逐条 append 模拟原子组，也不能让 port 导入 application 或 SQLAlchemy。中立的存储命令/查询 DTO 与 Protocol 一起放在 ports 边界，application 装配它们，adapter 实现它们。必要操作为 `create_run`、`commit_batch`、`verify_batch`、`list_runs`、`read_run`、`read_session`、`list_items`、`load_contents`、`repair_interrupted`；读取返回持久化原记录，公共 DTO 由同一个 reader 生成。引用抽取通过同一注册器注入，不让 adapter 维护第二份字段清单。

保持 `create_app(...)` 的测试注入能力；生产默认使用 PostgreSQL，测试可注入可控 store/workflow。直接替换旧 `/api/analysis/sessions` 路由及整体模型名，不保留兼容路由。同步导入、fakes、架构测试、OpenAPI、CLI、smoke 和 README；不重写 M1 领域模型或 M2 历史验收记录。

## 3. 身份、状态与时间模型

### 3.1 身份与顺序

| 标识 | 生成与作用域 | 保存方式 |
|---|---|---|
| run_id | 新主动提交生成 UUID v4 | run 主键；幂等重试沿用。 |
| session_id | 准备执行一个 Agent 时生成 UUID v4 | session 主键；不是旧 M2 整体 ID。 |
| item_id | 原事实第一次进入统一入口时生成 UUID v4 | 全局主键；重复发生的同文记录仍不同。 |
| transcript_sequence | RunRuntime 在同一临界区递增，从 1 开始 | run 内唯一 bigint；筛选保留缺口。 |
| operation_id | 每次模型/工具调用开始生成 UUID v4 | 开始/结束及直接 hook 共用；原 tool_call_id 保留正文。 |
| session SSE id | 每个 session 的可见过程事件从 1 递增 | 仅 live 事件日志；不写 transcript 行。 |

创建事务分配 request 为 transcript_sequence=1，run_created 为 2。生命周期正文里的 `sequence` 仍是该生命周期流的序号，created=1、唯一 terminal=2；它既不是 transcript_sequence，也不是 session SSE id。新生命周期版本与旧版本区别见 §5。

流式消息在开始时同时固定 item_id、transcript_sequence、created_at，允许未定稿的较小序号和已经保存的较大序号并存。RunRuntime 的下一个序号只在内存维护；重启不恢复执行，修复线程在锁定所属 run 后用数据库 `max(transcript_sequence)+1` 给新修复事实分号。这个最大值只用于新追加排序，不能用于判断旧内容是否全已保存。

### 3.2 当前状态

| 对象 | 非终态 | 终态 |
|---|---|---|
| run | pending、running | completed、failed、cancelled、completed_with_persistence_error |
| session | pending、running | completed、failed、cancelled |

session 的 pending 是“已经创建，准备执行”，不是等待上游。问题解析 session 的 `agent_role=query_parser`、target_comid=null；水体 session 的 `agent_role=waterbody`、target_comid 为 M1 COMID。水体等待所有上游完成、准备执行时才创建；同 run 同 COMID 最多一个水体 session，问题解析最多一个。未参与和等待中的节点由实际拓扑/调度事实表达，不预建 session。

统一状态函数执行合法转换，并在同一 live 临界区形成适用的原记录、状态更新与通知。终态只选一次；失败或取消可由 pending 直接进入。running 没有公开生命周期事件，仍产生必需状态写入，不补造 started 事件或依赖 transcript 回放更新状态。

session 完成不代表 run 完成；run 取消保留已经完成/失败的 sessions，只取消尚未终态者。保存错误属于 run 的 persistence，不能把每个已正常完成的 Agent 改成失败。

### 3.3 时间

所有时间通过 `ClockPort` 取得，复用 M1 的 UTC 校验与序列化。数据库通用时间用 `timestamptz`，保留微秒；拒绝 naive datetime。

- runs/sessions：created_at、updated_at，初始取同一时刻；updated_at 只随对应状态的真实变化更新。
- items/contents：只有 created_at，contents 使用原 item 的时间；API occurred_at 从它装配。
- 重试、读取、落库成功回调不更新时间。updated_at 不能替代开始/结束时间；确需特定动作时间时读取实际原事件。预测窗口、观测时间等正文时间照常保留。

## 4. PostgreSQL schema

四张核心业务表外只有 Alembic 自己的版本元数据表。状态/角色使用 text + CHECK，内容版本使用正整数，不增加 PostgreSQL enum 类型的独立迁移负担。以下未标“可空”的列均 NOT NULL。

### 4.1 analysis_runs

| 列 | PostgreSQL 类型 | 事实与约束 |
|---|---|---|
| run_id | uuid PK | 原 run 身份。 |
| status | text | §3 run 状态 CHECK。 |
| created_at / updated_at | timestamptz | 原创建与真实状态变更时间。 |
| query | text | 从脱敏后 analysis_request 的 query 一次生成的列表投影，无独立修改接口。 |
| idempotency_key | uuid，可空 UNIQUE | 按 UUID 值全库唯一；无请求头则 null。 |
| request_fingerprint | bytea，可空 | 有 key 时为原 HTTP body 的 SHA-256，32 字节；与 key 同时有值/为空。 |
| creation_receipt | jsonb | 首次创建的完整小型成功响应，一次捕获、不可变，不含 HTTP request_id。 |
| request_item_id | uuid | 原安全请求引用。 |
| created_item_id | uuid | 原 run_created 引用，配置只从此记录装载。 |
| terminal_item_id | uuid，可空 | 已选定终态记录引用；非终态为空。 |

原回执为无 key 的创建也保留，以统一创建结果核实；只保留这一份，不增加同义快照。它包含创建配置是为了原响应稳定复用，不作为可编辑配置源。

上述内容引用使用 `(run_id, item_id)` 复合外键指向 transcript_items，`DEFERRABLE INITIALLY DEFERRED`，允许同一创建事务先插入 run 再插入原记录。类型及 run-root 归属由统一批次验证器检查。必要 CHECK 保证 terminal status 与 terminal_item_id 同时成立。不增加完整 request/config/result JSON 或保存进度列。

### 4.2 analysis_sessions

| 列 | 类型 | 事实与约束 |
|---|---|---|
| session_id | uuid PK | 独立 Agent 上下文身份。 |
| run_id | uuid FK | 所属 run；另设 UNIQUE(run_id, session_id) 供联合归属校验。 |
| agent_role | text | query_parser / waterbody。 |
| target_comid | text，可空 | 复用 M1 字符串 COMID 验证，parser 必须 null，waterbody 必须有效。 |
| status | text | §3 session 五态 CHECK。 |
| created_at / updated_at | timestamptz | 本 session 的原时间。 |
| created_item_id / terminal_item_id | uuid / uuid 可空 | 自己的原生命周期引用，终态引用与状态一致。 |

增加部分唯一索引：query_parser 的 run_id 唯一；waterbody 的 `(run_id, target_comid)` 唯一。内容引用使用同 run 的延迟外键，验证器再检查 item.session_id 等于本 session。role/COMID 不重复保存到 item 查询列或 scope JSON；API scope 从此表装配。

### 4.3 transcript_items

| 列 | 类型 | 事实与约束 |
|---|---|---|
| item_id | uuid PK | 不可变身份。 |
| run_id | uuid FK | 所有记录必有所属 run；UNIQUE(run_id, item_id)。 |
| session_id | uuid，可空 | null 是 run 记录；联合 FK(run_id, session_id) 指向 sessions，防止跨 run。 |
| transcript_sequence | bigint | >0；UNIQUE(run_id, transcript_sequence)。 |
| created_at | timestamptz | 原记录时间。 |
| content_type | text | 注册类型，不另存外层 type。 |
| content_schema_version | integer | >0。 |
| request_id / operation_id | uuid，可空 | 原请求关联、调用关联；error_id 从对应 typed error 提取。 |
| tags | text[] | 默认空数组；首批类型在公共入口维护，正文已有同义属性时只能由其派生。 |
| rag_eligible | boolean | 默认 false；首批类型在公共入口维护，正文已有同义资格时只能由其派生。 |
| inline_body | text，可空 | 完整安全正文的 UTF-8 JSON 序列化文本；超阈值时为空。 |
| preview_head / preview_tail | text，可空 | 仅大正文有值；安全字符边界截取，不冒充 JSON。 |
| content_bytes | bigint | 完整安全正文的 UTF-8 字节数，>=0，同一编码入口计算。 |

正文采用文本保存一次序列化的 typed JSON，避免 JSONB 重排导致 preview、字节数及原文读取不一致；查询所需身份/顺序在列中，不对完整正文建 GIN 索引。行 CHECK：inline 有值时两个 preview 均为空；inline 为空时两个 preview 均有值；内联字节数与 `octet_length(inline_body)` 相符。

### 4.4 transcript_contents

| 列 | 类型 | 事实与约束 |
|---|---|---|
| item_id | uuid PK、FK | 直接归属于原 item，不另设 content_id。 |
| body | text | 原 item 唯一完整安全正文。 |
| created_at | timestamptz | 使用原 item.created_at。 |

跨表的“inline XOR contents、字节数、同时间”由同一个 adapter 在原子事务内验证：内联 item 禁止附带 contents，外置 item 必须同批提交全文；幂等核实也检查这些条件。普通 CHECK 无法独自验证另一张表是否有行，因此文档不把它误称为行级数据库约束。M3 无独立 contents 写接口或正文编辑接口，不增加触发器框架；人工越过 adapter 改库属于数据损坏，读取显式报错。PK/FK/唯一性仍由 PostgreSQL 强制保证。

### 4.5 索引、引用及事务边界

- 历史列表索引 `(created_at DESC, run_id DESC)`。
- session 目录索引 `(run_id, created_at, session_id)`；完整过程索引 `(run_id, session_id, transcript_sequence)`；run 全部原记录利用序号唯一索引。
- 初版不为 confidence、provenance、tags、工具名等预建筛选索引；不存 scope/correlation JSON、额外公开 content_ref 或关联表。
- 所有业务内容只追加，冲突时不能 `DO UPDATE` 覆盖正文。相同身份且各不可变字段/正文相同才视为同一事实；同 ID 不同内容报 `persistence_identity_conflict` 并保留待写与诊断，不能吞冲突。
- 记录和所需同批引用、状态、全文形成原子组，先验证再提交；事务中的引用目标必须已存在于同 run 或在本批。引用生成不能指向仍在生成且不在本批的草稿；这种有依赖的组等待目标定稿，无关可写组照常保存。
- 明确引用在原 typed content、AnalysisDetail、AnalysisStep 中保留；注册器唯一实现提取/验证。首版不做反向索引，M6/M7 按真实查询需求评估。

## 5. Typed content 与读取表示

### 5.1 注册器

每个 `(content_type, content_schema_version)` 注册严格的 Pydantic model、序列化、引用提取、业务元信息提取、可见性和可选消息适配器。禁止 extra 与任意未注册写入；不为未来结果引入 `Any`、自由 dict 或假水质结果。

| 首批类型 | 版本 | 正文 |
|---|---|---|
| analysis_request | 1 | `query`、请求中实际出现的 `time_resolution`；保留字段有无，不把生效默认值回填成用户输入。原始传输 bytes 不保存。 |
| lifecycle_event | 2 | run/session created、completed、failed、cancelled 的严格联合；保留原 type、schema_version、sequence、occurred_at 和 typed payload。 |
| error | 1 | 不附属于已有生命周期的独立 `PublicError`；已嵌入失败事件的错误不再复制一条。 |
| runtime_fact | 1 | 下表的严格 fact 联合；不是任意 JSON 容器。 |

新 lifecycle_event 的内部 schema_version=2，content_schema_version=2，统一使用 run_id 和适用的 session_id。run 类型为 `run_created/run_completed/run_failed/run_cancelled`；Agent 类型为 `session_created/session_completed/session_failed/session_cancelled`。created 的 configuration 只属于 run_created；session_created 从所属 run 引用配置。terminal payload 保留状态、对应结束时间和适用 PublicError/warning。`run_completed` 允许 completed 与 completed_with_persistence_error。时间与身份都从同一原变化生成。

版本 2 的共同字段为 `{type,schema_version,run_id,session_id,sequence,occurred_at,payload}`，run 事件的 session_id=null。payload 明确为：run_created 的 `{status,created_at,configuration}`；session_created 的 `{status,created_at}`；completed 的 `{status,completed_at}`（run_completed 另有 warning，正常为 null，保存错误终态为 PublicError）；failed 的 `{status,failed_at,error}`；cancelled 的 `{status,cancelled_at}`。类型与 status 必须匹配，各动作时间取同一状态变化时刻。session_created 不复制角色/COMID/配置，归属从 session 与 run 的原事实读取。

M2 版本 1 的事件保留 reader 支持，原字段不改写，不把旧整体 ID 变成真实水体 session；当前 M2 没有持久化数据库，因此不引入旧内存数据迁移。新路由只生成版本 2 的生命周期正文；它作为 transcript 内容发送，不再直接充当整个 run 的 SSE 协议。

| runtime_fact.fact | 正文中的特有字段与语义 |
|---|---|
| workflow_quiesced | 无额外正文；所有执行方收敛时追加一次，归属由 item 决定。 |
| persistence_recovered | `error_id`：关联该轮实际保存故障，恢复后追加一次，不在保存恢复记录自身成功时递归新增。 |
| late_message_output | `original_item_id`、`text`、`end_reason`（completed/cancelled/error/truncated）；只含取消后的新增文本，是历史审计。 |

进程遗留修复使用 failed 生命周期及 `PublicError(code=service_interrupted)`，不复制 runtime interruption 记录。BFS、业务输入、LLM、tool、skill、hook、结果由实际生产阶段注册类型。M3 的消息能力通过测试专用严格 `fixture_message`（text、generation_status、可选 end_reason）验证，它不注册进生产，也不作为公开业务 payload 承诺。

### 5.2 消息与元信息

消息适配器从对应 typed body 读取 `generation_status`：generating 仅在 live，completed/incomplete 可持久化；只维护正文中的一份值。适配器为 started/delta/finalized 提供通用事件装配，类型注册仍由生产阶段完成。工具结构等非文本内容作为自己的 typed 记录追加，不硬塞进 text delta。

API 的 confidence/provenance 由注册器按对应业务层级提取；不适用时为 null，confidence 真实值须在 [0,1]，多来源原样保留。不能把时间片、参数、综合指标的置信度压成一个任意公共值；无单一适用值就返回 null 并保留正文各层事实。首批类型的 tags/rag_eligible 在公共入口一次指定并落列；已有业务类型（例如证据）的正文包含同义资格时，以正文为事实源，查询列只能由注册器派生，调用方不得再传一份公共值。无需为了存储删改 M1 业务字段；语义不同的业务标签仍属于正文。

### 5.3 单份正文与大内容

统一入口先脱敏，再用统一 UTF-8 JSON 编码（`ensure_ascii=False`、紧凑分隔符、拒绝 NaN）取得固定文本及字节数；解析/验证通过后再进入 live。持久化边界复核同一脱敏规则，不按重试重新产生不同正文。

- 默认 `content_bytes <= 65536` 内联；超过则完整正文在 contents，仅 item 保留首尾预览。
- 预览正文默认 head/tail 各最多 1024 bytes，总计最多 2048；按 Unicode 字符边界截取。`truncated=true` 是 API 装载标志，不把省略号拼进原文或伪造可解析 JSON。
- 全文接口对内联和外置 item 都适用，返回同一份安全文本；引用由 `{run_id,item_id}` 装配，URL 由 adapter 生成。
- 消息 finalization、大正文、历史及错误容错复用此表示；preview 不能覆盖成一条“完整消息”。完整 JSON 的业务字段只由全文解析器提取，不从 preview 猜测。
- 存储大小与前端折叠无关；技能/工具/钩子等始终按语义默认折叠，展开可读完整内容。

### 5.4 未知版本与损坏

reader 返回 item 的 `parse_status=ok/unsupported/corrupt`。unsupported 保留身份/类型/时间、raw 全文引用，不把它当业务结果；corrupt 附安全 PublicError 和 item_id，正常邻近记录继续返回。两者不修改数据库或静默跳过。全文装载能取得原文本时仍经过安全出口返回，无法取得外置正文则返回明确 500 `transcript_content_unreadable`，不返回空文本。整个数据库查询失败返回 503。

已知类型损坏可能使引用/摘要/配置不可解析：将问题记录放进返回记录列表与 `issues`，对应派生值为 null，不用当前配置或猜测值补齐。未知类型默认只允许严格历史原文查看；auto 无法证明其取消可见性时不公开正文，防止未知晚到审计绕过边界。

## 6. 创建、事务与补写

### 6.1 创建与所有权

1. HTTP 边界只读取 body 一次。先校验 optional UUID v4 key，计算原 bytes 指纹，再解析/验证业务请求。query 的业务 trim 和默认值解析不参与指纹；不保存凭据未处理的原 bytes。
2. 相同 key 已在库中时先比指纹，相同直接取不可变回执，不同 409；这一步在应用新默认配置或重新构建请求模型之前完成，避免旧创建被新配置影响。
3. 新创建生成 run、原 request、配置创建事件和 receipt，一次事务提交。DB UNIQUE 处理同 key 竞争；内存创建所有者表只用于本进程协调和注册窗口，不能代替 DB 唯一性。
4. 创建操作由 coordinator 持有，HTTP 只等待其结果；HTTP 断开或等待者超时不取消所有者。已提交到 live 注册/任务发放完成前保留所有者，读取/SSE 遇到窗口等待该状态收敛，不能立即判 410。
5. 只有确认提交才打开 workflow start gate。已确认回滚不执行；提交结果不明保留相同 run/item/receipt 和所有者，用 §6.3 核实，不能另建执行或把超时当回滚。请求可先返回安全 503 `creation_commit_unconfirmed`；它不是“未创建”的保证。
6. 核实成功即完成本进程注册与一次启动；核实明确无提交则释放所有权。无法核实时保留受理信息，在 DB 可读后核实，后续同 key 请求也复用该所有者；这是收敛一次未知结果，不是新的创建写入重试预算。进程退出后的已保存 pending 仍按启动修复失败，不自动执行。
7. 提交成功但 task 启动失败，返回原 201，统一状态机产生 failed/workflow_start_failed；取消先胜出则保持 cancelled。重试只返回原回执，绝不再启动。

同 key 等待默认 5 秒，锁等待与重读共用截止时间，超时 503/idempotency_wait_timeout。创建写入默认 5 秒覆盖连接获取、SQL、commit；二者相交采用更早截止时间。创建不套用后台五次补写。receipt 不含动态 persistence、当前状态、临时读取来源或当次 request_id，避免重试响应漂移。

### 6.2 RunWriter

live buffer 保存原事实；writer 待写集合只持有原对象身份和必要状态更新，不另复制正文队列。每 run 一个 task、一个活动事务，多个 run 可并发。状态锁内选取原子组和冻结批次，在锁外等待数据库。

空闲收到可写组或独立状态更新立即开始；默认目标 100 items，不等凑满，不做空事务。不能拆散同一变化的状态/事件/全文/引用目标；大于目标的原子组独立提交。生成中草稿不是可写组，但已产生的必需正文计入 pending。失败后冻结原批次，新事实排在后续；无依赖的新组可越过未定稿草稿，不能越过已失败的活动批次。

状态变更的内存确认使用具体 mutation 身份与目标快照；它不对外暴露，也不增加 DB state_revision。适用状态与事件同事务提交；一个批次包含同一行多次变化时按原顺序验证/应用，数据库最终保存最后状态，全部原事件仍保留。独立 running 更新也需确认，不能因没有公开事件而漏存。

### 6.3 原子提交、超时与核实

每次尝试用一个应用层单调时钟 deadline 覆盖取连接、begin、SQL 和 commit；SQL `statement_timeout` 按剩余预算设置，只是辅助限制，不为每条语句重给 5 秒。异常先取消调用、失效并关闭不能安全复用的连接；连接关闭收尾使用单独短上限 1 秒，超出后丢弃连接并保留 unknown 状态，不认为回滚已成立。旧客户端写调用退出前不能并行发新批次。

创建、增量写入、核实及遗留修复统一先取得所属 run 的 PostgreSQL 事务级 advisory lock，再读取/修改该 run。锁键由规范 UUID 文本的固定 64 位散列生成，使用 pg_advisory_xact_lock；散列碰撞仅导致无关 run 暂时串行，不替代真实 UUID/唯一约束。锁随事务结束释放，不增加锁表、租约或多实例调度系统。[PostgreSQL advisory locks](https://www.postgresql.org/docs/17/explicit-locking.html#ADVISORY-LOCKS)

核实在取得同一锁后的新 READ COMMITTED 查询中检查冻结批次的实际身份、不可变字段、全文和目标状态。已完整提交则仅确认；确认未提交则按预算重写同一批；混合/内容冲突显式失败，不补成另一个事实。创建 run 行尚不可见时，行锁或一次 SELECT 查无行不能证明回滚；只有旧调用已退出且同一事务锁已重新取得后，才能查询其最终提交结果。核实不靠补插一条新创建记录探测，也不能启动第二个所有者。同 key 不同候选 run 的竞争仍由数据库 key 唯一约束裁决。

每次核实使用有限读/锁预算，失败仍保持未确认；禁止 adapter 透明重试 SQL 或绕过 writer 预算。实际 commit 成功/核实成功才回调具体 item/group/mutation 的确认集合。没有持久化 batch 表，不用最大已保存序号推断此前草稿已保存。

psycopg 文档说明异步任务取消时数据库操作可能已经完成，因此未知提交核实是必要边界，不能靠 Python CancelledError 推断回滚。[psycopg 异步取消说明](https://www.psycopg.org/psycopg3/docs/advanced/async.html#interrupting-async-operations)

### 6.4 重试和交付窗口

| 次序 | 相对上一尝试失败并收尾后的等待 |
|---|---|
| 首次尝试 | 立即 |
| 重试 1 / 2 / 3 / 4 / 5 | 1 / 2 / 4 / 8 / 16 秒 |

单次默认 5 秒，共首次加最多 5 次重试。核实属于对应尝试的预算，不能形成第七次写入。每次真实恢复重置连续失败周期；新 item、分析结束、GET、数据库恢复本身不重置。第 5 次仍失败即停止并保留内存；手动恢复只在耗尽后开启新一轮。

分析逻辑成功时冻结此前已产生的必需 facts/mutations 集合。该集合全部确认就选 completed；仍未保存则沿正在进行的失败周期等待到“第 3 次重试失败后的 2 秒”，已过期不重新等，届时选 completed_with_persistence_error 并携带原安全 warning。后续第 4/5 次继续补写。该窗口不是固定的总共 9 秒或 5 秒，因为各次数据库尝试本身耗时不同。

终态选择在原状态机里执行一次，等待不持有状态锁，cancel 仍能立即胜出。终态状态/事件另作为适用原子组保存，选定后它们或收尾保存失败只改变当前 persistence，不改原终态或重发 terminal。

### 6.5 保存状态、手动恢复与清理

`PersistenceInfo` 是 run 的 live 派生结果：`state=up_to_date/pending/retry_exhausted` 与 `warning:PublicError|null`。先判断耗尽；否则生成中必需正文、待写组或未确认状态均为 pending；仅所有当前必需事实确认时为 up_to_date。正常等待生成时 warning=null，实际失败才有警告；保存恢复后当前 warning 清除，原终态 warning 不改。

手动 POST 重试复用原 writer：耗尽时开始新一轮，pending 时返回现况、不重置，up_to_date 时不做空事务。等待生成也不能被手动请求强制定稿。没有 live 无法恢复未保存内容，响应见 API 指南。

run 终态、所有工作执行方 quiesced、所有必需事实及状态实际确认后，开始默认 60 秒可配置 grace；包括终态、晚到审计与恢复事实。失败不计时，后续新待写使资格失效，恢复后重新获得完整 grace。到期在原状态锁中复查再移除 live runtime/事件/正文；cleanup 幂等、失败可重试，不影响业务终态。SSE 的正常结束不等待 grace，也不增加极慢连接排空/浏览器 ACK 协议。

## 7. 统一读取与历史查询

### 7.1 一次选择来源

reader 先固定 `view` 与来源：auto 有 live 使用该 run 内存快照（即使 PG 故障），否则使用 PostgreSQL；history 直接用 PostgreSQL。一次响应中的记录、session 归属、配置和引用来自同一来源，不能逐字段混合。live 提取在短临界区复制不可变引用，编码在锁外完成；PG 多语句装载使用短只读 REPEATABLE READ 事务，保证同一响应的目录/原记录一致。

响应 `source=live/postgresql`。live 返回 PersistenceInfo，数据库返回 persistence=null；null 仅表示没有 live 保存进度，不证明没有历史丢失。HTTP 引用继续带同一个 view；若两次 HTTP 之间 live 已释放，auto 的新响应可合法转 PostgreSQL，并明确报告来源。history URL 显式带 view=history。

### 7.2 run 详情与 session 完整过程

run 详情装配当前 run、原配置、真实 session 目录和 **全部 run-owned 记录**，提供后续实际 context/result 的原 item 引用；不读取所有 Agent 完整消息来拼地图。session 详情给出自身状态/时间、适用真实结果引用与 transcript URL。生产方未产生业务类型前不提供假结果/目标参数占位；必要业务展示由后续类型扩展。

session transcript 先读取本 session 的全部可见原记录，按 run transcript_sequence 升序返回；再沿这些记录的显式引用装载公共上下文为 `referenced_items`，同 item 去重。引用闭包保留原身份/序号，用 visited 集合防止循环，不无边界扩张为其他 session 全文。只有被显式引用的其他 session 内容会带入；不能只看最终总结选的几条步骤。

引用解析可为提取正文引用临时装载大正文，HTTP 仍返回统一的内联或 preview/ref；不能从 preview 猜引用。原子组提交保证新历史不会有半批引用；未知/损坏记录通过 parse_status/issues 显式呈现。session 无记录返回真实空数组，数据库异常不能返回假空。Agent 过程无 cursor、limit、tail 或固定条数截断。

### 7.3 可见性

取消后的 `late_message_output` 以及后续注册的仅审计类型，从类型规则决定 history-only，不增加 after-cancel 字段。auto 的列表、详情、全文、引用和 SSE 都执行同一过滤，包括 auto 回退 PostgreSQL。直接请求被隐藏 item 返回 404 `transcript_item_not_found`；history 可读取已提交审计及原前缀，但不把它拼回当前消息。

### 7.4 run 历史分页

只接受 `limit`（默认 50，整数 1–200）和 `cursor`；多取一条判断有无后页。排序为 created_at DESC、run_id DESC，续页谓词为 `(created_at, run_id) < (:created_at, :run_id)`，只读库。返回 items 与 next_cursor，无 total；状态变化不改变位置，新 run 通过刷新首页查看。

游标内容是 UTF-8 JSON `{v:1,created_at:UTC微秒字符串,run_id:UUID}` 的 URL-safe base64（去 padding）；解码严格检查字段、版本、时间精度、UUID 与长度（最大 1024 字符），异常 422 `invalid_history_cursor`，不回首页。无需签名、数据库游标表或页面快照；游标只是公开排序位置，不作为权限凭据。

## 8. 两层 SSE 与消息装配

### 8.1 run 通知

`event: run_snapshot` 推送 run 状态/时间、实际 session 简表、persistence 和已存在的水体参与/执行状态。没有 SSE id、事件回放日志或 DB 通知行。首次及重连先取最新完整轻量快照；状态变更后可合并尚未发送的旧通知，仍发送完整当前快照。只有原状态变化才唤醒，不用轮询数据库维持连接。

水体通知使用 `{target_comid, participating, session_id, session_status}`，尚无 session 两项为 null；参与判断来自 M4/M7 的真实拓扑与调度，session 状态直接引用原目录。M3 未产生拓扑时 waterbodies=[]，测试注入显式事实覆盖 excluded/waiting/running/completed/failed。颜色和取消样式不进入后端字段。历史水体装配读取原拓扑记录与 sessions，不保存通知快照。

### 8.2 session 过程

| 事件 | data payload | 是否形成独立 transcript item |
|---|---|---|
| transcript_item | `item: TranscriptItemView`，非流式的原可见事实 | 原事实本来就有，发送不再次追加。 |
| message_started | `item: TranscriptItemHeader`，含原身份/序号/时间/类型/归属 | 只分配原消息草稿，暂不写库。 |
| message_delta | `item_id, text`，仅安全新增文本 | 否。 |
| message_finalized | `item: TranscriptItemView, generation_status`，后者由原正文派生为 completed/incomplete | 原消息整理保存一次，不另建事件正文行。 |

共用 wrapper `{schema_version:1,run_id,session_id,sequence,occurred_at,payload}`；SSE event 名表达类型，不再在 wrapper 复制 type。这里的 occurred_at 是该次推送事实产生时间；item 内 occurred_at 保持原消息创建时间。所有可见事件先在 session 日志分配稳定正整数，再唤醒订阅者，即使没有连接也保留。message_delta 之间可穿插其他记录，单消息 started 必须在首个 delta 前，finalized 在全部可见 delta 后。

一次消息最终正文可由已收片段形成；如果提供方还返回累计最终文本，适配器只接受与原片段对应的新增后缀，不能再追加整段。两种输出不一致时保留实际收到的安全正文并产生类型明确的诊断，不静默改写已发内容；具体供应商语义由真实接入阶段验证。

message_finalized 不等待数据库；大正文沿统一装载表示，最终状态从原完整消息适配器提取到事件 payload 的 `generation_status`，仅为该正文事实的派生值，调用方不能独立填写。前端结束同一消息的打字状态，不判断整个 Agent/run 已结束或保存成功。

### 8.3 续传、握手与结束

session 第一次省略 Last-Event-ID 或传 0，先发送全部已有可见事件，再接新增；条件锁中完成快照/订阅衔接，避免两步之间遗漏。重连只发送大于最后已消费编号的事件，原 id 不变。只接受非负 ASCII 十进制，非法 400/invalid_event_cursor，超过该 session 末尾 409/event_cursor_ahead。run 流不使用该头，提供时忽略，不更新 session 游标。

合法且 live 在的两流统一 200 text/event-stream，禁止旧 204 分支。run 首发快照；session 已终态也先补齐事件。keepalive 沿用 15 秒 SSE comment，无 id、无数据库/业务日志。以下条件成立发送无 id 的 `stream_end` 后关闭：

- session：自身终态、所有可见过程和 message_finalized 已生成且发完。不等 run 数据库补写，晚到审计也不会再进入可见流。
- run：run 终态、执行收敛、全部必需内容和状态确认，先发最终保存快照再 end。pending/exhausted 时继续可通知。

stream_end 只表明这条流正常结束；未收到而 EOF 是中断。网络断开不取消 workflow；客户端重连不启动分析。live 已释放/重启且历史存在返回 410 及明确 history URL，无历史 404，DB 失败 503；不能从数据库拼 token 回放或恢复执行。

## 9. 取消与流式脱敏

### 9.1 原消息与晚到审计

run cancel 在统一状态边界关闭调度入口，固定全部生成中原消息为 incomplete，再调用内部 session cancel 能力覆盖未终态 sessions。返回立即选定的 run 状态，不等待模型停止或落库。这个临界区中不做数据库/网络等待。

取消边界前已接收的文本属于原 item，保留身份/序号/时间；尚在脱敏私有缓冲中的不确定敏感区安全替换后纳入原文，不能直接 flush 原始字节。其后接收的新片段按原消息聚合一个 late_message_output 审计，独立分配身份/序号/时间，只存新尾部并引用原消息；空尾部不创建假审计。待执行方收敛后定稿入库，引用原文必须已保存或同批保存。原前缀不再改写，也不因历史查看拼接成实时消息。

### 9.2 凭据边界

复用并扩展现有 redaction：结构化敏感键、Authorization/Cookie、URL userinfo、已知实际凭据，以及常见凭据标记后的值。普通业务 query 不整段隐藏；HTTP、workflow、异常 repr/JSON 转义、driver 错误、日志及 safe preview 都走相同规则。原 Agent 实际输入不因日志脱敏而被改写。

流式过滤器在不可读的私有缓冲中维护已知 secret 前缀和敏感字段识别状态：仅释放已经排除匹配可能的安全前缀；凭据值进入遮蔽状态后不缓存或发送原值，直到确定分隔边界。known secret 的后缀前缀保留到后片段确定；敏感标记可能跨片段时同样保留。结束/取消时未能判定的敏感候选按安全替换收尾，不能原样放行。取消之后保留遮蔽状态，避免跨边界剩余凭据出现在历史尾部；不保留一份未脱敏全文。

这是限定凭据规则的跨片段处理，不宣称识别所有敏感信息。使用合成凭据逐切分点验证（包括 Unicode、引号、转义、分隔符、取消处与日志），并验证正常文本有持续增量输出；不能通过整段等到结束才输出规避流式验收。持久化出口再次校验与脱敏，不能用最终安全覆盖弥补已泄露 SSE。

## 10. 配置、schema 命令与应用生命周期

### 10.1 技术选择

选择 PostgreSQL 17 系列作为首版统一验证目标，Compose 使用 `postgres:17`，实际验收记录所用 minor/digest；运维保持 17 系列受支持小版本。当前官方支持表显示 17 的支持期至 2029-11-08；不要求第一版支持所有大版本。[PostgreSQL 版本政策](https://www.postgresql.org/support/versioning/)

新增依赖约束为 SQLAlchemy 2.0 系列（含 asyncio extra）、psycopg 3（含 binary extra，>=3.2,<4）、Alembic（>=1.13,<2）；`uv.lock` 固定本次验证过的实际版本。运行时使用 SQLAlchemy Core AsyncEngine + psycopg，不引入 ORM session/关系对象图；迁移走独立 CLI。SQLAlchemy 的 psycopg dialect 支持异步 engine。[SQLAlchemy psycopg dialect](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html#module-sqlalchemy.dialects.postgresql.psycopg)

Windows 正式入口在创建事件循环前选择 psycopg 支持的 Selector loop；测试与 smoke 共用入口，不能只让迁移命令成功。不能在已运行的 loop 中更换 policy。Python 3.11 是跨平台基线，其他版本据实际回归登记。[psycopg Windows 异步要求](https://www.psycopg.org/psycopg3/docs/advanced/async.html)

### 10.2 配置与命令

配置沿用 WATERAGENTS_ 前缀。新增字段、默认值和可复制命令集中在 [API 指南 §8](m3_transcript_history_api.md#8-跨平台搭建与配置)；这里固定实现边界：

- `.env.example` 可提交，真实 `backend/.env` 忽略；AppSettings 显式以 backend 根目录解析该文件，避免依赖启动 cwd。
- 优先级：显式 settings 注入 > 进程环境变量 > backend/.env > 默认值。数据库密码是 SecretStr，不 trim，不自动展示；不配置 password 默认值。
- host/port/name/user/password 分列，通过 URL.create 构造连接，密码不手工 URL 拼接或百分号编码。远程 SSL 使用 sslmode/sslrootcert 参数。[SQLAlchemy URL.create](https://docs.sqlalchemy.org/en/20/core/engines.html#creating-urls-programmatically)
- `wateragents-db upgrade` 从空库或旧版执行 Alembic upgrade head；`wateragents-db check` 只读连接与严格版本检查。命令共享 settings，凭据不写 alembic.ini、不回显 URL。
- `migrations/versions` 的单 head 是代码所需版本的唯一来源；读取 alembic_version 比较，禁止再硬编码一套版本号。Alembic 负责版本升级与记录。[Alembic 教程](https://alembic.sqlalchemy.org/en/latest/tutorial.html)
- Compose 本地端口只绑定 127.0.0.1，持久卷挂载 PG17 数据目录，healthcheck 只证明 PG 可连接。已有远程实例只需同一套连接变量，不要求 Docker。

### 10.3 启动、恢复和关闭

正式 lifespan 按顺序：创建 engine → 连接检查 → strict schema head 检查 → 修复遗留 → 创建 coordinator → 接收请求。前两项用配置的数据库操作预算；修复按 run 逐事务处理，每事务相同预算，不给整个历史量固定 5 秒上限。

恢复枚举所有非终态 run，以及所有非终态 session 所属的 run（包括已终态 run），去重并按 run 锁行处理。保持各层既有终态，pending/running 改 failed/service_interrupted；状态、terminal 引用和新失败事件同事务保存，序号延续本 run 已存事实。新时间表示实际修复发生，不假装知道崩溃时刻。每次修复成功后下一批继续，失败则本次启动失败，下一次只处理尚未修复者。没有未保存事实可回放，不重启 Agent。

同一数据库只允许一套活动的单进程/单 worker 服务；多个 run 并发不代表允许另一个实例在启动时修复正在运行的数据。M3 不交付多实例协调，部署说明必须明确此限制。

关闭停止受理/新调度，通知现有工作协作收敛，保留用户 cancel 与进程 stop 的区别；在有界 shutdown grace 内完成可完成的收尾与写入，不等待完整五次退避。超时取消并关闭 writer/engine，不假装写入成功，下一次启动修复已保存非终态；未保存内存可能丢失。沿用现有 HTTP 连接关闭机制，新增资源必须有跨平台实际关闭验证。

### 10.4 跨操作系统可迁移性

本节承接用户明确要求的“不同操作系统的可迁移性和简单迁移”，作为 [GLOBAL-C11](../deferred_milestone_contracts.md#2-跨-milestone-硬约束) 的 M3 落地。这里的迁移指应用运行环境在 macOS、Windows、Linux 之间切换，PostgreSQL 仍是唯一数据库方案。

| 层面 | 必须保持的约束 |
|---|---|
| 源码与入口 | 同一份 Python 业务代码、项目配置和正式 CLI；操作系统差异集中在启动/基础设施边界，例如 Windows 事件循环，不能分叉业务状态机、API 或保存逻辑。 |
| 依赖 | 共同提交 pyproject.toml 与 uv.lock；目标机器按锁重新安装对应平台依赖，不复制 .venv、原生二进制或本机缓存，不维护三份人工同步的依赖清单。 |
| 路径与资源 | 使用 pathlib、包资源或明确的项目根解析；不硬编码开发机路径、盘符、分隔符、当前工作目录或 /tmp。配置文件、文本和正文显式使用 UTF-8，支持含空格/中文路径和系统换行差异。 |
| 脚本与进程 | 初始化、升级、检查、smoke 的核心由共同 Python/CLI 实现；不依赖 Bash、GNU 命令、Unix signal/socket 或文件锁才能完成正常启动/关闭。子进程使用参数列表和实际解释器路径；必要的平台处理集中封装。 |
| 配置 | 变量名与优先级不随操作系统改变；只替换目标环境的连接、端口、凭据及证书位置。证书等本地路径可配置，不能成为历史全文或业务 item 的定位身份。 |
| schema 与历史 | 相同应用版本使用相同 Alembic head、四表和 API；已保存 UUID、顺序、UTC 时间、原回执与 run/item 引用不依赖宿主机。更换系统不触发业务数据格式转换。 |

uv.lock 能描述操作系统、架构和 Python 条件下的依赖选择；本项目仍须实测所支持的平台组合，不能把有锁文件等同于原生依赖一定可安装。迁移验收记录 OS、CPU 架构、Python 和锁文件版本；使用锁定安装发现不兼容时修正共同依赖配置，不在目标机临时改业务代码。[uv 跨平台锁文件](https://docs.astral.sh/uv/concepts/projects/layout/#the-lockfile)

最短迁移路径是取得同一代码版本、按共同配置模板填写目标环境、重新安装依赖、check 后启动。继续连接同一可达的 PostgreSQL 时，不复制历史、不重新建表；新建空 PostgreSQL 时先用现有 upgrade 命令初始化同一 schema。迁移步骤统一记录在 [API 指南 §8.5](m3_transcript_history_api.md#85-更换操作系统或机器的最短流程)，不新增一套系统专用部署工具。

迁移是停止旧实例后的重新部署，不转移在途 Agent、SSE 日志或未保存内存。保留已有单实例、保存确认、启动遗留处理及不自动恢复执行的语义；本项不扩大为历史数据搬迁、在线迁移或跨机任务接管。

## 11. 测试与验收设计

| 层 | 必须证明的行为 |
|---|---|
| 模型与纯函数 | run/session 五态与六态、UTC、UUID、严格注册、JSON round-trip、字节边界、引用提取、游标编解码。 |
| Application + 可控故障 store | 单 writer、草稿缺口、状态独立确认、原子组、1/2/4/8/16、第三次失败后两秒、取消胜出、耗尽/手动恢复/清理。 |
| 真 PostgreSQL | 唯一 key 竞争、回执复用、FK/序号/事务回滚、全文单份、引用批次、未知 commit 后核实、启动遗留与严格 schema。 |
| HTTP / SSE | 十路由及错误、auto/history、完整过程、两层流、首次与新增无缺口、三阶段消息及晚到审计、200/410/stream_end。 |
| 脱敏与日志 | 全切分点合成凭据、取消跨边界、普通 query、异常与 driver 输出、全文/预览和无订阅生成。 |
| 正式服务 smoke | 实际 PG + Uvicorn，创建→两层观察→历史→重启读取；取消与连接关闭；Windows selector/资源收尾。 |
| 跨系统迁移 | 三个系统分别作为目标环境，以同一代码/锁完成安装和启动；顺序接入同一测试 PG，核对原历史、回执、游标与全文引用，期间不修改业务代码。 |

时序用可控 Clock 和 async gate，避免真实等待五次退避。真实 PG 测试不能被 SQLite 或 fakes 替代；故障注入应区分提交前失败、提交后响应丢失、未知结果未核实、旧 SQL 尚未结束，不能把一个通用 raise 当作所有场景。

跨平台 CI 拆为三平台 Python 单元/协议检查和 PostgreSQL 集成任务；如 hosted runner 的 PG 供应方式不同，采用其平台可用实例或专门测试环境并记录证据，不假设 macOS/Windows 支持 Linux service containers。三平台正式入口连接 PG 与 smoke 必须有实际证据；缺少某平台只标 partial，不能宣称全平台完成。

逐条契约映射、工作顺序与 Verify 命令见 [实施清单](m3_implementation_todo.md)。文档、OpenAPI、配置、错误码、fixture 与 smoke 同批维护；通过必要验证后才进入下一工作包，不为复述实现而制造低价值测试。

## 12. 完成标准与接力

M3 完成需同时满足：四表与环境可搭建；已保存事实可重启读取；创建/取消/补写/唯一终态保证成立；用户能完整读取任一实际 session 和原公共引用；实时两层流与三阶段消息不重复写正文；凭据不进入可见流；中心 M3-C01–C20 和 M3-A01–A12 有实际实现与验证证据。

M4/M6/M7 通过同一记录入口、session 生命周期与类型注册接入，不能另建自己的 transcript/SSE/补写系统。未来增加真实 content_type、跨 run 证据或查询索引时，需同步原类型与对应阶段契约；不能以未来扩展为由削弱 M3 的当前完整读取。配置与 API 指南必须可供新环境独立使用，未完成内容在 TODO 与中心登记中保持未完成。
