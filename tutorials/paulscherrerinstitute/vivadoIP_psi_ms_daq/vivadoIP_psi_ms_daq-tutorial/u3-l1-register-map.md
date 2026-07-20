# 寄存器映射全景

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 psi_ms_daq IP-Core 的**四类寄存器地址空间**——通用、逐流录制、逐流上下文、窗口——各自的基址与步进公式。
- 看懂 `psi_ms_daq.h` 里那些带参数的宏（如 `PSI_MS_DAQ_REG_MODE(n)`、`PSI_MS_DAQ_WIN_WINCNT(n,w,so)`）是如何把「流号 n、窗口号 w、流偏移 so」翻译成 32 位字节地址的。
- 解释关键使能位：`GCFG_BIT_ENA`、`GCFG_BIT_IRQENA`、`IRQVEC`、`IRQENA`、`STRENA` 各管什么。
- 读懂 `WINCNT` 寄存器里 `[30:0]` 是采样数、`[31]` 是 ISTRIG（是否含触发）的编码约定。
- 解释窗口地址空间里「每流偏移 `strAddrOffs`」是如何由 `maxWindows` 推出的，并能手算给定 `stream/window/maxWindows` 下的窗口寄存器地址。

## 2. 前置知识

在进入本讲前，你需要先建立以下心智模型（这些都在前序讲义中讲过）：

- **本仓库只是 Vivado 封装层**：真正的 VHDL 功能逻辑在上游 `psi_multi_stream_daq`，C 驱动真身也在上游；本地 `drivers/*.c *.h` 只是打包时拷贝下来的自包含副本（见 u1-l2、u1-l4）。
- **CPU 只通过 AXI Slave 配置寄存器**：IP-Core 把采集到的数据经 AXI Master 直接写进 DDR，CPU 不搬数据，只读写一组 32 位寄存器来「下命令 / 读状态」（见 u1-l1）。
- **IP 打包产物的接口契约**：`package.tcl` 把泛型包成 GUI 控件、把端口按条件使能（见 u1-l4、u2-l1）。

本讲我们要回答的问题是：**CPU 到底往哪些地址写、从哪些地址读，才能驱动这个 IP？** 这些地址的定义全部集中在驱动头文件 `psi_ms_daq.h` 的宏里，并由 `.c` 文件里的访问函数叠加一个 `baseAddr` 后真正发出。

几个本讲会用到的术语：

| 术语 | 含义 |
|---|---|
| 寄存器（register） | IP 内部一个 32 位的存储单元，CPU 通过 AXI Slave 读写它 |
| 字节地址（byte address） | 本讲所有宏给出的都是**字节地址**，最终访问函数会把它加到 `baseAddr` 上 |
| 流（stream） | 一路 AXI-Stream 输入，最多 16 路，编号 0..15 |
| 窗口（window） | 一条流上的一段独立录制区域，一条流最多 `maxWindows` 个 |
| RMW | Read-Modify-Write，先读整寄存器、改某几位、再写回，用于按字段访问 |

## 3. 本讲源码地图

本讲只涉及两个文件：

| 文件 | 在本讲中的作用 |
|---|---|
| `drivers/psi_ms_daq_axi/src/psi_ms_daq.h` | **唯一事实来源**。第 145–183 行用 `#define` 列出了全部寄存器地址宏与比特字段宏，是本讲精读的核心。 |
| `drivers/psi_ms_daq_axi/src/psi_ms_daq.c` | 提供两个补充视角：①`PsiMsDaq_Init` 里 `strAddrOffs` 的计算（第 143 行）；②`PsiMsDaq_RegWrite/RegRead` 如何把宏地址叠加 `baseAddr`（第 730–752 行）。 |

> 提醒：这两个文件每次 IP 打包都会被上游同名文件覆盖（见 u1-l2）。但寄存器映射是 IP 与驱动之间的**稳定接口契约**，通常不会随版本随意变动，所以精读它们是安全的。

## 4. 核心概念与源码讲解

### 4.1 寄存器地址空间全景：为什么分成四类

#### 4.1.1 概念说明

psi_ms_daq 的寄存器空间不是一张平坦的大表，而是按「**作用范围**」分成四块互不重叠的区域：

1. **通用寄存器（General）**：整个 IP 只有一份，管全局使能、全局状态、中断向量/使能、所有流的使能位。
2. **逐流录制寄存器（Per-Stream Recording）**：每条流一组，管「这条流怎么录制」——触发后采样数、录制模式、ARM/REC 状态、最近写完的窗口号。
3. **逐流上下文寄存器（Per-Stream Context）**：每条流一组，管「这条流往 DDR 哪里写」——缓冲起始地址、窗口大小、写指针、缓冲结束地址，以及环形/覆盖/窗口数等运行时配置。
4. **窗口寄存器（Per-Window）**：每条流的每个窗口一组，管「这个窗口录到了什么」——采样数与是否含触发、最后一个采样地址、时间戳低/高 32 位。

