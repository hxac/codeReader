# QuickJS 引擎包装层 qjs.c

## 1. 本讲目标

本讲是「QuickJS 集成与双引擎架构」单元的起点。读完本讲，你应该能够：

- 说清 `qjs_new_context()` 在上游 QuickJS 的 `JSRuntime`/`JSContext` 之上做了哪几层「njs 定制」（启用哪些 intrinsic、补充哪些全局对象、删掉了什么）。
- 区分两套模块注册结构：njs 内置引擎用的 `njs_module_t{preinit,init}` 与 QuickJS 引擎用的 `qjs_module_t{init}`，并指出 `qjs_modules[]`（构建期固定清单）与 `addons`（嵌入者按需传入）两条注册通道的分工。
- 认识 `QJS_CORE_CLASS_ID_*` 类 id 枚举为什么从 64 开始，以及 `qjs_add_intrinsic_*` 系列如何为 QuickJS 补上 `btoa/atob`、`TextEncoder/TextDecoder`、`njs` 等内建。
- 理解 `quickjs_compat.h` 在「跨 QuickJS / QuickJS-NG 版本可移植」这件事上扮演的角色。

本讲只聚焦 QuickJS 引擎的**包装层**本身；具体的双实现模块（fs/crypto/xml …）约定留到 u6-l2，Buffer 留到 u6-l3。

## 2. 前置知识

本讲假设你已经读过 u2-l1（VM 生命周期 API）和 u1-l4（CLI 与 `-n` 引擎切换）。下面补几个 QuickJS 侧的最小术语，它们在 njs 内置引擎里没有对应物：

| 术语 | 含义 |
|---|---|
| `JSRuntime` | QuickJS 的「运行时」，一个进程级容器，持有 GC、内存分配器、class 注册表等。一个 runtime 可挂多个 context。 |
| `JSContext` | QuickJS 的「执行上下文」，持有全局对象、模块表，是执行 JS 的基本单位。njs 的「一个 VM」≈ 一个 `JSContext`。 |
| `JSValue` | QuickJS 的值类型（带标签的 128 位联合体）。对应 njs 内置引擎的 `njs_value_t`（见 u2-l2）。 |
| `JSModuleDef` | QuickJS 的 ES 模块对象。`import` 一个模块就是拿到它的 `JSModuleDef`。 |
| `JSClass` / class id | QuickJS 用整数 id 标识「带不透明数据 + finalizer/mark 的类」。C 侧靠 `JS_NewClass` + `JS_NewObjectClass(id)` 造实例，靠 `JS_GetOpaque(val, id)` 取回 C 指针。 |
| intrinsic | QuickJS 的「可内建」开关。`JS_NewContextRaw()` 造出的是**空壳**上下文，必须逐个调用 `JS_AddIntrinsic*()` 才会有 `Date`、`RegExp`、`Promise`、`Map/Set` 等。 |

一句话：njs 没有自己重新实现 QuickJS，而是**链接上游 QuickJS 库**（见 u1-l3 的 `auto/quickjs` 探测），再用 `src/qjs.c`、`src/qjs.h`、`src/quickjs_compat.h` 这一组文件在它之上「裁剪 + 补丁 + 注册模块」，做出一个 njs 想要的执行环境。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/qjs.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.c) | **包装层主体**。`qjs_new_context`、四个 `qjs_add_intrinsic_*`、`process` 对象、`TextEncoder/Decoder` 实现、各种 buffer/字符串/base64 工具。 |
| [src/qjs.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.h) | 包装层公共头。定义 `QJS_CORE_CLASS_ID_*` 枚举、`qjs_module_t`/`qjs_addon_init_pt`、`qjs_new_context` 声明，以及一组跨版本兼容宏。 |
| [src/quickjs_compat.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/quickjs_compat.h) | **移植胶水**。负责安全地 `#include <quickjs.h>` 并补齐缺失的宏。 |
| [auto/qjs_modules](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/qjs_modules) | QuickJS 扩展模块清单，决定哪些 `qjs_*` 模块被编入。 |
| [auto/make](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/make) | 构建期生成 `build/qjs_modules.c`，即 `qjs_modules[]` 数组的真正定义处。 |
| [external/qjs_fs_module.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/qjs_fs_module.c) | 典型的 `qjs_module_t` 注册结构样板，用于看 `init` 回调长什么样。 |
| [external/njs_shell.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c) | CLI 里调用 `qjs_new_context` 的地方（`njs_engine_qjs_init`）。 |
| [nginx/ngx_js.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.c) | NGINX 集成层调用 `qjs_new_context` 的地方，以及传 `addons` 的地方。 |

---

## 4. 核心概念与源码讲解

本讲拆三个最小模块：**上下文创建**、**模块注册类型**、**intrinsic 补充与裁剪**。

