# 驱动数据读取与环形缓冲解包

## 1. 本讲目标

学完本讲后，读者应该能够：

- 说清 `PsiMsDaq_StrWin_GetDataUnwrapped()`（以下简称 `GetDataUnwrapped`）解决的是什么问题：把硬件以**环形（ring buffer）方式**写入 DDR 一个窗口的数据，**按时间顺序**搬成一段连续内存。
- 复述四个辅助函数 `GetLastSplAddr` / `GetNoOfSamples` / `GetPreTrigSamples` / `GetNoOfBytes` 各自从上下文 RAM 读什么、返回什么。
- 手工推导触发字节地址 `trigByteAddr`、末字节地址 `lastByteAddr` 在窗口内的**模运算回绕**过程。
- 判断一次读取会落在「单段直拷」还是「两段拼接」分支，并写出两段 `memcpy` 的源地址与长度。
- 解释 `preTrigSamples` / `postTrigSamples` 与窗口 `winSize`、`streamWidthBytes` 的关系，以及各种越界情况对应的返回码。

本讲只涉及 C 驱动 `driver/psi_ms_daq.c` 与 `driver/psi_ms_daq.h`，是 [u4-l2（驱动中断处理与窗口回调）](u4-l2-driver-irq-window-callback.md) 的直接后续：u4-l2 讲「中断来了怎么把窗口派发给回调」，本讲讲「回调里拿到窗口后，怎么把这一窗数据正确地拷出来」。

## 2. 前置知识

在进入源码前，先用通俗语言把几个概念讲清楚。

### 2.1 为什么要「解包（unwrap）」

DMA 引擎把样本写入 DDR 时是**按地址递增**写的。当一个窗口被配置成环形缓冲（`winAsRingbuf = true`）时，写指针写到窗口末尾会**绕回窗口起点**继续覆盖写。于是在物理地址空间里，一窗数据的**时间顺序**和**地址顺序**不一致：

- 地址靠近窗口**末尾**的样本是**较早**写入的（旧的）；
- 地址靠近窗口**起点**的样本是**较晚**写入的（新的，刚绕回来覆盖写的）。

软件通常希望拿到一段**按时间从旧到新排列**的连续数组，方便直接做 FFT、绘图等处理。`memcpy` 不会「绕弯」，所以驱动必须自己判断读取区间是否横跨了「窗口末尾 → 窗口起点」这一回绕边界；若横跨，就拆成两段拷贝再拼起来——这就是「unwrap」。

> 提示：线性缓冲（`winAsRingbuf = false`）模式下，一个窗口写满即封口，写指针不会在窗口内部回绕，因此读取区间不会跨边界，必然走「单段直拷」分支。两段拼接分支本质上是**为环形缓冲准备的**。

### 2.2 触发、前触发、后触发

DAQ IP 的触发型记录（TriggerMask / SingleShot）以一个「触发样本」为锚点：

- **后触发样本数（postTrig）**：**包含触发样本本身**在内，触发之后记录多少个样本（见头文件注释 [driver/psi_ms_daq.h:266](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L266)）。所以触发样本是「第 0 个后触发样本」。
- **前触发样本数（preTrig）**：触发之前已经环形记录在窗口里的样本数，**运行时由硬件统计**，软件通过 `GetPreTrigSamples()` 读取。

一次触发结束记录后，窗口里**最后写入的样本**就是最后一个后触发样本。硬件把这个样本的首字节地址存进上下文 RAM 的 `WIN_LAST` 字段（见 4.3 节的硬件来源），驱动读出来作为推导整段数据位置的「锚」。

### 2.3 每个样本占多少字节

`streamWidthBits` 必须是 8 的整数倍（配置时校验，见 [driver/psi_ms_daq.c:273-275](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L273-L275)），配置时换算成字节存入流实例结构体：

```c
inst_p->widthBytes = config_p->streamWidthBits/8;   // 例如 16 位 -> 2 字节
```

本讲下文用 \( w \) 表示 `widthBytes`，用 \( S \) 表示 `winSize`（字节）。

### 2.4 窗口上下文里有什么

回顾 [u3-l4（上下文存储模型）](u3-l4-context-memory-model.md)：每个「流 × 窗口」在上下文 RAM 里有一组窗口上下文，其中与本讲直接相关的两个字段是：

