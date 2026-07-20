# 控制状态机：窗口切换、环形缓冲与上下文回写

## 1. 本讲目标

本讲是控制状态机 `psi_ms_daq_daq_sm` 的第三部分，承接 [u3-l2](u3-l2-sm-context-calcaccess.md) 讲完的「命令路径」（读上下文 → 计算 DMA 地址与长度），转而讲解**响应路径**：DMA 一次传输完成后，状态机如何更新写指针、判断是否要切换窗口、在环形缓冲与线性缓冲两种模式下分别如何回绕，最后把所有变化写回上下文存储。

学完本讲，你应当能够：

- 说清 `ProcResp0_s` 如何用 DMA 响应里的 `Size` 推进指针、累计本窗口已写字节数；
- 准确判断 `NextWin_s` 在什么条件下「关闭当前窗口、跳到下一个窗口」，以及在「线性缓冲」与「环形缓冲」两种模式下指针回绕轨迹的差异；
- 解释 `HndlWinLast`（末样地址）和 `HndlTs`（时间戳）的锁存条件与用途；
- 跟踪 `WriteCtx_s` 把哪些字段（采样数、末样地址、触发标志、时间戳、新指针、新窗口末地址）分别写回流上下文与窗口上下文存储。

本讲只覆盖响应路径的三个状态（`ProcResp0_s`、`NextWin_s`、`WriteCtx_s`）及相关计算；命令路径请看 [u3-l2](u3-l2-sm-context-calcaccess.md)，仲裁与调度请看 [u3-l1](u3-l1-sm-overview-arbitration.md)，窗口保护协议（`NewBuffer`/`WinProtected`/`winOverwrite`）的完整语义在 [u4-l5](u4-l5-window-protection-overwrite.md) 展开。

## 2. 前置知识

在进入源码前，先用一张图把「窗口」这个概念在内存里的物理形态画清楚。一个流在 DDR 里被分配了一段连续区域，起点是 `bufstart`，长度是 `winSize`。这段区域可能被切成多个等长的**窗口（window）**，编号从 0 到 `winCnt-1`：

```
DDR 地址:   bufstart        +winSize         +2*winSize        +3*winSize
             |===窗口 0====|====窗口 1====|====窗口 2====| ...
             ^ptr(写指针)   ^winEnd(窗口0末)
```

- `ptr`：当前写指针，下一次 DMA 从这里往 DDR 写。
- `winEnd`：当前窗口的结束地址（即下一个窗口的起点），`winEnd = bufstart + winSize` 在窗口 0 时成立。
- `wincur`：当前写到第几个窗口（窗口编号）。
- `wincnt`：窗口总数减一（最后一个窗口的编号），见 [u3-l4](u3-l4-context-memory-model.md) 与驱动里「WINCNT 写 winCnt-1」的约定。

数据采集时，硬件持续把样本写入当前窗口。本讲要回答的核心问题是：**一次 DMA 结束后，写指针该指向哪里？当前窗口要不要「封口」并切换到下一个？**

这里有两种截然不同的缓冲策略，由流上下文里的 `RINGBUF` 位（`HndlRingbuf`）选择：

- **线性缓冲（`HndlRingbuf = '0'`）**：写满一个窗口就关闭它、跳到下一个窗口；旧窗口交给软件读取后释放。
- **环形缓冲（`HndlRingbuf = '1'`）**：写满一个窗口**不切换**，而是把写指针绕回本窗口起点继续覆盖写，直到一个**触发**到来才关闭这个窗口。这种方式能保证触发前后一段历史数据都在同一个窗口里，便于软件事后「解环形」取出完整的触发帧。

> 术语提示：本讲里「窗口完成 / 封口」对应代码里的 `HndlWinDone`，表示一个窗口的数据已经齐整、可以通知软件来读了。而「触发」`Dma_Resp.Trigger` 来自输入逻辑（[u2-l3](u2-l3-input-modes-trigger.md)），表示这一帧是因为触发而结束的。

另外回顾 [u3-l2](u3-l2-sm-context-calcaccess.md) 的结尾：状态机读上下文后用寄存器 `HndlAfterCtxt` 决定「分流到命令路径还是响应路径」。响应路径的入口是 `CheckResp_s`，它在发现有 DMA 响应待处理时，把 `HndlAfterCtxt` 设为 `ProcResp0_s`，并锁存 `EndByTrig := Dma_Resp.Trigger`：

[hdl/psi_ms_daq_daq_sm.vhd:326-335](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L326-L335) —— 检测到 DMA 响应有效时，路由到响应路径 `ProcResp0_s`，并把触发标志存入 `EndByTrig` 供后续写窗口上下文使用。

注意 `HndlAfterCtxt` 这个机制让「读上下文」子链既能服务于命令路径（`CalcAccess0_s`），也能服务于响应路径（`ProcResp0_s`）：进入 `ProcResp0_s` 前，状态机会先把该流与该窗口的上下文重新读一遍，这样 `HndlPtr0`、`HndlWinEnd`、`HndlWinBytes` 等寄存器都装载了**当前最新**的值，下面的响应处理才是基于正确初值的。

## 3. 本讲源码地图

本讲只涉及一个源文件，但会用到包里的若干常量：

| 文件 | 作用 |
| --- | --- |
| [hdl/psi_ms_daq_daq_sm.vhd](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd) | 控制状态机主体，本讲聚焦其中的 `ProcResp0_s`、`NextWin_s`、`WriteCtx_s` 三个状态 |
| [hdl/psi_ms_daq_pkg.vhd](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_pkg.vhd) | 提供上下文访问记录类型与选择常量（`CtxStr_Sel_*`、`CtxWin_Sel_*`、`CtxStr_Sft_*`），用于解释回写的字段位置 |

涉及的关键寄存器（都在 `two_process_r` 记录里声明）一览，便于对照：

| 寄存器 | 宽度 | 含义 |
| --- | --- | --- |
| `HndlPtr0` | 32 | 本次传输**前**的写指针（DMA 起始地址） |
| `HndlPtr1` | 32 | 本次传输**后**的写指针（`HndlPtr0 + Size`） |
| `HndlPtr2` | 32 | 回绕/切换后的最终写指针，写回流上下文 `PTR` |
| `HndlWinEnd` | 32 | 当前窗口结束地址 |
| `HndlWincur` | log2(Windows) | 当前窗口编号 |
| `HndlWincnt` | log2(Windows) | 最后一个窗口编号（窗口总数 − 1） |
| `HndlWinBytes` | 33 | 本窗口已写字节数（带 1 位保护位，防溢出） |
| `HndlWinSize` | 32 | 单个窗口字节数 |
| `HndlBufstart` | 32 | 本流 DDR 区域起点 |
| `HndlRingbuf` | 1 | 环形 / 线性缓冲选择 |
| `HndlWinDone` | 1 | 本拍是否「关闭」了当前窗口 |
| `HndlWinLast` | 32 | 本窗口最后一个样本的地址 |
| `HndlTs` | 64 | 锁存的时间戳 |
| `EndByTrig` | 1 | 本次传输是否因触发结束（在 `CheckResp_s` 锁存） |

