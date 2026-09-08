# M1 Project Shell + Domain Skeleton Implementation TODO

技术契约以 [m1_project_shell_domain_design.md](m1_project_shell_domain_design.md) 为准，总体边界以 [../backend_refactor_milestones.md](../backend_refactor_milestones.md) 和 [../backend_architecture_skeleton.md](../backend_architecture_skeleton.md) 为准。每个子 Milestone 只引入当前阶段需要的代码，完成后运行对应聚焦测试并回写本清单。

## 执行原则

- 新代码只写入 `backend/`。`backend_bk/`、`frontend_bk/` 与旧契约文档完全排除在新后端上下文外，不得用来推导功能或实现。
- 不读取、导入、复制、适配、枚举或测试旧后端或旧前端的代码和契约；新后端不为任何旧行为添加兼容或特殊处理。
- M1 只实现 project shell、Domain、窗口规划、ports 协议和测试；不提前实现 session、SSE、transcript、数据库、evidence tools、Agent、RAG 或真实 adapter。
- Python 最低版本为 3.11；Domain 使用 Pydantic v2；同一份新后端代码必须支持 macOS、Windows 和 Linux。
- 生产代码使用 `pathlib`/`tempfile` 等标准库跨平台抽象，不硬编码 POSIX 路径、`/tmp`、盘符、shell 命令或 macOS 专用能力。
- Domain 只能依赖标准库、Pydantic 和 `app.domain`，不能依赖 FastAPI、数据库、LLM framework、外部 API SDK 或其他应用层。
- 采用一类一文件，不把多个核心对象重新合并成通用 `models.py`。
- Domain model 使用 `extra=forbid`、`frozen=true`、`str_strip_whitespace=true`、`allow_inf_nan=false`。
- Pydantic frozen 只保证浅层不可变；动态 JSON payload 在边界深拷贝，不新增 FrozenDict 或第三方不可变集合依赖。
- 第一版只建模 COMID，不新增 `Waterbody`。
- 所有时间事实存 UTC；窗口按固化的 IANA timezone 的当地墙上时钟推进。
- 不用裸 `dict[str, Any]` 或第三方 SDK 对象绕过尚未定型的契约；后期 DTO 使用 generic Protocol 占位。
- 如果实现需要改变已确认契约，先更新设计文档、总 milestones 和本 TODO，再继续编码。
- 保留当前工作区已有改动，不做无关格式化、重构、依赖升级或清理。

## 成功标准

- `backend/` 是可安装、可启动、可测试的 Python 3.11+ 工程。
- `python -m uvicorn app.main:app --app-dir backend` 可以启动，`GET /health` 精确返回 `{"status":"healthy"}`。
- M1 约定的 Domain 对象都有真实类型、校验和 JSON round-trip 测试。
- `TimeResolution` 只接受 `6h / 12h / 24h`，默认 `12h`，并保留 `time_resolution_source`。
- `build_time_windows` 正确处理普通日期、DST、重复本地时间和从请求起点滚动的窗口。
- 每个参数覆盖所有计划窗口；缺少可用证据时使用 `indeterminate`，不省略 point。
- `TimeWindowResult` 正确表达 `quantified / directional / indeterminate`，quantified 可附带风险/趋势。
- Evidence 的 collection status、provenance、ParameterKey 和引用完整性成立。
- Domain 依赖扫描证明没有 FastAPI、数据库、LLM provider 或外部 API 依赖。
- `backend/` 的生产代码、包配置和测试不依赖或引用 `backend_bk/`、`frontend_bk/` 与旧契约文档，也没有引入 M2-M9 运行时实现。
- project shell、配置、路径和启动方式不依赖当前开发机，macOS、Windows、Linux 使用同一份代码和 `pyproject.toml`。

## 已确认设计结论

- 默认分析时区为 `America/Chicago`，以后可动态修改；每个 session 必须固化实际时区。
- 第一版请求只支持日期范围，结束日期包含整天。
- `6h / 12h / 24h` 是当地墙上时钟跨度。DST 日的 5/7 小时和 23/25 小时 elapsed duration 合法。
- 只有真实 IANA offset transition 可以解释完整窗口的 ±1 小时差异，不能设置宽泛时长容差。
- UTC start/end 是窗口唯一身份，重复本地时间不能互相覆盖。
- 未来自定义时间从请求起点滚动，例如 `03:20-09:20`，不对齐固定 `00/06/12/18` 网格。
- 参数身份使用 `ParameterKey(code_system, code)`，第一版默认 `code_system=usgs`。
- COMID 和 station 缺少坐标时使用 `None`，不能用 `0,0` 表示缺失。
- Evidence Tool 构造 `EvidenceFragment`；adapter 只返回标准化 source record。
- `collection_status` 区分 `available / empty / unavailable / invalid`。
- evidence ID 只要求 session 内唯一；跨 session 源记录由 provenance 识别。
- quantified 必须有数值且可选风险/趋势；directional 无数值但有明确方向或风险；indeterminate 无数值并带 reason code。
- confidence 只校验 `[0,1]`，不与 outcome status 硬绑定。
- 归因必须带 `basis: observed | correlated | inferred | hypothesized`。
- 多站冲突由 M6 Agent 判断；原样采用有效观测可标 observed，任何加权、折中或综合结果标 inferred。
- 最终结果保存 evidence 引用和摘要；完整上下文由 M3 transcript 保存并按需加载。

