# ngx_js 共享层与引擎抽象

## 1. 本讲目标

本讲是「NGINX 集成」单元的第一讲，回答一个核心问题：

> njs 把 JavaScript 跑进 NGINX 时，HTTP 模块（`ngx_http_js_module`）和 Stream 模块（`ngx_stream_js_module`）是怎么共享同一套「把 JS 引擎绑到 NGINX 上」的基础设施的？

学完后你应该能够：

- 说清 `nginx/ngx_js.c` 为什么叫「共享绑定层」，它和两个 NGINX 模块的编译关系是什么。
- 读懂引擎抽象结构体 `ngx_engine_t`：它如何用一组函数指针，把内置 njs 引擎和 QuickJS 引擎包装成同一个接口。
- 画出「配置期创建模板 VM → 请求期 clone 出独立 VM → 请求结束 cleanup」的标准时序。
- 解释 `ngx_njs_clone` 与 `ngx_qjs_clone` 在克隆机制上的分工与差异。
- 理解 `js_import` / `js_engine` / `js_path` 等指令为什么能被两个模块共用，它们在配置解析期被谁处理。

本讲依赖 [u2-l1（njs_vm_t 生命周期）](u2-l1-vm-lifecycle-api.md) 中建立的「`njs_vm_create` → `njs_vm_compile` → `njs_vm_clone` → `njs_vm_destroy`」流程，以及 [u6-l1（QuickJS 包装层）](u6-l1-quickjs-wrapper.md) 中对 `qjs_new_context` 的认识。

## 2. 前置知识

阅读本讲前，最好已经了解：

- **NGINX 的配置树与请求阶段**：NGINX 在启动时解析 `nginx.conf` 形成「main → server → location」三层配置（location config，简称 loc_conf）；处理一个请求时，请求会经过 access、content、header_filter、body_filter 等阶段（phase）。`js_content`、`js_access` 等指令就是把一个 JS 函数挂到某个阶段上。
- **「模板 VM + 克隆」模型**（见 u2-l1）：njs 内置引擎支持「编译一次、克隆多次」。配置期把模块源码编译进一个**模板 VM**（只读的共享字节码与内建对象）；每个请求到来时，从模板 `njs_vm_clone` 出一个**独立 VM**，拥有自己的内存池与可变状态，请求结束销毁。QuickJS 侧没有 `clone`，而是「重放预编译字节码 + 新建上下文」达到同等效果。
- **函数指针表（手工 vtable）**：C 语言里实现「多态」的常用手法——把一组操作（编译、调用、克隆、销毁……）写成函数指针塞进结构体，运行时按引擎类型填入不同的实现，上层只通过指针调用、不关心底层差异。njs CLI 里叫它 `njs_create_engine`（见 [u1-l4](u1-l4-cli-entry-and-options.md)），NGINX 侧的对应物就是本讲的 `ngx_engine_t`。
- **`#if (NJS_HAVE_QUICKJS)`**：所有 QuickJS 相关代码都被这个构建期宏守卫。如果构建时没有链接 QuickJS（`--no-quickjs` 或探测失败），这些代码不参与编译，引擎只剩内置 njs 一种。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [nginx/ngx_js.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.h) | 共享层头文件：定义引擎抽象 `ngx_engine_t`、选项 `ngx_engine_opts_t`、上下文 `ngx_js_ctx_t`、loc_conf 公共字段宏，以及 `ngx_njs_clone` / `ngx_qjs_clone` 的声明。 |
| [nginx/ngx_js.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.c) | 共享层实现：`ngx_create_engine`（建模板引擎）、`ngx_njs_clone` / `ngx_qjs_clone`（克隆引擎）、`ngx_js_init_conf_vm` / `ngx_js_merge_vm`（配置期建机）、`ngx_js_import` / `ngx_js_engine`（指令解析）。 |
| [nginx/ngx_http_js_module.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c) | HTTP 模块：消费共享层。提供指令表、`r` 对象原型、phase handler、以及「绑定 `r`」的克隆包装 `ngx_engine_njs_clone` / `ngx_engine_qjs_clone`。 |
| [nginx/ngx_stream_js_module.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_stream_js_module.c) | Stream 模块：结构与 HTTP 模块对称，把共享层接到 TCP/UDP 流上，绑定 `s` 会话对象。 |
| [nginx/config](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/config) | NGINX 第三方模块构建胶水：声明共享源码清单 `NJS_SRCS`，并把它们同时链接进 http 与 stream 两个模块。 |

## 4. 核心概念与源码讲解

### 4.1 共享绑定层：ngx_js.c 与 nginx/config 构建胶水

#### 4.1.1 概念说明

njs 集成进 NGINX 不是「写一个模块」那么简单，而是要同时支持两种业务场景：

- **HTTP 请求处理**：在 `location` 里用 `js_content` 动态生成响应、用 `js_header_filter` 改头部。
- **TCP/UDP 流处理**：在 `server` 里用 `js_preread` 做协议判别、用 `js_filter` 改流内容。

两类场景都要做同一批「脏活」：

1. 选引擎（njs 还是 QuickJS），创建并编译模板 VM。
2. 每个请求/会话克隆一个独立 VM。
3. 解析 `js_import` / `js_engine` / `js_path` 等公共指令。
4. 驱动 Promise 作业、追踪未处理拒绝、注册 `ngx.fetch`、`ngx.shared` 等公共绑定。