## 4. 核心概念与源码讲解

### 4.1 ProcResp0_s：DMA 响应处理与指针推进

#### 4.1.1 概念说明

DMA 引擎（[u2-l6](u2-l6-dma-statemachine-alignment.md)）每完成一次 DDR 写入，就向控制状态机回送一个响应 `DaqDma2DaqSm_Resp_t`，其中两个字段最关键：

- `Size`：本次实际写入 DDR 的**字节数**（受 `MaxSize`、窗口末、4KB 边界裁剪后的真实长度）；
- `Trigger`：本次传输是否承载了一个触发帧的结尾（`RspFifo_Data.Trigger`，见 [u2-l6](u2-l6-dma-statemachine-alignment.md)）。

`ProcResp0_s` 的职责很纯粹：用 `Size` 把写指针向前推一格、把本窗口已写字节数累加上去，并清掉「在途命令」标记，然后交给 `NextWin_s` 去决定窗口命运。

#### 4.1.2 核心流程

```
进入 ProcResp0_s（已读回该流/窗口最新上下文）:
  1. OpenCommand(stream)  <= '0'   // 本次命令的响应已到，命令不再「在途」
  2. FirstOngoing(stream) <= '0'   // 首次访问流程结束
  3. HndlPtr1   := HndlPtr0 + Size // 传输后的新写指针
  4. HndlWinBytes := HndlWinBytes + Size  // 本窗口已写字节累加
  5. 跳转 NextWin_s
```

其中第 3、4 步是核心数学关系。设传输前指针为 \( p_0 \)、本次写入 \( s \) 字节，则传输后指针：

\[
p_1 = p_0 + s
\]

本窗口累计字节（带 1 位保护位，所以是 33 位）：

\[
B_{\text{win}} \leftarrow B_{\text{win}} + s
\]

为什么 `HndlWinBytes` 要 33 位（比 32 位的 `HndlWinSize` 多一位）？因为累加后可能瞬间溢出 `HndlWinSize`，多出的保护位让 `NextWin_s` 能做「钳位到最大值」的比较（见 4.2.3），而不至于回绕成一个很小的数。

#### 4.1.3 源码精读

[hdl/psi_ms_daq_daq_sm.vhd:459-465](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L459-L465) —— `ProcResp0_s`：清除在途命令与首次访问标记，用 `Dma_Resp.Size` 同时推进写指针 `HndlPtr1` 与累加本窗口字节 `HndlWinBytes`，然后进入 `NextWin_s`。

逐行说明：

- `v.OpenCommand(r.HndlStream) := '0'`：这条 DMA 命令的响应已经收到，从「在途」集合里移除。`OpenCommand` 在 [u3-l1](u3-l1-sm-overview-arbitration.md) 里用于阻止「同一流同时挂多条未完成命令」，这里释放它。
- `v.FirstOngoing(r.HndlStream) := '0'`：首次访问握手（[u3-l2](u3-l2-sm-context-calcaccess.md) 的 `FirstAfterEna`/`FirstOngoing`）在响应回来时清零。
- `v.HndlPtr1 := HndlPtr0 + Dma_Resp.Size`：算出传输后的指针。注意这里用的是 `r.HndlPtr0`（传输前指针，命令路径里装配的 `Dma_Cmd.Address` 就是它）。
- `v.HndlWinBytes := HndlWinBytes + Dma_Resp.Size`：本窗口字节累加。`HndlWinBytes` 的初值来自读窗口上下文时把 `WINCNT`（采样数）左移回字节（见 [u3-l2](u3-l2-sm-context-calcaccess.md) 的 `ReadCtxWin_s`），所以它代表「本窗口此前已写的字节数 + 本次写的字节数」。

> 小结：`ProcResp0_s` 只做加法与清标记，不做任何「要不要换窗口」的判断——那是 `NextWin_s` 的事。这种职责分离让响应路径的逻辑非常清晰。

#### 4.1.4 代码实践

**实践目标**：通过阅读源码与跟踪寄存器，验证「写指针推进」与「窗口字节累加」的数学关系，并理解保护位的作用。

**操作步骤**：

1. 打开 [hdl/psi_ms_daq_daq_sm.vhd:459-465](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L459-L465)，确认 `HndlPtr1` 与 `HndlWinBytes` 都只依赖 `Dma_Resp.Size`。
2. 假设某次传输：`HndlPtr0 = 0x0000_1000`、`HndlWinBytes = 0x0200`（此前已写 512 字节）、`Dma_Resp.Size = 0x0400`（本次 1024 字节）。
3. 手算 `HndlPtr1` 与 `HndlWinBytes` 的新值。

**需要观察的现象 / 预期结果**：

- `HndlPtr1 = 0x0000_1000 + 0x0400 = 0x0000_1400`；
- `HndlWinBytes = 0x0200 + 0x0400 = 0x0600`（即本窗口此时累计 1536 字节，仍在 33 位表示范围内，无溢出）。

**进一步思考**：若 `HndlWinSize = 0x0400`（窗口仅 1024 字节），而 `HndlWinBytes` 累加到 `0x0600` 已超过窗口大小——这一步并不报错，而是留给 `NextWin_s` 第 495–497 行做钳位（见 4.2.3）。请记住这个「先溢出、后钳位」的设计，它正是 `HndlWinBytes` 取 33 位的理由。

（结果待本地验证：可在 `tb/psi_ms_daq_daq_sm/` 下用仿真把 `Dma_Resp.Size` 与窗口大小设成上述值，观察波形里 `HndlPtr1`、`HndlWinBytes` 的变化。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `OpenCommand(stream)` 必须在 `ProcResp0_s`（响应路径）里清除，而不是在发出命令时就清？

**参考答案**：`OpenCommand` 表示「该流当前有一条 DMA 命令在途、尚未收到响应」。它的作用是在命令还没完成时阻止同一流再发新命令（避免地址/上下文竞争）。只有响应回来、传输真正结束，才能清除。若在发命令时清，就失去了「在途保护」的意义。

