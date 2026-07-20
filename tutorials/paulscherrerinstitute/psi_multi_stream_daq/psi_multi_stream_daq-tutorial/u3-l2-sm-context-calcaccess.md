# 控制状态机：上下文读取与 DMA 命令计算

## 1. 本讲目标

本讲接着 [u3-l1](u3-l1-sm-overview-arbitration.md) 的「优先级仲裁」继续往下走。在上一讲里，状态机已经通过三个仲裁器选出了「现在该服务哪一路流」，并把流号存进 `HndlStream`。从这一拍开始，状态机要回答两个具体问题：

1. **这一路流当前应该往内存的哪个地址写？** —— 这需要读回该流在上下文存储里保存的写指针 `ptr`、窗口末尾 `winEnd`、缓冲区起点 `bufstart` 等字段。
2. **这次 DMA 最多能写多少字节？** —— 这需要同时满足三条约束：不超过软件设定的最大突发长度 `MaxBurstSize_g`、不跨越 AXI 4KB 边界、不超过当前窗口的末尾。

学完本讲，读者应该能够：

- 看懂 `ReadCtxStr_s` / `ReadCtxWin_s` 如何用 `HndlCtxCnt` 计数器分多拍、带 2 拍 RAM 读延迟地把流上下文与窗口上下文读出来并装配进 `Hndl*` 一组寄存器；
- 理解 `First_s` 状态如何用 `FirstAfterEna` / `FirstOngoing` 两个标志，在流（重新）使能后的首次访问中把写指针重置回 `bufstart`，并解释为什么要用「两个标志 + 命令/响应路径区分」这种略显绕弯的写法；
- 手工计算 `Hndl4kMax`（4KB 边界剩余）与 `HndlWinMax`（窗口剩余）两个上限，并说明它们在 `CalcAccess0_s` / `CalcAccess1_s` 中如何把 `Dma_Cmd.MaxSize` 裁剪到合法值；
- 给定 `bufstart`、`winSize`、当前 `ptr`、`MaxBurstSize_g`，推出下一次 DMA 的 `Dma_Cmd.Address` 与 `Dma_Cmd.MaxSize`。

本讲**只覆盖命令路径**（读上下文 → 算出 DMA 命令）。响应路径里的「窗口切换、环形缓冲回绕、上下文回写」是 [u3-l3](u3-l3-sm-window-switch-ringbuf.md) 的主题；窗口保护（`WinProtected`/`NewBuffer`）的完整协议是 [u4-l5](u4-l5-window-protection-overwrite.md) 的主题，本讲只在 `CalcAccess0_s` 处点到为止。

## 2. 前置知识

在进入源码前，先用三段话把「为什么状态机要读上下文」讲清楚。

### 2.1 什么是「上下文（Context）」

`psi_multi_stream_daq` 是一个多流、多窗口的采集核。每一路流（stream）都有自己的配置和运行状态，每一路流的每一个窗口（window）也有自己的运行状态。这些状态不能只放在状态机内部的寄存器里——因为**软件（CPU 通过 AXI Slave）也要读写它们**（例如配置缓冲区地址、读回已写入的字节数）。所以这些状态被放在一块**双口 RAM** 里，AXI 侧用 A 口访问、状态机用 B 口访问。这块 RAM 就是「上下文存储」，它分成两块：

- **流上下文（Stream Context）**：每路流 5 个 32 位字段，记录该流的全局信息。用 `CtxStr_Sel_*`（2 位）选择读哪一组：
  - `CtxStr_Sel_ScfgBufstart_c = "00"`：低 32 位是 SCFG（流配置，内含 ringbuf/overwrite/winCnt/winCur 标志），高 32 位是 `bufstart`（缓冲区起始地址）。
  - `CtxStr_Sel_WinsizePtr_c = "01"`：低 32 位是 `winSize`（单个窗口的字节大小），高 32 位是 `ptr`（当前写指针）。
  - `CtxStr_Sel_Winend_c = "10"`：低 32 位是 `winEnd`（当前窗口的结束地址）。
- **窗口上下文（Window Context）**：每「流 × 窗口」一组，记录该窗口的运行状态。用 `CtxWin_Sel_*`（1 位）选择：
  - `CtxWin_Sel_WincntWinlast_c = "0"`：低 31 位是 WINCNT（该窗口已写入的**采样数**），bit 31 是 WINLAST 标志，高 32 位是末样本地址。
  - `CtxWin_Sel_WinTs_c = "1"`：该窗口完成时锁存的时间戳（64 位）。

这些选择常量、记录类型都定义在公共包里（详见 [u2-l1](u2-l1-common-package.md)），上下文 RAM 的地址译码与物理布局详见 [u3-l4](u3-l4-context-memory-model.md)。本讲只需记住：**状态机要发出 `CtxStr_Cmd` / `CtxWin_Cmd`（带 `Stream`、`Sel`、`Rd` 字段）去读，两拍后从 `CtxStr_Resp` / `CtxWin_Resp` 拿到 `RdatLo` / `RdatHi`。**

### 2.2 为什么读一次上下文要分多拍

上下文 RAM 是同步 RAM：在时钟 N 发出读命令（`Rd='1'` + 地址），数据要到时钟 N+1（或 N+2，取决于 RAM 是否寄存输出）才能在 `RdatLo/RdatHi` 上稳定。本设计的 RAM 读延迟是 **2 拍**。因此状态机用一个计数器 `HndlCtxCnt` 来「发命令」和「收响应」错开两拍：

- 第 k 拍发第 i 个字段的读命令；
- 第 k+2 拍才能拿到第 i 个字段的响应。

这就是为什么 `ReadCtxStr_s` 要跑 5 拍（`HndlCtxCnt = 0,1,2,3,4`）、`ReadCtxWin_s` 要跑 3 拍（`0,1,2`）。

### 2.3 单位要分清（很容易踩坑）

本讲会反复出现两组单位，务必区分：

| 量 | 单位 | 出处 |
|---|---|---|
| `Inp_Level`、`MinBurstSize_g`、`MaxBurstSize_g` | 内部 64 位字（QWORD，8 字节） | 输入 FIFO 水位、仲裁门限 |
| `Dma_Cmd.MaxSize`、`Hndl4kMax`、`HndlWinMax`、`HndlWinBytes`、`ptr`、`winEnd`、`bufstart` | 字节（byte） | 内存地址与传输长度 |

`Dma_Cmd.MaxSize` 是**字节**，源码里把 `MaxBurstSize_g`（字）乘 8 得到：注释明写「8 bytes per 64-bit QWORD」。这是后面所有「裁剪」运算的关键。

