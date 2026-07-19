# FIR 多通道可配置滤波器

## 1. 本讲目标

本讲在 u7-l1 建立的「FIR 六字段命名规则 + ser/par/semi 三种计算结构」全景图之上，把镜头推进到三个真实可综合实体的内部实现：

- `psi_fix_fir_dec_ser_nch_chtdm_conf`（串行、抽取、可配置比率与抽头数）
- `psi_fix_fir_par_nch_chtdm_conf`（并行、非抽取、每抽头一个乘法器）
- `psi_fix_fir_dec_semi_nch_chtdm_conf`（半并行、抽取、乘法器数量可调）

学完本讲，你应当能够：

1. 说清三种 FIR 在「数据流组织、乘法器数量、累加方式」上的实现差异与各自适用场景。
2. 理解 `psi_fix_param_ram` 双口 RAM 如何充当「运行时系数存储」，以及 `use_fix_coefs_g` 如何在综合期二选一切换「RAM / ROM」。
3. 掌握 `multipliers_g`、`ratio_g`、`clk_per_spl_g`（以及 ser 的 `duty_cycle_g`）如何共同决定单个样本的处理周期数与吞吐可行性边界。
4. 读懂组件用来报告「正在计算」的 `busy_o` / `CalcOngoing`、以及 semi 用来清空延时线的 `flush_mem_i`/`flush_done_o` 接口。

## 2. 前置知识

阅读本讲前，请先确认你理解以下概念（它们来自前置讲义，这里只做最小回顾）：

- **位真双模型**：每个 VHDL FIR 组件都共用同一份 Python 黄金参考 `model/psi_fix_fir.py`，组件实现只负责与黄金模型逐位对齐，命名变体只描述「RTL 怎么算」而不改变数学（见 u3-l1、u7-l1）。
- **累加器格式契约**：FIR 黄金模型规定 `accuFmt = (1, outFmt.I+1, inFmt.F+coefFmt.F)`，并假设累加器中途可以回绕、只在输出处做舍入与饱和（见 u7-l1）。
- **两段式编码 (two-process method)**：组合进程 `p_comb` 描述 `r_next`、时序进程 `p_seq` 只打拍；`v := r` 让未赋值字段默认保持；valid 数组住进 record 并逐级平移（见 u3-l3）。
- **AXI-S 握手与命名**：`dat/vld/rdy`，端口后缀 `_i/_o`，generic 后缀 `_g`，常量后缀 `_c`（见 u1-l4、u3-l3）。
- **TDM（时分复用）**：多个通道轮流共用一根数据线，连续 `channels_g` 个样本构成一个 TDM 帧，每帧为每个通道提供一个样本（见 u6-l3）。
- **`psi_fix_param_ram`**：纯 VHDL 真双口 RAM，A/B 两口对称、可接不同时钟，`behavior_g` 取 `RBW`（先读后写）或 `WBR`（先写后读），`init_g` 在综合期把前 N 个浮点系数量化为定点初值（见 u4-l2）。

## 3. 本讲源码地图

本讲涉及的关键文件及其职责：

| 文件 | 作用 |
| --- | --- |
| [hdl/psi_fix_fir_dec_ser_nch_chtdm_conf.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_dec_ser_nch_chtdm_conf.vhd) | 串行 FIR：1 个乘法器逐抽头计算，运行时可配比率与抽头数。 |
| [hdl/psi_fix_fir_par_nch_chtdm_conf.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_par_nch_chtdm_conf.vhd) | 并行 FIR：每抽头一个乘法器，DSP 加法链累加，非抽取。 |
| [hdl/psi_fix_fir_dec_semi_nch_chtdm_conf.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_dec_semi_nch_chtdm_conf.vhd) | 半并行 FIR：`multipliers_g` 个乘法器构成 MAC 链，抽取。 |
| [hdl/psi_fix_param_ram.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_param_ram.vhd) | 真双口参数 RAM，作为系数的运行时存储载体（conf 模式）。 |
| [hdl/psi_fix_mult_add_stage.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mult_add_stage.vhd) | 乘加级，par/semi 都靠它拼出 DSP 加法链。 |
| [model/psi_fix_fir.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_fir.py) | 三种 FIR 共用的位真黄金参考模型。 |
| [sim/config.tcl](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl) | 回归脚本，声明三个 FIR 测试台及其 generics 参数矩阵。 |
| [hdl/FirNaming.txt](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/FirNaming.txt) | FIR 命名规则权威定义（u7-l1 已建立）。 |

## 4. 核心概念与源码讲解

### 4.1 ser/par/semi 三种 FIR 的实现差异

#### 4.1.1 概念说明

三种 FIR 的本质区别是**「用多少个乘法器、在多少个时钟周期内算完一个输出样本」**：

