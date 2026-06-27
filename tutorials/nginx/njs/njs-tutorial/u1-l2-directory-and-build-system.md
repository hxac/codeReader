# 源码目录结构与构建系统总览

## 1. 本讲目标

上一讲我们已经知道：njs 是一个深度集成进 NGINX 的 JavaScript 引擎，既能作为 NGINX 模块运行，也能作为独立 CLI 使用。本讲我们要回答一个工程层面的问题——**这套东西在仓库里是怎么摆放的？又是怎么从源码变成可执行程序的？**

学完本讲你应该能够：

- 建立 `src` / `external` / `nginx` / `ts` / `test` / `auto` 六大顶层目录的职责心智地图，拿到一个文件名就能猜出它属于哪一层。
- 说清楚 `./configure` 这一个 shell 命令内部依次做了哪些事，最终如何吐出 `build/Makefile`。
- 理解 `auto/sources`、`auto/modules`、`auto/qjs_modules` 这三类「清单文件」的作用，明白 njs 是如何用 shell 变量来管理「编译哪些源文件」「注册哪些扩展模块」的。

本讲不涉及任何 C 语言细节，只关心「工程骨架」。它是后续进入 `src/` 内核源码前的导航图。

## 2. 前置知识

- **会一点 shell。** njs 没有使用 CMake、Make、Autotools 这些通用构建工具，而是自研了一套**纯 shell 脚本**的构建系统。你需要看得懂 `. auto/options`（在 shell 里就是 `source`，即「在当前 shell 中执行该脚本」）和 `NJS_LIB_SRCS="..."`（给变量赋一个多行字符串）。
- **懂一点 Makefile。** `configure` 的最终产物是一个 `build/Makefile`，再用 `make` 把 `.c` 编译成 `.o` 并链接成 `build/njs`。你只要知道「目标: 依赖」+「缩进的命令行」这种基本语法即可。
- **回顾上一讲。** 记住 njs 有「内置 njs 引擎」与「QuickJS 引擎」两套可互换引擎，本讲你会看到这套「双引擎」是如何在目录和构建清单里体现为「双份代码」的。

## 3. 本讲源码地图

本讲围绕「构建系统」展开，涉及的关键文件都集中在仓库根目录和 `auto/` 下：

| 文件 | 作用 |
|---|---|
| `configure` | 构建入口脚本（66 行）。按固定顺序 `source` 一连串 `auto/*.sh` 脚本，最终生成 `build/Makefile`。 |
| `auto/sources` | **内核源码清单**。用一个 shell 变量 `NJS_LIB_SRCS` 列出引擎核心要编译的所有 `.c` 文件。 |
| `auto/modules` | **njs 引擎扩展模块清单**。声明 `njs_buffer` / `njs_fs` / `njs_crypto` 等模块，按条件注册。 |
| `auto/qjs_modules` | **QuickJS 引擎扩展模块清单**。声明 `qjs_*` 版本的同样模块。 |
| `auto/module`、`auto/qjs_module` | 两个小辅助脚本（各 7 行），把单个模块追加进对应清单。 |
| `auto/make` | **Makefile 生成器**。读清单变量，逐条写出编译规则，并生成 `njs_modules.c` / `qjs_modules.c` 两个胶水文件。 |
| `auto/init`、`auto/options`、`auto/summary` | 变量初始化、命令行选项解析、构建摘要输出。 |

> 提示：`auto/` 目录下还有 `cc`、`os`、`pcre`、`openssl`、`libxml2`、`zlib`、`quickjs` 等几十个脚本，它们是「特性检测」脚本，本讲只在「加载流程」里点到，不展开。

## 4. 核心概念与源码讲解

### 4.1 目录职责划分

#### 4.1.1 概念说明

njs 是一个体量大、层次多的项目。为了避免「所有东西都堆在一个文件夹」，它把代码按职责拆成了几个顶层目录。理解这套划分，是你日后定位代码的前提：

