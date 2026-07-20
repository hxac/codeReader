# 录制模式与窗口/环形缓冲概念

## 1. 本讲目标

前面三讲（u3-l1～u3-l4）我们已经把「一条 AXI-Stream 流如何从 C 结构体变成 IP-Core 寄存器值」讲透了。但在 `PsiMsDaq_StrConfig_t` 里，有几个字段我们只是「写进去」却没解释它们到底**控制了什么硬件行为**——`recMode`、`winCnt`、`winSize`、`winAsRingbuf`、`winOverwrite`、以及那个在 main.c 里看似多余的 `PsiMsDaq_Str_Arm()`。

本讲要回答的核心问题是：

1. 四种录制模式（Continuous / TriggerMask / SingleShot / Manual）到底有什么区别？`Arm` 位在每种模式下什么时候起作用？
2. 什么是「窗口（Window）」？一条流为什么要有多个窗口？`winCnt` 和 `winSize` 共同决定了内存里什么样的数据布局？
3. 线性缓冲（linear）和环形缓冲（ringbuf）在 DDR 里长得有什么不一样？为什么环形缓冲会让「读数据」变得复杂（这正是下一讲 u4-l3 要解决的）？
4. 「覆盖（overwrite）」策略与「触发前后采样（pre/postTrig）」是什么？它们和窗口中断方案（Window based IRQ vs Stream based IRQ）为什么存在强绑定关系？

学完本讲，你应当能够：为任意一个真实采集场景**选对录制模式**、**算对一个流占用的 DDR 布局**、**判断该用窗口式还是流式中断**，并能把这些选择和驱动里具体的寄存器位（`RECM`、`ARM`、`REC`、`RINGBUF`、`OVERWRITE`、`ISTRIG`）一一对应起来。

> 本讲是「概念图」为主、源码为锚。真正的「读数据 + 去环绕」实现细节在 u4-l3，真正的「中断派发」在 u4-l2，本讲只搭好这两讲所依赖的概念地基。

## 2. 前置知识

在进入本讲前，请确认你理解下面几个来自前序讲义的概念（不熟悉的话建议先回看对应讲义）：

- **本仓库是 Vivado IP 封装层**，真正的采集逻辑硬件在上游 `psi_multi_stream_daq`，驱动只是「写寄存器去配置硬件」（u1-l1）。
- **寄存器地址模型**：寄存器空间分为通用（`0x000`）、逐流录制（`0x200`，步进 `0x10`）、逐流上下文 CTX（`0x1000`，步进 `0x20`）、窗口（`0x4000`，窗口间步进 `0x10`、流间步进 `strAddrOffs`）四块（u3-l1）。
- **流配置流程**：`PsiMsDaq_Str_Configure` 把结构体字段写进上述寄存器，并在软件侧缓存几何参数（u3-l4）。
- **句柄与状态**：`PsiMsDaq_Inst_t`（IP 实例）与 `PsiMsDaq_StrInst_t`（流实例）的私有结构（u3-l2）。

本讲还会用到几个术语，先统一解释：

| 术语 | 含义 |
|------|------|
| **触发（Trigger）** | 一个标记「这一拍数据很重要」的事件。硬件靠它决定一段录制在哪里「定中心」。 |
| **录制（Recording）** | 硬件把 AXI-Stream 上的采样持续搬进 DDR 的动作，对应 `REC` 位 = 1。 |
| **武装（Arm）** | 软件告诉硬件「我准备好了，可以开始按规则捕捉触发」的动作，对应 `ARM` 位 = 1。 |
| **窗口（Window）** | 一条流里独立的一段录制缓冲区，可有多份，由 `winCnt` 决定数量。 |
| **样本（Sample）** | AXI-Stream 上一个完整数据单元，宽度由 `streamWidthBits` 决定（必须是 8 的倍数）。 |

> ⚠️ 重要边界：本讲描述的「录制模式时序行为」是驱动头文件注释 + 寄存器位语义给出的**高层约定**。**真正的触发检测状态机实现在上游 IP-Core 硬件里**（即 `psi_multi_stream_daq` 的 VHDL，本仓库不含其源码），驱动只负责把 `recMode` 写进 `RECM` 字段。所以下文凡涉及「硬件会在某条件下做什么」的描述，依据都是头文件枚举注释，详细 FSM 请参考上游 `psi_multi_stream_daq.pdf` 文档。

## 3. 本讲源码地图

本讲只涉及驱动层的两个文件，外加参考设计 main.c 作为真实用例：

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| [drivers/psi_ms_daq_axi/src/psi_ms_daq.h](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h) | 驱动头文件：类型、枚举、寄存器宏、API 声明 | `PsiMsDaq_RecMode_t` 枚举、`PsiMsDaq_StrConfig_t` 结构体、`MODE_BIT_ARM/REC`、`CTX_SCFG_BIT_RINGBUF/OVERWRITE`、IRQ 方案说明 |
| [drivers/psi_ms_daq_axi/src/psi_ms_daq.c](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c) | 驱动实现 | `PsiMsDaq_Str_Configure`（模式/缓冲字段如何落寄存器）、`PsiMsDaq_Str_Arm`、`PsiMsDaq_Str_IsRecording` |
| [refdesign/ZCU102/Sdk/app/src/main.c](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/Sdk/app/src/main.c) | ZCU102 参考应用 | `cfg0`/`cfg1` 两份真实流配置、菜单触发循环 |

## 4. 核心概念与源码讲解

### 4.1 录制模式四态：RecMode_t 与 ARM/REC 位

#### 4.1.1 概念说明

「录制模式」回答的是这样一个问题：**硬件要怎样配合「触发」这件事来决定把哪些数据写进 DDR？**

回忆一下采集系统的典型诉求：

- 有的场景（如电源监控）只想要「源源不断地把最新数据留下」，触发有没有都无所谓——这是**持续录制**。
- 有的场景（如粒子探测器）只在「我按下开始按钮之后」到来的触发才算数，之前的不算——这是**触发屏蔽**。
- 有的场景（如捕获一次性上电瞬态）只要**一个**触发，抓到就停——这是**单次捕获**。
- 有的场景（如手动调试）想自己像按开关一样控制录制的起停——这是**手动控制**。

这四种诉求被抽象成枚举 `PsiMsDaq_RecMode_t` 的四个值。每个值不仅决定「要不要触发」，还决定**软件的 `Arm` 位在什么时机有意义**。

这里要严格区分两个位（它们都在逐流录制区的 `MODE` 寄存器里，u3-l1 已介绍过其地址 `0x208+0x10*n`）：

- **`ARM`（bit 8）**：**软件写**的位。CPU 通过 `PsiMsDaq_Str_Arm()` 把它置 1，表示「武装硬件」。它是软件对硬件的「放行」指令。
- **`REC`（bit 16）**：**硬件置**的位（只读）。硬件真的在往 DDR 写数据时它为 1，是硬件对软件的「我正在录」回报。`PsiMsDaq_Str_IsRecording()` 读的就是它。

理解 `ARM` 是因、`REC` 是果，是理解四种模式差异的钥匙。

