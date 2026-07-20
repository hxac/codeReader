# C 软件驱动

## 1. 本讲目标

本讲把视角从硬件 RTL 切换到嵌入式软件。学完后你应当能够：

- 看懂 `drivers/axi_mm_reader` 这套 C 驱动的整体结构与 API 列表。
- 理解驱动如何通过 `Xil_In32` / `Xil_Out32` 访问 IP 的寄存器映射，并能把驱动里的地址常量与 RTL 侧 `definitions_pkg.vhd` 一一对应。
- 掌握 `MmReader_SetEnable` / `MmReader_GetEnable` / `MmReader_SetRegTable` / `MmReader_GetLevel` / `MmReader_ReadFifoEntry` / `MmReader_ReadFifoPacket` 六个核心函数的行为。
- 说出三个错误码（`IpMustBeDisabled` / `FifoIsEmpty` / `NoCompletePacketInFifo`）的触发条件。
- 理解 AXIMM 输出模式下「先读 `RdLast`、再读 `RdData`」这一约定背后的硬件原因。
- 知道 `Makefile`、`.mdd`、`.tcl` 三个文件如何把驱动编进 Vitis 的 `libxil.a` 并生成 `xparameters.h`。

## 2. 前置知识

阅读本讲前，你需要先掌握下面两讲建立的硬件认知（本讲全程在复用它们）：

- **u2-l2 寄存器映射与配置表**：IP 经 `s00_axi` 把软件视图铺成一段连续地址空间，依次是 `Ena@0x00`、`RegCnt@0x04`、`RdData@0x08`、`RdLast@0x0C`、`Level@0x10`，以及从 `0x20` 起的 `Addr[]` 配置表（即 RegTable）。本讲的驱动地址常量就是这张表的镜像。
- **u2-l7 输出模式与 FIFO/RegTable 存储**：AXIMM 模式下，读回数据被映射到 `RdData`/`RdLast` 寄存器。其中读 `RdData` 是 **RV（带副作用读）**——会弹出 FIFO 一项；读 `RdLast` 只 peek（看而不弹）。这是本讲「先读 RdLast 再读 RdData」约定的硬件根源。

此外你需要一点 Xilinx 嵌入式软件的常识：

- **`Xil_In32(addr)` / `Xil_Out32(addr, val)`**：Xilinx 提供的 32 位内存映射 IO 读写宏，对一个地址读/写一个 32 位字。驱动不直接用指针解引用，而是统一走这两个函数，便于跨处理器（MicroBlaze/ARM）移植。
- **`xparameters.h`**：Vitis 在生成 BSP 时自动产生的头文件，记录硬件平台里每个 IP 实例的基地址（`C_BASEADDR`）等参数。驱动函数的第一个参数 `baseAddr` 通常就取自这里。
- **`libxil.a`**：Vitis BSP 的静态库，所有驱动 `.c` 编译后归档进去，应用程序链接它即可调用驱动 API。

> 一个贯穿全讲的关键事实：**驱动没有任何「软件触发」寄存器**。读周期只能由硬件 `Trig` 端口脉冲或内部超时自动启动（见 u2-l4）。因此驱动对一次读周期的参与方式是「配置好表 → 使能 → 等数据落进 FIFO → 读出来」，而不是「发一条命令让它现在读」。

## 3. 本讲源码地图

本讲涉及的关键文件都在 `drivers/axi_mm_reader/` 下，外加一个 RTL 包用于地址交叉验证：

| 文件 | 作用 |
| --- | --- |
| `drivers/axi_mm_reader/src/axi_mm_reader.h` | 驱动公共头：错误码枚举、寄存器地址与位掩码常量、6 个 API 函数原型。 |
| `drivers/axi_mm_reader/src/axi_mm_reader.c` | 驱动实现：用 `Xil_In32`/`Xil_Out32` 实现全部 API。 |
| `drivers/axi_mm_reader/src/Makefile` | 把 `.c` 编译成 `.o` 并归档进 `libxil.a`，把 `.h` 拷进 include 目录。 |
| `drivers/axi_mm_reader/data/axi_mm_reader.mdd` | 驱动元数据声明（MDD）：把驱动绑定到名为 `axi_mm_reader` 的外设。 |
| `drivers/axi_mm_reader/data/axi_mm_reader.tcl` | BSP 生成脚本：向 `xparameters.h` 写入该 IP 的实例信息。 |
| `hdl/definitions_pkg.vhd` | RTL 侧寄存器索引常量，用于与驱动地址常量交叉验证。 |
| `scripts/package.tcl` | 打包时通过 `add_drivers_relative` 把 `.c`/`.h` 纳入 IP（见 u1-l4）。 |

驱动目录非常小：`src/` 放源码与 Makefile，`data/` 放 MDD/TCL 声明，没有任何额外子模块。

## 4. 核心概念与源码讲解

### 4.1 寄存器访问 API：地址常量、Xil_IO 与基本读写

#### 4.1.1 概念说明

驱动的本质是一层很薄的封装：把「软件想做的事」翻译成「对若干个 32 位寄存器的读/写」。它不关心 AXI4 握手、不关心 FSM 状态机，那些都由 `psi_common_axi_slave_ipif`（见 u2-l5）在硬件侧处理完毕。驱动只看到一段平坦的、内存映射的寄存器空间。

