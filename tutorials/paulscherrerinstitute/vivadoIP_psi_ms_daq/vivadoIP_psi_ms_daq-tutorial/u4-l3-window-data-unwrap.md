# 窗口数据回读与去环绕回拷贝

## 1. 本讲目标

上一讲（u4-l2）我们讲完了「中断来了，驱动如何把回调派发到正确的窗口」。回调拿到的是一个 `PsiMsDaq_WinInfo_t`，它只告诉你「是哪条流的第几个窗口」——窗口里**到底存了多少采样、触发在哪里、数据在 DDR 里怎么排布**，本讲才给出答案。

本讲精读驱动里最绕、也最关键的一个函数 `PsiMsDaq_StrWin_GetDataUnwrapped`，以及它依赖的一组窗口信息读取助手。学完本讲你应当能：

1. 说清每个窗口在 IP 寄存器空间里维护了哪四字元数据（采样数 / 末样本地址 / 时间戳低 / 时间戳高），以及它们各自的比特含义。
2. 掌握 `GetNoOfSamples` / `GetPreTrigSamples` / `GetTimestamp` / `GetLastSplAddr` 四个助手分别从哪个寄存器取值、做了什么换算。
3. 看懂 `GetDataUnwrapped` 如何从「末样本地址 + 配置的 postTrig」**反推出触发字节地址与末字节地址**，并在环形缓冲数据跨越窗口边界时用**两段 memcpy 拼接**把环形数据展开成线性顺序。
4. 理解三类校验（buffer 大小、postTrig 上限、preTrig 上限）与对应返回码的作用。
5. 能够手工追踪一段具体地址，预测函数会走「单次拷贝」还是「两段拼接」分支。

## 2. 前置知识

本讲假设你已掌握 u3-l1（寄存器映射）、u3-l2（句柄与数据模型）、u3-l4（流配置）、u4-l1（录制模式与窗口/环形缓冲概念）。为独立阅读，这里重温三个关键事实：

- **窗口是 DDR 里一段固定大小的缓冲**。一条流配置了 `bufStartAddr`（缓冲起始）、`winSize`（每窗字节数）、`winCnt`（窗口数），第 `w` 号窗口在 DDR 的起始字节地址为：
  \[
  \text{winStart}(w) = \text{bufStart} + \text{winSize}\cdot w
  \]
  末字节地址为 \(\text{winLast}(w) = \text{winStart} + \text{winSize} - 1\)。

- **环形缓冲 vs 线性缓冲**（u4-l1）。当 `winAsRingbuf=true` 时，IP 在单窗口内**环形写入**：写指针到达 `winLast` 后回绕到 `winStart` 继续写。于是「时间上连续」的数据，在 DDR 物理地址上可能被切成「窗口尾部 + 窗口头部」两段。`GetDataUnwrapped` 的全部意义，就是把这种环形排布**还原成线性**再交给用户。

- **postTrig 在配置时定死并缓存**。`PsiMsDaq_Str_Configure` 把用户传入的 `postTrigSamples` 既写进寄存器，又缓存进流实例的 `str_p->postTrig` 字段（见 u3-l4）。本讲地址反推高度依赖这个缓存值。

- **`WinInfo_t` 是按值传递的栈上结构体**（u3-l2），只含 `winNr` / `ipHandle` / `strHandle` 三字段，不可长期持有。本讲所有助手函数的第一个参数都是它。

一个易混点要先点明：本讲里会出现两个「postTrig」和一个「preTrig」，务必分清：

| 名称 | 含义 | 来源 |
|---|---|---|
| `str_p->postTrig` | **配置时**定死的 post 触发采样数（含触发样本） | 流实例缓存（u3-l4 写入） |
| `postTrigSamples`（参数） | 本次回读**想要**的 post 触发采样数 | `GetDataUnwrapped` 的入参，必须 ≤ 上者 |
| `preTrig`（局部） | 本窗口**实际可用**的 pre 触发采样数 | 运行时由总采样数 − `str_p->postTrig` 算出 |

## 3. 本讲源码地图

本讲只涉及两个文件，焦点是窗口相关的几个函数与寄存器宏：

| 文件 | 本讲关注的内容 |
|---|---|
| [drivers/psi_ms_daq_axi/src/psi_ms_daq.h](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h) | 窗口寄存器宏 `PSI_MS_DAQ_WIN_*`、`WinInfo_t`、相关返回码枚举、`GetDataUnwrapped` 等函数声明 |
| [drivers/psi_ms_daq_axi/src/psi_ms_daq.c](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c) | `GetNoOfSamples` / `GetPreTrigSamples` / `GetTimestamp` / `GetLastSplAddr` / `GetDataUnwrapped` 的实现 |

辅助但非本讲重点：流实例结构体 `PsiMsDaq_StrInst_t`（缓存了 `widthBytes`/`bufStart`/`winSize`/`postTrig`，见 u3-l2）与寄存器访问间接层 `PsiMsDaq_RegRead`/`RegGetField`/`RegGetBit`（见 u3-l3）。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，按「寄存器 → 读取助手 → 去环绕主函数」自底向上推进。

