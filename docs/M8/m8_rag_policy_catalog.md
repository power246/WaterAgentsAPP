# M8 Memory/RAG Policy Catalog

更新日期：2026-10-07

本文是 M8 可执行 policy 的设计输入。runtime 不解析 Markdown；实施时必须将规则固化为 immutable typed policy、projection registry、query builder、validator 和 tests。总体边界见 [M8 Memory/RAG 设计](m8_memory_rag_design.md)。

## 1. 权威来源

| 事实 | 唯一权威 |
|---|---|
| 当前 query、mode、ParameterKey、window、timezone、memory mode | 保存后的 `AnalysisContext` 与 run configuration |
| 当前 waterbody COMID、合法 nearby/topology set | 当前 session identity 与保存后的 `HydrologyGraph` |
| 历史 run/session 终态 | `RunCoordinator`/PostgreSQL canonical state |
| 历史科学结论 | 原 session 的 validated `inference_result v1` |
| supporting evidence | 原 `evidence_fragment v1` 与完整引用闭包 |
| source eligibility | typed transcript registry + 本文 final eligibility policy |
| active embedding/profile | PostgreSQL 中唯一 active index generation |
| 检索相关性 | 本文 strategy-specific score；不得替代 confidence/quality |

Agent 参数、prompt、日志、index row、embedding、自然语言摘要和 UI 标签均不能替代上述事实。

## 2. Source eligibility

### 2.1 Case root

一个 historical case 的 root 必须是 `inference_result v1`，并同时满足：

- 原 run 与 waterbody session 均为正常 `completed`；
- item 的 `rag_eligible=true`，持久化状态 confirmed；
- result 可由 registry 解析，run/session/item/target COMID 归属一致；
- result 引用的 `AnalysisContext`、result cells 和必要 evidence closure 完整；
- content/schema/projection adapter 在 allowlist 中；
- 不属于 cancelled、failed、repair 中间产物或 persistence-error 未补写事实。

每个合格 result 形成一个 `HistoricalCaseRef`；同一 source result 在同一 generation 中只能有一个 ready case projection。

### 2.2 Supporting fragment

只有被 root result 的返回 cells 直接引用、`rag_eligible=true` 且独立通过 schema、scope、quality、persistence 和引用校验的 `evidence_fragment v1` 才能成为 supporting fragment projection。`suspicious` 可以保留，必须携带原 quality flags/reason/coverage；`invalid`、unavailable、empty 和引用损坏不得投影。

一个 fragment 的缺失不允许伪造；若它是 result 有效性所必需的引用，整个 case 不 eligible。若它只是非必需支持且原 result 仍完整，case 可 ready，但不得返回该 fragment。

### 2.3 明确排除

message、reasoning、planning、prompt、model raw request/response、query-understood item 本身、run/topology summary、tool call/result、raw source batch、inspection、bundle、error、diagnostic、provider envelope 和未知未来 content type 全部排除。不得使用通用 JSON/text fallback 扩大 allowlist。

## 3. Canonical case query

### 3.1 输入

`CanonicalCaseQueryV1` 只从 validated `AnalysisContext` 与对应 waterbody result 生成：

```text
schema_version = 1
mode
requested_parameter_keys[]
supporting_parameter_keys[]
window_descriptors[]
  window_kind
  duration_days
  local_months[]
  season_buckets[]
  resolution
normalized_intent_tokens[]
```

数组按 registry/window 稳定顺序输出。`normalized_intent_tokens` 只能来自后端枚举与 capability registry，例如 `analyze_current_state`、`fill_gap`、`predict`、`requires_discharge_support`；不得复制 raw query、parser message、rationale 或模型生成摘要。

COMID、绝对 UTC 时间、source identity 和 topology relation 作为结构化 filters 单独保存，不进入 embedding text，避免水体编号或具体年份支配“问题相似性”。绝对时间仍用于 exact-window hard filter，local month/season/duration 用于历史类比。

### 3.2 Canonical text

embedding 与 full-text search 共用一个版本化模板：固定字段名、固定顺序、ASCII enum、canonical ParameterKey、canonical resolution、无省略的 schema/version。相同结构必须生成字节相同 UTF-8 text；模板 digest 属于 embedding profile identity。

canonical text 有硬字节/token 上限。builder 在调用供应商前完成脱敏与 fit；超限使 indexing job 或当前 search 安全失败，不截断字段、不回退 raw query、不换模型。

