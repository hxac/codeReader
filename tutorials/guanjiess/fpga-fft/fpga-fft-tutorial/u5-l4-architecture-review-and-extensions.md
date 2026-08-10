# 架构反思与扩展：SDF 取舍、倒序缺失与改进方向

> 本讲是整本手册的收官篇。前面四单元你已经从「项目全貌 → 算子 → 存储时序 → 逐级解析」一路走到「验证与移植」。这一讲不再逐行读代码，而是**站远一步**，把这套 FFT 当成一个工程作品来审视：它为什么长成这样？哪些地方做完了、哪些地方还留着坑？如果你要拿去二次开发，该往哪里使劲？

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 **SDF（单路延迟反馈）流水线 FFT** 相比并行结构、MDC/MDF 结构，在「资源 / 吞吐 / 延迟」三个维度上做了怎样的取舍，并据此解释本项目为何选 SDF。
- 仅凭源码（蝶形加减与旋转因子相乘的先后顺序）判定本设计是 **DIF 还是 DIT** 流水，并推出「输出为何是 bit-reverse 倒序」的结论。
- 逐一指出项目中**已知未完成的点**：输出倒序（Reverse）未实现、`data_config` 配置端口未接线、`out_last` 未赋值、over/end 结束链未贯通。
- 针对「倒序输出、参数化点数、连续流式输入、块浮点防溢出」给出**最小可行的改进思路**。

## 2. 前置知识

本讲假定你已经读过：

- **u1-l3 ~ u1-l4**：DFT/Cooley-Tukey、DIF/DIT、bit-reverse 倒序，以及 `fft_top` 如何把 14 级串成流水线。
- **u2-l1 ~ u2-l2**：蝶形单元 `butterfly.v`（加减）与复数乘法器 `multiplier.v`（乘旋转因子）。
- **u3-l2**：`delay.v` 用双口 RAM 做 SDF 的反馈延时，每级延时 \(2^{\text{layer}-1}\) 拍。
- **u4-l3 ~ u4-l4**：`butterfly_general.v` 把单级逻辑参数化，`fft_32`~`fft_16k` 结构同构。
- **u5-l1 ~ u5-l3**：MATLAB 黄金参考、testbench 分级验证、Xilinx/Anlogic 双平台 IP 依赖。

本讲会用到的几个术语，先用一句话复习：

- **SDF（Single-path Delay Feedback，单路延迟反馈）**：数据走单条主线，每一级把「暂时用不上的那半」存进延时 RAM，等下半周期样本到达时再配对做蝶形。
- **吞吐（throughput）**：每个时钟能输出多少个有效结果。
- **延迟（latency）**：从第一个样本进入到最后一个结果输出，相隔多少个时钟。
- **bit-reverse 倒序**：把一个地址的二进制位反过来读，例如 3 位地址 `110`(6) 倒过来是 `011`(3)。DIF 流水线的输出地址天然是倒序的。

## 3. 本讲源码地图

本讲横跨多个文件做整体评估，但核心落脚点是两个：

| 文件 | 在本讲的作用 |
| --- | --- |
| `src/fft_top.v` | 全局架构的「骨架」：14 级如何串、哪些握手链通了、哪些端口是「声明了但没接」。 |
| `scheme/参数和问题.md` | 作者的设计动机笔记，点明「实时性 → 流水线 → SDF」的选型逻辑。 |
| `scheme/FFT.md` | 算法侧依据：bit-reverse 原理与计算量分析。 |
| `src/butterfly_general.v` | 单级流水线的「心脏」，用来核算资源、判定 DIF/DIT。 |
| `src/butterfly.v` / `src/fft_32.v` | 佐证「先蝶形后乘旋转因子」这一 DIF 特征的代码证据。 |
| `README.md` | 设计文档，`### Reverse` 一节直接写明倒序「还没有具体思路」。 |

## 4. 核心概念与源码讲解

### 4.1 SDF 流水线架构：为何选它，资源/吞吐/延迟的权衡

#### 4.1.1 概念说明

