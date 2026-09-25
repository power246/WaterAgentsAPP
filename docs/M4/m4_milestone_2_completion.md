# M4 Milestone 2 完成记录

记录日期：2026-09-25

实现分支：`codex/m4-query-topology`

## 1. 阶段结论

Milestone 2 已完成 154 项参数 capability manifest、启动期不可变注册表、完整目录候选发现 port 与确定性 parameter resolver。参数候选现在可以得到唯一 capability、明确歧义、不支持、缺少参数或超过上限的稳定结果，并自动组合唯一 required support `usgs:00060`。

本阶段没有创建 parser session、接入 workflow/HTTP、查询远端 availability/observation、构建 topology 或调用水体模型。

## 2. 唯一 capability manifest

- `parameter_capabilities.v1.json` 是随 Python 包发布的唯一有限能力清单，精确包含 154 个 `usgs:<code>`。
- 每项登记 canonical/official name、canonical/official unit、官方 group、medium、sample fraction、中英文 aliases、target/support role、WDFN/Samples availability 与 observation route、允许的 indirect evidence rule IDs、fixture identity。
- key 集合 SHA-256 为 `3f9a0146b474c6c637adf837774b0680c389025cb60771fae55f11ab13d2c8bb`。
- 与设计目录双向离线比较结果为 `document=154, manifest=154, difference=0`；USGS Parameter Codes v1 两页快照也返回全部 154 个设计 key。
- manifest 使用 `resolution_enabled` 表示 M2 确定性解析资格。它不表示 M5 observation/evidence adapter 已完成，不能用作 154 项最终 evidence 能力声明。

## 3. 注册表与 resolver 契约

- `ParameterCapabilityRegistry` 启动时一次校验 154 数量、唯一 key/fixture/canonical name、规定分组计数、provider/evidence enum 引用、别名完整性、key 摘要和唯一 required support；任一错误均启动失败。
- registry、其中的 Pydantic capability 和公开映射均不可变；默认 loader 使用 package resource 与进程内单例缓存，不依赖当前工作目录或仓库路径。
- `ParameterCatalogPort.find_candidates()` 只负责完整目录候选发现；候选分数保留为诊断，不作为可执行 key 的最高分裁决。
- resolver 先查唯一 canonical name，再查显式 alias；跨 capability 的 alias 冲突保持为 `parameter_ambiguous`。只有一个目录候选且 registry 存在该 key 时才成功。
- `ParameterConcept` 的原始 concept、rationale 与 `explicitly_named` 在 unique/ambiguous/unsupported 结果中保留；任一参数失败时不删除失败项，也不暴露部分可执行计划。
- 用户目标按 `ParameterKey` 去重后最多 6 项；第 7 项使整体成为 `too_many_parameters`。`usgs:00060` 自动加入 supporting list，不占上限；它本身是目标时不重复。

## 4. 验证证据

| 验证 | 结果 |
|---|---|
| M2 capability/resolver/manifest focused + architecture | `111 passed in 2.03s` |
| `python -m pytest tests/domain tests/application -q` | `522 passed in 8.06s` |
| `python -m pytest tests/architecture -q` | `76 passed in 0.82s` |
| 完整非 PostgreSQL backend 回归 | `927 passed in 32.25s` |
| 离线 manifest audit | `154 capabilities / 154 unique keys / 154 resolution-enabled / 1 required support` |
| 原始设计目录与 manifest key 双向比较 | `154 / 154 / difference 0` |
| wheel 隔离构建 | passed，SHA-256 `e7427388de24ddff3588bd242d0f4ddc59e8dbf3b26b7e0acb9e12646c6f77ae` |
| wheel 临时安装后从不同 cwd 加载 | `154 154 00060` |
| `python -m compileall -q app scripts` | passed |

真实 PostgreSQL integration 不属于本阶段验收，完整回归继续排除 `tests/persistence/test_postgres_persistence.py`；本阶段没有数据库 schema 或持久化格式变更。

## 5. 后续边界

Milestone 3 可以消费本阶段的成功解析结果，组合 target/date/model/memory mode 并建立纯确定性的 `AnalysisContext`。真实 parser session 属于 Milestone 4，USGS/NLDI/geocoding adapters 与 observation/evidence 完成门禁属于 Milestone 5 及以后；本提交不提前宣称这些能力已接通。
