# Autofuse 原理：自动融合缓解 Memory Bound

> 学习阶段：入门层（beginner）
> 依赖讲义：u1-l1（项目整体概览与定位）

## 1. 本讲目标

本讲是 Autofuse 组件的第一讲，只讲「为什么」，不深入「怎么做」。读完本讲你应当能够：

1. 说清什么是 **Memory Bound（内存受限）**，为什么相邻的 Vector 算子会成为性能瓶颈。
2. 用一句话讲明白 **自动融合（Auto Fusion）** 是怎么消除中间内存搬运、缓解 Memory Bound 的。
3. 理解 Autofuse 对外承诺的两项关键能力：**动态 shape** 与 **混合精度**，以及它们的直觉含义。
4. 对一条「三个相邻 elementwise 算子」的链路，手算出融合前后各需要多少次全局内存搬运。

本讲只做原理与直觉铺垫，具体的目录结构、使能方式、源码精读分别留给 u3-l2、u3-l3 以及进阶层（u4 之后）。

## 2. 前置知识

在进入 Autofuse 之前，先用最通俗的方式建立两个硬件直觉。这里的描述是教学用的简化模型，但足够支撑后续理解。

### 2.1 两级存储：全局内存（HBM）与片上缓冲（UB）

昇腾 AI 核（AI Core）内部有一块很小但极快的片上存储，叫做 **统一缓冲区（Unified Buffer，UB）**；而算子真正要处理的大块数据（输入张量、输出张量）放在芯片外的大容量 **全局内存（即显存 HBM）** 里。

- 全局内存：容量大（GB 级），但带宽相对有限、延迟高。
- 片上 UB：容量小（MB 级甚至更小），但带宽高、延迟低。

数据不能在全局内存里直接被计算单元使用，必须先搬到 UB；计算完的结果也要从 UB 搬回全局内存才能被下一个算子读到。这就引出了下一节的搬运流水线。

### 2.2 AI Core 上的三类硬件单元

一个 Vector（向量）算子在 AI Core 上执行时，本质上经历三个阶段，分别由不同的硬件单元承担：

| 阶段 | 硬件单元 | 含义 |
|------|----------|------|
| 数据搬入 | MTE2（Memory Transfer Engine 2） | 全局内存 → UB（读入/load） |
| 向量计算 | Vector（向量计算单元） | 在 UB 里做加减乘除、比较等 |
| 数据搬出 | MTE3（Memory Transfer Engine 3） | UB → 全局内存（写出/store） |

> 术语提示：本讲后面会引用项目文档里的 `aiv_mte2_time` 与 `aiv_mte3_time` 两个 profiling 指标，其中 `aiv` 指 AI Vector 核，`mte2`/`mte3` 就是上表里的搬入 / 搬出。

## 3. 本讲源码地图

本讲主要围绕 Autofuse 的定位与原理，引用的源码文件都很轻量：

| 文件 | 作用 |
|------|------|
| `autofuse/README.md` | Autofuse 组件的总说明：定位、Memory Bound 问题、目录结构、使能与调测环境变量 |
| `autofuse/examples/pytorch/af_pointwise/af_add_ge.py` | 最小可运行示例：把 `add` + `ge` 两个 elementwise 算子交给 Autofuse 融合 |
| `autofuse/examples/pytorch/af_pointwise/README.md` | 示例说明，给出融合后的 kernel 命名规则 |
| `docs/zh/autofuse/autofuse_precision_consistency.md` | 精度一致性说明，间接解释了融合的收益来源与混合精度策略 |

> 说明：本讲是「原理讲」，所以引用的多为 README、示例与说明文档；真正的编译器源码（graph_metadef / ascir / optimize / att / codegen）会在后续单元逐层展开。

## 4. 核心概念与源码讲解

本讲覆盖三个最小模块：

- 4.1 Memory Bound 问题（Vector 计算为什么被内存拖慢）
- 4.2 自动融合范围识别（把相邻 Vector 算子缝合成一个 kernel）
- 4.3 动态 shape 与混合精度（Autofuse 对真实网络的两个承诺）

### 4.1 Memory Bound 问题

#### 4.1.1 概念说明

在深度学习网络里，存在大量「每个元素只做一点点计算」的算子，比如 `Add`、`Mul`、`Exp`、`Ge`（大于等于比较）等，这类算子统称为 **elementwise（逐元素）算子**，它们在昇腾上属于 **Vector 算子**。

