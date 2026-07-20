# spi_simple 核心架构与数据流

## 1. 本讲目标

上一篇（u2-l1）我们建立了 `spi_simple` IP 的寄存器地图——也就是软硬件之间的「契约」。本讲要回答下一个问题：**软件往这些寄存器里写了一个字之后，硬件内部到底发生了什么？那个字是怎么变成 SPI 线上的比特流的？从机回送的数据又是怎么回到软件手里的？**

读完本讲，你应当能够：

- 说清 `spi_simple.vhd` 里**两个 FIFO**（命令 FIFO / 响应 FIFO）各自的角色，以及为什么必须用 FIFO 把 AXI 侧和 SPI 引擎解耦。
- 看懂项目采用的**双进程方法（two-process method）**：`p_comb` 算下一拍状态、`p_seq` 负责寄存，二者靠 `two_process_r` 这个 record 串起来。
- 理清一次 SPI 事务的**启动与完成握手**：`SpiStart` / `SpiBusy` / `SpiDone` / `StoreRx` / `RxWrite` 这几个内部信号是怎么配合的。
- 画出一次「写后读」事务从「AXI 写 Data 寄存器」到「读 Data 寄存器弹出 RX FIFO」的完整数据流。

本讲只聚焦**核心数据通路与控制握手**。中断向量、状态位的细节留到 u2-l6；AXI 五通道的具体时序留到 u2-l3；SPI 时序（CPOL/CPHA/分频）留到 u2-l4。

## 2. 前置知识

本讲默认你已经读过 u2-l1（寄存器地图）。此外用到的几个概念，先用大白话过一遍：

- **SPI 主从通信**：一条总线由 Master 驱动时钟 `SCK` 和片选 `CS_n`（低有效），Master 发数据走 `MOSI`，从机回数据走 `MISO`。一次「传输」就是 Master 拉低某个从机的 `CS_n`，然后打出若干个 `SCK` 边沿，每个边沿搬一个 bit。
- **FIFO（先进先出队列）**：一种数据缓冲结构，`push` 进去的数据按顺序 `pop` 出来。本项目的 FIFO 来自 `psi_common` 库的 `psi_common_sync_fifo`（同步 FIFO，读写共用一个时钟）。
- **AXI4 寄存器接口**：CPU（如 Zynq 的 ARM 核）通过一组总线读写 FPGA 内的寄存器。在本 IP 里，AXI 侧先被 `psi_common_axi_slave_ipif` 翻译成简单的 `reg_wr` / `reg_wdata` / `reg_rd` / `reg_rdata` 信号，再喂给 `spi_simple`。
- **握手信号**：数字电路里常见的「valid/ready」式配合。本讲的握手信号名字里带 `Start` / `Done` / `Busy`，含义见名知意，下面会逐一展开。
- **RTL 与时钟域**：本讲所有逻辑都跑在同一个 AXI 时钟 `Clk`（即 `s00_axi_aclk`）下，是单时钟域设计，理解时序时只需盯住这一个时钟的上升沿。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲怎么用 |
|------|------|-----------|
| `hdl/spi_simple.vhd` | **核心实体**，本讲的主角。内部例化两个 FIFO 和一个 SPI 引擎，并用双进程方法做控制 | 全篇精读，几乎所有永久链接都指向这里 |
| `hdl/definitions_pkg.vhd` | 寄存器/状态/中断常量包，u2-l1 已详述 | 仅引用其中的寄存器索引常量，定位「Data 寄存器」等名字 |
| `hdl/spi_vivado_wrp.vhd` | 顶层 wrapper，把 AXI 翻译成 `spi_simple` 的端口信号 | 用它的 FIFO 端口映射说明「AXI 写 Data → `TxWrite`/`TxData`」这条线怎么连进来 |
| `tb/top_tb.vhd` | 测试平台，含「写后读」场景 | 代码实践时定位 `Write/Read Transaction` 段作为数据流的活样本 |

## 4. 核心概念与源码讲解

### 4.1 命令 FIFO 与响应 FIFO 的角色

#### 4.1.1 概念说明

