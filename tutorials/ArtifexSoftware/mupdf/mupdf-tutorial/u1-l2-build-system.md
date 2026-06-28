# 构建系统：如何编译 MuPDF

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 MuPDF 顶层 `Makefile` 里 `default`、`libs`、`apps`、`tools`、`examples`、`install` 等目标分别产出什么。
- 解释 `Makerules`、`Makelists`、`Makethird` 三个被 include 进来的辅助文件各自负责什么。
- 理解 `build=debug/release/sanitize` 等编译模式与 `FZ_ENABLE_*` 功能裁剪开关的作用。
- 看懂「系统库 vs 内置子模块」的两选一集成模式，并能说出 `thirdparty/` 下各子模块分别对应哪个第三方库。
- 在自己的机器上把 MuPDF 编译出库（`libmupdf.a`）和命令行工具（`mutool`），并记录它们的输出路径。

## 2. 前置知识

在动手之前，需要几个基础概念：

- **GNU Make**：MuPDF 用 GNU Make（不是 CMake、不是 Meson）来组织编译。Make 的核心是「目标（target）—依赖（prerequisite）—命令（recipe）」三元组。本讲会大量出现 `目标:` 这样的写法。
- **`include` 指令**：一个 Makefile 可以用 `include 文件名` 把另一个文件的内容原样插入进来。MuPDF 把构建逻辑拆成了 4 个文件互相 include，这是它构建系统看起来「文件多」的根本原因。
- **静态库 vs 动态库**：`.a` 是静态库（编译期把代码链接进可执行文件），`.so`/`.dylib` 是动态库（运行期加载）。MuPDF 默认产出静态库。
- **第三方库（thirdparty）**：MuPDF 本身不实现字体光栅化、JPEG 解码、zlib 解压这些底层能力，而是复用 freetype、libjpeg、zlib 等成熟库。这些库的源码以 git 子模块（submodule）形式放在 `thirdparty/` 下，既可以「随 MuPDF 一起编译」，也可以「直接用系统已安装的版本」。
- **pkg-config**：Linux/macOS 上用于查询系统已装库的编译参数（头文件路径、链接库）的标准工具，`Makerules` 大量用它做自动探测。

承接上一讲（u1-l1）：我们已经知道 MuPDF 是「fitz 通用层 + 各格式专用层」的双层架构，且 14 种格式 handler 在 `source/fitz/document-all.c` 注册。本讲要回答的问题是：**这一大堆源码到底是怎么变成可执行程序和库的？**

## 3. 本讲源码地图

| 文件 | 行数(约) | 作用 |
|---|---|---|
| `Makefile` | 706 | 顶层入口。定义默认目标、输出目录、编译命令宏、库/工具/viewer 的链接规则、install 等。 |
| `Makerules` | 403 | 构建配置中心。解析 `build=` 编译模式、`xps/svg/html` 等功能开关（转成 `FZ_ENABLE_*` 宏）、OS 探测、pkg-config 探测系统库。 |
| `Makethird` | 410 | 第三方库集成。对每个库判断「用系统的还是用内置的」，据此把源码加进 `THIRD_SRC` 或把链接参数加进 `THIRD_LIBS`。 |
| `Makelists` | 1060 | 纯粹的源码清单。逐个第三方库列出要编译的 `.c/.cc/.cpp` 文件列表，被 `Makethird` include。 |
| `.gitmodules` | 87 | git 子模块登记表。列出 `thirdparty/` 下 18 个子模块的仓库地址与本地路径。 |
| `include/mupdf/fitz/version.h` | 31 | 版本号定义，`Makefile` 会从中 grep 出主次修订号用于动态库命名。 |
| `docs/guide/install.md` | 59 | 官方安装文档，给出 `make` / `make tools` 等命令与输出路径说明。 |

## 4. 核心概念与源码讲解

### 4.1 顶层 Makefile：目标、输出目录与编译命令

#### 4.1.1 概念说明

`Makefile` 是整个构建的入口。当你在仓库根目录敲下 `make`，GNU Make 会读这个文件，并执行第一个目标（默认目标）。MuPDF 的 `Makefile` 并不是把所有逻辑都堆在一个文件里，而是用 `include` 把另外三个文件拼接进来，自己则负责：

1. 决定默认构建什么。
2. 决定编译产物放到哪个目录（`OUT`）。
3. 定义把 `.c` 变成 `.o`、把 `.o` 打包成 `.a`、把 `.o` 链接成可执行文件这些「命令宏」。
4. 声明 `libmupdf`、`mutool`、各类 viewer、examples、install 等具体目标。

