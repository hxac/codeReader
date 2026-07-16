# 定点原理与 en_cl_fix 基础

## 1. 本讲目标

Open Logic 的 `fix` 区域专门做**定点数（fixed-point）运算**：乘法、加法、FIR 滤波、CORDIC、CIC 抽取等等。这些实体并不是从零发明一套定点规则，而是建立在一个第三方库 **en_cl_fix** 之上。本讲是整个 `fix` 区域的「地基课」：不写任何电路逻辑，而是把三件事讲透——

1. 定点数怎么用 `(S, I, F)` 三元组表示，en_cl_fix 定义了哪些格式 / 舍入 / 饱和类型。
2. en_cl_fix 原本只提供 VHDL **函数**，Open Logic 为什么要把它包装成**实体（entity）**。
3. 为什么实体的接口上一律用**字符串泛型**（如 `"(1,8,23)"`、`"Trunc_s"`），而不是直接用 en_cl_fix 的自定义类型。

学完后，你应该能：看懂任何一个 `olo_fix_*` 实体的泛型列表、用字符串正确写出格式与舍入参数、并解释这一设计背后「为了同时支持 VHDL 与 Verilog」的根本动机。后续 `u8-l2`～`u8-l5` 以及第 9 单元的所有 DSP 讲义，都建立在本讲的术语与约定之上。

## 2. 前置知识

本讲依赖 `u1-l5`（编码规范与阅读一个实体）建立的基础，特别需要你已经熟悉：

- **泛型（generic）与常量（constant）**：实例化时确定的参数。Open Logic 用后缀 `_g` 标注泛型、`_c` 标注常量（见 `olo_fix_resize.vhd` 中的 `AFmt_g` 与 `AFmt_c`）。
- **实体（entity）与函数（function）的区别**：实体是「电路元件」，有端口、可被实例化；函数是「运算」，写在进程或表达式里。

本讲用到、但会当场解释的新术语：

- **定点数（fixed-point）**：用固定位数 + 固定小数点位置表示带小数的数，区别于浮点（小数点会浮动）。FPGA 里定点几乎总是比浮点省资源、快得多。
- **分辨率（resolution / LSB 权重）**：定点数最小能区分的步长，即「1 个最低位」代表的实数值。
- **饱和（saturation）**：运算结果超出格式能表示的范围时，钳位到最大 / 最小可表示值，而不是回绕（wrap）。
- **舍入（rounding）**：把高位宽的结果截短到目标位宽时，决定「丢掉的那几位怎么处理」的策略。
- **子模块（submodule）**：en_cl_fix 是一个独立的 git 仓库，被嵌套进 Open Logic 的 `3rdParty/en_cl_fix/` 目录，需要用 `--recursive` 克隆（详见 `u1-l3`）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [doc/fix/olo_fix_principles.md](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/fix/olo_fix_principles.md) | `fix` 区域的设计原则总纲，是本讲最重要的依据：解释 en_cl_fix 的定位、组件 vs 函数、字符串泛型、流水线寄存器与协仿真。 |
| [3rdParty/en_cl_fix/hdl/en_cl_fix_pkg.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/3rdParty/en_cl_fix/hdl/en_cl_fix_pkg.vhd) | en_cl_fix 的 VHDL 包，真正定义 `FixFormat_t` / `FixRound_t` / `FixSaturate_t` 类型与 `cl_fix_*` 函数。**该文件位于 git 子模块内**，若未用 `--recursive` 克隆则本地不存在；其具体行号在本环境中无法确认（标注「待确认」）。 |
| [compile_order.txt](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/compile_order.txt) | 全库编译顺序，是 en_cl_fix 必须先于 `olo_fix` 编译这一依赖关系的可验证证据。 |
| [src/fix/vhdl/olo_fix_pkg.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_pkg.vhd) | Open Logic 自己的 fix 包：把 en_cl_fix 的枚举值导出成字符串常量、提供「字符串 → 位宽」等工具函数。 |
| [src/fix/vhdl/olo_fix_resize.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_resize.vhd) | 一个最基本、也最适合做样例的实体：把 `cl_fix_resize` 函数包装成实体，演示字符串泛型、位宽推导与「舍入 + 饱和」两段结构。 |

---

## 4. 核心概念与源码讲解

### 4.1 en_cl_fix 与定点格式类型

#### 4.1.1 概念说明

定点数的核心思想是：**一串二进制位 + 一个固定的小数点位置**。两个同样 8 位的数，如果小数点位置不同，它们表示的真实数值就完全不同。所以「描述一个定点格式」必须说清三件事，en_cl_fix 用一个三元组 `(S, I, F)` 来概括：

- **S（Sign）**：符号位的个数。`S=1` 表示有符号数（最高位是符号位，用补码），`S=0` 表示无符号数。
- **I（Integer-Bits）**：整数部分的位数。
- **F（Fractional-Bits）**：小数部分的位数。

例如 `(1, 8, 23)` 表示：1 位符号、8 位整数、23 位小数，总共 \(1+8+23=32\) 位。这其实就是 IEEE 754 单精度浮点「尾数」的位宽，是一个常见的定点格式。

由此可以推出两条关键性质。设某格式为 \((S, I, F)\)，总位宽与分辨率分别为：

\[
W = S + I + F,\qquad \Delta = 2^{-F}
\]

\(\Delta\) 就是「最低位代表多少」，也叫分辨率或 LSB 权重。对 `(1,8,23)`，\(\Delta = 2^{-23} \approx 1.19\times10^{-7}\)。该格式能表示的数值范围为：

