# 系统总体架构与数据流

> 本讲是入门单元（Unit 1）的第三篇。前两篇已经让我们知道了「项目是什么」和「仓库怎么用」。这一篇我们第一次真正打开顶层文件 `TOP.v`，从整体上画出系统是怎么干活的——数据从 ADC 进来，最后怎么变成电脑屏幕上的波形和频谱。

## 1. 本讲目标

学完本讲，你应当能够：

1. 读懂 `TOP.v` 头部注释里描述的 **8 步采集/处理流程**，并能用自己的话复述。
2. 从 `TOP.v` 的一堆「模块例化语句」里，识别出系统有哪些功能模块、它们彼此怎么连。
3. 区分 `ram1 / ram2 / ram3` 三块存储各自存的是什么、在数据流的哪一环。
4. 用一张框图把 `ADC → ram1 → FFT → (平方+求和) → ram2 → 开方 → ram3 → UART` 这条主线画出来，并标出每一步的模块名与时钟域。

本讲**只看全局**，不钻进任何一个模块的内部细节（那些留给进阶单元）。目标是建立一张「地图」，后续每一篇讲义都是在这张地图上放大某一块。

## 2. 前置知识

### 2.1 承接前两讲

- **u1-l1** 告诉我们：这是一个跑在 Nexys 4 DDR（Xilinx Artix-7）上的高速数据采集系统，三大功能（示波器/心率/GSR）共用同一条数字骨架：传感器 → 模拟前端板 → ADC（AD9215） → FPGA → USB/串口桥 → LabVIEW GUI。示波器的信号处理（采样→FFT→幅度→串口）**全部在 FPGA 内部实现**，这是后续手册的主线。
- **u1-l2** 告诉我们：顶层模块是 [`verilog files/TOP.v`](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v) 里的 `TOP`；这是一个 **Verilog + VHDL 混合语言**工程；并且**文件名经常和模块名不一致**（例如 `Radical.v` 里的模块其实叫 `Root_square`）。本讲引用代码时，一律以「模块名」为准。

### 2.2 本讲需要的几个新概念

| 概念 | 一句话解释 |
| --- | --- |
| **双端口 RAM** | 一块存储器同时有「写端口」和「读端口」，可以一边写入新数据、一边读出旧数据，互不干扰。本项目的三块 RAM 都是这种结构。 |
| **FFT（快速傅里叶变换）** | 把一段「随时间变化的波形」转换成「随频率分布的频谱」。输入是时域采样点，输出是一组**复数**，每个复数描述某一个频率分量的强度与相位。 |
| **复数 / 实部 / 虚部** | FFT 的每一个输出可以写成 \( X = \text{Re} + j\cdot\text{Im} \)，其中 Re 是实部、Im 是虚部。 |
| **幅度（magnitude）** | 复数 \(X\) 的「大小」定义为 \( \vert X\vert = \sqrt{\text{Re}^2 + \text{Im}^2} \)。频谱图画的通常就是每个频率分量的幅度。 |
| **时钟域（clock domain）** | 由同一根时钟信号驱动的所有电路属于一个「时钟域」。不同时钟域之间交换数据需要特别小心（跨时钟域）。本系统有好几路不同频率的时钟。 |

> 关键直觉：为什么要 `平方 + 求和 + 开方` 这么麻烦？因为 FFT 吐出来的是复数（Re, Im），而我们要画的是「幅度」频谱。把幅度公式 \( \sqrt{\text{Re}^2+\text{Im}^2} \) 拆开：先用两个乘法器算 \( \text{Re}^2 \)、\( \text{Im}^2 \)，再用加法器算 \( \text{Re}^2+\text{Im}^2 \)，最后做一次开方。这就是 `ram2`（存平方和）和 `ram3`（存开方后的幅度）存在的根本原因。

## 3. 本讲源码地图

本讲只看**一个文件**，但这个文件「例化」（调用）了系统里几乎所有模块。所以本讲的「源码地图」其实是「`TOP.v` 里有哪些例化」。

