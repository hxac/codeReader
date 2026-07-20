# 中断生成机制与 IRQ FIFO

## 1. 本讲目标

本讲深入 DAQ IP 核的**硬件侧中断生成机制**，全部围绕控制状态机 `psi_ms_daq_daq_sm` 中的一段「IRQ 处理」逻辑展开。读完本讲你应当能够：

- 说清楚一次 DMA 传输完成后，硬件是**如何**、在**哪一拍**产生发往 CPU 的 `StrIrq` 脉冲与伴随的 `StrLastWin` 窗口号的；
- 解释 IRQ 信息 FIFO（`i_irq_fifo`）为什么用 `Streams_g*3` 作为 almost-full 阈值，以及它如何通过 `IrqFifoAlmFull` **反向反压**状态机、从源头避免 FIFO 溢出；
- 理解 `TfDone`（AXI 传输完成脉冲）、`TfDoneCnt`（完成计数）、`TfDoneReg`（打一拍）与 IRQ FIFO 读出之间的时序配合；
- 用 `git show 3957ce3` 读懂 2024 年那次「切换窗口时丢中断」修复的 diff，并能解释修改前**为什么**会在窗口切换瞬间丢中断。

本讲只讲**硬件侧**的脉冲生成，不涉及 CPU 上 C 驱动如何读 `IRQVEC`、如何分窗口回调——那是下一讲 [u4-l2](u4-l2-driver-irq-window-callback.md) 的内容。中断在 `reg_axi` 中的聚合（`IrqOut = IRQVEC & IRQENA & Gcfg_IrqEna`）已在 [u3-l5](u3-l5-register-interface.md) 讲过，本讲直接承接：本讲产生的 `StrIrq` 就是 `reg_axi` 聚合的输入。

## 2. 前置知识

在进入源码前，先用三句话建立直觉。

**直觉一：传输完成 ≠ 立刻发中断。** DMA 把一帧数据写进 DDR 后，AXI 主接口会回送一个 `Done` 脉冲（顶层改名为 `TfDone`，见 [u2-l7](u2-l7-axi-master-interface.md)）。但「这一帧写完了」和「这一帧值得通知 CPU」不是同一件事：只有**填满了一个窗口或遇到触发**的那一帧，才需要打断 CPU。所以硬件需要一个小仓库，把「这次传输完成后该不该报、报给哪条流、窗口号是多少」先存起来，等条件凑齐再发脉冲。这个小仓库就是 IRQ FIFO。

**直觉二：两个异步的「节拍」要对齐。** 中断逻辑里有两个互相独立的节拍在跑：

- **写节拍**：状态机在 `NextWin_s` 处理一次 DMA 响应，决定要不要往 IRQ FIFO 写一条「流号 + 窗口号 + WinDone」记录；
- **读节拍**：AXI 主接口每完成一次传输给一个 `TfDone`，`TfDoneCnt` 累计「有几条记录等待被发出」。

读节拍每消费一个 `TfDone`，就从 IRQ FIFO 弹出一条记录，若该记录的 `WinDone` 位为 1，就向对应流发一个 `StrIrq` 脉冲。这两个节拍谁先到都有可能（testbench 专门测了「正序」和「反序」两种到达顺序），逻辑必须都正确。

**直觉三：要堵住溢出，只能在源头。** IRQ FIFO 容量有限。如果 CPU 一直不来读（即 `TfDone` 一直不来，因为 `TfDone` 实际上受 CPU 侧 `IRQENA` 间接的下游处理节奏牵连——这里简化理解为「发出 `StrIrq` 后到 CPU 应答需要时间」），FIFO 会被写满。一旦写满还继续写，记录就丢了。本设计选择**不让它写满**：当 FIFO 快满时，`IrqFifoAlmFull` 拉高，状态机在 `Idle_s` 就拒绝再发新的 DMA 命令——既然没有新命令，就不会有新响应，自然就不会有新的 IRQ 记录要入队。这就是「源头反压」。

> 名词速查：`StrIrq`（per-stream 中断脉冲）、`StrLastWin`（该中断对应的窗口号）、`TfDone`（transfer done，单次 AXI 写突发完成）、`WinDone`（本条记录对应的传输是否「完成了某个窗口」，即填满窗口或遇到触发）、`IrqFifoAlmFull`（IRQ FIFO 几乎满反压）。

## 3. 本讲源码地图

本讲几乎只看一个文件，但会牵出三条信号链：

| 文件 / 信号 | 作用 | 本讲角色 |
| --- | --- | --- |
| [hdl/psi_ms_daq_daq_sm.vhd](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd) | 控制状态机（IP 的「大脑」） | **唯一主角**，IRQ 逻辑全在这里 |
| └ 端口 `StrIrq` / `StrLastWin` / `TfDone` | 中断输出、传输完成输入 | 中断对外的「门面」 |
| └ `i_irq_fifo`（`psi_common_sync_fifo`） | IRQ 信息 FIFO | 缓存待发出的中断记录 |
| └ `IrqFifoWrite`（`NextWin_s`） | 入队：流号+窗口号+WinDone | 写节拍 |
| └ `TfDoneCnt` / `TfDoneReg` / `IrqFifoRead` | 完成计数与 FIFO 读出 | 读节拍 |
| └ `StrIrq`/`StrLastWin` 生成（`p_comb` 末尾） | 把 FIFO 弹出的记录变成脉冲 | 输出节拍 |
| └ `Idle_s` 里的 `IrqFifoAlmFull` 判断 | 源头反压 | 溢出保护 |
| [tb/psi_ms_daq_daq_sm/psi_ms_daq_daq_sm_tb_case_irq.vhd](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_daq_sm/psi_ms_daq_daq_sm_tb_case_irq.vhd) | IRQ 用例 testbench | 验证「每个完成的窗口恰好一次中断」 |