要在硬件上做一个 \(N\) 点 FFT，常见的流水线结构有三类，它们的「花钱方式」截然不同：

| 结构 | 含义 | 复数乘法器数量 | 蝶形单元数量 | 延时存储 | 相对特点 |
| --- | --- | --- | --- | --- | --- |
| **全并行（full parallel）** | 把整张 FFT 蝶形图铺成硬件，每一级所有蝶形同时算 | \(\frac{N}{2}\log_2 N\) | \(\frac{N}{2}\log_2 N\) | 几乎不需要 | 吞吐最大，但资源爆炸，大点数不可行 |
| **MDC / MDF（多路）** | 多条并行数据通路，延时跨路径分布 | \(\log_2 N\) × 路径数 | \(\log_2 N\) × 路径数 | 较多 | 吞吐高（每拍多路），资源中等 |
| **SDF（单路延迟反馈）** | 单条主线，每级一个蝶形、一个乘法器、一段反馈延时 | \(\log_2 N\) | \(\log_2 N\) | \(N-1\)（各级延时之和） | 资源最少，吞吐 1 样本/拍，延时约 \(N\) 拍 |

一句话总结取舍：

- **全并行**用资源换吞吐，\(N=16384\) 时需要 \(\frac{16384}{2}\times 14 \approx 11.5\) 万个复数乘法器，任何单片 FPGA 都塞不下。
- **SDF** 用「时间换面积」：只保留 \(\log_2 N\) 个运算单元，靠反馈延时把数据「攒齐了再算」，是**大点数 FFT 在资源受限 FPGA 上唯一现实的选择**。代价是吞吐被限制在每拍 1 个样本、且需要一个长长的「灌满」延迟。

#### 4.1.2 核心流程

SDF 一级的资源可以这样算（设该级对应分治层 `layer`，则半周期 \(=\) 延时深度 \(=2^{\text{layer}-1}\)）：

\[
\text{延时存储(样本数)}=\sum_{i=1}^{\log_2 N}2^{i-1}=2^{\log_2 N}-1=N-1
\]

对 \(N=16384\)（\(\log_2 N=14\)）的本项目：

- **蝶形单元数** = 14（每级一个 `butterfly`）。
- **复数乘法器数** = 13（每级一个 `multiplier`；最末级 `fft_2` 因 \(W_2^0=1\) 省去乘法器）。
- **延时存储** = \(2^{14}-1=16383\) 个复数样本（实虚各 32 位，即约 16383×64 bit ≈ 1 Mbit，落到十几块 BRAM 上）。

延迟与吞吐：

- **吞吐**：每级内部每个 PERIOD（\(=2^{\text{layer}}\)）内，前半周期灌延时、后半周期输出，整条流水线填满后**每拍输出 1 个有效结果**。
- **延迟**：第一级 `fft_16k` 要先攒满 \(N/2=8192\) 个样本才开始往第二级吐数，逐级累加，端到端延迟约 \(N\) 量级时钟。这是 SDF「先灌满再流水」的固有代价。

#### 4.1.3 源码精读

顶层把 14 个级从大点数层串到小点数层，数据始终从 `fft_16k` 流向 `fft_2`：