- **`src/`——引擎内核。** 这是 njs 的心脏：词法分析、语法解析、字节码生成、解释器执行、对象模型、所有内建类型（string/array/promise/...）都在这里。它同时也包含 QuickJS 的**包装层**（`src/qjs.c`、`src/qjs_buffer.c`、`src/quickjs_compat.h`）。内核自带一套单元测试在 `src/test/`。
- **`external/`——扩展模块。** 这是「内核之外、用 JS 模块形式暴露的能力」，例如文件系统 `fs`、加密 `crypto`/`webcrypto`、`querystring`、`xml`、`zlib`。关键是：**这里每个功能都有两份实现**，文件名前缀分别是 `njs_`（给内置引擎）和 `qjs_`（给 QuickJS 引擎）。这个目录还放 CLI 的入口 `njs_shell.c`。
- **`nginx/`——NGINX 集成。** 把 njs 接入 NGINX 的两个模块（`ngx_http_js_module.c`、`ngx_stream_js_module.c`），以及它们共享的绑定基座（`ngx_js.c`/`ngx_js.h`）、fetch 客户端、共享字典、表单解析等。测试在 `nginx/t/`。
- **`ts/`——TypeScript 类型定义。** 一组 `.d.ts` 文件，是两套引擎**共用的权威 API 描述**，会被打包成 `njs-types` npm 包。
- **`test/`——JS 层与集成测试。** 包括 test262 风格的 `*.t.js`、shell 交互测试 `shell_test.exp`、各模块测试（`fs/`、`webcrypto/`、`xml/`）和 TS 测试（`test/ts/`）。
- **`auto/`——构建系统。** 上一节已述。
- 另有 `docs/`（文档，含 `docs/agent/`）、`utils/`（辅助脚本，如生成关键字表的 `lexer_keyword.py`）。

一句话记忆：**`src` 是引擎，`external` 是扩展，`nginx` 是宿主集成，`ts`/`test` 是描述与验证，`auto` 是把它们编译到一起的胶水。**

#### 4.1.2 核心流程

仓库根目录的文件可以分成三类：

```text
顶层文件分类
├── 入口与构建:  configure, auto/*          ← 本讲主角
├── 源码主体:    src/, external/, nginx/    ← 后续各单元的主角
└── 描述与测试:  ts/, test/, docs/, utils/
```

它们最终被「编译」成两类产物：

```text
build/  (configure + make 的产物目录)
├── Makefile          ← auto/make 生成
├── njs_auto_config.h ← configure 写入的特性宏
├── libnjs.a          ← src/ 内核 + njs_* 扩展 打包的静态库
├── libqjs.a          ← src/qjs.c + qjs_* 扩展 打包的静态库 (仅当启用 QuickJS)
└── njs               ← CLI，链接 external/njs_shell.c + 上面两个库
```

注意「双引擎 = 双份产物」：内核库 `libnjs.a` 与 QuickJS 库 `libqjs.a` 是分开打包的，CLI `njs` 在链接时同时带上两者，运行时再用 `-n` 选择其一（下一讲详解）。

#### 4.1.3 源码精读

我们用一次 `ls` 来印证上面的划分。下面是仓库顶层目录的真实内容：

```text
AGENTS.md  CHANGES  CLAUDE.md  ...  README.md  ...
auto/      configure    docs/   external/   nginx/
njs-tutorial/   src/   test/   ts/   utils/
```

进入 `src/` 你会看到内核源码，其中既有 `njs_*.c`（内置引擎），也有 QuickJS 包装层：

- [src/:njs_vm.c 等内核文件](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c) —— `src/` 顶层共有约 54 个 `.c` 文件，覆盖值表示、VM、字节码、编译前端与全部内建类型。
- [src/qjs.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.c) —— QuickJS 包装层，与 `src/quickjs_compat.h` 配套。

进入 `external/` 你会清楚看到「双份实现」的命名约定：

- [external/njs_fs_module.c 与 external/qjs_fs_module.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_fs_module.c) —— 同一个 `fs` 模块的两份实现，分别服务两套引擎。
- [external/njs_shell.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c) —— 独立 CLI 的 `main()` 入口（下一讲精读）。

`nginx/` 则把集成代码集中在一起：

- [nginx/ngx_js.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.c) —— http/stream 两模块共享的绑定基座。
- [nginx/ngx_http_js_module.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c) 与 `nginx/ngx_stream_js_module.c` —— 两个 NGINX 模块。

#### 4.1.4 代码实践

