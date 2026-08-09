# 读懂利用率报告（utilization）

## 1. 本讲目标

学完本讲后，你应当能够：

- 打开一份 Vivado 利用率（utilization）报告，**按目录跳转**到 `Slice Logic`、`Memory`、`DSP`、`Clocking`、`Primitives` 等章节，并理解每一节回答的是什么问题。
- 从那张贯穿全文的 `Used / Fixed / Prohibited / Available / Util%` 六列表里，准确读出 **Slice LUTs、Slice Registers、DSP、Block RAM** 的使用量与占比。
- 拆解 LUT 的子类（`LUT as Logic` / `LUT as Memory` / `LUT as Shift Register`），理解“一片 LUT 既能当组合逻辑，也能当移位寄存器（SRL）”。
- 看懂 `Primitives` 原语表里的常见条目（`FDRE`、`FDCE`、`LUT2~6`、`CARRY4`、`SRL16E`、`BUFG`），并能据此反推一个设计是“怎么搭出来的”。

本讲所有结论都基于仓库内真实存在的两份报告：CIC Compiler 方案的 `utilization_impl_R16_N4.txt`（主分析对象）与 MATLAB HDL Coder 方案的同名报告（对照对象），均为 R=16、N=4、时钟 100 MHz、实现（impl）阶段。

---

## 2. 前置知识

进入本讲前，你需要已经掌握（来自 u1-l3 与 u1-l4）：

- **CIC 滤波器只用加减法、不用乘法器**。这是 u1-l3 讲过的核心结论，本讲会在报告里直接验证它——`DSP` 那一栏会是 0。
- **综合（synth）与实现（impl）的区别**。本讲只看 `impl` 报告（文件名里的 `impl`），因为它对应真实布局布线后的资源占用，是论文“Post-Implementation 评估”的依据。
- **目标器件 xc7a100tcsg324-1**。这是一颗 Artix-7 FPGA，本讲里所有 `Available`（可用量）数字都由它决定：Slice LUTs 共 63400、Slice Registers 共 126800、Block RAM 135 块、DSP 240 个。

本讲会用到的唯一公式——利用率（Util%）的计算：

\[ \text{Util\%} = \frac{\text{Used}}{\text{Available}} \times 100\% \]

例如 Slice LUTs 用了 155 个，可用 63400 个，则占比为 155 / 63400 × 100% ≈ 0.244%，报告里四舍五入显示为 `0.24`。

> 上一讲（u2-l1）我们看的是**时序**报告——它回答“设计跑得够不够快”。本讲看的是**利用率**报告——它回答“设计占多大地方、吃了哪些资源”。两者合起来，就是 FPGA 后实现评估的两根支柱。

---

## 3. 本讲源码地图

本讲的“源码”是两份 Vivado 利用率报告文本：

| 文件 | 方案 | 作用 |
| --- | --- | --- |
| `vivado_reports/reports_at_100Mhz/CIC Compiler/utilization_impl_R16_N4.txt` | CIC Compiler IP | **主分析对象**。绝大多数引用取自这里，它用 `SRL16E` 实现梳状器延迟，风格很有代表性。 |
| `vivado_reports/reports_at_100Mhz/MATLAB HDL Coder/utilization_impl_R16_N4.txt` | MATLAB HDL Coder | **对照对象**。同样 R16/N4/100MHz，但资源构成不同，用来体会“实现风格”的差异。 |

两份报告都由 Vivado 命令 `report_utilization` 生成，文件头部就写明了这一点。

---

## 4. 核心概念与源码讲解

### 4.1 报告整体结构与 Util% 计算规则

#### 4.1.1 概念说明

利用率报告回答一个朴素的问题：**这个设计在 FPGA 上到底占用了多少资源？**

为了把“占用”讲清楚，Vivado 把 FPGA 里各种可量化的硬件按类别分成了若干章节。打开报告你会先看到一份 `Table of Contents`（目录），列出了全部章节：

