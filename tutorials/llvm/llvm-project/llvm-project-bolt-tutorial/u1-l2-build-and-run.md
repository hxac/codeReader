# 构建与运行 BOLT：CMake 构建、目标架构与工具一览

> 本讲是「认识 BOLT」单元的第 2 讲（u1-l2），承接 [u1-l1](u1-l1-bolt-overview.md) 对 BOLT 定位的介绍，带你把 BOLT 从源码「真正编译出来」，并认识编译产物里那一堆可执行文件各自干什么。

## 1. 本讲目标

学完本讲，你应该能够：

1. 独立写出一条完整的 `cmake -G Ninja ... + ninja bolt` 构建命令，并说清每个关键选项（`LLVM_ENABLE_PROJECTS`、`LLVM_TARGETS_TO_BUILD`、`CMAKE_BUILD_TYPE`、`LLVM_ENABLE_ASSERTIONS`）的作用。
2. 解释 BOLT 作为「LLVM 子项目」是如何被挂到 LLVM 总构建里的，并能区分「声明身份」与「真正被纳入构建」这两件不同的事。
3. 理解 `BOLT_TARGETS_TO_BUILD` 与 `LLVM_TARGETS_TO_BUILD` 的约束关系，知道 BOLT 当前支持哪几个目标架构。
4. 认出 `bin/` 下产出的各个可执行文件（`llvm-bolt`、`perf2bolt`、`llvm-boltdiff`、`merge-fdata`、`llvm-bolt-heatmap`、`llvm-bat-dump`、`llvm-bolt-binary-analysis`）的用途。

## 2. 前置知识

在动手之前，先建立几个直觉。如果你已经熟悉 LLVM 的构建套路，可以跳过本节。

- **LLVM 是一个 monorepo（单仓库）**：`llvm-project` 仓库里同时放着 `llvm/`、`clang/`、`lld/`、`bolt/` 等多个「子项目」。配置时站在 `llvm/` 目录上执行 `cmake`，通过 `-DLLVM_ENABLE_PROJECTS="bolt"` 决定把哪些子项目一起编译。
- **CMake + Ninja 是 LLVM 的标准构建组合**：`cmake -G Ninja` 生成 `build.ninja` 文件，再用 `ninja <target>` 实际编译。`ninja bolt` 里的 `bolt` 是一个「元目标（metatarget）」，构建它会顺带把 BOLT 的所有工具都编出来。
- **「目标架构（target）」**指 BOLT 能处理哪种 CPU 的二进制。BOLT 不是解释所有 CPU 的通用工具，它只认得有限的几种架构（X86、AArch64、RISCV）。
- **assertions（断言）**：LLVM 开发期间建议开 `-DLLVM_ENABLE_ASSERTIONS=ON`，它会在代码里启用大量 `assert(...)` 检查，方便早发现问题；发布构建通常会关掉以换性能。BOLT 官方文档默认推荐开着。

如果你对「后链接优化」「profile」这些概念还陌生，请先读 [u1-l1](u1-l1-bolt-overview.md)。

## 3. 本讲源码地图

本讲涉及的文件很少，但它们决定了 BOLT 怎么被构建出来：

| 文件 | 作用 |
| --- | --- |
| `bolt/CMakeLists.txt` | BOLT 顶层构建脚本：声明子项目身份、确定目标架构、构建运行时库、挂载 `lib/` 与 `tools/`。本讲最重要的文件。 |
| `bolt/tools/CMakeLists.txt` | 列出要编译的所有工具子目录，决定 `bin/` 下会产出哪些可执行文件。 |
| `bolt/tools/driver/CMakeLists.txt` | 定义主驱动 `llvm-bolt`，并用「符号链接」派生出 `perf2bolt` 和 `llvm-boltdiff`。 |
| `bolt/tools/driver/llvm-bolt.cpp` | 主驱动入口 `main()`，根据 `argv[0]`（即被调用的名字）分发到三种模式。 |
| `bolt/README.md` / `bolt/docs/GettingStarted.md` | 官方构建与使用说明，给出推荐的 `cmake`/`ninja` 命令。 |
| `llvm/CMakeLists.txt`、`llvm/cmake/modules/AddLLVM.cmake` | （BOLT 之外的总构建）定义 `LLVM_ENABLE_PROJECTS` 机制，解释「子项目」是怎么被纳入构建的。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：① BOLT 如何作为子项目被挂载；② 构建命令与 `ninja bolt` 目标；③ `bin/` 下各工具速览。