**实践目标：** 用肉眼把仓库「按职责归类」，建立心智地图。

**操作步骤：**

1. 在仓库根目录执行 `ls`，对照本节「目录职责」表格，给每个目录贴上「内核 / 扩展 / 集成 / 类型 / 测试 / 构建」标签。
2. 进入 `external/` 执行 `ls`，找出所有「同名但前缀不同」的模块对（例如 `njs_fs_module.c` 与 `qjs_fs_module.c`）。
3. 进入 `ts/` 执行 `ls`，观察它与 `external/`、`nginx/` 在命名上的对应关系（例如 `ngx_http_js_module.d.ts` 对应 `nginx/ngx_http_js_module.c`）。

**需要观察的现象：**

- `external/` 里几乎每个功能模块都成对出现（`njs_*` + `qjs_*`），这印证了「双引擎 = 双份代码」。
- `nginx/` 里既有 `ngx_js_fetch.c`（njs 侧）也有 `ngx_qjs_fetch.c`（qjs 侧），同样是双份。

**预期结果：** 你能不假思索地说出「找 fs 模块去 `external/`」「找 r 对象绑定去 `nginx/ngx_http_js_module.c`」「找 TS 类型去 `ts/`」。

**运行结果：** 待本地验证（取决于你的本地 `ls` 输出，但目录组成与本讲描述一致）。

#### 4.1.5 小练习与答案

**练习 1：** 我想给 njs 增加一个新内建类型（比如 `Temporal`），应该把实现放在哪个目录？  
**参考答案：** 放在 `src/`，因为内建类型属于引擎内核（参照已有的 `src/njs_promise.c`、`src/njs_array.c`）。

**练习 2：** 为什么 `external/njs_fs_module.c` 和 `external/qjs_fs_module.c` 要分成两个文件，而不是合并？  
**参考答案：** 因为它们分别面向两套不同的引擎底座：`njs_*` 基于 njs 自己的 `njs_value_t`/`njs_module_t`，`qjs_*` 基于 QuickJS 的 `JSValue`/`qjs_module_t`。两套底层 API 不同，无法用同一份 C 代码兼容，所以分开维护。

**练习 3：** `ts/` 目录下的 `.d.ts` 文件，对应的「真相源」是哪一层？  
**参考答案：** 是 `external/`（扩展模块 API）与 `nginx/`（`r`/`s` 对象绑定）里 C 代码实际暴露的 API 表面。`ts/` 是对这些 API 的 TypeScript 描述。

---

### 4.2 configure 加载流程

#### 4.2.1 概念说明

`configure` 是 njs 构建的**唯一入口**。它本身只有 66 行，是一个纯粹的「指挥官」——自己几乎不做事，只负责**按固定顺序**调用（在 shell 中用 `.` 即 `source`）`auto/` 下的一组脚本。这种写法的好处是：每一步都被拆成独立的小脚本，便于阅读和维护，也和 NGINX 官方的构建系统风格保持一致。

`configure` 做的事可以分成四个阶段：

1. **准备阶段**：设环境、初始化变量、解析命令行选项、建立 `build/` 目录。
2. **特性检测阶段**：探测编译器、操作系统、以及 PCRE/OpenSSL/libxml2/zlib/QuickJS 等可选依赖是否存在。
3. **清单收集阶段**：把要编译的源文件、要注册的模块收集进若干 shell 变量。
4. **生成阶段**：把上面的结果写进 `build/Makefile`，并打印一份配置摘要。

#### 4.2.2 核心流程

下面是 `configure` 的执行流程，用伪代码表示：

