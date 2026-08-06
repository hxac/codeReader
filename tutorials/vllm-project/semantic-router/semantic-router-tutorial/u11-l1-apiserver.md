# API Server 管理 API

## 1. 本讲目标

vLLM Semantic Router（SR）的核心流量由 Envoy 承载，但「配置怎么改、分类能力怎么调、知识库怎么管、服务是否就绪」这些问题，都由一个独立的 **管理面 HTTP 服务** 回答——它就是 `pkg/apiserver`。本讲结束后，你应该能够：

1. 说清楚 apiserver 与 Envoy/ExtProc 数据面的分工，以及它如何与 `routerruntime.Registry` 共享运行时依赖。
2. 画出一次管理请求从「进来」到「命中 handler」的中间件链路，并能解读路由目录的分组方式。
3. 解释配置部署的三态模型（source / runtime / active）、ETag 的乐观并发作用，以及热重载如何被「先校验、后切换」地同步到数据面。
4. 看懂鉴权的角色-权限矩阵，并用 OpenAPI 规范自己查出某个能力端点（如 classify）的入参。

## 2. 前置知识

在进入源码前，先建立三个直觉：

- **控制面 vs 数据面**：推理请求（`/v1/chat/completions` 这类）走 Envoy → ExtProc 这条**数据面**（见 u4-l3、u5 系列）；而管理、调试、配置变更走 apiserver 这条**控制面**。两者共享同一个进程里的 `Registry` 依赖容器（见 u4-l2），所以 apiserver 改了配置，数据面立刻能用上。
- **配置的「三态」**：磁盘上有一份用户写的**源配置**（source config，`config.yaml`）；启动时可能按运行环境派生出一份**运行时配置**（runtime config）；进程内存里还有一份正在生效的**活动配置**（active config）。本讲的关键之一就是理解这三者如何通过文件写盘 + 文件监听 + 原子发布保持一致。
- **乐观并发（optimistic concurrency）**：多个客户端可能同时改同一份配置。apiserver 用 **ETag**（文档内容的 SHA-256 摘要）+ `If-Match` 请求头实现「读时拿到版本号，写时校验版本号没变」，避免盲写覆盖别人。

> 名词速查：`ExtProc`（Envoy External Processing，外部处理协议）、`Registry`（运行时依赖容器）、`ETag`（实体标签，内容的指纹）、`If-Match`（HTTP 条件请求头，要求资源仍是某个版本）。

## 3. 本讲源码地图

本讲涉及的文件都位于 `src/semantic-router/pkg/apiserver/` 下（除配置定义外），按下表分四组对应四个最小模块：

| 文件 | 作用 | 所属模块 |
| --- | --- | --- |
| `server.go` | 服务入口 `InitWithOptions`、`setupRoutes` 装配路由、health/ready handler | 路由组织 |
| `routes.go` | 路由元类型（`apiRoute`）、`apiRoutes()` 总装入口 | 路由组织 |
| `routes_catalog.go` | 按业务域分组的路由目录（health/classify/info/config/memory/...） | 路由组织 |
| `route_policy.go` | 权限 `RoutePermission`、敏感度 `RouteSensitivity`、审计动作枚举 | 路由组织 / 鉴权 |
| `middleware.go` | 统一包装器 `wrapRouteHandler`：请求 ID、鉴权、请求体上限 | 路由组织 / 鉴权 |
| `route_config_etag.go` | ETag 计算 `configDocumentETag` 与乐观并发检查 `checkConfigPrecondition` | 配置部署 |
| `route_router_config_update.go` | `PUT/PATCH /config/router`、提交 `commitRouterConfigDocument`、运行时激活等待 | 配置部署 |
| `route_config_deploy.go` | `GET /config/router`、回滚、版本列表、`/config/hash` 三态对比 | 配置部署 |
| `route_recipe_config.go` | recipe 子资源的增删改查（强制 `If-Match`） | 配置部署 |
| `runtime_config_sync.go` | 源/运行时配置路径解析与运行时配置同步 | 配置部署 |
| `runtime_config.go` | `liveRuntimeConfig`、`publishConfigMutation` 把新配置推给运行时 | 配置部署 |
| `route_classify.go` | `classify/intent`、`classify/pii`、`eval` 等分类能力端点 | 能力端点 |
| `route_kbs.go` | 知识库（KB）的列表/创建/更新/删除 | 能力端点 |
| `config.go` | 请求/响应 DTO（`BatchClassificationRequest`、`EmbeddingRequest` 等）与服务器结构体 | 能力端点 |
| `auth.go` | `authorize` / `authorizeBearer`、令牌解析、`hasPermission` | 鉴权 |
| `openapi_spec.go` | 从路由目录自动生成 OpenAPI 3.0 规范 | OpenAPI |
| `route_api_doc.go` | `/api/v1` 概览、`/openapi.json`、`/docs` Swagger UI | OpenAPI |
| `pkg/config/management_api.go` | 管理面监听器与鉴权的配置默认值、角色矩阵 | 鉴权 |

