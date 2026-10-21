# 专业知识与推理导向参考库：调查与决策记录

更新日期：2026-10-20（Asia/Shanghai）

状态：产品讨论已整理。本文保留用户决定、调查依据及方案收敛过程；**后续实施以[完整技术设计](guideline_knowledge_design.md)为准**。本文中的历史候选项不是与主设计并行的第二套规格，文档完成也不是功能实现完成声明。

## 1. 目标与已确认决策

### D01：当前仅开展调查、代码阅读与设计讨论

- 不修改应用代码，不实现检索或推理功能。
- 允许记录设计决定到 Markdown；讨论中的建议与用户确认的决定分开。
- 对话使用通俗表达；面向后续 coding agent 的文档保留技术术语和精确契约。
- 最终交付应是整理后的完整技术文档，而不是未经归并的问答记录。

### D02：说明书要求的数据不足时，保留模型自主推理

讨论场景：推算下游 pH，已有上游 pH 和流量，但缺少某份说明书要求的其他水化学数据。

用户决定：**“让模型自行推理”。**

含义：不得仅因说明书中的必要输入不齐，就新增强制停止、强制拒绝数值或强制降级规则。说明书可以帮助模型认识假设与不确定性，但不接管模型的判断权。

这条决定没有要求删除项目现有的数据授权、引用、测量溯源、数值结构或结果资格校验；这些既有约束是否需要调整，应以具体需求另行讨论。

### D03：说明书是 advisory reference，不是必须执行的 procedure

用户选择：**“把说明书作为可选参考，由模型决定是否采用及如何分析”。**

即使找到适用说明书且资料足够，模型仍有权决定是否采用及采用哪些内容。本功能不应把 guideline applicability 转成必须使用指定方法的执行门槛，也不应仅因模型没有采用命中的 guideline 就判定结果非法。

检索触发方式已按 D07 确认为系统预先提供相关资料；是否记录采用理由仍待确定。“可选采用”与“系统自动提供”是独立语义。

### D04：参考内容必须包含解释性推理链路

用户补充：**“不想让说明书成为必须遵循的内容，但是可以给模型一个推理导向”，说明书需要保存“推理链路，让模型理解为什么会出现某种情况”。**

因此，知识内容应覆盖专业因果机制、成立条件、中间过程及现象之间的联系，不能只有公式、阈值或操作清单。

这里的推理链路是可编辑、可引用的领域知识和解释性论证。具体表示形式、检索粒度及如何关联本次证据仍待设计。

### D05：应用运行时检索预先收集、整理的专业资料库

用户选择：**“搜索我们预先收集、整理好的专业资料库”。**

因此，首版运行时知识来源是预先维护的 corpus，不采用每次分析直接联网搜集新资料的方案。当前设计调研可以联网查找第一方专业资料，这与应用运行时来源是两个不同问题。

资料整理方式后续按 D08 确认，检索触发方式按 D07 确认。内容复核责任、储存格式、搜索算法及是否使用 embeddings 仍待确定。

### D06：Demo 首先覆盖下游 pH 的估计和原因解释，设计保留其他指标的扩展空间

用户决定：**“demo 可以先完善……下游 pH 的估计和原因解释……但是要留后续拓展空间”。**

首批知识内容、示范分析与验收案例以 downstream pH estimation / explanation 为重点。知识组织与检索设计需能容纳后续指标及分析场景，不能把整个功能的内容模型或调用入口限定为 pH 专用。

扩展需求不等于首版同时交付所有指标的 corpus，也不自动要求通用规则引擎、图数据库或额外管理系统。具体扩展机制将在知识格式和调用方式确定后定义。

### D07：系统自动检索并向模型提供相关说明书

用户选择：**“系统先根据问题找几份相关资料，提供给模型参考”。**

因此，首版由系统根据分析问题和可用的任务信息检索预先维护的 corpus，提供有界的相关知识上下文；不依赖模型主动调用搜索工具才能获得说明书。模型是否采用及怎样分析仍由 D02/D03 决定。

确切检索输入、检索时点、每次文档数、排序和预算，以及不同水体、数值任务和后续 synthesis 之间怎样复用内容，仍待技术设计。该决定不意味着每个模型调用都重新搜索，也不意味着全量知识库进入 prompt。

### D08：将专业资料整理成解释性说明书，并保留出处

用户选择：**“先整理成说明书，讲清原因、成立条件、判断线索和例外，并保留出处”。**

首版主要检索和提供的知识单元是整理后的说明书，而非只把原始专业文献随机切片交给模型。说明书正文需有完整的解释性推理链路；根据专业资料整理的内容应能追溯到原始出处。D13 进一步允许本地用户自编内容不附外部出处，并显式标明其来源类型。

是否保留原文全文或片段、怎样细化 claim-to-source 对应关系、谁负责整理与复核，仍待设计。整理说明书不等于已获得领域专家对其科学正确性的确认。

