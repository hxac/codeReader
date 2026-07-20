# DMA 引擎状态机：传输流程与字节对齐

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 DMA 引擎 `p_comb` 里六个状态（`Idle_s / RemRd1_s / RemRd2_s / Transfer_s / Done_s / Cmd_s`）各自干什么，并把一条命令从进入 CmdFifo 到响应返回 RspFifo 的完整生命周期画成一张状态流转图。
- 解释 `DataSft` 这个「双倍宽度移位寄存器」如何按字节把输入端送来的样本拼成内存字，以及为什么它的宽度是 \(2 \times \text{IntDataWidth\_g}\)。
- 区分 `RdBytes`、`WrBytes`、`RemWrBytes` 三个字节计数器的含义，说明一次「被 `MaxSize` 提前截断」的传输里，哪些字节真正写进了内存、哪些被存进了 Remaining Data RAM。
- 说清 `RspFifo_Data.Trigger` 到底在什么条件下才置 1，以及为什么有时触发会被「推迟到下一次传输」才上报。

本讲是 u2-l5 的直接续篇。u2-l5 把 DMA 当作「黑盒加四个缓存」看清了结构与接口（CmdFifo / RspFifo / DatFifo / Remaining Data RAM、`FirstDma`），却刻意把字节级的计数与移位留给了本讲。本讲就钻进 `p_comb` 状态机内部，把那条「命令进 → 数据进 → 内存出 → 响应出」的主线逐拍拆开。

## 2. 前置知识

进入状态机之前，先用通俗语言把本讲反复出现的几个概念讲清。

**两进程法（two-process method）**
本模块和输入逻辑（u2-l3）一样采用两进程法：一个纯组合进程 `p_comb` 算出下一拍的状态 `r_next`，一个时序进程 `p_seq` 在时钟上升沿把 `r_next` 打入寄存器 `r`。所有寄存器被打包在一个 record `two_process_r` 里。读状态机时，盯住 `p_comb` 里 `case r.State is` 的大框架即可。

**「字」与「字节」，以及为什么要拼字**
内存接口 `Mem_DatData` 一次只接受一个完整的 `IntDataWidth_g` 位字（默认 64 位 = 8 字节）。但输入端送来的样本可能「不足一整字」（例如一个触发帧只有 3 个字节），也可能跨在两个字中间。DMA 的核心职责之一，就是把零碎的字节流**按字节拼成整字**再发给内存——这就是「字节对齐」问题的来源。

**双倍宽度移位寄存器 `DataSft`**
为了在拼字时不丢字节，本模块用了一个宽度为 \(2 \times \text{IntDataWidth\_g}\) 的移位寄存器 `DataSft`。可以把它想象成上下两半：
- **低半字**（`IntDataWidth_g-1 downto 0`）：当前正要送进内存的那个字。
- **高半字**（`2*IntDataWidth_g-1 downto IntDataWidth_g`）：上一拍「溢出」、还没凑成完整字的若干字节，留着给下一拍补到低半字开头。

**`HndlSft`（handling shift）——一次传输里不变的「错位量」**
`HndlSft` 是当前正在拼的这个字里「已经预填了多少字节」。它来自上一次传输留下的残余（`RemWrBytes`），在 `RemRd2_s` 装载一次后，**整个 `Transfer_s` 期间保持不变**。这个「不变」是理解拼字逻辑的关键，本讲 4.3 会专门讲。

**`RdBytes` / `WrBytes` / `RemWrBytes` 三个计数器**
- `RdBytes`：本次传输**从输入端累计接收的有效字节数**（开头会把上一次的残余字节数预填进来）。它最终决定响应里上报的 `Size`。
- `WrBytes`：本次传输**已经凑成整字、送向内存的字节数**，每凑一个字 +8。它是判断「是否达到 `MaxSize`」的工作计数器。
- `RemWrBytes`：本次传输**多读出来、却没写进内存的字节数**（因为被 `MaxSize` 在字中间截断），会作为残余写回 Remaining Data RAM，留给下一次传输。

**fall-through（直通）输入接口**
输入端 `Inp_Data` 是「直通」式的：当 `Inp_Vld=1` 且本拍要接收时，数据**本拍就有效**，必须本拍处理掉，不能等到下一拍再读。这就是为什么 `Transfer_s` 里 `Inp_Rdy` 是组合信号、`RdBytes` 的累加也是组合发生的（源码注释称 "Combinatorial handling because of fall-through interface at input"）。

**承接 u2-l5 的结论**
u2-l5 已经讲清：命令经 CmdFifo 解耦、响应经 RspFifo 解耦、数据经 DatFifo（`alm_full` 反压）缓冲、残余经按流号寻址的 Remaining Data RAM 衔接跨传输的字边界；`FirstDma` 在复位后让每个流的首次传输跳过 RAM 未定义初值。本讲直接复用这些结论，只把镜头对准状态机本身。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [hdl/psi_ms_daq_daq_dma.vhd](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd) | 本讲唯一主角：DMA 引擎的实体与架构。状态机 `p_comb`（L137–L271）、时序 `p_seq`（L283–L298）、四个缓存的例化都在这里。 |
| [hdl/psi_ms_daq_pkg.vhd](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_pkg.vhd) | 公共类型包（u2-l1 已讲）。本讲引用命令记录 `DaqSm2DaqDma_Cmd_t`（Address/MaxSize/Stream）、响应记录 `DaqDma2DaqSm_Resp_t`（Size/Trigger/Stream）、输入数据记录 `Input2Daq_Data_t`（Last/Data/Bytes/IsTo/IsTrig）。 |
| [tb/psi_ms_daq_daq_dma/psi_ms_daq_daq_dma_tb_case_unaligned.vhd](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_daq_dma/psi_ms_daq_daq_dma_tb_case_unaligned.vhd) | 「非对齐」用例，覆盖残余衔接、触发在字内不同位置、`NextDone` 额外字等场景，是本讲代码实践的主要观察对象。 |
| [tb/psi_ms_daq_daq_dma/psi_ms_daq_daq_dma_tb_case_no_data_read.vhd](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_daq_dma/psi_ms_daq_daq_dma_tb_case_no_data_read.vhd) | 「无数据 / 残余字节」用例，验证 `MaxSize` 截断后残余字节的跨命令衔接，以及无数据时不上报内存命令。 |
| [tb/psi_ms_daq_daq_dma/psi_ms_daq_daq_dma_tb_pkg.vhd](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_daq_dma/psi_ms_daq_daq_dma_tb_pkg.vhd) | testbench 辅助过程包。`ApplyCmd`/`ApplyData`/`CheckResp`/`CheckMemData`/`CheckMemCmd` 的实现，是理解实践任务里「激励怎么给、期望怎么校验」的钥匙。 |