SPI 是慢速串行协议，AXI 是快速并行总线。一次 SPI 传输往往要几十甚至几百个 `Clk` 周期才能搬完一个字。如果让 AXI 每写一个字就「原地等」SPI 传完，CPU 就会被钉死在这里。

`spi_simple` 的解法是在 AXI 侧与 SPI 引擎之间插**两个 FIFO**：

- **命令 FIFO（TX 侧）**：把发起一次 SPI 传输所需的**全部信息**打包成一个「命令字」推入队列。命令字里包含三样东西——发给哪个从机、发什么数据、本次要不要把读回来的数据存下来。这样 AXI 主机可以一口气塞多条命令然后转身去做别的事，SPI 引擎按自己的节奏从队列里一条条取出来执行。
- **响应 FIFO（RX 侧）**：SPI 引擎每完成一次「需要读回」的传输，就把从 `MISO` 采样到的数据推进这个队列。CPU 之后读 `Data` 寄存器时再按先进先出的顺序弹出来。

这条「命令进、响应出」的对称结构，是整个 IP 最核心的设计直觉。后面所有的控制逻辑，都是围绕「怎么往命令 FIFO 塞、怎么从响应 FIFO 取、什么时候启动引擎」展开的。

#### 4.1.2 核心流程

命令字（命令 FIFO 里每个条目的布局）由三段拼接而成：

\[ W_{\text{cmd}} = \underbrace{1}_{\text{StoreRx}} + \underbrace{\lceil \log_2(\text{SlaveCnt}) \rceil}_{\text{Slave}} + \underbrace{\text{TransWidth}}_{\text{Data}} \text{ bit} \]

- 最低位 `Cmd_StoreRx`：1 bit 标志，1 = 本次传输结束后把 `MISO` 读到的数据存入响应 FIFO。
- 中间 `Cmd_Slave`：$\lceil \log_2(\text{SlaveCnt}) \rceil$ 位，选中哪个从机。
- 高位 `Cmd_Data`：`TransWidth_g` 位，要经 `MOSI` 发出去的数据。

**写命令方向**（AXI → 命令 FIFO）的数据流：

1. AXI 写 `SlaveNr` 寄存器（索引 4）→ 更新 `CfgSlave`。
2. AXI 写 `StoreRx` 寄存器（索引 5）→ 更新 `CfgStoreRx`。
3. AXI 写 `Data` 寄存器（索引 0）→ 产生一个 `TxWrite` 脉冲，同时 `TxData` 上是写进来的数据。
4. `spi_simple` 把 `{CfgStoreRx, CfgSlave, TxData}` 拼成 `CmdIn`，在 `TxWrite` 有效时推入命令 FIFO。

注意：`SlaveNr` 和 `StoreRx` 是「**粘性**」配置——它们保持上一次写入的值，直到下一次写。所以软件只需在切换从机或切换读写模式时改写它们，之后每次写 `Data` 都复用当前配置。

**读响应方向**（响应 FIFO → AXI）的数据流：

1. SPI 引擎完成一次「StoreRx=1」的传输 → 给出 `SpiRxData` 和一个 `RxWrite` 脉冲 → 推入响应 FIFO。
2. AXI 读 `Data` 寄存器 → 产生 `RxAck` 脉冲 → 从响应 FIFO 弹出一个字到 `RxData`，经 `reg_rdata(0)` 回送给 CPU。

#### 4.1.3 源码精读

命令字的段位定义，用常量 + `subtype` 描述，便于按字段名取位：[hdl/spi_simple.vhd:87-90](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L87-L90) —— 这里声明 `Cmd_StoreRx` 在 bit0、`Cmd_Slave` 占接下来的 $\lceil\log_2(\text{SlaveCnt})\rceil$ 位、`Cmd_Data` 占最高的 `TransWidth_g` 位。

把当前配置 + 数据拼成命令字 `CmdIn`：[hdl/spi_simple.vhd:222-224](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L222-L224) —— 三行并发赋值分别填 StoreRx 位、Slave 段、Data 段。

