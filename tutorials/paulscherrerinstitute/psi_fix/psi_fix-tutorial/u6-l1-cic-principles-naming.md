# CIC 原理与命名规则

## 1. 本讲目标

CIC（Cascaded Integrator-Comb，级联积分梳状）滤波器是 FPGA 上最常用的「无乘法器」采样率变换结构，几乎所有高速抽取/插值链路的「第一级粗变换」都用它。psi_fix 把 CIC 做成了一个庞大的组件族——单通道/多通道、并行/TDM、固定比率/可配置比率——族内每个成员都遵守同一套命名规则。

学完本讲，读者应该能够：

- 说清 CIC 抽取器与插值器的「积分 → 速率变换 → 梳状」三段结构，以及为什么它不需要乘法器。
- 用 Hogenauer 公式算出 CIC 的增益与「位增长」，并据此推出累加器格式 `AccuFmt`。
- 逐字段解读任意 `psi_fix_cic_*` 组件的名字，凭名字判断它的抽取/插值方向、比率是否可配、通道数与输入输出组织方式。
- 理解 `auto_gain_corr` 这个开关：移位把增益压到 [0.5, 1.0]，再可选地用一个乘法器把增益精确校正到 1.0。

本讲是「滤波器族 I：CIC」单元的导论，**只讲原理与命名**，不展开多通道/可配置比率的 RTL 实现细节（那是 u6-l2、u6-l3 的事）。

## 2. 前置知识

在进入本讲前，读者应当已经掌握（见前置讲义摘要）：

- **定点格式三元组 `[s,i,f]`** 与位增长三规则：加减法整数位 +1，乘法整数位相加再 +1（u1-l4）。
- **差分-累加结构**：u4-l1 讲 `psi_fix_mov_avg` 时已见过「延时线 + 减法器 + 累加器」的雏形——CIC 正是把这个思路推广到「先累加、后差分」并插入采样率变换。
- **EXACT 增益校正**：u4-l1 中 mov_avg 用一个 `[0,1,16]` 的系数乘一个封顶 25 位的数据通路来精确补偿增益——CIC 的 `auto_gain_corr` 复用的正是同一套格式约定。
- **两段式编码**：`two_process_r` record + 组合进程 `p_comb` + 时序进程 `p_seq`（u3-l3）。本讲的 VHDL 精读会反复用到。
- **位真双模型**：每个 VHDL 组件配套一个逐位一致的 Python 模型作为黄金参考（u2-l3、u3-l2）。

三个常用记号：

| 记号 | 含义 |
|------|------|
| R | 抽取/插值比率（ratio） |
| M | 差分延时（differential delay，通常 1 或 2） |
| N | CIC 阶数（order，即积分器和梳状器各 N 级） |

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| [hdl/CicNaming.txt](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/CicNaming.txt) | CIC 组件族的命名规则定义（纯文本，6 个字段） |
| [doc/files/CicNaming.md](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/CicNaming.md) | 命名规则的文档占位（由 hdl2md 生成骨架） |
| [model/psi_fix_cic_dec.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_cic_dec.py) | 抽取 CIC 的位真 Python 模型——任何 RTL 实现的黄金参考 |
| [model/psi_fix_cic_int.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_cic_int.py) | 插值 CIC 的位真 Python 模型（结构镜像抽取器） |
| [hdl/psi_fix_cic_dec_fix_1ch.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_fix_1ch.vhd) | 最简单的 CIC 抽取器（单通道、固定比率），本讲主样板 |
| [hdl/psi_fix_cic_dec_cfg_1ch.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_cfg_1ch.vhd) | 可配置比率版，用来对比 `fix` 与 `cfg` 的接口差异 |
| [doc/files/psi_fix_cic_dec_fix_1ch.md](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/psi_fix_cic_dec_fix_1ch.md) | 单通道抽取器的官方说明（位增长与增益校正的文字版） |
| [sim/config.tcl](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl) | 回归脚本，记录了 CIC 各组件的参数矩阵 |

> 提示：CIC 在仓库里共有 7 个 RTL 实现 + 2 个 Python 模型。**抽取器只有一套 Python 模型 `psi_fix_cic_dec.py`，但被所有抽取 RTL 共用作黄金参考**——这正是位真双模型「一个模型、多种实现」的思想（见模型类 docstring）。

## 4. 核心概念与源码讲解

### 4.1 CIC 原理与位增长

#### 4.1.1 概念说明

CIC 是一种只用**加法、减法和寄存器**（不含乘法器）就能完成采样率变换的滤波器。它的核心由两类级联单元组成：

- **积分器（Integrator）**：就是一个累加器 \( y[n] = y[n-1] + x[n] \)，传递函数 \( 1/(1-z^{-1}) \)。
- **梳状器 / 差分器（Comb / Differentiator）**：是一个 \( M \) 抽头差分 \( y[n] = x[n] - x[n-M] \)，传递函数 \( 1-z^{-M} \)。

