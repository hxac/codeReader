# 构建并运行第一个 njs 程序

## 1. 本讲目标

上一讲（u1-l2）我们看清了 njs 的目录骨架与构建系统是如何「用 shell 变量当清单」的。本讲把那些清单真正「跑起来」：动手把 njs 从源码编译成独立命令行工具，并用三种方式执行 JavaScript。

学完本讲你应当能够：

1. 用 `./configure && make njs` 把 njs 编译出独立可执行文件 `build/njs`，并知道每一步背后调用了哪些 `auto/` 脚本。
2. 用「一行命令 `-c`」「文件」「交互式 REPL」三种方式运行 JavaScript。
3. 理解 QuickJS 后端是「可选链接」的，掌握用 `--cc-opt`/`--ld-opt`（或 `--with-quickjs`）让 `configure` 找到并链接 QuickJS 库的完整流程。
4. 看懂 `-n njs` 与 `-n QuickJS` 在运行期切换引擎的差别，以及 `NJS_ENGINE` 环境变量的作用。

> 说明：本讲只覆盖「构建 + 运行」。命令行选项的逐项详解（`-d` 反汇编、`-o` opcode 跟踪、`-m` 模块模式等）放在下一讲 u1-l4，本讲只用到运行所需的少数几个选项。

## 2. 前置知识

- **CLI（Command Line Interface，命令行界面）**：在本项目里特指 `build/njs` 这个可执行程序。它脱离 NGINX 独立运行，可以像 `node` 一样执行 JS 文件或进入交互式 shell，但**没有 `r`（HTTP 请求对象）和 `s`（stream 会话对象）**——这两个对象只有把 njs 嵌进 NGINX 才会注入。所以 CLI 主要用来**验证 JS 语法和语言行为**，而不是复现 NGINX 集成效果。
- **QuickJS 与 njs 双引擎**：njs 仓库里内置了两种可互换的 JS 引擎。默认的「njs 引擎」是 ES5.1 strict 子集（1.0.0 起标记弃用）；推荐的「QuickJS 引擎」是 ES2023。CLI 在运行期用 `-n` 选择其一。QuickJS 是一个**独立的外部 C 库**，需要你额外编译并提供给 njs 的 `configure` 去发现、链接。
- **configure + Makefile 构建范式**：njs 没有使用 autotools 或 cmake。`./configure` 是一个 shell 脚本，它通过 `source`（`.` 命令）依次加载 `auto/` 下的一堆 `.sh` 脚本做「特性检测」，最终**生成** `build/Makefile`。所以 `configure` 本身不编译代码，它只负责「收集清单 + 探测环境 + 生成 Makefile」，真正的编译由随后的 `make` 完成。
- **`make clean` 的必要性**：因为 Makefile 是 `configure` 生成的，**不会原地更新**。一旦你换了 `configure` 选项（比如从「不链接 QuickJS」改成「链接 QuickJS」），必须先 `make clean` 再重新 `./configure`，否则旧的 Makefile/目标文件会残留。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [configure](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/configure) | 构建总入口。依次 `source` `auto/*.sh`，做特性检测并生成 `build/Makefile`。 |
| [auto/options](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/options) | 解析 `./configure` 的命令行选项，设置 `NJS_QUICKJS`/`NJS_TRY_QUICKJS` 等开关与默认值。 |
| [auto/quickjs](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/quickjs) | QuickJS 库的「特性检测」脚本：尝试多种方式定位 `libquickjs`，找到则置 `NJS_HAVE_QUICKJS=YES` 并把链接库记入 `NJS_LIB_AUX_LIBS`。 |
| [auto/make](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/make) | 把所有清单与检测结果拼装成 `build/Makefile`，其中包含 `njs` 目标（产出 `build/njs`）与 QuickJS 库的链接规则。 |
| [external/njs_shell.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c) | CLI 的 C 源码（含 `main()`），负责命令行选项解析与引擎选择。 |
| [src/njs.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs.h) | 公共 C API 头文件，其中定义了版本号宏 `NJS_VERSION`。 |
| [README.md](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/README.md) | 官方安装与构建说明，是构建步骤的权威依据。 |
| [docs/agent/engine-dev.md](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/engine-dev.md) | 仓库内的引擎开发指南，列出了所有 `configure` 选项和 CLI 调试开关。 |

