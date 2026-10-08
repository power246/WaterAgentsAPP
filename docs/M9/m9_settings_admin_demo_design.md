# M9 Settings/Keys/Admin + Frontend Demo Closure 设计

更新日期：2026-10-08

本文记录经 `$grill-me` 对齐后冻结的 M9 产品与跨域架构决定。当前内容只表示设计完成，不表示 M9 已实现、已联调或已通过第一版验收。分阶段施工、验证门禁与完成证据见 [M9 Implementation TODO](m9_implementation_todo.md)。

## 1. M9 的定位

M9 是 M1–M9 同一套第一版系统的收口阶段，不是一个需要兼容 M1–M8 阶段接口的独立产品版本。M9 同时负责：

- 基于当前最终 OpenAPI、两层 SSE 和 history/transcript 读取契约独立实现新前端；
- 定型并实现 settings、provider/model、secret/key store 和受控 admin API；
- 把 M8 已实现的 index maintenance、profile handoff 和 history cleanup ownership 接到受控管理流程；
- 接入 demo 所需的真实 adapter，并完成从创建 run 到实时查看、结果、历史、取消和保存恢复的端到端验收；
- 对第一版的 OpenAPI、SSE raw frames、错误码、前端消费者、smoke、启动说明和实际运行路径做最终一致性校对。

M9 不建立 milestone-specific V2、兼容 union、临时 feature gate 或第二套读取模型。`backend_bk/`、`frontend_bk/` 和 `docs/backend_refactor_contract.md` 是历史归档，不是 M9 的实现、交互或测试 oracle。

## 2. 当前仓库事实

- 当前正式实现根为 `backend/`；仓库尚无可复用的正式新前端工程，M9 前端需要从当前契约独立建立。
- 当前 production HTTP surface 只有 health、run 创建/列表/详情、session 详情/transcript、全文、两层 SSE、run cancel 和 persistence retry；settings/keys/admin 尚无 production route。
- `AppSettings` 当前从 `WATERAGENTS_*` 环境配置加载，包含默认 IANA timezone、model catalog、provider keys、数据库和各类后端硬预算。M9 不能把全部部署级 settings 原样变成可写 UI。
- `KeyStorePort` 目前只冻结 `get/set/delete` 接缝，masked status DTO、持久化实现和公开 API 仍由 M9 定型。
- M8 已提供不公开 HTTP 的 `RAGMaintenanceOperations`：index health、generation build/retry/replace/delete、cleanup dependency inspection、history delete 和 profile handoff inspection。M9 必须在其外层补充授权边界、用户确认、幂等和可观察状态，不能复制第二套 RAG maintenance 逻辑。
- M8 production implementation 已有 Windows 本机 fake/PostgreSQL 证据；真实 embedding supplier、生产容量和 Ubuntu/macOS 同提交验证仍分别为 `not run` / `partial`。M9 不能把这些边界写成已经通过。

### 2.1 `frontend_bk/` 只读调研结论

用户于 2026-10-08 明确授权把 `frontend_bk/` 作为历史产品调研材料。该目录仍不是当前 API、状态、字段、测试或实现的权威来源；下列内容只用于判断交互取舍，不授权复制旧协议或恢复旧系统行为。

历史实现采用 Vite + React、Leaflet/react-leaflet 和 Recharts，桌面布局为固定宽度的左侧控制栏、中间地图和右侧日志/Inspector，地图底部按需覆盖 time-series panel，设置使用侧滑 drawer。根 `App` 同时持有连接、日志、拓扑、当前 Agent、结果、图表与异常状态，并消费一条 `/ws/hydro-analysis` WebSocket。

可保留为 M9 设计参考的交互思路：

- 地图作为当前 run 的主要空间入口；用户点击坐标建立 target，点击水体进入详情；
- 拓扑出现后自动 fit bounds，节点状态有图例，水体执行中有明显反馈；
- 左侧集中组织 query、target、日期和开始/停止动作；
- 右侧为所选水体提供结果与过程上下文，技术内容按语义折叠；
- 设置按模型选择、secret 状态和其他管理类别分组，secret 输入使用 password control，读取只显示 masked status；
- 地图无商业 tile key 时可回退到 OpenStreetMap，避免把地图可用性绑定到一个 secret。

旧实现中不得复制到 M9 的路径：

