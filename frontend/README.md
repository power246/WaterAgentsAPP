# WaterAgents 前端

这是 WaterAgentsAPP 的 React + TypeScript 界面。它负责提交水体分析问题，展示水系地图、运行进度、各水体结果与证据，并提供历史、设置和管理页面。真实数据与分析由同仓库的 [FastAPI 后端](../backend/README.md)提供。

## 页面与代码结构

| 路径 | 用途 |
| --- | --- |
| `src/app/` | 应用路由和分析工作台。 |
| `src/features/analysis/` | 新建分析、地图、结果与时间序列。 |
| `src/features/history/` | 已保存运行的列表与回看。 |
| `src/features/settings/`、`src/features/admin/` | 设置与本地管理界面。 |
| `src/api/` | HTTP/SSE 客户端及根据后端 OpenAPI 生成的类型。 |
| `src/styles.css`、`public/` | 样式和静态资源。 |
| `tests/`、`e2e/` | 单元/组件测试与 Playwright 浏览器测试。 |
| `scripts/` | API 契约生成/检查和构建检查。 |

主要路由有 `/`（新分析）、`/history`（历史）、`/runs/:runId`（运行详情）和 `/admin`（管理）。设置从工作台打开。

## 本地开发

需要 Node.js 24、npm 11，以及运行在 `http://127.0.0.1:8000` 的后端。完整的数据库和后端启动步骤见[项目 README](../README.md#快速启动windows-powershell)。在项目根目录运行：

```powershell
npm --prefix frontend ci
npm --prefix frontend run dev
```

打开 <http://127.0.0.1:5173/>。Vite 默认把 `/api` 和 `/openapi.json` 代理到 `http://127.0.0.1:8000`；如果后端监听其他地址，可在启动前设置 `WATERAGENTS_DEV_API_TARGET`。

## 构建与 API 契约

后端的 Python 依赖需先安装，`check:api` 才能从当前后端导出 OpenAPI 并核对 `src/api/generated.ts`。后端接口变更时，运行 `npm --prefix frontend run generate:api` 更新生成的类型，再运行检查。

```powershell
npm --prefix frontend run check:api
npm --prefix frontend run build
```

构建输出位于 `frontend/dist/`。正式本地服务由后端在 <http://127.0.0.1:8000/> 提供该页面和同源 API；修改前端后需重新构建。

## 验证

在项目根目录运行：

```powershell
npm --prefix frontend run lint
npm --prefix frontend run typecheck
npm --prefix frontend run test -- --run
npm --prefix frontend run test:e2e
```

浏览器测试默认启动前端开发服务，并使用 Playwright 的 Chromium。要检查后端提供的构建版本，先按[项目 README](../README.md#快速启动windows-powershell)启动后端，再把 `WATERAGENTS_E2E_BASE_URL` 设为 `http://127.0.0.1:8000` 运行 `test:e2e`。

测试源码及其截图基线位于 `tests/` 和 `e2e/`；临时生成的测试报告、审计截图与浏览器缓存不提交。需要展示的实际运行证据见 [`runtime-test-1019/`](../runtime-test-1019/README.md)。