## 4. 核心概念与源码讲解

### 4.1 CLI 构建步骤

#### 4.1.1 概念说明

njs 的「构建」分两步：

1. **`./configure`**：不编译任何代码。它读取命令行选项、探测编译器与各种可选依赖（PCRE/OpenSSL/libxml2/zlib/QuickJS），把「要编译哪些源文件」「链接哪些库」「打哪些 `-D` 宏」这些信息收集起来，**生成** `build/Makefile` 和 `build/njs_auto_config.h`。
2. **`make njs`**：按生成的 Makefile，把 `src/`、`external/` 下的 C 文件编译成 `.o`，归档为 `libnjs.a`（必要时还有 `libqjs.a`），最后链接出 `build/njs`。

之所以强调「分两步」，是因为很多人误以为 `./configure` 就能出可执行文件——其实它只是「生成蓝图」，`make` 才是「施工」。

#### 4.1.2 核心流程

`./configure` 内部的执行顺序（每行 `. auto/xxx` 就是「source 加载一个脚本」）：

```
configure
 ├─ . auto/init          # 初始化各种变量与 build 目录
 ├─ . auto/options       # ① 解析 ./configure 的 --xxx 选项
 ├─ . auto/os / auto/cc  # 探测操作系统与编译器
 ├─ . auto/quickjs       # ② 探测 QuickJS 库（本讲重点）
 ├─ . auto/openssl ...   # 探测其它可选依赖
 ├─ . auto/sources       # ③ 收集内核源码清单
 ├─ . auto/modules       # ④ 收集 njs 扩展模块清单
 ├─ . auto/qjs_modules   # ⑤ 收集 QuickJS 扩展模块清单
 └─ . auto/make          # ⑥ 生成 build/Makefile
```

随后 `make njs` 走 Makefile 里的 `njs` 目标，依赖 `libnjs.a`（和 QuickJS 启用时的 `libqjs.a`），链接产出 `build/njs`。

#### 4.1.3 源码精读

先看 `configure` 总入口，它用一连串 `. auto/xxx` 串起整个构建探测链：

