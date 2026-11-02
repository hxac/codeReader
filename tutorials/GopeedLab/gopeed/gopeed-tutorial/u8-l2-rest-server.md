# pkg/rest：REST 服务器、鉴权与 Web UI 托管

## 1. 本讲目标

上一讲（u8-l1）我们精读了 `pkg/api`：一套与传输无关的服务层，29 条 `/api/v1` 路由靠 `Service.Dispatch` 在进程内直接执行。本讲回答紧接着的下一个问题：**这些接口如何被「翻译」成真正的 HTTP 服务？** 学完本讲，你应该能够：

1. 说出 `Start`/`Stop` 与 `BuildServer` 两条启动路径各自的适用场景，以及 `initializeCore` 如何把 `StartConfig` 翻译成 Downloader。
2. 逐段读懂 `buildServer` 的组装流水线：API 路由注册、`/mcp` 与 `/api` 命名空间保留、Web 静态资源兜底、CORS 与 panic 恢复中间件，并解释**为什么命名空间保留必须注册在 Web 兜底路由之前**。
3. 掌握双层鉴权的判定顺序：API token（`X-Api-Token` / `Authorization: Bearer`）优先、Web 会话 cookie 兜底，以及两者各自的实现细节（常数时间比较、7 天 TTL 会话）。
4. 解释 `/fs/tasks` 与 `/fs/extensions` 两个文件系统如何把磁盘上的下载文件与扩展资源暴露成 HTTP 路径。
5. 理解 `apiServerManager` 的「期望配置 vs 实际运行」双状态模型，以及桌面/移动端如何在进程内按需启停 API 服务器。

## 2. 前置知识

本讲是 HTTP 服务端「组装课」，需要以下概念垫底。已熟悉的读者可以快速扫过。

- **路由匹配（gorilla/mux）**：项目使用 `github.com/gorilla/mux`。`r.Methods("GET").Path("/api/v1/config")` 注册精确路由；`r.PathPrefix("/")` 注册前缀路由。关键规则：**请求到来时按注册顺序尝试匹配，第一个命中的路由生效**。一条 `PathPrefix("/")` 的「兜底路由」会吃掉所有未被前面路由认领的路径——这是本讲命名空间问题的根源。
- **中间件（middleware）**：形如 `func(http.Handler) http.Handler` 的函数，把业务 handler 包一层。`r.Use(m)` 添加的中间件按**添加顺序**由外向内包裹：先 `Use` 的在最外层、最先执行。鉴权、日志、panic 恢复都靠它实现。
- **API token 鉴权**：服务端启动时配置一个秘密字符串，客户端每次请求带上（本项目支持 `X-Api-Token` 头或 `Authorization: Bearer <token>`）。适合脚本、MCP 客户端等非浏览器调用方。
- **Cookie 会话鉴权**：浏览器不适合存 token，于是登录成功后服务端生成一个随机 sessionID，通过 `Set-Cookie` 下发（`HttpOnly` 让 JS 读不到、`SameSite=Lax` 缓解 CSRF），之后浏览器每次请求自动携带。服务端维护 sessionID → 过期时间的映射表。
- **CORS（跨域资源共享）**：浏览器同源策略下，A 站点的 JS 要调 B 站点的 API，需要 B 在响应里声明放行。`handlers.CORS(...)` 中间件负责加这些响应头，包括允许跨域携带的自定义头（如 `X-Api-Token`）。
- **gzip 压缩**：Web 静态资源（JS/CSS）体积大，客户端 `Accept-Encoding: gzip` 时服务端可压缩后再传。
- **go:embed**：编译期把文件目录嵌入二进制。u1-l2 讲过 `cmd/web` 用 `//go:embed dist/*` 把 Flutter Web 产物打进可执行文件；本讲会看到它如何变成一个 `http.FileSystem`。

另外请回顾 u8-l1 的两个结论，本讲直接沿用：**handler 只接触纯数据的 `Context`/`Response`**；**业务错误用 HTTP 200 + 信封错误码表达**（`Result` 结构：`code`/`msg`/`data`）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `pkg/rest/server.go` | 核心文件：`Start`/`Stop`/`BuildServer`、`buildServer` 组装流水线、双层鉴权、`webSessionStore`、`taskFileSystem`/`extensionFileSystem`/`embedCacheFileSystem`、`initializeCore` |
| `pkg/rest/api.go` | 各 `/api/v1` 路由对应的 HTTP handler 薄封装（调 `Downloader` 并写 JSON 信封） |
| `pkg/rest/api_server_manager.go` | `apiServerManager`：运行中 API 服务器的启停/重启/状态查询 |
| `pkg/rest/model/server.go` | `StartConfig`（启动参数）、`WebAuth`、`APIServerState`（运行状态） |
| `pkg/rest/gizp_middleware.go` | Web 静态资源的 gzip 压缩中间件 |
| `pkg/rest/model/result.go` | `Result[T]` 响应信封与错误码常量（u8-l1 已讲，本讲引用其错误码） |
| `cmd/server.go` | 服务端形态共享启动逻辑：`BuildServer` 后自行 `Serve`，并处理首启默认配置 |
| `cmd/web/main.go`、`cmd/web/flags.go` | Web 产品入口：命令行/配置文件/环境变量组装 `StartConfig` |
| `pkg/rest/server_test.go` | 鉴权、命名空间保留、API token 的行为测试（本讲实践的依据） |

## 4. 核心概念与源码讲解

### 4.1 两条启动路径：Start/Stop 与 BuildServer

#### 4.1.1 概念说明

`pkg/rest` 对外提供两种把服务跑起来的方式：

- **`Start(startCfg)` / `Stop()`**：库风格的「托管」接口。调用方（原生端桌面/移动程序，见 u8-l3）只给一份配置，包内自己创建监听器、起 goroutine 服务、返回端口号；`Stop` 负责优雅关闭并清理全局状态。它还管理一个可选的进程内 API 服务器（`apiServerManager`，4.5 节）。
- **`BuildServer(startCfg)`**：返回未启动的 `(*http.Server, net.Listener, error)`，**不调用 `Serve`**。服务端部署形态（`cmd/web`、`cmd/api`）走这条路，由 `cmd.Start` 自己掌握服务循环与退出信号处理。

