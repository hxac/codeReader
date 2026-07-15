# 工具链 bug 规避与综合友好性

## 1. 本讲目标

学完本讲，你应该能够：

- 理解一个可综合定点库为何不能只写「标准 VHDL」，还要为各 EDA 工具链（仿真器 + 综合器）的已知缺陷「打补丁」。
- 在源码中识别出 **workaround（规避手段）** 的典型形态：局部重定义函数、注释说明被规避的工具与缺陷、刻意避开某些标准属性。
- 逐条讲清楚 en_cl_fix 中五处代表性规避背后的原因：`real_mod` 规避 `math_real."mod"` bug、`cl_fix_add/sub` 统一走 `signed` 规避 Vivado DSP 综合错误、自实现 `maximum/minimum`、显式 `to_string`、`cl_fix_to_integer` 的 Modelsim 特例。
- 学会用 `Changelog.md` 反查每段规避代码的「历史档案」。

本讲是**专家层**的工程化视角，依赖你已经读过 U5 的 VHDL 包头与数学函数（u5-l1、u5-l3），知道 `cl_fix_add/sub/mult`、`convert`、`resize` 这些函数在做什么。

## 2. 前置知识

### 2.1 仿真正确性 ≠ 综合正确性

同一段 VHDL，可能在仿真器里结果完全正确，但综合到 FPGA 后行为错误——因为综合器会把你的算术推断成特定的硬件原语（如 DSP slice），而某些推断路径有 bug。反过来，仿真器也可能对标准运算的实现存在缺陷。**en_cl_fix 要保证的是「综合后真实硬件」也正确**，所以代码里大量精力花在「既绕开仿真器 bug，又让综合器推断到正确的硬件」。

### 2.2 VHDL-93 合规是一条硬约束

README 明确写道：

> All RTL code is VHDL-93 compliant (for maximum compatibility with synthesis toolchains). Testbenches are VHDL-2008 compliant.

