# DRP 动态重配置端口

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 **DRP（Dynamic Reconfiguration Port，动态重配置端口）** 是什么、它解决了 Xilinx PCIe 硬核的哪个痛点。
- 画出主机软件经 USB → `pcileech_fifo` 的 `rw` 寄存器 → `IfPCIeFifoCore` 接口 → `pcie_7x_0` 硬核 DRP 引脚的**完整间接驱动路径**。
- 读懂 `rw` 寄存器中 DRP 相关字段的字节布局（`drp_di`、`drp_addr`、触发位、`WAIT_COMPLETE`）。
- 讲清 DRP 的「**两级触发**」状态机：`rw` 触发位（一次性脉冲）与 `rwi_drp_*` 黏滞位（保持到完成）如何配合，以及主机如何经 `ro` 寄存器轮询完成状态并回读数据。
- 理解 `pcileech_com.sv` 的 `initial_rx` 在 **PCIe 核上线之前**注入 DRP/配置动作的典型用法与正确时序。

本讲是专家层「时序、约束与 Xilinx IP」单元的一篇，承接 [u2-l5 命令/控制寄存器文件](u5-l4-drp-dynamic-reconfiguration.md)（主机命令如何写 `rw` 寄存器）与 [u3-l1 PCIe 核心封装](u5-l4-drp-dynamic-reconfiguration.md)（`pcie_7x_0` IP 的端口分组），把这两条线在 DRP 这个点上交汇。

## 2. 前置知识

在进入 DRP 之前，先用三段话补齐背景。

**Xilinx 7 系列 PCIe 硬核的「配置困境」。** `pcie_7x_0` 是 Xilinx 集成在 Artix-7 里的 PCIe 硬核 IP。它的很多内部参数（如收发器发送摆幅、接收均衡、链路速率上限、部分 PHY 寄存器）在 Vivado GUI 里点好之后，会被烘焙进 `.xci` 并最终固化进比特流，**运行时本应不可改**。如果想在运行时调这些参数（调试链路质量、适配不同主板），唯一的合法通道就是 **DRP**——硬核对外暴露的一组寄存器读写端口，绕过 GUI、直接读写硬核内部的配置寄存器空间。

**主机软件看不到 DRP 引脚。** DRP 是 FPGA 片内信号，主机（PCILeech/LeechCore）只能通过 USB 与板卡通信。因此工程必须把「主机命令」翻译成「DRP 时序」，中间的翻译官就是 `pcileech_fifo` 的控制寄存器文件。这套「**间接驱动**」是本讲的主线。

**`rw`/`ro` 寄存器文件回顾。** 如 [u2-l5](u5-l4-drp-dynamic-reconfiguration.md) 所述，`pcileech_fifo` 维护两张表：`ro`（320 位，只读，硬件状态镜像）与 `rw`（240 位，可读写，主机控制面板）。主机用 Command 路（MAGIC type=11）的「字节地址 + 16 位窗口 + 逐位掩码」协议写 `rw`、读 `ro`。DRP 的所有控制位和数据都嵌在这两张表里。

> 一个关键术语：**DRP 运行在 `clk_sys`（100MHz 系统时钟）域，不是 `clk_pcie` 域**。这一点稍后会反复用到，也解释了为什么 DRP 通路**不需要跨时钟 FIFO**（见 [u5-l1 跨时钟域设计](u5-l1-clock-domain-crossing.md)）。

## 3. 本讲源码地图

| 文件 | 在本讲的作用 |
| --- | --- |
| [PCIeSquirrel/src/pcileech_header.svh](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_header.svh) | 定义 `IfPCIeFifoCore` 接口，即承载 6 个 DRP 信号 + 2 个复位信号的「契约电缆」。 |
| [PCIeSquirrel/src/pcileech_fifo.sv](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv) | DRP 的**控制面**：`rw`/`ro` 寄存器映射、两级触发状态机、`WAIT_COMPLETE` 门控。 |
| [PCIeSquirrel/src/pcileech_pcie_a7.sv](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv) | DRP 的**物理面**：把 `IfPCIeFifoCore` 的信号接到 `pcie_7x_0` 硬核的 DRP 引脚，并固定 `pcie_drp_clk = clk_sys`。 |
| [PCIeSquirrel/src/pcileech_com.sv](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_com.sv) | `initial_rx` 上电注入机制：在 PCIe 核上线之前塞入 DRP/配置动作。 |

## 4. 核心概念与源码讲解

### 4.1 DRP 是什么：运行时改写 PCIe 硬核内部寄存器

