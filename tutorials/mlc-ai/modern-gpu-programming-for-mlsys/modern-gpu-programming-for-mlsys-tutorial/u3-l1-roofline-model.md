# Roofline 模型与算术强度（u3-l1）

## 1. 本讲目标

学完本讲，你应该能够：

1. 用**带宽**和**算力**两个硬件上限，推出一个内核的性能屋顶（roofline），并计算 B200 上的拐点（ridge point ≈ 250 FLOP/byte）。
2. 对 elementwise、reduction、GEMM、attention 四类典型算子**动手计算算术强度**（arithmetic intensity，AI），并据此判断内核是 memory-bound 还是 compute-bound。
3. 区分 compute-bound 与 memory-bound 两类内核，并说出它们各自正确的优化方向（避免在不是瓶颈的资源上白费力气）。

本讲是单元三「性能模型与优化方法」的第一讲。上一单元（u2-l3）我们知道了 GPU 内部有三类引擎（CUDA Core、Tensor Core、TMA）并且它们可以重叠工作；本讲回答优化之前更根本的问题：**这个内核的性能天花板在哪里？哪条路先到顶？**

本讲所有实践只需要 Python（numpy、matplotlib），**不需要 GPU**。

## 2. 前置知识

本讲会用到的概念，均已在前面讲义建立，这里做最小回顾：

- **FLOP 与 FLOPs**：FLOP 是浮点运算次数（FLoating-point OPeration）的计数单位。按全书约定：一次浮点加法或乘法计 1 FLOP；一次融合乘加 `a * b + c`（FMA）计 2 FLOP。注意 FLOP 数是**数学运算量**，不等于内核执行的指令条数。
- **带宽（bandwidth）**：某一级内存在单位时间内能搬运的数据量，单位 GB/s 或 TB/s。承接 u2-l2：GPU 有 HBM（GMEM 背后的显存）、L2、SMEM 等多层存储，**每层各有自己的带宽**，谈带宽必须指明层级。
- **三类引擎**（u2-l3）：CUDA Core 做标量/向量运算，Tensor Core 做矩阵乘累加，TMA 做异步整块搬运。"算力天花板"指的是你内核所走那条计算路径对应引擎的峰值吞吐。
- **数量级近似**：本讲沿用书中两个取整值——B200 稠密 fp16/bf16 Tensor Core 峰值约 2 PFLOP/s（即 2000 TFLOP/s）、HBM3e 带宽约 8 TB/s。它们是方便计算的近似，不是规格书数值。
- **对数坐标**：roofline 图的横轴（AI）跨越多个数量级（0.1 到上万 FLOP/byte），所以 x、y 轴都用对数刻度。读图时等距离代表等倍数。

一个后面反复用到的小单位技巧：\( \text{TB/s} \times \text{FLOP/byte} \) 在数值上恰好等于 \( \text{TFLOP/s} \)（两个 \(10^{12}\) 相消）。所以「8 TB/s 带宽 × AI」直接得到「8 × AI TFLOP/s」的性能上限。

## 3. 本讲源码地图

本仓库是一本开源教材（承接 u1-l1/u1-l2 的认知：产品是 Sphinx 文档站点），所以本讲的"源码"由**章节正文**和**生成书中插图的脚本**两部分构成：

| 文件 | 作用 |
| --- | --- |
| [chapter_performance/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md) | 本章正文《What Makes a Kernel Fast》：roofline 模型定义、算术强度约定、常见算子分析，以及内存受限/计算受限两类优化方向（L150 之后的优化部分是下一讲 u3-l2 的主战场） |
| [img/scripts/gen_roofline.py](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_roofline.py) | 用 matplotlib 生成书中 roofline 插图的脚本：B200 取整参数、屋顶曲线、拐点标注和三个示例工作负载点，全都在这 54 行里 |
| [img/scripts/README.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/README.md) | 全部图表脚本的运行说明：必须 `cd img/scripts` 后运行；依赖 matplotlib 与 numpy |
| [img/roofline.png](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/roofline.png) | 上述脚本的产出，被正文在 [chapter_performance/index.md:L106](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L106) 引用 |

中文读者可对照中文镜像 [zh/chapter_performance/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/zh/chapter_performance/index.md)（中英同构，路径加 `zh/` 前缀，见 u1-l2）。

## 4. 核心概念与源码讲解

### 4.1 roofline 模型：给内核一把尺子

#### 4.1.1 概念说明

一个内核"快不快"只有相对天花板才有意义。正文的第一个论点就是：330 TFLOP/s 这个数字单看很大，但在一个稠密 fp16 Tensor Core 能持续约 2 PFLOP/s 的 GPU 上，它只发挥了约 1/6 的算力——大片芯片在空转。没有天花板，你无法区分"接近硬件极限"和"还有十倍空间"。

roofline 模型把这把尺子做成两条线：

- **计算屋顶（compute roof）**：当前计算路径的峰值吞吐（fp16 GEMM 来自 Tensor Core；elementwise 内核可能来自 CUDA Core 等）。
- **内存屋顶（memory roof）**：带宽 × 算术强度。每搬一个字节能支撑多少运算，决定了这条斜线的斜率。

内核能达到的性能被两条线的**较低者**封顶。两线交点叫**拐点（ridge point）**，是 memory-bound 与 compute-bound 的分界。B200 上：

