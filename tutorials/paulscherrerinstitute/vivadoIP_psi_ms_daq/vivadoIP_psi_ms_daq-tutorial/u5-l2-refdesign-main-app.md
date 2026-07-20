# 参考设计：端到端 C 应用主程序

## 1. 本讲目标

学完本讲，读者应该能够：

- 独立读懂 `refdesign/ZCU102` 参考应用 `main.c` 的完整结构，复述「初始化 → 使能 → 中断回读 → 释放窗口」这条端到端调用链。
- 说明在 Zynq 裸机环境下，为什么读取 IP-Core 直写 DDR 的数据前必须调用 `Xil_DCacheInvalidateRange`。
- 看懂用 `XScuGic` 把 psi_ms_daq 的一条电平敏感 IRQ 接到 CPU 的完整步骤（优先级、触发类型、连接、使能）。
- 理解 `Str1Irq` 中对回读数据做「连续性 + 100000 回绕」校验的逻辑，并知道触发样本在回读缓冲区中的确切下标。
- 具备把某条流从「窗口式 IRQ」改造成「流式 IRQ」的判断与改造能力。

## 2. 前置知识

本讲是 u5 单元（参考设计与端到端集成）的第 2 讲，需要以下前置：

- **驱动 API 与寄存器模型**（u3 全单元）：知道 `PsiMsDaq_Init` / `GetStrHandle` / `Str_Configure` / `SetEnable` 这些 API 的作用，以及通用 / 逐流 / 上下文 / 窗口四类寄存器。
- **窗口式 vs 流式 IRQ**（u4-l2）：知道 `SetIrqCallbackWin` 与 `SetIrqCallbackStr` 的互斥关系、`HandleIrq` 的派发逻辑、`MarkAsFree` 的握手作用。
- **窗口数据去环绕回拷贝**（u4-l3）：知道 `GetDataUnwrapped` 如何把环形缓冲展开成线性数据，以及 pre/post 触发样本的地址反推。
- **录制模式与窗口概念**（u4-l1）：知道 Continuous 模式与窗口、环形缓冲的含义。
- **Vivado 参考工程与时钟域**（u5-l1）：知道 `system_wrapper`、`xparameters.h` 里 `XPAR_*` 宏的来源。

几个本讲会用到、但初学者可能陌生的术语，先做通俗解释：

- **AXI Master 直写 DDR**：psi_ms_daq 的 IP-Core 通过 AXI Master 端口把采集到的样本直接写进 DDR，**完全绕过 CPU**。这就像有人直接往你的笔记本里塞新纸条，而你的眼睛（CPU）可能正盯着笔记本里那张旧纸条的复印件。
- **D-Cache（数据缓存）与缓存失效**：Zynq 的 ARM 核有一层数据缓存，CPU 读内存时可能命中缓存里的旧值，而不是 DDR 里的新值。`Xil_DCacheInvalidateRange(addr, len)` 把这段地址的缓存行标记为「无效」，强制下一次读取重新从 DDR 拉数据。
- **GIC（Generic Interrupt Controller）**：ARM 的中断控制器。PL（FPGA）发出的中断信号先到 GIC，GIC 再按优先级派发给 CPU。Xilinx 提供 `XScuGic` 这套库来配置它。
- **电平敏感（level-sensitive）中断**：只要中断信号线保持高电平，就被视为「中断持续有效」。这意味着中断处理函数里**必须清除中断源**（让信号线拉低），否则同一个中断会反复触发。psi_ms_daq 用「写 IRQVEC 寄存器（写 1 清除）」来拉低信号。
- **sporadic trigger（偶发触发）**：相对周期性触发而言，指由用户在运行时「按需」手动注入的一次性触发。本讲 `main.c` 的菜单就是用它来模拟外部事件。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
|---|---|---|
| [refdesign/ZCU102/Sdk/app/src/main.c](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/Sdk/app/src/main.c) | ZCU102 参考应用的唯一 C 源文件，全部逻辑都在这一个文件里 | `Init()`、`Str0Irq`/`Str1Irq`、`main()` |
| [drivers/psi_ms_daq_axi/src/psi_ms_daq.h](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h) | 驱动头文件 | `PsiMsDaq_WinInfo_t`、回调 typedef、`StrConfig_t`、`RetCode`、`HandleIrq` 声明 |
| [drivers/psi_ms_daq_axi/src/psi_ms_daq.c](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c) | 驱动实现 | `PsiMsDaq_Init`、`HandleIrq`、`Str_Configure`、`SetIrqCallbackWin/Str`、`GetDataUnwrapped`、`MarkAsFree` |

补充说明：

- `main.c` 里 `#include <axis_data_gen.h>` 用**尖括号**引入，说明 axis_data_gen 是**另一个 IP**（`vivadoIP_axis_data_gen`，见 u1-l3 依赖说明）的驱动，它的头文件由 BSP 放在 include 搜索路径里，**本仓库不含其源码**。本讲只依据 `main.c` 内的注释描述它的行为，不臆测其 API 细节。
- `XPAR_PSI_MS_DAQ_BASEADDR`、`XPAR_FABRIC_PSI_MS_DAQ_IRQ_INTR`、`XPAR_STR0_TESTGEN_GEN_0_BASEADDR` 等宏来自 BSP 自动生成的 `xparameters.h`（见 u5-l3）。
- 本地 `drivers/` 目录的 `.c/.h` 是上游 `psi_multi_stream_daq` 的拷贝（见 u1-l2），每次打包会被覆盖；本讲引用它们只为讲解 API 行为。

## 4. 核心概念与源码讲解

### 4.1 端到端调用链与测试数据来源

#### 4.1.1 概念说明

