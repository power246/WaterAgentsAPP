# M4 Milestone 1 完成记录

记录日期：2026-09-25

实现分支：`codex/m4-query-topology`

## 1. 阶段结论

Milestone 1 已完成最终 create request、唯一 run configuration、模型目录、动态日期校验和创建幂等顺序的纵向实现。所有新 run 都使用扩展后的 `analysis_request v1` 与 `lifecycle_event v2`；没有增加旧请求兼容入口、configuration version/union、parameter resolver、NLDI 或 BFS。

## 2. 已实现契约

- create body 必填 `query`、`parser_model`、`waterbody_model`、`memory_mode`，可选 `target`、`date_range`、`time_resolution`；未知字段和显式 null 按稳定 violation 拒绝。
- `target.comid` 只接受无前导零的正十进制字符串；coordinates 使用严格有限经纬度；target 至少提供一种身份。
- structured date 必须完整、顺序正确，并使用 run 的服务端 timezone 与同一次 Clock 快照校验 `today±7` 闭区间。
- `AnalysisConfigurationSnapshot` 只保留 timezone、resolution preference/source、parser model、waterbody model、memory mode；不伪造实际 resolution。
- `ModelCatalogPort` 与单一 runtime `ModelCatalog` 按 `query_parser` / `waterbody` role 校验安全 provider/model identity。catalog schema 不接受 endpoint、credential 或 API key。
- 创建链路先按 idempotency key + 原始 bytes 查找已有创建。相同 bytes 重放直接返回原回执，不读取新 Clock/catalog；只有新 key 才各取一次快照做动态校验。
- composition root 一次解析模型目录、topology hard limits、prediction baseline、parser/topology timeout；非法类型、非有限值和越界值启动失败。
- 实际 OpenAPI 已核对最终字段、required、enum、嵌套 date required 和非 nullable 形状。临时导出 SHA-256：`C821D1DE439C5C911AC6E57CCE5668BF5FCC29CCC4EF777CE747A90672D10A17`。

## 3. 验证证据

| 验证 | 结果 |
|---|---|
| 最终 request/OpenAPI/幂等 focused | `142 passed in 5.17s` |
| API + application create + persistence config + architecture | `321 passed in 10.88s` |
| architecture | `75 passed in 0.66s` |
| observability + socket smoke tests | `86 passed in 14.60s` |
| 完整非 PostgreSQL backend 回归（排除 real-PostgreSQL 文件） | `891 passed in 39.40s` |
| `python -m compileall -q app scripts` | passed |
| `git diff --check` | passed |

真实 PostgreSQL integration 与正式数据库 smoke 本次为 `not run`：隔离 worktree 没有 `WATERAGENTS_DB_PASSWORD`，专用数据库测试在建立连接前即被配置门禁拒绝；没有 provider 请求，也没有把零连接记为通过。CI 已配置 M1 smoke 使用的安全测试模型目录，具备数据库凭据的正式流水线会继续运行完整 persistence 和 history/restart smoke。

## 4. 后续边界

Milestone 2 可以在当前最终请求、不可变配置和模型身份上增加 154 项参数 capability 与 deterministic resolver。当前 workflow 仍不调用 parser，不构建 topology，也不包含任何后续里程碑的临时行为。