#### 4.1.2 核心流程

四种模式与 `ARM`/`REC` 的关系（依据头文件枚举注释归纳）：

| 模式（值） | `ARM` 的作用 | 触发检测 | 典型停止条件 |
|-----------|------------|---------|------------|
| **Continuous** (0) | 不需要 Arm | 持续检测 | 不会自动停，软件禁用流（`STRENA`）才停 |
| **TriggerMask** (1) | Arm 之前：只录预触发数据、**屏蔽**触发；Arm 之后：开始接受触发 | Arm 之后才生效 | 不自动停，可反复触发 |
| **SingleShot** (2) | Arm 之前：不录；Arm 之后：录预触发并等待；触发后录完 postTrig 即停 | Arm 之后生效，**仅一次** | 抓到一次触发、写完 postTrig 后硬件停 `REC` |
| **Manual** (3) | `ARM` 直接当录制开关：置 1 即录、清 0 即停 | 与触发无关 | 软件清 `ARM` 即停 |

四种模式的「触发检测是否需要 Arm 放行」可以浓缩成下面的判定：

\[
\text{触发被接受} \iff (\text{模式} = \text{Continuous}) \lor \big((\text{模式} \in \{\text{TriggerMask},\text{SingleShot}\}) \land \text{ARM}=1\big)
\]

而 Manual 模式根本不走「触发→窗口」这条路径，`ARM` 直接控制 `REC`。

> 说明：上表「典型停止条件」「Arm 之前/之后」的行为细节来自驱动头文件枚举注释的概括。硬件内部完整的触发 FSM（如预触发环形回绕、postTrig 计数清零时机）由上游 IP-Core 实现，本仓库不含其 RTL，精确时序以 `psi_multi_stream_daq.pdf` 为准。

#### 4.1.3 源码精读

先看枚举定义本身——四种模式的名字、数值和注释就是契约：

[drivers/psi_ms_daq_axi/src/psi_ms_daq.h:252-260](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L252-L260) — `PsiMsDaq_RecMode_t` 枚举，注释逐条说明了 Arm 的作用时机：

```c
typedef enum {
	PsiMsDaqn_RecMode_Continuous	= 0,	// 持续录制
	PsiMsDaqn_RecMode_TriggerMask	= 1,	// 持续录预触发，但只在 Arm() 后才检测触发
	PsiMsDaqn_RecMode_SingleShot	= 2,	// 只在 Arm() 后录预触发，抓到一个触发即停
	PsiMsDaqn_RecMode_Manual		= 3 	// 通过置/清 arm 位手动控制录制
} PsiMsDaq_RecMode_t;
```

这四个值最终被写进 `MODE` 寄存器的 `RECM[1:0]` 字段。相关宏定义：

[drivers/psi_ms_daq_axi/src/psi_ms_daq.h:157-161](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L157-L161) — `MODE` 寄存器字段定义，`RECM` 占 bit0~1，`ARM` 是 bit8，`REC` 是 bit16：

```c
#define PSI_MS_DAQ_REG_MODE(n)				(0x208+0x10*(n))
#define PSI_MS_DAQ_REG_MODE_LSB_RECM		0
#define PSI_MS_DAQ_REG_MODE_MSB_RECM		1
#define PSI_MS_DAQ_REG_MODE_BIT_ARM			(1 << 8)
#define PSI_MS_DAQ_REG_MODE_BIT_REC			(1 << 16)
```

驱动把 `recMode` 写入 `RECM` 字段发生在 `PsiMsDaq_Str_Configure` 中（u3-l4 已详细讲过此函数，这里只看模式相关的那几行）：

[drivers/psi_ms_daq_axi/src/psi_ms_daq.c:287-291](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L287-L291) — 用 RMW 把 `config_p->recMode` 写入 `MODE` 寄存器的 `RECM` 字段：

```c
SAFE_CALL(PsiMsDaq_RegSetField(	ipHandle,
                                PSI_MS_DAQ_REG_MODE(strNr),
                                PSI_MS_DAQ_REG_MODE_LSB_RECM,
                                PSI_MS_DAQ_REG_MODE_MSB_RECM,
                                config_p->recMode));
```

注意驱动**只写 `RECM`，不碰 `ARM`**。`ARM` 是运行时控制的，由独立的 `PsiMsDaq_Str_Arm()` 负责：

[drivers/psi_ms_daq_axi/src/psi_ms_daq.c:384-394](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L384-L394) — `PsiMsDaq_Str_Arm()` 仅把 `ARM` 位置 1，**没有提供「清 Arm」的对称 API**（Manual 模式下要停录制时，需通过调试函数 `PsiMsDaq_RegSetBit` 清，见 u4-l4）：

```c
PsiMsDaq_RetCode_t PsiMsDaq_Str_Arm(PsiMsDaq_StrHandle strHndl)
{
    ...
    SAFE_CALL(PsiMsDaq_RegSetBit(ipHandle, PSI_MS_DAQ_REG_MODE(strNr),
                                 PSI_MS_DAQ_REG_MODE_BIT_ARM, true));
    return PsiMsDaq_RetCode_Success;
}
```

而读 `REC` 位判断「现在到底在不在录」则由 `PsiMsDaq_Str_IsRecording()` 完成：

[drivers/psi_ms_daq_axi/src/psi_ms_daq.c:675-687](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L675-L687) — 读 `MODE` 寄存器的 `REC` 位，这是软件感知硬件录制状态的唯一入口：

```c
SAFE_CALL(PsiMsDaq_RegGetBit(	inst_p->ipHandle,
                                PSI_MS_DAQ_REG_MODE(inst_p->nr),
                                PSI_MS_DAQ_REG_MODE_BIT_REC,
                                isRecording_p));
```

一条贯穿心智模型：**配置时写 `RECM`（模式）→ 运行时写 `ARM`（放行）→ 硬件回写 `REC`（在录）**。

#### 4.1.4 代码实践

**实践目标**：通过阅读 main.c，理解 Continuous 模式在参考设计里是怎么用的，并推断如果改成 TriggerMask 模式需要补什么调用。

**操作步骤**：

