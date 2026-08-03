# 构建、配置与测试

> 讲义 id：`u1-l2` ｜ 阶段：beginner ｜ 依赖：`u1-l1`

## 1. 本讲目标

学完本讲，你应当能够：

1. 用 CMake 的 **runtimes 集成方式**把 orc-rt 纳入一次 LLVM 构建，并知道它最终产出哪个库目标。
2. 说清 `ORC_RT_ENABLE_RTTI` / `ORC_RT_ENABLE_EXCEPTIONS` / `ORC_RT_LOG_BACKEND` / `ORC_RT_LOG_LEVEL` 等选项分别控制什么、默认值是什么、以及它们如何变成可执行代码里的真实开关。
3. 理解 `include/orc-rt-c/config.h.in` 这个模板文件如何被 CMake 渲染成 `config.h`，并在源码里提供 `ORC_RT_*` 宏。
4. 知道如何运行 `check-orc-rt`（回归测试）与 `check-orc-rt-unit`（单元测试），以及它们的依赖条件。

## 2. 前置知识

在进入本讲前，你需要先建立 `u1-l1` 的两个认知：

- **orc-rt 是「执行端」运行时**：它被链接进执行 JIT 代码的进程，而不是编译端。
- **它仍是实验性项目**：ABI/API 不稳定，**必须与同一次构建的 LLVM ORC 库配套使用**——这正是本讲的动机： orc-rt 几乎不可能「单独构建后拿来用」，它总是作为某一次 LLVM/runtimes 构建的一部分被生产出来。

此外，本讲假定你：

- 见过 CMake 的基本命令（`cmake -S / -B`、`-D` 传参、`option()`、`add_library`）。
- 知道「编译期宏」（`#define`）与「运行时变量」的区别——orc-rt 的多数配置是**编译期**生效的，改完配置要重新编译。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`CMakeLists.txt`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/CMakeLists.txt) | 顶层构建脚本：声明项目、定义所有 `ORC_RT_*` 选项、推导编译标志、渲染 `config.h`。 |
| [`include/orc-rt-c/config.h.in`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt-c/config.h.in) | 配置头模板：用 `@...@` / `#cmakedefine01` 占位，构建时被替换成真实的 `config.h`。 |
| [`include/orc-rt-c/Logging.h`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt-c/Logging.h) | 日志宏 `ORC_RT_LOG` 的定义，演示 `config.h` 里的宏如何驱动「编译期裁剪」。 |
| [`lib/executor/CMakeLists.txt`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/CMakeLists.txt) | 产出静态库目标 `orc-rt-executor`，并把编译标志与日志后端源文件接入。 |
| [`include/CMakeLists.txt`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/CMakeLists.txt) | 定义 INTERFACE 库 `orc-rt-headers`，把「源码头文件目录」与「生成的 config.h 目录」一并暴露给消费者。 |
| [`test/CMakeLists.txt`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/CMakeLists.txt) | 注册 `check-orc-rt`（回归）与 `check-orc-rt-unit`（单元）两个测试目标。 |
| [`docs/Building-orc-rt.md`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/docs/Building-orc-rt.md) | 官方构建说明，给出命令骨架与常用 CMake 变量。 |

---

## 4. 核心概念与源码讲解

### 4.1 CMake 构建流程与 runtimes 集成

#### 4.1.1 概念说明

orc-rt 不是「解压后 `mkdir build && cmake .` 就能独立跑起来」的普通库。它属于 LLVM 的 **runtimes** 体系——一组与 LLVM 主树一起被「带进」构建的运行时（如 libcxx、compiler-rt、libc 等）。

这意味着两件事：

1. **入口在外部**：你配置的不是 `orc-rt/` 本身，而是 `<llvm-monorepo>/runtimes`，并通过 `-DLLVM_ENABLE_RUNTIMES=orc-rt` 指定「这次只带 orc-rt 进来」。orc-rt 的 `CMakeLists.txt` 随后被 runtimes 框架以正确的目标三元组（target triple）、编译器、工具链上下文调用。
2. **配套约束**：因为 orc-rt 要服务同一次构建出来的 ORC 库，所以它的 RTTI/异常/标准库 ABI 必须能和主树对齐——这也是为什么 orc-rt 把这些做成 CMake 选项，而不是写死。