- `WINCNT[30:0]`：本窗口已写入的**样本数**（注意硬件存的是样本数，不是字节数）。
- `WINCNT[31]`：本窗口是否**由触发结束**（`IsTrig` 位）。
- `WIN_LAST`：本窗口**最后写入样本的首字节地址**。

寄存器宏定义见 [driver/psi_ms_daq.h:176-182](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L176-L182)。`GetDataUnwrapped` 要求窗口必须包含触发（否则不知道数据锚点在哪），这一前提由 `GetPreTrigSamples` 内部检查 `IsTrig` 位来强制。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [driver/psi_ms_daq.c](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c) | 驱动实现。本讲主角 `GetDataUnwrapped` 与四个辅助函数都在此。 |
| [driver/psi_ms_daq.h](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h) | 类型与返回码定义：`PsiMsDaq_WinInfo_t`、`PsiMsDaq_StrConfig_t`、`PsiMsDaq_RetCode_t`、`PsiMsDaq_DataCopy_f`。 |
| hdl/psi_ms_daq_daq_sm.vhd | （仅引用一行）确认硬件如何计算 `WIN_LAST`，作为 `lastSplAddr` 语义的依据。 |

数据读取相关的函数在 `.c` 文件里的位置一览：

- `GetNoOfBytes`：[driver/psi_ms_daq.c:496-507](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L496-L507)
- `GetNoOfSamples`：[driver/psi_ms_daq.c:509-528](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L509-L528)
- `GetPreTrigSamples`：[driver/psi_ms_daq.c:530-551](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L530-L551)
- **`GetDataUnwrapped`（本讲核心）**：[driver/psi_ms_daq.c:579-639](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L579-L639)
- `GetLastSplAddr`：[driver/psi_ms_daq.c:656-667](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L656-L667)

## 4. 核心概念与源码讲解

### 4.1 辅助函数：从上下文 RAM 读窗口元数据

#### 4.1.1 概念说明

`GetDataUnwrapped` 不直接访问硬件寄存器去拼地址，而是先调用三个「读取元数据」的辅助函数拿到三件事：

1. **本窗口共有多少样本**（`GetNoOfSamples`）——用来推算「可用的前触发样本数」。
2. **本窗口可用的前触发样本数**（`GetPreTrigSamples`）——用来校验用户请求是否超过实际存量；同时它内部检查 `IsTrig`，**强制窗口必须含触发**。
3. **最后写入样本的首字节地址**（`GetLastSplAddr`）——作为推导整段数据地址的锚点。

外加一个便利函数 `GetNoOfBytes`，把样本数换算成字节数。

#### 4.1.2 核心流程

三个函数都遵循同一套套路：指针cast →（可能的）检查 → 用 `PsiMsDaq_RegGetField` / `PsiMsDaq_RegGetBit` / `PsiMsDaq_RegRead` 读上下文 RAM 的某个字段 → 返回。

#### 4.1.3 源码精读

**`GetNoOfSamples`** 直接读 `WINCNT[0:30]` 这 31 位字段，它存的就是样本数：

```c
SAFE_CALL(PsiMsDaq_RegGetField( winInfo.ipHandle,
                PSI_MS_DAQ_WIN_WINCNT(strNr, winInfo.winNr, ip_p->strAddrOffs),
                PSI_MS_DAQ_WIN_WINCNT_LSB_CNT,   // 0
                PSI_MS_DAQ_WIN_WINCNT_MSB_CNT,   // 30
                noOfSamples_p));
```

> [driver/psi_ms_daq.c:521-525](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L521-L525) 读取窗口样本数。硬件侧写入时已把字节数右移换算成样本数（`shift_right(HndlWinBytes, Log2StrBytes)`，见 hdl 状态机），所以这里读出来直接是样本数，无需再除。

**`GetPreTrigSamples`** 先读 `WINCNT` 的最高位 `IsTrig`；若该窗口不是由触发结束，直接返回 `NoTrigInWin`，否则用「总样本数 − 配置的后触发样本数」得到前触发样本数：

```c
SAFE_CALL(PsiMsDaq_RegGetBit( ..., PSI_MS_DAQ_WIN_WINCNT_BIT_ISTRIG, &containsTrig));
if (!containsTrig) {
    return PsiMsDaq_RetCode_NoTrigInWin;          // -6
}
uint32_t samples;
SAFE_CALL(PsiMsDaq_StrWin_GetNoOfSamples(winInfo, &samples));
*preTrigSamples_p = samples - str_p->postTrig;     // str_p->postTrig 是配置时存下的值
```

