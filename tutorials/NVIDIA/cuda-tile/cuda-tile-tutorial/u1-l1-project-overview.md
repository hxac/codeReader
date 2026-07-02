# CUDA Tile IR 是什么：定位、组成与生态

> 本讲是 cuda-tile 学习手册的第一篇，目标是让你在 **不动手编译任何东西** 的情况下，先建立一个清晰的心智模型：CUDA Tile IR 到底是什么、它由哪几部分组成、它处于 CUDA 工具链的哪一层。
>
> 本篇不要求你预先了解 MLIR。所有概念都会从直觉讲起，再落到真实源码。

---

## 1. 本讲目标

学完本讲后，你应该能够：

1. 用一句话说清 **CUDA Tile IR 解决的核心问题**——面向 NVIDIA 张量核（tensor core）的分块计算与显存层次抽象。
2. 识别项目的 **四大核心组件**：CUDA Tile Dialect（方言）、Python Bindings（Python 绑定）、Bytecode（字节码）、Conformance Test Suite（一致性测试套件），并把它们对应到仓库里的真实目录。
3. 理解 **版本号规则** 以及它与 CUDA Toolkit、MLIR/LLVM 的对齐与兼容关系。
4. 复述一条 **端到端的数据流**：文本 MLIR → 字节码 → cubin/JIT → 在 GPU 上运行。

本讲只做「俯瞰」，不深入任何具体操作或机制。具体的构建、运行、方言语法会在后续讲义中展开。

---

## 2. 前置知识

为了让你不至于在术语里迷路，先用大白话解释几个本讲会反复出现的概念。

### 2.1 什么是 IR（中间表示）

编译器通常不是一步把源代码翻译成机器码，而是先翻译成一种「中间表示（Intermediate Representation，IR）」，再在 IR 上做各种优化，最后才生成目标代码。这样做的好处是：**优化逻辑可以和具体语言、具体硬件解耦**。

例如 LLVM 的核心就是把 C/C++/Rust 等语言都先编译成 LLVM IR，再统一优化、统一生成机器码。

### 2.2 什么是 MLIR

MLIR（Multi-Level Intermediate Representation）是 LLVM 社区推出的一个 **「可扩展」的 IR 框架**。它的核心思想是 **方言（Dialect）**：每个领域可以定义自己的「操作」和「类型」，组成一个方言，然后把多个方言混在同一个 IR 里。

> 类比：如果把 IR 比作一种「编程语言」，那么 MLIR 方言就像这种语言里的「专业术语词典」。`cuda_tile` 就是 CUDA Tile 项目自定义的一本词典。

CUDA Tile IR 就是一个 **建立在 MLIR 之上的、专门面向 NVIDIA GPU 张量核的方言 + 配套编译基础设施**。

### 2.3 什么是张量核与分块（Tile）

NVIDIA GPU 里有两种计算单元：

- **CUDA 核**：标量级运算，一个线程算一个元素。
- **张量核（tensor core）**：矩阵乘加专用单元，一次能算一小块矩阵（例如 16×16×16）的乘加。

「分块（Tile）」就是指把大矩阵切成适配张量核的小块，让计算、访存都按「一块一块」来组织。CUDA Tile IR 的 **几乎所有类型和操作都围绕 tile 这个概念展开**（例如 `tile<4x8xf32>` 表示「4×8 的 f32 分块」）。

### 2.4 CUTLASS 与 Triton 是什么（用于后续对比）

- **CUTLASS**：NVIDIA 的 C++ 模板库，用来手写高性能 CUDA 内核，本质是一个 **源码级库**。
- **Triton**：一种 Python 语言 + 编译器，让用户用 Python 写 GPU 内核，自带一套自己的 IR。

CUDA Tile IR 和它们 **不是同一层的东西**，后面的实践题会让你自己总结区别。

---

## 3. 本讲源码地图

本讲涉及的文件很少，重点是建立全局认知。下表列出本讲引用的真实文件及其作用。

