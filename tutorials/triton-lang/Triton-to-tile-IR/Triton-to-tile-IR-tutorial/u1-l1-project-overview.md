# 项目总览：Triton 与 CUDA Tile IR 后端

## 1. 本讲目标

本讲是整套学习手册的第一篇，面向从未接触过本仓库的读者。学完本讲后，你应当能够：

- 用一句话说清 **Triton-to-tile-IR** 这个仓库是什么、它与上游 Triton 是什么关系；
- 说出 **CUDA Tile IR 后端（TileIR）** 与 Triton 默认的 **NVIDIA PTX 后端** 在定位、调优旋钮和内存模型上的关键差异；
- 知道 **`ENABLE_TILE` 开关**的作用，以及 TileIR 后端当前有哪些**已知的功能/性能限制**。

本讲只读不写代码，重点是建立全局认知，为后续单元（安装运行、目录结构、编译链路、MLIR 转换 Pass 等）打基础。

## 2. 前置知识

在开始之前，先建立三个通俗的概念。如果你已经很熟悉，可以跳过。

- **Triton 是什么？**
  Triton 是一门「语言 + 编译器」，用来写 GPU 上的高性能深度学习算子（比如矩阵乘、注意力）。它的目标是：比写 CUDA 生产力更高，又比一般 DSL 更灵活。你可以粗略把它理解成「用 Python 写、底层自动编译成 GPU 机器码」的 DSL。

- **什么是「后端（backend）」？**
  Triton 的前端（Python 内核代码）会先被翻译成一种中间表示 **TTIR**，再由「后端」把 TTIR 编译成可以在 GPU 上运行的产物（如 `cubin`）。不同的后端走不同的编译路径：上游 Triton 默认走 **NVIDIA PTX 后端**（TTIR → TTGIR → LLVM IR → PTX → cubin）。本仓库新增了**第二条后端路径**——CUDA Tile IR 后端。

- **什么是「Tile / Tile IR」？**
  在 GPU 编程里，一个「tile」通常指一小块被加载进片上高速存储（共享内存 / TMEM）的数据。**CUDA Tile IR** 是 NVIDIA 提出的一种以「tile」为一等公民的中间表示，相关方言在 [NVIDIA/cuda-tile](https://github.com/NVIDIA/cuda-tile) 维护。本仓库做的事情，就是把 Triton 的 TTIR 转换成 CUDA Tile IR，再交给 CUDA 13.1 自带的 `tileiras` 工具生成最终的 `cubin`。

> 一句话直觉：**上游 Triton 只有一条通往 GPU 的路（PTX 后端）；本仓库给它修了第二条新路（TileIR 后端），并设计了一个开关让你自由切换。**

## 3. 本讲源码地图

本讲是总览，主要阅读两份说明文档，不深入具体实现：

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/README.md) | **本仓库自己的说明**：说明这是孵化器仓库、如何启用 TileIR、已知功能/性能问题、变更清单（ChangeList）。本讲的主要信息来源。 |
| [README.original.md](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/README.original.md) | **上游 Triton 的原始 README**，被原样保留。用来对比「上游 Triton 是什么」与「本仓库改了什么」。 |

为了让后续单元有据可循，这里先预告几个本仓库新增/改动的实现文件（本讲暂不深入，仅作目录锚点）：

- 后端 Python 代码：[third_party/tileir/backend/](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/) （`compiler.py`、`driver.py`、`conf.py` 等）
- C++ 转换 Pass：[third_party/tileir/triton_tileir.cc](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/triton_tileir.cc) 及 `lib/`、`include/`
- 构建：[third_party/tileir/CMakeLists.txt](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/CMakeLists.txt) 与 [setup.py](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/setup.py)

---

## 4. 核心概念与源码讲解

本讲围绕三个最小模块展开：项目背景与定位、TileIR 后端与 PTX 后端的差异、已知功能与性能限制。

### 4.1 项目背景与定位

#### 4.1.1 概念说明

要理解本仓库，先看它的「身份」。它在 README 里给自己下了一个明确的定义——这是一个 **incubator repo（孵化器仓库）**，在上游 Triton 的基础上**新增**了 CUDA Tile IR 后端。

- **孵化器（incubator）**：意味着这是实验性、早期阶段的尝试，还没有合并进上游 Triton 主线，先独立成一个仓库进行验证。
- **不是 fork 替换**：它不是把 Triton 推翻重写，而是「上游 Triton + 新后端」。原有的 NVIDIA PTX 后端依然完整存在，TileIR 只是一个可开关的**第二条编译路径**。
- **硬件与 CUDA 版本**：仅使用 CUDA 13.1 提供的特性，且**只支持 Blackwell 架构 GPU**。