对这类算子，每个元素只做一两次乘加，计算量极小；但每处理一个元素，都必须先从全局内存把输入搬进来（MTE2）、再把结果搬出去（MTE3）。也就是说：**搬运的数据量远大于真正的计算量，性能瓶颈卡在内存带宽上，而不是卡在算力上。** 这种「计算单元在等内存」的状态，就叫 **Memory Bound（内存受限）**。

README 用一句话点明了这个问题：

> 「由于存在大量的 Vector 计算，各个 Vector 计算之间会产生大量的内存搬运，导致 Memory Bound 问题。」 —— 见 [autofuse/README.md:L3](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/README.md#L3)

#### 4.1.2 核心流程：三个相邻算子的搬运代价

把三个相邻的 elementwise 算子串成一条链：

\[
\text{out} = f_3(f_2(f_1(x)))
\]

其中 \(f_1, f_2, f_3\) 各是一个 Vector 算子，\(t_1 = f_1(x)\)、\(t_2 = f_2(t_1)\) 是中间结果。

**没有融合时（每个算子各跑一个独立 kernel）：**

每个算子都要完整走一遍 `MTE2 搬入 → Vector 计算 → MTE3 搬出`，于是中间结果必须落回全局内存：

| 算子 | 读入（MTE2） | 写出（MTE3） |
|------|--------------|--------------|
| \(f_1\) | 读 \(x\) | 写 \(t_1\) |
| \(f_2\) | 读 \(t_1\) | 写 \(t_2\) |
| \(f_3\) | 读 \(t_2\) | 写 out |

合计：**3 次读 + 3 次写 = 6 次全局内存访问**。其中 \(t_1, t_2\) 这两个中间结果被各写一次、各读一次，共 **4 次「纯搬运」**，而这 4 次搬运对最终结果没有任何数学贡献，纯粹是「因为算子被拆开了」才产生的。

**融合成一个 kernel 后：**

三个算子合并进同一个 kernel，中间结果 \(t_1, t_2\) 直接留在片上 UB，不再落回全局内存：

| 动作 | 说明 |
|------|------|
| 1 次读 | 把 \(x\) 搬进 UB |
| UB 内连算 | \(f_1 \rightarrow f_2 \rightarrow f_3\)，全程不出 UB |
| 1 次写 | 把最终 out 搬回全局内存 |

合计：**1 次读 + 1 次写 = 2 次全局内存访问**，原来的 4 次中间搬运全部消除。

一般化地，对单链上的 \(N\) 个相邻 elementwise 算子：

\[
\text{未融合的全局内存访问} = 2N \quad (\text{$N$ 次读} + \text{$N$ 次写})
\]

\[
\text{融合后的全局内存访问} = 2 \quad (\text{首部 }1\text{ 次读} + \text{尾部 }1\text{ 次写})
\]

\[
\text{被消除的中间搬运} = 2N - 2
\]

代入 \(N = 3\)：未融合 6 次，融合后 2 次，消除 4 次。这就是「融合缓解 Memory Bound」的定量直觉。

> 说明：上面的计数假设每个算子只有一个链上输入。若算子还有额外的旁路输入（例如 `add(x, y)` 里的 `y`），边界读写次数会更多，但「中间结果被消除」这一核心结论完全不变——这也是 Autofuse 收益的主要来源。

#### 4.1.3 源码精读

README 的开篇把「问题」和「解法」写在了同一句话里：

[autofuse/README.md:L3](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/README.md#L3) —— 这段既点明了「Vector 计算之间大量内存搬运 → Memory Bound」，也给出了 Autofuse 的解法：「通过自动将多个算子融合为一个算子，减少网络中的算子数量和内存搬运」。

而要「看见」这种搬运代价，最直接的观测手段是 profiling 里的搬运耗时指标：

[autofuse/README.md:L147](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/README.md#L147) —— README 在「结果分析」一节指出，可以对比融合算子相对于单算子的 `aiv_mte2_time`（输入搬运耗时）和 `aiv_mte3_time`（输出搬运耗时）的提升，并给出提升比公式：

\[
\text{融合提升比} = \frac{T_{\text{融合前}} - T_{\text{融合后}}}{T_{\text{融合前}}}
\]

也就是说，4.1.2 里那种「搬运次数减少」的纸面分析，在真实上板 profiling 里就体现为 `aiv_mte2_time` / `aiv_mte3_time` 的下降。

此外，精度一致性文档把 Autofuse 的收益来源总结得更精炼，可以作为本模块的收尾佐证：

[docs/zh/autofuse/autofuse_precision_consistency.md:L53-L56](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/docs/zh/autofuse/autofuse_precision_consistency.md#L53-L56) —— 收益主要来自「多个计算算子融合（消除中间结果的读写）」以及「计算与访存 overlap」。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：把 4.1.2 的纸面分析变成你能复述、能自洽的一段定量说明。
2. **操作步骤**：
   - 在纸上（或注释里）写出链路 `out = f3(f2(f1(x)))` 的三行搬运表（如 4.1.2 所示）。
   - 分别数出未融合时「读」「写」的次数，以及融合后「读」「写」的次数。
3. **需要观察的现象**：中间结果 \(t_1, t_2\) 在未融合时各出现一次写、一次读；融合后它们只在 UB 里流转，全局内存里完全看不到。
4. **预期结果**：未融合 = 6 次全局内存访问（3 读 + 3 写）；融合后 = 2 次（1 读 + 1 写）；被消除的中间搬运 = 4 次。收益来源 = 消除中间结果的重复读写。
5. 若想进一步验证：在真实昇腾设备上跑通 4.2 的示例后，对比融合前后的 `aiv_mte2_time` / `aiv_mte3_time`（**待本地验证**，需要上板环境）。

#### 4.1.5 小练习与答案

**练习 1**：把链路改成 5 个相邻 elementwise 算子 \(f_1 \dots f_5\)，未融合和融合后分别需要多少次全局内存访问？

> **答案**：未融合 \(2 \times 5 = 10\) 次（5 读 + 5 写）；融合后 2 次；消除的中间搬运 \(2 \times 5 - 2 = 8\) 次。

**练习 2**：为什么说 elementwise 算子「天然容易 Memory Bound」，而矩阵乘（matmul）相对更偏 Compute Bound？

> **答案**：elementwise 每个元素只做极少计算，却要读写整块数据，搬运量 ≫ 计算量，因此卡在内存带宽上；matmul 每个输出元素要做大量乘加（计算量远大于读写量），计算单元更忙，瓶颈更容易落在算力上。

---

### 4.2 自动融合范围识别

#### 4.2.1 概念说明

知道了「相邻 Vector 算子之间的中间搬运是浪费」，解法就很自然了：**把若干个相邻的 Vector 算子合并成一个 kernel**，让中间结果只在片上 UB 里流动，不再落回全局内存。这就是 **自动融合（Auto Fusion）**。

之所以强调「自动」，是因为这件事对用户是透明的：用户只需要用一行 `torch.compile(...)` 指定昇腾后端，Autofuse 会自己完成两件事：

1. **融合范围识别**：在计算图里找出哪些相邻 Vector 算子可以安全地合并成一个子图。
2. **代码生成 + Auto Tiling**：把这个子图编译成一个真正可执行的融合 kernel，并自动决定切块（tiling）策略。

README 把这两点列成了 Autofuse 的特性清单：

[autofuse/README.md:L3](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/README.md#L3) —— 「支持自动融合范围识别、自动算子代码生成、Auto Tiling 优化、动态 shape 及混合精度等特性」。

#### 4.2.2 核心流程：从计算图到融合 kernel

虽然本讲不深入源码，但需要建立一个高层的数据流印象（细节留给 u3-l2 与进阶层）：

```text
计算图（相邻 Vector 算子）
      │
      ▼
[graph_metadef]  基本图接口：把模型子图表达成 ComputeGraph / Node / 算子描述
      │
      ▼
[ascir]          算子注册：登记「哪些算子可被 Autofuse 理解和处理」
      │
      ▼
[optimize]       融合范围识别 + 调度切分：圈出可融合子图，安排执行计划
      │
      ▼
[att]            Auto Tiling：自动决定切块大小，平衡 UB 占用与并行度
      │
      ▼
[codegen]        kernel 代码生成：把融合子图落成一个 AscendC kernel 源码
      │
      ▼
[compiler]       对外接口：编译、产出可执行的融合算子
```

每一行方括号对应 Autofuse 的一个子目录（见 README 的目录结构）：

[autofuse/README.md:L9-L27](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/README.md#L9-L27) —— 目录注释里能看到 `ascir # 算子注册`、`optimize # 调度切分 模块`、`att # 自动 tiling 生成 模块`、`codegen # kernel 代码生成 模块` 等，正好与上面的数据流一一对应。

> 提醒：融合范围识别并不是「无脑把所有相邻算子都合进去」。有些算子（如 reduction 类）合进去会改变数值（见 4.3 和精度文档），有些算子可能尚未被 lowering，因此最终只有「被识别为可融合的子图」才会变成融合 kernel。这一点会在 u3-l3 讲 `Fallback` 时进一步说明。

#### 4.2.3 源码精读

最直观的「两个算子被融合成一个」的例子，是仓库自带的 `af_add_ge.py` 示例。模型 `forward` 只有一行：

[autofuse/examples/pytorch/af_pointwise/af_add_ge.py:L27-L29](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/examples/pytorch/af_pointwise/af_add_ge.py#L27-L29) —— `result = torch.ge(torch.add(x, y), z)`，即先做 `add(x, y)`，再把结果和 `z` 做 `ge`（大于等于）比较。这就是两个相邻 elementwise 算子。

使能 Autofuse 只需在 `torch.compile` 里指定 ascendc 后端：

[autofuse/examples/pytorch/af_pointwise/af_add_ge.py:L33-L38](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/examples/pytorch/af_pointwise/af_add_ge.py#L33-L38) —— `options={"npu_backend": "ascendc"}`，整网以 `fullgraph=True` 编译。

跑通之后，profiling 里不会出现独立的 `add` 和 `ge` 两个 kernel，而是出现一个融合后的 kernel，其命名直接体现了融合范围：

[autofuse/examples/pytorch/af_pointwise/README.md:L4-L5](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/examples/pytorch/af_pointwise/README.md#L4-L5) 与 [autofuse/examples/pytorch/af_pointwise/README.md:L15](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/examples/pytorch/af_pointwise/README.md#L15) —— 融合后的算子名为 `autofused_add_ge_拓扑哈希`，即「两个原始算子的名字被拼在一起 + 一段拓扑哈希」。这正是「融合范围识别」最终落到产物上的可观察证据。

> 命名规则里带「拓扑哈希」也有工程含义：相同结构、相同 shape/dtype 的融合子图会得到相同的哈希，从而可以缓存、复用编译结果。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：不看运行结果，仅凭源码就能预测「这个示例会融合成什么」。
2. **操作步骤**：
   - 打开 `af_add_ge.py`，定位 `forward` 里出现了哪几个 elementwise 算子。
   - 按照融合命名规则 `autofused_<算子名>_<算子名>_...`，写出你预测的融合 kernel 名字前缀。
   - 阅读示例 README 确认预测。
3. **需要观察的现象**：原始图里有两个独立的 Vector 算子（`add`、`ge`），它们的数据流是首尾相接的（`add` 的输出正好是 `ge` 的输入之一），这正是「相邻、可融合」的典型形态。
4. **预期结果**：融合后只剩一个 kernel，名字以 `autofused_add_ge_` 开头；独立的 `add`、`ge` kernel 消失。这也侧面印证了 4.1 的结论——中间结果（`add` 的输出）不再落回全局内存。
5. 若有上板环境，可实际运行 `python3 af_add_ge.py`，打开 `profiling` 目录下的 `op_summary_*.csv` 核对（**待本地验证**）。

#### 4.2.5 小练习与答案

**练习 1**：如果 `forward` 里是三个独立、互不相连的 elementwise 算子（输出谁也不是谁的输入），Autofuse 会把它们融合成一个 kernel 吗？

> **答案**：不会。融合的前提是算子之间存在数据流上的「相邻」关系（一个的输出是另一个的输入），这样中间结果才能在 UB 里流转。互不相连的算子没有可消除的中间搬运，不会被强行合并。

**练习 2**：融合 kernel 名字里为什么要带一段「拓扑哈希」？

> **答案**：用来区分不同结构 / 不同 shape / 不同 dtype 的融合子图。结构相同则哈希相同，可以命中缓存、复用已编译的 kernel，避免重复编译；结构不同则哈希不同，避免误用错误的 kernel。

---

### 4.3 动态 shape 与混合精度

#### 4.3.1 概念说明

真实业务网络对编译器有两个很现实的要求，Autofuse 把它们列在了特性清单里：

[autofuse/README.md:L3](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/README.md#L3) —— 「动态 shape 及混合精度等特性」。

- **动态 shape（Dynamic Shape）**：同一段融合 kernel 不必为每一种输入尺寸都重新编译一次，而是能在一定范围内适应变化的维度（例如变化的 batch size、序列长度）。这在真实训练 / 推理里非常常见——如果每变一个 shape 都要重新 codegen，编译开销会难以承受。
- **混合精度（Mixed Precision）**：网络的前后向往往在低精度（如 fp16 / bf16）下运行以省显存、提速度，但低精度直接累加容易掉精度。Autofuse 的做法是：**融合块的输入 / 输出保留原始 dtype，块内部把中间计算统一提升到 fp32 来累加**，兼顾性能与数值稳定。

需要特别说明：本讲对「动态 shape」只讲原理层面的含义，其具体实现分布在 optimize / att / codegen 多个模块里，会在进阶层（u6/u7/u8）展开；本讲不展开代码细节。

#### 4.3.2 核心流程：块内升精度的混合精度策略

混合精度的关键直觉可以用「截断次数」来理解：

- **未融合（Eager）时**：每个算子独立执行，算子之间的中间结果必须按原始 dtype（比如 fp16）落盘，再被下一个算子读入。于是在**每个算子边界**都发生一次 fp16 截断。
- **自动融合时**：一串算子被合并成一个 kernel，**只在融合块的入口 / 出口**保留原始 dtype，块内全程 fp32 累加。

用累加误差的直觉来表达：融合块内因为全程 fp32，舍入误差更小；而边界处的截断次数从「每算子一次」减少到「每融合块一次」。因此，融合不仅在性能上占优，在「累积舍入误差」上也不会比 Eager 更差——这是 Autofuse 的精度承诺，具体证明留给 u12-l4，本讲只建立直觉。

#### 4.3.3 源码精读

精度一致性文档对「块内升精度」有明确表述：

[docs/zh/autofuse/autofuse_precision_consistency.md:L66-L68](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/docs/zh/autofuse/autofuse_precision_consistency.md#L66-L68) —— 「自动融合在生成的融合 kernel 内部，默认将所有中间计算统一提升到 fp32，只在融合块的输入 / 输出边界保留原 dtype」。

并对「精度不差于 Eager」给出了基于截断次数的论证：

[docs/zh/autofuse/autofuse_precision_consistency.md:L70-L78](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/docs/zh/autofuse/autofuse_precision_consistency.md#L70-L78) —— Eager 在每个算子边界截断，自动融合只在融合块边界截断、块内全程 fp32，故「自动融合的累积舍入误差上界 ≤ Eager」。

至于动态 shape，可以在示例里看到一个对照点：`af_add_ge.py` 的 `torch.compile` 显式传了 `dynamic=False`：

[autofuse/examples/pytorch/af_pointwise/af_add_ge.py:L33-L38](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/examples/pytorch/af_pointwise/af_add_ge.py#L33-L38) —— 这意味着该示例选择了「静态 shape」模式（追求最大性能、shape 固定）。反过来说明：Autofuse 同样支持 `dynamic=True` 的动态 shape 场景，二者是用户可以按需选择的两种模式。

> 提醒：Autofuse 默认开启的是 elementwise 融合，而 reduce / concat / slice / transpose 等类别默认关闭、属实验特性。相关开关与环境变量属于「使能与调测」范畴，统一在 u3-l3 讲解，本讲不展开。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：用「截断次数」直觉解释「为什么融合后精度不劣于 Eager」。
2. **操作步骤**：
   - 假设一条 3 算子链路 `f3(f2(f1(x)))`，输入输出均为 fp16。
   - 数一数 Eager 模式下发生 fp16 截断的次数（提示：每个算子边界各一次）。
   - 数一数 Autofuse 融合后发生 fp16 截断的次数（提示：只在融合块入口 / 出口）。
3. **需要观察的现象**：Eager 的截断次数随算子个数线性增长；融合后固定为块边界两次，与块内算子数无关。
4. **预期结果**：Eager 模式 ≈ 3 次截断（算子边界），Autofuse ≈ 2 次截断（块入口读入 fp16、块出口写出 fp16），块内全程 fp32。因此累积误差不劣于 Eager。
5. 说明：这是基于精度文档的原理推导，属「源码阅读型实践」；若需严格数值验证，参考 u12-l4 的精度测试方法（**待本地验证**）。

#### 4.3.5 小练习与答案

**练习 1**：示例 `af_add_ge.py` 用的是 `dynamic=False`，这说明 Autofuse 不支持动态 shape 吗？

> **答案**：不是。`dynamic=False` 只是该示例为了追求最大性能、在 shape 已知时主动选择的静态模式。Autofuse 本身支持动态 shape（README 特性清单明确列出），动态 / 静态是用户可按场景选择的两种模式。

**练习 2**：为什么 Autofuse 选择「块内 fp32、边界保留原 dtype」，而不是全程都用原 dtype？

> **答案**：全程用 fp16/bf16 直接累加，多次运算后末位误差会快速累积，可能影响收敛和数值稳定；而块内升到 fp32 累加、只在边界降回原 dtype，既能减少截断次数、控制累积误差，又只在融合块边界付出一次 dtype 转换的代价，是性能与精度的折中。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「纸面推演」任务（无需上板，但写完后可以对照示例源码自查）：

**任务**：考虑链路 `out = ge(mul(exp(x), y), z)`，即依次执行 `exp → mul → ge` 三个相邻 elementwise 算子，输入 `x, y, z` 与输出 `out` 均为 fp16 张量。

请用一段文字回答：

1. **Memory Bound**：没有 Autofuse 时，这条链路会产生多少次全局内存访问（读 + 写）？其中有多少次是「纯搬运」的中间结果读写？融合成一个 kernel 后能减少到几次？指出收益来源。
2. **融合范围识别**：这三个算子会被 Autofuse 融合成几个 kernel？按照命名规则，融合 kernel 的名字前缀大概长什么样？
3. **混合精度**：相比每个算子各自以 fp16 落盘的 Eager 模式，融合后 fp16 截断大约发生几次？块内以什么精度累加？

**参考要点**：

1. 未融合：`exp` 读 \(x\) 写 \(t_1\)、`mul` 读 \(t_1, y\) 写 \(t_2\)、`ge` 读 \(t_2, z\) 写 out。聚焦链上中间结果：\(t_1\)（一写一读）、\(t_2\)（一写一读）共 **4 次纯搬运**被消除；按单链口径，全局内存访问从 6 次降到 2 次（首读 \(x\) + 末写 out，旁路输入 \(y, z\) 的读入是边界必要开销）。收益来源 = 消除中间结果的重复读写（以及计算与搬运的 overlap）。
2. 融合成 **1 个** kernel，名字前缀形如 `autofused_exp_mul_ge_`（后接拓扑哈希）。
3. Eager 模式约每个算子边界一次 fp16 截断；融合后只在融合块入口 / 出口共约 **2 次**截断，块内全程 **fp32** 累加。

> 完成后，建议打开 [af_add_ge.py](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/examples/pytorch/af_pointwise/af_add_ge.py) 与其 [README](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/examples/pytorch/af_pointwise/README.md) 对照：你的推演结论（融合成一个 `autofused_*` kernel）与示例描述是否一致。

## 6. 本讲小结

- **Memory Bound**：elementwise / Vector 算子计算量小、搬运量大，相邻算子之间的中间结果要在全局内存里反复读写，瓶颈卡在内存带宽。
- **自动融合的本质**：把若干相邻 Vector 算子合并成一个 kernel，让中间结果只在片上 UB 流转，消除无谓的中间搬运。单链上 \(N\) 个算子的全局内存访问可从 \(2N\) 次降到 2 次。
- **可观测证据**：融合在 profiling 上体现为 `aiv_mte2_time` / `aiv_mte3_time` 的下降，以及产物里出现 `autofused_<算子名>_..._<拓扑哈希>` 形态的单一 kernel。
- **数据流骨架**：Autofuse 内部沿 `graph_metadef → ascir → optimize → att → codegen → compiler` 把一张子图编译成一个融合 kernel（细节后续单元展开）。
- **动态 shape / 混合精度**：Autofuse 支持动态 shape（一个 kernel 适应多种尺寸）与混合精度（块内 fp32 累加、边界保留原 dtype），使累积误差不劣于 Eager。
- **边界提醒**：并非所有算子都会被融合，未识别 / 未 lowering 的算子仍以单算子形式存在；相关 fallback 与开关留给 u3-l3。

## 7. 下一步学习建议

- 想看 Autofuse 各子目录到底各自干什么、数据流怎么衔接 → 学习 **u3-l2 Autofuse 目录结构与六大模块总览**。
- 想亲手跑通 `af_add_ge.py`、用 `TORCH_COMPILE_DEBUG` / `AUTOFUSE_DFX_FLAGS` 观察 `autofused_` 产物与 fallback → 学习 **u3-l3 框架使能与 DFX 调测**。
- 想从「原理」进入「源码」、理解融合范围识别背后的图 IR → 在进阶层从 **u4 graph_metadef 图元数据** 开始。
- 想深入「混合精度的精度边界」证明 → 在专家层学习 **u12-l4 Autofuse 精度一致性原理与验证**。
