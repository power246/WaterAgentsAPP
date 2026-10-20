# 2026-10-19 运行证据包与新电脑导入说明

这个文件夹保存了一次已经完成的 WaterAgentsAPP 实际运行。运行 ID 为 `12ccd705-d5be-4de9-8f53-0e04a358cb11`，原始提问是 `Compute the pH at coordinates: -92.414165705, 46.682323895`。分析对象是 COMID `1777642`，选择了 `America/Chicago` 时区的 2026-10-15，每 6 小时一个结果。

## 本次结果

| 当地时间（2026-10-15） | 目标 pH 预测值 | 模型给出的可能范围 |
| --- | ---: | ---: |
| 00:00–06:00 | 8.2 | 7.8–8.6 |
| 06:00–12:00 | 8.2 | 7.8–8.6 |
| 12:00–18:00 | 8.2 | 7.8–8.6 |
| 18:00–24:00 | 8.2 | 7.9–8.5 |

这四个数值都是**目标水体的预测**，不是目标点的实测。目标结果的整体依据评分为 `0.34`；四窗均标记 `estimated` 和 `provider_flagged`。可能范围是程序的估计范围，并非经过实测校准的统计置信区间，也不能把 `0.34` 当作准确率。结果引用了 USGS-04024000 的上游 pH 观测；保存的两条河道路径约为 2.94 km／经过 3 个水体连接、3.78 km／经过 4 个水体连接。

整次运行保存了 9 个 Agent 会话：1 个提问解析、7 个水体推理、1 个运行摘要。7 个水体结果均为 `usable`；目标结果引用的 33 个证据片段能够在目标会话中找到。运行过程中曾观察到一次数据库写入超时，随后自动恢复；本包导出时运行状态为 `completed`，771 条 transcript 正文和已保存引用均通过完整性检查。这些记录证明运行过程和引用可追溯，**不证明预测值与真实水质一致**。

## 文件及其用途

| 文件 | 用途 |
| --- | --- |
| [`run-database-records.json`](run-database-records.json) | 从 PostgreSQL 的 `analysis_runs`、`analysis_sessions`、`transcript_items`、`transcript_contents` 四表读取的本次运行原始快照；包含外置正文、原始 UUID 和时间。**导入使用它。** |
| [`results.json`](results.json) | 从原始 transcript 提取的 7 个水体结果、运行摘要和拓扑结果，供人直接阅读；它不替代原始快照。 |
| [`manifest.json`](manifest.json) | 导出时间、文件 SHA-256、逐条正文 SHA-256 及完整性计数。 |
| [`export_run.py`](export_run.py) | 在源电脑上重新从生产数据库只读导出这个 run。 |
| [`import_run.py`](import_run.py) | 先校验归档，再把四表原样导入已迁移的 PostgreSQL 数据库。 |
| [`verify_history.py`](verify_history.py) | 导入后通过运行中的 History API 核对目标结果、证据引用及水体归属。 |
| [`source-compatibility/`](source-compatibility/) | 基础 Git bundle、归档时的源码差异和 16 个新增源码文件；用于在新电脑恢复能解析本次记录的项目版本。 |
| [`verification/`](verification/) | 本包校验输出、本机 History API 读回结果、后端全量测试和浏览器 E2E 日志。 |

导出时有 9 个会话、771 条 transcript、72 条外置正文、555 个不同的 typed 引用；771 条均解析成功，正文长度和引用完整性检查均通过。`manifest.json` 记录了两个数据文件的 SHA-256。归档没有复制旧电脑的 `.env`、API 密钥、虚拟环境或 PostgreSQL 数据目录。

## 在新 Windows 电脑导入

以下步骤把**保存的历史记录**放进新电脑的项目数据库，打开历史页面即可展示；不需要重新请求模型，也不需要重新跑 USGS 数据采集。建议先把本文件夹复制到新电脑的一个临时位置，例如 `D:\transfer\runtime-test-1019`。

### 1. 恢复兼容的项目源码

这次运行所在的 `codex/runtime-smoke-fixes-20261014` 工作树有未提交改动，单独从 GitHub 克隆当前默认分支不能保证解析同样的记录。包里的 `base.bundle` 保存了基础提交 `39b56d5ffde54e15b418c3abf9fe64671ba00f1d`，`restore_source.py` 会校验并恢复归档时兼容的源码。以下示例在 PowerShell 中执行；把两个 `D:\...` 路径换成新电脑上的实际位置，并确保 `python` 指向已安装的 Python 3.11 或更新版本：