| 例化语句里的名字 | 模块名 | 源文件 | 在数据流里的角色 |
| --- | --- | --- | --- |
| `clk_mult` | `pll_loop` | `verilog files/pll_loop.v` | 时钟发生器，分发 4 路时钟 |
| `receiver` | `serial_rx` | `vhdl files/serial_rx.vhd` | UART 接收：PC → FPGA |
| `transmitter` | `serialt` | `vhdl files/serialt.vhd` | UART 发送：FPGA → PC |
| `adc_conf` | `read_adc` | `vhdl files/custom_adc_ad9215.vhd` | 生成 ADC 时钟、读 ADC 数据 |
| `ram_adc` | `SRAM` | `verilog files/ram2.v` ⚠️ | **ram1**：存 ADC 原始采样 |
| `ram_fft_20bit` | `SRAM2` | `verilog files/SRAM.v` ⚠️ | **ram2**：存 FFT 平方和（21 位） |
| `ram_fft_10bit` | `SRAM3` | `verilog files/SRAM3.v` | **ram3**：存开方后的幅度（10 位） |
| `FFT` | `Fourier` | `verilog files/Fourier.v` | FFT 核封装 |
| `sq_real` / `sq_im` | `Square` | `verilog files/Square.v` | 乘法器：算 Re² / Im² |
| `adder` | `Sum` | `verilog files/Sum.v` | 加法器：Re² + Im² |
| `root_square` | `Root_square` | `verilog files/Radical.v` ⚠️ | 开方模块 |
| `dec` | `decoder` | `verilog files/decoder.v` | 编码转换：喂给 FFT 前的预处理 |
| `mux_ram1` / `mux_ram3` | `MUX` | `verilog files/MUX.v` | 读地址二选一开关 |
| `ADC_mux` | `ADC_clock_mux` | `verilog files/ADC_clock_mux.v` | ADC 时钟二选一 |
| `t1` | `Transcoder` | `verilog files/Transcodor.v` ⚠️ | 时基编码 → 分频值 |

> ⚠️ 标记的四行正是 u1-l2 提醒过的「文件名 ≠ 模块名」陷阱：**`ram2.v` 里写的模块叫 `SRAM`，而 `SRAM.v` 里写的模块叫 `SRAM2`**，文件名和内容是「交叉」的；`Radical.v` 里是 `Root_square`、`Transcodor.v` 里是 `Transcoder`。读代码时认模块名，别认文件名。

## 4. 核心概念与源码讲解

本讲拆成两个最小模块：
- **4.1 数据流 8 步算法**：先读作者写在文件头的「说明书」。
- **4.2 模块例化清单与互连**：再用例化语句把说明书翻译成具体模块和连线。

### 4.1 数据流 8 步算法

#### 4.1.1 概念说明

`TOP.v` 文件最开头，作者 Niculescu Vlad 用一段注释把整套算法「剧透」了一遍。这是一份非常宝贵的「全局说明书」——它告诉我们这个系统**从头到尾在做什么**，以及**为什么中间要插入这些步骤**。

读懂这段注释的意义在于：以后再看到任何具体模块，你都能立刻把它「放回」这张大图里的正确位置，而不会迷失在细节里。

#### 4.1.2 核心流程

把作者注释里的 8 步整理成下面这条主线（括号里是本讲给的关键词，方便和后面的例化对应）：

```
步骤 1  采集一帧，存入 ram1            （ADC → ram_adc / SRAM）
步骤 2  把 ram1 的样本喂给 FFT          （ram1 → Fourier）
步骤 3  计算 FFT                        （Fourier 核内部）
步骤 4  卸载 FFT 结果：先平方再求和，     （Square × 2 + Sum → ram2）
        得到 Re²+Im²，存入 ram2
步骤 5  开方模块不是 0 延迟，需要 8 个时钟周期
步骤 6  开方后存入 ram3，从这里发给 PC    （Root_square → ram3 → 串口）
        （波形数据 和 FFT 数据 都从这里发）
步骤 7  LabVIEW 在 PC 上把波形画出来
步骤 8  FPGA 里的另一次采集，只在 PC 请求时才启动
```

注意步骤 4 是整条链路的「关键设计点」：作者没有让 FFT 输出直接进 RAM，而是在中间插了 `Square`（平方）和 `Sum`（求和）两级**组合逻辑**，这样写入 `ram2` 的就已经是 \( \text{Re}^2+\text{Im}^2 \)。再经过步骤 6 的开方，就得到了幅度 \( \vert X\vert \)。

幅度公式（用来说明步骤 4+6 在做什么）：

\[
\vert X[k]\vert = \sqrt{\,\text{Re}(X[k])^{2} + \text{Im}(X[k])^{2}\,}
\]

#### 4.1.3 源码精读

这段「说明书」原文就在文件开头，是整个项目最重要的导读材料：

