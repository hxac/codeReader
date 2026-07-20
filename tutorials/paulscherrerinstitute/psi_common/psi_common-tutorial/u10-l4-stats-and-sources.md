# 统计与信号源：min_max / prbs / pwm / sample_rate_converter

## 1. 本讲目标

学完本讲后，你应该能够：

- 用 `psi_common_find_min_max` / `psi_common_min_max_sum` 在数据流上**实时**提取最小值、最大值与向量求和，并说清楚「窗口」「RAZ 复位」「运行输出」三件事的关系。
- 说清 `psi_common_prbs` 这一类**线性反馈移位寄存器（LFSR）**伪随机序列发生器的工作原理：多项式抽头、反馈位、最长周期、全零种子的危害。
- 描述 `psi_common_pwm` 如何用一个「步进相位计数器 + 窗口比较器」在选通节拍上产生带占空比、带延时、可倍频的脉冲输出。
- 区分 `psi_common_sample_rate_converter` 的**抽取（DOWN）**与**插值（UP）**两种模式，并理解它为何「无滤波」、为何上下游都建议加滤波器。
- 把这些组件当作**数据通路上的探针与信号源**组合使用（例如 `prbs → find_min_max` 的自检链路）。

本讲属于「杂项组件」单元，依赖 **u7-l1（pl_stage 与二进程设计法）** 中建立的两个共识：**vld/strobe 选通驱动的时序约定**，以及**库内通用的单进程/双进程写法**。本讲会反复用到这两点。

## 2. 前置知识

在进入源码前，先用三段话把四个组件共用的「世界观」说清。

**第一，选通（strobe / vld）驱动的流式处理。** 本讲的四个组件都不用 `ready` 反压，而是「你给我一个 `vld_i` 脉冲，我就处理一个样本」。这与 u7-l1 中 `pl_stage` 的 AXI-S 双向握手不同——这些组件面向**统计/信号源**这类「单向数据流」场景：上游以固定或近固定速率来数据，下游只关心结果。所以你会看到 `vld_i` 是几乎所有组件的「心跳」。

**第二，单进程时序风格。** u7-l1 介绍了库内的「二进程 record 设计法」（`r`/`r_next` + 组合进程 + 时序进程）。本讲的五个组件**没有**严格采用那套范式，而是用更轻量的「一个 `process(clk_i)`、若干独立信号」写法。原因是它们的状态少（一两个寄存器）、控制简单，单进程更直观。这是库内「不同复杂度用不同风格」的一个真实样本，读源码时不要硬套 record 模板。

**第三，signed_g / mode_g 这类 generic 的二选一切换。** 这些组件大量用 `if mode_g = "..." then` / `if signed_g then` 在**综合期**选择比较方向、符号解释，从而一份代码覆盖「有符号/无符号」「最小/最大」「抽取/插值」多种配置。这和 u7-l1 的 `use_rdy_g` 用 `if generate` 切两套实现是同一思想。

下面三个数学记号会用到：

- 窗口内求极值：\( \text{min}_w = \min_{k\in W} x_k \)，\( \text{max}_w = \max_{k\in W} x_k \)，\( \text{sum}_w = \sum_{k\in W} x_k \)。
- LFSR 最长周期：\( L = 2^{N} - 1 \)（\(N\) 为寄存器位数，全零态除外）。
- 采样率变换：抽取 \( f_{out} = f_{in}/R \)，插值 \( f_{out} = R\cdot f_{in} \)。

## 3. 本讲源码地图

| 文件 | 作用 | 是否有专属 TB | 是否在回归中运行 |
|:--|:--|:--:|:--:|
| `hdl/psi_common_find_min_max.vhd` | 在数据流上跟踪并输出当前窗口的极值（MIN 或 MAX），是 `min_max_sum` 的底层积木 | 有 | 有（4 组 generic） |
| `hdl/psi_common_min_max_sum.vhd` | 组合两个 `find_min_max` + 一个累加器，在一个时间窗内同时给出 min / max / 向量和 | 有 | 有（4 组 generic） |
| `hdl/psi_common_prbs.vhd` | 基于 LFSR 的伪随机二进制序列发生器，位宽 2–32 | 有 | 有（PRBS-10 两组种子） |
| `hdl/psi_common_pwm.vhd` | 通用 PWM：步进相位计数器 + 延时/脉宽窗口比较器，输出单 bit 脉冲 | 有 | **否**（仅编译，无 `create_tb_run`） |
| `hdl/psi_common_sample_rate_converter.vhd` | 无滤波的整数倍抽取/插值（采样率变换） | 有 | **否**（仅编译，2024 年新增） |