---

## 4. 核心概念与源码讲解

### 4.1 IRQ 信息 FIFO 的结构：一条记录装「流号 + 窗口号 + WinDone」

#### 4.1.1 概念说明

IRQ FIFO 是一个**同步 FIFO**（读写在同一个 `Clk` 域，不需要异步跨时钟域，因为状态机、AXI 主接口的 `Done` 都已经在 `ClkMem`/状态机时钟下同步过）。它缓存的是「**一次 DMA 传输完成后，要不要给某条流发中断、发的是哪个窗口号**」这条三字段记录：

- **流号 Stream**：`StreamBits_c` 位，告诉 `StrIrq` 向哪一路拉高；
- **窗口号 LastWinNr**：`log2ceil(Windows_g)` 位，告诉 CPU 这次完成的是第几个窗口（即 `StrLastWin`）；
- **WinDone**：1 位，标志「这次传输是否完成了一个窗口（填满或触发）」。**只有这一位为 1 的记录，弹出时才会真正产生 `StrIrq` 脉冲**；为 0 的记录也会被弹出、也会消耗一个 `TfDone`，但不发脉冲。

为什么会有 `WinDone=0` 的记录？因为状态机对**每一次非零大小的传输**都会往 IRQ FIFO 写一条记录（见 4.2），但只有「填满窗口/触发」那次才把 `WinDone` 置 1。这样设计让「传输完成计数 `TfDoneCnt`」和「FIFO 中的记录条数」严格一一对应，便于用 `TfDoneCnt` 来驱动 FIFO 的读出。

#### 4.1.2 核心流程

入队位拼接（`IrqFifoIn` 的字段从低位到高位）：

```
IrqFifoIn = [ WinDone (1位) | LastWinNr (log2ceil(Windows_g)位) | Stream (StreamBits_c位) ]
             ^IrqFifoIn'high                                              ^bit 0
```

出队时再按同样布局切片还原成 `IrqFifoGenIrq`（最高位，即 WinDone）、`IrqLastWinNr`、`IrqFifoStream`。FIFO 的数据宽度因此是 `StreamBits_c + log2ceil(Windows_g) + 1`。

FIFO 关键 generic：

- `depth_g => 2**StreamBits_c * 4`：容量至少是「流的数量向上取整到 2 的幂」再乘 4（`StreamBits_c = max(log2ceil(Streams_g), 1)`）；
- `alm_full_level_g => Streams_g * 3`：almost-full 阈值；
- `alm_full_on_g => true`、`ram_style_g => "distributed"`：开启 almost-full 标志，用分布式 RAM（容量小、速度快）。

#### 4.1.3 源码精读

先看连接 IRQ FIFO 的内部信号声明（注意 `IrqFifoIn`/`IrqFifoOut` 的宽度）：

[hdl/psi_ms_daq_daq_sm.vhd:135-141](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L135-L141) — 声明 `IrqFifoAlmFull`、`IrqFifoEmpty`、`IrqFifoGenIrq`、`IrqFifoStream`、`IrqLastWinNr` 以及入队/出队向量 `IrqFifoIn`/`IrqFifoOut`，宽度统一为 `StreamBits_c + log2ceil(Windows_g) + 1`。

入队向量的位拼接（**字段从 `r.*` 寄存器取值，所以写进去的是「上一拍」已稳定的值**）：

[hdl/psi_ms_daq_daq_sm.vhd:683-685](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L683-L685) — 低位放 `r.HndlStream`（流号），中间放 `r.HndlLastWinNr`（窗口号），最高位放 `r.HndlWinDone`（即弹出后的 `IrqFifoGenIrq`）。

FIFO 例化（`psi_common_sync_fifo`，开启 almost-full）：

[hdl/psi_ms_daq_daq_sm.vhd:688-705](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L688-L705) — `dat_i` 接 `IrqFifoIn`、`vld_i` 接 `r.IrqFifoWrite`、`dat_o` 接 `IrqFifoOut`、`rdy_i` 接 `r.IrqFifoRead`，并输出 `alm_full_o => IrqFifoAlmFull`、`empty_o => IrqFifoEmpty`。注意 `alm_full_level_g => Streams_g * 3`。

出队向量切片（**把 `dat_o` 还原成三个字段**）：

[hdl/psi_ms_daq_daq_sm.vhd:708-710](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L708-L710) — `IrqFifoStream`、`IrqLastWinNr`、`IrqFifoGenIrq` 分别取低段、中段、最高位。

`StrIrq`/`StrLastWin` 的对外端口（per-stream 中断脉冲、per-stream 窗口号）：

[hdl/psi_ms_daq_daq_sm.vhd:46-47](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L46-L47) — `StrIrq` 是 `std_logic_vector(Streams_g-1 downto 0)`，`StrLastWin` 是 `WinType_a`（每路 5 位窗口号，`MaxWindowsBits_c=5`）。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：把 IRQ FIFO 一条记录的位布局画出来，体会「字段拼接 ↔ 切片还原」的对称性。

**操作步骤**：

