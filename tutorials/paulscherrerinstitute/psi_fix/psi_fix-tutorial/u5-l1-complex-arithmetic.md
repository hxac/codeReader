# 复数运算族

## 1. 本讲目标

学完本讲，你应当能够：

- 说清复数加减、复数乘法、复数求模三种运算在 psi_fix 里的定点实现方式。
- 自己推导两个复数相乘后实部/虚部的中间定点格式，并对照 VHDL 里的常量验证一致性。
- 理解「复数乘复数」为何需要 4 个乘法器 + 1 个加法 + 1 个减法，以及 `in_a_is_cplx_g`/`in_b_is_cplx_g` 两个 generic 如何在「复数×实数」时砍掉乘法器省资源。
- 理解 `psi_fix_complex_abs` 用「平方 + 求和 + 平方根线性近似」求模，而**不是** CORDIC，并说清它为何要先做范围归一化。

## 2. 前置知识

本讲是「复数运算与 CORDIC」单元的第一讲，默认你已经掌握：

- **定点格式三元组 [s,i,f]** 与总位宽 `W = s+i+f`（见 u1-l4）。
- **位增长三规则**：加减法整数位 +1；舍入再 +1；两个有符号数相乘 `[1,a,b]×[1,c,d] = [1,a+c+1,b+d]`（见 u1-l4、u2-l2）。
- **两段式编码风格 (two-process method)**：组合进程 `p_comb` 写 `r_next`，时序进程 `p_seq` 只打拍；用 record `two_process_r` 封装流水线寄存器，valid 以数组住进 record 逐级平移（见 u3-l3）。
- **psi_fix_pkg 运算函数**：`psi_fix_add/sub/mult/resize` 的签名是 `(数据, 格式, 数据, 格式, 结果格式, round, sat)`，结果格式由调用者指定、函数不自动位增长（见 u2-l2）。
- **位真双模型 + 协同仿真**：每个组件配一个逐位一致的 Python 模型，preScript 把浮点结果用 `psi_fix_get_bits_as_int` 写成整数位模式文本，VHDL 测试台逐位比对，不一致就打印 `###ERROR###`（见 u3-l2）。
- **Manual Splitting（手动拆分流水）**：把「舍入级」和「饱和级」拆成两级寄存器以提升 Fmax，但与一次性 resize 位真等价（见 u2-l2、u4-l1）。

关于复数本身，只需要中学数学：一个复数 \( z = a + jb \)，其中 a 是实部 (Inphase, I)，b 是虚部 (Quadrature, Q)。本讲里「复数」就是一对定点数 `(I, Q)`，在端口上体现为 `dat_..._inp_i`（实部）和 `dat_..._qua_i`（虚部）两路并行。

## 3. 本讲源码地图

| 文件 | 作用 |
|:--|:--|
| [hdl/psi_fix_complex_addsub.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_complex_addsub.vhd) | 复数加减：实部、虚部各自独立做一次 add/sub，由 `add_sub_g` 选择。 |
| [hdl/psi_fix_complex_mult.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_complex_mult.vhd) | 复数乘法：4 个乘法 + 实部减、虚部加；含复数×实数省乘法器选项与可选深流水。 |
| [hdl/psi_fix_complex_abs.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_complex_abs.vhd) | 复数求模 \(|z|=\sqrt{I^2+Q^2}\)：平方、求和、线性近似开方，非 CORDIC。 |
| [model/psi_fix_complex_mult.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_complex_mult.py) | 复数乘法的 Python 位真模型（黄金参考），与 VHDL 逐位对齐。 |
| [testbench/psi_fix_complex_mult_tb/Scripts/preScript.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_complex_mult_tb/Scripts/preScript.py) | 协同仿真脚本：实例化 Python 模型、生成输入输出文本。 |

三个组件都遵循 u1-l2 建立的「一组件、五件套」组织方式（VHDL + Python 模型 + 测试台 + preScript + 文档），命名同构，靠文件名即可互相定位。

## 4. 核心概念与源码讲解

### 4.1 复数加减

#### 4.1.1 概念说明

两个复数 \( x=(a+jb) \)、\( y=(c+jd) \) 的加减，按实部、虚部分别独立运算：

\[
x+y = (a+c) + j(b+d), \qquad x-y = (a-c) + j(b-d)
\]