## 4. 核心概念与源码讲解

### 4.1 状态机全景：六个状态与一次传输的生命周期

#### 4.1.1 概念说明

DMA 引擎的核心是一个六状态有限状态机。一条命令的生命周期可以分成三个阶段：

1. **准备阶段**（`Idle_s → RemRd1_s → RemRd2_s`）：从 CmdFifo 取出命令，从 Remaining Data RAM 读回上一次留给本流的残余字节，把残余「预填」进移位寄存器 `DataSft`。
2. **传输阶段**（`Transfer_s`）：按节拍从输入端取数据、拼字、发往内存；同时统计字节数，判断是否因「达到 `MaxSize`」「遇到末帧/触发」或「输入暂时没数据」而结束。
3. **收尾阶段**（`Done_s → Cmd_s`）：把本次没拼满的残余字节写回 Remaining Data RAM，发出内存写命令，把响应（字节数、是否触发、流号）推进 RspFifo，然后回到 `Idle_s` 等下一条命令。

这六个状态在源码里就是一个枚举类型（[psi_ms_daq_daq_dma.vhd:98](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L98)）：

```vhdl
type State_t is (Idle_s, RemRd1_s, RemRd2_s, Transfer_s, Done_s, Cmd_s);
```

#### 4.1.2 核心流程

一次传输的状态流转（自上而下读，箭头表示下一拍要进入的状态）：

```
Idle_s ──(CmdFifo 有命令)──> RemRd1_s
RemRd1_s ──────────────────> RemRd2_s   (等 RAM 读延迟一拍)
RemRd2_s ──────────────────> Transfer_s (装载 HndlSft / 预填 DataSft)
Transfer_s ──(WrBytes>=MaxSize 或 末帧 或 输入空)──> Done_s
            └─(否则处理一拍数据，留在 Transfer_s)
Done_s ────────────────────> Cmd_s      (写残余、置 Mem_CmdVld)
Cmd_s ──(Mem_CmdRdy=1 或 无内存命令)────> Idle_s (推响应进 RspFifo)
```

> 注意：`Idle_s` 里 DMA 是「预读」CmdFifo 的队头命令（`CmdFifo_Cmd` 是 FIFO 输出端反序列化出来的记录，组合可见），等确认 `CmdFifo_Vld=1` 才置 `CmdFifo_Rdy=1` 弹出命令并进入 `RemRd1_s`。

`p_comb` 进程的敏感量里包含了所有它会用到的输入与内部寄存器（[L137](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L137)）。进程开头先做「保持 + 默认值」处理（[L142-L151](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L142-L151)）：

```vhdl
v := r;                 -- 默认保持所有寄存器稳定
v.CmdFifo_Rdy := '0';   -- 默认不弹命令
Inp_Rdy       <= (others => '0');  -- 默认不接收输入（直通信号，直接驱动端口）
v.Mem_DataVld := '0';   -- 默认无内存数据
v.RspFifo_Vld := '0';   -- 默认无响应
v.RemWen      := '0';   -- 默认不写残余 RAM
v.UpdateLast  := '0';
```

这种「先把所有输出置成空闲默认值，再在具体状态里按需拉高」的写法，是避免锁存、保证组合逻辑完整的标准手法。

#### 4.1.3 源码精读：寄存器组与三个入口状态

所有状态机寄存器都收在 record `two_process_r` 里（[L101-L130](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L101-L130)）。与本讲关系最密切的字段：

| 字段 | 含义 |
| --- | --- |
| `State` | 当前状态 |
| `HndlStream` / `StreamStdlv` | 当前命令的目标流号（后者是 stdlv，用作 RAM 地址） |
| `HndlAddress` / `HndlMaxSize` | 本次传输的写地址 / 最大字节数 |
| `HndlSft` | 拼字的字节偏移（整次传输不变） |
| `DataSft` | 双倍宽度移位寄存器（低半字=输出字，高半字=溢出） |
| `RdBytes` / `WrBytes` | 已接收 / 已凑整的字节计数 |
| `Trigger` / `Last` | 本次传输是否见到触发 / 末帧 |
| `NextDone` / `DataWritten` | 是否需要再凑一个字 / 本次是否真写出过数据 |
| `RemTrigger` / `RemLast` | 从 RAM 读回的、上次遗留的触发/末帧 |
| `RemWrBytes` / `RemData` / `RemWrTrigger` / `RemWrLast` | 要写回 RAM 的新残余 |
| `Mem_CmdVld` / `Mem_DataVld` | 内存命令 / 内存数据有效 |
| `RspFifo_Vld` / `RspFifo_Data` | 响应有效 / 响应内容 |
| `HasLast` | 每流一位，反馈给状态机「该流搬过一个带末帧的字」 |

**`Idle_s`（[L156-L166](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L156-L166)）**：把 CmdFifo 队头命令的字段锁存进 `HndlMaxSize`/`HndlStream`/`StreamStdlv`/`HndlAddress`，并把本地的 `Trigger`/`Last` 清零；看到 `CmdFifo_Vld=1` 就弹命令（`CmdFifo_Rdy=1`）并进入 `RemRd1_s`。

**`RemRd1_s`（[L168-L169](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L168-L169)）**：纯粹「等一拍」。原因是 Remaining Data RAM 的读出是**寄存输出**：`Idle_s` 把新流号写进 `r.StreamStdlv` 后，要经过「地址寄存器一拍 + RAM 输出寄存器一拍」数据才稳定到 `Rem_Data`/`Rem_RdBytes`/`Rem_Trigger`/`Rem_Last`。所以 `RemRd1_s` 等地址生效，`RemRd2_s` 才读到正确内容。这也是为什么需要**两个**等待状态而不是一个。

**`RemRd2_s`（[L171-L190](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L171-L190)）**：读到残余后做「装载」，并复位本次的工作计数 `WrBytes:=0`。这里正是 u2-l5 讲过的 `FirstDma` 分支：

```vhdl
v.WrBytes := (others => '0');
if r.FirstDma(r.HndlStream) = '1' then
  -- 首次传输：忽略 RAM 未定义初值，零偏移干净起步
  v.HndlSft := (others => '0');
  v.RdBytes := (others => '0');
  v.DataSft := (others => '0');
  v.RemTrigger := '0';  v.RemLast := '0';
else
  -- 正常传输：用真实残余预填
  v.HndlSft    := unsigned(Rem_RdBytes);                              -- 字节偏移
  v.DataSft(2*IntDataWidth_g-1 downto IntDataWidth_g) := Rem_Data;    -- 残余填进高半字
  v.RdBytes    := resize(unsigned(Rem_RdBytes), v.RdBytes'length);    -- 字节计数接着上次的算
  v.RemTrigger := Rem_Trigger;  v.RemLast := Rem_Last;
end if;
v.FirstDma(r.HndlStream) := '0';   -- 用过即清
v.State := Transfer_s;
v.NextDone := '0';  v.DataWritten := '0';
```

