# 扩展实践与改进方向

## 1. 本讲目标

本讲是整本学习手册的收官篇。前面 17 讲我们从「项目是什么」一路拆到了 TOP 的三套状态机、混合语言集成、PC 协议与多板硬件。本讲不再引入新模块，而是站在系统之上做两件事：

1. **回头审视**：诚实地评估当前 HDL 实现的局限与可优化点，每一条都落到真实源码行号上。
2. **向前看**：理解心率（体积描记法）与皮电响应（GSR）这两大功能为何「主要活在模拟前端而非 HDL」，并据此提出 1~2 个可落地的扩展方向。

学完后你应当能够：

- 说出当前实现的至少 4 处局限，并能指出对应的源码位置。
- 解释为什么心率/GSR 不需要专门的 HDL 模块。
- 针对一个改进方向（如开方全流水线、第二路 ADC 通道），写出要改动哪些模块与状态。

## 2. 前置知识

本讲是综合评估，默认你已经读过以下讲义（否则部分结论会显得突兀）：

- **u1-l3** 系统总体架构与数据流：ADC→ram1→FFT→平方求和→ram2→开方→ram3→UART 的主链路。
- **u3-l3 / u3-l4** 开方 8 拍流水线延迟与整条 DSP 链的串联。
- **u5-l1** 主采集状态机与命令协议。
- **u6-l3** 多板硬件与模拟前端。

两个本讲会用到的工程概念，先用一句话解释：

- **吞吐（throughput）vs 延迟（latency）**：延迟是一个数据「从进到出」花多少拍；吞吐是「单位时间能处理多少个数据」。串行开方延迟高、吞吐也低；流水线化可以把吞吐提上去，但单点延迟不一定降。
- **可编程增益（PGA, Programmable Gain Amplifier）**：模拟前端里增益可由数字信号在线调节的放大器，本项目用 3 位 `adj` 选 8 档。

## 3. 本讲源码地图

本讲引用的核心文件很少，但会反复回看：

| 文件 | 作用 | 本讲怎么用 |
| --- | --- | --- |
| `verilog files/TOP.v` | 顶层编排器，含三套状态机 | 逐条定位局限的源码行号 |
| `readme.md` | 项目说明 | 确认三大功能定义与心率/GSR 方法 |
| `verilog files/Radical.v` | 开方 IP 封装（模块名 `Root_square`） | 讨论「开方延迟」改进的落点 |

> 命名提醒（贯穿全手册）：文件名、模块名、例化名常常错位。`Radical.v` 里的模块叫 `Root_square`，例化名是 `root_square`，背后真正的 Xilinx IP 原语叫 `Root`。读代码一律认 `module` 关键字后的名字。

## 4. 核心概念与源码讲解

本讲含两个最小模块：

- **4.1 局限与优化评估**：盘点当前 HDL 实现的不足，并给出改进方向。
- **4.2 心率/GSR 功能定位**：澄清这两大功能「活在模拟前端而非 HDL」的事实。

---

### 4.1 局限与优化评估

#### 4.1.1 概念说明

任何工程实现都是在「功能正确」与「资源/时间预算」之间做取舍的结果。本项目的 TOP.v 用一套清晰的状态机把示波器+FFT 的整条链路跑通了，这是它的优点——**结构直白、可读、可教学**。但同样的「直白」也带来若干局限：

- 一些参数被**写死**（FFT 点数、缩放系数、波特率分频），换场景就要重新综合比特流。
- 个别环节是**串行逐点处理**（开方），成为整条流水线的瓶颈。
- 通信与控制**缺乏校验与时钟域同步**，鲁棒性依赖 PC 自律与硬件运气。
- 存在**覆盖缺口**（开方漏掉最后一个频点）。

评估局限的目的不是苛责，而是为「下一步改哪里收益最大」提供依据。下面我们用一张表把局限、源码位置、影响、改进方向四者对齐。

#### 4.1.2 核心流程：从「找痛点」到「定方向」

评估一台 FPGA 系统的典型流程：

