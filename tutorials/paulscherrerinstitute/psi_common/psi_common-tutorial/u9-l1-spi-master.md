# SPI 主机 spi_master 与 spi_master_cfg

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 SPI 的四种时序模式（CPOL/CPHA 组合）各自在 SCLK、MOSI 上的表现。
- 读懂 `psi_common_spi_master` 用「状态机 + 二进程 record 法」生成 SCLK、移位收发数据的全过程。
- 区分 MSB 优先与 LSB 优先两种位序，并指出代码中切换位序的那几行。
- 解释 `spi_master_cfg` 如何把「传输位宽」从综合期 generic 下放到运行时端口 `trans_width_i`。
- 了解三线 SPI（3-wire SPI）下 `spi_tri_o` 的作用，以及它与普通四线 SPI 的差异。

## 2. 前置知识

### 2.1 SPI 是什么

SPI（Serial Peripheral Interface，串行外设接口）是主机与一个或多个从机之间点对点的**同步串行**总线。典型四线 SPI 有四根信号：

| 信号 | 方向（主机视角） | 作用 |
|:--|:--|:--|
| SCLK | 主机→从机 | 串行时钟，由主机产生 |
| MOSI | 主机→从机 | 主出从入（Master Out Slave In）|
| MISO | 从机→主机 | 主入从出（Master In Slave Out）|
| CS_n | 主机→从机 | 片选（低有效），选中某个从机 |

每次传输是「全双工」的：主机在 MOSI 上每拍移出 1 比特的同时，从机在 MISO 上移回 1 比特。传完 N 比特，主机拿到从机的 N 比特，从机也拿到主机的 N 比特。

### 2.2 CPOL 与 CPHA

SPI 没有统一的标准时序，靠两个参数约定：

- **CPOL**（Clock Polarity，时钟极性）：SCLK **空闲时**的电平。CPOL=0 空闲低，CPOL=1 空闲高。
- **CPHA**（Clock Phase，时钟相位）：数据在哪个沿被采样。CPHA=0 在**第一个**时钟沿（leading edge）采样、第二个沿（trailing edge）切换数据；CPHA=1 反过来，第一个沿切换数据、第二个沿采样。

两者组合出四种模式：

| 模式 | CPOL | CPHA | SCLK 空闲 | 数据采样沿 |
|:--|:--|:--|:--|:--|
| 0 | 0 | 0 | 低 | 上升沿（leading）|
| 1 | 0 | 1 | 低 | 下降沿（trailing）|
| 2 | 1 | 0 | 高 | 下降沿（leading）|
| 3 | 1 | 1 | 高 | 上升沿（trailing）|

> 口诀：**CPOL 决定空闲电平，CPHA 决定数据在哪一沿采样**。主机和从机必须配成同一模式才能通信。

### 2.3 与前置讲义的衔接

- 本组件用**二进程 record 设计法**（`r`/`r_next` + 组合进程 `p_comb` + 时序进程 `p_seq`），这套范式在 [u7-l1 pl_stage](u7-l1-pl-stage.md) 已详细讲过，本讲直接套用。
- SCLK 的生成本质上是一个「分频计数器」，思路与 [u6-l1 选通与节拍生成](u6-l1-strobe-tick-generator.md) 的 `strobe_generator` 同源——都是用系统时钟计数产生更低频的节拍。
- 位宽推导继续用 `math_pkg.log2ceil`（见 [u2-l1](u2-l1-math-pkg.md)）。

## 3. 本讲源码地图

| 文件 | 作用 |
|:--|:--|
| [hdl/psi_common_spi_master.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_spi_master.vhd) | 固定位宽 SPI 主机，支持四线/三线 SPI、多从机、latch enable |
| [hdl/psi_common_spi_master_cfg.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_spi_master_cfg.vhd) | 运行时可配置传输位宽的变体，去掉三线 SPI 与 latch enable |
| [testbench/psi_common_spi_master_tb/...tb.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_spi_master_tb/psi_common_spi_master_tb.vhd) | 自校验测试平台，用一个「从机进程」回环校验收发数据 |

## 4. 核心概念与源码讲解

### 4.1 SPI 时序模式（CPOL/CPHA）

#### 4.1.1 概念说明

