# 项目定位与整体架构

## 1. 本讲目标

本讲是整本学习手册的第一篇。读完本讲，你应该能够：

- 用一句话说清楚 **tilelang-ascend 是什么**，它和上游 tile-lang、TVM 是什么关系。
- 说清楚华为昇腾（Ascend）NPU 的**片上存储层级**（global / L1 / Unified Buffer / L0A / L0B / L0C），并能把它们和 GPU 的三级存储（global / shared / register）对应起来。
- 区分本仓库的**两条后端技术路线**：Ascend C（`ascendc`）与 PTO（`pto`）。
- 区分 tile-lang 提供的**三层编程抽象**：Beginner / Developer / Expert，以及它们各自面向的用户。
- 从全局理解一次算子调用「Python DSL → 编译 → 在 NPU 上跑出结果」经历了哪些阶段，为后面逐篇深入打下基础。

本讲偏概念与全景，几乎不涉及具体语法细节，目的是先建立正确的「心智模型」。

## 2. 前置知识

本讲尽量从零讲起，但如果你具备以下背景会更容易理解：

- **会用 GPU 写一点 CUDA 或看过一两个 kernel**：本讲大量使用 GPU 存储层级来类比 Ascend，有过 `shared memory`、寄存器、`__syncthreads()` 的概念会非常顺。
- **知道什么是矩阵乘（GEMM）**：本讲的示例算子是 GEMM，知道 \( C = A \times B\)（其中 \(A\in\mathbb{R}^{M\times K}\)、\(B\in\mathbb{R}^{K\times N}\)、\(C\in\mathbb{R}^{M\times N}\)）即可。
- **了解「领域特定语言（DSL）」这个词**：DSL 指为某一领域专门设计的语言，这里是为「AI 算子开发」专门设计的语言。
- **基本了解编译器的「前端 / 中间表示 / 后端」概念**：知道源代码会先变成某种中间表示（IR），再被翻译成机器能执行的代码即可。本仓库的中间表示基于 TVM 的 TensorIR。

如果以上都还不熟悉也没关系，本讲会用通俗语言把必要概念补齐。

## 3. 本讲源码地图

本讲涉及的「源码」主要是项目里最顶层的说明性文档和入口示例，它们是你认识整个项目最快的入口：

| 文件 | 作用 | 本讲用来看什么 |
| :--- | :--- | :--- |
| [README.md](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/README.md) | 项目主页，定位、安装、示例、特性总览 | 项目一句话定位、两条后端路线、GPU↔NPU 存储映射 |
| [docs/get_started/overview.md](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/get_started/overview.md) | tile-lang 总体介绍（继承自上游） | 三层编程抽象、整体编译流程 |
| [docs/TileLang-Ascend Programming Guide.md](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md) | 官方编程手册（中文，最权威） | 第 1 节 TileLang 介绍、编译运行流程、存储层级 |
| [examples/gemm/example_gemm.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm.py) | 最简 GEMM 示例，可一键运行 | 看一个真实 kernel 长什么样，建立感性认识 |

> 提示：本仓库当前处于 `ascendc_pto` 分支，这正是「Ascend C + PTO」两条路线并存的分支。本讲所有永久链接都指向当前 HEAD（`ee60e122`）。

## 4. 核心概念与源码讲解

### 4.1 tilelang-ascend 是什么：定位、价值与两条后端路线

#### 4.1.1 概念说明

先给结论：**tilelang-ascend 是一个面向华为昇腾 NPU 的高性能算子开发 DSL**。

它有三个关键身份：

1. **它是 tile-lang 的一个专用变体（variant）**。tile-lang 本身是一个面向 GPU 等加速器的 tile（数据块）级编程 DSL；tilelang-ascend 把它「搬」到了昇腾 NPU 上，并针对昇腾硬件做了专门优化。
2. **它建立在 TVM 编译基础设施之上**。也就是说，你的 Python kernel 代码会被翻译成 TVM 的中间表示（TensorIR），再经过一系列编译 pass，最终生成能在 NPU 上跑的代码。
3. **它的目标是让开发者既能享受 Pythonic 的高生产率，又不牺牲 NPU 上极致的底层性能**——涵盖 GEMM、向量运算、注意力机制（Attention）等典型 AI 算子。