关键观察：**实部和虚部互不耦合**——实部只和实部运算，虚部只和虚部运算。所以复数加减本质上就是「两个独立的标量加减器」并排放，没有任何跨实虚部的数据通路。这也是为什么它是最简单的复数组件：不需要乘法，也没有实虚部交叉。

#### 4.1.2 核心流程

```text
输入 A=(Ainp, Aqua) 格式 in_a_fmt_g
输入 B=(Binp, Bqua) 格式 in_b_fmt_g
        │
        ├── 若 add_sub_g="ADD": 实部 = Ainp + Binp,  虚部 = Aqua + Bqua
        ├── 若 add_sub_g="SUB": 实部 = Ainp - Binp,  虚部 = Aqua - Bqua
        │
        ├── 两个操作数格式可能不同 → 用 SumFmt_c 对齐（整数位取大者+1，小数位取大者）
        │
        └── resize 到 out_fmt_g（round + sat）→ 输出 (OutI, OutQ)
```

注意：A、B 两个输入允许有不同的定点格式（如 A 是 [1,0,15]、B 是 [1,0,24]），所以在相加前必须先把它们对齐到一个公共格式 `SumFmt_c`。

#### 4.1.3 源码精读

实体用 generic `add_sub_g`（字符串 `"ADD"` 或 `"SUB"`）选择模式，并在实体语句里用 `assert` 做防呆：