命令 FIFO 的例化（`psi_common_sync_fifo`）：[hdl/spi_simple.vhd:226-243](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L226-L243) —— 关键映射：`dat_i ← CmdIn`、`vld_i ← TxWrite`（AXI 写 Data 时推入）、`rdy_i ← r.SpiStart`（引擎启动时弹出）、`empty_o/full_o/out_level_o` 给出空/满/水位。宽度 `width_g = CmdIn'length`，深度 `FifoDepth_g`，RAM 风格 `auto`、行为 `RBW`（Read-Before-Write）。

命令 FIFO 输出侧的字段拆解：[hdl/spi_simple.vhd:245-247](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L245-L247) —— 把弹出的 `CmdOut` 重新切成 `CmdSlave` / `CmdData` / `CmdStoreRx` 三个信号，分别送给 SPI 引擎和控制逻辑。

响应 FIFO 的例化：[hdl/spi_simple.vhd:251-268](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L251-L268) —— 关键映射：`dat_i ← SpiRxData`（引擎采到的 MISO 数据）、`vld_i ← r.RxWrite`（传输完成且 StoreRx=1 时推入）、`rdy_i ← RxAck`（AXI 读 Data 时弹出）、`dat_o ← RxData`、`in_level_o ← RxLevel_I`。注意响应 FIFO 用的是 `in_level_o`（入端水位），命令 FIFO 用的是 `out_level_o`（出端水位），两者都能反映 FIFO 里的条目数，但分别贴着各自关心的那一侧。

> **关于 `SlaveCnt_g = 1` 的一个小细节**：此时 $\lceil\log_2 1\rceil = 0$，`Cmd_Slave` 退化成一个「空区间」（VHDL 里的 null range），命令字里不占任何位，单从机被隐式选中。这是合法的 VHDL 行为，不必担心。

补充：AXI 侧如何把信号连到 `spi_simple` 的 FIFO 接口，见 wrapper 里的端口映射 [hdl/spi_vivado_wrp.vhd:250-255](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L250-L255) —— `TxWrite ← reg_wr(Data)`、`TxData ← reg_wdata(Data)`、`RxAck ← reg_rd(Data)`、`RxData → reg_rdata(Data)`。这就是「写 Data 推 TX FIFO、读 Data 弹 RX FIFO」这条约定在 RTL 层的落地。

#### 4.1.4 代码实践

**实践目标**：用 testbench 里的真实场景验证「命令 FIFO 装的是三段拼接」这件事。

**操作步骤**：

1. 打开 `tb/top_tb.vhd`，定位 `Write/Read Transaction` 场景：[tb/top_tb.vhd:192-205](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L192-L205)。
2. 该场景的 `TransWidth_g = 8`、`SlaveCnt_g = 3`（见 DUT 例化 [tb/top_tb.vhd:86-99](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L86-L99)）。代入公式：命令字宽 = \(1 + \lceil\log_2 3\rceil + 8 = 1 + 2 + 8 = 11\) bit。
3. 读这段代码的三次 `axi_single_write`：
   - 写 `SlaveNr=0` → `CfgSlave=00`
   - 写 `StoreRx=1` → `CfgStoreRx=1`
   - 写 `Data=0x12` → `TxData=0x12`，同时产生 `TxWrite` 脉冲。
4. 在脑中（或纸上）拼出这次被推入命令 FIFO 的 11 bit 命令字：bit0=1（StoreRx）、bit2..1=00（Slave）、bit10..3=0x12（Data）。

**需要观察的现象 / 预期结果**：推入的命令字应为 `0_0001_0010_0_1`（按 bit10..bit0 排列，即 StoreRx=1、Slave=00、Data=0x12），换算成 11 位整数即 `0b100010010_01`。后续 SPI 引擎启动时会弹出这个字，把 `0x12` 经 MOSI 发出，并在完成后把 MISO 收到的 `0x34`（见 [tb/top_tb.vhd:205](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L205) 的 `axi_single_expect(... 0x34 ...)`）存入响应 FIFO。

> 若你本地没有 Modelsim/Vivado 仿真环境，无法实际跑通，这一步可作为「源码阅读型实践」完成——重点是确认命令字三段拼接与公式一致。**待本地验证**：实际波形中 `CmdIn` 的 11 bit 取值。

#### 4.1.5 小练习与答案

**练习 1**：如果要把 IP 配成 5 个从机、`TransWidth_g = 16`，命令 FIFO 的数据宽度是多少 bit？