这段注释把算法的 8 个步骤逐条列出，其中第 4 步特别解释了为什么在 FFT 输出和 ram2 之间要插入 square（平方）和 adder（加法）两级，第 5 步特别提醒开方模块有 8 拍延迟。见 [verilog files/TOP.v:4-18](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L4-L18)。

摘出最核心的一句（第 4 步，解释 ram2 存的是什么）：

```verilog
//4.Unload data from FFT module, and store it into ram2. Because the FFT's output is a complex number,
//between FFT's out and ram2, it was introduced a square module(^2 operation), and an adder. In this way,
//if re and im are the output of the FFT, at the ram2's input will be re^2+im^2.
```

这句话直接对应了后面 `sq_real` / `sq_im` / `adder` 三个例化，以及 `ram2` 存 21 位数据（10 位 Re 平方得 20 位，20+20 相加得 21 位）的设计。

> 小贴士：在 Verilog 里，`//` 是单行注释、`/* ... */` 是块注释。作者的 8 步说明用的是块注释，所以它不会被综合成电路，纯粹是写给「读代码的人」看的。

#### 4.1.4 代码实践

**实践目标**：亲手把作者的「英文 8 步」翻译成自己的「中文流程」，确认你真的读懂了。

**操作步骤**：

1. 打开 [verilog files/TOP.v:4-18](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L4-L18)。
2. 准备一张纸或文本文件，把注释里的 `Steps: 1~8` 逐条翻译成中文。
3. 在每一步后面，**预测**一下：这一步会用到后面哪几个模块例化（参考第 3 节的源码地图表）。

**需要观察的现象**：

- 第 4 步同时提到了 `square module` 和 `adder`——你应该预测到后面会有「两个 Square + 一个 Sum」。
- 第 6 步说「both waveform and FFT」都从 ram3 之后发出——这暗示**波形数据也会走串口**，而不只是频谱。
- 第 8 步说「Another process is started... just when it is requested by PC」——这暗示系统是**被 PC 触发**的，不是自己不停地采。

**预期结果**：你会得到一份和 4.1.2 几乎一致的中文流程，并且对「为什么需要 ram2 和 ram3 两块 RAM」有了基于公式的理解。

> 本实践为「源码阅读型实践」，不需要硬件，待本地验证的是你对英文注释的理解是否和作者原意一致。

#### 4.1.5 小练习与答案

**练习 1**：作者为什么把「平方+求和」和「开方」分成两个阶段（分别存进 ram2 和 ram3），而不是一步算出幅度直接存？

<details><summary>参考答案</summary>
因为 FFT 是一边算一边**连续吐出** Re/Im 数据流的，平方和加法是「组合逻辑」，几乎即时，可以跟上 FFT 的输出节奏，把结果流式写进 ram2；而开方模块有 **8 个时钟周期的流水线延迟**（步骤 5），跟不上 FFT 的节奏，需要单独从 ram2 慢慢读、开方、再写进 ram3。所以 ram2 在这里还起到了「节奏缓冲」的作用。
</details>

**练习 2**：注释步骤 6 说数据「both waveform and FFT」都从 ram3 之后发出。但波形明明存在 ram1、频谱存在 ram3，波形数据是怎么「绕」到串口的？

<details><summary>参考答案</summary>
波形数据并没有先进 ram3。`ram1`（`ram_adc`）的读端口输出到信号 `buffer`，发送阶段会直接把 `buffer` 装进发送缓冲 `aggregated`；频谱数据则从 `ram3`（`data_send`）装进 `aggregated`。两者**共用同一个 UART 发送器** `serialt`，只是在主状态机的不同阶段轮流发送（见 4.2.3）。所以「从 ram3 之后发出」更准确的理解是「在 ram3 处理完之后，整个发送阶段才开始」。
</details>

### 4.2 模块例化清单与互连

#### 4.2.1 概念说明

「例化（instantiation）」就是在一个模块里**调用**另一个模块，相当于在主板上「插一颗芯片并接好线」。`TOP.v` 作为顶层，它自己几乎不做计算，它的主要工作就是：把十几个子模块「摆好」，再用 `wire`（连线）把它们接成一条完整的流水线。

读懂例化，要看三件事：
1. **调用了哪个模块**（模块名）；
2. **给这个实例起了什么名字**（实例名，方便区分同一种模块的多个副本，比如两个 `Square`）；
3. **端口怎么连**（`.端口名(连线)` —— 点号后面是子模块的端口，括号里是顶层里的连线）。