## Milestone 0：实现基线与工作区确认

目标：确认新后端起点、工具链和用户已有改动，不开始生产代码。

- [x] 执行 `git status --short --branch`，记录当前分支和已有 rename/untracked 文件。
- [x] 确认 `backend/` 当前没有需要保留的新架构实现；如已有文件，逐项判断归属，不直接覆盖。
- [x] 确认 `backend_bk/` 和 `frontend_bk/` 是完全排除在新后端上下文外的独立归档。
- [x] 确认正式技术设计为 `docs/M1/m1_project_shell_domain_design.md`。
- [x] 确认总 milestones 已移除第一版 `Waterbody` 并登记所有 deferred contracts。
- [x] 执行 `python --version`，确认实际解释器为 Python 3.11+。
- [x] 确认当前环境可创建隔离虚拟环境并安装 `pyproject.toml` 依赖。
- [x] 确认新后端独立建立 Python packaging/test 配置，不从旧后端归档读取或复用配置。
- [x] 记录 M1 开工前的测试基线；如果没有新后端测试，明确记录为“尚无 M1 test suite”。

验证点：

- [x] 能说明哪些工作区改动属于用户，M1 不会覆盖哪些文件。
- [x] 能说明新 backend 的启动目录、package import root 和测试执行目录。
- [x] 准备阶段没有产生 backend 生产代码修改。

### Milestone 0 基线记录（2026-09-07）

- 起始分支为 `codex/m1-design`，跟踪 `origin/m1-design`；工作树干净，没有 rename、untracked 或其他用户未提交改动。基线确认后创建并切换到 `codex/m1-implementation`。
- `backend/` 为空，没有需要保留或判断归属的新架构文件。后续 M1 不覆盖 `backend_bk/`、`frontend_bk/` 或根目录中的旧项目文件。
- `backend_bk/` 和 `frontend_bk/` 是完全独立的旧系统归档；本 Milestone 没有修改它们，也没有从中推导新后端契约或实现。
- 正式技术设计为 `docs/M1/m1_project_shell_domain_design.md`。总 milestones 中第一版业务身份只保留 COMID，`Waterbody` 仅出现在 Future deferred contract；M3-M9 与 Future 的延后契约均已登记。
- 当前 macOS 开发机的系统默认 `/usr/bin/python3` 为 Python 3.9.6，不能用于 M1。已明确选择 Codex bundled Python 3.12.14 作为本机 M1 解释器，并在 `/private/tmp/wateragents-m0-venv` 成功创建隔离虚拟环境；这些绝对路径只属于 M0 本机验证，不得进入生产代码、配置、测试或用户文档。
- `backend/pyproject.toml` 按设计要在 Milestone 1 创建，因此 M0 无法执行尚不存在文件的 editable install。为验证安装能力，已在临时 Python 3.12 环境中安装设计声明的生产/开发依赖（FastAPI、Pydantic v2、pydantic-settings、Uvicorn、tzdata、pytest、httpx），`pip check` 与导入检查均通过；清空 `PYTHONTZPATH` 后仍能通过 `tzdata` 加载 `America/Chicago`。创建正式 `pyproject.toml` 后仍须执行清单中的 editable install 验收。
- 基线时根目录与空 `backend/` 没有 `pyproject.toml`、setup/pytest/tox 配置；M1 从新后端需求独立建立配置。
- 测试基线：尚无 M1 test suite。
- 新 backend 从仓库根启动，使用 `--app-dir backend`；package import root 为 `backend/`（Python package 为 `backend/app/`）；测试从仓库根执行，目标目录为 `backend/tests/`。
- 准备阶段仅回写本 TODO，没有产生 `backend/` 生产代码修改。

建议命令：

```bash
git status --short --branch
python --version
find backend -maxdepth 3 -type f -print
```

## Milestone 1：Project shell 与健康检查

目标：建立最小可安装工程和无副作用 FastAPI 入口。

### Packaging

- [x] 创建 `backend/pyproject.toml`，设置 Python `>=3.11`。
- [x] 生产依赖仅加入 FastAPI、Pydantic v2、pydantic-settings、Uvicorn 和跨平台 IANA timezone fallback `tzdata`。
- [x] dev 依赖仅加入 pytest 和 httpx。
- [x] 配置 pytest test path，保证从仓库根执行命令时能稳定导入 `app`。
- [x] 不加入 LangChain、LangGraph、SQLAlchemy、数据库 driver、provider SDK、cache 或 retry 库。
- [x] 创建 `backend/README.md`，记录 macOS/Linux POSIX shell 与 Windows PowerShell 的虚拟环境激活方式，以及统一的安装、测试和启动命令。
- [x] 在 `backend/.venv` 创建本机隔离环境并通过 `.gitignore` 排除；各平台从同一 `pyproject.toml` 创建自己的 `.venv`，不提交平台专用二进制。
- [x] 提交跨平台 `backend/uv.lock`；安装了 `uv` 的环境可用一条 `uv run --project backend --extra dev python -m app` 创建/同步隔离环境并启动，未安装时保留标准库 `venv + pip` 路径。

### App 与 settings