**参考答案**：\(1 + \lceil\log_2 5\rceil + 16 = 1 + 3 + 16 = 20\) bit（`CmdIn'length` 即 `Cmd_Data'high + 1`）。

**练习 2**：为什么命令 FIFO 用 `out_level_o`，而响应 FIFO 用 `in_level_o`？

**参考答案**：两个 level 都反映 FIFO 内当前条目数，差别只在「贴着哪一侧」。命令 FIFO 关心「还剩多少命令没执行」（出端视角），响应 FIFO 关心「已经收了多少个回送数据」（入端视角）。对同步 FIFO 而言两者数值相等，选哪个更多是语义表达。

---

### 4.2 双进程方法 p_comb / p_seq

#### 4.2.1 概念说明

`spi_simple` 的全部控制逻辑（什么时候启动 SPI、什么时候写响应 FIFO、怎么维护状态和中断）都集中在一段很短的代码里，用的是一种叫**双进程方法（two-process method）**的编码风格：

- 把这个时钟域里**所有需要寄存的量**打包进一个 `record` 类型 `two_process_r`。
- 用一个**纯组合进程 `p_comb`** 计算下一拍所有寄存器的取值（`r_next`）：它读当前状态 `r` 和外部输入，经过各种 `if` 判断后得到下一拍值 `v`，最后 `r_next <= v`。
- 用一个**纯时序进程 `p_seq`** 在 `Clk` 上升沿把 `r_next` 写进 `r`，并处理复位。

这种风格的好处：

- 时序进程极简（只有 `r <= r_next` 加复位），几乎不会写出时序错误。
- 所有「决策逻辑」集中在 `p_comb` 一处，读起来像读一段顺序程序：先 `v := r`（默认保持不变），再按条件修改 `v` 的个别字段。
- `record` 让一组相关信号成组搬运，加字段时只改 record 定义，不必到处牵线。

> 这是 PSI / `psi_common` 系列代码里相当常见的写法。`p_comb` 的敏感表里**显式列出**它依赖的所有信号（`r` 加全部外部输入），属于保守但清晰的写法。

#### 4.2.2 核心流程

`p_comb` 的固定骨架（伪代码）：

```
p_comb(r, ...所有外部输入...):
    v := r                      # ① 默认保持稳定
    v.SpiStart := '0'           # ② 对脉冲型信号先归零
    ...按条件改 v 的各字段...    # ③ 决策逻辑
    r_next <= v                 # ④ 输出下一拍值
```

`p_seq` 的固定骨架：

```
p_seq(Clk):
    if rising_edge(Clk):
        r <= r_next             # ⑤ 统一寄存
        if Rst = '1':
            r.SpiStart <= '0'   # ⑥ 同步复位，只覆盖需要确定的字段
            r.RxWrite  <= '0'
            r.IrqVec   <= (others => '0')
            r.Irq      <= '0'
```

注意第 ⑥ 点：复位只覆盖 `SpiStart / RxWrite / IrqVec / Irq` 这几个字段，`StoreRx`、`Status` 等不在复位列表里——它们在 `p_comb` 里每拍都会被重新计算（`Status` 每拍先清零再按条件置位），所以不需要复位初值。

#### 4.2.3 源码精读

`two_process_r` record 定义了所有被寄存的量：[hdl/spi_simple.vhd:110-118](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L110-L118) —— 包含 `SpiStart` / `StoreRx` / `RxWrite`（本讲主角）以及 `IrqVec` / `Irq` / `Status`（u2-l6 详述）。紧接着声明 `signal r, r_next : two_process_r;`，当前态与下一拍态成对出现。

`p_comb` 进程头与「保持稳定」开头：[hdl/spi_simple.vhd:125-129](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L125-L129) —— 敏感表里 `r` 打头，后面跟着 `SpiBusy`、`TxEmpty`、`CmdStoreRx`、`SpiDone` 等所有外部输入；进程体第一句 `v := r`。

`p_seq` 进程：[hdl/spi_simple.vhd:204-215](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L204-L215) —— `rising_edge(Clk)` 下 `r <= r_next`，复位分支覆盖列出的几个字段。