为什么要分四块、而且每块用不同的步进？因为 IP 内部对它们的访问频率和实现成本不同：通用与逐流寄存器数量少、地址稀疏；窗口寄存器数量巨大（`maxStreams × maxWindows` 个），必须用规整的二维数组式编址才能用简单的算术寻址。

#### 4.1.2 核心流程

所有寄存器地址都由头文件里的**参数化宏**生成。宏的参数就是「流号 n」「窗口号 w」「流偏移 so」，宏内部用整式算出字节地址。CPU 侧调用链如下：

```
应用代码 / 驱动内部
   │  调用 PsiMsDaq_RegWrite(ip, PSI_MS_DAQ_REG_MODE(2), val)
   │  宏展开：PSI_MS_DAQ_REG_MODE(2) = 0x208 + 0x10*2 = 0x228
   ▼
PsiMsDaq_RegWrite(ip, 0x228, val)
   │  addr = inst->baseAddr + 0x228
   ▼
inst->regWrFct(baseAddr + 0x228, val)   // 默认实现：*(volatile u32*)(baseAddr+0x228) = val
   │
   ▼  AXI Slave 写事务 → IP 内部寄存器
```

四类地址空间的基址与步进一览（下文逐块精读）：

| 类别 | 基址 | 流间步进 | 窗口间步进 | 代表宏 |
|---|---|---|---|---|
| 通用 | `0x000` | — | — | `PSI_MS_DAQ_REG_GCFG` |
| 逐流录制 | `0x200` | `0x10` | — | `PSI_MS_DAQ_REG_MODE(n)` |
| 逐流上下文 | `0x1000` | `0x20` | — | `PSI_MS_DAQ_CTX_SCFG(n)` |
| 窗口 | `0x4000` | `strAddrOffs`（运行时算） | `0x10` | `PSI_MS_DAQ_WIN_WINCNT(n,w,so)` |

注意：逐流录制寄存器一组有 4 个（每 `0x10` 里 4 个 32 位字），上下文寄存器一组有 5 个（每 `0x20` 里 5 个字，留了对齐空隙），所以两者的流间步进不同（`0x10` vs `0x20`）。

#### 4.1.3 源码精读

寄存器宏集中在一个 `/// @cond ... /// @endcond` 块里（Doxygen 不导出，因为这是给驱动内部用的）。开头两行注释把四块区域点了出来：

```
//ACQCONF Registers - General           ← 通用
//ACQCONF Registers - Per Stream        ← 逐流录制
//CTXMEM for Stream n                   ← 逐流上下文
//WNDW Window w for Stream n            ← 窗口
```

这是 `psi_ms_daq.h` 第 146–175 行的注释分段：[drivers/psi_ms_daq_axi/src/psi_ms_daq.h:145-183](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L145-L183) ——这一段就是本讲全部内容的「目录」。

地址如何叠加 `baseAddr`：`PsiMsDaq_RegWrite` 把宏算出的偏移加上实例的 `baseAddr`，再交给注入的写函数：[drivers/psi_ms_daq_axi/src/psi_ms_daq.c:730-740](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L730-L740)。`RegRead` 对称：[drivers/psi_ms_daq_axi/src/psi_ms_daq.c:742-752](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L742-L752)。所以宏里写的 `0x200`、`0x1000`、`0x4000` 都是**相对 IP 基址的偏移**，不是绝对物理地址。

#### 4.1.4 代码实践

**实践目标**：用肉眼把四类寄存器空间「分拣」开，确认它们互不重叠。

**操作步骤**：

1. 打开 `psi_ms_daq.h` 第 145–183 行。
2. 对每个 `#define PSI_MS_DAQ_...`，看它的地址表达式落在哪个区间：
   - 通用：`0x000`–`0x020` 附近
   - 逐流录制：`0x200+`
   - 逐流上下文：`0x1000+`
   - 窗口：`0x4000+`
3. 算一下「逐流录制」区域最多占多少：16 路 × `0x10` = `0x100`，即 `0x200`–`0x300`，不会撞到 `0x1000` 的上下文区。

**需要观察的现象**：四块区域的起始地址（`0x000 / 0x200 / 0x1000 / 0x4000`）之间留了很大空隙，这是 IP-XACT 地址译码器按高位片选实现的典型布局。

