# 采样存储与三块 RAM

## 1. 本讲目标

学完本讲，你应当能够：

- 读懂 Verilog 里 `reg [W-1:0] mem [0:2047];` 这种**寄存器数组内存**的写法，并说清它的同步写、组合读时序。
- 理解**双端口 RAM**「写地址 `addr` 与读地址 `addr_r` 分离」的结构，以及为什么这种结构对本项目特别有用。
- 区分系统里的三块 RAM —— ram1（10 位采样）、ram2（21 位幅度平方）、ram3（10 位幅度），并说清它们各自在 `ADC → FFT → 平方求和 → 开方 → UART` 信号链中的位置。
- 用数学解释一个反直觉的现象：**为什么 ram2 必须宽到 21 位，而 ram3 却只要 10 位**。
- 识破本仓库最大的命名陷阱：**文件名、模块名、例化名、逻辑名四者互相错位**。

## 2. 前置知识

在进入源码前，先用大白话建立三个概念。

**(1) 寄存器数组内存（register-array memory）**

在 Verilog 里，一行

```verilog
reg [9:0] mem [0:2047];
```

声明了一个「2048 个抽屉、每个抽屉能放 10 位数据」的存储体。你可以把它想象成一排 2048 个格子，每个格子标号 0~2047，每格里放一个 0~1023 的数。给一个地址 `addr`，就能读写对应那一格。

**(2) 双端口（dual-port）**

「端口」在这里指「一套独立的地址/数据引脚」。单端口 RAM 只有一套地址线，同一时刻要么读、要么写；双端口 RAM 有两套地址线（一个写地址、一个读地址），**可以同时写一个地址、读另一个地址**。本项目里 FFT 正在往 ram2 里写第 N 个结果时，主状态机完全可以同时从 ram2 读出第 N-K 个旧结果——这就是双端口带来的自由度。

**(3) 位宽为什么会膨胀**

采样值是 10 位的有符号数。算 FFT 得到的实部、虚部也是 10 位。但当我们把实部**平方**时，10 位 × 10 位会变成 20 位；再把实部平方和虚部平方**相加**，又要多一位进位，变成 21 位。所以信号在「平方求和」这一步会「变胖」，必须用更宽的 RAM 来接住；而最后做**开方**又把数值「压回」原来的量级，所以下一块 RAM 可以再变窄。本讲的核心就是追踪这条位宽曲线。

> 本讲承接 [u1-l3 系统总体架构与数据流](u1-l3-system-architecture-and-dataflow.md)（你已经知道数据流是 `ADC→ram1→FFT→(平方+求和)→ram2→开方→ram3→UART`）和 [u2-l1 时钟生成与多时钟域](u2-l1-clock-generation-and-domains.md)（你知道三块 RAM 都跑在 200 MHz 的 `clk` 域）。

## 3. 本讲源码地图

| 文件 | 内部模块名 | 角色 | 本讲怎么看 |
|---|---|---|---|
| `verilog files/ram2.v` | `SRAM` | **ram1**，存 ADC 原始采样（10 位） | 当作「模板」精读，结构最典型 |
| `verilog files/SRAM.v` | `SRAM2` | **ram2**，存 FFT 幅度平方 `re²+im²`（21 位） | 看它和模板差在哪（位宽） |
| `verilog files/SRAM3.v` | `SRAM3` | **ram3**，存开方后的幅度（10 位） | 看它和模板几乎一模一样 |
| `verilog files/TOP.v` | `TOP` | 顶层，例化这三块 RAM 并接线 | 看例化语句，确认每块 RAM 接到信号链的哪一段 |

⚠️ **命名陷阱预警**：注意上表里「文件名」和「模块名」是错位的——文件 `ram2.v` 里写的模块叫 `SRAM`（是 ram1），文件 `SRAM.v` 里写的模块叫 `SRAM2`（是 ram2）。读代码时一律以**模块名**（`module XXX`）为准，不要被文件名误导。这一点我们在 4.1 节会再强调一次。

## 4. 核心概念与源码讲解