> 备注：状态机这一层把内部字宽固定按 64 位/8 字节处理（`MaxBurstSize_g * 8` 是硬编码）。`IntDataWidth_g` 参数化是 [u4-l4](u4-l4-axi-cache-intdatawidth.md) 的内容，本讲不展开。

## 3. 本讲源码地图

本讲几乎只看一个文件：

| 文件 | 作用 |
|---|---|
| `hdl/psi_ms_daq_daq_sm.vhd` | 控制状态机。本讲关注其中的 `ReadCtxStr_s`、`First_s`、`ReadCtxWin_s`、`CalcAccess0_s`、`CalcAccess1_s` 五个状态，以及 `Hndl4kMax` / `HndlWinMax` 的计算和「禁用流」处理循环。 |

为解释上下文访问的 `Sel` 字段，会少量引用：

| 文件 | 作用 |
|---|---|
| `hdl/psi_ms_daq_pkg.vhd` | `CtxStr_Sel_*` / `CtxWin_Sel_*` / `CtxStr_Sft_*` 常量与 `ToCtxStr_t` / `ToCtxWin_t` / `FromCtx_t` 记录定义。 |

实践环节会用到测试平台，用来核对计算结果：

| 文件 | 作用 |
|---|---|
| `tb/psi_ms_daq_daq_sm/psi_ms_daq_daq_sm_tb_case_single_simple.vhd` | 用 `ExpectDmaCmd` 校验 4KB 边界裁剪、窗口大小裁剪等场景的 `Dma_Cmd`。 |

## 4. 核心概念与源码讲解

本讲按数据流动的自然顺序拆成 5 个最小模块：先读流上下文（4.1），再处理使能后首次访问（4.2），再算两个上限（4.3），最后装配并裁剪 DMA 命令（4.4）；4.5 给出完整的状态跳转全景。

### 4.1 上下文读取：ReadCtxStr_s 与 ReadCtxWin_s 的多拍协议

#### 4.1.1 概念说明

仲裁器在 `CheckPrio*_s` 选定 `HndlStream` 后，状态机进入 `ReadCtxStr_s`。它要做的事情是：把这一路流的 5 个流上下文字段（`winEnd`、`winSize`、`ptr`、SCFG、`bufstart`）全部读回来，存进以 `Hndl` 开头的一组寄存器，供后面的 `CalcAccess` 使用。

为什么不能一拍读完？因为（a）这些字段在 RAM 里分属 3 个不同的地址（由 `Sel` 选择），每次只能读一个地址；（b）同步 RAM 有 2 拍读延迟。所以状态机用一个计数器 `HndlCtxCnt` 在 0..4 之间走 5 拍，前 3 拍发命令、后 3 拍收响应（中间有重叠）。

读完流上下文后，状态机并不直接去算 DMA 命令，而是先经过 `First_s`（4.2），再到 `ReadCtxWin_s` 读窗口上下文（WINCNT，用于判断当前窗口是否已有数据、是否需要保护）。读完窗口上下文，才进入 `CalcAccess0_s`。

#### 4.1.2 核心流程

`ReadCtxStr_s` 的 5 拍流水（`HndlCtxCnt` 从 0 数到 4）：

```
拍号(cnt)   发出的读命令(Sel)            两拍后收到的响应 → 装配
---------  --------------------------   ----------------------------------------
0          Sel=Winend(10), Rd=1          (尚未到响应)
1          Sel=WinsizePtr(01), Rd=1      (尚未到响应)
2          Sel=ScfgBufstart(00), Rd=1    cnt=2: RdatLo → HndlWinEnd   (Winend 的响应)
3          (停止发命令)                   cnt=3: RdatLo → HndlWinSize
                                               RdatHi → HndlPtr0       (WinsizePtr 的响应)
4          (停止发命令)                   cnt=4: 拆 SCFG → HndlRingbuf/HndlOverwrite
                                                    /HndlWincnt/HndlWincur
                                               RdatHi → HndlBufstart    (ScfgBufstart 的响应)
                                          并立即计算 Hndl4kMax、HndlWinMax (见 4.3)
                                          → 跳 First_s
```

关键点：**命令和响应错开整整 2 拍**。`cnt=0` 发 Winend 的命令，`cnt=2` 才收 Winend 的响应。这就是 `ReadCtxStr_s` 必须跑到 `cnt=4` 的原因——为了把 `cnt=2` 发的最后一个命令（ScfgBufstart）的响应收回来。

`ReadCtxWin_s` 只有 3 拍（`HndlCtxCnt` 0..2），只读一个字段（`WincntWinlast`）：`cnt=0` 发命令，`cnt=2` 收响应并把 WINCNT 从「采样数」左移成「字节数」存进 `HndlWinBytes`。

#### 4.1.3 源码精读

先看状态枚举，确认本讲涉及的 5 个状态都在内：

[hdl/psi_ms_daq_daq_sm.vhd:144-144](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L144-L144) 定义了 14 个状态，其中 `ReadCtxStr_s`、`First_s`、`ReadCtxWin_s`、`CalcAccess0_s`、`CalcAccess1_s` 就是本讲的命令路径。

`ReadCtxStr_s` 完整代码：

[hdl/psi_ms_daq_daq_sm.vhd:339-373](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L339-L373) 做三件事：

- **状态推进**（339-346 行）：`cnt` 数到 4 就跳 `First_s` 并清零，否则 `cnt+1`。
- **命令断言**（348-358 行）：按 `cnt` 给出 `Sel` 并置 `Rd='1'`。注意 `cnt=0/1/2` 分别发 Winend、WinsizePtr、ScfgBufstart 三个不同的 `Sel`；`cnt=3/4`（`others`）不再发命令——因为命令已经发完了，只剩等响应。
- **响应装配**（360-373 行）：按 `cnt` 把两拍前那个命令的响应塞进 `Hndl*` 寄存器。最关键的是 `cnt=4` 这一拍（370-371 行）顺手算出了两个上限：

```vhdl
v.Hndl4kMax  := std_logic_vector(to_unsigned(4096, 13) - unsigned(r.HndlPtr0(11 downto 0)));
v.HndlWinMax := std_logic_vector(unsigned(r.HndlWinEnd) - unsigned(r.HndlPtr0));
```

这两行的含义留到 4.3 详细讲。这里只需注意：**它们在 `cnt=4` 这一拍计算，前提是 `HndlPtr0`、`HndlWinEnd` 此时已经装配好**——而 `HndlPtr0`/`HndlWinEnd` 正好在 `cnt=3`/`cnt=2` 装配，时序上刚好赶得上。