### 4.1 窗口元数据寄存器：WINCNT / LAST / TSLO / TSHI

#### 4.1.1 概念说明

IP-Core 每完成对一个窗口的写入，会在寄存器空间里为该 **(流 n, 窗口 w)** 维护四字（4×32 bit）元数据，用来回答四个问题：

1. **WINCNT**——这个窗口里写进了多少个采样？里面含不含触发？
2. **LAST**——最后一个采样写在 DDR 的哪个字节地址？（环形缓冲反推的锚点）
3. **TSLO / TSHI**——触发发生时刻的 64 位时间戳的低 32 位 / 高 32 位。

这四字是 `GetDataUnwrapped` 全部计算的输入。CPU 不需要自己去 DDR 里数采样，IP 已经替你数好了。

#### 4.1.2 核心流程

四字寄存器在地址空间里的位置由 u3-l1 的窗口区基址公式决定（基址 `0x4000`，窗口间步进 `0x10`，流间步进 `strAddrOffs`）：

\[
\text{addr}(\text{reg}, n, w) = 0x4000 + \text{strAddrOffs}\cdot n + 0x10\cdot w + \text{wordOffset}
\]

其中 `wordOffset` 对 WINCNT/LAST/TSLO/TSHI 分别为 `0x0`/`0x4`/`0x8`/`0xC`。

**WINCNT 是一个双含义字段**，把「采样数」和「是否含触发」压进同一个 32 位字：

| 比特 | 字段 | 含义 |
|---|---|---|
| `[30:0]` | `CNT` | 写入的采样数（低 31 位） |
| `[31]` | `ISTRIG` | 1 = 本窗口包含一次触发；0 = 不含触发 |

为什么要把「含触发」单独标出来？因为不是所有窗口都有触发（例如 `Continuous` 模式或环形缓冲里被覆盖掉的旧窗口）。**只有含触发的窗口，时间戳和时间戳/preTrig 计算才有意义**——这个约束会贯穿后面所有助手函数。

#### 4.1.3 源码精读

窗口寄存器宏全部集中在头文件一段里（注意 `so` 就是 `strAddrOffs`）：

窗口区四字寄存器的地址宏与 WINCNT 的字段比特定义——[psi_ms_daq.h:L176-L182](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L176-L182)：

```c
#define PSI_MS_DAQ_WIN_WINCNT(n, w, so)   (0x4000+(so)*(n)+0x10*(w))
#define PSI_MS_DAQ_WIN_WINCNT_LSB_CNT     0
#define PSI_MS_DAQ_WIN_WINCNT_MSB_CNT     30
#define PSI_MS_DAQ_WIN_WINCNT_BIT_ISTRIG  (1 << 31)
#define PSI_MS_DAQ_WIN_LAST(n, w, so)     (0x4004+(so)*(n)+0x10*(w))
#define PSI_MS_DAQ_WIN_TSLO(n, w, so)     (0x4008+(so)*(n)+0x10*(w))
#define PSI_MS_DAQ_WIN_TSHI(n, w, so)     (0x400C+(so)*(n)+0x10*(w))
```

每个宏都接受三个参数 `(n, w, so)`：流号、窗口号、流间字节步进 `strAddrOffs`。四个 wordOffset `0x0/0x4/0x8/0xC` 决定了它们在同一段 `0x10` 步进里首尾相接。

`WINCNT_LSB_CNT=0`、`WINCNT_MSB_CNT=30`、`WINCNT_BIT_ISTRIG=(1<<31)` 这三个常量会被 `RegGetField` / `RegGetBit` 直接复用（见 4.2），分别取出 31 位采样数和最高位的触发标志。

> 提醒（u3-l1）：`strAddrOffs = 2^⌊log₂(maxWindows)⌋ × 0x10`，其正确性依赖 `maxWindows` 必须是 2 的幂，否则相邻流的窗口区会重叠。本讲所有地址都建立在它正确的前提下。

#### 4.1.4 代码实践

**目标**：手工计算一个具体 (流, 窗口) 的四字寄存器字节地址，巩固「基址 + 流步进 + 窗口步进 + wordOffset」公式。

**步骤**：

1. 设 `maxWindows = 16`，先算 `strAddrOffs`：\(\text{strAddrOffs} = 2^{\lfloor\log_2 16\rfloor}\times 0x10 = 2^4 \times 0x10 = 0x100\)。
2. 取流 `n=2`、窗口 `w=5`。
3. 分别套公式算 WINCNT / LAST / TSLO / TSHI 四个寄存器相对 IP 基址的字节偏移。

**预期结果（源码推导）**：

- 公共前缀：\(0x4000 + 0x100\times 2 + 0x10\times 5 = 0x4000 + 0x200 + 0x50 = 0x4250\)。
- `WINCNT = 0x4250`、`LAST = 0x4254`、`TSLO = 0x4258`、`TSHI = 0x425C`。
- 验证：相邻窗口 `w=6` 的 WINCNT 应为 `0x4260`（差 `0x10`）；相邻流 `n=3` 同窗口的 WINCNT 应为 `0x4350`（差 `0x100`）。这两条性质正是驱动能用 `(so)` 与 `0x10` 线性索引所有窗口的前提。

