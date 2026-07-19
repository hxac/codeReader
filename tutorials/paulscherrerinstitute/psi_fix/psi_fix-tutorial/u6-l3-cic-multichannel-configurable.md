# CIC 多通道与可配置比率

## 1. 本讲目标

本讲是「滤波器族 I：CIC」单元的最后一讲。u6-l2 已经把单通道 CIC（`cic_dec_fix_1ch` / `cic_int_fix_1ch`）的数据流与握手讲透了。但在真实的多通道采集系统里（例如多路 ADC 同步采样），单通道组件显然不够用——你会要么例化 N 份重复的滤波器，要么用一个能「同时处理多通道」的组件。psi_fix 给出的答案是后者，并把「比率是否运行时可改」做成第二个正交维度。

学完本讲你应当能够：

1. 读懂 psi_fix 的 CIC 命名规则，仅凭组件名就能推断出它的「输入组织 / 输出组织 / 比率是否可配」。
2. 区分两类多通道结构：**并行输入→TDM 输出**（每个通道各占一组累加器）与 **TDM 输入→TDM 输出**（用延时线把一组算子时分复用给所有通道）。
3. 说清楚「TDM 共享算子」为什么需要 `channels_g - 1` 拍的积分器反馈延时、以及 `channels_g * diff_delay_g` 拍的梳状器延时。
4. 理解 `cfg` 变体如何把比率从综合期常量变成运行时端口，以及它为此新增的三个配置接口 `cfg_ratio_i` / `cfg_shift_i` / `cfg_gain_corr_i` 与动态移位器。

## 2. 前置知识

本讲默认你已经掌握以下内容（来自前置讲义）：

- **CIC 原理与位增长**（u6-l1）：抽取 CIC = 高速 N 级积分器 → ↓R → 低速 N 级梳状器；直流增益 \(G=(R\cdot M)^N \)，累加器位增长 \(B_{add}=\lceil\log_2 G\rceil \)。
- **CIC 命名规则**（u6-l1）：`cic_<int|dec>_<fix|cfg>_<nch|1ch>_<input>_<output>_<arch>`，单通道时省略 input/output 字段。
- **增益校正两段式**（u6-l1）：粗校正（恒做，右移 \(B_{add}\) 位）+ 精校正（乘 \(G_c=2^{B_{add}}/G\)，落进单个 DSP48）。
- **单通道抽取器**（u6-l2）：`Rcnt` 抽取计数、`AccuFmt_c`/`DiffFmt_c` 格式推导、AXI-S 握手。
- **两段式编码与 record 流水封装**（u3-l3）：组合进程 `p_comb` 写 `r_next`、时序进程 `p_seq` 仅打拍，valid 用 `std_logic_vector` 切片逐级平移。
- **位真双模型与 preScript 协同仿真**（u3-l2）：Python 模型是黄金参考，测试台逐位比对、`###ERROR###` 为唯一失败判据。

如果对「为什么累加器要加 \(B_{add}\) 位」「为什么粗校正要右移」还有疑问，请先回看 u6-l1。

补充一个本讲反复用到的术语：

- **TDM（Time-Division Multiplexing，时分复用）**：把 N 个通道的数据轮流放在同一根信号线上，第 0 拍传通道 0、第 1 拍传通道 1、……、第 N-1 拍传通道 N-1，如此循环。一组完整的 N 个样本称为一个 **TDM 帧（frame）**。
- **par（parallel，并行）**：N 个通道各有独立信号线，同一拍同时给出 N 个样本。

## 3. 本讲源码地图

本讲围绕三个 CIC 抽取器实体展开，它们的命名恰好覆盖了「多通道组织 × 比率可配」的两个维度：

| 组件 | 输入 | 输出 | 比率 | 角色 |
|------|------|------|------|------|
| `psi_fix_cic_dec_fix_nch_par_tdm` | par | tdm | fix（综合期常量） | 多通道、并行累加 |
| `psi_fix_cic_dec_fix_nch_tdm_tdm` | tdm | tdm | fix | 多通道、TDM 共享算子 |
| `psi_fix_cic_dec_cfg_nch_tdm_tdm` | tdm | tdm | cfg（运行时可改） | 在 tdm_tdm 基础上加运行时配置 |

关键文件：

- [hdl/psi_fix_cic_dec_fix_nch_par_tdm.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_fix_nch_par_tdm.vhd) — 并行输入 / TDM 输出 / 固定比率。
- [hdl/psi_fix_cic_dec_fix_nch_tdm_tdm.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_fix_nch_tdm_tdm.vhd) — TDM 输入 / TDM 输出 / 固定比率，本讲「TDM 共享算子」的主样板。
- [hdl/psi_fix_cic_dec_cfg_nch_tdm_tdm.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_cfg_nch_tdm_tdm.vhd) — 在 tdm_tdm 基础上把比率改为运行时可配。
- [hdl/CicNaming.txt](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/CicNaming.txt) — CIC 命名规则权威说明。
- [model/psi_fix_cic_dec.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_cic_dec.py) — 三者共用的位真 Python 模型（与 RTL 实现无关）。
- [testbench/psi_fix_cic_dec_cfg_nch_tdm_tdm_tb/psi_fix_cic_dec_cfg_nch_tdm_tdm_tb.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_cic_dec_cfg_nch_tdm_tdm_tb/psi_fix_cic_dec_cfg_nch_tdm_tdm_tb.vhd) — cfg 变体的测试台，演示如何从外部计算并驱动三个 `cfg_*` 端口。
- [sim/config.tcl](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl) — 回归脚本，声明三个组件的测试台与参数矩阵。