#### 4.1.2 核心流程

读取与组装顺序如下（伪代码）：

```
1. -include user.make          # 可选：开发者本地覆盖变量
2. build 默认为 release
3. 默认目标: apps libs
4. include Makerules           # 注入编译模式/功能开关/系统探测
5. 计算 OUT = build/<前缀><build><后缀>
6. include Makethird           # 注入第三方库源码清单与链接参数
   └─ Makethird 内部 include Makelists
7. 定义命令宏: CC_CMD / AR_CMD / LINK_CMD ...
8. 定义模式规则: %.o 怎么来、%.a 怎么来
9. 定义文件清单: MUPDF_SRC / THIRD_SRC / MUTOOL_SRC ...
10. 定义最终目标: libmupdf.a / mutool / examples / install ...
```

输出目录 `OUT` 是一个组合量，可以形式化写成：

\[ OUT \;=\; \mathrm{build}/\,\text{build\_prefix}\,\cdot\,\text{build}\,\cdot\,\text{build\_suffix} \]

其中 `build` 是编译模式（默认 `release`），`build_prefix`/`build_suffix` 由是否 `shared=yes`、是否 `OS=wasm`、是否开启 `tesseract`/`barcode` 等附加。所以最常见的输出路径就是 `build/release/`。

#### 4.1.3 源码精读

文件开头先允许本地覆盖，再定默认编译模式与默认目标：