寄存器输出到端口的并发赋值：[hdl/spi_simple.vhd:195-199](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L195-L199) —— `CfgIrqVec ← r.IrqVec`、`Irq ← r.Irq`、`Status ← r.Status` 等，把 record 字段引到实体端口（`RxLevel/TxLevel` 引的是 FIFO 直接输出的 `RxLevel_I/TxLevel_I`，不走 record）。

#### 4.2.4 代码实践

**实践目标**：在脑中跑一遍 `r` 与 `r_next` 的关系，确认双进程的时序语义。

**操作步骤**：

1. 读 `p_comb` 里 `v.SpiStart := '0';` 这一行（[hdl/spi_simple.vhd:132](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L132)）。它在每拍开头把 `SpiStart` 默认置 0。
2. 紧接着的 `if` 在条件满足时把 `v.SpiStart := '1'`（[hdl/spi_simple.vhd:133-136](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L133-L136)）。
3. 追踪连续两拍 `r.SpiStart` 的取值：假设第 N 拍条件满足 → `r_next.SpiStart=1` → 第 N+1 拍 `r.SpiStart=1`。

**需要观察的现象 / 预期结果**：第 N+1 拍时，`p_comb` 敏感表里的 `r.SpiStart` 变成了 1，使得 `if` 条件里的 `r.SpiStart = '0'` 不成立，于是 `v.SpiStart` 保持为默认的 0。**这就是 `SpiStart` 天然只持续一个时钟周期的原因**——它是一个自限的单周期脉冲。预期：`SpiStart` 永远不会连续两拍为 1（除非中间隔了一次新的条件成立）。

**待本地验证**：在仿真波形里加 `r.SpiStart` 和 `r_next.SpiStart` 两个信号，确认前者比后者晚一拍，且只单拍有效。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `p_seq` 的复位分支里没有 `r.StoreRx`？

**参考答案**：`StoreRx` 只在 `SpiStart` 触发时被赋值（`v.StoreRx := CmdStoreRx`），其它拍由 `v := r` 保持。它没有「每拍重算」的逻辑，但因为它只在 `SpiStart` 那一拍被采样后随 `RxWrite` 一起使用，未复位时的初值不影响正确性（复位后 `SpiStart` 为 0，不会误触发写响应 FIFO）。作者选择不为它写复位，属于安全的省略。

**练习 2**：如果把 `p_comb` 敏感表里漏写 `SpiDone`，会发生什么？

**参考答案**：综合工具通常会基于进程体自动推导敏感表（综合阶段不影响），但在**仿真**里，`p_comb` 不会在 `SpiDone` 翻转时被重新求值，导致 `r_next.RxWrite`（依赖 `SpiDone`）更新滞后，出现仿真与综合行为不一致。所以显式写全敏感表是好习惯。

---

### 4.3 SPI 事务启动与完成握手

#### 4.3.1 概念说明

现在把 4.1 的 FIFO 和 4.2 的双进程拼起来，看一次 SPI 事务是如何被「启动」和「结束」的。参与握手的有三方：

- **命令 FIFO**（`i_tx_fifo`）：提供命令。
- **SPI 引擎**（`psi_common_spi_master`，例化为 `i_spi`）：执行串行收发。
- **控制逻辑**（`p_comb` 里的一段）：决定何时启动、何时把结果写回。

它们之间靠四个信号配合：

| 信号 | 方向 | 含义 |
|------|------|------|
| `SpiStart` | 控制逻辑 → FIFO 的 `rdy_i` & 引擎的 `start_i` | 单周期脉冲：弹出一条命令 + 启动引擎 |
| `SpiBusy` | 引擎 → 控制逻辑 | 传输进行中为 1，用于禁止重触发 |
| `SpiDone` | 引擎 → 控制逻辑 | 传输完成的单周期脉冲 |
| `StoreRx` / `RxWrite` | 控制逻辑内部 | 锁存本次命令的读回标志；完成时决定是否写响应 FIFO |