#### 4.2.2 核心流程

把第 3 节的例化表「接成线」，就得到本系统的核心数据流框图。下面这张图标注了**每一步的模块名**和**所在时钟域**：

```
                 ┌─────────── 时钟发生器 ───────────┐
 板载 100MHz ───► pll_loop ──► clk_100(100M) ─────────► Fourier
   (clk_in)                 ├─► clk(200M)  ───────────► ram1/ram2/ram3, Root_square, 主FSM
                             ├─► clk_50(50M) ──────────► ADC_clock_mux
                             └─► clk_UART(50M) ────────► serial_rx, serialt

  ┌──── ADC 时钟域 (clock_adc_out) ────┐
  │ read_adc ──► adc_read[9:0] ────────┼─────────────────────────────────┐
  └──────────────────────────────────── ┘                                 │
                                                                          ▼
                                              ┌──────── ram1 (SRAM, ram_adc) ────────┐
  PC 串口命令 P                                │ 写: ADR(由 ADC 域更新)  读: ram_read  │
   │  serial_rx (clk_UART)                    │ 存 10 位 ADC 采样, 满标志 carry       │
   ▼                                          └──────────┬────────────────────────────┘
  触发主 FSM (clk 200M)                                   │ buffer[9:0]
                                                          ▼
                                                    decoder(→补码)
                                                          │ decoder_out
                                                          ▼
   ┌─────────── FFT 时钟域 (clk_100) ──────────┐    Fourier ──► xk_re, xk_im (各 10 位)
   │                                            │              │
   └──────────────────────────────────────────── ┘              ▼
                                              Square×2 (Re², Im²) ──► Sum ──► sum[20:0]
                                                                          │
                                                          ┌───────────────▼───────────────┐
                                                          │ ram2 (SRAM2, ram_fft_20bit)   │  clk(200M)
                                                          │ 存 Re²+Im² (21 位)            │
                                                          └───────────────┬───────────────┘
                                                                          │ out_fft[19:0]
                                                                          ▼
                                                       Root_square (开方, 8 拍延迟)  clk(200M)
                                                                          │ square_out[9:0]
                                                          ┌───────────────▼───────────────┐
                                                          │ ram3 (SRAM3, ram_fft_10bit)   │  clk(200M)
                                                          │ 存幅度 (10 位) → data_send    │
                                                          └───────────────┬───────────────┘
                                                                          │
                                  ram1.buffer (波形) ─────────────────────┤
                                                                          ▼
                                                       aggregated[15:0] (发送打包)
                                                                          │  clk_UART(50M)
                                                                          ▼
                                                                serialt ──► serial_out ──► PC
```

一句话总结这条主线：

> **ADC**（ADC 时钟域）采样进 **ram1**；**Fourier**（100MHz）读 ram1 算 FFT；FFT 的 Re/Im 经 **Square×2 + Sum** 组合成 Re²+Im² 写进 **ram2**；**Root_square**（200MHz，8 拍）读 ram2 开方写进 **ram3**；最后 ram3 的频谱和 ram1 的波形一起，由 **serialt**（UART 时钟域）发给 PC。

#### 4.2.3 源码精读

**① 时钟分发**：板载晶振进来后，`pll_loop` 一次分出 4 路时钟。见 [verilog files/TOP.v:95-100](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L95-L100)，连线命名 `clk_100 / clk(200M) / clk_50 / clk_UART` 的定义在 [verilog files/TOP.v:48-51](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L48-L51)。

**② ram1：存 ADC 采样**。`ram_adc` 的写端口接 ADC 数据 `adc_read`，读端口输出到 `buffer`，并有满标志 `carry`。见 [verilog files/TOP.v:128-134](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L128-L134)。

**③ FFT 核**：跑在 `clk_100`，输入实部来自 `decoder_out`（由 ram1 的 `buffer` 经 `decoder` 转成补码），虚部恒为 0；输出 Re/Im 给后面的平方链。见 [verilog files/TOP.v:154-170](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L154-L170)。

**④ 平方 + 求和**：两个 `Square` 分别把 `xk_re`、`xk_im` 自乘（10×10→20 位），`Sum` 把两个 20 位结果相加（20+20→21 位 `sum`）。见 [verilog files/TOP.v:172-182](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L172-L182)。