> 说明：`pwm` 与 `sample_rate_converter` 的测试台已登记进 `sim/config.tcl` 的源码编译列表（`add_sources -tag lib/tb`），但还没有 `create_tb_run` 回归条目。这意味着它们随库一起编译、可手工交互仿真，但不参与 `run.tcl` 的自动回归。这是你「按讲义跑实践」时需要知道的现状——下文凡涉及运行结果，若该组件无回归 TB，一律标注「待本地验证」。

## 4. 核心概念与源码讲解

### 4.1 min_max 统计：find_min_max 与 min_max_sum

#### 4.1.1 概念说明

很多实时系统需要对一段连续到来的数据回答三个问题：**这段时间里最小是多少？最大是多少？总和（进而均值）是多少？** 例如监测 ADC 输出的峰峰值、统计传感器读数的均值、给后续告警逻辑提供门限。

`psi_common_find_min_max` 就是「在数据流上持续跟踪当前极值」的最小积木：每来一个 `vld_i` 样本，它就拿当前样本和「到目前为止的极值寄存器」比较，更极端就更新。它还提供一个 `raz_i`（reset accumulation）输入——在其上升沿，把累积到现在的极值**锁存到输出**并打一拍 `vld_o`，同时把极值寄存器**重置为当前样本**，开始下一个统计窗。

`psi_common_min_max_sum` 则在 `find_min_max` 之上再上一层：**同时例化一个 MIN 例化、一个 MAX 例化**，并加一个累加器求向量和；用一个内部计数器自动产生 `raz`，使统计窗固定为 `clock_cycle_g` 个有效样本，也允许外部 `sync_i` 强制对齐窗口边界。这是库内「**用例化复用，而非复制粘贴**」的一个干净样本。

#### 4.1.2 核心流程

`find_min_max` 的运行机制（单进程）：

```
每个 clk_i 上升沿：
  if 复位:
      极值寄存器 data_s ← 0；raz 延迟寄存器 ← 0
  else:
      str_s ← vld_i                      # 运行输出选通延迟一拍
      raz_dff_s ← raz_i                  # 用于 raz 上升沿检测

      if (raz_i 上升沿):                  # 窗口结束
          dat_o ← data_s；vld_o ← 1       # 锁存本窗极值并脉冲输出
      else:
          vld_o ← 0

      if (raz_i 上升沿):
          data_s ← dat_i                  # 新窗口以当前样本为起点
      elif vld_i = 1:
          if mode=MIN:  data_s ← min(data_s, dat_i)
          if mode=MAX:  data_s ← max(data_s, dat_i)

      run_dat_o ← data_s                  # 运行输出 = 当前累积极值
      run_str_o ← str_s
```

要点三条：

1. **两种输出**：`dat_o`/`vld_o` 是「窗口结束才更新一次」的结果输出；`run_dat_o`/`run_str_o` 是「每拍都反映当前累积极值」的运行输出，适合需要随时读取当前极值的场景。
2. **极值更新用 `<=` / `>=`**（不是严格 `<` / `>`），所以遇到相等的样本也会刷新为最新值，避免「相等不更新」造成的陈旧值。
3. **符号与方向靠 generic 切**：`signed_g` 选 `signed()` 还是 `unsigned()` 比较，`mode_g` 选 MIN 还是 MAX。

`min_max_sum` 在此基础上：

```
每个 clk_i 上升沿：
  if (sync_i=1 或 计数器到 clock_cycle_g-1):   # 窗口结束
      计数器 ← 0
      累加器 ← dat_i                          # 新窗口以当前样本重新起算
      mean_s ← 旧累加器值                      # 锁存本窗向量和
      raz_s ← 1                               # 通知两个 find_min_max 锁存极值
  elif vld_i=1:
      计数器 ← 计数器+1
      累加器 ← 累加器 + dat_i

  if (min_str_s=1):                           # find_min_max 给出窗口结束脉冲
      vld_o ← 1；min_o/max_o/sum_o ← 各自锁存值
```

注意 `clock_cycle_g` 计的是**有效样本数**（计数器只在 `vld_i=1` 时自增），名字里的 "clock cycle" 容易误导。要得到均值，需把 `sum_o` 再除以窗口样本数。

#### 4.1.3 源码精读

`find_min_max` 的实体声明——四个 generic 决定复位极性、位宽、符号、方向：