```
1. 画出数据流主链路（ADC→ram1→FFT→ram2→开方→ram3→UART）
2. 对每一级问三个问题：
   - 它处理多少数据？（覆盖范围）
   - 它花多少拍？（延迟/吞吐）
   - 它是写死的还是可配的？（灵活性）
3. 找出「最慢的一级」和「最不灵活的一处」 → 这就是优先改进点
4. 评估改动的「波及面」：要动几个模块、几套状态机
```

对本项目套用这个流程，瓶颈很清楚：**开方是延迟与吞吐双重最差的一级，UART 是上行带宽瓶颈，FFT 点数与缩放是最不灵活的一处。**

#### 4.1.3 源码精读：七处局限逐条定位

下面每一条都直接指向 TOP.v 的真实代码。

**局限 ①：FFT 点数与缩放系数写死，不可在线配置**

FFT 核的缩放编码 `scale_sch` 在例化时被钉死为常量，正向变换开关也是常量：

[verilog files/TOP.v:154-170](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L154-L170) — 这里 `.scale_sch(12'b001010101011)` 与 `.fwd_inv(1'b1)` 都是字面量，意味着 FFT 点数（由 IP 配置决定，本工程为 1024 点）和定点缩放都在综合时固定，运行时无法切换 512/1024/2048 点或调整缩放以防溢出。

**影响**：想做「低频用长窗、高频用短窗」的自适应频谱分析，或信号动态范围变化时自动调缩放，都做不到——必须重新打开 Vivado 工程、重配 IP、重新综合。

**局限 ②：单 ADC 通道**

顶层只有一个 10 位并行 ADC 数据输入端口，ADC 配置模块也只例化了一个：

[verilog files/TOP.v:22-31](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L22-L31) — `input [9:0] adc_read` 是唯一的采样输入。

[verilog files/TOP.v:120-125](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L120-L125) — 只有一个 `read_adc adc_conf(...)` 例化。

**影响**：无法做两路信号的相位比较、差分测量或双通道示波器。整个信号链（ram1、decoder、FFT、平方求和、开方、ram3）都是按单通道设计的。

**局限 ③：开方串行逐点 + 8 拍延迟，是流水线瓶颈**

作者自己在文件头的算法注释里就点明了这一点：

[verilog files/TOP.v:13-15](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L13-L15) — 原文："The square root module is not a 0 latency module, so it requires eight clock cycles."

TOP 用 `square_state`~`square_state5` 五个状态把单次开方编排成「等 rdy → 写 ram3 → 打 sclr 复位 → 地址 +1 → 重新放行」的逐点小循环：

[verilog files/TOP.v:355-391](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L355-L391) — 这五个状态每处理一个点要绕一圈、还要手动 `sclr` 复位 CORDIC 核，每点约 13 拍（待本地验证），1023 个点串行处理独占整帧约六成时间（详见 u3-l3/u3-l4）。

封装壳本身只是端口转接，零运算，瓶颈在 IP 的非流水线配置：