[Makefile:L3-L9](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/Makefile#L3-L9) — `-include user.make` 让你可以在不修改 Makefile 的情况下用本地文件覆盖变量；`build` 未指定时默认 `release`；`default: apps libs` 决定了裸敲 `make` 就会同时构建应用程序和库。

紧接着用两次 `include` 把配置文件拉进来，并据此算出输出目录：

[Makefile:L11-L17](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/Makefile#L11-L17) — `include Makerules` 与 `include Makethird`，并在两者之间用 `OUT := build/$(build_prefix)$(build)$(build_suffix)` 确定产物目录。注意顺序：必须先 include Makerules（它设置了 `build_prefix` 等），才能正确算出 `OUT`。

`Makefile` 还从版本头里 grep 出主次修订号，用于给动态库命名（soname）：

[Makefile:L39-L48](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/Makefile#L39-L48) — 从 `include/mupdf/fitz/version.h` 读出 `FZ_VERSION_MAJOR/MINOR/PATCH`，非 Darwin 平台下拼成 `libmupdf.so.29.0` 这样的带版本号文件名。

为了在终端里输出简洁（默认不回显完整命令），`Makefile` 定义了一组 `QUIET_*` 前缀，你看到的 `CC xxx.o` 就是它们产生的：

[Makefile:L59-L71](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/Makefile#L59-L71) — 只有 `verbose=yes` 时才会回显完整命令，否则用 `@ echo "CC $@" ;` 这种「先打印简短提示再执行」的方式。

把 `.c` 编成 `.o` 的核心命令宏：

[Makefile:L74-L75](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/Makefile#L74-L75) — `CC_CMD` 调用编译器，带 `-MMD -MP` 自动生成头文件依赖（后面用 `-include *.d` 拉回来），`-Iinclude` 让源码能找到 `include/mupdf/...` 头文件。

MuPDF 自身的源码清单是用 `wildcard` 自动收集的，并按格式开关增减：

[Makefile:L177-L190](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/Makefile#L177-L190) — 始终纳入 `source/fitz`、`source/pdf`、`source/reflow`、`source/cbz`；而 `source/xps`、`source/svg`、`source/html` 三者受同名变量控制（`make xps=no` 即可裁掉 XPS 支持）。

库的产物形态由 `shared` 变量决定（静态 vs 动态）：

[Makefile:L345-L352](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/Makefile#L345-L352) — 默认（非 shared）产出 `libmupdf.a`；若 `THIRD_OBJ` 非空，则额外产出 `libmupdf-third.a`，把所有第三方库代码单独打包，便于应用按需链接。

命令行工具 `mutool` 的链接规则：

[Makefile:L367-L380](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/Makefile#L367-L380) — `mutool` 由 `source/tools/mutool.c` 等一批源码编译后，与 `libmupdf`、`libmupdf-third`、pkcs7、threads 一起链接而成，并把 `THIRD_LIBS`、`THREADING_LIBS`、`LIBCRYPTO_LIBS` 作为链接依赖。

examples 目标把官方示例编出来（下一讲 u1-l5 就要跑其中的 `example.c`）：

[Makefile:L440-L449](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/Makefile#L440-L449) — `examples` 目标依赖 `docs/examples/` 下的四个 `.c` 示例文件，各自链接库后产出 `example`、`multi-threaded`、`storytest`、`searchtest`。

最重要的「别名目标」聚集在这一段，它们就是你在命令行最常敲的名字：

[Makefile:L472-L479](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/Makefile#L472-L479) — 这里集中声明了 `third`、`libs`、`tools`、`apps`、`extra-apps`、`libmupdf-threads` 等便捷目标。`tools` 只构建命令行工具（不含 viewer），`apps` 还会带上 viewer。

最后是清理与编译模式切换目标：

[Makefile:L588-L604](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/Makefile#L588-L604) — `all` 构建库+apps+extra-apps；`clean` 只删 `OUT` 目录；`nuke` 更彻底地清掉 `build/*` 与生成的字体目录；`release`/`debug`/`sanitize` 是 `$(MAKE) build=xxx` 的快捷写法。

#### 4.1.4 代码实践

**实践目标**：搞清楚裸敲 `make` 到底会构建什么，并把库和工具编出来。

> 说明：MuPDF 的 Makefile **没有 `help` 目标**（用 `grep '^help' Makefile` 查无此项）。所以本实践用「阅读 Makefile + 实际编译」的方式来列出目标。

操作步骤：

1. 列出主要目标。阅读 [Makefile:L472-L479](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/Makefile#L472-L479)，把 `libs`/`tools`/`apps`/`extra-apps`/`examples`/`install` 这几个目标各自依赖什么记成一张表。
2. 如果你是从 git 克隆的源码，先初始化子模块（官方文档 `docs/guide/install.md` 明确要求）：`git submodule update --init --depth 1`。
3. 如果你没有 X11/OpenGL 开发包（viewer 依赖），只编命令行工具即可：`make tools`。否则直接 `make`。
4. 编译完成后验证：`./build/release/mutool -v`，应打印版本号（本 HEAD 为 `1.29.0`，见 `include/mupdf/fitz/version.h`）。

需要观察的现象：编译时会逐行打印 `CC source/fitz/xxx.o`、`AR build/release/libmupdf.a`、`LINK build/release/mutool` 等简短提示（来自 4.1.3 提到的 `QUIET_*` 宏）。

预期结果：在 `build/release/` 下出现 `libmupdf.a`、`libmupdf-third.a`、`mutool`（以及若编了 apps 还会有 `mupdf-gl`/`mupdf-x11`）。请把它们的实际绝对路径记录下来。

如果无法确定运行结果（例如缺依赖导致 viewer 编不出），明确标注「待本地验证」，并改用 `make tools` 这一最小路径。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `include Makethird` 必须放在 `OUT := ...` 之后、而不是文件最开头？
**答案**：因为 `OUT` 的取值依赖 `build_prefix`/`build_suffix`，而这些变量部分由 `Makerules` 设置（如 `shared=yes` 时加 `shared-` 前缀）。`Makethird` 里的编译规则又依赖 `OUT` 作为输出路径。所以顺序必须是：先 include Makerules（设前缀）→ 算 OUT → include Makethird（用 OUT）。

**练习 2**：`make`、`make apps`、`make tools` 三者构建范围有何不同？
**答案**：`make` 走默认目标 `default: apps libs`，等于同时构建 apps 和 libs；`apps` = `TOOL_APPS + VIEW_APPS`（命令行工具 + viewer）；`tools` 只有 `TOOL_APPS`（不含 viewer）。所以在没有图形依赖的机器上，`make tools` 是最小可用路径。

---

### 4.2 Makerules：编译模式、功能裁剪与系统探测

#### 4.2.1 概念说明

`Makerules` 是被 `Makefile` 第一个 include 进来的「配置中心」。它本身不产出任何文件，而是通过设置一堆 Make 变量来影响后续所有编译：

- 把 `build=debug/release/sanitize/...` 翻译成具体的 `CFLAGS`/`LDFLAGS`。
- 把 `xps=no`、`html=no`、`mujs=no` 等开关翻译成 `-DFZ_ENABLE_XXX=0` 宏，从而在编译期裁掉对应功能。
- 探测操作系统（`OS`）、是否支持 objcopy、是否有 pthread、是否有 libcrypto。
- 用 pkg-config 探测系统里已安装的第三方库，为 4.3 节的「系统库 vs 内置库」二选一做准备。

理解 `Makerules` 的关键在于：**MuPDF 的功能裁剪是编译期的**——你不想要的格式/能力，在编译时就被 `FZ_ENABLE_*=0` 宏整块排除掉，而不是运行期判断。这正好呼应 u1-l1 提到的 `config.h` 裁剪机制。

#### 4.2.2 核心流程

```
输入: 命令行变量 (build=, xps=, html=, mujs=, shared=, OS= ...)
   │
   ├─ OS 探测 (uname) → 决定 SO 后缀、HAVE_OBJCOPY、HAVE_PTHREAD ...
   ├─ 功能开关翻译:
   │     html=no → USE_HARFBUZZ/GUMBO/CMARK_GFM=no + 一串 FZ_ENABLE_*=0
   │     mujs=no  → FZ_ENABLE_JS=0
   │     xps=no   → FZ_ENABLE_XPS=0
   │     svg=no   → FZ_ENABLE_SVG=0
   │     ...
   ├─ build 模式 → CFLAGS/LDFLAGS (-g/-O2/-DNDEBUG/sanitize ...)
   ├─ shared=yes → build_prefix += shared-, LIB_CFLAGS=-fPIC
   └─ pkg-config 探测 → 填充 SYS_*_CFLAGS / SYS_*_LIBS
输出: 一组成熟的 CFLAGS/LDFLAGS/SYS_* 变量, 供 Makefile 与 Makethird 使用
```

#### 4.2.3 源码精读

OS 探测与基础警告开关：

[Makerules:L4-L8](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/Makerules#L4-L8) — 若环境未设 `OS`，则用 `uname` 推断；同时定义 `WARNING_CFLAGS = -Wall -Wsign-compare -Wshadow`，所有源码编译都会带上。

功能裁剪最典型的例子是 `html=no`，它会连带关掉一整套 HTML 排版依赖和一堆电子书格式：

[Makerules:L28-L44](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/Makerules#L28-L44) — 关掉 HTML 引擎时，`USE_HARFBUZZ/GUMBO/CMARK_GFM` 全置 no（直接影响 4.3 节是否编译这些第三方库），并通过 `-DFZ_ENABLE_HTML_ENGINE=0`、`-DFZ_ENABLE_EPUB=0`、`-DFZ_ENABLE_MOBI=0` 等宏在 C 源码里排除对应 handler。这正是 u1-l1 所说「EPUB/MOBI/FB2/Office 等共享 HTML 引擎」的编译期体现。

其余格式开关同理，每个都对应一个 `FZ_ENABLE_*` 宏：

[Makerules:L46-L59](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/Makerules#L46-L59) — `xps=no` → `-DFZ_ENABLE_XPS=0`；`svg=no` → `-DFZ_ENABLE_SVG=0`；`extract=no` → `-DFZ_ENABLE_DOCX_OUTPUT=0`（extract 库用于 DOCX 输出）。

`build=` 编译模式翻译成具体优化/调试标志：

[Makerules:L154-L160](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/Makerules#L154-L160) — `debug` 加 `-g`；`release` 加 `-O2 -DNDEBUG` 并在链接时做 dead-strip；若 `build` 取了未知值，则用 `$(error unknown build setting: '$(build)')` 直接报错中止，避免静默用错配置。

默认的系统库名（当走「用系统库」路线时，作为 pkg-config 探测失败后的兜底）：

[Makerules:L203-L216](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/Makerules#L203-L216) — 这里给出 `SYS_FREETYPE_LIBS := -lfreetype2` 等一整套默认链接名，是 4.3 节 `USE_SYSTEM_*=yes` 分支会用到的值。

Linux 上启用 objcopy（用于把字体/连字符数据直接嵌成目标文件，见 Makefile L130-L135）：

[Makerules:L246-L248](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/Makerules#L246-L248) — `HAVE_OBJCOPY := yes` 决定了字体资源是用 `objcopy` 嵌入（更省体积）还是用 hexdump 转 C 数组编译（兜底方案）。

线程与加密支持探测：

[Makerules:L340-L352](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/Makerules#L340-L352) — 用 pkg-config 探测 libcrypto（数字签名/PKCS7），并默认 `HAVE_PTHREAD := yes`、`PTHREAD_LIBS := -lpthread`，这关系到多线程渲染（u9-l2）。

#### 4.2.4 代码实践

**实践目标**：用编译期裁剪亲自验证「功能开关 → `FZ_ENABLE_*` 宏」的传导。

操作步骤：

1. 先正常预览一次会被传给编译器的宏：`make -n build=release 2>&1 | grep -m1 'FZ_ENABLE' || true`（`-n` 只打印不执行）。
2. 再用裁剪方式预览：`make -n html=no mujs=no 2>&1 | grep -o '\-DFZ_ENABLE_[A-Z_]*=0' | sort -u`。

需要观察的现象：第 2 步应能看到 `-DFZ_ENABLE_JS=0`、`-DFZ_ENABLE_HTML=0`、`-DFZ_ENABLE_EPUB=0`、`-DFZ_ENABLE_MOBI=0` 等一串宏。

预期结果：你拿到了一份「关掉 HTML+JS 后被排除的功能清单」。把这份清单和 u1-l1 的格式对照表对照，会发现 EPUB/MOBI/FB2/Office 等 handler 正是被这些宏整块排除的。

> 提示：`make -n` 是只 dry-run 不真正编译的安全做法，适合「观察现象」阶段。

#### 4.2.5 小练习与答案

**练习 1**：如果你只想编出一个「只能看 PDF、不能看电子书、不能跑 JS」的极简 mutool，命令该怎么写？
**答案**：`make tools html=no mujs=no`。这样会通过 `-DFZ_ENABLE_HTML*=0`、`-DFZ_ENABLE_JS=0` 排除 HTML 引擎与 JS，并连带不编译 harfbuzz/gumbo/cmark-gfm/mujs 等第三方库，产物更小。

**练习 2**：`build=sanitize` 和 `build=debug` 有何区别？
**答案**：`debug` 只加 `-g`（带调试符号，无优化）；`sanitize` 在 `-g` 基础上再加 `-fsanitize=undefined,address,leak`（见 Makerules L119-L121），用于在运行期捕获未定义行为、内存越界和泄漏，是排查 mupdf 内存问题的首选模式。

---

### 4.3 Makethird + Makelists：第三方库的集成模式

#### 4.3.1 概念说明

MuPDF 依赖十几个第三方库（freetype 字体、harfbuzz 整形、libjpeg/openjpeg 图片解码、zlib/brotli 解压、jbig2dec、mujs JS 引擎……）。`Makethird` 负责把这些库「接入」构建，而 `Makelists` 只是一份份纯源码清单。

这里有一个贯穿全文件的**统一二选一模式**，理解了它就理解了整个 `Makethird`：

```
对每个第三方库 X:
  ifeq ($(USE_SYSTEM_X),yes)
      → 用系统已装的 X：把 SYS_X_CFLAGS / SYS_X_LIBS 加进 THIRD_LIBS
  else
      → 用内置子模块：把 X_SRC 加进 THIRD_SRC（最终编进 libmupdf-third.a）
        并给出 thirdparty/X 下 .c → .o 的编译规则
```

也就是说：**同一个库，要么链接系统版本，要么把它的源码和 mupdf 一起编进 `libmupdf-third.a`**。默认走「内置」路线（开箱即用、不依赖系统包），通过 `USE_SYSTEM_LIBS=yes` 一键切换到「全部用系统库」。

#### 4.3.2 核心流程

```
USE_SYSTEM_LIBS=yes?
   ├─ 是 → 把所有 USE_SYSTEM_* 默认设为 yes (Makethird 顶部)
   │        每个库走 SYS_* 分支 → 只加链接参数, 不编译子模块源码
   └─ 否 → 每个库走内置分支:
            Makethird 引用 X_SRC (定义在 Makelists)
            Makethird 把 X_SRC 累加进 THIRD_SRC
            THIRD_SRC → THIRD_OBJ (Makefile L171-L173)
            THIRD_OBJ → libmupdf-third.a (Makefile L348-L351)
   └─ 可选库 (tesseract/leptonica/zxing/extract/curl/libarchive)
        只有对应 USE_* =yes 时才进入上述流程, 否则完全不参与编译
```

#### 4.3.3 源码精读

`USE_SYSTEM_LIBS=yes` 是总开关，它会一次性把所有第三方库都默认切到「用系统版」：

[Makethird:L3-L21](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/Makethird#L3-L21) — 设置该总开关后，`USE_SYSTEM_FREETYPE`、`USE_SYSTEM_HARFBUZZ`、`USE_SYSTEM_ZLIB` 等逐个默认为 yes；少数系统通常没有的（如 mujs、jpegxr、lcms2mt、cmark-gfm）显式保持 no，仍走内置。

`Makethird` 通过 include 把源码清单文件拉进来：

[Makethird:L89](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/Makethird#L89) — `include Makelists`，于是 `FREETYPE_SRC`、`HARFBUZZ_SRC`、`ZLIB_SRC` 等变量在这里被定义。

最典型的「二选一」块以 BROTLI 为例：

[Makethird:L91-L108](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/Makethird#L91-L108) — 外层 `USE_BROTLI=yes`（默认开）控制是否启用；内层 `USE_SYSTEM_BROTLI` 决定用系统库（加 `SYS_BROTLI_*`）还是内置（把 `BROTLI_SRC` 加进 `THIRD_SRC` 并给出编译规则）。其余库都是这个套路。

freetype 是核心必装库（字体光栅化的基础）：

[Makethird:L127-L138](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/Makethird#L127-L138) — 注意 freetype 块**没有**外层 `USE_FREETYPE` 判断，说明它始终参与编译（字体是 PDF 渲染的刚需）。`FREETYPE_SRC` 在 Makelists 里展开成 20 余个 `.c` 文件。

mujs（PDF JavaScript 引擎）块：

[Makethird:L212-L225](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/Makethird#L212-L225) — 受 `USE_MUJS`（由 Makerules 里 `mujs=no` 控制） gating。

`Makelists` 本身非常朴素，以 zlib 为例就是一串源文件：

[Makelists:L265-L281](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/Makelists#L265-L281) — `ZLIB_SRC +=` 逐个累加 `adler32.c inflate.c ...`，这些值最终被 Makethird 的内置分支引用。`MUJS_SRC` 更简单，只有一个聚合文件 `one.c`（见 Makelists L259-L263）。

可选库（OCR/条码等）只有在显式开启时才进入流程：

[Makethird:L31-L44](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/Makethird#L31-L44) — `USE_TESSERACT=yes` 时才会去找 tesseract（优先系统版，否则内置子模块），找不到则 `$(error ...)` 报错。条码库 zxing 同理（L66-L77）。

#### 4.3.4 代码实践

**实践目标**：用 dry-run 对比「全内置」与「用系统库」两种路线下，被编译的第三方源文件数量差异。

操作步骤：

1. 全内置（默认）路线下，统计将被编译的第三方 `.c` 数量：`make -n 2>&1 | grep -c 'thirdparty/.*\.c'`。
2. 切到系统库路线再统计：`make -n USE_SYSTEM_LIBS=yes 2>&1 | grep -c 'thirdparty/.*\.c'`。

需要观察的现象：第 2 步的计数应显著下降（理想情况接近 0，因为系统库不再需要编译内置源码）。

预期结果：直观感受到 `USE_SYSTEM_LIBS=yes` 的作用——把第三方库的编译负担转移给系统包管理器，从而加快 mupdf 自身的编译。如果系统没装齐这些 `-dev` 包，第 2 步可能仍会回退编译部分内置库，请如实记录你机器上的实际计数（若无法运行，标注「待本地验证」）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 freetype 的集成块（Makethird L127-L138）没有外层 `USE_FREETYPE` 判断，而 brotli（L91-L108）有？
**答案**：freetype 是字体渲染的刚需，几乎所有格式都依赖它，所以始终编译，没有「关掉」的选项；brotli 只是部分 PDF/XPS 流的可选解压算法，用 `USE_BROTLI`（由 `brotli=no` 触发 `FZ_ENABLE_BROTLI=0`）允许裁掉，以减小体积。

**练习 2**：`Makelists` 文件为什么能写得这么「枯燥」（全是 `X_SRC += path`）？把它单列出来有什么好处？
**答案**：因为第三方库的源文件列表经常随上游版本变化，单独抽成 `Makelists` 可以让 `Makethird` 只关心「集成策略（系统 vs 内置）」，而 `Makelists` 只关心「具体有哪些源文件」，职责分离，升级第三方库时只需改清单、不动策略。

---

### 4.4 子模块清单：thirdparty 依赖图

#### 4.4.1 概念说明

`.gitmodules` 是 git 的子模块登记表。MuPDF 把每个第三方库作为一个独立的 git 仓库（子模块）挂到 `thirdparty/` 下。这样做的目的：

- **版本锁定**：MuPDF 可以精确指定每个依赖用的哪个提交，避免「上游一升级就编译失败」。
- **可选下载**：子模块默认不在 `git clone` 时拉取，需要 `git submodule update --init`，从而让仓库本体保持轻量。
- **浅克隆**：大多数子模块标注 `shallow = true`，初始化时只拉最新一次提交，节省时间和磁盘。

#### 4.4.2 核心流程

```
.gitmodules 登记 N 个子模块 (path + url + branch + shallow)
   │
git clone mupdf            → thirdparty/* 是空目录
git submodule update --init → 按登记表把每个子模块的代码拉到本地
   │
Makethird 根据开关决定编译哪些子模块:
   ├─ 必装: freetype, libjpeg, zlib, openjpeg, jbig2dec, harfbuzz*, gumbo*, lcms2, brotli*, mujs*
   └─ 可选: tesseract, leptonica, zxing-cpp, zint, extract, curl, freeglut
   (* 标记者会因 html=/mujs=/brotli= 等开关而变化)
```

#### 4.4.3 源码精读

`.gitmodules` 每条记录形如 `[submodule "名字"]` + `path` + `url` + 可选 `branch`/`shallow`：

[.gitmodules:L1-L13](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/.gitmodules#L1-L13) — jbig2dec、mujs、freetype、gumbo-parser 的登记。注意 freetype/gumbo/harfbuzz 都用 `branch = artifex`，即 Artifex 维护的定制分支（通常带了 mupdf 需要的补丁），且 `shallow = true`。

注意子模块名与本地路径并不总一致，最典型的是 jpeg：

[.gitmodules:L24-L28](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/.gitmodules#L24-L28) — 子模块名是 `thirdparty/jpeg`，但实际 checkout 到 `thirdparty/libjpeg`（对应 Makelists 里 `LIBJPEG_SRC += thirdparty/libjpeg/...`）。

zlib（PDF 流解压的基础，FlateDecode 用的就是它）：

[.gitmodules:L39-L43](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/.gitmodules#L39-L43) — `thirdparty/zlib`，是 u8-l1「流与过滤管线」会深入讲解的底层依赖之一。

最后一条 cmark-gfm（Markdown 解析，供 HTML 引擎使用）甚至没有 `shallow = true`：

[.gitmodules:L84-L86](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/.gitmodules#L84-L86) — 它是新增依赖（最近的提交 `39101dd81` 正是「允许禁用 cmark-gfm」相关改动），登记较晚，因此没有 shallow 标记。

下表把 18 个子模块按「是否必装」归类（粗略，实际受开关影响）：

| 子模块路径 | 用途 | 必装? | 对应开关 |
|---|---|---|---|
| `thirdparty/freetype` | 字体光栅化 | 是 | 无（始终） |
| `thirdparty/libjpeg` | JPEG(DCT) 解码 | 是 | 无 |
| `thirdparty/zlib` | Flate 解压 | 是 | 无 |
| `thirdparty/openjpeg` | JPEG2000(JPX) 解码 | 是 | 无 |
| `thirdparty/jbig2dec` | JBIG2 解码 | 是 | 无 |
| `thirdparty/lcms2` | 色彩管理(lcms2mt) | 是 | 无 |
| `thirdparty/harfbuzz` | 文本整形 | 随 html | `html=` |
| `thirdparty/gumbo-parser` | HTML5 解析 | 随 html | `html=` |
| `thirdparty/cmark-gfm` | Markdown 解析 | 随 html | `html=` |
| `thirdparty/brotli` | Brotli 解压 | 是(可裁) | `brotli=` |
| `thirdparty/mujs` | JavaScript 引擎 | 是(可裁) | `mujs=` |
| `thirdparty/tesseract` | OCR | 否 | `tesseract=yes` |
| `thirdparty/leptonica` | 图像处理(tesseract 依赖) | 否 | 随 tesseract |
| `thirdparty/zxing-cpp` | 条码识别 | 否 | `barcode=yes` |
| `thirdparty/zint` | 条码后端(zxing 依赖) | 否 | 随 barcode |
| `thirdparty/extract` | DOCX/ODT 输出 | 否(可裁) | `extract=` |
| `thirdparty/curl` | HTTP 远程文档 | 否 | `HAVE_CURL` |
| `thirdparty/freeglut` | mupdf-gl viewer 窗口 | 否 | viewer 构建 |

#### 4.4.4 代码实践

**实践目标**：核对子模块是否已就位，并理解「未初始化子模块 → 编译失败」的因果关系。

操作步骤：

1. 查看子模块状态：`git submodule status`。每行前缀的字符表示状态：空格=已检出、`-`=未初始化、`+`=HEAD 与登记不一致。
2. 若看到大量 `-` 前缀（即未初始化），执行 `git submodule update --init --depth 1`。
3. 再次 `git submodule status` 确认 `thirdparty/freetype`、`thirdparty/zlib`、`thirdparty/libjpeg` 等必装项已就位。
4. 对照上表，确认你打算启用的可选能力（如 OCR）对应的子模块也已初始化。

需要观察的现象：未初始化时，`thirdparty/freetype` 等目录是空的；执行 update 后目录被填充。

预期结果：必装子模块全部就位后，`make tools` 才能成功。如果你跳过步骤 2 直接 `make`，大概率会在编译 freetype/zlib 阶段报「找不到源文件」之类的错误——这正是官方 `docs/guide/install.md` 强调「克隆后必须初始化子模块」的原因。若你的环境无法访问子模块仓库（如离线），请标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么克隆 MuPDF 仓库后，`thirdparty/` 下的目录是空的？
**答案**：因为这些是 git 子模块，`git clone` 默认不会自动拉取子模块内容，需要额外执行 `git submodule update --init`。这是为了让仓库本体保持轻量，并允许使用者按需决定是否拉取。

**练习 2**：如果你只想构建一个支持 OCR 的 mupdf，需要初始化哪些**可选**子模块？
**答案**：至少 `thirdparty/tesseract` 和 `thirdparty/leptonica`（tesseract 依赖 leptonica，见 Makethird L59-L63 的强校验），然后用 `make tesseract=yes` 构建。zxing/extract/curl/freeglut 与 OCR 无关，可不初始化。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「有意识地裁剪 + 编译 + 验证」的全流程：

1. **规划**：决定你要构建的配置——例如「只要命令行工具、支持 PDF 与图片、不要 HTML/JS、启用 sanitize 以便后续调试内存」。
2. **准备依赖**：`git submodule status` 检查必装子模块；按需 `git submodule update --init --depth 1`。
3. **编译**：用对应的开关构建，例如：
   ```
   make tools html=no mujs=no build=sanitize
   ```
4. **验证产物**：
   - 确认输出目录：由于 `build=sanitize`，产物应在 `build/sanitize/`（而不是默认的 `build/release/`）。用 `ls build/sanitize/` 核对。
   - 运行 `./build/sanitize/mutool -v` 确认版本。
   - 用 4.2.4 的 dry-run 思路反向验证：`make -n html=no mujs=no 2>&1 | grep -o '\-DFZ_ENABLE_[A-Z_]*=0' | sort -u`，确认 HTML/JS 确实被裁掉。
5. **记录**：把「命令 → 实际输出路径 → 产物清单 → 被裁掉的功能宏」整理成一张表，作为你后续阅读源码时的构建基线。

> 这一步把「顶层目标(4.1) → 功能裁剪(4.2) → 第三方集成(4.3) → 子模块依赖(4.4)」串成了一条完整链路：你写的每一个 `make` 变量，最终都通过这条链传导到了具体的 `.o` 文件和宏定义上。

## 6. 本讲小结

- MuPDF 的构建由 4 个互相 include 的 Make 文件协作：`Makefile`（目标与命令）、`Makerules`（配置与探测）、`Makethird`（第三方集成策略）、`Makelists`（第三方源码清单）。
- 裸敲 `make` 走默认目标 `apps libs`；缺图形依赖时用 `make tools` 是最小可用路径；产物默认落在 `build/release/`，可被 `build=`/`shared=`/`OS=` 等变量改写。
- 功能裁剪是**编译期**的：`html=no`/`mujs=no`/`xps=no` 等开关在 `Makerules` 里被翻译成 `-DFZ_ENABLE_*=0` 宏，从源头排除对应格式 handler 和第三方库。
- 第三方库遵循统一的「系统版 vs 内置子模块」二选一模式：`USE_SYSTEM_LIBS=yes` 全切系统库，否则把各库源码编进 `libmupdf-third.a`。
- 18 个第三方库以 git 子模块形式放在 `thirdparty/` 下，克隆后必须 `git submodule update --init` 才能编译；其中 freetype/libjpeg/zlib/openjpeg/jbig2dec/lcms2 为必装，tesseract/zxing/extract 等为可选。
- `build=debug/release/sanitize/memento` 提供不同用途的编译模式，其中 `sanitize` 与 `memento` 是排查内存问题的关键工具。

## 7. 下一步学习建议

构建系统能跑通之后，建议按以下顺序继续：

1. **下一讲 u1-l3（源码目录与模块布局）**：在编译产物之外，系统认识 `include/mupdf/fitz`、`include/mupdf/pdf`、`source/*`、`platform/*` 的目录职责，为阅读源码建立地图。
2. **u1-l4（mutool 命令行）**：亲手运行你刚编出来的 `mutool`，看它的子命令分发，验证编译正确性。
3. **u1-l5（第一个渲染程序 example.c）**：用本讲编出的 `libmupdf.a` + `libmupdf-third.a`，编译并运行官方示例，完成从「会编译」到「会用库」的跃迁。
4. 进阶可回头精读 `Makefile` 的「字体/连字符资源嵌入」段（L201-L312）与「install」段（L461-L523），理解 mupdf 如何把字体打包进库、如何系统级安装。
