# 输入逻辑：超时、时间戳与跨时钟域状态

> 本讲承接 [u2-l3（记录模式、触发与后触发计数）](u2-l3-input-modes-trigger.md)。在上一讲里我们看清了「一个帧何时开始、何时因为触发而结束」。但还有一个绕不开的问题：**如果一帧的数据迟迟不凑满一个内部字、又始终等不到触发，挂在移位寄存器里的残余样本该怎么办？** 本讲就回答这个问题，并顺带把「时间戳是怎么打上标记的」「下游 DMA 怎么知道一帧已经凑齐了」一并讲透。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 `TimeoutCnt`/`Timeout` 是如何把「卡在位宽转换里的残余样本」冲刷成一个完整的内部字（`DataFifoIsTo` 帧）的。
- 区分 `ToDisable` 与 `FrameTo` 两个 MODE 寄存器控制位（bit24 / bit25）的作用，并指出它们在当前 RTL 中对超时计数器的实际影响。
- 解释时间戳在哪个时刻被锁存、时间戳 FIFO 溢出时用什么占位值替代、以及 `g_timestamp` 生成块做了什么。
- 读懂输出侧 `p_outlast` 进程：它如何用 `TLastCnt`/`InTlastCnt`/`OutTlastCnt` 三个计数器统计「缓冲里还有几个完整帧」，并算出反馈给状态机的 `Daq_HasLast` 与 `Daq_Level`。

## 2. 前置知识

本讲假设你已经读过 [u2-l2（接口、时钟域与缓冲）](u2-l2-input-interface-clocks.md) 和 [u2-l3（记录模式、触发与后触发计数）](u2-l3-input-modes-trigger.md)。下面几个概念会反复用到，先做个 30 秒回顾：

- **两进程法（two-process）**：`p_comb` 在 `Str_Clk` 域算出下一拍状态 `r_next`，`p_seq` 把它打入寄存器 `r`。本讲分析的所有时序，都是「在 `p_comb` 里看 `r → v(r_next)`」。
- **位宽转换**：流样本宽度 `StreamWidth_g`（8/16/32/64）要拼成内部宽度 `IntDataWidth_g`（默认 64）。常量 `WconvFactor_c = IntDataWidth_g / StreamWidth_g` 表示「几个样本拼一个内部字」。例如 16 位流、64 位内部时 `WconvFactor_c = 4`，即 4 个样本拼一个 64 位字。
- **数据 FIFO 的打包宽度**：`DataFifoWidth_c = IntDataWidth_g + BytesWidth_c + 2`（[hdl/psi_ms_daq_input.vhd:90](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L90)）。也就是说进 FIFO 的不光是数据，还有「有效字节数」`Bytes` 和两个标志位 `IsTo`/`IsTrig`，它们平铺成一个 `std_logic_vector`。
- **`Last = IsTo or IsTrig`**：输出侧解包后，只要这个字是被超时冲出来的（`IsTo`）或是触发帧末字（`IsTrig`），它就是一个「帧末字」（AXI 意义上的 TLAST）。本讲讲的就是 `IsTo` 这一半来源，以及它如何被统计。

一个直觉性的比喻：位宽转换就像「把零钱（窄样本）攒成整钞（宽字）」。整钞凑够了自然进库（FIFO）；可如果顾客半路走了、零钱没攒够，又没人喊「结账」（触发），那这把零钱就一直攥在手里。**超时机制**就是一个「等太久了就强制结账」的服务员。

## 3. 本讲源码地图

本讲几乎全部围绕单文件展开，再借用寄存器接口文件确认两个控制位的来源。

| 文件 | 作用 |
| --- | --- |
| [hdl/psi_ms_daq_input.vhd](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd) | 输入逻辑全部实现：超时、时间戳、TLAST 统计都在这里 |
| [hdl/psi_ms_daq_reg_axi.vhd](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd) | 确认 `ToDisable`/`FrameTo` 来自 MODE 寄存器的 bit24 / bit25 |
| [tb/psi_ms_daq_input/psi_ms_daq_input_tb_case_timeout.vhd](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_input/psi_ms_daq_input_tb_case_timeout.vhd) | 超时用例：施加奇/偶个样本后等待超时冲刷 |
| [tb/psi_ms_daq_input/psi_ms_daq_input_tb_case_ts_overflow.vhd](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_input/psi_ms_daq_input_tb_case_ts_overflow.vhd) | 时间戳 FIFO 溢出用例：验证 `0xFF..FF` 占位与恢复 |

涉及的关键代码点（行号供快速定位，下文都会给永久链接）：

- 超时常量 `TimeoutLimit_c`：第 86 行
- 记录类型里的超时/时间戳字段：第 103–112 行
- `FrameInProgr` 帧进行中标志：第 244–251 行
- 超时检测：第 253–263 行
- 超时冲刷（产生 `DataFifoIsTo`）：第 270–275 行
- 字节计数 `AddSamples_v`：第 290–303 行
- 时间戳溢出检测 `TsOverflow`：第 201–210 行
- 时间戳锁存 `TsLatch`：第 212–221 行
- 时间戳 FIFO 生成块 `g_timestamp`：第 545–574 行
- TLAST 计数 `TLastCnt`：第 265–268 行
- 输出侧 TLAST 统计 `p_outlast`：第 364–401 行
- `TLastCnt` 跨时钟域同步 `i_cc`：第 477–489 行

## 4. 核心概念与源码讲解

### 4.1 超时检测与 Timeout 帧（DataFifoIsTo）

#### 4.1.1 概念说明

位宽转换要求攒满 `WconvFactor_c` 个样本才写一个字进数据 FIFO。问题来了：**如果一帧只来了不到 `WconvFactor_c` 个样本就不再来数据，也始终没有触发来「结账」，那这把残余样本会永远卡在移位寄存器 `DataSftReg` 里**，既不进 FIFO，也不会被下游 DMA 读走，对应的窗口也就永远凑不出一个完整帧。

超时机制就是来解决这个「挂单」的：当一帧正在进行中、却又长时间没有新样本到来时，启动一个计数器；计数器数到上限就强制把当前残余内容当做一个字写进 FIFO，并打上 `IsTo = 1`（To = Timeout）标志，告诉下游「这一帧是被超时结束的，请注意它的有效字节数可能不足一整字」。

> ⚠️ 一个常被忽略的细节：超时不只用于「冲刷残余样本」。即便一帧的数据恰好字对齐（凑满了若干整字），只要它**没有以触发结尾**，超时仍然会在随后发一个**空字**（`Bytes = 0`）作为帧结束标记。这一点会在 4.1.4 的实践里用测试平台代码佐证。