## 4. Typed search request

Agent 可提交的 request 只有：

```text
HistoricalSearchIntentV1
  strategy
  parameter_keys[]
  window_ids[]
  limit                 # 1..5，默认 5
```

后端补全 `operation_id/run_id/session_id/memory_mode/current_comid/authorized_comids/time ranges/season/topology/profile`。每次 request 只允许一个 strategy；ParameterKey 至少 1 个且均来自当前 `AnalysisContext`，window 至少 1 个且均来自当前 stable window IDs。Agent 不得提交 source identity、COMID、自由文本、距离、hop、SQL、profile 或 embedding。

scope preparation 失败必须保持 `0 search / 0 hydrate / 0 source record`。同一 waterbody session 最多执行两次 historical search；这一限制同时进入 tool descriptor 和 server-side budget，不因模型重试而重置。

## 5. 固定检索策略

所有策略共同执行：`memory_augmented`、current session scope、source lifecycle、active generation、schema/projection compatibility、ParameterKey overlap、result/fragment final eligibility 等前置 hard filters。先过滤，后排名。

### 5.1 `exact_comid_parameter_window`

- candidate COMID 必须等于当前 waterbody COMID；
- candidate 至少覆盖全部请求 ParameterKeys 中的一个；
- candidate historical window 与请求 window 的 UTC interval 必须有正长度重叠；
- 只返回相交 ParameterKey × window cells；
- primary structural signal 为 interval overlap ratio；semantic/lexical 只在 hard match 内辅助排序。

该策略用于查询与既有历史实际时间重叠的分析。若当前是未来预测且历史时间不重叠，应使用 season 或 semantic strategy，不放宽本策略。

### 5.2 `nearby_comid_parameter`

- candidate COMID 必须位于当前保存 graph 的 active node set；
- 使用已保存 geometry 计算 candidate waterbody 与当前 waterbody 的确定性最短 geodesic distance；
- 首版 nearby 半径固定为 25 km，并且不得超过该 graph 保存的 `max_distance_km`；
- candidate 至少覆盖一个请求 ParameterKey；
- primary structural signal 为 `1 - min(distance_km / allowed_radius_km, 1)`；
- 当前 COMID 不属于 nearby 结果，避免与 exact strategy 重复。

不得为该搜索重新调用 NLDI、地理编码或外部空间服务，也不得接受 Agent 提交的半径。

### 5.3 `parameter_season`

- candidate 至少覆盖一个请求 ParameterKey；
- candidate COMID 必须是当前 COMID或当前 graph 的 active node；
- candidate mode 必须与当前 mode 相同；
- candidate window 与请求 window 至少共享一个 local meteorological season bucket：`DJF/MAM/JJA/SON`；
- season 按各自已保存 IANA timezone 的 local calendar month 派生，跨年 `DJF` 使用稳定 `season_year`；
- primary structural signal 依次为 same COMID、共享 season/month 数、duration/resolution compatibility。

首版 target 范围仍是 M4 的 CONUS COMID，因此使用北半球 season buckets；未来扩展非 CONUS 前必须版本化 season policy，不能静默复用。

### 5.4 `semantic_case_query`

- candidate COMID 必须是当前 COMID或当前 graph 的 active node；
- candidate mode 必须与当前 mode 相同；
- candidate 至少覆盖一个请求 ParameterKey；
- 当前 canonical query 与 candidate canonical query 必须使用 active generation 的同一 profile/template；
- 在 hard-filtered set 上执行 exact pgvector distance，不先做全库 nearest-neighbor；
- lexical signal 只来自同一 canonical text，不解析 raw query；
- semantic similarity 是 primary signal，COMID relation、ParameterKey overlap 和时间/season仅作为已验证 components/tie-break。

首版不使用 result body、summary 或 fragment body embedding 做第二次召回。

### 5.5 `topology_neighborhood_parameter`

- candidate COMID 必须出现在当前保存 graph 的 active node set；
- candidate 与当前 waterbody 必须通过保存后的 participating edges 存在祖先/后代路径；
- hop 与 cumulative path distance 从该 graph 计算，并受其已保存 `max_hops/max_distance_km` 约束；
- candidate 至少覆盖一个请求 ParameterKey；
- primary structural signal 依次为 relation direction、hop、path distance；direct upstream 优先于更远 ancestor，current node 排除；
- 不使用 excluded node，不把 graph coverage partial 解释为 graph 外没有候选。

