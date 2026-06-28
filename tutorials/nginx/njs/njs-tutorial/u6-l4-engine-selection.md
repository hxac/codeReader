# 引擎选择：js_engine 指令与运行时切换

## 1. 本讲目标

学完本讲，你应当能够：

- 说清「选择引擎」在 njs 里有哪两条入口（CLI 的 `-n`、nginx 的 `js_engine`），以及它们各自如何把一个字符串最终落到具体的引擎实现上。
- 描述两套引擎（内置 njs 引擎、QuickJS）在语言能力上的差异矩阵，并能判断一段 JS 在两个引擎下是否会行为不同。
- 识别「引擎专属特性」（`js_preload_object`、原生模块、`njs.dump`、顶层 `await` 等）分别属于哪个引擎，知道为什么跨引擎可移植代码要避开它们。
- 独立完成「同一段脚本在两个引擎下分别运行并对比差异」的动手实践。

## 2. 前置知识

本讲建立在 **u6-l1（QuickJS 包装层 `qjs.c`）** 的基础上，默认你已经知道：

- njs 内置两套**可互换**的 JS 引擎：自研的内置 njs 引擎（ES5.1 严格子集，1.0.0 起弃用）与上游 QuickJS（ES2023，推荐）。
- 两套引擎的根本不兼容：值类型不同（`njs_value_t` vs QuickJS 的 `JSValue`）、模块体系不同，所以扩展模块都是 `njs_*` 与 `qjs_*` 双实现（见 u6-l2）。
- njs 是**指令驱动**的：JS 不会自己跑起来，只能由 nginx 在请求处理某个阶段触发，或由 CLI 主动启动。

如果你还没读过 u6-l1，建议先读完，因为本讲会频繁引用 `qjs_new_context` 等已建立的概念。此外，理解 u1-l4（CLI 的 `-n` 选项）和 u8-l1（`ngx_engine_t` 引擎抽象）会有帮助，但不是硬性前置。

几个本讲会用到的术语先解释清楚：

- **引擎选择（engine selection）**：在运行期决定「这段 JS 用内置引擎还是 QuickJS 来跑」。注意它和「构建期是否链接 QuickJS」是两件事——后者由 `auto/quickjs` 探测决定（见 u1-l3），定义出 `NJS_HAVE_QUICKJS` 宏；只有这个宏被定义，QuickJS 引擎在运行期才是可选的。
- **可移植代码（portable code）**：同时在两套引擎下都能正确运行的 JS。本讲的一个核心目标就是教你写出可移植代码。

## 3. 本讲源码地图

本讲涉及的文件按职责分为三组：

| 文件 | 作用 |
|---|---|
| [external/njs_shell.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c) | CLI 入口：`-n` 选项、`NJS_ENGINE` 环境变量、`njs_create_engine` 按枚举分发到两套引擎 |
| [nginx/ngx_js.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.h) | 定义 nginx 侧引擎常量 `NGX_ENGINE_NJS` / `NGX_ENGINE_QJS` |
| [nginx/ngx_js.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.c) | nginx 共享层：`js_engine` 指令解析、配置合并默认值、请求期 `ngx_create_engine` 分发 |
| [nginx/ngx_http_js_module.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c) | 注册 `js_engine` / `js_preload_object` / `js_load_http_native_module` 等指令 |
| [nginx/ngx_stream_js_module.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_stream_js_module.c) | stream 侧同名指令注册 |
| [src/qjs.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.c) | QuickJS 侧把 `njs.engine` 设为字符串 `"QuickJS"` |
| [src/njs_builtin.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_builtin.c) | 内置引擎侧把 `njs.engine` 设为 `"njs"`，并注册仅 njs 可用的 `dump` |
| [docs/agent/js-dev.md](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/js-dev.md) | 权威的「引擎差异表」与引擎专属绑定清单 |
| [docs/agent/js-dev-njs.md](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/js-dev-njs.md) | 内置 njs 引擎的语言基线说明 |

## 4. 核心概念与源码讲解

### 4.1 引擎切换方式：两条入口，一种机制

#### 4.1.1 概念说明

「切换引擎」听起来像是运行期的一个开关，实际上 njs 在两个不同的运行环境里各提供了一条入口：

- **CLI（`build/njs`）**：用命令行选项 `-n njs` 或 `-n QuickJS`，或者环境变量 `NJS_ENGINE`。
- **nginx（http / stream 模块）**：用配置指令 `js_engine njs` 或 `js_engine qjs;`，可在 `http`/`server`/`location`（stream 是 `stream`/`server`）逐级覆盖。