**练习 2**：`HndlPtr1` 用的是 `r.HndlPtr0`，而 `HndlPtr0` 是命令路径里装配的 `Dma_Cmd.Address`。如果 DMA 实际写入的字节数与命令申请的 `MaxSize` 不一致（被窗口末或 4KB 边界裁短了），这里会不会出错？

**参考答案**：不会。`HndlPtr1` 用的是 `Dma_Resp.Size`（**实际**写入字节数），而不是命令里的 `MaxSize`（申请的上限）。DMA 引擎因边界裁剪实际写得更少时，`Size` 反映真实值，指针推进量与真实写入量一致。

---

### 4.2 NextWin_s：窗口切换与线性/环形回绕

这是本讲的核心状态，所有「换不换窗口、指针绕到哪」的判断都集中在这里。

#### 4.2.1 概念说明

`NextWin_s` 要回答三个问题：

1. **这次传输是否要把一个 IRQ 事件入队？**（非零传输才入队）
2. **当前窗口是否「完成」了？** 即要不要关闭它、切到下一个窗口（或回绕）。
3. **写指针最终落到哪里？**（`HndlPtr2`）

「窗口完成」有两种触发条件，任一成立即关闭当前窗口：

- **(A) 写满窗口**：`HndlPtr1 = HndlWinEnd`（写指针正好顶到窗口末地址）；
- **(B) 触发结束**：`Dma_Resp.Trigger = '1'`（本帧因触发结束，无论是否写满）。

但条件 (A) 在「线性缓冲」与「环形缓冲」下的处理完全不同，这是本模块最关键的分叉：

| 模式 | 写满窗口 (`HndlPtr1 = HndlWinEnd`) 且无触发时的行为 |
| --- | --- |
| 线性 (`HndlRingbuf='0'`) | **关闭当前窗口，切到下一个窗口**（`HndlWinDone=1`，`wincur` 递增或回 0） |
| 环形 (`HndlRingbuf='1'`) | **不关闭窗口**，写指针绕回本窗口起点继续覆盖写（`HndlPtr2 = HndlPtr1 − winSize`） |

而**触发结束**（条件 B）在两种模式下都关闭当前窗口——因为触发意味着「这一帧数据齐了，软件可以来读」，必须封口。

#### 4.2.2 核心流程

`NextWin_s` 的判定逻辑用伪代码表示（对照源码 468–512 行）：

```
HndlPtr2 := HndlPtr1                       // 默认：留在本窗口继续写

if Size /= 0:
    IrqFifoWrite := '1'                    // 非零传输才入队 IRQ 事件

HndlLastWinNr := HndlWincur                // 记下「刚处理的窗口号」

// —— 判定 1：是否「关闭当前窗口」——
if (HndlPtr1 = HndlWinEnd AND HndlRingbuf='0')  // 线性写满
   OR (Dma_Resp.Trigger = '1'):                 // 或触发结束
    HndlWinDone := 1
    NewBuffer(stream) := 1
    if HndlWincur = HndlWincnt:                 // 已是最后一个窗口
        HndlWincur := 0
        HndlPtr2   := HndlBufstart
        HndlWinEnd := HndlBufstart + HndlWinSize
    else:                                       // 还有后续窗口
        HndlWincur := HndlWincur + 1
        HndlPtr2   := HndlWinEnd                // 下一窗口起点 = 当前窗口末
        HndlWinEnd := HndlWinEnd + HndlWinSize

// —— 判定 2：环形缓冲的指针回绕（独立于判定 1）——
if (HndlPtr1 = HndlWinEnd) AND (HndlRingbuf='1') AND (Trigger='0'):
    HndlPtr2 := HndlPtr1 - HndlWinSize          // 绕回本窗口起点

// —— 钳位本窗口字节 ——
if HndlWinBytes > HndlWinSize:
    HndlWinBytes := '0' & HndlWinSize           // 不超过窗口大小
```

判定 1 与判定 2 之所以**分开写**，是因为它们处理的是两种不同的「回绕」：

- 判定 1 的回绕是**跨窗口**的（线性写满 → 换下一个窗口，必要时从最后一个窗口绕回窗口 0）；
- 判定 2 的回绕是**窗口内**的（环形写满 → 绕回本窗口起点，窗口不切换）。

注意二者互斥：判定 1 要求 `HndlRingbuf='0'`，判定 2 要求 `HndlRingbuf='1'`，所以同一次响应只会触发其中一个分支。

#### 4.2.3 源码精读

[hdl/psi_ms_daq_daq_sm.vhd:468-489](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L468-L489) —— `NextWin_s` 主体：默认指针、非零传输入队 IRQ、记录刚处理窗口号，以及判定 1（关闭窗口 + 跨窗口推进/回绕）。

逐段说明：

- `v.HndlPtr2 := r.HndlPtr1`（470 行）：默认把最终指针设为传输后指针，即「不换窗口、继续往后写」。后面若有切换或环形回绕，会覆盖这个默认值。
- 非零传输入队（472–474 行）：

  ```vhdl
  if unsigned(Dma_Resp.Size) /= 0 then
    v.IrqFifoWrite := '1';
  end if;
  ```

  零长度传输不会真正到达内存接口（[u2-l7](u2-l7-axi-master-interface.md) 只在有数据时发 AXI 写），所以不为它产生 IRQ/Done 事件，避免虚假中断。`IrqFifoIn` 的拼装在 683–685 行，把 `HndlStream`、`HndlLastWinNr`、`HndlWinDone` 打包成一条记录入队。

- `v.HndlLastWinNr := r.HndlWincur`（476 行）：在切换前记下「刚处理完的是哪个窗口」，供 IRQ FIFO 与 `StrLastWin` 输出使用（中断里告诉 CPU 哪个窗口完成了）。
- 判定 1 的条件（477 行）：

  ```vhdl
  if ((r.HndlPtr1 = r.HndlWinEnd) and (r.HndlRingbuf = '0')) or (Dma_Resp.Trigger = '1') then
  ```

  即「线性模式写满窗口」或「触发结束」。成立时置 `HndlWinDone`、`NewBuffer`，然后看 `HndlWincur` 是否等于 `HndlWincnt`（最后一个窗口）：
  - 相等（480–483 行）：从最后一个窗口绕回窗口 0，`HndlPtr2` 回到 `HndlBufstart`，`HndlWinEnd` 重算为 `HndlBufstart + HndlWinSize`。
  - 不等（484–488 行）：进入下一个窗口，`HndlWincur + 1`，`HndlPtr2` 取**旧的** `HndlWinEnd`（即下一窗口的起点），`HndlWinEnd` 再加一个 `HndlWinSize`。

  这里的几何关系很清晰：窗口在 DDR 里首尾相接，第 \(k\) 个窗口的起点是 `bufstart + k·winSize`，末地址就是下一个窗口的起点。