**预期结果**：你会确认四类空间两两不重叠，且高位地址（如 `0x4000` 对应地址线 bit14=1）可作为简单的片选条件。

**待本地验证**：若你在 Vivado 里打开 IP 的地址编辑器，能看到 AXI Slave 的地址范围下限是 `0x0`、上限随 `maxWindows/maxStreams` 变化，可与此处手算对照。

#### 4.1.5 小练习与答案

**练习 1**：逐流录制寄存器的流间步进是 `0x10`，逐流上下文寄存器的流间步进却是 `0x20`。为什么上下文要用两倍步进？

> **答案**：因为上下文寄存器一组有 5 个 32 位字（SCFG/BUFSTART/WINSIZE/PTR/WINEND），占用 `0x14` 字节；按 32 位总线对齐到下一个 2 的幂边界就是 `0x20`（留出 `0xC` 的对齐空隙）。而逐流录制一组只有 4 个字（MAXLVL/POSTTRIG/MODE/LASTWIN），正好 `0x10`。

**练习 2**：窗口区基址 `0x4000` 在二进制里是 `01 0000 0000 0000`，对应地址线哪一位为 1？这对手写地址译码有什么便利？

> **答案**：是 bit14。地址译码器只要判 `addr[14]==1 && addr[15..]==0`（配合更高位的上下文/录制片选）即可选中窗口区，省去复杂比较器。

---

### 4.2 通用寄存器与逐流录制寄存器（REG_*）

#### 4.2.1 概念说明

**通用寄存器**只有几个，但它们是「总开关」：

- `GCFG`（Global Config）：整个 IP 的使能位 `ENA`（bit0）和全局中断使能 `IRQENA`（bit8）。
- `GSTAT`（Global Status）：全局状态（本驱动未直接使用，留给高级调试）。
- `IRQVEC`（IRQ Vector）：**每一位对应一条流**，某位为 1 表示该流触发了中断；写 1 清除（电平中断的应答机制）。
- `IRQENA`（IRQ Enable）：**每一位对应一条流**，置 1 才允许该流的中断送到 IRQ 输出。
- `STRENA`（Stream Enable）：**每一位对应一条流**，置 1 才允许该流开始录制。

**逐流录制寄存器**每条流一组（4 个字），描述「这条流的录制行为」：

- `MAXLVL(n)`：该流输入 FIFO 出现过的最大填充水位（调试用，可清零）。
- `POSTTRIG(n)`：触发后要录的采样数（含触发样本本身）。
- `MODE(n)`：录制模式字段 `RECM[1:0]`（bit0–1）+ ARM 位（bit8）+ REC 位（bit16，硬件置位表示正在录）。
- `LASTWIN(n)`：该流最近一个完整写完的窗口号。

#### 4.2.2 核心流程

通用寄存器的「位 = 流」映射是 psi_ms_daq 的一个关键设计模式：`IRQVEC`、`IRQENA`、`STRENA` 三个寄存器**用同一个位编码**表示 16 条流。所以：

- 使能第 2 条流录制 → `STRENA |= (1<<2)`。
- 应答第 2 条流的中断 → `IRQVEC = (1<<2)`（写 1 清除）。
- 查谁触发了中断 → 读 `IRQVEC`，看哪些位为 1。

逐流录制寄存器的地址由流号 `n` 参数化：

\[ \text{REG}(n) = \text{基址} + 0x10 \cdot n \]

四种逐流寄存器的基址分别是 `0x200 / 0x204 / 0x208 / 0x20C`（正好落在同一个 `0x10` 槽位里的 4 个字）。

`MODE` 寄存器的字段布局（一图看清）：

```
 bit31........................bit16 ...bit8 ........bit1 bit0
┌─────────────────────────────┬──────────┬─────────────┬─────┐
│          (保留)             │   REC    │  ARM        │ RECM│
│                             │ (硬件置位)│ (软件置位)  │[1:0]│
└─────────────────────────────┴──────────┴─────────────┴─────┘
```

#### 4.2.3 源码精读

通用寄存器五个地址（注意 `IRQVEC=0x010`、`IRQENA=0x014`、`STRENA=0x020` 与 GCFG/GSTAT 之间留了空隙）：[drivers/psi_ms_daq_axi/src/psi_ms_daq.h:147-153](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L147-L153)。其中 `GCFG_BIT_ENA=(1<<0)`、`GCFG_BIT_IRQENA=(1<<8)` 是两个关键使能位。

逐流录制寄存器与 MODE 的字段定义：[drivers/psi_ms_daq_axi/src/psi_ms_daq.h:155-162](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L155-L162)。注意 `RECM` 占 bit0–bit1（LSB=0, MSB=1，两位可编码 4 种模式），`ARM=(1<<8)`、`REC=(1<<16)`。