README 开篇就用一句话给出了官方定位（见下方源码精读）。

它解决的核心矛盾是：**手写 Ascend C（华为官方的 C++ 算子开发语言）虽然性能可控，但开发门槛高、可读性差、难以跨架构复用**；而直接用高层框架（如 PyTorch）又拿不到极致性能。tilelang-ascend 用接近 Python 的语法让你描述算子，由编译器自动完成内存搬运、同步插入、缓冲复用等底层脏活，同时保留足够的「手动挡」接口供你压榨性能。

#### 4.1.2 核心流程：一条算子从代码到结果

从宏观看，一次算子调用走过这条链路：

```text
Python kernel (@T.prim_func)
        │  tile-lang 前端解析
        ▼
TensorIR（TVM 中间表示）
        │  多轮 lowering pass（针对昇腾硬件降级/优化）
        ▼
Ascend C / PTO 设备代码（C++ 源码）
        │  毕昇编译器（bisheng）编译
        ▼
动态链接库 .so
        │  ctypes 加载、封装为 Python 可调用对象
        ▼
用户调用 func(a, b) → 在 NPU 上执行 → 返回结果
```

这条链路里有三个「身份」会在本手册反复出现，先记名字：

- **前端（language）**：你写的 Python 语法，如 `T.Kernel`、`T.copy`、`T.gemm_v0`。
- **编译 pass（transform）**：把高层 IR 一层层降级、优化的 C++ pass，如自动同步插入、缓冲复用、存储重写。
- **后端（codegen / runtime）**：把 IR 翻译成 Ascend C 或 PTO 代码，再用毕昇编译器编成 `.so`，最后加载运行。

> 这条链路的具体细节会在「第 5 讲 JIT 与运行总流程」以及「第 6 单元 编译器后端」逐层展开。本讲只需建立这条粗链路即可。

#### 4.1.3 源码精读

**(1) 项目一句话定位**

README 第 12 行给出了最权威的一句话定义（摘录关键部分）：

> "Tile Language Ascend (**tilelang-ascend**) is a specialized variant of the tile-lang domain-specific language, specifically optimized for Huawei Ascend NPU ..."

源码位置：