[hdl/psi_ms_daq_daq_sm.vhd:491-493](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L491-L493) —— 判定 2：环形缓冲的窗口内回绕。

```vhdl
if (r.HndlPtr1 = r.HndlWinEnd) and (r.HndlRingbuf = '1') and (Dma_Resp.Trigger = '0') then
  v.HndlPtr2 := std_logic_vector(unsigned(r.HndlPtr1) - unsigned(r.HndlWinSize));
end if;
```

环形模式下写满窗口、且**不是**因触发结束（触发会走判定 1 关闭窗口），就把指针减去一个窗口大小，绕回本窗口起点。因为环形窗口的地址范围就是 \([winEnd - winSize,\ winEnd)\)，即 \([bufstart,\ bufstart+winSize)\)，所以：

\[
p_2 = p_1 - winSize = (bufstart + winSize) - winSize = bufstart
\]

正好回到窗口起点，下一拍从那里继续覆盖写。这就是「环形」二字的物理含义。

[hdl/psi_ms_daq_daq_sm.vhd:495-497](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L495-L497) —— 本窗口字节钳位。

```vhdl
if unsigned(r.HndlWinBytes) > unsigned(r.HndlWinSize) then
  v.HndlWinBytes := '0' & r.HndlWinSize;
end if;
```

由于一次传输可能把 `HndlWinBytes` 顶到略超 `winSize`（比如窗口只剩 100 字节，但 DMA 一次突发写了 512 字节里的前 100 字节就触底），这里把它钳到不超过窗口大小。`'0' & r.HndlWinSize` 是 33 位（1 位保护位 + 32 位值），与 `HndlWinBytes` 同宽。这个钳位后的值会被 `WriteCtx_s` 转成采样数写回 `WINCNT`（见 4.4）。

#### 4.2.4 代码实践

**实践目标**：对比「到达窗口末尾但非触发」在 `HndlRingbuf=true` 与 `HndlRingbuf=false` 两种情况下，`HndlPtr2`、`HndlWincur`、`HndlWinEnd` 分别如何更新，并画出指针在窗口内的回绕轨迹。

**操作步骤**：

1. 设定一组公共初值（单位均为字节，地址用十六进制）：

   ```
   HndlBufstart = 0x0000_0000
   HndlWinSize  = 0x0000_1000   (4 KiB)
   HndlWinEnd   = 0x0000_1000   (= bufstart + winSize，即窗口 0 末)
   HndlWincur   = 0x0           (当前窗口 0)
   HndlWincnt   = 0x1           (共 2 个窗口：0 和 1)
   HndlPtr0     = 0x0000_0C00
   Dma_Resp.Size    = 0x0000_0400   (本次写 1024 字节)
   Dma_Resp.Trigger = '0'           (非触发结束)
   ```

   先在 `ProcResp0_s` 算出 `HndlPtr1 = HndlPtr0 + Size = 0x0C00 + 0x0400 = 0x1000 = HndlWinEnd`，即恰好写满窗口 0、且非触发。

2. 分别代入两种模式，手算 `NextWin_s` 结束后 `HndlPtr2`、`HndlWincur`、`HndlWinEnd` 的值。

**情况 A：线性缓冲（`HndlRingbuf = '0'`）**

- 判定 1 条件 `(HndlPtr1 = HndlWinEnd) and (HndlRingbuf='0')` = `(0x1000=0x1000) and '1'` = **真**；触发为 0，但 OR 第一项已真。
  - `HndlWinDone := 1`，`NewBuffer(stream) := 1`，`HndlLastWinNr := 0`。
  - `HndlWincur(0) = HndlWincnt(1)`? 不等 → else 分支：
    - `HndlWincur := 0 + 1 = 1`
    - `HndlPtr2 := HndlWinEnd = 0x1000`（下一窗口起点）
    - `HndlWinEnd := 0x1000 + 0x1000 = 0x2000`
- 判定 2 条件含 `HndlRingbuf='1'`，本情况为 0 → **不执行**。

  结果：

  | 寄存器 | 值 |
  | --- | --- |
  | `HndlPtr2` | `0x0000_1000`（窗口 1 起点） |
  | `HndlWincur` | `0x1`（切到窗口 1） |
  | `HndlWinEnd` | `0x0000_2000`（窗口 1 末） |
  | `HndlWinDone` | `1`（窗口 0 已封口，待软件读取） |

  回绕轨迹（指针跨窗口推进）：

  ```
  窗口0 [0x0000 ─────────── 0x1000) │ 窗口1 [0x1000 ───── 0x2000)
                    写满→封口 ──────►^ptr 落在窗口1起点
  ```

**情况 B：环形缓冲（`HndlRingbuf = '1'`）**

- 判定 1 条件 `(HndlPtr1 = HndlWinEnd) and (HndlRingbuf='0')` = `... and '0'` = **假**；触发也为 0 → 整个判定 1 **不成立**。
  - `HndlWinDone` 保持 0、`NewBuffer` 不变、`HndlWincur` 保持 0、`HndlWinEnd` 保持 `0x1000`。
- 判定 2 条件 `(HndlPtr1 = HndlWinEnd) and (HndlRingbuf='1') and (Trigger='0')` = `'1' and '1' and '1'` = **真**：
  - `HndlPtr2 := HndlPtr1 - HndlWinSize = 0x1000 - 0x1000 = 0x0000`（本窗口起点）

  结果：

  | 寄存器 | 值 |
  | --- | --- |
  | `HndlPtr2` | `0x0000_0000`（绕回窗口 0 起点） |
  | `HndlWincur` | `0x0`（不变，仍在窗口 0） |
  | `HndlWinEnd` | `0x0000_1000`（不变） |
  | `HndlWinDone` | `0`（窗口未封口，继续覆盖写） |

  回绕轨迹（指针在窗口内绕圈）：

  ```
  窗口0 [0x0000 ─────────── 0x1000)
     ^◄────────写满后绕回──────^ (HndlPtr1 顶到 0x1000，HndlPtr2 回到 0x0000)
     ptr 下一拍从这里继续覆盖写
  ```