- 单一 WebSocket 同时承担命令、run 状态、Agent 过程与结果；M9 必须改为 create/cancel HTTP、run SSE、按需 session SSE 和统一 history/read API；
- 把 API key、facility types、topology depth、provider plan 与采样频率随分析请求发送；当前 create contract 禁止这些客户端权限；
- 浏览器根据本机日期自行判定 analysis mode/prediction boundary；当前 timezone、mode、window 与 context 由后端固化；
- 将 event 追加成自由日志列表并以数组位置作身份；当前页面必须以 run/session/item/operation/SSE cursor 的稳定身份归并和续传；
- 直接展示 full prompt 与 raw model output；当前只展示已注册的安全 message/tool/evidence/decision/result，不公开隐藏 reasoning 或 provider raw response；
- 在前端用 `value * (1-confidence)` 人工生成 confidence band，或从旧 result 字段重算总体含义；M9 只能展示后端定型的结果、confidence、availability、risk 和引用；
- 使用 `data/user_keys.json` 明文式持久化描述、session key override 和任意 provider/model surface；M9 secret 必须走新的受控 store/API，model catalog 由 D16 的后端 allowlist 管理；
- 用 target/inlet 身份覆盖执行状态颜色，或只显示已有 session 的节点；当前 run snapshot 需要覆盖 BFS 范围、参与/未参与、未开始、运行、完成、失败及后续定型的取消展示；
- 固定 `320px + map + 360px` 且没有窄屏布局、没有 route-level history/settings/admin、没有自动化前端测试。M9 需要独立的信息架构、响应式降级和契约测试。

## 3. 已继承的硬约束

### D01：新前端只消费当前唯一生产契约

新前端以 M9 实现时的 runtime OpenAPI、M3 两层 SSE 文档和 M4–M8 typed contents 为事实源，不复制或迁移旧前端字段、WebSocket 事件、状态模型与接口行为。`frontend_bk/` 只可按 D18 保留已确认的视觉布局和操作位置，不成为业务契约或测试 oracle。

普通 HTTP DTO、SSE frame 和历史读取复用同一组业务身份；前端不得为 live、history 或 M1–M8 阶段接口维护兼容数据模型。

### D02：实时页面只使用既有两层 SSE

run 页面持有一条轻量 run notification stream；用户打开某个未结束 Agent 的过程时，再持有一条该 session 的 process stream。切换水体只更换 session stream，离开 run 页面才关闭该页面持有的流。关闭连接不取消分析。

两类流由同一个 fetch-based SSE client 消费，但分别处理：

- run 重连取得最新完整轻量快照，不要求补播所有中间状态；
- session 重连按该 session 的整数 `Last-Event-ID` 补齐并去重；
- `stream_end` 表示该流正常结束，不代表业务成功或全部内容已经保存；
- `410` 转向严格 history，不能无限重连；
- GET 用于初始加载、断线核对、详情、全文和 history，不持续轮询 run detail。

不得新增第三条 SSE、单独 reasoning stream、direct model/tool stream 或旧 `204` 正常结束兼容分支。

### D03：消息、Agent、run 与保存状态彼此独立

`message_started/message_delta/message_finalized` 更新同一个 `item_id`，SSE id 只用于传输去重。消息的 `generating/completed/incomplete`、Agent session 的五态、run 终态和 persistence 状态必须分别展示，不能互相推断。

前端不得把消息定稿、session `completed`、`stream_end` 或 HTTP `202` 显示成“整个分析已经完成并保存”。取消后的 late output 不拼回当前消息。

### D04：用户只取消整个 run

首版不提供单 Agent cancel API，也不允许用户向 Agent 追加消息。停止按钮只调用 run cancel；后端负责停止后续调度并协作取消所有未终态 session。关闭或切换当前查看的 session stream 不触发 cancel。

### D05：创建分析的用户输入范围保持收敛

创建 run 的前端输入只包括：

- 自然语言 query；
- 可选地图 target（COMID/坐标）；
- 可选日期范围；
- `auto/6h/12h/24h` resolution preference；
- parser model 与 waterbody model 的 provider/model identity；
- `stateless/memory_augmented`。

前端不提供参数选择器，不提交 API key、mode、ParameterKey、provider plan、station、topology limits 或工具权限。模型身份必须来自后端按 role 过滤的 catalog metadata；后端在创建时再次校验并固化 snapshot。

### D06：地图与 Agent 上下文使用后端真实身份

run 页面展示 BFS 范围、参与状态、真实 session 状态和结果引用。水体 process/detail 只能从 run detail 中的 `run_id + session_id` 定位，不能按 COMID 猜测或另建第二套读取入口。未创建 session 的水体不补造空会话。

用户选中水体后可查看分窗口结果、逐窗口综合风险、日期范围总体风险、availability、理由和真实 message/tool/evidence/decision/result 上下文。`run_summarizer` 是独立且不绑定 COMID 的 session；其失败只展示后端安全错误与仍可用的水体结果，前端不得生成 fallback 总结或请求隐藏思维链。

skill/tool/hook 等内容按语义默认折叠，与正文是否 inline、是否已下载或大小无关。大正文通过既有稳定 `run_id + item_id` reference 按需读取。

### D07：历史以 run 分页、以 session 阅读

history 列表的分页对象是 run，只使用既有 `limit/cursor`；前端不解析后端 cursor，也不依赖尚未提供的关键词、状态或日期筛选。

进入某次历史 run 后，用户先选水体或 summarizer session，再读取该 session 的完整有序 transcript。history 全链路保持 `view=history`，不以 live memory 补齐；相同 query、COMID 或正文不能合并不同 run/session/item 身份。