### D09：结果展示采用的说明书，transcript 展示系统提供的说明书

用户决定同时包含：**“结果解释可引用实际采用的说明书，并能查看其内容和出处”**，以及 **“在 transcript 流程中显示系统提供了哪些说明书”**。

- 结果解释支持 guideline citations，并能从引用查看说明书内容和专业出处。
- transcript 流程展示系统向模型提供过的说明书，不能把全部 provided items 自动标为 adopted/cited。
- 模型可以不采用任何说明书；依 D03，不得为了满足展示要求伪造引用或强制采用。
- “采用”在实现与验收中只能依据模型明确返回的引用或采用记录表达，不等于独立证明模型内部如何产生结论。

记录 schema、引用粒度、版本快照、历史读回以及具体前端组件仍待技术设计；不预设新增顶层页面。

### D10：Demo 验收包含专业参考的完整展示，以及少量实测案例的估值核对

用户选择：**“打通专业参考与展示，再选少量有实测的例子核对估值”。**

首版不仅验收检索、上下文提供、结果引用和 transcript 展示，还需选取少量有可对照实测值的 pH 案例检查估值表现。样本数、样本筛选、比较时间尺度和基线方案将在验收设计中明确。

不把小样本核对宣称为系统性精度提升，也不把说明书引用当成估值准确的证明。既有 `runtime-test-1019` 归档可以帮助检查流程与引用，但其目标值是预测，不能单独充当目标水体的实测真值。

### D11：agent 整理的说明书先作为草稿，经人工检查后入库

用户选择：**“先作为草稿，由你或指定的人检查后再入库”。**

agent 生成或整理的内容先进入 draft 状态，由用户或其指定的人检查确认后，才能成为运行时可检索的正式内容。机器进行格式、出处或向量校验不能代替此人工发布决定，也不能伪造 reviewer 身份。

### D12：前端开放用户创建说明书的入口

用户补充：**“还可以在前端开放入口，让用户创建自己的说明书”。**

首版范围增加本地前端的说明书创建入口。设计需支持草稿编辑、人工检查/发布及已发布内容进入自动检索，保留说明书版本以支持结果引用和历史读回。

由于前端可修改正式资料的来源内容，前述“项目内 Markdown 文件作为唯一运行时内容源”的候选方案需调整。按后续 D14，内置发行资源由 Git/安装包维护，本地用户草稿和修改由数据库维护；同一有效版本只有一个内容权威，数据库中的内置副本是不可变导入记录。

### D13：用户自编说明书可无外部出处，资料库供本地用户使用

针对用户自编内容是否必须附专业出处，用户答复：**“不需要，因为只会保留在用户本地，用户自己用的”。**

因此，本地用户原创/经验类说明书可不附外部专业出处，但来源类型必须可区分为 user-authored；基于专业资料整理的 curated-reference 内容继续保留其专业出处。两类内容都遵循 advisory 使用语义和人工发布流程。

“用户本地”指用户创建或修改的内容不上传公共共享库；程序自带的资源按 D14 可通过 Git 分发。用户随后确认允许配置的模型服务处理内容，见 D14。

### D14：内置资料可随 Git 分发，用户资料与修改留在本机；允许配置的模型服务处理

用户确认：**“资料库只在本机保存、不公开共享；允许用户配置的模型服务处理内容”**，并补充：

1. 程序自带的说明书由制作者维护，可提交 Git 供他人下载。
2. 用户自定义的说明书保存在用户本地、供自己使用。
3. 用户也可以编辑程序自带的说明书。

因此，两种资料来源在运行时统一检索，但其维护和分发边界不同：内置发行资源可纳入 Git，用户数据与本地编辑不自动反写发行文件、不自动上传共享。

工程方案采用 local override：前端编辑内置内容时创建/更新本地修改稿，人工发布并完成索引后替代该内置文档参与后续检索。保留发行原文与本地修改的来源关系，更新程序不覆盖本地修改，并提供显式恢复内置版本的操作。

local override、版本固定、数据库表和接口是据本条要求确定的工程设计，并非额外假称用户逐项选择过的实现细节。

## 2. 当前代码调查

调查基线：分支 `codex/runtime-smoke-fixes-20261014`，HEAD `0f2bfd4d79b80d80dfcaaec03d8c8c6ea6879b41`。本轮以源码静态阅读为依据，未执行分析运行或回归测试。

### F01：当前系统已经获取外部证据，缺口集中在专业方法与解释知识

项目主链路见 [AGENT.md](../../AGENT.md) 与 [runner.py](../../backend/app/application/reasoning/runner.py)：查询解析与水系拓扑、证据采集和 planning、结果骨架与 gates、分批 reasoning、overall synthesis、结果装配与持久化。

因此，需要区分两类问题：实际水体证据从哪里来，以及模型根据什么专业方法解释这些证据。本需求主要补充第二类能力，并可能影响模型判断还需查哪些资料。

### F02：现有计算注册表与模型估计可以作为后续接入点