> [driver/psi_ms_daq.c:538-548](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L538-L548)。注意 `str_p->postTrig` 是流配置时（[driver/psi_ms_daq.c:316](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L316)）缓存进驱动的「配置后触发样本数」，与用户每次调用 `GetDataUnwrapped` 时传入的「请求后触发样本数」是两个不同概念，别混淆。

**`GetLastSplAddr`** 直接读 `WIN_LAST` 寄存器（整 32 位）：

```c
SAFE_CALL(PsiMsDaq_RegRead( winInfo.ipHandle,
            PSI_MS_DAQ_WIN_LAST(strNr, winInfo.winNr, ip_p->strAddrOffs), lastSplAddr_p));
```

> [driver/psi_ms_daq.c:664](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L664)。返回值 `lastSplAddr` 是**最后写入样本的首字节地址**。其硬件来源可在状态机里一行确认：

```vhdl
-- Store address of last sample in window
v.HndlWinLast := std_logic_vector(unsigned(r.HndlPtr1) - StreamWidth_g(r.HndlStream) / 8);
```

> [hdl/psi_ms_daq_daq_sm.vhd:498-499](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L498-L499)。`HndlPtr1` 是「写完本次传输后、指向下一个可写字节」的指针，减去一个样本的字节数 (`StreamWidth/8`)，正好得到最后一个样本的首字节地址。

**`GetNoOfBytes`** 只是把样本数乘以 `widthBytes`：`*noOfBytes_p = samples*str_p->widthBytes;`（[driver/psi_ms_daq.c:504](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L504)）。

#### 4.1.4 代码实践

**实践目标**：验证 `WINCNT` 字段在硬件侧确实以「样本数」而非「字节数」存放，理解 `GetPreTrigSamples` 的算术。

**操作步骤**：

1. 打开 [driver/psi_ms_daq.c:509-528](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L509-L528) 与 [driver/psi_ms_daq.c:530-551](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L530-L551)。
2. 假设一个 16 位流（\( w=2 \)）、配置 `postTrigSamples=5`、某窗口硬件写入了 18 个样本且由触发结束。
3. 手算：`WINCNT[30:0]` 应读出 18，`IsTrig` 位 = 1，`GetPreTrigSamples` 返回 \( 18-5=13 \)。

**需要观察的现象 / 预期结果**：

- 若该窗口并非触发结束（例如纯连续记录的一窗），调用 `GetPreTrigSamples`（进而 `GetDataUnwrapped`）应返回 `-6 (NoTrigInWin)`，因为无锚点可解包。
- 待本地验证：在真实硬件上读 `WINCNT` 寄存器，确认低位是样本数（与 `winSize/widthBytes` 量级一致，而不是字节数）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `GetNoOfSamples` 读出来的值不需要再除以 `widthBytes`？
**答案**：因为硬件在把 `HndlWinBytes` 写入 `WINCNT` 时已经做了 `shift_right(..., Log2StrBytes)`（按样本宽度右移），存的就是样本数；驱动再除一次就错了。

**练习 2**：一个窗口 `WINCNT` 读出 `0x80000009`（最高位 1，低位 9），配置 `postTrig=4`。`GetNoOfSamples` 与 `GetPreTrigSamples` 分别返回什么？
**答案**：`IsTrig=1`，样本数 = 9；前触发样本数 = \( 9-4=5 \)。

---

### 4.2 GetDataUnwrapped：地址模型与越界校验

#### 4.2.1 概念说明

`GetDataUnwrapped` 的输入是：用户想读多少**前触发**样本（`preTrigSamples`）和多少**后触发**样本（`postTrigSamples`，含触发样本），以及一个目标缓冲。它的职责是在环形窗口里定位出这 \( \text{preTrigSamples}+\text{postTrigSamples} \) 个样本对应的**字节区间**，再把这个区间拷成连续内存。

整个函数可以清晰地分成四段：① 算总字节数并取可用前触发数 → ② 越界校验 → ③ 算窗口地址与锚点（带回绕）→ ④ 按是否跨边界选拷贝方式。本节讲 ①②，下两节讲 ③④。

#### 4.2.2 核心流程

```
samples = preTrigSamples + postTrigSamples
bytes   = samples * widthBytes
preTrig = GetPreTrigSamples(winInfo)          # 同时强制窗口含触发
---- 校验 ----
bufferSize      < bytes        -> BufferTooSmall              (-7)
postTrigSamples > postTrig配置 -> MorePostTrigThanConfigured  (-8)
preTrigSamples  > preTrig可用  -> MorePreTrigThanAvailable    (-9)
```

