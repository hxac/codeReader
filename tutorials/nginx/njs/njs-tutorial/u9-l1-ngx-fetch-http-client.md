# ngx.fetch()：异步 HTTP 客户端

## 1. 本讲目标

本讲带你看懂 njs 在 NGINX 里如何实现一个「**符合 Web Fetch API 形状、但底层走 nginx 原生网络栈**」的异步 HTTP 客户端。学完后你应该能够：

- 说出 `ngx.fetch()` 为什么返回 `Promise`，以及这个 Promise 的结算（resolve/reject）是被谁、在什么时候触发的（关键认知：**nginx 事件循环驱动，而不是 JS 自己**）。
- 读懂 `ngx_js_http_t` 这个共享的「连接 + 响应解析」状态机：从 `resolver → connect → SSL → 读 status line → 读 headers → 读 body（含 chunked）`的完整链路。
- 列出 `js_fetch_*` 这一族指令（`js_fetch_timeout`、`js_fetch_buffer_size`、`js_fetch_proxy`、`js_fetch_keepalive`、`js_fetch_verify` 等）各自控制什么、默认值是多少，并解释为什么「用主机名而不是 IP」时必须配置 `resolver`。
- 理解「双引擎＝双份代码」铁律在 fetch 上的具体形态：`ngx_js_fetch.c`（njs 引擎）与 `ngx_qjs_fetch.c`（QuickJS）是两份 JS 包装，但二者都调用同一份引擎无关的 `ngx_js_http.c`。

## 2. 前置知识

在进入本讲前，请确认你已经理解下面几个概念（它们都来自前面几讲）：

- **Promise 与 jobs 队列**（u4-l5）：njs 的异步靠「状态机 + 作业队列 + 续体」在单线程同步 VM 上模拟。fetch 的 `then` 回调之所以不会立即执行，正是因为它的结算被投递进了 `njs_vm_enqueue_job` 的 jobs 队列，要等宿主（NGINX）回来排空。
- **NGINX 指令驱动的运行模型与 `r` 对象**（u8-l2）：JS 只能在 `js_content`/`js_access` 等指令绑定的阶段被 NGINX 调用，且 `access`/`content`/`periodic` 阶段支持「挂起—恢复」（异步），而 `js_set`/`filter` 不支持。fetch 只能在支持异步的阶段里用。
- **ngx_js 共享层与引擎抽象**（u8-l1）：`nginx/ngx_js.c` 是 HTTP 与 Stream 两个模块共用的绑定基座，`ngx_engine_t` 用函数指针把 njs 与 qjs 两引擎抽象成统一接口。
- **外部对象与原生函数**（u5-l4）：内置 njs 引擎用 `njs_external_t` 描述符 + `njs_vm_external_create` 制造宿主对象（如 `r`、`ngx`、`Headers`）。fetch 的三类对象在 njs 引擎下就是这样造出来的。

一个直觉先行：浏览器里的 `fetch` 是「JS 发请求、等网络」。NGINX 里的 `ngx.fetch()` 是「**JS 把请求参数交给 nginx 的网络栈，然后立即返回一个 Promise 挂起；nginx 的事件循环负责真正去连接、收数据；数据收齐后再回来把 Promise 结算掉**」。JS 自己不做任何阻塞 IO。

## 3. 本讲源码地图

| 文件 | 角色 | 关键内容 |
|---|---|---|
| [nginx/ngx_js_http.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_http.h) | 共享头文件 | `ngx_js_request_t`/`ngx_js_response_t`/`ngx_js_headers_t` 数据结构、`ngx_js_http_t` 连接与解析上下文、解析状态结构 `ngx_js_http_parse_t` |
| [nginx/ngx_js_http.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_http.c) | **引擎无关的 HTTP 客户端**（被两引擎共享） | resolver、connect、SSL、读写处理器、status line / header / chunked 三套解析器、keepalive 缓存池、请求行拼装 |
| [nginx/ngx_js_fetch.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_fetch.c) | **njs 引擎侧** fetch 包装 | `ngx_js_ext_fetch` 入口、Request/Response/Headers 三类外部原型、Promise + event 绑定 |
| [nginx/ngx_qjs_fetch.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_qjs_fetch.c) | **QuickJS 侧** fetch 包装 | `ngx_qjs_ext_fetch` 入口、JS 类注册，与 `ngx_js_fetch.c` 对偶 |
| [nginx/ngx_js.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.c) | 共享绑定层 | `fetch` 方法挂到 `ngx` 对象上、`js_fetch_*` 指令的合并默认值 |
| [nginx/ngx_http_js_module.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c) | HTTP 模块 | `js_fetch_*` 指令表 |
| [nginx/config](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/config) | 构建胶水 | 把 `ngx_js_http.c` + `ngx_js_fetch.c` 编进两模块，把 `ngx_qjs_fetch.c` 仅追加进 QuickJS |

