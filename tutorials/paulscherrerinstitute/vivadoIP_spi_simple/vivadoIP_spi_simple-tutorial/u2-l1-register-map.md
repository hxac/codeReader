# 寄存器地图与常量定义包

## 1. 本讲目标

学完本讲，你应当能够：

- 说出本 IP 一共有 **10 个软件可见寄存器**，并掌握「寄存器索引 → 4 字节步进字节地址」的换算关系（例如索引 9 对应 `0x24`）。
- 读懂 [hdl/definitions_pkg.vhd](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/definitions_pkg.vhd) 中定义的全部寄存器索引、状态位、中断位常量，理解它们为什么用「包（package）」集中声明。
- 把 VHDL 侧的常量与 C 驱动头文件 [drivers/spi_simple/src/spi_simple.h](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/drivers/spi_simple/src/spi_simple.h) 里的 `SPI_SIMPLE_REG_*`、`SPI_SIMPLE_STATUS_*`、`SPI_SIMPLE_IRQ_*` 宏**一一对应**起来，理解「硬件地址 ↔ 软件宏」必须同步这一契约。
- 理解 **Status 寄存器 7 个 bit**（TxEmpty/TxFull/TxAlmEmpty/RxEmpty/RxFull/RxAlmFull/Busy）与 **IRQ 向量 5 个 bit**（TxEmpty/TxAlmEmpty/TfDone/RxFull/RxAlmFull）各自的含义。

本讲是进阶层（u2）的第一讲，从「软件怎么看到这块硬件」切入，先把寄存器地图这张「公共契约表」彻底搞清楚，后续讲义（核心架构、AXI 接口、FIFO、中断）都会反复引用这里的常量。

## 2. 前置知识

阅读本讲前，请确认你已经理解入门层（u1）建立的几个概念：

- **AXI4 寄存器接口**：外部主机（如 Zynq ARM 核）通过一组总线读写 IP 内部寄存器来控制 SPI 收发。本讲关心的是「这些寄存器都有哪些、排在哪些地址上」。
- **wrapper 与顶层实体**：[hdl/spi_vivado_wrp.vhd](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd) 是顶层 wrapper，把 AXI 总线解码成一组「寄存器读/写」信号，再喂给 SPI 核心 `spi_simple`。
- **目录角色**：`hdl/` 是 RTL，`drivers/` 是 C 驱动（参见 u1-l2）。

此外本讲会用到几个 FPGA/嵌入式领域的通用概念：

- **寄存器（register）**：这里的「寄存器」不是 VHDL 里的 `signal` 触发器，而是指**软件可寻址的一个 32 位字（word）**。AXI 主机给出一个地址，就能读或写这个字。
- **字节地址 vs 字地址**：AXI 总线按**字节**编址，而每个寄存器占 4 个字节（32 位）。因此「第 N 个寄存器」的字节地址是 \(N \times 4\)。这是本讲最关键的一句口算。
- **VHDL package（包）**：一段可被多个设计单元 `use` 进来的常量/类型/函数声明集合。把寄存器常量放进包里，是为了让 RTL（`spi_simple`、`spi_vivado_wrp`）和未来可能的仿真、文档共享同一份「单一数据源」。
- **位掩码（bitmask）**：用一个整数的某一位表示一个布尔标志，如 `(1 << 6)` 表示第 6 位。C 驱动大量使用位掩码来读写 Status / IRQ。

> 小贴士：VHDL 的标识符是**大小写不敏感**的，`Irq_RxAlmFull_c`、`IRq_RxAlmFull_c`、`irq_rxalmfull_c` 是同一个名字。这一点在本讲的源码里会直接遇到，先记下。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `hdl/definitions_pkg.vhd` | **常量定义包**：声明全部寄存器索引、状态位、中断位常量与子类型，是 RTL 内部的「寄存器地图」 |
| `drivers/spi_simple/src/spi_simple.h` | **C 驱动头文件**：用 `#define` 把同样的地址与位掩码暴露给软件，是「寄存器地图」在 C 侧的镜像 |
| `hdl/spi_vivado_wrp.vhd` | **顶层 wrapper**：消费 `definitions_pkg` 的常量，把 AXI 地址解码成对单个寄存器的读写，决定了每个寄存器的「读/写方向」 |
| `drivers/spi_simple/src/spi_simple.c` | **C 驱动实现**：用 `Xil_Out32/Xil_In32` 配合 `SPI_SIMPLE_REG_*` 宏真正访问硬件，印证地址换算 |