[configure:16-63](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/configure#L16-L63)：从 `. auto/init`、`. auto/options` 开始，到第 53 行 `. auto/quickjs`（QuickJS 探测），最后第 60–63 行收集三份清单并 `. auto/make` 生成 Makefile。这段就是「构建蓝图」的装配线。

再看生成的 `build/njs` 是怎么链接出来的。`auto/make` 中写了一条专门的规则：

[auto/make:175-182](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/make#L175-L182)：`build/njs` 依赖 `libnjs.a`、`$QJS_LIB`（QuickJS 启用时才非空）和 `external/njs_shell.c`，链接时带上 `$NJS_LD_OPT`、`$NJS_LIBS`、`$NJS_LIB_AUX_LIBS`（QuickJS 等可选库会被追加进这个变量）以及 `$NJS_READLINE_LIB`（交互式 REPL 用的 readline/edit 库）。

而 `njs` 这个 make 目标的声明在：

[auto/make:298](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/make#L298)：`njs:` 目标依赖 `njs_auto_config.h` 和 `build/njs`。所以 `make njs` 实际触发的就是上面那条链接规则。

最后是版本号——CLI 启动时会打印 `NJS_VERSION`，当前定义为：

[src/njs.h:14](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs.h#L14)：`#define NJS_VERSION "1.0.1"`。注意 README 里的交互示例写的是 `0.8.4`，那是较早版本的截图，**以你本地构建出的实际版本号为准**（本 HEAD 下为 `1.0.1`）。

#### 4.1.4 代码实践

**实践目标**：亲手构建出 `build/njs`，并验证产物存在。

**操作步骤**（依据 [docs/agent/engine-dev.md:15-20](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/engine-dev.md#L15-L20)）：

```bash
# 在 njs 源码根目录执行
./configure
make -j$(nproc) njs      # 产物在 build/njs
```

> 若交互式 REPL 报缺少 readline/edit 相关库，需先装依赖（见 [README.md:291-296](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/README.md#L291-L296)）：`sudo apt install libedit-dev`，再 `make clean && ./configure`。

**需要观察的现象**：`configure` 会打印一串 `checking ...` 形式的特性检测结果；`make` 末尾不再报错。

**预期结果**：执行 `ls -l build/njs` 能看到一个可执行文件。运行 `./build/njs -c 'njs.version'` 应直接打印版本字符串（本 HEAD 下为 `1.0.1`）。**待本地验证**：具体版本号以你本地输出为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么修改了 `./configure` 的选项后，文档要求先 `make clean` 再重新 `./configure`？

**参考答案**：因为 `build/Makefile` 和 `build/njs_auto_config.h` 是 `configure` **生成**的，`configure` 不会在原地增量更新已生成的 Makefile，旧的 `.o` 与 Makefile 会带着旧配置残留，导致新选项（如 QuickJS）不生效。`make clean` 清掉旧产物后重新生成才能保证一致。

**练习 2**：`./configure` 这一步本身编译了任何 C 代码吗？

**参考答案**：没有。`./configure` 只做选项解析、环境探测、清单收集并生成 `build/Makefile`（以及触发少量「探测小程序」的临时编译来检测特性，但这些不是最终产物）。真正的 njs 源码编译发生在随后的 `make`。

---

### 4.2 三种运行方式

#### 4.2.1 概念说明

构建出 `build/njs` 后，可以用三种方式执行 JavaScript：

| 方式 | 命令形态 | 适用场景 |
|---|---|---|
| 一行命令 | `./build/njs -c '<代码>'` | 快速验证一段表达式/语句 |
| 文件 | `./build/njs file.js` | 运行完整的脚本文件 |
| 交互式 REPL | `./build/njs`（无参数） | 边敲边试，逐行求值 |

注意 CLI 与 NGINX 集成的本质差别（见 [README.md:208-209](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/README.md#L208-L209)）：CLI 独立运行，**没有 `r`/`s` 等 NGINX 对象**，所以不要指望在 CLI 里调用 `r.return(...)`。

#### 4.2.2 核心流程

CLI 的 `main()` 位于 `external/njs_shell.c`。启动流程大致是：

```
main()
 ├─ njs_options_parse()      # 解析命令行选项
 │     ├─ 默认 opts.engine = NJS_ENGINE_NJS
 │     ├─ 读取环境变量 NJS_ENGINE（可覆盖默认引擎）
 │     └─ 处理 -c / -d / -n / -q 等开关
 ├─ 根据 opts.engine 选择引擎初始化函数
 │     ├─ NJS_ENGINE_NJS      → njs_engine_njs_init()
 │     └─ NJS_ENGINE_QUICKJS  → njs_engine_qjs_init()（需编译时启用）
 └─ 进入交互式 REPL 或执行 -c/文件
```

引擎用一个小枚举表示：

```c
enum {
    NJS_ENGINE_NJS = 0,
    NJS_ENGINE_QUICKJS = 1,
} engine;
```

#### 4.2.3 源码精读

选项解析入口与默认引擎设置：

[external/njs_shell.c:504-549](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L504-L549)：`njs_options_parse()` 函数开头，第 549 行 `opts->engine = NJS_ENGINE_NJS;` 设定了**默认引擎就是内置 njs 引擎**。

`-c` 选项的处理：

[external/njs_shell.c:618-628](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L618-L628)：`-c` 会把 `interactive` 置 0（即非交互），并把紧随其后的参数作为要执行的命令字符串存入 `opts->command`。这正是 `./build/njs -c 'console.log(2**10)'` 的实现。

`-q`（quiet）选项——README 里 `echo "2**3" | njs -q` 就靠它抑制提示符：

[external/njs_shell.c:698-700](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L698-L700)：`-q` 置 `opts->quiet = 1`，抑制 banner/提示符，从 stdin 读入并求值。

运行期选择引擎：`-n` 选项与 `NJS_ENGINE` 环境变量最终都调用同一个解析函数：

[external/njs_shell.c:567-572](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L567-L572)：启动时读取环境变量 `NJS_ENGINE`，若存在则用它覆盖默认引擎。所以你可以用 `NJS_ENGINE=QuickJS ./build/njs -c '...'` 等价于 `-n QuickJS`。

[external/njs_shell.c:796-805](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L796-L805)：`njs_options_parse_engine()` 用 `strncasecmp` 比对——`"njs"`（大小写不敏感）→ `NJS_ENGINE_NJS`；`"QuickJS"` → `NJS_ENGINE_QUICKJS`（仅当编译时定义了 `NJS_HAVE_QUICKJS`，否则该分支被 `#ifdef` 掉，选 QuickJS 会走到「未知引擎」错误）。

#### 4.2.4 代码实践

**实践目标**：用三种方式各跑一遍，并观察 REPL 里暴露的全局对象。

**操作步骤**：

```bash
# 方式一：一行命令
./build/njs -c 'console.log(2**10)'

# 方式二：交互式 REPL，打印全局对象
./build/njs
# 进入后输入：
>> globalThis
>> njs.version
>> process.argv
# 退出按 Ctrl-D

# 方式三：管道 + quiet 模式（参考 README 示例）
echo "2**3" | ./build/njs -q
```

> 交互式 REPL 里能看到的全局对象结构，可参考 [README.md:213-241](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/README.md#L213-L241) 给出的示例（其中 `njs` 对象含 `version`，`process` 对象含 `argv`/`env`）。

**需要观察的现象**：
- 方式一应打印 `1024`。
- REPL 中 `globalThis` 会列出 `njs`、`process`、`console`、`print` 等全局成员，但**没有** `r`/`s`/`ngx`（因为没有 NGINX 集成）。
- 方式三应打印 `8`。

**预期结果**：`console.log(2**10)` → `1024`；`echo "2**3" | ./build/njs -q` → `8`。REPL 中 `njs.version` 反映本地构建版本（本 HEAD 下为 `1.0.1`）。**待本地验证**：REPL 具体显示的版本号以本地为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `./build/njs -c 'r.return(200)'` 在 CLI 里会报错，而在 NGINX 的 `js_content` 里却能工作？

**参考答案**：CLI 是脱离 NGINX 独立运行的，运行时**不会注入**请求对象 `r`，所以 `r` 未定义、访问即抛 `ReferenceError`。只有把 njs 嵌入 NGINX 并通过 `js_content` 等指令触发时，NGINX 才会把当前请求对象 `r` 作为参数传进处理函数。这正是「指令驱动」模型（u1-l1）的体现。

**练习 2**：用环境变量而不是 `-n` 选项，如何让 CLI 默认用 QuickJS 引擎运行一段代码？

**参考答案**：`NJS_ENGINE=QuickJS ./build/njs -c '<代码>'`。`NJS_ENGINE` 环境变量在 `njs_options_parse` 中被读取，效果等同于在命令行加 `-n QuickJS`（前提是该 `build/njs` 编译时已链接 QuickJS）。

---

### 4.3 QuickJS 链接配置

#### 4.3.1 概念说明

QuickJS 是一个**独立的外部 C 库**，并不随 njs 源码一起分发。要把 njs 的 QuickJS 后端用起来，需要三件事：

1. **单独编译 QuickJS**，得到静态库 `libquickjs.a`（注意要带 `-fPIC`，因为 njs 会把它编进自己的库）。
2. **告诉 njs 的 `configure` 去哪里找** QuickJS 的头文件和库——用 `--cc-opt='-I<路径>'` 提供头文件目录、`--ld-opt='-L<路径>'` 提供库目录。
3. **让 `configure` 真正检测到并链接它**——`auto/quickjs` 会跑一连串探测；只有检测成功，才会定义 `NJS_HAVE_QUICKJS` 宏、把 `libqjs.a` 加入链接。

关键认知：QuickJS 在 njs 里是**可选**的。默认情况下 `configure` 只是「尝试」寻找 QuickJS（`NJS_TRY_QUICKJS=YES`），找不到也不报错——只是产出的 `build/njs` 不支持 `-n QuickJS`。若你用 `--with-quickjs` 明确「必须要有」，则找不到时 `configure` 直接报错退出。

#### 4.3.2 核心流程

QuickJS 从「外部库」变成「链接进 `build/njs`」的流程：

```
auto/options:
  默认 NJS_TRY_QUICKJS=YES, NJS_QUICKJS=NO
  --with-quickjs → 把 NJS_QUICKJS 也置 YES（强制要求）
        │
        ▼
auto/quickjs:  （configure 第 53 行加载）
  若 NJS_TRY_QUICKJS=YES，依次尝试：
    1) -lquickjs.lto
    2) -lquickjs
    3) -I/usr/include/quickjs -L/usr/lib/quickjs -lquickjs.lto/.lto
    4) pkg-config quickjs-ng
    5) -lqjs
  找到 → 跑若干子特性探测（JS_GetClassID、JS_NewTypedArray 等）
        → NJS_HAVE_QUICKJS=YES
        → 把库追加进 NJS_LIB_AUX_LIBS
        → 在 njs_auto_config.h 定义 NJS_HAVE_QUICKJS 宏
  若用了 --with-quickjs 却没找到 → exit 1 报错
        │
        ▼
auto/make:
  若 NJS_HAVE_QUICKJS=YES → QJS_LIB=libqjs.a，链入 build/njs
  否则 QJS_LIB 为空，build/njs 不含 QuickJS 后端
```

`--cc-opt` / `--ld-opt` 的作用，就是在上面第 1–5 步探测时，给编译器/链接器补上 `-I<QuickJS源码>` 和 `-L<QuickJS源码>`，让这些探测能在你自行编译的（非系统安装的）QuickJS 上成功。

#### 4.3.3 源码精读

先看选项默认值与相关开关：

[auto/options:19-20](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/options#L19-L20)：`NJS_QUICKJS=NO`（默认不强制）与 `NJS_TRY_QUICKJS=YES`（默认会尝试探测）。这两个变量的分离是「可选 vs 强制」的关键。

[auto/options:40-42](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/options#L40-L42)：`--cc-opt=*` → `NJS_CC_OPT`，`--ld-opt=*` → `NJS_LD_OPT`。这就是你用来塞 `-I`/`-L` 的入口。

[auto/options:56](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/options#L56) 与 [auto/options:65](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/options#L65)：`--no-quickjs` 把 `NJS_TRY_QUICKJS` 置 `NO`（完全不试）；`--with-quickjs` 同时把 `NJS_TRY_QUICKJS=YES` 和 `NJS_QUICKJS=YES`（试且必须成功）。

接着看探测脚本本体。最外层判断与首轮探测：

[auto/quickjs:10-27](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/quickjs#L10-L27)：仅当 `NJS_TRY_QUICKJS=YES` 才进入探测；第一条尝试 `-lquickjs.lto`，用一个最小测试程序（`JS_NewRuntime()`/`JS_FreeRuntime()`）验证库可用。

[auto/quickjs:29-73](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/quickjs#L29-L73)：首轮失败后的回退序列——`-lquickjs`、系统目录 `/usr/include/quickjs/`、`pkg-config quickjs-ng`、最后 `-lqjs`（quickjs-ng 的新库名）。这一串回退解释了「为什么 QuickJS 装在不同位置 njs 都可能找到」。

找到之后做什么：

[auto/quickjs:233-237](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/quickjs#L233-L237)：置 `NJS_HAVE_QUICKJS=YES`，把探测到的 `njs_feature_libs` 追加进 `NJS_LIB_AUX_LIBS`。这正是 QuickJS 库最终能被链接进 `build/njs` 的源头（见 4.1.3 中 `auto/make` 的链接规则引用了 `$NJS_LIB_AUX_LIBS`）。

「强制却没找到」的错误路径：

[auto/quickjs:240-245](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/quickjs#L240-L245)：当 `--with-quickjs`（`NJS_QUICKJS=YES`）但探测失败（`NJS_HAVE_QUICKJS=NO`）时，打印 `no QuickJS library found.` 并 `exit 1`。这就是「强制要求」的语义。

最后看 `auto/make` 如何据此决定是否链入 QuickJS 库：

[auto/make:80-83](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/make#L80-L83)：`QJS_LIB` 默认为空；仅当 `NJS_HAVE_QUICKJS=YES` 时才赋值为 `libqjs.a`。这个 `$QJS_LIB` 正是 4.1.3 链接规则里 `build/njs` 的依赖项之一——为空时 `build/njs` 不依赖 QuickJS，自然也没有 `-n QuickJS` 能力。

构建 QuickJS 后端的官方命令范式见 [docs/agent/engine-dev.md:22-36](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/engine-dev.md#L22-L36)：先在 QuickJS 源码树里 `CFLAGS=-fPIC make libquickjs.a`，再 `make clean && ./configure --cc-opt='-I<QUICKJS_SRC>' --ld-opt='-L<QUICKJS_SRC>' && make -j$(nproc) njs`。

#### 4.3.4 代码实践

**实践目标**：理解 QuickJS「可选链接」的两个分支——「不链接」与「强制链接」，并观察二者产物差异。

**操作步骤 A（不链接 QuickJS，对照基准）**：

```bash
make clean
./configure --no-quickjs        # 明确不探测
make -j$(nproc) njs
./build/njs -n QuickJS -c 'console.log(1)'
```

**操作步骤 B（强制链接 QuickJS）**（参考 [docs/agent/engine-dev.md:22-36](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/engine-dev.md#L22-L36)）：

```bash
# 1) 先编译 QuickJS 静态库（在 QuickJS 源码目录里）
( cd <QUICKJS_SRC> && CFLAGS=-fPIC make libquickjs.a )

# 2) 回到 njs 源码目录，重新配置并构建
make clean
./configure \
    --cc-opt='-I<QUICKJS_SRC>' \
    --ld-opt='-L<QUICKJS_SRC>'
make -j$(nproc) njs

# 3) 验证 QuickJS 后端可用
./build/njs -n QuickJS -c 'console.log(typeof Map)'
```

> 如果你没有 QuickJS 源码，可以用 `--with-quickjs` 直接观察「强制但找不到」的报错：`./configure --with-quickjs` 应在探测失败后打印 `no QuickJS library found.` 并退出（对应 [auto/quickjs:240-245](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/quickjs#L240-L245)）。

**需要观察的现象**：
- 步骤 A 中 `-n QuickJS` 应报 `unknown engine "QuickJS"`（因为该二进制未链接 QuickJS，`NJS_ENGINE_QUICKJS` 分支被 `#ifdef` 掉）。
- 步骤 B 中 `-n QuickJS -c 'console.log(typeof Map)'` 应打印 `function`（QuickJS 是 ES2023，原生支持 `Map`）；而 `-n njs -c 'console.log(typeof Map)'` 则可能不同（njs 引擎对 `Map` 的支持取决于版本/配置）。

**预期结果**：步骤 B 成功后，QuickJS 后端可用；步骤 A 的二进制不支持 `-n QuickJS`。**待本地验证**：是否拥有 QuickJS 源码及具体 `typeof Map` 在两引擎下的取值，以本地实际输出为准。

#### 4.3.5 小练习与答案

**练习 1**：`NJS_TRY_QUICKJS` 与 `NJS_QUICKJS` 这两个变量分别控制什么？为什么默认 `NJS_TRY_QUICKJS=YES` 而 `NJS_QUICKJS=NO`？

**参考答案**：`NJS_TRY_QUICKJS` 控制「是否**尝试**探测 QuickJS」，`NJS_QUICKJS` 控制「是否**强制要求** QuickJS 存在」。默认让「尝试」打开、「强制」关闭，意味着 njs 默认会把 QuickJS 当作可选依赖——装了就用、没装也能正常出 `build/njs`（只是没有 QuickJS 后端）。只有显式 `--with-quickjs` 才会把「强制」打开，找不到即报错退出。

**练习 2**：为什么单独编译 QuickJS 时要带 `CFLAGS=-fPIC`？

**参考答案**：njs 会把 QuickJS 的对象文件归档进自己的静态库（`libqjs.a`）再链接成 `build/njs`。`-fPIC`（Position Independent Code，位置无关代码）生成可在共享库/任意地址加载的目标代码；不带 `-fPIC` 的静态库在某些链接场景（尤其后续要编进动态模块 `.so` 时）会报重定位错误。README 的官方步骤也明确要求 `CFLAGS='-fPIC' make libquickjs.a`（见 [README.md:270-276](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/README.md#L270-L276)）。

---

## 5. 综合实践

把本讲三个模块串起来：**构建一个带 QuickJS 后端的 CLI，并用三种运行方式 + 引擎切换验证它**。

1. 编译 QuickJS 静态库（带 `-fPIC`）。
2. `make clean && ./configure --cc-opt='-I<QUICKJS_SRC>' --ld-opt='-L<QUICKJS_SRC>' && make -j$(nproc) njs`。
3. 用一行命令分别在两个引擎下运行同一段现代 JS，对比差异：

   ```bash
   ./build/njs -n njs     -c 'console.log(typeof Map)'
   ./build/njs -n QuickJS -c 'console.log(typeof Map)'
   ```

4. 把同一段代码写进 `test.js`，用文件方式 + 环境变量切换引擎各跑一次：

   ```bash
   echo "console.log(2**10, typeof Map)" > test.js
   ./build/njs test.js
   NJS_ENGINE=QuickJS ./build/njs test.js
   ```

5. 进入交互式 REPL，打印 `globalThis` 与 `njs.version`，确认 CLI 里**没有** `r`/`s` 对象。

**验收标准**：
- `make njs` 成功产出 `build/njs`。
- `-n QuickJS` 能正常工作（说明 QuickJS 已正确链接）。
- 你能说清楚：`configure` → `auto/options` → `auto/quickjs` → `auto/make` 这条链路上，QuickJS 是如何从「可选外部库」变成「`build/njs` 的一部分」的。

> ⚠️ 注意：本讲不修改任何源码，所有命令都只读地构建与运行。`test.js` 是你自己新建的临时文件，不在 njs 仓库内，可自行删除。

## 6. 本讲小结

- njs 的构建分两步：`./configure` 只解析选项、探测环境、生成 `build/Makefile`；`make njs` 才真正编译并链接出 `build/njs`。改了 `configure` 选项后必须先 `make clean`。
- CLI 有三种运行方式：`-c '<代码>'` 一行命令、`./build/njs file.js` 文件、无参数进入交互式 REPL。CLI 没有 NGINX 的 `r`/`s` 对象，主要用于验证 JS 语法与语言行为。
- 引擎在运行期用 `-n njs` / `-n QuickJS` 切换，也可用 `NJS_ENGINE` 环境变量覆盖；默认引擎是内置 njs 引擎（`opts->engine = NJS_ENGINE_NJS`）。
- QuickJS 是**可选链接**的外部库：默认 `configure` 只是尝试探测（`NJS_TRY_QUICKJS=YES`），`auto/quickjs` 用一串回退策略寻找 `libquickjs`；找到才定义 `NJS_HAVE_QUICKJS` 并把库追加进 `NJS_LIB_AUX_LIBS`，`auto/make` 据此把 `libqjs.a` 链入 `build/njs`。
- `--cc-opt='-I<路径>'` / `--ld-opt='-L<路径>'` 用来把自行编译的 QuickJS 暴露给探测与链接；`--with-quickjs` 则把 QuickJS 从「可选」升级为「强制」，找不到即报错退出。

## 7. 下一步学习建议

- 下一讲 **u1-l4「CLI 入口与命令行选项」** 会把本讲略过的选项逐一讲透：`-d` 反汇编、`-o` opcode 逐指令跟踪、`-m` 模块模式、`-p` 模块路径、`-a` AST 打印等，并带你在 `external/njs_shell.c` 的 `njs_options_parse` 里走一遍完整选项表。建议把本讲构建出的 `build/njs` 留着，下一讲直接复用。
- 若你想提前感受「njs 嵌进 NGINX」的完整形态，可先跳读 [README.md:159-201](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/README.md#L159-L201) 的 Hello World 示例（`js_path`/`js_import`/`js_content`），体会 CLI 与 NGINX 集成的差别；完整的 NGINX 集成在单元八讲解。
- 后续单元二（u2）会进入 `src/` 内核，从 `njs_vm_t` 的生命周期开始。届时你会需要 `build/njs` 配合 `-d` 反汇编来观察字节码，所以本讲构建出的可执行文件会一直用到。