为了让你直观对比，本仓库特意把上游 Triton 的原始 README 保留为 `README.original.md`。对比两份文档，就能看出「上游是什么样」与「本仓库改了什么」。

#### 4.1.2 核心流程

整个项目可以这样建立认知：

```
        ┌─────────────────────────────────────────────┐
        │  上游 Triton (README.original.md)            │
        │  语言 + 编译器，默认 NVIDIA PTX 后端          │
        └─────────────────────────────────────────────┘
                          │
        本仓库「在其基础上」新增了第二条后端路径
                          ▼
        ┌─────────────────────────────────────────────┐
        │  Triton-to-tile-IR (README.md)              │
        │  孵化器仓库，新增 CUDA Tile IR 后端           │
        │  开关：ENABLE_TILE=1                         │
        │  依赖：CUDA 13.1（tileiras/ptxas/libnvvm）   │
        │  硬件：仅 Blackwell                          │
        └─────────────────────────────────────────────┘
```

注意一个关键点：**两个后端共享同一套前端（Python 内核 → TTIR）**，区别只在于 TTIR 之后的编译路径不同。这也就是为什么后续单元会讲到的 `make_ttir`（生成 TTIR）是共通的，而 `make_tileir` / `make_cubin`（走 TileIR 路径）是本仓库新增的。

#### 4.1.3 源码精读

仓库 README 的核心定位句在开头部分：

> This incubator repo adds the CUDA Tile IR backend to Triton. Users can enable the CUDA Tile IR backend by setting the environment variable `ENABLE_TILE=1`. The CUDA Tile IR backend in this repo only uses features available in CUDA 13.1.

