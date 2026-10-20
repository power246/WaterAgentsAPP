# WaterAgentsAPP

WaterAgentsAPP 是一个本地运行的水体分析 Demo。输入水质问题、地点或坐标以及时间范围后，系统沿水系查找相关水体和公开观测，组织多个 Agent 分析，并按时间窗给出带证据和可能范围的结果。它面向监测站稀疏的场景：界面会区分观测与推测，预测数值不等同于目标水体的实测值。

## 可以做什么

- 用自然语言提出问题，可补充目标 COMID、坐标、地图选点、日期和时间粒度。
- 查看水系地图、实时运行进度、各水体的 Agent 会话、证据和时间序列。
- 查看预测范围、依据及观测点到目标水体的距离和水体连接数。
- 从历史记录重看已保存的运行，在设置和管理页面调整本地配置、查看存储状态。

分析由 React 前端发起；FastAPI 后端负责查询解析、水系拓扑、证据采集、按数值依赖关系推理和结果保存。前端与 API 由同一个本地服务提供，运行记录保存在 PostgreSQL 中。

例如，可以询问 `Compute the pH at coordinates: -92.414165705, 46.682323895`。仓库中的[运行证据包](runtime-test-1019/README.md)保存了这类问题的一次完整分析及导入方法。

## 项目目录

| 路径 | 内容 |
| --- | --- |
| [`backend/`](backend/README.md) | FastAPI 服务、分析流程、数据源适配、数据库迁移及后端测试。 |
| [`frontend/`](frontend/README.md) | React 页面、地图与图表、API 客户端及前端测试。 |
| [`docs/`](docs/) | 架构、接口与运维文档。 |
| [`runtime-test-1019/`](runtime-test-1019/README.md) | 一次实际运行的完整证据包及新电脑导入说明。 |
| [`AGENT.md`](AGENT.md) | 项目结构、启动和测试产物约定。 |

## 快速启动（Windows PowerShell）

需要 Python 3.11 或更新版本、`uv`、Node.js 24、npm 11，以及可运行 Docker Compose 的 Docker Desktop。以下命令从项目根目录执行。

首次安装依赖并创建本机配置：

```powershell
uv sync --project backend --extra dev --locked
npm --prefix frontend ci

if (-not (Test-Path backend/.env)) {
    Copy-Item backend/.env.example backend/.env
}
```

编辑 `backend/.env`，至少设置本地数据库密码，并把模板中的模型目录和所需服务密钥替换为实际配置。模板中的模型名及密钥是占位值，不能用于真实分析。没有 embedding 服务时，可以设置 `WATERAGENTS_MEMORY_ENABLED='false'`；模型目录仍须保留符合格式的 embedding 条目，但不会调用它。不要将 `.env` 提交到 Git。配置详情见[后端说明](backend/README.md#configuration)。

构建前端，然后启动数据库与应用：

```powershell
npm --prefix frontend run check:api
npm --prefix frontend run build

Set-Location backend
.\.venv\Scripts\Activate.ps1
docker compose --env-file .env -f compose.yaml up -d --wait
wateragents-db upgrade
wateragents-db check
python -m app
```

打开 <http://127.0.0.1:8000/>。健康检查在 <http://127.0.0.1:8000/health>，正常时返回 `{"status":"healthy"}`。停止应用请在该终端按 `Ctrl+C`；数据库可从 `backend/` 运行 `docker compose --env-file .env -f compose.yaml down`，该命令保留数据卷。

前端开发时，可让后端继续运行，在另一终端从项目根目录执行 `npm --prefix frontend run dev`，然后打开 <http://127.0.0.1:5173/>。前端开发服务会把 API 请求代理到本地后端。其他操作系统和外部 PostgreSQL 的配置见[后端说明](backend/README.md)。

## 验证与运行证据

前端的 lint、类型检查、单元测试和浏览器测试命令见[前端说明](frontend/README.md#验证)。后端测试需要与业务库分开的测试数据库，步骤见[后端说明](backend/README.md#test)。

[`runtime-test-1019/`](runtime-test-1019/README.md) 保存了一次真实分析的数据库记录、结果、校验文件和导入脚本，可在另一台电脑恢复并查看历史。证据包能展示运行和引用的可追溯性，不能证明预测值与真实水质一致。其他本地测试结果与临时截图无需提交到 Git。
