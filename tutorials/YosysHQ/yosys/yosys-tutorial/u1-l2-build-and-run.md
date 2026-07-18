# 构建 Yosys：CMake 构建与可执行入口

## 1. 本讲目标

上一篇（u1-l1）我们已经知道 Yosys 是一个开源 RTL 综合框架，它的核心思想是「组合 pass 完成综合」。本篇要回答两个非常具体的问题：

1. **怎么把这份 C++ 源码变成一个能跑的 `yosys` 可执行程序？**
2. **当你敲下 `./yosys -V` 或 `./yosys -p "synth"` 时，程序内部到底发生了什么？**

学完本讲，你应当能够：

- 用 CMake 完成 Yosys 的 Release 构建，并理解构建选项（ABC / Python / TCL / zlib 等开关）的含义。
- 说清楚 `kernel/driver.cc` 中 `main()` 的完整执行流程：解析命令行 → 初始化 → 读输入 → 跑 pass → 写输出 → 收尾。
- 解释 `yosys_setup()` / `yosys_shutdown()` 这对函数到底初始化和销毁了哪些全局状态，尤其是 pass 是如何被自动注册进 `pass_register` 的。

这三个点正好对应本讲的三个最小模块。

## 2. 前置知识

在进入源码之前，先通俗地铺垫几个概念。

### 2.1 什么是「构建（build）」

源码（一堆 `.cc` / `.h` 文件）本身不能直接运行，需要用**编译器**（如 `g++` / `clang++`）把它们翻译成机器码，再由**链接器**拼成一个可执行文件。当源码文件成百上千、还依赖很多外部库时，手动敲编译命令是不现实的，于是有了**构建系统**：你写一份配置清单，它自动算出「先编译谁、再链接谁、带哪些库」。

Yosys 用的构建系统是 **CMake**。CMake 本身不直接编译，它根据你写的 `CMakeLists.txt` 生成一份「原生构建文件」（Linux 上默认是 Makefile 或 Ninja 文件），然后再调用 `make` / `ninja` 去真正编译。

### 2.2 「out-of-tree 构建」与「构建类型」

- **out-of-tree（树外）构建**：把所有编译产生的中间文件（`.o`、可执行文件等）放到一个单独的目录里（习惯叫 `build/`），不污染源码目录。Yosys 强制要求这样做，**不允许在源码根目录直接构建**。
- **构建类型（CMAKE_BUILD_TYPE）**：决定优化级别。常见的有 `Debug`（`-O0 -g`，方便调试）、`Release`（`-O3`，跑得快）。Yosys 还自定义了一个 `Sanitize` 类型用于内存/地址排错。

### 2.3 命令行参数解析库 cxxopts

C++ 标准库没有自带「解析命令行参数（`-V`、`-p ...`）」的工具。Yosys 用了一个第三方头文件库 **cxxopts**（在 `libs/cxxopts/`），它让你用类似表格的方式声明「有哪些选项、每个选项带不带参数」，然后一次性把 `argv` 解析成一个可查询的结果对象。本讲在 `driver.cc` 里会看到它的用法。

### 2.4 「pass 自动注册」要解决的问题

Yosys 里有上百个 pass（如 `synth`、`opt`、`abc`）。如果每加一个 pass 都要去某个中心列表里手动登记一行，既啰嗦又容易漏。Yosys 用了一个巧妙办法：**利用 C++ 全局对象的构造函数**。每个 pass 在源码里都有一个「全局静态实例」，程序一启动这些实例的构造函数就自动执行，把自己挂到一个全局链表上；之后 `yosys_setup()` 统一遍历这条链表，把它们登记进 `pass_register`。我们在 4.3 节会精读这段逻辑。

> 承接 u1-l1 的术语：本讲会反复出现 **pass、前端(frontend)、后端(backend)、RTLIL、synth** 这些词，含义与上一篇一致，不再重复定义。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [CMakeLists.txt](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/CMakeLists.txt) | 顶层 CMake 配置：声明构建选项、依赖、定义 `yosys` 可执行与 `libyosys` 库两个目标。 |
| [kernel/driver.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc) | 程序入口 `main()` 所在文件：解析命令行、调度前端/pass/后端、打印统计。 |
| [kernel/yosys.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys.cc) | 运行时核心：`yosys_setup()` / `yosys_shutdown()`、`run_frontend` / `run_pass` / `run_backend` / `shell`。 |
| [kernel/yosys.h](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys.h) | 上述函数的声明，以及全局 `yosys_design` 指针。 |
| [kernel/register.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc) | pass / frontend / backend 的注册机制：`pass_register` 表与 `init_register()`。 |
| [docs/source/getting_started/installation.rst](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/docs/source/getting_started/installation.rst) | 官方安装与构建说明，是本讲命令行的权威出处。 |

记忆要点：**「配置在 CMakeLists，入口在 driver.cc，初始化在 yosys.cc，注册在 register.cc」**。

## 4. 核心概念与源码讲解

### 4.1 CMake 选项与组件

#### 4.1.1 概念说明

Yosys 是个庞大工程，不同用户需要的功能不一样：做 ASIC 的需要 ABC 逻辑综合、做形式验证的需要 SMT 后端、做插件开发的需要动态加载（libffi/dlfcn）、想在 Python 里调用的需要 Python 绑定。如果把这些全编译进去，既慢又依赖一堆库；如果全砍掉，又不好用。