[numeric_methods.py](../../backend/app/application/evidence_tools/numeric_methods.py) 的 `NumericMethod` 已包含 method identity/version、输入参数和单位、时间关系、confidence cap、执行类别及 manifest digest。生产 registry 为各能力注册模型估计，并为符合条件的指标注册时间线性插值与最近值保持方法。

这些结构可供后续追踪已有计算方法；不能因为 guideline 文本描述了一个公式，就把它视为已注册、已验证或已执行的 calculation。是否新增计算方法不属于本轮已确认范围。

### F03：模型估计现在仍会利用自身知识

[numeric_estimation.py](../../backend/app/application/reasoning/numeric_estimation.py) 的 `_POLICY` 明确允许 related evidence 与 general knowledge 支持估计，同时要求说明缺少的本地信息；同指标原始测量的可追溯性影响是否允许输出点估计。

该文件已有 pH 专项提醒：pH 是对数尺度，复制、平均或对上游 pH 做流量加权不能直接宣称为经过验证的化学混合方法。但这些提醒仍主要是固定 prompt 中的文字，不是可按问题检索、关联权威出处的专业知识包。

### F04：既有 validator 主要核对结构、引用与已保存数值的一致性

[draft_validator.py](../../backend/app/application/reasoning/draft_validator.py) 核对 evidence scope、结果来源、confidence cap，以及最终数值/范围/单位是否与已保存 numeric evidence 一致。

这不等于验证某段自然语言因果解释在科学上成立。后续也不能把“包含 guideline 引用”描述成“科学推理已验证”。

### F05：专业参考与现有历史 RAG 的语义不同

[M8 设计](../M8/m8_memory_rag_design.md) 的主要检索对象是历史 transcript/result 的投影，命中以 historical inference 进入本次上下文。[HistoricalInferencePayload](../../backend/app/domain/evidence/historical_inference_payload.py) 保留 historical case 与 cell projection；[applicability.py](../../backend/app/application/evidence_policy/applicability.py) 管理其证据适用性。

专业 guideline 提供解释机制与方法知识，不是某个历史 run 的结果，也不是目标水体的 observation。可研究复用检索基础设施，但其 corpus、source identity、角色、引用语义和生命周期需明确，不宜直接伪装成现有 historical case。

### F06：参考知识不能直接放进要求强制遵守的规则集合

[prompt_builder.py](../../backend/app/application/reasoning/prompt_builder.py) 的 `_render_system_prompt()` 对现有 bracketed rules 明确要求严格遵守；[prompt_policy.py](../../backend/app/application/reasoning/prompt_policy.py) 还定义不同 phase 的 tool permissions。

依 D03，应研究独立的 advisory knowledge context，而非把检索出的专业文本直接追加为必须执行的 system rules。检索在哪个 phase 发生、后续 numeric estimation 与 synthesis 如何共享选定内容，尚未冻结。

### F07：numeric estimation 发生在最终 reasoning 之前，不能只在结果解释阶段提供参考

[planning_loop.py](../../backend/app/application/reasoning/planning_loop.py) 在 evidence planning 收敛后，通过 `ModelNumericEstimator` 和 `resolve_numeric()` 执行数值估计，再 seal 本次 evidence input；随后 runner 才处理 reasoning batches 与 synthesis。

[numeric_estimation.py](../../backend/app/application/reasoning/numeric_estimation.py) 为每个 scheduled task 单独建立包含观测锚点、相关证据及上下文的模型请求。目前该请求不提供交互搜索工具，且现有 prompt policy 对 numeric estimation 明确设置 `tools_allowed=False`。

设计影响：若目标包含改善 pH 数值估计，应明确知识如何在 numeric estimation 之前或期间可用。仅向后续 reasoning/synthesis 加入知识上下文，只能影响后续解释，不能自动改变已保存的估计值。若选择模型主动搜索，应设计对应的工具调用阶段与 bounded lifecycle，不能假定把知识工具加入 evidence planning roster 就能覆盖全部数值估计路径。

### F08：现有 transcript 与结果解释可承载关联，但新知识类型不会自动展示

[transcripts/registry.py](../../backend/app/application/transcripts/registry.py) 通过 `ContentRegistration` 管理 typed content、references、metadata 与 public projection；`inference_result` 已注册对应的引用提取器。

[AnalysisStep](../../backend/app/domain/attribution/analysis_step.py) 和 [AnalysisDetail](../../backend/app/domain/attribution/analysis_detail.py) 支持 `transcript_item_refs`；但 [AnalysisStepDraft](../../backend/app/application/reasoning/draft_models.py) 目前主要返回 evidence/result refs，尚无专用 guideline citation。可以评估复用 transcript 引用闭包来关联知识快照，但模型引用入口和引用资格仍需明确扩展。