**需要观察的现象 / 预期结果**：两种模式下，**同样的「写满窗口、非触发」输入**，线性模式把数据流导向下一个窗口并封口当前窗口；环形模式则把写指针拉回本窗口起点继续转圈、不封口。这正是两种缓冲策略服务于不同采集需求（顺序记录 vs. 触发前后历史）的根本机制。

（结果待本地验证：可参考 `tb/psi_ms_daq_daq_sm/psi_ms_daq_daq_sm_tb_case_multi_window.vhd` 等 case 文件，构造上述初值，在波形里对照 `HndlPtr2`/`HndlWincur`/`HndlWinEnd`。）

#### 4.2.5 小练习与答案

**练习 1**：如果把判定 2 的条件里 `(Dma_Resp.Trigger = '0')` 去掉，环形缓冲在「写满窗口同时正好触发」时会发生什么？

**参考答案**：判定 1 会因 `Trigger='1'` 成立而关闭窗口、切到下一个窗口（`HndlPtr2` 设为下一窗口起点）。若判定 2 不再排除触发，它也会同时把 `HndlPtr2` 改写成「绕回本窗口起点」，**覆盖**掉判定 1 设的下一窗口起点（因为判定 2 在源码里位于判定 1 之后，后写胜出）。结果是窗口被标记为完成、`wincur` 已推进，但写指针却指向旧窗口——状态不一致。所以排除触发是必要的：触发时只走判定 1 的「封口+换窗」路径。

**练习 2**：当 `HndlWincur = HndlWincnt`（已是最后一个窗口）且线性写满时，为什么要把 `HndlWinEnd` 重算为 `HndlBufstart + HndlWinSize`？

**参考答案**：因为绕回了窗口 0，`HndlWinEnd` 必须对应**窗口 0** 的末地址，即 `bufstart + winSize`。若不重算，`HndlWinEnd` 仍停留在最后一个窗口的末地址，下一次比较 `HndlPtr1 = HndlWinEnd` 就会出错，导致永远判定不到「写满」。

---

### 4.3 HndlWinLast 与 HndlTs：末样地址与时间戳锁存

#### 4.3.1 概念说明

`NextWin_s` 在算完指针之后，还顺带计算两个「附加信息」供写回窗口上下文：

- **末样地址 `HndlWinLast`**：本次传输后，本窗口里**最后一个样本**所在的 DDR 地址。它等于传输后指针 `HndlPtr1` 减去一个样本的字节数（`StreamWidth_g/8`）。驱动在环形缓冲模式下正是用这个地址来「解环形」——从末样地址倒推触发前后的数据范围（详见 [u4-l3](u4-l3-driver-data-unwrap.md) 的 `GetDataUnwrapped`）。
- **时间戳 `HndlTs`**：仅当本次传输**因触发结束**（`Dma_Resp.Trigger='1'`）且该流**当前有时间戳有效**（`Ts_Vld='1'`）时，才锁存输入逻辑送来的 `Ts_Data`；否则写一个全 1 的「无效标记」`0xFFFF...FFFF`。

> 术语提示：时间戳来自输入逻辑的时间戳 FIFO（[u2-l4](u2-l4-input-timeout-timestamp.md)），只在触发帧时有效。`Ts_Vld`/`Ts_Rdy` 是状态机与输入逻辑之间的握手：状态机拉高 `Ts_Rdy` 表示「我收下了这个时间戳」。

#### 4.3.2 核心流程

```
// 末样地址：传输后指针回退一个样本
HndlWinLast := HndlPtr1 - StreamWidth_g(stream)/8

// 时间戳锁存
if (Dma_Resp.Trigger = '1') and (Ts_Vld(stream) = '1'):
    Ts_Rdy(stream) := '1'          // 握手：收下时间戳
    HndlTs := Ts_Data(stream)
else:
    HndlTs := (others => '1')      // 无效标记
```

末样地址的几何意义：传输把数据写到 \([p_0,\ p_1)\)，最后一个样本落在 \(p_1 - \text{sampleBytes}\) 处：

\[
\text{lastAddr} = p_1 - \frac{\text{StreamWidth}}{8}
\]

#### 4.3.3 源码精读

[hdl/psi_ms_daq_daq_sm.vhd:499](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L499) —— 末样地址计算。

```vhdl
v.HndlWinLast := std_logic_vector(unsigned(r.HndlPtr1) - StreamWidth_g(r.HndlStream) / 8);
```

注意用的是**每流**的 `StreamWidth_g(HndlStream)`（流宽度是数组型 generic，见 [u1-l3](u1-l3-toplevel-generics-ports.md)），所以 8 位流回退 1 字节、64 位流回退 8 字节，与该流样本实际大小一致。

[hdl/psi_ms_daq_daq_sm.vhd:501-506](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L501-L506) —— 时间戳锁存与无效标记。

```vhdl
if (Dma_Resp.Trigger = '1') and (Ts_Vld(r.HndlStream) = '1') then
  v.Ts_Rdy(r.HndlStream) := '1';
  v.HndlTs               := Ts_Data(r.HndlStream);
else
  v.HndlTs := (others => '1');
end if;
```

要点：

- 只有触发帧才锁存时间戳——这与输入逻辑「时间戳只在触发帧锁存」（[u2-l4](u2-l4-input-timeout-timestamp.md)）一一对齐。
- `Ts_Rdy` 拉高完成握手，输入逻辑据此出队一个时间戳。
- 无效情况（非触发、或触发但当前无有效时间戳）写全 1，与输入逻辑「时间戳 FIFO 无数据时输出全 1」的无效约定保持一致，软件可据此判断该窗口是否有有效时间戳。

[hdl/psi_ms_daq_daq_sm.vhd:507-511](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L507-L511) —— `NextWin_s` 收尾：跳转 `WriteCtx_s`、复位上下文写计数器、置响应就绪。

```vhdl
v.State        := WriteCtx_s;
v.HndlCtxCnt   := 0;
v.Dma_Resp_Rdy := '1';
```

`Dma_Resp_Rdy := '1'` 表示「响应处理完毕，可以接收下一个响应」，它会被 DMA 引擎看到，从而允许下一拍送来新的 `Dma_Resp`。

#### 4.3.4 代码实践

**实践目标**：手算末样地址，理解它如何被驱动用来定位环形缓冲里的数据。

**操作步骤**：

1. 承接 4.2.4 的公共初值，但设 `StreamWidth_g(stream) = 32` 位（即每样本 4 字节）。
2. 已知 `HndlPtr1 = 0x1000`，手算 `HndlWinLast`。