关键巧思：**`SpiStart` 这一根线同时干两件事**——它既是命令 FIFO 的读使能（弹出一个命令），又是 SPI 引擎的启动脉冲。因为命令 FIFO 是「弹出的数据立刻出现在 `CmdOut` 上」的同步 FIFO，所以同一拍里：命令被弹出 → `CmdSlave/CmdData/CmdStoreRx` 立刻可用 → 引擎拿到 `CmdData` 和 `CmdSlave` 开始干活。一根线完成了「取指」和「执行」的同步。

#### 4.3.2 核心流程

启动与完成的控制逻辑（`p_comb` 内，伪代码）：

```
# 启动判定
SpiStart := '0'
if (SpiBusy = '0') and (TxEmpty = '0') and (r.SpiStart = '0'):
    SpiStart := '1'
    StoreRx  := CmdStoreRx          # 锁存本次命令的读回标志

# 完成判定
RxWrite := r.StoreRx and SpiDone    # 只在「要读回」且「完成」时写响应 FIFO
```

一次事务的逐拍时序：

```
拍号  TxEmpty  SpiBusy  r.SpiStart  → 动作
T0      0        0         0        条件全满足 → SpiStart=1；FIFO 弹命令；引擎启动；StoreRx 锁存
T1      *        1         1        r.SpiStart=1 → 条件不满足 → SpiStart 回 0（脉冲结束）
T2..    *        1         0        传输中，SpiBusy=1 禁止新启动
Tdone   *        1→0       0        SpiDone=1 一拍 → RxWrite = StoreRx & 1
Tdone+1 *        0         0        若 StoreRx=1：SpiRxData 已被推入响应 FIFO；引擎空闲，可启动下一条
```

两个要点：

1. **启动条件里的 `r.SpiStart = '0'`** 是自限条件，保证 `SpiStart` 是单周期脉冲（见 4.2.4 的推演）。
2. **`RxWrite := r.StoreRx and SpiDone`** 把「本次命令要不要读回」和「传输完成了没」相与。`r.StoreRx` 是在启动那一拍锁存的，会一直保持到下次启动；它只有在 `SpiDone` 脉冲那一拍才真正发挥作用（决定写不写响应 FIFO）。所以即便一次「只写不读」（StoreRx=0）的传输完成时 `SpiDone` 也会来，但因为 `StoreRx=0`，`RxWrite` 为 0，响应 FIFO 不会被写入。

#### 4.3.3 源码精读

启动判定与 RX 写控制：[hdl/spi_simple.vhd:131-139](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L131-L139) —— `SpiStart` 先置 0，再在「引擎空闲 + FIFO 非空 + 上一拍没启动」时置 1 并锁存 `StoreRx`；`RxWrite` 由 `r.StoreRx and SpiDone` 给出。这是本讲最核心的 9 行代码。

命令 FIFO 把 `rdy_i` 接到 `r.SpiStart`：[hdl/spi_simple.vhd:239](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L239) —— 这就是「`SpiStart` 弹命令」的连线点。

响应 FIFO 把 `vld_i` 接到 `r.RxWrite`：[hdl/spi_simple.vhd:262](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L262) —— 这就是「`RxWrite` 推响应」的连线点。

SPI 引擎例化与握手端口映射：[hdl/spi_simple.vhd:271-299](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L271-L299) —— `start_i ← r.SpiStart`、`slave_i ← CmdSlave`、`busy_o → SpiBusy`、`done_o → SpiDone`、`dat_i ← CmdData`、`dat_o → SpiRxData`。引擎对外只有 `start/busy/done` 三信号握手，时序细节全部封装在 `psi_common_spi_master` 内部（u2-l4 展开）。

#### 4.3.4 代码实践

**实践目标**：画出一次「StoreRx=1」事务里 `SpiStart / SpiBusy / SpiDone / r.StoreRx / RxWrite` 五个信号的理想时序波形。

**操作步骤**：

1. 准备一张时序草稿，横轴是 `Clk` 周期（T0, T1, …, Tdone, Tdone+1）。
2. 根据本节「逐拍时序」表，逐行画出五个信号的电平。
3. 在 `SpiStart=1` 的那一拍标注「FIFO 弹命令 + 引擎启动」；在 `SpiDone=1` 的那一拍标注「`RxWrite=StoreRx & 1` → 推响应 FIFO」。

