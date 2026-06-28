# subrequest、表单解析与请求体读取

## 1. 本讲目标

本讲聚焦 HTTP 请求处理中三类「读输入」的高级能力，它们都建立在「JS 由 NGINX 驱动、可在多个 phase 挂起恢复」这一模型之上（见 u8-l2、u9-l1）。读完本讲你应该能够：

- 说清 `r.subrequest()` 如何发起一个内部子请求、它返回 Promise 还是 `undefined`、子请求的响应如何回传给 JS。
- 描述 `readRequestText` / `readRequestArrayBuffer` / `readRequestJSON` / `readRequestForm` 这一组异步读取背后的 `body_read_state` 状态机，理解「同步完成」与「异步挂起」两条路径，以及为什么只有部分 phase 支持 async。
- 读懂 `nginx/ngx_js_form.c` 对 `application/x-www-form-urlencoded` 与 `multipart/form-data` 的解析逻辑，知道各类边界常量（如 `NGX_JS_FORM_MAX_PART_HEADERS`）的用途，以及文件上传字段在 JS 侧的呈现方式。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**第一，NGINX 的「请求体」并不自动出现在 JS 里。** 默认情况下，NGINX 只有在配置了 `client_body_in_*`、代理、或显式调用读取函数时才会去读请求体。对 njs 而言，请求体是「按需读取」的资源：`r.requestText`、`readRequestText()` 等访问触发读取，读到的字节会被缓存到请求上下文里，后续访问直接复用。

**第二，njs 的异步 = 把回调推迟到 NGINX 事件循环的某个未来时刻。** 与 u4-l5 讲过的 Promise/jobs 队列、u9-l1 讲过的 fetch 一样，这里的 `readRequest*()` 也返回 Promise；真正的网络读取由 NGINX 事件循环驱动，收齐后再通过 jobs 队列把结果投递回 JS（见 `ngx_http_js_body_resolve`）。

**第三，子请求（subrequest）是 NGINX 内部的概念。** 它不是发起新的 TCP 连接，而是让 NGINX 在处理当前请求的过程中「内部派发」一个虚拟子请求去命中另一个 location，子请求的响应被收集到内存里供父请求的 JS 读取。`ngx.fetch()`（u9-l1）是真正的对外 HTTP 客户端，而 `r.subrequest()` 是对内的、零网络开销的派发，二者用途不同。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `nginx/ngx_http_js_module.c` | HTTP 模块主体。本讲涉及 `r.subrequest()`、四个 `readRequest*()` 方法、`body_read_state` 状态机、请求体收集与缓存、`RequestForm` 外部对象原型。 |
| `nginx/ngx_js_form.c` | 引擎无关的表单解析器，负责把请求体字节流解析成 `ngx_js_form_t` 条目数组，支持 urlencoded 与 multipart 两种格式。 |
| `nginx/ngx_js_form.h` | 表单解析器的公共头：`ngx_js_form_entry_t` / `ngx_js_form_t` 结构与 `ngx_js_parse_form()` 入口原型、默认键上限 `NGX_JS_FORM_DEFAULT_MAX_KEYS`。 |
| `nginx/t/js_request_form.t` | `readRequestForm()` 的官方集成测试，包含 urlencoded / multipart / 文件上传 / 缓存 / 限流等用例，是本讲代码实践的蓝本。 |
| `nginx/t/js_subrequests.t` | `r.subrequest()` 的官方集成测试，覆盖 Promise/回调/detached/方法/请求体等用法。 |

## 4. 核心概念与源码讲解

### 4.1 subrequest：发起内部子请求

#### 4.1.1 概念说明

`r.subrequest(uri, args, callback)` 让 JS 在处理当前请求时，向 NGINX 内部派发一个子请求去命中 `uri` 对应的 location。它的典型用途是「在 JS 里聚合多个内部上游的结果」——例如先 subrequest 一个鉴权 location，再根据返回状态决定如何响应，全程不产生额外网络连接。

调用形态有三种，由第二个参数的类型决定：

- `r.subrequest('/auth')` —— 不传回调，**返回 Promise**，resolve 值是子请求的 reply 对象。
- `r.subrequest('/auth', 'h=xxx', reply => { ... })` —— 第二参数是 query string、第三参数是回调，**返回 `undefined`**，回调在子请求完成时被调用。
- `r.subrequest('/auth', { method:'POST', body:'["x"]', args:'k=1' }, cb)` —— 第二参数是选项对象，可指定 `args` / `method` / `body` / `detached`。

reply 对象本质上就是子请求自己的请求对象 `r`（与父请求共享同一个外部原型 `ngx_http_js_request_proto_id`），因此可以用 `reply.status`、`reply.headersOut`、`reply.responseText` 等读取子请求的响应。

#### 4.1.2 核心流程

`r.subrequest()` 的执行可以概括为：

