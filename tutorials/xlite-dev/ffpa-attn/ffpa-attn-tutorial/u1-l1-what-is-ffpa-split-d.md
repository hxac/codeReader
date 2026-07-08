# FFPA 是什么：Split-D 与大 head_dim 注意力

## 1. 本讲目标

本讲是整本学习手册的第一篇，目标是让你在**不写一行代码**的情况下，建立起对 FFPA（Faster Flash Prefill Attention）这个项目的整体认知。学完本讲后，你应该能够：

1. 说清楚 **FFPA 是什么**，它和 PyTorch 内置的 `scaled_dot_product_attention`（简称 SDPA）、以及经典 FlashAttention-2 是什么关系、有什么差异。
2. 理解**为什么大的 head_dim（D）会让 GPU 显存吃紧**——具体是 SRAM（共享内存）和寄存器两方面的压力，以及 **Split-D 如何化解**。
3. 掌握 FFPA 的**适用场景**与**已知局限**：在什么样的输入下它比 SDPA 快、在什么样的输入下它反而不如 SDPA。

本讲是纯概念 + 源码（文档）阅读型的入门讲义，不要求你有 CUDA 或 Triton 的开发经验。涉及到的关键源码只有两个文档文件：`README.md` 与 `docs/index.md`。

---

## 2. 前置知识

在进入正题前，我们先用大白话把几个会反复出现的术语讲清楚。如果你已经熟悉，可以跳过本节。

**注意力（Attention）** 是 Transformer 的核心运算。给定查询（query，记作 \(Q\)）、键（key，记作 \(K\)）、值（value，记作 \(V\)）三组向量，缩放点积注意力的公式是：

\[
O = \mathrm{softmax}\!\left(\frac{QK^\top}{\sqrt{d}}\right) V
\]

直观地说，就是先用 \(Q\) 和 \(K\) 算出「相关性分数」，归一化（softmax）成一组权重，再用这组权重对 \(V\) 做加权求和，得到输出 \(O\)。

**head_dim（D）** 是每个注意力「头」里单个向量的维度，也就是 \(Q/K/V\) 每一行有多少个元素。常见的 head_dim 是 64、128；而一些新型大模型（如某些 MoE、长上下文模型）会用到 256 甚至更大的 head_dim。FFPA 关注的正是 **D > 256** 这种「大 head_dim」场景。

**SDPA（scaled_dot_product_attention）** 是 PyTorch 自带的注意力算子，背后通常是 FlashAttention 系列实现，质量高、兼容性好，是大家默认使用的「基线」。

**FlashAttention-2（FA-2）** 是目前最主流的高效注意力算法。它通过把注意力矩阵分块（tiling）并在片上高速存储里做 online softmax，避免了把巨大的 \(N \times N\) 注意力矩阵写回显存。**但它有一个硬限制：head_dim 最大只支持到 256。**

**SRAM（Shared Memory，共享内存）** 是 GPU 流多处理器（SM）芯片上的一小块高速存储，容量很小（通常每 SM 几十 KB），但速度极快。kernel 运行时需要把数据从全局显存（HBM）加载到 SRAM 里再计算。SRAM 用得越多，能同时驻留的 block 越少，性能越差。

**寄存器（Register）** 比 SRAM 更靠近计算单元、速度更快，但每个线程能用的寄存器数量也有限。寄存器用爆了，数据就会被「溢出」到 SRAM，性能骤降。

**MMA（Matrix Multiply-Accumulate，矩阵乘加指令）** 是 GPU 上做小矩阵乘法（比如 16×16 这种片段）的硬件指令。GPU 上的大矩阵乘，本质上是无数个小 MMA 拼出来的。FFPA 的 Split-D 思想正是「在 MMA 这一层」做文章，本讲后面会详细展开。

**O(1) 复杂度**：在算法分析里，\(O(1)\) 表示「与输入规模无关的常数」。FFPA 宣称 SRAM 复杂度是 \(O(1)\)，意思是**无论 head_dim D 多大，它占用的 SRAM 都是一个固定的小常数**，这正是它能在超大 D 下仍高效运行的关键。

---

## 3. 本讲源码地图

本讲涉及的关键文件只有两个，都是项目文档（而非 kernel 源码）。真正的 kernel 实现会在后续讲义（如 `u4-l1`、`u4-l2`）深入。