```text
configure()
  ├─ set -e / set -u                 # 出错即停、禁用未定义变量
  ├─ . auto/init                     # 把变量初始化为空/默认值
  ├─ . auto/options                  # 解析 ./configure --xxx 选项
  ├─ 设置路径常量 (NJS_MAKEFILE 等)
  ├─ mkdir build/  写空的 autoconf.err / njs_auto_config.h
  │
  ├─ 【特性检测】依次 source:
  │     auto/os  auto/cc  auto/types  auto/endianness  auto/clang
  │     auto/time  auto/memalign  auto/getrandom  auto/stat
  │     auto/computed_goto  auto/explicit_bzero
  │     auto/pcre  auto/readline  auto/quickjs
  │     auto/openssl  auto/libxml2  auto/zlib  auto/libbfd  auto/link
  │     (每个脚本通过编译并运行一段小程序来判定某特性是否存在,
  │      存在就把对应 NJS_HAVE_* 置 YES, 否则置 NO)
  │
  ├─ 【清单收集】依次 source:
  │     auto/sources        # NJS_LIB_SRCS  = 内核 .c 清单
  │     auto/modules        # NJS_LIB_MODULES = njs_* 扩展模块
  │     auto/qjs_modules    # QJS_LIB_MODULES = qjs_* 扩展模块
  │
  └─ 【生成】依次 source:
        auto/make           # 用上面三个变量写出 build/Makefile
        auto/expect         # shell 交互测试依赖检测
        auto/summary        # 打印 "NJS configuration summary"
```

这里的关键设计是：**特性检测脚本只负责把 `NJS_HAVE_OPENSSL` 之类的开关置为 `YES`/`NO`，而真正决定「编不编某段代码」的是后面的清单脚本**。例如 `auto/modules` 里会写「如果 `NJS_OPENSSL = YES` 且 `NJS_HAVE_OPENSSL = YES`，才注册 `njs_crypto_module`」。这就把「能力探测」和「编译选择」解耦了。

#### 4.2.3 源码精读

`configure` 开头先做严格的 shell 环境约束，并加载初始化与选项脚本：