#### 4.1.2 核心流程

一次典型构建的数据流（伪流程）：

```text
你执行:
  cmake -G <generator> \
        -DLLVM_ENABLE_RUNTIMES=orc-rt \
        [其他 -D 选项] \
        <llvm-monorepo>/runtimes
        │
        ▼
runtimes 框架发现 ORC_RT，进入 orc-rt/CMakeLists.txt
        │
        ├── project(OrcRT LANGUAGES C CXX ASM)        # 确立工程
        ├── 设置 C++17、禁用编译器扩展
        ├── 定义所有 option()                           # RTTI/异常/日志/断言...
        ├── 由选项推导 ORC_RT_COMPILE_FLAGS            # -frtti / -fexceptions ...
        ├── configure_file(... config.h.in -> config.h) # 渲染配置头
        └── add_subdirectory(include / lib / tools / test)
                                  │
                                  ▼
        lib/executor 产出静态库目标 orc-rt-executor
        test         注册 check-orc-rt / check-orc-rt-unit
```

#### 4.1.3 源码精读

**项目确立与最低版本**。顶层脚本先声明最低 CMake 版本与项目名（C++/C/汇编三语言）：

[CMakeLists.txt:7-20](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/CMakeLists.txt#L7-L20) —— `cmake_minimum_required(VERSION 3.20.0)` 并 `project(OrcRT LANGUAGES C CXX ASM)`。注意它同时给出一个**前瞻警告**：LLVM 24 起最低版本将升到 3.31.0，旧 CMake 现在能用，但未来会变成错误。

**C++ 标准固定为 17、不允许编译器扩展**：

[CMakeLists.txt:32-35](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/CMakeLists.txt#L32-L35) —— `CMAKE_CXX_STANDARD 17`、`CMAKE_CXX_STANDARD_REQUIRED YES`、`CMAKE_CXX_EXTENSIONS NO`。最后这一条很关键：orc-rt 要求**可移植**的标准 C++，不依赖 GCC/Clang 的方言扩展。

**子目录顺序**：

[CMakeLists.txt:125-135](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/CMakeLists.txt#L125-L135) —— 依次 `add_subdirectory(docs / include / lib / tools / test)`。`include` 和 `lib` 的顺序很重要：`lib` 里的目标要链接 `include` 产出的 `orc-rt-headers`。

**产出的库目标**。最终的执行端运行时是一个**静态库**：

[lib/executor/CMakeLists.txt:32-35](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/CMakeLists.txt#L32-L35) —— `add_library(orc-rt-executor STATIC ${files})`，并 `target_link_libraries(... PUBLIC orc-rt-headers)`。也就是说，**真正的构建产物叫 `orc-rt-executor`**，消费者链接它即可同时拿到头文件目录（含生成的 `config.h`）。

> ⚠️ 命名小提醒：`docs/Building-orc-rt.md` 里写的是 `make orc-rt`。在 `orc-rt/` 目录内的 CMake 里，**能直接验证的库名是 `orc-rt-executor`**；`orc-rt` 这个聚合名由 runtimes 集成层提供。构建时如果 `make orc-rt` 找不到目标，请改用 `orc-rt-executor`。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：确认构建入口、产物目标与子目录顺序。
2. **步骤**：
   - 打开 [`docs/Building-orc-rt.md`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/docs/Building-orc-rt.md) 的 Getting Started，抄下官方推荐的 `cmake` 命令行（配置源指向 `<llvm-monorepo>/runtimes`）。
   - 在 [`CMakeLists.txt:125-135`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/CMakeLists.txt#L125-L135) 数清楚有哪些 `add_subdirectory`。
   - 在 [`lib/executor/CMakeLists.txt:32`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/CMakeLists.txt#L32) 确认库类型与名称。
3. **应观察**：配置阶段 CMake 会打印 `ORC_RT_LLVM_TOOLS_AVAILABLE` 的探测结果（FileCheck / not 是否找到），并写入生成的 `config.h`。
4. **预期结果**：你能用一句话回答「 orc-rt 构建产出的库目标叫什么、是静态还是动态」。
5. 实际运行 `cmake`+`make` 的构建结果：**待本地验证**（完整 LLVM/runtimes 构建耗时较长，且依赖你本机的 LLVM 主树）。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 orc-rt 用 `-DLLVM_ENABLE_RUNTIMES=orc-rt` 配置 runtimes 目录，而不是直接在 `orc-rt/` 里配置？
  - **答案**：runtimes 框架会以正确的目标三元组、配套编译器与工具链上下文调用 orc-rt 的 `CMakeLists.txt`，保证它和同一次构建的 LLVM/ORC 库 ABI 一致；直接在 `orc-rt/` 配置会丢失这些上下文。
- **练习 2**：顶层要求 `CMAKE_CXX_EXTENSIONS NO`，这排除了哪类风险？
  - **答案**：避免代码依赖 GCC/Clang 方言扩展，保证在不同编译器上都按 ISO C++17 行为编译，提升可移植性。

---

### 4.2 编译期配置选项与生成的 config.h

#### 4.2.1 概念说明

orc-rt 的「配置」几乎全是**编译期**的：你在 `cmake -D` 阶段选好，CMake 把结果写进一个生成的头文件 `config.h`，源码再用 `#if ORC_RT_...` 来开启/关闭整段代码。改配置 = 重新编译，**没有运行时开关**。

这一机制由两部分组成：

- **CMake 侧**：一堆 `option(...)` 定义布尔开关，外加 `configure_file()` 把模板里的占位符替换成真实值。
- **模板侧**：`include/orc-rt-c/config.h.in` 用 `#cmakedefine01` 和 `@VAR@` 两种占位语法，描述「生成出来的 `config.h` 长什么样」。

#### 4.2.2 核心流程

```text
option(ORC_RT_ENABLE_RTTI ...)        # 用户可改的布尔开关
option(ORC_RT_ENABLE_EXCEPTIONS ...)
   │
   ▼  推导
ORC_RT_COMPILE_FLAGS  = [-frtti/-fno-rtti, -fexceptions/-fno-exceptions]
ORC_RT_LOG_LEVEL_VALUE  = ORC_RT_LOG_LEVEL_INFO   (字符串大写化)
ORC_RT_LOG_BACKEND_VALUE= ORC_RT_LOG_BACKEND_NONE  (字符串大写化)
   │
   ▼  configure_file(... @ONLY)
include/orc-rt-c/config.h.in   ──渲染──>   ${BUILD}/include/orc-rt-c/config.h
   #cmakedefine01 ORC_RT_ENABLE_RTTI   ->  #define ORC_RT_ENABLE_RTTI 1
   @ORC_RT_LOG_LEVEL_VALUE@             ->  ORC_RT_LOG_LEVEL_INFO
   │
   ▼  被源码 #include "orc-rt-c/config.h" 使用
Logging.h / Error.h / RTTI.h  用 #if 判定编译哪些代码
```

#### 4.2.3 源码精读

**布尔开关**——RTTI 与异常默认都是开的：

[CMakeLists.txt:55-71](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/CMakeLists.txt#L55-L71) —— `option(ORC_RT_ENABLE_RTTI "Enable RTTI." ON)`、`option(ORC_RT_ENABLE_EXCEPTIONS "Enable exceptions." ON)`，并根据取值把 `-frtti`/`-fno-rtti`、`-fexceptions`/`-fno-exceptions` 追加进 `ORC_RT_COMPILE_FLAGS`。注意这套标志随后是 **PRIVATE** 挂到目标上的：

[lib/executor/CMakeLists.txt:39](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/CMakeLists.txt#L39) —— `target_compile_options(orc-rt-executor PRIVATE ${ORC_RT_COMPILE_FLAGS})`。`PRIVATE` 意味着这些标志只作用于 orc-rt 自身的编译，**不会**传染给链接 orc-rt 的消费者（消费者要自己决定开不开 RTTI/异常）。

**模板文件如何变 `config.h`**。`configure_file` 调用就是渲染动作：

[CMakeLists.txt:113-119](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/CMakeLists.txt#L113-L119) —— 把 `config.h.in` 渲染到 `${CMAKE_CURRENT_BINARY_DIR}/include/orc-rt-c/config.h`，`@ONLY` 表示只替换 `@...@` 形式的变量，避免误伤 `${...}`。

**消费者如何拿到这个生成头**。`orc-rt-headers` 是一个 INTERFACE 库，同时把「源码 include 目录」和「构建 include 目录」（生成的 `config.h` 在此）暴露出去：

[include/CMakeLists.txt:51-61](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/CMakeLists.txt#L51-L61) —— `ORC_RT_GENERATED_HEADERS` 指向 `${CMAKE_CURRENT_BINARY_DIR}/orc-rt-c/config.h`，并通过 `$<BUILD_INTERFACE:${PROJECT_BINARY_DIR}/include>` 让 `#include "orc-rt-c/config.h"` 在构建期可被发现。

**模板里到底有哪些占位**。`config.h.in` 用两种语法：

[config.h.in:13-39](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt-c/config.h.in#L13-L39) ——
- `#cmakedefine01 ORC_RT_ENABLE_RTTI` / `ORC_RT_ENABLE_EXCEPTIONS`：CMake 里对应变量为真则生成 `#define X 1`，否则 `#define X 0`。
- `@ORC_RT_LOG_LEVEL_VALUE@`、`@ORC_RT_LOG_BACKEND_VALUE@`：被替换成具体符号名（如 `ORC_RT_LOG_LEVEL_INFO`）。
- 模板里还自带两道 `#ifndef @...@ ... #error` 防线：如果 CMake 推导出的符号没在上面定义，就**编译期报错**，防止配置不一致。

#### 4.2.4 代码实践（本讲核心任务）

1. **目标**：读 `config.h.in`，列出它最终定义的所有 `ORC_RT_*` 宏，并验证你对 CMake→头文件链路的理解。
2. **步骤**：
   - 打开 [`include/orc-rt-c/config.h.in`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt-c/config.h.in) 全文。
   - 把其中出现的每一个 `ORC_RT_*` 标记分成三类抄下来：① `#cmakedefine01` 类（受选项控制）；② 永远定义的常量（固定数值）；③ 由 `@...@` 替换得到的「选中值」宏。
   - 对照 [`CMakeLists.txt:88-101`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/CMakeLists.txt#L88-L101) 的 `string(TOUPPER ...)` 推导，预测默认配置下 `ORC_RT_LOG_LEVEL` 与 `ORC_RT_LOG_BACKEND` 分别展开成哪个符号。
3. **应观察**：模板里没有任何运行时值，全部是编译期常量；`#error` 守卫保证「选中的级别/后端」必须落在预定义集合内。
4. **预期结果**（见 4.2.5）。
5. 实际构建产物 `config.h`：**待本地验证**（你在自己的 build 目录里打开 `${build}/include/orc-rt-c/config.h` 即可核对）。

#### 4.2.5 小练习与答案

- **练习 1**：`config.h.in` 会生成/定义哪些 `ORC_RT_*` 宏？
  - **答案**（共 13 个 `ORC_RT_*` 标记）：
    - 受选项控制（`#cmakedefine01`，值为 0/1）：`ORC_RT_ENABLE_RTTI`、`ORC_RT_ENABLE_EXCEPTIONS`。
    - 永久常量（固定数值）：`ORC_RT_LOG_LEVEL_DEBUG/INFO/WARNING/ERROR/OFF`（0..4）与 `ORC_RT_LOG_LEVEL_COUNT`；`ORC_RT_LOG_BACKEND_NONE/PRINTF/OS_LOG`（0..2）。
    - 选中值（由 `@...@` 替换）：`ORC_RT_LOG_LEVEL`（展开成上面某个 `_LEVEL_*` 符号）、`ORC_RT_LOG_BACKEND`（展开成某个 `_BACKEND_*` 符号）。
- **练习 2**：`target_compile_options(... PRIVATE ${ORC_RT_COMPILE_FLAGS})` 里为什么是 `PRIVATE` 而不是 `PUBLIC`？
  - **答案**：RTTI/异常标志只应作用于 orc-rt 自身源码的编译；消费者（链接 `orc-rt-executor` 的程序）是否开启 RTTI/异常由它自己决定，不应被 orc-rt 的内部选择强制传染。
- **练习 3**：模板里 `#ifndef @ORC_RT_LOG_LEVEL_VALUE@ #error ...` 这道防线防的是什么？
  - **答案**：防止 CMake 推导出一个不在预定义集合（DEBUG/INFO/WARNING/ERROR/OFF）里的级别符号——一旦推导出错，编译 `config.h` 时就会立刻报错，把配置错误挡在编译之前。

---

### 4.3 日志后端、级别与编译期裁剪

#### 4.3.1 概念说明

orc-rt 的日志由两个**正交**的编译期旋钮控制：

| 旋钮 | 取值 | 默认 | 含义 |
| --- | --- | --- | --- |
| `ORC_RT_LOG_BACKEND` | `none` / `printf` / `os_log` | `none` | 日志往**哪里**写（或完全不写）。 |
| `ORC_RT_LOG_LEVEL` | `error` / `warning` / `info` / `debug` | `info` | 编译进二进制的**最低级别**（floor）。 |

理解两个要点：

1. **默认 `none` 会把日志整段编译掉**——这时的 orc-rt 二进制里没有任何日志代码，`ORC_RT_LOG_LEVEL` 完全失效。这也是为什么 orc-rt 默认是「安静的」。
2. **`os_log` 仅限 Apple 平台**，且需要 `<os/log.h>`；非 Apple 上选它会被 CMake 直接 `FATAL_ERROR`。

> 名词解释：**floor（下限）**——只编译「级别 ≥ floor」的日志点。`info` 为 floor 时，`Debug` 日志会被裁掉（不进二进制），`Info/Warning/Error` 保留。

#### 4.3.2 核心流程

```text
ORC_RT_LOG(Debug, General, "x=%d", n)
        │
        ▼  展开 ORC_RT_LOG(Level, Category, ...)
   ORC_RT_LOG_Debug(orc_rt_log_Category_General, "x=%d", n)
        │
        ▼  按 ORC_RT_LOG_BACKEND 选择实现
   backend == none  -> ORC_RT_LOG_DISABLED(...)   # 只做编译期类型检查，不发代码
   backend == printf & DEBUG < floor -> ORC_RT_LOG_DISABLED(...)
   backend == printf & DEBUG >= floor -> orc_rt_log_printf(LEVEL_DEBUG, Cat, "x=%d", n)
```

关键：即便日志被裁掉，调用点仍会被 **`sizeof` 上下文里的格式串类型检查**——所以 `ORC_RT_LOG` 永远是「类型安全」的，只是有时不产生任何机器码。

#### 4.3.3 源码精读

**CMake 侧的两个旋钮与校验**：

[CMakeLists.txt:73-111](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/CMakeLists.txt#L73-L111) —— 定义 `ORC_RT_LOG_LEVEL`（默认 `info`）与 `ORC_RT_LOG_BACKEND`（默认 `none`），并做了三层校验：
- 级别必须在 `error/warning/info/debug` 集合内（否则 `FATAL_ERROR`）。
- 后端必须在 `none/printf/os_log` 内；`os_log` 还要求 `APPLE` 为真。
- **当 backend 为 `none` 且 level 不是默认 `info` 时，发出一个 `WARNING`**：提醒你 level 在这种组合下没有任何效果。这段逻辑（[L105-L111](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/CMakeLists.txt#L105-L111)）是回答「关闭日志要设哪两个变量」的关键依据。

**后端决定要编译哪些源文件**。`printf` 后端需要一个运行时实现文件，`none` 是纯头文件、不发代码：

[lib/executor/CMakeLists.txt:24-30](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/CMakeLists.txt#L24-L30) —— 仅当 `ORC_RT_LOG_BACKEND STREQUAL "printf"` 才把 `Logging_printf.cpp` 编进库；`os_log` 才编 `Logging_oslog.cpp`。这直接说明：换后端会改变库的源文件集合。

**裁剪是怎么发生的**。在 `Logging.h` 里，`none` 后端把每个级别都映射到「只类型检查」的禁用宏：

[Logging.h:166-176](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt-c/Logging.h#L166-L176) —— `none` 下 `ORC_RT_LOG_Error/Warning/Info/Debug` 全部等于 `ORC_RT_LOG_DISABLED`。而 `printf` 后端则按 floor 逐级判断（[Logging.h:194-224](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt-c/Logging.h#L194-L224)），如 `#if ORC_RT_LOG_LEVEL_DEBUG >= ORC_RT_LOG_LEVEL` 才生成 `orc_rt_log_printf(...)` 调用，否则同样落到 `DISABLED`。

#### 4.3.4 代码实践（源码阅读型）

1. **目标**：预测同一句 `ORC_RT_LOG(Debug, General, "hi")` 在不同 backend/level 组合下是否产生代码。
2. **步骤**：
   - 假设四种组合：① backend=none / level=info；② backend=printf / level=info；③ backend=printf / level=debug；④ backend=none / level=debug。
   - 对每种组合，沿 [`Logging.h:178-224`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt-c/Logging.h#L178-L224) 的 `#if` 判定 `ORC_RT_LOG_Debug` 展开成 `orc_rt_log_printf` 还是 `ORC_RT_LOG_DISABLED`。
   - 对组合④，解释为何它会触发 [`CMakeLists.txt:105-111`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/CMakeLists.txt#L105-L111) 的 WARNING。
3. **应观察**：`none` 永远裁掉所有级别；`printf` 下低于 floor 的级别被裁、≥ floor 的保留。
4. **预期结果**：①无代码；②无代码（debug<info floor）；③有代码；④无代码 + 配置 WARNING。
5. 是否真的「无代码」需看汇编：**待本地验证**（可用 `clang -S -emit-llvm` 或开 `-O` 后看符号表）。

#### 4.3.5 小练习与答案

- **练习 1**（本讲核心实践题）：若想**完全关闭日志**，应同时设置哪两个 CMake 变量？
  - **答案**：真正的开关只有**一个**——`-DORC_RT_LOG_BACKEND=none`（默认即是）。在 `none` 下，[`Logging.h:166-176`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt-c/Logging.h#L166-L176) 把所有级别映射成 `ORC_RT_LOG_DISABLED`，`ORC_RT_LOG_LEVEL` **完全不生效**。题目里涉及的「第二个变量」是 `ORC_RT_LOG_LEVEL`：为避免 [`CMakeLists.txt:105-111`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/CMakeLists.txt#L105-L111) 那条「level 在 none 下被忽略」的 WARNING，请让它保持默认 `info`（或显式 `-DORC_RT_LOG_LEVEL=info`）。一句话：**`ORC_RT_LOG_BACKEND=none` 关日志，`ORC_RT_LOG_LEVEL=info` 保持安静不报警告**。
- **练习 2**：为什么 `ORC_RT_LOG` 的参数被要求「没有可观察的副作用」？
  - **答案**：因为参数是否被求值取决于后端与级别（被裁掉的日志点里参数根本不运行），见 [`Logging.h:35-37`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt-c/Logging.h#L35-L37)。如果把 `++count` 当参数，在不同配置下行为会变，是隐患。
- **练习 3**：把后端从 `none` 切到 `printf`，库的源文件集合会发生什么变化？
  - **答案**：会多编一个 [`lib/executor/Logging_printf.cpp`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/lib/executor/CMakeLists.txt#L24-L30)（`orc_rt_log_printf` 的实现），库体积变大、开始有 stderr/文件输出。

---

## 5. 综合实践

把本讲三块内容串起来，做一次「配置即代码」的端到端演练。

**任务**：用三种不同配置描述同一个 orc-rt，预测它们各自生成的 `config.h` 关键宏与行为差异，最后（在本地）验证。

**步骤**：

1. **基线配置**（默认）：
   ```bash
   cmake -G Ninja -DLLVM_ENABLE_RUNTIMES=orc-rt \
         -DCMAKE_BUILD_TYPE=Release <llvm>/runtimes
   ```
   预测：`ORC_RT_ENABLE_RTTI=1`、`ORC_RT_ENABLE_EXCEPTIONS=1`、`ORC_RT_LOG_BACKEND=ORC_RT_LOG_BACKEND_NONE`、`ORC_RT_LOG_LEVEL=ORC_RT_LOG_LEVEL_INFO`；库目标 `orc-rt-executor`，无日志代码。

2. **关闭 RTTI、开启 printf 日志到 debug**：
   ```bash
   cmake -G Ninja -DLLVM_ENABLE_RUNTIMES=orc-rt \
         -DORC_RT_ENABLE_RTTI=OFF \
         -DORC_RT_LOG_BACKEND=printf -DORC_RT_LOG_LEVEL=debug ...
   ```
   预测：`ORC_RT_ENABLE_RTTI=0`（源码以 `-fno-rtti` 编译，orc-rt 自带的「自定义 RTTI」体系要能独立工作——见后续 `u9-l1`）；`config.h` 里 `ORC_RT_LOG_BACKEND=ORC_RT_LOG_BACKEND_PRINTF`、`ORC_RT_LOG_LEVEL=ORC_RT_LOG_LEVEL_DEBUG`，且库多编了 `Logging_printf.cpp`。

3. **运行测试**（两条命令，验证 `u1-l2` 的测试目标）：
   ```bash
   cmake --build . --target orc-rt-executor   # 或 docs 里的 'make orc-rt'
   cmake --build . --target check-orc-rt-unit # GoogleTest 单元测试
   cmake --build . --target check-orc-rt      # lit 回归测试（需 FileCheck/not）
   ```

**需要核对的现象**：
- 打开构建出的 `${build}/include/orc-rt-c/config.h`，确认三类宏与你的预测一致。
- 回归测试 `check-orc-rt` 只有在 `ORC_RT_LLVM_TOOLS_AVAILABLE=TRUE`（找到 `FileCheck` 与 `not`）时才会注册，否则 [`test/CMakeLists.txt:27-29`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/CMakeLists.txt#L27-L29) 会打印 WARNING 并禁用；探测逻辑见 [`cmake/OrcRTTesting.cmake`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/cmake/OrcRTTesting.cmake)。
- 单元测试 `check-orc-rt-unit` 走 GoogleTest，若 GTest 不可用会在 [`test/unit/CMakeLists.txt:4-8`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/test/unit/CMakeLists.txt#L4-L8) 打印 WARNING 并 `return()`（提示用 `LLVM_INSTALL_GTEST=ON`）。

**最终交付**：一张三列表格（基线 / 关 RTTI+开日志 / 你自选第三种），每行写明 `config.h` 关键宏取值、是否含 `Logging_printf.cpp`、`check-orc-rt` 是否可用。

> 实际构建与测试运行结果：**待本地验证**——以上命令与目标名均来自真实源码，但能否成功取决于你本机 LLVM 主树与工具链状态。

## 6. 本讲小结

- orc-rt 通过 **runtimes 集成**被构建：配置 `<llvm>/runtimes` 并 `-DLLVM_ENABLE_RUNTIMES=orc-rt`，最终产出**静态库目标 `orc-rt-executor`**（`PRIVATE` 挂编译标志，不传染消费者）。
- 顶层固定 **C++17、禁用编译器扩展**；`ORC_RT_ENABLE_RTTI` / `ORC_RT_ENABLE_EXCEPTIONS` 默认都开，分别决定 `-frtti`/`-fexceptions` 是否加入编译。
- 配置是**编译期**的：`option()` → `ORC_RT_COMPILE_FLAGS` → `configure_file(config.h.in → config.h)`，源码再用 `ORC_RT_*` 宏裁剪代码；模板里的 `#error` 守卫把非法级别/后端挡在编译前。
- 日志由两个**正交**旋钮控制：`ORC_RT_LOG_BACKEND`（`none` 默认，整段裁掉 / `printf` / `os_log` 仅 Apple）与 `ORC_RT_LOG_LEVEL`（floor，仅对非 none 后端生效）。
- 默认 `none` 时日志完全不出现在二进制里，但调用点仍被 `sizeof` 做**类型检查**。
- 测试有两个目标：`check-orc-rt`（lit 回归，依赖 `FileCheck`/`not`）与 `check-orc-rt-unit`（GoogleTest，依赖 GTest）。

## 7. 下一步学习建议

- 配置好能跑 `check-orc-rt-unit` 的环境后，下一讲 [`u1-l3`](u1-l3-directory-layout.md) 会带你逛 orc-rt 的目录结构（`include/orc-rt` vs `include/orc-rt-c`、`lib/executor`、`test` 三类测试），为读懂具体模块做准备。
- 对「日志裁剪」感兴趣，可直接读 [`include/orc-rt-c/Logging.h`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/orc-rt/include/orc-rt-c/Logging.h)，后续 `u10-l2` 会系统讲解三种后端实现。
- 想理解「关掉 RTTI 后 orc-rt 怎么做类型判别」，可预习 `u9-l1`（自定义 RTTI 体系）。