- [x] 创建 `backend/app/__init__.py` 及最小 API/config package。
- [x] 实现 `AppSettings`：`app_name`、`environment`、`log_level`、`host`、`port`、`analysis_timezone`。
- [x] 默认 `analysis_timezone="America/Chicago"`。
- [x] 使用 `zoneinfo.ZoneInfo` 校验 IANA timezone；非法配置启动失败，不静默回退。
- [x] 环境变量统一使用 `WATERAGENTS_` 前缀。
- [x] 实现 `create_app(settings: AppSettings | None = None) -> FastAPI` 和模块级 `app`。
- [x] 实现 `GET /health`，精确返回 `{"status":"healthy"}`。
- [x] 启动时不清理文件、不创建数据库、不联网、不启动后台任务。
- [x] 除 `GET /health` 外不添加业务 router、streaming transport、CORS 或 workflow engine 运行时。
- [x] 提供跨平台最短启动入口 `python -m app` 和安装后的 `wateragents` console script。

### 测试

- [x] app 在无 `.env`、API key、网络和数据库时可构造。
- [x] health 返回 HTTP 200 和精确 body。
- [x] 非法 analysis timezone 产生明确配置错误。
- [x] 导入 `app.main` 不产生文件、网络或线程副作用。
- [x] shell/config 测试证明没有硬编码开发机绝对路径、POSIX-only shell/subprocess、`/tmp`、盘符或 macOS-only import。
- [x] `America/Chicago` 的校验不假设操作系统自带 IANA timezone database，Windows 可通过 `tzdata` 获得相同语义。

验证点：

- [x] editable install 成功。
- [x] 聚焦测试通过。
- [x] Uvicorn 手工启动后 health 可访问。

### Milestone 1 完成记录（2026-09-07）

- 本机隔离环境：`backend/.venv`，Python 3.12.14；目录已忽略，不作为跨平台产物提交。
- 最短跨平台入口：提交 `backend/uv.lock`，`uv run --project backend --extra dev python -m app` 自动创建/同步项目内 `.venv` 并启动；本机已验证该入口返回健康响应，`uv` 不是生产依赖。
- editable install：`backend/.venv/bin/python -m pip install -e './backend[dev]'` 成功，`pip check` 无损坏依赖。
- 已安装版本：FastAPI 0.141.1、Pydantic 2.13.5、pydantic-settings 2.15.0、Uvicorn 0.52.4、tzdata 2026.3、pytest 9.1.1、httpx 0.28.1。
- 测试：`backend/.venv/bin/python -m pytest backend/tests -q`，8 passed。
- 时区 fallback：清空 `PYTHONTZPATH` 后，`AppSettings` 仍通过 `tzdata` 加载 `America/Chicago`。
- 进程 smoke test：显式 Uvicorn 命令和 `python -m app` 均在 `127.0.0.1:8000` 成功启动；`GET /health` 返回 HTTP 200 与精确 body `{"status":"healthy"}`；服务已正常停止。
- 平台说明：当前仅在 macOS 完成动态运行；Windows/Linux 的真实环境矩阵验收保留在 Milestone 7，M1 已加入平台无关静态门禁和 `tzdata` fallback 测试。

建议命令：

```bash
python -m pip install -e './backend[dev]'
python -m pytest backend/tests/api/test_health.py -q
python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
python -c "import httpx; response = httpx.get('http://127.0.0.1:8000/health'); response.raise_for_status(); print(response.json())"
```

## Milestone 2：Domain 基类与 Hydrology 对象

目标：建立严格、可序列化、浅层不可变的领域基础类型。

### Common

- [x] 实现 `DomainModel` 的统一 Pydantic 配置。
- [x] 实现递归 `JsonValue` 类型，不允许任意 Python 对象。
- [x] 建立非空字符串、UTC aware datetime、唯一 tuple 等最小复用 validator；不要为了单次使用建立复杂框架。
- [x] JSON payload 进入模型前深拷贝，对外 dump 后修改结果不影响原模型。
- [x] 枚举使用稳定小写字符串值。

### Hydrology

- [x] 实现 `GeoPoint(latitude, longitude)` 和范围校验。
- [x] 实现 `RiverSegment(comid, location, upstream_comids, downstream_comids)`。
- [x] RiverSegment 拒绝重复上下游 COMID 和 self-reference。
- [x] RiverSegment location 允许 `None`；不使用 `0,0` 哨兵。
- [x] 实现 `ParameterKey(code_system, code)`，两字段共同构成参数身份。
- [x] 实现 `WaterQualityParameter(key, name, canonical_unit, is_dimensionless, category)`。
- [x] 非无量纲参数缺少 canonical unit 时校验失败。
- [x] 实现 `MonitoringStation(station_id, source, name, location, associated_comid, available_parameters)`。
- [x] MonitoringStation location 允许 `None`，available ParameterKey 不得重复。
- [x] 不创建 `Waterbody`、lake、reservoir 或 geometry 抽象。

### 测试

- [x] 每个对象覆盖合法构造和 JSON round-trip。
- [x] 空 ID/code system/code/name 被拒绝。
- [x] 非法坐标、重复关系、自引用和重复 ParameterKey 被拒绝。
- [x] `location=None` 合法，真实 `GeoPoint(0,0)` 仍是合法坐标而不是缺失。
- [x] frozen model 不能重新赋值。
- [x] JSON payload defensive copy 行为有测试。

验证点：

- [x] Hydrology 对象没有 FastAPI、ports 或 infrastructure import。
- [x] ParameterKey 在 dump/load 后保留 code system 和前导零。

### Milestone 2 完成记录（2026-09-07）