#### 4.1.5 小练习与答案

**练习 1**：`WINCNT` 为什么只给采样数 31 位（`[30:0]`）而不是全 32 位？

**答案**：因为最高位 `[31]` 被复用为 `ISTRIG` 标志。把「是否含触发」与「采样数」压进同一字，可让驱动只读一次 `WINCNT` 就同时拿到这两个信息（`RegGetBit` 取 bit31，`RegGetField` 取 [30:0]），减少寄存器访问次数。

**练习 2**：若某窗口 `WINCNT` 寄存器读回值为 `0x8000_000A`，说明什么？

**答案**：bit31=1 表示含触发；低 31 位 `0xA = 10` 表示该窗口写入了 10 个采样。

---

### 4.2 窗口信息读取助手：采样数 / preTrig / 时间戳 / 末样本地址

#### 4.2.1 概念说明

有了四字元数据寄存器，驱动提供了一组薄包装函数，把「原始寄存器值」翻译成「人类/算法可直接用的量」。本模块讲四个：

- `PsiMsDaq_StrWin_GetNoOfSamples`——取采样数（直接读 WINCNT 的 `[30:0]`）。
- `PsiMsDaq_StrWin_GetNoOfBytes`——采样数 × 每采样字节数。
- `PsiMsDaq_StrWin_GetPreTrigSamples`——本窗口实际可用的 pre 触发采样数。
- `PsiMsDaq_StrWin_GetTimestamp`——触发的 64 位时间戳。
- `PsiMsDaq_StrWin_GetLastSplAddr`——最后一个采样的 DDR 字节地址。

它们共同的特点是：**输入一个 `WinInfo_t`，输出一个标量**，并做必要的合法性校验。

#### 4.2.2 核心流程

四个函数的数据来源与换算关系如下：

```
WINCNT[30:0]  ──GetNoOfSamples──►  samples
                                    │
                   samples × widthBytes ──GetNoOfBytes──► bytes
                                    │
 WINCNT[31] ──(含触发?)──► ─────────┴── GetPreTrigSamples ──► preTrig = samples − str_p->postTrig
                                    │
 WINCNT[31] ──(含触发?)──► ─────────┴── GetTimestamp ──► ts = (TSHI<<32) | TSLO
                                    │
 LAST          ──────────────────── GetLastSplAddr ──► 末样本字节地址
```

关键约束（贯穿三个函数）：**`GetPreTrigSamples` 与 `GetTimestamp` 都要求窗口含触发**，否则直接返回 `PsiMsDaq_RetCode_NoTrigInWin`（-6）。原因是 preTrig 与时间戳都是「相对触发」定义的量——没有触发就无从谈起。`GetNoOfSamples` 与 `GetLastSplAddr` 则不要求含触发，因为采样数和末样本地址与触发无关。

#### 4.2.3 源码精读

**取采样数**——`PsiMsDaq_StrWin_GetNoOfSamples`，用 `RegGetField` 从 WINCNT 抠出 `[30:0]`：[psi_ms_daq.c:L509-L528](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L509-L528)

```c
SAFE_CALL(PsiMsDaq_RegGetField( winInfo.ipHandle,
                                PSI_MS_DAQ_WIN_WINCNT(strNr, winInfo.winNr, ip_p->strAddrOffs),
                                PSI_MS_DAQ_WIN_WINCNT_LSB_CNT,   // 0
                                PSI_MS_DAQ_WIN_WINCNT_MSB_CNT,   // 30
                                noOfSamples_p));
```

注意它先做 `CheckStrNr` / `CheckWinNr` 两道守卫，防止越界。`GetNoOfBytes` 只是在它基础上乘以缓存的 `str_p->widthBytes`：[psi_ms_daq.c:L496-L507](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L496-L507)。

**取 preTrig**——`PsiMsDaq_StrWin_GetPreTrigSamples`，先查 ISTRIG 位，再相减：[psi_ms_daq.c:L530-L551](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L530-L551)

```c
bool containsTrig;
SAFE_CALL(PsiMsDaq_RegGetBit( winInfo.ipHandle,
                              PSI_MS_DAQ_WIN_WINCNT(str_p->nr, winInfo.winNr, ip_p->strAddrOffs),
                              PSI_MS_DAQ_WIN_WINCNT_BIT_ISTRIG, &containsTrig))
if (!containsTrig) {
    return PsiMsDaq_RetCode_NoTrigInWin;
}
uint32_t samples;
SAFE_CALL(PsiMsDaq_StrWin_GetNoOfSamples(winInfo, &samples));
*preTrigSamples_p = samples - str_p->postTrig;
```

这里的减法是 u4-l1 强调的不对称的体现：postTrig 是配置时定死的（含触发样本），所以 preTrig = 总采样 − postTrig。这一行也是为什么 `GetDataUnwrapped` 内部要先调用本函数拿到「可用 preTrig」再去校验用户请求。

