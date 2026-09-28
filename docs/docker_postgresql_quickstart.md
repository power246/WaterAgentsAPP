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