[hdl/psi_fix_complex_addsub.vhd:L47-L48](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_complex_addsub.vhd#L47-L48) —— 实体制品阶段就检查 `add_sub_g` 必须是 ADD 或 SUB，写错直接 `severity error`。

公共求和格式 `SumFmt_c` 把两个不同输入格式对齐，并对加减法预留 +1 整数位（位增长规则）：

[hdl/psi_fix_complex_addsub.vhd:L56-L57](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_complex_addsub.vhd#L56-L57) —— `SumFmt_c := (max(in_a_fmt_g.S, in_b_fmt_g.S), max(in_a_fmt_g.I, in_b_fmt_g.I)+1, max(in_a_fmt_g.F, in_b_fmt_g.F))`：符号位取大者（有符号「吃掉」无符号）、整数位取大者再 +1（防加减溢出）、小数位取大者（不丢精度）。

核心运算分两路独立做，`if add_sub_g = "ADD"` 决定调 `psi_fix_add` 还是 `psi_fix_sub`：

[hdl/psi_fix_complex_addsub.vhd:L101-L112](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_complex_addsub.vhd#L101-L112) —— `AddII` 是实部 (Inphase·Inphase)、`AddQQ` 是虚部 (Quadrature·Quadrature)，两路完全对称、互不引用。注意中间一律用 `psi_fix_trunc, psi_fix_wrap`（`SumFmt_c` 已足够宽，实际不会触发截断/回绕，注释里写明「format sufficient」），真正的 round/sat 只在最后 resize 到 `out_fmt_g` 时发生。

整个实体用两段式 record `two_process_r` 封装（[hdl/psi_fix_complex_addsub.vhd:L60-L77](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_complex_addsub.vhd#L60-L77)），valid 数组 `Vld : std_logic_vector(0 to 3)` 随数据逐级平移，这正是 u3-l3 讲过的统一风格。

#### 4.1.4 代码实践

**实践目标**：确认 addsub 在 SUB 模式下确实只做实虚部独立减法，并观察 `pipeline_g` 对流水深度的改变。

**操作步骤**：

1. 打开 `hdl/psi_fix_complex_addsub.vhd`，在 [L107-L112](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_complex_addsub.vhd#L107-L112) 处确认 `add_sub_g="SUB"` 分支调的是 `psi_fix_sub`。
2. 看 [L130-L135](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_complex_addsub.vhd#L130-L135) 的输出 valid 选择：`pipeline_g=true` 时 `vld_o <= r.Vld(3)`，`pipeline_g=false` 时 `vld_o <= r.Vld(1)`。
3. 打开 `sim/config.tcl`，找到 `create_tb_run "psi_fix_complex_addsub_tb"`（约 [L376-L378](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L376-L378)），观察它如何用 `tb_run_add_arguments` 给同一个测试台传不同的 `pipeline_g` 跑多轮。

**需要观察的现象**：非流水模式下数据从输入到输出延迟 2 拍（Vld(1)）；流水模式延迟翻倍但 Fmax 更高。

**预期结果**：两种 `pipeline_g` 配置都通过位真比对（因为 Python 模型与两种流水深度都逐位等价）。

**待本地验证**：若你本地有 Modelsim/GHDL，可 `source ./sim/run.tcl` 跑回归确认 addsub_tb 报 `SIMULATIONS COMPLETED SUCCESSFULLY` 且无 `###ERROR###`。

#### 4.1.5 小练习与答案

**练习 1**：若 `in_a_fmt_g = (1,0,15)`、`in_b_fmt_g = (1,0,24)`，`SumFmt_c` 等于多少？

**答案**：`(max(1,1), max(0,0)+1, max(15,24)) = (1, 1, 24)`。整数位 +1 用于容纳加减法的位增长。

**练习 2**：为什么 addsub 实部、虚部两路可以并行、互不通信？

**答案**：复数加减的定义里实部只与实部运算、虚部只与虚部运算，两者没有交叉项（不像复数乘法有 `ad+bc` 这种交叉项），所以是两个完全独立的标量加减器。

---

### 4.2 复数乘法

#### 4.2.1 概念说明

复数乘法是本讲重头戏。两个复数 \( x=(a+jb) \)、\( y=(c+jd) \) 相乘：

\[
x \cdot y = (a+jb)(c+jd) = \underbrace{(ac - bd)}_{\text{实部}} + j\underbrace{(ad + bc)}_{\text{虚部}}
\]

把它拆成 4 个实数乘法和 1 加 1 减：

- 实部 = `MultII − MultQQ`，其中 `MultII = a·c`，`MultQQ = b·d`
- 虚部 = `MultIQ + MultQI`，其中 `MultIQ = a·d`，`MultQI = b·c`

所以一个完整的「复数×复数」要 **4 个乘法器 + 1 个减法器 + 1 个加法器**。当其中一个操作数其实是实数（虚部恒为 0）时，上面 4 个乘积里有 2 个必然为 0，对应的乘法器可以整个省掉——这就是 psi_fix 用 `in_a_is_cplx_g` / `in_b_is_cplx_g` 两个 generic 提供的「复数×实数」资源优化。

#### 4.2.2 核心流程

```text
A=(Ai,Aq), B=(Bi,Bq)
   │
   ├── 4 个并行乘法（结果量化到 internal_fmt_g，trunc+wrap）
   │     MultII = Ai·Bi      (永远需要)
   │     MultQQ = Aq·Bq      (仅 in_a_is_cplx 且 in_b_is_cplx 时需要，否则 0)
   │     MultIQ = Ai·Bq      (仅 in_b_is_cplx 时需要，否则 0)
   │     MultQI = Aq·Bi      (仅 in_a_is_cplx 时需要，否则 0)
   │
   ├── SumI = MultII − MultQQ   → 实部，格式 SumFmt_c (internal + 1 整数位)
   ├── SumQ = MultIQ + MultQI   → 虚部，格式 SumFmt_c
   │
   └── resize 到 out_fmt_g (round + sat)
        pipeline_g=false: 一次性 resize
        pipeline_g=true : Manual Splitting（先 round 级、再 sat 级）提升 Fmax
```

**资源账本**：复数×复数 = 4 乘法器；若 B 是实数（`in_b_is_cplx_g=false`），`MultIQ` 砍掉；若 A 是实数（`in_a_is_cplx_g=false`），`MultQI`、`MultQQ` 都砍掉。极端情况「实数×实数」只剩 1 个 `MultII`。

#### 4.2.3 源码精读

实体的格式 generics 有 4 个：两个输入格式、一个内部计算格式、一个输出格式，外加 round/sat 和两个 `is_cplx` 开关：

[hdl/psi_fix_complex_mult.vhd:L22-L33](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_complex_mult.vhd#L22-L33) —— 注意 `internal_fmt_g` 默认 `(1,1,24)`，它**不是**乘积的满精度格式，而是由用户决定的「内部精度旋钮」（见下面实践任务）：满精度乘积会被截断到这个格式，用精度换资源。

两个关键中间格式常量直接由位增长规则推出：

[hdl/psi_fix_complex_mult.vhd:L53-L54](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_complex_mult.vhd#L53-L54) —— `SumFmt_c := (internal_fmt_g.S, internal_fmt_g.I+1, internal_fmt_g.F)`（加减法 +1 整数位）；`RndFmt_c := (SumFmt_c.S, SumFmt_c.I+1, out_fmt_g.F)`（舍入再 +1 整数位，且小数位先对齐到输出）。这两个常量就是你手算格式推导时该得到的结果。

4 个乘法用 `if in_a_is_cplx_g / in_b_is_cplx_g then ... else (others=>'0')` 门控，省下的乘法器直接填零：

[hdl/psi_fix_complex_mult.vhd:L96-L126](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_complex_mult.vhd#L96-L126) —— 例如 `MultIQ`（`Ai·Bq`）只在 `in_b_is_cplx_g=true` 时才计算（[L109-L114](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_complex_mult.vhd#L109-L114)）；`MultQQ`（`Aq·Bq`）需要两边都是复数才算（[L121-L126](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_complex_mult.vhd#L121-L126)）。所有乘法都量化到 `internal_fmt_g`，统一用 `psi_fix_trunc, psi_fix_wrap`。这里的 `choose(cond, a, b)` 是来自 `psi_common_math_pkg` 的三元运算符（条件为真取 a、否则取 b），配合 `pipeline_g` 在「打一拍的输入」和「原始输入」间切换。

实部减、虚部加：

[hdl/psi_fix_complex_mult.vhd:L128-L132](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_complex_mult.vhd#L128-L132) —— `SumI = psi_fix_sub(MultII, MultQQ, ..., SumFmt_c, trunc, wrap)`（实部 = II − QQ）；`SumQ = psi_fix_add(MultIQ, MultQI, ..., SumFmt_c, trunc, wrap)`（虚部 = IQ + QI）。注释强调 `SumFmt_c` 足够宽、不会真触发截断/回绕。

输出级在两种流水模式下走不同路径：

[hdl/psi_fix_complex_mult.vhd:L134-L143](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_complex_mult.vhd#L134-L143) —— `pipeline_g=false` 时一次性 `resize(SumI, SumFmt_c, out_fmt_g, round_g, sat_g)`；`pipeline_g=true` 时拆成两级：先 `resize(..., RndFmt_c, round, wrap)`（只舍入、多留 1 整数位防舍入进位回绕），再 `resize(..., out_fmt_g, trunc, sat)`（只饱和）。这正是 u2-l2/u4-l1 的 Manual Splitting，两级在 Fmax 和位真上同时获益。valid 输出也据此选 `r.Vld(2)`（非流水，3 级）或 `r.Vld(5)`（流水，6 级）——见 [L151-L156](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_complex_mult.vhd#L151-L156)。

Python 位真模型 `Process()` 与非流水 VHDL 路径一一对应，是黄金参考：

[model/psi_fix_complex_mult.py:L61-L68](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_complex_mult.py#L61-L68) —— 4 个 `psi_fix_mult`（全部 `trunc, wrap`）+ `sumI = sub(II, QQ)` + `sumQ = add(IQ, QI)`，round/sat 只在最后到 `outFmt` 时施加。注意 Python 模型只用一套路径建模，VHDL 的两种 `pipeline_g` 配置都位真等价于它——回归时两种配置都跑（见下面实践）。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：手推两个 `[1,0,15]` 复数相乘的中间格式，对照 VHDL 常量验证。

**操作步骤**：

1. 设 A、B 每个实部/虚部都是 `[1,0,15]`。先用位增长规则算每个实数乘积的**满精度**格式：`[1,0,15]×[1,0,15] = [1, 0+0+1, 15+15] = [1,1,30]`。
2. 对照 [hdl/psi_fix_complex_mult.vhd:L107-L108](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_complex_mult.vhd#L107-L108) 的 `MultII` 调用：它把满精度 `[1,1,30]` 量化到 `internal_fmt_g`。默认 `internal_fmt_g=(1,1,24)`，意味着**截掉了 6 个小数位**（30→24）——这就是 `internal_fmt_g` 作为「精度/资源旋钮」的意义。
3. 推加/减后的格式：两个 `[1,1,24]` 相减/相加，整数位 +1 → `[1,2,24]`。对照 [L53](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_complex_mult.vhd#L53) 的 `SumFmt_c := (internal_fmt_g.S, internal_fmt_g.I+1, internal_fmt_g.F) = (1, 2, 24)`，完全吻合。
4. 打开 `sim/config.tcl` 的 [L289-L294](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L289-L294)，确认 `complex_mult_tb` 用 `tb_run_add_arguments` 把 `pipeline_g=true` 和 `pipeline_g=false` 各跑一轮，两轮共用 preScript 生成的一套输入输出文本。

**需要观察的现象 / 预期结果**：

| 阶段 | 你推导的格式 | VHDL 常量 |
|:--|:--|:--|
| 单个乘积（满精度） | `[1,1,30]` | （函数内部） |
| 乘积量化后（默认 internal） | `[1,1,24]` | `internal_fmt_g` |
| 加减后 (SumFmt_c) | `[1,2,24]` | `(1, internal.I+1, internal.F)` ✓ |
| 最终输出（默认 out） | `[1,0,20]` | `out_fmt_g` |

你的手推结果应当与 VHDL 常量逐项一致；若不一致，说明你对某条位增长规则理解有偏差，回头查 u1-l4。

**待本地验证**：preScript 里 `inAFmt=(1,0,15)`、`intFmt=(1,1,24)`、`outFmt=(1,0,20)`（[preScript.py:L29-L32](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_complex_mult_tb/Scripts/preScript.py#L29-L32)），与本实践的默认 generics 一致，跑回归应两轮全过。

#### 4.2.5 小练习与答案

**练习 1**：把 `in_b_is_cplx_g` 设为 `false`（B 是实数，`Bq=0`），实际会用几个乘法器？写出实部和虚部的化简式。

**答案**：2 个乘法器（`MultII=Ai·Bi`、`MultQI=Aq·Bi`），`MultIQ` 和 `MultQQ` 因含 `Bq=0` 被门控为 0。实部 = `MultII − 0 = Ai·Bi`，虚部 = `0 + MultQI = Aq·Bi`。这就是「复数×实数」省一半乘法器的来源。

**练习 2**：为什么 `pipeline_g=true` 时要先 resize 到 `RndFmt_c`（比 `out_fmt_g` 多 1 个整数位）、再 resize 到 `out_fmt_g`，而不是直接一步到位？

**答案**：第一级只做舍入、刻意多留 1 个整数位（`RndFmt_c.I = SumFmt_c.I+1`），让舍入产生的进位不会回绕（注释「Never wrapps」）；第二级才做饱和到 `out_fmt_g`。两级行先舍入再饱和、中间插一拍寄存器，与一次性 `resize(round, sat)` 位真等价，但拆出了寄存器、提升了 Fmax——即 Manual Splitting。

**练习 3**：`pipeline_g=false` 时输出 valid 取 `r.Vld(2)`，`pipeline_g=true` 取 `r.Vld(5)`。数据通路分别经过了哪几级寄存器？

**答案**：非流水 3 级（乘法级 → 求和级 → 输出级，valid 走到 Vld(2)）；流水 6 级（输入打拍 → 乘法打拍 → 求和 → 舍入级 → 饱和级，valid 走到 Vld(5)）。流水深度翻倍换来更高的可综合时钟频率。

---

### 4.3 复数求模

#### 4.3.1 概念说明

复数 \( z = I + jQ \) 的模（绝对值/幅值）为：

\[
|z| = \sqrt{I^2 + Q^2}
\]

求模有两种主流硬件实现：**CORDIC 矢量模式**（u5-l3 会讲）和**代数法**（平方 + 求和 + 开方）。`psi_fix_complex_abs` 选的是后者——它用乘法器算 \(I^2\)、\(Q^2\)，相加后再用一个**线性近似的平方根**（`psi_fix_lin_approx_sqrt18b`，见 u8-l1/u8-l2）来开方。

为什么不用 CORDIC？文档说得很直白：这种实现「比 CORDIC 少用很多 LUT，但要用乘法器和一点 BRAM」。代价是平方根线性近似被限制在 18 位，结果有相对误差。这是一个典型的**资源/精度取舍**。

#### 4.3.2 核心流程

整个数据流分两大段：先把输入归一化到平方根近似的有效范围，开方后再把归一化还原。

```text
(I, Q) 格式 in_fmt_g
  │
  ├── Stage 0: 输入打拍
  ├── Stage 1: 隐式归一化 —— 把 [s,i,f] 重新解读为 [s,0,i+f]（总位宽不变，数值被看作 [-1,+1]）
  │            平方: ISqr = I·I, QSqr = Q·Q  (SqrFmt: 无符号, 小数位翻倍)
  ├── Stage 2: Sum = ISqr + QSqr            (AddFmt: 无符号, 整数位 +1)
  ├── Stage 3: Limit —— 把 Sum 饱和到 [0,1]  (LimFmt: 无符号, 整数位 0)
  │
  ├── 移位归一化（开方前）—— 逐级检测前导位，把数移进 sqrt 近似有效的 [0.25, 1.0] 区间，
  │            并用一个计数器 SftCnt 记下一共移了多少（这些位要开方后还回去）
  ├── psi_fix_lin_approx_sqrt18b —— 在 [0.25,1.0] 内做 18 位线性近似开方
  │            （SftCnt 经一个 FIFO 延时，与 sqrt 流水延迟对齐后送回）
  ├── 移位还原（开方后）—— 用 SftCnt 把开方结果移回原量级
  │
  └── 输出 resize（shift_left 还原整数位 + round/sat）→ out_fmt_g
```

两个要点：①「归一化」是**隐式**的——不移动任何位，只是把同一串位重新按 `[s,0,i+f]` 解释，等于在数学上把数除以 \(2^{i}\)（让量纲变成 \([-1,+1]\)），输出时再用 `shift_left` 乘回来，对外完全透明。②平方根近似只在 `[0.25, 1.0]` 有效，所以必须先把数移进这个区间、记下移了多少、开方后再移回——平方根会把「移 2 位」变成「移 1 位」，所以还原时的移位量是归一化时的一半。

#### 4.3.3 源码精读

一组常量把上述每个阶段的格式钉死，全部由位增长规则严格推出：

[hdl/psi_fix_complex_abs.vhd:L48-L59](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_complex_abs.vhd#L48-L59) —— 几个关键常量：
- `InFmtNorm_c := (in_fmt_g.S, 0, in_fmt_g.I+in_fmt_g.F)`：归一化格式，整数位清零、全部位当小数，总位宽不变（隐式归一化）。
- `SqrFmt_c := (0, InFmtNorm_c.I+1, InFmtNorm_c.F*2)`：平方结果**无符号**（`s=0`，因为平方非负），小数位翻倍。
- `AddFmt_c := (0, SqrFmt_c.I+1, SqrFmt_c.F)`：两平方相加，无符号、整数位 +1。
- `LimFmt_c := (0, 0, AddFmt_c.F)`：饱和到 `[0,1]`，整数位清零。

平方与求和就是三个 `psi_fix_mult/add`：

[hdl/psi_fix_complex_abs.vhd:L121-L126](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_complex_abs.vhd#L121-L126) —— `ISqr_1 = I·I`、`QSqr_1 = Q·Q`（量化到 `SqrFmt_c`），`Sum_2 = ISqr + QSqr`（量化到 `AddFmt_c`）。注意输入用的是归一化格式 `InFmtNorm_c`。

[hdl/psi_fix_complex_abs.vhd:L130](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_complex_abs.vhd#L130) —— `Lim_3 := resize(Sum_2, AddFmt_c, LimFmt_c, trunc, sat)`：把平方和饱和到 `[0,1]`，这是该组件「模大于 1 会被限幅」这一近似的来源（文档说的相对误差主要来自这里 + 18 位 sqrt 近似）。

开方前用一组移位级把数搬进 `[0.25, 1.0]`，并用 `SftCnt` 记录移动量：

[hdl/psi_fix_complex_abs.vhd:L132-L152](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_complex_abs.vhd#L132-L152) —— `for stg in 0 to SftStgBeforeApprox_c-1 loop` 逐级二分检测前导位：若高位为 0 就左移补零、并在 `SftCnt` 对应位记 1；否则保持。最终 `InSft` 落在 sqrt 有效区间，`SftCnt` 编码了总移位量。

平方根本体是个被例化的子组件（来自 u8 的代码生成族）：

[hdl/psi_fix_complex_abs.vhd:L208-L216](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_complex_abs.vhd#L208-L216) —— `inst_sqrt : entity work.psi_fix_lin_approx_sqrt18b`，输入是归一化、移位后的 `SqrtIn_s`（格式 `[0,0,20]`），输出 `SqrtData_s`（`[0,0,17]`）。

一个精巧之处：开方组件有自己几拍的流水延迟，而归一化阶段算出的 `SftCnt` 必须在**同一拍**到达输出端才能正确还原。于是用一个 FIFO 把 `SftCnt`（连同「输入是否为零」标志）延时到 sqrt 出结果：

[hdl/psi_fix_complex_abs.vhd:L219-L239](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_complex_abs.vhd#L219-L239) —— `inst_sft_del : entity work.psi_common_sync_fifo`，把 `IsZeroIn & SftCnt` 跟着数据流排进队列、用 `SqrtVld_s` 作读就绪，到输出端 `SftCntOut_s`/`IsZeroOut_s` 正好与开方结果对齐。注释说明这是为了「即使将来 sqrt 近似的延迟变了也能正常工作」。

最后开方结果经移位还原 + `shift_left` 把归一化除掉的整数位乘回来，resize 到 `out_fmt_g`：

[hdl/psi_fix_complex_abs.vhd:L176](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_complex_abs.vhd#L176) —— `OutRes := shift_left(OutSft, OutFmtNorm_c, in_fmt_g.I, in_fmt_g.I, out_fmt_g, round_g, sat_g)`，左移量正好是输入的整数位 `in_fmt_g.I`，把隐式归一化彻底还原，对外可见的量纲与输入一致。

#### 4.3.4 代码实践

**实践目标**：理解 complex_abs 的「隐式归一化」对调用者完全透明，并定位它依赖的子组件。

**操作步骤**：

1. 打开 `hdl/psi_fix_complex_abs.vhd` 的 [L49](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_complex_abs.vhd#L49)，确认 `InFmtNorm_c` 与 `in_fmt_g` 总位宽相同（`0 + (I+F) = I+F`），只是把整数位挪到了小数位。
2. 读 [doc/files/psi_fix_complex_abs.md:L47-L53](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/psi_fix_complex_abs.md#L47-L53) 中关于「归一化到 ±1、输出时还原、对外不可见」的说明。
3. 在仓库里确认被例化的子组件存在：它是 `hdl/psi_fix_lin_approx_sqrt18b.vhd`（由 u8-l2 的代码生成器生成）。开方近似只在 `[0.25, 1.0]` 内有效——这正是 Stage 3 之后那组移位级存在的理由。

**需要观察的现象**：从调用者角度看，complex_abs 就是一个 `in_fmt_g → out_fmt_g` 的求模器，内部归一化/反归一化完全不可见；唯一的外部可见副作用是「模 > 1 会被限幅」带来的相对误差。

**预期结果**：能向同伴讲清「为什么输入 [1,8,15] 时，内部先当 [1,0,23] 处理、开方后再左移 8 位还回去」。

**待本地验证**：complex_abs 有自己的 Python 位真模型 `model/psi_fix_complex_abs.py` 和 preScript（`config.tcl` [L382-L384](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L382-L384)），跑回归可确认其精度在文档承诺的相对误差范围内。

#### 4.3.5 小练习与答案

**练习 1**：`SqrFmt_c` 为什么符号位 `s=0`（无符号）？

**答案**：任何实数的平方都非负，所以 \(I^2\)、\(Q^2\) 恒 ≥ 0，无需符号位。去掉符号位还能省 1 bit。

**练习 2**：为什么归一化时移了 N 位、开方后还原时只移约 N/2 位？

**答案**：平方根满足 \(\sqrt{x \cdot 2^{N}} = \sqrt{x} \cdot 2^{N/2}\)。归一化把输入放大/缩小 \(2^{N}\)，开方后量级只变化 \(2^{N/2}\)，所以还原移位量是归一化移位量的一半。代码里 `SftStgAfterApprox_c = SftStgBeforeApprox_c/2`（[L57-L58](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_complex_abs.vhd#L57-L58)）正反映这一点。

**练习 3**：`SftCnt` 为什么要过一个 FIFO 再送到输出端，而不是直接拉一根线？

**答案**：开方子组件 `psi_fix_lin_approx_sqrt18b` 有自己的多拍流水延迟。某个样本的 `SftCnt` 必须和它对应的开方结果在同一拍到达，才能正确还原量级。FIFO 把 `SftCnt` 沿数据流排队、以 `SqrtVld_s` 为读就绪，恰好补偿这段延迟；用 FIFO 而非定长移位寄存器，是为了在 sqrt 延迟将来变化时仍能自适应。

---

## 5. 综合实践

把本讲三个组件串起来，搭一个「复数幅值响应」迷你链路（纯源码阅读 + 格式推导，无需综合）：

**任务**：假设要计算复数信号 \( A \) 与固定复数系数 \( B \) 相乘后的幅值，链路是 `complex_mult → complex_abs`。给定 A、B 每个实虚部都是 `[1,0,15]`。

1. **选资源优化**：若 B 是固定已知系数、且你愿意在别处把它当成实数处理，你会把 `complex_mult` 的哪个 `is_cplx` generic 设为 false？省几个乘法器？
2. **推导中间格式**：按 4.2.4 的方法，推出 `complex_mult` 在默认 `internal_fmt_g=(1,1,24)`、`out_fmt_g=(1,0,20)` 下实部/虚部的输出格式。
3. **接 complex_abs**：把上一步的 `out_fmt_g=(1,0,20)` 作为 `complex_abs` 的 `in_fmt_g`，写出 complex_abs 内部 `InFmtNorm_c`、`SqrFmt_c`、`AddFmt_c` 的具体值。
4. **验证对齐**：打开三个 VHDL 文件，确认你推导的常量与源码里的 `constant :=` 完全一致。

**参考答案要点**：

1. 若 B 当实数，设 `in_b_is_cplx_g=false`，省 2 个乘法器（剩 `MultII`、`MultQI`）。但题目说 B 是复数系数，若必须保留虚部则四个乘法器都不能省——这正说明 `is_cplx` 开关只在「该路信号确实是实数」时才有意义。
2. 乘积满精度 `[1,1,30]` → 截到 internal `[1,1,24]` → 加减后 `SumFmt_c=[1,2,24]` → 输出 `[1,0,20]`。
3. `InFmtNorm_c=(1,0,20)`、`SqrFmt_c=(0,1,40)`、`AddFmt_c=(0,2,40)`。
4. 与 [complex_mult.vhd:L53](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_complex_mult.vhd#L53) 和 [complex_abs.vhd:L49-L52](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_complex_abs.vhd#L49-L52) 逐一核对。

## 6. 本讲小结

- 复数在 psi_fix 里就是一对定点 `(I,Q)=(实部,虚部)`，端口分 `inp`/`qua` 两路并行。
- **复数加减**最简单：实虚部独立、互不耦合，是两个并排的标量加减器；`add_sub_g` 选 ADD/SUB，`SumFmt_c` 用 `max` 对齐两路不同格式并 +1 整数位。
- **复数乘法** = 4 个乘法 + 实部减 (`II−QQ`) + 虚部加 (`IQ+QI`)；`in_a_is_cplx_g`/`in_b_is_cplx_g` 在「复数×实数」时把恒零乘积门控掉，省下乘法器；`internal_fmt_g` 是精度/资源旋钮；`pipeline_g` 走 Manual Splitting 提 Fmax。
- 所有中间格式都由位增长规则严格推出（乘法 `[1,a,b]×[1,c,d]=[1,a+c+1,b+d]`、加减 +1 整数位、舍入再 +1），VHDL 常量 `SumFmt_c`/`RndFmt_c` 就是你手算的答案。
- **复数求模**用代数法（平方+求和+线性近似开方）而非 CORDIC，省 LUT 但用乘法器+BRAM、有 18 位限制带来的相对误差；内部做隐式归一化到 `[-1,+1]`、sqrt 仅在 `[0.25,1.0]` 有效故需移位进区间并记 `SftCnt`、开方后用 FIFO 对齐延迟再移位还原。
- 三个组件都配 Python 位真模型 + preScript 协同仿真，`config.tcl` 里 complex_mult 还对两种 `pipeline_g` 各跑一轮，证明两种流水深度位真等价。

## 7. 下一步学习建议

- 下一讲 **u5-l2 CORDIC 旋转模式** 会讲另一种求坐标变换的思路（极坐标→直角坐标），可与本讲的 `complex_mult`（直接复数乘法做旋转）和 `complex_abs`（代数法求模）形成对比：同样是旋转/求模，CORDIC 用迭代移位加法、不用乘法器。
- 想深入 `complex_abs` 的开方内核，先读 **u8-l1 线性近似 lin_approx_calc 原理** 和 **u8-l2 Python 代码生成器**，理解 `psi_fix_lin_approx_sqrt18b` 是怎么由数学函数自动生成的。
- 想验证自己对格式推导的掌握，建议回到 **u2-l2 定点运算函数** 重做一遍位增长练习，再回来手推任意复数组件的中间格式。
