# 仿真测试平台架构与公共过程

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `top_tb` 顶层测试平台是如何用**两个独立时钟**（160 MHz 数据域 + 125 MHz AXI 域）驱动被测器件（DUT）的，并理解为什么要用双时钟。
- 读懂 `top_tb` 里 AXI master/slave 记录类型（`axi_ms_r` / `axi_sm_r`）如何把几十根 AXI 信号打包成两条信号，简化 DUT 实例化。
- 掌握 `top_tb_pkg` 中四个公共过程的作用：`InputSamples` / `InputSamplesNoCh`（生成数据与触发激励）、`CheckData` / `CheckDataNoCh`（经 AXI 回读并自动比对）。
- 能推导样本值由「PatternCnt_v 计数器 + 通道偏移」组合而成的原因，并解释 `CheckData` 中期望值公式 `ExpVal_v = ch*2**(W-3) + spl + startValue` 的每一项来源。

本讲是整个 u6（验证、集成与二次开发）单元的基石：后续 u6-l2 会逐一拆解六个 case，而所有 case 的激励与校验都建立在本讲的四个公共过程之上。

## 2. 前置知识

本讲默认你已经掌握以下概念（它们在前置讲义中已建立，这里只做最小回顾）：

- **两进程法与四级流水线（u3-l3）**：`data_rec` 核心用 `p_comb`/`p_seq` 两进程，数据从 `In_Data` 到存储写端口要经过 Stage0→3 共 3 拍延迟。测试平台在发完数据后总会留出足够的等待时间，正是为了等满这几拍流水。
- **寄存器与存储地址地图（u2-l2）**：寄存器在 `0x0000`–`0x0030`，存储区从 `Mem_Addr_c = 0x0080` 开始；函数 `MemAddr(ch, spl, depth) = 0x80 + (ch·2^log2ceil(depth) + spl)·4` 给出任意通道/样本的字节地址。本讲的 `CheckData` 正是用它来逐样本回读。
- **PsiSim 回归仿真框架（u1-l3）**：仿真由 `sim/run.tcl` 驱动，`config.tcl` 把 `top_tb` 以两组 `MemoryDepth_g`（32 与 30）各编译运行一次；CI 用 `ciFlow.py` 扫描日志中的 `###ERROR###` 标记判定成败。这个 `###ERROR###` 标记正是由本讲的 `axi_single_expect` 在「期望值 ≠ 实际值」时打印的。

还需要了解一个来自外部库 `psi_tb`（依赖项，不在本仓库内）的工具：

- **`axi_single_write(addr, data, ms, sm, aclk)`**：在 AXI 总线上向 `addr` 写一个 32 位字 `data`。
- **`axi_single_expect(addr, expected, ms, sm, aclk, msg, ...)`**：在 AXI 总线上从 `addr` 读一个 32 位字，与 `expected` 比较，不相等则打印 `###ERROR###` 并附带 `msg`。可选尾部参数用于位掩码与误差容忍（case0 中检查 `DoneTime` 时就用了容忍范围）。
- **`axi_ms_r` / `axi_sm_r`**：`psi_tb_axi_pkg` 定义的两个 record 类型，把 AXI master 侧（AR/AW/W 通道的 valid、地址、数据等）与 slave 侧（R/B 通道的 ready、数据、响应等）的所有信号各打包成**一条**信号。本仓库的测试平台就用它们把几十根 AXI 线压成 `axi_ms`、`axi_sm` 两根，再在 DUT port map 里逐字段展开。

> 小贴士：VHDL 标识符**大小写不敏感**，所以过程体里写的 `wait until rising_edge(Clk)` 引用的其实就是参数 `clk`，二者是同一个信号，不是 bug。

## 3. 本讲源码地图

本讲只涉及两个文件，它们一起构成「测试平台主结构 + 公共工具库」：

| 文件 | 作用 |
| --- | --- |
| [testbench/top_tb/top_tb.vhd](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb.vhd) | 顶层测试平台实体。声明 AXI 信号、数据/触发记录、双时钟进程，实例化 DUT `data_rec_vivado_wrp`，并用 `p_control` 进程按顺序调度六个 case。 |
| [testbench/top_tb/top_tb_pkg.vhd](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_pkg.vhd) | 测试平台公共包。定义全局常量（时钟频率、通道数、位宽）、共享变量、记录类型 `In_t`/`Out_t`/`Data_t`，以及四个公共过程 `InputSamples`/`InputSamplesNoCh`/`CheckData`/`CheckDataNoCh`。 |