#### 4.1.2 核心流程

超时机制由「一个上限常量 + 一个计数器 + 一个标志 + 一个冲刷动作」四件套组成：

1. **编译期换算上限**：把用户给的人话参数 `StreamTimeout_g`（秒）和 `StreamClkFreq_g`（Hz）乘起来，减 1 得到需要数多少个时钟周期：

   \[
   \text{TimeoutLimit\_c} = \lfloor \text{StreamClkFreq\_g} \times \text{StreamTimeout\_g} \rfloor - 1
   \]

   例如默认 `125e6 × 1e-3 − 1 = 124999`，即大约 1 ms 后触发超时。

2. **计数条件**：每一拍判断「要不要把 `TimeoutCnt` 清零」。只有当下面四件事**同时**成立时才会计数（否则清零）：
   - 当前没有新样本：`Str_Vld = 0`
   - 超时未被禁用：`ToDisable = 0`
   - 未选择「帧超时」模式：`FrameTo = 0`
   - 有一帧正在进行：`FrameInProgr = 1`

3. **到顶置标志**：`TimeoutCnt` 数到 `TimeoutLimit_c` 时，清零并拉高 `Timeout` 标志一拍。

4. **冲刷**：`Timeout = 1` 的下一拍，把残余内容写出：`DataFifoVld = 1`、`DataFifoIsTo = 1`，并把 `Timeout` 拉低。

可以用下面的状态化伪代码描述（对应 `p_comb` 里第 253–275 行）：

```
每一拍:
  if (Str_Vld=1) or (ToDisable=1) or (FrameTo=1) or (FrameInProgr=0):
      TimeoutCnt <= 0                     # 不具备计数条件，清零
  else:
      if TimeoutCnt == TimeoutLimit_c:
          TimeoutCnt <= 0
          Timeout     <= 1                # 到顶，置标志
      else:
          TimeoutCnt <= TimeoutCnt + 1    # 继续数

  if Timeout == 1:                        # 上一拍到顶，本拍冲刷
      DataFifoVld  <= RecEna
      DataFifoIsTo <= 1
      Timeout      <= 0
```

#### 4.1.3 源码精读

**上限常量**（[hdl/psi_ms_daq_input.vhd:86](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L86)）：编译期把「秒」换算成「周期数」。

```vhdl
constant TimeoutLimit_c  : integer  := integer(StreamClkFreq_g * StreamTimeout_g) - 1;
```

**记录字段**（[hdl/psi_ms_daq_input.vhd:103-104](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L103-L104)）：超时计数器 `TimeoutCnt` 取值范围被严格约束在 `0 .. TimeoutLimit_c`，`Timeout` 是一拍标志。

```vhdl
TimeoutCnt     : integer range 0 to TimeoutLimit_c;
Timeout        : std_logic;
```

**帧进行中标志**（[hdl/psi_ms_daq_input.vhd:244-251](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L244-L251)）：只要来了一个非触发的有效样本，就认为「一帧正在进行」；来了触发样本则认为「帧结束」。复位后 `FrameInProgr = 0`。这正是超时计数「只在帧内才数」的依据——空闲等待第一个样本时不会误触发超时。

```vhdl
if Str_Vld = '1' then
  if Str_Trig = '1' then
    v.FrameInProgr := '0';
  else
    v.FrameInProgr := '1';
  end if;
end if;
```

**超时检测**（[hdl/psi_ms_daq_input.vhd:253-263](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L253-L263)）：注意那条 `if` 的 OR 链——`Str_Vld=1`、`ToDisable=1`、`FrameTo=1`、`FrameInProgr=0` 四者只要有一个成立就清零。这等价于「只有四者全不成立才计数」。

```vhdl
if Str_Vld = '1' or ToDisable_Sync = '1' or (FrameTo_Sync = '1' or r.FrameInProgr = '0') then
  v.TimeoutCnt := 0;
else
  if r.TimeoutCnt = TimeoutLimit_c then
    v.TimeoutCnt := 0;
    v.Timeout    := '1';
  else
    v.TimeoutCnt := r.TimeoutCnt + 1;
  end if;
end if;
```

**超时冲刷**（[hdl/psi_ms_daq_input.vhd:270-275](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L270-L275)）：`r.Timeout = 1` 时，把残余内容写出去并打上 `DataFifoIsTo`。注释里的 “only if data is stuck in conversion” 说的就是「挂单冲刷」语义。

```vhdl
if r.Timeout = '1' then
  v.DataFifoVld  := r.DataFifoVld or r.RecEna;
  v.DataFifoIsTo := '1';
  v.Timeout      := '0';            -- reset timeout after data was flushed to the FIFO
end if;
```

**字节计数**（[hdl/psi_ms_daq_input.vhd:290-303](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L290-L303)）：这一段决定写入 FIFO 的字带多少「有效字节」。关键是 `AddSamples_v`：正常拍它是 1（要把「正在进来的这个样本」也算上），而**超时拍它是 0**（因为这一拍没有新样本进来，只是把已经攒在移位寄存器里的残余冲出去）。所以超时字的字节正好等于「已攒样本数 × 每样本字节数」。

```vhdl
v.DataFifoBytes := (others => '0');
if r.Timeout = '1' then
  AddSamples_v := 0;
else
  AddSamples_v := 1;
end if;
case StreamWidth_g is
  when 8      => v.DataFifoBytes := r.WordCnt + AddSamples_v;
  when 16     => v.DataFifoBytes := (r.WordCnt + AddSamples_v) & "0";
  when 32     => v.DataFifoBytes := (r.WordCnt + AddSamples_v) & "00";
  when 64     => v.DataFifoBytes := (r.WordCnt + AddSamples_v) & "000";
  when others => null;
end case;
```

> 对 16 位流，`(WordCnt + AddSamples_v) & "0"` 等价于「样本数左移 1 位」，即 `样本数 × 2` 字节。例如攒了 3 个样本被超时冲刷：`WordCnt = 3`、`AddSamples_v = 0` → `Bytes = 3 & "0" = 6`，正好是 3 个 16 位样本的字节数。

**打包进 FIFO**（[hdl/psi_ms_daq_input.vhd:492-495](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L492-L495)）：`IsTo` 与 `IsTrig` 各占 FIFO 宽度的最高两位。