SPI 主机最核心的职责是**按指定模式产生 SCLK，并在正确的沿上切换/采样 MOSI**。`psi_common_spi_master` 用两个 generic 描述模式：`spi_cpol_g` 和 `spi_cpha_g`（各取 0 或 1）。它们不是运行时参数，而是在综合期定死，决定硬件里 SCLK 的空闲电平和数据切换时机。

#### 4.1.2 核心流程

把 SCLK 的一个完整周期拆成「**激活半周期**」和「**非激活半周期**」两段：

- **激活电平（active）**：与空闲电平相反的那一档。
- **非激活电平（inactive）**：等于空闲电平。

`ClkDivCnt` 从 0 数到 `clk_div_g/2 - 1`，正好用 `clk_div_g/2` 个系统时钟走完半个 SCLK 周期。于是：

\[ f_{\text{SCLK}} = \frac{f_{\text{clk}}}{\text{clk\_div\_g}} \]

例如 TB 里 `clk_div_g = 8`、系统时钟 100 MHz，则 SCLK = 100 MHz / 8 = 12.5 MHz，每个 SCLK 周期 8 个系统时钟（每半周期 4 个）。

状态机在 `ClkInact_s`（非激活半周期）和 `ClkAct_s`（激活半周期）之间来回切换，每完成「一非激活 + 一激活」就移出/移入 1 比特，`BitCnt` 加 1。CPHA 决定 MOSI 在**非激活半周期开头**改还是**激活半周期开头**改：

- **CPHA=0**：MOSI 在非激活半周期开头就改好，等激活沿到来时从机采样 → 在 leading 沿采样。
- **CPHA=1**：MOSI 在激活半周期开头改，从机在下一个非激活沿采样 → 在 trailing 沿采样。

#### 4.1.3 源码精读

SCLK 电平由一个小函数 `GetClockLevel` 决定，CPOL=0 时激活=‘1’、非激活=‘0’，CPOL=1 时反过来：

