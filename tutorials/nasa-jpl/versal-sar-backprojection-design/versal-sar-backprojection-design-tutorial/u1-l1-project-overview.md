# 项目总览：SAR 反投影与 Versal ACAP 是什么

## 1. 本讲目标

本讲是整本学习手册的**第一篇**，不要求你已经读过任何一行本项目的代码。读完本讲，你应当能够：

1. 用自己的话说清楚**合成孔径雷达（SAR）**到底在做什么、为什么需要它。
2. 理解**聚束模式反投影（spotlight-mode backprojection）**这一图像重建技术的核心思路：为什么要对每个像素计算双程时延，再做相位对齐的相干累加。
3. 认识 **AMD Versal ACAP** 这颗芯片的「三引擎」异构架构（Scalar / Adaptable / Intelligent），并能对应到本项目里「谁负责哪件事」。
4. 明白本仓库的**实现边界**：聚束模式、GOTCHA 测试数据集、`main` 分支，以及「星载/机载在轨处理（OBP）」这一总体目标。

本讲只讲**直觉和背景**，几乎不涉及 C++/HLS 细节。后续讲义（u3 主机应用、u4–u5 AIE 内核、u6 PL 内核）才会进入真正的源码精读。

## 2. 前置知识

本讲面向零基础读者，但有几个名词先建立一个粗略印象会很有帮助：

- **雷达（Radar）**：用电磁波（这里通常是厘米波）主动照射目标，再接收回波，从而测距、成像。它自己发射能量，所以**不依赖阳光**，夜间和多数天气下都能工作。
- **相位（phase）与复数**：雷达回波是复信号，每个采样点有幅度和相位。相位是「波的振动位置」，成像时靠它判断回波是相互增强（同相）还是相互抵消（反相）。本讲会用到复数指数 \(e^{j\varphi}\)，但不会展开计算。
- **FPGA / 可编程逻辑（PL）**：一种可以通过编程重新连接内部逻辑的芯片，灵活但需要硬件思维。
- **SoC（片上系统）**：把 CPU、加速器等放在同一颗芯片里的设计。Versal ACAP 就是这种「异构 SoC」。