#### 4.2.3 源码精读

总字节数与「可用前触发数」的计算，注意 `GetPreTrigSamples` 被包在 `SAFE_CALL` 里——若窗口不含触发，这里就直接带错误码返回了：

```c
const uint32_t samples = preTrigSamples+postTrigSamples;
const uint32_t bytes   = samples*str_p->widthBytes;
uint32_t preTrig;
SAFE_CALL(PsiMsDaq_StrWin_GetPreTrigSamples(winInfo, &preTrig));
```

> [driver/psi_ms_daq.c:590-593](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L590-L593)。

三个越界检查（顺序即源码顺序）：

```c
if (bufferSize < bytes)                       return PsiMsDaq_RetCode_BufferTooSmall;             // -7
if (postTrigSamples > str_p->postTrig)        return PsiMsDaq_RetCode_MorePostTrigThanConfigured; // -8
if (preTrigSamples  > preTrig)                return PsiMsDaq_RetCode_MorePreTrigThanAvailable;   // -9
```

> [driver/psi_ms_daq.c:596-604](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L596-L604)。返回码定义见 [driver/psi_ms_daq.h:288-301](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L288-L301)。

三个检查的语义要点：

| 检查 | 比较 | 含义 |
|------|------|------|
| `BufferTooSmall` (-7) | 用户缓冲 `bufferSize` < 实际要拷的 `bytes` | 缓冲装不下，防止越界写 |
| `MorePostTrigThanConfigured` (-8) | 请求后触发 > **配置**后触发 `str_p->postTrig` | 硬件根本没记录那么多后触发样本 |
| `MorePreTrigThanAvailable` (-9) | 请求前触发 > **可用**前触发 `preTrig` | 触发前来不及存下这么多样本 |

> 注意区分两个 postTrig：检查里的 `str_p->postTrig` 是**配置时**写进驱动的、IP 实际记录的后触发样本数上限；参数 `postTrigSamples` 是**本次调用**想要读出来的。允许只读一部分（请求 ≤ 配置）。

#### 4.2.4 代码实践

**实践目标**：熟悉四种返回码的触发条件。

**操作步骤**：阅读 [driver/psi_ms_daq.c:590-604](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L590-L604)，假设一个 16 位流、配置 `postTrigSamples=10`、某窗口可用前触发 `preTrig=20`。

**需要观察的现象 / 预期结果**（待本地验证）：

- 调用 `GetDataUnwrapped(win, 20, 10, buf, 30)`：`bytes=(20+10)*2=60 > 30` → 返回 `-7`。
- 调用 `GetDataUnwrapped(win, 5, 12, buf, 1024)`：`postTrigSamples=12 > 配置10` → 返回 `-8`。
- 调用 `GetDataUnwrapped(win, 25, 10, buf, 1024)`：`preTrigSamples=25 > 可用20` → 返回 `-9`。
- 调用 `GetDataUnwrapped(win, 20, 10, buf, 1024)`：全部通过 → 进入地址计算。

#### 4.2.5 小练习与答案

**练习**：用户配置 `postTrigSamples=8`，调用时传 `postTrigSamples=8, preTrigSamples=3`，缓冲足够大，但该窗口实际只有 2 个前触发样本可用。会返回什么？
**答案**：前两个检查（缓冲、后触发）都通过；第三项 `preTrigSamples(3) > preTrig(2)` 成立，返回 `-9 (MorePreTrigThanAvailable)`。

---

### 4.3 trigByteAddr / lastByteAddr 的环形回绕计算

#### 4.3.1 概念说明

校验通过后，函数要把「用户想读的那段数据」在 DDR 里的**字节区间**算出来。区间用两个端点描述：

- **`lastByteAddr`**：要拷出的数据的**最后一个字节**地址（最新样本的末字节）。
- **`firstByteLinear`**：要拷出的数据的**第一个字节**地址（最旧样本的首字节），用带符号 64 位算，便于检测它是否「掉到窗口起点之下」（即发生回绕）。

由于环形缓冲里地址会绕，这两个端点都不能简单加减，而要用「窗口内的模运算」修正。源码用一个中间量 `trigByteAddr` 作为跳板。

#### 4.3.2 核心流程