**关键细节**：残余 `Rem_Data` 被装进 `DataSft` 的**高半字**（而不是低半字）。这一点初看反直觉，但配合 4.3 的拼字逻辑就会明白——高半字扮演「上一拍溢出、本拍要补到输出字开头」的角色，而残余恰恰就是「上一次传输溢出、这一次要先补上」的字节。`RdBytes` 也接着上次的残余字节数继续累加，所以 `RdBytes` 自始至终代表「这条逻辑字节流里累计接收的位置」。

#### 4.1.4 代码实践：用 grep 确认「入口三状态只读不写数据」

1. **实践目标**：确认 `Idle_s/RemRd1_s/RemRd2_s` 三个状态既不向内存发数据（`Mem_DataVld` 保持默认 0），也不接收输入（`Inp_Rdy` 保持默认 0），它们只做「取命令 + 装载残余」。
2. **操作步骤**：在 [hdl/psi_ms_daq_daq_dma.vhd](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd) 中检索 `Mem_DataVld` 与 `Inp_Rdy`，你会发现 `v.Mem_DataVld := '1'` 只出现在 `Transfer_s`（[L225](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L225)），`Inp_Rdy(...) <= '1'` 也只出现在 `Transfer_s`（[L203](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L203)）。其余状态都靠进程开头的默认 0 维持空闲。
3. **需要观察的现象**：入口三状态的代码段里完全没有对 `Mem_DataVld`/`Inp_Rdy` 的赋值，印证它们是「纯准备」状态。
4. **预期结果**：你能说出「数据真正流动只发生在 `Transfer_s` 一个状态里，其它五个状态都是在做命令调度、残余搬运和响应收尾」。
5. 本实践为源码阅读型，无需运行仿真。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `RemRd1_s/RemRd2_s` 要占**两**个状态、而不是合并成一个？
**答案**：因为 Remaining Data RAM 的输出是寄存输出，有「地址寄存 + 输出寄存」两拍延迟。`Idle_s` 把新流号写进 `r.StreamStdlv`（占第一拍），RAM 在 `RemRd1_s` 期间输出仍是旧地址的内容，要到 `RemRd2_s` 才稳定给出当前流的残余（[L168-L190](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L168-L190)）。少一拍就会读到上一个流的残余。

**练习 2**：`Idle_s` 里既然预读了 CmdFifo 队头命令，为什么还要等到 `CmdFifo_Vld=1` 才弹出？
**答案**：预读只是把队头字段锁存到内部寄存器，便于组合使用；只有确认 FIFO 真的有有效命令（`CmdFifo_Vld=1`）时才能置 `CmdFifo_Rdy=1` 弹出，否则会「凭空消费」一个不存在的命令（[L163-L166](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L163-L166)）。

---

### 4.2 `Transfer_s`：双倍宽度移位拼字与字节计数

#### 4.2.1 概念说明

`Transfer_s` 是 DMA 真正「干活」的状态，它同时完成三件事：

1. **拼字**：用 `DataSft` 把零碎的输入字节拼成整字，每拍产出一个字送进 DatFifo。
2. **计数**：用 `RdBytes`/`WrBytes` 跟踪接收与凑整进度，用 `HndlMaxSize` 给本次传输封顶。
3. **判束**：判断何时结束——`MaxSize` 用满、遇到末帧（`Last`）、或输入暂时没数据。

拼字的核心难点是「字节错位」：本流上一次传输可能在某个字的中间被截断，留下 `HndlSft` 个字节没拼满。于是这一次每个 8 字节的输入 beat，都要先拿出前若干字节去**补齐**当前字，剩下的字节才能成为下一个字的开头。`DataSft` 用「低半字 = 输出字、高半字 = 溢出」的双倍宽度结构，把这件事在一拍内做完。

#### 4.2.2 核心流程

`Transfer_s` 每拍的判断顺序（[L192-L228](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L192-L228)）：

```
若 WrBytes >= HndlMaxSize          → 进 Done_s（MaxSize 用满）
否则若 DatFifo_AlmFull=0（有缓冲） → 处理一拍：
    [1] RdBytes += Inp_Data.Bytes   （仅当 NextDone=0、Inp_Vld=1、RemLast=0）
    [2] WrBytes += IntDataBytes_c   （每拍固定 +8）
    [3] 必要时拉高 Inp_Rdy          （直通接口，组合接收）
    [4] 末帧处理：若 Last=1 或 RemLast=1
           - 若 HndlSft+Bytes <= 8 或 RemLast=1 → 进 Done_s
           - 否则置 NextDone=1（再凑一个字）
           - 若 IsTrig=1 或 RemTrigger=1 → Trigger:=1
           - Last:=1
    [5] 若 NextDone=1 或 Inp_Vld=0  → 进 Done_s
    [6] 拼字：低半字←高半字；把输入 Data 写到字节偏移 HndlSft 处
    [7] 若 Inp_Vld=1 或 HndlSft≠0   → Mem_DataVld:=1、DataWritten:=1
否则（DatFifo 快满）                → 原地等待（反压）
```

**拼字逻辑（步骤 6）的两条赋值**是本状态最精妙之处，下面单独拆解。

#### 4.2.3 源码精读：拼字与计数

**MaxSize 封顶与反压**（[L194-L196](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L194-L196)）：先看 `WrBytes >= HndlMaxSize` 是否到顶，到顶就收尾；否则看 `DatFifo_AlmFull`——这正是 u2-l5 讲过的「用 alm_full 回头踩刹车」，避免在内存侧不 ready 时丢数据。

**字节计数**（[L197-L200](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L197-L200)）：

```vhdl
if r.NextDone = '0' and Inp_Vld(r.HndlStream) = '1' and r.RemLast = '0' then
  v.RdBytes := r.RdBytes + unsigned(Inp_Data(r.HndlStream).Bytes);
end if;
v.WrBytes := r.WrBytes + IntDataBytes_c;
```

`RdBytes` 只在「真正消费了一拍输入」时加上该拍的有效字节数 `Inp_Data.Bytes`；`WrBytes` 则每拍无条件 +8（每拍都凑一个字）。注意 `NextDone` 那一拍不消费新输入，所以 `RdBytes` 不增——那拍输出的字是用已经计过数的溢出字节凑出来的。