另外会引用到（已在 u2-l2 讲过）：

| 文件 | 本讲用到的部分 |
| --- | --- |
| [hdl/data_rec_register_pkg.vhd](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd) | `MemAddr` 函数与 `Mem_Addr_c` 常量，供 `CheckData` 计算回读地址。 |

## 4. 核心概念与源码讲解

本讲拆为三个最小模块：4.1 测试平台主结构（双时钟 + DUT）、4.2 激励生成过程、4.3 数据校验过程。

### 4.1 top_tb：双时钟驱动与 DUT 实例化

#### 4.1.1 概念说明

`data_rec` 是一个**双时钟域** IP（数据域 `Clk` 与 AXI 域 `s00_axi_aclk`，详见 u2-l1、u5-l2）。要真实地验证它，测试平台必须也用**两个独立、频率不同**的时钟分别驱动这两个域，否则跨时钟域（CDC）逻辑永远得不到充分暴露。`top_tb` 正是这么做的：用 160 MHz 驱动数据采集，用 125 MHz 驱动 AXI 总线访问，两时钟异步，逼出真实的亚稳态与同步路径行为。

#### 4.1.2 核心流程

`top_tb` 的架构 `sim` 由四部分组成：

1. **声明 AXI 与数据信号**：用 `axi_ms_r`/`axi_sm_r` 两条 record 信号表达整条 AXI 总线；用自定义记录 `In_t`（数据+有效+触发）、`Out_t`（中断+触发转发）表达 DUT 的算法侧端口。
2. **实例化 DUT** `data_rec_vivado_wrp`，把 generics（`InputWidth_c=16`、`NumOfInputs_c=4`、`MemoryDepth_g`、`TriggerInputs_c=4`、`TrigForwarding_g=true`）和两组时钟/复位、数据、触发、AXI 信号连上。
3. **两个时钟进程** `p_aclk`（125 MHz）与 `p_pclk`（160 MHz），靠 `TbRunning` 标志驱动，测试结束后停振。
4. **控制进程** `p_control`：先复位，再依次调用六个 case 的 `run()` 过程，最后令 `TbRunning <= false` 收尾。

```text
            ┌──────────── top_tb (architecture sim) ────────────┐
 p_pclk ──▶ Clk (160 MHz) ─────────────┐
 p_aclk ──▶ aclk (125 MHz) ────────────┤
 aresetn/Rst (复位) ───────────────────┤
                                       ▼
   ToDut (In_t: In_Data, In_Vld, Trig_In) ──▶  data_rec_vivado_wrp (DUT)
   axi_ms (AXI master record) ──────────────▶        │
   axi_sm (AXI slave  record) ◀──────────────        │
       ▲                                              │
       │   FromDut (Out_t: Done_Irq, Trig_Out) ◀─────┘
 p_control: reset → case0.run → ... → case5.run → stop clocks
```

#### 4.1.3 源码精读