[vivado_reports/reports_at_100Mhz/CIC Compiler/utilization_impl_R16_N4.txt:L15-L27](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/utilization_impl_R16_N4.txt#L15-L27)

其中本讲要逐节精读的是：

- `1. Slice Logic`：逻辑资源（LUT 与寄存器），**最重要的一节**。
- `3. Memory`：Block RAM 存储块。
- `4. DSP`：DSP 乘加单元。
- `6. Clocking`：时钟资源（如全局时钟缓冲）。
- `8. Primitives`：原语表，设计最终被映射成的底层器件清单。

报告头部则是一段元信息，告诉你这份报告是谁、在何时、用什么命令、对哪个设计生成的：

[vivado_reports/reports_at_100Mhz/CIC Compiler/utilization_impl_R16_N4.txt:L1-L11](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/utilization_impl_R16_N4.txt#L1-L11)

关键信息包括：`Command : report_utilization`（生成命令）、`Design : cic_compiler_0`（设计名）、`Device : xc7a100tcsg324-1`（目标器件）、`Design State : Routed`（已布线，确认是 impl 阶段）。

#### 4.1.2 核心流程

阅读利用率报告的标准动作：

1. **先看头部**确认这是哪份报告（设计名、器件、`Design State`）。
2. **看目录**，按需跳节。日常评估只需看 `Slice Logic`、`DSP`、`Memory`、`Primitives` 四节。
3. **每个节都是同一张六列表**，列名固定如下：

```
| Site Type | Used | Fixed | Prohibited | Available | Util% |
```

- `Site Type`：资源类型名。
- `Used`：实际使用了多少。
- `Fixed`：被固定（不可移动）的数量，通常为 0。
- `Prohibited`：被禁止使用的数量，通常为 0。
- `Available`：该资源在**这颗器件**上的总可用量。
- `Util%`：占比，即 \(\text{Used}/\text{Available}\times 100\%\)。

只要看懂这一张表的列含义，剩下的所有节都只是换了 `Site Type` 而已。

#### 4.1.3 源码精读

以 `1. Slice Logic` 的表头与表身为例，确认六列的写法：

[vivado_reports/reports_at_100Mhz/CIC Compiler/utilization_impl_R16_N4.txt:L32-L45](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/utilization_impl_R16_N4.txt#L32-L45)

取 `Slice LUTs` 这一行验算 Util%：

```
| Slice LUTs   |  155 |     0 |          0 |     63400 |  0.24 |
```

套公式：

\[ \frac{155}{63400} \times 100\% \approx 0.244\% \;\to\; \text{报告显示 } 0.24 \]

完全吻合。也就是说，**你随时可以用 `Used ÷ Available` 自行复核报告里的 `Util%`**，这是一种很好的自查手段。

> 小贴士：表下方偶尔会出现一行 `* Warning! LUT value is adjusted to account for LUT combining.`（见 L46）。这是说工具为“把两个 LUT 合并进一个物理切片”而对计数做了微调，遇到时不必纠结具体数值，关注数量级即可。

#### 4.1.4 代码实践

**实践目标**：熟悉报告导航与 Util% 复核。

**操作步骤**：

1. 打开 `vivado_reports/reports_at_100Mhz/CIC Compiler/utilization_impl_R16_N4.txt`。
2. 找到 `Table of Contents`（L15–L27），数一数共列出多少个章节。
3. 跳到 `6. Clocking`，找到 `BUFGCTRL` 那一行，记下 `Used` 与 `Available`。

**需要观察的现象**：每个章节的表头六列名称是否完全一致；`BUFGCTRL` 的 Used 是否只有 1 个。

**预期结果**：目录共 10 个章节；`BUFGCTRL | 1 | 0 | 0 | 32 | 3.13`，即只用了 1 个全局时钟缓冲，占比 1/32 ≈ 3.13%。

#### 4.1.5 小练习与答案

**练习 1**：`Slice Registers` 的 Available 是 126800、Used 是 261，手算 Util% 应该是多少？

**参考答案**：261 / 126800 × 100% ≈ 0.2058%，报告四舍五入为 `0.21`（见 L40）。

**练习 2**：为什么几乎每行的 `Fixed` 和 `Prohibited` 都是 0？

**参考答案**：这两个列表示被工具或约束“钉死/禁用”的资源，普通综合实现流程一般不主动固定或禁止，所以为 0；它们只有在手动放置（如 `Pblock`、`LOC` 约束）时才可能非 0。

---

### 4.2 Slice Logic：LUT 与寄存器

#### 4.2.1 概念说明

`Slice Logic` 是利用率报告里**最关键的一节**，因为它统计了组合逻辑和时序逻辑两类基础资源：

- **LUT（Look-Up Table，查找表）**：实现组合逻辑的基本单元。7 系列 FPGA 的一个 LUT 是 6 输入的，既可以当一个 6 输入函数（`LUT6`），也可以拆成两个 5 输入函数（对应报告里的 `O5/O6` 输出）。`Slice LUTs` 是 LUT 的总量。
- **寄存器（Slice Registers）**：即触发器（Flip-Flop），用来保存状态。CIC 滤波器里大量寄存器来自积分器的累加器和梳状器的延迟链。

这两类下面还有子类，初学者最需要分清的是 LUT 的两种“身份”：

- **`LUT as Logic`**：LUT 被当作普通组合逻辑用（实现与/或/异或等布尔函数）。
- **`LUT as Memory`**：LUT 被当作存储用，又细分为：
  - `LUT as Distributed RAM`（分布式 RAM）；
  - `LUT as Shift Register`（移位寄存器，即后面会看到的 `SRL16E`）。

也就是说，**同一片 LUT，既可以是“逻辑门”，也可以是“一小段移位寄存器”**——这点对理解 CIC 的实现风格至关重要。

寄存器则分两种：`Register as Flip Flop`（边沿触发的触发器，正常时序逻辑）与 `Register as Latch`（电平敏感的锁存器，通常是要避免的）。

#### 4.2.2 核心流程

读 `Slice Logic` 一节的步骤：

1. 先抓**两个总量**：`Slice LUTs` 的 Used、`Slice Registers` 的 Used。这是汇报资源占用时最常被引用的两个数字。
2. 再看 LUT 的**子类拆分**：`LUT as Logic` 占多少、`LUT as Memory` 占多少、其中又有多少是 `Shift Register`。
3. 看寄存器子类：`Register as Flip Flop` 是否等于总量（`Register as Latch` 是否为 0——为 0 才是好设计）。
4. 交叉看 `1.1 Summary of Registers by Type`，了解这些触发器带什么样的控制信号（复位/置位/时钟使能）。

#### 4.2.3 源码精读

抓总量与子类——这是本设计（CIC Compiler、R16、N4、100MHz）的逻辑资源画像：

[vivado_reports/reports_at_100Mhz/CIC Compiler/utilization_impl_R16_N4.txt:L35-L42](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/utilization_impl_R16_N4.txt#L35-L42)

关键几行：

```
| Slice LUTs                 |  155 |  ... |     63400 |  0.24 |
|   LUT as Logic             |  130 |      |           |       |
|   LUT as Memory            |   25 |      |           |       |
|     LUT as Shift Register  |   25 |      |           |       |
| Slice Registers            |  261 |  ... |    126800 |  0.21 |
|   Register as Flip Flop    |  261 |      |           |       |
|   Register as Latch        |    0 |      |           |       |
```

读出来的事实：

- **LUT 总量 155**：其中 130 片当组合逻辑，25 片当存储——而且这 25 片**全部是移位寄存器**（`LUT as Shift Register`，分布式 RAM 为 0）。这 25 片移位寄存器正是梳状器差分延迟的载体（详见 4.4 节）。
- **寄存器总量 261**：`Register as Flip Flop` 也是 261、`Register as Latch` 为 0。说明设计**没有意外生成锁存器**，结构干净；这 261 个触发器主要是积分器的累加寄存器与各流水级寄存器。
- 占用率极低（LUT 0.24%、寄存器 0.21%）：这对 xc7a100t（中规模器件）来说，CIC 是个很“小”的设计。

再看 `1.1 Summary of Registers by Type`，它会按“是否带时钟使能（CE）+ 同步/异步 复位或置位”拆分这 261 个触发器：

[vivado_reports/reports_at_100Mhz/CIC Compiler/utilization_impl_R16_N4.txt:L49-L65](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/utilization_impl_R16_N4.txt#L49-L65)

其中最显著的一行是 `260 | Yes | Reset | -`（L64）：**260 个触发器都带时钟使能（CE=Yes）和同步复位（Synchronous Reset）**，另有 1 个带同步置位。这正好对应后面 Primitives 表里 `FDRE = 260`（带同步复位的 D 触发器）。

#### 4.2.4 代码实践

**实践目标**：从报告中提取逻辑资源并理解子类。

**操作步骤**：

1. 打开 CIC Compiler 的 `utilization_impl_R16_N4.txt`，定位 `1. Slice Logic`。
2. 抄下 `Slice LUTs`、`LUT as Logic`、`LUT as Memory`、`LUT as Shift Register` 四个值。
3. 抄下 `Slice Registers` 与 `Register as Latch`。

**需要观察的现象**：`LUT as Memory` 是否等于 `LUT as Shift Register`？`Register as Latch` 是否为 0？

**预期结果**：`LUT as Memory = 25` 且 `LUT as Shift Register = 25`（两者相等，说明这片“存储型 LUT”全是移位寄存器、没有分布式 RAM）；`Register as Latch = 0`（没有锁存器）。

#### 4.2.5 小练习与答案

**练习 1**：如果一个设计的 `LUT as Memory` 很高、且其中大部分是 `LUT as Shift Register`，这通常意味着什么？

**参考答案**：意味着设计里有较深的延迟链/移位需求，工具选择用 LUT 实现移位寄存器（`SRL16E`），而不是用普通触发器堆叠，以省面积。CIC 的梳状器差分延迟正是这种场景。

**练习 2**：为什么我们要关注 `Register as Latch` 是否为 0？

**参考答案**：锁存器（latch）是电平敏感的，常由 `if` 不写全 `else` 这类不完整的条件分支意外生成，会导致时序难以分析和收敛。专业 RTL 设计通常**刻意避免**锁存器，所以 `Register as Latch = 0` 是一个“代码写得很规范”的信号。

---

### 4.3 Memory、DSP 与 Clocking 章节

#### 4.3.1 概念说明

这三节各自统计一类“专用资源”，对 CIC 滤波器而言结论都非常干净，但含义不同：

- **Memory（Block RAM，块 RAM）**：FPGA 里专用的双端口存储块（7 系列里叫 `RAMB36/RAMB18`），适合做大容量存储（FIFO、数据缓存、查找表）。CIC 报告里这一项是 **0**——因为它的延迟链用 LUT（SRL16E）就够了，没必要上 Block RAM。
- **DSP**：即 `DSP48` 单元，内含硬件乘法器与累加器，是做 FIR、FFT、矩阵乘的主力。CIC 报告里这一项也是 **0**——因为 CIC 滤波器**只有加法/减法，没有乘法**（这是 u1-l3 讲过的根本特性）。`DSP = 0` 几乎就是“这是个 CIC”的签名。
- **Clocking**：时钟相关资源。本设计只用到 `BUFGCTRL = 1`，即一个全局时钟缓冲（`BUFG`），把单一时钟分发到全片。FPGA 里全局时钟网络数量有限（这里 32 个），所以时钟越少越好。

#### 4.3.2 核心流程

评估这三节的步骤：

1. **`3. Memory`**：看 `Block RAM Tile` 的 Used 是否为 0。为 0 表示没用专用存储块。
2. **`4. DSP`**：看 `DSPs` 的 Used。对照 CIC“无乘法”的理论预期，验证是否为 0。
3. **`6. Clocking`**：看 `BUFGCTRL` 的 Used，判断用了几个全局时钟域（通常 CIC 单时钟域为 1）。

#### 4.3.3 源码精读

**Memory 节**——Block RAM 全部为 0：

[vivado_reports/reports_at_100Mhz/CIC Compiler/utilization_impl_R16_N4.txt:L100-L106](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/utilization_impl_R16_N4.txt#L100-L106)

```
| Block RAM Tile |    0 |  ... |       135 |  0.00 |
|   RAMB36/FIFO* |    0 |      |       135 |       |
|   RAMB18       |    0 |      |       270 |       |
```

器件共有 135 个 Block RAM 块，本设计一个都没用——延迟链落在 LUT（SRL16E）上，而非 Block RAM。

**DSP 节**——零乘法器，CIC 的特征签名：

[vivado_reports/reports_at_100Mhz/CIC Compiler/utilization_impl_R16_N4.txt:L113-L117](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/utilization_impl_R16_N4.txt#L113-L117)

```
| DSPs  |    0 |  ... |       240 |  0.00 |
```

器件共有 240 个 DSP 单元，本设计用了 **0 个**。这把 u1-l3 的理论结论变成了一个可观测的事实：**CIC 滤波器不需要任何乘法器**，因此它极省 DSP 资源、也极适合放在需要大量乘法器的高端滤波之前作为第一级抽取。

**Clocking 节**——单全局时钟域：

[vivado_reports/reports_at_100Mhz/CIC Compiler/utilization_impl_R16_N4.txt:L147-L157](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/utilization_impl_R16_N4.txt#L147-L157)

```
| BUFGCTRL   |    1 |  ... |        32 |  3.13 |
```

只用了 1 个 `BUFG`（全局时钟缓冲）。说明整个设计跑在一个全局时钟域上，时序约束简单、好收敛（这与 u2-l1 看到的 WNS 充裕也互相印证）。

> 旁注：`5. IO and GT Specific` 节里 `Bonded IOB = 30`（见 L126）表示占用 30 个引脚，属于端口数量统计，不是核心评估指标，本讲略过。

#### 4.3.4 代码实践

**实践目标**：验证 CIC“零乘法器、零块 RAM”的资源特征。

**操作步骤**：

1. 在同一份报告里跳到 `4. DSP`，记录 `DSPs` 的 Used。
2. 跳到 `3. Memory`，记录 `Block RAM Tile` 的 Used。
3. 跳到 `6. Clocking`，记录 `BUFGCTRL` 的 Used。

**需要观察的现象**：DSP 和 Block RAM 是否都为 0？时钟缓冲是否只有 1 个？

**预期结果**：`DSPs = 0`、`Block RAM Tile = 0`、`BUFGCTRL = 1`。三者共同勾勒出一个“小而纯”的 CIC：无乘法、无大存储、单时钟。

#### 4.3.5 小练习与答案

**练习 1**：同样是滤波器，为什么 FIR 滤波器的 `DSP` 一栏通常远大于 0，而 CIC 却是 0？

**参考答案**：FIR 需要做“抽头系数 × 样本”的乘法，必须用乘法器（DSP）；CIC 的冲激响应只有 +1/−1，差分与积分只用到加法和减法，因此不需要乘法器，`DSP = 0`。

**练习 2**：本设计的延迟链用 LUT（`SRL16E`）而非 Block RAM 实现。什么情况下设计才会“被迫”使用 Block RAM？

**参考答案**：当单条延迟链的深度超过 LUT 能容纳的范围（一个 `SRL16E` 最多 16 级），或需要同时缓存大块数据（如大 FIFO、多通道缓存）时，工具才会改用 Block RAM。本设计 R、N 不大，延迟深度有限，LUT 移位寄存器就足够了。

---

### 4.4 Primitives 原语表

#### 4.4.1 概念说明

`Primitives`（原语）节是整份报告里**信息量最大**的一节：它列出设计最终被“拆”成了哪些底层器件库元件（Xilinx 原语），并按使用数量从多到少排序。读懂它，就等于看懂了设计是“用什么零件搭出来的”。

本讲常见的原语名词解释：

| 原语 | 全称（含义） | Functional Category | 作用 |
| --- | --- | --- | --- |
| `FDRE` | D Flip-Flop with Clock Enable + synchronous **Reset** | Flop & Latch | 带同步复位的 D 触发器 |
| `FDSE` | D Flip-Flop with Clock Enable + synchronous **Set** | Flop & Latch | 带同步置位的 D 触发器 |
| `FDCE` | D Flip-Flop with Clock Enable + asynchronous **Clear** | Flop & Latch | 带异步清零的 D 触发器 |
| `LUT2~LUT6` | 2~6 输入查找表 | LUT | 组合逻辑（输入越少越省面积） |
| `CARRY4` | 4 位快速进位链 | CarryLogic | 加法器/计数器的专用进位逻辑 |
| `SRL16E` | 16 位移位寄存器（带时钟使能） | Distributed Memory | 用一片 LUT 实现最多 16 级移位 |
| `BUFG` | 全局时钟缓冲 | Clock | 全局时钟网络驱动 |
| `IBUF`/`OBUF` | 输入/输出缓冲 | IO | 芯片引脚的输入/输出接口 |

#### 4.4.2 核心流程

读 `Primitives` 节的步骤：

1. **按 `Used` 从大到小读**，前几名往往就勾勒出设计的骨架（触发器最多？LUT 最多？有无 SRL？有无 DSP？）。
2. **做一个交叉校验**：触发器原语（`FDRE+FDSE+FDCE…`）的总数，应当≈ `Slice Registers` 的总量；这是验证报告自洽的好办法。
3. **对比不同实现风格**：同样是 CIC，IP 生成的与 HDL Coder 生成的，原语构成会明显不同——这正是 u2-l4 / u3-l1 跨方案对比的基础。

#### 4.4.3 源码精读

先看主分析对象（CIC Compiler）的 Primitives 表：

[vivado_reports/reports_at_100Mhz/CIC Compiler/utilization_impl_R16_N4.txt:L181-L196](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/utilization_impl_R16_N4.txt#L181-L196)

关键几行（按用量排序）：

```
| FDRE     |  260 |        Flop & Latch |
| LUT3     |   96 |                 LUT |
| LUT2     |   74 |                 LUT |
| SRL16E   |   41 |  Distributed Memory |
| LUT4     |   36 |                 LUT |
| CARRY4   |   25 |          CarryLogic |
| BUFG     |    1 |               Clock |
| FDSE     |    1 |        Flop & Latch |
```

读出来的事实：

- **`FDRE = 260` + `FDSE = 1` = 261**，与 4.2 节 `Slice Registers = 261` **完全吻合**——这是报告内部的自洽性证据。
- **`SRL16E = 41`**：出现了移位寄存器原语，说明 CIC Compiler 把梳状器的差分延迟做成了 LUT 移位寄存器（对应 4.2 节 `LUT as Shift Register`）。注意 `SRL16E` 的“原语实例数”（41）与 `LUT as Shift Register` 的“LUT 站点数”（25）并不直接相等，差异来自 LUT 合并与计数口径（见 L46 的 warning），两者都只是共同确认“用了 LUT 移位寄存器”这一事实。
- **`CARRY4 = 25`**：25 个进位链，对应积分器累加与梳状器差分里的多比特加/减法。
- 没有 `DSP48`、没有 `RAMB36/RAMB18`：再次印证无乘法器、无块 RAM。

再看对照对象（MATLAB HDL Coder）的 Primitives 表，体会风格差异：

[vivado_reports/reports_at_100Mhz/MATLAB HDL Coder/utilization_impl_R16_N4.txt:L176-L190](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/MATLAB%20HDL%20Coder/utilization_impl_R16_N4.txt#L176-L190)

```
| FDCE     |  266 |        Flop & Latch |
| LUT2     |  186 |                 LUT |
| CARRY4   |   40 |          CarryLogic |
| BUFG     |    1 |               Clock |
```

两个方案、同样 R16/N4/100MHz，实现零件却大不相同：

| 维度 | CIC Compiler | MATLAB HDL Coder |
| --- | --- | --- |
| 触发器原语 | `FDRE` 260 + `FDSE` 1（**同步**复位/置位） | `FDCE` 266（**异步**清零） |
| 延迟实现 | `SRL16E` 41（LUT 移位寄存器） | **没有** SRL16E（0 个） |
| LUT 构成 | 大量 `LUT3/LUT4`，总量 155 | 几乎全是 `LUT2`，总量 169 |

这说明：HDL Coder 把梳状器延迟展开成了一串**离散触发器 + 少量 LUT**，而 CIC Compiler IP 用了 **LUT 移位寄存器（SRL16E）** 这种更紧凑的器件。两份报告 `Design` 名也不同（`cic_compiler_0` vs `CIC_R16_N4`，见各自头部 L7），印证它们来自不同生成流程。这种“同功能、不同零件”的差异，正是后续 u2-l4、u3-l1 跨方案对比的核心素材。

#### 4.4.4 代码实践

**实践目标**：用 Primitives 表反推实现风格，并做自洽校验。

**操作步骤**：

1. 打开 CIC Compiler 报告的 `8. Primitives`，列出 `Used` 排名前 5 的原语及数量。
2. 把所有触发器原语（`FDRE`、`FDSE`、`FDCE` …）的 `Used` 相加。
3. 打开 MATLAB HDL Coder 报告的 `8. Primitives`，看它有没有 `SRL16E`。

**需要观察的现象**：触发器原语总数是否等于 `Slice Registers`？两个方案在 `SRL16E` 上是否不同？

**预期结果**：CIC Compiler 前 5 名 = `FDRE 260、LUT3 96、LUT2 74、SRL16E 41、LUT4 36`；触发器原语 260 + 1 = 261 = `Slice Registers`。MATLAB HDL Coder **没有** `SRL16E`，触发器为 `FDCE 266`，对应其 `Slice Registers = 266`。

#### 4.4.5 小练习与答案

**练习 1**：为什么说"`FDRE` 数量 + `FDSE` 数量"应当与 `Slice Registers` 相等？

**参考答案**：`FDRE/FDSE/FDCE` 等都是触发器原语，每一个都占用一个寄存器站点。把所有触发器类原语的实例数求和，就是触发器总数；而 `Slice Registers` 统计的也是寄存器站点占用，二者口径一致，理应相等（CIC Compiler 中 260 + 1 = 261，精确吻合）。

**练习 2**：CIC Compiler 有 `SRL16E` 而 HDL Coder 没有，这对面积意味着什么？

**参考答案**：`SRL16E` 能用一片 LUT 实现 16 级移位，比“16 个触发器”省得多。CIC Compiler 用它实现梳状延迟，因而触发器更少、资源更紧凑；HDL Coder 没用此优化，转而用更多普通 LUT/触发器搭延迟链，所以同配置下 LUT 略多（169 vs 155）。这是实现风格而非功能差异。

---

## 5. 综合实践

本实践把 4.2–4.4 三节串起来，完成一次完整的资源画像。它也是本讲指定的主实践任务。

**任务**：仅凭 CIC Compiler 的 `utilization_impl_R16_N4.txt`，提取四项核心资源，并列出用量前 5 的原语。

**操作步骤**：

1. 打开 `vivado_reports/reports_at_100Mhz/CIC Compiler/utilization_impl_R16_N4.txt`。
2. 从 `1. Slice Logic` 抄录 `Slice LUTs`、`Slice Registers`（含各自的 Available 与 Util%）。
3. 从 `3. Memory` 抄录 `Block RAM Tile`，从 `4. DSP` 抄录 `DSPs`。
4. 从 `8. Primitives` 列出 `Used` 排名前 5 的原语。
5. 用 \(\text{Used}/\text{Available}\) 复核至少一项 Util%。

把结果填入下表：

| 指标 | Used | Available | Util% |
| --- | --- | --- | --- |
| Slice LUTs | | 63400 | |
| Slice Registers | | 126800 | |
| Block RAM Tile | | 135 | |
| DSPs | | 240 | |

**预期结果**（CIC Compiler、R16、N4、100MHz）：

| 指标 | Used | Available | Util% |
| --- | --- | --- | --- |
| Slice LUTs | 155 | 63400 | 0.24 |
| Slice Registers | 261 | 126800 | 0.21 |
| Block RAM Tile | 0 | 135 | 0.00 |
| DSPs | 0 | 240 | 0.00 |

用量前 5 的原语：`FDRE 260`、`LUT3 96`、`LUT2 74`、`SRL16E 41`、`LUT4 36`。

**一句话结论**：这是一个极小、无乘法器（DSP=0）、无块 RAM、用 LUT 移位寄存器（SRL16E）承担梳状延迟的 CIC 设计，逻辑资源占用不到器件的 0.25%。

> 进阶（可选）：再打开 MATLAB HDL Coder 的同名报告，把它的四项指标与前 5 原语也填一遍，对照体会“同功能、不同零件”。这会自然地把你引向 u2-l4（三方案对比）与 u3-l1（资源横向对比）。

---

## 6. 本讲小结

- 利用率报告由 `report_utilization` 生成，靠一份 `Table of Contents` 分节，**每一节都是同一张六列表**：`Site Type / Used / Fixed / Prohibited / Available / Util%`。
- `Util% = Used / Available × 100%`，可随时手算复核（如 155/63400 ≈ 0.24%）。
- `Slice Logic` 是最关键一节：本设计 `Slice LUTs = 155`（其中 25 片当移位寄存器）、`Slice Registers = 261`、无锁存器。
- `Memory` 与 `DSP` 双双为 0——这是 CIC“无乘法器、无大存储”的直接证据，`DSP = 0` 几乎是 CIC 的特征签名。
- `Clocking` 显示只用了 1 个 `BUFG`，单时钟域，结构简单、好收敛。
- `Primitives` 节信息量最大：CIC Compiler 用 `FDRE`(260) + `SRL16E`(41) + `CARRY4`(25) 搭建，触发器总数 261 与 `Slice Registers` 自洽；而 HDL Coder 用 `FDCE`(266) + `LUT2`(186)、无 SRL16E，体现不同生成流程的风格差异。

---

## 7. 下一步学习建议

- 接下来学 **u2-l3（报告元信息与 .txt/.rpx 格式）**：本讲多次引用报告头部的 `Command / Design / Device` 等元信息，u2-l3 会把“这些元信息从哪来、`.txt` 与 `.rpx` 有何区别”讲透。
- 之后学 **u2-l4（三种 CIC 实现方案对比）**：本讲末尾已经埋下“CIC Compiler vs HDL Coder 原语差异”的伏笔，u2-l4 会加入 Open-source RTL 方案做三方对比。
- 到专家层再看 **u3-l1（资源利用率横向对比）**：在固定 R/N 下系统比较三方案的 LUT/寄存器/DSP，本讲的提取方法（抓总量 + 看子类 + 读 Primitives）正是那篇的基础工具。