**末帧与触发捕获**（[L205-L217](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L205-L217)）：当输入声明 `Last=1`（来自 `IsTo` 或 `IsTrig`，见 u2-l4），或 RAM 读回的 `RemLast=1`，就进入末帧处理。其中关键判断是 `r.HndlSft + Bytes <= IntDataBytes_c`：

- **能放下**（`HndlSft + Bytes ≤ 8`）：末帧字节正好补进当前字，本拍结束进 `Done_s`。
- **放不下**（`HndlSft + Bytes > 8`）：末帧字节跨到了下一个字，必须再凑一个字，于是置 `NextDone=1`，下一拍把溢出字输出后再进 `Done_s`。这个「再凑一个字」就是 testbench 里所谓的 "rem-word"（残余字）。

只要本拍见到 `IsTrig` 或遗留的 `RemTrigger`，就把本地 `Trigger:=1`，并置 `Last:=1`。

**拼字的两条赋值**（[L221-L224](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L221-L224)）：

```vhdl
-- (a) 低半字 := 旧高半字（把上拍的溢出提升到输出位置）
v.DataSft(IntDataWidth_g - 1 downto 0) := r.DataSft(2*IntDataWidth_g - 1 downto IntDataWidth_g);
-- (b) 把本拍输入 Data 写到「字节偏移 HndlSft」处（可能横跨低/高半字）
v.DataSft(8*to_integer(r.HndlSft) + IntDataWidth_g - 1 downto 8*to_integer(r.HndlSft)) := Inp_Data(r.HndlStream).Data;
```

理解这两条，要记住 `HndlSft` 在整个传输里是**常量**（设为 \(s\)，范围 0..7）。赋值 (a) 先把高半字（上拍的溢出）搬到低半字；赋值 (b) 再把输入 8 字节写到从字节 \(s\) 开始的位置。因为写的是一个 8 字节宽的切片、起点在字节 \(s\)，它正好**填满低半字的第 \(s\ldots7\) 字节，并把多出来的 \(s\) 字节溢出到新高半字的第 \(0\ldots s\!-\!1\) 字节**。净效果（设输入是完整 8 字节）：

- **低半字（输出字）** = 旧高半字的前 \(s\) 字节（溢出）+ 输入的前 \(8\!-\!s\) 字节 = 一个完整的 8 字节字。
- **新高半字（新溢出）** = 输入的后 \(s\) 字节，留给下一拍。

举个具体例子：设 `IntDataWidth_g=64`（8 字节/字），上一次传输留下残余 3 字节（`HndlSft=3`，高半字 = 字节 `[B0,B1,B2,0,0,0,0,0]`）。本拍输入一个完整 8 字节 beat `[b0..b7]`：

| 字节位置 | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | （跨到高半字）8 | 9 | 10 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 来源 | B0 | B1 | B2 | b0 | b1 | b2 | b3 | b4 | b5 | b6 | b7 |

低半字（字节 0..7）= `[B0,B1,B2,b0,b1,b2,b3,b4]` → **送进内存的完整字**；高半字（字节 8..10）= `[b5,b6,b7]` → **成为下一拍的溢出**。下一拍又重复同样的「补 3 字节 + 溢出 3 字节」。这就是为什么 `HndlSft` 不变也能持续正确拼字。

当 `HndlSft=0`（对齐）时，赋值 (b) 直接覆盖整个低半字，溢出恒为 0——退化成「每拍输入即输出」的最简单情形。

**输出有效判定**（[L224-L227](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L224-L227)）：`if Inp_Vld=1 or HndlSft/=0 then Mem_DataVld:=1; DataWritten:=1`。即只要本拍有真输入，**或**存在待冲刷的残余（`HndlSft≠0`），就产出一个内存字。`DataWritten` 会在 `Done_s` 用来决定「要不要发内存命令」——如果整次传输一个字都没写出（纯无数据），就不发命令（见 4.4）。

#### 4.2.4 代码实践：手工推演一次「末帧放不下」的 `NextDone`