1. 打开 [hdl/psi_ms_daq_daq_sm.vhd:683-685](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L683-L685)（入队拼接）和 [:708-710](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L708-L710)（出队切片）。
2. 取默认配置 `Streams_g=4, Windows_g=4`：`StreamBits_c = max(log2ceil(4),1) = 2`，`log2ceil(4)=2`，所以一条记录共 `2+2+1 = 5` 位。
3. 在纸上画出 5 位的位段表，标注 bit0–bit1 = Stream、bit2–bit3 = LastWinNr、bit4 = WinDone。

**需要观察的现象**：入队用的索引（`StreamBits_c-1 downto 0`、`StreamBits_c+log2ceil(Windows_g)-1 downto StreamBits_c`、`'high`）和出队用的索引完全镜像。

**预期结果**：你能写出「stream=2、窗口=3、WinDone=1」时 `IrqFifoIn` 的二进制值 = `1_11_10` = `0b11110`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 FIFO 深度取 `2**StreamBits_c * 4`，而不是直接 `Streams_g * 4`？
**答案**：`2**StreamBits_c` 是「≥ Streams_g 的最小 2 的幂」（`StreamBits_c = max(log2ceil(Streams_g),1)`）。同步 FIFO 的深度通常是 2 的幂以简化地址译码，所以用向上取整后的值。例如 `Streams_g=3` 时 `StreamBits_c=2`，深度取 `4*4=16` 而非 `3*4=12`。

**练习 2**：`StrLastWin` 每路是 5 位（`WinType_t`），而 IRQ FIFO 里只存 `log2ceil(Windows_g)` 位。弹出时怎么对齐？
**答案**：见 [:584](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L584)，用 `resize(unsigned(IrqLastWinNr), 5)` 把 `log2ceil(Windows_g)` 位零扩展成 5 位再赋给 `StrLastWin`，所以 `Windows_g<32` 时高位恒为 0。

---

### 4.2 IRQ 事件的写入时机：`NextWin_s` 里按 `Size!=0` 入队

#### 4.2.1 概念说明

IRQ FIFO 的写节拍挂在**响应路径**上（不是命令路径）。回忆 [u3-l3](u3-l3-sm-window-switch-ringbuf.md)：状态机处理一次 DMA 响应的链路是 `ProcResp0_s → NextWin_s → WriteCtx_s`。`NextWin_s` 这个状态既要决定「指针怎么推进、要不要换窗口」，也要决定「要不要往 IRQ FIFO 写一条记录」。

入队规则很简洁：**只要这次响应的 `Dma_Resp.Size /= 0`（真正搬了数据），就写一条记录**。记录里的 `WinDone` 位（即 `r.HndlWinDone`）是否为 1，取决于这一拍是否「填满窗口且非环形」或「遇到触发」——也就是同一状态里换窗判定的结果。

> 注意区分：`IrqFifoWrite` 是「**是否入队**」（任何非零传输都入队），`HndlWinDone` 是「**入队的这条记录是否值得发中断**」（只有换窗/触发才置 1）。两者正交。

#### 4.2.2 核心流程

`NextWin_s` 内与 IRQ 相关的逻辑顺序：

1. 先把当前窗口号 `HndlWincur` 存到 `HndlLastWinNr`（这是要写进记录的窗口号）；
2. 若 `Dma_Resp.Size /= 0`，置 `IrqFifoWrite := '1'`（入队）；
3. 判断换窗：`(写满窗口且非环形) or 触发` → 置 `HndlWinDone := '1'`、`NewBuffer := '1'`，推进窗口号、回绕指针；
4. 环形缓冲单独的回绕处理（`HndlPtr2 -= WinSize`），**且不置 `HndlWinDone`**——所以环形回绕不发中断（testbench 的「No IRQ on Ringbuf Wrap」用例就在测这个）。

为什么 `HndlWinDone` 取自 `r.`（寄存器）写入 `IrqFifoIn`？因为 `IrqFifoIn` 是用 `r.HndlStream`/`r.HndlLastWinNr`/`r.HndlWinDone` 拼的，写节拍 `r.IrqFifoWrite` 比 `v.IrqFifoWrite` 晚一拍，正好让 `HndlWinDone` 等字段先稳定再入队。

#### 4.2.3 源码精读

入队判定（`Size /= 0` 才入队）：

[hdl/psi_ms_daq_daq_sm.vhd:472-474](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L472-L474) — 注释明确写道「不为零大小的传输等待 TfDone（因为它们根本不会传到内存接口）」。零大小传输不产生 AXI 写、也不产生 `TfDone`，所以必须跳过入队，否则 `TfDoneCnt` 和 FIFO 条数会对不上。

窗口号捕获：

[hdl/psi_ms_daq_daq_sm.vhd:476](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L476) — `v.HndlLastWinNr := r.HndlWincur`，在换窗判定**之前**捕获当前窗口号，所以记录里写的是「这次传输所属的窗口」，而不是换窗之后的新窗口号。

换窗判定（决定 `HndlWinDone`，即记录的「值得中断」位）：

[hdl/psi_ms_daq_daq_sm.vhd:477-489](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L477-L489) — 条件 `((Ptr1 = WinEnd) and (Ringbuf='0')) or (Trigger='1')` 成立时置 `HndlWinDone:=1` 并切窗口；环形缓冲另在 [:491-493](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L491-L493) 单独回绕指针但**不**置 `HndlWinDone`。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：搞清「入队」和「值得中断」两个条件的区别，并理解为什么零大小传输必须跳过。

**操作步骤**：

1. 读 [:472-474](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L472-L474)，确认「`Size/=0` → 入队」。
2. 读 [:477-493](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L477-L493)，列出三种情形：
   - 线性缓冲写满窗口 → `WinDone=1`（发中断）；
   - 触发结束 → `WinDone=1`（发中断）；
   - 环形缓冲写满回绕 → `WinDone=0`（不发中断，继续覆盖写）。