项目文档里反复出现的缩写，本项目在 [doc/sections/glossary.tex:L1-L14](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/glossary.tex#L1-L14) 里给出了正式定义，这里列出本讲会接触到的几个：

| 缩写 | 全称 | 本讲含义 |
|------|------|----------|
| SAR | Synthetic Aperture Radar | 合成孔径雷达 |
| OBP | On-Board Processing | 星载/机载在平台上的实时处理 |
| ACAP | Adaptive Compute Acceleration Platform | Versal 这颗芯片的正式名称 |
| AIE | AI Engine | Versal 里的「智能引擎」阵列 |
| PL | Programmable Logic | 可编程逻辑，即 FPGA 部分 |
| NoC | Network-on-Chip | 片上网络，连接芯片各部分的总线 |

## 3. 本讲源码地图

本讲只用两个「源文件」，且都是**说明性文档**而非可执行代码。这是有意为之——总览篇的任务是建立全局认识，而不是陷入实现细节。

| 文件 | 作用 | 本讲用它讲什么 |
|------|------|----------------|
| [README.md](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/README.md) | 仓库入口说明，给出项目定位、目录结构、构建与部署流程、三个分支 | 项目定位、目录职责、实现边界 |
| [doc/sections/intro.tex](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/intro.tex) | 设计文档的「引言」章节（LaTeX 源），讲 SAR 原理、反投影算法、Versal 三引擎与 OBP 目标 | SAR 背景、反投影直觉、三引擎分工 |

补充提示：完整设计文档会被编译成 PDF（`doc/versal_sar_backproject.pdf`），入口文件是 [doc/versal_sar_backproject.tex](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/versal_sar_backproject.tex)，本讲引用的 `intro.tex` 是其中的第一节。后续讲义会陆续读到 `versal_overview.tex`、`implementation.tex`、`performance_metrics.tex`、`future_work.tex` 等其他章节。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 SAR 与聚束模式反投影背景**（雷达成像与反投影算法的直觉）
- **4.2 Versal 三引擎异构架构**（Scalar / Adaptable / Intelligent 及其在本项目里的分工）
- **4.3 星载/机载在轨处理（OBP）目标**（为什么要在 Versal 上做这件事）

### 4.1 SAR 与聚束模式反投影背景

#### 4.1.1 概念说明

**SAR（合成孔径雷达）解决的问题**是：在天上（卫星或飞机）用一个尺寸现实的小天线，得到**高分辨率**的对地图像。

直觉是这样的：雷达的方位（沿飞行方向）分辨率，大致正比于「天线孔径」。要从卫星轨道上分辨地面约 10 米的目标，用单根真实天线的话，理论上需要一根**几公里长**的天线；即便从飞机上也需要几十到几百米——这种天线根本造不出来、也飞不上去。

SAR 的妙处在于：让天线随平台**沿飞行路径移动**，在许多相邻位置各打一发、收一个回波；然后在处理阶段把这些回波**合成**起来，效果就等同于用了一根「虚拟的长天线」。这样既不用造巨型天线，又获得了高方位分辨率。

这一点在项目文档里写得很清楚：

> SAR is a radar imaging modality that enables high-resolution imagery by using antenna motion over the scene and combining measurements from many nearby positions along the flight path ... the many measurements are combined so they behave as if they were taken with a much longer antenna.

详见 [doc/sections/intro.tex:L19-L31](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/intro.tex#L19-L31)。文档还强调，因为 SAR 不依赖阳光，可以**昼夜成像、且能穿透多数天气**，因此可用于对地观测、灾害评估、海冰跟踪、湿地测绘等。

**几个几何术语**（后文会反复出现）：

- **方位方向（azimuth / along-track）**：飞行方向。
- **距离方向（range / across-track）**：垂直于飞行方向。
- **斜距（slant range）**：沿雷达视线量到的距离，可投影成地面距离（ground range）。
- **视角（look angle）**：天线指向偏离星下点的角度。
- **入射角（incidence angle）**：雷达波与当地地表法线的夹角。

这些定义见 [doc/sections/intro.tex:L33-L40](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/intro.tex#L33-L40)。

**反投影（backprojection）**是把这些回波「还原成图像」的一种重建算法。它操作的对象是**相位历史数据（phase-history data）**——也就是雷达原始记录的、复数形式的回波序列。本项目聚焦的是其中一种叫**聚束模式（spotlight-mode）**的反投影；另一种叫 **stripmap 模式**，文档明确说明留给未来工作：

> The work throughout this paper focuses on spotlight-mode backprojection, an image reconstruction technique that operates on the raw complex radar measurements known as phase-history data. Stripmap-mode backprojection is left for future work.

详见 [doc/sections/intro.tex:L42-L59](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/intro.tex#L42-L59)。

#### 4.1.2 核心流程

反投影的思路可以一句话概括：**对图像里的每一个像素，去问「雷达从每个位置看这个像素的回波应该是什么样」，然后把所有脉冲对齐后相干地加起来。**

具体流程（伪代码，**示例代码**，非项目原始代码）：

```text
for 每个待成像的像素 p (x, y, z):
    I(p) = 0                          # 像素值的复数累加器
    for 每个脉冲 n (天线位置 a_n):
        1. 用平台轨迹算「天线到像素 p 的双程(往返)距离」
              r_n(p) = 2 * ||a_n - p||
        2. 把这个往返距离换算成「应取回波里的第几个距离样本」
              即在脉冲 n 的相位历史数据中定位到这个像素的回波
        3. 取出该样本 s_n(r_n(p))，并施加相位校正(让相位对齐)
        4. 累加进 I(p)
    I(p) 即为该像素的最终复亮度
```

用数学表达，核心是一次**相干累加（coherent summation）**：

\[ r_n(\vec{p}) = 2\,\|\vec{a}_n - \vec{p}\| \quad \text{(双程斜距)} \]

\[ \tau_n(\vec{p}) = \frac{r_n(\vec{p})}{c} \quad \text{(双程时延, } c \text{ 为光速)} \]

\[ I(\vec{p}) = \sum_{n=1}^{N_{\text{pulses}}} s_n\!\bigl(r_n(\vec{p})\bigr)\cdot e^{-\,j\varphi_n(\vec p)} \]

其中 \(\varphi_n(\vec p)\) 是把脉冲 \(n\) 对齐到像素 \(\vec p\) 所需的相位校正量，本项目里它正比于 \(\frac{4\pi f_c}{c}\) 乘以「差分距离」（这部分细节在讲义 u5-l4 相位校正里展开）。

**为什么这样做能成像？** 关键在「相干（coherent）」二字：当几何与时延算得准时，来自目标**真实位置**的回波，在累加时相位都对齐、相互增强；而其他位置的噪声/旁瓣则相位杂乱、相互抵消。于是真实散射点的亮度被「抬」出来，形成聚焦图像。

这也带来反投影的**最大代价**：它要为**每一个像素 × 每一个脉冲**算一次双程时延、取一次样本、做一次相位校正——计算量巨大。文档指出，正因为如此，**许多实际系统选择把相位历史数据下传到地面、在地面成像**，而不是在平台上处理：

> Consequently, many operational systems transmit phase-history data to the ground and form images there rather than on board.

这句话正是下一个模块（OBP）要反驳的动机：本项目偏要把它搬到平台上做。

#### 4.1.3 源码精读

本模块的两段核心依据都来自 `intro.tex`：

1. **SAR 的定义与动机**——为什么要「合成孔径」：
   [doc/sections/intro.tex:L19-L31](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/intro.tex#L19-L31)
   这段说明：真实天线要做大不现实 → 靠平台移动合成「虚拟长天线」→ 不依赖阳光与天气。

2. **聚束模式反投影的定义**——逐像素、逐脉冲、相位对齐、相干累加：
   [doc/sections/intro.tex:L42-L59](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/intro.tex#L42-L59)
   关键句：「For each image pixel, backprojection uses the recorded platform trajectory to compute the two-way travel time to that pixel for every pulse. Each recorded echo is aligned to that pixel by applying the computed delay ... After alignment, the echoes are summed.」——这正是 4.1.2 流程的文字版。

> 提示：本模块**没有可执行的源码**可读，因为它是算法背景。真正的反投影循环在 AIE 内核 `design/aie/backprojection.cc` 里（见讲义 u5-l3 ~ u5-l5）。本讲先建立直觉，到 u5 再对照实现。

#### 4.1.4 代码实践

**实践目标**：把 4.1.2 的流程用 NumPy 在小规模数据上「跑通一遍」，亲眼看相干累加如何把目标点「点亮」。

**操作步骤**（**示例代码**，需要本地有 Python + NumPy，**待本地验证**具体输出数值）：

1. 构造一个最简场景：一个点目标在原点 \((0,0,0)\)，一条直线的飞行轨迹上有 \(N\) 个天线位置。
2. 对每个脉冲，模拟一个相位历史回波（在目标对应的双程距离处放一个复数尖峰）。
3. 按反投影流程，对一个候选像素网格逐像素累加。

```python
# 示例代码：仅用于体会「逐像素双程时延 + 相干累加」的流程
import numpy as np

fc = 9.6e9          # 载频 (Hz), 约 X 波段
c  = 3e8            # 光速
ant_pos = np.linspace(-50, 50, 21)   # 21 个天线位置(沿方位方向), 单位 m
target  = np.array([100.0, 0.0])     # 真实点目标 (x=100m, y=0)

# 1) 仿真相位历史: 每个脉冲在「目标的双程距离」处有回波
def echo(r_two_way):
    return np.exp(-1j * 4*np.pi*fc/c * r_two_way)

# 2) 反投影: 对候选像素网格逐像素累加
grid_x = np.arange(90, 111, 1.0)
I = np.zeros_like(grid_x, dtype=complex)
for px in grid_x:
    acc = 0+0j
    for ax in ant_pos:
        a_n = np.array([ax, 0.0])
        p   = np.array([px, 0.0])
        r   = 2*np.linalg.norm(a_n - p)         # 双程斜距
        s   = echo(2*np.linalg.norm(a_n - target))  # 取该脉冲回波(这里简化为点目标)
        phi = 4*np.pi*fc/c * (r/2)              # 相位校正(对应差分距离)
        acc += s * np.exp(+1j*phi)
    I[grid_x == px] = acc
# 预期: 在 px==100(真实目标处)|I|出现明显峰值
```

**需要观察的现象**：当候选像素 \(p\) 正好落在真实目标位置（\(x=100\) m）时，各脉冲相位彼此对齐，\(|I|\) 出现明显峰值；偏离目标时，相位杂乱、\(|I|\) 明显变小。

**预期结果**：输出图像在真实目标处有一个清晰尖峰，即「相干累加点亮目标」的效果。

**如果无法运行**：明确标注「待本地验证」，并改为阅读型实践——精读 [doc/sections/intro.tex:L42-L59](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/intro.tex#L42-L59)，逐句对应到 4.1.2 流程的 4 个步骤。

#### 4.1.5 小练习与答案

**练习 1**：如果雷达只用一个静止的真实天线（不移动），还能叫 SAR 吗？为什么？

> **答案**：不能。SAR 的「合成」二字就来自「靠平台移动合成一个长虚拟孔径」。静止单天线没有孔径合成，方位分辨率受真实孔径限制，属于「真实孔径雷达（RAR）」。

**练习 2**：反投影为什么必须用**复数**回波（带相位），而不能只用幅度？

> **答案**：成像靠的是「相位对齐后相干相加」。如果丢掉相位只用幅度，各脉冲就成了非负数简单相加，无法让真实目标的回波「同相增强、别处抵消」，也就无法聚焦成高分辨率图像。相位是反投影的核心信息。

**练习 3**：文档说反投影「computationally cost」很高。请从 4.1.2 流程估算：若有 \(P\) 个像素、\(N\) 个脉冲，核心计算大约要做多少次「定位 + 相位校正 + 累加」？

> **答案**：约 \(P \times N\) 次。每像素对每脉冲都要做一次，所以总计算量与「像素数 × 脉冲数」成正比。这正是它昂贵、也正是不适合用标量 CPU 串行处理、而要搬到 AIE 并行阵列上的根本原因（见 4.2、4.3）。

---

### 4.2 Versal 三引擎异构架构

#### 4.2.1 概念说明

要把上面那个「\(P \times N\) 次重计算」在平台上实时做完，单靠 CPU 是不现实的。**AMD Versal ACAP** 是一颗**异构**芯片：它在同一片硅片上集成了三类性质截然不同的「引擎」，让开发者把不同性质的工作丢给最合适的硬件。

文档对这三类引擎的命名是：

> The Versal ACAP is a heterogeneous compute environment featuring **Scalar Engines (CPUs)**, **Adaptable Engines (FPGA/programmable logic)**, and **Intelligent Engines (AI and DSP cores)**, which allow developers to accelerate compute-heavy tasks.

详见 [doc/sections/intro.tex:L74-L91](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/intro.tex#L74-L91)。

对应到本项目，三者分工如下表：

| 引擎（文档术语） | 俗称 | 硬件实体 | 在本项目里的职责 |
|------------------|------|----------|------------------|
| **Scalar Engines** | CPU | 双核 ARM Cortex-A72 | **控制与编排**：发起聚束模式反投影任务、搬运数据、记录各阶段执行时间 |
| **Adaptable Engines** | PL（FPGA） | 可编程逻辑 | **数据重排与缓冲**：DMA 步长（stride）控制的重排序、PL 包路由器、ping-pong 缓冲 |
| **Intelligent Engines** | AIE + DSP | AI Engine 阵列 | **核心计算**：反投影的核心数学运算（差分距离、相位校正、插值累加） |

> 名词提示：本项目代码与文档里 **AIE = AI Engine**，**PL = Programmable Logic（即 FPGA 部分）**。后文一律沿用这两个缩写。

#### 4.2.2 核心流程

文档用一句话勾勒了整个数据通路：

> We design and integrate a data path from the **dual Cortex-A72 processors**, through the **network-on-chip**, to the **AI Engines**, with ping-pong buffering, DMA stride-controlled reordering to feed the AI Engine and PL kernels efficiently. The **FPGA fabric** hosts the DMA reordering logic, and the **AI Engines** perform the core backprojection computations. ... Upon completion, the AI Engines stream the image through the **PL DMA reordering kernel** to assemble a contiguous image in DDR.

把它画成一条流水线（文字版框图）：

```text
[ARM Cortex-A72 (Scalar)]                ← 编排：发起任务、记录耗时
        │  通过 NoC(片上网络) 搬运数据
        ▼
   DDR (片外内存)  ←→  NoC  ←→  [PL (Adaptable)]   ← DMA 重排 / ping-pong 缓冲 / 包路由
                                       │  AXI-Stream 流
                                       ▼
                              [AI Engine 阵列 (Intelligent)]  ← 反投影核心计算
                                       │  结果以流形式回写
                                       ▼
                          [PL DMA 重排内核]  → 把乱序结果拼成连续图像 → DDR
```

要点：

1. **数据进**：ARM 把相位历史数据与目标像素送进 DDR，经 NoC 流向 PL/AIE。
2. **核心算**：AIE 阵列承担反投影的「逐像素 × 逐脉冲」重计算（4.1.2 流程）。
3. **数据出**：AIE 算完后，结果以**流**的形式经 **PL DMA 重排内核**重新排序，在 DDR 里拼成一张连续的图像。
4. **谁管谁**：ARM 是「指挥」，PL 是「物流与分拣」，AIE 是「干活的工厂」。

为什么这样分工？因为这三类硬件各有所长：CPU 擅长控制流但不擅长海量数值计算；FPGA 擅长定制的、流式的数据搬运与重排；AIE 阵列（本质是大量 VLIW+SIMD 的小处理器核，见讲义 u2）擅长高密度向量数学。把反投影的数学丢给 AIE，正好扬长避短。

#### 4.2.3 源码精读

本模块依据集中在一段，它同时回答了「三引擎分别干什么」和「数据怎么流」（与 4.3 的 OBP 目标也强相关）：

- **三引擎分工与端到端数据通路**：
  [doc/sections/intro.tex:L74-L91](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/intro.tex#L74-L91)

补充：4.2.2 框图里提到的 **NoC（片上网络）**、**AXI-Stream 流**、**ping-pong 缓冲**、**DDR bank** 等概念，在 AIE 编程模型里都有正式定义，讲义 u2-l1、u2-l2 会专门讲。本讲只需知道「NoC 是芯片内部连接各域的高速总线」即可。

#### 4.2.4 代码实践

**实践目标**：把 4.2.1 的分工表和 4.2.2 的数据通路，与**仓库目录结构**对应起来，验证「每个引擎都有对应的源码目录」。

**操作步骤**：

1. 打开 [README.md:L14-L29](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/README.md#L14-L29) 的目录表。
2. 找到这三个目录，并写出它们对应哪个引擎：
   - `design/aie` → ？（答：Intelligent Engines / AIE）
   - `design/pl` → ？（答：Adaptable Engines / PL）
   - `design/host` → ？（答：Scalar Engines / ARM Cortex-A72）
3. 再找出三个「不属于单一引擎、而是系统级」的目录：
   - `design/common.h`（跨三域共享的配置）
   - `design/system_cfgs`（系统级连接信息）
   - `design/profiling_cfgs`（性能采集配置）

**需要观察的现象**：三引擎在本仓库里有**一一对应的源码目录**，说明「异构三域」不只是文档说法，而是真实落在代码组织上的。

**预期结果**：得到一张「目录 ↔ 引擎」对照表（本讲末尾的综合实践会把它和数据通路画在一起）。

#### 4.2.5 小练习与答案

**练习 1**：本项目里「记录各阶段执行时间」的工作交给哪个引擎？为什么不是 AIE？

> **答案**：交给 Scalar Engine（ARM Cortex-A72）。计时与任务编排属于「控制流」工作，需要操作系统、文件 I/O、定时器等，这正是 CPU 擅长的；AIE 是向量计算核，没有这些能力，也不该被计时等杂务打断。

**练习 2**：AIE 算完的图像为什么要再过一遍「PL DMA 重排内核」才写进 DDR？

> **答案**：因为 AIE 阵列里多个内核并行输出，结果到达 PL 时是**乱序**的（由包交换/调度决定）。PL 重排内核根据包头里的标识，把乱序数据重排成**连续的**图像存入 DDR，供主机读取。这是讲义 u6（PL 包路由器）的核心问题。

**练习 3**：如果有人问「为什么不用纯 FPGA 来做反投影，省掉 AIE？」你会怎么用 4.1.2 的计算量来回答？

> **答案**：反投影是海量、规则的向量数学（\(P\times N\) 次差分距离、相位校正、插值累加），AIE 阵列（数百个 VLIW+SIMD 核，见 u2）在单位面积/功耗下的向量吞吐远高于通用 FPGA 逻辑。纯 FPGA 实现同样的浮点向量算力会占用大量逻辑资源、功耗更高。所以把数学放 AIE、把数据搬运放 PL，是最优分工。

---

### 4.3 星载/机载在轨处理（OBP）目标

#### 4.3.1 概念说明

**OBP = On-Board Processing（在平台上处理）**，指把数据处理放在**卫星或飞机平台上**（on board），而不是把原始数据下传到地面再处理。

4.1.3 已经提到：传统做法是「把相位历史数据下传到地面、在地面成像」。本项目的目标恰恰相反——文档开宗明义：

> This paper develops an **OBP implementation** of backprojection on a Versal ACAP.

详见 [doc/sections/intro.tex:L74-L91](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/intro.tex#L74-L91)。

**为什么要在平台上做（OBP 的价值）？**

1. **省下行带宽**：原始相位历史数据体量巨大，下行链路是星载系统的稀缺资源；在平台上直接出图像，下行的就是小得多的图像。
2. **低延迟**：灾害评估等场景需要「立刻」拿到结果，地面处理的下行+排队延迟不可接受。
3. **自主性**：平台可自主决策（比如发现目标后立即调整观测）。

**为什么 Versal 适合做 OBP？** 因为它把 4.2 的三引擎集成在**一颗**低功耗芯片里——既能跑控制（ARM），又能做高速数据重排（PL），还能做密集向量计算（AIE），且功耗/体积/重量适合星载/机载环境。文档把本项目的贡献定位为：在这类器件上**刻画反投影的性能与功耗权衡空间**：

> We report execution times and representative power measurements at selected AI Engine core counts and illustrate how the design scales with additional cores. These results characterize the performance and power trade space for on-board backprojection on this class of device.

也就是说，本项目不只是「跑通」，而是要回答：「在 Versal 这类器件上做星载反投影，用多少个 AIE 核、付出多少功耗、能换来多快的执行时间？」这些性能与功耗数字在讲义 u8（度量）里展开。

#### 4.3.2 核心流程

从「项目目标」的角度，本仓库的最终用途可以概括成一条链：

```text
目标: 在 Versal 平台上(on-board) 把相位历史数据实时重建为 SAR 图像
   │
   ├── 算法侧: 聚束模式反投影 (spotlight-mode backprojection)
   ├── 硬件侧: ARM(编排) + PL(重排/缓冲) + AIE(核心计算)
   ├── 数据侧: GOTCHA 数据集做测试输入
   └── 评测侧: 在不同 AIE 核数下测量 执行时间 + 功耗
```

其中「GOTCHA 数据集」是本项目的**测试输入**：仓库 `design/test_data` 目录下的 CSV 文件（如 `gotcha_slowtime_*_pass1_360deg_HH.csv`、`gotcha_phdata_*_pass1_360deg_HH.csv`）就是 GOTCHA 采集的真实相位历史数据，用来验证反投影实现是否正确。GOTCHA 是 AFRL 的公开机载 SAR 数据采集实验，360° 全方位、HH 极化。这些数据通过 Git LFS 管理（讲义 u1-l2 会讲拉取流程）。

> 边界提醒：本项目**只验证聚束模式**（stripmap 留给未来工作，见 future_work.tex），测试数据用 **GOTCHA**，本讲义基于 **`main` 分支**。README 还提到另外两个分支：

#### 4.3.3 源码精读

- **OBP 总目标与 Versal 三引擎实现**：
  [doc/sections/intro.tex:L74-L91](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/intro.tex#L74-L91)
  这一段同时是 4.2 和 4.3 的核心依据——它既讲了三引擎分工，也讲了「为什么要在 Versal 上做 OBP」与「要测什么」。

- **三个分支的边界**：
  [README.md:L265-L273](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/README.md#L265-L273)
  README 列出 `main`、`host_stride`、`pl_stride` 三个分支，并说明它们的差异在配套 PDF 文档里详述。**本讲义（及本手册默认）基于 `main` 分支**；三分支的职责差异在讲义 u7-l2 专门对比。

- **项目一句话定位**：
  [README.md:L10-L12](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/README.md#L10-L12)
  「This repo contains the source code for implementing backprojection onto the AMD Versal ACAP」——一句话说清了仓库是什么。

#### 4.3.4 代码实践

**实践目标**：动手确认「GOTCHA 测试数据」确实存在于仓库中，并理解它就是反投影的输入。

**操作步骤**：

1. 列出 `design/test_data` 目录（README 目录表里写它是 "Test data for slowtime and range compression samples"，见 [README.md:L14-L29](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/README.md#L14-L29)）。
2. 观察文件名里的关键词：`gotcha`（数据来源）、`slowtime`（慢时间/方位时间）、`phdata`（phase-history data，相位历史数据）、`pass1_360deg`（第一航迹、360° 全方位）、`HH`（极化方式）。
3. 把这两个文件分别对应到 4.1.2 流程里的角色：
   - `slowtime` CSV → 提供天线方位角随时间变化（平台轨迹）
   - `phdata` CSV → 提供复数相位历史回波（即流程里的 \(s_n\)）

**需要观察的现象**：测试数据是**两类 CSV**，一类描述「天线在哪/朝哪」（轨迹），一类是「收到的复回波」（相位历史）。这正好对应反投影流程需要的两类输入。

**预期结果**：能用自己的话说出「GOTCHA slowtime CSV 给轨迹、phdata CSV 给回波，二者一起喂给反投影算法」。

**注意**：这些 CSV 由 Git LFS 管理，未 `git lfs pull` 时只是指针文件。实际下载流程在讲义 u1-l2。

#### 4.3.5 小练习与答案

**练习 1**：OBP 相对「地面处理」的最大好处是什么？为什么 SAR 特别需要 OBP？

> **答案**：最大好处是**省下行带宽 + 低延迟**。SAR 的原始相位历史数据体量极大，下行链路是星载瓶颈；OBP 在平台上出图像，下行量骤减，且能即时支持灾害评估等时效场景。

**练习 2**：本项目「评测」要回答的核心问题是什么？（提示：和 AIE 核数有关）

> **答案**：「在 Versal 这类器件上做星载反投影，用多少个 AIE 核、付出多少功耗，能换来多快的执行时间」——即刻画性能与功耗的权衡空间（trade space），并展示设计如何随核数扩展。

**练习 3**：本讲义基于哪个分支？另外两个分支叫什么？

> **答案**：本讲义（及本手册默认）基于 **`main`** 分支。另外两个分支是 **`host_stride`** 和 **`pl_stride`**，它们在「输入侧数据预排序由谁负责」上有差异，详见讲义 u7-l2。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个**贯穿任务**（对应大纲指定的实践任务）：

> 阅读 README 与 intro.tex 后，**用自己的话写出一段话**说明：反投影为何要对每个像素计算双程时延并做相位对齐相干累加，以及 **Versal 的哪一部分负责哪件事**。

建议按以下步骤完成：

1. **画一张总图**。在一张纸上画出：
   - 上方：一条飞行轨迹 + 一个待成像像素 \(p\) + 双程斜距 \(r_n(p)\) 的示意（来自 4.1）。
   - 下方：4.2.2 的三引擎数据通路框图（ARM → NoC → PL → AIE → PL → DDR）。
   - 把上下两层用箭头连起来：标出「逐像素逐脉冲的时延+相位+累加」这一步发生在 **AIE**。

2. **填一张三列对照表**：

   | 算法步骤（4.1.2） | 负责的 Versal 引擎 | 对应的仓库目录 |
   |-------------------|---------------------|----------------|
   | 读取相位历史数据/轨迹、发起任务、记录耗时 | Scalar / ARM | `design/host` |
   | 数据重排、ping-pong 缓冲、结果拼接 | Adaptable / PL | `design/pl` |
   | 双程时延、相位校正、插值、相干累加 | Intelligent / AIE | `design/aie` |

3. **写一段话**（约 150–250 字），要求包含：
   - 为什么必须算双程时延（定位回波）；
   - 为什么必须相位对齐再相干累加（同相增强、异相抵消 → 聚焦成像）；
   - 这三步在 Versal 上分别交给 ARM / PL / AIE，并各用一句话说明理由。

**参考答案要点**（示例，非唯一答案）：

> 反投影对每个像素都要依据平台轨迹算出每个脉冲到该像素的**双程斜距**，是为了在相位历史回波里**精确定位**属于这个像素的那份信号；只有取对了样本，后续对齐才有意义。接着必须施加**相位校正**再做**相干累加**，因为只有当来自真实目标的回波被对齐到同相位，它们才会相互增强、而别处的杂乱相位相互抵消，从而把目标「点亮」成聚焦图像。在本项目的 Versal 平台上，**ARM Cortex-A72** 负责读取数据、发起任务并记录各阶段耗时；**PL（FPGA）** 负责数据的 DMA 重排、ping-pong 缓冲以及把 AIE 乱序输出的结果拼成连续图像；**AIE 阵列** 则承担逐像素×逐脉冲的双程时延、相位校正、插值与累加这些密集向量计算。三者通过 NoC 与 AXI-Stream 流协同，共同实现星载在轨（OBP）的聚束模式 SAR 成像。

## 6. 本讲小结

- **SAR** 靠平台移动合成「虚拟长天线」，从而用现实尺寸的小天线获得高方位分辨率，且能昼夜、穿云成像。
- **聚束模式反投影** 是一种逐像素、逐脉冲的图像重建算法：对每个像素算双程时延 → 取对应回波样本 → 相位对齐 → **相干累加**，让真实目标同相增强、他处抵消，形成聚焦图像。
- 它的代价是 \(P \times N\) 量级的巨量计算，传统做法因此下传到地面处理。
- **Versal ACAP** 是「三引擎」异构芯片：**Scalar（ARM CPU）** 做控制编排、**Adaptable（PL/FPGA）** 做数据重排与缓冲、**Intelligent（AIE 阵列）** 做核心向量计算。
- 本项目把这些计算搬上平台，目标是实现 **OBP（在轨处理）**，并在不同 AIE 核数下刻画**性能与功耗**的权衡空间。
- 仓库边界：**聚束模式**（stripmap 留作未来工作）、**GOTCHA 测试数据**、本讲义基于 **`main`** 分支（另有 `host_stride`、`pl_stride`）。

## 7. 下一步学习建议

本讲建立了「算法目标 + 硬件平台 + 项目定位」的全局图景，但还没有进入任何 C++ 源码。建议按以下顺序继续：

1. **先看仓库骨架**：下一讲 **u1-l2《仓库结构与 GOTCHA 测试数据》** 会带你逐目录认识仓库，并动手 `git lfs pull` 拿到真实测试数据。
2. **再看怎么构建**：**u1-l3《构建系统与 Makefile 目标》** 讲清 `make aie/pl/host/package` 这些命令各自产出什么；**u1-l4《全局配置中心 common.h》** 讲决定设计规模的关键宏。
3. **补 AIE 前置知识**：第 2 单元（**u2-l1 ~ u2-l3**）补齐 Versal 平台与 ADF 图编程模型，这是后续读懂 AIE 源码的必备基础。
4. **最后进源码**：第 3 单元起进入主机应用（ARM）、第 4–5 单元进入 AIE 图与内核、第 6 单元进入 PL 包路由器。

> 阅读建议：本讲涉及的 `doc/sections/intro.tex` 是整份设计文档的「引言」，强烈推荐在进入下一讲前**完整通读一遍**（只有不到 100 行 LaTeX），它会让后续所有讲义的语境都更清晰。