参考设计 `main.c` 把前面几讲学的「IP 封装 + 驱动 + 中断 + 窗口回读」串成一条能在真实 ZCU102 板子上跑起来的链路。整体可以概括成一个数据环：

1. **产生数据**：两路 `axis_data_gen` IP 各自往 psi_ms_daq 的 AXI-Stream 输入口灌测试数据（自增计数 + 周期性触发）。
2. **采集入窗**：psi_ms_daq 把数据按窗口写进 DDR，每个窗口写满就拉高一次中断。
3. **中断派发**：CPU 经 GIC 收到中断，进入 `PsiMsDaqIrqHandler` → `PsiMsDaq_HandleIrq`，驱动判断是哪条流、哪个窗口，回调用户的 `Str0Irq` / `Str1Irq`。
4. **回读校验**：用户回调里失效 D-Cache、用 `GetDataUnwrapped` 把环形数据拷成线性、做连续性校验。
5. **释放窗口**：调用 `MarkAsFree` 确认，让该窗口可以接收新数据。
6. **手动触发**：主循环菜单调用 `AxisDataGen_SendSporadicTriggers` 按需注入触发。

这条链路里，**psi_ms_daq 自身不产生数据**，它只是「搬运 + 记录」。数据来源完全由 axis_data_gen 决定。

#### 4.1.2 核心流程

```text
[axis_data_gen 0] --AXI-Stream--> [psi_ms_daq Str0] --AXI-Master--> DDR 0x40000000..
[axis_data_gen 1] --AXI-Stream--> [psi_ms_daq Str1] --AXI-Master--> DDR 0x50000000..
                                          |
                                     窗口写满 -> IRQ 线
                                          v
                          PL IRQ -> GIC -> PsiMsDaqIrqHandler(arg=daqHandle)
                                          |
                                          v
                              PsiMsDaq_HandleIrq  (读/写 IRQVEC 应答, 派发)
                                          |
                          +---------------+----------------+
                          v                                v
                     Str0Irq(winInfo)                Str1Irq(winInfo)
                  DCache失效 0x400..              DCache失效 0x500..
                  GetDataUnwrapped(5,5)           GetDataUnwrapped(1000,1000)
                  打印+触发标记=T                 连续性+100000回绕校验
                  MarkAsFree                       MarkAsFree
```

两路流的关键差异：

| 属性 | Stream 0 | Stream 1 |
|---|---|---|
| 数据位宽 | 16 bit | 32 bit |
| 窗口大小 winSize | 32 字节（16 样本） | 32000 字节（8000 样本） |
| 窗口数 winCnt | 3 | 6 |
| DDR 起始地址 | 0x40000000 | 0x50000000 |
| postTrigSamples | 5 | 1000 |
| 计数回绕点 | 10000 | 100000 |
| 周期触发间隔 | 每 1000 样本 | 每 25000 样本（偏移 500） |

#### 4.1.3 源码精读

`main.c` 顶部声明了全程使用的句柄与缓冲区：两个数据发生器实例、DAQ 的 IP/流句柄、GIC 实例，以及一个 80000 字节的共用回读缓冲 `buf`。

