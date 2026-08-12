# Live View（DCV）集成与轮询约定

> 2026-08-08 随任务 `08-08-ingestion-task-manager` 落地，位置 `web/src/tasks/`。

## Convention: DCV Live View 的 Vite 集成

**What**：`bedrock-agentcore` npm 包的 `BrowserLiveView`（React 组件）渲染
AgentCore Browser 直播流，集成有四个硬要求，配置都在 `web/vite.config.ts`：

1. `resolve.alias` 把裸 specifier `dcv`/`dcv-ui` 指到包内 vendored 的
   `nice-dcv-web-client-sdk/{dcvjs-esm/dcv.js, dcv-ui/dcv-ui.js}`（这两个不是
   npm 包，不 alias 直接 resolve 失败）。
2. `vite-plugin-static-copy` 把 `dcvjs-esm/{dcv,lib}/` 原样复制到站点根
   `nice-dcv-web-client-sdk/dcvjs-esm/` 下——DCV 运行时按固定 baseUrl fetch
   worker 再 importScripts；**必须用 `rename: stripBase` 剥掉源路径前缀**，
   否则产物落在 `dist/.../node_modules/...` 且**静默失败**（无构建报错，
   实况黑屏）。
3. `resolve.dedupe: ["react","react-dom"]`（dcv-ui 自带 react 引用，双实例
   会让 hooks 崩）。
4. `web/.npmrc` 写 `legacy-peer-deps=true`（该包 peer 声明了 playwright/ai/
   strands 等前端无关重依赖）；另需补装 dcv-ui 漏声明的运行时依赖
   `@cloudscape-design/{components,design-tokens,global-styles}`、`prop-types`、
   `@babel/runtime`。

**Why**：以上任何一条缺失都表现为「构建通过但实况连不上/黑屏」，极难定位。

其他要点：`remoteWidth/remoteHeight` 必须取后端返回值（源头是 Part 1
`browser_ext.py` 的 viewport 1600x900），前端不得硬编码；presigned URL 上限
300 秒，做倒计时 + 「刷新重连」（换 key 重挂组件）；组件必须 `React.lazy`
懒加载（chunk ≈2.9MB）；headless/无 GPU 环境要加
`--use-angle=swiftshader --enable-unsafe-swiftshader` 才有画面（写进了 part2
README FAQ）。

## Pattern: 增量轮询的防重入

**Problem**：`after_seq` 游标在响应返回后才前移，轮询间隔 < 响应耗时时两拍
带同一游标，事件被重复 append（React 重复 key；StrictMode 双 effect 也会暴露）。

**Solution**：in-flight 标记（上一拍未回来则跳过本拍）+ 按 `seq` 去重兜底。
实现见 `web/src/tasks/RunDetail.tsx`。终态（succeeded/failed）必须停轮询。

## Convention: 判定词表按原文显示

**What**：`stats.verdicts` 的键是 Part 1 SOP 的中文域 `PASS/排除/补采/采集`，
一律原样渲染 + tooltip 解释语义；未知键也原样显示不丢数据。

**Why**：`PASS` 在这套 SOP 里是「未过 R1 硬门槛已丢弃」，「采集」才是通过。
凭英文直觉造 `{PASS:"通过"}` 映射会把统计卡语义整个弄反（已踩过）。
统计聚合失败（`stats_error` 存在）时卡片显示 `—` 而非 0——未知不要伪装成零。

## 其他沿用约定

- 视图切换不引 router：`App.tsx` 顶层 state + `hidden` 属性；注意自带
  `display:flex` 的容器会盖掉 UA 的 `[hidden]`，需补 `.x[hidden]{display:none}`。
- Phosphor 图标一律 `*Icon` 后缀导出；颜色只用 `styles.css :root` 既有 token
  （含其 rgba 透明形式），不新增色系。
- 任务 API 类型定义（`web/src/tasks/api.ts`）是后端 REST 契约的唯一前端镜像，
  后端改形状必须同步这里。
