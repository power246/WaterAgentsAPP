# Docker PostgreSQL 快速指南（Coding Agent）

所有命令都从 `backend/` 目录执行。

## 当前配置

| 项目 | 配置 |
|---|---|
| 镜像 | `postgres:17` |
| Compose 服务 | `postgres` |
| 地址 | `127.0.0.1:5432` |
| 业务数据库 | `wateragents` |
| 用户 | `wateragents` |
| 数据卷 | `wateragents-postgres-data` |
| SSL | 本地默认 `disable` |

实际值可由 `backend/.env` 覆盖。密码只保存在未提交的 `.env` 中，不要打印或写入文档。

### Windows Docker 数据位置

当前 Windows 开发机的 Docker Desktop Linux 数据盘位于
`D:\DockerData\DockerDesktopWSL\disk\docker_data.vhdx`。镜像、容器和命名卷都在这块
VHDX 中，因此 PostgreSQL 的 `wateragents_wateragents-postgres-data` 命名卷也随数据盘
一起迁移。Docker Desktop 程序本体仍由 Windows 安装器管理，默认位于
`C:\Program Files\Docker\Docker`；项目数据库是否位于 D 盘应以数据盘设置和 VHDX
实际路径为准，而不是以程序安装目录为准。

迁移或再次变更位置时使用 Docker Desktop 的
**Settings > Resources > Advanced > Disk image location**，选择 `D:\DockerData` 后，
当前版本会使用其下的 `DockerDesktopWSL` 子目录。不要在 Docker Desktop 运行时手工
移动 VHDX，也不要直接猜测或修改 `settings-store.json` 中的内部字段。

迁移前先停止 PostgreSQL 并保留两层回滚材料：不含角色口令的 `pg_dumpall` 逻辑备份，
以及 Docker Desktop 完全停止后复制的原始 `docker_data.vhdx`。本机 2026-10-03 的回滚
目录为 `D:\DockerData\MigrationBackup\20261003-173714`；其中 VHDX 已与迁移前源文件
按 SHA-256 核对一致。确认长期运行稳定前不要删除该目录。

迁移完成后至少检查：

```powershell
# Docker Desktop 设置中确认 Disk image location 为 D:\DockerData\DockerDesktopWSL
Test-Path 'D:\DockerData\DockerDesktopWSL\disk\docker_data.vhdx'
Test-Path "$env:LOCALAPPDATA\Docker\wsl\disk\docker_data.vhdx"  # 应为 False

Set-Location backend
docker compose --env-file .env -f compose.yaml up -d --wait
docker compose --env-file .env -f compose.yaml exec postgres `
  psql -U wateragents -d postgres -Atc `
  'SELECT datname FROM pg_database WHERE datallowconn AND NOT datistemplate ORDER BY datname;'
wateragents-db check
```

本机迁移验收已确认 Docker Desktop 4.93.0 / Engine 29.8.1 重启后仍使用 D 盘；容器为
`healthy`，`postgres`、`wateragents`、`wateragents_test` 均存在，迁移前后规范化逻辑
dump 的 SHA-256 一致；M7 Milestone 1 后当前 schema head 为 `20261003_01`（在既有四表上增加 `run_summarizer` session CHECK/唯一索引），health 与 run/history/restart smoke 通过。

## 首次启动

```powershell
Set-Location backend
Copy-Item .env.example .env   # 已存在时不要覆盖
# 编辑 .env，设置 WATERAGENTS_DB_PASSWORD
docker compose --env-file .env -f compose.yaml up -d --wait
wateragents-db upgrade
wateragents-db check
```

启动应用并检查健康状态：

```powershell
python -m app
# 另开终端访问 http://127.0.0.1:8000/health
```

## 创建测试库

Compose 只自动创建业务库。首次测试前另建一个空库；下面命令通常只需执行一次：

```powershell
docker compose --env-file .env -f compose.yaml exec postgres createdb -U wateragents -O wateragents wateragents_test
```

若已存在 `wateragents_test`，跳过该命令。自定义过数据库用户时，也要替换命令中的 `wateragents`。

## 测试

`WATERAGENTS_TEST_DB_NAME` 只能设置在测试进程环境中，不能写进 `backend/.env`，且必须与业务库名不同。

PowerShell：

```powershell
$env:WATERAGENTS_TEST_DB_NAME = 'wateragents_test'
python -m pytest -q
python scripts/health_smoke.py
python scripts/run_history_smoke.py
Remove-Item Env:WATERAGENTS_TEST_DB_NAME
```

bash：

```bash
export WATERAGENTS_TEST_DB_NAME='wateragents_test'
python -m pytest -q
python scripts/health_smoke.py
python scripts/run_history_smoke.py
unset WATERAGENTS_TEST_DB_NAME
```

测试和 smoke 会在测试库中创建并清理临时 schema，不会把正式服务切换到测试库。

## 停止与排查

```powershell
docker compose --env-file .env -f compose.yaml ps
docker compose --env-file .env -f compose.yaml logs postgres
docker compose --env-file .env -f compose.yaml down
```

普通 `down` 会保留数据卷。除非明确要永久删除全部本地数据库数据，否则不要使用 `down --volumes`。

### Docker Desktop 在 `dockerInference` 处启动失败

若 Windows 上出现下面这类错误，故障位于 Docker Desktop 的 Model Runner 运行时 socket，不是 PostgreSQL 数据卷：

```text
starting services: initializing Inference manager: ...\Docker\run\dockerInference:
The file cannot be accessed by the system.
```

本项目不依赖 Docker Model Runner。先检查版本；旧版 Docker Desktop（本机曾在 4.46.0 复现）应优先升级，并保持 Model Runner 关闭：

```powershell
docker desktop update --check-only
docker desktop update --quiet
docker desktop disable model-runner
docker desktop restart
docker desktop status
docker info
```

若 Desktop 已在启动阶段崩溃，导致上述关闭或升级命令无法连接后端，只恢复运行时目录，不要执行 Factory Reset、`docker system prune` 或删除卷：

1. 完全退出 Docker Desktop，确认 `Docker Desktop`、`com.docker.backend`、`com.docker.build` 和 `docker-model` 进程均已停止，`com.docker.service` 与 `docker-desktop` WSL 实例也不在运行。
2. 将整个 `%LOCALAPPDATA%\Docker\run` 移动为带时间戳的备份，再创建空的 `run` 目录。不要单独删除失效的 `dockerInference` reparse point，也不要触碰当前 Disk image location；本机当前为 `D:\DockerData\DockerDesktopWSL`，未迁移的默认环境才是 `%LOCALAPPDATA%\Docker\wsl`。
3. 启动 Docker Desktop，升级并关闭 Model Runner，然后至少执行一次 `docker desktop restart` 验证可连续启动。
4. 用 `docker ps -a` 和 `docker volume ls` 确认容器与卷仍在，再重新执行本指南的 Compose 启动命令。

本机已验证 Docker Desktop 4.93.0、Engine 29.8.1 在 `EnableInference=false` 下可连续重启；失效的旧 `run` 目录应先保留用于回滚，确认后续运行稳定后再人工清理。
