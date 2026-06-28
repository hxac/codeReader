# 测试体系：unit_test / lib_test / test262 / nginx-t

## 1. 本讲目标

njs 是一个语言引擎 + NGINX 集成模块的复合体，任何一处内核改动都可能牵动词法、字节码、内建对象、双引擎行为和 NGINX 集成。靠人工「点一下」不可能覆盖得住。因此 njs 维护了一套**分层的、覆盖范围互相补充**的测试体系。本讲要让你学会：

- 说清四类测试——**C 单元测试**（`unit_test`/`lib_test`）、**test262 风格 JS 测试**（`test/js/*.t.js`）、**nginx 集成测试**（`nginx/t/*.t`）、**辅助套件**（`shell_test`/`benchmark`）——各自测什么、由哪个 `make` 目标驱动、产物在哪。
- 读懂 test262 frontmatter（`includes`/`flags`/`negative`）如何被 `test/test262` 脚本解析并决定「一个 `.t.js` 文件该怎么跑」。
- 掌握**双引擎测试**的核心套路：用 `js_engine qjs;`（CLI 的 `-n QuickJS`、nginx 的 `TEST_NGINX_GLOBALS_HTTP`）把同一套用例分别在两套引擎上跑一遍。
- 具备「加一个回归用例」的最小能力，知道把它放进哪个文件、用什么格式、跑哪条命令验证。

本讲是「测试、类型定义与工程实践」单元的第一讲，依赖 u1-l3（构建运行）与 u8-l2（HTTP 模块）。读完本讲，u10-l2（TS 类型）与 u10-l4（调试技巧）就有了验收手段。

## 2. 前置知识

- **回归用例（regression test）**：修复一个 bug 后，专门写一条「专门触发该 bug」的测试并长期保留，防止以后回归。njs 仓库里大量 commit 都附带一条「在 `njs_unit_test.c` 加一条」或「在 `test/js` 加一个 `.t.js`」的改动。
- **test262**：ECMA 国际维护的 JavaScript 语言合规性官方测试套件。它的测试文件用一种 **YAML frontmatter**（文件首部 `/*--- ... ---*/`）声明元数据，例如 `flags: [async]` 表示这是一个异步测试、`negative:` 表示预期抛错。njs 把这套约定「借用」过来组织自己的 JS 测试，所以你在 `test/js` 下看到的 `*.t.js` 不是凭空发明，而是 test262 格式的本地化用例。
- **`Test::Nginx`**：Perl 社区为 nginx 写的集成测试框架（来自 `nginx-tests` 仓库）。它自动起一个 nginx 实例、注入你写的 `nginx.conf`、用 `http_get` 打真实 HTTP 请求、再用 `like()` 正则断言响应。njs 的 `nginx/t/*.t` 全部基于它。
- **`prove`**：Perl 的测试运行器，能批量跑 `*.t` 并汇总成 TAP（Test Anything Protocol）报告。
- **期望值比较**：njs 单元测试的基本范式是「跑一段脚本 → 拿到输出/异常串 → 与期望串做前缀匹配」。下文会看到它用 `njs_strstr_starts_with`（前缀匹配）而非全等，这样期望串只写错误前缀即可。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `src/test/njs_unit_test.c` | 内置引擎最大的 C 单元测试文件：约 6000 条「脚本→期望输出」用例，外加 `externals`/`module`/`shared`/`interactive`/`backtraces`/`async` 等子套件 |
| `src/test/njs_externals_test.c` | 单元测试用的「宿主对象」实现（模拟 NGINX 注入的 `$r` 等外部对象），被 `njs_unit_test` 链接复用 |
| `src/test/{rbtree,flathsh,random,unicode}_unit_test.c` | `lib_test` 的四个程序：分别压测内部红黑树、扁平哈希表、随机数、Unicode 表 |
| `src/test/njs_benchmark.c` | `make benchmark` 产物：注册一个 `benchmark` 模块，重复运行脚本测吞吐 |
| `auto/make` | 由 `./configure` 生成的 `Makefile` 模板，定义 `unit_test`/`lib_test`/`test262`/`test`/`benchmark` 等目标 |
| `test/test262` | test262 风格 JS 测试的 shell 驱动：遍历 `*.t.js`、解析 frontmatter、组装 harness、比对结果 |
| `test/options` `test/setup` `test/prepare` `test/finalize` `test/report` | 驱动的拆分片段：选项解析、用例收集、frontmatter 注入、清理、汇总 |
| `test/harness/*.js` | 测试辅助 JS：`assert.js`/`sta.js`/`doneprintHandle.js` 等，被 frontmatter 的 `includes` 引用 |
| `test/js/*.t.js` `*.t.mjs` | test262 风格用例（含 30 个 `async_*.t.js`），覆盖语言特性与内建 API |
| `test/shell_test.exp` | 用 Expect（Tcl）驱动交互式 REPL 的测试 |
| `nginx/t/*.t` | 基于 `Test::Nginx` 的 NGINX 集成测试，`js.t` 是总览入口 |

## 4. 核心概念与源码讲解