如果让 http 和 stream 两个模块各写一遍，就会出现大段重复代码、且行为容易分叉。因此 njs 把这批公共逻辑抽到 `nginx/ngx_js.c`，称为**共享绑定层（shared layer）**。两个上层模块各自只关心「自己的专属对象」——HTTP 模块定义请求对象 `r`，Stream 模块定义会话对象 `s`——其余全复用共享层。

> 类比：`ngx_js.c` 像一个「JS 引擎驱动框架」，`ngx_http_js_module.c` 和 `ngx_stream_js_module.c` 是两个「插件」，往框架里填入各自的对象定义。

#### 4.1.2 核心流程

「共享」这件事不是靠 include 头文件实现的，而是靠**编译期把同一份 `.c` 源文件编进两个模块**实现的。`nginx/config` 里有一份共享源码清单：

```
NJS_SRCS="$ngx_addon_dir/ngx_js.c \
    $ngx_addon_dir/ngx_js_form.c \
    $ngx_addon_dir/ngx_js_http.c \
    $ngx_addon_dir/ngx_js_fetch.c \
    $ngx_addon_dir/ngx_js_regex.c \
    $ngx_addon_dir/ngx_js_shared_dict.c"
```

随后 HTTP 模块和 Stream 模块的源码列表都把 `NJS_SRCS` 拼了进去：

```
ngx_module_srcs="$ngx_addon_dir/ngx_http_js_module.c $NJS_SRCS $QJS_SRCS"   # HTTP
...
ngx_module_srcs="$ngx_addon_dir/ngx_stream_js_module.c $NJS_SRCS $QJS_SRCS" # Stream
```

也就是说，`ngx_js.c`（连同表单解析 `ngx_js_form.c`、fetch 客户端 `ngx_js_fetch.c`、共享字典 `ngx_js_shared_dict.c`）被同时编进了两个 NGINX 模块。

这里有一个对静态链接至关重要的去重细节：

```
if [ "$ngx_module_link" != DYNAMIC ]; then
    NJS_SRCS=
fi
```

含义是：如果两个模块都是**静态链接**进 nginx 主程序，那么 `NJS_SRCS` 只在第一个模块（http）里编译一次，编译完立刻清空（`NJS_SRCS=`），第二个模块（stream）就不再重复编译这些文件，避免符号重复定义。只有当模块是**动态链接**（`.so`）时，每个 `.so` 才会各自编入一份 `ngx_js.c`。

正是为了适配动态链接里「同一份 `ngx_js.c` 被两份 `.so` 各编一份」的情况，头文件里那段关于 `JSClassID` 的注释才特意把类 id 改成了一张**静态枚举表**（而不是各 `.so` 各自 `JS_NewClassID()` 动态申请），避免 `-Wl,-Bsymbolic-functions` 下的符号冲突：

```c
/*
 * This static table solves the problem of a native QuickJS approach
 * which uses a static variables of type JSClassID and JS_NewClassID() to
 * allocate class ids for custom classes. The static variables approach
 * causes a problem when two modules linked with -Wl,-Bsymbolic-functions flag
 * are loaded dynamically.
 */
enum {
    NGX_QJS_CLASS_ID_CONSOLE = QJS_CORE_CLASS_ID_LAST,
    NGX_QJS_CLASS_ID_HTTP_REQUEST,
    ...
    NGX_QJS_CLASS_ID_STREAM_SESSION,
    ...
};
```

#### 4.1.3 源码精读