**⑤ ram2：存平方和（21 位）**。`ram_fft_20bit` 的写数据就是上一步的 `sum`。见 [verilog files/TOP.v:137-142](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L137-L142)。

**⑥ 开方**：`Root_square` 读 ram2 的 `out_fft[19:0]`，开方后输出 `square_out`。见 [verilog files/TOP.v:184-188](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L184-L188)。

**⑦ ram3：存幅度（10 位）+ 串口发送**。`ram_fft_10bit` 写入 `square_out[9:0]`，读出为 `data_send`；`transmitter`（`serialt`）把 `data_send`（频谱）和 `buffer`（波形）轮流装进 `aggregated` 发出。见 ram3 例化 [verilog files/TOP.v:145-150](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L145-L150) 与发送器例化 [verilog files/TOP.v:110-117](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L110-L117)。

**⑧ 谁在指挥这一切？** 一个主状态机（参数定义在 [verilog files/TOP.v:213-227](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L213-L227)，含 `acq_state / fft_state / fft_write_state / square_state / send_state` 等）按顺序「点亮」上面这条流水线的每一级。本讲只需知道「有这么一个指挥」，它的内部逻辑是专家层（Unit 5）的内容。

#### 4.2.4 代码实践

**实践目标**：验证你「会读例化语句」，并能定位每个模块跑在哪个时钟域。

**操作步骤**：

1. 打开 `TOP.v`，找到 [pll_loop 例化（L95-100）](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L95-L100)，确认 4 路时钟连线的名字。
2. 用编辑器搜索这 4 个连线名（`clk_100`、`clk`、`clk_50`、`clk_UART`），看它们各自被接到哪些模块的 `.clk(...)` 端口。
3. 填写下面这张表：

| 时钟连线 | 频率（注释） | 喂给了哪些模块？ |
| --- | --- | --- |
| `clk_100` | 100MHz | ________ |
| `clk` | 200MHz | ________ |
| `clk_UART` | 50MHz | ________ |
| `clk_50` | 50MHz | ________ |

**需要观察的现象**：
- FFT 核（`Fourier`）用的是 `clk_100`，而三块 RAM 和开方模块用的是 200MHz 的 `clk`——同一份数据在不同时钟域之间流动。
- UART 收发用的是 `clk_UART`（50MHz），和 DSP 链路的时钟不同。

**预期结果**：
- `clk_100` → `Fourier`
- `clk`（200M）→ `SRAM(ram_adc)`、`SRAM2`、`SRAM3`、`Root_square`，以及主状态机 `always @(posedge clk)`
- `clk_UART` → `serial_rx`、`serialt`
- `clk_50` → `ADC_clock_mux`

> 波特率为什么用 50MHz、为什么 FFT 用 100MHz——这些「为什么」要等进阶单元（u2-l1 讲时钟、u4-l1 讲 UART 波特率）才能确认。本讲只要求你「看得到」这个分工。其中波特率换算部分**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`TOP.v` 里有两处 `Square` 例化（`sq_real` 和 `sq_im`），却只写了一个 `module Square`。为什么要例化两次？

<details><summary>参考答案</summary>
因为同一个时刻要**同时**算 \( \text{Re}^2 \) 和 \( \text{Im}^2 \) 两份平方。一个 `Square` 实例（一块乘法器硬件）同一拍只能算一个乘法，所以要例化两份：`sq_real` 算 `xk_re*xk_re`、`sq_im` 算 `xk_im*xk_im`，再一起送进 `Sum` 相加。这是硬件「空间换时间」的典型做法。
</details>

**练习 2**：从数据位宽看，为什么 ram2 要 21 位、而 ram1 和 ram3 都只有 10 位？

<details><summary>参考答案</summary>
ram1 存 ADC 原始采样，ADC（AD9215）是 10 位的，所以 10 位够。FFT 输出的 Re/Im 各 10 位，平方后 \(10 \times 10\) 需要 20 位，两个 20 位相加为防溢出再多一位，所以 ram2 要 21 位。开方之后又「压回」成幅度，所以 ram3 又恢复成 10 位。位宽在 `Square` 处膨胀、在 `Root_square` 处收缩，这正好对应公式里先平方求和、再开方的过程。
</details>

**练习 3**：`ram1`（`ram_adc`）的读地址由谁决定？提示：看例化里的 `.addr_r(...)` 连到了什么。