- **ser（serial）**：只用 1 个乘法器，把 `taps_g` 个抽头一个接一个地乘加。算一个输出样本需要约 `taps_g` 个时钟周期。资源最省、吞吐最低。
- **par（parallel）**：每个抽头配 1 个乘法器，`taps_g` 个乘法器排成一条 DSP 加法链，**1 个时钟周期就能算完一个样本**。资源最贵、吞吐最高（每拍一个样本）。
- **semi（semi-parallel）**：用 `multipliers_g` 个乘法器（介于 1 和 `taps_g` 之间），把抽头分成若干组、每组 `multipliers_g` 个，跨多个周期算完。算一个样本需要 \(\lceil \text{taps\_g}/\text{multipliers\_g} \rceil\) 个周期。这是 ser 与 par 之间一条**可调的滑动条**。

三种都支持**多通道 TDM**：输入 `dat_i` 是单根线，通道轮流到达；都支持**系数可配置**（`conf`）。命名上的差别直接对应实现，参见 [hdl/FirNaming.txt:9-12](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/FirNaming.txt#L9-L12)。

> 注意：`par` 这个变体名字里**没有 `dec`**，意味着它不抽取、吞吐为 1 样本/时钟；`ser` 与 `semi` 名字里有 `dec`，是抽取 FIR。

#### 4.1.2 核心流程

**ser 的数据流**（差分式「读 RAM → 乘 → 累加 → 出」）：

```
dat_i/vld_i ──► [通道计数] ──► 写入数据RAM(每通道一段)
                                  │
        抽取计数 cfg_ratio_i ──► 触发一次计算
                                  │
   对每个通道(0..channels_g-1):
       对每个抽头(0..taps_g-1):
           读 数据RAM[通道][tap_addr]
           读 系数RAM/ROM[tap_addr]
           accu += data * coef
       舍入+饱和 ──► dat_o/vld_o
```

ser 的关键资源：1 个乘法器 + 1 块真双口数据 RAM（深度 `max_taps_g + max_ratio_g`，按通道分段寻址）。

**par 的数据流**（每抽头一个 DSP，加法链横向求和）：

```
dat_i ──► [slice0: mult+add] ──► [delay ch-1] ──► [slice1] ──► ... ──► [slice N-1] ──► round+sat ──► dat_o
                  chain_add_o──────────────► chain_add_i(1) ─────────► ... ──►
```

每个 `psi_fix_mult_add_stage` 是一个 DSP slice：输入 a 是数据、输入 b 是该抽头系数，`chain_add_i/o` 把乘积沿链累加。多通道时，相邻 slice 之间插入 `channels_g-1` 拍的延时线，让第 k 个抽头恰好看到「比第 k-1 个抽头晚一整帧」的样本。

**semi 的数据流**（`multipliers_g` 条 MAC 链，跨周期循环累加）：

```
dat_i ──► 写入 数据RAM(每个乘法器一块)
             │
   抽取计数 ratio_g ──► 触发一次计算
             │
   对每个通道:
       循环 CyclesPerCalc = ceil(taps/multipliers) 次:
           每个乘法器读 [data, coef] ──► mult ──► 加法链(chain) ──► 累加到 Accu_8n
       舍入+饱和 ──► dat_o/vld_o
```

semi 与 par 共用 `psi_fix_mult_add_stage`，但 semi 的 MAC 链是**时间复用**的：同一条链在 `CyclesPerCalc_c` 个周期里处理 `TapsPerStage_c` 组抽头，外部累加器 `Accu_8n` 把各周期的部分和累加起来。

#### 4.1.3 源码精读

**(a) ser——单乘法器逐抽头累加。** 关键在 9 级流水线里 Stage 6（乘）与 Stage 7（累加）的配合：

[hdl/psi_fix_fir_dec_ser_nch_chtdm_conf.vhd:256-271](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_dec_ser_nch_chtdm_conf.vhd#L256-L271) —— Stage 6 做单次满精度乘法，Stage 7 在每个新通道开始时（`First(6)='1'`）把累加器清零、否则累加；注释明确「溢出会在计算结束时互补、中途不需舍入」，这正是黄金模型「累加器可回绕、只输出处量化」的契约。

ser 的抽取与「逐通道、逐抽头」循环控制见 [hdl/psi_fix_fir_dec_ser_nch_chtdm_conf.vhd:173-212](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_dec_ser_nch_chtdm_conf.vhd#L173-L212)：`DecCnt_1` 数到 0 才触发计算（抽取），`TapCnt_1` 倒数抽头、`CalcChnl_1` 推进通道，`Last(1)` 标记一个通道的最后一个抽头。ser 还把比率与抽头数做成了**运行时端口** `cfg_ratio_i` / `cfg_taps_i`（见 [hdl/psi_fix_fir_dec_ser_nch_chtdm_conf.vhd:50-51](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_dec_ser_nch_chtdm_conf.vhd#L50-L51)），所以它是三兄弟里唯一能在运行时改比率/抽头数的（`max_ratio_g`/`max_taps_g` 只是为最坏情况定宽）。

> 一个细节：ser 在 Stage 4 有 `ReplaceZero_4` 机制（[hdl/psi_fix_fir_dec_ser_nch_chtdm_conf.vhd:228-248](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_dec_ser_nch_chtdm_conf.vhd#L228-L248)）。复位后数据 RAM 里是垃圾，但计算可能已经读到「尚未被写入的有效抽头位置」；为保位真，它把那些位置替换成 0，直到首轮抽头真正被填满。

**(b) par——每抽头一个 DSP slice，加法链求和。** par 不写两段式 record，而是直接例化 `taps_g` 个 `psi_fix_mult_add_stage`：

[hdl/psi_fix_fir_par_nch_chtdm_conf.vhd:109-176](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_par_nch_chtdm_conf.vhd#L109-L176) —— 第 0 个 slice 特殊（链输入接 0），其余 slice 用 `for-generate` 批量例化；相邻 slice 之间插入 `psi_common_delay`（`delay_g = channels_g-1`），见 [hdl/psi_fix_fir_par_nch_chtdm_conf.vhd:138-151](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_par_nch_chtdm_conf.vhd#L138-L151)，单通道时该延时被短路（`g_1ch`）。链末端 `DspAccuChain(taps_g-1)` 就是完整卷积和，再做舍入+饱和输出（[hdl/psi_fix_fir_par_nch_chtdm_conf.vhd:181-198](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_par_nch_chtdm_conf.vhd#L181-L198)）。

**(c) semi——`multipliers_g` 条 MAC 链 + 外部累加。** semi 复用 par 的 DSP slice 思路，但抽头数远大于乘法器数，于是**时间复用**：

[hdl/psi_fix_fir_dec_semi_nch_chtdm_conf.vhd:377-513](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_dec_semi_nch_chtdm_conf.vhd#L377-L513) —— `g_mac` 循环例化 `multipliers_g` 个 `psi_fix_mult_add_stage`，每个 slice 配一块独立的数据 RAM 与系数 RAM/ROM；链输出 `AccuChain(7+multipliers_g)` 在 Stage 8 被 `Accu_8n` 跨周期累加（[hdl/psi_fix_fir_dec_semi_nch_chtdm_conf.vhd:303-314](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_dec_semi_nch_chtdm_conf.vhd#L303-L314)）。

semi 的累加器格式比 ser/par 多出 `log2ceil(multipliers_g)` 个整数位：

[hdl/psi_fix_fir_dec_semi_nch_chtdm_conf.vhd:78](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_dec_semi_nch_chtdm_conf.vhd#L78) —— `AccuFmt_c = (1, in_fmt_g.I + coef_fmt_g.I + log2ceil(multipliers_g), in_fmt_g.F + coef_fmt_g.F)`。原因是 MAC 加法链在一个周期内会把 `multipliers_g` 个乘积相加（链式 add），这部分位增长用 `log2ceil(multipliers_g)` 兜底；而 ser（单乘法器）与 par（每抽头一个 slice、链只累加固定 `taps_g` 项且受滤波器增益约束）则沿用黄金模型的 `outFmt.I+1`。对照 ser 的 [hdl/psi_fix_fir_dec_ser_nch_chtdm_conf.vhd:73](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_dec_ser_nch_chtdm_conf.vhd#L73) 与 par 的 [hdl/psi_fix_fir_par_nch_chtdm_conf.vhd:55](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_par_nch_chtdm_conf.vhd#L55) 即可看到差异。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：通过比对三种 FIR 的累加器格式与乘法器数量，验证「ser/par 用同一累加格式、semi 多一截」这一结论。
2. **操作步骤**：
   - 打开 `model/psi_fix_fir.py` 第 40 行，确认黄金模型 `self.accuFmt = psi_fix_fmt_t(1, outFmt.i + 1, inFmt.f + coefFmt.f)`。
   - 打开 ser 第 73 行、par 第 55 行、semi 第 78 行，分别记下三者的 `AccuFmt_c` 整数位表达式。
3. **需要观察的现象**：ser 与 par 的整数位是 `out_fmt_g.I + 1`，与黄金模型一致；semi 的整数位是 `in_fmt_g.I + coef_fmt_g.I + log2ceil(multipliers_g)`，多了 `log2ceil(multipliers_g)`。
4. **预期结果**：你能用一句话解释「为什么只有 semi 需要额外整数位」——因为它在一个时钟周期内通过加法链合并 `multipliers_g` 个乘积。
5. 本实践为纯源码阅读，运行结果「待本地验证」（若要验证可跑相应回归，见 4.3）。

#### 4.1.5 小练习与答案

**练习 1**：给定 `taps_g=64`，分别用 ser、par、`multipliers_g=8` 的 semi 实现，各自需要多少个乘法器？算一个样本各需多少周期？

答案：ser 用 1 个乘法器、约 64 周期/样本；par 用 64 个乘法器、1 周期/样本；semi 用 8 个乘法器、\(\lceil 64/8\rceil=8\) 周期/样本。

**练习 2**：为什么 par 的名字里没有 `dec`，而 ser/semi 有？

答案：`dec` 表示支持抽取；par 是非抽取结构、每拍输出一个样本，故省略 `dec`；ser/semi 通过 `ratio_g`/`cfg_ratio_i` 做抽取，名字带 `dec`（见 [hdl/FirNaming.txt:5-7](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/FirNaming.txt#L5-L7)）。

### 4.2 系数 RAM 配置

#### 4.2.1 概念说明

三种 FIR 都用 generic `use_fix_coefs_g : boolean` 在综合期二选一：

- `use_fix_coefs_g = true` → **fix（固定系数）**：系数在综合期就烧死，实现为 ROM（或常量数组），资源最省、不可更改。
- `use_fix_coefs_g = false` → **conf（可配置系数）**：系数存放在 `psi_fix_param_ram` 双口 RAM 里，运行时可通过专用端口写入，**所有通道共用同一组系数**（这正是命名规则里 `<coefficient-handling>` 取 `conf` 的含义，见 [hdl/FirNaming.txt:22-24](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/FirNaming.txt#L22-L24)）。

`psi_fix_param_ram`（见 u4-l2）之所以适合当系数 RAM，是因为它的 A/B 两口可接不同时钟：**A 口接配置时钟域写系数、B 口接数据时钟域读系数**，从而实现「不停机换系数」——数据流照常跑，软件在另一时钟域把新系数灌进 RAM。

#### 4.2.2 核心流程

系数配置的通用模式（ser 为例）：

```
use_fix_coefs_g = false (conf):
   coef_if_clk_i/addr_i/wr_i/wr_dat_i ──► [param_ram A口写] ──► RAM
                                                                │
                            数据时钟域 r.CoefRdAddr_2 ──► [param_ram B口读] ──► CoefRamDout_3 ──► 乘法器

use_fix_coefs_g = true (fix):
   coefs_g(常量数组) ──综合期──► CoefRom[] ──► r.CoefRdAddr_2 寻址 ──► CoefRamDout_3 ──► 乘法器
```

无论 fix 还是 conf，下游「乘法器输入」看到的都是同一个 `CoefRamDout_3` 信号——**两种实现共享同一份计算流水线**，只是系数来源不同。这是整个 FIR 家族「同一份 RTL、`use_fix_coefs_g` 二选一」的关键设计。

`psi_fix_param_ram` 内部用 `shared variable` 才能让 A/B 两个进程在不同时钟沿并发访问同一存储阵列，`behavior_g` 默认 `RBW`（同地址同时读写时返回旧值）贴合 FPGA BRAM 的 READ_FIRST 语义：

[hdl/psi_fix_param_ram.vhd:66-95](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_param_ram.vhd#L66-L95) —— `RBW` 先把旧值读到 `Dout`、再写入；`WBR` 先写、再读出新值。综合期初值由 `GetInit` 用 `psi_fix_from_real` 把 `init_g` 的浮点系数量化（[hdl/psi_fix_param_ram.vhd:50-59](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_param_ram.vhd#L50-L59)）。

#### 4.2.3 源码精读

**(a) ser——一块系数 RAM（或 ROM）服务全部抽头。** ser 的系数存储是「全库最教科书」的二选一切换：

[hdl/psi_fix_fir_dec_ser_nch_chtdm_conf.vhd:332-371](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_dec_ser_nch_chtdm_conf.vhd#L332-L371) —— `g_nFixCoef`（conf 分支）例化一块 `psi_fix_param_ram`，A 口接 `coef_if_clk_i`/`coef_if_addr_i`/`coef_if_wr_i`/`coef_if_wr_dat_i`（配置接口），B 口接 `clk_i` 与读地址 `r.CoefRdAddr_2`，输出 `CoefRamDout_3`；`g_FixCoef`（fix 分支）用 `for-generate` 在综合期把 `coefs_g` 烧进 `CoefRom` 数组，再用一个读进程按 `CoefRdAddr_2` 输出。注意两个分支的输出都落到同一个 `CoefRamDout_3`，下游无感知。

**(b) par——系数寄存器阵列，复位期自动初始化。** par 不用 RAM，而是用一组寄存器 `CoefReg`：

[hdl/psi_fix_fir_par_nch_chtdm_conf.vhd:80-103](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_par_nch_chtdm_conf.vhd#L80-L103) —— conf 模式下，写使能 `coef_if_wr_i` 配合地址 `coef_if_wr_addr_i` 选中某一个抽头寄存器更新（`CoefWe`）；复位时若 `CoefRstDone='0'`，则用 `coefs_g` 一次性初始化全部抽头，保证上电就有合法系数。

**(c) semi——每个乘法器配一块独立系数 RAM。** semi 因为有 `multipliers_g` 条 MAC 链、每条链负责 `TapsPerStage_c` 个抽头，所以系数被切成 `multipliers_g` 段：

[hdl/psi_fix_fir_dec_semi_nch_chtdm_conf.vhd:322-333](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_dec_semi_nch_chtdm_conf.vhd#L322-L333) —— 单个 `coef_addr_i` 被解码：判断它落在哪一段 `[m*TapsPerStage, (m+1)*TapsPerStage)`，把写使能 `CoefWrStg(m)` 与段内地址 `CoefAddrStg(m)` 分别派发给第 m 块 RAM；每块 RAM 实例化见 [hdl/psi_fix_fir_dec_semi_nch_chtdm_conf.vhd:466-489](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_dec_semi_nch_chtdm_conf.vhd#L466-L489)，深度 `2**log2ceil(TapsPerStage_c)`、初值由 `GetCoefsReal(i)` 生成。对外仍是一组统一的「地址 + 写使能 + 数据」接口，段划分对用户透明。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：确认「conf/fix 两种模式共享同一份计算流水线」这一设计在三种 FIR 中都成立。
2. **操作步骤**：
   - 在 ser 中找到 `g_nFixCoef` / `g_FixCoef` 两个 generate（[hdl/psi_fix_fir_dec_ser_nch_chtdm_conf.vhd:332-371](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_dec_ser_nch_chtdm_conf.vhd#L332-L371)），确认二者输出信号名相同。
   - 在 semi 中找到 `g_coefrom` / `g_coefram`（[hdl/psi_fix_fir_dec_semi_nch_chtdm_conf.vhd:452-489](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_dec_semi_nch_chtdm_conf.vhd#L452-L489)），确认二者都把系数送到同一个 `Coef_4i`。
3. **需要观察的现象**：切换 `use_fix_coefs_g` 时，仅系数来源（RAM vs ROM）变化，乘法器、累加器、输出流水完全不变。
4. **预期结果**：你能画出「系数 RAM/ROM → `Coef_4i` → `mult_add_stage.dat_b_i`」这条公共路径，说明位真模型无需关心系数怎么存。
5. 本实践为源码阅读，运行结果「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：ser 的系数 RAM 为什么用双口、且 A 口接 `coef_if_clk_i`、B 口接 `clk_i`？

答案：A 口在配置时钟域由软件写系数，B 口在数据时钟域由滤波器自己读系数；双口 + 异步时钟让「换系数」与「数据流处理」互不阻塞，实现不停机更新。

**练习 2**：semi 用 `multipliers_g=4` 处理 `taps_g=48`，需要几块系数 RAM？每块深度（按地址位）是多少？

答案：需要 4 块；`TapsPerStage_c = ceil(48/4) = 12`，每块深度 `2**log2ceil(12) = 16`（向上取 2 的幂），地址位 `log2ceil(12) = 4` 位。

### 4.3 吞吐与资源调度

#### 4.3.1 概念说明

三种 FIR 的吞吐与资源由三组参数共同决定，但要区分「组件 generic」与「测试台 generic」：

| 参数 | 归属 | 含义 |
| --- | --- | --- |
| `multipliers_g` | 组件 generic（仅 semi） | 并行乘法器数量，决定单样本计算周期。 |
| `ratio_g` | 组件 generic（ser 用 `cfg_ratio_i`，semi 用 `ratio_g`） | 抽取比率，输出样本率 = 输入样本率 / ratio。 |
| `clk_per_spl_g` | **测试台** generic | 测试台每 `clk_per_spl_g` 个时钟喂一个输入样本（控制输入速率）。 |
| `duty_cycle_g` | **测试台** generic（ser TB） | ser 测试台里等价的「每样本时钟数」旋钮。 |

> 重要：`clk_per_spl_g` 与 `duty_cycle_g` **不是组件 generic**，它们只出现在测试台里（见 [testbench/psi_fix_fir_dec_semi_nch_chtdm_conf_tb/psi_fix_fir_dec_semi_nch_chtdm_conf_tb.vhd:37](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_fir_dec_semi_nch_chtdm_conf_tb/psi_fix_fir_dec_semi_nch_chtdm_conf_tb.vhd#L37)），被传给 `psi_tb` 的 `ApplyTextfileContent` 的 `ClkPerSpl` 参数（[...tb.vhd:215](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_fir_dec_semi_nch_chtdm_conf_tb/psi_fix_fir_dec_semi_nch_chtdm_conf_tb.vhd#L215)），用来调节喂数据的快慢。组件本身的处理能力只由 `multipliers_g`、`ratio_g`、`taps_g`、`channels_g` 决定。

此外，semi 还提供两个面向软件的辅助接口：

- **`busy_o` / `CalcOngoing`**：组件正在接收数据或计算时拉高，提示外部「别在此时改配置」。
- **`flush_mem_i` / `flush_done_o`**（`impl_flush_if_g=true` 时生效）：发一个脉冲把所有数据 RAM 清零，复位后用来消除延时线里的残留数据（注释明确「复位后不冲刷延时线会有暂态」，见 [hdl/psi_fix_fir_dec_semi_nch_chtdm_conf.vhd:15-16](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_dec_semi_nch_chtdm_conf.vhd#L15-L16)）。

#### 4.3.2 核心流程

**单样本处理周期数**（这是本模块的核心公式，以 semi 为例）：

一个输出样本（单个通道）需要把 `taps_g` 个抽头全部乘加一遍。semi 有 `multipliers_g` 个乘法器并行工作，每个周期处理 `multipliers_g` 个抽头，故：

\[
\text{CyclesPerCalc\_c} = \left\lceil \frac{\text{taps\_g}}{\text{multipliers\_g}} \right\rceil
\]

对应源码常量 [hdl/psi_fix_fir_dec_semi_nch_chtdm_conf.vhd:80](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_dec_semi_nch_chtdm_conf.vhd#L80)。一次抽取点触发后，要为**所有通道**各算一遍，故一次「计算爆发」耗时 `channels_g × CyclesPerCalc_c` 个周期。

**吞吐可行性边界**（与组件内的断言对应）：

TDM 输入下，一个 TDM 帧 = `channels_g` 个样本，喂一帧耗时 `channels_g × clk_per_spl_g` 个时钟。抽取点每 `ratio_g` 帧出现一次，故两次抽取点之间的时间窗口为：

\[
T_{\text{dec}} = \text{ratio\_g} \cdot \text{channels\_g} \cdot \text{clk\_per\_spl\_g}
\]

组件必须在这个窗口内算完一次爆发（`channels_g × CyclesPerCalc_c`），否则下一个抽取点到来时上一轮还没算完。化简后得到 semi 的吞吐可行性条件：

\[
\left\lceil \frac{\text{taps\_g}}{\text{multipliers\_g}} \right\rceil \;\le\; \text{ratio\_g} \cdot \text{clk\_per\_spl\_g}
\]

这正是 semi 组件内那条断言要检查的——当一次新计算启动（`CalcStartLoop_1='1'`）时，上一轮必须已经结束（或恰在最后一拍）：

[hdl/psi_fix_fir_dec_semi_nch_chtdm_conf.vhd:175-179](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_dec_semi_nch_chtdm_conf.vhd#L175-L179) —— 不满足就报 `###ERROR###: ... insufficient processing power ... (multipliers_g is set too low!)`。

**`full_inp_rate_support_g` 的作用**：当输入真的每拍都来（`clk_per_spl_g=1`）时，数据 RAM 的「写」与「读」会在同地址同沿冲突。此时 semi 切换到用 `psi_common_delay` 移位寄存器链承载延时数据（[hdl/psi_fix_fir_dec_semi_nch_chtdm_conf.vhd:422-438](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_dec_semi_nch_chtdm_conf.vhd#L422-L438)），并放宽断言；若 `full_inp_rate_support_g=false`，则禁止连续两拍 `vld_i='1'`（[hdl/psi_fix_fir_dec_semi_nch_chtdm_conf.vhd:169-173](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_dec_semi_nch_chtdm_conf.vhd#L169-L173)），依赖 RAM 的 read-before-write 行为。

#### 4.3.3 源码精读

**(a) semi 的计算调度核心。** Stage 2 是半并行的调度心脏：

[hdl/psi_fix_fir_dec_semi_nch_chtdm_conf.vhd:236-279](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_dec_semi_nch_chtdm_conf.vhd#L236-L279) —— `CalcStartLoop_1` 触发后置 `CalcRunning(2)`、加载 `CalcCycLeft_2 = CyclesPerCalc_c - 1`，随后每拍 `CalcCycLeft_2` 递减、`TapRdAddr_2` 递增、`CoefRdIdx_2` 递减；倒数到 1 时拉 `CalcLast(2)`，本通道算完则切下一通道（`CalcChannel_2 + 1`、重载起始抽头），所有通道算完则清 `CalcRunning(2)`。

**(b) `busy_o` / `CalcOngoing` 的来源。** 三个组件都把「输入有效 + 计算进行中 + 输出待发」三者之一为真视为忙：

- semi：[hdl/psi_fix_fir_dec_semi_nch_chtdm_conf.vhd:336-346](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_dec_semi_nch_chtdm_conf.vhd#L336-L346)
- ser：[hdl/psi_fix_fir_dec_ser_nch_chtdm_conf.vhd:287-296](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_dec_ser_nch_chtdm_conf.vhd#L287-L296)

**(c) 回归脚本里的参数矩阵。** `config.tcl` 为每个 FIR 测试台声明多组 generics（同一 TB 换参数跑多轮）：

- ser TB：[sim/config.tcl:207-213](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L207-L213) —— 用 `duty_cycle_g`（32 / 4）与 `ram_behavior_g`（RBW / WBR）组合。
- par TB：[sim/config.tcl:220-227](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L220-L227) —— 用 `channels_g`（1/3）、`clk_per_spl_g`（1/5）、`use_fix_coefs_g` 组合。
- semi TB：[sim/config.tcl:229-241](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L229-L241) —— 8 组参数，覆盖 `multipliers_g`、`ratio_g`、`clk_per_spl_g`、`channels_g`、`taps_g`、`use_fix_coefs_g`、`full_inp_rate_support_g`、`ram_behavior_g` 的多种组合，且每组都跑 `preScript.py` 生成位真数据（`tb_run_add_pre_script`）。

#### 4.3.4 代码实践（参数矩阵分析型，对应讲义指定实践）

1. **实践目标**：用上面的吞吐公式，逐组验证 `config.tcl` 里 semi 测试台的 8 组参数是否都满足可行性条件，从而理解 `multipliers_g`、`ratio_g`、`clk_per_spl_g` 如何共同决定样本处理周期数。
2. **操作步骤**：
   - 打开 [sim/config.tcl:233-240](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L233-L240)。
   - 对每一组，计算 `CyclesPerCalc_c = ⌈taps_g / multipliers_g⌉`，再与 `ratio_g × clk_per_spl_g` 比较。
   - 重点关注第 3 组（`taps=48, mult=8, ratio=3, clk_per_spl=2`）与第 5 组（`taps=48, mult=24, ratio=1, clk_per_spl=2`），它们恰好踩在边界上。
3. **需要观察的现象**：每组都应满足 `⌈taps/multipliers⌉ ≤ ratio × clk_per_spl`，否则组件会触发 `###ERROR###: insufficient processing power`。
4. **预期结果**（验算表，`Cyc=⌈taps/mult⌉`，`Win=ratio×clk_per_spl`）：

   | 组 | taps | mult | ratio | clk_per_spl | Cyc | Win=ratio×clk_per_spl | Cyc≤Win? |
   | --- | --- | --- | --- | --- | --- | --- | --- |
   | 1 | 48 | 8 | 3 | 10 | 6 | 30 | ✓ 富余 |
   | 2 | 48 | 10 | 3 | 10 | 5 | 30 | ✓ 富余 |
   | 3 | 48 | 8 | 3 | 2 | 6 | 6 | ✓ 恰好边界 |
   | 4 | 160 | 40 | 12 | 2 | 4 | 24 | ✓ 富余 |
   | 5 | 48 | 24 | 1 | 2 | 2 | 2 | ✓ 恰好边界 |
   | 6 | 48 | 8 | 3 | 10 | 6 | 30 | ✓（full rate） |
   | 7 | 48 | 16 | 3 | 1 | 3 | 3 | ✓ 恰好边界（full rate, clk_per_spl=1） |
   | 8 | 48 | 16 | 3 | 1 | 3 | 3 | ✓ 恰好边界（full rate） |

   第 3、5、7、8 组都「卡在边界」，说明库里故意挑了这些参数来验证断言在最紧调度下仍成立。
5. 若你本地能跑回归（`source sim/run.tcl` 或 `runGhdl.tcl`），可观察这 8 组均无 `###ERROR###`；无法运行则上述验算即为结论，运行结果「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：semi 第 3 组参数 `taps=48, mult=8, ratio=3, clk_per_spl=2`，若把 `multipliers_g` 降到 4，会发生什么？

答案：`CyclesPerCalc = ⌈48/4⌉ = 12`，而 `ratio×clk_per_spl = 3×2 = 6`，`12 > 6`，违反可行性条件；下一个抽取点到来时上一轮未算完，触发 [hdl/psi_fix_fir_dec_semi_nch_chtdm_conf.vhd:175-179](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_dec_semi_nch_chtdm_conf.vhd#L175-L179) 的断言，打印 `###ERROR###: insufficient processing power (multipliers_g is set too low!)`。

**练习 2**：`clk_per_spl_g` 是组件 generic 还是测试台 generic？它影响组件综合后的资源吗？

答案：是测试台 generic（见 TB 第 37 行），只控制 `ApplyTextfileContent` 喂数据的节奏；不影响组件综合资源。组件资源由 `multipliers_g`/`taps_g`/`channels_g` 决定，`clk_per_spl_g` 只是验证时用来逼近吞吐边界。

**练习 3**：`flush_mem_i` 接口为什么是可选的（`impl_flush_if_g`）？

答案：复位后数据 RAM 里是上电初值/残留，前若干样本会带暂态。若应用能容忍暂态（如长期运行的流），可不实现该接口省资源；若需要干净启动（如每次采集前），则启用并在复位后发一个 `flush_mem_i` 脉冲清零全部数据 RAM，`flush_done_o` 回脉冲表示完成。

## 5. 综合实践

**任务**：为一个假想需求选择合适的 FIR 变体并配置参数，验证其可行性。

**需求**：输入是 4 通道 TDM 数据，每通道采样率要求每 2 个时钟来一个样本（即 `clk_per_spl_g=2`），需要抽取 4 倍（`ratio_g=4`），滤波器 64 个抽头，系数需要运行时可更新。

**请完成**：

1. 根据需求判断应选 ser、par 还是 semi，并说明理由（提示：需要抽取 → 排除 par；需要运行时换系数 → 三者都支持 `conf`；资源/吞吐折中 → 看你想要的乘法器数）。
2. 若选 semi，计算至少需要多少个 `multipliers_g` 才满足吞吐可行性条件 `⌈taps/multipliers⌉ ≤ ratio × clk_per_spl`。
3. 写出对应的 `config.tcl` 参数行（参照 [sim/config.tcl:233-240](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L233-L240) 的格式），并指出应使用 `use_fix_coefs_g=false`（conf 模式）。
4. 说明这个配置下，组件会在哪些情况下把 `busy_o` 拉高（参照 [hdl/psi_fix_fir_dec_semi_nch_chtdm_conf.vhd:336-346](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_dec_semi_nch_chtdm_conf.vhd#L336-L346)）。

**参考答案**：

1. 选 semi：需要抽取（排除 par），且希望在资源与吞吐间折中（ser 太慢、par 不抽取）；semi 还支持 `conf`。
2. `⌈64/M⌉ ≤ ratio×clk_per_spl = 4×2 = 8`，故 `M ≥ 8`（`⌈64/8⌉=8`，恰好满足）。`multipliers_g=8` 是最小可行值。
3. 参数行示例（示例代码，非项目原有）：
   ```
   "-gfile_folder_g=$dataDir -gchannels_g=4 -gtaps_g=64 -gclk_per_spl_g=2 -guse_fix_coefs_g=false -gmultipliers_g=8 -gratio_g=4 -gram_behavior_g=RBW -gfull_inp_rate_support_g=false -ginit_coefs_g=false"
   ```
4. `busy_o` 在以下任一情况拉高：输入 `vld_i='1'`（正在接收数据）、`CalcRunning` 任一级有效（正在计算）、或 `OutVld_n` 流水中有待发输出。

## 6. 本讲小结

- 三种 FIR 的差异是「乘法器数量 × 单样本周期」的折中：ser=1 个乘法器/~N 周期、par=N 个乘法器/1 周期、semi=`multipliers_g` 个乘法器/`⌈N/multipliers_g⌉` 周期。
- semi 因 MAC 加法链在一个周期内合并 `multipliers_g` 个乘积，累加器格式比 ser/par 多 `log2ceil(multipliers_g)` 个整数位（[hdl/psi_fix_fir_dec_semi_nch_chtdm_conf.vhd:78](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_dec_semi_nch_chtdm_conf.vhd#L78)）。
- 系数存储由 `use_fix_coefs_g` 在综合期二选一：`false`→`psi_fix_param_ram` 双口 RAM（conf，A 口配置时钟域写、B 口数据时钟域读，不停机换系数），`true`→ROM/常量（fix）；两种实现共享同一份计算流水线。
- semi 把系数切成 `multipliers_g` 段，每段一块独立 RAM，对外仍是统一的单组写接口（[hdl/psi_fix_fir_dec_semi_nch_chtdm_conf.vhd:322-333](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_dec_semi_nch_chtdm_conf.vhd#L322-L333)）。
- 吞吐可行性条件为 `⌈taps/multipliers⌉ ≤ ratio × clk_per_spl`，由组件内断言强制（[hdl/psi_fix_fir_dec_semi_nch_chtdm_conf.vhd:175-179](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_dec_semi_nch_chtdm_conf.vhd#L175-L179)）；`clk_per_spl_g`/`duty_cycle_g` 是测试台旋钮，不影响综合资源。
- `busy_o` 汇报「接收/计算/待发」三态之一；`flush_mem_i`/`flush_done_o` 是可选的延时线清零接口，用于消除复位后暂态。

## 7. 下一步学习建议

- **继续横向对比**：阅读 [hdl/psi_fix_fir_3tap_hbw_dec2.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_3tap_hbw_dec2.vhd)（u7-l3），看半带 FIR 如何利用抽头对称性做到「无乘法器」，与本讲的 ser/par/semi 形成资源优化的另一种思路。
- **深入乘加内核**：精读 [hdl/psi_fix_mult_add_stage.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mult_add_stage.vhd)，理解它如何映射到 Xilinx DSP48 的乘法器 + 加法器链 + 预/后寄存器，这是 par/semi 高 Fmax 的根基。
- **回到位真闭环**：阅读 `testbench/psi_fix_fir_dec_semi_nch_chtdm_conf_tb/Scripts/preScript.py`，看它如何用 `model/psi_fix_fir.py` 生成 `Data/Input_*.txt` 与 `Output_*.txt`，并体会 `config.tcl` 里 `tb_run_add_pre_script` 如何在每次仿真前重生成数据（承接 u3-l2）。
- **扩展到其他组件族**：学完 FIR 后，可进入单元 8（函数近似与代码生成）或单元 9（DDS/调制解调/噪声），它们会复用本讲建立的「参数矩阵回归 + 位真协同仿真」工程套路。