「位 = 流」模式在初始化里被反复使用。`PsiMsDaq_Init` 复位时写 0 给 `GCFG / STRENA / IRQENA`，并向 `IRQVEC` 写 `0xFFFFFFFF` 清除所有挂起中断：[drivers/psi_ms_daq_axi/src/psi_ms_daq.c:156-159](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L156-L159)。最后再置 `ENA|IRQENA` 完成「先全关、再只开全局使能」的复位序列：[drivers/psi_ms_daq_axi/src/psi_ms_daq.c:178-180](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L178-L180)。

「位 = 流」也用在判断流是否已禁用的守卫函数里——读 `STRENA` 测试对应位：[drivers/psi_ms_daq_axi/src/psi_ms_daq.c:68-77](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L68-L77)。中断处理里读 `IRQVEC` 再回写清除，正是电平中断的应答套路：[drivers/psi_ms_daq_axi/src/psi_ms_daq.c:204-205](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L204-L205)。

#### 4.2.4 代码实践

**实践目标**：手算流 2 与流 3 的逐流录制寄存器地址，体会「流间步进 0x10」。

**操作步骤**：

1. 对 `n=2` 展开：`MAXLVL(2)=0x200+0x10*2=0x220`、`POSTTRIG(2)=0x204+0x20=0x224`、`MODE(2)=0x208+0x20=0x228`、`LASTWIN(2)=0x20C+0x20=0x22C`。
2. 对 `n=3` 展开：基址整体再加 `0x10`，即 `0x230 / 0x234 / 0x238 / 0x23C`。
3. 验证流 2 的 4 个寄存器是否都落在 `[0x220, 0x230)` 这一个 `0x10` 槽位里。

**需要观察的现象**：流号每加 1，4 个寄存器地址整体平移 `0x10`；同一条流的 4 个寄存器紧密排列在同一槽位。

**预期结果**：流 2 → `0x220..0x22C`；流 3 → `0x230..0x23C`。两流的 `MODE` 地址分别是 `0x228`、`0x238`，相差正好 `0x10`。

#### 4.2.5 小练习与答案

**练习 1**：要让 IP 同时录制流 0 和流 5，应该往 `STRENA`（`0x020`）写什么值？

> **答案**：置位 bit0 和 bit5，即 `(1<<0)|(1<<5) = 0x21`。注意驱动不是直接写裸值，而是用 RMW（`PsiMsDaq_Str_SetEnable` 内部按位或），以免清掉其他流的使能位。

**练习 2**：`MODE` 寄存器的 `RECM` 字段是 `[1:0]` 两 bit，但 `ARM` 却在 bit8、`REC` 在 bit16，中间隔了 6 位和 14 位。为什么不把它们紧挨着放？

> **答案**：把状态/控制位分散到不同的字节边界，便于软件按字节或按位带（bit-band）单独访问，也便于硬件把这些位分到不同的子模块；同时 `RECM` 占满最低字节的部分位、`ARM` 落在第 2 字节、`REC` 落在第 3 字节，使各自可以用独立的 `RegSetBit` 而互不干扰。

---

### 4.3 逐流上下文寄存器（CTX_*）

#### 4.3.1 概念说明

逐流录制寄存器（4.2）管的是「怎么录」，而**上下文寄存器（CTXMEM）**管的是「录到 DDR 的哪里、缓冲怎么组织」。每条流一组 5 个字：

- `SCFG(n)`（Stream Context Config）：一个寄存器塞了 4 个字段：
  - `RINGBUF`（bit0）：该流的多个窗口是否当成**环形缓冲**用（true=环形，false=线性）。
  - `OVERWRITE`（bit8）：窗口数据未被软件确认释放前，是否允许被新数据覆盖。
  - `WINCNT[20:16]`：该流实际使用的窗口数（注意寄存器里存的是 `窗口数-1`，见 u3-l4）。
  - `WINCUR[28:24]`：硬件当前正在写入的窗口号（只读状态）。
- `BUFSTART(n)`：该流缓冲在 DDR 中的起始地址（IP 视角的绝对地址）。
- `WINSIZE(n)`：每个窗口的字节大小。
- `PTR(n)`：硬件当前写入指针（字节地址，只读状态）。
- `WINEND(n)`：该流缓冲的结束地址（IP 用来判断回绕）。

#### 4.3.2 核心流程

上下文寄存器的流间步进是 `0x20`，地址由流号参数化：

\[ \text{CTX}(n) = \text{基址} + 0x20 \cdot n, \quad \text{基址} \in \{0x1000, 0x1004, 0x1008, 0x100C, 0x1010\} \]

