# ngx_http_js_module：HTTP 请求处理与 r 对象

## 1. 本讲目标

本讲承接 u8-l1「ngx_js 共享层与引擎抽象」。上一讲我们看到共享层用 `ngx_engine_t` 把 njs 与 QuickJS 两套引擎抽象成统一的「模板 VM → 请求期 clone → 销毁」时序，并解析了 `js_import`/`js_engine`/`js_path` 等指令。但那些指令只是「配置期准备」，真正让 JavaScript **在请求处理过程中跑起来**的是本讲的主角——`ngx_http_js_module`。

读完本讲，你应当能够：

1. 说清楚 `js_content`、`js_access`、`js_header_filter`、`js_body_filter`、`js_set`、`js_var`、`js_periodic` 这些指令分别通过什么机制被 NGINX 触发（phase handler、content handler、output filter、变量 get_handler、定时器），以及它们各自的同步/异步约束。
2. 描绘一次 HTTP 请求中「per-request VM」的完整生命周期：何时 `ngx_http_js_init_vm` 创建请求级 ctx、何时 `engine->clone` 出独立 VM、何时由 `ngx_pool_cleanup_add` 注册销毁钩子。
3. 看懂 `r` 对象（Request）的外部原型 `ngx_http_js_ext_request[]`，理解 `headersIn`/`headersOut`/`variables`/`return`/`send`/`finish`/`subrequest` 等成员的声明方式，以及 `r.return()` / `r.finish()` / `r.done()` 三者截然不同的终止语义。

## 2. 前置知识

在进入源码前，先建立两个直觉。

### 2.1 NGINX 的请求处理阶段（phase）

NGINX 处理一个 HTTP 请求不是一锅烩，而是把工作切成一条有序的阶段链：`post_read → server_rewrite → find_config → rewrite → post_rewrite → preaccess → access → post_access → precontent → content → log`。每个阶段挂着一组 handler，核心模块 `ngx_http_core_module` 依次调用它们。其中和 njs 最相关的是：

- **access 阶段**：决定「放行还是拒绝」，典型如 IP 黑白名单、鉴权。handler 返回 `NGX_OK` 放行、返回 `4xx`（如 403）则拒绝。
- **content 阶段**：生成响应内容。这是「真正干活」的阶段，location 通过 `clcf->handler` 指派一个内容生成器。
- **output filter（输出过滤器）**：不属于「阶段」，而是 NGINX 发送响应时的一条过滤链。`ngx_http_top_header_filter` 和 `ngx_http_top_body_filter` 是两条过滤链的链头，模块通过把链头换成自己的函数、并把旧链头存进 `next` 变量来「插入」过滤器（这是经典的装饰器/责任链模式）。

njs 把 JS 钩子挂到 access 阶段、content 阶段、header/body 过滤器这四个点上，从而覆盖「鉴权 → 生成内容 → 改写头 → 改写体」的完整链路。

### 2.2 指令驱动与「先收集、后编译」

回顾 u1-l1 的核心结论：**JS 不会自启动**，必须由 NGINX 指令在某个阶段触发。这里再补一条 u8-l1 已建立的认知：像 `js_import`、`js_path` 这类指令在**配置解析期**只是把名字和路径收集进 `loc_conf` 的数组里，真正的「拼引导脚本 → 编译成模板 VM」被刻意推迟到合并配置结束时的 `ngx_js_init_conf_vm`。本讲的 `js_content` 等指令也是同样思路——解析期只记录「要调用哪个函数」（存成字符串如 `main.hello`），运行期才去模板 VM 里找到它并执行。

> 关键术语速查：**loc_conf**（location 级配置）、**ctx**（请求级上下文，挂在 `r` 上）、**模板 VM**（配置期创建、跨请求复用的引擎实例）、**proto_id**（外部原型句柄，连接 JS 对象与 C 结构体）。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [nginx/ngx_http_js_module.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c) | 本讲主战场：指令表、phase handler、`r` 对象原型、per-request VM 初始化与清理，约 1 万行 |
| [nginx/ngx_js.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.c) | 共享层。本讲用到 `ngx_js_init_conf_vm`（配置期创建模板 VM）、`ngx_js_ctx_init`/`ngx_js_ctx_destroy` |
| [nginx/ngx_js.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.h) | `ngx_engine_t` 引擎抽象（u8-l1 主角）、`ngx_js_ctx_t` 请求级 ctx 的公共字段宏 |
| [nginx/t/js.t](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/t/js.t) | HTTP 模块集成测试，提供本讲实践任务的可运行配置样例 |
| [README.md](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/README.md) | Hello World 示例（`r.return(200, ...)` + `js_content`） |

## 4. 核心概念与源码讲解

本讲拆为三个最小模块：**4.1 指令与 phase 绑定**、**4.2 per-request VM**、**4.3 r 对象原型**。

### 4.1 指令与 phase 绑定

#### 4.1.1 概念说明

`ngx_http_js_module` 对外暴露的指令分两类：

