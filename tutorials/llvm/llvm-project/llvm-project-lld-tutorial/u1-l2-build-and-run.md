# 构建与运行 LLD

## 1. 本讲目标

本讲承接上一讲对 LLD 的整体定位，回答一个最实际的问题：**这一堆源码如何变成一个能用的链接器，以及它产出哪些二进制文件、如何替换系统默认链接器**。

学完本讲你应当能够：

1. 用 CMake 把 LLD 从源码编译出来，理解「随 LLVM 构建」和「独立构建」两种形态的差异。
2. 说清楚一次构建会产出哪些可执行文件、它们为什么是同一个二进制的不同名字（符号链接）。
3. 知道 LLD 的测试套件依赖哪些工具（FileCheck、llvm-readelf、not 等），以及如何运行它们。
4. 把编译出来的 LLD 真正用起来：要么用符号链接覆盖 `/usr/bin/ld`，要么用 `clang -fuse-ld=lld` 调用它。

## 2. 前置知识

在进入源码之前，先建立三个直觉。

**第一，LLD 是 LLVM 的一个「子项目（subproject）」。** LLVM 并不是单一仓库，而是一组工具链（clang、lld、lldb、compiler-rt……），现在统一放在 `llvm-project` 这个 monorepo 里。每个子项目都能以两种身份参与构建：要么作为 LLVM 大树的一部分一起编译，要么把自己当成一个独立项目，把已经编好的 LLVM 当作外部库来链接。LLD 的构建脚本会**自动检测**自己处在哪种身份下，并走不同的配置分支。理解这一点是看懂 LLD `CMakeLists.txt` 的关键。

**第二，CMake 用「目标（target）」来描述构建产物。** 一个 `add_library(lldELF ...)` 定义一个库目标，一个 `add_lld_tool(lld ...)` 定义一个可执行目标，`install(TARGETS ...)` 决定它被 `make install` 拷到哪里。符号链接（symlink）在 CMake 里通常通过自定义命令/宏来创建——LLD 就是这么把一个 `lld` 二进制「变」出 `ld.lld`、`lld-link`、`ld64.lld`、`wasm-ld` 四个名字的。

**第三，链接器很少被直接调用。** 在 Unix 上，编译器驱动（如 `gcc`/`clang`）会在最后一步悄悄调用链接器。所谓「使用 LLD」通常不是手敲 `ld.lld`，而是告诉编译器驱动「请用 LLD 而不是系统默认的 ld」。这正是 `clang -fuse-ld=lld` 的作用，也是本讲实践的落点。

> 名词速查：
> - **monorepo**：把多个相关项目放在同一个 git 仓库里管理。`llvm-project` 就是 LLVM 全家桶的 monorepo。
> - **flavor**：LLD 内部对「ELF / COFF / Mach-O / wasm 哪个后端」的称呼，根据被调用时的程序名（`ld.lld` 等）来决定，下一讲会专门讲。
> - **lit**：LLVM 的测试运行器（test runner），一个基于 Python 的工具，用来跑 `.test` / `.s` / `.ll` 这类端到端测试。
> - **FileCheck**：LLVM 自带的工具，用 `CHECK:` 行做文本模式匹配，是 lit 测试里断言输出的标准手段。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [lld/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/CMakeLists.txt) | LLD 顶层构建入口：判断是否独立构建、声明关键选项（`LLD_BUILD_TOOLS` 等）、用 `add_subdirectory` 把 Common、四个后端、tools/lld、test 都纳入构建。 |
| [lld/tools/lld/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/tools/lld/CMakeLists.txt) | 定义唯一的可执行目标 `lld`，链接全部后端库，并为它创建 4 个符号链接。 |
| [lld/cmake/modules/AddLLD.cmake](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/cmake/modules/AddLLD.cmake) | LLD 自己的 CMake 宏：`add_lld_library`、`add_lld_tool`、`add_lld_symlink`，封装「编译 + 安装 + 建符号链接」的细节。 |
| [lld/test/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/test/CMakeLists.txt) | 声明 lit 测试套件 `check-lld`，列出测试依赖的全部 LLVM 工具。 |
| [lld/test/lit.cfg.py](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/test/lit.cfg.py) | lit 测试运行时的 Python 配置：注入工具路径、设置 `LLD_IN_TEST` 环境变量。 |
| [lld/docs/index.md](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/docs/index.md) | LLD 官方说明，含 Build 与 Using LLD 两节，是官方推荐的最短构建/使用路径。 |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 CMake 构建逻辑**（讲清两种构建形态和顶层选项）、**4.2 构建产物与符号链接**（讲清一个二进制怎么变成四个名字）、**4.3 lit 测试体系与依赖工具**（讲清测试如何运行、依赖什么）。

