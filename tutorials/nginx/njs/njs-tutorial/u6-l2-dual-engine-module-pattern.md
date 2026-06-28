# 双引擎模块模式：njs_* 与 qjs_* 双实现

## 1. 本讲目标

本讲承接 u6-l1（QuickJS 包装层）与 u5-l4（外部对象与原生函数），回答一个在阅读 njs 源码时几乎一定会冒出来的问题：

> 为什么 `external/` 下几乎每个功能都有 `njs_xxx_module.c` 和 `qjs_xxx_module.c` **两份**长得像、又不完全一样的代码？

学完本讲，你应当能够：

1. 说清楚「双实现」约定背后的根本原因——两套引擎的**值类型**和**模块体系**完全不同。
2. 对比 `njs_module_t{name, preinit, init}` 与 `qjs_module_t{name, init}` 两种注册结构的差异，并理解它们各自被谁、在何时调用。
3. 跟踪从 `auto/modules` / `auto/qjs_modules` 两份清单，经 `auto/make` 汇总成 `njs_modules[]` / `qjs_modules[]` 两个全局数组、最终分别进入 `libnjs.a` / `libqjs.a` 的完整构建链路。
4. 当你「只想改一处 fs 行为」时，准确指出**必须同步修改的所有位置**，避免出现「QuickJS 上修好了、内置 njs 引擎上还在报错」的尴尬。

---

## 2. 前置知识

本讲需要你已经建立以下认知（来自前置讲义），这里只做一句话回顾，不展开：

- **双引擎并存**（u1-l1 / u6-l1）：njs 同时内置两套可互换引擎——自研的 njs 引擎（ES5.1 子集，1.0.0 起弃用）与包装上游的 QuickJS 引擎（ES2023，推荐）。运行期用 `js_engine` 指令或 CLI 的 `-n` 选项切换。
- **两套值类型不兼容**（u2-l2）：内置引擎的 JS 值统一是 16 字节的 `njs_value_t`（标签联合体）；QuickJS 用的是 QuickJS 自家的 `JSValue`。两者**不能直接互通**。
- **两套模块体系不兼容**：内置引擎的「模块」是一段编译产物 `njs_mod_t`，靠 `njs_vm_add_module` 注册（u5-l4 的外部对象机制）；QuickJS 走标准 ES Module，靠 `JSModuleDef *` 与 `JS_NewCModule` 注册（u6-l1）。
- **外部对象是内置引擎机制**（u5-l4）：`njs_external_t` 描述符、`njs_vm_external_prototype` / `njs_vm_external_create` 这一套只属于内置 njs 引擎，QuickJS 侧用 `JSClassDef` + `JS_NewClass` 实现等价能力。

一句话：**正因为两套引擎的「值」和「模块」底层都不一样，凡是想暴露给 JS 的扩展功能（fs、crypto、xml、zlib……），都得为每套引擎各写一份粘合代码。**这就是「双实现 = 双份代码」这条贯穿整个项目的铁律。

> 名词速查
> - **external 模块**：指 `external/` 目录下、为 JS 提供运行时能力的 C 扩展（fs/crypto/zlib 等），区别于 `src/` 里的引擎内核。
> - **宿主模块（addons）**：由嵌入方（如 NGINX）在运行期传入的模块，与构建期固定的 `njs_modules[]`/`qjs_modules[]` 并列，本讲末尾会提到。

---

## 3. 本讲源码地图

| 文件 | 角色 | 本讲用它说明什么 |
|---|---|---|
| `external/njs_fs_module.c` | 内置 njs 引擎的 fs 实现 | `njs_module_t` 注册结构、`njs_external_t` 声明表、`njs_fs_init` 物化流程 |
| `external/qjs_fs_module.c` | QuickJS 引擎的 fs 实现 | `qjs_module_t` 注册结构、`JSCFunctionListEntry` 导出表、`qjs_fs_init` 注册流程 |
| `src/njs.h` | 内置引擎公共头 | `njs_module_t` 类型定义、`njs_addon_init_pt` 签名 |
| `src/qjs.h` | QuickJS 包装层头 | `qjs_module_t` 类型定义、`qjs_addon_init_pt` 签名、`QJS_CORE_CLASS_ID_*` 枚举 |
| `auto/modules` | 内置引擎模块清单 | 哪些 `njs_*_module` 被收录、各自的收录条件 |
| `auto/qjs_modules` | QuickJS 模块清单 | 哪些 `qjs_*_module` 被收录、各自的收录条件 |
| `auto/module` / `auto/qjs_module` | 单模块收录脚本 | 如何把一个模块追加进源码/模块清单 |
| `auto/make` | Makefile 生成器 | 如何把清单汇总成 `njs_modules[]`/`qjs_modules[]` 与 `libnjs.a`/`libqjs.a` |
| `src/njs_vm.c` / `src/qjs.c` | 引擎启动入口 | 两个全局数组分别在何处被消费（`preinit`/`init` 调用点） |