- 首级接外部输入，[src/fft_top.v:26-37](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_top.v#L26-L37)：`fft_16k` 的 `data_in_real/img` 接外部 `data_real/img`，`start_next` 输出给下一级的启动信号。
- 每一级的 `data_out_*` 喂给下一级 `data_in_*`，例如 [src/fft_top.v:43-54](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_top.v#L43-L54) 中 `fft_8k` 的输入接到 `fft_16k` 的输出。

之所以**最大的延时层（`fft_16k`）必须排在最前**，正是 SDF 的本性：第一级要先把 \(N/2\) 个样本存进它那 8192 深的延时 RAM，攒够了才放行；如果把它放后面，前面的小延时层会很快空转，流水线永远填不满。设计笔记也把这条逻辑写得很直白——[scheme/参数和问题.md:5-29](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/scheme/参数和问题.md#L5-L29)：从「信号处理的实时性」一路推到「流水计算结构」「SDF FFT」「计算、存储的优化」。

单级的心脏 `butterfly_general.v` 用一个 `layer` 参数导出全部节拍，运算单元恰好是一个蝶形 + 一段延时，正是 SDF「每级极简」的体现：[src/butterfly_general.v:23-25](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v#L23-L25) 定义 `PERIOD=1<<layer`、`HALT_FOR_NEXT_LAYER=6+(PERIOD)/2`，并在 [src/butterfly_general.v:208-235](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v#L208-L235) 例化一个 `delay` 与一个 `butterfly`，构成「下支 B → RAM → 上支 C」的反馈环。

#### 4.1.4 代码实践

**资源核算实践（源码阅读型）。**

1. **目标**：用 `fft_top.v` 验证上面的资源公式。
2. **步骤**：
   - 数 `fft_top.v` 中一共例化了多少个 `fft_*` 模块（应为 14 个）。
   - 打开 `fft_16k.v`、`fft_2.v`，分别确认它们内部各例化了几个 `multiplier`（`fft_16k` 应为 1，`fft_2` 应为 0）。
   - 按 \(\sum_{i=1}^{14}2^{i-1}\) 手算总延时样本数，与 16383 比对。
3. **观察现象**：你会看到 `fft_2.v` 里没有乘法器，因为它处理的 \(W_2^0=1\)。
4. **预期结果**：14 级、13 个复数乘法器、16383 个复数样本延时存储，全部与公式吻合。
5. 若你手上有综合工具，可把工程综合一次，查看 DSP（乘法器）与 BRAM（延时）用量是否落在该量级——**待本地验证**。

#### 4.1.5 小练习与答案

- **练习 1**：如果把点数从 16384 改成 65536（\(\log_2 N=16\)），按 SDF 公式，复数乘法器、延时存储各变成多少？
  - **答**：乘法器 \(16-1=15\) 个（末级仍省乘法器）；延时存储 \(2^{16}-1=65535\) 个复数样本。
- **练习 2**：为什么本项目不直接用全并行结构？
  - **答**：全并行需要 \(\frac{N}{2}\log_2 N\) 个乘法器，\(N=16384\) 时约 11.5 万个，远超单片 FPGA 的 DSP 资源；SDF 只用 \(\log_2 N\) 个乘法器，是大点数的现实选择。

### 4.2 DIF 还是 DIT：从「蝶形与旋转因子的先后」判定

#### 4.2.1 概念说明

DIF 与 DIT 是 Cooley-Tukey 的两条等价实现路线，区分它们只看一件事——**在每一级里，蝶形加减和旋转因子相乘谁先谁后**：

- **DIF（Decimation In Frequency，按频率抽取）**：**先做蝶形加减，再乘旋转因子**。输入自然顺序，输出 bit-reverse 倒序。
- **DIT（Decimation In Time，按时间抽取）**：**先乘旋转因子，再做蝶形加减**。输入 bit-reverse 倒序，输出自然顺序。

这条判定规则的好处是：你不必死记流程图，只要在源码里找到「加减」和「乘旋转因子」两段代码，看它们的拓扑先后即可。

#### 4.2.2 核心流程

判定伪代码：

```
if (蝶形加减的结果 → 进入乘法器 × 旋转因子):   # 先蝶形后乘
    判定为 DIF  → 输出是 bit-reverse 倒序
else if (输入数据 × 旋转因子 → 进入蝶形加减):  # 先乘后蝶形
    判定为 DIT  → 输入需 bit-reverse 倒序
```

一旦判为 DIF，由于输入是外部送来的自然顺序样本、而输出未经倒序，**最终结果必然是 bit-reverse 顺序**——这正是下一节「倒序缺失」要解决的根因。

bit-reverse 的算法侧依据见 [scheme/FFT.md:104-118](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/scheme/FFT.md#L104-L118)：逐级分治后，地址的低位（奇偶）被不断翻到高位，最终形成倒序地址；计算量分析 [scheme/FFT.md:176-178](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/scheme/FFT.md#L176-L178) 也写明「最终输出的频率计算结果需要进行倒序操作」。

#### 4.2.3 源码精读

三步证据链，证明本设计是 **DIF**：

1. **蝶形单元只做加减**：[src/butterfly.v:66](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly.v#L66) `r_x_added_real <= A_real + C_real;`（上支求和 D），[src/butterfly.v:90](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly.v#L90) `r_x_subtracted_real <= C_real - A_real;`（下支求差 B），整模块内没有任何乘旋转因子操作。
2. **乘法器的「数据」输入接到蝶形输出（先蝶形的结果）**：在 `fft_32` 中，[src/fft_32.v:97-109](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_32.v#L97-L109) 例化 `multiplier`，`.a(w_D_real)` / `.b(w_D_img)` 接的是蝶形的 D 输出，`.c/.d` 接旋转因子。`fft_16` 同理，[src/fft_16.v:255-267](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_16.v#L255-L267)。
3. **拓扑顺序**：在 `butterfly_general.v` 里数据走的是 `A → butterfly → D → multiplier → 下一级`，[src/butterfly_general.v:237-238](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v#L237-L238) `D_real = w_D_real_tmp`，而这个 `D` 正是下游 `multiplier` 的输入。

**先蝶形加减、后乘旋转因子** → 判定为 **DIF** → 输出是 bit-reverse 倒序。（这与 u1-l3、u5-l1 的结论一致：MATLAB 走的也是迭代 DIF，末行为未倒序的 bit-reverse 结果，可逐级与硬件比对。）

#### 4.2.4 代码实践

**判定实践（源码阅读型）。**

1. **目标**：用自己的话写出 DIF/DIT 判定依据。
2. **步骤**：打开 `butterfly.v`、`multiplier.v`、`fft_32.v` 三份源码，画出单级数据通路：`data_in → butterfly(加减) → D → multiplier(×旋转因子) → data_out`。
3. **观察现象**：蝶形加减发生在乘法器之前；旋转因子没有出现在蝶形输入端。
4. **预期结果**：得出「DIF，输出倒序」的结论，并写下「若是 DIT，则旋转因子应出现在蝶形输入侧」作为反证。
5. **待本地验证**：无（纯源码阅读）。

#### 4.2.5 小练习与答案

- **练习 1**：假如把 `multiplier` 移到 `butterfly` 之前（即输入先乘旋转因子再进蝶形），设计会变成哪条路线？
  - **答**：变成 DIT；相应地，输入需要预先做 bit-reverse 倒序，输出才是自然顺序。
- **练习 2**：DIF 的输出为什么「恰好」是 bit-reverse 顺序，而不是某种随机乱序？
  - **答**：DIF 每级按频率下标的高/低位（相当于地址的高/低位）分治，逐级把低位翻到高位，等价于对最终地址做二进制位反转，因此是有规律的 bit-reverse，可逆。

### 4.3 已知未完成点：倒序缺失与悬空端口

#### 4.3.1 概念说明

把这套设计当成「能跑但未收尾」的工程来看，至少有四处明显的未完成点，使用前必须心里有数：

1. **输出倒序（Reverse）未实现**——DIF 流水直接吐出 bit-reverse 顺序的结果，需要外部重排，而重排硬件还没做。
2. **`data_config` 配置端口未接线**——顶层留了配置 FFT 级数的端口，但内部完全没用，点数被写死成 16384。
3. **`out_last` 末尾脉冲未赋值**——端口声明了，但没有任何 `assign` 驱动它。
4. **over/end 结束链未贯通**——只有 `start_next → start` 启动链真正贯通；`end_next`/`over` 链基本断开。

#### 4.3.2 核心流程

逐项定位的思路：

```
对于「声明了却没用」的端口：
    在文件内全文搜索该端口名 → 若除声明外无任何引用 → 判定为悬空

对于「跨级握手链」：
    逐级看 next.start 接到的是上一级 start_next，还是被常量 0 填掉
    → 被 0 填掉的即断点
```

倒序缺失的影响：由于输出是 bit-reverse，**直接把 `out_real/out_img` 当成 \(X(0),X(1),\dots,X(N-1)\) 来用会得到错位的频谱**。例如 8 点输出，下标 `0..7` 实际对应 `0,4,2,6,1,5,3,7`（3 位 bit-reverse）。对只关心幅度谱（取绝对值）的场合，倒序不影响「有没有峰值」，但会严重影响「峰值在第几个频点」的判断。

#### 4.3.3 源码精读

- **`data_config` 声明未用**：[src/fft_top.v:13](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_top.v#L13) `input [3:0] data_config,`，但在整个 `fft_top.v` 内再无任何引用。README 的接口表也标注它「条件不成熟」——[README.md:16](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/README.md#L16)。
- **`out_last` 声明未赋值**：[src/fft_top.v:19](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_top.v#L19) `output out_last`，但末尾只有三条 `assign`，[src/fft_top.v:263-265](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_top.v#L263-L265) 驱动 `out_real/out_img/out_first`，没有 `out_last`。
- **over 链断点**：首级 [src/fft_top.v:30](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_top.v#L30) `.over(over)` 接外部 `over`，但从第二级起全部填 0，例如 [src/fft_top.v:47](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_top.v#L47) `fft_8k` 的 `.over(0)`、[src/fft_top.v:65](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_top.v#L65) `fft_4k` 的 `.over(0)`，后续级同理。也就是说上一级算出的 `end_next` 没有喂给下一级的 `over`，**结束信号无法向下游传播**，下游各级的 `STATE_END` 实际不会被这一路触发。
- **倒序未实现**：README 的 `### Reverse` 一节直说——[README.md:215-217](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/README.md#L215-L217)「对最终的输出结果进行倒序操作，还没有具体的思路」。

> 提示：上一级 `start_next → 下一级 start` 这条**启动链是通的**（如 [src/fft_top.v:35](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_top.v#L35) 的 `start_next(w_start_8k)` 接到 [src/fft_top.v:46](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_top.v#L46) `fft_8k` 的 `.start(w_start_8k)`）。所以流水线能启动、能出结果；断的是「怎么知道全部算完了」的结束链。

#### 4.3.4 代码实践

**悬空端口排查实践（源码阅读型）。**

1. **目标**：独立找出 `fft_top.v` 中所有「声明未用 / 声明未赋」的端口与断开的握手链。
2. **步骤**：
   - 在 `fft_top.v` 中搜索 `data_config`、`out_last`，确认它们各只出现一次（声明处）。
   - 逐级统计 `.over(...)` 的连接：哪几级接外部 `over`、哪几级接 `0`。
   - 列一张表：端口/信号名 → 状态（在用 / 悬空 / 链断点）。
3. **观察现象**：`data_config`、`out_last` 全文仅一处；`.over` 仅首级接外部。
4. **预期结果**：表格应含 4 行——`data_config`(悬空)、`out_last`(悬空)、`over` 链(仅首级接，其余断)、`end_next` 链(算出但未消费)。
5. **待本地验证**：无。

#### 4.3.5 小练习与答案

- **练习 1**：对一个只看幅度谱（取模）的应用，bit-reverse 倒序缺失会不会影响「检测到某个频率分量存在」？
  - **答**：不会。取模后样本集合相同，峰值数量与高度不变；但「峰值出现在第几个输出下标」是错的，需倒序后才能对应真实频点。
- **练习 2**：`over` 链断开会直接导致流水线算不出结果吗？
  - **答**：不会直接导致算不出结果（启动链通、各级靠内部状态机计数驱动），但会导致下游缺少一个明确的「本帧结束」脉冲，逐帧连续运行时的帧边界管理需要额外处理。

### 4.4 改进与二次开发方向

#### 4.4.1 概念说明

针对上一节的未完成点和 SDF 的固有局限，列出四个改进方向，按「从最小改动到大改」排序：

1. **实现 bit-reverse 输出**：在输出端加一级重排缓冲，把倒序结果还原成自然顺序。
2. **参数化点数**：把当前写死的 14 级，改为可由 `data_config` 选择有效级数（如 1024/4096/16384）。
3. **支持连续流式输入**：让流水线处理完一帧后无缝接下一帧，而不是一帧一停。
4. **块浮点防溢出**：FFT 逐级放大动态范围，定点实现可能在中间级溢出或丢精度，需要逐级跟踪指数。

#### 4.4.2 核心流程

**方向 1（倒序输出）的最小可行方案**——输出端加一块深度为 \(N\) 的双口 RAM 作重排缓冲：

```
写侧：第 i 个到达的结果，写入地址 = bit_reverse(i)
       （例如 i=6(110) → 写入地址 011=3）
读侧：按地址 0,1,2,...,N-1 顺序读出 → 自然顺序结果
```

要点：

- 写地址生成：用一个 \(\log_2 N\) 位计数器，将其二进制位反转后作写地址。
- 利用 `out_first` 标记一帧开始、`out_last`（需先补上赋值）标记一帧结束，触发读侧顺序读出。
- 深度 \(N\)、位宽 32（实部或虚部），实虚分两块；可复用项目已有的双口 RAM IP 风格。

**方向 2（参数化点数）**：在 `fft_top` 里根据 `data_config` 给超出选定级数的「高层模块」强制 `butterfly_enable=0`、并把它们的输出旁路（直通），从而在 2、4、…、16384 间切换。难点在于各级延时深度与旋转因子 ROM 是按层固定生成的，真正可变点数往往要换一套 ROM。

**方向 3（连续流式）**：关键是消除「灌满」带来的帧间隔——可以让输入持续喂入，每级用满即放行，并补全 over/end 链来标记帧边界，使多帧在流水线里交叠。

**方向 4（块浮点）**：每级蝶形后统计本块数据的最大有效位数，整体左/右移并记一个公共指数，保证既不溢出又不丢精度；这是商用 FFT IP（如 Xilinx xfft）的标准做法。

#### 4.4.3 源码精读

- 倒序方案需要的「帧边界」信号已经存在一半：[src/fft_top.v:265](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_top.v#L265) `assign out_first = w_out_start;` 已经给出帧首脉冲，缺的 `out_last` 正好可作为帧尾。
- 倒序缓冲可复用 `delay.v` 里那套双口 RAM「先写后读 + 地址推进」的思路（见 u3-l2），把读地址改成顺序、写地址改成 bit-reverse 即可。
- 参数化点数若想利用现成同构结构，`butterfly_general.v` 的 `layer` 参数与 [src/butterfly_general.v:23-25](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v#L23-L25) 的派生常量已经具备「改一个参数换一级」的能力，瓶颈在于 ROM 实例名不可参数化（见 u4-l4、u5-l3）。

#### 4.4.4 代码实践

**最小倒序输出方案设计（设计型实践）。**

1. **目标**：给出一个「输出端 RAM 重排」的可落地草稿（伪代码 + 接口），不要求综合通过。
2. **步骤**：
   - 写一个 `bit_reverse_order` 模块，端口：`clk/rst`、`in_valid`、`in_data[31:0]`、`frame_first`、`frame_last`、`out_valid`、`out_data[31:0]`。
   - 内部例化一块深度 \(N\)（如 16384）、位宽 32 的双口 RAM。
   - 写地址 = `bit_reverse(write_counter)`；写使能 = `in_valid`。
   - 当 `frame_last` 到达后，从地址 0 起顺序读出 \(N\) 个，配 `out_valid`。
   - 实部、虚部各例化一份。
3. **观察现象**：手算一个 8 点例子验证——若输入到达顺序的下标为 `0,4,2,6,1,5,3,7`（bit-reverse 后写入地址 `0,1,2,3,4,5,6,7`），顺序读出即得到自然下标 `0..7`。
4. **预期结果**：得到一份接口与写/读时序清晰的草稿，能说明它如何把 DIF 倒序还原为自然顺序。
5. **待本地验证**：真要综合，需复用项目的双口 RAM IP 并校准读写时序，与 `out_first`/`out_last` 对齐——本地搭建后验证。

> 说明：以上为示例代码/设计草稿，并非项目原有源码，标注清楚以便区分。

#### 4.4.5 小练习与答案

- **练习 1**：倒序重排缓冲的深度为什么必须是 \(N\)，能不能更小？
  - **答**：必须能装下完整一帧 \(N\) 个结果，才能在写完整帧后按自然顺序读出；更小会丢数据，除非做更复杂的多帧交叠调度。
- **练习 2**：块浮点为什么能同时缓解「溢出」和「丢精度」？
  - **答**：它把整个数据块整体左/右移并记录公共指数，大值不溢出、小值不被定点截断吞掉，等价于给整块数据一个共享的「浮点」动态范围。

## 5. 综合实践

**撰写一份 1 页《架构评估报告》**（建议用 Markdown，300~500 字 + 一张图），把本讲四节串起来。报告至少包含：

1. **整体定位**：一句话说明这是一条什么结构、多少点、走 DIF 还是 DIT 的 FFT 流水线。
2. **DIF/DIT 判定依据**：引用 `butterfly.v`（先加减）与 `fft_32.v`（后乘旋转因子）的代码位置，给出「先蝶形后乘 → DIF」的判断，并据此推出输出为 bit-reverse。
3. **资源核算**：列出 14 级、13 个复数乘法器、\(N-1=16383\) 个复数样本延时存储，并说明为何选 SDF（大点数 + 资源受限）。
4. **已知缺陷清单**：倒序未实现、`data_config` 未接线、`out_last` 未赋值、over/end 链未贯通，各给一行影响说明。
5. **bit-reverse 缺失的影响**：举例（如 8 点下标 `0,4,2,6,1,5,3,7`）说明直接用 `out_*` 会频点错位。
6. **最小改进方案**：画出「输出端加一块深度 \(N\) 双口 RAM，写地址 = bit_reverse(i)，读地址顺序」的重排框图。

完成后，建议把它与 `tb/fft_top_tb.v` 的仿真波形对照：在波形里数一数 `out_real` 的出现顺序，验证它确实不是自然顺序，从而印证你的报告结论。

## 6. 本讲小结

- 本设计是一条 **SDF（单路延迟反馈）流水线 FFT**：14 级级联，约 13 个复数乘法器、\(N-1\) 个复数样本延时存储，是「大点数 + 资源受限」下的现实选择。
- 由「**先蝶形加减、后乘旋转因子**」（`butterfly.v` → `fft_32.v` 的 `multiplier`）判定为 **DIF**，故输出是 **bit-reverse 倒序**。
- 已知未完成点：**倒序（Reverse）未实现**、`data_config` 配置端口未接线、`out_last` 未赋值、over/end 结束链未贯通（仅 start_next→start 启动链通）。
- bit-reverse 缺失使直接输出的频点下标错位，使用前必须外部重排或补一级重排硬件。
- 改进方向：输出端 RAM 重排做倒序、参数化点数、连续流式输入、块浮点防溢出；其中倒序重排是最小可行、收益最高的一步。

## 7. 下一步学习建议

- **横向对比**：阅读 Xilinx LogiCORE `xfft`（README 中提到的 `pg109-xfft.pdf`）的架构章节，对比商用 IP 如何用「可配置点数 + 块浮点 + 内置倒序」解决本项目的未完成点。
- **动手改进**：选 4.4 的「输出端 RAM 重排」作为第一个二次开发任务，先在 `tb/fft_top_tb.v` 上加一个倒序后处理模块，用 MATLAB 黄金参考（u5-l1）验证频点是否对齐。
- **回到源码**：若想更深理解 SDF 时序，重读 u3-l3 的 `rotator_valid` / `HALT_FOR_NEXT_LAYER` 对齐机制，并尝试把 over/end 结束链补全，做成可连续流式运行的版本。
- **延伸阅读**：`scheme/参数和问题.md` 里列出的实时 FFT 参考链接，以及奥本海姆《离散时间信号处理》第 9 章，补齐算法与工程的双重背景。