- Common：实现 `DomainModel`、`DomainEnum`、递归 `JsonValue`、`NonEmptyString`、`UtcDatetime` 和唯一 tuple 校验；模型统一启用 `extra=forbid`、`frozen=true`、字符串 trim 与非有限数拒绝。
- Hydrology：按一类一文件实现并导出 `GeoPoint`、`RiverSegment`、`ParameterKey`、`WaterQualityParameter`、`MonitoringStation`，未创建 `Waterbody` 或其他超范围抽象。
- 聚焦测试：`python -m pytest backend/tests/domain/test_hydrology.py backend/tests/domain/test_serialization.py -q`，31 passed。
- 完整回归：`python -m pytest backend/tests -q`，39 passed。
- 依赖回扫：`backend/app/domain` 未发现 FastAPI、ports、infrastructure、数据库、HTTP/LLM framework 或 `Any` import；`git diff --check` 通过。

建议测试：

```bash
python -m pytest backend/tests/domain/test_hydrology.py backend/tests/domain/test_serialization.py -q
```

## Milestone 3：Time 对象与墙上时钟窗口规划

目标：固定 UTC 存储、本地墙上时钟切窗和 DST 语义。

### Time objects

- [x] 实现 `TimeResolution`，只接受 `6h / 12h / 24h`。
- [x] 实现 `TimeResolutionSource`：`frontend / default / auto`。
- [x] 实现 `TimeRange(start, end)`；拒绝 naive、相等和反向时间。
- [x] aware datetime 进入 Domain 时统一转 UTC。
- [x] 实现无副作用 `contains` 和 `overlaps`。
- [x] 实现 `InferenceMode`：`gap_filling / prediction / mixed`。
- [x] 实现 `TimeSegment`，只允许 gap filling 或 prediction，不允许 mixed segment。
- [x] 含 resolution 的模型默认 `12h`。

### Window builder

- [x] 实现 `build_time_windows(time_range, time_resolution, analysis_timezone)`。
- [x] 校验 analysis timezone 为有效 IANA 名称。
- [x] 从请求起点对应的本地墙上时间滚动 6/12/24 小时。
- [x] 每个本地边界转换为唯一 UTC instant 后写入 TimeRange。
- [x] 最后一个窗口不能超过请求 end；不足完整墙上时钟跨度时截断。
- [x] 第一版日期请求从本地 00:00 开始，可生成 `00-06/06-12/12-18/18-24`。
- [x] 不使用固定 elapsed seconds 直接在 UTC 上加时长。
- [x] 不使用“5 到 7 小时都合法”的宽泛容差；只有 zoneinfo offset transition 可以解释完整窗口时长差。
- [x] 不用格式化后的本地时间字符串作为窗口 key。
- [x] 保留未来自定义时间从请求起点滚动的能力，不实现对应 API。

### DST tests

- [x] 普通日 6h/12h/24h 边界正确。
- [x] `America/Chicago` spring-forward 的 `00-06` elapsed 5h，`00-24` elapsed 23h。
- [x] `America/Chicago` fall-back 的 `00-06` elapsed 7h，`00-24` elapsed 25h。
- [x] fall-back 重复的本地 `01:xx` 使用 fold/UTC 后是两个不同 instant，写入映射时不覆盖。
- [x] 无 offset transition 时，错误的 5h/7h“完整 6h 窗口”不被接受。
- [x] `03:20-17:40` 预留行为生成 `03:20-09:20 / 09:20-15:20 / 15:20-17:40`。
- [x] 尾部截断窗口可被识别，后续可添加 `partial_coverage`。
- [x] 非 America/Chicago 的有效 IANA timezone 至少有一个测试，证明配置不是硬编码。

验证点：

- [x] 生成窗口按 UTC 严格递增、无重叠、完整覆盖 request range。
- [x] DST 测试不依赖机器本地 timezone。
- [x] Time 模块只使用标准库、Pydantic 和 Domain 内部类型。

### Milestone 3 完成记录（2026-09-07）

- Time objects：实现并导出 `TimeResolution`、`TimeResolutionSource`、`TimeRange`、`InferenceMode` 和 `TimeSegment`；统一 UTC、半开区间与 atomic segment mode 约束，并以 `DEFAULT_TIME_RESOLUTION` 固定 `12h` 默认值。
- Window builder：使用标准库 `zoneinfo` 从请求起点按本地墙上时钟推进，UTC `TimeRange` 作为窗口身份，尾窗在 request end 截断；无固定 UTC elapsed duration 容差。
- 聚焦测试：`python -m pytest backend/tests/domain/test_time.py backend/tests/domain/test_time_windows.py -q`，28 passed。
- 完整回归：`python -m pytest backend/tests -q`，67 passed。
- 依赖回扫：Time/Inference 仅依赖标准库、Pydantic 和 Domain 内部类型；`git diff --check` 通过。

建议测试：

```bash
python -m pytest backend/tests/domain/test_time.py backend/tests/domain/test_time_windows.py -q
```

## Milestone 4：Evidence Domain

目标：固定 evidence envelope、采集状态、provenance 和引用关系，不实现真实采集。

### Enums 与 supporting objects

- [x] 实现 `CollectionStatus`：`available / empty / unavailable / invalid`。
- [x] 实现 `DataQualityFlag`：`missing`、`partial_coverage`、`possible_freezing`、`anomalous`、`estimated`、`historical_inference`、`stale`、`provider_flagged`、`unit_unresolved`、`conflicting_observations`。
- [x] 实现 `EvidenceLocation(point, comid, station_id, distance_to_target_km)`。
- [x] EvidenceLocation 至少包含 point/comid/station_id 之一，距离不能为负。
- [x] 实现 `Provenance(provider, retrieved_at, source_uri, source_record_ids, source_timezone, source_utc_offset, metadata)`。
- [x] provenance timestamp 统一 UTC；源 timezone/offset 可以保留。