[DetailPanel.tsx](../../frontend/src/features/analysis/DetailPanel.tsx) 的 `visibleRecords` 显式列出可见 transcript 类型，`RecordView` 支持 inline 内容展开与保存正文读取。新知识记录需要相应注册、公开投影、类型/引用传递及前端渲染；仅写入数据库或模型 prompt 不足以完成 D09。

### F09：已有 embedding 能力，但输入契约绑定历史 case query，不能直接嵌入说明书

[EmbeddingPort](../../backend/app/ports/embedding.py) 提供带 profile、deadline 和 cancellation 的 embedding 接口。[EmbeddingProfile](../../backend/app/domain/memory_rag/embedding_profile.py) 固定 provider/model、dimensions、distance metric、输入版本及 digest。

[OpenAIEmbeddingAdapter](../../backend/app/infrastructure/adapters/openai_embeddings.py) 已有远程调用、响应 model/count/order 验证和取消处理；其输入会先调用 [validate_canonical_embedding_texts](../../backend/app/application/transcript_memory/embedding_contract.py)，该函数要求文本可解析为 `CanonicalCaseQueryV1` 且 canonical bytes 与版本匹配。

因此，模型配置、供应商传输及通用向量校验可作为复用基础，但当前 adapter 并不接受任意 guideline 正文。后续需要明确 guideline document/query 的 canonical text contract，以及如何分离或推广现有历史 case 的格式校验；不能只把说明书文本传入现有 adapter，也不能为此取消输入边界校验。

[M8 runtime](../../backend/app/infrastructure/runtime/m8.py) 将 embedding、历史 case index、generation 与 transcript source loader 组合成历史检索服务。专业资料可研究共用底层供应商模型与 PostgreSQL/向量基础设施，但其 corpus/index identity 和 lifecycle 不应直接复用历史 case 身份或以历史 memory mode 代替专业资料的开关语义。

以上为静态源码证据。本轮没有调用实际 embedding 服务，未验证当前供应商配置、检索质量或性能。

## 3. 外部调查与可借鉴内容

以下为 2026-10-20 查阅的第一方资料。网页知识用于设计调研，不代表已将其作为应用运行时知识源接入。Firecrawl CLI 已检查，但未认证，本轮联网调查使用可用的网页搜索/读取工具；未修改登录状态或项目依赖。