| 文件 | 作用 | 本讲如何使用 |
|------|------|--------------|
| [README.md](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/README.md) | 项目主文档，包含定位、组件、构建、示例、版本规则 | 作为本讲的主要信息来源 |
| [LICENSE.txt](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/LICENSE.txt) | 许可证（Apache 2.0 with LLVM Exceptions） | 说明项目的开源协议 |
| [cmake/IncludeLLVM.cmake](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/cmake/IncludeLLVM.cmake) | 锁定 MLIR/LLVM 的具体 commit | 讲版本兼容性时引用 |

仓库顶层目录与四大组件的对应关系（后面会反复用到）：

| 顶层目录 | 对应组件 |
|----------|----------|
| `include/cuda_tile/Dialect/CudaTile/`、`lib/Dialect/CudaTile/` | CUDA Tile Dialect（方言定义与实现） |
| `include/cuda_tile/Bytecode/`、`lib/Bytecode/` | Bytecode（字节码读写） |
| `python/` | Python Bindings（Python 绑定） |
| `test/` | Conformance Test Suite（一致性测试套件） |
| `tools/` | 命令行工具（`cuda-tile-translate`、`cuda-tile-opt` 等），贯穿各组件 |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **4.1 项目定位**：CUDA Tile IR 解决什么问题，它在工具链里处于什么位置。
2. **4.2 四大核心组件**：方言、Python 绑定、字节码、一致性测试分别是什么。
3. **4.3 版本与 LLVM 兼容性**：版本号怎么读，为什么必须锁定到 LLVM 的某个具体 commit。

---

### 4.1 项目定位：CUDA Tile IR 解决什么问题

#### 4.1.1 概念说明

写一个高性能 CUDA 内核是非常困难的事：你需要同时考虑

- 如何把数据切成适配张量核的小块；
- 如何管理显存层次（全局显存 / 共享内存 / 寄存器）；
- 如何排布线程网格（grid / CTA / warp）；
- 如何让访存和计算重叠以隐藏延迟。

CUDA Tile IR 的目标是：**用一个高层 IR 直接表达「分块计算 + 显存层次」这两个核心抽象，让上层前端（编译器、库）可以把内核 lowering 到这个 IR，再由它生成高效的 GPU 机器码**。换句话说，它把「张量核分块优化」这件事做成了 IR 层的一等公民，而不是让每个库各写一遍。

#### 4.1.2 核心流程：数据流总览

CUDA Tile IR 的一条完整数据流如下：

```text
文本 MLIR 程序（人写的 .mlir）
        │  cuda-tile-translate --mlir-to-cudatilebc
        ▼
CUDA Tile 字节码（.tilebc，二进制）
        │
        ├── 路线 A（AoT，提前编译）：tileiras 把字节码编译成 cubin
        │       ▼
        │   .cubin（GPU 机器码）
        │       │  cuModuleLoad + cuLaunchKernel
        │       ▼
        │   在 GPU 上运行
        │
        └── 路线 B（JIT，即时编译）：CUDA 驱动直接加载 .tilebc
                ▼
            驱动自动 JIT → 运行
```

两个关键点：

1. **输入**是文本 MLIR；**输出**是可以被 CUDA 驱动加载的字节码（或进一步编译出的 cubin）。
2. **AoT 与 JIT 两条路线的驱动调用方式完全一样**，区别只是加载 `.cubin` 还是 `.tilebc`。

#### 4.1.3 源码精读

README 开头的第一段就给项目下了准确定义。注意它强调的三个关键词：**MLIR-based**、**tile-based**、**tensor core**：