### Evidence models

- [x] 实现 `EvidenceFragment` 全部 envelope 字段。
- [x] parameter refs 使用 ParameterKey，不使用裸 code。
- [x] value/summary 只接受 JsonValue。
- [x] confidence hint 只校验 `[0,1]`。
- [x] rag eligible 默认 false。
- [x] empty/unavailable/invalid 可以携带结构化原因或 null payload。
- [x] 实现 `EvidenceBundle`，固化 COMID、time range、analysis timezone、resolution/source 和 requested parameters。
- [x] EvidenceBundle 允许没有 available evidence，但 requested parameters 至少一个且唯一。
- [x] 同一 bundle 的 evidence ID 不得重复；fragment 允许来自上游 COMID。
- [x] 实现 `EvidenceReference(evidence_id, role, summary)` 和 `EvidenceChain(references)`。
- [x] EvidenceChain 非空且 evidence ID 唯一，只保存引用和摘要，不嵌入 fragment。

### 测试

- [x] 四种 collection status 均可 round-trip。
- [x] empty 与 unavailable 在序列化后保持不同语义。
- [x] 重复 evidence ID、重复 ParameterKey、非法位置/距离/confidence 被拒绝。
- [x] evidence ID 不承担跨 session 稳定性；source record IDs 完整保留。
- [x] 修改原始 payload 或 dump 后 payload 不改变已创建模型。

验证点：

- [x] Evidence Domain 没有真实 adapter、HTTP client、tool registry 或 source-specific payload class。
- [x] Adapter 构造 EvidenceFragment 的行为没有出现在 M1。

### Milestone 4 完成记录（2026-09-07）

- Supporting objects：实现并导出 `CollectionStatus`、`DataQualityFlag`、`EvidenceLocation` 和 `Provenance`，包含位置身份、非负距离、UTC retrieval timestamp 与来源 metadata 约束。
- Evidence envelope：实现 `EvidenceFragment`、`EvidenceBundle`、`EvidenceReference` 和 `EvidenceChain`；ParameterKey、bundle/chain 局部唯一性、默认 `12h` resolution、IANA timezone、null/结构化原因和 reference-only chain 均按契约校验。
- 聚焦测试：`python -m pytest backend/tests/domain/test_evidence.py backend/tests/domain/test_serialization.py -q`，44 passed。
- 完整回归：`python -m pytest backend/tests -q`，102 passed。
- 依赖回扫：Evidence Domain 未引入 adapter、HTTP client、tool registry、source-specific payload、ports 或 infrastructure；`git diff --check` 通过。

建议测试：

```bash
python -m pytest backend/tests/domain/test_evidence.py backend/tests/domain/test_serialization.py -q
```

## Milestone 5：Inference 与 Attribution Domain

目标：实现三态时间窗口结果、完整 series、归因和可折叠分析上下文。

### Outcome

- [x] 实现 `OutcomeStatus`：`quantified / directional / indeterminate`。
- [x] 实现 `ResultSource`：`observed / inferred / predicted`。
- [x] 实现 aggregation、trend、risk 和 indeterminate reason 枚举。
- [x] 实现 `RiskOrTrendAssessment(summary, trend, risk_level, reason_codes)`。
- [x] quantified 必须有 finite value，assessment 可选。
- [x] directional 禁止 value，且必须有至少一个确定的 trend/risk，不能只填 uncertain/indeterminate。
- [x] indeterminate 禁止 value，assessment 和 reason code 必填。
- [x] reason code 只允许出现在 indeterminate。
- [x] 三种状态都只对 confidence 做 `[0,1]` 校验，不增加状态绑定规则。
- [x] 所有窗口 evidence refs 非空且唯一。

### Series 与 result

- [x] 实现 `ParameterTimeSeries`，包含 parameter、resolution/source、points 和 parameter confidence。
- [x] points 按 UTC start 严格升序且不重叠。
- [x] 每个 `build_time_windows` 计划窗口恰好有一个 point；缺少/额外 point 均失败。
- [x] 不以 UTC elapsed duration 判断 resolution，使用 window plan 校验。
- [x] 实现 `InferenceResult` 的 target COMID、mode、time range/segments、analysis timezone、resolution/source、series、overall confidence、attribution、analysis detail、evidence chain 和 generated time。
- [x] ParameterKey 在 series 中唯一。
- [x] time segments 有序、无重叠、无缺口并完整覆盖 request range。
- [x] mixed 同时包含 gap filling 和 prediction segment。
- [x] prediction segment 只接受 predicted source；gap filling 接受 observed/inferred source。
- [x] 每条 series 的 resolution/source 与 result 一致。
- [x] window、attribution、analysis step 的 evidence refs 必须存在于 EvidenceChain。

### Attribution 与 detail

