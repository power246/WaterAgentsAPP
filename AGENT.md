# WaterAgentsAPP

## 项目介绍

WaterAgentsAPP 是本地运行的水体分析 Demo。用户可以输入地点或坐标、分析指标和时间范围；系统结合水系拓扑、公开监测数据与模型推理，展示水体结果、证据、时间序列、地图、Agent 会话和历史记录。预测结果会保留其依据及不确定性。

- `backend/`：Python 3.11+、FastAPI、PostgreSQL/pgvector；提供页面、API 和实时事件流。
- `frontend/`：React、TypeScript、Vite；提供分析、历史、设置和管理界面。
- `docs/`：架构、数据库和功能说明。

## 主要流程

前端将地点、指标和时间范围提交给后端。后端确定目标水体和相连水系，采集监测读数及相关证据，按数值依赖关系组织 Agent 推理，再按时间窗形成结果。运行过程通过事件流展示；运行、Agent 会话和证据保存在 PostgreSQL 中，可从历史页面重新查看。

## 项目结构

| 路径 | 用途 |
| --- | --- |
| `backend/app/api/` | HTTP API、事件流和请求/响应契约。 |
| `backend/app/application/` | 分析运行、查询解析、水系拓扑、证据采集、数值推理和摘要等流程。 |
| `backend/app/domain/`、`backend/app/ports/` | 水体与证据的业务规则，以及外部依赖接口。 |
| `backend/app/infrastructure/` | 数据源适配、PostgreSQL 持久化、配置和服务装配。 |
| `backend/tests/`、`backend/scripts/` | 后端测试源码、诊断和烟测脚本。 |
| `backend/compose.yaml`、`backend/.env.example` | 本地数据库服务和配置模板。 |
| `frontend/src/app/`、`frontend/src/features/` | 前端入口及分析、历史、设置、管理界面。 |
| `frontend/src/api/`、`frontend/public/` | API 客户端、生成的契约和静态资源。 |
| `frontend/tests/`、`frontend/e2e/` | 前端单元/组件测试和浏览器测试源码。 |
| `docs/` | 设计、接口及运维文档。 |
| `runtime-test-1019/` | 完整实际运行证据包，含结果、数据库记录、校验/导入脚本和新电脑导入说明；详见其中的 [README.md](runtime-test-1019/README.md)。 |

## 本地启动（Windows PowerShell）

需要 Python 3.11 或更新版本、`uv`、Node.js 24、npm 11、Docker Desktop 与 Docker Compose。以下命令从项目根目录开始执行。

### 首次准备

```powershell
uv sync --project backend --extra dev --locked
npm --prefix frontend ci

if (-not (Test-Path backend/.env)) {
    Copy-Item backend/.env.example backend/.env
}
```

编辑 `backend/.env`：设置本地数据库密码，并将示例中的模型目录、模型名称及所需服务密钥替换为真实配置。`backend/.env.example` 中的模型和密钥只是占位值，不能用于真实分析。没有 embedding 服务时，可设置 `WATERAGENTS_MEMORY_ENABLED='false'`；模型目录仍须包含配置格式要求的 embedding 条目，但此模式不会调用它。配置字段见 [backend/README.md](backend/README.md#configuration)。不要提交或打印 `backend/.env` 中的密钥。

### 构建并运行

在项目根目录构建前端：

```powershell
npm --prefix frontend run check:api
npm --prefix frontend run build
```

然后启动数据库、升级并检查结构，最后启动应用：

```powershell
Set-Location backend
.\.venv\Scripts\Activate.ps1
docker compose --env-file .env -f compose.yaml up -d --wait
wateragents-db upgrade
wateragents-db check
python -m app
```

浏览器打开 `http://127.0.0.1:8000/`；`http://127.0.0.1:8000/health` 应返回 `{"status":"healthy"}`。后端在同一地址提供已构建的前端和 API。修改前端后需重新运行 `npm --prefix frontend run build` 才能在该地址看到更新。

开发前端时，也可以让后端保持运行，在另一终端从项目根目录执行 `npm --prefix frontend run dev`，然后打开 `http://127.0.0.1:5173/`。Vite 会将 API 请求代理到本地后端。

停止应用时先在运行 `python -m app` 的终端按 `Ctrl+C`。如需停止数据库，在 `backend/` 执行 `docker compose --env-file .env -f compose.yaml down`；数据库卷会保留。

## 验证

前端检查从项目根目录执行：

```powershell
npm --prefix frontend run lint
npm --prefix frontend run typecheck
npm --prefix frontend run test -- --run
```

后端测试需要独立于业务库的测试数据库；设置方法和命令见 [backend/README.md](backend/README.md#test) 与 [Docker PostgreSQL 快速指南](docs/docker_postgresql_quickstart.md)。`WATERAGENTS_TEST_DB_NAME` 只设置在测试进程环境中，不写入 `backend/.env`。

## Git 与测试产物

项目源码、测试**代码**、配置模板和必要文档按正常开发流程纳入 Git。测试**结果**只保留 `runtime-test-1019/` 这一完整证据包及直接关联的说明/恢复材料；包内的 JSON、脚本、源码兼容材料和验证日志需要一起保存。其他本地运行结果、截图、浏览器审计文件及临时测试目录不提交。它们可以留在本机，无须为提交而删除。提交前检查 `git status`，确认没有把测试结果与测试代码混淆。