因此驱动的第一步，是把 u2-l2 的寄存器地图翻译成 C 的 `#define` 地址常量。头文件里集中定义了这些常量，它们必须与 RTL 侧 `definitions_pkg.vhd` 的字索引严格一致——只是 RTL 用「字索引」（0,1,2,…），驱动用「字节地址」（索引×4）。

#### 4.1.2 核心流程

一次普通的「写寄存器」调用流程：

```text
应用程序调用 MmReader_SetEnable(base, true)
   └─ 驱动计算字节地址 = base + MM_READER_ENA_REG (0x00)
   └─ Xil_Out32(地址, 使能位)  ──► AXI 写事务 ──► s00_axi 从机 ──► reg_wdata ──► Enable 端口
```

一次「读寄存器」调用流程：

```text
应用程序调用 MmReader_GetLevel(base, &lvl)
   └─ 驱动计算字节地址 = base + MM_READER_LEVEL_REG (0x10)
   └─ Xil_In32(地址)  ◄── AXI 读事务 ◄── s00_axi 从机 ◄── reg_rdata(Level) ◄── AxiS_Level
```

地址换算的统一公式：

\[
\text{字节地址} = \text{baseAddr} + \text{寄存器字索引} \times 4
\]

#### 4.1.3 源码精读

寄存器地址常量定义在头文件里，注释里同时标注了对应的 RTL 字索引：

