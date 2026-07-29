# Tilus 是什么：项目定位与设计目标

## 1. 本讲目标

本讲是整本《Tilus 学习手册》的第一讲。读完本讲后，你应当能够：

- 说清楚 **Tilus 是什么**：它的全称、定位、读音，以及它解决的核心问题。
- 理解 **tile-level（线程块级）编程模型**和「以张量为一等公民」的设计理念。
- 说出 Tilus 相对 **CUDA** 和 **Triton** 的关键取舍——尤其是它对共享内存与寄存器张量的「显式控制」。
- 了解 Tilus 支持 **任意位宽（1–8 bit）低精度类型** 的卖点，以及这为什么重要。
- 知道 Tilus 背后站在哪些前辈项目的肩膀上（**Hidet / TVM / Triton / Hexcute**），并理解它的「传承关系」。

本讲**不要求你写代码**，重点是建立正确的心智模型。从下一讲开始，我们才会动手安装、运行、逐行精读内核。

---

## 2. 前置知识

在开始之前，用最通俗的方式先建立几个概念。如果你已经熟悉，可以跳到第 3 节。

### 2.1 GPU 为什么要「并发」地算

CPU 擅长把单个任务做得又快又复杂；GPU 擅长**把成千上万个简单任务同时做掉**。比如「把一百万个数相加」，CPU 一次算一个，GPU 可以同时算成千上万个。这种「同时做大量简单运算」的能力，让 GPU 非常适合矩阵乘法、深度学习这类任务。

### 2.2 GPU 的执行层次：线程 → 线程束 → 线程块 → 网格

现代 GPU（尤其是 NVIDIA GPU）有一套层级化的执行模型：

| 层级 | 英文 | 大致含义 |
|------|------|----------|
| 线程 | thread | 最小执行单元 |
| 线程束 | warp | 通常 32 个线程组成一个 warp，是 GPU 真正调度的最小单位 |
| 线程块 | thread block / CTA | 若干 warp 组成，可共享一块片上共享内存 |
| 网格 | grid | 若干线程块组成，是一次内核启动的全部 |

> **小术语提示**：CTA（Cooperative Thread Array）就是「线程块」的硬件术语；sm（streaming multiprocessor）是 GPU 上执行线程块的物理单元。这些词在后续读源码时会反复出现。

### 2.3 GPU 的存储层次

| 存储类型 | 位置 | 特点 |
|----------|------|------|
| 全局内存（Global Memory / DRAM） | 芯片外（显存） | 容量大、速度慢 |
| 共享内存（Shared Memory） | 芯片内（片上） | 容量小、速度快、一个线程块内可见 |
| 寄存器（Register） | 每个线程私有 | 最快，但每个线程数量有限 |

**核心矛盾**：计算越快，但要把数据从慢速的显存搬到快速的寄存器，搬运本身就成了瓶颈。如何高效搬运、如何让线程之间分工合作，就是 GPU 编程的核心难题。

### 2.4 什么是「DSL」

DSL = Domain-Specific Language（领域专用语言）。它不是像 Python/C 这样的「通用语言」，而是**专门为某个领域设计的语言**。Tilus 就是一种**为 GPU 内核编程专门设计的 DSL**，但它「长得像 Python」——你用 Python 的语法写内核，Tilus 在后台把它翻译成 GPU 能跑的代码。

> 有了这些背景，我们就可以理解 Tilus 到底做了什么取舍。

---

## 3. 本讲源码地图

本讲只涉及项目最顶层的「门面」文档，目的是让你先看清全景，而不是一头扎进代码细节。

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/README.md) | 项目第一张名片：一句话定位、三大核心卖点、安装方式、版本路线图、致谢与传承关系。 |
| [docs/source/programming-guides/overview.rst](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/docs/source/programming-guides/overview.rst) | 编程指南总览：用一个 Hello World 例子展示 Tilus 的最小写法，并列出后续要展开的主题。 |
| [docs/source/index.rst](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/docs/source/index.rst) | 文档站点的目录树：告诉你整个项目的文档分成「安装 / 教程 / 编程指南 / Python API」几大块。 |