**需要观察的现象 / 预期结果**：理想波形应满足：

- `SpiStart` 只在 T0 单拍为 1。
- `SpiBusy` 在 T0 当拍或 T1 起为 1，直到 Tdone 拍结束前都为 1，Tdone+1 恢复 0。
- `SpiDone` 在 Tdone 单拍为 1。
- `r.StoreRx` 在 T0 被置成 `CmdStoreRx`（本例为 1），之后保持。
- `RxWrite` 仅在 Tdone 拍为 1（因为 `StoreRx=1` 且 `SpiDone=1`）。

**待本地验证**：用 `sim/run.tcl` 跑回归，在波形窗口把 `i_spi` 实例的 `start_i`/`busy_o`/`done_o` 以及 `i_resp_fifo` 的 `vld_i`/`rdy_i` 都拉出来，对照上图确认。

#### 4.3.5 小练习与答案

**练习 1**：如果软件连发两条「只写不读」（StoreRx=0）的命令，响应 FIFO 会被写入吗？

**参考答案**：不会。两次传输完成时 `SpiDone` 都会来一拍，但 `r.StoreRx=0`，所以 `RxWrite = 0 & 1 = 0`，响应 FIFO 不被写入，`RxLevel` 保持不变。

**练习 2**：启动条件里如果删掉 `SpiBusy = '0'` 这个判断，会出现什么问题？

**参考答案**：在一次传输进行中（`SpiBusy=1`），只要命令 FIFO 非空、且 `r.SpiStart=0`（T1 拍之后成立），就会再次拉高 `SpiStart`，向正在忙的引擎重复发 `start_i`，并从命令 FIFO 多弹一条命令——这会丢失命令或破坏当前传输。`SpiBusy=0` 的作用是「等引擎闲下来再启动下一条」。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面的「写后读」全链路数据流追踪。这是本讲的主实践。

**任务**：画出一次「写后读」事务，从 **AXI 写 `Data` 寄存器**开始，到 **AXI 读 `Data` 寄存器弹出 RX FIFO**结束的完整数据流框图，并在图上标注下列关键信号的变化点。

**素材**：直接以 testbench 的 `Write/Read Transaction` 场景为依据——`SlaveNr=0`、`StoreRx=1`、发送 `0x12`、期望读回 `0x34`（[tb/top_tb.vhd:192-205](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L192-L205)）。

**建议的框图骨架**（请把每个方框的信号名、寄存器索引、关键脉冲补全）：

```
 AXI 主机 (testbench p_control)
      │
      │ ① axi_single_write(SlaveNr*4, 0)   ──► reg_wdata(4) ──► CfgSlave
      │ ② axi_single_write(StoreRx*4, 1)   ──► reg_wdata(5) ──► CfgStoreRx
      │ ③ axi_single_write(Data*4, 0x12)   ──► reg_wr(0)=TxWrite 脉冲
      │                                        reg_wdata(0)=TxData=0x12
      ▼
 ┌─────────────── spi_simple (本讲主角) ───────────────┐
 │                                                       │
 │  CmdIn = {CfgStoreRx, CfgSlave, TxData}  ──push──►  命令 FIFO (i_tx_fifo)
 │                                                       │
 │  p_comb: TxEmpty=0 & SpiBusy=0 ──► SpiStart=1 ──pop─►│ (弹出 CmdSlave/CmdData/CmdStoreRx)
 │                       │                               │
 │                       └──start_i──►  SPI 引擎 (i_spi) │
 │                          busy_o ◄─── SpiBusy          │
 │                          done_o ◄─── SpiDone          │
 │                          dat_i  ◄── CmdData(0x12)     │
 │                          dat_o  ──► SpiRxData(0x34)   │
 │                                                       │
 │  RxWrite = r.StoreRx & SpiDone ──push──► 响应 FIFO (i_resp_fifo)
 │                                                       │
 └───────────────────────────────────────────────────────┘
      │
      │ ④ axi_single_expect(Data*4, 0x34) ──► reg_rd(0)=RxAck 脉冲
      ▼                                            ──pop──► RxData(0x34) ──► reg_rdata(0)
 AXI 主机读到 0x34
```

**操作步骤**：