1. **校验**：当前请求不能已经是一个「内存中子请求」（`r->subrequest_in_memory`），否则报错——子请求不能再嵌套 in-memory 子请求。
2. **解析参数**：从 `uri`、`args|options|callback` 中取出 uri、query string、method、body、detached 标志、回调。
3. **建立事件与回调容器**：
   - 若未提供回调且非 detached，创建一个 Promise，把它的 resolve/reject 函数存进一个 `ngx_js_event_t`；回调就是这个 resolve。
   - 若提供了回调，把回调存进 event。
   - 若 `detached`，则既不要 Promise 也不要回调（「射后不理」）。
4. **调用 NGINX 原生 `ngx_http_subrequest()`**，带上 `NGX_HTTP_SUBREQUEST_BACKGROUND`（后台子请求，不计入主请求完成判定）和 `NGX_HTTP_SUBREQUEST_IN_MEMORY`（响应收进内存缓冲而非发送给客户端），并注册完成回调 `ngx_http_js_subrequest_done`。
5. **设置子请求的 method / body**：覆盖 `sr->method`、必要时构造 `sr->request_body`。
6. **完成时**：`ngx_http_js_subrequest_done` 被调用，它创建子请求的 reply 外部对象，调用回调（或 resolve Promise），再 `ngx_http_post_request(r->parent)` 把父请求重新投递回事件循环继续处理。

```text
JS: r.subrequest('/auth') ──► 创建 Promise + event
                              │
                              ├── ngx_http_subrequest(parent, uri, ..., &sr, ps, IN_MEMORY|BACKGROUND)
                              │        └── NGINX 内部跑完 /auth 的 location
                              │
   /auth 处理完毕 ──► ngx_http_js_subrequest_done(sr, event, rc)
                              │   ├── 构造 reply = 外部对象(sr)
                              │   ├── 调用 event->function (resolve/reject/用户回调)
                              │   └── ngx_http_post_request(parent)  // 父请求继续
```

#### 4.1.3 源码精读

入口函数先做校验：必须是主请求、uri 非空，并解析第二个参数（字符串=查询串、函数=回调、对象=选项）—— [nginx/ngx_http_js_module.c:4757-4801](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L4757-L4801)。下面这段是选项解析与 detached/回调互斥校验：

[nginx/ngx_http_js_module.c:4803-4870](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L4803-L4870) — 这段代码读出 `options.args` / `options.detached` / `options.method` / `options.body`，并强制 `detached` 与 `callback` 互斥（射后不理的子请求没有地方放回调）。

接着是事件与 Promise 的创建，以及调用 `ngx_http_subrequest` 的关键片段：

```c
promise = !!(callback == NULL);
event = njs_mp_zalloc(...);              // 没回调才需要 Promise 的两个槽
...
if (promise) {
    event->args = (njs_opaque_value_t *) &event[1];
    rc = njs_vm_promise_create(vm, retval, njs_value_arg(event->args));
    ...
    callback = njs_value_arg(event->args);  // 把 resolve 当作回调
}
njs_value_assign(&event->function, callback);
ps->handler = ngx_http_js_subrequest_done;
ps->data = event;
flags |= NGX_HTTP_SUBREQUEST_IN_MEMORY;
```
— 见 [nginx/ngx_http_js_module.c:4883-4915](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L4883-L4915)。注意 `promise = !!(callback == NULL)`：**有没有回调决定返回值类型**，有回调返回 `undefined`、无回调返回 Promise。`NGX_HTTP_SUBREQUEST_IN_MEMORY` 保证子请求响应被收进 `sr->out` 内存链，供后续读取。

真正发起子请求并设置 method/body 的是这段：

[nginx/ngx_http_js_module.c:4923-4975](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L4923-L4975) — 调用 `ngx_http_subrequest(r, &uri, &rargs, &sr, ps, flags)`；若 `has_body` 则手工构造 `sr->request_body`（一个内存 buf 链）并设 `sr->headers_in.content_length_n`，使子请求带上 JS 指定的请求体。

完成回调 `ngx_http_js_subrequest_done` 负责把结果送回 JS：

[nginx/ngx_http_js_module.c:4987-5059](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L4987-L5059) — 关键三步：先用 `njs_vm_external_create(..., ngx_http_js_request_proto_id, r, 0)` 把子请求对象包成 reply 外部对象（这就是为什么 reply 与父 `r` 共享同一原型、能用 `.status`/`.headersOut`）；再用 `ngx_js_call(vm, event->function, &reply, 1)` 调用回调或 resolve Promise；最后用 `ngx_http_post_request(r->parent)` 唤醒父请求。注释特意说明为何**不**用 `ngx_http_js_event_finalize()`：后者会调 `ngx_http_run_posted_requests()`，而从 finalize 路径回调进来时存在重入风险。

#### 4.1.4 代码实践

**实践目标**：用 Promise 形态的 subrequest 聚合一个内部 location 的响应头，体会「reply 是子请求对象」。

**操作步骤**（参考 `nginx/t/js_subrequests.t`）：

1. 在 `nginx.conf` 里配两个 location：一个对外 `js_content`，一个内部 `/p/sub1` 返回固定头。
2. 写 `test.js`，在 content handler 里发起 subrequest 并读取 reply。