### D08：M8 历史证据只作为上下文展示

`memory_augmented` 只表示 waterbody Agent 可以显式调用历史工具，不保证自动搜索或必有命中。前端必须区分：正常 empty、index/embedding unavailable、invalid/corrupt source 与真实 historical hit。

历史卡片保留原 run/session/result、COMID、原时间、schema、confidence/quality、matched strategy、score components 和 provenance，并明确标注 `historical/contextual_only`。`rag_score` 只能表示检索相关性，不能显示成水质置信度、风险分或科学优先级。

前端不提供自由 RAG 搜索、source run/item、半径、hop、embedding 或 direct reindex 输入。打开原事实时只使用后端返回的原 run/item 组合。

### D09：设置变更不改写既有分析事实

默认 IANA analysis timezone 的变更只影响之后创建的 run；已有 run/session/configuration snapshot、日期边界、结果和 history 保持不变。

parser/waterbody model、memory mode 和 resolution 都在 run 创建时固化。普通 settings 变更不能修改在途或历史 run，也不能提高 server hard limits 或改变既有 evidence/tool authorization。

### D10：secret 只写入专用边界，公开面只显示 masked status

API key 不进入普通 settings DTO、run create body、transcript、SSE、history、日志或错误。secret endpoint 支持保存与清除；读取只返回是否已配置、适用 provider/用途和安全 masked status，不回显原值。

admin cleanup 永远不删除 API keys，也不能用“清理完成”暗示 credential 已被移除。

### D11：单位变更区分单次 override 与全局 policy

自定义单位先经过确定性解析与量纲校验，Agent 只判断该单位是否适用于当前参数。默认行为只生成当前 session/run 的 override；写入全局目标单位 policy 必须由用户显式确认并保存版本记录。

前端或 Agent 都不能提供自由换算公式、绕过 capability registry，或用显示单位改写原 source value、provenance 和历史事实。

### D12：RAG profile 变更是受控 handoff workflow

embedding setting 表达 desired profile，不是每次 run 的可选项。变更顺序固定为：保存 desired profile，构建并验证新 generation，原子激活，等待旧 reader drain/retention，最后才允许清理旧 credential 或 retired generation。

building/active/failed/reader-drain/credential-cleanup-ready 必须可观察。切换前旧 active search 保持可用；失败保留旧 active。profile 变更与 reindex 不改写既有 run/session/transcript。

### D13：index cleanup、history cleanup 与 credential cleanup 是三种不同动作

- index-only cleanup/reindex 只处理可重建的 inactive derived rows，不删除 source transcript；
- history cleanup 删除 source run 前必须检查 `rag_consumption_links`；若被 retained run 消费则稳定 conflict，只有用户显式确认才允许 consumer-first 的 whole-dependent-run 级联；
- credential cleanup 只在 profile handoff 明确 ready 后进行，且不属于普通 history/index cleanup 图。

任何删除都不得改写 retained transcript、留下悬空 historical reference 或把清理 index 表述为删除用户历史。

### D14：第一版 demo 明确是单进程、单 worker

启动说明、部署示例、UI 状态和验收不得暗示已经支持共享 live state、跨进程 cancel、多 worker pub/sub、复杂恢复或多租户隔离。

### D15：第一版是本机单用户应用

M9 第一版的产品和部署边界是本机单用户：前端、后端与用户在同一台受信任机器上使用，默认只监听 `127.0.0.1`。本阶段不引入账号体系、多租户、远程管理员、组织级权限或把 admin surface 暴露到局域网/公网。

该边界不等于“管理写操作无需保护”。settings、secret、cleanup、profile handoff 等写操作仍须采用同源前端、严格 JSON/DTO、受控 action、显式确认和浏览器跨站请求防护；具体机制在 M9 implementation/API 设计中冻结。后端不能因为是本机应用就允许任意 origin、GET 写操作、query-string secret 或无确认的破坏性调用。

若以后改为远程或多人部署，必须先实现 Future security contract 中的认证、授权、租户隔离、限流和审计，再开放对应监听地址；不能把本机 admin API 直接当作共享部署 API。

### D16：模型目录由后端配置，UI 只选择已登记能力

第一版的 provider/model catalog 继续由后端部署配置定义，并由现有 typed `ModelCapability` 校验角色、token limits 和 embedding profile。M9 前端不得创建任意 provider、填写任意 base URL、声明自定义模型能力或修改 server hard limits。

设置页只显示后端返回的安全 catalog metadata，并允许用户从对应 role 的 allowlist 中选择默认 parser model、默认 waterbody model 和 desired embedding profile。创建 run 时仍只发送已登记的 parser/waterbody `provider + model` identity；后端再次按 role 校验并固化到 run configuration。

API key 通过独立 secret surface 保存，不属于 model DTO。provider base URL、模型 capability、context/output limits、embedding dimensions/distance/template identity 等部署级字段保持环境/部署配置所有权，不进入普通可写 settings API。