[drivers/axi_mm_reader/src/axi_mm_reader.h:L31-L37](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/drivers/axi_mm_reader/src/axi_mm_reader.h#L31-L37) —— 定义 `Ena/RegCnt/RdData/RdLast/Level` 五个固定寄存器的字节地址，以及 RegTable 内存区的起始偏移 `0x20`。

对照 RTL 侧的索引常量：

[hdl/definitions_pkg.vhd:L25-L35](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/definitions_pkg.vhd#L25-L35) —— `RegIdx_Ctrl_c=0`（即 Ena）、`RegIdx_RegCnt_c=1`、`RegIdx_RdData_c=2`、`RegIdx_RdLast_c=3`、`RegIdx_Level_c=4`、`MemOffs_c=8`。把每个字索引乘 4，就得到驱动里 `0x00/0x04/0x08/0x0C/0x10/0x20`，两侧完全吻合。

使能位掩码：

[drivers/axi_mm_reader/src/axi_mm_reader.h:L40-L41](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/drivers/axi_mm_reader/src/axi_mm_reader.h#L40-L41) —— `MM_READER_ENA_REG_ENA = (1<<0)`，使能位在 bit0，与 RTL 的 `BitIdx_Ctrl_Ena_c=0` 对应。（第二行 `MM_READER_RD_REG_LAST` 是给 `RdLast` 的「计划用」位掩码，但实现里并未真正使用，见 4.2.3 的说明。）

最简单的写/读实现——使能控制与状态查询：

[drivers/axi_mm_reader/src/axi_mm_reader.c:L19-L35](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/drivers/axi_mm_reader/src/axi_mm_reader.c#L19-L35) —— `MmReader_SetEnable` 用 `Xil_Out32` 写 `Ena` 寄存器，使能时写 `1<<0`、禁用时写 `0`；`MmReader_GetEnable` 用 `Xil_In32` 读回 `Ena`，直接转 `bool`。

[drivers/axi_mm_reader/src/axi_mm_reader.c:L62-L67](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/drivers/axi_mm_reader/src/axi_mm_reader.c#L62-L67) —— `MmReader_GetLevel` 读 `Level` 寄存器，返回 FIFO 当前水位（单位是 32 位字）。

可以看到：每个 API 的核心就是「算地址 + 一次 `Xil_In32`/`Xil_Out32`」，封装极薄。

#### 4.1.4 代码实践（源码阅读型）

**目标**：亲手把驱动地址常量与 RTL 索引对齐一遍。

1. 打开 `axi_mm_reader.h` 的地址常量段与 `definitions_pkg.vhd` 的常量段。
2. 画一张三列表格：`寄存器名 | RTL 字索引 | 驱动字节地址`。
3. 逐行验证「驱动字节地址 == 字索引 × 4」。

**预期结果**：五行固定寄存器全部满足等式，`MemOffs_c=8 → 8×4=32=0x20` 也成立。若任何一行对不上，说明驱动与 RTL 版本不一致（本仓库当前 HEAD 下应当全部一致）。

#### 4.1.5 小练习与答案

**练习 1**：为什么驱动用 `Xil_Out32/Xil_In32` 而不是直接 `*((volatile uint32_t*)addr) = val`？

**参考答案**：`Xil_*` 是 Xilinx 官方的 IO 访问层，内部已处理字节序、内存屏障、缓存属性与不同处理器（MicroBlaze/ARM Cortex-A9/A53/R5）的差异。直接解引用在简单情况下也能工作，但移植到另一颗 CPU 或带缓存的平台时可能出错，所以驱动统一走 `Xil_*`。

**练习 2**：`MmReader_GetEnable` 把整个 `Ena` 寄存器转成 `bool`，而不是 `& MM_READER_ENA_REG_ENA`。这样安全吗？

**参考答案**：安全。因为 `Ena` 寄存器当前只有 bit0 一个有效位（见 RTL `BitIdx_Ctrl_Ena_c=0`），其余位读回为 0；使能时寄存器值为 1，禁用时为 0，C 的 `bool` 转换「非零即真」正好匹配。代价是：如果将来 RTL 给 `Ena` 寄存器增加了别的位，这种写法会读到「非零但未必使能」的值，到时需要改成掩码判断。

---

### 4.2 寄存器表配置 SetRegTable 与使能门控

#### 4.2.1 概念说明

`MmReader_SetRegTable` 是驱动里逻辑最重的函数。它的作用是把「这一轮要读哪些寄存器地址」整张表写进 IP 的 RegTable 内存区，并告诉 IP 一共读几个。

这个函数有一条硬性前提：**调用前 IP 必须处于禁用状态**。原因来自硬件侧（u2-l3/u2-l5）：

- 核心 FSM 只在 `Idle_s` 状态采样 `RegCount`，并在读周期中遍历 RegTable。
- 若在读周期进行中改写 RegTable 或 RegCount，正在进行的遍历会读到「半新半旧」的地址表，行为不可预测。

因此驱动在函数入口主动检查使能状态，命中即拒绝并返回 `MmReader_IpMustBeDisabled`，把硬件的隐性约束变成软件的显式契约。

#### 4.2.2 核心流程

```text
MmReader_SetRegTable(base, regs[], N):
  1. GetEnable(base) ─► 若已使能 ─► 返回 IpMustBeDisabled
  2. for idx in 0..N-1:
        Xil_Out32(base + 0x20 + 4*idx, regs[idx])   // 写 Addr[idx]
  3. Xil_Out32(base + 0x04, N)                        // 写 RegCount
```

注意第 2 步的地址计算 `0x20 + 4*idx`：`0x20` 是 RegTable 内存区起点（`MM_READER_REGMAP_OFFS`），`4*idx` 是第 idx 项的字节偏移（每项 32 位 = 4 字节）。这与 u2-l5 中 `mem_addr(… downto 2)`（字节地址右移 2 位除以 4 得字索引）是同一段映射的软件侧写法。

#### 4.2.3 源码精读

[drivers/axi_mm_reader/src/axi_mm_reader.c:L37-L60](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/drivers/axi_mm_reader/src/axi_mm_reader.c#L37-L60) —— `MmReader_SetRegTable` 全过程：先复用 `MmReader_GetEnable` 查使能状态，命中即返回错误码；随后循环写 `Addr[]` 表，最后写 `RegCount`。

[drivers/axi_mm_reader/src/axi_mm_reader.c:L44-L50](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/drivers/axi_mm_reader/src/axi_mm_reader.c#L44-L50) —— 使能检查的实体代码。注意它复用了 `MmReader_GetEnable` 而不是再写一次读操作，体现「单一数据源」的工程习惯。

> 顺带说明一个容易踩坑的细节（与 `SetRegTable` 无关，但同属「位掩码」话题）：头文件 [axi_mm_reader.h:L41](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/drivers/axi_mm_reader/src/axi_mm_reader.h#L41) 把 `MM_READER_RD_REG_LAST` 声明为 `(1<<1)`（bit1），但实现 `MmReader_ReadFifoEntry` 并没有使用它，而是把整个 `RdLast` 寄存器直接转 `bool`（见 4.3.3）。而 RTL 侧 `BitIdx_RdLast_c=0`（bit0）。也就是说，这个常量的声明位与 RTL 实际位并不一致；由于实现从不做掩码，依赖「非零即真」，所以**当前没有功能影响**，但它是一处「声明了却没用、且数值对不上 RTL」的遗留定义，扩展驱动时若要按位判断 `Last`，应直接读 RTL 的 bit0。

#### 4.2.4 代码实践（源码阅读型）

**目标**：确认「软件写 RegCount」最终到达核心 FSM 的 `RegCount` 端口。

1. 从 `axi_mm_reader.c` 的 `MmReader_SetRegTable` 末尾那行 `Xil_Out32(base + MM_READER_REG_CNT_REG, numRegs)` 出发。
2. 跳到 wrapper `axi_mm_reader_wrp.vhd`，找到 `reg_wdata(RegIdx_RegCnt_c)(…) => RegCount` 的端口映射。
3. 再跳到核心 `axi_mm_reader.vhd`，确认 `RegCount` 在 `Idle_s` 被采样进内部寄存器。

**预期结果**：能画出一条从「C 函数」到「FSM 状态寄存器」的完整路径，路径上每一段都对应一个你已经学过的环节（u2-l5 → u2-l3）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `SetRegTable` 先写 `Addr[]` 表、最后才写 `RegCount`，而不是反过来？

**参考答案**：虽然此函数要求 IP 禁用（禁用时 FSM 不会立刻开始遍历），顺序看似无关，但「先填表、后告知长度」是更稳健的惯例：即使将来有人违反约定在使能态调用，或硬件在某版本下对 `RegCount` 边沿敏感，先写好完整表再设置长度，也能保证 FSM 看到的长度与表内容自洽。这是一种防御性写法。

**练习 2**：函数注释里说「驱动不检查 `numRegs` 是否超过 IP 支持的最大寄存器数」。这样设计的代价是什么？

**参考答案**：代价是把「越界」的责任甩给调用者。若软件传入的 `numRegs` 大于综合时的 `MaxRegCount_g`，多余的写会落到 RegTable 内存区之外（可能命中别的寄存器或无效地址），而核心 FSM 也只会读到表的前 `MaxRegCount_g` 项。好处是驱动不依赖具体实例的参数、代码通用。正确做法是应用程序自己根据 `xparameters.h` 或生成参数限制 `numRegs`。

---

### 4.3 FIFO 包读取：单条与整包，及「先读 RdLast 再读 RdData」

#### 4.3.1 概念说明

当 IP 工作在 **AXIMM** 输出模式（默认）时，读回的数据不在 `m_axis` 端口上，而是落在内部 FIFO 里，软件通过 `RdData`/`RdLast` 两个寄存器取数。这两个函数（`ReadFifoEntry` / `ReadFifoPacket`）**仅 AXIMM 模式可用**；AXI-Stream 模式下数据走 `m_axis`，软件根本不接触 FIFO，头文件注释明确禁止在 AXIS 模式下调用它们。

这一模块的全部难点浓缩成一句话：**读 `RdData` 会弹出 FIFO 一项（RV，带副作用读），读 `RdLast` 只 peek 不弹**（见 u2-l7）。因此取一个值时，必须先读 `RdLast` 看它是不是包尾，再读 `RdData` 把值（连同这一项）弹出。顺序反了，`RdLast` 反映的就会是「下一项」而不是「当前项」。

#### 4.3.2 核心流程

单条读取 `MmReader_ReadFifoEntry`：

```text
1. GetLevel(base) ─► 若 level==0 ─► 返回 FifoIsEmpty
2. last = Xil_In32(base + 0x0C)   // 读 RdLast（peek，不弹）
3. data = Xil_In32(base + 0x08)   // 读 RdData（弹出一项）
```

整包读取 `MmReader_ReadFifoPacket`：循环调用 `ReadFifoEntry`，把值塞进调用者缓冲区；每读一项检查 `last`，命中包尾就提前结束并记录实际包长 `pktSize`。若直到缓冲区写满都没遇到 `last`，返回 `NoCompletePacketInFifo`。

#### 4.3.3 源码精读

[drivers/axi_mm_reader/src/axi_mm_reader.c:L69-L90](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/drivers/axi_mm_reader/src/axi_mm_reader.c#L69-L90) —— `MmReader_ReadFifoEntry`。注意第 85–87 行的顺序：先读 `RdLast`，再读 `RdData`，注释 `Read last first (because reading data removes the FIFO entry)` 一语道破原因。

[drivers/axi_mm_reader/src/axi_mm_reader.c:L85-L87](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/drivers/axi_mm_reader/src/axi_mm_reader.c#L85-L87) —— 关键两行：先 peek `RdLast`，再 pop `RdData`。

硬件侧的对应关系（为什么读 `RdData` 会弹、读 `RdLast` 不会）：

[drivers/axi_mm_reader/src/axi_mm_reader.c:L92-L115](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/drivers/axi_mm_reader/src/axi_mm_reader.c#L92-L115) —— `MmReader_ReadFifoPacket`：循环 + 提前退出 + 包完整性检查。

[drivers/axi_mm_reader/src/axi_mm_reader.c:L98-L112](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/drivers/axi_mm_reader/src/axi_mm_reader.c#L98-L112) —— 循环体：每拍读一项，遇 `last` 即停；循环结束后若 `!last` 说明缓冲区装满仍未遇包尾，返回 `NoCompletePacketInFifo`。

回顾 u2-l7 的 wrapper 代码可印证「读 RdData 弹 FIFO」的硬件实现：

`AxiS_Rdy <= reg_rd(RegIdx_RdData_c);`（[hdl/axi_mm_reader_wrp.vhd:L180](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L180)）——软件对 `RdData` 寄存器的一次读（`reg_rd(RegIdx_RdData_c)` 拉高）直接驱动 FIFO 的 `AxiS_Rdy`，于是 FIFO 弹出一拍；而 `RdLast` 寄存器不参与这个赋值，所以读它不会弹。

#### 4.3.4 代码实践（源码阅读型 + 推理）

**目标**：亲手推演「顺序读反」会发生什么。

1. 假设 FIFO 当前为 `[A(last=0), B(last=1)]`（一个长度 2 的包，B 是包尾）。
2. 按**正确顺序**（先 RdLast 后 RdData）走两遍 `ReadFifoEntry`，记录每次返回的 `(data, last)`。
3. 再按**错误顺序**（先 RdData 后 RdLast）走一遍，看 `last` 错位到了哪里。

**预期结果**：

| 步骤 | 正确顺序 (last,data) | 错误顺序 (data,last) |
| --- | --- | --- |
| 第 1 项 | last=0, data=A | data=A, last=**1**（读到的是 B 的 last！） |
| 第 2 项 | last=1, data=B | data=B, last=0（FIFO 已空，读 RdLast 得 0） |

错误顺序会让软件误以为 A 就是包尾、提前截断，这正是驱动强制「先 RdLast 后 RdData」的原因。

> 本地验证（可选）：在有硬件的环境下，可对 `top_tb` 增加一个用例，或在 Vitis 里手写一段先读 `RdData` 再读 `RdLast` 的代码，观察取到的包是否被错位截断。无硬件时上述推理即为结论（**待本地验证**）。

#### 4.3.5 小练习与答案

**练习 1**：`ReadFifoPacket` 的 `size` 参数是「缓冲区容量」还是「期望包长」？为什么循环上限用 `size`？

**参考答案**：是缓冲区容量（缓冲区能装多少个 32 位字）。循环上限用 `size` 是为了**永不越界写缓冲区**：最坏情况下 FIFO 里有一个比缓冲区还大的包，函数也会在写满前停止（随后返回 `NoCompletePacketInFifo`），而不是溢出。真正的包尾由 `last` 标志决定，与 `size` 无关。

**练习 2**：如果调用者只想要包的前几项、不需要整包，能不能只调几次 `ReadFifoEntry` 就停？

**参考答案**：技术上可以，但**不推荐**。因为只要读到 `RdData`，FIFO 里对应项就被永久弹出了。如果中途停下，剩余项会留在 FIFO 里，下一次 `ReadFifoPacket` 会从一个「半包」开始，`last` 标志与包边界错位，极难正确处理。规范用法是：要么整包读（`ReadFifoPacket`），要么确认自己能跟踪包边界后再逐项读。

---

### 4.4 错误码体系

#### 4.4.1 概念说明

驱动用一个枚举 `MmReader_ErrCode` 统一所有函数的返回值，约定「0 表示成功，负数表示错误」。这是裸机/嵌入式 C 里最常见的轻量错误处理风格——没有异常、没有 `errno`，调用者每次都必须检查返回值。

这套错误码不是为了「描述硬件故障」，而是为了「描述调用契约被违反」：在错误的时机调用了函数，或在不具备条件时读数据。

#### 4.4.2 核心流程

错误码与产生它的函数、触发条件一一对应：

| 错误码 | 值 | 由谁返回 | 触发条件 |
| --- | --- | --- | --- |
| `MmReader_Success` | 0 | 所有函数 | 一切正常 |
| `MmReader_IpMustBeDisabled` | -1 | `SetRegTable` | IP 当前已使能，不允许改表 |
| `MmReader_FifoIsEmpty` | -2 | `ReadFifoEntry`（进而 `ReadFifoPacket`） | `Level` 寄存器为 0，FIFO 无数据 |
| `MmReader_NoCompletePacketInFifo` | -3 | `ReadFifoPacket` | 缓冲区写满仍未遇 `last`，FIFO 里没有完整包 |

注意错误的「传染性」：`ReadFifoPacket` 内部循环调用 `ReadFifoEntry`，所以 `FifoIsEmpty` 也会从 `ReadFifoPacket` 透传出来（循环中途 FIFO 被别人读空了的情况）。

#### 4.4.3 源码精读

[drivers/axi_mm_reader/src/axi_mm_reader.h:L23-L29](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/drivers/axi_mm_reader/src/axi_mm_reader.h#L23-L29) —— 枚举定义，四个值，成功为 0、错误为负。

三个错误码的产生点散布在 `.c` 中：

- `IpMustBeDisabled`：[axi_mm_reader.c:L48-L50](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/drivers/axi_mm_reader/src/axi_mm_reader.c#L48-L50)
- `FifoIsEmpty`：[axi_mm_reader.c:L80-L82](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/drivers/axi_mm_reader/src/axi_mm_reader.c#L80-L82)
- `NoCompletePacketInFifo`：[axi_mm_reader.c:L110-L112](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/drivers/axi_mm_reader/src/axi_mm_reader.c#L110-L112)

注意 `ReadFifoPacket` 还有一处「透传」：

[drivers/axi_mm_reader/src/axi_mm_reader.c:L100-L102](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/drivers/axi_mm_reader/src/axi_mm_reader.c#L100-L102) —— 循环内一旦 `ReadFifoEntry` 返回非 0（最典型是 `FifoIsEmpty`），立即原样返回，错误向上冒泡。

#### 4.4.4 代码实践（源码阅读型）

**目标**：把每个错误码的触发条件与硬件行为挂钩。

1. 对 `IpMustBeDisabled`：回忆 u2-l3「FSM 只在 `Idle_s` 采样 `RegCount`」。说明为什么改表前必须禁用。
2. 对 `FifoIsEmpty`：回忆 u2-l7「`Level` 寄存器反映 FIFO 水位」。说明 `Level==0` 为何等价于「无数据可读」。
3. 对 `NoCompletePacketInFifo`：回忆 u2-l3「每个读周期结束时在最后一项打 `Last` 标记」。说明「没有完整包」意味着什么。

**预期结果**：三个错误码不再是一串数字，而是三条「调用契约违反」——分别对应「在错的时机改配置」「在没数据时取数」「在包不完整时强取整包」。

#### 4.4.5 小练习与答案

**练习 1**：为什么错误码用负数、成功用 0，而不是反过来？

**参考答案**：这是 C 的普遍惯例（很多标准库函数、POSIX 接口都如此）。好处是「任何非 0 都代表有问题」，调用者可以用 `if (ret != MmReader_Success)` 或更宽松的 `if (ret)` 统一判断；成功值 0 在逻辑表达式中为假，写起来顺手。用枚举具名（而非裸数字）则兼顾了可读性。

**练习 2**：`ReadFifoPacket` 在返回 `NoCompletePacketInFifo` 之前，已经往缓冲区写了多少项？这些项还能用吗？

**参考答案**：已经写了 `size` 项（缓冲区全满）。这些项**已被弹出 FIFO、不可回退**，所以是「可读但残缺」的数据——它们构成某个包的前 `size` 项，但因没有遇到 `last`，软件无法知道这个包原本有多长。稳妥做法是丢弃这批数据并检查 IP 是否配置异常（例如 `size` 小于 `RegCount`），或干脆让缓冲区足够大（≥ `MaxRegCount_g`）以避免触发此错误。

---

### 4.5 驱动集成：Makefile、MDD、TCL 与 xparameters.h

#### 4.5.1 概念说明

驱动代码写完只是第一步，还要让 Vitis 的 BSP 工具链「认识」它、把它编译进 `libxil.a`、并在生成 `xparameters.h` 时为该 IP 实例写入基地址。这套集成由三个小文件分工完成：

- **`Makefile`**：Vitis 调用它，把 `.c` 编译成 `.o`、归档进 `libxil.a`，把 `.h` 拷进 BSP 的 include 目录。
- **`.mdd`（Microprocessor Driver Description）**：声明「这个驱动服务于哪种外设」，把驱动绑定到 IP 名 `axi_mm_reader`。
- **`.tcl`**：BSP 生成阶段执行的脚本，负责向 `xparameters.h` 注入该 IP 实例的常量。

这三者都是 Xilinx 驱动框架的约定产物——驱动开发者照模板填即可。

#### 4.5.2 核心流程

```text
Vivado 打包 IP（scripts/package.tcl，见 u1-l4）
   └─ add_drivers_relative 把 .c/.h 纳入 IP；data/ 下的 .mdd/.tcl 作为驱动声明随 IP 分发
        │
Vitis 创建 BSP
   ├─ 读 .mdd：发现外设 axi_mm_reader 对应这个驱动 ──► 选中它
   ├─ 执行 .tcl 的 generate ──► 写 xparameters.h（NUM_INSTANCES/DEVICE_ID/C_BASEADDR/C_HIGHADDR）
   └─ 执行 Makefile 的 libs 目标 ──► 编译 .c ──► 归档进 libxil.a
        │
应用程序
   └─ #include "axi_mm_reader.h"，用 xparameters.h 里的 C_BASEADDR 调用 API
```

#### 4.5.3 源码精读

`Makefile` 的核心目标 `libs`：

[drivers/axi_mm_reader/src/Makefile:L14-L21](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/drivers/axi_mm_reader/src/Makefile#L14-L21) —— 先用 `$(wildcard *.c)` 显式枚举所有 `.o` 目标，`libs` 目标编译所有 `.c`、用 `$(ARCHIVER) -r` 归档进 `libxil.a`，最后 `make clean`。

这里有个值得讲的设计细节：早期版本用的是 `OUTS = *.o`（一个裸 glob 字符串），后来改成：

[drivers/axi_mm_reader/src/Makefile:L14-L15](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/drivers/axi_mm_reader/src/Makefile#L14-L15) —— `OBJECTS = $(addsuffix .o, $(basename $(wildcard *.c)))`，在 Make 解析期就把 `.o` 文件名展开成具体列表。

这次改动（commit `bb0c212`，"BUGFIX: Fix driver Makefile to work with Vitis (Windows)"）是为了修一个 Windows/Vitis 下的真实 bug：当 `ar`（归档器）收到字面字符串 `*.o` 时，在某些 shell/平台上不会做通配展开，导致归档失败或库为空；而在 Make 解析期用 `$(wildcard)` 显式求值后，传给 `ar` 的就是具体的 `axi_mm_reader.o`，问题消失。这是「把通配符求值从归档器挪到 Make」的标准修法。

MDD 声明：

[drivers/axi_mm_reader/data/axi_mm_reader.mdd:L5-L10](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/drivers/axi_mm_reader/data/axi_mm_reader.mdd#L5-L10) —— `supported_peripherals = (axi_mm_reader)` 把驱动与 IP 名绑定；`copyfiles = all` 表示 BSP 生成时拷贝全部驱动文件；`VERSION=1.0`、`NAME=axi_mm_reader`。

TCL 生成 `xparameters.h`：

[drivers/axi_mm_reader/data/axi_mm_reader.tcl:L3-L5](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/drivers/axi_mm_reader/data/axi_mm_reader.tcl#L3-L5) —— `generate` 过程调用 Xilinx 的 `xdefine_include_file`，向 `xparameters.h` 写入 `NUM_INSTANCES`、`DEVICE_ID`、`C_BASEADDR`、`C_HIGHADDR` 四个字段。其中 `C_BASEADDR` 就是应用程序传给每个 API 第一个参数 `baseAddr` 的来源。

打包阶段如何把驱动纳入 IP（回顾 u1-l4）：

[scripts/package.tcl:L61-L64](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/scripts/package.tcl#L61-L64) —— `add_drivers_relative` 显式加入 `axi_mm_reader.c` 与 `axi_mm_reader.h`。注意这里只列了 `.c`/`.h`，`data/` 下的 MDD/TCL 由 PsiIpPackage/IP-XACT 的驱动约定一并带入 IP 包，供 Vitis BSP 工具链识别。

#### 4.5.4 代码实践（源码阅读型）

**目标**：追踪 `baseAddr` 这个参数从硬件平台到应用程序的完整传递链。

1. 在 Vivado Block Design 里，该 IP 实例被分配一个基地址（如 `0x4000_0000`）。
2. Vitas 生成 BSP 时，`axi_mm_reader.tcl` 的 `generate` 把它写进 `xparameters.h`，形如 `#define XPAR_AXI_MM_READER_0_BASEADDR 0x40000000`。
3. 应用程序里：`MmReader_SetEnable(XPAR_AXI_MM_READER_0_BASEADDR, true);`，驱动内部 `baseAddr + 0x00` 命中 `Ena` 寄存器。

**预期结果**：你能解释「为什么驱动函数第一个参数叫 `baseAddr`、它的值从哪里来」。这一步**待本地验证**（需要真实 Vivado/Vitis 工程才能看到生成的 `xparameters.h`），但传递逻辑本身可在源码层完全确认。

#### 4.5.5 小练习与答案

**练习 1**：`Makefile` 里 `libs` 目标最后为什么调 `make clean`？

**参考答案**：为了不留临时 `.o` 文件。Vitis BSP 是一套干净的分发物，编译产生的中间目标文件不应残留；`libs` 归档完立刻 `clean` 删掉 `OBJECTS`/`ASSEMBLY_OBJECTS`，保证 BSP 目录里只剩源码与归档库。

**练习 2**：如果要让驱动支持两个独立的 `axi_mm_reader` 实例，`.tcl` 里的 `NUM_INSTANCES` 字段起什么作用？

**参考答案**：`NUM_INSTANCES` 告诉应用程序「平台里有几个该 IP 实例」。BSP 工具会为每个实例生成各自的 `DEVICE_ID`/`C_BASEADDR`/`C_HIGHADDR`（如 `…_0_BASEADDR`、`…_1_BASEADDR`），应用程序据此分别初始化、分别传不同的 `baseAddr` 调用驱动。驱动代码本身是单实例、可重入的（全部参数由调用者传入，无全局状态），所以天然支持多实例。

---

## 5. 综合实践

把本讲四个模块串成一个最小的端到端用例：**禁用 IP → 配置寄存器表 → 使能 → 等数据 → 读一个完整包并打印**，并解释 `SetRegTable` 为何要求禁用。

下面的程序是**示例代码**（非仓库原有文件），假定运行在 Vitis 裸机环境，IP 配置为 AXIMM 输出模式：

```c
/* 示例代码：演示 axi_mm_reader 驱动的典型调用顺序 */
#include <stdio.h>
#include "xparameters.h"
#include "axi_mm_reader.h"

#define BASE XPAR_AXI_MM_READER_0_BASEADDR   /* 来自 .tcl 生成的 xparameters.h */

/* 假设要把两个外部寄存器每周期各读一次：
 *   0x40010000 —— 例如某状态寄存器
 *   0x40010004 —— 例如某温度寄存器
 * MaxRegCount_g 在综合时须 >= 2，否则 SetRegTable 不报错但越界（见 4.2.5）。 */
static const uint32_t kRegs[] = { 0x40010000u, 0x40010004u };

int main(void) {
    MmReader_ErrCode ret;
    uint32_t buf[8];          /* 容量 >= 一个包的长度即可，这里给足余量 */
    uint32_t pktSize = 0;

    /* 1) 先禁用 IP —— SetRegTable 的硬性前提 */
    ret = MmReader_SetEnable(BASE, false);
    if (ret != MmReader_Success) { printf("disable failed: %d\n", ret); return 1; }

    /* 2) 配置寄存器表（IP 必须处于禁用态，否则返回 IpMustBeDisabled） */
    ret = MmReader_SetRegTable(BASE, kRegs, sizeof(kRegs)/sizeof(kRegs[0]));
    if (ret != MmReader_Success) { printf("set table failed: %d\n", ret); return 1; }

    /* 3) 使能 —— 此后读周期由硬件 Trig 或内部超时自动启动（驱动无法软件触发） */
    ret = MmReader_SetEnable(BASE, true);
    if (ret != MmReader_Success) { printf("enable failed: %d\n", ret); return 1; }

    /* 4) 轮询 Level，直到 FIFO 里至少有一个完整包的数据 */
    uint32_t level = 0;
    do {
        ret = MmReader_GetLevel(BASE, &level);
    } while (ret == MmReader_Success && level == 0);

    /* 5) 读出完整包并打印 */
    ret = MmReader_ReadFifoPacket(BASE, buf, sizeof(buf)/sizeof(buf[0]), &pktSize);
    if (ret != MmReader_Success) {
        printf("read packet failed: %d\n", ret);
    } else {
        printf("got a packet of %lu words:\n", (unsigned long)pktSize);
        for (uint32_t i = 0; i < pktSize; i++) {
            printf("  [%lu] = 0x%08lX\n", (unsigned long)i, (unsigned long)buf[i]);
        }
    }
    return 0;
}
```

**操作步骤与观察点**：

1. 把示例代码放进 Vitis 应用工程，确保 BSP 里已包含 `axi_mm_reader` 驱动（即 `libxil.a` 里有它）。
2. 编译、下载到目标板，运行。
3. 观察串口输出：应先看到「got a packet of 2 words」，再看到两行寄存器值。`pktSize` 应等于 `SetRegTable` 时传入的 `numRegs`（这里是 2）。

**为什么 `SetRegTable` 必须在禁用态调用**：

- 硬件侧（u2-l3）：核心 FSM 只在 `Idle_s` 采样 `RegCount`，并在整个读周期内遍历 RegTable。若在周期进行中改写表或计数，FSM 会读到「半新半旧」的地址，读出错误数据。
- 软件侧（4.2.3）：`SetRegTable` 入口主动 `GetEnable`，使能态直接返回 `IpMustBeDisabled`，把上述硬件约束变成可检测的契约。
- 工程模式：因此规范用法是「禁用 → 改配置 → 使能」三步走，示例代码正是按此顺序编写。如果在上面的示例里把第 1 步 `SetEnable(BASE, false)` 去掉、且 IP 恰好处于使能态，第 2 步会立刻返回 `-1`（`IpMustBeDisabled`），程序打印 `set table failed: -1` 并退出。

> 运行时现象需在真实 Vivado/Vitis + 硬件环境验证；本仓库不提供可在 PC 上直接跑的主机程序，故运行结果标注为**待本地验证**。在没有硬件时，可以把示例代码当作「调用顺序与契约」的阅读材料，并在 `top_tb`（见 u3-l2）里对照激励用例确认这些 API 的预期行为。

## 6. 本讲小结

- 驱动是一层极薄的封装：全部 API 的本质是「按地址常量算字节地址 + 一次 `Xil_In32`/`Xil_Out32`」，地址常量与 RTL `definitions_pkg.vhd` 的字索引严格对应（字节地址 = 字索引 × 4）。
- `SetRegTable` 是逻辑最重的函数，它复用 `GetEnable` 做使能检查、写整张 `Addr[]` 表、最后写 `RegCount`；调用前 IP 必须禁用，否则返回 `IpMustBeDisabled`，对应硬件「FSM 仅在 `Idle_s` 采样 `RegCount`」的约束。
- AXIMM 模式下取数必须「先读 `RdLast`（peek）、再读 `RdData`（pop）」，因为读 `RdData` 会弹出 FIFO 一项；这一约定由 wrapper 的 `AxiS_Rdy <= reg_rd(RegIdx_RdData_c)` 在硬件侧落地。
- 错误码体系用「0 成功、负数失败」的枚举统一返回值，三个错误码分别对应「改表时未禁用」「FIFO 空」「缓冲区写满仍无完整包」三种契约违反，且会从 `ReadFifoPacket` 向 `ReadFifoEntry` 透传。
- 驱动没有任何软件触发寄存器——读周期只能由硬件 `Trig` 或内部超时启动；驱动的角色是「配置 + 使能 + 取数」，不是「发命令」。
- 集成三件套：`Makefile`（`$(wildcard)` 显式枚举 `.o` 以兼容 Vitis/Windows）把驱动编进 `libxil.a`；`.mdd` 把驱动绑定到外设名；`.tcl` 向 `xparameters.h` 写入 `C_BASEADDR` 等，提供 API 的 `baseAddr`。

## 7. 下一步学习建议

- **u3-l2 测试台架构与用例**：本讲多次提到「AXIMM 模式」「先读 RdLast 再读 RdData」「FIFO 缓冲」等行为，`tb/top_tb.vhd` 的自校验测试台正是这些行为被验证的地方，建议接着读它，把软件视角的契约和硬件视角的校验对上。
- **u3-l3 参数化与 GUI 配置**：本讲反复出现的 `MaxRegCount_g`、输出模式（AXIS/AXIMM）等参数在 GUI 侧如何配置、如何映射到 RTL generic，是驱动与硬件协同的下一层细节。
- **u3-l5 二次开发实践：扩展该 IP**：当你想给 IP 增加一个新寄存器（例如把 `DoneCnt` 或 `Level` 暴露成只读寄存器），需要同步改动 `definitions_pkg.vhd`（地址）、wrapper（解码）、文档、`top_tb`（用例）**以及本讲的驱动**（新增地址常量 + API）——这是把本讲知识用于实战的最佳落脚点。
- 若想深入 Xilinx 驱动框架本身（`xdefine_include_file`、MDD 语法、BSP 生成流程），可进一步阅读 Vitis 嵌入式平台的官方《Driver Development Guide》，本仓库的 `.mdd`/`.tcl` 是最小可读样例。