```nginx
# nginx.conf 片段（示例配置，非仓库文件）
http {
    js_import test.js;

    server {
        listen 127.0.0.1:8080;

        location /p/sub1 {
            add_header X-Sub hello;     # 内部子请求目标
            return 200 "ok";
        }

        location /main {
            js_content test.main;
        }
    }
}
```

```js
// test.js（示例代码，参考 nginx/t/js_subrequests.t 第 292-295 行）
async function main(r) {
    const reply = await r.subrequest('/p/sub1', 'h=xxx');
    r.return(200, JSON.stringify({ status: reply.status, h: reply.headersOut.H })); // 'h' header
}

export default { main };
```

**需要观察的现象**：

- 访问 `/main` 应返回子请求的 `status` 与 `X-Sub` 头；`X-Sub` 在 `headersOut` 里按小写键名访问。
- 把 `await r.subrequest(...)` 改成回调形式 `r.subrequest('/p/sub1', 'h=xxx', reply => r.return(...))`，观察返回值变成 `undefined` 但行为等价。
- 把 `js_content` 换成 `js_header_filter` 再调 `r.subrequest()`，应当报错——子请求只在支持挂起的 phase（content/access/periodic）可用。

**预期结果**：`/main` 返回 `{"status":200,"h":"hello"}`（具体头名大小写取决于 NGINX 对 `headersOut` 的规范化）。

> 待本地验证：本实践依赖一个已编译并加载 `ngx_http_js_module` 的 NGINX（构建方式见 u1-l3 / u8-l2）。若仅用 `build/njs` CLI，则没有 `r` 对象，无法运行。

#### 4.1.5 小练习与答案

**练习 1**：`r.subrequest(uri)` 与 `r.subrequest(uri, cb)` 的返回值分别是什么？为什么？

答案：前者返回 Promise（因为没有回调，需要 Promise 把结果传给 `await`/`.then`）；后者返回 `undefined`，因为结果会直接传给回调 `cb`。判定依据是源码里的 `promise = !!(callback == NULL)`（[nginx/ngx_http_js_module.c:4883](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L4883)）。

**练习 2**：`{detached:true}` 与回调为什么互斥？

答案：detached 子请求是「射后不理」的，不收集响应、不触发任何 JS 回调；既然没有地方安放回调，源码就在 [nginx/ngx_http_js_module.c:4866-4870](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L4866-L4870) 直接拒绝两者同时出现。

---

### 4.2 请求体异步读取：readRequest* 与 body_read_state

#### 4.2.1 概念说明

`r` 对象上有两类读取请求体的入口：

- **同步属性**：`r.requestText`、`r.requestBuffer`（属性 getter，见 `ngx_http_js_ext_get_request_body`）。它们要求请求体**已经被 NGINX 读好**（`r->request_body` 非空），否则返回 `undefined`。
- **异步方法**：`r.readRequestText()`、`r.readRequestArrayBuffer()`、`r.readRequestJSON()`、`r.readRequestForm(options)`。它们返回 Promise，会按需触发 NGINX 读取请求体，读完再 resolve。

四个异步方法里前三个共用同一个 C 实现 `ngx_http_js_ext_read_request_body`，靠 `magic8` 区分输出类型（`NGX_JS_BODY_TEXT` / `NGX_JS_BODY_ARRAY_BUFFER` / `NGX_JS_BODY_JSON`，见 [nginx/ngx_http_js_module.c:1146-1191](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L1146-L1191)）；表单解析走单独的 `ngx_http_js_ext_read_request_form`。

核心难点在于：**JS 原生函数不能直接调用 `ngx_http_read_client_request_body()` 并正确返回给 phase 引擎。** 因为读取可能同步完成、也可能异步挂起，而 phase 处理函数该返回 `NGX_AGAIN`（「我还持有请求」）还是 `NGX_DONE`（「我自己终结了」）取决于这一点——只有调用返回后才知道。于是 njs 用一个状态机把「JS 请求读体」和「真正发起读」拆成两步。

#### 4.2.2 核心流程

状态机定义在请求上下文 `ngx_http_js_ctx_t` 的 `body_read_state` 字段（3 个位），核心状态如下（注释见 [nginx/ngx_http_js_module.c:82-122](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L82-L122)）：

```text
IDLE          未请求读体（初始与终态）
DEFERRED      JS 已调 readRequest*()；access 处理函数将在 call() 返回后发起读取
IN_PROGRESS   读取返回 NGX_AGAIN，请求所有权已通过 NGX_DONE 移交给读体器
FORM          附加位：本次读是为了表单解析（readRequestForm）
```

合法迁移（注释原文）：

```text
IDLE -> DEFERRED -> IDLE                  （同步完成或出错）
IDLE -> DEFERRED -> IN_PROGRESS -> IDLE   （异步完成）
```

两条路径的差别在于 `ngx_http_read_client_request_body()` 的返回值：