#### 4.1.1 概念说明

DRP 是 Xilinx 给 7 系列 PCIe 硬核（以及 GTP/GTX 收发器）开的一扇「后门」：一组简单的寄存器读写端口，让你在比特流已经加载、链路已经在跑之后，**仍能读写硬核内部的配置寄存器**。

可以把硬核内部想象成一个 16 位宽的小 SRAM，每个 9 位地址（`drp_addr`）对应一个 16 位寄存器。读：给地址、置 `drp_en`，等 `drp_rdy`，从 `drp_do` 取数。写：给地址、给数据 `drp_di`、置 `drp_en` 与 `drp_we`，等 `drp_rdy`。和 [u3-l2](u5-l4-drp-dynamic-reconfiguration.md) 讲过的 `cfg_mgmt` 读写配置空间的握手几乎同构，只是 DRP 面向的是**硬核内部 PHY/PLL 级寄存器**，而非 PCIe 协议层配置空间。

#### 4.1.2 核心流程（间接驱动链路）

DRP 不能被主机直接拉线，必须经过工程内部的翻译。完整链路如下（下行＝主机→硬核，上行＝硬核→主机）：

```
主机软件(LeechCore device_fpga.c)
   │  64 位 Command 包（MAGIC 0x77 / type 11）
   ▼  USB3 → FT601
pcileech_com.sv  (32→64 位、跨 clk_com→clk)
   │  com_dout[63:0]
   ▼  MAGIC 路由 type=11 → Command
pcileech_fifo.sv
   │  ① 写 rw[208+:16] = drp_di        (数据)
   │  ② 写 rw[224+:9]  = drp_addr      (地址)
   │  ③ 置 rw[21]      = DRP_WR_EN     (触发)
   │  ④ 组合赋值 → dpcie.drp_en/we/addr/di
   │  ⑤ 状态机: rw触发位 → rwi 黏滞位(等完成)
   ▼  IfPCIeFifoCore 接口 (dpcie)
pcileech_pcie_a7.sv
   │  drp_clk = clk_sys
   ▼
pcie_7x_0 硬核 DRP 端口
   │  处理后置 drp_rdy=1, drp_do=读回值
   ▼  (原路返回)
pcileech_fifo.sv: rwi_drp_data <= dpcie.drp_do
   │  映射进 ro[271:256] / ro[21] (完成标志)
   ▼
主机读 ro 寄存器 → 拿到完成状态与读回数据
```

记住一个全局要点：**整条 DRP 通路只跨越一个时钟域——`clk_sys`**。因为 `pcie_drp_clk` 直接绑到 `clk_sys`（见 4.2.3），`pcileech_fifo` 本身也跑在 `clk`（即 `clk_sys`）上，所以两端同域，**省掉了双时钟 FIFO**。这与 [u3-l2](u5-l4-drp-dynamic-reconfiguration.md) 中 `cfg_mgmt` 必须用 `fifo_32_32_clk2` 跨域的情况不同。

### 4.2 DRP 寄存器映射

这是本讲第一个最小模块。我们要精确定位 `rw`/`ro` 两张表里所有跟 DRP 有关的位。

#### 4.2.1 概念说明

DRP 的「地址、数据、方向、完成状态」全部编码进 `pcileech_fifo` 的寄存器文件。主机不需要任何特殊命令，**用普通的读写 `rw`/`ro` 协议**就能驱动一次 DRP 访问。这种「把硬件动作折叠进一张寄存器表」的设计，是 pcileech-fpga 控制面的统一风格（参见 [u2-l5](u5-l4-drp-dynamic-reconfiguration.md) 的 `_pcie_core_config` 段搬运）。

#### 4.2.2 核心流程：字段总览

先把 DRP 相关字段列成一张表（位宽/字节偏移均来自源码注释）：

| 字段名 | 所在表 | 位域 | 字节偏移 | 方向 | 含义 |
| --- | --- | --- | --- | --- | --- |
| `drp_di`（数据入） | `rw` | `[208+:16]` | `+0x1A` | 主机写 | 要写入硬核的 16 位数据 |
| `drp_addr`（地址） | `rw` | `[224+:9]` | `+0x1C` | 主机写 | 9 位 DRP 寄存器地址 |
| `DRP_RD_EN`（读触发） | `rw` | `[20]` | `+0x02` bit4 | 主机写 | 置 1 触发一次 DRP **读** |
| `DRP_WR_EN`（写触发） | `rw` | `[21]` | `+0x02` bit5 | 主机写 | 置 1 触发一次 DRP **写** |
| `WAIT_COMPLETE` | `rw` | `[18]` | `+0x02` bit2 | 主机写 | =1 时，DRP 未完成则暂停接收下一条命令 |
| `drp_do`（数据出） | `ro` | `[271:256]` | `+0x20` | 主机读 | DRP 读回的 16 位数据 |
| DRP 读忙/完成标志 | `ro` | `[20]` | `+0x02` bit4 | 主机读 | 镜像 `rwi_drp_rd_en`（读进行中） |
| DRP 写忙/完成标志 | `ro` | `[21]` | `+0x02` bit5 | 主机读 | 镜像 `rwi_drp_wr_en`（写进行中） |