不得重跑 BFS 或按历史 graph 擅自扩大当前 authorized topology scope。

## 6. Ranking 与 `rag_score`

### 6.1 Components

每个候选保存并审计以下适用 components：

```text
strategy
parameter_overlap_ratio
window_overlap_ratio?
spatial_proximity?
season_similarity?
topology_relation/hops/path_distance?
vector_similarity?
lexical_similarity?
quality_tier
original_confidence?
source_completed_at
```

各结构化 component 使用以下唯一 normalization：

- `parameter_overlap_ratio = matched_requested_parameter_count / requested_parameter_count`；
- `window_overlap_ratio = union(requested intervals covered by matching candidate intervals) / union(requested interval duration)`；
- `spatial_proximity = 1 - min(distance_km / allowed_radius_km, 1)`；
- `season_similarity = shared_requested_season_bucket_count / requested_season_bucket_count`；
- `duration_compatibility = min(requested_duration, candidate_duration) / max(requested_duration, candidate_duration)`，多窗口取符合hard filters的最高pair值；
- `hop_proximity = 1 - min((hops - 1) / max(max_hops - 1, 1), 1)`，direct neighbor为1；
- `distance_proximity = 1 - min(path_distance_km / max_distance_km, 1)`；
- `topology_proximity = 0.6*hop_proximity + 0.4*distance_proximity`；
- `upstream_preference = 1` 表示candidate是当前节点ancestor，descendant为0。

cosine similarity 先 clamp 到 `[-1, 1]`，再映射到 `[0, 1]`；full-text `ts_rank_cd` 使用 `rank/(1+rank)` 映射到 `[0, 1]`。比例分母必须大于0，非法/空输入在preflight拒绝。NaN、Infinity、维度/profile不一致或非法score使search failed，不丢掉坏row后继续伪装成功。

### 6.2 Strategy score

`rag_score` 是 `[0,1]` 检索相关性：

| strategy | 固定公式 |
|---|---|
| exact window | `0.75*window_overlap + 0.25*lexical` |
| nearby | `0.70*spatial_proximity + 0.30*lexical` |
| parameter season | `0.35*same_comid + 0.35*season_similarity + 0.20*duration_compatibility + 0.10*lexical` |
| semantic query | `0.85*vector + 0.15*lexical` |
| topology neighborhood | `0.55*topology_proximity + 0.25*upstream_preference + 0.20*lexical` |

适用 strategy 的 vector/lexical components 均由同一 canonical case query 产生；只有 `semantic_case_query` 必须在线生成当前 query embedding，四种结构化策略在 embedding supplier 暂时不可用时仍可确定性搜索。`same_comid/upstream_preference` 是0/1。公式变更需要新policy/profile version与离线排序fixture，不能在部署时自由调权。

### 6.3 Stable order

先按 `rag_score desc`，再按：

1. ParameterKey overlap 数量 desc；
2. `valid` quality before `suspicious`；
3. original confidence 数值 desc，null last；
4. source completed time desc；
5. `run_id/session_id/result_item_id` lexical asc。

confidence/quality 不进入 `rag_score`，不得伪装为语义相似度；它们只在同等相关候选中降低可疑/低 confidence 历史的排序资格。`semantic_case_query` 要求 `vector_similarity >= active_profile.semantic_min_similarity`；该 `[0,1]` threshold无默认值，必须用该profile的版本化离线fixtures确定并进入profile digest。低于threshold返回empty，不能靠凑满limit返回无关案例。

## 7. Limits、去重与 prompt fit

- exact search 对 hard-filtered rows 扫描，单 signal 最多保留 50 个内部 candidates；lexical/vector union 后按 case identity 去重；
- 每次 tool call 最多返回 5 个 cases；同一 source session/result 最多 1 个，同一 source run 最多 2 个；
- 每个 case 最多返回 2 个 directly-related supporting fragments；
- result cells 只保留请求 ParameterKey × window 的 stable order；不相关 cell 不进入 record；
- 最终 tool preview 复用 M5 `evidence_preview_max_bytes`（当前上限 16,384 bytes），先按 case 排名、再按 cell/fragment stable order选择完整记录；
- 一个 case 自身无法完整 fit 时跳过并记录 safe truncation reason，不输出截断 JSON；所有 case 都无法 fit 时是 `invalid/result_too_large`，不是 empty；
- 不把一个 case 拆成多次隐式分页。Agent 需要另一策略/窗口时发起第二次且最后一次 typed call。