- **同步路径**：`r->request_body` 在 JS 调用时已就绪（例如请求体已被读、或读取同步完成），直接收集并 resolve，状态回到 IDLE。
- **异步路径**：JS 调用时置 DEFERRED；phase 处理函数（目前是 access handler）在 `engine->call()` 返回后检查到 DEFERRED，调用 `ngx_http_read_client_request_body`；若返回 `NGX_AGAIN`，状态转 IN_PROGRESS 并 `ngx_http_finalize_request(NGX_DONE)` 把所有权交给读体器；读取完成时 `ngx_http_js_access_body_done` 回调收集字节、resolve Promise、状态回 IDLE。

> **phase 支持差异**：异步读体只在 **access / content / periodic** 这些支持挂起恢复的 phase 可用。`js_header_filter`、`js_body_filter`、`js_set` 不支持异步（见 u8-l2），在这些 phase 里调 `readRequest*()` 会失败。access handler 是专门为 DEFERRED 路径写了 `ngx_http_read_client_request_body` 调用代码的唯一位置（[nginx/ngx_http_js_module.c:1579-1608](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L1579-L1608)）。

读取到的请求体被**缓存**在上下文里（`body_read_data` / `body_read_len`），同一个请求内重复调用 `readRequest*()` 不会重复读 IO；表单解析结果 `ngx_js_form_t` 也单独缓存在 `ctx->request_form`，二次调用直接复用。

#### 4.2.3 源码精读

状态机宏与字段定义：

[nginx/ngx_http_js_module.c:109-123](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L109-L123) — 注意 `body_read_phase(state) = state & 3` 取低两位做主状态，`FORM` 是独立的第 3 位标志，`to_in_progress` 在升级到 IN_PROGRESS 时保留 FORM 位。缓存的请求体字段 `body_read_data` / `body_read_len` / `body_read_event` / `request_form` 紧随其后（[nginx/ngx_http_js_module.c:125-137](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L125-L137)）。

异步方法入口 `ngx_http_js_ext_read_request_body` 的关键分支：

```c
if (r->request_body) {        // 请求体已就绪 → 同步 resolve
    goto resolve;
}
...
rc = njs_vm_promise_create(vm, retval, njs_value_arg(event->args));  // 建 Promise
njs_value_assign(&event->function, njs_value_arg(event->args));
ngx_js_add_event(ctx, event);
ctx->body_read_event = event;
ctx->body_read_state = NGX_HTTP_JS_BODY_READ_DEFERRED;   // 置 DEFERRED，等 phase handler 发起读取
return NJS_OK;
```
— 见 [nginx/ngx_http_js_module.c:3881-3947](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L3881-L3947)。注意 `resolve:` 标签处若已有请求体，调用 `ngx_http_js_collect_body` 后用 `ngx_http_js_body_to_value` 按 magic 类型转换输出。

`readRequestForm` 走几乎相同的结构，差别是状态带上 FORM 位、event 的 `data` 存的是 `maxKeys`（[nginx/ngx_http_js_module.c:3991-4061](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L3991-L4061)）：

```c
ctx->body_read_state = NGX_HTTP_JS_BODY_READ_DEFERRED | NGX_HTTP_JS_BODY_READ_FORM;
```

access handler 在 `engine->call()` 返回后处理 DEFERRED：

[nginx/ngx_http_js_module.c:1579-1608](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L1579-L1608) — 调 `ngx_http_read_client_request_body(r, ngx_http_js_access_body_done)`；返回 `NGX_OK`（同步完成）则状态直接回 IDLE 并 `goto done`；返回 `NGX_AGAIN` 则状态升级为 IN_PROGRESS、`ngx_http_finalize_request(r, NGX_DONE)` 把请求交给读体器。

读体完成回调 `ngx_http_js_access_body_done`：

[nginx/ngx_http_js_module.c:3836-3878](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L3836-L3878) — 调 `ngx_http_js_collect_body` 收集字节，再经 `ngx_http_js_body_resolve`（表单则经 `form_to_value`）resolve/reject Promise，最后 `ngx_http_js_access_body_finalize` 根据 phase 状态决定恢复 phase 引擎。

请求体收集函数 `ngx_http_js_collect_body` 处理三种存放形态：

[nginx/ngx_http_js_module.c:3546-3626](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L3546-L3626) — 单 buf 直接引用其 `pos..last`；多 buf 链拼接成连续内存；落临时文件的大体则 `ngx_read_file` 读回。结果统一写入 `ctx->body_read_data/len`，并保证可空终止（`body_read_nul`）。

> **缓存复用**：`collect_body` 开头 `if (ctx->body_read_data != NULL) return NGX_OK;`（[nginx/ngx_http_js_module.c:3555-3557](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L3555-L3557)），表单则在 `ngx_http_js_request_form` 里 `if (ctx->request_form != NULL) return;`（[nginx/ngx_http_js_module.c:3688-3691](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L3688-L3691)）。官方测试 `content_text_then_form`（先 `readRequestText` 再 `readRequestForm`）正是验证这两个缓存层协作。

#### 4.2.4 代码实践

**实践目标**：观察同步 vs 异步两条路径，并验证请求体缓存。