catalog 变更需要服务端配置校验和重启，不伪装成 UI 热更新。default selection 的运行时持久化、secret 生效时机与既有 run 隔离语义由后续 M9 决策继续冻结。

### D17：运行时设置和 secret 只影响之后创建的 run

default timezone、default parser/waterbody model、default memory mode、default resolution 及 provider secret 保存成功后，立即成为后续新建 run 的候选默认值或 credential 来源。已经创建的 run 继续使用创建时固化的 configuration snapshot 和 credential lease，不因后续设置保存、密钥替换或清除而在执行中途切换。

前端默认值只负责预填；create request 仍显式提交 parser/waterbody model、memory mode 和适用 resolution preference，后端以保存瞬间的 server defaults/secret version 完成校验与运行时组合。run 的公开 configuration 不包含 secret value、masked secret、credential version 或内部 lease identity。

清除 secret 后：

- 尚未创建且需要该 credential 的新 run 必须在启动外部调用前安全失败或显示 provider unavailable；
- 已创建 run 保留其进程内 credential lease，继续遵守原 timeout、cancel、terminal 和审计规则；
- 已经发出的外部请求不会因 secret row 删除被伪装成已取消；
- 进程重启不恢复在途 run，仍按既有 `service_interrupted` 契约收敛，因此不需要把明文 credential 放入持久化 run snapshot。

非 run 的 background operation 在每次 attempt 开始时取得一次 credential lease；该 attempt 中不切换，之后的新 attempt 才读取最新 secret 状态。embedding profile handoff 继续受 D12 的旧 credential cleanup readiness 约束，不能用普通 key clear 绕过 active generation 的可用性门禁。

### D18：旧前端只提供视觉参考，不形成兼容关系

M9 新前端和 `frontend_bk/` 所属旧系统是两套相互独立的系统。M9 不是旧系统的升级、迁移或兼容版本，不承诺复用旧代码、旧状态、旧 API、旧数据、旧 URL、旧构建产物或旧部署方式，也不需要为旧系统提供迁移适配层。

旧 UI 的价值仅限于视觉和操作习惯参考。为了降低熟悉旧界面的用户重新学习的成本，M9 桌面端优先沿用三栏工作台及相近视觉层级，但不要求逐像素复刻。只要不违反当前 API、安全、状态和结果契约，如果局部排版能明显改善信息层级、可读性、无障碍或窄屏体验，可以调整；不应无理由同时移动多个核心区域或让高频操作失去原有位置线索。

桌面主界面保留：

- 左侧固定控制栏、中间主地图、右侧详情栏的空间关系和相近宽度比例；
- 左上品牌/header，设置齿轮仍位于控制栏 header 右侧；
- target/map point、query、模型摘要、日期、状态、Start、Stop 和使用提示从上到下的主要顺序；
- 地图右上角图例、选点 marker、河网边、水体圆形节点、运行中反馈和点击水体查看详情；
- 右侧 header、三个 tab 的位置和可滚动内容区；具体标签按 D29 定为 `Overview / Agent / Evidence`，不再展示自由日志或 raw model data；
- 选择有结果的水体后，time-series 仍可从地图底部的可关闭区域展开；它只能画后端提供的 series/interval/quality，不在浏览器生成 confidence band 或科学结论；
- settings 继续从原齿轮打开左侧 drawer。日常 defaults、模型选择和 keys 保留分组位置；破坏性 Admin 不直接混入普通设置区，而由 drawer 内稳定的 Advanced/Admin 入口进入独立管理视图。

在不移动主要操作的前提下增量加入：

- 左侧控制栏在同一位置提供 `New Analysis / History` 切换；New Analysis 保留原表单顺序，History 替换控制栏 body，不覆盖地图和右侧详情；
- 当前 create contract 需要 parser model、waterbody model、memory mode 和 resolution，使用旧模型摘要/设置位置承载，不恢复 facility、topology depth、session key override 或客户端采样权限；
- 右侧以真实 run/session/item 身份展示 run summary、水体结果、Agent message/tool/evidence/decision/result 和 M8 historical context；
- persistence warning/retry、partial coverage、historical empty/unavailable/invalid 和 cleanup disappearance 使用现有状态区域、右侧内容区或专用管理视图，不另造全屏主流程。

视觉参考不等于复制旧组件，也不等于冻结旧排版缺陷。M9 应建立新的 typed client/state boundaries；可由设计人员参考旧系统的 logo、颜色、间距和图标位置，但生产实现不依赖旧系统源码。局部改良需要能够说明解决了什么问题。M9 自己建立 screenshot/visual regression 基线用于防止后续 M9 改动漂移，不把与旧系统逐像素一致作为验收门禁。

窄屏没有既有稳定操作习惯，可以将三栏降级为 tabs/drawers；桌面布局仍是第一版 demo 的主要验收面。无障碍、键盘焦点、错误提示和响应式修复不视为破坏旧 UI。