裁剪只影响本次 prompt projection，不修改 index/source。`matched_by` 必须列出实际生效的 strategy 与 components，不能只返回自由字符串。

## 8. Hydration policy

search row 只产生 `HistoricalCaseRef`，hydrator 必须重新读取原 result 并验证：

- run/session completed、target COMID 与 item 归属；
- source content type/schema 与 projection digest；
- 当前 memory scope 与 strategy hard filters仍成立；
- requested ParameterKey/window cells 存在且引用闭包完整；
- supporting fragment item 属于原 run、仍 eligible、确实被返回 cell 引用。

任一 identity/authority 校验失败使本次 search `invalid`；不得静默跳过 corrupt top hit 后假装 corpus empty。返回 record 只包含授权后的 cells、必要 overall risk/availability、原 confidence/quality/time、score components 和稳定 source refs。历史原文可由用户通过既有 history API 阅读，Agent 没有任意全文展开能力。

## 9. Empty、unavailable 与失败

| 情况 | Tool execution | Collection fact | 当前 reasoning |
|---|---|---|---|
| 合法查询、active generation 正常、无合格 hit | completed | empty | 可继续 |
| 首代 generation 合法 building、尚无 active | failed | unavailable/index_building | 可继续 |
| extension/schema/profile/active generation 损坏 | failed | invalid/index_invalid | 可继续；配置错误另由 startup gate 报告 |
| embedding timeout/rate limit/unavailable | failed | unavailable/embedding_unavailable | 可继续，不换模型 |
| query/profile/dimension/vector 非法 | failed | invalid/embedding_protocol_invalid | 可继续，不混 profile |
| source hydration/ref/schema 不一致 | failed | invalid/historical_source_invalid | 可继续，不返回部分命中 |
| request scope/limit/strategy 非法 | preflight rejected | 无 collection/source | Agent 可在剩余 loop 内修正 |
| run cancel | cancelled | 既有 M5 cancellation semantics | 不再发布后续 source/result |

historical failure 永远不能满足 target/discharge gate，也不使已有 current evidence 失效；当前 required acquisition 的真实技术失败仍按 M5/M6 原规则处理。

## 10. Indexing job policy

- job 只在 source run/session completed 且 persistence confirmed 后创建；
- identity 固定 generation + source result item + projection version；重复发现为幂等；
- claim 使用数据库 lease 和有界 batch，process crash 后 lease 到期可恢复；
- 每个 attempt 有 deadline，指数退避带上限，最多 5 次；retryable 只含 timeout、rate limit 和供应商临时 unavailable；
- schema、profile、dimension、非有限向量、source invalid 等 non-retryable 错误直接 failed；
- projection rows 与 completed transition 在一个 transaction 提交；任何 partial batch 不可见；
- worker 不阻塞 run terminal，不读取 raw prompt/message，不自动换 supplier/model/profile；
- shutdown 停止 claim 新 job，给在途 transaction 有界收敛时间；未完成 lease 由下次启动恢复。

attempt 数、错误码、lag、ready/failed counts 进入内部 metrics/logs；canonical text、vector、credential 和供应商 raw response不得记录。

## 11. Current evidence priority

historical records 始终为 `historical_inference/contextual_only`。结果组装时：

1. 当前水体、当前窗口的 valid direct observation 优先；
2. M7 合法 direct-upstream result 按既有 relation 使用；
3. current-session allowed-indirect/context evidence 按 M5/M6 gate 使用；
4. M8 historical case 只补充背景、相似情况与局限；
5. 不得用 history 解锁 gate、覆盖当前值、平均历史 series、生成精确预测或提高原 confidence。

Agent 可以明确比较当前与历史差异，但最终 cell/source/attribution 仍由 M6 validator 和 assembler 决定。

## 12. Policy 变更门禁

以下任何变化都必须同步更新 typed policy、projection/profile version、migration/backfill 策略、offline fixtures、API 文档和本文件：source allowlist、canonical template、embedding dimensions/distance、hard filters、strategy score、threshold、limits、season/topology semantics、hydration projection 或 error code。只修改 prompt、SQL 或运行设置不构成合法 policy 变更。
