# fs：文件系统模块

## 1. 本讲目标

本讲带你读懂 njs 的 `fs`（文件系统）扩展模块。它是 u6 单元「双引擎架构」之后的第一个真实扩展模块案例，也是练习「一个功能、两份实现」这一定律的最好入口。读完本讲，你应当能够：

- 说出 `fs` 模块在 njs 内置引擎与 QuickJS 两份实现中各提供了哪些 API（`readFile`/`writeFile`/`readdir`/`stat`/`mkdir` 等的同步、回调、Promise 三套写法）。
- 解释「一个 C 函数服务多种 JS 方法」的 `magic` 编码机制，并理解同步/回调/Promise 三种结果是如何通过同一个 `njs_fs_result` 分发的。
- 描述 `Stats`、`Dirent`、`FileHandle` 三类对象在内置引擎（外部原型）与 QuickJS（注册类）两种实现下的表示差异。
- 知道 `-s` sandbox 模式如何让 `fs` 模块整体消失，以及为什么 QuickJS 不支持 sandbox。

本讲对应的最小模块为：**fs API 表面**、**Stats/Dirent 对象**、**sandbox 限制**。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**Node.js 的 fs 你应该见过。** njs 的 `fs` 模块刻意模仿 Node.js 的 `fs`，提供了几乎同名的 API：`readFile`、`writeFile`、`readdir`、`stat`、`mkdir`、`unlink`、`rename`…… 而且每个方法通常有三种「调用约定」：

| 调用约定 | 后缀 | 行为 |
|---|---|---|
| 同步（Direct） | `...Sync` | 直接返回结果或抛错 |
| 回调（Callback） | 无后缀 | 最后一个参数是 `callback(err, result)` |
| Promise | 放在 `fs.promises.*` 下 | 返回一个 Promise |

这是 Node.js 生态的惯例，本讲的看点在于：njs 怎么用**同一份 C 代码**同时实现这三套写法。

**为什么 fs 是「双引擎＝双份代码」的典型案例。** 回顾 u6-l2：内置引擎用 `njs_value_t`、`njs_module_t`、外部原型机制；QuickJS 用 `JSValue`、`qjs_module_t`、标准 ES Module。两套值类型根本不兼容，所以 `external/` 下每个扩展模块都成对存在 `njs_*_module.c` 与 `qjs_*_module.c`。`fs` 正是这种成对结构最完整的样本。

**fs 与异步模型的关系。** 回顾 u4-l5：njs 内置引擎是同步单线程的 VM，异步靠「状态机 + jobs 作业队列 + 续体」模拟。你会看到 `fs` 的「异步」方法其实**先把文件读完，再把结果投递进 jobs 队列**——它不是真正的非阻塞 IO，而是把「结果的交付」延迟到下一轮 job。理解这一点，你就把 u4-l5 的 jobs 队列和真实的扩展模块联系起来了。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `external/njs_fs_module.c` | `fs` 模块的**内置引擎**实现（3950 行）。声明 `njs_ext_fs` 等外部表、`njs_fs_init` 注册、所有 `njs_fs_*` C 函数。 |
| `external/qjs_fs_module.c` | `fs` 模块的 **QuickJS** 实现（3026 行）。声明 `qjs_fs_export`/`qjs_fs_promises` 函数表、`qjs_fs_init` 注册类与模块、所有 `qjs_fs_*` C 函数。 |
| `src/qjs.h` | 定义 `QJS_CORE_CLASS_ID_FS_STATS/DIRENT/FILEHANDLE` 等类 id 枚举。 |
| `test/fs/methods.t.mjs`、`test/fs/read_write.t.mjs` | test262 风格的 fs 测试，覆盖 sync/callback/promise 三种写法。 |
| `test/harness/compatFs.js` | 测试辅助脚本，演示 `import fs from 'fs'` 与 `fs.promises` 的真实用法。 |
| `external/njs_shell.c` | CLI 入口，处理 `-s` sandbox 选项（与本讲的 sandbox 限制直接相关）。 |

## 4. 核心概念与源码讲解

### 4.1 fs 模块的 API 表面与双实现注册

#### 4.1.1 概念说明

`fs` 模块对外暴露一个名为 `fs` 的 ES Module，使用者写 `import fs from 'fs'` 就能拿到它。这个模块对象上有：

- 一堆**同步方法**：`readFileSync`、`writeFileSync`、`readdirSync`、`statSync`、`mkdirSync`、`unlinkSync`、`renameSync`、`existsSync`、`realpathSync`、`readlinkSync`、`symlinkSync`、`accessSync`……
- 一堆**回调方法**（同名无后缀）：`readFile`、`writeFile`、`readdir`、`stat`、`mkdir`……
- 一个 `promises` 子对象，里面是 **Promise 方法**：`fs.promises.readFile`、`fs.promises.stat`……
- 一个 `constants` 子对象，提供 `F_OK/R_OK/W_OK/X_OK` 等访问模式常量。

关键认知是：**内置引擎和 QuickJS 各自独立实现这一整套 API**。两份实现的「对外形状」必须一致（同一个 JS 测试套件 `test/fs/*.t.mjs` 同时跑在两引擎上），但「内部机制」完全不同——这正是 u6-l2 双实现铁律的体现。

#### 4.1.2 核心流程

以内置引擎为例，`fs` 模块从「配置」到「可用」经过三步：

1. **构建期**：`auto/modules` 把 `external/njs_fs_module.c` 收进 `njs_modules[]`，编译进 `libnjs.a`。（QuickJS 侧由 `auto/qjs_modules` 收 `external/qjs_fs_module.c` 进 `qjs_modules[]`，编译进 `libqjs.a`。）
2. **VM 创建期**：`njs_vm_create` 调用每个模块的 `.init`，即 `njs_fs_init`。它先决定是否「跳过」（sandbox），再用 `njs_vm_external_prototype` 把若干张外部描述符表物化成原型，最后 `njs_vm_add_module(vm, "fs", value)` 把模块对象挂到模块表。
3. **运行期**：JS 里写 `import fs from 'fs'`，解析器从模块表取回 `njs_fs_init` 创建的那个对象。