### 4.1 CMake 构建逻辑：LLD 的两种构建形态

#### 4.1.1 概念说明

LLD 的 `CMakeLists.txt` 要同时服务两种使用场景：

- **随 LLVM 构建（最常见）**：你在 monorepo 顶层 `llvm-project/llvm` 目录上跑 CMake，并用 `-DLLVM_ENABLE_PROJECTS=lld` 告诉它「把 lld 也带上」。这时 `lld/CMakeLists.txt` 不是顶层脚本（`CMAKE_SOURCE_DIR` 指向 `llvm-project/llvm`，不是 `lld`），它会跳过一大段「独立构建」初始化，直接进入正题。
- **独立构建（standalone）**：你单独对着 `lld` 目录跑 CMake，把它当作一个独立项目。这时 CMake 找不到 LLVM 提供的那些已加载的宏和变量，必须自己 `find_package(LLVM)`、自己 `find_program(llvm-tblgen)`、自己 include 一堆 LLVM 的 CMake 模块（`AddLLVM`、`TableGen`、`HandleLLVMOptions`）。

判断方法非常巧妙：比较 `CMAKE_SOURCE_DIR`（整个构建树的根）和 `CMAKE_CURRENT_SOURCE_DIR`（当前脚本所在目录，即 `lld`）。两者相等，说明 `lld` 就是构建根 → 独立构建；否则它只是某个子目录 → 随 LLVM 构建。

#### 4.1.2 核心流程

把 `lld/CMakeLists.txt` 的执行过程抽象成伪代码：

```
# 1. 版本与策略
要求 CMake >= 3.20（并提示 LLVM 24 起需要 3.31）

# 2. 判断身份
if (lld 就是构建根):           # CMAKE_SOURCE_DIR == CMAKE_CURRENT_SOURCE_DIR
    project(lld)
    标记 LLD_BUILT_STANDALONE = TRUE

# 3. 若为独立构建：自己补齐 LLVM 环境
if (独立构建):
    find_package(LLVM)
    find_program(llvm-tblgen)
    include(AddLLVM / TableGen / HandleLLVMOptions ...)
    （若开启测试）准备 FileCheck / not / lit

# 4. 声明本项目的关键选项
option(LLD_BUILD_TOOLS ...)            # 是否真的编译出可执行文件
option(LLD_DEFAULT_LD_LLD_IS_MINGW ...)# ld.lld 默认走 ELF 还是 MinGW

# 5. 用 add_subdirectory 纳入各模块
add_subdirectory(Common)               # 公共基础设施
add_subdirectory(tools/lld)            # 唯一的可执行文件
（若开启测试）add_subdirectory(test / unittests)
add_subdirectory(COFF / ELF / MachO / MinGW / wasm)  # 四个后端 + MinGW 薄包装
```

关键点有两个：一是**身份判断决定走哪条分支**，二是**`add_subdirectory` 的清单决定了哪些目录真正参与编译**。第 5 步那份清单是本讲实践要重点记录的内容。

#### 4.1.3 源码精读

**身份判断（独立构建检测）**。脚本顶部用一行比较来决定是否调用 `project(lld)`：