| 资料 | 核实内容 | 对本需求的启发与边界 |
| --- | --- | --- |
| [EPA CADDIS: About Conceptual Diagrams](https://www.epa.gov/caddis/about-conceptual-diagrams) | 用图及配套文字描述 sources、stressors、中间过程和生物响应之间的可能关系；关系存在表示可能发生，并非总会发生。 | 与 D04 高度匹配：知识包可包含 causal pathways、modifying factors 和适用情境。其本来用途是水生态损害的原因分析，不直接证明任意水质数值估计准确。 |
| [EPA CADDIS: Getting Started with Data Analysis](https://www.epa.gov/caddis/getting-started-data-analysis) | 将已有数据映射到假设的因果路径，有助于识别可开展的分析与数据缺口。 | 推理链路应能关联支持证据、反证、未知环节，而非仅输出一条听起来合理的解释。是否要求模型逐项记录仍待确认。 |
| [EPA CADDIS: Nutrients](https://www.epa.gov/caddis/nutrients) | 营养物质、光照、植物生长、光合作用与呼吸可能通过不同路径影响溶解氧及 pH。 | 示例应包含成立条件与竞争过程，不宜写成单向、无条件的结论。 |
| [USGS PHREEQC: MIX](https://water.usgs.gov/water-resources/software/PHREEQC/documentation/phreeqc3-html/phreeqc3-27.htm) | 混合计算按元素物质的量及混合比例组织，并涉及水溶液平衡。 | 可解释为何不能仅把上游 pH 的算术处理称为完整化学混合计算。不是引入 PHREEQC 或新计算引擎的决定。 |
| [USGS: Alkalinity and Water](https://www.usgs.gov/water-science-school/science/alkalinity-and-water) | 碱度描述水体中和酸的缓冲能力；较高碱度可减弱酸性输入造成的 pH 改变。 | 说明书可以解释变化机制以及为什么未知碱度意味着一个解释环节未知；不能把区域常识当作目标水体的碱度观测。 |
| [EPA: Guidance on the Development, Evaluation, and Application of Environmental Models](https://nepis.epa.gov/Exe/ZyPURL.cgi?Dockey=P1003E4R.TXT) | 2009 年指南讨论应用目标、数据条件、domain of applicability、假设、验证与不确定性。 | 可借鉴方法说明书应记录什么；它是方法学指南，不是本项目必须执行的法规或数值规则。 |
| [Agent Skills Specification](https://agentskills.io/specification) | 支持 metadata、正文说明及按需加载的 references/resources。 | 可借鉴 skill 的分层组织方式；采用该文件格式并不自动获得检索、运行时集成或遵循保证。 |
| [Sentence Transformers: Semantic Search](https://www.sbert.net/examples/sentence_transformer/applications/semantic-search/README.html) | 将查询和 corpus 内容编码到兼容向量空间，用向量接近程度检索相关条目；短查询到长文档属于需考虑的检索任务类型。 | 支持用户提出的 embedding 方向。具体模型需覆盖问题与说明书的语言和任务；本文引用其原理说明，不表示选定该库或某个模型。 |

## 4. 候选方案：可检索的解释性知识包（未冻结）

用户提出的“RAG 搜索 + skill 形式说明书”可以拆为三个独立设计问题：

1. **Knowledge representation**：怎样组织专业机制、方法、适用条件、理由和来源。
2. **Retrieval and delivery**：怎样找到相关内容，并把有界、连贯的内容提供给本次模型调用。
3. **Use and observability**：模型如何自主选择，以及系统能记录哪些可观察的参考与结果关联。

依 D04/D08，一个知识单元需包含有上下文的解释性说明书和出处。候选技术表达是以某项机制、分析问题或方法为主题的知识包，包含正文与检索 metadata，必要时链接详细依据。可以借鉴 skill 的结构，不预设必须原样采用完整 Agent Skills 格式、附带脚本或开放代码执行。

依 D06，候选扩展方式是让知识包通过通用 parameter / scenario metadata 关联能力目录和分析场景，首批发布 pH 内容；检索和上下文接口处理通用 guideline identity 与版本。此处是设计建议，具体字段与匹配规则尚未冻结。

### 4.1 知识包候选内容

以下是讨论清单，不是冻结 schema，也不表示每个字段必填：

| 部分 | 候选内容 |
| --- | --- |
| Identity / provenance | guideline_id、version、title、来源 URL/文献位置、发布/获取日期、整理者、内容状态 |
| Discovery metadata | 指标、现象、场景、触发条件、相关术语、区域/尺度、适用范围 |
| Mechanism explanation | 起因、中间过程、可能结果、作用方向、机理解释和关键条件 |
| Reasoning guidance | 建议考虑的判断顺序，每一步为什么有意义，何时应改用其他解释 |
| Evidence mapping | 哪些观测支持或削弱该机制，哪些信息未知，怎样区分竞争解释 |
| Calculation reference | 可参考公式、变量、单位、假设及适用条件；与已实现 calculator 的关联可选 |
| Limitations | 不适用情境、反例、不确定性、缺数据时可考虑的推理方向 |
| Worked examples | 有专业依据的解释示例；示例情境不自动成为目标水体的事实 |

对 D04 的设计推论：应优先保留完整解释单元。即使检索索引采用 chunks，也应研究命中后恢复其条件、例外和出处的方法，避免只给模型一段脱离前提的结论。

### 4.2 示例内容形态（仅用于讨论）

参考 EPA 的营养物质模块，可以组织这样一条有条件的解释路径：

```text
在光照等条件合适时：
营养物质增加
  -> 水生植物或藻类生长可能增加
  -> 光合作用可能增强
  -> pH 可能升高，溶解氧也可能增加

同时考虑竞争过程：
植物和微生物呼吸会消耗氧；光合作用受限时，耗氧可能更明显。

本次分析可参考的问题：
已有营养物质、时间、溶解氧或植物相关信息能否支持这条路径？
哪些环节没有观测支持？是否存在其他解释？
```

此处保存的是可参考的领域知识。它不证明目标水体已发生藻类增长，也不能单独确定 pH 变化的数值；模型可结合本次证据自主分析。[来源](https://www.epa.gov/caddis/nutrients)

### 4.3 引用与提供记录：已确定行为及候选技术边界

依 D09，必须区分系统实际提供的知识与模型引用的知识，并在对应界面展示。进一步记录检索候选、模型明确声明不采用的内容和不采用理由，仍是可选设计，不属于已确认要求。

检索命中不等于已提供；已提供不等于模型已采用；模型自述采用也不等于已经客观验证其遵循程度。设计验收需要区分这些可观察事实。

候选做法：持久化本次提供内容的不可变 snapshot，包含 guideline identity/version、正文或有界完整解释单元、出处和内容摘要哈希；模型请求引用相应 snapshot。历史结果查看该次 snapshot，避免后来编辑知识文件改变旧分析的含义。

结果中的 guideline citations 可验证为实际提供集合的子集；空集合合法。这是来源与引用完整性检查，不是强制模型遵循说明书，也不是专业正确性验证。引用的具体表示以及 numeric estimation、后续 reasoning 和 synthesis 之间如何保留采用信息待定。

### 4.4 pH 说明书样例骨架（讨论稿，未发布为正式 corpus）

**主题**：从上游 pH 观测推断下游变化时，怎样考虑缓冲能力。

**可参考的机制**：酸性物质输入水体后，pH 的变化还受到水体中和酸的能力影响。较高碱度通常可减弱此类输入带来的 pH 改变。碱度是与水体所含物质有关的性质，不能仅根据一个 pH 读数认定已知。[机理依据：USGS, Alkalinity and Water](https://www.usgs.gov/water-science-school/science/alkalinity-and-water)

**候选推理导向**：模型可把上游观测作为本次估计的参考，再考虑沿途是否存在新增输入、缓冲能力信息，以及是否有其他证据支持变化。该判断顺序是本项目的说明书编排建议，不是原资料给出的可计算下游 pH 的公式。

**可关联的判断线索**：当前数据是否真的包含碱度或相关水化学观测；沿途影响是否有本次证据；哪些环节只是一般机理；是否有相反观测或其他解释。

**例外与缺口**：未知碱度不能被自动当成高碱度或低碱度，也不能据此认定下游 pH 一定保持稳定或一定明显改变。依 D02/D03，资料不齐时模型仍可自主推理；说明书帮助其理解缺口，不作为强制停止条件。

**与数值的关系**：此机理本身没有给出数值估计大小，不能把它包装成已执行的计算或准确度证明。模型若给出估计，仍沿用现有数值来源、观测溯源和结果结构契约；本样例没有新增一套估值规则。

### 4.5 自动提供资料的候选接入思路（待技术定型）

依 D07/F07，可研究在每个 waterbody session 需要模型分析前，根据 query、parameter 和 topology/task context 选择有界的知识包集合，并将实际提供给模型的 selection snapshot 传递到相关请求。证据采集、数值估计和后续解释可各自从同一有版本的集合取得相关内容。selection snapshot 记录系统提供了什么，不等于模型已采用其中内容。

具体检索时点及是否允许采集完成后补充选择仍待设计。候选约束包括：避免每个时间窗重复执行相同搜索；不同水体场景不盲目共用同一命中列表；prompt budget 同时计算 evidence 与 knowledge；保持机理、成立条件和例外连贯；不新增为加载参考知识而单独调用模型的需求。

guideline references 应独立于 measurement evidence refs，不能通过伪装为可满足 gate 的 evidence fragment 绕过现有边界。知识库、检索索引与运行时引用的具体存储结构尚未确定。

### 4.6 小样本实测核对的候选方法（待定样本与执行规格）

依 D10，可选择少量有上游参考信息、且目标位置存在 pH 实测记录的案例。在评估输入中保留用于预测的上游证据，隔离待核对的目标观测及其派生内容，再对照输出与保留的观测。

候选检查点：

- 按预先定义的时间、位置、station/parameter 和输出语义匹配实测对照；点观测与整窗平均不得不加说明地当作同一种真值。
- 被保留用于核对的目标观测不能经原始记录、派生聚合、历史 RAG、缓存或说明书示例重新进入模型可见输入。
- 尽可能使用相同模型配置、相同证据与相同运行预算，分别比较无 guideline 与有 guideline 的估计，保留每次实际请求和输出。
- 同时报告 pH 数值差、估计范围宽度、观测是否落入范围，以及模型是否输出可比较的数值；不能仅靠扩大范围使结果看起来更好。
- 单独检查解释是否准确区分本次观测、一般机理和假设，是否能查看对应 guideline 内容及出处。
- 报告案例选择方法、样本量、缺测/未运行情况与模型波动限制，不从少量案例推导普遍精度结论。

以上是验收设计建议，不是已选定数据集、已完成对照实验或新增计算方法的声明。

### 4.7 Demo 技术结构的收敛建议（尚未冻结）

依已确认决定，优先讨论以下最小完整方案，而非为每项技术细节新增产品分支：

| 层次 | 候选结构 | 需保持的行为 |
| --- | --- | --- |
| Authoring | 按 D12/D14，内置发行资源在 Git/安装包维护，用户草稿、本地修改与发布版本存本地数据库；正文为 Markdown | 首批 pH 内容；支持用户自编与编辑内置。来源边界明确，同一有效版本不产生两份可独立修改的真相。 |
| Retrieval | 统一的 guideline retrieval application boundary，输入 canonical parameters、scenario/task facts 与用户问题 | 自动提供有界结果；当前按用户提出的 embedding 方向建议采用 metadata filtering + semantic retrieval，见 4.8。历史 memory 开关不应被直接当成专业参考库开关。 |
| Selection | 记录本次选中且实际提供的版本和内容；相同内容可在同一 session 的相关请求中复用 | 不把入选或提供标成模型已采用；提供内容必须计入每个请求的上下文预算。 |
| Prompt integration | 专用 advisory knowledge context，覆盖需要专业参考的 estimation 与 explanation 请求 | 不追加为必须执行的系统规则，不改变 measurement evidence 的资格与身份。 |
| Persistence | 复用 transcript typed-content 与既有正文保存/读取机制，关联知识 snapshot 和模型请求 | 历史读回能查看当时提供的内容与出处，知识更新不改写旧记录。 |
| Model citations | 模型可返回对已提供知识的明确引用；引用合法性由应用校验 | 无引用合法；只检查 identity/version/scope，不强制采用，也不把引用当作科学证明。 |
| Presentation | Agent/transcript 流程展示 provided records；结果解释提供 cited guideline 的内容/出处入口 | 两类状态分开；复用当前页面和读取链路，具体渲染与交互细节后续确定。 |
| Validation | 少量 pH 实测对照案例，加引用与历史读回的端到端验收 | 目标观测不泄露到输入；报告数值偏差、范围宽度、缺测和样本限制。 |

整理首批专业参考类说明书可继续依据本轮已查阅的 USGS/EPA 等第一方资料开展。前端创建与编辑已按 D12 纳入范围；文件上传解析、图形知识编辑器或自动持续采集仍不属于当前已确认需求。

### 4.8 用户提出 embedding：可行性与当前推荐方案（尚未冻结配置）

用户提问：**“用 embedding 模型可以吗”。** 该方向可行，建议以 embedding semantic retrieval 作为首版方案的组成部分；这不是精确 provider/model 已选定或已进行运行验证的声明。

候选处理流程：

```text
维护说明书 -> 生成有版本的检索文本 -> 预计算文档向量 -> guideline index
分析问题 + 指标/场景 -> 确定适用候选范围 -> 生成查询向量
  -> 按语义相关性排序 -> 选择有界知识包 -> 提供完整解释/条件/例外/出处
  -> 模型自主分析 -> 已提供记录与模型引用分别进入 transcript/result
```

embedding 在此用于查找相关内容。向量本身不承载可供模型直接阅读的推理链路；模型仍应收到说明书正文或具备完整上下文的解释单元。semantic similarity 不是科学正确性、因果成立或本次实测证据的证明。

候选技术原则：

- 指标、场景和 source/knowledge status 等 metadata 约束适用范围；embedding 在候选范围内比较含义，具体过滤规则不能简单排除跨指标相关机制。
- 文档侧可由标题、适用场景和机制正文等组成检索文本。短说明书可按完整单元建向量；长文按连贯解释单元索引，并保留所属说明书、条件、例外和出处的关联。
- 文档与查询使用兼容的 embedding profile；若模型要求不同的 query/document instructions，按对应任务处理。需验证中文问题、英文专业资料及整理后的中文说明书之间的匹配，而非默认所有 embedding 模型都满足需求。
- 预计算并持久化文档向量；内容、模板或 profile 变更时重建受影响索引。单次分析只对必要的检索输入生成查询向量，复用可重复的查询，不在每个时间窗重新嵌入整库。
- 可以研究共用现有 embedding provider/model 与 PostgreSQL/向量能力；guideline 的格式校验、索引身份和来源角色单独定义，见 F09。不得让不同 profile 的向量直接比较。
- Demo corpus 较小时，候选方案采用可复现的 exact similarity ranking；是否增加近似索引、全文融合或重排序，应由检索案例和规模证据决定，不是引入 embedding 的前提。
- Top-k、最低相关性、去重与知识输入预算通过检索样例确定。未命中、embedding 不可用与输入/索引不匹配分别记录；具体继续分析策略仍待设计，并保持 D02/D03 的自主推理方向。

待明确的配置包括 provider/model、语言支持、dimensions、document/query text template、索引存储及更新方式、timeout/budget 与索引切换契约。模型选择以实际检索样例验证为准，不因当前只有一个供应商 adapter 就视为已选定该供应商。

## 5. 下一轮待讨论问题

| ID | 问题 | 状态 |
| --- | --- | --- |
| Q01 | 应用正式分析时，从维护好的参考库检索、直接联网找资料，还是两者结合？ | 已确认：预先维护的资料库，见 D05 |
| Q02 | 首批场景覆盖什么：下游 pH 估计、其他指标、风险解释、污染归因，或其中一个具体问题？ | 已确认：Demo 优先下游 pH 估计与解释，并保留扩展空间，见 D06 |
| Q03 | 谁整理和维护知识：人工编写、从专业资料辅助生成后复核、直接检索原文？ | 已按 D11 确认人工检查后入库；D12 增加前端创建，D13 允许本地用户自编内容无外部出处 |
| Q04 | 因果链路用正文、结构化节点/关系，还是两者并存？是否需要图结构存储？ | 主设计第 5 节：Markdown 完整解释单元 + metadata，不引入图数据库 |
| Q05 | 何时检索、由谁触发、numeric estimation/reasoning/synthesis 各自获得什么内容？ | D07 及主设计第 6–8 节：run pin、首次实际模型请求前加载、session 复用 |
| Q06 | 多份资料有冲突、超出适用范围、没有命中、检索失败时，提供怎样的上下文？ | 主设计第 12 节：保持自主推理，状态明确、观点保留条件 |
| Q07 | 记录哪些引用、知识版本、采用信息与可见解释；前端展示到什么程度？ | D09 及主设计第 8–11 节：独立 delivery/usage、结果关联、历史快照 viewer |
| Q08 | 如何验证专业导向确实有帮助：代表性案例、对照运行、证据支持与反证、专业复核标准？ | D10 及主设计第 14–16 节：3–5 例 holdout 配对、完整引用与流程验收；实际数据调查在实施中完成 |
| Q09 | 在 corpus 规模与查询方式明确后，是否需要向量检索，哪些现有 PostgreSQL 能力值得复用？ | 主设计第 7 节：embedding + metadata + exact cosine，复用 catalog 配置与通用传输，独立 canonical contract/profile |
| Q10 | 本地资料能否发送给用户配置的远程 embedding/分析模型处理？ | 已按 D14 确认允许；仅用户自建/本地修改内容不进入公共分发库，内置资源可随 Git 分发 |

### 5.1 对齐收敛：需用户选择的内容与可由技术设计确定的内容

用户询问是否仍有内容需要对齐。当前产品方向已足够形成完整技术方案；不需要把字段名称、数据库结构、检索数量或代码分层逐一转为用户选择题。

Q03 已按 D11 确定为人工检查后入库；Q10 及两类资料库已按 D14 确定。当前产品方向已可整理为完整技术文档，工程默认值应明确记录；不需要额外索取对每项技术实现的批准。

其他未决项可按以下建议默认值继续收敛。这些是待纳入整体方案审阅的设计建议，不是新增的用户确认决定：

| 情境 | 建议默认行为 | 与已确认决定的关系 |
| --- | --- | --- |
| 未命中相关资料、索引未就绪或 embedding 服务失败 | 保留安全、明确的检索状态，在 transcript 可见；模型继续自主分析，不伪装成已获得专业参考。各失败状态分别记录，现有分析自身的错误仍按原契约处理。 | 遵循 D02/D03；说明书不成为强制停止条件。 |
| 资料有不同解释或相互冲突 | 保留与本次问题相关的解释及各自成立条件、来源，由模型结合实际证据判断；不按相似度把分歧包装成唯一结论。 | 遵循 D03/D04/D08；检索排序不接管判断权。 |
| 说明书更新后查看历史结果 | 读取该次分析保存的知识快照及出处；当前知识库更新不修改旧分析的参考内容。 | 支持 D09 的可查看性与准确的历史语义。 |
| 首批正文与因果链路的表示 | 本地数据库的不可变发布版本保存 Markdown 正文与结构化 metadata；解释性链路用正文/步骤表达。用户自编内容按 D13 标明来源类型。 | 满足 D04/D06/D08/D12/D13，不因链路需求预设图数据库。 |
| embedding 与检索的工程参数 | 基于现有模型配置、语言需求和小型检索案例确定 profile、维度、top-k、预算与索引结构；在技术文档明确给出默认值及验证方式。 | 遵循用户提出的 embedding 方向，不要求用户先选择底层实现细节。 |
| 小样本核对的具体数据 | 选择能隔离目标实测、保留有效上游输入的少量案例；按实际可获得的数据明确数量、匹配语义与基线，不虚构数据可用性或测试结果。 | 落实 D10；最终验收规格需可执行。 |

上述默认项中的任何内容若与后续用户偏好冲突，应调整最终方案；技术文档需清楚区分产品决定、工程默认值和运行验证证据。

## 6. 整理完成与实施入口

[完整技术设计](guideline_knowledge_design.md)已根据 D01–D14 和代码调查整理，包含两类资料与本地修改、人工发布、数据结构、embedding/检索、自动模型接入、独立引用记录、前端创建与历史查看、失败语义、验收和实施步骤。

详细施工清单已整理为 [Implementation TODO](guideline_implementation_todo.md)，覆盖 Milestone 0–12 的工作、测试、验证命令、阶段出口、提交边界和最终验收；所有实施项初始未完成。主设计第 15 节只保留导航，不维护重复进度。

本轮只新增/编辑文档，没有进行应用实现、模型调用、实测实验或专业说明书的人工批准/发布。后续功能契约以主设计为准，实施进度只更新 TODO，本文用于追溯决定与调查依据。


## 7. 后续已确认：摘要索引、查询 skill 与补证收敛（2026-10-21）

用户确认以标题/摘要/专业主题信息建 embedding 索引，命中后指向原文，不把全文塞入向量输入；正文、出处和不可变版本继续完整保存。查询 skill 指示模型将原问题专业化改写，以 `search_guidelines` 工具的 `query` 参数提交；后端校验并使用固定范围/profile 检索，返回最多三份相关说明书，后续阶段复用。该 skill 是应用内可信指令与工具 schema，不新增脚本执行或任意请求体字段触发入口。

用户同时接受：每次补证服务原问题并说明判断用途；辅助指标不升级为新研究任务；无新证据或已确认不可获取时收敛；缺口进入不确定性说明，保持原证据资格。用户随后回复“ok，开始修改吧”，授权实现上述变更。实现和验证进度仅记入现有 TODO；此确认不替代四份 pH 草稿所需的内容批准。
