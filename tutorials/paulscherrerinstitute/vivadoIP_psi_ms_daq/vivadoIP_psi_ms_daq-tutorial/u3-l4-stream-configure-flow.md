# 流配置流程：从结构体到寄存器

## 1. 本讲目标

本讲承接 u3-l3（初始化与寄存器访问抽象），把镜头从「驱动底座」上移到「每条流怎么被配置起来」。读完本讲你应该能够：

- 说清 `PsiMsDaq_StrConfig_t` 八个字段各自的单位、含义，以及它们分别落到哪一类寄存器区。
- 解释为什么 `PsiMsDaq_Str_Configure` 在动任何寄存器之前，必须先让流处于「禁用」状态（`CheckStrDisabled`），以及流号校验 `CheckStrNr` 在整个驱动里的复用方式。
- 一步步跟踪一次真实的 `Str_Configure` 调用，写出它对每条流各寄存器的（地址, 值）写入序列。
- 理解 `winCnt` 写入寄存器时要减 1（`winCnt-1`）的「从 0 起编号」编码约定。
- 看懂配置函数「硬件寄存器 + 软件缓存」双写设计：哪些字段会被缓存在流实例里、为什么后面的窗口回读函数必须依赖这份缓存。

本讲只解决一个问题：**一条 AXI-Stream 流，是怎么从一个 C 结构体配置，变成 IP-Core 里一组寄存器值的。**

## 2. 前置知识

本讲默认你已经读过 u3-l1（寄存器映射全景）、u3-l2（驱动数据模型与句柄抽象）、u3-l3（初始化与寄存器访问抽象）。下面三句话快速回忆关键结论：

1. **四块地址空间**：寄存器分为通用（`0x000`）、逐流录制（`0x200`，流间步进 `0x10`）、逐流上下文 CTX（`0x1000`，步进 `0x20`）、窗口 WIN（`0x4000`，窗口间步进 `0x10`、流间步进 `strAddrOffs`）。本讲配置写入只用到「逐流录制」和「逐流上下文」两块，不碰窗口区。
2. **两个不透明句柄**：`PsiMsDaq_IpHandle` 指向整片 IP 的 `PsiMsDaq_Inst_t`，`PsiMsDaq_StrHandle` 指向某条流的 `PsiMsDaq_StrInst_t`；流实例里有一个回指指针 `ipHandle`，是流级 API 取回 `baseAddr` 与访问函数的唯一通道。
3. **三层访问栈**：高层 API → `PsiMsDaq_RegWrite/RegRead`（叠加 `baseAddr`、经函数指针分发）→ `PsiMsDaq_*_Standard`（`volatile` 裸指针 MMIO）。本讲还会用到两个「读改写」字段助手 `PsiMsDaq_RegSetField` / `PsiMsDaq_RegSetBit`（它们的 RMW 细节属于 u4-l4，本讲只需知道：它们先读回整个 32 位字、只改目标字段、再整字写回，因此对同一寄存器的多次字段写入不会互相覆盖）。

两个本讲要用到的术语：

- **RMW（Read-Modify-Write）**：先读回一个寄存器全字，在内存里修改其中某几位，再把全字写回。用于「一个 32 位寄存器里塞了多个独立字段」的场景。
- **软件缓存（software cache）**：驱动把一部分配置参数既写进硬件寄存器、又记在流实例结构体里，让后续 API 不必再次访问寄存器或要求用户重复传参。

## 3. 本讲源码地图

本讲涉及的关键文件只有两个，外加参考设计里的一个调用点：

| 文件 | 作用 |
|---|---|
| [drivers/psi_ms_daq_axi/src/psi_ms_daq.h](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h) | 公开头文件。定义 `PsiMsDaq_StrConfig_t` 配置结构体、`PsiMsDaq_RecMode_t` 录制模式枚举、所有寄存器宏、`PsiMsDaq_RetCode_t` 返回码。 |
| [drivers/psi_ms_daq_axi/src/psi_ms_daq.c](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c) | 驱动实现。包含 `PsiMsDaq_Str_Configure`、守卫函数 `CheckStrDisabled` / `CheckStrNr`、私有结构体 `PsiMsDaq_StrInst_t`。 |
| [refdesign/ZCU102/Sdk/app/src/main.c](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/Sdk/app/src/main.c) | ZCU102 参考应用。`Init()` 里对 stream 0 的 `cfg0` 配置是本讲代码实践的依据。 |

## 4. 核心概念与源码讲解

### 4.1 配置结构体 PsiMsDaq_StrConfig_t

#### 4.1.1 概念说明