[verilog files/Radical.v:6-23](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/Radical.v#L6-L23) — `Root_square` 把 20 位输入、11 位输出直接接给 Xilinx CORDIC 开方 IP `Root`，没有做任何流水线打拍。

**影响**：开方成为整条 DSP 链最慢的一级。若把 CORDIC 配成「全流水线（fully pipelined）」模式，理论上可做到每拍进一个、每拍出一个，把吞吐提升约一个量级。

**局限 ④：开方漏掉最后一个频点（覆盖缺口）**

开方循环的退出判据是 `cnt_s < 1023`，于是只处理了 0..1022 共 1023 个频点：

[verilog files/TOP.v:357-370](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L357-L370) — `if(cnt_s<11'b1111111111)` 中 `11'b1111111111` = 1023，意味着 `cnt_s` 等于 1023 时直接跳 `send_state`，第 1023 号频点（对应奈奎斯特频率 bin）未被开方。

**影响**：频谱最末一个 bin 的幅度在 ram3 中是旧值或未定义值，上位机画出的频谱尾端可能有误差。这是一处潜在 bug，改进时可顺手修正判据为 `cnt_s<1024`（注意位宽与 FFT 实际输出点数要对齐，待确认 FFT 是否真的输出满 1024 点）。

**局限 ⑤：UART 上行带宽受限 + 帧间死等**

上行链路把每个 10 位样本拆成 2 字节发送，且字节发送机 `serial_tx` 在停止位之后还有一段额外的死区（详见 u4-l2）。发射机控制器 `serialt` 的例化：

[verilog files/TOP.v:109-117](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L109-L117) — 波特率由 `clk_UART`（50 MHz）与 `serialt/serial_tx` 内部 generic `M` 共同决定，本项目约 1.5625 Mbaud（待本地验证）。

**影响**：一帧含约 1024 个谱样本 + 约 2048 个波形样本，每个 2 字节，再加上 `F/F/T` 帧头，总字节量在数千字节量级；按约 1.5 Mbaud 且每字节还有额外帧间间隔，刷新率被严重拉低。改进方向是提高波特率或换 USB/以太网上行。

**局限 ⑥：命令协议无校验、无 ACK**

下行命令解析完全在 `wait_state` 里手写，没有任何校验或回执：

[verilog files/TOP.v:266-311](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L266-L311) — `P/A/B/C/D` 单字节或两字节命令直接写寄存器，参数字节若恰好等于某个命令字母会被误判为新命令（详见 u5-l1、u6-l2）。

**影响**：一次串口受干扰误码，就可能把时基、触发电平、增益写成意外值，且 PC 无从得知。生产级应用应加帧校验（如校验和）与简单 ACK。

**局限 ⑦：跨时钟域信号缺乏显式同步器**

`d_avail`、`start` 等信号在 50 MHz（UART）与 200 MHz（`clk`）域之间直接传递，`clock_adc_out`（ADC 域）与 `clk` 之间的 `carry`、`slope` 等也未走同步链（详见 u4-l1、u5-l2）。源码里看不到两级打拍寄存器。

**影响**：亚稳态风险。在竞赛演示温度/电压下通常能跑通，但量产或环境变化时是隐患。改进时应在每个跨域边界加两级触发器同步器（或更安全的握手/异步 FIFO）。

> 小结成表：

| # | 局限 | 源码位置 | 影响等级 | 改进方向 |
| --- | --- | --- | --- | --- |
| ① | FFT 点数/缩放写死 | TOP.v:154-170 | 中 | 把 `scale_sch` 改成寄存器可配 |
| ② | 单 ADC 通道 | TOP.v:22-31, 120-125 | 中 | 扩第二路通道（见 4.1.4） |
| ③ | 开方串行 + 8 拍延迟 | TOP.v:355-391, Radical.v | 高 | CORDIC 配全流水线 |
| ④ | 开方漏第 1023 点 | TOP.v:357-370 | 低(bug) | 判据改 `cnt_s<1024` |
| ⑤ | UART 上行带宽低 | TOP.v:109-117 | 高 | 提波特率/换 USB |
| ⑥ | 协议无校验/ACK | TOP.v:266-311 | 中 | 加校验和 + ACK |
| ⑦ | 跨时钟域无同步器 | 多处 | 中(隐患) | 加两级同步器/异步 FIFO |

#### 4.1.4 代码实践：改进方向之二选一深挖

下面给两个改进方向的「改动清单」，作为综合实践的备选素材。这里只是**设计层面的阅读与推演**，不要求你真的综合——目标字段明确写了「待本地验证」的地方，不要假装已经跑过。

**方向 A：把开方改成全流水线**

目标：把「每点约 13 拍、串行」的开方，变成「每拍进一点、8 拍后每拍出一点」的流水线，吞吐提升约一个量级。

改动清单：

1. **重配 CORDIC IP**：在 Vivado 里把 `Root` 开方核的流水线模式从「非流水线/低延迟」改成「全流水线（fully pipelined）」，保留其内部多级流水线寄存器。这一步在 IP 向导里点选，不在 HDL 里。
2. **改 `Root_square` 封装**：全流水线下核会**自动**每拍输出一个有效结果，配合自身的 `rdy`/`nd`（new data）握手，不再需要外部手动 `sclr` 复位。`Radical.v` 的端口可能要补一个输入有效信号（待确认该版本 IP 的端口名）。
3. **删掉 `square_state2~5` 的复位小循环**：TOP 里那五个状态本来是为了「处理一点 → 手动复位核 → 处理下一点」。全流水线下无需手动复位，可把五状态压缩成「每拍把 `cnt_s` 同时作为 ram2 读地址与 ram3 写地址递增」，并让 `we3` 在流水线填满后常开。
4. **处理流水线填充延迟**：前 8 拍输出无效，需要用一个长度等于流水线级数的小计数器屏蔽前 8 拍的 `we3`。
5. **顺手修局限 ④**：把退出判据改成覆盖全部频点。

要动的模块/状态：`Radical.v`（封装）、TOP 的 `square_state` 系列（五态→约两态）。波及面小，是性价比最高的改进。

**方向 B：增加第二路 ADC 通道**

目标：支持双通道同步采样，为差分测量/双通道示波器打底。

改动清单：

1. **顶层端口**：新增 `input [9:0] adc_read2`，以及（若第二路 ADC 也受控）扩展 `clock_adc_out`/`adc_pwdn` 的扇出或新增一组。
2. **复制存储与转换链**：新增 `ram_adc2`（SRAM 实例）、第二套 `decoder`，以及对应的 `cnt_waveform`/读地址 MUX。
3. **复用还是复制 FFT？** FFT 核同一时刻只能处理一路。两条路要么「分时复用同一个 FFT（两次变换）」，要么「再例化一个 FFT 核（资源翻倍）」。分时复用要在主状态机里把 `fft_state`/`fft_write_state` 跑两遍，分别写 ram2 和一个新增的 ram2b。
4. **平方/求和/开方**：若两路都要频谱，则 Square×2+Sum+Root_square 都要复制一份或分时复用。
5. **上行帧格式**：`serialt`/state3 的打包逻辑要区分通道，帧头需扩展（如 `F/F/T` 后加通道标识字节），PC 端解析也要同步改（见 u6-l2）。

要动的模块/状态：TOP 端口、SRAM 例化、主状态机的 FFT/开方段、state3 打包、PC 协议。波及面大，是一个中等规模的二次开发项目。

> 两个方向对比：方向 A 改动小、收益（吞吐）显著，适合作为练手；方向 B 改动大、但解锁新能力（双通道），适合作为课程项目或毕业设计课题。

#### 4.1.5 小练习与答案

**练习 1**：局限 ④ 说开方漏掉了第 1023 号频点。请根据 [TOP.v:357-370](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L357-L370) 的判据 `cnt_s<11'b1111111111`，确认这个二进制字面量的十进制值，并说明它处理了哪几个点、漏了哪个点。

**参考答案**：`11'b1111111111` 是 10 个 1、最高位（第 10 位）为 0，即十进制 1023。循环在 `cnt_s < 1023` 时继续处理，所以处理了 `cnt_s = 0,1,…,1022` 共 1023 个点；当 `cnt_s` 到 1023 时直接转 `send_state`，第 1023 号频点未被开方（其 ram3 内容为旧值/未定义）。

**练习 2**：若想把 `scale_sch` 从写死的 `12'b001010101011` 改成可在线配置，至少要改 TOP.v 的哪几处？提示：参考 [TOP.v:154-170](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L154-L170) 的例化与命令协议。

**参考答案**：(a) 新增一个 `reg [11:0] scale_sch_reg`；(b) 把例化的 `.scale_sch(12'b001010101011)` 改成 `.scale_sch(scale_sch_reg)`，并保留 `.scale_sch_we(1'b1)` 或改成受控写使能；(c) 在 `wait_state` 的命令协议里新增一条命令（如 `S`），用一个新 `conf_index` 值接收 2 字节参数写入 `scale_sch_reg`；(d) 同步更新 PC 端协议（见 u6-l2）。注意运行时改缩放会影响正在进行的变换，应在 `wait_state` 空闲态写入。

**练习 3**：为什么说方向 A（开方全流水线）是「波及面最小、性价比最高」的改进？用一句话回答。

**参考答案**：因为它只触及 `Radical.v` 封装与 TOP 的 `square_state` 五状态小循环，不碰端口、不碰 PC 协议、不碰 FFT，却能把整条链路最慢一级（独占约六成帧时间）的吞吐提升约一个量级。

---

### 4.2 心率/GSR 功能定位

#### 4.2.1 概念说明

readme 里写着本系统集成三大功能：示波器（含 FFT）、心率测量、皮电响应 GSR。初学者很容易假设「既然是 FPGA 项目，那心率/GSR 一定也有专门的 HDL 模块」。**事实并非如此。**

关键结论先放出：

> **心率（体积描记法）与 GSR 的「特色」主要活在模拟前端 PCB 上，而不是 HDL 里。它们复用了示波器的同一条数字骨架（ADC→FPGA→UART→PC），HDL 侧没有为它们单独编写处理模块。**

这不是项目缺陷，而是合理的架构选择：心率与 GSR 都是**低频、慢变**的生理信号（心率约 1 Hz 量级、GSR 变化以秒计），用示波器那套「高速采样 + FFT + 上传」的骨架去采集它们完全够用，差异由三件事决定：

1. **传感器不同**：心率用光学体积描记传感器，GSR 用皮肤电极。
2. **模拟前端调理不同**：把传感器微弱信号放大、滤波到 AD9215 能数字化的范围。
3. **上位机算法不同**：心率/GSR 的特征提取（峰值检测、电平统计）在 PC 端 LabVIEW 做，不在 FPGA。

也就是说，FPGA 这块「数字后端」对三种功能是同一套代码；把三种功能区分开来的，是模拟前端板和 PC 算法。

#### 4.2.2 核心流程：一个信号在三种功能下的路径

```
传感器（光电/电极/探头）
   │
   ▼
模拟前端 PCB（放大、滤波、偏置）  ← 心率/GSR 与示波器的差异主要在这里
   │
   ▼
AD9215 ADC（10 位并行）           ← 三种功能共用
   │
   ▼
FPGA / TOP.v（采样→ram1→FFT→…→UART）  ← 三种功能共用同一套 HDL
   │
   ▼
MCP2200 USB↔串口桥                ← 三种功能共用
   │
   ▼
PC / LabVIEW GUI                  ← 心率/GSR 的特征提取算法在这里
```

对本讲义系列最重要的一点：**我们在前 17 讲精读的所有 HDL（TOP、FFT、平方求和、开方、UART、三套状态机）都是三种功能共用的「通用数据采集骨架」。**心率/GSR 并未在这套骨架里增加任何专属模块。

#### 4.2.3 源码精读：用源码证明「HDL 里没有心率/GSR 专属模块」

**证据一：readme 明确把心率/GSR 的方法标注在「传感器+方法」层面，而非 FPGA 处理层面。**

[readme.md:17-21](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/readme.md#L17-L21) — 原文："a heart rate measurement module (based on plethysmograph method) and a galvanic skin response (GSR) module." 这里「module」指的是系统功能模块（含传感器+模拟前端+PC 算法），不是 Verilog/VHDL 模块。

**证据二：TOP.v 的顶层端口里没有任何心率/GSR 专属引脚。**

[verilog files/TOP.v:22-31](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L22-L31) — 端口只有：`clk_in`（时钟）、`adc_read`（ADC 数据）、`serial_in/out`（串口）、`clock_adc_out`/`adc_pwdn`（ADC 控制）、`leds`（调试）、`adj`（模拟前端增益控制）。没有任何标着 heart/GSR/pleth 的端口。心率/GSR 信号若要进 FPGA，只能经 `adc_read` 这同一条 10 位并行总线。

**证据三：FPGA 对模拟前端唯一的反向控制是 `adj`，且是通用的可编程增益控制，非心率/GSR 专属。**

[verilog files/TOP.v:29](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L29) — `output reg [2:0] adj` 注释为 "adjustments for the analogic circuit (set the amplification/attenuation)"。

[verilog files/TOP.v:301-304](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L301-L304) — `adj` 由 PC 的 `D` 命令参数字节低 3 位在线改写，上电默认 `3'b100`（第 4 档，见 [TOP.v:256](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L256)）。这是「通用模拟前端增益调节」，对三种功能一视同仁。

> 综上三条：HDL 层面，心率与 GSR 没有任何专属代码，它们只是「通过同一根 `adc_read` 进来的、由不同传感器和模拟前端调理出的慢变信号」。把它们区分开的，是 `Electronic boards design/` 下的模拟前端 PCB（心率板等，详见 u6-l3）和 PC 端 LabVIEW 算法。这正呼应了本手册自 u1-l1 起反复强调的结论。

#### 4.2.4 代码实践：在仓库里「找反例」

这是一个**源码搜索型实践**，目的是让你亲手验证上面「HDL 无专属模块」的结论，而不是听我一面之词。

1. **实践目标**：确认 `verilog files/` 与 `vhdl files/` 下不存在任何心率/GSR/pleth/EDA/EKG 专属的处理模块。
2. **操作步骤**：
   - 在仓库根目录用文本搜索工具（ripgrep/grep）搜索关键词：`heart`、`pleth`、`gsr`、`galvanic`、`ekg`、`eda`、`pulse`、`hr`（不区分大小写）。
   - 同时列出 `verilog files/` 与 `vhdl files/` 的全部文件清单，逐个看模块名。
3. **需要观察的现象**：这些关键词在 HDL 源码里应当**几乎不出现**（最多出现在 readme.md 或注释里作为说明），文件清单里也找不到以心率/GSR 命名的模块文件。
4. **预期结果**：HDL 目录下全是通用数据采集模块（TOP/SRAM/Fourier/Square/Sum/Root_square/serial_* 等），没有任何一个模块名暗示心率或 GSR。这与本讲的结论一致。
5. **若搜到了**：若你在 HDL 里真的搜到带这些词的模块，说明项目比你想象的更复杂——请以源码为准，回头修正本讲的结论，并标注「待确认」。

> 这一步体现的是源码阅读的基本素养：**任何「文档说有、代码里找不到」的功能，都要打一个问号**，直到你在源码或硬件里落实它。

#### 4.2.5 小练习与答案

**练习 1**：心率测量用的是「体积描记法（plethysmograph）」。结合本讲，这个方法在系统里主要靠哪一层实现——HDL、模拟前端、还是 PC 算法？

**参考答案**：主要靠模拟前端（光学传感器 + 调理电路）与 PC 算法（峰值检测算心率）。HDL 只负责把经模拟前端调理后的信号用通用骨架采样、上传，不包含体积描记法专属的处理逻辑。

**练习 2**：如果要让 FPGA「亲自」算心率（在片上做峰值检测并只上传每分钟心跳数），最小改动是什么？需要新增什么？

**参考答案**：新增一个 HDL 模块（如 `heart_rate_detector`），接 `adc_read`（或 ram1 读出的波形），用一个阈值 + 去抖的状态机检测脉冲峰值，再用一个计数器在固定时间窗内数峰值；结果经 `serialt` 上传。同时要在主状态机里给这条「轻量上行」留一个状态分支。这是把原本在 PC 做的算法下沉到 FPGA，会降低上行带宽需求，但增加 FPGA 逻辑资源占用。

---

## 5. 综合实践

本讲的综合实践是写一份**改进提案**，把 4.1 和 4.2 的认知串起来。

**任务**：写一份不超过 300 字的改进提案，从 4.1.4 的两个方向（或自选一个合理方向，如「提高波特率」「加协议校验」「心率片上检测」）中任选一个，说明：

1. 你要解决 4.1 表中的哪一条局限。
2. 要改动哪些模块、哪些状态机（给出 TOP.v 的状态名或模块名）。
3. 改动后预期的收益（吞吐/带宽/功能）与风险（资源/时序/波及面）。
4. 哪些部分需要「待本地验证」（如综合后的资源占用、时序是否收敛、波特率实测值）。

**写作模板（示例，非标准答案）**：

```text
方向：把开方改成全流水线（对应局限③）。
改动：
  1) 在 Vivado 里把 CORDIC 开方 IP Root 重配为 fully pipelined；
  2) Radical.v 封装补输入有效握手（端口名待确认）；
  3) TOP 删除 square_state2~5 的手动 sclr 复位小循环，
     改为每拍把 cnt_s 同时作 ram2 读址与 ram3 写址递增，
     用一个小计数器屏蔽前 8 拍流水线填充；
  4) 顺手把判据 cnt_s<1023 改为覆盖全部频点（修局限④）。
收益：开方从每点约13拍串行→每拍一点，整帧开方段从约百微秒级
      降至约十微秒级（待本地验证）。
风险：流水线寄存器增多、资源略增；时序需重新收敛（待本地验证）。
波及面：仅 Radical.v 与 square_state 系列，不碰端口与 PC 协议。
```

**自检清单**：写完后对照检查——

- [ ] 是否每条改动都对应到了具体的模块名或状态名？
- [ ] 是否区分了「确定能改」与「待本地验证」？
- [ ] 是否说明了波及面（动了几个模块/几套状态机）？

> 这份提案的格式，可以直接迁移到你将来评估任何 FPGA 项目改进点时使用：**局限 → 落点（模块/状态）→ 收益 → 风险 → 待验证项**。

## 6. 本讲小结

- 本讲用一张七行表把当前 HDL 实现的局限逐条钉到了源码行号上：FFT 点数/缩放写死（TOP.v:154-170）、单通道（TOP.v:22-31）、开方串行 8 拍延迟（TOP.v:355-391）、开方漏第 1023 点（TOP.v:357-370）、UART 带宽低（TOP.v:109-117）、协议无校验（TOP.v:266-311）、跨时钟域无同步器。
- 评估局限的标准流程是：沿主链路逐级问「覆盖多少、花多少拍、是否可配」，找出最慢一级与最不灵活一处。
- 两个性价比最高的改进方向：**方向 A（开方全流水线）**波及面最小、吞吐收益最大；**方向 B（第二路 ADC 通道）**波及面大但解锁双通道新能力。
- 心率（体积描记法）与 GSR 的特色**主要活在模拟前端 PCB 与 PC 算法**，HDL 侧没有任何专属模块——三种功能共用同一套通用数据采集骨架，差异在传感器、模拟前端与上位机算法。
- 源码素养：任何「文档说有、代码里找不到」的功能都要打问号；本讲通过搜索 HDL 关键词亲手验证了「无心率/GSR 专属模块」。
- 改进提案的五要素：局限 → 落点（模块/状态）→ 收益 → 风险 → 待验证项。

## 7. 下一步学习建议

至此，整本手册的 18 讲全部完成。你已经从「项目是什么」走到了「能评估并改进它」。建议的后续学习路径：

1. **动手验证**：按本讲 4.1.4 的方向 A，在 Vivado 2016.1（或新版）里重配 CORDIC 开方 IP 为全流水线，尝试重综合，记录资源与时序报告，把本讲所有「待本地验证」逐条落实。
2. **补全 u6-l1**：本系列 u6-l1（混合语言集成与 Xilinx IP）讲义缺失。建议你结合 u1-l2、u6-l3 自行整理一份「TOP 例化的所有模块按自研 Verilog / 自研 VHDL / Xilinx IP 封装三类归类」的笔记，填补这块认知。
3. **横向对照**：拿另一个开源 FPGA 示波器/DAQ 项目（如 GitHub 上的 `scope`、`fpga-fft` 类工程）与本项目的开方/UART/FFT 握手做对比，理解不同实现风格的取舍。
4. **深入 IP**：挑一个本项目的 Xilinx IP（mult/adder/fft/CORDIC 开方/PLL），阅读其官方 Product Guide（PG 文档），理解 scale_sch、流水线模式、握手时序的官方定义，回头印证 u3 系列讲义。
5. **向下到模拟**：如果你对心率/GSR 感兴趣，下一步应离开 HDL，去学模拟前端设计（仪表放大器、有源滤波、PGA），把 u6-3 讲的多板系统中锁在 Altium 二进制里的电路，用分立元件重新搭一遍验证。

> 全手册到此结束。回顾主线：**ADC→ram1→decoder→FFT→平方求和→ram2→开方→ram3→UART**，由 TOP 的三套状态机（主 FSM、ADC 域 state2、发送打包 state3）编排，用三块双端口 RAM 做级间缓冲，跨四个时钟域（200/100/50 MHz 与 ADC 时基），通过 P/A/B/C/D 协议受 PC 控制——这就是这台 100 MSPS 数据采集系统的全貌。祝你在自己的 FPGA 项目里用得上这套从源码读懂、再改进它的方法。