### D19：新前端的唯一生产目录是 `frontend/`

M9 新前端必须在仓库根目录的 `frontend/` 中建设。`frontend_bk/` 只作为只读的视觉与交互参考，不作为新前端的源码目录、构建输入、运行时依赖或渐进修改目标。

后续设计、实现、测试、构建和部署命令都以 `frontend/` 为准。新前端可以参考旧界面的可见设计，但不把迁移旧组件、状态逻辑或样式源码列为实施工作；不得通过 import、软链接、复制脚本或运行时 fallback 依赖 `frontend_bk/`。

### D20：日常删除、批量清理和索引维护必须分开

History 只提供单个 run 的删除入口。删除前先展示实际影响；如果 M8 historical context 或 generation 引用使其需要级联删除依赖 run，必须明确列出影响并进行第二次确认，不能把级联效果藏在普通删除按钮后面。

批量或全量 history cleanup 只放在 Advanced/Admin 独立管理视图：先执行只读 preview/impact inspection，再凭后端签发且绑定当前影响集合的确认信息执行。日常主界面不得提供靠近常用按钮的“一键清空全部”。

index generation 的构建、切换、退役、清理和重建位于独立管理卡片，不与 history cleanup 共用动作或含糊的 `Clear data` 入口。删除历史记录和维护派生索引是两个不同操作，前端与 API 都必须让用户看得出区别。

### D21：`frontend/` 采用独立的 React + TypeScript + Vite 客户端

M9 在 `frontend/` 中新建 React + TypeScript + Vite 应用。地图能力可选用 Leaflet 生态，但依赖版本、组件边界和状态模型由 M9 自己定义，不以 `frontend_bk/` 的 package、组件或 hook 为实现起点。

前端通过 REST 创建 run、读取历史与详情、修改 settings/keys，以及发起受控的 Admin operation；实时进度只消费当前后端定义的 run SSE 和 session SSE。不得恢复旧系统的单一 WebSocket 命令/状态协议，也不得增加与当前后端平行的兼容 API。

### D22：开发期使用代理，正式运行由 backend 同源托管

本地开发允许分别启动 Vite development server 和 backend。`frontend/` 的开发服务器将 `/api` 及 SSE 请求代理到 backend，使前端代码始终使用相对 URL，不把开发端口或 backend origin 写入业务组件。

正式构建由 backend 在同一 origin 下提供 `frontend/` 的静态产物和 API；用户只访问一个本地地址。前端路由的非 API GET fallback 到应用入口，未知 `/api` route 仍返回 API 404，不能被静态入口吞掉。SSE 经过同源路径时必须保持流式传输，不得被静态托管或代理层缓冲为完整响应。

第一版仍遵守 D15：默认仅监听 `127.0.0.1`。开发代理和同源托管不构成允许远程访问、关闭写请求来源校验或扩大信任边界的理由。

### D23：secret 按本地单用户应用的最低充分方案持久化

通过 UI 保存的 provider API key 在应用重启后仍应可用，原值写入操作系统提供的用户级安全凭据库；PostgreSQL 或普通配置只保存 provider identity、是否配置、更新时间、masked status 所需的非敏感 metadata，不保存可还原的 secret value。

第一版只实现一个窄的 `KeyStorePort` adapter 和测试 fake，不引入云 KMS、Vault、自建加密服务、后台轮换任务、多用户 ACL、组织级审计或跨机器同步。不得为了“可移植”自行把加密主密钥和 ciphertext 放在同一个本地文件中，也不得降级到 `frontend_bk/` 描述的明文 JSON key store。

如果当前操作系统的安全凭据库不可用，应用仍可使用启动环境中已有的 deployment-managed secret；UI 保存/替换能力明确显示 unavailable，不静默写明文文件。环境提供的 secret 只能显示 `configured` 和来源类别，不能通过 UI 读取或删除；UI 清除只删除由本应用写入凭据库的值。具体凭据库 library 与 Windows/macOS/Linux backend 选择属于实施细节，但必须通过 `KeyStorePort` 隔离并可用 fake 测试。

### D24：mutable defaults 使用单记录和乐观版本，不建设配置历史系统

M9 可由用户修改的普通默认值保存为 PostgreSQL 中的一条 typed current-settings record，至少包含当前 schema version、递增 revision 和更新时间。第一版不实现配置历史、差异审计、回滚、定时生效或多用户 ownership。

读取 settings 返回完整的可写 defaults、当前 revision，以及后端 catalog/hard limits 提供的只读选择约束。保存时提交完整的可写 defaults 和读取到的 revision；只有 revision 仍匹配才原子替换并递增。若另一个页面已经先保存，返回稳定 conflict，前端提示刷新后重新选择，不做字段级自动合并。

数据库没有记录时，从经过验证的 deployment settings 生成初始默认值；首次保存后以数据库记录作为 mutable defaults 的权威来源。model catalog、provider base URL、capability、hard budgets、数据库连接、监听地址和安全边界继续由 deployment settings 拥有，不能写入该记录。无数据库连接时可以构造应用和读取 deployment defaults，但 mutable settings 的读取/保存明确 unavailable，不伪装成已持久化。