CMake 的解决思路是「**开关 + 条件编译**」：

1. 暴露一批 `YOSYS_WITHOUT_*` / `YOSYS_WITH_*` 选项给用户，用户在配置时用 `-D` 决定开哪些。
2. CMake 探测系统里是否真的装了对应的库（如 `zlib`、`tcl`）。
3. 把「用户意愿」和「库是否存在」合并，得到一组最终的 `YOSYS_ENABLE_*` 宏。
4. 这些宏通过编译选项传给所有源文件，源码里用 `#ifdef YOSYS_ENABLE_TCL` 这样的条件编译来决定是否编入某段代码。

最终构建会产出两个核心产物：可执行程序 **`yosys`**（命令行驱动器）和共享库 **`libyosys`**（供别人当库链接，详见 u9 单元的 C++ API 讲义）。

#### 4.1.2 核心流程

整个配置阶段的流程可以概括为：

```text
1. 防呆检查：禁止 in-tree 构建
2. 声明项目、C++ 标准（C++20）
3. 声明构建选项（YOSYS_WITHOUT_ABC / ZLIB / TCL ...）
4. find_package 探测必需依赖（FLEX / BISON / Python3）
5. pkg_config_import 探测可选依赖（zlib / libffi / readline / editline / tcl）
6. condition() 合并「意愿 ∧ 探测结果」→ YOSYS_ENABLE_*
7. feature_summary 打印一份「启用了哪些特性」的清单
8. 定义目标：可执行 yosys、库 libyosys
```

其中第 6 步是理解整个开关体系的关键。一个特性最终启用，必须同时满足「用户没主动关掉它」且「系统里能找到它的依赖」。形式化地写成布尔表达式就是：

\[
\text{YOSYS\_ENABLE\_X} \;=\; \text{x\_FOUND} \;\wedge\; \neg\,\text{YOSYS\_WITHOUT\_X}
\]