**预期结果**：

\[
\text{HndlWinLast} = 0x1000 - 32/8 = 0x1000 - 4 = 0x0FFC
\]

即本窗口最后一个样本落在 `0x0FFC`。在环形模式下，驱动 `GetDataUnwrapped` 会以这个地址为锚点，向前取 `preTrig` 个样本、向后取 `postTrig` 个样本，并在窗口内做环形回绕解包（[u4-l3](u4-l3-driver-data-unwrap.md)）。

**进一步思考**：若该流配置了 `StreamUseTs_g = false`（[u2-l4](u2-l4-input-timeout-timestamp.md)），`Ts_Vld` 永远为 0，那么 `HndlTs` 恒为全 1，写回的窗口时间戳就是无效标记——软件读到全 1 即知该流不带时间戳。

#### 4.3.5 小练习与答案

**练习 1**：末样地址用 `HndlPtr1`（传输后指针）减一个样本，而不是用 `HndlPtr2`（回绕后的指针）。为什么？

**参考答案**：`HndlPtr2` 可能已经被环形回绕或窗口切换改写过（比如绕回窗口起点），用它减样本会得到一个与「实际最后写入位置」无关的地址。而 `HndlPtr1` 是本次传输**真实**结束的位置（`= HndlPtr0 + 实际 Size`），回退一个样本正好是本窗口最后一个样本的真实落点，这才是软件解环形时需要的锚点。

**练习 2**：为什么时间戳的无效标记选全 1，而不是选 0？

**参考答案**：因为时间戳 `0` 是一个合法的时间值（开机时刻），无法与「无效」区分；而全 1（`0xFFFF...FFFF`）是一个极大的、几乎不可能作为真实时间戳出现的值，输入逻辑侧的时间戳 FIFO 在无数据时也输出全 1，两侧用同一个「全 1 = 无效」约定，软件据此判断最稳妥。

---

### 4.4 WriteCtx_s：流/窗口上下文回写

#### 4.4.1 概念说明

`NextWin_s` 算完所有新值（新指针 `HndlPtr2`、新窗口号 `HndlWincur`、新窗口末 `HndlWinEnd`、末样地址 `HndlWinLast`、钳位后的 `HndlWinBytes`、时间戳 `HndlTs`、窗口完成标志 `HndlWinDone`）后，必须把它们**持久化**回上下文存储，否则下一次该流被服务时读到的还是旧值。

上下文存储分为两块（见 [u3-l4](u3-l4-context-memory-model.md)）：

- **流上下文（CtxStr）**：每个流一份，5 个字段——`SCFG`（含 RINGBUF/OVERWRITE/WINCNT/WINCUR）、`BUFSTART`、`WINSIZE`、`PTR`、`WINEND`。通过 `Sel` 选择读哪 64 位（`Sel=0` 读 SCFG+BUFSTART、`Sel=1` 读 WINSIZE+PTR、`Sel=2` 读 WINEND，见 [u2-l1](u2-l1-common-package.md)）。
- **窗口上下文（CtxWin）**：每个「流×窗口」一份，2 组——`WINCNT+WINLAST`（`Sel=0`）、`时间戳`（`Sel=1`）。

`WriteCtx_s` 用 3 拍（`HndlCtxCnt = 0,1,2`）把变化写回，每拍同时写流上下文与窗口上下文（双口 RAM 允许同时写）。

#### 4.4.2 核心流程

```
cnt=0:
  流:  Sel=ScfgBufstart, WenLo=1   -> 写 SCFG (RINGBUF/OVERWRITE/WINCNT/WINCUR 原样回写，WINCUR 已更新)
  窗:  Sel=WincntWinlast, WenLo=1, WenHi=1
       WdatLo = (HndlWinBytes>>log2Bytes) 末31位, bit31 = EndByTrig   // 字节->采样数 + 触发标志
       WdatHi = HndlWinLast                                            // 末样地址
cnt=1:
  流:  Sel=WinsizePtr, WenHi=1      -> 写 PTR = HndlPtr2
  窗:  if HndlWinDone=1: Sel=WinTs, WenLo=1, WenHi=1
                     WdatLo = Ts[31:0], WdatHi = Ts[63:32]            // 仅完成窗口才写时间戳
cnt=2:
  流:  Sel=Winend, WenLo=1          -> 写 WINEND = HndlWinEnd
  窗:  (无操作)
```

注意三个细节：

1. **字节转采样数**：`HndlWinBytes` 是字节数，但窗口上下文 `WINCNT` 字段存的是**采样数**。回写时右移 `log2(StreamWidth/8)` 位（8 位流移 0、16 位移 1、32 位移 2、64 位移 3），把字节换算回采样。读出时（`ReadCtxWin_s`）则左移同样的位数换回字节——两端互逆。
2. **触发标志存进 WINCNT 的最高位**：`WdatLo(31) := EndByTrig`。软件读 WINCNT 时，低 31 位是采样数、最高位是「本窗口是否因触发结束」。这正是驱动判断「这是个触发帧窗口」的依据。
3. **时间戳只在窗口完成时写**：`HndlWinDone=1` 才写 `WinTs`。未完成的窗口（比如环形缓冲中途的覆盖写）不更新时间戳。

#### 4.4.3 源码精读

[hdl/psi_ms_daq_daq_sm.vhd:514-558](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L514-L558) —— `WriteCtx_s` 完整体：3 拍把流/窗口上下文写回。

**cnt=0（524–538 行）**：

[hdl/psi_ms_daq_daq_sm.vhd:524-538](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L524-L538) —— 第 0 拍：回写流上下文 SCFG（含更新后的 WINCUR）与窗口上下文 WINCNT（采样数+触发标志）/ WINLAST（末样地址）。

关键行：

```vhdl
-- 窗口上下文：字节 -> 采样数，bit31 = 触发标志
v.CtxWin_Cmd.WdatLo := shift_right(r.HndlWinBytes(31 downto 0), Log2StrBytes_c(r.HndlStream));
v.CtxWin_Cmd.WdatLo(31) := r.EndByTrig;
v.CtxWin_Cmd.WdatHi := r.HndlWinLast;
```

- `shift_right(HndlWinBytes(31 downto 0), Log2StrBytes_c(...))`：先砍掉 33 位里的保护位（取 `31 downto 0`），再右移换算成采样数。`Log2StrBytes_c` 在文件头部 117–125 行按每流宽度预算（`log2(StreamWidth/8)`）。
- `WdatLo(31) := EndByTrig`：把 `CheckResp_s` 锁存的触发标志塞进最高位。
- `WdatHi := HndlWinLast`：末样地址写进窗口上下文的高 32 位。