参见 [README.md:17](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/README.md#L17)。

这句话是本讲很多 workaround 的**总根源**：VHDL-93 缺少许多 VHDL-2008 才有的便利特性（如内置 `maximum/minimum`、表达式函数中的某些写法），但为了让 RTL 能被尽可能多的综合器接受，项目刻意停留在 VHDL-93。Changelog 1.1.3 记录了「为了能在 Vivado Simulator 运行而移除 VHDL-2008 语句」的历史（见 [Changelog.md:89-94](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/Changelog.md#L89-L94)）。

### 2.3 什么叫「综合友好」

综合友好（synthesis-friendly）指代码容易被综合器正确理解、推断出高质量硬件，且不踩已知工具缺陷。在 en_cl_fix 中它体现在三方面：

1. **坚持 VHDL-93**，扩大综合器兼容面。
2. **绕开具体工具的已知 bug**（Vivado、Efinity、Gowin、Quartus、Modelsim 等）。
3. **刻意选择更「直白」的实现**，例如普通截断而非 `numeric_std.resize` 的保符号截断。

### 2.4 怎么在源码里找到 workaround

en_cl_fix 的规避手段通常伴随**注释**，注释里会点名「哪个工具有什么问题」。本讲的代码实践核心技能就是：用关键词（`bug`、`workaround`、`Vivado`、`Modelsim`、`mod` 等）在 `hdl/` 下搜索这些注释，把它们和 `Changelog.md` 的条目一一对应。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [hdl/en_cl_fix_pkg.vhd](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd) | 主包。含 `real_mod`、`cl_fix_add/sub` 的 signed 统一、显式 `to_string`、`cl_fix_to_integer` 特例、`resize_sensible` 等规避点。 |
| [hdl/en_cl_fix_private_pkg.vhd](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_private_pkg.vhd) | 私有工具包。含自实现的 `maximum/minimum`（VHDL-93 合规 + Quartus 命名规避）。 |
| [Changelog.md](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/Changelog.md) | 版本档案。记录每个 workaround 进入版本的时间和针对的工具。 |
| [README.md](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/README.md) | 说明 VHDL-93 合规约束与受测仿真器列表。 |

## 4. 核心概念与源码讲解

### 4.1 工具链兼容性的两类问题

#### 4.1.1 概念说明

写一个跨多家 EDA 工具的库，会遇到两类「标准管不了」的问题：

1. **标准定义了语义，但某工具实现有 bug**：例如 `ieee.math_real."mod"` 运算符，标准定义清楚，但 Vivado、Efinity、Gowin 的实现各自出错。
2. **标准没提供该特性，不同工具支持程度不同**：例如 VHDL-93 没有内置 `maximum/minimum`，而 `'image` 属性对枚举类型的支持，部分综合工具不完整。

en_cl_fix 的应对策略统一为「**自己实现一份保底版本，绝不依赖工具的正确性**」，并在注释中写明被规避的工具与问题，方便后人维护与回归。

#### 4.1.2 核心流程

定位一处 workaround 的通用流程：

```text
1. 在 hdl/ 下搜注释关键词（bug / workaround / 工具名）
2. 读注释 → 弄清「针对哪个工具的什么缺陷」
3. 读实现 → 弄清「用什么写法绕开」
4. 查 Changelog.md → 确认进入版本的时间与动机
```

下文 4.2–4.6 用这个流程逐一拆解五处代表性规避。

### 4.2 `real_mod`：规避 `math_real."mod"` 的多工具 bug

#### 4.2.1 概念说明

`cl_fix_from_real` 把一个 `real` 数量化进定点格式 `[S,I,F]`。它不能简单地 `integer(value)` 一次性转换，因为：

- VHDL 的 `integer` 通常是 32 位，**装不下宽格式**（参见 4.2.3 的分块机制）。
- 取「当前最低若干位」需要做**取模运算** `value mod 2^N`。

而取模用的正是 `ieee.math_real` 里的 `"mod"` 运算符——它有多家工具的实现 bug。

#### 4.2.2 核心流程

数学上，定点取模应基于向下取整除法（floor division）：

\[
a \bmod b \;=\; a - b\cdot\left\lfloor \frac{a}{b} \right\rfloor
\]

en_cl_fix 不信任工具内置的 `mod`，而在函数体内部**重新定义**一个局部函数 `real_mod`，用 `floor` 手算：

```text
real_mod(a, b) = a - b * floor(a / b)
```

它随后被 `cl_fix_from_real` 在分块循环里调用，取出每一 30 位块的低位。

#### 4.2.3 源码精读

`real_mod` 的定义与注释见主包：

```vhdl
-- Several toolchains have bugs in ieee.math_real."mod" (Vivado, Efinity, Gowin EDA).
-- Therefore, this local function is used as a workaround.
function real_mod(a, b : real) return real is
begin
    return a - b * floor(a/b);
end function;
```

见 [en_cl_fix_pkg.vhd:800-806](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L800-L806)。注释直接点名三家工具：**Vivado、Efinity、Gowin EDA**。

它在 `cl_fix_from_real` 分块转换中被调用：

```vhdl
Chunk_v := std_logic_vector(to_unsigned(integer(real_mod(ASat_v, 2.0**ChunkSize_c)), ChunkSize_c));
```

见 [en_cl_fix_pkg.vhd:831-836](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L831-L836)。这里 `ChunkSize_c = 30`，把宽实数切成 30 位一块，每块取模后转 `unsigned` 拼接。

**补充：分块本身也是 workaround。** 之所以按 30 位（而非 32 位）切块，是因为 `integer(real)` 在某些仿真器上对超过 31 位的值会出错。Changelog 记录了这段历史——先支持「>31 位」再为 Modelsim 修正：

- 1.1.5：「Support numbers > 31 bits for cl_fix_from_real … 结果不精确（仅高 31 位正确）并打印告警」，见 [Changelog.md:74-80](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/Changelog.md#L74-L80)。
- 1.1.6：「Fixed "numbers > 31 bits for cl_fix_from_real" for Modelsim」，见 [Changelog.md:67-72](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/Changelog.md#L67-L72)。

而 `mod` 取模本身的规避分两次进入版本：

- 1.2.0：「Added workaround for Xilinx Vivado bug (resolution of "mod" operator)」，见 [Changelog.md:42-46](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/Changelog.md#L42-L46)。
- 2.2.1：「Added workaround for Efinity and Gowin EDA bugs in ieee.math_real."mod"」，见 [Changelog.md:7-9](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/Changelog.md#L7-L9)。

可见同一段 `real_mod` 注释里点名的三家工具，正好对应 Changelog 里两个时间点的两次修正。

#### 4.2.4 代码实践

1. **实践目标**：验证 `real_mod` 与数学取模等价，并理解负数下的行为。
2. **操作步骤**：
   - 打开 [en_cl_fix_pkg.vhd:800-806](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L800-L806)，用计算器或脚本手算 `real_mod(-1.0, 4.0) = -1 - 4*floor(-0.25) = -1 - 4*(-1) = 3`。
   - 思考：若改用「截断取整」`a - b*trunc(a/b)`，结果会变成 `-1 - 4*0 = -1`，正是某些工具 `mod` 实现的错误来源。
3. **需要观察的现象**：`floor` 版本对负数也返回非负余数（与数学取模一致），这正是 `to_unsigned` 拼位所需的。
4. **预期结果**：`real_mod` 对任意实数输入都落在 \([0, b)\) 区间（当 \(b>0\)），可安全转 `unsigned`。
5. 结果是否真的在某仿真器上不同属「待本地验证」，但 `floor` 版本的数学正确性是确定的。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `real_mod` 要定义成局部函数（在 `cl_fix_from_real` 内部），而不是放进私有包？

**参考答案**：它是为绕开特定工具 bug 而设的「保底取模」，作用域只在 `cl_fix_from_real` 内部，不必对外暴露；放成局部函数既缩小可见性，也让规避代码与使用点紧挨着，便于维护。

**练习 2**：如果某天 Vivado 修好了 `math_real."mod"`，能否直接把 `real_mod` 调用换回 `mod`？

**参考答案**：不建议。该规避同时覆盖 Efinity、Gowin 等多家工具，只要还有一家没修，去掉规避就会回归 bug。这正是「注释点名多家工具」的价值——它提醒后人这是跨工具的保底措施。

### 4.3 `cl_fix_add/sub` 统一走 `signed`：规避 Vivado DSP slice bug

#### 4.3.1 概念说明

加减法在数学上分有符号、无符号，但**补码（two's complement）位串运算下二者完全等价**——同一串 0/1，按 signed 还是 unsigned 解释，加减结果位串一致。因此 `cl_fix_add` 理论上对无符号输入用 `unsigned` 相加即可。

但 Vivado（AMD-Xilinx）综合器有一个长期 bug：当用 `numeric_std.unsigned` 做加减时，DSP slice 的 pre-adder / post-adder 推断会出错，导致**综合后硬件行为不正确**。`numeric_std.signed` 则没有已知问题。

#### 4.3.2 核心流程

```text
1. 把两个输入 convert 到全精度中间格式 mid_fmt
2. 无视原始符号性，统一转成 signed 再做 +/- （补码等价，结果位串相同）
3. resize 收敛到 result_fmt
```

关键点：因为补码等价，统一走 `signed` **不改变正确结果**，却让综合器走「无 bug 的 signed 推断路径」。

#### 4.3.3 源码精读

`cl_fix_add` 的注释与实现：

```vhdl
-- Signed/unsigned addition/subtraction are identical when using two's complement.
-- However, a long-standing Vivado bug causes incorrect post-synthesis behavior in DSP
-- slices (pre-add or post-add) if numeric_std.unsigned is used. There are no known issues
-- for numeric_std.signed, so we always use that.
mid_v := std_logic_vector(signed(a_v) + signed(b_v));
```

见 [en_cl_fix_pkg.vhd:1162-1169](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L1162-L1169)。

`cl_fix_sub` 完全对称，注释逐字相同，只是把 `+` 换成 `-`：

见 [en_cl_fix_pkg.vhd:1187-1194](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L1187-L1194)。

Changelog 2.0.0 记录了这次修正：

> Fixed cl_fix_add / cl_fix_sub bug that sometimes prevented correct inference of AMD-Xilinx DSP slice pre-adders / post-adders.

见 [Changelog.md:32-33](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/Changelog.md#L32-L33)。

#### 4.3.4 代码实践

1. **实践目标**：验证「补码等价」如何让统一 `signed` 不影响结果。
2. **操作步骤**：
   - 取两个无符号 4 位数 `a=1011`(11)、`b=0011`(3)。
   - 按 unsigned 相加：`11+3=14=1110`。
   - 把它们当 4 位 signed 解释（`1011` = -5，`0011` = 3），相加得 `-2`，4 位 signed 补码也是 `1110`。
3. **需要观察的现象**：两种解释下，**结果位串完全相同**（都是 `1110`），只是数值含义不同。
4. **预期结果**：位串相同 ⇒ `cl_fix_resize` 后续按 `mid_fmt` 的真实符号性解释，结果正确。这正是「统一 signed 安全」的数学依据。
5. 仅 4 位时不溢出；若担心溢出，注意 `mid_fmt` 已由 `cl_fix_add_fmt` 预留了进位位（回顾 u3-l1），故中间格式下不会溢出。

#### 4.3.5 小练习与答案

**练习 1**：既然补码等价，为什么不干脆只用 `signed` 类型、取消 unsigned 分支？

**参考答案**：`cl_fix_mult` 必须区分符号性（无符号×无符号的乘积位宽与符号位和无符号×有符号不同，见 u5-l3），加减法只是「恰好可以」统一。库整体仍需保留符号性信息用于格式预测与乘法；这里只是在**加减运算这一步**借补码等价绕开 Vivado bug。

**练习 2**：这个 bug 为什么是「post-synthesis」才暴露、仿真却查不出来？

**参考答案**：仿真器执行的是 RTL 语义（`unsigned` 相加本身正确），bug 出在综合器把 `unsigned` 加法**推断到 DSP slice pre/post-adder** 的那一步——只有真正综合并上板才会显现。这类「仿真过、硬件错」的缺陷正是最危险的，必须靠规避写法预防。

### 4.4 自实现 `maximum/minimum`：VHDL-93 合规与 Quartus 命名规避

#### 4.4.1 概念说明

格式预测函数（`union`、`cl_fix_add_fmt` 等）大量用到「取两整数之大/小」。但 **VHDL-93 没有内置 `maximum/minimum`**（它们是 VHDL-2008 才加入的预定义运算符）。由于 RTL 锁定 VHDL-93（见 2.2），项目必须自己实现。

此外，Quartus 曾对 `min/max` 这样的名字有命名冲突问题，项目为此把函数命名为 `maximum/minimum` 而非更短的 `max/min`。

#### 4.4.2 核心流程

```text
maximum(a,b) = if a>=b then a else b
minimum(a,b) = if a<=b then a else b
```

这两个函数定义在私有包 `en_cl_fix_private_pkg` 里，主包通过 `use work.en_cl_fix_private_pkg.all` 引入（见 [en_cl_fix_pkg.vhd:28-29](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L28-L29)）。

#### 4.4.3 源码精读

私有包里的实现：

```vhdl
function maximum(a, b : integer) return integer is
begin
    if a >= b then
        return a;
    else
        return b;
    end if;
end;

function minimum(a, b : integer) return integer is
begin
    if a <= b then
        return a;
    else
        return b;
    end if;
end;
```

见 [en_cl_fix_private_pkg.vhd:96-112](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_private_pkg.vhd#L96-L112)。

它在格式预测里被频繁使用，例如 `union` 对 `S/I/F` 各取最大：

```vhdl
function union(aFmt, bFmt : FixFormat_t) return FixFormat_t is
begin
    return (
        S => maximum(aFmt.S, bFmt.S),
        I => maximum(aFmt.I, bFmt.I),
        F => maximum(aFmt.F, bFmt.F)
    );
end function;
```

见 [en_cl_fix_pkg.vhd:353-360](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L353-L360)。`union` 又是 `cl_fix_addsub_fmt` 等的公共积木（回顾 u3-l1、u2-l4）。

Quartus 命名规避的历史见 Changelog 2.1.1：

> Renamed min (and max) to work around name conflict issue seen only in Quartus.

见 [Changelog.md:15-17](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/Changelog.md#L15-L17)。即现在的 `maximum/minimum` 是从更早的 `max/min` 改名而来，正是为了绕开 Quartus。

#### 4.4.4 代码实践

1. **实践目标**：理解「为何不能直接用 VHDL-2008 的 `maximum`」。
2. **操作步骤**：
   - 在主包搜索 `maximum(` 的所有出现，统计调用次数（提示：`union`、`cl_fix_add_fmt`、`cl_fix_sub_fmt`、`cl_fix_mult_fmt` 等都用）。
   - 假设把私有包的 `maximum` 删掉、改用 VHDL-2008 内置 `maximum`，思考会发生什么。
3. **需要观察的现象**：这些调用大多出现在**常量声明 / 函数返回值**等位置。
4. **预期结果**：在 VHDL-93 综合器上会编译失败（不认识内置 `maximum`）；即使工具支持 2008，也违背了「RTL 锁 93」的兼容性承诺。故自实现是必须的。
5. 是否所有目标综合器都缺内置版本属「待本地验证」，但项目选择自实现以统一行为。

#### 4.4.5 小练习与答案

**练习 1**：`maximum/minimum` 放在私有包而非主包，有什么好处？

**参考答案**：它们是与定点语义无关的通用整数工具，放进私有包可以：① 不污染主包公共 API；② 被主包 `use` 后像本地函数一样使用；③ 清晰表达「这是内部零件」的定位（回顾 u5-l4 私有包的定位）。

**练习 2**：为什么 Quartus 的命名冲突会专门针对 `min/max`？

**参考答案**：`min/max` 是极常见的短名，容易与 Quartus 自带库或用户上下文中的同名符号冲突（短名碰撞概率高）。改名成更长的 `maximum/minimum` 降低了冲突可能——这是典型的「用命名隔离规避工具缺陷」。

### 4.5 显式 `to_string`：规避综合工具不支持枚举 `'image`

#### 4.5.1 概念说明

VHDL 的 `'image` 属性可以把标量值转成字符串，例如 `FixRound_t'image(Trunc_s)` 应返回 `"Trunc_s"`。但**部分综合工具对枚举类型的 `'image` 支持不完整**，因此 en_cl_fix 对两个枚举类型 `FixRound_t`、`FixSaturate_t` 手写 `case` 显式实现 `to_string`。

注意一个重要区分：**整数类型（`integer`、`natural`）的 `'image` 是安全可用的**，只有枚举 `'image` 不可靠。

#### 4.5.2 核心流程

```text
to_string(枚举值):
    case 值 of
        每个枚举字面量 => return 对应字符串
        others => assert Failure（防止新增枚举值后漏处理）
```

#### 4.5.3 源码精读

`to_string(FixRound_t)` 显式 `case`：

```vhdl
function to_string(rnd : FixRound_t) return string is
begin
    -- Some synthesis tools do not support FixRound_t'image(), so we implement explicitly.
    case rnd is
        when Trunc_s     => return "Trunc_s";
        when NonSymPos_s => return "NonSymPos_s";
        ...
        when others => report "to_string(FixRound_t) : Unsupported input." severity Failure;
    end case;
    return "";
end;
```

见 [en_cl_fix_pkg.vhd:700-714](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L700-L714)。`to_string(FixSaturate_t)` 结构完全相同，见 [en_cl_fix_pkg.vhd:716-727](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L716-L727)。两处注释都写明原因：「Some synthesis tools do not support …'image(), so we implement explicitly」。

对比 `to_string(FixFormat_t)`，它用的是**整数 `'image`**，因安全而无需手写：

```vhdl
function to_string(fmt : FixFormat_t) return string is
begin
    return "(" & natural'image(fmt.S) & "," & integer'image(fmt.I) & "," & integer'image(fmt.F) & ")";
end;
```

见 [en_cl_fix_pkg.vhd:695-698](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L695-L698)。这里 `fmt.S/I/F` 都是整数类型，`'image` 可靠。

`others => report ... severity Failure` 还有一层工程价值：若未来给枚举新增一个值而忘了更新 `to_string`，运行时会立即报致命错误，防止静默错误（同样的「防新增遗漏」模式也出现在 `cl_fix_recommended_pipelining`，见 [en_cl_fix_pkg.vhd:1059-1062](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L1059-L1062) 与 [en_cl_fix_pkg.vhd:1086-1089](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L1086-L1089)）。

#### 4.5.4 代码实践

1. **实践目标**：体会「枚举 `'image` 不可靠、整数 `'image` 可靠」的区分。
2. **操作步骤**：
   - 对比 [en_cl_fix_pkg.vhd:695-698](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L695-L698)（用 `'image`）与 [en_cl_fix_pkg.vhd:700-714](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L700-L714)（手写 case）。
   - 假设在 `FixRound_t` 里新增一个 `Floor_s` 舍入模式，思考哪些地方会被 `assert` 兜住。
3. **需要观察的现象**：手写版用 `others` 分支兜底。
4. **预期结果**：新增枚举值后，`to_string`、`cl_fix_recommended_pipelining` 等所有 `case` 都会因 `others` 触发致命断言，强制开发者补全实现——这是「防漏」设计。
5. `'image` 在你本地综合器是否真不支持属「待本地验证」，但项目按保守策略一律手写枚举转换。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `to_string(FixFormat_t)` 敢用 `'image`，`to_string(FixRound_t)` 却不敢？

**参考答案**：`FixFormat_t` 的三个字段是 `natural`/`integer`，属于整数类型，其 `'image` 在各综合工具中支持良好；`FixRound_t` 是自定义枚举类型，枚举 `'image` 的综合支持参差不齐，故手写。

**练习 2**：手写 `case` 比 `'image` 多了一个好处，是什么？

**参考答案**：可以在 `others` 分支放 `severity Failure` 的断言，充当「枚举值扩展后的回归守卫」——一旦新增模式而漏改，立即在仿真期报致命错误，而不是默默返回错误字符串。

### 4.6 `cl_fix_to_integer` 的特例：规避 Modelsim 警告

#### 4.6.1 概念说明

`cl_fix_to_integer` 把定点位串转成 `integer`。Modelsim 仿真器有一个怪癖：对 **1 位 `signed`** 或**任意 0 位**输入调用 `numeric_std.to_integer()` 会抛出警告。这些警告并非错误，但会污染仿真日志、干扰问题定位。

#### 4.6.2 核心流程

```text
若位宽 == 0:        直接返回 0（0 位无值可言）
若 1 位且 signed:   手动解释（'1' -> -1，'0' -> 0）
否则:               正常用 to_integer(signed/unsigned)
```

#### 4.6.3 源码精读

```vhdl
-- Modelsim throws warnings if to_integer() is called on 1-bit signed or any 0-bit input.
-- We handle these special cases explicitly to avoid the warnings.
if cl_fix_width(aFmt) = 0 then
    return 0;
elsif aFmt.S = 1 and cl_fix_width(aFmt) = 1 then
    if a_c(0) = '1' then
        return -1;
    else
        return 0;
    end if;
end if;

-- Normal cases
if aFmt.S = 1 then
    return to_integer(signed(a_c));
else
    return to_integer(unsigned(a_c));
end if;
```

见 [en_cl_fix_pkg.vhd:885-908](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L885-L908)。

注意 1 位 signed 的语义：补码下 1 位 signed 只能表示 `0` 和 `-1`（无 `+1`），所以 `'1'` 解释为 `-1`，注释里也点明「-1 in the integer representation is -2**aFmt.I in fixed point」（见 [en_cl_fix_pkg.vhd:894-895](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L894-L895)）。

这类规避「只防警告、不改数值」——因为正确路径（normal cases）的结果与特例完全一致，特例只是换了条不触发警告的表达路径。

#### 4.6.4 代码实践

1. **实践目标**：理解「绕开警告」类规避与「绕开 bug」类规避的区别。
2. **操作步骤**：
   - 阅读 [en_cl_fix_pkg.vhd:889-900](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L889-L900)。
   - 思考：若删掉这两个特例分支、统一走 `to_integer(signed(...))`，数值结果会变吗？
3. **需要观察的现象**：特例分支返回的值与「直接 to_integer」在标准语义下相同。
4. **预期结果**：数值不变，只是 Modelsim 日志里少了警告。这属于「工程整洁性」规避，而非正确性规避。
5. 在 GHDL/NVC 上是否也有同样警告属「待本地验证」，但特例对所有工具都安全。

#### 4.6.5 小练习与答案

**练习 1**：1 位 signed 为何 `'1'` 是 `-1` 而不是 `+1`？

**参考答案**：补码表示下，最高位（也是唯一位）是符号位，权重为 \(-2^{I}\)。1 位 signed 即 \(I=0\)，权重为 \(-2^0=-1\)，故 `'1'` 表示 \(-1\)、`'0'` 表示 \(0\)，不存在 \(+1\)。

**练习 2**：这类「防警告」规避有没有可能也掩盖真实问题？

**参考答案**：有一定风险——若 `to_integer` 的警告本应提示某种异常（如位宽退化），特例会把它静默掉。但本例中 0 位/1 位 signed 是格式预测中合法的退化情形（回顾 u3 的边界情况），其语义明确，故特例化是安全的。

### 4.7 （延伸）`resize_sensible`：综合友好的「普通截断」

这虽不是针对某个工具 bug，却是「综合友好」设计的典型，值得一并了解。`numeric_std.resize` 在截断有符号数时会**保留符号位**，这在定点收敛里往往不是想要的行为；`resize_sensible` 在截断时改用**普通截断**（直接取低位），并注释说明理由：

```vhdl
-- Truncation: Just do plain truncation.
-- This is usually more sensible than numeric_std.resize, which preserves the sign bit.
v := a_c(n-1 downto 0);
```

见 [en_cl_fix_pkg.vhd:313-327](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L313-L327)。它被 `convert`、`cl_fix_mult` 等多处使用，确保截断语义符合定点预期且实现直白、易综合。

## 5. 综合实践

**任务**：在源码中搜索并汇总至少三处「针对工具链 bug / 缺陷」的代码与注释，建立一份「规避档案表」。

### 操作步骤

1. 在 `hdl/` 目录下用关键词搜索规避注释。建议关键词：`bug`、`workaround`、`Vivado`、`Efinity`、`Gowin`、`Quartus`、`Modelsim`、`mod`、`'image`、`DSP`。
2. 对每一处，记录：
   - 文件与行号（给出永久链接）。
   - 被规避的工具与缺陷（从注释和 Changelog 推断）。
   - 规避写法（用了什么替代实现）。
3. 把每条与 `Changelog.md` 对应的版本条目关联起来。

### 预期产出（档案表样例）

| 规避点 | 文件:行 | 针对工具/缺陷 | 规避写法 | Changelog |
| --- | --- | --- | --- | --- |
| `real_mod` | en_cl_fix_pkg.vhd:800-806 | Vivado / Efinity / Gowin 的 `math_real."mod"` | 局部函数 `a-b*floor(a/b)` | 1.2.0、2.2.1 |
| signed 加减 | en_cl_fix_pkg.vhd:1162-1169 | Vivado DSP pre/post-adder 推断 unsigned 出错 | 统一 `signed` 相加 | 2.0.0 |
| 自实现 max/min | en_cl_fix_private_pkg.vhd:96-112 | VHDL-93 无内置；Quartus `min/max` 命名冲突 | 手写 `if`；改名 `maximum/minimum` | 2.1.1 |
| 显式 `to_string` | en_cl_fix_pkg.vhd:700-727 | 部分综合器不支持枚举 `'image` | 手写 `case` | （隐性，随 2.0 重构） |
| `to_integer` 特例 | en_cl_fix_pkg.vhd:889-900 | Modelsim 对 1 位 signed / 0 位输入告警 | 特例分支直接返回 | （隐性） |

### 观察与反思

- 完成表格后，回答：哪些规避是「保正确性」（去掉会出错），哪些是「保整洁性」（去掉只是多警告/少兼容）？
- 思考：如果项目未来放宽到 VHDL-2008，哪些规避可以移除（如 `maximum/minimum`），哪些必须保留（如 `real_mod`、signed 加减）？为什么？

## 6. 本讲小结

- en_cl_fix 之所以写很多「不像标准 VHDL」的代码，根本原因是**坚持 VHDL-93 + 跨多家 EDA 工具兼容**，必须为仿真器与综合器的已知缺陷打补丁。
- `real_mod`（[en_cl_fix_pkg.vhd:800-806](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L800-L806)）用 `a-b*floor(a/b)` 规避 Vivado / Efinity / Gowin 的 `math_real."mod"` bug，分块 `ChunkSize_c=30` 还顺带规避了 `integer()` 的 >31 位限制。
- `cl_fix_add/sub` 借补码等价**统一走 `signed`**（[en_cl_fix_pkg.vhd:1162-1194](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L1162-L1194)），规避 Vivado DSP slice 对 `unsigned` 的综合错误，结果位串不变。
- 自实现 `maximum/minimum`（[en_cl_fix_private_pkg.vhd:96-112](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_private_pkg.vhd#L96-L112)）兼顾 VHDL-93 合规与 Quartus 命名冲突（Changelog 2.1.1）。
- 显式 `to_string`（[en_cl_fix_pkg.vhd:700-727](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L700-L727)）规避综合器对枚举 `'image` 支持不全，并用 `others=>Failure` 守护枚举扩展。
- `cl_fix_to_integer` 特例（[en_cl_fix_pkg.vhd:889-900](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L889-L900)）属「保整洁性」规避，消除 Modelsim 对退化位宽的警告。
- 每条规避都应在 `Changelog.md` 里找到对应的「进入版本」，注释点名工具名是这类代码的关键维护线索。

## 7. 下一步学习建议

- **本系列收尾**：结合 u8-l1（MATLAB 薄封装）与本讲，你已经从「算法语义（U2–U5）→ 流水线组件（U6）→ 协同验证（U7）→ 工具链工程化（U8）」走完了 en_cl_fix 的完整知识地图。建议重读 README 的「Running Tests」章节，亲手跑一次 `sim/run.py`，观察这些规避代码在真实仿真中是否如本讲所述。
- **深入源码**：通读 [hdl/en_cl_fix_pkg.vhd](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd) 全文，把本讲没覆盖的注释（如 `convert` 的 `offset_c` 类型护栏、`cl_fix_recommended_pipelining` 的 `others` 断言）也纳入你的「规避/防御性编程」笔记。
- **横向对比**：若你接触过 `psi_fix` 等同类定点库，可对比它们如何处理同样的工具链缺陷，体会 en_cl_fix「保底自实现 + 注释点名工具」风格的取舍。
- **动手扩展**：仿照 u8-l3 的测试思路，写一个小测试，针对 `real_mod` 的负数输入与 `to_string` 的全枚举值做断言，验证本讲所述的「数学正确性」与「防漏守卫」。