`SCFG` 寄存器是上下文区里信息密度最高的一个，字段布局：

```
 bit31...bit28  bit27...bit24  bit23...bit21  bit20...bit16  bit15...bit9   bit8      bit7...bit1   bit0
┌──────────────┬──────────────┬───────────────┬──────────────┬─────────────┬──────────┬─────────────┬─────────┐
│   (保留)     │  WINCUR[3:0]  │   (保留)      │ WINCNT[4:0]  │   (保留)    │ OVERWRITE│   (保留)    │ RINGBUF │
└──────────────┴──────────────┴───────────────┴──────────────┴─────────────┴──────────┴─────────────┴─────────┘
```

注意 `WINCNT` 与 `WINCUR` 都是 5 位字段，因此单流最多支持 32 个窗口（与 IP 上限一致，见 u1-l1）。

#### 4.3.3 源码精读

五个上下文寄存器的地址宏：[drivers/psi_ms_daq_axi/src/psi_ms_daq.h:164-174](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L164-L174)。`SCFG` 的四个字段（`RINGBUF` bit0、`OVERWRITE` bit8、`WINCNT` bit16–20、`WINCUR` bit24–28）紧随其后。

这些字段在流配置时被一一写入。`PsiMsDaq_Str_Configure` 用 `RegSetBit` 写 `RINGBUF`、`OVERWRITE`，用 `RegSetField` 写 `WINCNT`（注意传入 `winCnt-1`），用 `RegWrite` 写 `BUFSTART`、`WINSIZE`：[drivers/psi_ms_daq_axi/src/psi_ms_daq.c:292-310](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L292-L310)。

`WINCUR` 与 `PTR` 这两个**只读状态**字段在高级查询函数里被读出——`Str_CurrentWin` 读 `WINCUR`：[drivers/psi_ms_daq_axi/src/psi_ms_daq.c:689-704](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L689-L704)；`Str_CurrentPtr` 读 `PTR`：[drivers/psi_ms_daq_axi/src/psi_ms_daq.c:706-715](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L706-L715)。

#### 4.3.4 代码实践

**实践目标**：体会「录制配置」与「上下文配置」落在不同地址区，但都属于同一条流。

**操作步骤**：以流 4 为例，列出它全部 9 个相关寄存器的地址：

1. 逐流录制区（步进 `0x10`，基址 `0x200+`）：`MAXLVL(4)=0x240`、`POSTTRIG(4)=0x244`、`MODE(4)=0x248`、`LASTWIN(4)=0x24C`。
2. 上下文区（步进 `0x20`，基址 `0x1000+`）：`SCFG(4)=0x1000+0x20*4=0x1080`、`BUFSTART(4)=0x1084`、`WINSIZE(4)=0x1088`、`PTR(4)=0x108C`、`WINEND(4)=0x1090`。

**需要观察的现象**：同一条流的「录制」寄存器和「上下文」寄存器被放到了地址空间里相距很远（`0x240` vs `0x1080`）的两个区，靠流号 `n` 各自独立索引。

**预期结果**：流 4 的录制寄存器在 `0x240–0x24C`，上下文寄存器在 `0x1080–0x1090`。

#### 4.3.5 小练习与答案

**练习 1**：`SCFG` 的 `WINCNT` 字段是 5 位。这对应 IP 最多支持多少个窗口？为什么窗口寄存器宏里的 `so`（流偏移）必须据此设计？

> **答案**：5 位最多编码 0–31，但寄存器里存的是 `窗口数-1`，所以实际可表示 1–32 个窗口，上限 32（与 IP 上限一致）。因为每条流的窗口区在地址空间里是连续 `窗口数 × 0x10` 字节，`so` 必须大于等于这个大小，否则相邻流的窗口会地址重叠。

**练习 2**：`SCFG` 里 `RINGBUF` 与 `OVERWRITE` 为什么不用一个字段而是两个独立 bit？

> **答案**：它们是正交的两个开关：`RINGBUF` 决定窗口内缓冲是环形还是线性（影响单窗口内数据是否回绕），`OVERWRITE` 决定窗口之间是否允许未确认就覆盖（影响多窗口策略）。正交开关用独立 bit，方便单独 `RegSetBit` 而不影响对方。

---

### 4.4 窗口寄存器与 strAddrOffs 的来源（WIN_*）

#### 4.4.1 概念说明

**窗口寄存器**是地址空间里最庞大的一块：每条流的每个窗口一组（4 个字），总数 \( \text{maxStreams} \times \text{maxWindows} \)。每组包含：