> 这三份文件是「文档」，不是「实现」。本讲引用它们来确立概念；从下一讲起我们才会进入 `python/tilus/` 下的真正源码。

---

## 4. 核心概念与源码讲解

本讲对应三个最小模块：

1. **tile-level 与张量数据类型**（Tilus 的核心编程模型）
2. **低精度任意位宽类型**（Tilus 的特色能力）
3. **与 Hidet / TVM / Triton / Hexcute 的传承关系**（Tilus 站在谁的肩膀上）

---

### 4.1 tile-level 编程模型与「张量为一等公民」

#### 4.1.1 概念说明

写 GPU 内核，传统上有两条路：

- **CUDA / PTX（线程级）**：你写的代码以「单个线程」为视角。要算一个矩阵，你得手动计算「我是第几个线程、我负责哪几个元素、我的数据在共享内存里怎么排布」。自由度极高，但心智负担极重，门槛极高。
- **Triton（线程块级，但张量是「黑箱」）**：你以「一个线程块」为视角写代码，操作的是「块」级别的张量，编译器帮你把张量拆到各个线程。写起来轻松，但你**看不到也无法显式控制**张量在共享内存和寄存器里的排布——这些都由编译器代劳，想要榨干性能时往往无从下手。

**Tilus 选择了一条中间路线**：它和 Triton 一样以「线程块（tile）」为编程视角，但它把**张量提升为第一等公民（first-class citizen）**，并让你**显式控制**共享内存张量和寄存器张量。

> 「一等公民」的意思是：张量像整数、浮点数一样，是语言里被直接操作的基本对象。你可以声明一个寄存器张量、一个共享内存张量，明确地让数据在它们之间搬运，并明确地指定它们的布局（layout）。

这样一来，Tilus 兼顾了两端：

- 比线程级（CUDA）**好写**：你不用关心「第几个线程」，只关心「这个块要做哪些张量运算」。
- 比线程块级「黑箱」（Triton）**可控**：你能精确指定数据在共享内存/寄存器中的排布，从而榨干硬件性能。

#### 4.1.2 核心流程

一个 Tilus 内核的「思考流程」可以概括为：

```text
1. 以「一个线程块」为单位思考问题：
   "我要处理一个多大的块？（block_m × block_n × block_k）"
2. 数据先从全局内存（显存）进入共享内存或寄存器张量；
3. 在寄存器张量上做计算（如矩阵乘的累加器）；
4. 把结果张量写回全局内存。
```

关键点：**你操作的是「张量」，而不是「线程的标量」**。线程如何分工去搬运/计算这个张量，由 Tilus 的布局系统（layout system）在后台完成——这是后续 U4 单元的重点，本讲只需知道「有这么一回事」。

用一张图理解三种抽象的层次差异：

```text
抽象层级（由低到高，开发效率递增、可控性递减）

   CUDA / PTX        ← 线程级，标量视角，全手动
       ↑
   Tilus             ← 线程块级，张量视角，但显式可控共享内存/寄存器
       ↑
   Triton            ← 线程块级，张量视角，但布局由编译器托管（黑箱）
```

#### 4.1.3 源码精读

README 的开篇就一锤定音地给出了 Tilus 的定位。注意这几个加粗关键词，它们正是 Tilus 的三大卖点：

```text
**Tilus** is a powerful research domain-specific language (DSL) for GPU programming that offers:

* **Thread-block-level granularity** with **tensors** as the primary data type.
* **Explicit control** over shared memory and register tensors (unlike Triton).
* **Low-precision types** with arbitrary bit-widths (1 to 8 bits).
```

