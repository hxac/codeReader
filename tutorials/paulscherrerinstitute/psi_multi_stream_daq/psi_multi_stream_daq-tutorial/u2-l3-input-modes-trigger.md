# 输入逻辑核心：记录模式、触发与后触发计数

## 1. 本讲目标

上一讲（u2-l2）我们已经看清 `psi_ms_daq_input` 的接口、三个时钟域和数据缓冲结构。本讲我们要钻进它的「大脑」——组合进程 `p_comb`，回答三个核心问题：

1. **什么算一次触发？** 四种记录模式（Continuous / TriggerMask / SingleShot / Manual）对 `Str_Trig` 的「屏蔽规则」各不相同，本讲要讲清 `TrigMasked_v` 这个变量在每种模式下的取值。
2. **触发之后什么时候结束一帧？** 由后触发计数器 `PostTrigCnt` 倒数决定，倒数到 0 时给当前字打上 `DataFifoIsTrig` 标志并结束一帧。
3. **什么时候允许往 FIFO 写数据？** 由 `IsArmed`（是否武装）和 `RecEna`（是否允许录制）两个状态位共同决定，二者在模式切换时有不同的复位行为。

学完本讲，你应该能够：
- 看着 `p_comb` 源码，说出任意一种模式下「一个 `Str_Trig` 脉冲会不会触发、会不会结束一帧」。
- 手工推演 `PostTrigCnt` 的倒数过程，预测一帧从触发到结束会记录多少个样本。
- 区分 TriggerMask 与 SingleShot 在「停止记录」语义上的关键差异——这是本讲代码实践的重点。

## 2. 前置知识

阅读本讲前，你需要先理解以下概念（部分已在 u2-l1、u2-l2 建立）：

- **样本（Sample）与字（Word）**：流输入 `Str_Data` 一次给出一个样本，位宽由 `StreamWidth_g`（8/16/32/64）决定；输入逻辑把若干样本拼成一个 `IntDataWidth_g`（默认 64）位的内部字再写入数据 FIFO。一个完整字包含 \( WconvFactor_c = IntDataWidth_g / StreamWidth_g \) 个样本。
- **帧（Frame）**：一段连续记录的数据，以一个带 `Last=1` 的字结尾。`Last` 由 `IsTrig`（触发结束）或 `IsTo`（超时结束，下一讲 u2-l4 讲）置位。本讲只关心 `IsTrig`。
- **两进程法（two-process method）**：`p_comb` 是纯组合进程，根据当前寄存器状态 `r` 和输入算出下一拍状态 `r_next`；`p_seq` 在 `Str_Clk` 上升沿把 `r_next` 打入 `r`。所以源码里 `r.Xxx` 是「当前值」、`v.Xxx` 是「下一拍值」。
- **`ProcessSample_v`**：本讲最关键的辅助判断，定义为 `(DataFifo_InRdy = '1') and (Str_Vld = '1')`，即「本拍真的有一个样本被接收进 FIFO」。后触发计数只在「正在接收样本」时才推进。
- **记录模式 `RecMode_t`**：2 位枚举，定义在公共包里，取值见下表。

| 常量 | 编码 | 含义 |
|---|---|---|
| `RecMode_Continuous_c` | `00` | 连续录制，触发直接结束当前帧 |
| `RecMode_TriggerMask_c` | `01` | 触发屏蔽模式，需先 Arm，触发结束当前帧后继续录 |
| `RecMode_SingleShot_c` | `10` | 单次模式，需先 Arm，触发结束帧后停止录制 |
| `RecMode_ManuelMode_c` | `11` | 手动模式，Arm 脉冲本身即触发源 |

> 提示：源码里 Manual 模式的常量与注释写作 `ManuelMode`（拼写如此），并非笔误，本讲沿用源码写法。