### 4.1 C 单元测试：njs_unit_test 与 lib_test

#### 4.1.1 概念说明

C 单元测试直接用 C 代码驱动引擎，是最底层、最快、反馈最直接的测试。它分两类：

1. **语言/API 单元测试**（`make unit_test`）：把「一段 JS 源码字符串」喂给引擎，跑完后拿到它的返回值或异常串，再与期望串比较。`njs_unit_test.c` 一个文件就装了约 6000 条这样的用例，覆盖语法、表达式、内建对象、字符串、正则、模块等几乎所有内置引擎行为。
2. **内部数据结构测试**（`make lib_test`）：njs 引擎内部用了自研的红黑树、扁平哈希表（flathsh）、随机数生成器、Unicode 大小写表等数据结构。`lib_test` 把它们各自编译成独立小程序做高强度压测（插入/查找/删除百万次），验证数据结构本身的正确性，与 JS 语义无关。

这两类测试都用 C 写，编译产物落在 `build/` 下，**直接以退出码 0/非 0 表示通过/失败**，非常适合在 CI 里做门禁。

#### 4.1.2 核心流程

一条 `unit_test` 用例的执行流程：

```
for 每条用例 (script, ret):
    1. njs_vm_opt_init + njs_vm_create        # 建一个全新 VM
    2. njs_vm_compile(&script)                # 编译，失败则取异常串
    3. njs_process_test:                      # 运行
         njs_vm_start(...)                     #   跑全局字节码
         while njs_vm_execute_pending_job>0:   #   排空 Promise 作业队列
    4. 拿到「实际输出串 s」 = 正常返回值 / 异常串
    5. success = njs_strstr_starts_with(s, ret)  # 前缀匹配
    6. stat->passed++ / stat->failed++
    7. njs_vm_destroy(vm)                     # 整体回收
```

第 3 步里的「排空作业队列」正是 u4-l5 讲过的 jobs 队列——单元测试必须排空它，`Promise.resolve(1).then(...)` 的回调才能执行，否则异步行为测不到。

`lib_test` 的流程更简单：每个程序就是一个 `main()`，反复调用 `njs_rbtree_*`/`njs_flathsh_*` 等接口做随机插入删除，最后断言数据结构不变量（如「中序遍历有序」「找不到已删键」），失败即 `abort()`。

#### 4.1.3 源码精读

**用例的数据结构**极其简单——一对 `njs_str_t`（脚本 + 期望输出）：

