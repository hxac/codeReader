# u8-l1 pkg/api：与传输无关的服务层

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `pkg/api` 包在整体架构中的位置：它为什么必须「与传输无关」，REST、FFI、测试三端如何共享同一套 handler。
2. 读懂 `RouteSpec` 路由表的声明方式与 `Match` 的路径参数解析算法（分段匹配、`{param}` 提取、URL 解码恰好一次、`cleanPath` 归一化）。
3. 会用 `Service.Dispatch` / `Service.JSON` 在进程内直接调用任意接口，不启动任何 HTTP 服务器——这是本项目测试 API 的标准姿势。
4. 梳理 `/api/v1` 全部 29 条路由与 handler 的对应关系。
5. 说明本轮提交（1e36599）后 `POST /api/v1/tasks/batch` 的新响应结构：由任务 ID 数组改为 `CreateTaskBatchResult` 对象（`taskIds` + 可选 `parentId`），以及它与任务分组（TaskGroup）机制的关系。

## 2. 前置知识

本讲建立在前面几讲已建立的心智模型之上，先快速回顾：

- **「一个核心、多种外壳」**（u1-l1）：桌面 Dart FFI、移动 gomobile、Web REST 三条通道最终都汇入同一份业务实现。本讲要精读的 `pkg/api` 就是这份「同一份实现」的载体。
- **Downloader 主链路**（u4-l2）：任务有三类建任务入口——`CreateDirect`（延迟解析）、`Resolve`→`Create`（消费解析缓存）、批量 `CreateDirectBatchWithResult`。本讲把这些 Go API「翻译」成 HTTP 路由。
- **批量请求模型**（u2-l1）：`base.CreateTaskBatch` 由批级 `Opts` 加成员 `Req`/`Rid`/`Opts` 组装，规则为 rid 优先、成员 opts 整体替换、部分成功；`Group`/`GroupName` 建立 TaskGroup 共享预留目录并以 `ParentID` 关联成员任务。
- **响应信封**：所有接口返回统一 JSON 结构 `Result[T]{code, msg, data}`（定义在 `pkg/rest/model/result.go`）。`code=0` 表示成功，业务错误码（如 1002 参数非法、2001 任务不存在）放在信封里，而 HTTP 状态码大多仍是 200——这是「业务错误不打断传输层」的设计选择。

一个需要先建立的关键直觉：**`pkg/api` 里的 handler 不接触任何网络对象**。它们收到的 `Context` 只含纯数据（字符串方法名、路径、`url.Values` 查询参数、`[]byte` 请求体），返回的 `Response` 也只是「状态码 + 任意可序列化数据」。网络（或不网络）是外层调用者的事。这样同一份 handler 既能被 gorilla/mux 包成 HTTP 接口，也能被桌面 FFI 桥以字符串进出地调用，还能被单元测试直接调用。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `pkg/api/routes.go` | 路由基建：`RouteSpec` 结构、模式解析（`parseRouteParts`）、匹配算法（`Match`）、路径清洗（`cleanPath`）、路由表校验（`validateRoutes`） |
| `pkg/api/service.go` | 服务层主体：`Context`/`Response`/`Service` 类型、29 条路由的注册表（`NewService`）、进程内分发（`Dispatch`/`JSON`）、全部 handler 实现与响应构造 helper |
| `pkg/api/service_test.go` | 服务层测试：展示如何不启动 HTTP 服务器直接调用接口（本讲的实践蓝本） |
| `pkg/base/model.go` | 批量请求模型 `CreateTaskBatch`/`CreateTaskBatchItem`（含本轮新增的 `Group`/`GroupName`/`Rid` 字段） |
| `pkg/download/model.go` | 批量响应模型 `CreateTaskBatchResult` 与 `TaskGroup` |
| `pkg/download/downloader.go` | `CreateDirectBatchWithResult` 的批量创建实现（本讲只看签名与语义，细节在 u4-l6） |
| `pkg/rest/server.go` | 传输层消费者之一：把 `RouteSpecs()` 注册到 gorilla/mux |
| `pkg/rest/invoke.go` | 传输层消费者之二：FFI 桥，字符串进出调用 `Service.JSON` |
| `pkg/rest/model/result.go` | 响应信封 `Result[T]` 与 `RespCode` 常量 |

## 4. 核心概念与源码讲解

### 4.1 RouteSpec 与 Match：一张自研路由表

#### 4.1.1 概念说明

Gopeed 没有直接用 gorilla/mux 的路由声明来承载业务路由，而是先在 `pkg/api` 内自建了一张极简路由表：每条路由是一个 `RouteSpec`（方法 + 模式 + handler），模式支持 `{param}` 段（如 `/api/v1/tasks/{id}`）。

为什么多此一举？因为路由表要在**没有 HTTP 请求对象**的环境下也能匹配。桌面端 FFI 传来的只是一个路径字符串，测试里也只是随手写一个字符串。自建匹配算法后，`RouteSpec.Match(rawPath)` 接受任意字符串即可完成路由，gorilla/mux 只在真正需要监听端口时才登场（见 4.2.3）。

#### 4.1.2 核心流程

路由匹配的完整流程：