四种模式编码定义在公共包 [hdl/psi_ms_daq_pkg.vhd:28-32](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_pkg.vhd#L28-L32)。

## 3. 本讲源码地图

本讲全部围绕单一文件展开：

| 文件 | 作用 |
|---|---|
| [hdl/psi_ms_daq_input.vhd](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd) | 输入逻辑本体。本讲聚焦其中的组合进程 `p_comb`（约 159–335 行） |

辅助参考（用于代码实践对照）：

| 文件 | 作用 |
|---|---|
| hdl/psi_ms_daq_pkg.vhd | `RecMode_t` 与四个模式常量的定义 |
| tb/psi_ms_daq_input/psi_ms_daq_input_tb_case_modes.vhd | 四种模式的仿真用例，本讲用它验证我们的推演 |
| tb/psi_ms_daq_input/psi_ms_daq_input_tb_pkg.vhd | `ApplyStrData`/`CheckAcqData` 激励与校验过程 |

`p_comb` 内部与本讲相关的三个最小模块在源码中的位置：

- **触发屏蔽** `TrigMasked_v` 的 case 分支：[hdl/psi_ms_daq_input.vhd:179-189](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L179-L189)
- **后触发计数与帧结束** `PostTrigCnt` / `DataFifoIsTrig`：[hdl/psi_ms_daq_input.vhd:223-242](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L223-L242)
- **武装与录制使能** `IsArmed` / `RecEna`：[hdl/psi_ms_daq_input.vhd:305-330](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L305-L330)

## 4. 核心概念与源码讲解

在进入三个模块之前，先看 `p_comb` 最顶部的「默认值」与「寄存器镜像」段，这是理解后续逻辑的前提：

```vhdl
-- *** Hold variables stable ***
v := r;
...
-- *** Input Logic Stage ***
-- Default values
v.DataFifoIsTo   := '0';
v.DataFifoIsTrig := '0';
v.ModeReg        := Mode_Sync;
v.ArmReg         := Arm_Sync;
```

引自 [hdl/psi_ms_daq_input.vhd:166-177](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L166-L177)。

要点：
- `v := r` 把整拍状态「续」下来，所以 `v` 中任何没被显式赋值的字段都保持原值。这是两进程法的关键——状态有「惯性」。
- `DataFifoIsTrig` 每拍默认清 0，只有触发结束帧的那一拍才会被置 1。
- `ModeReg`/`ArmReg` 每拍把跨时钟域同步过来的 `Mode_Sync`/`Arm_Sync`（来自寄存器时钟域，见 u2-l2 的 `i_cc_reg_status`/`i_cc_reg_pulse`）锁进本地寄存器，后续逻辑读 `r.ModeReg`/`r.ArmReg`。

### 4.1 触发屏蔽：四种记录模式如何决定「什么算触发」

#### 4.1.1 概念说明

外部送的 `Str_Trig` 是一个原始触发脉冲，但输入逻辑并不会无条件接受它。不同应用场景对触发的要求不同：

- **Continuous（连续）**：永远在录，任何 `Str_Trig` 都立刻生效，用来把当前累积的数据「封口」成一帧。
- **TriggerMask / SingleShot**：必须先「武装」（Arm）才接受触发，避免在准备就绪前误触发。二者在「触发之后是否继续录」上有区别（4.3 节讲）。
- **Manual（手动）**：根本不看 `Str_Trig`，软件给的 `Arm` 脉冲本身就是触发源，常用于软件控制的一次性抓取。

`TrigMasked_v` 就是「经过模式屏蔽后的有效触发」，后续所有触发相关逻辑都用它，而不用原始 `Str_Trig`。

#### 4.1.2 核心流程

`TrigMasked_v` 是一个组合变量，按 `r.ModeReg`（当前模式）查表得出：

```
TrigMasked_v =
  Continuous  : Str_Trig                       -- 原样放行
  TriggerMask : Str_Trig AND r.IsArmed         -- 必须已武装
  SingleShot  : Str_Trig AND r.IsArmed         -- 必须已武装
  Manual      : r.ArmReg                       -- Arm 寄存器即触发
```

注意 Manual 模式里 `TrigMasked_v` 取的是 `r.ArmReg`（已寄存的武装信号），而不是当拍的 `Arm_Sync`，这会让「武装」与「触发」之间存在一拍对齐关系，4.3 节会用到。

#### 4.1.3 源码精读

```vhdl
-- Masking trigger according to recording mode
case r.ModeReg is
  when RecMode_Continuous_c =>
    TrigMasked_v := Str_Trig;
  when RecMode_TriggerMask_c |
         RecMode_SingleShot_c =>
    TrigMasked_v := Str_Trig and r.IsArmed;
  when RecMode_ManuelMode_c =>
    TrigMasked_v := r.ArmReg;
  when others => null;
end case;
```

引自 [hdl/psi_ms_daq_input.vhd:179-189](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L179-L189)。

中文逐行说明：
- 第 181–182 行：Continuous 模式直接把外部 `Str_Trig` 当有效触发。
- 第 183–185 行：TriggerMask 与 SingleShot 共用同一条规则——`Str_Trig` 必须和「已武装」标志 `r.IsArmed` 同时为 1 才算有效。`IsArmed` 由 Arm 脉冲置位、由触发清零（详见 4.3）。
- 第 186–187 行：Manual 模式忽略 `Str_Trig`，把武装寄存器 `r.ArmReg` 直接当作触发。

紧接其后还有一段「触发锁存」，用于处理「触发来了但本拍没有样本被接收」的情况：

```vhdl
-- Trigger Latching
if ProcessSample_v then
  v.TrigLatch := '0';
else
  v.TrigLatch := r.TrigLatch or TrigMasked_v;
end if;
```

引自 [hdl/psi_ms_daq_input.vhd:194-199](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L194-L199)。

含义：后触发计数只在「正在接收样本」（`ProcessSample_v`）时推进，所以如果一个有效触发 `TrigMasked_v` 出现在没有样本的拍里，就先锁进 `TrigLatch`，等下一个样本到来时一并处理；一旦有样本被处理，锁存即清零。这保证了「触发不丢」。

#### 4.1.4 代码实践

**目标**：用仿真用例确认「未武装时触发被屏蔽」。

**操作步骤**（源码阅读型实践）：
1. 打开 [tb/psi_ms_daq_input/psi_ms_daq_input_tb_case_modes.vhd](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_input/psi_ms_daq_input_tb_case_modes.vhd)。
2. 看 TriggerMask 段（`TestCase := 0` 附近）：先调用 `ApplyStrData(5, 1, 1, ...)` 送 5 个样本、在第 1 个样本上带 `Str_Trig`，**此时还没有 `PulseSig(Arm)`**。
3. 接着 `PulseSig(Arm)` 后再送第二组数据。

**需要观察的现象**：第一组的 `Str_Trig` 因为 `IsArmed=0` 被 `TrigMasked_v` 屏蔽，不会结束帧；这 5 个样本会「挂」在 FIFO 里，直到第二组的触发才一起被封口。

**预期结果**：daq 侧用 `CheckAcqData(5+4, 2, ...)` 校验——期望 9 个样本、时间戳 2、`FrameType_Trigger_c`。9 = 第一组的 5 个（无触发，未封口）+ 第二组的 4 个（触发后含 3 个后触发样本）。这正好印证「未武装的触发被丢弃，但数据仍连续记录」。

> 说明：本实践基于源码与已有用例的静态分析，具体波形「待本地用 PsiSim/Modelsim 运行 `modes` 用例验证」（运行方式见 u1-l2、u5-l1）。

#### 4.1.5 小练习与答案

**练习 1**：在 Continuous 模式下，`IsArmed` 还有意义吗？为什么 `TrigMasked_v` 不依赖它？

**答案**：没有意义。Continuous 模式下 `TrigMasked_v := Str_Trig`，完全不读 `IsArmed`；对应地，4.3 节会看到 Continuous 模式下 `IsArmed` 被强制清 0，因为它不被使用。

**练习 2**：Manual 模式下，外部送一个 `Str_Trig` 脉冲会发生什么？

**答案**：什么都不会发生。Manual 模式 `TrigMasked_v := r.ArmReg`，根本不看 `Str_Trig`。要触发必须由软件发 `Arm` 脉冲。

### 4.2 后触发计数与帧结束：`DataFifoIsTrig` 的产生

#### 4.2.1 概念说明

「触发」只是标记「从这里开始倒计时」。真正决定一帧何时结束的是**后触发样本数** `PostTrigSpls`（由软件配置，跨时钟域同步后为 `PostTrigSpls_Sync`）。它的语义是：**触发之后再记录多少个样本**。

很多采集场景（比如示波器）都需要「触发点之后还要看 N 个样本」的能力，这就是后触发（post-trigger）。输入逻辑用一个倒数计数器 `PostTrigCnt` 实现：触发到来时装载 `PostTrigSpls`，此后每接收一个样本减 1，减到 0 时给当前字打上 `DataFifoIsTrig=1` 并结束这一帧。

#### 4.2.2 核心流程

设后触发样本数 \( N = PostTrigSpls\_Sync \)，触发发生在样本 \( T \)（该样本当拍 `TrigMasked_v=1` 或 `TrigLatch=1` 且 `PostTrigCnt=0`）：

```
触发拍 (样本 T):
  if N == 0:            # 不要后触发样本，立即在当前字结束
      DataFifoIsTrig := 1; 写 FIFO; RecEna := 0
  else:                 # 要 N 个后触发样本
      PostTrigCnt := N  # 装载计数器（样本 T 本身仍被记录）

后续每接收一个样本:
  PostTrigCnt := PostTrigCnt - 1
  当 PostTrigCnt 从 1 减到 0 的那一拍:
      DataFifoIsTrig := 1; 写 FIFO; RecEna := 0   # 帧结束
```

从触发样本起总共记录的样本数为：

\[
\text{帧内样本数（自触发起）} = 1 + N
\]

即「触发样本 + N 个后触发样本」。如果之前还有连续记录的「预触发」样本（TriggerMask 模式常见），它们会和这 \( 1+N \) 个样本拼在同一帧里。

> 边界：`PostTrigSpls=0` 表示「触发即结束」——触发当拍就给当前字打 `DataFifoIsTrig` 并写 FIFO，不装载计数器。

#### 4.2.3 源码精读

```vhdl
-- Trigger handling and post trigger counter
if ProcessSample_v and r.RecEna = '1' then
  if r.PostTrigCnt /= 0 then
    v.PostTrigCnt := r.PostTrigCnt - 1;
    if r.PostTrigCnt = 1 then
      v.DataFifoIsTrig := '1';
      v.DataFifoVld    := r.DataFifoVld or r.RecEna;
      v.RecEna         := '0';      -- stop recording after frame
    end if;
  elsif (r.TrigLatch = '1') or (TrigMasked_v = '1') then
    -- Handle incoming trigger sample
    if unsigned(PostTrigSpls_Sync) = 0 then
      v.DataFifoIsTrig := '1';
      v.DataFifoVld    := r.DataFifoVld or r.RecEna;
      v.RecEna         := '0';      -- stop recording after frame
    else
      v.PostTrigCnt := unsigned(PostTrigSpls_Sync);
    end if;
  end if;
end if;
```

引自 [hdl/psi_ms_daq_input.vhd:223-242](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L223-L242)。

中文逐段说明：
- **外层 `if`**（第 224 行）：整段逻辑只在「正在接收样本」且「允许录制」时才执行。这保证触发与计数都按「样本粒度」推进。
- **第 225–231 行（正在倒数）**：`PostTrigCnt != 0` 说明处于后触发窗口。每接收一个样本减 1；当「当前值是 1」（即这一拍减到 0）时，给当前字打 `DataFifoIsTrig=1`，把字写进 FIFO（`DataFifoVld := ... or RecEna`，因 `RecEna=1` 所以一定会写），并清 `RecEna` 结束录制。
- **第 232–241 行（检测到新触发）**：`PostTrigCnt=0` 时若发现有效触发（锁存或本拍 `TrigMasked_v`）。若 `PostTrigSpls=0` 立即结束当前字；否则把 `PostTrigSpls_Sync` 装进 `PostTrigCnt`，开始倒数。

注意：触发当拍并不会写 FIFO（除非 `PostTrigSpls=0`），它只装载计数器；触发样本本身走的是下方「Process input data」段（第 277 行起）正常进入移位寄存器。

`DataFifoIsTrig` 如何变成下游的 `Last`？看数据打包与解包：

```vhdl
DataFifo_InData(DataFifo_InData'high)     <= r.DataFifoIsTrig;
...
Daq_Data_I.IsTrig <= DataFifo_OutData(DataFifo_OutData'high);
Daq_Data_I.Last   <= Daq_Data_I.IsTo or Daq_Data_I.IsTrig;
```

引自 [hdl/psi_ms_daq_input.vhd:494-495](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L494-L495) 与 [hdl/psi_ms_daq_input.vhd:538-540](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L538-L540)。

即 `DataFifoIsTrig` 作为元数据位随字写入数据 FIFO，经 `pl_stage` 流水级后，在输出侧还原成 `Daq_Data.IsTrig`，再与 `IsTo` 合成 `Last`。所以「触发结束的帧」在下游表现为「带 `IsTrig=1`、`Last=1` 的最后一个字」。

> 关于「字未满」：若帧结束在一个未拼满的中间字（`WordCnt < WconvFactor_c-1`），上述逻辑仍会把该部分字写进 FIFO（`DataFifoVld` 被强置为 1），字节计数字段 `DataFifoBytes`（[第 291–303 行](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L291-L303)）会正确反映有效字节数，下游不会误读残余字节。

#### 4.2.4 代码实践

**目标**：手工推演一次后触发计数，对照仿真用例验证样本数。

**场景**：SingleShot 模式，`PostTrigSpls=3`，`StreamWidth_g=16`（故 `WconvFactor_c=4`，每 4 样本一个 64 位字）。Arm 后送 6 个样本，`Str_Trig` 出现在第 2 个样本上（样本序号从 0 计）。

**操作步骤**：
1. 建一张表，列出每个样本到来时 `PostTrigCnt`、`WordCnt`、本字是否写 FIFO、是否带 `DataFifoIsTrig`。
2. 数出总共记录多少个样本、最后一个字的 `Bytes` 是多少。

**预期结果**（对照用例 `case_modes` 的 SingleShot 段 `CheckAcqData(6, 2, ..., FrameType_Trigger_c, ..., false, 5)`）：

| 样本序号 | 触发? | PostTrigCnt (处理后) | WordCnt (处理后) | 写 FIFO? | IsTrig? |
|---|---|---|---|---|---|
| 0 | 否 | 0 | 1 | 否（未满 4） | 否 |
| 1 | 否 | 0 | 2 | 否 | 否 |
| 2 | **是** | 装载→3 | 3 | 否 | 否 |
| 3 | 否 | 3→2 | 0（满字写 FIFO） | **是**（满字） | 否 |
| 4 | 否 | 2→1 | 1 | 否 | 否 |
| 5 | 否 | 1→0 | 2 | **是**（触发结束，部分字） | **是** |

记录总数 = 6 个样本（0–5），其中 2 个预触发（0、1）+ 触发样本（2）+ 3 个后触发（3、4、5），与公式 \( 1+N = 1+3 = 4 \) 个「触发及之后」样本一致。最后一字只含样本 4、5 共 4 字节，带 `IsTrig=1`。这与用例期望的「6 样本、`FrameType_Trigger_c`」吻合。

> 本表为静态推演，实际 `WordCnt` 是否在满字当拍清零请「待本地仿真确认」。

#### 4.2.5 小练习与答案

**练习 1**：把 `PostTrigSpls` 设为 0，触发到来后会发生什么？

**答案**：触发当拍直接进入 `if unsigned(PostTrigSpls_Sync) = 0 then` 分支，立即给当前字打 `DataFifoIsTrig=1`、写 FIFO、清 `RecEna`，帧在触发样本处就结束，不记录任何后触发样本。

**练习 2**：如果触发脉冲出现在「没有样本被接收」的拍里，这个触发会丢吗？

**答案**：不会丢。`TrigMasked_v` 会被锁进 `TrigLatch`（4.1.3 的锁存逻辑），等下一个样本被接收时，`elsif (r.TrigLatch='1') or ...` 分支会捕获它并装载 `PostTrigCnt`。

**练习 3**：`DataFifoIsTrig` 与 `Daq_Data.Last` 是什么关系？

**答案**：`DataFifoIsTrig` 是输入逻辑内部的「本字因触发而成为帧尾」标志，随字写入 FIFO；输出侧还原为 `Daq_Data.IsTrig`，并与超时标志 `IsTo`「或」得到 `Daq_Data.Last`。所以触发结束的帧，最后一个字 `IsTrig=1` 且 `Last=1`。

### 4.3 武装与录制使能：`IsArmed` 与 `RecEna` 的状态管理

#### 4.3.1 概念说明

4.1 用到了 `IsArmed`，4.2 用到了 `RecEna`，这两个状态位决定了「触发是否被接受」和「数据是否被写入 FIFO」。它们看似简单，却承载了四种模式之间的本质差异：

- **`IsArmed`（已武装）**：只在 TriggerMask / SingleShot 里有意义，表示「软件已经发过 Arm，准备好接受下一次触发」。Arm 脉冲置 1，触发清 0——即「一次武装只接受一次触发」。
- **`RecEna`（允许录制）**：决定样本能否进入数据 FIFO 与后触发计数（见 4.2 外层 `if r.RecEna='1'`）。Continuous / TriggerMask 把它常置 1（一直在录）；SingleShot / Manual 只在 Arm 后置 1，触发结束后清 0（只录一帧）。

模式切换时，二者都会被复位，避免新模式的运行被旧模式残留状态污染。

#### 4.3.2 核心流程

**Arming Logic（`IsArmed`）：**

```
if (模式切换) or (Continuous) or (Manual):   # 这些情况 IsArmed 不用
    IsArmed := 0
elif ArmReg = 1:                              # Arm 脉冲到来
    IsArmed := 1
elif TrigMasked_v = 1:                        # 触发发生
    IsArmed := 0                              # 一次武装只接受一次触发
```

**Enable Recording Logic（`RecEna`）：**

```
case 模式:
  Continuous | TriggerMask:  RecEna := 1            # 永远允许录制
  SingleShot | Manual:       if ArmReg = 1: RecEna := 1   # Arm 时开始允许
if 模式切换:                  RecEna := 0            # 切模式立刻停录
# 注意：触发结束帧时清 RecEna 的动作在 4.2 的触发处理段里完成
```

**TriggerMask vs SingleShot 的关键差异**（本讲代码实践的核心）：

- **TriggerMask**：`RecEna` 被 case 段每拍重置为 1。所以即便 4.2 的触发处理在帧尾把 `RecEna` 清成 0，下一拍又会被这里置回 1。结果是：**触发只结束当前帧，紧接着就自动开始记录下一帧**——「停止记录」对 TriggerMask 不存在，只有「结束一帧」。
- **SingleShot**：`RecEna` 仅在 `ArmReg=1` 那拍被置 1，其余拍靠 `v:=r` 续住。触发结束帧时清 0 后，**没有任何逻辑把它置回 1**，于是录制真正停止，直到下一次 Arm。这才是真正的「停止记录」。

#### 4.3.3 源码精读

Arming Logic：

```vhdl
-- Handle Arming Logic
if (r.ModeReg /= Mode_Sync) or (r.ModeReg = RecMode_Continuous_c) or (r.ModeReg = RecMode_ManuelMode_c) then -- reset on mode change!
  v.IsArmed := '0';
elsif r.ArmReg = '1' then
  v.IsArmed := '1';
elsif TrigMasked_v = '1' then
  v.IsArmed := '0';
end if;
```

引自 [hdl/psi_ms_daq_input.vhd:305-312](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L305-L312)。

说明：
- 第 306 行：模式切换（`r.ModeReg /= Mode_Sync`）、或处于 Continuous、或处于 Manual 时，`IsArmed` 强制为 0（这三者都不依赖 `IsArmed` 做触发屏蔽）。
- 第 308–309 行：Arm 脉冲（已寄存的 `r.ArmReg`）置 1。
- 第 310–311 行：有效触发 `TrigMasked_v` 清 0——保证「一次武装只吃一次触发」。

Enable Recording Logic：

```vhdl
-- Enable Recording Logic
case r.ModeReg is
  when RecMode_Continuous_c |
         RecMode_TriggerMask_c =>
    -- always enabled
    v.RecEna := '1';
  when RecMode_SingleShot_c |
         RecMode_ManuelMode_c =>
    -- enable on arming (disable happens after recording)
    if v.ArmReg = '1' then
      v.RecEna := '1';
    end if;
  when others => null;
end case;
if r.ModeReg /= Mode_Sync then
  v.RecEna := '0';
end if;
```

引自 [hdl/psi_ms_daq_input.vhd:314-330](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L314-L330)。

说明：
- 第 316–319 行：Continuous / TriggerMask 每拍把 `RecEna` 置 1（注释「always enabled」）。这就是 TriggerMask「帧结束后立刻录下一帧」的根源。
- 第 320–325 行：SingleShot / Manual 只在 `v.ArmReg='1'`（注意这里用的是 `v.ArmReg`，即武装当拍）置 1，其余拍续住当前值；注释明确「disable happens after recording」——清 0 发生在 4.2 的触发处理里。
- 第 328–330 行：模式切换时无条件清 0。

> 细节对照：Arming Logic 用 `r.ArmReg`（已寄存值），Enable Recording Logic 用 `v.ArmReg`（本拍新值）。这让「武装」与「武装后开始允许录制」基本对齐到同一拍附近，Manual 模式下 `TrigMasked_v`（用 `r.ArmReg`）与 `RecEna` 的配合因此自洽。

复位行为还要看 `p_seq`（[hdl/psi_ms_daq_input.vhd:340-359](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L340-L359)），其中 `Str_Rst=1` 时 `IsArmed`、`RecEna`、`ArmReg`、`PostTrigCnt` 等都会被清零。

#### 4.3.4 代码实践

**目标**：在源码中定位「TriggerMask 与 SingleShot 停止记录条件不同」的根因。

**操作步骤**：
1. 在 [hdl/psi_ms_daq_input.vhd:314-330](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L314-L330) 找到「Enable Recording Logic」。
2. 假设一次触发刚在 4.2 的逻辑里把 `RecEna` 清成了 0（`v.RecEna := '0'`）。
3. 问自己：下一拍 `p_comb` 再跑一次时，TriggerMask 与 SingleShot 分别会把 `RecEna` 置成什么？

**预期结果**：
- TriggerMask 命中第 316–319 行 `when RecMode_Continuous_c | RecMode_TriggerMask_c`，`v.RecEna := '1'`——下一拍立刻恢复录制。
- SingleShot 命中第 320–325 行，只有 `v.ArmReg='1'` 才置 1；此时没有新的 Arm，所以 `RecEna` 保持 0——录制停止。

结论：**TriggerMask 的「停止」只是结束一帧；SingleShot 的「停止」是真正停止录制，直到再次 Arm。**

> 本实践为源码阅读型，无需运行即可得出结论。

#### 4.3.5 小练习与答案

**练习 1**：为什么 Continuous 模式下 `IsArmed` 永远是 0，但触发照样能工作？

**答案**：因为 Continuous 的 `TrigMasked_v := Str_Trig`（4.1.3），根本不读 `IsArmed`；`IsArmed` 在 Continuous 模式被强制清 0（第 306 行）正是因为它不被使用。

**练习 2**：软件在运行中把模式从 TriggerMask 切换成 SingleShot，`RecEna` 会立刻变成什么？

**答案**：会立刻变成 0。第 328–330 行 `if r.ModeReg /= Mode_Sync then v.RecEna := '0'` 在任何模式切换时强制清录制，确保新模式从一个干净的「未录制」状态开始；之后只有发 Arm 才会重新允许录制。

**练习 3**：Manual 模式下，「武装」和「触发」是同一件事吗？

**答案**：是的。Manual 模式 `TrigMasked_v := r.ArmReg`（4.1.3），Arm 脉冲既是「开始允许录制」的信号（4.3.3 第 323 行），又是「触发源」本身。所以一次 Arm 就同时完成武装与触发，记录 \( 1+N \) 个样本后停止，直到下一次 Arm。

## 5. 综合实践：TriggerMask 与 SingleShot 时序对比

本任务把本讲三个最小模块串起来。**目标**：用两张时序图说明 Arm 脉冲、`Str_Trig`、`PostTrigCnt` 与最终写入 FIFO 的 `IsTrig`/`Last` 之间的关系，并指出 TriggerMask 与 SingleShot 在「停止记录条件」上的差异。

### 公共配置

- `StreamWidth_g = 16`，`IntDataWidth_g = 64` ⇒ `WconvFactor_c = 4`（4 样本/字）
- `PostTrigSpls = 3` ⇒ 后触发窗口 3 个样本

### 场景 A：TriggerMask 模式

激励（参照 `case_modes` 的 TriggerMask 段，略作简化）：

1. 软件写 `Mode = TriggerMask`，送 `Arm` 脉冲 ⇒ `IsArmed: 0→1`、`RecEna` 保持 1。
2. 送第一组数据：样本 `S0`(带 `Str_Trig`)、`S1`、`S2`、`S3`。

请画出（或用文字表格表示）下列信号逐拍的变化：`Str_Vld`、`Str_Trig`、`IsArmed`、`TrigMasked_v`、`PostTrigCnt`、`WordCnt`、`DataFifoIsTrig`、写入 FIFO 的字（标出 `Bytes` 与 `Last`）。

**关键现象预期**：
- `S0` 拍：`TrigMasked_v = Str_Trig and IsArmed = 1`，`PostTrigCnt` 由 0 装载为 3；`IsArmed` 被清 0（一次武装一次触发）。
- `S1`、`S2`、`S3` 拍：`PostTrigCnt` 依次 3→2→1→0；`S3` 拍 `PostTrigCnt` 从 1 减到 0，置 `DataFifoIsTrig=1`，写一个带 `IsTrig=1`、`Last=1` 的字结束帧。
- 记录总样本 = 触发样本 + 3 后触发 = 4 个。
- **帧结束后下一拍**：因为 TriggerMask 走第 316–319 行，`RecEna` 立即回到 1，**继续录制**。若紧接着再来数据且没有新的 Arm，这些数据会被记录成一帧（且因为没有重新 Arm，`IsArmed=0`，期间任何 `Str_Trig` 都被屏蔽），最终靠超时（下一讲 u2-l4）封口。

### 场景 B：SingleShot 模式

激励：

1. 软件写 `Mode = SingleShot`，送 `Arm` 脉冲 ⇒ `IsArmed: 0→1`、`RecEna: 0→1`。
2. 送数据：`P0`、`P1`(带 `Str_Trig`)、`P2`、`P3`、`P4`。

请画出与场景 A 相同的信号序列。

**关键现象预期**：
- `P0` 拍：预触发样本，`PostTrigCnt=0`，正常进入移位寄存器。
- `P1` 拍：`TrigMasked_v=1`，`PostTrigCnt` 装载为 3，`IsArmed` 清 0。
- `P2`、`P3`、`P4` 拍：`PostTrigCnt` 3→2→1→0；`P4` 拍结束帧，写带 `IsTrig=1`、`Last=1` 的字。
- 记录总样本 = 预触发 1（`P0`）+ 触发样本 1（`P1`）+ 后触发 3（`P2`–`P4`）= 5 个。
- **帧结束后下一拍**：SingleShot 走第 320–325 行，没有新 Arm ⇒ `RecEna` 保持 0，**录制真正停止**。即使紧接着再来数据，也不会进入 FIFO（4.2 外层 `if r.RecEna='1'` 不成立，4.3 的「Process input data」段也受 `r.RecEna='1'` 约束）。

### 需要你回答的差异

把两张图并排放，圈出「帧结束那一拍之后」的区域：

| 问题 | TriggerMask | SingleShot |
|---|---|---|
| 帧结束后 `RecEna` 下一拍取值 | 1（第 316–319 行强制） | 0（无新 Arm，续住清零值） |
| 后续无 Arm 的数据会进 FIFO 吗 | 会，连续记录成新帧 | 不会，录制已停 |
| 后续 `Str_Trig` 会生效吗 | 不会（`IsArmed=0`，需重新 Arm） | 不会（且 `RecEna=0`，根本不录） |
| 「停止记录」的真实含义 | 不存在，只是「结束一帧」 | 真正停止，直到下一次 Arm |

### 验证方式

把你的时序图与仿真用例对照：
- TriggerMask：[tb/psi_ms_daq_input/psi_ms_daq_input_tb_case_modes.vhd](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_input/psi_ms_daq_input_tb_case_modes.vhd) 的 `CheckAcqData(5+4, 2, ..., FrameType_Trigger_c, ...)` 与随后的 `CheckAcqData(7, 3, ..., FrameType_Timeout_c, ...)`——后者正是「帧结束后继续录、无新 Arm、靠超时封口」的证据。
- SingleShot：同文件 `CheckAcqData(6, 2, ..., FrameType_Trigger_c, ..., false, 5)`，其后 `StdlCompare(0, Daq_Vld, "Unexpected data is available")`——这正校验了「帧结束后无数据可用」，即录制已停。

> 如需观察真实波形，可在 PsiSim/Modelsim 中跑 `modes` 用例（见 u1-l2 的 `sim/run.tcl` 流程），具体波形「待本地运行验证」。

## 6. 本讲小结

- `TrigMasked_v` 是「经模式屏蔽后的有效触发」：Continuous 原样放行 `Str_Trig`；TriggerMask/SingleShot 要求 `Str_Trig and IsArmed`；Manual 直接把 `r.ArmReg` 当触发。未处理拍里的触发会被 `TrigLatch` 锁存，不丢。
- 后触发计数器 `PostTrigCnt` 在触发时装载 `PostTrigSpls`，此后每接收一个样本减 1；减到 0 的那一拍给当前字打 `DataFifoIsTrig=1`、写 FIFO 并清 `RecEna`。自触发起记录 \( 1+N \) 个样本。`PostTrigSpls=0` 时触发即结束。
- `DataFifoIsTrig` 随字写入数据 FIFO，在输出侧还原为 `Daq_Data.IsTrig`，并与 `IsTo` 合成 `Daq_Data.Last`，标记一帧的结束。
- `IsArmed` 由 Arm 置位、由触发清零（一次武装一次触发），在 Continuous/Manual/模式切换时被强制清 0。
- `RecEna` 决定样本能否进入 FIFO 与后触发计数：Continuous/TriggerMask 每拍强置 1；SingleShot/Manual 仅在 Arm 拍置 1，触发结束后保持 0。
- **核心差异**：TriggerMask 触发后只「结束一帧」、立刻继续录；SingleShot 触发后「真正停止录制」直到下一次 Arm。

## 7. 下一步学习建议

本讲只讲了「触发如何结束一帧」，但留了两个扣子：

1. **如果一直不来触发，挂在一个未拼满的字里怎么办？** 这就是超时（Timeout）机制——它用 `DataFifoIsTo` 标志把残余数据冲刷成完整字。请继续学习 **u2-l4（输入逻辑：超时、时间戳与跨时钟域状态）**，那里会讲清 `TimeoutCnt`、`ToDisable`/`FrameTo` 与 `IsTo` 的关系，以及 `Last = IsTo or IsTrig` 的另一半来源。
2. **`Daq_Data.Last` 之后数据去了哪里？** 数据 FIFO 输出侧的 `Daq_Vld`/`Daq_Data` 会被 DMA 引擎消费。学完 u2-l4 后，可以进入 **u2-l5（DMA 引擎接口与缓存结构）** 看 `Last` 标志如何驱动 DMA 把一帧数据搬进内存。
3. 想验证本讲推演的读者，可先跳到 **u5-l1（测试平台结构与 PsiSim 仿真流程）** 学会跑 `modes` 用例，再回来对照波形。
