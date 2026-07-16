# 基本定点运算：resize / add / mult / round / saturate 等

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 `olo_fix_round`、`olo_fix_saturate`、`olo_fix_resize` 三者的关系，以及它们如何把「一次定点运算」切成 **operation → round → saturate** 三段。
- 看懂 `olo_fix_add` / `olo_fix_sub` / `olo_fix_mult` 这些二元运算实体是如何「算完后再 resize」的，并能算出它们的默认流水线延迟。
- 理解 `olo_fix_neg` / `olo_fix_abs` / `olo_fix_compare` 这组一元与比较运算的接口与延迟。
- 掌握 `olo_fix_from_real` / `olo_fix_to_real` 这对「real 桥接器」为什么不带流水线、为什么只在仿真/综合期存在。
- 把上述实体串成一条定点数据通路，并用 en_cl_fix 的位真函数验证结果一致。

本讲承接 [u8-l2](./u8-l2-olo-fix-pkg.md) 建立的「字符串泛型 + `olo_fix_pkg`」认知，所有实体的泛型/端口宽度都由 `fixFmtWidthFromString` 在编译期推导，本讲不再重复这一机制，而是聚焦**运算本身如何分解、如何插寄存器、如何与 en_cl_fix 函数位真等价**。

## 2. 前置知识

在进入源码前，先用通俗语言把几个本讲反复用到的概念讲透。

### 2.1 定点格式 (S, I, F) 回顾

Open Logic 的定点数用三元组描述（来自 en_cl_fix）：

- `S`：符号位个数（0 表示无符号，1 表示有符号）。
- `I`：整数位个数（不含符号位）。
- `F`：小数位个数。

位宽 \(W = S + I + F\)，分辨率（最小可分辨步长）\(\Delta = 2^{-F}\)。例如 `(1,1,8)` 是一个 10 位有符号数：1 个符号位、1 个整数位、8 个小数位，分辨率 \(2^{-8} \approx 0.0039\)，能表示 \([-2,\,2)\) 范围的值。

### 2.2 为什么定点运算要「三段化」

一次「正确」的定点运算分三步：

1. **operation（运算）**：按数学定义把操作数算成**全精度**结果。这一步绝不丢精度——例如两个 `(1,1,8)` 相乘，乘积天然需要 `(2,2,16)` 才装得下。
2. **round（舍入）**：全精度结果的小数位往往多于目标格式，需要按某种规则（截断、就近舍入……）减少小数位。
3. **saturate（饱和）**：全精度结果的整数位/符号位也可能多于目标格式，需要把超出范围的值「夹」到目标能表示的最大/最小值（或仅报警）。

把这三步拆开，是为了**在段与段之间插入流水线寄存器**，从而提升主频。这是本讲所有实体的核心设计动机。

### 2.3 LogicPresent：这一段到底有没有活干

`olo_fix_round` 和 `olo_fix_saturate` 在源码里都算了一个布尔量 `LogicPresent_c`，含义是「这段是否真的有舍入/饱和逻辑」：

- 舍入逻辑存在 ⟺ 输入的小数位 **多于** 结果的小数位。
- 饱和逻辑存在 ⟺ 输入的整数位或符号位 **多于** 结果的对应位，**且** 饱和模式不是 `None_s`/`Warn_s`（即选择了真正会限幅的 `Sat_s`/`SatWarn_s`）。

这个判断决定了 `AUTO` 寄存模式下到底插不插寄存器（见 4.1.3）。

## 3. 本讲源码地图

本讲涉及的全部实体都在 `src/fix/vhdl/` 下，它们是 Open Logic 定点区域的「原子积木」：

| 文件 | 作用 | 是否含流水线寄存器 |
| :--- | :--- | :--- |
| `olo_fix_round.vhd` | 单独做舍入（减小数位） | 是（可选，1 级） |
| `olo_fix_saturate.vhd` | 单独做饱和（减整数/符号位） | 是（可选，1 级） |
| `olo_fix_resize.vhd` | 舍入 + 饱和的组合（格式转换） | 是（可选，最多 2 级） |
| `olo_fix_add.vhd` / `olo_fix_sub.vhd` / `olo_fix_mult.vhd` | 二元运算，算完后接一次 resize | 是 |
| `olo_fix_neg.vhd` / `olo_fix_abs.vhd` | 一元运算，算完后接一次 resize | 是 |
| `olo_fix_compare.vhd` | 比较 A 与 B，输出 1 位布尔 | 是（可选，OpRegs） |
| `olo_fix_from_real.vhd` / `olo_fix_to_real.vhd` | `real` 与定点互转 | **否**（见 4.3） |
| `olo_fix_private_optional_reg.vhd` | 上述实体共用的私有可选寄存器 | —— |
| `olo_fix_pkg.vhd` | 字符串常量、`fixImplementReg`、宽度推导 | —— |