### 4.1 先建立全局：三块 RAM 在信号链里的分工

TOP.v 头部的算法注释用 8 步描述了整个流程，其中和存储相关的几步是：

[verilog files/TOP.v:8-8](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L8-L8) —— 第 1 步：采集一帧，存入 **ram1**（`ram_adc`）。

[verilog files/TOP.v:11-13](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L11-L13) —— 第 4 步：把 FFT 输出（复数）经平方、求和后存入 **ram2**，得到 `re²+im²`。

[verilog files/TOP.v:15-16](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L15-L16) —— 第 6 步：经 8 拍开方后存入 **ram3**，再发给 PC。

把这三块 RAM 放回数据流，就是下面这张图（方括号里是数据位宽）：

```
ADC 采样[10] ──► ram1[10] ──► FFT ──► (平方+求和)[21] ──► ram2[21] ──► 开方 ──► ram3[10] ──► UART
                 (SRAM)              re²+im² 膨胀         (SRAM2)        压回量级   (SRAM3)
```

三块 RAM 在结构上是**同一个模板的三份拷贝**，区别只在「数据位宽」。所以本讲的策略是：4.2 节把这个模板彻底讲透（用 ram1 当样本），4.3、4.4 节只看 ram2/ram3 与模板的差异，4.5 节用数学回答「位宽为什么是 10/21/10」。

### 4.2 双端口 RAM 模板精读 —— module SRAM（即 ram1）

#### 4.2.1 概念说明

`module SRAM` 是项目里最典型的存储器：它存的是 ADC 送来的 10 位原始采样，所以在 TOP 里被例化为 `ram_adc`，逻辑上叫 **ram1**。它的任务很简单——**一边按写地址把 ADC 样本逐个塞进格子，一边让别的模块按读地址把样本取走**。因为 FFT 要读它、UART 发送模块也要读它（读两次），所以它必须做成双端口、读地址与写地址独立。

注意：这个模块住在文件 `verilog files/ram2.v` 里（文件名带 "ram2"，但模块名是 `SRAM`，逻辑角色是 ram1）——这是本仓库最坑的命名错位，第一次读几乎人人踩。**永远认 `module` 关键字后面的名字。**

#### 4.2.2 核心流程

一块 SRAM 在每个时钟上升沿做三件事，彼此独立：

1. **写**：若写使能 `we` 为高，则把 `data_in` 写进 `mem[addr]` 那一格。
2. **满标志**：若写地址 `addr` 已经走到最后一个格子（2047），就把 `carry` 拉高，告诉外部「我写满了」；否则 `carry` 为低。
3. **读**：用一个**连续赋值**，把 `mem[addr_r]` 的内容直接送到 `data_out`——这是组合读，不依赖时钟。

伪代码描述：

```
每个 posedge clk：
    if (we)  mem[addr] ← data_in          // 同步写
    carry    ← (addr == 2047) ? 1 : 0      // 同步产生满标志
任何时候（组合）：
    data_out = mem[addr_r]                 // 异步读
```

关键点：写和满标志是**时序逻辑**（在 `always @(posedge clk)` 里、用非阻塞赋值），读是**组合逻辑**（`assign`）。这种「同步写 + 组合读」的结构在 Xilinx 工具里通常会被推断为**分布式 RAM（LUT RAM）**而非块 RAM，因为真正的块 RAM 要求寄存读。具体推断结果待本地综合确认。

#### 4.2.3 源码精读

先看端口声明。注意数据是 `[9:0]`，即 10 位；地址是 `[10:0]`，即 11 位（可寻址 0~2047，正好 2048 格）：