[README.md:12](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/README.md#L12) —— 这一行同时说明了三件事：tile-lang 变体、面向华为昇腾 NPU、建立在 tile-lang 语法与 TVM 之上。它紧接着点出了两条后端技术路线。

**(2) 两条后端技术路线**

同一行 README 明确写：

> "The compiler backend supports two technical routes: Ascend C & PTO and AscendNPU IR."

需要特别注意，这里描述的是「项目整体支持的方向」，而**当前 `ascendc_pto` 分支聚焦的是 Ascend C 与 PTO 两条路线**：

- **Ascend C（`ascendc`）**：基于华为官方 Ascend C 编程模型，生成标准的 AscendC C++ 代码，由毕昇编译器以 `-xasc` 方式编译。它是「与官方生态对齐」的稳妥路线。
- **PTO（`pto`）**：一条更新的代码生成目标，生成更贴近底层指令的 PTO IR。PTO 路线的一个重要用途是支持 A5 平台的 camodel 软件仿真（本手册第 7 单元会讲）。

编程手册也佐证了「生成 Ascend C 代码 → 毕昇编译」这一事实：

[docs/TileLang-Ascend Programming Guide.md:177-178](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L177-L178) —— 这两行说明编译阶段会先生成 AscendC 代码，再用毕昇编译器编成动态库。

> 小结：本讲你只需记住「Ascend C 是稳妥主线、PTO 是更新的、可仿真」即可，两者的 codegen 实现差异留到「第 6 单元」讲。

#### 4.1.4 代码实践

**实践目标**：亲手用官方措辞建立项目定位的「肌肉记忆」，并把它翻译成自己的话。

**操作步骤**：

1. 打开 [README.md](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/README.md)，找到第 12 行那段定位描述。
2. 阅读这段话，提炼出 4 个关键词填空：tile-lang 的「____」、面向「____」硬件、建立在「____」之上、两条后端路线是「____」。
3. 用**你自己的话**写一段不超过 80 字的中文，回答：「tilelang-ascend 相比手写 Ascend C，价值到底在哪里？」

**需要观察的现象**：你会注意到官方定位特别强调 "productivity without sacrificing ... low-level optimizations"，即「生产率」与「底层性能」两头都要。

**预期结果**：你的填空答案大致是「专用变体 / 华为昇腾 NPU / tile-lang 语法 + TVM / Ascend C 与 PTO」。价值描述应至少包含「Pythonic 语法、自动化的内存搬运与同步、保留底层手动挡」这类要素。本实践为阅读理解型，无需运行命令。

#### 4.1.5 小练习与答案

**练习 1**：tilelang-ascend 是「从头造的全新语言」还是「tile-lang 的变体」？它复用了 tile-lang 的什么、又新增了什么？

> **参考答案**：它是 tile-lang 的专用变体，复用了 tile-lang 的 Pythonic 语法与 tile 级抽象（以及 TVM 编译基础设施），新增的是面向华为昇腾 NPU 的 lowering pass、Ascend C / PTO 双 codegen、以及 L1/UB/L0A/L0B/L0C 等昇腾专属存储与同步原语。

**练习 2**：Ascend C 与 PTO 这两条路线，哪一条更适合「在没有真实 A5 硬件时做软件仿真」？

> **参考答案**：PTO。PTO 路线支持 A5 平台的 camodel 仿真（详见编程手册 2.4 节及本手册第 7 单元）。

---

### 4.2 Ascend NPU 硬件模型与存储层级（与 GPU 类比）

#### 4.2.1 概念说明

要理解 tilelang-ascend 的所有原语，第一步是搞懂 Ascend NPU 的**片上存储层级**。如果你写过 CUDA，最快的理解方式是「类比映射」。

GPU 的经典三级存储是：

| GPU 存储 | 特点 |
| :--- | :--- |
| **global memory**（显存/HBM） | 容量大、速度慢，所有线程块都能访问 |
| **shared memory** | 片上高速存储，速度远快于 global，由同一个线程块内的线程共享 |
| **register**（寄存器） | 最快、最小，属于单个计算单元 |

Ascend NPU 也有类似的三级结构，只是名字不同、而且因为 NPU 同时有 **Cube（矩阵）核** 和 **Vector（向量）核** 两类计算单元，存储也分得更细：

| Ascend 存储 | 对应 GPU | 用途 |
| :--- | :--- | :--- |
| **global memory（GM/HBM）** | global memory | 容量大、速度慢，主机与设备、Cube 与 Vector 之间都通过它交换数据 |
| **L1 Buffer**（Cube 核上） | ≈ shared memory | Cube 计算时的高速片上缓存 |
| **Unified Buffer / UB**（Vector 核上） | ≈ shared memory | Vector 计算时的高速片上缓存 |
| **L0A / L0B Buffer** | ≈ register（输入） | Cube 矩阵计算的左矩阵/右矩阵输入 |
| **L0C Buffer** | ≈ register（输出/累加器） | Cube 矩阵计算的结果与累加器 |

这里有两个「反直觉但关键」的点，先记住，后面单元会反复用到：

1. **L1 和 UB 在 tile-lang 里被抽象成同一个层级（`shared`）**。你写 `alloc_shared` 时不用自己判断该放 L1 还是 UB——编译器会根据上下文（这块数据是给 Cube 用还是 Vector 用）自动推断。
2. **L0A / L0B / L0C 也被抽象成同一个层级（`fragment`）**。同理，编译器自动判断。

此外，Ascend 每个 **AI Core** 内含 **两个 Vector 计算单元**，所以一个 Tile 还能再切成两个 sub-tile（用 `VEC_NUM=2`）来充分利用向量算力。这点在后面的 elementwise / attention 例子里会用到。

#### 4.2.2 核心流程：一个 GEMM Tile 的数据旅程

以 GEMM 为例，一个 tile 的数据在存储层级间的移动路径（也叫「数据搬运矩阵」）是理解整个编程模型的核心：

```text
GM ──(DataCopy)──▶ L1 ──(搬运)──▶ L0A / L0B
                                       │
                                   Cube 计算（MMA）
                                       ▼
                  GM ◀──(写回)──── L0C（累加器）
```

官方手册给出的搬运矩阵（节选）如下：

| src | dst | 说明 |
| --- | --- | --- |
| GM | L1 | 全局内存 → L1 Buffer |
| L1 | L0A | L1 → L0A，Cube 的左矩阵 |
| L1 | L0B | L1 → L0B，Cube 的右矩阵 |
| L0C | GM | 累加器结果 → 全局内存 |
| GM | UB | 全局内存 → Unified Buffer（向量计算用） |
| UB | GM | Unified Buffer → 全局内存 |
| UB | UB | UB 之间拷贝 |
| UB | L1 | Unified Buffer → L1 |

可以看到，无论是 `GM↔L1`、`L1→L0A/L0B`、`L0C→GM`，还是 `GM↔UB`，都由同一个 `T.copy` 原语来表达——编译器会根据 src/dst 的 scope 自动选择底层对应的 AscendC 指令。

#### 4.2.3 源码精读

**(1) GPU ↔ Ascend 存储映射（最权威出处）**

README 的「Comparison with NVIDIA Backend Implementation」一节直接给出了三级存储的映射关系：

[README.md:156-161](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/README.md#L156-L161) —— 这里写明：`global↔global`、`shared↔L1(cube) 与 UB(vector)`、`register↔L0A/B/C`。这是整本手册存储类比的「基准参照」。

紧接着一行说明 tile-lang 提供了与 GPU 版本类似的分配原语：

[README.md:163-164](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/README.md#L163-L164) —— `alloc_{L1/ub/...}` 这些原语让你像 GPU 那样显式分配片上内存。

**(2) shared / fragment 两个抽象层 → 昇腾物理存储**

编程手册明确解释了两个抽象如何映射到昇腾的物理存储：

[docs/TileLang-Ascend Programming Guide.md:597](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L597) —— 这一行说明：shared 层对应 **L1 Buffer（Cube 用）和 Unified Buffer（Vector 用）**，fragment 层对应 **L0A/L0B/L0C**，且都由编译器按上下文自动识别，无需用户显式指定具体是哪一种。

**(3) 完整搬运矩阵**

[docs/TileLang-Ascend Programming Guide.md:627-636](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L627-L636) —— 这张表是上面「数据搬运矩阵」的官方版本，是后续所有搬运实践的查阅依据。

**(4) 每个 AI Core 有两个 Vector 单元**

[docs/TileLang-Ascend Programming Guide.md:155](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L155) —— 这一行解释了 `VEC_NUM`：Ascend 每个 AI Core 含两个 Vector 计算单元，因此 Tile 可进一步切成两个 sub-tile。

#### 4.2.4 代码实践

**实践目标**：把 GPU 三级存储到 Ascend 片上存储的对应关系画成一张图，建立空间感。

**操作步骤**：

1. 准备纸笔或任意画图工具。
2. 横向画两组并列的「存储金字塔」：左边 GPU（自下而上：global → shared → register），右边 Ascend（自下而上：GM → L1/UB → L0A/L0B/L0C）。
3. 用箭头把对应层级连起来（global↔GM、shared↔L1+UB、register↔L0A/L0B/L0C）。
4. 在右侧金字塔上额外标注：L1 属于 **Cube 核**、UB 属于 **Vector 核**；并在 L0C 旁标注「累加器」。

**需要观察的现象**：你会发现 Ascend 右侧的「中间层」比 GPU「胖」——因为 NPU 把 Cube 和 Vector 拆成了两类核，各自有独立的片上高速缓存（L1 与 UB）。

**预期结果**：得到一张清晰的对应关系图，能与 [README.md:156-161](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/README.md#L156-L161) 的描述对得上。本实践为画图型，无需运行命令，但建议你把它拍照或保存下来——后面讲到 `alloc_shared` / `alloc_fragment` 时会反复回看。

#### 4.2.5 小练习与答案

**练习 1**：在 tile-lang 里，`alloc_shared` 分配出来的内存，到底是 L1 还是 UB？

> **参考答案**：不固定。`shared` 是一个抽象层，可能落到 L1（当数据给 Cube 用时）或 Unified Buffer（当数据给 Vector 用时），由编译器按上下文自动推断。这就是「用户无需显式指定」的设计。

**练习 2**：为什么 Ascend 的 L0C 被类比成 GPU 的 register 而不是 shared memory？

> **参考答案**：L0C 容量很小、访问最快，专门用于存放 Cube 矩阵计算的累加结果，属于某个计算单元的「私有」高速存储，这与 GPU 寄存器（小、快、私有）的定位一致；而 shared memory 是片上可共享的中等容量存储，对应的是 L1/UB。

---

### 4.3 编译运行总流程：DSL 如何变成 NPU 上的执行结果

#### 4.3.1 概念说明

第 4.1 节我们画过一条粗链路。这里把它讲得更具体一点，因为「编译」和「运行」是 tilelang-ascend 的两条生命线。

tile-lang 的算子默认采用 **JIT（Just-In-Time，即时编译）** 模式：当你第一次调用被 `@tilelang.jit` 装饰的函数时，它才会真正去编译——根据你传入的张量维度、数据类型，动态生成对应的 Ascend C 代码，再编译成 `.so`，加载执行。

为什么用 JIT 而不是静态预编译？因为算子的最优实现高度依赖**具体的形状和类型**（例如不同的 M/N/K、float16 还是 float32）。JIT 能针对当前输入「量身定制」一份代码，拿到最佳性能。代价是「第一次调用会慢一些」（编译耗时），后续调用则复用已编译的 `.so`，几乎无开销。

#### 4.3.2 核心流程：编译阶段 + 运行阶段

官方手册把整个过程清晰地拆成了「编译」和「运行」两阶段：

**编译阶段**（3 步）：

1. **多轮 lowering**：把 tile-lang 前端代码根据 NPU 硬件特性做多级降级，生成针对昇腾优化的 TensorIR。
2. **Ascend C 代码生成**：基于 TensorIR，用专门的 Ascend Codegen 模块生成 Ascend C 代码。
3. **动态库编译**：用**毕昇编译器（bisheng）**把 Ascend C 代码编译成动态链接库（`.so`）。

**运行阶段**（3 步）：

1. **库文件加载**：通过 `ctypes` 把 `.so` 加载进 Python。
2. **函数封装**：把算子封装成可调用的 Python 函数对象。
3. **执行调用**：用户像普通 Python 函数那样调用 `func(a, b)`，传入张量即可在 NPU 上执行并拿到结果。

> 这 6 步的「具体实现文件」（`tilelang/jit/`、`tilelang/engine/lower.py`、`src/target/codegen_ascend*.cc`）会在第 5 讲和第 6 单元逐层打开。本讲只要求你记住这条流程的名字和顺序。

#### 4.3.3 源码精读

**(1) 编译 + 运行两阶段（最权威出处）**

[docs/TileLang-Ascend Programming Guide.md:171-182](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L171-L182) —— 这段是上面「编译 3 步 + 运行 3 步」的直接出处。第 178 行明确写了「通过毕昇编译器（bisheng）将 AscendC 代码编译成动态链接库」。

**(2) JIT 的价值说明**

[docs/TileLang-Ascend Programming Guide.md:186-192](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L186-L192) —— 这段解释了为什么要 JIT：动态参数驱动定制代码生成、遵守硬件资源约束、运行时即时优化。第 192 行指出开发中通过 `@jit` 装饰器触发即时编译。

**(3) 真实可运行入口**

[examples/gemm/example_gemm.py:20-21](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm.py#L20-L21) —— 这两行是 JIT 的真实用法：`@tilelang.jit(out_idx=[-1])` 装饰 `matmul` 函数。`out_idx=[-1]` 表示最后一个参数 `C` 是输出张量。当你调用 `func(a, b)` 时就会触发上面整条编译+运行链路，最终在 NPU 上算出 `C`。

#### 4.3.4 代码实践

**实践目标**：通过阅读一个能跑通的真实示例，把「编译 + 运行」流程和真实代码对上号。

**操作步骤**：

1. 打开 [examples/gemm/example_gemm.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm.py)。
2. 定位第 20 行的 `@tilelang.jit(...)` 与第 65 行的 `c = func(a, b)`，确认这就是「装饰 → 调用触发编译」的入口。
3. （可选，需要昇腾环境）按 README「Run」一节执行：`cd examples/gemm && python example_gemm.py`，期望看到 `Kernel Output Match!`。
4. **如果你没有真实 NPU 环境**：跳过运行，改为「源码阅读型实践」——在第 56 行 `func = matmul(M, N, K, 128, 256, 64)` 处，对照第 4.3.2 节的 6 步流程，在心里标注每一步对应到代码的哪个位置（提示：`func(a,b)` 那一刻触发编译阶段 1~3，随后进入运行阶段 1~3）。

**需要观察的现象**：

- 若能运行：第一次调用 `func(a, b)` 会有明显的编译等待（打印了若干编译日志），第二次调用则很快。
- 若为阅读型：你能说清楚 `@jit`、`func = matmul(...)`、`func(a, b)` 三者各自处于「编译」还是「运行」阶段。

**预期结果**：

- 运行成功时终端打印 `Kernel Output Match!`（参见 [README.md:148-152](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/README.md#L148-L152)）。
- 无硬件时：能口头复述 6 步流程，并指出 `func(a, b)` 是触发点。本实践结果若无法在本地确认，请标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 tile-lang 默认用 JIT，而不是把所有形状的算子都预编译好？

> **参考答案**：因为算子的最优实现与具体形状/数据类型强相关，穷举所有组合既不现实也不优。JIT 针对当前输入「量身定制」代码，能拿到最佳性能；编译结果会被缓存，后续相同输入的调用几乎无额外开销。

**练习 2**：在「编译 → 运行」6 步中，是哪一步把 Ascend C 代码变成 `.so` 的？用什么工具？

> **参考答案**：编译阶段的第 3 步，使用毕昇编译器（bisheng）把 Ascend C 代码编译成动态链接库 `.so`（见编程手册第 178 行）。

---

### 4.4 三层编程抽象：Beginner / Developer / Expert

#### 4.4.1 概念说明

tile-lang 提供了**三层**编程接口，分别面向不同熟练度的用户。同一套语言里，你可以在不同抽象层之间自由切换甚至混用——这是 tile-lang 设计上很讨喜的一点。

| 抽象层 | 面向用户 | 特点 | 当前状态 |
| :--- | :--- | :--- | :--- |
| **Beginner（硬件无关）** | 只想写基本逻辑、不想碰硬件细节的人 | 完全屏蔽内存层级与硬件优化 | 尚未完全实现 |
| **Developer（硬件感知 + Tile 库）** | 了解 NPU 内存层级、关注性能的开发者 | 提供现成的 Tile Library 原语（搬运/gemm/reduce 等），不用管底层线程细节 | **主力模式** |
| **Expert（硬件感知 + 线程级原语）** | 深入理解 Cube/Vector/MTE/UB 等底层特性的高手 | 暴露细粒度同步、低级操作接口、寄存器级控制，性能上限最高 | 完整支持 |

理解这三层，关键是把握一个**取舍轴**：

```text
  生产率高、门槛低                       性能上限高、控制力强
  Beginner ──────────▶ Developer ──────────▶ Expert
  (屏蔽硬件)            (Tile 库原语)          (线程级/同步/寄存器)
```

- **Developer 模式**是大多数算子开发的「甜点区」：你写 `T.copy`、`T.gemm_v0`、`T.reduce_max` 这些高层原语，编译器帮你自动推断存储 scope、自动插入核内同步、自动做缓冲复用。
- **Expert 模式**则是「手动挡」：你显式写 `T.Scope("C")` / `T.Scope("V")` 划分 Cube/Vector 执行域，用 `T.set_flag` / `T.wait_flag` 手写多级流水同步，用 `T.mma` 直接操作 L0A/L0B/L0C。它给你最大灵活性，但也最复杂。
- 两者还能**混合编程**：在 Developer 模式的主体里，对性能关键的段落切到 Expert 接口。

#### 4.4.2 核心流程：Cube/Vector 协同与作用域

无论哪一层，只要你写的是真实算子，就绕不开 **Cube 与 Vector 的协同**。这是 Ascend 架构的本质特征：

- **Cube 核**负责矩阵乘（GEMM/MMA）这类密集线性代数。
- **Vector 核**负责逐元素运算（exp、softmax、reduce 等）。
- A2/A3 的 Cube 与 Vector **不能直接交换数据**，必须经 **global memory / L2 cache** 中转。

针对这一约束，两个抽象层给出了不同的写法：

```text
                    Cube 计算结果 (L0C)
                           │
            （必须经 GM/L2 中转，不能直连）
                           ▼
                    Vector 后续处理 (UB)

  Developer 模式：编译器自动分离 Cube/Vector scope、自动插入同步
  Expert    模式：你用 T.Scope("C")/("V") 显式划分，用 set_cross_flag/wait_cross_flag 手动同步
```

这就是为什么后面的单元会出现「自动 CV 分离」「跨核流水」「workspace 消除」这些 Ascend 专属机制——它们都是为了让 Cube↔Vector 的数据交换更高效、更省心。

#### 4.4.3 源码精读

**(1) 三层抽象的官方定义（中文）**

[docs/TileLang-Ascend Programming Guide.md:11-23](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L11-L23) —— 这段逐条列出了三层抽象：第 15 行注明 Beginner「当前还不支持」；第 17-19 行讲 Developer（Tile 库 + 现成原语，无需深入线程细节）；第 21-23 行讲 Expert（Cube/Vector/MTE/Unified Buffer 等底层控制，细粒度同步）。

**(2) 上游 overview 的同一套三层划分（英文）**

[docs/get_started/overview.md:17-30](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/get_started/overview.md#L17-L30) —— 这是 tile-lang 上游对三层接口的原始描述，与本仓库手册一一对应，可对照阅读以加深理解。

**(3) Cube/Vector 自动分离 vs 显式同步**

[README.md:178-179](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/README.md#L178-L179) —— 这两行是理解第 4.4.2 节「两个模式两种写法」的直接依据：Developer 模式下编译器自动分离 Cube/Vector scope 并插入同步；Expert 模式下用 `T.Scope("C")`/`T.Scope("V")` 显式划分，用 `T.set_cross_flag` / `T.wait_cross_flag` 管理同步。

#### 4.4.4 代码实践

**实践目标**：在真实示例里辨认出 Developer 与 Expert 的痕迹，理解「同一份代码可以混合抽象层」。

**操作步骤**：

1. 打开 [examples/gemm/example_gemm.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm.py)。
2. 找到这几行并标注它们属于哪个抽象层：
   - 第 35-36 行 `T.alloc_L1(...)`：显式指定 L1 存储（偏 Expert 风格的分配原语）。
   - 第 40 行 `with T.Scope("C"):`：显式声明 Cube 执行域（Expert 接口）。
   - 第 43-44 行 `T.copy(...)`：数据搬运（通用原语）。
   - 第 47 行 `T.gemm_v0(...)`：矩阵乘（Developer 风格的高层接口）。
3. 思考：这份最简 GEMM 其实**混用了多层抽象**——既有 Expert 的 `T.Scope`，也有 Developer 的 `T.gemm_v0`。这正是 tile-lang「可在同一 kernel 内混合抽象层」的体现。

**需要观察的现象**：你会发现即便是最简单的示例，也已经在用 `T.Scope("C")` 这种 Expert 风格的写法来包裹计算域。

**预期结果**：你能给上述 4 处代码各贴上一个抽象层标签，并解释「为什么 tile-lang 允许这样混用」（因为这三层共用同一套前端、会统一降到 TensorIR）。本实践为源码阅读型，无需运行命令；若不确定标注是否准确，标注「待确认」。

#### 4.4.5 小练习与答案

**练习 1**：Beginner、Developer、Expert 三层中，哪一层目前还不支持？日常算子开发主要用哪一层？

> **参考答案**：Beginner 层「当前还不支持」（编程手册第 15 行）。日常算子开发主要用 **Developer 层**，它提供 Tile Library 现成原语、门槛适中、性能足够好；对极致性能段落再切到 Expert 层。

**练习 2**：在 Expert 模式下，你如何告诉编译器「这段代码在 Cube 核上跑」「那段在 Vector 核上跑」？

> **参考答案**：用 `T.Scope("C")` 包裹 Cube 执行域、用 `T.Scope("V")` 包裹 Vector 执行域（见 README 第 179 行）。

**练习 3**：Cube 计算的结果如何传递给 Vector 继续处理？为什么不能直接传？

> **参考答案**：A2/A3 的 Cube 与 Vector 不能直接交换数据，必须经 global memory / L2 cache 中转（README 第 179 行）。Developer 模式下由编译器自动处理这一中转（含自动同步与 workspace 消除），Expert 模式下需手动管理同步与搬运。

---

## 5. 综合实践

本实践把本讲四个模块串起来，产出一份「一页纸项目认知卡」。

**任务**：假设你要给一位**只懂 CUDA、从没接触过昇腾**的同事用 10 分钟介绍 tilelang-ascend。请完成下面 4 个交付物：

1. **一句话定位**（对应 4.1）：用自己的话写一句不超过 30 字的中文定位，必须包含「tile-lang 变体」「昇腾 NPU」「TVM」三要素之一以上。
2. **存储对应图**（对应 4.2）：画出 GPU 三级存储到 Ascend 片上存储的对应关系图（global↔GM、shared↔L1/UB、register↔L0A/L0B/L0C），并标注 L1 属 Cube、UB 属 Vector。
3. **流程速记表**（对应 4.3）：列出「编译 3 步 + 运行 3 步」共 6 步，并在旁边标出触发它们的那行代码（`func(a, b)`）。
4. **抽象层取舍说明**（对应 4.4）：用一句话说明 Developer 与 Expert 的区别，并举 `example_gemm.py` 中「混合使用两层」的一个例子（如 `T.Scope("C")` 是 Expert、`T.gemm_v0` 是 Developer）。

**验收标准**：

- 交付物 1 能让 CUDA 同事立刻明白「这是个 DSL，不是手写 Ascend C」。
- 交付物 2 的图与 [README.md:156-161](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/README.md#L156-L161) 一致。
- 交付物 3 的 6 步与 [docs/TileLang-Ascend Programming Guide.md:171-182](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L171-L182) 一致，且能指出 bisheng 是编译 `.so` 的工具。
- 交付物 4 能说清「同一 kernel 可混合抽象层」这一点。

> 提示：这份「认知卡」建议保存下来，它是你后续阅读所有讲义时的「速查表」。

## 6. 本讲小结

- **tilelang-ascend 是 tile-lang 面向华为昇腾 NPU 的专用变体**，建立在 tile-lang 语法与 TVM 编译基础设施之上，目标是兼顾 Pythonic 的开发生产率与 NPU 的极致性能。
- **存储三级类比**：global↔GM、shared↔L1(Cube)/UB(Vector)、register↔L0A/L0B/L0C；其中 shared 与 fragment 在 tile-lang 里是两个抽象层，编译器按上下文自动落到具体物理存储。
- **两条后端路线**：Ascend C（`ascendc`，稳妥主线）与 PTO（`pto`，更新且支持 A5 仿真）；二者都先降级到 TensorIR 再 codegen，最后由**毕昇编译器（bisheng）**编译成 `.so`。
- **三层编程抽象**：Beginner（尚未支持）/ Developer（主力，Tile 库原语）/ Expert（线程级、同步、寄存器），且**可在同一 kernel 内混合使用**。
- **本质特征**：Ascend 的 Cube 与 Vector 经 GM/L2 中转交换数据，由此衍生出自动 CV 分离、跨核流水、workspace 消除等后续单元的核心机制。
- **执行模型**：`@tilelang.jit` 装饰 → 调用时 JIT 触发「多轮 lowering → Ascend C 代码生成 → bisheng 编 `.so` → ctypes 加载 → 执行」。

## 7. 下一步学习建议

本讲建立了全景认知，接下来建议：

1. **动手装环境、跑通第一个算子**：进入本手册第 2 讲《环境准备与安装构建》与第 4 讲《第一个算子：运行并读懂 GEMM》，亲手在（或仿真环境里）跑出 `Kernel Output Match!`。
2. **建立模块地图**：先读第 3 讲《仓库目录结构与模块地图》，搞清楚 `src/`、`tilelang/`、`examples/` 各自装了什么，避免在后续阅读源码时迷路。
3. **想立刻看「生成出来的代码」**：可先跳读第 5 讲《JIT 与运行总流程》，学会用 `get_kernel_source()` 打印 tile-lang 帮你生成的 Ascend C 代码——这是从「使用者」过渡到「理解者」最快的一步。
4. **延伸阅读**（非必须）：通读一遍 [docs/TileLang-Ascend Programming Guide.md](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md) 的第 1、2 节，作为本讲概念的双保险。