[njs_unit_test.c:53-56](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/test/njs_unit_test.c#L53-L56) —— 定义 `njs_unit_test_t`，只有 `script` 和 `ret` 两个字段。

下面是 `njs_test[]` 数组最前面的几条用例，可以看到典型写法：正常表达式期望一个值，语法错误期望一段 `SyntaxError: ...` 串：

[njs_unit_test.c:61-90](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/test/njs_unit_test.c#L61-L90) —— `njs_test[]` 头部用例：`"@"` 期望 `SyntaxError: Unexpected token "@"`，`"/***/1/*\n**/"` 期望 `"1"`（注释被词法器跳过）。

**运行函数** `njs_unit_test()` 是理解这类测试的关键。它对每条用例新建 VM、按需预编译一段 `preload`、再编译真正的脚本：

[njs_unit_test.c:22875-22920](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/test/njs_unit_test.c#L22875-L22920) —— 主循环：`njs_vm_opt_init`→`njs_vm_create`（L22885）→可选 `preload` 预编译（L22897）→对每条用例 `njs_vm_compile`（L22919）。注意 `options.unsafe`、`options.module`、`options.addons` 都按套件开关传入。

真正「跑」用例的是 `njs_process_test`，它驱动 `njs_vm_start` 后用双层循环排空 jobs 队列：

[njs_unit_test.c:22734-22772](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/test/njs_unit_test.c#L22734-L22772) —— L22734 `njs_vm_start` 跑全局代码；L22771 `njs_vm_execute_pending_job` 循环排空作业队列，让 `async`/`Promise` 用例的回调得以执行（承接 u4-l5）。

**比较与计数**：核心是用 `njs_strstr_starts_with` 做前缀匹配，再累加 `stat->passed/failed`：

[njs_unit_test.c:22956-22984](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/test/njs_unit_test.c#L22956-L22984) —— 编译失败时调 `njs_vm_exception_string` 取异常串；L22968 `njs_strstr_starts_with(&s, &tests[i].ret)` 判定通过；L22977-22980 累加计数；L22983 `njs_vm_destroy` 销毁 VM。这也解释了为什么期望串常只写错误前缀——前缀匹配足以区分类型。

**套件表**把多组用例+各自选项打包，`main()` 按名字过滤后依次运行：

[njs_unit_test.c:24175-24192](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/test/njs_unit_test.c#L24175-L24192) —— `njs_test_suite_t` 含名字、选项（`.module`/`.unsafe`/`.backtrace`/`.repeat` 等）、用例数组、运行回调。第一条套件 `"script"` 用默认的 `njs_test[]`，套件 `"module"` 则开 `.module = 1`。

[njs_unit_test.c:24288-24289](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/test/njs_unit_test.c#L24288-L24289) —— `main()`：解析 `njs_opts_t`，按 `njs_match_test` 过滤要跑的套件，最后打印 `TOTAL: PASSED/FAILED [passed/passed+failed]` 并以退出码反映结果。注意 L24280 的 `restricted_environ` 把环境变量收窄成 `TZ=UTC` 等，保证时区相关测试可复现。

**Makefile 目标**由 `./configure` 写进 `build/Makefile`。`unit_test` 只编译运行 `njs_unit_test`，`lib_test` 编译运行四个数据结构程序：

[auto/make:302-319](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/make#L302-L319) —— `lib_test` 依赖 `random_unit_test`/`rbtree_unit_test`/`flathsh_unit_test`/`unicode_unit_test` 四个程序；`unit_test` 依赖 `njs_unit_test`。两者都靠程序自身退出码判定成败。

`lib_test` 的程序入口都很短，例如扁平哈希表压测的 `main`：

[src/test/flathsh_unit_test.c:202](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/test/flathsh_unit_test.c#L202) —— `flathsh` 压测程序入口（同目录 `rbtree_unit_test.c` 的 `main` 在 [rbtree_unit_test.c:184](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/test/rbtree_unit_test.c#L184)）。它们不碰 JS，只验证 u2-l3 讲过的 `njs_flathsh_t`/`njs_rbtree_t` 数据结构。

#### 4.1.4 代码实践

**实践目标**：亲手跑通 C 单元测试，并验证「加一条用例」能被测到。

**操作步骤**：

1. 构建并跑内置引擎单元测试：
   ```sh
   ./configure && make njs
   make unit_test
   ```
2. 观察末尾的 `TOTAL: ...` 行，记录 `[通过数/总数]`。
3. 跑内部数据结构压测：
   ```sh
   make lib_test
   ```
4. （源码阅读型）打开 `src/test/njs_unit_test.c`，定位 `njs_test[]`（约 L59），仿照已有用例加一条，例如：
   ```c
   { njs_str("1 + 2 * 3"),
     njs_str("7") },
   ```
5. 重新 `make unit_test`，确认总数 +1 且仍 PASSED。

**需要观察的现象**：
- `unit_test` 会按套件逐段打印，每段形如 `script ...... [N/M]`，最后给出 `TOTAL`。
- `lib_test` 四个程序各自打印插入/查找统计，无 FAILED 即通过。

**预期结果**：`make unit_test` 末尾出现 `TOTAL: PASSED [...]`；`make lib_test` 四个程序全部无错退出。若改了用例，总数应精确 +1。

> 待本地验证：受运行环境影响，确切的 `[通过/总数]` 数字以你本机构建为准；本讲引用的行号基于 HEAD `f078f143`。

#### 4.1.5 小练习与答案

**练习 1**：为什么期望串比较用「前缀匹配」`njs_strstr_starts_with` 而不是全等？  
**答案**：异常信息常含平台相关细节（如正则引擎、行号、附加原因），写全等会脆弱。前缀匹配只需锁定错误类型前缀（如 `TypeError: Cannot read property`），既能区分行为又足够稳健。

**练习 2**：`main()` 里的 `restricted_environ` 把环境设成 `TZ=UTC` 有什么用？  
**答案**：`Date`、时区格式化等用例对时区敏感；固定 `TZ=UTC` 可让测试结果在任何机器上都一致，消除「本地时区导致用例忽过忽挂」的噪音。

**练习 3**：`lib_test` 测的是「JS 语义」还是「C 数据结构」？  
**答案**：是 C 数据结构。它直接调用 `njs_rbtree_*`/`njs_flathsh_*` 等 C 接口做大规模增删查，验证 u2-l3 的红黑树与扁平哈希表本身正确，与 JS 行为无关。

---

### 4.2 test262 风格 JS 测试：test/js 与 test/test262 驱动

#### 4.2.1 概念说明

C 单元测试虽快，但每加一条用例都要在 C 文件里写一对 `njs_str_t`，对复杂场景（多文件模块、异步、需要 `assert` 库）不友好。于是 njs 引入了第二层：**用 JavaScript 本身写的测试**，放在 `test/js/` 下，文件名以 `.t.js`（普通脚本）或 `.t.mjs`（ES 模块）结尾。

这些文件**借用 test262 的 frontmatter 约定**：在文件首部用 `/*--- ... ---*/` 声明元数据，最常用的是：

- `includes: [foo.js]`：本用例需要预先拼接进 `test/harness/foo.js`（如 `assert.js` 提供 `assert`，`compareArray.js` 提供 `assert.compareArray`）。
- `flags: [async]`：这是一个异步测试，通过 `$DONE` 通知完成。
- `negative:` + `phase:`：预期在某阶段抛错。

驱动这些文件的不是 C，而是一个 shell 脚本 `test/test262`：它遍历所有 `.t.js`/`.t.mjs`，逐个解析 frontmatter、把 harness 拼到用例前面、用 `build/njs` 运行、按退出码与 stdout 判定通过。`make test262` 就是它的入口。

#### 4.2.2 核心流程

```
test/test262 --binary=build/njs
  │
  ├─ test/options   解析 --binary/--log 等，确定 NJS_TEST_BINARY、NJS_TEST_EXIT_CODE
  ├─ test/setup     收集 NJS_TESTS：find $path -name '*.t.js' -o -name '*.t.mjs'
  │
  for njs_test in $NJS_TESTS:
  │   ├─ test/prepare  解析该文件的 frontmatter：
  │   │                   includes → 把 test/harness/*.js 拼到文件头
  │   │                   flags:async → 额外拼 compatPrint.js + doneprintHandle.js
  │   │                   negative → 记录预期抛错
  │   ├─ 运行：$NJS_TEST_BINARY 文件  （退出码 status）
  │   └─ 判定：
  │        正常用例：stdout 必须为空（脚本不该打印任何东西）→ passed
  │        async 用例：stdout 必须是 'Test262:AsyncTestComplete' → passed
  │        negative 用例：status 必须等于 NJS_TEST_EXIT_CODE → passed
  │
  ├─ test/finalize   清理临时目录
  └─ test/report     打印 TOTAL: PASSED/FAILED [passed/total]
```

关键约定：**普通用例「不打印即通过」**。断言失败会 `throw`，引擎以非零码退出；只要打印了任何东西就算失败。异步用例则用 `$DONE` 打印一个魔法串 `Test262:AsyncTestComplete` 表示成功收尾。

#### 4.2.3 源码精读

**驱动主循环** `test/test262`：

[test/test262:13-57](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/test/test262#L13-L57) —— 对每个用例先 `. test/prepare` 注入 harness（L15），再用 `$NJS_TEST_BINARY` 运行（L31-33）。L38-57 是判定三态：async 用例要求 stdout 恰为 `Test262:AsyncTestComplete`，普通用例要求 stdout 为空，negative 用例要求退出码等于 `NJS_TEST_EXIT_CODE`。`NJS_SKIP_LIST`（L24）用于临时跳过已知失败用例（如 QuickJS 下两条已知挂的 async 用例）。

**frontmatter 注入** `test/prepare`：

[test/prepare:6-37](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/test/prepare#L6-L37) —— 用 `grep`+`sed` 从用例文件里抠出 `includes`/`paths`/`flags`/`negative` 字段（L6-17）；`flags: [async]` 时额外把 `compatPrint.js doneprintHandle.js` 加进 includes（L20-28）；最后 `cat $njs_inc $njs_test > 临时文件`（L37）把 harness 拼在用例之前。这就是为什么你的 `.t.js` 里能直接用 `assert`、`$DONE`——它们是拼接进来的。

**用例收集** `test/setup`：

[test/setup:38-48](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/test/setup#L38-L48) —— `find $arg -name '*.t.js' -o -name '*.t.mjs'` 递归收集用例并排序，结果存入 `NJS_TESTS`。

**一个真实异步用例** `test/js/async_try_catch.t.js`：

[test/js/async_try_catch.t.js:1-31](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/test/js/async_try_catch.t.js#L1-L31) —— 顶部 frontmatter 声明 `includes: [compareArray.js]`、`flags: [async]`（L1-4）。脚本里 `af()` 是 async 函数，`.then(...)` 里用 `assert.compareArray` 断言执行阶段顺序，最后 `.then($DONE, $DONE)`（L30）。`$DONE` 成功时打印 `Test262:AsyncTestComplete`，失败打印 `Test262:AsyncTestFailure:...`，正是 `test/test262` 判定的依据。

**harness 文件**——`$DONE` 的定义：

[test/harness/doneprintHandle.js:8-22](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/test/harness/doneprintHandle.js#L8-L22) —— `$DONE(error)` 在成功时打印魔法串 `Test262:AsyncTestComplete`，失败时打印 `Test262:AsyncTestFailure:...`。这就是驱动器与用例之间的「握手段」。

[test/harness/sta.js:25-27](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/test/harness/sta.js#L25-L27) —— `$DONOTEVALUATE()`：抛出「本语句不该被执行」标记，常用于「如果走到了这一行就算失败」的断言点（如 `async_try_catch.t.js` 的 L14）。

**Makefile 目标**：

[auto/make:313-314](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/make#L313-L314) —— `test262_njs: njs` 然后 `test/test262 --binary=$NJS_BUILD_DIR/njs`，即用内置引擎跑全部用例。

#### 4.2.4 代码实践

**实践目标**：跑一遍 test262 风格用例，并读懂一条 async 用例的 frontmatter 与断言。

**操作步骤**：

1. 构建 CLI：`./configure && make njs`
2. 跑全部 JS 用例（内置引擎）：`make test262`（等价 `test/test262 --binary=build/njs`）
3. 只跑某一条用例，便于观察：
   ```sh
   NJS_TEST_PATHS=test/js/async_try_catch.t.js test/test262 --binary=build/njs
   ```
   > 注：`NJS_TEST_PATHS` 在 `test/options` 中由命令行剩余参数决定（默认 `test`），`test/setup` 据此 `find`。具体传参以本地 `test/test262 --help` 为准。
4. 打开 [test/js/async_try_catch.t.js](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/test/js/async_try_catch.t.js)，对照本讲 4.2.3 逐行标注：frontmatter 的 `includes`/`flags`、`$DONOTEVALUATE` 的守卫位置、`assert.compareArray` 的断言、`$DONE` 的收尾。

**需要观察的现象**：
- 第 2 步末尾出现 `TOTAL: PASSED/FAILED [passed/total]`，总数应与 `test/js` 下 `.t.js`+`.t.mjs` 文件数（约百余个）量级一致。
- 第 3 步若用例通过，stdout 应只含 `Test262:AsyncTestComplete`。

**预期结果**：`make test262` 全绿；单独跑 `async_try_catch.t.js` 通过。

> 待本地验证：`NJS_TEST_PATHS` 的确切传参语法请以 `test/test262 --help` 与 `test/options` 为准。

#### 4.2.5 小练习与答案

**练习 1**：一个 `.t.js` 普通用例（非 async）「通过」的判定标准是什么？  
**答案**：进程退出码为 0 **且** stdout 完全为空。因为脚本不应主动打印；`assert` 失败会 `throw` 导致非零退出，所以「没打印 + 正常退出」即代表断言全过。

**练习 2**：`includes: [compareArray.js]` 是怎么生效的？  
**答案**：`test/prepare` 用 `grep`/`sed` 抠出该字段，把 `test/harness/compareArray.js` 连同默认的 `assert.js`、`sta.js` 用 `cat` 拼到用例文件最前面，再交给 `build/njs` 运行。于是用例里 `assert.compareArray` 才有定义。

**练习 3**：`$DONE` 在成功和失败时分别打印什么？驱动器据此如何判定？  
**答案**：成功打印 `Test262:AsyncTestComplete`，失败打印 `Test262:AsyncTestFailure:<name>: <msg>`。驱动器对 async 用例要求 stdout 恰为 `Test262:AsyncTestComplete` 才算 passed（见 `test/test262` L42-48）。

---

### 4.3 nginx 集成测试：nginx/t 与 Perl Test::Nginx

#### 4.3.1 概念说明

前面两层都只测「JS 引擎本身」，不涉及 NGINX。但 njs 的一半价值在于它**嵌在 NGINX 里**——`js_content`/`js_set`/`js_access` 等指令是否正确触发、`r` 对象的方法是否工作、与 nginx 配置的交互是否正常，只有把真 nginx 起起来、打真 HTTP 请求才能验证。这就是 `nginx/t/*.t` 的职责。

这些 `.t` 文件是 **Perl 脚本**，基于 `Test::Nginx` 框架（来自 `nginx-tests` 仓库）。每个文件：

1. 用 heredoc 写一份 `nginx.conf`（含 `js_import`、`js_content`、`js_set` 等指令）；
2. 写一份被 import 的 `.js`（含若干 handler 函数）；
3. 用 `http_get()`/`http_post()` 打真实请求；
4. 用 `like($resp, qr/.../, '说明')` 正则断言响应；
5. 用 `$t->read_file('error.log')` 检查日志里的预期串（如 backtrace）。

用 `prove` 批量运行，输出标准 TAP 报告。

#### 4.3.2 核心流程

```
prove -r -I /path/to/nginx-tests/lib nginx/t
  │
  for 每个 *.t:
  │   1. Test::Nginx->new()->has(qw/http rewrite/)  # 检查 nginx 是否编了对应模块
  │   2. write_file_expand('nginx.conf', ...)        # 写入用例自带的 nginx.conf
  │   3. write_file('test.js', ...)                  # 写入 handler 脚本
  │   4. try_run()->plan(N)                          # 启 nginx、规划 N 条断言
  │   5. http_get('/uri') -> like(qr/.../)            # 打请求 + 断言
  │   6. $t->stop(); read error.log 断言             # 查日志
  │
  └─ 汇总成 TAP：ok N / not ok N
```

关键点：`%%TEST_GLOBALS_HTTP%%` 是占位符，`prove` 运行时可用环境变量 `TEST_NGINX_GLOBALS_HTTP='js_engine qjs;'` 把内容**注入到每个用例的 `http {}` 块顶部**——这正是「用同一套 `.t` 跑 QuickJS 引擎」的双引擎测试入口。

#### 4.3.3 源码精读

**用例骨架** `nginx/t/js.t`——它测 `r` 对象的一组核心属性/方法：

[nginx/t/js.t:11-27](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/t/js.t#L11-L27) —— 引入 `Test::Nginx`，构造测试对象并写 `nginx.conf`。`%%TEST_GLOBALS%%` 与 `%%TEST_GLOBALS_HTTP%%`（L30、L38）是 `prove` 注入点。

**配置段**展示了 NGINX 集成的典型写法：

[nginx/t/js.t:37-60](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/t/js.t#L37-L60) —— `js_set $test_method test.method;`（L40）把变量绑定到 JS 函数；`js_import test.js;`（L52）导入模块；`location /njs { js_content test.njs; }`（L58-60）把 location 内容生成交给 JS handler。这正是 u8-l2 讲过的「指令→phase」绑定在配置层的体现。

**handler 脚本**：`js.t` 在 [nginx/t/js.t:139-245](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/t/js.t#L139-L245) 用 heredoc 写入 `test.js`，定义 `method(r){return 'method='+r.method}`、`async function internal(r){ let reply = await r.subrequest('/sub_internal'); ... }` 等函数，最后 `export default {...}` 导出。

**断言**——打真实请求并用正则验证：

[nginx/t/js.t:251-289](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/t/js.t#L251-L289) —— `like(http_get('/method'), qr/method=GET/, 'r.method')` 验证 `r.method`；`like(http_get('/internal'), qr/parent: false sub: true/, 'r.internal')` 验证 `r.subrequest` 的异步子请求结果。`TODO { ... }` 块用于「低版本 nginx 上预期失败」的兼容处理。

**日志断言**——验证 JS 抛错后的 backtrace 真的进了 error.log：

[nginx/t/js.t:314-318](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/t/js.t#L314-L318) —— `ok(index($t->read_file('error.log'), 'SEE-LOG') > 0, 'log js')` 确认 `r.log('SEE-LOG')` 写进了日志；后两条验证 `js_set`/`js_content` 抛错时的调用栈回溯（承接 u4-l4 异常机制）。

**运行说明**：

[nginx/t/README:8-12](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/t/README#L8-L12) —— 明确说明依赖 `nginx-tests` 仓库的测试库，运行命令是 `TEST_NGINX_BINARY=/path/to/nginx prove -r -I /path/to/nginx-tests/lib/ nginx/t`。

#### 4.3.4 代码实践

**实践目标**：跑通 nginx 集成测试，并切换引擎观察双引擎覆盖。

**操作步骤**：

> 前置：需要一个编入了 njs `ngx_http_js_module` 的 nginx 可执行文件（按 u1-l2/CLAUDE.md 用 `--add-module` 配置 nginx），以及 `nginx-tests` 仓库。若本机无此环境，下面的步骤改为「源码阅读型」。

1. 用内置引擎跑 `js.t`：
   ```sh
   TEST_NGINX_BINARY=/path/to/nginx \
   prove -r -I /path/to/nginx-tests/lib nginx/t/js.t
   ```
2. 切换到 QuickJS 引擎再跑一遍（同一份 `.t`，注入不同 globals）：
   ```sh
   TEST_NGINX_BINARY=/path/to/nginx \
   TEST_NGINX_GLOBALS_HTTP='js_engine qjs;' \
   prove -r -I /path/to/nginx-tests/lib nginx/t/js.t
   ```
3. 保留 nginx 测试现场以便排错（u10-l4 会用到）：
   ```sh
   TEST_NGINX_LEAVE=1 prove -I /path/to/nginx-tests/lib nginx/t/js.t
   # 然后查看保留下来的 nginx.conf 与 error.log
   ```
4. （源码阅读型）打开 [nginx/t/js.t](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/t/js.t)，定位 `js_set`/`js_content` 指令，对照 u8-l2 画出「指令→phase→JS 函数」的对应表。

**需要观察的现象**：
- 第 1、2 步都应出现 `ok N` 系列，结尾 `All tests successful`。
- 对比两次输出：用例数应一致（说明同一份测试覆盖两引擎），但个别用例可能因引擎能力差异而用 `TODO` 跳过或结果不同。

**预期结果**：两引擎下 `js.t` 均全绿（或仅有标注的低版本 TODO）。

> 待本地验证：`nginx-tests` 路径与 nginx 二进制路径以本机为准；若未构建带 njs 的 nginx，本实践退化为源码阅读。

#### 4.3.5 小练习与答案

**练习 1**：`nginx/t/*.t` 与 `test/js/*.t.js` 最大的区别是什么？  
**答案**：前者是 Perl 脚本，基于 `Test::Nginx` 起真 nginx、打真 HTTP 请求，验证**集成行为**（指令绑定、`r` 对象、phase）；后者是纯 JS 文件，只验证**引擎语义**，不碰 nginx。

**练习 2**：怎样用同一份 `nginx/t/*.t` 把 QuickJS 引擎也测一遍？  
**答案**：运行时设 `TEST_NGINX_GLOBALS_HTTP='js_engine qjs;'`。`prove` 会把该内容注入每个用例 `nginx.conf` 的 `%%TEST_GLOBALS_HTTP%%` 占位处（即 `http {}` 块顶部），使 `js_engine` 指令对整批用例生效。

**练习 3**：`like(http_get('/method'), qr/method=GET/, 'r.method')` 中的 `http_get` 是谁提供的？  
**答案**：由 `Test::Nginx` 框架（`nginx-tests` 库）提供，它内部向测试用 nginx 发起一次真实 HTTP GET 并返回响应体；`like` 是 Perl `Test::More` 的正则断言。

---

### 4.4 双引擎测试与辅助套件（shell_test、benchmark）

#### 4.4.1 概念说明

前三个模块覆盖了主战场，本模块补齐两块：

- **双引擎测试**：njs 内置两套引擎（u6 系列讲过），测试体系必须让**同一批用例在两引擎上各跑一遍**。C 单元测试主要面向内置引擎；test262 套件和 nginx 集成测试都能切换到 QuickJS。本模块点明各层的「切引擎开关」。
- **辅助套件**：
  - `shell_test`（Expect）测**交互式 REPL** 行为——逐字符输入、多行续行、提示符回显，这些用 C 单元测试不方便表达。
  - `benchmark` 不是正确性测试，而是性能基线，重复运行脚本测吞吐，用于回归「改了实现后有没有变慢」。

#### 4.4.2 核心流程

```
make test           = shell_test + unit_test + test262       # 内置引擎一站式
make test262        = test262_njs + test262_quickjs(若有)    # 两引擎各跑 JS 用例
  test262_quickjs:  NJS_SKIP_LIST="..." test/test262 --binary='build/njs -n QuickJS -m'
make benchmark      = 运行 build/njs_benchmark              # 性能基线
```

切引擎的三种入口：

| 层 | 内置引擎 | QuickJS |
|---|---|---|
| C `unit_test` | 默认 | 主要靠 test262/nginx 覆盖（unit_test 面向内置引擎） |
| test262 JS | `--binary=build/njs` | `--binary='build/njs -n QuickJS -m'` |
| nginx 集成 | 默认 | `TEST_NGINX_GLOBALS_HTTP='js_engine qjs;'` |
| CLI 交互 | `shell_test.exp` 默认 | `spawn njs -n QuickJS` |

#### 4.4.3 源码精读

**`make test` 的组成**：

[auto/make:321](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/make#L321) —— `test: shell_test unit_test test262`，把交互式、单元、JS 三类串成一个目标。

**QuickJS 版 test262**——同一套用例换引擎跑，并跳过两条已知不兼容项：

[auto/make:339-345](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/make#L339-L345) —— `test262` 依赖 `test262_njs test262_quickjs`；后者用 `--binary='$NJS_BUILD_DIR/njs -n QuickJS -m'` 切到 QuickJS，并通过 `NJS_SKIP_LIST` 跳过 `promise_rejection_tracker_recursive.t.js` 与 `async_exception_in_await.t.js`（QuickJS 语义差异）。这正体现了「双引擎＝双份关注」：用例可共享，但差异点要显式标注。注意该目标只在 `NJS_HAVE_QUICKJS=YES`（构建期链接了 QuickJS）时才生成（见 L336）。

**交互式 REPL 测试** `test/shell_test.exp`——用 Tcl/Expect 模拟终端逐字符输入：

[test/shell_test.exp:6-27](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/test/shell_test.exp#L6-L27) —— `njs_test` 过程用 `spawn njs` 启动 REPL，`expect` 匹配提示符 `>> `，再循环 `send`/`expect` 逐条输入并校验回显。第 36-39 行的用例验证 `njs.version` 匹配 `*.*.*`。这种「逐字符 + 回显」的细粒度交互行为，正是 C 单元测试难以覆盖、必须用 Expect 的原因。

**性能基线** `njs_benchmark.c`——注册一个 `benchmark` 模块，把脚本重复跑很多次计时：

[src/test/njs_benchmark.c:62-73](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/test/njs_benchmark.c#L62-L73) —— `njs_benchmark_module` 是一个标准 `njs_module_t`（u6-l2 讲过注册结构），提供 `benchmark.string()` 方法供脚本调用测吞吐。`make benchmark`（[auto/make:323-326](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/make#L323-L326)）编译运行它。

#### 4.4.4 代码实践

**实践目标**：分别在内置引擎与 QuickJS 下跑 test262，并体验交互式测试。

**操作步骤**：

1. 用 `--with-quickjs`（或本机已有 libquickjs）配置并构建，确认 `NJS_HAVE_QUICKJS=YES`：
   ```sh
   ./configure --with-quickjs && make njs
   ```
2. 跑双引擎 test262（会先后跑 njs 与 QuickJS 两轮）：
   ```sh
   make test262
   ```
3. 手动只跑 QuickJS 轮，便于对照：
   ```sh
   NJS_SKIP_LIST="test/js/promise_rejection_tracker_recursive.t.js test/js/async_exception_in_await.t.js" \
     test/test262 --binary='build/njs -n QuickJS -m'
   ```
4. （可选）跑交互式测试需 Expect：`make test` 会包含 `shell_test`；或单独 `prove test/shell_test.exp`（具体子目标以本地 Makefile 为准）。
5. 跑性能基线（如本机已构建）：`make benchmark`

**需要观察的现象**：
- 第 2 步会看到两段 `TOTAL:`——分别对应内置引擎与 QuickJS。
- 第 3 步 QuickJS 轮的用例数应与内置引擎轮接近；`NJS_SKIP_LIST` 中的两条被 `skip`。

**预期结果**：两引擎 test262 均 PASSED（QuickJS 轮跳过两条已知项）。

> 待本地验证：是否链接了 QuickJS 取决于本机环境；若无 QuickJS，`make test262` 只跑 `test262_njs`。

#### 4.4.5 小练习与答案

**练习 1**：`make test` 包含哪三部分？为什么把 `shell_test` 也算进去？  
**答案**：`shell_test unit_test test262`。`shell_test` 用 Expect 测交互式 REPL 的逐字符回显与多行续行，这类终端交互行为 C 单元测试无法表达，需单独覆盖。

**练习 2**：`make test262` 在 QuickJS 下为什么要设 `NJS_SKIP_LIST`？  
**答案**：`promise_rejection_tracker_recursive.t.js` 与 `async_exception_in_await.t.js` 在 QuickJS 上的语义与内置引擎不同（unhandled rejection / await 异常处理差异），属已知的引擎差异而非 bug，故显式跳过，避免噪声。

**练习 3**：`benchmark` 测的是正确性还是性能？它和 `unit_test` 有何不同？  
**答案**：测性能（吞吐基线），不判对错。`unit_test` 比对输出串判正确性；`benchmark` 重复运行脚本只计时，用于回归「实现重构后是否变慢」。

## 5. 综合实践

**任务**：为「`Array.prototype.flat` 在内置引擎上的一个行为」补一条覆盖四层中至少两层的回归用例，并分别在两引擎验证。

**步骤**：

1. **C 单元测试层**：在 [src/test/njs_unit_test.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/test/njs_unit_test.c) 的 `njs_test[]`（约 L59 起）数组里，仿照已有用例加一条，例如：
   ```c
   { njs_str("[1,[2,[3]]].flat(Infinity)"),
     njs_str("1,2,3") },
   ```
   运行 `make unit_test`，确认总数 +1 且 PASSED。
2. **test262 JS 层**：在 `test/js/` 新建 `flat_infinity.t.js`，用 frontmatter + `assert`：
   ```js
   /*---
   includes: [compareArray.js]
   ---*/
   assert.compareArray([1,[2,[3]]].flat(Infinity), [1,2,3]);
   ```
   运行 `test/test262 --binary=build/njs test/js/flat_infinity.t.js`（传参以本地为准），应 PASSED。
3. **双引擎对照**：用 `--binary='build/njs -n QuickJS -m'` 再跑一次第 2 步的用例，确认 QuickJS 也通过。
4. **思考题**：这条用例要不要进 `nginx/t`？为什么？  
   *参考答案*：不需要。`flat` 是纯语言特性，与 NGINX 集成无关；`nginx/t` 应留给「指令绑定 / `r`/`s` 对象 / phase」这类集成行为，否则徒增起 nginx 的开销。

> 待本地验证：`.flat(Infinity)` 的支持情况以本机构建的引擎版本为准；若某引擎不支持，可改用 `.flat(2)`。

## 6. 本讲小结

- njs 的测试体系是**分层互补**的：C `unit_test`/`lib_test` 测引擎语义与内部数据结构，`test/js` 的 test262 风格 `.t.js` 测语言/API，`nginx/t` 的 Perl `Test::Nginx` 测 NGINX 集成，`shell_test`/`benchmark` 补交互与性能。
- C 单元测试的范式是「跑脚本→取输出/异常串→`njs_strstr_starts_with` 前缀匹配」，并在 `njs_process_test` 里循环 `njs_vm_execute_pending_job` 排空作业队列以支持异步用例（承接 u4-l5）。
- `make` 目标由 `./configure` 写进 `build/Makefile`：`unit_test`/`lib_test`/`test262`/`test`/`benchmark`，分别对应不同测试层。
- test262 frontmatter（`includes`/`flags`/`negative`）由 `test/prepare` 解析并把 `test/harness/*.js` 拼到用例前；普通用例「不打印即通过」，异步用例靠 `$DONE` 打印 `Test262:AsyncTestComplete`。
- **双引擎测试**是贯穿各层的主题：test262 用 `--binary='build/njs -n QuickJS -m'`，nginx 集成用 `TEST_NGINX_GLOBALS_HTTP='js_engine qjs;'`，差异点用 `NJS_SKIP_LIST` 显式标注。

## 7. 下一步学习建议

- **u10-l2 TypeScript 类型定义**：测试保证了运行时行为正确，`ts/` 则给出编译期类型契约，两者互补；学完类型后可尝试为你的回归用例补一份 `.d.ts`。
- **u10-l4 调试技巧**：本讲多次提到 `TEST_NGINX_LEAVE=1`、`-d` 反汇编等手段，下一讲会把它们系统化；建议先回到 u3-l5、u4-l1 复习字节码与解释器，再学调试。
- **深读测试源码**：想更懂引擎，最好的办法是「读一条失败用例的修复 commit」。可在仓库 `CHANGES` 或 `git log` 里找形如 `Fix: ...` 的提交，对照本讲定位它改了 `njs_unit_test.c` 还是 `test/js`，反推 bug 性质。
- **参与贡献**：`CONTRIBUTING.md` 指出修复应附回归用例；按本讲的分层原则选择最合适的测试层（语言→unit/test262，集成→nginx/t），能让你的 PR 更易被接受。