**预期结果**：你能解释「同一个窗口被多次部分写入、中间没有触发也没有写满」时，IRQ FIFO 里会堆多条 `WinDone=0` 的记录——它们都各占一个 `TfDone` 名额，但都不发 `StrIrq`。

#### 4.2.5 小练习与答案

**练习 1**：如果一个窗口被分 3 次 DMA 传完（第 3 次写满窗口、非环形、无触发），IRQ FIFO 会写几条记录、其中 `WinDone=1` 的有几条？
**答案**：3 条记录（3 次都是非零传输），其中只有第 3 条 `WinDone=1`。前两条 `WinDone=0`，弹出时不发 `StrIrq` 但各消耗一个 `TfDone`。

**练习 2**：环形缓冲模式下，窗口被反复写满回绕，会发中断吗？为什么？
**答案**：不会。环形回绕走 [:491-493](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L491-L493)，该分支不置 `HndlWinDone`，所以记录的 `WinDone=0`，弹出时不发 `StrIrq`。环形缓冲的语义是「持续覆盖、不需要逐窗口通知 CPU」。

---

### 4.3 传输完成计数与 IRQ FIFO 读出：`TfDoneCnt` / `TfDoneReg` / `IrqFifoRead`

#### 4.3.1 概念说明

读节拍由 AXI 主接口驱动：每完成一次 DDR 写突发，顶层 `i_memif` 回送一个 `TfDone` 脉冲（见 [u2-l7](u2-l7-axi-master-interface.md) 里 `Done <= DoneI or ErrorI`）。状态机不能直接用 `TfDone` 去读 FIFO——脉冲可能和其它逻辑竞争同一拍——所以做了两步处理：

1. **打一拍**：`TfDoneReg <= TfDone`，把外部脉冲同步进状态机的两进程法节奏；
2. **计数**：`TfDoneCnt` 每看到 `TfDoneReg=1` 就 `+1`，记录「有几条 IRQ 记录等待被发出」。

注意「`TfDoneCnt` 里的数」与「IRQ FIFO 里的记录条数」**在稳态下相等**，因为每一次非零传输恰好产生一个 `TfDone`（来自 AXI 主接口）和一条 IRQ 记录（来自 `NextWin_s`）。但由于写节拍（状态机响应路径，要走 `CheckResp → ReadCtxStr → ... → NextWin`，多拍）和读节拍（`TfDone` 来自 AXI 主接口，路径不同）时序不同，二者到达的**先后顺序不固定**——这正是 testbench 要测「正序/反序」的原因。

`TfDoneCnt` 的作用就是**解耦这个到达顺序**：哪怕 `TfDone` 先到（FIFO 里还没记录），`TfDoneCnt` 先 `+1` 记着账，等 FIFO 里有记录了再一起读出。

#### 4.3.2 核心流程

读出判定（每拍最多读一条）：

```
if (TfDoneCnt != 0) and (IrqFifoEmpty = '0') then
    IrqFifoRead := 1      # 弹出一条
    TfDoneCnt   -= 1      # 销掉一个名额
```

即：**只有「确实有传输完成待发出」(`TfDoneCnt/=0`) 且「FIFO 里确实有记录可弹」(`Empty=0`) 同时成立**，才弹一条。两个条件缺一不可，所以无论 `TfDone` 和记录谁先到，都能正确等到对方。

时序示意（正序：先写记录，后 `TfDone`）：

```
拍:      ...   W(Rd入队)    ...    TfDone   读出+发脉冲  ...
TfDoneCnt: 0      0                 1→0        0
FIFO条数:  0  →   1        ...       1    →    0
Empty:     1      0        ...       0         1
动作:            写入                          弹出
```

时序示意（反序：先 `TfDone`，后写记录）：

```
拍:      ...   TfDone    ...   W(入队)   读出+发脉冲  ...
TfDoneCnt: 0      1        1      1          0        # TfDone 先到，记账等记录
FIFO条数:  0      0        0  →   1          0
Empty:     1      1        1      0          1        # 等到 Empty=0 才读
动作:                   （等待）  写入        弹出
```

`TfDoneCnt` 的位宽是 `StreamBits_c`，能容纳最多 `2**StreamBits_c - 1` 个待发出的完成（与 OpenCommand「每流最多一条在途命令」配合，足够）。

#### 4.3.3 源码精读

`TfDone` 输入端口：

[hdl/psi_ms_daq_daq_sm.vhd:63](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L63) — 来自内存控制器（顶层即 AXI 主接口的 `Done`）。

`TfDoneCnt`/`TfDoneReg` 在两进程记录中的声明：

[hdl/psi_ms_daq_daq_sm.vhd:184-185](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L184-L185) — `TfDoneCnt` 是 `StreamBits_c` 位，`TfDoneReg` 是 1 位。

`TfDone` 打一拍（纯流水寄存器）：

[hdl/psi_ms_daq_daq_sm.vhd:244](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L244) — `v.TfDoneReg := TfDone`。

完成计数自增：

[hdl/psi_ms_daq_daq_sm.vhd:571-573](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L571-L573) — 每看到 `r.TfDoneReg=1` 就 `TfDoneCnt + 1`。

读出判定（双条件）：

[hdl/psi_ms_daq_daq_sm.vhd:576-579](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L576-L579) — `(TfDoneCnt /= 0) and (Empty = '0')` 同时成立才弹出并 `TfDoneCnt - 1`。