```vhdl
DataFifo_InData(IntDataWidth_g-1 downto 0)  <= r.DataSftReg;
DataFifo_InData(IntDataWidth_g+BytesWidth_c-1 downto IntDataWidth_g) <= std_logic_vector(r.DataFifoBytes);
DataFifo_InData(DataFifo_InData'high - 1) <= r.DataFifoIsTo;
DataFifo_InData(DataFifo_InData'high)     <= r.DataFifoIsTrig;
```

**输出侧解包**（[hdl/psi_ms_daq_input.vhd:536-540](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L536-L540)）：`Last` 就是 `IsTo or IsTrig`，把「超时结束」与「触发结束」统一成一个帧末标志交给下游 DMA。

```vhdl
Daq_Data_I.IsTo   <= DataFifo_OutData(DataFifo_OutData'high-1);
Daq_Data_I.IsTrig <= DataFifo_OutData(DataFifo_OutData'high);
Daq_Data_I.Last   <= Daq_Data_I.IsTo or Daq_Data_I.IsTrig;
```

#### 4.1.4 代码实践

**实践目标**：用一个「不足 4 样本的短帧」场景，验证超时冲刷出来的字确实是「残余样本 + 正确字节计数 + `IsTo = 1`」，并对照测试平台确认你的推断。

**操作步骤（源码阅读型 + 可选仿真）**：

1. 设定场景：`StreamWidth_g = 16`、`IntDataWidth_g = 64`，则 `WconvFactor_c = 4`。记录模式 Continuous，`RecEna = 1`。施加 3 个非触发样本（S0、S1、S2）后让 `Str_Vld = 0` 一直保持，且不发触发。
2. 在 [hdl/psi_ms_daq_input.vhd:253-275](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L253-L275) 上手工走拍：
   - 来 S0/S1/S2 的三拍：`WordCnt` 从 0 → 1 → 2 → 3，`DataSftReg` 低 48 位依次填入 S0/S1/S2；每拍 `Str_Vld = 1` 把 `TimeoutCnt` 清零；`FrameInProgr` 被置 1。
   - 第 4 拍起 `Str_Vld = 0`：`FrameInProgr` 仍为 1（只在 `Str_Vld = 1` 时才变），`TimeoutCnt` 开始计数。
   - 计满 `TimeoutLimit_c` 那一拍：`TimeoutCnt → 0`、`Timeout → 1`。
   - 下一拍（`r.Timeout = 1`）：写 FIFO，`DataFifoIsTo = 1`；`AddSamples_v = 0`、`WordCnt = 3` → `Bytes = 6`；`WordCnt` 被清零。
