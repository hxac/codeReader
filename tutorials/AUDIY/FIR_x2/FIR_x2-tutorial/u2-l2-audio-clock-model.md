# 音频时钟模型：MCLK/BCK/LRCK 与 2 倍过采样概念

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚测试激励（testbench）是如何用**一个自由运行的计数器**，从一个主时钟 MCLK 分频出位时钟 BCK 和声道时钟 LRCK 的；
- 掌握「计数器的第 k 位 = MCLK 的 \(2^{k+1}\) 分频」这一通用公式，并据此算出 BCK、LRCK 的真实频率；
- 理解 FIR_x2 是**单时钟域**设计——所有触发器都挂在 MCLK 上，BCK/LRCK 只是「数据信号」而非时钟；
- 建立「2 倍过采样 = 输出时钟频率翻倍」的直观认识，并知道输出端的 LRCKx2_O/BCKx2_O 是**在芯片内部派生**出来的，而不是外部再送一个时钟进来。

本讲承接 u2-l1 建立的顶层实例化图谱，把焦点从「模块怎么连」收窄到「时钟怎么来、怎么对齐」。

## 2. 前置知识

在读懂本讲之前，你需要先熟悉下面几个名词（u1-l1 已铺垫，这里再强调一次）：

- **MCLK（Master Clock，主时钟）**：数字音频系统里频率最高的基准时钟，其它时钟通常都由它分频得到。
- **BCK（Bit Clock，位时钟）**：串行音频里每搬移 1 个比特需要一个边沿。在 FIR_x2 中，DATA_I 虽然是 32 位并行总线，但 BCK 仍然作为「音频节拍」存在。
- **LRCK（LR Clock，声道时钟）**：也叫 WS（Word Select）。它的一个完整周期对应**一个音频样点（sample）**，所以 LRCK 的频率就等于**采样频率 fs**。
- **过采样（Oversampling）**：把采样频率提高整数倍。FIR_x2 做的是 **2 倍过采样**，即把 44.1 kHz 升到 88.2 kHz、把 48 kHz 升到 96 kHz。
- **分频比**：输出频率是输入频率的几分之一。例如 8 分频表示输出频率 = 输入频率 ÷ 8。

一个关键直觉：**仿真里时钟的绝对频率并不重要，重要的是各时钟之间的分频比值**。本讲的测试激励用一个周期仅 2 ns 的「假」MCLK（约 500 MHz），但这完全不影响结论——我们关心的是 BCK、LRCK 相对 MCLK 的比例。

## 3. 本讲源码地图

本讲主要围绕两份测试激励，并用两份设计文件佐证「过采样时钟从哪里来」。

| 文件 | 作用 |
| --- | --- |
| [07_FIR_x2/FIR_x2_TB.v](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2_TB.v) | 顶层测试激励。用计数器 `MCLK_CNT` 分频出 BCK_I/LRCK_I，并按 LRCK 节拍把 PCM 样点喂进被测设计。本讲的主战场。 |
| [01_DPRAM_CONT/DPRAM_CONT_TB.v](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/01_DPRAM_CONT/DPRAM_CONT_TB.v) | 控制器单元测试激励。用同样的计数器分频手法（`MCLK_REG`）产生 BCK_I/LRCK_I，作为「同一套时钟模型在子模块级也成立」的对照。 |
| 07_FIR_x2/FIR_x2.v（辅助） | 顶层设计。展示输出过采样时钟 `LRCKx2_O`/`BCKx2_O` 的最终赋值，以及所有子模块共用 MCLK_I 的单时钟域事实。 |
| 03_SPROM_CONT/SPROM_CONT.v（辅助） | 系数 ROM 控制器。过采样时钟 LRCKx2 的真正「出生地」——它来自系数地址的最高位。细节会在 u4-l2 详讲，本讲只点明来源。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**计数器分频时钟生成**、**单时钟域设计**、**过采样时钟概念**。

### 4.1 计数器分频时钟生成

#### 4.1.1 概念说明

数字音频的一个核心约定是「**MCLK 是一切时钟的祖先**」。在一块音频板卡上，通常只有一个晶振产生 MCLK，BCK 和 LRCK 都由 MCLK 分频而来。这样做的好处是：BCK、LRCK 天然与 MCLK 同步，不存在跨时钟域问题。

测试激励要模拟这样的音频源，最简洁的办法就是**用一个二进制计数器**：让计数器在每个 MCLK 沿加 1，那么它的每一位就是 MCLK 的某个 2 的幂次分频。FIR_x2_TB 用的正是这个手法。