两条入口看似不同，但底层是**同一种机制**：把一个人类可读的字符串（`"njs"` / `"QuickJS"` / `"qjs"`）映射成一个整数枚举，再用一个 `switch` 把枚举分发到对应引擎的初始化函数。上层只调函数指针，不感知两套引擎的差异。这是「双引擎」架构能在运行期平滑切换的关键。

需要特别区分的两件事：

1. **运行期选择**（本讲主题）决定「用哪个引擎跑」。
2. **构建期链接**决定「QuickJS 这个引擎是否可用」。只有用 `auto/quickjs` 探测到 `libquickjs` 并定义了 `NJS_HAVE_QUICKJS` 宏，QuickJS 分支才会被编译进来；否则 `-n QuickJS` / `js_engine qjs` 在运行期直接报「unknown engine」。你在源码里会反复看到 `#ifdef NJS_HAVE_QUICKJS` / `#if (NJS_HAVE_QUICKJS)` 这类条件编译守卫，就是这个原因。

#### 4.1.2 核心流程

**CLI 路径：**

```text
argv / NJS_ENGINE 环境变量
        │
        ▼
njs_options_parse_engine()     ← strncasecmp 把字符串映射成枚举
        │  写入 opts->engine (NJS_ENGINE_NJS=0 / NJS_ENGINE_QUICKJS=1)
        ▼
njs_create_engine()            ← switch(opts->engine)
        │
        ├── NJS_ENGINE_NJS      → njs_engine_njs_init()  + 函数指针表（njs）
        └── NJS_ENGINE_QUICKJS  → njs_engine_qjs_init()  + 函数指针表（qjs）
```

**nginx 路径：**

```text
nginx.conf: js_engine qjs;
        │  (配置解析期)
        ▼
ngx_js_engine()                ← 在 ngx_*_js_engines[] bitmask 表里查名字
        │  写入 loc_conf->type (NGX_ENGINE_NJS=1 / NGX_ENGINE_QJS=2)
        ▼
ngx_js_merge_conftime_loc_conf ← 未配置时合并默认值 NGX_ENGINE_NJS
        │  (请求期)
        ▼
ngx_create_engine()            ← switch(opts->engine)
        │
        ├── NGX_ENGINE_NJS      → ngx_engine_njs_init()  + 函数指针表
        └── NGX_ENGINE_QJS      → ngx_engine_qjs_init()  + 函数指针表
```

注意两个枚举的取值不一样：CLI 用 `0/1`，nginx 用 `1/2`——它们是两套独立的常量，互不影响。

#### 4.1.3 源码精读

**（1）CLI 的引擎枚举与默认值**

CLI 在 `njs_opts_t` 里用一个内嵌枚举记录选择，默认是内置 njs 引擎：

```c
typedef struct {
    enum {
        NJS_ENGINE_NJS = 0,
        NJS_ENGINE_QUICKJS = 1,
    }                       engine;
```

[external/njs_shell.c:62-64](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L62-L64) —— 定义 CLI 的两个引擎枚举值。`njs` 在前，正是「默认引擎」。

[external/njs_shell.c:549](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L549) —— 选项初始化时 `opts->engine = NJS_ENGINE_NJS;`，确认 CLI 不带 `-n` 时默认走内置引擎。

**（2）环境变量与 `-n` 选项都汇聚到同一个解析函数**

`NJS_ENGINE` 环境变量在主循环前读入；命令行 `-n` 取下一段参数。两者最后都调用 `njs_options_parse_engine`：

```c
case 'n':
    if (++i < argc) {
        ret = njs_options_parse_engine(opts, argv[i]);
```

[external/njs_shell.c:665-673](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L665-L673) —— `-n` 选项处理，需要参数则取 `argv[++i]`。

`njs_options_parse_engine` 是「字符串→枚举」的核心，用大小写不敏感比较：

```c
if (strncasecmp(engine, "njs", 3) == 0) {
    opts->engine = NJS_ENGINE_NJS;
#ifdef NJS_HAVE_QUICKJS
} else if (strncasecmp(engine, "QuickJS", 7) == 0) {
    opts->engine = NJS_ENGINE_QUICKJS;
#endif
} else {
    njs_stderror("unknown engine \"%s\"\n", engine);
    return NJS_ERROR;
}
```

[external/njs_shell.c:797-813](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L797-L813) —— 重点看 `#ifdef NJS_HAVE_QUICKJS`：QuickJS 分支被条件编译守卫。**若构建时未链接 QuickJS，`-n QuickJS` 会被当作未知引擎直接拒绝**。

**（3）CLI 的分发：switch + 函数指针表**