| 文件 | 作用 | 本讲用到哪部分 |
|---|---|---|
| `README.md` | 项目主页，包含项目定位、Split-D 设计章节、后端能力矩阵、快速上手 | 第 11 行项目定位；第 13–19 行功能表；第 58–72 行 Split-D 章节；第 85–101 行后端表 |
| `docs/index.md` | 在线文档（ReadTheDocs）首页，内容比 README 更完整，含多种使用示例 | 第 13 行项目定位；第 202–216 行 Split-D 章节；第 67 行关于 FA-2/FFPA head_dim 范围的注释 |

> 提示：`README.md` 和 `docs/index.md` 的 Split-D 段落文字几乎完全一致，是因为它们描述的是同一个核心设计。本讲会两个都引用，但讲解时以 `README.md` 为主。

---

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：先讲清「问题」（大 head_dim 为什么难），再讲「方案」（Split-D），再讲「结果」（复杂度），最后讲「边界」（什么时候用、什么时候不用）。

### 4.1 注意力计算与 FlashAttention 的 head_dim 上限

#### 4.1.1 概念说明

这一模块要回答一个前置问题：**既然 FlashAttention 已经这么快了，为什么还需要 FFPA？**

答案是：FlashAttention-2 有一个「天花板」——它只支持 head_dim \(D \le 256\)。而近年的大模型（尤其长上下文、MoE 架构）开始使用 \(D = 320, 512, 1024\) 这种大 head_dim。一旦 \(D > 256\)，标准 FA-2 的 tiling（分块）策略就会因为「每个数据块太宽」而把 SRAM 撑爆，只能退化成更慢的实现，甚至直接不可用。

FFPA 的项目定位，正是在 README 顶部那行标语里点明的——它是一个**为「大 head_dim」专门设计的、更快的 Flash Prefill Attention**，并且号称拥有 \(O(1)\) 的 SRAM 复杂度。