最重要的一条架构事实来自构建胶水：`NJS_SRCS` 同时包含 `ngx_js_http.c` 与 `ngx_js_fetch.c`（[nginx/config:14-19](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/config#L14-L19)），而 `QJS_SRCS` 只额外追加 `ngx_qjs_fetch.c`（[nginx/config:160](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/config#L160)）。也就是说**根本不存在 `ngx_qjs_http.c`**——`ngx_js_http.c` 这份「连接与解析」逻辑是真正引擎无关、被两引擎共享的；只有「面向 JS 的对象包装」是两份。

## 4. 核心概念与源码讲解

### 4.1 Request / Response / Headers：fetch 的三件套对象

#### 4.1.1 概念说明

Web Fetch API 规范定义了三类对象：

- **`Request`**：描述一次出站请求（URL、`method`、`headers`、`body`、`cache`/`credentials`/`mode` 等模式标志）。
- **`Response`**：描述一次入站响应（`status`、`statusText`、`ok`、`headers`、`type`，以及 `text()`/`json()`/`arrayBuffer()` 三种异步读体方法）。
- **`Headers`**：一个大小写不敏感、可重复的头部容器，支持 `get`/`set`/`append`/`delete`/`has`/`forEach`。

njs 把它们实现为**宿主对象**（external objects，见 u5-l4）：JS 层看到一个普通对象，C 层用一个固定结构体 + 一张「属性处理器表」来支撑。在 njs 引擎下用 `njs_external_t` 声明表，在 QuickJS 下用 JS 类（`JSClassDef` + exotic methods）声明，两份代码各管各的对象模型，但底层的 `ngx_js_request_t`/`ngx_js_response_t`/`ngx_js_headers_t` 结构体是**共享**的。

> 关于 `fetch` 本身：它不是一个独立模块导出的函数，而是挂在 `ngx` 全局对象上的一个方法。在共享层里，`fetch` 被声明为 `ngx` 对象的一个 method（[nginx/ngx_js.c:243-252](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.c#L243-L252) 指向 `ngx_js_ext_fetch`，QuickJS 侧在 [nginx/ngx_js.c:471](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.c#L471) 用 `JS_CFUNC_DEF("fetch", 2, ngx_qjs_ext_fetch)` 对偶）。这就是你在 JS 里写 `ngx.fetch(...)` 而不是 `fetch(...)` 的原因。

#### 4.1.2 核心流程

从「JS 调用 `ngx.fetch(url, init)`」到「拿到一个 Response 对象」的骨架：

1. **构造 Promise**：进入 `ngx_js_ext_fetch`（njs）或 `ngx_qjs_ext_fetch`（qjs）后，第一件事是分配一个 `fetch` 上下文，并立即 `njs_vm_promise_create` 造一个新 Promise，把它作为返回值丢回 JS。此刻网络还没动。
2. **解析请求**：把 `url`/`init` 参数解析成一个 `ngx_js_request_t`（method、headers、body、模式标志），并校验 URL（只允许 `http://`/`https://`，禁用 `CONNECT`/`TRACE` 等方法）。
3. **交给共享层**：把请求信息塞进 `ngx_js_http_t`，调用 `ngx_js_http_resolve` 或直接 `ngx_js_http_connect` 启动网络栈，然后把控制权**交还给 nginx 事件循环**。
4. **nginx 干活**：事件循环负责 resolver→connect→SSL→读写。这一步 JS 完全不参与。
5. **结算 Promise**：数据收齐后，共享层回调 `ready_handler`（即 `ngx_js_fetch_process_done`），它造一个 Response 外部对象，再通过预存的 promise 回调把 Response 结算出去。

整条链路的精髓是：**JS 侧的 Promise 与 nginx 侧的网络状态机之间，靠一个 `ngx_js_event_t`（njs）/`ngx_qjs_event_t`（qjs）事件对象做挂钩**，二者通过 `http->ready_handler` / `http->error_handler` 这两个函数指针回连。

#### 4.1.3 源码精读

**三类对象的数据结构**（共享，[nginx/ngx_js_http.h:58-109](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_http.h#L58-L109)）：

- `ngx_js_headers_t`：一个 `ngx_list_t header_list`（nginx 原生链表头容器）+ 一个 `guard` 枚举（`GUARD_REQUEST`/`GUARD_RESPONSE`/`GUARD_IMMUTABLE`，控制能否再 `append`）+ 一个指向 `Content-Type` 节点的快指针。
- `ngx_js_request_t`：`url`、`method`、`body`、`headers`，外加 `cache_mode`/`credentials`/`mode` 三个 Fetch 模式枚举和 `body_used`（体是否已被读过，读体是一次性的）。
- `ngx_js_response_t`：`url`、`code`、`status_text`、`headers`，以及一个 `njs_chb_t chain`（njs 的链式 chunk buffer，用来拼接响应体）和 `body_used`。

注意 `header_list` 用的是 nginx 自己的 `ngx_list_t`，而**不是** njs 的对象哈希——因为 fetch 的头部操作发生在 C 层、与 JS 对象模型无关，用 nginx 原生容器更顺手，也方便直接喂给网络栈。

**njs 引擎侧的三张外部原型表**声明了 JS 可见的属性/方法：

- Headers 表 [nginx/ngx_js_fetch.c:153-250](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_fetch.c#L153-L250)：声明了 `append`/`delete`/`forEach`/`get`/`getAll`/`has`/`set`，其中 `get` 与 `getAll` 共用同一个 C 函数 `ngx_headers_js_ext_get`，靠 `magic8=1` 区分（见 u5-l4 的 magic 复用模式）。
- Request 表 [nginx/ngx_js_fetch.c:253-364](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_fetch.c#L253-L364)：`arrayBuffer()`/`json()`/`text()` 三个读体方法共用 `ngx_request_js_ext_body`，靠 `magic8` 取 `NGX_JS_BODY_ARRAY_BUFFER`/`JSON`/`TEXT`；`method`/`url` 直接用通用的 `ngx_js_ext_string` 加 `magic32=offsetof(...)` 一步取到结构体字段。
- Response 表 [nginx/ngx_js_fetch.c:367-487](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_fetch.c#L367-L487)：除了对应的 `status`/`statusText`/`ok`/`headers`/读体方法，还把 `redirected` 写死成常量 `false`（[nginx/ngx_js_fetch.c:428-437](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_fetch.c#L428-L437)）——njs 的 fetch 不跟随重定向。

**初始化注册**发生在模块 init：`ngx_js_fetch_init` 先用 `njs_vm_external_prototype` 把三张表编译成三个整数 `proto_id` 句柄，再用 `ngx_js_fetch_function_bind` 把 `Headers`/`Request`/`Response` 三个构造器挂成全局函数（[nginx/ngx_js_fetch.c:2531-2580](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_fetch.c#L2531-L2580)）。这个 init 由 `njs_module_t ngx_js_fetch_module`（[nginx/ngx_js_fetch.c:495-499](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_fetch.c#L495-L499)）在 VM 创建期调用——正是 u6-l2 讲的「`njs_module_t` 双阶段 init」机制。QuickJS 侧对偶的是 `ngx_qjs_fetch_init`，它返回一个 `JSModuleDef *`（[nginx/ngx_qjs_fetch.c:2496](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_qjs_fetch.c#L2496)），符合 `qjs_module_t` 的注册约定。

**Promise 的制造与结算**是理解「fetch 为何异步」的核心。分配上下文时（[nginx/ngx_js_fetch.c:1073-1147](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_fetch.c#L1073-L1147)）做了三件关键事：

1. `njs_vm_promise_create` 造 Promise，同时拿到 `promise_callbacks[2]`（下标 0 是 resolve、1 是 reject，这是 njs Promise 的固定布局，承接 u4-l5）。
2. 分配一个 `ngx_js_event_t`，把 VM 指针、一个 trampoline 函数、析构回调 `ngx_js_fetch_destructor` 都塞进去，再 `ngx_js_add_event` 注册到 ctx 的事件表里——这个 event 就是「网络完成后回来找 JS」的挂钩。
3. 安装三个回调指针：`http->ready_handler = ngx_js_fetch_process_done`、`http->error_handler = ngx_js_fetch_error`、`http->append_headers = ngx_js_fetch_append_headers`。它们把引擎无关的 `ngx_js_http_t` 与引擎相关的 fetch 上下文粘起来。

结算走 `ngx_js_fetch_done`（[nginx/ngx_js_fetch.c:1227-1260](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_fetch.c#L1227-L1260)）：根据 `rc` 选 `promise_callbacks[0]`（resolve）或 `[1]`（reject），把 Response/异常作为参数，调用 trampoline 触发 `then`/`catch`。trampoline 本体 [nginx/ngx_js_fetch.c:1263-1276](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_fetch.c#L1263-L1276) 极简：取出参数里的回调函数并 `njs_vm_call` 它——这正是 u4-l5 讲的 jobs 队列投递点之一。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 `Request`/`Response`/`Headers` 三类对象的形状，并验证「读体是一次性的」。

**操作步骤**（需要先按 u8-l2/u9 集成测试的方式搭一个最小 nginx，或直接复用 `nginx/t/js_fetch.t` 的测试架）：

1. 构建 NGINX + njs 模块后，写一个 `js_content` handler，里面 `new Headers()`/`new Request()`/`new Response()` 并打印：
   ```js
   // 示例代码：放在 test.js 里，由 js_content test.shapes 调用
   function shapes(r) {
       let h = new Headers({a: '1', A: '2'});   // 大小写不敏感：get 会合并
       h.append('a', '3');
       let resp = new Response('hello', {status: 201, statusText: 'Created'});
       r.return(200, JSON.stringify({
           getA: h.get('a'),        // 预期 "1, 2, 3"
           hasA: h.has('a'),        // 预期 true
           status: resp.status,     // 预期 201
           ok: resp.ok              // 预期 true（201 在 200..299）
       }));
   }
   export default {shapes};
   ```
2. 把它跑在 `js_engine qjs;` 与默认 njs 引擎下各一次，对比输出是否一致。

**需要观察的现象**：`Headers` 对大小写不敏感且 `append` 累积、`get` 用逗号合并多个值（对应源码 [nginx/ngx_js_fetch.c:1683-1707](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_fetch.c#L1683-L1707) 里把同名牌串成链表、读取时用 `", "` 拼接）；`ok` 仅在 `200..300` 为真（[nginx/ngx_js_fetch.c:2395-2412](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_fetch.c#L2395-L2412)）。

**预期结果**：`getA` 为 `"1, 2, 3"`，`hasA` 为 `true`，`status` 为 `201`，`ok` 为 `true`。若你直接读两次 `resp.text()`，第二次会抛 `body stream already read`（对应 [nginx/ngx_js_fetch.c:2300-2305](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_fetch.c#L2300-L2305) 的 `body_used` 守卫）。若没有现成 nginx 环境，可标注「待本地验证」并改为源码阅读：跟踪 `ngx_response_js_ext_body` 如何置 `body_used=1` 并通过 `ngx_js_fetch_promissified_result` 把结果包成 Promise 返回。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Request` 的 `text()`/`json()`/`arrayBuffer()` 三个方法可以共用同一个 C 函数 `ngx_request_js_ext_body`？引擎靠什么区分它们？

**参考答案**：靠 `njs_external_t.u.method.magic8` 字段。三者在原型表里分别写 `magic8 = NGX_JS_BODY_TEXT/JSON/ARRAY_BUFFER`（[nginx/ngx_js_fetch.c:264-352](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_fetch.c#L264-L352)），C 函数进入后读 `type` 参数做 `switch`（[nginx/ngx_js_fetch.c:2149-2176](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_fetch.c#L2149-L2176)）。这是 u5-l4 讲的「一个 C 函数服务多个 JS 方法」的标准手法。

**练习 2**：`Headers` 的 `guard` 字段有哪几个取值？`Response` 收完头部后 guard 会变成什么？这会影响什么操作？

**参考答案**：取值见 [nginx/ngx_js_http.h:59-64](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_http.h#L59-L64)：`GUARD_NONE/REQUEST/IMMUTABLE/RESPONSE`。响应头解析完毕（headers 末尾 `\r\n`）时，[nginx/ngx_js_http.c:916-918](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_http.c#L916-L918) 把它置为 `GUARD_IMMUTABLE`，之后任何 `append` 都会被 [nginx/ngx_js_fetch.c:1551-1554](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_fetch.c#L1551-L1554) 拒绝并抛 type error——这是 Fetch 规范「响应头不可变」的体现。

### 4.2 HTTP 状态机：resolver → connect → SSL → 读响应

#### 4.2.1 概念说明

`ngx_js_http_t`（[nginx/ngx_js_http.h:112-173](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_http.h#L112-L173)）是整个 fetch 的「连接 + 解析」上下文，**完全引擎无关**。它持有：

- 网络地址信息：`ctx`（resolver 上下文）、`addrs`/`naddrs`/`naddr`（解析出来的多个地址及当前游标）、`host`/`port`/`connect_port`、`peer`（nginx 的连接对端结构）。
- 配置（来自 `js_fetch_*` 指令）：`buffer_size`、`max_response_body_size`、`conf->timeout`。
- 协议状态：`chunked`、`keepalive`、`content_length_n`、`header_only`、`done`。
- **状态机的三根指针**：`process`（当前解析阶段函数）、`append_headers`（引擎相关，把响应头塞进 Headers）、`ready_handler`/`error_handler`（结算回调）。`process` 是这套状态机的灵魂——它是一个函数指针，会在 status line / headers / body 三个阶段被改写。

注意 SSL 相关字段都被 `#if (NGX_SSL)` 包起来（[nginx/ngx_js_http.h:137-140](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_http.h#L137-L140)），说明 HTTPS 是可选特性，取决于 nginx 是否带 SSL 模块编译。

#### 4.2.2 核心流程

一次 fetch 的网络生命周期可以画成下面的状态推进（箭头上的标注是触发条件）：

```
ngx_js_ext_fetch
   │
   ├─ host 需解析? ──yes──> ngx_js_http_resolve ──(异步)──> resolve_handler ─┐
   │                                                                  │
   └─ no (已是 IP) ───────────────────────────────────────────────────┐│
                                                                      ▼│
                                                            ngx_js_http_connect
                                                                      │
                                          ┌───────────────────────────┼──────────────────────┐
                                  (keepalive 命中)              (新连接)                  (HTTPS+代理)
                                      复用连接            ngx_event_connect_peer        CONNECT 隧道
                                                          │                            process_connect_response
                                          ┌───────────────┴───────────────┐                    │
                                       (需 SSL)                        (明文)                   │
                            ssl_init_connection ── handshake ──┘        │                     │
                                                          ┌──────────────┘                     │
                                                          ▼                                    │
                                            write_handler (发送请求行+头+体)                    │
                                                          │                                    │
                                                          ▼                                    │
                                       read_handler ──for(;;)──> http->process(http) <──────────┘
                                                          │
                              ┌───────────────────────────┼────────────────────────────┐
                              ▼                           ▼                            ▼
                   process_status_line          process_headers              process_body
                  (parse_status_line)        (parse_header_line)        (明文按 Content-Length /
                              │                  设 chunked/                  chunked 分支)
                              │                  content_length_n)                 │
                              └──────────── 数据收齐 / 对端关闭 ───────────────────┘
                                                          │
                                              ready_handler → 结算 Promise
```

几个关键设计点：

- **多地址回退**：resolver 可能返回多个 A/AAAA 记录，`naddr` 是当前尝试的下标。某地址连接失败时 `ngx_js_http_next` 会自增 `naddr` 重试下一个（[nginx/ngx_js_http.c:608-626](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_http.c#L608-L626)）。
- **process 指针的状态迁移**：`process` 一开始是 `ngx_js_http_process_status_line`；解析完状态行后函数内部把它改写成 `ngx_js_http_process_headers`（[nginx/ngx_js_http.c:809](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_http.c#L809)）；头部收完再改成 `ngx_js_http_process_body`（[nginx/ngx_js_http.c:934](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_http.c#L934)）。读处理器 `ngx_js_http_read_handler` 只管 `for(;;){ recv; http->process(http); }`（[nginx/ngx_js_http.c:732-752](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_http.c#L732-L752)），完全不关心当前在哪一阶段——典型的「策略与机制分离」。
- **响应体上限**：明文按 `Content-Length` 判定收齐；若没有 `Content-Length`（如 HTTP/1.0），就以「对端关闭连接」为结束信号，但总量受 `max_response_body_size` 限制（防内存爆掉）。chunked 编码走单独的增量解析器。

#### 4.2.3 源码精读

**resolver（共享层入口）**：`ngx_js_http_resolve`（[nginx/ngx_js_http.c:83-113](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_http.c#L83-L113)）只是 nginx `ngx_resolve_start`/`ngx_resolve_name` 的薄封装，把回调设成 `ngx_js_http_resolve_handler`。注意它返回 `NGX_NO_RESOLVER` 哨兵的特殊含义——见 4.3 节的 resolver 依赖。解析完成在 `ngx_js_http_resolve_handler`（[nginx/ngx_js_http.c:116-196](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_http.c#L116-L196)）里把所有地址拷进 `http->addrs[]`（端口统一改成 `connect_port`，HTTPS-over-proxy 时它指向代理端口），然后调 `ngx_js_http_connect`。

**connect**：`ngx_js_http_connect`（[nginx/ngx_js_http.c:257-343](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_http.c#L257-L343)）先尝试从 keepalive 池捞连接（[L276](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_http.c#L276)），捞不到再 `ngx_event_connect_peer`；接着安装读写 handler、挂上 `conf->timeout` 定时器，并根据「是否 HTTPS、是否走代理」把 `process` 指到正确的起点：

- 普通明文：`process = ngx_js_http_process_status_line`（[L337](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_http.c#L337)）。
- HTTPS（非代理）：先 `ngx_js_http_ssl_init_connection` 做 TLS 握手（[L332-335](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_http.c#L332-L335)），握手成功后 `ngx_js_http_ssl_handshake` 再把 `process` 设为 status line（[L445-446](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_http.c#L445-L446)）。
- HTTPS-over-forward-proxy：先发 `CONNECT host:port` 建隧道，`process = ngx_js_http_process_connect_response`（[L321-322](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_http.c#L321-L322)），收到 `200` 后再在隧道上做 TLS 握手（`dea33189 Fetch: added forward proxy support with HTTPS tunneling`）。

**status line 解析**：`ngx_js_http_parse_status_line`（[nginx/ngx_js_http.c:1053-1273](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_http.c#L1053-L1273)）是一个经典的手写字符级状态机（`sw_H → sw_HT → … → sw_HTTP → 版本号 → 状态码 → status_text`），逐字节 `switch(state)`。它只接受 HTTP/1.x（`http_major > 1` 直接 `NGX_ERROR`，[L1140-1142](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_http.c#L1140-L1142)），并在结束时算出 `http_version = major*1000+minor`（[L1270](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_http.c#L1270)）——这个版本号决定了 keepalive 是否可用（见 4.3）。状态行解析成功后，`process_status_line` 把 `code`/`status_text` 写进 `http->response`，并把 `process` 推进到 headers（[nginx/ngx_js_http.c:802-815](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_http.c#L802-L815)）。

**headers 解析**：`ngx_js_http_process_headers`（[nginx/ngx_js_http.c:830-937](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_http.c#L830-L937)）循环调 `ngx_js_http_parse_header_line`，每解出一对 name/value 就经 `append_headers` 回调塞进 `response.headers`。期间它还顺手「嗅探」三个对后续解析至关重要的头部：

- `Transfer-Encoding: chunked` → 置 `http->chunked = 1`（[L871-879](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_http.c#L871-L879)）。
- `Connection: close` → 关掉 `http->keepalive`（[L881-891](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_http.c#L881-L891)）。
- `Content-Length` → 算出 `content_length_n`，并校验不超过 `max_response_body_size`（[L893-911](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_http.c#L893-L911)）。

收完头部（解析器返回 `NGX_DONE`，即遇到空行）就把 `response.headers.guard` 置为 `GUARD_IMMUTABLE`（[L916-918](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_http.c#L916-L918)）。

**body 解析**：`ngx_js_http_process_body`（[nginx/ngx_js_http.c:940-1050](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_http.c#L940-L1050)）分两条路径：

- **chunked 路径**（`http->chunked` 真）：调 `ngx_js_http_parse_chunked`（[nginx/ngx_js_http.c:1496-1637](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_http.c#L1496-L1637)）按 `<hex 长度>\r\n<data>\r\n` 增量解码，把真实数据 `njs_chb_append` 进 `response.chain`，并把 `content_length_n` 更新为已累计的解码字节数。这个解析器对「数据正好落在 chunk 边界上」单独处理（`NGX_JS_HTTP_CHUNK_ON_BORDER`），避免越界读未到达的内存（[L1516-1538](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_http.c#L1516-L1538) 的注释明确点出这一点）。
- **明文路径**：按 `need = content_length_n - 已收` 计算还差多少，取 `min(need, buf 可读)` 追加；若没有 `Content-Length`，则一直收到对端关闭。

**收尾判定**：当 `http->done`（对端 FIN，由 `read_handler` 在 `recv` 返回 0 时置位）为真，或明文路径累计字节 `== content_length_n` 时，调 `ready_handler(http)`（[L969](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_http.c#L969)），即 4.1 节的 `ngx_js_fetch_process_done`——它造 Response 对象、结算 Promise。整条 fetch 至此闭环。

#### 4.2.4 代码实践

**实践目标**：精读请求行拼装函数，并定位近期一个真实 bug 修复点（`02d83583 Fetch: fix Content-Length reservation in request building`），理解为什么它是个「静默截断」级别的隐患。

**操作步骤**（纯源码阅读型实践）：

1. 打开 `ngx_js_fetch_build_request`（[nginx/ngx_js_http.c:2165-2286](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_http.c#L2165-L2286)），它是把 `ngx_js_request_t` 拼成一段 HTTP/1.1 请求文本（写进 `http->chain`）的函数。逐段阅读：方法 + 路径 + `HTTP/1.1\r\n`（[L2175-2192](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_http.c#L2175-L2192)）→ 补 `Host`/`User-Agent`（默认 `nginx-js`，[L2236-2252](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_http.c#L2236-L2252)）→ 追加用户自定义头（`ngx_js_fetch_append_request_headers` 会跳过 `Host`/`Content-Length`/`Connection`，避免重复，[L2130-2161](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_http.c#L2130-L2161)）→ `Connection` 与 `Content-Length`。
2. 重点看有请求体时的 `Content-Length` 写法（修复后版本，[nginx/ngx_js_http.c:2265-2270](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_http.c#L2265-L2270)）：
   ```c
   njs_chb_sprintf(&http->chain,
                   sizeof("Content-Length: " CRLF CRLF) - 1
                   + NGX_SIZE_T_LEN,
                   "Content-Length: %uz" CRLF CRLF, request->body.len);
   ```
3. 用 `git show 02d83583` 看修复前的写法：第二个参数（预留缓冲大小）是硬编码的 `32`。

**需要观察的现象 / 思考**：`njs_chb_sprintf` 的第二个参数是「为这次格式化预留的最大字节数」。修复前预留 `32`，而 `"%uz"`（`size_t` 的十进制）在 64 位上最长可达 20 位，加上 `"Content-Length: "`(16) + `CRLF CRLF`(4) 已经接近/超过 32——一旦请求体非常大（`size_t` 接近上限），预留区可能装不下，导致 **`Content-Length` 头被静默截断**，服务端读到错误的（甚至缺失的）长度，行为错乱却不报错。

**预期结果**：你能用自己的话讲清——修复把预留量改成「字面量长度 + `NGX_SIZE_T_LEN`（`size_t` 的最大十进制位数）」，保证任何 `size_t` 值都不会溢出预留区。这是一个典型的「缓冲预留不足」边界条件，与 u6 讲的 Buffer 边界陷阱同类。

**附加（可选动手）**：用 `./build/njs` 或集成测试跑一个带较大 body 的 POST，对照 `nginx/t/js_fetch.t` 里 `body_content_length` 用例（它发 `{headers:{'Content-Length':'100'}, body:"CONTENT-BODY"}`），观察响应码。

#### 4.2.5 小练习与答案

**练习 1**：`ngx_js_http_read_handler` 里的主循环 `for(;;){ recv; http->process(http); }`，如何做到「不关心当前是 status line 还是 body 阶段」？

**参考答案**：靠 `http->process` 这个函数指针。每个解析阶段函数（`process_status_line`/`process_headers`/`process_body`）在完成本阶段后，会自行把 `http->process` 改写成下一阶段的函数（[nginx/ngx_js_http.c:809](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_http.c#L809) 与 [L934](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_http.c#L934)）。读循环只调「当前指针所指的函数」，从而把「何时切换阶段」的策略下沉到各阶段函数里，循环本身只管「机制」（收数据 + 分派）。这是 nginx 代码里常见的策略/机制分离写法。

**练习 2**：当响应既没有 `Content-Length` 也不是 `chunked` 时（典型是 HTTP/1.0），fetch 怎么知道 body 何时结束？

**参考答案**：此时 `content_length_n` 保持初值 `-1`，明文路径里 `need = max_response_body_size - size`（[nginx/ngx_js_http.c:1010-1011](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_http.c#L1010-L1011)），即一直收到上限；真正的结束信号是对端关闭连接——`recv` 返回 0，读循环 `break` 并置 `http->done=1`（[nginx/ngx_js_http.c:767-770](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_http.c#L767-L770)），随后 `process_body` 在 `done` 分支判定 `content_length_n == -1` 即调 `ready_handler` 收尾（[nginx/ngx_js_http.c:965-971](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_http.c#L965-L971)）。

### 4.3 js_fetch_\* 指令与 resolver 依赖

#### 4.3.1 概念说明

`js_fetch_*` 是一组 NGINX 配置指令，全部挂在 `http`/`server`/`location` 三级（MAIN/SRV/LOC），用来调优 fetch 的网络行为。它们最终都被存进 `ngx_js_loc_conf_t`（共享层定义，见 u8-l1 的 `NGX_JS_COMMON_LOC_CONF` 宏），并在请求期被 fetch 上下文读取。指令分四组：

| 指令 | 类型 | 默认值 | 作用 |
|---|---|---|---|
| `js_fetch_timeout` | 时间 | 60s | 单次连接/读/写的超时 |
| `js_fetch_buffer_size` | size | 16K | 读响应的缓冲区大小 |
| `js_fetch_max_response_buffer_size` | size | 1M | 响应体最大允许字节数（防爆内存） |
| `js_fetch_verify` / `_verify_depth` / `_trusted_certificate` / `_ciphers` / `_protocols` | SSL | verify=on | HTTPS 证书校验策略（仅 `NGX_SSL` 编译时存在） |
| `js_fetch_keepalive` / `_keepalive_requests` / `_keepalive_time` / `_keepalive_timeout` | keepalive | 0(off) / 1000 / 1h / 60s | 连接复用池大小与寿命策略 |
| `js_fetch_proxy` | URL | 无 | 正向代理地址（可带 `user:pass@`） |

#### 4.3.2 核心流程

指令如何影响 fetch：

1. **配置期**：nginx 解析 `nginx.conf` 时，每条 `js_fetch_*` 经 `ngx_conf_set_*_slot` 写进 `ngx_js_loc_conf_t` 的对应字段（指令表见 [nginx/ngx_http_js_module.c:654-754](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L654-L754)）。合并阶段填默认值（[nginx/ngx_js.c:4499-4521](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.c#L4499-L4521)）。
2. **请求期**：`ngx_js_ext_fetch` 从 `external`（即 `r`/`s`）取出 loc_conf，拷贝 `buffer_size`/`max_response_body_size` 到 `http`（[nginx/ngx_js_fetch.c:550-551](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_fetch.c#L550-L551)），HTTPS 时拷 SSL 配置（[nginx/ngx_js_fetch.c:553-558](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_fetch.c#L553-L558)）。`timeout`/`resolver` 则在 connect 阶段经 `http->conf->timeout` 与 `ngx_external_resolver` 读取。
3. **运行期还能覆盖**：`fetch(url, init)` 的第二个参数 `init` 里的 `buffer_size` / `max_response_body_size` / `verify` 可以**单次请求级**覆盖配置（[nginx/ngx_js_fetch.c:560-585](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_fetch.c#L560-L585)）。

#### 4.3.3 源码精读

**resolver 依赖——最容易踩的坑**：当 URL 用的是主机名（如 `http://example.com/`）而不是 IP 时，fetch 必须把名字解析成地址。`ngx_js_ext_fetch` 在 [nginx/ngx_js_fetch.c:638-659](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_fetch.c#L638-L659) 调 `ngx_js_http_resolve`，并特别处理两种失败：

```c
ctx = ngx_js_http_resolve(http, ngx_external_resolver(vm, external),
                          resolve_host, ngx_external_resolver_timeout(vm, external));
...
if (ctx == NGX_NO_RESOLVER) {
    njs_vm_internal_error(vm, "no resolver defined");
    goto fail;
}
```

`NGX_NO_RESOLVER` 是 nginx resolver 子系统的特殊返回值——表示「这个 server/location 块里压根没配 `resolver` 指令」。所以**用主机名 fetch 的前提是：所在 server/location 必须配 `resolver 8.8.8.8;`（或类似）**，否则会拿到 `no resolver defined` 错误。用 IP 地址则不需要（4.1.2 里 `u.no_resolve=1` 的 URL 解析只切分串、不解析名字，[nginx/ngx_js_fetch.c:1366-1367](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_fetch.c#L1366-L1367)）——这就是为什么 `nginx/t/js_fetch.t` 全用 `127.0.0.1`，它的测试 nginx 不需要 resolver。

**keepalive 池**：`js_fetch_keepalive N` 开启后，`ngx_js_fetch_alloc` 里 `http->keepalive = (conf->fetch_keepalive > 0 && !动态代理)`（[nginx/ngx_js_fetch.c:1102-1103](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_fetch.c#L1102-L1103)）。连接复用走两个函数：取连接 `ngx_js_http_get_keepalive_connection`（[nginx/ngx_js_http.c:1813-1899](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_http.c#L1813-L1899)，按 host+port+ssl 匹配、并 `MSG_PEEK` 探活），归还连接 `ngx_js_http_free_keepalive_connection`（[nginx/ngx_js_http.c:1928-2043](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_http.c#L1928-L2043)，按请求数/存活时间/超时淘汰，LRU 驱逐）。注意两个互斥条件：动态代理（`js_fetch_proxy` 传变量）会强制关闭 keepalive（`12fb6ba6`），关掉 TLS 校验也会关（`ebcc9dee`，[nginx/ngx_js_fetch.c:587-591](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_fetch.c#L587-L591)）。

**proxy（正向代理）**：`js_fetch_proxy http://user:pass@host:port` 把所有请求改走代理。`ngx_js_conf_proxy(conf)` 宏判定是否配置了代理（[nginx/ngx_js.h:169-171](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.h#L169-L171)）。HTTPS-over-代理时走 CONNECT 隧道（见 4.2.3），明文 HTTP-over-代理时直接把绝对 URL 写进请求行（`is_proxy` 分支，[nginx/ngx_js_http.c:2178-2185](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_http.c#L2178-L2185)）。代理凭证被 `ngx_js_http_fetch_build_request` 跳过不外泄给上游（[nginx/ngx_js_http.c:2150-2155](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_http.c#L2150-L2155)）。

**安全校验**：fetch 在构造请求时还会拒掉一批「不安全」输入——禁用方法 `CONNECT`/`TRACE`/`TRACK`（[nginx/ngx_js_fetch.c:883-917](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_fetch.c#L883-L917)）、校验 URL 路径不含控制字符（[nginx/ngx_js_fetch.c:630-633](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_fetch.c#L630-L633)）、校验头部名/值的合法性（`50f8b9ae`/`317fe7ff`/`5ee6e076` 一系列 reject 提交）。

#### 4.3.4 代码实践

**实践目标**：亲手验证「主机名 fetch 必须配 resolver」，并观察 keepalive 复用。

**操作步骤**：

1. 准备一个最小 `nginx.conf`（基于 `nginx/t/js_fetch_keepalive.t` 的骨架，**待本地验证**——需要编译好带 njs 的 nginx）：
   ```nginx
   # 示例配置
   http {
       js_import test.js;
       resolver 127.0.0.1;            # 关键：用主机名时必须配

       server {
           listen 127.0.0.1:8080;
           location /by_ip {
               js_content test.by_ip;   # fetch('http://127.0.0.1:8081/') 不需 resolver
           }
           location /by_name {
               js_content test.by_name; # fetch('http://localhost:8081/') 需要 resolver
           }
           location /ka {
               js_fetch_keepalive 4;    # 开 4 个连接的复用池
               js_content test.ka;
           }
       }
       server { listen 127.0.0.1:8081; return 200 "ok"; }
   }
   ```
   ```js
   // 示例代码 test.js
   async function by_ip(r)  { let rep = await ngx.fetch('http://127.0.0.1:8081/'); r.return(rep.status); }
   async function by_name(r){ let rep = await ngx.fetch('http://localhost:8081/'); r.return(rep.status); }
   async function ka(r){ for (let i=0;i<5;i++){ await ngx.fetch('http://127.0.0.1:8081/'); } r.return(200,'done'); }
   export default {by_ip, by_name, ka};
   ```
2. 先**注释掉** `resolver` 那行，请求 `/by_name`，再看 `/by_ip`。

**需要观察的现象**：注释掉 resolver 时，`/by_name` 应返回 500 且 `error.log` 里有 `no resolver defined`（对应 [nginx/ngx_js_fetch.c:651-654](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_fetch.c#L651-L654)）；而 `/by_ip` 因为是 IP，不需要 resolver，正常返回 200。加上 resolver 后 `/by_name` 恢复正常。开 `error_log` 调试级别后请求 `/ka`，可看到 `js http keepalive using cached connection` 日志（[nginx/ngx_js_http.c:1879-1881](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_http.c#L1879-L1881)），证明连接被复用。

**预期结果**：上述三类行为按描述复现。若没有 nginx 编译环境，可改为纯阅读型实践——跟踪 `ngx_external_resolver` 宏（[nginx/ngx_js.h:330-331](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.h#L330-L331)）如何从 `r` 取到所在 server 的 resolver，并解释为何 IP 路径走不到 `ngx_js_http_resolve`（[nginx/ngx_js_fetch.c:626-628](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_fetch.c#L626-L628) 的 `u.addrs == NULL` 判定）。

#### 4.3.5 小练习与答案

**练习 1**：`js_fetch_keepalive` 默认是 0（关闭）。给出两个会**强制关闭** keepalive 的条件，并说明各自的源码位置。

**参考答案**：(1) 使用了动态代理（`js_fetch_proxy` 的值是变量，运行期求值），见 [nginx/ngx_js_fetch.c:1102-1103](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_fetch.c#L1102-L1103) 的 `!ngx_js_conf_dynamic_proxy(conf)`；(2) HTTPS 且关掉了证书校验（`verify off`），见 [nginx/ngx_js_fetch.c:587-591](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_fetch.c#L587-L591)。此外响应若是 HTTP/1.0 或带 `Connection: close` 头，也会在运行期被关掉（[nginx/ngx_js_http.c:811-813](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_http.c#L811-L813) 与 [L881-891](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_http.c#L881-L891)）。

**练习 2**：`fetch(url, {buffer_size: 4096})` 这种「请求级覆盖」是如何实现的？它覆盖的是配置里的哪个字段？

**参考答案**：在 `ngx_js_ext_fetch` 里，先从 loc_conf 把 `buffer_size`/`max_response_body_size` 拷进 `http`（[nginx/ngx_js_fetch.c:550-551](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_fetch.c#L550-L551)），然后若 `init` 对象里有 `buffer_size`/`max_response_body_size`/`verify` 键，就用 `ngx_js_integer`/`njs_value_bool` 覆盖 `http->buffer_size` 等（[nginx/ngx_js_fetch.c:562-585](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_fetch.c#L562-L585)）。注意这只改本次 fetch 的 `http` 上下文，不回写 loc_conf，所以只影响这一次请求。

## 5. 综合实践

把本讲三块知识串起来：实现一个「**用 fetch 聚合两个后端、按 hostname 解析、带连接复用**」的 `js_content` handler，并解释每一步对应的源码机制。

任务（**待本地验证**，需带 njs 的 nginx + 可达的两个后端 + 一个 DNS）：

1. 写一段 JS（示例代码）：
   ```js
   async function aggregate(r) {
       // 并发拉两个后端；用主机名，因此 location 必须配 resolver
       let [a, b] = await Promise.all([
           ngx.fetch(`http://backend.local:${r.args.p}/a`),
           ngx.fetch(`http://127.0.0.1:${r.args.p}/b`),  // IP，不需 resolver
       ]);
       let ja = await a.json();
       let jb = await b.text();
       r.return(200, JSON.stringify({a: ja, b: jb, ok: a.ok && b.ok}));
   }
   export default {aggregate};
   ```
2. 在 `nginx.conf` 里配 `resolver`、`js_fetch_keepalive 4`、`js_fetch_timeout 10s`，把 handler 绑到某 location。
3. 发起请求，对照源码解释三件事：
   - 为什么两次 `fetch` 看似并发，却不会让 JS 卡住？（答：每次 fetch 立即返回 Promise 挂起，nginx 事件循环并发推进两条 `ngx_js_http_t` 状态机；JS 靠 `await` 续体在 job 中恢复——回看 u4-l5 的 jobs 队列与本讲 4.1.3 的 `ngx_js_fetch_done` trampoline。）
   - 为什么 `backend.local` 那条必须配 resolver 而 `127.0.0.1` 那条不用？（答：4.3.3 的 `NGX_NO_RESOLVER` 分支。）
   - 第二次请求同名后端时，为什么连接可能被复用？（答：4.3.3 的 keepalive 池按 host+port+ssl 匹配。）
4. 故意把 `resolver` 注释掉重跑，确认 `backend.local` 那条 fetch 被 reject、`Promise.all` 抛错、`.catch`/`try-catch` 捕获到 `no resolver defined`。

完成这个任务意味着你已经能把「JS 层 Promise → 事件挂钩 → 共享 HTTP 状态机 → 配置指令」整条链路对上源码。

## 6. 本讲小结

- `ngx.fetch()` 是「**Web Fetch API 形状 + nginx 原生网络栈**」的结合：JS 立即拿到一个 Promise 挂起，真正的连接/收发由 nginx 事件循环驱动，收齐后再经 jobs 队列结算 Promise。
- **双引擎＝双份代码**在 fetch 上体现为：`ngx_js_fetch.c`（njs）与 `ngx_qjs_fetch.c`（qjs）是两份 JS 包装，但二者共享同一份引擎无关的 `ngx_js_http.c`（连接 + 解析状态机）——构建胶水里没有 `ngx_qjs_http.c` 就是铁证。
- 三类对象 `Request`/`Response`/`Headers` 是宿主对象，njs 引擎用 `njs_external_t` 表 + `proto_id`，QuickJS 用 JS 类；共享的 `ngx_js_request_t`/`response_t`/`headers_t` 用 nginx 原生 `ngx_list_t` 存头部。
- HTTP 解析是**函数指针状态机**：`http->process` 在 status line → headers → body 三阶段间被各阶段函数自行改写，读循环只做「收数据 + 调 process」，机制与策略分离。
- 响应体有明文（按 `Content-Length` 或对端关闭）与 chunked（增量解码、边界单独处理）两条路径，总量受 `js_fetch_max_response_buffer_size` 限制。
- `js_fetch_*` 指令存在 loc_conf、默认值在合并期填入（timeout 60s / buffer 16K / body 1M / keepalive 关）；**用主机名 fetch 必须配 `resolver`**，否则报 `no resolver defined`；keepalive 受动态代理与 TLS 校验关闭双重抑制。

## 7. 下一步学习建议

- **u9-l2 ngx.shared**：fetch 是「出站」网络，`ngx.shared` 是「跨 worker 共享状态」。把它们组合可以做「fetch 拉数据 + shared 缓存」的模式，建议接着读 `nginx/ngx_js_shared_dict.c`。
- **u9-l3 subrequest / 表单 / 请求体**：fetch 是「对外的客户端」，`r.subrequest()` 是「对 nginx 内部的子请求」，二者常被对比；`ngx_js_form.c` 的表单解析与本讲 `ngx_js_http_parse_header_line` 同属「手写字符级状态机」家族，值得对照阅读。
- **深入 QuickJS 包装**：本讲的 qjs 侧只点到 `ngx_qjs_ext_fetch`（[nginx/ngx_qjs_fetch.c:228](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_qjs_fetch.c#L228)），想完整理解 `JSValue` 生命周期管理（finalizer、引用计数）可通读 `nginx/ngx_qjs_fetch.c`，并与 njs 侧的 event 析构 `ngx_js_fetch_destructor`（[nginx/ngx_js_fetch.c:1165-1179](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_fetch.c#L1165-L1179)）对比。
- **回溯基础**：若对状态机的「函数指针分派」或「Promise/jobs」还觉得模糊，可回看 u4-l1（解释器主循环）、u4-l5（Promise 与 jobs）、u8-l1（共享层与引擎抽象）。