[external/njs_shell.c:3234-3274](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L3234-L3274) —— `njs_create_engine` 是 CLI 的分发枢纽。它先建内存池、零分配一个 `njs_engine_t`，然后按 `opts->engine` 进入对应分支：调用各自的 `*_init`，再把 `eval` / `execute_pending_job` / `destroy` / `output` 等函数指针指向该引擎的实现。之后上层只通过函数指针调用，完全感知不到底下是 njs 还是 QuickJS。QuickJS 分支整体也被 `#ifdef NJS_HAVE_QUICKJS` 包住。

**（4）nginx 侧的引擎常量**

[nginx/ngx_js.h:25-26](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.h#L25-L26) —— nginx 用独立的常量 `NGX_ENGINE_NJS = 1` / `NGX_ENGINE_QJS = 2`。

**（5）nginx 侧的 bitmask 名字表**

`js_engine` 指令的合法取值由一张 bitmask 表给出，名字到常量的映射就在表里：

```c
static ngx_conf_bitmask_t  ngx_http_js_engines[] = {
    { ngx_string("njs"), NGX_ENGINE_NJS },
#if (NJS_HAVE_QUICKJS)
    { ngx_string("qjs"), NGX_ENGINE_QJS },
#endif
    { ngx_null_string, 0 }
};
```

[nginx/ngx_http_js_module.c:541-547](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L541-L547) —— 注意 http 模块里 QuickJS 的名字是 **`qjs`**（不是 `QuickJS`），这与 CLI 的 `-n QuickJS` 不同；stream 模块用完全相同的表，见 [nginx/ngx_stream_js_module.c:237-243](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_stream_js_module.c#L237-L243)。

指令本身在指令表里注册，`post` 字段指向这张 bitmask 表：

[nginx/ngx_http_js_module.c:563-568](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L563-L568) —— `js_engine` 指令，可用在 `MAIN`/`SRV`/`LOC` 三个 http 配置层级，处理函数是共享层的 `ngx_js_engine`。

**（6）nginx 侧的指令解析函数**

`ngx_js_engine` 在 bitmask 表里做不敏感匹配，把结果写进 `loc_conf->type`：

```c
type = (size_t *) (p + cmd->offset);
...
for (m = 0; mask[m].name.len != 0; m++) {
    if (mask[m].name.len != value[1].len
        || ngx_strcasecmp(mask[m].name.data, value[1].data) != 0) {
        continue;
    }
    *type = mask[m].mask;
    break;
}
```

[nginx/ngx_js.c:3353-3390](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.c#L3353-L3390) —— 这与 CLI 的 `njs_options_parse_engine` 思路一致：都是「字符串→整数」。`*type != NGX_CONF_UNSET_UINT` 时直接返回 `"is duplicate"`，所以同一作用域重复写 `js_engine` 会报错；覆盖只能在更内层作用域。

**（7）nginx 侧的合并默认值**

[nginx/ngx_js.c:3774](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.c#L3774) —— `ngx_conf_merge_uint_value(conf->type, prev->type, NGX_ENGINE_NJS);`。这条等价于「本层未配、上层也未配时，默认 njs 引擎」。这解释了为什么 nginx 不写 `js_engine` 时跑的是内置引擎。

**（8）nginx 侧请求期的分发**

[nginx/ngx_js.c:542-583](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.c#L542-L583) —— `ngx_create_engine`（共享层，注意与 CLI 的 `njs_create_engine` 同名但不同函数）在请求处理时按 `opts->engine` 进入 `NGX_ENGINE_NJS` 或 `NGX_ENGINE_QJS` 分支，调用各自的 `*_init`，并装配 `engine->compile` / `engine->call` / `engine->destroy` 等函数指针——与 CLI 的做法完全对称，只是函数指针集合不同（nginx 的引擎抽象见 u8-l1 的 `ngx_engine_t`）。

> 一句话总结：两条入口、两种字符串、两套枚举，但收敛到同一个 `switch + 函数指针表` 模式。

#### 4.1.4 代码实践

**实践目标**：亲手验证 CLI 的 `-n` 切换确实改了引擎，并观察到「未链接 QuickJS 时 `-n QuickJS` 被拒」的行为。

**操作步骤**：

1. 按 u1-l3 构建 `build/njs`（若不确定是否链接了 QuickJS，看构建末尾是否打印 `enabled QuickJS engine`）。
2. 跑默认引擎：`./build/njs -c 'console.log(njs.engine)'`。
3. 显式切到内置引擎：`./build/njs -n njs -c 'console.log(njs.engine)'`。
4. 切到 QuickJS：`./build/njs -n QuickJS -c 'console.log(njs.engine)'`。
5. 用环境变量切换：`NJS_ENGINE=QuickJS ./build/njs -c 'console.log(njs.engine)'`。

**需要观察的现象**：

- 步骤 2/3 应打印 `njs`；步骤 4/5 应打印 `QuickJS`（前提是构建带了 QuickJS）。
- 若构建未链接 QuickJS，步骤 4 会输出 `unknown engine "QuickJS"` 并退出非零。

**预期结果**：成功时 `njs.engine` 的字符串随 `-n` 改变；失败时是明确的错误而非静默回退。**若你的环境未链接 QuickJS，步骤 4/5 的结果为「待本地验证」。** 不要假装已经跑过——请实际执行确认。

#### 4.1.5 小练习与答案

**练习 1**：为什么 nginx 里写 `js_engine QuickJS;`（大写 Q）会报错，而 CLI 里 `-n QuickJS` 却是正确的？

**答案**：两套入口的合法名字不同。CLI 的 `njs_options_parse_engine` 用 `strncasecmp(engine, "QuickJS", 7)`，大小写不敏感且接受 `QuickJS`；nginx 的 `ngx_js_engine` 在 bitmask 表 [nginx/ngx_http_js_module.c:541-547](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L541-L547) 里登记的是小写的 `qjs`，表里没有 `QuickJS` 这个名字，所以匹配失败、报 invalid value。

**练习 2**：在一个构建了 QuickJS 的 nginx 里，`http {}` 块写了 `js_engine qjs;`，但某个 `location` 没写 `js_engine`，该 location 用哪个引擎？

**答案**：用 `qjs`。`js_engine` 是可继承配置，`ngx_conf_merge_uint_value(conf->type, prev->type, NGX_ENGINE_NJS)`（[nginx/ngx_js.c:3774](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.c#L3774)）表示「本层未配则继承上层」；默认值 `NGX_ENGINE_NJS` 只在「本层和所有上层都没配」时才生效。

### 4.2 语言能力差异：为什么「可移植」要费心

#### 4.2.1 概念说明

两套引擎的语言基线天差地别：

- **内置 njs 引擎**：实现 **ECMAScript 5.1 严格模式**，外加一组手工挑选的 ES6+ 扩展（箭头函数、`let/const`、模板字符串、`async/await`、`?.`/`??`、`**` 等）。
- **QuickJS**：**ES2023**，现代语法几乎都支持。

这导致同一段 JS 在两个引擎下可能「能跑」或「报语法错」，甚至「结果不同」。如果你写的代码要同时跑在两个引擎上（比如正在从 njs 引擎迁移到 QuickJS 的过渡期），就必须知道差异边界在哪里。官方把这张差异表维护在 [docs/agent/js-dev.md](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/js-dev.md)，本讲带你对照源码理解它从哪来。

#### 4.2.2 核心流程

差异主要分三类：

| 类别 | 含义 | 例子 |
|---|---|---|
| **语法支持差异** | njs 引擎解析期就拒绝的语法 | `class`、生成器 `function*`、解构 `{a,b}=x`、展开 `f(...a)` |
| **内建对象差异** | njs 引擎根本没实现的全局构造器 | `Map`/`Set`/`WeakMap`、`BigInt`、`Proxy`、`Reflect` |
| **模块语法差异** | ES 模块导入形式 | njs 引擎只支持默认导入，不支持 `import {x}` / `import *` / `import "s"` |

差异表节选（完整版见 [docs/agent/js-dev.md:38-62](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/js-dev.md#L38-L62)）：

| 特性 | njs 引擎 | QuickJS |
|---|---|---|
| `class` | ✗ | ✓ |
| 生成器 `function*` / `yield` | ✗ | ✓ |
| 展开展示 `f(...a)` / `[...a]` | ✗ | ✓ |
| 解构 `{a,b}=x` / `[a,b]=x` | ✗ | ✓ |
| `Map`/`Set`/`WeakMap`/`WeakSet` | ✗ | ✓ |
| `BigInt`/`Proxy`/`Reflect` | ✗ | ✓ |
| 顶层 `await` | ✗ | ✓ |
| 非默认导入 `import {x}` | ✗ | ✓ |

判断「这段代码可不可移植」的直觉：**只要用了上表中 njs 列是 ✗ 的特性，就不是可移植代码**。可移植代码 = 只用两者都是 ✓ 的子集。

#### 4.2.3 源码精读

**（1）运行期自省：`njs.engine`**

脚本可以读取全局 `njs.engine` 做能力分支。它的值在每个引擎里是硬编码的字符串。

QuickJS 侧（u6-l1 已讲过 `qjs_njs_proto`，这里只看取值）：

```c
static const JSCFunctionListEntry qjs_njs_proto[] = {
    ...
    JS_PROP_STRING_DEF("engine", "QuickJS", JS_PROP_C_W_E),
```

[src/qjs.c:137-144](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.c#L137-L144) —— QuickJS 引擎下 `njs.engine === "QuickJS"`。

内置引擎侧用声明式属性表：

```c
static const njs_object_prop_init_t  njs_njs_object_properties[] =
{
    NJS_DECLARE_PROP_VALUE(SYMBOL_toStringTag, njs_ascii_strval("njs"), ...),
    NJS_DECLARE_PROP_VALUE(STRING_engine, njs_ascii_strval("njs"), ...),
    NJS_DECLARE_PROP_VALUE(STRING_version, njs_ascii_strval(NJS_VERSION), ...),
    ...
    NJS_DECLARE_PROP_NATIVE(STRING_dump, njs_ext_dump, 0, 0),
```

[src/njs_builtin.c:833-855](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_builtin.c#L833-L855) —— 内置引擎下 `njs.engine === "njs"`（L838）。注意 L848 的 `dump` 是**仅 njs 引擎**的方法（QuickJS 没有对应声明），属于「引擎专属特性」，下节细讲。

**（2）权威差异表**

[docs/agent/js-dev.md:38-62](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/js-dev.md#L38-L62) —— 「Engine differences at a glance」表，是判断可移植性的权威依据。`njs` 列为 ✗ 的特性都不能出现在跨引擎代码里。

[docs/agent/js-dev-njs.md:16-30](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/js-dev-njs.md#L16-L30) —— 内置 njs 引擎的语言基线白名单：箭头函数、`let/const`、模板字符串、完整 `Promise`、`async/await`（仅限 async 函数内，无顶层 await）、rest 参数、`?.`/`??`/逻辑赋值、`**`、Symbol 子集、**仅默认导入的 ES 模块**。

**（3）CLI 对 njs 专属选项的兼容性校验**

CLI 有一组选项（`-a` AST、`-d` 反汇编、`-g` 生成器跟踪、`-o` 逐指令跟踪、`-s` sandbox、`-u` safe）只对内置引擎有意义。选 QuickJS 时会被显式拒绝：

```c
if (opts->engine == NJS_ENGINE_QUICKJS) {
    if (opts->ast)        { njs_stderror("option \"-a\" is not supported for quickjs\n"); ... }
    if (opts->disassemble){ njs_stderror("option \"-d\" is not supported for quickjs\n"); ... }
    if (opts->generator_debug) { ... }
    if (opts->opcode_debug)    { ... }
    if (opts->sandbox)    { ... }
    if (opts->safe)       { ... }
}
```

[external/njs_shell.c:745-777](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L745-L777) —— 这段校验整块被 `#ifdef NJS_HAVE_QUICKJS` 包住。它揭示了一个要点：调试字节码/AST/反汇编的工具链（u3-l5、u4 系列）是**内置引擎专属**的，因为 QuickJS 的字节码是 QuickJS 自己的，njs 的反汇编器无法解读。

#### 4.2.4 代码实践

**实践目标**：写一段在两引擎下行为不同的最小 JS，亲手对比差异。

**操作步骤**：

1. 准备一段用了 `class` 和解构的脚本（两者在 njs 引擎都是 ✗）：

   ```js
   class Point { constructor(x, y) { this.x = x; this.y = y; } }
   const p = new Point(3, 4);
   const { x, y } = p;
   console.log(x + y);
   ```

   存为 `diff.mjs`（或用 `-c` 内联，注意 shell 转义）。

2. 在内置引擎跑：`./build/njs -n njs diff.mjs`。
3. 在 QuickJS 跑：`./build/njs -n QuickJS diff.mjs`。

**需要观察的现象**：

- njs 引擎：应在解析期报语法错（如 `unexpected token` / `Unexpected token`，针对 `class` 或解构），无输出。
- QuickJS：正常打印 `7`。

**预期结果**：同一份源码、不同引擎，一个报错一个成功。这就直观证明了「可移植代码必须避开 njs 列为 ✗ 的语法」。

**源码阅读型补充实践**（若本机无法构建 QuickJS）：对照 [docs/agent/js-dev.md:38-62](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/js-dev.md#L38-L62) 的表，把你常用 JS 特性逐个查一遍，列出「你写过、但 njs 引擎不支持」的特性清单——这是迁移旧代码前的自查步骤。

#### 4.2.5 小练习与答案

**练习 1**：下面这段代码在哪个引擎会失败？为什么？

```js
const m = new Map();
m.set('a', 1);
```

**答案**：njs 引擎会失败（`Map` 在 [docs/agent/js-dev.md:49](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/js-dev.md#L49) 标为 njs ✗）。`Map` 不是语法而是内建构造器，njs 引擎根本没注册它，运行期会得到 `Map is not defined`（或类似）。QuickJS 正常。

**练习 2**：你想要一段「在两个引擎都跑、但按引擎走不同分支」的代码，应该用什么判断？为什么不能用 `try/catch` 包住 `class` 定义？

**答案**：用 `if (njs.engine === 'QuickJS') { ... } else { ... }`，依据是 [src/qjs.c:142](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.c#L142) 与 [src/njs_builtin.c:838](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_builtin.c#L838) 的取值。不能用 `try/catch` 包 `class`，因为 `class` 在 njs 引擎是**解析期**错误（语法错），脚本根本没机会执行到 `try`——错误在编译阶段就抛出了，`try/catch` 是运行期机制，拦不住语法错。

### 4.3 引擎专属特性：preload、原生模块与 dump

#### 4.3.1 概念说明

除了语言语法，两套引擎还有一组**绑定级**的专属特性——它们是 nginx 指令或全局函数，只在一个引擎下可用。这些是跨引擎代码最容易踩的坑：

| 特性 | 仅 njs | 仅 QuickJS | 用途 |
|---|---|---|---|
| `js_preload_object` | ✓ | | 配置期预载一个不可变共享对象 |
| 原生模块 `js_load_*_native_module` | | ✓ | 把共享库（.so）当 JS 模块加载 |
| `njs.dump()` / `console.dump()` | ✓ | | 漂亮打印（含隐藏属性） |
| 顶层 `await` | | ✓ | 模块顶层直接用 await |
| `require()` | ✓ | | 旧式模块（已弃用，QuickJS 用 `import`） |

理解这些「专属」的来源，能帮你在迁移时知道每项该怎么替换。[docs/agent/js-dev.md](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/js-dev.md) 的「Don't」一节明确要求：跨引擎代码不要依赖这些扩展。

#### 4.3.2 核心流程

```text
跨引擎可移植代码 ──应避开──▶ 引擎专属特性
                              │
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
  js_preload_object     原生模块              njs.dump/顶层 await
  (仅 njs 引擎)         (仅 QuickJS)          (各自专属)
        │                     │
        ▼                     ▼
  迁移建议:                 迁移建议:
  数据折进普通模块,          无对应物,
  或用 ngx.shared            QuickJS 独有
```

迁移规则（来自 [docs/agent/js-dev-njs.md:40-57](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/js-dev-njs.md#L40-L57)）：

- `require()` → 改 `import`。
- `njs.dump()` → 改 `JSON.stringify` 或自写 helper。
- `js_preload_object` → 把数据折进一个普通模块用 `import`；跨 worker 共享态用 `ngx.shared` + `js_shared_dict_zone`。
- 旧语法 → 可放心现代化（解构、`class`、`Map/Set`）。

#### 4.3.3 源码精读

**（1）`js_preload_object`：仅 njs 引擎的指令**

[nginx/ngx_http_js_module.c:598-602](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L598-L602) —— http 模块注册 `js_preload_object`，处理函数是共享层的 `ngx_js_preload_object`。stream 模块同样注册，见 [nginx/ngx_stream_js_module.c:294-298](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_stream_js_module.c#L294-L298)。

[nginx/ngx_js.c:3393-3416](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.c#L3393-L3416) —— `ngx_js_preload_object` 的开头：解析 `name [from path]` 形式。注意它解析的只是「名字+路径」，真正只在 njs 引擎生效的限制体现在这条指令在 QuickJS 下被忽略/拒绝——这是 njs 引擎的预载机制（把对象塞进共享 hash），QuickJS 的值体系不支持这种共享方式。

**（2）原生模块：仅 QuickJS 的指令**

```c
{ ngx_string("js_load_http_native_module"),
  NGX_MAIN_CONF|NGX_DIRECT_CONF|NGX_CONF_TAKE13,
  ngx_js_core_load_native_module, ... }
```

[nginx/ngx_http_js_module.c:762-768](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L762-L768) —— `js_load_http_native_module` 注册在「核心指令」表（`NGX_MAIN_CONF`，只能写在配置最外层），加载一个 `.so` 共享库作为 JS 模块。它依赖 QuickJS 的 `JS_LoadModule`/原生模块加载能力（见 u6-l1 的 `qjs_module_t` 与 [nginx/ngx_js.c:2098-2159](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.c#L2098-L2159) 的 `ngx_qjs_native_module_lookup` / `ngx_qjs_module_loader`），内置引擎没有等价物。stream 侧对应 `js_load_stream_native_module`，见 [nginx/ngx_stream_js_module.c:451](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_stream_js_module.c#L451)。

**（3）`njs.dump` / `console.dump`：仅 njs 引擎的全局方法**

[src/njs_builtin.c:833-855](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_builtin.c#L833-L855) —— L848 `NJS_DECLARE_PROP_NATIVE(STRING_dump, njs_ext_dump, 0, 0)` 把 `dump` 挂在 `njs` 全局对象上，这是**仅 njs 引擎**的注册（QuickJS 的 `qjs_njs_proto` 里没有 `dump`，对照 [src/qjs.c:137-144](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.c#L137-L144)）。所以 `njs.dump(x)` / `console.dump(x)` 在 QuickJS 下是 `not a function`。

**（4）权威清单**

[docs/agent/js-dev.md:286-296](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/js-dev.md#L286-L296) —— 「Engine-specific bindings」明确列出三类：`js_preload_object`（njs only）、原生模块（QuickJS only）、`njs.dump`/`console.dump`（njs only）。

[docs/agent/js-dev.md:353-355](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/js-dev.md#L353-L355) —— 「Don't」清单：跨引擎代码不要依赖 `njs.dump`/`console.dump`、`js_preload_object`、原生模块、顶层 await、非默认导入。

#### 4.3.4 代码实践

**实践目标**：用 `njs.engine` 做能力分支，写一段「在两引擎都安全运行」的可移植封装，替代引擎专属的 `njs.dump`。

**操作步骤**：

1. 写一个 dump helper，按引擎分流：

   ```js
   // 示例代码：可移植的 dump 封装
   function dump(x) {
       if (njs.engine === 'QuickJS') {
           // QuickJS 没有 njs.dump，退回 JSON
           return JSON.stringify(x);
       }
       // 内置 njs 引擎才有 njs.dump，能打印隐藏属性
       return njs.dump(x);
   }

   console.log(dump({a: 1}));
   ```

2. 内置引擎跑：`./build/njs -n njs -c "$(cat dump.js)"`（或存文件）。
3. QuickJS 跑：`./build/njs -n QuickJS dump.js`。

**需要观察的现象**：两个引擎都打印出对象，且都不报错。去掉那个 `if` 分支、直接调 `njs.dump`，在 QuickJS 下会报 `njs.dump is not a function`。

**预期结果**：能力分支让同一段代码在两引擎都安全。这正是迁移期「既要支持旧 njs 代码、又要兼容新 QuickJS」的标准写法。

> 说明：上面的 `dump.js` 是**示例代码**，不是仓库原有文件，仅用于演示能力分支模式。

#### 4.3.5 小练习与答案

**练习 1**：一个旧 njs 配置里有 `js_preload_object conf from conf.json;`，迁移到 QuickJS 后这条指令怎么办？

**答案**：`js_preload_object` 仅 njs 引擎（[nginx/ngx_http_js_module.c:598-602](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L598-L602) + [docs/agent/js-dev.md:288-290](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/js-dev.md#L288-L290)）。按 [docs/agent/js-dev-njs.md:51-54](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/js-dev-njs.md#L51-L54)：把 `conf.json` 的数据折进一个普通 JS 模块，代码用 `import` 引入；若该数据本就是要跨 worker 共享的，改用 `ngx.shared` + `js_shared_dict_zone`（见 u9-l2）。

**练习 2**：为什么 `js_load_http_native_module` 写在 `http {}` 里的任意 location 都不行，而 `js_engine` 可以逐 location 覆盖？

**答案**：从指令的配置标志看，`js_load_http_native_module` 注册为 `NGX_MAIN_CONF|NGX_DIRECT_CONF`（[nginx/ngx_http_js_module.c:762-764](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L762-L764)），是「核心/顶层」指令，只能在配置最外层声明、全 worker 生效；而 `js_engine` 是 `NGX_HTTP_MAIN_CONF|NGX_HTTP_SRV_CONF|NGX_HTTP_LOC_CONF`（[nginx/ngx_http_js_module.c:563-564](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L563-L564)），可在 main/server/location 三级逐层覆盖。两者的作用域粒度由 nginx 指令表的标志决定。

## 5. 综合实践

把本讲三块知识串起来，完成一个「双引擎兼容性体检」小任务。

**背景**：你接手了一个老的 njs 配置，要确认它在 QuickJS 下是否还能跑、哪些地方需要改。

**任务**：

1. 写一段「体检脚本」`probe.js`，用 `njs.engine` 打印当前引擎，并探测 5 个关键能力的存在性：

   ```js
   // 示例代码：引擎能力体检
   console.log('engine =', njs.engine);

   function has(name, fn) {
       try { return typeof fn(); }
       } catch (e) { return '✗ ' + e.message; }
   }

   console.log('Map       :', has('Map', () => Map));
   console.log('class     :', (() => {
       try { eval('class C{}'); return 'supported'; }
       } catch (e) { return '✗ ' + e.message; }
   })());
   console.log('dump      :', typeof njs.dump);
   console.log('require   :', typeof require);
   ```

2. 分别在两引擎运行：

   ```bash
   ./build/njs -n njs      probe.js
   ./build/njs -n QuickJS  probe.js
   ```

3. 把两次输出列成对照表，标注每个能力的「可移植性」结论（两者都 ✓ = 可移植；只有一方有 = 引擎专属，需迁移）。
4. 阅读 [docs/agent/js-dev.md:38-62](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/js-dev.md#L38-L62) 与 [docs/agent/js-dev.md:286-296](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/js-dev.md#L286-L296)，核对你观察到的差异是否与官方表一致。

**验收标准**：

- 能解释 `njs.engine` 的两个取值分别来自 [src/qjs.c:142](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.c#L142) 与 [src/njs_builtin.c:838](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_builtin.c#L838)。
- 能说出 `Map`、`class`、`njs.dump`、`require` 各属于哪一类差异（语法/内建/专属绑定）。
- 能给出把这段不可移植代码改造为可移植的方案（参考 [docs/agent/js-dev-njs.md:40-57](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/js-dev-njs.md#L40-L57)）。

> 说明：`probe.js` 为示例代码，非仓库原有文件。所有 `eval`/运行结果以本地实际执行为准。

## 6. 本讲小结

- njs 有**两条切换入口**：CLI 的 `-n njs|QuickJS`（或 `NJS_ENGINE` 环境变量）与 nginx 的 `js_engine njs|qjs;`。两者都收敛为「字符串→枚举→`switch` + 函数指针表」，但枚举不同（CLI 用 `NJS_ENGINE_NJS=0/QUICKJS=1`，nginx 用 `NGX_ENGINE_NJS=1/QJS=2`）、合法名字也不同（CLI 接 `QuickJS`，nginx 要小写 `qjs`）。
- nginx 的 `js_engine` 可在 main/server/location 逐级覆盖，未配置时合并默认值为内置 njs 引擎；同一作用域重复写会因 `is duplicate` 报错。
- QuickJS 分支全程被 `NJS_HAVE_QUICKJS` / `#if (NJS_HAVE_QUICKJS)` 守卫——**构建期没链接 QuickJS，运行期就选不了它**。运行期选择与构建期链接是两件事。
- 两引擎**语言基线**差异巨大（ES5.1 子集 vs ES2023）：`class`、生成器、解构、展开、`Map/Set`、`BigInt`、顶层 `await`、非默认导入等在 njs 引擎不可用。判断可移植性的权威依据是 [docs/agent/js-dev.md](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/js-dev.md) 的差异表。
- 一组**引擎专属绑定**：`js_preload_object`（仅 njs）、原生模块 `js_load_*_native_module`（仅 QuickJS，且是顶层指令）、`njs.dump`/`console.dump`（仅 njs）、`require`（仅 njs）。跨引擎代码必须避开它们，迁移规则见 [docs/agent/js-dev-njs.md](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/js-dev-njs.md)。
- 调试工具链（`-a`/`-d`/`-g`/`-o`/`-s`/`-u`）是内置引擎专属，选 QuickJS 时被 CLI 显式拒绝（[external/njs_shell.c:745-777](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L745-L777)）——因为 QuickJS 的字节码是它自己的，njs 反汇编器读不了。
- 运行期可用 `njs.engine` 做能力分支（QuickJS 返回 `"QuickJS"`，njs 返回 `"njs"`），这是写双引擎兼容代码的标准手段。

## 7. 下一步学习建议

- **横向打通 nginx 集成**：本讲只讲了「怎么选引擎」。引擎选定后，nginx 如何在请求期 clone 出 per-request VM、如何把 `r`/`s` 对象注入——这是 u8-l1（`ngx_js` 共享层与 `ngx_engine_t` 抽象）、u8-l2（http 模块与 `r` 对象）、u8-l3（stream 模块与 `s` 对象）的主题，建议按序读。
- **纵向吃透引擎实现**：若你想理解两引擎「内部」的差异，回看 u6-l1（`qjs_new_context` 如何裁剪/补充 QuickJS 内建）和 u5 系列（内置引擎的对象模型），对照本讲的「差异表」会更有体感。
- **亲手迁移一个模块**：找一个还在用 `require`/`njs.dump`/`js_preload_object` 的旧 njs 配置，按 [docs/agent/js-dev-njs.md:40-57](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/js-dev-njs.md#L40-L57) 的清单改造为 QuickJS 可用，并用本讲的 `TEST_NGINX_GLOBALS_HTTP='js_engine qjs;'` 方式在两引擎各跑一遍测试（双引擎测试见 u10-l1）。
- **阅读测试用例**：[nginx/t/js_engine.t](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/t/js_engine.t) 用四个 location 演示了 `js_engine` 在 server/location 两级的覆盖语义，是理解指令继承与覆盖的最佳样例。
