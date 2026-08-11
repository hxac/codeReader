# 顶层模块端口与引脚定义

## 1. 本讲目标

本讲带你把 IKA32010 顶层模块的「引脚排布」彻底看懂。学完后你应当能够：

- 读懂 `module IKA32010 (...)` 端口列表里的每一行，区分它是输入还是输出、是高电平有效还是低电平有效、位宽是多少。
- 说清 `i_EMUCLK`、`i_CLKIN_PCEN`、`o_CLKOUT` 三者之间的关系——谁是主时钟、谁是时钟使能、谁是对外分频时钟。
- 掌握 `o_MEN_n` / `o_DEN_n` / `o_WE_n` / `o_DOUT_OE` 这一整套外部总线控制信号的用途，知道它们各自在哪一类总线事务中起作用。
- 看懂 `o_AOUT`、`i_DIN`、`o_DOUT` 这组地址/数据线如何在程序 ROM、数据 I/O 之间复用。

本讲只讲「引脚的含义与连线对象」，不深入指令译码和时序波形——波形细节留给 [u1-l4 时钟与周期计数器](u1-l4-clock-and-cycle-counter.md)，总线事务的逐相位时序留给 u2-l3。

## 2. 前置知识

阅读本讲前，请先确认你已经了解：

- **软核（soft core）**：用硬件描述语言（这里是 SystemVerilog）写成的、可以在 FPGA 上综合的处理器实现。它对应一块真实的芯片，本讲里就是 TI 1983 年的定点 DSP **TMS32010**。
- **端口（port）**：Verilog 模块对外暴露的信号，类似芯片的物理引脚。`input` 是模块读入的信号，`output` 是模块驱动的信号。
- **有效极性（polarity）**：一个信号在什么电平下「起作用」。高电平有效表示 1 代表有效；低电平有效表示 0 代表有效。低电平有效的信号名通常带 `_n` 后缀（n = negative/active-low）。
- **三态输出（tri-state）**：一根线既能由芯片驱动、也能由外部驱动，靠「输出使能（OE, Output Enable）」信号决定当前由谁驱动。FPGA 内部没有真正的三态线，三态只出现在对外 I/O 引脚上，靠 `o_DOUT_OE` 这类使能信号模拟。

> 如果你还没读过 [u1-l1 项目总览](u1-l1-project-overview.md)，建议先读。本讲直接承接 u1-l1 关于「端口名即文档」的命名约定。

## 3. 本讲源码地图

本讲只涉及两个文件，重点在第一个：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `src/IKA32010.sv` | 顶层模块，包含端口声明、时钟分频、总线控制器等 | 第 1–29 行端口声明；第 48–58 行时钟；第 152–254 行总线控制器 |
| `README.md` | 项目说明，给出实例化代码片段与每个信号的一句话解释 | 第 17–50 行实例化示例与引脚说明 |

记住一个关键事实：IKA32010 只有一个顶层对外接口——所有与外部程序 ROM、数据外设、时钟、复位、中断的交互，都集中在这 15 个端口上。读懂这 15 个端口，就等于看懂了 IKA32010 在系统中的「边界」。

## 4. 核心概念与源码讲解

### 4.1 顶层端口声明与命名约定

#### 4.1.1 概念说明

IKA32010 的顶层模块声明只有短短一段，但它定义了整个软核对外的全部接口。源码作者刻意采用了一套「自解释」的命名约定，让你光看端口名就能猜出信号的方向和极性，几乎不需要翻文档。

命名约定有三条规则：

1. **方向前缀**：`i_` 开头是输入（input），`o_` 开头是输出（output）。
2. **极性后缀**：名字以 `_n` 结尾表示**低电平有效**（0 = 有效）；没有 `_n` 则是高电平有效。
3. **位宽**：方括号里的数字是位宽，例如 `[15:0]` 是 16 位、`[11:0]` 是 12 位。没有方括号就是 1 位。

举例：`i_RS_n` = 输入、低电平有效（复位）；`o_DOUT[15:0]` = 输出、16 位、高电平有效（输出数据）。README 也专门强调了这一点：「The direction and the polarity of the signals are described in the port names.」（方向和极性都写在端口名里。）

#### 4.1.2 核心流程

实例化 IKA32010 的标准流程（README 给出）是：

1. 把仓库加入工程（下载或作为 submodule）。
2. 用 README 提供的 Verilog 片段实例化模块，把每个端口连到你的信号上。
3. 外部信号按端口名的方向/极性连接即可，不需要额外查表。

端口一共 15 个，可分为 5 组：

| 组别 | 端口 | 方向 |
| --- | --- | --- |
| 时钟 | `i_EMUCLK`、`i_CLKIN_PCEN`、`o_CLKOUT`、`o_CLKOUT_PCEN`、`o_CLKOUT_NCEN` | 2 入 3 出 |
| 复位 | `i_RS_n` | 1 入 |
| 外部数据总线 | `o_AOUT`、`i_DIN`、`o_DOUT`、`o_DOUT_OE` | 1 入 3 出 |
| 总线事务选通 | `o_MEN_n`、`o_DEN_n`、`o_WE_n` | 3 出 |
| 外部事件 | `i_BIO_n`、`i_INT_n` | 2 入 |