[CMakeLists.txt:18-23](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/CMakeLists.txt#L18-L23) —— 注释说明「若不是作为 LLVM 的一部分构建，就把 LLD 当独立项目、把 LLVM 当外部库」。只有当 `CMAKE_SOURCE_DIR STREQUAL CMAKE_CURRENT_SOURCE_DIR`（即 lld 自己是构建根）时才 `set(LLD_BUILT_STANDALONE TRUE)`。

**独立构建的环境补齐**。这一大段只在 `LLD_BUILT_STANDALONE` 为真时执行，作用是补上「随 LLVM 构建」时本由顶层脚本提供的东西：

[CMakeLists.txt:28-58](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/CMakeLists.txt#L28-L58) —— 关键行包括 `find_package(LLVM REQUIRED ...)`（找到已安装的 LLVM）、`find_program(LLVM_TABLEGEN_EXE "llvm-tblgen" ...)`（找到 tblgen），以及 `include(AddLLVM)` / `include(TableGen)` / `include(HandleLLVMOptions)`。随 LLVM 构建时，这些 `include` 已由 `llvm/CMakeLists.txt` 提前加载，所以 LLD 这里不必重复。

**测试依赖在独立构建里的处理**。独立构建下，LLD 还要自己想办法凑出 `FileCheck`、`not` 和 `lit`——优先用 LLVM 构建树里现成的预编译版本，找不到就从源码编：

[CMakeLists.txt:65-117](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/CMakeLists.txt#L65-L117) —— 例如它先 `find_package(Python3 ...)`，然后检查 LLVM 工具目录里有没有 `FileCheck`、`not`；没有就把它们从 `utils/FileCheck`、`utils/not` 编出来，并设 `LLD_TEST_DEPS FileCheck not`。这一段解释了「为什么 LLD 测试离不开 LLVM 的那些小工具」。

**关键选项**。`LLD_BUILD_TOOLS` 决定是否真的产出可执行文件（默认 `ON`）：

[CMakeLists.txt:179-186](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/CMakeLists.txt#L179-L186) —— `option(LLD_BUILD_TOOLS "Build the lld tools. If OFF, just generate build targets." ON)`。把它设为 `OFF`，LLD 就只编出库目标（供「把 LLD 当库」的场景用），不编可执行文件。紧随其后的 `LLD_DEFAULT_LD_LLD_IS_MINGW` 则决定 `ld.lld` 这个名字默认走 ELF 还是 MinGW（COFF）后端。

**`add_subdirectory` 清单**。这是「哪些目录参与编译」的最终答案，也是本讲实践要求记录的部分：

[CMakeLists.txt:198-215](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/CMakeLists.txt#L198-L215) —— 依次纳入 `Common`（公共基础设施）、`tools/lld`（分发器与唯一可执行文件）；在 `LLVM_INCLUDE_TESTS` 为真时纳入 `unittests` 与 `test`；之后无条件纳入 `docs`、`COFF`、`ELF`、`MachO`、`MinGW`、`wasm`。注意：四个后端目录（COFF/ELF/MachO/wasm）都是无条件加入的——这呼应上一讲提到的「LLD 不提供构建期开关来逐个启用/禁用目标，它始终是全架构的交叉链接器」。

#### 4.1.4 代码实践

> **实践目标**：亲手完成一次「随 LLVM 构建」，并对照源码确认 `add_subdirectory` 的清单。

**操作步骤**：

1. 克隆 monorepo：
   ```console
   $ git clone https://github.com/llvm/llvm-project llvm-project
   ```
2. 新建并进入构建目录（LLD 禁止 in-source build，必须另开目录）：
   ```console
   $ mkdir build && cd build
   ```
3. 配置。注意最后指向的是 `llvm-project/llvm`，并用 `LLVM_ENABLE_PROJECTS=lld` 把 lld 带上：
   ```console
   $ cmake -DCMAKE_BUILD_TYPE=Release -DLLVM_ENABLE_PROJECTS=lld \
           -DCMAKE_INSTALL_PREFIX=/usr/local ../llvm-project/llvm
   ```
4. 编译并安装（`-j` 按核数调整）：
   ```console
   $ make -j$(nproc) install
   ```
   > 这组命令直接来自官方文档 [docs/index.md:83-89](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/docs/index.md#L83-L89)。
5. 打开 [CMakeLists.txt:198-215](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/CMakeLists.txt#L198-L215)，把每个 `add_subdirectory` 对应的目录、是否被 `if(LLVM_INCLUDE_TESTS)` 包裹，整理成一张表。

**需要观察的现象**：

- 配置阶段 CMake 会打印 `LLD version: ...`（来自 [CMakeLists.txt:138](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/CMakeLists.txt#L138) 的 `message(STATUS "LLD version: ${LLD_VERSION}")`）。
- 安装后在 `CMAKE_INSTALL_PREFIX/bin` 下应能看到 `lld`、`ld.lld`、`lld-link`、`ld64.lld`、`wasm-ld` 这一组文件。

**预期结果**：

| `add_subdirectory` | 是否条件性 | 作用 |
| --- | --- | --- |
| `Common` | 否 | 四后端共享的公共库 `lldCommon` |
| `tools/lld` | 否 | 分发器，产出唯一可执行文件 `lld` 及其符号链接 |
| `unittests` | 是（`LLVM_INCLUDE_TESTS`） | C++ 单元测试（把 LLD 当库） |
| `test` | 是（`LLVM_INCLUDE_TESTS`） | lit 端到端测试套件 |
| `docs` | 否 | Sphinx 文档 |
| `COFF` / `ELF` / `MachO` / `MinGW` / `wasm` | 否 | 四个链接后端 + MinGW 薄包装 |

> 待本地验证：实际编译耗时与机器配置强相关，首次全量构建可能数十分钟到数小时不等。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `lld/CMakeLists.txt` 里要在 `project(lld)` 之前判断 `CMAKE_SOURCE_DIR`？

**参考答案**：因为 `project()` 必须在启用语言编译之前调用，而判断「是否独立构建」决定了后续要不要自己 `find_package(LLVM)`、要不要 `include(AddLLVM)` 等。在顶层脚本的 `CMAKE_SOURCE_DIR` 已知时就完成判断，能让独立构建与随 LLVM 构建共用同一段后续逻辑。

**练习 2**：如果把 `-DLLD_BUILD_TOOLS=OFF` 传给 CMake，构建会发生什么变化？

**参考答案**：`LLD_BUILD_TOOLS` 默认 `ON`（[CMakeLists.txt:179-180](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/CMakeLists.txt#L179-L180)）。设为 `OFF` 后，定义可执行目标的 `add_lld_tool` 宏会设置 `EXCLUDE_FROM_ALL`（见 [AddLLD.cmake:40-42](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/cmake/modules/AddLLD.cmake#L40-L42)），即只生成构建目标、默认不编、不安装。这时 LLD 只产出库目标，供「把 LLD 当库嵌入」使用（第 3 单元会讲）。

---

### 4.2 构建产物与符号链接：一个二进制，四个名字

#### 4.2.1 概念说明

上一讲提过：LLD 是**单个**可执行文件，却同时充当 ELF/COFF/Mach-O/wasm 四个链接器。体现在构建上就是——`tools/lld/CMakeLists.txt` 只定义了**一个**可执行目标 `lld`，然后通过创建符号链接，让它多出 `ld.lld`、`lld-link`、`ld64.lld`、`wasm-ld` 几个名字。运行时，`lld` 根据 `argv[0]`（即被调用时的程序名）判断自己该走哪个后端——这就是「flavor 分发」，下一讲会深入。

为什么不直接编出四个独立的二进制？因为四个后端的代码会全部链进同一个 `lld`（无论你这次链接实际用哪个后端），分四个二进制只会浪费磁盘和构建时间。用符号链接共享一份机器码，是这种「单二进制多身份」设计的标准做法。

#### 4.2.2 核心流程

```
# tools/lld/CMakeLists.txt
add_lld_tool(lld lld.cpp ...)               # 1. 定义可执行目标 lld
lld_target_link_libraries(lld PRIVATE        # 2. 把全部后端库链进来
    lldCommon lldCOFF lldELF lldMachO lldMinGW lldWasm)

LLD_SYMLINKS_TO_CREATE = lld-link ld.lld ld64.lld wasm-ld
foreach(link in 上述列表):                   # 3. 为每个名字建一个指向 lld 的符号链接
    add_lld_symlink(link, lld)
```

`add_lld_symlink` 内部做两件事：在构建树里创建符号链接（`llvm_add_tool_symlink`），并注册一个安装目标，让 `make install` 时也把符号链接拷到安装目录（`llvm_install_symlink`）。

#### 4.2.3 源码精读

**定义唯一的可执行目标**：

[tools/lld/CMakeLists.txt:6-11](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/tools/lld/CMakeLists.txt#L6-L11) —— `add_lld_tool(lld lld.cpp SUPPORT_PLUGINS GENERATE_DRIVER)`。`SUPPORT_PLUGINS` 表示支持插件机制，`GENERATE_DRIVER` 用于把 `lld` 接入 LLVM 统一的 `llvm-driver`（一个把多个工具收拢进单一入口的实验性机制）。

**链接全部后端库**：

[tools/lld/CMakeLists.txt:27-35](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/CMakeLists.txt#L27-L35) —— 把 `lldCommon`、`lldCOFF`、`lldELF`、`lldMachO`、`lldMinGW`、`lldWasm` 全链进 `lld`。这一行就是「单二进制含四后端」的物证：四个后端的代码同时存在于同一个可执行文件里。

**声明并创建符号链接**：

[tools/lld/CMakeLists.txt:37-44](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/CMakeLists.txt#L37-L44) —— 默认 `LLD_SYMLINKS_TO_CREATE` 为 `lld-link ld.lld ld64.lld wasm-ld`，`foreach` 对每个名字调用 `add_lld_symlink(${link} lld)`，让它们都指向 `lld`。注意这里**没有**单独为 MinGW 创建名字——MinGW 复用 `ld.lld` 这个名字（由 4.1 提到的 `LLD_DEFAULT_LD_LLD_IS_MINGW` 控制默认后端）。

**`add_lld_symlink` 宏的实现**：

[AddLLD.cmake:75-87](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/cmake/modules/AddLLD.cmake#L75-L87) —— 正常路径下调 `llvm_add_tool_symlink(LLD ${name} ${dest} ALWAYS_GENERATE)`（建链接）和 `llvm_install_symlink(LLD ${name} ${dest} ALWAYS_GENERATE)`（注册安装）。开头那段 `if(LLVM_TOOL_LLVM_DRIVER_BUILD ...)` 是为统一 driver 模式准备的特例，把符号链接变成 driver 的别名，不影响普通理解。

**`add_lld_tool` 宏对 `LLD_BUILD_TOOLS` 的处理**：

[AddLLD.cmake:38-73](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/cmake/modules/AddLLD.cmake#L38-L73) —— 第 40-42 行：若 `NOT LLD_BUILD_TOOLS` 则 `set(EXCLUDE_FROM_ALL ON)`，即目标存在但默认不构建。`LLD_BUILD_TOOLS` 为真时还会 `install(TARGETS ...)`，把可执行文件拷到 `LLD_TOOLS_INSTALL_DIR`（默认即 `bin`，见 [CMakeLists.txt:120-122](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/CMakeLists.txt#L120-L122)）。

#### 4.2.4 代码实践

> **实践目标**：验证「四个名字指向同一个二进制」，并真正用它链接一个程序。

**操作步骤**：

1. 编译完成后，进入构建目录的 `bin/`，列出 lld 相关文件：
   ```console
   $ ls -l bin/ | grep -E 'lld|wasm-ld'
   ```
2. 用 `readelf` 确认它们是同一份内容（符号链接指向 `lld`）：
   ```console
   $ readlink -f bin/ld.lld
   $ readlink -f bin/lld-link
   ```
3. 分别运行 `--version`，观察分发到不同后端：
   ```console
   $ bin/ld.lld --version    # 应显示 LLD，走 ELF
   $ bin/lld-link --version  # 应显示 LLD，走 COFF
   ```
4. 写一个最小 C 程序 `hello.c`，用 clang 指定 LLD 来链接：
   ```console
   $ cat > hello.c <<'EOF'
   int main(void) { return 0; }
   EOF
   $ clang -fuse-ld=lld hello.c -o hello
   ```
   > 这一用法来自官方文档 [docs/index.md:103-105](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/docs/index.md#L103-L105)。
5. 验证输出确实由 LLD 产生：
   ```console
   $ readelf --string-dump .comment hello
   ```
   > 来自 [docs/index.md:108-111](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/docs/index.md#L108-L111)：若含字符串 `Linker: LLD` 即说明是 LLD 链接的。

**需要观察的现象**：

- 第 1 步应看到 `lld` 是普通文件，`ld.lld` / `lld-link` / `ld64.lld` / `wasm-ld` 是符号链接（`->` 指向 `lld`）。
- 第 3 步，几个名字都打印 `LLD ...` 版本信息，但内部默认后端不同。
- 第 5 步，`.comment` 段里出现 `Linker: LLD ...` 字样。

**预期结果**：四个符号链接全部解析到同一个 `lld` 二进制；`hello` 程序能正常运行，且其 `.comment` 段证明由 LLD 链接。

> 待本地验证：若系统 clang 未启用 `-fuse-ld=lld` 支持，可能需要确认 clang 版本足够新；Windows/macOS 上的命令略有不同。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `lld-link`、`ld64.lld`、`wasm-ld` 不各自编一个独立二进制？

**参考答案**：因为 [tools/lld/CMakeLists.txt:27-35](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/CMakeLists.txt#L27-L35) 把四个后端库都链进了同一个 `lld`，分四个二进制只是复制四份同样的机器码。用符号链接共享一份二进制，再靠运行时根据 `argv[0]` 做 flavor 分发，既省空间又简化构建。

**练习 2**：如果我希望 `ld.lld` 默认走 MinGW（COFF）而非 ELF，该用哪个 CMake 选项？

**参考答案**：用 `-DLLD_DEFAULT_LD_LLD_IS_MINGW=ON`（[CMakeLists.txt:182-186](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/CMakeLists.txt#L182-L186)），它会在编译期定义宏 `LLD_DEFAULT_LD_LLD_IS_MINGW=1`，改变 `ld.lld` 这个名字的默认后端。

---

### 4.3 lit 测试体系与依赖工具

#### 4.3.1 概念说明

LLD 的测试分两大类，本讲关注第一类：

- **端到端测试（`test/` 目录）**：用 lit 跑，每个测试用 `.s`/`.test`/`.ll` 文件描述「编译一段输入 → 用 lld 链接 → 用 FileCheck 校验输出」。目录按后端分：`test/ELF`、`test/COFF`、`test/MachO`、`test/wasm`、`test/MinGW`，外加 `test/Unit`（C++ 单元测试的 lit 包装）。
- **C++ 单元测试（`unittests/` 目录）**：用 GoogleTest，主要测「把 LLD 当库」的场景（`AsLibELF`、`AsLibAll`），第 9 单元会专门讲。

lit 测试本质上是 shell 风格脚本，但它依赖一大批工具：`FileCheck`（断言输出）、`llvm-readelf`/`llvm-readobj`（查看 ELF）、`llvm-objdump`（反汇编）、`not`（断言某命令失败）、`llvm-mc`（汇编出测试输入）等等。这些工具绝大多数来自 LLVM，所以 `test/CMakeLists.txt` 第一件事就是**把它们列成测试依赖**，确保跑 `check-lld` 前这些工具都已编好。

这里还有一个对「把 LLD 当库」至关重要的细节：`lit.cfg.py` 会设置环境变量 `LLD_IN_TEST`，让 `lld` 在**同一个进程里把 main 连跑两遍**，以验证它能否正确清理全局状态、可被反复调用。这与「LLD 可作为库」的特性直接相关。

#### 4.3.2 核心流程

```
# test/CMakeLists.txt
LLD_TEST_DEPS = lld LLDUnitTests
if (不是独立构建):
    把一大堆 LLVM 工具(FileCheck/llvm-readelf/not/...)追加进 LLD_TEST_DEPS
add_lit_testsuite(check-lld, "Running lld test suite", ..., DEPENDS LLD_TEST_DEPS)

# 运行测试
$ cmake --build . --target check-lld
  → lit 加载 lit.site.cfg.py（注入工具路径）
  → 对每个测试文件执行 RUN 行，用 FileCheck 校验输出
```

`LLD_IN_TEST` 的作用（在 `lit.cfg.py`）：

```
run_lld_main_twice = lit_config.params.get("RUN_LLD_MAIN_TWICE", False)
if not run_lld_main_twice:
    设 LLD_IN_TEST = "1"     # 每个进程里 lld 的 main 跑 1 次（其实是跑 1 遍再退出）
else:
    设 LLD_IN_TEST = "2"     # 跑 2 遍，额外验证可重入/全局状态清理
```

#### 4.3.3 源码精读

**测试依赖清单**：

[test/CMakeLists.txt:42-83](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/test/CMakeLists.txt#L42-L83) —— `LLD_TEST_DEPS lld LLDUnitTests` 是基础；在非独立构建时追加 `FileCheck`、`count`、`llc`、`llvm-ar`、`llvm-readelf`、`llvm-readobj`、`llvm-objdump`、`llvm-nm`、`not`、`split-file`、`yaml2obj`、`obj2yaml` 等约 30 个工具。这段就是「LLD 测试离不开 LLVM 工具链」的物证——很多测试需要先用 `llvm-mc` 汇编出特定输入，再用 `llvm-readobj` 检查输出。

**`check-lld` 目标**：

[test/CMakeLists.txt:85-88](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/test/CMakeLists.txt#L85-L88) —— `add_lit_testsuite(check-lld ...)` 定义了 `check-lld` 这个目标，依赖上面全部 `LLD_TEST_DEPS`。所以 `cmake --build . --target check-lld` 会先确保 lld 及所有 LLVM 工具就绪，再跑 lit。

**`LLD_IN_TEST` 与「连跑两遍 main」**：

[test/lit.cfg.py:100-107](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/test/lit.cfg.py#L100-L107) —— 注释明确写道：`LLD_IN_TEST` 决定 `main` 在每个进程里跑多少次，用来测试「LLD 是否在正确清理自己、重置全局状态」，这对「作为库使用」很重要。`RUN_LLD_MAIN_TWICE` 参数打开时设为 `"2"`，会连跑两遍，并因「很多 wasm 测试会失败」而把 `wasm` 目录排除。

**测试后缀与排除项**：

[test/lit.cfg.py:25-30](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/test/lit.cfg.py#L25-L30) —— lit 只把 `.ll`、`.s`、`.test`、`.yaml`、`.objtxt` 当测试文件，并排除 `Inputs` 目录（那是测试用的辅助输入，不是测试本身）。

**版本号固定化**：

[test/lit.cfg.py:98](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/test/lit.cfg.py#L98) —— `config.environment["LLD_VERSION"] = "LLD 1.0"` 设了一个假的固定版本号，避免真实版本号写进测试预期输出里导致每次发版都要改测试。

#### 4.3.4 代码实践

> **实践目标**：跑一遍 ELF 后端的一小组测试，并看懂一个 `.test` 文件的 RUN/CHECK 行。

**操作步骤**（源码阅读型 + 可选运行）：

1. 在构建目录里只跑 `check-lld` 的 ELF 子集（比全量快得多）：
   ```console
   $ cmake --build . --target check-lld   # 全量（耗时）
   # 或只跑 ELF 目录：
   $ bin/llvm-lit ../llvm-project/lld/test/ELF -j$(nproc)
   ```
2. 任意挑一个 ELF 测试文件，例如 `test/ELF/` 下与 `--gc-sections` 相关的 `.test`，打开阅读。
3. 对照 [test/lit.cfg.py:41-60](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/test/lit.cfg.py#L41-L60)，确认测试里出现的 `llvm-readelf`、`llvm-objdump` 等工具名都来自这份 `tool_patterns`。
4. 思考：为什么这些测试需要 `FileCheck` 而不是直接看退出码？（因为链接器的大量行为体现在**输出文件的内容**上，必须用模式匹配去断言段布局、重定位结果等。）

**需要观察的现象**：

- lit 会逐个测试打印 `PASS` / `FAIL`，并在失败时给出 `RUN` 行的命令和 `FileCheck` 的差异。
- 若开启了 `RUN_LLD_MAIN_TWICE`，部分 wasm 测试会被排除（见 [lit.cfg.py:108-112](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/test/lit.cfg.py#L108-L112)）。

**预期结果**：`check-lld` 全绿（在官方支持的平台上）；能说清一个 `.test` 文件里 `RUN:` 行如何调用 lld、`CHECK:` 行如何用 FileCheck 断言输出。

> 待本地验证：测试结果取决于构建时启用的架构、线程、可选库（zlib/zstd/libxml2）等，部分带 `REQUIRES:` 标记的测试在缺条件时会显示 `UNSUPPORTED` 而非失败。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `test/CMakeLists.txt` 要把 `FileCheck`、`llvm-readelf` 等这么多工具列为测试依赖？

**参考答案**：因为 [test/CMakeLists.txt:43-83](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/test/CMakeLists.txt#L43-L83) 这些 lit 测试不是单纯调用 lld，它们还要先用 `llvm-mc` 造输入、用 `llvm-readobj`/`llvm-readelf` 查输出、用 `FileCheck` 做断言、用 `not` 验证失败用例。把它们列为依赖能保证 `check-lld` 运行前这些工具都已编好，避免「测试因工具缺失而误报失败」。

**练习 2**：`LLD_IN_TEST=2`（即 `RUN_LLD_MAIN_TWICE`）想测的是什么？

**参考答案**：它让 `lld` 的 `main` 在同一个进程里连跑两遍（[lit.cfg.py:100-107](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/test/lit.cfg.py#L100-L107)），用来验证 LLD 是否在每次运行后正确清理全局状态、能被反复调用——这正是「把 LLD 当库嵌入」所必需的可重入性。注释也直接点明「这对作为库使用很重要」。

---

## 5. 综合实践

把三个最小模块串起来，完成下面这个**「从源码到链接出可执行文件」的端到端任务**：

1. **构建**：按 4.1.4 的步骤用 `-DLLVM_ENABLE_PROJECTS=lld` 配置并编译 LLD。
2. **盘点产物**：用 `ls -l` 列出 `bin/` 下的 lld 家族，用 `readlink -f` 确认 `ld.lld`、`lld-link`、`ld64.lld`、`wasm-ld` 都指向同一个 `lld`；并对照 [tools/lld/CMakeLists.txt:37-44](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/tools/lld/CMakeLists.txt#L37-L44) 解释这组符号链接的来源。
3. **替换默认链接器**：用 4.2.4 的 `clang -fuse-ld=lld` 链接一个 hello world，并用 `readelf --string-dump .comment` 确认 `Linker: LLD`（参考 [docs/index.md:108-111](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/docs/index.md#L108-L111)）。
4. **跑测试**：执行 `cmake --build . --target check-lld`（或只跑 `test/ELF` 子集），打开一个 ELF `.test` 文件，标注其中调用了哪些 [test/lit.cfg.py:41-60](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/test/lit.cfg.py#L41-L60) 里登记的工具。
5. **记录清单**：把 [CMakeLists.txt:198-215](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/CMakeLists.txt#L198-L215) 里每个 `add_subdirectory` 对应的目录及其职责写成一张表，作为后续阅读源码的索引。

**交付物**：一张「add_subdirectory 清单表」+ 一段 `readelf --string-dump .comment` 的输出截图/文本 + 一份标注了 RUN/CHECK 工具调用的 `.test` 文件。

> 待本地验证：综合实践的每一步都依赖一次真实的本地构建，耗时与机器强相关；若全量构建太慢，可只构建 `lld` 目标（`cmake --build . --target lld`）来加速验证。

## 6. 本讲小结

- LLD 有两种构建形态：**随 LLVM 构建**（`-DLLVM_ENABLE_PROJECTS=lld`，最常用）和**独立构建**（`LLD_BUILT_STANDALONE`，自己 `find_package(LLVM)`），由 `CMAKE_SOURCE_DIR` 与 `CMAKE_CURRENT_SOURCE_DIR` 是否相等来区分。
- 顶层 [CMakeLists.txt:198-215](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/CMakeLists.txt#L198-L215) 用 `add_subdirectory` 无条件纳入四个后端目录，印证「LLD 始终是全架构交叉链接器，无构建期开关逐个启停目标」。
- 一次构建只产出**一个**可执行目标 `lld`，再通过 [tools/lld/CMakeLists.txt:37-44](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/tools/lld/CMakeLists.txt#L37-L44) 的符号链接变出 `ld.lld`/`lld-link`/`ld64.lld`/`wasm-ld` 四个名字；运行时根据 `argv[0]` 分发后端（下一讲详述）。
- `LLD_BUILD_TOOLS`（默认 `ON`）控制是否真的编出可执行文件；设 `OFF` 时只产库目标，供「把 LLD 当库」使用。
- lit 测试（`check-lld`）重度依赖 LLVM 工具链（FileCheck、llvm-readelf、not 等，见 [test/CMakeLists.txt:42-83](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/test/CMakeLists.txt#L42-L83)），并通过 `LLD_IN_TEST` 环境变量连跑两遍 `main` 来验证可重入性。
- 实际使用 LLD 通常不直接调用，而是 `clang -fuse-ld=lld` 或符号链接覆盖 `/usr/bin/ld`；可用 `readelf --string-dump .comment` 确认输出含 `Linker: LLD`。

## 7. 下一步学习建议

到这里你已经能把 LLD 编出来并跑通它的测试。下一讲 **u1-l3「目录结构与多后端源码组织」** 会带你走进 `Common/`、`ELF/`、`COFF/`、`MachO/`、`wasm/` 各自内部，看清每个后端目录里都重复出现的模块（Driver/SymbolTable/Symbols/Writer 等）——也就是本讲 `add_subdirectory` 清单的「内部细节」。

再之后，**u1-l4「单一可执行文件与 flavor 分发机制」** 会解释本讲留下的那个核心问题：同一个 `lld` 二进制，运行时究竟如何根据 `argv[0]` 或 `-flavor` 选项决定走哪个后端的 `link()`。建议在进入第二单元的 ELF 主线之前，先完成这两讲，建立完整的「外部→内部」地图。
