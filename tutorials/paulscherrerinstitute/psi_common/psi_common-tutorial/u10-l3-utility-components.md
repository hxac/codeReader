# 实用组件：看门狗 / 消抖 / 防优化 / 触发器 / 动态移位

## 1. 本讲目标

psi_common 库里有一批「个头不大、但几乎每个 FPGA 工程都会用到」的实用组件。它们不像 FIFO、AXI 那样自成体系，却解决着真实工程中反复出现的零碎问题：按键抖动怎么滤、心跳信号丢了怎么报警、综合工具把我的调试端口优化掉了怎么办、模拟信号越过门限怎么产生一拍触发、按运行时变量做任意位移怎么不拖垮时序。

学完本讲，你应当能够：

- 说清**看门狗（watchdog）**与**消抖器（debouncer）**各自监视的「时间窗」语义，以及二者的本质区别。
- 理解 **dont_opt** 如何只用 4 个物理引脚就骗过综合工具、保住一整片「虚拟 I/O」。
- 掌握模拟/数字**触发器**的「越限/边沿 + arm/disarm」状态机模型。
- 看懂 **dyn_sft** 为什么把一个桶形移位器拆成多级流水线、代价与收益各是什么。
- 会为本讲所有组件挑选正确的 generic 参数（尤其是把「秒/赫兹」换算成「时钟周期数」）。

本讲承接 [u6-l1 选通与节拍生成](u6-l1-strobe-tick-generator.md)：那里建立的「频率↔周期计数比」「边沿检测（打一拍再比较）」是本讲多个组件的共同基础。本讲全部组件也都沿用 [u7-l1](u7-l1-pl-stage.md) 确立的**二进程 record 设计法**（`r`/`r_next` + 组合进程 + 时序进程）。

## 2. 前置知识

在进入源码前，先用三段话把本讲反复出现的几个概念讲透。

**边沿检测与「变化检测」。** 数字信号处理里，我们经常想知道「信号刚刚跳变了吗」。通用做法是先把信号打一拍得到 `sig_dff`，再比较：

- 数字边沿：`sig_dff='0' and sig='1'` → 上升沿；`sig_dff='1' and sig='0'` → 下降沿。
- 向量变化：`dat_i /= dat_dff` → 任意一位发生了变化（看门狗就用它判「事件来了」）。

这一手法在 u6-l1 的 `strobe_divider`、本讲的 watchdog / debouncer / trigger 里反复出现。

**从「时间」到「计数」。** FPGA 里没有「秒」，只有时钟周期。要把一个以秒为单位的物理量（如消抖周期 10 ms）变成计数器初值，靠的是时钟频率：

\[
\text{count} = \lceil\, t_{\text{目标}} \cdot f_{\text{clk}}\,\rceil - 1
\]

例如 \( f_{\text{clk}}=100\,\text{MHz} \)、\( t=10\,\text{ms} \)，则计数约 \( 10\times10^{-3}\times10^{8}=10^{6} \)，即 100 万拍、约 20 位计数器。本讲的 debouncer / watchdog 都在内部做这个换算，调用 `math_pkg` 的 `log2ceil` 自动推导计数器位宽。

**二进程 record 设计法回顾。** 所有寄存器收敛进一个 record（如 `two_process_t`），用 `r` 表示当前态、`r_next` 表示次态；组合进程算 `r_next`、时序进程在时钟沿把 `r_next` 写回 `r` 并处理复位。增删寄存器只动 record 与组合进程。本讲六个组件无一例外都采用此法。

## 3. 本讲源码地图

本讲涉及六个源文件，按「最小模块」归组如下：

| 最小模块 | 源文件 | 一句话作用 |
|---|---|---|
| watchdog / debouncer | [hdl/psi_common_watchdog.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_watchdog.vhd) | 监视 dat_i 是否在规定时间内发生过变化，否则报警 |
|  | [hdl/psi_common_debouncer.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_debouncer.vhd) | 输入稳定满一个消抖周期才放行到输出 |
| dont_opt 防优化 | [hdl/psi_common_dont_opt.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_dont_opt.vhd) | 用 4 个物理引脚保住任意多位 I/O 不被综合优化掉 |
| trigger 模拟/数字 | [hdl/psi_common_trigger_analog.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_trigger_analog.vhd) | 模拟值越过门限时产生单拍触发 |
|  | [hdl/psi_common_trigger_digital.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_trigger_digital.vhd) | 数字信号上升/下降沿产生单拍触发 |
| dyn_sft 动态移位 | [hdl/psi_common_dyn_sft.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_dyn_sft.vhd) | 运行时按 shift_i 做多级流水线桶形移位 |