合计：6 个输入、9 个输出。

#### 4.1.3 源码精读

顶层端口声明位于文件最开头。先看完整的一段：

[src/IKA32010.sv:1-29](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1-L29) —— 整个顶层端口列表，按「时钟 / 时钟输出 / 复位 / 总线控制 / 地址数据 / 标志 / 中断」分段注释。

读端口时注意注释里的关键词，它们直接说明了每个信号的用途：

[src/IKA32010.sv:2-4](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L2-L4) —— 时钟输入：`i_EMUCLK`（emulator master clock，仿真主时钟）与 `i_CLKIN_PCEN`（CLKIN 上升沿使能）。

[src/IKA32010.sv:15-17](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L15-L17) —— 三个总线选通信号，注释直接点明各自对应哪类事务：`o_MEN_n`（external instruction read，指令读）、`o_DEN_n`（IN instruction，IN 指令读外设）、`o_WE_n`（OUT instruction，OUT 指令写外设）。

README 给出的实例化模板正好和这份端口列表一一对应，注意所有输出都留空、所有输入都接了激励信号：

[README.md:17-41](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/README.md#L17-L41) —— Verilog 实例化示例，端口顺序与源码声明完全一致。

README 还对几个容易误解的信号做了重点说明：

[README.md:45-50](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/README.md#L45-L50) —— `i_EMUCLK` 是系统时钟；`i_CLKIN_PCEN` 是 CLKIN 上升沿的（高电平有效）时钟使能；`o_CLKOUT` 是 DSP 对外的分频时钟；`i_RS_n` 是同步复位；`o_DOUT_OE` 是 FPGA 三态 I/O 驱动器的输出使能；其余信号与原芯片引脚功能相同。

#### 4.1.4 代码实践

**实践目标**：建立端口清单的全貌，验证你能独立判断每个信号的方向与极性。

**操作步骤**：

1. 打开 [src/IKA32010.sv:1-29](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1-L29)。
2. 用一张三列表格列出全部 15 个端口：列 1 端口名、列 2 方向（输入/输出）、列 3 极性（高有效/低有效）+ 位宽。
3. 用「`i_`/`o_` 判方向、`_n` 判极性、`[x:0]` 判位宽」三条规则**仅凭名字**填写，再回头与源码注释核对。

**需要观察的现象**：所有 15 个端口的命名都严格遵守三条规则，没有任何例外。

**预期结果**：你会得到 6 个输入、9 个输出；带 `_n` 的低有效信号恰好是 `i_RS_n`、`o_MEN_n`、`o_DEN_n`、`o_WE_n`、`i_BIO_n`、`i_INT_n` 共 6 个。

#### 4.1.5 小练习与答案

**练习 1**：端口名 `o_DOUT_OE` 中，`o_` 和 `_OE` 各代表什么？这个信号是高有效还是低有效？

**参考答案**：`o_` 表示输出；`_OE` 是 Output Enable（输出使能）的缩写。名字**没有** `_n` 后缀，所以是高电平有效——当它为 1 时，`o_DOUT` 引脚才真正驱动外部总线。

**练习 2**：为什么 `i_RS_n` 用低电平有效，而不是高电平有效？

**参考答案**：这是历史硬件习惯——复位信号在大量老芯片（包括原始 TMS32010）上都是低有效，这样上电时外部复位电路（如 RC 电路或复位芯片）可以方便地通过开漏/集电极开路把复位线拉低。IKA32010 沿用了原芯片的极性约定，方便直接替换。

**练习 3**：数一数顶层端口里有多少个输出是 `reg` 类型、有多少是 `wire` 类型？为什么会有这种区别？

**参考答案**：`reg` 类型输出有 `o_MEN_n`、`o_DEN_n`、`o_WE_n`、`o_DOUT`、`o_DOUT_OE`（5 个，都在 `always` 块里被赋值）；`wire` 类型输出有 `o_CLKOUT`、`o_CLKOUT_PCEN`、`o_CLKOUT_NCEN`、`o_AOUT`（4 个，用 `assign` 驱动）。区别在于驱动方式：在 `always` 块里过程性赋值的必须是 `reg`，用连续赋值 `assign` 的必须是 `wire`。

---

### 4.2 时钟与周期端口：i_EMUCLK / i_CLKIN_PCEN / o_CLKOUT 系列

#### 4.2.1 概念说明

时钟端口有 5 个，分两类：进来的 2 个（主时钟 + 时钟使能），出去的 3 个（分频时钟 + 两个相位脉冲）。它们共同回答一个问题：**外部给多快的时钟，DSP 内部按什么节奏工作，对外又提供什么时钟参考？**

三个关键概念：

- **i_EMUCLK（emulator master clock）**：仿真主时钟，也就是你系统里的「原始」时钟。IKA32010 内部的所有 `always @(posedge i_EMUCLK)` 都以它的上升沿为节拍。
- **i_CLKIN_PCEN（CLKIN positive edge enable）**：主时钟的**使能**信号（高电平有效）。它不是另一个时钟，而是一个「这一拍算不算数」的开关。只有当它在某个 EMUCLK 上升沿为高时，核心的状态才会前进。
- **o_CLKOUT 系列**：IKA32010 对外提供的分频时钟与相位标记，让外部电路知道「现在到了一个 DSP 机器周期的哪个相位」。

#### 4.2.2 核心流程

IKA32010 用一个 2 位计数器 `cyclecntr` 把 `i_EMUCLK` 四分频为一个 DSP 机器周期：

```
每一个 i_EMUCLK 上升沿（且 i_CLKIN_PCEN 为高）：
    若复位：cyclecntr = 0
    否则：  cyclecntr = (cyclecntr == 3) ? 0 : cyclecntr + 1
```

于是 `cyclecntr` 在 0 → 1 → 2 → 3 → 0 之间循环，4 个 EMUCLK 周期 = 1 个 DSP 机器周期。由此推出三个输出时钟：

- `o_CLKOUT = cyclecntr[1]`：`cyclecntr` 的最高位。它在 0,1 相位为 0、2,3 相位为 1，周期是 4 个 EMUCLK，所以频率 \(f_{\text{CLKOUT}} = f_{\text{EMUCLK}} / 4\)。
- `o_CLKOUT_PCEN`：在 `cyclecntr == 1` 且 `i_CLKIN_PCEN` 为高时拉高一个 EMUCLK 宽度，标记「相位 1」。
- `o_CLKOUT_NCEN`：在 `cyclecntr == 3` 且 `i_CLKIN_PCEN` 为高时拉高一个 EMUCLK 宽度，标记「相位 3」。

三者的关系一句话概括：**EMUCLK 是原料、CLKIN_PCEN 是闸门、CLKOUT 是产品**。`i_CLKIN_PCEN` 决定核心是否前进；`o_CLKOUT` 是对外的 /4 分频时钟；`o_CLKOUT_PCEN/NCEN` 是机器周期内的两个相位脉冲。

> 关键区别：`i_CLKIN_PCEN` 是「输入」——由你（或更上层系统）告诉 IKA32010 哪些 EMUCLK 边沿有效；`o_CLKOUT_PCEN/NCEN` 是「输出」——IKA32010 告诉外部现在到了哪个相位。输入使能与输出脉冲同名（都叫 PCEN/NCEN），容易混淆，务必分清方向。详细的波形图与相位对齐分析留到 [u1-l4](u1-l4-clock-and-cycle-counter.md)。

#### 4.2.3 源码精读

时钟输入端口声明：

[src/IKA32010.sv:2-4](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L2-L4) —— `i_EMUCLK` 与 `i_CLKIN_PCEN`，注意 `i_CLKIN_PCEN` 没有 `_n`，是高电平有效的使能。

时钟输出端口声明：

[src/IKA32010.sv:6-9](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L6-L9) —— `o_CLKOUT`、`o_CLKOUT_PCEN`、`o_CLKOUT_NCEN`，都是高有效的 `wire` 输出。

`cyclecntr` 计数器与三个分频时钟的生成逻辑：

[src/IKA32010.sv:48-58](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L48-L58) —— 注意第 50 行 `always @(posedge i_EMUCLK) if(i_CLKIN_PCEN)`：只有使能为高时计数器才前进，复位时归零；第 56–58 行用 `assign` 产生三个对外时钟。

第 60–61 行还把两个相位脉冲取了内部别名，方便其他模块引用：

[src/IKA32010.sv:60-61](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L60-L61) —— 内部信号 `cyc_ncen = o_CLKOUT_NCEN`、`cyc_pcen = o_CLKOUT_PCEN`，后续大量寄存器更新都挂在 `cyc_ncen` 上。

#### 4.2.4 代码实践

**实践目标**：在不画完整波形的前提下，验证你理解了 5 个时钟端口的角色分工。

**操作步骤**：

1. 阅读 [src/IKA32010.sv:48-58](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L48-L58)。
2. 回答：如果 `i_CLKIN_PCEN` 恒为 0，`cyclecntr` 会怎样？`o_CLKOUT` 会怎样？
3. 回答：`o_CLKOUT` 在一个 DSP 机器周期（4 个 EMUCLK）里，前几个相位是低、后几个相位是高？

**需要观察的现象**：从源码逻辑推断行为，无需运行仿真。

**预期结果**：

1. `i_CLKIN_PCEN` 恒为 0 时，`if(i_CLKIN_PCEN)` 永远不成立，`cyclecntr` 停在当前值不动，整个核心冻结；`o_CLKOUT = cyclecntr[1]` 保持不变。
2. `cyclecntr` 取值 0,1 时 `cyclecntr[1]=0`（低），取值 2,3 时 `cyclecntr[1]=1`（高）。所以 `o_CLKOUT` 在前两相位（0,1）为低、后两相位（2,3）为高。完整波形待本地验证（见 u1-l4）。

#### 4.2.5 小练习与答案

**练习 1**：`i_EMUCLK` 与 `i_CLKIN_PCEN` 哪个才是「真正」的时钟？为什么需要两个？

**参考答案**：`i_EMUCLK` 是真正的时钟（所有 `always @(posedge i_EMUCLK)` 都挂在它上面）。`i_CLKIN_PCEN` 只是时钟使能，不是时钟。需要两个的原因是：当系统中 EMUCLK 的频率高于 DSP 实际需要的工作频率时，可以用 `i_CLKIN_PCEN` 标记「哪些 EMUCLK 边沿对应真实的 DSP 节拍」，从而让 IKA32010 与更高频的系统时钟域共存，而不必真的生成一个更慢的时钟。

**练习 2**：`o_CLKOUT_PCEN` 和 `o_CLKOUT_NCEN` 的脉冲分别在哪个 `cyclecntr` 取值时出现？它们持续几个 EMUCLK？

**参考答案**：`o_CLKOUT_PCEN` 在 `cyclecntr == 1` 时为高，`o_CLKOUT_NCEN` 在 `cyclecntr == 3` 时为高。因为 `cyclecntr` 每个值只占一个 EMUCLK，所以两个脉冲各持续 1 个 EMUCLK 宽度（且要求该拍 `i_CLKIN_PCEN` 为高）。

**练习 3**：`f_CLKOUT = f_EMUCLK / 4` 这个结论是从哪一行代码得出的？

**参考答案**：从 [src/IKA32010.sv:56](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L56) `assign o_CLKOUT = cyclecntr[1];` 得出。`cyclecntr` 是 2 位计数器、4 个值循环一次（4 个 EMUCLK），而 `cyclecntr[1]` 在一半时间（2 个 EMUCLK）为高、一半时间为低，所以 `o_CLKOUT` 的完整周期是 4 个 EMUCLK，即频率为 EMUCLK 的 1/4。

---

### 4.3 外部总线端口：地址 / 数据 / 三态 / 选通

#### 4.3.1 概念说明

这是本讲信息量最大的一组端口，因为 IKA32010 没有内置程序 ROM，所有指令和数据外设都挂在一条**外部总线**上。这条总线由 7 个端口共同构成：

| 端口 | 方向 / 位宽 | 角色 |
| --- | --- | --- |
| `o_AOUT[11:0]` | 输出 12 位 | 地址线——告诉外部「我要读/写哪个地址」 |
| `i_DIN[15:0]` | 输入 16 位 | 读数据线——外部把数据送进来 |
| `o_DOUT[15:0]` | 输出 16 位 | 写数据线——IKA32010 把数据送出去 |
| `o_DOUT_OE` | 输出 1 位 | `o_DOUT` 的输出使能（三态控制） |
| `o_MEN_n` | 输出 1 位 | Memory Enable——指令/表读选通（低有效） |
| `o_DEN_n` | 输出 1 位 | Data Enable——IN 指令读外设选通（低有效） |
| `o_WE_n` | 输出 1 位 | Write Enable——OUT/TBLW 写选通（低有效） |

注意 `i_DIN` 与 `o_DOUT` 是**两条独立的 16 位线**，不是双向总线。在 FPGA 上，「双向」是靠 `o_DOUT_OE` 控制三态、把 `o_DOUT` 和 `i_DIN` 合并到同一个外部引脚来模拟的——这一点 README 明确说明：`o_DOUT_OE` 是「FPGA 三态 I/O 驱动器的输出使能」。

三个选通信号 `o_MEN_n` / `o_DEN_n` / `o_WE_n` 决定了当前这一拍 `i_DIN` / `o_AOUT` 上发生的是哪一类事务。源码用一个 3 位寄存器 `busctrl_req` 区分 6 种事务：

| `busctrl_req` 值 | 事务类型 | 含义 |
| --- | --- | --- |
| 0 | stop（无事务） | 总线空闲，所有选通无效 |
| 1 | instruction read（指令读） | 从程序 ROM 取指令字，靠 `o_MEN_n` 选通 |
| 2 | table read（表读） | TBLR 指令读程序区数据，靠 `o_MEN_n` 选通 |
| 3 | table write（表写） | TBLW 指令写程序区，靠 `o_WE_n` 选通 |
| 4 | IN（输入） | IN 指令从外设读数据，靠 `o_DEN_n` 选通 |
| 5 | OUT（输出） | OUT 指令向外设写数据，靠 `o_WE_n` 选通 |

#### 4.3.2 核心流程

一次外部总线事务的通用时序骨架（一个 DSP 机器周期 = 4 个 EMUCLK 相位）：

```
相位 0~2：拉低对应的选通信号（o_MEN_n / o_DEN_n / o_WE_n 之一）
         地址 o_AOUT 给出目标地址
         （若是写事务：o_DOUT_OE=1，o_DOUT 给出数据）
相位 3：  拉高选通信号，结束本次事务
         （若是读事务：在相位 3 把 i_DIN 锁存进内部 inlatch）
```

关键细节：

- **`o_AOUT` 的双重身份**：它由一个 MUX 选择——事务类型决定它输出什么。当 `busctrl_mode[3] == 0` 时，`o_AOUT = if_pc`（程序计数器，用于取指令/表读写，寻址程序 ROM）；当 `busctrl_mode[3] == 1` 时，`o_AOUT = {9'b0, if_opcodereg[10:8]}`（即 PA 端口地址 PA0/PA1/PA2，用于 IN/OUT 寻址外设）。换句话说，**同一条 12 位地址线既寻址程序 ROM，又寻址外设端口**，靠事务类型切换。
- **读数据的来源**：`i_DIN` 在指令读/表读时来自程序 ROM，在 IN 时来自外设。外部电路根据哪个选通（`o_MEN_n` 还是 `o_DEN_n`）有效，决定把哪一路数据放到 `i_DIN` 上——这就是总线复用（bus mux）。

> 完整的「每种事务 × 每个相位」的电平时序表是 u2-l3 的核心内容。本讲只要求你建立「选通信号决定事务类型」的总印象。

#### 4.3.3 源码精读

外部数据总线端口声明：

[src/IKA32010.sv:14-22](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L14-L22) —— 三个选通信号 + 地址/数据线。注意三个选通都是 `reg`（在 `always` 块里赋值），而 `o_AOUT` 是 `wire`（用 `assign`）。

`o_AOUT` 与地址 MUX 的核心逻辑：

[src/IKA32010.sv:152-164](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L152-L164) —— 第 157 行 `assign o_AOUT = busctrl_addr;`；第 159–164 行的 MUX 根据 `busctrl_mode[3]` 在「程序计数器 `if_pc`」与「PA 端口地址」之间选择，这就是 `o_AOUT` 双重身份的来源。

6 种事务类型的定义注释：

[src/IKA32010.sv:166-169](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L166-L169) —— `busctrl_req` 的 0~5 分别对应 stop / instruction read / table read / table write / IN / OUT。

总线控制器的相位时序主块（这是选通信号真正被驱动的地方）：

[src/IKA32010.sv:176-254](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L176-L254) —— 按 `busctrl_mode[2:0]` 分支，每个分支内用 `case(cyclecntr)` 给出 4 个相位上各选通信号的电平。

挑两个最具代表性的分支细看。「指令读」事务（`busctrl_req == 1`）：`o_MEN_n` 在相位 0、1、2 为低、相位 3 为高，其余选通恒高：

[src/IKA32010.sv:200-208](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L200-L208) —— 指令读时只有 `o_MEN_n` 被拉低，地址线输出 PC，程序 ROM 据此把指令字送上 `i_DIN`。

「OUT 输出」事务（`busctrl_req == 5`）：`o_DOUT_OE` 在相位 1、2 拉高（驱动 `o_DOUT`），`o_WE_n` 仅在相位 2 拉低（写脉冲），地址线输出 PA：

[src/IKA32010.sv:243-252](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L243-L252) —— 注意第 247–248 行在相位 1 把 `reg_wrbus` 装入 `o_DOUT`，第 249 行相位 2 才拉低 `o_WE_n`，形成「先驱动数据、后给写脉冲」的标准写时序。

#### 4.3.4 代码实践

**实践目标**：亲手从源码读出「指令读」与「OUT 输出」两种事务下选通信号的粗略电平，建立时序直觉。

**操作步骤**：

1. 打开 [src/IKA32010.sv:200-208](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L200-L208)（指令读分支）。
2. 填一张小表：相位 0~3 中，`o_MEN_n` / `o_DEN_n` / `o_WE_n` / `o_DOUT_OE` 各是 0 还是 1？
3. 再打开 [src/IKA32010.sv:243-252](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L243-L252)（OUT 分支），同样填一张表。
4. 对比两张表，找出「读事务」与「写事务」在选通信号使用上的根本区别。

**需要观察的现象**：从源码 `case(cyclecntr)` 分支直接读取电平，无需仿真。

**预期结果**：

- 指令读：只有 `o_MEN_n` 在相位 0–2 为 0，其余全程为 1；`o_DOUT_OE` 全程为 0（不驱动写线）。
- OUT：`o_DOUT_OE` 在相位 1–2 为 1（驱动写线）、`o_WE_n` 仅相位 2 为 0；`o_MEN_n` / `o_DEN_n` 全程为 1。
- 根本区别：读事务靠拉低 `o_MEN_n`/`o_DEN_n` 让外部送数进来（`i_DIN`），写事务靠拉高 `o_DOUT_OE` + 拉低 `o_WE_n` 把数送出去（`o_DOUT`）。完整的 6 种事务 × 4 相位时序表留待 u2-l3。

#### 4.3.5 小练习与答案

**练习 1**：`o_AOUT` 是 12 位，程序 ROM 用它寻址；但 IN/OUT 指令寻址外设时，真正有效的地址位只有几位？为什么？

**参考答案**：只有低 3 位有效。因为地址 MUX 在 PA 模式下输出的是 `{9'b0, if_opcodereg[10:8]}`——高 9 位恒为 0，只有最低 3 位（来自指令字的 10:8 位）承载真实的端口地址 PA0/PA1/PA2（参见 [src/IKA32010.sv:159-164](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L159-L164)）。

**练习 2**：为什么 `i_DIN` 和 `o_DOUT` 不合并成一条双向数据线？

**参考答案**：模块内部把它们实现为两条独立的单向线，更清晰、便于综合。真正的「双向」只发生在 FPGA 对外的物理引脚上：把 `o_DOUT` 和 `i_DIN` 接到同一个外部引脚，用 `o_DOUT_OE` 控制该引脚何时由 IKA32010 驱动、何时浮空（让外部驱动）。这是 FPGA 设计的常规做法。

**练习 3**：在一次 OUT 事务中，为什么 `o_DOUT_OE` 在相位 1 就拉高、而 `o_WE_n` 要等到相位 2 才拉低？

**参考答案**：为了满足写时序的建立/保持要求。先在相位 1 拉高 `o_DOUT_OE` 并把数据放到 `o_DOUT` 上（数据提前就位，给外部留建立时间），再在相位 2 用 `o_WE_n` 的下降沿触发外部锁存。如果数据和写脉冲同时出现，外部可能采到不稳定的数据。

---

### 4.4 复位与外部事件端口：i_RS_n / i_BIO_n / i_INT_n

#### 4.4.1 概念说明

这一组的 3 个端口都是输入，负责「外部告诉 IKA32010 发生了什么」：

- **i_RS_n（reset，低有效）**：同步复位。为 0 时核心回到初始状态——`cyclecntr` 清零、程序计数器 PC 清零、各寄存器恢复初值。它是 IKA32010 唯一的复位手段。
- **i_BIO_n（branch on I/O，低有效）**：配合 `BIOZ` 指令使用。当 `i_BIO_n` 为低时，`BIOZ` 指令会跳转（类似「外部条件成立则分支」）。它是一个由外部 I/O 状态驱动的条件输入。
- **i_INT_n（interrupt，低有效）**：外部中断请求。IKA32010 在 `i_INT_n` 上检测到下降沿且中断未被屏蔽时，会保存现场并跳到中断向量地址 `0x002`。

三者的共同点：都是**低电平有效**（名字带 `_n`），都是输入。区别在于触发方式和用途：复位是电平触发（一直为低就持续复位），BIO 是电平采样（由指令读取），中断是**边沿触发**（检测下降沿）。

#### 4.4.2 核心流程

- **复位流程**：`i_RS_n == 0` 时，`cyclecntr` 在下一个有效 EMUCLK 边沿清零；PC 被置为 `0x000`；中断屏蔽位 `reg_intm` 被置 1（复位后默认关中断）；辅助寄存器等也回到初值。复位释放（`i_RS_n` 回到 1）后，从地址 0 开始执行。
- **BIO 采样流程**：`i_BIO_n` 在每个 `cyc_pcen`（相位 1 脉冲）被采样进内部寄存器 `bio_n`，供 `BIOZ` 指令判断。
- **中断流程**：`i_INT_n` 经过三级同步寄存器消除亚稳态，再做下降沿检测；当检测到下降沿、且 `reg_intm == 0`（中断使能）时，产生内部中断请求，最终跳到 `0x002`。

> 中断的三级同步、下降沿检测、IACK 应答等细节属于 u3-l3 的内容，本讲只需知道「`i_INT_n` 低有效、下降沿触发、向量地址 0x002」。

#### 4.4.3 源码精读

复位与外部事件端口声明：

[src/IKA32010.sv:11-12](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L11-L12) —— `i_RS_n`（chip reset）。
[src/IKA32010.sv:24-25](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L24-L25) —— `i_BIO_n`（flag）。
[src/IKA32010.sv:27-28](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L27-L28) —— `i_INT_n`（interrupt）。

复位对 `cyclecntr` 的影响：

[src/IKA32010.sv:50-53](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L50-L53) —— 第 51 行 `if(!i_RS_n) cyclecntr <= 2'd0;`，复位时计数器归零。

复位对程序计数器 PC 的影响：

[src/IKA32010.sv:102-104](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L102-L104) —— 第 103 行 `if(!i_RS_n) if_pc <= 12'h000;`，复位时 PC 指向地址 0。

`i_BIO_n` 的采样逻辑：

[src/IKA32010.sv:69-73](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L69-L73) —— 内部寄存器 `bio_n` 在每个 `cyc_pcen`（相位 1）锁存 `i_BIO_n` 的值，注释里的问号「sampled at every positive edge?」说明作者自己也留了一处待确认的标注。

复位对中断屏蔽位的影响：

[src/IKA32010.sv:274-277](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L274-L277) —— `reg_intm`（中断屏蔽，1=关中断）复位时被置 1，即复位后默认**禁止**中断。

#### 4.4.4 代码实践

**实践目标**：从源码系统梳理「复位到底复位了哪些东西」，建立对 `i_RS_n` 作用范围的完整认识。

**操作步骤**：

1. 在 [src/IKA32010.sv](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv) 中搜索所有 `if(!i_RS_n)` 出现的位置（可借助编辑器的查找功能）。
2. 对每处出现，记录它复位了哪个寄存器、复位成什么值。
3. 特别留意 [src/IKA32010.sv:262-263](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L262-L263) 的注释：「alu overflow mode bit, RESET will not clear this bit!!!」——找出哪个寄存器**不**受复位影响。

**需要观察的现象**：纯源码阅读，统计复位分支覆盖的寄存器集合。

**预期结果**：复位至少覆盖 `cyclecntr`（→0）、`if_pc`（→0）、`reg_intm`（→1）、`reg_arp`（→0）、`reg_ar[0:1]`（→0）等；而 `reg_ovm`（溢出饱和模式位）复位时**保持原值**，源码注释明确警告这一点。完整清单待本地验证（取决于你搜索到的全部 `if(!i_RS_n)` 分支）。

#### 4.4.5 小练习与答案

**练习 1**：`i_RS_n`、`i_BIO_n`、`i_INT_n` 三个输入中，哪个是「电平触发」、哪个是「边沿触发」？

**参考答案**：`i_RS_n`（复位）和 `i_BIO_n` 都是**电平触发/电平采样**——复位只要为低就持续生效，BIO 由指令在某个时刻采样其电平。`i_INT_n` 是**边沿触发**——IKA32010 检测它的下降沿来发起中断请求，而不是看它的持续电平。（边沿检测的细节见 u3-l3。）

**练习 2**：为什么复位后 `reg_intm` 被置成 1（关中断）而不是 0？

**参考答案**：这是一种安全默认。复位后系统状态刚初始化、可能尚未准备好处理中断（中断服务例程、栈等还没就绪），所以先默认关闭中断，由初始化代码在合适的时机用 `EINT` 指令显式打开，避免复位后立刻被一个未准备好的中断打断。

**练习 3**：`i_BIO_n` 的采样信号是 `cyc_pcen`（相位 1），而不是每个 EMUCLK 都采样。这样做有什么好处？

**参考答案**：`cyc_pcen` 是一个机器周期一次的相位脉冲，按机器周期采样可以避免在单个 DSP 周期内多次采样到不同的值、也能让采样的 BIO 状态与指令执行节奏对齐（一条指令占用整数个机器周期）。同时这相当于对异步的 `i_BIO_n` 做了一次同步采样，降低亚稳态风险。

---

## 5. 综合实践

### 实践任务：绘制 IKA32010 与外部程序 ROM、数据 I/O 端口的连线示意图

本任务把本讲全部 15 个端口串起来，画一张系统级连线图。这是把「引脚定义」转化为「实际能用」的关键一步。

**实践目标**：能用一张图说清 IKA32010 在系统里如何与程序 ROM、数据外设、时钟源、复位/中断源相连。

**操作步骤**：

1. 在纸或绘图工具上画三个方框：中央 `IKA32010`、左侧「程序 ROM」（存放指令字）、右侧「数据 I/O 外设」（IN/OUT 端口）。
2. 按下表把每个端口连到对应方框，并标注有效极性（低有效信号加个小圈或写 `_n`）：

   | IKA32010 端口 | 连接到 | 说明 |
   | --- | --- | --- |
   | `i_EMUCLK` | 时钟源 | 系统主时钟 |
   | `i_CLKIN_PCEN` | 时钟使能逻辑（或恒接 1） | 高有效使能 |
   | `o_CLKOUT` / `o_CLKOUT_PCEN` / `o_CLKOUT_NCEN` | 外部时序参考（可悬空） | /4 分频时钟与相位脉冲 |
   | `i_RS_n` | 复位电路 | 低有效复位 |
   | `o_AOUT[11:0]` | 程序 ROM 地址线 + 外设地址译码 | 寻址程序 ROM（取指/表读写）或 PA 端口（IN/OUT） |
   | `i_DIN[15:0]` | 程序 ROM 数据输出 **和** 外设数据输出（经 MUX） | 受 `o_MEN_n` / `o_DEN_n` 选择 |
   | `o_DOUT[15:0]` + `o_DOUT_OE` | 外设数据输入（三态） | OUT/TBLW 写数据 |
   | `o_MEN_n` | 程序 ROM 输出使能 | 低有效，指令读/表读 |
   | `o_DEN_n` | 外设读选通 | 低有效，IN 指令 |
   | `o_WE_n` | 外设/ROM 写选通 | 低有效，OUT/TBLW 指令 |
   | `i_BIO_n` | 外部 I/O 状态信号（可接恒高） | 低有效，配合 BIOZ |
   | `i_INT_n` | 外部中断源 | 低有效，下降沿触发 |

3. 在程序 ROM 与外设输出之间画一个 2 选 1 MUX，选择端由 `o_MEN_n` / `o_DEN_n` 控制，输出接到 `i_DIN`——这就是「总线复用」的体现。
4. 对照 [README.md:17-41](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/README.md#L17-L41) 的实例化模板，检查你的连线是否覆盖了模板里出现的每一个端口。

**需要观察的现象**：绘图完成后，回头检查——每个输入端口都应有来源、每个输出端口都应有去向，没有悬空的关键信号。

**预期结果**：得到一张清晰的系统框图。你会发现 IKA32010 的外部连线可以归为四股：①时钟与复位（`i_EMUCLK`/`i_CLKIN_PCEN`/`i_RS_n`）；②程序 ROM 访问（`o_AOUT`+`o_MEN_n`+`i_DIN`）；③外设访问（`o_AOUT` 低 3 位+`o_DEN_n`/`o_WE_n`+`i_DIN`/`o_DOUT`+`o_DOUT_OE`）；④异步事件（`i_BIO_n`/`i_INT_n`）。这份图也将是你后续阅读 [u1-l5 仿真与 testbench](u1-l5-simulation-and-testbench.md) 时理解 testbench 连线的直接参考。

> 提示：仓库里的 [src/IKA32010_tb.v](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_tb.v) 就是一份现成的「连线范例」——它的 `assign RDBUS = (DEN_n) ? ... : ...` 与 `assign RDBUS = (MEN_n) ? ... : ...` 正是用 `o_MEN_n` / `o_DEN_n` 选择 `i_DIN` 数据源的真实例子，画完图后可以对照检验你的理解。

## 6. 本讲小结

- IKA32010 顶层共 **15 个端口**（6 入 9 出），命名严格遵循「`i_`/`o_` 判方向、`_n` 判低有效、`[x:0]` 判位宽」三条规则，端口名本身即文档。
- **时钟三件套**：`i_EMUCLK` 是主时钟、`i_CLKIN_PCEN` 是高有效时钟使能、`o_CLKOUT = cyclecntr[1]` 是 /4 分频对外时钟，另加 `o_CLKOUT_PCEN/NCEN` 两个相位脉冲。
- **外部总线**由 `o_AOUT`（12 位地址，复用为 PC 或 PA）、`i_DIN`（读数据）、`o_DOUT`+`o_DOUT_OE`（写数据 + 三态使能）构成；`o_MEN_n`/`o_DEN_n`/`o_WE_n` 三个低有效选通区分指令读/IN/OUT 等事务类型。
- `o_AOUT` 有**双重身份**：取指/表读写时输出程序计数器 `if_pc`，IN/OUT 时输出 PA 端口地址（仅低 3 位有效），由 `busctrl_mode[3]` 切换。
- **复位 `i_RS_n` 低有效**，复位时 `cyclecntr`、PC、`reg_intm`（置 1，默认关中断）等清零；但 `reg_ovm` 不受复位影响。
- **外部事件**：`i_BIO_n` 配合 `BIOZ` 指令、按 `cyc_pcen` 周期采样；`i_INT_n` 下降沿触发中断、向量地址 `0x002`。

## 7. 下一步学习建议

- 想看清 `o_CLKOUT` / `o_CLKOUT_PCEN` / `o_CLKOUT_NCEN` 在一个机器周期里的真实波形与相位对齐？继续读 **[u1-l4 时钟分频与周期计数器](u1-l4-clock-and-cycle-counter.md)**。
- 想看这些端口如何被一个 testbench 实际驱动（时钟生成、复位/中断激励、`$readmemh` 加载 ROM、`o_MEN_n`/`o_DEN_n` 选择数据源）？继续读 **[u1-l5 仿真与 testbench 入门](u1-l5-simulation-and-testbench.md)**。
- 想深入 6 种总线事务在 4 个相位上的完整电平时序表？那是 **u2-l3 外部总线控制器 Bus Controller** 的主题，建议在学完 u1 全部讲义后再进入。
- 课外延伸：对照 [README.md:57-59](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/README.md#L57-L59) 的 FPGA 资源占用数据，思考「15 个端口 + 275 个寄存器 + BRAM/DSP 块」如何在真实 FPGA 上落地。