\[
\text{有符号 }(S=1):\ [-2^{I},\ 2^{I}-2^{-F}],\qquad
\text{无符号 }(S=0):\ [0,\ 2^{I}-2^{-F}]
\]

所以 `(1,8,23)` 能表示 \([-256,\ 255.99999988]\) 内、步长为 \(2^{-23}\) 的值。

仅有「格式」还不够。把一个高位宽的运算结果（例如两个数相乘，结果位宽变大）放回目标格式时，要丢掉多余的小数位和整数位，于是还需要两类策略：

- **舍入（rounding）**：决定丢掉低位小数时怎么取舍（直接截断？四舍五入？向偶数取整？）。
- **饱和（saturation）**：决定结果超出范围时怎么办（钳位到最大/最小？允许溢出回绕？只警告不处理？）。

en_cl_fix 把这三件事分别做成三个自定义类型，这正是本模块要掌握的「格式类型」：

- **`FixFormat_t`**：格式 `(S, I, F)`，用一个 record（记录）表示。
- **`FixRound_t`**：舍入策略枚举，取值 `Trunc_s, NonSymPos_s, NonSymNeg_s, SymInf_s, SymZero_s, ConvEven_s, ConvOdd_s`。最常用的是 `Trunc_s`（直接截断）和 `NonSymPos_s`（四舍五入、半值向上）。
- **`FixSaturate_t`**：饱和策略枚举，取值 `Sat_s, None_s, SatWarn_s, Warn_s`。`Sat_s` 表示钳位饱和、`None_s` 表示允许回绕。

这三组类型与取值在原则文档里有权威列举：