---

## 4. 核心概念与源码讲解

### 4.1 双实现约定

#### 4.1.1 概念说明

打开 `external/` 目录，你会看到成对出现的文件：

```
external/njs_fs_module.c        external/qjs_fs_module.c
external/njs_crypto_module.c    external/qjs_crypto_module.c
external/njs_xml_module.c       external/qjs_xml_module.c
external/njs_zlib_module.c      external/qjs_zlib_module.c
external/njs_query_string_module.c   external/qjs_query_string_module.c
external/njs_webcrypto_module.c external/qjs_webcrypto_module.c
```

命名约定极其严格：

- `njs_xxx_module.c` —— 服务于**内置 njs 引擎**，基于 `njs_value_t` / `njs_module_t` / `njs_external_t`。
- `qjs_xxx_module.c` —— 服务于 **QuickJS 引擎**，基于 `JSValue` / `qjs_module_t` / QuickJS 的 `JSClassDef` / `JSModuleDef`。

**为什么非要写两份？** 因为「暴露一个 C 函数给 JS 调用」这件事，在两套引擎里走的 API 完全不同。以「读文件」为例：

- 内置引擎侧：C 函数签名是 `njs_int_t f(njs_vm_t *vm, njs_value_t *args, njs_uint_t nargs, njs_index_t calltype, njs_value_t *retval)`，参数从 `njs_value_t` 数组取，返回值写进 `retval`。
- QuickJS 侧：C 函数签名是 `JSValue f(JSContext *cx, JSValueConst this_val, int argc, JSValueConst *argv, int magic)`，参数是 `JSValue`，返回 `JSValue`（出错返回 `JS_EXCEPTION`）。

两个签名从**第一个参数开始就不兼容**，更不用说值的内部表示（16 字节标签联合体 vs QuickJS 的 `JSValue`）。所以即便两份文件实现的「业务逻辑」（怎么 `open`/`read`/`write` 一个文件）几乎一模一样，**粘合层也必须各写一份**。这就是「双引擎 = 双份代码」在文件层面的具体体现。

> 好消息：真正容易出 bug 的**文件系统/加密业务逻辑**，两份实现往往会调用同一组底层 helper（如 `njs_fs_path`、`qjs_fs_path`），尽量复用。差异主要集中在「值的读写」和「模块的注册」这两层粘合代码上。

#### 4.1.2 核心流程

一个 external 模块从「源码」到「JS 里能 `import`」的生命周期，两套引擎各有走法：

**内置 njs 引擎**（声明式 + 外部对象）：

```
njs_ext_fs[] 声明表（njs_external_t 数组）
        │  njs_vm_external_prototype() 编译成 proto_id
        ▼
njs_fs_init(vm)  ──► njs_vm_external_create() 造实例
        │           ──► njs_vm_add_module("fs", value) 注册成模块
        ▼
JS 里通过 require/import 拿到这个外部对象
```

**QuickJS 引擎**（ES Module + 类系统）：

```
qjs_fs_export[] 导出表（JSCFunctionListEntry 数组）
        │
qjs_fs_init(cx, "fs") 返回 JSModuleDef*
        │  ├── JS_NewClass() + JS_SetClassProto() 注册 Stats/Dirent/FileHandle 类
        │  └── JS_NewCModule(cx, name, qjs_fs_module_init)
        │           └── qjs_fs_module_init: JS_SetModuleExportList 导出列表
        ▼
JS 里 import fs from 'fs' 拿到模块命名空间
```

注意一个关键差异：**内置引擎把模块注册成一个「外部对象」**（fs 本身是一个宿主对象，它的方法是 external method）；**QuickJS 把模块注册成一个标准 ES Module**（fs 是一个有 `default` 导出和命名导出的模块对象）。这是两条不同的物化路径。

#### 4.1.3 源码精读

先看内置引擎侧 fs 的声明表头部。这是一张 `njs_external_t` 数组，每一条声明一个属性/方法，靠 `flags` 区分类型（u5-l4 讲过这套机制）：