**操作步骤**（基于 `nginx/t/js_request_form.t` 的 `content_text_then_form` 用例）：

1. 配置一个 `js_content` location。
2. 写 handler 先读 Text、再读 Form，二者应复用同一份缓存的请求体字节：

```js
// 示例代码，改写自 nginx/t/js_request_form.t 第 188-193 行
async function content_text_then_form(r) {
    const text = await r.readRequestText();          // 触发读体，缓存到 ctx
    const form = await r.readRequestForm({maxKeys: 8}); // 复用缓存，不重复 IO
    r.return(200, `${text.length}|${form.get('a')}`);
}
export default { content_text_then_form };
```

3. 用 urlencoded body `a=1&a=2&z=3` POST 请求该 location。

**需要观察的现象**：

- 返回体长度等于请求体字节数（如 `a=1&a=2&z=3` 为 11 字节），`form.get('a')` 返回 `'1'`。
- 在 `ngx_http_js_collect_body` 入口加一行调试日志（仅本地实验，勿提交），确认第二次 `readRequestForm` 不会重新进入拼接逻辑——因为 `body_read_data` 已非空，直接返回。

**预期结果**：返回 `11|1`（与官方测试断言 `qr/200.*11\|1\|.../` 一致）。

> 待本地验证：精确字节长度取决于实际发送的请求体；若用 `curl --data` 注意末尾无多余换行。

#### 4.2.5 小练习与答案

**练习 1**：为什么不能在 `readRequestText()` 的 C 原生函数里直接调 `ngx_http_read_client_request_body()`？

答案：因为读取可能同步也可能异步，而 phase 处理函数该返回 `NGX_AGAIN` 还是 `NGX_DONE` 取决于此，但这一点在原生函数返回前无法确定。njs 的解法是让原生函数只置 DEFERRED、返回 Promise，由 phase handler 在调用返回后再统一发起读取并据结果选择返回码（设计说明见 [nginx/ngx_http_js_module.c:82-108](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L82-L108) 的注释）。

**练习 2**：`body_read_state` 用 3 个位编码了哪些信息？

答案：低 2 位（`state & 3`）是主状态 IDLE/DEFERRED/IN_PROGRESS；第 3 位是 FORM 标志，标记本次读取是为表单解析，resolve 时改走 `form_to_value` 而非 `body_to_value`。宏定义见 [nginx/ngx_http_js_module.c:113-122](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L113-L122)。

---

### 4.3 表单解析：ngx_js_form 的 urlencoded 与 multipart

#### 4.3.1 概念说明