3. 推断 FIFO 输出端（`ClkMem` 域）会看到一个字：`Data = S0|S1|S2`（低 48 位有效，高 16 位为 0）、`Bytes = 6`、`IsTo = 1`、`IsTrig = 0`、`Last = 1`。
4. （可选）运行超时用例验证：参照 [tb/psi_ms_daq_input/psi_ms_daq_input_tb_case_timeout.vhd:80-141](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_input/psi_ms_daq_input_tb_case_timeout.vhd#L80-L141)，它施加 11 个样本后等待 `StreamTimeout_g*(1 sec)`，再用 `CheckAcqData(..., FrameType_Timeout_c, ...)` 校验。运行方式见 [u1-l2](u1-l2-repo-and-simulation.md) 介绍的 `sim/run.tcl` / `ciFlow.py`。

**需要观察的现象**：
- 超时前 `Daq_HasLast` 必须为 0（测试平台在 [tb/psi_ms_daq_input/psi_ms_daq_input_tb_case_timeout.vhd:125](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_input/psi_ms_daq_input_tb_case_timeout.vhd#L125) 显式断言了这一点）——说明残余数据尚未凑成帧。
- 超时后恰好出现一个带 `IsTo = 1` 的字，字节数与残余样本数吻合。

**预期结果**：3 个 16 位样本 → 1 个 64 位字，`Bytes = 6`、`IsTo = 1`、`Last = 1`；该字不消耗时间戳 FIFO（超时帧无时间戳，见 4.3）。

> 关于「字对齐帧也会收到一个空超时字」的佐证：测试平台辅助过程 `CheckAcqData` 里有专门处理 `DelayedToTlast_v` 的逻辑——「在超时情况下，帧尾可能后跟一个 0 字节的 TLAST 空字」（见 [tb/psi_ms_daq_input/psi_ms_daq_input_tb_pkg.vhd:138](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_input/psi_ms_daq_input_tb_pkg.vhd#L138) 及第 178–189 行）。这就是「凑满整字但没触发」时，超时随后补发 `Bytes = 0` 空字的真实证据。是否在本地复现该现象：**待本地验证**（取决于你施加的样本数是否恰好字对齐）。

#### 4.1.5 小练习与答案

**练习 1**：把 `StreamTimeout_g` 从 `1.0e-3` 改成 `10.0e-6`、`StreamClkFreq_g` 仍为 `125.0e6`，`TimeoutLimit_c` 变成多少？超时大约多久触发？
**答案**：`integer(125.0e6 × 10.0e-6) − 1 = integer(1250) − 1 = 1249`，即约 10 µs（1249 个 8 ns 周期）后触发。这也正是 input testbench 在仿真里用的常量（见 [hdl/psi_ms_daq_input.vhd:33-34](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L33-L34) 的 `$$ constant= ...` 注解）。

**练习 2**：32 位流、`IntDataWidth_g = 64`，一帧来了 5 个样本后停止且无触发。超时冲刷出来的字有几个有效字节？
**答案**：`WconvFactor_c = 2`，5 个样本会先凑满 2 个整字（各 8 字节，正常写出），剩 1 个样本挂在移位寄存器。超时冲刷这 1 个残余样本：`WordCnt = 1`、`AddSamples_v = 0` → `Bytes = (1+0) & "00" = 4`，即 4 字节。

---

### 4.2 ToDisable / FrameTo：关闭超时的两种方式

#### 4.2.1 概念说明

并不是所有应用都想要「等久了就强制结账」。比如某些数据源保证**每一帧都一定以触发结尾**，那么残余样本迟早会被触发冲出去（触发会打 `DataFifoIsTrig` 并强制写出当前字，见 [u2-l3](u2-l3-input-modes-trigger.md)），根本用不着超时来兜底；又或者上游希望完全自己掌控帧边界，不愿意 IP 自作主张地插入超时帧。为此，MODE 寄存器提供了两个控制位：

- **`ToDisable`（MODE bit24）**：超时禁用位。置 1 表示「我不要超时」。
- **`FrameTo`（MODE bit25）**：帧超时位。置 1 表示「改用基于帧的方式处理超时」。

这两个位是在 commit `12c010f`（*Add timeout control bits and logic to ignore timeout or configure framebased timeout in input logic*）里一起加入的——提交信息明确区分了「ignore timeout（忽略超时）」与「framebased timeout（基于帧的超时）」两种意图。

#### 4.2.2 核心流程

两个位都是**准静态配置**（CPU 写一次后基本不动），从寄存器时钟域 `ClkReg` 经 `psi_common_status_cc` 同步到流时钟域 `Str_Clk`（与 `PostTrigSpls`、`Mode` 打包在同一组 36 位里一起过 CDC）。

它们只在一个地方起作用——超时计数器的清零条件（4.1.3 那条 OR 链）：

```
清零 TimeoutCnt  ⟺  Str_Vld=1  OR  ToDisable=1  OR  FrameTo=1  OR  FrameInProgr=0
```

把 `ToDisable` 或 `FrameTo` 任一置 1，都会让这条 OR 永远成立，于是 `TimeoutCnt` 被恒定钉在 0，`Timeout` 永远不会拉高——**时间驱动的超时冲刷被关闭**。二者的实际电路效果因此是相同的：都让计数器停摆。它们的差别体现在**使用契约**上：

| 位 | 置 1 时的语义（提交信息意图） | 残余数据靠谁冲出去 |
| --- | --- | --- |
| `ToDisable` | 「忽略超时」，无条件不要时间兜底 | 只能靠后续新样本凑满整字，或靠触发 |
| `FrameTo` | 「基于帧的超时」，帧边界由触发界定 | 靠触发（`DataFifoIsTrig`）来结账 |

> 📌 **诚实提示**：在当前 HEAD 的 RTL 里，这两个位对 `TimeoutCnt` 的作用完全一致（都把它钉在 0），并且 input testbench 中 `FrameTo`/`ToDisable` 信号被声明却始终驱动为 `'0'`（见 [tb/psi_ms_daq_input/psi_ms_daq_input_tb.vhd:91-92](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_input/psi_ms_daq_input_tb.vhd#L91-L92)），即「置 1」的路径**没有被测试用例主动覆盖**。所以读者在二次开发时若要依赖它们的差异，建议先在自己的环境里实测确认。

#### 4.2.3 源码精读

**MODE 寄存器映射**（[hdl/psi_ms_daq_reg_axi.vhd:255-263](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L255-L263)）：MODE 寄存器（每流偏移 `0x8`）的 bit24/bit25 由字节写使能 `AccWr(3)` 统一解锁写入，并回读。

```vhdl
if AccWr(3) = '1' then
  v.Reg_Mode_ToDisable(Stream_v) := AccWrData(24);
  v.Reg_Mode_FrameTo(Stream_v)   := AccWrData(25);
end if;
...
v.RegRdval(24) := r.Reg_Mode_ToDisable(Stream_v);
v.RegRdval(25) := r.Reg_Mode_FrameTo(Stream_v);
```

**端口与跨时钟域**（[hdl/psi_ms_daq_input.vhd:56-57](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L56-L57) 与 [hdl/psi_ms_daq_input.vhd:408-425](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L408-L425)）：两个位作为单比特输入，与 `PostTrigSpls`(32 位)、`Mode`(2 位) 拼成 36 位，经 `psi_common_status_cc` 同步到 `Str_Clk`。

```vhdl
ToDisable    : in  std_logic;       -- $$ proc=stream $$
FrameTo      : in  std_logic;       -- $$ proc=stream $$
```

```vhdl
i_cc_reg_status : entity work.psi_common_status_cc
  generic map( width_g => 36 )
  port map(
    ...
    a_dat_i(34)           => ToDisable,
    a_dat_i(35)           => FrameTo,
    ...
    b_dat_o(34)           => ToDisable_Sync,
    b_dat_o(35)           => FrameTo_Sync
  );
```

**唯一使用点**（[hdl/psi_ms_daq_input.vhd:254](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L254)）：整份 input 代码里，`ToDisable_Sync`/`FrameTo_Sync` 只出现在这一行。

```vhdl
if Str_Vld = '1' or ToDisable_Sync = '1' or (FrameTo_Sync = '1' or r.FrameInProgr = '0') then
```

#### 4.2.4 代码实践

**实践目标**：在 4.1.4 的同一场景（16 位流、3 样本短帧）下，对比「超时启用」与「超时禁用」两种情况，残余数据的命运有何不同；并说明 `ToDisable` 与 `FrameTo` 的区别。

**操作步骤（源码阅读 + 推理）**：

1. 保持 4.1.4 的场景：施加 S0、S1、S2 后停住，不发触发。
2. **情况 A：超时启用（`ToDisable = 0`、`FrameTo = 0`）**。按 4.1.4 的走拍，约 `TimeoutLimit_c` 周期后残余被冲刷成 1 个字（`Bytes = 6`、`IsTo = 1`、`Last = 1`）写入 FIFO。
3. **情况 B：超时禁用（`ToDisable = 1`，或 `FrameTo = 1`）**。回到第 254 行那条 OR：因为 `ToDisable = 1`（或 `FrameTo = 1`），`TimeoutCnt` 每拍都被清零，永远到不了 `TimeoutLimit_c`，`Timeout` 永远为 0。于是：
   - 3 个样本一直挂在 `DataSftReg`，`WordCnt` 停在 3；
   - 数据 FIFO 不会收到任何字，`Daq_HasLast` 不会因这帧而置位；
   - 只有等到第 4 个样本到来（凑满整字 → 正常写出，`IsTo = 0`），或者等到一个触发（→ 写出 `IsTrig = 1` 的字），残余才会被冲出去。
4. 用一句话写下 `ToDisable` 与 `FrameTo` 的区别（参考 4.2.2 的表格）。

**需要观察的现象**：
- 情况 A：固定延迟后出现一个 `IsTo = 1` 的字；
- 情况 B：在该帧后续无样本、无触发的前提下，**永远不出现**对应的字；`Daq_Level` 也不因这帧而增加。

**预期结果**：超时启用 → 残余被定时冲刷为完整字；超时禁用 → 残余挂起直到新样本或触发到来。`ToDisable` 与 `FrameTo` 在当前 RTL 中电路效果相同（都关闭时间超时），差别在于设计意图：前者「无条件忽略超时」，后者「以触发作为帧边界、用基于帧的方式取代时间超时」。是否在本地真实观察到二者行为差异：**待本地验证**（因 testbench 未覆盖置 1 路径）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ToDisable`/`FrameTo` 用 `psi_common_status_cc`（多位电平 CDC）同步，而不是像 `Arm` 那样用 `psi_common_pulse_cc`（脉冲 CDC）？
**答案**：`ToDisable`/`FrameTo` 是持续生效的电平型配置（置 1 后一直生效），用 status_cc 同步电平即可；`Arm` 是一次性脉冲事件（来一拍就走），若用电平同步可能漏采，所以必须用 pulse_cc。这与 [u2-l2](u2-l2-input-interface-clocks.md) 讲的「按信号性质选 CDC」一致。

**练习 2**：若同时把 `ToDisable = 1` 且 `FrameTo = 1`，行为会变成「更严格的帧超时」吗？
**答案**：不会。由于两者在 OR 链里是并列关系，任一为 1 就把 `TimeoutCnt` 钉在 0；同时为 1 仍是「超时不计数」，与单独置 1 效果相同。

---

### 4.3 时间戳锁存、TsFifo 溢出处理与 g_timestamp 生成

#### 4.3.1 概念说明

很多采集场景需要知道「这一帧是什么时候发生的」。`psi_ms_daq_input` 支持为**每一个触发帧**附带一个 64 位时间戳（`Str_Ts` 输入）。注意三个关键设计决策：

1. **只在触发帧打时间戳**：时间戳代表「帧的发生时刻」，而帧是由触发界定的，所以只有 `IsTrig` 帧才有时间戳；纯超时帧（`IsTo`）不带时间戳（测试平台在 [tb/psi_ms_daq_input/psi_ms_daq_input_tb_pkg.vhd:203](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_input/psi_ms_daq_input_tb_pkg.vhd#L203) 显式断言超时帧时 `Ts_Vld = 0`）。
2. **锁存时刻 = 触发起始**：时间戳在「触发被认可且后触发计数尚未开始」的那一拍锁存进 `TsLatch`，再随触发帧末字写入时间戳 FIFO。
3. **溢出用全 1 占位**：时间戳 FIFO 很浅（默认 `StreamTsFifoDepth_g = 16`，仿真常量仅 3）。一旦它接近满，新时间戳不再丢弃整条帧，而是把这一帧的时间戳替换成 `0xFFFF..FFFF`（全 1）作为「时间戳无效」标记，并在 FIFO 排空后自动恢复。

整个时间戳通路受 `StreamUseTs_g`（布尔）总开关控制，关掉时用 `g_ntimestamp` 生成块把输出固定为「无效」。

#### 4.3.2 核心流程

时间戳数据通路是独立于数据 FIFO 的**第二条异步 FIFO**：

```
Str_Ts(64) ──► [TsLatch 锁存] ──► TsFifo(异步) ──► Ts_Data/Ts_Vld
                   ▲                   │
                   │                   └── 几乎满 ──► TsOverflow ──► 后续 TsLatch 填 0xFF..FF
                   │
              触发帧末字写出时（DataFifoIsTrig=1）才真正入 FIFO
```

- **溢出检测 `TsOverflow`**：当「时间戳 FIFO 几乎满」且「数据 FIFO 正在写」时，置位 `TsOverflow`；当「无待处理帧末」且「时间戳 FIFO 空」时清零。它是个**粘滞标志**：一旦置位，会持续影响后续若干帧的时间戳，直到 FIFO 彻底排空才解除（测试平台在 [tb/psi_ms_daq_input/psi_ms_daq_input_tb_case_ts_overflow.vhd:144-148](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_input/psi_ms_daq_input_tb_case_ts_overflow.vhd#L144-L148) 验证了「需要空若干拍才恢复」）。
- **锁存 `TsLatch`**：在触发起始拍（`TrigMasked = 1` 且 `PostTrigCnt = 0`），若 FIFO 不满且未溢出 → 锁存真实 `Str_Ts`；否则锁存全 1。
- **入 FIFO 时机**：`TsFifo_InVld = DataFifoVld and DataFifoIsTrig`——只有触发帧末字写出时才把 `TsLatch` 推进时间戳 FIFO，与数据严格对齐。
- **无数据时输出全 1**：FIFO 读侧没有有效时间戳时，`Ts_Data = (others => '1')`，`Ts_Vld = 0`。

#### 4.3.3 源码精读

**溢出检测**（[hdl/psi_ms_daq_input.vhd:201-210](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L201-L210)）：注意 `HasTlastSync` 是把 `ClkMem` 域的 `Daq_HasLast_I` 经两级打拍同步回 `Str_Clk` 域，用来判断「下游还有没有未读的帧末」。

```vhdl
if StreamUseTs_g then
  v.HasTlastSync(0) := Daq_HasLast_I;
  v.HasTlastSync(1) := r.HasTlastSync(0);
  if (TsFifo_AlmFull = '1') and (r.DataFifoVld = '1') then
    v.TsOverflow := '1';
  elsif (r.HasTlastSync(1) = '0') and (TsFifo_Empty = '1') then
    v.TsOverflow := '0';
  end if;
end if;
```

**时间戳锁存**（[hdl/psi_ms_daq_input.vhd:212-221](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L212-L221)）：触发起始拍决定锁存真值还是全 1 占位。

```vhdl
if StreamUseTs_g then
  if (TrigMasked_v = '1') and (unsigned(r.PostTrigCnt) = 0) then
    if (TsFifo_AlmFull = '1') or (r.TsOverflow = '1') then
      v.TsLatch := (others => '1');
    else
      v.TsLatch := Str_Ts;
    end if;
  end if;
end if;
```

**时间戳 FIFO 生成块 `g_timestamp`**（[hdl/psi_ms_daq_input.vhd:545-574](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L545-L574)）：这是第二条 `psi_common_async_fifo`，跨 `Str_Clk → ClkMem`。关键是 `afull_lvl_g => StreamTsFifoDepth_g - 1`（只剩 1 项就报几乎满），以及读侧「无有效数据则输出全 1」。

```vhdl
g_timestamp : if StreamUseTs_g generate
  TsFifo_InVld <= r.DataFifoVld and r.DataFifoIsTrig;

  i_tsfifo : entity work.psi_common_async_fifo
    generic map(
      width_g     => 64,
      depth_g     => StreamTsFifoDepth_g,
      afull_on_g  => True,
      afull_lvl_g => StreamTsFifoDepth_g - 1,
      ...
      ram_style_g => TsFifoStyle_c
    )
    port map( ... in_dat_i => r.TsLatch, in_vld_i => TsFifo_InVld, ... );
  Ts_Vld  <= Ts_Vld_I;
  -- Replace data by 0xFF... if no valid timestamp is available
  Ts_Data <= (others => '1') when Ts_Vld_I = '0' else TsFifo_RdData;
end generate;
```

**关闭时间戳时的占位**（[hdl/psi_ms_daq_input.vhd:576-579](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L576-L579)）：`StreamUseTs_g = false` 时直接输出无效。

```vhdl
g_ntimestamp : if not StreamUseTs_g generate
  Ts_Vld  <= '0';
  Ts_Data <= (others => '1');
end generate;
```

> 小细节：浅 FIFO 用分布式 RAM、深 FIFO 用块 RAM，由 [hdl/psi_ms_daq_input.vhd:83](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L83) 的 `TsFifoStyle_c` 自动选择（`depth ≤ 64` 用 distributed）。

#### 4.3.4 代码实践

**实践目标**：阅读时间戳溢出用例，理解「全 1 占位」与「粘滞溢出恢复」的真实表现。

**操作步骤（源码阅读型）**：

1. 打开 [tb/psi_ms_daq_input/psi_ms_daq_input_tb_case_ts_overflow.vhd](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_input/psi_ms_daq_input_tb_case_ts_overflow.vhd)。
2. 看 `stream` 过程（第 81–103 行）：先连续施加 `StreamTsFifoDepth_g` 个触发帧把时间戳 FIFO 喂到接近满（第 82–84 行），制造溢出；接着在溢出条件下再施加 2 个帧（第 92–93 行）；最后施加 2 个正常帧验证恢复（第 101–102 行）。
3. 看 `daq` 过程（第 130–157 行）：前若干帧用 `CheckAcqData(..., FrameType_Trigger_c, ...)` 校验正常时间戳；溢出期间的 2 个帧用 `CheckAcqData(2, -1, ..., True)` 校验——`-1` 即期望 `0xFFFF..FFFF`，末尾 `True` 即 `IsBadTs = true`（见 [tb/psi_ms_daq_input/psi_ms_daq_input_tb_pkg.vhd:132](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_input/psi_ms_daq_input_tb_pkg.vhd#L132) 与第 193–197 行）。
4. 注意第 148 行的 `wait for 1 us` 与注释：恢复需要「时间戳 FIFO 空若干拍且数据 FIFO 无触发」，正对应 4.3.3 里 `TsOverflow` 的清零条件。

**需要观察的现象**：
- 溢出期间的时间戳值是 `0xFFFF..FFFF`，且 `Ts_Vld` 行为与 `IsBadTs` 期望一致；
- 即便溢出，**数据帧本身仍然正常写出**（只是时间戳被标记无效），这正是「不丢帧、只标记时间戳」的设计。

**预期结果**：与用例断言一致——溢出帧时间戳为全 1，排空后恢复正常时间戳。

#### 4.3.5 小练习与答案

**练习 1**：为什么时间戳 FIFO 的 `afull_lvl_g` 设成 `depth - 1`（只剩 1 项就报几乎满），而不是快真正满了才报？
**答案**：因为锁存与入 FIFO 之间隔了一拍（`TsLatch` 在触发起始拍锁存，到触发帧末字写出时才入 FIFO）。提前一拍报几乎满，可以给 `TsOverflow` 留出反应时间，尽量避免真正写满后丢数据；即便反应不及，也有「全 1 占位」兜底，保证帧与时间戳仍一一对齐。

**练习 2**：如果一个超时帧（`IsTo`）后面紧跟着一个触发帧（`IsTrig`），时间戳 FIFO 会收到几个写入？
**答案**：1 个。因为 `TsFifo_InVld = DataFifoVld and DataFifoIsTrig`，只有触发帧末字会触发时间戳入 FIFO；超时帧不消耗时间戳。

---

### 4.4 输出侧 TLAST 计数与电平上报（p_outlast）

#### 4.4.1 概念说明

数据 FIFO 与时间戳 FIFO 把帧写进去之后，下游（DMA 引擎 + 控制状态机）需要知道两件事才能正确调度：

1. **「这条流里有没有完整帧可以搬？」** —— 即有没有至少一个带 `Last` 的字停在缓冲里。这由 `Daq_HasLast` 信号回答。
2. **「缓冲里现在压了多少数据？」** —— 即 FIFO + 流水级里一共有多少个未读字。这由 `Daq_Level` 信号回答，状态机用它判断能不能凑一个最小突发。

这两个信号都产生在输出侧 `ClkMem` 域的 `p_outlast` 进程里。难点在于：**帧是在 `Str_Clk` 域产生的（写 FIFO 时打 `IsTo`/`IsTrig`），却在 `ClkMem` 域被消费（DMA 读走）**。所以需要一个跨时钟域的「帧计数对账」机制——这正是 `TLastCnt`/`InTlastCnt`/`OutTlastCnt` 三个计数器的职责。

#### 4.4.2 核心流程

TLAST 对账的核心思想是**「生产计数 − 消费计数 = 缓冲里剩余的帧末数」**：

```
Str_Clk 域:                              ClkMem 域:
  每写一个 IsTo/IsTrig 字                  每读一个 Last 字
     └─► TLastCnt ++                        └─► OutTlastCnt ++
                    │                                       │
                    │  psi_common_status_cc                 │
                    └──────────► InTlastCnt ◄───────────────┘
                                       │
                          Daq_HasLast = (InTlastCnt != OutTlastCnt)
                          （不等 ⇒ 缓冲里还有未消费的帧末）
```

- **生产端 `TLastCnt`**（`Str_Clk`）：每当一个带 `IsTo` 或 `IsTrig` 的字被写进数据 FIFO，它就 +1（[hdl/psi_ms_daq_input.vhd:265-268](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L265-L268)）。
- **跨域同步 `InTlastCnt`**：`TLastCnt` 经 `psi_common_status_cc` 同步到 `ClkMem` 域，成为「已生产的帧末数」的 `ClkMem` 视角（[hdl/psi_ms_daq_input.vhd:477-489](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L477-L489)）。
- **消费端 `OutTlastCnt`**（`ClkMem`）：每当 DMA 用 `Daq_Rdy` 握手读走一个 `Last = 1` 的字，它就 +1。
- **`Daq_HasLast`**：`InTlastCnt ≠ OutTlastCnt` 即说明「还有未读的帧末」，置 1。

> 用计数器差值而不是直接传递电平，是因为「帧末」本质上是脉冲事件、却又要跨异步时钟域。用「单调递增计数器 + status_cc 同步」是处理这类「跨域事件计数」的标准稳妥做法：计数器只会递增、不会回退，即使同步过程中出现短暂的旧值，差值也最多短暂偏小，不会误报「有帧」。

`Daq_Level` 的计算则要补偿流水级 `pl_stage` 的那一拍延迟：

\[
\text{Daq\_Level} = \text{DataFifo\_Level} + \text{DataPl\_Level}
\]

其中 `DataPl_Level` 跟踪流水级里是否压着一个未读字。

#### 4.4.3 源码精读

**生产端 `TLastCnt`**（[hdl/psi_ms_daq_input.vhd:265-268](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L265-L268)）：在 `Str_Clk` 域的 `p_comb` 里，写 FIFO 且本字是帧末（`IsTo` 或 `IsTrig`）时计数。

```vhdl
-- TLast counter
if (r.DataFifoVld = '1') and ((r.DataFifoIsTo = '1') or (r.DataFifoIsTrig = '1')) then
  v.TLastCnt := std_logic_vector(unsigned(r.TLastCnt) + 1);
end if;
```

计数器宽度由缓冲深度派生（[hdl/psi_ms_daq_input.vhd:89](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L89)）：

```vhdl
constant TlastCntWidth_c : positive := log2ceil(StreamBuffer_g) + 1;
```

**跨域同步**（[hdl/psi_ms_daq_input.vhd:477-489](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L477-L489)）：把 `TLastCnt` 从 `Str_Clk` 同步到 `ClkMem` 得到 `InTlastCnt`。

```vhdl
i_cc : entity work.psi_common_status_cc
  generic map( width_g => TlastCntWidth_c )
  port map(
    a_clk_i => Str_Clk,
    a_rst_i => Str_Rst,
    a_rst_o => open,
    a_dat_i => r.TLastCnt,
    b_clk_i => ClkMem,
    b_rst_i => '0',
    b_dat_o => InTlastCnt
  );
```

**输出侧对账与电平计算 `p_outlast`**（[hdl/psi_ms_daq_input.vhd:364-402](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L364-L402)）：整个进程跑在 `ClkMem`。`Daq_HasLast_I` 默认每拍为 0，仅当「生产 ≠ 消费」时拉 1；同时维护流水级占用 `DataPl_Level`，把 FIFO 水位与流水级占用相加得到 `Daq_Level`。

```vhdl
p_outlast : process(ClkMem)
  variable PlLevel_v : unsigned(DataPl_Level'range);
begin
  if rising_edge(ClkMem) then
    Daq_HasLast_I <= '0';                                          -- 默认值

    -- 消费端：每读走一个 Last 字，OutTlastCnt +1
    if (Daq_Vld_I = '1') and (Daq_Rdy = '1') and (Daq_Data_I.Last = '1') then
      OutTlastCnt <= std_logic_vector(unsigned(OutTlastCnt) + 1);
    end if;

    -- 对账：生产 != 消费 ⇒ 还有未读帧末
    if OutTlastCnt /= InTlastCnt then
      Daq_HasLast_I <= '1';
    end if;

    -- 电平计算（把流水级里可能压着的一个字也算进去）
    PlLevel_v := DataPl_Level;
    if DataFifo_PlRdy = '1' and DataFifo_PlVld = '1' then
      PlLevel_v := PlLevel_v + 1;
    end if;
    if Daq_Vld_I = '1' and Daq_Rdy = '1' then
      PlLevel_v := PlLevel_v - 1;
    end if;
    DataPl_Level <= PlLevel_v;
    Daq_Level    <= std_logic_vector(resize(unsigned(DataFifo_Level), Daq_Level'length) + DataPl_Level);
    ...
```

> 注释（第 390 行）特别说明：`Daq_Level` 比真实值晚一拍，但这对状态机无影响——状态机只在数据真正搬完之后才据此决策。这是典型的「用一拍延迟换时序」的权衡。

#### 4.4.4 代码实践

**实践目标**：跟踪一次「超时帧从写入到被 DMA 读走」的 TLAST 对账全过程，亲手验证 `Daq_HasLast` 何时置位、何时清零。

**操作步骤（源码阅读 + 推理）**：

1. 沿用 4.1.4 的超时冲刷结果：残余被写成一个 `IsTo = 1`、`Last = 1` 的字进入数据 FIFO。
2. 在 `Str_Clk` 域：因 `DataFifoVld = 1` 且 `DataFifoIsTo = 1`，`TLastCnt` 自增 1（[hdl/psi_ms_daq_input.vhd:265-268](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L265-L268)）。
3. `TLastCnt` 经 `i_cc` 同步到 `ClkMem`，`InTlastCnt` 随之变为新值。
4. 在 `ClkMem` 域的 `p_outlast`：此时 `OutTlastCnt` 尚未变（DMA 还没读），`InTlastCnt ≠ OutTlastCnt` 成立 → `Daq_HasLast_I = 1`。
5. DMA 看到 `Daq_HasLast = 1`，开始用 `Daq_Rdy` 读走这个 `Last = 1` 的字；读完这一拍 `OutTlastCnt +1`。
6. 之后 `InTlastCnt = OutTlastCnt` → `Daq_HasLast` 回到 0。

**需要观察的现象**：
- `Daq_HasLast` 在「帧已入 FIFO 但尚未被读走」期间为 1；
- 一旦 DMA 读走该帧末字，`Daq_HasLast` 在随后一拍回到 0；
- 若缓冲里压着多个帧末（例如连续多帧未读），`Daq_HasLast` 会持续为 1，直到全部读完。

**预期结果**：与上述对账过程一致。是否在本地波形上精确捕捉「晚一拍」的 `Daq_Level`：**待本地验证**（取决于具体激励与采样时刻）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 TLAST 计数用「单调递增计数器 + status_cc 同步」，而不是把 `IsTo`/`IsTrig` 直接做成脉冲用 `pulse_cc` 跨域？
**答案**：因为 `Daq_HasLast` 需要在 `ClkMem` 域知道「缓冲里累计还剩几个帧末」，这是一个需要持续比对的**电平型**信息，而不是一次性事件。用单调递增计数器对账（生产 − 消费）天然抗跨域毛刺：即使同步拿到稍旧的计数值，也只是短暂少报，绝不会凭空多报一个不存在的帧；而脉冲跨域更适合「通知一次」的场景，不便做累计对账。

**练习 2**：`Daq_Level = DataFifo_Level + DataPl_Level` 里，`DataPl_Level` 的作用是什么？
**答案**：数据 FIFO 后面跟着一级 `psi_common_pl_stage` 流水级（用于时序优化，见 [u2-l2](u2-l2-input-interface-clocks.md)）。一个字可能正卡在这一级里尚未出现在 `Daq_Vld` 上，因此 `DataFifo_Level`（FIFO 自身水位）会少算这一个字。`DataPl_Level` 正是用来补偿这一拍延迟，让上报给状态机的 `Daq_Level` 更接近真实未读字数。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「端到端」的源码追踪与预测。

**场景**：`StreamWidth_g = 16`、`IntDataWidth_g = 64`、`StreamTimeout_g = 10.0e-6`、`StreamClkFreq_g = 125.0e6`、`StreamTsFifoDepth_g = 3`、`StreamUseTs_g = true`，记录模式 Continuous，`PostTrigSpls = 0`。激励序列：

1. 施加 2 个非触发样本 S0、S1，随后 `Str_Vld = 0` 保持足够久（远超超时周期），期间无触发；
2. 接着施加 1 个带触发的样本（`Str_Trig = 1`，携带 `Str_Ts = 0x1234`）。

**任务**：

- **(a)** 第 1 步结束后，写出超时冲刷出来的字的 `Data`、`Bytes`、`IsTo`、`IsTrig`、`Last` 各为何值；指出 `TLastCnt` 是否自增、时间戳 FIFO 是否收到写入。
- **(b)** 第 1 步产生的 `Daq_HasLast` 何时变 1？若 DMA 一直不来读（`Daq_Rdy = 0`），`Daq_HasLast` 会怎样？
- **(c)** 第 2 步的触发帧写出后，时间戳 FIFO 收到的 64 位数据是 `0x1234` 还是 `0xFFFF..FFFF`？为什么？（提示：先判断在触发起始拍时 `TsOverflow` 与 `TsFifo_AlmFull` 的状态。）
- **(d)** 若在第 1 步前就把 `ToDisable` 置 1，整个序列的输出会变成什么？

**参考思路**（建议你先独立推导，再对照）：

- (a) `WconvFactor_c = 4`，2 个样本是残余。超时冲刷：`Data` 低 32 位为 S0|S1、高 32 位为 0；`Bytes = (2+0)&"0" = 4`；`IsTo = 1`、`IsTrig = 0`、`Last = 1`。`TLastCnt` 自增 1；时间戳 FIFO **不**写入（超时帧无时间戳）。
- (b) 冲刷字进入 FIFO、`InTlastCnt` 同步过来后，`Daq_HasLast = 1`；若 DMA 不读，`OutTlastCnt` 不变，`Daq_HasLast` 持续为 1。
- (c) 在第 2 步触发起始拍，时间戳 FIFO 此前未被任何触发帧写入过（第 1 步是超时帧），故 `TsFifo_AlmFull = 0`、`TsOverflow = 0` → 锁存真实 `Str_Ts = 0x1234`；该触发帧末字写出时入 FIFO。所以收到 `0x0000000000001234`。
- (d) `ToDisable = 1` 使第 1 步的超时不触发，S0、S1 挂在移位寄存器；第 2 步的触发样本到来时会与残余一起被冲刷（触发强制写出当前字），产生一个 `IsTrig = 1` 的字，包含 S0、S1 与触发样本。

> 是否需要本地仿真复核：(a)(b)(c) 可通过细读源码严格推导得出；(d) 涉及「禁用超时 + 触发合并残余」的边界行为，建议**本地验证**。

## 6. 本讲小结

- 超时机制由 `TimeoutLimit_c`（编译期由 `StreamClkFreq_g × StreamTimeout_g` 换算）、`TimeoutCnt`、`Timeout` 标志和冲刷动作四件套构成；它只在「帧进行中（`FrameInProgr = 1`）且无新样本」时计数，到顶后把残余样本冲成一个带 `IsTo = 1` 的字。
- 超时帧的字节计数用 `AddSamples_v = 0` 来「不算当前拍的新样本」，因此残余字的 `Bytes` 正好等于「已攒样本数 × 每样本字节数」；字对齐但无触发的帧还会被补发一个 `Bytes = 0` 的空超时字作帧尾。
- `ToDisable`（MODE bit24）与 `FrameTo`（MODE bit25）都出现在超时清零的 OR 链里，置任一个都会把 `TimeoutCnt` 钉在 0、关闭时间超时；二者设计意图不同（「忽略超时」 vs 「基于帧/触发的超时」），但当前 RTL 电路效果相同，且 input testbench 未覆盖置 1 路径。
- 时间戳只在触发帧（`DataFifoIsTrig`）打标，锁存于触发起始拍；时间戳 FIFO 几乎满或溢出时用 `0xFF..FF` 占位、且 `TsOverflow` 是粘滞标志，需 FIFO 排空若干拍才恢复；`StreamUseTs_g = false` 时直接输出全 1 无效。
- 输出侧 `p_outlast` 用「生产计数 `TLastCnt → InTlastCnt`」减「消费计数 `OutTlastCnt`」得到 `Daq_HasLast`（缓冲里是否还有未读帧末），并用 `Daq_Level = DataFifo_Level + DataPl_Level` 补偿流水级延迟上报数据量。

## 7. 下一步学习建议

本讲把「单路流入口」的输入逻辑彻底讲完了（接口与时钟域 → 模式/触发/后触发 → 超时/时间戳/TLAST 上报）。接下来可以：

- **沿数据通路继续向下游**：学习 [u2-l5（DMA 引擎接口与缓存结构）](u2-l5-dma-interface-fifos.md) 与 [u2-l6（DMA 引擎状态机）](u2-l6-dma-statemachine-alignment.md)，看本讲输出的 `Daq_Data`/`Daq_Vld`/`Daq_HasLast`/`Daq_Level` 是如何被 DMA 引擎消费、并拼成 AXI 突发写进 DDR 的。
- **理解 `Daq_HasLast` 的归宿**：它在 [u3-l1（控制状态机总览与仲裁）](u3-l1-sm-overview-arbitration.md) 里是状态机判断「这条流有没有完整窗口可搬」的关键输入之一，并与 [u4-l1（中断生成与 IRQ FIFO）](u4-l1-irq-generation-fifo.md) 的中断链路挂钩——窗口的「完成」正是由这里的帧末（`Last`/`IsTrig`）逐级传递上去的。
- **想动手验证超时与时间戳**：直接跑 input testbench 的 `timeout` 与 `ts_overflow` 两个用例（运行方式见 [u1-l2](u1-l2-repo-and-simulation.md)），对照本讲的走拍预测看波形。
