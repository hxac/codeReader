# 窗口保护、覆盖与 NewBuffer/FirstAfterEna 协议

> 本讲是 u4（专家层）的关键一讲。前面 u3-l3 讲了「状态机如何写满一个窗口、如何切换/回绕」，u4-l2 讲了「软件如何通过 `MarkAsFree` 释放窗口」。本讲把这两端拧到一起，回答一个核心问题：**当硬件想往一个窗口写、而软件还没把那个窗口读完时，状态机靠什么避免把还没读走的数据冲掉？** 答案就是标题里的四个标志：`WinProtected`、`NewBuffer`、`FirstAfterEna`/`FirstOngoing`，外加一个在途命令互斥标志 `OpenCommand`。

## 1. 本讲目标

学完本讲，读者应该能够：

- 说清楚 `OpenCommand`、`WinProtected`、`NewBuffer` 三个 per-stream 标志各自记录的是「哪一种占用」，以及它们如何叠加成 `DataAvailArbIn` / `DataPending` 两个屏蔽向量。
- 在 `winOverwrite=false`（不允许覆盖）时，追踪一次「窗口已被硬件写满、软件尚未 `MarkAsFree`」的完整过程：`CalcAccess0_s` 置 `WinProtected` → 屏蔽仲裁 → `TlastCheck_s` 清零重试 → 软件释放后恢复。
- 在 `winOverwrite=true`（允许覆盖）时，说清楚状态机如何**跳过**保护检查、直接覆盖旧数据。
- 解释 `FirstAfterEna` 与 `FirstOngoing` 这对「待办 / 进行中」握手如何保证流在（重新）使能后把写指针、窗口末尾、当前窗口号重置回 `bufstart`，且命令路径与响应路径一致。
- 识别 `OpenCommand` 如何防止同一流同时存在多条在途 DMA 命令。

## 2. 前置知识

本讲默认你已经读过：

- **u3-l3**（窗口切换、环形缓冲与上下文回写）：知道 `NextWin_s` 何时判定「窗口完成」、何时 `NewBuffer` 被置位、线性缓冲与环形缓冲在指针回绕上的差异。
- **u3-l4 / u3-l5**（上下文存储模型与寄存器接口）：知道每个流有 5 个流上下文字段（SCFG、BUFSTART、WINSIZE、PTR、WINEND），每个「流×窗口」有窗口上下文（WINCNT、LAST、TS）；WINCNT=0 表示窗口空闲。
- **u4-l2**（驱动中断处理与窗口回调）：知道软件靠 `PsiMsDaq_StrWin_MarkAsFree()` 把窗口 WINCNT 写回 0 来「释放」窗口；窗口方案要求 `winOverwrite=false`。

几个本讲会反复用到的术语：

- **窗口可用（free）**：该窗口的 WINCNT=0，即软件已确认读完或从未写入，硬件可以安全使用。
- **窗口被占用（not free）**：WINCNT≠0，硬件已写入过数据，软件尚未释放。
- **在途命令（open command）**：状态机已经发给 DMA 引擎、但还没收到响应（`Dma_Resp`）的那条命令。
- **保护（protected）**：状态机主动给某流打上的「当前窗口不可写」标记，用于在 `winOverwrite=false` 时阻止覆盖。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [hdl/psi_ms_daq_daq_sm.vhd](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd) | 控制状态机。本讲几乎全部逻辑都在这里：标志声明、`CalcAccess0_s`、`NextWin_s`、`TlastCheck_s`、`First_s`、禁用流处理、复位。 |
| [hdl/psi_ms_daq_pkg.vhd](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_pkg.vhd) | 提供 SCFG 内部位移常量 `CtxStr_Sft_SCFG_RINGBUF_c` / `OVERWRITE_c` 等，决定 `HndlOverwrite` / `HndlRingbuf` 从哪个比特读出。 |
| [driver/psi_ms_daq.h](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h) | C 驱动头文件。`winOverwrite`/`winAsRingbuf` 字段与 `PSI_MS_DAQ_CTX_SCFG_BIT_OVERWRITE` 宏，说明软件如何把覆盖位写进 SCFG。 |
| [driver/psi_ms_daq.c](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c) | `Str_Configure` 把 `winOverwrite` 翻译成 SCFG 写；初始化时把窗口 WINCNT 清 0（释放）。 |
| [tb/psi_ms_daq_daq_sm/psi_ms_daq_daq_sm_tb_case_multi_window.vhd](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_daq_sm/psi_ms_daq_daq_sm_tb_case_multi_window.vhd) | 状态机 testbench 的多窗口用例，系统地覆盖「线性/环形 × 覆盖/不覆盖」四种组合，是本讲代码实践的依据。 |

## 4. 核心概念与源码讲解

### 4.1 数据竞争与三个保护标志

#### 4.1.1 概念说明

多窗口 DAQ 的本质是一个**生产者（硬件 DMA 写）与消费者（软件 CPU 读）共享一块环形排列的窗口内存**的并发系统。每个流有 `Windows_g` 个窗口（最多 32），硬件按 `Wincur` 依次写满，写满或触发后切到下一个；软件则在回调里把已写满的窗口拷走、再 `MarkAsFree` 释放。

竞争就出现在「环形回绕」那一刻：当硬件写完最后一个窗口、绕回到窗口 0 时，窗口 0 里的旧数据软件可能**还没读走**。如果硬件径直覆盖，就会丢失尚未消费的数据。

