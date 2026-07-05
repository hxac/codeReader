# 仓库目录结构地图

## 1. 本讲目标

学完本讲后，你应当能够：

- 把仓库里每个顶层目录与它所属的子系统（编译器、C++ 运行时、Python 前端、Realtime）对应起来。
- 区分四类代码的查找位置：头文件（include）、实现（lib）、工具（tools）、测试（test）。
- 在不读细节的前提下，快速定位「示例在哪里」「测试在哪里」「某个模拟器后端在哪里」「某个编译 Pass 在哪里」。
- 读懂根目录 `CMakeLists.txt` 是如何用 `add_subdirectory` 把四大子系统拼装成一个完整项目的。

本讲是后续所有讲义的「地图」。后续讲义讲到某个文件时，你都可以回到这张地图，确认它落在哪个子系统、哪一层。

## 2. 前置知识

阅读本讲前，请确认你已经学完 [u1-l1（项目定位与架构总览）](u1-l1-project-overview.md)。本讲会直接沿用 u1-l1 建立的几个结论：

- CUDA-Q 由四大子系统组成：`cudaq/`（基于 MLIR 的编译器）、`runtime/`（C++ 运行时与 nvqir 模拟器，是公共底盘）、`python/`（Python 前端）、`realtime/`（FPGA/GPU 紧耦合实时控制）。
- 关键名词：**Quake**（量子方言）、**CC**（经典计算方言）、**QIR**（量子中间表示，最终 lowering 目标）、**nvq++**（编排编译流程的 bash 驱动脚本）。
- 后端在**链接期**切换，源码不写死具体模拟器。

此外需要一点背景：

- **CMake**：C/C++ 项目的构建系统。`CMakeLists.txt` 是它的配置文件，`add_subdirectory(xxx)` 表示「把 `xxx` 子目录也纳入构建」。
- **头文件 vs 实现**：C++ 习惯把声明放在 `include/`（`.h`），把定义放在 `lib/`（`.cpp`）。
- **TableGen（`.td`）**：MLIR/LLVM 用来声明方言操作、Pass 的领域特定语言，构建时会被翻译成 C++ 代码。看到 `.td` 文件，就理解为「这是声明文件」。

> 阅读建议：本讲涉及的目录很多，第一次读不必逐个记。重点是建立「四象限」直觉——遇到任何文件，先判断它属于编译器、运行时、Python 前端还是 Realtime。

## 3. 本讲源码地图

本讲主要围绕仓库的目录组织展开，重点参考以下文件（它们本身就是「目录的说明」）：

| 文件 | 作用 |
| --- | --- |
| `Overview.md` | 官方架构总览，包含一节 **Code Map**，逐目录解释编译器各文件夹职责。 |
| `CMakeLists.txt` | 根构建文件，用 `add_subdirectory` 决定哪些子系统参与构建。是判断「目录 ↔ 子系统」归属的权威依据。 |
| `cudaq/`、`runtime/`、`python/`、`realtime/` | 四大子系统的根目录，本讲逐一展开。 |
| `docs/sphinx/examples/`、`unittests/`、`targettests/`、`cudaq/test/` | 示例与三类测试，本讲说明它们的分工。 |

## 4. 核心概念与源码讲解

### 4.1 顶层鸟瞰：四大子系统的拼装方式

#### 4.1.1 概念说明

CUDA-Q 是一个**多语言、多子项目**的大型仓库。最容易出现的情况是：你打开一个文件，却不知道它属于哪一层、被谁调用、和谁在同一个子系统里。

为了避免这种迷失，我们先建立一个最重要的判断框架——**根 `CMakeLists.txt` 用 `add_subdirectory` 决定了项目骨架**。每一条 `add_subdirectory(xxx)` 都对应一个相对独立的构建模块。看懂这几行，就等于看懂了仓库的「目录 → 子系统」归属表。

#### 4.1.2 核心流程

仓库根目录下的产物可以分成五组：

1. **四大子系统源码**：`cudaq/`、`runtime/`、`python/`、`realtime/`。
2. **示例与文档**：`docs/`（含 `sphinx/examples`），以及指向它的软链接 `examples`。
3. **测试**：`unittests/`、`targettests/`、`cudaq/test/`。
4. **第三方依赖**：`tpls/`（vendored，即随仓库一起分发的第三方库源码）。
5. **构建与脚本**：`CMakeLists.txt`、`cmake/`、`scripts/`、`docker/`、`utils/`、`pyproject.toml*`。

构建时，根 `CMakeLists.txt` 会按需把这些子目录挂进构建树，关键逻辑如下（伪代码）：

```text
如果启用 cudaq 项目   → 挂载 cudaq/
如果启用 realtime 项目 → 挂载 realtime/
如果启用 runtime 项目 → 挂载 runtime/
始终挂载 utils/
如果启用 python 项目  → 先找 nanobind，再挂载 python/
如果开启 CUDAQ_BUILD_TESTS
    → 挂载 targettests/、unittests/、cudaq/unittests/、cudaq/test/Unit/、docs/
```