**取时间戳**——`PsiMsDaq_StrWin_GetTimestamp`，同样先查 ISTRIG，再把 TSLO/TSHI 拼成 64 位：[psi_ms_daq.c:L553-L577](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L553-L577)

```c
SAFE_CALL(PsiMsDaq_RegRead(winInfo.ipHandle, PSI_MS_DAQ_WIN_TSLO(...), &tsLo));
SAFE_CALL(PsiMsDaq_RegRead(winInfo.ipHandle, PSI_MS_DAQ_WIN_TSHI(...), &tsHi));
*timestamp_p = (((uint64_t)tsHi) << 32) + tsLo;
```

**取末样本地址**——`PsiMsDaq_StrWin_GetLastSplAddr`，最简单，直接读 LAST 寄存器：[psi_ms_daq.c:L656-L667](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L656-L667)

```c
SAFE_CALL(PsiMsDaq_RegRead(winInfo.ipHandle,
                           PSI_MS_DAQ_WIN_LAST(strNr, winInfo.winNr, ip_p->strAddrOffs),
                           lastSplAddr_p));
```

注意：尽管名字含 "Spl"（sample），返回的其实是一个**字节地址**（后面 `GetDataUnwrapped` 把它当字节地址用）。它指向 IP 在该窗口里写入的最后一个采样的起始字节。

#### 4.2.4 代码实践

**目标**：跟踪一条调用链，理解「为什么 `GetPreTrigSamples` 可能返回 -6」。

**步骤**：

1. 打开 [psi_ms_daq.c:L530-L551](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L530-L551)。
2. 顺着 `RegGetBit(..., WINCNT_BIT_ISTRIG, &containsTrig)` 这一行往里看一层（u3-l3 / u4-l4 讲过的 `RegGetBit`）：它读 WINCNT，再与 `(1<<31)` 做按位与。
3. 设想一个 `Continuous` 录制模式下、尚未发生任何触发的窗口，IP 会把它的 `ISTRIG` 位置 0。
4. 推断：对该窗口调用 `GetPreTrigSamples`，会命中 `if (!containsTrig) return PsiMsDaq_RetCode_NoTrigInWin;`。

**需要观察的现象**：返回码为 `-6`（`NoTrigInWin`），且函数不会去执行 `samples - str_p->postTrig` 的减法——这是合理的，因为没触发就无所谓 preTrig。

**预期结果**：返回 `PsiMsDaq_RetCode_NoTrigInWin`。**待本地验证**：可在参考设计（u5-l2）里人为制造一个不含触发的窗口并调用本函数观察返回值。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `GetNoOfSamples` 不查 ISTRIG 位，而 `GetPreTrigSamples` 要查？

**答案**：采样数与是否含触发无关——无论有没有触发，窗口里写入的采样数都有效。而 preTrig 是「触发之前」的采样数，没有触发这个概念就不成立，所以必须先确认 ISTRIG=1。

**练习 2**：`GetTimestamp` 为什么要读两个寄存器（TSLO + TSHI）？

**答案**：时间戳是 64 位的，而寄存器一次只能读 32 位，所以 IP 把它拆成低 32 位（TSLO）和高 32 位（TSHI）两个字存放；驱动读出后用 `(((uint64_t)tsHi) << 32) + tsLo` 重新拼成 64 位。

---

### 4.3 GetDataUnwrapped：地址反推与去环绕回拷贝

#### 4.3.1 概念说明

`PsiMsDaq_StrWin_GetDataUnwrapped` 是本讲的主角。它解决的问题是：

> 环形缓冲里，时间上连续的一段采样，在 DDR 物理地址上可能被窗口边界切断。请把它**按时间顺序**拷贝到用户给的一段**线性缓冲**里。

它的输入是「想要多少 pre 触发样本 + 想要多少 post 触发样本」，输出是把这段数据按触发居中、时间从早到晚排好放进 `buffer_p`。

核心难点有三个：

1. **反推触发位置**：IP 只告诉你「最后一个采样写在哪（LAST）」和「配置了几个 post 触发样本」。触发样本的地址要靠这两个值**减回去**。
2. **环形回绕修正**：反推过程中地址可能「掉出窗口下界」或「冲出窗口上界」，要用 ±winSize 把它拉回窗口内。
3. **判断是否需要两段拷贝**：如果想要的整段数据在环形缓冲里恰好跨越了窗口边界（末尾与开头），一次 memcpy 取不到连续源地址，必须拆成「窗口尾部一段 + 窗口头部一段」分别拷贝、在目的缓冲里首尾拼接。

#### 4.3.2 核心流程

整个函数可以分成「校验 → 算窗口框 → 反推触发/末字节 → 选分支拷贝」四步。下面用伪代码示意（变量名与源码一致）：