```powershell
$archive = 'D:\transfer\runtime-test-1019'
$project = 'D:\projects\WaterAgentsAPP'
git clone --branch codex/runtime-smoke-fixes-20261014 "$archive\source-compatibility\base.bundle" $project
Copy-Item -LiteralPath $archive -Destination "$project\runtime-test-1019" -Recurse
Set-Location $project
python .\runtime-test-1019\restore_source.py
python .\runtime-test-1019\restore_source.py --apply
```

第一次调用只检查基础提交、Git patch 和新增文件的哈希；第二次才应用源码。如果新电脑已经有经验证兼容的代码，可以跳过源码恢复，但仍把整个证据包放在项目根目录。这个源码包是**归档时的兼容工作树**，不是对运行进程当时每个代码字节的独立证明。

### 2. 准备依赖和空数据库

安装 Python 3.11 或更新版本、`uv`、Node.js、Docker Desktop；项目数据库使用 PostgreSQL 17 和 pgvector。参见项目的 [`backend/README.md`](../backend/README.md) 获取其他操作系统或现有 PostgreSQL 的配置方法。在项目根目录执行：

```powershell
uv sync --project backend --extra dev --locked
npm --prefix frontend ci
npm --prefix frontend run check:api
npm --prefix frontend run build
Copy-Item .\backend\.env.example .\backend\.env
```

编辑新电脑的 `backend/.env`：为 `WATERAGENTS_DB_PASSWORD` 设置本机密码，并使它与 Docker Compose 使用的密码一致；设置 `WATERAGENTS_MEMORY_ENABLED='false'`。仅查看历史时，把示例中的 `WATERAGENTS_OPENAI_API_KEY` 和 `WATERAGENTS_GOOGLE_GEOCODING_API_KEY` 行注释掉即可，保留合法的模型目录配置。`.env` 不在证据包内，不要从旧电脑复制密钥。

```powershell
Set-Location .\backend
docker compose --env-file .env -f compose.yaml up -d --wait
.\.venv\Scripts\wateragents-db.exe upgrade
.\.venv\Scripts\wateragents-db.exe check
Set-Location ..
```

如果已有数据库，请先确认它是要导入的目标数据库，并使用对应的 `WATERAGENTS_DB_*` 配置；导入脚本默认读取 `backend/.env`。它只新增这次运行的四表记录，不覆盖同 ID 的现有运行。重复导入会明确报错。

### 3. 校验、导入、读回

从项目根目录执行：

```powershell
.\backend\.venv\Scripts\python.exe .\runtime-test-1019\import_run.py --check-only
.\backend\.venv\Scripts\python.exe .\runtime-test-1019\import_run.py
```

第一条命令不连接数据库，会核对文件 SHA-256、771 条正文解析、字节长度和引用闭合。第二条命令在**一个事务**中保留原 UUID、顺序和时间写入四表；数据库拒绝任一部分时不会留下半次运行。

启动服务：

```powershell
Set-Location .\backend
.\.venv\Scripts\python.exe -m app
```

在另一个 PowerShell 窗口，从项目根目录执行：

```powershell
.\backend\.venv\Scripts\python.exe .\runtime-test-1019\verify_history.py 12ccd705-d5be-4de9-8f53-0e04a358cb11 --expect-provider-flagged
```

检查输出中的 `waterbodies: 7`、4 个 `target_ph` 点、`target_fragments: 33`。也可以打开 `http://127.0.0.1:8000/`，在**历史记录**里选这个 run；或直接读取 `http://127.0.0.1:8000/api/analysis/runs/12ccd705-d5be-4de9-8f53-0e04a358cb11?view=history`。展示历史不会再次调用 DeepSeek。若目标数据库已有这个 run，直接启动服务查看即可，无须再次导入。

## 已做过的验证

- 本包 `import_run.py --check-only` 通过，输出见 [`verification/package-check.txt`](verification/package-check.txt)；一次实际导入在独立的临时 PostgreSQL schema 中成功，项目 History 读取器读到了 9 个会话；重复导入被拒绝，临时 schema 已清理，原业务库未被此测试修改。
- 原电脑的 History API 读回核对通过，结果保存于 [`verification/history-api.json`](verification/history-api.json)。
- 归档时的兼容源码已从 `base.bundle` 克隆到临时目录，`restore_source.py` 检查和应用均通过；在这个克隆中复制本包并执行 `import_run.py --check-only` 也通过，临时目录随后清理。
- 相关本机回归记录为后端 `2907 passed, 2 warnings` 和浏览器 `8 passed`；原始输出分别见 [`verification/backend-pytest.log`](verification/backend-pytest.log) 与 [`verification/browser-e2e.log`](verification/browser-e2e.log)。

本包保存的是一次运行的数据库历史与归档时兼容的源码，不包含服务运行环境、第三方服务的未来状态或独立的水质真值。可验证的是保存结果、过程记录及其引用关系。
