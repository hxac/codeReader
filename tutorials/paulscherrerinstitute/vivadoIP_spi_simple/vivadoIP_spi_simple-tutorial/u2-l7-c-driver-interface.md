# C 驱动软件接口

## 1. 本讲目标

本讲把视线从 FPGA 内部的 RTL（`spi_simple`、`spi_vivado_wrp`）转到运行在 PS（如 Zynq ARM 核）上的**软件侧**。读完本讲，你应当能够：

- 看懂 `drivers/spi_simple/src/spi_simple.h` 与 `spi_simple.c` 这对头文件/源文件的整体结构。
- 说清 **阻塞（Blocking）** 与 **非阻塞（NonBlocking）** 两类收发 API 的语义差别，以及 TX-only 与 RX/TX 两种事务的差别。
- 解释驱动如何只靠读 `Status` 寄存器的若干 bit 就实现 FIFO 满空判断与忙等待（软件背压）。
- 独立使用 `SpiSimple_RxTxBlocking` 等函数完成一次“发 0x55、读回一个字”的事务，并说清楚每一步访问了哪个寄存器。
- 理解 `ClrIrqVec` / `SetIrqEna` / `SetTxAlmEmptyThreshold` / `SetRxAlmFullThreshold` 等配置函数与 u2-l6 讲过的中断/阈值硬件机制的对应关系。

## 2. 前置知识

本讲假设你已经读过：

- **u2-l1 寄存器地图**：知道 IP 有 10 个软件可见寄存器，地址 = 索引 × 4，`Data(0x00)` 是“写推 TX FIFO、读弹 RX FIFO”的双语义寄存器。
- **u2-l3 AXI 从接口**：知道 wrapper 把 AXI4 五通道翻译成扁平的寄存器读写，PS 侧 CPU 通过一段内存映射（Memory-Mapped I/O）就能访问这些寄存器。

下面用到的几个基础概念：

- **内存映射 I/O（MMIO）**：把设备的寄存器映射到一段 CPU 地址空间，CPU 用普通的“写内存 / 读内存”指令就能操作设备。Xilinx 的裸机库（libxil，BSP）提供了 `Xil_Out32(addr, val)` 写 32 位、`Xil_In32(addr)` 读 32 位两个底层函数来访问这些地址。
- **基地址（base address）**：一个 IP 实例在地址空间里的起点。所有寄存器的实际地址 = `基地址 + 偏移量`。偏移量就是 `SPI_SIMPLE_REG_*` 宏里那个 `0x00 / 0x04 / ...`。
- **背压（backpressure）**：生产者快、消费者慢时，消费者反过来告诉生产者“慢一点 / 别再发了”。在硬件里靠握手信号，在软件里靠轮询状态位。
- **FIFO 的“水位”**：FIFO 里当前有多少个数据。本 IP 用 `TxLevel` / `RxLevel` 两个寄存器暴露水位。

一句话定位：**C 驱动是一层很薄的胶水代码**——它把“往哪几个寄存器写什么、读什么”这套固定流程封装成带返回码的 C 函数，让应用代码不用每次都手敲 `Xil_Out32`。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲如何使用 |
|------|------|--------------|
| [drivers/spi_simple/src/spi_simple.h](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/drivers/spi_simple/src/spi_simple.h) | 公开头文件：返回码枚举、寄存器/状态/中断位宏、全部 API 原型 | 看驱动“对外承诺”了什么 |
| [drivers/spi_simple/src/spi_simple.c](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/drivers/spi_simple/src/spi_simple.c) | API 实现：内部 helper、阻塞/非阻塞收发、状态查询、配置 | 看驱动“实际怎么做” |
| [hdl/definitions_pkg.vhd](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/definitions_pkg.vhd) | RTL 侧的寄存器索引/位常量（单一数据源） | 与 C 宏逐位对照，确认软硬件契约一致 |
| [tb/top_tb.vhd](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd) | 测试平台里**手工**操作寄存器的 AXI 序列 | 作为驱动行为的“参考实现”来印证 |
| [drivers/spi_simple/data/spi_simple.tcl](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/drivers/spi_simple/data/spi_simple.tcl) | BSP 生成脚本：把基地址写进 `xparameters.h` | 解释实践代码里 `XPAR_*_BASEADDR` 从哪来 |

---

## 4. 核心概念与源码讲解

### 4.1 寄存器宏与返回码枚举

#### 4.1.1 概念说明

驱动要操作设备，首先要“知道设备有哪些寄存器、每个 bit 是什么意思”。`spi_simple.h` 用两套 `#define` 把 u2-l1 讲的寄存器地图原样镜像到 C 世界：

- **寄存器偏移量宏** `SPI_SIMPLE_REG_*`：每个宏的值就是该寄存器相对基地址的字节偏移。注意它们都是 4 字节步进（`0x00, 0x04, 0x08, …`），与 RTL 里“地址 = 索引 × 4”完全一致：\( \text{addr} = \text{idx} \times 4 \)。
- **位掩码宏** `SPI_SIMPLE_STATUS_*` 与 `SPI_SIMPLE_IRQ_*`：用 `(1 << n)` 给出某一位的掩码，方便与读回的寄存器值做按位与（`&`）来判断该位是否置 1。