```
samples = preTrigSamples + postTrigSamples          # 想要的总采样数
bytes   = samples * widthBytes                       # 对应字节数
preTrig = GetPreTrigSamples(winInfo)                 # 窗口实际可用 preTrig（含触发校验）

# 三类校验
if bufferSize  < bytes                => BufferTooSmall          (-7)
if postTrigSamples > str_p->postTrig  => MorePostTrigThanConfigured (-8)
if preTrigSamples  > preTrig          => MorePreTrigThanAvailable  (-9)

# 窗口框
winStart = bufStart + winSize * winNr
winLast  = winStart + winSize - 1

# 反推触发字节地址（用【配置的】postTrig）
lastSplAddr   = GetLastSplAddr(winInfo)
trigByteAddr  = lastSplAddr - str_p->postTrig * widthBytes
if trigByteAddr < winStart:  trigByteAddr += winSize     # 回绕修正

# 反推末字节地址（用【请求的】postTrigSamples）
lastByteAddr  = trigByteAddr + postTrigSamples*widthBytes + widthBytes - 1
if lastByteAddr > winLast:   lastByteAddr -= winSize     # 回绕修正

# 选分支
firstByteLinear = lastByteAddr - bytes + 1
if firstByteLinear >= winStart:
    单次 memcpy(src = firstByteLinear, n = bytes)         # 数据没跨边界
else:
    secondChunkSize = lastByteAddr - winStart + 1          # 窗口头部那段
    firstChunkSize  = bytes - secondChunkSize              # 窗口尾部那段
    memcpy(buffer,            winLast-firstChunkSize+1, firstChunkSize)   # 先拷尾部
    memcpy(buffer+firstChunkSize, winStart,           secondChunkSize)   # 再拼头部
```

两个反推公式的直觉：

- **触发字节地址**：最后一个采样在 `lastSplAddr`，从触发样本（含）到最后样本（含）共有 `str_p->postTrig` 个样本，所以触发样本的起始字节 = `lastSplAddr − postTrig·widthBytes`。这里**用配置的 postTrig**，因为触发在环形缓冲里的绝对位置是 IP 按配置定死的，与用户本次想要多少无关。
- **末字节地址**：用户想要 `postTrigSamples` 个 post 样本，末样本的最后一字节 = 触发起始 + `postTrigSamples·widthBytes + (widthBytes−1)`。这里**用请求的 postTrigSamples**。

两段拼接的直觉：环形缓冲里数据按时间顺序排成一圈，当我们想要的那段（`bytes` 字节）的「线性起点」`firstByteLinear` 落到 `winStart` 之前，说明这段数据有一部分在窗口**末尾**（时间较早）、一部分在窗口**开头**（时间较晚）。此时先把末尾那段拷到 buffer 前部，再把开头那段拷到 buffer 后部，buffer 里就恢复了正确的时间顺序。

#### 4.3.3 源码精读

整段实现——[psi_ms_daq.c:L579-L639](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L579-L639)。下面按段拆。

**(a) Setup + 隐式的「含触发」要求**：[psi_ms_daq.c:L589-L593](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L589-L593)

```c
const uint32_t samples = preTrigSamples+postTrigSamples;
const uint32_t bytes = samples*str_p->widthBytes;
uint32_t preTrig;
SAFE_CALL(PsiMsDaq_StrWin_GetPreTrigSamples(winInfo, &preTrig));
```

注意：这里通过 `SAFE_CALL(GetPreTrigSamples(...))` 拿到可用 preTrig。由于 `GetPreTrigSamples` 在窗口不含触发时返回 `NoTrigInWin`，而 `SAFE_CALL` 会短路传播（u4-l4），所以 **`GetDataUnwrapped` 隐式要求窗口必须含触发**——对不含触发的窗口调用它，会直接返回 -6。

**(b) 三类校验**：[psi_ms_daq.c:L596-L604](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L596-L604)

```c
if (bufferSize < bytes)                            return PsiMsDaq_RetCode_BufferTooSmall;             // -7
if (postTrigSamples > str_p->postTrig)             return PsiMsDaq_RetCode_MorePostTrigThanConfigured;  // -8
if (preTrigSamples > preTrig)                      return PsiMsDaq_RetCode_MorePreTrigThanAvailable;    // -9
```

三道校验各管一件事：buffer 装得下想要的字节数、请求的 post 不超过配置、请求的 pre 不超过窗口实际可用的 pre。

**(c) 反推触发字节地址（含回绕）**：[psi_ms_daq.c:L607-L616](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L607-L616)

```c
const uint32_t winStart = str_p->bufStart + str_p->winSize*winInfo.winNr;
const uint32_t winLast  = winStart + str_p->winSize - 1;
uint32_t lastSplAddr;
SAFE_CALL(PsiMsDaq_StrWin_GetLastSplAddr(winInfo, &lastSplAddr));
uint32_t trigByteAddr = lastSplAddr - str_p->postTrig*str_p->widthBytes;
if (trigByteAddr < winStart) {
    trigByteAddr += str_p->winSize;     // 掉出窗口下界，回绕 +winSize
}
```