\[
\text{ridge point} = \frac{\text{峰值计算吞吐}}{\text{带宽}} \approx \frac{2000\ \text{TFLOP/s}}{8\ \text{TB/s}} \approx 250\ \text{FLOP/byte}
\]

AI 低于 250 → 内存带宽先到顶（memory-bound）；高于 250 → 计算吞吐先到顶（compute-bound）。

#### 4.1.2 核心流程

给定一个内核，roofline 分析的流程是：

```text
输入: 计算量 F (FLOP)、对某内存层级的流量 B (byte)、该层级带宽 BW、计算路径峰值 PEAK

1. AI = F / B                      # 算术强度，FLOP/byte
2. memory_roof = BW × AI           # 数值上 TB/s × FLOP/byte = TFLOP/s
3. roof = min(PEAK, memory_roof)   # 内核的性能上限
4. ridge = PEAK / BW               # B200 上 ≈ 250 FLOP/byte
5. 分类:
     AI < ridge  → memory-bound（优化方向: 少搬字节）
     AI > ridge  → compute-bound（优化方向: 少让计算路径空闲）
     AI ≈ ridge  → 两个上限相近，都可能卡
```

画在图上（对数坐标）：

- 内存屋顶是一条斜率为带宽的直线：\[ \text{performance} = \text{bandwidth} \times \text{AI} \]
- 计算屋顶是一条水平线：\[ \text{performance} = \text{peak compute throughput} \]
- 两者交点即拐点；整条屋顶线呈「斜线抬升 → 水平封顶」的 L 形（对数坐标下是两条直线）。

注意一个重要限定（正文反复强调）：这个分类是**初始判断**，不是测量与剖析的替代品。它的价值是给优化方向——对 memory-bound 内核省几条算术指令几乎无用，对 compute-bound 内核做个小访存优化也动不了主要瓶颈。

#### 4.1.3 源码精读

**（1）为什么需要天花板，以及两个取整数字。** [chapter_performance/index.md:L12-L19](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L12-L19) 用 330 TFLOP/s 的例子说明性能必须相对天花板解读，并给出 B200 的两个近似值：约 2 PFLOP/s 稠密 fp16/bf16 Tensor Core 吞吐、约 8 TB/s HBM3e 带宽，同时声明这些数值随 SKU、时钟、功耗与测量环境变化。

**（2）两个天花板的定义。** [chapter_performance/index.md:L21-L31](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L21-L31) 定义计算屋顶（当前计算路径能提供的最大 FLOP/s）与内存带宽（某级内存单位时间的搬运量），并明确本章"内存带宽"默认指 HBM 带宽——**带宽永远绑定具体内存层级**。

**（3）roofline 不等式。** [chapter_performance/index.md:L39-L44](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L39-L44) 给出核心公式：

\[
\text{attainable performance} \le \min(\text{peak compute throughput},\ \text{memory bandwidth} \times \text{arithmetic intensity})
\]

