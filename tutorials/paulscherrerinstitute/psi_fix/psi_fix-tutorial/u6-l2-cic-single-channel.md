# CIC 单通道抽取与插值

## 1. 本讲目标

u6-l1 建立了 CIC 的「原理 + 命名 + 增益校正」全景图，但**故意没有展开任何 RTL 实现细节**。本讲就把那张图落地到最简单的两个单通道组件——抽取器 `psi_fix_cic_dec_fix_1ch` 与插值器 `psi_fix_cic_int_fix_1ch`——逐级走通它们的真实数据流与握手时序。

学完本讲，读者应该能够：

- 画出抽取 CIC「积分器（高速）→ ↓R → 梳状器（低速）」在 RTL 里的逐级数据流，并说清每一级在 record 里对应哪个字段。
- 画出插值 CIC「梳状器（低速）→ ↑R 插零 → 积分器（高速）」的**镜像**数据流，并解释为什么它的握手比抽取器复杂得多。
- 解释 `ratio_g` / `order_g` / `diff_delay_g` 三个 generic 如何塑形硬件（累加器级数、抽取计数器宽度、差分延时寄存器数量）。
- 读懂 `config.tcl` 里 `cic_int_fix_1ch_tb` 的多组参数，说清 `in_idle_cycles_g`（输入饥饿）与 `out_idle_cycles_g`（输出阻塞）各自模拟了哪种握手压力场景。

本讲**不重复** u6-l1 已讲透的 Hogenauer 位增长公式、命名规则与增益校正常量（`AccuFmt`/`DiffFmt`/`Gc_c` 等），只在用到时引用；本讲的精力全部放在「数据怎么流、valid/ready 怎么握手」。

## 2. 前置知识

进入本讲前，读者应当已经掌握（见前置讲义摘要）：

- **CIC 原理与位增长**（u6-l1）：抽取器 = 高速 N 级积分 → ↓R → 低速 N 级梳状；直流增益 \(G=(R\cdot M)^N\)，累加器整数位 +\(B_{\text{add}}=\lceil\log_2 G\rceil\)；梳状器格式比输出多 `order+1` 个分数位。本讲直接复用这些结论。
- **增益校正两级**（u6-l1）：粗校正（右移 `Shift_c` 位，恒做）+ 精校正（乘 `Gc_c`，`auto_gain_corr_g=True` 时）。
- **两段式编码**（u3-l3）：`two_process_r` record + 组合进程 `p_comb`（`v := r` 保持默认）+ 时序进程 `p_seq`；valid 用数组住进 record、用切片平移逐级传递。
- **AXI4-Stream 握手**（u1-l4）：`vld` 与 `rdy` 同拍为高才完成传递。
- **位真双模型**（u2-l3、u3-l2）：VHDL 与 Python 两侧逐位一致，由 `psi_fix_get_bits_as_int` 把定点值写成位模式有符号整数、用 `###ERROR###` 约定逐位比对。

三个记号沿用 u6-l1：\(R\)（ratio）、\(M\)（diff_delay，1 或 2）、\(N\)（order）。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| [hdl/psi_fix_cic_dec_fix_1ch.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_fix_1ch.vhd) | 单通道固定比率抽取 CIC，**本讲主样板之一**（无反压握手） |
| [hdl/psi_fix_cic_int_fix_1ch.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_int_fix_1ch.vhd) | 单通道固定比率插值 CIC，**本讲主样板之二**（双向 AXI-S 握手） |
| [model/psi_fix_cic_dec.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_cic_dec.py) | 抽取 CIC 位真 Python 模型（黄金参考） |
| [model/psi_fix_cic_int.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_cic_int.py) | 插值 CIC 位真 Python 模型（结构与抽取镜像） |
| [sim/config.tcl](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl) | 回归脚本，注册了两个 TB 的参数矩阵（含握手压力参数） |
| [testbench/psi_fix_cic_int_fix_1ch_tb/psi_fix_cic_int_fix_1ch_tb.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_cic_int_fix_1ch_tb/psi_fix_cic_int_fix_1ch_tb.vhd) | 插值 CIC 自检测试台，注入输入饥饿/输出阻塞 |
| [testbench/psi_fix_cic_dec_fix_1ch_tb/psi_fix_cic_dec_fix_1ch_tb.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_cic_dec_fix_1ch_tb/psi_fix_cic_dec_fix_1ch_tb.vhd) | 抽取 CIC 自检测试台，只注入输入间隙 |

> 一句话定位：抽取器与插值器是同一套 CIC 数学的**两种速率变换方向**，RTL 上则是「积分-梳状顺序相反 + 移位点不同 + 握手复杂度天差地别」。

## 4. 核心概念与源码讲解

### 4.1 抽取 CIC 数据流

#### 4.1.1 概念说明

抽取 CIC 把采样率从 \(F_s\) 降到 \(F_s/R\)。它的 RTL 结构严格遵循 u6-l1 的三段式：**先在高速时钟域（\(F_s\)）跑 N 级积分器，再在抽取点 ↓R 并完成粗校正移位，最后在低速时钟域（\(F_s/R\)）跑 N 级梳状器**。

这里要特别强调抽取器的**接口形态**——它**没有任何 ready 信号**：