[hdl/psi_common_find_min_max.vhd:L18-L32](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_find_min_max.vhd#L18-L32) —— 实体端口；注意 `run_dat_o`/`run_str_o` 这对运行输出。

整个组件就是一个时钟进程，`raz` 上升沿检测与极值比较都写在里面：

[hdl/psi_common_find_min_max.vhd:L40-L89](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_find_min_max.vhd#L40-L89) —— 核心进程 `proc_min_max`。其中 L51–L56 做 `raz` 上升沿检测并锁存输出；L59–L81 是 `mode_g`/`signed_g` 四种组合下的极值更新。

运行输出在末尾两行：

[hdl/psi_common_find_min_max.vhd:L83-L85](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_find_min_max.vhd#L83-L85) —— `run_dat_o <= data_s` 把当前累积极值持续送出。

`min_max_sum` 的实体（generic 名以源码为准：`data_width_g`、`accu_width_g`；**文档表里写成 `data_length_g`/`accu_length_g`，与源码不一致，读源码时以源码为准**）：

[hdl/psi_common_min_max_sum.vhd:L14-L29](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_min_max_sum.vhd#L14-L29) —— 实体端口，输出 `min_o`/`max_o`/`sum_o`。

累加器位宽保护——`assert` 在综合期强制「累加器位宽必须容得下 `clock_cycle_g` 个样本相加」：

[hdl/psi_common_min_max_sum.vhd:L48-L50](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_min_max_sum.vhd#L48-L50) —— `assert accu_width_g > log2ceil(clock_cycle_g)+data_width_g`，否则 `severity failure`。这里复用了 u2-l1 讲过的 `log2ceil`。

窗口/累加主进程：

[hdl/psi_common_min_max_sum.vhd:L53-L101](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_min_max_sum.vhd#L53-L101) —— `proc_count`；L62–L71 处理窗口结束（`sync_i` 或计数器饱和）时累加器重起算、`mean_s` 锁存；L73–L81 是常规累加；L84–L88 产生内部 `raz_s`。

「用例化复用」的关键——直接例化两个 `find_min_max`：

[hdl/psi_common_min_max_sum.vhd:L104-L129](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_min_max_sum.vhd#L104-L129) —— 一个例化配 `mode_g=>"MIN"`，一个配 `mode_g=>"MAX"`，共用同一份 `dat_i`/`vld_i`/`raz_s`。

#### 4.1.4 代码实践

**实践目标**：用 `prbs` 生成一段随机数据流，喂给 `find_min_max`（MAX 模式），在一段样本后给 `raz` 脉冲，验证 `dat_o` 确实等于这段时间的最大值。

**操作步骤**（源码阅读型实践，基于已有的 `find_min_max_tb` 思路）：

1. 打开 `testbench/psi_common_find_min_max_tb/psi_common_find_min_max_tb.vhd`，阅读 `proc_stim`（L86–L146）：它用 `uniform()` 生成随机数作为 `data_sti`，同时**在 TB 里用软件方式**维护一个参考极值 `val_v`，最后在 `raz` 后用 `IntCompare(val_v, data_obs)` 比对。
2. 注意 L101–L133 的循环：每拍产生一个随机 `data_sti`，并按 `mode_g`/`signed_g` 更新参考值 `val_v`。
3. 注意 L135–L142：拉高 `raz_sti` 一个周期，等 `str_obs='1'`，再与 `data_obs` 比对。

**需要观察的现象**：

- `run_dat_obs` 在整个过程中**单调（非严格）逼近**最终极值——对 MAX 模式，它只增不减。
- `raz` 上升沿后那一拍，`str_obs` 拉高一拍，`data_obs` 等于参考极值 `val_v`。

**预期结果**：`IntCompare` 不报错，仿真正常结束（`tb_run_s <= false`）。

**待本地验证**：如果你想亲手跑，需把 TB 接到 PsiSim（见 u1-l3）。本讲不假装已运行。

#### 4.1.5 小练习与答案

**练习 1**：把 `find_min_max` 配成无符号 MIN 模式，输入序列是 `5, 2, 8, 2, 1`，每个样本都带 `vld_i=1`。运行输出 `run_dat_o` 依次是什么？在 `raz` 上升沿后 `dat_o` 是多少？

**答案**：因为更新用 `<=`（相等也更新），`run_dat_o` 依次为 `5, 2, 2, 2, 1`（第 3 个 `2` 刷新了寄存器但值不变）。`raz` 后 `dat_o = 1`（本窗最小值）。

**练习 2**：`min_max_sum` 里为什么需要那条 `assert accu_width_g > log2ceil(clock_cycle_g)+data_width_g`？如果去掉会怎样？

**答案**：累加器要把最多 `clock_cycle_g` 个 `data_width_g` 位宽的样本相加，和的最大位宽约为 `data_width_g + log2ceil(clock_cycle_g)`。`assert` 在综合期保证 `accu_width_g` 足够，否则累加会溢出、`sum_o` 截断出错且无任何运行期告警。

**练习 3**：为什么 `min_max_sum` 选择「例化两个 `find_min_max`」而不是把比较逻辑复制两份？

**答案**：复用既有已验证组件，减少重复代码、保证 MIN/MAX 两路行为一致、便于维护；这也符合库「组件即积木」的组织方式。

---

### 4.2 PRBS：prbs 伪随机序列发生器

#### 4.2.1 概念说明

**PRBS（Pseudo-Random Binary Sequence，伪随机二进制序列）** 在硬件测试里几乎是「万能激励」：鉴相/误码测试（BERT）、数据通路压测、填充 RAM 做链路自检、给 `find_min_max` 这类统计组件喂数据……都靠它。它「看起来随机」，但其实是**确定可重复**的——给定种子和多项式，序列完全可复现，这对回归测试至关重要。

`psi_common_prbs` 用经典的**线性反馈移位寄存器（LFSR, Linear-Feedback Shift Register）** 实现：一个 N 位移位寄存器，按某个「抽头多项式」选出若干位，把它们异或（XOR）后塞回最低位，整体每次 `vld_i` 移一位。多项式选得对，序列周期达到最长 \( L = 2^{N}-1 \)（除去全零态），称为**最长 LFSR（maximal-length LFSR）**。

#### 4.2.2 核心流程

```
综合期：
  从查找表 poly_c(width_g) 取出该位宽的最长多项式掩码
  mask_c ← poly_c(width_g) 的低 width_g 位

每个 clk_i 上升沿：
  if 复位:
      q_s ← seed_i          # 装载种子（全 0 非法！）
      vld_o ← 0
  else:
      if vld_i = 1:
          q_s ← q_s 左移一位，最低位填入反馈位 d0_s
      vld_o ← vld_i         # 输出随输入节拍

组合逻辑（每拍）：
  q_masked_s ← mask_c and q_s     # 只保留抽头位
  d0_s       ← XOR(q_masked_s)    # 反馈位 = 抽头位的异或（奇偶）
  dat_o      ← q_s                # 当前寄存器值即输出
```

关键点：

- **反馈位**是抽头位的奇偶（VHDL-2008 一元 `xor`）：\( d_0 = \bigoplus_{i\in \text{taps}} q_i \)。
- **最长周期** \( L = 2^{N}-1 \)：全零态是「死锁态」（XOR of zeros = 0，永远停在 0），所以**种子不能全零**，否则输出恒 0。
- 位宽 `width_g` 限定 `2..32`，每个位宽都配了一条经查表得到的、能达到最长周期的多项式。

#### 4.2.3 源码精读

实体声明——位宽范围 `2 to 32`，种子从端口送入：

[hdl/psi_common_prbs.vhd:L17-L26](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_prbs.vhd#L17-L26) —— 注意 `seed_i` 位宽随 `width_g`。

最长多项式查找表（PRBS2 ~ PRBS32 各一条）：

[hdl/psi_common_prbs.vhd:L30-L64](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_prbs.vhd#L30-L64) —— `poly_c` 数组，注释标注了每条对应的 PRBS 编号（如 PRBS7 = `0x60`、PRBS9 = `0x110`、PRBS31 = `0x48000000`）。

掩码与反馈位的组合逻辑（三行就把 LFSR 的「抽头 + 异或」说清）：

[hdl/psi_common_prbs.vhd:L66-L76](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_prbs.vhd#L66-L76) —— `mask_c` 取表项低位、`q_masked_s` 选抽头、`d0_s <= xor q_masked_s`（VHDL-2008 一元归约异或）。

移位/复位进程：

[hdl/psi_common_prbs.vhd:L78-L91](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_prbs.vhd#L78-L91) —— 复位时装载种子（L82–L83）；`vld_i=1` 时左移并把 `d0_s` 塞进最低位（L86）；`vld_o` 直接跟随 `vld_i`（L88）。

回归测试用一条「参考 LFSR」交叉验证周期性：

[testbench/psi_common_prbs_tb/psi_common_prbs_tb.vhd:L135-L161](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_prbs_tb/psi_common_prbs_tb.vhd#L135-L161) —— 例化 DUT `psi_common_prbs`，并例化一个独立参考模型 `maximal_length_lfsr`，用 `StdlvCompareStdlv` 逐拍比对。

#### 4.2.4 代码实践

**实践目标**：理解 PRBS 的「确定性 + 最长周期」，并把它作为激励源。

**操作步骤**（阅读型 + 思考型实践）：

1. 看 `config.tcl` 中 prbs 的回归配置：

   [sim/config.tcl:L472-L476](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl#L472-L476) —— 两组运行：`width_g=10, seed_g=79` 与 `width_g=10, seed_g=6`。即只回归 PRBS-10，两种子。

2. 看 TB 常量 `CYCLE`（L28）：`choose(width_g<20, 2**N-1, 1024)`——对 PRBS-10，期望周期就是 \( 2^{10}-1 = 1023 \)。

3. 在 TB 的 `assrt_p`（L117–L132）里理解自检策略：把第一轮 `CYCLE` 个输出存进数组 `mem`，第二轮逐拍与 `mem` 比对，期望**完全相同**（验证周期性）。

**需要观察的现象 / 预期结果**：

- 对 PRBS-10，序列每 1023 拍精确重复一次。
- 同一种子每次仿真产生完全相同的序列（确定性）。
- 若把 `seed_g` 改成全 0，输出应恒为 0（死锁）——**不要**这样设。

**待本地验证**：实际 1023 拍周期需本地跑仿真确认。

#### 4.2.5 小练习与答案

**练习 1**：为什么种子全零会产生「死锁」？

**答案**：反馈位 \( d_0 = \bigoplus q_i \)，当所有 \( q_i=0 \) 时 \( d_0=0 \)，左移后最低位仍是 0，寄存器永远停在全零态，输出恒 0。

**练习 2**：PRBS-7（`width_g=7`）的最长周期是多少？它最多能产生多少种**不同的**非零 N 位值？

**答案**：最长周期 \( L = 2^{7}-1 = 127 \)，会遍历全部 127 个非零 7 位状态各一次。

**练习 3**：把 `vld_i` 拉低若干拍，`dat_o` 会变吗？`vld_o` 呢？

**答案**：`vld_i=0` 时移位被冻结（L85 的 `if vld_i='1'`），`q_s`（即 `dat_o`）保持不变；`vld_o` 跟随 `vld_i`，也为 0。所以 PRBS 是「选通驱动」——节拍完全由 `vld_i` 决定。

---

### 4.3 PWM：pwm 通用脉冲发生器

#### 4.3.1 概念说明

`psi_common_pwm` 是一个比 u10-l2 脉冲整形器更「模拟向」的 PWM：它输出一个单 bit `dat_o`，在一个由 `period_g` 定义的「大周期（super period）」里，按 `pwm_i`（脉宽/占空比）和 `dly_i`（起始延时）产生一个高电平窗口，并可用 `rate_i` 在一个大周期里产生多个脉冲（倍频）。

它的核心模型是一个**步进相位计数器**：在一个选通节拍（`str_freq_g`，默认 100 kHz）域里，计数器每拍前进 `rate_i` 步，在 `[dly, dly+pwm]` 这个相位窗口内拉高输出。节拍本身由内部例化的 `psi_common_strobe_generator`（u6-l1）产生，`trig_i` 用来对齐相位边界。

#### 4.3.2 核心流程

```
综合期常量：
  ratio_c ← 自然数(str_freq_g / period_g)     # 一个大周期含多少个选通拍
  nbit_c  ← log2ceil(str_freq_g / period_g)   # 计数器位宽

运行时（每个 clk_i 上升沿）：
  1. trig_i 上升沿检测 → 锁存 pwm_i/dly_i，按 is_sync_g 决定是否锁存 rate_i
  2. str_100k_s（内部 strobe_generator 产生）为 1 时：
       相位计数器 cpt_period_s += rate_i，到 ratio_c 回绕
  3. 输出窗口比较：
       if (dly ≤ cpt_period_s ≤ dly+pwm) 且 (pwm ≠ 0):
           dat_o ← 1
       else:
           dat_o ← 0
```

三个控制口的语义（单位都是「`str_freq_g` 选通拍数」）：

| 端口 | 含义 |
|:--|:--|
| `pwm_i` | 脉宽（高电平持续多少拍），等价于占空比；为 0 则输出常 0 |
| `dly_i` | 起始延时（窗口左端从第几拍开始） |
| `rate_i` | 相位步进，增大可在 `period_g` 内塞入更多脉冲 |

`is_sync_g=true` 时，`rate_i` 在 `trig_i` 上升沿采样（与 PWM 周期同步刷新）；`false` 时连续生效（运行中可变）。

#### 4.3.3 源码精读

实体声明——注意三个控制端口的位宽都是 `log2ceil(real(str_freq_g)/period_g)`：

[hdl/psi_common_pwm.vhd:L25-L39](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pwm.vhd#L25-L39) —— 默认 `clk_freq_g=125E6`、`str_freq_g=100e3`、`period_g=3.125`，故 `ratio_c=32000`、`nbit_c=15`。

关键常量：

[hdl/psi_common_pwm.vhd:L42-L43](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pwm.vhd#L42-L43) —— `ratio_c`、`nbit_c`，复用 u2-l1 的 `log2ceil`。

内部节拍源——直接例化 strobe_generator（u6-l1）：

[hdl/psi_common_pwm.vhd:L55-L62](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pwm.vhd#L55-L62) —— 在系统时钟上生成 `str_freq_g` 选通，`sync_i` 接 `trig_i` 做相位对齐。

主进程中的「窗口比较器」——PWM 输出就在这里决定：

[hdl/psi_common_pwm.vhd:L100-L107](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pwm.vhd#L100-L107) —— `cpt_period_s >= dly_s and cpt_period_s <= pwm_plus_dly_s and pwm_s /= 0` 时拉高 `dat_o`。

步进相位计数器与回绕：

[hdl/psi_common_pwm.vhd:L92-L99](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pwm.vhd#L92-L99) —— 选通为 1 时按 `cpt_inc_s`（=锁存的 `rate_i`）步进，到 `ratio_c - cpt_inc_s` 回绕。

`is_sync_g` 的两种 rate 刷新方式：

[hdl/psi_common_pwm.vhd:L81-L89](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pwm.vhd#L81-L89) —— `true` 在 `trig_i` 边沿刷新，`false` 连续刷新。

#### 4.3.4 代码实践

**实践目标**：在默认 generic（125 MHz 时钟、100 kHz 选通、`period_g=3.125` → `ratio_c=32000`）下，理解一个 PWM 周期的「时间尺度」。

**操作步骤**（源码阅读型实践）：

1. 算时间尺度：选通周期 \( T_{str} = 1/100\,\text{kHz} = 10\,\mu s \)；一个大周期含 32000 个选通拍，故 \( T_{period} = 32000 \times 10\,\mu s = 320\,\text{ms} \)。这与 `period_g=3.125` 对应的「3.125 周期/秒」一致（\( 1/0.32 \approx 3.125 \)）。
2. 设 `pwm_i=8000`、`dly_i=0`、`rate_i=1`：则占空比约 \( 8000/32000 = 25\% \)，高电平窗口占大周期的 1/4。
3. 阅读已有 TB `testbench/psi_common_pwm_tb/psi_common_pwm_tb.vhd`，观察它如何施加 `trig_i`/`rate_i`/`pwm_i`/`dly_i` 并检查 `dat_o` 波形。

**需要观察的现象**：

- `dat_o` 在每个大周期内的 `[dly, dly+pwm]` 相位窗口为高，其余为低。
- `vld_o` 以 `str_freq_g`（100 kHz）速率脉冲。
- `pwm_i=0` 时 `dat_o` 常 0（被 L102 的 `pwm_s /= 0` 条件屏蔽）。

**预期结果 / 待本地验证**：由于 `pwm` 当前无回归 `create_tb_run`，波形需用 `sim/interactive.tcl` 手工交互仿真确认（见 u1-l3）。

#### 4.3.5 小练习与答案

**练习 1**：默认 generic 下，`pwm_i=32000`、`dly_i=0`、`rate_i=1` 时 `dat_o` 几乎常高，为什么？

**答案**：窗口条件 `0 ≤ cpt ≤ 0+32000` 覆盖整个 `ratio_c=32000` 范围（除回绕点），所以几乎全周期为高（≈100% 占空比）。

**练习 2**：把 `rate_i` 从 1 改成 2，输出脉冲数会怎么变？

**答案**：相位计数器每拍前进 2 步，一个大周期内会走完两个相位循环，因此 `[dly, dly+pwm]` 窗口在一个 `period_g` 内出现两次——即脉冲数翻倍（倍频）。

**练习 3**：为什么 `is_sync_g=false` 时 `rate_i` 可以「运行中改变」，而 `true` 不行？

**答案**：`true` 时 `cpt_inc_s` 只在 `trig_i` 上升沿采样（L82–L85），两次触发之间保持不变，保证一个大周期内步进一致；`false` 时 `cpt_inc_s` 每拍都取 `rate_i`（L87–L89），允许在线调整，代价是可能在一个周期中间改变步进。

---

### 4.4 sample_rate_converter：无滤波采样率变换

#### 4.4.1 概念说明

采样率变换（sample rate conversion）回答的问题是：**把一个数据流的「有效样本率」按整数比升高或降低**。例如把 250 Msps 的 ADC 数据降到 1/512，或把一个低速流 8 倍插值到高速域。

`psi_common_sample_rate_converter` 用最朴素的方式做这件事——**完全不做滤波**：

- **抽取（DOWN，\( f_{out}=f_{in}/R \)）**：每 R 个有效输入样本，只输出 1 个（即「每 R 个选一个」）。
- **插值（UP，\( f_{out}=R\cdot f_{in} \)）**：每来 1 个有效输入样本，把它**重复**输出 R 次（零阶保持，zero-order hold）。

正因为不滤波，文档明确建议：抽取前加**抗混叠预滤波**，插值后加**平滑/插值后滤波**——本组件只负责「换节拍」，不负责「换频谱」。

#### 4.4.2 核心流程

```
DOWN 模式（每个 clk_i 上升沿，vld_i 驱动）：
  if vld_i = 1:
      if 样本计数 = R-1:        # 攒满 R 个
          dat_o ← dat_i；vld_o ← 1；计数 ← 0
      else:
          vld_o ← 0；计数 += 1
  else:
      vld_o ← 0

UP 模式（零阶保持重复）：
  特例：clk_to_vld_ratio_g = rate_g  → 直接透传（dat_o←dat_i, vld_o←1）
  一般：
      vld_i 上升沿 → 锁存 sample_s ← dat_i；输出计数 ← clk_to_vld_ratio_g/rate_g
      每拍：
          if 输出计数 > 0:  dat_o ← sample_s；vld_o ← 0；计数 -= 1
          elif 计数 = 0:    计数 ← clk_to_vld_ratio_g/rate_g；dat_o ← sample_s；vld_o ← 1
```

要点：

- DOWN 的「换节拍」靠数有效样本数，到 R-1 输出一个。
- UP 靠 `clk_to_vld_ratio_g`（时钟频率与输入 vld 频率之比）来安排「在哪些时钟拍上把同一样本重复送出」，使输出 vld 的频率是输入的 R 倍。
- `clk_to_vld_ratio_g = rate_g` 是「输入每个有效样本正好占一个输出重复槽」的退化情形，直接透传。

#### 4.4.3 源码精读

实体声明——`mode_g` 选 DOWN/UP，`rate_g` 是变换比：

[hdl/psi_common_sample_rate_converter.vhd:L10-L22](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sample_rate_converter.vhd#L10-L22) —— 注意 `clk_to_vld_ratio_g`（时钟/vld 频率比）这个 UP 模式专用参数。

DOWN 分支——每 R 个有效样本输出一个：

[hdl/psi_common_sample_rate_converter.vhd:L44-L58](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sample_rate_converter.vhd#L44-L58) —— `sample_count_s = rate_g-1` 时输出并清零，否则计数。

UP 分支——零阶保持重复：

[hdl/psi_common_sample_rate_converter.vhd:L60-L83](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sample_rate_converter.vhd#L60-L83) —— L62–L65 是透传特例；L66–L70 在 `vld_i` 上升沿锁存样本并设重复计数；L73–L82 按计数分发样本、周期性拉高 `vld_o`。

已有 TB 同时测 DOWN 与 UP，并**级联**（DOWN 输出喂给 UP 输入）：

[testbench/psi_common_sample_rate_converter_tb/psi_common_sample_rate_converter_tb.vhd:L79-L108](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_sample_rate_converter_tb/psi_common_sample_rate_converter_tb.vhd#L79-L108) —— `dut_dw_inst`（DOWN, R=512）+ `dut_up_inst`（UP, R=8），DOWN 的 `vld_o`/`dat_o` 直接作为 UP 的 `vld_i`/`dat_i`。

#### 4.4.4 代码实践

**实践目标**：验证「DOWN 输出 vld 频率 = 输入 vld 频率 / R」「UP 输出 vld 频率 = 输入 vld 频率 × R」。

**操作步骤**（源码阅读型实践）：

1. 打开 `testbench/psi_common_sample_rate_converter_tb/psi_common_sample_rate_converter_tb.vhd`。
2. 看常量（L15–L16）：`DOWNSAMPLE_RATE_c=512`、`UP_SAMPLING_RATE_c=8`。
3. 看 `count_valid_dw_proc`（L111–L131）：它在两个 `vld_o` 之间数 `vld_i` 的个数，断言这个数应在 `[R-1, R+1]` 之内（允许 ±1 抖动，因节拍对齐）。
4. 看 `count_valid_up_proc`（L134–L158）：同理对 UP 路径计数。

**需要观察的现象**：

- DOWN 路径：每出现一个输出 `vld_o`，期间约有 512 个输入 `vld_i`。
- UP 路径：每出现一个输出 `vldup_o`，期间约有 8 个输入（=DOWN 的 `vld_o`）。
- TB 用 250 MHz、1 MHz 正弦做激励（L12–L14, L63–L69），仅检查「节拍比」，**不**检查波形保真（因为本就无滤波）。

**预期结果 / 待本地验证**：两条 `count_ok`/`countup_ok` 保持 true，无 `###ERROR###`。由于该 TB 当前无回归条目，需本地交互仿真确认。

#### 4.4.5 小练习与答案

**练习 1**：用 DOWN 模式、`rate_g=4`，输入 vld 连续为 1，输入样本为 `10,20,30,40,50,60,...`。输出序列（在 `vld_o=1` 拍）是什么？

**答案**：每 4 个样本输出第 4 个，即 `40, 80, ...`（输出 `dat_i` 在 `sample_count_s=rate_g-1=3` 那拍，对应第 4、8、… 个样本）。

**练习 2**：为什么说本组件「无滤波」？对 DOWN 模式，缺预滤波会带来什么后果？

**答案**：它只做「选样本/重复样本」，没有任何抗混叠/插值滤波。DOWN 时若输入含高于 \( f_{out}/2 \) 的分量，下采样会把它混叠（alias）回基带，造成失真，故需预滤波。

**练习 3**：UP 模式里 `clk_to_vld_ratio_g = rate_g` 的「透传特例」在物理上意味着什么？

**答案**：意味着每个输入有效样本所跨的时钟周期数恰好等于要重复的次数 R，于是可以把该样本在每个槽位原样送出并每拍都有效——退化为「输入每来一个样本，输出立刻连续 R 拍有效」，无需复杂计数。

---

## 5. 综合实践

**任务**：搭建一条 **`prbs → find_min_max`** 的自检数据通路，用伪随机流验证统计组件。

**设计要点**：

1. 选 `psi_common_prbs`，`width_g=16`、种子取非零常数（例如 `0xACE1`）。
2. 把 `prbs` 的 `vld_o` 接到 `find_min_max` 的 `vld_i`，`dat_o` 接 `dat_i`；`find_min_max` 配 `mode_g=>"MAX"`、`signed_g=false`、`width_g=16`。
3. 用一个计数器在 1000 个 `vld` 后产生一个 `raz_i` 单周期脉冲。
4. 仿真方式有两种：
   - **纯阅读型**：参考 `psi_common_prbs_tb`（用参考模型比对）和 `psi_common_find_min_max_tb`（在 TB 里软件维护参考极值）的写法，在 TB 里**用软件**复现 LFSR 序列并算出 1000 个样本的无符号最大值，与 `find_min_max` 在 `raz` 后的 `dat_o` 比对。
   - **运行型（待本地验证）**：把这条链路写成一个新 TB，登记进 `sim/config.tcl`（参照 u1-l3 的 `create_tb_run` 写法），用 PsiSim 跑回归。

**预期结果**：

- `find_min_max` 的 `run_dat_o` 在 1000 拍内单调逼近该段 PRBS 序列的无符号最大值。
- `raz` 上升沿后一拍，`dat_o` 等于软件参考算出的最大值，比对通过。

**进阶**：把 `find_min_max` 换成 `min_max_sum`（同种子、同窗长），验证 `min_o`/`max_o`/`sum_o` 三路同时正确，并思考 `sum_o / 1000` 是否等于该段均值（注意 `clock_cycle_g` 计的是有效样本数）。

## 6. 本讲小结

- **`find_min_max`** 是流式极值跟踪积木：`vld_i` 选通比较、`raz_i` 上升沿锁存窗口结果并重置；同时提供持续更新的 `run_dat_o` 运行输出；`mode_g`/`signed_g` 在综合期切四套比较。
- **`min_max_sum`** = 两个 `find_min_max`（MIN+MAX 例化复用）+ 一个窗口累加器，在 `clock_cycle_g` 个有效样本或 `sync_i` 处给出 min/max/向量和；`assert` 保护累加器位宽。
- **`prbs`** 是最长 LFSR 伪随机源：多项式抽头异或反馈、周期 \( 2^{N}-1 \)、全零种子死锁、`vld_i` 选通移位、序列确定可复现。
- **`pwm`** 是步进相位计数器 + 窗口比较器：`pwm_i` 定脉宽、`dly_i` 定延时、`rate_i` 定倍频，内部例化 `strobe_generator`，`is_sync_g` 决定参数是否与 `trig_i` 同步刷新。
- **`sample_rate_converter`** 是无滤波整数倍抽取/插值：DOWN「每 R 选一」、UP「零阶保持重复 R 次」；文档建议 DOWN 前加预滤波、UP 后加平滑滤波。
- **风格差异**：这五个组件都没用 u7-l1 的二进程 record 范式，而是轻量单进程；`pwm` 与 `sample_rate_converter` 暂无回归 TB（仅编译），相关运行结论需本地验证。

## 7. 下一步学习建议

- 想把这些统计组件接到带反压的数据通路上，回去重读 **u7-l1（pl_stage）** 与 **u4-l1（sync_fifo）**，用 FIFO/pl_stage 把 vld-only 的统计端包成 AXI-S。
- 想深入了解 PWM 所依赖的节拍机制，看 **u6-l1（strobe_generator / strobe_divider）**；想看更「脉冲整形向」的同类组件，看 **u10-l2（pulse_shaper）**。
- 想把 PRBS 用到完整的链路自检里（发端 PRBS → 信道 → 收端比对），可结合 **u11-l1（自校验测试平台）** 编写带 `###ERROR###` 约定的回归 TB。
- 继续本单元的其余杂项组件：仲裁器（u10-l1）、看门狗/消抖/动态移位（u10-l3）。