复位值：

[hdl/psi_ms_daq_daq_sm.vhd:626-627](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L626-L627) — 复位时 `TfDoneCnt`、`TfDoneReg` 清零。

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：验证「`TfDoneCnt` 与 FIFO 条数在稳态相等」这一不变量，并理解正/反序都能被处理。

**操作步骤**：

1. 读 testbench [tb/.../psi_ms_daq_daq_sm_tb_case_irq.vhd:245-265](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_daq_sm/psi_ms_daq_daq_sm_tb_case_irq.vhd#L245-L265)，对比 Case 0「Normal Order」（先 `ApplyDmaRespAuto` 后 `AssertTfDone`）和 Case 1「Flipped Order」（先 `AssertTfDone` 后 `ApplyDmaRespAuto`）。
2. 两个用例最后都调用 `CheckIrq(Stream=>0, LastWin=>0, ...)`，期望**都**恰好产生一次 stream 0、窗口 0 的中断。

**需要观察的现象**：两种到达顺序下，`CheckIrq` 都能等到 `StrIrq(0)` 的脉冲——证明 `TfDoneCnt` 起到了「等齐两个节拍」的作用。

**预期结果**：你能用一句话解释——「`TfDoneCnt` 先到就记账，FIFO 记录先到就排队，谁先到都等到对方齐了再弹出」。**待本地验证**：若你有 Modelsim/PsiSim 环境，按 [u5-l1](u5-l1-tb-structure-psisim.md) 跑 `daq_sm` 的 `irq` 用例，观察两种顺序的波形。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `TfDoneCnt` 的位宽是 `StreamBits_c` 而不是 1 位？
**答案**：因为多个流的传输完成可能堆积。每流最多一条在途命令（`OpenCommand` 机制），所以最多 `Streams_g` 个 `TfDone` 可能同时待发出；`StreamBits_c = max(log2ceil(Streams_g),1)` 位足以容纳 `Streams_g`（最小 2 的幂）。

**练习 2**：如果把 [:576](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L576) 的条件改成只看 `Empty=0`（去掉 `TfDoneCnt/=0`），会出什么问题？
**答案**：FIFO 里有记录但对应的 AXI 传输还没真正完成（`TfDone` 未到）时就会被提前弹出并发中断，CPU 收到中断时数据可能还没真正落到 DDR，读到的是旧数据。`TfDoneCnt` 保证了「发中断当拍，传输确实已完成」。

---

### 4.4 StrIrq / StrLastWin 输出脉冲：3957ce3 如何修掉「窗口切换丢中断」

#### 4.4.1 概念说明

弹出一条记录后，若它的 `WinDone`（即 `IrqFifoGenIrq`）为 1，就向 `IrqFifoStream` 指示的那条流发一个时钟周期的 `StrIrq` 脉冲，同时把 `IrqLastWinNr` 输出到对应流的 `StrLastWin`。这个脉冲经 `reg_axi` 聚合（[u3-l5](u3-l5-register-interface.md)）后变成 `IRQVEC` 的一位，等待 CPU 应答。

这部分逻辑看似简单，却是 2024 年 issue #6「切换窗口时中断不可靠」的修复点。修复的 diff 只有 5–6 行，但要把「为什么改之前会丢中断」讲清楚，需要理解一个关键的时序对齐问题。

#### 4.4.2 核心流程

**修复前（有 bug）**——`StrIrq` 生成嵌套在读判定 `if` **内部**：

```
if (TfDoneCnt /= 0) and (Empty = '0') then
    IrqFifoRead := 1          # 组合地决定读
    TfDoneCnt   -= 1
    if IrqFifoGenIrq = '1' then     # 同一拍、用组合输出的 dat_o 判断
        StrIrq(stream) := 1
        StrLastWin(stream) := winNr
    end if
end if
```

**修复后（正确）**——`StrIrq` 生成从 `if` **内部移到外部**，并改用**寄存器版** `r.IrqFifoRead` 作门控：

```
if (TfDoneCnt /= 0) and (Empty = '0') then
    IrqFifoRead := 1
    TfDoneCnt   -= 1
end if

if IrqFifoGenIrq = '1' and r.IrqFifoRead = '1' then   # 用寄存器版读，延迟一拍
    StrIrq(stream) := 1
    StrLastWin(stream) := winNr
end if
```

**改动本质**：把「采样 FIFO 数据 `IrqFifoGenIrq`」的时机，从「**决定要读的那一拍**（`v.IrqFifoRead`）」推迟到「**真正消费读出的那一拍**（`r.IrqFifoRead`）」——也就是延迟一个寄存器节拍。

**为什么旧代码会丢中断（窗口切换场景）**：

在窗口切换前后，`TfDone` 脉冲和 IRQ FIFO 的写入（来自 `NextWin_s`）时序紧贴、且先后顺序不定。旧代码在「决定读」的同一拍就去采样 `dat_o`（`IrqFifoGenIrq`），而这一拍 `dat_o` 可能正处于**写入/弹出交接的瞬态**——也就是说，「读判定成立的拍」和「`dat_o` 上是该记录有效值的拍」之间，在窗口切换这种紧密时序下并不总是吻合。一旦在 `dat_o` 还没稳定成 `WinDone=1` 的那一拍做了判定，就会读到瞬态的 0：

- `TfDoneCnt` 已经 `-1`（名额销掉了）；
- FIFO 已经弹出（记录没了）；
- 但 `IrqFifoGenIrq` 被采样成 0 → **`StrIrq` 没发出去**。

记录既被消费、名额又被销掉，这条中断就**永久丢失**，无法恢复。这正对应 issue 标题「切换窗口时中断不可靠」。

新代码用 `r.IrqFifoRead`（寄存器版）做门控，保证「采样 `dat_o` 的那一拍」一定是「读真正生效、`dat_o` 已稳定成被弹出的那条记录」的那一拍，消除了这个 1 拍的竞态。

#### 4.4.3 源码精读

**修复后**的 `StrIrq`/`StrLastWin` 生成（本讲最关键的一段）：

[hdl/psi_ms_daq_daq_sm.vhd:582-585](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L582-L585) — 门控条件是 `IrqFifoGenIrq = '1' and r.IrqFifoRead = '1'`。注意是 `r.IrqFifoRead`（寄存器，上一拍 `v.IrqFifoRead` 的结果），不是 `v.IrqFifoRead`（组合）。`StrLastWin` 用 `resize(unsigned(IrqLastWinNr), 5)` 把窗口号零扩展到 5 位。

`StrIrq`/`StrLastWin` 的组合默认值与寄存器输出：

[hdl/psi_ms_daq_daq_sm.vhd:232-234](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L232-L234) — 每拍先把 `v.StrIrq` 清成全 0（脉冲默认不拉高，仅在 [:583](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L583) 命中时拉高一拍）。

[hdl/psi_ms_daq_daq_sm.vhd:599-600](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L599-L600) — 寄存器输出到端口 `StrIrq`/`StrLastWin`。

`StrIrq`/`StrLastWin` 在记录中的声明与复位：

[hdl/psi_ms_daq_daq_sm.vhd:194-197](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L194-L197)、[:630-631](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L630-L631)。

> ⚠️ 关于「`dat_o` 在哪一拍稳定」的精确周期级描述，依赖于 `psi_common_sync_fifo` 的内部读延迟模型（FWFT 与否），该源码不在本仓库（属外部依赖 psi_common）。本讲的解释以 **diff 本身的语义**（`v.IrqFifoRead`→`r.IrqFifoRead` 延迟一拍）和**症状**（窗口切换丢中断、testbench 增设「Flipped Order」用例）为依据。**待本地验证**：若你能读到 psi_common 的 `psi_common_sync_fifo.vhd`，可据此把周期级时序画到拍级。

#### 4.4.4 代码实践（必做：读 diff）

**实践目标**：亲眼看到 3957ce3 的改动，并用自己的话解释它为何能修掉「窗口切换丢中断」。

**操作步骤**：

1. 在仓库根目录运行：

   ```bash
   git show 3957ce3
   ```

2. 你会看到 `hdl/psi_ms_daq_daq_sm.vhd` 的 diff：删除了嵌在 `if (unsigned(r.TfDoneCnt) /= 0) and (IrqFifoEmpty = '0') then ... ` 内部的 `if IrqFifoGenIrq = '1' then ...` 块，并在该 `if` 块**之外**新增 `if IrqFifoGenIrq = '1' and r.IrqFifoRead = '1' then ...`。

3. 对照本讲 4.4.2 的伪代码，确认两处差异：
   - 门控从组合 `v.IrqFifoRead`（隐含，嵌在判定内）改为寄存器 `r.IrqFifoRead`（显式，移到判定外）；
   - `StrIrq` 生成不再被 `TfDoneCnt/=0 and Empty=0` 这个「读判定」直接包住，而是只依赖「上一拍确实读了」。

4. （可选）看 testbench [tb/.../psi_ms_daq_daq_sm_tb_case_irq.vhd:107-114, 256-265](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_daq_sm/psi_ms_daq_daq_sm_tb_case_irq.vhd#L107-L114)，确认增设的「Flipped Order」（Case 1：`AssertTfDone` 在前、`ApplyDmaRespAuto` 在后）正是这个修复的回归用例。

**需要观察的现象**：diff 只动了 6 行（增 6 删 5），却是 issue #6 的完整修复。

**预期结果**：你能写出一段解释——「旧代码在决定读的同一拍采样 `dat_o`，窗口切换时这一拍的 `dat_o` 可能尚不稳定，导致读到 `WinDone=0`；而 `TfDoneCnt` 与 FIFO 弹出照常发生，中断就此丢失。新代码用 `r.IrqFifoRead` 把采样推迟到读真正生效的那一拍，保证 `dat_o` 已稳定。」

#### 4.4.5 小练习与答案

**练习 1**：修复后，`StrIrq` 比 `IrqFifoRead` 晚几拍出现？
**答案**：晚 1 拍。`IrqFifoRead` 在拍 N 由 `v.IrqFifoRead` 决定（拍 N+1 成为 `r.IrqFifoRead=1`）；`StrIrq` 的门控用的是 `r.IrqFifoRead`，所以在拍 N+1 拉高、拍 N+2 出现在端口（经寄存器输出 [:599](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L599)）。整体上 `StrIrq` 端口脉冲比「读判定成立的拍」晚 2 拍、比 `IrqFifoRead` 寄存器晚 1 拍。

**练习 2**：如果只把 `r.IrqFifoRead` 改回嵌套在 `if` 内（即恢复结构但保留 `r.IrqFifoRead`），能修复吗？
**答案**：不能。嵌套在 `(TfDoneCnt/=0 and Empty=0)` 内意味着 `StrIrq` 生成还要求「这一拍 `TfDoneCnt/=0 且 Empty=0`」，但延迟一拍后这俩条件未必还成立（`TfDoneCnt` 已减、FIFO 可能已空），门控会失效。把 `StrIrq` 生成**移出**读判定 `if`、只挂 `r.IrqFifoRead`，才是修复的关键——两者缺一不可。

---

### 4.5 IrqFifoAlmFull 源头反压：在 `Idle_s` 拦住新命令

#### 4.5.1 概念说明

IRQ FIFO 一旦溢出，记录丢失、中断永久缺失，这是不可接受的。本设计不用「满了再丢」或「满了再停响应」，而是**在更上游的命令签发处**就拦住：当 IRQ FIFO 几乎满（`IrqFifoAlmFull=1`）时，状态机在 `Idle_s` 拒绝进入 `CheckPrio1_s`（即不再签发新的 DMA 命令）。

这条反压链是：

```
IrqFifoAlmFull=1  →  Idle_s 不进 CheckPrio1_s  →  不发新 DMA 命令
                                              →  没有新 DMA 响应
                                              →  NextWin_s 不再写 IRQ FIFO
                                              →  IRQ FIFO 不会溢出
```

「almost-full 阈值 `Streams_g*3`」与「FIFO 深度 `2**StreamBits_c*4`（≥ Streams_g*4）」之间留了至少 `Streams_g` 的余量，保证从「拉高 `IrqFifoAlmFull`」到「新命令真正停发」之间的若干拍流水里，已经在途的命令产生的记录仍有地方可放——即**保证响应一定有处可放**。

#### 4.5.2 核心流程

- testbench 的注释印证了这个阈值：`tb_case_irq` 第 117 行写「FIFO is full after Streams (4) x 3 = 12 open transfers」，并据此构造「IRQ FIFO full」用例（Case 2：先连发 12 个响应填满，再验证第 13 个命令被反压延迟、`CheckNoActivity(Dma_Cmd_Vld, 1us, 0, "Full")` 期间不签发新命令，等 FIFO 排空后才放行）。

- 反压与「每流最多一条在途命令」(`OpenCommand`) 叠加：任意时刻在途命令 ≤ `Streams_g` 条，每条至多产生 1 条 IRQ 记录，所以稳态下 FIFO 待发出记录 ≤ `Streams_g`；`Streams_g*3` 的阈值给出 3 倍裕量，吸收写节拍（`NextWin_s`）与读节拍（`TfDone`）之间的时序差。

#### 4.5.3 源码精读

`Idle_s` 的源头反压（**本讲 4.1 提到的 `Streams_g*3` 在这里生效**）：

[hdl/psi_ms_daq_daq_sm.vhd:267-270](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L267-L270) — 注释写明「only if IRQ FIFO has space for the response for sure」。只有 `IrqFifoAlmFull='0'` 才进入 `CheckPrio1_s` 开始仲裁新命令；否则停在 `Idle_s`。

`IrqFifoAlmFull` 来自 FIFO 例化的 `alm_full_o`：

[hdl/psi_ms_daq_daq_sm.vhd:692-693](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L692-L693) — `alm_full_on_g => true`、`alm_full_level_g => Streams_g * 3`。

testbench 对反压的验证：

[tb/.../psi_ms_daq_daq_sm_tb_case_irq.vhd:116-130](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_daq_sm/psi_ms_daq_daq_sm_tb_case_irq.vhd#L116-L130) — Case 2「IRQ FIFO full」：先连发 12 次填到 `Streams_g*3`，第 13 次期望命令被反压（`CheckNoActivity`），随后 FIFO 排空、放行。

#### 4.5.4 代码实践（源码阅读型）

**实践目标**：把「源头反压」这条链从 FIFO 一直追到 `Idle_s`，理解为什么必须在命令侧（而不是响应侧）反压。

**操作步骤**：

1. 从 [:693](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L693) 的 `alm_full_level_g => Streams_g * 3` 出发，找到 `IrqFifoAlmFull` 的消费点 [:267](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L267)。
2. 回答：如果改成「在 `NextWin_s` 里检测 `IrqFifoAlmFull` 再决定要不要写」会怎样？
3. 对照 testbench Case 2，确认反压期间 `Dma_Cmd_Vld` 不会有新脉冲。

**预期结果**：你能解释——「响应侧反压来不及，因为命令一旦签发，数据流经 DMA、AXI 主接口后必然产生响应；唯一能阻止新记录产生的，是不签发新命令。所以在 `Idle_s` 反压是最上游、最可靠的。」

#### 4.5.5 小练习与答案

**练习 1**：`IrqFifoAlmFull` 拉高后，已经在途的 DMA 命令（`OpenCommand=1`）会被取消吗？
**答案**：不会。`IrqFifoAlmFull` 只在 `Idle_s` 拦**新**命令，已经在途的命令会照常完成、产生响应、写一条 IRQ 记录。这正是阈值取 `Streams_g*3`（而非 `Streams_g`）的原因：要为「反压生效前已在途的命令」预留空间。

**练习 2**：为什么几乎满阈值是 `Streams_g*3`，而 FIFO 深度是 `2**StreamBits_c*4`？
**答案**：深度 `2**StreamBits_c*4 ≥ Streams_g*4` 给出 4 倍容量的「桶」，almost-full 在 `Streams_g*3` 处报警，留出至少 `Streams_g` 的余量。3 倍 vs 4 倍之间的 1 倍余量，用于吸收「反压生效的若干拍流水里仍在途命令产生的记录」。具体倍数是设计裕量选择，**待确认**是否有更精确的时序推导。

---

## 5. 综合实践

把本讲四个最小模块串起来，做一次**端到端的中断链路追踪**。给定场景：`Streams_g=4, Windows_g=4`，stream 0 配置为「线性缓冲（`Ringbuf=0`）、允许覆盖（`Overwrite=1`）、2 个窗口（`Wincnt=2`）、窗口大小 4096 字节」，连续写入数据直到把窗口 0 写满。

**任务**：

1. **入队追踪（4.2）**：stream 0 写满窗口 0 这次传输，`NextWin_s` 里 `IrqFifoWrite` 是否拉高？记录里的 `Stream`、`LastWinNr`、`WinDone` 三个字段分别是什么值？写出 `IrqFifoIn` 的 5 位二进制。

2. **完成与读出追踪（4.3）**：这次传输对应的 `TfDone` 到达后，`TfDoneCnt` 如何变化？`IrqFifoRead` 在哪一拍拉高（需要 `Empty=0` 与 `TfDoneCnt/=0` 同时成立）？

3. **脉冲生成追踪（4.4）**：根据**修复后**的 [:582-585](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L582-L585)，`StrIrq(0)` 在哪一拍拉高？`StrLastWin(0)` 的值是多少？如果用**修复前**的代码、且恰好这次 `TfDone` 与窗口写入紧贴，可能发生什么？

4. **反压追踪（4.5）**：如果 CPU 长时间不处理中断（`TfDone` 持续不来），IRQ FIFO 被写到 `Streams_g*3=12` 条，`Idle_s` 会怎样？testbench Case 2 是如何验证这一点的？

**参考答案要点**：
1. `IrqFifoWrite=1`（`Size/=0`）；`Stream=0(00)`、`LastWinNr=0(00)`、`WinDone=1(1)`；`IrqFifoIn = 0b10000`。
2. `TfDone` 到 → `TfDoneCnt=1`；当 `Empty=0`（记录已入队）且 `TfDoneCnt=1` 时，`IrqFifoRead` 拉高一拍、`TfDoneCnt` 减回 0。
3. 修复后：`IrqFifoRead` 寄存器（`r.IrqFifoRead`）拉高的下一拍，因 `IrqFifoGenIrq=1`，`StrIrq(0)` 拉高一拍，`StrLastWin(0)=0`。修复前若采样瞬态读到 `WinDone=0`：记录被弹出、`TfDoneCnt` 已减，但 `StrIrq` 不发——中断永久丢失。
4. `IrqFifoAlmFull=1` → `Idle_s` 停在原地、不进 `CheckPrio1_s`，不再签发新命令，直到 FIFO 排空。testbench Case 2 用 `CheckNoActivity(Dma_Cmd_Vld, 1us, 0, "Full")` 验证反压期间无新命令。

## 6. 本讲小结

- IRQ 信息 FIFO（`i_irq_fifo`）缓存「流号 + 窗口号 + WinDone」三字段记录，只有 `WinDone=1` 的记录弹出时才发 `StrIrq`；深度 `2**StreamBits_c*4`、almost-full 阈值 `Streams_g*3`。
- 写节拍挂在响应路径的 `NextWin_s`：**任何 `Size/=0` 的传输都入队**，但只有「线性缓冲写满窗口」或「触发」才把记录的 `WinDone` 置 1（环形回绕不发中断）。
- 读节拍由 AXI 主接口的 `TfDone` 驱动：`TfDoneReg` 打一拍、`TfDoneCnt` 计数「待发出的完成」，与 `IrqFifoEmpty` 双条件成立才弹出一条；`TfDoneCnt` 解耦了 `TfDone` 与记录到达的先后顺序（testbench 的「正序/反序」用例）。
- `StrIrq`/`StrLastWin` 的生成在 commit 3957ce3 中从「读判定 `if` 内部、用组合 `v.IrqFifoRead`」改为「移到外部、用寄存器 `r.IrqFifoRead`」，消除了窗口切换时采样 `dat_o` 瞬态导致的「记录被消费但中断丢失」竞态。
- 溢出保护是**源头反压**：`IrqFifoAlmFull` 在 `Idle_s` 拦住新命令的签发，保证「响应一定有处可放」；阈值 `Streams_g*3` 给在途命令留出余量。

## 7. 下一步学习建议

- **下一篇 [u4-l2](u4-l2-driver-irq-window-callback.md)**：本讲产生的 `StrIrq` 经 `reg_axi` 聚合成 `IRQVEC` 后，CPU 侧的 C 驱动 `PsiMsDaq_HandleIrq` 如何读 `IRQVEC`、如何区分「流方案」与「窗口方案」、如何用 `irqCalledWin` 位图保证每窗口恰好回调一次——那里是本讲硬件脉冲的「消费者」。
- **回看 [u3-l5](u3-l5-register-interface.md)**：`IrqOut = IRQVEC & IRQENA & Gcfg_IrqEna` 的三级聚合，以及 CPU 写 `IRQVEC`（W1C）应答清位的语义。
- **若想验证时序细节**：按 [u5-l1](u5-l1-tb-structure-psisim.md) 跑 `daq_sm` 的 `irq` 用例（含 Normal/Flipped Order、IRQ FIFO full、Win-Change without trigger、No IRQ on Ringbuf Wrap），在波形上观察 `IrqFifoWrite`/`TfDoneCnt`/`IrqFifoRead`/`StrIrq` 的拍级关系。
- **进阶**：阅读外部依赖 psi_common 里的 `psi_common_sync_fifo.vhd`，确认 `dat_o` 在读命令后的有效拍，把本讲 4.4 的「待本地验证」补成精确的拍级时序图。