### D25：第一版 UI 只管理 OpenAI 与 Google Geocoding credential

M9 secret allowlist 只包含当前需要由本地用户配置的两项：

- OpenAI credential：供 query parser、waterbody reasoning 和当前 embedding adapter 使用；
- Google Geocoding credential：供地点解析 adapter 使用。

前端从后端返回的 typed allowlist 渲染固定用途的 credential rows，不接受任意 key name、provider name 或 base URL。地图默认使用无需用户 secret 的 OpenStreetMap tile source，不增加 Google Maps credential。

USGS 当前公共数据路径不要求用户 credential，因此不进入 M9 key UI、masked status DTO 或可写 secret API。现有可选 `usgs_api_key` 仍可保留为 deployment-only setting，供特殊部署通过环境配置；它不应因为存在于 `AppSettings` 就自动暴露给浏览器。同理，当前生产链未使用的 Grok 等旧系统 provider 不进入 allowlist。

### D26：地图节点状态保留五态基线，并明确表达取消

地图节点必须使用 backend topology、run snapshot 和真实 session status 映射状态，不根据日志文字、是否收到消息或前端计时猜测：

| 状态 | 视觉 | 语义 |
|---|---|---|
| BFS 范围内但不参与 | 半透明灰色 | 拓扑可见，但不属于本次实际推理集合 |
| 参与但尚未开始 | 实心灰色 | 已纳入本次运行，仍在正常等待调度 |
| 正在推理 | 蓝色节点 + 轻微呼吸外圈 | 对应 session 正在执行 |
| 已完成 | 绿色 | 该 session 已完成；之后取消整个 run 也不覆盖这个终态 |
| 失败 | 红色 | 该 session 真实失败 |
| 已开始后取消 | 橙色 | 已有 session 且真实终态为 `cancelled` |
| 因 run 取消而未执行 | 灰色节点 + 虚线外圈/停止标记 | 原计划参与，但取消使其未创建或未开始；不得显示为失败或伪造 cancelled session |

蓝色呼吸效果只作用于节点外圈，以低频 opacity/scale 变化表达活动，不让节点本体、标签或整张地图闪烁。第一版不增加“减少动态效果”模式；视觉回归测试关闭或冻结动画，避免截图时序导致不稳定。

颜色不是唯一信息来源。legend、tooltip/详情和可访问文本必须同时给出状态标签；“正常等待”和“因取消未执行”虽然都使用灰色，仍通过实心/虚线或图标与文字区分。已经完成、失败或取消的 session 保留自身终态，run 后续状态不能批量重染所有节点。

### D27：单位 override 默认限于单次分析，全局默认必须另行确认

用户在新分析 query 中提出的目标单位只形成该 run/session 的 override。确定性 unit tool 负责解析单位和校验量纲，Agent 只判断该参数是否适用；无法识别、量纲不兼容或参数不适用时返回明确限制，不猜测转换，也不修改全局配置。

Settings 可展示后端 unit registry 提供的兼容单位选项。将某个目标单位保存为以后新分析的全局默认时，必须先显示受影响的参数/量纲并由用户显式确认；确认后写入带 version 的全局目标单位表。第一版不接受任意 conversion formula、客户端自定义倍率或无量纲校验的自由文本全局规则。

全局单位变更只影响之后创建的 run/session。历史记录继续按创建时固化的 unit policy snapshot 展示，不因当前 Settings 改变而重算、重标或重写已有 transcript/result；前端也不能仅为显示方便偷偷把历史值换算成当前全局默认。

### D28：时区按 run 固化，历史界面必须明确显示原时区

Settings 提供一个默认 analysis timezone，只接受后端校验通过的 IANA timezone identity。创建新 run 时将当时的默认时区固化进 configuration snapshot；之后修改默认时区只影响未来 run，不改变已经创建的 run/session。

历史列表、run 详情及涉及日期/时间的结果区域必须明确展示该次分析保存的时区，例如 `Analysis timezone: America/Chicago`。具体时间点除 IANA identity 外可同时显示当时适用的 UTC offset，以正确表达夏令时，但不能只显示模糊的 `local time`、浏览器时区缩写或当前 Settings 值。

前端不得根据浏览器/操作系统时区自动替换 analysis timezone。查看历史时必须使用并显示该 run 的原 timezone snapshot；若旧数据缺失该字段，应显示明确的 legacy/unknown 状态，不能拿当前默认值补写或假装它就是原时区。

### D29：右侧详情栏使用 Overview、Agent、Evidence 三个稳定 tab

右侧继续保留三个 tab 的熟悉位置，但不继承旧系统的自由日志模型：