两条路径共享同一个 `buildServer` 组装函数与同一个进程级核心（`Downloader` + `APIService`）：`initializeCore` 里有 `if APIService != nil { return nil }` 守卫，核心只初始化一次，先到先得。

#### 4.1.2 核心流程

`Start` 的执行流程（伪代码）：

```text
Start(startCfg):
    加 runtimeMu 写锁（全局串行）
    if APIService != nil: return 已运行端口        # 幂等：避免重复启动
    startCfg.Init() + Validate()                   # 补默认值、校验
    initializeCore(startCfg)                       # 创建 Downloader + APIService（仅一次）
    if startCfg.NativeMode:                        # 原生端：API 参数来自持久化配置
        apiConfig = Downloader.GetConfig().API
        用 apiConfig 覆盖 startCfg 的 Enable/MCP/Network/Address/Token
    Downloader.ContinueOnStartup()                 # 断点任务自动续跑（失败仅告警）
    if !startCfg.APIEnabled(): return              # Web 版恒为启用；原生端可关
    port, err = apiServer.startWithConfigLocked(startCfg, config)
    if err 且 NativeMode: 记日志，返回 0, nil       # API 服务器是可选组件，失败不致命
    return port, err
```

`Stop` 的清理顺序：`apiServer.stopLocked()`（关 HTTP 服务）→ `APIService.SubscribeTaskEvents(0, nil)`（退订任务事件回调，u8-l4 讲）→ `Downloader.Close()`（关下载核心）→ 全局变量置空。顺序保证：先不再接收新请求，再断事件流，最后落盘关闭。

`initializeCore` 把 `StartConfig` 翻译成 `download.DownloaderConfig`：

| StartConfig 字段 | 去向 |
| --- | --- |
| `ProductionMode` / `RefreshInterval` / `WhiteDownloadDirs` / `WebViewProvider` | 同名透传（生产模式、进度刷新间隔、下载目录白名单、WebView 提供者） |
| `Storage == "bolt"` | `NewBoltStorage(StorageDir)`，否则 `NewMemStorage()` |
| `StorageDir` / `TempDir` | 透传 |

随后 `NewDownloader` → `Setup()` → `goapi.NewService(Downloader)`；失败时回滚（`Downloader.Close()` 并置 nil）。

#### 4.1.3 源码精读

`Start` 的幂等守卫与 NativeMode 分支——原生端（桌面/移动）进程内启动时，API 服务器的开关、地址、token 不来自启动参数，而来自用户在界面里保存的持久化配置（`base.APIServerConfig`）：