`readRequestForm()` 把已读到的请求体字节交给引擎无关的解析器 `ngx_js_parse_form()`（定义在 `nginx/ngx_js_form.c`，HTTP 与 Stream 模块都可复用），解析结果是一个 `ngx_js_form_t`，内含一个 `ngx_js_form_entry_t` 条目数组。每个条目有 `name` / `value` / `filename` / `is_file` 四个字段（[nginx/ngx_js_form.h:21-32](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_form.h#L21-L32)）。

解析器按 `Content-Type` 分两条路径：

- `application/x-www-form-urlencoded`：形如 `a=1&b=2`，按 `&` 切分、按第一个 `=` 分割名值、解码 `+` 与 `%XX`。
- `multipart/form-data`：用 boundary 分隔多个 part，每个 part 有自己的头（`Content-Disposition: form-data; name="..."; filename="..."`）和体。

**关键设计取舍——文件上传只保留文件名，不保留内容。** 对 `is_file` 条目，解析器把 `value` 置空、只存 `filename`，并在 JS 侧把它呈现为一个 `{name: filename}` 对象（见 `ngx_http_js_request_form_entry_value`，[nginx/ngx_http_js_module.c:4229-4254](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L4229-L4254)）。换言之，njs 的 `readRequestForm` 是「表单字段元数据」解析器，不是「文件落盘」工具——上传文件的字节内容会被丢弃，只暴露文件名。

为防御恶意输入，解析器用一组常量给 boundary、part 头数量与大小、键数量设上限。

#### 4.3.2 核心流程

`ngx_js_parse_form` 的总流程：

```text
1. ngx_js_form_parse_content_type(content_type)
       ├── 识别 application/x-www-form-urlencoded  → URLENCODED
       └── 识别 multipart/form-data；提取 boundary → MULTIPART
2. switch(type):
       URLENCODED → ngx_js_form_parse_urlencoded(body)
       MULTIPART  → ngx_js_form_parse_multipart(body, boundary)
3. 每个 (name,value) 经 ngx_js_form_add_entry 入数组，受 maxKeys 限制
```

**urlencoded 路径**（[nginx/ngx_js_form.c:219-282](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_form.c#L219-L282)）：跳过前导 `&`；对每段找到下一个 `&` 为一段，再在其中找第一个 `=` 切名/值（没有 `=` 则值为空）；名和值都经 `ngx_js_form_decode_urlencoded` 解码（`+`→空格，`%XX`→字节）。

**multipart 路径**（[nginx/ngx_js_form.c:285-412](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_form.c#L285-L412)）：

1. 构造开边界 `--BOUNDARY`（`dlen`）与闭边界 `--BOUNDARY--`（`cdlen`）。
2. 校验 body 以开边界打头、且闭边界不在开边界之前。
3. 循环处理每个 part：
   - 找 `\r\n\r\n` 分隔 part 头与 part 体。
   - 用 `ngx_js_form_parse_part_headers` 解析头，提取 `Content-Disposition` 的 `name` 与 `filename`（有 `filename` 即 `is_file`）。
   - 找下一个 `\r\n--BOUNDARY` 作为 part 体结束。
   - `is_file` 则 `value` 置空并标记 `has_files`；否则把 part 体字节拷为 `value`。
4. 遇到 `--BOUNDARY--`（闭边界）结束。

边界常量（[nginx/ngx_js_form.c:19-22](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_form.c#L19-L22)）：

```c
#define NGX_JS_FORM_MAX_BOUNDARY_LEN     200   // RFC 2046 限 70，放宽到 200 容忍非标准客户端
#define NGX_JS_FORM_MAX_PART_HEADERS     32    // 单个 part 最多 32 个头
#define NGX_JS_FORM_MAX_PART_HEADER_LINE 4096  // 单行头最长 4096
#define NGX_JS_FORM_MAX_PART_HEADER_SIZE 16384 // 单个 part 头区合计最长 16384
```

键数量上限默认 128（`NGX_JS_FORM_DEFAULT_MAX_KEYS`，[nginx/ngx_js_form.h:14](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_form.h#L14)），可被 `readRequestForm({maxKeys: N})` 覆盖；超过则在 `ngx_js_form_add_entry` 报 `maxKeys limit exceeded`（[nginx/ngx_js_form.c:679-708](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_form.c#L679-L708)）。

JS 侧的 `RequestForm` 对象（原型表 [nginx/ngx_http_js_module.c:1236-1301](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L1236-L1301)）暴露 `get(name)` / `getAll(name)` / `has(name)` / `hasFiles()` / `forEach(cb)`，语义对齐 Web 的 `FormData`。`get` 找不到返回 `null`、找到多个返回第一个；`getAll` 返回数组。

#### 4.3.3 源码精读

总入口：

[nginx/ngx_js_form.c:65-112](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_form.c#L65-L112) — `ngx_js_parse_form` 先 `ngx_js_form_parse_content_type` 识别类型与 boundary，再按 `ct.type` 分派到 urlencoded 或 multipart 解析器，成功后回填 `*form`。返回码三态：`NGX_JS_FORM_OK`（成功）、`NGX_JS_FORM_TYPE_ERROR`（`NGX_DECLINED`，不支持的 content-type）、`NGX_JS_FORM_PARSE_ERROR`（`NGX_DONE`，格式错误）。HTTP 层据此映射成 `TypeError`（见 `ngx_http_js_form_to_value`，[nginx/ngx_http_js_module.c:3730-3756](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L3730-L3756)）。

content-type 解析（含 boundary 提取与上限校验）：

[nginx/ngx_js_form.c:115-216](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_form.c#L115-L216) — 用 `ngx_strncasecmp` 大小写不敏感匹配主类型，再用 `ngx_js_form_parse_param` 解析 `; boundary=...` 参数；boundary 长度必须 `0 < len <= 200`，重复 boundary 报错，multipart 缺 boundary 报错。

multipart 主循环里最关键的「找下一个 part 边界」逻辑：

```c
for ( ;; ) {
    next = ngx_js_form_find(scan, end, (u_char *) "\r\n--", 4);
    if (next == NULL) { /* truncated */ }
    if (next + 4 + boundary->len <= end
        && ngx_memcmp(next + 4, boundary->data, boundary->len) == 0) {
        break;            // 真正的 part 边界
    }
    scan = next + 4;      // 否则是 part 体内的伪匹配，继续往后找
}
```
— 见 [nginx/ngx_js_form.c:363-378](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_form.c#L363-L378)。这段代码处理一个边界条件：part 体里可能恰好包含 `\r\n--` 序列，必须再比对后面的 boundary 字节才能确认是真边界，避免误切。

part 头解析与 part 头大小限制：

[nginx/ngx_js_form.c:343-358](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_form.c#L343-L358) — 在解析 part 头前先校验头区不超过 `MAX_PART_HEADER_SIZE`（16384），`ngx_js_form_parse_part_headers` 内部再逐行校验行长（`MAX_PART_HEADER_LINE`）与头数（`MAX_PART_HEADERS`），任一超标即 `PARSE_ERROR`。

文件字段处理：

[nginx/ngx_js_form.c:382-391](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_form.c#L382-L391) — `is_file` 时 `value` 设为空串、`form->has_files = 1`；文件**字节内容被丢弃**，只保留 `filename`。这解释了为什么 JS 侧文件条目是 `{name}` 对象而非字符串。

JS 侧呈现：`ngx_http_js_request_form_entry_value`（[nginx/ngx_http_js_module.c:4229-4254](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L4229-L4254)）对普通条目返回字符串、对 `is_file` 条目返回只含 `name` 属性的对象。

#### 4.3.4 代码实践

**实践目标**：写一个接收 `multipart/form-data` 上传、用 `readRequestForm()` 解析并回显字段名与文件名的 `js_content` handler；同时说明 `body_read_state` 的状态切换。

**操作步骤**（蓝本：`nginx/t/js_request_form.t` 第 298-310 行的 multipart 用例与 `content_form` handler）：

1. 配置 location 与 handler：

```nginx
# 示例配置
location /upload {
    js_content test.upload;
}
```

```js
// 示例代码，改写自 nginx/t/js_request_form.t 第 155-162、97-140 行
function render(form) {
    const pairs = [];
    form.forEach((value, key) => {
        pairs.push(typeof value === 'string'
            ? `${key}=${value}`
            : `${key}=[file:${value.name}]`);
    });
    return `hasFiles=${form.hasFiles()}|${pairs.join('&')}`;
}

async function upload(r) {
    try {
        const form = await r.readRequestForm({maxKeys: 8});
        r.return(200, render(form));
    } catch (e) {
        r.return(500, `${e.constructor.name}:${e.message}`);
    }
}
export default { upload };
```

2. 用 curl 发一个 multipart 请求（含一个文本字段和一个文件字段）：

```bash
curl -s http://127.0.0.1:8080/upload \
  -F 'a=1' -F 'upload=@a.txt'
```

**需要观察的现象**：

- 返回类似 `hasFiles=true|a=1&upload=[file:a.txt]`：文件字段的**内容**不出现，只有文件名 `a.txt`。
- 把 `{maxKeys: 8}` 改成 `{maxKeys: 1}` 再发两个字段的表单，应得到 `RangeError`/`TypeError`（`maxKeys limit exceeded`），对应源码 `ngx_js_form_add_entry` 的限制。
- 把 Content-Type 故意改成 `text/plain`，应得到 `TypeError: unsupported content type`（`NGX_JS_FORM_TYPE_ERROR`）。

**`body_read_state` 状态切换说明**（async 路径，在 access/content 阶段）：

1. JS 调 `readRequestForm()` → 若 `r->request_body` 已存在则同步 resolve（`DEFERRED→IDLE`，见 4.2.3）；否则置 `DEFERRED | FORM`，返回未决 Promise。
2. phase handler（content 阶段在 `ngx_http_js_content_handler` 内、access 阶段在 [nginx/ngx_http_js_module.c:1583-1607](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L1583-L1607)）检测到 DEFERRED，调 `ngx_http_read_client_request_body`。
3. 同步完成（`NGX_OK`）→ 状态回 IDLE；异步（`NGX_AGAIN`）→ 转 `IN_PROGRESS | FORM`，请求挂起。
4. 读体完成回调 `ngx_http_js_access_body_done`（[nginx/ngx_http_js_module.c:3836-3878](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L3836-L3878）→ 收集字节 → 因 FORM 位被 `ngx_http_js_body_resolve` 走 `form_to_value` 解析表单 → resolve Promise → 状态回 IDLE。

**预期结果**：multipart 上传返回 `hasFiles=true|a=1&upload=[file:a.txt]`；与官方测试断言（[nginx/t/js_request_form.t:306-309](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/t/js_request_form.t#L306-L309)）一致。

> 待本地验证：文件名字段在 `headersOut`/FormData 下的精确大小写与 curl 的 `-F` 行为相关；可对照官方 `.t` 文件用 `http_post_form` + `multipart_form` 构造的请求体来复现。

#### 4.3.5 小练习与答案

**练习 1**：multipart part 体里如果出现 `\r\n--` 但后面不是真正的 boundary，解析器会怎样？

答案：不会误判。`ngx_js_form_parse_multipart` 在 [nginx/ngx_js_form.c:363-378](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_form.c#L363-L378) 找到 `\r\n--` 后还会比对后续 `boundary->len` 字节是否等于 boundary，不等则把 `scan` 前推继续找，确保只在该 part 真正结束时切分。

**练习 2**：上传一个 1MB 的文件，JS 侧 `form.get('file')` 拿到的是什么？为什么？

答案：拿到的是 `{name: "文件名"}` 对象，**不是**文件内容。源码在 [nginx/ngx_js_form.c:382-391](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_form.c#L382-L391) 对 `is_file` 条目丢弃内容、只存 filename，`ngx_http_js_request_form_entry_value`（[nginx/ngx_http_js_module.c:4237-4253](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L4237-L4253)）据此生成只含 `name` 的对象。njs 的表单解析面向「字段元数据」，文件落盘需另行处理。

**练习 3**：`readRequestForm({maxKeys: 1})` 解析一个两字段的 urlencoded 体，会得到什么？

答案：抛 `TypeError`，消息为 `maxKeys limit exceeded`。计数在 `ngx_js_form_add_entry`（[nginx/ngx_js_form.c:686-689](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_form.c#L686-L689)）里 `++(*count) > max_keys` 时触发，HTTP 层把它映射成 `TypeError`（[nginx/ngx_http_js_module.c:3748-3751](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L3748-L3751)）。官方测试 `content_form_limit` 即验证此行为。

## 5. 综合实践

把三个模块串起来：写一个 `js_content` handler，接收一个 `multipart/form-data` 请求，先用 `readRequestForm()` 解析出字段，然后对其中名为 `next` 的字段值（一个内部 URI）发起 `r.subrequest()`，最后把子请求的状态与表单内容一起返回。

```js
// 示例代码（综合实践，需在加载 ngx_http_js_module 的 NGINX 中运行）
async function aggregate(r) {
    // 1) 异步读体并解析表单（触发 body_read_state: IDLE -> DEFERRED -> ... -> IDLE）
    const form = await r.readRequestForm({maxKeys: 8});

    // 2) 取出要派发的内部 URI
    const target = form.get('next');        // 字符串字段
    if (typeof target !== 'string') {
        r.return(400, 'missing next');
        return;
    }

    // 3) 对内部 location 发起子请求（IN_MEMORY，零网络开销）
    const reply = await r.subrequest(target);

    // 4) 汇总输出：表单是否有文件 + 子请求状态
    const files = form.hasFiles();
    r.return(200, JSON.stringify({ target, files, subStatus: reply.status }));
}

export default { aggregate };
```

**验证要点**：

- 用 `curl -F 'next=/p/sub1' http://.../aggregate` 触发；`reply.status` 应为 `/p/sub1` 的响应码。
- 在 `ngx_http_js_collect_body` 与 `ngx_http_js_subrequest_done` 处加临时日志，分别观察「请求体读取完成」与「子请求完成」两个事件的发生顺序，体会「两次异步挂起—恢复」如何被 NGINX 事件循环串联。
- 思考：为什么整个 handler 能用 `async/await` 顺序写，却不会阻塞 NGINX worker？（答：每次 `await` 都让出控制权，由事件循环在对应 IO 完成时通过 jobs 队列恢复，见 u4-l5、u9-l1。）

> 待本地验证：综合实践依赖完整 NGINX 集成环境；若仅 `build/njs` CLI 无 `r` 对象，可退化为「源码阅读型实践」——跟踪 `aggregate` 中每个 `await` 分别对应 `body_read_state` 的哪次迁移与 `ngx_http_js_subrequest_done` 的哪次回调。

## 6. 本讲小结

- `r.subrequest()` 发起的是 NGINX **内部**子请求（非真实网络请求），靠 `NGX_HTTP_SUBREQUEST_IN_MEMORY` 把响应收进内存；无回调时返回 Promise、有回调时返回 `undefined`，`detached` 与回调互斥。完成回调 `ngx_http_js_subrequest_done` 把子请求对象包成 reply 送回 JS。
- 四个 `readRequest*()` 方法的异步性由 `body_read_state` 状态机（IDLE/DEFERRED/IN_PROGRESS + FORM 位）管理：JS 调用只置 DEFERRED，由 phase handler 在调用返回后才发起 `ngx_http_read_client_request_body`，从而正确选择 `NGX_AGAIN`/`NGX_DONE`；只有 access/content/periodic 支持 async。
- 读到的请求体被缓存（`body_read_data/len`），表单解析结果单独缓存（`request_form`），同一请求内重复读取不重复 IO。
- `ngx_js_form.c` 是引擎无关的表单解析器：urlencoded 按 `&`/`=` 切分并解码 `+`/`%XX`；multipart 用 boundary 切 part、解析 `Content-Disposition` 取 name/filename。
- 文件上传字段**只保留文件名、丢弃内容**，JS 侧呈现为 `{name}` 对象；`NGX_JS_FORM_MAX_*` 常量与 `maxKeys`（默认 128）共同防御恶意输入。
- 子请求完成与读体完成都通过 jobs/事件机制把结果投递回 JS，体现「JS 异步 = NGINX 事件循环驱动 + jobs 队列结算」的统一模型。

## 7. 下一步学习建议

- **回到 fetch 对比**：重读 u9-l1 的 `ngx.fetch()` 与本讲的 `r.subrequest()`，整理一张「内部派发 vs 对外 HTTP」的对照表，弄清各自何时用 `ngx_js_http.c` 状态机、何时用 NGINX subrequest 机制。
- **深入 stream 模块**：`ngx_js_form.c` 同样服务于 stream 场景，可阅读 `nginx/ngx_stream_js_module.c` 看表单解析在非 HTTP 上下文如何复用。
- **扩展到请求体过滤**：结合 u8-l2 的 `js_body_filter`，理解为何 body filter 不支持异步读体（它本身就在处理 body 流），以及它与 `readRequest*()` 一次性读取模型的对立。
- **测试工程**：阅读 `nginx/t/js_request_form.t` 与 `nginx/t/js_subrequests.t` 全文，对照 u10-l1 学习如何用 `prove` + `TEST_NGINX_GLOBALS_HTTP='js_engine qjs;'` 在双引擎下跑这些用例。
