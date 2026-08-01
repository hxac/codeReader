# 构建系统：CMake 与编译流程

## 1. 本讲目标

学完本讲后，你应该能够：

- 看懂 LLVM 用 CMake 配置时的「配置（configure）→ 构建（build）→ 安装（install）」三步流程，并能写出一条最小化的配置命令。
- 理解 `CMAKE_BUILD_TYPE`、`LLVM_ENABLE_PROJECTS`、`LLVM_TARGETS_TO_BUILD`、`LLVM_ENABLE_ASSERTIONS` 这四个最常用变量的作用，以及它们在源码里是如何被读取和处理的。
- 明白为什么 LLVM **必须**做「源码目录之外」的构建（out-of-source build），以及 Ninja 作为推荐生成器带来的好处。
- 独立完成（或至少写出）一次只构建 LLVM 核心与 X86 目标的最小构建。

本讲不要求你已经编译过 LLVM，但会承接 [u1-l1](u1-l1-project-overview.md)（monorepo 与子项目划分）和 [u1-l2](u1-l2-directory-structure.md)（目录结构）的认知。前置讲义已经讲过「monorepo 里 `llvm/clang/mlir/...` 平级同居一仓库、目录名即项目名」，本讲要回答的是：**这些项目是如何被 CMake 选中、编排、并最终被编译出来的**。

## 2. 前置知识

在进入源码之前，先用大白话讲清几个概念。

**CMake 是什么？** CMake 不是编译器，而是一个「构建系统生成器（build-system generator）」。它读入项目根目录的 `CMakeLists.txt`，根据你指定的平台、编译器、选项，**生成**一份具体的构建脚本（比如 Ninja 的 `build.ninja`、或者 Make 的 `Makefile`），然后再由 Ninja/Make 去真正调用编译器。所以 CMake 的工作分两阶段：

1. **配置（configure）**：运行 `cmake -S <源码> -B <构建目录> -D<选项>=<值> ...`，CMake 探测系统、读取选项、生成构建脚本。
2. **构建（build）**：运行 `cmake --build <构建目录>`（或直接 `ninja`、`make`），按脚本调用编译器把源码编译成二进制。

**生成器（generator）是什么？** 配置时用 `-G` 指定生成哪一种构建脚本。LLVM 官方文档明确推荐 **Ninja**，因为它并行调度好、增量构建快、特别适合处理 LLVM 这种上万文件的巨型工程。后文会看到 Ninja 还配合 `LLVM_PARALLEL_LINK_JOBS` 这类选项使用。

**配置变量（cache variable）是什么？** 用 `-D` 传给 CMake 的变量会被写进构建目录里的 `CMakeCache.txt`，下次再配置时会记住上次的值。本讲重点讲的四个变量都是 cache variable。

**断言（assertion）与 NDEBUG。** C/C++ 的 `assert(...)` 宏在定义了宏 `NDEBUG` 后会变成空操作。CMake 的 `Release`/`RelWithDebInfo` 等非 Debug 构建类型默认会定义 `NDEBUG`，从而关闭断言；而 LLVM 想在 Release 下也能保留断言，于是用一个「反向 undefine」的技巧，下文会讲。

## 3. 本讲源码地图

本讲主要围绕构建系统的「中枢」文件展开：

| 文件 | 作用 |
| --- | --- |
| [llvm/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/CMakeLists.txt) | LLVM 的顶层 CMake 配置文件，是本讲的绝对主角：定义所有构建选项、校验子项目、枚举目标后端、编排子目录编译顺序。 |
| [llvm/cmake/modules/HandleLLVMOptions.cmake](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/cmake/modules/HandleLLVMOptions.cmake) | 把选项「翻译」成具体编译/链接标志的地方，例如断言如何变成 `-UNDEBUG`。 |
| [llvm/cmake/config-ix.cmake](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/cmake/config-ix.cmake) | 平台探测脚本，负责检测「本机原生架构」并把 `host`/`Native` 关键字替换成真实目标。 |
| [llvm/runtimes/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/runtimes/CMakeLists.txt) | 运行时（compiler-rt/libcxx 等）的二级构建入口，演示「自举（bootstrap）」式分层构建。 |
| [llvm/tools/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/tools/CMakeLists.txt) | 工具子目录的递归入口，对应 [u1-l2](u1-l2-directory-structure.md) 讲过的「薄壳工具」。 |
| [llvm/docs/CMake.md](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/docs/CMake.md) | 官方 CMake 变量说明文档，是查询选项含义的权威参考。 |

## 4. 核心概念与源码讲解

本讲把构建系统拆成 5 个最小模块：先看 CMake 的整体配置流程与构建目录约定（4.1），再依次讲四个最常用变量——子项目选择（4.2）、目标后端选择（4.3）、断言与调试构建（4.4），最后落到「用 Ninja 执行构建」（4.5）。

### 4.1 CMake 配置流程与构建目录约定

#### 4.1.1 概念说明

LLVM 的顶层入口是 [llvm/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/CMakeLists.txt)，注意它**位于 `llvm/` 子目录内，而不是 monorepo 根目录**。这是因为 monorepo 里 LLVM 是「中心项目」，其他子项目（clang/lld/...）通过相对路径被它「挂载」进来——这是 [u1-l1](u1-l1-project-overview.md) 讲过的「目录名即项目名」的直接体现。

CMake 处理这份文件有一套固定顺序，理解这个顺序，你就能判断「某个选项必须在哪一步之前生效」：