`lastSplAddr - str_p->postTrig*str_p->widthBytes` 是 `uint32_t` 减法；当结果「负」时它会下溢成一个很大的数，但无论如何该值会 `< winStart`，于是触发 `+= winSize` 把它修正回窗口内。这正是「触发字节地址可能需要 +winSize 回绕」的来源。

**(d) 反推末字节地址（含回绕）**：[psi_ms_daq.c:L617-L620](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L617-L620)

```c
uint32_t lastByteAddr = trigByteAddr + postTrigSamples*str_p->widthBytes + str_p->widthBytes-1;
if (lastByteAddr > winLast) {
    lastByteAddr -= str_p->winSize;     // 冲出窗口上界，回绕 -winSize
}
```

**(e) 分支选择**：[psi_ms_daq.c:L624-L635](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L624-L635)

```c
const int64_t firstByteLinear = (int64_t)lastByteAddr - bytes + 1;
if (firstByteLinear >= winStart) {
    ip_p->memcpyFct(buffer_p, (void*)(size_t)firstByteLinear, bytes);          // 单次拷贝
} else {
    const uint32_t secondChunkSize = lastByteAddr - winStart + 1;
    const uint32_t firstChunkSize  = bytes - secondChunkSize;
    const int64_t  firstChunkStartAddr = winLast - firstChunkSize + 1;
    ip_p->memcpyFct(buffer_p, (void*)(size_t)firstChunkStartAddr, firstChunkSize);              // 先拷窗口尾部
    ip_p->memcpyFct((void*)((uint32_t)buffer_p+firstChunkSize), (void*)(size_t)winStart, secondChunkSize); // 再拼窗口头部
}
```

注意 `firstByteLinear` 被声明为 `int64_t`——正是为了能表示「负」的线性起点（即数据跨越边界的情况），从而用 `< winStart` 干净地判定要不要走两段分支。两段分支里，**先拷的那段是窗口尾部**（`firstChunkStartAddr` 靠近 `winLast`，时间较早），**后拷的那段是窗口头部**（从 `winStart` 起，时间较晚），拼到 buffer 里恰好恢复时间顺序。

最后注意 `ip_p->memcpyFct`：它不是直接调标准 `memcpy`，而是经 IP 实例里注入的函数指针（u3-l2/u3-l3）。默认实现 `PsiMsDaq_DataCopy_Standard` 就是 `memcpy`，但注入版本可在 Linux 用户态先做地址翻译、或在仿真里走后门——这正是「同一份拷贝逻辑跨裸机/Linux/仿真复用」的关键。

#### 4.3.4 代码实践（本讲核心手算任务）

**目标**：给定一组真实参数，手工追踪 `GetDataUnwrapped` 的地址计算，预测它会走单次拷贝还是两段拼接分支。

**已知**：
- `winStart = 0x40000000`（即第 0 号窗口的起始字节）
- `winSize = 32`（字节，即 `0x20`）
- `postTrig = 5`（**配置的** `str_p->postTrig`，含触发样本）
- `widthBytes = 2`（即 16 位流）
- `lastSplAddr = 0x40000006`（LAST 寄存器读回的末样本字节地址）
- 调用：`GetDataUnwrapped(pre=5, post=5)`

**步骤 1：算辅助量**
- `winLast = winStart + winSize - 1 = 0x40000000 + 0x20 - 1 = 0x4000001F`
- `samples = pre + post = 5 + 5 = 10`
- `bytes = samples × widthBytes = 10 × 2 = 20`

**步骤 2：反推触发字节地址 `trigByteAddr`**
\[
\text{trigByteAddr} = \text{lastSplAddr} - \text{postTrig}\cdot\text{widthBytes} = 0x40000006 - 5\cdot 2 = 0x40000006 - 0xA = 0x3FFFFFFC
\]
（`uint32_t` 下溢）由于 \(0x3FFFFFFC < \text{winStart}(0x40000000)\)，触发回绕修正：
\[
\text{trigByteAddr} \mathrel{+}= \text{winSize} \;\Rightarrow\; 0x3FFFFFFC + 0x20 = \boxed{0x4000001C}
\]

**步骤 3：反推末字节地址 `lastByteAddr`**（用请求的 post=5）
\[
\text{lastByteAddr} = \text{trigByteAddr} + \text{post}\cdot\text{widthBytes} + \text{widthBytes} - 1 = 0x4000001C + 5\cdot 2 + 2 - 1 = 0x4000001C + 11 = 0x40000027
\]
由于 \(0x40000027 > \text{winLast}(0x4000001F)\)，回绕修正：
\[
\text{lastByteAddr} \mathrel{-}= \text{winSize} \;\Rightarrow\; 0x40000027 - 0x20 = \boxed{0x40000007}
\]

**步骤 4：判断分支**
\[
\text{firstByteLinear} = \text{lastByteAddr} - \text{bytes} + 1 = 0x40000007 - 20 + 1 = 0x40000007 - 19 = 0x3FFFFFF4
\]
由于 \(0x3FFFFFF4 < \text{winStart}(0x40000000)\)，走 **else 分支（两段拼接）**。