```text
newRoute(method, pattern, handler)
  ├── cleanPath(pattern)          # 归一化模式
  ├── parseRouteParts(pattern)    # 按 "/" 切段，{xxx} 段标记为参数段
  └── 产出 RouteSpec{Method, Pattern, Handler, parts}

Match(rawPath)
  ├── cleanPath(rawPath)          # 与模式同构的归一化
  ├── Trim(path, "/")             # 去首尾斜杠
  ├── Split(path, "/")            # 切成段数组
  ├── len(segments) != len(parts) # 段数不等 → 直接失配
  └── 逐段比较：
        参数段  → url.PathUnescape 解码后存入 params
        字面段  → 必须完全相等
```

两个值得注意的设计点：

- **段数必须严格相等**：没有通配符、没有可选段。`/api/v1/tasks` 与 `/api/v1/tasks/{id}` 是两条不同的路由，靠段数即可区分，`GET /api/v1/tasks/`（尾斜杠清洗后段数对不上）不会误匹配。
- **参数段解码恰好一次**：`url.PathUnescape` 在 `Match` 内完成，之后 `Context.PathParams` 里就是已解码的明文。测试 `TestNativePathParametersAreDecodedOnce` 专门守住了这一点（见 4.1.4），因为 REST 层的 gorilla/mux 可能已经解码过一次，若 Service 再解一次就会把 `%252F`（字面 `%2F`）错误地变成 `/`。

#### 4.1.3 源码精读

路由的数据结构与方法签名：