- [x] 实现 attribution direction/magnitude/basis 枚举。
- [x] 实现 `AttributionFactor`，每项必须有 basis、explanation 和 evidence refs。
- [x] 实现 `AttributionSummary`；无 factor 时允许用 summary 说明证据不足。
- [x] 实现 `AnalysisStep(type, title, content, evidence_refs, transcript_item_refs)`。
- [x] 实现 `AnalysisDetail(summary, steps, transcript_item_refs)`。
- [x] analysis detail 只保存可折叠 timeline 内容和引用，不复制完整 EvidenceFragment。
- [x] 不在 M1 实现 transcript lazy loader、prompt/raw output 持久化或 secret redaction。

### 测试

- [x] quantified value-only 和 value + assessment 均通过。
- [x] directional/indeterminate 的缺失字段和非法字段组合均失败。
- [x] confidence=0 或 1 在任意 outcome status 均可通过，证明没有隐藏硬绑定。
- [x] 完整无证据样例为所有窗口构造 indeterminate，并引用 empty/unavailable evidence。
- [x] series 缺点、额外点、错边界、乱序和重叠均失败。
- [x] evidence chain 外引用失败。
- [x] attribution basis 和 analysis timeline JSON round-trip。

验证点：

- [x] 模型只校验结构一致性，不实现 SufficiencyPolicy 或 confidence 算法。
- [x] 没有 LLM prompt、parser、Agent 或跨服务兼容字段。

### Milestone 5 完成记录（2026-09-07）

- Outcome：实现三态 `TimeWindowResult`、结果来源及 aggregation/trend/risk/reason 枚举；finite value、方向性判断、结构化空判断、confidence 和 evidence refs 均按契约校验。
- Series/result：实现 `ParameterTimeSeries` 与 `InferenceResult`，使用 `build_time_windows` 校验完整窗口计划，并校验 ParameterKey、resolution/source、segment coverage/mode/source 及 EvidenceChain 引用闭包。
- Attribution/detail：实现 direction/magnitude/basis、`AttributionFactor`、`AttributionSummary`、`AnalysisStep` 和 `AnalysisDetail`；只保存 JSON timeline 内容与引用。
- 聚焦测试：`python -m pytest backend/tests/domain/test_results.py backend/tests/domain/test_serialization.py -q`，55 passed。
- 完整回归：`python -m pytest backend/tests -q`，148 passed；所有 Domain 包入口可独立导入，`git diff --check` 通过。
- 依赖回扫：未实现 SufficiencyPolicy、confidence 算法、LLM prompt/parser/Agent、transcript loader 或跨服务兼容字段。

建议测试：

```bash
python -m pytest backend/tests/domain/test_results.py backend/tests/domain/test_serialization.py -q
```

## Milestone 6：Ports 与异常契约

目标：为后续能力提供稳定入口，不提前猜测尚未设计的 DTO。

### Stable protocols

- [x] 实现 `ClockPort.now() -> UTC datetime`。
- [x] 实现 `GeocodingPort.resolve(location_text) -> GeoPoint | None`。
- [x] 实现 `ComidLookupPort.lookup_comid(point) -> str | None`。
- [x] 实现 `TopologyDataPort.get_segment(...)` 和 `get_direct_upstreams(...)`。
- [x] 实现 `MonitoringStationPort.find_for_segment(...)`，参数身份使用 ParameterKey。

### Generic protocols

- [x] 为 WaterObservation、WeatherObservation、WeatherForecast、FacilitySearch 定义 RequestT/RecordT generic Protocol。
- [x] 为 LLM 定义 RequestT/ResponseT generic Protocol。
- [x] 为 TranscriptStore、RAGIndex、EventPublisher、Cache 定义最小 generic 操作语义。
- [x] 为 KeyStore 保留模块和 Protocol；masked status DTO 延后到 M9。
- [x] 不使用 `Any`、裸 dict 或 EvidenceFragment 代替尚未定型的 source record。
- [x] 不创建 fake/real adapter；它们在使用这些 port 的 milestone 引入。

### Errors

- [x] 实现 `PortError`、`PortUnavailableError`、`PortTimeoutError`、`InvalidPortResponseError`。
- [x] 找不到使用 None/空 tuple，不作为 PortError。
- [x] 协议 docstring 明确 empty 与 unavailable 不同。

### 测试

- [x] 所有约定 port 可从 `app.ports` 导入。
- [x] Protocol 方法签名和 async/sync 语义符合设计。
- [x] port modules 不导入 infrastructure、FastAPI 或第三方 SDK。

验证点：

- [x] M1 port 能为 M2-M5 提供 import-stable 入口。
- [x] 没有为了通过 import test 创建无意义 adapter 或 runtime wiring。

### Milestone 6 完成记录（2026-09-07）

- Stable protocols：实现同步 `ClockPort` 与异步 geocoding、COMID、topology、monitoring station 端口，返回 Domain 类型并使用 `ParameterKey` 表达参数身份。
- Generic protocols：实现 observation/weather/forecast/facility、LLM、transcript、RAG、event publisher、cache 和 key store 的类型安全泛型入口；KeyStore masked status DTO 保持延后。
- Errors：实现统一 `PortError` 层级；查询协议 docstring 明确 None/空 tuple 是成功但无结果，unavailable 属于外部失败。
- 聚焦测试：`python -m pytest backend/tests/architecture/test_port_imports.py -q`，27 passed。
- 完整回归：`python -m pytest backend/tests -q`，175 passed；`app.ports` 公开入口导入和 `git diff --check` 通过。
- 架构回扫：Ports 只依赖标准库、Domain 和 Ports 自身；无 `Any`、裸 dict、EvidenceFragment、第三方 SDK、infrastructure、生产 adapter 或 runtime wiring。