> 说明：本包所有 `.go` 顶部都带 `//go:build !windows && cgo`，即 apiserver 只在非 Windows 且开启 CGO 时编译——因为分类与嵌入依赖本地推理绑定（见 u12-l4）。

## 4. 核心概念与源码讲解

### 4.1 管理 API 路由组织与中间件

#### 4.1.1 概念说明

apiserver 是一个**单进程内的 HTTP 服务**，它不和 Envoy 抢流量，而是另开一个监听端口（默认 `127.0.0.1:8080`，仅本地），对外暴露管理能力。它的设计哲学是「**路由目录即真相**」：所有路由集中在一个目录里声明，每条路由自带元数据（方法、路径、描述、权限、敏感度、审计动作、请求体规格），中间件、OpenAPI 文档、API 概览全都从这份目录派生，避免「路由注册」和「文档/权限」各写一遍而失同步。

它和 u4-l2 讲的 `Registry` 的关系是：apiserver 是 Registry 的**消费者**，启动时拿到 Registry 指针，每个请求现读其中的 config、classification service、memory store 等依赖；同时它也是**生产者**之一，配置变更时把新配置写回 Registry，让数据面立即生效。

#### 4.1.2 核心流程

一次管理请求的生命周期：

```
HTTP 请求
   │
   ▼
http.ServeMux 按 "METHOD /path" 分发（Go 1.22 路径匹配，支持 {name} 占位）
   │
   ▼
route.bind(s) → wrapRouteHandler 包装
   │   1. 生成/透传 X-Request-Id
   │   2. 鉴权 policy.authorize(route, r) → 失败直接写错误返回
   │   3. 用 http.MaxBytesReader 给请求体设上限
   │   4. 把 principal + requestID 塞进 context
   ▼
真正的业务 handler（如 handleIntentClassification）
   │
   ▼
调用 Registry 里的依赖（classificationSvc / config / memoryStore）
   │
   ▼
writeJSONResponse 返回
```

关键点：**鉴权发生在业务 handler 之前**，由统一的包装器完成，业务 handler 只关心业务，不必各自重复写鉴权代码。

#### 4.1.3 源码精读

**服务入口与路由装配**。`InitWithOptions` 解析配置、装配依赖、构造 `ClassificationAPIServer`，再用 `setupRoutes` 把路由目录注册进标准库的 `http.ServeMux`：