**（4）图上的两条线与拐点。** [chapter_performance/index.md:L67-L83](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L67-L83) 说明横轴是 AI（FLOP/byte）、纵轴是可达性能，内存带宽是斜线、计算吞吐是水平线，交点即 ridge point；[chapter_performance/index.md:L85-L91](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L85-L91) 代入 B200 数值得到 2000/8 ≈ 250 FLOP/byte。中文镜像对应 [zh/chapter_performance/index.md:L65-L82](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/zh/chapter_performance/index.md#L65-L82)（中文术语为"拐点"）。

**（5）三分类与使用边界。** [chapter_performance/index.md:L93-L104](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L93-L104) 给出低于/高于/接近拐点三种情形的归类，并明确这只是初始分类，不能替代测量与剖析。

**（6）脚本中的三个常量与屋顶曲线。** [img/scripts/gen_roofline.py:L14-L19](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_roofline.py#L14-L19) 是整张图的数值核心：

```python
PEAK_TFLOPS = 2000.0      # ~2 PFLOP/s dense fp16 tensor core (order of magnitude)
BW_TB_S = 8.0             # HBM3e, TB/s  (==> attainable = 8 * AI  TFLOP/s)
RIDGE = PEAK_TFLOPS / BW_TB_S   # ~281 FLOP/byte

ai = np.logspace(-1, 4.3, 500)            # arithmetic intensity, FLOP/byte
roof = np.minimum(PEAK_TFLOPS, BW_TB_S * ai)
```

`np.minimum` 一行就是 roofline 不等式的向量化实现：对每个 AI 取「水平线」与「斜线」的较低者。注意 `BW_TB_S` 行内注释里的单位说明——`8 * AI` 直接得到 TFLOP/s，正是 4.1.2 提到的单位相消技巧。另外，`RIDGE` 行内注释写着 "~281"，但实际计算 \(2000/8=250\)；文件头部 docstring（[img/scripts/gen_roofline.py:L1-L8](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_roofline.py#L1-L8)）和正文都写 ~250，图中渲染的也是 250——这是一处过时的行内注释，以计算值为准（见 4.1.5 练习 2）。

**（7）拐点与两条屋顶线的绘制。** [img/scripts/gen_roofline.py:L21-L27](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_roofline.py#L21-L27) 画出屋顶折线、`axhline` 水平虚线（计算屋顶）、`axvline` 竖直点线（拐点位置），并用 `f'ridge ≈ {RIDGE:.0f} FLOP/byte'` 把数值写上图——图上的 250 就来自这里。

**（8）三个示例工作负载点。** [img/scripts/gen_roofline.py:L29-L44](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_roofline.py#L29-L44)：

```python
pts = [
    ('Elementwise / RMSNorm\n(memory-bound)', 0.4, 8 * 0.4 * 0.7, ...),
    ('GEMM 4096³ — naive\n(leaves the SM idle)', 1365, 2.9, ...),
    ('GEMM 4096³ — SOTA\n(~⅔ of peak)', 1365, 1320, ...),
]
```

三个点的读法：

| 点 | AI (FLOP/byte) | 达到性能 (TFLOP/s) | 含义 |
| --- | --- | --- | --- |
| Elementwise / RMSNorm | 0.4 | \(8\times0.4\times0.7=2.24\) | 深处 memory-bound 区；0.7 因子模拟只达到七成峰值带宽的现实 |
| GEMM 4096³ naive | 1365 | 2.9 | AI 已在 compute-bound 区，但实现差，离屋顶差 3 个数量级 |
| GEMM 4096³ SOTA | 1365 | 1320 | 同样的 AI，接近 2 PFLOP/s 屋顶（约 2/3 峰值） |

最后一个关键观察：**naive 与 SOTA 的 AI 相同（1365），达到的性能却差 450 倍**。roofline 只给上限，不保证达到——[img/scripts/gen_roofline.py:L42-L44](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_roofline.py#L42-L44) 的绿色箭头（"optimization climbs here"）画的就是这段由实现质量决定的爬坡，它是 GEMM 九步优化（单元十一~十三）要走的路。

#### 4.1.4 代码实践：亲手重建 roofline 图

1. **实践目标**：验证整张 roofline 图（两条屋顶、拐点 250、三个示例点）确实能由约 20 行核心代码确定性地复现，把"模型"变成"可运行的模型"。

2. **操作步骤**：
   - 确认依赖：`matplotlib`、`numpy`（见 [img/scripts/README.md:L23](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/README.md#L23)）。
   - 按 [img/scripts/README.md:L3-L5](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/README.md#L3-L5) 的约定，**必须先进入脚本目录**再运行（脚本用相对路径写出到 `../roofline.png`）：

     ```bash
     cd img/scripts
     python gen_roofline.py
     ```

   - 用图片查看器打开 `img/roofline.png`。

3. **需要观察的现象**：控制台输出什么；图中拐点竖线旁标注的数值是多少；三个彩色点分别落在屋顶线的什么位置；绿色箭头从哪里指向哪里。

4. **预期结果**（依据源码逐行推演）：
   - 控制台打印 `Saved roofline.png`（[img/scripts/gen_roofline.py:L53](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_roofline.py#L53)）。
   - 图中拐点标注为 **250**（L25 的 f-string 计算 \(2000/8\)），而不是行内注释里的 281。
   - 红点在左下（低 AI、贴着斜线下方），橙点与绿点横坐标相同（1365）、纵坐标相差约 450 倍，绿点贴近水平屋顶线。
   - 脚本会原位覆盖仓库中已提交的 `img/roofline.png`（内容为确定性重建）；如需还原可执行 `git checkout -- img/roofline.png`。

5. 图像的视觉细节（字体渲染、dpi 效果）取决于本地 matplotlib 版本——**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果改用「假设带宽为 16 TB/s 的某条内存路径」做屋顶分析，拐点是多少？这说明屋顶分析必须声明什么？

**答案**：\(2000/16 = 125\) FLOP/byte。带宽翻倍使拐点左移一半——同一内核可能从 compute-bound 变成 memory-bound。所以屋顶分析必须**声明内存层级与带宽取值**（正文 [chapter_performance/index.md:L27-L31](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L27-L31)、[L65](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L65)）。16 TB/s 只是演示换算的假设值，不是任何真实层级的规格。

**练习 2**：[img/scripts/gen_roofline.py:L16](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_roofline.py#L16) 的行内注释写 "~281 FLOP/byte"，图上实际渲染的数值是多少？哪个对？为什么？

**答案**：图上是 250。\( \text{TFLOP/s} \div (\text{TB/s}) = (10^{12}\ \text{FLOP/s}) \div (10^{12}\ \text{byte/s}) = \text{FLOP/byte} \)，所以 \(2000/8 = 250\)。docstring（L7）与正文（[chapter_performance/index.md:L85-L91](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L85-L91)）也都是 ~250，行内注释是过时笔误。教训：读任何性能脚本，先自己做一遍单位自检。

**练习 3**：为什么"某内核测得 330 TFLOP/s"这个数字本身说明不了快慢？

**答案**：性能只有相对天花板才有意义（[chapter_performance/index.md:L12](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L12)）。在约 2000 TFLOP/s 的 B200 fp16 Tensor Core 屋顶下，330 只占约 17%，说明大部分算力在闲置，还有约 6 倍空间；而在一条低矮得多的屋顶下，330 可能已经接近极限。

### 4.2 算术强度：横轴上的坐标

#### 4.2.1 概念说明

算术强度（arithmetic intensity，AI）是 roofline 图的横轴，定义为：

\[
\text{arithmetic intensity} = \frac{\text{compute work}}{\text{data moved}}
\]

即**每搬运一个字节支撑多少次浮点运算**，单位 FLOP/byte。它由算法的数据复用方式决定，因此**在写内核之前就能预估**——这是 roofline 模型实用性的来源：不用先实现，就能预判瓶颈在哪一侧。

关于 AI 的三个口径约定（都来自正文）：

1. **分子是数学运算量**，不是指令数：乘/加各计 1 FLOP，FMA 计 2 FLOP；访存、同步等指令不计入。
2. **分母必须绑定内存层级**：HBM roofline 用 HBM 字节数，L2 roofline 用 L2 字节数，SMEM roofline 用共享内存字节数。本章默认 HBM。
3. **理想化假设要写清**：例如 GEMM 的经典估算假设 A、B 各读一次、C 写一次、片上完美复用、无 padding 与元数据。真实内核通常搬得更多。

#### 4.2.2 核心流程

计算一个算子的 AI，就是完成一份"记账清单"：

```text
第 1 问（分子）: 算法需要多少次浮点运算？
    - 乘法 a*b、加法 a+b 各 1 FLOP；FMA a*b+c 计 2 FLOP
    - GEMM C = A@B（A: M×K, B: K×N）的答案: 2*M*N*K
第 2 问（分母）: 对哪个内存层级产生多少字节？
    - 默认 HBM：统计读入 + 写出的字节数（dtype 字节数 × 元素数）
第 3 问（口径）: 是否理想化？
    - 中间张量是否落盘？输出是否回读（beta≠0）？有无 scale factor 等元数据？
AI = 分子 / 分母
```

对 GEMM，正文的记账结果是 [chapter_performance/index.md:L58-L63](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L58-L63)：计算量 \(2 \times M \times N \times K\)。直觉：每个输出元素 \(C_{ij}\) 需要 K 次乘法与 K 次加法（2K FLOP），共 M×N 个输出，即 \(2MNK\)——每一步 K 循环里的乘加正好对应一条 FMA（2 FLOP）。

#### 4.2.3 源码精读

**（1）AI 的定义与 FLOP 约定。** [chapter_performance/index.md:L46-L56](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L46-L56) 给出 \( \text{AI} = \text{compute work}/\text{data moved} \)，并明确分子是数学运算量（FLOPs）而非指令总数，乘/加 1 FLOP、FMA 2 FLOP。

**（2）GEMM 计算量。** [chapter_performance/index.md:L58-L63](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L58-L63) 写出 \(2 \times M \times N \times K\)。

**（3）分母绑定层级。** [chapter_performance/index.md:L65](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L65) 一句话点明：HBM roofline 数 HBM 字节，L2 roofline 数 L2 字节，SMEM roofline 数 SMEM 字节。这承接 u2-l2 的多级存储体系——同一内核可以同时画在几张不同层级的 roofline 图上，分别诊断不同层级的压力。

#### 4.2.4 代码实践：用脚本验证 2MNK 与 N/3

1. **实践目标**：把 4.2.2 的记账清单代码化，验证 GEMM 计算量公式与（下一模块要用的）方阵理想 AI 公式 \(N/3\) 的数值。

2. **操作步骤**：新建一个独立脚本（例如放在仓库外的临时目录，或 `img/scripts/` 下的未跟踪新文件），运行以下**示例代码**：

   ```python
   # 示例代码：验证 GEMM 的 FLOP 记账与理想算术强度
   def gemm_flops(M, N, K):
       # 每个输出元素 2K FLOP（K 次乘 + K 次加，即 K 条 FMA），共 M*N 个输出
       return 2 * M * N * K

   def gemm_hbm_bytes_square(N, s=2):
       # 理想口径: A 读一次 + B 读一次 + C 写一次（beta=0，不回读旧 C）
       return 3 * s * N * N

   M = N = K = 4096
   flops = gemm_flops(M, N, K)
   bytes_ = gemm_hbm_bytes_square(N, s=2)      # fp16, s=2 字节
   print(f"FLOP   = {flops:,}")                  # 计算量
   print(f"bytes = {bytes_:,}")
   print(f"AI     = {flops / bytes_:.2f} FLOP/byte (= N/3 = {N/3:.2f})")
   print(f"理论最短耗时 @2000 TFLOP/s = {flops / 2000e12 * 1e6:.1f} us")
   ```

3. **需要观察的现象**：AI 是否恰好等于 N/3；在 2000 TFLOP/s 屋顶下理论最短耗时是多少微秒量级。

4. **预期结果**（手算可复核）：
   - `FLOP = 137,438,953,472`（约 137.4 GFLOP）；
   - `AI = 1365.33 FLOP/byte`，即 \(4096/3\)——这正是 gen_roofline.py 中 GEMM 示例点横坐标 1365 的来历（[img/scripts/gen_roofline.py:L34-L35](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_roofline.py#L34-L35)）；
   - `理论最短耗时 ≈ 68.7 us`（\(137.4\ \text{GFLOP} \div 2000\ \text{TFLOP/s}\)）。对照 4.1.3 表格：naive 实现只有 2.9 TFLOP/s，同样计算量要花约 47 ms——理论与实践的差距就是后面九章 GEMM 优化要填的坑。
   - 实际打印格式因 Python 版本略有差异——**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：一条 FMA 指令 `a * b + c` 计几个 FLOP？为什么"内核指令数"和"FLOP 数"是两个不同的量？

**答案**：2 FLOP（一次乘 + 一次加）。指令数统计的是处理器实际执行的指令（含访存、同步、地址计算等零 FLOP 指令），FLOP 数统计算法的数学运算量；一条 Tensor Core 指令又可能一次完成成百上千次乘加。AI 的分子永远用后者（[chapter_performance/index.md:L53-L56](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L53-L56)）。

**练习 2**：同一个内核的"HBM roofline"和"SMEM roofline"为什么会给出不同的 AI 与不同的屋顶？

**答案**：分母不同。HBM roofline 统计内核对显存的读写，SMEM roofline 统计对共享内存的读写；分子（FLOP）不变，AI 就不同，且两层带宽也不同（[chapter_performance/index.md:L65](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L65)）。分析片上复用（如 tile 在 SMEM 里被 MMA 反复读）时用 SMEM roofline 更合适。

**练习 3**：计算 `M=1024, N=2048, K=512` 的 GEMM 计算量。

**答案**：\(2 \times 1024 \times 2048 \times 512 = 2{,}147{,}483{,}648\) FLOP ≈ 2.15 GFLOP。

### 4.3 常见算子强度计算：四类算子在图上的位置

#### 4.3.1 概念说明

正文的分类直觉（[chapter_performance/index.md:L108-L111](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L108-L111)）：AI 首先取决于算法如何复用数据，所以**写内核之前**就能预判瓶颈。四类算子在谱系上的位置：

- **Elementwise / reduction**（GELU、RMSNorm 等）：读写大张量、每元素计算很少 → AI 很低，在拐点左侧深处，memory-bound。
- **GEMM**：每个加载的 tile 可被大量乘加复用 → AI 随问题规模增长（方阵约 \(N/3\)），大 GEMM 落在 compute-bound 区。
- **Attention**：介于两者之间，AI 取决于序列长度、head 维度、分块、掩码，以及**中间张量是否落盘**——标准 attention 要把 \(QK^T\) 的 score 矩阵写回 HBM 再读回来，这笔往返主导了流量；Flash Attention（含 FA4）把相关 tile 留在片上、避开往返，从而抬高 AI。

#### 4.3.2 核心流程

逐个推导（全部采用 4.2 的理想化口径：fp16 \(s=2\) 字节、只计 HBM 流量、假设完美片上复用；**以下公式中前两组与 tile 公式来自正文，attention 的两个估值为本讲依同一口径推出的示例推导**，用于练习而非规格）：

**(a) Elementwise**（\(y = f(x)\)，每元素 \(f\) 次 FLOP，读写各一遍）：

\[
\text{AI} \approx \frac{f \cdot n}{2 \cdot s \cdot n} = \frac{f}{2s} \xrightarrow{s=2} \frac{f}{4}\ \text{FLOP/byte}
\]

取 \(f = 4\)（一个带少量算术的激活函数的数量级）得 AI ≈ 1。图中红点的 0.4（[img/scripts/gen_roofline.py:L33](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_roofline.py#L33)）就是这一量级的示例锚点（相当于 \(f \approx 1.6\)，且实际只达到七成带宽）。

**(b) Reduction**（对 \(n\) 个 fp16 元素求和，只读大张量、写出标量可忽略）：

\[
\text{AI} \approx \frac{n}{2sn} = \frac{1}{2s} \xrightarrow{s=2} 0.25\ \text{FLOP/byte}
\]

RMSNorm 这类"读一行 + 写一行"的变体：每元素约 \(f\approx 4\) 次 FLOP（平方和、开方、缩放），AI ≈ \(4/(2\times2) = 1\)。无论按哪种口径，都在拐点左侧两个数量级以上——**分类对精确值稳健**。

**(c) GEMM 方阵** \(M=N=K\)（正文公式，[chapter_performance/index.md:L124-L129](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L124-L129)）：分子 \(2N^3\)，分母 \(3 \cdot 2N^2\)（A、B 各读一次 + C 写一次）：

\[
\text{AI} \approx \frac{2N^3}{3 \cdot 2N^2} = \frac{N}{3}\ \text{FLOP/byte}
\]

**tile 视角**（正文公式，[chapter_performance/index.md:L171-L190](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L171-L190)）：一个 CTA 算 \(B_M \times B_N\) 的 C tile，每个 K-stage 载入 \(B_M \times B_K\) 的 A 与 \(B_K \times B_N\) 的 B，每元素 \(s\) 字节，**只计 A/B 流量、忽略 C 的读写**（注意与 \(N/3\) 公式的记账口径不同）：

\[
\text{AI} \approx \frac{2 \times B_M \times B_N \times B_K}{s \times (B_M \times B_K + B_K \times B_N)} = \frac{2 \times B_M \times B_N}{s \times (B_M + B_N)}
\]

当 \(B_M = B_N = B\) 时退化为极简的：

\[
\text{AI} \approx \frac{B}{s}
\]

正文的具体数值（[chapter_performance/index.md:L192-L228](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L192-L228)）：\(B_K=64\)、fp16（\(s=2\)）下，\(16\times16\) tile 的一个 stage：工作量 \(2\times16\times16\times64=32768\) FLOP、流量 \(2\times(16\times64+64\times16)=4096\) 字节，AI = 8 FLOP/byte；tile 长到 \(64\times64\)：\(524288/16384 = 32\) FLOP/byte。**tile 越大，每个从全局内存载入的 A/B 元素能在片上参与越多乘加**——这就是分块（tiling）提高 AI 的机理。

**(d) Attention**（序列长 \(L\)、head 维 \(d\)，两个 matmul \(S=QK^T\) 与 \(O=PV\)，分子 \(4L^2 d\)；softmax 的指数运算先不计）：

- 标准（naive）实现，\(S\) 与 \(P=\text{softmax}(S)\) 都落盘往返：字节 ≈ \(s\,(4Ld + 4L^2)\)，当 \(L \gg d\) 时流量被 \(4L^2\) 主导：

\[
\text{AI}_{\text{naive}} \approx \frac{4L^2 d}{4sL^2} \approx \frac{d}{2s} \xrightarrow{s=2,\ d=128} 64\ \text{FLOP/byte 量级}
\]

- Flash Attention，只搬 Q/K/V 入片、O 出片：字节 ≈ \(s \cdot 4Ld\)：

\[
\text{AI}_{\text{flash}} \approx \frac{4L^2 d}{4sLd} = \frac{L}{2s} \xrightarrow{s=2,\ L=4096} 1024\ \text{FLOP/byte 量级}
\]

结论：naive attention 的 AI 由 head 维（小、固定）决定，flash 的 AI 由序列长（大、可增长）决定；**同一个数学问题，仅"中间张量是否落盘"一项就足以让 AI 跨过 250 的拐点**——这正是正文 [chapter_performance/index.md:L137-L148](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L137-L148) 说的「Flash Attention 把相关 tile 留在片上、避免往返、抬高算术强度」的定量版本，也是第 14 单元 FA4 的动机。（注意上述估值为理想口径：单次读写、无元数据、忽略 softmax——真实内核搬得更多，见正文的假设清单 [chapter_performance/index.md:L131-L135](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L131-L135)。）

#### 4.3.3 源码精读

**（1）elementwise / reduction。** [chapter_performance/index.md:L113-L118](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L113-L118)：GELU、RMSNorm 这类内核读写大张量而每元素计算少，AI 低，位于拐点左侧，性能主要受内存带宽限制。

**（2）GEMM 的 \(N/3\) 与假设清单。** [chapter_performance/index.md:L120-L135](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L120-L135)：AI 随问题规模增长；方阵估算 \(\frac{2N^3}{3\cdot 2N^2} = \frac{N}{3}\)，假设 A、B 各读一次、C 写一次、`beta = 0`、片上完美复用、无额外元数据（元数据指低精度格式的 scale 等辅助值）；真实内核流量更大，但趋势不变。

**（3）attention。** [chapter_performance/index.md:L137-L148](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L137-L148)：AI 取决于序列长度、head 维、tiling、masking 与中间张量是否物化；\(QK^T\) 的 score 矩阵写回再读回是主要流量成本；Flash Attention（含 FA4）把相关 tile 留在片上避免往返；attention 优化因此在算法层（减 HBM 流量、抬 AI）与实现层（调度重叠）两个层面展开。

**（4）tile 级 AI 公式。** [chapter_performance/index.md:L171-L190](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L171-L190) 给出 4.3.2(c) 的分块公式，并明确该口径**忽略 C 的读写**、只计 A/B 全局内存流量；[chapter_performance/index.md:L192-L228](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L192-L228) 用 16×16（AI=8）对 64×64（AI=32）的数值例子说明大 tile 的复用收益；[chapter_performance/index.md:L225-L228](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L225-L228) 总结机理：一个从全局内存载入的 A/B 元素能在 tile 内部服务更多乘加。

**（5）脚本中示例点坐标的来历。** [img/scripts/gen_roofline.py:L32-L36](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_roofline.py#L32-L36) 三个点的横坐标 0.4 与 1365 分别对应 4.3.2(a) 的量级和 \(4096/3 \approx 1365\)（与 4.2.4 实践的输出互相印证）。

#### 4.3.4 代码实践：四算子 AI 计算器并标到 roofline 图上（本讲主实践）

1. **实践目标**：实现一个可复用的算术强度计算器，覆盖 elementwise、reduction、GEMM、attention 四类算子；把算出的 AI 连同各自适用的性能上限标到 roofline 图上，得到一张"我的算子分布图"。

2. **操作步骤**：

   **第一步**，编写计算器（**示例代码**，口径与 4.3.2 一致）：

   ```python
   # 示例代码：ai_calculator.py —— 四类算子的理想算术强度
   PEAK_TFLOPS = 2000.0   # B200 稠密 fp16 Tensor Core 峰值（书中取整值）
   BW_TB_S = 8.0          # HBM3e 带宽（书中取整值）
   RIDGE = PEAK_TFLOPS / BW_TB_S   # ≈ 250 FLOP/byte

   def roof(ai):
       """返回 (性能上限 TFLOP/s, 受限资源)"""
       mem_roof = BW_TB_S * ai              # TB/s * FLOP/byte = TFLOP/s
       return min(PEAK_TFLOPS, mem_roof), ("compute" if mem_roof > PEAK_TFLOPS else "memory")

   def ai_elementwise(flops_per_elem, s=2):
       return flops_per_elem / (2 * s)      # 读一遍 + 写一遍

   def ai_reduction(s=2):
       return 1.0 / (2 * s)                 # 只读大张量，写出量可忽略

   def ai_gemm_square(N, s=2):
       return (2 * N**3) / (3 * s * N**2)   # = N/3

   def ai_attention(L, d, s=2, flash=False):
       flops = 4 * L * L * d                       # QK^T 与 PV 两个 matmul
       if flash:
           bytes_ = s * (3 * L * d + L * d)        # 读 Q,K,V + 写 O
       else:
           bytes_ = s * (3 * L * d + 4 * L * L + L * d)  # 另有 S/P 各写一次、读一次
       return flops / bytes_
   ```

   **第二步**，调用并打印分类表（**示例代码**）：

   ```python
   cases = [
       ("reduction (fp16)",            ai_reduction()),
       ("elementwise f=4 (fp16)",      ai_elementwise(4)),
       ("GEMM N=512",                  ai_gemm_square(512)),
       ("GEMM N=4096",                 ai_gemm_square(4096)),
       ("attention naive L=4096 d=128", ai_attention(4096, 128)),
       ("attention flash  L=4096 d=128", ai_attention(4096, 128, flash=True)),
   ]
   for name, ai in cases:
       r, bound = roof(ai)
       print(f"{name:32s} AI={ai:9.2f}  roof={r:8.1f} TFLOP/s  ({bound}-bound)")
   ```

   **第三步**，把点标上 roofline 图。建议在 `img/scripts/` 下新建脚本（与其它脚本同目录、便于相对路径；它是未跟踪的新文件，可随时删除），**输出用新文件名，避免覆盖已提交的书图**。画法直接仿照 [img/scripts/gen_roofline.py:L18-L27](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_roofline.py#L18-L27)：

   ```python
   # 示例代码：在 roofline 上标出我的算子（放在 img/scripts/ 下运行）
   import matplotlib
   matplotlib.use('Agg')
   import matplotlib.pyplot as plt
   import numpy as np

   ai_axis = np.logspace(-1, 4.3, 500)
   roof_line = np.minimum(PEAK_TFLOPS, BW_TB_S * ai_axis)
   fig, ax = plt.subplots(figsize=(8.8, 5.0), constrained_layout=True)
   ax.plot(ai_axis, roof_line, color='#222', lw=2.2)
   ax.axvline(RIDGE, color='#888', ls=':', lw=1)
   ax.set_xscale('log'); ax.set_yscale('log')
   ax.set_xlim(0.1, 2e4); ax.set_ylim(2, 4000)
   ax.set_xlabel('Arithmetic intensity (FLOP / byte)')
   ax.set_ylabel('Attainable performance (TFLOP/s)')
   for name, ai in cases:                  # cases 来自第二步
       y = min(PEAK_TFLOPS, BW_TB_S * ai)  # 该算子适用的上限
       ax.scatter([ai], [y], s=60)
       ax.annotate(name, (ai, y), fontsize=8, textcoords='offset points', xytext=(6, 4))
   plt.savefig('../roofline_my_workloads.png', dpi=150, bbox_inches='tight')
   ```

3. **需要观察的现象**：哪些点落在拐点左侧（斜线之下）、哪些落在右侧（水平线之下）；naive 与 flash 两个 attention 点是否分居拐点两侧；GEMM N=512 与 N=4096 是否也分居两侧。

4. **预期结果**（手算可复核，fp16、\(s=2\)）：

   | 算子 | AI (FLOP/byte) | 适用屋顶 (TFLOP/s) | 分类 |
   | --- | --- | --- | --- |
   | reduction | 0.25 | 2.0 | memory |
   | elementwise \(f=4\) | 1.0 | 8.0 | memory |
   | GEMM N=512 | 170.7 | 1365.3 | memory（理想模型下！） |
   | GEMM N=4096 | 1365.3 | 2000 | compute |
   | attention naive | ≈62.1 | ≈496.5 | memory |
   | attention flash | 2048.0 | 2000 | compute |

   两个值得盯住的现象：其一，**小 GEMM 在理想模型下也是 memory-bound**（\(8 \times 170.7 < 2000\)），只有 N 超过约 750 后才转入 compute-bound 区；其二，attention 仅因"中间张量不落盘"一项就从 62 跳到 2048，跨过拐点 250。表格数值为公式推演结果，实际运行输出待本地验证。

5. 若本地没有 matplotlib，前两步（纯计算与分类表）仍可独立完成——核心是计算器，不是图。

#### 4.3.5 小练习与答案

**练习 1**：用 \(N/3\) 估算 \(M=N=K=8192\) 方阵 GEMM 的理想 AI，并判断分类。

**答案**：\(8192/3 \approx 2731\) FLOP/byte，远大于拐点 250；适用屋顶为 \(\min(2000, 8\times2731) = 2000\) TFLOP/s，compute-bound（理想模型下）。

**练习 2**：\(B_K=64\)、fp16（\(s=2\)）下 16×16 tile 的 AI 是 8 FLOP/byte。若 dtype 换成 fp8（\(s=1\)），16×16 与 64×64 tile 的 AI 各变成多少？

**答案**：用 \(B/s\)（[chapter_performance/index.md:L186-L190](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L186-L190)）：16×16 fp8 → \(16/1 = 16\)；64×64 fp8 → \(64/1 = 64\) FLOP/byte。dtype 字节数减半使 AI 翻倍——这就是正文"更小的 dtype 提高 AI"（[chapter_performance/index.md:L230-L235](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L230-L235)）的定量含义；若低精度格式需要 scale factor 等元数据，实际增益会打折扣。

**练习 3**：为什么 naive attention 的 AI 近似与 head 维 \(d\) 相关（\(\approx d/2s\)），而 flash attention 的理想 AI 与序列长 \(L\) 相关（\(\approx L/2s\)）？

**答案**：分子同为 \(4L^2 d\)，差别全在分母。naive 的流量被 \(S/P\) 矩阵的落盘往返主导（\(\approx 4sL^2\)），\(L^2\) 与分子中的 \(L^2\) 相消，剩下 \(d\)；flash 只搬 Q/K/V/O（\(\approx 4sLd\)），消去 \(Ld\) 后剩下 \(L\)。head 维通常固定且小（如 128），序列长则可达数千——所以 flash 把 AI 从"被小常数锁死"变成"随序列长度增长"，这也是长序列场景 Flash Attention 收益更大的原因。（估算为理想口径，忽略 softmax 指数运算与元数据。）

## 5. 综合实践：一张图看懂你的算子组合

把本讲三个模块串成一个完整的调研脚本 `roofline_survey.py`（纯 Python，无 GPU 也可完成）：

1. **计算与分类**：纳入 4.3.4 的六个算子，再对 GEMM 做 \(N \in \{256, 512, 768, 1024, 2048, 4096, 8192\}\) 扫描，打印「AI、适用屋顶、分类」三列表格。
2. **找临界规模**：由扫描结果回答——理想模型下，方阵 GEMM 从哪个 \(N\) 开始跨过拐点 250？（提示：解 \(N/3 > 250\)，即 \(N > 750\)；验证 768 是否刚好越线。）
3. **变体实验**：固定 \(L=4096, d=128\)，对比 `ai_attention(flash=False)` 与 `ai_attention(flash=True)`，把两点画到图上，用文字说明"中间张量是否落盘"如何决定 attention 落在拐点的哪一侧。
4. **画图**：按 4.3.4 第三步的方法重绘 roofline 并标出全部点与拐点竖线，输出到新文件名。
5. **写结论**（各一段话）：
   - 哪些算子值得优先做**融合/复用/降字节**（AI 远低于 250 的那些——下一讲 u3-l2 的主题）？
   - 哪些算子值得投入 **Tensor Core 流水线与重叠**（AI 高于 250 的那些——GEMM 单元与 u3-l3 的主题）？
   - 图中 (1365, 2.9) 到 (1365, 1320) 这段竖直爬升（[img/scripts/gen_roofline.py:L42-L44](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_roofline.py#L42-L44) 的绿色箭头）说明 roofline 模型**不**回答什么问题？

   第 3 问参考答案：它不回答"如何接近屋顶"——AI 相同的两个实现性能可差数百倍，逼近屋顶靠的是正确的指令、布局、暂存、同步与调度（见 [chapter_performance/index.md:L253](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L253) 起的优化阶梯讨论）；前两问的参考答案分别为 reduction/elementwise/naive attention（以及小规模 GEMM）与大 GEMM、flash attention。

完成后，你就有了一张自己算出来的"算子分布图"——它是后续所有优化决策的出发点。

## 6. 本讲小结

- 内核性能上限由两条屋顶的较低者决定：\[ \text{performance} \le \min(\text{算力峰值},\ \text{带宽} \times \text{AI}) \]；B200 取整值 2 PFLOP/s 与 8 TB/s 给出拐点 \(\approx 250\) FLOP/byte。
- 算术强度 \( \text{AI} = \text{FLOP}/\text{byte} \)：分子是数学运算量（乘/加 1、FMA 2），分母必须绑定内存层级（默认 HBM）；理想化假设要写清。
- 分类与方向：AI < 拐点 → memory-bound，优化靠少搬字节（融合、复用、更小 dtype）；AI > 拐点 → compute-bound，优化靠减少计算路径空闲。roofline 是初始判断，不替代测量。
- 四类算子的谱系：reduction ≈ 0.25、elementwise ≈ 1、naive attention ≈ \(d/2s\)（约 62）、GEMM \(N/3\)（4096³ 时 1365）、flash attention ≈ \(L/2s\)（2048）——AI 跨越四个数量级，attention 的位置几乎完全由"中间张量是否落盘"决定。
- tile 级记账 \(\text{AI} \approx B/s\) 解释了分块的机理：tile 越大、dtype 越小，每个从 HBM 载入的元素能在片上服务越多乘加。
- roofline 只给上限不给实现：图中 AI 同为 1365 的 naive（2.9 TFLOP/s）与 SOTA（1320 TFLOP/s）相差约 450 倍，这段坡要靠 GEMM 九步优化（单元十一~十三）去爬。

## 7. 下一步学习建议

- **下一讲（u3-l2）内存受限内核的优化**：本讲已判出"左侧算子"该怎么想，下一讲读 [chapter_performance/index.md:L150-L246](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L150-L246) 的正文——算子融合、tiling 复用、更小 dtype、以及"抬不动 AI 时如何逼近带宽屋顶"（合并访存、TMA、保持足够在途请求）。
- **u3-l3 重叠与 occupancy**：compute-bound 内核"减少计算路径空闲"的具体手段——软件流水线、warp 特化，以及低 occupancy 与显式重叠之间的取舍。
- **实战主线**：GEMM 单元（u11–u13）是图中绿色箭头的完整展开；FA4 单元（u14）是 flash attention 那次"跨过拐点"的工程实现。
- **源码阅读建议**：读 [img/scripts/gen_gemm_perf.py](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_gemm_perf.py)，看书中 GEMM 优化路径图（[chapter_performance/index.md:L270](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L270) 引用的 `gemm_perf.png`）的数据从哪来；再浏览 [appendix/benchmarking_gpu_kernels.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/benchmarking_gpu_kernels.md)，为"测量离屋顶多远"（正文 [chapter_performance/index.md:L327-L340](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L327-L340) 的三步流程第三步）做准备。