建议测试：

```bash
python -m pytest backend/tests/architecture/test_port_imports.py -q
```

## Milestone 7：架构门禁与完整回归

目标：用自动化测试证明 M1 边界，而不是只依赖目录约定。

### Dependency test

- [x] 使用 Python AST 扫描 `backend/app/domain/**/*.py`。
- [x] allowlist 只包含标准库、Pydantic 和 `app.domain`。
- [x] 明确拒绝 FastAPI、SQLAlchemy、httpx、LangChain、LangGraph、provider SDK、app.api、app.ports、app.infrastructure 及后续应用层。
- [x] 扫描 import 和 from-import 两种语法。
- [x] 测试失败信息包含违规文件、行号和 import 名称。

### Contract sweep

- [x] 搜索所有 Domain class 名称，确认一类一文件且导出完整。
- [x] 搜索 `Waterbody`，确认只存在文档中的 deferred 描述，不存在 M1 生产类型。
- [x] 检查新 backend 的生产代码与包配置，确认不引用 `backend_bk/`、`frontend_bk/` 或旧契约文档；不枚举或判断旧接口内容。
- [x] 搜索 Domain 中的 `Any`、FastAPI、数据库、LLM 和 SDK import。
- [x] 搜索 datetime 构造和 serialization，确认没有 naive timestamp 漏入。
- [x] 搜索窗口 key，确认没有只用本地格式化时间字符串。
- [x] 搜索 ParameterKey 调用，确认没有退回裸 USGS code 去重。
- [x] 搜索 evidence refs，确认 result/attribution/detail 引用均经过 cross validation。

### Full tests

- [x] 运行全部 M1 tests。
- [x] 运行 `git diff --check`。
- [x] 在干净 Python 3.11+ 虚拟环境执行 editable install 和测试。
- [x] 手工启动 Uvicorn 并验证 health。
- [x] 确认测试不访问外部网络、不依赖 `.env`、API key、数据库或本机 timezone；health smoke 仅使用 loopback。
- [x] 确认生产代码和测试使用 `pathlib`/`tempfile` 等跨平台 API，不依赖 POSIX shell、路径分隔符、`/tmp`、盘符、macOS-only import、`fork` 或大小写敏感文件系统。
- [ ] 在 macOS、Windows、Linux CI 或等效干净环境中分别执行 editable install、完整测试和 health smoke test，并记录结果。

验证点：

- [x] `python -m pytest backend/tests -q` 全绿。
- [x] 架构测试对一个临时禁止 import 能稳定报错，移除后恢复通过。
- [ ] 所有 M1 成功标准均有自动化或明确手工验收证据。

建议命令：

```bash
python -m pytest backend/tests -q
rg -n 'fastapi|sqlalchemy|httpx|langchain|langgraph|Any' backend/app/domain
rg -n 'Waterbody' backend/app
rg -n 'backend_bk|frontend_bk|backend_refactor_contract' backend/app backend/pyproject.toml
git diff --check
```

### Milestone 7 本地完成记录（2026-09-08）

- 架构门禁：新增 AST dependency allowlist、Domain 一类一文件/导出完整性、deferred type、UTC model field、窗口身份、叶子模块导入和跨平台 CI contract 测试；临时禁止 import 的失败信息包含文件、行号和 import 名称，移除后恢复通过。
- 结构整理：将 `DomainModel` 与 `DomainEnum` 拆分到各自文件，同时保留 `app.domain.common` 兼容导入。
- 干净环境：Python 3.12.14（满足 3.11+）；`python -m pip install -e './backend[dev]'` 成功，`python -m pip check` 无 broken requirements。
- 生产依赖：FastAPI 0.141.1、Pydantic 2.13.5、pydantic-settings 2.15.0、Uvicorn 0.52.4、tzdata 2026.3；测试依赖 pytest 9.1.1、httpx 0.28.1。
- 完整回归：`python -m pytest backend/tests -q`，186 passed；`python backend/scripts/health_smoke.py` 返回 `{"status":"healthy"}`；`git diff --check` 通过。
- 跨平台入口：新增 GitHub Actions 的 Ubuntu/macOS/Windows Python 3.11 matrix，每个平台执行 editable install、完整测试和 health smoke；本地已验证 macOS，Windows/Linux 结果需在分支提交并推送后由 CI 记录，因此对应两项暂不勾选。

## Milestone 8：收尾与接力

目标：把 M1 留在可复用、可审计、后续可以直接接力的状态。

- [x] 回看正式设计，逐条确认实现没有遗留被推翻的旧假设。
- [x] 回看 [../deferred_milestone_contracts.md](../deferred_milestone_contracts.md)，确认 M1 没有抢跑 M3 及之后的职责。
- [x] 在本 TODO 中勾选实际完成项；未完成项不能因为“后续会做”而标记完成。
- [x] 对任何与设计不同但已获确认的实现，同步更新设计和总 milestones。
- [x] 在本节追加完成记录：日期、Python 版本、依赖版本、测试命令和结果。
- [x] 记录 M2 可直接复用的 app factory、settings snapshot 字段和 EventPublisherPort 入口。
- [x] 记录 M3/M4/M5/M6 需要补全的 generic DTO，不在 M1 提前实现。
- [x] 确认没有修改或依赖 `backend_bk/`，没有覆盖用户无关改动。

最终验证：