> 提示：`olo_fix_private_optional_reg` 的文件名以 `olo_fix_private_` 开头，属于「私有实体」（见 [u1-l5](./u1-l5-conventions-and-anatomy.md) 讲过的私有实体约定），不在文档里详细公开，但它的实现很简短，本讲会读一遍。

## 4. 核心概念与源码讲解

### 4.1 模块一：round / saturate / resize —— 三段式转换

#### 4.1.1 概念说明

这三个实体是「格式转换」家族，自身不做加减乘，只负责把一个格式的定点数**无损或可控有损地**搬到另一个格式：

- `olo_fix_round`：只动小数位。输入小数位 > 结果小数位时，按 `Round_g` 舍入。
- `olo_fix_saturate`：只动整数位/符号位。输入范围超出结果时，按 `Saturate_g` 限幅。
- `olo_fix_resize`：**两者都做**，先 round 再 saturate，是日常用得最多的「格式转换器」。

它们都只是把对应的 en_cl_fix 函数（`cl_fix_round` / `cl_fix_saturate` / `cl_fix_resize`）包装成实体，目的是能被 Verilog 实例化（见 [u8-l1](./u8-l1-fix-principles-enclfix.md) 讲过的「组件 vs 函数」动机）。

#### 4.1.2 核心流程

`olo_fix_resize` 的核心思想是：**把一次格式转换拆成 round → saturate 两段**，中间用一个「舍入后、饱和前的中间格式」`RoundFmt_c` 衔接。流程如下：

```text
In_A (AFmt) ──► [round: AFmt → RoundFmt] ──► [saturate: RoundFmt → ResultFmt] ──► Out_Result
```

其中 `RoundFmt` 的整数位/符号位仍保留输入的规模（饱和还没发生），但小数位已经收敛到结果的小数位。这样 round 段只砍小数位、saturate 段只砍整数位，职责清晰、各自可独立插寄存器。

`olo_fix_round` 与 `olo_fix_saturate` 单独使用时则是上面流程的「半成品」——只跑其中一段。

#### 4.1.3 源码精读

**(1) `olo_fix_round` 的接口与舍入逻辑判定**

泛型里 `Round_g` 默认是 `NonSymPos_s`（就近舍入），还有一个 `FmtCheck_g`（默认 `true`）控制是否在仿真期做格式合法性检查：