[README.md:3-9](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/README.md#L3-L9) — 这是项目的一句话定位：基于 MLIR 的中间表示与编译基础设施，面向 CUDA 内核优化，重点是分块计算与 NVIDIA 张量核。

紧接着一句交代了它与 CUDA Toolkit 的对齐版本：

[README.md:11-12](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/README.md#L11-L12) — 说明本次开源发布与 **CUDA Toolkit 13.1** 对齐。

要直观感受「输入是文本 MLIR」，看 README 里给出的最小示例内核（这段代码你不必完全读懂，只需看出它是文本、里面出现了 `tile<...>`、`load_ptr_tko`、`print_tko` 等操作即可）：

[README.md:234-245](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/README.md#L234-L245) — 一个最简 Tile IR 内核：接收一个 `tile<ptr<f32>>` 指针，构造偏移、加载 128 个 float，打印出来。这里的 `cuda_tile.module` / `entry` / `tile<128xf32>` 就是 CUDA Tile 方言的语法。

而「如何把这段 MLIR 变成可运行的字节码」，README 给出了确切命令：

[README.md:328-329](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/README.md#L328-L329) — 第 1 步用 `cuda-tile-translate` 把 `.mlir` 翻译成字节码 `example.tilebc`；第 2 步（可选）用 CUDA Toolkit 的 `tileiras` 把字节码编译成 `cubin`。这两步正好对应 4.1.2 流程图里的「翻译」与「AoT 编译」。

#### 4.1.4 代码实践

这是一个 **源码阅读型实践**，无需运行任何命令。

**实践目标**：用自己的话把项目定位讲清楚，并区分 CUDA Tile IR 与同类项目。

**操作步骤**：

1. 通读 [README.md:1-34](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/README.md#L1-L34)（从开头到「Core Components」之后）。
2. 在笔记本上写一段 **不超过 200 字** 的项目摘要，必须回答两个问题：
   - CUDA Tile IR 的 **输入** 和 **输出** 分别是什么？
   - 它和 **CUTLASS / Triton** 这类库的区别在哪里？（提示：一个是 IR/编译基础设施，一个是源码库，一个是语言+编译器。）
3. 列出 README 中明确提到的 **四个核心组件** 名称。

**预期结果**：你应该能写出类似这样的回答——

- 输入是文本 MLIR（或经 Python 绑定构造的 IR）；输出是字节码 `.tilebc`，可进一步编成 cubin 或被驱动 JIT 运行。
- 区别在于：CUDA Tile IR 是一个 **中间表示 + 编译基础设施**，而 CUTLASS 是一个 **C++ 源码库**、Triton 是一种 **面向用户的语言+编译器**。CUDA Tile IR 更适合作为前端 lowering 的目标 IR。
- 四个核心组件：CUDA Tile Dialect、Python Bindings、Bytecode、Conformance Test Suite。

> 由于本实践是阅读理解题，没有「运行结果」。如果你写不出 200 字摘要，说明需要重读 4.1.1 和 4.1.2。

#### 4.1.5 小练习与答案

**练习 1**：CUDA Tile IR 为什么要基于 MLIR，而不是自己从零设计一套 IR？

**参考答案**：MLIR 提供了成熟的「方言」扩展机制、类型/操作定义工具（TableGen）、Pass 基础设施和字节码框架。基于 MLIR，CUDA Tile 只需专注定义自己的领域语义（分块、张量核、显存层次），而把通用编译器基础设施交给 MLIR 复用，既省工作量又能和 LLVM 生态互通。

**练习 2**：把下面这句话翻译成大白话——「focusing on tile-based computation patterns and optimizations targeting NVIDIA tensor core units」。

**参考答案**：它的重点是把计算组织成「一小块一小块（tile）」的模式，并专门针对 NVIDIA 的张量核做优化。

**练习 3**：在 4.1.2 的流程图里，AoT 和 JIT 两条路线的 **哪一步是完全相同** 的？

**参考答案**：**驱动启动内核的调用方式完全相同**——都是用 `cuModuleLoad` 加载模块、`cuModuleGetFunction` 取得入口、`cuLaunchKernel` 启动，区别只在于加载的是 `.cubin` 还是 `.tilebc`。

---

### 4.2 四大核心组件

#### 4.2.1 概念说明

README 的「Core Components」一节明确把 CUDA Tile 拆成四块。理解这四块各自的角色，就理解了整个项目的骨架。

| 组件 | 一句话作用 |
|------|-----------|
| **CUDA Tile Dialect**（方言） | 用 MLIR 的方式定义「有哪些操作、有哪些类型」。这是项目语义的 **核心**。 |
| **Python Bindings**（Python 绑定） | 让你用 Python 代码 **程序化地构造、修改、变换 IR**，而不是只能手写文本 MLIR。 |
| **Bytecode**（字节码） | 一种高效的二进制格式，支持 IR 与二进制之间的 **序列化/反序列化**，是部署产物。 |
| **Conformance Test Suite**（一致性测试套件） | 一套测试，确保实现 **符合 CUDA Tile 规范**、方言语义被正确验证。 |

四者的关系可以这样理解：

- **Dialect** 定义「语言本身」；
- **Python Bindings** 提供一种「编程方式生成这门语言的程序」；
- **Bytecode** 是「这门语言程序的保存与发布格式」；
- **Conformance Tests** 是「这门语言的语法/语义裁判」。

#### 4.2.2 核心流程：组件如何协同

把它们串进一条工作流：

```text
   开发者
     │
     │  (1) 用 Python Bindings 构造 IR
     │      或 手写文本 MLIR
     ▼
  CUDA Tile Dialect 定义的 IR（操作 + 类型）
     │
     │  (2) 由 Bytecode Writer 序列化
     ▼
   .tilebc 字节码文件  ──► (4) Conformance Tests 持续验证
     │                         上述每一步都符合规范
     │  (3) AoT/JIT 编译运行
     ▼
   GPU 执行
```

注意 **Conformance Tests 是横切关注点**：它不参与运行时，但贯穿 Dialect、字节码、绑定的开发全过程，保证三者的语义一致。

#### 4.2.3 源码精读

README 的「Core Components」章节用四个要点列出了组件定义：

[README.md:16-25](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/README.md#L16-L25) — 四个核心组件的官方定义：CUDA Tile Dialect（领域专用 MLIR 方言，提供分块计算的一等操作与类型）、Python Bindings（用于程序化构造/修改/变换 IR 的完整 Python API）、Bytecode（支持序列化/反序列化的高效二进制表示）、Conformance Test Suite（确保符合规范并校验方言语义的测试套件）。

把这四个抽象组件落到仓库目录上（目录在本讲「源码地图」已列出，这里再强调对应关系）：

- **Dialect** 的定义在 `include/cuda_tile/Dialect/CudaTile/IR/` 下，由一堆 `.td`（TableGen）和 `.h` 文件构成。比如方言根定义就在 [include/cuda_tile/Dialect/CudaTile/IR/Dialect.td](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Dialect.td)。你现在不必读懂它，只需知道「方言在这里定义」。
- **Bytecode** 的读写实现分别在 [lib/Bytecode/Writer/](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode) 与 [lib/Bytecode/Reader/](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode) 下。
- **Python Bindings** 的入口在 [python/SiteInitializer.cpp](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/SiteInitializer.cpp)，高层 Python API 在 `python/cuda_tile/` 下。
- **Conformance Tests** 在 [test/](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test) 目录，按 `Dialect/Bytecode/Transforms/CAPI/python` 分类。

> 小提示：`README.md:22` 写的是 `Bytecode:`（多了一个冒号），这是原文档的一处笔误，组件名实际就是 **Bytecode**。读源码时不要被这个冒号误导。

#### 4.2.4 代码实践

**实践目标**：把抽象的「四大组件」和仓库里的真实目录一一对应，建立物理直觉。

**操作步骤**：

1. 在仓库根目录浏览顶层目录结构（可用 `ls` 或文件树）。
2. 填写下面这张表（在脑中或笔记本上）：

| 组件 | 对应的源码目录/文件（至少一个） |
|------|--------------------------------|
| CUDA Tile Dialect | `include/cuda_tile/Dialect/CudaTile/...`、`lib/Dialect/CudaTile/...` |
| Python Bindings | ？ |
| Bytecode | ？ |
| Conformance Test Suite | ？ |

3. 进入 `test/` 目录，观察它的子目录命名（`Dialect/Bytecode/Transforms/CAPI/python`），思考：**为什么测试子目录恰好对应组件？**（提示：因为每个组件都需要被独立测试。）

**需要观察的现象**：你会发现 `test/` 的子目录几乎是「组件清单的镜像」——Dialect 有方言测试、Bytecode 有读写测试、CAPI 有 C 接口测试、python 有绑定测试。这印证了 4.2.2 里「测试是横切关注点」的说法。

**预期结果**：能准确说出 Python Bindings 在 `python/`、Bytecode 在 `include/cuda_tile/Bytecode/` + `lib/Bytecode/`、Conformance Test Suite 在 `test/`。

> 待本地验证：本实践是目录浏览题，只要你的仓库结构与本讲「源码地图」一致即视为正确。

#### 4.2.5 小练习与答案

**练习 1**：如果让你「保存一个 Tile IR 程序，明天再加载回来」，你会用到四大组件里的哪一个？

**参考答案**：**Bytecode**。它正是负责 IR 与二进制之间序列化/反序列化的组件（Writer 写出 `.tilebc`，Reader 读回）。

**练习 2**：为什么 Conformance Test Suite 不算「运行时组件」，却仍然重要？

**参考答案**：因为它不参与内核的执行，但在开发期持续校验：方言定义、字节码读写、Python 绑定三者的语义是否都符合 CUDA Tile 规范。没有它，三处实现可能出现「同一个操作行为不一致」的回归。

**练习 3**：四大组件中，哪一个最接近「这门 IR 语言本身」？

**参考答案**：**CUDA Tile Dialect**。它定义了操作和类型，即语言本身；其余三者分别是「生成语言的工具」「保存语言的格式」「校验语言的裁判」。

---

### 4.3 版本与 LLVM 兼容性

#### 4.3.1 概念说明

CUDA Tile 有 **两套独立的版本约束**，初学者很容易混淆：

1. **CUDA Tile 自身的版本号**：形如 `13.1.0`、`13.3.0`，遵循 `Major.Minor.Patch`。
2. **对 MLIR/LLVM commit 的锁定**：CUDA Tile 必须基于某个 **具体的 LLVM commit** 构建，换一个 commit 可能就编不过。

为什么要锁定到具体 commit？因为 MLIR/LLVM 是快速演进的上游项目，它的内部 API 经常 breaking change。CUDA Tile 作为下游消费者，必须跟着上游打补丁，于是形成了「兼容区间」的概念。

#### 4.3.2 核心流程：版本号怎么读、兼容区间怎么算

**版本号规则**：`Major.Minor` 直接对应 CUDA Toolkit 版本；`Patch` 是开源发布自己的计数，与 toolkit 的 patch 无关。当 toolkit 的 major 或 minor 升级时，Patch 归零。

用区间记号表示某个 CUDA Tile 版本 `v` 能兼容的 LLVM commit 范围。设两个相邻版本 `v_i`、`v_{i+1}`，它们各自锁定的 LLVM commit 为 \(c_i\)、\(c_{i+1}\)，则：

\[
\text{v_i 的兼容区间} = [c_i,\ c_{i+1})
\]

即左闭右开：`v_i` 能跑 `[c_i, c_{i+1})` 之间的 LLVM commit。

README 给出了真实例子（下表节选）：

| CUDA Tile IR 版本 | 兼容的 LLVM commit 区间 |
|------------------|------------------------|
| `v13.1.0` | \([81b576e66,\ cfbb4cc3121)\) |
| `v13.1.1` | \([cfbb4cc3121,\ 3d7018c70)\) |
| `v13.1.3` | \([3d7018c70,\ \text{latest})\) |

**怎么确定我的 LLVM commit 该用哪个 CUDA Tile 版本？** 项目提供了脚本 `scripts/get-cuda-tile-version-for-llvm-hash.sh` 自动查询，详见 4.3.4 的实践。

#### 4.3.3 源码精读

README 的「Versioning」一节定义了版本号语义：

[README.md:347-353](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/README.md#L347-L353) — 版本号规则：Major/Minor 对应 CUDA Toolkit，Patch 独立计开源发布；当 toolkit 主/次版本升级时 Patch 归零（如 `13.1.5 → 13.2.0`）。

「Keeping Compatibility with LLVM」一节解释了为什么要锁定 commit，并给出了兼容区间表：

[README.md:363-381](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/README.md#L363-L381) — 说明 CUDA Tile IR 需要 LLVM 处于特定兼容 commit；每遇到一个 LLVM breaking commit，就发一个新的修复版本（如 `v13.1.1`、`v13.1.3`），从而形成与 LLVM 的兼容区间，并列出了具体的区间表。

而「当前这个仓库到底锁了哪个 LLVM commit」这个事实，藏在 CMake 配置里，不在 README 正文里。需要去 `cmake/IncludeLLVM.cmake` 找：

[cmake/IncludeLLVM.cmake:28-30](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/cmake/IncludeLLVM.cmake#L28-L30) — 这里把 `LLVM_BUILD_COMMIT_HASH` 写死为 `57109befac92811d2253109242ca6fa69c961fb2`。也就是说，**当前仓库（HEAD `e01244d`）必须用这个具体 commit 的 LLVM 来构建**。

> 交叉印证：本仓库最近一次提交信息是 `[LLVM-FIX] Breaking commit 57109befac92`，正好对应上面这个 hash——说明这次提交就是为了让 CUDA Tile 适配 LLVM 的 `57109befac92` breaking 变更。

确定版本的脚本用法在 README 后半段：

[README.md:391-396](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/README.md#L391-L396) — 用 `scripts/get-cuda-tile-version-for-llvm-hash.sh` 传入你的 LLVM commit，即可查到该用哪个 CUDA Tile IR 版本。

#### 4.3.4 代码实践

**实践目标**：亲手找到当前仓库锁定的 LLVM commit，并理解「为什么必须锁定」。

**操作步骤**：

1. 打开 [cmake/IncludeLLVM.cmake:29](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/cmake/IncludeLLVM.cmake#L29)，记录 `LLVM_BUILD_COMMIT_HASH` 的值（应为 `57109befac92811d2253109242ca6fa69c961fb2`）。
2. 用 git 查看本仓库最近 5 条提交信息：

   ```bash
   git log --oneline -5
   ```

3. 观察：最新提交信息里的「Breaking commit」hash 与第 1 步记录的 hash 是否吻合。
4. 阅读 [README.md:363-384](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/README.md#L363-L384)，用自己的话解释「为什么 CUDA Tile 要锁定到 LLVM 的具体 commit」。

**需要观察的现象**：第 2 步的输出里，最新提交形如 `e01244d [LLVM-FIX] Breaking commit 57109befac92 ...`，前缀 `57109befac92` 与第 1 步的 hash 前缀一致。

**预期结果**：你能写出类似——「因为 MLIR/LLVM 上游会引入 breaking change，CUDA Tile 作为下游必须针对每个 breaking commit 发布一个修复版本，所以构建时必须用 CMake 里锁定的那个具体 LLVM commit，否则可能编不过或行为异常。」

> 待本地验证：`git log` 的确切输出取决于你本地的克隆状态，但提交 hash 与 message 应与上面描述一致。

#### 4.3.5 小练习与答案

**练习 1**：CUDA Tile 版本号 `13.3.0` 中的 `13.3` 和 `0` 分别代表什么？

**参考答案**：`13.3` 对应 CUDA Toolkit 13.3；`0` 是该 toolkit 版本下的第 1 个（Patch 从 0 起）开源发布。

**练习 2**：假如某天 MLIR 上游又来一个 breaking commit，CUDA Tile 项目通常会如何应对？

**参考答案**：发一个新的 `[LLVM-FIX]` 版本（例如 `v13.1.5`），把 `LLVM_BUILD_COMMIT_HASH` 更新为新 commit，从而开启一个新的兼容区间。

**练习 3**：为什么 toolkit 的 patch 版本（如 `13.1.0 → 13.1.1`）通常不需要 CUDA Tile 发新版本？

**参考答案**：因为 toolkit 的 patch 版本一般不含新功能，也就很少影响 CUDA Tile 的开源组件；若有影响，会合并进下一次开源 patch 发布（见 [README.md:355-359](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/README.md#L355-L359)）。

---

## 5. 综合实践

设计一个把本讲三个模块串起来的小任务：**画一张「CUDA Tile IR 全景图」并配文字解说**。

**任务要求**：

1. 在一张纸上（或文档里）画出三块内容：
   - **数据流图**：从「文本 MLIR」到「GPU 运行」的完整路径，标注 AoT 与 JIT 两条分支（参考 4.1.2）。
   - **组件关系图**：四大组件的方框 + 连线，体现 Dialect 是核心、Bytecode 串起序列化、Python Bindings 用于构造、Conformance Tests 横切校验（参考 4.2.2）。
   - **版本对应表**：写出当前仓库 HEAD、CUDA Toolkit 对齐版本、锁定的 LLVM commit 三者的值（参考 4.3.3）。
2. 在图旁写一段 **150 字以内** 的解说，向一个完全没听过 CUDA Tile 的同事讲清楚「这是什么、能干什么」。
3. 最后在图上标注 **下一步你最想深入的一块**（例如「我想搞懂 `tile<4x8xf32>` 这种类型到底怎么定义」），作为后续学习的入口。

**评判标准**：

- 数据流图必须同时包含 AoT 和 JIT 两条路线，且都最终指向「GPU 运行」。
- 组件关系图必须体现「测试是横切关注点」。
- 版本对应表里 LLVM commit 必须是 `57109befac92811d2253109242ca6fa69c961fb2`，CUDA Toolkit 对齐版本应写作 **13.1**（README 当前所写）。

> 这个综合实践无需运行代码，但完成它意味着你已经建立了本讲要求的全局心智模型。

---

## 6. 本讲小结

- **CUDA Tile IR 是什么**：一个基于 MLIR 的中间表示与编译基础设施，专注 NVIDIA 张量核上的分块计算与显存层次抽象。
- **输入与输出**：输入是文本 MLIR（或经 Python 绑定构造的 IR），输出是字节码 `.tilebc`，可 AoT 编成 cubin 或被驱动 JIT 运行。
- **四大核心组件**：CUDA Tile Dialect（语义核心）、Python Bindings（程序化构造）、Bytecode（序列化格式）、Conformance Test Suite（规范校验），分别落在 `include/lib` 的 Dialect/Bytecode、`python/`、`test/` 目录。
- **版本规则**：`Major.Minor` 对应 CUDA Toolkit，`Patch` 独立计开源发布。
- **LLVM 锁定**：必须用 `cmake/IncludeLLVM.cmake` 里锁定的具体 LLVM commit 构建，当前为 `57109befac92...`；每个 breaking commit 对应一个 `[LLVM-FIX]` 版本，形成兼容区间。
- **与同类区别**：CUTLASS 是源码库、Triton 是语言+编译器，而 CUDA Tile IR 是一个 IR + 编译基础设施，更适合作为前端 lowering 的目标。

---

## 7. 下一步学习建议

本讲只建立了全局认知，接下来建议：

1. **跑通工具链**：进入下一篇 [u1-l2 仓库结构与 CMake 构建系统](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/CMakeLists.txt)，亲手用 CMake 配置并构建一次项目，把 `check-cuda-tile` 测试跑起来。
2. **亲手跑一个内核**：随后读 [u1-l3 工具链与端到端示例](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/README.md#L213)，复现 README 的 print 示例，把 4.1.2 的数据流图变成真实命令。
3. **想深入类型**：如果综合实践里你标注的「最想深入」是类型系统，可以提前浏览 [include/cuda_tile/Dialect/CudaTile/IR/Types.td](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Types.td)，正式讲解在第 3 单元。

> 提醒：本讲引用的 README 明确写「与 CUDA Toolkit 13.1 对齐」，但仓库 HEAD（`e01244d`）实际已位于 13.3.0 之后的 `[LLVM-FIX]` 提交上。后续讲义在涉及具体版本号时会以源码实际状态为准；本讲作为概览，沿用 README 的 13.1 表述。