记窗口起点 `winStart = bufStart + winSize*winNr`，窗口末字节 `winLast = winStart + winSize - 1`，样本宽度 \( w \)。先把窗口看作一个长度为 \( S \)（= `winSize`）的环，所有地址都在绝对区间 \([\text{winStart}, \text{winStart}+S-1]\) 内，绕回规则是「越界一次就 ±S」。

锚点是 `lastSplAddr`（最后样本首字节）。由此向前减去「配置后触发样本数 × 字节宽」得到 `trigByteAddr`（一个工作变量，落在触发样本前一个样本宽处），若减出窗口下界就 +S 绕回；再向后加上「请求后触发样本数 × 字节宽 + 一个样本宽 − 1」得到 `lastByteAddr`（要拷数据的末字节），若超出窗口上界就 −S 绕回。

用偏移坐标 \( x' = x - \text{winStart} \)（取值 \([0, S-1]\)）写更清楚：

\[
\text{trigByteAddr}' = \big(\text{lastSplAddr}' - \text{postTrig}\cdot w\big) \bmod S
\]

\[
\text{lastByteAddr}' = \big(\text{trigByteAddr}' + \text{postTrigSamples}\cdot w + w - 1\big) \bmod S
\]

源码没有用真正的取模，而是用**单次条件加减**实现，因为这两个运算最多只会越界一个窗口宽度（前提：记录的数据能放进窗口，即 \( \text{postTrig}\cdot w \le S \)、\( \text{postTrigSamples}\cdot w \le S \)）。

#### 4.3.3 源码精读

先算窗口起止：

```c
const uint32_t winStart = str_p->bufStart + str_p->winSize*winInfo.winNr;
const uint32_t winLast  = winStart + str_p->winSize - 1;
```

> [driver/psi_ms_daq.c:607-608](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L607-L608)。

取锚点并算 `trigByteAddr`（含一次「下溢 +S」回绕）：

```c
uint32_t lastSplAddr;
SAFE_CALL(PsiMsDaq_StrWin_GetLastSplAddr(winInfo, &lastSplAddr));
uint32_t trigByteAddr = lastSplAddr - str_p->postTrig*str_p->widthBytes;
if (trigByteAddr < winStart) {
    trigByteAddr += str_p->winSize;          // 绕回到窗口末尾侧
}
```

> [driver/psi_ms_daq.c:611-616](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L611-L616)。这里有个易错点值得讲清：`trigByteAddr` 是 `uint32_t`，而 `lastSplAddr - postTrig*w` 是**无符号减法**，为何不会因下溢而出错？关键在于 `lastSplAddr` 是一个**绝对 DDR 地址**（例如 `bufStart + 偏移`，是个很大的数），而 `postTrig*w` 相对很小。又因记录的数据必须放得进窗口（`postTrig*w ≤ S = winSize`）且 `lastSplAddr ∈ [winStart, winLast]`，故真实差值最小只到 `winStart - S`——仍是一个很大的正数，**不会让 uint32 下溢到补码大数**。于是差值落在 \([\text{winStart}-S,\ \text{winLast}]\)，`< winStart` 这一次比较恰好抓住「掉到窗口下界以下（最多一个窗口宽）」的情形，再 `+= S` 把它绕回 \([\text{winStart}, \text{winLast}]\)。一句话：单次条件加减等价于模 \( S \)，前提是数据量不超过一个窗口。

由 `trigByteAddr` 算 `lastByteAddr`（含一次「上溢 −S」回绕）：

```c
uint32_t lastByteAddr = trigByteAddr + postTrigSamples*str_p->widthBytes + str_p->widthBytes-1;
if (lastByteAddr > winLast) {
    lastByteAddr -= str_p->winSize;          // 绕回到窗口起点侧
}
```

> [driver/psi_ms_daq.c:617-620](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L617-L620)。`+w-1` 把 `trigByteAddr`（一个样本首字节性质的位置）延伸到「请求数据末样本的末字节」。当请求量等于配置量时，`lastByteAddr` 恰好等于 `lastSplAddr + w - 1`（最后样本的末字节），可作为正确性的快速自检。

#### 4.3.4 代码实践

**实践目标**：手工跑一遍回绕算术，确认 `lastByteAddr` 落在窗口内。

**操作步骤**：取 \( w=2 \)、`winSize=20`、`winStart=1000`（故 `winLast=1019`）、配置 `postTrig=3`、`lastSplAddr=1002`、请求 `postTrigSamples=3`。