辅助依赖：debouncer 内部例化 [hdl/psi_common_bit_cc.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_bit_cc.vhd)（两级同步器，详见 u5-l2）；dyn_sft 用 [hdl/psi_common_logic_pkg.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_logic_pkg.vhd) 的 `shift_right`；位宽推导统一用 [hdl/psi_common_math_pkg.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_math_pkg.vhd) 的 `log2ceil` / `ceil` / `choose`。

除 `dont_opt` 外，其余五个组件都有专属自校验测试平台，并已登记到 [sim/config.tcl](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl) 的回归列表中（参见 L142、L145-L150、L434-L464）。

## 4. 核心概念与源码讲解

### 4.1 看门狗与消抖器（watchdog / debouncer）

#### 4.1.1 概念说明

这两个组件都围绕一个「时间窗」工作，但方向相反，初学者最容易混淆，先把区别讲死：

- **消抖器 debouncer**——「**输入必须连续稳定够久，我才相信它**」。机械按键按下/松开时触点会弹跳，产生几毫秒的毛刺。debouncer 在输入每次跳变时把一个倒计数器重载到满量程；只有当输入稳定到计数器数到 0，输出才更新。任何抖动都会重置计数器，于是短毛刺被「耗死」在窗口里，永远到不了输出。它面向**慢变、有噪声的物理输入**。

- **看门狗 watchdog**——「**输入必须定期变化，否则我报警**」。它监视一个本该周期性跳变的信号（心跳、选通、握手脉冲）。每检测到一次变化（`dat_i /= dat_dff`）就把活动计数器清零；若活动计数器数满一个完整周期都没等到变化，就算「漏一次」，累计漏次达到阈值就拉 `warn_o` / `fault_o`。它面向**本该活着、却可能卡死的事件源**。

一句话区分：debouncer 怕输入**变得太快**（滤掉），watchdog 怕输入**变得太慢**（报警）。

#### 4.1.2 核心流程

**watchdog** 流程（伪代码）：

```
每个时钟沿:
  dat_dff <= dat_i                       -- 打一拍
  if dat_i /= dat_dff:                   -- 检测到事件（任意位变化）
      activ_count <= 0                   -- 重置活动计时
  elif activ_count < 一个周期(thld_c):
      activ_count <= activ_count + 1     -- 没事件就继续计时
  else:                                  -- 整整一个周期没事件
      miss_count <= miss_count + 1       -- 记一次漏
      (successive 模式下，来事件会把 succ_count 清零)
  -- miss_count / succ_count 达到阈值 => warn_o / fault_o 置 1（锁存）
```

其中「一个周期」由 generic 换算：

\[
\text{thld\_c} = \lfloor f_{\text{clk}} / f_{\text{act}} \rfloor - 1
\]

\( f_{\text{act}} \) 是期望的事件频率。默认 \( f_{\text{clk}}=100\,\text{MHz}, f_{\text{act}}=100\,\text{kHz} \) 时，thld_c = 999，即每 1000 拍应来一次事件。

**debouncer** 流程（伪代码）：

```
每个时钟沿:
  inp_dff <= inp_sync_s                  -- 打一拍（先经 bit_cc 同步）
  if inp_dff /= inp_sync_s:              -- 输入又跳变了
      counter <= count_max_c             -- 重载满量程（重新开始稳态计时）
  elif counter /= 0:
      counter <= counter - 1             -- 继续倒计数
  if counter = 0:                        -- 已稳定满一个窗口
      output <= inp_dff (按极性)         -- 才把新值放到输出
```

计数满量程：

\[
\text{count\_max\_c} = \lceil\, \text{dbnc\_per\_g} \cdot f_{\text{clk}}\,\rceil - 1
\]

#### 4.1.3 源码精读

**watchdog 的 generic 与周期换算**——`freq_clk_g`/`freq_act_g` 在 elaboration 期算出每个事件折合多少个时钟周期，并推导计数器位宽：