[psi_fix_cic_dec_fix_1ch.vhd:25-33](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_fix_1ch.vhd#L25-L33) —— 输入只有 `dat_i`/`vld_i`，输出只有 `dat_o`/`vld_o`，外加一个状态位 `busy_o`。没有 `rdy_o`、没有 `rdy_i`。原因是：输入端跑在高速 \(F_s\)，积分器对每个有效样本只是做一次加法，**永远来得及接收**（不存在会溢出的输入缓冲），所以无需向源头反压；输出端跑在低速 \(F_s/R\)，输出速率足够慢，约定下游**必须即时接收**每一个 `vld_o` 脉冲。这是一种有意的简化——抽取器是「单向数据流」。

#### 4.1.2 核心流程

抽取器内部数据流（对应 `p_comb` 的 stage 划分）：

```
dat_i/vld_i @ Fs
   │  [Stage Accu 0] 输入寄存 Input_0，打 vld
   ▼
┌──────────────────────────┐
│ [Stage Accu 1..N]         │  N 级积分器，每级 y[k]=y[k-1]+x[k]
│  Accu(1..order)           │  运行在 Fs（每来一个 vld 累加一次）
│  仅在 VldAccu(stage)='1'  │  累加器格式 AccuFmt_c（整数位 +B_add）
│  时更新（防虚假累加）      │
└──────────────────────────┘
   │  @ Fs
   ▼
┌──────────────────────────┐
│ [Stage Diff 0] 抽取 ↓R    │  Rcnt 计数：每 R 个有效样本放行 1 个
│  + 粗校正移位 Shift_c      │  放行时 psi_fix_shift_right(Accu(N))
│  Rcnt: 0→ratio_g-1 循环   │  → DiffIn_0（进入低速域）
└──────────────────────────┘
   │  @ Fs/R
   ▼
┌──────────────────────────┐
│ [Stage Diff 1..N]         │  N 级梳状器，每级 y[k]=x[k]-x[k-M]
│  DiffVal(1..order)        │  运行在 Fs/R；M=diff_delay_g
│  DiffLast / DiffLast2     │  M=1 用 DiffLast，M=2 再加 DiffLast2
└──────────────────────────┘
   │
   ▼
┌──────────────────────────┐
│ [GC 0..2] 增益校正        │  auto_gain_corr_g=True 时三级流水
│  resize→mult→resize       │  （u6-l1 已详述）
└──────────────────────────┘
   │
   ▼
dat_o/vld_o @ Fs/R        busy_o = 流水线非空
```

三个参数如何塑形这段硬件：

| 参数 | 作用于 | 硬件影响 |
|------|--------|----------|
| `ratio_g` (R) | 抽取计数器 `Rcnt`、增益 `CicGain_c`、移位 `Shift_c` | `Rcnt` 范围 `0..ratio_g-1`；R 越大累加器整数位越多 |
| `order_g` (N) | 积分器/梳状器级数、valid 数组宽度 | `Accu`/`DiffVal` 数组长度 = N；`VldAccu`/`VldDiff` 宽度 = N+1 |
| `diff_delay_g` (M) | 梳状器延时寄存器 | M=1 用 `DiffLast`；M=2 额外用 `DiffLast2`，梳状减 2 拍前的值 |

#### 4.1.3 源码精读

**record 把整条流水线装进一个类型**。这是读懂抽取器的钥匙——每个字段对应一级寄存器：

[psi_fix_cic_dec_fix_1ch.vhd:52-75](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_fix_1ch.vhd#L52-L75) —— 注意三段划分：`Accu` 段（`Input_0`、`VldAccu`、`Accu(1..order)`、`Rcnt`）、`Diff` 段（`DiffIn_0`、`VldDiff`、`DiffVal`/`DiffLast`/`DiffLast2`）、`GC` 段（`GcVld`、`GcIn_0`/`GcMult_1`/`GcOut_2`）。`VldAccu`/`VldDiff`/`GcVld` 都是 `std_logic_vector`，用数组下标表示流水级号——这正是 u3-l3 的「valid 住进 record」手法。

**valid 流水用切片整体平移**。每拍先把三段 valid 各前移一级：

[psi_fix_cic_dec_fix_1ch.vhd:90-93](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_fix_1ch.vhd#L90-L93) —— `v.VldAccu(low+1..high) := r.VldAccu(low..high-1)` 这类切片赋值让 valid 随数据逐级下传，无需逐级手写。

**积分器：第一级 + 循环展开剩余级**。第一级积分器把输入加到累加器上；其余级用一个 `for stage` 循环展开：

[psi_fix_cic_dec_fix_1ch.vhd:100-115](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_fix_1ch.vhd#L100-L115) —— 关键守卫是 `if r.VldAccu(stage)='1'`：**只有在有有效数据时才累加**，否则保持原值。这保证了输入出现间隙（`vld_i` 为 0 的拍）时积分器不会拿 0 当数据累加。所有累加都用 `psi_fix_add`，结果格式 `AccuFmt_c`（u6-l1 推导的带位增长格式），wrap 回绕即二进制补码模运算。

**抽取点：Rcnt 计数 + 粗校正移位一次完成**。这是抽取器的核心节拍：

[psi_fix_cic_dec_fix_1ch.vhd:117-128](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_fix_1ch.vhd#L117-L128) —— 每当最后一级积分器输出有效（`r.VldAccu(order_g)='1'`），`Rcnt` 从 `ratio_g-1` 递减到 0；**仅在 `Rcnt=0` 的那一拍**把积分结果右移 `Shift_c` 位（即粗校正，u6-l1）送入 `DiffIn_0` 并拉高 `VldDiff(0)`——这相当于「每 R 个有效样本放行 1 个」实现 ↓R。注意 `VldDiff(0)` 默认置 0，只在放行拍置 1，因此梳状器天然运行在 \(F_s/R\)。

**梳状器：diff_delay 1 与 2 的两种实现**。这解释了为什么 record 里同时有 `DiffLast` 和 `DiffLast2`：

[psi_fix_cic_dec_fix_1ch.vhd:130-144](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_fix_1ch.vhd#L130-L144) —— `M=1` 时减 `DiffLast(1)`（1 拍前的值，即 \(y[k]=x[k]-x[k-1]\)）；`M=2` 时减 `DiffLast2(1)`（2 拍前的值，\(y[k]=x[k]-x[k-2]\)），并把 `DiffLast` 顺移进 `DiffLast2` 维护延时链。后续级（`for stage in 1 to order_g-1`）在 [L148-162](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_fix_1ch.vhd#L148-L162) 用完全相同的模式展开。

**busy_o：流水线非空指示**。抽取器没有握手，但提供一个状态位告诉外部「是否还在算」：

[psi_fix_cic_dec_fix_1ch.vhd:176-181](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_fix_1ch.vhd#L176-L181) —— 只要任一段 valid 数组非零，`CalcOngoing='1'`，`busy_o` 随之拉高。这在「等滤波器排空后再改配置」等场景有用（多通道/可配置变体尤其依赖它，见 u6-l3）。

**复位只清状态、不清数据通路**（承接 u3-l3 的选择性同步复位）：

[psi_fix_cic_dec_fix_1ch.vhd:211-221](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_fix_1ch.vhd#L211-L221) —— 复位只把各 valid 数组、`Accu`、`Rcnt`、`DiffLast` 等清零；数据通路寄存器（`Input_0`、`DiffIn_0`、`GcIn_0` 等）不复位，靠首个有效数据自然覆盖。

#### 4.1.4 代码实践

**实践目标**：跟踪抽取器如何处理「输入带间隙」的场景，理解为什么它不需要 `rdy_o`。

**操作步骤**：

1. 打开抽取器测试台 [psi_fix_cic_dec_fix_1ch_tb.vhd:110-124](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_cic_dec_fix_1ch_tb/psi_fix_cic_dec_fix_1ch_tb.vhd#L110-L124)。观察 `p_input` 进程：每写一个样本后，把 `InVld<='0'` 并空转 `idle_cycles_g` 拍。
2. 对照 [config.tcl:253-256](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L253-L256)，注意第 1 组参数 `-gidle_cycles_g=5`（每样本后插 5 个空拍），其余组 `-gidle_cycles_g=0`（满速输入）。
3. 回到 DUT [psi_fix_cic_dec_fix_1ch.vhd:100-115](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_fix_1ch.vhd#L100-L115)，确认积分器只在 `VldAccu(stage)='1'` 时累加——空拍期间 `vld_i=0` → `VldAccu(0)=0` → 各级积分器保持原值，抽取计数器 `Rcnt` 也不递减。

**需要观察的现象**：

- 即使输入每隔 5 拍才来一个样本，积分器**只对真实样本累加**，`Rcnt` 只对真实样本计数，因此抽取比 R 仍精确为「每 R 个**有效**样本输出 1 个」——与满速输入在数学上等价（只是耗时更长）。
- 这正是抽取器**不需要 `rdy_o`** 的原因：它能无条件接收任意节拍的输入，靠 `vld_i` 门控自然处理间隙。

**预期结果**：`idle_cycles_g=5` 与 `idle_cycles_g=0` 两组 TB 的 `output_*.txt` 比对结果应**完全一致**（位真模型不受输入节拍影响，仅看有效样本序列）。

**待本地验证**：上述「两组输出一致」的结论需实跑回归确认；位真 Python 模型 `psi_fix_cic_dec.Process()` 的输入就是纯样本数组、不含时间节拍，从原理上保证了这一点。

#### 4.1.5 小练习与答案

**练习 1**：抽取器为什么把粗校正移位（`Shift_c`）放在**抽取点**（Stage Diff 0），而不是放在最末端的增益校正段？

**参考答案**：因为移位的本质是丢掉低 \(B_{\text{add}}\) 位。在抽取点移位，可以**让后续所有梳状器和增益校正都在更窄的 `DiffFmt_c` 上运行**，省下大量寄存器与布线。若拖到末端才移位，整条低速梳状链都得按宽累加器格式 `AccuFmt_c`（可能 90+ 位）来算，资源浪费严重。这也是 u6-l1 强调「粗校正免费」的工程体现——它不仅是免费乘法器，更是免费缩窄了低速通路。

**练习 2**：`diff_delay_g=2` 时，record 里的 `DiffLast2` 数组起什么作用？能否把它与 `DiffLast` 合并？

**参考答案**：`DiffLast2` 保存**两拍前**的梳状输入，使梳状器实现 \(y[k]=x[k]-x[k-2]\)（M=2）。它与 `DiffLast`（一拍前）共同构成一条 2 级延时链：每拍 `DiffLast2(stage):=DiffLast(stage)`、`DiffLast(stage):=当前输入`，于是 `DiffLast2` 永远滞后 `DiffLast` 一拍。二者**不能合并**：M=2 需要同时访问 1 拍前和 2 拍前的两个不同值，必须用两个独立寄存器。M=1 时 `DiffLast2` 不被赋值（综合期死代码消除）。

### 4.2 插值 CIC 数据流

#### 4.2.1 概念说明

插值 CIC 把采样率从 \(F_s\) 升到 \(F_s\cdot R\)，结构与抽取器**镜像**：**先在低速时钟域（\(F_s\)）跑 N 级梳状器，再在插值点 ↑R 插零，最后在高速时钟域（\(F_s\cdot R\)）跑 N 级积分器**。

两个关键差异要在读源码前先记住：

1. **移位点不同**。抽取器在「中间」（抽取点）移位；插值器在「末端」（积分器之后）移位。因为插值器的梳状器跑在低速、积分器跑在高速，粗校正移位只能放在高速积分链的输出端。这导致插值器多了 `ShiftInFmt_c`/`ShiftOutFmt_c` 两个中间格式。
2. **直流增益少了因子 R**。u6-l1 的练习已解释：插零使积分器每 R 个样本里只累加 1 个非零值，净增益 \(G=(R\cdot M)^N/R\)，体现在 [psi_fix_cic_int_fix_1ch.vhd:40](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_int_fix_1ch.vhd#L40) 的 `((ratio*diffDelay)**order)/ratio`。

最重要的差异在**接口**——插值器有**完整的双向 AXI-S 握手**：

[psi_fix_cic_int_fix_1ch.vhd:26-35](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_int_fix_1ch.vhd#L26-L35) —— 输入侧 `dat_i`/`vld_i`/**`rdy_o`**（向源头反压），输出侧 `dat_o`/`vld_o`/**`rdy_i`**（受下游反压）。注意这里的方向命名：`rdy_o` 是**输出端口**（本组件产生、告诉源头「我准备好了」），`rdy_i` 是**输入端口**（下游产生、告诉本组件「下游准备好了」）。这与 u5-l3 里 `cordic_vect` 把 `rdy_i` 改名 `rdy_o` 的修正同理——对接收数据的实体，TREADY 是它对外输出的。

> 为什么插值器需要双向握手、抽取器不需要？因为插值器的**输出跑在 R 倍高速**（同一时钟下每拍都可能出结果），若下游来不及取（`rdy_i=0`），高速积分链必须整体停摆；而停摆期间又不能接收新输入，于是必须用 `rdy_o` 向源头反压。抽取器输出是低速的，下游默认总能即时接收，故无需此机制。

#### 4.2.2 核心流程

插值器内部数据流（注意与 4.1.2 的镜像关系）：

```
dat_i/vld_i/rdy_o @ Fs          （低速输入，受 InRdy 反压）
   │  [Stage Diff 0] 输入寄存 Input_0，注册 rdy（Rdy_0）
   ▼
┌──────────────────────────┐
│ [Stage Diff 1..N]         │  N 级梳状器，运行在 Fs（低速）
│  DiffVal(1..order)        │  DiffFmt_c 基于 in_fmt（整数位 +order+1）
└──────────────────────────┘
   │  @ Fs
   ▼
┌──────────────────────────┐
│ [Stage Accu 0] 插零 ↑R    │  Rcnt: 0→ratio_g 循环
│  + 速率提升到 Fs·R        │  Rcnt=0 拍装真实 DiffVal(order)
│  AccuIn_0                 │  其余拍 AccuIn_0:=0（插零）
└──────────────────────────┘
   │  @ Fs·R（高速）
   ▼
┌──────────────────────────┐
│ [Stage Accu 1..N]         │  N 级积分器，运行在 Fs·R（高速）
│  Accu(1..order)           │  对「真实样本 + R-1 个零」累加
└──────────────────────────┘
   │  纯连线移位
   ▼  Sft_v = shift_right(Accu(order), Shift_c)
┌──────────────────────────┐
│ [GC 0..4] 增益校正        │  auto_gain_corr_g=True 时五级流水
└──────────────────────────┘
   │
   ▼
dat_o/vld_o/rdy_i @ Fs·R   （高速输出，受 OutRdy 反压）
```

插零是插值器的灵魂：每接收 1 个低速输入样本，`Rcnt` 控制「装 1 个真实值 + 插 \(R-1\) 个零」共 R 拍，喂给高速积分链——于是输出端得到 R 个样本（其中 R-1 个是阶梯插值的结果）。这对应 Python 模型里的零插入：

[psi_fix_cic_int.py:86-90](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_cic_int.py#L86-L90) —— 把梳状输出 `diffOut` 放进一个 `(ratio, N)` 零矩阵的第 0 行，再按列重排（`"F"`），得到「1 个真实 + R-1 个零」交替的序列。RTL 用计数器 `Rcnt` 在时域上完成等价操作。

#### 4.2.3 源码精读

**握手控制信号先于一切算出**。插值器 `p_comb` 的前 30 行都在算「本拍流水线能否前进」，这是双向握手的关键：

[psi_fix_cic_int_fix_1ch.vhd:102-115](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_int_fix_1ch.vhd#L102-L115) —— 两个局部变量：

- `OutRdy_v`：输出侧能否前进。当「没有待输出数据」(`r.OutVld='0'`) **或**「下游准备好」(`rdy_i='1'`) 时为 `'1'`。注释点明这是遵循 AXI-S 规范——**valid 不允许等待 ready**，所以只有「已有结果占着输出」且「下游没准备好」时才停摆。
- `InRdy_v`：输入侧能否接收。当 `Rcnt=0`（一个插值周期刚结束、可以接新样本）**或**（`Rcnt=1` 且 `OutRdy_v='1'`，即插值周期最后一拍且输出不阻塞）时为 `'1'`。

这两个变量随后**门控所有流水级的前移**：

[psi_fix_cic_int_fix_1ch.vhd:117-124](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_int_fix_1ch.vhd#L117-L124) —— 梳状段 valid（`VldDiff`）的前移受 `InRdy_v` 门控；积分段与增益校正段（`VldAccu`、`GcVld`）的前移受 `OutRdy_v` 门控。这实现了「输入停摆只冻结梳状段、输出停摆冻结整条高速段」的精细反压。

**输入 ready 做成寄存器输出（Rdy_0）**。AXI-S 要求 `ready` 不能是纯组合的长链路，故用一个寄存器 `Rdy_0` 打破组合路径：

[psi_fix_cic_int_fix_1ch.vhd:126-135](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_int_fix_1ch.vhd#L126-L135) —— 当 `Rdy_0='1'` 且 `vld_i='1'`（一次成功握手）时锁存输入、把 `Rdy_0` 拉低一拍；否则在允许前进时重新置 `'1'`。最终 `rdy_o <= r.Rdy_0`（[L245](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_int_fix_1ch.vhd#L245)）输出的是**寄存器后的** ready，时序友好。

**插零计数器（Stage Accu 0）**。这是插值器区别于抽取器的核心逻辑：

[psi_fix_cic_int_fix_1ch.vhd:175-186](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_int_fix_1ch.vhd#L175-L186) —— 三分支：

1. `Rcnt=0` 且有新梳状结果 → 装真实值 `AccuIn_0 := resize(DiffVal(order))`，`Rcnt:=ratio_g`，拉高 `VldAccu(0)`；
2. `Rcnt=1` 且输出不阻塞且无新结果 → 这一拍不喂积分器（`VldAccu(0):='0'`），`Rcnt` 继续递减；
3. 其余 `Rcnt≠0` 且输出不阻塞的拍 → `AccuIn_0 := (others=>'0')`（**插零**），`Rcnt` 递减。

于是一个输入样本被展开成「1 个真实值 + 若干零」的高速序列送入积分链。所有分支都受 `OutRdy_v` 门控——下游一旦反压，插零与累加全部暂停。

**移位是纯连线（无寄存器）**。与抽取器在抽取点移位不同，插值器在积分链末端用一个组合移位把粗校正折叠进去：

[psi_fix_cic_int_fix_1ch.vhd:205](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_int_fix_1ch.vhd#L205) —— `Sft_v := psi_fix_shift_right(r.Accu(order_g), AccuFmt_c, Shift_c, Shift_c, ShiftOutFmt_c)`，结果 `Sft_v` 直接进增益校正段，不再单独打拍。

**增益校正段比抽取器多两级**。因为插值器输出在高速域、且 `rdy_i` 反压会让流水线停摆，增益校正用了 5 级（`GcVld(0..4)`，`GcIn_0/1/2`、`GcMult_3`、`GcOut_4`），见 [psi_fix_cic_int_fix_1ch.vhd:207-229](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_int_fix_1ch.vhd#L207-L229)，全部受 `OutRdy_v` 门控。数学内容（resize→mult→resize）与抽取器一致（u6-l1）。

**复位把 `Rdy_0` 置 1**（初始即声明「准备好接收」）：

[psi_fix_cic_int_fix_1ch.vhd:259-268](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_int_fix_1ch.vhd#L259-L268) —— 注意末行 `r.Rdy_0 <= '1'`，保证复位释放后第一个周期能立即握手接收输入。

#### 4.2.4 代码实践

**实践目标**：对照 RTL 与 Python 模型，确认「插零」在两侧的实现等价。

**操作步骤**：

1. 在 Python 模型里定位插零：[psi_fix_cic_int.py:86-90](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_cic_int.py#L86-L90)。`interpol[0]=diffOut` 把梳状输出放在第 0 行，其余 `ratio-1` 行是零；`reshape(...,"F")` 按列重排成「真、0、0、…、真、0、0、…」的一维序列。
2. 在 RTL 里定位插零：[psi_fix_cic_int_fix_1ch.vhd:175-186](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_int_fix_1ch.vhd#L175-L186) 的第 3 分支 `v.AccuIn_0 := (others => '0')`。
3. 取 `config.tcl` 第 1 组参数 `ratio_g=10`（[config.tcl:264](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L264)）：每 1 个低速输入样本应展开为 10 拍高速序列（1 真 + 9 零）。

**需要观察的现象**：

- Python 侧 `interpol` 序列长度 = 输入样本数 × R；其中非零位置恰为 `0, R, 2R, …`。
- RTL 侧 `Rcnt` 从 `ratio_g` 递减到 0，期间第 1 拍装真值、后续拍装零，与 Python 的「1 真 + (R-1) 零」逐位对应。

**预期结果**：两侧插零模式一致，故积分器输出（阶梯状保持）位真相等——这正是 `output_*.txt` 能逐行比对的根基。

**待本地验证**：可在 `model/` 下实例化 `psi_fix_cic_int(order=3, ratio=10, ...)`，对一个单位冲激输入打印 `Process()` 中间序列，肉眼确认零插入位置（需先按 u1-l1 摆好 `en_cl_fix` 依赖）。

#### 4.2.5 小练习与答案

**练习 1**：插值器的增益校正段（5 级）比抽取器（3 级）多两级，为什么？

**参考答案**：两个原因叠加。其一，插值器输出跑在 R 倍高速，需要更深的流水来满足 Fmax 时序约束；其二，插值器带 `rdy_i` 反压，增益校正段必须能随 `OutRdy_v` 停摆与恢复，多几级寄存器让反压控制更稳定（避免组合路径穿越移位与乘法）。两级数量差异不影响数学结果——两侧的 resize→mult→resize 是等价的，只是寄存器切分粒度不同。

**练习 2**：为什么插值器的 `DiffFmt_c` 基于 `in_fmt_g`，而抽取器的 `DiffFmt_c` 基于 `out_fmt_g`（见 u6-l1）？

**参考答案**：因为梳状器在两类组件里**所在的时钟域不同**。抽取器梳状器在低速**输出**域，其输入来自已被移位的累加器，故按输出格式定 `DiffFmt`；插值器梳状器在低速**输入**域，直接处理输入样本，故按输入格式定 `DiffFmt`（`in_fmt_g.I+order+1` 个整数位）。两者都遵循「梳状器格式比其所在域的数据多 `order+1` 个分数位」的同一条规则，只是基准（输入/输出）随方向而变。

### 4.3 握手与参数

#### 4.3.1 概念说明

抽取器与插值器用**同一组三个 generic**（`ratio_g`/`order_g`/`diff_delay_g`）描述数学，但**握手复杂度天差地别**。本模块把两者的接口差异和测试覆盖策略讲清楚——这是 `config.tcl` 参数矩阵设计的依据，也是本讲指定的核心实践任务。

核心对照：

| 维度 | 抽取器 `cic_dec_fix_1ch` | 插值器 `cic_int_fix_1ch` |
|------|--------------------------|--------------------------|
| 输入速率 | 高速 \(F_s\) | 低速 \(F_s\) |
| 输出速率 | 低速 \(F_s/R\) | 高速 \(F_s\cdot R\) |
| 输入握手 | 仅 `vld_i`（无 `rdy_o`） | `vld_i` + `rdy_o`（可反压源头） |
| 输出握手 | 仅 `vld_o`（无 `rdy_i`，下游须即时取） | `vld_o` + `rdy_i`（受下游反压） |
| 状态位 | `busy_o`（流水线非空） | 无（握手已表达忙/闲） |
| 移位点 | 抽取点（中段） | 积分末端 |
| 直流增益 | \((R\cdot M)^N\) | \((R\cdot M)^N/R\) |

一句话总结：**输出跑高速的那一侧（插值器输出 / 抽取器输入）不需要反压，输出跑低速或会被下游拖慢的那一侧必须配反压**。抽取器高速侧是输入（源头总能被无条件接收），故无 `rdy_o`；插值器高速侧是输出（必须能被下游反压），故有 `rdy_i`，并连带要求输入侧也有 `rdy_o` 以反压源头。

#### 4.3.2 核心流程

两个 generic 的握手测试覆盖矩阵（对应插值器 TB 的设计意图，注释见 [config.tcl:263](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L263)）：

| 握手参数 | 注入位置 | 模拟的场景 |
|----------|----------|------------|
| `in_idle_cycles_g` | 输入进程：每接收一个样本后，空转 N 拍再发下一个 | **输入饥饿**（input starving）：上游供数慢，`vld_i` 周期性缺失 |
| `out_idle_cycles_g` | 输出进程：每比对一个样本后，把 `rdy_i` 拉低 N 拍 | **输出阻塞**（output blocked）：下游消费慢，对 DUT 反压 |

抽取器只有一个 `idle_cycles_g`（输入间隙），因为它没有输出反压通道——这正反映了「抽取器无 `rdy_i`」的接口事实。

#### 4.3.3 源码精读

**插值器 TB 如何注入两种握手压力**。`p_input` 进程在每次成功握手后空转 `in_idle_cycles_g` 拍：

[psi_fix_cic_int_fix_1ch_tb.vhd:120-131](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_cic_int_fix_1ch_tb/psi_fix_cic_int_fix_1ch_tb.vhd#L120-L131) —— `wait until rising_edge(Clk) and InRdy='1'` 完成一次握手，随后 `for c in 0 to in_idle_cycles_g-1 loop` 空转（期间保持 `InVld='0'`），制造输入饥饿。

`p_output` 进程在每次比对后把 `OutRdy` 拉低 `out_idle_cycles_g` 拍：

[psi_fix_cic_int_fix_1ch_tb.vhd:155-164](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_cic_int_fix_1ch_tb/psi_fix_cic_int_fix_1ch_tb.vhd#L155-L164) —— 比对完一个样本，`for c in 0 to out_idle_cycles_g-1 loop` 内 `OutRdy<='0'`，强制 DUT 进入输出阻塞、触发其内部 `OutRdy_v='0'` 冻结高速段、并通过 `rdy_o` 向源头反压。

**`config.tcl` 用参数矩阵遍历所有握手组合**。6 组参数刻意覆盖「无压力 / 输入饥饿 / 输出阻塞 / 二者兼有」：

[config.tcl:263-269](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L263-L269) —— 第 263 行的注释 `"input starving, output blocked, both"` 直接点明设计意图。注意第 4 组 `in_idle_cycles_g=20` 是重度输入饥饿、第 6 组 `out_idle_cycles_g=20` 是重度输出阻塞——这两个极端值用来逼出反压逻辑的边界 bug。

**抽取器 TB 只测输入间隙**：

[psi_fix_cic_dec_fix_1ch_tb.vhd:113-123](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_cic_dec_fix_1ch_tb/psi_fix_cic_dec_fix_1ch_tb.vhd#L113-L123) —— 只有 `idle_cycles_g` 一个参数，每样本后 `InVld<='0'` 空转。输出侧 [L148-154](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_cic_dec_fix_1ch_tb/psi_fix_cic_dec_fix_1ch_tb.vhd#L148-L154) 仅 `wait until OutVld='1'` 即取走，无任何反压——呼应抽取器无 `rdy_i`。

> 命名提示：`config.tcl` 里写成 `-gin_idle_cycles_g`/`-gout_idle_cycles_g`，前缀 `-g` 是仿真器传 generic 的开关，真实 generic 名是 `in_idle_cycles_g`/`out_idle_cycles_g`（见 TB 实体 [psi_fix_cic_int_fix_1ch_tb.vhd:24-25](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_cic_int_fix_1ch_tb/psi_fix_cic_int_fix_1ch_tb.vhd#L24-L25)）。

#### 4.3.4 代码实践（本讲核心任务）

**实践目标**：阅读 `config.tcl` 中 `cic_int_fix_1ch_tb` 的 6 组参数，说明 `in_idle_cycles_g`（即 `gin_idle_cycles_g`）与 `out_idle_cycles_g`（即 `gout_idle_cycles_g`）分别模拟了哪种握手压力场景，并按场景归类这 6 组参数。

**操作步骤**：

1. 打开 [config.tcl:260-270](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L260-L270)，逐行读 6 组 `-g...` 参数。
2. 对照 TB 注入逻辑 [psi_fix_cic_int_fix_1ch_tb.vhd:120-131](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_cic_int_fix_1ch_tb/psi_fix_cic_int_fix_1ch_tb.vhd#L120-L131)（输入饥饿）与 [L155-164](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_cic_int_fix_1ch_tb/psi_fix_cic_int_fix_1ch_tb.vhd#L155-L164)（输出阻塞），把每组参数归入一个场景。
3. 思考：为什么前 3 组都设两个 idle 为 0，而后 3 组各带一个或两个非零值？

**需要观察的现象**：

- `in_idle_cycles_g=N` → 输入进程每发一个样本就停 N 拍，`vld_i` 周期性缺失，逼 DUT 的输入握手 `rdy_o` 与内部 `InRdy_v` 反复进入「等待源头」状态。
- `out_idle_cycles_g=N` → 输出进程每取一个样本就拉低 `rdy_i` 共 N 拍，逼 DUT 的 `OutRdy_v='0'` 冻结整条高速积分/增益链，并通过 `rdy_o` 向源头反压。

**预期结果**（6 组参数的场景归类）：

| 组 | `in_idle_cycles_g` | `out_idle_cycles_g` | 滤波参数 | 模拟场景 |
|----|--------------------|---------------------|----------|----------|
| 1 | 0 | 0 | o3/r10/dd1/gcTrue | 无压力（纯位真） |
| 2 | 0 | 0 | o4/r9/dd2/gcTrue | 无压力（换滤波参数） |
| 3 | 0 | 0 | o4/r6/dd2/gcFalse | 无压力（关增益校正） |
| 4 | 20 | 2 | o4/r6/dd2/gcFalse | **重度输入饥饿 + 轻度输出阻塞**（二者兼有） |
| 5 | 2 | 0 | o4/r6/dd2/gcFalse | **轻度输入饥饿**（仅 input starving） |
| 6 | 2 | 20 | o3/r10/dd1/gcTrue | **轻度输入饥饿 + 重度输出阻塞**（仅 output blocked 的极端） |

结论：`in_idle_cycles_g` 模拟**输入饥饿**（上游供不上数），`out_idle_cycles_g` 模拟**输出阻塞**（下游消费不动、反压 DUT）。前 3 组用「双零」锁定纯位真行为（与抽取器 TB 的位真基准对齐），后 3 组则系统遍历「单侧饥饿 / 单侧阻塞 / 双侧兼有」三种握手压力，确保 `OutRdy_v`/`InRdy_v` 反压逻辑在所有节拍组合下都不破坏位真。

**待本地验证**：实跑 `cic_int_fix_1ch_tb` 6 组回归，确认 6 组均无 `###ERROR###`——尤其第 4、6 组的 20 拍极端反压是否仍逐位通过。

#### 4.3.5 小练习与答案

**练习 1**：如果用同一个 `cic_int_fix_1ch_tb`，把 `in_idle_cycles_g` 设成 0、`out_idle_cycles_g` 设成 0，插值器的吞吐（样本/秒）由什么决定？把 `out_idle_cycles_g` 改成 20 后吞吐如何变化？

**参考答案**：双零时，输入每拍可被接收（受 `Rcnt` 周期约束：每 R 拍接收 1 个低速样本），输出每拍可被取走，吞吐由 `Rcnt` 周期决定——每 R 个高速时钟产出 R 个输出样本（对应 1 个输入样本），即输出满速 \(F_s\cdot R\)、输入满速 \(F_s\)。`out_idle_cycles_g=20` 后，每取 1 个输出样本就阻塞 20 拍，输出吞吐降到约 \(F_s\cdot R/21\)；由于输出反压经 `OutRdy_v` 冻结积分链、再经 `rdy_o` 传到输入，输入吞吐也同比下降。位真结果不变（只是更慢），这正是 `output_*.txt` 仍能逐行比对的原因。

**练习 2**：抽取器 TB 只有一个 `idle_cycles_g` 而没有 `out_idle_cycles_g`，这是否意味着抽取器的输出永远不会被反压？如果是，下游消费不动时会发生什么？

**参考答案**：是的，抽取器**接口上没有 `rdy_i`**，约定下游必须即时接收每一个 `vld_o` 脉冲。若实际下游消费不动（没有遵守该约定），由于抽取器内部输出端**没有 FIFO 缓存**，下一个抽取完成的样本会直接覆盖 `Outp`/`OutVld`，导致样本丢失。因此使用抽取器时，必须确保下游（通常是下一级滤波器或 FIFO）能跟上 \(F_s/R\) 的低速输出，或在抽取器后自行加一级 FIFO 吸收抖动。需要双向反压的场合应改用带完整握手的实现（或外接 FIFO）。

## 5. 综合实践

**任务**：把一个真实配置的抽取 CIC「从输入样本到输出样本」完整走一遍，标出它在 record 每一级字段里的踪迹，并与插值器做镜像对照。

取 `config.tcl` 抽取器第 1 组参数 `order_g=3, ratio_g=10, diff_delay_g=1, auto_gain_corr_g=True`（[config.tcl:253](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L253)），完成下表：

| 核查项 | 你的答案 |
|--------|---------|
| **积分器级数** | 3 级（`Accu(1..3)`） |
| **梳状器级数** | 3 级（`DiffVal(1..3)`） |
| **抽取计数器 `Rcnt` 范围** | _0..9_ |
| **每多少个有效输入样本产出 1 个输出** | _10_ |
| **`diff_delay_g=1` 用到的延时寄存器** | _仅 `DiffLast`（不用 `DiffLast2`）_ |
| **粗校正移位量 `Shift_c`** | _\(B_{\text{add}}=\lceil\log_2(10\cdot1)^3\rceil=\lceil\log_2 1000\rceil=10\)（u6-l1）_ |
| **抽取器有没有 `rdy_o` / `rdy_i`** | _都没有，只有 `busy_o`_ |
| **镜像到插值器：哪些顺序反过来？** | _梳状在前、积分在后；插零替代抽取；移位移到末端_ |

**操作步骤**：

1. 打开 [hdl/psi_fix_cic_dec_fix_1ch.vhd:52-75](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_fix_1ch.vhd#L52-L75)，确认 `Accu`/`DiffVal` 数组长度 = `order_g`、`VldAccu`/`VldDiff` 宽度 = `order_g+1`。
2. 在 [L117-128](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_fix_1ch.vhd#L117-L128) 跟踪 `Rcnt` 从 9 递减到 0 才放行一次，印证「每 10 个有效样本输出 1 个」。
3. 打开插值器 [hdl/psi_fix_cic_int_fix_1ch.vhd:175-186](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_int_fix_1ch.vhd#L175-L186)，对比「插零 `Rcnt`」与抽取器的「抽取 `Rcnt`」——前者把 1 个样本展开成 R 拍，后者把 R 个样本收成 1 拍，方向相反。
4. 用一句话写下两者的握手差异：抽取器单向（`vld_i`→`vld_o`+`busy_o`），插值器双向（`vld_i`/`rdy_o`↔`vld_o`/`rdy_i`）。

**预期结果**：你能不看源码复述抽取器「输入寄存→3 级积分→Rcnt 抽取+移位→3 级梳状→增益校正→输出」的完整链路，并说出插值器在哪几处与之镜像、为何握手更复杂。

**待本地验证**：`Shift_c=10`、各级格式位宽需阅读 RTL 常量或运行位真模型确认（u6-l1 已给出公式）。

## 6. 本讲小结

- 抽取 CIC 数据流：`Input_0` → N 级积分器（`Accu`，高速、`vld` 门控累加）→ `Rcnt` 抽取 ↓R 并在抽取点做粗校正移位 → N 级梳状器（`DiffVal`，低速，`DiffLast`/`DiffLast2` 实现 M=1/2）→ 增益校正 → 输出。
- 插值 CIC 数据流是抽取的**镜像**：`Input_0` → N 级梳状器（低速）→ `Rcnt` 插零 ↑R → N 级积分器（高速）→ 末端移位 → 增益校正 → 输出；直流增益少因子 R，故移位与中间格式（`ShiftInFmt`/`ShiftOutFmt`）与抽取器不同。
- `ratio_g` 决定抽取/插值计数器宽度与位增长；`order_g` 决定积分/梳状级数与 valid 数组宽度；`diff_delay_g` 决定梳状器用 `DiffLast`（M=1）还是再加 `DiffLast2`（M=2）。
- 握手复杂度取决于「哪一侧跑高速」：抽取器高速侧是输入（无条件接收），故**无任何 `rdy`、仅 `busy_o`**；插值器高速侧是输出（须被下游反压），故**双向 AXI-S**（`rdy_o` 反压源头、`rdy_i` 受下游反压），用 `OutRdy_v`/`InRdy_v` 门控流水线前移、用寄存器 `Rdy_0` 打破组合 ready 路径。
- 测试覆盖用两个参数制造压力：`in_idle_cycles_g` 模拟**输入饥饿**（`vld_i` 周期性缺失），`out_idle_cycles_g` 模拟**输出阻塞**（`rdy_i` 周期性拉低）；抽取器 TB 只有输入间隙参数（因无输出反压通道）。
- `config.tcl` 的 6 组插值器参数系统遍历「无压力 / 单侧饥饿 / 单侧阻塞 / 双侧兼有」，其中 `=20` 的极端值专门逼出反压逻辑边界 bug，所有组都须无 `###ERROR###` 才算位真通过。

## 7. 下一步学习建议

本讲把单通道 CIC 的数据流与握手讲透了，但刻意回避了两件事：**多通道**与**可配置比率**。建议按以下顺序继续：

1. **u6-l3 CIC 多通道与可配置比率**：进入 `cic_dec_fix_nch_par_tdm`、`cic_dec_fix_nch_tdm_tdm`、`cic_dec_cfg_*` 变体。重点看 TDM 时分复用如何让多个通道**共享同一套积分/梳状算子**（本讲的 `Accu`/`DiffVal` 数组如何被多通道轮流使用），以及 `cfg` 版如何在不溢出前提下用运行时端口 `cfg_ratio_i`/`cfg_shift_i`/`cfg_gain_corr_i` 改比率（对照 u6-l1 的 fix/cfg 差异）。
2. **横向对比半带 FIR（u7-l3）**：半带 FIR `fir_3tap_hbw_dec2` 同样做 ↓2 粗变换，但用乘法器（抽头是 2 的幂故可用移位替代）。对比它与本讲 CIC 在「粗抽取第一级」角色上的资源-精度取舍——CIC 无乘法器但通带 droop 大，半带 FIR 阻带好但用了算术资源。
3. **回头验证位真**：若条件允许，实跑 `cic_dec_fix_1ch_tb` 与 `cic_int_fix_1ch_tb` 的全部参数组，确认本讲描述的握手行为（尤其插值器第 4、6 组的 20 拍极端反压）在仿真中与讲述一致。

阅读多通道源码时，请把本讲的 record 字段名（`Accu`/`DiffVal`/`Rcnt`/`VldAccu`/`VldDiff`）与 `OutRdy_v`/`InRdy_v` 反压逻辑放在手边——多通道变体是在这套骨架上叠加「通道计数 + TDM 帧同步」，核心数据流与本讲同构。