- **配置型指令**（不绑定 phase）：`js_engine`（选引擎）、`js_import`/`js_path`（引入模块）、`js_preload_object`（预加载对象）、`js_context_reuse*`（上下文复用）、`js_fetch_*`（fetch 客户端调参）、`js_shared_dict_zone`（声明共享字典）。它们只在配置解析期生效，产出 loc_conf 字段。
- **执行型指令**（绑定到请求处理的某个触发点）：`js_access`、`js_content`、`js_header_filter`、`js_body_filter`、`js_set`、`js_var`、`js_periodic`。本模块的核心价值就在这里——它们决定了「JS 函数在请求生命周期的哪个时刻、以什么方式被调用」。

一个反直觉但必须记牢的点：**`js_set` 并不绑定到某个固定 phase**。它做的事是注册一个 NGINX 变量（如 `$test_method`），并把这个变量的「取值回调」设成一个调用 JS 的函数。NGINX 变量是**惰性求值**的——只有在配置里（如 `return 200 $test_method;`）或代码里实际读取该变量时，取值回调才会被调用，调用时刻取决于「谁读了它」。因此 `js_set` 不能用异步操作（不能在里头 `await`/`fetch`），否则会报错。

#### 4.1.2 核心流程

整个模块的「钩子安装」发生在配置后处理（postconfiguration）阶段，由 `ngx_http_js_init` 完成：

```text
postconfiguration (ngx_http_js_init):
  ├─ 把 ngx_http_top_header_filter 接管成 ngx_http_js_header_filter  (插入 header 过滤器)
  ├─ 把 ngx_http_top_body_filter   接管成 ngx_http_js_body_filter    (插入 body 过滤器)
  └─ 向 NGX_HTTP_ACCESS_PHASE 阶段注册 ngx_http_js_access_handler   (注册 access handler)
```

注意上面**只注册了 access handler**，并没有注册 content handler。content 阶段的钩子是另一种机制：`js_content` 指令在解析期把自己设为 location 的内容生成器（`clcf->handler`），这只有写了 `js_content` 的 location 才会触发。而 header/body 过滤器则相反——只要模块加载就会插入链头，但每个请求真正调用 JS 的前提是该 location 配了 `js_header_filter`/`js_body_filter`（否则直接放行给 `next` 过滤器）。

各执行型指令的触发点与同步/异步约束归纳如下表：

| 指令 | 触发机制 | 时刻 | 异步支持 |
|---|---|---|---|
| `js_access` | 注册进 `NGX_HTTP_ACCESS_PHASE` 的 handler | access 阶段 | **支持**（`readRequest*`、`subrequest`、`fetch`） |
| `js_content` | 设为 `clcf->handler` | content 阶段 | **支持** |
| `js_header_filter` | 接管 `ngx_http_top_header_filter` | 发送响应头时 | **不支持**（同步） |
| `js_body_filter` | 接管 `ngx_http_top_body_filter` | 发送响应体每个 chunk 时 | **不支持**（同步） |
| `js_set` | 变量 get_handler | 变量被读取时 | **不支持**（同步） |
| `js_var` | 变量 get_handler（可被 JS 写） | 变量被读取时 | 同步 |
| `js_periodic` | worker 启动时挂的定时器事件 | 周期性后台 | 内部可异步 |

#### 4.1.3 源码精读

**指令表** 全部声明在 `ngx_http_js_commands[]`，每个条目是 NGINX 标准的 `ngx_command_t`：`名字 + 适用上下文 + 解析回调 + 偏移量`。执行型指令都带 `NGX_HTTP_LOC_CONF|NGX_HTTP_LIF_CONF|NGX_HTTP_LMT_CONF`，表示可在 location、location 内 `if`、`limit_except` 三种块里使用：

- `js_access` 解析回调 `ngx_http_js_access` 把函数名字符串存进 `jlcf->access`：[nginx/ngx_http_js_module.c:626-631](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L626-L631)
- `js_content` 解析回调 `ngx_http_js_content`：[nginx/ngx_http_js_module.c:633-638](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L633-L638)
- `js_header_filter` 直接用通用 `ngx_conf_set_str_slot` 存进 `jlcf->header_filter`：[nginx/ngx_http_js_module.c:640-645](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L640-L645)
- `js_body_filter` 用 `ngx_http_js_body_filter_set`，额外解析 `buffer_type=` 参数：[nginx/ngx_http_js_module.c:647-652](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L647-L652)
- `js_set` 用 `ngx_http_js_set`，`NGX_CONF_TAKE23`（2~3 个参数：`$变量 函数 [nocache]`）：[nginx/ngx_http_js_module.c:612-617](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L612-L617)
- `js_periodic` 用 `ngx_http_js_periodic`：[nginx/ngx_http_js_module.c:591-596](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L591-L596)

**postconfiguration 注册钩子**——整个 phase 绑定的核心在 `ngx_http_js_init`，注意它只注册 access handler，并把两条 filter 链头接管过来：[nginx/ngx_http_js_module.c:9565-9587](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L9565-L9587)（关键三行：把旧链头存进 `ngx_http_next_header_filter`/`ngx_http_next_body_filter`，再把链头换成自己的 filter；以及 `ngx_array_push(&cmcf->phases[NGX_HTTP_ACCESS_PHASE].handlers)` 推入 access handler）。