1. **实践目标**：用 `unaligned` 用例的 "without rem-word" 子用例，亲手验证 `HndlSft + Bytes > 8` 会触发 `NextDone`，从而多写一个内存字。
2. **操作步骤**：
   - 打开 [psi_ms_daq_daq_dma_tb_case_unaligned.vhd:164-174](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_daq_dma/psi_ms_daq_daq_dma_tb_case_unaligned.vhd#L164-L174)。该子用例三条命令的 `MaxSize` 是 30/64/30，输入是 `ApplyData(2, 30+25, Trigger_s, offset 0)` 再接 `ApplyData(2, 30, NoEnd, offset 55)`。
   - 结合 `ApplyData` 的实现（[tb_pkg.vhd:171-199](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_daq_dma/psi_ms_daq_daq_dma_tb_pkg.vhd#L171-L199)）可知：30 字节分成 8/8/8/6 四个 beat；55 字节分成 8×6+7 七个 beat，最后一个 7 字节 beat 带 `Last=1, IsTrig=1`。
   - 第一条命令 `MaxSize=30`、首次传输（`FirstDma=1`，`HndlSft=0`）会读 4 个完整 beat（32 字节），在 `Done_s` 把多读的 2 字节存为残余，所以第二条命令的 `HndlSft=2`。
   - 第二条命令 `MaxSize=64`、`HndlSft=2`，末帧是那个 7 字节的触发 beat：\(2+7=9>8\)，命中 `NextDone` 分支（[L208-L212](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L208-L212)），多写一个字。
3. **需要观察的现象**：`CheckResp(2, 25, Trigger_s)`（[L171](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_daq_dma/psi_ms_daq_daq_dma_tb_case_unaligned.vhd#L171)）期望第二条命令上报 25 字节且带触发；`CheckMemData(25, ...)`（[L372](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_daq_dma/psi_ms_daq_daq_dma_tb_case_unaligned.vhd#L372)）期望写出 \(\lceil 25/8\rceil=4\) 个内存字（最后一个字只有 1 个有效字节，即触发所在字节）。
4. **预期结果**：你能解释「`NextDone` 那一拍 `RdBytes` 不增（数据已计过），但 `WrBytes` 仍 +8、`Mem_DataVld=1`，于是多写了一个字；这个字承载的就是末帧溢出的那 1 个字节」。精确到每一拍的波形建议在 Modelsim 中跑 `sim/run.tcl`、在 `>> Unaligned end by trigger (without rem-word)` 处观察确认（**待本地验证**）。
5. 本实践为「读 testbench + 手工推演」型；若要跑仿真，按 u1-l2 的 PsiSim 流程即可。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `HndlSft` 在整个 `Transfer_s` 期间不需要更新？
**答案**：`HndlSft` 代表的是「这条逻辑字节流相对 8 字节字边界的固定错位量」，它由上一次传输的残余决定、在 `RemRd2_s` 装载一次。由于每一拍输入的 8 字节里恰好有 \(s\) 字节溢出、\(8\!-\!s\) 字节补齐，错位量逐拍自洽，不需要重新计算（[L221-L224](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L221-L224)）。

**练习 2**：`RdBytes` 和 `WrBytes` 在对齐传输（`HndlSft=0`、每拍满 8 字节）里数值上有什么关系？
**答案**：二者相等且同步增长——每拍都 +8。只有当出现残余偏移、末帧部分 beat、或 `NextDone` 时，两者才会拉开差距，差距正是「多读但未提交」的字节数。

**练习 3**：`Mem_DataVld` 的判定条件里为什么除了 `Inp_Vld=1` 还要「或 `HndlSft/=0`」？
**答案**：当本拍没有新输入（`Inp_Vld=0`）但仍挂着残余（`HndlSft≠0`）时，必须把残余冲刷成一个内存字输出，否则残余永远卡在移位寄存器里。这个分支正是「无新数据也要冲残余」的来源（[L224-L227](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L224-L227)），`no_data_read` 用例的第二条命令就靠它把 2 字节残余写出去。

---

### 4.3 `Done_s`：残余回写与末帧/触发捕获

#### 4.3.1 概念说明

`Transfer_s` 决定「何时结束」，`Done_s` 负责「结束后怎么收尾」。收尾要回答三个问题：

1. **多读的字节怎么办**：本次传输若被 `MaxSize` 在字中间截断，`RdBytes` 会超过 `MaxSize`，多读的字节既不能丢、也不能重复写进内存——必须作为残余存回 Remaining Data RAM，留给下一次传输。
2. **末字的数据怎么存**：截断位置在字内的第几个字节，决定了从 `DataSft` 的哪个位置取出残余数据。
3. **触发/末帧要不要立刻上报**：如果触发恰好落在「被截断、还没写完」的半个字里，本次不能上报，要把触发标志随残余一起留到下一次。

`Done_s` 还要把 `HasLast`（每流一位的「搬过末帧」反馈）刷新给状态机，并在确有数据写出时发出内存写命令（`Mem_CmdVld`）。

#### 4.3.2 核心流程

`Done_s` 一拍完成（[L230-L249](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L230-L249)）：

```
RemSft_v := HndlMaxSize mod 8                    （末字内截断的字节号）
RemWrTrigger/RemWrLast := 0                       （默认）
若 HndlMaxSize < RdBytes  （被 MaxSize 截断、有 overshoot）：
    RemWrBytes := RdBytes - HndlMaxSize           （多读的字节数 → 残余）
    RdBytes    := HndlMaxSize                     （上报 Size 截到 MaxSize）
    RemWrTrigger := Trigger ; RemWrLast := Last   （触发/末帧随残余留下次）
    HasLast(stream) := Last
否则 （没 overshoot，正常结束）：
    RemWrBytes := 0 ; HasLast(stream) := 0
RemData := DataSft[ 字节偏移 RemSft_v .. RemSft_v+7 ]   （从移位寄存器取出残余数据）
State := Cmd_s
若 DataWritten=1 → Mem_CmdVld := 1               （确有数据才发内存命令）
RemWen := 1                                       （写回残余 RAM）
```

残余字节数与上报字节数的关系可以用一个分段公式概括：

\[
\text{Size} = \min(\text{RdBytes},\ \text{HndlMaxSize}),\qquad
\text{RemWrBytes} = \max(0,\ \text{RdBytes}-\text{HndlMaxSize})
\]

#### 4.3.3 源码精读

**截断判定与残余计算**（[L234-L243](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L234-L243)）：

```vhdl
if r.HndlMaxSize < r.RdBytes then
  v.RemWrBytes   := std_logic_vector(resize(r.RdBytes - r.HndlMaxSize, v.RemWrBytes'length));
  v.RdBytes      := r.HndlMaxSize;          -- 上报 Size 钳到 MaxSize
  v.RemWrTrigger := r.Trigger;
  v.RemWrLast    := r.Last;
  v.HasLast(r.HndlStream) := r.Last;
else
  v.RemWrBytes   := (others => '0');
  v.HasLast(r.HndlStream) := '0';
end if;
```

注意 `RdBytes` 在这里被**原地钳到 `HndlMaxSize`**——这个钳制后的 `RdBytes` 就是 `Cmd_s` 里上报给状态机的 `Size`。所以「响应里的字节数永远不会超过命令的 `MaxSize`」，多出来的部分以残余形式留给下次。

**残余数据的取位**（[L231](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L231) 与 [L244](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L244)）：

```vhdl
RemSft_v := to_integer(resize(r.HndlMaxSize, 3));   -- = HndlMaxSize mod 8
...
v.RemData := v.DataSft(8*RemSft_v + IntDataWidth_g - 1 downto 8*RemSft_v);
```

`RemSft_v` 取 `HndlMaxSize` 的低 3 位，也就是「截断点在末字内的第几个字节」。`RemData` 从 `DataSft` 的字节 `RemSft_v` 开始取一个完整字宽。这与 4.2 的拼字结构是配套的：截断点之后的字节（即多读的字节）正好排在 `DataSft` 从 `RemSft_v` 开始的位置上，取出来就是下一次传输要预填的残余。在「对齐截断」（`HndlMaxSize` 是 8 的倍数，`RemSft_v=0`）的特例下，取的是低半字本身。

> 自检：当 `RemWrBytes≠0` 时，下一次传输的 `HndlSft` 会等于这次的 `RemWrBytes`；而 `RemData` 取位用的 `RemSft_v = MaxSize mod 8`。在「最后一拍是完整 8 字节」时 \( \text{RemWrBytes} = 8 - (\text{MaxSize}\bmod 8) \)，二者描述的是同一件事的两种视角——一个说「下次数着几个字节开始」，一个说「这次从第几个字节截断」。末拍不完整（带 `Last`）的情形略复杂，但 testbench 已覆盖。

**内存命令与残余写使能**（[L245-L249](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L245-L249)）：

```vhdl
v.State := Cmd_s;
if r.DataWritten = '1' then
  v.Mem_CmdVld := '1';        -- 确有数据写出，才发内存命令
end if;
v.RemWen := '1';               -- 无论是否写出数据，都把（可能为零的）残余写回 RAM
```

`DataWritten` 是「本次传输是否真产出过至少一个内存字」的标志。若整次传输一个字都没写（输入全程无数据、又无残余），`Mem_CmdVld` 不会被拉高——这就是 `no_data_read` 用例里 `StdlCompare(0, Mem_CmdVld, "Unexpected memory command")` 能通过的原因（[no_data_read L177](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_daq_dma/psi_ms_daq_daq_dma_tb_case_no_data_read.vhd#L177)）。注意 `RemWen` 恒为 1：即使残余字节数为 0，也要写一次（把 `RemWrBytes=0` 等写回），保证下一次读到的是干净的「无残余」状态，而不是 RAM 里的旧值。

#### 4.3.4 代码实践：验证「MaxSize 截断 → 残余跨命令衔接」

1. **实践目标**：用 `no_data_read` 用例的 "No Data with leftover bytes" 子用例，确认 `MaxSize` 截断产生的残余会被下一条命令正确接续。
2. **操作步骤**：
   - 打开 [psi_ms_daq_daq_dma_tb_case_no_data_read.vhd:116-128](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_daq_dma/psi_ms_daq_daq_dma_tb_case_no_data_read.vhd#L116-L128)。三条命令 `MaxSize=30/30/30`，输入 `ApplyData(2, 32, NoEnd)` 再 `ApplyData(2, 30, NoEnd, offset 32)`，共 62 字节。
   - 第一条命令 `MaxSize=30`、对齐起步（`HndlSft=0`），输入有 32 字节（4 个满 beat）。按 4.2 的计数：`WrBytes` 走到 32 时（≥30）进 `Done_s`，此时 `RdBytes=32 > MaxSize=30`，于是 `RemWrBytes=2`、`RdBytes` 钳到 30。
   - 期望校验：`CheckResp(2, 30, NoEnd)`（[L121](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_daq_dma/psi_ms_daq_daq_dma_tb_case_no_data_read.vhd#L121)）、`CheckMemCmd(addr, 30)`（[L195](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_daq_dma/psi_ms_daq_daq_dma_tb_case_no_data_read.vhd#L195)）、`CheckMemData(30, ...)`（[L229](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_daq_dma/psi_ms_daq_daq_dma_tb_case_no_data_read.vhd#L229)）。
   - 第二条命令 `MaxSize=30`、`HndlSft=2`（接 2 字节残余），且此刻无新输入（`SubCase` 控制下 `ApplyData(30,...)` 尚未到）。于是它把 2 字节残余冲成一个内存字就收尾：`CheckResp(2, 2, NoEnd)`（[L123](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_daq_dma/psi_ms_daq_daq_dma_tb_case_no_data_read.vhd#L123)）、`CheckMemCmd(addr, 2)`（[L196](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_daq_dma/psi_ms_daq_daq_dma_tb_case_no_data_read.vhd#L196)）。
3. **需要观察的现象**：30 + 2 + 30 = 62，恰好等于输入总字节数；三条命令的字节数全部命中校验，说明残余既没丢也没重复计。
4. **预期结果**：你能讲清「第一条命令读了 32 字节、上报 30、存 2 字节残余；第二条命令没有新数据，仅把这 2 字节残余冲成一个字上报；第三条命令接着读剩下的 30 字节」。**待本地验证**：可在 Modelsim 波形里观察第二条命令期间 `HndlSft=2`、`Inp_Vld=0`、仍输出一个 `Mem_DatData` 字。
5. 本实践为「读 testbench + 字节账对平」型。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Done_s` 里 `RdBytes` 被「原地改写」成 `HndlMaxSize` 后，`Cmd_s` 才能拿它当 `Size` 上报？
**答案**：`RdBytes` 原值可能超过 `MaxSize`（多读了），但响应给状态机的字节数不能超过命令允许的上限。所以先在 `Done_s` 钳到 `MaxSize`（[L236](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L236)），`Cmd_s` 直接读它作为 `Size`（[L256](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L256)），二者共用一个寄存器、省一份逻辑。

**练习 2**：`RemWen` 为什么在「残余为 0」时也要置 1？
**答案**：Remaining Data RAM 是按流号寻址、跨传输保留的。如果某次传输结束时残余为 0 却不写回，下一次该流读到的会是 RAM 里**上上次的旧残余**。所以必须每次都写（哪怕写 0），把「本次无残余」这个事实落盘（[L249](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L249)）。

**练习 3**：`HasLast(stream)` 在什么条件下置 1？它反馈给谁？
**答案**：仅当本次传输「被 `MaxSize` 截断且 `Last=1`」时置 1（[L239](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L239)）。它经端口 `DaqSm_HasLast` 反馈给控制状态机，用于窗口判断——状态机据此知道某流的末帧已经搬过一个字（即便触发还没上报）。

---

### 4.4 `Cmd_s`：响应组装与 Trigger 判定

#### 4.4.1 概念说明

`Cmd_s` 是状态机的「出口柜台」。它要做两件事：等内存命令被下游（AXI 主接口）握手接收（`Mem_CmdRdy=1`），然后把本次传输的结果打包成响应 `DaqDma2DaqSm_Resp_t` 推进 RspFifo。响应只有三个字段：

- `Size`：本次传输最终上报的字节数（就是钳到 `MaxSize` 后的 `RdBytes`）。
- `Trigger`：本次传输是否「完整地把一个触发写进了内存」。
- `Stream`：本次传输对应的流号。

其中 `Trigger` 的判定最容易出错，也是本模块的重点：**不是见到触发就上报**。

#### 4.4.2 核心流程

`Cmd_s` 的逻辑（[L251-L264](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L251-L264)）：

```
若 Mem_CmdRdy=1 或 Mem_CmdVld=0 （内存命令已接受，或本次根本没发命令）：
    State := Idle_s
    Mem_CmdVld := 0
    RspFifo_Vld := 1
    RspFifo_Data.Size   := RdBytes            （已钳到 MaxSize）
    RspFifo_Data.Trigger := 1  仅当 RemWrBytes=0 且 Trigger=1
    RspFifo_Data.Stream := HndlStream
```

触发的上报规则用公式写出来就是：

\[
\text{RspFifo\_Data.Trigger} = 1 \iff (\text{RemWrBytes}=0)\ \land\ (\text{Trigger}=1)
\]

#### 4.4.3 源码精读

**握手与状态返回**（[L252-L255](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L252-L255)）：

```vhdl
if Mem_CmdRdy = '1' or r.Mem_CmdVld = '0' then
  v.State      := Idle_s;
  v.Mem_CmdVld := '0';
  v.RspFifo_Vld := '1';
```

注意条件是「`Mem_CmdRdy=1` **或** `Mem_CmdVld=0`」。后者处理的是 `Done_s` 没有置 `Mem_CmdVld`（本次无数据写出）的情况——这时根本没有内存命令要发，不必等 `Mem_CmdRdy`，直接收尾。这样无论有没有数据，`Cmd_s` 都能在一拍内（或等到命令握手后）回到 `Idle_s`。

**触发判定**（[L256-L262](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L256-L262)）：

```vhdl
v.RspFifo_Data.Size := std_logic_vector(r.RdBytes);
-- Only mark as trigger if all samples are completely written to memory (no remaining samples in REM RAM)
if (unsigned(r.RemWrBytes) = 0) and (r.Trigger = '1') then
  v.RspFifo_Data.Trigger := '1';
else
  v.RspFifo_Data.Trigger := '0';
end if;
v.RspFifo_Data.Stream := r.HndlStream;
```

源码注释一句话点破了设计意图：**只有当所有样本都已完整写进内存（REM RAM 里没有残余样本）时，才标记触发**。原因是：触发所在的那个字必须**整字落地**才算真正写进内存。如果触发恰好落在一个被 `MaxSize` 截断的半字里（`RemWrBytes≠0`），这个字还没写完，触发就被「扣留」——`Done_s` 已把它随残余存进 RAM（`RemWrTrigger`），下一次传输把它拼完整后才在这里上报。

`Stream` 直接取 `HndlStream`，让状态机知道这条响应属于哪个流。

**响应的出口**：`v.RspFifo_Vld:=1` 配合 `v.RspFifo_Data` 把响应推进 RspFifo（RspFifo 的 `vld_i` 接 `r.RspFifo_Vld`、`dat_i` 接 `DaqDma2DaqSm_Resp_ToStdlv(r.RspFifo_Data)`，见 [L326](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L326)）。u2-l5 已说明 RspFifo 不需要 ready 握手——因为「在途命令数从不超过流数」，RspFifo 深度 `2**StreamBits_c` 足够，绝不会溢出。

#### 4.4.4 代码实践：对照 testbench 理解「触发何时上报、何时推迟」

1. **实践目标**：用 `unaligned` 用例的两个触发子用例，分别确认「触发立即上报」与「触发随字节数据完整落地后上报」两种情形都满足 \( \text{RemWrBytes}=0 \) 的条件。
2. **操作步骤**：
   - 子用例 "with rem-word"（[L152-L162](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_daq_dma/psi_ms_daq_daq_dma_tb_case_unaligned.vhd#L152-L162)）：第二条命令 `MaxSize=64`，末帧是 3 字节触发 beat，`HndlSft=2`，\(2+3=5\le8\) 不触发 `NextDone`，末字共 5 个有效字节；因 `MaxSize(64) ≥ RdBytes`，`RemWrBytes=0`，于是 `Trigger` 立即上报 → `CheckResp(2, 29, Trigger_s)`。
   - 子用例 "without rem-word"（[L164-L174](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_daq_dma/psi_ms_daq_daq_dma_tb_case_unaligned.vhd#L164-L174)）：末帧是 7 字节触发 beat，\(2+7=9>8\) 触发 `NextDone`，多写一个字承载溢出；同样 `MaxSize(64) ≥ RdBytes`，`RemWrBytes=0`，`Trigger` 仍上报 → `CheckResp(2, 25, Trigger_s)`。
   - 再对照 `CheckResp` 的实现（[tb_pkg.vhd:133-137](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_daq_dma/psi_ms_daq_daq_dma_tb_pkg.vhd#L133-L137)）：当 `EndType=Trigger_s` 时它断言 `DaqSm_Resp.Trigger=1`，否则断言 `=0`。
3. **需要观察的现象**：两个子用例都上报了触发，但写出的字节数（29 与 25）都不是 8 的倍数，且都不相等——说明「触发上报」与「字节数是否整字对齐」无关，只与 `RemWrBytes`（是否有超出 `MaxSize` 的未提交字节）有关。
4. **预期结果**：你能总结出规则——「只要本次传输没有把任何字节扣留在 REM RAM（`RemWrBytes=0`），且见过触发（`Trigger=1`），就在响应里上报触发；否则触发随残余推迟」。如果想观察「触发推迟到下一次传输」的反向情形，可构造一个 `MaxSize` 把触发字截成两半的命令并跑仿真验证（**待本地验证**）。
5. 本实践为「读 testbench + 规则归纳」型。

#### 4.4.5 小练习与答案

**练习 1**：`Cmd_s` 的握手条件为什么是 `Mem_CmdRdy=1 or Mem_CmdVld=0`，而不是单纯的 `Mem_CmdRdy=1`？
**答案**：本次传输若一个字都没写出（`DataWritten=0`），`Done_s` 不会置 `Mem_CmdVld`（[L246-L248](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L246-L248)）。这种「无数据」情况下没有命令要握手，若死等 `Mem_CmdRdy` 会永远卡在 `Cmd_s`。`or Mem_CmdVld=0` 让无命令的情形直接放行回 `Idle_s`（[L252](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L252)）。

**练习 2**：假设一次传输见到了触发（`Trigger=1`），但 `MaxSize` 把触发所在字截成了两半（`RemWrBytes≠0`）。本次响应的 `Trigger` 是什么？触发最终在哪里被上报？
**答案**：本次响应 `Trigger=0`（[L258-L262](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L258-L262)）。触发标志在 `Done_s` 随残余写进 RAM（`RemWrTrigger`，[L237](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L237)），下一次传输在 `RemRd2_s` 读回为 `RemTrigger`（[L184](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L184)），等那个字拼完整、`RemWrBytes=0` 时才在响应里上报。

**练习 3**：为什么 RspFifo 不需要 ready 握手，而 CmdFifo 需要？
**答案**：因为「每流最多一条在途命令」——状态机对同一个流不会在前一条命令的响应还没回来时再发第二条（u2-l5 已论证）。所以响应数量被流数封顶，RspFifo 深度 `2**StreamBits_c` 足够吸收，永远不会溢出，无需反压（[L325-L326](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L325-L326)）。CmdFifo 则要应对状态机连续下发命令，需要握手。

---

## 5. 综合实践：追踪一次「被 `MaxSize` 提前截断」的传输

**任务**：给定一条命令、一组输入，逐拍追踪 `RdBytes`、`WrBytes`、`RemWrBytes`、`DataSft` 的变化，说清「哪些字节写进了内存、哪些进了 Remaining Data RAM、`RspFifo_Data.Trigger` 何时才会置 1」。这是本讲的总练习，把 4.1–4.4 串起来。

**场景**：`IntDataWidth_g=64`（8 字节/字），某流首次传输（`FirstDma=1`，所以 `HndlSft=0`），命令 `MaxSize=30`，输入端有足够数据，每拍送来完整 8 字节 beat（无 `Last`、无触发）。这等价于 `no_data_read` 用例 "No Data with leftover bytes" 的第一条命令（`ApplyCmd(2, addr, 30)` + `ApplyData(2, 32, NoEnd)`）。

**建议步骤**：

1. **装载（RemRd2_s）**：`HndlSft=0`、`RdBytes=0`、`WrBytes=0`、`DataSft=0`。
2. **逐拍传输（Transfer_s）**：

   | 拍 | 输入 beat | RdBytes | WrBytes | DataSft 低半字（输出字） | 判束 |
   | --- | --- | --- | --- | --- | --- |
   | 1 | 字节 0..7 | 8 | 8 | `[0..7]` | WrBytes=8 < 30，继续 |
   | 2 | 字节 8..15 | 16 | 16 | `[8..15]` | 继续 |
   | 3 | 字节 16..23 | 24 | 24 | `[16..23]` | 继续 |
   | 4 | 字节 24..31 | 32 | 32 | `[24..31]` | WrBytes=32，本拍先处理完才判束 |
   | 5 | — | 32 | 32 | — | 入口判 `WrBytes(32)≥MaxSize(30)` → `Done_s` |

3. **收尾（Done_s）**：
   - `RemSft_v = 30 mod 8 = 6`。
   - `HndlMaxSize(30) < RdBytes(32)` 成立 → `RemWrBytes = 32-30 = 2`；`RdBytes` 钳到 30；`RemData = DataSft` 从字节 6 起取一个字 = `[字节30, 字节31, 0,0,0,0,0,0]`（字节 30、31 是多读的，其余位为 0）。
   - `DataWritten=1` → `Mem_CmdVld=1`；`RemWen=1`。
4. **出口（Cmd_s）**：`RspFifo_Data.Size = RdBytes = 30`；因 `RemWrBytes=2≠0`，即便本地 `Trigger=0`，`RspFifo_Data.Trigger` 也为 0；`Stream` = 该流。

**需要回答的三个问题**：

- **哪些字节写进了内存？** 字节 0..29 共 30 字节。DMA 向 DatFifo 推了 4 个字（`WrBytes/8 = 4`），但内存命令 `Mem_CmdSize=30` 告诉 AXI 主接口「只写 30 字节」——前 3 个字完整写入（24 字节），第 4 个字只写前 6 个有效字节（字节 24..29），后 2 个字节（字节 30、31）虽然出现在 `Mem_DatData` 上但不会被写进内存。
- **哪些字节进了 Remaining Data RAM？** 字节 30、31（`RemWrBytes=2`），存在 `RemData` 的前 2 个字节位置，地址为该流号。下一次该流的命令在 `RemRd2_s` 会以 `HndlSft=2` 把它们预填进 `DataSft` 高半字，从而正确接续。
- **`RspFifo_Data.Trigger` 何时置 1？** 本次传输 `Trigger=0`（根本没见到触发），所以为 0。即便假设本拍见到了触发，只要 `RemWrBytes≠0`（触发字被截断），`Trigger` 也会被扣留，等下一次传输把字拼完整（`RemWrBytes=0`）后才上报。

**自检**：把上面表格里的 30 换成 `MaxSize`、把 32 换成「读到的实际字节数」，你能复现任意 `MaxSize` 截断场景。若想验证，在 Modelsim 中跑 `sim/run.tcl`，在 `>> No Data with leftover bytes` 处观察 `r.RdBytes`、`r.WrBytes`、`r.RemWrBytes` 与 `Mem_CmdSize` 的波形（**待本地验证**）。

## 6. 本讲小结

- DMA 引擎是一个六状态机：`Idle_s`（取命令）→ `RemRd1_s/RemRd2_s`（读残余、装载 `DataSft`，两拍等 RAM 读延迟）→ `Transfer_s`（拼字、计数、判束）→ `Done_s`（残余回写、置内存命令）→ `Cmd_s`（发响应）→ 回 `Idle_s`。数据真正流动只发生在 `Transfer_s`。
- 拼字靠双倍宽度移位寄存器 `DataSft`：低半字是送内存的字，高半字是溢出；每拍「低←高」再把输入写到字节偏移 `HndlSft` 处。`HndlSft` 来自残余、整次传输不变，是字节对齐的关键。
- `RdBytes` 是累计接收字节（最终决定上报 `Size`），`WrBytes` 是凑整进度（每拍 +8，用于判 `MaxSize`），二者在末字不对齐时拉开差距；差距就是 `Done_s` 里算出的 `RemWrBytes`。
- `Done_s` 把「多读的字节」（`RdBytes - MaxSize`）作为残余写回 Remaining Data RAM，把 `RdBytes` 钳到 `MaxSize` 作为上报 `Size`；只有 `DataWritten=1` 才发内存命令，无数据时连命令都不发。
- `RspFifo_Data.Trigger` 仅当 `RemWrBytes=0 且 Trigger=1` 时置 1；触发若落在被截断的半字里，会随残余推迟到下一次传输上报。
- 末帧若 `HndlSft + Bytes > 8`，会置 `NextDone` 多凑一个「残余字」再结束；这是保证末帧字节完整落地的机制。

## 7. 下一步学习建议

- **下一讲 u2-l7《AXI 主接口 psi_ms_daq_axi_if》**：本讲产出的 `Mem_Cmd*`（地址/大小/有效）与 `Mem_DatData/Vld` 是怎么被 `psi_common_axi_master_full` 包装成 AXI 写事务、按 `Mem_CmdSize` 处理末 beat 部分字节，并最终写进 DDR 的——这是 DMA 之后的最后一段数据通路。
- **回顾 u2-l3/u2-l4**：本讲的 `Last`/`IsTrig`/`IsTo` 都来自输入逻辑；如果想搞清「触发字节为什么恰好落在那个位置」，建议结合 u2-l3（后触发计数）与 u2-l4（超时帧）一起看。
- **进阶 u4 单元**：本讲的响应 `Trigger`、`HasLast`、`Size` 是控制状态机做「窗口切换、环形缓冲回绕、中断生成」的依据；u4-l1（中断生成）与 u4-l5（窗口保护）会把这条响应链路接到底。
- **动手验证**：在 `tb/psi_ms_daq_daq_dma/` 下还有 `aligned`、`cmd_full`、`data_full`、`errors`、`empty_timeout` 等用例，分别从「对齐」「命令 FIFO 满」「数据 FIFO 反压」「错误」「空超时」角度 exercise 状态机，是检验你是否真的理解本讲的最佳素材。