也就是说，`cudaq / runtime / realtime / python` 这四个子系统是**可按需开关**的（通过缓存变量 `CUDAQ_ENABLE_PROJECTS` 控制），而测试相关目录受 `CUDAQ_BUILD_TESTS` 控制。这是理解为什么有些目录「存在却不一定参与构建」的关键。

#### 4.1.3 源码精读

根 `CMakeLists.txt` 的目录挂载段落，是本讲的「总钥匙」：

```cmake
if("cudaq" IN_LIST CUDAQ_ENABLE_PROJECTS)
  add_subdirectory(cudaq)
endif()
if("realtime" IN_LIST CUDAQ_ENABLE_PROJECTS)
  add_subdirectory(realtime)
endif()
if("runtime" IN_LIST CUDAQ_ENABLE_PROJECTS)
  add_subdirectory(runtime)
endif()
add_subdirectory(utils)
```

这段代码把 `cudaq`、`realtime`、`runtime` 三个子系统挂进构建，且都受 `CUDAQ_ENABLE_PROJECTS` 列表控制（[CMakeLists.txt:916-925](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/CMakeLists.txt#L916-L925)）。其中 `utils/` 无条件挂载，是公共辅助工具。

Python 前端的挂载略复杂，因为它依赖 nanobind（Python 绑定生成器），所以先 `find_package(nanobind)` 再挂载 `python/`（[CMakeLists.txt:927-943](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/CMakeLists.txt#L927-L943)）。

测试目录则集中挂载，并整体受 `CUDAQ_BUILD_TESTS` 与运行时未禁用的双重保护（[CMakeLists.txt:968-1005](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/CMakeLists.txt#L968-L1005)）：

```cmake
if(CUDAQ_BUILD_TESTS AND NOT CUDAQ_DISABLE_RUNTIME)
    add_subdirectory(targettests)
    add_subdirectory(unittests)
    add_subdirectory(cudaq/unittests)
    add_subdirectory(cudaq/test/Unit)
    add_subdirectory(docs)
endif()
```

> 注意：`cudaq/test/`（lit/FileCheck 测试，详见 4.5）的大多数子目录由 `cudaq/` 自己的 CMake 挂载，只有其中的 `Unit` 子目录在这里被根 CMake 单独挂载。这是「测试目录归属」一个容易混淆的点，记住「`cudaq/test/` 整体属于编译器子系统」即可。

#### 4.1.4 代码实践

**实践目标**：把根 `CMakeLists.txt` 的目录挂载行为固化为一张「目录归属速查表」。

**操作步骤**：

1. 打开根 `CMakeLists.txt`，定位到约第 910–1005 行。
2. 列出所有 `add_subdirectory(...)` 调用及其外层的 `if` 守卫条件。
3. 用一张三列表格记录：`目录`、`所属子系统`、`守卫条件`。

**需要观察的现象**：哪些目录是无条件挂载的？哪些受 `CUDAQ_ENABLE_PROJECTS` 控制？哪些受 `CUDAQ_BUILD_TESTS` 控制？

**预期结果**（参考答案）：

| 目录 | 所属子系统 | 守卫条件 |
| --- | --- | --- |
| `utils/` | 公共辅助 | 无条件 |
| `cudaq/` | 编译器 | `CUDAQ_ENABLE_PROJECTS` 含 `cudaq` |
| `runtime/` | C++ 运行时 + nvqir | `CUDAQ_ENABLE_PROJECTS` 含 `runtime` |
| `realtime/` | Realtime 子系统 | `CUDAQ_ENABLE_PROJECTS` 含 `realtime` |
| `python/` | Python 前端 | `CUDAQ_ENABLE_PROJECTS` 含 `python` + nanobind |
| `targettests/`、`unittests/` 等 | 测试 | `CUDAQ_BUILD_TESTS` 且未禁用运行时 |

#### 4.1.5 小练习与答案

**练习 1**：为什么 `examples` 是一个软链接，而不是真实目录？它指向哪里？

> **答案**：`examples` 指向 `docs/sphinx/examples`（可由 `readlink examples` 验证）。这样做的目的是让仓库根目录也能直接访问示例，同时保持示例源文件只在 `docs/sphinx/examples` 下维护一份，避免双份维护。

**练习 2**：如果你只关心编译器、不需要 Python 前端，构建时该如何设置？

> **答案**：通过缓存变量 `CUDAQ_ENABLE_PROJECTS` 只列出需要的项目（如 `cudaq;runtime`），不包含 `python`，从而跳过 nanobind 与 `python/` 的构建。

---

### 4.2 `cudaq/`：编译器目录树（MLIR / Quake / nvq++）

#### 4.2.1 概念说明

`cudaq/` 是**编译器子系统**，它是一个标准的 MLIR 编译器项目。如果你看过 LLVM/MLIR 项目的目录布局，会觉得非常眼熟——它严格遵守「头文件在 `include/`、实现在 `lib/`、可执行工具在 `tools/`、回归测试在 `test/`」的 C++ 四层约定。

这一层把 C++ 源码翻译成 QIR，是整个 CUDA-Q 最庞大、最复杂的一块。

#### 4.2.2 核心流程

`cudaq/` 内部按照「编译流水线的各阶段」组织：

```text
cudaq/
├── include/cudaq/      # 头文件（声明层）
│   ├── Frontend/nvqpp/ #   AST Bridge 头：C++ AST → Quake 的入口
│   ├── Optimizer/      #   方言与 Pass 的声明（Quake/CC/QEC + CodeGen/Transforms）
│   ├── Target/         #   Quake → OpenQASM 等导出的声明
│   ├── ADT/、Support/、Verifier/  # 公共数据结构、工具、校验器
├── lib/                # 实现层
│   ├── Frontend/nvqpp/ #   AST Bridge 实现：Convert{Expr,Stmt,Decl}.cpp
│   ├── Optimizer/      #   方言实现 + 所有 Pass
│   │   ├── Dialect/    #     Quake/CC 方言实现
│   │   ├── Transforms/ #     ★ 所有变换 Pass（68 个 .cpp）
│   │   ├── CodeGen/    #     lowering 到 QIR/LLVM 的 Pass
│   │   ├── Conversion/ #     方言间转换
│   │   └── Analysis/、Builder/、CAPI/
│   └── Target/         #   导出 OpenQASM/IQM JSON 等的实现
├── tools/              # 可执行工具（编译流水线的「积木」）
│   ├── cudaq-quake/    #   C++ AST → Quake MLIR
│   ├── cudaq-opt/      #   对 Quake 跑指定 Pass 流水线
│   ├── cudaq-translate/#   Quake → QIR
│   ├── nvqpp/          #   ★ nvq++ 驱动脚本（bash）
│   ├── cudaq-target-conf/、cudaq-lsp-server/、fixup-linkage/
├── test/               # lit/FileCheck 回归测试（编译器测试）
└── unittests/          # C++ 单元测试
```

#### 4.2.3 源码精读

`Overview.md` 的 **Code Map** 一节正是对 `cudaq/`（以及运行时）目录的官方解释，建议与上面的树形图对照阅读（[Overview.md:42-67](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/Overview.md#L42-L67)）。例如它对 Frontend 头文件的描述：

> `include/cudaq/Frontend/nvqpp`：This folder contains the header files for the AST Bridge. The AST Bridge is responsible for mapping C++ to Quake MLIR via the Clang abstract syntax tree.

实际目录里，该头文件确实是 [cudaq/include/cudaq/Frontend/nvqpp/ASTBridge.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/include/cudaq/Frontend/nvqpp/ASTBridge.h)。

方言的声明采用 TableGen，集中在 `Optimizer/Dialect/` 下，分为 Quake、CC、QEC、Common 四组（[cudaq/include/cudaq/Optimizer/Dialect/](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/include/cudaq/Optimizer/Dialect/)）。其中最核心的两个是：

- Quake 方言操作定义：[cudaq/include/cudaq/Optimizer/Dialect/Quake/QuakeOps.td](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/include/cudaq/Optimizer/Dialect/Quake/QuakeOps.td)
- CC 方言操作定义：[cudaq/include/cudaq/Optimizer/Dialect/CC/CCOps.td](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/include/cudaq/Optimizer/Dialect/CC/CCOps.td)

变换 Pass 的实现全部集中在 `lib/Optimizer/Transforms/`，这是一个**重要的导航锚点**——后续讲义讲到某个具体 Pass（如 `LambdaLifting`、`ArgumentSynthesis`、`GenKernelExecution`）时，你都来这里找同名 `.cpp`。代表性文件（[cudaq/lib/Optimizer/Transforms/](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/lib/Optimizer/Transforms/)）：

- [cudaq/lib/Optimizer/Transforms/LambdaLifting.cpp](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/lib/Optimizer/Transforms/LambdaLifting.cpp)
- [cudaq/lib/Optimizer/Transforms/ArgumentSynthesis.cpp](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/lib/Optimizer/Transforms/ArgumentSynthesis.cpp)
- [cudaq/lib/Optimizer/Transforms/GenKernelExecution.cpp](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/lib/Optimizer/Transforms/GenKernelExecution.cpp)

`tools/` 里最重要的是 `nvqpp/`，它里面是 `nvq++.in`——一个 **bash 脚本模板**，负责把 `cudaq-quake`、`cudaq-opt`、`cudaq-translate` 和链接器串成完整的编译流程（详见讲义 u4-l5）。

#### 4.2.4 代码实践

> ⚠️ 本小节的「统计 Pass 数量」就是本讲的主实践任务，详细步骤见第 5 节综合实践。这里先给出一个小预热。

**实践目标**：确认编译器工具链有几个可执行工具，并理解它们在流水线中的先后顺序。

**操作步骤**：

1. 列出 `cudaq/tools/` 下所有子目录。
2. 对照 `Overview.md` 第 125–159 行（`tools/cudaq-quake`、`tools/cudaq-opt`、`tools/quake-translate`、`tools/nvqpp` 四段）了解每个工具的职责。

**需要观察的现象**：四个核心工具分别处理编译流水线的哪一段？

**预期结果**：`cudaq-quake`（AST→Quake）→ `cudaq-opt`（跑 Pass 优化 Quake）→ `cudaq-translate`（Quake→QIR），而 `nvq++` 是编排前三者＋链接的外层脚本。

#### 4.2.5 小练习与答案

**练习 1**：如果你想阅读「Quake 方言里到底定义了哪些量子操作」，应该打开哪个文件？

> **答案**：`cudaq/include/cudaq/Optimizer/Dialect/Quake/QuakeOps.td`。`.td` 是 TableGen 声明文件，列出了所有 Quake 操作的定义。

**练习 2**：`lib/Optimizer/Transforms/`、`lib/Optimizer/CodeGen/`、`lib/Optimizer/Conversion/` 三者都含「对 IR 做变换」的代码，它们的分工区别是什么？

> **答案**：`Transforms` 是**同一方言内部**的优化变换（如化简、内联、提升）；`Conversion` 是**方言之间**的转换（如 CC→LLVM）；`CodeGen` 是把高层表示**降低到目标代码**（Quake/CC→LLVM IR / QIR）。三者对应 MLIR 的三类 Pass 机制。

---

### 4.3 `runtime/`：C++ 运行时与 nvqir 模拟器目录树

#### 4.3.1 概念说明

`runtime/` 是**公共底盘**：无论你用 C++ 还是 Python 写内核，最终都落到这一层执行。它包含三块：

- `runtime/common/`：`cudaq-common` 公共库，提供跨子系统的通用类型（采样结果、执行上下文、噪声模型、日志、远程 REST 客户端等）。
- `runtime/cudaq/`：CUDA-Q 规范的 C++ 实现——量子类型、门、算法原语（`sample`/`observe`/`evolve`）、平台抽象。
- `runtime/nvqir/`：`libnvqir`，实现 QIR 规范，并委托给可扩展的 `CircuitSimulator` API——所有模拟器后端都在这里。

#### 4.3.2 核心流程

```text
runtime/
├── common/          # cudaq-common：ExecutionContext、SampleResult/MeasureCounts、
│                    #   ObserveResult、NoiseModel、logger、BaseRemoteRESTQPU、ServerHelpers…
├── cudaq/           # CUDA-Q 规范的 C++ 运行时
│   ├── *.h          #   顶层算法头：algorithm.h、optimizers.h、gradients.h、
│   │                #     spin_op.h、operators.h、builder.h、platform.h、schedule.h…
│   ├── qis/         #   ★ 量子指令集：qubit/qvector/qspan 类型 + 门 + execution_manager
│   ├── algorithms/  #   sample/observe/get_state/evolve 等算法实现
│   ├── operators/   #   spin/fermion/boson/matrix 算符实现
│   ├── platform/    #   ★ quantum_platform 实现：default/、mqpu/、fermioniq/、orca/、pasqal/、quera/
│   ├── builder/     #   C++ kernel_builder（动态构造内核）
│   ├── distributed/ #   MPI 插件等分布式支持
│   ├── domains/、dynamics_integrators.h、realtime/、kernels/、utils/、analysis/、driver/
│   └── cudaq.cpp    #   运行时入口实现
├── nvqir/           # ★ libnvqir：模拟器后端
│   ├── CircuitSimulator.h   #   可扩展模拟器基类（核心抽象）
│   ├── NVQIR.cpp            #   QIR QIS 实现入口 + 注册宏
│   ├── Gates.h              #   门的原语定义
│   ├── qpp/                 #   CPU 状态向量后端
│   ├── custatevec/          #   GPU 状态向量后端（cuQuantum）
│   ├── cutensornet/         #   张量网络 / MPS 后端
│   ├── cudensitymat/        #   密度矩阵动力学后端
│   ├── dem/                 #   密度矩阵后端
│   ├── stim/                #   刺激采样后端
│   └── resourcecounter/     #   门计数后端（调试用）
├── logger/          # 日志实现
├── cudaq.h、include/、internal/、test/
```

`Overview.md` 对 `runtime/common`、`runtime/nvqir`、`runtime/cudaq` 三块都有官方说明（[Overview.md:89-124](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/Overview.md#L89-L124)）。一个关键结论原文如下：

> Switching the backend simulation capability is a link-time task that is handled implicitly by `nvq++`.

即**切换模拟器后端是「链接期」任务**——这就是为什么 `nvqir/` 下每个后端（qpp/custatevec/...）都是独立的编译单元，由链接阶段决定把哪一个挂进来。

#### 4.3.3 源码精读

模拟器抽象的基类位于 [runtime/nvqir/CircuitSimulator.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/nvqir/CircuitSimulator.h)，所有具体后端都继承自它。QIR QIS 实现与注册宏在 [runtime/nvqir/NVQIR.cpp](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/nvqir/NVQIR.cpp)。

量子指令集（类型 + 门）在 [runtime/cudaq/qis/](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/)，例如 `modifiers.h`（控制/负控修饰符）、`qkernel.h`、`execution_manager.h`、`qarray.h`。这些头文件就是 C++ 内核能直接 `#include` 使用的 API。

平台抽象的实现分布在 [runtime/cudaq/platform/](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/platform/) 下，本地模拟用 `default/`，多 QPU 用 `mqpu/`，各硬件厂商平台各有独立子目录。后续讲义 u6 会逐个展开。

#### 4.3.4 代码实践

**实践目标**：把「模拟器后端」与「平台」两类目录区分清楚，理解它们是两个不同维度。

**操作步骤**：

1. 列出 `runtime/nvqir/` 下的所有后端子目录（qpp、custatevec、cutensornet、cudensitymat、dem、stim、resourcecounter）。
2. 列出 `runtime/cudaq/platform/` 下的所有平台子目录（default、mqpu、fermioniq、orca、pasqal、quera、common）。
3. 用一句话区分：后端负责「**怎么算**」，平台负责「**在哪里跑、怎么调度**」。

**需要观察的现象**：后端目录里通常有 `*CircuitSimulator.cpp`，平台目录里通常有 `*QPU.cpp` / `*Platform.cpp`。注意这种命名约定。

**预期结果**：你能凭文件名一眼判断一个 `.cpp` 属于「模拟器后端」还是「平台」。

**待本地验证**：如果你的构建启用了不同后端，可在构建产物（如 `build/runtime/nvqir/`）中确认哪些后端 `.so` 被实际生成。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `runtime/common/` 要单独成库（`cudaq-common`），而不是放进 `runtime/cudaq/`？

> **答案**：因为它提供的是**跨子系统**的通用类型（如 `MeasureCounts`、`ExecutionContext`、日志、远程 REST 客户端）。Python 前端、远程平台、nvqir 都要复用这些类型，所以抽成底层公共库，避免循环依赖。

**练习 2**：`runtime/cudaq/qis/` 与 `runtime/nvqir/` 都涉及「门」，二者职责有何不同？

> **答案**：`qis/` 提供**面向用户**的 C++ API（量子类型 + 门函数 + 修饰符），供内核直接调用；`nvqir/` 提供**面向后端**的执行实现，把门操作真正作用到状态向量/张量网络上。前者是「声明意图」，后者是「执行计算」。

---

### 4.4 `python/` 与 `realtime/`：Python 前端与实时子系统

#### 4.4.1 概念说明

- **`python/`**：Python 前端。它最终复用 `runtime/` 这个底盘，所以你在 Python 里调用的 `cudaq.sample`、`cudaq.observe`，底层都是 C++ 运行时。Python 这一层的核心工作是：把 Python 函数（被 `@cudaq.kernel` 装饰）翻译成 Quake MLIR——这部分逻辑在 `python/cudaq/kernel/`。
- **`realtime/`**：Realtime 子系统，负责 FPGA 与 GPU 的紧耦合、低延迟实时控制，是一个相对独立、自包含的子项目（自带 `docs/`、`examples/`、`unittests/`、`docker/`）。

#### 4.4.2 核心流程

```text
python/
├── cudaq/            # ★ 安装后的 Python 包源码
│   ├── __init__.py   #   包入口（导出 sample/observe/...）
│   ├── kernel/       #   ★ @cudaq.kernel 装饰器、ast_bridge.py、kernel_builder、quake_value、register_op
│   ├── runtime/      #   Python 端运行时封装（sample.py、state.py 等）
│   ├── operators/、qis/、domains/、dynamics/
│   ├── display/、visualization/、mlir/、kernels/、lib/、contrib/、dbg/、handlers/、util/
├── runtime/          # C++/nanobind 绑定源码（common/、cudaq/、interop/、mlir/）
├── extension/、metapackages/、tests/、utils/
└── metadata.cmake、README.md.in
```

```text
realtime/
├── README.md、docs/   # 含 user_guide、host_api、message_protocol 文档
├── include/、lib/     # Realtime 实现源码
├── examples/、scripts/、unittests/、docker/、cmake/
```

#### 4.4.3 源码精读

Python 包入口 [python/cudaq/__init__.py](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/python/cudaq/__init__.py) 是用户 `import cudaq` 时最先执行的文件，它把 `sample`、`observe`、`kernel`、`spin_op` 等符号暴露出来。

Python 前端把 Python 函数翻译成 Quake 的核心目录是 [python/cudaq/kernel/](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/python/cudaq/kernel/)，其中：

- 装饰器：`kernel_decorator.py`
- Python AST → Quake：`ast_bridge.py`（与 C++ 端 `ASTBridge.cpp` 是一对镜像，但语言不同）
- 内核表示与动态构造：`kernel_builder.py`、`quake_value.py`
- 自定义门注册：`register_op.py`

`python/runtime/` 不是 Python 文件，而是 **C++ + nanobind** 绑定源码——它把 `runtime/` 里的 C++ 类暴露给 Python（[python/runtime/](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/python/runtime/)）。

Realtime 子系统是自包含的，入口文档 [realtime/README.md](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/realtime/README.md)，详细文档在其 `realtime/docs/` 下。它和编译器/运行时的耦合较弱，是仓库里独立性最强的一块。

#### 4.4.4 代码实践

**实践目标**：对比 Python 前端与 C++ 前端的「AST → Quake」翻译入口，建立对照关系。

**操作步骤**：

1. 打开 `python/cudaq/kernel/ast_bridge.py`，记住它是 Python 侧的翻译器。
2. 打开 `cudaq/lib/Frontend/nvqpp/ASTBridge.cpp`（C++ 侧的翻译器，见 4.2）。
3. 记下这条对照：**Python 函数 → `python/.../ast_bridge.py` → Quake**；**C++ 源码 → `cudaq/.../ASTBridge.cpp` → Quake**。两条路径最终汇入同一个 Quake/运行时底盘。

**需要观察的现象**：两个翻译器名字相似（都叫 AST Bridge），但一个遍历 Python AST，一个遍历 Clang AST。

**预期结果**：你理解了「为什么 Python 和 C++ 内核可以共享同一套后端与算法原语」——因为它们在 Quake 这一层就合流了。

#### 4.4.5 小练习与答案

**练习 1**：`python/runtime/` 目录下是 `.py` 文件还是 `.cpp` 文件为主？

> **答案**：主要是 `.cpp` 文件（nanobind 绑定源码）。它的作用是把 C++ 运行时绑定到 Python，而非 Python 业务逻辑。Python 业务逻辑在 `python/cudaq/` 下。

**练习 2**：`realtime/` 子系统为什么自带 `docs/`、`examples/`、`unittests/`？

> **答案**：因为 Realtime 是一个**相对独立、面向硬件实时控制**的子项目，有自己的用户群（控制工程师）、自己的文档与示例需求，所以采用自包含布局，与编译器/运行时的文档分离。

---

### 4.5 `docs/` 与测试目录：示例、FileCheck、单元测试、跨目标测试

#### 4.5.1 概念说明

CUDA-Q 的「可运行示例」和「测试」分散在四个地方，初学者常常找不到入口。本节把它们一次性梳理清楚：

| 目录 | 内容 | 形式 |
| --- | --- | --- |
| `docs/sphinx/examples/cpp/` | C++ 示例（`basics/`、`dynamics/`、`mpi/`、`other/` + 顶层 `.cpp`） | 可用 `nvq++` 编译的源码 |
| `docs/sphinx/examples/python/` | Python 示例（`intro.py`、`building_kernels.py`、`.ipynb` 等） | 可直接运行的脚本/笔记本 |
| `cudaq/test/` | **编译器**回归测试 | lit + FileCheck（`.cpp` 输入 + 期望 IR） |
| `unittests/` | **运行时**单元测试 | GoogleTest（C++） |
| `targettests/` | **跨目标**端到端测试 | lit（用各种 `-target` 编译运行） |

#### 4.5.2 核心流程

这三类测试在构建时通过根 `CMakeLists.txt` 挂载（见 4.1.3）。它们的分工可以这样记忆：

- **`cudaq/test/`（FileCheck 测试）**：验证「编译器把源码翻译成正确的 IR」。输入是一个 `.cpp`，用 FileCheck 比对生成的 Quake/QIR 文本。按主题分子目录：`AST-Quake/`（C++→Quake 对照）、`AST-error/`（期望编译报错）、`Transforms/`（Pass 效果）、`Translate/`（导出 QASM 等）、`ArgumentConversion/`、`CompileTarget/`、`MixedLanguage/`、`NVQPP/`、`Unit/`、`plugin/`。
- **`unittests/`（GoogleTest）**：验证「运行时库的 C++ 单元行为正确」。子目录按运行时模块分：`nvqpp/`、`spin_op/`、`operators/`、`dynamics/`、`qir/`、`target_config/`、`device_call/`、`logger/`、`output_record/`、`common/`、`utils/`。
- **`targettests/`（跨目标端到端）**：验证「在各种 target（不同后端/不同硬件厂商）下程序能正确编译并运行出预期结果」。除了 `Kernel/`、`SeparateCompilation/`、`Target/`、`TargetConfig/`、`execution/`、`analog/` 这些通用子目录，还有大量**硬件厂商专属**子目录：`braket/`、`ionq/`、`iqm/`、`quantinuum/`、`oqc/`、`qci/`、`infleqtion/`、`anyon/`、`tii/`、`quantum_machines/`、`scaleway/`、`qbraid/` 等——每个对应一个远程 QPU 平台。

> 导航提示：找「怎么用」看 `docs/sphinx/examples/`；找「编译器某行为是否被测」看 `cudaq/test/`；找「运行时某 API 是否被测」看 `unittests/`；找「某硬件平台是否支持某特性」看 `targettests/<vendor>/`。

#### 4.5.3 源码精读

C++ 示例的入口目录是 [docs/sphinx/examples/cpp/](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/cpp/)，其中 `basics/` 收纳了最小可运行示例（如 `expectation_values.cpp`、`static_kernel.cpp`、`mid_circuit_measurement.cpp`、`noise_amplitude_damping.cpp`），是初学者最好的起点；这些示例会在 u1-l4、u2、u3 等讲义中反复出现。

Python 示例入口 [docs/sphinx/examples/python/intro.py](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/python/intro.py) 是 Python 端的「Hello World」。

编译器测试的总入口配置在 [cudaq/test/lit.cfg.py](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/test/lit.cfg.py)，跨目标测试的总入口配置在 [targettests/lit.cfg.py](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/targettests/lit.cfg.py)——这两个 `lit.cfg.py` 是用 `lit`（LLVM 集成测试器）跑这两类测试时的关键配置。

#### 4.5.4 代码实践

**实践目标**：为「示例与三类测试」制作一份导航速查表，并验证你能找到指定文件。

**操作步骤**：

1. 在 `docs/sphinx/examples/cpp/basics/` 下找一个最小示例文件名（如 `static_kernel.cpp`）。
2. 在 `cudaq/test/AST-Quake/` 下任选一个测试（如 `adjoint-1.cpp`），打开它，观察它如何用 `// CHECK:` 注释声明期望的 IR。
3. 在 `targettests/` 下找一个硬件厂商目录（如 `braket/`），看看里面测试的组织方式。

**需要观察的现象**：`cudaq/test/` 的文件里大量出现 `RUN:` 和 `CHECK:` 注释——这是 lit + FileCheck 的标志。

**预期结果**：你能凭目录名判断「这是示例还是测试、是哪一类测试」，并知道在哪运行它（详见讲义 u8-l1）。

#### 4.5.5 小练习与答案

**练习 1**：你想验证「优化 Pass `AggressiveInlining` 是否把内层内核正确内联了」，应该去哪个目录找测试？

> **答案**：`cudaq/test/Transforms/`。编译器 Pass 的行为由 FileCheck 测试覆盖，按 Pass 主题归类。

**练习 2**：`unittests/` 和 `cudaq/test/Unit/`（即 `cudaq/unittests/`）都是 C++ 单元测试，它们的覆盖对象有何不同？

> **答案**：`unittests/`（根目录下）覆盖**运行时**（runtime）的 C++ 单元行为；`cudaq/unittests/` 与 `cudaq/test/Unit/` 覆盖**编译器**（cudaq）的 C++ 单元行为。两者分别随 `runtime` 与 `cudaq` 子系统构建。

---

## 5. 综合实践

**实践任务**：在仓库根目录用 `find` / `ls` 列出 `cudaq/lib/Optimizer/Transforms/` 下所有 Pass，统计数量，并按功能「猜测分类」，最终产出一份《编译器 Pass 速查表》。

这个任务把你本讲学到的「`cudaq/` 编译器目录树」「`lib/Optimizer/Transforms/` 是 Pass 锚点」「如何用命令探查目录」串联起来，并直接为后续 u4-l6（优化 Pass 流水线）讲义做铺垫。

### 步骤 1：实践目标

- 熟练使用 `ls`/`find` 探查大型目录。
- 建立「Pass 文件名 → 功能」的初步直觉。
- 产出一份可长期维护的速查表文档（你可以把它放在自己的笔记里，不要写进仓库）。

### 步骤 2：操作步骤

1. 进入仓库根目录，列出该目录所有 `.cpp` 文件（排除 `.inc`、`.h` 等辅助文件）：

   ```bash
   ls cudaq/lib/Optimizer/Transforms/*.cpp
   ```

2. 统计数量：

   ```bash
   ls cudaq/lib/Optimizer/Transforms/*.cpp | wc -l
   ```

3. 浏览文件名，按**关键词**给它们「猜测分类」。建议的分类维度（关键词 → 推测功能）：

   | 关键词（文件名片段） | 推测功能 |
   | --- | --- |
   | `Inlining` / `LambdaLifting` | 内联与 λ 提升 |
   | `Decomposition*` | 门分解 |
   | `Loop*` | 循环处理（展开、规范化、剥离） |
   | `Measure*` / `BasisConversion` | 测量与基变换 |
   | `ObserveAnsatz` | observe 相关 |
   | `GenKernelExecution` / `GenDeviceCodeLoader` | 内核执行胶水代码生成 |
   | `ArgumentSynthesis` / `PySynthCallableBlockArgs` | 参数合成 |
   | `Dead*` / `Elimination` / `MemToReg` / `SROA` | 死代码消除与 SSA 化 |
   | `QuakeSimplify` / `PhaseFolding` / `ConstantPropagation` | 化简与常量传播 |
   | `Mapping` / `DependencyAnalysis` / `ResourceCount*` | 分析与资源统计 |

4. 选 2–3 个你感兴趣的 Pass（如 `AggressiveInlining.cpp`、`Decomposition.cpp`、`GenKernelExecution.cpp`），打开文件头部注释，核对你的「猜测分类」是否正确。

### 步骤 3：需要观察的现象

- 该目录下 `.cpp` 文件总数（含少量辅助实现文件）。
- 文件名是否高度「自描述」（CUDA-Q 的 Pass 命名通常能直接看出功能）。
- 是否有同名 `.inc` / `.h` 配套文件（这些是 Pass 内部使用的模式/工具，不是独立 Pass）。

### 步骤 4：预期结果（参考答案）

> 经实际统计，`cudaq/lib/Optimizer/Transforms/` 下有 **68 个 `.cpp` 文件**（确切数量可能随版本微调；本讲基于当前 HEAD）。其中绝大多数每个文件对应一个独立的 MLIR Pass，少数（如 `DecompositionPatterns.cpp`、`LoopAnalysis.cpp`）是某些 Pass 共用的辅助实现。`PassDetails.h` 是所有 Pass 的公共基类声明。

你的速查表应当能回答：「我想看内联优化，去哪个文件？」「我想看门分解，去哪个文件？」——这正是后续阅读编译器源码时的常用入口。

### 步骤 5：待本地验证项

- 你本地仓库的 `.cpp` 数量是否与本文给出的 68 一致（若不一致，以你本地 `wc -l` 的结果为准）。
- 选定 Pass 的头部注释是否印证了你的分类猜测。

> ⚠️ 本实践不修改任何源码，只在终端执行只读命令。请勿把速查表写进仓库目录。

## 6. 本讲小结

- 仓库由四大子系统构成：`cudaq/`（编译器）、`runtime/`（C++ 运行时 + nvqir）、`python/`（Python 前端）、`realtime/`（实时控制），它们的挂载关系由根 `CMakeLists.txt` 的 `add_subdirectory` 决定。
- `cudaq/` 是标准 MLIR 编译器布局：`include/`（声明，含 `.td` 方言定义）+ `lib/`（实现，Pass 集中在 `lib/Optimizer/Transforms/`）+ `tools/`（cudaq-quake/opt/translate/nvq++）+ `test/`（FileCheck）。
- `runtime/` 是公共底盘：`common/`（公共类型）、`cudaq/`（规范实现：qis 算法、平台）、`nvqir/`（模拟器后端，后端切换是链接期任务）。
- `python/` 通过 `kernel/ast_bridge.py` 把 Python 函数翻译成 Quake，通过 `python/runtime/` 的 nanobind 绑定复用 C++ 运行时；`realtime/` 是自包含的硬件实时子项目。
- 示例在 `docs/sphinx/examples/`；测试分三类：`cudaq/test/`（编译器 FileCheck）、`unittests/`（运行时 GoogleTest）、`targettests/`（跨目标端到端，含众多硬件厂商目录）。
- 遇到任何文件，先用「四象限」判断它属于哪个子系统，再判断它在 include/lib/tools/test 哪一层——这是后续所有讲义的基本功。

## 7. 下一步学习建议

有了这张目录地图，接下来的学习路径：

1. **想立刻跑通一个程序** → 继续学本单元的 [u1-l3（从源码构建与运行 CUDA-Q）](u1-l3-build-and-run.md)，掌握 `build_cudaq.sh` 与 `nvq++` 工作流。
2. **想看第一个量子内核** → 学 [u1-l4（第一个 C++ 量子内核）](u1-l4-first-cpp-kernel.md)，会用到本讲提到的 `docs/sphinx/examples/cpp/basics/`。
3. **想深入编译器** → 先学单元 4 的 [u4-l1（MLIR 基础与编译总览）](u4-l1-mlir-overview.md)，届时你会回到 `cudaq/lib/Optimizer/Transforms/` 详细阅读具体 Pass。
4. **想深入后端** → 先学 [u3-l1（执行模型）](u3-l1-execution-model.md)，再到单元 6 深入 `runtime/nvqir/` 的各个模拟器。

> 建议把本讲的目录树截图或抄进自己的笔记，作为阅读后续讲义时的「速查地图」。