1. 按上面的骨架，把每个箭头标注的信号名与本项目源码里的真实信号一一对应（可参照 4.1.3、4.3.3 的永久链接）。
2. 在「命令 FIFO → 引擎」这段，标出 `SpiStart` 单周期脉冲发生在哪一拍；在「引擎 → 响应 FIFO」这段，标出 `SpiDone` 与 `RxWrite` 发生在哪一拍。
3. 在框图旁边写出本次推入命令 FIFO 的 11 bit 命令字（参考 4.1.4 的结论）。
4. 对照 [tb/top_tb.vhd:205](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L205) 的 `axi_single_expect(RegIdx_Data_c*4, 16#34#, ...)`，确认读回值与响应 FIFO 弹出值一致。

**预期结果**：框图应清楚呈现「写 `Data` 推命令 FIFO → 控制逻辑启动引擎 → 引擎完成后写响应 FIFO → 读 `Data` 弹响应 FIFO」这条闭环，且能解释为何发送 `0x12` 却读回 `0x34`（`0x34` 是从机经 `MISO` 回送、由 `SlaveTx` 驱动的数据，见 [tb/top_tb.vhd:196](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L196)）。

**待本地验证**：若本地有仿真环境，用 `sim/run.tcl` 跑通后，在波形里沿上述信号链抓一遍，确认每一跳的因果关系与框图一致。

## 6. 本讲小结

- `spi_simple` 用**两个 FIFO** 把慢速 SPI 引擎和快速 AXI 总线解耦：命令 FIFO 打包 `{StoreRx, Slave, Data}` 三段，响应 FIFO 缓存 MISO 读回数据。
- AXI 写 `Data` = 推命令 FIFO；AXI 读 `Data` = 弹响应 FIFO；`SlaveNr` 和 `StoreRx` 是粘性配置，改一次后对后续每次写 `Data` 都生效。
- 全部控制逻辑采用**双进程方法**：`two_process_r` record 聚拢所有寄存量，`p_comb` 算 `r_next`、`p_seq` 在上升沿寄存并处理同步复位。
- 一次 SPI 事务靠 `SpiStart`（单周期脉冲，兼任 FIFO 弹出与引擎启动）、`SpiBusy`（忙闲闭锁）、`SpiDone`（完成脉冲）三个信号握手。
- 是否把读回数据写入响应 FIFO，由 `RxWrite = r.StoreRx and SpiDone` 决定——只写不读的事务不会污染响应 FIFO。
- 本讲刻意把中断向量、状态位的细节留到 u2-l6，把 AXI 五通道时序留到 u2-l3，把 SPI 时序（CPOL/CPHA/分频）留到 u2-l4。

## 7. 下一步学习建议

接下来推荐按以下顺序继续：

- **u2-l3（AXI4 从接口与寄存器映射）**：本讲的框图里，AXI 侧的 `reg_wr`/`reg_wdata`/`reg_rd`/`reg_rdata` 是怎么从 AXI 五通道翻译过来的？那一段在 wrapper 里的 `psi_common_axi_slave_ipif`。建议接着读它，把框图最左边的「AXI 主机」那段补全。
- **u2-l4（SPI 主控时序与引擎集成）**：本讲把 `psi_common_spi_master` 当成一个「给 `start` 就干活、出 `done` 就完成」的黑盒。下一讲进到引擎内部，看 `ClockDivider`/`CPOL`/`CPHA`/`CsHighCycles` 这些 generic 如何决定 `SCK` 的频率与采样边沿。
- **u2-l5（FIFO 缓冲与背压机制）**：本讲提到命令/响应 FIFO 的 `empty/full/level` 输出，下一讲讲它们如何驱动状态位与中断、`CfgTxAlmEmpty`/`CfgRxAlmFull` 阈值如何产生 almost 条件。
- **u2-l6（中断向量与状态机制）**：本讲刻意略过的 `p_comb` 里 `IrqVec`/`Status` 那一大段，下一讲集中拆解锁存、按位清除与自动重置。

如果想立刻动手巩固本讲，建议先重做一遍第 5 节的综合实践——把那张数据流框图画到能脱稿为止，本讲的核心就真正落地了。