#### 4.1.2 核心流程

一个自由运行的 n 位计数器，第 k 位（bit k，从 0 开始）每 \(2^k\) 个时钟周期翻转一次，因此其**完整周期**为 \(2^{k+1}\) 个时钟周期。于是第 k 位的频率为：

\[
f_{\text{bit}\,k} = \frac{f_{\text{MCLK}}}{2^{k+1}}
\]

把 FIR_x2_TB 的取位代入：

- BCK 取 `MCLK_CNT[2]`：\(f_{\text{BCK}} = f_{\text{MCLK}} / 2^{3} = f_{\text{MCLK}} / 8\)
- LRCK 取 `MCLK_CNT[8]`：\(f_{\text{LRCK}} = f_{\text{MCLK}} / 2^{9} = f_{\text{MCLK}} / 512\)

两者的比值：

\[
\frac{f_{\text{BCK}}}{f_{\text{LRCK}}} = \frac{512}{8} = 64
\]

也就是说**一个 LRCK 周期里包含 64 个 BCK 周期**——这正是 32 位立体声 I2S 的标准帧格式（每个声道 32 bit × 2 声道 = 64 bit）。而**一个 LRCK 周期包含 512 个 MCLK**，恰好对应本项目的 `FIR512` 命名（每样点 512 个 MCLK）。

#### 4.1.3 源码精读

先看主时钟怎么来。`FIR_x2_TB.v` 用一个 `always` 块每 1 ns 翻转一次 `MCLK_I`，得到周期 2 ns 的方波（注意这只是仿真激励）：

```verilog
always begin
    #1 MCLK_I <= ~MCLK_I;
end
```