**content handler 是 location 级设置**——`js_content` 的解析回调里，关键就一句 `clcf->handler = ngx_http_js_content_handler`，把内容生成器指派给 njs：[nginx/ngx_http_js_module.c:9981-10000](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L9981-L10000)（第 9997 行）。

**access 阶段 handler**：[nginx/ngx_http_js_module.c:1528-1619](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L1528-L1619)。逻辑要点：先看 `jlcf->access.len` 是否为 0（没配就 `NGX_DECLINED` 跳过）；只对主请求执行（`r != r->main` 时跳过，子请求不跑 access JS）；初始化 VM 后调用 `ctx->engine->call(ctx, &jlcf->access, &ctx->args[0], 1)` 执行 JS。异步分支用 `ctx->in_progress` 配合 `NGX_AGAIN` 挂起，恢复后由 `ngx_http_js_access_finalize` 把 `ctx->status` 交还 NGINX 阶段引擎——这就是「JS 决定放行/拒绝」的物理实现。

**content 阶段 handler**：[nginx/ngx_http_js_module.c:1668-1683](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L1668-L1683)。它先 `ngx_http_read_client_request_body` 读取请求体（回调 `ngx_http_js_content_event_handler`），再在回调里调用 JS：[nginx/ngx_http_js_module.c:1687-1732](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L1687-L1732)。注意第 1716 行 `ctx->status = NGX_HTTP_INTERNAL_SERVER_ERROR`——**默认就是 500**，注释明确说明「期望被 `finish()`/`return()`/`internalRedirect()` 覆盖，否则视为 content handler 无效」。这是理解 `r` 对象终止语义的关键伏笔。

**header 过滤器**：[nginx/ngx_http_js_module.c:1837-1886](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L1837-L1886)。若 `jlcf->header_filter.len == 0` 直接调用 `ngx_http_next_header_filter` 放行；否则调 JS。第 1878-1883 行的检查很重要：若 JS 里出现异步操作（`rc == NGX_AGAIN` 且之前不 pending），直接 `NGX_LOG_ERR` 并返回 `NGX_ERROR`——**header filter 不允许异步**。

**js_set 的变量机制**：指令函数 `ngx_http_js_set` 用 `ngx_http_add_variable` 注册变量、设 `v->get_handler = ngx_http_js_variable_set`：[nginx/ngx_http_js_module.c:9807-9846](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L9807-L9846)（关键第 9846 行）。真正调 JS 的 `ngx_http_js_variable_set` 在：[nginx/ngx_http_js_module.c:2040-2097](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L2040-L2097)。注意第 2080-2084 行同样禁止异步，且最后用 `ctx->engine->string(...)` 把 JS 返回值转成字符串填进 `ngx_http_variable_value_t`——这就是 `js_set` 函数「必须 `return` 一个字符串/可转字符串的值」的原因。

#### 4.1.4 代码实践：从测试里反推 phase 绑定

**实践目标**：在不实际编译 NGINX 的前提下，通过阅读测试配置与源码，验证「`js_set` 注册的变量在何时被求值」。

**操作步骤**：

1. 打开 [nginx/t/js.t](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/t/js.t)，找到第 40-51 行的多个 `js_set $xxx test.xxx;` 声明，以及第 62-64 行的 `location /method { return 200 $test_method; }`。
2. 对照源码 `ngx_http_js_variable_set`（[L2040-L2097](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L2040-L2097)），确认 `$test_method` 只有在 `return 200 $test_method` 真正求值时才触发 `ctx->engine->call`。
3. 回想 NGINX 的 `return` 指令运行在 rewrite 阶段。于是得出结论：**`js_set` 绑定的函数实际在 rewrite 阶段（或任何读取该变量的阶段）被惰性调用，而非 access/content**。

**需要观察的现象**：在 error.log 里能否看到 `js_set` 函数被多次调用？（NGINX 变量默认可缓存，同一请求内只求一次；加 `nocache` 参数则每次读取都重新调用。）

**预期结果**：`js_set` 的求值次数取决于变量被读取的次数与缓存标志，而非绑定到固定 phase。

**待本地验证**：若本地有 nginx + njs 环境，可用 `prove -I nginx/t/lib nginx/t/js.t` 跑该用例，并在 `test.js` 的 `method` 函数里加 `r.log('called')` 观察日志。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `js_header_filter` 不允许异步操作，而 `js_content` 允许？

> **答案**：header filter 运行在 NGINX 发送响应头的同步路径上，filter 必须立即返回决定是否放行/改写头；若允许异步，NGINX 无法在该阶段挂起等待。content handler 则可以通过 `NGX_AGAIN` 把请求挂起、改写 `r->write_event_handler`，由事件循环驱动恢复（见 `ngx_http_js_content_write_event_handler`）。

**练习 2**：指令表里 `js_content` 的适用上下文是 `NGX_HTTP_LOC_CONF|NGX_HTTP_LIF_CONF|NGX_HTTP_LMT_CONF`，缺了 `NGX_HTTP_MAIN_CONF` 和 `NGX_HTTP_SRV_CONF`。这说明什么？