> FixFormat_t：格式定义为 record，形式 `(S, I, F)`，例如 `(1, 8, 23)`；FixRound_t 与 FixSaturate_t 为枚举类型，分别定义舍入与饱和选项。
> ——见 [doc/fix/olo_fix_principles.md:47-58](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/fix/olo_fix_principles.md#L47-L58)

> ⚠️ **关于 `en_cl_fix_pkg.vhd` 的行号**：上述类型真正定义在子模块文件 `3rdParty/en_cl_fix/hdl/en_cl_fix_pkg.vhd` 中（`FixFormat_t` 是 record，`FixRound_t`/`FixSaturate_t` 是枚举，`cl_fix_*` 是运算函数）。该子模块在当前阅读环境未检出，无法提供准确行号——**待确认**。本讲对这些类型的描述以原则文档 `olo_fix_principles.md` 为准。

#### 4.1.2 核心流程

一个基本定点运算（如加法）在 en_cl_fix 的世界观里被自然地分成三段，Open Logic 把这套思想原样继承了下来：

1. **运算（operation）**：执行真正的数学运算，产生一个「自然位宽」的中间结果。比如两个 `(1,8,23)` 相加，结果自然需要 `(1,9,23)`（整数位多 1 位防溢出）。
2. **舍入（rounding）**：把中间结果的小数位截短到目标格式 `F`。
3. **饱和（saturation）**：把整数位截短到目标格式 `I`，超出范围则按饱和策略钳位。

为什么要拆成三段而不是一步到位？因为「一步到位」会把一长串组合逻辑塞进同一个时钟周期，严重拖慢最高时钟频率。拆开之后，每一段之间都可以按需插入流水线寄存器（详见 4.2）。

伪代码描述这三段的依赖：

```text
输入 a (AFmt) ──▶ [operation] ──▶ 中间结果 (自然格式)
                                     │
                               [rounding]  ── 按目标 F 截小数
                                     │
                               [saturation] ── 按目标 I 截整数 / 钳位
                                     │
                                   输出 (ResultFmt)
```

#### 4.1.3 源码精读

三段式结构（operation → round → saturate）在原则文档里以加法为例明确画出，并解释了为何要分段：

> Executing all three steps in one clock cycle very often can be limiting to the clock speed. Therefore the user can configure pipeline registers.
> ——见 [doc/fix/olo_fix_principles.md:112-124](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/fix/olo_fix_principles.md#L112-L124)

这三段在实体里是如何落地的？以 `olo_fix_resize`（格式转换，本身不含 operation，只有 round + saturate）为例，它的架构体里直接实例化了两个子实体——先 `olo_fix_round`，再 `olo_fix_saturate`，串成一条流水线：

```vhdl
-- 先做舍入（把小数位截到目标 F）
i_round : entity work.olo_fix_round
    generic map (
        AFmt_g      => AFmt_g,
        ResultFmt_g => to_string(RoundFmt_c),
        Round_g     => Round_g,
        RoundReg_g  => RoundReg_g
    ) ...

-- 再做饱和（把整数位截到目标 I，超出则钳位）
i_saturate : entity work.olo_fix_saturate
    generic map (
        AFmt_g      => to_string(RoundFmt_c),
        ResultFmt_g => ResultFmt_g,
        Saturate_g  => Saturate_g,
        SatReg_g    => SatReg_g
    ) ...
```

这段代码展示了两段串联，完整定义在 [src/fix/vhdl/olo_fix_resize.vhd:82-113](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_resize.vhd#L82-L113)。注意 `RoundFmt_c` 这个中间格式——它是「舍入后、饱和前」的结果格式，由 `cl_fix_round_fmt()` 算出，让两段衔接时格式不会错位。

#### 4.1.4 代码实践

**实践目标**：用 `(S, I, F)` 三元组「手算」一个定点格式的位宽、分辨率与范围，建立对格式的直觉。

**操作步骤**：

1. 选定格式 `(1, 8, 23)`。
2. 套用本节的公式：总位宽 \(W=S+I+F\)、分辨率 \(\Delta=2^{-F}\)、有符号范围 \([-2^I,\ 2^I-2^{-F}]\)。
3. 再算一个对照格式 `(0, 4, 4)`（无符号、4 整数位、4 小数位、共 8 位），写出它的范围与分辨率。

**需要观察的现象 / 预期结果**（待本地验证你手算是否正确）：

| 格式 | 总位宽 | 分辨率 \(\Delta\) | 表示范围 |
| --- | --- | --- | --- |
| `(1, 8, 23)` | 32 | \(2^{-23}\approx1.19\times10^{-7}\) | \([-256,\ 256-2^{-23})\) |
| `(0, 4, 4)` | 8 | \(2^{-4}=0.0625\) | \([0,\ 16-2^{-4})\) |

> 说明：表中的范围上界用半开区间 \(2^I-2^{-F}\) 表示「能取到的最大值再大一点点」。\((1,8,23)\) 的最大可表示值约为 \(255.99999988\)。

#### 4.1.5 小练习与答案

**练习 1**：`(0, 0, 8)` 表示什么样的数？总位宽是多少？
**答案**：无符号、没有整数位、8 位小数，总位宽 8 位。它只能表示 \([0,\ 1-2^{-8})\) 即 \([0,\ 0.99609375]\) 范围内的纯小数，分辨率 \(2^{-8}=0.00390625\)。

**练习 2**：为什么两个 `(1, 8, 23)` 的数相加，结果的自然格式是 `(1, 9, 23)` 而不是 `(1, 8, 23)`？
**答案**：两个同范围的有符号数相加，结果可能比任一输入大 1 倍，整数位需要多 1 位才能不溢出（即 \([-2^9, 2^9)\) 才放得下 \([-2^9, 2^9)\) 量级的和）。小数位不变，故为 `(1, 9, 23)`。

---

### 4.2 组件 vs 函数：为什么要把 en_cl_fix 包装成实体

#### 4.2.1 概念说明

en_cl_fix 的核心是一组 VHDL **函数**（如 `cl_fix_resize`、`cl_fix_add`、`cl_fix_mult`）。从纯功能角度看，这些函数「完全够用」——VHDL 用户在进程里直接调用即可写出精炼的定点运算。那 Open Logic 为什么还要再造一套 `olo_fix_*` **实体**？

根本原因是**跨语言互操作**。原则文档讲得很直白：

> 函数在功能上正是所需，但 VHDL 包里的函数**无法从 Verilog 调用**。因此 Open Logic 把 `resize`、`add`、`multiply` 等基本函数包装成实体——实体可以从 Verilog 轻松实例化。
> ——见 [doc/fix/olo_fix_principles.md:28-43](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/fix/olo_fix_principles.md#L28-L43)

也就是说，「函数 → 实体」的包装有两层动机：

1. **互操作**：实体（component）是所有 HDL 工具都认得的基本单位，Verilog/SystemVerilog 可以直接实例化；而 VHDL 包函数跨语言不可见。包装成实体后，VHDL 与 Verilog 用户都能用同一套定点功能。
2. **加流水线寄存器**：函数是「一拍内的组合运算」，没法内嵌寄存存器。实体可以在运算、舍入、饱和之间插入寄存器（见 4.1.2 的三段式），从而把长组合路径切断、提升时钟频率——这对 DSP、视频处理这类对主频敏感的应用至关重要。

由此引出一条重要的**分工**（务必记住）：

- **Verilog 用户**：只能使用 Open Logic 提供的 `olo_fix_*` 实体。
- **VHDL 用户**：既可以用这些实体，也可以直接在进程里调用 `en_cl_fix` 的函数。两种方式都受完整支持，且能与 Open Logic 的定点组件无缝对接。

#### 4.2.2 核心流程

把一个函数包装成实体，流程是「套壳 + 加寄存」：

```text
cl_fix_xxx() 函数（纯组合运算）
        │  套壳为 entity
        ▼
olo_fix_xxx 实体
   ├── 泛型：输入/输出格式、舍入、饱和、各段是否插寄存
   ├── 端口：Clk/Rst + AXI-S 风格 Valid 握手 + 数据
   └── 内部：把函数拆成 operation → round → saturate，按泛型决定每段是否打拍
```

其中「是否插流水线寄存器」由一组字符串泛型控制，三档可选（详见原则文档 [olo_fix_principles.md:126-148](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/fix/olo_fix_principles.md#L126-L148)）：

| 取值 | 含义 | 优点 | 缺点 |
| --- | --- | --- | --- |
| `"YES"` | 该段之后**总是**插一拍寄存（哪怕没有舍入/饱和逻辑） | 主频高、延迟固定（与格式无关） | 没必要的场景也多吃一拍延迟。这是**默认值**。 |
| `"NO"` | **不**插寄存 | 延迟低且固定 | 组合路径长，可能拖慢主频 |
| `"AUTO"` | 仅在该段**确实需要**舍入/饱和逻辑时才插寄存 | 主频高、不白吃延迟 | 延迟随格式变化 |

注意 `"YES"` 是默认值——Open Logic 在「主频」与「延迟」之间默认偏向前者，因为定点数学常用于对速度敏感的场景。

#### 4.2.3 源码精读

`olo_fix_resize` 就是「把 `cl_fix_resize` 函数包装成实体」的典型样例。它的实体声明里，舍入段和饱和段各自有一个寄存控制泛型：

```vhdl
generic (
    -- Formats / Round / Saturate
    AFmt_g      : string;
    ResultFmt_g : string;
    Round_g     : string := FixRound_Trunc_c;   -- 默认截断
    Saturate_g  : string := FixSaturate_Warn_c;  -- 默认只警告
    -- Registers
    RoundReg_g  : string := "YES";               -- 舍入段寄存：默认 YES
    SatReg_g    : string := "YES"                -- 饱和段寄存：默认 YES
);
```

见 [src/fix/vhdl/olo_fix_resize.vhd:36-45](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_resize.vhd#L36-L45)。这两个 `...Reg_g` 正是 4.2.2 表里那三档（`"YES"`/`"NO"`/`"AUTO"`）的入口。而真正的「函数调用」发生在它实例化的子实体内部（`olo_fix_round` / `olo_fix_saturate` 里会调用 `cl_fix_round` / `cl_fix_saturate`）——本实体只负责把它们串起来并按泛型插寄存。这正体现了「套壳」的含义。

> 关于 operation 段的寄存：`resize` 没有 operation 段（它只做格式转换），所以只看到 `RoundReg_g` / `SatReg_g`。对 `olo_fix_mult` 这类有运算的实体，还会有控制 operation 段寄存**个数**的泛型（可填 0、1、2…），用于在乘法器/宽加法器后多打几拍。原则文档对此有说明：见 [olo_fix_principles.md:145-148](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/fix/olo_fix_principles.md#L145-L148)。

#### 4.2.4 代码实践

**实践目标**：体会「函数 vs 实体」在两种语言里的可调用性差异。

**操作步骤**：

1. 在仓库里用搜索确认 `olo_fix_resize` 确实是个 entity（而非函数）——它有 `generic` 和 `port`，可以被实例化。
2. 阅读它的架构体，确认它**没有自己实现 resize 数学**，而是实例化 `olo_fix_round` + `olo_fix_saturate`（见 4.1.3 引用的代码）。
3. （选做，源码阅读型）打开任意一个 `olo_fix_*` 实体，找出它内部调用 en_cl_fix 函数（形如 `cl_fix_...`）的位置，说明「数学在子实体里、壳在外层」的分层。

**需要观察的现象 / 预期结果**：你会确认 `olo_fix_resize` 是一层「壳」，真正的 `cl_fix_*` 函数调用被下沉到了更底层的 `olo_fix_round` / `olo_fix_saturate`。这种分层让外层只关心「接口 + 串联 + 寄存配置」，内层只关心「数学」，正是把函数包装成实体的标准做法。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `"YES"` 被设为寄存泛型的默认值，而不是 `"AUTO"`？
**答案**：`"YES"` 保证延迟固定（与具体格式无关），且主频最高——在定点数学常见的 DSP/视频等对速度敏感的场景里，可预测的高主频比省一两拍延迟更重要。`"AUTO"` 虽然不白吃延迟，但延迟会随格式变化，难以预测。

**练习 2**：一个纯 VHDL 项目，想用最少的代码做一次定点乘法，有两种选择：直接调 `cl_fix_mult` 函数，或实例化 `olo_fix_mult` 实体。分别适合什么场景？
**答案**：若不在意主频、想要最少代码与最低延迟，直接在进程里调函数即可；若需要可配置的流水线寄存器来切断长路径、或希望将来能被 Verilog 同事复用，就用实体。

---

### 4.3 字符串泛型模式：用字符串传递自定义类型

#### 4.3.1 概念说明

这是本讲最核心、也最容易让初学者困惑的设计。上一节说实体用泛型接收格式、舍入、饱和配置。那为什么泛型类型是 `string`，而不是 en_cl_fix 的 `FixFormat_t` / `FixRound_t` / `FixSaturate_t`？

因为**自定义类型在「从 Verilog 实例化 VHDL」时没有跨工具的统一支持**。原则文档解释：

> 从 Verilog 实例化 VHDL 时，自定义类型通常不被支持。有些工具在一定程度上允许，但在 Open Logic 支持的所有工具之间没有共同立场。因此 Open Logic 组件的接口上避免使用这些类型，以确保所有组件都能在 Verilog 中实例化。
> ——见 [doc/fix/olo_fix_principles.md:60-66](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/fix/olo_fix_principles.md#L60-L66)

解决办法出奇地简单：**把这些值当作字符串传递**，记法和 VHDL 原生写法完全一样，只是外面加一对引号。于是：

- 格式 `(1,8,23)` → 字符串 `"(1,8,23)"`
- 舍入 `Trunc_s` → 字符串 `"Trunc_s"`
- 饱和 `Sat_s` → 字符串 `"Sat_s"`

在 Verilog 里：

```verilog
localparam string fmt      = "(1,8,23)";   // FixFormat_t
localparam string round    = "Trunc_s";    // FixRound_t
localparam string saturate = "Sat_s";      // FixSaturate_t
```

在 VHDL 里完全对应：

```vhdl
constant fmt      : string := "(1,8,23)";
constant round    : string := "Trunc_s";
constant saturate : string := "Sat_s";
```

以上两段示例直接对应原则文档：见 [olo_fix_principles.md:67-91](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/fix/olo_fix_principles.md#L67-L91)。

> 注意：本节以及全文出现的这类 `constant ... : string := ...` 片段，是为了说明字符串泛型写法而摘录的**示例代码**（取自原则文档），并非某个实体的完整源码。

#### 4.3.2 核心流程

字符串只是「搬运工」，真正的类型转换发生在**实体内部、综合前的 elaboration（详述）阶段**。流程是：

```text
用户传字符串泛型  "(1,8,23)"  /  "Trunc_s"
        │
        ▼  实体内部用 en_cl_fix 的转换函数（编译期常量）
cl_fix_format_from_string()  ──▶  FixFormat_t   (S,I,F) record
cl_fix_round_from_string()   ──▶  FixRound_t    枚举
        │
        ▼  用于真正的 cl_fix_* 运算
   cl_fix_resize(...) / cl_fix_round(...) ...
```

关键点：这些转换是在 `constant` 声明里完成的，因而是**编译期常量**——不消耗运行期资源，等价于你直接写 typed 值。对 VHDL 用户，en_cl_fix 还提供了反向函数 `to_string()`，可以把 typed 值转回字符串，方便「在自家代码里用 typed 类型算格式、再转成字符串去实例化 Open Logic 实体」。原则文档对此有完整示例：见 [olo_fix_principles.md:93-110](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/fix/olo_fix_principles.md#L93-L110)。

#### 4.3.3 源码精读

`olo_fix_resize` 是字符串泛型模式的活样板。三处关键：

**① 泛型全用 `string`，并用字符串常量给默认值：**

```vhdl
AFmt_g      : string;                              -- 输入格式，如 "(1,8,23)"
ResultFmt_g : string;                              -- 输出格式
Round_g     : string := FixRound_Trunc_c;          -- 默认 "Trunc_s"
Saturate_g  : string := FixSaturate_Warn_c;        -- 默认 "Warn_s"
```

见 [src/fix/vhdl/olo_fix_resize.vhd:38-41](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_resize.vhd#L38-L41)。注意默认值 `FixRound_Trunc_c` 本身就是一个字符串常量——它定义在 Open Logic 自己的 `olo_fix_pkg` 里：

```vhdl
constant FixRound_Trunc_c     : string := "Trunc_s";
constant FixRound_NonSymPos_c : string := "NonSymPos_s";
-- ... 其余舍入枚举 ...
constant FixSaturate_None_c    : string := "None_s";
constant FixSaturate_Sat_c     : string := "Sat_s";
-- ... 其余饱和枚举 ...
```

见 [src/fix/vhdl/olo_fix_pkg.vhd:43-54](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_pkg.vhd#L43-L54)。这就是「en_cl_fix 的枚举值被导出成字符串常量」的地方——VHDL 用户既可以直接写 `"Trunc_s"`，也可以用更醒目的 `FixRound_Trunc_c`，两者完全等价。

**② 端口位宽由字符串「现场推导」：**

```vhdl
In_A       : in  std_logic_vector(fixFmtWidthFromString(AFmt_g) - 1 downto 0);
Out_Result : out std_logic_vector(fixFmtWidthFromString(ResultFmt_g) - 1 downto 0)
```

见 [src/fix/vhdl/olo_fix_resize.vhd:52](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_resize.vhd#L52) 与 [olo_fix_resize.vhd:55](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_resize.vhd#L55)。`fixFmtWidthFromString()` 是 `olo_fix_pkg` 提供的工具函数，它把 `"(1,8,23)"` 解析成 `FixFormat_t`、再用 `cl_fix_width()` 算出位宽 32。它的实现非常简短：

```vhdl
function fixFmtWidthFromString (fmt : string) return natural is
    constant FixFmt_c : FixFormat_t := cl_fix_format_from_string(fmt);
begin
    return cl_fix_width(FixFmt_c);
end function;
```

见 [src/fix/vhdl/olo_fix_pkg.vhd:118-122](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_pkg.vhd#L118-L122)。这正印证了 4.3.2 的流程：字符串 →（`cl_fix_format_from_string`）→ `FixFormat_t` →（`cl_fix_width`）→ 位宽。

**③ 内部把字符串转回 typed 常量，供真正的运算使用：**

```vhdl
-- String to en_cl_fix
constant AFmt_c      : FixFormat_t := cl_fix_format_from_string(AFmt_g);
constant ResultFmt_c : FixFormat_t := cl_fix_format_from_string(ResultFmt_g);
constant Round_c     : FixRound_t  := cl_fix_round_from_string(Round_g);
```

见 [src/fix/vhdl/olo_fix_resize.vhd:61-64](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_resize.vhd#L61-L64)。注意命名：泛型是 `AFmt_g`（`_g` 后缀），由它派生的常量是 `AFmt_c`（`_c` 后缀）——这正是 `u1-l5` 讲过的命名规范在定点代码里的体现。转换用的 `cl_fix_format_from_string` 与 `cl_fix_round_from_string` 都是 en_cl_fix 提供的函数（定义于子模块 `en_cl_fix_pkg.vhd`，行号待确认）。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：阅读原则文档，亲手用字符串形式写出本讲规格要求的那三个泛型值，并能解释「为何不在接口上用 VHDL 自定义类型」。

**操作步骤**：

1. 打开 [doc/fix/olo_fix_principles.md](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/fix/olo_fix_principles.md)，定位到 *String Generics* 一节（约 45–110 行）。
2. 用 **VHDL** 写出三个字符串常量，分别表示格式 `(1,8,23)`、舍入 `Trunc_s`（截断）、饱和 `Sat_s`（钳位饱和）：

   ```vhdl
   -- 示例代码：三个定点配置的字符串形式
   constant FMT_C      : string := "(1,8,23)";  -- FixFormat_t
   constant ROUND_C    : string := "Trunc_s";   -- FixRound_t：截断
   constant SATURATE_C : string := "Sat_s";     -- FixSaturate_t：饱和钳位
   ```

3. 用 **Verilog** 再写一遍等价的 `localparam`（见 4.3.1 的示例）。
4. 用一句话回答：**为什么 Open Logic 不在实体接口上直接用 `FixFormat_t` 这类自定义类型，而要绕一层字符串？**

**需要观察的现象 / 预期结果**：

- 三个字符串值就是 `"(1,8,23)"`、`"Trunc_s"`、`"Sat_s"`，记法与 VHDL 原生 typed 写法完全一致，只多一对引号。
- 第 4 步的参考答案：因为自定义 VHDL 类型在「从 Verilog 实例化 VHDL」时，各 EDA 工具的支持程度不一、没有跨工具的共同立场；而 `string` 是所有工具都能传递的基本类型。用字符串就能保证**同一套实体既能被 VHDL、也能被 Verilog 实例化**，满足 Open Logic「纯 VHDL 实现、但全语言可用」的设计哲学（承接 `u1-l1`）。

> 说明：本实践为源码阅读 + 写法练习，不涉及运行；若想验证字符串真的能被解析，可进入 4.4 的综合实践，把这三个值塞进 `olo_fix_resize` 的实例化并跑仿真（仿真需先检出 en_cl_fix 子模块，**待本地验证**）。

#### 4.3.5 小练习与答案

**练习 1**：字符串 `"(0,4,4)"` 表示什么格式？它的位宽是多少？
**答案**：无符号、4 整数位、4 小数位，位宽 \(0+4+4=8\)。经 `fixFmtWidthFromString("(0,4,4)")` 返回 8。

**练习 2**：`olo_fix_resize` 的 `In_A` 端口为什么能写成 `std_logic_vector(fixFmtWidthFromString(AFmt_g)-1 downto 0)`，而不是写死宽度？
**答案**：因为输入格式 `AFmt_g` 是泛型，不同实例化会传不同格式（如 `"(1,8,23)"` 或 `"(0,4,4)"`），位宽随之变化。`fixFmtWidthFromString` 在 elaboration 阶段把字符串泛型解析成具体位宽，让端口宽度自动适配——这正是字符串泛型模式的收益之一。

**练习 3**：VHDL 用户在自家代码里用 typed `FixFormat_t` 算出了一个格式 `MyFmt`，想拿去实例化 `olo_fix_resize`，该怎么做？
**答案**：用 en_cl_fix 的 `to_string()` 把 typed 值转成字符串，再传给 `AFmt_g`。例如 `AFmt_g => to_string(MyFmt)`。原理文档在 [olo_fix_principles.md:98-110](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/fix/olo_fix_principles.md#L98-L110) 给出了完整示例。

---

### 4.4 VHDL/Verilog 互操作与编译依赖

#### 4.4.1 概念说明

前三个模块其实都在回答同一个问题：**怎样让一套定点功能同时服务于 VHDL 与 Verilog 用户**。本模块把这条主线收口，并补上「这些文件怎么编译到一起」的工程事实。

- **互操作闭环**：`olo_fix_*` 实体用字符串泛型（4.3）+ 函数包装成实体（4.2）这两招，绕开了「自定义类型跨语言不可见」和「包函数跨语言不可调用」两个障碍，使得 Verilog 用户也能像 VHDL 用户一样直接实例化定点组件。这也是为什么 Open Logic 的 `fix` 区域虽然底层是纯 VHDL 的 en_cl_fix，对外却宣称「VHDL 与 Verilog 均完全可用」（承接 `u1-l1` 的 Ease of Use 哲学）。
- **单一真相源（single source of truth）**：定点设计里，数值格式、滤波器系数等参数既要在 Python 模型里用、又要在 HDL 里用。Open Logic 强烈建议用 `olo_fix_pkg_writer`（Python）生成 HDL 包，让 Python 成为唯一来源、自动同步到 HDL，避免两边不一致。原则文档把这条称为 *Python to HDL Workflow*：见 [olo_fix_principles.md:192-199](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/fix/olo_fix_principles.md#L192-L199)。
- **协仿真（co-simulation）**：所有 `olo_fix_*` 组件都带 Python 位真模型，可先用 Python（NumPy/SciPy）开发算法、再把输入和期望输出通过协仿真文件喂给 HDL 仿真做逐位比对。这是 en_cl_fix 一脉相承的方法论：见 [olo_fix_principles.md:150-176](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/fix/olo_fix_principles.md#L150-L176)。本讲只建立概念，具体工具（`olo_fix_cosim` / `olo_fix_sim_stimuli` / `olo_fix_sim_checker` / `olo_fix_pkg_writer`）留给 `u8-l4`、`u8-l5` 详讲。

#### 4.4.2 核心流程

把 `fix` 区域编译起来的依赖链如下（关键：**en_cl_fix 必须先于 olo_fix**）：

```text
3rdParty/en_cl_fix/hdl/en_cl_fix_private_pkg.vhd   ← en_cl_fix 内部私有包
3rdParty/en_cl_fix/hdl/en_cl_fix_pkg.vhd           ← 定义 FixFormat_t / cl_fix_* 等
src/fix/vhdl/olo_fix_pkg.vhd                       ← use work.en_cl_fix_pkg.all
        │
        ▼  被 olo_fix_resize / olo_fix_round / ... 实体 use
src/fix/vhdl/olo_fix_resize.vhd  (及其余 olo_fix_* 实体)
        │
        ▼  部分实体内部还会回用 en_cl_fix 的底层运算实体
3rdParty/en_cl_fix/hdl/en_cl_fix_saturate.vhd
3rdParty/en_cl_fix/hdl/en_cl_fix_round.vhd
3rdParty/en_cl_fix/hdl/en_cl_fix_resize.vhd
```

这条顺序就是 `compile_order.txt` 的真实排列。注意两件事：其一，`olo_fix_pkg` 开头就 `use work.en_cl_fix_pkg.all`（见 [src/fix/vhdl/olo_fix_pkg.vhd:31](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_pkg.vhd#L31)），所以 en_cl_fix 必须先编译进同一个 `olo` 库；其二，en_cl_fix 不仅提供包函数，还提供了 `en_cl_fix_round/saturate/resize` 等底层运算实体，它们排在编译顺序的末尾，被 Open Logic 的 `olo_fix_*` 实体复用。

#### 4.4.3 源码精读

`compile_order.txt` 直接证明了这条依赖链——en_cl_fix 的两个包紧挨在 `olo_fix_pkg` 之前：

```text
3rdParty/en_cl_fix/hdl/en_cl_fix_private_pkg.vhd   # 第 56 行
3rdParty/en_cl_fix/hdl/en_cl_fix_pkg.vhd           # 第 57 行
src/fix/vhdl/olo_fix_pkg.vhd                       # 第 58 行
```

见 [compile_order.txt:56-58](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/compile_order.txt#L56-L58)。而 en_cl_fix 的三个底层运算实体则排在全部 `olo_fix_*` 实体之后：

```text
3rdParty/en_cl_fix/hdl/en_cl_fix_saturate.vhd      # 第 91 行
3rdParty/en_cl_fix/hdl/en_cl_fix_round.vhd         # 第 92 行
3rdParty/en_cl_fix/hdl/en_cl_fix_resize.vhd        # 第 93 行
```

见 [compile_order.txt:91-93](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/compile_order.txt#L91-L93)。这正是「Open Logic 的 `olo_fix_*` 实体复用 en_cl_fix 底层实体」的证据——两者共用同一个 `olo` 库、同一套编译顺序（单库编译策略，承接 `u1-l3`）。

而 `olo_fix_pkg` 对 en_cl_fix 的依赖，在它的库声明里写得很明白：

```vhdl
library work;
    use work.en_cl_fix_pkg.all;        -- 第 31 行
    use work.en_cl_fix_private_pkg.all;
    use work.olo_base_pkg_string.all;
    ...
```

见 [src/fix/vhdl/olo_fix_pkg.vhd:31-32](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_pkg.vhd#L31-L32)。这也是为什么没有用 `--recursive` 检出子模块时，`fix` 区域根本无法编译——缺了 `en_cl_fix_pkg`，`olo_fix_pkg` 连 `use` 都过不了。

#### 4.4.4 代码实践

**实践目标**：确认 en_cl_fix 子模块的存在与依赖关系，理解「不检出子模块则 fix 区域不可用」。

**操作步骤**：

1. 查看 `.gitmodules`，确认 `3rdParty/en_cl_fix` 是一个 git 子模块（指向 `https://github.com/open-logic/en_cl_fix.git`）。
2. 查看 `compile_order.txt` 第 56–58 行与第 91–93 行，确认 en_cl_fix 的文件被纳入全库编译顺序。
3. （可选，待本地验证）若本地已 `git submodule update --init`，打开 `3rdParty/en_cl_fix/hdl/en_cl_fix_pkg.vhd`，找到 `type FixFormat_t is record ...` 与 `cl_fix_format_from_string` 的真实定义，核对本讲对类型的描述。

**需要观察的现象 / 预期结果**：`.gitmodules` 里登记了 en_cl_fix 子模块；`compile_order.txt` 里 en_cl_fix 的两个包排在 `olo_fix_pkg` 之前。若子模块未检出，`3rdParty/en_cl_fix/hdl/` 目录为空，`olo_fix_pkg.vhd` 第 31 行的 `use work.en_cl_fix_pkg.all` 会编译失败——这正解释了 `u1-l3` 强调「必须 `--recursive` 克隆」的原因。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `olo_fix_pkg.vhd` 在 `compile_order.txt` 里必须排在 `en_cl_fix_pkg.vhd` 之后？
**答案**：因为 `olo_fix_pkg` 第 31 行 `use work.en_cl_fix_pkg.all`——它直接依赖 en_cl_fix 定义的各种类型与函数。VHDL 要求被 `use` 的包必须先编译进同一个库。

**练习 2**：`compile_order.txt` 末尾（第 91–93 行）为什么还有 en_cl_fix 的 `round/saturate/resize` 三个实体？
**答案**：en_cl_fix 不仅提供包函数，还提供了底层运算实体；Open Logic 的 `olo_fix_round` / `olo_fix_saturate` / `olo_fix_resize` 等实体在内部实例化或等价复用它们。三者排在末尾，是因为它们被前面的 `olo_fix_*` 实体依赖（或等价实现），编译顺序上靠后。

---

## 5. 综合实践

把本讲四个模块串起来：用字符串泛型实例化一个真实的 `olo_fix_resize`，并跟踪「字符串 → 类型 → 位宽」的完整链路。

**任务**：写一个 VHDL 包装（示例代码），把 8 位无符号输入 `(0,4,4)` 转成 16 位有符号输出 `(1,8,7)`，舍入用 `NonSymPos_s`（四舍五入）、饱和用 `Sat_s`（钳位），并标注每一步用到的本讲知识。

```vhdl
-- 示例代码：olo_fix_resize 的实例化片段（仅作说明，非仓库原有文件）
i_resize : entity work.olo_fix_resize
    generic map (
        -- ① 字符串泛型（本讲 4.3）：格式/舍入/饱和全用字符串
        AFmt_g      => "(0,4,4)",                       -- 输入：无符号 4整 4小 = 8 位
        ResultFmt_g => "(1,8,7)",                       -- 输出：有符号 8整 7小 = 16 位
        Round_g     => FixRound_NonSymPos_c,            -- 等价字符串 "NonSymPos_s"
        Saturate_g  => FixSaturate_Sat_c,               -- 等价字符串 "Sat_s"
        -- ② 流水线寄存（本讲 4.2）：两段都插寄存，默认 YES
        RoundReg_g  => "YES",
        SatReg_g    => "YES"
    )
    port map (
        Clk        => Clk,
        Rst        => Rst,
        In_Valid   => In_Valid,
        In_A       => In_A,        -- 宽度由 fixFmtWidthFromString("(0,4,4)") 推导为 8
        Out_Valid  => Out_Valid,
        Out_Result => Out_Result   -- 宽度由 fixFmtWidthFromString("(1,8,7)") 推导为 16
    );
```

**跟踪任务**（源码阅读型）：

1. **格式 → 位宽**：对照 [olo_fix_pkg.vhd:118-122](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_pkg.vhd#L118-L122) 的 `fixFmtWidthFromString`，确认 `In_A` 被推导为 8 位、`Out_Result` 被推导为 16 位。
2. **字符串 → 类型**：对照 [olo_fix_resize.vhd:61-64](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_resize.vhd#L61-L64)，说明 `"(0,4,4)"` 如何经 `cl_fix_format_from_string` 变成 `AFmt_c : FixFormat_t`。
3. **函数 → 实体**：对照 [olo_fix_resize.vhd:82-113](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_resize.vhd#L82-L113)，说明这个壳如何把工作交给 `olo_fix_round` + `olo_fix_saturate` 两段串联。
4. **依赖**：对照 [compile_order.txt:56-58](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/compile_order.txt#L56-L58)，说明为什么这个实例能编译，前提是 en_cl_fix 已先编进 `olo` 库。

**预期结果 / 待本地验证**：若已检出 en_cl_fix 子模块并按 `compile_order.txt` 编译，把上述实例放进一个测试台、对 `In_A` 喂几个 `(0,4,4)` 样本，应在 `Out_Result` 看到格式转换、舍入与饱和后的 `(1,8,7)` 结果；两端口的实际位宽分别为 8 与 16。若未检出子模块，编译会在 `use work.en_cl_fix_pkg.all` 处失败——这本身就是对 4.4 的验证。本环境未运行仿真，**结果待本地验证**。

---

## 6. 本讲小结

- Open Logic 的 `fix` 区域建立在第三方库 **en_cl_fix** 之上；定点格式用 `(S, I, F)` 三元组描述，配套 `FixRound_t`（舍入）与 `FixSaturate_t`（饱和）两类枚举策略。
- 一个基本定点运算自然分成 **operation → round → saturate** 三段，分段是为了能在段间按需插流水线寄存器、提升主频。
- en_cl_fix 原本只提供 VHDL **函数**，但包函数无法从 Verilog 调用；Open Logic 把它们包装成**实体**（如 `olo_fix_resize`），既打通了 Verilog，又能配置流水线寄存器。
- 实体接口一律用**字符串泛型**（如 `"(1,8,23)"`、`"Trunc_s"`）而非自定义类型，因为自定义类型在跨语言实例化时没有统一支持；字符串在实体内部经 `cl_fix_format_from_string` 等函数转回 typed 常量。
- 编译上 en_cl_fix 必须先于 `olo_fix` 编进同一个 `olo` 库（见 `compile_order.txt` 第 56–58 行），这也是「必须 `--recursive` 克隆」的根本原因。
- 配套的 Python 代码生成（`olo_fix_pkg_writer`）与协仿真（`olo_fix_cosim` 等）让 Python 成为单一真相源、并与 HDL 做位真比对——这些将在 `u8-l4`、`u8-l5` 详讲。

## 7. 下一步学习建议

- 下一讲 **`u8-l2`（olo_fix_pkg 与字符串泛型模式）** 会深入 `olo_fix_pkg.vhd` 全貌，逐个讲解它导出的字符串常量与工具函数（如 `fixImplementReg` 如何实现 `"YES"/"NO"/"AUTO"` 三档寄存判定），是本讲 4.3 的自然延伸。
- 之后 **`u8-l3`（基本定点运算）** 会逐一精读 `olo_fix_resize / round / saturate / add / mult / compare` 等实体，把本讲的「三段式 + 字符串泛型」落实到每一个运算上。
- 想验证理解，建议同时读 en_cl_fix 官方文档（`3rdParty/en_cl_fix/README.md`，需先检出子模块）与原则文档推荐的 [Webinar](https://www.youtube.com/watch?v=DajbzQurjqI&t=346)，后者直观讲解了 en_cl_fix 的核心概念。
- 如果你对「Python 生成 HDL 包」「Python 位真协仿真」更感兴趣，可以跳读 `u8-l4`、`u8-l5`，但建议先过 `u8-l3` 以掌握各运算实体的接口约定。
