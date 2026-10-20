# M4 Query Understanding + Hydrology Topology 设计决策记录

更新日期：2026-09-27

> 状态：Milestone 0–10 已完成本机实现与集成审计；M4 仅交付 query/context/topology 内部切片，真实 observation/evidence、reasoning、调度、memory 和新前端仍分别由 M5–M9 承接。精确类型、API 示例与实际证据见同目录实施设计、API 文档和 Implementation TODO。

本文承接：

- [后端架构骨架](../backend_architecture_skeleton.md)
- [总 Milestones 的 M4 范围](../backend_refactor_milestones.md#6-m4-query-understanding--hydrology-topology)
- [跨 Milestone M4 契约](../deferred_milestone_contracts.md#6-m4query-understanding--hydrology-topology)
- [M3 实施设计的后续接力](../M3/m3_transcript_history_implementation_design.md#12-完成标准与接力)
- [M4 Implementation TODO](m4_implementation_todo.md)

## 1. 当前边界

M4 负责把一次用户请求转换为经过校验的结构化分析上下文，并从目标 COMID 向上游构建可供后续 evidence 与 reasoning 阶段使用的水文拓扑。问题解析 Agent 是 run 下不绑定 COMID 的真实 session；BFS、范围判定和拓扑构建是 run 的确定性过程，不另建 Agent session。

M4 必须复用 M3 已有的 run/session 生命周期、四表、writer、typed-content registry、统一读取和两层 SSE。不得另建 transcript、历史、补写或事件系统，也不得在发现水体或生成 DAG 时预建水体 session。

`backend_bk/` 与 `frontend_bk/` 是完全独立、最终丢弃的归档系统。设计、实现、测试、评审和验收 M4 时不得读取、导入、复制、适配、枚举或测试其中任何代码、服务、接口、数据模型、依赖、配置、缓存或行为；未来新前端也只依据届时确认的产品设计和新后端契约独立实现。新设计只以当前 `backend/`、本文和 `GLOBAL-C01`–`GLOBAL-C11` 为准。

## 2. 已确认决策

### D01：可覆盖自然语言的业务字段只有地图目标与日期，并具有最高优先级

前端用于确定用户业务对象的结构化字段是：

- 用户在地图选择产生的 COMID 和/或坐标；
- 用户选择的日期范围。

前端不提供水质参数选择表单。用户需要分析的参数必须由任务解析 LLM 从自然语言 query 中推断。

前端还会提交粒度、parser/waterbody 模型选择和 memory mode，但这些是执行配置，不从自然语言 fallback；其规则分别见 D12、D13、D18 和 D21。

前端已提供目标或日期时，以该结构化选择为准；对应字段未提供时，才允许使用自然语言解析结果。结构化输入与自然语言出现明显冲突时不得静默覆盖或任选其一，而应停止并提示用户确认。COMID、坐标在最终 request 中的精确组合和一致性校验见 API 文档。

### D02：LLM 提取候选意图，确定性 resolver 拥有最终裁决权

任务解析分成两个阶段：

1. Query Parser LLM 从 query 中产生参数语义，以及缺失时的地点、日期候选；
2. 确定性 resolver/validator 校验地点与日期，并把参数语义映射为受支持的 `ParameterKey(code_system, code)`。

LLM 不得直接创造或最终裁定 COMID、参数代码、UTC 边界、推理模式或 topology。未经原模型校验的流式文本也不能成为 `AnalysisContext`。解析 session 保存实际输入、模型过程和结构化输出，后续流程只消费通过确定性校验的同一份结构化事实。

### D03：越界、歧义或无法确认的请求在 topology 和水体 Agent 之前停止

以下情况不进入 topology，也不创建或运行水体 Agent：

- 地点无法确认或超出产品支持的美国地域；
- 日期缺失、无法确认或超出允许范围；
- 无法判断用户需要哪些水质参数；
- 参数含义存在不可接受的歧义；
- 参数不能映射到受支持的参数目录；
- 结构化输入与自然语言存在需要用户确认的实质冲突。

请求结构本身非法时可以在创建 run 前返回 HTTP 校验错误。只有创建 run 后才能发现的语义问题，必须保留 parser session、已有 transcript 和安全诊断，再以明确的解析失败结束；不能用默认地点、默认日期或默认参数继续。

### D04：M4 第一版采用“失败并提示，修正后新建 run”

第一版不在同一个 run 中进行多轮澄清，也不增加 `waiting_for_user` 一类执行状态。解析失败后：

- 保存问题解析 session 的实际过程与失败原因；
- 给前端返回可操作且不泄露内部异常的提示；
- 结束当前 run，并阻止 BFS、evidence 获取和水体 Agent；
- 用户修正输入后主动提交新的 run，使用新的创建 key。

网络重试仍遵守 M3 幂等契约；“修正后再分析”不是原请求的网络重试。

### D05：推理前必须经过确定性的 evidence sufficiency 门禁

“参数已经明确”不等于“已有足够依据调用推理 LLM”。M4 负责提供目标参数、必需支持参数、时间段和 topology 范围；M5 获取并验证 evidence 后，必须在进入 M6 水体推理前执行确定性的证据充分性检查：

- 有直接证据：允许进入推理；
- 没有直接证据，但存在策略明确允许的侧面证据：允许进入推理，同时保留证据不足事实并降低最终置信度；
- 完全没有有效证据：不得调用对应水体推理 LLM。

每个目标参数单元还必须具有 `usgs:00060` discharge 的直接或明确允许的侧面证据；目标证据或 discharge 任一完全缺失，都不得调用对应水体推理 LLM。侧面证据必须来自后续设计中明确登记的证据种类与关系，不能由 LLM 临时声称某项数据“可以作为证据”。外部服务不可用、响应损坏或超时是获取失败，不能伪装成“无数据”。

### D06：证据门禁按“参数 × 时间段”执行，允许部分继续

同一请求中的不同参数和时间段分别判断：

- 目标参数与必需 discharge 都有直接或允许的侧面证据时，该部分继续；
- 目标参数或 discharge 任一完全无证据时，该部分标记为 `insufficient_evidence`，并跳过对应 LLM 调用；
- 所有部分均完全无证据时，整次 run 在 reasoning 前提前结束并向用户提示。

该策略用于避免无依据推理和无意义 token 消耗。部分继续时的 run 最终状态、结果 DTO 和前端措辞仍需在 M5/M6 设计中定型，不在 M4 草稿中提前发明第二套状态。

### D07：上游 topology 使用逐窗候选与有界备选 BFS

拓扑从目标 COMID 开始，沿水流方向逐层向上游搜索：

- 每条汇入支流分别搜索，不能因一条支流已有证据而忽略其他支流；
- 历史 coverage unit 找到第一个实际读数候选后，再沿该路径寻找一个上游备选；预测 unit 找到首个候选即停止；
- 尚未覆盖的目标或支持参数可以继续沿该支流搜索；
- 目标水体自身已有历史候选时，也只按同样的一个备选上限继续搜索；
- 同时设置整图最多 128 个唯一节点、每条路径最多 12 hop 和累计 100 km 三重硬上限；外部查询并发最多 8，均由后端配置管理。

历史候选只证明该参数在该时间窗有实际读数，不代表整个窗口的证据充分；M5 仍检查质量、单位和时间覆盖。

### D08：保存完整 BFS 范围与排除原因，active graph 是有效路径并集

最终 `HydrologyGraph` 必须区分：

- 目标节点；
- 被选为证据源头的 inlet；
- inlet 到目标之间实际参与后续流程的 active 节点与边；
- 搜索范围内未参与的 excluded 节点及结构化原因。

active graph 是已选证据锚点到目标水体路径的并集，并始终保留 target 供 M5 查询本地/非站点 evidence；它不等于所有被探查过的水体。即使没有 qualifying inlet，target 也只表示候选分析对象，不代表已创建 session 或会调用模型。达到搜索上限、无匹配站点、参数不覆盖、超出支持范围或其他确定性原因必须可区分，不能全部压成一个布尔值或依赖日志解释。

这些 topology 与 BFS 事实属于 run 原记录，用于当前/历史状态装配；不是额外地图快照。发现节点、完成 BFS 或确定 DAG 都不得预建水体 session。

### D09：完整参数目录用于发现，能力注册表决定能否执行

M4 不使用“对完整 USGS 参数表做向量搜索并直接执行最高相似项”的模式。参数处理分成两层：

- 完整参数目录用于名称、同义表达和候选发现；
- 可执行参数能力注册表登记当前系统真正支持的分析参数，并决定候选能否进入后续流程。

Query Parser LLM 输出业务参数概念和推断理由，不直接决定代码。确定性 resolver 只把无歧义且已登记的概念映射到 `ParameterKey(code_system, code)`。能力注册项还必须关联规范单位、适用数据源与 evidence tools、允许的侧面证据关系，以及后续验收所需的参数语义。

参数目录中存在某个代码，不代表系统已经有能力正确取数、换算、聚合或推理。候选未登记时提示当前不支持；多个已登记候选仍无法消歧时提示用户重新说明，不按向量排名擅自选择。注册表可以持续扩展，但每次扩展都要同时提供能力配置、边界验证和测试。

系统可以限制一次 run 自动展开的参数数量，避免宽泛问题产生无界取数与模型调用。精确上限和第一批注册参数在后续设计中公开说明，不由 LLM 自行截断或排序后静默丢弃。

### D10：第一版地域范围限定为美国本土 48 州

第一版只承诺 conterminous United States（CONUS，美国本土相连的 48 州），不把“美国境内”宣传为阿拉斯加、夏威夷和美国领地均可用。当前 [NLDI `comid` 网络](https://api.water.usgs.gov/docs/nldi/)以 NHDPlusV2 为基础，其地域能力不能从覆盖更广的 NHDPlus HR 直接推断；不同数据产品的官方地域差异见 [USGS 对比](https://www.usgs.gov/news/which-nhd-product-do-you-need-and-which-do-you-have)。

地域与水文能力是两道独立门禁：

1. 地名或坐标必须位于 CONUS 支持范围；
2. 该位置必须能通过当前 NLDI 能力解析为有效目标 COMID。

位于 CONUS 但无法取得有效 COMID 时返回“当前区域不支持”，不能静默吸附到远处或边界另一侧的水体。前端提供 COMID 与坐标时还必须校验二者属于同一目标；不一致时停止并提示用户确认。阿拉斯加、夏威夷和领地只有在对应数据源、网络身份、适配器及验收证据单独完成后才能开放。

### D11：首期日期限定为当前日期前后各 7 天，粒度由前端表单提供

前端提供开始日期、结束日期和时间粒度表单。第一版仍只接受 date-only 范围，结束日期包含整天，并按 run 创建时固化的 session IANA timezone 转换为 UTC `[start, end)`。

首期可选日期限定在 session 当地“今天”的前后各 7 天内：最早为 `today - 7 days`，最晚为 `today + 7 days`，两个边界日期均可选择。开始和结束日期必须都在该闭区间内，且开始日期不晚于结束日期。该规则使首期只处理近期观测、补全和短期预测；更早历史查询留待后续产品与数据能力验证后再设计。

“今天”在 run 创建时从同一 `ClockPort.now()` 和 session timezone 计算并固化。执行跨过午夜不重新解释范围或改变 mode。严格早于今天的日期属于 `gap_filling`；今天及未来日期属于 `prediction`；跨越今天的请求属于 `mixed`，在当地今天 `00:00` 切分。今天已经产生的观测可以作为 prediction 的基线 evidence，但今天的输出不同时混入两种 mode。

超出允许范围时返回可操作提示并停止，不静默截短日期。前端结构化日期仍高于 query 中的日期候选；两者明显冲突时按 D03–D04 提示用户重新提交。

粒度表单支持既有 `6h / 12h / 24h` 及 `auto`；默认值和 `time_resolution_source` 规则见 D12。

### D12：粒度表单默认自动，并保留三种显式粒度

前端粒度表单提供 `auto / 6h / 12h / 24h`，默认选择 `auto`。自动规则按请求覆盖的日历日数量确定：

- 1–7 个日历日选择 `6h`；
- 8–15 个日历日选择 `12h`；
- `24h` 只在用户明确选择时使用，不由首期自动规则选择。

用户明确选择 `6h / 12h / 24h` 时，解析后的 `time_resolution_source=frontend`；用户选择 `auto` 且后端实际执行上述规则时为 `auto`。非前端 API 调用省略粒度时使用 `12h` 和 `time_resolution_source=default`。显式 `null`、未知字符串或其他类型均为请求校验错误。

`AnalysisContext` 保存解析后的实际 `TimeResolution` 和 source，不把 `auto` 当成第四种 domain resolution。15 个日历日显式选择 `6h` 会产生最多 60 个窗口，仍在首期允许范围内；日期门禁继续按 D11 执行。

### D13：前端分别选择解析模型和水体模拟模型，解析模型最多修复一次且没有工具权限

前端分别提供任务解析模型和水体模拟模型的选择。二者可以使用不同模型；选择结果在创建 run 时经过后端能力校验并固化到该 run 的配置事实，后续全程和历史读取保持不变。前端只提交已公开的 provider/model 身份，不提交、读取或回显 API key；凭据仍只从后端 secret 配置取得。

Query Parser 正常只执行一次结构化生成。如果返回内容不能通过 `ParsedIntent` schema 校验，最多允许一次携带安全校验摘要的修复调用；第二次仍不合法则结束 parser session 和 run，不继续消耗 token，也不填默认意图。模型/provider 不可用时明确失败，不静默切换到其他模型。

解析模型的调用环境不注册 geocoding、NLDI、参数目录搜索或其他工具，也不授予这些工具的调用权限，因此它不能发起 tool loop。它只产生地点、日期和业务参数概念等候选；随后由 application workflow 调用确定性的 resolver ports 完成地域校验、坐标/COMID 解析、参数能力映射与日期验证。水体模拟模型直到后续 evidence 门禁通过后才允许调用。

parser session 的 transcript 保存实际模型身份、调用次数、安全的输入/输出过程和最终校验结果；不得记录 API key、Authorization 或其他凭据。水体模拟模型的实际调用过程由后续 M6/M7 按同一 M3 记录入口接入。

### D14：站点满足参数与时间覆盖资格后才能成为 inlet

“水体附近存在某个站点”不足以停止上游搜索。M4 的低成本 availability probe 必须确认站点与当前河段的有效关联，并按本次 `AnalysisContext` 判断覆盖资格：

- 对历史 segment，站点目录或 inventory metadata 必须表明目标 `ParameterKey` 与请求日期存在覆盖；
- 对预测 segment，必须存在该参数可用的近期观测基线；
- 一个站点只覆盖部分目标/支持参数时，只停止这些参数在该支流上的搜索，尚未覆盖的参数继续向上游；每个时间段的 coverage units 必须包含 discharge；
- metadata 明确无覆盖时继续 BFS；
- metadata 服务不可用、响应损坏或无法完成必要判定时属于外部能力故障，不能当成“无数据”。

M4 的资格检查只决定 topology 候选 inlet，不把目录声明冒充实际 evidence。M5 仍须获取真实记录，校验完整性、单位、时间覆盖与质量，并执行 D05–D06 的最终 evidence sufficiency 门禁。如果 M5 发现目录与真实记录不一致，必须保留该事实，不能让水体 Agent 在无证据情况下继续。

### D15：单支流缺失可以透明降级，全部无证据才停止

一条支流自然结束但没有合格 inlet 时标记 `no_qualifying_source`；因节点数、跳数或水文距离硬上限停止时标记 `search_limit_reached`。后者表示搜索未完成，不能被报告成已确认无数据。两类节点和路径都保留在本次 BFS 范围及 excluded facts 中，但不创建水体 session。

如果其他支流、直接证据或允许的侧面证据仍能通过 D05–D06 的“参数 × 时间段”门禁，则 run 可以以 `partial_topology` 继续。后续结果必须引用缺失支流事实、降低适用置信度并说明覆盖不完整；不能让 LLM 自行忽略缺口。所有路径均无有效证据时，在 reasoning 前提前结束且不调用水体模拟模型。

NLDI、站点目录或必要 availability 服务发生不可恢复的请求失败、响应损坏或协议错误时，属于 topology 技术故障，不能伪装成某条支流没有数据后继续。

搜索上限由后端配置统一管理，不向前端提供最大深度等调参入口。至少同时限制发现节点数、路径跳数和累计水文距离；具体默认值须通过固定 topology fixtures、调用量和延迟测试确定，并在实施设计中公开。

### D16：解析过程走 session 流，topology 就绪通过 run 快照与原记录引用通知

M4 继续使用 M3 的两层 SSE，不增加第三条流或另一套事件存储：

- Query Parser 的实时模型文本属于 parser session，通过既有 `message_started / message_delta / message_finalized` 过程事件交付；
- 通过最终模型校验的 `AnalysisContext` 保存为 parser session 下的 `query_understood` typed item，并通过既有 `transcript_item` 过程事件交付；
- 确定性 BFS 产生的完整 `HydrologyGraph` 保存为 run 下的 `topology_ready` typed item，因为 topology 不属于任何 Agent session；
- topology 完成后，run SSE 的下一份完整轻量 `run_snapshot` 提供当前阶段、水体轻量状态和 `topology_ready` 原记录引用，不复制完整节点、边和 excluded reasons；
- 前端通过现有 run 详情及原全文接口按引用读取完整图。

run 重连仍只取得最新完整快照，不补播中间快照或依赖一次性通知；即使客户端断线时 topology 完成，重连也能从最新 phase、waterbodies 和引用恢复。数据库只保存一份原 `AnalysisContext` 和一份原 `HydrologyGraph`，SSE payload、历史响应和页面状态不建立语义不同的副本。

### D17：无证据产生确定性 indeterminate 结果，不新增 run 生命周期状态

M4–M6 继续复用 M3 的 run 状态枚举，不增加 `clarification_required`、`insufficient_evidence` 或 `partial_topology` 等生命周期状态：

- 请求含义不明确、地点不支持、日期越界或参数无法确认时，run 进入 `failed`，并携带可操作的业务错误码和安全提示；
- 模型、NLDI、站点目录或其他必要外部能力发生技术故障时，run 进入 `failed`，保留 `failed_stage`、`error_id` 和安全诊断关联；
- 请求有效但所有“参数 × 时间段”均完全无证据时，不调用水体模拟模型，由确定性逻辑生成全部为 `indeterminate` 的结果，run 正常进入 `completed`，并明确说明“证据不足，未执行推理”；
- 只有部分范围无证据时，确定性逻辑为这些范围生成 `indeterminate`，只对通过 evidence 门禁的范围调用模型。

`partial_topology` 是 topology 覆盖属性，`insufficient_evidence` 是结果原因或提示码，均不能冒充新的执行状态。解析失败和无证据结果仍作为原 run 保留在历史中，不通过删除失败事实来美化成功率。

### D18：M1–M9 是同一项目进程，M4 只为 M8 保留 memory_mode 接缝

M1–M9 共同组成同一套第一版系统，不按每个 Milestone 分别发布、正式联调、展示功能开关或进行独立产品验收；M9 完成后才开始正式前后端联调和整体验收。M4 在最终 create request、配置事实和 `AnalysisContext` 中保留稳定的 `memory_mode: stateless | memory_augmented` 字段，使后续阶段无需重造同义请求或上下文模型。

M4 不单独实现跨 run memory/RAG，不增加“尚未启用”API 分支、M4 专用前端显隐逻辑或假装生效的临时行为。M8 直接承接该字段，实现历史检索、来源结构、优先级和 provenance；最终前端在完整系统中提供相应模式选择，并与 M1–M9 一起做端到端验收。

`stateless` 与 `memory_augmented` 的最终语义以 M8 契约为准。M4 只保证字段能够被校验、固化、持久化和原样传递，不在本阶段消费它来检索历史，也不为内部实施顺序引入运行时 feature flag。

### D19：few-shot 只引导解析，LLM 保留自主参数推测能力

用户在 query 中明确提出 pH、溶解氧等参数时，Query Parser 直接提取对应业务概念并交给确定性 parameter resolver。

对“降雨后水质如何变化”“是否存在缺氧风险”等没有逐字列出参数的任务，LLM 必须能够根据用户目标自主推测所需参数；不要求该任务先命中硬编码意图模板。解析 prompt 可以提供少量 few-shot 示例，用于说明输出 schema、推断粒度和何时报告歧义，但这些示例不是穷举规则，也不建立按模板分支的业务逻辑。

LLM 为每个推测参数输出业务概念、与用户目标的简短可展示依据，以及是否存在需要澄清的歧义；不要求或保存隐藏思维链。确定性 resolver 随后把概念映射到 D09 的可执行参数能力注册表：

- 无模板但推断明确、参数已登记且不存在冲突时，可以正常继续；
- 推断概念无法映射、关键参数不受支持或存在多个实质不同的解释时，按 D03–D04 提示用户重新说明；
- 能力注册表验证系统是否会执行某参数，不把 LLM 的自主推测限制为固定任务集合；
- LLM 不能因某个关键参数不受支持而静默换成较弱、但无法回答原问题的参数。

前端仍不提供参数选择表单；最终使用的 `ParameterKey`、推测依据与无法满足的需求都必须在 `query_understood` 原记录中可见。

### D20：参数推测明确时自动继续，仅在不确定时打断

Query Parser 的候选通过 schema、parameter resolver 和全部输入门禁后，run 不增加人工确认等待点，直接进入 topology。`query_understood` 必须及时向前端和历史读取公开最终 `params_used`、每项简短选择依据及实际输入来源，使用户能够核对模型如何理解任务。

用户发现理解不符合预期时，可以使用既有 run cancel 停止整次分析；不能在原 run 中修改参数后继续，也不为此增加 `waiting_for_confirmation` 状态。取消受理后不得启动新的 BFS、evidence 或水体 Agent 工作，已产生的 parser 过程和结构化输出按 M3 规则保留。

只有参数存在实质歧义、无法映射、关键需求不受支持或超过已公开的安全数量上限时，才在 topology 前停止并给出可操作提示。不得为了避免提示而静默删减、替换或优先选择部分参数。

### D21：最终创建请求使用明确子对象，模型选择是 run 配置事实

最终请求由 `query`、可选 `target`、可选 `date_range`、`time_resolution`、`parser_model`、`waterbody_model` 和 `memory_mode` 组成。`target` 至少含 COMID 或坐标之一；前端地图正常提交两者，后端交叉校验。`date_range` 必须同时包含开始和结束日期，不能只给一端。

前端始终显式选择解析模型和水体模拟模型；后端只接受运行时模型目录已经发布的 provider/model 组合，并把两项选择与 timezone、实际粒度、memory mode 一起固化为 run 配置事实。API key 不属于请求模型。M4 只传递 `memory_mode` 接缝，不实现或伪造 M8 行为。

### D22：第一版能力覆盖主要问题族，discharge 是必需支持参数

第一版正式联调基线明确为 [M4 参数能力目录](m4_parameter_capability_catalog.md) 中的 154 个条目，而不是只登记少量示例或把完整外部目录原样视为可执行。覆盖面包括流量/水力、通用现场指标、氧与有机负荷、营养盐、主要离子与矿化度、固体/沉积物、金属与类金属、微生物/生物指标、农药、挥发性有机物和放射性指标。每项都必须具有稳定 `ParameterKey`、规范名称、canonical unit、同义词、availability/observation provider 和允许的 evidence 关系；缺少任一执行要素的目录项只能用于候选发现。参数代码与单位以 [USGS Parameter Codes v1](https://api.waterdata.usgs.gov/ogcapi/v1/collections/parameter-codes/items?f=html) 及其 [schema](https://api.waterdata.usgs.gov/ogcapi/v1/collections/parameter-codes/schema?f=html) 为依据。

`usgs:00060` discharge（streamflow）是每次分析计划都必须自动加入的支持参数，也可以在用户问题明确要求时成为目标参数。它按与其他参数相同的 evidence 门禁处理：直接数据优先，只有注册表明确允许的侧面估计才可作为较弱证据；完全没有 discharge 证据的受影响“水体 × 时间段”不进入水体模型，而产生可解释的 `indeterminate`。自动加入的 discharge 不伪装成用户选择，也不占用户目标参数上限。

单次 run 最多解析 6 个不同的用户目标 `ParameterKey`，以限制 15 天、6 小时粒度及 128 节点拓扑下的取数与推理扇出。超过上限时整次解析失败并提示缩小问题，不截断、不按模型排序取前 6 项。能力表的 154 项覆盖宽度与单次分析的 6 项成本边界是两件事；LLM 仍可自主推测目录中的新概念，resolver 对未接通能力明确报告 unsupported。

### D23：CONUS 校验使用行政区事实与 COMID 覆盖双门禁

地点文字由 geocoder 受限到 `US` 候选，但 [Google Geocoding 的 region bias](https://developers.google.com/maps/documentation/geocoding/guides-v3/requests-geocoding#region-biasing) 只表达偏好，不能当作地域校验。解析结果必须带国家和一级行政区；只接受美国 48 个相连州及 District of Columbia，并排除 Alaska、Hawaii 和领地。前端坐标、COMID 反查得到的代表点同样走行政区校验。

坐标到 COMID 使用 NLDI point-in-polygon 的 position 能力，不使用会吸附附近河道的 hydrolocation/raindrop 行为。只给 COMID 时先读取河段及代表点再反查地域；COMID 与坐标同时给出时，坐标解析出的 COMID 必须与请求值一致。geocoder 无匹配、地域不支持、NLDI 无 COMID、二者不一致分别返回稳定错误，不能合并成“地点不存在”。

### D24：availability 使用现代 WDFN v1，旧 WaterServices 不进入新基线

NLDI 只提供 NHDPlusV2 河网和 monitoring-site 关联；USGS Water Data OGC API v1 的 metadata 用于发现序列，历史参数与时间窗资格由实际观测存在性查询判定。按 [USGS WDFN API v1 公告](https://waterdata.usgs.gov/blog/api-v1-release/)，现代化 API 已成为主接口且旧 `waterservices.usgs.gov` 将于 2027 年初退役，因此 M4 不新增对旧 WaterServices 的生产依赖。

adapter 必须把“成功但没有匹配记录”与超时、限流、协议损坏和服务不可用分开。历史连续序列与离散 sample 都需要有界的实际读数探测，不能凭 metadata 时间包络或站点名称推断覆盖。

### D25：topology 默认使用 100 km、12 跳、128 节点三重上限

首期默认上限为每条搜索路径累计 100 km、12 个上游 hop、整张图最多发现 128 个唯一 COMID；并发外部查询上限为 8。[NLDI navigation](https://api.water.usgs.gov/docs/nldi/navigation/) 请求始终显式传 distance，并在可用时使用 `excludeGeometry=true` 降低响应体。前端不能覆盖这些值。

BFS 以 `(depth, cumulative_distance_km, comid)` 排序 frontier；同层并发查询可以乱序完成，但合并、节点编号、边和最终 `execution_layers` 必须按稳定排序生成。重复 COMID 合并、环检测和汇流边去重都在 domain builder 内完成。命中任一上限只停止受影响分支并标记 `search_limit_reached`；不得把它写成已确认没有数据。

### D26：预测 inlet 的近期基线窗口固定为 48 小时

prediction 或 mixed 的预测段使用预测起点前 48 小时作为近期观测基线窗口。availability metadata 必须表明相同参数在该窗口内可能有观测，站点才对该参数取得 inlet 资格。M5 读取真实数据后仍需重新确认，不因 metadata 声明而绕过 evidence gate。

48 小时是后端确定性策略，不是前端参数。若后续实验证明不同参数需要不同基线长度，应在唯一能力注册表中显式增加对应规则并补测试，不能由 LLM 临时决定，也不因 Milestone 变化建立另一份配置版本。

### D27：typed 内容与读模型只扩展 M3，不增加路由或第三条流

M4 直接扩展当前 `analysis_request v1`、`lifecycle_event v2`、唯一 run configuration 和 `run_snapshot v1`，并注册新的 `query_parser_input v1`、`query_parser_message v1`、`query_understood v1` 和 `topology_ready v1`。resolution preference、双模型选择和 memory mode 直接加入当前模型；所有调用方、fixture、存储和读取同步更新。M1–M9 内不为先前内部数据保留 V1/V2 union，也不因进入 M4 增加 configuration、lifecycle 或 snapshot 版本；开发数据库按当前 HEAD 迁移或重建。

`query_understood` 归 parser session，正文是唯一 `AnalysisContext`；`topology_ready` 归 run，正文是唯一 `HydrologyGraph`。run history 从持久化的 `query_understood` 投影轻量 `context_summary`；run detail 通过原 item 和 reference 读取完整图。run SSE 只给 `analysis_phase`、两个 item reference 和轻量 waterbody 状态，session SSE 沿用 M3 四种过程事件。M4 不增加 API 路由、生命周期状态、事件表或专用快照表。

## 3. 详细设计导航

- [M4 实施设计](m4_query_understanding_topology_implementation_design.md)：类型、模块、workflow、ports/adapters、确定性 BFS、配置和测试矩阵。
- [M4 API 与前端接入](m4_query_understanding_topology_api.md)：最终 create request、读模型、typed content、两层 SSE 和公开错误。
- [M4 Implementation TODO](m4_implementation_todo.md)：coding agent 的施工顺序、阶段门禁、测试命令、提交边界和证据记录格式。

M5/M6 仍须实现最终 evidence 获取、质量校验和“参数 × 时间段”充分性门禁；M4 只冻结它们必须消费的上下文、topology、availability qualification 和 `insufficient_evidence` 语义接缝。该后续责任已同步到根契约文档和 Implementation TODO。