> **答案**：`js_content` 只能写在 location（含其内部 `if`、`limit_except`）里，不能写在 `http {}` 或 `server {}` 顶层。这与「content 阶段是 location 级内容生成器」的语义一致——必须有具体 location 才能指派 `clcf->handler`。

### 4.2 per-request VM

#### 4.2.1 概念说明

回顾 u8-l1 的标准时序：配置期用 `ngx_js_init_conf_vm` 建一个**模板 VM**（挂在 loc_conf 的 `engine` 字段上），请求期用 `engine->clone` 出一个**独立副本**，请求结束销毁副本。本节专门讲「请求期」这一段，入口是 `ngx_http_js_init_vm`。

为什么每个请求要单独 clone 一份 VM？因为模板 VM 是跨请求共享的只读资源（编译好的字节码、内建对象），而每个请求有自己的 JS 状态：当前正在执行的函数、`r` 对象绑定的具体请求、临时变量、Promise jobs 等。clone 复用模板的字节码与共享内建对象（省内存、省编译），却重建私有的 runtime，实现**多请求隔离**——这正是 u2-l1 讲过的 `njs_vm_clone`「浅拷贝 shared、重建私有」的工程落地。

请求级状态全部装在 `ngx_http_js_ctx_t`（结构体 `ngx_http_js_ctx_s`）里，通过 `ngx_http_get_module_ctx(r, ngx_http_js_module)` 挂在请求 `r` 上。它的前半部分来自共享宏 `NGX_JS_COMMON_CTX`（含 `engine`、`log`、`args[3]`、`retval` 等字段），后半部分是 HTTP 专属字段（`status`、`done`、`filter`、`request_body`、`response_body`、`redirect_uri` 等）。

#### 4.2.2 核心流程

一次 access/content/filter 调用 JS 前，都会先调 `ngx_http_js_init_vm(r, proto_id)` 确保「本请求已具备 VM」。它的逻辑是幂等的——已初始化则直接返回 `NGX_OK`：

```text
ngx_http_js_init_vm(r, proto_id):
  1. jlcf = loc_conf(r)
     若 jlcf->engine == NULL (没配 js_import)  → 返回 NGX_DECLINED (本 location 没有 JS)
  2. ctx = module_ctx(r)
     若 ctx == NULL:
        ctx = ngx_pcalloc(r->pool, sizeof(ngx_http_js_ctx_t))   // 在请求池上分配
        ngx_js_ctx_init(ctx, r->connection->log)                // 初始化公共字段
        ngx_http_set_ctx(r, ctx, module)                        // 挂到请求上
  3. 若 ctx->engine != NULL → 返回 NGX_OK (幂等：本请求已 clone 过)
  4. ctx->engine = jlcf->engine->clone(ctx, jlcf, proto_id, r)  // 克隆模板 VM 并绑定 r
     若失败 → NGX_ERROR
  5. 注册请求池清理钩子:
        cln->handler = ngx_http_js_cleanup_ctx
        cln->data = ctx
  6. 返回 NGX_OK
```

第 4 步的 `clone` 是引擎抽象的函数指针（见 u8-l1 的 `ngx_engine_t`），它做两件事：克隆 VM、用 `proto_id` 和请求对象 `r` 创建 `r` 这个外部对象并绑进 VM。这样 JS 代码里看到的 `r` 才指向「当前这个 HTTP 请求」。

第 5 步是关键的生命周期保障：NGINX 在销毁请求池（`r->pool`）时会自动跑所有清理钩子，于是 `ngx_http_js_cleanup_ctx` 会在请求结束时销毁 VM 副本——**零泄漏、无需手动 free**。

#### 4.2.3 源码精读

**ctx 与 loc_conf 结构**：loc_conf 复用共享宏并加 4 个函数名字段（`access`/`content`/`header_filter`/`body_filter`）：[nginx/ngx_http_js_module.c:17-27](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L17-L27)。请求级 ctx 结构体 `ngx_http_js_ctx_s`，首字段即 `NGX_JS_COMMON_CTX`：[nginx/ngx_http_js_module.c:59-73](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L59-L73)（关注 `status`、`done`、`filter`、`request_body`、`response_body`、`redirect_uri` 这些字段，后续讲 `r` 对象方法时都会用到）。

**per-request VM 初始化主函数** `ngx_http_js_init_vm`：[nginx/ngx_http_js_module.c:2128-2175](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L2128-L2175)。逐段对应上面的流程图：第 2135-2137 行判 `engine == NULL` 返回 `NGX_DECLINED`；第 2141-2150 行首次创建 ctx；第 2152-2154 行幂等返回；第 2156 行的 `jlcf->engine->clone(...)` 是核心克隆调用，注意第四个参数 `r` 就是后面 JS 里拿到的请求对象；第 2166-2172 行注册清理钩子。

**请求结束清理** `ngx_http_js_cleanup_ctx`：[nginx/ngx_http_js_module.c:2178-2214](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L2178-L2214)。注意第 2186-2188 行：若销毁时仍有未完成的异步事件（`ngx_js_ctx_pending`），记一条 ERR 日志——这是定位「JS 异步没跑完请求就结束」类 bug 的线索。第 2209 行特意新建一个临时池，是为了让 `njs.on('exit', ...)` 这类退出钩子即便在 `r->pool` 已被 NGINX 置空后仍能执行。最后 `ngx_js_ctx_destroy` 真正销毁引擎副本。