- `WINCNT(n,w)`：**一个寄存器两个含义**——
  - `CNT[30:0]`（bit0–30）：该窗口已录到的采样数。
  - `ISTRIG`（bit31）：该窗口是否包含触发样本（1=含触发）。软件据此判断能否算 preTrig。
- `LAST(n,w)`：该窗口最后一个采样（不是字节）的 DDR 地址。
- `TSLO(n,w)`：窗口时间戳低 32 位。
- `TSHI(n,w)`：窗口时间戳高 32 位（拼成 64 位时间戳）。

由于窗口数量随 `maxWindows` 变化，**窗口区的「流间步进」无法在头文件里写死**，必须由驱动在运行时算出来，这就是 `strAddrOffs`（stream address offset）。头文件宏用第三个参数 `so` 把它留作占位，等 `.c` 把真值填进去。

#### 4.4.2 核心流程

窗口地址是二维索引 \((n, w)\) 映射到一维字节地址：

\[ \text{WIN}(n, w) = 0x4000 + \text{strAddrOffs} \cdot n + 0x10 \cdot w \]

- 流号 `n` 乘以**流偏移** `strAddrOffs` 跳到该流的窗口子区起点；
- 窗口号 `w` 乘以 `0x10`（4 个 32 位字）跳到该流内具体窗口。

`strAddrOffs` 的来源在 `PsiMsDaq_Init` 里：

\[ \text{strAddrOffs} = 2^{\lfloor \log_2(\text{maxWindows}) \rfloor} \times 0x10 \]

也就是说，取「不超过 `maxWindows` 的最大 2 的幂」再乘每窗口 `0x10` 字节。当 `maxWindows` 本身是 2 的幂（IP 的典型/约束配置）时，这个值正好等于 \(\text{maxWindows} \times 0x10\)，即每流窗口区正好排满、无空隙。

`WINCNT` 的双含义字段布局：

```
   bit31        bit30........................bit0
┌──────────┬─────────────────────────────────────┐
│ ISTRIG   │            CNT (采样数)             │
│ (1=含触发)│                                     │
└──────────┴─────────────────────────────────────┘
```

软件读取时：采样数 = `RegGetField(WINCNT, lsb=0, msb=30)`；是否含触发 = `RegGetBit(WINCNT, mask=1<<31)`。已知配置里的 `postTrig`，则 preTrig = `CNT - postTrig`（仅当 ISTRIG=1 时才有意义）。

#### 4.4.3 源码精读

四个窗口寄存器宏（注意第三个参数 `so` 就是 `strAddrOffs`，运行时填入）：[drivers/psi_ms_daq_axi/src/psi_ms_daq.h:176-182](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L176-L182)。`WINCNT_LSB_CNT=0`、`WINCNT_MSB_CNT=30`、`WINCNT_BIT_ISTRIG=(1<<31)` 三行紧随其后，定义了双含义字段的边界。

`strAddrOffs` 的计算在 `PsiMsDaq_Init` 第 143 行，依赖三个辅助函数 `Log2 / Log2Ceil / Pow`：[drivers/psi_ms_daq_axi/src/psi_ms_daq.c:99-125](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L99-L125)。计算结果存进实例结构体：[drivers/psi_ms_daq_axi/src/psi_ms_daq.c:143](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L143)。实例结构体里的 `strAddrOffs` 字段：[drivers/psi_ms_daq_axi/src/psi_ms_daq.c:30-39](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L30-L39)。

> **一个值得留意的细节**：`Log2Ceil` 虽然名字带 "Ceil"（向上取整），实现上却是 `Log2`（向下取整 / floor）。因此 \(2^{\text{Log2Ceil}(x)}\) 实际给出的是「不超过 x 的最大 2 的幂」。只有当 `maxWindows` 本身是 2 的幂时，结果才恰好等于 `maxWindows`；这正是 IP 对 `maxWindows` 的取值约束所保证的。换算公式因此是自洽的，但若有人传入非 2 的幂的 `maxWindows`，流间步进会偏小、相邻流窗口区会重叠——这也是为什么 `maxWindows` 必须是 2 的幂。

宏里的 `so` 在实际调用处被填入实例的 `strAddrOffs`，例如读窗口采样数：[drivers/psi_ms_daq_axi/src/psi_ms_daq.c:521-525](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L521-L525)；判断窗口是否含触发（读 ISTRIG 位）：[drivers/psi_ms_daq_axi/src/psi_ms_daq.c:538-541](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L538-L541)；读时间戳低/高字：[drivers/psi_ms_daq_axi/src/psi_ms_daq.c:572-573](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L572-L573)。

#### 4.4.4 代码实践