**步骤 5：算两段各自的来源与大小**（验证拼接正确性）
- `secondChunkSize = lastByteAddr - winStart + 1 = 0x40000007 - 0x40000000 + 1 = 8`（窗口头部 8 字节）
- `firstChunkSize = bytes - secondChunkSize = 20 - 8 = 12`（窗口尾部 12 字节）
- `firstChunkStartAddr = winLast - firstChunkSize + 1 = 0x4000001F - 12 + 1 = 0x40000014`

即：第一次 memcpy 从 `0x40000014` 拷 12 字节（覆盖窗口尾部 `[0x40000014..0x4000001F]`），第二次 memcpy 从 `0x40000000` 拷 8 字节（覆盖窗口头部 `[0x40000000..0x40000007]`），拼成 20 字节的线性 buffer。

**需要观察的现象 / 预期结果**：
- `trigByteAddr = 0x4000001C`
- `lastByteAddr = 0x40000007`
- 走**两段拼接分支**。
- 末字节地址 `0x40000007` 恰好等于 `lastSplAddr + widthBytes - 1 = 0x40000006 + 1 = 0x40000007`，这印证了「lastByteAddr 就是最后一个样本的最后一字节」。
- 本结果是**纯源码静态推导**，未在硬件上运行；如需在真实 IP 上复现，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：如果把请求改成 `GetDataUnwrapped(pre=5, post=4)`（其余参数同 4.3.4），`lastByteAddr` 会变成多少？还会走两段拼接吗？

**答案**：
- `samples = 5+4 = 9`，`bytes = 9×2 = 18`。
- `trigByteAddr` 仍由配置的 postTrig=5 决定，依然是 `0x4000001C`（不变，因为它用的是 `str_p->postTrig`）。
- `lastByteAddr = 0x4000001C + 4·2 + 2 - 1 = 0x4000001C + 9 = 0x40000025 > winLast`，回绕后 `0x40000025 - 0x20 = 0x40000005`。
- `firstByteLinear = 0x40000005 - 18 + 1 = 0x40000005 - 17 = 0x3FFFFFF4 < winStart`，**仍走两段拼接**。
- 规律：只要想要的 `bytes` 足够大、数据跨越了窗口边界，就会走两段分支；减小请求只是缩小总字节数，本例下界依然落在窗口外。

**练习 2**：`GetDataUnwrapped` 为什么对**不含触发**的窗口会失败？失败码是什么？

**答案**：因为函数内部第一步就 `SAFE_CALL(PsiMsDaq_StrWin_GetPreTrigSamples(...))`，而后者在 `ISTRIG=0` 时返回 `PsiMsDaq_RetCode_NoTrigInWin`（-6）；`SAFE_CALL` 把该非 Success 码短路返回给上层。所以失败码是 `-6`。这也说明本函数只适合处理「含触发的窗口」——无触发窗口的数据应改用别的读取方式（例如直接读 DDR）。

**练习 3**：`trigByteAddr` 的计算用的是 `str_p->postTrig`，而 `lastByteAddr` 用的是参数 `postTrigSamples`。为什么不能用同一个？

**答案**：`str_p->postTrig` 是 IP 硬件配置时定死的 post 触发采样数，决定了**触发样本在环形缓冲里的绝对物理位置**——这是客观事实，与用户本次想读多少无关，所以反推触发地址必须用它。`postTrigSamples` 是用户本次**想要**回读的 post 样本数（允许 ≤ 配置值），它决定的是「读多远」，所以反推末字节地址用它。两者职责不同，校验 `-8 (MorePostTrigThanConfigured)` 正是保证 `postTrigSamples ≤ str_p->postTrig`。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「从寄存器值预测回读行为」的完整推演。

**场景**：某 16 位流（`widthBytes=2`）配置为 `bufStart=0x40000000`、`winSize=64`、`winCnt=4`、`postTrigSamples=8`、`winAsRingbuf=true`。一次触发后，第 0 号窗口的寄存器读回：`WINCNT = 0x8000_0014`、`LAST = 0x4000_000A`。

**任务**：

1. 解读 `WINCNT`：本窗口有多少采样？是否含触发？
2. 算出该窗口实际可用的 preTrig（即 `GetPreTrigSamples` 的返回值）。
3. 手算 `trigByteAddr` 与 `lastByteAddr`（注意 `winStart/winLast/winSize` 都要用本题的值）。
4. 判断 `GetDataUnwrapped(pre=8, post=8, buffer, 64)` 会走哪个分支，并算出两段（或一段）的来源地址与大小。
5. 说明在调用 `GetDataUnwrapped` **之前**，参考设计（u5-l2 的 `Str0Irq`/`Str1Irq`）为什么必须先 `Xil_DCacheInvalidateRange`。