**实体与 generic**：`top_tb` 只有一个 generic `MemoryDepth_g`（默认 32），CI 会分别用 32 与 30 实例化它跑两次（见 u1-l3）。见 [testbench/top_tb/top_tb.vhd:27-31](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb.vhd#L27-L31)。

**AXI 信号用 record 打包**：下面这段把整条 AXI 总线压成 `axi_ms`（master 侧：arid/araddr/awaddr/wdata/wstrb 等）与 `axi_sm`（slave 侧：rid/rdata/bresp 等）两根信号，宽度由前面的常量（`ADDR_WIDTH=14`、`DATA_WIDTH=32` 等）约束。见 [testbench/top_tb/top_tb.vhd:51-59](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb.vhd#L51-L59)。这样在 case 里只需把 `axi_ms`/`axi_sm` 整体传给 `axi_single_write`/`axi_single_expect`，无需逐根线操作。

**时钟常量**：AXI 域频率写在 `top_tb` 本体里，为 125 MHz。见 [testbench/top_tb/top_tb.vhd:70-71](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb.vhd#L70-L71)。

> 注意：数据域频率 `ClockFrequency_c = 160.0e6` 写在 `top_tb_pkg` 里（见 4.2.3），通过 `ClockPeriod_c` 反过来被 `p_pclk` 使用。两个频率分居两个文件，但都被本架构引用。

**算法侧信号用记录 `In_t`/`Out_t` 表达**：`ToDut` 打包了要喂给 DUT 的数据、有效和触发；`FromDut` 打包了 DUT 输出的中断与触发转发。`DataDut` 是一个 8 元数组，`g_data` generate 只把前 `NumOfInputs_c` 路接到 DUT 的 `In_Data0..3`。见 [testbench/top_tb/top_tb.vhd:83-93](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb.vhd#L83-L93)。

**DUT 实例化**：把 generics 与全部端口连上；AXI 五通道的每一根线都在 port map 里从 `axi_ms`/`axi_sm` 字段展开。`TrigForwarding_g => TRIG_FWD`（`TRIG_FWD = true`）使能 `Trig_Out` 端口，这样 `FromDut.Trig_Out` 才有信号可监测。见 [testbench/top_tb/top_tb.vhd:98-168](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb.vhd#L98-L168)。

**双时钟进程**：`p_aclk` 产 125 MHz 的 `aclk`，`p_pclk` 产 160 MHz 的 `Clk`；两者都靠 `while TbRunning loop` 持续翻转，测试结束后 `wait;` 永久挂起以停振。见 [testbench/top_tb/top_tb.vhd:173-195](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb.vhd#L173-L195)。

**控制进程 `p_control`**：先令 `MemoryDepth_v := 30`（设置 `CheckData` 计算 `MemAddr` 时用的深度，见 4.3），再做复位（`aresetn='0'`、`Rst='1'`，等 1 µs 后在 `aclk` 上升沿释放），然后依次调用 `case0..case5` 的 `run()`，最后 `TbRunning <= false` 停钟。见 [testbench/top_tb/top_tb.vhd:200-234](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb.vhd#L200-L234)。每个 case 的 `run()` 签名略不同（case2~5 多带一个 `FromDut`，用于监测 `Trig_Out`/`Done_Irq`），但都接收 `axi_ms`/`axi_sm`/`aclk`/`Clk`/`ToDut`。

#### 4.1.4 代码实践

**实践目标**：确认双时钟真的频率不同、且独立运行。

**操作步骤**：

1. 打开 `testbench/top_tb/top_tb.vhd`，找到 `p_aclk` 与 `p_pclk` 两个进程。
2. 计算 `ClockPeriodAxi_c`（125 MHz）与 `ClockPeriod_c`（160 MHz）的半周期，分别是多少纳秒。
3. 在 `p_control` 的复位释放后、第一个 case 调用前，想象你加一行 `wait until rising_edge(Clk); wait until rising_edge(aclk);`，思考这两个时钟沿是否会对齐。

**需要观察的现象 / 预期结果**：

- 125 MHz 半周期 ≈ 4 ns，160 MHz 半周期 ≈ 3.125 ns。两周期之比是 8:6.25，**不可公约**，所以两时钟的上升沿永远不会周期性对齐——这正是异步时钟域的真实写照，也是本测试平台要用双时钟的原因。
- 待本地验证：若你在 Modelsim 里跑 `interactive.tcl`（只编译不自动运行，见 u1-l3）并手动 `run 1 us`，应在波形窗口看到 `Clk` 与 `aclk` 不断相对滑动。

#### 4.1.5 小练习与答案

**练习 1**：为什么测试平台用两个频率「故意不同」的时钟，而不是两个相同频率的时钟？

> **答案**：相同频率且同相的两时钟沿会周期性对齐，跨时钟域同步器（`status_cc`/`pulse_cc`，见 u5-l2）采样到的总是稳定值，掩盖了亚稳态风险与数据撕裂问题；频率不同且不可约，才能在统计上覆盖各种采样相位，真正压测 CDC 逻辑。

**练习 2**：`MemoryDepth_g`（实体 generic，默认 32）与 `MemoryDepth_v`（`p_control` 里设为 30 的共享变量）分别用在哪？

> **答案**：`MemoryDepth_g` 传给 DUT，决定其内部存储深度与地址位宽（见 u3-l1）；`MemoryDepth_v` 只用在 `CheckData`/`CheckDataNoCh` 里，作为 `MemAddr` 函数的 `memdepth` 参数来计算回读地址（见 4.3.3）。两者必须语义一致，否则回读地址会算错。

### 4.2 激励生成：InputSamples 与 InputSamplesNoCh

#### 4.2.1 概念说明

要验证一个数据记录器，最繁重的工作是「造数据」：得在数据时钟域里一拍一拍地给出 `In_Data`、拉高/拉低 `In_Vld`、在指定时刻发外部触发，并且让每个通道的数据**可预测、可回算**。`InputSamples` 把这件事封装成一个过程：用一个全局递增的计数器 `PatternCnt_v` 当样本基底，再用「通道号写入最高 3 位」的方式叠加通道偏移，使得**每个通道每个样本的值都唯一且可公式化推导**——这正是后面 `CheckData` 能用闭式公式给出期望值的原因。

`InputSamplesNoCh` 是它的「有符号、通道间小幅偏移」变体，用于自触发测试（u4-l4）：通道之间不再是「高 3 位不同」的大跳变，而是按 `chStep` 做小幅 signed 偏移，便于构造跨零点、范围进入/退出等场景。

#### 4.2.2 核心流程

`InputSamples(samples, inp, clk, startCnt, trigAt, dutycycle, trigIdx)` 的执行逻辑（伪代码）：

```text
若 startCnt >= 0：把全局 PatternCnt_v 重置为 startCnt   # 否则沿用上次的值（连续计数）
等待 clk 上升沿
for cnt in 0 .. samples-1:
    Data  := to_unsigned(PatternCnt_v, W)              # 基底 = 计数器值
    PatternCnt_v += 1
    for d in 0 .. NumOfInputs_c-1:                     # 给每个通道叠加偏移
        Data 的高 3 位 := d                              # 通道号写入最高 3 位
        inp.In_Data(d) <= Data
    若 cnt == trigAt:  inp.Trig_In(trigIdx) <= '1'      # 在指定样本上注入外部触发
    等待 (dutycycle-1) 个额外 clk 沿                    # dutycycle>1 制造断流(间隙)
    inp.In_Vld <= '1'; 等一个 clk 沿; inp.In_Vld <= '0' # 一个有效样本
    若 trigAt 被用过: inp.Trig_In <= 全 0               # 触发只维持一拍
```

关键点：

- **基底计数器 `PatternCnt_v` 是 `shared variable`**，跨多次 `InputSamples` 调用持续累加；只有显式传 `startCnt >= 0` 才会重置。这让 case 可以「先发若干前触发样本，再发后触发样本」而样本值自然连续。
- **通道偏移靠「覆写最高 3 位」**实现，而不是相加。下面 4.2.4 会证明它在 `PatternCnt_v < 2^(W-3)` 时等价于加法。
- **`dutycycle`** 控制数据有效占空比：默认 1 表示每拍都有效（连续流）；>1 则在有效拍之间插入空洞，用来验证「只有 `In_Vld=1` 的样本才被记录/计数」（见 u3-l4 的 `In_Vld` 门控）。
- **`trigAt` / `trigIdx`** 在第 `trigAt` 个样本上、向第 `trigIdx` 路外部触发发一个单拍脉冲，用于精确控制触发时刻与触发路号（u4-l2 多路 OR 测试会用到）。

#### 4.2.3 源码精读

**全局常量与共享变量**：数据时钟 160 MHz、`InputWidth_c=16`、`NumOfInputs_c=4`、`TriggerInputs_c=4`；`PatternCnt_v` 与 `MemoryDepth_v` 是 `shared variable`（可被多个进程/过程读写）。见 [testbench/top_tb/top_tb_pkg.vhd:27-41](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_pkg.vhd#L27-L41)。

**记录类型**：`Data_t` 是每路 `InputWidth_c` 位的数组；`In_t` 把数据/有效/触发打包，`Out_t` 把 `Done_Irq`/`Trig_Out` 打包。见 [testbench/top_tb/top_tb_pkg.vhd:46-57](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_pkg.vhd#L46-L57)。

**`InputSamples` 主体**：注意「基底取计数器 + 通道号写入高 3 位 + 可选触发 + 占空比插入 + 单拍有效」这几步。通道偏移那一行是本讲的焦点之一。见 [testbench/top_tb/top_tb_pkg.vhd:95-132](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_pkg.vhd#L95-L132)，其中通道偏移在 [testbench/top_tb/top_tb_pkg.vhd:113-116](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_pkg.vhd#L113-L116)：

```vhdl
Data := to_unsigned(PatternCnt_v, InputWidth_c);   -- 基底
...
for d in 0 to NumOfInputs_c-1 loop
    Data(Data'left downto Data'left-2) := to_unsigned(d, 3);  -- 高 3 位写通道号
    inp.In_Data(d) <= std_logic_vector(Data);
end loop;
```

**`InputSamplesNoCh` 主体**：用 `signed` 算术，通道间按 `chStep` 做小幅偏移 `ChData := Data + chStep*d`，计数器按 `cntStep` 递增。见 [testbench/top_tb/top_tb_pkg.vhd:150-177](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_pkg.vhd#L150-L177)。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：搞清楚样本值如何由 `PatternCnt_v` 与通道偏移组合而成，为 4.3 的期望值公式打下基础。

**操作步骤（源码阅读型实践）**：

1. 打开 [testbench/top_tb/top_tb_pkg.vhd:108-116](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_pkg.vhd#L108-L116)。
2. 设 `InputWidth_c = W = 16`，`PatternCnt_v` 当前为某个值 `P`。回答：
   - `to_unsigned(P, 16)` 的最高 3 位（bit 15 downto 13）在什么条件下全为 0？
   - 当最高 3 位全为 0 时，把最高 3 位改成通道号 `d`，得到的整数值是多少？

**推导（关键结论）**：

- 一个 16 位无符号数 `P`，其最高 3 位全为 0 当且仅当 \(P < 2^{13} = 8192\)。
- 此时把最高 3 位（bit 15..13）写成 `d`（占 3 位），新值 = 原低 13 位 + \(d \cdot 2^{13}\)。因为原高 3 位是 0，这次「覆写」就等价于「相加」：

\[
\text{value}(d, P) \;=\; P \;+\; d \cdot 2^{W-3} \quad (\text{当 } P < 2^{W-3})
\]

- 本仓库所有测试的样本量都远小于 8192（`MemoryDepth_v` 才 30），所以该等价恒成立。**这正是 `CheckData` 里期望值公式 `ch*2**(W-3) + ...` 的来源**：通道项 `ch*2**(W-3)` 来自「通道号写进高 3 位」。

**预期结果**：例如 `PatternCnt_v = 5`（即 `P=5`）、`W=16` 时，通道 0/1/2/3 的样本值分别是 \(5 + 0\cdot 8192 = 5\)、\(5 + 8192 = 8197\)、\(5 + 16384 = 16389\)、\(5 + 24576 = 24581\)。通道间固定相差 8192。

#### 4.2.5 小练习与答案

**练习 1**：若把 `InputWidth_c` 从 16 改成 8，`CheckData` 公式里的 `2**(InputWidth_c-3)` 会变成多少？通道间偏移还是 8192 吗？

> **答案**：会变成 \(2^{8-3} = 2^5 = 32\)。通道间偏移变成 32，不再是 8192。同时「基底 < 偏移」的安全阈值也从 8192 降到 32——这意味着样本数不能超过 32，否则高 3 位会被计数器自己占用，覆写不再等价于加法，期望值公式就会失效。这正是该公式隐含的约束。

**练习 2**：`InputSamples` 里 `for w in 0 to dutycycle-2 loop wait until rising_edge(Clk);` 这段，当 `dutycycle=1` 时执行几次？为什么需要它？

> **答案**：`0 to -1` 区间为空，执行 0 次。它的作用是当 `dutycycle>1`（如 2）时，在两个有效样本之间插入一个无效拍，制造断流数据，用来验证记录器只在 `In_Vld=1` 时计数/写入（见 u3-l4 的 `In_Vld(1)` 门控）。默认 `dutycycle=1` 即连续流。

### 4.3 数据校验：CheckData 与 CheckDataNoCh

#### 4.3.1 概念说明

数据写进去之后，要验证「写到的地址」和「读出来的值」都对。`CheckData` 做两件事：第一，用 `MemAddr(ch, spl, depth)` 算出第 `ch` 通道第 `spl` 个样本的**线性**字节地址，经 AXI 读出来；第二，用闭式公式给出该样本的期望值，交给 `axi_single_expect` 自动比对——不一致就打印 `###ERROR###`，这正是 CI 判定失败的信号源（见 u1-l3）。

之所以能只用「一个公式」就把整段波形的期望值写完，完全得益于 4.2 的激励构造方式：写入值 = `PatternCnt_v`（计数器）+ 通道偏移；而录制窗口内第 `spl` 个样本对应的 `PatternCnt_v` 恰好 = `spl + startValue`，其中 `startValue` 是「窗口里第一个样本」的计数器值。于是期望值是一行算术表达式，无需逐样本手填。

`CheckDataNoCh` 对应 `InputSamplesNoCh`：期望值 `ch*chStep + spl*cntStep + startValue`，分别匹配通道间偏移 `chStep` 与计数步进 `cntStep`。

#### 4.3.2 核心流程

`CheckData(samples, startValue, ms, sm, aclk)` 的逻辑：

```text
for ch in 0 .. NumOfInputs_c-1:
    for spl in 0 .. samples-1:
        ExpVal_v := ch*2**(W-3) + spl + startValue          # 闭式期望值
        axi_single_expect( MemAddr(ch, spl, MemoryDepth_v),  # 线性地址（字节）
                           ExpVal_v, ms, sm, aclk, "Data Ch.. Spl .." )
```

注意它检验的是**完整数据通路**：核心环形写入 → 封装层 `FirstSplAddr` 把环形对齐成线性（见 u3-l5、u5-l3）→ 每通道双口 RAM 读出与符号扩展 → AXI 单字读。只要这条链上任何一环算错地址或数据，`axi_single_expect` 都会报 `###ERROR###`。

#### 4.3.3 源码精读

**`MemAddr` 函数**：把「线性通道号 + 线性样本号」映射成字节地址，通道间距向上取整到二次幂（`2**log2ceil(memdepth)`），这样地址的高位可直接当通道选择。见 [hdl/data_rec_register_pkg.vhd:80-86](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd#L80-L86)，存储区起点 `Mem_Addr_c = 0x0080` 见 [hdl/data_rec_register_pkg.vhd:60](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd#L60)。

**`CheckData` 主体**：双重循环逐通道逐样本回读，期望值用闭式公式。见 [testbench/top_tb/top_tb_pkg.vhd:135-148](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_pkg.vhd#L135-L148)，期望值那一行在 [testbench/top_tb/top_tb_pkg.vhd:144-145](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_pkg.vhd#L144-L145)：

```vhdl
ExpVal_v := ch*2**(InputWidth_c-3)+spl+startValue;
axi_single_expect(MemAddr(ch, spl, MemoryDepth_v), ExpVal_v, ms, sm, aclk,
                  "Data Ch" & integer'image(ch) & " Spl " & integer'image(spl));
```

**`CheckDataNoCh` 主体**：期望值 `ch*chStep + spl*cntStep + startValue`，与 `InputSamplesNoCh` 的 `Data + chStep*d`、`PatternCnt_v += cntStep` 严格对偶。见 [testbench/top_tb/top_tb_pkg.vhd:179-194](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_pkg.vhd#L179-L194)。

**一次真实调用的串联（来自 case0）**：case0 先 `InputSamples(PreTrigger_c+UnusedSamples, ToDut, Clk, 0)` 连续发样本（`startCnt=0`），中间穿插触发与状态检查，录制完成后用 `CheckData(Samples_c, UnusedSamples, ms, sm, aclk)` 校验——注意这里 `startValue = UnusedSamples = 4`。见 [testbench/top_tb/top_tb_case0_pkg.vhd:89](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case0_pkg.vhd#L89) 与 [testbench/top_tb/top_tb_case0_pkg.vhd:147](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case0_pkg.vhd#L147)。

#### 4.3.4 代码实践（承接 4.2.4）

**实践目标**：解释期望值公式 `ExpVal_v = ch*2**(W-3) + spl + startValue` 中每一项的来源，并弄清 `startValue` 为什么等于 case0 里的 `UnusedSamples`。

**操作步骤（源码阅读 + 手算）**：

1. 结合 4.2.4 的结论，公式中 `ch*2**(W-3)` 来自「通道号写进高 3 位」（通道偏移）。
2. 公式中 `spl + startValue` 这一项对应「该样本的基底计数器值 `PatternCnt_v`」。请回答：为什么第 `spl` 个窗口样本的 `PatternCnt_v` 恰好是 `spl + startValue`？
3. 手算：`MemoryDepth_v = 30`、`UnusedSamples = 4`（即 `startValue = 4`）、`W = 16`，求 `CheckData` 在 `ch=1, spl=0` 与 `ch=0, spl=2` 两点的期望值。

**推导**：

- `InputSamples` 里 `PatternCnt_v` 每发一个样本就 +1，且窗口内样本是连续编号的。录制窗口捕获的是「最后 `TotalSpls` 个样本」（环形缓冲，见 u3-l4），但 case0 在前触发阶段**多发了 `UnusedSamples` 个样本**（`PreTrigger_c + UnusedSamples`），这些多余的最早样本会被挤出窗口。因此窗口里**第一个样本**（`spl=0`）对应的计数器值不是 0，而是 `UnusedSamples`。于是第 `spl` 个窗口样本的 `PatternCnt_v = UnusedSamples + spl = startValue + spl`。
- 这就完全解释了公式：**通道偏移（高 3 位）+ 基底计数器（低 13 位）= `ch*2**(W-3) + (spl + startValue)`**。

**预期结果**：

- `ch=1, spl=0`：\(1 \cdot 2^{13} + 0 + 4 = 8196\)。
- `ch=0, spl=2`：\(0 \cdot 2^{13} + 2 + 4 = 6\)。

**若无法本地运行**：以上为静态手算，可在阅读源码时直接验证；如要观察实际 `###ERROR###` 行为，需在 Modelsim 中跑 `sim/run.tcl`（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：如果某次录制把 `PreTrigger_c` 设得很大、前触发阶段发的样本数超过了 8192，`CheckData` 的期望值公式还成立吗？为什么？

> **答案**：不成立。一旦 `PatternCnt_v >= 2^{13}`，计数器自身就会占用最高 3 位，「覆写高 3 位为通道号」就不再等价于「加 `ch*2^{13}`」，公式 `ch*2**(W-3) + spl + startValue` 会与实际写入值不符。该公式隐含「总样本数 < `2**(W-3)`」的约束；这是用高 3 位编码通道号的代价。

**练习 2**：`CheckData` 经 AXI 读地址用的是 `MemAddr(ch, spl, MemoryDepth_v)`（**线性**地址），而核心写 RAM 用的是环形地址。这两者之间的「对齐」由谁完成？

> **答案**：由封装层的 `FirstSplAddr` 机制完成（见 u3-l5、u5-l3）。核心在触发拍算出环形缓冲的起点 `FirstSplAddr` 并输出；封装层读出时把软件给出的线性样本号 `spl` 映射回环形物理地址 `(spl + FirstSplAddr) mod MemoryDepth_g`。`CheckData` 用线性地址读、却读到正确顺序的数据，正说明这条「环→线」对齐链路工作正常——这也是 v2.3.2 修复非二次幂回绕 bug 所保护的关键路径。

## 5. 综合实践

把本讲三个模块串起来，做一个**端到端追踪**任务（源码阅读型，无需运行仿真）：

**任务**：以 case0 的一次录制为例，画出「激励值 → 计数器 → 通道偏移 → 环形写入 → 线性读出 → 期望值比对」的完整数据流，并用具体数字填一张小表。

**步骤**：

1. 读 [testbench/top_tb/top_tb_case0_pkg.vhd:50-53](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case0_pkg.vhd#L50-L53)，确认 `Samples_c = MemoryDepth_v - 4`、`PreTrigger_c = MemoryDepth_v/2`、`UnusedSamples = 4`。
2. 读 [testbench/top_tb/top_tb_case0_pkg.vhd:89](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case0_pkg.vhd#L89) 与 [testbench/top_tb/top_tb_case0_pkg.vhd:147](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case0_pkg.vhd#L147)，看清激励的 `startCnt=0` 与校验的 `startValue=UnusedSamples=4` 是如何对应的。
3. 设 `MemoryDepth_v = 30`，填下表（`W=16`，通道偏移 = `ch*8192`）：

   | ch | spl | PatternCnt_v | 期望值 `ch*8192 + spl + 4` |
   |----|-----|--------------|----------------------------|
   | 0  | 0   | 4            | 4                          |
   | 0  | 1   | 5            | 5                          |
   | 1  | 0   | 4            | 8196                       |
   | 2  | 3   | 7            | 16391                      |
   | 3  | (Samples_c-1) | ?   | ?                          |

4. 解释：为什么 `startValue` 必须等于前触发阶段「多发」的那 `UnusedSamples` 个样本数，否则 `CheckData` 会全报 `###ERROR###`？

**预期结果**：你能用一句话讲清「`InputSamples` 用计数器+通道偏移造数据 → 录制窗口截取最后 `TotalSpls` 个样本 → `CheckData` 用同一套计数器假设反推期望值」这三者为何必须严格对齐；并指出 `startValue` 就是窗口首样本的计数器偏移。最后一行：`Samples_c = 26`，`spl = 25`，`PatternCnt_v = 29`，期望值 `= 3*8192 + 25 + 4 = 24605`。

## 6. 本讲小结

- `top_tb` 用**两个独立、不可约的时钟**（160 MHz 数据域 + 125 MHz AXI 域）真实驱动双时钟域 DUT `data_rec_vivado_wrp`，靠 `p_control` 顺序调度 case0~case5。
- AXI 总线被 `psi_tb` 的 record 类型 `axi_ms_r`/`axi_sm_r` 压成两根信号 `axi_ms`/`axi_sm`，case 内用 `axi_single_write`/`axi_single_expect` 整体操作，无需逐线连接。
- `InputSamples` 用全局递增的 `PatternCnt_v` 当基底、把**通道号写进最高 3 位**叠加通道偏移，并可控制占空比（`dutycycle`）与外部触发注入（`trigAt`/`trigIdx`）。
- `CheckData` 用 `MemAddr(ch, spl, depth)` 经 AXI 逐样本回读，期望值公式 `ch*2**(W-3) + spl + startValue` 直接来自激励构造方式——通道项来自高 3 位编码，`spl + startValue` 是该样本的计数器值。
- `InputSamplesNoCh`/`CheckDataNoCh` 是「signed、通道间小幅偏移」变体，服务于自触发测试；其期望值 `ch*chStep + spl*cntStep + startValue` 与激励严格对偶。
- 该公式隐含约束：总样本数必须 \(< 2^{W-3}\)，否则计数器占用高 3 位、覆写不再等价于相加；同时 `MemoryDepth_g`（DUT）与 `MemoryDepth_v`（地址计算）必须语义一致。

## 7. 下一步学习建议

- **下一步读 u6-l2（六个测试用例的覆盖设计）**：本讲的四个公共过程是所有 case 的「积木」，u6-l2 会逐一说明 case0~case5 各自验证什么功能点（边沿触发、最小录制间隔、自触发跨零点、软件触发 sticky、异常恢复、多路外部触发 OR），并把这些用例与本讲的 `InputSamples`/`CheckData` 调用对应起来。
- **回头印证地址地图**：本讲 `CheckData` 反复用到的 `MemAddr` 来自 u2-l2，若对通道间距取二次幂、存储区起点 `0x0080` 还有疑问，可回看 u2-l2 与 u5-l3（每通道双口 RAM 读出与 `FirstSplAddr` 对齐）。
- **想动手跑仿真**：按 u1-l3 在 `sim/` 目录执行回归仿真，在日志里搜索 `###ERROR###`（由本讲的 `axi_single_expect` 产生）与成功横幅，体会 `ciFlow.py` 的三退出码判定。