**实践目标**：亲手把 `strAddrOffs` 算出来，并据此推出指定窗口寄存器的字节地址（本讲核心实践任务）。

**给定**：`stream=2`，`window=5`，`maxWindows=16`。

**操作步骤**：

1. **算 `strAddrOffs`**（按 `PsiMsDaq_Init` 第 143 行的公式）：
   - `Log2Ceil(16)`：因 `16≠0`，得 `Log2(16)`。`Log2` 不断除 2：`16→8→4→2→1`，共 4 次，返回 4。
   - `Pow(2, 4)`：`r=2`，循环 `i=1..3` 各乘一次 2，得 `2→4→8→16`，返回 16。
   - `strAddrOffs = 16 × 0x10 = 256 = 0x100`。

2. **算三个窗口寄存器地址**（公式 `基址 + 0x100×2 + 0x10×5 = 基址 + 0x200 + 0x50 = 基址 + 0x250`）：
   - `WINCNT(2,5,0x100) = 0x4000 + 0x250 = 0x4250`
   - `TSLO(2,5,0x100)   = 0x4008 + 0x250 = 0x4258`
   - `TSHI(2,5,0x100)   = 0x400C + 0x250 = 0x425C`

**需要观察的现象**：
- `strAddrOffs = 0x100`（256 字节/流），正好等于 `maxWindows(16) × 每窗口 0x10`，证明 16 是 2 的幂时无空隙。
- 同一条流内，相邻窗口（如 w=5 与 w=6）地址差 `0x10`；相邻流（如 n=2 与 n=3 的同 w）地址差 `0x100`。
- `WINCNT/TSLO/TSHI` 在同一窗口内相差 8（`0x4250/0x4258/0x425C`），因为中间还隔着一个 `LAST`（`0x4254`）。

**预期结果**：

| 寄存器 | 字节地址（相对 IP 基址） |
|---|---|
| `PSI_MS_DAQ_WIN_WINCNT(2,5,0x100)` | `0x4250` |
| `PSI_MS_DAQ_WIN_TSLO(2,5,0x100)` | `0x4258` |
| `PSI_MS_DAQ_WIN_TSHI(2,5,0x100)` | `0x425C` |
| `strAddrOffs`（maxWindows=16） | `0x100`（256） |

> 若把这些偏移加上 IP 的 `baseAddr`（如 ZCU102 上的 `XPAR_PSI_MS_DAQ_BASEADDR`），就是 CPU 视角的绝对物理地址。

**待本地验证**：在 ZCU102 参考设计里（见 u5-l2），可打印 `PsiMsDaq_Init` 返回实例里实际算出的 `strAddrOffs`，与本处手算的 `0x100` 对照（需把结构体指针强转为 `PsiMsDaq_Inst_t*` 读取该字段——仅供调试）。

#### 4.4.5 小练习与答案

**练习 1**：若 `maxWindows=8` 而非 16，`strAddrOffs` 变成多少？流 2、窗口 5 的 `WINCNT` 地址变成多少？

> **答案**：`Log2Ceil(8)=Log2(8)=3`，`Pow(2,3)=8`，`strAddrOffs = 8×0x10 = 0x80`。于是 `WINCNT(2,5,0x80) = 0x4000 + 0x80×2 + 0x10×5 = 0x4000 + 0x100 + 0x50 = 0x4150`。可见 `maxWindows` 减半，流间步进也减半，整块窗口区缩小一半。

**练习 2**：`WINCNT` 的 `CNT` 字段是 `[30:0]`，为什么最高位 bit31 单独留给 `ISTRIG`，而不是让 `CNT` 用满 32 位？

> **答案**：采样数 31 位（最多约 21 亿样本）对单窗口已绰绰有余；而「该窗口是否含触发」是软件回读时必须立刻知道的关键状态（决定能否算 preTrig、能否读时间戳），把它与采样数挤在同一个寄存器、用最高的一个 bit 标记，可以让软件一次 32 位读就同时拿到「有多少数据」和「是否含触发」两个信息，省去多一次寄存器访问。

**练习 3**：`Log2Ceil` 名为「向上取整」却实现成 floor。如果有人误传 `maxWindows=12`（非 2 的幂），`strAddrOffs` 会算成多少？会引发什么后果？

> **答案**：`Log2Ceil(12)=Log2(12)=3`（`12→6→3→1`，3 次），`Pow(2,3)=8`，`strAddrOffs=8×0x10=0x80=128` 字节。但 12 个窗口实际需要 `12×0x10=192` 字节，流间步进 `128` 不够，相邻流的窗口区会重叠，读写串流。这正是 IP 要求 `maxWindows` 必须是 2 的幂的根因。

## 5. 综合实践