共享源码清单与「两个模块共用」的编译关系，见构建胶水文件 [nginx/config:L14-L19](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/config#L14-L19)，这里列出 `ngx_js.c` 等共享文件。HTTP 与 Stream 模块分别把 `NJS_SRCS` 拼进各自源码列表，见 [nginx/config:L163-L191](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/config#L163-L191)。静态链接去重逻辑见 [nginx/config:L175-L177](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/config#L175-L177)。

QuickJS 类 id 静态枚举表及其「为动态链接避免符号冲突」的注释，见 [nginx/ngx_js.h:L48-L75](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.h#L48-L75)。

#### 4.1.4 代码实践

1. **实践目标**：从构建产物层面确认「`ngx_js.c` 被两个模块共用」。
2. **操作步骤**：
   - 打开 [nginx/config](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/config)，定位 `NJS_SRCS` 的定义（约 L14）。
   - 向下找到 HTTP 模块的 `ngx_module_srcs=...ngx_http_js_module.c $NJS_SRCS...`（约 L169）和 Stream 模块的对应行（约 L186）。
   - 确认两者都引用了同一个 `$NJS_SRCS` 变量。
3. **需要观察的现象**：两行的差异**只有**顶层模块文件名（`ngx_http_js_module.c` vs `ngx_stream_js_module.c`）和 `ngx_module_type`（`HTTP_AUX_FILTER` vs `STREAM`），共享源码部分完全一致。
4. **预期结果**：能指出 `ngx_js.c`、`ngx_js_fetch.c`、`ngx_js_shared_dict.c` 等 6 个共享文件既属于 http 模块也属于 stream 模块。
5. 待本地验证：若你有一份独立编译的 NGINX 源码树，执行 `./configure --add-module=<njs>/nginx ...` 后查看 `objs/Makefile`，可以看到 `ngx_js.o` 出现在两个模块的目标列表里（动态模块模式下各出现一次）。

### 4.2 引擎抽象 ngx_engine_t：统一两引擎的接口

#### 4.2.1 概念说明

共享层要同时驱动内置 njs 引擎（操作 `njs_vm_t *`）和 QuickJS 引擎（操作 `JSContext *`），这两种句柄类型完全不同、不能互通。怎么办？答案就是本节的主角——**引擎抽象 `ngx_engine_t`**。

它的设计思路和 u1-l4 里 CLI 的 `njs_create_engine` 一样：一个结构体持有「底层引擎句柄」+「一组操作它的函数指针」。上层（http/stream 模块）只认 `ngx_engine_t *`，永远通过函数指针调用，从不直接碰 `njs_vm_t` 或 `JSContext`。这样一来，切换引擎对上层是透明的。

#### 4.2.2 核心流程

`ngx_engine_t` 的核心字段可以分成三组：

1. **底层句柄（union u）**：用 `union` 让同一个槽位在不同引擎下存不同类型——njs 存 `njs_vm_t *vm`，QuickJS 存 `JSContext *ctx`。
2. **操作函数指针（vtable）**：`compile`（编译）、`call`（调用函数）、`clone`（克隆）、`external`（取外部对象指针）、`pending`（是否有未完成的异步）、`string`（值转字符串）、`destroy`（销毁）。这就是手工 vtable。
3. **元数据**：`type`（`NGX_ENGINE_NJS=1` 或 `NGX_ENGINE_QJS=2`）、`name`（`"njs"` 或 `"QuickJS"`）、`pool`（njs 内存池）、`precompiled`（QuickJS 预编译字节码数组）、`native_modules`（原生模块清单）、`core_conf`（核心配置）。

`ngx_engine_t` 由 `ngx_create_engine(opts)` 创建。注意它的顺序：**先**从 `opts` 复制 `clone` 回调（在 switch 之前），**再**按 `opts->engine` 进入 `switch` 的两个分支，分别用 `ngx_engine_njs_init` 或 `ngx_engine_qjs_init` 初始化底层句柄，并把对应那一组函数实现填进 vtable：

```
engine->clone = opts->clone;        // 先复制：克隆回调由上层模块提供（见 4.3）

switch (opts->engine) {
case NGX_ENGINE_NJS:
    ngx_engine_njs_init(engine, opts);     // 建 njs_vm_t
    engine->name = "njs";
    engine->compile = ngx_engine_njs_compile;   // 填 njs 版 vtable
    engine->call    = ngx_engine_njs_call;
    engine->external= ngx_engine_njs_external;
    engine->pending = ngx_engine_njs_pending;
    engine->string  = ngx_engine_njs_string;
    engine->destroy = opts->destroy ? opts->destroy : ngx_engine_njs_destroy;
    break;
case NGX_ENGINE_QJS:
    ngx_engine_qjs_init(engine, opts);     // 建 JSContext
    engine->name = "QuickJS";
    engine->compile = ngx_engine_qjs_compile;   // 填 qjs 版 vtable
    ...
    break;
}
```

这里有个关键细节：`clone`（以及可选的 `destroy`）回调**不是**在 `ngx_create_engine` 里写死的，而是从传入的 `opts->clone` / `opts->destroy` 复制过来的。原因是：克隆时需要「绑定 `r` 或 `s` 这种模块专属对象」，而这件事共享层做不到——所以克隆回调必须由**上层模块**提供（见 4.3）。

#### 4.2.3 源码精读

引擎抽象结构体 `ngx_engine_t` 的完整定义，见 [nginx/ngx_js.h:L274-L311](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.h#L274-L311)。注意 `union u` 区分 njs/qjs 句柄、七个函数指针组成 vtable、`type/name/pool/precompiled/core_conf` 等元数据。两个引擎类型常量见 [nginx/ngx_js.h:L25-L26](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.h#L25-L26)（`NGX_ENGINE_NJS=1`、`NGX_ENGINE_QJS=2`）。

引擎工厂 `ngx_create_engine` 的实现，见 [nginx/ngx_js.c:L518-L586](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.c#L518-L586)。其中 `switch (opts->engine)` 按 [nginx/ngx_js.c:L542-L583](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.c#L542-L583) 分流到两个分支并填写各自的 vtable。

njs 引擎的初始化 `ngx_engine_njs_init` 见 [nginx/ngx_js.c:L589-L630](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.c#L589-L630)：它调用 u2-l1 介绍过的 `njs_vm_create` 建出模板 `njs_vm_t`，设置 rejection tracker 与模块加载器，最后把句柄存进 `engine->u.njs.vm`。QuickJS 引擎的初始化 `ngx_engine_qjs_init` 见 [nginx/ngx_js.c:L969-L990](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.c#L969-L990)：它 `JS_NewRuntime()` 后调用 u6-l1 介绍过的 `qjs_new_context()` 建上下文，把句柄存进 `engine->u.qjs.ctx`。

选项结构体 `ngx_engine_opts_t` 见 [nginx/ngx_js.h:L242-L265](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.h#L242-L265)：`engine` 字段选引擎，`union u` 传各自的 metas/addons，末尾的 `clone` / `destroy` 是由上层模块填入的两个回调。

#### 4.2.4 代码实践

1. **实践目标**：看清「同一组操作，两套实现」在源码里如何对仗。
2. **操作步骤**：
   - 在 [nginx/ngx_js.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.c) 中并列定位七对函数：`ngx_engine_njs_compile`↔`ngx_engine_qjs_compile`、`..._call`、`..._external`、`..._pending`、`..._string`、`..._destroy`，以及 `ngx_engine_njs_init`↔`ngx_engine_qjs_init`。
   - 对比 `ngx_engine_njs_pending`（调用 `njs_vm_pending`）与 `ngx_engine_qjs_pending`，看它们各自如何回答「引擎里还有没有未完成的异步作业」。
3. **需要观察的现象**：每对函数签名完全相同（都是 vtable 要求的类型），但内部一个用 `njs_vm_*` API、一个用 `JS_*` API。
4. **预期结果**：能口头复述「上层调用 `ctx->engine->pending(ctx->engine)` 时，不关心底层是哪个引擎，由 vtable 自动分发」。
5. 待本地验证：用 `grep -n "engine->call\|engine->clone\|engine->pending"` 在 `nginx/` 下搜索，确认这些调用点从不区分引擎类型（少数需要区分的，如 dump/periodic，会显式判断 `engine->type == NGX_ENGINE_QJS`）。

### 4.3 per-request 克隆：模板 VM 到请求 VM 的标准流程

#### 4.3.1 概念说明

NGINX 是多进程、每个 worker 单线程事件驱动的服务器。一个 worker 在生命周期里要处理成千上万个请求。如果每个请求都从零创建并编译一个 JS 引擎，开销巨大；但如果所有请求共用一个引擎，又无法隔离各自的 JS 全局变量。

njs 的解决方案是**配置期一个模板、请求期克隆多个副本**（与 u2-l1 的 `njs_vm_clone` 思想一致）：

- **配置期**（nginx 启动、解析 `nginx.conf` 时）：为每个带 `js_import` 的 location/server 创建**一个模板 `ngx_engine_t`**，编译好所有导入的模块，存进 `loc_conf->engine`。这个模板被该 location 下的所有请求共享（只读部分）。
- **请求期**（处理单个请求时）：每个请求克隆出一个**独立的 `ngx_engine_t`**，挂到请求上下文 `ngx_http_js_ctx_t` 上，请求结束销毁。

本节要分清两组容易混淆的函数：

| 角色 | 函数 | 定义位置 | 参数 | 干什么 |
|---|---|---|---|---|
| 共享层克隆（不带 `r`/`s`） | `ngx_njs_clone` / `ngx_qjs_clone` | `ngx_js.c` | 3 个（ctx, cf, external） | 只负责「克隆出独立引擎」这件公共的事 |
| 上层克隆包装（绑定 `r`/`s`） | `ngx_engine_njs_clone` / `ngx_engine_qjs_clone` | 各模块 | 4 个（ctx, cf, proto_id, external） | 先调共享层克隆，再把 `r`/`s` 对象绑到新引擎 |

`opts->clone` 字段填的是**上层包装**（4 参数），所以共享层的 `ngx_engine_t.clone` 也是 4 参数；它内部再去调 3 参数的共享层实现。这样「绑 `r`/`s`」这件模块专属的事就留在了模块里，共享层保持通用。

#### 4.3.2 核心流程

完整的「模板 → 请求副本 → 销毁」时序如下：

```
【配置期：nginx -t / 启动时，每个有 js_import 的 location 触发一次】
  loc_conf 合并
    └─> ngx_js_merge_conf(cf, parent, child, init_vm)          # 共享层合并
          └─> ngx_js_merge_vm(...)                              # 决定是否要建模板
                └─> init_vm(cf, conf) = ngx_http_js_init_conf_vm  # 上层回调
                      ├─> 填 ngx_engine_opts_t (engine/metas/addons/clone)
                      └─> ngx_js_init_conf_vm(cf, conf, &options)  # 共享层建模板
                            ├─> 拼装 import 引导脚本
                            ├─> ngx_create_engine(options) ──> conf->engine  ★ 模板引擎
                            ├─> 注册 pool cleanup: ngx_js_cleanup_vm
                            └─> conf->engine->compile(...) 编译引导脚本

【请求期：一个 HTTP 请求进入 js_content 阶段】
  phase handler
    └─> ngx_http_js_init_vm(r, proto_id)
          ├─> ngx_js_ctx_init(ctx, log)                 # 初始化请求上下文
          └─> jlcf->engine->clone(ctx, jlcf, proto_id, r)   ★ 克隆
                = ngx_engine_njs_clone(ctx, cf, proto_id, r)  # 上层包装
                    ├─> ngx_njs_clone(ctx, cf, r)            # 共享层克隆引擎
                    └─> njs_vm_external_create(...proto_id...) ★ 把 r 绑到新 VM
          └─> 注册 pool cleanup: ngx_http_js_cleanup_ctx

【请求结束】
  ngx_http_js_cleanup_ctx
    └─> ngx_js_ctx_destroy(ctx, conf)
          └─> ctx->engine->destroy(...)   # 销毁请求级引擎副本
```

「模板 vs 副本」的关键在于 `clone` 的实现。两引擎差别很大：

- **`ngx_njs_clone`（内置引擎）**：直接调用 `njs_vm_clone(cf->engine->u.njs.vm, external)`——这正是 u2-l1 讲过的浅拷贝克隆，**复用模板的 shared 与字节码**，只新建私有 runtime、atom 表、levels。然后用模板的 vtable 填充新 `ngx_engine_t`，再 `njs_vm_start` 跑一遍引导脚本（执行那些 `import ...; globalThis.x = x;` 语句）。

- **`ngx_qjs_clone`（QuickJS）**：QuickJS 没有 `clone`，所以走另一条路——新建一个全新的 `JSRuntime` + `qjs_new_context`，然后把模板期**预编译并序列化好的字节码**（`engine->precompiled` 数组）逐条 `JS_ReadObject` 反序列化、`JS_ResolveModule`、`JS_EvalFunction` 重放一遍。此外它还支持**上下文复用**：若 location 配了 `js_context_reuse`，会先从一个 LRU 队列 `reuse_queue` 里弹出一个已用过的 `JSContext` 直接复用，省去重建开销。

#### 4.3.3 源码精读

请求上下文 `ngx_js_ctx_t` 的定义基于公共字段宏 `NGX_JS_COMMON_CTX`，见 [nginx/ngx_js.h:L190-L197](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.h#L190-L197)（含 `engine`、`log`、`args`、`retval`、`rejected_promises`、`waiting_events` 等字段）与结构体 [nginx/ngx_js.h:L232-L234](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.h#L232-L234)。loc_conf 公共字段宏见 [nginx/ngx_js.h:L133-L163](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.h#L133-L163)（含 `type`、`engine`、`imports`、`paths` 等），HTTP 模块的 loc_conf 就是把它展开后再追加 `access/content/...` 字段，见 [nginx/ngx_http_js_module.c:L17-L27](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L17-L27)。

配置期建模板引擎的核心 `ngx_js_init_conf_vm`，见 [nginx/ngx_js.c:L4247-L4328](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.c#L4247-L4328)。其中拼装 `import <name> from '<path>'; globalThis.<name> = <name>;` 引导脚本见 [nginx/ngx_js.c:L4263-L4295](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.c#L4263-L4295)——这就是为什么你在 `nginx.conf` 里写 `js_import test.js;` 后，JS 代码里就能直接用 `test` 这个全局名字。建模板并注册销毁回调见 [nginx/ngx_js.c:L4303-L4316](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.c#L4303-L4316)，配置期的销毁函数 `ngx_js_cleanup_vm` 见 [nginx/ngx_js.c:L4355-L4361](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.c#L4355-L4361)。

请求期克隆的入口 `ngx_http_js_init_vm`，见 [nginx/ngx_http_js_module.c:L2128-L2175](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L2128-L2175)。其中关键的克隆调用 `jlcf->engine->clone(ctx, jlcf, proto_id, r)` 见 [nginx/ngx_http_js_module.c:L2156-L2160](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L2156-L2160)。请求结束清理 `ngx_http_js_cleanup_ctx` 见 [nginx/ngx_http_js_module.c:L2178-L2214](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L2178-L2214)，它最终调共享层 `ngx_js_ctx_destroy`（[nginx/ngx_js.c:L2569-L2573](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.c#L2569-L2573)），后者再调 `ctx->engine->destroy(...)`。

上层克隆包装（绑定 `r`）`ngx_engine_njs_clone` 见 [nginx/ngx_http_js_module.c:L6007-L6031](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L6007-L6031)：先调共享层 `ngx_njs_clone`，再用 `njs_vm_external_create(..., proto_id, ...)` 把 `r` 绑到新 VM。QuickJS 版包装 `ngx_engine_qjs_clone` 见 [nginx/ngx_http_js_module.c:L9423-L9451](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L9423-L9451)：先调 `ngx_qjs_clone`，再注册 `NGX_QJS_CLASS_ID_HTTP_REQUEST` 类并创建 `r` 的原型。

共享层两引擎克隆实现的分工：内置引擎 `ngx_njs_clone` 见 [nginx/ngx_js.c:L752-L790](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.c#L752-L790)，核心是 `njs_vm_clone` 复用模板、`njs_vm_start` 跑引导；QuickJS `ngx_qjs_clone` 见 [nginx/ngx_js.c:L1034-L1147](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.c#L1034-L1147)，核心是 `JS_NewRuntime` + `qjs_new_context` 后重放 `precompiled` 字节码（[nginx/ngx_js.c:L1101-L1124](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.c#L1101-L1124)），并支持从 `reuse_queue` 复用上下文（[nginx/ngx_js.c:L1059-L1067](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.c#L1059-L1067)）。

#### 4.3.4 代码实践

1. **实践目标**：亲手把「模板 → 副本 → 销毁」三个阶段的函数串成一条调用链。
2. **操作步骤**：
   - 从配置期入口出发：读 `ngx_js_init_conf_vm`（[nginx/ngx_js.c:L4247](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.c#L4247)），找到 `ngx_create_engine` 调用与 `compile` 调用。
   - 跳到请求期入口：读 `ngx_http_js_init_vm`（[nginx/ngx_http_js_module.c:L2128](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L2128)），找到 `clone` 调用。
   - 进入上层包装：读 `ngx_engine_njs_clone`（[nginx/ngx_http_js_module.c:L6007](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L6007)），找到对共享层 `ngx_njs_clone` 的调用与 `njs_vm_external_create`。
   - 最后看销毁：读 `ngx_http_js_cleanup_ctx`（[nginx/ngx_http_js_module.c:L2178](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L2178)）→ `ngx_js_ctx_destroy` → `engine->destroy`。
3. **需要观察的现象**：模板引擎（`loc_conf->engine`）只被创建一次，而请求级引擎（`ctx->engine`）在每次 `clone` 时新建、在请求 cleanup 时销毁。
4. **预期结果**：能画出一张时序图，标出「配置期 1 次建模板」与「请求期 N 次克隆+销毁」的对照，并指出 `ngx_njs_clone` 与 `ngx_qjs_clone` 一个走 `njs_vm_clone`、一个走「重建上下文 + 重放字节码」。
5. 待本地验证：参考 [nginx/t/js.t](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/t/js.t) 第 52、59 行的 `js_import test.js;` + `js_content test.njs;`，用 `prove` 跑这条用例，开启 `error_log` 的 `debug` 级别，可以在日志里看到形如 `http js vm clone njs: 0x... from: 0x...` 的输出（对应 [nginx/ngx_http_js_module.c:L2162-L2164](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L2162-L2164) 的 debug 日志）。

#### 4.3.5 小练习与答案

**练习 1**：`ngx_engine_t.clone` 是 4 个参数，但 `ngx_js.c` 里的 `ngx_njs_clone` 只有 3 个参数，这矛盾吗？

**参考答案**：不矛盾。`clone` vtable 指向的是上层模块里的 4 参数包装 `ngx_engine_njs_clone`，它先调共享层 3 参数的 `ngx_njs_clone` 完成克隆，再用第 3 个参数 `proto_id`（描述要绑哪种对象，如 `r`/`s` 的原型 id）执行 `njs_vm_external_create` 把 `r`/`s` 绑上去。「绑对象」是模块专属的，所以这层包装必须在模块里。

**练习 2**：为什么 QuickJS 的克隆比 njs 复杂得多？

**参考答案**：内置引擎 njs 在内核层就提供了 `njs_vm_clone`（浅拷贝 shared + 字节码，见 u2-l1），所以 `ngx_njs_clone` 一行 `njs_vm_clone` 即可。QuickJS 没有等价 API，njs 只能在 `ngx_qjs_clone` 里「新建 runtime/context + 重放模板期预编译并序列化好的字节码（`precompiled` 数组）」来近似克隆；为了降开销，还加了 `reuse_queue` 复用已建好的 `JSContext`。

### 4.4 共享指令处理：js_import / js_engine / js_path

#### 4.4.1 概念说明

NGINX 的指令（如 `js_import`、`js_content`、`js_engine`）解析靠「指令表」驱动：每条指令注册一个解析回调函数。本节关注三条两个模块**完全共用**的指令：

- `js_import`：导入一个 JS 模块（如 `js_import test.js;`）。
- `js_engine`：选引擎（`js_engine njs;` 或 `js_engine qjs;`）。
- `js_path`：追加模块搜索路径。

它们的解析回调（`ngx_js_import`、`ngx_js_engine`）都定义在共享层 `ngx_js.c` 里，因此 HTTP 和 Stream 模块在各自的指令表里**指向同一个回调**，行为天然一致。指令解析的结果（导入名、路径、引擎类型）写入公共字段宏 `NGX_JS_COMMON_LOC_CONF` 定义的 `imports` / `paths` / `type` 等字段——而这些字段两个模块的 loc_conf 都有，所以解析结果能被共享层统一消费。

#### 4.4.2 核心流程

**`js_engine`** 的处理非常简洁：它是一个 bitmask 匹配。HTTP 模块的指令表里挂着一张引擎名→常量的映射表：

```c
static ngx_conf_bitmask_t  ngx_http_js_engines[] = {
    { ngx_string("njs"), NGX_ENGINE_NJS },
    { ngx_string("qjs"), NGX_ENGINE_QJS },
    { ngx_null_string, 0 }
};
```

通用回调 `ngx_js_engine` 拿配置文件里的字符串（如 `"qjs"`）去这张表里查，查到就把对应的常量写进 `conf->type`。注意 HTTP 里写的是小写 `njs`/`qjs`（这与 CLI 的 `njs`/`QuickJS` 不同，见 u6-l4）。`#if (NJS_HAVE_QUICKJS)` 守卫保证未链接 QuickJS 时 `qjs` 选项根本不存在。

**`js_import`** 的处理更值得看：它**不在解析期就加载文件**，而只是把「模块名 + 路径」记录进 `conf->imports` 数组。真正拼成 JS 代码、编译进模板 VM，是延迟到配置合并结束、`ngx_js_init_conf_vm` 里完成的——也就是 4.3 里看到的那段 `import <name> from '<path>'; globalThis.<name> = <name>;` 引导脚本。这种「先收集、后批量编译」的设计，让指令解析与引擎选型解耦。

**`js_path`** 更简单：用 NGINX 自带的 `ngx_conf_set_str_array_slot` 把每个路径追加进 `conf->paths` 数组，不写自定义回调。这些路径在编译期被 `ngx_js_module_loader` 用作模块查找根（njs 引擎）或 `ngx_qjs_module_loader`（QuickJS）。

这三条指令写入的字段，会在配置合并阶段被 `ngx_js_merge_vm` 处理：

- 如果子配置什么都没改、且父配置已有模板引擎，就直接**继承**父级的 `conf->engine`（指针赋值，零开销）。
- 如果子配置有自己的 `js_import`，就把父子的 imports/paths/preload 合并，再为子配置**新建一个模板引擎**。
- 这样实现了 NGINX 期望的「main 里写一次 `js_import`，所有 server/location 继承」的语义。

#### 4.4.3 源码精读

HTTP 模块的引擎名映射表 `ngx_http_js_engines` 见 [nginx/ngx_http_js_module.c:L541-L547](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L541-L547)。指令表 `ngx_http_js_commands` 中 `js_engine` / `js_import` / `js_path` 三条指令的注册，见 [nginx/ngx_http_js_module.c:L561-L610](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L561-L610)：注意 `js_engine` 的回调是 `ngx_js_engine`、`js_import` 的回调是 `ngx_js_import`，两者都是共享层函数（被 stream 模块复用）；`js_path` 用通用 `ngx_conf_set_str_array_slot`。

`js_engine` 解析回调 `ngx_js_engine`（bitmask 查表）见 [nginx/ngx_js.c:L3353-L3390](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.c#L3353-L3390)。`js_import` 解析回调 `ngx_js_import`（只收集名/路径进 `conf->imports`）见 [nginx/ngx_js.c:L3247-L3349](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.c#L3247-L3349)。

配置合并逻辑 `ngx_js_merge_vm` 见 [nginx/ngx_js.c:L3779-L3931](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.c#L3779-L3931)。其中「子配置无改动就继承父模板」的捷径见 [nginx/ngx_js.c:L3801-L3814](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.c#L3801-L3814)，合并完数组后决定是否为子配置建模板的判断见 [nginx/ngx_js.c:L3926-L3930](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.c#L3926-L3930)（有 imports 才调 `init_vm`）。HTTP 模块填充 `ngx_engine_opts_t`（选 metas/addons/clone 回调）的 `ngx_http_js_init_conf_vm` 见 [nginx/ngx_http_js_module.c:L9532-L9562](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L9532-L9562)。

#### 4.4.4 代码实践

1. **实践目标**：理解「指令只收集、合并期才编译」这条设计，并验证两模块共用同一批指令回调。
2. **操作步骤**：
   - 在 [nginx/ngx_stream_js_module.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_stream_js_module.c) 里找到 stream 的指令表，确认它的 `js_engine` / `js_import` 回调也是 `ngx_js_engine` / `ngx_js_import`（与 HTTP 完全相同）。
   - 在 `ngx_js_import`（[nginx/ngx_js.c:L3247](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.c#L3247)）里确认：它**没有**调用任何 `njs_vm_*` 函数，只是 `ngx_array_push(jscf->imports)` 记录名/路径。真正的编译发生在后面的 `ngx_js_init_conf_vm`。
3. **需要观察的现象**：指令解析回调对引擎类型一无所知；`js_engine` 写入的 `conf->type` 一直等到 `ngx_http_js_init_conf_vm` 才被读出来决定建哪种模板。
4. **预期结果**：能解释「为什么 `js_import` 的位置（main/server/location）不影响解析逻辑，但会影响 `ngx_js_merge_vm` 在哪一层创建模板引擎」。
5. 待本地验证：写一个最小 `nginx.conf` 片段，在 `http {}` 顶层放 `js_engine qjs;` 和一条 `js_import test.js;`，在某个 `location {}` 里只写 `js_content test.hello;`。预期能看到该 location **继承** http 层的模板引擎（不重建）；若在 location 里再加一条不同的 `js_import`，则会触发为该 location 新建模板。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `js_import` 不在解析期就读取并编译 JS 文件？

**参考答案**：为了让指令解析与引擎选型解耦。解析期还不知道最终用哪个引擎（`js_engine` 可能写在更靠后的位置或继承自上层），也不知道要不要为这一层建模板（可能直接继承父级引擎）。所以解析期只把名/路径记进数组，等合并结束、`ngx_js_init_conf_vm` 时再按已确定的 `conf->type` 拼引导脚本、统一编译。

**练习 2**：`js_engine` 在 HTTP 里合法值是 `njs` / `qjs`，而 CLI 的 `-n` 合法值是 `njs` / `QuickJS`，大小写还不一样，为什么？

**参考答案**：两套入口各自有独立的「名字 → 枚举」映射表。HTTP 走 `ngx_js_engine` 配合 `ngx_http_js_engines[]`（小写 `njs`/`qjs`，见 [nginx/ngx_http_js_module.c:L541-L547](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L541-L547)），CLI 走自己的 `njs_options_parse`（见 u1-l4）。它们最终都映射到 `NGX_ENGINE_NJS`/`NGX_ENGINE_QJS` 或等价的内部枚举，但对外暴露的名字串是各自约定的——这是 njs 一个需要注意的可移植性细节。

## 5. 综合实践

把本讲三条主线（共享层存在、引擎抽象、克隆时序）串起来，完成下面这个「源码考古」任务：

**任务**：追踪一句 `js_import test.js;` 从配置文件到「请求里能调用 `test.hello()`」的完整旅程，标出每一步落在共享层还是模块层。

**建议步骤**：

1. **指令解析**：`js_import test.js;` 被 `ngx_js_import`（共享层）解析，记录 `{name="test", path="test.js"}` 进 `loc_conf->imports`。
2. **配置合并**：`ngx_js_merge_conf` → `ngx_js_merge_vm`（共享层）决定为这一层建模板，调用 HTTP 模块的 `ngx_http_js_init_conf_vm`（模块层）。
3. **填选项**：该函数按 `conf->type` 填 `ngx_engine_opts_t`（含 `metas`/`addons`/`clone` 回调），再调共享层 `ngx_js_init_conf_vm`。
4. **建模板**：`ngx_js_init_conf_vm` 拼出 `import test from 'test.js'; globalThis.test = test;` 引导脚本，`ngx_create_engine`（共享层）建出模板 `ngx_engine_t`（njs 或 qjs），`compile` 编译引导脚本，存进 `conf->engine`。
5. **请求到来**：`js_content` phase handler → `ngx_http_js_init_vm`（模块层）→ `jlcf->engine->clone(...)` = `ngx_engine_njs_clone`/`ngx_engine_qjs_clone`（模块层包装）。
6. **克隆**：包装调 `ngx_njs_clone`/`ngx_qjs_clone`（共享层）出新引擎，再 `njs_vm_external_create`/注册 `r` 类把请求对象 `r` 绑上去。
7. **执行**：上层用 `engine->call(ctx, "test.hello", ...)`（共享层 vtable 分发到 `ngx_engine_njs_call`/`ngx_engine_qjs_call`）真正执行 JS。
8. **收尾**：请求结束 → `ngx_http_js_cleanup_ctx`（模块层）→ `ngx_js_ctx_destroy`（共享层）→ `engine->destroy`。

**产出**：一张表格，左列是上述 8 步，右列标注「共享层 `ngx_js.c`」还是「模块层 `ngx_http_js_module.c`」，并给出每一步的关键函数永久链接。你会发现：**与引擎打交道的脏活全在共享层，与 `r`/`s` 对象、phase 绑定相关的活全在模块层**——这就是本讲想建立的「共享 vs 专属」分界。

## 6. 本讲小结

- `nginx/ngx_js.c` 是 **http 与 stream 两个 NGINX 模块共用的共享绑定层**，通过 `nginx/config` 把同一批 `NJS_SRCS` 编进两个模块实现复用。
- 引擎抽象 **`ngx_engine_t`** 用 `union` 持底层句柄（`njs_vm_t`/`JSContext`）、用七个函数指针组成手工 vtable，上层只通过指针调用、对引擎类型透明。
- 模板与克隆的标准时序是 **配置期 `ngx_js_init_conf_vm` 建一个模板 → 请求期 `engine->clone` 出独立副本 → 请求结束 `engine->destroy` 销毁**。
- `ngx_njs_clone` 与 `ngx_qjs_clone` 分工不同：**前者一行 `njs_vm_clone` 复用模板，后者新建上下文并重放预编译字节码**（还支持 `reuse_queue` 上下文复用）。
- 「绑 `r`/`s`」是模块专属的，所以真正的克隆回调是模块里的 4 参数包装 `ngx_engine_njs_clone`/`ngx_engine_qjs_clone`，它先调共享层 3 参数克隆、再绑对象。
- `js_import`/`js_engine`/`js_path` 的解析回调（`ngx_js_import`/`ngx_js_engine`）定义在共享层、被两模块共用；指令**只在解析期收集**，真正编译推迟到合并结束的 `ngx_js_init_conf_vm`。

## 7. 下一步学习建议

本讲建立了「共享层 + 引擎抽象 + 克隆时序」的地基，后续两讲会在这地基上往两个方向展开：

- [**u8-l2：ngx_http_js_module**](u8-l2-http-js-module.md)——本讲只说「绑 `r`」，下一讲深入 `r` 对象本身：`js_content`/`js_access`/`js_set` 等指令分别绑到哪个 phase，`r` 暴露了哪些方法（`return`/`send`/`subrequest`/`headersIn`…），以及 phase handler 如何调用 `engine->call`。
- [**u8-l3：ngx_stream_js_module**](u8-l3-stream-js-module.md)——对照 `s` 会话对象与 `js_preread`/`js_filter`，看清 stream 与 http 在共享层之上的差异。

如果对克隆机制还想挖更深，建议回看 [u2-l1（njs_vm_t 生命周期）](u2-l1-vm-lifecycle-api.md) 里 `njs_vm_clone` 如何浅拷贝 `shared` 与字节码、重建私有 runtime——那正是本讲 `ngx_njs_clone` 一行调用背后真正发生的事。