> 旁注：SCFG 里 RINGBUF/OVERWRITE/WINCNT 这几个字段其实是「读出来原样写回去」（528–530 行），因为它们在响应路径里不变化；只有 WINCUR 可能因为窗口切换而更新（531 行）。之所以整字回写，是因为上下文 RAM 是按字写的，不能只改一位。

**cnt=1（539–551 行）**：

[hdl/psi_ms_daq_daq_sm.vhd:539-551](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L539-L551) —— 第 1 拍：回写流上下文 PTR（= `HndlPtr2`）；若窗口完成则回写窗口上下文时间戳。

```vhdl
v.CtxStr_Cmd.WdatHi := r.HndlPtr2;          -- PTR = 最终写指针
...
if r.HndlWinDone = '1' then
  v.CtxWin_Cmd.Sel    := CtxWin_Sel_WinTs_c;
  v.CtxWin_Cmd.WdatLo := r.HndlTs(31 downto 0);
  v.CtxWin_Cmd.WdatHi := r.HndlTs(63 downto 32);
end if;
```

- 流上下文 `Sel=WinsizePtr` 时低 32 位是 WINSIZE、高 32 位是 PTR，这里只写 `WenHi`（PTR），WINSIZE 不变。
- 时间戳只在 `HndlWinDone=1` 时写——这与 4.3 里「时间戳只在触发/完成时才有意义」一致。未完成窗口（`HndlWinDone=0`）的 `Sel`/`WenLo`/`WenHi` 保持默认 0，不触发写。

**cnt=2（552–556 行）**：

[hdl/psi_ms_daq_daq_sm.vhd:552-556](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L552-L556) —— 第 2 拍：回写流上下文 WINEND（= 更新后的 `HndlWinEnd`），然后回 `Idle_s`。

```vhdl
v.CtxStr_Cmd.Sel    := CtxStr_Sel_Winend_c;
v.CtxStr_Cmd.WenLo  := '1';
v.CtxStr_Cmd.WdatLo := r.HndlWinEnd;
```

写完后 `HndlCtxCnt=2` 触发状态返回 `Idle_s`（516–517 行），一次完整的响应处理结束。

> 写顺序小议：3 拍里流上下文依次写 SCFG→PTR→WINEND（`Sel=0,1,2`），窗口上下文在第 0 拍写 WINCNT/WINLAST、第 1 拍（若完成）写时间戳。这个顺序与 `ReadCtxStr_s`/`ReadCtxWin_s` 的读顺序对应，保证同一 `Sel` 编码下读写一致。

#### 4.4.4 代码实践

**实践目标**：跟踪一次「触发结束、窗口完成」的响应，把 `WriteCtx_s` 三拍里实际写入上下文存储的字段全部列出来，建立「寄存器 → 上下文字段」的完整映射。

**操作步骤**：

1. 承接 4.2.4 情况 A（线性缓冲，写满窗口 0 切到窗口 1），并设本次 `Dma_Resp.Trigger = '1'`（同时写满 + 触发，`HndlWinDone=1`）、`StreamWidth_g(stream)=16` 位（每样本 2 字节，`Log2StrBytes=1`）、`HndlWinBytes` 钳位后 = `0x1000`、`HndlWinLast = 0x1000 - 2 = 0x0FFE`、`HndlTs = 0x0000_0000_DEAD_BEEF`（假设有效）。
2. 逐拍列出流上下文与窗口上下文各写了哪个字段、值是多少。

**预期结果**（窗口上下文针对窗口 0，因为 `HndlLastWinNr=0`、`HndlWincur` 在 cnt=0 写回时还是切换前的值 0——注意 `CtxWin_Cmd.Window` 在 `WriteCtx_s` 里未被显式重新赋值，沿用进入响应路径时读上下文设定的窗口）：

| 拍 | 流上下文（CtxStr）写入 | 窗口上下文（CtxWin）写入 |
| --- | --- | --- |
| cnt=0 | SCFG（WINCUR 更新为 1） | WINCNT = `0x1000>>1 = 0x0800`，bit31=1（触发）→ `0x8000_0800`；WINLAST = `0x0000_0FFE` |
| cnt=1 | PTR = `0x0000_1000`（窗口 1 起点） | 时间戳 Lo = `0xDEAD_BEEF`，Hi = `0x0000_0000`（因 `HndlWinDone=1`） |
| cnt=2 | WINEND = `0x0000_2000`（窗口 1 末） | （无操作） |

**需要观察的现象**：

- WINCNT 字段值 `0x8000_0800`：最高位 1 表示「因触发结束」，低 31 位 `0x0800 = 2048` 表示本窗口写了 2048 个 16 位样本（= 4096 字节，正好 4 KiB 窗口）。软件读这个字段就能同时知道「样本数」和「是否触发帧」。
- PTR 已指向窗口 1，下次该流被服务时从这里继续写。
- 时间戳只写进了窗口 0（完成的那个），窗口 1 的时间戳保持原值（未完成不写）。

（结果待本地验证：可在 `tb/psi_ms_daq_daq_sm/` 的 case 文件里构造触发场景，观察 `CtxWin_Cmd.WdatLo/WdatHi` 与 `CtxStr_Cmd.WdatHi/WdatLo` 的波形。）

#### 4.4.5 小练习与答案

**练习 1**：`HndlWinBytes` 是 33 位，但回写 WINCNT 时取的是 `HndlWinBytes(31 downto 0)` 再右移。那个保护位（第 32 位）为什么不写回？

**参考答案**：保护位只是为了在 `NextWin_s` 做 `HndlWinBytes > HndlWinSize` 比较时不溢出，它不承载有效数据。而且经过 495–497 行的钳位后，`HndlWinBytes` 已被限制到不超过 `winSize`（32 位以内），第 32 位此时是 0，写回它没有意义；WINCNT 字段本身也只有 31 位（bit31 留给触发标志），所以只取低 32 位再换算。

**练习 2**：为什么时间戳回写用 `CtxWin_Sel_WinTs_c`（`Sel=1`）单独一组，而不和 WINCNT/WINLAST（`Sel=0`）合在一起？

**参考答案**：一个「流×窗口」的窗口上下文要存 4 个 32 位字（WINCNT、WINLAST、时间戳低、时间戳高），一次 64 位读写只能传 2 个字。用 `Sel` 把它们分成两组（`Sel=0` 是 WINCNT+WINLAST，`Sel=1` 是时间戳高低），就能用同一块 RAM、同一套地址（`Stream & Window & Sel`）复用访问。时间戳只在窗口完成时才写，单独成组也方便「按需写」——未完成时干脆不访问这一组。