状态机用三个 per-stream（每个流一比特）的标志来治理这种竞争，它们各自记录「哪一种占用」：

| 标志 | =1 的含义 | 谁置位 | 谁清除 |
| --- | --- | --- | --- |
| `OpenCommand` | 该流有一条 DMA 命令在途（已发未回应） | `CalcAccess0_s`（发命令时） | `ProcResp0_s`（收到响应时） |
| `WinProtected` | 该流当前窗口不可写（软件未释放），且不允许覆盖 | `CalcAccess0_s`（保护检查触发） | `TlastCheck_s`（周期性全清） |
| `NewBuffer` | 该流当前窗口是「刚完成、待确认」的新缓冲 | `NextWin_s`（窗口完成时）/ 禁用流时 | `CalcAccess0_s`（成功使用该窗口时） |

另有 `FirstAfterEna` / `FirstOngoing` 一对握手标志，专管「（重新）使能后的首次访问重置」，在 4.5 节展开。

#### 4.1.2 核心流程

三个标志最终汇聚成两条屏蔽向量，喂给仲裁器与状态转移决策：

```
InpDataAvail(str)   = 流使能 且 全局使能 且 输入水位 >= MinBurstSize_g
DataAvailArbIn      = InpDataAvail AND (NOT OpenCommand) AND (NOT WinProtected)
DataPending         = InpDataAvail AND (NOT WinProtected)
```

注意两条向量的**不对称**：

- `DataAvailArbIn` 同时屏蔽 `OpenCommand` 和 `WinProtected`，用于「能否给这个流发新命令」。
- `DataPending` **只**屏蔽 `WinProtected`，**不**屏蔽 `OpenCommand`。它的作用是：当一个高优先级流的命令正在飞时，仍然把它算作「有数据待处理」，于是状态机选择去等它的响应（`CheckResp_s`），而不是降级去服务低优先级流——防止低优先级流插队。

#### 4.1.3 源码精读

五个 per-stream 标志的声明（注意行尾注释点明 `WinProtected` 的含义）：

[hdl/psi_ms_daq_daq_sm.vhd:153-157](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L153-L157) — 声明 `OpenCommand` / `WinProtected`（注释：Set if the current window is not yet available）/ `NewBuffer` / `FirstAfterEna` / `FirstOngoing`。

两条屏蔽向量的计算，行尾注释解释了为何 `DataPending` 不屏蔽 `OpenCommand`：

[hdl/psi_ms_daq_daq_sm.vhd:254-255](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L254-L255)

而「当前流是否允许覆盖 / 是否环形」是从流上下文 SCFG 里读出的两个运行期标量（一次命令处理期间不变）：

[hdl/psi_ms_daq_daq_sm.vhd:167-168](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L167-L168) — `HndlRingbuf` / `HndlOverwrite` 标量。

SCFG 里的位布局（`OVERWRITE` 在 bit 8、`RINGBUF` 在 bit 0）来自包常量，软件侧用同名宏对应：