### 4.1 BOLT 作为 LLVM 子项目：顶层 CMakeLists.txt 的挂载方式

#### 4.1.1 概念说明

很多人第一次构建 BOLT 会疑惑：为什么 BOLT 的源码在 `bolt/` 目录，但构建命令却要在 `llvm/` 目录上执行 `cmake`？答案是 BOLT 在设计上就是「LLVM 的一个子项目」（和 clang、lld 同级），它高度依赖 LLVM 的库（`MC`、`Object`、`Support` 等），所以必须搭着 LLVM 一起编。

这里有一个**容易被误解的关键点**，本讲的实践任务也会触及它：

- **声明身份** ≠ **被纳入构建**。
- `bolt/CMakeLists.txt` 里有一行 `set(LLVM_SUBPROJECT_TITLE "BOLT")`，它的作用是「告诉 LLVM 总构建：我这个子项目叫 BOLT」，主要用于在 IDE（Visual Studio/XCode）里生成文件夹名称，**并不是**把 BOLT 加进构建的开关。
- **真正**把 BOLT 加进构建的，是你在 `cmake` 命令行里写的 `-DLLVM_ENABLE_PROJECTS="bolt"`。

#### 4.1.2 核心流程

把 BOLT 纳入 LLVM 构建的真实流程是：

1. 用户配置时写 `-DLLVM_ENABLE_PROJECTS="bolt"`。
2. `llvm/CMakeLists.txt` 维护一份已知项目清单 `LLVM_ALL_PROJECTS`，其中包含 `"bolt"`。
3. 对每个被启用的项目，总构建把它的源码目录记到 `LLVM_EXTERNAL_BOLT_SOURCE_DIR`，并把开关 `LLVM_TOOL_BOLT_BUILD` 置为 `ON`。
4. 总构建随后把 `bolt/` 目录 `add_subdirectory` 进来，于是开始执行 `bolt/CMakeLists.txt`。
5. `bolt/CMakeLists.txt` 顶部先 `set(LLVM_SUBPROJECT_TITLE "BOLT")` 声明身份，再处理目标架构、运行时库、子目录等。

换句话说：`LLVM_ENABLE_PROJECTS=bolt` 是「请把我加进来」的请求；`LLVM_SUBPROJECT_TITLE` 是「我叫什么名字」的登记。两者配合，BOLT 才成为 LLVM 树的一部分。

#### 4.1.3 源码精读

BOLT 顶层脚本的第一件正事就是声明身份：