[psi_common_spi_master.vhd:85-100](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_spi_master.vhd#L85-L100) —— 据 CPOL 把「激活/非激活」映射成具体电平。

分频阈值常量把 `clk_div_g` 折成半周期计数值：

[psi_common_spi_master.vhd:60](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_spi_master.vhd#L60) —— `ClkDivThres_c = clk_div_g/2 - 1`，计数到它表示半周期走完。

`ClkInact_s` 里 SCLK 拉到非激活电平，并在 CPHA=0 时提前把 MOSI 改好：

[psi_common_spi_master.vhd:153-182](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_spi_master.vhd#L153-L182) —— 非激活半周期：更新 SCLK、按 CPHA 决定改 MOSI 还是移位、计数到阈值后判断是否比特传完。

`ClkAct_s` 把 SCLK 拉到激活电平，并在 CPHA=1 时才改 MOSI：

[psi_common_spi_master.vhd:184-205](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_spi_master.vhd#L184-L205) —— 激活半周期：SCLK 激活、`BitCnt` 加 1、完成后切回 `ClkInact_s`。

注意 `SftComp_s`（shift compensate）这个状态：它在 CPHA=0 时**多移一位**，弥补「CPHA=0 第一拍 MOSI 必须在 leading 沿之前就绪」的需求：

[psi_common_spi_master.vhd:146-151](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_spi_master.vhd#L146-L151) —— 仅 CPHA=0 时预先移位，把第一个待发比特取到 `MosiNext`。

#### 4.1.4 代码实践

1. **目标**：通过阅读 TB，确认四种模式各对应怎样的 SCLK/采样沿关系。
2. **步骤**：
   - 打开 `testbench/psi_common_spi_master_tb/psi_common_spi_master_tb.vhd`，看 `p_spi` 进程里等待 SCLK 沿的逻辑（约 272–298 行）。
   - 对照 `sim/config.tcl` 中 `psi_common_spi_master_tb` 的 6 组 generic（见 362–368 行），四线模式覆盖了 mode 0/1/2/3。
3. **观察**：TB 里 `p_spi` 用 `wait until rising_edge(spi_sck_o)` / `falling_edge(spi_sck_o)` 在不同 CPOL/CPHA 下选择「采样沿」，这正是模式表的代码化体现。
4. **预期结果**：四组模式组合（CPOL×CPHA）TB 全部通过、无 `###ERROR###`。
5. 待本地验证：仿真实际波形。

#### 4.1.5 小练习与答案

**练习 1**：若 `spi_cpol_g=1`，SCLK 空闲时是高还是低？`GetClockLevel(false)` 返回什么？
**答案**：空闲为高（CPOL=1）。`GetClockLevel(false)` 返回非激活电平 = ‘1’。

**练习 2**：为什么 CPHA=0 需要 `SftComp_s` 这个补偿状态，CPHA=1 却不需要？
**答案**：CPHA=0 要求 MOSI 在 leading 沿之前就稳定，故必须在第一拍 SCLK 翻转前先把首比特取出；CPHA=1 是在 leading 沿才改 MOSI，首比特可在正常流程里取，无需预移位。

---

### 4.2 位序与握手（lsb_first、start/busy/done）

#### 4.2.1 概念说明

两个问题要讲清：

- **位序**：一串 N 比特数据，先发最高位（MSB first）还是最低位（LSB first）？SPI 不强制，主从必须一致。`lsb_first_g` 用布尔值在综合期二选一。
- **并行侧握手**：用户逻辑怎么触发一次传输、怎么知道传完、怎么拿回数据？这里**不用** AXI-S 的 VLD/RDY，而是一套更简单的 `start_i` / `busy_o` / `done_o` 脉冲约定。

#### 4.2.2 核心流程

并行侧一次完整传输的时序（见组件文档 fig1 的「Parallel interface signal behavior」）：

1. 用户确认 `busy_o = '0'`。
2. 把待发数据放到 `dat_i`、把目标从机号放到 `slave_i`，拉高 `start_i` **一个**系统时钟周期。
3. 主机在 `start_i=‘1’` 那拍采样 `dat_i`，拉高 `busy_o`，选中从机（`spi_cs_n_o` 对应位拉低）。
4. SCLK 开始翻转，逐比特收发。
5. 传完后 `done_o` 拉高**一拍**，`dat_o` 此时有效，`busy_o` 回到 ‘0’。

注意 `start_i` 只有在 `busy_o=‘0’` 时才被接受；传输途中拉 `start_i` 会被忽略（TB 专门测了这一点）。

#### 4.2.3 源码精读

位序由 `ShiftReg` 过程用一个 `if lsb_first_g then` 分支实现：

[psi_common_spi_master.vhd:102-114](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_spi_master.vhd#L102-L114) —— LSB 先发时取最低位、整体右移；MSB 先发时取最高位、整体左移；`InputBit`（来自 MISO）补进空出的位置。

`Idle_s` 里采样 `dat_i`、启动传输：

[psi_common_spi_master.vhd:133-145](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_spi_master.vhd#L133-L145) —— `start_i=‘1’` 时锁存数据、置位 `busy_o`、按 `slave_i` 拉低对应 CS。

传输结束、产生 `done_o` 与回读数据：

[psi_common_spi_master.vhd:207-218](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_spi_master.vhd#L207-L218) —— `CsHigh_s` 计满 `cs_high_cycles_g` 后回到 `Idle_s`，拉 `done_o` 一拍、把移位寄存器内容送到 `dat_o`。

#### 4.2.4 代码实践

1. **目标**：验证 MSB/LSB 两种位序下收发数据正确。
2. **步骤**：看 TB 的常量 `MosiWords_c` / `MisoWords_c`（86–87 行）与 `p_spi` 进程里 `lsb_first_g` 分支（286–304 行）。`p_stim` 发送 `MosiWords_c`，`p_spi` 把收到的 MOSI 移位拼回，与 `ExpectedSlaveRx` 比对。
3. **观察**：`lsb_first_g=true` 时，从机模型先把 `ShiftRegTx_v(0)` 挪到 MISO、整体右移；`false` 时取最高位、整体左移。
4. **预期结果**：`config.tcl` 中 `-glsb_first_g=true` 与 `=false` 两组都通过自检。
5. 待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：用户在 `busy_o=‘1’` 时拉 `start_i`，会发生什么？
**答案**：因为启动逻辑在 `Idle_s` 状态里，而 `busy_o=‘1’` 时状态机不在 `Idle_s`，`start_i` 被忽略；TB 第三段专门验证传输途中发 start 不触发新传输。

**练习 2**：`done_o` 高电平持续几个系统时钟？
**答案**：恰好一拍。`p_comb` 里 `v.done_o := '0'` 是默认值，只有 `CsHigh_s` 末拍置 ‘1’，下一拍又被默认值清回 ‘0’。

---

### 4.3 可配置位宽：spi_master_cfg 与 trans_width_i

#### 4.3.1 概念说明

`spi_master` 的 `trans_width_g` 在综合期定死，一个实例只能跑一种位宽。但很多 SPI 从器件（例如 ADC）会在一次会话里先用短命令字、再读长数据字，位宽会变。`spi_common_spi_master_cfg` 正是为这种场景做的变体：综合期只定一个**上限** `max_trans_width_g`，真实位宽由运行时端口 `trans_width_i` 每次传输指定。

代价是：它**去掉了**三线 SPI 相关端口和 latch enable，只保留最常见的四线 SPI 功能。

#### 4.3.2 核心流程

- 上电时，硬件按 `max_trans_width_g` 分配最大宽度的移位寄存器。
- 每次启动传输时，`start_i=‘1’` 那拍把 `trans_width_i` 锁存进 record，本次传输就按这个长度移位。
- 状态机里 `BitCnt` 与锁存的 `trans_width_i` 比较（而非与常量比较），传满指定比特数即结束。

#### 4.3.3 源码精读

新增的运行时位宽端口（注意位宽是 `log2ceil(max_trans_width_g)+1`，能表示到上限值）：

[psi_common_spi_master_cfg.vhd:43](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_spi_master_cfg.vhd#L43) —— `trans_width_i` 指示本次实际传输位宽。

启动时把位宽锁存进 record：

[psi_common_spi_master_cfg.vhd:136-142](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_spi_master_cfg.vhd#L136-L142) —— `start_i=‘1’` 时 `v.trans_width_i := trans_width_i`，与数据一起被采样。

比特计数与运行时位宽比较（这是与固定版的**关键差异**）：

[psi_common_spi_master_cfg.vhd:165-173](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_spi_master_cfg.vhd#L165-L173) —— `if r.BitCnt = to_integer(unsigned(r.trans_width_i))` 判断传完，替代了固定版的 `r.BitCnt = trans_width_g`。

两个组件的对照：

| 维度 | spi_master | spi_master_cfg |
|:--|:--|:--|
| 分频 generic 名 | `clk_div_g` | `clock_divider_g`（带默认值 4）|
| 位宽 | `trans_width_g`（综合期定死）| `max_trans_width_g` + 运行时 `trans_width_i` |
| 数据端口 | `dat_i` / `dat_o` | `wr_dat_i` / `rd_dat_o` |
| 三线 SPI | 支持（`spi_tri_o` 等）| **不支持** |
| Latch enable | `spi_le_o` | 无 |
| CPOL/CPHA/lsb_first | 支持 | 支持 |

#### 4.3.4 代码实践

1. **目标**：观察同一个 `spi_master_cfg` 实例在不同 `trans_width_i` 下传不同位宽。
2. **步骤**：看 `sim/config.tcl` 里 `psi_common_spi_master_cfg_tb` 的 generic（195–203 行），其中 `max_trans_width_g` 取 8/16/24，覆盖「位宽上限可变」。
3. **观察**：TB 会给同一 DUT 喂不同 `trans_width_i`，确认移位长度随之改变而收发数据仍正确。
4. **预期结果**：6 组 generic 全部通过。
5. 待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`trans_width_i` 是「传完即用」还是必须配合 `start_i`？
**答案**：必须配合。它只在 `start_i=‘1’` 那拍被采样锁存（见 136–142 行），中途改 `trans_width_i` 不影响正在进行中的传输。

**练习 2**：为什么 `trans_width_i` 的位宽是 `log2ceil(max_trans_width_g)+1` 而不是 `log2ceil(max_trans_width_g)`？
**答案**：因为要能表示等于上限本身的值。例如 `max_trans_width_g=8` 时需表示 8，`log2ceil(8)=3` 只能编到 7，故多一位用 4 比特编码到 8。

---

### 4.4 三线 SPI：spi_tri_o 与读/写位

#### 4.4.1 概念说明

四线 SPI 里 MOSI 和 MISO 是两根独立线。**三线 SPI**（又叫 3-wire / single-bidirectional SPI）把收发合并到**一根双向线**（常仍叫 MOSI），节省一根引脚。代价是：主机先发命令、然后必须把驱动线**让出**（高阻/三态），由从机反向驱动同一根线回数据。

`spi_master`（固定版）支持三线 SPI，靠几个 generic 和一个 `spi_tri_o`（三态选择）端口实现；`spi_master_cfg` 不支持。

#### 4.4.2 核心流程

协议约定一个「命令字」结构：

- 最高位（`trans_width_g-1`）是 **R/W 位**，指示本次是读还是写。代码里 `IsRead := dat_i(trans_width_g - 1)`。
- `read_bit_pol_g` 约定「读」对应的电平（例如 ‘1’ 表示读）。
- `spi_data_pos_g` 是命令字里**数据段的起始位**，由此推出命令/地址占多少位 → `BitCntDataPos_c = trans_width_g - spi_data_pos_g`。

工作流程：主机先把命令/地址逐位移出；当 `BitCnt` 数到 `BitCntDataPos_c`（即命令位移完、进入数据段）且本次是「读」操作时，主机拉 `spi_tri_o` 到 `tri_state_pol_g`，把驱动权交给从机，自己改为在这根线上**采样**回读数据。

#### 4.4.3 源码精读

计算数据段起始对应的比特计数：

[psi_common_spi_master.vhd:61](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_spi_master.vhd#L61) —— `BitCntDataPos_c = trans_width_g - spi_data_pos_g`，标志「命令位移完、数据段开始」。

在非激活半周期（CPHA=0）切换三态、让出驱动线：

[psi_common_spi_master.vhd:156-166](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_spi_master.vhd#L156-L166) —— 当 `BitCnt = BitCntDataPos_c` 且 `IsRead = read_bit_pol_g` 时，`spi_tri_o := tri_state_pol_g`，通知外部 IO 把这根线切到从机驱动。

激活半周期（CPHA=1）做同样的切换：

[psi_common_spi_master.vhd:187-197](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_spi_master.vhd#L187-L197) —— CPHA=1 时三态切换发生在激活半周期开头。

传输结束、回到主机驱动：

[psi_common_spi_master.vhd:207-210](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_spi_master.vhd#L207-L210) —— `CsHigh_s` 里 `spi_tri_o := not tri_state_pol_g`，把驱动权收回。

#### 4.4.4 代码实践

1. **目标**：理解三线 SPI 下 `spi_tri_o` 的翻转时机。
2. **步骤**：在 `psi_common_spi_master_tb.vhd` 中确认 `spi_tri_o` 是 DUT 输出端口（77 行接出），TB 默认 generic `read_bit_pol_g=1`、`tri_state_pol_g=1`、`spi_data_pos_g=3`。
3. **观察**：当发送一个最高位为 ‘1’（读操作）的字时，`spi_tri_o` 应在移完前 3 个命令位（`BitCnt=trans_width_g-3`）后翻转到 ‘1’，让外部三态缓冲把线交给从机；传完再翻回。
4. **预期结果**：该 TB 在三线相关 generic 下能正确完成回环（注：四线模式下 `spi_tri_o` 不影响数据线，仅作状态指示）。
5. 待本地验证：实际三线场景需在顶层用一个受 `spi_tri_o` 控制的 IO 三态单元把 MOSI/MISO 合并到一根 pad。

#### 4.4.5 小练习与答案

**练习 1**：为什么三线 SPI 需要 `read_bit_pol_g` 和 `spi_data_pos_g`，四线 SPI 却不需要？
**答案**：四线 SPI 收发各走一线，无需让出驱动权。三线 SPI 必须知道「本次是读还是写」（`read_bit_pol_g`）以及「命令位何时结束、数据段何时开始」（`spi_data_pos_g`），才能在正确时刻把线交给从机。

**练习 2**：`spi_tri_o` 是直接驱动 FPGA pad 的三态使能吗？
**答案**：不是。它是一个普通输出信号，告诉**顶层**何时把外部三态缓冲（IO 三态原语）切到从机驱动；组件本身不实现真正的硬件高阻。

---

## 5. 综合实践

把本讲的 CPOL/CPHA、位序、握手和可配置位宽串起来：

**任务**：配置 `psi_common_spi_master` 为 **mode 1、MSB 优先**，说明一次 8 比特传输的 SCLK 与 CS 时序，并预测关键信号的周期。

1. **generic 设置**：`spi_cpol_g=0`、`spi_cpha_g=1`、`lsb_first_g=false`、`trans_width_g=8`、`clk_div_g=8`、`cs_high_cycles_g=12`、`slave_cnt_g=2`（与 TB 一致）。
2. **说明 SCLK 时序（mode 1）**：
   - 空闲为低（CPOL=0）。
   - 因 CPHA=1，MOSI 在激活半周期开头（SCLK 翻向高电平的 leading 沿）更新，从机在 trailing（下降）沿采样。
   - SCLK 频率 = 100 MHz / 8 = 12.5 MHz，每比特 8 个系统时钟。
3. **说明 CS 与 LE 时序**：
   - `start_i` 一拉，`spi_cs_n_o(slave_i)` 立即变低并保持到所有比特传完。
   - 最后一个比特传完时 `spi_le_o(slave_i)` 拉高（latch enable，通知从机锁存）。
   - 进入 `CsHigh_s` 后 `spi_cs_n_o` 回到全高，再过 `cs_high_cycles_g` 拍 `done_o` 脉冲一拍、`dat_o` 给出从机回传数据。
4. **验证方式**：跑 `sim/config.tcl` 里 `-gspi_cpol_g=0 -gspi_cpha_g=1 -glsb_first_g=false` 这一组合（364 行），用波形核对上面的描述。
5. **预测**：8 比特传输主体约占 `8 × 8 = 64` 个系统时钟，加上 `SftComp`（1 拍）和 `CsHigh`（12 拍）等开销，`busy_o` 高电平约 70 余拍（待本地验证精确值）。

## 6. 本讲小结

- SPI 四模式由 `spi_cpol_g`（空闲电平）和 `spi_cpha_g`（采样沿）组合而成，组件用 `GetClockLevel` 函数和 `ClkInact/ClkAct` 两个状态生成 SCLK。
- SCLK 频率 = 系统时钟 / `clk_div_g`，半周期计数阈值 `ClkDivThres_c = clk_div_g/2 - 1`。
- 并行侧用 `start_i`/`busy_o`/`done_o` 脉冲约定，而非 AXI-S 的 VLD/RDY；`done_o` 恰好一拍。
- 位序由 `lsb_first_g` 在 `ShiftReg` 过程里二选一，MSB 先发取高位左移、LSB 先发取低位右移。
- `spi_master_cfg` 把位宽下放到运行时端口 `trans_width_i`，代价是去掉三线 SPI 与 latch enable。
- 三线 SPI 靠 `spi_tri_o` + `read_bit_pol_g` + `spi_data_pos_g` 在命令位移完后把驱动权让给从机。
- 全程沿用 u7-l1 的二进程 record 设计法：`r`/`r_next` + `p_comb` + `p_seq`。

## 7. 下一步学习建议

- 下一讲 [u9-l2 i2c_master](u9-l2-i2c-master.md) 讲另一种慢速串行总线 I2C，可对比两者「主机产生时钟、片选/寻址方式、握手」的差异。
- 若对 AXI 总线感兴趣，可跳读 [u9-l3 axi_master_simple](u9-l3-axi-master-simple.md)，体会 AXI 多通道握手与 SPI 单比特流的区别。
- 想加深「分频计数器产生节拍」的直觉，可回顾 [u6-l1 选通与节拍生成](u6-l1-strobe-tick-generator.md)，对照 `strobe_generator` 与本组件 SCLK 生成。
- 建议阅读 `testbench/psi_common_spi_master_tb/psi_common_spi_master_tb.vhd` 的 `p_spi` 进程，它是一个用纯 VHDL 写的「SPI 从机模型」，是理解主从配合的最佳参考。