1. `trigByteAddr = 1002 - 3*2 = 996`；`996 < 1000` → `trigByteAddr = 996+20 = 1016`。
2. `lastByteAddr = 1016 + 3*2 + 2 - 1 = 1023`；`1023 > 1019` → `lastByteAddr = 1023-20 = 1003`。

**预期结果**：`lastByteAddr = 1003 = lastSplAddr + w - 1 = 1002+2-1`，确实是最后样本的末字节，落在 \([1000,1019]\) 内。

#### 4.3.5 小练习与答案

**练习 1**：上例中触发样本的首字节地址是多少？
**答案**：触发样本是第 0 个后触发样本，其首字节 = `lastSplAddr - (postTrig-1)*w = 1002 - 2*2 = 998`，绕回 = `998+20 = 1018`，即样本占 \([1018,1019]\)。

**练习 2**：为什么源码用「单次 ±winSize」而不是真正的取模？
**答案**：因为一次记录的数据量必然不超过一个窗口（`postTrig*w ≤ S`），回绕最多跨一个窗口边界，单次条件加减等价于模 S，且更省逻辑。

---

### 4.4 不跨界 vs 跨界：两段 memcpy 拼接

#### 4.4.1 概念说明

有了末字节 `lastByteAddr` 和总字节数 `bytes`，要拷的区间在「逻辑上」是 \([\text{lastByteAddr}-\text{bytes}+1,\ \text{lastByteAddr}]\)。问题是：这个区间的**起点**可能掉到 `winStart` 之下——意味着数据在环形缓冲里横跨了「窗口末尾 → 窗口起点」的回绕缝。

- **不跨界**：起点仍在窗口内 → 一段连续地址，直接一次 `memcpy`。
- **跨界**：起点掉到窗口下 → 数据被回绕缝切成两段：一段贴在窗口**末尾**（旧数据），一段贴在窗口**起点**（新数据）。按时间顺序，应先拷末尾段、再拷起点段，拼成连续缓冲。

#### 4.4.2 核心流程

用 64 位带符号算起点，避免无符号下溢误判：

```
firstByteLinear = (int64_t)lastByteAddr - bytes + 1
if firstByteLinear >= winStart:
        memcpy(dst, firstByteLinear, bytes)                 # 单段直拷
else:                                                       # 跨界，两段
        secondChunkSize = lastByteAddr - winStart + 1        # 贴在起点的较新段
        firstChunkSize  = bytes - secondChunkSize            # 贴在末尾的较旧段
        firstChunkStart = winLast - firstChunkSize + 1
        memcpy(dst,                     firstChunkStart, firstChunkSize)   # 先旧
        memcpy(dst + firstChunkSize,     winStart,       secondChunkSize)  # 后新
```

两段拼接后，`dst` 里就是按时间从旧到新排列的连续样本。

#### 4.4.3 源码精读

分支判定与单段直拷：

```c
const int64_t firstByteLinear = (int64_t)lastByteAddr - bytes + 1;
if (firstByteLinear >= winStart) {
    ip_p->memcpyFct(buffer_p, (void*)(size_t)firstByteLinear, bytes);
}
```

> [driver/psi_ms_daq.c:624-627](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L624-L627)。`lastByteAddr` 与 `bytes` 都是 `uint32_t`，直接相减会无符号下溢；先转 `int64_t` 再减，得到的 `firstByteLinear` 可正可负，与 `winStart` 比较才有意义。源指针 `(void*)(size_t)firstByteLinear` 直接把 IP 视角的 DDR 地址当 CPU 指针用——这依赖「DMA 目标地址空间 == CPU 物理地址空间」（如 Zynq PS DDR），是 `PsiMsDaq_DataCopy_f` 注释里强调的语义（[driver/psi_ms_daq.h:196-201](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L196-L201)）。

两段拼接分支：

```c
else {
    const uint32_t secondChunkSize    = lastByteAddr - winStart + 1;
    const uint32_t firstChunkSize     = bytes - secondChunkSize;
    const int64_t  firstChunkStartAddr= winLast - firstChunkSize + 1;
    ip_p->memcpyFct(buffer_p, (void*)(size_t)firstChunkStartAddr, firstChunkSize);
    ip_p->memcpyFct((void*)((uint32_t)buffer_p+firstChunkSize), (void*)(size_t)winStart, secondChunkSize);
}
```