- [configure:L7-L17](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/configure#L7-L17) —— 设 `LC_ALL=C`（避免本地化输出干扰检测）、`set -e`/`set -u`（出错即停、未定义变量即错），随后 `. auto/init` 与 `. auto/options`。

接下来设置产物路径，并创建 `build/` 目录与两个初始文件：

- [configure:L19-L34](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/configure#L19-L34) —— 定义 `NJS_MAKEFILE=build/Makefile`、`NJS_LIB_INCS="src external $NJS_BUILD_DIR"`，`mkdir` 出 build 目录，并给 `njs_auto_config.h` 写入「本文件由 configure 自动生成」的注释头。

最核心的两段是「特性检测」和「清单/生成」，都在文件末尾连续 source：

- [configure:L40-L58](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/configure#L40-L58) —— **特性检测阶段**：从 `. auto/os` 一路到 `. auto/link`，共 19 个脚本。注意 QuickJS 的检测在 `. auto/quickjs`（L53），而三大可选加密/解析/压缩库在 L54-L56。
- [configure:L60-L65](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/configure#L60-L65) —— **清单与生成阶段**：`. auto/sources` → `. auto/modules` → `. auto/qjs_modules` → `. auto/make` → `. auto/expect` → `. auto/summary`。这 6 行就是本讲 4.3 节的入口。

> 补充：命令行选项的解析在 [auto/options](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/options)，它负责把 `--no-openssl`、`--address-sanitizer=YES`、`--with-quickjs` 等翻译成对应的 `NJS_*` 变量（进阶构建见 u10-l3）。而最终打印给用户看的摘要则由 [auto/summary](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/summary#L6-L44) 输出，例如「+ using QuickJS library: ...」「njs CLI: build/njs」。

#### 4.2.4 代码实践

**实践目标：** 在 `configure` 里标注出它依次 `source` 了哪些脚本，分清「检测」与「清单/生成」两类。

**操作步骤：**

1. 打开 `configure` 文件，从第 16 行读到第 65 行。
2. 用三种颜色（或三种记号）分别标注：① 准备阶段（`. auto/init`、`. auto/options`）；② 特性检测阶段（L40-L58 的 19 个脚本）；③ 清单/生成阶段（L60-L65 的 6 个脚本）。
3. 在 L52-L56 旁边标注：「PCRE → readline → QuickJS → OpenSSL → libxml2 → zlib」是按依赖顺序探测的。

**需要观察的现象：**

- 检测脚本里很多是「探测某个系统特性或第三方库」，例如 `auto/pcre` 探测 PCRE 正则库、`auto/openssl` 探测 OpenSSL。
- 这些脚本之间通过共享的 shell 变量（如 `NJS_HAVE_OPENSSL`）通信，检测结果会**自上而下流动**到后面的清单脚本。

**预期结果：** 你能画出一张「configure 依次 source 的脚本列表」，并能指出「决定是否编译 crypto 模块的开关，是在 `auto/openssl` 设置、在 `auto/modules` 消费的」。

**运行结果：** 待本地验证（标注是阅读型任务，无需运行）。

#### 4.2.5 小练习与答案

**练习 1：** 如果系统里没装 OpenSSL，`configure` 会失败退出吗？  
**参考答案：** 不会。`auto/openssl` 只是把 `NJS_HAVE_OPENSSL` 置为 `NO`，后续 `auto/modules` 里的 `if [ $NJS_OPENSSL = YES -a $NJS_HAVE_OPENSSL = YES ]` 条件不成立，于是不注册 crypto/webcrypto 模块。OpenSSL 是**可选依赖**。

**练习 2：** `configure` 末尾为什么是先 `. auto/sources` 再 `. auto/modules`？顺序能调换吗？  
**参考答案：** 不能简单调换。`auto/sources` 先建立 `NJS_LIB_SRCS` 等基础清单变量，`auto/modules` 会向 `NJS_LIB_SRCS` **追加**扩展模块的源文件（见 4.3.3 的 `auto/module`）。如果调换，追加就失去了基础对象。

**练习 3：** `configure` 里 `set -u`（禁用未定义变量）的作用是什么？和 `auto/init` 有什么配合关系？  
**参考答案：** `set -u` 让脚本在引用未定义变量时立刻报错，避免「空变量悄悄扩散」导致的难以排查的 bug。`auto/init` 的职责正是**预先把所有变量初始化**为空或默认值，确保后续脚本引用它们时不会触发 `set -u`。

---

### 4.3 源码/模块清单机制

#### 4.3.1 概念说明

njs 没有用一个「项目配置文件」（如 `CMakeLists.txt`）来声明源文件，而是用 **shell 变量当清单**。`auto/sources`、`auto/modules`、`auto/qjs_modules` 这三个文件的本质就是三组赋值语句。它们回答两个问题：

1. **编译哪些 `.c` 文件？** 由 `NJS_LIB_SRCS`（内置引擎内核）和 `QJS_LIB_SRCS`（QuickJS 包装层）回答。
2. **注册哪些扩展模块？** 由 `NJS_LIB_MODULES`（`njs_*` 模块名列表）和 `QJS_LIB_MODULES`（`qjs_*` 模块名列表）回答。

这套机制最巧妙的地方在于：**模块清单不仅列出名字，还会「顺手」把该模块的源文件追加进 `NJS_LIB_SRCS`**。所以你只要在 `auto/modules` 里登记一个模块，它的源文件就会自动被纳入编译——不用再改 `auto/sources`。

#### 4.3.2 核心流程

清单从「声明」到「变成可链接代码」的流程：

```text
auto/sources         → NJS_LIB_SRCS   = 48 个内核 .c   (固定, 总是编译)
auto/modules         → 逐个模块:
                         设 njs_module_name / njs_module_srcs
                         . auto/module  → 把 name 追加进 NJS_LIB_MODULES
                                        → 把 srcs 追加进 NJS_LIB_SRCS
                                        → 把 incs 追加进 NJS_LIB_INCS
auto/qjs_modules     → 逐个 qjs 模块:
                         . auto/qjs_module → 同理追加进 QJS_LIB_* 系列

auto/make            → 读 NJS_LIB_SRCS / QJS_LIB_SRCS, 为每个 .c 生成一条编译规则
                     → 读 NJS_LIB_MODULES / QJS_LIB_MODULES, 生成 njs_modules.c / qjs_modules.c
                       (这两个 .c 里定义 njs_modules[] / qjs_modules[] 数组,
                        引擎启动时遍历它来初始化所有扩展模块)
                     → 链接成 libnjs.a / libqjs.a / njs
```

注意 `auto/modules` 里很多模块是**有条件注册**的（用 `if` 包裹），所以最终编进二进制的模块集合，取决于你在 `configure` 时探测到了哪些第三方库。

#### 4.3.3 源码精读

**① 内核源码清单 `auto/sources`**

这个文件用一个变量列出了内置引擎的全部核心源文件：

- [auto/sources:L1-L50](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/sources#L1-L50) —— `NJS_LIB_SRCS` 列出 **48 个内核 `.c` 文件**。大致可分为五组：
  - 基础设施：`njs_mp.c`（内存池）、`njs_flathsh.c`（哈希表）、`njs_rbtree.c`（红黑树）、`njs_utf8.c`/`njs_utf16.c`（编码）、`njs_dtoa.c`（数字转字符串）等；
  - 值与 VM：`njs_value.c`、`njs_atom.c`、`njs_vm.c`、`njs_vmcode.c`；
  - 编译前端：`njs_lexer.c` → `njs_parser.c` → `njs_variable.c`/`njs_scope.c` → `njs_generator.c` → `njs_disassembler.c`；
  - 内建类型：`njs_string.c`、`njs_array.c`、`njs_object.c`、`njs_function.c`、`njs_promise.c`、`njs_date.c`、`njs_regexp.c`、`njs_error.c` 等；
  - 内建注册：`njs_builtin.c`。
- [auto/sources:L52-L54](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/sources#L52-L54) —— `QJS_LIB_SRCS` 只有 `src/qjs.c` 一个文件（QuickJS 包装层主体，其余 `qjs_*` 由 `auto/qjs_modules` 追加）。
- [auto/sources:L68-L74](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/sources#L68-L74) —— **条件源文件**：若启用 PCRE 则追加 `external/njs_regex.c`；若同时启用 libbfd 与 `dl_iterate_phdr`（用于符号化栈回溯）则追加 `src/njs_addr2line.c`。

**② njs 扩展模块清单 `auto/modules`**

这里用「设变量 + `. auto/module`」的模式逐个登记模块：

- [auto/modules:L4-L8](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/modules#L4-L8) —— `njs_buffer_module` 无条件注册（Buffer 是核心能力，源文件 `src/njs_buffer.c`）。
- [auto/modules:L10-L22](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/modules#L10-L22) —— `crypto` 与 `webcrypto` **仅当 OpenSSL 可用时**才注册（`if [ $NJS_OPENSSL = YES -a $NJS_HAVE_OPENSSL = YES ]`）。`xml`、`zlib` 同理受 libxml2/zlib 开关控制（L24-L38）。
- [auto/modules:L40-L50](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/modules#L40-L50) —— `fs`、`query_string` 无条件注册。

每一段 `. auto/module` 实际做的事，看这个 7 行辅助脚本就明白：

- [auto/module:L4-L6](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/module#L4-L6) —— 把 `njs_module_name` 追加进 `NJS_LIB_MODULES`，把 `njs_module_srcs` 追加进 `NJS_LIB_SRCS`，把 `njs_module_incs` 追加进 `NJS_LIB_INCS`。这就是「登记一个模块 = 自动纳入它的源文件」的实现。

**③ QuickJS 扩展模块清单 `auto/qjs_modules`**

与 `auto/modules` 完全对称，只是改用 `qjs_` 前缀和 `. auto/qjs_module` 辅助脚本：

- [auto/qjs_modules:L4-L8](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/qjs_modules#L4-L8) —— `qjs_buffer_module`（源文件 `src/qjs_buffer.c`）。
- [auto/qjs_module:L4-L6](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/qjs_module#L4-L6) —— 与 `auto/module` 对称，追加进 `QJS_LIB_MODULES`/`QJS_LIB_SRCS`。

**④ 清单如何变成 Makefile：`auto/make`**

最后，`auto/make` 读这些变量，生成 `build/Makefile`。其中两段最能体现清单的作用：

- [auto/make:L14-L41](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/make#L14-L41) —— 遍历 `NJS_LIB_MODULES`，生成一个胶水文件 `build/njs_modules.c`，里面写出 `njs_module_t *njs_modules[] = { &njs_buffer_module, &njs_fs_module, ..., NULL };`。引擎启动时遍历这个数组来完成所有扩展模块的初始化。
- [auto/make:L173-L184](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/make#L173-L184) —— CLI `build/njs` 的链接规则：把 `external/njs_shell.c` 与 `libnjs.a`、（启用时的）`libqjs.a` 链在一起，得到独立的 `njs` 可执行文件。
- [auto/make:L285-L334](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/make#L285-L334) —— 主目标定义：`njs`、`lib_test`、`unit_test`、`test`、`test262`、`benchmark` 等，这就是 README 里那些 `make xxx` 命令的来源。

#### 4.3.4 代码实践

**实践目标：** 对照 `auto/sources` 数清楚内核被编译的源文件数量与分组，并追踪一个模块从「登记」到「进入编译」的完整路径。

**操作步骤：**

1. 打开 [auto/sources](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/sources)，统计 `NJS_LIB_SRCS`（L1-L50）里的 `.c` 文件数量，并按「基础设施 / 值与VM / 编译前端 / 内建类型 / 内建注册」五组归类。
2. 打开 `auto/modules`，挑 `njs_fs_module`（L40-L44）这一段，顺着 `. auto/module` 跳到 `auto/module`，确认它的 `srcs` 被追加进了 `NJS_LIB_SRCS`。
3. 打开 `auto/make` 的 L14-L41，理解 `njs_modules[]` 数组是怎么从 `NJS_LIB_MODULES` 生成的。

**需要观察的现象：**

- `NJS_LIB_SRCS` 里固定有 **48 个**内核 `.c`（外加最多 2 个条件文件 `njs_regex.c`、`njs_addr2line.c`）。
- `fs` 模块的源文件 `external/njs_fs_module.c` **没有出现在 `auto/sources` 里**，而是通过 `auto/modules` + `auto/module` 间接追加进去的。
- `crypto`/`webcrypto`/`xml`/`zlib` 都被 `if` 包裹，说明它们是可选的。

**预期结果：** 你能写出一张表，回答「内核 48 个 .c 分几组、扩展模块有几个、其中哪几个是可选的」。

**运行结果验证（可选）：** 构建完成后，查看 `build/Makefile` 里 `NJS_LIB_OBJS =` 这一行，数一数里面列出的 `.o` 文件个数，应该等于「48 内核 + 条件文件 + 已注册扩展模块源文件」之和。如果系统装了 OpenSSL，你会看到 `njs_crypto_module.o` 在里面；如果没装，就看不到。这是最直观的验证。

> 说明：本实践主要是源码阅读 + 数量统计，运行构建属于可选项（构建步骤见下一讲 u1-l3）。如果你暂时无法构建，标注「待本地验证」即可，核心结论（清单机制本身）不依赖运行。

#### 4.3.5 小练习与答案

**练习 1：** `fs` 模块的源文件 `external/njs_fs_module.c` 为什么没有写在 `auto/sources` 里？它是怎么被编译的？  
**参考答案：** 因为 `auto/sources` 只列「内核核心」源文件；扩展模块的源文件由 `auto/modules` 登记，再通过 `auto/module` 这 7 行脚本**追加**进 `NJS_LIB_SRCS`。这样设计让「新增一个扩展模块」只需改 `auto/modules` 一处。

**练习 2：** 如果我想让 `xml` 模块在没装 libxml2 时也能编译，应该改哪里？这样改合理吗？  
**参考答案：** 表面上可以去掉 `auto/modules` L24 的 `if [ $NJS_LIBXML2 = YES -a $NJS_HAVE_LIBXML2 = YES ]` 条件让它无条件注册，但 `external/njs_xml_module.c` 的实现依赖 libxml2 头文件和符号，没装 libxml2 时**编译会失败**。所以不合理——xml 模块本质上需要 libxml2，正确做法是先装依赖再 `--libxml2=YES`。

**练习 3：** `auto/make` 生成的 `build/njs_modules.c` 文件里那个 `njs_modules[]` 数组有什么用？  
**参考答案：** 它是引擎启动时的「模块初始化清单」。引擎在 `njs_builtin.c` 等初始化代码里会遍历 `njs_modules[]`，逐个调用每个模块的初始化函数，从而把 `fs`、`crypto` 等扩展能力挂到 JS 全局对象上。`qjs_modules[]` 数组对 QuickJS 引擎起同样作用。

## 5. 综合实践

**任务：画出 njs 从「源码」到 `build/njs` 的完整构建数据流图。**

请综合本讲三个最小模块，完成下面这张图的填空与补充：

```text
源码层                         清单层                    生成层              产物
───────                       ──────                   ──────              ────
src/njs_*.c  ─┐
src/qjs.c    ─┼─► ( ? ) ───────────► NJS_LIB_SRCS ─┐
              │                   QJS_LIB_SRCS ─┐  │
external/      │                                 │  │
  njs_*_module.c ─► auto/modules ─► . auto/module ─┼──┤
  qjs_*_module.c ─► auto/qjs_modules ─► . auto/qjs_module ┤
  njs_shell.c  ────────────────────────────────────┼──┘
                                                  │
              configure 依次 source 上述清单 ──► auto/make ─► build/Makefile
                                                                   │
                                                                   ▼
                                              make ─► libnjs.a / libqjs.a / build/njs
```

具体要求：

1. 在 `( ? )` 处填入正确的脚本名（`auto/sources`）。
2. 标注 `configure` 在收集清单**之前**必须先跑完的「特性检测」阶段（写出 3 个有代表性的检测脚本，如 `auto/openssl`）。
3. 解释为什么 `build/njs_modules.c` 和 `build/qjs_modules.c` 是**在构建期由 `auto/make` 动态生成**的，而不是仓库里现成的源文件。
4.（可选，待本地验证）如果你能运行 `./configure`，打开生成的 `build/Makefile`，找到 `NJS_LIB_OBJS =` 那一行，确认它列出的 `.o` 数量与本讲 4.3.4 统计的一致。

完成后，你应该能用一句话向别人讲清：「njs 用 shell 变量当清单，`configure` 负责探测特性并收集清单，`auto/make` 把清单翻译成 Makefile，最后 `make` 编译链接出 `build/njs`。」

## 6. 本讲小结

- njs 顶层目录按职责清晰分层：`src/`（引擎内核，含 QuickJS 包装层）、`external/`（双份实现的扩展模块 + CLI 入口）、`nginx/`（NGINX 集成）、`ts/`（类型定义）、`test/`（测试）、`auto/`（构建系统）。
- 「双引擎」在工程上体现为「双份代码」：`external/` 下几乎每个模块都有 `njs_*` 与 `qjs_*` 两份实现，构建产物也分 `libnjs.a` 与 `libqjs.a`。
- `configure` 是一个 66 行的「指挥官」，按「准备 → 特性检测（19 个脚本）→ 清单收集 → 生成」四阶段顺序 `source` 各个 `auto/*.sh`，最终产出 `build/Makefile`。
- 源码/模块用 shell 变量当清单：`auto/sources` 固定列出 48 个内核 `.c`，`auto/modules`/`auto/qjs_modules` 登记扩展模块并通过 `auto/module`/`auto/qjs_module` 把模块源文件追加进编译清单。
- 扩展模块可按条件注册（如 crypto/webcrypto 依赖 OpenSSL、xml 依赖 libxml2、zlib 依赖 zlib），最终编进二进制的模块集合由 `configure` 的探测结果决定。
- `auto/make` 会在构建期动态生成 `njs_modules.c`/`qjs_modules.c` 两个胶水文件，它们定义的 `njs_modules[]`/`qjs_modules[]` 数组是引擎启动时初始化所有扩展模块的入口。

## 7. 下一步学习建议

本讲只看了「骨架」，还没有真正把 njs 跑起来。建议下一步：

- **下一讲 u1-l3「构建并运行第一个 njs 程序」** 会动手执行 `./configure && make njs`，得到 `build/njs`，并用三种方式运行 JS，把本讲的构建流程「跑通」一遍。
- 如果你对 CLI 的命令行选项好奇，u1-l4 会精读 `external/njs_shell.c` 的 `main()`，那正是本讲 `auto/make` 链接规则里反复出现的 `external/njs_shell.c`。
- 等你想深入内核时，回到本讲的 `auto/sources`，从 `NJS_LIB_SRCS` 里挑一个文件开始读——推荐的起点是 `src/njs_vm.c`（VM 生命周期，对应 u2-l1）和 `src/njs_value.c`（值表示，对应 u2-l2）。
- 想了解进阶构建（ASan、`--no-openssl` 等）可跳读 u10-l3，但建议先把基础构建跑通。