另一件重要的事是**返回码**。驱动函数不是无脑执行，它会在前置条件不满足时提前返回一个负数错误码，让调用者决定如何处理（重试、报错等）。

#### 4.1.2 核心流程

寄存器地图在 RTL（`definitions_pkg.vhd`）和 C（`spi_simple.h`）两侧各声明一次。这是一份**必须手工保持同步**的契约：

```
definitions_pkg.vhd (RTL 单一数据源)
        │  RegIdx_*_c / BitIdx_*_c
        ▼
spi_simple.h (C 镜像，×4 得到字节地址)
        │  SPI_SIMPLE_REG_* / SPI_SIMPLE_STATUS_* / SPI_SIMPLE_IRQ_*
        ▼
spi_simple.c (用宏拼出真实地址 baseAddr + REG_*)
```

返回码是一个 `typedef enum`，约定 `0 = 成功`、负数 = 各类错误。

#### 4.1.3 源码精读

**返回码枚举**：成功为 0，四个负值分别覆盖“FIFO 满 / 空”这两类最常见的运行期冲突。

[spi_simple.h:23-30](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/drivers/spi_simple/src/spi_simple.h#L23-L30) —— 定义 `SpiSimple_ErrCode` 枚举，`SpiSimple_Success=0`，其余 `-1`～`-4`。

> 经验法则：判断成功要写 `retCode == SpiSimple_Success` 或 `!= SpiSimple_Success`，**不要**写 `if (retCode)`，因为成功是 0、会被当成假。

**寄存器偏移量宏**：把 u2-l1 的 10 个寄存器索引乘以 4，得到 MMIO 偏移地址。

[spi_simple.h:32-42](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/drivers/spi_simple/src/spi_simple.h#L32-L42) —— `SPI_SIMPLE_REG_DATA=0x00`、`…STATUS=0x04`、…、`…IRQ_ENA=0x24`。

与 RTL 对照（以 `SlaveNr` 为例）：RTL 里 `RegIdx_SlaveNr_c := 4`（索引 4），C 里 `SPI_SIMPLE_REG_SLAVE_NR = 0x10`（= 4×4）。两边描述的是同一个寄存器。

[definitions_pkg.vhd:46-53](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/definitions_pkg.vhd#L46-L53) —— RTL 寄存器索引常量，`RegIdx_SlaveNr_c=4`、`RegIdx_Data_c=0` 等。

**状态位与中断位掩码**：分别镜像 `Status`（7 位）与 `IrqVec`（5 位）。

[spi_simple.h:44-58](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/drivers/spi_simple/src/spi_simple.h#L44-L58) —— `SPI_SIMPLE_STATUS_TX_EMPTY (1<<0)` … `SPI_SIMPLE_STATUS_BUSY (1<<6)`；`SPI_SIMPLE_IRQ_TX_EMPTY (1<<0)` … `SPI_SIMPLE_IRQ_RX_ALM_FULL (1<<4)`。

与 RTL 位号一一对应：例如 C 的 `SPI_SIMPLE_STATUS_BUSY (1<<6)` 对应 RTL `BitIdx_Status_Busy_c := 6`（[definitions_pkg.vhd:42](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/definitions_pkg.vhd#L42)）；C 的 `SPI_SIMPLE_IRQ_TF_DONE (1<<2)` 对应 RTL `Irq_TfDone_c := 2`（[definitions_pkg.vhd:24-28](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/definitions_pkg.vhd#L24-L28)）。

#### 4.1.4 代码实践（源码阅读型）

**目标**：验证软硬件寄存器契约完全一致，亲手做一次“双向对照”。

**步骤**：

1. 打开 `hdl/definitions_pkg.vhd`，把 10 个 `RegIdx_*_c` 的索引值列成表。
2. 打开 `drivers/spi_simple/src/spi_simple.h`，把 10 个 `SPI_SIMPLE_REG_*` 的十六进制偏移列成表。
3. 对每一行验证 \( \text{偏移} = \text{索引} \times 4 \)。
4. 同样对 7 个 `BitIdx_Status_*_c` 与 `SPI_SIMPLE_STATUS_*`、5 个 `Irq_*_c` 与 `SPI_SIMPLE_IRQ_*` 做位号对照。

**预期结果**：三张表（寄存器、状态位、中断位）的 RTL 与 C 完全对齐，没有一位错位。如果将来有人改了 RTL 的索引却忘了改 C（或反之），这张对照表能立刻发现。

**观察现象**：你会注意到 RTL 用的是“索引”（0,1,2,…），C 用的是“字节地址”（0x00,0x04,0x08,…）。这是 AXI 以字节寻址、而 RTL 内部以寄存器号索引的自然结果（详见 u2-l3）。

#### 4.1.5 小练习与答案

**练习 1**：`SPI_SIMPLE_REG_RX_LEVEL` 的值是多少？它对应 RTL 哪个常量？
**答案**：`0x08`。对应 RTL 的 `RegIdx_RxLevel_c := 2`，因为 \( 2 \times 4 = 8 = \texttt{0x08} \)。

**练习 2**：为什么判断返回码要用 `== SpiSimple_Success` 而不是 `if (retCode)`？
**答案**：枚举里 `SpiSimple_Success = 0`。`if (retCode)` 会把 0 当假、非零当真，语义正好反了——成功会被当成“不执行”。

---

### 4.2 阻塞 / 非阻塞收发 API

#### 4.2.1 概念说明

驱动的核心是 5 个收发函数，它们在两个维度上正交：

| 维度 | 取值 | 含义 |
|------|------|------|
| **是否读回 MISO** | TX-only / RX+TX | TX-only：`StoreRx=false`，忽略 MISO；RX+TX：`StoreRx=true`，把 MISO 读回值存进响应 FIFO |
| **是否等待完成** | Blocking / NonBlocking | Blocking：函数返回时事务已经**物理完成**（SCK 已经打完）；NonBlocking：函数返回时事务只是**排进了 FIFO**，真正收发在硬件后台异步进行 |

“阻塞”这个名字容易误导：它**不是**指占用 CPU 去做时序，而是指函数会**轮询 `Status.Busy` 直到硬件做完**才返回。在此期间 CPU 确实在空转等待。

为什么需要 NonBlocking？因为 SPI 引擎比 CPU 慢得多。如果每次都要等事务做完才发下一条，CPU 大量时间耗在 `while(IsBusy){}` 上。NonBlocking 让你把多条命令**快速灌进 TX FIFO**，硬件自己排队执行，实现流水线——代价是你得自己管理 RX FIFO 别溢出（头文件注释明确警告了这一点）。

#### 4.2.2 核心流程

四个收发函数 + 一个取数函数的调用关系：

```
SpiSimple_TxBlocking       ──► WaitNotBusy(先) + TxNonBlocking + WaitNotBusy(后)
SpiSimple_RxTxBlocking     ──► WaitNotBusy(先) + RX空检查 + RxTxNonBlocking + WaitNotBusy(后) + GetRxData
SpiSimple_TxNonBlocking    ──► TX满检查 + SetSlaveNr + SetStoreRx(false) + 写Data
SpiSimple_RxTxNonBlocking  ──► TX满检查 + RX满检查 + SetSlaveNr + SetStoreRx(true) + 写Data
SpiSimple_GetRxData        ──► RX空检查 + 读Data
```

三条贯穿全程的关键事实：

1. **`SlaveNr` 与 `StoreRx` 是“粘性配置”**：它们是普通寄存器，写一次就一直保持，直到下次改写。每次发起事务前都要确保它们指向你想要的 slave / 读回模式。驱动每次都重新设置，保证幂等。
2. **写 `Data` 即“扣扳机”**：写 `Data(0x00)` 这一下，才是真正把一条命令（SlaveNr+StoreRx+Data 拼成的命令字，见 u2-l2）推进 TX FIFO、可能立刻启动 SPI 引擎的动作。前面的 `SetSlaveNr` / `SetStoreRx` 只是装填弹药。
3. **读 `Data` 即“取回 MISO”**：因为 `Data` 是双语义寄存器，读它就是弹出响应 FIFO 里最早那条 MISO 读回值（u2-l1 / u2-l3）。

#### 4.2.3 源码精读

**三个内部 helper**：注意它们只定义在 `.c` 里，**没有**在 `.h` 里声明原型，所以是驱动的私有实现细节，外部代码无法（也不应）直接调用。

[spi_simple.c:13-16](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/drivers/spi_simple/src/spi_simple.c#L13-L16) —— `SetSlaveNr` 写 `SLAVE_NR(0x10)`。

[spi_simple.c:18-25](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/drivers/spi_simple/src/spi_simple.c#L18-L25) —— `SetStoreRx` 把 `bool` 转成 0/1 写 `STORE_RX(0x14)`。

[spi_simple.c:27-30](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/drivers/spi_simple/src/spi_simple.c#L27-L30) —— `WaitNotBusy` 死循环轮询 `IsBusy`，是“阻塞”二字的物理来源。

**非阻塞 TX-only**：先检查 TX 满没有，没满才装填并扣扳机。

[spi_simple.c:92-107](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/drivers/spi_simple/src/spi_simple.c#L92-L107) —— `SpiSimple_TxNonBlocking`：`IsTxFifoFull` 为真就直接返回 `SpiSimple_TxFifoFull`，否则设 slave、设 `StoreRx=false`、`Xil_Out32(DATA, txData)` 启动事务。

**非阻塞 RX+TX**：多一个 RX 满检查（因为本次要往响应 FIFO 里再塞一个读回值，得先确认有地方放）。

[spi_simple.c:109-128](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/drivers/spi_simple/src/spi_simple.c#L109-L128) —— `SpiSimple_RxTxNonBlocking`：依次检查 TX 满、RX 满，任一为真即返回对应错误码；都通过则设 `StoreRx=true` 并写 `Data`。

> 头文件对它的警告值得读一遍（[spi_simple.h:95-106](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/drivers/spi_simple/src/spi_simple.h#L95-L106)）：NonBlocking 无法知道你还有多少笔 RX 没取走，**RX FIFO 可能溢出，防溢出是调用者的责任**。

**阻塞 TX-only**：在 NonBlocking 外面包了两层“等”——先等 TX 有空位，发起后再等事务完成。

[spi_simple.c:37-57](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/drivers/spi_simple/src/spi_simple.c#L37-L57) —— `SpiSimple_TxBlocking`：`while(IsTxFifoFull){}` 等出空间 → 调 `TxNonBlocking` → `WaitNotBusy` 等做完。

**阻塞 RX+TX**：最讲究的一个。它的目标是“发起一笔读事务并立刻拿到唯一的读回值”，因此必须保证取数时 RX FIFO 里**只有这一笔**，否则会读到前一笔遗留的数据。

[spi_simple.c:59-90](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/drivers/spi_simple/src/spi_simple.c#L59-L90) —— `SpiSimple_RxTxBlocking`：先 `WaitNotBusy` 确认空闲 → 若 `!IsRxFifoEmpty` 直接返回 `SpiSimple_RxFifoNotEmpty`（FIFO 里有陈旧数据，无法保证读到的是本次结果）→ 设配置 → `RxTxNonBlocking` → `WaitNotBusy` → `GetRxData` 取回。

**取 RX 数据**：先确认 FIFO 非空再读。

[spi_simple.c:130-142](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/drivers/spi_simple/src/spi_simple.c#L130-L142) —— `SpiSimple_GetRxData`：`IsRxFifoEmpty` 为真返回 `SpiSimple_RxFifoEmpty`，否则 `*rxData_p = Xil_In32(DATA)`。

**与 testbench 的印证**：测试平台里那段手工 AXI 序列，做的就是 `RxTxBlocking` 的工作——写 `SlaveNr`、写 `StoreRx`、写 `Data`、轮询 `Status.Busy`、读 `Data` 取回。两者的寄存器访问顺序完全一致。

[top_tb.vhd:193-205](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L193-L205) —— testbench 里 slave 0 的读回测试：`axi_single_write(StoreRx*4, 1)` → `axi_single_write(Data*4, 0x12)` → 轮询 `Status.Busy` → `axi_single_expect(Data*4, 0x34, …)` 期望读到 `0x34`。

#### 4.2.4 代码实践（写一段最小代码）

**目标**：用 C 驱动向 slave 0 发送 `0x55` 并读回一个字，显式处理 `TxFifoFull` / `RxFifoEmpty` 返回码，并标注每步访问的寄存器。下面是**示例代码**（不是仓库原有文件，可放到 BSP 工程的 `main.c` 里）：

```c
/* 示例代码：用驱动高层 API 完成 “发 0x55、读回一个字” */
#include "spi_simple.h"
#include "xparameters.h"   /* BSP 生成，提供 XPAR_SPI_SIMPLE_0_BASEADDR */

#define SPI_BASE XPAR_SPI_SIMPLE_0_BASEADDR

int spi_ping(uint32_t *rxOut)
{
    SpiSimple_ErrCode rc;

    /* RxTxBlocking 内部依次访问：
     *   读 STATUS(0x04) 轮询 Busy(0x40)        —— 等空闲
     *   读 STATUS(0x04) 检查 RxEmpty(0x08)     —— 非空则返回 RxFifoNotEmpty
     *   写 SLAVE_NR(0x10)=0
     *   写 STORE_RX(0x14)=1
     *   写 DATA(0x00)=0x55                     —— 扣扳机，命令入队
     *   读 STATUS(0x04) 轮询 Busy(0x40)        —— 等事务完成
     *   读 DATA(0x00)                          —— 弹响应 FIFO，得 MISO
     */
    rc = SpiSimple_RxTxBlocking(SPI_BASE, 0u, 0x55u, rxOut);

    if (rc == SpiSimple_TxFifoFull) {
        return -1;   /* 理论上阻塞 API 会自旋等出空间，走到这里属于异常 */
    }
    if (rc == SpiSimple_RxFifoEmpty) {
        return -2;   /* 事务已结束却读不到 RX：StoreRx 未生效或 FIFO 被清空 */
    }
    return (rc == SpiSimple_Success) ? 0 : -3;
}
```

如果想“看穿”高层 API、把每一步都写成可见的寄存器访问（与 testbench 那段一一对应），下面是**等价的手工实现（示例代码）**。注意：因为 `SetSlaveNr`/`SetStoreRx` 是私有 helper，用户侧只能直接用 `Xil_Out32`：

```c
/* 示例代码：手工逐寄存器实现，便于观察每一步 */
#include "spi_simple.h"
#include <xil_io.h>

SpiSimple_ErrCode spi_ping_manual(uint32_t base, uint32_t *rxOut)
{
    /* (a) TX 满检查：读 STATUS(0x04) & TX_FULL(0x02) */
    if (Xil_In32(base + SPI_SIMPLE_REG_STATUS) & SPI_SIMPLE_STATUS_TX_FULL)
        return SpiSimple_TxFifoFull;

    /* (b) 选 slave 0：写 SLAVE_NR(0x10)=0 */
    Xil_Out32(base + SPI_SIMPLE_REG_SLAVE_NR, 0);

    /* (c) 声明本次要存 RX：写 STORE_RX(0x14)=1 */
    Xil_Out32(base + SPI_SIMPLE_REG_STORE_RX, 1);

    /* (d) 扣扳机：写 DATA(0x00)=0x55，命令入 TX FIFO */
    Xil_Out32(base + SPI_SIMPLE_REG_DATA, 0x55);

    /* (e) 等事务完成：轮询 STATUS(0x04) 的 Busy(0x40) */
    while (Xil_In32(base + SPI_SIMPLE_REG_STATUS) & SPI_SIMPLE_STATUS_BUSY) { }

    /* (f) RX 空检查：读 STATUS(0x04) & RX_EMPTY(0x08) */
    if (Xil_In32(base + SPI_SIMPLE_REG_STATUS) & SPI_SIMPLE_STATUS_RX_EMPTY)
        return SpiSimple_RxFifoEmpty;

    /* (g) 取回 MISO：读 DATA(0x00)，弹响应 FIFO */
    *rxOut = Xil_In32(base + SPI_SIMPLE_REG_DATA);
    return SpiSimple_Success;
}
```

**操作步骤**：

1. 在 Vitis/Vivado SDK 里创建一个基于 BSP（含 `spi_simple` 驱动）的应用工程。
2. 把上面任一版本放进 `main.c`，在 `main()` 里定义 `uint32_t rx;` 并调用 `spi_ping(&rx);`。
3. 连接真实硬件（或运行带 AXI BFM 的仿真）。

**需要观察的现象**：

- 用调试器在 `(g)` 之后看 `rx` 的值，应当等于 slave 设备在收到 `0x55` 那一帧期间、在 MISO 上回送的数据（testbench 里 slave 0 对 `0x12` 回送的是 `0x34`）。
- 把断点打在 `(e)` 的 `while` 上，单步可见 `STATUS` 的 Busy 位先是 1、若干周期后变 0——这正是“阻塞等待”的可观察表现。
- 若在 `(a)` 之前故意把 TX FIFO 灌满（连发超过 `FifoDepth_g` 条），`spi_ping_manual` 会在 `(a)` 直接返回 `SpiSimple_TxFifoFull`。

**预期结果**：正常情况下函数返回 `SpiSimple_Success`（0），`*rxOut` 拿到有效读回值；任何一步前置条件不满足都返回对应的负错误码，不会“硬写”导致数据错乱。

> 待本地验证：在真实 Zynq 板上 `XPAR_SPI_SIMPLE_0_BASEADDR` 的具体值由 Block Design 的地址分配决定；在纯仿真环境里没有 BSP，需要用 AXI BFM（如 testbench 那样）驱动，本示例的 `Xil_Out32/In32` 路径不适用。

#### 4.2.5 小练习与答案

**练习 1**：`SpiSimple_RxTxBlocking` 为什么在发起事务**之前**就要检查 RX FIFO 是否为空？
**答案**：它要在事务完成后用 `GetRxData` 读回**本次**的 MISO。如果发起前 RX FIFO 已有陈旧数据，读回时无法区分哪个是本次结果（FIFO 是先进先出）。所以要求 FIFO 必须干净，否则直接返回 `SpiSimple_RxFifoNotEmpty`。

**练习 2**：`SpiSimple_TxNonBlocking` 和 `SpiSimple_TxBlocking` 各访问 `SLAVE_NR`、`STORE_RX`、`DATA`、`STATUS` 中的哪些？顺序如何？
**答案**：
- `TxNonBlocking`：先读 `STATUS`（查 TX 满），再写 `SLAVE_NR`、写 `STORE_RX=false`、写 `DATA`。
- `TxBlocking`：先读 `STATUS`（轮询 TX 满等出空间），然后调用 `TxNonBlocking`（重复上面四步），最后再读 `STATUS`（轮询 Busy 等完成）。即比 NonBlocking 多了“等 TX 有空位”和“等事务完成”两段轮询。

**练习 3**：`SpiSimple_TxBlocking` 能不能用来连发 10 条命令做流水线？为什么？
**答案**：不适合。它每条命令都会 `WaitNotBusy` 等硬件做完才返回，10 条命令串行执行，CPU 大量时间耗在空转上。流水线应该用 `TxNonBlocking` 连续把 10 条灌进 FIFO，让硬件排队执行。

---

### 4.3 状态查询与配置 API

#### 4.3.1 概念说明

除了发数据，驱动还提供两类辅助 API：

- **状态查询**：读 `Status(0x04)`、`TxLevel(0x0C)`、`RxLevel(0x08)`、`IrqVec(0x20)`，把硬件当前的水位、忙闲、中断锁存情况暴露给软件。
- **配置**：写 `IrqVec(0x20)` 做中断清除（W1C）、写 `IrqEna(0x24)` 使能中断上报、写两个阈值寄存器设置“快空 / 快满”报警点。

状态查询是 4.2 节里软件背压（轮询 Busy / TX 满 / RX 空）的底层支撑。配置 API 则是 u2-l6 讲的硬件中断/阈值机制的软件入口。

#### 4.3.2 核心流程

```
读路径：  Xil_In32(base + REG)  ──►  原始 32 位值
                                    ├─ GetStatusReg / GetIrqVec      原样返回
                                    ├─ GetRxFifoLevel / GetTxFifoLevel  返回水位
                                    └─ IsXxxFull/Empty/Busy           & 掩码后当 bool 返回

写路径：  Xil_Out32(base + REG, mask/threshold)
                                    ├─ ClrIrqVec        写 IRQ_VEC，W1C 清中断
                                    ├─ SetIrqEna        写 IRQ_ENA，使能上报
                                    ├─ SetTxAlmEmptyThreshold  写 TX_ALM_EMPTY_LVL
                                    └─ SetRxAlmFullThreshold   写 RX_ALM_FULL_LVL
```

#### 4.3.3 源码精读

**状态位查询**：所有 `IsXxx` 都读一次 `Status`，再与对应掩码按位与。结果非零即真。

[spi_simple.c:147-170](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/drivers/spi_simple/src/spi_simple.c#L147-L170) —— `SpiSimple_IsTxFifoFull/Emtpy`、`IsRxFifoEmpty/Full`、`IsBusy`，模式统一为 `return (GetStatusReg(baseAddr) & 对应掩码);`。

> 两个值得注意的细节：
> 1. C 里 `return (expr & mask);` 返回的是**掩码后的值**（不是 0/1）。因为返回类型是 `bool`，非零值都会被规约为 `true`，所以行为正确，但调试时别惊讶看到返回值是 `0x40` 而不是 `1`。
> 2. 函数名 `SpiSimple_IsTxFifoEmtpy` 里 **“Emtpy”是源码里的真实拼写**（少了个字母换位，应为 Empty）。查头文件原型（[spi_simple.h:134](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/drivers/spi_simple/src/spi_simple.h#L134)）时请按这个拼写来，别误以为是文档笔误。

**水位与原始寄存器读取**：直接 `Xil_In32` 对应偏移。

[spi_simple.c:172-190](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/drivers/spi_simple/src/spi_simple.c#L172-L190) —— `GetRxFifoLevel` 读 `RX_LEVEL(0x08)`、`GetTxFifoLevel` 读 `TX_LEVEL(0x0C)`、`GetIrqVec` 读 `IRQ_VEC(0x20)`、`GetStatusReg` 读 `STATUS(0x04)`。

**中断清除（W1C）**：写 `IRQ_VEC`，写 1 的位被清零，写 0 的位不动。这正是 u2-l6 讲的 write-1-to-clear 语义的软件入口。

[spi_simple.c:195-198](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/drivers/spi_simple/src/spi_simple.c#L195-L198) —— `SpiSimple_ClrIrqVec(base, mask)` 写 `IRQ_VEC(0x20)`。

头文件给了一个典型用法（[spi_simple.h:195-205](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/drivers/spi_simple/src/spi_simple.h#L195-L205)）：先 `vec = GetIrqVec()` 读出，再 `ClrIrqVec(vec)` 把“被 ISR 认过的那些位”一次性清掉——这是中断服务程序里的标准模式。

**中断使能**：写 `IRQ_ENA`，决定哪些锁存位能驱动 `irq` 引脚。复位默认全关（u2-l6）。

[spi_simple.c:200-203](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/drivers/spi_simple/src/spi_simple.c#L200-L203) —— `SpiSimple_SetIrqEna(base, mask)` 写 `IRQ_ENA(0x24)`。

**阈值配置**：写两个 almost 阈值寄存器，对应 u2-l5 讲的 `TxAlmEmpty` / `RxAlmFull` 报警点。

[spi_simple.c:205-213](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/drivers/spi_simple/src/spi_simple.c#L205-L213) —— `SetTxAlmEmptyThreshold` 写 `TX_ALM_EMPTY_LVL(0x18)`、`SetRxAlmFullThreshold` 写 `RX_ALM_FULL_LVL(0x1C)`。

与 testbench 印证：仿真里就是这么设阈值和中断的——

[top_tb.vhd:212-214](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L212-L214) —— `axi_single_write(IrqVec*4, 0xFF)` 清全部中断 → `axi_single_write(IrqEna*4, 2**Irq_TfDone_c)` 只使能 TfDone → `axi_single_write(TxAlmEmptyLevel*4, 3)` 设 TX 快空阈值为 3。这正是 `ClrIrqVec(0xFF)` + `SetIrqEna(SPI_SIMPLE_IRQ_TF_DONE)` + `SetTxAlmEmptyThreshold(3)` 的硬件等价写法。

#### 4.3.4 代码实践（写一个中断就绪 + 阈值配置片段）

**目标**：用配置 API 把驱动初始化成“事务完成即中断、TX 快空报警、RX 快满报警”的状态，并写一个 ISR 骨架演示 `GetIrqVec` + `ClrIrqVec` 的标准用法。下面是**示例代码**：

```c
/* 示例代码：驱动初始化 + ISR 骨架 */
#include "spi_simple.h"
#include "xparameters.h"

#define SPI_BASE XPAR_SPI_SIMPLE_0_BASEADDR

void spi_init(void)
{
    /* 清掉上电可能残留的中断锁存（写 1 清除，0xFF 清全部） */
    SpiSimple_ClrIrqVec(SPI_BASE, 0xFFFFFFFF);

    /* 只让 “事务完成(TfDone)” 和 “RX 快满” 两类事件产生中断 */
    SpiSimple_SetIrqEna(SPI_BASE,
                        SPI_SIMPLE_IRQ_TF_DONE | SPI_SIMPLE_IRQ_RX_ALM_FULL);

    /* TX 剩 <=2 条时报警；RX 装 >=6 条时报警（假设 FifoDepth_g=8） */
    SpiSimple_SetTxAlmEmptyThreshold(SPI_BASE, 2);
    SpiSimple_SetRxAlmFullThreshold(SPI_BASE, 6);
}

/* 示例代码：SPI 中断服务程序骨架 */
void SpiIrqHandler(void)
{
    uint32_t vec = SpiSimple_GetIrqVec(SPI_BASE);   /* 读 IRQ_VEC(0x20) */

    if (vec & SPI_SIMPLE_IRQ_TF_DONE) {
        /* 一笔事务完成，可以发下一笔或取数据 */
    }
    if (vec & SPI_SIMPLE_IRQ_RX_ALM_FULL) {
        /* RX 快满了，赶紧取数据腾地方 */
    }

    /* 认领过的位一次性清掉，避免重复进中断 */
    SpiSimple_ClrIrqVec(SPI_BASE, vec);             /* 写 IRQ_VEC(0x20), W1C */
}
```

**操作步骤**：

1. 在 `main()` 里先调 `spi_init()`。
2. 把 `SpiIrqHandler` 注册到 BSP 的中断控制器，绑定到 SPI IP 的 IRQ 号（具体注册 API 属于 Xilinx ScuGic 范畴，本讲不展开）。
3. 用 NonBlocking 连发若干条命令触发中断。

**需要观察的现象**：

- 每笔事务完成，`vec` 的 bit2（`TF_DONE`，0x04）会被锁存为 1。
- 如果不清（注释掉 `ClrIrqVec` 那行），同一个事件会反复进中断——这就是 W1C 没清干净的典型症状。
- 把 TX 阈值改成很大的值（例如等于 `FifoDepth_g`），你会发现 almost-empty 几乎一直置位，对应 u2-l6 讲的“电平型中断清不掉”现象。

**预期结果**：`spi_init()` 后 `GetIrqVec` 应为 0（残留已清）；每笔事务完成 ISR 触发一次；正确清中断后不会重入。

> 待本地验证：阈值与 `FifoDepth_g`（综合时设定）相关，上面写的 2/6 假设深度为 8，实际取值需与你的 IP 配置匹配。

#### 4.3.5 小练习与答案

**练习 1**：`SpiSimple_ClrIrqVec(base, 0x04)` 和 `SpiSimple_ClrIrqVec(base, 0xFF)` 分别清除哪些中断位？
**答案**：`0x04` 只清除 bit2（`TF_DONE`，事务完成），其余位不动；`0xFF` 清除低 5 位全部中断（`TX_EMPTY`/`TX_ALM_EMPTY`/`TF_DONE`/`RX_FULL`/`RX_ALM_FULL`）。因为 IRQ_VEC 是 5 位、W1C 语义——写 1 的位被清，写 0 的位保持。

**练习 2**：`SpiSimple_IsBusy` 返回值在调试器里显示成 `0x40` 而不是 `1`，这正常吗？为什么？
**答案**：正常。实现是 `return (GetStatusReg(base) & SPI_SIMPLE_STATUS_BUSY)`，而 `SPI_SIMPLE_STATUS_BUSY = (1<<6) = 0x40`。按位与的结果是 `0x40`（非零），返回类型 `bool` 会把它规约为 `true`。行为正确，只是显示值不是 0/1。

**练习 3**：为什么 ISR 里推荐 `ClrIrqVec(vec)` 而不是 `ClrIrqVec(0xFF)`？
**答案**：`ClrIrqVec(vec)` 只清 ISR 实际认领处理过的那些位，保留未被处理（或尚未检查）的位；`ClrIrqVec(0xFF)` 会把还没看的事件也清掉，可能丢失中断。读出 `vec` 再原样写回是 W1C 寄存器的标准“认领即清”模式。

---

## 5. 综合实践

把 4.1～4.3 串起来，完成一个小任务：**写一个“查询 + 阻塞 + 中断”三模式可切换的 SPI 访问封装**，并画出它和 RTL 的对应关系。

任务要求：

1. 写一个函数 `int spi_xfer(uint8_t slave, uint32_t tx, uint32_t *rx, bool poll_mode)`：
   - `poll_mode=true` 时，**不使用**高层 Blocking API，而是用 4.2.4 节的手工实现逐寄存器访问（`STATUS` 轮询 `BUSY`、写 `SLAVE_NR`/`STORE_RX`/`DATA`、读 `DATA`），并在每一步用 `printf` 打印访问的寄存器名与值。
   - `poll_mode=false` 时，直接调 `SpiSimple_RxTxBlocking`。
2. 在 `main` 里先用配置 API 初始化（清中断、使能 `TF_DONE`、设两个阈值），再分别用两种模式各做一次 slave 0 的 `0x55` 收发，打印两种模式下访问寄存器的**总次数**差异。
3. 回答：阻塞模式相比手工轮询，访问 `STATUS` 的次数是更多、更少还是相等？为什么？

**评判标准**：

- 代码能编译（纳入含 `spi_simple` 驱动的 BSP 工程）。
- 两种模式都能正确读回 MISO。
- 能准确说出：手工轮询里 `STATUS` 被读了“TX 满检查 + 若干次 Busy 轮询 + RX 空检查”多次；高层 `RxTxBlocking` 内部读 `STATUS` 的次数本质相同（它也是轮询实现），差异主要在**可读性和错误码语义**，而非访问次数。这个结论会打破“高层 API 一定更省总线”的直觉。

> 待本地验证：具体 `STATUS` 读取次数取决于 SPI 时钟分频与轮询间隔，需在真实硬件或带 AXI BFM 的仿真里计数确认。

---

## 6. 本讲小结

- C 驱动是一层薄胶水：用 `SPI_SIMPLE_REG_*` / `SPI_SIMPLE_STATUS_*` / `SPI_SIMPLE_IRQ_*` 三组宏把 RTL 的寄存器地图（`definitions_pkg.vhd`）镜像到 C 侧，二者必须手工同步。
- 返回码枚举 `SpiSimple_ErrCode` 约定 **0 = 成功、负数 = 错误**，判断要用 `== SpiSimple_Success` 而非 `if (retCode)`。
- 收发 API 在两个维度正交：**TX-only vs RX+TX**（由 `StoreRx` 决定是否把 MISO 存进响应 FIFO）、**Blocking vs NonBlocking**（是否轮询 `Status.Busy` 等事务物理完成）。
- `Data(0x00)` 是双语义：写它=扣扳机（命令入 TX FIFO），读它=取回 MISO（弹响应 FIFO）。
- 软件背压全靠读 `Status`：`TX_FULL` 门控写入、`RX_EMPTY` 门控读取、`BUSY` 用于等待完成。
- 配置 API 对应 u2-l5/u2-l6 的硬件机制：`ClrIrqVec`(W1C)、`SetIrqEna`(中断使能)、两个阈值 setter；ISR 标准模式是 `GetIrqVec` 读出后 `ClrIrqVec(vec)` 认领即清。

## 7. 下一步学习建议

- **回到仿真侧**：读 [tb/top_tb.vhd](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd) 的 `p_control` 与 `p_spi` 进程（u2-l8），把本讲的 C 函数和 testbench 里手工 AXI 序列做最后一次端到端对照——你会看到二者是同一份寄存器流程的“C 版”与“VHDL BFM 版”。
- **理解打包**：本讲提到的 `XPAR_SPI_SIMPLE_0_BASEADDR` 是 BSP 根据 `drivers/spi_simple/data/spi_simple.tcl` 生成的（[spi_simple.tcl:3-5](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/drivers/spi_simple/data/spi_simple.tcl#L3-L5)），而 `.mdd` 声明驱动支持哪个 IP（[spi_simple.mdd](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/drivers/spi_simple/data/spi_simple.mdd)）。这套机制属于 u3-l4（IP 打包与发布）。
- **动手扩展**：尝试给驱动加一个 `SpiSimple_RxTxBlockingMulti` 函数，连续发起 N 笔读事务并依次取回——你会真切体会到 NonBlocking 的 RX 溢出风险和 `RxLevel` 寄存器的价值。