- [pkg/rest/server.go:94-148](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/rest/server.go#L94-L148)：`Start` 主体。注意 L99-101 的防重复启动、L112-128 的 NativeMode 覆盖、L142-146 中 NativeMode 下启动失败仅记日志并返回 `0, nil`（API 服务器是可选增强，不能因为它失败而拖垮宿主 App）。

`Stop` 的有序清理与全局重置：

- [pkg/rest/server.go:150-166](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/rest/server.go#L150-L166)：依次关闭 HTTP 服务器、退订事件、关闭 Downloader，最后把 `APIService` 置 nil、`apiServer` 换成全新空实例，让包回到可重新 `Start` 的初始状态。

`BuildServer` 只是 `buildServer` 的公开包装：

- [pkg/rest/server.go:168-170](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/rest/server.go#L168-L170)：`BuildServer` 固定传 `continueOnStartup = true`；而 4.5 节会看到 `apiServerManager.startWithConfigLocked` 调 `buildServer(startConfig, false)`——运行期重启 API 服务器时**不应**再次触发任务自动续跑。

服务端部署形态如何消费 `BuildServer`：

- [cmd/server.go:19-68](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/cmd/server.go#L19-L68)：`cmd.Start` 拿到 server 与 listener 后打印 banner、处理首启默认配置（默认下载目录、内置分类种子），然后 `watchExit()` 注册退出信号，最后 `srv.Serve(listener)` 阻塞服务。退出信号到来时 `Downloader.Close()` 后直接退出（L71-80）。

`initializeCore` 的「只此一次」守卫与失败回滚：

- [pkg/rest/server.go:393-423](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/rest/server.go#L393-L423)：L394-396 守卫保证进程内 Downloader 只建一次——`Start` 之后再 `BuildServer` 会复用同一个核心（u8-l3 原生桥接正是靠这一点：宿主 App 先 `Start`，本地 REST 服务器与桥接请求共享同一 Downloader）；L415-420 在 `NewService` 失败时关闭并清空 Downloader，不留半初始化状态。

#### 4.1.4 代码实践

**实践目标**：通过阅读 + 运行测试，确认「两条路径共享同一核心」。

1. 打开 [pkg/rest/server.go:393-423](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/rest/server.go#L393-L423)，找到「只初始化一次」的守卫行。
2. 运行 `go test ./pkg/rest/ -run 'TestBuildServerPropagatesWebViewProvider' -v`（测试位于 [pkg/rest/server_test.go:1158](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/rest/server_test.go#L1158)），观察测试中连续多次 `buildServer` 后 `Downloader` 与 `APIService` 是否保持同一实例。
3. 观察 `Stop()` 之后再 `Start()` 能否成功（对照 L164-165 的全局重置）。

**需要观察的现象**：测试输出 PASS；多次构建后核心单例不变；Stop→Start 可以重新跑起来。具体输出**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `cmd/web` 不直接用 `Start()`，而要走 `BuildServer` + 自己 `Serve`？

**答案**：`Start` 是托管式接口，启动后无法插入部署形态需要的额外逻辑；而 `cmd/server.go` 需要在 `Serve` 阻塞之前完成首启默认配置（下载目录、内置分类）、注册 SIGINT/SIGTERM 退出信号处理（[cmd/server.go:30-64](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/cmd/server.go#L30-L64)）。`BuildServer` 把「组装」与「服务循环」解耦，把阻塞点交还调用方。

**练习 2**：`Start` 中 `if !startCfg.APIEnabled()` 直接 `return 0, nil`（L132-134），为什么此时不报错？

**答案**：看 [pkg/rest/model/server.go:41-48](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/rest/model/server.go#L41-L48)：`APIEnabled()` 在 `WebEnable` 时恒为 true（Web 界面必须靠 API 工作）；否则 `ApiEnable == nil || *ApiEnable`（保持旧 CLI/库调用者默认开启的兼容行为）。只有原生端显式关闭 API 时才会走到这个分支——此时 Downloader 已就绪，只是「不需要对外监听端口」，属于正常状态而非错误。

### 4.2 buildServer 组装流水线：路由、命名空间保留与中间件

#### 4.2.1 概念说明

`buildServer` 是本讲的主菜，按固定顺序往 `mux.Router` 上「叠」东西：

1. **API 路由**：遍历 `APIService.RouteSpecs()`，把每条声明式路由注册成真正的 HTTP 端点；
2. **`/mcp` 端点**：启用 MCP 时挂 `mcpserver.NewHandler`，否则挂 `NotFoundHandler`；并保留 `/mcp/` 前缀；
3. **登录端点**：启用 Web 鉴权时注册 `POST /api/web/login`；
4. **命名空间保留**：`/api` 与 `/api/` 前缀挂 `NotFoundHandler`；
5. **Web 文件路由**（仅 `WebEnable`）：`/fs/tasks`、`/fs/extensions`、以及 `/` 兜底的 gzip 静态服务；
6. **鉴权中间件**、**panic 恢复中间件**、**CORS 包装**。

顺序本身就是语义：mux 按注册顺序匹配，**先注册的精确/前缀路由优先于后注册的 `/` 兜底**。

#### 4.2.2 核心流程

一个请求进入服务器后的判定流程（自上而下短路）：

```text
请求 GET /api/v1/config
  → CORS 中间件（补跨域响应头）
  → 鉴权中间件（4.3 节：不在保护范围则直通；在则先验 token、再验 cookie）
  → panic 恢复中间件（defer recover，兜住业务 handler 的 panic）
  → mux 路由表按注册顺序匹配：
      /api/v1/config 命中第 1 步注册的 API 路由
      → 读取 body → 组装 goapi.Context{Method, Path, Query, Body, Headers, PathParams}
      → route.Handler(ctx) 执行（与 REST/FFI/测试共享的同一实现）
      → WriteStatusJson(响应码, 响应体)

请求 GET /api/v1/missing（不存在的 API 路径）
  → 鉴权 → panic 恢复 → 匹配：API 路由全不命中
      → 命中第 4 步的 PathPrefix("/api/") → 404（而不会被 Web 兜底吐出 index.html）

请求 GET /settings（前端路由路径）
  → 全部具名路由不命中 → 命中第 5 步 PathPrefix("/")
      → embedCacheFileSystem 打开 /settings 失败 → FileServer 回退 index.html（SPA 行为）
```

#### 4.2.3 源码精读

API 路由的注册——把传输无关的 handler 适配成 HTTP handler 的「翻译层」就在这个闭包里：

- [pkg/rest/server.go:199-218](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/rest/server.go#L199-L218)：对每条 `RouteSpec` 用 `r.Methods(...).Path(...).HandlerFunc(...)` 注册；闭包内 `io.ReadAll(r.Body)` 读请求体，组装 `goapi.Context`（Headers 与 `mux.Vars(r)` 路径参数也一并塞入），交给 `route.Handler` 后按其返回的状态码写响应。**handler 内部完全不接触 `http.Request`/`http.ResponseWriter`**——这正是 u8-l1「三端共享同一实现」结论在 REST 侧的落点。

`/mcp` 端点与命名空间保留：

- [pkg/rest/server.go:219-225](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/rest/server.go#L219-L225)：MCP 启用时挂真实 handler，禁用时 `/mcp` 与 `/mcp/` 前缀都挂 `http.NotFoundHandler()`，注释写明意图——"Reserve the endpoint namespace so the web UI catch-all cannot serve it"（保留端点命名空间，防止被 Web 兜底路由吃掉）。这样客户端探测 `/mcp` 时得到明确的 404，能可靠判断「MCP 未启用」，而不是收到一张 200 的 HTML 页面。

`/api` 命名空间保留与 Web 文件路由：

- [pkg/rest/server.go:261-269](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/rest/server.go#L261-L269)：`/api` 与 `/api/` 前缀挂 `NotFoundHandler()`，注释点明要覆盖「未知端点与已知 API 路径上的不支持方法」两类情况；随后（`WebEnable` 时）注册 `/fs/tasks`、`/fs/extensions` 两个文件服务与 `/` 兜底的 gzip 静态服务。**这三行保留代码必须写在 `PathPrefix("/")` 之前**——兜底路由一旦先注册，所有未知 `/api/...` 请求都会命中它并返回 SPA 的 `index.html`。

panic 恢复中间件与 CORS：

- [pkg/rest/server.go:316-328](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/rest/server.go#L316-L328)：`defer recover()` 兜住 handler panic，用 `zerolog` 的 `Stack()` 记录调用栈，并按项目惯例返回 HTTP 200 + `code=1000` 的错误信封——单个请求的业务 panic 不会击垮整个 HTTP 服务。
- [pkg/rest/server.go:330-336](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/rest/server.go#L330-L336)：`handlers.CORS` 放行的请求头里特别包含 `X-Api-Token` 与一组 `Mcp-*` 头——没有这一行，浏览器里的跨域 API 调用与 MCP 客户端会被预检（preflight）拦下；`ExposedHeaders` 里的 `Mcp-Session-Id` 让客户端能读到 MCP 会话 ID 响应头。

一个 handler 例子（本轮增量更新的落点）：

- [pkg/rest/api.go:59-73](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/rest/api.go#L59-L73)：`CreateTaskBatch` 校验 `reqs` 非空后调用 `Downloader.CreateDirectBatchWithResult(&req)` 并把结果装进信封。相比上一版本（调用 `CreateDirectBatch` 返回 `[]string`），现在返回的是 `CreateTaskBatchResult` 对象（`taskIds` + 可选 `parentId`，指向任务分组）——REST 层只做透传，批量与分组语义详见 u2-l1 与 u4-l6。新增行为有测试背书：[pkg/rest/server_test.go:250-282](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/rest/server_test.go#L250-L282) 的 `TestCreateDirectTaskBatchWithGroup` 验证了 `Group: true` 时响应含 `parentId`、任务列表只含真实任务且每个成员带 `group` 元数据。

#### 4.2.4 代码实践

**实践目标**：亲眼验证「命名空间保留在兜底路由之前」的必要性。

1. 阅读 [pkg/rest/server.go:219-269](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/rest/server.go#L219-L269)，记住注册顺序：`/mcp`（219）→ 登录（234）→ `/api` 保留（263-264）→ Web 兜底（268）。
2. 运行现成测试：`go test ./pkg/rest/ -run 'TestUnknownAPIPathsReturn404|TestMCPRouteToggle' -v`，对照 [pkg/rest/server_test.go:708-752](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/rest/server_test.go#L708-L752)（它对 `/api`、`/api/`、`/api/missing`、`/api/v1/missing`、`/api/web/missing` 等七条路径逐一断言 404）。
3. **思想实验**（不必改源码）：如果把 L263-264 两行移动到 L268 之后重新编译，再请求 `GET /api/v1/missing` 会得到什么？先写下你的预测，再说明理由。

**需要观察的现象**：测试输出 PASS，所有未知 API 路径返回 404；思想实验的预测是「返回 200 + index.html 的 HTML」，理由是 mux 按注册顺序匹配、`PathPrefix("/")` 先注册即命中。**测试运行结果待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`/api` 命名空间保留为什么同时注册 `r.Path("/api")` 和 `r.PathPrefix("/api/")` 两条？

**答案**：两条覆盖两类路径：`/api` 精确匹配根路径本身；`/api/` 前缀匹配其下所有子路径。只注册前缀的话，裸 `/api` 仍会漏给兜底路由；只注册精确的话，`/api/anything` 会漏。`/mcp` 的处理同理（L220-225：精确一条 + 前缀一条）。

**练习 2**：panic 恢复中间件（L317 `r.Use`）注册在鉴权中间件（L275 `r.Use`）之后，这意味着什么？

**答案**：mux 中间件按注册顺序由外向内包裹，先 `Use` 的在外层。所以执行链是「CORS → 鉴权 → panic 恢复 → handler」：panic 恢复保护的是它内层的路由分发与业务 handler（最容易 panic 的地方）；鉴权中间件自身逻辑极简（读头、比对），在恢复层之外，若它 panic 会由 Go net/http 自身的连接级 recover 兜底（连接被断开，进程不退出）。

### 4.3 双层鉴权：API token 与 Web 会话

#### 4.3.1 概念说明

服务可能同时服务两类调用方：**浏览器里的 Web UI**（人，用登录表单）和 **脚本/API 客户端/MCP 工具**（程序，用 token）。项目用一套中间件统一放行两者：

- **API token**（`startCfg.ApiToken` 非空时启用）：请求头 `X-Api-Token: <token>` 或 `Authorization: Bearer <token>`；
- **Web 会话**（`WebEnable` 且配置了 `WebAuth` 用户名密码时启用）：先 `POST /api/web/login` 换取 `gopeed-session` cookie，之后自动携带。

判定顺序是本模块的关键：**只要请求「提供了」token 头，就只按 token 判定（对错都就此定论）；没提供 token 才轮到 cookie**。此外两条约束：登录端点 `/api/web/login` 永远放行（否则鸡生蛋问题）；配置校验要求「Web 服务 + token 必须同时配置 WebAuth 用户名密码」，防止锁死管理员。

#### 4.3.2 核心流程

```text
鉴权中间件(请求):
    保护范围 = path 以 /api/ 开头
             或 path 以 /fs 或 /fs/ 开头（Web 文件服务）
             或（MCP 启用 且 path 为 /mcp 或 /mcp/...）
    if path == /api/web/login 或 不在保护范围:
        直接放行                                # 静态资源、登录本身不需要凭证
    if 启用 token 且 请求携带了 token 头:
        token 匹配 → 放行；不匹配 → 401          # 提供了 token 就只看 token
    if 启用 Web 鉴权:
        无 cookie 或 会话无效 → 401
        会话有效 → 放行
    都不满足（未配置任何鉴权方式却处于保护范围）→ 401
```

会话的生命周期：登录成功 → 生成 32 字节随机数的 base64 sessionID → 存入内存表（过期时间 = 现在 + 7 天）→ `Set-Cookie`（HttpOnly、SameSite=Lax、HTTPS 下加 Secure）。判定时查表；`create()` 顺手清理表中已过期条目（惰性清理，没有后台协程）。

#### 4.3.3 源码精读

保护范围与放行名单、token 优先、cookie 兜底的完整判定：

- [pkg/rest/server.go:270-314](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/rest/server.go#L270-L314)：L277-279 用 `strings.HasPrefix` 判定保护范围（配合 [pkg/rest/server.go:425-431](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/rest/server.go#L425-L431) 的 `isProtectedWebFilePath`/`isMCPPath` 两个小函数），并把 `/api/web/login` 写进白名单；L284-295 是「提供了 token 就只按 token 判定」的两分支——注释写得很清楚："If an API token header is set, validate it before other authentication methods"；L297-310 是 cookie 会话判定；末尾兜底 401。401 响应统一用 `CodeUnauthorized = 1001` 信封码（[pkg/rest/model/result.go:9-10](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/rest/model/result.go#L9-L10)）。

token 的提取与常数时间比较：

- [pkg/rest/server.go:369-391](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/rest/server.go#L369-L391)：`validateAPITokenHeaders` 遍历 `X-Api-Token` 的**所有**值（一个请求可有多个同名头），再解析 `Authorization: Bearer <token>`（`strings.Fields` 分词、`EqualFold` 大小写不敏感匹配 scheme）；返回 `(provided, valid)` 二元组，正好支撑上层「提供了才判定」的语义。`constantTimeTokenEqual` 先比长度再 `subtle.ConstantTimeCompare`——**常数时间比较防时序攻击**：若用 `==`，比较耗时与匹配前缀长度相关，攻击者可逐字节猜 token。

会话存储与登录端点：

- [pkg/rest/server.go:43-92](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/rest/server.go#L43-L92)：`webSessionStore` 用 `map[sessionID]过期时间` 存会话；`create()` 用 `crypto/rand` 生成 32 字节随机数、base64 编码为 sessionID、TTL 取常量 `webAuthSessionTTL = 7 天`（L38-41），并在加锁期间顺手删除已过期条目；`valid()` 查表时同样惰性删除过期项。纯内存实现意味着**重启即全部登出**——对下载器场景足够。
- [pkg/rest/server.go:233-260](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/rest/server.go#L233-L260)：`POST /api/web/login` 明文比对用户名密码（网络层交给 HTTPS，见练习 3），成功后 `http.SetCookie`：`Path=/`、`MaxAge/Expires` 对齐 7 天、`HttpOnly`（JS 不可读，防 XSS 窃取）、`SameSite=Lax`（防 CSRF）、`Secure` 由 [pkg/rest/server.go:433-439](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/rest/server.go#L433-L439) 的 `requestUsesHTTPS` 判定（直连 TLS 或 `X-Forwarded-Proto: https`，兼容反向代理）。失败统一 401，不区分「用户名错」还是「密码错」（避免账号枚举）。

「token 必须搭配 WebAuth」的配置校验：

- [pkg/rest/model/server.go:50-59](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/rest/model/server.go#L50-L59)：`Validate` 规定 `WebEnable && ApiToken != ""` 时必须提供非空用户名密码。原因：Web 版开着 token 却没有登录入口，浏览器用户将永远无法通过鉴权（浏览器不会带 token 头）——等于把自己锁在门外。对应测试 [pkg/rest/server_test.go:996](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/rest/server_test.go#L996) `TestBuildServerRejectsWebAPITokenWithoutWebAuth`。

#### 4.3.4 代码实践

**实践目标**：用测试复现鉴权判定的关键分支（无需启动真实服务）。

1. 通读 [pkg/rest/server_test.go:1018-1134](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/rest/server_test.go#L1018-L1134) 的 `TestAuthorization`，把它断言的场景整理成下表：

| 场景 | 期望 |
| --- | --- |
| 错误用户名/密码登录 | 401 |
| 无凭证访问 `/api/v1/config` | 401 |
| `Bearer invalid` / 无效 cookie | 401 |
| 正确登录后带 cookie 访问 | 200，且 sessionID 只出现在 Set-Cookie、不出现在响应体 |
| 无凭证访问 `/fs/extensions/...`、`/fs/tasks/...` | 401（先鉴权后 404） |
| 带 `X-Api-Token` 访问 | 200 |
| 同时带有效 cookie 与 token | 200（token 优先） |
| 同时带有效 cookie 与**空** token 头 | 401（提供了 token 头即只按 token 判定，空值不匹配） |

2. 运行 `go test ./pkg/rest/ -run 'TestAuthorization|TestApiToken|TestWebSessionStoreExpiry' -v`。
3. 重点体会最后两行的差异：`X-Api-Token: <正确值>` 与 `X-Api-Token: ""`（空串也是「提供了」），对照 `validateAPITokenHeaders` 的 `provided = true` 无条件置位逻辑。

**需要观察的现象**：上述测试全部 PASS，表格中每个期望与断言一一对应。**运行结果待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么「提供了 token 就只按 token 判定」，而不是「token 或 cookie 任一有效即放行」？

**答案**：明确优先级可以避免语义混乱与安全漏洞。设想客户端本想用 token（如 MCP 工具），但请求被反向代理注入了一个过期 cookie——若「任一有效即放行」，过期 cookie + 错误 token 的组合会被 cookie 判定拒绝、token 判定也可能被短路；而「提供了 token 就只看 token」让判定结果可预测：token 是显式凭证，显式凭证出错就该显式失败，而不是静默降级到另一种机制。`TestAuthorization` L1127-1133 正是钉死这个行为：有效 cookie + 空 token = 401。

**练习 2**：会话为什么不用 JWT 而用服务端内存表？

**答案**：内存 sessionID 的优点是**可即时吊销**（重启即失效、删除条目即失效）且实现极简（约 50 行、无依赖）；JWT 是无状态的，签发后无法在过期前吊销，还要处理密钥保管。对单进程、单管理员的下载器 Web 服务，内存表的成本（重启登出一次）远低于引入 JWT 的复杂度。

**练习 3**：登录端点明文比对用户名密码，传输安全靠什么？

**答案**：靠传输层。`requestUsesHTTPS` 只影响 cookie 的 `Secure` 标志；若服务以裸 HTTP 暴露在公网，密码会明文过网。生产部署应置于 HTTPS 反向代理之后（这也是 `X-Forwarded-Proto` 判定存在的原因），或仅监听 `127.0.0.1`/内网（`StartConfig.Init` 的默认地址就是 `127.0.0.1:0`，见 [pkg/rest/model/server.go:61-67](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/rest/model/server.go#L61-L67)）。

### 4.4 静态资源与文件系统：/fs、embed 与 gzip

#### 4.4.1 概念说明

Web 版要回答三个「文件从哪来」的问题：

1. **下载产物**：Web UI 里的图片预览、视频播放需要直接访问任务已下载的文件 → `/fs/tasks/{taskId}/相对路径`；
2. **扩展资源**：扩展的图标等静态资源 → `/fs/extensions/{identity}/相对路径`；
3. **Web UI 本身**：编译期嵌入的 Flutter Web 产物（JS/CSS/HTML）→ `/` 兜底路由。

前两者靠两个实现 `http.FileSystem` 接口（只有一个 `Open(name)` 方法）的适配器，把「URL 路径」翻译成「磁盘路径」；第三个用 `embed.FS` 包一层缓存适配器再交给 `http.FileServer`。`/fs` 前缀整体处于鉴权保护范围内（4.3 节），所以下载文件不会裸奔。

#### 4.4.2 核心流程

```text
GET /fs/tasks/<taskId>/sub/dir/file.png
  → 鉴权中间件（/fs/ 在保护范围）
  → mux 命中 PathPrefix("/fs/tasks") → http.FileServer(taskFileSystem{})
  → taskFileSystem.Open("/fs/tasks/<taskId>/sub/dir/file.png")
      → resolvePath: 剥掉前缀 "/fs/tasks"，正则 ^/([^/]+)/(.*)$ 拆出
          identity = <taskId>，path = "sub/dir/file.png"
      → task = Downloader.GetTask(identity)；不存在 → os.ErrNotExist（404）
      → os.Open(filepath.Join(task.Meta.RootDirPath(), path))   # 任务根目录 + 相对路径

GET /（Web UI）
  → 命中 PathPrefix("/") → gzipMiddleware(http.FileServer(embedCacheFileSystem))
      → embedCacheFileSystem.Open(name): 打开嵌入 FS，把返回的 File 包一层，
        其 Stat().ModTime() 统一改写为「可执行文件的修改时间」
```

#### 4.4.3 源码精读

路径拆解函数——两个文件系统共用的「剥前缀 + 正则拆两段」逻辑：

- [pkg/rest/server.go:441-456](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/rest/server.go#L441-L456)：`resolvePath` 先 `TrimPrefix` 去掉 `/fs/tasks` 或 `/fs/extensions`，再用正则把剩余部分拆成「第一段 identity + 其余 path」；不匹配（比如只有前缀没有内容）返回 `os.ErrNotExist`。

任务文件系统：

- [pkg/rest/server.go:458-473](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/rest/server.go#L458-L473)：`taskFileSystem.Open` 用 identity 查任务（`Downloader.GetTask`），再以 `task.Meta.RootDirPath()` 为根拼接相对路径打开磁盘文件。`RootDirPath()` 是任务资源（Request + Resource）的根目录计算（u3-l1 讲过 FetcherMeta 的同名方法族）：单文件任务是所在目录，多文件任务是资源根名目录。

扩展文件系统：

- [pkg/rest/server.go:475-491](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/rest/server.go#L475-L491)：同样的两段式拆解后，用 identity 查扩展、取 `Downloader.ExtensionPath(extension)` 得到扩展安装目录，拼接打开。典型用途是 Web UI 展示扩展图标。

嵌入文件系统与统一的 ModTime：

- [pkg/rest/server.go:493-553](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/rest/server.go#L493-L553)：`embedCacheFileSystem` 构造时取**可执行文件**的 ModTime 作为 `lastModTime`（拿不到就用当前时间兜底）；`Open` 把嵌入 FS 返回的 `http.File` 包成 `embedFile`，其 `Stat()` 再把 `FileInfo` 包成 `embedFileInfo`，`ModTime()` 一律返回 `lastModTime`。动机：`go:embed` 嵌入的文件没有真实的修改时间，`http.FileServer` 的 `Last-Modified`/`If-Modified-Since` 协商缓存就无从谈起；统一改写成可执行文件的构建时间后，浏览器可以拿一个稳定基准做 304 协商，二次访问不必重新下载几百 KB 的 JS。

gzip 中间件：

- [pkg/rest/gizp_middleware.go:19-34](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/rest/gizp_middleware.go#L19-L34)：请求头不含 `gzip`（`Accept-Encoding` 检查）则原样透传；否则预设 `Content-Encoding: gzip` 与 `Vary: Accept-Encoding`，用 `gzipResponseWriter`（只覆写 `Write` 落到压缩器，[gizp_middleware.go:10-17](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/rest/gizp_middleware.go#L10-L17)）包住 ResponseWriter，`defer gz.Close()` 保证尾部块写出。注意它只包在 `/` 兜底的静态路由上（[pkg/rest/server.go:268](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/rest/server.go#L268)）——API 的 JSON 响应不走这里。

#### 4.4.4 代码实践

**实践目标**：读懂 `/fs/tasks` 的打开链路并评估其路径处理。

1. 依次打开 [pkg/rest/server.go:441-456](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/rest/server.go#L441-L456)、[pkg/rest/server.go:458-473](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/rest/server.go#L458-L473)，画出从 URL 到 `os.Open` 的数据流图。
2. 运行 `go test ./pkg/rest/ -run 'TestFsExtension|TestFsExtensionFail|TestWebFsEnhance' -v`，对照 [pkg/rest/server_test.go:629-706](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/rest/server_test.go#L629-L706) 观察扩展图标路径与 404 分支。
3. 思考题（源码阅读型）：若请求 `/fs/tasks/{id}/../secret.txt`，`filepath.Join(RootDirPath, "../secret.txt")` 会清理成什么路径？`http.FileServer` 在调用 `Open` 之前会不会先清洗 URL？（提示：`filepath.Join` 会做 `Clean`；`net/http.FileServer` 内部对 URL 有 `cleanPath` 处理。）写下你追踪到的结论。**结论待本地验证**。

**需要观察的现象**：测试 PASS；能完整叙述「URL → resolvePath → GetTask → RootDirPath 拼接 → os.Open」五步。

#### 4.4.5 小练习与答案

**练习 1**：`taskFileSystem` 与 `extensionFileSystem` 为什么不直接用 `http.Dir("/某根目录")`？

**答案**：因为根目录不是固定的——每个任务有自己的 `RootDirPath()`，每个扩展有自己的安装目录，都取决于运行时状态（任务表、扩展表）。`http.FileSystem` 接口恰好只有一个 `Open` 方法，于是用适配器在 `Open` 内动态解析 identity 再定位根目录，把「按 ID 寻址」翻译成「按磁盘路径寻址」。

**练习 2**：gzip 中间件为什么手动预设 `Vary: Accept-Encoding` 响应头？

**答案**：同一 URL 可能返回压缩或未压缩两种表述（取决于请求是否带 `Accept-Encoding: gzip`）。`Vary` 告诉中间的 HTTP 缓存（浏览器缓存、CDN）：缓存键必须包含 `Accept-Encoding`，否则可能把 gzip 响应发给不支持解压的客户端。

### 4.5 apiServerManager：运行中 API 服务的生命周期

#### 4.5.1 概念说明

桌面/移动端默认不需要 HTTP 端口（走 FFI/gomobile 桥接，见 u1-l1/u8-l3），但用户可能想在本机开一个 API 服务给脚本或 MCP 工具用。这个「进程内可选 API 服务器」由 `apiServerManager` 管理，导出四个操作：`GetAPIServerState` / `StartAPIServer` / `StopAPIServer` / `RestartAPIServer`（调用方是 [bind/desktop/main.go:56-71](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/bind/desktop/main.go#L56-L71) 与 [bind/mobile/main.go:35-47](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/bind/mobile/main.go#L35-L47) 的原生桥接接口）。

它的核心设计是**双状态模型**：

- **desired（期望配置）**：用户在界面里保存的 `base.APIServerConfig`，持久化在存储里（`desiredAPIServerConfigLocked` 每次实时读取）；
- **active（实际运行）**：`apiServerManager` 字段里记录的当前监听配置。

两者不一致时 `APIServerState.PendingApply = true`，提示前端「配置改了但还没生效」。所有操作都在 `runtimeMu` 写锁内完成（与 `Start`/`Stop` 串行）。

#### 4.5.2 核心流程

```text
StartAPIServer():
    desired = 读持久化配置
    if !desired.Enable: 报错「API server is disabled in persisted config」
    startLocked(desired):
        已在运行且配置相同 → 幂等返回端口
        已在运行且配置不同 → 报错（提示应先 Restart/Stop）
        未运行 → startWithConfigLocked: buildServer(…, continueOnStartup=false)
                  → 记录 server/端口/activeConfig → go serve()

serve()（后台协程）:
    err = server.Serve(listener)
    正常关闭（ErrServerClosed）→ 平静返回
    意外退出 → 加锁清空 server/端口/activeConfig，记 lastError，打错误日志

StopAPIServer(): stopLocked()
    Shutdown（3 秒超时）→ 失败则强制 Close → 清 unix socket 文件 → 清状态

RestartAPIServer():
    desired 与 active 相同 → no-op
    否则 stopLocked() 后 startLocked(desired)

GetAPIServerState(): 汇报 Running/RunningPort/PendingApply/LastError 等
```

#### 4.5.3 源码精读

管理器结构与四个导出操作：

- [pkg/rest/api_server_manager.go:15-29](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/rest/api_server_manager.go#L15-L29)：包级单例 `apiServer` 保存 `server`、`runningPort`、`activeConfig`、`lastError`；`GetAPIServerState` 先取 desired 再汇报状态。
- [pkg/rest/api_server_manager.go:74-86](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/rest/api_server_manager.go#L74-L86)：`desiredAPIServerConfigLocked` 从 `Downloader.GetConfig().API` 实时读取并 `Init()` 补默认值——期望状态永远是「存储里的最新值」，不缓存。

启动的幂等与拒绝语义：

- [pkg/rest/api_server_manager.go:88-124](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/rest/api_server_manager.go#L88-L124)：`startLocked` 在已运行时区分两种情况：配置相同 → 返回现有端口（幂等）；配置不同 → 明确报错而不是静默换配置（避免端口/语义突变）。`startWithConfigLocked` 调 `buildServer(startConfig, false)`（第二个参数即 4.1 节提到的 `continueOnStartup=false`：重启 API 服务器不重复触发任务自动续跑），从 `*net.TCPAddr` 取实际端口后 `go m.serve(...)`。

后台协程的意外退出处理：

- [pkg/rest/api_server_manager.go:126-143](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/rest/api_server_manager.go#L126-L143)：`serve` 只有在非 `ErrServerClosed` 退出时才清理状态；注意 L132 的守卫 `if m.server == server`——防止「旧 server 的延迟错误」误删「新 server」的状态（先 Stop 再 Start 后，旧协程才返回错误的场景）。

优雅关闭与强制回退：

- [pkg/rest/api_server_manager.go:145-175](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/rest/api_server_manager.go#L145-L175)：`stopLocked` 给 `Shutdown` 3 秒超时（等在途请求排空），超时则 `server.Close()` 强制断连；unix socket 场景还要删掉 socket 文件（否则下次 `net.Listen` 会因文件已存在失败，对应 `buildServer` 里 L185-187 的 `util.SafeRemove`）。

状态汇报与 PendingApply：

- [pkg/rest/api_server_manager.go:193-215](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/rest/api_server_manager.go#L193-L215)：`stateLocked` 组装 [pkg/rest/model/server.go:87-96](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/rest/model/server.go#L87-L96) 的 `APIServerState`；`apiServerConfigApplied` 定义「已生效」：desired 关闭时要求 active 为空，desired 开启时要求 active 存在且配置全等。字段注释点明架构意图："The persisted APIServerConfig remains the desired state"（持久化配置是期望态，`APIServerState` 描述实际运行的监听器）。

#### 4.5.4 代码实践

**实践目标**：通过测试观察启停与意外失败路径。

1. 阅读 [pkg/rest/invoke_test.go:41-190](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/rest/invoke_test.go#L41-L190) 中的 `TestStartAPIServerIsNotBlockedByPendingDispatch` 与 `TestRestartAPIServerFailsFastAndLeavesListenerStopped`。
2. 运行 `go test ./pkg/rest/ -run 'TestStartAPIServerIsNotBlockedByPendingDispatch|TestRestartAPIServerFailsFastAndLeavesListenerStopped' -v`。
3. 对照 4.5.2 的流程图，标出测试每一步触发了哪个函数（`startLocked`/`stopLocked`/`restartLocked`/`serve`）。

**需要观察的现象**：测试 PASS；`RestartAPIServer` 在配置非法时「快速失败且不留半开监听器」——这正是 `stopLocked` 先行、`startLocked` 后败时状态已被清空的体现。**运行结果待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：`StartAPIServer` 在已运行且配置不同时为什么「报错」而不是「自动改成新配置」？

**答案**：隐式改配置会造成端口漂移与语义突变（客户端还连着旧端口），而且「改配置」可能伴随地址变化导致失败，错误归属不清。明确报错把决策交还调用方：UI 收到错误后提示用户走 `RestartAPIServer`（它会按「先停干净再启」的顺序执行，见 L177-191），失败路径也只发生在 restart 一处。

**练习 2**：`Start`（4.1 节）与 `StartAPIServer` 都会启动 HTTP 服务，两者什么关系？

**答案**：`Start` 是宿主进程的「总启动」，其中 NativeMode 分支读取持久化 API 配置后**内部走的就是** `apiServer.startWithConfigLocked`（[pkg/rest/server.go:142](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/rest/server.go#L142)）——所以桌面 App 开机时若配置了 API 服务，开机即监听；`StartAPIServer` 则是运行期的「按需开」，供 UI 开关使用。两者共享同一个 `apiServer` 单例与 `runtimeMu`，互斥且幂等。

## 5. 综合实践

**任务**：启动一个带 API token 与 Web 账号密码的完整 Web 服务，用 curl 亲测三种鉴权情况，并给出命名空间顺序问题的书面解释。

**路径 A（完整构建，需要 Flutter 环境）**：

1. 按 README 构建 Web 版（[README.md:184-188](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/README.md#L184-L188)）：

   ```bash
   cd ui/flutter && flutter build web && cd ../..
   rm -rf cmd/web/dist
   cp -r ui/flutter/build/web cmd/web/dist
   go build -tags nosqlite,web -ldflags="-s -w" -o bin/gopeed github.com/GopeedLab/gopeed/cmd/web
   ```

2. 启动服务（参数定义见 [cmd/web/flags.go:51-57](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/cmd/web/flags.go#L51-L57)：`-u/-p` 是 Web 登录账号密码，`-T` 是 API token，且 `-T` 要求必须配 `-u/-p`——对应 4.3 节的 `Validate` 规则）：

   ```bash
   ./bin/gopeed -P 19999 -u admin -p secret -T mytoken
   ```

3. 用 curl 验证三种情况（预期行为均有测试背书，见 `TestAuthorization`）：

   ```bash
   # 情况一：X-Api-Token —— 期望 200，信封 code=0
   curl -i -H 'X-Api-Token: mytoken' http://127.0.0.1:19999/api/v1/config

   # 情况二：登录 cookie —— 先登录拿 Set-Cookie，再带 cookie 访问
   curl -i -c cookies.txt -X POST -H 'Content-Type: application/json' \
        -d '{"username":"admin","password":"secret"}' \
        http://127.0.0.1:19999/api/web/login        # 期望 200 + Set-Cookie: gopeed-session=…
   curl -i -b cookies.txt http://127.0.0.1:19999/api/v1/config   # 期望 200
   curl -i -b cookies.txt -H 'X-Api-Token: wrong' \
        http://127.0.0.1:19999/api/v1/config        # 期望 401（token 优先，错 token 不降级）

   # 情况三：未授权 —— 期望 401 + code=1001
   curl -i http://127.0.0.1:19999/api/v1/config
   curl -i http://127.0.0.1:19999/fs/tasks/xxx/file.png   # /fs 也在保护范围
   curl -i http://127.0.0.1:19999/api/v1/missing          # 期望 404（不是 index.html）
   curl -i http://127.0.0.1:19999/                        # 期望 200，Web UI 的 index.html
   ```

4. 写下对下面问题的解释（150 字以内）：`/mcp` 与 `/api` 命名空间为什么必须注册在 Web 静态兜底路由之前？

**路径 B（无 Flutter 环境的替代）**：直接运行行为等价的测试集并整理输出：

```bash
go test ./pkg/rest/ -run 'TestAuthorization|TestApiToken|TestUnknownAPIPathsReturn404|TestMCPRouteToggle' -v
```

把每个子测试对应到上面 curl 的哪一条命令。

**参考答案（第 4 步）**：gorilla/mux 按注册顺序匹配路由，`PathPrefix("/")` 的 Web 兜底路由会吃掉一切未被认领的路径（SPA 需要它响应 `/settings` 这类前端路由）。若兜底先注册，`GET /api/v1/missing`、`GET /mcp`（未启用时）会命中兜底、返回 200 + `index.html`：API 客户端会把 HTML 当 JSON 解析而报出误导性错误，且无法区分「端点不存在」与「端点存在但鉴权失败」；MCP 客户端同样无法探测端点是否启用。因此先显式注册 `/api`、`/api/`、`/mcp/`（以及未启用时的 `/mcp`）为 `NotFoundHandler`「保留命名空间」，让这两个前缀下的未知路径得到语义正确的 404。**全部 curl 输出待本地验证**。

## 6. 本讲小结

- `pkg/rest` 提供 `Start`/`Stop`（托管式，原生端用）与 `BuildServer`（组装式，服务端部署用）两条启动路径，共享 `initializeCore` 建立的唯一 Downloader + APIService 单例；`Stop` 按「关 HTTP → 退订事件 → 关核心 → 重置全局」有序清理。
- `buildServer` 的组装顺序即语义：API 路由 → `/mcp` 端点与命名空间保留 → 登录端点 → `/api` 命名空间保留 → Web 文件路由（`/fs/tasks`、`/fs/extensions`、`/` 兜底）→ 鉴权 → panic 恢复 → CORS；命名空间保留必须先于兜底注册，否则未知 API/MCP 路径会收到 SPA 的 HTML。
- 双层鉴权一次判定：保护范围为 `/api/`、`/fs`、`/mcp`；`/api/web/login` 永远放行；提供了 token（`X-Api-Token` 或 `Bearer`）就只按 token 判定（常数时间比较防时序攻击），否则查 7 天 TTL 的内存会话 cookie（HttpOnly + SameSite=Lax + HTTPS 下 Secure）。
- 文件服务由三个 `http.FileSystem` 适配器构成：`taskFileSystem`/`extensionFileSystem` 把「ID + 相对路径」翻译到任务根目录/扩展安装目录，`embedCacheFileSystem` 把嵌入资源 ModTime 统一为可执行文件时间以支撑缓存协商；gzip 只包静态兜底路由。
- `apiServerManager` 用 desired（持久化配置）/ active（实际监听）双状态模型管理进程内可选 API 服务器：启动幂等、配置不同报错、3 秒优雅关闭加强制回退、`PendingApply` 提示未生效配置；调用方是桌面/移动原生桥接层。
- 本轮增量在 REST 层的落点：`POST /api/v1/tasks/batch` 改用 `CreateDirectBatchWithResult`，响应由 ID 数组变为 `CreateTaskBatchResult`（`taskIds` + 可选 `parentId`），配套新增 `TestCreateDirectTaskBatchWithGroup` 测试。

## 7. 下一步学习建议

- 下一讲 **u8-l3 原生桥接**：桌面 FFI 与移动 gomobile 如何通过 `bind/native` 的 `invokeExecutor` 把 `InvokeAsync` 请求送进 `rest.Dispatch`，以及 `PauseInvokesAndWait` 的有限时间排水——你会看到本讲 `initializeCore` 单例守卫在「宿主先 Start、桥接共享核心」场景中的关键作用。
- 若对 MCP 端点意犹未尽，可跳读 **u8-l5**：`mcpserver.NewHandler` 如何复用同一个 Downloader 暴露 AI 工具。
- 部署视角的端口/白名单/token 加固实践见 **u8-l6**（cmd/web、cmd/api 与 Docker）。
- 事件流如何从 Downloader 流到前端（`SubscribeTaskEvents` 的位掩码订阅，本讲 `Stop` 里那行退订的完整故事）见 **u8-l4**。