[pkg/api/routes.go:L10-L22](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/api/routes.go#L10-L22)

`HandlerFunc` 是本包的核心回调类型：入参 `*Context`、出参 `*Response`，全程无网络对象。`RouteSpec` 前三个字段可被外部读取（传输层注册时要用），小写 `parts` 是预解析好的模式段，属于包内私有。

构造与校验：

[pkg/api/routes.go:L24-L47](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/api/routes.go#L24-L47)

`newRoute` 在构造期就把模式切好段存进 `parts`，匹配时零解析开销；`validateRoutes` 保证路由表里没有空方法/空模式/空 handler，且 `方法 + 模式` 组合不重复——它在 `NewService` 里被调用，路由表写错会在启动期立刻炸出来而不是运行期莫名失配。

模式解析与匹配算法：

[pkg/api/routes.go:L49-L91](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/api/routes.go#L49-L91)

- `parseRouteParts`：`{`开头 `}`结尾的段识别为参数段，段名存进 `routePart.param`；其余存 `literal`。
- `Match`：先做与模式同构的清洗（`cleanPath` + 去首尾斜杠），段数不等直接返回失配；参数段用 `url.PathUnescape` 解码后写入 `params`，解码失败（如非法的 `%zz`）视为失配而非报错；字面段大小写敏感地全等比较。

路径清洗：

[pkg/api/routes.go:L93-L99](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/api/routes.go#L93-L99)

`cleanPath` 的技巧是 `pathpkg.Clean("/" + path)`：先补一个根斜杠，`Clean` 就会消灭 `//`、`./`、`../` 并保留一个前导 `/`。这使得 `/api/v1/tasks`、`/api/v1//tasks`、`/api/v1/tasks/` 都归一到同一形式——无论调用方是浏览器、FFI 还是测试代码，路径书写习惯的差异不会影响路由。

#### 4.1.4 代码实践

**实践目标**：验证 `Match` 的段数匹配与「解码恰好一次」语义，全程不需要启动任何服务。

**操作步骤**：

1. 运行项目已有的测试（这是真实存在于仓库中的测试，直接运行即可）：

   ```bash
   go test ./pkg/api/ -run TestNativePathParametersAreDecodedOnce -v
   ```

2. 阅读该测试源码：[pkg/api/service_test.go:L121-L137](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/api/service_test.go#L121-L137)。它手工 `newRoute` 了一条路由，handler 把 `PathParam("id")` 原样返回，然后对两个编码过的路径断言解码结果。

**需要观察的现象**：

- `%E5%BC%80%E5%8F%91%E8%80%85%40name` 被解码为 `开发者@name`——解码发生在 `Match` 内部。
- `literal%252Fname`（双重编码的 `%2F`）解码一次后是字面 `literal%2Fname`，而不是再变成 `literal/name`。

**预期结果**：测试通过，输出 `ok github.com/GopeedLab/gopeed/pkg/api`。（待本地验证——如果你本地没跑过，请以实际输出为准。）

#### 4.1.5 小练习与答案

**练习 1**：`GET /api/v1/tasks/?id=abc` 会被路由表中的哪条规则匹配？为什么查询参数不影响匹配？

答案：清洗后路径为 `/api/v1/tasks`，与 `GET /api/v1/tasks`（`getTasks`）匹配。查询参数根本不传给 `Match`——`Dispatch` 只把 `rawPath` 交给 `Match`，query 是独立放进 `Context.Query` 供 handler 读取的，路由只看路径。

**练习 2**：如果两条路由分别是 `PUT /api/v1/tasks/pause` 与 `PUT /api/v1/tasks/{id}/pause`，`PUT /api/v1/tasks/pause/pause` 会命中哪条？为什么不会歧义？

答案：命中后者。前者是 3 段（api/v1/tasks/pause），后者是 4 段；`pause/pause` 清洗后是 4 段，与前者段数不等直接失配，与后者逐段吻合（`{id}` 捕获 `pause`）。段数严格相等规则保证了这类「字面段与参数段相邻」的路由不会互相干扰。

**练习 3**：为什么 `Match` 里参数段解码失败时选择返回失配，而不是返回 500 错误？

答案：`Match` 的职责是「这条路径是否属于这条路由」，它没有通道去表达错误响应；把非法编码当作「不匹配」交给下一条路由尝试，最终所有路由都不匹配时由 `Dispatch` 统一返回 404（见 4.2.3）。这是把「匹配判定」与「错误响应」解耦的常见做法。

### 4.2 Service 与 Dispatch：进程内直接执行 handler

#### 4.2.1 概念说明

`Service` 是「路由表 + Downloader 引用 + 任务事件订阅」的三合一容器。它对外暴露两个执行入口：

- `Dispatch(method, path, query, body, headers)` → `*Response`：进程内执行一次请求，返回结构化响应。
- `JSON(...)` → `(int, []byte)`：在 `Dispatch` 之上再做一次 JSON 序列化，直接得到字节数组。

这两个方法就是「与传输无关」的落点：REST 服务器、FFI 桥、单元测试都以它们为唯一入口。新增一个 API 时，只需在 `NewService` 的路由表里加一行，三种传输形态同时获得该能力，桥接层零改动——这正是 u1-l1 讲过的架构承诺在代码上的兑现。

#### 4.2.2 核心流程

`Dispatch` 的执行流程：

```text
Dispatch(method, rawPath, query, body, headers)
  ├── defer recover()          # handler panic → 500 风格错误信封，进程不死
  ├── 规范化 method（大写化、去空白）
  ├── cleanPath(rawPath)
  ├── 顺序遍历 s.routes：
  │     ├── route.Method != method → 跳过
  │     ├── route.Match(path) 失配 → 跳过
  │     └── 命中 → 构造 Context{Method, Path, Query, Body, Headers, PathParams}
  │                 并返回 route.Handler(ctx)
  └── 全部未命中 → 404 + 错误信封
```

三个入口的消费关系：

```text
                    ┌─ pkg/rest/server.go  HTTP：mux 路由 → 手工构造 Context → Handler
Service.Dispatch ───┼─ pkg/rest/invoke.go  FFI：字符串 → service.JSON → 字符串
   Service.JSON ────┴─ 测试：直接调用，断言返回值
```

#### 4.2.3 源码精读

先看两个核心数据结构：

[pkg/api/service.go:L19-L44](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/api/service.go#L19-L44)

`Context` 的五个字段全是纯数据类型——`url.Values` 本质是 `map[string][]string`，`http.Header` 本质是 `map[string][]string`，`Body` 是 `[]byte`。没有任何 `io.Reader`/`*http.Request` 之类的网络对象。`Response` 同理：状态码 + `any` 类型的 body，序列化方式由调用方决定。这就是「与传输无关」在类型层面的实现。

`NewService` 与路由表：

[pkg/api/service.go:L46-L90](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/api/service.go#L46-L90)

`NewService` 做三件事：声明 29 条路由、`validateRoutes` 校验、把 `service.emitTaskEvent` 注册为 Downloader 的 Listener（任务事件管道，u8-l4 详讲）。`RouteSpecs` 返回路由表副本（`append([]RouteSpec(nil), ...)` 防止外部篡改内部切片），REST 服务器靠它注册 HTTP 路由。

`Dispatch` 与 `JSON`：

[pkg/api/service.go:L92-L129](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/api/service.go#L92-L129)

注意三个细节：第一，`defer recover` 兜住 handler 的 panic，翻译成错误信封而不是让进程崩溃——这对桌面端尤其重要，一个坏请求不能拖死整个应用。第二，业务错误（`newErrorResponse`）的 HTTP 状态码是 200，错误信息在信封的 `code`/`msg` 里；而「路由不存在」「协议任务不存在」这类资源级错误用 `newStatusResponse` 返回真正的 404。第三，`JSON` 在 `Dispatch` 之上处理序列化失败（body 无法 Marshal 时降级为错误信封）。

三个消费者如何使用 Service：

- REST HTTP 层：[pkg/rest/server.go:L199-L218](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/rest/server.go#L199-L218)。构建服务器时遍历 `APIService.RouteSpecs()`，把每条 `RouteSpec` 注册成 gorilla/mux 路由：读出请求体、手工组装 `goapi.Context`（路径参数来自 `mux.Vars(r)`）、调用 `route.Handler` 并把响应写回 `http.ResponseWriter`。HTTP 只是一层薄薄的搬运。
- FFI 桥：[pkg/rest/invoke.go:L19-L32](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/rest/invoke.go#L19-L32)。`Dispatch(method, path, rawQuery, body string) string` 四个字符串进、一个字符串出，内部解析 query 后调 `service.JSON`。注释明确说明它「不开 HTTP listener，只桥接进共享的 API Service」。
- 测试：[pkg/api/service_test.go:L71-L86](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/api/service_test.go#L71-L86)。泛型 helper `serviceTestRequest` 把「序列化请求体 → `service.JSON` → 反序列化 `Result[T]` → 校验 code」封装成一行调用，是全项目测试 API 的标准姿势。

顺带一个值得知道的考古细节：`pkg/rest/api.go` 里还残留着一组旧 handler（例如 [pkg/rest/api.go:L59-L73](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/rest/api.go#L59-L73) 的 `CreateTaskBatch`），它们直接操作 `http.ResponseWriter`。但当前 `server.go` 的注册循环只遍历 `APIService.RouteSpecs()`，这些旧函数已无调用方——读代码时以 `pkg/api/service.go` 为准，不要被同名旧文件迷惑。

#### 4.2.4 代码实践

**实践目标**：体验「零 HTTP」调用接口——用 `Service.Dispatch` 直接调用配置读写接口。

**操作步骤**：

1. 运行项目已有测试：

   ```bash
   go test ./pkg/api/ -run TestFullConfigDispatch -v
   ```

2. 阅读源码：[pkg/api/service_test.go:L88-L119](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/api/service_test.go#L88-L119)。测试先用 `Dispatch` PUT 一份含 HTTP 连接数的配置，再用 `JSON` GET 回来断言 `connections == 3`，最后验证触发不存在的协议任务返回 HTTP 404。

**需要观察的现象**：

- 整个过程没有 `net.Listen`、没有端口号、没有 HTTP 客户端，纯进程内函数调用。
- 业务校验失败（如 `jobs/missing/trigger`）的响应 `StatusCode` 是真正的 404，而 `newErrorResponse` 产生的业务错误是 200 + 信封错误码。

**预期结果**：测试通过。（待本地验证。）

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Dispatch` 用 `defer recover` 而 REST 层的服务器又有自己的 panic 恢复中间件（u8-l2 会讲到）？

答案：`Dispatch` 面向三种调用方，其中 FFI 桥（`rest/invoke.go`）没有 HTTP 中间件链可以依靠；如果 handler panic 直接冒泡到桌面端 cgo 边界，可能造成未定义行为甚至崩溃。`Dispatch` 内的 recover 是「最后一道、与传输无关」的防线，保证任何调用方拿到的都是错误信封。

**练习 2**：`Service.JSON` 与 `Service.Dispatch` 的返回类型分别适合什么调用方？

答案：`Dispatch` 返回 `*Response{StatusCode, Body any}`，适合测试与进程内调用方（可以直接断言结构化字段）；`JSON` 返回 `(int, []byte)`，适合需要把响应作为字节流传输的场景——FFI 桥要跨 cgo 边界传字符串、HTTP 层要写响应体，都是 `JSON` 的用户。

**练习 3**：`RouteSpecs()` 为什么要 `append([]RouteSpec(nil), s.routes...)` 复制一份而不是直接返回 `s.routes`？

答案：`s.routes` 是切片，直接返回会让外部拿到底层数组的引用，外部 `append` 或改写元素可能污染内部路由表。复制一份是「防御性快照」，配合小写的 `parts` 字段（包外不可构造）共同保证路由表注册后不可变。

### 4.3 handler 实现集：/api/v1 全景

#### 4.3.1 概念说明

29 条路由全部注册在 `NewService` 中，每条路由的 handler 是一个小函数：解码请求体 → 调用 Downloader 对应方法 → 包装响应。理解了几个「模式」之后，整个 handler 集可以成批读懂：

- **单任务操作归一化为过滤器操作**：`pauseTask(id)` 不是独立逻辑，而是构造 `TaskFilter{IDs: [id]}` 复用批量路径。
- **查询参数即过滤器**：批量接口从 query 里解析 `id`/`status`/`notStatus` 构造 `TaskFilter`。
- **响应构造只有四个 helper**：`newOKResponse`（数据）、`newNilResponse`（无数据）、`newErrorResponse`（业务错误，默认 code=1000）、`newStatusResponse`（真正的 HTTP 非 200）。

#### 4.3.2 核心流程

一张表覆盖全部路由（这是本讲实践任务第 1 项的成果示例）：

| 方法 | 路径 | handler | 用途 |
| --- | --- | --- | --- |
| GET | `/api/v1/info` | `info` | 版本、运行时、OS/架构、是否 Docker 环境 |
| POST | `/api/v1/resolve` | `resolve` | 解析请求，返回 `ResolveResult`（资源预览，u4-l2） |
| POST | `/api/v1/tasks` | `createTask` | 建单个任务：rid（解析缓存）或 req（直建）二选一 |
| POST | `/api/v1/tasks/batch` | `createTaskBatch` | 批量建任务，支持 rid/req 混合与分组（见 4.4） |
| PATCH | `/api/v1/tasks/{id}` | `patchTask` | 修改进行中任务的请求/选项（如 BT 追加选文件） |
| PUT | `/api/v1/tasks/{id}/pause` | `pauseTask` | 暂停单任务 |
| PUT | `/api/v1/tasks/pause` | `pauseTasks` | 按过滤器批量暂停 |
| PUT | `/api/v1/tasks/{id}/continue` | `continueTask` | 继续单任务 |
| PUT | `/api/v1/tasks/continue` | `continueTasks` | 按过滤器批量继续 |
| DELETE | `/api/v1/tasks/{id}` | `deleteTask` | 删单任务（query `force` 强制） |
| DELETE | `/api/v1/tasks` | `deleteTasks` | 按过滤器批量删除 |
| GET | `/api/v1/tasks/{id}` | `getTask` | 查单个任务详情 |
| GET | `/api/v1/tasks/{id}/status` | `getTaskStatus` | 查任务运行时状态 |
| GET | `/api/v1/tasks` | `getTasks` | 按过滤器列任务 |
| GET | `/api/v1/tasks/{id}/stats` | `getStats` | 查协议统计（peers、piece 等，u6-l3） |
| POST | `/api/v1/protocols/{protocol}/jobs/{name}/trigger` | `triggerProtocolJob` | 手动触发协议定时任务（u6-l4） |
| POST | `/api/v1/mirrors/{provider}/candidates` | `mirrorCandidates` | 查询镜像候选列表（u10-l4） |
| GET | `/api/v1/config` | `getConfig` | 读全局配置 |
| PUT | `/api/v1/config` | `putConfig` | 写全局配置（整体替换，不 Merge） |
| POST | `/api/v1/extensions` | `installExtension` | 安装扩展（git 或 dev 文件夹） |
| GET | `/api/v1/extensions` | `getExtensions` | 列出扩展 |
| GET | `/api/v1/extensions/{identity}` | `getExtension` | 查单个扩展 |
| PUT | `/api/v1/extensions/{identity}/settings` | `updateExtensionSettings` | 更新扩展设置 |
| PUT | `/api/v1/extensions/{identity}/switch` | `switchExtension` | 启用/禁用扩展 |
| DELETE | `/api/v1/extensions/{identity}` | `deleteExtension` | 删除扩展 |
| GET | `/api/v1/extensions/{identity}/update` | `updateCheckExtension` | 检查扩展更新 |
| POST | `/api/v1/extensions/{identity}/update` | `updateExtension` | 执行扩展更新 |
| POST | `/api/v1/webhook/test` | `testWebhook` | 测试 webhook 连通性（u10-l3） |

#### 4.3.3 源码精读

路由表本体：

[pkg/api/service.go:L51-L80](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/api/service.go#L51-L80)

对照上表逐行读一遍即可，注意 URL 设计里的两个小心思：单任务操作用 `{id}` 路径参数（`/tasks/{id}/pause`），而批量操作复用资源集合路径加动作段（`/tasks/pause`），两者靠段数天然区分；扩展路由的 `{identity}` 是扩展唯一标识。

单任务操作复用过滤器批量路径：

[pkg/api/service.go:L224-L237](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/api/service.go#L224-L237)

`applyTaskAction` 把「对 id 的操作」翻译成 `TaskFilter{IDs: []string{taskID}}` 再走 `applyFilterAction`。于是 Downloader 侧只需要实现过滤器版语义（组 ID 如何在过滤器里展开为成员任务，见 u4-l4），API 层零额外逻辑。

查询参数到过滤器的翻译：

[pkg/api/service.go:L450-L459](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/api/service.go#L450-L459)

`taskFilterFromQuery` 支持三种过滤维度：`id`（可重复出现）、`status`（正选）、`notStatus`（排除）。例如 `GET /api/v1/tasks?notStatus=done` 列出所有未完成任务；组 ID 也可以作为 `id` 传入，由 Downloader 展开为成员任务。

请求解码与响应构造：

[pkg/api/service.go:L443-L448](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/api/service.go#L443-L448) 与 [pkg/api/service.go:L477-L491](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/api/service.go#L477-L491)

`decodeRequest` 统一做 JSON 解码，失败返回参数错误信封；四个响应 helper 的分工见 4.3.1。信封结构与错误码定义在 [pkg/rest/model/result.go:L3-L47](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/rest/model/result.go#L3-L47)：`CodeOk=0`、`CodeError=1000`、`CodeInvalidParam=1002`、`CodeTaskNotFound=2001`。

错误码分流的实例：

[pkg/api/service.go:L301-L310](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/api/service.go#L301-L310)

`triggerProtocolJob` 对「协议不存在/任务不存在」返回真正的 HTTP 404（`errors.Is` 判别 `ErrUnSupportedProtocol` 与 `ErrProtocolJobNotFound`），其余错误返回 400。这就是「资源级错误用 HTTP 状态码、业务级错误用信封码」的分界线实例。

#### 4.3.4 代码实践

**实践目标**：亲手梳理路由表并验证一个单任务操作与批量操作走的是同一条 Downloader 路径。

**操作步骤**：

1. 打开 [pkg/api/service.go:L51-L80](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/api/service.go#L51-L80)，按「任务生命周期 / 配置 / 扩展 / 协议任务 / 镜像 / 杂项」给 29 条路由分组，产出你自己的表格（可对照 4.3.2 的示例表查漏）。
2. 对比 `pauseTask`（L208-L210）与 `pauseTasks`（L212-L214）的实现，确认前者只是后者的「单元素过滤器」特例。

**需要观察的现象**：两条 pause 路由的 handler 都只有一行，真正逻辑在 `applyFilterAction` 与 Downloader 的过滤器语义里。

**预期结果**：你梳理出的表格与 4.3.2 一致，并能指出每条路由背后的 Downloader 方法名。

#### 4.3.5 小练习与答案

**练习 1**：`GET /api/v1/tasks?status=running&id=g1` 中 `g1` 是一个任务组的 ID，会发生什么？

答案：`taskFilterFromQuery` 会把 `g1` 放进 `TaskFilter.IDs`，Downloader 的过滤逻辑会把组 ID 展开为该组的成员任务 ID（u4-l4 讲过的 `expandGroupTaskIDs`），再叠加 `status=running` 过滤，返回组内运行中的任务。

**练习 2**：为什么 `PUT /api/v1/config` 的注释说「整体替换，不 Merge」？

答案：`putConfig` 直接把解码后的完整 `DownloaderStoreConfig` 交给 `Downloader.PutConfig`（[pkg/api/service.go:L339-L348](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/api/service.go#L339-L348)）。u2-l3 讲过：`Merge` 的真实调用方是服务端首启注入启动参数，而运行期 `PutConfig` 不做 Merge，客户端必须「读-改-写」整体提交，否则未填字段会回到零值。

**练习 3**：`deleteTask` 与 `deleteTasks` 的 `force` 参数从哪里来、语义是什么？

答案：来自查询参数 `ctx.Query.Get("force") == "true"`（[pkg/api/service.go:L239-L256](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/api/service.go#L239-L256)），两个 handler 都汇入同一个 `deleteTasks` helper 并透传给 `Downloader.Delete(filter, force)`；force 表示强制删除（即使任务处于下载中等不可直接删除的状态）。

### 4.4 createTaskBatch 与 CreateTaskBatchResult：批量接口的新结构

#### 4.4.1 概念说明

本模块聚焦本轮提交（1e36599 "feat(tasks): group extension folder downloads"）对服务层的改动。过去 `POST /api/v1/tasks/batch` 的响应 `data` 是一个字符串数组（任务 ID 列表）；引入任务分组后，仅返回 ID 数组无法表达「这批任务同属哪个组」，因此响应改为结构化对象：

```go
// pkg/download/model.go
type CreateTaskBatchResult struct {
    TaskIDs  []string `json:"taskIds"`
    ParentID string   `json:"parentId,omitempty"`
}
```

`taskIds` 是创建成功的任务 ID（部分成功语义：个别成员失败不影响其余成员）；`parentId` 仅在请求带 `group=true` 时出现（`omitempty`），指向为本批任务预留目录的 TaskGroup。

与之配套，请求模型 `base.CreateTaskBatch` 新增了 `Group`/`GroupName` 字段，成员模型 `CreateTaskBatchItem` 新增了 `Rid` 字段（批量成员也可以消费 Resolve 缓存）。

#### 4.4.2 核心流程

批量接口的完整数据流：

```text
POST /api/v1/tasks/batch
  body: {"reqs":[{rid|req, opts}...], "opts":{...}, "group":bool, "groupName":"..."}

createTaskBatch (pkg/api/service.go)
  ├── decodeRequest → base.CreateTaskBatch
  ├── len(reqs)==0 → 业务错误 code=1002
  └── Downloader.CreateDirectBatchWithResult(&request)
        ├── group=true → createTaskGroup 预留组目录 → result.ParentID
        ├── 逐成员：rid 优先 → createResolved；否则 req → createDirect
        ├── 部分成功：失败进 failures，成功 ID 进 result.TaskIDs
        └── 全部失败 → error("all batch tasks failed: ...")

响应 data：
  普通批量   → {"taskIds": ["t1", "t2"]}
  分组批量   → {"taskIds": ["t1", "t2"], "parentId": "g1"}
  全部失败   → code=1000, msg="all batch tasks failed: ..."
```

兼容性处理：Go 侧旧入口 `CreateDirectBatch`（只返回 `[]string`）被保留为薄包装，内部委托 `CreateDirectBatchWithResult` 后只取 `TaskIDs`——「暴露组元数据的 API 用 WithResult，只要 ID 的旧调用方继续可用」。

#### 4.4.3 源码精读

改动后的 handler：

[pkg/api/service.go:L175-L188](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/api/service.go#L175-L188)

与旧版唯一的差别在第 183 行与第 187 行：`CreateDirectBatch` → `CreateDirectBatchWithResult`，`newOKResponse(taskIDs)` → `newOKResponse(result)`。handler 结构不变，说明这类「响应模型升级」在分层架构下只触及最外层一行。

请求模型：

[pkg/base/model.go:L202-L213](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/base/model.go#L202-L213)

`CreateTaskBatch` 的四个字段：`Reqs` 成员列表、`Opts` 批级默认选项、`Group` 是否建组、`GroupName` 组名（决定预留目录名）。`CreateTaskBatchItem` 的 `Rid` 带 `omitempty`——rid 与 req 二选一，rid 优先。

响应模型与 TaskGroup：

[pkg/download/model.go:L32-L44](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/model.go#L32-L44)

`TaskGroup` 只存目录元数据（ID、名字、路径、创建时间），不是任务；成员任务以 `Task.ParentID` 单向引用它（u4-l6 的「双轨制」）。`CreateTaskBatchResult` 是普通批量与分组批量共用的响应模型，`ParentID` 的 `omitempty` 使普通批量的 JSON 里干脆不出现该字段——测试正是靠「响应字段个数」来断言这一点。

Downloader 侧的批量入口与兼容包装：

[pkg/download/downloader.go:L654-L662](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L654-L662)

注释写明了保留旧签名的理由：只要 ID 的 Go 调用方继续用 `CreateDirectBatch`，暴露组元数据的 API 用 `CreateDirectBatchWithResult`。

[pkg/download/downloader.go:L664-L753](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L664-L753)

`CreateDirectBatchWithResult` 主体（细节归 u4-l6，这里看与 API 响应直接相关的三处）：`result.ParentID = group.ID` 在建组成功后立即填充；成员循环中 rid 优先（`case item.Rid != ""` 分支在前）；`len(result.TaskIDs) == 0` 时用 `errors.Join` 聚合所有成员错误返回「全部失败」——这解释了 4.4.2 里三种响应形态的来源。

服务层测试对响应结构的断言：

[pkg/api/service_test.go:L271-L331](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/api/service_test.go#L271-L331)

`TestBatchResolvedAndDirectCreation` 用 `serviceTestRequest` 直接 POST `/api/v1/tasks/batch`：一批里混合了无效 rid、有效 rid（带成员级 opts）与直接 req 三个成员；断言 `taskIds` 恰有 2 个（部分成功）；非分组响应只有 1 个字段、分组响应有 2 个字段且 `parentId` 非空；随后 GET 单任务验证 `task.ParentID == parentId` 且成员级 `SelectFiles` 未丢失；最后一批全失败成员验证返回业务错误码。这就是综合实践的蓝本。

#### 4.4.4 代码实践

**实践目标**：用 `Service.Dispatch`/`JSON` 直接调用 `createTaskBatch` 与 `getTasks`，验证批量响应中的 `taskIds`/`parentId` 字段，全程不启动 HTTP 服务器。

**操作步骤**：

1. 先运行仓库中现成的测试作为基线：

   ```bash
   go test ./pkg/api/ -run TestBatchResolvedAndDirectCreation -v
   ```

2. 在仓库外（或 `_examples` 风格的独立小项目中）写一个自己的验证测试。下面是**示例代码**（非项目原有代码，仿照 `service_test.go` 的写法，依赖 `github.com/GopeedLab/gopeed` 模块）：

   ```go
   package mytest

   import (
       "encoding/json"
       "net/http"
       "net/http/httptest"
       "testing"

       "github.com/GopeedLab/gopeed/pkg/api"
       "github.com/GopeedLab/gopeed/pkg/base"
       "github.com/GopeedLab/gopeed/pkg/download"
   )

   func TestMyBatchDispatch(t *testing.T) {
       // 1. 起一个本地假上游，供任务解析与直建使用
       upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
           w.Header().Set("Content-Length", "4")
           _, _ = w.Write([]byte("test"))
       }))
       t.Cleanup(upstream.Close)

       // 2. 构造 Downloader + Service：不指定 FetchManagers，走默认协议注册；
       //    MaxRunning=0 让任务停在就绪态，便于断言
       d := download.NewDownloader(&download.DownloaderConfig{
           StorageDir: t.TempDir(), TempDir: t.TempDir(),
       })
       if err := d.Setup(); err != nil {
           t.Fatal(err)
       }
       t.Cleanup(func() { d.Close() })
       cfg, _ := d.GetConfig()
       cfg.MaxRunning = 0
       if err := d.PutConfig(cfg); err != nil {
           t.Fatal(err)
       }
       service, err := api.NewService(d)
       if err != nil {
           t.Fatal(err)
       }

       // 3. 进程内调用批量创建（group=true）
       body, _ := json.Marshal(&base.CreateTaskBatch{
           Group: true, GroupName: "demo",
           Opts: &base.Options{Path: t.TempDir()},
           Reqs: []*base.CreateTaskBatchItem{
               {Req: &base.Request{URL: upstream.URL + "/a"}},
               {Req: &base.Request{URL: upstream.URL + "/b"}},
           },
       })
       status, raw := service.JSON(http.MethodPost, "/api/v1/tasks/batch", nil, body, nil)
       if status != http.StatusOK {
           t.Fatalf("status=%d body=%s", status, raw)
       }
       var created struct {
           Code int `json:"code"`
           Data struct {
               TaskIDs  []string `json:"taskIds"`
               ParentID string   `json:"parentId"`
           } `json:"data"`
       }
       _ = json.Unmarshal(raw, &created)
       if created.Code != 0 || len(created.Data.TaskIDs) != 2 || created.Data.ParentID == "" {
           t.Fatalf("unexpected batch result: %s", raw)
       }

       // 4. 进程内调用 getTasks，按组 ID 过滤验证展开
       status, raw = service.JSON(http.MethodGet,
           "/api/v1/tasks?id="+created.Data.ParentID, nil, nil, nil)
       if status != http.StatusOK {
           t.Fatalf("status=%d body=%s", status, raw)
       }
       var listed struct {
           Data []*download.Task `json:"data"`
       }
       _ = json.Unmarshal(raw, &listed)
       if len(listed.Data) != 2 {
           t.Fatalf("group filter should expand to 2 tasks, got %d", len(listed.Data))
       }
       for _, task := range listed.Data {
           if task.ParentID != created.Data.ParentID {
               t.Fatalf("task %s not in group", task.ID)
           }
       }
   }
   ```

   说明：示例刻意不引用 `internal/` 下的包（仓库测试里 `TestBatchResolvedAndDirectCreation` 用 `internal/protocol/http` 精简了协议列表，但 `internal/` 受 Go 可见性规则保护，仓库外无法引用），因此采用与 `TestResolveThenCreateWithDownloadOptions`（[pkg/api/service_test.go:L28](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/api/service_test.go#L28)）相同的「默认协议注册」写法，在仓库内外的模块里都能编译运行。

**需要观察的现象**：

- 批量响应的 `data` 是对象而非数组，分组批量多出 `parentId` 字段。
- `getTasks` 用组 ID 作为 `id` 查询参数时，返回的是组内全部成员任务（组 ID 被展开）。
- 把 `Group` 改为 `false` 再跑一次：`parentId` 字段从响应中消失（`omitempty` 生效）。

**预期结果**：测试通过；`taskIds` 长度为 2、`parentId` 非空、组过滤命中 2 个成员。（待本地验证。）

#### 4.4.5 小练习与答案

**练习 1**：为什么 `CreateTaskBatchResult.ParentID` 要加 `omitempty`，而不是在普通批量时返回空字符串？

答案：普通批量没有组的概念，返回 `"parentId": ""` 会误导客户端把它当成有效引用；`omitempty` 让字段彻底不出现在 JSON 里，客户端用「字段是否存在」即可区分两种批量形态。仓库测试也正是用响应字段个数（1 或 2）来断言这一行为（[pkg/api/service_test.go:L308-L320](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/api/service_test.go#L308-L320)）。

**练习 2**：批量请求中一个成员同时给了 `rid` 和 `req`，哪个生效？依据是哪段代码？

答案：`rid` 生效。`CreateDirectBatchWithResult` 的成员 switch 中 `case item.Rid != ""` 分支在前（[pkg/download/downloader.go:L730-L740](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L730-L740)），`item.Req` 只有在 rid 为空时才走 `createDirect`。这也是 u2-l1 讲过的「rid 优先」规则在源码中的落点。

**练习 3**：如果批量里 3 个成员全部创建失败，HTTP 状态码和响应信封分别是什么？

答案：HTTP 状态码是 200（`newErrorResponse` 不改状态码），信封为 `code=1000`（CodeError），`msg` 是 `all batch tasks failed: ` 加 `errors.Join` 聚合的各成员错误串；`data` 为空。服务层测试用「`failed.Code == restmodel.CodeOk` 为假」来断言这一形态（[pkg/api/service_test.go:L325-L329](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/api/service_test.go#L325-L329)）。

## 5. 综合实践

**任务：不启动 HTTP 服务器，走完「解析 → 分组批量创建 → 组过滤查询 → 暂停整组」的全链路。**

把 4.4.4 的示例测试扩展成一个小验收脚本（以仓库内测试的形式）：

1. `POST /api/v1/resolve` 解析假上游的 URL，拿到 `ResolveResult.ID`（rid）。
2. `POST /api/v1/tasks/batch` 提交 `group=true` 的批量：一个成员用 rid（带成员级 `SelectFiles`），一个成员用 req 直建，再混入一个 `unsupported://` 的必败成员——验证部分成功下 `taskIds` 恰有 2 个且 `parentId` 非空。
3. `GET /api/v1/tasks?id={parentId}` 验证组 ID 展开为 2 个成员，且每个成员的 `ParentID` 与 `parentId` 一致、rid 成员的 `SelectFiles` 未被批级 opts 覆盖。
4. `PUT /api/v1/tasks/pause?id={parentId}` 暂停整组，再 `GET /api/v1/tasks/{id}` 抽查一个成员状态变为 pause。
5. 全程只用 `service.Dispatch`/`service.JSON`，最后对照 `TestBatchResolvedAndDirectCreation`（[pkg/api/service_test.go:L271-L331](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/api/service_test.go#L271-L331)）与 `pkg/rest/server_test.go` 中对应的 HTTP 层测试，确认「进程内调用」与「HTTP 调用」看到的行为完全一致——这正是服务层与传输无关的价值证明。

验收标准：步骤 2 的部分成功语义、步骤 3 的成员归属与 opts 保留、步骤 4 的组批量暂停全部符合预期。运行结果以本地为准（待本地验证）。

## 6. 本讲小结

- `pkg/api` 是与传输无关的服务层：`Context`/`Response` 只含纯数据，handler 不接触网络对象，REST（`rest/server.go` 注册 `RouteSpecs`）、FFI（`rest/invoke.go` 字符串桥）、测试三方共享同一份实现，新增 API 桥接层零改动。
- `RouteSpec` 是自研路由表：构造期预解析模式段，`Match` 按段数严格相等 + 字面全等 + 参数段 `url.PathUnescape` 恰好解码一次的规则匹配，`cleanPath` 统一各种路径写法差异。
- `Dispatch` 顺序遍历路由表并带 recover 兜底（任何调用方都不会被 handler panic 打死），未命中返回真 404；业务错误默认 HTTP 200 + 信封 `code`，资源级错误才用真 HTTP 状态码。
- 29 条 `/api/v1` 路由可按「任务生命周期 / 配置 / 扩展 / 协议任务 / 镜像 / 杂项」成批读懂；单任务操作统一归一为 `TaskFilter`，批量操作从 query 的 `id`/`status`/`notStatus` 构造过滤器。
- 本轮更新后 `POST /api/v1/tasks/batch` 的响应 `data` 从 ID 数组升级为 `CreateTaskBatchResult{taskIds, parentId?}`：分组批量多出 `parentId` 指向 TaskGroup，部分成功语义不变，全败时聚合报错；Go 侧旧入口 `CreateDirectBatch` 保留为兼容薄包装。

## 7. 下一步学习建议

本讲只解决了「接口在哪里、怎么调」，接下来两条线：

- **传输层深潜**：下一讲 u8-l2（`pkg/rest`：REST 服务器、鉴权与 Web UI 托管）讲 gorilla/mux 如何把 `RouteSpecs` 包成真正的 HTTP 服务、token/cookie 双层鉴权与静态资源托管；u8-l3（原生桥接）讲 FFI 侧 `InvokeAsync` 如何经 `rest.Dispatch` 到达本讲的 `Service.JSON`。
- **事件管道**：u8-l4 讲 `NewService` 里注册的 `downloader.Listener(service.emitTaskEvent)` 如何变成 `TaskEventMask` 位掩码订阅，推送给前端。
- **批量语义的另一半**：`parentId` 背后的 TaskGroup 目录预留、组内路径包含性校验、空组清理等细节在 u4-l6，建议与本讲对照阅读；`pkg/rest/server_test.go` 的 HTTP 层批量测试与本讲 `pkg/api/service_test.go` 的进程内测试是「同一行为、两种传输」的对照样本。