- 静态变量区：[main.c:22-30](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/Sdk/app/src/main.c#L22-L30) — 中文说明：定义 `str0Gen/str1Gen` 两个数据发生器、`daqHandle/daqStr0/daqStr1` 三个 DAQ 句柄、GIC 实例 `gicInst`、回读缓冲 `buf[20000*4]`。

数据发生器的行为由 `main.c` 注释点明：stream 0 计数在 10000 处回绕、每 1000 样本产生一个触发；stream 1 计数在 100000 处回绕、每 25000 样本（偏移 500）产生一个触发。

- stream 0 数据源注释：[main.c:136-137](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/Sdk/app/src/main.c#L136-L137) — 中文说明：注释「Wraparound at 10000, generate triggers at X*1000」。
- stream 1 数据源注释：[main.c:144-145](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/Sdk/app/src/main.c#L144-L145) — 中文说明：注释「Wraparound at 100000, generate triggers at X*25000+500」。

> 说明：`AxisDataGen_ConfigurePattern(...)` 的具体形参含义属于外部 IP（axis_data_gen），本仓库不含其头文件，故本讲只依据注释描述其行为，不展开参数表。

#### 4.1.4 代码实践

**实践目标**：在不开发板的情况下，凭源码画出两路流的数据流图并算出窗口容量。

**操作步骤**：

1. 打开 `main.c` 的 `Init()` 与两个回调，列出每条流的「位宽 / winSize / winCnt / bufStart / postTrig」。
2. 计算每条流单个窗口能放多少个样本（`winSize ÷ 字宽`）。

**需要观察的现象 / 预期结果**：

- Stream 0：32 字节 ÷ 2 字节 = **16 样本/窗口**。
- Stream 1：32000 字节 ÷ 4 字节 = **8000 样本/窗口**。
- Stream 1 单次回读请求 pre=1000、post=1000，共 2000 样本 = 8000 字节，**小于**一个窗口的 8000 样本容量，符合「请求量 ≤ 窗口容量」的隐含约束。

#### 4.1.5 小练习与答案

**练习 1**：参考设计为什么要用两路不同的位宽（16/32）和不同的窗口大小？

**答案**：为了演示 psi_ms_daq「每路流独立位宽、独立窗口大小、独立 DDR 区域」的能力。stream 1 的大窗口（8000 样本）专门用来验证连续性 + 回绕校验；stream 0 的小窗口用来演示逐样本打印与触发标记。

**练习 2**：`buf` 被声明为 `char buf[20000*4]`，为什么是 80000 字节？

**答案**：stream 1 单次最多回读 2000 个 32-bit 样本 = 8000 字节，80000 字节能容纳多次回读或更大请求，留足余量；它是所有流共用的临时回读缓冲。

---

### 4.2 Init() 初始化序列

#### 4.2.1 概念说明

`Init()` 是 `main.c` 里最长、最关键的函数，它把一个「刚上电、什么都没配」的 psi_ms_daq IP 变成「两条流都在采集、中断就绪」的工作状态。它严格遵循驱动头文件给出的推荐顺序（见 `psi_ms_daq.h` 的 Example Code 段）：先 `Init` → 取流句柄 → `Str_Configure` → 注册回调 → 使能 IRQ → 使能流。这个顺序不是随意的——**必须先配置（流必须禁用）再使能**，且回调必须在使能 IRQ 之前注册。

#### 4.2.2 核心流程

`Init()` 内部按顺序做四件事：

1. **启动两路测试数据发生器**（`Disable` → `ConfigurePattern` → `Enable`）。
2. **初始化 DAQ 驱动并取流句柄**：`PsiMsDaq_Init(base, 2, 16, NULL)` 声明本 IP 有 2 条流、每流最多 16 个窗口；再 `GetStrHandle` 取 stream 0/1 的句柄。
3. **逐流配置 + 注册窗口回调 + 使能**：对每条流依次 `Str_Configure` → `SetIrqCallbackWin` → `SetIrqEnable(true)` → `SetEnable(true)`。
4. **最后配置中断控制器**（XScuGic）：注释特意写「last, since psi_ms_daq is used in IRQ handler」——一旦使能 GIC，中断就可能立刻进来，此时驱动必须已经完全就绪。

```text
Init():
  AxisDataGen_Init/Disable/ConfigurePattern/Enable  (两路数据源)
        |
  PsiMsDaq_Init(base, 2, 16, NULL)  ->  GetStrHandle(0) / GetStrHandle(1)
        |
  对每条流:  Str_Configure -> SetIrqCallbackWin -> SetIrqEnable(true) -> SetEnable(true)
        |
  XScuGic 配置 + Xil_ExceptionEnable   (最后开中断)
```

#### 4.2.3 源码精读

**DAQ 驱动初始化与取句柄**：传 `NULL` 表示用默认（裸机 volatile 直访）访问函数；`2 = maxStreams`、`16 = maxWindows`。

- [main.c:149-155](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/Sdk/app/src/main.c#L149-L155) — 中文说明：`PsiMsDaq_Init` 创建驱动实例并取 stream 0/1 句柄，每步检查返回码。

对应的驱动实现里，`PsiMsDaq_Init` 会算出窗口区流间步进 `strAddrOffs = Pow(2,Log2Ceil(maxWindows))*0x10`（maxWindows=16 → 16×0x10 = 0x100），禁掉所有流/IRQ、清窗口计数，最后置 `GCFG = ENA | IRQENA`。

- [psi_ms_daq.c:132-181](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L132-L181) — 中文说明：驱动构造函数，禁能→清状态→置全局使能的三段式复位（详见 u3-l3）。

**Stream 0 的配置结构体**：用 C99 指定初始化器，把 `PsiMsDaq_StrConfig_t` 的 8 个字段一次性填好。

- [main.c:159-168](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/Sdk/app/src/main.c#L159-L168) — 中文说明：stream 0 配置（postTrig=5、Continuous、ringbuf=true、overwrite=false、winCnt=3、bufStart=0x40000000、winSize=32 字节、width=16 bit）。

```c
// 示例代码（节选自 main.c，省略非关键字段）
PsiMsDaq_StrConfig_t cfg0 = {
    .postTrigSamples = 5,
    .recMode = PsiMsDaqn_RecMode_Continuous,
    .winAsRingbuf = true,
    .winOverwrite = false,   // 窗口式 IRQ 的前提
    .winCnt = 3,
    .bufStartAddr = 0x40000000,
    .winSize = 32,           // 单位是字节，不是样本
    .streamWidthBits = 16
};
```

注意 `winOverwrite = false` 是使用窗口式 IRQ（`SetIrqCallbackWin`）的**前提**（见 4.4 与 u4-l1/l2）；`winSize` 单位是**字节**，不是样本。

**配置→回调→使能 IRQ→使能流的四连击**（stream 0）：每步都用 `if (Success != ret) printf(...)` 做返回码检查，但**不阻断**（出错只打印，继续往下跑）。

- [main.c:169-176](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/Sdk/app/src/main.c#L169-L176) — 中文说明：`Str_Configure` → `SetIrqCallbackWin(Str0Irq)` → `SetIrqEnable(true)` → `SetEnable(true)`。

驱动侧 `PsiMsDaq_Str_Configure` 会先做四道校验（位宽 8 的倍数、winCnt ≤ maxWindows、winSize 是样本倍数、流已禁用），再把字段写入 POSTTRIG/MODE/SCFG/BUFSTART/WINSIZE 等寄存器，并缓存 `widthBytes/windows/bufStart/postTrig/winSize` 供回读用。

- [psi_ms_daq.c:264-320](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L264-L320) — 中文说明：流配置实现，四道校验后写寄存器并更新软件缓存（详见 u3-l4）。

**Stream 1 配置**：与 stream 0 同构，只是参数不同（postTrig=1000、winCnt=6、bufStart=0x50000000、winSize=32000、width=32），回调换成 `Str1Irq`。

- [main.c:180-197](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/Sdk/app/src/main.c#L180-L197) — 中文说明：stream 1 配置与使能序列。

#### 4.2.4 代码实践

**实践目标**：验证「先配置后使能、配置时流必须禁用」这一约束。

**操作步骤**：

1. 在 `Init()` 里找到 `PsiMsDaq_Str_SetEnable(daqStr0, true)`（[main.c:175-176](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/Sdk/app/src/main.c#L175-L176)）。
2. 假想把它**移到** `PsiMsDaq_Str_Configure(daqStr0, &cfg0)`（[main.c:169](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/Sdk/app/src/main.c#L169)）**之前**。
3. 对照 `Str_Configure` 里的 `CheckStrDisabled` 调用（[psi_ms_daq.c:282](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L282)）与 `CheckStrDisabled` 实现（[psi_ms_daq.c:68-77](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L68-L77)）。

**需要观察的现象 / 预期结果**：`Configure` 会读 STRENA 发现 stream 0 的使能位为 1，返回 `PsiMsDaq_RetCode_StrNotDisabled`（-3），`main.c` 打印 `PsiMsDaq_Str_Configure(0) failed: -3`。**待本地验证**（需 ZCU102 板）。

#### 4.2.5 小练习与答案

**练习 1**：`PsiMsDaq_Init(..., 2, 16, NULL)` 里的 2 和 16 分别对应什么？填错会怎样？

**答案**：2 = `maxStreams`（必须和 Vivado 里 IP 例化的 `Streams_g` 一致），16 = `maxWindows`（必须和 `MaxWindows_g` 一致，且为 2 的幂）。填大可能越界访问 streams 数组或窗口地址错乱；填小会导致部分流/窗口用不了。`NULL` 表示用默认裸机访问函数。

**练习 2**：为什么 GIC 配置要放在 `Init()` 的最后？

**答案**：因为一旦 `XScuGic_Enable` + `Xil_ExceptionEnable` 执行，psi_ms_daq 的中断就可能立即被 CPU 接收并进入 `PsiMsDaqIrqHandler` → `HandleIrq`。此时两条流必须已配置完、回调已注册，否则会访问未初始化的状态。所以「中断最后开」是安全顺序。

---

### 4.3 电平敏感中断接线：XScuGic 与 PsiMsDaq_HandleIrq

#### 4.3.1 概念说明

psi_ms_daq 的 IRQ 是**电平敏感、高有效**（见 `psi_ms_daq.h` 文档「the IP core asserts its interrupt (level sensitive, high active)」）。这意味着中断线在被清除前会一直保持高电平。驱动的 `PsiMsDaq_HandleIrq` 负责在派发回调前/中用「写 IRQVEC = 当前 IRQVEC 值」（写 1 清除，W1C）来应答、拉低中断线。在 Zynq 裸机上，把这条 IRQ 线接到 CPU 的工作由 Xilinx 的 `XScuGic` 库完成。GIC 必须被配置成「电平敏感」才能与 IP 的电平中断匹配。

#### 4.3.2 核心流程

GIC 接线步骤（按 `Init()` 中的顺序）：

1. `XScuGic_LookupConfig` + `XScuGic_CfgInitialize`：找到并初始化 GIC 实例。
2. `XScuGic_Disable`：先关掉本中断，防止配置过程中误触发。
3. `Xil_ExceptionInit` + `Xil_ExceptionRegisterHandler`：把 ARM 异常向量入口指向 `XScuGic_InterruptHandler`，并把 GIC 实例作为参数传入。
4. `XScuGic_SetPriorityTriggerType(..., 0xA0, 0x1)`：优先级 0xA0（默认），**触发类型 0x1 = 电平敏感**（依据 `main.c` 行内注释）。
5. `XScuGic_Connect(..., PsiMsDaqIrqHandler, daqHandle)`：把中断号连到我们的顶层 ISR，并把 IP 句柄作为回调参数。
6. `XScuGic_Enable` + `Xil_ExceptionEnable`：开中断、开 ARM 异常。

中断到达后的派发链：

```text
IRQ 线拉高 -> GIC -> XScuGic_InterruptHandler -> PsiMsDaqIrqHandler(arg=daqHandle)
  -> PsiMsDaq_HandleIrq(ipHandle)
       1. 读 IRQVEC 得到 strWithIrq 位图
       2. 把 strWithIrq 原样写回 IRQVEC (W1C 应答, 拉低电平)
       3. 对每个置位的流:
            - 若注册了 irqFctStr: 直接调用流回调
            - 若注册了 irqFctWin: 循环推进 lastProcWin..lastWrittenWin,
              用 irqCalledWin 位图保证每窗口恰好一次, 清流位屏蔽伪中断
```

#### 4.3.3 源码精读

**顶层 ISR**：`XScuGic` 的回调签名是 `void(void* arg)`，所以 `PsiMsDaqIrqHandler` 把 `arg` 还原成 IP 句柄再交给驱动唯一的 IRQ 入口。

- [main.c:38-44](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/Sdk/app/src/main.c#L38-L44) — 中文说明：把 `arg` 强转回 `PsiMsDaq_IpHandle`，调用 `PsiMsDaq_HandleIrq`。

**GIC 配置全段**：注意 `0xA0` 是优先级、`0x1` 是触发类型（注释明确「level sensitive」）；`XScuGic_Connect` 把 `daqHandle` 作为回调参数传进去。

- [main.c:200-212](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/Sdk/app/src/main.c#L200-L212) — 中文说明：GIC 查找/初始化/禁用、异常注册、设优先级与电平触发、连接 ISR、使能。

```c
// 示例代码（节选自 main.c）
XScuGic_SetPriorityTriggerType(&gicInst, XPAR_FABRIC_PSI_MS_DAQ_IRQ_INTR, 0xA0, 0x1); // 0x1 = level sensitive
XScuGic_Connect(&gicInst, XPAR_FABRIC_PSI_MS_DAQ_IRQ_INTR, PsiMsDaqIrqHandler, (void*)daqHandle);
XScuGic_Enable(&gicInst, XPAR_FABRIC_PSI_MS_DAQ_IRQ_INTR);
Xil_ExceptionEnable();
```

触发类型行：[main.c:209](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/Sdk/app/src/main.c#L209) — 中文说明：第二个参数 `0x1` 按注释为电平敏感，与 IP 的 level-sensitive 高有效 IRQ 匹配。

**驱动 HandleIrq 的应答与派发**：第 1 步读 IRQVEC、第 2 步原样写回（W1C 清除），这是电平中断的命门——不清除则电平不落、中断反复进。

- [psi_ms_daq.c:197-258](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L197-L258) — 中文说明：`HandleIrq` 全函数。

关键几行：

- [psi_ms_daq.c:203-205](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L203-L205) — 中文说明：读 IRQVEC 得位图、原样写回应答（W1C）。
- [psi_ms_daq.c:225-253](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L225-L253) — 中文说明：窗口式派发的 do-while 循环，用 `irqCalledWin` 位图保证恰好一次并屏蔽伪中断（详见 u4-l2）。

> 关于触发类型值的精确含义：`main.c` 注释把 `0x1` 标注为「level sensitive IRQ」，本讲依据该注释与 `psi_ms_daq.h`「level sensitive, high active」的文档来描述。GIC 寄存器位的精确位语义属于 ARM GIC / `XScuGic` 库细节，超出本仓库范围，**待结合 Xilinx 官方 XScuGic 文档确认**。

#### 4.3.4 代码实践

**实践目标**：理解「不清除电平中断」会发生什么。

**操作步骤**：

1. 阅读驱动 `HandleIrq` 开头的应答两行（[psi_ms_daq.c:204-205](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L204-L205)）。
2. 假想把第 205 行（写回 IRQVEC）注释掉。
3. 推理：GIC 触发类型仍是电平敏感，IP 的 IRQ 线在窗口数据未被 `MarkAsFree` 前会一直为高。

**需要观察的现象 / 预期结果**：中断会「风暴式」反复进入，`Str0Irq`/`Str1Irq` 被疯狂调用、串口刷屏，CPU 占满。这正是电平敏感中断未应答的典型症状，从反面解释了为什么 `HandleIrq` 必须先写 IRQVEC。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `XScuGic_Connect` 要把 `daqHandle` 作为参数传给 `PsiMsDaqIrqHandler`？

**答案**：因为 `XScuGic_InterruptHandler` 派发到用户 ISR 时只传一个 `void* arg`，而 `HandleIrq` 需要 IP 句柄才能读寄存器。把 `daqHandle` 作为 arg 透传，ISR 里就能 `(PsiMsDaq_IpHandle)arg` 还原它，无需依赖全局变量。

**练习 2**：窗口式派发里 `irqCalledWin` 位图的作用是什么？

**答案**：保证「每个窗口回调恰好一次」并屏蔽伪中断。当一个窗口已回调但用户还没 `MarkAsFree` 时，`irqCalledWin` 对应位置 1；下一次相同 IRQ 再来时，do-while 遇到该位置位就 break，不再重复回调（见 u4-l2）。

---

### 4.4 Str0Irq / Str1Irq 中断回调

#### 4.4.1 概念说明

`Str0Irq` 和 `Str1Irq` 是用户注册的**窗口式 IRQ 回调**（签名 `PsiMsDaqn_WinIrq_f`），每写满一个窗口，驱动就构造一个栈上的 `PsiMsDaq_WinInfo_t`（含 `winNr`、`ipHandle`、`strHandle`）按值传进来。回调里要做四件事：① 失效 D-Cache；② 取时间戳；③ `GetDataUnwrapped` 回读数据；④ `MarkAsFree` 释放窗口。两路回调逻辑同构，但 stream 1 多了一步「连续性 + 100000 回绕」校验，用来验证整条采集链路（数据发生器 → IP → DDR → 回读）没有丢样本。

`PsiMsDaq_WinInfo_t` 是**按值传递的栈上结构体**，只在回调期间有效，回调返回后即失效——所以不能把它的地址存起来以后用。

- [psi_ms_daq.h:219-228](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L219-L228) — 中文说明：`WinInfo_t` 定义与「栈上、不可长期持有」的 note。

#### 4.4.2 核心流程

`Str0Irq` 流程：

1. `Xil_DCacheInvalidateRange(0x40000000, 32*16)`：失效 stream 0 的 DDR 区域（保守覆盖整个流缓冲）。
2. `PsiMsDaq_StrWin_GetTimestamp` 取 64 位时间戳。
3. `PsiMsDaq_StrWin_GetDataUnwrapped(winInfo, 5, 5, buf, sizeof(buf))`：读 5 个 pre + 5 个 post 样本。
4. 打印前 10 个样本，把下标 4 标记为触发（`=T`）。
5. `PsiMsDaq_StrWin_MarkAsFree(winInfo)` 释放窗口。

`Str1Irq` 流程：

1. `Xil_DCacheInvalidateRange(0x50000000, 32000*16)`：失效 stream 1 的 DDR 区域（远大于必需，属保守失效）。
2. 取时间戳（注意源码里调用了两次 `GetTimestamp`，见下方精读）。
3. `GetDataUnwrapped(winInfo, 1000, 1000, buf, sizeof(buf))`：读 1000 pre + 1000 post = 2000 个 32-bit 样本。
4. 打印触发样本 `data[PRE_TRIG_CHECK-1]` 与首样本 `data[0]`。
5. 连续性校验：从 `data[0]` 起，期望每个样本 = 上一个 + 1（到 100000 回绕），不一致则报 FAILED。
6. `MarkAsFree`。

**触发样本在回读缓冲里的下标**：两路回调都用 `preTrigSamples - 1` 作为触发下标（stream 0 的 4、stream 1 的 999）。这不是巧合，而是 `GetDataUnwrapped` 的地址反推决定的——4.4.3 给出推导。

#### 4.4.3 源码精读

**Str0Irq 全文**：注意 DCache 失效在最前、`MarkAsFree` 在最后。

- [main.c:47-80](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/Sdk/app/src/main.c#L47-L80) — 中文说明：stream 0 回调完整实现。

关键行——DCache 失效与回读：

- [main.c:53-64](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/Sdk/app/src/main.c#L53-L64) — 中文说明：先失效 D-Cache、取时间戳、再 `GetDataUnwrapped(5,5)` 回读。

```c
// 示例代码（节选自 Str0Irq）
Xil_DCacheInvalidateRange(0x40000000, 32*16);                       // 失效 D-Cache
PsiMsDaq_StrWin_GetDataUnwrapped(winInfo, 5, 5, buf, sizeof(buf));  // pre=5, post=5
uint16_t* data = (uint16_t*)buf;
for (int i = 0; i < 10; i++) {
    printf(" %d", data[i]);
    if (i == 4) printf("=T");   // 下标 4 = preTrigSamples-1 = 触发样本
}
```

**Str1Irq 的连续性校验**：

- [main.c:98-119](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/Sdk/app/src/main.c#L98-L119) — 中文说明：回读 2000 样本后，从首样本起逐个比对「+1 取模 100000」的连续性。

```c
// 示例代码（节选自 Str1Irq）
PsiMsDaq_StrWin_GetDataUnwrapped(winInfo, PRE_TRIG_CHECK, POST_TRIG_CHECK, buf, sizeof(buf));
uint32_t* data = (uint32_t*)buf;
printf("> Trigger Sample: %d\n", data[PRE_TRIG_CHECK-1]);   // 下标 999 = 触发
uint32_t exp = data[0];
for (int i = 0; i < PRE_TRIG_CHECK+POST_TRIG_CHECK; i++) {
    if (exp != data[i]) { printf("FAILED at %d\n", i); break; }
    exp = (exp+1) % 100000;    // 与数据发生器的 100000 回绕对齐
}
```

> 注意：[main.c:94-95](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/Sdk/app/src/main.c#L94-L95) 连续调用了两次 `PsiMsDaq_StrWin_GetTimestamp`，第二次的返回值未使用、第一次的返回码也被忽略。从功能上看第二次调用是多余的，疑似复制粘贴遗留；本讲按源码原样描述，不臆测其意图。

**为什么触发样本在下标 `preTrigSamples - 1`？** 推导自 `GetDataUnwrapped` 的地址计算。设请求 pre = P、post = Q（Q 含触发），样本字宽 w，则拷贝字节 `bytes = (P+Q)·w`。函数末字节地址 `lastByteAddr = trigByteAddr + Q·w + w − 1`，首字节地址 `firstByteLinear = lastByteAddr − bytes + 1`。触发字节相对缓冲首字节的偏移：

\[
\text{trigByteAddr} - \text{firstByteLinear}
= \text{trigByteAddr} - \text{lastByteAddr} + \text{bytes} - 1
\]

代入 `lastByteAddr`：

\[
= -(Q\!\cdot\! w + w - 1) + (P+Q)\!\cdot\! w - 1
= (P-1)\!\cdot\! w
\]

所以触发字节在缓冲偏移 \((P-1)\cdot w\) 处，即**触发样本下标 = P − 1 = preTrigSamples − 1**。这就是 stream 0 标 `i==4`、stream 1 读 `data[999]` 的根据。

- [psi_ms_daq.c:606-635](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L606-L635) — 中文说明：`GetDataUnwrapped` 的窗口地址反推与单次/两段拷贝（详见 u4-l3）。

**MarkAsFree 的双重作用**：清软件 `irqCalledWin` 位 + 向窗口 WINCNT 寄存器写 0，完成「投递—确认」握手，让该窗口可接收新数据。

- [psi_ms_daq.c:641-653](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L641-L653) — 中文说明：清位图位 + 写 WINCNT=0。

**为什么必须 `Xil_DCacheInvalidateRange`**：psi_ms_daq 通过 AXI Master 直接写 DDR，**绕过 CPU 缓存**。CPU 的 D-Cache 里可能还留着该地址的旧副本，若不失效就直接 `GetDataUnwrapped`（它本质是从 DDR 地址 memcpy），会读到陈旧数据。失效后，下一次读强制走 DDR，拿到 IP 刚写进去的新数据。失效范围要**覆盖本次回读可能命中的所有地址**（含环形回绕的尾部/头部）。

#### 4.4.4 代码实践

**实践目标**：手算 stream 1 一次回读的地址关系，验证连续性校验的合理性。

**操作步骤**：

1. stream 1：`bufStart=0x50000000`、`winSize=32000`、`widthBytes=4`、`postTrig(configured)=1000`。假设某次硬件写指针 `lastSplAddr = 0x50002710`（WIN_LAST 读出的值）。
2. 按 `GetDataUnwrapped` 公式算 `trigByteAddr`（注意若小于 `winStart` 要 `+winSize` 回绕）。
3. 请求 P=1000、Q=1000，算 `lastByteAddr`、`firstByteLinear`，判断走单次拷贝还是两段拼接。

**需要观察的现象 / 预期结果**：

- 本实践意在让读者熟悉地址反推；具体数值取决于 `lastSplAddr`，**待本地验证**（可在一个最小仿真或板子上 dump WINLAST 后代入）。
- 不论数值如何，触发样本都在 `data[999]`，连续性校验从 `data[0]` 起递增、在 100000 回绕。

#### 4.4.5 小练习与答案

**练习 1**：如果忘了在 `Str0Irq` 里调 `Xil_DCacheInvalidateRange`，串口可能看到什么？

**答案**：回读可能拿到全 0 或上一次的旧样本（缓存陈旧副本），触发标记 `=T` 错位、数值不递增。具体表现取决于缓存是否曾加载过该地址。

**练习 2**：`Str1Irq` 的连续性校验里，为什么回绕用 `% 100000` 而不是别的数？

**答案**：因为 stream 1 的数据发生器被配成「在 100000 处回绕自增计数」（[main.c:144-145](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/Sdk/app/src/main.c#L144-L145)）。校验逻辑必须和发生器的回绕点一致，否则跨过 100000 的样本会被误判为 FAILED。这个数是应用层与数据源之间的约定。

**练习 3**：`PsiMsDaq_WinInfo_t` 能不能存到全局变量里，等主循环再处理？

**答案**：不能。它是驱动在栈上构造、按值传入的瞬时结构体（[psi_ms_daq.h:219-228](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L219-L228)），回调返回后栈帧失效。要延后处理必须自己拷贝 `winNr` 并在主循环里用流级 API（如 `PsiMsDaq_Str_GetLastWrittenWin`）重新定位窗口。

---

### 4.5 main() 菜单循环与 sporadic 触发

#### 4.5.1 概念说明

`main()` 的结构非常简单：调一次 `Init()`，然后进入一个文本菜单循环。两条流都用 Continuous 录制模式 + 周期触发（由 axis_data_gen 自动产生），所以即使菜单什么都不做，中断也会源源不断地进来、回调不断打印。菜单的作用是**额外**注入「sporadic（偶发）触发」——用户按键时，让数据发生器在指定流上立刻发出若干个触发，模拟外部事件。这相当于一个手动「按下示波器 trigger」的演示。

#### 4.5.2 核心流程

主循环每轮：

1. 打印菜单（`0`=流0触发、`1`=流1触发、`b`=两路、`q`=退出）。
2. 读一个字符（跳过 `\n`/`\r`），`switch` 决定选哪条流；`q` 退出、非法字符提示重来。
3. 再读一个字符作为「触发个数（1-9）」，非法重来。
4. 对选中的流调 `AxisDataGen_SendSporadicTriggers(hndl, triggers)` 注入触发。
5. `usleep(100000)` 留时间给采集与中断打印。
6. `q` 退出时 `Xil_ExceptionDisable()` 关异常后返回。

```text
main():
  Init()
  while running:
     读字符 -> 选择流 (0/1/b/q)
     读字符 -> 触发个数 (1..9)
     对选中流: AxisDataGen_SendSporadicTriggers(hndl, triggers)
     usleep(100000)
  Xil_ExceptionDisable(); return 0
```

#### 4.5.3 源码精读

**主循环菜单与触发**：

- [main.c:232-283](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/Sdk/app/src/main.c#L232-L283) — 中文说明：菜单打印、读字符、`switch` 选择流、读触发个数、注入 sporadic 触发。

关键段——读取选择与注入触发：

- [main.c:276-281](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/Sdk/app/src/main.c#L276-L281) — 中文说明：对选中的流发送 N 个 sporadic 触发，然后 `usleep(100000)`。

```c
// 示例代码（节选自 main()）
if (str0Sel) AxisDataGen_SendSporadicTriggers(str0Hndl, triggers);
if (str1Sel) AxisDataGen_SendSporadicTriggers(str1Hndl, triggers);
usleep(100000);
```

退出清理：

- [main.c:286-289](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/Sdk/app/src/main.c#L286-L289) — 中文说明：`q` 退出时 `Xil_ExceptionDisable()` 关异常、`return 0`。

> 说明：sporadic 触发之所以能叠加在 Continuous 模式上，是因为 Continuous 模式本身就「始终接受触发」（见 u4-l1）；sporadic 只是让用户决定「何时、发几个」。在 TriggerMask/SingleShot 模式下还需要先 `PsiMsDaq_Str_Arm`，但本参考设计用 Continuous，故无需 Arm。

#### 4.5.4 代码实践

**实践目标**：通过菜单交互理解「周期触发」与「sporadic 触发」并存。

**操作步骤**：

1. 烧录参考设计后，在串口终端观察：不按任何键，是否已有 `Str0Irq`/`Str1Irq` 在打印？（因为 axis_data_gen 周期触发）
2. 按 `b` 再按 `3`，观察是否在原有周期打印之外，又「扎堆」多出若干次窗口回调。
3. 按 `q` 退出，观察是否打印 `Quitted!` 且不再有中断打印。

**需要观察的现象 / 预期结果**：

- 静止时：两路回调按各自周期（stream 0 每 1000 样本、stream 1 每 25000 样本）周期性触发、打印。
- 按 `b`+`3`：两路各注入 3 个 sporadic 触发，短时间内多出若干窗口写满 → 多次回调打印。
- 退出后中断停止。**待本地验证**（需 ZCU102 板与串口）。

#### 4.5.5 小练习与答案

**练习 1**：菜单里 `do { c = getchar(); } while ((c == '\n') || (c == '\r'));` 的作用？

**答案**：`getchar` 会连回车符一起读入，这里跳过换行/回车，只取有效字符，避免把 `\n` 当成一次菜单选择。

**练习 2**：把某条流改成 SingleShot 模式后，菜单的 sporadic 触发还按一下就立即生效吗？

**答案**：不一定。SingleShot 模式下需要先 `PsiMsDaq_Str_Arm` 才会接受触发（u4-l1）。本参考设计用 Continuous 才能做到「按键即触发」。

---

## 5. 综合实践

**任务**：把 stream 1 从「窗口式 IRQ + overwrite=false」改造成「流式 IRQ + overwrite=true」，并相应改写回调。

**背景**：依据 `psi_ms_daq.h` 的说明，窗口式 IRQ（`SetIrqCallbackWin`）要求 `winOverwrite=false` 且每个窗口都要被 `MarkAsFree`；一旦启用 overwrite（窗口未释放也可被覆盖写），窗口式 IRQ「每窗口恰好一次」的前提就被破坏，必须改用流式 IRQ（`SetIrqCallbackStr`），由用户自己负责窗口级处理。这也是 u4-l1 与 u4-l2 反复强调的隐含契约。

**改造清单（请逐条写出你的修改方案）**：

1. **配置结构体**：把 `cfg1` 的 `.winOverwrite` 改为 `true`。

   - 改动点：[main.c:184](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/Sdk/app/src/main.c#L184)（当前为 `.winOverwrite = false,`）。

2. **回调注册函数**：把 `PsiMsDaq_Str_SetIrqCallbackWin(daqStr1, Str1Irq, NULL)` 换成 `PsiMsDaq_Str_SetIrqCallbackStr(daqStr1, Str1IrqStream, NULL)`。

   - 改动点：[main.c:192](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/Sdk/app/src/main.c#L192)。
   - 注意两者签名不同：
     - 窗口式回调：`void f(PsiMsDaq_WinInfo_t winInfo, void* arg)`
     - 流式回调：`void f(PsiMsDaq_StrHandle strHandle, void* arg)`（见 [psi_ms_daq.h:241-250](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L241-L250)）

3. **改写回调**：新回调 `Str1IrqStream(PsiMsDaq_StrHandle strHndl, void* arg)` 不再自动收到 `winInfo`，需要自己做窗口遍历：

   - 用 `PsiMsDaq_Str_GetLastWrittenWin(strHndl, &lastWin)` 找到最新写完的窗口（声明见 [psi_ms_daq.h:610-618](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L610-L618)）。
   - 手动构造 `PsiMsDaq_WinInfo_t`（填 `winNr`、`ipHandle`、`strHandle`）后调用 `PsiMsDaq_StrWin_GetDataUnwrapped`。
   - 自己维护「已处理到哪个窗口」的游标，避免漏处理或重复处理——驱动不再替你保证恰好一次。
   - `MarkAsFree` 仍可调用以清窗口计数，但在 overwrite 模式下其「防覆盖」语义已失效，主要用来清 WINCNT。

4. **互斥校验**：驱动里 `SetIrqCallbackWin` 与 `SetIrqCallbackStr` 互斥（若对方已注册会返回 `IrqSchemesWinAndStrAreExclusive = -11`）。

   - 依据：[psi_ms_daq.c:336-368](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L336-L368)。
   - 本改造是从 Win 切到 Str，注册 Str 之前 Win 回调为 NULL，不会触发互斥；但绝不能两条都注册。

**预期结果（设计层面，待本地验证）**：改造后 stream 1 在 overwrite=true 下也能正常采集，中断回调由你手动管理窗口游标；由于允许覆盖，当回调处理慢于采集时，旧窗口数据会被新数据覆盖（这正是 overwrite 的用途），连续性校验可能出现 FAILED——这是预期行为而非 bug。

## 6. 本讲小结

- `main.c` 把「数据发生器 → psi_ms_daq → DDR → 中断 → 回读 → 释放」串成一条可在 ZCU102 上运行的完整链路，全部代码集中在一个文件里。
- `Init()` 严格按「Init → GetStrHandle → Str_Configure → SetIrqCallbackWin → SetIrqEnable → SetEnable → 最后配 GIC」的顺序，体现「先配置（流须禁用）后使能、中断最后开」的安全约束。
- psi_ms_daq 的 IRQ 是电平敏感高有效，`HandleIrq` 必须用「写 IRQVEC = 读出值」（W1C）应答以拉低电平，否则中断风暴；GIC 端用 `XScuGic_SetPriorityTriggerType(..., 0x1)` 配成电平敏感与之匹配。
- 回调读 DDR 前必须 `Xil_DCacheInvalidateRange`，因为 IP 经 AXI Master 直写 DDR、绕过 CPU 缓存，否则读到陈旧副本。
- 触发样本恒落在回读缓冲下标 `preTrigSamples - 1`（stream 0 的 4、stream 1 的 999），可由 `GetDataUnwrapped` 的地址反推严格证明。
- `Str1Irq` 的连续性 + 100000 回绕校验是与 axis_data_gen 发生器约定的端到端正确性检查；窗口式 IRQ 要求 overwrite=false，启用 overwrite 必须切到流式 IRQ 并自行管理窗口游标。

## 7. 下一步学习建议

- 阅读 u5-l1（参考设计 Vivado 工程与时钟域），理解 `system_wrapper`、`xparameters.h` 里 `XPAR_PSI_MS_DAQ_BASEADDR` / `XPAR_FABRIC_PSI_MS_DAQ_IRQ_INTR` 是怎么由 BSP 生成的。
- 阅读 u5-l3（IP-XACT 与驱动 BSP 集成），看清 `main.c` 能 `#include "psi_ms_daq.h"` 并链接进 `libxil.a` 的完整工具链。
- 若想深入中断派发细节，重读 u4-l2 的 `HandleIrq` 源码与 u4-l3 的 `GetDataUnwrapped` 地址推导。
- 进阶练习：把参考设计改成 TriggerMask 或 SingleShot 模式，在菜单里加一个「Arm」选项，体会 Arm 位与触发的关系。