- `Overview`：显示 run 状态、后端生成的浅显 run summary、partial/失败提示、特殊值和 persistence warning/retry；不得由前端模板伪造总结；
- `Agent`：显示当前选中水体的自身状态、分窗口/总体结果以及真实 message/tool/decision/result 过程；running session 可在此实时追加，已结束与历史读取保存内容；
- `Evidence`：显示当前选中水体或记录关联的 evidence、source attribution 和 M8 historical context；tool/skill/hook 与大正文等细节默认折叠，按需展开。

没有选中水体时，`Overview` 仍可工作；`Agent` 和 `Evidence` 显示明确的选择提示，不混合展示全 run 中多个 session 的过程。选择水体不会强制切换用户当前 tab，但当前 tab 内容必须同步到新的真实 run/session/item identity。

异常、限制和 unavailable 原因放在它所影响的 summary、result、evidence 或 persistence 区域附近，不设置一个经常为空且脱离上下文的独立 `Anomalies` tab。红色失败状态不等同于“存在统计异常”，前端不得根据名称把二者合并。

### D30：耗时 Admin 动作使用可恢复查看的异步 operation，不新增 SSE

generation build/reindex、批量 history cleanup 等不能保证在一次短 HTTP 请求内完成的动作，在 preview/确认后创建 backend-owned async operation。创建接口返回 `202` 和 typed operation snapshot；operation identity、kind、safe request summary、state、progress counts、安全错误码及时间写入 PostgreSQL，secret、完整 preview payload 和 provider raw response 不进入记录。

Admin 页面打开且 operation 未终态时，前端通过对应 GET endpoint 进行有界状态查询；页面隐藏或离开后停止查询，但不取消后端任务，重新打开可按 operation identity 恢复查看。M9 不为 Admin 增加第三条 SSE，也不把 operation 塞进 run/session SSE。

相同 resource/kind 已有活动 operation 时，重复提交不得并行启动副本；后端返回稳定 conflict/现有 operation identity，前端显示“正在执行”并转到已有状态。会互相破坏前置条件的不同操作也必须由后端拒绝，而不是只在按钮上做禁用判断。

服务重启后，operation 必须从持久化事实判断实际状态；不能把失去进程内执行者的记录继续显示为 running。底层 M8 job 能安全续接时由其既有 reconciliation 继续；否则 operation 收敛为明确的 interrupted/failed 状态并允许用户重新 preview 后重试，不宣称自动成功。

### D31：删除 run 历史后保留最小 idempotency tombstone

history cleanup 删除某个 run 时，不释放其已经使用过的 create `Idempotency-Key`。原幂等记录在同一数据库事务中收敛为最小 tombstone，只保留识别旧提交所需的 key identity、原始 request bytes fingerprint、已删除 run identity、deleted state 和时间；原创建回执、query/body、session/transcript/result 和其他可恢复业务内容必须删除。

删除后再次使用旧 key：

- request bytes fingerprint 与原提交相同，返回 `410 idempotency_resource_deleted`，不创建 run，也不返回已删除内容；
- fingerprint 不同，继续返回稳定 `409 idempotency_key_conflict`；
- 用户主动重新分析时，前端生成新的 UUID v4 key，按正常 create 流程建立新 run。

tombstone 不因普通 history/index/evidence cleanup 自动过期，也不被 live-state 清理删除。它不是可读历史条目，不出现在 History 列表；cleanup preview/完成结果只需分别报告 deleted run 数和 retained idempotency tombstone 数。这样可以防止迟到的网络重试在用户删除历史后静默重新执行。

### D32：真实外部调用必须由用户动作触发，默认测试不产生费用

保存/替换 OpenAI 或 Google Geocoding credential 只写入安全 store 并更新 masked status，不为“自动验证”调用供应商。应用启动、打开 Settings/Admin/History、读取 catalog、普通 health check、前端构建和默认自动化测试都不得触发真实 provider 请求。

只有下列明确动作可以产生真实外部请求：

- 用户点击 Start 创建并运行真实分析；
- 用户在 credential row 明确点击 `Test connection`，并在按钮附近看到该动作会联系外部供应商、可能产生用量；
- 开发者显式启用独立的 real-provider smoke 命令。

`Test connection` 只验证该 allowlisted credential 对应的最小固定能力，不接受任意 URL/model/payload，不保存供应商 raw response，也不能被页面加载或 key save 隐式调用。测试连接的成功不冒充完整分析、RAG generation 或容量验收通过。

默认 backend/frontend/E2E suite 使用 deterministic fake 或受控 fixture，必须在无真实 key 和无公网时可运行。real-provider smoke 默认关闭，使用单独的显式开关、严格的最小请求上限和清晰证据标签；未执行时记录 `not run`，不能由 fake test 代替。

### D33：当前没有持久化 evidence cache，不提供虚假的清理按钮

当前 production backend 没有跨 run、落盘并持续增长的 evidence cache：M5 exact-request reuse 只存在于同一 run，随既有 run live-resource cleanup 清除；M8 query embedding reuse 只存在于一个 sealed session search 实例，session 结束时清除。`CachePort` 接缝和旧系统曾存在的 disk cache 描述不能当作当前已有存储。