> 源码引用：[README.md:L4-L8](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/README.md#L4-L8) —— 这一段定义了 Tilus 的「身份」：线程块粒度 + 张量为基本数据类型 + 显式控制共享内存与寄存器张量 + 任意位宽低精度。

注意第 7 行那句括号里的话：**(unlike Triton)**。这是 Tilus 全文里最直白的「我和 Triton 的区别」——它明确强调自己对共享内存和寄存器张量是「显式控制」的。

- 源码引用：[README.md:L7](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/README.md#L7) —— 「Explicit control over shared memory and register tensors (unlike Triton)」，这是理解 Tilus 与 Triton 差异的最关键一句。

而「Hello World」例子则展示了这种「线程块视角」的最小形态。你**不需要写任何线程索引**，只要告诉 Tilus「用 1 个线程块、每个块 1 个 warp」：

```python
# define the kernel by subclassing `tilus.Script`
class MyKernel(tilus.Script):
    def __call__(self):
        self.attrs.blocks = 1   # one thread block
        self.attrs.warps = 1    # one warp per thread block

        self.printf("Hello, World!\n")
```

> 源码引用：[overview.rst:L21-L26](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/docs/source/programming-guides/overview.rst#L21-L26) —— 一个最简 Tilus 内核：继承 `tilus.Script`，在 `__call__` 里通过 `self.attrs.blocks / self.attrs.warps` 声明线程块与 warp 规模，整个内核以「块」为视角书写，看不到任何线程索引。

这就是「tile-level」的最直观体验：**你写的不是「第 i 个线程做什么」，而是「一个线程块整体做什么」**。

#### 4.1.4 代码实践

**实践目标**：动手跑通官方 Hello World，建立「Tilus 内核 = 一个 Python 类」的直觉。

**操作步骤**：

1. 如果你还没装 Tilus，先按下一讲的方式安装（`pip install tilus`）。本实践也可以**暂时跳过运行**，只做阅读。
2. 把上面 [overview.rst](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/docs/source/programming-guides/overview.rst) 的 Hello World 代码原样保存为 `hello.py`：
   ```python
   import torch
   import tilus

   class MyKernel(tilus.Script):
       def __call__(self):
           self.attrs.blocks = 1
           self.attrs.warps = 1
           self.printf("Hello, World!\n")

   kernel = MyKernel()
   kernel()
   torch.cuda.synchronize()
   ```
3. 运行 `python hello.py`。

**需要观察的现象**：

- 终端是否打印出 `Hello, World!`。
- 注意：这段代码里**没有任何 `threadIdx`、`blockIdx`**——这就是 tile-level 抽象带来的简洁。

**预期结果**：

- 输出 `Hello, World!`。
- 如果你的环境暂时没有可用 GPU，则**待本地验证**：先把这段代码读懂即可，运行留到第二讲装好环境之后。

> 说明：上面的 `self.attrs.blocks` / `self.attrs.warps` 在源码层面属于 `Script` 的属性系统，我们会在 U1-L3 / U2-L1 详细展开，这里只需感性认识。

#### 4.1.5 小练习与答案

**练习 1**：用一句话概括「tile-level（线程块级）编程」和「线程级编程（如 CUDA）」的区别。

> **参考答案**：线程级编程以单个线程为视角，需要手动计算每个线程处理哪些数据；线程块级编程以一个线程块为整体视角，操作块级别的张量，由系统负责把张量拆分到各个线程。

**练习 2**：README 里强调 Tilus「unlike Triton」的那一点是什么？为什么这很重要？

> **参考答案**：那一点是「对共享内存和寄存器张量的**显式控制**」。Triton 把布局托管成了「黑箱」，而 Tilus 让程序员能精确指定数据在共享内存/寄存器中的排布，从而在需要极致性能时有更强的可控性。

**练习 3**：在 Hello World 例子中，`self.attrs.blocks = 1` 和 `self.attrs.warps = 1` 分别表示什么？

> **参考答案**：分别表示「这次内核启动用 1 个线程块」和「每个线程块里有 1 个 warp（即 32 个线程）」。它们以「块/束」为单位描述规模，而不是以「线程」为单位。

---

### 4.2 低精度与任意位宽类型（1–8 bit）

#### 4.2.1 概念说明

「低精度（low precision）」指的是**用更少的比特位来表示一个数**。常见的有 `float16`、`bfloat16`（16 bit），还有更激进的 `int8`、甚至 4 bit、2 bit。

为什么要用低精度？因为现代 AI 计算（尤其是大模型推理）有两个瓶颈：

1. **显存带宽**：把数据从显存搬进芯片，搬运速度跟不上计算速度。数据位宽越窄，同样带宽下能搬的数越多。
2. **算力**：GPU 的低精度张量核（Tensor Core）吞吐量远高于高精度。比如用 4 bit 算，单位时间能完成的运算数远多于 16 bit。

**关键矛盾**：硬件支持的位宽往往是「固定几档」（比如只支持 16/8 bit 的某些组合）。如果你想用一个**非标准位宽**（比如 MX 格式里的 5 bit、6 bit），传统框架很难表达。

**Tilus 的卖点**：它原生支持 **1 到 8 bit 的任意位宽（arbitrary bit-widths）类型**。这意味着你可以表达那些「不那么标准」的低精度格式，而不必受限于硬件预设的几档。

#### 4.2.2 核心流程

理解低精度计算的关键，是一个简单的「容量-速度」权衡公式。在显存带宽 \(B\)（字节/秒）和总数据量 \(V\)（比特）给定时，搬运所需时间大致为：

\[
T_{\text{load}} \approx \frac{V}{B}
\]

如果我们把每个数的位宽从 \(w_1\) 降到 \(w_2\)（\(w_2 < w_1\)），处理同样**元素个数** \(N\) 时，总数据量从 \(N \cdot w_1\) 降到 \(N \cdot w_2\)，于是：

\[
\frac{T_{\text{load,new}}}{T_{\text{load,old}}} = \frac{w_2}{w_1}
\]

也就是说，**位宽每砍一半，搬运时间近似砍一半**。这就是低精度的核心动力。Tilus 让你能灵活地把 \(w\) 设成 1 到 8 之间的任意整数，从而在不同精度-性能权衡里精细取舍。

> 小提示：低精度会损失数值精度，所以它适合「对误差有一定容忍度」的场景（如神经网络推理/训练），而不适合需要精确结果的科学计算。这是工程取舍，不是「低精度一定更好」。

#### 4.2.3 源码精读

README 第 8 行明确把「任意位宽低精度」列为三大卖点之一：

```text
* **Low-precision types** with arbitrary bit-widths (1 to 8 bits).
```

> 源码引用：[README.md:L8](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/README.md#L8) —— Tilus 支持 1 到 8 bit 的任意位宽低精度类型，这是它区别于很多只支持固定几档位宽的框架的关键特性。

README 的论文标题也呼应了这一点——「Low-Precision Computation」正是 Tilus 的研究主题：

> 源码引用：[README.md:L48](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/README.md#L48) —— 论文标题 *Tilus: A Tile-Level GPGPU Programming Language for **Low-Precision** Computation*，点明低精度是 Tilus 的核心研究问题。

> 说明：具体的位宽类型在代码里如何定义（如 `float16`、自定义位宽等），属于数据类型系统，我们会在 **U1-L4「数据类型与指针类型」** 用真实源码精读，本讲只建立概念。

#### 4.2.4 代码实践

**实践目标**：通过阅读，建立「位宽 → 带宽 → 时间」的量化直觉。

**操作步骤**：

1. 假设一个 GPU 的显存带宽为 \(B = 3000\) GB/s。
2. 计算：搬运 \(N = 10^9\) 个数（十亿个数），分别用 16 bit、8 bit、4 bit，各需搬运多少数据、耗时多少（忽略计算，只看搬运）。

**需要观察的现象**：

- 位宽减半，搬运数据量与耗时是否也近似减半。

**预期结果**（待本地验证，下面是按公式估算的示例值）：

| 位宽 | 每数字节 | 总数据量 | 按 3000 GB/s 估算搬运耗时 |
|------|----------|----------|--------------------------|
| 16 bit | 2 B | 2 GB | ≈ 0.67 ms |
| 8 bit  | 1 B | 1 GB | ≈ 0.33 ms |
| 4 bit  | 0.5 B | 0.5 GB | ≈ 0.17 ms |

> 这个表是「示例计算」，不是实测。真实 GPU 还有计算开销、对齐、bank conflict 等因素，但它说明了低精度的根本收益来源。

#### 4.2.5 小练习与答案

**练习 1**：为什么 AI 推理场景特别青睐低精度？

> **参考答案**：因为推理对数值误差有一定容忍度，而低精度能成倍降低显存带宽压力并提升张量核算力吞吐，从而在「可接受的精度损失」下换取显著的速度与显存节省。

**练习 2**：「任意位宽 1–8 bit」相比「只支持 8/16 bit」的框架，优势在哪里？

> **参考答案**：它允许表达非标准的中间位宽（如 MX 格式的 4/5/6 bit 等），让程序员在精度与性能之间做更精细的取舍，而不被硬件预设的几档位宽卡死。

**练习 3**：低精度「一定更好」吗？举一个不适合用低精度的例子。

> **参考答案**：不一定。需要精确数值结果的科学计算、金融计算（如求解偏微分方程、累加大量小数）就不适合，因为低精度带来的舍入误差会累积放大，导致结果不可信。

---

### 4.3 与 Hidet / TVM / Triton / Hexcute 的传承关系

#### 4.3.1 概念说明

理解一个项目，往往要看它「站在谁的肩膀上」。Tilus 不是凭空出现的，它的 README 末尾有一段「Acknowledgement（致谢）」，坦白说明了四个前辈项目对自己的影响：

- **Hidet**：Tilus 把它当作低层 IR（中间表示）目标，并复用了它的运行时系统。
- **TVM**：Hidet 的早期 IR 又脱胎于 TVM，Tilus 也从 TVM 学到了「如何构建一个编译器」。
- **Triton**：Tilus「在线程块级定义内核、以 tile 为单位工作」的核心思想，受 Triton 启发。
- **Hexcute**：Tilus「用自动布局推理（automatic layout inference）来简化编程」的想法，来自 Hexcute。

> **小术语**：IR（Intermediate Representation，中间表示）是编译器内部用来表示程序的数据结构。把高层代码翻译成机器码，通常要经过好几层 IR。Tilus 有自己的「Tilus IR」，最终又会降到 Hidet 的 IR，再变成 CUDA 代码。

#### 4.3.2 核心流程

可以把 Tilus 的「传承」画成一张「家谱」：

```text
        TVM  ──(早期 IR / 编译器构建方法论)──┐
                                          ▼
                                       Hidet  ──(低层 IR 目标 + 运行时)──┐
                                                                          ▼
       Triton ──(线程块级 / tile 思想)──────────────────────────────────►  Tilus
                                                                          ▲
       Hexcute ──(自动布局推理)──────────────────────────────────────────┘
```

用一句话串起来：

> **Tilus ≈ Triton 的线程块编程思想 + Hexcute 的自动布局推理 + 自创的「显式张量/布局控制」+ 任意位宽低精度，底层编译到 Hidet IR 并复用 Hidet 运行时，而 Hidet 又继承了 TVM 的 IR 与编译器方法论。**

这四股力量分工明确，正好对应 Tilus 的不同侧面，理解它们就理解了 Tilus 「为什么这么设计」。

#### 4.3.3 源码精读

README 的致谢段落把每一项影响都说得很清楚：

```text
- **Hidet**: We take Hidet IR as our low-level target and reuse its runtime system.
- **TVM**: Hidet's initial IR was adopted from TVM, and we also learned a lot from TVM on how to build a compiler.
- **Triton**: The core idea of defining kernels at a thread-block level and working with tiles was inspired by Triton.
- **Hexcute**: We adopted the idea of using automatic layout inference to simplify programming from Hexcute.
```

> 源码引用：[README.md:L57-L60](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/README.md#L57-L60) —— 四项致谢，分别说明 Hidet（IR 目标 + 运行时）、TVM（IR 渊源 + 编译器方法论）、Triton（线程块级 tile 思想）、Hexcute（自动布局推理）对 Tilus 的影响。

注意第一句「We take Hidet IR as our low-level target and reuse its runtime system」——这一点直接解释了为什么 Tilus 的编译流水线最终会降到「Hidet IR」、为什么运行时里有 Hidet 的影子。这在本手册 U3（编译流水线）和 U6（后端代码生成）会反复出现。

另外，README 的版本路线图透露了 Tilus 对 GPU 架构的覆盖范围：

```text
* **[2025/07] Tilus v0.2.0** — Blackwell and Hopper GPU support, ...
* **[2025/04] Tilus v0.1.0** — Initial release with Ampere support.
```

> 源码引用：[README.md:L16-L20](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/README.md#L16-L20) —— 版本路线：v0.1.0 先支持 Ampere，v0.2.0 扩展到 Hopper 与 Blackwell。Tilus 当前覆盖 NVIDIA 三代主流架构。

也就是说，Tilus 当前（v0.2.0）支持 **Ampere（如 A100）、Hopper（如 H100）、Blackwell（如 B200）** 三代 NVIDIA GPU 架构。这呼应了本讲的学习目标之一。

#### 4.3.4 代码实践

**实践目标**：把「传承关系」和「Tilus 自身的取舍」对应起来。

**操作步骤**：

1. 重新阅读 [README.md:L55-L60](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/README.md#L55-L60) 的致谢段落。
2. 填写下面这张「影响来源 → Tilus 中的体现」对照表（这是源码阅读型实践，答案见 4.3.5）。

| 前辈项目 | 对 Tilus 的影响 | 在 Tilus 的哪个部分能感受到？ |
|----------|----------------|------------------------------|
| Hidet | | |
| TVM | | |
| Triton | | |
| Hexcute | | |

**需要观察的现象**：

- 你能否把每个前辈项目对应到 Tilus 的某个具体侧面（编程模型 / IR / 编译器构建 / 布局推理）。

**预期结果**：见下一节的参考答案。

#### 4.3.5 小练习与答案

**练习 1**（即上一节表格的答案）：完成「影响来源 → Tilus 中的体现」对照表。

> **参考答案**：
>
> | 前辈项目 | 对 Tilus 的影响 | 在 Tilus 的哪个部分能感受到？ |
> |----------|----------------|------------------------------|
> | Hidet | 低层 IR 目标 + 运行时复用 | 编译流水线最终降到 Hidet IR；运行时加载 `.so`、启动内核 |
> | TVM | IR 渊源 + 编译器构建方法论 | IR 数据结构的设计风格、Pass（变换）框架 |
> | Triton | 线程块级、以 tile 为单位的编程思想 | `tilus.Script` 以线程块视角写内核、tile 分块 |
> | Hexcute | 自动布局推理（layout inference）简化编程 | U4 布局系统、U5 的 layout_inference 变换 |

**练习 2**：为什么说 Tilus「基于 Hidet 但不只是 Hidet」？

> **参考答案**：因为 Hidet 只提供「低层 IR 目标和运行时」这一层底座；Tilus 在其之上自创了以张量为一等公民的编程模型、显式的共享内存/寄存器张量控制、任意位宽低精度类型，以及自动布局推理等高层能力。Hidet 是 Tilus 的「地基」，不是 Tilus 的全部。

**练习 3**：从传承关系看，Tilus 和 Triton 最像、又最不一样的地方分别是什么？

> **参考答案**：最像的是「线程块级、以 tile 为单位写内核」的编程思想（来自 Triton 的启发）。最不一样的是 Tilus 对共享内存与寄存器张量采取「显式控制」，并把张量布局做成一等公民（受 Hexcute 启发），而 Triton 把布局托管成相对的黑箱。

---

## 5. 综合实践

把本讲的三个模块串起来，完成一个「阅读理解 + 自我总结」任务。

**任务**：阅读 [README.md](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/README.md) 全文与 [overview.rst](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/docs/source/programming-guides/overview.rst)，然后**用自己的话写一段约 200 字的中文总结**，回答以下两个问题：

1. **Tilus 解决了什么问题？**（提示：结合 tile-level 抽象、显式张量控制、任意位宽低精度三点）
2. **相比 CUDA 和 Triton，Tilus 做了怎样的取舍？**（提示：CUDA = 自由但难写；Triton = 好写但布局不可控；Tilus = 中间路线）

**参考写作框架**（你可以用自己的话改写，不要照抄）：

> Tilus 是一种面向 GPU 的线程块级内核 DSL，它把张量作为一等公民，并允许程序员显式控制共享内存与寄存器张量的布局。它解决的核心问题是：既想拥有 Triton 那样以「块」为单位、好写的编程体验，又想保留 CUDA 那样对硬件资源的精细可控性，从而能在任意位宽（1–8 bit）低精度场景下榨取极致性能。相比 CUDA，Tilus 屏蔽了繁琐的线程索引；相比 Triton，Tilus 不把布局托管成黑箱，而是让程序员能精确指定数据排布。它的底层编译到 Hidet IR 并复用其运行时，融合了 Triton 的 tile 思想与 Hexcute 的自动布局推理。

**自检清单**：

- [ ] 我的总结里提到了「线程块级 / tile-level」。
- [ ] 我的总结里提到了「张量为一等公民」或「显式控制共享内存/寄存器」。
- [ ] 我的总结里提到了与 CUDA、Triton 的取舍对比。
- [ ] （加分项）我提到了任意位宽低精度 或 传承关系。

---

## 6. 本讲小结

- **Tilus 是一种面向 GPU 的线程块级（tile-level）内核 DSL**，读音为 tie-lus（/ˈtaɪləs/），核心是把**张量**当作一等公民来编程。
- 它的三大卖点是：**线程块级粒度 + 张量为基本数据类型**、**对共享内存/寄存器张量的显式控制（unlike Triton）**、**1–8 bit 任意位宽低精度类型**。
- 相比 **CUDA**，Tilus 以「块」为视角，屏蔽了线程索引，更好写；相比 **Triton**，Tilus 让布局可显式控制，更可控。
- **低精度**通过减小位宽来缓解显存带宽与算力瓶颈，位宽减半、搬运耗时近似减半（\(\frac{T_2}{T_1} = \frac{w_2}{w_1}\)）。
- Tilus **站在 Hidet / TVM / Triton / Hexcute 四个前辈项目的肩膀上**：Hidet 提供 IR 目标与运行时，TVM 提供 IR 与编译器方法论，Triton 启发了 tile 思想，Hexcute 启发了自动布局推理。
- 当前（v0.2.0）支持 **Ampere / Hopper / Blackwell** 三代 NVIDIA GPU 架构。

---

## 7. 下一步学习建议

本讲只建立了「Tilus 是什么」的概念地图，**还没有真正运行任何内核、也没有进入源码**。建议接下来：

1. **下一讲 U1-L2《安装、运行与包目录结构》**：动手 `pip install tilus`，并了解 `python/tilus/` 下各顶层目录的职责，为后续读源码建立目录地图。
2. **U1-L3《第一个内核：vector_add 逐行精读》**：从最简单的内核开始，逐行理解 `tilus.Script` 的 `__init__`/`__call__` 骨架与全局内存读写。
3. 如果你急于看「真实硬件」的内核，可以提前浏览 [examples/matmul](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/matmul) 目录——但建议先跟完 U1 的基础三讲，再回头看这些示例会顺畅得多。

> 学习路线总览：U1（起步）→ U2（Tilus Script 编程模型）→ U3（Tilus IR 与编译流水线）→ U4（布局系统）→ U5（IR 变换）→ U6（后端代码生成）→ U7（架构特性与高性能内核）→ U8（缓存/调优/运行时/扩展）。本讲是 U1 的第一块基石。