把四类地址空间串成一次完整的「配置 + 读状态」心智演练。假设你要配置流 2 录制 4 个窗口、每窗口 1024 字节、环形缓冲，然后读回它当前正在写的窗口号和最近写完的窗口号。

请按以下步骤**手写**（不调用驱动 API，直接用寄存器宏）：

1. **检查流 2 是否已禁用**：读 `STRENA`（地址 `0x020`），测 bit2 是否为 0。
2. **配置上下文**（CTX 区，步进 `0x20`，流 2 基址 `0x1000+0x40=0x1040`）：
   - 往 `SCFG(2)=0x1040` 写：`RINGBUF=1`（bit0）、`WINCNT=4-1=3`（写入 bit16–20）。即 `(1<<0) | (3<<16) = 0x30001`。
   - 往 `BUFSTART(2)=0x1044` 写 DDR 起始地址。
   - 往 `WINSIZE(2)=0x1048` 写 `1024`。
3. **配置录制**（REG 区，步进 `0x10`，流 2 基址 `0x200+0x20=0x220`）：
   - 往 `POSTTRIG(2)=0x224` 写触发后采样数。
   - 往 `MODE(2)=0x228` 写 `RECM` 字段（bit0–1）。
4. **使能**：`STRENA`（`0x020`）置 bit2；`GCFG`（`0x000`）置 `ENA|IRQENA`。
5. **读状态**：
   - 当前窗口：读 `SCFG(2)=0x1040` 的 `WINCUR` 字段（bit24–28）。
   - 最近写完窗口：读 `LASTWIN(2)=0x20C+0x20=0x22C`。
6. **读窗口内容**（设 `maxWindows=4`，先算 `strAddrOffs`）：`Log2Ceil(4)=2`，`Pow(2,2)=4`，`strAddrOffs=4×0x10=0x40`。窗口 1 的采样数在 `WINCNT(2,1,0x40)=0x4000+0x40×2+0x10×1=0x4090`。

完成后，你应当能用一张表把流 2 涉及的所有寄存器地址列全（REG 区 `0x220–0x22C`、CTX 区 `0x1040–0x1050`、窗口区 `0x4040–0x407C` 共 4 个窗口）。这就把本讲四块地址空间全部串了起来。

## 6. 本讲小结

- psi_ms_daq 的寄存器空间分为**四类**：通用（`0x000`）、逐流录制（`0x200`，步进 `0x10`）、逐流上下文（`0x1000`，步进 `0x20`）、窗口（`0x4000`，流间步进 `strAddrOffs`、窗口间步进 `0x10`）。
- `IRQVEC / IRQENA / STRENA` 三个通用寄存器共享「位 = 流」编码：bit0 对应流 0，bit15 对应流 15；中断用「写 1 清除」应答。
- `GCFG` 的 `ENA`（bit0）是 IP 总使能、`IRQENA`（bit8）是全局中断使能；`PsiMsDaq_Init` 末尾置 `ENA|IRQENA` 完成复位。
- `MODE` 寄存器把录制模式（`RECM[1:0]`）、软件置位的 `ARM`（bit8）、硬件置位的 `REC`（bit16）分散在不同字节。
- `SCFG` 寄存器把 `RINGBUF`、`OVERWRITE`、`WINCNT[20:16]`、`WINCUR[28:24]` 四个上下文字段挤在一个 32 位字里。
- `WINCNT` 是双含义寄存器：`[30:0]` 是窗口采样数，`[31]`（ISTRIG）标记是否含触发；`strAddrOffs = 2^⌊log₂(maxWindows)⌋ × 0x10`，依赖 `maxWindows` 是 2 的幂。

## 7. 下一步学习建议

本讲只看了「地址是什么」，还没看「驱动如何持有这些地址、如何组织软件状态」。建议下一步：

- **学 u3-l2（驱动数据模型与句柄抽象）**：看 `PsiMsDaq_Inst_t` 如何把 `baseAddr / maxStreams / maxWindows / strAddrOffs` 与三条访问函数指针打包成一个 IP 句柄，以及 `PsiMsDaq_StrInst_t` 如何缓存每条流的软件状态。
- **学 u3-l3（初始化与寄存器访问抽象）**：精读 `PsiMsDaq_Init` 的完整复位序列，以及默认访问函数 `PsiMsDaq_RegWrite_Standard` 如何用 `volatile` 指针直接读写物理地址。
- **学 u4-l3（窗口数据回读与去环绕回拷贝）**：本讲的 `WINCNT/LAST/TSLO/TSHI` 在那里被真正用来反推触发字节地址、做环形缓冲去环绕回拷贝，是窗口寄存器的实战应用。