SCFG 字段的拆分（365-368 行）用了 `CtxStr_Sft_*` 常量，把一个 32 位的 SCFG 切成 ringbuf(bit0)、overwrite(bit8)、winCnt(bit16..)、winCur(bit24..)。这些常量定义在：

[hdl/psi_ms_daq_pkg.vhd:73-79](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_pkg.vhd#L73-L79) —— `CtxStr_Sel_*` 三个选择值（00/01/10）与 `CtxStr_Sft_*` 四个位偏移。

`ReadCtxWin_s` 完整代码：

[hdl/psi_ms_daq_daq_sm.vhd:398-425](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L398-L425) 与流上下文读法同构，只是更短：

- `cnt` 数到 2 就跳 `r.HndlAfterCtxt`（命令路径是 `CalcAccess0_s`，响应路径是 `ProcResp0_s`）。
- `cnt=0` 发 `CtxWin_Sel_WincntWinlast_c` 读命令。
- `cnt=2` 把响应处理成 `HndlWinBytes`：

```vhdl
v.HndlWinBytes := '0' & shift_left(CtxWin_Resp.RdatLo, Log2StrBytes_c(i));
```

WINCNT 在 RAM 里以**采样数**存储（驱动写回时也是采样数），这里左移 `log2(样本字节数)` 转成**字节数**。最高位补一个 `0` 作为保护位（guard bit），因为 `HndlWinBytes` 声明为 33 位（见 [hdl/psi_ms_daq_daq_sm.vhd:181-181](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L181-L181)），防止后续 `HndlWinBytes + Size` 累加溢出影响符号判断。外层那个 `for i in 0 to Streams_g-1` 循环是 Vivado 综合的 workaround——因为 `HndlStream` 不是局部静态值，不能直接写 `Log2StrBytes_c(r.HndlStream)`，只能用循环加 `if i = r.HndlStream` 等效索引。

#### 4.1.4 代码实践

**实践目标**：在不跑仿真的前提下，仅靠读源码画出 `ReadCtxStr_s` 的「命令/响应时序表」，确认 2 拍延迟。

**操作步骤**：

1. 打开 [hdl/psi_ms_daq_daq_sm.vhd:339-373](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L339-L373)。
2. 准备一张 5 列表：`cnt`、`CtxStr_Cmd.Sel`、`CtxStr_Cmd.Rd`、`CtxStr_Resp 来源`、`被装配的 Hndl 寄存器`。
3. 逐拍填写。注意「命令」一列在 `cnt=0,1,2` 有值、`cnt=3,4` 为空；「响应」一列在 `cnt=2,3,4` 有值、`cnt=0,1` 为空。

**需要观察的现象**：`Sel=Winend` 的命令出现在 `cnt=0`，而它的响应 `RdatLo → HndlWinEnd` 出现在 `cnt=2`，正好相差 2 拍。

**预期结果**：填出的表应与 4.1.2 中的流水表完全一致，能清楚看到「命令窗口（0..2）」与「响应窗口（2..4）」重叠 1 拍、整体跨度 5 拍。如果对「为什么是 2 拍延迟」存疑，标注「待本地验证」并在仿真波形里量 `CtxStr_Cmd.Rd` 上升沿到 `CtxStr_Resp` 数据有效的拍数。

#### 4.1.5 小练习与答案

**练习 1**：如果把上下文 RAM 的读延迟从 2 拍改成 3 拍，`ReadCtxStr_s` 需要跑几拍？`HndlCtxCnt` 的判别值要怎么改？

**答案**：需要跑 6 拍（`cnt` 0..5）。命令仍在 `cnt=0,1,2` 发出，但响应要相应推迟到 `cnt=3,4,5` 才能装配；判别从 `if r.HndlCtxCnt = 4` 改成 `= 5`，最大值声明 `integer range 0 to 4`（[hdl/psi_ms_daq_daq_sm.vhd:166-166](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L166-L166)）也要扩到 `0 to 5`。

**练习 2**：`ReadCtxStr_s` 读取 SCFG 时，`HndlRingbuf`、`HndlOverwrite` 分别从 SCFG 的哪一位取出？为什么 `winCnt` 用一个位段范围而 `ringbuf` 用单 bit？

**答案**：`HndlRingbuf` 取 `CtxStr_Sft_SCFG_RINGBUF_c`=bit0，`HndlOverwrite` 取 bit8。`winCnt` 是多比特字段（窗口计数，取 `CtxStr_Sft_SCFG_WINCNT_c` 起的若干位），所以要用 `Sft + high downto Sft` 的位段；`ringbuf`/`overwrite` 是单 bit 标志，直接 `(bit)` 即可。位偏移定义见 [hdl/psi_ms_daq_pkg.vhd:76-79](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_pkg.vhd#L76-L79)。

### 4.2 首次访问重置：First_s 与 FirstAfterEna / FirstOngoing

#### 4.2.1 概念说明

流上下文 RAM 里的 `ptr`、`winEnd` 等字段，记录的是「上一次传输结束后」的状态。绝大多数情况下，状态机读到的 `ptr` 就是这次 DMA 的起始地址，直接用即可。

但有一个例外：**当一路流刚刚被（重新）使能时**，RAM 里的 `ptr` 可能是上次禁用前留下的旧值，甚至是上电后未定义的初值。此时软件期望这次采集从 `bufstart` 重新开始。为此状态机引入了「首次访问」机制：在流被禁用期间置位 `FirstAfterEna`，等流重新使能、状态机处理它的第一个命令时，把 `ptr`/`winEnd`/`winCur` 重置回 `bufstart` 起点。

为什么需要**两个**标志（`FirstAfterEna` 和 `FirstOngoing`）而不是一个？因为 `First_s` 这个状态**既被命令路径访问，也被响应路径访问**（两条路径都要先读上下文）。重置只应该在「命令路径的首次访问」发生一次，不能在随后的响应路径里重复触发。于是用了一次「握手」：`FirstAfterEna` 是「待办标志」，在 `First_s` 的命令路径里被搬进 `FirstOngoing`（实际生效标志）并随即清掉 `FirstAfterEna`，保证只生效一次。

#### 4.2.2 核心流程

```
[流被禁用]  →  每拍扫描：FirstAfterEna(stream) := '1', NewBuffer(stream) := '1'
                    │
                    │  (流重新使能，状态机经仲裁选中该流)
                    ▼
              CheckPrio*_s → ReadCtxStr_s → First_s
                                              │
                ┌─────────────────────────────┴─────────────────────────────┐
                │ 命令路径 (HndlAfterCtxt = CalcAccess0_s):                  │
                │   FirstAfterEna(s) := '0'          ← 清待办                 │
                │   FirstOngoing(s)  := FirstAfterEna(s)  ← 搬到生效位        │
                │   若 FirstOngoing(s)='1':                                   │
                │       HndlWinEnd := bufstart + winSize                     │
                │       HndlPtr0   := bufstart                               │
                │       HndlWincur := 0                                      │
                │       Hndl4kMax  := 4096 - bufstart[11:0]                  │
                │       HndlWinMax := winSize                                │
                │ 响应路径 (HndlAfterCtxt = ProcResp0_s): 什么都不做          │
                └────────────────────────────────────────────────────────────┘
                                              │
                                              ▼
                                        ReadCtxWin_s → ...
```

注意 `First_s` 本身不改 `v.State` 之外的状态机走向：它无条件 `v.State := ReadCtxWin_s`（[hdl/psi_ms_daq_daq_sm.vhd:378-378](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L378-L378)），只在「首次」时顺手重写 `Hndl*` 几个寄存器。

#### 4.2.3 源码精读

「禁用流」时置位 `FirstAfterEna` 的扫描循环：

[hdl/psi_ms_daq_daq_sm.vhd:561-567](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L561-L567) 每拍检查每路流：只要全局未使能（`GlbEnaReg='0'`）或该流未使能（`StrEnaReg(str)='0'`），就把 `FirstAfterEna(str)` 和 `NewBuffer(str)` 置 1。这段代码在 `case r.State` **之外**，意味着无论状态机当前在哪个状态，禁用检测都持续生效——这是它能「随时标记待办」的关键。

`First_s` 状态本体：

[hdl/psi_ms_daq_daq_sm.vhd:376-395](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L376-L395) 是本讲最精巧的一段。逐句解读：

```vhdl
if r.HndlAfterCtxt = ProcResp0_s then
  -- 响应路径：什么都不做
else  -- 命令路径
  v.FirstAfterEna(r.HndlStream) := '0';              -- 清待办
  v.FirstOngoing(r.HndlStream)  := r.FirstAfterEna(r.HndlStream);  -- 搬到生效位
end if;

if v.FirstOngoing(r.HndlStream) = '1' then           -- 仅首次访问重置
  v.HndlWinEnd := unsigned(r.HndlBufstart) + unsigned(r.HndlWinSize);
  v.HndlPtr0   := r.HndlBufstart;
  v.HndlWincur := (others => '0');
  v.Hndl4kMax  := to_unsigned(4096,13) - r.HndlBufstart(11 downto 0);
  v.HndlWinMax := r.HndlWinSize;
end if;
```

几个要点：

- **命令路径才搬标志**：响应路径（`HndlAfterCtxt = ProcResp0_s`）走 `then` 分支什么都不做，既不动 `FirstAfterEna` 也不动 `FirstOngoing`。所以即使首次命令之后紧跟着首次响应（响应路径也会经过 `First_s`），重置不会被重复触发。
- **先搬后用**：`FirstOngoing` 在同一拍内先被赋成 `FirstAfterEna` 的旧值，紧接着下面的 `if v.FirstOngoing=...` 就用这个新值判断。VHDL 变量语义保证「搬」和「用」在同一拍内顺序执行。
- **重置内容**：把 `HndlPtr0` 钉到 `bufstart`、`HndlWinEnd` 设成 `bufstart + winSize`、`HndlWincur` 清零，并**重算** `Hndl4kMax`/`HndlWinMax`（因为指针变了，两个上限也得跟着用新指针重算——否则会沿用 `ReadCtxStr_s` 里用旧 `ptr` 算出的错误上限）。

#### 4.2.4 代码实践

**实践目标**：用 git 历史或测试用例确认「首次访问重置」确实发生过。

**操作步骤**：

1. 打开测试平台 `tb/psi_ms_daq_daq_sm/psi_ms_daq_daq_sm_tb_case_enable.vhd`（使能相关用例）。
2. 搜索其中对 `bufstart`、首次 `ExpectDmaCmd` 的断言，观察用例是否在流使能后、第一次传输时，期望 `Dma_Cmd.Address` 等于 `bufstart`（即 `16#01230000#` 一类）。
3. 对照 [hdl/psi_ms_daq_daq_sm.vhd:389-395](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L389-L395) 确认：首次访问时 `HndlPtr0 := HndlBufstart`，所以 `CalcAccess0_s` 里 `Dma_Cmd.Address := r.HndlPtr0` 就等于 `bufstart`。

**需要观察的现象**：用例在 `StrEna` 从 0→1 之后的第一个 `Dma_Cmd_Vld` 脉冲，其 `Address` 应等于软件配置的 `BUFSTART`，而不是 RAM 里残留的旧 `ptr`。

**预期结果**：能找到一条断言证明「使能后首传地址 = bufstart」。若用例书写方式不易直接看出，标注「待本地验证」并在波形里跟踪 `r.FirstAfterEna`、`r.FirstOngoing`、`HndlPtr0` 三者在使能前后的变化。

#### 4.2.5 小练习与答案

**练习 1**：假如把 `First_s` 里的 `v.FirstOngoing(...) := r.FirstAfterEna(...)` 改成直接写 `v.FirstOngoing(...) := '1'`，会出什么问题？

**答案**：那样只要走命令路径就一定触发重置，无法区分「首次」和「非首次」。后果是每次发命令都把 `ptr` 强制拉回 `bufstart`，已写入的数据会被覆盖、窗口逻辑全乱。`FirstAfterEna` 这个「待办」标志的作用就是让重置**只在禁用→使能过渡后的第一次**发生。

**练习 2**：为什么响应路径经过 `First_s` 时不能也执行重置？

**答案**：响应路径（`HndlAfterCtxt = ProcResp0_s`）处理的是「一次已发出 DMA 的回响」，此时 `ptr` 必须沿用命令发出时的值来推进（`HndlPtr1 := HndlPtr0 + Size`）。若在响应路径也重置 `ptr` 到 `bufstart`，会丢失「这次传输真正写到了哪里」的信息，导致后续窗口切换、回写全错。所以源码用 `if HndlAfterCtxt = ProcResp0_s` 把响应路径显式排除。

### 4.3 两个上限的计算：Hndl4kMax 与 HndlWinMax

#### 4.3.1 概念说明

DMA 命令的 `MaxSize`（这次最多写多少字节）受到三条约束，本模块讲其中两条由地址几何关系决定的上限：

1. **AXI 4KB 边界约束（`Hndl4kMax`）**：AXI 协议规定，单次突发事务**不得跨越 4KB 地址边界**（4KB 是大多数内存系统的最小页大小，跨页会引发内存控制器/互联的额外处理甚至错误）。所以从当前 `ptr` 出发，最多只能写到「下一个 4KB 边界」为止。
2. **窗口末尾约束（`HndlWinMax`）**：每个窗口在内存里是一段连续区域 `[bufstart + winIdx*winSize, ... + winSize]`。当前窗口的写指针绝不能越过本窗口的 `winEnd`，否则会写到下一个窗口（或越界）。

两条上线都以**字节**为单位。第三个约束（不超过软件设定的 `MaxBurstSize_g`）在 4.4 讲。

#### 4.3.2 核心流程

设当前写指针为 `ptr`（字节地址）、当前窗口末尾为 `winEnd`（字节地址）。

**4KB 边界剩余**：4KB 边界就是地址低 12 位全为 0 的那些位置。`ptr` 距离下一个 4KB 边界的字节数为：

\[
\text{Hndl4kMax} = 4096 - \text{ptr}[11{:}0]
\]

当 `ptr` 恰好在 4KB 边界上（`ptr[11:0]=0`）时，`Hndl4kMax = 4096`，允许写满一整页；当 `ptr[11:0]=384` 时，`Hndl4kMax = 4096-384 = 3712`。由于结果范围是 1..4096（4096 需要 13 位表示，`2^12=4096` 在 12 位里会溢出为 0），所以 `Hndl4kMax` 声明为 **13 位**（[hdl/psi_ms_daq_daq_sm.vhd:178-178](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L178-L178)）。

**窗口末尾剩余**：

\[
\text{HndlWinMax} = \text{winEnd} - \text{ptr}
\]

这是全 32 位地址减法（[hdl/psi_ms_daq_daq_sm.vhd:179-180](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L179-L180)）。正常情况下 `winEnd >= ptr`，结果非负。

这两个值在两处计算：

- **常规情况**：`ReadCtxStr_s` 的 `cnt=4` 拍，用刚装配好的 `HndlPtr0`/`HndlWinEnd` 计算（[hdl/psi_ms_daq_daq_sm.vhd:370-371](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L370-L371)）。
- **首次访问情况**：`First_s` 里 `ptr` 被重置成 `bufstart`，所以必须用 `bufstart` 重算（[hdl/psi_ms_daq_daq_sm.vhd:393-394](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L393-L394)）。

#### 4.3.3 源码精读

常规计算（`ReadCtxStr_s` 内）：

[hdl/psi_ms_daq_daq_sm.vhd:370-371](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L370-L371) 注释明写「Calculate maximum size within this 4k Region」和「within this window」。注意 `Hndl4kMax` 只取 `HndlPtr0` 的低 12 位参与运算，这正是「只关心 4KB 页内偏移」的体现。

首次访问重算（`First_s` 内）：

[hdl/psi_ms_daq_daq_sm.vhd:393-394](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L393-L394) 用 `HndlBufstart` 替代 `HndlPtr0`，`HndlWinSize` 替代 `HndlWinEnd-HndlPtr0`（因为此时 `winEnd = bufstart + winSize`，所以 `winEnd - bufstart = winSize`，直接赋 `winSize` 即可）。

`Hndl4kMax` / `HndlWinMax` 的声明：

[hdl/psi_ms_daq_daq_sm.vhd:178-180](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L178-L180) —— `Hndl4kMax` 为 13 位、`HndlWinMax` 为 32 位。

#### 4.3.4 代码实践

**实践目标**：用测试平台里现成的 4KB 边界用例，验证 `Hndl4kMax` 的算式。

**操作步骤**：

1. 打开 [tb/psi_ms_daq_daq_sm/psi_ms_daq_daq_sm_tb_case_single_simple.vhd](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_daq_sm/psi_ms_daq_daq_sm_tb_case_single_simple.vhd)。
2. 找到 TestCase 6「Check 4k boundary limitation」（约 153-159 行设 `Inp_Level`、252-260 行用 `ExpectDmaCmd` 校验）。
3. 读到期望值：`Address => 16#01238000#+384`，`MaxSize => Size4k_c-384`。

**需要观察的现象**：`ptr = 0x01238000 + 384`，其低 12 位 `ptr[11:0] = 384 = 0x180`。按本模块算式，`Hndl4kMax = 4096 - 384 = 3712`，而 `Size4k_c - 384 = 4096 - 384 = 3712`，两者完全一致——这就是测试平台期望的 `MaxSize`。

**预期结果**：手算 `Hndl4kMax = 3712` 字节，与 `ExpectDmaCmd` 的 `MaxSize => Size4k_c-384` 吻合。这说明 4KB 边界约束在这一拍是「卡脖子」的上限（窗口足够大、`MaxBurstSize` 也足够大，只有 4KB 边界在限制）。

#### 4.3.5 小练习与答案

**练习 1**：`Hndl4kMax` 为什么是 13 位而不是 12 位？

**答案**：因为结果范围是 1..4096，而 4096 在 12 位无符号数里无法表示（`2^12 = 4096` 会溢出成 0）。需要 13 位才能表示「正好还剩 4096 字节」（即 `ptr` 恰在 4KB 边界上）这种情况。见声明 [hdl/psi_ms_daq_daq_sm.vhd:178-178](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L178-L178)。

**练习 2**：在 `First_s` 里，为什么 `HndlWinMax` 直接赋成 `HndlWinSize`，而不是写 `HndlWinEnd - HndlBufstart`？

**答案**：因为首次访问时刚把 `HndlWinEnd` 设成 `HndlBufstart + HndlWinSize`（389-390 行），所以 `HndlWinEnd - HndlBufstart` 恒等于 `HndlWinSize`。直接赋 `HndlWinSize` 省一次减法、综合更省，逻辑等价。这也是「重置后两个上限必须用新指针重算」的体现。

### 4.4 DMA 命令装配与 MaxSize 裁剪：CalcAccess0_s 与 CalcAccess1_s

#### 4.4.1 概念说明

读完上下文、处理好首次访问、算好两个上限之后，状态机进入 `CalcAccess0_s` / `CalcAccess1_s`，把一切装配成最终发给 DMA 引擎的命令 `Dma_Cmd`（类型 `DaqSm2DaqDma_Cmd_t`，含 `Address`、`MaxSize`、`Stream` 三个字段）。

装配分两拍：

- **`CalcAccess0_s`**：填 `Address = ptr`、`Stream = HndlStream`、`MaxSize = MaxBurstSize_g * 8`（第三个约束：软件设定的最大突发长度，字转字节）。同时做一次**窗口保护检查**——若当前窗口已被软件占用且不允许覆盖，则放弃这次命令、给该流置 `WinProtected`。
- **`CalcAccess1_s`**：把 `MaxSize` 与「两个上限中的较小者」比较，超过就裁剪。最后置 `Dma_Cmd_Vld='1'` 把命令发出去，回到 `Idle_s`。

最终生效的传输长度是三条约束的交集：

\[
\text{MaxSize}_{\text{final}} = \min\big(\ \text{MaxBurstSize}_g \times 8,\ \ \text{Hndl4kMax},\ \ \text{HndlWinMax}\ \big)
\]

#### 4.4.2 核心流程

```
CalcAccess0_s:
  Dma_Cmd.Address := HndlPtr0
  Dma_Cmd.Stream  := HndlStream
  Dma_Cmd.MaxSize := MaxBurstSize_g * 8          -- 第 3 个约束（字节）
  -- 窗口保护检查（winOverwrite=false 才生效，详见 u4-l5）:
  if (HndlOverwrite='0') and (HndlWinBytes/=0) and (NewBuffer(s)='1') then
      State := Idle_s            -- 放弃，窗口被占
      WinProtected(s) := '1'
  else
      State := CalcAccess1_s
      NewBuffer(s)   := '0'
      OpenCommand(s) := '1'      -- 标记该流有一条命令在飞

CalcAccess1_s:
  -- 在 Hndl4kMax 与 HndlWinMax 中取较小者作为上限:
  if Hndl4kMax < HndlWinMax:
      if MaxSize > Hndl4kMax:  MaxSize := Hndl4kMax
  else:
      if MaxSize > HndlWinMax: MaxSize := HndlWinMax
  Dma_Cmd_Vld := '1'            -- 发命令
  State := Idle_s
```

`OpenCommand(s):='1'` 这一步很关键：它告诉仲裁器「这一路已经有一条 DMA 命令在飞了，别再给它发新命令」，直到响应处理（`ProcResp0_s`）把它清掉。这是避免同一流出现多条在途命令的机制（详见 [u4-l5](u4-l5-window-protection-overwrite.md)）。

#### 4.4.3 源码精读

`CalcAccess0_s`：

[hdl/psi_ms_daq_daq_sm.vhd:428-442](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L428-L442) 三件事一目了然：

- 430-432 行装配 `Dma_Cmd` 三个字段。注意 432 行 `MaxBurstSize_g * 8`，注释「8 bytes per 64-bit QWORD」——把「字数」换算成「字节数」。
- 434-436 行是窗口保护短路：`winOverwrite='0'` 且窗口已有数据（`HndlWinBytes/=0`）且 `NewBuffer='1'`（该窗口刚被切走、尚未被软件释放）时，拒绝写入，置 `WinProtected`，回 `Idle_s`。
- 437-442 行是正常路径：进 `CalcAccess1_s`，清 `NewBuffer`，置 `OpenCommand`。

> 说明：`HndlWinBytes` 来自 4.1 读回的窗口上下文（WINCNT 转字节），表示当前窗口已写入字节数。`NewBuffer` 的完整语义（窗口切换时置位、软件 `MarkAsFree` 后清除）在 [u4-l5](u4-l5-window-protection-overwrite.md) 详述。

`CalcAccess1_s`：

[hdl/psi_ms_daq_daq_sm.vhd:444-455](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L444-L455) 是本讲的「收口」。逻辑是「先在两个几何上限里取较小者，再把 `MaxSize` 削到这个较小者以下」：

```vhdl
if unsigned(r.Hndl4kMax) < unsigned(r.HndlWinMax) then
  if unsigned(r.Dma_Cmd.MaxSize) > unsigned(r.Hndl4kMax) then
    v.Dma_Cmd.MaxSize := resize(r.Hndl4kMax, ...);
  end if;
else
  if unsigned(r.Dma_Cmd.MaxSize) > unsigned(r.HndlWinMax) then
    v.Dma_Cmd.MaxSize := resize(r.HndlWinMax, ...);
  end if;
end if;
v.Dma_Cmd_Vld := '1';
v.State := Idle_s;
```

注意几个细节：

- `Hndl4kMax`（13 位）和 `HndlWinMax`（32 位）位宽不同，但 `unsigned` 比较会自动按零扩展对齐，语义正确。
- 用 `resize` 把上限扩到 `Dma_Cmd.MaxSize` 的位宽（16 位）再赋值。
- 若 `MaxSize` 本来就不超过较小上限（数据量小），则**保持不变**——这正是「数据不足时按实际数据量传」的来源（实际数据量在响应路径由 `Dma_Resp.Size` 体现，见 [u3-l3](u3-l3-sm-window-switch-ringbuf.md)）。
- 最后置 `Dma_Cmd_Vld='1'` 并回 `Idle_s`，命令发出，本轮结束。

`DaqSm2DaqDma_Cmd_t` 类型定义（供查阅字段位宽）：

[hdl/psi_ms_daq_pkg.vhd:46-53](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_pkg.vhd#L46-L53) —— `Address` 32 位、`MaxSize` 16 位、`Stream` 整数。

#### 4.4.4 代码实践

**实践目标**：用测试平台的「窗口大小限制」用例，验证 `HndlWinMax` 的裁剪。

**操作步骤**：

1. 打开 [tb/psi_ms_daq_daq_sm/psi_ms_daq_daq_sm_tb_case_single_simple.vhd](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_daq_sm/psi_ms_daq_daq_sm_tb_case_single_simple.vhd)。
2. 找到 TestCase 7「Check window size limitation」（约 162-167 行设 `Inp_Level`、262-270 行 `ExpectDmaCmd`、516-532 行 `ctx` 用 `ExpCtxFullBurst` 配 `WinSize => 502`）。
3. 读到期望值：`Address => 16#01230000#`，`MaxSize => 502`。

**需要观察的现象**：该用例配置 `WinSize = 502` 字节、`bufstart = 0x01230000`。此时 `ptr = bufstart`，`winEnd = bufstart + 502`，于是 `HndlWinMax = 502`，`Hndl4kMax = 4096 - 0 = 4096`，`MaxBurstSize_g*8 = 4096`。三者取 min 得 502，所以 `MaxSize` 被裁到 502。

**预期结果**：手算 `min(4096, 4096, 502) = 502`，与 `ExpectDmaCmd` 的 `MaxSize => 502` 完全吻合。这一拍是窗口末尾约束在「卡脖子」。

#### 4.4.5 小练习与答案

**练习 1**：`CalcAccess1_s` 里若 `Hndl4kMax = HndlWinMax`（两者相等），会走哪个分支？结果对吗？

**答案**：走 `else` 分支（因为判断是 `Hndl4kMax < HndlWinMax`，相等时为假），裁剪到 `HndlWinMax`。由于此时 `HndlWinMax = Hndl4kMax`，裁到哪个都一样，结果正确。

**练习 2**：`CalcAccess0_s` 里 `OpenCommand(HndlStream) := '1'` 是在**哪个分支**置位的？为什么不在保护短路（拒绝写入）那一支也置？

**答案**：在 `else`（正常）分支置位，即 [hdl/psi_ms_daq_daq_sm.vhd:441-441](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L441-L441)。因为 `OpenCommand` 表示「该流已有一条 DMA 命令在飞」——只有真正发出了命令（进 `CalcAccess1_s` 并置 `Dma_Cmd_Vld`）才应该置位。保护短路那一支**没有**发出命令（直接回 `Idle_s`），所以绝不能置 `OpenCommand`，否则该流会被永久「挂起」、再也无法被仲裁选中。

### 4.5 命令路径状态跳转全景

#### 4.5.1 概念说明

把前 4 个模块串起来，命令路径（从仲裁选中到发出 `Dma_Cmd`）一共经过 6 个状态、约 11 拍。本模块用一张图把这条链路完整画出，作为本讲的总纲。

#### 4.5.2 核心流程

```
Idle_s ──(IrqFifoAlmFull=0)──> CheckPrio1_s ──┐
                                               │ (GrantVldReg(1..3) 命中某档)
                                               ▼
          HndlAfterCtxt := CalcAccess0_s   ReadCtxStr_s   (5拍: cnt 0..4, 读流上下文)
                                               │
                                               ▼
                                           First_s        (1拍: 处理首次访问重置)
                                               │
                                               ▼
                                           ReadCtxWin_s   (3拍: cnt 0..2, 读窗口上下文)
                                               │
                                               ▼  (HndlAfterCtxt)
                                           CalcAccess0_s  (1拍: 装Address/Stream/MaxSize, 保护检查)
                                               │
                                               ▼
                                           CalcAccess1_s  (1拍: 裁剪MaxSize, 发Dma_Cmd_Vld)
                                               │
                                               ▼
                                            Idle_s
```

总耗时约 `5 + 1 + 3 + 1 + 1 = 11` 个 `Clk` 周期（不含仲裁与 `Idle_s` 的等待）。这条链路每一拍都在 [hdl/psi_ms_daq_daq_sm.vhd:261-559](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L261-L559) 的 `case r.State` 大块里。

`HndlAfterCtxt` 这条「返回去哪」的线索在两处置位：命令路径在 `Idle_s` 设成 `CalcAccess0_s`（[hdl/psi_ms_daq_daq_sm.vhd:269-269](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L269-L269)），响应路径在 `CheckResp_s` 设成 `ProcResp0_s`（[hdl/psi_ms_daq_daq_sm.vhd:330-330](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L330-L330)）。`ReadCtxWin_s` 末尾用 `v.State := r.HndlAfterCtxt`（[hdl/psi_ms_daq_daq_sm.vhd:401-401](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L401-L401)）实现「读完上下文后，按来路分流」。

#### 4.5.3 源码精读

入口与分流的关键三行：

- [hdl/psi_ms_daq_daq_sm.vhd:268-269](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L268-L269)：`Idle_s` 进入仲裁前设定命令路径的「归宿」。
- [hdl/psi_ms_daq_daq_sm.vhd:283-284](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L283-L284)：`CheckPrio1_s` 命中后选流、进 `ReadCtxStr_s`（prio2/prio3 同构，见 295-297、308-310 行）。
- [hdl/psi_ms_daq_daq_sm.vhd:401-401](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L401-L401)：`ReadCtxWin_s` 末尾按 `HndlAfterCtxt` 分流。

#### 4.5.4 代码实践

**实践目标**：在波形/源码层面把命令路径的 6 个状态名按顺序背下来，并能在 `case r.State` 里快速定位每个状态。

**操作步骤**：

1. 打开 [hdl/psi_ms_daq_daq_sm.vhd:261-559](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L261-L559)。
2. 用编辑器搜索 `when ReadCtxStr_s`、`when First_s`、`when ReadCtxWin_s`、`when CalcAccess0_s`、`when CalcAccess1_s`，记下各自行号。
3. 在脑中（或纸上）按 4.5.2 的流程图连成链。

**需要观察的现象**：5 个状态在源码里出现的顺序与执行顺序一致（`ReadCtxStr_s` → `First_s` → `ReadCtxWin_s` → `CalcAccess0_s` → `CalcAccess1_s`）。

**预期结果**：能在不看流程图的情况下，说出每个状态的「下一状态」与「核心动作」。

#### 4.5.5 小练习与答案

**练习 1**：命令路径发出一个 `Dma_Cmd` 至少要经过几个状态、大约几拍？

**答案**：6 个状态（`Idle_s`→`CheckPrio*_s`→`ReadCtxStr_s`→`First_s`→`ReadCtxWin_s`→`CalcAccess0_s`→`CalcAccess1_s`→`Idle_s`，其中 `CheckPrio*_s` 视优先级档可能跳多级）。核心计算链 `ReadCtxStr_s`(5) + `First_s`(1) + `ReadCtxWin_s`(3) + `CalcAccess0_s`(1) + `CalcAccess1_s`(1) ≈ 11 拍。

**练习 2**：`HndlAfterCtxt` 这个寄存器解决了什么问题？如果删掉它、把 `ReadCtxWin_s` 的下一状态硬编码成 `CalcAccess0_s`，会怎样？

**答案**：它让 `ReadCtxStr_s → First_s → ReadCtxWin_s` 这段「读上下文」的公共子链路能被**命令路径**和**响应路径**复用，仅在末尾分流。若硬编码成 `CalcAccess0_s`，响应路径处理完上下文后就无法进入 `ProcResp0_s`，DMA 响应（指针推进、窗口切换、回写）将彻底无法处理。它是「公共前缀 + 末尾分流」模式的开关。

## 5. 综合实践

**任务**：手工计算一次 DMA 的 `Dma_Cmd.Address` 与 `Dma_Cmd.MaxSize`，要求同时满足「不跨越 4KB AXI 边界」与「不超过当前窗口末尾」两个约束。

**给定条件**（模拟一组软件配置）：

| 参数 | 值 | 说明 |
|---|---|---|
| `bufstart` | `0x0123_0800` | 缓冲区起始地址（字节） |
| `winSize` | `0x0000_0800`（2048 字节） | 单个窗口大小 |
| 当前 `ptr` | `0x0123_0F00` | 上一次传输结束后的写指针 |
| `winEnd` | `bufstart + winSize = 0x0123_1000` | 当前窗口末尾 |
| `MaxBurstSize_g` | 512（QWORD） | 软件设定的最大突发长度 |
| `winOverwrite` | `true` | 允许覆盖（跳过窗口保护） |
| 是否首次访问 | 否 | `FirstOngoing = 0` |

**要求按以下步骤推导**（建议拿纸笔算，把每步结果填进去）：

1. **地址**：`Dma_Cmd.Address = ptr = ?`
2. **第 3 约束**：`MaxSize`（字节）= `MaxBurstSize_g * 8 = ?`
3. **4KB 边界上限**：`ptr[11:0] = ?`，`Hndl4kMax = 4096 - ptr[11:0] = ?`
4. **窗口末尾上限**：`HndlWinMax = winEnd - ptr = ?`
5. **取较小上限**：`min(Hndl4kMax, HndlWinMax) = ?`
6. **最终裁剪**：`Dma_Cmd.MaxSize = min(MaxSize, 较小上限) = ?`
7. **核对**：这次传输会写到哪个地址结束？是否越过了 `winEnd`？是否跨越了 4KB 边界？

**参考答案**（自己先算再对照）：

1. `Address = 0x0123_0F00`。
2. `MaxBurstSize_g * 8 = 512 * 8 = 4096` 字节。
3. `ptr[11:0] = 0xF00 = 3840`；`Hndl4kMax = 4096 - 3840 = 256` 字节。
4. `HndlWinMax = 0x0123_1000 - 0x0123_0F00 = 0x100 = 256` 字节。
5. `min(256, 256) = 256`（两者恰好相等，走 `CalcAccess1_s` 的 `else` 分支，裁到 `HndlWinMax=256`）。
6. `MaxSize = min(4096, 256) = 256` 字节。
7. 传输写到 `0x0123_0F00 + 256 = 0x0123_1000 = winEnd`，**正好到窗口末尾、也正好到 4KB 边界**，两个约束同时卡住，没有越界。响应回来后 `ptr` 会推进到 `winEnd`，触发窗口切换（详见 [u3-l3](u3-l3-sm-window-switch-ringbuf.md)）。

**延伸（选做）**：把 `ptr` 改成 `0x0123_0E00`（其余不变），重算一遍。此时 `Hndl4kMax = 4096 - 0xE00 = 512`，`HndlWinMax = 0x1000 - 0xE00 = 0x200 = 512`，两者又相等，`MaxSize = 512`。可见当 `winEnd` 恰在 4KB 边界上时，两个上限会在同一点收敛——这也是为什么工程上常把 `winSize` 取成 4KB 的整除值，让窗口边界与 AXI 页边界天然对齐。

## 6. 本讲小结

- 状态机选中流后，用 `ReadCtxStr_s`（5 拍）和 `ReadCtxWin_s`（3 拍）以 `HndlCtxCnt` 计数、带 2 拍 RAM 读延迟，把流/窗口上下文读回并装配进 `Hndl*` 寄存器；命令在 `cnt=0..2` 发出、响应在 `cnt=2..4`（流）或 `cnt=2`（窗口）收回。
- `First_s` 用 `FirstAfterEna`（禁用期间持续置位的「待办」）→ `FirstOngoing`（命令路径生效一次的「执行」）两步握手，在流（重新）使能后的首次访问里把 `ptr`/`winEnd`/`winCur` 重置回 `bufstart`，并用新指针重算两个上限；响应路径被显式排除，避免重复触发。
- `Hndl4kMax = 4096 - ptr[11:0]`（13 位，AXI 4KB 边界剩余字节）和 `HndlWinMax = winEnd - ptr`（32 位，窗口剩余字节）是两条几何上限，分别在 `ReadCtxStr_s` 的 `cnt=4` 拍和 `First_s` 里计算。
- `CalcAccess0_s` 装配 `Address = ptr`、`Stream`、`MaxSize = MaxBurstSize_g*8`（字转字节），并做窗口保护检查（`winOverwrite=false` 且窗口被占时拒绝、置 `WinProtected`）；`CalcAccess1_s` 把 `MaxSize` 裁到「两上限较小者」之下，置 `Dma_Cmd_Vld` 发出命令。
- 最终传输长度 = `min(MaxBurstSize_g*8, Hndl4kMax, HndlWinMax)`；单位上「水位/突发长度」按 64 位字计、「地址/MaxSize/上限」按字节计，二者以 `*8` 桥接。
- `HndlAfterCtxt` 寄存器让「读上下文」公共子链路在末尾按来路分流到 `CalcAccess0_s`（命令）或 `ProcResp0_s`（响应），是复用关键。

## 7. 下一步学习建议

本讲只讲了命令路径——「读上下文 → 算出 `Dma_Cmd`」。命令发出去之后呢？建议按以下顺序继续：

1. **[u3-l3 控制状态机：窗口切换、环形缓冲与上下文回写](u3-l3-sm-window-switch-ringbuf.md)**：这是本讲的天然续集。读 `ProcResp0_s` / `NextWin_s` / `WriteCtx_s`，看 DMA 响应回来后如何推进 `ptr`、如何在到达 `winEnd` 或触发时切换/回绕窗口、如何把更新写回上下文 RAM。
2. **[u3-l4 上下文存储模型：流上下文与窗口上下文](u3-l4-context-memory-model.md)**：深入 `reg_axi.vhd`，看清本讲里那些 `Sel` 字段到底怎么被译码成 RAM 的 B 口地址（`Stream & Sel`、`Stream & Window & Sel`），以及 64 位字段为何拆成高/低两块 32 位 RAM。
3. **[u4-l5 窗口保护、覆盖与 NewBuffer/FirstAfterEna 协议](u4-l5-window-protection-overwrite.md)**：把本讲在 `CalcAccess0_s` 一笔带过的 `WinProtected` / `NewBuffer` / `OpenCommand` 协议讲透，理解多窗口、不覆盖场景下的数据竞争防护。
4. **想验证本讲的计算**：可直接阅读并（有仿真环境时）运行 `tb/psi_ms_daq_daq_sm/psi_ms_daq_daq_sm_tb_case_single_simple.vhd` 的 TestCase 5/6/7，对照 `ExpectDmaCmd` 的期望值核对手算结果。