[src/fix/vhdl/olo_fix_round.vhd:36-56](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_round.vhd#L36-L56) —— 实体声明，`In_A` 与 `Out_Result` 的位宽都由 `fixFmtWidthFromString(...)` 在端口声明处编译期推导。

关键的三行常量计算：

[src/fix/vhdl/olo_fix_round.vhd:66-68](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_round.vhd#L66-L68)

```vhdl
constant LogicPresent_c : boolean := AFmt_c.F > ResultFmt_c.F;
constant ImplementReg_c : boolean := fixImplementReg(LogicPresent_c, RoundReg_g);
constant OpRegStages_c  : integer := choose(ImplementReg_c, 1, 0);
```

- 第 66 行：**只有当输入小数位严格多于结果小数位时，舍入才真的有逻辑**（否则只是位宽对齐的纯连线）。
- 第 67 行：调用 `olo_fix_pkg` 的 `fixImplementReg`，把 `RoundReg_g`（`YES`/`NO`/`AUTO`）和「有没有逻辑」结合起来，决定要不要插寄存器。
- 第 68 行：要插就 1 级，不插就 0 级（`choose` 来自 `olo_base_pkg_math`）。

真正的舍入运算只有一行（组合逻辑）：

[src/fix/vhdl/olo_fix_round.vhd:76](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_round.vhd#L76) —— `ResultComb <= cl_fix_round(In_A, AFmt_c, ResultFmt_c, Round_c, FmtCheck_g);`

随后把结果送进私有可选寄存器：

[src/fix/vhdl/olo_fix_round.vhd:79-91](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_round.vhd#L79-L91) —— 实例化 `olo_fix_private_optional_reg`，级数由 `OpRegStages_c` 决定。

**(2) `olo_fix_saturate` 的饱和逻辑判定**

结构几乎和 `round` 一模一样，区别在于 `LogicPresent_c` 的判定条件——它看的是**整数位和符号位**，并且要求饱和模式是真正会限幅的：

[src/fix/vhdl/olo_fix_saturate.vhd:65-68](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_saturate.vhd#L65-L68)

```vhdl
constant LogicPresent_c : boolean := (AFmt_c.I > ResultFmt_c.I or
                                     AFmt_c.S > ResultFmt_c.S) and
                                     (Saturate_c = Sat_s
                                     or Saturate_c = SatWarn_s);
```

注意默认值差异：`saturate` 的 `Saturate_g` 默认是 `Sat_c`（真限幅，[src/fix/vhdl/olo_fix_saturate.vhd:40](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_saturate.vhd#L40)），而 `resize` 里用的是 `Warn_c`（仅报警、不限幅，见下）。运算本体同样是组合一行：[src/fix/vhdl/olo_fix_saturate.vhd:78](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_saturate.vhd#L78)。

**(3) `olo_fix_resize` 的两段串联**

`resize` 自己不写运算逻辑，而是实例化一个 `round` 再实例化一个 `saturate` 串起来。先把泛型（字符串）经 `cl_fix_format_from_string` 转成 typed 常量，再算出中间格式 `RoundFmt_c`：

[src/fix/vhdl/olo_fix_resize.vhd:62-67](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_resize.vhd#L62-L67)

```vhdl
constant AFmt_c      : FixFormat_t := cl_fix_format_from_string(AFmt_g);
constant ResultFmt_c : FixFormat_t := cl_fix_format_from_string(ResultFmt_g);
constant Round_c     : FixRound_t  := cl_fix_round_from_string(Round_g);
constant RoundFmt_c  : FixFormat_t := cl_fix_round_fmt(AFmt_c, ResultFmt_c.F, Round_c);
```

- 第 67 行的 `cl_fix_round_fmt` 是 en_cl_fix 提供的辅助函数，算出「对 `AFmt` 做 `Round_c` 舍入、目标小数位为 `ResultFmt.F`」之后得到的中间格式——它的整数位/符号位仍是 `AFmt` 的规模，小数位已是结果的小数位。

然后两段串联，注意第二段的输入格式用的是 `to_string(RoundFmt_c)`：

[src/fix/vhdl/olo_fix_resize.vhd:82-113](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_resize.vhd#L82-L113) —— 先 `i_round`（`AFmt → RoundFmt`），再 `i_saturate`（`RoundFmt → ResultFmt`），两段的 `RoundReg_g`/`SatReg_g` 直接透传用户配置。

`resize` 的默认 `Round_g=Trunc_c`、`Saturate_g=Warn_c`（[src/fix/vhdl/olo_fix_resize.vhd:40-41](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_resize.vhd#L40-L41)），即「默认只截断、溢出仅报警」——这是一个偏「保守不丢精度信息」的默认。

**(4) 私有可选寄存器 `olo_fix_private_optional_reg`**

这是上面所有实体共用的「可插拔寄存器」。当 `Stages_g=0` 时它退化成纯组合连线；`Stages_g>0` 时是一组移位寄存器：

[src/fix/vhdl/olo_fix_private_optional_reg.vhd:68-98](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_private_optional_reg.vhd#L68-L98)

- `g_noreg`（L68-71）：`Stages_g=0` 时直接 `Out <= In`，连时钟都不需要。
- `g_reg`（L74-98）：一个进程把 `Data`/`Valid` 逐级打拍。
- **复位只清 `Valid`、不清 `Data`**（L90-92）。这与 Open Logic 全库「只复位状态、降低复位扇出」的约定一致（见 [u1-l5](./u1-l5-conventions-and-anatomy.md)）。

> 为什么不直接复用 [u2-l2](./u2-l2-pipeline-stage-handshake.md) 讲过的 `olo_base_pl_stage`？源码注释给出了解释：`olo_base_pl_stage` 内含阻止 retiming（重定时）的综合属性，而定点运算希望综合器自由地把寄存器「推进/拉出」乘法器/加法器以优化时序，所以另写了一个**不带这些属性**的轻量寄存器（[src/fix/vhdl/olo_fix_private_optional_reg.vhd:10-17](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_private_optional_reg.vhd#L10-L17)）。这是个很值得记住的工程取舍。

**(5) `fixImplementReg` 的三态判定**

[src/fix/vhdl/olo_fix_pkg.vhd:130-156](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_pkg.vhd#L130-L156) —— 用 `compareNoCase`（大小写不敏感）比较 `regMode`：

- `"YES"` → 永远插（即便没有舍入/饱和逻辑，也插一个「空转」寄存器，换来**固定延迟**与最快主频，是默认值）。
- `"NO"` → 永不插（延迟最低、但组合路径长）。
- `"AUTO"` → 仅当 `LogicPresent` 为真才插（主频高且无多余延迟，但**延迟随格式变化**）。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `olo_fix_resize` 默认配置（`RoundReg=SatReg=YES`）的延迟恒为 2 拍，以及 `Round_g`/`Saturate_g` 不同取值下的行为差异。

**操作步骤（源码阅读 + 手算）**：

1. 打开 [src/fix/vhdl/olo_fix_resize.vhd:82-113](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_resize.vhd#L82-L113)，确认它由 `i_round` + `i_saturate` 两级构成。
2. 设 `AFmt="(1,3,12)"`、`ResultFmt="(1,1,8)"`：小数位 12→8（有舍入逻辑）、整数位 3→1（有饱和逻辑）。
3. 手算一个超界样本：输入值 `7.5`（在 `(1,3,12)` 范围内合法）。截断到 8 位小数后仍是 `7.5`，但 `(1,1,8)` 最大只能表示约 `+1.996`，故：
   - `Saturate_g="Sat_s"` → 输出被夹到 `+1.99609375`（即 `01.11111111`）。
   - `Saturate_g="Warn_s"` → 输出仍按数学截断（高位直接丢弃），值会变成 `7.5 - 8 = -0.5`（环绕），同时仿真期拉响 warning。

**需要观察的现象**：默认 `Saturate_g=Warn_c` 时，溢出不会自动限幅而是「绕回」——这是初学者最容易踩的坑。

**预期结果**：在 `Warn_s` 下高位整数位被直接舍掉，结果语义错误；改用 `Sat_s` 才得到正确的限幅值。**仿真波形待本地验证**（可参照 4.2.4 的综合实践一起跑）。

#### 4.1.5 小练习与答案

**练习 1**：把 `(0,3,4)` 转成 `(1,5,6)`（无符号变有符号、且整数位和小数位都变多），`olo_fix_resize` 在默认 `RoundReg/SatReg=YES` 下会插几个寄存器？延迟是多少？

> **答案**：由于 `LogicPresent` 为假（小数位 4<6、整数位 3<5，既无需舍入也无需饱和），但 `YES` 模式强制每段都插一个寄存器，因此仍插 2 级、延迟 2 拍。这正是 `YES` 模式「用多余延迟换固定延迟」的代价。改用 `AUTO` 则 0 级、0 延迟。

**练习 2**：`olo_fix_round` 的 `Round_g` 默认是 `NonSymPos_s`，而 `olo_fix_resize` 的 `Round_g` 默认是 `Trunc_s`，为什么不同？

> **答案**：`olo_fix_round` 是「专门做舍入」的实体，默认就给一个常用的就近舍入；`olo_fix_resize` 是通用格式转换器，默认 `Trunc_s`（截断）+ `Warn_s`（仅报警）更「不主动改变数值」，把是否舍入/限幅的决策权留给用户显式指定。

### 4.2 模块二：add / sub / mult —— 二元运算

#### 4.2.1 概念说明

`olo_fix_add`、`olo_fix_sub`、`olo_fix_mult` 三个实体结构完全同构，都是「**先用 en_cl_fix 函数算出全精度结果，再接一个 `olo_fix_resize` 把它收敛到用户要的 `ResultFmt`**」。它们解决的问题是：加法/减法/乘法的全精度结果格式是固定的（由操作数格式决定），而用户想要的输出格式千差万别，因此必须有一个统一的「算完再 resize」外壳。

#### 4.2.2 核心流程

以乘法为例，全流程是四段（前三段在 mult 内部，最后一段是 mult 实例化 resize 时由 resize 内部完成）：

```text
In_A,In_B ──► [cl_fix_mult: 全精度 MultFmt] ──► OpRegs ──► [resize(round+sat)] ──► Out_Result
```

各运算的「全精度自然格式」由 en_cl_fix 的辅助函数给出（这些函数在实体内被调用，见源码）：

| 运算 | 自然格式辅助函数 | 规模直觉 |
| :--- | :--- | :--- |
| 加法 `cl_fix_add_fmt(A,B)` | `I=max+1`（进位）、`F=max`、`S=max` | 比操作数多 1 个整数位 |
| 减法 `cl_fix_sub_fmt(A,B)` | `I=max+1`、`F=max`、`S=1`（恒有符号） | 结果恒为有符号 |
| 乘法 `cl_fix_mult_fmt(A,B)` | `S=A.S+B.S`、`I=A.I+B.I`、`F=A.F+B.F` | 位宽大致翻倍 |

> 这三条是 en_cl_fix 的对外约定（详见 `3rdParty/en_cl_fix` 文档）。例如两个 `(1,1,8)` 相乘，自然格式为 `(2,2,16)`，共 20 位——这就是为什么乘法器后面几乎总要接一个 resize 把位宽压回去。

#### 4.2.3 源码精读

三个实体的源码高度相似，以 `olo_fix_mult` 为代表。

**(1) 泛型与默认延迟**

[src/fix/vhdl/olo_fix_mult.vhd:36-48](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_mult.vhd#L36-L48) —— 关键默认值：`OpRegs_g=1`（运算后 1 级寄存器）、`RoundReg_g=SatReg_g="YES"`。三者叠加 ⇒ 默认延迟 = 1（OpRegs）+ 1（round）+ 1（sat）= **3 拍**，与官方文档 [doc/fix/olo_fix_mult.md:20-21](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/fix/olo_fix_mult.md#L20-L21) 一致。

**(2) 全精度运算 + 运算寄存器**

[src/fix/vhdl/olo_fix_mult.vhd:70-95](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_mult.vhd#L70-L95)

```vhdl
constant MultFmt_c : FixFormat_t := cl_fix_mult_fmt(AFmt_c, BFmt_c);
...
Mult_DataComb <= cl_fix_mult(In_A, AFmt_c, In_B, BFmt_c, MultFmt_c, Trunc_s, Warn_s);
```

- 第 70 行：用 `cl_fix_mult_fmt` 算出能装下全精度乘积的格式。
- 第 80 行：调用 `cl_fix_mult` 做真正的乘法。注意这里硬编码 `Trunc_s, Warn_s`——**全精度运算段绝不丢精度**（截断也无影响，因为自然格式恰好够装），舍入/饱和完全交给后面的 resize。
- 第 83-95 行：运算结果送入 `olo_fix_private_optional_reg`，级数 = `OpRegs_g`（默认 1）。这一级寄存在乘法器之后，是切分乘法器组合路径、提主频的关键。

**(3) 接 resize 收敛到目标格式**

[src/fix/vhdl/olo_fix_mult.vhd:98-114](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_mult.vhd#L98-L114) —— 实例化 `olo_fix_resize`，输入格式用 `to_string(MultFmt_c)`（把全精度格式转回字符串传给 resize 的字符串泛型），输出格式、舍入、饱和模式全部透传用户配置。

`olo_fix_add`（[src/fix/vhdl/olo_fix_add.vhd:70-114](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_add.vhd#L70-L114)）与 `olo_fix_sub`（[src/fix/vhdl/olo_fix_sub.vhd:70-114](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_sub.vhd#L70-L114)）结构完全相同，只是把 `cl_fix_mult_fmt`/`cl_fix_mult` 换成 `cl_fix_add_fmt`/`cl_fix_add`、`cl_fix_sub_fmt`/`cl_fix_sub`。

**默认延迟一览**（`OpRegs_g=1`、`Round/SatReg=YES`）：

| 实体 | OpRegs | round | sat | 总延迟 |
| :--- | :-: | :-: | :-: | :-: |
| `olo_fix_add` / `olo_fix_sub` / `olo_fix_mult` | 1 | 1 | 1 | 3 拍 |
| 同上，但 `Round/SatReg="NO"` | 1 | 0 | 0 | 1 拍 |
| 同上，且 `OpRegs_g=0`、`Round/SatReg="NO"` | 0 | 0 | 0 | 0 拍（纯组合） |

#### 4.2.4 代码实践

**实践目标**：用 `olo_fix_mult` 与 `olo_fix_resize` 串联实现「a×b 再截断到目标格式」，并用 en_cl_fix 函数手算验证位真一致。这正是本讲规格要求的实践任务。

**操作步骤**：

1. 配置：`AFmt=BFmt="(1,1,8)"`（10 位有符号），`ResultFmt="(0,1,8)"`（9 位无符号），`Round_g="Trunc_s"`，`Saturate_g="Sat_s"`。
   - 等价地，直接用 `olo_fix_mult` 一个实体即可（它内部已含 resize）；想验证「显式串联」也可在 mult 输出后再接一个 `olo_fix_resize`，行为位真等价。
2. 取样本 `A = 1.5`、`B = 0.25`：
   - `A` 的 `(1,1,8)` 编码：`01.10000000` → 10 位向量 `0110000000`。
   - `B` 的 `(1,1,8)` 编码：`00.01000000` → `0001000000`。
3. 全精度乘积 `A×B = 0.375`，自然格式 `(2,2,16)`：以 \(2^{-16}\) 为单位，\(0.375 = 24576 = \texttt{0x6000}\)。
4. 截断到 `(0,1,8)`：丢掉低 8 位小数，\(24576 \gg 8 = 96 = \texttt{0x60}\)。在 `(0,1,8)`（分辨率 \(2^{-8}\)）下，\(96 \times 2^{-8} = 0.375\)。最终 9 位输出 = `0_01100000`。

**手算对照**（位真）：

| 阶段 | 格式 | 数值（十进制，按各自分辨率） | 说明 |
| :--- | :--- | :--- | :--- |
| A | (1,1,8) | \(384 \times 2^{-8} = 1.5\) | `0110000000` |
| B | (1,1,8) | \(64 \times 2^{-8} = 0.25\) | `0001000000` |
| 乘积（自然） | (2,2,16) | \(24576 \times 2^{-16} = 0.375\) | `0x6000` |
| 截断后 | (0,1,8) | \(96 \times 2^{-8} = 0.375\) | `001100000` |

**需要观察的现象**：mult 输出比输入晚 3 拍（默认配置）；`Out_Valid` 在第 3 个时钟上升沿后跟随 `In_Valid`。

**预期结果**：DUT 输出的 9 位向量应为 `001100000`（= 96），与上表完全一致。**完整仿真波形待本地验证**——仓库已提供现成测试台 [test/fix/olo_fix_mult/olo_fix_mult_tb.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/fix/olo_fix_mult/olo_fix_mult_tb.vhd)，它正是用协仿真文件（`*.fix`）做这种位真比对的样板，运行方式见 [u1-l4](./u1-l4-run-first-simulation.md)：

```bash
cd sim
python run.py --ghdl    # 默认全跑；要单跑 mult 可用 -p 过滤 "*olo_fix_mult*"
```

> 该 TB 默认泛型正是 `AFmt=BFmt="(1,1,8)"`、`ResultFmt="(0,1,8)"`、`Round="NonSymPos_s"`、`Saturate="Sat_s"`（[test/fix/olo_fix_mult/olo_fix_mult_tb.vhd:34-42](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/fix/olo_fix_mult/olo_fix_mult_tb.vhd#L34-L42)），与上面的手算例子格式相同，可直接对照。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `olo_fix_mult` 在全精度乘法段硬编码 `Trunc_s, Warn_s`，而不是用用户传进来的 `Round_g`/`Saturate_g`？

> **答案**：因为全精度自然格式 `MultFmt` 恰好能无损装下乘积，这一段既不需要舍入也不需要饱和，传什么模式结果都一样。用户的 `Round_g`/`Saturate_g` 是为「自然格式 → ResultFmt」这一步准备的，所以透传给后面的 `olo_fix_resize`。把决策点集中在 resize，逻辑更清晰。

**练习 2**：把 `olo_fix_mult` 的 `OpRegs_g` 设为 0、`RoundReg_g`/`SatReg_g` 设为 `"NO"`，这个实体还能用吗？时钟还要接吗？

> **答案**：能用，此时它退化成纯组合的 `Out = A*B`（延迟 0 拍）。由于没有任何寄存器，`Clk`/`Rst` 不再必需——这正是端口 `Clk`/`Rst` 默认值给 `'0'` 的原因（见 mult 文档 Control 端口说明）。

### 4.3 模块三：from_real / to_real —— real 桥接器

#### 4.3.1 概念说明

`olo_fix_from_real` 和 `olo_fix_to_real` 是一对特殊的桥接器：它们在 `real`（VHDL 的浮点数）与定点之间转换。**它们是本讲唯一不带流水线寄存器的实体**，原因在源码注释里写得很直白：`real` 只在综合期/仿真期用于「算常量或打印观察」，它在真实硬件里根本不存在，因此没有「延迟」可言。

#### 4.3.2 核心流程

- `olo_fix_from_real`：把一个 `real` 常量（编译期已知）转成定点向量，用于给设计提供常量（如滤波器系数、增益）。值可通过泛型 `Value_g` 传入。
- `olo_fix_to_real`：把一个定点向量转成 `real`，**仅用于仿真**（打印、断言），综合时会被裁掉。

#### 4.3.3 源码精读

**(1) `olo_fix_from_real`：纯组合、综合期生效**

[src/fix/vhdl/olo_fix_from_real.vhd:36-61](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_from_real.vhd#L36-L61)

- 没有 `Clk`/`Rst`/`In_Valid`，只有一个输出端口 `Out_Value`（L46）。
- `Value_g`（L42）默认 `0.0`，`Saturate_g` 默认 `SatWarn_c`（L40）——即「常量超界时既限幅又报警」。
- 转换只有一行（L59）：`Out_Value <= cl_fix_from_real(Value_g, ResultFmt_c, Saturate_c);` 综合后这根线被折叠成一个常量向量，不占任何寄存器。

**(2) `olo_fix_to_real`：仅仿真期生效**

[src/fix/vhdl/olo_fix_to_real.vhd:36-59](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_to_real.vhd#L36-L59)

- 输出是 `real` 类型（L45），这种类型无法综合成硬件。
- 转换一行（L57）：`Out_Value <= cl_fix_to_real(In_A, AFmt_c);`
- 典型用法：在 testbench 里把 DUT 的定点输出转成 `real` 打印出来，或与期望值做浮点比较。

> 注意 `from_real` 的注释里写「Value can be passed as generic or port」，但当前版本只暴露了泛型 `Value_g`（[src/fix/vhdl/olo_fix_from_real.vhd:42](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_from_real.vhd#L42)），并无运行期端口——阅读源码时以实际端口为准。

#### 4.3.4 代码实践

**实践目标**：用 `olo_fix_from_real` 生成一个常量，喂给 4.2 的乘法通路，体会「real → 定点常量」的零开销。

**操作步骤**：

1. 实例化 `olo_fix_from_real`，`ResultFmt="(1,1,8)"`、`Value_g=0.25`，其 `Out_Value` 即为 4.2.4 手算里 `B` 的编码 `0001000000`。
2. 把它接到 `olo_fix_mult` 的 `In_B`，`In_A` 接可变激励。
3. 综合后查看资源报告：`from_real` 不应产生任何寄存器，只是一组接 `0`/`1` 的常量线。

**需要观察的现象**：`from_real` 的输出在仿真第 0 拍就稳定（无延迟），因为它本质是常量。

**预期结果**：mult 输出 = `In_A × 0.25`，延迟仍为 3 拍（全部来自 mult，`from_real` 贡献 0）。**综合资源报告待本地验证**。

#### 4.3.5 小练习与答案

**练习**：为什么 `olo_fix_to_real` 不能出现在可综合的设计顶层，而只能用在 testbench？

> **答案**：`real` 是 VHDL 的浮点类型，FPGA 综合工具无法把它映射成硬件。`olo_fix_to_real` 的输出端口类型就是 `real`，综合时该路径会被忽略或报错。它的设计目的就是「在仿真里把定点值翻译成易读的浮点数」，属于纯验证辅助。

### 4.4 模块四：neg / abs / compare —— 一元运算与比较

#### 4.4.1 概念说明

这一组实体结构与二元运算同构（都是「全精度运算 + OpRegs + resize」），只是输入只有一个操作数（compare 除外，它有两个输入但输出是 1 位）：

- `olo_fix_neg`：取负。
- `olo_fix_abs`：取绝对值（结果恒为无符号）。
- `olo_fix_compare`：按 `Comparison_g`（如 `"="`、`"/="`、`"<"`、`">="` 等）比较 A 与 B，输出 1 位布尔。

#### 4.4.2 核心流程

`neg`/`abs` 的流程与 mult 完全一致：

```text
In_A ──► [cl_fix_neg/abs: 全精度 Fmt] ──► OpRegs ──► [resize] ──► Out_Result
```

`compare` 略有不同：它没有 round/saturate（结果是 1 位布尔，无所谓格式），所以只有运算 + OpRegs：

```text
In_A,In_B ──► [cl_fix_compare] ──► OpRegs ──► Out_Result (1 bit)
```

#### 4.4.3 源码精读

**(1) `olo_fix_neg` / `olo_fix_abs` 的全精度格式**

[src/fix/vhdl/olo_fix_neg.vhd:66-76](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_neg.vhd#L66-L76) —— `NegFmt_c := cl_fix_neg_fmt(AFmt_c)`，运算 `cl_fix_neg(...)`。取负的自然格式比输入多出整数位余量（因为最小负数取负会溢出，en_cl_fix 会预留位）。

[src/fix/vhdl/olo_fix_abs.vhd:66-76](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_abs.vhd#L66-L76) —— `AbsFmt_c := cl_fix_abs_fmt(AFmt_c)`，运算 `cl_fix_abs(...)`。绝对值的自然格式是**无符号**的（`S=0`），但整数位会比输入多（用来容纳可能的最小负数取绝对值）。

两者其后都接 `olo_fix_private_optional_reg`（OpRegs）与 `olo_fix_resize`（round+sat），与 mult 完全同构（`neg` 的 resize 接法见 [src/fix/vhdl/olo_fix_neg.vhd:94-110](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_neg.vhd#L94-L110)，`abs` 见 [src/fix/vhdl/olo_fix_abs.vhd:94-110](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_abs.vhd#L94-L110)）。

**(2) `olo_fix_compare`：1 位输出**

[src/fix/vhdl/olo_fix_compare.vhd:36-57](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_compare.vhd#L36-L57) —— 关键差异：

- 泛型 `Comparison_g`（L41）是字符串，如 `"="`、`"/="`、`"<"`、`">="` 等（直接对应 en_cl_fix 支持的比较运算符）。
- 输出 `Out_Result` 只有 1 位（L55）。
- **没有 `Round_g`/`Saturate_g`/`RoundReg`/`SatReg`**，只有 `OpRegs_g`（默认 1）。

运算本体把布尔结果转成 `std_logic`：

[src/fix/vhdl/olo_fix_compare.vhd:72-73](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_compare.vhd#L72-L73)

```vhdl
Comp_DataBool <= cl_fix_compare(Comparison_g, In_A, AFmt_c, In_B, BFmt_c);
Comp_DataComb <= '1' when Comp_DataBool else '0';
```

寄存器宽度固定为 1（[src/fix/vhdl/olo_fix_compare.vhd:76-88](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_compare.vhd#L76-L88)，`Width_g => 1`），默认延迟 1 拍。

#### 4.4.4 代码实践

**实践目标**：用 `olo_fix_abs` 把一个有符号信号转成无符号幅度，并验证其自然格式为无符号。

**操作步骤**：

1. 实例化 `olo_fix_abs`，`AFmt="(1,1,8)"`，`ResultFmt="(0,2,8)"`（无符号、整数位给到 2 以容纳绝对值范围）。
2. 输入 `A = -1.5`（`(1,1,8)` 编码 `10.10000000`，即 `1010000000`）。
3. 手算：`|-1.5| = 1.5`，在 `(0,2,8)` 下编码为 `001.10000000` = `00110000000`（11 位）。

**需要观察的现象**：输出始终为正（最高位为 0）；延迟默认 3 拍（OpRegs=1 + round + sat）。

**预期结果**：`Out_Result = 00110000000`（= 384，\(384 \times 2^{-8} = 1.5\)）。**波形待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`olo_fix_abs` 的 `AbsFmt_c` 为什么是 `S=0`（无符号）？

> **答案**：绝对值结果恒非负，不需要符号位；en_cl_fix 的 `cl_fix_abs_fmt` 据此把符号位设为 0，但会相应增加整数位以容纳「最小负数取绝对值」可能产生的溢出。

**练习 2**：`olo_fix_compare` 的 `Comparison_g` 用字符串而不是自定义枚举类型，原因是什么？

> **答案**：与全库的字符串泛型策略一致（见 [u8-l1](./u8-l1-fix-principles-enclfix.md)、[u8-l2](./u8-l2-olo-fix-pkg.md)）——自定义类型无法被 Verilog 实例化时统一传递，而字符串（如 `"<"`）在 VHDL/Verilog 两边都能直接表达。

## 5. 综合实践

把本讲四个模块串起来，搭一条「**带增益的差分通路**」：

**设计目标**：计算 \(Y = (A - B) \times G\)，其中 A、B 为 `(1,5,8)` 的有符号输入，G 是一个常量增益（如 `0.5`），输出 `ResultFmt="(1,5,8)"`。

**实现要点**：

1. 用 `olo_fix_from_real`（模块三）把 `Value_g=0.5` 转成常量，作为增益 G 的来源——零寄存器开销。
2. 用 `olo_fix_sub`（模块二）计算 `A - B`，全精度自然格式为有符号、整数位 +1。
3. 用 `olo_fix_mult`（模块二）把差值乘上 G，注意 mult 的 `BFmt` 要匹配 G 的格式。
4. 整条链路的舍入/饱和在各实体的 resize 段独立配置：中间级建议 `Trunc_s`/`None_s`（不丢中间精度），仅在最后一级用 `NonSymPos_s`/`Sat_s`。

**验证**：

- 用 Python（en_cl_fix 的 Python 模型，或直接手算）算出若干样本的期望值。
- 写一个最小 testbench，用 `olo_fix_to_real`（模块三）把 DUT 输出转成 `real` 打印，与期望值比对；或更规范地用仓库的协仿真组件（见 [u8-l5](./u8-l5-cosimulation.md)）做位真检查。

**思考题**（不必实现，仅用于巩固）：

- 整条链路在默认寄存配置下的总延迟是多少拍？（提示：sub 3 拍 + mult 3 拍 = 6 拍，`from_real` 0 拍。）
- 如果想压到最低延迟，哪些寄存可以关掉？关掉后组合路径变长会带来什么风险？

> 本综合实践的仿真结果**待本地验证**。建议跑通后对比「中间全截断、末端舍入」与「每级都舍入」两种配置下的输出噪声差异，直观体会三段式分解的意义。

## 6. 本讲小结

- 所有基本运算实体都遵循 **operation → round → saturate** 三段式：`round`/`saturate` 是单独的两段，`resize` 把它们串成一个通用格式转换器。
- 二元运算（`add`/`sub`/`mult`）与一元运算（`neg`/`abs`）结构同构：**先用 en_cl_fix 函数算全精度结果（自然格式由 `cl_fix_*_fmt` 给出），再接 OpRegs，最后接 `resize` 收敛到目标格式**；默认延迟 3 拍。
- 全精度运算段硬编码 `Trunc_s, Warn_s`（不丢精度），用户的 `Round_g`/`Saturate_g` 只在 resize 段生效——决策点集中、语义清晰。
- 寄存由 `olo_fix_private_optional_reg` 提供，是否插入由 `fixImplementReg` 按 `YES`/`NO`/`AUTO` 三态判定；`AUTO` 用每段的 `LogicPresent_c`（round 看小数位、saturate 看整数/符号位+饱和模式）决定。
- `from_real`/`to_real` 是**唯一无寄存器**的实体对：`real` 只存在于综合期/仿真期，硬件里没有，故无延迟可言；`to_real` 仅限 testbench 使用。
- `compare` 输出 1 位布尔、无 round/sat，靠字符串泛型 `Comparison_g` 选择比较运算符，体现全库字符串泛型策略。
- 所有实体与对应 en_cl_fix 函数**位真等价**，可用仓库现成的协仿真测试台（如 `olo_fix_mult_tb`）验证。

## 7. 下一步学习建议

- **深入 en_cl_fix 本体**：本讲多次引用 `cl_fix_*_fmt`、`cl_fix_round_fmt` 等函数，它们的具体规则在 `3rdParty/en_cl_fix` 的文档与源码里。建议 `git submodule update --init` 后通读其 README，把各 `*_fmt` 函数的精确公式补齐。
- **Python 单一真相源**：本讲实践里提到「Python 模型与 HDL 位真等价」，下一讲 [u8-l4 Python 代码生成：olo_fix_pkg_writer](./u8-l4-python-codegen-pkg-writer.md) 会讲如何用 Python 维护常量与格式并自动生成 HDL 包。
- **协仿真闭环**：[u8-l5 协同仿真：olo_fix_cosim 与 sim_stimuli/checker](./u8-l5-cosimulation.md) 将把本讲的「手算对照」升级为自动化位真验证流程（即 `olo_fix_mult_tb` 所用的那套机制）。
- **组合出复杂 DSP**：掌握基本运算后，可进入第 9 单元（[u9-l1 复数运算、MAC 与除法/限幅](./u9-l1-complex-mac-div-limit.md) 等），看 `olo_fix_cplx_mult`、`olo_fix_madd` 等如何复用本讲的积木构建滤波器与混频器。