<details><summary>参考答案</summary>
连到了 `ram_read`，而 `ram_read` 来自 `mux_ram1`（一个 `MUX`）。这个 MUX 在 `index_in`（FFT 加载时的读地址）和 `cnt_waveform`（发送波形时的读地址）之间二选一。也就是说 **ram1 会被读两次**：第一次被 FFT 读走算频谱，第二次被发送器读走当波形上传——这正呼应了 4.1 注释里「the ram is read two times every process」。
</details>

## 5. 综合实践

**任务**：画出本系统的**完整数据流框图**，作为你后续学习所有进阶讲义的「随身地图」。

要求：

1. 画出主线：`ADC → ram1 → FFT → (平方+求和) → ram2 → 开方 → ram3 → UART → PC`。
2. 在每一步上标注：
   - **模块名 + 实例名**（如 `Fourier / FFT`、`Root_square / root_square`）；
   - **所在时钟域**（ADC 域 / `clk_100` / `clk`200M / `clk_UART`）；
   - **数据位宽**（10 / 20 / 21 / 10）。
3. 额外标出两条「旁路」：
   - PC 的串口命令是怎么进来的（`serial_rx → 主 FSM`）；
   - 波形数据是怎么「抄近路」从 ram1 直接到发送器的（`buffer → aggregated`）。
4. 在图旁边用一句话写清 ram1 / ram2 / ram3 各自存什么。

**检查方法**：画完后，对照本讲 4.2.2 的框图自查。如果你的图能回答下面三个问题，就算通关：
- 频谱数据经过了几块 RAM？（答：ram2、ram3）
- 为什么 ram2 是 21 位？（答：Re² + Im² 的位宽）
- 系统靠什么触发一次采集？（答：PC 发 `P` 命令经 `serial_rx` 触发主 FSM）

> 这是一个纯「源码阅读 + 画图」的实践，不需要硬件。把它保存好，后面每一篇进阶讲义都会让你在这张图上「放大」某一块。

## 6. 本讲小结

- `TOP.v` 文件头的注释给出了系统的 **8 步算法说明书**：采集→存 ram1→FFT→平方求和存 ram2→开方存 ram3→串口发 PC→LabVIEW 显示→PC 触发下一轮。
- `TOP.v` 本身是**顶层编排者**，它通过十几个「例化」把子模块接成一条流水线，自己只做连线与状态机调度。
- 三块 RAM 各司其职：**ram1**（`SRAM`，10 位）存 ADC 采样；**ram2**（`SRAM2`，21 位）存 \( \text{Re}^2+\text{Im}^2 \)；**ram3**（`SRAM3`，10 位）存开方后的幅度。
- 幅度公式 \( \vert X\vert=\sqrt{\text{Re}^2+\text{Im}^2} \) 解释了为什么需要 `Square×2 + Sum + Root_square` 这条链，以及为什么 ram2 位宽会膨胀到 21。
- 系统有**多个时钟域**：FFT 跑 100MHz，RAM 与开方跑 200MHz，UART 跑 50MHz，ADC 自带独立时基——同一份数据在它们之间流动。
- **文件名常和模块名不一致**（`ram2.v`↔`SRAM`、`SRAM.v`↔`SRAM2`、`Radical.v`↔`Root_square`、`Transcodor.v`↔`Transcoder`），读代码一律认模块名。

## 7. 下一步学习建议

入门单元（Unit 1）到此结束，你已经有了全局地图。接下来按兴趣选择进阶方向（它们彼此相对独立，可按任意顺序读）：

- **想搞懂时钟怎么来、多时钟域怎么协同** → 读 **u2-l1 时钟生成与多时钟域**（深入 `pll_loop` / `ADC_clock_mux` / `Transcoder`）。
- **想搞懂三块 RAM 的内部结构** → 读 **u2-l2 采样存储与三块 RAM**（深入 `SRAM` / `SRAM2` / `SRAM3`）。
- **想搞懂 FFT→幅度这条 DSP 链** → 依次读 **u3-l1 ~ u3-l4**（`Fourier` / `Square` / `Sum` / `Root_square`）。
- **想搞懂和 PC 怎么通信** → 读 **u4-l1、u4-l2**（`serial_rx` / `serialt` / `serial_tx`）。
- **想搞懂「指挥」这一切的主状态机** → 读专家层 **u5-l1 主采集状态机与命令协议**（这会把你今天画的框图「动」起来）。

建议：无论先读哪条线，都把本讲的框图放在手边，每学一个模块就在图上「点亮」它，逐步把静态的地图变成会动的系统。