### 4.1 上下文创建：从 JSRuntime 到 njs 定制 JSContext

#### 4.1.1 概念说明

QuickJS 把「造上下文」拆成两步：

1. `JS_NewContextRaw(rt)` 造一个**空壳**上下文——除了语法，什么内建都没有（没有 `Date`、`RegExp`、`JSON`、`Promise`、`Map`/`Set`、`TypedArray` …）。
2. 宿主按需调用一组 `JS_AddIntrinsic*()`，像点菜一样决定「这个上下文支持哪些语言特性」。

这是 QuickJS 为嵌入场景设计的「按需付费」机制：一个只想跑极简脚本的嵌入式设备可以不开 `Date`/`RegExp`，省内存。njs 借这个机制做了一层封装 `qjs_new_context(rt, addons)`，**统一替 njs 把该点的菜点好、再补几个 QuickJS 没有但 njs 需要的全局对象**。这样无论 CLI 还是 NGINX，创建 QuickJS 上下文都只需调同一个函数，行为一致。

#### 4.1.2 核心流程

`qjs_new_context` 的执行顺序可以分成四段：

```text
qjs_new_context(rt, addons)
  │
  ├─① 建空壳 + 点 intrinsic ─────► JS_NewContextRaw(rt)
  │                                JS_AddIntrinsic{BaseObjects,Date,RegExp,
  │                                  JSON,Proxy,MapSet,TypedArrays,Promise,
  │                                  [BigInt],Eval}
  │
  ├─② 注册内置模块 qjs_modules[] ─► 逐个调 init(ctx, name) → JSModuleDef*
  │   注册附加模块 addons         ─► 逐个调 init(ctx, name) → JSModuleDef*
  │
  ├─③ 补 njs 自己的全局对象 ──────► qjs_add_intrinsic_njs           (全局 njs)
  │                                qjs_add_intrinsic_text_decoder  (TextDecoder)
  │                                qjs_add_intrinsic_text_encoder  (TextEncoder)
  │                                qjs_add_intrinsic_btoa_atob     (btoa/atob)
  │
  └─④ 安全裁剪 ──────────────────► 从 global 删除 eval、Function
                                    返回 ctx
```

注意先后顺序很关键：①先开 intrinsic（②③④都依赖全局对象已具备基础内建）；②再注册模块（模块的 `init` 会注册自己的 `JSClass`，需要 runtime 已经就绪）；③④操作全局对象，必须在 ①②之后。

#### 4.1.3 源码精读

整个函数只有 80 多行，先看它的整体骨架：