关键洞见：**积分器和梳状器都是线性时不变的，当二者之间没有采样率变换时它们可以交换顺序**。Hogenauer 的巧妙之处在于把速率变换插在两类单元**之间**：

- **抽取器（decimation，↓R）**：先在**高采样率** \( F_s \) 上跑 N 级积分器 → 抽取 ↓R（每 R 个样本取 1 个）→ 再在**低采样率** \( F_s/R \) 上跑 N 级梳状器。积分器高速累加，梳状器低速差分，资源最省。
- **插值器（interpolation，↑R）**：顺序反过来——先在低采样率跑 N 级梳状器 → 插零 ↑R → 再在高采样率跑 N 级积分器。

为什么 CIC 这么受欢迎？因为它**完全没有乘法器**，在第一级把几 GHz 的数据流迅速降到几百 MHz，后续再用 FIR 做精细滤波。代价是：阻带衰减有限、通带 droop 明显，所以 CIC 几乎总是后面跟一级 FIR 补偿。

#### 4.1.2 核心流程

抽取 CIC 的数据流（对应 Python 模型 `Process()` 的步骤）：

```
输入 x[n] @ Fs
   │
   ▼
┌──────────────┐
│ N 级积分器    │  每级 y[n]=y[n-1]+x[n]，运行在 Fs（高速）
│ (Integrator) │  —— 累加器位宽随位增长加宽
└──────────────┘
   │  @ Fs
   ▼
┌──────────────┐
│ 抽取 ↓R      │  每 R 个样本取 1 个；同时右移 Shift_c 位补偿增益
└──────────────┘
   │  @ Fs/R
   ▼
┌──────────────┐
│ N 级梳状器    │  每级 y[n]=x[n]-x[n-M]，运行在 Fs/R（低速）
│ (Comb)       │
└──────────────┘
   │
   ▼
┌──────────────┐
│ 增益校正      │  可选：乘 Gc 把增益精确拉到 1.0
└──────────────┘
   │
   ▼
输出 y[m] @ Fs/R
```

**位增长（Hogenauer 公式）**。N 阶、比率 R、差分延时 M 的抽取 CIC，其直流增益为：

\[
G = (R\cdot M)^{N}
\]

这个增益要求累加器**多备若干整数位**才不溢出（更准确地说，是保证梳状器输出端量化噪声低于 1 LSB）。Hogenauer 给出的最小附加整数位数为：

\[
B_{\text{add}} = \lceil \log_2 G \rceil = \big\lceil N\cdot \log_2(R\cdot M) \big\rceil
\]

于是累加器格式在输入格式基础上整数位增长 \( B_{\text{add}} \) 位：

\[
\text{AccuFmt} = (\,s_{\text{in}},\ i_{\text{in}} + B_{\text{add}},\ f_{\text{in}}\,)
\]

> **关于积分器溢出**：由于采用二进制补码的模运算，积分器中途溢出（回绕）是**无害**的——只要累加器位宽足够，后续梳状器的减法会自动把回绕抵消，最终结果正确。这是 CIC 能用窄累加器的理论保证，也是位真模型必须用「整数 + 显式取模」来仿真的原因（见 4.1.3）。

#### 4.1.3 源码精读

**Python 模型里的增益与位增长推导**。构造函数把上面的公式原样翻译成常量：