1. 声明最低 CMake 版本、加载公共 CMake 工具模块、读出版本号；
2. `project(LLVM ...)`：正式确立工程名、语言（C/C++/ASM）、版本；
3. 定义并**校验**所有 cache 变量（构建类型、子项目、目标、断言……）；
4. `include(config-ix)` 做平台探测；
5. `include(HandleLLVMOptions)` 把选项翻译成编译标志；
6. 一连串 `add_subdirectory(...)` 把各子目录纳入编译，并决定先后顺序。

#### 4.1.2 核心流程

```
cmake -S llvm -B build -G Ninja -DCMAKE_BUILD_TYPE=Release -DLLVM_ENABLE_PROJECTS=clang ...
        │            │            │
        │            │            └─ 生成器：生成 build/build.ninja
        │            └─ 构建目录（out-of-source，存放 CMakeCache.txt 与产物）
        └─ 源码目录（指向 llvm/CMakeLists.txt）
                    │
                    ▼
        读 llvm/CMakeLists.txt → 校验选项 → include(config-ix) → include(HandleLLVMOptions)
                    │
                    ▼
        add_subdirectory(lib) → add_subdirectory(tools) → ... → 写出 build.ninja
```

一个关键约定：**LLVM 不允许「在源码目录里直接构建」（in-source build）**。源码树一旦混入 `CMakeCache.txt` 和 `CMakeFiles/`，会干扰头文件搜索，所以配置脚本会主动拒绝这种用法（详见 4.1.3）。

#### 4.1.3 源码精读

**最低版本与工程声明。** 文件开头声明所需 CMake 版本，并提前警告未来 LLVM 24 将要求 CMake ≥ 3.31：