配置一条流需要告诉 IP 八件事：触发后录多少采样、用什么录制模式、窗口当环形缓冲还是线性缓冲、窗口满了是否允许覆盖、用几个窗口、缓冲区在 DDR 的哪里、每个窗口多大、流的位宽是多少。这八个参数被打包成一个普通 C 结构体 `PsiMsDaq_StrConfig_t`，由调用者按字段初始化后传给 `PsiMsDaq_Str_Configure`。

结构体是「按值聚合的纯数据」，没有任何函数指针或句柄——它是用户与驱动之间的一次性「配置快照」。

#### 4.1.2 核心流程

用户侧的典型初始化片段（摘自头文件示例与参考设计）：

```
PsiMsDaq_StrConfig_t cfg = {
    .postTrigSamples  = ...;   // 触发后采样数（含触发采样）
    .recMode          = ...;   // 录制模式枚举
    .winAsRingbuf     = ...;   // 窗口是否当环形缓冲
    .winOverwrite     = ...;   // 窗口是否允许覆盖
    .winCnt           = ...;   // 用几个窗口
    .bufStartAddr     = ...;   // DDR 缓冲区起始地址
    .winSize          = ...;   // 单个窗口大小（字节）
    .streamWidthBits  = ...;   // 流位宽（比特，必须是 8 的倍数）
};
PsiMsDaq_Str_Configure(strHandle, &cfg);
```

八个字段到寄存器区的大致归属：

| 字段 | 单位/类型 | 落到的寄存器区 |
|---|---|---|
| `postTrigSamples` | 采样数 | 逐流录制 `POSTTRIG` |
| `recMode` | 枚举（0..3） | 逐流录制 `MODE.RECM` |
| `winAsRingbuf` | bool | 逐流上下文 `SCFG.RINGBUF` |
| `winOverwrite` | bool | 逐流上下文 `SCFG.OVERWRITE` |
| `winCnt` | 窗口数 | 逐流上下文 `SCFG.WINCNT`（**减 1 编码**） |
| `bufStartAddr` | 字节地址 | 逐流上下文 `BUFSTART` |
| `winSize` | 字节 | 逐流上下文 `WINSIZE` |
| `streamWidthBits` | 比特 | 不直接写寄存器，仅用于校验和换算成 `widthBytes` 缓存 |

注意一个不对称：**`streamWidthBits` 没有对应的硬件寄存器**。硬件的 AXI-Stream 位宽在 Vivado 打包时由 `StreamNWidth_g` 泛型固化（见 u2-l1），运行时不可改；驱动拿到 `streamWidthBits` 只是为了校验「是 8 的倍数」以及换算成字节数 `widthBytes = streamWidthBits/8` 存进软件缓存，供后面窗口回读做字节地址计算。

#### 4.1.3 源码精读

结构体定义在头文件里，每个字段都带注释说明单位和含义：

[drivers/psi_ms_daq_axi/src/psi_ms_daq.h:L262-L274](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L262-L274) —— 定义 `PsiMsDaq_StrConfig_t`，注释里特别强调 `winSize` 单位是字节、`streamWidthBits` 必须是 8 的倍数。

录制模式枚举紧挨在结构体之前：

[drivers/psi_ms_daq_axi/src/psi_ms_daq.h:L252-L260](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L252-L260) —— `PsiMsDaq_RecMode_t`：`Continuous=0`、`TriggerMask=1`、`SingleShot=2`、`Manual=3`。这个整数值会被原样写进 `MODE` 寄存器的 `RECM[1:0]` 字段。

参考设计 stream 0 的真实配置（本讲实践的依据）：