[verilog files/ram2.v:6-13](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/ram2.v#L6-L13) —— 模块 `SRAM` 的端口：`clk`、写地址 `addr`、读地址 `addr_r`、10 位数据输出 `data_out`、10 位数据输入 `data_in`、写使能 `we`、满标志 `carry`。

存储体本体——2048 格、每格 10 位：

[verilog files/ram2.v:15-15](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/ram2.v#L15-L15) —— `reg [9:0] mem [0:2047];` 声明寄存器数组内存。

时序逻辑（写 + 满标志）与组合读：

[verilog files/ram2.v:16-21](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/ram2.v#L16-L21) —— `always @(posedge clk)` 块里同时做「条件写」和「产生 carry」；块外的 `assign data_out=mem[addr_r];` 是异步读。

注意第 18 行 `if(addr==11'b11111111111)`：`11'b11111111111` 就是十进制 2047，即最后一格的地址。当写地址走到这里，`carry` 被置 1。因为 `carry` 是用非阻塞赋值（`<=`）在这个时钟沿更新的，所以它是**寄存过的电平信号**——只要 `addr` 停在 2047，`carry` 就一直为高。

再看 TOP 怎么用它。ram1 的例化如下：

[verilog files/TOP.v:128-134](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L128-L134) —— 例化 `SRAM ram_adc`：写地址接 `ADR`，写使能接 `we`，数据输入接 `adc_read`（ADC 的 10 位并行数据），数据输出接 `buffer`，读地址接 `ram_read`，满标志接 `carry`。

相关的顶层信号定义：

- [verilog files/TOP.v:39-39](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L39-L39) —— `reg [10:0] ADR`：ram1 的写地址，初始 0。
- [verilog files/TOP.v:42-42](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L42-L42) —— `reg we=1'b1`：ram1 的写使能，默认为高（上电即开始写）。
- [verilog files/TOP.v:43-43](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L43-L43) —— `wire [9:0] buffer`：ram1 的读出数据，会喂给 decoder 和发送模块。
- [verilog files/TOP.v:46-46](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L46-L46) —— `wire carry`：ram1 满标志。
- [verilog files/TOP.v:74-74](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L74-L74) —— `wire [10:0] ram_read`：ram1 的读地址总线（由 MUX 在 FFT 的 `index_in` 和发送的 `cnt_waveform` 之间切换，这就是「读两次」的实现）。

`ADR` 这个写地址由 ADC 时钟域的子状态机递增（见 [verilog files/TOP.v:474-482](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L474-L482)），而写动作本身发生在 200 MHz 的 `clk` 域——这是一个跨时钟域的细节，我们放到 [u5-l2](u5-l2-trigger-and-slope-subfsm.md) 再细讲，这里只要知道「写地址来自 ADC 域、carry 是 200 MHz 域的寄存器」即可。

主状态机在 `acq_state` 里轮询 `carry` 来判断 ram1 是否写满：

[verilog files/TOP.v:330-336](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L330-L336) —— `acq_state`：只要 `carry` 还没拉高就保持 `we=1` 继续写；一旦 `carry==1`，关闭写使能并跳到 `fft_state` 开始算 FFT。

#### 4.2.4 代码实践

**实践目标**：亲手确认「写满即停」的逻辑，以及读地址 MUX 的存在。

**操作步骤**（源码阅读型实践，无需上板）：

1. 打开 [verilog files/TOP.v:128-134](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L128-L134)，确认 ram1 的 `addr_r`（读地址）接的是 `ram_read` 而不是 `ADR`——这说明读写地址是分离的。
2. 搜索 `ram_read` 在 TOP.v 中的来源，会找到 [verilog files/TOP.v:195-198](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L195-L198) 的 `mux_ram1`，它在 `index_in`（FFT 读）和 `cnt_waveform`（发送读）之间二选一。
3. 在 [TOP.v:330-336](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L330-L336) 跟踪 `carry` 如何终结采集阶段。

**需要观察的现象**：读地址 `ram_read` 并不等于写地址 `ADR`；`carry` 在 `addr` 到达 2047 时才变 1，主状态机据此切换阶段。

**预期结果**：你能画出「ADC 域递增 ADR → ram1 在 200 MHz 域写入 → addr=2047 时 carry=1 → acq_state 退出」这条因果链。

> 若要本地验证，可在仿真里给 ram1 喂一个自增的 `addr` 和固定 `data_in`，观察 `carry` 是否恰好在 `addr=2047` 的那个上升沿之后变高（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：`reg [9:0] mem [0:2047];` 一共能存多少位数据？

**答案**：2048 格 × 10 位 = 20480 位。

**练习 2**：地址为什么是 11 位（`[10:0]`）？

**答案**：\(2^{11} = 2048\)，11 位正好编码 0~2047 共 2048 个地址，与 `mem [0:2047]` 的深度一一对应。

**练习 3**：如果删除第 18 行的 `carry` 逻辑，系统还能正常工作吗？

**答案**：不能正常结束采集。主状态机 `acq_state` 依赖 `carry==1` 才跳到 `fft_state`（见 [TOP.v:331](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L331-L331)）。没有 `carry`，机器会一直停在 `acq_state`，FFT 永远不会启动。

### 4.3 module SRAM2 —— ram2（21 位幅度平方存储）

#### 4.3.1 概念说明

`module SRAM2` 是 ram2，在 TOP 里例化为 `ram_fft_20bit`，存的是 FFT 输出经过「平方 + 求和」后的结果 `re²+im²`。它住在文件 `verilog files/SRAM.v` 里（文件名 `SRAM`、模块名 `SRAM2`、逻辑名 ram2——三者各不相同，务必小心）。它的结构与 4.2 节的模板**完全相同**，唯一的差别是数据位宽：从 10 位扩到 **21 位**。

#### 4.3.2 核心流程与源码精读

端口声明里数据是 `[20:0]`（21 位），其余引脚与模板一致：

[verilog files/SRAM.v:6-13](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/SRAM.v#L6-L13) —— 模块 `SRAM2` 的端口：21 位 `data_in`/`data_out`，地址仍为 11 位。

存储体 21 位宽：

[verilog files/SRAM.v:15-15](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/SRAM.v#L15-L15) —— `reg [20:0] mem [0:2047];`。

读写与满标志逻辑与模板逐字相同（只是位宽不同）：

[verilog files/SRAM.v:16-21](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/SRAM.v#L16-L21) —— 同步写、`carry` 满标志、组合读。

再看 TOP 的例化，理解它在信号链里的位置：

[verilog files/TOP.v:137-142](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L137-L142) —— 例化 `SRAM2 ram_fft_20bit`：写地址接 `index_out`（FFT 卸载时自动递增的输出索引），读地址接 `cnt_s`，数据输入接 `sum`（= `re²+im²`，21 位），输出接 `out_fft`，写使能接 `we2`。

注意两点：
- **`carry` 端口悬空**：例化里没有连 `.carry(...)`。因为 ram2 的「写满」时机由 FFT 的 `index_out` 计数控制（见 [TOP.v:346-354](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L346-L354) 的 `fft_write_state`），不需要 RAM 自己报满。所以模板里的 `carry` 在这里是个「用不到也无所谓」的输出。
- **数据来源链**：`sum` 来自加法器 `Sum adder`，而加法器的两个输入 `in1_s`、`in2_s` 分别是两个平方器 `Square` 的输出。相关的位宽定义见 [TOP.v:64-68](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L64-L68)（`out_fft[20:0]`、`data_send[9:0]`、`in1_s/in2_s[19:0]`、`sum[20:0]`）。

#### 4.3.3 小练习与答案

**练习**：ram2 的 `carry` 引脚在例化时悬空，这会不会导致综合报错？

**答案**：不会。`carry` 是模块的 `output`，悬空只是表示「这个输出没人用」，综合器会将其优化掉。模块内部的 `carry` 寄存器仍会按地址逻辑翻转，只是值不被外部读取。

### 4.4 module SRAM3 —— ram3（10 位幅度存储）与位宽演变数学

#### 4.4.1 概念说明

`module SRAM3` 是 ram3，在 TOP 里例化为 `ram_fft_10bit`，存的是开方后的**幅度谱** \(|X(k)|\)。它住在文件 `verilog files/SRAM3.v` 里——这一次文件名、模块名、逻辑名难得地基本对齐。结构仍是同一个模板，数据位宽回到 **10 位**。本节把它和「位宽为什么从 21 又缩回 10」的数学一起讲，因为 ram3 正是这条位宽曲线的终点。

#### 4.4.2 核心流程与源码精读

端口与模板一致（10 位数据）：

[verilog files/SRAM3.v:8-15](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/SRAM3.v#L8-L15) —— 模块 `SRAM3` 的端口。

存储体与读写逻辑（10 位）：

[verilog files/SRAM3.v:17-23](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/SRAM3.v#L17-L23) —— `reg [9:0] mem [0:2047];` 加同步写、满标志、组合读。

TOP 的例化：

[verilog files/TOP.v:145-150](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L145-L150) —— 例化 `SRAM3 ram_fft_10bit`：写地址接 `cnt_s`，读地址接 `ADR_r`，数据输入接 `square_out[9:0]`（注意只取了低 10 位！），输出接 `data_send`，写使能接 `we3`。

这里出现第二个关键的位宽截断：`square_out` 本身是 11 位（`wire [10:0] square_out`，见 [TOP.v:73-73](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L73-L73)），但写入 ram3 时只取 `square_out[9:0]`，丢弃最高位。结合 4.3 节 ram2 那里「`out_fft` 是 21 位、但开方模块只读 `out_fft[19:0]` 共 20 位」（见 [TOP.v:184-184](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L184-L184)），我们就能用数学解释位宽的整条曲线了。

#### 4.4.3 位宽演变的数学：为什么是 10 / 21 / 10

幅度谱的定义是：

\[
|X(k)| = \sqrt{\mathrm{Re}(k)^2 + \mathrm{Im}(k)^2}
\]

设 FFT 输出的实部、虚部都是 10 位有符号数（二进制补码），最大幅度约为 \(511\)。下面追踪每一步的位宽：

| 阶段 | 信号 | 位宽 | 来源（TOP.v） | 说明 |
|---|---|---|---|---|
| FFT 实部 | `xk_re` | 10 位 | [L60](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L60-L60) | 有符号补码，范围约 ±511 |
| 平方 re² | `in1_s` | 20 位 | [L66](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L66-L66) | 10 位 × 10 位 = 20 位 |
| 求和 re²+im² | `sum` | 21 位 | [L68](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L68-L68) | 两个 20 位相加，多 1 位进位 |
| ram2 存储 | `out_fft` | 21 位 | [L64](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L64-L64) | 接住 `sum` |
| 开方输入 | `out_fft[19:0]` | 20 位 | [L184](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L184-L184) | 丢弃 bit20 |
| 开方输出 | `square_out` | 11 位 | [L73](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L73-L73) | 幅度量级 |
| ram3 存储 | `square_out[9:0]` | 10 位 | [L148](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L148-L148) | 丢弃 bit10 |

**为什么 ram2 要 21 位？** 平方运算把位宽翻倍（10 → 20），求和再加一位进位（20 → 21）。最坏情况下：

\[
511^2 + 511^2 = 261121 + 261121 = 522242
\]

而 \(2^{19} = 524288\)，所以 \(522242\) 刚好放得进 20 位，第 21 位（bit20）实际上是加法器的**结构进位位**，在合法输入下恒为 0——这也解释了为什么开方模块只读 `out_fft[19:0]` 共 20 位就够（bit20 本来就是 0）。但 RAM 模块要和加法器输出对齐，所以仍声明成 21 位。

**为什么 ram3 只要 10 位？** 开方把数值「压回」原来的量级：

\[
\sqrt{522242} \approx 722.7
\]

\(722 < 1024 = 2^{10}\)，所以幅度 \(|X|\) 能放进 10 位。开方模块输出 11 位（`square_out[10:0]`）只是为了留余量，最高位 bit10 在合法范围内恒为 0，于是 ram3 直接取 `square_out[9:0]` 即可。

一句话总结这条曲线：**平方让位宽膨胀（10→20→21），开方让位宽收缩（21→10）**。ram2 站在「最胖」的位置，必须最宽；ram3 站在「瘦回来」的位置，可以和 ram1 一样窄。

#### 4.4.4 小练习与答案

**练习 1**：如果把 ram2 也做成 10 位宽，会发生什么？

**答案**：`sum` 是 21 位，写入 10 位的 RAM 会被截断，`re²+im²` 的高 11 位丢失，数值完全错误，随后的开方得到的幅度谱也就错了。ram2 必须至少 21 位才能无损接住加法器输出。

**练习 2**：ram2 存 21 位，但开方只读 20 位（`out_fft[19:0]`），丢掉的那一位会不会是有效数据？

**答案**：在合法输入下不会。如上所算，\(511^2+511^2=522242 < 2^{20}\)，最高位 bit20 恒为 0。所以丢弃它无损。但如果输入超过设计预期（例如 FFT 增益没被 `scale_sch` 压住），bit20 可能为 1，此时会被悄悄丢弃——这是一个潜在的设计边界，待本地验证。

### 4.5 carry 满标志：RAM 怎样通知系统「我写满了」

#### 4.5.1 概念说明

三块 RAM 都有 `carry` 输出，但**只有 ram1 真正用了它**。`carry` 解决的问题是：主状态机怎么知道「一帧 2048 个样本已经采完，可以进入 FFT 了？」RAM 自己最清楚自己写到哪个地址了，于是用一个 `carry` 标志在「写地址到达最后一格 2047」时拉高，主状态机轮询这个标志即可。这是一种最朴素的「握手」：存储器主动报满，控制器被动等待。

#### 4.5.2 源码精读

`carry` 的产生逻辑在三份 RAM 里完全相同：

[verilog files/SRAM.v:18-19](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/SRAM.v#L18-L19) —— `if(addr==11'b11111111111) carry<=1'b1; else carry<=1'b0;`：当写地址 `addr`（=2047）走到最后一格，`carry` 置 1。

注意 `11'b11111111111` 是 11 位全 1，即 2047，正是 `mem[0:2047]` 的最后一格。`carry` 用非阻塞赋值在 `posedge clk` 更新，所以它是**寄存过的电平**：只要 `addr` 停在 2047，`carry` 就保持高。

主状态机消费这个标志：

[verilog files/TOP.v:330-336](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L330-L336) —— `acq_state`：`carry==1` 时关掉写使能 `we`、跳到 `fft_state`；否则保持 `we=1` 继续写。

ram2、ram3 的 `carry` 在例化时都没接线（[TOP.v:137-142](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L137-L142)、[TOP.v:145-150](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L145-L150)），因为它们的「写多少」分别由 FFT 卸载计数 `index_out` 和开方流水线计数 `cnt_s` 控制，不需要 RAM 自己报满。这是「同一个模板，不同用法」的体现——模板够通用，用不到的输出悬空即可。

#### 4.5.3 小练习与答案

**练习**：`carry` 是在 `addr==2047` 的那个时钟沿**之后**才变高，还是**当**沿变高？会不会导致多写或少写一格？

**答案**：`carry` 在 `always @(posedge clk)` 内用 `<=` 赋值，是寄存器输出。当某个上升沿 `addr` 已等于 2047，`carry` 在该沿结束时更新为 1——也就是说，`mem[2047]` 的写入和 `carry` 变高发生在**同一个**时钟沿。主状态机在下一个沿才会看到 `carry==1` 并退出 `acq_state`。因此最后一格 `mem[2047]` 已经被正确写入，不会少写；退出后 `we` 关闭，也不会多写。这个时序是自洽的。

## 5. 综合实践

**任务**：完成一张「三块 RAM 全景对照表」，并用一段话解释位宽曲线。这是本讲所有知识的汇总。

**操作步骤**：

1. 打开三份源码 [ram2.v](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/ram2.v)、[SRAM.v](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/SRAM.v)、[SRAM3.v](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/SRAM3.v)，以及 TOP 的三处例化 [128-134](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L128-L134)、[137-142](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L137-L142)、[145-150](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L145-L150)。
2. 填写下面这张表（答案见本讲各节，建议先自己填再对照）：

| 逻辑名 | 例化名 | 模块名 | 所在文件 | 数据位宽 | 深度 | 信号链位置 | carry 是否使用 |
|---|---|---|---|---|---|---|---|
| ram1 | `ram_adc` | `SRAM` | `ram2.v` | 10 | 2048 | ADC 采样存储，供 FFT 与发送读 | 是 |
| ram2 | ? | ? | ? | ? | ? | ? | ? |
| ram3 | ? | ? | ? | ? | ? | ? | ? |

3. 用一段话（不超过 100 字）解释：为什么 ram2 是三块里最宽的，而 ram1 和 ram3 都是 10 位？

**预期结果**：

- ram2 行：例化名 `ram_fft_20bit`、模块名 `SRAM2`、文件 `SRAM.v`、21 位、2048 深、位于「FFT→平方求和」与「开方」之间、`carry` 不使用。
- ram3 行：例化名 `ram_fft_10bit`、模块名 `SRAM3`、文件 `SRAM3.v`、10 位、2048 深、位于「开方」与「UART 发送」之间、`carry` 不使用。
- 解释要点：平方让位宽膨胀到 21 位，所以 ram2 必须最宽；开方把幅度压回原始量级，所以 ram3 回到 10 位；ram1 存的是原始 10 位采样，自然也是 10 位。

> 如果你打算本地验证，推荐写一个只含三块 RAM 的小 testbench：用同一个自增写地址分别写 10 位、21 位、10 位的测试数据，观察三者 `carry` 是否都在地址 2047 时拉高，并检查异步读端口 `data_out` 是否在 `addr_r` 改变后立刻变化（待本地验证）。

## 6. 本讲小结

- 三块 RAM（`SRAM`/`SRAM2`/`SRAM3`）是**同一个双端口模板**的三份拷贝，结构完全一致，只在数据位宽（10/21/10）上有别。
- 模板的核心是 `reg [W-1:0] mem [0:2047];`：**同步写**（`always @(posedge clk)` + `we`）、**组合读**（`assign data_out=mem[addr_r]`）、读写地址分离的双端口。
- **命名陷阱**：文件名、模块名、例化名、逻辑名四者错位——`ram2.v` 含模块 `SRAM`（是 ram1），`SRAM.v` 含模块 `SRAM2`（是 ram2）。读代码一律认 `module` 名。
- 位宽曲线由数学决定：平方膨胀（10→20→21），开方收缩（21→10）。ram2 最宽是因为它站在平方和的位置；ram3 回到 10 位是因为开方恢复了幅度量级。
- `carry` 满标志在写地址到达 2047 时拉高，是 RAM 对主状态机的「我写满了」通知；只有 ram1 真正使用它，ram2/ram3 悬空。
- 三块 RAM 都跑在 200 MHz 的 `clk` 域，而 ram1 的写地址 `ADR` 来自 ADC 时钟域——这是一处跨时钟域细节，留待 [u5-l2](u5-l2-trigger-and-slope-subfsm.md) 详谈。

## 7. 下一步学习建议

- 想知道 ram2 里存的 `re²+im²` 是怎么算出来的？继续看 [u3-l2 幅度平方与求和](u3-l2-magnitude-square-and-sum.md)，那里精读 `Square`（乘法器）和 `Sum`（加法器）两个 IP 封装。
- 想知道 ram3 里存的幅度是怎么从 ram2 算出来的？继续看 [u3-l3 开方与流水线时序](u3-l3-square-root-and-pipeline-timing.md)，理解 `Root_square` 的 8 拍延迟。
- 想知道 ram1 的写地址 `ADR` 为什么由另一个状态机驱动、`carry` 如何跨时钟域被消费？进入专家层 [u5-l2 触发与斜率子状态机](u5-l2-trigger-and-slope-subfsm.md)。
- 建议同步阅读的源码：把 [TOP.v:127-150](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L127-L150) 这三处例化对照本讲的对照表看一遍，确认你已经能从例化语句反推出每块 RAM 的角色。