[hdl/psi_ms_daq_pkg.vhd:76-79](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_pkg.vhd#L76-L79)

[driver/psi_ms_daq.h:165-166](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L165-L166) — `PSI_MS_DAQ_CTX_SCFG_BIT_RINGBUF` = `1<<0`、`PSI_MS_DAQ_CTX_SCFG_BIT_OVERWRITE` = `1<<8`，与上面的位移常量严格对齐。

#### 4.1.4 代码实践

**目标**：确认软件写入的 `winOverwrite` 位如何流到硬件的 `HndlOverwrite`。

**步骤**：

1. 打开 [driver/psi_ms_daq.c:292-310](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L292-L310)，看到 `Str_Configure` 用 `PsiMsDaq_RegSetBit(... PSI_MS_DAQ_CTX_SCFG_BIT_OVERWRITE, config_p->winOverwrite)` 把覆盖位写进 SCFG。
2. 跟着 SCFG 进 [hdl/psi_ms_daq_daq_sm.vhd:365-366](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L365-L366)，看到 `ReadCtxStr_s` 在第 4 拍把 `CtxStr_Resp.RdatLo(CtxStr_Sft_SCFG_OVERWRITE_c)` 装进 `HndlOverwrite`。
3. 在 [hdl/psi_ms_daq_daq_sm.vhd:434](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L434) 确认 `HndlOverwrite` 正是保护判定的第一个条件。

**预期结果**：你应当能画出一条完整的位传播链 `winOverwrite(C 结构体) → RegSetBit → SCFG[8] → CtxStr_Resp.RdatLo[8] → HndlOverwrite → CalcAccess0_s 判定`。这是一个纯源码阅读型实践，不依赖仿真器。

#### 4.1.5 小练习与答案

**练习 1**：`DataAvailArbIn` 和 `DataPending` 都屏蔽了 `WinProtected`，但只有前者屏蔽 `OpenCommand`。如果一个高优先级流的命令正在飞、`OpenCommand=1`，此时一个低优先级流也有满突发数据，状态机会怎么做？

**答案**：因为 `DataPending` 不屏蔽 `OpenCommand`，高优先级流仍算「pending」，`CheckPrio1_s` 会跳到 `CheckResp_s` 去等那条在途命令的响应，而不是降级到 `CheckPrio2_s`。低优先级流必须等高优先级流的响应处理完才有机会。这正是「档间严格优先级」的体现。

**练习 2**：`OpenCommand`、`WinProtected`、`NewBuffer` 三个标志，哪个是「一次性事件」、哪个是「持续状态」？

**答案**：`OpenCommand` 和 `NewBuffer` 都是「一次性事件」——前者在发命令时置 1、收响应时清 0；后者在窗口完成时置 1、成功复用窗口时清 0。`WinProtected` 是「持续状态」，一旦置位会一直屏蔽仲裁，直到 `TlastCheck_s` 主动清零才解除，期间是一个「保护—重试」循环。

---

### 4.2 CalcAccess0_s：窗口保护判定与 winOverwrite 分叉

#### 4.2.1 概念说明

`CalcAccess0_s` 是命令路径的核心决策点。它已经从上下文里读出了当前窗口的已写字节数（`HndlWinBytes`，来自窗口 WINCNT 换算）、本流的覆盖位（`HndlOverwrite`）、以及 `NewBuffer` 标志。它要回答一个问题：**这个窗口现在能不能写？**

判定的逻辑可以用一句话概括：**「不允许覆盖」且「窗口里已经有数据」且「这个窗口是刚完成待确认的新缓冲」→ 保护它，别写。** 其余情况都允许写。

三个条件缺一不可的设计意图：

- `HndlOverwrite = '0'`：软件明确不允许覆盖，保护才有意义；允许覆盖时整个判定短路为「可写」。
- `HndlWinBytes /= 0`：窗口里确实有数据（WINCNT≠0）。首次使用一个空闲窗口时 WINCNT=0，此条件为假，自然放行。
- `NewBuffer = '1'`：这个窗口是「上一轮写完后标记为待确认」的。如果没有这个标志，状态机会对任何非空窗口都保护，连「正在连续写、还没写满」的窗口也会被误保护——`NewBuffer` 把保护范围精确限定在「已完成、待软件确认」的窗口上。

#### 4.2.2 核心流程

```
进入 CalcAccess0_s（已读上下文，已算 Dma_Cmd.Address / MaxSize）
├─ if (NOT Overwrite) AND (WinBytes != 0) AND (NewBuffer==1):
│     ├─ State ← Idle_s            # 放弃这次命令
│     ├─ WinProtected[str] ← 1     # 标记保护，屏蔽后续仲裁
│     └─ （NewBuffer 保持 1，下次重试再判）
│
└─ else:                            # 窗口可用（含「允许覆盖」的所有情况）
      ├─ State ← CalcAccess1_s     # 继续算最终 MaxSize
      ├─ NewBuffer[str] ← 0        # 消费掉「待确认」标记
      └─ OpenCommand[str] ← 1      # 标记在途命令
```

关键对照：`winOverwrite=true` 时，无论窗口是否被占用、`NewBuffer` 是否为 1，都走 else 分支——这就是「允许覆盖」的字面含义：**旧数据可以被无条件冲掉**。

#### 4.2.3 源码精读

`CalcAccess0_s` 的完整判定，注意三条件与三个赋值的对应：

[hdl/psi_ms_daq_daq_sm.vhd:428-442](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L428-L442) — 装配 `Dma_Cmd.Address/Stream/MaxSize`，然后用 `if (r.HndlOverwrite = '0') and (unsigned(r.HndlWinBytes) /= 0) and (r.NewBuffer(r.HndlStream) = '1')` 分叉：成立则 `State:=Idle_s` + `WinProtected:=1`；否则进入 `CalcAccess1_s`，清 `NewBuffer`、置 `OpenCommand`。

`HndlWinBytes` 是在第 4.1.3 节之外、由 `ReadCtxWin_s` 从窗口 WINCNT 换算来的（带 1 位保护位防溢出）：

[hdl/psi_ms_daq_daq_sm.vhd:417-423](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L417-L423) — `'0' & shift_left(CtxWin_Resp.RdatLo, Log2StrBytes_c(i))`：窗口 WINCNT（采样数）左移成字节数，高位补 0 作保护位。窗口空闲（WINCNT=0）时 `HndlWinBytes=0`。

#### 4.2.4 代码实践

**目标**：手工模拟四种 `(Overwrite, 窗口状态, NewBuffer)` 组合，预测 `CalcAccess0_s` 走哪个分支。

**步骤**：在下表每一行填入「保护 / 放行」与被改写的标志：

| Overwrite | 窗口 WINCNT | NewBuffer | 分支？ | 改写的标志 |
| --- | --- | --- | --- | --- |
| 0 | 0（空闲） | 1 | ? | ? |
| 0 | ≠0（占用） | 1 | ? | ? |
| 0 | ≠0（占用） | 0 | ? | ? |
| 1 | ≠0（占用） | 1 | ? | ? |

**需要观察的现象 / 预期结果**：

- 第 1 行：`WinBytes=0` → 条件为假 → **放行**，`NewBuffer←0`、`OpenCommand←1`。这正是「首次使用空闲窗口」。
- 第 2 行：三条件全真 → **保护**，`WinProtected←1`，回 `Idle_s`。这就是「窗口写满了、软件没释放、又不让覆盖」的典型保护。
- 第 3 行：`NewBuffer=0`（窗口还在连续写、没经历过完成）→ 条件为假 → **放行**。这保证了「一个长记录在窗口内多次 DMA」不被误保护。
- 第 4 行：`Overwrite=1` → 条件短路为假 → **放行**，旧数据被覆盖。

（纯推理型实践，无需运行。）

#### 4.2.5 小练习与答案

**练习 1**：为什么保护判定里要有 `NewBuffer=1` 这一项？去掉它会怎样？

**答案**：`NewBuffer` 把保护精确限定在「上一轮已完成、等待软件确认」的窗口上。若去掉它，只要窗口里有数据（`WinBytes≠0`）就会触发保护，于是「一个长记录在窗口内连续多次 DMA」时，第二次 DMA 一上来就会被误判为保护、写不进去，记录无法连续。`NewBuffer` 让保护只发生在「窗口确实绕回到了一个已满的旧窗口」这种真正需要软件介入的场景。

**练习 2**：`winOverwrite=true` 时，`NewBuffer` 还会被清 0 吗？`OpenCommand` 还会被置 1 吗？

**答案**：会。else 分支不区分覆盖与否，照样执行 `NewBuffer←0` 和 `OpenCommand←1`。也就是说，覆盖模式只是**跳过保护检查**，命令签发与在途管理的流程完全一样。这保证了一旦软件把 `winOverwrite` 改回 false，状态机的命令/响应记账仍然正确。

---

### 4.3 NextWin_s：写满/触发后置位 NewBuffer

#### 4.3.1 概念说明

`NewBuffer` 不是凭空出现的——它由响应路径的 `NextWin_s` 在「窗口完成」时置位。回顾 u3-l3：窗口完成有两种判定：

1. **线性缓冲写满**：`HndlPtr1 = HndlWinEnd` 且 `HndlRingbuf = '0'`。
2. **触发结束**：`Dma_Resp.Trigger = '1'`（任何模式下，触发都封口当前窗口）。

只要满足其一，当前窗口就被封口、切到下一个窗口，同时 `NewBuffer[str] ← 1`。这个 `NewBuffer` 标志的语义正是 4.2 节保护判定所消费的：「这个流现在指向的新窗口，在上一次轮到它时是否会被当作待确认缓冲来检查」。

注意一个重要区别：**环形缓冲在窗口内回绕时（指针绕回本窗口起点继续覆盖写）不算「窗口完成」**，因此**不**置 `NewBuffer`、也**不**发中断（见 u3-l3 与 u4-l1）。只有「切到下一个窗口」或「触发封口」才算完成。

#### 4.3.2 核心流程

```
NextWin_s（已用 Dma_Resp.Size 推进指针到 HndlPtr1，已累计 HndlWinBytes）
├─ 窗口完成判定： (Ptr1==WinEnd AND NOT Ringbuf) OR Trigger
│   ├─ 是 → HndlWinDone←1, NewBuffer[str]←1
│   │        切窗：到末窗(Wincur==Wincnt)则绕回 0+bufstart，否则 Wincur++、Ptr2←旧WinEnd
│   └─ 否 → 不切窗
│
├─ 环形窗口内回绕判定： (Ptr1==WinEnd AND Ringbuf AND NOT Trigger)
│   └─ 是 → Ptr2 ← Ptr1 - WinSize   # 绕回窗口起点继续覆盖，不置 NewBuffer
│
└─ 算末样地址、时间戳、置 Dma_Resp_Rdy，进入 WriteCtx_s
```

#### 4.3.3 源码精读

窗口完成判定与 `NewBuffer` 置位（注意它和 `HndlWinDone` 同时置位）：

[hdl/psi_ms_daq_daq_sm.vhd:477-489](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L477-L489) — `if ((r.HndlPtr1 = r.HndlWinEnd) and (r.HndlRingbuf = '0')) or (Dma_Resp.Trigger = '1') then ... v.NewBuffer(r.HndlStream) := '1';` 随后按 `Wincur` 是否等于 `Wincnt` 决定绕回窗口 0 还是递进到下一窗口。

环形窗口内回绕（关键：**不**置 `NewBuffer`、**不**置 `HndlWinDone`）：

[hdl/psi_ms_daq_daq_sm.vhd:491-493](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L491-L493) — `if (r.HndlPtr1 = r.HndlWinEnd) and (r.HndlRingbuf = '1') and (Dma_Resp.Trigger = '0') then v.HndlPtr2 := std_logic_vector(unsigned(r.HndlPtr1) - unsigned(r.HndlWinSize));` 仅改写指针，不封口。

#### 4.3.4 代码实践

**目标**：用 testbench 用例核对「环形不覆盖」模式下 `NewBuffer` 的置位时机。

**步骤**：

1. 打开 [tb/psi_ms_daq_daq_sm/psi_ms_daq_daq_sm_tb_case_multi_window.vhd:142-146](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_daq_sm/psi_ms_daq_daq_sm_tb_case_multi_window.vhd#L142-L146)，这是 `-- Ringbuf without overwrite` 用例（`Ringbuf=>'1', Overwrite=>'0', Wincnt=>2, Wincur=>0`）。
2. 对照同文件响应期望（`dma_cmd` 过程）中 case 4 的期望序列（约 [L236 起](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_daq_sm/psi_ms_daq_daq_sm_tb_case_multi_window.vhd#L236-L260)）。
3. 找出：哪些 `ExpectDmaCmdAuto` 带 `NextWin => true`（对应窗口完成、`NewBuffer` 置位），哪些不带（对应环形窗口内回绕、不置 `NewBuffer`）。

**需要观察的现象 / 预期结果**：在环形模式下，写指针到达 `WinEnd` 但未触发时，期望命令**不带** `NextWin`（说明只是绕回窗口起点、没切窗、没置 `NewBuffer`）；只有在「触发」发生时才出现 `NextWin => true`（封口切窗、置 `NewBuffer`）。这正是 4.3.3 两段代码的差异在 testbench 里的投影。**待本地验证**（需要 PsiSim/Modelsim 跑该 TB）。

#### 4.3.5 小练习与答案

**练习 1**：一个流配置为「线性、不覆盖、3 个窗口」。硬件依次写满窗口 0、1、2，然后绕回窗口 0 但软件还没释放窗口 0。写出每写满一个窗口后 `NewBuffer` 的变化。

**答案**：写满窗口 0 → `NewBuffer←1`（切到窗口 1）；写满窗口 1 → `NewBuffer←1`（切到窗口 2）；写满窗口 2 → `NewBuffer←1`（绕回窗口 0，`Wincur` 从 `Wincnt=2` 回到 0）。注意 `NewBuffer` 是 per-stream 单比特，每次切窗都把它重新置 1，下一次轮到该流时 `CalcAccess0_s` 就会用它 + WINCNT≠0 来判定保护。绕回窗口 0 时，因为窗口 0 未释放（WINCNT≠0）且不覆盖，会被保护挡住。

**练习 2**：为什么环形缓冲的「窗口内回绕」不置 `NewBuffer`？

**答案**：环形缓冲的设计意图就是「单个窗口内持续滚动覆盖」，软件通过末样地址（`WIN_LAST`）解包出最新的一段数据（见 u4-l3）。如果窗口内回绕也置 `NewBuffer`，在 `winOverwrite=false` 时会把正在滚动写的窗口自己保护起来，导致环形缓冲停摆。所以只有「切到下一个独立窗口」或「触发封口」才算完成。

---

### 4.4 TlastCheck_s：周期性清零 WinProtected 重试

#### 4.4.1 概念说明

`WinProtected` 一旦在 `CalcAccess0_s` 被置位，就会通过 `DataAvailArbIn` / `DataPending` 把这个流从仲裁里屏蔽掉。问题是：**谁来清除 `WinProtected`，让这个流在软件释放窗口后重新获得资格？**

答案是一个看似无关的状态：`TlastCheck_s`。它是优先级扫描链的末端（`CheckPrio3_s` 之后），专门用来「冲刷帧末尾（HasLast）残余」。但在本讲的语境里，它还承担了**周期性全清 `WinProtected`** 的职责。

设计上的精妙之处在于触发时机：只有当**所有优先级档都没有可服务的满突发**（`CheckPrio1/2/3` 都没命中、落到 `TlastCheck_s`）时，才执行清零。行尾注释把它解释得很清楚：「没有任何流的满突发可用，说明所有流都被检查过了，可以重试——也许软件已经腾空了某个窗口」。

清零不是「立刻就能写」，而是「给被保护的流一次重新读上下文、重新判定 WINCNT 的机会」。于是形成一个**保护—等待—重试**的轮询循环：

```
保护(WinProtected←1) → 屏蔽仲裁 → 等到所有流都无可服务突发
   → TlastCheck_s 清零 WinProtected → 下一轮重新读 WINCNT
   → 软件已释放(WINCNT=0)？放行；否则再次保护
```

#### 4.4.2 核心流程

```
TlastCheck_s:
  State ← CheckResp_s                         # 默认：去看有没有响应要处理
  WinProtected ← (others => '0')              # 关键：全清所有流的保护
  for each stream idx:
    if HasLast[idx]==1 AND OpenCommand[idx]==0 AND WinProtected[idx]==0:
        State ← ReadCtxStr_s                  # 有帧末尾残余要冲刷
        HndlStream ← idx
```

注意清零 `WinProtected` 是**无条件全清**（对整条向量赋 0），而选择「冲刷哪个流的 HasLast 残余」则是有条件的循环。两者在同一状态里并行发生。

#### 4.4.3 源码精读

`TlastCheck_s` 的完整实现，行尾注释解释了为何此刻可以安全清零：

[hdl/psi_ms_daq_daq_sm.vhd:316-324](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L316-L324) — `v.WinProtected := (others => '0');` 紧跟注释「No bursts where available on any stream, so all of them were checked and we can retry whether SW emptied a window.」；随后遍历各流，挑出一个 `HasLast` 且无在途命令且未被保护的流去冲刷帧末尾。

`TlastCheck_s` 在优先级链里的位置（`CheckPrio3_s` 失败后进入）：

[hdl/psi_ms_daq_daq_sm.vhd:306-314](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L306-L314) — `CheckPrio3_s` 的 else 分支 `v.State := TlastCheck_s;`。

#### 4.4.4 代码实践

**目标**：估算「保护—重试」循环的重试周期，理解它为何不会饿死被保护的流。

**步骤**：

1. 假设一个 4 流系统，只有流 0 被保护（`WinProtected[0]=1`），其余流都没有满突发数据（`InpDataAvail` 全 0），也没有 `HasLast` 残余、没有待处理响应。
2. 从 `Idle_s` 开始，逐拍跟踪 `State`：`Idle_s` → `CheckPrio1_s` → `CheckPrio2_s` → `CheckPrio3_s` → `TlastCheck_s`（清零 `WinProtected`）→ `CheckResp_s`（无响应）→ `Idle_s`。
3. 数一数从「被保护」到「`WinProtected` 被清零」经过多少拍，再算下一轮 `Idle_s` 重新仲裁时流 0 是否能被重新选中（前提是它的输入水位仍达标）。

**需要观察的现象 / 预期结果**：重试周期约等于「一轮优先级扫描 + Idle 仲裁延迟」的拍数（量级为个位数到十几拍，受 `ArbDelCnt` 仿真延迟影响）。只要软件在某次重试前把窗口 `MarkAsFree`（WINCNT 写 0），下一次 `ReadCtxWin_s` 读出的 `HndlWinBytes` 就是 0，`CalcAccess0_s` 放行，流恢复采集。这保证被保护的流**最终**一定会被重试，不会永久饿死——除非软件永远不释放窗口（那属于应用层死锁，不是硬件 bug）。**待本地验证**（建议在 TB 里把某窗口的 WINCNT 延迟若干拍再清 0，观察状态机何时恢复发命令）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `TlastCheck_s` 清零 `WinProtected` 是「全清」而不是「只清当前处理的那个流」？

**答案**：因为到达 `TlastCheck_s` 意味着所有优先级档都没有可服务的满突发，即所有流当前都没有命令可发。这是一个「全局静默」时刻，正是检查「有没有哪个流因为保护而被挡、但其实软件已经释放了窗口」的最佳时机。如果只清单个流，其他被保护的流要等更久才能重试；全清让所有被保护流在下一轮仲裁里一起被重新评估，公平且高效。

**练习 2**：`TlastCheck_s` 里挑选 HasLast 冲刷目标时，条件包含 `WinProtected[idx]==0`。但本状态开头刚刚把 `WinProtected` 全清成 0，这个条件岂不是永远成立？

**答案**：这里用的是 `r.WinProtected`（寄存器当前值，本拍进入状态前的值），而清零写的是 `v.WinProtected`（下一拍才生效）。所以循环里读到的 `r.WinProtected` 仍是上一拍的状态——如果某流刚被保护过，这一拍它仍被排除在 HasLast 冲刷之外，避免对一个已知不可写的流白白发起上下文读取。这是两进程法里「读 `r`、写 `v`」的典型细节。

---

### 4.5 FirstAfterEna/FirstOngoing：使能与禁用时的指针重置

#### 4.5.1 概念说明

前四节解决「窗口级别的数据竞争」。本节解决另一个一致性问题：**当一个流从「禁用」切到「使能」（或上电后首次使能）时，它上下文里的 PTR/WINEND/WINCUR 可能是上次运行留下的陈旧值，甚至是未定义初值。** 如果直接用，新一次采集会从一个错误地址开始写，可能破坏内存。

状态机用一对握手标志把这件事做严谨：

- `FirstAfterEna[str]`：**待办标志**。=1 表示「这个流被（重新）使能了，需要做一次首次访问重置」。它在流或全局使能为低时由禁用处理循环持续置 1。
- `FirstOngoing[str]`：**进行中标志**。=1 表示「首次访问（命令路径）正在执行，本轮要用重置后的指针」。

为什么需要**两个**标志而不是一个？因为一次完整传输横跨**命令路径**（`ReadCtxStr` → `First` → `ReadCtxWin` → `CalcAccess0/1` → 发命令）和**响应路径**（`ReadCtxStr` → `First` → `ReadCtxWin` → `ProcResp0` → `NextWin` → `WriteCtx`）两趟，两趟都经过 `First_s`。`FirstAfterEna` 在命令路径的 `First_s` 里被「消费」转成 `FirstOngoing`，并立即清零自己；这样即使传输中途流被再次禁用又使能，待办也不会丢失或重复施加。注释里写得很直白：「Ensure that command and response are both handled as first or not」。

#### 4.5.2 核心流程

```
禁用处理（每拍对所有流执行）:
  if (NOT GlbEna) OR (NOT StrEna[str]):
      FirstAfterEna[str] ← 1      # 标记「下次使能后要重置」
      NewBuffer[str]       ← 1      # 同时标记「待确认新缓冲」

First_s（命令路径，HndlAfterCtxt != ProcResp0_s）:
  FirstAfterEna[str] ← 0                      # 消费待办
  FirstOngoing[str]  ← FirstAfterEna[str]     # 待办转「进行中」
  if FirstOngoing[str]==1:                    # 本轮要重置
      HndlWinEnd ← bufstart + winSize
      HndlPtr0   ← bufstart
      HndlWincur ← 0
      Hndl4kMax  ← 4096 - bufstart[11:0]
      HndlWinMax ← winSize

First_s（响应路径，HndlAfterCtxt == ProcResp0_s）:
  （不做任何 First 相关处理）

ProcResp0_s（响应到达）:
  OpenCommand[str]   ← 0
  FirstOngoing[str]  ← 0      # 首次传输完成，清「进行中」
```

复位时 `FirstAfterEna` / `NewBuffer` 不在显式复位列表里，但因为 `GlbEnaReg` 复位为 0，禁用处理循环会在复位后第一拍就把它们都置 1——这就是上电自举。

#### 4.5.3 源码精读

`First_s` 的命令/响应分叉与重置（核心）：

[hdl/psi_ms_daq_daq_sm.vhd:376-395](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L376-L395) — 响应路径「nothing to do」；命令路径 `v.FirstAfterEna(...) := '0'; v.FirstOngoing(...) := r.FirstAfterEna(...);`，随后 `if v.FirstOngoing(...) = '1' then` 把 `HndlWinEnd/HndlPtr0/HndlWincur/Hndl4kMax/HndlWinMax` 重置到 `bufstart` 基准。

`ProcResp0_s` 清除在途命令与「首次进行中」：

[hdl/psi_ms_daq_daq_sm.vhd:459-461](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L459-L461) — `v.OpenCommand(...) := '0'; v.FirstOngoing(...) := '0';` 同时用 `Dma_Resp.Size` 推进指针。

禁用处理循环（每拍执行，置待办 + 待确认）：

[hdl/psi_ms_daq_daq_sm.vhd:561-567](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L561-L567) — `if (r.GlbEnaReg = '0') or (r.StrEnaReg(str) = '0') then v.FirstAfterEna(str) := '1'; v.NewBuffer(str) := '1';`

`p_seq` 复位：注意 `FirstAfterEna` 不在列表里，靠禁用循环自举；`FirstOngoing` 显式复位为 0：

[hdl/psi_ms_daq_daq_sm.vhd:620-625](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L620-L625) — `r.OpenCommand <= (others => '0'); r.WinProtected <= (others => '0'); ... r.FirstOngoing <= (others => '0');`

#### 4.5.4 代码实践

**目标**：验证 testbench 的 `enable` 用例确实覆盖了「禁用 → 重新使能后指针重置」。

**步骤**：

1. 打开 [tb/psi_ms_daq_daq_sm/psi_ms_daq_daq_sm_tb_case_enable.vhd](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_daq_sm/psi_ms_daq_daq_sm_tb_case_enable.vhd)，找到它在某次传输**中途**把 `StrEna` 或 `GlbEna` 拉低的片段。
2. 观察拉低期间 `FirstAfterEna` 被置 1（在波形上）；重新拉高使能后，下一次命令的 `Dma_Cmd.Address` 是否回到了 `bufstart`（而非上一轮的陈旧 PTR）。
3. 对照 [hdl/psi_ms_daq_daq_sm.vhd:389-394](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L389-L394)，确认重置后的地址就是 `bufstart`。

**需要观察的现象 / 预期结果**：重新使能后的第一条 DMA 命令地址 = `bufstart`，证明 `FirstAfterEna→FirstOngoing` 握手成功覆盖了陈旧 PTR。**待本地验证**（需要仿真器与该 TB）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `First_s` 在响应路径（`HndlAfterCtxt == ProcResp0_s`）里什么都不做？

**答案**：首次访问的指针重置只对「命令路径」有意义——命令路径负责发 DMA、决定写地址。响应路径只是处理已完成传输的记账（推进指针、累计字节数、切窗），用重置值去覆盖响应路径的指针反而会破坏正常的指针推进。所以 `FirstOngoing` 只在命令路径的 `First_s` 里被设置并生效，响应路径「什么都不做」正好保证了两条路径对「是不是首次」的一致处理。

**练习 2**：复位后 `FirstAfterEna` 的初值是什么？它如何变成 1？

**答案**：`FirstAfterEna` 不在 `p_seq` 的显式复位列表里，复位后初值为 std_logic 的默认值（综合时通常为 0，仿真为 'U'）。但复位时 `GlbEnaReg` 被强制为 0，于是复位释放后第一拍，禁用处理循环（[L562-566](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L562-L566）检测到 `GlbEnaReg=0`，把所有流的 `FirstAfterEna` 置 1。因此「全局使能尚未打开」这件事天然保证了首次使能后一定会做指针重置，无需为 `FirstAfterEna` 单独写复位。

---

## 5. 综合实践

把本讲的四个标志串起来，设计并分析下面这个端到端场景（这是本讲规格里要求的实践任务，**建议结合仿真波形完成**）。

**场景设置**：

- 单流（stream 0），`winOverwrite=false`，`winAsRingbuf=false`（线性），`Windows_g=2`（窗口 0、1），`winSize` 取一个能在两三次 DMA 内写满的值（例如 4096 字节，`MaxBurstSize_g=512` 个 64 位字 = 4096 字节）。
- 配置时驱动已把两个窗口的 WINCNT 都初始化为 0（空闲），见 [driver/psi_ms_daq.c:166](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L166) 的 `PsiMsDaq_RegWrite(... PSI_MS_DAQ_WIN_WINCNT(...), 0)`。
- 使能后持续输入数据。

**任务**：按时间顺序追踪下列事件链，逐拍/逐窗口记录 `OpenCommand[0]`、`WinProtected[0]`、`NewBuffer[0]`、当前 `Wincur`、下一条 DMA 命令的地址：

1. **首次使能**：复位后 `GlbEna` 拉高、`StrEna[0]` 拉高。指出 `FirstAfterEna[0]` 何时为 1，第一条命令地址为何等于 `bufstart`。
2. **写满窗口 0**：数据持续，指针到达 `WinEnd`。指出 `NextWin_s` 在哪一拍置 `NewBuffer[0]←1`、`Wincur` 从 0 变 1，并切到窗口 1。
3. **写满窗口 1**：同理绕回窗口 0（`Wincur` 回到 0），`NewBuffer[0]←1`。
4. **窗口 0 未释放**：此时软件**没有**调用 `MarkAsFree`，窗口 0 的 WINCNT 仍为非 0。状态机下一轮读到窗口 0 上下文，`HndlWinBytes≠0`。追踪 `CalcAccess0_s`：三条件全真 → `WinProtected[0]←1`，回 `Idle_s`，**不发命令**。
5. **保护生效**：指出 `WinProtected[0]=1` 如何通过 [L254-255](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L254-L255) 把流 0 从仲裁屏蔽掉，数据在输入 FIFO 里堆积（水位上涨）。
6. **重试循环**：因为没有其他流，状态机很快落到 `TlastCheck_s`，`WinProtected` 全清。下一轮重新读窗口 0 上下文——若软件仍未释放，再次保护；如此循环。
7. **软件释放**：在某次重试之间，软件调用 `MarkAsFree` 把窗口 0 的 WINCNT 写 0。下一次 `ReadCtxWin_s` 读出 `HndlWinBytes=0`，`CalcAccess0_s` 放行，`NewBuffer[0]←0`、`OpenCommand[0]←1`，采集恢复。

**对照实验**：把 `winOverwrite` 改为 `true`（重新配置、重新使能）。重复步骤 4。预期：`CalcAccess0_s` 的 `HndlOverwrite='1'` 使三条件短路为假，**直接放行**，窗口 0 的旧数据被覆盖，`WinProtected[0]` 永远不会被置位，也不会有「保护—重试」停顿。

**如何完成**：

- 有仿真环境：参考 [tb/psi_ms_daq_daq_sm/psi_ms_daq_daq_sm_tb_case_multi_window.vhd:118-128](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_daq_sm/psi_ms_daq_daq_sm_tb_case_multi_window.vhd#L118-L128)（`-- Linear without overwrite, no trigger`，case 2，`Overwrite=>'0'`）与 [tb/psi_ms_daq_daq_sm/psi_ms_daq_daq_sm_tb_case_multi_window.vhd:94-104](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_daq_sm/psi_ms_daq_daq_sm_tb_case_multi_window.vhd#L94-L104)（`-- Linear write with Overwrite`，case 0，`Overwrite=>'1'`），按 u1-l2 的 PsiSim/Modelsim 流程跑这两个 case，在波形上看 `r.OpenCommand`/`r.WinProtected`/`r.NewBuffer` 与 `Dma_Cmd.Address`。
- 无仿真环境：完成步骤 1–7 的「纸面推演」表格，把每个事件后的五个观测值填出来；这本身就是一次完整的源码阅读型实践。

> 关于 case 2 里「软件何时释放窗口」的细节：状态机 TB 用一个上下文模型（`ctx` 过程）来模拟软件释放窗口的行为。你可以阅读 `psi_ms_daq_daq_sm_tb_pkg` 里 `ctx` 过程对 WINCNT 的处理，理解 TB 是如何替「软件」在合适的时机把窗口 WINCNT 清 0 的——这正好对应真实系统里驱动 `MarkAsFree` 的效果。

## 6. 本讲小结

- 状态机用三个 per-stream 标志治理「硬件写 / 软件读」的数据竞争：`OpenCommand`（在途命令互斥）、`WinProtected`（当前窗口不可写）、`NewBuffer`（当前窗口是待确认的新缓冲）。三者叠加成 `DataAvailArbIn`（屏蔽 OpenCommand+WinProtected）与 `DataPending`（只屏蔽 WinProtected）两条屏蔽向量。
- `CalcAccess0_s` 的保护判定是三个条件的与：`NOT Overwrite AND WinBytes≠0 AND NewBuffer=1`。`winOverwrite=false` 时它会在「窗口已满且未释放」时置 `WinProtected` 并中止命令；`winOverwrite=true` 时短路放行，直接覆盖旧数据。
- `NewBuffer` 由 `NextWin_s` 在「线性写满或触发封口」时置位；环形缓冲的「窗口内回绕」不置 `NewBuffer`、不封口、不发中断。
- `TlastCheck_s` 在「所有流都无满突发可服务」时**全清** `WinProtected`，给被保护的流一次重新读 WINCNT、重新判定的机会，形成「保护—等待—重试」轮询，保证被保护的流最终会被重试。
- `FirstAfterEna`（待办）/ `FirstOngoing`（进行中）这对握手保证流在（重新）使能后把 PTR/WINEND/WINCUR 重置回 `bufstart`，且命令路径与响应路径对「是否首次」处理一致；禁用处理循环每拍把禁用流的 `FirstAfterEna` 和 `NewBuffer` 都置 1，复位后靠 `GlbEnaReg=0` 自举。

## 7. 下一步学习建议

- **回到 testbench**：本讲的多个结论都可以在 `tb/psi_ms_daq_daq_sm/` 下验证。建议接着读 u5-l2（模块级 testbench 实例分析），重点看 `psi_ms_daq_daq_sm_tb_case_multi_window.vhd` 的四种 `(Ringbuf, Overwrite)` 组合如何被设计成对照实验。
- **软件侧闭环**：本讲的 `MarkAsFree` 是 u4-l2 讲过的驱动函数。可以把本讲的「保护—重试」循环与 u4-l2 的「窗口回调 + MarkAsFree」对照阅读，理解软硬件协定的两面：硬件承诺「不覆盖未释放窗口」，软件承诺「及时释放窗口避免采集停顿」。
- **中断关联**：`winOverwrite=false` 下「窗口完成」会触发 `NextWin_s` 写 IRQ FIFO（u4-l1）。读完本讲后，可以回头理顺「窗口完成 → NewBuffer←1 → IRQ 入队 → 软件回调 → MarkAsFree → WINCNT=0 → TlastCheck 重试放行」这条完整的端到端链路。
- **进阶思考**：如果应用层既不想丢数据、又担心 `winOverwrite=false` 导致采集停顿，工程上通常怎么权衡窗口数量 `Windows_g` 与软件处理延迟？这是把本讲机制推向系统设计层面的切入点。