[njs_fs_module.c:502-521](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_fs_module.c#L502-L521) —— `njs_ext_fs[]` 声明表的开头：第一条声明 `Symbol.toStringTag` 等于 `"fs"`，第二条把 `access` 方法绑到 C 函数 `njs_fs_access`，并用 `magic8 = NJS_FS_CALLBACK` 标记它走回调风格。`accessSync` 复用**同一个** C 函数 `njs_fs_access`，只把 `magic8` 改成 `NJS_FS_DIRECT`（同步风格）——这就是 u5-l4 讲过的「一个 C 实现服务多个 JS 方法」的 `magic` 技巧。

再看 QuickJS 侧 fs 的导出表头部。这是一张 QuickJS 标准的 `JSCFunctionListEntry` 数组：

[qjs_fs_module.c:290-300](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/qjs_fs_module.c#L290-L300) —— `qjs_fs_export[]` 导出表的开头：用 `JS_OBJECT_DEF` 嵌入 `constants`/`promises` 子对象，用 `JS_CFUNC_MAGIC_DEF("access", 3, qjs_fs_access, QJS_FS_CALLBACK)` 声明方法。注意它和上面 `njs_ext_fs[]` 是**一一对应**的（都有 `access`/`accessSync`，都复用同一个 C 函数 `xxx_fs_access`，都用 magic 区分同步/回调），只是声明宏从 `njs_external_t` 换成了 `JSCFunctionListEntry`、C 函数从 `njs_fs_access` 换成了 `qjs_fs_access`。

对比这两个 C 函数的签名，差异一目了然：

[njs_fs_module.c:1453-1456](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_fs_module.c#L1453-L1456) —— 内置引擎的 `njs_fs_access`，入参是 `njs_vm_t *vm, njs_value_t *args, njs_uint_t nargs, njs_index_t calltype, njs_value_t *retval`。

[qjs_fs_module.c:368-371](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/qjs_fs_module.c#L368-L371) —— QuickJS 的 `qjs_fs_access`，入参是 `JSContext *cx, JSValueConst this_val, int argc, JSValueConst *argv, int calltype`。

业务逻辑（`path` 校验、`mode`/`callback` 取值）几乎逐行对应，但**取参数、返回结果、报异常**的 API 调用全部不同。这就是「为什么必须两份」最直接的证据。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：亲手验证「两份文件一一对应」的约定。

**操作步骤**：

1. 在 `external/njs_fs_module.c` 的 `njs_ext_fs[]` 表里（从约 502 行起）找出 `readFile`、`writeFile`、`readdir` 三个方法各自绑定的 C 函数名（看 `.u.method.native`）。
2. 在 `external/qjs_fs_module.c` 的 `qjs_fs_export[]` 表里（从约 290 行起）找出同名的三个方法，看它们绑定的 C 函数名。
3. 对比两份实现里同名 C 函数（如 `njs_fs_read_file` 与 `qjs_fs_read_file`）的**前 10 行**，数一数有多少行是「业务逻辑」（相同）、多少行是「粘合代码」（不同）。

**需要观察的现象**：你会发现方法的**名字、个数、magic 含义**在两份表里高度一致，差异只在 C 函数名前缀（`njs_` vs `qjs_`）和声明宏。

**预期结果**：两份导出表是「同一份 API 设计的两套绑定」。这也意味着——**改 API 表面（新增/删除/重命名一个方法）时，两个文件必须同步改**。

#### 4.1.5 小练习与答案

**练习 1**：如果只想给 fs 增加一个全新的方法 `fs.copyFileSync`，需要改哪些文件？为什么不能只改一份？

**参考答案**：至少要改 `external/njs_fs_module.c`（在 `njs_ext_fs[]` 加一条 `NJS_EXTERN_METHOD`，并实现 `njs_fs_copy_file` C 函数）**和** `external/qjs_fs_module.c`（在 `qjs_fs_export[]` 加一条 `JS_CFUNC_MAGIC_DEF`，并实现 `qjs_fs_copy_file`）。不能只改一份，因为两套引擎的值类型与模块体系不兼容，`qjs_*.c` 里的 `JSValue` 函数无法被内置引擎调用，反之亦然。共享的底层 helper（如真正执行 `copy_file` 的系统调用封装）可以放在其中一个文件并互相复用。

**练习 2**：`njs_fs_access` 和 `qjs_fs_access` 的参数个数和类型都不一样，那它们「等价」体现在哪里？

**参考答案**：体现在**对外暴露给 JS 的 API 表面相同**——都叫 `access`/`accessSync`、都接受 `(path, mode[, callback])`、都用 magic 区分同步/回调风格、都返回相同语义的结果。差异完全被各自的「粘合层」吸收，对 JS 用户透明。

---

### 4.2 两种注册结构

#### 4.2.1 概念说明

光有声明表还不够，引擎得知道「在启动时去初始化这个模块」。每种引擎都有一个**注册结构体**，每个 external 模块文件末尾都定义一个该类型的全局变量，作为「模块的身份卡片」：

- 内置引擎：`njs_module_t`，字段 `{name, preinit, init}`。
- QuickJS：`qjs_module_t`，字段 `{name, init}`。

两者最显眼的差异是：内置引擎有 **`preinit` 和 `init` 两个阶段**，QuickJS 只有 **`init` 一个函数**。这背后是两套引擎初始化时机的不同——内置引擎把模块初始化拆成「原型物化前（preinit）」和「原型物化后（init）」两步，让某些模块有机会在内建原型就绪前/后分别插手；QuickJS 的模块初始化发生在 `JS_NewContextRaw` 之后、intrinsic 补齐的同一阶段，没有这种拆分需求。

另一个本质差异是**返回类型**：

- `njs_addon_init_pt` 返回 `njs_int_t`（状态码 `NJS_OK`/`NJS_ERROR`）。
- `qjs_addon_init_pt` 返回 `JSModuleDef *`（一个指向 ES 模块对象的指针，失败返回 `NULL`）。

也就是说，QuickJS 的「init」**本身就在创建并返回模块对象**；而内置引擎的「init」只是把外部对象塞进 VM 的模块表，返回成功与否。

#### 4.2.2 核心流程

**注册结构的定义与消费时机**：

内置引擎 `njs_module_t`：
```
njs_vm_create(vm)
  ├─ for 每个 njs_modules[i]: 调 preinit(vm)   ← 原型物化前
  ├─ njs_vm_protos_init(vm)                    ← 物化内建原型
  └─ for 每个 njs_modules[i]: 调 init(vm)      ← 原型物化后
```

QuickJS `qjs_module_t`：
```
qjs_new_context(rt, addons)
  ├─ JS_NewContextRaw + JS_AddIntrinsic*       ← 建空上下文、点齐语言特性
  ├─ for 每个 qjs_modules[i]: 调 init(ctx, name) ← 返回 JSModuleDef*
  └─ qjs_add_intrinsic_*                       ← 补 btoa/atob/TextEncoder/njs
```

注意：`init` 的「参数」也不同——内置引擎只传 `vm`（模块名已在结构体 `.name` 里），QuickJS 传 `(ctx, name)`（把名字作为参数传入，因为要用它去 `JS_NewCModule(cx, name, ...)`）。

#### 4.2.3 源码精读

**注册结构体定义**

[njs.h:240-246](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs.h#L240-L246) —— `njs_module_t` 定义：`preinit` 与 `init` 都是 `njs_addon_init_pt` 类型，即 `njs_int_t (*)(njs_vm_t *vm)`。

[qjs.h:44-49](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.h#L44-L49) —— `qjs_module_t` 定义：只有 `name`（注意是 `const char *` 而非 `njs_str_t`）和 `init`，`init` 是 `qjs_addon_init_pt`，即 `JSModuleDef *(*)(JSContext *ctx, const char *name)`，返回的是模块对象指针。

**fs 的身份卡片**

[njs_fs_module.c:1446-1450](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_fs_module.c#L1446-L1450) —— 内置引擎 fs 的注册结构：`.name = "fs"`、`.preinit = NULL`（fs 不需要原型物化前的钩子）、`.init = njs_fs_init`。

[qjs_fs_module.c:362-365](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/qjs_fs_module.c#L362-L365) —— QuickJS fs 的注册结构：`.name = "fs"`、`.init = qjs_fs_init`。

**两份 init 各做了什么**

[njs_fs_module.c:3889-3950](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_fs_module.c#L3889-L3950) —— 内置引擎的 `njs_fs_init(vm)`：先检查 sandbox（沙箱下直接跳过、不注册 fs）；接着用 `njs_vm_external_prototype` 把 `njs_ext_stats` / `njs_ext_dirent` / `njs_ext_filehandle` / `njs_ext_bytes_read` / `njs_ext_bytes_written` / `njs_ext_fs` 六张声明表编译成 `proto_id`；再用 `njs_vm_external_create` 造出 fs 实例；最后 `njs_vm_add_module(vm, "fs", value)` 把它注册进 VM 的模块表。这是 u5-l4 外部对象机制的典型应用。

[qjs_fs_module.c:2957-3019](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/qjs_fs_module.c#L2957-L3019) —— QuickJS 的 `qjs_fs_init(cx, name)`：先检查 `QJS_CORE_CLASS_ID_FS_STATS` 是否已注册（幂等保护），未注册则用 `JS_NewClass` + `JS_SetClassProto` 注册 `Stats`/`Dirent`/`FileHandle` 三个类；然后 `JS_NewCModule(cx, name, qjs_fs_module_init)` 创建 ES 模块、`JS_AddModuleExport`/`JS_AddModuleExportList` 声明导出，最后**返回 `m`（JSModuleDef \*）**。模块真正的导出填充发生在回调 `qjs_fs_module_init` 里（[qjs_fs_module.c:2933-2954](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/qjs_fs_module.c#L2933-L2954)），它用 `JS_SetPropertyFunctionList` 把 `qjs_fs_export` 表挂到模块的 `default` 导出和命名导出上。

**注册结构的消费点**（验证两套机制各自的调用时机）

[njs_vm.c:85-138](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L85-L138) —— 内置引擎在 `njs_vm_create` 里**两段式**遍历 `njs_modules[]`：第 85-94 行先跑所有 `preinit`，第 110 行 `njs_vm_protos_init` 物化原型，第 115-124 行再跑所有 `init`。紧随其后（第 126-137 行）以同样方式跑嵌入方传入的 `addons`。

[qjs.c:258-270](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.c#L258-L270) —— QuickJS 在 `qjs_new_context` 里**单段式**遍历 `qjs_modules[]`，对每个模块调 `(*module)->init(ctx, (*module)->name)`，返回 `NULL` 即失败；之后（第 264-270 行）同样处理 `addons`。

> 一句话对比：内置引擎的模块是「**外部对象 + 模块表登记**」，分两阶段；QuickJS 的模块是「**标准 ES Module + 类注册**」，单阶段、且 init 直接返回模块对象。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：用「`magic`/`magic8` 复用」这条线索，理解同一个 C 函数如何服务多个 JS 方法。

**操作步骤**：

1. 读 `njs_fs_module.c` 的 `njs_ext_fs[]`，找到 `readFile` 与 `readFileSync`，确认它们是否绑同一个 `.native`（如 `njs_fs_read_file`），并记录各自 `magic8` 的值。
2. 读 `qjs_fs_module.c` 的 `qjs_fs_export[]`，做同样的事（`JS_CFUNC_MAGIC_DEF` 的第 4 个参数即 magic）。
3. 打开对应的 C 函数（如 `njs_fs_read_file` / `qjs_fs_read_file`），看它如何根据 magic（`QJS_FS_CALLBACK` vs `QJS_FS_DIRECT`）分支决定「走回调」还是「直接返回」。

**需要观察的现象**：两套实现都用「函数指针 + magic 整数」把一簇相关 JS 方法（Sync / 异步回调 / Promise）收敛到一个 C 实现。

**预期结果**：你能口头复述「`access` 与 `accessSync` 为何能共用一个 C 函数」——靠 `magic8`/`magic` 在函数内部分支。

#### 4.2.5 小练习与答案

**练习 1**：`njs_module_t` 有 `preinit` 而 `qjs_module_t` 没有，这是 QuickJS 的功能缺失吗？

**参考答案**：不是缺失，而是**架构差异**。内置引擎把模块初始化拆成「原型物化前」「原型物化后」两步，给某些模块在内建原型就绪前后插手的余地；QuickJS 的模块初始化发生在上下文 intrinsic 补齐阶段，模块直接用 `JS_NewClass` 注册自己的类、不依赖内建原型的两阶段物化，故无需 `preinit`。

**练习 2**：为什么 `qjs_addon_init_pt` 返回 `JSModuleDef *`，而 `njs_addon_init_pt` 只返回 `njs_int_t`？

**参考答案**：QuickJS 的 init **职责就是创建并返回一个 ES 模块对象**（`JS_NewCModule` 的产物），引擎拿到这个指针才能把模块登记进上下文的模块表供 `import` 解析；内置引擎的 init 只是「把外部对象塞进 VM 模块表」这个副作用，成功与否用一个状态码表达即可，模块本身早已通过 `njs_vm_add_module` 进表，不需要再「返回」。

---

### 4.3 构建清单汇总

#### 4.3.1 概念说明

前两节讲的是「源码层面怎么写双实现」。这一节回答：**这两份实现是怎么被编译进去、又怎么在启动时被引擎找到的？**

njs 用的是一套**基于 shell 变量的自研构建系统**（u1-l2 讲过总览）。核心思路：用几个全局 shell 变量当「清单」，`auto/modules` 和 `auto/qjs_modules` 两份脚本分别往里**追加**模块名和源文件，最后 `auto/make` 读这些变量，**生成**两个 C 文件 `njs_modules.c` / `qjs_modules.c`——它们各自定义全局数组 `njs_modules[]` / `qjs_modules[]`，把所有模块的「身份卡片」指针收成一个以 `NULL` 结尾的数组。引擎启动时遍历这个数组即可。

关键认知：**这两份清单是完全独立的两条流水线**。`auto/modules` 只管内置引擎（产出进 `libnjs.a`），`auto/qjs_modules` 只管 QuickJS（产出进 `libqjs.a`）。一个 `njs_*` 模块忘了进清单，只影响内置引擎；一个 `qjs_*` 模块忘了进清单，只影响 QuickJS——这正是「改一处行为要同步多处」的根源之一。

#### 4.3.2 核心流程

从 `./configure` 到 `njs_modules[]` / `qjs_modules[]` 的完整链路：

```
./configure
  ├─ . auto/init            ← 清空 NJS_LIB_MODULES= / QJS_LIB_MODULES=
  ├─ ...（特性检测：openssl/libxml2/zlib/quickjs ...）
  ├─ . auto/sources         ← 内核固定源码进 NJS_LIB_SRCS
  ├─ . auto/modules         ← 逐个 source auto/module，追加 njs_* 模块
  │       └─ auto/module:   NJS_LIB_MODULES+=" njs_fs_module"
  │                          NJS_LIB_SRCS+=" external/njs_fs_module.c"
  ├─ . auto/qjs_modules     ← 逐个 source auto/qjs_module，追加 qjs_* 模块
  │       └─ auto/qjs_module: QJS_LIB_MODULES+=" qjs_fs_module"
  │                              QJS_LIB_SRCS+=" external/qjs_fs_module.c"
  └─ . auto/make            ← 读变量，生成 build/Makefile
          ├─ 生成 build/njs_modules.c  → 定义 njs_module_t *njs_modules[]
          ├─ 生成 build/qjs_modules.c  → 定义 qjs_module_t *qjs_modules[]
          └─ 生成 libnjs.a / libqjs.a / build/njs 目标
```

注意 `auto/module` 与 `auto/qjs_module` 是**被反复 source 的「函数式」脚本**：每次调用前先设好 `njs_module_name` / `njs_module_srcs` 两个临时变量，source 之后它就把这两个值追加进对应清单。条件依赖（如 OpenSSL、libxml2、zlib）通过 `if [ $NJS_OPENSSL = YES -a $NJS_HAVE_OPENSSL = YES ]` 这样的判断，决定是否 source 某个模块——这解释了为什么 crypto/webcrypto/xml/zlib 是「可选模块」，而 fs/query_string/buffer 是「必选模块」。

#### 4.3.3 源码精读

**两份清单本身**

[auto/modules:40-50](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/modules#L40-L50) —— 内置引擎清单片段：fs 与 query_string 是**无条件**收录的（直接 `. auto/module`），而 [auto/modules:10-38](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/modules#L10-L38) 显示 crypto/webcrypto/xml/zlib 都被包在 `if [ ... = YES ]` 条件里。

[auto/qjs_modules:18-28](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/qjs_modules#L18-L28) —— QuickJS 清单片段：fs 与 query_string 同样无条件收录，`. auto/qjs_module`（注意是 `qjs_module` 不是 `module`）。两份清单的「模块名」一一对应（`njs_fs_module` ↔ `qjs_fs_module`），但分属不同变量。

**单模块收录脚本（被反复 source 的「函数」）**

[auto/module:1-7](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/module#L1-L7) —— 把当前 `njs_module_name` 追加进 `NJS_LIB_MODULES`，把 `njs_module_srcs` 追加进 `NJS_LIB_SRCS`，把 `njs_module_incs` 追加进 `NJS_LIB_INCS`。

[auto/qjs_module:1-7](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/qjs_module#L1-L7) —— QuickJS 版本：追加进 `QJS_LIB_MODULES` / `QJS_LIB_SRCS`。注意第 6 行它也往 `NJS_LIB_INCS` 追加（include 目录两引擎共用），但源码与模块名走各自的 `QJS_*` 变量。

**清单的初始化（清空）**

[auto/init:18-19](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/init#L18-L19) —— 在 `configure` 最开头把两个清单变量初始化为空，确保后续追加从一个干净的起点开始。`configure` 在 [configure:16](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/configure#L16) source 它，随后在 [configure:60-63](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/configure#L60-L63) 依次 source `auto/sources` → `auto/modules` → `auto/qjs_modules` → `auto/make`。

**auto/make 生成两个数组文件（核心汇总点）**

[auto/make:14-41](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/make#L14-L41) —— 生成 `build/njs_modules.c`：先对每个 `NJS_LIB_MODULES` 元素 `echo "extern njs_module_t $mod;"` 声明外部符号，再 `echo 'njs_module_t *njs_modules[] = {'`，逐个 `echo "    &$mod,"`，最后写 `NULL` 收尾。这就是 `njs_modules[]` 全局数组的来源——它把所有内置引擎模块的「身份卡片」地址收成一个数组。

[auto/make:43-70](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/make#L43-L70) —— 对称地生成 `build/qjs_modules.c`：`extern qjs_module_t $mod;` 声明 + `qjs_module_t *qjs_modules[] = { ... NULL }` 数组。两个生成块几乎逐行对称，差异只在类型名（`njs_module_t` vs `qjs_module_t`）和包含的头（`njs_main.h` vs `qjs.h`）。

**两个库与最终 CLI 的链接**

[auto/make:107-123](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/make#L107-L123) —— 定义 `libnjs.a`（由 `NJS_LIB_OBJS` 打包，含 `njs_modules.o`）与 `libqjs.a`（由 `QJS_LIB_OBJS` 打包，含 `qjs_modules.o`）两个静态库目标。注意第 80-83 行：仅当 `NJS_HAVE_QUICKJS = YES`（configure 探测到 quickjs 库）时 `QJS_LIB` 才非空，否则不构建 `libqjs.a`。

[auto/make:175-184](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/make#L175-L184) —— 最终的 `build/njs` CLI 目标**同时链接 `libnjs.a` 和 `QJS_LIB`（即 `libqjs.a`）**。这就是为什么同一个 `build/njs` 二进制能用 `-n njs` / `-n QuickJS` 切换引擎——两套实现都被编进去了，运行期靠 `njs.engine` 分支选择（u6-l1）。

**数组的外部声明（消费侧）**

[njs_module.h:25](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_module.h#L25) —— `extern njs_module_t *njs_modules[];`，供 `njs_vm.c` 遍历。

[qjs.h:194](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.h#L194) —— `extern qjs_module_t *qjs_modules[];`，供 `qjs.c` 遍历。

至此整条链路闭环：清单脚本 → shell 变量 → 生成的 `.c` 文件 → 全局数组 → 引擎启动遍历 → 调用每个模块的 `init`。

#### 4.3.4 代码实践（构建验证型）

**实践目标**：亲眼看到 `auto/make` 生成的 `njs_modules[]` / `qjs_modules[]` 数组长什么样。

**操作步骤**：

1. 在项目根目录执行 `./configure && make njs`（参考 u1-l3 的构建步骤）。
2. 打开 `build/njs_modules.c`，确认它形如：
   ```c
   extern njs_module_t  njs_buffer_module;
   extern njs_module_t  njs_fs_module;
   ...
   njs_module_t *njs_modules[] = {
       &njs_buffer_module,
       &njs_fs_module,
       ...
       NULL
   };
   ```
3. 打开 `build/qjs_modules.c`，做同样观察，对比两者收录的模块**是否一一对应**。
4. （可选）用 `./configure --no-openssl && make clean && make njs` 重新构建，再比较两个数组文件，观察 crypto/webcrypto 模块**从数组里消失**——验证条件收录机制。

**需要观察的现象**：`njs_modules.c` 里每个 `&xxx_module` 都对应 `auto/modules` 里 source 过的一个模块；`--no-openssl` 后 crypto 相关条目不复存在。

**预期结果**：你能根据 `build/njs_modules.c` / `build/qjs_modules.c` 的内容，反推出本次构建启用了哪些 external 模块。若本地未装 QuickJS 库，则 `build/qjs_modules.c` 可能不存在（`NJS_HAVE_QUICKJS != YES`），此情形标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：如果你新增了一个 `external/njs_sqlite_module.c` 和 `external/qjs_sqlite_module.c`，构建系统层面要让它们被收录，需要在哪两个文件里加内容？加什么？

**参考答案**：在 `auto/modules` 里加一段（设 `njs_module_name=njs_sqlite_module`、`njs_module_srcs=external/njs_sqlite_module.c`，再 `. auto/module`），并在 `auto/qjs_modules` 里加对称的一段（`njs_module_name=qjs_sqlite_module`、`njs_module_srcs=external/qjs_sqlite_module.c`，再 `. auto/qjs_module`）。两处缺一不可，否则对应引擎里 `import sqlite` 会找不到模块。

**练习 2**：为什么 `build/njs` 二进制能同时支持两套引擎，而不是编译成两个独立的可执行文件？

**参考答案**：因为 `auto/make` 在链接 `build/njs` 时同时链入了 `libnjs.a` 和 `libqjs.a`（[auto/make:175-184](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/make#L175-L184)），两套实现共存于同一进程。运行期由 CLI 的 `-n` 选项或 `NJS_ENGINE` 环境变量（u1-l4）选择激活哪一套，`njs.engine` 字段供 JS 代码做能力分支（u6-l1）。这样的好处是单个二进制即可覆盖两种引擎，便于测试与部署。

---

## 5. 综合实践

**任务**：假设你要给 fs 模块**修复一个 bug**——`readFileSync` 在读取空文件时错误地返回了 `undefined`，应该返回空字符串/空 Buffer。请完成一次「双引擎正确修复」的全流程演练。

**要求**：

1. **定位**：分别在 `external/njs_fs_module.c`（找 `njs_fs_read_file`，关注 `QJS_FS_DIRECT`/`NJS_FS_DIRECT` 分支）和 `external/qjs_fs_module.c`（找 `qjs_fs_read_file`）里定位返回结果的构造点。
2. **改业务逻辑**：如果两份实现共享某个底层 helper（如读取后构造返回值的公共函数），优先改 helper（只改一处，两引擎同时受益）；如果各自独立构造，则**两处都要改**。
3. **检查 API 表面**：确认无需改动 `njs_ext_fs[]` / `qjs_fs_export[]` 两张声明表（因为只是修返回值、没改方法签名）。
4. **构建清单**：确认无需改 `auto/modules` / `auto/qjs_modules`（没有新增/删除源文件）。
5. **双引擎验证**：用 `./build/njs -n njs -c '...'` 与 `./build/njs -n QuickJS -c '...'` 各跑一遍读取空文件的用例，确认两边都返回正确结果。
6. **测试**：参考 u10-l1，在 `test/fs/methods.t.mjs`（或对应测试）补充空文件用例，确保回归被覆盖。

**交付**：列出你**实际改动**的文件清单，并说明为什么其他「看起来相关」的文件（声明表、构建清单、对侧引擎文件）**不需要改**或**也必须改**。

**这道题的价值**：它逼你把本讲三个最小模块串起来——「改行为」可能只动 helper（最小成本），但也可能因为两份实现各自独立而**必须双改**；而只要你**没动 API 表面、没增删源文件**，声明表和构建清单就可以安全地不碰。判断「哪些必须同步改、哪些不用」正是阅读 njs 双引擎源码最关键的工程直觉。

---

## 6. 本讲小结

- njs 的每个 external 模块都成对提供 `njs_xxx_module.c`（内置引擎，基于 `njs_value_t`/`njs_external_t`）与 `qjs_xxx_module.c`（QuickJS，基于 `JSValue`/`JSClassDef`/`JSModuleDef`），因为两套引擎的**值类型**和**模块体系**根本不兼容。
- 注册结构两套：`njs_module_t{name, preinit, init}`（init 返回状态码，分两阶段被 `njs_vm_create` 调用）vs `qjs_module_t{name, init}`（init 返回 `JSModuleDef *`，单阶段被 `qjs_new_context` 调用）。
- 内置引擎把模块物化成「外部对象 + 模块表登记」（`njs_vm_external_prototype`/`njs_vm_external_create`/`njs_vm_add_module`）；QuickJS 把模块物化成「标准 ES Module + 类注册」（`JS_NewClass`/`JS_SetClassProto`/`JS_NewCModule`/`JS_AddModuleExportList`）。
- 构建链路是两条独立流水线：`auto/modules`→`NJS_LIB_*`→`build/njs_modules.c`→`libnjs.a`；`auto/qjs_modules`→`QJS_LIB_*`→`build/qjs_modules.c`→`libqjs.a`。`auto/make` 把两个库都链进同一个 `build/njs`。
- 「改一处 fs 行为」的最小同步集：业务逻辑尽量复用底层 helper（一处改、两引擎受益）；若两份实现各自独立，则 `njs_*` 与 `qjs_*` **必须双改**；新增/删除方法才需要动两张声明表和两份清单。
- `njs.engine === 'QuickJS'` 让 JS 代码能在运行期针对不同引擎做能力分支，这是双实现对外暴露的统一逃生口（承接 u6-l1）。

---

## 7. 下一步学习建议

- **u6-l3（Buffer 与 TypedArray 的双引擎实现）**：本讲以 fs 为案例，下一讲换一个更贴近「二进制边界条件」的模块（Buffer），看双实现如何在 `njs_buffer.c` / `src/njs_typed_array.c` 与 `qjs_buffer.c` 之间分布，并复盘近期多个 Buffer 越界/类型混淆修复是如何同时落在两份实现上的。
- **u7（外部扩展模块）**：本讲建立了「双实现」的总框架，u7 各篇会逐个深入 fs/crypto/webcrypto/querystring/xml/zlib 的具体 API 表面与可选依赖检测，把本讲的「身份卡片」落到每个模块的真实方法上。
- **u8-l1（ngx_js 共享层与引擎抽象）**：若你想知道 NGINX 集成层如何用 `ngx_engine_t` 把本讲的两套注册结构再抽象成统一接口、并把 `r`/`s` 作为「宿主模块（addons）」分别注入两套引擎，那是下一单元的入口。
- **延伸阅读**：直接对照阅读 `external/njs_fs_module.c` 与 `external/qjs_fs_module.c` 的 `init` 函数，是体会「同一设计、两套绑定」最快的练习；也可挑一个**只在某一引擎存在**的模块（如某些 webcrypto 子能力）观察「双实现不完全对称」的真实情况。