> [driver/psi_ms_daq.c:629-635](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L629-L635)。要点：
>
> - `secondChunkSize` 是从 `winStart` 到 `lastByteAddr` 的字节数——即贴在**窗口起点**的那段**较新**数据。
> - `firstChunkSize` 是剩下的——贴在**窗口末尾**的那段**较旧**数据。
> - 第一次拷贝 `firstChunk`（旧）到缓冲开头，第二次拷贝 `secondChunk`（新）紧随其后，于是缓冲内时间顺序为「旧 → 新」，完成解包。

数据拷贝走的是注入的 `ip_p->memcpyFct`，默认实现就是标准 `memcpy`（[driver/psi_ms_daq.c:51-54](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L51-L54)）。这与 u1-l4 讲的「访问函数注入」一脉相承：换平台时只需注入带 cache 维护的拷贝函数（如 `Xil_DCacheInvalidateRange` 后再拷），驱动主体不用改。

#### 4.4.4 代码实践（本讲综合手工推导）

**实践目标**：给定一个末字节地址回绕到窗口起点附近的环形窗口，手工推导两段 `memcpy` 的源地址与长度，写出实际调用参数。

**已知条件**：

- 16 位流，\( w=2 \)；`winSize=20` 字节；`winStart=1000`（`winLast=1019`）。
- 配置 `postTrig=3`；窗口可用前触发 `preTrig` 足够大（例如 6，通过校验）。
- 硬件回写 `lastSplAddr=1002`。
- 请求 `preTrigSamples=4`、`postTrigSamples=3`，缓冲足够大。

**操作步骤**（按源码逐步算）：

1. `samples = 4+3 = 7`；`bytes = 7*2 = 14`。校验全过。
2. `winStart=1000`，`winLast=1019`。
3. `trigByteAddr = 1002 - 3*2 = 996 < 1000` → `+20` → `1016`。
4. `lastByteAddr = 1016 + 3*2 + 2 - 1 = 1023 > 1019` → `-20` → `1003`。
5. `firstByteLinear = 1003 - 14 + 1 = 990 < 1000` → **走两段拼接分支**。
6. `secondChunkSize = 1003 - 1000 + 1 = 4`（窗口起点段，较新）。
7. `firstChunkSize = 14 - 4 = 10`（窗口末尾段，较旧）。
8. `firstChunkStartAddr = 1019 - 10 + 1 = 1010`。

**预期结果**：两次 `memcpy`（以注入函数 `memcpyFct(dst, src, n)` 记）参数为：

```c
memcpyFct(buffer,       (void*)1010, 10);   // 拷窗口 [1010..1019]：较旧的 5 个前触发样本
memcpyFct(buffer + 10,  (void*)1000,  4);   // 拷窗口 [1000..1003]：较新的触发后 2 个样本
```

拼好后 `buffer` 的 14 字节按时间顺序为：

| buffer 偏移 | 源地址 | 内容 |
|-------------|--------|------|
| 0–9   | 1010–1019 | 前 4 个前触发样本 + 触发样本（[1018,1019]） |
| 10–13 | 1000–1003 | 触发后第 1、第 2 个样本（[1002,1003] 即最后样本） |

触发样本落在 `[1018,1019]`（buffer 偏移 8–9），整段 7 个样本时间顺序正确，回绕缝被「缝合」。

> 待本地验证：可在 testbench 里构造同样的环形回绕场景，比对 `GetDataUnwrapped` 输出与期望序列。

#### 4.4.5 小练习与答案

**练习 1**：把上例请求改成 `preTrigSamples=1, postTrigSamples=3`（其余不变），会走哪个分支？
**答案**：`bytes=(1+3)*2=8`；`lastByteAddr` 仍为 1003；`firstByteLinear=1003-8+1=996 < 1000`，仍走两段拼接：`secondChunkSize=4`、`firstChunkSize=4`、`firstChunkStartAddr=1019-4+1=1016`。两次拷贝：`(buffer,1016,4)`、`(buffer+4,1000,4)`。

**练习 2**：什么条件下会走「单段直拷」分支？
**答案**：当 `firstByteLinear = lastByteAddr - bytes + 1 >= winStart`，即要拷的整段数据在地址空间里没有横跨回绕缝——典型是线性缓冲窗口，或环形缓冲里数据恰好没绕过窗口起点。

---

## 5. 综合实践

把本讲四块知识串起来，完成一次「窗口回调里的完整数据读取」纸面推演。

**场景**：你在 Zynq PS 上跑驱动，注册了窗口回调（参见 u4-l2）。某次中断送来一个 `winInfo`，你需要把这一窗数据读出来。