> 提示：`doc/files/psi_fix_cic_dec_cfg_nch_tdm_tdm.md` 明确写道——cfg 变体「与 fix 版本相同，但抽取比率可在运行时选择」，且「静态移位被替换为两级流水化的动态移位」。本讲就是把这两句话展开讲清楚。

---

## 4. 核心概念与源码讲解

### 4.1 多通道 par/tdm 组合

#### 4.1.1 概念说明

CIC 命名规则的第四、第五字段描述数据怎么进、怎么出：

```
psi_fix_cic_dec_fix_nch_<input>_<output>
                               ^^^^^^   ^^^^^^^
                               输入组织   输出组织
```

[CicNaming.txt:17-25](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/CicNaming.txt#L17-L25) 给出取值：`par`（并行，每通道一根线）/ `tdm`（时分复用，所有通道共享一根线）/ `-`（单通道时省略）。

本模块聚焦 **par_tdm** 组合：输入端 N 个通道并行到达（一拍同时给出 N 个样本），输出端把它们串行化成 TDM 流（单根 `dat_o`，通道轮流出现）。这种结构对应「多片 ADC 同步并行采样、后续处理链路复用一条数据通路」的常见场景。

为什么输出统一做成 TDM？因为抽取后的采样率低（Fs/R），后续的增益校正、FIR 等级只需较低速率即可完成，用一根 TDM 线串行送出比并行 N 根线省得多的布线与引脚资源。

#### 4.1.2 核心流程

par_tdm 抽取器的数据流分为四段：

```
 并行输入(dat_i: N路同时)
        │
        ▼
 [N 级积分器]  ← 每通道各自独立累加（2D 数组：stage × channel）
        │  高速域（每个 input vld 都更新）
        ▼
 [抽取 ↓R]     ← Rcnt 计数，每 R 个输入样本触发一次
        │
        ▼
 [par→tdm 串行化] ← psi_common_par_tdm：一拍并行进，N 拍串行出
        │  低速域
        ▼
 [N 级梳状器 + 增益校正]  ← 在 TDM 域工作，延时按 TDM 拍数计
        │
        ▼
   TDM 输出(dat_o)
```

关键设计抉择：

1. **积分器并行、梳状器串行**。所有通道的积分器必须高速运行（在输入采样率 Fs 下），所以干脆给每个通道配一份独立的累加器寄存器——这就是「2D 数组」的由来。梳状器在抽取后低速域（Fs/R）运行，且天然适合 TDM，所以串行复用。
2. **抽取点正好在 par→tdm 转换之前**。Rcnt 在高速域计数，每 R 个输入样本产生一个 `VldParTdm` 脉冲，触发串行化器吐出一整帧（N 个通道）样本。
3. **通道数有下限**。三个多通道实体都在 `p_seq` 里断言 `channels_g >= 2`（见 [psi_fix_cic_dec_fix_nch_par_tdm.vhd:206-208](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_fix_nch_par_tdm.vhd#L206-L208)），单通道场景应使用 `cic_dec_fix_1ch`。

#### 4.1.3 源码精读

**(a) 并行输入端口宽度**

[psi_fix_cic_dec_fix_nch_par_tdm.vhd:29](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_fix_nch_par_tdm.vhd#L29) 定义了并行输入：`dat_i` 的位宽是 `psi_fix_size(in_fmt_g) * channels_g`，即把 N 个通道的定点样本首尾相接拼成一根宽总线。而输出 `dat_o` 只有 `psi_fix_size(out_fmt_g)`——单通道宽度，证实了「并行进、TDM 出」。

```vhdl
dat_i  : in  std_logic_vector(psi_fix_size(in_fmt_g) * channels_g - 1 downto 0);
dat_o  : out std_logic_vector(psi_fix_size(out_fmt_g) - 1 downto 0);
```

**(b) 2D 累加器数组——「每通道一份」**

[psi_fix_cic_dec_fix_nch_par_tdm.vhd:50-51](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_fix_nch_par_tdm.vhd#L50-L51) 定义了二维数组：`AccuStage_t` 是单个通道的累加值，`Accus_t` 是「stage × channel」的二维结构：

```vhdl
type AccuStage_t is array (natural range <>) of std_logic_vector(psi_fix_size(AccuFmt_c) - 1 downto 0);
type Accus_t      is array (natural range <>) of AccuStage_t(0 to channels_g - 1);
```

因此 `r.Accu(stage)(ch)` 精确表示「第 stage 级积分器、第 ch 通道」的累加值。这与下面 tdm_tdm 的「一维数组 + 延时线」形成鲜明对比。

**(c) 每通道独立累加**

[psi_fix_cic_dec_fix_nch_par_tdm.vhd:107-126](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_fix_nch_par_tdm.vhd#L107-L126) 用 `for ch in 0 to channels_g-1 loop` 循环为每个通道各算一次 `psi_fix_add`。由于所有通道同一拍到达，它们共享同一组 `VldAccu` 有效信号，但累加器寄存器各自独立：

```vhdl
if r.VldAccu(0) = '1' then
  for ch in 0 to channels_g - 1 loop
    v.Accu(1)(ch) := psi_fix_add(r.Accu(1)(ch), AccuFmt_c,
                                 r.Input_0(ch), in_fmt_g, AccuFmt_c);
  end loop;
end if;
```

**(d) 抽取计数与 par→tdm 串行化**

[psi_fix_cic_dec_fix_nch_par_tdm.vhd:128-138](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_fix_nch_par_tdm.vhd#L128-L138) 是高速域的抽取计数：每来一个输入有效，`Rcnt` 减一；归零时拉高 `VldParTdm` 一拍，并重装为 `ratio_g - 1`：

```vhdl
v.VldParTdm := '0';
if r.VldAccu(order_g - 1) = '1' then
  if r.Rcnt = 0 then
    v.VldParTdm := '1';
    v.Rcnt      := ratio_g - 1;
  else
    v.Rcnt := r.Rcnt - 1;
  end if;
end if;
```

随后 [psi_fix_cic_dec_fix_nch_par_tdm.vhd:226-243](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_fix_nch_par_tdm.vhd#L226-L243) 例化外部库组件 `psi_common_par_tdm`，在 `VldParTdm` 触发下把并行 N 路样本串行成 N 拍 TDM 流，喂给后续梳状器。每个通道在进入串行化前都先做了一次 `psi_fix_shift_right(..., Shift_c, ...)`（[第 227 行](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_fix_nch_par_tdm.vhd#L227)），即增益校正的「粗校正右移」。

> 注意 `Shift_c` 在这里是综合期常量（由 `ratio_g` 推出），所以这次移位最终综合成一堆固定连线，不耗移位逻辑——这正是 `cfg` 变体做不到、必须改用动态移位器的地方（见 4.3）。

#### 4.1.4 代码实践

**实践目标**：通过对照端口宽度与数组维度，确认 par_tdm「并行进、TDM 出」的结构，并验证每通道累加器的独立性。

**操作步骤**：

1. 打开 [psi_fix_cic_dec_fix_nch_par_tdm.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_fix_nch_par_tdm.vhd)。
2. 在第 29 行确认 `dat_i` 位宽含 `* channels_g` 因子，`dat_o` 不含。
3. 在第 50-51 行数出数组的维度：`Accus_t` 的第二维是 `0 to channels_g-1`。
4. 在第 109-114 行确认累加发生在 `for ch` 循环内，每个通道各自加到自己的 `r.Accu(1)(ch)`。

**需要观察的现象**：累加逻辑里 **没有任何** `psi_common_delay` 把反馈延迟 `channels_g-1` 拍（对比 4.2 的 tdm_tdm）——因为并行结构每个通道有独立寄存器，反馈就是「自己加自己」，无需跨通道延时。

**预期结果**：你能用一句话概括——「par_tdm 用空间换时间：N 个通道各占一份累加器，所以积分器反馈无需延时线」。

**待本地验证**：若你已搭好 PsiSim 环境，可在 `sim/` 下跑 `psi_fix_cic_dec_fix_nch_par_tdm_tb`（参数见 [config.tcl:341-346](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L341-L346)），观察输出 `vld_o` 的节拍：每 `ratio_g` 个输入有效后才出现一串 `channels_g` 个连续的输出有效（一帧）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 par_tdm 的积分器用 2D 数组、而单通道 `cic_dec_fix_1ch` 用 1D 数组？

> **答案**：并行输入意味着 N 个通道同一拍都要更新各自的累加值，因此每个积分级需要 N 个独立寄存器，自然成为「stage × channel」的二维数组；单通道只有一路数据，一级一个寄存器即可，用一维数组。

**练习 2**：如果一个系统有 8 路 16-bit ADC 并行输出，希望抽取后用一条 TDM 总线送入下游 FIR，应选哪个 CIC？为什么不用 8 份 `cic_dec_fix_1ch`？

> **答案**：选 `cic_dec_fix_nch_par_tdm`。它天然接收并行输入、输出 TDM 流，且梳状器与增益校正低速部分被 N 个通道复用，比 8 份独立单通道组件省下大量低速域逻辑与布线。

---

### 4.2 TDM 共享算子

#### 4.2.1 概念说明

现在考虑另一种更常见的场景：**输入本身就是 TDM**——例如 ADC 已经把多路样本时分复用到一根线上送进来。此时如果仍给每个通道配一份独立累加器，就浪费了：因为任意一拍只有一个通道的数据在场，累加器大部分时间在空转。

psi_fix 的做法是 **TDM 共享算子**：只用 **一组** 积分器/梳状器寄存器，让所有通道「轮流」使用它。秘诀在于——积分器的反馈不是「上一拍的值」，而是「**同一通道上一次的值**」。由于同一通道两次出现之间隔了 `channels_g` 拍，所以只要把反馈路径延时 `channels_g - 1` 拍，单组寄存器就能在每个通道「自己的时刻」读回它自己的历史值。

这就是 tdm_tdm 变体（`psi_fix_cic_dec_fix_nch_tdm_tdm`）的核心思想：**用延时线把一组算子时分复用给 N 个通道**。

#### 4.2.2 核心流程

tdm_tdm 的数据流与单通道几乎同构，只多了「通道计数」与「跨通道延时」：

```
TDM 输入(dat_i: 一拍一个通道，循环 ch0,ch1,...,chN-1)
        │
        ▼
 [N 级积分器]  ← 单组寄存器 + IntDel(channels_g-1) 反馈延时
        │  每个 vld 都更新（高速域）
        ▼
 [抽取 ↓R + Chcnt 通道计数]
        │  Rcnt 在「整帧边界」递减；Rcnt=0 的那一帧整帧输出
        ▼
 [N 级梳状器]  ← DiffDel(channels_g*diff_delay_g) 延时
        │
        ▼
   TDM 输出(dat_o)
```

两个关键计数器协同工作（见 [psi_fix_cic_dec_fix_nch_tdm_tdm.vhd:119-139](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_fix_nch_tdm_tdm.vhd#L119-L139)）：

- `Chcnt`：帧内通道计数，从 `channels_g-1` 数到 0，标志一个完整 TDM 帧。
- `Rcnt`：帧间抽取计数，**只在帧边界**（`Chcnt=0`）递减；当 `Rcnt=0` 时，整帧 `channels_g` 个样本都释放到梳状器。

效果上：每 `ratio_g` 个输入帧产生一个抽取输出帧（含 `channels_g` 个样本），**每通道抽取比恰为 \(R\)**，符合 CIC 抽取定义。位增长与增益校正的推导与单通道完全一致，因为对单个通道而言，它看到的就是一个采样率为 Fs/R 的连续流。

#### 4.2.3 源码精读

**(a) 一维数组 + 单组累加器**

[psi_fix_cic_dec_fix_nch_tdm_tdm.vhd:50-51](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_fix_nch_tdm_tdm.vhd#L50-L51) 只有一维数组——每级积分器只有一个累加寄存器，被所有通道共享：

```vhdl
type Accus_t is array (natural range <>) of std_logic_vector(psi_fix_size(AccuFmt_c) - 1 downto 0);
```

对比 par_tdm 的二维 `Accus_t is array of AccuStage_t(0 to channels_g-1)`，差别一目了然。

**(b) 积分器反馈来自 `IntDel(1)` 而非 `r.Accu(1)`**

[psi_fix_cic_dec_fix_nch_tdm_tdm.vhd:102-108](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_fix_nch_tdm_tdm.vhd#L102-L108) 是关键：

```vhdl
if r.VldAccu(0) = '1' then
  v.Accu(1) := psi_fix_add(IntDel(1), AccuFmt_c,   -- ← 反馈来自 IntDel(1)
                           r.Input_0, in_fmt_g,
                           AccuFmt_c);
end if;
```

`IntDel(1)` 是 `r.Accu(1)` 经一条 `channels_g - 1` 拍延时线后的值（见下方 g_intdel）。换言之，本次累加加的不是「上一拍」而是「**channels_g 拍前**」——对每个通道而言，那正是它自己上一次的累加结果。于是单组寄存器被 N 个通道分时复用，每个通道都拥有「虚拟的私有累加器」。

**(c) IntDel 延时线 = channels_g - 1**

[psi_fix_cic_dec_fix_nch_tdm_tdm.vhd:250-268](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_fix_nch_tdm_tdm.vhd#L250-L268) 例化 `psi_common_delay`，把每级积分器输出延时 `channels_g - 1` 拍作为反馈：

```vhdl
i_del : entity work.psi_common_delay
  generic map( width_g => psi_fix_size(AccuFmt_c),
               delay_g => channels_g - 1,   -- ← 关键：跨通道延时
               rst_state_g => true )
  port map( dat_i => r.Accu(stage + 1), ... dat_o => IntDel(stage + 1) );
```

> 这就是本讲的核心公式：**TDM 共享算子 = 单组寄存器 + (channels_g - 1) 反馈延时**。

**(d) 梳状器延时 = channels_g × diff_delay_g**

[psi_fix_cic_dec_fix_nch_tdm_tdm.vhd:225-248](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_fix_nch_tdm_tdm.vhd#L225-L248) 中，梳状器延时为 `channels_g * diff_delay_g`。道理相同：单通道梳状器要减去 `diff_delay_g` 拍前的值；TDM 下「同一通道差分延时 M」对应 TDM 拍数 `M × channels_g`。注意 par_tdm 的梳状器延时同样是 `channels_g * diff_delay_g`（[psi_fix_cic_dec_fix_nch_par_tdm.vhd:257](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_fix_nch_par_tdm.vhd#L257)），因为两者在抽取后都进入了 TDM 域。

**(e) 通道帧计数与抽取释放**

[psi_fix_cic_dec_fix_nch_tdm_tdm.vhd:119-139](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_fix_nch_tdm_tdm.vhd#L119-L139) 实现「帧内 Chcnt + 帧间 Rcnt」双计数，并在 `Rcnt=0` 时把整帧释放（`VldDiff(0):='1'` 连续 `channels_g` 拍）。复位时 `Chcnt <= channels_g - 1`（[第 213 行](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_fix_nch_tdm_tdm.vhd#L213)），与计数方向一致。

#### 4.2.4 代码实践

**实践目标**：亲手验证「TDM 共享算子」的延时线长度公式。

**操作步骤**：

1. 打开 [psi_fix_cic_dec_fix_nch_tdm_tdm.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_fix_nch_tdm_tdm.vhd)。
2. 在第 105 行确认第一级积分器反馈用的是 `IntDel(1)` 而非 `r.Accu(1)`。
3. 跳到第 250-268 行 `g_intdel`，记录 `delay_g => channels_g - 1`。
4. 跳到第 225-248 行 `g_diffdel`，记录 `delay_g => channels_g * diff_delay_g`。

**需要观察的现象**：所有跨通道延时都精确地以 `channels_g` 为因子。

**预期结果**：你能填出下表——

| 算子 | 单通道延时 | TDM 域延时 |
|------|-----------|-----------| 
| 积分器反馈（相邻样本） | 1 拍 | `channels_g - 1` 拍 |
| 梳状器差分（差分延时 M） | `diff_delay_g` 拍 | `channels_g * diff_delay_g` 拍 |

**待本地验证**：若运行 [config.tcl:350-356](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L350-L356) 的 `psi_fix_cic_dec_fix_nch_tdm_tdm_tb`，可在波形上看到 `r.Chcnt` 在 0..channels_g-1 间循环，且每 `ratio_g` 个帧边界才出现一串连续 `channels_g` 拍的输出有效。

#### 4.2.5 小练习与答案

**练习 1**：为什么积分器反馈延时是 `channels_g - 1` 而不是 `channels_g`？

> **答案**：积分器本身在 `p_seq` 里已经打了一拍寄存器（`r <= r_next`）。要让反馈对准「同一通道上一次的值」，需要总延时等于通道周期 `channels_g` 拍。寄存器贡献 1 拍，延时线再补 `channels_g - 1` 拍，合计 `channels_g`。

**练习 2**：把 `channels_g` 从 3 改成 5，IntDel 与 DiffDel 的延时分别变成多少（`diff_delay_g=1`）？

> **答案**：IntDel = `5 - 1 = 4` 拍；DiffDel = `5 * 1 = 5` 拍。

**练习 3**：par_tdm 与 tdm_tdm 的梳状器延时都是 `channels_g * diff_delay_g`，但两者的积分器结构完全不同。请解释为什么梳状器延时却一致。

> **答案**：抽取之后，两种结构的数据都已进入 TDM 域（par_tdm 经 par→tdm 串行化，tdm_tdm 本来就是 TDM）。在 TDM 域里，单通道的 `diff_delay_g` 样本间隔对应 `channels_g * diff_delay_g` 个 TDM 拍，所以梳状器延时公式相同；差别只在积分器域（并行 vs 共享）。

---

### 4.3 运行时配置比率（cfg 变体）

#### 4.3.1 概念说明

前面两个 `fix` 变体把抽取比率 `ratio_g` 烧死在综合期。但很多应用（软件无线电、可重构采集）要求**上电后由软件动态选择抽取比率**——例如先 10× 抽取看全景，再切到 100× 抽取看细节。重新综合.bit 太慢，于是 psi_fix 提供 `cfg` 变体：比率可在运行时（确切说是**复位期间**）改写。

代价是显而易见的：综合器必须按**最坏情况**（最大比率）来定宽数据通路，因为累加器位宽在综合后就固定了。于是 `ratio_g` 被 `max_ratio_g` 取代，累加器格式按 `max_ratio_g` 推导。真正影响「当前抽取多少」的三个量——比率值、粗校正移位量、精校正系数——则作为**运行时端口**由外部（FPGA 里的软核或寄存器）送进来。

> 这正是本讲规格里的实践任务：**对比 fix 与 cfg 在比率参数上的差异，说明 cfg 需要新增哪些运行时接口**。

#### 4.3.2 核心流程

cfg 变体在 tdm_tdm 的基础上做了三处替换，其余结构（积分器、梳状器、增益校正流水）完全不变：

```
   fix 变体                        cfg 变体
   ─────────                       ─────────
   ratio_g (常量)          ──▶     max_ratio_g (定宽上限) + cfg_ratio_i (运行时比率)
   Shift_c (常量移位)      ──▶     cfg_shift_i (运行时移位) → psi_common_dyn_sft 动态移位器
   Gc_c   (常量系数)       ──▶     cfg_gain_corr_i (运行时系数 [0,1,16])
```

外部控制器（或测试台）必须自行计算并下发这三个值。实体头部的注释给出了公式（[psi_fix_cic_dec_cfg_nch_tdm_tdm.vhd:16-21](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_cfg_nch_tdm_tdm.vhd#L16-L21)）：

\[
\text{CIC\_GAIN} = (\text{Ratio}\cdot \text{DifDelay})^{\text{Order}}
\]

\[
\text{CIC\_GROWTH} = \lceil \log_2(\text{CIC\_GAIN}) \rceil, \quad
\text{SHIFT} = \text{CIC\_GROWTH}, \quad
\text{GAINCORR} = \frac{2^{\text{CIC\_GROWTH}}}{\text{CIC\_GAIN}}
\]

- `cfg_shift_i` ← SHIFT
- `cfg_gain_corr_i` ← GAINCORR（定点化为 `[0,1,16]`）
- `cfg_ratio_i` ← Ratio − 1（0 表示不抽取，3 表示抽取 4，依此类推）

注意一个关键架构后果：`fix` 里的 `psi_fix_shift_right(..., Shift_c, ...)` 因为移位量是常量，综合后只是一堆固定连线（见 u2-l2）；而 `cfg` 里移位量是运行时变量，**不能再** 用 `psi_fix_shift_right`，必须换成真正的动态移位器 `psi_common_dyn_sft`。这就是 `doc/files/psi_fix_cic_dec_cfg_nch_tdm_tdm.md` 所说「静态移位被替换为流水化的动态移位」的含义。

#### 4.3.3 源码精读

**(a) 用 max_ratio_g 取代 ratio_g，并新增三个 cfg 端口**

[psi_fix_cic_dec_cfg_nch_tdm_tdm.vhd:23-49](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_cfg_nch_tdm_tdm.vhd#L23-L49) 是 cfg 实体。generic 用 `max_ratio_g` 替换 `ratio_g`；端口新增 `cfg_ratio_i` / `cfg_shift_i` / `cfg_gain_corr_i`：

```vhdl
max_ratio_g      : natural              := 12;   -- 最大支持比率，决定数据通路宽度
...
cfg_ratio_i     : in std_logic_vector(log2ceil(max_ratio_g) - 1 downto 0); -- Ratio-1
cfg_shift_i     : in std_logic_vector(7 downto 0);                          -- 移位量（≤255）
cfg_gain_corr_i : in std_logic_vector(16 downto 0);                         -- 增益系数 [0,1,16]
```

注释明确：配置「只能在复位时改」（*only change when in reset!*）——因为运行中改比率会破坏积分器/延时线里残留的、按旧比率积累的状态。

**(b) 累加器按最坏情况定宽**

[psi_fix_cic_dec_cfg_nch_tdm_tdm.vhd:54-57](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_cfg_nch_tdm_tdm.vhd#L54-L57) 用 `max_ratio_g` 推 `MaxCicGain_c` / `MaxCicAddBits_c`，累加器格式 `AccuFmt_c` 的整数位按最大位增长定宽：

```vhdl
constant MaxCicGain_c    : real := (real(max_ratio_g) * real(diff_delay_g))**real(order_g);
constant MaxCicAddBits_c : integer := log2ceil(MaxCicGain_c - 0.1);
constant AccuFmt_c       : psi_fix_fmt_t := (in_fmt_g.S, in_fmt_g.I + MaxCicAddBits_c, in_fmt_g.F);
```

对比 fix 版本的 `AccuFmt_c`（[psi_fix_cic_dec_fix_nch_tdm_tdm.vhd:42](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_fix_nch_tdm_tdm.vhd#L42)）用的是 `CicAddBits_c`（当前比率的位增长）。这就是 cfg 的资源代价：**永远按最大比率占用累加器位宽，哪怕实际跑小比率**。

**(c) 抽取计数器从 cfg_ratio_i 重装**

[psi_fix_cic_dec_cfg_nch_tdm_tdm.vhd:156-171](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_cfg_nch_tdm_tdm.vhd#L156-L171) 是比率可配的核心——`Rcnt` 归零时不再写死 `ratio_g - 1`，而是写 `to_integer(unsigned(cfg_ratio_i))`：

```vhdl
if r.Rcnt = 0 then
  v.Rcnt := to_integer(unsigned(cfg_ratio_i));   -- ← 运行时比率
else
  v.Rcnt := r.Rcnt - 1;
end if;
```

`Rcnt` 的范围也按 `max_ratio_g - 1` 定（[第 76 行](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_cfg_nch_tdm_tdm.vhd#L76)），与最宽数据通路匹配。

**(d) 动态移位器替换常量移位**

[psi_fix_cic_dec_cfg_nch_tdm_tdm.vhd:261-280](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_cfg_nch_tdm_tdm.vhd#L261-L280) 例化 `psi_common_dyn_sft`，移位量来自 `cfg_shift_i`（低 `log2ceil(MaxShift_c+1)` 位，见 [第 262 行](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_cfg_nch_tdm_tdm.vhd#L262)）：

```vhdl
i_sft : entity work.psi_common_dyn_sft
  generic map( direction_g => "RIGHT", sel_bit_per_stage_g => 4,
               max_shift_g => MaxShift_c, width_g => psi_fix_size(SftFmt_c),
               sign_extend_g => true )
  port map( vld_i => r.VldAccu(order_g), shift_i => ShiftSel,
            dat_i => ShiftDataIn, vld_o => ShiftVld, dat_o => ShiftDataOut );
```

由于动态移位器引入了若干拍流水延迟，cfg 变体额外加了 `SftVldCnt`（[第 78、144-149、209 行](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_cfg_nch_tdm_tdm.vhd#L142-L149)）来跟踪「有多少样本还在移位器流水线里」，供 `busy_o` 正确反映状态。

**(e) 增益系数从端口直接进乘法器**

[psi_fix_cic_dec_cfg_nch_tdm_tdm.vhd:193-206](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_cfg_nch_tdm_tdm.vhd#L193-L206)：精校正乘法不再用常量 `Gc_c`，而是直接乘 `cfg_gain_corr_i`，并把该系数同时寄存一份（`v.GcCoef := cfg_gain_corr_i`，[第 195 行](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_cfg_nch_tdm_tdm.vhd#L195)）。

```vhdl
v.GcMult_1 := psi_fix_mult(r.GcIn_0, GcInFmt_c,
                           cfg_gain_corr_i, GcCoefFmt_c,    -- ← 运行时系数
                           GcMultFmt_c, psi_fix_trunc, psi_fix_wrap);
```

> 三处替换之外，积分器、梳状器、增益校正流水与 tdm_tdm 完全相同——这正是 `doc/files/psi_fix_cic_dec_cfg_nch_tdm_tdm.md` 只讲「差异」、其余「refer to fix 版本」的原因。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：对比 fix 与 cfg 两类 CIC 在比率参数上的差异，并用 Python 模型亲手算出 cfg 变体所需的三个运行时参数，与测试台对照。

**操作步骤**：

1. **对照 fix 与 cfg 的比率处理差异**。打开下面两段代码并填表：

   | 关注点 | fix（[tdm_tdm.vhd:39-42](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_fix_nch_tdm_tdm.vhd#L39-L42)） | cfg（[cfg...tdm_tdm.vhd:54-57](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_cfg_nch_tdm_tdm.vhd#L54-L57)） |
   |--------|------|-----|
   | 比率来源 | `ratio_g`（综合期常量） | `max_ratio_g` 定宽 + `cfg_ratio_i` 运行时 |
   | 位增长依据 | 当前比率 `CicAddBits_c` | 最大比率 `MaxCicAddBits_c` |
   | 粗校正移位 | 常量 `Shift_c`，综合为连线 | 运行时 `cfg_shift_i`，需动态移位器 |
   | 精校正系数 | 常量 `Gc_c` | 运行时 `cfg_gain_corr_i` |

2. **列出 cfg 新增的运行时接口**。从 [cfg 实体端口（第 34-49 行）](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_cfg_nch_tdm_tdm.vhd#L34-L49) 抄出三个端口及其语义：`cfg_ratio_i`（Ratio−1）、`cfg_shift_i`（SHIFT）、`cfg_gain_corr_i`（GAINCORR，定点 `[0,1,16]`）。

3. **用 Python 模型算出这三个值**。新建一个脚本（**示例代码**，非项目原有文件），复用 [model/psi_fix_cic_dec.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_cic_dec.py) 已有的常量推导：

   ```python
   # 示例代码：计算 cfg 变体所需的运行时参数（order=4, diff_delay=2）
   from math import ceil, log2
   from psi_fix_pkg import *     # 需 sys.path 指向 model/

   ratio, diff_delay, order = 9, 2, 4
   cic_gain    = (ratio * diff_delay) ** order
   cic_growth  = ceil(log2(cic_gain))                 # = SHIFT
   gain_corr   = 2 ** cic_growth / cic_gain           # GAINCORR（浮点）
   gc_coef     = psi_fix_from_real(gain_corr, psi_fix_fmt_t(0, 1, 16))  # 定点化
   print("cfg_ratio_i    =", ratio - 1)
   print("cfg_shift_i    =", cic_growth)
   print("cfg_gain_corr_i bits =", psi_fix_get_bits_as_int(gc_coef, psi_fix_fmt_t(0, 1, 16)))
   ```

4. **与测试台对照**。打开 [testbench/psi_fix_cic_dec_cfg_nch_tdm_tdm_tb/psi_fix_cic_dec_cfg_nch_tdm_tdm_tb.vhd:42-49](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_cic_dec_cfg_nch_tdm_tdm_tb/psi_fix_cic_dec_cfg_nch_tdm_tdm_tb.vhd#L42-L49)，确认测试台正是用同样的公式算 `CicGain_c` / `CicGrowth_c` / `Shift_c` / `GainCorrCoef_c`，并接到 DUT 的 `cfg_ratio_i` / `cfg_shift_i` / `cfg_gain_corr_i`（[第 88-90 行](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_cic_dec_cfg_nch_tdm_tdm_tb/psi_fix_cic_dec_cfg_nch_tdm_tdm_tb.vhd#L88-L90)）。

**需要观察的现象**：你算出的 `cfg_shift_i`、`cfg_gain_corr_i` 应与测试台常量完全一致；`cfg_ratio_i` 是 `ratio_g - 1`（注意减一）。

**预期结果**：对于 `order=4, ratio=9, diff_delay=2`，应得到 `cfg_ratio_i = 8`、`cfg_shift_i = cic_growth`、`cfg_gain_corr_i` 为一个 `[0,1,16]` 定点整数。**待本地验证**：在你的 Python 环境里实际运行上述脚本以确认数值（因涉及 `log2ceil` 的具体取整，留作本地核验）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `cfg_ratio_i` 编码为「Ratio − 1」而不是直接 Ratio？

> **答案**：抽取计数器 `Rcnt` 的范围是 `0 .. max_ratio_g - 1`。用 Ratio−1 作为重装值，可使「Ratio=1（不抽取）」对应 `cfg_ratio_i = 0`，计数器每拍都归零、每帧都释放，语义自然；同时端口位宽 `log2ceil(max_ratio_g)` 恰好能表示 `0 .. max_ratio_g-1`，无浪费。

**练习 2**：cfg 变体的注释强调「配置只能在复位时改」。如果在运行中改 `cfg_ratio_i` 会出什么问题？

> **答案**：积分器、IntDel/DiffDel 延时线里残留着按**旧比率**积累的样本与状态。运行中切换比率会让新计数周期与旧残留状态错配，输出在一段时间内既不是旧比率也不是新比率的正确结果。复位会清空这些状态，所以要求在复位时改。

**练习 3**：既然 cfg 的累加器按 `max_ratio_g` 定宽，实际跑小比率时多余的高位是否浪费？

> **答案**：是。这是 cfg 为「运行时灵活」付出的资源代价：累加器永远按最坏情况位增长占用位宽与 DSP 资源，即便实际抽取比远小于 `max_ratio_g`。比率确定不变的应用应优先用 fix 变体以省资源。

---

## 5. 综合实践

**任务**：为一个「4 通道、采样率可软件切换的采集前端」选择 CIC 组件并完成配置。

场景设定：

- 4 路 ADC 数据以 **TDM** 方式送入 FPGA（一根数据线，4 个通道轮流）。
- 软件需要在运行时在 **抽取 8×** 与 **抽取 16×** 之间切换。
- `order = 4`，`diff_delay = 1`。

请完成：

1. **选型**。从本讲三个组件中选出合适的一个，并说明理由（输入组织、输出组织、比率是否需运行时可改）。
2. **定宽**。确定应设置多大的 `max_ratio_g`，以及累加器因此多出多少整数位（相对抽取 8× 而言）。
3. **算参数**。分别计算抽取 8× 与 16× 两种比率下，软件需要下发到 `cfg_ratio_i` / `cfg_shift_i` / `cfg_gain_corr_i` 的值（用 4.3.4 的 Python 脚本，**待本地验证**具体数值）。
4. **解释延时**。说明该组件的积分器反馈延时线与梳状器延时线分别应是多少拍。
5. **回归**。指出在 [config.tcl](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl) 中哪一段会跑该组件的测试台，以及测试台用什么机制（preScript + 位真比对 + `###ERROR###`）来保证两种比率下都与 Python 黄金参考逐位一致。

**参考思路**：

1. 选 `psi_fix_cic_dec_cfg_nch_tdm_tdm`：输入是 TDM、输出也走 TDM、比率需运行时可改——三字段全部命中。
2. `max_ratio_g` 至少取 16；累加器位增长按 16× 推（比 8× 多出若干位），这是 cfg 的资源代价。
3. 用实体头部公式（[cfg...tdm_tdm.vhd:16-21](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cic_dec_cfg_nch_tdm_tdm.vhd#L16-L21)）分别算两种比率，`cfg_ratio_i` = Ratio−1。
4. 积分器反馈延时 = `channels_g - 1 = 3` 拍；梳状器延时 = `channels_g * diff_delay_g = 4 * 1 = 4` 拍。
5. 见 [config.tcl:449-455](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L449-L455)，preScript 用 [model/psi_fix_cic_dec.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_cic_dec.py) 生成期望文本，测试台逐行 `StdlvCompareInt` 比对，不符即打印 `###ERROR###`。

---

## 6. 本讲小结

- **命名即规格**：CIC 名字的 `<input>_<output>` 字段（par/tdm/-）直接告诉你数据怎么进、怎么出；`<fix|cfg>` 告诉你比率是否运行时可改。
- **par_tdm**：并行输入给每通道配独立累加器（2D 数组），抽取后用 `psi_common_par_tdm` 串行化成 TDM 流；积分器反馈无需延时线。
- **tdm_tdm**：单组算子被所有通道时分复用，秘诀是积分器反馈延时 `channels_g - 1` 拍、梳状器延时 `channels_g * diff_delay_g` 拍——本讲的核心公式。
- **抽取双计数**：`Chcnt`（帧内通道）+ `Rcnt`（帧间比率）协同，每 `ratio_g` 个输入帧释放一个含 `channels_g` 个样本的输出帧，每通道抽取比恰为 R。
- **cfg 变体**：`ratio_g` 拆成 `max_ratio_g`（定宽数据通路）+ `cfg_ratio_i`（运行时比率），并新增 `cfg_shift_i` / `cfg_gain_corr_i` 两个运行时接口；常量移位被替换为 `psi_common_dyn_sft` 动态移位器。
- **代价**：cfg 永远按最大比率占用累加器位宽；配置只能在复位时改。比率确定的应用应选 fix。

---

## 7. 下一步学习建议

本讲把 CIC 家族讲完了。接下来建议：

1. **进入 FIR 家族（单元 7）**。FIR 与 CIC 都是采样率变换滤波器，但 FIR 用乘法器、系数可任意；建议先读 u7-l1「FIR 命名规则与架构选择」，与本讲的 CIC 命名规则对照——两者的 `<channels>` / `<coefficient-handling>` 字段高度同构，`conf`（FIR 的运行时系数）对应这里的 `cfg`（运行时比率）。
2. **关注 param_ram 与 cfg 的呼应**。u4-l2 讲过的 `psi_fix_param_ram` 正是 FIR `conf` 变体「运行时换系数」的存储底座；可对比 CIC `cfg`「运行时换比率」用了什么（端口 + 动态移位器，而非 RAM）。
3. **深读 psi_common**。本讲引用了 `psi_common_par_tdm`、`psi_common_delay`、`psi_common_dyn_sft` 三个外部组件。若你想理解 TDM 串行化与动态移位器的内部实现，建议到并排摆放的 `psi_common` 库里读它们的源码。
4. **动手扩展**。试着仿照 cfg 变体，把 `cic_int_fix_1ch`（u6-l2 的插值器）改造成「运行时可配插值比率」的版本——列出需要新增哪些端口、哪些常量要改成 max 版本。