1. 打开 [refdesign/ZCU102/Sdk/app/src/main.c:159-168](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/Sdk/app/src/main.c#L159-L168)，确认 `cfg0.recMode = PsiMsDaqn_RecMode_Continuous`。
2. 在同文件搜索 `PsiMsDaq_Str_Arm`，你会发现在参考设计中**根本没有被调用**——这验证了 Continuous 模式确实不需要 Arm。
3. 想象把 `cfg0.recMode` 改成 `PsiMsDaqn_RecMode_TriggerMask`，问自己：现在 `AxisDataGen_SendSporadicTriggers()` 发出的触发（main.c 第 277 行）在 Arm 之前会不会产生窗口？依据 4.1.2 的表格回答。

**需要观察的现象**：

- Continuous 模式下，只要流被 `SetEnable(true)`（main.c:175），数据就会持续进入窗口、持续触发 IRQ 回调 `Str0Irq`，与菜单里是否「Generate Triggers」无关（菜单触发的只是测试数据里的额外触发标记）。
- 改成 TriggerMask 后，若不调用 `PsiMsDaq_Str_Arm(daqStr0)`，则即使有触发也不会产生含触发的新窗口（`ISTRIG` 不会置位）。

**预期结果**：在阅读层面你应当得出结论——TriggerMask/SingleShot/Manual 三种模式都必须在 `SetEnable` 之后、期待触发之前，补一次 `PsiMsDaq_Str_Arm()` 调用；Continuous 则不需要。

**待本地验证**：上述「Arm 之前不产生触发窗口」的行为依赖上游硬件 FSM，若手头有 ZCU102 板可烧录参考设计、改模式后实际运行菜单触发来确认。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `PsiMsDaq_Str_Arm()` 只提供「置位」而不提供「清位」的 API？这会对 Manual 模式的使用者带来什么不便？

> **参考答案**：因为 Continuous/TriggerMask/SingleShot 三种模式都不需要在运行中清 `ARM`（SingleShot 抓到一个触发后是硬件自动停 `REC`，不靠清 `ARM`）。只有 Manual 模式需要清 `ARM` 来停录制，而 Manual 是少数高级用例，驱动作者把它留给用户用调试函数 `PsiMsDaq_RegSetBit(ip, MODE(n), ARM, false)` 自行清（见 u4-l4）。代价是 Manual 用户要多写一行底层调用，且这条路径被标注为「debugging purposes」。

**练习 2**：某用户在 SingleShot 模式下调用了一次 `Arm()`，硬件抓到一个触发并写完 postTrig 后停了。现在他想再抓一次，直接再调一次 `Arm()` 是否一定有效？

> **参考答案**：不一定。`ARM` 位是电平敏感的软件写位，若硬件在抓到触发后没有自动清零 `ARM`，则「再写一次 1」并不会产生上升沿，硬件可能不会重新进入「等待触发」状态。稳妥做法是先清 `ARM`（用 `RegSetBit`）再置 `ARM`，制造一个干净的上升沿；具体硬件是否自动清 `ARM` 需查上游文档。本题旨在强化「`ARM` 是写位、其时序边沿语义由硬件 FSM 决定」的认知。

---

### 4.2 窗口作为录制单元：winCnt 与窗口布局

#### 4.2.1 概念说明

光有「模式」还不够。实际采集里，一次触发产生的数据往往要分成好几份独立保存——比如 8 通道示波器想保留最近 4 次触发波形做对比、或环形采集想让 CPU 处理第 N 段时硬件同时往第 N+1 段写。本 IP 把「一份独立的录制缓冲」叫做一个**窗口（Window）**。

一条流可以拥有**多个窗口**，数量由 `PsiMsDaq_StrConfig_t::winCnt` 决定。每个窗口在 DDR 里占 `winSize` 字节的连续空间，所有窗口首尾相接排列。这就是 u3-l1 里「窗口寄存器区」要按「流号 n、窗口号 w」二维寻址的原因——硬件为 `(流 n, 窗口 w)` 维护一份独立的元数据（采样数 `WINCNT`、末样本地址 `LAST`、时间戳 `TSLO/TSHI`）。

`winCnt` 不是「越多越好」：

- 窗口越多，单流占用 DDR 越大（总占用 = `winCnt × winSize`）。
- 窗口越多，窗口寄存器区越大，导致流间步进 `strAddrOffs` 越大（见 4.2.2 的公式），地址空间消耗也越大。
- 上限受 IP 泛型 `MaxWindows_g`（封装层 u2-l1）约束，驱动用 `maxWindows` 校验。

#### 4.2.2 核心流程

一条流 `n` 的 DDR 缓冲布局（线性视角，4.3 再讨论环形）：

```
bufStartAddr ───────────────────────►
┌──────────┬──────────┬──────────┬──────────┐
│ Window 0 │ Window 1 │ Window 2 │   ...    │  共 winCnt 个窗口
│ winSize  │ winSize  │ winSize  │          │  每个winSize字节
└──────────┴──────────┴──────────┴──────────┘
```

第 `w` 号窗口的起始字节地址为：

\[
\text{winStart}(n,w) = \text{bufStart}(n) + \text{winSize} \cdot w
\]

而窗口寄存器区里，第 `(n, w)` 号窗口元数据的字节偏移（相对 IP 基址）为：

\[
\text{WIN}(n,w) = 0\text{x}4000 + \text{strAddrOffs} \cdot n + 0\text{x}10 \cdot w
\]

其中流间步进 `strAddrOffs` 在 `PsiMsDaq_Init` 里算出（u3-l3 已讲）：

\[
\text{strAddrOffs} = 2^{\lfloor \log_2(\text{maxWindows}) \rfloor} \times 0\text{x}10
\]

这个公式要求 `maxWindows` 必须是 2 的幂（否则相邻流的窗口寄存器区会重叠，u3-l1 已强调）。例如 `maxWindows=16` 时 `strAddrOffs = 16 × 0x10 = 0x100`。

「窗口号」在驱动里有两层语义要区分清楚：

- **`winCnt`**（配置时）：这条流**要用**几个窗口。写入 `SCFG` 的 `WINCNT[20:16]` 字段时**减 1**（即写 `winCnt-1`），因为字段编码从 0 开始（`winCnt=1` → 写 0，`winCnt=16` → 写 15）。
- **`WINCUR[28:24]`**（运行时，只读）：硬件**当前正在写**哪个窗口。`PsiMsDaq_Str_CurrentWin()` 读它。
- **`LASTWIN`**（运行时，只读）：硬件**最后一个写完整**的窗口号。`PsiMsDaq_Str_GetLastWrittenWin()` 读它，这是中断派发判断「有哪些新窗口」的关键（u4-l2）。

#### 4.2.3 源码精读

先看 `PsiMsDaq_StrConfig_t` 里和窗口几何直接相关的字段：

[drivers/psi_ms_daq_axi/src/psi_ms_daq.h:265-274](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L265-L274) — 流配置结构体，注意 `winCnt`/`winSize`/`bufStartAddr` 的单位：

```c
typedef struct {
    uint32_t postTrigSamples;   // postTrig 样本数（含触发样本）
    PsiMsDaq_RecMode_t recMode;
    bool winAsRingbuf;          // 4.3 详讲
    bool winOverwrite;          // 4.4 详讲
    uint8_t  winCnt;            // 窗口个数
    uint32_t bufStartAddr;      // 本流缓冲起始地址
    uint32_t winSize;           // 单个窗口字节数
    uint16_t streamWidthBits;   // 流位宽（8 的倍数）
} PsiMsDaq_StrConfig_t;
```

`winCnt` 写入寄存器时的「减 1」编码，以及同时写进软件缓存，在 `PsiMsDaq_Str_Configure` 末尾：

[drivers/psi_ms_daq_axi/src/psi_ms_daq.c:306-317](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L306-L317) — `winCnt-1` 写入 `SCFG.WINCNT` 字段，`winCnt`/`winSize`/`bufStart` 原值缓存进流实例供后续窗口回读复用：

```c
SAFE_CALL(PsiMsDaq_RegSetField(ipHandle, PSI_MS_DAQ_CTX_SCFG(strNr),
        PSI_MS_DAQ_CTX_SCFG_LSB_WINCNT, PSI_MS_DAQ_CTX_SCFG_MSB_WINCNT,
        config_p->winCnt-1));            // 注意减 1
//缓存到软件状态
inst_p->widthBytes = config_p->streamWidthBits/8;
inst_p->isConfigured = true;
inst_p->windows    = config_p->winCnt;   // 缓存原值（不减1）
inst_p->bufStart   = config_p->bufStartAddr;
inst_p->postTrig   = config_p->postTrigSamples;
inst_p->winSize    = config_p->winSize;
```

⚠️ 注意这里有一个**不对称**：写寄存器用 `winCnt-1`，但软件缓存 `inst_p->windows` 用原值 `winCnt`。所以寄存器里的 `WINCNT` 字段值永远比 `inst_p->windows` 小 1，下回读 `SCFG` 时要记得加回 1。

`winCnt` 上限校验在函数开头：

[drivers/psi_ms_daq_axi/src/psi_ms_daq.c:276-278](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L276-L278) — `winCnt` 不得超过 IP 的 `maxWindows`，否则返回 `IllegalWinCnt`：

```c
if (config_p->winCnt > ipInst_p->maxWindows) {
    return PsiMsDaq_RetCode_IllegalWinCnt;
}
```

窗口寄存器区的元数据布局（u3-l1 已列，这里聚焦和「窗口」概念最相关的 `WINCNT` 双含义字段）：

[drivers/psi_ms_daq_axi/src/psi_ms_daq.h:176-182](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L176-L182) — 每个 `(n,w)` 窗口占 4 个字：采样数/触发标志、末样本地址、时间戳低/高：

```c
#define PSI_MS_DAQ_WIN_WINCNT(n, w, so)   (0x4000+(so)*(n)+0x10*(w))   // [30:0]=CNT, [31]=ISTRIG
#define PSI_MS_DAQ_WIN_LAST(n, w, so)     (0x4004+(so)*(n)+0x10*(w))   // 最后一个样本的字节地址
#define PSI_MS_DAQ_WIN_TSLO(n, w, so)     (0x4008+(so)*(n)+0x10*(w))   // 时间戳低32位
#define PSI_MS_DAQ_WIN_TSHI(n, w, so)     (0x400C+(so)*(n)+0x10*(w))   // 时间戳高32位
```

真实的两份窗口配置（main.c 里 stream0 用 3 个窗口、stream1 用 6 个窗口）：

[refdesign/ZCU102/Sdk/app/src/main.c:159-168](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/Sdk/app/src/main.c#L159-L168) — stream0：`winCnt=3`、`winSize=32` 字节、`bufStart=0x40000000`，占 DDR `3×32=96` 字节；stream1（[main.c:180-189](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/Sdk/app/src/main.c#L180-L189)）：`winCnt=6`、`winSize=32000` 字节、`bufStart=0x50000000`，占 `6×32000=192000` 字节。

#### 4.2.4 代码实践

**实践目标**：手算 stream1 的窗口 DDR 布局，验证你对 `winCnt`/`winSize`/`bufStart` 三者关系的理解。

**操作步骤**：

1. 取 stream1 参数：`bufStart=0x50000000`、`winSize=32000 (0x7D00)`、`winCnt=6`。
2. 用公式 4.2.2 计算 window0～window5 的起始地址。
3. 计算这条流总占用 DDR 字节数，确认它不会越界到 stream0 的区域（`0x40000000` 段）。

**预期结果**（待本地验证的纯算术，可自行核对）：

- window0 起始 `0x50000000`、window1 起始 `0x50007D00`、window2 起始 `0x5000FA00`、window3 起始 `0x50017700`、window4 起始 `0x5001F400`、window5 起始 `0x50027100`。
- 总占用 `6 × 32000 = 192000` 字节 ≈ 187.5 KiB，远小于 `0x10000000`（256 MiB）的段间距，与 stream0 的 `0x40000000` 段无重叠。

#### 4.2.5 小练习与答案

**练习 1**：若把 stream0 的 `winCnt` 从 3 改成 20，而 IP 的 `MaxWindows_g`（`PsiMsDaq_Init` 传入的 `maxWindows`）是 16，会发生什么？

> **参考答案**：`PsiMsDaq_Str_Configure` 第 276 行的校验 `config_p->winCnt > ipInst_p->maxWindows` 命中（20 > 16），函数立即返回 `PsiMsDaq_RetCode_IllegalWinCnt`，没有任何寄存器被改动。注意是和**软件侧的 `maxWindows`** 比较，这个值必须和 Vivado IPI 里设的 `MaxWindows_g` 一致（见头文件 `PsiMsDaq_Init` 注释）。

**练习 2**：为什么 `SCFG` 寄存器里 `WINCNT` 字段写入时要减 1，而 `WINCUR`（当前窗口）字段不需要？

> **参考答案**：`WINCNT` 表示「数量」，数量字段的 0 是无效值（至少 1 个窗口），硬件用 `[WINCNT-1]` 编码可以表示 `1..2^N` 个窗口，这是寄存器设计的常见惯例（类似「N 选 1」用 0 起始编号）。`WINCUR` 表示「索引」，索引天然从 0 起始，直接用原值即可，无需偏移。

---

### 4.3 缓冲布局：线性 vs 环形（RINGBUF 位）

#### 4.3.1 概念说明

4.2 画的窗口布局图是「线性视角」——每个窗口像一根直条，数据从左端写到右端就停。但实际采集里有个常见痛点：**触发可能出现在窗口的任意位置**，我们往往希望「触发前的预触发数据 + 触发后的 postTrig 数据」都留在同一个窗口里。如果触发靠近窗口开头，预触发数据会不够；如果用线性缓冲，硬件只能丢掉超出窗口开头的数据。

**环形缓冲（ringbuf）** 解决这个问题：把窗口看成一个**首尾相接的环**，硬件永远往环里写最新数据、覆盖最旧数据。当触发到来时，触发**前后**一段连续数据都还在环里（只要总采样数不超过窗口容量），驱动再负责把环「剪开、拉直」成线性顺序交给用户——这正是 `GetDataUnwrapped` 名字里「Unwrapped（去环绕）」的由来，也是 u4-l3 的主题。

所以 `winAsRingbuf` 这个字段回答的是：**单个窗口内部，硬件按线性写还是环形写？**

- `winAsRingbuf = false`（线性）：从窗口起点开始写，写满即停/即换窗口。触发必须发生在窗口开头之后才能留住预触发数据。
- `winAsRingbuf = true`（环形）：窗口成一个环，硬件持续覆盖最旧数据。触发可在任意位置，预触发数据天然被保留在环里。

两者在 DDR 上的「物理布局」其实是一样的（都是 `winSize` 字节的连续区），差别只在**硬件写指针的回绕行为**和**驱动读出来时是否要拼接**。

#### 4.3.2 核心流程

线性缓冲里，触发样本在窗口中的位置：

```
窗口: [预触发数据 ... | 触发样本 | postTrig数据 ...]
       ^winStart                                  
       数据从 winStart 开始线性向右写
```

环形缓冲里，触发样本可在环上任意位置，数据可能「绕一圈」：

```
环形窗口(winSize字节，首尾相接):
      ... postTrig尾 | 预触发头 ... | 触发样本 | postTrig头 ...
                       ^__________________________^
                       读时要按"触发样本"为中心拼成线性序列
                       可能需要两段 memcpy 拼接
```

设窗口起始 `winStart`、窗口末字节 `winLast = winStart + winSize - 1`。环形模式下，硬件记录的「最后写入样本地址」`lastSplAddr` 可能在窗口任意位置；以触发样本为中心、向后取 postTrig、向前取 preTrig 时，数据范围可能跨越 `winLast ↔ winStart` 的回绕边界，这就是 u4-l3 里 `trigByteAddr`/`lastByteAddr` 要做 `±winSize` 修正的原因。

一个关键约束：环形模式下，单次可读的总样本数不能超过窗口容量，否则环里最早的预触发数据已经被覆盖、无法恢复。这构成 `GetDataUnwrapped` 里 `preTrigSamples > preTrig` 等校验的物理意义（u4-l3）。

#### 4.3.3 源码精读

`winAsRingbuf` 字段定义：

[drivers/psi_ms_daq_axi/src/psi_ms_daq.h:268](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L268) — 注释明确「true=环形，false=线性」：

```c
bool winAsRingbuf;   // true=环形缓冲模式, false=线性模式
```

它最终落到 CTX 区 `SCFG` 寄存器的 `RINGBUF` 位：

[drivers/psi_ms_daq_axi/src/psi_ms_daq.h:164-166](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L164-L166) — `SCFG` 寄存器把 `RINGBUF`(bit0) 与 `OVERWRITE`(bit8) 两个独立策略压进同一字：

```c
#define PSI_MS_DAQ_CTX_SCFG(n)				(0x1000+0x20*(n))
#define PSI_MS_DAQ_CTX_SCFG_BIT_RINGBUF		(1 << 0)
#define PSI_MS_DAQ_CTX_SCFG_BIT_OVERWRITE	(1 << 8)
```

写入发生在 `PsiMsDaq_Str_Configure`（注意是用 `RegSetBit` 单独写一位，而非整字覆盖，因此 `RINGBUF` 和 `OVERWRITE` 是两次独立的 RMW）：

[drivers/psi_ms_daq_axi/src/psi_ms_daq.c:292-299](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L292-L299) — 分别把 `winAsRingbuf`、`winOverwrite` 写入 `SCFG` 的对应位：

```c
SAFE_CALL(PsiMsDaq_RegSetBit(ipHandle, PSI_MS_DAQ_CTX_SCFG(strNr),
        PSI_MS_DAQ_CTX_SCFG_BIT_RINGBUF,   config_p->winAsRingbuf));
SAFE_CALL(PsiMsDaq_RegSetBit(ipHandle, PSI_MS_DAQ_CTX_SCFG(strNr),
        PSI_MS_DAQ_CTX_SCFG_BIT_OVERWRITE, config_p->winOverwrite));
```

注意：`winAsRingbuf` **没有被缓存进 `PsiMsDaq_StrInst_t`**（对比 4.2.3 里 `winSize`/`bufStart` 都被缓存了）。原因是驱动在回读数据时并不需要知道「当年配的是不是环形」——它通过 `lastSplAddr` 与 `winStart/winLast` 的相对位置关系**动态判断**当前窗口这次录制有没有发生回绕（u4-l3 的 `firstByteLinear >= winStart` 判定），而不是依赖一个静态标志。这是一个值得品味的设计选择。

参考设计里两路流都用了环形缓冲：

[refdesign/ZCU102/Sdk/app/src/main.c:162](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/Sdk/app/src/main.c#L162) — `cfg0.winAsRingbuf = true`，stream1 同样（[main.c:183](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/Sdk/app/src/main.c#L183)）。

#### 4.3.4 代码实践

**实践目标**：通过对比 main.c 里 stream0 的「读 5 个预触发 + 5 个 postTrig」调用，体会环形缓冲为何能让「触发在中间」的数据被完整读出。

**操作步骤**：

1. 看 [main.c:63](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/Sdk/app/src/main.c#L63)：`PsiMsDaq_StrWin_GetDataUnwrapped(winInfo, 5, 5, buf, sizeof(buf))`，即读 5 个预触发样本 + 5 个 postTrig 样本（含触发样本）。
2. 结合 stream0 的 `winSize=32` 字节、`streamWidthBits=16`（每样本 2 字节），算出窗口容量 = 16 个样本。
3. 思考：若 `winAsRingbuf=false`（线性），且某次触发恰好出现在窗口第 2 个样本位置，还能读出 5 个预触发样本吗？

**需要观察的现象 / 预期结果**：

- 窗口容量 16 样本，读 10 样本（5+5）在容量内，环形下没问题。
- 线性模式下若触发在第 2 个样本，则其前面只有 1~2 个样本可作预触发，强行读 5 个预触发会得到 `PsiMsDaq_RetCode_MorePreTrigThanAvailable`（u4-l3 的校验）。**这正说明为什么参考设计默认用环形**：它让触发位置不影响预触发数据可获取性。

**待本地验证**：上述线性模式越界返回码的推断基于 u4-l3 的校验逻辑（本讲尚未精读该函数），可在学完 u4-l3 后回看确认。

#### 4.3.5 小练习与答案

**练习 1**：既然 `SCFG` 里 `RINGBUF` 和 `OVERWRITE` 是两个独立位，能否同时设 `winAsRingbuf=true` 且 `winOverwrite=true`？语义上是什么效果？

> **参考答案**：从驱动代码看，这两个位互不校验、可独立置位，所以技术上可以同时设。语义上是「单窗口内部环形写 + 窗口之间可被覆盖（即使含未处理数据）」——这是一种「我只要最新数据、丢掉旧的也无妨」的激进策略。但要注意：`winOverwrite=true` 会导致窗口式 IRQ 不可用（见 4.4），此时必须改用流式 IRQ。

**练习 2**：为什么驱动不在 `PsiMsDaq_StrInst_t` 里缓存 `winAsRingbuf`，却要缓存 `winSize`？

> **参考答案**：`winSize` 在回读时要做地址计算（`winStart = bufStart + winSize*winNr`、回绕修正 `±winSize`），是必须的几何参数；而是否环形这件事，驱动通过比较 `lastSplAddr` 与 `winStart` 的相对位置就能在运行时动态推断（数据有没有绕回），不需要静态标志。少缓存一个字段 = 少一处「配置与状态不一致」的隐患。

---

### 4.4 覆盖策略与触发前后采样：OVERWRITE 位、pre/postTrig、ISTRIG

#### 4.4.1 概念说明

最后一个核心概念其实是两个紧密耦合的子概念：

**(a) 覆盖策略 `winOverwrite`**：当一个窗口已经写满、且 CPU 还没来得及处理（没调 `MarkAsFree`）时，硬件能不能往这个窗口里写新数据、覆盖掉旧数据？

- `winOverwrite = false`：**不允许覆盖**。窗口写满且未释放时，硬件宁可停止往该窗口写（或跳过），保证每份录制数据都能被 CPU 完整读到。这是大多数应用的安全选择。
- `winOverwrite = true`：**允许覆盖**。硬件永远保留最新数据，即使 CPU 还没处理旧的。适合「我只关心最新波形、丢掉旧的没关系」的监控场景。

**关键耦合**：`winOverwrite` 直接决定了你能用哪种中断方案！

- `false` → 可以用**窗口式 IRQ**（`SetIrqCallbackWin`）：驱动能保证「每个窗口回调恰好一次」，因为它假定窗口不会被偷偷覆盖。
- `true` → 只能用**流式 IRQ**（`SetIrqCallbackStr`）：因为窗口可能被覆盖，驱动无法再保证「每窗口一次」的语义，回调里由用户全权处理。

这一点写在了头文件最顶部的 `@section irq_handling` 大段注释里（u4-l2 会精读实现）。

**(b) 触发前后采样 pre/postTrig 与 `ISTRIG` 标志**：一次含触发的录制，由「触发前的预触发样本」+「触发后（含触发样本本身）的 postTrig 样本」组成。两个量在配置时就定死：

- `postTrigSamples`（配置时给）：含触发样本在内的 postTrig 样本数，写入 `POSTTRIG` 寄存器，硬件据此知道触发后还要录多少。
- `preTrig`（运行时算）：等于「本窗口实际写入的总样本数 − postTrigSamples」。因为环形缓冲下总样本数取决于触发时机，所以预触发数是**运行时才知道**的，无法配置。

每个窗口的 `WINCNT` 寄存器最高位 `ISTRIG`（bit31）标记这个窗口**是否含触发**。为什么要有这个标志？因为并非所有窗口都含触发——比如 Continuous 模式下硬件可能周期性地把环形缓冲「快照」到某窗口，这种窗口没有触发中心点，`GetPreTrigSamples`/`GetTimestamp` 对它无意义，调用会返回 `NoTrigInWin`。

#### 4.4.2 核心流程

四要素的关系图：

```
配置阶段(Configure):
  postTrigSamples ──────► POSTTRIG 寄存器 (硬件用)
                      └─► inst_p->postTrig (缓存, 回读时用)
  winOverwrite ─────────► SCFG.OVERWRITE 位 ──► 决定可用 IRQ 方案
  winAsRingbuf ─────────► SCFG.RINGBUF 位   ──► 决定窗口内写入是否回绕

运行阶段(每个完成的窗口):
  硬件写: 总采样数 N ──► WINCNT[30:0]
         含触发?    ──► WINCNT[31] = ISTRIG
         末样本地址 ──► LAST
         触发时间戳 ──► TSLO/TSHI (仅当 ISTRIG=1 有意义)

回读阶段(GetPreTrigSamples):
  preTrig = N − postTrigSamples    (要求 ISTRIG=1, 否则返回 NoTrigInWin)
```

覆盖策略与 IRQ 方案的绑定关系（本讲最重要的实践结论之一）：

| `winOverwrite` | 可用的 IRQ 回调注册函数 | 回调被调用语义 |
|---------------|----------------------|--------------|
| `false` | `PsiMsDaq_Str_SetIrqCallbackWin` | 每个完成的窗口恰好一次，带 `WinInfo_t` |
| `true`  | `PsiMsDaq_Str_SetIrqCallbackStr` | 每次 IP 触发 IRQ 一次，仅给流句柄，用户自管 |
| 任意 | 二者**只能选一**，混用返回 `IrqSchemesWinAndStrAreExclusive` | — |

#### 4.4.3 源码精读

`winOverwrite` 字段与覆盖策略说明：

[drivers/psi_ms_daq_axi/src/psi_ms_daq.h:269](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L269) — 注释建议「通常设 false」：

```c
bool winOverwrite;   // true=即使窗口含数据也覆盖；通常设 false
```

`OVERWRITE` 位写入见 4.3.3 的 `RegSetBit` 调用（[psi_ms_daq.c:296-299](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L296-L299)）。

覆盖策略与 IRQ 方案的强绑定，写在头文件顶部文档里：

[drivers/psi_ms_daq_axi/src/psi_ms_daq.h:52-63](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L52-L63) — Window based IRQ 方案的适用前提就是「overwrite 必须关闭 + 用户必须 MarkAsFree」：

```c
// This handling scheme only works if each window is really processed by the user
// and protected against being overwritten until the user acknowledged the processing.
// ...window overwriting must be disabled (config.overwrite = false) and that the
// user must acknowledge ... by calling PsiMsDaq_StrWin_MarkAsFree().
```

两种 IRQ 方案互斥的代码校验：

[drivers/psi_ms_daq_axi/src/psi_ms_daq.c:336-368](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L336-L368) — `SetIrqCallbackWin` 检查「是否已注册流式回调」，反之亦然，任何一边已注册就拒绝另一边：

```c
PsiMsDaq_RetCode_t PsiMsDaq_Str_SetIrqCallbackWin(...) {
    ...
    if (NULL != inst_p->irqFctStr) {                       // 已用流式?
        return PsiMsDaq_RetCode_IrqSchemesWinAndStrAreExclusive;
    }
    inst_p->irqFctWin = irqCb;
    ...
}
// SetIrqCallbackStr 对称地检查 irqFctWin != NULL
```

注意：**驱动并没有在 `SetIrqCallbackWin` 里校验 `winOverwrite` 是否真的为 false**——它只保证「两种 IRQ 方案互斥」，至于「窗口式 IRQ 是否真的适合你的 overwrite 设置」留给用户自己负责。这是一个容易被踩坑的隐含契约。

pre/postTrig 与 `ISTRIG` 的关系，最清楚地体现在 `GetPreTrigSamples`：

[drivers/psi_ms_daq_axi/src/psi_ms_daq.c:530-551](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L530-L551) — 先读 `ISTRIG`，无触发直接返回 `NoTrigInWin`；有触发则 `preTrig = 总样本数 − 缓存的 postTrig`：

```c
bool containsTrig;
SAFE_CALL(PsiMsDaq_RegGetBit(ipHandle,
        PSI_MS_DAQ_WIN_WINCNT(str_p->nr, winInfo.winNr, ip_p->strAddrOffs),
        PSI_MS_DAQ_WIN_WINCNT_BIT_ISTRIG, &containsTrig));
if (!containsTrig) {
    return PsiMsDaq_RetCode_NoTrigInWin;          // 本窗口无触发
}
uint32_t samples;
SAFE_CALL(PsiMsDaq_StrWin_GetNoOfSamples(winInfo, &samples));
*preTrigSamples_p = samples - str_p->postTrig;     // 运行时才算得出
```

`ISTRIG` 标志位定义：

[drivers/psi_ms_daq_axi/src/psi_ms_daq.h:176-179](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L176-L179) — `WINCNT` 寄存器是双含义字段：低 31 位是采样数，最高位是触发标志：

```c
#define PSI_MS_DAQ_WIN_WINCNT(n, w, so)        (0x4000+(so)*(n)+0x10*(w))
#define PSI_MS_DAQ_WIN_WINCNT_LSB_CNT           0
#define PSI_MS_DAQ_WIN_WINCNT_MSB_CNT           30
#define PSI_MS_DAQ_WIN_WINCNT_BIT_ISTRIG        (1 << 31)
```

参考设计 main.c 里 stream0 用了 `postTrigSamples=5`、`winOverwrite=false`、窗口式 IRQ 的典型组合：

[refdesign/ZCU102/Sdk/app/src/main.c:159-176](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/Sdk/app/src/main.c#L159-L176) — 配 `winOverwrite=false` 后，紧接着注册的就是 `SetIrqCallbackWin`，二者配套出现：

```c
PsiMsDaq_StrConfig_t cfg0 = {
    .postTrigSamples = 5,
    .recMode = PsiMsDaqn_RecMode_Continuous,
    .winAsRingbuf = true,
    .winOverwrite = false,        // ← 不覆盖
    .winCnt = 3, ...
};
PsiMsDaq_Str_Configure(daqStr0, &cfg0);
PsiMsDaq_Str_SetIrqCallbackWin(daqStr0, Str0Irq, NULL);  // ← 故而用窗口式
```

#### 4.4.4 代码实践

**实践目标**：为四种录制模式各设计一个真实采集场景，并验证你对 TriggerMask 模式下 `ARM` 前后行为差异的理解。这是本讲规格指定的核心实践任务。

**操作步骤**：

1. 为下表四个场景，填入「推荐 `recMode`」「是否需要 `Arm()`」「推荐 `winOverwrite`」「推荐 IRQ 方案」四列。先自己填，再对照下面的参考答案。
2. 针对 TriggerMask 模式，用一段话描述「`Arm()` 之前」与「`Arm()` 之后」在触发处理上的差异。

**场景清单（请先独立思考）**：

| 场景 | 推荐 recMode | 需 Arm? | winOverwrite | IRQ 方案 |
|------|-------------|---------|--------------|---------|
| (A) 持续监控电源电流，CPU 实时画最新波形 | ? | ? | ? | ? |
| (B) 粒子探测器：实验开始后才接受粒子击中事件，且每条击中都要完整记录 | ? | ? | ? | ? |
| (C) 捕获设备上电瞬态（仅一次），抓到就停 | ? | ? | ? | ? |
| (D) 手动调试：工程师在终端里按命令开始/停止录制 | ? | ? | ? | ? |

**参考答案**：

| 场景 | recMode | 需 Arm? | winOverwrite | IRQ 方案 |
|------|---------|---------|---------|---------|
| (A) | Continuous | 否 | true（只要最新） | Stream（`SetIrqCallbackStr`） |
| (B) | TriggerMask | 是（实验开始时 Arm） | false（每条都要完整读） | Window（`SetIrqCallbackWin`） |
| (C) | SingleShot | 是（上电后 Arm 一次） | false | Window |
| (D) | Manual | 是（当开关用） | 视需求 | 视 `winOverwrite` 而定 |

**TriggerMask 模式 `ARM` 前后差异**（核心描述）：

- **`Arm()` 之前**：硬件已经在持续录制预触发数据（环形缓冲里始终保留着「最近一段」），但**触发检测被屏蔽**——即使此时流上到来符合触发条件的样本，硬件也不会把它认定为一个有效触发、不会产生含 `ISTRIG=1` 的新窗口。这一阶段相当于「枪已上膛但保险没开」。
- **`Arm()` 之后**：触发检测被打开，下一个符合触发条件的样本会被认定为一个有效触发，硬件围绕它录完 `postTrigSamples`，写出一个 `ISTRIG=1` 的窗口并（若开了中断）触发 IRQ。此后硬件继续屏蔽/继续接受下一次触发，取决于上游 FSM 的具体行为。

> 说明：「屏蔽期间仍在录预触发」「Arm 后接受一次或多次触发」的精确时序由上游 IP-Core 硬件实现，本仓库驱动只写 `RECM` 字段并把 `ARM` 位置 1；以上描述是头文件枚举注释的概括，详细行为以 `psi_multi_stream_daq.pdf` 为准。

**待本地验证**：若手头有 ZCU102 板，可把 main.c 的 `cfg0.recMode` 改成 `TriggerMask`、在 `SetEnable` 后加一行 `PsiMsDaq_Str_Arm(daqStr0)`，然后分别在 Arm 前后用菜单发触发，对比是否产生 `Str0Irq` 回调来验证上述差异。

#### 4.4.5 小练习与答案

**练习 1**：用户配置了 `winOverwrite=true`，却调用了 `PsiMsDaq_Str_SetIrqCallbackWin`，驱动会报错吗？会发生什么？

> **参考答案**：**驱动不会报错**。`SetIrqCallbackWin` 只检查「是否已注册流式回调」（`irqFctStr != NULL`），不检查 `winOverwrite`。所以注册会成功。但在运行时，由于窗口可能被硬件偷偷覆盖，窗口式 IRQ「每窗口恰好一次」的语义无法保证——可能出现某个窗口的回调丢失、或 `irqCalledWin` 位图逻辑（u4-l2）与实际硬件状态不一致。这是一个**隐含契约被违反但驱动不拦截**的陷阱，用户必须自己保证 `winOverwrite=true` 时只用 `SetIrqCallbackStr`。

**练习 2**：`GetPreTrigSamples` 为什么要在函数里先读 `ISTRIG` 再算减法，而不是直接相信调用者传来的窗口一定有触发？

> **参考答案**：因为「窗口」不一定都含触发。Continuous 模式下硬件可能把环形缓冲定期快照成无触发中心的窗口；即使触发类模式，硬件也可能产出不含完整触发的窗口。若不检查 `ISTRIG` 就直接 `samples − postTrig`，对一个总样本数小于 `postTrig` 的无触发窗口会得到下溢的巨大无符号数。所以先用 `ISTRIG` 把「无触发」情况挡掉、返回 `NoTrigInWin`，是必要的安全卫哨。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「为一条新流设计完整配置」的小任务。

**任务背景**：你要给参考设计加第三条流 stream2，需求如下：

- 数据源：一个 32 位宽的传感器 AXI-Stream。
- 目的：捕获稀有事件。事件由外部触发标记（流上的 `TLast`，对应 `UseLastAsTrigger_g=true`），事件非常稀少，但一旦发生必须完整记录「事件前 200 个样本 + 事件后 200 个样本」。
- CPU 处理能力有限，希望每个事件都通过中断被独立通知、独立处理，绝不丢失。
- DDR 区 `0x60000000` 起的 1 MiB 可分配给这条流。IP 的 `maxWindows=16`。

**请完成**：

1. **选模式**：从四种 `recMode` 中选一个并说明理由。（提示：稀有事件、要反复捕获、每次都要等「准备好」）
2. **定窗口几何**：算出 `winCnt`、`winSize`、`bufStartAddr`、`streamWidthBits`，要求总占用 ≤ 1 MiB 且 `winCnt ≤ 16`。给出 `SCFG.WINCNT` 字段实际写入的值。
3. **定缓冲策略**：选 `winAsRingbuf` 并解释为什么这个场景几乎必须选它。
4. **定覆盖与 IRQ**：选 `winOverwrite` 和 IRQ 注册函数，并指出二者为何必须配套。
5. **画调用顺序**：按正确顺序写出从 `PsiMsDaq_Init` 到开始接受触发的完整 API 调用序列（注意 `Arm` 的时机）。

**参考答案要点**（自己先做再对照）：

1. **TriggerMask**。理由：事件稀少但需反复捕获、每次都要在「系统准备好」后才接受 → 持续录预触发 + Arm 后才检测触发的语义最契合。（SingleShot 也可考虑，但只能抓一次，不符合「反复捕获」。）
2. `streamWidthBits=32`（每样本 4 字节）。每个事件要 200+200=400 样本 = 1600 字节，留余量取 `winSize=2048` 字节（0x800，且必须是每样本字节数 4 的倍数）。`winCnt` 取 8（≤16，总占用 `8×2048=16384` 字节，远小于 1 MiB，且为 2 的幂便于对齐）。`bufStartAddr=0x60000000`。`SCFG.WINCNT` 字段实际写入 `winCnt-1 = 7`。
3. `winAsRingbuf=true`。因为触发（事件）出现在窗口任意位置，必须靠环形缓冲才能保证「触发前 200 样本」始终在环里。线性模式若事件靠近窗口开头则预触发数据不足。
4. `winOverwrite=false`（每个事件都不能丢，必须等 CPU 处理完）→ 配套用 `SetIrqCallbackWin`，回调里读完数据调 `MarkAsFree`。两者配套是因为窗口式 IRQ 的「每窗口恰好一次」语义依赖「窗口不被覆盖」。
5. 调用序列：
   ```c
   daqHandle = PsiMsDaq_Init(BASE, 3, 16, NULL);          // 假设共3条流
   PsiMsDaq_GetStrHandle(daqHandle, 2, &daqStr2);
   PsiMsDaq_StrConfig_t cfg2 = { .postTrigSamples=200,
       .recMode=PsiMsDaqn_RecMode_TriggerMask, .winAsRingbuf=true,
       .winOverwrite=false, .winCnt=8, .bufStartAddr=0x60000000,
       .winSize=2048, .streamWidthBits=32 };
   PsiMsDaq_Str_Configure(daqStr2, &cfg2);
   PsiMsDaq_Str_SetIrqCallbackWin(daqStr2, Str2Irq, NULL); // 先注册回调
   PsiMsDaq_Str_SetIrqEnable(daqStr2, true);               // 再开中断
   PsiMsDaq_Str_SetEnable(daqStr2, true);                  // 最后使能流
   PsiMsDaq_Str_Arm(daqStr2);                              // TriggerMask 必须Arm!
   // 此后流开始接受触发；每个完成窗口触发 Str2Irq
   ```

> ⚠️ 顺序细节：`Arm` 必须在 `SetEnable` 之后（流没使能时 Arm 无意义）；`SetIrqCallbackWin` 必须在 `SetIrqEnable` 之前，否则开中断但没回调会丢事件。这些顺序约束在参考设计 main.c 的 Init() 里都有体现（[main.c:169-176](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/Sdk/app/src/main.c#L169-L176)）。

## 6. 本讲小结

- **四种录制模式**的差别本质是「`ARM` 位在什么时机放行触发」：Continuous 不需 Arm、TriggerMask 在 Arm 后才检测触发、SingleShot 在 Arm 后只接受一次、Manual 把 ARM 当录制开关。`ARM` 是软件写位、`REC` 是硬件回报位。
- **窗口（Window）** 是一条流里独立的录制缓冲单元，数量 `winCnt`、大小 `winSize`，在 DDR 里首尾相接；窗口寄存器区为每个 `(流, 窗口)` 维护采样数/末样本地址/时间戳四字元数据。`winCnt` 写寄存器时减 1、缓存时用原值，注意这个不对称。
- **线性 vs 环形**（`SCFG.RINGBUF`）决定窗口内部写指针是否回绕；环形让「触发在中间」的数据天然被保留，代价是回读时要「去环绕」拼接（u4-l3 主题）。驱动不缓存 `winAsRingbuf`，而是运行时按地址相对位置动态判断。
- **覆盖策略 `winOverwrite`**（`SCFG.OVERWRITE`）与 **IRQ 方案强绑定**：false 配窗口式 IRQ（每窗口恰好一次），true 只能配流式 IRQ；驱动只校验「两种 IRQ 互斥」、不校验 overwrite，是个隐含契约陷阱。
- **pre/postTrig 与 `ISTRIG`**：postTrig 配置时定死、写入 `POSTTRIG` 寄存器并缓存；preTrig 运行时由 `总样本数 − postTrig` 算出；`WINCNT` 寄存器是双含义字段，低 31 位采样数、最高位 `ISTRIG` 标记是否含触发，无触发窗口调用 `GetPreTrigSamples`/`GetTimestamp` 会返回 `NoTrigInWin`。
- **边界认知**：本讲描述的「模式时序行为」依据是驱动头文件枚举注释 + 寄存器位语义；真正的触发检测 FSM 实现在上游 `psi_multi_stream_daq` 硬件里（本仓库不含），精确时序以 `psi_multi_stream_daq.pdf` 为准。

## 7. 下一步学习建议

本讲搭好了「录制模式 + 窗口 + 缓冲布局 + pre/postTrig」的概念地基，接下来两讲会把这些概念落到驱动的两个最复杂实现上：

- **u4-l2 中断处理：窗口式 vs 流式两种方案**：精读 `PsiMsDaq_HandleIrq`，看它如何用 `lastProcWin`/`irqCalledWin` 两个软件状态字段保证「每个窗口回调恰好一次」、如何屏蔽伪中断、如何用 `MarkAsFree` 清除 `irqCalledWin` 位。本讲 4.4 讲的「窗口式 IRQ 依赖 overwrite=false」会在那里看到实现层面的原因。
- **u4-l3 窗口数据回读与去环绕回拷贝**：精读 `PsiMsDaq_StrWin_GetDataUnwrapped`，看它如何用本讲 4.3 的环形布局概念，通过 `trigByteAddr`/`lastByteAddr` 的 `±winSize` 修正和「单次 vs 两段 memcpy」分支，把环形数据拉直。本讲 4.3.4 留下的「线性模式预触发不足返回 MorePreTrigThanAvailable」推断会在那里得到证实。
- **u4-l4 寄存器字段/比特助手、SAFE_CALL 与返回码**：把本讲反复出现的 `RegSetBit`/`RegSetField`（RMW）、`SAFE_CALL`、以及 `NoTrigInWin`/`IllegalWinCnt`/`IrqSchemesWinAndStrAreExclusive` 等返回码的系统来源讲清楚。

建议阅读顺序：u4-l2 → u4-l3 → u4-l4，然后进入 u5 用 ZCU102 参考设计做端到端验收。