[README.md:39-41](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/README.md#L39-L41) —— 这句话把项目定位说透了：**incubator repo（孵化器仓库）**、**adds（新增而非替换）**、**`ENABLE_TILE=1`（开关）**、**CUDA 13.1（特性上限）**。

硬件与依赖的限定：

[README.md:94-98](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/README.md#L94-L98) —— 说明只支持 Blackwell GPU，并依赖 CUDA 13.1 中的 `bin/tileiras`、`bin/ptxas`、`nvvm/lib64/libnvvm.so`，以及外部 [CUDA Tile IR dialect](https://github.com/NVIDIA/cuda-tile)。

作为对比，上游 Triton 对自己的描述是：

[README.original.md:23-25](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/README.original.md#L23-L25) —— "This is the development repository of Triton, a language and compiler for writing highly efficient custom Deep-Learning primitives." 这就是上游 Triton 的自我定位，本仓库正是在它之上「加后端」。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是让你亲眼确认两个仓库的「身份差异」。

1. **实践目标**：对比两份 README 的开头，确认「上游定位」与「本仓库定位」。
2. **操作步骤**：
   - 打开 [README.original.md](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/README.original.md)，找到上游 Triton 的自我介绍（"This is the development repository of Triton..."）。
   - 打开 [README.md](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/README.md)，注意它第 1 行写着「See The original Triton README for more details.」——这说明本仓库有意区分了「原始部分」和「新增部分」。
3. **需要观察的现象**：本仓库 README 的内容**几乎全是**关于「TileIR 后端如何启用、有何限制、改了哪些文件」，而上游内容被原样挪到了 `README.original.md`。
4. **预期结果**：你会确认本仓库不是「另一个 Triton」，而是「Triton + 一个可开关的新后端」。
5. 如果无法本地克隆仓库，可以直接在 GitHub 网页上阅读上述永久链接，效果相同（**待本地验证**：本地 `git log` 可看到本仓库的提交都是围绕 TileIR 的，与上游提交风格不同）。

#### 4.1.5 小练习与答案

**练习 1**：为什么本仓库要保留一份 `README.original.md`？

> **参考答案**：为了让读者清楚区分「上游 Triton 原本的内容」和「本仓库新增/改动的内容」。上游的自我定位、安装、构建说明被原样保留；而本仓库自己的 README 专注于描述 TileIR 后端的启用方式、限制与变更。这正体现了「incubator repo 在上游基础上新增」的定位。

**练习 2**：本仓库说自己「only uses features available in CUDA 13.1」，这对使用者意味着什么？

> **参考答案**：意味着要运行 TileIR 后端，必须安装 CUDA 13.1，因为编译依赖其中的 `tileiras`、`ptxas`、`libnvvm.so` 等工具；同时它只支持 Blackwell 架构 GPU。如果你的环境是更低版本的 CUDA 或更老的显卡，TileIR 后端无法工作。

---

### 4.2 TileIR 后端与 PTX 后端的差异

#### 4.2.1 概念说明

TileIR 后端和 PTX 后端都是「把 TTIR 变成 GPU 可执行产物」的编译路径，但二者在三个维度上明显不同：

1. **是不是默认**：PTX 后端是 Triton 3.6 的**默认后端**；TileIR 后端默认关闭，需要 `ENABLE_TILE=1` 手动开启。
2. **调优旋钮不同**：PTX 后端有 `range_*`、`static_ranges` 等一系列旋钮；TileIR 后端**不支持**这些，转而提供 `occupancy`、`num_ctas`、更宽的 `num_stages` 等自己的旋钮，且 `num_warps` 尚未暴露。
3. **内存模型不同**：TileIR 当前只支持**无序内存模型（unordered memory model）**，全局内存访问默认不保证顺序，需要时用 memory token 显式控制；这与 PTX 后端的语义不同。

理解这点很重要：**两个后端的旋钮语义不同，配置不能直接互相搬运。**

#### 4.2.2 核心流程

后端选择与差异可以这样表达：

```
用户设置 ENABLE_TILE
        │
        ├─ 未设置 / ENABLE_TILE=0 ──► 走默认 NVIDIA PTX 后端 (Triton 3.6)
        │                              旋钮: num_warps, range_*, static_ranges ...
        │
        └─ ENABLE_TILE=1 ──────────► 走 CUDA Tile IR 后端
                                       旋钮: occupancy(1-32), num_ctas, num_stages ...
                                       num_warps 暂不支持
                                       内存模型: unordered (需要 memory token 排序)
```

关于「配置不能直接搬运」，README 在 Helion 调优指南章节给出了明确的错误对照表：把 PTX 后端的 `range_unroll_factors`、`static_ranges` 等配置直接用于 TileIR 会报 `InvalidConfig` 错误，必须移除这些不支持旋钮，并推荐**从零开始 autotune**。

#### 4.2.3 源码精读

README 顶部就点明了默认后端是谁：

[README.md:3-5](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/README.md#L3-L5) —— "**Default backend is OSS PTX backend(Triton 3.6).**" 这一句直接说明 PTX 是默认后端，TileIR 需要显式启用。

旋钮差异的对照（PTX 后端独有、TileIR 不支持的旋钮清单）：

[README.md:29-33](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/README.md#L29-L33) —— 列出 `range_flattens`、`range_multi_buffers`、`range_num_stages`、`range_unroll_factors`、`range_warp_specializes`、`static_ranges`、`load_eviction_policies`、`indexing="block_ptr"` 等在 TileIR 上不可用，并建议「从零开始 autotune」。

TileIR 自己的关键旋钮：

[README.md:74-77](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/README.md#L74-L77) —— 引入新的 `occupancy` 提示（取 1 到 32 的整数，表示期望每个 SM 同时活跃的线程块数，默认 1），并强调 `num_ctas=2` 对 Blackwell 上的密集 dot 类负载很关键（开启 2CTA 模式 MMA）。

`num_warps` 与 `occupancy` 的取舍：

[README.md:100-101](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/README.md#L100-L101) —— "CUDA Tile IR in CUDA 13.1 doesn't support `num_warps` ... while CUDA Tile IR adds a new tuning attribute `occupancy`"，并指出实践中 `occupancy` 和 `num_ctas` 对 TileIR 性能至关重要。

#### 4.2.4 代码实践

这是一个**配置对比型实践**，目标是体会两个后端旋钮的不兼容。

1. **实践目标**：理解为什么不能把 PTX 后端的 autotune 配置直接搬到 TileIR。
2. **操作步骤**：
   - 阅读 [README.md:27-33](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/README.md#L27-L33) 的错误对照表。
   - 用自己的话列一张「两栏表格」：左栏写 PTX 后端独有、TileIR 不支持的旋钮，右栏写 TileIR 自己的关键旋钮。
3. **需要观察的现象**：你会发现两栏几乎**没有任何交集**——PTX 的 `range_*` / `static_ranges` 在 TileIR 一栏完全消失，而 TileIR 的 `occupancy` 在 PTX 一栏不存在。
4. **预期结果**：得到类似下表的结论。

| PTX 后端独有（TileIR 不支持） | TileIR 后端关键旋钮 |
|---|---|
| `range_unroll_factors` / `range_multi_buffers` / `range_flattens` / `range_num_stages` / `range_warp_specializes` | `occupancy`（1–32，默认 1） |
| `static_ranges` | `num_ctas`（dot 类负载推荐 2） |
| `load_eviction_policies`、`indexing="block_ptr"` | 更宽的 `num_stages` 范围 |

5. 这一步纯阅读，无需 GPU 即可完成。

#### 4.2.5 小练习与答案

**练习 1**：为什么 README 反复强调「不要直接复用 PTX 后端的配置」？

> **参考答案**：因为两个后端的调优旋钮集合和语义都不同。TileIR 不认识 PTX 的 `range_*`、`static_ranges` 等参数，直接套用会触发 `InvalidConfig` 错误；而 TileIR 真正起作用的是 `occupancy`、`num_ctas`、`num_stages` 这类旋钮，PTX 配置里往往没有或取值不合适。因此推荐从零开始 autotune。

**练习 2**：在 TileIR 后端下，`num_warps` 和 `occupancy` 的状态分别是什么？

> **参考答案**：`num_warps` 在 TileIR（CUDA 13.1）下**暂不支持/未暴露**；而 `occupancy` 是 TileIR **新增**的调优属性，取 1 到 32 的整数（默认 1），表示期望每个 SM 同时活跃的线程块数，是 TileIR 性能调优最关键的旋钮之一。

---

### 4.3 已知功能与性能限制

#### 4.3.1 概念说明

因为是孵化器阶段，TileIR 后端目前有不少**已知限制**。把它们分成两类来记：

- **功能性问题（可能算错结果）**：根源是 TileIR 当前只有**无序内存模型**——全局内存访问默认不排序。如果你的内核里存在「全局内存别名访问」或「跨 tile 块的数据传递（如 splitK/streamK）」，又没有显式加 memory token 排序，**可能得到错误结果**。
- **性能问题（能算对但慢）**：小 GEMM 性能差、使用旧式「tensor-of-pointer」load/store 的内核性能差、`num_warps` 未暴露导致大归约维度内核可能因寄存器溢出而变慢。
- **尚未支持的算子/特性**：README 列出了一份明确清单（如 `tt.gather`、`cf.cond_br`、`math.erf` 等），遇到这些算子的内核无法用 TileIR 编译。

> 直觉：**「能跑、能算对、跑得快」是三件不同的事**。TileIR 当前在某些场景还卡在「能跑但不保证算对」或「能算对但不够快」。

#### 4.3.2 核心流程

判断一个内核能否安全用 TileIR，可以按下面的检查清单走：

```
拿到一个 Triton 内核
     │
     ▼
(1) 是否用到「不支持算子清单」里的操作？
     ├─ 是 ──► 无法用 TileIR 编译（需改写或退回 PTX）
     └─ 否 ──► 继续
     ▼
(2) 是否存在全局内存别名访问，或跨 tile 块的数据传递(splitK/streamK)？
     ├─ 是 ──► 无序内存模型下可能算错，需要 memory token 显式排序
     └─ 否 ──► 结果正确性基本无忧
     ▼
(3) 是否是小 GEMM / 旧式指针 load-store / 大归约维度？
     ├─ 是 ──► 当前性能可能不佳（属于已知性能问题）
     └─ 否 ──► 适合尝试 TileIR，再用 occupancy/num_ctas 调优
```

#### 4.3.3 源码精读

无序内存模型与算错风险场景：

[README.md:54-62](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/README.md#L54-L62) —— 明确「CUDA Tile IR now supports only an unordered memory model」，并列出两种可能算错的场景：全局内存访问之间的别名、跨 tile 块的数据传递（如 splitK/streamK 需要全局内存锁逻辑做确定性归约）。

已知性能问题：

[README.md:69-72](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/README.md#L69-L72) —— 小 GEMM 性能差、旧式 tensor-of-pointer load/store 性能差、`num_warps` 未暴露（大归约维度的 XXXNorm 内核可能因寄存器溢出变慢）。

尚未支持的算子/特性清单：

[README.md:103-123](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/README.md#L103-L123) —— 列出了当前不支持或未完全支持的操作，包括 `tt.elementwise_inline_asm`、`cf.cond_br`、`cuda_tile.reduce`（仅允许纯操作）、`tt.gather`、`tt.unsplat`、`tt.dot_scaled`、`tt.extern_elementwise`、`tt.map_elementwise`、多种 TMA 特性（scatter/gather/reduce/rmw、任意 offset、load padding 默认值）、`math.erf`、`atomic_rmw`（不支持 bf16）、`atomic_cas`（不支持 bf16/fp16）、i64 索引的 memref 等。

另外，README 的 ChangeList 还指出 TileIR **默认关闭了两类数值优化**：

[README.md:85-86](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/README.md#L85-L86) —— 「CUDA Tile IR disables approx by default」「CUDA Tile IR disables FTZ by default」，分别用 `TILEIR_ENABLE_APPROX=1`、`TILEIR_ENABLE_FTZ=1` 重新开启。

> 名词解释：
> - **approx（近似计算）**：允许使用近似指令换取速度，但会牺牲一点数值精度。
> - **FTZ（Flush-To-Zero）**：把非规格化（极小的）浮点数直接当成 0 处理，可加快计算但改变了数值行为。TileIR 默认把这两者都关掉，即默认更「保守、更精确」。

#### 4.3.4 代码实践

这是本讲的核心实践，对应规格中的任务。这是一个**源码阅读 + 归纳型实践**。

1. **实践目标**：通读 README 的 **ChangeList** 与 **Known issues** 两节，用自己的话回答三个问题。
2. **操作步骤**：
   - 阅读 [README.md:79-92](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/README.md#L79-L92)（ChangeList）。
   - 阅读 [README.md:54-72](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/README.md#L54-L72)（Known functional / performance issues）。
   - 阅读 [README.md:103-123](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/README.md#L103-L123)（尚未支持的算子清单）。
3. **用你自己的话写出三段答案**：
   - **(A) 启用 TileIR 后端需要哪些环境变量？**
     最核心的是 `ENABLE_TILE=1`（这是后端开关）；若与 Helion 一起用，还需 `HELION_BACKEND=tileir`（见 [README.md:9-21](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/README.md#L9-L21)，且必须在 `import helion/triton` **之前**设置）。此外 `TILEIR_ENABLE_APPROX=1`、`TILEIR_ENABLE_FTZ=1` 是可选的「重新开启数值优化」开关，不是启用后端必需的。
   - **(B) TileIR 默认关闭了哪两类数值优化？**
     默认关闭 **approx（近似计算）** 和 **FTZ（Flush-To-Zero）**。这意味着 TileIR 默认走更精确、更保守的数值路径；要恢复这两类优化需分别设置 `TILEIR_ENABLE_APPROX=1` 和 `TILEIR_ENABLE_FTZ=1`。
   - **(C) 当前不支持哪些算子？**
     包括但不限于：`tt.elementwise_inline_asm`、`cf.cond_br`、`cuda_tile.reduce`（仅纯操作）、`tt.gather`、`tt.unsplat`、`tt.dot_scaled`、`cuda_tile.ftof`（不支持 rtz 模式）、`tt.extern_elementwise`、`tt.map_elementwise`、`math.erf`、`atomic_rmw`（不支持 bf16）、`atomic_cas`（不支持 bf16/fp16），以及 TMA 的 scatter/gather/reduce/rmw/任意 offset/load padding 默认值等特性，还有 i64 索引的 memref。
4. **需要观察的现象**：你会注意到 README 把限制分得很细——有「根本不支持」，也有「部分不支持」（如 `atomic_rmw` 只是 bf16 不支持）。归纳时要区分这两种程度。
5. **预期结果**：得到一份清晰的「启用方式 + 关闭项 + 不支持项」清单。本步骤纯阅读，无需 GPU。
6. 如果你想进一步动手（**待本地验证，需要 Blackwell GPU + CUDA 13.1**）：可以写一个用到上述任意不支持算子（如 `tl.where` 触发的条件分支、或 `math.erf`）的最小内核，在 `ENABLE_TILE=1` 下编译，预期会编译失败并触发回退（fallback 机制将在 u4-l3 讲解）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 TileIR 在「全局内存别名访问」或「splitK/streamK」场景下可能算错？根本原因是什么？

> **参考答案**：根本原因是 TileIR 当前只有**无序内存模型（unordered memory model）**——全局内存访问默认不保证先后顺序。在别名访问或跨 tile 块需要确定性归约（依赖全局内存锁逻辑）的场景下，如果没有显式的 memory token 来串行化这些访存，访问顺序不确定，就会导致结果错误。README 也给出了潜在解法，如扩展 Triton API 显式支持该内存模型、或把全局内存锁抽象成独立 API。

**练习 2**：TileIR 默认关闭 approx 和 FTZ，对用户是好是坏？

> **参考答案**：是一把双刃剑。好处是**默认更精确、更保守**，数值行为可预测，适合对精度敏感的场景；坏处是**牺牲了潜在的速度**（近似指令和 FTZ 通常更快）。所以如果你的内核对精度不敏感、想榨性能，可以用 `TILEIR_ENABLE_APPROX=1` / `TILEIR_ENABLE_FTZ=1` 重新打开。

**练习 3**：下面哪个算子目前在 TileIR 后端完全可用、不在「不支持清单」里：`tt.gather`、`math.erf`、`tt.dot`、`cf.cond_br`？

> **参考答案**：`tt.dot`。`tt.gather`、`math.erf`、`cf.cond_br` 都在 [README.md:103-123](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/README.md#L103-L123) 的不支持清单里；而 `tt.dot`（矩阵乘）是 TileIR 的核心目标负载（README 多处提到 dot-related workloads 的调优），属于受支持的重点算子。

---

## 5. 综合实践

把本讲三个模块串起来，完成一份「**TileIR 后端快速评估表**」。假设你的同事问你：「我有个 Triton 内核，能不能用 TileIR 后端跑？」

请你基于本讲读到的 README 内容，写一份不超过一页的评估表，包含以下四栏（每栏都引用对应的 README 行号作为依据）：

1. **启用条件**：需要哪些环境变量 / CUDA 版本 / 硬件？（依据 [README.md:39-41](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/README.md#L39-L41)、[README.md:94-98](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/README.md#L94-L98)）
2. **数值行为**：默认关闭了哪两类优化？（依据 [README.md:85-86](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/README.md#L85-L86)）
3. **正确性风险**：内核若有别名访存或 splitK/streamK，会怎样？（依据 [README.md:54-62](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/README.md#L54-L62)）
4. **调优起点**：最先应该调哪两个旋钮？（依据 [README.md:74-77](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/README.md#L74-L77)、[README.md:100-101](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/README.md#L100-L101)）

完成这张表，你就把本讲的「定位、差异、限制」三大模块融会贯通了。这一步纯阅读归纳，无需 GPU。

## 6. 本讲小结

- **本仓库是 incubator repo**：在上游 Triton 之上**新增**了 CUDA Tile IR 后端，而非重写；上游内容保留在 `README.original.md`。
- **启用方式**：核心开关是 `ENABLE_TILE=1`（与 Helion 同用时还要在 import 前 `HELION_BACKEND=tileir`）；依赖 CUDA 13.1 的 `tileiras`/`ptxas`/`libnvvm.so`，且仅支持 Blackwell GPU。
- **与 PTX 后端的关键差异**：PTX 是默认后端；TileIR 的调优旋钮完全不同（`occupancy`/`num_ctas`/`num_stages` 为主，不支持 `range_*`/`static_ranges`/`num_warps`），配置不能直接互搬。
- **内存模型**：TileIR 当前只有**无序内存模型**，别名访存或跨 tile 块传递（splitK/streamK）可能算错，需要 memory token 排序。
- **默认关闭两类数值优化**：approx 与 FTZ，分别用 `TILEIR_ENABLE_APPROX=1`、`TILEIR_ENABLE_FTZ=1` 重新开启。
- **已知限制明确**：小 GEMM 与旧式指针 load/store 性能差；一批算子（`tt.gather`、`cf.cond_br`、`math.erf` 等）和部分 TMA 特性尚未支持。

## 7. 下一步学习建议

本讲建立了「项目是什么、后端有何不同、有哪些限制」的全局认知。建议按以下顺序继续：

1. **动手安装与切换后端**：学习 [u1-l2 安装与运行方式](u1-l2-install-and-run.md)，掌握从源码 / wheel 安装，并用 `ENABLE_TILE` 在两个后端之间切换并验证 `driver.active`。
2. **摸清代码在哪**：学习 [u1-l3 目录结构与代码组织](u1-l3-repo-structure.md)，识别 TileIR 的 Python / C++ / 测试 / 构建代码各在哪些路径。
3. **建立端到端视图**：学习 [u1-l4 端到端编译链路总览](u1-l4-e2e-pipeline-overview.md)，把 `make_ttir` → `make_tileir` → `make_cubin` → 启动 的完整数据流串起来。

继续阅读的源码：本讲只看了 README，后续可先读 [third_party/tileir/README.md](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/README.md) 和 [INSTALL.md](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/INSTALL.md)，为 u1-l2 做准备。