[refdesign/ZCU102/Sdk/app/src/main.c:L159-L169](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/Sdk/app/src/main.c#L159-L169) —— `cfg0` 用指定初始化器（designated initializer）逐字段赋值，`winSize` 注释明确写「in bytes」。

#### 4.1.4 代码实践

1. **实践目标**：建立「字段 → 单位 → 寄存器区」的直觉。
2. **操作步骤**：打开 `psi_ms_daq.h` 的 `PsiMsDaq_StrConfig_t` 定义，对照上面的归属表，给每个字段标注一行「单位 / 落到哪块寄存器区」。
3. **需要观察的现象**：八个字段里有七个会变成硬件寄存器值，唯独 `streamWidthBits` 不对应任何运行时寄存器。
4. **预期结果**：你会得出 `streamWidthBits` 是「软件专用」字段，它的唯一硬件去向是 Vivado 综合期的泛型，而不是 CPU 可写的寄存器。
5. 待本地验证（纯阅读型实践，无需运行）。

#### 4.1.5 小练习与答案

**练习 1**：`winSize` 的单位是采样数还是字节？为什么不能用采样数？

**参考答案**：字节。注释写明 `Size of the windows`，参考设计里 `winSize = 32` 配 `streamWidthBits = 16`，即 32 字节 = 16 个 16-bit 采样；若以采样数为单位，不同位宽的流就无法用同一套地址加法（`bufStart + winSize*winNr`）来定位窗口起始地址。

**练习 2**：`winCnt` 字段类型是 `uint8_t`，理论范围 0..255，但实际有效上限是多少？

**参考答案**：受 `SCFG.WINCNT` 字段位宽 [20:16]（5 位）限制，最多编码 32 个窗口，且 `Str_Configure` 还会用 `winCnt > maxWindows` 做显式校验（`maxWindows` 来自 `PsiMsDaq_Init`，必须为 2 的幂）。

---

### 4.2 配置前置守卫：CheckStrNr 与 CheckStrDisabled

#### 4.2.1 概念说明

`PsiMsDaq_Str_Configure` 在改任何寄存器之前要做两类检查：

- **输入合法性**：流号是否越界（`CheckStrNr`）。
- **状态安全性**：这条流当前是不是已经「使能」着（`CheckStrDisabled`）。

第二类检查尤其关键：**配置寄存器时流必须处于禁用状态**。想象一条流正在以 2 GB/s 往 DDR 写数据，此时如果 CPU 改了 `BUFSTART` 或 `WINSIZE`，硬件的写地址生成器会立刻用新值继续写，可能写到非法地址、覆盖别的数据，或让窗口边界错乱。因此驱动用 `CheckStrDisabled` 强制「先停后改」。

`CheckStrNr` 则是整个驱动复用的通用守卫：`PsiMsDaq_GetStrHandle`、窗口回读等多处都调用它，把「流号 < maxStreams」这个不变量集中在唯一一处实现。

#### 4.2.2 核心流程

两个守卫的判断逻辑：

```
CheckStrNr(ipHandle, streamNr):
    读 inst_p->maxStreams
    若 streamNr >= maxStreams: 返回 IllegalStrNr
    否则: 返回 Success

CheckStrDisabled(ipHandle, streamNr):
    读 STRENA 寄存器（使能位图，bit=n 表示流 n 使能）
    若 (STRENA & (1 << streamNr)) != 0: 返回 StrNotDisabled
    否则: 返回 Success
```

注意两者的判断方向相反：`CheckStrNr` 检查「不能太大」，`CheckStrDisabled` 检查「对应位不能是 1」。

`STRENA`（Stream Enable）是通用寄存器区的一个位图寄存器，bit n 对应流 n 的使能状态；`PsiMsDaq_Str_SetEnable` 就是通过 `RegSetBit(STRENA, 1<<strNr, enable)` 来开关单条流的。

#### 4.2.3 源码精读

[drivers/psi_ms_daq_axi/src/psi_ms_daq.c:L68-L77](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L68-L77) —— `CheckStrDisabled`：读 `STRENA`，按位与判断流是否使能；使能了就返回 `StrNotDisabled`。它用 `SAFE_CALL` 包裹 `RegRead`，读失败会短路向上传错误码。

[drivers/psi_ms_daq_axi/src/psi_ms_daq.c:L79-L87](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L79-L87) —— `CheckStrNr`：把 `ipHandle` 强转回 `PsiMsDaq_Inst_t*`，比较 `streamNr >= maxStreams`。这个 `maxStreams` 是 `PsiMsDaq_Init` 时由调用者传入并固化在实例里的。

`STRENA` 寄存器偏移定义：

[drivers/psi_ms_daq_axi/src/psi_ms_daq.h:L153](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L153) —— `PSI_MS_DAQ_REG_STRENA = 0x020`，通用寄存器区，单字位图。

返回码定义（`StrNotDisabled` 与 `IllegalStrNr`）：

[drivers/psi_ms_daq_axi/src/psi_ms_daq.h:L288-L301](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L288-L301) —— `PsiMsDaq_RetCode_t`：`IllegalStrNr=-1`、`StrNotDisabled=-3`，以及本讲涉及的 `IllegalStrWidth=-2`、`IllegalWinCnt=-4`、`WinSizeMustBeMultipleOfSamples=-10`。

#### 4.2.4 代码实践

1. **实践目标**：理解「配置前必须禁用流」的强制约束如何在代码里落地。
2. **操作步骤**：在参考设计 `main.c` 的 `Init()` 里，找到 `PsiMsDaq_Str_Configure(daqStr0, &cfg0)`（[main.c:L169](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/Sdk/app/src/main.c#L169)），观察它和后续 `PsiMsDaq_Str_SetEnable(daqStr0, true)`（[main.c:L175](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/Sdk/app/src/main.c#L175)）的先后顺序。
3. **需要观察的现象**：配置永远在 `SetEnable` **之前**；这是因为 `PsiMsDaq_Init` 把 `STRENA` 清零（见 u3-l3），刚初始化完所有流天然是禁用的，所以首次配置能通过 `CheckStrDisabled`。
4. **预期结果**：若你在 `SetEnable(true)` 之后再调一次 `Str_Configure`，应返回 `PsiMsDaq_RetCode_StrNotDisabled`（-3）。
5. 待本地验证（可在 ZCU102 上加一行「使能后再次配置」的测试代码观察返回码；纯逻辑阅读亦可推断）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `CheckStrDisabled` 要做成一个独立函数，而不是直接内联在 `Str_Configure` 里？

**参考答案**：因为「流必须禁用」这个前置条件在驱动里多处复用——任何会修改流运行时状态（窗口计数、模式等）的 API 都应先调用它。抽成函数既保证判断逻辑唯一，也便于未来新增 API 时复用同一守卫。

**练习 2**：`CheckStrNr` 比较 `streamNr >= maxStreams` 而不是 `>`，为什么？

**参考答案**：流号从 0 起编号，合法范围是 `0 .. maxStreams-1`，所以 `streamNr == maxStreams` 已经越界，必须用 `>=`。

---

### 4.3 PsiMsDaq_Str_Configure 主体：校验与寄存器写入

#### 4.3.1 概念说明

这是本讲的核心。`PsiMsDaq_Str_Configure` 做三件事，严格按顺序：

1. **三道参数校验**（位宽是 8 的倍数、窗口数不超上限、窗口大小是采样倍数）+ 一道状态校验（流必须禁用）。
2. **把七个配置值写进硬件寄存器**（用直写 `RegWrite` 或字段/比特助手 `RegSetField`/`RegSetBit`）。
3. **把其中五个值同步缓存进流实例结构体**。

三个细节值得提前点破：

- **`winCnt` 减 1 编码**：硬件 `SCFG.WINCNT` 字段按「从 0 起编号」存储窗口数，所以 3 个窗口要写 `3-1=2`。这一点 u3-l1 已提过窗口数编码，这里看到它在配置函数里的具体落点。
- **同一寄存器的多次 RMW 不会互相覆盖**：`SCFG` 一个字里塞了 `RINGBUF`(bit0)、`OVERWRITE`(bit8)、`WINCNT`[20:16] 三个独立配置。`Str_Configure` 对 `SCFG` 连续做了三次写（两次 `RegSetBit`、一次 `RegSetField`），因为都是 RMW（先读回全字、只改目标位、再写回），三者叠加而非覆盖，最终 `SCFG` 是三段配置的合并结果。
- **校验顺序有讲究**：先做纯本地、不访问硬件的算术校验（位宽、窗口数、窗口大小），最后才做需要读 `STRENA` 寄存器的 `CheckStrDisabled`。这样对一份明显非法的配置，驱动不必发起任何寄存器读就能拒绝。

#### 4.3.2 核心流程

```
PsiMsDaq_Str_Configure(strHndl, config_p):
    把 strHndl 强转回 PsiMsDaq_StrInst_t*，取出 strNr、ipHandle
    # —— 校验阶段 ——
    若 streamWidthBits % 8 != 0:              返回 IllegalStrWidth
    若 winCnt > maxWindows:                    返回 IllegalWinCnt
    若 winSize % (streamWidthBits/8) != 0:     返回 WinSizeMustBeMultipleOfSamples
    SAFE_CALL(CheckStrDisabled(ipHandle, strNr))   # 读 STRENA
    # —— 写寄存器阶段 ——
    RegWrite(POSTTRIG(strNr), postTrigSamples)
    RegSetField(MODE(strNr), [1:0], recMode)              # RECM 字段
    RegSetBit(SCFG(strNr), RINGBUF, winAsRingbuf)         # SCFG 第 1 次 RMW
    RegSetBit(SCFG(strNr), OVERWRITE, winOverwrite)       # SCFG 第 2 次 RMW
    RegWrite(BUFSTART(strNr), bufStartAddr)
    RegWrite(WINSIZE(strNr), winSize)
    RegSetField(SCFG(strNr), [20:16], winCnt-1)           # SCFG 第 3 次 RMW（减 1 编码）
    # —— 写软件缓存阶段 ——（见 4.4）
    返回 Success
```

`SCFG` 最终值可写成一条按位组合公式（设 `R=winAsRingbuf`, `O=winOverwrite`, `N=winCnt`）：

\[
\texttt{SCFG} = (R \ll 0)\ \|\ (O \ll 8)\ \|\ (((N-1) \ \&\ \texttt{0x1F}) \ll 16)
\]

其中 `0x1F` 是 5 位 `WINCNT` 字段的掩码（[20:16]）。`WINCUR`[28:24] 是硬件维护的「当前窗口」只读字段，配置时不写。

#### 4.3.3 源码精读

[drivers/psi_ms_daq_axi/src/psi_ms_daq.c:L264-L282](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L264-L282) —— 函数开头：指针强转 + 四道校验。注意三个算术校验在前、`CheckStrDisabled` 在最后，且每条都用 `SAFE_CALL` 或直接 `return` 短路。

`SAFE_CALL` 宏的定义（任何子调用返回非 Success 就立刻把该错误码原样返回上层）：

[drivers/psi_ms_daq_axi/src/psi_ms_daq.c:L44-L46](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L44-L46) —— `#define SAFE_CALL(fctCall)`：把 `fctCall` 的返回值存进 `r`，不等于 `Success` 就 `return r`，实现错误短路传播。

[drivers/psi_ms_daq_axi/src/psi_ms_daq.c:L283-L310](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L283-L310) —— 七次寄存器写入。重点看三处对 `SCFG` 的 RMW（行 292-299 与 306-310）如何合并，以及第 310 行 `config_p->winCnt-1` 的减 1 编码。

`SCFG` 各字段定义（印证上面公式里的位位置）：

[drivers/psi_ms_daq_axi/src/psi_ms_daq.h:L164-L170](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L164-L170) —— `RINGBUF=1<<0`、`OVERWRITE=1<<8`、`WINCNT` 为 [20:16]、`WINCUR` 为 [28:24]。

`MODE` 寄存器字段定义（`RECM` 写在 [1:0]）：

[drivers/psi_ms_daq_axi/src/psi_ms_daq.h:L157-L161](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L157-L161) —— `RECM` 占 [1:0]、`ARM` 占 bit8（软件置位）、`REC` 占 bit16（硬件置位，只读）。

`POSTTRIG` / `BUFSTART` / `WINSIZE` 偏移（直写型，整字即值）：

[drivers/psi_ms_daq_axi/src/psi_ms_daq.h:L156](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L156) —— `POSTTRIG(n) = 0x204+0x10*n`。

[drivers/psi_ms_daq_axi/src/psi_ms_daq.h:L171-L172](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L171-L172) —— `BUFSTART(n)=0x1004+0x20*n`、`WINSIZE(n)=0x1008+0x20*n`（CTX 区，步进 0x20）。

`RegSetField` / `RegSetBit` 的 RMW 实现（证明三次 SCFG 写入是合并而非覆盖）：

[drivers/psi_ms_daq_axi/src/psi_ms_daq.c:L754-L771](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L754-L771) —— `PsiMsDaq_RegSetField`：构造掩码 `msk=(1<<(msb+1))-1`，读回全字，清掉目标字段位 `reg &= ~(msk<<lsb)`，再或上新值 `reg |= (value&msk)<<lsb`，整字写回。

[drivers/psi_ms_daq_axi/src/psi_ms_daq.c:L790-L805](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L790-L805) —— `PsiMsDaq_RegSetBit`：读回全字，清掩码位，按 `value` 决定是否置位，整字写回。

#### 4.3.4 代码实践（本讲主实践）

1. **实践目标**：对照参考设计 `main.c` 中的 `cfg0`，手算 `PsiMsDaq_Str_Configure(daqStr0, &cfg0)` 对 stream 0 写出的全部（寄存器偏移, 值）序列，并确定 `winCnt` 字段最终写入值。
2. **给定输入**（[main.c:L159-L168](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/Sdk/app/src/main.c#L159-L168)）：`postTrigSamples=5`、`recMode=Continuous(=0)`、`winAsRingbuf=true`、`winOverwrite=false`、`winCnt=3`、`bufStartAddr=0x40000000`、`winSize=32`、`streamWidthBits=16`；`strNr=0`，且 `Init()` 后流 0 未使能。
3. **操作步骤**：
   - 先核对三道校验：`16%8==0`✓、`3<=16`✓、`32%(16/8)==32%2==0`✓、流 0 未使能✓，全部通过。
   - 按 [psi_ms_daq.c:L283-L310](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L283-L310) 的顺序，逐条算出地址（代入 `n=0`）和值。
4. **需要观察的现象**：`SCFG(0)` 被写了三次，但因为是 RMW，三段配置叠加；`winCnt` 写入的是 `3-1`。
5. **预期结果（答案）**：

   | 序号 | 调用 | 寄存器偏移（n=0） | 写入的整字值 / 字段语义 |
   |---|---|---|---|
   | 1 | `RegWrite(POSTTRIG(0), 5)` | `0x204` | `0x00000005` |
   | 2 | `RegSetField(MODE(0), [1:0], 0)` | `0x208` | `RECM=0`(Continuous)；整字为 RMW 结果，复位值 0 时即 `0x00000000` |
   | 3 | `RegSetBit(SCFG(0), RINGBUF, true)` | `0x1000` | RMW 置 bit0 → `0x00000001` |
   | 4 | `RegSetBit(SCFG(0), OVERWRITE, false)` | `0x1000` | RMW 清 bit8（本就为 0）→ 仍 `0x00000001` |
   | 5 | `RegWrite(BUFSTART(0), 0x40000000)` | `0x1004` | `0x40000000` |
   | 6 | `RegWrite(WINSIZE(0), 32)` | `0x1008` | `0x00000020` |
   | 7 | `RegSetField(SCFG(0), [20:16], 3-1=2)` | `0x1000` | RMW 写 WINCNT=2 → `0x00020001` |

   **`winCnt` 字段最终写入值 = `winCnt - 1 = 3 - 1 = 2`**（寄存器按「从 0 起编号」存窗口数，2 代表 3 个窗口）。

   `SCFG(0)` 最终整字 `0x00020001` 的语义：bit0 `RINGBUF=1`、bit8 `OVERWRITE=0`、[20:16] `WINCNT=2`。

6. 待本地验证（可在裸机调试时用 `PsiMsDaq_RegRead(ip, 0x1000, &v)` 回读 `SCFG(0)` 验证等于 `0x00020001`；表中第 2 行 `MODE` 的整字值依赖硬件复位值，确定无疑的只是 `RECM[1:0]=0`）。

#### 4.3.5 小练习与答案

**练习 1**：如果用户把 `streamWidthBits` 设成 12（不是 8 的倍数），`Str_Configure` 会返回哪个码？在第几道校验失败？

**参考答案**：返回 `PsiMsDaq_RetCode_IllegalStrWidth`（-2），在第一道校验 `streamWidthBits % 8 != 0` 处失败（[psi_ms_daq.c:L273-L275](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L273-L275)）。

**练习 2**：如果 `winSize=33`、`streamWidthBits=16`，会发生什么？

**参考答案**：`winSize % (streamWidthBits/8) = 33 % 2 = 1 != 0`，返回 `PsiMsDaq_RetCode_WinSizeMustBeMultipleOfSamples`（-10）。这条校验保证窗口边界永远落在整采样上，否则窗口回读时按采样寻址会错位。

**练习 3**：为什么把 `winCnt` 写进 `SCFG` 用的是 `RegSetField`（RMW），而 `bufStart` 用的是 `RegWrite`（直写整字）？

**参考答案**：`SCFG` 一个字里挤了 `RINGBUF/OVERWRITE/WINCNT/WINCUR` 多个字段，必须用 RMW 只改 `WINCNT` 而保留其余字段；`BUFSTART` 是独占整字的寄存器，没有别的字段需要保留，所以直写整字最简单高效。

---

### 4.4 软件状态缓存：配置「写两遍」的设计

#### 4.4.1 概念说明

仔细看 `Str_Configure` 的最后几行，你会发现它在写完硬件寄存器之后，又把一部分参数存进了流实例结构体 `PsiMsDaq_StrInst_t`。这就是「配置写两遍」：一遍给硬件（寄存器），一遍给软件（缓存）。

为什么要缓存？因为后续的窗口回读 API（如 `PsiMsDaq_StrWin_GetDataUnwrapped`，属于 u4-l3）需要这些参数来算字节地址和采样数，但它们**既不在硬件寄存器里能方便地读回，也不该要求用户每次调用都重新传一遍**。比如：

- `widthBytes`（由 `streamWidthBits/8` 算出）：回读时要把「采样数」换算成「字节数」。
- `bufStart`、`winSize`：回读时要算某个窗口的 DDR 起始地址 `bufStart + winSize*winNr`。
- `postTrig`：回读时要由「窗口总采样数」反推「触发前采样数 = 总数 - postTrig」。
- `windows`（即 `winCnt`）：`CheckWinNr` 要用它判断窗口号是否越界。
- `isConfigured`：标记这条流已被合法配置过（供其他 API 做前置检查）。

注意一个不对称：**`recMode`、`winAsRingbuf`、`winOverwrite` 没有被缓存**。它们只写进了硬件寄存器，因为回读路径不需要——`recMode` 只影响硬件的触发/录制行为，`winOverwrite` 影响硬件覆盖策略与可用的中断方案（其约束在 `SetIrqCallbackWin/Str` 里用「两种方案互斥」间接体现，而非靠回读 overwrite 值），`winAsRingbuf` 同理。缓存的是「地址/采样计算要用到的几何参数」，不缓存的是「纯硬件行为策略」。

#### 4.4.2 核心流程

寄存器写完后的缓存更新（[psi_ms_daq.c:L311-L317](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L311-L317)）：

```
inst_p->widthBytes   = streamWidthBits / 8
inst_p->isConfigured = true
inst_p->windows      = winCnt            # 注意：缓存的是原始 winCnt，不是 winCnt-1
inst_p->bufStart     = bufStartAddr
inst_p->postTrig     = postTrigSamples
inst_p->winSize      = winSize
```

一个容易踩的细节：硬件寄存器 `SCFG.WINCNT` 存的是 `winCnt-1`（减 1 编码），但**软件缓存 `inst_p->windows` 存的是原始 `winCnt`**（不减 1）。两者表示同一个意思——「这条流配了几个窗口」——但数值差 1，因为硬件字段从 0 起编号、软件计数从 1 起更自然。`CheckWinNr` 用的是软件缓存的 `windows`，所以 `winNr >= windows` 即越界（合法范围 `0 .. winCnt-1`），与硬件字段语义自洽。

#### 4.4.3 源码精读

[drivers/psi_ms_daq_axi/src/psi_ms_daq.c:L311-L317](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L311-L317) —— 「Set data structure values」段：把五个几何参数 + `isConfigured` 写进流实例。

被写入的流实例结构体字段定义：

[drivers/psi_ms_daq_axi/src/psi_ms_daq.c:L13-L27](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L13-L27) —— `PsiMsDaq_StrInst_t`：可见 `widthBytes`(L16)、`windows`(L17)、`bufStart`(L24)、`winSize`(L25)、`postTrig`(L26) 这五个被缓存的几何字段，以及 `isConfigured`(L15)。对比可知没有 `recMode/ringbuf/overwrite` 字段。

`CheckWinNr` 如何消费缓存的 `windows`：

[drivers/psi_ms_daq_axi/src/psi_ms_daq.c:L89-L97](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L89-L97) —— `CheckWinNr`：`winNr >= inst_p->windows` 即返回 `IllegalWinNr`。这里的 `windows` 正是 `Str_Configure` 缓存进去的 `winCnt`。

#### 4.4.4 代码实践

1. **实践目标**：看清「软件缓存」如何让窗口回读 API 的签名保持简洁。
2. **操作步骤**：打开 `PsiMsDaq_StrWin_GetDataUnwrapped` 的签名（[psi_ms_daq.h:L552-L556](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L552-L556)），注意它只接收 `winInfo`（窗口号 + 两个句柄）、`preTrigSamples`、`postTrigSamples`、`buffer`、`bufferSize`——**不接收** `widthBytes/bufStart/winSize/postTrig`。
3. **需要观察的现象**：函数内部（[psi_ms_daq.c:L607](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L607)）用 `str_p->bufStart + str_p->winSize*winInfo.winNr` 算窗口起始地址——这些 `str_p->` 字段正是 `Str_Configure` 缓存进去的。
4. **预期结果**：你能解释「为什么用户回读数据时不用再传一遍位宽和缓冲区地址」——因为它们已在配置时被驱动缓存。
5. 待本地验证（阅读型实践；具体回读逻辑在 u4-l3 详讲）。

#### 4.4.5 小练习与答案

**练习 1**：`inst_p->windows` 存的是 `winCnt` 还是 `winCnt-1`？为什么和硬件寄存器 `SCFG.WINCNT` 不一样？

**参考答案**：存原始 `winCnt`（不减 1）。硬件 `SCFG.WINCNT` 字段按「从 0 起编号」编码（3 个窗口写 2），软件 `windows` 按「自然计数」存（3 个窗口存 3）。两者都合法，只是各自场合的惯例不同；`CheckWinNr` 用软件值 `winNr >= windows` 判越界，与硬件字段语义一致。

**练习 2**：如果以后想新增一个 API「读回当前流的位宽」，应该从硬件寄存器读，还是从软件缓存读？

**参考答案**：从软件缓存 `str_p->widthBytes` 读。因为位宽没有运行时硬件寄存器（它固化在 Vivado 泛型里），运行时唯一记录处就是 `Str_Configure` 写入的这份缓存。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「纸上配置 + 反向核对」：

**任务**：假设你要给 stream 1 配置一个「线性缓冲、不允许覆盖、4 个窗口、位宽 32-bit、窗口 64 字节、触发后录 10 个采样、缓冲区起始 `0x50000000`」的流。请：

1. 写出对应的 `PsiMsDaq_StrConfig_t cfg` 初始化（C 代码）。
2. 列出 `PsiMsDaq_Str_Configure(daqStr1, &cfg)` 对 stream 1（`strNr=1`）的（寄存器偏移, 值）写入序列，并算出 `SCFG(1)` 最终整字值与 `winCnt` 字段写入值。
3. 列出会被写进 `PsiMsDaq_StrInst_t` 的软件缓存字段及其值。

**参考答案**：

1. 配置结构体：

   ```c
   PsiMsDaq_StrConfig_t cfg = {
       .postTrigSamples = 10,
       .recMode         = PsiMsDaqn_RecMode_Continuous, // 按需
       .winAsRingbuf    = false,    // 线性缓冲
       .winOverwrite    = false,
       .winCnt          = 4,
       .bufStartAddr    = 0x50000000,
       .winSize         = 64,
       .streamWidthBits = 32
   };
   ```

2. 校验：`32%8==0`✓、`4<=maxWindows`（假设 maxWindows≥4）✓、`64%(32/8)==64%4==0`✓、流 1 未使能✓。写入序列（`n=1`，注意逐流录制区步进 `0x10`、CTX 区步进 `0x20`）：

   | 调用 | 偏移 | 整字值 |
   |---|---|---|
   | `RegWrite(POSTTRIG(1), 10)` | `0x204+0x10 = 0x214` | `0x0000000A` |
   | `RegSetField(MODE(1), [1:0], 0)` | `0x208+0x10 = 0x218` | `RECM=0`（整字为 RMW 结果） |
   | `RegSetBit(SCFG(1), RINGBUF, false)` | `0x1000+0x20 = 0x1020` | RMW 清 bit0 → `0x00000000` |
   | `RegSetBit(SCFG(1), OVERWRITE, false)` | `0x1020` | RMW 清 bit8 → `0x00000000` |
   | `RegWrite(BUFSTART(1), 0x50000000)` | `0x1004+0x20 = 0x1024` | `0x50000000` |
   | `RegWrite(WINSIZE(1), 64)` | `0x1008+0x20 = 0x1028` | `0x00000040` |
   | `RegSetField(SCFG(1), [20:16], 4-1=3)` | `0x1020` | RMW 写 WINCNT=3 → `0x00030000` |

   `SCFG(1)` 最终整字 `0x00030000`：`RINGBUF=0`、`OVERWRITE=0`、`WINCNT=3`。**`winCnt` 字段写入值 = `4-1 = 3`**。

3. 软件缓存：`widthBytes = 32/8 = 4`、`isConfigured = true`、`windows = 4`、`bufStart = 0x50000000`、`postTrig = 10`、`winSize = 64`。

## 6. 本讲小结

- `PsiMsDaq_StrConfig_t` 八个字段里，七个落到硬件寄存器（`POSTTRIG/MODE/SCFG/BUFSTART/WINSIZE`），唯独 `streamWidthBits` 不对应运行时寄存器，只用于校验和换算成 `widthBytes` 缓存。
- `Str_Configure` 严格按「三道算术校验 → 一道状态校验 → 写寄存器 → 写缓存」执行；算术校验在前、读寄存器的 `CheckStrDisabled` 在后，非法输入不必发起任何总线读即可被拒绝。
- 配置前流必须禁用（`CheckStrDisabled` 读 `STRENA`），强制「先停后改」，避免硬件写地址生成器在运行中被改参数。
- `SCFG` 一个字塞了 `RINGBUF/OVERWRITE/WINCNT` 三个配置，靠 `RegSetBit/RegSetField` 的 RMW 三次叠加合并，最终值是三段配置的按位组合。
- `winCnt` 写入硬件时减 1（`winCnt-1`，从 0 起编号），但软件缓存 `windows` 存原始 `winCnt`；`CheckWinNr` 用缓存值判越界，两者语义自洽。
- 「配置写两遍」：几何参数（`widthBytes/windows/bufStart/winSize/postTrig` + `isConfigured`）被缓存，让窗口回读 API 不必重复传参；纯策略参数（`recMode/ringbuf/overwrite`）只写硬件、不缓存。

## 7. 下一步学习建议

配置只是「把流武装起来」，真正读数据要等触发和中断到来。建议按以下顺序继续：

1. **u4-l1 录制模式与窗口/环形缓冲概念**：搞清 `recMode` 四种模式的触发行为、`Arm` 位时机、环形 vs 线性缓冲对数据布局的影响——这些是配置字段 `recMode/winAsRingbuf` 背后的语义。
2. **u4-l2 中断处理：窗口式 vs 流式**：理解 `winOverwrite=false`（本讲 cfg0 的设置）为何对应窗口式 IRQ，以及 `SetIrqCallbackWin/Str` 的互斥校验。
3. **u4-l3 窗口数据回读与去环绕回拷贝**：直接消费本讲缓存的 `bufStart/winSize/postTrig/widthBytes`，看它们如何参与字节地址计算。
4. 若想再往下钻底层工具，可读 **u4-l4 寄存器字段/比特助手、SAFE_CALL 与返回码**，把本讲用到的 `RegSetField/RegSetBit/SAFE_CALL` 的 RMW 与短路机制彻底弄透。
