# M4 Milestone 3 完成记录

记录日期：2026-09-25

实现分支：`codex/m4-query-topology`

## 1. 阶段结论

Milestone 3 已完成唯一的强类型 `AnalysisContext`、parser 候选模型、地点/日期确定性 resolver、扩展后的 geocoding/COMID ports，以及 M2 参数结果、双模型和 memory mode 的上下文组装。给定 fake parser 输出与 fake ports，应用层现在只会产出一个满足全部 invariant 的 context，或返回稳定、无部分默认值的失败状态。

本阶段没有调用真实 LLM 或网络 provider，没有创建 parser session，没有执行 topology BFS、observation/evidence 查询或 workflow side effect。

## 2. Typed context 与 invariant

- `ParsedIntentV1` 只允许地点文字/坐标、成对本地日期、非空 `ParameterConcept[]` 和结构化 ambiguities；不能输出最终 COMID、`ParameterKey`、UTC、mode、resolution 或 graph。
- `AnalysisContext` 固化同一次 decision instant、IANA timezone、已校验 target、本地闭区间日期、UTC `[start,end)`、一到两段 mode、实际 resolution/source、1–6 个目标参数、required support、双模型和 memory mode。
- context 会重新计算本地日期边界与 DST 安全 UTC 边界、gap/prediction/mixed 分段、auto/default 粒度组合、最多 60 个窗口、参数去重以及 `usgs:00060` 并集。
- `validate_capability_registry()` 重新核对每个参数 key/name/unit 与唯一 M2 registry，并验证 required support 集合；`validate_configuration()` 拒绝 timezone、粒度 preference/source、模型或 memory mode 漂移。
- `AnalysisConfigurationSnapshot` 现在禁止把非 `12h` preference 标记成 API `default`。

## 3. 地点与日期 resolver

- `GeocodingPort` 明确区分文字正向候选和坐标反向行政区；`ComidLookupPort.lookup_position()` 表示 point-in-polygon，`None`/空 tuple 只表示成功无匹配，port error 不会被改写成业务空结果。
- 地点 resolver 覆盖坐标、COMID、二者交叉校验、文字唯一/多候选和 frontend/query 业务身份比较。同一 COMID 的不同显示名称不冲突，不同 COMID 分别返回 `target_mismatch` 或 `location_conflict`。
- 只接受 CONUS 48 州和 DC；点行政区与 COMID 河段代表点行政区执行双门禁。AK、HI、领地、外国、无 NHDPlusV2 COMID 和 target mismatch 保持不同状态。
- 日期 resolver 只消费一次固化的 clock snapshot。frontend structured date 优先，query date 只做 fallback；同值接受，实质不同返回 conflict，半缺失 parser 输出在 schema 层拒绝。
- `[today-7,today+7]` 是当地日历日闭区间；inclusive end 转成下一当地午夜的 UTC exclusive end，覆盖 spring-forward/fall-back。
- mode 严格从固化的当地 today 推导；mixed 在当地 today 00:00 切成 gap_filling 后 prediction，执行跨午夜不会重算。
- auto 的 1–7 日为 6h、8–15 日为 12h；省略 API 字段为 12h/default，24h 只由显式 preference 产生；15 日显式 6h 恰好 60 个窗口。

## 4. 验证证据

| 验证 | 结果 |
|---|---|
| M3 新增 location/date/context/resolver focused | `60 passed` |
| `python -m pytest backend/tests/domain backend/tests/application -q` | `582 passed in 11.43s` |
| `python -m pytest backend/tests/domain/test_time.py backend/tests/domain/test_time_windows.py -q` | `33 passed in 0.06s` |
| `python -m pytest backend/tests/architecture -q` | `77 passed in 1.03s` |
| 完整非 PostgreSQL backend 回归 | `988 passed in 35.07s` |
| `python -m compileall -q backend/app` | passed |

真实 PostgreSQL integration 不属于本阶段验收，完整回归继续排除 `backend/tests/persistence/test_postgres_persistence.py`；本阶段没有数据库 schema 或持久化格式变更。

## 5. 后续边界

Milestone 4 可以把真实 parser session 的合法 `ParsedIntentV1` 交给同一个 resolver，并把唯一 `AnalysisContext` 持久化为 `query_understood v1`。真实 geocoder/NLDI provider adapters、topology BFS、observation/evidence 和水体模型仍属于后续 Milestones，本提交没有提前实现或声明这些能力。