[bolt/CMakeLists.txt:10-10](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/CMakeLists.txt#L10-L10) —— 设置 `LLVM_SUBPROJECT_TITLE` 为 `"BOLT"`，向 LLVM 总构建登记子项目名。

紧跟着它判断「是不是独立构建」（standalone），即是否脱离 LLVM 总树单独编译 BOLT：

[bolt/CMakeLists.txt:18-21](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/CMakeLists.txt#L18-L21) —— 当 `CMAKE_SOURCE_DIR` 就是 BOLT 自身目录时，标记为独立构建并 `project(bolt)`。本讲的常规流程（站在 `llvm/` 上配 cmake）不是这种模式，但 BOLT 也支持单独编译。

`LLVM_SUBPROJECT_TITLE` 这个变量的真正用途，总构建里有明确注释：

[llvm/cmake/modules/AddLLVM.cmake:7-20](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/llvm/cmake/modules/AddLLVM.cmake#L7-L20) —— 注释直言「标题没有语义上的重要性（not semantically significant），只是用来在 IDE 工程里建文件夹」。所以它不是纳入构建的开关。

而「纳入构建」的开关在 LLVM 总脚本里：

[llvm/CMakeLists.txt:138-138](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/llvm/CMakeLists.txt#L138-L138) —— `LLVM_ALL_PROJECTS` 清单里登记了 `"bolt"`，因此 `bolt` 是一个「已知项目」，可以被 `LLVM_ENABLE_PROJECTS` 选中。

[llvm/CMakeLists.txt:295-323](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/llvm/CMakeLists.txt#L295-L323) —— 对每个启用项目，把目录记到 `LLVM_EXTERNAL_<PROJ>_SOURCE_DIR`，并强制设置 `LLVM_TOOL_<PROJ>_BUILD=ON`。这才是 BOLT 真正进入构建的环节。

#### 4.1.4 代码实践

**实践目标**：亲手验证「声明身份」与「纳入构建」是两件事。

**操作步骤**：

1. 打开 [bolt/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/CMakeLists.txt#L10-L10)，定位第 10 行 `set(LLVM_SUBPROJECT_TITLE "BOLT")`。
2. 打开 [llvm/cmake/modules/AddLLVM.cmake 第 7–20 行](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/llvm/cmake/modules/AddLLVM.cmake#L7-L20)，阅读 `get_subproject_title` 函数及其上方注释。
3. 打开 [llvm/CMakeLists.txt 第 138 行](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/llvm/CMakeLists.txt#L138-L138) 与 [第 295–323 行](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/llvm/CMakeLists.txt#L295-L323)。

**需要观察的现象 / 预期结果**：

- 你会发现 `LLVM_SUBPROJECT_TITLE` 在源码里只被 `get_subproject_title` 这个「取名字」的函数读取，注释明确说它「没有语义重要性」。
- 真正控制 BOLT 是否编译的是 `LLVM_TOOL_BOLT_BUILD` 与 `LLVM_ENABLE_PROJECTS`，而第 10 行的那一行只负责「登记名字」。
- 因此对本讲实践任务的准确回答是：**`LLVM_SUBPROJECT_TITLE "BOLT"` 让 BOLT 在 LLVM 总构建中以「BOLT」这个身份出现（用于 IDE 分组与显示），但让 BOLT 真正成为 LLVM 树一部分的是命令行里的 `-DLLVM_ENABLE_PROJECTS="bolt"`，它驱动总构建把 `bolt/` 目录纳入编译。**

> 本实践为源码阅读型，不修改任何文件，可在不实际编译的情况下完成。

#### 4.1.5 小练习与答案

**练习 1**：如果完全删掉 `bolt/CMakeLists.txt` 第 10 行的 `set(LLVM_SUBPROJECT_TITLE "BOLT")`，BOLT 还能正常编译吗？

**参考答案**：能编译。这一行只决定 `get_subproject_title` 返回的名字（用于 IDE 文件夹分组）。删掉后，BOLT 会被当作没有显式标题的子项目（函数里 `LLVM_SUBPROJECT_TITLE` 为空，回退到默认逻辑），构建本身不受影响——因为纳入构建靠的是 `LLVM_ENABLE_PROJECTS=bolt`。

**练习 2**：BOLT 也支持「独立构建（standalone）」。看 [bolt/CMakeLists.txt:18-21](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/CMakeLists.txt#L18-L21)，独立构建和「作为 LLVM 子项目」构建在配置入口上有什么区别？

**参考答案**：独立构建时，`CMAKE_SOURCE_DIR` 就是 `bolt/` 自身，脚本会执行 `project(bolt)` 并走一段 `find_package(LLVM REQUIRED ...)` 的逻辑去「找」一个已安装的 LLVM 来依赖；而子项目构建时，入口在 `llvm/`，BOLT 的 `CMakeLists.txt` 是被总构建 `add_subdirectory` 进来的，LLVM 的目标和头文件路径天然就在上下文里。

### 4.2 构建命令与 ninja bolt 目标：CMake 选项与目标架构

#### 4.2.1 概念说明

构建 BOLT 的命令和构建普通 LLVM 工具几乎一样，关键选项有四个：

| 选项 | 含义 | 本讲推荐值 |
| --- | --- | --- |
| `-DLLVM_ENABLE_PROJECTS="bolt"` | 把 BOLT 纳入构建（**必须**，这是上一模块说的「纳入开关」） | `bolt`（开发测试时再加 `clang;lld`） |
| `-DLLVM_TARGETS_TO_BUILD="X86;AArch64"` | 让 LLVM 编译哪些后端目标 | 至少含 BOLT 要处理的架构 |
| `-DCMAKE_BUILD_TYPE=Release` | 构建类型 | `Release`（更快） |
| `-DLLVM_ENABLE_ASSERTIONS=ON` | 启用断言 | `ON`（官方推荐，便于发现问题） |

`ninja bolt` 中的 `bolt` 是一个**元目标**——构建它会自动把 BOLT 的所有工具（见模块 4.3）都编出来，而不需要你逐个点名。

#### 4.2.2 核心流程

1. 在仓库根目录建一个 `build/` 目录。
2. 站在 `llvm/` 上执行 `cmake -G Ninja`，传入上述选项，把构建系统生成到 `build/`。
3. 进入 `build/`，执行 `ninja bolt`，编译 BOLT 全部工具。
4. 产物在 `build/bin/` 下，把它加进 `PATH` 即可使用。

目标架构有一层**约束**：BOLT 自己只支持 `AArch64;X86;RISCV` 这三种（见下方源码），而且你给 BOLT 选的目标必须是 LLVM 也编译了的目标，否则配置阶段就会 `FATAL_ERROR`。

#### 4.2.3 源码精读

官方 README 给出的标准命令（也是本讲实践要复现的）：

[bolt/README.md:51-57](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/README.md#L51-L57) —— README 推荐的 `cmake -G Ninja` 配置行与 `ninja bolt` 编译命令。

BOLT 自己对「支持哪些目标架构」有明确清单，并与 LLVM 选中的目标做交集：

[bolt/CMakeLists.txt:72-92](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/CMakeLists.txt#L72-L92) —— 这段代码做了三件事：① 列出 BOLT 全部支持的目标 `AArch64;X86;RISCV`；② 把它与 `LLVM_TARGETS_TO_BUILD` 取交集作为默认值；③ 校验用户显式传入的 `BOLT_TARGETS_TO_BUILD` 必须都落在 `LLVM_TARGETS_TO_BUILD` 里，否则报致命错误。也就是说，你不能让 BOLT 支持 X86 却不给 LLVM 编 X86 后端。

构建运行时库（`-instrument` 插桩和大页 hugify 会用到）是可选的，按宿主平台默认开启：

[bolt/CMakeLists.txt:94-113](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/CMakeLists.txt#L94-L113) —— `BOLT_ENABLE_RUNTIME` 默认只在宿主是 x86_64/arm64/aarch64/riscv64 且系统是 Linux/Darwin 且非交叉编译时为 `ON`。

运行时库通过 `ExternalProject_Add` 单独构建（因为它要按 `-ffreestanding` 等特殊约束编）：

[bolt/CMakeLists.txt:157-179](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/CMakeLists.txt#L157-L179) —— 用 `ExternalProject_Add(bolt_rt ...)` 在子目录 `runtime/` 单独编译，产物是 `libbolt_rt_instr.a` 与 `libbolt_rt_hugify.a`（运行时库的细节在 u8-l2 讲）。

最后，`bolt` 这个元目标和 `lib/`、`tools/` 子目录的挂载：

[bolt/CMakeLists.txt:189-199](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/CMakeLists.txt#L189-L199) —— `add_custom_target(bolt)` 创建元目标，随后 `add_subdirectory(lib)` 与 `add_subdirectory(tools)` 把库和工具都纳入构建，所以 `ninja bolt` 能一次编出所有可执行文件。

> 顺带一提：BOLT 内部大量 pass 并行执行，如果你要重度使用它，可以用 `jemalloc`/`tcmalloc` 加速，方法是 `LD_PRELOAD` 预加载对应库，详见 [bolt/README.md:100-112](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/README.md#L100-L112)。这属于性能调优，不影响正确性。

#### 4.2.4 代码实践

**实践目标**：按照 `GettingStarted.md` 写出一条完整可用的 `cmake + ninja` 构建命令，并说清每个选项。

**操作步骤**（以下为示例命令，实际是否能在你本机跑通取决于环境）：

```bash
# 1. 克隆仓库（已克隆可跳过）
git clone https://github.com/llvm/llvm-project.git
cd llvm-project

# 2. 建构建目录
mkdir build && cd build

# 3. 配置：站在 llvm/ 上，启用 bolt，编译 X86 与 AArch64 两个目标
cmake -G Ninja ../llvm \
  -DLLVM_ENABLE_PROJECTS="bolt" \
  -DLLVM_TARGETS_TO_BUILD="X86;AArch64" \
  -DCMAKE_BUILD_TYPE=Release \
  -DLLVM_ENABLE_ASSERTIONS=ON

# 4. 编译 BOLT 全部工具
ninja bolt

# 5. 产物在 bin/，加进 PATH
export PATH="$PWD/bin:$PATH"
llvm-bolt --version    # 应打印 BOLT revision 与已注册目标
```

逐项核对（对照本模块的源码）：

- `-DLLVM_ENABLE_PROJECTS="bolt"`：模块 4.1 解释的「纳入构建」开关。
- `-DLLVM_TARGETS_TO_BUILD="X86;AArch64"`：让 LLVM 编这两个后端。配合 [CMakeLists.txt:72-92](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/CMakeLists.txt#L72-L92)，`BOLT_TARGETS_TO_BUILD` 会自动取交集得到 `X86;AArch64`。
- `-DCMAKE_BUILD_TYPE=Release`：发布构建，编译产物更快。
- `-DLLVM_ENABLE_ASSERTIONS=ON`：启用断言，官方推荐。

**需要观察的现象 / 预期结果**：

- 配置阶段会打印 `Targeting X86 in llvm-bolt`、`Targeting AArch64 in llvm-bolt`（来自 [CMakeLists.txt:91](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/CMakeLists.txt#L91-L91)）以及 `Building BOLT runtime libraries for ...`。
- 编译完成后 `build/bin/` 下应出现 `llvm-bolt`、`perf2bolt`、`llvm-boltdiff`、`merge-fdata`、`llvm-bolt-heatmap`、`llvm-bat-dump`、`llvm-bolt-binary-analysis` 等可执行文件（清单见模块 4.3）。
- 如果实验环境里无法真的执行编译，请标注「待本地验证」——不要假装自己跑过。

**如果编译耗时太久**：可以先只验证配置阶段（只跑 `cmake` 那一步），它会立刻暴露目标架构不匹配等错误，不需要等完整编译。

#### 4.2.5 小练习与答案

**练习 1**：如果把命令里 `LLVM_TARGETS_TO_BUILD` 改成 `"X86"`（只有 X86），BOLT 还能处理 AArch64 二进制吗？

**参考答案**：不能。根据 [CMakeLists.txt:72-92](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/CMakeLists.txt#L72-L92)，`BOLT_TARGETS_TO_BUILD` 是 BOLT 支持目标与 LLVM 目标的交集。LLVM 只编 X86，BOLT 也就只有 X86，运行时遇到 AArch64 二进制无法反汇编。

**练习 2**：为什么 BOLT 默认在「交叉编译」时不构建运行时库（[CMakeLists.txt:94-102](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/CMakeLists.txt#L94-L102) 里的 `NOT CMAKE_CROSSCOMPILING`）？

**参考答案**：运行时库（`libbolt_rt_instr`/`libbolt_rt_hugify`）会被**注入进被优化的二进制**并在目标程序进程里执行，所以它的指令集必须和被优化二进制一致。交叉编译时宿主 ≠ 目标，简单按宿主编出来的运行时库装不进去，因此默认关闭，需要用户自行处理。

### 4.3 bin/ 下各工具的用途速览：从同一个仓库产出多少可执行文件

#### 4.3.1 概念说明

构建完 `ninja bolt`，`bin/` 下不是一个而是**一堆**可执行文件。它们其实分两类：

1. **多模式单二进制**：`llvm-bolt` 这一个二进制，根据你用什么名字调用它（`argv[0]`），表现为三种工具：`llvm-bolt`（优化器）、`perf2bolt`（profile 聚合器）、`llvm-boltdiff`（差异对比器）。后两个只是指向同一个二进制的**符号链接**。
2. **独立工具**：`merge-fdata`、`llvm-bolt-heatmap`、`llvm-bat-dump`、`llvm-bolt-binary-analysis` 各自是独立的可执行文件，源码各自在 `tools/` 下的一个子目录里。

> 注意命名：实际产物名带 `llvm-` / `llvm-bolt-` 前缀（如 `llvm-bolt-heatmap`、`llvm-bat-dump`、`llvm-bolt-binary-analysis`），文档里有时简写成 `heatmap`/`bat-dump`/`binary-analysis`。以 `bin/` 实际文件名为准。

#### 4.3.2 核心流程

`tools/CMakeLists.txt` 决定编译哪些工具：

1. 它 `add_subdirectory` 进 6 个工具子目录。
2. 其中 `driver/` 产出主二进制 `llvm-bolt`，并通过 `add_bolt_tool_symlink` 再造两个符号链接 `perf2bolt`、`llvm-boltdiff`。
3. 其余子目录各产出一个独立可执行文件。
4. 全部可执行文件都被加为元目标 `bolt` 的依赖，于是 `ninja bolt` 一键产出全部。

#### 4.3.3 源码精读

工具子目录清单：

[bolt/tools/CMakeLists.txt:5-10](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/tools/CMakeLists.txt#L5-L10) —— `tools/CMakeLists.txt` 列出的 6 个子目录：`driver`、`llvm-bolt-fuzzer`、`bat-dump`、`merge-fdata`、`heatmap`、`binary-analysis`。

主驱动与两个符号链接：

[bolt/tools/driver/CMakeLists.txt:14-31](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/tools/driver/CMakeLists.txt#L14-L31) —— `add_bolt_tool(llvm-bolt llvm-bolt.cpp ...)` 定义主二进制；随后 `add_bolt_tool_symlink(perf2bolt llvm-bolt)` 与 `add_bolt_tool_symlink(llvm-boltdiff llvm-bolt)` 造两个符号链接。注意它链接了 `LLVMBOLTProfile`、`LLVMBOLTRewrite`、`LLVMBOLTUtils` 三个 BOLT 库——这正是 BOLT 重写逻辑所在。

主二进制如何据名字分发模式：

[bolt/tools/driver/llvm-bolt.cpp:187-192](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/tools/driver/llvm-bolt.cpp#L187-L192) —— `main()` 取 `argv[0]` 的文件名：以 `perf2bolt` 开头就走 `perf2boltMode`，以 `llvm-boltdiff` 开头就走 `boltDiffMode`，否则走默认的 `boltMode`。这就是「同一个二进制，三种人格」的实现。

三种模式各自设置的开关，例如：

[bolt/tools/driver/llvm-bolt.cpp:105-122](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/tools/driver/llvm-bolt.cpp#L105-L122) —— `perf2boltMode` 把 `AggregateOnly=true`、`ShowDensity=true` 打开，于是同一个 `RewriteInstance::run()` 主流程会按「只聚合 profile」的方式跑。

下面这张表汇总 `ninja bolt` 产出的主要可执行文件（按用途记忆即可，细节会在后续讲义展开）：

| 可执行文件 | 是符号链接吗？ | 用途 |
| --- | --- | --- |
| `llvm-bolt` | 否（主二进制） | 优化器主体：读二进制 + profile，重排代码并输出优化后的二进制。 |
| `perf2bolt` | 是（→ llvm-bolt） | 把 `perf.data`（LBR/brstack）聚合成 BOLT 的 `fdata` 格式（u4-l2 详讲）。 |
| `llvm-boltdiff` | 是（→ llvm-bolt） | 对比两个二进制 + 各自 profile，输出性能统计差异。 |
| `merge-fdata` | 否（独立） | 合并多个 `*.fdata` profile 为一个，适配多模式负载（README「Multiple Profiles」）。 |
| `llvm-bolt-heatmap` | 否（独立） | 把 profile 密度可视化为热点地图（u4-l4 详讲）。 |
| `llvm-bat-dump` | 否（独立） | 打印 BAT（Bolt Address Translation）section 内容（u4-l3 详讲）。 |
| `llvm-bolt-binary-analysis` | 否（独立） | 只做分析、不输出二进制的模式（配套 `docs/BinaryAnalysis.md`）。 |
| `llvm-bolt-fuzzer` | 否（独立） | 用于持续测试的 fuzzer，普通用户无需关心。 |

#### 4.3.4 代码实践

**实践目标**：通过阅读 CMake 配置，**不实际编译**就能推断 `bin/` 下会出现哪些文件、哪些是符号链接。

**操作步骤**：

1. 读 [tools/CMakeLists.txt:5-10](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/CMakeLists.txt#L5-L10) 得到全部工具子目录。
2. 读各子目录的 `CMakeLists.txt` 里的 `add_bolt_tool(<名字> ...)` / `add_bolt_executable(<名字> ...)`，记下产物名。例如 [heatmap/CMakeLists.txt:8](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/tools/heatmap/CMakeLists.txt#L8-L8) 产出的是 `llvm-bolt-heatmap`，[bat-dump/CMakeLists.txt:6](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/tools/bat-dump/CMakeLists.txt#L6-L6) 产出的是 `llvm-bat-dump`。
3. 在 [driver/CMakeLists.txt:30-31](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/tools/driver/CMakeLists.txt#L30-L31) 找到两个 `add_bolt_tool_symlink`，确认 `perf2bolt`、`llvm-boltdiff` 是链接到 `llvm-bolt` 的符号链接。
4. 对照 [llvm-bolt.cpp:187-192](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/tools/driver/llvm-bolt.cpp#L187-L192) 理解「符号链接 → argv[0] → 模式分发」的闭环。

**需要观察的现象 / 预期结果**：你应该能不运行编译，仅凭源码就列出 `bin/` 下的产物清单与本模块那张表一致；并能解释为什么 `perf2bolt` 和 `llvm-boltdiff` 的二进制大小和 `llvm-bolt` 完全一样（它们就是同一个文件的不同名字）。

> 本实践为源码阅读型；如本地已编译，可用 `ls -l build/bin/ | grep '\->'` 观察到符号链接指向 `llvm-bolt`，作为额外验证（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 BOLT 要把 `perf2bolt` 做成 `llvm-bolt` 的符号链接，而不是单独编译一个二进制？

**参考答案**：因为 `perf2bolt` 的「聚合 perf.data」逻辑（`DataAggregator`）和 `llvm-bolt` 主流程共享同一套反汇编、profile 解析代码（都链接 `LLVMBOLTProfile`/`LLVMBOLTRewrite`）。做成符号链接可以避免重复编译一份几乎相同的二进制，只在 `main()` 里用 `argv[0]` 分流即可（[llvm-bolt.cpp:187-192](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/tools/driver/llvm-bolt.cpp#L187-L192)）。

**练习 2**：模块里说产物名是 `llvm-bolt-heatmap` 而不是 `heatmap`。请从 [heatmap/CMakeLists.txt:8](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/tools/heatmap/CMakeLists.txt#L8-L8) 找到证据，并判断是否存在 `heatmap → llvm-bolt-heatmap` 的符号链接。

**参考答案**：第 8 行 `add_bolt_tool(llvm-bolt-heatmap heatmap.cpp ...)` 直接以 `llvm-bolt-heatmap` 作为目标名，所以产物就是这个名字。该文件里没有 `add_bolt_tool_symlink`，所以**不存在** `heatmap` 这个符号链接；文档里写「heatmap」只是简称，调用时仍要用 `llvm-bolt-heatmap`。

**练习 3**：`merge-fdata` 链接的 LLVM 组件只有 `Support`（[merge-fdata/CMakeLists.txt:1](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/tools/merge-fdata/CMakeLists.txt#L1-L1)），而 `llvm-bolt` 链接了 BOLT 的三大库。这暗示了什么？

**参考答案**：`merge-fdata` 是一个「纯文本 profile 合并」工具，不需要反汇编、不需要重写二进制，所以它不依赖 `LLVMBOLTRewrite` 等重组件，只用到 LLVM 的基础 `Support` 库，因此编译又快又轻量。

## 5. 综合实践

把三个模块串起来，完成下面这个「从零到能调用」的小任务：

1. **写命令**：参照 [README.md:51-57](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/README.md#L51-L57) 写出完整 `cmake + ninja bolt` 命令（必须含 `X86;AArch64` 两个目标、`Release`、`ASSERTIONS=ON`），并在每行用中文注释解释该选项的作用。
2. **判身份**：定位 [CMakeLists.txt:10](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/CMakeLists.txt#L10-L10) 的 `LLVM_SUBPROJECT_TITLE`，用一两句话说明它**不是**纳入构建的开关，真正纳入构建的是 `LLVM_ENABLE_PROJECTS=bolt`（参考 [llvm/CMakeLists.txt:295-323](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/llvm/CMakeLists.txt#L295-L323)）。
3. **数工具**：不实际编译，仅读 `tools/CMakeLists.txt` 与各子目录 `CMakeLists.txt`，列出 `ninja bolt` 后 `bin/` 会出现哪些可执行文件，并标出哪些是符号链接、各链接到谁。
4. **自检**：如果你本地能编译，跑通后执行 `llvm-bolt --version` 与 `perf2bolt --help`，观察前者打印「BOLT revision + 已注册目标」，后者因 `argv[0]` 命中 `perf2bolt` 而进入聚合器帮助（待本地验证）。

完成上述 4 步，你就建立起了「配置 → 编译 → 产物 → 工具分流」的完整心智模型，为下一讲「端到端使用流程」打下基础。

## 6. 本讲小结

- BOLT 是 LLVM 的一个**子项目**，必须搭着 LLVM 一起编，配置入口在 `llvm/`，纳入构建的开关是 `-DLLVM_ENABLE_PROJECTS="bolt"`。
- `bolt/CMakeLists.txt` 顶部的 `set(LLVM_SUBPROJECT_TITLE "BOLT")` 只是**登记身份/用于 IDE 分组**，并不是纳入构建的开关——这是本讲最容易踩的认知坑。
- 标准构建命令是 `cmake -G Ninja ../llvm -DLLVM_ENABLE_PROJECTS="bolt" -DLLVM_TARGETS_TO_BUILD="X86;AArch64" -DCMAKE_BUILD_TYPE=Release -DLLVM_ENABLE_ASSERTIONS=ON` 加 `ninja bolt`。
- BOLT 支持的目标架构只有 `AArch64;X86;RISCV`，且必须被 `LLVM_TARGETS_TO_BUILD` 包含，`BOLT_TARGETS_TO_BUILD` 是两者交集。
- `ninja bolt` 会一次产出多个可执行文件：`llvm-bolt` 是主二进制，`perf2bolt`/`llvm-boltdiff` 是它的符号链接（靠 `argv[0]` 分流三种模式）；`merge-fdata`、`llvm-bolt-heatmap`、`llvm-bat-dump`、`llvm-bolt-binary-analysis` 是各自独立的工具。
- 重度使用 BOLT 时可用 `jemalloc`/`tcmalloc` 经 `LD_PRELOAD` 加速其并行 pass。

## 7. 下一步学习建议

- 下一讲 [u1-l3 端到端使用流程](u1-l3-end-to-end-workflow.md) 会教你把这堆工具真正「串」起来：从 `-Wl,-q` 链接、`perf` 采集、`perf2bolt` 转换，到 `llvm-bolt -reorder-blocks=ext-tsp ...` 优化，并对比 `-dyno-stats`。建议先把本讲的构建跑通（或至少配置通过），再进入下一讲。
- 如果你对「BOLT 的源码目录如何划分」更感兴趣，可以跳到 [u1-l4 代码目录结构与程序入口](u1-l4-directory-and-entry.md)，它会讲 `lib/Core`、`lib/Passes`、`lib/Profile`、`lib/Rewrite`、`lib/Target` 各自的职责。
- 想深入了解运行时库（`libbolt_rt_instr`/`libbolt_rt_hugify`）为何按 `-ffreestanding` 单独构建，留到 [u8-l2 运行时库与插桩](u8-l2-runtime-and-instrumentation.md)。