[psi_fix_cic_dec.py:49-60](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_cic_dec.py#L49-L60) —— 注意 `cicGain`、`cicAddBits`、`shift`、`accuFmt` 四行的对应关系：

```python
self.cicGain    = (ratio*diffDelay)**order          # G = (R·M)^N
self.cicAddBits = ceil(log2(self.cicGain))          # B_add = ceil(log2 G)
self.shift      = self.cicAddBits                    # 移位量 = B_add
self.accuFmt    = psi_fix_fmt_t(inFmt.s, inFmt.i+self.cicAddBits, inFmt.f)  # 整数位 +B_add
```

**积分器用「整数 + 取模」仿真补码回绕**。这是全篇最值得读的一段——它精确复现了硬件累加器的溢出行为：

[psi_fix_cic_dec.py:74-83](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_cic_dec.py#L74-L83) —— 逐级累加并显式 `% (1 << accuFmt.size)` 取模。注释点明原因：累加器很大，直接用浮点 `psi_fix_add` 会撞上 53 位双精度上限（承接 u2-l3 的位真精度限制），所以转成任意精度整数 + 取模来**逐位复刻硬件**。

**抽取 + 移位**。在 N 级积分器之后，模型用切片 `sigInt[order][::ratio]` 完成 ↓R 抽取，并一次性把增益补偿移位与分数位对齐合并到一次移位里：

[psi_fix_cic_dec.py:85-94](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_cic_dec.py#L85-L94) —— `addFracPlaces = diffFmt.f - accuFmt.f` 把 accu 的分数位对齐到 diffFmt，再叠加 `shift` 位增益补偿。

**VHDL 侧的同一组公式**。RTL 实现里这些全是编译期常量，与 Python 模型逐行对应（位真的前提）：

[psi_fix_cic_dec_fix_1ch.vhd:38-42](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_fix_1ch.vhd#L38-L42) —— `CicGain_c`、`CicAddBits_c`、`Shift_c`、`AccuFmt_c`。注意第 39 行 `log2ceil(CicGain_c - 0.1)` 的 `-0.1` 是注释里写明的 **Vivado 实数计算不精确的 workaround**（当 CicGain 恰为 2 的幂时，Vivado 的实数 log2 会算出整数值，ceil 不再加 1，导致位数少算一位）。

**梳状器格式为何多 `order+1` 个分数位**？这是 CIC 位宽设计里最反直觉的一处：

[psi_fix_cic_dec_fix_1ch.vhd:42](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_fix_1ch.vhd#L42) —— `DiffFmt_c := (out_fmt_g.S, in_fmt_g.I, out_fmt_g.F + order_g + 1)`。官方文档 [psi_fix_cic_dec_fix_1ch.md:22](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/psi_fix_cic_dec_fix_1ch.md#L22) 解释：每一级梳状器对某些频率成分的增益可达 2，N 级链路最坏情况放大 \( 2^N \)，所以要为每级预留 1 个分数位（共 `order` 个），再加 1 位裕量，才能保证「梳状器输入端能改变输出 1 LSB 的那一位」不被丢掉。

**RTL 里积分器与抽取的实现**。积分器在高速时钟域，用一个 `for stage` 循环展开 N 级 `psi_fix_add`；抽取由计数器 `Rcnt` 控制每 R 拍放行一次，并在放行时一次性 `psi_fix_shift_right` 完成增益补偿移位：

[psi_fix_cic_dec_fix_1ch.vhd:100-128](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_fix_1ch.vhd#L100-L128) —— 第 102-115 行是 N 级积分器；第 119-128 行是 `Rcnt` 计数抽取，第 124 行的 `psi_fix_shift_right(... Shift_c ...)` 即增益补偿粗移位。

> 两段式细节（承接 u3-l3）：`v := r` 让未赋值字段保持；`VldAccu` / `VldDiff` 数组切片平移驱动 valid 流水；积分器只在 `r.VldAccu(stage)='1'` 时更新——这套写法与 mov_avg 完全同构。

#### 4.1.4 代码实践

**实践目标**：手算一个具体配置的位增长，并到源码里核对。

**操作步骤**：

1. 取 `config.tcl` 里 `cic_dec_fix_1ch_tb` 的第 4 组参数（最大的一组）：`order_g=6, ratio_g=5001, diff_delay_g=2`（见 [config.tcl:256](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L256)）。
2. 按公式计算：
   - \( G = (R\cdot M)^N = (5001 \times 2)^6 = 10002^6 \)
   - \( B_{\text{add}} = \lceil \log_2 G \rceil = \lceil 6 \cdot \log_2(10002) \rceil \approx \lceil 6 \times 13.288 \rceil = \lceil 79.73 \rceil = 80 \)
3. 若输入格式为默认 `in_fmt_g=(1,0,15)`，则累加器格式应为 `(1, 0+80, 15) = (1, 80, 15)`，即 **96 位**累加器。

**需要观察的现象**：

- 打开 [psi_fix_cic_dec_fix_1ch.vhd:41](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_fix_1ch.vhd#L41) 的 `AccuFmt_c` 表达式，确认它的整数位就是 `in_fmt_g.I + CicAddBits_c`，与手算一致。
- 这也解释了为什么 Python 模型非要「转整数 + 取模」仿真：96 位累加器远超 53 位双精度，浮点 `psi_fix_add` 根本无法逐位保真。

**预期结果**：手算的 \( B_{\text{add}}=80 \) 与源码公式吻合；累加器宽度 96 位。若你的 `log2` 计算结果与 80 相差 1，检查是否漏掉了 `ceil` 或 Vivado 的 `-0.1` workaround（该 workaround 仅影响「G 恰为 2 的幂」的边界情形，本例 \( G=10002^6 \) 非幂，无影响）。

**待本地验证**：若想看真实数值，可在 `model/` 目录下写一段最小脚本实例化 `psi_fix_cic_dec(order=6, ratio=5001, diffDelay=2, ...)` 并打印 `.accuFmt`，但需先按 u1-l1 的并排目录要求摆好 `en_cl_fix` 等依赖。

#### 4.1.5 小练习与答案

**练习 1**：为什么 CIC 抽取器把积分器放在抽取**之前**（高速侧），而不是之后？

**参考答案**：因为积分器和梳状器在无采样率变换时等价可交换，但积分器是递归的（有反馈），必须在它运行的整个速率上每拍都算；梳状器是 FIR（无反馈）。把积分器放在高速侧、梳状器放在低速侧，可以让**两类单元都只在其必要的速率上运行**——积分器高速累加、梳状器低速差分，整体资源最省。若反过来，积分器就得在低速侧补回所有漏掉的样本，失去了 CIC 的资源优势。

**练习 2**：抽取 CIC 的直流增益是 \( (R\cdot M)^N \)，而插值 CIC 模型 [psi_fix_cic_int.py:49](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_cic_int.py#L49) 写的却是 `((ratio*diffDelay)**order)/ratio`，多除了一个 R，为什么？

**参考答案**：插值器在梳状器后「插零 ↑R」（N-1 个零、1 个有效样本），随后积分器对这串含大量零的序列累加。插零使输出端的等效增益比纯抽取结构少了因子 R（每 R 个里只有 1 个非零样本被积分），所以插值 CIC 的净增益是 \( (R\cdot M)^N / R \)。位增长公式 `cicAddBits = ceil(log2(cicGain))` 因此对插值器略小一些。

### 4.2 命名规则

#### 4.2.1 概念说明

psi_fix 的 CIC 族成员众多，但全部遵循**一个固定的命名模板**。掌握这套规则后，看到任何 `psi_fix_cic_*` 文件名就能立刻判断它的能力边界——这是「凭名字机械定位组件」的关键（承接 u1-l2 的「文件名即索引」纪律）。

命名模板有 **6 个字段**（最后两个可省略）：

```
psi_fix_cic_<int/dec>_<ratio-handling>_<channels>_<input-handling>_<output-handling>_<architecture>
```

#### 4.2.2 核心流程

逐字段含义（直接对应 [CicNaming.txt](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/CicNaming.txt)）：

| 字段 | 取值 | 含义 |
|------|------|------|
| `<int/dec>` | `int` / `dec` | 插值 CIC / 抽取 CIC（方向） |
| `<ratio-handling>` | `fix` / `cfg` | 比率编译期固定 / 运行时可配置 |
| `<channels>` | `nch` / `1ch` | 多通道（通道数可配）/ 单通道 |
| `<input-handling>` | `par` / `tdm` / `-` | 多通道输入并行 / TDM 时分复用 / 单通道时省略 |
| `<output-handling>` | `par` / `tdm` / `-` | 多通道输出并行 / TDM / 单通道时省略 |
| `<architecture>` | `x7` / `-` | Xilinx 7 系列专用 / 省略表示厂商无关通用实现 |

三个要点：

1. **单通道组件没有 `<input-handling>` 和 `<output-handling>`**——因为只有一路数据，不存在「并行 vs TDM」之分。所以 `1ch` 组件名字比 `nch` 组件短两段。
2. **`par`（parallel）与 `tdm`（time-division multiplex）是描述多通道「怎么排」的两种方式**。`par` = 每个通道一根独立数据线，同拍送达；`tdm` = 所有通道轮流共用一根数据线，按帧节拍依次出现。输入输出可以独立选择，于是多通道组件有 `par_tdm`、`tdm_tdm`、`par_par` 等组合。
3. **`fix` vs `cfg` 是比率处理方式的分水岭**（见 4.3）：`fix` 把 R 写死成 generic；`cfg` 允许运行时通过端口改 R（但只能在复位时改）。

#### 4.2.3 源码精读

**命名规则的权威定义**就放在 `hdl` 目录下、与 VHDL 源同层：

[CicNaming.txt:1-28](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/CicNaming.txt#L1-L28) —— 完整模板与每个字段的两行说明。

**仓库里真实存在的 CIC 组件**（可对照命名规则逐一解读）：

| 文件名 | int/dec | ratio | ch | in | out | arch |
|--------|---------|-------|----|----|-----|------|
| `psi_fix_cic_dec_fix_1ch` | dec | fix | 1ch | — | — | 通用 |
| `psi_fix_cic_int_fix_1ch` | int | fix | 1ch | — | — | 通用 |
| `psi_fix_cic_dec_fix_nch_par_tdm` | dec | fix | nch | par | tdm | 通用 |
| `psi_fix_cic_dec_fix_nch_tdm_tdm` | dec | fix | nch | tdm | tdm | 通用 |
| `psi_fix_cic_dec_cfg_1ch` | dec | cfg | 1ch | — | — | 通用 |
| `psi_fix_cic_dec_cfg_nch_par_tdm` | dec | cfg | nch | par | tdm | 通用 |
| `psi_fix_cic_dec_cfg_nch_tdm_tdm` | dec | cfg | nch | tdm | tdm | 通用 |

几个观察：

- **插值器目前只有 `int_fix_1ch` 一种**——多通道/可配置插值尚未实现（读者日后想贡献，这正是空白点）。
- **没有任何组件用到 `x7` 架构后缀**——当前所有 CIC 都是厂商无关的通用实现。
- **`config.tcl` 的 TB 注册顺序与上表一致**：[config.tcl:250-256](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L250-L256) 注册 `cic_dec_fix_1ch_tb`，[config.tcl:430-436](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L430-L436) 注册 `cic_dec_cfg_1ch_tb`，两者用**几乎相同**的参数矩阵——说明 `fix` 与 `cfg` 在数学行为上完全等价，区别仅在比率来源。

#### 4.2.4 代码实践

**实践目标**（本讲指定的核心任务）：解读 `psi_fix_cic_dec_cfg_nch_tdm_tdm` 的每个字段，并说出它相对 `cic_dec_fix_1ch` 多了哪些可配置维度。

**操作步骤**：

1. 按模板 `psi_fix_cic_<int/dec>_<ratio-handling>_<channels>_<input-handling>_<output-handling>` 把名字拆成 6 段。
2. 逐字段对照 [CicNaming.txt:5-25](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/CicNaming.txt#L5-L25) 填含义。
3. 与 `cic_dec_fix_1ch` 对比，列出多出来的维度。

**需要观察的现象**：拆解后应得到一张字段对照表。

**预期结果**：

`psi_fix_cic_dec_cfg_nch_tdm_tdm` 拆解：

| 字段 | 值 | 含义 |
|------|----|------|
| int/dec | `dec` | 抽取 CIC |
| ratio-handling | `cfg` | 抽取比率运行时可配置 |
| channels | `nch` | 多通道（通道数由 generic 配） |
| input-handling | `tdm` | 多通道输入走 TDM 时分复用 |
| output-handling | `tdm` | 多通道输出走 TDM |

相对 `cic_dec_fix_1ch`（dec / fix / 1ch / 无输入组织 / 无输出组织）多了**三个可配置维度**：

1. **比率可配置**（`fix`→`cfg`）：比率不再写死，运行时由端口 `cfg_ratio_i` 指定，需配合 `cfg_shift_i`、`cfg_gain_corr_i`（见 4.3.3）。
2. **通道数可配置**（`1ch`→`nch`）：从单通道变为多通道，通道数成为 generic。
3. **输入/输出组织方式**（新增 `tdm`/`tdm`）：单通道无所谓组织方式；多通道必须指明输入是并行还是 TDM、输出是并行还是 TDM——这里两者都是 TDM，意味着所有通道共用一根输入线和一根输出线、按帧节拍轮流收发。

**待本地验证**：可选——在仓库根目录执行 `ls hdl/ | grep cic` 自行列出全部 CIC 组件，核对上表是否完整、是否真有 `cfg_nch_tdm_tdm` 这个文件。

#### 4.2.5 小练习与答案

**练习 1**：假设要做一个「Xilinx 7 系列专用、4 通道、并行输入并行输出、比率运行时可配的抽取 CIC」，按规则它的文件名应该是什么？

**参考答案**：`psi_fix_cic_dec_cfg_nch_par_par_x7`。依次拼：`dec`（抽取）+ `cfg`（比率可配）+ `nch`（多通道）+ `par`（输入并行）+ `par`（输出并行）+ `x7`（7 系列专用）。注意当前仓库里**并不存在**这个组件——这是按规则推导出的「假想名」，用来检验你是否掌握了模板。

**练习 2**：为什么 `psi_fix_cic_dec_fix_1ch` 的名字在 `1ch` 之后就结束了，而 `nch` 组件后面还跟两段？

**参考答案**：因为单通道只有一路数据，不存在「多通道如何排列」的问题，`<input-handling>` 和 `<output-handling>` 字段对单通道**省略**（[CicNaming.txt:20,25](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/CicNaming.txt#L20) 明确写了 "Omitted for single channel implementations"）。所以 `1ch` 是名字里「通道处理」部分的天然终止符。

### 4.3 增益校正

#### 4.3.1 概念说明

CIC 的直流增益 \( G=(R\cdot M)^N \) 通常是个很大的数（例如 4 阶、比率 10、M=1 时 \( G=10^4=10000 \)）。如果直接输出，幅度会被放大成千上万倍，完全不可用。所以**任何** CIC 都必须做增益补偿。psi_fix 用两级补偿：

1. **粗校正（始终执行）——右移**。把累加器结果右移 \( B_{\text{add}}=\lceil\log_2 G\rceil \) 位。由于 \( 2^{B_{\text{add}}-1} < G \le 2^{B_{\text{add}}} \)，移位后剩余增益 \( G/2^{B_{\text{add}}} \in (0.5,\,1.0] \)。这一步**只需移位、不需要乘法器**，是「免费」的。
2. **精校正（可选，`auto_gain_corr=True`）——乘法**。再乘一个系数 \( G_c = 2^{B_{\text{add}}}/G \) 把增益精确拉回 1.0。这一步**需要一个乘法器**，但换来精确的单位增益。

这正是 u4-l1 里 mov_avg 的 EXACT 增益校正思路在 CIC 上的重演——连系数格式都完全一样。

#### 4.3.2 核心流程

\[
\text{粗校正后增益} = \frac{G}{2^{B_{\text{add}}}} \in (0.5,\ 1.0]
\]

\[
\text{精校正系数} = G_c = \frac{2^{B_{\text{add}}}}{G} \quad(\text{auto\_gain\_corr=True 时才乘})
\]

精校正通路的数据与系数格式被刻意约束（承接 u4-l1 的 DSP48 友好设计）：

- **系数格式** `GcCoefFmt = (0,1,16)`：无符号、1 整数位、16 小数位 = 17 位，表示范围 \([0,2)\)，正好覆盖 \( G_c \in (0.5,1.0] \)。
- **数据通路格式** `GcInFmt = (1, outFmt.I, min(24-outFmt.I, DiffFmt.F))`：有符号、整数位取输出格式，**分数位封顶到使总宽 ≤ 25 位**（1 符号 + outFmt.I 整数 + 分数位）。
- **乘积格式** `GcMultFmt = (1, GcInFmt.I + GcCoefFmt.I, GcInFmt.F + GcCoefFmt.F)`：标准乘法位增长。

25 位数据 × 17 位系数 = 恰好落入单个 DSP48 slice——这是把「精确增益校正」压到 1 个乘法器的关键资源决策（与 mov_avg 完全同构）。

#### 4.3.3 源码精读

**Python 模型里的增益校正系数**：

[psi_fix_cic_dec.py:57-60](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_cic_dec.py#L57-L60) —— `gcInFmt` 的封顶 `min(24-outFmt.i, diffFmt.f)`、`gcCoefFmt=(0,1,16)`、`gc = 2^cicAddBits/cicGain`，三行与下面的 VHDL 一一对应。

[psi_fix_cic_dec.py:103-111](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_cic_dec.py#L103-L111) —— `Process()` 末尾的分叉：`autoGainCorr=True` 时走「resize→mult」精校正，`False` 时只把梳状结果 resize 到输出格式（增益留在 [0.5,1.0]）。

**VHDL 侧的精校正三级流水**（注意是 Manual Splitting 范式，承接 u2-l2）：

[psi_fix_cic_dec_fix_1ch.vhd:43-46](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_fix_1ch.vhd#L43-L46) —— `GcInFmt_c`、`GcCoefFmt_c`、`GcMultFmt_c`、`Gc_c` 四个常量。`Gc_c` 由 `psi_fix_from_real(2.0**CicAddBits_c/CicGain_c, GcCoefFmt_c)` 在**综合期**算定。

[psi_fix_cic_dec_fix_1ch.vhd:164-174](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_fix_1ch.vhd#L164-L174) —— 三级：
- Stage 0：`psi_fix_resize` 把梳状结果舍入+饱和到 `GcInFmt_c`（25 位通路）；
- Stage 1：`psi_fix_mult` 乘 `Gc_c`（`trunc/wrap`，把舍入推到下一级）；
- Stage 2：`psi_fix_resize` 把乘积舍入+饱和到 `out_fmt_g`。

**粗校正移位**发生在抽取点，不在增益校正段：

[psi_fix_cic_dec_fix_1ch.vhd:124](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_fix_1ch.vhd#L124) —— `psi_fix_shift_right(r.Accu(order_g), AccuFmt_c, Shift_c, Shift_c, DiffFmt_c, trunc, wrap)`，`Shift_c = CicAddBits_c` 就是粗校正。

**`auto_gain_corr_g=False` 的行为**：跳过整段精校正，直接把梳状结果 resize 到输出格式，此时增益在 [0.5,1.0] 之间：

[psi_fix_cic_dec_fix_1ch.vhd:184-190](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_fix_1ch.vhd#L184-L190) —— 输出赋值的 `if auto_gain_corr_g then ... else ...` 分叉。`config.tcl` 第 3 组参数 `-gauto_gain_corr_g=False`（[config.tcl:255](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L255)）正是用来覆盖这条路径的。

**`fix` 与 `cfg` 在增益校正上的根本差异**。`fix` 版比率写死，所以 `Shift_c`、`Gc_c` 都是综合期常量（如上）。`cfg` 版比率运行时可变，无法在综合期算定，于是把这些值**改成运行时输入端口**：

[psi_fix_cic_dec_cfg_1ch.vhd:36-47](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_cfg_1ch.vhd#L36-L47) —— 新增三个配置端口：`cfg_ratio_i`（实际比率-1）、`cfg_shift_i`（移位量）、`cfg_gain_corr_i`（精校正系数，17 位即 `[0,1,16]`）。文件头注释 [psi_fix_cic_dec_cfg_1ch.vhd:6](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_cfg_1ch.vhd#L6) 与 [psi_fix_cic_dec_cfg_1ch.vhd:20-23](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_cfg_1ch.vhd#L20-L23) 还给出了换算公式 `SHIFT=CIC_GROWTH`、`GAINCORR=2^CIC_GROWTH/CIC_GAIN`——**软件侧按当前比率算好再喂给硬件**。同时累加器须按**最大比率** `max_ratio_g` 来定宽：

[psi_fix_cic_dec_cfg_1ch.vhd:53-56](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_cfg_1ch.vhd#L53-L56) —— `MaxCicGain_c`、`MaxCicAddBits_c`、`AccuFmt_c` 全部基于 `max_ratio_g` 而非运行时比率，确保任何允许的比率下累加器都不溢出。

#### 4.3.4 代码实践

**实践目标**：跟踪一个样本走过「移位 + 精校正」两段，验证系数 \( G_c \) 的取值。

**操作步骤**：

1. 取 `config.tcl` 第 1 组参数 `order_g=3, ratio_g=10, diff_delay_g=1, auto_gain_corr_g=True`（[config.tcl:253](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L253)）。
2. 计算：
   - \( G = (10\times1)^3 = 1000 \)
   - \( B_{\text{add}} = \lceil\log_2 1000\rceil = 10 \)（因为 \( 2^9=512 < 1000 \le 1024=2^{10} \)）
   - 粗校正后增益 \( = 1000/1024 \approx 0.9766 \)（落在 [0.5,1.0]）
   - 精校正系数 \( G_c = 1024/1000 = 1.024 \)
3. 打开 [psi_fix_cic_dec_fix_1ch.vhd:46](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_fix_1ch.vhd#L46)，确认 `Gc_c` 的表达式 `2.0**CicAddBits_c/CicGain_c = 2^10/1000 = 1.024`，与手算一致。
4. 跟踪这个 \( G_c=1.024 \) 落入 `[0,1,16]` 格式后的量化值：\( 1.024 \times 2^{16} \approx 67092 \)（待本地验证精确值）。

**需要观察的现象**：

- 粗校正移位让增益落到 [0.5,1.0]——即使关掉 `auto_gain_corr`，幅度也只会在「最多差一倍」范围内，不会爆炸。
- 精校正系数 \( G_c \) 始终落在 \( (0.5,1.0] \) 之外（本例 1.024 > 1），所以系数格式才需要 `[0,1,16]` 的「1 个整数位」来表示略大于 1 的值。

**预期结果**：\( B_{\text{add}}=10 \)，\( G_c=1.024 \)，与源码常量表达式吻合。

**待本地验证**：系数经 `[0,1,16]` 量化后的精确整数（约 67092）需实际运行 Python 模型或综合后查看。

#### 4.3.5 小练习与答案

**练习 1**：`auto_gain_corr_g=True` 与 `False` 各适合什么场景？

**参考答案**：`False`（仅粗校正）省一个乘法器，但输出增益在 [0.5,1.0] 之间且随比率变化——适合「后面反正要接一级增益可调的 FIR/AGC，CIC 的增益误差无所谓」的链路。`True`（精校正）多花一个 DSP48，换来精确的单位增益——适合「CIC 输出直接用作最终结果」或「链路要求严格单位增益」的场合。这是一个典型的资源-精度权衡。

**练习 2**：为什么 `cfg` 版用 `max_ratio_g` 而不是运行时 `cfg_ratio_i` 来定累加器宽度？

**参考答案**：因为硬件位宽在**综合期**就固定了，运行时无法改变累加器位宽。综合期不知道用户运行时会选哪个比率，只能按**可能的最大比率**算出最大位增长 \( B_{\text{add,max}} \) 来定宽，保证「任何允许的比率下累加器都不溢出」。代价是当实际比率小于最大值时累加器会比必须的宽一些（多几位未用的整数位），这是「可配置」必然付出的资源代价。精校正系数 \( G_c \) 与移位量则可以运行时按实际比率算好，通过 `cfg_gain_corr_i`、`cfg_shift_i` 喂进来。

## 5. 综合实践

**任务**：选一个真实存在的 CIC 组件，把它「从名字到数学到 RTL 常量」完整贯通一遍。

以 `psi_fix_cic_dec_fix_nch_tdm_tdm` 为对象（多通道 TDM 抽取器），完成下表（可参考 [config.tcl:350-356](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L350-L356) 的测试参数 `order_g=3, ratio_g=10, diff_delay_g=1`）：

| 核查项 | 你的答案 |
|--------|---------|
| **命名拆解**（6 字段） | _dec / fix / nch / tdm / tdm / 通用_ |
| **方向** | 抽取 |
| **直流增益 G** | \( (10\times1)^3 = 1000 \) |
| **位增长 \( B_{\text{add}} \)** | \( \lceil\log_2 1000\rceil = 10 \) |
| **AccuFmt**（设 in_fmt=(1,0,15)） | (1, 10, 15) = 26 位 |
| **DiffFmt**（设 out_fmt=(1,0,15)） | (1, 0, 15+3+1) = (1,0,19) |
| **粗校正移位量** | 10 |
| **精校正系数 \( G_c \)** | 1024/1000 = 1.024 |
| **相对 `cic_dec_fix_1ch` 多的维度** | 通道数可配 + 输入 TDM + 输出 TDM |

**操作步骤**：

1. 先按 4.2 的规则拆名字，填前两行。
2. 按 4.1 的 Hogenauer 公式算 G、\( B_{\text{add}} \)、AccuFmt、DiffFmt。
3. 按 4.3 算移位量与 \( G_c \)。
4. 打开 [hdl/psi_fix_cic_dec_fix_nch_tdm_tdm.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_fix_nch_tdm_tdm.vhd)，找到它的 `CicGain_c`、`AccuFmt_c`、`DiffFmt_c`、`Gc_c` 常量，核对与你手算的是否一致（这些公式在族内所有成员中是同构的）。

**预期结果**：手算的位增长与增益校正参数，与 `nch_tdm_tdm` 的 RTL 常量表达式逐项吻合；同时你能清楚说出「多通道 TDM」相对单通道多了哪些复杂度（通道计数、TDM 帧同步）——这部分 RTL 实现细节留待 u6-l3 展开。

**待本地验证**：AccuFmt/DiffFmt 的精确位宽、\( G_c \) 量化后的整数值，需阅读实际 RTL 或运行位真模型确认。

## 6. 本讲小结

- CIC 是**无乘法器**的采样率变换结构：抽取器 = 高速 N 级积分器 → ↓R → 低速 N 级梳状器；插值器顺序相反。
- 直流增益 \( G=(R\cdot M)^N \)，由 Hogenauer 公式得位增长 \( B_{\text{add}}=\lceil\log_2 G\rceil \)，累加器整数位 +\( B_{\text{add}} \)；积分器中途的二进制补码回绕无害，故位真模型用「整数+取模」仿真。
- 梳状器格式比输出多 `order+1` 个分数位，因为每级梳状器对某些频率增益可达 2，需为每级预留 1 位再加 1 位裕量。
- CIC 命名是 6 字段模板 `cic_<int/dec>_<fix|cfg>_<nch|1ch>_<par|tdm|->_<par|tdm|->_<x7|->`；单通道省略输入/输出组织字段。
- 增益校正两级：**粗校正**（恒做，右移 \( B_{\text{add}} \) 位，增益落到 [0.5,1.0]，免费）+ **精校正**（`auto_gain_corr=True` 时，乘 \( G_c=2^{B_{\text{add}}}/G \)，耗 1 个 DSP48，增益精确为 1.0）。
- `fix` 与 `cfg` 的本质区别：`fix` 把比率和 \( G_c \) 都烧成综合期常量；`cfg` 用 `max_ratio_g` 定宽、用运行时端口 `cfg_ratio_i/cfg_shift_i/cfg_gain_corr_i` 接收软件算好的参数（仅复位时可改）。

## 7. 下一步学习建议

本讲只建立了 CIC 的「原理 + 命名 + 增益校正」全景图，**尚未展开任何 RTL 实现细节**。建议按以下顺序继续：

1. **u6-l2 CIC 单通道抽取与插值**：精读 `psi_fix_cic_dec_fix_1ch.vhd` 与 `psi_fix_cic_int_fix_1ch.vhd` 的完整数据流与握手，理解 `ratio_g`/`order_g`/`diff_delay_g` 三个参数如何塑形硬件，以及 `config.tcl` 中 `gin_idle_cycles_g`/`gout_idle_cycles_g` 如何制造输入饥饿/输出阻塞的握手压力。
2. **u6-l3 CIC 多通道与可配置比率**：进入 `nch_par_tdm`、`nch_tdm_tdm`、`cfg_*` 变体，看 TDM 时分复用如何让多个通道共享同一套积分/梳状算子，以及 `cfg` 版如何在不溢出前提下支持运行时改比率。
3. 若想横向对比「另一种无乘法器/低资源滤波器」，可跳读 u7-l3 的半带 FIR（`fir_3tap_hbw_dec2`），体会 CIC 与半带 FIR 在「粗抽取」角色上的取舍。

阅读源码时，建议始终把本讲的三个公式（增益、位增长、精校正系数）放在手边——它们是核对族内**任何** CIC 成员常量正确性的标尺。