这段在 [07_FIR_x2/FIR_x2_TB.v:L91-L93](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2_TB.v#L91-L93)，作用是产生自由运行的 MCLK。

接着是计数器本体。`MCLK_CNT` 被声明为 9 位寄存器（[L54](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2_TB.v#L54)），并在 MCLK 的**下降沿**自增：

```verilog
always @ (negedge MCLK_I) begin
    MCLK_CNT <= MCLK_CNT + 1'b1;
end
```

见 [07_FIR_x2/FIR_x2_TB.v:L101-L103](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2_TB.v#L101-L103)。最后用两条连续赋值把计数器的某一位直接当作 BCK/LRCK：

```verilog
assign BCK_I  = MCLK_CNT[2];
assign LRCK_I = MCLK_CNT[8];
```

见 [07_FIR_x2/FIR_x2_TB.v:L105-L106](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2_TB.v#L105-L106)。`MCLK_CNT[8]` 是这个 9 位计数器的**最高位**，所以 LRCK 恰好是 512 分频——这是计数器位宽选 9 位的根本原因。

DPRAM_CONT 的单元测试用的是同一套模型，只是计数器在**上升沿**更新、名字叫 `MCLK_REG`：

```verilog
always @ (posedge MCLK_I) begin
    MCLK_REG <= MCLK_REG + 1'b1;
end
assign BCK_I  = MCLK_REG[2];
assign LRCK_I = MCLK_REG[8];
```

见 [01_DPRAM_CONT/DPRAM_CONT_TB.v:L79-L84](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/01_DPRAM_CONT/DPRAM_CONT_TB.v#L79-L84)。两份激励的频率结论完全一致，差别只在采样沿（见 4.2）。

#### 4.1.4 代码实践

> **实践目标**：亲手用「第 k 位 = \(2^{k+1}\) 分频」公式，算出 BCK、LRCK 的分频比，并推断真实音频频率。

**操作步骤**：

1. 打开 [07_FIR_x2/FIR_x2_TB.v](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2_TB.v)，确认第 54 行 `MCLK_CNT` 是 9 位、第 105–106 行 BCK 取 bit2、LRCK 取 bit8。
2. 套用 \(f_{\text{bit}\,k} = f_{\text{MCLK}} / 2^{k+1}\)：
   - BCK：\(k=2\) → 分频比 \(2^{3} = 8\)
   - LRCK：\(k=8\) → 分频比 \(2^{9} = 512\)
3. 取 44.1 kHz 系列的标准 MCLK = **22.5792 MHz**（即 \(44100 \times 512\)），计算：
   - \(f_{\text{BCK}} = 22.5792\,\text{MHz} / 8 = 2.8224\,\text{MHz}\)
   - \(f_{\text{LRCK}} = 22.5792\,\text{MHz} / 512 = 44.1\,\text{kHz}\)

**预期结果**：BCK ≈ 2.8224 MHz，LRCK = 44.1 kHz，且二者比值为 64。这是纯算术推导，结果确定，无需「待本地验证」。

#### 4.1.5 小练习与答案

1. **练习**：如果把 `assign BCK_I = MCLK_CNT[2];` 改成 `MCLK_CNT[3]`，BCK 相对 MCLK 的分频比变成多少？
   **答**：\(2^{3+1} = 16\)，即 BCK = MCLK/16。

2. **练习**：为什么 LRCK 恰好取 `MCLK_CNT[8]` 这个最高位，而不是更低或更高？
   **答**：因为计数器只有 9 位，bit8 是最高位，512 分频正好对应「1 个音频样点 = 512 个 MCLK」（即 `FIR512`）。若要支持更长滤波器或不同采样率，需要先扩展计数器位宽。

3. **练习**：仿真里 MCLK 周期是 2 ns（约 500 MHz），这是真实音频板卡上的 MCLK 吗？
   **答**：不是。这只是仿真激励，绝对频率无意义；仿真只检验分频比值与时序关系。

### 4.2 单时钟域设计

#### 4.2.1 概念说明

「单时钟域」是指**整个设计里所有的触发器都由同一个时钟 MCLK 驱动**。FIR_x2 严格遵守这一点：BCK_I 和 LRCK_I 虽然名字里有「Clock」，但它们**不会出现在任何 `always @(posedge ...)` 的敏感列表里**，而是被当作普通的「数据信号」来用。

为什么能这么做？因为 BCK、LRCK 本来就是 MCLK 分频出来的（在 TB 里它们甚至是同一个计数器的不同位），它们天然与 MCLK 同步，因此不需要跨时钟域同步器（CDC），只需在 MCLK 域里做**边沿检测**就能安全使用。

#### 4.2.2 核心流程

把一个「慢」信号（如 LRCK，频率只有 MCLK 的 1/512）在「快」时钟域（MCLK）里安全使用，标准做法是**打一拍再比较**：

```
上升沿检测：rise = LRCK_I & ~LRCK_I_prev
下降沿检测：fall = ~LRCK_I & LRCK_I_prev
```

其中 `LRCK_I_prev` 是 LRCK_I 经过一个 MCLK 寄存器后的延迟版本。这样原本「宽宽的」LRCK 高/低电平，就被压缩成「每个 LRCK 周期仅 1 个 MCLK 宽」的单脉冲，可以用来触发「每样点执行一次」的动作（例如写入一个新的 PCM 样点）。

#### 4.2.3 源码精读

证据一：测试激励里的 BCK_I/LRCK_I 就是 MCLK 的同步分频（见 4.1.3），二者天然同步。

证据二：测试激励特意在 **MCLK 下降沿**更新计数器：

```verilog
always @ (negedge MCLK_I) begin
    MCLK_CNT <= MCLK_CNT + 1'b1;
end
```

见 [07_FIR_x2/FIR_x2_TB.v:L101-L103](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2_TB.v#L101-L103)。被测设计（DUT）内部一律在 **MCLK 上升沿**采样。让 BCK/LRCK 在下降沿变化、DUT 在上升沿采样，刚好错开半拍，**避免了仿真里的竞争冒险**。这是一个很值得学习的 testbench 习惯。（对照之下，[01_DPRAM_CONT/DPRAM_CONT_TB.v:L79-L81](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/01_DPRAM_CONT/DPRAM_CONT_TB.v#L79-L81) 用的是上升沿，结论一致但不如前者「干净」。）

证据三：在顶层 [07_FIR_x2/FIR_x2.v](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v) 中，四个子模块 `DATA_BUFFER`/`FIR_COEF`/`MULT`/`ADD` 的端口列表里都只把 `MCLK_I` 当时钟（例如 [L109](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L109)、[L125](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L125)），`BCK_I`/`LRCK_I` 只是普通输入。

证据四：边沿检测在控制器里随处可见。例如 `SPROM_CONT` 检测 LRCK 上升沿来重置系数地址：

```verilog
if (LRCK_I & ~LRCK_REG == 1'b1) begin
    CADDR_REG <= {{(ROM_ADDR_WIDTH-1){1'b0}}, 1'b1};
end
```

见 [03_SPROM_CONT/SPROM_CONT.v:L82-L84](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/03_SPROM_CONT/SPROM_CONT.v#L82-L84)。其中 `LRCK_REG` 就是 LRCK_I 打一拍后的版本，`LRCK_I & ~LRCK_REG` 即上升沿单脉冲。（注意 Verilog 中 `==` 优先级高于 `&`，所以该表达式等价于 `LRCK_I & (~LRCK_REG == 1'b1)`，对 1 比特信号而言就是 `LRCK_I & ~LRCK_REG`。）

#### 4.2.4 代码实践

> **实践目标**：在仿真与源码中确认「全设计只有一个时钟 MCLK，BCK/LRCK 是数据」。

**操作步骤**：

1. 在 [07_FIR_x2/FIR_x2.v](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v) 里逐个子模块检查 `always @ (posedge ...)` 的敏感信号，确认全部是 `MCLK_I`，没有 `posedge BCK_I` 或 `posedge LRCK_I`。
2. 按 u1-l3 的流程在 Questa 中跑通顶层仿真，展开 `u_FIR_x2` 层次，观察波形。
3. 在波形里把 `BCK_I`、`LRCK_I` 与 `MCLK_I` 对齐显示。

**需要观察的现象**：

- DUT 内部所有寄存器的跳变都发生在 `MCLK_I` 上升沿；
- `BCK_I`、`LRCK_I` 的电平变化发生在 `MCLK_I` 下降沿（因为 TB 用 negedge 更新计数器），验证了「错开半拍」的设计。

**预期结果**：波形上 DUT 内部信号全部锁步于 MCLK 上升沿，BCK/LRCK 像数据一样被采样，而非作为时钟。

> 若本地尚未配好 Questa，可先做源码阅读部分（步骤 1），仿真部分待本地验证。

#### 4.2.5 小练习与答案

1. **练习**：既然 BCK/LRCK 不当时钟，那它们在 `SPROM_CONT` 里是干什么用的？
   **答**：作为数据信号参与边沿检测。例如检测 LRCK 上升沿，用来在「每个样点开始时」把系数地址复位/重新计数。

2. **练习**：FIR_x2_TB 用 negedge 更新计数器，DPRAM_CONT_TB 用 posedge，频率结论是否相同？为什么 FIR_x2_TB 更可取？
   **答**：频率结论相同（分频比只取决于取哪一位）。FIR_x2_TB 用 negedge 更可取，因为它让 BCK/LRCK 在 MCLK 下降沿变化，与 DUT 的上升沿采样错开，避免仿真竞争。

3. **练习**：为什么 LRCK 这种慢信号必须做边沿检测，而不能直接 `if (LRCK_I)` 使用？
   **答**：`if (LRCK_I)` 在 LRCK 为高的整整 256 个 MCLK 周期内都成立，会重复触发；边沿检测把它压成单周期脉冲，才能实现「每样点只动作一次」。

### 4.3 过采样时钟概念

#### 4.3.1 概念说明

FIR_x2 的输出采样率是输入的 2 倍，所以输出端必须配套提供 **2 倍频的时钟**：`LRCKx2_O`、`BCKx2_O`。初学者容易以为「2 倍频需要外部 PLL」，但在本项目里，**这两个过采样时钟是设计内部自己派生出来的**，并且和数据一起逐级打拍，保证它们与数据同时抵达输出端口。

这背后有一个很巧妙的设计：过采样时钟并不是凭空「造」一个 2 倍频，而是**复用系数地址计数器的最高位**。系数地址在 MCLK 驱动下扫过所有抽头，其最高位天然地每个过采样样点翻转一次——于是它既是「系数寻址」的一部分，又顺带当成了「输出节拍」。细节属于 u4-l2 的多相分解，本讲只确认结论：**LRCKx2_O = 2 × LRCK_I**。

#### 4.3.2 核心流程

由 4.1 已知 \(f_{\text{LRCK}} = f_{\text{MCLK}} / 512\)，则：

\[
f_{\text{LRCKx2}} = 2 \times f_{\text{LRCK}} = \frac{f_{\text{MCLK}}}{256}
\]

也就是说，**每 256 个 MCLK 就输出一个过采样样点**（输入端是每 512 个 MCLK 来一个样点，输出端插值补出一个，所以输出密度翻倍）。

过采样时钟的传播路径（与数据并行，逐级延迟以对齐）：

```
SPROM_CONT 内部 ──(LRCKx)──> FIR_COEF ──(打拍)──> MULT ──(打拍)──> ADD ──> 顶层输出寄存 ──> LRCKx2_O
```

每一级都把 LRCKx2 寄存一拍，恰好补偿该级数据通路的流水线延迟，使时钟沿与对应数据在输出端对齐。

#### 4.3.3 源码精读

过采样时钟的「出生地」在 `SPROM_CONT`——它取系数地址寄存器的最高位作为 LRCKx：

```verilog
LRCKx_REG  <= CADDR_REG[ROM_ADDR_WIDTH-1];
...
assign LRCKx_O = LRCKx_REG;
```

见 [03_SPROM_CONT/SPROM_CONT.v:L92](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/03_SPROM_CONT/SPROM_CONT.v#L92) 与 [L98](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/03_SPROM_CONT/SPROM_CONT.v#L98)。这里 `ROM_ADDR_WIDTH` 默认为 9（即 `RADDR_WIDTH = WADDR_WIDTH + 1 = 8 + 1`），所以 `CADDR_REG[8]` 是 9 位地址的最高位，其频率为 \(f_{\text{MCLK}}/2^9\) 在「每 MCLK 自增」的高段计数下表现为 256 分频（具体推导见 u4-l2），结果正是 \(f_{\text{MCLK}}/256 = 2 f_{\text{LRCK}}\)。

在顶层，过采样时钟经 `ADD` 模块上送后，由最终输出寄存器锁存，再赋给输出端口：

```verilog
assign BCKx2_O  = (WADDR_WIDTH >= 7) ? BCKx2O_REG : MCLK_I;
assign LRCKx2_O = LRCKx2O_REG;
```

见 [07_FIR_x2/FIR_x2.v:L175-L176](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L175-L176)。`LRCKx2_O` 直接取自寄存器 `LRCKx2O_REG`（[L170](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L170) 打拍）。`BCKx2_O` 则有个分支：当 `WADDR_WIDTH >= 7`（默认 8，成立）时走派生通路 `BCKx2O_REG`，否则直接用 `MCLK_I`——这是为短滤波器配置预留的兼容路径，默认场景下用不到。

#### 4.3.4 代码实践

> **实践目标**：在仿真波形里亲眼看到 LRCKx2_O 的周期是 LRCK_I 的一半。

**操作步骤**：

1. 按 u1-l3 流程在 Questa 跑通顶层仿真（执行 [07_FIR_x2/Questa/FIR_x2.bat](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/Questa/FIR_x2.bat) 与 [run.do](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/Questa/run.do)）。
2. 在波形窗口添加顶层信号 `LRCK_I` 与 `LRCKx2_O`，再添加 `BCK_I` 与 `BCKx2_O`。
3. 用波形游标测量 `LRCK_I` 一个完整周期的时间 \(T_{\text{LRCK}}\)，以及 `LRCKx2_O` 一个完整周期的时间 \(T_{\text{LRCKx2}}\)。

**需要观察的现象**：

- \(T_{\text{LRCKx2}} \approx T_{\text{LRCK}} / 2\)，即 LRCKx2 频率是 LRCK 的 2 倍；
- 同理 \(T_{\text{BCKx2}} \approx T_{\text{BCK}} / 2\)。

**预期结果**：在仿真里 `LRCK_I` 周期 = 512 个 MCLK = 1024 ns，`LRCKx2_O` 周期 = 256 个 MCLK = 512 ns，恰好一半。

> 若本地暂无 Questa，可改做源码阅读：追踪 `LRCKx2_O` 在 [FIR_x2.v](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v) 中从 `LRCKx2O_REG ← LRCKx2O_wire ← ADD ← MULT ← FIR_COEF ← SPROM_CONT.LRCKx_O` 的逐级寄存链路，数一数共经过几级 MCLK 寄存器（仿真数值待本地验证）。

#### 4.3.5 小练习与答案

1. **练习**：用 MCLK 表示 \(f_{\text{LRCKx2}}\)。
   **答**：\(f_{\text{LRCKx2}} = f_{\text{MCLK}} / 256\)。

2. **练习**：在 22.5792 MHz MCLK 下，LRCKx2_O 是多少？
   **答**：\(22.5792\,\text{MHz} / 256 = 88.2\,\text{kHz}\)（正好是 44.1 kHz 的 2 倍）。

3. **练习**：过采样时钟 LRCKx2 在设计里**首次产生**于哪个模块、哪条语句？
   **答**：在 `SPROM_CONT` 中，由 `LRCKx_REG <= CADDR_REG[ROM_ADDR_WIDTH-1];`（系数地址最高位）产生，之后逐级寄存上送至顶层 `LRCKx2_O`。

## 5. 综合实践

把本讲三个模块串起来：**为 48 kHz 采样率场景配置 FIR_x2 的时钟模型**。

**背景**：48 kHz 系列的标准 MCLK = 24.576 MHz（因为 \(48000 \times 512 = 24{,}576{,}000\)）。仓库里也正好有对应的系数文件 `FIR512_x2_48000.hex`（见 [07_FIR_x2/FIR_x2_TB.v:L60](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2_TB.v#L60)）。

**任务**：

1. 基于本讲的分频公式，计算该场景下的：
   - \(f_{\text{BCK}}\)（BCK = MCLK/8）
   - \(f_{\text{LRCK}}\)（LRCK = MCLK/512）
   - \(f_{\text{LRCKx2}}\)（= 2 × LRCK = MCLK/256）
2. 验证算出的 LRCK 是否等于 48 kHz，LRCKx2 是否等于 96 kHz。
3. 思考：如果保持 MCLK = 24.576 MHz 不变，却想让 LRCK 变成 96 kHz（即不做过采样的「直通」），需要把 `assign LRCK_I = MCLK_CNT[?]` 里的取位改成第几位？这种改动会破坏「1 样点 = 512 MCLK = FIR512」的耦合，说说为什么不可取。

**参考答案**：

1. \(f_{\text{BCK}} = 24.576/8 = 3.072\,\text{MHz}\)；\(f_{\text{LRCK}} = 24.576/512 = 48\,\text{kHz}\)；\(f_{\text{LRCKx2}} = 24.576/256 = 96\,\text{kHz}\)。
2. LRCK = 48 kHz ✓，LRCKx2 = 96 kHz ✓，正好是 2 倍过采样。
3. 要让 LRCK = 96 kHz（MCLK/256），需取 `MCLK_CNT[7]`（\(2^{7+1}=256\)）。但这会让「每样点只剩 256 个 MCLK」，而滤波器有 512 个系数需要逐一相乘累加，MCLK 预算不够，时序无法收敛——这正是本项目坚持「1 样点 = 512 MCLK」并用 2 倍过采样在输出端补回样点密度的原因。

## 6. 本讲小结

- **分频公式**：计数器第 k 位的频率 \(f = f_{\text{MCLK}} / 2^{k+1}\)。本项目 BCK = `MCLK_CNT[2]` = MCLK/8，LRCK = `MCLK_CNT[8]` = MCLK/512。
- **9 位计数器的玄机**：bit8 是最高位，512 分频恰好对应「1 个音频样点 = 512 个 MCLK」，这是 `FIR512` 命名与时钟模型的内在耦合。
- **单时钟域**：全设计只有 MCLK 一个时钟（posedge 触发），BCK/LRCK 是数据信号，靠边沿检测（`sig & ~sig_reg`）安全使用。
- **错开半拍**：TB 在 MCLK 下降沿更新计数器，DUT 在上升沿采样，避开仿真竞争。
- **过采样时钟内部派生**：`LRCKx2_O = 2 × LRCK_I = MCLK/256`，源自 `SPROM_CONT` 的系数地址最高位，再随数据逐级寄存以保持对齐，无需外部 PLL。
- **仿真看比值不看绝对值**：2 ns 周期的 MCLK 只是激励，真正有意义的是 BCK:LRCK:MCLK = 64:512:1（即 1:8:512）的分频关系。

## 7. 下一步学习建议

- **进入数据通路（u3）**：本讲明确了「LRCK 上升沿 = 一个新样点到来」。下一讲 [u3-l2](u3-l2-dpram-ring-buffer-controller.md) 会讲 `DPRAM_CONT` 如何用这个上升沿驱动环形缓冲的写入地址，把样点逐个存进 RAM。
- **进入系数通路（u4）**：本讲点到为止的「过采样时钟来自系数地址最高位」，将在 [u4-l2](u4-l2-sprom-cont-polyphase-clock.md) 中结合多相奇偶抽头分解彻底讲透。
- **回顾顶层图谱**：建议重看 u2-l1 的实例化图谱，把本讲的时钟模型（MCLK 怎么分、LRCKx2 怎么派生）标注到那张图上，形成「时钟+数据」双视图。