本讲的「主线」就是在这四个文件之间来回对照：**VHDL 声明地址 → wrapper 决定方向 → C 宏镜像地址 → C 实现访问硬件**。

## 4. 核心概念与源码讲解

### 4.1 寄存器索引与地址映射

#### 4.1.1 概念说明

一个被 AXI 主机控制的 IP，本质上是一张「地址 → 功能」的表。主机往某个地址写一个字，就触发一个动作（例如把数据推进发送 FIFO）；从某个地址读一个字，就拿到一个状态（例如 FIFO 里还有几个数据）。

本 IP 把这张表设计成 **10 个 32 位寄存器**，编号 0 到 9。因为每个寄存器占 4 个字节，所以它们在 AXI 字节地址空间里等间距排列，步长为 4：

\[ \texttt{字节地址} = \texttt{RegIdx} \times 4 \]

这些索引编号不是散落在各处，而是集中声明在 `definitions_pkg` 这个 VHDL 包里。这样做的好处是：wrapper、SPI 核心、甚至未来的文档生成脚本都可以 `use work.definitions_pkg.all` 引用同一份常量，**改一个地方就全改了**，避免「RTL 写 8、C 驱动写成 9」这类典型灾难。

#### 4.1.2 核心流程

地址映射在系统中的传递路径：

1. **软件**（C 驱动）用 `SPI_SIMPLE_REG_*` 宏算出字节地址，例如 `baseAddr + 0x24`。
2. **AXI 总线**把这个字节地址送到 wrapper 的 `s00_axi_awaddr` / `s00_axi_araddr`（8 位地址，参见 wrapper 端口声明）。
3. **wrapper** 里的 `psi_common_axi_slave_ipif` 把地址解码成「第几个寄存器被读/写」，表现为 `reg_rd(i)` / `reg_wr(i)` 这些单比特脉冲，以及 `reg_wdata(i)` 这个 32 位写数据。
4. **SPI 核心 `spi_simple`** 根据这些信号执行真正的动作（推 FIFO、上报状态等）。

这里有一个容易踩坑的细节：地址解码阵列的大小不是恰好 10，而是**向上取整到 2 的幂**。wrapper 里这样计算寄存器阵列规模：

```vhdl
constant USER_SLV_NUM_REG : integer := 2**log2ceil(RegCount_c);
```

代入 `RegCount_c = 10`：

\[ \texttt{USER\_SLV\_NUM\_REG} = 2^{\lceil \log_2 10 \rceil} = 2^{4} = 16 \]

也就是说硬件实际开辟了 **16 个寄存器槽位**（`reg_rd` 是 16 位），但只有索引 0..9 有实际功能，10..15 是空槽（读回为 0、写被忽略）。这个「16 vs 10」的差别在排查地址范围时非常重要。

#### 4.1.3 源码精读

先看 VHDL 包里全部的寄存器索引常量（按地址顺序排列）：