**任务**：

1. 在回调里先调用 `PsiMsDaq_StrWin_GetPreTrigSamples(winInfo, &preTrig)` 拿到可用前触发数（若返回 `-6` 说明这窗没触发，应跳过——但窗口回调方案下一般每窗都含触发）。
2. 决定要读多少前/后触发（例如全读：`preTrigSamples=preTrig`、`postTrigSamples=str_p 配置值`），调用 `GetDataUnwrapped(winInfo, preTrig, postTrigCfg, buf, sizeof(buf))`。
3. 参照 [driver/psi_ms_daq.h:92-100](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L92-L100) 的示例回调，写出最小骨架：先 `Xil_DCacheInvalidateRange`（或你平台的等价操作）维护 cache 一致性，再 `GetDataUnwrapped`，最后 `PsiMsDaq_StrWin_MarkAsFree(winInfo)` 释放窗口。
4. 对照 4.4.4 的手算，解释为什么 cache 维护必须在 `GetDataUnwrapped` **之前**做（提示：DMA 写 DDR 是不经 CPU cache 的，驱动用 CPU 指针直接读，必须先 invalidate）。

**预期产出**：一段约 10 行的回调伪代码 + 一句关于 cache 顺序的解释。结论要点：`GetDataUnwrapped` 只搬数据、**不**应答窗口，应答必须靠随后的 `MarkAsFree`（把 `WINCNT` 写 0，见 [driver/psi_ms_daq.c:641-653](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L641-L653)）。

> 待本地验证：在真实板子上用一组已知样本跑通此回调，比对 `buf` 内容与示波器/激励数据。

## 6. 本讲小结

- `GetDataUnwrapped` 的本质是**环形缓冲解包**：依据硬件回写的 `WIN_LAST`（最后样本首字节地址）作锚，定位用户请求的「前触发 + 后触发」字节区间，再按是否跨回绕缝选拷贝方式。
- 四个辅助函数分工明确：`GetNoOfSamples` 读 `WINCNT[30:0]` 样本数；`GetPreTrigSamples` 检查 `IsTrig` 位并算「样本数 − 配置后触发」；`GetLastSplAddr` 读 `WIN_LAST`；`GetNoOfBytes` 把样本数换算成字节。
- `trigByteAddr` / `lastByteAddr` 用「单次 ±winSize」实现窗口内的模 \( S \) 回绕；`lastByteAddr` 在请求量等于配置量时正好等于 `lastSplAddr + w − 1`，可作正确性自检。
- 三道越界检查对应 `-7 BufferTooSmall`、`-8 MorePostTrigThanConfigured`、`-9 MorePreTrigThanAvailable`，外加 `GetPreTrigSamples` 内部的 `-6 NoTrigInWin`。
- 拷贝分两支：`firstByteLinear >= winStart` 走单段直拷；否则按「末尾段（旧）+ 起点段（新）」两段 `memcpy` 拼接，输出按时间从旧到新排列。
- 数据搬运走注入的 `memcpyFct`（默认 `memcpy`），源指针直接用 IP 视角的 DDR 地址，依赖 DMA 地址空间与 CPU 物理地址空间一致；cache 维护须在调用前完成。

## 7. 下一步学习建议

- **硬件侧对应物**：阅读 [hdl/psi_ms_daq_daq_sm.vhd](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd) 的 `ProcResp0_s`/`NextWin_s`/`WriteCtx_s`，看 `HndlWinLast`、`HndlWinBytes`（→ `WINCNT` 样本数）、`IsTrig`（→ `WINCNT[31]`）是如何在窗口完成时写回上下文 RAM 的——这正是本讲驱动所消费数据的产生端。
- **窗口保护与覆盖**：本讲的解包假设窗口数据已稳定（回调时硬件已写完且未被覆盖）。若 `winOverwrite=true`，窗口可能在你读取时被新数据覆盖，此时应改用「流方案」中断（u4-l2）自行保护，相关硬件协议见 u4-l5（窗口保护、覆盖与 NewBuffer 协议）。
- **测试验证**：要看环形解包的端到端校验如何实现，可阅读 `tb/psi_ms_daq_axi/` 下顶层 testbench（u5-l3），其中 `str*_pkg` 数据包定义了每路期望的帧/触发/时间戳，用于在共享内存模型里比对 `GetDataUnwrapped` 等价的结果。