[README.md:11](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/README.md#L11-L11) 这一行就是项目的一句话定位：FFPA 用 Split-D 策略，为大 head_dim（>256）实现了 \(O(1)\) SRAM 复杂度和 \(O(d/4)\) 寄存器复杂度，比 SDPA 快 1.5~3 倍。

#### 4.1.2 核心流程

标准 FlashAttention 的分块思路，是沿着**序列长度 \(N\)** 方向切分（把 \(Q\) 切成 \(B_r\) 行一块，把 \(K/V\) 切成 \(B_c\) 行一块），在 SRAM 里做 online softmax。问题在于：每一块 \(Q\) 的形状是 \(B_r \times D\)，每一块 \(K\)、\(V\) 的形状是 \(B_c \times D\)。

当 \(D\) 很大时：

\[
\text{SRAM 占用} \;\propto\; (B_r + 2 B_c) \times D
\]

也就是说，**SRAM 占用随 \(D\) 线性增长**。\(D=256\) 时还能放下，\(D=1024\) 时单块就要占用 4 倍 SRAM，直接超出每 SM 的 SRAM 预算，导致能并发的 block 数量锐减、性能崩塌。

用伪流程概括标准 FA 在大 D 下的困境：

```
标准 FA 分块（按 N 切）:
    加载 Q 块  B_r × D     -> SRAM 占用随 D 线性增长
    加载 K 块  B_c × D     -> SRAM 占用随 D 线性增长
    加载 V 块  B_c × D     -> SRAM 占用随 D 线性增长
    当 D > 256: 三块之和超出 SRAM 预算 -> 性能骤降 / 不可用
```

#### 4.1.3 源码精读

我们看 README 顶部那张「功能表」，它用一行就标明了 FFPA 支持的 head_dim 范围和加速比：

[README.md:13-19](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/README.md#L13-L19) 这张表里，`Headdim` 列写的是 **320~1024**，`Fwd/Bwd` 列写的是 **1.5~3x↑**。这两组数字定义了 FFPA 的工作区间：它只在 D ∈ [320, 1024] 这个「大 head_dim」区间发力，且相对 SDPA 有 1.5~3 倍加速。

`docs/index.md` 的自注意力示例代码注释里，把这条「FA-2 上限 256、FFPA 上限 1024」的边界写得更直白：

[docs/index.md:67](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/index.md#L67-L67) 注释 `# D: 32, 64, ..., 320, ..., 1024 (FA-2 <= 256, FFPA supports up to 1024).` 明确说明：FA-2 止步于 256，而 FFPA 最高可到 1024。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是让你亲手确认「FFPA 的 head_dim 工作区间」。

1. **实践目标**：从文档中提取出 FFPA 与 FA-2 在 head_dim 上的能力边界。
2. **操作步骤**：
   - 打开 `README.md`，定位到第 13–19 行的功能表。
   - 找到 `Headdim` 那一列，记下它的取值范围。
   - 再打开 `docs/index.md`，定位到第 67 行那段自注意力示例代码的注释。
3. **需要观察的现象**：两处文档对 head_dim 范围的描述应该是一致的（都是 320~1024），并且都点明了 FA-2 的上限是 256。
4. **预期结果**：你会得到一张对照表——FA-2：D ≤ 256；FFPA：D ∈ [320, 1024]。两者在 256 与 320 之间几乎没有重叠，FFPA 恰好填补了 FA-2 之上的空白区间。
5. 如果暂时无法在本地打开仓库浏览，这部分是纯文档阅读，不影响理解。

#### 4.1.5 小练习与答案

**练习 1**：为什么说 FFPA 和 FA-2 是「互补」而非「替代」关系？

> **参考答案**：因为两者的 head_dim 工作区间几乎不重叠。FA-2 专精 D ≤ 256（覆盖绝大多数传统模型），FFPA 专精 D ∈ [320, 1024]（大 head_dim 新场景）。FFPA 在小 D 下并不比 SDPA 快（见 4.4 节），所以它并不是要取代 FA-2，而是补上 FA-2 够不着的那段区间。

**练习 2**：标准 FA 按 \(N\) 方向分块时，\(Q\) 块、\(K\) 块、\(V\) 块各自的 SRAM 占用形状是什么？为什么 \(D\) 变大会出问题？

> **参考答案**：\(Q\) 块是 \(B_r \times D\)，\(K\)、\(V\) 块是 \(B_c \times D\)。三者宽度都是 \(D\)，所以总 SRAM 占用 \(\propto (B_r + 2B_c) \cdot D\)，随 \(D\) 线性增长；当 \(D > 256\) 时单 SM 的 SRAM 预算装不下足够多的块，并发度下降，性能崩塌。

---

### 4.2 Split-D：在 MMA 层做 D 维精细分块

#### 4.2.1 概念说明

这是整个项目的**核心创新**，也是「Split-D」这个名字的由来。

标准 FA 沿 \(N\) 方向分块，但每个块的「宽度」仍然是完整的 \(D\)。Split-D 的关键洞察是：**不仅要沿 \(N\) 切，还要沿 \(D\)（head_dim 维度）继续切成很窄的片段**，而且这个切分要精细到 **MMA 指令**那一级别（一个片段宽 16）。

具体地说，把 \(QK^\top\) 和 \(PV\) 这两次矩阵乘，都在 \(D\) 方向切成宽为 16 的片段来做。这样，任何时候驻留在 SRAM 里的 \(Q/K/V\) 切片，宽度都只有 16，而不是完整的 \(D\)。

这正是 README/docs 里对 Split-D 的官方定义。

#### 4.2.2 核心流程

Split-D 把注意力里的两次矩阵乘拆开看：

**第一次乘：\(S = QK^\top\)（算分数）**

分数矩阵 \(S\) 的形状是 \(B_r \times B_c\)，**与 \(D\) 无关**（它沿 \(D\) 做内积）。Split-D 让这个内积**分多次完成**：每次只取 \(Q\) 和 \(K\) 在 \(D\) 方向上的一个 16-宽片段，做一次小 MMA 得到一个部分分数，再把所有片段的部分分数累加起来，得到完整的 \(S\)。

```
算分数 S = Q K^T   (S 形状 B_r × B_c，与 D 无关)
    for d = 0, 16, 32, ..., D-16:          # 沿 D 切成 16 宽片段
        加载 Q 切片  B_r × 16   -> SRAM (只占 B_r × 16)
        加载 K 切片  B_c × 16   -> SRAM (只占 B_c × 16)
        S += (Q切片) @ (K切片)^T            # MMA 累加部分分数
    # 循环结束后，S 里是完整的分数
```

**第二次乘：\(O = P V\)（算输出，\(P=\mathrm{softmax}(S)\)）**

\(P\) 的形状是 \(B_r \times B_c\)（完整宽度，沿 \(B_c\) 归一化）；输出 \(O\) 的形状是 \(B_r \times D\)。这里 Split-D 把 **\(V\) 沿 \(D\) 切成 16-宽的 V-group**，每次只算输出 \(O\) 的一个 16-宽片段：

```
算输出 O = P V     (O 形状 B_r × D)
    for vg = 0, 16, 32, ..., D-16:          # V 沿 D 切成 16 宽 group
        加载 V 切片  B_c × 16   -> SRAM (只占 B_c × 16)
        O[*, vg:vg+16] = P @ (V切片)        # MMA 算输出的一个 16 宽片段
    # 循环结束后，O 里是完整输出
```

两次乘的共同点：**SRAM 里任何时刻只出现宽度为 16 的 \(Q/K/V\) 切片**。这就是「Split-D」——把 \(D\) 劈成 16 宽的小片。

> 注意：上面是教学用的伪代码，只为说明思路。真实的 Triton kernel 里 `NUM_V_GROUPS`、`BLOCK_HEADDIM_V` 等实现细节，会在讲义 `u4-l2`（Split-D 精细分块）里精读，本讲不展开。

#### 4.2.3 源码精读

Split-D 的官方定义在 README 的「Split-D」章节开头那一整段：

[README.md:58-72](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/README.md#L58-L72) 其中第 62 行是核心定义句：FFPA 通过在 **MMA 层**对 \(QK^\top\) 和 \(PV\) 做 **fine-grained tiling（精细分块）** 来支持大 head_dim，称为 Split-D。这一段还点明了 SRAM 占用固定在 \(B_r \times 16\)（且 \(B_r = B_c\)），给出 \(O(B_r \times 16) \approx O(1)\) 的 SRAM 复杂度与 \(O(d/4)\) 的寄存器复杂度。

`docs/index.md` 里这段定义完全一致：

[docs/index.md:202-216](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/index.md#L202-L216) 第 206 行与 README 第 62 行是同一句话，确认了 Split-D = 「MMA 级 fine-grained tiling for \(QK^\top\) and \(PV\)」。

#### 4.2.4 代码实践

1. **实践目标**：在文档里精确定位「MMA 级精细分块」这个表述，确认 Split-D 的切分粒度。
2. **操作步骤**：
   - 打开 `README.md` 第 62 行（或 `docs/index.md` 第 206 行）。
   - 圈出句子里的两个关键词：`fine-grained tiling` 和 `at the MMA level`。
   - 留意它明确指出是对 \(QK^\top\) **和** \(PV\) **两次**矩阵乘都做这种分块，不是只对其中一次。
3. **需要观察的现象**：定义句会同时提到 \(QK^\top\) 和 \(PV\)，说明 Split-D 是贯穿前后两次乘的统一策略。
4. **预期结果**：你能在不依赖任何 kernel 代码的前提下，仅凭这一句文档，向别人解释「Split-D = 把 \(QK^\top\) 与 \(PV\) 在 MMA 层切成 16 宽片段来做」。
5. 无需运行任何命令，纯文档阅读。

#### 4.2.5 小练习与答案

**练习 1**：Split-D 的「分块」和标准 FlashAttention 的「分块」，切的方向有什么不同？

> **参考答案**：标准 FA 主要沿**序列长度 \(N\)** 方向切（\(B_r\) 行、\(B_c\) 行），但每块的宽度仍是完整的 \(D\)；Split-D 在此基础上**额外沿 head_dim \(D\) 方向切**，把宽度也降到 16。所以 Split-D 是「\(N\) 方向分块 + \(D\) 方向细切」的双重分块。

**练习 2**：为什么分数矩阵 \(S = QK^\top\) 的形状与 \(D\) 无关？这对 Split-D 有什么好处？

> **参考答案**：\(S = QK^\top\) 中 \(Q\) 是 \(B_r \times D\)、\(K^\top\) 是 \(D \times B_c\)，矩阵乘后 \(D\) 维被「内积消掉」，\(S\) 形状是 \(B_r \times B_c\)，与 \(D\) 无关。好处是：\(S\) 可以先在 SRAM 里用一个固定大小（与 \(D\) 无关）的缓冲区累加，再算 softmax，这正是 online softmax 能在固定 SRAM 下工作的前提。

---

### 4.3 O(1) SRAM 与 O(d/4) 寄存器复杂度

#### 4.3.1 概念说明

有了 Split-D 的切分方式，我们就能算清楚两件事的复杂度：**SRAM** 和**寄存器**。这是 FFPA 最硬核的两个结论，也是它敢宣称支持到 \(D=1024\) 的底气。

- **SRAM 复杂度 \(O(1)\)**：因为 Split-D 让 \(Q/K/V\) 的切片宽度固定为 16，所以 SRAM 占用是 \(B_r \times 16\)（\(B_r\)、16 都是常数，与 \(D\) 无关），即 \(O(1)\)。
- **寄存器复杂度 \(O(d/4)\)**：输出 \(O\) 的形状是 \(B_r \times D\)，它必须**完整地**跨在寄存器里做累加（不能像 SRAM 那样切片，因为每个输出元素要持续累加）。所以寄存器用量随 \(D\) 线性增长，量级是 \(O(d/4)\)（这里的 1/4 因子来自 MMA 片段的打包方式，会在 kernel 讲义里讲清）。

换句话说：**Split-D 把「随 D 爆掉的那部分压力」从 SRAM 转移到了寄存器**，并且 SRAM 部分干脆变成了与 D 无关的常数。由于 GPU 的寄存器总量比单 block 可用的 SRAM 大得多、且能分摊到大量线程上，这种转移在大 D 下是划算的。

#### 4.3.2 核心流程

我们用一张「前后对比」来量化这个收益。设 \(B_r = B_c = B\)。

| 资源 | 标准 FA（宽 D 切块） | Split-D（宽 16 切块） | 是否随 D 增长 |
|---|---|---|---|
| Q 块在 SRAM | \(B \times D\) | \(B \times 16\) | FA：是；Split-D：**否** |
| K 块在 SRAM | \(B \times D\) | \(B \times 16\) | FA：是；Split-D：**否** |
| V 块在 SRAM | \(B \times D\) | \(B \times 16\) | FA：是；Split-D：**否** |
| SRAM 总复杂度 | \(O(D)\) | \(O(B \times 16) \approx O(1)\) | Split-D **常数** |
| 输出 O 在寄存器 | \(B \times D\) | \(B \times D\) | 两者**都是** \(O(D)\) |

关键结论可以用公式浓缩：

\[
\text{SRAM: } O(B_r \times 16) \approx O(1) \quad\text{（与 } D \text{ 无关）}
\]

\[
\text{寄存器: } O(d/4) \quad\text{（随 } D \text{ 线性，但分摊到线程，可承受）}
\]

举个直观的数字：当 \(D\) 从 256 涨到 1024（4 倍）时，标准 FA 的 SRAM 占用也涨 4 倍（很可能直接爆掉），而 Split-D 的 SRAM 占用**纹丝不动**——它永远只放一个 16 宽的切片。

#### 4.3.3 源码精读

这两个复杂度结论，在项目首页标语和 Split-D 章节里被反复强调。

[README.md:11](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/README.md#L11-L11) 标语句直接写明 `O(1) SRAM complexity and O(d/4) register complexity`，这是这两个复杂度结论首次出现的地方。

[README.md:62](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/README.md#L62-L62) Split-D 定义句进一步解释：SRAM 占用「fixed at \(B_r \times 16\)（with \(B_r = B_c\)）」，由此得到 \(O(B_r \times 16) \approx O(1)\) 的 SRAM 与 \(O(d/4)\) 的寄存器复杂度。

[docs/index.md:206](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/index.md#L206-L206) 在线文档里同样的句子，作为交叉印证。

> 说明：README/docs 只给出了复杂度结论与「\(B_r \times 16\)」的来源，但**没有**展开解释 \(O(d/4)\) 里那个「除以 4」具体怎么来的。它和 fp16 数据在 MMA fragment（如 m16n8k16）里的打包布局有关，属于 kernel 实现细节。本讲不臆测，留到讲义 `u4-l2`/`u5-l4` 讲到 Triton kernel 的寄存器布局时再精确推导。在此之前，我们只需记住结论：**SRAM 是常数，寄存器随 D 线性、且有 1/4 的打包折扣**。

#### 4.3.4 代码实践

1. **实践目标**：把「SRAM 常数 vs 寄存器线性」这个不对称结论，从文档里落实成你自己的理解。
2. **操作步骤**：
   - 重读 `README.md` 第 62 行，把 `SRAM usage fixed at Br × 16` 与 `O(d/4) register complexity` 这两处圈出来。
   - 思考一个问题：为什么 \(O\)（输出）不能像 \(Q/K/V\) 那样也切成 16 宽驻留 SRAM？提示：\(Q/K/V\) 切片是「读一次用一次」的输入，而 \(O\) 是「每个元素都要跨所有 KV 块持续累加」的输出，必须常驻。
3. **需要观察的现象**：你会意识到——文档把 SRAM 和寄存器分开列两个复杂度，正是因为 Split-D 对这两者的效果不对称（一个变常数，一个仍是线性）。
4. **预期结果**：你能用自己的话讲清「Split-D 把 D 方向的压力从 SRAM 搬到了寄存器，而寄存器更能扛」。
5. 待本地验证：如果你想进一步验证寄存器压力的真实表现，需要编译并跑 kernel、用 `nvcc --ptxas-options=-v` 或类似工具看寄存器占用，这超出了本篇入门讲义的范围，留到后续 CUDA/Triton 讲义。

#### 4.3.5 小练习与答案

**练习 1**：为什么 SRAM 复杂度能写成 \(O(B_r \times 16) \approx O(1)\)？这里的「常数」是相对于什么而言的？

> **参考答案**：因为 \(B_r\)（行块大小，如 64/128）和 16（MMA 片段宽度）都是与 head_dim \(D\) 无关的固定值，所以 \(B_r \times 16\) 是一个常数。写成 \(O(1)\) 是**相对于 \(D\)** 而言的——即无论 \(D=320\) 还是 \(1024\)，SRAM 占用都不变。

**练习 2**：如果未来某个模型用到 \(D=2048\)，按本讲的复杂度结论，FFPA 的 SRAM 和寄存器分别会怎样变化？

> **参考答案**：SRAM 仍是 \(O(B_r \times 16)\)，**不变**；寄存器按 \(O(d/4)\) **继续线性增长**到原来的 2 倍（相对 \(D=1024\)）。所以瓶颈会从 SRAM 转移到寄存器是否够用——这也是 FFPA 上限定在 1024 的现实约束之一（具体上限以项目当前支持为准，README 标注为 320~1024）。

---

### 4.4 适用场景与已知局限

#### 4.4.1 概念说明

一项技术再好，也不可能「全场景通吃」。FFPA 自己在文档里就坦白了它的边界：**它主要面向 prefill（预填充）+ 大 head_dim 的场景；当序列长度太短（N < 512）或 head_dim 太小（D ≤ 256）时，它可能并不比 SDPA 快，甚至更慢。**

理解这条边界非常重要，因为它直接决定了「你该不该用 FFPA」。在实际集成时，FFPA 也正是依据这类条件来判断：满足大 D、长序列时自己上，否则自动**回退（fallback）到 SDPA**。这个回退机制是后续讲义（`u3-l3` 校验与回退）的主题，这里只建立直觉。

#### 4.4.2 核心流程

FFPA 的「用 or 不用」决策，可以概括成一个简单的判别表：

| 输入条件 | FFPA 是否值得用 | 原因 |
|---|---|---|
| 大 head_dim（D ∈ [320, 1024]）且长序列（N ≥ 512） | ✅ 值得，1.5~3x 加速 | 正是 Split-D 的主场，SRAM 常数优势充分发挥 |
| 小 head_dim（D ≤ 256） | ❌ 不如 SDPA，回退 | 标准 FA-2 在小 D 下已经很高效，Split-D 多次 MMA 反成开销 |
| 短序列（N < 512） | ❌ 不如 SDPA，回退 | 序列短、注意力矩阵小，kernel 启动/分块开销占比过大 |

所以 FFPA 的设计哲学是：**长 prefill + 大 D 才是它的舞台**；其他场景乖乖让位给 SDPA。这也解释了它名字里的「**Prefill**」——它就是为预填充阶段量身定制的。

#### 4.4.3 源码精读

这条适用边界，文档用一个醒目的 `> [!NOTE]` 提示框写得很清楚：

[README.md:71-72](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/README.md#L71-L72) 第 72 行的 NOTE 明确说：FFPA 主要为 **prefill 和大 head_dim** 设计，对于**小序列长度（N < 512）或小 head_dim（D ≤ 256）可能并不比 SDPA 快**。这一行是整本手册里关于「FFPA 边界」最权威的一句话。

[docs/index.md:215-216](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/index.md#L215-L216) 同一条 NOTE 在在线文档里的版本，文字一致。

此外，README 的快速上手示例还展示了 FFPA 与 SDPA 的「优雅共存」方式——一行 monkey-patch，让 FFPA 接管大 D，其余自动回退 SDPA：

[README.md:46-54](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/README.md#L46-L54) 这段示例注释里写明：`Everything that FFPA does not support will auto fallback to SDPA: D <= 256, etc.`（FFPA 不支持的情形会自动回退到 SDPA，例如 D ≤ 256）。这正是「适用边界」在 API 层面的体现——你不用手动判断，FFPA 会自己回退。（具体的回退判定代码在 `functional.py` 的 `FFPAAttnMeta.fallback()`，将在 `u3-l3` 精读。）

#### 4.4.4 代码实践

1. **实践目标**：从文档里提炼出 FFPA 的「三个回退条件」并理解每条的物理直觉。
2. **操作步骤**：
   - 打开 `README.md` 第 72 行（或 `docs/index.md` 第 216 行）的 NOTE。
   - 找出 NOTE 里列出的「不适合 FFPA」的两类输入。
   - 结合 4.2 节的 Split-D 原理，思考「为什么 D 小或 N 小时 Split-D 不划算」。
3. **需要观察的现象**：NOTE 用词是 `may not be faster`（可能不更快），说明这是经验性的性能边界，而非硬性错误——FFPA 在这些场景下仍可运行，只是不划算，所以选择回退。
4. **预期结果**：你能写出三条「FFPA 适用判据」——大 D（≥320）、长序列（≥512）、prefill 阶段；并解释为什么短序列下 Split-D 的多次 MMA 反而是负担。
5. 待本地验证：若要在 GPU 上实测这条边界（例如对比 D=128 与 D=512 下 FFPA vs SDPA 的耗时），需要先按 `u1-l2` 安装好环境，超出本讲范围。

#### 4.4.5 小练习与答案

**练习 1**：FFPA 名字里的「Prefill」意味着它的最佳场景是推理/训练的哪个阶段？为什么 decode（逐 token 解码）阶段不一定吃香？

> **参考答案**：最佳场景是**预填充（prefill）阶段**，即一次性处理一整段长 prompt，序列长（N 大）、计算密集。而 decode 阶段通常是 \(N_q = 1\)（每次只算一个新 token 的 query），序列短、计算稀疏，Split-D 的多次 MMA 开销相对收益不划算；不过 FFPA 也为 decode 提供了专门的 split-KV 路径（见 `u4-l3`）来缓解，这属于后续内容。

**练习 2**：如果用户对 D=128 的输入强制使用 FFPA（不让它回退），按本讲的分析，性能大概会怎样？为什么？

> **参考答案**：大概率**不比 SDPA 快，甚至更慢**。原因是：D=128 时标准 FA-2 本身就很高效；而 Split-D 要把这个不大的 D 也切成 8 个 16-宽片段、跑多轮 MMA 与累加，引入了额外循环和指令开销，却几乎换不来 SRAM 收益（因为 D 本来就不大，SRAM 压力本不严重）。所以文档说这类场景「may not be faster」。

---

## 5. 综合实践

本讲的综合实践，是把 4.2 节的 Split-D 切分思路**画成一张你自己的示意图**，并用 4.3 节的复杂度结论**解释它为什么能省 SRAM**。这是整篇讲义最关键的一次「动手」——它检验你是否真的把 Split-D 内化了。

**实践目标**：用一张手画图 + 一段文字说明，向一个没读过 FFPA 的人讲清楚「Split-D 如何让大 head_dim 注意力的 SRAM 占用变成常数」。

**操作步骤**：

1. **画出标准注意力的两次乘**：
   - 左图：\(S = QK^\top\)。画一个 \(B_r \times D\) 的 \(Q\) 矩阵、一个 \(B_c \times D\) 的 \(K\) 矩阵，中间标出它们的内积得到 \(B_r \times B_c\) 的分数矩阵 \(S\)。
   - 右图：\(O = PV\)。画一个 \(B_r \times B_c\) 的 \(P\) 矩阵、一个 \(B_c \times D\) 的 \(V\) 矩阵，得到 \(B_r \times D\) 的输出 \(O\)。
2. **在两张图上标出 Split-D 的切片**：
   - 在 \(Q\) 和 \(K\) 上沿 \(D\) 方向画虚线，切成宽 16 的小条（共 \(D/16\) 条）。标注：算 \(S\) 时，每次只把 \(Q\)、\(K\) 的**一个 16 宽片段**加载进 SRAM，做一次 MMA，累加到 \(S\)。
   - 在 \(V\) 上沿 \(D\) 方向画虚线，切成宽 16 的 V-group。标注：算 \(O\) 时，每次只把 \(V\) 的**一个 16 宽 V-group** 加载进 SRAM，算出 \(O\) 的一个 16 宽片段。
3. **写出 SRAM 复杂度说明**（约 150–300 字），要点包括：
   - 任何时刻 SRAM 里只有 16 宽的 \(Q/K/V\) 切片，占用 = \(B_r \times 16\)（\(Q\)）、\(B_c \times 16\)（\(K\)、\(V\)）。
   - 因为 \(B_r\)、16 都是与 \(D\) 无关的常数，所以 SRAM 复杂度 \(O(B_r \times 16) \approx O(1)\)。
   - 与标准 FA 的 \(O(D)\) 对比，\(D\) 从 256 涨到 1024 时，Split-D 的 SRAM 占用不变，而标准 FA 会涨 4 倍。

**需要观察的现象（在你自己的图与文字里自检）**：

- 你的图里，SRAM 区域（你可以用方框圈出来）应该**只包含 16 宽的切片**，绝不能出现完整的 \(D\) 宽块。
- 你的文字里应该明确点出「常数是相对 \(D\) 而言」，而不是绝对的零开销。

**预期结果**：完成一张标注清晰的 Split-D 示意图 + 一段能自洽解释 \(O(1)\) SRAM 的文字。如果你能对着图把 4.2 节的伪流程完整复述一遍，说明你已经掌握了本讲的核心。如果某些细节（比如 16 这个数字为什么是 16、寄存器的 \(d/4\) 怎么来）你还说不清，那很正常——它们属于后续 kernel 讲义（`u4-l1`、`u4-l2`）的内容。

---

## 6. 本讲小结

- **FFPA 是为大 head_dim（D ∈ [320, 1024]）设计的更快 Flash Prefill Attention**，比 SDPA 快 1.5~3 倍；它和经典 FA-2（D ≤ 256）是互补关系，不是替代。
- **大 head_dim 的核心困难是 SRAM 爆炸**：标准 FA 沿 \(N\) 分块，每块宽 \(D\)，SRAM 占用 \(\propto D\)，\(D > 256\) 时撑爆单 SM 的 SRAM 预算。
- **Split-D 是 FFPA 的核心创新**：在 MMA 层把 \(QK^\top\) 与 \(PV\) 沿 \(D\) 方向切成 **16 宽**的精细片段，让 SRAM 里任何时刻只有 16 宽的 \(Q/K/V\) 切片。
- **复杂度结论**：SRAM 复杂度 \(O(B_r \times 16) \approx O(1)\)（与 D 无关）；寄存器复杂度 \(O(d/4)\)（随 D 线性，但分摊到线程、可承受）。Split-D 把 D 方向的压力从 SRAM 转移到了寄存器。
- **适用边界**：FFPA 主攻 **prefill + 大 D + 长序列**；当 **D ≤ 256** 或 **N < 512** 时未必比 SDPA 快，因此会自动回退到 SDPA。
- **API 关系**：FFPA 提供 `ffpa_attn_func`，签名与 PyTorch SDPA 对齐，支持一行 monkey-patch 接管大 D、其余回退 SDPA。

---

## 7. 下一步学习建议

本讲只建立了「FFPA 是什么、为什么需要它」的概念。建议按以下顺序继续：

1. **先把 FFPA 跑起来**：进入 `u1-l2`（安装、构建与三种构建模式），学会从 PyPI 或源码安装，区分 Triton-only 与 CUDA 扩展两种构建产物。
2. **熟悉仓库结构**：进入 `u1-l3`（仓库目录结构与代码地图），建立对 `src/ffpa_attn`、`csrc/cuffpa`、`bench`、`tests` 等目录的整体认知，方便后续定位源码。
3. **亲手试用 API**：进入 `u1-l4`（一行代码替换 SDPA）和整个第 2 单元，跑通自注意力、交叉注意力、GQA、varlen 等典型用例，建立对 `ffpa_attn_func` 的肌肉记忆。
4. **想看 Split-D 的真实 kernel 实现**：可以跳到第 4 单元的 `u4-l1`（Triton 前向 kernel 与 online softmax）和 `u4-l2`（Split-D 精细分块），届时本讲的「16 宽片段」「O(d/4) 寄存器」都会在源码里一一兑现。

> 阅读源码建议：本讲引用的 `README.md`、`docs/index.md` 是全文最轻量的入口，建议先通读一遍这两份文档（约 10 分钟），再进入后续需要看 Python/CUDA 源码的讲义，会顺畅很多。