[hdl/definitions_pkg.vhd:33-55](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/definitions_pkg.vhd#L33-L55) —— 声明了 `RegIdx_Data_c`(0) 到 `RegIdx_IrqEna_c`(9) 共 10 个索引，并在最后用 `RegCount_c := RegIdx_IrqEna_c+1` 汇总成 10，作为「寄存器总数」的唯一数据源。

注意 `Data` 寄存器（索引 0）的特殊性——它写在最前面、单独一行带 `-- tested` 注释，因为它是「双含义」寄存器：**写它 = 把数据推进 TX FIFO；读它 = 从 RX FIFO 弹出一个数据**。同一个地址，读和写是两条完全独立的数据通路。

再看 C 驱动头文件里镜像的地址宏：

[drivers/spi_simple/src/spi_simple.h:33-42](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/drivers/spi_simple/src/spi_simple.h#L33-L42) —— `SPI_SIMPLE_REG_DATA`(0x00) 到 `SPI_SIMPLE_REG_IRQ_ENA`(0x24)，逐字对应 VHDL 的 `RegIdx_*_c × 4`。

你可以逐行验证「索引 × 4 = 字节地址」这条口算：

| RegIdx (VHDL) | ×4 | 字节地址 (C 宏) |
| --- | --- | --- |
| `RegIdx_Data_c` = 0 | 0 | `SPI_SIMPLE_REG_DATA` = 0x00 |
| `RegIdx_Status_c` = 1 | 4 | `SPI_SIMPLE_REG_STATUS` = 0x04 |
| `RegIdx_RxLevel_c` = 2 | 8 | `SPI_SIMPLE_REG_RX_LEVEL` = 0x08 |
| `RegIdx_TxLevel_c` = 3 | 12 | `SPI_SIMPLE_REG_TX_LEVEL` = 0x0C |
| `RegIdx_SlaveNr_c` = 4 | 16 | `SPI_SIMPLE_REG_SLAVE_NR` = 0x10 |
| `RegIdx_StoreRx_c` = 5 | 20 | `SPI_SIMPLE_REG_STORE_RX` = 0x14 |
| `RegIdx_TxAlmEmptyLevel_c` = 6 | 24 | `SPI_SIMPLE_REG_TX_ALM_EMPTY_LVL` = 0x18 |
| `RegIdx_RxAlmFullLevel_c` = 7 | 28 | `SPI_SIMPLE_REG_RX_ALM_FULL_LVL` = 0x1C |
| `RegIdx_IrqVec_c` = 8 | 32 | `SPI_SIMPLE_REG_IRQ_VEC` = 0x20 |
| `RegIdx_IrqEna_c` = 9 | 36 | `SPI_SIMPLE_REG_IRQ_ENA` = 0x24 |

最后看 wrapper 是怎么把地址变成「第几个寄存器」的，以及它为每个寄存器开辟的读写阵列：

[hdl/spi_vivado_wrp.vhd:117](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L117) —— `USER_SLV_NUM_REG := 2**log2ceil(RegCount_c)`，把 10 向上取整到 16。

[hdl/spi_vivado_wrp.vhd:120-123](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L120-L123) —— 声明 4 组 IPIC（IP Interconnect）信号：`reg_rd`/`reg_wr` 是 16 位的「哪个寄存器被读/写」脉冲，`reg_rdata`/`reg_wdata` 是 16 × 32 位的「读回数据 / 写入数据」数组。

#### 4.1.4 代码实践

**实践目标**：亲手验证「VHDL 索引 × 4 = C 宏地址」这条契约，并理解 16 个槽位中只有 10 个有效。

**操作步骤**：

1. 打开 `hdl/definitions_pkg.vhd`，找到 `RegIdx_IrqEna_c`（索引 9）。
2. 打开 `drivers/spi_simple/src/spi_simple.h`，找到 `SPI_SIMPLE_REG_IRQ_ENA`。
3. 口算：\(9 \times 4 = 36 = \texttt{0x24}\)，与 `#define SPI_SIMPLE_REG_IRQ_ENA 0x24` 对照。
4. 再任选 `RegIdx_SlaveNr_c`(4) 与 `SPI_SIMPLE_REG_SLAVE_NR` 验证一次（\(4 \times 4 = 16 = \texttt{0x10}\)）。

**需要观察的现象**：每一行 VHDL 索引都能在 C 宏里找到一个完全匹配的 `×4` 地址，二者是同一张表的两个视角。

**预期结果**：10 个寄存器全部一一对应，地址范围 `0x00`–`0x24`（40 字节）。硬件槽位是 16 个（`0x00`–`0x3C`），但 `0x28`–`0x3C` 这 6 个槽位未使用。

> 待本地验证：如果你有 Vivado 硬件，可在 Vitis 里 `Xil_In32(base + 0x28)` 读未使用槽位，预期读回 0。

#### 4.1.5 小练习与答案

**练习 1**：为什么 wrapper 里开辟的寄存器槽位是 16 个，而不是恰好 10 个？

**参考答案**：因为 `USER_SLV_NUM_REG := 2**log2ceil(RegCount_c)`，地址解码阵列被向上取整到 2 的幂（`log2ceil(10)=4`，`2^4=16`）。这样 AXI 地址解码可以用整数高比特直接选通，综合出的电路更规整；代价是多了 6 个未使用的空槽。

**练习 2**：往地址 `0x00` 写一个字、和从地址 `0x00` 读一个字，分别会发生什么？

**参考答案**：`0x00` 是 `Data` 寄存器（`RegIdx_Data_c`）。**写**它会把写入数据推进 **TX FIFO**（触发一次 SPI 发送的命令）；**读**它则会从 **RX FIFO** 弹出一个已收到的数据。同一个地址承载了两条方向相反的数据通路，是本 IP 最容易混淆的一点。

---

### 4.2 状态位常量（Status Register）

#### 4.2.1 概念说明

`Status` 寄存器（索引 1，地址 `0x04`）是一个**只读**寄存器，它把 SPI 核心当前的各种「FIFO 水位 + 忙闲」状态压缩进一个 32 位字里的低 7 位。软件（尤其是 C 驱动）通过轮询这些位来实现背压：发送前查「TX FIFO 满了吗」，接收前查「RX FIFO 空了吗」。

这 7 个状态位同样在 `definitions_pkg` 里用「位索引常量」声明，并用一个 `StatusSize_c` 汇总位数、一个 `Status_t` 子类型固定宽度，确保 RTL 各处使用的状态向量宽度一致。

#### 4.2.2 核心流程

Status 位的生成与使用链路：

1. SPI 核心 `spi_simple` 内部根据两个 FIFO 的 `level`/`empty`/`full` 输出，组合出 7 位 `Status` 向量。
2. wrapper 把这个向量送到 `reg_rdata(RegIdx_Status_c)`，软件读 `0x04` 就能看到。
3. C 驱动用 `SPI_SIMPLE_STATUS_*` 位掩码与读回值做按位与，判断某一位是否置起。

宽度推导：

\[ \texttt{StatusSize\_c} = \texttt{BitIdx\_Status\_Busy\_c} + 1 = 6 + 1 = 7 \]

因此 `Status_t` 是 `std_logic_vector(6 downto 0)`，共 7 位。C 侧对应 `SPI_SIMPLE_STATUS_BUSY` = `(1 << 6)`，最大掩码也止于第 6 位。

#### 4.2.3 源码精读

VHDL 包里的状态位索引声明：

[hdl/definitions_pkg.vhd:35-44](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/definitions_pkg.vhd#L35-L44) —— `RegIdx_Status_c`=1 之后，依次声明 7 个 `BitIdx_Status_*_c` 位索引（0..6），再用 `StatusSize_c := BitIdx_Status_Busy_c+1` 汇总成 7，并用 `subtype Status_t is std_logic_vector(StatusSize_c-1 downto 0)` 固定为 7 位向量。

> 注意第 38 行的常量名拼作 `BitIDx_Status_TxAlmEmpty_c`（中间是大写 `D`），而其它都是 `BitIdx_`。由于 VHDL 大小写不敏感，它和 `BitIdx_Status_TxAlmEmpty_c` 是同一个名字，不影响编译——但读源码时别被这个大小写差异骗到。

C 驱动头文件里的状态位掩码镜像：

[drivers/spi_simple/src/spi_simple.h:45-51](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/drivers/spi_simple/src/spi_simple.h#L45-L51) —— `SPI_SIMPLE_STATUS_TX_EMPTY`(1<<0) 到 `SPI_SIMPLE_STATUS_BUSY`(1<<6)，与 VHDL 位索引完全对齐。

完整对照表：

| bit | 含义 | VHDL 常量 | C 宏 |
| --- | --- | --- | --- |
| 0 | TX FIFO 空 | `BitIdx_Status_TxEmpty_c` | `SPI_SIMPLE_STATUS_TX_EMPTY` |
| 1 | TX FIFO 满 | `BitIdx_Status_TxFull_c` | `SPI_SIMPLE_STATUS_TX_FULL` |
| 2 | TX FIFO 几乎空 | `BitIDx_Status_TxAlmEmpty_c` | `SPI_SIMPLE_STATUS_TX_ALM_EMPTY` |
| 3 | RX FIFO 空 | `BitIdx_Status_RxEmpty_c` | `SPI_SIMPLE_STATUS_RX_EMPTY` |
| 4 | RX FIFO 满 | `BitIdx_Status_RxFull_c` | `SPI_SIMPLE_STATUS_RX_FULL` |
| 5 | RX FIFO 几乎满 | `BitIdx_Status_RxAlmFull_c` | `SPI_SIMPLE_STATUS_RX_ALM_FULL` |
| 6 | SPI 正在传输（忙） | `BitIdx_Status_Busy_c` | `SPI_SIMPLE_STATUS_BUSY` |

再看 C 驱动如何真正「消费」这些位。以「忙检测」为例：

[drivers/spi_simple/src/spi_simple.c:167-170](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/drivers/spi_simple/src/spi_simple.c#L167-L170) —— `SpiSimple_IsBusy` 读 `Status` 寄存器，再与 `SPI_SIMPLE_STATUS_BUSY`(1<<6) 按位与，非 0 即忙。这就是阻塞型 API（如 `SpiSimple_TxBlocking`）等待传输完成的轮询手段。

而 wrapper 把 SPI 核心的 `Status` 输出接到读回阵列的对应槽位：

[hdl/spi_vivado_wrp.vhd:247](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L247) —— `Status => reg_rdata(RegIdx_Status_c)(StatusSize_c-1 downto 0)`，把核心的 7 位状态送进寄存器 1 的低 7 位，软件读 `0x04` 即得。

#### 4.2.4 代码实践

**实践目标**：跟踪一个状态位从 VHDL 常量到 C 判断函数的完整链路。

**操作步骤**：

1. 在 `definitions_pkg.vhd` 找到 `BitIdx_Status_TxFull_c` = 1。
2. 在 `spi_simple.h` 找到 `SPI_SIMPLE_STATUS_TX_FULL` = `(1 << 1)`。
3. 在 `spi_simple.c` 找到 `SpiSimple_IsTxFifoFull`（第 147–150 行），它对 `SpiSimple_GetStatusReg(...)` 的返回值与 `SPI_SIMPLE_STATUS_TX_FULL` 做按位与。

**需要观察的现象**：同一位（bit 1）在三处出现——VHDL 索引常量、C 宏、C 判断函数，三者必须保持一致。

**预期结果**：`SpiSimple_IsTxFifoFull` 返回非 0 当且仅当硬件 `Status` 的 bit 1 为 1。发送前调用它，就能避免往已满的 TX FIFO 里再塞数据（这正是 `SpiSimple_TxNonBlocking` 第 95–98 行先检查再写的逻辑）。

> 待本地验证：若在仿真里把 TX FIFO 写满，读 `0x04` 应观察到 bit 1 = 1。

#### 4.2.5 小练习与答案

**练习 1**：`StatusSize_c` 的值是多少？`Status_t` 是几位？

**参考答案**：`StatusSize_c := BitIdx_Status_Busy_c + 1 = 6 + 1 = 7`；`Status_t` 是 `std_logic_vector(6 downto 0)`，共 7 位。

**练习 2**：C 驱动判断「TX FIFO 还有空间可以写」时，应检查哪个状态位、如何判断？

**参考答案**：检查 `SPI_SIMPLE_STATUS_TX_FULL`（bit 1，对应 `BitIdx_Status_TxFull_c`）。当该位为 0 时表示 TX FIFO 未满、还有空间。驱动里 `SpiSimple_IsTxFifoFull` 返回该位的值，发送 API 在其返回真时阻塞或返回 `SpiSimple_TxFifoFull` 错误码。

---

### 4.3 中断位常量与宽度（IRQ Vector）

#### 4.3.1 概念说明

除了轮询 `Status`，本 IP 还能主动用**中断（IRQ）**通知软件「有事发生」。中断向量 `IrqVec`（索引 8，地址 `0x20`）是一个 **5 位**的锁存寄存器：每当某个事件发生（如传输完成、FIFO 几乎空），对应位就被**锁存置 1**，直到软件显式清除。

与 `Status` 的「实时反映当前水位」不同，`IrqVec` 是「**记忆性**」的——即使触发条件已经消失，已置起的位也会保持，等软件来读并清除。这保证软件不会错过瞬时事件。

配套还有 `IrqEna`（索引 9，地址 `0x24`）：一个 5 位使能掩码，决定哪些锁存的 IRQ 位能真正拉高顶层 `irq` 引脚产生中断。

#### 4.3.2 核心流程

IRQ 的工作链路：

1. SPI 核心检测到事件 → 把 `IrqVec` 对应位**锁存**为 1。
2. 软件读 `0x20`（`IrqVec`）得知是哪几个事件。
3. 软件把要清除的位写成 1 到 `0x20` → wrapper 把这次写转换成 `CfgIrqClr` + `CfgIrqClrVld`，核心按位清零（若条件仍成立会自动重新置位，详见 u2-l6）。
4. `IrqEna`（`0x24`）对锁存的向量做使能过滤，决定顶层 `irq` 引脚电平。

宽度推导（注意 `IrqSize_c` 这一行源码写法）：

\[ \texttt{IrqSize\_c} = 5, \quad \texttt{Irq\_t} = \texttt{std\_logic\_vector}(4 \texttt{ downto } 0) \]

5 位 IRQ 对应 5 个事件，最大位掩码 `(1 << 4)`。

#### 4.3.3 源码精读

VHDL 包里的 IRQ 位常量与宽度声明：

[hdl/definitions_pkg.vhd:24-30](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/definitions_pkg.vhd#L24-L30) —— 定义 5 个 IRQ 位常量 `Irq_TxEmpty_c`(0) 到 `Irq_RxAlmFull_c`(4)，再用 `IrqSize_c` 汇总、`subtype Irq_t` 固定为 5 位向量。

> 这段源码有两个值得注意的细节：
> - 第 27 行的 `Irq_RxFull_c` 是唯一**没有** `-- tested` 注释的 IRQ 位（其余 4 位都标注了已测试），说明该位在现有回归测试里覆盖较弱。
> - 第 29 行 `IrqSize_c : natural := IRq_RxAlmFull_c+1` 里的 `IRq_RxAlmFull_c` 看似和上面定义的 `Irq_RxAlmFull_c` 不同，但 VHDL 标识符大小写不敏感，二者是同一个常量（=4），所以 `IrqSize_c = 4 + 1 = 5`。别被大写 `R` 骗到。

C 驱动头文件里的 IRQ 位掩码镜像：

[drivers/spi_simple/src/spi_simple.h:54-58](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/drivers/spi_simple/src/spi_simple.h#L54-L58) —— `SPI_SIMPLE_IRQ_TX_EMPTY`(1<<0) 到 `SPI_SIMPLE_IRQ_RX_ALM_FULL`(1<<4)，与 VHDL 位常量逐一对齐。

完整对照表：

| bit | 含义 | VHDL 常量 | C 宏 | 测试覆盖 |
| --- | --- | --- | --- | --- |
| 0 | TX FIFO 空 | `Irq_TxEmpty_c` | `SPI_SIMPLE_IRQ_TX_EMPTY` | 已测试 |
| 1 | TX FIFO 几乎空 | `Irq_TxAlmEmpty_c` | `SPI_SIMPLE_IRQ_TX_ALM_EMPTY` | 已测试 |
| 2 | 一次传输完成 | `Irq_TfDone_c` | `SPI_SIMPLE_IRQ_TF_DONE` | 已测试 |
| 3 | RX FIFO 满 | `Irq_RxFull_c` | `SPI_SIMPLE_IRQ_RX_FULL` | 源码未标注 |
| 4 | RX FIFO 几乎满 | `Irq_RxAlmFull_c` | `SPI_SIMPLE_IRQ_RX_ALM_FULL` | 已测试 |

再看 wrapper 是如何把「写 IrqVec 寄存器」翻译成「按位清除」、把「读 IrqVec」翻译成「读锁存向量」的：

[hdl/spi_vivado_wrp.vhd:240-243](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L240-L243) —— `CfgIrqClr` 取写入数据的低 5 位（`IrqSize_c-1 downto 0`），`CfgIrqClrVld` 用对该寄存器的写脉冲 `reg_wr(RegIdx_IrqVec_c)` 触发；读回的 `CfgIrqVec` 同样取低 5 位。这就是 `IrqVec`「读=看锁存、写=按位清」双语义的硬件落点。

最后看 C 驱动的清除与使能函数：

[drivers/spi_simple/src/spi_simple.c:195-198](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/drivers/spi_simple/src/spi_simple.c#L195-L198) —— `SpiSimple_ClrIrqVec` 把掩码写到 `SPI_SIMPLE_REG_IRQ_VEC`(0x20)，正是上面 wrapper 翻译成 `CfgIrqClr` 的那一写。头文件注释里给出的典型用法是「先 `GetIrqVec` 读出、再把读到的值原样写回清除」。

#### 4.3.4 代码实践

**实践目标**：把 5 个 IRQ 位在 VHDL、C 宏、wrapper 三处串起来，理解 `IrqVec` 的「写清除」语义。

**操作步骤**：

1. 在 `definitions_pkg.vhd` 数清 IRQ 位常量个数（应为 5），并确认 `IrqSize_c` = 5。
2. 在 `spi_simple.h` 数清 `SPI_SIMPLE_IRQ_*` 宏个数（应为 5），最大掩码为 `(1 << 4)`。
3. 在 `spi_vivado_wrp.vhd` 第 240–241 行确认：**写** `0x20` 的低 5 位会被当成 `CfgIrqClr` 清除掩码，而不是「设置」向量。

**需要观察的现象**：`IrqVec` 与一般配置寄存器不同——对它的「写」不是改写内容，而是「按位清除」已锁存的位。

**预期结果**：软件读 `0x20` 得到当前锁存的 IRQ 向量（如 `0x17` = bit0,1,2,4 置起）；把 `0x02` 写回 `0x20` 会清除 bit1，读回变为 `0x15`。具体推演见 u2-l6（中断与状态机制）。

> 待本地验证：在 testbench（`tb/top_tb.vhd`）的 "Test IRQ clearing" 段可以观察到上述清除行为。

#### 4.3.5 小练习与答案

**练习 1**：`IrqSize_c` 等于多少？`Irq_t` 是几位向量？为什么是这么多位？

**参考答案**：`IrqSize_c = Irq_RxAlmFull_c + 1 = 4 + 1 = 5`；`Irq_t` 是 `std_logic_vector(4 downto 0)`，共 5 位。因为有 5 个独立的 IRQ 事件（bit0..bit4），位数由最高位常量 `Irq_RxAlmFull_c` 决定。

**练习 2**：哪个 IRQ 位在源码里没有标注 `-- tested`？这暗示什么？

**参考答案**：`Irq_RxFull_c`（bit 3，RX FIFO 满中断）没有 `-- tested` 注释，其余 4 位都标注了。这暗示该位在现有回归测试中覆盖较弱，是二次开发或增强测试时值得优先补测的点。

---

## 5. 综合实践

把本讲三个模块串起来，完成一张**完整的寄存器地图表**，这是后续所有进阶讲义的公共参考。

**任务**：依据 `hdl/definitions_pkg.vhd` 与 `drivers/spi_simple/src/spi_simple.h`，绘制本 IP 的 10 寄存器地图，要求包含：寄存器索引、字节地址、名称、VHDL 常量、C 宏、**读写方向**、关键 bit 含义。读写方向需要你回到 [hdl/spi_vivado_wrp.vhd:207-255](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L207-L255) 推断（哪些寄存器只接了 `reg_rdata`、哪些接了 `reg_wdata`、哪些两者都有）。

**参考答案表**（建议你先自己画，再对照）：

| RegIdx | 地址 | 名称 | VHDL 常量 | C 宏 | 方向 | 关键 bit |
| --- | --- | --- | --- | --- | --- | --- |
| 0 | 0x00 | Data | `RegIdx_Data_c` | `SPI_SIMPLE_REG_DATA` | **W**=推 TX FIFO / **R**=弹 RX FIFO | `TransWidth_g` 位收发数据 |
| 1 | 0x04 | Status | `RegIdx_Status_c` | `SPI_SIMPLE_REG_STATUS` | R | bit0..6 见 4.2 表 |
| 2 | 0x08 | RxLevel | `RegIdx_RxLevel_c` | `SPI_SIMPLE_REG_RX_LEVEL` | R | RX FIFO 当前占用量 |
| 3 | 0x0C | TxLevel | `RegIdx_TxLevel_c` | `SPI_SIMPLE_REG_TX_LEVEL` | R | TX FIFO 当前占用量 |
| 4 | 0x10 | SlaveNr | `RegIdx_SlaveNr_c` | `SPI_SIMPLE_REG_SLAVE_NR` | R/W | 选中从机号（读回 = 写入值） |
| 5 | 0x14 | StoreRx | `RegIdx_StoreRx_c` | `SPI_SIMPLE_REG_STORE_RX` | R/W | bit0 = 是否存储 RX |
| 6 | 0x18 | TxAlmEmptyLvl | `RegIdx_TxAlmEmptyLevel_c` | `SPI_SIMPLE_REG_TX_ALM_EMPTY_LVL` | R/W | TX 几乎空阈值 |
| 7 | 0x1C | RxAlmFullLvl | `RegIdx_RxAlmFullLevel_c` | `SPI_SIMPLE_REG_RX_ALM_FULL_LVL` | R/W | RX 几乎满阈值 |
| 8 | 0x20 | IrqVec | `RegIdx_IrqVec_c` | `SPI_SIMPLE_REG_IRQ_VEC` | R=读锁存 / W=按位清 | bit0..4 见 4.3 表 |
| 9 | 0x24 | IrqEna | `RegIdx_IrqEna_c` | `SPI_SIMPLE_REG_IRQ_ENA` | R/W | bit0..4 IRQ 使能掩码 |

**方向判定的依据**（都来自 wrapper 第 207–255 行）：

- 纯只读（只出现在 `reg_rdata` 侧、由核心驱动）：Status、RxLevel、TxLevel。
- 读回 = 写入值（wrapper 第 207–211 行的 `reg_rdata(...) <= reg_wdata(...)` 回环）：SlaveNr、StoreRx、TxAlmEmptyLevel、RxAlmFullLevel、IrqEna。
- 双语义特殊：Data（写推 TX、读弹 RX，第 250、253–254 行）、IrqVec（读锁存、写清除，第 240–242 行）。

把这张表存下来，后续阅读 u2-l2（核心架构）、u2-l3（AXI 接口）、u2-l6（中断机制）时随时回看，你会发现所有信号名都能在这张表里找到归属。

## 6. 本讲小结

- 本 IP 共 **10 个软件可见寄存器**（`RegCount_c = 10`），索引 0..9，字节地址 = 索引 × 4，范围 `0x00`–`0x24`；硬件地址解码阵列因取整为 2 的幂而是 **16 个槽位**。
- 所有寄存器索引、状态位、中断位都集中声明在 `hdl/definitions_pkg.vhd`，构成 RTL 内部的「单一数据源」。
- C 驱动头文件 `spi_simple.h` 用 `SPI_SIMPLE_REG_*` / `SPI_SIMPLE_STATUS_*` / `SPI_SIMPLE_IRQ_*` 宏**镜像**了同一张表，软硬件必须同步维护。
- `Status` 寄存器（`0x04`）是 **7 位只读**状态（`StatusSize_c = 7`），反映 FIFO 水位与忙闲，C 驱动靠位掩码轮询实现背压。
- `IrqVec`（`0x20`）是 **5 位锁存**中断向量（`IrqSize_c = 5`），「读 = 看锁存、写 = 按位清」是双语义；配套 `IrqEna`（`0x24`）做使能过滤。
- `Data`（`0x00`）是最特殊的双义寄存器：**写推 TX FIFO、读弹 RX FIFO**，初学者最易混淆。
- VHDL 标识符大小写不敏感，源码里 `IRq_RxAlmFull_c`、`BitIDx_Status_TxAlmEmpty_c` 等大小写差异不影响编译，但读码时要留意。

## 7. 下一步学习建议

本讲建立的是「静态地图」。接下来建议：

- **u2-l2（spi_simple 核心架构与数据流）**：进入 `hdl/spi_simple.vhd`，看 `Data` 寄存器写入后如何被打包成「命令 FIFO」的一项（`StoreRx` + `Slave` + `Data`），以及 `Status` 各位是如何由两个 FIFO 的水位组合出来的。
- **u2-l3（AXI4 从接口与寄存器映射）**：精读 `psi_common_axi_slave_ipif` 如何把 8 位 AXI 地址翻译成本讲的 `reg_rd/reg_wr/reg_wdata/reg_rdata`，把本讲的「方向判定」落实到 AXI 五通道信号。
- **u2-l6（中断向量与状态机制）**：深入 `IrqVec` 的锁存/清除/自动重置逻辑，本讲练习里提到的「写 `0x02` 清除后向量如何变化」将在那里完整推演。

阅读源码时，建议始终把本讲的寄存器地图表放在手边，每遇到一个 `RegIdx_*_c` 或 `SPI_SIMPLE_REG_*` 就在表里定位一次，很快这张表就会成为你的肌肉记忆。