- 服务器结构体持有所有运行时依赖（[config.go:17-34](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/config.go#L17-L34)）：`classificationSvc`、`config`、`runtimeConfig`、`runtimeRegistry`、`memoryStore` 等，正是 u4-l2 Registry 里那批对象的镜像。
- `setupRoutes` 遍历 `apiRoutes()` 逐条 `mux.HandleFunc`（[server.go:309-315](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/server.go#L309-L315)），用 `route.pattern()`（`"METHOD /path"`）作为注册键。
- `apiRoutes()` 是总装入口，把若干分组拼成一个切片（[routes.go:77-88](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/routes.go#L77-L88)）。

**路由目录按业务域分组**。`routes_catalog.go` 把端点拆成若干 `apiXxxRoutes()` 工厂函数，每个分组返回一组 `apiRoute`：

- `apiClassifyRoutes()` 集中声明所有分类/评估端点（[routes_catalog.go:40-115](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/routes_catalog.go#L40-L115)），如 `/api/v1/classify/intent`、`/api/v1/eval`、`/api/v1/embeddings`。
- `apiRecipeRoutes()` 与 `apiNonRecipeConfigRoutes()` 声明配置类端点（[routes_catalog.go:157-268](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/routes_catalog.go#L157-L268)），前者管 recipe 子资源，后者管整份配置与知识库。
- 每条路由用 `managedRoute(元数据, routePolicy, handler, 可选请求体)` 构造，把权限/敏感度/审计动作绑死在路由声明里（[route_policy.go:56-73](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/route_policy.go#L56-L73)）。

**统一中间件**。`wrapRouteHandler` 是所有路由的公共壳（[middleware.go:23-42](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/middleware.go#L23-L42)），它依次做四件事：设请求 ID、鉴权、请求体限流、注入上下文。鉴权失败时调用 `writeManagementError` 返回结构化错误（[middleware.go:62-77](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/middleware.go#L62-L77)），错误体里带 `code`、`message`、`request_id`、`timestamp`。

**三类路由标签**。`route_policy.go` 定义了正交的三套枚举，用来描述每条路由的治理属性：

- **权限** `RoutePermission`（[route_policy.go:8-20](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/route_policy.go#L8-L20)）：如 `PermClassifyInvoke`、`PermConfigRead`、`PermConfigWrite`、`PermSecretView`、`PermDataWrite`。
- **敏感度** `RouteSensitivity`（[route_policy.go:23-31](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/route_policy.go#L23-L31)）：`public`/`operational`/`config`/`secret_view`/`mutation`，用于决定响应是否需要脱敏。
- **审计动作** `RouteAuditAction`（[route_policy.go:36-48](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/route_policy.go#L36-L48)）：所有写操作都打一个不可变审计标签，如 `config.put`、`recipe.save`、`knowledge_base.delete`、`memory.delete`。

#### 4.1.4 代码实践

**实践目标**：用一个只读请求验证路由目录与中间件链确实在工作，并观察到自动生成的 `X-Request-Id`。

**操作步骤**：

1. 按 u1-l3 / u4-l1 的方式本地起一个路由器（`make cpu-local` 或 `vllm-sr serve`），管理面默认监听 `127.0.0.1:8080`。
2. 访问 API 概览端点，列出所有路由：
   ```bash
   curl -s http://127.0.0.1:8080/api/v1 | head
   ```
3. 故意用一个不存在的路径，观察 404（标准 `ServeMux` 行为）。
4. 对 `/health` 发请求并显式带上一个自定义请求 ID，看响应是否透传：
   ```bash
   curl -i -H "X-Request-Id: my-trace-001" http://127.0.0.1:8080/health
   ```

**需要观察的现象**：

- `/api/v1` 返回的 JSON 里 `endpoints` 数组应与 `routes_catalog.go` 声明的端点一一对应。
- `/health` 响应头里 `X-Request-Id` 应为 `my-trace-001`（透传），不带该头时则是一个自动生成的 UUID。

**预期结果**：`/health` 返回 `{"status": "healthy", "service": "classification-api"}`（见 [server.go:318-322](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/server.go#L318-L322)）。若路由器还在启动，`/ready` 会返回 503 与启动阶段信息。

> 待本地验证：具体端口取决于你的 `management_api.port` 配置或 `--api-port` 启动参数；若改过请相应替换。

#### 4.1.5 小练习与答案

**练习 1**：如果想新增一个 `GET /api/v1/foo` 端点，最少要改哪几处？
**答案**：在 `routes_catalog.go` 的某个分组（或新建分组函数）里加一条 `managedRoute(...)`，并把该分组注册进 `routes.go` 的 `apiRoutes()`。中间件、OpenAPI、概览会自动跟上，无需额外改动。

**练习 2**：为什么业务 handler 里看不到任何 `Authorization` 头解析代码？
**答案**：因为鉴权被前移到了 `wrapRouteHandler`（[middleware.go:28-33](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/middleware.go#L28-L33)），业务 handler 拿到的 `*http.Request` 已经经过了鉴权，principal 存在 context 里。

### 4.2 配置部署与 ETag 乐观并发

#### 4.2.1 概念说明

这是 apiserver 最复杂、也最值得读的一部分。它要回答一个问题：「客户端通过 HTTP 改了配置，怎么保证改得对、改得安全、并且真正生效？」

先建立**配置三态**的心智模型：

- **source**（源配置）：用户写的 `config.yaml`，是「意图」的权威记录。
- **runtime**（运行时配置）：按当前运行环境（平台、算法覆盖等）从 source 派生出的配置，路由器实际读的是它。
- **active**（活动配置）：进程内存里、ExtProc 此刻正在用的 `RouterConfig`。

一次成功的部署必须让三者最终一致：source 落盘 → 派生出 runtime → 文件监听器构建新路由器并原子替换 → active 的文档哈希更新为 runtime 的哈希。

为防止并发改写互相覆盖，apiserver 引入两条机制：

- **ETag**：配置文档字节的 SHA-256 摘要，作为该版本的「指纹」。
- **`If-Match` 乐观并发**：客户端先 GET 拿到 ETag，写时用 `If-Match: <etag>` 声明「我基于这个版本改」；服务端若发现当前版本对不上，就拒绝（412）并要求重新拉取。

#### 4.2.2 核心流程

以 `PUT /config/router/recipes/{name}`（改单个 recipe）为例：

```
1. deployMu.TryLock()              ← 全局互斥，同一时刻只有一个部署在跑
2. 读 source 文档 + 原始字节 → existingData
3. checkConfigPrecondition(..., require=true)
   │  没有 If-Match？→ 428 PRECONDITION_REQUIRED
   │  If-Match 与当前 ETag 不符？→ 412 CONFIG_CHANGED（回传当前 ETag）
   └  通过 → 继续
4. 在内存 doc 上 applyRecipeMutation（改 recipes 数组）
5. validateAndEncodeRouterConfigDocument  ← 跑 config.ParseYAMLBytes 做语义校验
6. validateHotReloadCompatibility          ← 拒绝需要重启才能生效的变更（如换本地分类器）
7. recordConfigBackup                       ← 把旧配置备份到 .vllm-sr/config-backups/
8. writeConfigAtomically(sourcePath)        ← 原子写 source（tmp + rename）
9. 若 runtime≠source：syncRuntimeConfigOrRestore  ← 调 Python CLI 派生 runtime
10. waitForRuntimeConfigActivation          ← 轮询 active 哈希 == runtime 哈希（最多 20s）
11. 返回 {status, version, etag, runtime_status, runtime_hash}
```

注意第 10 步：**写盘成功 ≠ 数据面已生效**。只有当内存里的 active 配置文档哈希追上了 runtime 文件哈希，才说明文件监听器已经构建好新路由器并原子替换完毕，此时才算「真正生效」。

#### 4.2.3 源码精读

**ETag 与乐观并发**。`configDocumentETag` 对文档字节做 SHA-256，输出带引号的 ETag 字符串（[route_config_etag.go:14-17](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/route_config_etag.go#L14-L17)）。核心是 `checkConfigPrecondition`（[route_config_etag.go:23-59](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/route_config_etag.go#L23-L59)），它有一个关键参数 `require`：

- recipe 子资源端点（读-改-写共享文档）传 `require=true`，**强制要求 `If-Match`**，缺失则返回 428（`configPreconditionRequiredStatus`，[route_config_etag.go:12](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/route_config_etag.go#L12)）。
- 整文档 `PUT/PATCH /config/router` 传 `require=false`，`If-Match` 可选，向后兼容。
- `If-Match: *` 是通配，表示「只要资源存在就放行」（[route_config_etag.go:46-48](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/route_config_etag.go#L46-L48)）。

**全局部署互斥**。`deployMu sync.Mutex`（[route_config_deploy.go:25](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/route_config_deploy.go#L25)）保证同一时刻只有一个部署操作；用 `TryLock()` 抢不到就立刻返回 409 `DEPLOY_IN_PROGRESS`，而非阻塞排队（见 `handlePutRecipe` [route_recipe_config.go:124-127](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/route_recipe_config.go#L124-L127)）。

**提交管线**。`commitRouterConfigDocument` 是所有配置写操作的公共收尾（[route_router_config_update.go:331-379](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/route_router_config_update.go#L331-L379)）：备份 → 写文件 → 等激活 → 按激活状态选 HTTP 状态码。若 `runtimeStatus == "pending"`（20s 内没激活），返回 **202 Accepted** 并提示客户端轮询 `/config/hash`；若 `active`，返回 200 并告知「运行时激活完成」。

**等待运行时激活**。`waitForRuntimeConfigActivation`（[route_router_config_update.go:409-429](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/route_router_config_update.go#L409-L429)）以 50ms 为间隔轮询，比较 `activeConfigDocumentHash()` 与 runtime 文件哈希。没有 Registry 的旧式/测试服务器返回 `"unknown"`，保留异步行为。

**三态对比端点**。`GET /config/hash` 把三个哈希一次性给出，并用 `status` 字段总结一致性（[route_config_deploy.go:453-485](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/route_config_deploy.go#L453-L485)）：

- `hash`：source 文件哈希（向后兼容字段）。
- `runtime_hash`：runtime 文件哈希。
- `active_hash`：内存活动配置的 `DocumentHash`。
- `status`：`active_hash == runtime_hash` 即 `active`；`active_hash` 为空即 `unknown`；否则 `pending`。

**热重载兼容性闸门**。`validateHotReloadCompatibility` 调用 `config.ValidateLocalClassifierReload`（[route_router_config_update.go:162-172](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/route_router_config_update.go#L162-L172)），拒绝那些「必须重启才能生效」的变更（如更换本地分类器模型），返回 409 `RESTART_REQUIRED`——兑现 u4-l2 提到的「先校验再切换」不变式。

**把新配置推给运行时**。`publishConfigMutation`（[runtime_config.go:68-89](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/runtime_config.go#L68-L89)）在写盘后把新 `RouterConfig` 同时更新到本地字段、`liveRuntimeConfig`、Registry，并调用分类服务的 `RefreshRuntimeConfig`——这正是 u4-l2 里「API Server 通过 UpdateConfig 与 RefreshRuntimeConfig 把新配置写回 Registry」的那条反向通路。

> 安全细节：回滚端点把客户端传入的 `version` 拼进备份文件路径，因此用严格的 `configVersionPattern` 白名单（`^[0-9]{8}-[0-9]{6}(?:-[0-9]{3,9})?$`）防止路径穿越（[route_config_deploy.go:45](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/route_config_deploy.go#L45) 与 [route_config_deploy.go:275-279](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/route_config_deploy.go#L275-L279)）。

#### 4.2.4 代码实践

**实践目标**：亲手走一遍「GET 拿 ETag → 带 If-Match 写 recipe → 看三态同步」，体会乐观并发。

**操作步骤**：

1. 获取整份配置的当前 ETag（响应头里）：
   ```bash
   curl -i http://127.0.0.1:8080/config/router | grep -i etag
   ```
2. 读 recipe 集合，记下返回体里的 `etag` 字段：
   ```bash
   curl -s http://127.0.0.1:8080/config/router/recipes
   ```
3. 用上一步的 ETag 作为 `If-Match`，创建/替换一个新 recipe（body 里 `routing` 不能为空）：
   ```bash
   curl -i -X PUT http://127.0.0.1:8080/config/router/recipes/my-test \
     -H "Content-Type: application/json" \
     -H "If-Match: <上一步的 etag>" \
     -d '{"name":"my-test","routing":{"decisions":[]}}'
   ```
4. 故意用旧 ETag 再发一次，观察 412 `CONFIG_CHANGED`。
5. 轮询三态哈希，直到 `status` 变为 `active`：
   ```bash
   curl -s http://127.0.0.1:8080/config/hash
   ```

**需要观察的现象**：

- 步骤 3 成功时，响应里有新的 `etag`、`version`、`runtime_status`。
- 步骤 4 用过期 ETag 应被拒，且响应头回传**当前** ETag，提示重拉。
- 步骤 5 中 `runtime_hash` 与 `active_hash` 在激活后相等，`status` 为 `active`；若路由器还在构建，短暂为 `pending`。

**预期结果**：与 `commitRouterConfigDocument`（[route_router_config_update.go:331-379](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/route_router_config_update.go#L331-L379)）的逻辑一致：成功返回 200/201 并附新 ETag；并发抢占失败返回 409；版本不匹配返回 412。

> 待本地验证：如果你启用的是只读配置或 `configPath` 为空，写端点会返回 500 `NO_CONFIG_PATH`；本实践需要一个可写的本地 `config.yaml`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 recipe 端点强制 `If-Match`，而整文档 `PUT /config/router` 不强制？
**答案**：recipe 端点做的是「读-改-写」共享文档（先读整份、改其中一段、再写回），并发下极易覆盖；强制 `If-Match` 让客户端显式声明所基于的版本。整文档 `PUT` 是全量替换，语义上不存在「部分覆盖」风险，故 `require=false` 以保持向后兼容（见 [route_config_etag.go:19-22](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/route_config_etag.go#L19-L22) 的注释）。

**练习 2**：`/config/hash` 的 `status=pending` 说明什么？客户端该怎么办？
**答案**：说明 source/runtime 已写盘，但内存里的 active 配置还没追上（文件监听器还在构建新路由器）。客户端应轮询 `/config/hash` 直到 `status=active`，或依赖写端点返回的 202 + `runtime_status=pending` 提示。

**练习 3**：`deployMu` 用 `TryLock` 而不是 `Lock`，有什么好处？
**答案**：第二个并发部署请求不会阻塞等待，而是立即收到 409 `DEPLOY_IN_PROGRESS`，让客户端决定重试时机，避免 HTTP 连接被长时间挂起。

### 4.3 能力端点：classify / eval / kbs / embeddings

#### 4.3.1 概念说明

「能力端点」把路由器内部的分类、嵌入、知识库管理能力直接暴露成 HTTP，方便运维和评测在不发推理请求的前提下「问路由器这段文本会被怎么分类」「这个嵌入是多少」「知识库里有哪些库」。它们都是**只读或近只读**的（classify/eval/embeddings 不改配置），其权限多为 `classify.invoke` 或 `config.read`。

这些端点的共同骨架是：「解析 JSON 请求 → 调 Registry 里的服务依赖 → 序列化响应」。错误映射也有统一约定：输入相关的错（空文本、非法 task_type）映射为 400，真正的内部故障映射为 500。

#### 4.3.2 核心流程

以 `POST /api/v1/classify/intent` 为例：

```
1. parseJSONRequest → services.IntentRequest
2. classificationSvc.ClassifyIntent(req)   ← 走 u8 的信号驱动分类管线
3. 成功 → writeJSONResponse(200, response)
   失败 → writeClassificationError：
        ErrEmptyText/ErrInvalidRequestFacts → 400 INVALID_INPUT
        ErrUnknownRoutingModel              → 400 INVALID_ROUTING_MODEL
        其它                                 → 500 CLASSIFICATION_ERROR
```

知识库端点则更复杂，因为它们既改配置（`config.kbs`）又改磁盘资产（示例文件），所以走「事务式暂存（stage）→ 校验 → 写配置+同步 → 提交资产」的流程，失败可回滚。

#### 4.3.3 源码精读

**分类端点**。`handleIntentClassification` 是最典型的薄 handler（[route_classify.go:35-50](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/route_classify.go#L35-L50)）：解析 → 调 `classificationSvc.ClassifyIntent` → 写响应。统一的错误映射在 `writeClassificationError`（[route_classify.go:21-32](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/route_classify.go#L21-L32)）。

**评估端点**。`handleEvalClassification` 与 intent 的区别在于它强制 `EvaluateAllSignals = true`（[route_classify.go:55-77](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/route_classify.go#L55-L77)）：忽略决策是否引用某信号，把**所有已配置信号**都算一遍，并支持 `?trace=true` 打开投影追踪。这正是 u8-l1 里 `evaluateAllSignalsWithContext` 的对外入口，用于评测场景。

**请求/响应 DTO**。这些端点的入参类型集中在 `config.go`，例如 `BatchClassificationRequest` 带 `task_type`（`intent`/`pii`/`security`/`all`）与 `Options`（[config.go:47-51](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/config.go#L47-L51)），`EmbeddingRequest` 支持 `texts`/`images`/`model`/`dimension` 等字段（[config.go:84-93](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/config.go#L84-L93)）。

**知识库端点**。`handleListKnowledgeBases` 直接读当前配置 + 磁盘资产文档（[route_kbs.go:14-27](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/route_kbs.go#L14-L27)）；`handleCreateKnowledgeBase` 则把「写配置 + 暂存资产」封装进 `persistManagedKnowledgeBase`（[route_kbs.go:220-271](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/route_kbs.go#L220-L271)）：先用 `stageManagedKnowledgeBaseAssets` 把资产放进暂存区，配置写成功才 `Commit()`，否则 `Rollback()`。删除端点同样用 `stageManagedKnowledgeBaseRemoval` + `defer rollback` 保证资产与配置的一致性（[route_kbs.go:166-198](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/route_kbs.go#L166-L198)）。

> 衔接：知识库最终被分类器消费，对应 u8-l2 的 KB 增强类别分类（`category_kb`）；这里的「管理面写 KB」与那里的「运行时读 KB」是一体两面。

#### 4.3.4 代码实践

**实践目标**：调用一个能力端点，观察 SR 对一段文本的分类结果。

**操作步骤**：

1. 发起一次意图分类（字段名以 `IntentRequest` 为准，至少需要文本）：
   ```bash
   curl -s -X POST http://127.0.0.1:8080/api/v1/classify/intent \
     -H "Content-Type: application/json" \
     -d '{"text":"帮我写一段 Python 快排代码"}'
   ```
2. 再发起一次「全信号评估」并带上 trace：
   ```bash
   curl -s -X POST "http://127.0.0.1:8080/api/v1/eval?trace=true" \
     -H "Content-Type: application/json" \
     -d '{"text":"帮我写一段 Python 快排代码"}'
   ```

**需要观察的现象**：

- `/classify/intent` 返回命中的路由类别与置信度。
- `/api/v1/eval?trace=true` 返回的结果里应包含更多信号（即便没被决策引用）以及投影追踪细节。

**预期结果**：命中类别取决于你加载的 recipe（如 balance）；若分类服务未就绪（模型未加载），会返回 503 `UNIFIED_CLASSIFIER_UNAVAILABLE` 或 500 `CLASSIFICATION_ERROR`（见 [route_classify.go:184-193](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/route_classify.go#L184-L193)）。

> 待本地验证：响应的精确字段结构取决于 `services.IntentResponse`，可在 `pkg/services` 里进一步查阅；本实践重在跑通链路而非字段完整性。

#### 4.3.5 小练习与答案

**练习 1**：`/api/v1/classify/intent` 与 `/api/v1/eval` 在内部调用上的本质区别是什么？
**答案**：前者只算「决策引用到的信号」（懒求值），后者强制 `EvaluateAllSignals=true` 算全部信号并可选带 trace（[route_classify.go:62-68](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/route_classify.go#L62-L68)）。前者服务于在线路由，后者服务于离线评测。

**练习 2**：删除一个知识库时，如果配置写盘成功但资产删除还没提交，进程崩了，会怎样？
**答案**：`handleDeleteKnowledgeBase` 用 `defer rollbackManagedKnowledgeBaseRemoval`，只有在 `committed=true` 时才跳过回滚（[route_kbs.go:171-172](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/route_kbs.go#L171-L172)）。若在 `Commit()` 之前崩溃，下次启动时资产仍处于暂存状态、不会被部分提交，配置与资产的一致性由「先写配置、后提交资产」的顺序加事务回滚共同守护。

### 4.4 鉴权与 OpenAPI 规范

#### 4.4.1 概念说明

管理面虽然默认只监听本地（`127.0.0.1`），但一旦开启远程暴露（`remote_exposure: true`）就必须有鉴权。SR 用一套**模式（mode）+ 角色（roles）+ 权限（permissions）**的 RBAC 模型：

- **模式**：`disabled`（默认，所有路由放行，principal 视为 admin）或 `bearer`（按 Bearer 令牌鉴权）。
- **角色**：内置 `viewer`（只读）、`operator`（读 + 写数据/配置）、`admin`（通配，含 `secret_view`）。
- **权限**：即 4.1 里见到的 `RoutePermission`，每条路由声明它需要哪个权限。

与鉴权正交的是**脱敏**：即使有 `config.read` 权限，没有 `secret_view` 的角色拿到配置时，密钥字段会被打码（`maybeRedactConfigView`）。这解释了为什么 `GET /config/router` 的敏感度标的是 `secret_view` 而非普通 `config`。

另一条主线是 **OpenAPI 自描述**：apiserver 不手写接口文档，而是从同一个路由目录自动生成 OpenAPI 3.0 规范（`/openapi.json`），并用 Swagger UI（`/docs`）渲染。这保证了「代码、权限、文档」三者同源。

#### 4.4.2 核心流程

鉴权链路：

```
请求进入 wrapRouteHandler
   │
   ▼
managementAuthPolicy() 从当前配置解析 {Mode, Roles, Tokens}
   │
   ▼
policy.authorize(route, r)
   │  route.Permission == PermHealthRead? → 直接放行（匿名）
   │  Mode == disabled?                   → principal=admin, 放行
   │  Mode == bearer?                     → authorizeBearer:
   │       取 Authorization: Bearer <token>
   │       token 不在 Tokens 表? → 401 UNAUTHORIZED
   │       查出 role → hasPermission?
   │           否 → 403 FORBIDDEN
   │           是 → 放行
   ▼
principal 注入 context，handler 执行
```

OpenAPI 生成链路：

```
generateOpenAPISpec()
   遍历 apiRoutes()
   对每条路由 buildOpenAPIOperation():
       - summary/description ← route.Description
       - operationId ← 方法_路径（去标点）
       - parameters ← 从 {name} 占位提取路径参数
       - responses  ← 200 / 400 (+413 若有请求体)
       - requestBody ← 若有，按 kind 生成 JSON/multipart schema
   装配成 OpenAPISpec 返回
```

#### 4.4.3 源码精读

**鉴权策略解析**。`managementAuthPolicy` 把当前配置的 `ManagementAPI.Auth` 转成 `{Mode, Roles, Tokens}`（[auth.go:24-35](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/auth.go#L24-L35)）。其中 `Tokens` 由 `ResolvedManagementTokens()` 从环境变量解析而来——配置里只写「哪个环境变量对应哪个角色」，真正的令牌值不入配置文件（[management_api.go:196-211](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/config/management_api.go#L196-L211)），避免密钥落盘。

**鉴权决策**。`authorize` 是总分流（[auth.go:55-68](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/auth.go#L55-L68)）：健康检查类路由（`PermHealthRead`）永远匿名放行；其余按模式分派。Bearer 模式下 `authorizeBearer`（[auth.go:70-89](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/auth.go#L70-L89)）依次做「有令牌？→ 令牌有效？→ 角色有权限？」三道关，分别对应 401/401/403。

**权限匹配**。`hasPermission`（[auth.go:106-120](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/auth.go#L106-L120)）遍历角色拥有的权限列表，命中通配符 `*`（`ManagementPermWildcard`）或具体权限即放行；路由未声明权限（`required == ""`）也直接放行。

**角色矩阵**。`DefaultManagementAPIRoles`（[management_api.go:71-97](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/config/management_api.go#L71-L97)）给出三档内置角色：`viewer` 只读、`operator` 增加写、`admin` 通配（含 `secret_view`，可看明文密钥）。

**安全默认与暴露策略**。`DefaultManagementAPIConfig` 默认 `127.0.0.1:8080` + `disabled`（[management_api.go:51-61](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/config/management_api.go#L51-L61)）。`validateExposurePolicy` 强制：开 `remote_exposure` 必须同时用 `bearer` 且配了至少一个 token（[management_api.go:156-167](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/config/management_api.go#L156-L167)）；`validateBindExposureConsistency` 则防止「绑到 `0.0.0.0` 却没开远程暴露」这种危险组合。

**OpenAPI 生成**。`generateOpenAPISpec` 遍历路由目录，逐条用 `buildOpenAPIOperation` 构造操作对象（[openapi_spec.go:11-20](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/openapi_spec.go#L11-L20) 与 [openapi_spec.go:40-58](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/openapi_spec.go#L40-L58)）。注意它对请求体的 schema 是「泛型 object」而非强类型——`application/json` 的 schema 只声明 `type: object`（[openapi_spec.go:152-160](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/openapi_spec.go#L152-L160)），所以查具体字段要结合源码 DTO（如 `config.go` 里的 `EmbeddingRequest`）。

**文档端点**。`/api/v1` 返回概览与端点清单（[route_api_doc.go:28-53](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/route_api_doc.go#L28-L53)），`/openapi.json` 返回规范（[route_api_doc.go:56-59](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/route_api_doc.go#L56-L59)），`/docs` 返回一个加载 Swagger UI 的静态 HTML（[route_api_doc.go:62-105](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/route_api_doc.go#L62-L105)）。

#### 4.4.4 代码实践

**实践目标**：用 OpenAPI 规范查出 classify 端点的入参形态，并验证鉴权矩阵。

**操作步骤**：

1. 拉取 OpenAPI 规范，定位 classify 端点：
   ```bash
   curl -s http://127.0.0.1:8080/openapi.json \
     | python3 -c "import sys,json; d=json.load(sys.stdin); \
        print(json.dumps(d['paths']['/api/v1/classify/intent'], indent=2))"
   ```
2. 从规范里找到 `POST /api/v1/classify/intent` 的 `operationId`、`parameters`、`responses`，并记录 400/413 的含义。
3. 由于规范里的 requestBody 是泛型 object，结合本讲「源码精读」定位到真实 DTO：`IntentRequest`（被 `handleIntentClassification` 使用）。说明该端点至少需要 `text` 字段。
4. （可选，需要先配置 bearer）配置一个测试令牌环境变量并在 `config.yaml` 的 `management_api.auth.tokens` 里引用它，重启后验证：
   - 不带 `Authorization` 头 → 401 `UNAUTHORIZED`。
   - 带正确令牌但角色无 `config.write` → 对 `PUT /config/router` 返回 403 `FORBIDDEN`。

**需要观察的现象**：

- OpenAPI 规范里每个端点都有 `operationId`（如 `post_api_v1_classify_intent`），且带 `{name}` 占位的路径会自动生成 path 参数。
- 鉴权失败时返回的 JSON 错误体里有稳定的 `code` 字段（`UNAUTHORIZED`/`FORBIDDEN`），便于客户端程序化处理。

**预期结果**：与 `buildOpenAPIOperation`（[openapi_spec.go:40-58](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/openapi_spec.go#L40-L58)）和 `authorizeBearer`（[auth.go:70-89](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/auth.go#L70-L89)）的逻辑一致。

> 待本地验证：默认 `auth.mode=disabled`，鉴权步骤需要你先显式改成 `bearer` 并配置 token 才能复现 401/403；不配置时所有路由对本地直接放行。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `GET /config/router` 的敏感度标的是 `secret_view`，而 `/config/kbs` 标的是普通 `config`？
**答案**：整份路由配置里可能含密钥（API key、token 等），没有 `secret_view` 权限时必须脱敏；知识库配置一般不含密钥，普通 `config.read` 即可读全量（对照 [routes_catalog.go:228-232](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/routes_catalog.go#L228-L232) 与 [routes_catalog.go:191-195](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/routes_catalog.go#L191-L195)）。

**练习 2**：OpenAPI 规范里 `requestBody` 的 schema 为什么是泛型 `object`，而不是精确的 `IntentRequest` 字段表？
**答案**：`buildOpenAPIOperation` 只按请求体 `kind` 给出 `type: object`（[openapi_spec.go:107-113](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/openapi_spec.go#L107-L113)、[openapi_spec.go:152-160](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/openapi_spec.go#L152-L160)），没有把 Go 结构体反射成 JSON Schema。要查精确字段，需结合 `config.go` 里的 DTO 与各 handler 的解析逻辑。

## 5. 综合实践

把本讲四个模块串起来，完成一次「**带鉴权的配置变更 + 能力验证**」闭环：

1. **侦察**：`GET /api/v1` 与 `GET /openapi.json`，列出管理面暴露的所有能力，并用 `operationId` 在 `routes_catalog.go` 里反向定位每条路由的声明位置。
2. **基线**：`GET /config/router`（记录 ETag）与 `GET /config/hash`（记录三态哈希与 `status`）。
3. **变更**：用一个有 `config.write` 权限的令牌，带正确的 `If-Match` 调 `PUT /config/router/recipes/{name}` 新增一个最小 recipe（`routing` 含至少一条已有决策）；观察返回的 `version`、新 `etag`、`runtime_status`。
4. **确认生效**：轮询 `GET /config/hash` 直到 `status=active`，证明 source→runtime→active 三态已一致。
5. **能力验证**：用 `POST /api/v1/classify/intent` 发一段文本，对照新 recipe 的决策规则，解释返回的命中类别。
6. **清理**：带最新 ETag 调 `DELETE /config/router/recipes/{name}`（注意它要求 recipe 未被 entrypoint 引用，否则会返回 409 `RECIPE_IN_USE`），再用 `/config/router/versions` 查看这次变更留下的备份版本。

> 若本地未启用 bearer，可跳过令牌部分，但请口头复述：若开启 `remote_exposure`，第 3、6 步必须带 bearer，且令牌对应角色需含 `config.write`，否则会收到 401/403。

## 6. 本讲小结

- apiserver 是 SR 的**控制面 HTTP 服务**，与 Envoy/ExtProc 数据面分工明确，二者通过 `routerruntime.Registry` 共享同一批运行时依赖（u4-l2）。
- **路由目录即真相**：所有端点集中在 `routes_catalog.go` 声明，每条带权限/敏感度/审计/请求体元数据；中间件、OpenAPI、概览全部从它派生，避免多处失同步。
- 配置部署采用**三态模型（source/runtime/active）+ ETag 乐观并发 + 全局部署互斥**：写盘后轮询 active 哈希以确认数据面真正生效，并通过热重载兼容性闸门拒绝需重启的变更。
- 能力端点（classify/eval/kbs/embeddings）是「解析 → 调 Registry 依赖 → 序列化」的薄封装，错误统一映射为 4xx（输入）或 5xx（内部）。
- 鉴权是 disabled/bearer 双模式 + viewer/operator/admin 三角色的 RBAC，密钥靠环境变量注入不落盘，`secret_view` 独立控制明文密钥可见性。
- OpenAPI 3.0 规范由路由目录**自动生成**，但 requestBody schema 是泛型 object，精确字段需回到源码 DTO 查证。

## 7. 下一步学习建议

- **继续本单元**：u11-l2（投影追踪与可解释性）会讲解 classify/eval 端点返回的 `ProjectionTrace` 的结构；u11-l3（限流/在途/延迟/授权）展开请求路径上的治理控制；u11-l4（可观测性）讲解日志/指标/追踪如何在 apiserver 启动序列里接入。
- **回看依赖**：若对「写回 Registry 让数据面生效」想看更深，回到 u4-l2 的 `RefreshRuntimeConfig` 与 `PublishRouterRuntime`；配置解析与语义校验的细节见 u3-l3。
- **延伸阅读源码**：`kb_persistence.go`（配置+资产事务）、`config_redact.go`（密钥脱敏）、`route_router_outcomes.go`（Router Learning 反馈端点）都是本讲提及但未展开的热点，可作为进阶阅读。