因此 M9 第一版 Admin 不提供 `Clear evidence cache` 按钮或无实际效果的 cleanup API，也不把 transcript 中的 evidence/source facts 错称为 cache。若 storage/status 页面列出资源，可将 evidence cache 表达为 `not_present`，但不能显示伪造的字节数、last cleared 或 success。

未来 milestone 若增加跨 run 持久化 evidence cache，必须在同一变更中定义 owner identity、TTL/freshness、source/transcript 引用、清除范围、在途 reader/worker 行为、重建方式和 Admin preview/confirmation，再增加真实按钮和 API。该未来 cache 仍不得把 stale data 当作 provider failure fallback，也不得被 history/index cleanup 顺带含糊删除。

### D34：自动 fake 验收与真实 provider 验收分别记账

M9 的默认自动化验收使用 deterministic fake/fixture 完成从新建分析、两层 SSE、地图/Agent/结果、历史读取、settings/keys/admin 到 cleanup 的完整用户流程；该流程必须离线、无真实 credential、无付费流量并可重复运行。

真实 OpenAI/Google adapter 需要进入 production composition，但真实供应商 smoke 是单独显式启用的最小验收通道。证据必须分别记录 provider、实际动作、时间、边界和结果；缺少 key、网络或用户授权时标记 `not run`，失败则按真实失败报告，不能用 fake、fixture、mock server 或 `Test connection` 替代完整 real-provider smoke。

实现/文档可以在 production adapter 已接线且默认自动化门禁通过后报告其准确范围，不以不可用的外部条件伪装阻塞所有本地工作；但只要真实 smoke 未运行，就不得宣称真实 provider 已验证、demo 已通过真实外部链路或具备生产容量。容量/稳定性验证也单独记账，不由一次最小真实请求推断。

### D35：第一版交付本地 Web 应用，不建设桌面壳

M9 第一版的最终用户形态是 browser-based local application：一个 backend 进程默认监听 `127.0.0.1`，同时提供 production frontend 静态产物和 API，用户访问一个本地 URL。正常使用不需要另外启动 Vite；Vite development server 只属于开发工作流。

第一版不实现 Electron/Tauri、原生桌面窗口、安装包、自动更新器、系统托盘、后台系统服务或浏览器扩展，也不把本地应用包装成远程多用户站点。启动说明提供明确的一条主启动路径、依赖/数据库前置检查和本地 URL；是否自动打开浏览器属于非必需便利功能。

该交付形式不削弱浏览器边界：写操作仍执行同源/来源校验，secret 不进入前端 bundle/localStorage/query string，静态资源 fallback 不吞掉 API 错误。未来若增加桌面壳或远程部署，必须重新评估 secret store、CSRF/origin、进程生命周期和更新信任边界，不能直接复用“仅本机”假设。

## 4. 决策落点

实现者不得只读取本文后自行补空白。已确认的细节按下列权威拆分：

| 主题 | 权威文档 |
|---|---|
| 页面/URL/三栏布局、地图、Overview/Agent/Evidence、History、Settings/Admin 交互 | [Frontend Experience](m9_frontend_experience_design.md) |
| mutable/deployment settings、unit policy、secret、preview/confirmation、cleanup/profile handoff policy | [Policy Catalog](m9_settings_admin_policy_catalog.md) |
| route、DTO、HTTP status/error、幂等、operation polling、OpenAPI | [Settings/Admin API](m9_settings_admin_api.md) |
| 模块、migration、OS key store、per-run runtime、static hosting、测试与发布门禁 | [实施设计](m9_implementation_design.md) |
| coding agent 分阶段工作、focused tests、命令、阶段出口、提交边界与 contract matrix | [Implementation TODO](m9_implementation_todo.md) |

仍由实现者在上述约束内决定的仅是普通局部细节，例如组件拆分、CSS 命名、具体 locked package patch version、内部 helper 命名和测试 fixture 组织；这些选择不得扩大公开 API、可写 settings、provider allowlist 或信任边界。

## 5. 文档集与状态

- `m9_settings_admin_demo_design.md`：本文；产品、范围与跨域架构决定；
- `m9_frontend_experience_design.md`：页面信息架构、状态映射、实时/历史交互与展示规则；
- `m9_settings_admin_policy_catalog.md`：可变设置、secret、确认、cleanup、profile handoff 与安全 policy；
- `m9_settings_admin_api.md`：settings/keys/admin DTO、HTTP/error/async operation 契约；
- `m9_implementation_design.md`：模块、持久化、前后端组合、测试和发布门禁；
- `m9_implementation_todo.md`：Milestone 0–9 施工清单、验证命令、阶段出口、提交边界和 contract matrix。

上述设计与 TODO 均已冻结，但 production implementation 尚未开始。TODO 的全部复选框保持未勾选；不得把文档完成项标记成代码、联调或验收完成项。