[qjs.c:231-256](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.c#L231-L256) —— **建空壳 + 点 intrinsic**。注意第一行是 `JS_NewContextRaw`（raw=空壳），随后一连串 `JS_AddIntrinsic*` 才是「点菜」。`BigInt` 用宏 `NJS_HAVE_QUICKJS_ADD_INTRINSIC_BIGINT` 包起来，因为这个 intrinsic 是较新版 QuickJS 才有的（见 4.3 的兼容性讨论）。

[qjs.c:258-270](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.c#L258-L270) —— **两条模块注册通道**。先遍历全局的 `qjs_modules[]`，再遍历调用者传入的 `addons`，两者结构相同（都是 `qjs_module_t*` 数组、`NULL` 结尾），都调用 `(*module)->init(ctx, (*module)->name)`。它们的区别见 4.2。

[qjs.c:272-288](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.c#L272-L288) —— **补充 njs 专属全局对象**。取到 `global_obj` 后依次挂上 `njs`、`TextDecoder`、`TextEncoder`、`btoa/atob`。这四个就是 4.3 要展开的内容。

[qjs.c:290-310](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.c#L290-L310) —— **安全裁剪**。这里是个看似矛盾的设计：上一段刚 `JS_AddIntrinsicEval(ctx)`（①里），这里却把全局的 `eval` 删掉；同时还删了 `Function` 构造器。原因见下面的解释。

> **为什么先加 `Eval` intrinsic 又删全局 `eval`？**
> `JS_AddIntrinsicEval` 不仅提供全局 `eval`，还开启 QuickJS 内部「把字符串编译成字节码」的能力（`JS_Eval` 函数）。njs 自己需要这个能力（NGINX 集成层 `ngx_engine_qjs_compile` 要用 `JS_Eval` 把用户脚本预编译成字节码，见 [nginx/ngx_js.c:1005-1006](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.c#L1005-L1006)），但不想把 `eval`/`Function` 这种「运行期执行任意字符串」的能力暴露给用户脚本。于是：开 intrinsic 拿到 C 侧的 `JS_Eval`，同时从全局对象上删掉 JS 侧的 `eval` 和 `Function`，达到「内核能用、脚本不能用」的效果。

最后看它在两个真实场景里是怎么被调的：

[njs_shell.c:2720-2730](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L2720-L2730) —— **CLI 调用**：`JS_NewRuntime()` 之后直接 `qjs_new_context(rt, NULL)`，第二个参数（addons）传 `NULL`，因为 CLI 不需要 `r`/`s` 这些 NGINX 对象。

[nginx/ngx_js.c:969-990](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.c#L969-L990) —— **NGINX 配置期调用**：`qjs_new_context(rt, opts->u.qjs.addons)`，这次传了 `addons`，里面装着把 `r`/`s`、`ngx`、`ngx.shared`、`ngx.fetch` 等注册成 ES 模块的那些 `qjs_module_t`（见 4.2）。

#### 4.1.4 代码实践

**实践目标：** 用 `qjs_new_context` 的源码，画出 QuickJS 上下文从「空壳」到「njs 可用」的状态变化。

**操作步骤：**

1. 打开 [src/qjs.c:231-315](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.c#L231-L315)，逐段标注：哪些是 QuickJS 原生能力（`JS_*`）、哪些是 njs 自己加的（`qjs_*`）。
2. 对照 [nginx/ngx_js.c:979](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.c#L979) 与 [njs_shell.c:2726](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L2726)，写出两个调用点的第二个参数差异。
3. 回答：如果删掉 [qjs.c:290-310](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.c#L290-L310) 这段「删除 eval/Function」的代码，用户脚本会发生什么变化？

**预期结果：** 第一段全是 `JS_*` 前缀；第三段全是 `qjs_*` 前缀。CLI 传 `NULL`、NGINX 传 `opts->u.qjs.addons`。第三问：用户脚本里将能直接调用 `eval("...")` 和 `new Function("...")`，破坏 njs 的「不执行任意字符串」安全约束。

#### 4.1.5 小练习与答案

**练习 1：** `qjs_new_context` 为什么要用 `JS_NewContextRaw` 而不是 `JS_NewContext`？

**参考答案：** `JS_NewContext`（非 raw）会一次性开齐所有常用 intrinsic，剥夺了宿主「按需点菜」的权力。njs 想自己控制开哪些（比如 `BigInt` 要看 QuickJS 版本，见 4.3），所以用 `JS_NewContextRaw` 拿空壳，再逐个 `JS_AddIntrinsic*`。

**练习 2：** 函数里有两处 `for (module = ...; *module != NULL; module++)` 循环（[qjs.c:258](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.c#L258) 和 [qjs.c:265](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.c#L265)），它们分别遍历什么？

**参考答案：** 第一个遍历全局内置的 `qjs_modules[]`（fs/query_string/buffer 等通用模块，构建期固定）；第二个遍历调用者传入的 `addons`（NGINX 用来注册 `r`/`s`/`ngx` 等宿主模块）。两者结构一致但来源不同，详见 4.2。

---

### 4.2 模块注册类型：qjs_module_t、qjs_modules[] 与 addons

#### 4.2.1 概念说明

QuickJS 的扩展以 **ES 模块**形式存在：一个 C 函数负责「按名字注册一个模块」，返回 `JSModuleDef *`。njs 把这个回调的类型起名为 `qjs_addon_init_pt`，再把「名字 + 回调」打包成 `qjs_module_t`：

[qjs.h:44-49](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.h#L44-L49) —— `init` 的签名是 `JSModuleDef *(*)(JSContext *ctx, const char *name)`。

这是它与 njs 内置引擎模块注册的最大区别：

| 维度 | njs 内置引擎（见 u5-l3 / u6-l2） | QuickJS 引擎（本讲） |
|---|---|---|
| 注册结构 | `njs_module_t` | `qjs_module_t` |
| 回调阶段 | `preinit` + `init` 两阶段 | 只有 `init` 一个阶段 |
| `init` 返回 | `njs_int_t`（状态码） | `JSModuleDef *`（一个 ES 模块对象） |
| 值类型 | `njs_value_t` / `njs_vm_t` | `JSValue` / `JSContext` |
| 模块形态 | 在 `vm` 上挂属性/原型 | 注册成可被 `import` 的 ES 模块 |

为什么 QuickJS 侧只有一个 `init`？因为 QuickJS 的 ES 模块机制本身就把「声明导出」和「填充导出」合成了一步（`JS_NewCModule` 时传一个初始化回调，模块首次被 import 时再调它），不需要像 njs 内置引擎那样分 preinit/init。

#### 4.2.2 核心流程

一个 `qjs_module_t` 的「一生」：

```text
构建期 (auto/make)
   auto/qjs_modules 声明 qjs_buffer_module / qjs_fs_module / ...
        │  . auto/qjs_module  → 追加进 QJS_LIB_MODULES
        ▼
   auto/make 读 QJS_LIB_MODULES → 生成 build/qjs_modules.c
        │   里面定义: qjs_module_t *qjs_modules[] = { &qjs_buffer_module, ..., NULL };
        ▼
运行期 (qjs_new_context)
   for (module = qjs_modules; *module; module++)
        (*module)->init(ctx, (*module)->name)   ──►  JS_NewCModule(ctx, name, ...)
                                                  ──►  返回 JSModuleDef*
        │
        ▼
   用户脚本里  import fs from 'fs'   ──►  QuickJS 按名字找到这个 JSModuleDef
```

关键点：`qjs_modules[]` 不是手写的，是**构建期生成**的；而 `addons` 是**运行期由嵌入者传入**的。两者在 `qjs_new_context` 里被同等对待（同样的 `init` 调用），但来源完全不同。

#### 4.2.3 源码精读

**① 典型的 `qjs_module_t` 长什么样**——以 fs 模块为例：

[qjs_fs_module.c:362-365](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/qjs_fs_module.c#L362-L365) —— 结构体只有两个字段：模块名 `"fs"` 和初始化函数 `qjs_fs_init`。

[qjs_fs_module.c:2957-3013](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/qjs_fs_module.c#L2957-L3013) —— `qjs_fs_init` 干三件事：(a) 注册自己用到的 `QJS_CORE_CLASS_ID_FS_STATS` 等 JSClass（先检查 `JS_IsRegisteredClass` 避免重复注册）；(b) 用 `JS_NewCModule(cx, name, qjs_fs_module_init)` 创建模块；(c) 返回 `JSModuleDef *`。注意它**返回的是模块对象**，而不是状态码——这就是 `qjs_addon_init_pt` 的契约。

**② `qjs_modules[]` 是构建期生成的**——它是 `extern` 声明在头里、定义在生成文件里：

[qjs.h:194](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.h#L194) —— `extern qjs_module_t *qjs_modules[];`，真正的数组定义在 `build/qjs_modules.c`。

[auto/qjs_modules](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/qjs_modules) —— 这是清单本身。它逐个设置 `njs_module_name=qjs_xxx_module` + `njs_module_srcs=...`，然后 `. auto/qjs_module` 把名字追加进 `QJS_LIB_MODULES`。注意几处 `if [ $NJS_OPENSSL = YES ... ]` / `if [ $NJS_LIBXML2 ... ]`：可选依赖（crypto/webcrypto/xml/zlib）只有探测到库才进清单。

[auto/make:43-70](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/make#L43-L70) —— 这段 shell 就是「生成器」：先 `echo 'qjs_module_t *qjs_modules[] = {'`，再对 `$QJS_LIB_MODULES` 里每个模块 `echo "    &$mod,"`，最后 `echo 'NULL };`。所以 `qjs_modules[]` 的内容完全由 `auto/qjs_modules` 决定，改一份就够。

**③ `addons` 通道：嵌入者按需传入**——NGINX 用它注册宿主对象：

[nginx/ngx_http_js_module.c:1518-1521](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L1518-L1521) —— `njs_http_qjs_addon_modules[]` 里展开宏 `NGX_JS_QJS_ADDON_MODULES`。

[nginx/ngx_js_modules.h:77-83](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_modules.h#L77-L83) —— 这个宏列出 `ngx_qjs_ngx_module`（即 `r` 对象）、`ngx_qjs_ngx_shared_dict_module`（`ngx.shared`）、`ngx_qjs_ngx_fetch_module`（`ngx.fetch`）等。这些模块只有 NGINX 集成才有意义，所以走 `addons` 而不是全局 `qjs_modules[]`；CLI 不需要它们，于是 [njs_shell.c:2726](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L2726) 传 `NULL`。

#### 4.2.4 代码实践

**实践目标：** 验证「改一处 fs 行为，注册链路上有哪些文件要同步」这个 u6-l2 会反复强调的主题，先在注册层落地。

**操作步骤：**

1. 假设想新增一个 QuickJS 模块 `qjs_foo_module`（源文件 `external/qjs_foo_module.c`），先在文件里照 [qjs_fs_module.c:362-365](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/qjs_fs_module.c#L362-L365) 写一个 `qjs_module_t qjs_foo_module = { .name="foo", .init=qjs_foo_init };`。
2. 在 [auto/qjs_modules](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/qjs_modules) 末尾加一段：
   ```sh
   njs_module_name=qjs_foo_module
   njs_module_incs=
   njs_module_srcs=external/qjs_foo_module.c
   . auto/qjs_module
   ```
3. 重新 `./configure && make njs`，运行 `./build/njs -e 'import foo from "foo"'`（`-e` 详见 u1-l4）观察效果（**待本地验证**，因为本练习不会真的创建源文件）。

**预期结果：** 重新构建后，`build/qjs_modules.c` 里会自动多出 `&qjs_foo_module,` 一行，`qjs_new_context` 启动时就会调用 `qjs_foo_init`，从而 `import foo from "foo"` 可用。这印证了「加模块 = 加源文件 + 在清单里登记」的约定。

#### 4.2.5 小练习与答案

**练习 1：** 为什么 `r`/`s` 这些 NGINX 对象走 `addons` 通道，而 `fs` 走全局 `qjs_modules[]`？

**参考答案：** `fs` 这类通用模块 CLI 和 NGINX 都要用，没有宿主依赖，放进全局清单一次注册即可；`r`/`s` 是 NGINX 请求/会话对象，CLI 里没有请求概念，强行注册会引用不存在的 C 符号。所以按「是否依赖宿主」分流到两个通道，CLI 传 `NULL` 就自然屏蔽了宿主模块。

**练习 2：** `qjs_module_t.init` 和 njs 内置引擎 `njs_module_t` 的回调返回值有什么本质不同？

**参考答案：** `qjs_module_t.init` 返回 `JSModuleDef *`（一个 ES 模块对象，失败返回 `NULL`），意味着 QuickJS 侧扩展天然就是「可被 `import` 的 ES 模块」；`njs_module_t` 的 init 返回的是 `njs_int_t` 状态码，模块以挂属性/原型的方式存在 VM 上。前者是模块系统的一等公民，后者更像「往全局对象上贴方法」。

---

### 4.3 intrinsic 补充与裁剪：btoa/atob、TextEncoder、njs 与兼容层

#### 4.3.1 概念说明

`qjs_add_intrinsic_*` 是 njs 自己起的函数名前缀（**不是** QuickJS 的 API），含义是「像 QuickJS 的 `JS_AddIntrinsic*` 一样，往全局补一个内建」。它解决两类问题：

1. **QuickJS 没有但 Web/Node 环境有的全局**：`btoa`/`atob`、`TextEncoder`/`TextDecoder`。这些在浏览器和 Node 里是标配，QuickJS 核心不提供，njs 出于兼容性要补上。
2. **njs 自身的运行时信息**：全局 `njs` 对象（`njs.version`、`njs.engine`、`njs.on('exit', ...)`）。

此外还有「反向操作」——裁剪掉不希望用户脚本碰的全局（`eval`、`Function`），在 4.1.3 已讲。

> **关于 `process` 对象的澄清（重要陷阱）：**
> 实践任务里提到「process」，但要准确地说：`process` **不是**在 `qjs_new_context` 里挂上去的。`qjs.c` 只**提供**了构造它的工具函数 `qjs_process_object()`（[qjs.c:625-664](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.c#L625-L664)）和原型表 `qjs_process_proto`（[qjs.c:146-152](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.c#L146-L152)），真正把它挂到全局对象上的是**嵌入者**：CLI 在 [njs_shell.c:1940](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L1940) 调 `qjs_process_object(ctx, argc, argv)`，NGINX 在 [nginx/ngx_js.c:1683](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.c#L1683) 调同样函数。所以 `process` 是「由 qjs.c 供给、由宿主挂载」，与 `btoa`/`njs` 这种「qjs_new_context 内部直接挂载」不同。

#### 4.3.2 核心流程

四种「补充型」intrinsic 的挂载套路高度一致：

```text
qjs_add_intrinsic_<name>(ctx, global)
   ├─ （若需要存 C 状态）JS_NewClass(runtime, QJS_CORE_CLASS_ID_<NAME>, &class_def)
   │                     JS_SetClassProto(ctx, id, proto)   // proto 上贴方法/属性
   ├─ ctor = JS_NewCFunction2(...)  或  JS_NewCFunction(...)
   └─ JS_SetPropertyStr(ctx, global, "<Name>", ctor)       // 挂到全局
```

而 `btoa/atob` 因为是无状态的纯函数，连 `JSClass` 都不需要，直接 `JS_NewCFunction` 后挂全局即可。

类 id 的分配集中在 [qjs.h:25-41](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.h#L25-L41) 的 `QJS_CORE_CLASS_ID_*` 枚举，**从 64 开始**。原因：QuickJS 自己的内建类占用 1..63 这段 id（`JS_CLASS_OBJECT=1`、`JS_CLASS_ARRAY`、`JS_CLASS_NUMBER` …），njs 必须从 64 起避免冲突。

#### 4.3.3 源码精读

**① 类 id 枚举**

[qjs.h:25-41](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.h#L25-L41) —— 从 `QJS_CORE_CLASS_ID_BUFFER = 64` 开始连续递增，覆盖 Buffer、TextDecoder/Encoder、njs、fs 的 Stats/Dirent/FileHandle、WebCryptoKey、Crypto Hash/HMAC、xml 的 Doc/Node/Attr。这些 id 是跨模块共享的全局命名空间，所以集中定义在一个头里；任何一个 `qjs_*_module.c`（如 fs 的 `qjs_fs_init`）要用自己的类时，先 `JS_IsRegisteredClass` 检查再 `JS_NewClass`，避免重复注册。

**② btoa/atob（HEAD 提交刚刚补上的）**

[qjs.c:168-188](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.c#L168-L188) —— `qjs_add_intrinsic_btoa_atob` 把两个 C 函数 `qjs_global_btoa`/`qjs_global_atob` 包成 `JS_NewCFunction` 挂到全局。这正是当前 HEAD 提交 `f078f143 QuickJS: add missing btoa() and atob()` 做的事——QuickJS 核心此前没有这两个全局，njs 在包装层补齐。

[qjs.c:191-208](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.c#L191-L208) —— `qjs_global_btoa` 的实现：把 JS 字符串取出成 `njs_str_t`，调 `qjs_string_btoa` 做 Base64 编码。底层编码函数（`qjs_base64_encode` 等）在 qjs.h 里成组声明，供 Buffer、crypto 等多处复用。

**③ `njs` 全局对象（带不透明状态 + GC）**

[qjs.c:357-388](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.c#L357-L388) —— `qjs_add_intrinsic_njs` 是「带状态 intrinsic」的样板：先 `JS_NewClass` 注册 `QJS_CORE_CLASS_ID_NJS`（带 `finalizer` 和 `gc_mark`，因为 `njs.on('exit', cb)` 要持有一个 JS 回调，必须让 GC 知道它的存在，见 [qjs.c:155-159](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.c#L155-L159)），再 `JS_SetClassProto` 贴上原型表 `qjs_njs_proto`，最后造一个单例对象挂到全局 `njs`。

[qjs.c:137-144](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.c#L137-L144) —— 原型表 `qjs_njs_proto`：`version`（编译期 `NJS_VERSION` 字符串）、`version_number`、`engine`（硬编码 `"QuickJS"`，这正是区分两引擎的运行期标志）、`on` 方法（注册 exit 回调）。exit 回调由 [qjs.c:318-354](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.c#L318-L354) 的 `qjs_call_exit_hook` 在 VM 销毁前触发。

**④ TextDecoder/TextEncoder**

[qjs.c:773-803](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.c#L773-L803) —— `qjs_add_intrinsic_text_decoder` 用 `JS_NewCFunction2(..., JS_CFUNC_constructor, ...)` 造一个可 `new` 的构造器，再 `JS_SetConstructor` 把它和原型绑起来，挂到全局 `TextDecoder`。它的不透明数据 `qjs_text_decoder_t`（[qjs.c:33-39](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.c#L33-L39)）持有解码状态，所以也有 `finalizer` 释放。TextEncoder（[qjs.c:951-975](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.c#L951-L975)）套路相同但无状态。

**⑤ quickjs_compat.h：跨版本兼容**

[quickjs_compat.h:6-24](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/quickjs_compat.h#L6-L24) —— 它的第一个职责：在 GCC≥8 / clang 下，`#include <quickjs.h>` 会触发 `-Wcast-function-type` 警告（QuickJS 头里有函数指针强转），njs 用 `#pragma GCC diagnostic ignored` 把这段包含包起来静默警告；第二个职责：若上游没定义 `JS_BOOL`，就 `#define JS_BOOL bool`。

兼容工作其实分两层，另一层在 qjs.h 末尾的一组宏里，把「同一件事在不同 QuickJS 版本下名字/参数不同」抹平：

[qjs.h:170-192](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.h#L170-L192) —— 一组 `NJS_HAVE_QUICKJS_*` 条件宏：`JS_IsSameValue`（新版）vs `JS_SameValue`（旧版）、`JS_IsArray` 单参 vs 双参、`JS_IsError` 单参 vs 双参、`qjs_new_error2`（处理 QuickJS-NG 过早贴 `stack` 的问题，见头文件 L143-L168 注释）vs `JS_NewError`。这些 `NJS_HAVE_QUICKJS_*` 宏由 configure 在 [auto/quickjs](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/quickjs) 探测生成。这就是 `quickjs_compat.h` + 这组宏共同的意义：**让同一份 `qjs_*.c` 代码能在上游 Bellard QuickJS 和 QuickJS-NG 两个分支上都编译通过**。

#### 4.3.4 代码实践

**实践目标：** 用 CLI 实地验证 `qjs_new_context` 挂上/删掉了哪些全局，把源码阅读变成可观察的行为。

**操作步骤：**

1. 按 u1-l3 构建 `build/njs`（需要链接了 QuickJS，即 `auto/quickjs` 探测成功）。
2. 依次运行下面几条（`-n QuickJS` 切到 QuickJS 引擎，详见 u1-l4）：

   ```sh
   ./build/njs -n QuickJS -e 'console.log(typeof TextDecoder, typeof TextEncoder)'
   ./build/njs -n QuickJS -e 'console.log(typeof btoa, typeof atob)'
   ./build/njs -n QuickJS -e 'console.log(njs.version, njs.engine)'
   ./build/njs -n QuickJS -e 'console.log(typeof eval, typeof Function)'
   ./build/njs -n QuickJS -e 'console.log(typeof process)'
   ./build/njs -n QuickJS -e 'console.log(btoa("hello"))'
   ```

3. 对照源码解释每条输出。

**需要观察的现象与预期结果：**

| 命令 | 预期输出 | 对应源码 |
|---|---|---|
| TextDecoder/Encoder | `function function` | [qjs.c:278-284](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.c#L278-L284) |
| btoa/atob | `function function` | [qjs.c:286-288](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.c#L286-L288)（HEAD 新增） |
| njs.version/engine | 版本号 + `QuickJS` | [qjs.c:139-142](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.c#L139-L142) |
| eval/Function | `undefined undefined` | [qjs.c:290-310](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.c#L290-L310) 被删 |
| process | `object` | 由 [njs_shell.c:1940](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L1940) 挂载（非 qjs_new_context） |
| `btoa("hello")` | `aGVsbG8=` | Base64 编码 |

> 若未本地构建 QuickJS，以上输出均为「待本地验证」。`process` 一行尤其重要：它说明 `process` 不是 `qjs_new_context` 的产物，而是 CLI 后续挂上的——这印证了 4.3.1 的陷阱说明。

**关于 `quickjs_compat.h` 作用的实践（阅读型）：** 打开 [quickjs_compat.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/quickjs_compat.h)，回答：如果删掉这个文件、直接在 qjs.h 里 `#include <quickjs.h>`，在 GCC≥8 下 `make` 会出现什么？答：QuickJS 头里的函数指针强转会触发 `-Wcast-function-type`，而 njs 默认 `-Werror`（见 docs/agent/engine-dev.md），编译会失败。这就是 compat 头存在的直接理由。

#### 4.3.5 小练习与答案

**练习 1：** `QJS_CORE_CLASS_ID_*` 为什么从 64 开始，而不是 0 或 1？

**参考答案：** QuickJS 内核自己的内建类占用 1..63（`JS_CLASS_OBJECT`=1 等），用户自定义类必须从 64 起。从 0/1 起会和 QuickJS 内建类 id 冲突，导致 `JS_GetOpaque(val, id)` 取错对象的 C 指针。

**练习 2：** `njs.engine` 在 QuickJS 引擎下返回什么？这个值是哪里写死的？它在双引擎代码里有什么用？

**参考答案：** 返回字符串 `"QuickJS"`，写死在 [qjs.c:142](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.c#L142) 的 `qjs_njs_proto` 表里。它是运行期区分当前用的是哪个引擎的唯一可靠标志——脚本可据此做能力分支（例如 `if (njs.engine === 'QuickJS') { /* 用 class */ }`），这也是 u6-l4「引擎选择」会用到的一个关键钩子。

**练习 3：** `qjs_add_intrinsic_njs` 为什么要给 `njs` 类注册 `gc_mark` 回调（[qjs.c:391-403](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.c#L391-L403)），而 `btoa`/`atob` 完全不需要？

**参考答案：** `njs.on('exit', cb)` 会把一个 JS 函数 `cb` 存进 `qjs_njs_t.hooks[]`，这是一条「C 结构体持有 JS 值」的 GC 根。若不告诉 GC 去标记它，GC 可能在 `cb` 仍被使用时就回收它，导致 use-after-free。`gc_mark` 就是显式告诉 GC「我还持有这些值，别回收」。`btoa`/`atob` 是无状态纯函数，不持有任何 JS 值，自然不需要。

---

## 5. 综合实践

**任务：** 假设你要给 QuickJS 引擎新增一个全局函数 `njsHash(s)`，它用 `qjs_string_hex` 把字符串编成 hex 摘要（仅用于练习包装层流程，不是真加密）。请只阅读源码、不实际改代码，回答下列设计问题，把本讲三个模块串起来：

1. **它该放在哪？** 这个函数无状态、纯函数，参考 [qjs.c:168-188](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.c#L168-L188) 的 `qjs_add_intrinsic_btoa_atob`，写出你设计的 `qjs_add_intrinsic_hash` 函数骨架（只需说明：是否要 `JS_NewClass`、怎么造 `JSValue func`、怎么挂全局）。
2. **它何时被调用？** 参照 [qjs.c:272-288](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.c#L272-L288)，写出在 `qjs_new_context` 里加一行调用的位置，并说明为什么必须放在模块注册（[qjs.c:258-270](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.c#L258-L270)）之后。
3. **CLI 和 NGINX 都会自动有它吗？** 结合 4.1.3 两个调用点（[njs_shell.c:2726](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L2726) 与 [nginx/ngx_js.c:979](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.c#L979)）回答，并指出这和把功能做成 `qjs_module_t` 走 `qjs_modules[]`/`addons`（4.2）有什么不同。
4. **可移植性：** 你的函数用了 `JS_NewCFunction`、`JS_SetPropertyStr` 这些 API，需要担心 `quickjs_compat.h`（[quickjs_compat.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/quickjs_compat.h)）吗？为什么？

**参考要点：**

1. 不需要 `JS_NewClass`（无状态）。骨架：`func = JS_NewCFunction(cx, qjs_global_hash, "njsHash", 1); JS_SetPropertyStr(cx, global, "njsHash", func);`，内部把字符串取成 `njs_str_t` 后调 `qjs_string_hex`。
2. 放在 [qjs.c:286-288](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.c#L286-L288) 那一组 `qjs_add_intrinsic_*` 之后即可。必须在模块注册之后，因为某些模块的 `init` 可能依赖全局对象已就绪；更重要的是必须先 `JS_GetGlobalObject` 之后再操作全局，而那一步在 [qjs.c:272](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.c#L272)。
3. 会。因为写在 `qjs_new_context` 里，而 CLI 和 NGINX 都走这一个函数，所以两处自动都有 `njsHash`。这与做成 `qjs_module_t` 不同：模块走 `import` 才能用，且可选模块要登记进 `auto/qjs_modules` 并受 `NJS_OPENSSL` 等条件控制；直接挂全局则无需 import、不受可选依赖开关影响。
4. 基本不用。`JS_NewCFunction`/`JS_SetPropertyStr` 是 QuickJS 稳定核心 API，签名长期不变；`quickjs_compat.h` 与那组 `NJS_HAVE_QUICKJS_*` 宏主要处理的是少数签名有差异的 API（`JS_IsSameValue`/`JS_IsArray`/`JS_IsError`/`JS_NewError`）和编译警告。除非你用了这些易变 API，否则不需要额外兼容处理。

---

## 6. 本讲小结

- `qjs_new_context(rt, addons)` 是 njs 包装 QuickJS 的唯一入口：用 `JS_NewContextRaw` 建空壳，逐个 `JS_AddIntrinsic*` 点齐语言特性，再注册模块、补全局对象、删危险全局。
- 模块注册有两条对等通道：构建期生成的全局清单 `qjs_modules[]`（fs/query_string/buffer 等通用模块）和运行期由嵌入者传入的 `addons`（NGINX 的 `r`/`s`/`ngx` 等宿主模块）；CLI 传 `NULL` 屏蔽宿主模块。
- `qjs_module_t{name, init}` 的 `init` 返回 `JSModuleDef *`，意味着 QuickJS 侧扩展天然是「可 `import` 的 ES 模块」；这和 njs 内置引擎 `njs_module_t{preinit, init}`（返回状态码、挂原型）的形态本质不同。
- `QJS_CORE_CLASS_ID_*` 集中定义、从 64 起编（避开 QuickJS 内建类 1..63），供 Buffer/TextDecoder/fs/xml 等跨模块共享。
- `qjs_add_intrinsic_*` 系列替 QuickJS 补上 `btoa/atob`（HEAD 刚补）、`TextEncoder/TextDecoder`、`njs` 等全局；同时反向删掉 `eval`/`Function` 做安全裁剪。`process` 不在其中，而是由 qjs.c 供给、宿主（CLI/NGINX）挂载。
- `quickjs_compat.h` + qjs.h 末尾的 `NJS_HAVE_QUICKJS_*` 宏共同让一份 `qjs_*.c` 代码能在 Bellard QuickJS 与 QuickJS-NG 上都编译通过。

## 7. 下一步学习建议

- **u6-l2 双引擎模块模式**：本讲只看了 `qjs_module_t` 这一侧。下一讲对照 `external/njs_fs_module.c` 与 `external/qjs_fs_module.c`，看「一个功能两份实现」的完整约定，以及 `auto/modules` 与 `auto/qjs_modules` 两份清单如何分别汇总。
- **u6-l3 Buffer 与 TypedArray**：本讲的 `QJS_CORE_CLASS_ID_BUFFER`/`UINT8_ARRAY_CTOR` 在 `src/qjs_buffer.c` 里如何展开成完整的 Buffer 实现。
- **动手验证**：先按 u1-l3 构建带 QuickJS 的 `build/njs`，跑一遍 4.3.4 的命令表，再回头读 `qjs_new_context` 会更有体感。
- **延伸阅读**：[docs/agent/js-dev.md](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/js-dev.md) 里关于两引擎语言能力差异的表格，与本讲的 `njs.engine === 'QuickJS'` 标志对照阅读。