注意一个容易踩的坑：`rw[20]/rw[21]` 与 `ro[20]/ro[21]` **位号相同但属于两张不同的表**，含义也微妙不同——`rw[20]/rw[21]` 是主机写的「一次性触发位」，`ro[20]/ro[21]` 是硬件回送的「黏滞状态位」（见 4.3）。

#### 4.2.3 源码精读

先看 `IfPCIeFifoCore` 这条「契约电缆」声明了哪些 DRP 信号，以及 `mp_fifo`/`mp_pcie` 两个 modport 如何规定方向：

[pcileech_header.svh:244-265](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_header.svh#L244-L265) —— 定义 `drp_en`/`drp_we`/`drp_addr[8:0]`/`drp_di[15:0]` 为 fifo→pcie 方向，`drp_rdy`/`drp_do[15:0]` 为 pcie→fifo 方向。`mp_fifo` 视角下前者是 `output`、后者是 `input`，与 `mp_pcie` 严格互补。

接着看 `rw` 表里 DRP 字段的初始化（上电默认值）与局部参数定义：

[pcileech_fifo.sv:207-210](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L207-L210) —— 把三个关键位号起好名字：`RWPOS_WAIT_COMPLETE=18`、`RWPOS_DRP_RD_EN=20`、`RWPOS_DRP_WR_EN=21`。

[pcileech_fifo.sv:295-298](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L295-L298) —— `rw[208+:16]`（drp_di）与 `rw[224+:9]`（drp_addr）上电清零；注释里写明了字节偏移 `+01A` / `+01C`，与上表一致。

再看 `rw` 触发位的组合赋值（这是 DRP 控制面到物理面的「出口」）：

[pcileech_fifo.sv:319-322](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L319-L322) —— 四行赋值把 `rw` 翻译成 `dpcie.drp_*`。要点：

- `drp_en = DRP_WR_EN | DRP_RD_EN`（读或写都拉高使能）；
- `drp_we = DRP_WR_EN`（仅写时拉高写使能）；
- `drp_addr`、`drp_di` 直接从 `rw` 整段搬运，**没有任何时钟域跨越**。

最后看 `ro` 表如何把 DRP 状态和数据回送给主机：

[pcileech_fifo.sv:229-232](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L229-L232) —— `ro[20]=rwi_drp_rd_en`、`ro[21]=rwi_drp_wr_en`（这两个 `rwi_*` 是 4.3 要讲的黏滞位）。

[pcileech_fifo.sv:244-245](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L244-L245) —— `ro[271:256] = rwi_drp_data`，注释 `+020: DRP: pcie_drp_do`，即主机读 `ro` 字节 `0x20` 就能拿到 DRP 读回值。

物理面这边，`pcileech_pcie_a7.sv` 把 `dpcie` 信号接到硬核引脚，并固定时钟域：

[pcileech_pcie_a7.sv:246-253](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L246-L253) —— `pcie_7x_0` 的 7 个 DRP 引脚逐一对接 `dfifo_pcie.drp_*`。**注意第 247 行 `pcie_drp_clk = clk_sys`**，以及第 246 行那句关键注释：

> `// DRP - clock domain clk_100 - write should only happen when core is in reset state ...`

这句注释是整条 DRP 通路最重要的**使用约束**：DRP 写应当发生在核处于复位态时（详见 4.4 的 `initial_rx` 用法）。`clk_100` 即 `clk_sys`，再次印证 DRP 与 fifo 同域。

#### 4.2.4 代码实践：定位一次 DRP 写需要的三个 `rw` 字段

1. **实践目标**：不靠记忆，纯靠源码注释，把「发起一次 DRP 写」要写的三个 `rw` 字段及其字节地址查出来。
2. **操作步骤**：
   - 打开 [pcileech_fifo.sv:295-298](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L295-L298)，确认 `drp_di` 在字节 `0x1A`、`drp_addr` 在字节 `0x1C`。
   - 打开 [pcileech_fifo.sv:268-271](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L268-L271)，确认 `WAIT_COMPLETE`/`DRP_RD_EN`/`DRP_WR_EN` 都在字节 `0x02`（`rw[16..23]`）里。
   - 结合 [u2-l5](u5-l4-drp-dynamic-reconfiguration.md) 的命令包格式：地址字段低 15 位是字节地址（左移 3 位得 bit 地址），命令一次写 16 位窗口。
3. **需要观察的现象**：`drp_addr` 虽是 9 位，但它落在 `rw[224+:9]`，而一条命令写的是 16 位对齐窗口（字节 `0x1C` 对应 `rw[224+:16]`），所以**一次命令写字节 `0x1C` 即可覆盖全部 9 位地址**（高位多出来的 7 位是注释里的 `SLACK`）。
4. **预期结果**：你能写出三条命令的「字节地址 + 掩码 + 值」三元组——
   - 写 `0x1A`，掩码 `0xFFFF`，值 = 想写的 16 位 `drp_di`；
   - 写 `0x1C`，掩码 `0xFFFF`（或 `0x01FF`），值 = 9 位 `drp_addr`；
   - 写 `0x02`，掩码 `0x0020`（bit5），值 `0x0020`，即置位 `DRP_WR_EN`。
5. **待本地验证**：具体某个 `drp_addr` 写下去会改变硬核什么行为，取决于 Xilinx `pcie_7x_0` 的 DRP 地址映射（IP 版本相关、不在本仓库内），需对照 Vivado 生成的 IP 文档确认。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `drp_addr` 用 9 位而不是 16 位？
**答案**：硬核 DRP 地址空间就是 9 位（512 个 16 位寄存器），`drp_addr[8:0]` 直接对接 `pcie_7x_0` 的 `pcie_drp_addr[8:0]`（见 [pcileech_pcie_a7.sv:250](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L250)）；多出来的位宽只是命令协议 16 位对齐的副作用，落在 `SLACK`。

**练习 2**：主机想发起一次 DRP **读**，应该置 `rw` 的哪个位？读回值从 `ro` 的哪个字节取？
**答案**：置 `rw[20]`（`DRP_RD_EN`，字节 `0x02` bit4）；读回值从 `ro` 字节 `0x20`（`ro[271:256]=rwi_drp_data`）取，完成标志看 `ro[21]`→`rwi_drp_wr_en` 回落……注意：**读操作**的完成标志应看 `ro[20]`（`rwi_drp_rd_en`），而非 `ro[21]`。

### 4.3 DRP 触发与完成状态机

这是本讲第二个最小模块，也是最容易读错的地方。关键在于理解「**两级触发**」。

#### 4.3.1 概念说明

`dpcie.drp_en` 是组合信号 `rw[20] | rw[21]`。如果只靠 `rw` 触发位，会出现两难：

- 主机把 `rw[21]` 置 1 后，若 `rw[21]` 一直保持 1，`drp_en` 就**持续高电平**，硬核会被反复触发写，这是灾难。
- 若主机写完立刻把 `rw[21]` 清 0，又得自己精确把握时序，且无法表达「正在进行中、请等待」。

工程的解法是**两级触发**：

- **第一级 `rw[20]/rw[21]`**：一次性触发位。状态机一旦采样到它被置 1，**同一拍**就把它清零，并生成一个短暂的 `drp_en` 脉冲交给硬核。
- **第二级 `rwi_drp_rd_en/rwi_drp_wr_en`**：黏滞（sticky）状态位。它镜像「当前是否有一次 DRP 操作在途」，从触发那一刻起保持 1，直到硬核回 `drp_rdy` 才清 0。主机轮询 `ro[20]/ro[21]`（就是这两个 `rwi_*`）来判断完成。

#### 4.3.2 核心流程：状态机伪代码与时序

DRP 状态机位于 `pcileech_fifo` 主 `always` 块的尾部，逻辑可归纳为：

```
每个 clk_sys 上升沿：
    if (drp_rdy == 1):                     # 硬核报告完成
        rwi_drp_rd_en <= 0
        rwi_drp_wr_en <= 0
        rwi_drp_data  <= drp_do            # 锁存读回值
    elif (rw[DRP_RD_EN] or rw[DRP_WR_EN]): # 主机刚置了触发位
        rw[DRP_RD_EN] <= 0                 # 一次性清零(第一级)
        rw[DRP_WR_EN] <= 0
        rwi_drp_rd_en <= rwi_drp_rd_en | rw[DRP_RD_EN]   # 转移到黏滞位(第二级)
        rwi_drp_wr_en <= rwi_drp_wr_en | rw[DRP_WR_EN]
    # else: 保持现状(黏滞位继续 = 1, 等待 drp_rdy)
```

与之配套的 `WAIT_COMPLETE` 门控，控制「DRP 进行中时是否暂停接收下一条命令」：

```
drp_in_flight = rwi_drp_rd_en | rwi_drp_wr_en | rw[DRP_RD_EN] | rw[DRP_WR_EN]
cmd_rx_rd_en  = tickcount64[1]               # 每隔一拍读一次(限速)
              & ( ~rw[WAIT_COMPLETE]          # 要么主机关掉了等待
                | ~drp_in_flight )            # 要么当前没有 DRP 在途
```

一次主机发起的 DRP 写，时序大致如下（`drp_rdy` 何时拉高取决于硬核处理时长，图示为若干拍）：

```
周期:        N        N+1      N+2      ...      K         K+1
rw[21](WR):  0→1写    1        0(已清)   0        0         0
drp_en:      0        1脉冲    0        0        0         0
rwi_wr_en:   0        0        1        1        1→0清     0
drp_rdy:     1(闲)    0(忙)    0        0        1         1
drp_do:      xx       xx       xx       xx       有效      有效
rwi_drp_data:                          xx       锁存读回   保持
ro[21]主机看到: 0      0        1(忙)    1(忙)    1→0(完成) 0
```

要点：`drp_addr`/`drp_di` 来自 `rw[224+:9]`/`rw[208+:16]`，**状态机从不清这两个字段**，所以它们在整个操作期间保持稳定——这对 DRP 这类「使能有效沿上锁存地址/数据」的协议是安全的。

#### 4.3.3 源码精读

DRP 状态机本体：

[pcileech_fifo.sv:416-429](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L416-L429) —— `if (dpcie.drp_rdy)` 分支捕获 `drp_do` 并清黏滞位；`else if (rw[RWPOS_DRP_RD_EN] | rw[RWPOS_DRP_WR_EN])` 分支做「第一级→第二级」转移。注意用的是 `rwi_drp_rd_en | rw[RWPOS_DRP_RD_EN]` 这种「或」写法，保证即使 `rwi_*` 已为 1 也不会被误清。

`WAIT_COMPLETE` 门控：

[pcileech_fifo.sv:330-331](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L330-L331) —— `cmd_rx_rd_en_drp` 汇总四级「在途」来源（`rwi` 与 `rw` 的读/写各两位）；`cmd_rx_rd_en` 在 `rw[18]=1`（默认）且 DRP 在途时被强制为 0，命令 FIFO 停止出队，避免下一条 DRP 命令与未完成的当前操作叠加。

黏滞位与数据寄存器的声明：

[pcileech_fifo.sv:218-220](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L218-L220) —— `rwi_drp_rd_en`、`rwi_drp_wr_en`、`rwi_drp_data[15:0]` 三个寄存器。`rwi_` 前缀表示「内部（非用户直接可写）」，它们只能由状态机写、由 `ro` 对外读。

#### 4.3.4 代码实践：追踪一次主机发起的 DRP 写的完整路径

1. **实践目标**：把本讲规格里要求的「从 `rw[208+:16]`(di) / `rw[224+:9]`(addr) → `dpcie.drp_en/drp_we` → `dpcie.drp_rdy` 回读完成」整条路径，在源码里逐跳点出来。
2. **操作步骤**（源码阅读型实践）：
   - **第 1 跳·命令入队**：主机 Command 包经 `dcom.com_dout` → [pcileech_fifo.sv:74](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L74) 的 `_cmd_rx_wren`（type=11）写入命令 FIFO `i_fifo_cmd_rx`（[pcileech_fifo.sv:333-343](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L333-L343)）。
   - **第 2 跳·解析与写 rw**：命令出队为 `cmd_rx_dout`，经 [pcileech_fifo.sv:346-354](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L346-L354) 拆出地址/值/掩码，命中 `in_cmd_write` 后由 [pcileech_fifo.sv:405-410](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L405-L410) 的 16 位循环写进 `rw`，把 `rw[208+:16]`、`rw[224+:9]`、`rw[21]` 依次置好。
   - **第 3 跳·组合翻译**：[pcileech_fifo.sv:319-322](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L319-L322) 把 `rw` 翻译成 `dpcie.drp_en/drp_we/drp_addr/drp_di`。
   - **第 4 跳·跨接口到硬核**：`dpcie`（即 `IfPCIeFifoCore`）经顶层连到 `pcileech_pcie_a7` 的 `dfifo_pcie`，再由 [pcileech_pcie_a7.sv:248-253](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L248-L253) 接到 `pcie_7x_0` 的 DRP 引脚。
   - **第 5 跳·完成回读**：硬核处理完置 `drp_rdy=1` 并给出 `drp_do`，回到 [pcileech_fifo.sv:417-422](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L417-L422) 锁存进 `rwi_drp_data`，清掉黏滞位。
   - **第 6 跳·主机可见**：`rwi_drp_data` 经 [pcileech_fifo.sv:245](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L245) 映射到 `ro[271:256]`，完成标志经 [pcileech_fifo.sv:231](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L231) 映射到 `ro[21]`。主机随后读 `ro` 拿到结果。
3. **需要观察的现象**：在第 2 跳和第 5 跳之间，由于 [pcileech_fifo.sv:330-331](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L330-L331) 的门控，命令 FIFO 的 `rd_en` 被压低为 0，期间不会再消费下一条命令——这就是 `WAIT_COMPLETE` 的实际效果。
4. **预期结果**：你能复述出「触发位是组合生效、状态机下一拍清零、黏滞位顶上直到 `drp_rdy`」这三段，并指出主机判断完成的唯一可靠信号是 `ro[20]/ro[21]` 回落为 0。
5. **待本地验证**：`drp_rdy` 相对于 `drp_en` 脉冲的确切延时周期数取决于 `pcie_7x_0` IP 内部实现（Xilinx 黑盒），上表中的周期数仅供示意，需在仿真或硬件上以 ILA 抓取确认。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `rw[18]`（`WAIT_COMPLETE`）清成 0 会怎样？
**答案**：`cmd_rx_rd_en` 不再被 `drp_in_flight` 门控（见 [pcileech_fifo.sv:331](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L331)），命令 FIFO 会继续出队。若主机连发两条 DRP 命令，第二条可能在第一条的 `drp_rdy` 还没回来时就被处理，`drp_addr/drp_di` 被覆盖，造成丢操作或写错地址。默认值 `rw[18]=1` 是安全的。

**练习 2**：状态机里捕获读回值用的是 `rwi_drp_data`，为什么不直接把 `drp_do` 接到 `ro`？
**答案**：`drp_do` 只在 `drp_rdy` 拉高的那一拍有效；直接接 `ro` 会让主机在读到之前数据就消失。用寄存器 `rwi_drp_data` 锁存（[pcileech_fifo.sv:421](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L421)）后，读回值会**稳定保持到下一次 DRP 操作**，主机可以从容读。

### 4.4 核上线前的 DRP 预置：`initial_rx` 机制

#### 4.4.1 概念说明

[pcileech_pcie_a7.sv:246](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L246) 的注释明确写着「write should only happen when core is in reset state」。也就是说，**DRP 写最安全的时机是 PCIe 核还处在复位态、链路尚未训练时**。可一旦主机软件（PCILeech）跑起来，核往往早就上线了——这是个「先有鸡还是先有蛋」的问题：主机还没介入时，谁来替我做上线前的 DRP 配置？

答案在 `pcileech_com.sv`：上电瞬间，com 模块**伪造**几条主机命令注入 fifo，等价于主机在核上线前就发了 DRP/配置动作。这就是 `initial_rx` 机制（在 [u2-l2](u5-l4-drp-dynamic-reconfiguration.md) 已见过它如何把核拉上线，本讲补全它的 DRP 用途）。

#### 4.4.2 核心流程

`initial_rx` 是一个 5 元素的 64 位数组。上电后由 `tickcount64` 计数在第 16~20 拍依次「播放」这 5 条命令，每条命令都会被 fifo 当成普通主机命令处理（走 MAGIC 路由、写 `rw`）：

- 槽 `[0..3]`：默认全零的**占位符**，预留给用户填入自己的 DRP/配置/TLP 动作。
- 槽 `[4]`：固定值 `64'h00000003_80182377`，作用是**把 PCIe 核从复位态拉上线**（写 `rw` 字节 `0x18`、掩码 `0x0300`、值 `0`，清掉 `rw[200]` PCIE CORE RESET 与 `rw[201]` SUBSYS RESET，详见 [u2-l2](u5-l4-drp-dynamic-reconfiguration.md)）。

顺序至关重要：**前 4 条（用户 DRP/配置）必须在第 5 条（核上线）之前**，这正好满足「核处于复位态时做 DRP 写」的约束。

#### 4.4.3 源码精读

`initial_rx` 的注释块是全仓最权威的 DRP 用法说明：

[pcileech_com.sv:56-79](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_com.sv#L56-L79) —— 注释列出三类典型用途：「send some initial TLP」「set initial VID/PID」「write to DRP memory space to alter the core」；数组前 4 项为零占位，第 5 项是上线命令，并注明「This should ideally be done after DRP&Config actions are completed - but before sending PCIe TLPs」。

注入的时序控制：

[pcileech_com.sv:89-90](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_com.sv#L89-L90) —— `initial_rx_valid` 在 `tickcount64` 落在 `[16, 16+5)` 区间时为真；`initial_rx_data` 按计数索引取数组对应项。这两行配合 [pcileech_com.sv:134-135](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_com.sv#L134-L135) 的 `dfifo.com_dout` 多路选择，优先输出 `initial_rx` 数据、否则输出真实 USB 收到的数据。

#### 4.4.4 代码实践：为 `initial_rx` 设计一条「上线前的 DRP 写」

1. **实践目标**：把一个「在核上线前写一个 DRP 寄存器」的动作，落成 `initial_rx` 数组里的一项 64 位命令字。
2. **操作步骤**：
   - 选定目标：假设要在核上线前把某 DRP 地址 `0x01F` 写成 `0xABCD`（地址/值仅作示例，实际语义待本地验证）。
   - 构造命令字。沿用 [u2-l5](u5-l4-drp-dynamic-reconfiguration.md) 与 4.2.4 推导的命令格式：低 32 位为 `[31:16]=地址字节 + [15:8]=含 MAGIC/type 的字节 + [7:0]=0x77`，高 32 位为掩码与值。对 `drp_di`（字节 `0x1A`）：地址字段 `0x801A`（bit15 置 1 表示 `f_rw`），值 `0xABCD`，掩码 `0xFFFF`；对 `drp_addr`（字节 `0x1C`）：地址字段 `0x801C`，值 `0x001F`，掩码 `0xFFFF`；对触发位（字节 `0x02` bit5）：地址字段 `0x8002`，值 `0x0020`，掩码 `0x0020`。
   - 把这三条命令填进 `initial_rx[0..2]`，保留 `initial_rx[3]=0`，`initial_rx[4]` 维持原上线命令不变。
3. **需要观察的现象**：上电后 `led_pcie`（链路）应在第 5 条命令之后才亮起；若你在前 4 条里塞了非法 DRP 写，可能导致核训练失败、`led_pcie` 不亮或 `lspci` 找不到设备。
4. **预期结果**：你能解释「为何这三条必须排在 `initial_rx[4]` 之前」——因为核一旦脱离复位（第 5 条），再做 DRP 写就违背了 [pcileech_pcie_a7.sv:246](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L246) 的使用约束。
5. **待本地验证**：示例命令字的字节拼装需对照 LeechCore 的 `device_fpga.c`（注释在 [pcileech_com.sv:69](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_com.sv#L69) 指明「please consult sources and also device_fpga.c in the LeechCore project」）交叉确认；本仓库不含该文件，请在 LeechCore 仓库核对。

> 说明：本节给出的 64 位命令字拼装为**示例代码**，目的是演示字段摆放，具体取值需在本地对照 `device_fpga.c` 验证后再写入。

#### 4.4.5 小练习与答案

**练习 1**：如果用户把一条 DRP 写误填进了 `initial_rx[4]`（覆盖了上线命令），会发生什么？
**答案**：核永远不会脱离复位（`rw[200]` 不被清），`led_pcie` 不亮，`lspci` 看不到设备；但 DRP 写本身可能反而「合法」，因为核还在复位态。这正好从反面印证「DRP 写应在复位态、核上线应在最后」。

**练习 2**：`initial_rx` 的播放为什么从 `tickcount64 >= 16` 才开始，而不是从 0？
**答案**：上电最初若干拍系统尚未稳定（[pcileech_com.sv:122](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_com.sv#L122) 的 `tickcount64_com<2` 还在复位跨域 FIFO），留出 16 拍让复位与时钟域稳定后再注入命令，避免命令被复位毛刺吃掉。

## 5. 综合实践

把本讲三块内容（寄存器映射、状态机、`initial_rx`）串起来，完成一个**端到端的 DRP 读写推演**（纯源码阅读型，不依赖硬件）：

**任务**：假设你是 PCILeech 主机驱动作者，要实现一个 `drp_write(addr, data)` 函数和配套的 `drp_read(addr)` 函数。请基于本讲源码，写出两者的操作步骤与轮询条件。

**提示步骤**：

1. **写流程**：
   - 发 Command 包写 `rw` 字节 `0x1A` ← `data`（掩码 `0xFFFF`）。
   - 发 Command 包写 `rw` 字节 `0x1C` ← `addr`（掩码 `0xFFFF`）。
   - 发 Command 包写 `rw` 字节 `0x02` ← `0x0020`（置 `DRP_WR_EN`，掩码 `0x0020`）。
   - 轮询读 `ro` 字节 `0x02`，直到 bit5（`ro[21]=rwi_drp_wr_en`）为 0（默认 `WAIT_COMPLETE=1` 期间，硬件会自动暂停接收新命令，所以这里也可以直接顺序发送，但读回确认更稳妥）。
2. **读流程**：
   - 发 Command 包写 `rw` 字节 `0x1C` ← `addr`。
   - 发 Command 包写 `rw` 字节 `0x02` ← `0x0010`（置 `DRP_RD_EN`，掩码 `0x0010`）。
   - 轮询读 `ro` 字节 `0x02`，直到 bit4（`ro[20]=rwi_drp_rd_en`）为 0。
   - 读 `ro` 字节 `0x20`（`ro[271:256]=rwi_drp_data`）得到读回值。
3. **自检**：对照 [pcileech_fifo.sv:319-322](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L319-L322) 与 [pcileech_fifo.sv:416-429](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L416-L429)，确认你的「置触发位→组合生效→状态机清零→黏滞位保持→`drp_rdy` 捕获」心智模型与源码一致。

**预期产物**：一份函数伪代码 + 一张「`rw`/`ro` 字节地址速查表」。完成后，你应当能解释为什么这套机制天然防抖（不会重复触发）、且对主机时序要求极低（只需轮询）。

## 6. 本讲小结

- **DRP 是 Xilinx PCIe 硬核的运行时后门**：一组 16 位宽、9 位寻址的寄存器读写端口，让你在比特流加载后仍能调硬核内部 PHY/PLL 级参数。
- **间接驱动链路**：主机 → USB → `pcileech_com` → `pcileech_fifo`（写 `rw`）→ `IfPCIeFifoCore` → `pcileech_pcie_a7` → `pcie_7x_0` DRP 引脚；回读经 `ro` 原路返回。
- **DRP 寄存器映射**：`rw[208+:16]`=drp_di、`rw[224+:9]`=drp_addr、`rw[20]/rw[21]`=读/写触发、`rw[18]`=WAIT_COMPLETE；`ro[271:256]`=drp_do、`ro[20]/ro[21]`=完成标志。
- **两级触发状态机**：`rw` 触发位生成 `drp_en` 脉冲并被立即清零，黏滞位 `rwi_drp_*` 顶上保持到 `drp_rdy`；主机靠轮询 `ro` 判断完成。
- **同域优势**：`pcie_drp_clk = clk_sys`，DRP 与 fifo 同一时钟域，**无需跨时钟 FIFO**。
- **`initial_rx` 预置**：核上线前的 DRP/配置动作由 `pcileech_com` 在上电第 16~20 拍注入，须排在「核上线」命令之前，以满足「核复位态做 DRP 写」的约束。

## 7. 下一步学习建议

- **横向对比 `cfg_mgmt`**：回到 [u3-l2 PCIe 配置空间管理](u5-l4-drp-dynamic-reconfiguration.md)，把 `cfg_mgmt` 的读写握手与 DRP 的两级触发做对照——两者都是「主机经 `rw` 触发、靠 done/rdy 完成」，差别在面向的寄存器空间（配置空间 vs 硬核内部）与时钟域（`cfg_mgmt` 需跨域、DRP 不需要）。
- **补全时钟域全景**：阅读 [u5-l1 跨时钟域设计](u5-l1-clock-domain-crossing.md)，把本讲「DRP 同域、无需 FIFO」放进工程三大时钟域的整体图景里理解。
- **深入设备变种**：[u6-l1 设备变种对比](u5-l4-drp-dynamic-reconfiguration.md) 会讲到 x4 工程（`pcileech_pcie_a7x4`），那里的 DRP 通道数与多 lane 收发器的 DRP 串联是更高级的话题。
- **查阅外部文档**：DRP 地址映射不在本仓库，建议在 Vivado 中打开 `pcie_7x_0` 的 IP 文档（`pg054`，7 Series Integrated Block for PCIe）查看各 DRP 地址的语义；主机命令字的字节级格式则对照 LeechCore 仓库的 `device_fpga.c`。