---

## 5. 综合实践

把本讲三个状态串起来，完成一次完整的「响应路径」跟踪。

**场景**：某流配置为环形缓冲（`HndlRingbuf='1'`）、2 个窗口（`HndlWincnt=1`）、`HndlBufstart=0x0000`、`HndlWinSize=0x0800`（2 KiB）。该流当前在窗口 0 写到 `HndlPtr0=0x0600`，此时输入逻辑送来一次触发，DMA 把剩余 512 字节（`Size=0x0200`）写入后以 `Trigger='1'` 结束。设 `StreamWidth_g=32`（4 字节/样本），时间戳有效 `Ts_Data=0x0001_0002_0003_0004`。

**任务**：

1. 在 `ProcResp0_s` 里算 `HndlPtr1`、`HndlWinBytes`（设此前为 `0x0600`）。
2. 在 `NextWin_s` 里判定：`HndlPtr1` 是否等于 `HndlWinEnd`（= `0x0800`）？触发为 1 时走哪个判定分支？算出 `HndlPtr2`、`HndlWincur`、`HndlWinEnd`、`HndlWinDone`、`HndlWinLast`、`HndlTs`。
3. 在 `WriteCtx_s` 里列出三拍写回的字段与值，特别注意 WINCNT 的采样数与触发标志位、以及时间戳是否被写入。

**参考解答要点**：

1. `HndlPtr1 = 0x0600 + 0x0200 = 0x0800`；`HndlWinBytes = 0x0600 + 0x0200 = 0x0800`（恰好等于 `winSize`，不触发钳位）。
2. `HndlPtr1 = 0x0800 = HndlWinEnd`。判定 1：因 `Trigger='1'` 成立 → `HndlWinDone=1`、`NewBuffer=1`；`HndlWincur(0) ≠ HndlWincnt(1)` → `HndlWincur:=1`、`HndlPtr2:=HndlWinEnd=0x0800`、`HndlWinEnd:=0x0800+0x0800=0x1000`。判定 2 不执行（触发时排除）。`HndlWinLast = 0x0800 - 4 = 0x07FC`。时间戳有效 → `HndlTs = 0x0001_0002_0003_0004`、`Ts_Rdy=1`。
3. WriteCtx 三拍：
   - cnt=0：流 SCFG（WINCUR=1）；窗口 0 的 WINCNT = `0x0800>>2 = 0x0200`（512 个 32 位样本），bit31=1 → `0x8000_0200`；WINLAST = `0x0000_07FC`。
   - cnt=1：流 PTR = `0x0000_0800`；因 `HndlWinDone=1`，写窗口 0 时间戳 Lo=`0x0003_0004`、Hi=`0x0001_0002`。
   - cnt=2：流 WINEND = `0x0000_1000`。

   结论：环形窗口因触发被「封口」（`HndlWinDone=1`），写指针推进到窗口 1 起点，窗口 0 的采样数、末样地址、时间戳全部齐备，软件收到中断后即可用 `GetDataUnwrapped` 从 `0x07FC` 出发解环形读取这 512 个样本。

（本综合实践为源码阅读 + 手算推导型，结果待本地仿真验证。）

## 6. 本讲小结

- `ProcResp0_s` 是响应路径的入口：用 `Dma_Resp.Size` 把写指针从 `HndlPtr0` 推进到 `HndlPtr1`，并把本窗口已写字节数 `HndlWinBytes` 累加（33 位带保护位），同时清除 `OpenCommand`/`FirstOngoing`。
- `NextWin_s` 的窗口完成判定是 `(写满窗口 且 线性) 或 触发`；成立时关闭当前窗口（`HndlWinDone=1`、`NewBuffer=1`）并切到下一个窗口（或在最后一个窗口时绕回窗口 0）。
- 线性缓冲与环形缓冲的根本差异在「写满窗口、非触发」时：线性模式换下一个窗口并封口，环形模式用独立的判定 2 把写指针绕回本窗口起点（`HndlPtr2 = HndlPtr1 - winSize`）、不封口、继续覆盖写。
- 末样地址 `HndlWinLast = HndlPtr1 - 样本字节` 是驱动解环形的锚点；时间戳 `HndlTs` 仅在触发且有效时锁存，否则写全 1 无效标记。
- `WriteCtx_s` 用 3 拍把更新写回：SCFG（含新 WINCUR）、PTR（=`HndlPtr2`）、WINEND（=新 `HndlWinEnd`）写回流上下文；WINCNT（字节右移换采样、bit31 存触发标志）、WINLAST、时间戳（仅完成窗口）写回窗口上下文。
- 非零传输才向 IRQ FIFO 入队一条记录（含流号、窗口号、`HndlWinDone`），零长度传输不产生中断事件。

## 7. 下一步学习建议

- 阅读 [u3-l4](u3-l4-context-memory-model.md) 了解 `WriteCtx_s` 写回的这些字段在 `reg_axi.vhd` 的双口 RAM 里是如何寻址与组织的，以及 AXI 侧（A 口）与状态机侧（B 口）如何共享同一块上下文存储。
- 阅读 [u3-l5](u3-l5-register-interface.md) 把寄存器接口与上下文存储的访问路径补全，理解软件经 AXI Slave 读写这些上下文字段的地址映射。
- 阅读 [u4-l1](u4-l1-irq-generation-fifo.md) 深入 IRQ FIFO：本讲里 `IrqFifoWrite` 入队的记录如何配合 `TfDoneCnt` 出队、并由 `IrqFifoGenIrq`（即 `HndlWinDone` 位）决定是否真正拉起 `StrIrq` 中断。
- 阅读 [u4-l3](u4-l3-driver-data-unwrap.md) 看驱动如何用本讲写回的 `HndlWinLast`（末样地址）与 WINCNT 在环形窗口里做两段拼接解包，把本讲的硬件行为与软件使用闭环。
- 想动手验证本讲的指针回绕，可打开 `tb/psi_ms_daq_daq_sm/psi_ms_daq_daq_sm_tb_case_multi_window.vhd`（及 `case_ringbuf` 一类用例，若存在），对照波形观察 `HndlPtr2`/`HndlWincur`/`HndlWinEnd` 在两种缓冲模式下的差异（仿真流程见 [u5-l1](u5-l1-tb-structure-psisim.md)）。