- [x] `python -m pip install -e './backend[dev]'` 成功。
- [x] `python -m pytest backend/tests -q` 全绿。
- [x] `python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000` 可启动。
- [x] 使用 Python HTTP client 请求 `http://127.0.0.1:8000/health`，返回 `{"status":"healthy"}`。
- [x] `git diff --check` 通过。

### Milestone 8 完成记录（2026-09-08）

- 完成日期：2026-09-08。
- 干净环境：Python 3.12.14；`python -m pip install -e './backend[dev]'` 成功，`python -m pip check` 无 broken requirements。
- 生产依赖：FastAPI 0.141.1、Pydantic 2.13.5、pydantic-settings 2.15.0、Uvicorn 0.52.4、tzdata 2026.3；测试依赖 pytest 9.1.1、httpx 0.28.1。
- 测试命令：`python -m pytest backend/tests -q`；结果：186 passed。
- 启动验收：使用清单中的显式 Uvicorn 命令启动成功；独立 Python/httpx client 请求 `/health` 返回 HTTP 200 和 `{"status":"healthy"}`，随后正常关闭进程。
- 边界审计：实现相对 M1 设计基线没有修改或依赖 `backend_bk/`、`frontend_bk/` 或旧契约文档，没有 session/SSE/transcript/adapter/Agent/RAG/admin 运行时；M8 开始前工作区干净，未覆盖用户无关改动。
- 设计同步：M7 为满足一类一文件拆分了 `DomainModel` 与 `DomainEnum`，已同步详细设计的目录树；总 milestones 的行为契约无需变更。
- 新旧边界修订（2026-09-08）：旧后端和旧前端归档不作为业务、数据源、交互或契约参考；新后端架构测试只检查自身设计不变量，不包含旧系统接口、事件、DTO、组件或字段清单。
- 已知限制：M7 的 Ubuntu/macOS/Windows workflow 已配置，本机 macOS 验收通过；远端 Windows/Linux CI 尚未执行，因此 M7 的跨三平台结果及 M1 全成功标准两项继续保持未勾选。

#### M2 可直接复用入口

- App factory：`app.main.create_app(settings: AppSettings | None = None) -> FastAPI`；模块级 `app` 可直接供 Uvicorn 使用，解析后的 settings 保存在 `application.state.settings`。
- Session snapshot：创建 session 时从 `AppSettings.analysis_timezone` 固化实际时区，并同时固化请求解析出的 `TimeResolution` 与 `TimeResolutionSource`；后续全局设置变化不得修改已有 snapshot。
- Events/time：从 `app.ports` 导入异步 `EventPublisherPort[EventT].publish(event)`，由 M2 定义 lifecycle event DTO；时间戳可通过同步 `ClockPort.now()` 注入。

#### 后续 generic DTO 接力

M3 及之后的完整功能约定以 [../deferred_milestone_contracts.md](../deferred_milestone_contracts.md) 为准；下表只记录 M1 已留下的 generic port 接力点。

| 总里程碑 | 需要在获得真实上下文后定型的类型 |
|---|---|
| M3 | 为 `TranscriptStorePort[ItemT]` 定义版本化 `TranscriptItem` 和 history DTO；content 使用 `content_type + content_schema_version`，写入前 redaction，大 payload 使用 preview + reference。 |
| M4 | 定义 analysis request/context、日期 mapper、ParameterResolver result、`HydrologyGraph` 和 `query_understood/topology_ready` event DTO；现有 geocoding/COMID/topology/station ports 已返回稳定 Domain 类型，不需要猜测 source record。 |
| M5 | 为 `WaterObservationPort`、`WeatherObservationPort`、`WeatherForecastPort`、`FacilitySearchPort` 补全各自 Request/Record DTO，并按需要定型 `CachePort[ValueT]`；adapter 返回 record，Evidence Tool 才构造 `EvidenceFragment`。 |
| M6 | 为 `LLMPort[RequestT, ResponseT]` 定义 prompt/request 与 raw/structured response DTO，并补充 agent lifecycle event DTO；confidence、sufficiency、冲突处理和 attribution policy 留在 reasoning 层。 |
| M8 | 为 `RAGIndexPort[ItemT, QueryT, ResultT]` 定义 projection/query/result DTO，保留原 session、confidence、schema version 和 RAG score，不覆盖当前有效观测。 |
| M9 | 为 `KeyStorePort[SecretT]` 定义 secret 与 masked status DTO；动态时区、单位 override 和经用户确认的全局单位版本记录在该阶段完成。 |

## 后续非 M1

- [ ] M2：session 状态机、SSE、cancel，以及 timezone/resolution snapshot。
- [ ] M3：TranscriptItem、schema version、历史 API、完整折叠 timeline、secret redaction 和大 payload lazy load。
- [ ] M4：日期请求 mapper、结束日期包含整天、ParameterResolver、自动粒度规则和 HydrologyGraph。
- [ ] M5：source record DTO、Evidence Tool registry、真实/fake adapter、聚合、质量分类、canonical unit 表和确定性单位 tool。
- [ ] M6：Agent、prompt/parser、三态生成策略、confidence policy、多站冲突判断和 attribution policy。
- [ ] M7：拓扑 DAG 多 Agent 调度。
- [ ] M8：Transcript RAG。
- [ ] M9：settings/key/admin、动态时区、自定义单位 session override 和用户确认的全局单位表更新。
- [ ] Future：开放任意时分输入；只在出现非 COMID 水体需求时设计 Waterbody。