例如对 zlib（[CMakeLists.txt:307](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/CMakeLists.txt#L307)）：

\[
\text{YOSYS\_ENABLE\_ZLIB} \;=\; \text{zlib\_FOUND} \;\wedge\; \neg\,\text{YOSYS\_WITHOUT\_ZLIB}
\]

少数特例不依赖外部库，只看用户意愿，例如 ABC 只由 `¬YOSYS_WITHOUT_ABC` 决定（[CMakeLists.txt:306](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/CMakeLists.txt#L306)）。

#### 4.1.3 源码精读

**① 防呆：禁止 in-tree 构建**

[CMakeLists.txt:1-10](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/CMakeLists.txt#L1-L10) 在最开头就检查：如果「构建目录」和「源码目录」是同一个，就直接报错并提示正确的命令。这就是为什么官方文档反复强调必须用 `-B build` 指定一个单独目录。

**② C++20 与编译器要求**

[CMakeLists.txt:12-13](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/CMakeLists.txt#L12-L13) 声明项目名 `yosys`；[CMakeLists.txt:89-90](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/CMakeLists.txt#L89-L90) 把 C++ 标准设为 **C++20** 并设为必需。这也呼应了 installation.rst 里「A C++ compiler with C++20 support is required」的要求（[installation.rst:90](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/docs/source/getting_started/installation.rst#L90)）。

**③ 一组 `YOSYS_WITHOUT_*` / `YOSYS_WITH_*` 选项**

[CMakeLists.txt:48-58](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/CMakeLists.txt#L48-L58) 集中声明了开关，摘录关键几行（节选）：

```cmake
option(YOSYS_DISABLE_THREADS "Disable threading" OFF)
option(YOSYS_WITHOUT_ABC "Disable ABC support (not recommended)" OFF)
option(YOSYS_WITHOUT_ZLIB "Disable zlib integration" OFF)
option(YOSYS_WITHOUT_SLANG "Disable Slang integration" OFF)
option(YOSYS_WITHOUT_TCL "Disable Tcl integration" OFF)
option(YOSYS_WITH_PYTHON "Enable Python integration" OFF)
```

注意命名规律：默认**启用**的特性用 `YOSYS_WITHOUT_X`（默认 `OFF`，即「不关闭」= 启用）；默认**不启用**的 Python 用 `YOSYS_WITH_PYTHON`（默认 `OFF`，需显式打开）。ABC 那行还特意标注「not recommended」，因为关闭 ABC 会丢掉高质量的逻辑综合能力。

**④ `condition()` 把意愿与探测结果合并**

[CMakeLists.txt:302-314](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/CMakeLists.txt#L302-L314) 是开关体系的心脏（节选）：

```cmake
condition(YOSYS_ENABLE_THREADS Threads_FOUND AND HAVE_PTHREAD_CREATE AND NOT YOSYS_DISABLE_THREADS)
condition(YOSYS_ENABLE_ABC NOT YOSYS_WITHOUT_ABC)
condition(YOSYS_ENABLE_ZLIB zlib_FOUND AND NOT YOSYS_WITHOUT_ZLIB)
condition(YOSYS_ENABLE_TCL tcl_FOUND AND libtommath_FOUND AND NOT YOSYS_WITHOUT_TCL)
condition(YOSYS_ENABLE_PYTHON Python3Devel_FOUND AND PyosysEnv_FOUND AND YOSYS_WITH_PYTHON)
```

`condition(NAME expr)` 是 Yosys 自己在 `cmake/Condition.cmake` 里定义的宏：当 `expr` 为真，就把 `NAME` 设为 `ON`，并加入编译定义，于是源码里的 `#ifdef YOSYS_ENABLE_TCL` 就能生效。

**⑤ `feature_summary` 给用户一份清单**

[CMakeLists.txt:331-342](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/CMakeLists.txt#L331-L342) 调用 CMake 自带的 `feature_summary`，结合前面 `add_feature_info(...)`（[CMakeLists.txt:318-329](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/CMakeLists.txt#L318-L329)）打印一段类似下面的输出（实际输出待本地验证）：

```text
The following features have been enabled:
 * have_threads, Multithreaded netlist operations
 * with_abc, Production-quality logic synthesis flow
 * with_zlib, Transparent Gzip decompression and FST file format support
```

**⑥ 定义最终目标：`yosys` 与 `libyosys`**

[CMakeLists.txt:421-426](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/CMakeLists.txt#L421-L426) 定义了可执行程序 `yosys`（节选）：

```cmake
yosys_cxx_executable(yosys
    OUTPUT_NAME yosys
    INSTALL_IF ${YOSYS_INSTALL_DRIVER}
)
yosys_link_components(yosys PRIVATE ${driver_components})
```

`yosys_cxx_executable` / `yosys_link_components` 是 Yosys 自己封装的 CMake 函数（定义在 `cmake/YosysComponent.cmake` 等），用来把指定的一组组件（`${driver_components}`，默认就是 `everything`）链接进 `yosys`。类似地，[CMakeLists.txt:444-455](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/CMakeLists.txt#L444-L455) 定义了库 `libyosys`（`BUILD_SHARED_LIBS` 决定是 SHARED 还是 STATIC）。

**⑦ ABC 子模块的处理**

ABC 是作为 git 子模块引入的。[CMakeLists.txt:348-367](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/CMakeLists.txt#L348-L367) 处理三种 ABC 来源：默认会从子模块**一起编译**出 `yosys-abc`；也可以用 `YOSYS_ABC_EXECUTABLE` 指向一个已存在的 ABC 可执行文件；特殊值 `INTEGRATED-NOTFOUND` 则把 ABC 作为进程内库链接进来。这也是为什么克隆仓库后要执行 `git submodule update --init`（见 [README.md:70-75](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/README.md#L70-L75)）。

#### 4.1.4 代码实践

**实践目标**：亲手完成一次配置 + 构建，并观察特性清单随开关变化。

**操作步骤**：

1. 确认子模块已拉取（首次构建需要）：
   ```bash
   git submodule update --init --recursive
   ```
2. 安装最小依赖（Ubuntu 示例，来自 [README.md:86-88](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/README.md#L86-L88)）：
   ```bash
   sudo apt-get install gawk git make python3 lld bison clang flex \
       libffi-dev libfl-dev libreadline-dev pkg-config tcl-dev zlib1g-dev
   ```
   注意 CMake 要求 ≥3.28（[installation.rst:91](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/docs/source/getting_started/installation.rst#L91)），Ubuntu 22.04 自带的版本可能不够，需要 `sudo snap install cmake --classic`。
3. 配置 + 构建（Release，官方推荐命令见 [README.md:140-142](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/README.md#L140-L142)）：
   ```bash
   cmake -B build . -DCMAKE_BUILD_TYPE=Release
   cmake --build build --config Release --parallel $(nproc)
   ```
4. 观察配置阶段输出的 `feature_summary`，记录启用了哪些特性。
5. 再配置一次，故意关掉 zlib，对比差异：
   ```bash
   cmake -B build2 . -DYOSYS_WITHOUT_ZLIB=ON
   ```

**需要观察的现象**：

- 第 3 步会打印一段 `The following features have been enabled / disabled`，其中 `with_zlib` 应为 enabled。
- 第 5 步的输出里 `with_zlib` 应变为 disabled。

**预期结果**：构建成功后，`build/yosys` 可执行文件生成；两次配置的 `feature_summary` 在 zlib 一行上不同。如果某依赖没装，对应特性会自动 disabled 而不会报错（除非是 REQUIRED 依赖如 FLEX/BISON）。

> 说明：本实践需要真实编译环境，具体编译时长与输出文本**待本地验证**。若仅做阅读型实践，可只执行到第 4 步的配置阶段（不 `--build`），同样能看到 `feature_summary`。

#### 4.1.5 小练习与答案

**练习 1**：如果你执行 `cmake -B build . -DYOSYS_WITHOUT_TCL=ON`，但系统里没装 tcl，`YOSYS_ENABLE_TCL` 会是 `ON` 还是 `OFF`？为什么？

> **答案**：`OFF`。根据 [CMakeLists.txt:311](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/CMakeLists.txt#L311)，`YOSYS_ENABLE_TCL = tcl_FOUND ∧ libtommath_FOUND ∧ ¬YOSYS_WITHOUT_TCL`。即使 `¬YOSYS_WITHOUT_TCL` 为真，`tcl_FOUND` 为假，整体仍为假。

**练习 2**：为什么 Yosys 要在 `CMakeLists.txt` 最开头就 `FATAL_ERROR` 禁止 in-tree 构建？

> **答案**：因为 in-tree 构建会把大量 `.o` 等中间产物混进源码目录，污染源码树、干扰版本管理和重新配置。强制 out-of-tree（用 `-B build`）让源码目录保持干净，构建产物可整体删除重建。

---

### 4.2 driver.cc 中的 main() 流程

#### 4.2.1 概念说明

`kernel/driver.cc` 里的 `main()` 是整个 `yosys` 程序的**唯一入口**——C++ 运行时启动程序后第一个调用的函数。它的职责不是去做综合本身，而是当一名「总调度」：

- 接收用户在命令行给出的意图（读什么文件、跑哪些 pass、写到哪里、要不要进交互 shell）；
- 把这些意图翻译成对运行时函数（`run_frontend` / `run_pass` / `run_backend` / `shell`）的一连串调用；
- 处理好生命周期（`yosys_setup` 开始、`yosys_shutdown` 结束）和统计信息。

理解了 `main()`，就理解了「一次 `yosys` 调用从开始到结束的全貌」。

#### 4.2.2 核心流程

`main()` 的执行可以划分成清晰的阶段（行号以 [driver.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc) 为准）：

```text
[1] 声明默认变量               L117-139
    frontend/backend 默认 "auto"，run_shell 默认 true
[2] 用 cxxopts 声明所有选项     L141-223
    分三组：operation / logging / developer
[3] 解析 argv                  L235-396
    特殊处理 -V / --git-hash 直接打印后 exit(0)
    把 -p / -s / -f / -b 等存入对应变量
[4] readline/历史、栈大小等杂项  L398-447
[5] yosys_setup()              L449   ← 运行时初始化（4.3 节详解）
[6] 加载插件 -m                L457-458
[7] 处理 -D 定义的 Verilog 宏  L462-467
[8] 读入前端文件               L476-479  run_frontend(...)
[9] 处理 -r 顶层               L481-482  run_pass("hierarchy -top ...")
[10] 执行脚本 -s/-c/-y         L483-530
[11] 执行 -p 命令              L532-533  run_pass(...)
[12] 进入 shell 或写后端       L535-547
     run_shell ? shell(...) : run_backend(...)
[13] design->check() 与收尾统计 L549-696
[14] yosys_shutdown()          L711
```

一个关键变量是 `run_shell`：它决定了第 12 步是「进入交互式 shell」还是「把设计写到 `-o` 指定的输出文件」。很多选项（`-S` / `-p` / `-s` / `-b` / `-o`）一旦出现，就把 `run_shell` 置为 `false`，意味着「用户已经给出了完整任务，跑完直接写结果退出，不要进交互模式」。

#### 4.2.3 源码精读

**① main 的签名与默认值**

[driver.cc:115-139](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc#L115-L139) 是 `main` 的开头，声明了一批默认状态（节选）：

```cpp
int main(int argc, char **argv)
{
    auto wall_clock_start = std::chrono::steady_clock::now();
    std::string frontend_command = "auto";
    std::string backend_command = "auto";
    std::vector<std::string> passes_commands;
    std::vector<std::string> frontend_files;
    ...
    bool run_shell = true;
```

`frontend_command` / `backend_command` 默认 `"auto"`，意味着「根据文件扩展名自动猜前端/后端」（这个猜测逻辑在 `run_frontend` / `run_backend` 里实现，见 4.2.3 末尾）。

**② cxxopts 声明三组选项**

[driver.cc:141-223](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc#L141-L223) 用 cxxopts 把选项分成三组。其中 `operation` 组定义了最常用的几个（节选自 [driver.cc:144-180](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc#L144-L180)）：

```cpp
options.add_options("operation")
    ("b,backend",    "...", cxxopts::value<std::string>(), "<backend>")
    ("f,frontend",   "...", cxxopts::value<std::string>(), "<frontend>")
    ("s,scriptfile", "...", cxxopts::value<std::string>(), "<scriptfile>")
    ("p,commands",   "...", cxxopts::value<std::vector<std::string>>(), "<commands>")
    ("S,synth",      "shortcut for calling the \"synth\" command ...")
    ("V,version",    "print version information and exit")
    ("infile",       "input files", cxxopts::value<std::vector<std::string>>())
;
```

`("b,backend", ...)` 表示「短选项 `-b`，长选项 `--backend`」；带 `cxxopts::value<...>()` 的说明该选项需要跟一个参数。最后的 `infile` 是位置参数（不带 `-` 的文件名），由下一行专门声明：

```cpp
options.parse_positional({"infile"});   // driver.cc:225
```

**③ 解析与「短路退出」选项**

[driver.cc:244](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc#L244) 执行真正的解析 `options.parse(argc, argv)`。随后对一些「只查询就退出」的选项做了短路处理，最典型的就是 `-V`（[driver.cc:252-255](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc#L252-L255)）：

```cpp
if (result.count("V")) {
    std::cout << yosys_version_str << std::endl;
    exit(0);
}
```

这就是 `./yosys -V` 打印版本号并立刻退出的全部秘密——它甚至不会走到 `yosys_setup()`，因为查版本不需要初始化整个综合框架。`yosys_version_str` 这个字符串是在构建时由 CMake 从 `kernel/version.cc.in` 模板生成的（[version.cc.in:2](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/version.cc.in#L2)）。

**④ `-S` 是 `synth` 的快捷方式**

[driver.cc:260-263](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc#L260-L263)：

```cpp
if (result.count("S")) {
    passes_commands.push_back("synth");
    run_shell = false;
}
```

也就是说命令行上的 `-S` 等价于在 pass 列表里加一条 `synth` 命令。这就解释了 `README` 里 `yosys -o output.blif -S input.v` 的用法：`-S` 把「跑一遍通用综合脚本」塞进待执行命令，`-o` 指定输出，于是 `run_shell=false`，结束后写 `.blif` 退出。

**⑤ 读前端文件 → 跑脚本 → 跑 -p 命令**

三段串行调用是 `main` 的核心执行区：

- 读输入文件：[driver.cc:476-479](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc#L476-L479)
  ```cpp
  for (auto it = frontend_files.begin(); it != frontend_files.end(); ++it) {
      if (run_frontend((*it).c_str(), frontend_command))
          run_shell = false;
  }
  ```
- 执行脚本文件（`.ys` / `.tcl` / `.py`）：[driver.cc:483-530](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc#L483-L530)
- 执行 `-p` 命令列表：[driver.cc:532-533](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc#L532-L533)
  ```cpp
  for (auto it = passes_commands.begin(); it != passes_commands.end(); it++)
      run_pass(*it);
  ```

**⑥ 收尾：shell 还是 backend，然后 shutdown**

[driver.cc:542-547](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc#L542-L547) 是分叉点：

```cpp
if (run_shell)
    shell(yosys_design);
else
    run_backend(output_filename, backend_command);
```

如果前面没有任何「给定了任务」的选项，`run_shell` 仍为 `true`，就进入交互式 shell（`yosys>` 提示符）；否则把当前设计写到 `output_filename`。最后 [driver.cc:711](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc#L711) 调用 `yosys_shutdown()` 释放资源，`return 0`。

**⑦ 补充：`auto` 前端/后端如何「猜」**

`run_frontend` 在 `command == "auto"` 时按扩展名决定前端（[yosys.cc:728-763](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys.cc#L728-L763)）：`.v`→verilog、`.sv`→SystemVerilog、`.json`→json、`.il`→rtlil、`.ys`→script 等。`run_backend` 同理（[yosys.cc:872-895](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys.cc#L872-L895)）：`.v`→verilog、`.json`→json、`.blif`→blif、`.aig`→aiger 等。这就是「不指定 `-f`/`-b` 也能用」的原因。

#### 4.2.4 代码实践

**实践目标**：对照 `driver.cc`，验证几条常见命令行分别走了哪段代码路径。

**操作步骤**：

1. 假设你已完成构建，可执行文件在 `build/yosys`。
2. 运行版本查询，对照 [driver.cc:252-255](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc#L252-L255)：
   ```bash
   ./build/yosys -V
   ```
3. 运行一条 `-p` 命令（不进 shell），对照 [driver.cc:532-533](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc#L532-L533)：
   ```bash
   ./build/yosys -p "help"
   ```
4. 直接敲一个位置参数文件，对照 [driver.cc:476-479](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc#L476-L479)：
   ```bash
   ./build/yosys examples/cmos/counter.v
   ```
5. 不带任何参数直接启动，对照 [driver.cc:543-544](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc#L543-L544)：
   ```bash
   ./build/yosys
   ```

**需要观察的现象**：

- 第 2 步只打印一行版本号就退出，**不会**出现 banner 和 `yosys>` 提示符——因为它在 `yosys_setup()` 之前就 `exit(0)` 了。
- 第 3 步打印帮助后会直接退出（因为 `-p` 使 `run_shell=false`）。
- 第 4 步会先打印 banner、用 verilog 前端读入文件，然后**进入** `yosys>` 交互 shell（因为只读了文件、没给 `-o`/`-p`/`-S`，`run_shell` 仍为 true）。
- 第 5 步直接进入 `yosys>` 交互 shell。

**预期结果**：四条命令的行为差异，能用「`run_shell` 是否被置 false」与「是否走了 `run_frontend`」这两个变量完整解释。具体输出文本**待本地验证**。

> 说明：如果你目前没有可执行文件，可改为「阅读型实践」——只读 [driver.cc:115-547](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc#L115-L547)，在纸上为每条命令标出它命中的代码行。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `./yosys -V` 不会进入交互 shell，也不会执行 `yosys_setup()`？

> **答案**：因为 [driver.cc:252-255](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc#L252-L255) 在解析到 `-V` 后直接 `std::cout << yosys_version_str` 然后 `exit(0)`，这条路径根本不会走到 [driver.cc:449](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc#L449) 的 `yosys_setup()`。

**练习 2**：`./yosys -o out.json in.v` 这条命令里，`run_shell` 最终是 true 还是 false？设计会被怎么处理？

> **答案**：`false`。`-o` 触发 [driver.cc:286-289](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc#L286-L289) 把 `run_shell` 置 false。于是 `in.v` 被 `run_frontend` 读入后，最终走 [driver.cc:546](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc#L546) 的 `run_backend("out.json", "auto")`，`.json` 扩展名被自动猜成 json 后端，写出网表。

**练习 3**：`run_frontend` 在 `command == "auto"` 时，对 `counter.ys` 文件会选用哪个前端？

> **答案**：`script`。见 [yosys.cc:754-755](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys.cc#L754-L755)，`.ys` 后缀映射到 `"script"`，即把该文件当作 yosys 脚本逐行执行。

---

### 4.3 yosys_setup() 初始化

#### 4.3.1 概念说明

前面看到，`main()` 在解析完参数、准备好日志后，会调用 `yosys_setup()`（[driver.cc:449](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc#L449)）。这个函数做的是「**一次性地把综合运行时建立起来**」的工作。它之所以单独抽成一个函数（而不是写在 `main` 里），是因为 Yosys 还可以被当作**库**嵌入到别的程序里（`libyosys`），那种场景下没有 `main`，但同样需要调用 `yosys_setup()` 来初始化（详见 u9-l2 的 C++ API 讲义）。

`yosys_setup()` 主要做四件事：

1. 预填一些全局数据结构（如 `IdString` 的内部化表）；
2. （可选）初始化 Python 解释器；
3. **注册所有内置 pass**——这是最关键的一步；
4. 创建代表「当前设计」的全局对象 `yosys_design`（一个 `RTLIL::Design`）。

与之对称的 `yosys_shutdown()` 负责收尾：注销 pass、销毁 `yosys_design`、关闭日志、终结 Python/TCL。两者构成一对「括号」，把整个综合运行时的生命周期包起来。

#### 4.3.2 核心流程

`yosys_setup()` 的流程（行号见 [yosys.cc:236-268](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys.cc#L236-L268)）：

```text
1. 幂等保护：若 already_setup 则直接返回
2. IdString::ensure_prepopulated()     预填命名表
3. (若启用 Python) 初始化 Python 解释器
4. init_share_dirname()                定位 share 数据目录
5. init_abc_executable_name()          定位 ABC 可执行文件
6. Pass::init_register()               ★ 遍历链表，登记所有 pass
7. yosys_design = new RTLIL::Design    ★ 创建「当前设计」容器
8. 初始化静态单元类型表
9. log_push()                          压入一层日志
```

其中第 6 步 `Pass::init_register()` 是重点。前面 2.4 节提过「pass 靠全局对象构造函数自动登记」的机制，这里给出完整链条：

```text
程序启动
  └─ 各 .cc 里形如 struct FooPass : Pass {...} FooPass; 的全局对象
     其构造函数把自己「头插」进全局链表 first_queued_pass
        （register.cc:67-68）
  └─ main 调用 yosys_setup()
     └─ Pass::init_register()        （register.cc:80-90）
        ├─ 遍历 first_queued_pass 链表
        ├─ 对每个节点调 run_register()
        │    └─ pass_register[pass_name] = this   （register.cc:77）
        └─ 全部登记完后，对每个调 on_register()
```

这样设计的好处是：**写新 pass 时不需要修改任何中心注册代码**——只要定义一个全局静态实例，它就会自动出现在 `pass_register` 里，从而能被 `help` 列出、被脚本调用。这也是为什么 [installation.rst:330-331](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/docs/source/getting_started/installation.rst#L330-L331) 说「The Yosys kernel automatically detects all commands linked with Yosys. So it is not needed to add additional commands to a central list」。

#### 4.3.3 源码精读

**① yosys_setup 主体**

[yosys.cc:236-268](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys.cc#L236-L268)（节选）：

```cpp
void yosys_setup()
{
    if(already_setup) return;          // 幂等：重复调用安全
    already_setup = true;
    already_shutdown = false;

    IdString::ensure_prepopulated();
    ...
    init_share_dirname();
    init_abc_executable_name();

    Pass::init_register();             // ★ 注册所有 pass
    yosys_design = new RTLIL::Design;  // ★ 当前设计容器
    yosys_celltypes.static_cell_types = StaticCellTypes::categories.is_known;
    log_push();
}
```

几个要点：

- `already_setup` 幂等保护意味着重复调用 `yosys_setup()` 是安全的——这对「库被多次初始化」的场景很重要。
- `IdString` 是 RTLIL 的命名类型（详见 u3-l3），`ensure_prepopulated()` 预先把一批常用名字（如 `\clk`、端口名）内部化，加快后续速度。
- `yosys_design` 是一个**全局指针**，声明在 [yosys.h:64](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys.h#L64)（`extern RTLIL::Design *yosys_design;`），初始为 `NULL`（[yosys.cc:94](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys.cc#L94)），在这里被 `new` 出来。**整个 yosys 运行期间操作的「设计」就是这一个对象**，所有前端往里塞模块、所有 pass 修改它、所有后端从它读出。

**② pass 是如何进链表的**

每个 pass 的构造函数（[register.cc:64-71](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L64-L71)）把自己头插进全局链表：

```cpp
Pass::Pass(std::string name, std::string short_help, source_location location) :
    pass_name(name), short_help(short_help), location(location)
{
    next_queued_pass = first_queued_pass;   // 指向旧的表头
    first_queued_pass = this;               // 自己成为新表头
    call_counter = 0;
    runtime_ns = 0;
}
```

`first_queued_pass` 是个全局指针（[register.cc:38](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L38)）。所有 `struct XxxPass : Pass { ... } XxxPass;` 形式的全局对象在 `main` 之前就执行了这个构造，因此到 `yosys_setup()` 时，链表里已经挂好了全部内置 pass。

**③ init_register 把链表灌进 pass_register**

[register.cc:80-90](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L80-L90)：

```cpp
void Pass::init_register()
{
    vector<Pass*> added_passes;
    while (first_queued_pass) {
        added_passes.push_back(first_queued_pass);
        first_queued_pass->run_register();              // 插入 pass_register
        first_queued_pass = first_queued_pass->next_queued_pass;
    }
    for (auto added_pass : added_passes)
        added_pass->on_register();                       // 登记完毕的回调
}
```

`run_register()`（[register.cc:73-78](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L73-L78)）就是把 `this` 按 `pass_name` 存进全局 `std::map<std::string, Pass*> pass_register`（[register.cc:42](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L42)）。从此之后，任何 `run_pass("opt")` 都会在这张表里按名字 `"opt"` 找到对应的 `Pass*` 并执行它的 `execute()`。

**④ yosys_shutdown 对称收尾**

[yosys.cc:275-319](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys.cc#L275-L319)（节选）：

```cpp
void yosys_shutdown()
{
    if(already_shutdown) return;
    already_setup = false;
    already_shutdown = true;
    log_pop();

    Pass::done_register();          // 注销所有 pass
    delete yosys_design;            // 销毁当前设计
    yosys_design = NULL;
    RTLIL::OwningIdString::collect_garbage();
    ...                             // 关闭日志、终结 TCL/Python
}
```

`Pass::done_register()`（[register.cc:92-101](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L92-L101)）对每个 pass 调 `on_shutdown()`，然后清空 `frontend_register / pass_register / backend_register` 三张表。`delete yosys_design` 释放设计占用的全部 RTLIL 内存。这与 `setup` 完全对称。

> 小结：`setup` 与 `shutdown` 之间的这段区间，就是「综合运行时活着」的窗口。`driver.cc` 的所有 `run_frontend / run_pass / run_backend / shell` 都必须在这个窗口内调用。

#### 4.3.4 代码实践

**实践目标**：验证 pass 注册机制——确认内置 pass 确实被自动登记进了 `pass_register`，并理解它为何「无需中心登记」。

**操作步骤（阅读 + 运行结合）**：

1. 在源码里随便挑一个简单 pass，比如 `passes/opt/opt_dff.cc`（[installation.rst:334-335](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/docs/source/getting_started/installation.rst#L334-L335) 推荐它作为入门阅读）。找到它末尾形如 `struct OptDffPass : public Pass { ... } OptDffPass;` 的全局实例（**示例代码定位**：具体行号可在本地用编辑器搜索 `OptDffPass`）。
2. 对照 [register.cc:64-71](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L64-L71) 理解：这个全局对象的构造函数会把 `opt_dff` 这个名字挂进 `first_queued_pass` 链表。
3. 运行 yosys，用 `help` 列出所有命令，确认 `opt_dff` 在列表里：
   ```bash
   ./build/yosys -p "help"
   ```
4. 进一步确认它确实在 `pass_register` 里能按名字调用：
   ```bash
   ./build/yosys -p "read_verilog examples/cmos/counter.v; opt_dff -h"
   ```

**需要观察的现象**：

- 第 3 步输出里能找到 `opt_dff` 这一条，说明它被自动登记了——尽管你从没在任何「中心列表」里写过它。
- 第 4 步能打印 `opt_dff` 的帮助，说明按名字从 `pass_register` 查表成功。

**预期结果**：自动注册机制成立。`help` 列表里出现的命令数量，就等于「在源码里定义了全局 Pass 实例」的数量。具体命令清单**待本地验证**。

> 说明：若无法构建，可纯阅读 [register.cc:38-101](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L38-L101)，把「链表头插 → init_register 遍历 → run_register 入表 → done_register 清空」这条链在纸上画一遍。

#### 4.3.5 小练习与答案

**练习 1**：`yosys_setup()` 里的 `if(already_setup) return;` 起什么作用？去掉会有什么问题？

> **答案**：幂等保护，保证 `yosys_setup()` 被调用多次也只真正初始化一次。如果去掉，重复调用会再次 `Pass::init_register()`（但链表此时已空，无害）并 `new` 出第二个 `yosys::Design`，造成内存泄漏和全局指针错乱——这对「把 yosys 当库嵌入、宿主程序可能多次初始化」的场景尤其危险。

**练习 2**：假设你新写了一个 pass `MyPass`，定义了全局实例 `MyPass my_pass;`。你需要修改 `register.cc` 把它加进某张表吗？

> **答案**：不需要。因为 `my_pass` 的构造函数（[register.cc:64-71](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L64-L71)）会自动把它挂进 `first_queued_pass`，之后 `yosys_setup()` 里的 `Pass::init_register()`（[register.cc:80-90](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/register.cc#L80-L90)）会自动把它登记进 `pass_register`。只要这个 `.cc` 被编译链接进 `yosys` 即可。

**练习 3**：`yosys_design` 在 `yosys_setup()` 之前是什么值？为什么 `run_pass` 必须在 `setup` 之后才能调用？

> **答案**：之前是 `NULL`（[yosys.cc:94](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys.cc#L94)）。`run_pass` 默认作用于 `yosys_design`（[yosys.cc:859-860](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys.cc#L859-L860)），而 `yosys_design` 直到 `setup` 里 `new RTLIL::Design`（[yosys.cc:265](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys.cc#L265)）才有效，且 pass 也只在 `init_register` 后才在 `pass_register` 里，故必须在 `setup` 之后调用。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「**构建 → 运行 → 解释**」的端到端任务。

**任务**：构建 Yosys，运行一条最简单的命令，并用本讲学到的源码知识解释它内部走过的每一步。

**操作步骤**：

1. **配置 + 构建**（若尚未完成）：
   ```bash
   cmake -B build . -DCMAKE_BUILD_TYPE=Release
   cmake --build build --config Release --parallel $(nproc)
   ```
   对照 4.1 节，理解这两行分别触发了 `CMakeLists.txt` 的哪部分（配置阶段生成 Makefile、构建阶段调用编译器产出 `build/yosys`）。

2. **打印版本号**：
   ```bash
   ./build/yosys -V
   ```
   对照 [driver.cc:252-255](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc#L252-L255)，说明为什么这一步**没有**经过 `yosys_setup()`。

3. **跑一条 `-S` 综合并写出网表**（输入用仓库自带的示例）：
   ```bash
   ./build/yosys -o /tmp/counter.json -S examples/cmos/counter.v
   ```
   然后逐阶段解释这条命令在 `main()` 里的路径：
   - `examples/cmos/counter.v` 是位置参数 `infile`（[driver.cc:225](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc#L225)），被 `run_frontend` 以 `auto`→`verilog` 前端读入（[yosys.cc:742-743](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys.cc#L742-L743)）。
   - `-S` 把 `synth` 塞进 `passes_commands`（[driver.cc:260-263](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc#L260-L263)），随后被 `run_pass` 执行（[driver.cc:532-533](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc#L532-L533)）。
   - `-o /tmp/counter.json` 使 `run_shell=false`，最终 `run_backend` 把 `.json` 猜成 json 后端写出（[driver.cc:546](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc#L546) + [yosys.cc:887-888](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys.cc#L887-L888)）。
   - 全程的 pass 调用都建立在 `yosys_setup()` 已创建的 `yosys_design` 和已注册的 `pass_register` 之上（[yosys.cc:264-265](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys.cc#L264-L265)）。

**需要观察的现象 / 预期结果**：

- 第 2 步只输出版本号一行。
- 第 3 步会打印 banner、各 pass 的执行日志，最后在 `/tmp/counter.json` 生成网表文件（可用 `cat /tmp/counter.json | head` 查看，**待本地验证**具体内容）。
- 你能用本讲的三个模块，把「从敲下命令到生成文件」的每一步对应到具体源码行。

> 这是贯穿本讲的综合任务：**构建（4.1）让程序存在，main 调度（4.2）让命令流动，setup 初始化（4.3）让运行时就绪**。三者缺一不可。

## 6. 本讲小结

- Yosys 用 **CMake（≥3.28，C++20）** 构建，**强制 out-of-tree**（`cmake -B build .`）；通过 `YOSYS_WITHOUT_*` / `YOSYS_WITH_*` 选项与 `condition()` 把「用户意愿 ∧ 依赖探测」合并为 `YOSYS_ENABLE_*` 编译宏，最终产出 `yosys` 可执行与 `libyosys` 库两个目标。
- 程序入口是 [driver.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/driver.cc) 的 `main()`，它用 **cxxopts** 解析命令行，按「读前端 → 跑脚本 → 跑 `-p` 命令 → 进 shell 或写后端」的顺序调度；关键变量 `run_shell` 决定结尾是交互还是写文件。
- `-V` 等纯查询选项会在 `yosys_setup()` **之前**就 `exit(0)`；`-S` 是 `synth` 的快捷方式；`auto` 前端/后端按文件扩展名自动猜测。
- `yosys_setup()` 一次性建立运行时：预填 `IdString`、注册所有 pass（`Pass::init_register`）、创建全局 `yosys_design`；`yosys_shutdown()` 对称销毁。
- pass 采用**全局静态实例 + 链表**的自动注册机制：构造函数头插 `first_queued_pass`，`init_register` 遍历入 `pass_register` 表——所以新增 pass 无需修改任何中心注册代码。
- 所有 `run_frontend / run_pass / run_backend / shell` 都必须在 `setup` 与 `shutdown` 这个运行时窗口内调用。

## 7. 下一步学习建议

本讲让你拿到了一个能跑的 `yosys`，并理解了它的「骨架」（构建 + 入口 + 初始化）。接下来建议：

1. **u1-l3《顶层目录结构地图》**：趁热打铁，把 `kernel / frontends / passes / backends / techlibs` 这些目录的职责对上号，建立全局源码地图。
2. **u1-l4《第一次综合》**：真正进 `yosys>` 交互 shell，跑一遍 `read / hierarchy / proc / opt / techmap / write_verilog`，把本讲讲的「调度」变成手感。
3. 之后进入 **u2 RTLIL 内部表示入门**：本讲多次提到的 `yosys_design`、`RTLIL::Design`、pass 修改的「设计」到底是什么，将在那里展开。

如果想提前了解「把 yosys 当库嵌入」的用法（`yosys_setup` 之所以被独立成函数的原因），可以直接跳到 **u9-l2《C++ API》**，但建议先完成 u2–u6 打好 RTLIL 与 pass 的基础。