[llvm/CMakeLists.txt:2-9](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/CMakeLists.txt#L2-L9) —— 设定 `cmake_minimum_required(VERSION 3.20.0)`，并对旧版本给出升级提示。

随后用 `project()` 正式确立工程，声明语言为 `C CXX ASM`：

[llvm/CMakeLists.txt:77-79](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/CMakeLists.txt#L77-L79) —— 这一行之后，CMake 才真正「认识」编译器，后续的 `option(...)`、探测才能进行。

**强制 out-of-source 构建。** 当源码目录与构建目录相同时，配置直接报致命错误：

[llvm/CMakeLists.txt:509-515](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/CMakeLists.txt#L509-L515) —— 检查 `CMAKE_CURRENT_SOURCE_DIR STREQUAL CMAKE_CURRENT_BINARY_DIR`，若相同则 `FATAL_ERROR`，并提示用户删除误生成的 `CMakeCache.txt` 与 `CMakeFiles/`。这就是为什么我们必须 `-B build` 单独指定一个构建目录。

**子目录编排顺序。** 配置的收尾阶段是一连串 `add_subdirectory`，决定了哪些子目录被编译、以及先后依赖：

[llvm/CMakeLists.txt:1377-1441](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/CMakeLists.txt#L1377-L1441) —— 这里能读出整棵编译树的骨架：

- 先 `lib/Demangle`、`lib/Support`、`lib/TableGen`（最底层依赖，必须最先）；
- 再 `utils/TableGen`（表生成工具，后续目标的 `.td` 描述依赖它）；
- 然后 `include`、`lib`（核心库）；
- 接着受 `LLVM_INCLUDE_TOOLS` 控制的 `tools`，以及受 `LLVM_INCLUDE_RUNTIMES` 控制的 `runtimes`、受 `LLVM_INCLUDE_EXAMPLES` 控制的 `examples`。

每个 `add_subdirectory` 前都有形如 `if(LLVM_INCLUDE_TOOLS)` 的开关，这正是 `LLVM_INCLUDE_*` 系列选项能「裁剪」构建范围的落点。

#### 4.1.4 代码实践

**实践目标：** 亲手跑一次「配置」阶段，观察 CMake 的输出与产物，理解配置与构建是两个独立步骤。

**操作步骤（待本地验证，本机不一定具备完整构建环境）：**

1. 在仓库根目录执行（仅配置，不构建）：
   ```bash
   cmake -S llvm -B build-min -G Ninja \
         -DCMAKE_BUILD_TYPE=Release \
         -DLLVM_TARGETS_TO_BUILD=X86
   ```
2. 配置完成后，列出 `build-min/` 目录，确认生成了 `CMakeCache.txt` 和 `build.ninja`。
3. 用 `grep` 在 `CMakeCache.txt` 里查你刚才传的变量，例如 `grep LLVM_TARGETS_TO_BUILD build-min/CMakeCache.txt`，确认它被「记住」了。

**需要观察的现象：** CMake 会打印大量 `-- ...` 状态行，包括检测到的编译器、原生目标架构、被启用的子项目。`CMakeCache.txt` 会把所有 cache 变量（含默认值）固化下来。

**预期结果：** `build-min/build.ninja` 存在；`CMakeCache.txt` 中 `LLVM_TARGETS_TO_BUILD` 的值为 `X86`。若提示找不到 Ninja，则安装 `ninja-build` 或改用 `-G "Unix Makefiles"`。

#### 4.1.5 小练习与答案

**练习 1：** 为什么 LLVM 不允许 in-source build？
**参考答案：** in-source 构建会在源码树里留下 `CMakeCache.txt`、`CMakeFiles/` 等生成文件，它们会被头文件搜索误当成源码版本，破坏构建正确性；同时也不利于用同一份源码挂载多个不同配置的构建目录（参见 [llvm/CMakeLists.txt:509-515](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/CMakeLists.txt#L509-L515) 的检查与注释）。

**练习 2：** 假如你想同时维护「Debug 版」和「Release 版」两个构建产物，应该怎么做？
**参考答案：** 用同一份源码、两个不同的构建目录，例如 `cmake -S llvm -B build-debug -DCMAKE_BUILD_TYPE=Debug ...` 与 `cmake -S llvm -B build-release -DCMAKE_BUILD_TYPE=Release ...`。这正是 [llvm/CMakeLists.txt:133-137](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/CMakeLists.txt#L133-L137) 注释里说的「同一份源码挂载多个构建目录」的能力。

---

### 4.2 选择构建哪些子项目：LLVM_ENABLE_PROJECTS 与 LLVM_ENABLE_RUNTIMES

#### 4.2.1 概念说明

monorepo 里项目众多，你通常只想构建其中一部分。CMake 用两个变量来回答「构建哪些子项目」，二者分工不同（这是 [u1-l1](u1-l1-project-overview.md) 已建立认知的细化）：

- **`LLVM_ENABLE_PROJECTS`**：选择**构建期组件**，即那些和 LLVM 一起编译、需要 LLVM 作为构建工具的项目，如 `clang`、`lld`、`lldb`、`mlir`、`flang`。
- **`LLVM_ENABLE_RUNTIMES`**：选择**运行时库**，即那些要用「已经编好的 clang」去编译、目标运行在用户程序运行时的库，如 `compiler-rt`、`libcxx`、`libcxxabi`、`libunwind`、`openmp`。

这种区分是真实存在的：源码里把 `compiler-rt`/`libc`/`libclc` 等显式排除在 `LLVM_ALL_PROJECTS` 之外，并给出「请改用 `LLVM_ENABLE_RUNTIMES`」的告警。

#### 4.2.2 核心流程

```
LLVM_ENABLE_PROJECTS="clang;lld"  ──┐
                                     ├─► 校验是否在 LLVM_KNOWN_PROJECTS 内
LLVM_ENABLE_RUNTIMES="compiler-rt" ─┘    （不在则 FATAL_ERROR）
        │
        ▼
对每个 project：拼出源码目录 ../<项目名>
（CMAKE_CURRENT_SOURCE_DIR/../${proj}）
        │
        ▼
设置 LLVM_EXTERNAL_<PROJ>_SOURCE_DIR 与 LLVM_TOOL_<PROJ>_BUILD=ON
        │
        ▼
add_subdirectory(tools) / add_subdirectory(runtimes) 时据此递归进入对应子目录
```

关键点：项目路径不是硬编码的绝对路径，而是相对于 `llvm/` 的 `../<项目名>`，这正好对应 monorepo「平级同居一仓库」的布局。

#### 4.2.3 源码精读

**项目清单与「all」语义。** 源码先定义「常规项目」与「额外项目」两个集合，合并成权威清单 `LLVM_KNOWN_PROJECTS`：

[llvm/CMakeLists.txt:138-148](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/CMakeLists.txt#L138-L148) —— `LLVM_ALL_PROJECTS` 含 `bolt;clang;...;mlir;polly`；注释明确指出 `libc`、`compiler-rt` 不在「all」里，因为更推荐用 `LLVM_ENABLE_RUNTIMES`；`flang` 也暂未纳入「all」。`LLVM_ENABLE_PROJECTS` 接受这些名字或字面量 `"all"`。

**「all」展开与拼写校验。** 传 `"all"` 会被展开成 `LLVM_ALL_PROJECTS`；任何拼写不在清单里的项目名都会致命报错，并贴心地提示「你是不是想用 runtimes」：

[llvm/CMakeLists.txt:150-167](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/CMakeLists.txt#L150-L167) —— 这里能看出选项校验是「fail-fast」的，写错项目名立刻终止配置，避免后面一堆诡异错误。

**运行时的清单与校验。** 运行时有独立的「默认集合」与「支持集合」：

[llvm/CMakeLists.txt:173-184](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/CMakeLists.txt#L173-L184) —— `LLVM_DEFAULT_RUNTIMES` 给出 `all` 的含义，`LLVM_SUPPORTED_RUNTIMES` 给出合法取值；不在支持列表里的运行时名同样报错。

**挂载源码目录与开关。** 真正把「项目名」变成「目录路径」的逻辑在这段：

[llvm/CMakeLists.txt:290-306](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/CMakeLists.txt#L290-L306) —— 对每个启用的 project，拼出 `PROJ_DIR=${CMAKE_CURRENT_SOURCE_DIR}/../${proj}`（即 `llvm/../clang`），并检查目录是否存在；若存在则写入 `LLVM_EXTERNAL_<UPPER>_SOURCE_DIR`。这正是把 [u1-l1](u1-l1-project-overview.md) 的「相对路径 `../<项目名>` 写死进构建逻辑」落到代码里。

随后用 `LLVM_TOOL_<UPPER>_BUILD` 这个布尔变量作为「是否编译该子项目」的总开关：

[llvm/CMakeLists.txt:319-323](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/CMakeLists.txt#L319-L323) —— 强制令 `LLVM_TOOL_<UPPER>_BUILD` 与 `LLVM_ENABLE_PROJECTS` 保持一致，注释解释这是为了把「该构建谁」的唯一真相源统一到 `LLVM_ENABLE_PROJECTS`，避免用户另外手动设 `LLVM_TOOL_*_BUILD` 造成矛盾。

**运行时的分层构建。** 运行时不直接 `add_subdirectory`，而是通过 `llvm/runtimes/CMakeLists.txt` 以「外部项目」方式用刚编好的工具链去编：

[llvm/runtimes/CMakeLists.txt:1-22](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/runtimes/CMakeLists.txt#L1-L22) —— 注意它的源码目录拼法是 `../../${proj}`（从 `llvm/runtimes/` 往上两级回到 monorepo 根），与 projects 的 `../${proj}` 不同，因为二者起点目录不同。这种「用编好的 clang 去编运行时」就是自举（bootstrap）。

#### 4.2.4 代码实践

**实践目标：** 体会 `LLVM_ENABLE_PROJECTS` 与 `LLVM_ENABLE_RUNTIMES` 的区别，并验证拼写校验。

**操作步骤（待本地验证）：**

1. 写一个故意拼错的配置命令，观察报错：
   ```bash
   cmake -S llvm -B build-x -DLLVM_ENABLE_PROJECTS=clangg 2>&1 | tail -5
   ```
2. 改成正确写法并配置带 clang 的最小构建：
   ```bash
   cmake -S llvm -B build-x -G Ninja \
         -DCMAKE_BUILD_TYPE=Release \
         -DLLVM_ENABLE_PROJECTS=clang \
         -DLLVM_TARGETS_TO_BUILD=X86
   ```
3. 在配置输出里 grep `project is enabled`，确认 `clang project is enabled`。

**需要观察的现象：** 步骤 1 应输出形如 `clangg isn't a known project: ...` 的致命错误并终止；步骤 2 配置成功后，`build-x/` 下应出现 `bin/clang` 的构建目标（可用 `ninja -C build-x -t targets all | grep clang` 查看是否存在 `clang` 目标）。

**预期结果：** 拼写错误会被立即拦截；正确配置后 clang 被纳入构建图。

#### 4.2.5 小练习与答案

**练习 1：** 为什么 `compiler-rt` 推荐用 `LLVM_ENABLE_RUNTIMES` 而不是 `LLVM_ENABLE_PROJECTS`？
**参考答案：** `compiler-rt` 是运行时库，需要用「已经构建好的 clang」来编译（自举），而不是和 LLVM 一起用宿主编译器编。源码里若用 `LLVM_ENABLE_PROJECTS=compiler-rt` 会给出 deprecation 警告，建议改用 `LLVM_ENABLE_RUNTIMES=compiler-rt`（见 [llvm/CMakeLists.txt:218-223](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/CMakeLists.txt#L218-L223)）。

**练习 2：** `LLVM_ENABLE_PROJECTS=all` 实际会启用哪些项目？
**参考答案：** 它会被展开为 `LLVM_ALL_PROJECTS`，即 `bolt;clang;clang-tools-extra;cross-project-tests;lld;lldb;mlir;polly`（见 [llvm/CMakeLists.txt:138](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/CMakeLists.txt#L138) 与 [llvm/CMakeLists.txt:150-152](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/CMakeLists.txt#L150-L152)）。注意它**不**含 `flang`、`compiler-rt`、`libc`。

---

### 4.3 选择目标后端：LLVM_TARGETS_TO_BUILD

#### 4.3.1 概念说明

「目标（target）」在 LLVM 里指**它能为哪种 CPU/架构生成机器码**，例如 `X86`、`AArch64`、`RISCV`、`AMDGPU`、`WebAssembly`。每多支持一个架构，就要多编译一整套「指令选择、寄存器定义、汇编器、反汇编器」代码，体积和编译时间都不小。所以 `LLVM_TARGETS_TO_BUILD` 让你按需裁剪——只构建你关心的架构。

#### 4.3.2 核心流程

```
LLVM_TARGETS_TO_BUILD="X86"   ──（默认 "all"）──► 展开为 LLVM_ALL_TARGETS 全部架构
        │
        ▼
对每个目标 t：在 lib/Target/${t} 下查找 AsmPrinter/AsmParser/Disassembler/MCA...
        │
        ▼
拼接成 LLVM_ENUM_TARGETS、LLVM_ENUM_ASM_PRINTERS 等字符串
        │
        ▼
configure_file 把它们写进 include/llvm/Config/Targets.def
（这个 .def 决定运行时注册哪些目标）
```

此外，`config-ix.cmake` 会把关键字 `host` 或 `Native` 替换成「当前机器的架构」，这样写 `-DLLVM_TARGETS_TO_BUILD=host` 就能保证本机能跑（例如 `lli` 的 JIT）。

#### 4.3.3 源码精读

**目标清单。** 源码用两个列表给出全部「正式目标」和「实验目标」：

[llvm/CMakeLists.txt:571-601](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/CMakeLists.txt#L571-L601) —— `LLVM_ALL_TARGETS` 列出 AArch64、AMDGPU、ARM、X86、RISCV 等二十余个架构；`LLVM_ALL_EXPERIMENTAL_TARGETS` 列出 ARC、CSKY、DirectX、M68k、Xtensa 等实验性架构（实验目标必须通过 `LLVM_EXPERIMENTAL_TARGETS_TO_BUILD` 单独传，混进 `LLVM_TARGETS_TO_BUILD` 会被拦截）。

**默认值与「all」展开。**

[llvm/CMakeLists.txt:606-611](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/CMakeLists.txt#L606-L611) —— 默认 `LLVM_TARGETS_TO_BUILD` 为 `"all"`；随后 [llvm/CMakeLists.txt:709-711](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/CMakeLists.txt#L709-L711) 把 `"all"` 展开成全部正式目标。这就是「不指定就构建所有架构」的原因，也是首次构建很慢的一个来源。

**目标枚举与 `.def` 生成。** 这是构建系统最「魔法」的一段——它扫描每个目标目录，决定该把哪些架构注册到运行时：

[llvm/CMakeLists.txt:1144-1166](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/CMakeLists.txt#L1144-L1166) —— 对每个被选中的目标 `t`，拼接 `LLVM_ENUM_TARGETS += LLVM_TARGET(${t})`，并设 `LLVM_HAS_${T}_TARGET=1`；还会检查 `lib/Target/${t}/*AsmPrinter.cpp`、`AsmParser/CMakeLists.txt`、`Disassembler/CMakeLists.txt` 是否存在，从而决定是否注册对应的汇编打印器、汇编解析器、反汇编器。

这些字符串随后被 `configure_file` 注入到配置头：

[llvm/CMakeLists.txt:1196-1219](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/CMakeLists.txt#L1196-L1219) —— 把 `Targets.def.in` 等模板处理成 `Targets.def`。运行时代码通过 `#include "llvm/Config/Targets.def"` 来遍历「本次构建支持哪些目标」，于是目标的注册被构建选项动态决定。

**「host」关键字的替换。**

[llvm/cmake/config-ix.cmake:593-611](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/cmake/config-ix.cmake#L593-L611) —— 若 `LLVM_TARGETS_TO_BUILD` 含 `host` 或 `Native`，就替换成 `LLVM_NATIVE_ARCH`（本机架构），并据此设 `LLVM_NATIVE_TARGET` 等。注释还提醒：若不包含本机原生目标，`lli` 将无法 JIT。

#### 4.3.4 代码实践

**实践目标：** 直观感受「只构建 X86」与「构建全部目标」在编译目标数量上的差异。

**操作步骤（待本地验证）：**

1. 配置一个只含 X86 的最小构建：
   ```bash
   cmake -S llvm -B build-x86only -G Ninja -DLLVM_TARGETS_TO_BUILD=X86 -DCMAKE_BUILD_TYPE=Release
   ```
2. 查看生成的 `Targets.def` 内容：
   ```bash
   cat build-x86only/include/llvm/Config/Targets.def
   ```
3. 再配置一个 `LLVM_TARGETS_TO_BUILD=host` 的构建，重复步骤 2，对比注册的目标数量。

**需要观察的现象：** 步骤 2 的 `.def` 里应只有一行 `LLVM_TARGET(X86)`（外加可能的注释）；步骤 3 在 x86 机器上应同样只注册 X86（因为 `host` 被替换成 X86），但在 AArch64 机器上会注册 AArch64。

**预期结果：** `Targets.def` 的内容随 `LLVM_TARGETS_TO_BUILD` 精确变化，验证「目标注册由构建选项驱动」这一结论。

#### 4.3.5 小练习与答案

**练习 1：** 如果把实验目标 `M68k` 直接写进 `LLVM_TARGETS_TO_BUILD=M68k`，会发生什么？
**参考答案：** 配置会 `FATAL_ERROR`，提示该目标是实验性的，必须改用 `LLVM_EXPERIMENTAL_TARGETS_TO_BUILD=M68k`（见 [llvm/CMakeLists.txt:1151-1166](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/CMakeLists.txt#L1151-L1166)）。

**练习 2：** 为什么 `lli`（LLVM 解释器/JIT）依赖「本机原生目标被构建」？
**参考答案：** JIT 要把 IR 即时编译成「当前 CPU 能执行」的机器码，必须注册本机架构对应的初始化函数 `LLVM_NATIVE_TARGET`。若 `LLVM_TARGETS_TO_BUILD` 不含本机架构，[llvm/cmake/config-ix.cmake:603-605](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/cmake/config-ix.cmake#L603-L605) 会打印 `lli will not JIT code` 的告警。

---

### 4.4 断言与调试构建：CMAKE_BUILD_TYPE 与 LLVM_ENABLE_ASSERTIONS

#### 4.4.1 概念说明

构建类型（`CMAKE_BUILD_TYPE`）决定优化等级和是否带调试信息，主要有四种：

| 取值 | 含义 |
| --- | --- |
| `Release` | 高优化（`-O3`），无调试信息，体积小速度快。LLVM 的默认值。 |
| `Debug` | 无优化（`-O0`），带完整调试信息，编译快、运行慢，适合开发调试。 |
| `RelWithDebInfo` | 带优化（`-O2` 或 `-O3`）且带调试信息，兼顾性能与可调试性。 |
| `MinSizeRel` | 优化体积最小的发布构建。 |

`LLVM_ENABLE_ASSERTIONS` 控制 LLVM 源码里大量 `assert(...)` 是否生效。它的默认值很巧妙：**仅当 `CMAKE_BUILD_TYPE` 为 `Debug` 时默认开启，其余默认关闭**。但你也可以在 Release 下显式打开断言——这是 LLVM 开发者常用的配置，因为很多内部不变式（invariant）只在断言开启时才检查。

#### 4.4.2 核心流程

```
CMAKE_BUILD_TYPE 未指定？
   └─► 强制设为 Release（并打印告警）  ← 见 CMakeLists.txt:112-117

LLVM_ENABLE_ASSERTIONS 未显式指定？
   └─► Debug → 默认 ON；非 Debug → 默认 OFF  ← 见 CMakeLists.txt:749-753

进入 HandleLLVMOptions.cmake：
LLVM_ENABLE_ASSERTIONS 为真？
   ├─► 定义 _DEBUG、_GLIBCXX_ASSERTIONS
   ├─► 非 Debug 构建时：加 -UNDEBUG（抵消 Release 默认的 NDEBUG）
   └─► 启用 libc++ extensive hardening
```

关键技巧：CMake 的非 Debug 构建会自动给编译器加 `-DNDEBUG`，从而关掉标准 `assert`。LLVM 想「Release 也保留断言」，于是追加 `-UNDEBUG` 来「取消定义」`NDEBUG`，把断言重新打开。这种「先 NDEBUG 再 UNDEBUG」的拉锯，是理解 LLVM 断言行为的钥匙。

#### 4.4.3 源码精读

**构建类型的默认值与校验。**

[llvm/CMakeLists.txt:112-117](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/CMakeLists.txt#L112-L117) —— 若用户没指定 `CMAKE_BUILD_TYPE`，强制默认为 `Release` 并 `FORCE` 写入缓存，同时打印告警指引用户去文档查阅。

随后只允许几种合法取值，否则报错：

[llvm/CMakeLists.txt:517-528](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/CMakeLists.txt#L517-L528) —— `ALLOWED_BUILD_TYPES` 由 `DEBUG RELEASE RELWITHDEBINFO MINSIZEREL` 加上自定义的 `LLVM_ADDITIONAL_BUILD_TYPES` 构成，传了不在白名单里的值会 `FATAL_ERROR`。

**断言默认值随构建类型而变。**

[llvm/CMakeLists.txt:749-753](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/CMakeLists.txt#L749-L753) —— 用 `if(NOT uppercase_CMAKE_BUILD_TYPE STREQUAL "DEBUG")` 决定 `option(LLVM_ENABLE_ASSERTIONS ...)` 的默认布尔值：Debug 下默认 ON，其余默认 OFF。这就是「断言默认值依赖构建类型」的实现。

**断言如何落到编译标志。**

[llvm/cmake/modules/HandleLLVMOptions.cmake:113-155](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/cmake/modules/HandleLLVMOptions.cmake#L113-L155) —— 这是断言的核心处理：

- 定义 `_DEBUG`（非 Windows）、`_GLIBCXX_ASSERTIONS`（让 libstdc++ 也开断言）；
- **非 Debug 构建时追加 `-UNDEBUG`**（对 MSVC 则额外从各 `*_FLAGS_RELEASE` 里用正则抹掉 `/D NDEBUG`），从而在 Release 下重新启用 `assert`；
- 谨慎地开启 libc++ 的 extensive hardening 模式（`LIBCXX_HARDENING_MODE=extensive`）。

此外，更激进的 `LLVM_ENABLE_EXPENSIVE_CHECKS` 会强制要求断言已开：

[llvm/cmake/modules/HandleLLVMOptions.cmake:157-164](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/cmake/modules/HandleLLVMOptions.cmake#L157-L164) —— 若未开断言就开 expensive checks，直接 `FATAL_ERROR`，并定义 `EXPENSIVE_CHECKS` 宏。

#### 4.4.4 代码实践

**实践目标：** 验证「Release + 断言」组合会把 `-UNDEBUG` 加进编译命令，从而直观看到断言被保留。

**操作步骤（待本地验证）：**

1. 配置一个带断言的 Release 构建：
   ```bash
   cmake -S llvm -B build-assert -G Ninja \
         -DCMAKE_BUILD_TYPE=Release -DLLVM_ENABLE_ASSERTIONS=ON \
         -DLLVM_TARGETS_TO_BUILD=X86
   ```
2. 利用 LLVM 默认导出的编译数据库（`CMAKE_EXPORT_COMPILE_COMMANDS` 默认为 ON，见 [llvm/CMakeLists.txt:418](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/CMakeLists.txt#L418)）查看某个源文件的真实编译命令：
   ```bash
   grep -m1 -o -- '-O[0-9]\|-DNDEBUG\|-UNDEBUG' build-assert/compile_commands.json | sort -u
   ```
3. 对比一个 `-DLLVM_ENABLE_ASSERTIONS=OFF` 的构建，看 `-UNDEBUG` 是否消失。

**需要观察的现象：** 开启断言的 Release 构建里，应同时出现 `-O3`（或 `-O2`）和 `-UNDEBUG`；关闭断言时只有 `-O3 -DNDEBUG`，没有 `-UNDEBUG`。

**预期结果：** 直观印证「LLVM 用 `-UNDEBUG` 在优化构建里保留断言」的机制。

#### 4.4.5 小练习与答案

**练习 1：** 为什么 LLVM 在 Release 下默认关闭断言，却又提供 `LLVM_ENABLE_ASSERTIONS=ON` 的常见用法？
**参考答案：** Release 默认关断言是为了追求最高性能与最小体积（断言有运行时开销）；但 LLVM 内部有大量用于捕获逻辑错误的不变式检查，开发者和发行版常常愿意付一点性能代价换取更强的错误检测能力，所以官方也鼓励「Release + 断言」的配置（见 [llvm/docs/CMake.md:489-493](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/docs/CMake.md#L489-L493)）。

**练习 2：** 如果不传 `CMAKE_BUILD_TYPE`，最终构建会用哪个优化等级？
**参考答案：** `Release`（即 `-O3`），因为 [llvm/CMakeLists.txt:112-117](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/CMakeLists.txt#L112-L117) 会把未指定的 `CMAKE_BUILD_TYPE` 强制默认为 `Release` 并打印告警。

---

### 4.5 用 Ninja 执行构建：生成器、构建命令与增量编译

#### 4.5.1 概念说明

配置（`cmake -S ... -B ...`）只是生成了构建脚本，真正的「编译」要靠生成器对应的工具来完成。LLVM 强烈推荐 **Ninja**，原因有三：

1. **并行调度优秀**：Ninja 默认按 CPU 核数并行，且能精确分析文件依赖，几乎不浪费空闲。
2. **增量构建快**：改一个文件，Ninja 只重编受影响的部分，且依赖分析精准到「不会漏编也不会乱编」。
3. **与 LLVM 选项配合好**：如 `LLVM_PARALLEL_COMPILE_JOBS` / `LLVM_PARALLEL_LINK_JOBS` 可分别限制并发编译/链接数（链接极耗内存，需要单独限制）。

#### 4.5.2 核心流程

```
配置阶段：cmake -S llvm -B build -G Ninja -D...   →  生成 build/build.ninja
构建阶段：cmake --build build                      →  等价于在 build/ 里执行 ninja
              │
              ├─ 默认构建 all 目标
              ├─ 支持增量：只重编改动及其依赖
              └─ -j N 限制并行度
安装阶段：cmake --build build --target install     →  把产物拷到 CMAKE_INSTALL_PREFIX
```

`cmake --build` 是「与生成器无关」的统一入口，无论你用 Ninja 还是 Make，命令都一样；它内部会调用对应的工具。也可以直接 `cd build && ninja`。

#### 4.5.3 源码精读

**C++ 标准与编译标志的源头。** 构建时每个文件按什么标准编译，根源于顶层声明的 C++17 要求：

[llvm/CMakeLists.txt:92-110](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/CMakeLists.txt#L92-L110) —— 设 `LLVM_REQUIRED_CXX_STANDARD=17`，并强制 `CMAKE_CXX_STANDARD=17`、`CMAKE_CXX_EXTENSIONS=NO`；若用户老缓存的 `CMAKE_CXX_STANDARD` 低于 17 还会被重置。这说明构建系统对工具链有硬性要求。

**导出编译数据库。** 这个细节对阅读源码非常有用：LLVM 默认导出 `compile_commands.json`：

[llvm/CMakeLists.txt:416-418](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/CMakeLists.txt#L416-L418) —— `set(CMAKE_EXPORT_COMPILE_COMMANDS ON)`。配合 4.4.4 的实践，你可以直接查看任意源文件的真实编译命令（含所有 `-I`、`-D`、`-O` 标志），这是阅读 LLVM 源码时「搞清楚某个宏从哪来」的利器。

**工具子目录如何被纳入。** Ninja 构建图里的每个工具目标，来自 `tools/` 的递归：

[llvm/tools/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/tools/CMakeLists.txt) —— 它通过 `create_llvm_tool_options()` 为每个子目录生成开关，并依据 `LLVM_TOOL_<NAME>_BUILD`（即 4.2 讲到的、由 `LLVM_ENABLE_PROJECTS` 推动的变量）决定是否递归进入。这把「选项 → 是否编译某工具」的链路彻底打通。

**官方文档的命令样例。** 推荐的三步命令在官方文档里有明确写法：

[llvm/docs/CMake.md](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/docs/CMake.md)（约 73-85 行）—— `cmake --build .` 进行构建、`cmake --build . --target install` 进行安装，并说明底层工具可以是 `make`/`ninja`/`msbuild` 等。

#### 4.5.4 代码实践

**实践目标：** 完成一次「配置 → 构建 → 安装」的完整最小流程，并体验增量构建。

**操作步骤（待本地验证，LLVM 完整构建耗时较长，最小化目标可大幅缩短）：**

1. 配置（只构建 X86，关闭示例/测试以加速）：
   ```bash
   cmake -S llvm -B build -G Ninja \
         -DCMAKE_BUILD_TYPE=Release \
         -DLLVM_ENABLE_ASSERTIONS=ON \
         -DLLVM_TARGETS_TO_BUILD=X86 \
         -DLLVM_BUILD_EXAMPLES=OFF \
         -DLLVM_BUILD_TESTS=OFF \
         -DCMAKE_INSTALL_PREFIX=$PWD/install
   ```
2. 构建（指定并行度，链接内存紧张时调小 `LLVM_PARALLEL_LINK_JOBS`）：
   ```bash
   cmake --build build -j$(nproc)
   ```
3. 验证产物：
   ```bash
   ./build/bin/llvm-dis --version    # 或 opt --version
   ```
4. 安装到 `install/`：
   ```bash
   cmake --build build --target install
   ```
5. 体验增量构建：随意 `touch` 一个源文件后再次执行步骤 2，观察只重编了少量目标。

**需要观察的现象：** 步骤 2 会看到大量并行编译进程；步骤 3 能打印出版本与目标三元组（应含 X86）；步骤 5 第二次构建只编译受影响文件，速度远快于首次。

**预期结果：** 得到一个可用的 `opt`/`llc`/`llvm-dis` 等工具，并能安装到指定前缀。若机器内存不足导致链接 OOM，按提示降低并行链接数。

#### 4.5.5 小练习与答案

**练习 1：** `cmake --build build` 和直接 `cd build && ninja` 有什么区别？
**参考答案：** 功能上等价（Ninja 生成器下，前者就是调用 `ninja`）。区别在于 `cmake --build` 是「与生成器无关」的统一写法，换用 Make/MSBuild 时命令不变，便于脚本和文档通用。

**练习 2：** 链接大型工具（如 `clang`）极易内存溢出，应该用哪个选项缓解？
**参考答案：** 用 `-DLLVM_PARALLEL_LINK_JOBS=1`（或更小）限制并发链接数；该选项专门为 Ninja 生成器设计（见 [llvm/docs/CMake.md](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/docs/CMake.md) 中关于 `LLVM_PARALLEL_LINK_JOBS` 的说明，约 254-257 行）。

## 5. 综合实践

把本讲的四个核心变量串起来，完成一个贴近真实开发场景的任务：**为「只想读 LLVM/Clang 源码、偶尔改动并验证」的读者，设计并执行一份构建配置，并解释每一条选项的理由。**

要求：

1. 写出完整的配置命令，至少包含：
   - 构建类型与断言（例如 `Release` + `LLVM_ENABLE_ASSERTIONS=ON`，兼顾性能与错误检查）；
   - 只构建 `clang` 这一个子项目（`LLVM_ENABLE_PROJECTS=clang`）；
   - 只构建 `X86`（与你的本机架构一致）目标；
   - 关闭示例、测试、文档以节省时间。
2. 解释你**为什么**这样组合：把每条选项对应到本讲讲过的某个源码行为（例如 `LLVM_TARGETS_TO_BUILD=X86` 对应 [llvm/CMakeLists.txt:1144-1166](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/CMakeLists.txt#L1144-L1166) 的目标枚举）。
3. 执行 `cmake --build` 后，用 `compile_commands.json` 找到 `lib/Support/` 下任一文件的编译命令，确认其中确实含有 `-UNDEBUG`（因为你开了断言）。
4. 把你的配置命令与每个选项的「一行理由」整理成一张表，作为日后复用的模板。

**参考命令骨架（待本地验证）：**

```bash
cmake -S llvm -B build -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DLLVM_ENABLE_ASSERTIONS=ON \
  -DLLVM_ENABLE_PROJECTS=clang \
  -DLLVM_TARGETS_TO_BUILD=X86 \
  -DLLVM_BUILD_EXAMPLES=OFF \
  -DLLVM_BUILD_TESTS=OFF \
  -DLLVM_INCLUDE_DOCS=OFF \
  -DLLVM_PARALLEL_LINK_JOBS=2 \
  -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
cmake --build build -j$(nproc)
```

完成后，你就拥有了本系列后续讲义（如阅读 `opt`、改写 Pass）所依赖的「可用工具链」。

## 6. 本讲小结

- LLVM 的构建入口是 `llvm/CMakeLists.txt`（不是仓库根目录），遵循「配置 → 构建 → 安装」三步，配置阶段必须用 `-B` 指定**独立的构建目录**（out-of-source）。
- `LLVM_ENABLE_PROJECTS` 选构建期组件（如 `clang`），`LLVM_ENABLE_RUNTIMES` 选运行时库（如 `compiler-rt`）；二者通过相对路径 `../<项目名>` 把 monorepo 子项目挂载进来，并经 `LLVM_TOOL_<NAME>_BUILD` 控制是否编译。
- `LLVM_TARGETS_TO_BUILD` 决定支持哪些 CPU 架构，默认 `"all"` 会展开成全部正式目标；选中后通过扫描 `lib/Target/<架构>` 生成 `Targets.def`，动态决定运行时注册哪些目标。
- `CMAKE_BUILD_TYPE` 默认 `Release`；`LLVM_ENABLE_ASSERTIONS` 仅 Debug 默认 ON，但可用 `-UNDEBUG` 在优化构建里保留断言，这是 LLVM 开发的常见配置。
- Ninja 是推荐生成器，配合 `LLVM_PARALLEL_LINK_JOBS` 限制并发链接；`CMAKE_EXPORT_COMPILE_COMMANDS` 默认开启，生成的 `compile_commands.json` 是阅读源码的利器。

## 7. 下一步学习建议

有了能用的构建，下一讲 [u1-l4 核心命令行工具一览](u1-l4-core-tools.md) 将带你认识 `opt`、`llc`、`llvm-as`、`llvm-dis`、`lli` 等工具——你刚刚构建出来的正是它们。建议：

- 现在就跑一遍 `./build/bin/opt --version`、`./build/bin/llc --version`，确认构建产物可用。
- 进阶阅读 [llvm/docs/CMake.md](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/docs/CMake.md)，把本讲没覆盖的选项（如 `LLVM_ENABLE_LLD`、`LLVM_USE_SANITIZER`、`LLVM_CCACHE_BUILD`）浏览一遍，建立「需要时去哪查」的印象。
- 读完 [u1-l4](u1-l4-core-tools.md) 后，进入第二单元（LLVM IR 与三段式编译），届时你会用这里构建的 `opt`/`llc` 真正跑通「源码 → IR → 目标码」的链路。