[psi_common_watchdog.vhd:40-43](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_watchdog.vhd#L40-L43) —— `thld_c` 是一个事件周期对应的时钟拍数，`nbit_count0_c` 用 `log2ceil` 自动算活动计数器位宽。

[psi_common_watchdog.vhd:24-38](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_watchdog.vhd#L24-L38) —— 注意 generic 表：`thld_fault_succ_g` 为 `integer`，**取 0** 走「累计漏次」模式，**取正值**走「连续漏次」模式（来一次事件就把连续计数清零）。`miss_o` 的位宽是 `log2ceil(thld_fault_total_g)`。

事件检测与活动计数（核心三段）：

[psi_common_watchdog.vhd:67-77](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_watchdog.vhd#L67-L77) —— `dat_i /= r.dat_dff` 即「事件来了」，立即把 `activ_count` 清零；否则在未故障时向上数，到 `thld_usign_c` 回零。

[psi_common_watchdog.vhd:79-84](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_watchdog.vhd#L79-L84) —— 活动计数器数满一个周期（说明这一周期没来事件），`miss_count + 1`。

[psi_common_watchdog.vhd:98-117](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_watchdog.vhd#L98-L117) —— 两种模式分别用 `miss_count` 或 `succ_count` 与告警/故障阈值比较，置位后由时序进程锁存（`fault='1'` 后所有计数冻结，需复位才能恢复）。

**debouncer 的周期换算与极性处理**：

[psi_common_debouncer.vhd:36-38](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_debouncer.vhd#L36-L38) —— `count_max_c` 把消抖秒数换算成计数满量程；`pol_eq_c` 用 `math_pkg.choose` 判断输入/输出极性是否一致，决定输出时是否取反。

[psi_common_debouncer.vhd:52-64](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_debouncer.vhd#L52-L64) —— `sync_g=true` 时例化 `psi_common_bit_cc` 做两级同步（异步按键必备）；`sync_g=false` 时直通。两个 `generate` 分支二选一。

[psi_common_debouncer.vhd:75-90](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_debouncer.vhd#L75-L90) —— 输入一跳变就把计数器重载到 `count_max_c`；只有计数器数到 0（输入已稳定满一个窗口），输出才按极性更新。这正是「毛刺被窗口耗死」的实现。

[psi_common_debouncer.vhd:46](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_debouncer.vhd#L46) —— 复位常量 `rst_two_process_c`：计数器清 0、输入/输出寄存器预置为「空闲极性」（`not in_pol_g` / `not out_pol_g`），保证上电时输出处于确定的非激活电平。

#### 4.1.4 代码实践：用 debouncer 处理一个按键

> 本实践对应任务：「用 debouncer 处理一个按键输入，并选择合适的消抖周期。」

**实践目标**：为一只机械按键配置消抖器，验证短毛刺被滤除、长按被放行。

**第 1 步——选消抖周期。** 机械按键抖动通常持续 1–10 ms，工程上取 **10–20 ms** 的消抖窗即可覆盖最坏情况。本例取 `dbnc_per_g = 10.0e-3`（10 ms），时钟 `freq_clk_g = 100.0e6`。

**第 2 步——手工算计数满量程（理解原理）**：

\[
\text{count\_max\_c} = \lceil 10\times10^{-3} \times 10^{8}\rceil - 1 = 1{,}000{,}000 - 1 = 999{,}999
\]

计数器位宽 \( = \text{log2ceil}(999{,}999) = 20 \) 位（\( 2^{19}=524{,}288 < 999{,}999 < 2^{20}=1{,}048{,}576 \)）。组件内部会自动算这两项，你不必手填。

**第 3 步——阅读现成 TB 看如何驱动。** 仓库已带 [testbench/psi_common_debouncer_tb/psi_common_debouncer_tb.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_debouncer_tb/psi_common_debouncer_tb.vhd)。注意它的 generic 与源码默认值不同：`dbnc_per_g = 20.0e-6`（20 μs，仅为加速仿真）、`in_pol_g='1'` / `out_pol_g='0'`（输入输出极性相反）。TB 在 [L87-L96](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_debouncer_tb/psi_common_debouncer_tb.vhd#L87-L96) 先注入 4 个「半周期翻转」（每次抖动短于窗口），断言输出**不应**翻转；随后稳定保持 5 个窗口，断言输出**才**翻转。

**第 4 步——跑回归。** 该 TB 已登记在 [sim/config.tcl:460-461](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl#L460-L461)。按 u1-l3 介绍的方式执行 Modelsim 回归：

```tcl
source run.tcl    ;# 内部依次 init -> config.tcl -> compile_files -all -> run_tb -all -> run_check_errors
```

**需要观察的现象**：

1. 前 4 次半周期翻转期间，`dat_o` 始终保持空闲极性不变（毛刺被滤）。
2. 输入稳定满 5 个 `dbnc_per_g` 后，`dat_o` 才翻到激活极性。
3. 若控制台不出现 `###ERROR###`，即自校验通过。

**预期结果**：仿真结束无 `###ERROR###`，证明消抖窗对短抖动有效、对稳定输入放行。若你想验证 10 ms 的真实按键场景，把 TB 的 `dbnc_per_g` 改回 `10.0e-3` 即可（仿真时间会变长）。**待本地验证**：实际波形与拍数请在你本地的仿真器中确认。

#### 4.1.5 小练习与答案

**练习 1**：watchdog 默认 `freq_clk_g=100e6`、`freq_act_g=100e3`，`thld_fault_succ_g=0`、`thld_fault_total_g=10`。若 `dat_i` 从复位后一直不变，多少个时钟周期后 `fault_o` 拉高？

> **答案**：`thld_c = 100e6/100e3 - 1 = 999`，每个周期 1000 拍算一次漏。累计模式（`succ=0`）下漏 10 次达 `thld_fault_total_g`，故约 \( 10 \times 1000 = 10{,}000 \) 拍后 `fault_o='1'`（精确边界由 `miss_count >= 9 and activ_count = thld_usign_c` 判定，约第 10 个周期末）。

**练习 2**：把 watchdog 从「累计漏次」改成「连续漏次」模式，应改哪个 generic？来一次事件会发生什么？

> **答案**：把 `thld_fault_succ_g` 设为正值（如 4）。此后每来一次事件（`dat_i` 变化），连续漏次计数 `succ_count` 被 [L92-L94](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_watchdog.vhd#L92-L94) 清零，告警/故障标志也随之具备「恢复」语义——只要心跳恢复，连续计数就重新开始。

---

### 4.2 防综合优化 dont_opt

#### 4.2.1 概念说明

综合工具会自动剔除「输出没人用」的逻辑。做时序或资源评估时，你常需要把一个**引脚比芯片多得多的设计**综合上去看资源占用——可一旦 I/O 接不上，工具就把这些路径全优化掉了，测出来的资源数就失真。

`psi_common_dont_opt` 解决这个问题：它用**仅仅 4 个物理引脚**（`pin_io(3:0)`），通过移位/锁存逻辑把 DUT（被测设计）的全部输入输出「拴」在这 4 根线上，让综合工具无法证明这些信号无用，从而全部保留。可把它理解成一片「**虚拟引脚扩展器**」：4 根真实线 serial-in/serial-out 地搬运任意位宽的 DUT I/O。

#### 4.2.2 核心流程

dont_opt 有两条独立的移位通路，共享 4 个引脚：

```
DUT 输出 (dat_i, from_dut_width_g 位) ──► FromDutShiftReg
        pin_io(2)='1' 时整拍装入, 否则每拍左移补 0
        最高位 ──► pin_io(3)   (唯一一个对外输出引脚)

DUT 输入 (dat_o, to_dut_width_g 位) ◄── ToDutLatchReg ◄── ToDutShiftReg
        pin_io(1) 每拍串行移入 ToDutShiftReg
        pin_io(0)='1' 时把移位寄存器整体锁存到 ToDutLatchReg ──► dat_o
```

关键点：`pin_io(3:0)` 中 `(0)(1)(2)` 被驱动为 `'Z'`（高阻，当作外部输入用），只有 `(3)` 是真输出。数据经移位寄存器在 DUT 与物理引脚之间循环流动，形成真实的「数据依赖」，综合工具因而无法剔除。

#### 4.2.3 源码精读

[psi_common_dont_opt.vhd:36-43](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_dont_opt.vhd#L36-L43) —— generic `from_dut_width_g`（DUT 输出位数，进 `dat_i`）/ `to_dut_width_g`（DUT 输入位数，出 `dat_o`）。注意端口注释「signal from DUT / to DUT」是相对 **DUT** 视角描述的，`dat_o` 对 dont_opt 是输出、对 DUT 是输入。

[psi_common_dont_opt.vhd:64-73](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_dont_opt.vhd#L64-L73) —— 两条移位通路：`pin_io(2)` 为 1 时把 `dat_i` 整拍装入 `FromDutShiftReg`，否则逐拍左移补 0；`pin_io(1)` 逐拍串行移入 `ToDutShiftReg`，`pin_io(0)` 为 1 时锁存到 `ToDutLatchReg`。

[psi_common_dont_opt.vhd:79-84](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_dont_opt.vhd#L79-L84) —— `(0)(1)(2)` 置 `'Z'` 当输入，`(3)` 输出 `FromDutShiftReg` 的最高位，`dat_o <= ToDutLatchReg`。这 4 行就是「4 根线拴住全部 I/O」的全部魔法。

> 说明：dont_opt 无专属测试平台（它是综合期工具，行为在仿真里看不出价值），故本模块不设运行型实践，改用下面的源码阅读型实践。

#### 4.2.4 代码实践：阅读型实践——追踪「为何不会被优化」

**实践目标**：从源码推导出综合工具为何无法删掉 DUT 的任意一位 I/O。

**操作步骤**：

1. 读 [L67](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_dont_opt.vhd#L67)：`ToDutShiftReg` 每拍把 `pin_io(1)` 串行移入，若干拍后经 `pin_io(0)` 锁存到 `dat_o`，进入 DUT——所以 DUT 的每一位输入都有「可能影响内部」的外部来源。
2. 读 [L69-L73](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_dont_opt.vhd#L69-L73)：`dat_i`（DUT 输出）被装入 `FromDutShiftReg`，其最高位经 [L82](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_dont_opt.vhd#L82) 驱动到物理引脚 `pin_io(3)`——所以 DUT 的每一位输出都「最终连到一个真实焊盘」。
3. 结论：输入侧有外部源、输出侧有真实焊盘，综合工具无法证明任何一位是死逻辑，于是全保留。

**需要观察的现象 / 预期结果**：在 Quartus/ Vivado 里对一个引脚不足的设计套上 dont_opt 综合后，对比资源报告——原本被优化掉的 DUT 逻辑应重新出现在资源占用中。**待本地验证**：具体资源数字依你选用的器件与设计而定。

#### 4.2.5 小练习与答案

**练习 1**：为什么 dont_opt 只需要 4 个物理引脚，就能保住「任意位宽」的 DUT I/O？

> **答案**：因为它用**串行移位**而非并行接线：DUT 的多位 I/O 在片内移位寄存器里逐拍搬移，对外只暴露「串行数据入 `(1)`、移位/锁存控制 `(0)(2)`、串行数据出 `(3)`」共 4 根线。位宽由片内移位寄存器宽度决定，与对外引脚数无关。

**练习 2**：把 `pin_io(3)` 这一行注释掉（令 `(3)` 也为 `'Z'`），综合会发生什么？

> **答案**：`FromDutShiftReg` 不再驱动任何物理输出，DUT 输出侧变成纯片内死逻辑，综合工具会判定其无外部影响而把整条 DUT 输出通路优化掉——dont_opt 失效。这正是 `(3)` 必须保留为真输出的原因。

---

### 4.3 模拟 / 数字触发器（trigger_analog / trigger_digital）

#### 4.3.1 概念说明

示波器里「在信号越过某个门限时产生一次触发」是常见需求。psi_common 把它拆成两个组件：

- **trigger_analog**：输入是一个（或多个）多 bit 模拟样本（`signed` 或 `unsigned`），当样本**越过阈值** `anl_th_trig_i` 时产生单拍 `trig_o`，可选上升越限、下降越限或两者。
- **trigger_digital**：输入是一位（或多位）`std_logic`，当其出现**上升沿 / 下降沿**时产生单拍 `trigger_o`。

两者共享同一套「**arm/disarm 装弹机制**」与「**连续 / 单次模式**」：

- `trg_arm_cfg_i` 的**上升沿**切换 armed 状态（装弹/退弹）。
- `trg_mode_cfg_i(0)`：0 = 连续模式（每次满足条件都触发），1 = 单次模式（触发一次后自动 disarm，需重新装弹）。
- `ext_disarm_i`：当多个触发源同时装弹、只要一个触发就让其余退弹时使用。
- `trg_is_armed_o`：指示当前是否处于装弹待发状态。

#### 4.3.2 核心流程

**装弹状态机**（两组件相同）：

```
if (本拍产生了触发 OTrg='1' 或 ext_disarm_i='1') 且 单次模式:
    TrgArmed <= '0'                      -- 单次模式触发后自动退弹
elsif trg_arm_cfg_i 上升沿:
    TrgArmed <= not TrgArmed             -- 装弹/退弹翻转
```

**触发条件**（analog，以 signed 为例）：

```
把选中通道的样本打入 RegAnalogValueSigned，再打一拍得 _dff
若 TrgArmed='1':
    上升越限: _dff < 阈值 且 当前 >= 阈值 且 edge_bit(1)='1'  => OTrg='1'
    下降越限: _dff > 阈值 且 当前 <= 阈值 且 edge_bit(0)='1'  => OTrg='1'
```

注意「`_dff` 在阈值一侧、当前在另一侧」就是一次**穿越**，比单纯 `>=` 阈值更稳健（不会因样本持续高于阈值而每拍都触发）。

**触发条件**（digital）：

```
把选中通道打入 RegDigitalValue_dff
若 TrgArmed='1':
    上升沿: _dff='0' 且 当前='1' 且 edge_bit(1)='1'  => OTrg='1'
    下降沿: _dff='1' 且 当前='0' 且 edge_bit(0)='1'  => OTrg='1'
```

延迟差异：analog 因多了一级样本寄存再比较，触发延迟 **2 拍**；digital 只打一拍，延迟 **1 拍**（文档明确标注，可由用户外部补偿）。

#### 4.3.3 源码精读

[psi_common_trigger_digital.vhd:32](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_trigger_digital.vhd#L32) —— 通道选择位宽用 `choose(trig_nb_g > 1, log2ceil(trig_nb_g)-1, 0)`：只有 1 个触发源时不浪费选择位。这是 `math_pkg.choose` 在端口声明区当「三元运算符」的典型用法（端口声明区不能写 `if`）。

[psi_common_trigger_digital.vhd:63-67](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_trigger_digital.vhd#L63-L67) 与 [psi_common_trigger_analog.vhd:73-77](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_trigger_analog.vhd#L73-L77) —— 两组件的装弹状态机完全一致：单次模式触发即退弹，否则 `trg_arm_cfg_i` 上升沿翻转 armed。

[psi_common_trigger_digital.vhd:72-80](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_trigger_digital.vhd#L72-L80) —— 数字边沿检测：`dff='0' and cur='1'` 判上升、`dff='1' and cur='0'` 判下降，分别由 `trg_edge_cfg_i` 的 bit1/bit0 使能。

[psi_common_trigger_analog.vhd:84](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_trigger_analog.vhd#L84) —— 用 `trg_anlg_src_cfg_i` 从 `trig_nb_g` 路模拟输入里切出当前通道（按 `width_g` 位宽切片），再 [L88-L95](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_trigger_analog.vhd#L88-L95) 做「`_dff` 与阈值异侧」的穿越判定。

> 说明：`is_signed_g` 在 elaboration 期用两个独立 `if`（[L82](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_trigger_analog.vhd#L82) 与 [L98](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_trigger_analog.vhd#L98)）分别处理 signed / unsigned，综合后只保留其一。

#### 4.3.4 代码实践：阅读型实践——读懂一次模拟越限触发

**实践目标**：跟踪一个上升越限触发从输入到 `trig_o` 的完整路径，确认 2 拍延迟。

**操作步骤**：

1. 在 [trigger_analog_tb](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_trigger_analog_tb/psi_common_trigger_analog_tb.vhd) 中找到一次「装弹 → 阈值从低到高越过 → 检查 trig_o」的用例。
2. 对照源码 [L84](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_trigger_analog.vhd#L84) → [L86](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_trigger_analog.vhd#L86)（打一拍）→ [L89](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_trigger_analog.vhd#L89)（比较置 `OTrg`）→ [L116](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_trigger_analog.vhd#L116)（`trig_o <= r.OTrg` 再寄存一拍），数清楚穿越发生在第 N 拍时 `trig_o` 在第几拍拉高。
3. 跑回归：该 TB 已登记在 [sim/config.tcl:434-435](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl#L434-L435)。

**需要观察的现象**：样本穿越阈值后，`trig_o` 在约 **2 个时钟周期**后出现一个单周期脉冲；单次模式下脉冲出现后 `trg_is_armed_o` 立即掉到 0。

**预期结果**：波形中 `trig_o` 恰为一拍宽、相对穿越点延迟 2 拍。**待本地验证**：精确拍数与极性请对照本地仿真波形确认。

#### 4.3.5 小练习与答案

**练习 1**：为什么 trigger_analog 用「`_dff < 阈值 且 当前 >= 阈值`」而不是简单的「`当前 >= 阈值`」来判上升越限？

> **答案**：若只判 `当前 >= 阈值`，只要样本持续高于阈值，每拍都会触发——与「越限一次」的语义不符。用前后两拍分处阈值两侧来判定「穿越」，保证一次跨越只产生一拍触发，对缓慢变化的模拟信号更稳健。

**练习 2**：要在数字触发器上同时响应上升沿和下降沿，`trg_edge_cfg_i` 应设为何值？

> **答案**：`"11"`（bit1=1 使能上升、bit0=1 使能下降）。只上升沿用 `"10"`，只下降沿用 `"01"`。

---

### 4.4 动态移位器 dyn_sft

#### 4.4.1 概念说明

「按运行时变量 `shift_i` 把数据左/右移任意位」叫**动态移位**（barrel shifter，桶形移位器）。最朴素的实现是用一个 \( 2^{N}:1 \) 的巨大多路选择器一次选好结果——但移位位宽一宽，这个 mux 就又宽又深，成为时序瓶颈。

`psi_common_dyn_sft` 的思路是**把一次大移位拆成多级小移位**，每级只移「`sel_bit_per_stage_g` 位」的一段（即每级 mux 只有 \( 2^{\text{sel\_bit\_per\_stage\_g}}:1 \)），级与级之间插寄存器。这样每级组合逻辑很浅、频率高，代价是多花 `Stages_c` 拍延迟与若干寄存器——典型的「用流水线换频率」。它用 `vld_i`/`vld_o` 选通，是一段 AXI-S 风格的数据通路（但无反压）。

#### 4.4.2 核心流程

级数推导（关键公式）：

\[
\text{shift\_i 位宽} = \text{log2ceil}(\text{max\_shift\_g}+1)
\]

\[
\text{Stages\_c} = \left\lceil \frac{\text{shift\_i 位宽}}{\text{sel\_bit\_per\_stage\_g}} \right\rceil
\]

每级 `stg` 处理 `shift_i` 的低 `sel_bit_per_stage_g` 位（记 `Select_v`），实际移位量为：

\[
\text{移位量} = \text{Select\_v} \times 2^{\,\text{stg}\cdot\text{sel\_bit\_per\_stage\_g}}
\]

即第 0 级移 \( 0\ldots 2^{s}-1 \) 位、第 1 级移 \( 0 \) 或 \( 2^{s} \) 的整数倍、……（\( s=\text{sel\_bit\_per\_stage\_g} \)）。每级处理后把 `shift_i` 右移 `sel_bit_per_stage_g` 位交给下一级（用 `logic_pkg.shift_right`）。

例：`max_shift_g=16`、`sel_bit_per_stage_g=4` → shift_i 5 位 → Stages_c = ⌈5/4⌉ = 2 级；第 0 级移 0–15 位（步长 1），第 1 级移 0 或 16 位（步长 16），合计覆盖 0–31 ≥ 16。

左移填 0；右移可由 `sign_extend_g` 选择填符号位（算术移位）或填 0（逻辑移位）。

#### 4.4.3 源码精读

[psi_common_dyn_sft.vhd:41](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_dyn_sft.vhd#L41) —— `Stages_c` 用 `ceil(shift_i'length / sel_bit_per_stage_g)` 推导级数，决定流水线深度。

[psi_common_dyn_sft.vhd:48-53](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_dyn_sft.vhd#L48-L53) —— record 用**数组字段** `Data(0 to Stages_c)` / `Shift(...)` / `Vld(...)` 承载每级中间结果，是「带数组类型的 record」的范例。

[psi_common_dyn_sft.vhd:77-100](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_dyn_sft.vhd#L77-L100) —— `for stg in 0 to Stages_c-1 loop` 展开各级：算 `StepSize_v = 2**(stg*sel_bit_per_stage_g)`，取本级的 `Select_v`，按方向拼一个双倍宽临时向量 `TempData_v` 再切出结果。

[psi_common_dyn_sft.vhd:83-90](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_dyn_sft.vhd#L83-L90) —— 右移：`sign_extend_g` 为真时用符号位填充（算术右移），否则填 0。

[psi_common_dyn_sft.vhd:98](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_dyn_sft.vhd#L98) —— 把 `shift_i` 右移 `sel_bit_per_stage_g` 位交给下一级，逐级消耗移位指令。

[psi_common_dyn_sft.vhd:57](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_dyn_sft.vhd#L57) —— `assert` 校验 `direction_g` 必须是 `"LEFT"` 或 `"RIGHT"`，非法值在仿真期报 `###ERROR###`。

#### 4.4.4 代码实践：推算延迟与资源

**实践目标**：给定三组 generic，预测延迟拍数与每级 mux 规模，体会「流水线换频率」的取舍。

**操作步骤**：

1. 默认配置 `max_shift_g=16, sel_bit_per_stage_g=4, width_g=32`：按上文公式算出 shift_i=5 位、Stages_c=2、每级 mux \( 16:1 \)、数据延迟 2 拍。
2. 改成 `sel_bit_per_stage_g=1`：Stages_c=5、每级 mux 仅 \( 2:1 \)、延迟 5 拍——组合更浅、频率更高、但寄存器更多、延迟更长。
3. 改成 `sel_bit_per_stage_g=5`：Stages_c=1、单级 \( 32:1 \) mux、延迟 1 拍——回到「快但时序紧」。
4. 跑现成回归验证功能：[testbench/psi_common_dyn_sft_tb](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_dyn_sft_tb/psi_common_dyn_sft_tb.vhd)，登记在 [sim/config.tcl:463-464](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl#L463-L464)。

**需要观察的现象**：TB 在不同 `shift_i`、不同 `direction_g`/`sign_extend_g` 下注入数据，自校验 `dat_o` 与 `vld_o` 时序对齐——`vld_o` 应比 `vld_i` 恰好晚 `Stages_c` 拍。

**预期结果**：所有用例无 `###ERROR###`；`dat_o` 是 `dat_i` 按 `shift_i` 移位后的结果，延迟等于 Stages_c。**待本地验证**：波形与具体拍数请本地确认。

#### 4.4.5 小练习与答案

**练习 1**：`max_shift_g=16`、`sel_bit_per_stage_g=4` 时，数据从 `dat_i` 到 `dat_o` 延迟几拍？为什么？

> **答案**：2 拍。shift_i 5 位 → Stages_c = ⌈5/4⌉ = 2，每级之间有寄存器，`vld`/`Data` 沿 `0→1→2` 共穿过 2 级寄存器。

**练习 2**：要把右移改成「算术右移（保留符号位）」，应设哪个 generic？左移受它影响吗？

> **答案**：设 `sign_extend_g => true`（默认即真）。它只对 `direction_g="RIGHT"` 生效（见 [L84-L88](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_dyn_sft.vhd#L84-L88)）；左移恒填 0（[L92](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_dyn_sft.vhd#L92)），不受影响。

---

## 5. 综合实践

**任务：为一只按键搭建一条「带心跳监控的边沿触发通道」。** 把本讲三个组件串起来：

1. **输入消抖**：用 `psi_common_debouncer`（`width_g=1`、`dbnc_per_g=10.0e-3`、`freq_clk_g=100.0e6`、`sync_g=true`）处理异步按键，输出干净的电平 `btn_clean`。
2. **边沿触发**：把 `btn_clean` 接到 `psi_common_trigger_digital` 的 `digital_trg_i`，配置 `trg_edge_cfg_i="10"`（上升沿）、`trg_mode_cfg_i="0"`（连续）、预先给 `trg_arm_cfg_i` 一个上升沿装弹。观察每按一次键产生一个单拍 `trigger_o`。
3. **心跳看门狗**：另用一个 `psi_common_watchdog` 监视 `trigger_o`（作为 `dat_i`），设 `freq_act_g` 为「期望的最慢按键节奏」（如 2 Hz），`thld_fault_succ_g` 取正值走连续漏次模式。若用户长时间不按键，`fault_o` 拉高。

**完成建议**：

- 先逐个用现成 TB（debouncer_tb / trigger_digital_tb / watchdog_tb）验证单组件行为，再在自建顶层里把它们连起来。
- 复用 [sim/config.tcl](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl) 的注册与 `###ERROR###` 自检约定（u1-l3、u11-l1）写一个自校验 TB。
- 难点提示：触发器的 arm 是上升沿敏感，TB 里给 `trg_arm_cfg_i` 一个单周期脉冲即可，不要长拉高。

## 6. 本讲小结

- **debouncer 与 watchdog 方向相反**：前者滤掉「变得太快」的输入（需稳定满窗口才放行），后者报警「变得太慢」的事件源（一周期无变化记一次漏）；两者都用「打一拍比较」做变化/边沿检测。
- 两个组件都把 generic 里的**秒/赫兹**在 elaboration 期换算成**计数满量程**，并用 `log2ceil` 自动推导计数器位宽。
- **dont_opt** 用 4 个物理引脚 + 串行移位，制造真实数据依赖，骗过综合工具保住任意位宽 DUT I/O，是综合/资源评估的专用工具。
- **trigger_analog / trigger_digital** 共享「arm/disarm + 连续/单次」状态机；analog 判阈值**穿越**（延迟 2 拍），digital 判信号**边沿**（延迟 1 拍）。
- **dyn_sft** 把大动态移位拆成多级小移位、级间插寄存器，用 `Stages_c` 拍延迟换高频率，是「流水线换时序」的范例。
- 全部六个组件统一采用**二进程 record 设计法**，并复用 `math_pkg`（`log2ceil`/`ceil`/`choose`）、`logic_pkg`（`shift_right`）、`bit_cc`（同步器）等基础包。

## 7. 下一步学习建议

- **脉冲/斜坡生成与整形**（[u10-l2](u10-l2-ramp-pulse-shaper.md)）：与本讲的 trigger/选通组件同属「事件/波形生成」家族，可对照阅读 `ramp_gene`、`pulse_shaper` 如何用状态机产生持续波形。
- **统计与信号源**（[u10-l4](u10-l4-stats-and-sources.md)）：`find_min_max`、`prbs` 与本讲的 trigger_analog 配合，可组成「PRBS 激励 → 越限触发 → 流式统计」的完整数据采集链。
- **自校验测试平台**（[u11-l1](u11-l1-self-checking-testbench.md)）：本讲多次引用的 `###ERROR###` 自检约定与 psi_tb 工具包在那里系统讲解，是写好本讲组件 TB 的前提。
- 想深入边沿/同步细节，可回看 [u5-l1 pulse_cc](u5-l1-pulse-cc.md) 与 [u5-l2 bit_cc](u5-l2-simple-status-bit-cc.md)；想巩固流水线拆级思想，可回看 [u7-l1 pl_stage](u7-l1-pl-stage.md) 与 [u7-l2 multi_pl_stage](u7-l2-multi-pl-stage.md)。