QuickJS 侧对应：`qjs_new_context` 调用 `qjs_fs_module.init`，即 `qjs_fs_init`。它用 `JS_NewClass` 注册类、`JS_NewCModule` 建立标准 ES Module。

#### 4.1.3 源码精读

**模块注册结构（内置引擎）。** [external/njs_fs_module.c:1446-L1450](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_fs_module.c#L1446-L1450) 是模块描述符，`name="fs"`，初始化函数指向 `njs_fs_init`（u6-l2 讲过的 `njs_module_t{name, preinit, init}` 结构，`preinit=NULL`）。

**三个对象原型的 id 句柄。** fs 里有三类「对象类型」（Stats/Dirent/FileHandle，外加两个 bytes 计数小对象）。`njs_fs_init` 会把它们各自物化为一个外部原型，并把整数句柄存进文件级静态变量，供后续创建实例时引用：

```c
static njs_int_t    njs_fs_stats_proto_id;
static njs_int_t    njs_fs_dirent_proto_id;
static njs_int_t    njs_fs_filehandle_proto_id;
static njs_int_t    njs_fs_bytes_read_proto_id;
static njs_int_t    njs_fs_bytes_written_proto_id;
```
见 [external/njs_fs_module.c:1439-L1443](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_fs_module.c#L1439-L1443)。这个「proto_id 句柄」就是 u5-l4 讲过的 `njs_vm_external_prototype` 返回值。

**init 函数主体。** [external/njs_fs_module.c:3889-L3947](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_fs_module.c#L3889-L3947) 做了四件事：① sandbox 检查（见 4.4）；② 用 `njs_vm_external_prototype` 创建 5 个对象原型；③ 用 `njs_vm_external_create` 造一个 `fs` 模块实例；④ 用 `njs_vm_add_module(vm, "fs", value)` 把它登记为名为 `"fs"` 的模块。最后一步是关键——它让 `import fs from 'fs'` 能解析到这个对象。

**API 表面：一张外部表即一份 API 清单。** [external/njs_fs_module.c:711-L720](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_fs_module.c#L711-L720) 是 `readFileSync` 这一项的声明（节选）：

```c
{
    .flags = NJS_EXTERN_METHOD,
    .name.string = njs_str("readFileSync"),
    .u.method = {
        .native = njs_fs_read_file,
        .magic8 = NJS_FS_DIRECT,
    }
},
```

注意三点：① 整张 `njs_ext_fs[]` 表就是 fs 的**对外 API 清单**，每加一个方法就在这里加一行；② `readFileSync` 的 `native` 指向 `njs_fs_read_file`——而**不带 `Sync` 的回调版 `readFile` 也指向同一个 C 函数**（见 [external/njs_fs_module.c:700-L709](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_fs_module.c#L700-L709)），区别只在 `magic8`（前者 `NJS_FS_DIRECT`，后者 `NJS_FS_CALLBACK`）；③ `promises` 子对象在 [external/njs_fs_module.c:666-L676](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_fs_module.c#L666-L676) 作为 `NJS_EXTERN_OBJECT` 嵌入，指向另一张表 `njs_ext_fs_promises[]`。

**QuickJS 侧的等价物：函数表。** QuickJS 不用「外部描述符表」而用 `JSCFunctionListEntry` 数组。`fs.promises.*` 的全部方法列在 [external/qjs_fs_module.c:265-L287](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/qjs_fs_module.c#L265-L287)，例如：

```c
static const JSCFunctionListEntry qjs_fs_promises[] = {
    JS_CFUNC_MAGIC_DEF("access",     2, qjs_fs_access,    QJS_FS_PROMISE),
    JS_CFUNC_MAGIC_DEF("readFile",   2, qjs_fs_read_file, QJS_FS_PROMISE),
    JS_CFUNC_MAGIC_DEF("readdir",    2, qjs_fs_readdir,   QJS_FS_PROMISE),
    JS_CFUNC_MAGIC_DEF("stat",       2, qjs_fs_stat,
                       qjs_fs_magic(QJS_FS_PROMISE, QJS_FS_STAT)),
    ...
};
```

结构与内置引擎几乎一一对应，只是宏换了名字（`JS_CFUNC_MAGIC_DEF` 对应内置引擎的 `NJS_EXTERN_METHOD`）。对照这两张表，你就能体会到 u6-l2 说的「改一处行为，两份声明表都要同步」。

> 对照点：内置引擎用 `njs_ext_fs[]`（`njs_external_t`），QuickJS 用 `qjs_fs_export[]`（`JSCFunctionListEntry`）。API 形状靠这两张表声明，是改 fs 对外行为时必须同步维护的「两份清单」。

#### 4.1.4 代码实践

**实践目标**：通过对比两份声明表，亲手验证「fs API 表面在两引擎下形状一致」。

**操作步骤**：

1. 打开 [external/njs_fs_module.c:502-L899](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_fs_module.c#L502-L899)（`njs_ext_fs[]` 全表），数一下顶层方法名，记录 `readFile`/`readFileSync`/`writeFile`/`writeFileSync`/`stat`/`statSync`/`mkdir`/`mkdirSync`/`readdir`/`readdirSync` 各自的 `native` 与 `magic8`。
2. 打开 [external/qjs_fs_module.c:290-L345](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/qjs_fs_module.c#L290-L345)（`qjs_fs_export[]`），核对同样的方法名是否都在。
3. 检查 `promises` 子对象：内置引擎在 `njs_ext_fs_promises[]`（[external/njs_fs_module.c:310](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_fs_module.c#L310)），QuickJS 在 `qjs_fs_promises[]`（[external/qjs_fs_module.c:265](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/qjs_fs_module.c#L265)）。

**需要观察的现象**：两份表的方法名集合应当完全一致（可能顺序不同）；每一个同名方法，内置引擎的 `native` 与 QuickJS 的 C 函数是「成对」的（如 `njs_fs_read_file` ↔ `qjs_fs_read_file`、`njs_fs_stat` ↔ `qjs_fs_stat`）。

**预期结果**：你会得到一张形如「`readFileSync` → `njs_fs_read_file`(DIRECT) ↔ `qjs_fs_read_file`(DIRECT)」的对照表。这正是「双实现」在日常维护中的具体落点——在任一侧新增/删除一个方法，另一侧的声明表与构建清单（`auto/modules`/`auto/qjs_modules`）都要同步。

#### 4.1.5 小练习与答案

**练习 1**：`fs.statSync`、`fs.stat`、`fs.lstatSync`、`fs.fstat` 这四个方法，在内置引擎里分别由哪几个 C 函数实现？

**答案**：它们**全部**由同一个 C 函数 `njs_fs_stat` 实现，靠 `magic8` 区分。可在 [external/njs_fs_module.c:822-L862](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_fs_module.c#L822-L862) 一带核对：`statSync` 用 `njs_fs_magic(NJS_FS_DIRECT, NJS_FS_STAT)`、`stat`（回调）用 `njs_fs_magic(NJS_FS_CALLBACK, NJS_FS_STAT)`、`lstatSync` 用 `njs_fs_magic(NJS_FS_DIRECT, NJS_FS_LSTAT)`、`fstat` 用 `njs_fs_magic(NJS_FS_CALLBACK, NJS_FS_FSTAT)`。

**练习 2**：`fs.promises.stat` 在 QuickJS 实现里指向哪个 C 函数？它和内置引擎的对应关系是什么？

**答案**：指向 `qjs_fs_stat`，magic 为 `qjs_fs_magic(QJS_FS_PROMISE, QJS_FS_STAT)`（见 [external/qjs_fs_module.c:281-L282](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/qjs_fs_module.c#L281-L282)）。它与内置引擎的 `njs_fs_stat` 是「成对实现」关系，magic 编码方式完全一致（都是 `((mode)<<2)|calltype`）。

### 4.2 一个 C 函数服务多种调用约定：magic 编码与结果分发

#### 4.2.1 概念说明

上一节你已经看到 `njs_fs_read_file` 同时实现 `readFile` 和 `readFileSync`。本节回答两个问题：

- VM 怎么把「是哪个 JS 方法调进来的」信息告诉这个 C 函数？——靠 `magic8`。
- 同一个 C 函数怎么知道该「直接返回 / 调回调 / 返回 Promise」？——靠 `magic8` 里的 `calltype` 位，再交给 `njs_fs_result` 分发。

这个机制是 fs 模块最值得学的工程技巧，也是它和 u4-l5 异步模型的接口点。

#### 4.2.2 核心流程

**magic 的位编码。** `njs_fs_magic(calltype, mode)` 把两种信息打包进一个 8 位整数：

\[
\text{magic} = ((\text{mode}) \ll 2) \;|\; \text{calltype}
\]

其中 `calltype` 占低 2 位，取自枚举 `NJS_FS_DIRECT=0`、`NJS_FS_PROMISE=1`、`NJS_FS_CALLBACK=2`；`mode` 占高位，比如写文件有 `NJS_FS_TRUNC=0`/`NJS_FS_APPEND=1`，stat 有 `NJS_FS_STAT=0`/`NJS_FS_LSTAT=1`/`NJS_FS_FSTAT=2`。于是「调用了哪个 JS 方法」被唯一编码进 `magic8`。

**三路结果分发。** C 函数执行完业务逻辑（开文件、读字节等）后，把结果交给 `njs_fs_result(vm, &result, calltype, callback, ...)`，它按 `calltype` 走三条分支：

| calltype | 行为 |
|---|---|
| `NJS_FS_DIRECT` | 出错就把 result 作为异常抛出（`njs_vm_throw`）；成功就把 result 直接写入 `retval` 返回。对应 `...Sync`。 |
| `NJS_FS_PROMISE` | 新建一个 Promise，把 `(resolve, reject)` 中合适的一个连同 result 包成一个 job 入队（`njs_vm_enqueue_job`），并把 Promise 写入 `retval` 返回。对应 `fs.promises.*`。 |
| `NJS_FS_CALLBACK` | 把用户传入的 `callback` 函数包成 job 入队，调用时按 `(err, result)` 传参，函数本身返回 `undefined`。对应无后缀的回调版。 |

注意 **PROMISE 和 CALLBACK 都只是「把已算好的结果投递进 jobs 队列」**——真正的文件读写是同步发生的，异步性只体现在「结果交付」这一步。这就把 fs 和 u4-l5 的 jobs 作业队列连起来了。

#### 4.2.3 源码精读

**magic 宏与枚举（内置引擎）。** [external/njs_fs_module.c:38-L62](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_fs_module.c#L38-L62) 定义了打包宏和三组枚举：

```c
#define njs_fs_magic(calltype, mode)  (((mode) << 2) | calltype)

typedef enum { NJS_FS_DIRECT, NJS_FS_PROMISE, NJS_FS_CALLBACK } njs_fs_calltype_t;
typedef enum { NJS_FS_TRUNC,  NJS_FS_APPEND }                   njs_fs_writemode_t;
typedef enum { NJS_FS_STAT,   NJS_FS_LSTAT, NJS_FS_FSTAT }      njs_fs_statmode_t;
```

QuickJS 侧的对应物在 [external/qjs_fs_module.c:38-L48](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/qjs_fs_module.c#L38-L48)，宏名换成 `qjs_fs_magic`、枚举前缀换成 `QJS_FS_`，但**位编码完全相同**——所以同一份 magic 在两引擎下语义一致。

**readFile 的实现。** [external/njs_fs_module.c:1796-L1911](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_fs_module.c#L1796-L1911) 是 `njs_fs_read_file` 全貌。它的骨架是「解析参数 → 按 calltype 取出可能的 callback → 打开文件 → fstat 校验是普通文件 → `njs_fs_fd_read` 读全部字节 → 按 encoding 编码成 Buffer/字符串」，最后在 `done:` 处理返回：

```c
done:
    if (fd != -1) { (void) close(fd); }

    if (ret == NJS_OK) {
        return njs_fs_result(vm, &result, calltype, callback, 2, retval);
    }
    return NJS_ERROR;
```
见 [external/njs_fs_module.c:1900-L1910](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_fs_module.c#L1900-L1910)。注意：**只有成功的返回会经过 `njs_fs_result`，而错误（`ret != NJS_OK`）由前面 `njs_fs_error` 把 error 对象写进 `result` 后也走同一条 `njs_fs_result`**——所以三路分发对成功/失败都成立。

> 细节：[external/njs_fs_module.c:1818-L1828](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_fs_module.c#L1818-L1828) 显示只有 CALLBACK 才会去取 callback 实参并校验它是函数；DIRECT/PROMISE 不需要 callback。这就是「一个函数、靠 calltype 切换行为」的落点。

**三路分发函数 njs_fs_result。** [external/njs_fs_module.c:3396-L3454](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_fs_module.c#L3396-L3454) 是本节的精华。三段分别：

```c
case NJS_FS_DIRECT:                       // 直接返回或抛出
    if (njs_value_is_error(...result)) { njs_vm_throw(vm, ...); return NJS_ERROR; }
    njs_value_assign(retval, result); return NJS_OK;

case NJS_FS_PROMISE:                      // 建 Promise + 入队 job
    njs_vm_promise_create(vm, &promise, &callbacks[0]);
    cb = njs_vm_function_alloc(vm, ngx_fs_promise_trampoline, 0, 0);
    njs_value_assign(&arguments[0], &callbacks[njs_value_is_error(...result)]);
    njs_value_assign(&arguments[1], result);
    njs_vm_enqueue_job(vm, cb, arguments, 2);
    njs_value_assign(retval, &promise); return NJS_OK;

case NJS_FS_CALLBACK:                     // 入队用户回调，返回 undefined
    /* arguments[0]=err, arguments[1]=result，或反过来 */
    njs_vm_enqueue_job(vm, njs_value_function(callback), arguments, 2);
    njs_value_undefined_set(retval); ...
```

两个关键点：① PROMISE 分支用 `njs_value_is_error(result)` 作为下标在 `{resolve, reject}` 里选一个，**错误走 reject、成功走 resolve**——这就是 Node 风格 fs 的语义；② 两条异步分支都调用 `njs_vm_enqueue_job`，这正是 u4-l5 讲过的 jobs 队列入口。`ngx_fs_promise_trampoline` 是一个把 `(resolveOrReject, value)` 转成「调用该 resolve/reject 函数」的跳板。

#### 4.2.4 代码实践

**实践目标**：在源码层面跟踪一次 `fs.promises.readFile` 调用，看清它如何走到 `njs_fs_result` 的 PROMISE 分支并入队 job。

**操作步骤**：

1. 从 [external/njs_fs_module.c:389-L398](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_fs_module.c#L389-L398) 找到 `promises.readFile` 这一项，确认它的 `native=njs_fs_read_file`、`magic8=NJS_FS_PROMISE`。
2. 进入 `njs_fs_read_file`（[external/njs_fs_module.c:1796](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_fs_module.c#L1796)），定位末尾 `done:` 处的 `njs_fs_result` 调用（[external/njs_fs_module.c:1907](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_fs_module.c#L1907)）。
3. 跟进 `njs_fs_result`（[external/njs_fs_module.c:3396](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_fs_module.c#L3396)），看 `case NJS_FS_PROMISE` 如何 `njs_vm_promise_create` + `njs_vm_enqueue_job`。

**需要观察的现象**：调用栈里 C 函数其实**已经把文件读完了**，只是把结果的投递推迟到一个 job；返回给 JS 的是一个未结算的 Promise。

**预期结果**（待本地验证）：写一段 `import fs from 'fs'; fs.promises.readFile('/etc/hostname').then(b=>console.log(b.toString()))`，由于 job 要等当前同步代码结束后才执行，输出会出现在「脚本主体执行完」之后；这与 u4-l5 的 jobs 队列行为一致。

#### 4.2.5 小练习与答案

**练习 1**：`njs_fs_magic(NJS_FS_CALLBACK, NJS_FS_LSTAT)` 算出来等于多少？它对应哪个 JS 方法？

**答案**：`NJS_FS_CALLBACK=2`、`NJS_FS_LSTAT=1`，所以 \(((1)\ll 2)|2 = 4|2 = 6\)。它对应 `fs.lstat`（回调版 lstat），见 [external/njs_fs_module.c:611-L620](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_fs_module.c#L611-L620)。

**练习 2**：为什么 `readFileSync` 读一个不存在的文件会直接抛异常，而 `fs.promises.readFile` 读同样的文件却返回一个 rejected Promise？请用 `njs_fs_result` 的分支解释。

**答案**：两者都先在 `njs_fs_read_file` 里把错误对象写进 `result`，再走 `njs_fs_result`。DIRECT 分支（[external/njs_fs_module.c:3404-L3411](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_fs_module.c#L3404-L3411)）发现 result 是 error 就 `njs_vm_throw` 抛出；PROMISE 分支（[external/njs_fs_module.c:3413-L3436](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_fs_module.c#L3413-L3436)）则用 `njs_value_is_error` 在 `{resolve, reject}` 中选 `reject`，把 reject 与 error 入队，返回的 Promise 随后被 job 结算成 rejected。错误「内容」一样，只是「交付方式」由 calltype 决定。

### 4.3 Stats / Dirent / FileHandle 对象的表示

#### 4.3.1 概念说明

fs 里有三类「对象返回值」：

- **`Stats`**：`stat()`/`lstat()`/`fstat()` 返回的文件元信息对象，含 `size`、`mode`、`uid`、`gid`、`blocks`、`mtime`/`mtimeMs`…… 以及 `isFile()`/`isDirectory()`/`isSymbolicLink()` 等判断方法。`mtime` 是 `Date`、`mtimeMs` 是数字。
- **`Dirent`**：`readdir({withFileTypes:true})` 返回的目录项对象，含 `name` 与 `isDirectory()` 等判断方法。
- **`FileHandle`**：`fs.promises.open()` 返回的异步文件句柄，含 `fd`、`read`、`write`、`stat`、`close`。

这三类对象是「宿主对象」（C 侧持有真实数据的 JS 对象）。本节的看点是**两引擎用两套完全不同的机制实现它们**：

| 引擎 | 机制 | 数据存哪 |
|---|---|---|
| 内置引擎 | 外部原型（u5-l4）：`njs_vm_external_prototype` + `njs_vm_external_create` + 属性处理器 `njs_prop_handler_t` | C 结构体指针打标签存进 value |
| QuickJS | 注册类（QuickJS 原生 class）：`JS_NewClass` + `JS_NewObjectClass` + `JS_SetOpaque` | C 结构体指针存进对象 opaque 槽 |

#### 4.3.2 核心流程

**内置引擎的 Stats。** `njs_ext_stats[]` 是一张属性表，每个属性（`size`/`mode`/`mtime`/`mtimeMs`…）都声明同一个属性处理器 `njs_fs_stats_prop`，但带不同的 `magic32`。`magic32` 用另一个打包宏 `njs_fs_magic2(field, type)`：

\[
\text{magic32} = ((\text{type}) \ll 4) \;|\; \text{field}
\]

其中 `field`（低 4 位）选「取哪个统计字段」（DEV/INO/MODE/.../SIZE/.../ATIME/MTIME），`type`（高 4 位）选「输出成数字(0)还是 Date(1)」。于是 `mtime`(Date) 与 `mtimeMs`(number) 共用同一个 C 处理器，只差 `type` 位。读属性时，处理器解码 magic32、从 `njs_stat_t` 取出对应字段、按 type 转成 number 或 Date。

`isFile()` 等方法则是另一个处理器 `njs_fs_stats_test`，靠 `magic8` 区分要比较哪种文件类型掩码（`S_IFREG`/`S_IFDIR`/`S_IFLNK`...）。

**QuickJS 的 Stats。** 把 `Stats` 注册成一个 QuickJS 类（类 id `QJS_CORE_CLASS_ID_FS_STATS`），用 `JS_NewObjectClass` 创建实例、`JS_SetOpaque` 挂上 `qjs_stat_t*`。属性的读取则用 QuickJS 的 **exotic methods**（`get_own_property`/`get_own_property_names`），由 `qjs_fs_stats_get_own_property` 动态生成。

#### 4.3.3 源码精读

**Stats 的 C 数据结构（内置引擎）。** [external/njs_fs_module.c:95-L110](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_fs_module.c#L95-L110) 定义 `njs_stat_t`，字段几乎与 POSIX `struct stat` 一一对应（`st_size`/`st_mode`/`st_blocks`/`st_atim`/`st_mtim`…）。它就是 Stats 对象背后那块「真实数据」。属性字段编号枚举在 [external/njs_fs_module.c:113-L128](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_fs_module.c#L113-L128)。

**Stats 属性表与 magic2。** [external/njs_fs_module.c:1022-L1100](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_fs_module.c#L1022-L1100) 是 `njs_ext_stats[]` 开头部分，能看到 `atime`(Date) 与 `atimeMs`(number) 共用 `njs_fs_stats_prop`，magic32 只差高 4 位：

```c
{ .name.string = njs_str("atime"),   /* magic32 = njs_fs_magic2(NJS_FS_STAT_ATIME, 1) */ }
{ .name.string = njs_str("atimeMs"), /* magic32 = njs_fs_magic2(NJS_FS_STAT_ATIME, 0) */ }
```

**属性处理器 njs_fs_stats_prop。** [external/njs_fs_module.c:3661-L3750](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_fs_module.c#L3661-L3750) 是它的实现。先用 `njs_vm_external` 取回 value 背后的 `njs_stat_t*`，再两段 switch：

```c
switch (njs_vm_prop_magic32(prop) & 0xf) {   // 低4位：选字段
    case NJS_FS_STAT_SIZE:   v = st->st_size;         break;
    ...
    case NJS_FS_STAT_MTIME:  v = njs_fs_time_ms(&st->st_mtim); break;
}
switch (njs_vm_prop_magic32(prop) >> 4) {     // 高4位：选输出类型
    case 0: njs_value_number_set(retval, v);           break;  // number
    case 1: njs_vm_date_alloc(vm, retval, v);          break;  // Date
}
```

这是 u5-l4「属性处理器靠 magic16/magic32 复用一个 C 函数」的完美实例：**一个处理器 + 一个 magic32 = 一个属性**。`njs_fs_time_ms`（[external/njs_fs_module.c:3668](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_fs_module.c#L3668)）把 `timespec` 转成毫秒数 `tv_sec*1000 + tv_nsec/1e6`，正好是 JS `Date` 用的 epoch 毫秒。

**Stats 的判断方法。** [external/njs_fs_module.c:3613-L3657](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_fs_module.c#L3613-L3657) 是 `njs_fs_stats_test`：用 `magic8`（`DT_REG`/`DT_DIR`...）查表得到 `S_IFMT` 掩码，再比较 `st->st_mode`。`Dirent` 的 `isDirectory()` 等用的是同一套思路，见 `njs_ext_dirent[]`（[external/njs_fs_module.c:923-L994](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_fs_module.c#L923-L994)）。

**类 id 枚举（QuickJS）。** [src/qjs.h:31-L33](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.h#L31-L33) 给 fs 的三类对象各分一个类 id，紧接 `QJS_CORE_CLASS_ID_BUFFER=64` 之后递增（避开 QuickJS 内建类 1..63，这是 u6-l1 讲过的约定）：

```c
QJS_CORE_CLASS_ID_FS_STATS,
QJS_CORE_CLASS_ID_FS_DIRENT,
QJS_CORE_CLASS_ID_FS_FILEHANDLE,
```

**QuickJS 注册类。** [external/qjs_fs_module.c:346-L356](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/qjs_fs_module.c#L346-L356) 定义 `qjs_fs_stats_class`，用 QuickJS 的 `JSClassDef` + `JSClassExoticMethods`（`get_own_property`/`get_own_property_names`）来动态生成属性。这套机制与内置引擎的 `njs_prop_handler_t` 思路一致（都是「读属性时按需计算」），但走的是 QuickJS 的类系统。

**QuickJS Stats 创建。** [external/qjs_fs_module.c:1742-L1764](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/qjs_fs_module.c#L1742-L1764) 是 `qjs_fs_stats_create`：`js_malloc` 分配 `qjs_stat_t`、`JS_NewObjectClass` 建对象、`JS_SetOpaque` 挂指针。对照内置引擎的 `njs_vm_external_create(... njs_fs_stats_proto_id ...)`（[external/njs_fs_module.c:3607-L3608](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_fs_module.c#L3607-L3608)），可见两种「把 C 结构体变成 JS 对象」的写法。

**QuickJS 类与模块的注册时机。** [external/qjs_fs_module.c:2957-L3008](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/qjs_fs_module.c#L2957-L3008) 是 `qjs_fs_init`：先用 `JS_IsRegisteredClass` 判断类是否已注册（幂等），再为 `FS_STATS`/`FS_DIRENT`/`FS_FILEHANDLE` 各 `JS_NewClass` + `JS_SetClassProto`（挂上方法表），最后 `JS_NewCModule` 建模块并 `JS_AddModuleExportList` 导出 `qjs_fs_export[]`。注意它**没有 sandbox 判断**——QuickJS 侧 fs 总是被注册。

> 对照点：内置引擎 `njs_fs_init` 用「外部原型句柄（proto_id）」，QuickJS `qjs_fs_init` 用「注册类（class id）」；两者都在模块 init 时完成，但底层对象系统不同。

#### 4.3.4 代码实践

**实践目标**：从源码推断 `fs.statSync('/some/file').mtime` 与 `.mtimeMs` 的取值路径。

**操作步骤**：

1. 在 [external/njs_fs_module.c:1152-L1170](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_fs_module.c#L1152-L1170) 一带定位 `mtime` 与 `mtimeMs` 两个属性项，记下它们的 `magic32`。
2. 把 `magic32` 按公式 `((type)<<4)|field` 拆开：低 4 位应是 `NJS_FS_STAT_MTIME`，高 4 位分别是 1（mtime→Date）和 0（mtimeMs→number）。
3. 对照处理器 [external/njs_fs_module.c:3716-L3747](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_fs_module.c#L3716-L3747)，确认 `NJS_FS_STAT_MTIME` 分支取 `st->st_mtim` 并经 `njs_fs_time_ms` 转毫秒，再按高 4 位决定 number/Date。

**需要观察的现象**：`mtime` 和 `mtimeMs` 来自**同一个** `st->st_mtim`、同一个毫秒数 `v`，区别只在最后一步是用 `njs_value_number_set`（number）还是 `njs_vm_date_alloc`（Date）。

**预期结果**（待本地验证）：`statSync` 同一个文件，`st.mtimeMs` 是一个整数毫秒数，`st.mtime` 是一个 `instanceof Date` 的对象，且 `st.mtime.getTime() === st.mtimeMs`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `njs_ext_stats[]` 里 `blksize`/`blocks`/`dev` 等属性的 `magic2` 高 4 位都是 0，而 `atime`/`ctime`/`mtime`/`birthtime` 的高 4 位都是 1？

**答案**：高 4 位（`type`）决定输出类型——0 表示直接输出 number，1 表示输出 Date。`blksize`/`blocks`/`dev` 等本就是无单位整数，Node 的 Stats 也把它们定义为 number；而 `atime`/`mtime` 等时间在 Node 里定义为 `Date`（`atimeMs` 才是 number）。见 [external/njs_fs_module.c:1032-L1124](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_fs_module.c#L1032-L1124) 与处理器 [external/njs_fs_module.c:3734-L3747](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_fs_module.c#L3734-L3747)。

**练习 2**：在内置引擎里，`fs.readdirSync(dir, {withFileTypes:true})` 返回的每个 Dirent 是怎么变成 JS 对象的？`isDirectory()` 的判断发生在哪里？

**答案**：`njs_fs_readdir` 在 [external/njs_fs_module.c:3475](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_fs_module.c#L3475) 一带用 `njs_vm_external_create(vm, retval, njs_fs_dirent_proto_id, ...)` 把每条目录项造成 Dirent 对象；`isDirectory()` 由 `njs_ext_dirent[]`（[external/njs_fs_module.c:965-L974](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_fs_module.c#L965-L974)）声明，`magic8=DT_DIR`，最终在 `njs_fs_dirent_test`/`njs_fs_stats_test` 里用 `st->st_mode & S_IFMT == S_IFDIR` 比较。

### 4.4 sandbox 模式与文件访问限制

#### 4.4.1 概念说明

「sandbox」是 njs 的一个安全开关。开启后，VM 被剥夺一切「能触达宿主文件系统/网络」的能力——`fs`、`crypto`（部分）、`require('fs')` 等一律不可用。设计动机是：njs 经常执行来自配置文件或远程的不完全可信脚本，sandbox 提供一个「纯计算」的隔离环境。

本节给出一个**反直觉但重要**的事实：**sandbox 对两引擎的实现完全不同**：

- **内置引擎**：fs 模块照常编译进库，但 `njs_fs_init` 检测到 sandbox 就直接 `return NJS_OK` 跳过整个注册——于是模块表里根本没有 `"fs"`，`import fs from 'fs'` 会因找不到模块而报错。
- **QuickJS**：CLI 层面**直接拒绝** `-s` 选项。也就是说，QuickJS 不提供 sandbox 模式。

#### 4.4.2 核心流程

```
启动 CLI（内置引擎, -s）
   → njs_vm_create(..., sandbox=true)
      → 遍历模块调用 init
         → njs_fs_init: if (vm_options->sandbox) return NJS_OK;  // fs 整个不注册
   → 脚本里 import fs from 'fs'  →  模块未定义，抛异常

启动 CLI（QuickJS, -s）
   → 选项解析阶段直接报错 "option \"-s\" is not supported for quickjs" 并退出
```

#### 4.4.3 源码精读

**内置引擎的 sandbox 短路。** [external/njs_fs_module.c:3896-L3898](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_fs_module.c#L3896-L3898) 是 `njs_fs_init` 的开头：

```c
if (njs_vm_options(vm)->sandbox) {
    return NJS_OK;
}
```

注意它 `return NJS_OK`（而非 `NJS_ERROR`）——表示「初始化成功，只是我选择什么都不做」。后果是 `njs_fs_stats_proto_id` 等句柄保持为 0、`fs` 模块没被 `njs_vm_add_module`，脚本访问 fs 时拿不到模块。这是 njs 实现安全裁剪的惯用法：「不要在运行期逐个拦截，而在注册期整体不注册」。

**QuickJS 不支持 sandbox。** [external/njs_shell.c:767-L770](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L767-L770) 在选项校验阶段（当引擎选了 QuickJS 时）显式拒绝：

```c
if (opts->sandbox) {
    njs_stderror("option \"-s\" is not supported for quickjs\n");
    return NJS_ERROR;
}
```

这也解释了为什么 `qjs_fs_init` 里**没有** sandbox 判断——QuickJS 根本到不了那一步。同理，`-a`/`-d`/`-g`/`-o`/`-u` 等 njs 引擎专属选项也在这里被拒（见 u1-l4）。

> 这是「双引擎」带来的一个真实差异：同一个安全特性在一侧用「注册期跳过」实现，在另一侧用「选项期拒绝」表达。如果你写的是要兼容两引擎的库，**不能依赖 sandbox 来保证可移植性**——它在 QuickJS 下根本不存在。

#### 4.4.4 代码实践

**实践目标**：亲手观察 sandbox 下 `fs` 模块「消失」的现象（内置引擎），以及 QuickJS 拒绝 sandbox 的现象。

**操作步骤**：

1. 准备一段脚本 `demo.mjs`：
   ```js
   import fs from 'fs';
   fs.writeFileSync('/tmp/njs_fs_demo.txt', 'hello');
   console.log(fs.readFileSync('/tmp/njs_fs_demo.txt').toString());
   ```
2. 先正常构建并运行（默认内置引擎）：`./configure && make njs && ./build/njs demo.mjs`。
3. 加 `-s` 重跑（内置引擎 sandbox）：`./build/njs -s demo.mjs`。
4. 切到 QuickJS 并带 `-s`：`./build/njs -n QuickJS -s demo.mjs`（前提是构建时链接了 QuickJS，见 u1-l3）。

**需要观察的现象**：

- 步骤 2：应打印 `hello`（fs 可用）。
- 步骤 3：应报模块找不到之类的错误（fs 未注册）——这是「待本地验证」的预期。
- 步骤 4：应在启动时打印 `option "-s" is not supported for quickjs` 并退出，连脚本都不会执行。

**预期结果**（步骤 3、4 待本地验证）：步骤 3 的错误信息源于 `njs_fs_init` 跳过了 `njs_vm_add_module`，使 `import fs from 'fs'` 解析失败；步骤 4 的拒绝发生在 [external/njs_shell.c:767-L770](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L767-L770)，由 `njs_options_parse` 的兼容性校验触发。

#### 4.4.5 小练习与答案

**练习 1**：在 sandbox 模式（内置引擎）下，`njs_fs_stats_proto_id` 的值是多少？为什么？

**答案**：它是 0（静态变量初值），因为 `njs_fs_init` 在 sandbox 判断后直接 `return`，根本没执行 `njs_fs_stats_proto_id = njs_vm_external_prototype(...)`（[external/njs_fs_module.c:3896-L3904](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_fs_module.c#L3896-L3904)）。不过实际上脚本根本碰不到 Stats 对象，因为连 fs 模块都不存在。

**练习 2**：如果要在 QuickJS 下也实现一个「类 sandbox」的隔离，从本讲源码看，最自然的落点在哪里？为什么？

**答案**：从 `qjs_fs_init` 用 `qjs_modules[]` 全局数组注册模块（见 u6-l1/u6-l2）来看，最自然的做法是在创建上下文时**不注册** fs 模块（不让它进 `addons`/`qjs_modules[]`），或在 `qjs_new_context` 里不调用 `JS_AddModuleExport`。这对应内置引擎「注册期跳过」的思路；而内置引擎 sandbox 的精髓正是「不注册而非运行期拦截」。

## 5. 综合实践

把本讲三块内容串起来：读懂一段「用 Promise API 读文件、用 Stats 判断类型」的脚本背后发生了什么。

**脚本（示例代码，非项目原有）**：

```js
import fs from 'fs';

const path = '/etc/hostname';

const st = await fs.promises.stat(path);
console.log('size =', st.size, 'isFile =', st.isFile(), 'mtime =', st.mtime);

const buf = await fs.promises.readFile(path);
console.log('content =', buf.toString().trim());
```

**请完成以下追踪任务**（源码阅读型实践，逐项在源码里找到依据）：

1. **API 解析**：`fs.promises.stat` 在内置引擎里对应 `njs_ext_fs_promises[]` 中的哪一项？它的 `native` 和 `magic8` 分别是什么？（提示：[external/njs_fs_module.c:455-L464](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_fs_module.c#L455-L464) 一带；应得到 `native=njs_fs_stat`，`magic8=njs_fs_magic(NJS_FS_PROMISE, NJS_FS_STAT)`。）
2. **结果分发**：`stat` 成功后，`njs_fs_result` 的哪条分支把 Stats 对象和 resolve 绑在一起入队？（提示：[external/njs_fs_module.c:3413-L3436](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_fs_module.c#L3413-L3436)。）
3. **对象构造**：Stats 对象在哪一行被 `njs_vm_external_create` 造出来？（提示：[external/njs_fs_module.c:3607-L3608](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_fs_module.c#L3607-L3608)。）
4. **属性读取**：读 `st.size` 时，`njs_fs_stats_prop` 用 magic32 的哪一位选出 `NJS_FS_STAT_SIZE`？（提示：[external/njs_fs_module.c:3704-L3706](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_fs_module.c#L3704-L3706)。）
5. **双实现对照**：把第 1～3 步在 QuickJS 实现里重新走一遍——`fs.promises.stat` 对应 `qjs_fs_promises[]` 的哪一项？Stats 对象由哪个函数创建？（提示：[external/qjs_fs_module.c:281-L282](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/qjs_fs_module.c#L281-L282) 与 [external/qjs_fs_module.c:1742-L1764](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/qjs_fs_module.c#L1742-L1764)。）
6. **运行验证**（待本地验证）：构建 `build/njs` 后，分别用 `./build/njs demo.mjs`（默认内置引擎）与 `./build/njs -n QuickJS demo.mjs` 运行，确认两引擎下输出一致；再试 `./build/njs -s demo.mjs`，确认 sandbox 下报「模块未注册」类错误。

完成这六步，你就把「API 表面 → magic 调用约定 → 结果分发 → 对象表示 → 双实现 → sandbox」这条完整链路在源码里走通了一遍。

## 6. 本讲小结

- `fs` 模块是「双引擎＝双份代码」的典型样本：`external/njs_fs_module.c`（内置引擎，外部原型 + `njs_module_t`）与 `external/qjs_fs_module.c`（QuickJS，注册类 + `qjs_module_t`）成对存在，对外形状一致、内部机制不同。
- API 表面由「声明表」定义：内置引擎的 `njs_ext_fs[]`/`njs_ext_fs_promises[]` 与 QuickJS 的 `qjs_fs_export[]`/`qjs_fs_promises[]` 是两份必须同步维护的清单；新增/删除方法要同时改这两份表与 `auto/modules`/`auto/qjs_modules`。
- 「一个 C 函数服务多种 JS 方法」靠 `magic8`：\(\text{magic}=((\text{mode})\ll 2)|\text{calltype}\) 把调用约定（DIRECT/PROMISE/CALLBACK）和操作变体（STAT/LSTAT/FSTAT、TRUNC/APPEND）打包进一个字节，再由 `njs_fs_result` 三路分发。
- 异步 fs 方法的「异步」只是把已算好的结果投递进 `njs_vm_enqueue_job`（u4-l5 的 jobs 队列），真正的文件 IO 是同步完成的；PROMISE 分支用 `njs_value_is_error` 在 resolve/reject 间二选一。
- `Stats`/`Dirent`/`FileHandle` 是宿主对象：内置引擎用外部原型 + 属性处理器（`njs_fs_stats_prop` 靠 `magic32` 复用：低 4 位选字段、高 4 位选 number/Date），QuickJS 用注册类（`QJS_CORE_CLASS_ID_FS_*` + exotic methods）。
- sandbox 在两引擎下实现不同：内置引擎 `njs_fs_init` 检测到 sandbox 就 `return NJS_OK` 整体不注册 fs；QuickJS 则在 CLI 选项期直接拒绝 `-s`。因此 sandbox 不可作为跨引擎的可移植保证。

## 7. 下一步学习建议

- **u7-l2（crypto 与 WebCrypto）**：继续看一对「双实现」扩展模块，并第一次接触 `auto/openssl` 的可选依赖检测；fs 学到的 magic 编码、双声明表对照法可直接复用。
- **u7-l3（querystring / xml / zlib）**：了解 `auto/libxml2`、`auto/zlib` 等可选依赖如何影响扩展模块的编译；可与 fs 的「无外部依赖」对比。
- **回看 u4-l5（Promise 与 jobs）**：现在你见到了 `njs_vm_enqueue_job` 的真实调用方（`njs_fs_result`），可以更具体地理解 jobs 队列如何把同步 fs 结果「异步化」。
- **动手延伸**：仿照 `njs_fs_result` 的三路分发，尝试在脑中为 crypto 模块设计同样的 `magic` 编码；再去对照 `external/njs_crypto_module.c` 验证你的猜测。