**配置期模板 VM 创建**（u8-l1 的衔接点，本节回顾）：`ngx_http_js_init_conf_vm` 把所有 `js_import` 拼成引导脚本（`import <name> from '<path>'; globalThis.<name> = <name>;`），再调 `ngx_create_engine(options)` 编译出模板引擎挂到 `conf->engine`：[nginx/ngx_js.c:4247-4307](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.c#L4247-L4307)（关键第 4303 行）。合并配置时由 `ngx_http_js_merge_loc_conf` 经 `ngx_js_merge_conf` 触发：[nginx/ngx_http_js_module.c:10107-10111](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L10107-L10111)。模板引擎也在配置池上注册了清理钩子 `ngx_js_cleanup_vm`（第 4309-4315 行），随配置生命周期销毁。

**引擎抽象** `ngx_engine_t`（回顾 u8-l1）：用 union 持底层句柄（`njs_vm_t *vm` 或 `JSContext *ctx`），七个函数指针 `compile/call/clone/external/pending/string/destroy` 构成手工 vtable：[nginx/ngx_js.h:274-311](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.h#L274-L311)。本模块全程通过 `ctx->engine->call(...)`、`ctx->engine->clone(...)` 这类指针调用，对底层是 njs 还是 QuickJS 完全透明。

#### 4.2.4 代码实践：跟踪一次请求的 VM 生命周期

**实践目标**：把「模板 VM → clone → 销毁」串成一条可观测的时序。

**操作步骤**：

1. 在源码里标注三个关键点：模板创建 [ngx_js.c:4303](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.c#L4303)、请求 clone [ngx_http_js_module.c:2156](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L2156)、请求销毁 [ngx_http_js_module.c:2211](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L2211)。
2. 注意 [ngx_http_js_module.c:2162-2164](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L2162-L2164) 已有一条 `debug3` 日志 `"http js vm clone %s: %p from: %p"`，打印「副本地址 + 模板地址」。
3. 在 [ngx_http_js_module.c:2190](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L2190) 附近已有一条 `"http js vm destroy: %p"` 日志。

**需要观察的现象**：用 `--with-debug` 构建 NGINX 并把 `error_log` 设为 `debug`，发起一次请求，应在日志里依次看到「(配置加载时)create engine」「(请求时)clone <副本> from <模板>」「(请求结束时)destroy <副本>」。多次请求应看到同一个 `from: <模板>` 地址，但每次 `clone` 出的副本地址不同（或因 `js_context_reuse` 复用而相同）。

**预期结果**：模板地址恒定、副本随请求生灭，验证「一次编译、多次克隆执行」的设计。

**待本地验证**：本实践需在独立 NGINX 源码树里 `--add-module=<njs>/nginx --with-debug` 编译。

#### 4.2.5 小练习与答案

**练习 1**：`ngx_http_js_init_vm` 在第 2152-2154 行有一个「若 `ctx->engine` 已存在则直接返回 OK」的幂等检查。为什么需要它？

> **答案**：一次请求可能先后命中多个 JS 钩子（例如先 access 后 content，或先 header_filter 后 body_filter），它们各自会调 `ngx_http_js_init_vm`。幂等检查保证**整个请求只 clone 一次** VM，后续钩子复用同一个 `ctx->engine`，从而共享同一份 JS 状态（如模块作用域里设置的变量）。

**练习 2**：为什么 `ngx_http_js_cleanup_ctx` 里要检测 `ngx_js_ctx_pending(ctx)`？

> **答案**：若请求结束时仍有未结算的 Promise/未完成的 subrequest 或 fetch，说明 JS 异步逻辑没跑完就被 NGINX 回收了，通常是 handler 漏写 `r.done()`/`r.finish()` 或 await 未完成。记一条 ERR 日志帮助定位这类「静默丢请求」的 bug。

### 4.3 r 对象原型

#### 4.3.1 概念说明

JS 代码在 `js_content`/`js_access` 等 handler 里收到的第一个参数就是 `r`——一个代表当前 HTTP 请求的宿主对象。回顾 u5-l4「外部对象与原生函数」：内置 njs 引擎用「外部原型描述符 `njs_external_t` + 整数句柄 `proto_id`」把 C 结构体 `ngx_http_request_t` 包成 JS 对象。本模块用三个 `proto_id` 区分三类外部对象：

```c
static njs_int_t    ngx_http_js_request_proto_id = 1;        // r 对象
static njs_int_t    ngx_http_js_periodic_session_proto_id = 2; // js_periodic 的 session 对象
static njs_int_t    ngx_http_js_request_form_proto_id = 3;   // readRequestForm 返回的表单对象
```

这些 proto_id 在 `ngx_http_js_init_vm` 克隆时作为参数传入，用来告诉引擎「这次 clone 要绑哪种外部对象原型」。`r` 对应的是 `ngx_http_js_request_proto_id`。

QuickJS 引擎侧不使用 `njs_external_t`，而是用 JS 类（`NGX_QJS_CLASS_ID_HTTP_REQUEST`）注册同样一组方法（见 [ngx_http_js_module.c:9442-9456](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L9442-L9456)）。这是「双引擎=双份代码」铁律在 `r` 对象上的体现——**对外 JS API 形状一致，内部机制两套**。本节以内置 njs 引擎的 `ngx_http_js_ext_request[]` 为主讲解。

#### 4.3.2 核心流程

`r` 对象的所有成员声明在静态数组 `ngx_http_js_ext_request[]` 里，每个条目用 `flags` 低 2 位区分三类（u5-l4 已建立）：

- **`NJS_EXTERN_PROPERTY`**：只读或可写属性，如 `r.uri`、`r.method`、`r.status`、`r.headersIn`。
- **`NJS_EXTERN_METHOD`**：方法，如 `r.return()`、`r.send()`、`r.finish()`、`r.subrequest()`。
- **`NJS_EXTERN_OBJECT`**：子对象（带自己的 `prop_handler`），如 `r.headersIn`/`r.headersOut`/`r.variables`/`r.rawVariables`。

每个属性/方法都通过一个 C 回调读写底层 `ngx_http_request_t`。回调约定（u5-l4）：第一个参数 `args[0]` 是 `this`（即外部对象本身），用 `njs_vm_external(vm, proto_id, njs_argument(args, 0))` 反查出 C 指针 `r`；`magic8`/`magic32` 用作「字段选择器」，让一个 C 函数服务多个 JS 成员。

`r` 对象最核心的是它的**终止语义**——content handler 默认 status 是 500，JS 必须用以下方法之一收尾，否则视为无效：

| 方法 | 作用 | 设置的 ctx 字段 |
|---|---|---|
| `r.return(code, text)` | 直接返回状态码 + 文本 | 调 `ngx_http_send_response`，写 `ctx->status` |
| `r.finish()` | 结束分块流式输出 | 发 LAST 标记，`ctx->status = NGX_OK` |
| `r.done()` | 仅 body_filter 中标记本请求过滤完成 | `ctx->done = 1`（非 filter 调用会抛错） |
| `r.decline()` | 放弃（access 阶段交还默认行为） | `ctx->status = NGX_DECLINED` |
| `r.internalRedirect(uri)` | 内部重定向到另一 location | 设置 `ctx->redirect_uri` |

#### 4.3.3 源码精读

**proto_id 定义**：[nginx/ngx_http_js_module.c:831-833](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L831-L833)。

**`r` 对象外部原型 `ngx_http_js_ext_request[]`**：[nginx/ngx_http_js_module.c:836-1233](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L836-L1233)。这张表是 `r` 对象的「形状说明书」，逐条速览几个代表成员：

- 开头用 `NJS_SYMBOL_TO_STRING_TAG` 声明 `[Symbol.toStringTag] = "Request"`，使 `Object.prototype.toString.call(r)` 返回 `[object Request]`：[L838-844](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L838-L844)
- `r.args`（查询参数）属性，handler 为 `ngx_http_js_ext_get_args`：[L846-853](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L846-L853)
- `r.headersIn` 子对象，`prop_handler = ngx_http_js_ext_header_in`、`keys = ngx_http_js_ext_keys_header_in`（后者支撑 `Object.keys(r.headersIn)`）：[L889-898](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L889-L898)
- `r.headersOut` 子对象，带 `writable`（可写，可设响应头）：[L900-911](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L900-L911)
- `r.method` 属性，复用通用 handler `ngx_js_ext_string`，用 `magic32 = offsetof(ngx_http_request_t, method_name)` 指明读哪个字段（这是「一个 handler 服务多属性」的典型用法，与 `r.uri`、`r.requestLine` 共用同一 handler，只差偏移量）：[L954-962](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L954-L962)
- `r.status` 可写属性（读写响应状态码）：[L1134-1142](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L1134-L1142)
- `r.variables` / `r.rawVariables` 子对象，用 `magic32` 区分返回字符串(`NGX_JS_STRING`)还是 Buffer(`NGX_JS_BUFFER`)：[L990-998](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L990-L998) 与 [L1212-1220](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L1212-L1220)
- 一组 `readRequest*` 异步方法，共用 handler `ngx_http_js_ext_read_request_body`、用 `magic8` 区分 `Text`/`JSON`/`ArrayBuffer`：[L1144-1189](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L1144-L1189)
- `r.subrequest` 方法：[L1191-1200](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L1191-L1200)（u9-l3 详讲）

**`r.return()` 实现** `ngx_http_js_ext_return`：[nginx/ngx_http_js_module.c:3240-3296](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L3240-L3296)。要点：先用 `njs_vm_external` 从 `this` 取回 `ngx_http_request_t *r`（第 3249 行，这是所有 `r.xxx` 方法的固定起手式）；状态码做范围校验（0..999，否则 range error）；若提供了 body 文本（或状态码 `< 400`），用 `ngx_http_send_response(r, status, NULL, &cv)` 发送并把返回值存进 `ctx->status`（第 3282 行）；否则只设状态码不发包。

**`r.finish()` 实现** `ngx_http_js_ext_finish`：[nginx/ngx_http_js_module.c:3211-3236](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L3211-L3236)。核心是 `ngx_http_send_special(r, NGX_HTTP_LAST)` 发一个「最后一块」的标记，并把 `ctx->status` 置为 `NGX_OK`——这是配合 `r.send()` 做流式输出的收尾动作。

**`r.done()` 实现** `ngx_http_js_ext_done`：[nginx/ngx_http_js_module.c:3182-3207](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L3182-L3207)。注意第 3197-3200 行：**非 filter 模式调用 `r.done()` 会抛 type error**——它专属于 body_filter，用来提前结束过滤循环。

**`r.send()` 实现** `ngx_http_js_ext_send`：[nginx/ngx_http_js_module.c:3004-3041](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L3004-L3041)。它把每个字符串参数包成 `ngx_buf_t` 串成 chain，供后续输出（content 阶段的手动流式写）。第 3023-3026 行同样禁止在 filter 里调用 `send`（filter 改写体应返回新数据，不能用 `send`）。

#### 4.3.4 代码实践：阅读 README 的 Hello World 并对照源码

**实践目标**：把 README 的 4 行 JS 与本节源码一一对应，理解 `r.return()` 究竟做了什么。

**操作步骤**：

1. 阅读 [README.md:167-173](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/README.md#L167-L173) 的示例：
   ```javascript
   function hello(r) {
     r.return(200, "Hello world!\n");
   }
   export default {hello}
   ```
2. 在原型表里定位 `r.return` 条目，确认它映射到 C 函数 `ngx_http_js_ext_return`：[L1057-1066](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L1057-L1066)。
3. 跟进 `ngx_http_js_ext_return`：[L3240-L3296](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L3240-L3296)。确认 `r.return(200, "Hello world!\n")` 会走到第 3282 行的 `ngx_http_send_response(r, 200, NULL, &cv)`，把文本作为 200 响应体发出，并把 `ctx->status` 设为该函数返回值。
4. 回到 content handler：[L1716-L1731](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L1716-L1731)，确认 JS 跑完后 `ctx->status` 已被 `r.return()` 覆盖为 200（不再是默认的 500），content_finalize 才会正常结束请求。

**需要观察的现象**：若把 `r.return(200, ...)` 改成空函数体（什么都不做），content handler 的默认 `ctx->status = 500` 会保留，请求将返回 500——这就是 content handler「必须显式收尾」的由来。

**预期结果**：能复述「`r.return(code, text)` → `ngx_http_send_response` → 写 `ctx->status` → content_finalize 据此结束请求」的完整链路。

**待本地验证**：可在 [nginx/t/js.t](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/t/js.t) 的 `location /njs`（`js_content test.njs`）基础上改写验证。

#### 4.3.5 小练习与答案

**练习 1**：`r.uri`、`r.method`、`r.requestLine` 三个属性共用同一个 C handler `ngx_js_ext_string`，只靠 `magic32` 不同区分。这种设计的好处是什么？

> **答案**：三个属性都是从 `ngx_http_request_t` 读取某个 `ngx_str_t` 字段，逻辑完全一致（取偏移、读字符串、返回）。用一个 handler + `magic32=offsetof(...)` 复用，避免为每个属性写一个几乎相同的函数，减少代码重复。这是 njs 外部对象机制「用 magic 复用 handler」哲学的典型应用（u5-l4）。

**练习 2**：在 body_filter 里写 `r.done()` 和在 content handler 里写 `r.finish()`，二者各自的用途是什么？能互换吗？

> **答案**：不能互换。`r.done()` 仅在 body_filter 有效，作用是「提前结束本次请求的 body 过滤循环」（设 `ctx->done = 1`），在 content 里调用会抛 type error；`r.finish()` 用于 content 阶段「结束流式输出」（发 LAST 标记），在 filter 里没有意义。两者分别服务于「改写体」和「生成体」两种场景。

## 5. 综合实践

把三个最小模块串起来，完成一个「自定义访问控制 + 内容生成」的小任务。

**任务**：写一份最小可用的 `nginx.conf` 与配套 JS，实现——访问 `/hello` 时，若请求头 `X-Token` 等于 `secret` 则返回 `200 Hello`，否则 `403`；并把请求方法通过 `js_set` 暴露成变量 `$req_method`。

**第 1 步：编写 JS 模块**（示例代码，非项目原有文件）：

```javascript
// gate.js —— 示例代码
function access(r) {
  // 运行在 access 阶段：r.headersIn 可读
  if (r.headersIn['X-Token'] === 'secret') {
    r.decline();          // 放行，交给后续 content 阶段
  } else {
    r.return(403, "Forbidden\n"); // 拒绝
  }
}

function hello(r) {
  // 运行在 content 阶段
  r.return(200, "Hello\n");
}

function method(r) {
  // 供 js_set 调用，必须 return 一个字符串
  return r.method;
}

export default { access, hello, method };
```

**第 2 步：编写 nginx.conf**（参考 README 与 [nginx/t/js.t](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/t/js.t#L37-L73) 的结构）：

```nginx
# 示例配置
load_module modules/ngx_http_js_module.so;   # 动态模块，静态编译时省略

events {}
http {
  js_engine qjs;                    # 推荐用 QuickJS
  js_import gate from /etc/nginx/njs/gate.js;

  js_set $req_method gate.method;   # 注册变量，惰性求值

  server {
    listen 80;
    location /hello {
      js_access  gate.access;       # access 阶段
      js_content gate.hello;        # content 阶段
    }
    location /method {
      return 200 $req_method;       # 读取变量时才调用 gate.method
    }
  }
}
```

**第 3 步：对照源码自检**：

1. `js_engine` / `js_import` / `js_set` / `js_access` / `js_content` 分别能在指令表 [ngx_http_js_commands](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L561-L757) 里找到对应条目吗？（`js_engine` 在 L563、`js_import` 在 L584、`js_set` 在 L612、`js_access` 在 L626、`js_content` 在 L633。）
2. `gate.access` 里 `r.headersIn` 走的是哪个 prop_handler？（`ngx_http_js_ext_header_in`，[L895](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L895)。）
3. `r.decline()` 与 `r.return(403)` 如何影响 access handler 的返回？（分别把 `ctx->status` 设为 `NGX_DECLINED` / 403，由 [access_finalize L1623-L1639](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L1623-L1639) 交还 NGINX。）

**预期结果**：

- `curl -H 'X-Token: secret' http://localhost/hello` → `200 Hello`
- `curl http://localhost/hello` → `403 Forbidden`
- `curl http://localhost/method` → `200 GET`（变量惰性求值触发 `gate.method`）

**待本地验证**：本实践需要在独立 NGINX 源码树中 `./configure --add-module=<njs>/nginx` 编译出带 njs 的 nginx，或加载动态模块 `ngx_http_js_module.so`。若暂无环境，可改为纯源码阅读：在 [nginx/t/js.t](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/t/js.t) 里找到等价的 `js_access`/`js_set` 用例（如 `test.sub_internal`），用 `prove -I nginx/t/lib nginx/t/js.t` 运行验证。

## 6. 本讲小结

- `ngx_http_js_module` 通过四类触发点接入 NGINX：access 阶段（`ngx_http_js_access_handler` 注册进 `NGX_HTTP_ACCESS_PHASE`）、content 阶段（`js_content` 设为 `clcf->handler`）、header/body 过滤器（postconfiguration 里接管 `ngx_http_top_header_filter`/`ngx_http_top_body_filter`）、变量（`js_set`/`js_var` 注册惰性求值的 get_handler）。`js_set` 不绑定固定 phase，而是变量被读取时才调用 JS。
- header_filter、body_filter、js_set 三个钩子**禁止异步**（源码里有显式的 `NGX_AGAIN` 检测并报错）；只有 js_access、js_content、js_periodic 允许异步，通过 `NGX_AGAIN` + `write_event_handler` 挂起恢复。
- per-request VM 由 `ngx_http_js_init_vm` 负责：在请求池上分配 `ngx_http_js_ctx_t`、`engine->clone` 出独立副本并绑定 `r`、再用 `ngx_pool_cleanup_add` 注册 `ngx_http_js_cleanup_ctx` 确保请求结束销毁；该函数幂等，整请求只 clone 一次。
- `r` 对象的形状由静态原型表 `ngx_http_js_ext_request[]` 声明（内置引擎用 `proto_id` + `njs_external_t`，QuickJS 用 `NGX_QJS_CLASS_ID_HTTP_REQUEST` JS 类），属性/方法/子对象靠 `flags` 区分，靠 `magic8/magic32` 让一个 C handler 服务多个 JS 成员。
- 终止语义是 `r` 对象的关键：content handler 默认 status 为 500，必须用 `r.return()`/`r.finish()`/`r.decline()`/`r.internalRedirect()` 之一收尾；`r.done()` 仅 body_filter 有效；`r.return()` 走 `ngx_http_send_response`，`r.finish()` 走 `ngx_http_send_special(NGX_HTTP_LAST)`。

## 7. 下一步学习建议

- 下一讲 **u8-l3「ngx_stream_js_module」** 会对照讲解 TCP/UDP 流模块：`js_preread`/`js_filter`/`js_access` 指令、`s` 会话对象原型（`allow`/`deny`/`done`/`send`/`on`/`off`）与 upload/download 事件机制。建议带着「http 与 stream 在指令集和终止语义上的差异」这个问题去读 `nginx/ngx_stream_js_module.c`，并对照本讲的 `ngx_http_js_ext_request[]`。
- 进阶到 **u9 单元**：`ngx.fetch()`（u9-l1，异步 HTTP 客户端，对应 `js_fetch_*` 指令）、`ngx.shared`（u9-l2，跨 worker 共享字典）、`subrequest` + 表单解析（u9-l3，本讲只点了 `r.subrequest` 的入口，深入机制在 u9-l3）。
- 若想验证本讲的实践，参考 [docs/agent/js-dev.md](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/js-dev.md) 的「Test inside NGINX」一节，用 `prove -I <tests-lib> nginx/t/js.t` 并加 `TEST_NGINX_GLOBALS_HTTP='js_engine qjs;'` 跑 QuickJS 版本。