**参考推演**：
1. `WINCNT = 0x8000_0014`：bit31=1 含触发；低 31 位 `0x14 = 20` 个采样。
2. preTrig = samples − postTrig = 20 − 8 = 12。
3. `winStart = 0x40000000`，`winLast = 0x40000000 + 64 - 1 = 0x4000003F`，`winSize = 64 = 0x40`。
   - `trigByteAddr = LAST − postTrig·widthBytes = 0x4000000A − 8·2 = 0x4000000A − 0x10 = 0x3FFFFFFA`，`< winStart` → `+= 0x40` → `0x4000003A`。
   - `lastByteAddr = trigByteAddr + post·widthBytes + widthBytes − 1 = 0x4000003A + 8·2 + 2 − 1 = 0x4000003A + 17 = 0x4000004B`，`> winLast` → `−= 0x40` → `0x4000000B`。
4. `samples = 8+8 = 16`，`bytes = 16·2 = 32`，`bufferSize=64 ≥ 32` 通过；`post=8 ≤ postTrig=8` 通过；`pre=8 ≤ preTrig=12` 通过。`firstByteLinear = 0x4000000B − 32 + 1 = 0x4000000B − 31 = 0x3FFFFFFC < winStart` → **两段拼接**：`secondChunkSize = 0x4000000B − 0x40000000 + 1 = 12`，`firstChunkSize = 32 − 12 = 20`，`firstChunkStartAddr = 0x4000003F − 20 + 1 = 0x4000002C`。即先从 `0x4000002C` 拷 20 字节（窗口尾部），再从 `0x40000000` 拷 12 字节（窗口头部）。
5. IP 经 AXI Master 直写 DDR，而 CPU 侧 DCache 可能缓存了旧值；不先失效缓存，`memcpy` 读到的可能是脏的旧数据而非 IP 刚写进去的新数据。这正是 u5-l2 在回读前必须 `Xil_DCacheInvalidateRange` 的原因。

> 以上第 1–4 步为源码静态推导；若要在真实硬件复现同样数值，**待本地验证**。

## 6. 本讲小结

- 每个窗口在寄存器空间维护四字元数据：`WINCNT`（采样数 `[30:0]` + 触发标志 `ISTRIG[31]`）、`LAST`（末样本字节地址）、`TSLO`/`TSHI`（64 位时间戳的低/高 32 位），地址由 `0x4000 + strAddrOffs·n + 0x10·w + wordOffset` 给出。
- `GetNoOfSamples` / `GetLastSplAddr` 与触发无关；`GetPreTrigSamples`（= 总采样 − 配置 postTrig）/ `GetTimestamp` 要求窗口含触发，否则返回 `NoTrigInWin`(-6)。
- `GetDataUnwrapped` 用 LAST 与**配置的** `str_p->postTrig` 反推触发字节地址，用**请求的** `postTrigSamples` 反推末字节地址；两者都可能需要 ±winSize 做环形回绕修正。
- 是否走两段拼接，由 `firstByteLinear = lastByteAddr − bytes + 1` 是否 `< winStart` 决定；跨越边界时先拷窗口尾部、再拼窗口头部，以恢复时间顺序。
- 三类校验分别对应 `-7 BufferTooSmall` / `-8 MorePostTrigThanConfigured` / `-9 MorePreTrigThanAvailable`；此外函数隐式要求窗口含触发（经 `GetPreTrigSamples` 的 `SAFE_CALL` 传播）。
- 实际内存拷贝走注入的 `ip_p->memcpyFct`（默认是 `memcpy`），同一份去环绕逻辑因此能跨裸机/Linux/仿真复用。

## 7. 下一步学习建议

- **横向收口驱动底层工具**：本讲反复出现 `SAFE_CALL`、`RegGetField`/`RegGetBit` 的读改写与守卫函数 `CheckWinNr`/`CheckStrNr`，以及全部返回码。这些是 u4-l4 的主题，建议接着读 u4-l4 把这些底层工具与返回码一次性理清。
- **回到端到端应用**：本讲的 `GetDataUnwrapped` 在真实工程里如何被调用（DCache 失效、取时间戳、回读校验、`MarkAsFree` 释放窗口）是 u5-l2 的主线，建议读 [refdesign/ZCU102/Sdk/app/src/main.c](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/Sdk/app/src/main.c) 中的 `Str0Irq` / `Str1Irq` 回调，把本讲的「两段拼接」放到完整中断链路里看一遍。
- **向上追硬件实现**：本讲所有元数据寄存器（WINCNT/LAST/TSLO/TSHI）的写入方是上游 `psi_multi_stream_daq` 的 IP-Core 硬件（本仓库只是 Vivado 封装层，见 u1-l1）。若你想知道「IP 如何在环形缓冲里维护 LAST 与 ISTRIG」，需要去上游仓库阅读 `psi_ms_daq_axi` 的 RTL 与 PDF 文档。
- **动手验证建议**：在 ZCU102 参考设计里把一条流配成 `winAsRingbuf=true`、故意让单窗口数据量接近 `winSize`，触发后用本讲的手算方法预测 `trigByteAddr` / `lastByteAddr` 与分支选择，再对比 `GetDataUnwrapped` 实际拷出的 buffer 内容，检验你对去环绕逻辑的理解。
