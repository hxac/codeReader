# 字符串解析工具与 generic 传参

## 1. 本讲目标

本讲是专家层（Unit 7）的一讲，聚焦 `en_cl_fix_pkg.vhd` 中一条常被忽视、却贯穿真实工程使用场景的「字符串 ↔ 定点格式」转换链路。读者学完后应该能够：

1. 说清 **为什么** 必须用字符串把定点格式传给仿真——理解 VHDL `generic` 在 Modelsim 下的类型限制这一工程背景。
2. 读懂 `cl_fix_format_from_string` 的「游标推进式」解析主流程，并能手动跟踪它对 `"(true,3,4)"` 的解析过程。
3. 读懂三个**包体私有**的底层原语：`toLower`（大小写归一化）、`string_find_next_match`（找下一个分隔符）、`string_parse_int`/`string_parse_boolean`（字段值解析）。
4. 识别贯穿整条解析链的三类 `assert` 防御式检查，并理解「找不到就返回 -1、再用断言拦截」这一 VHDL 里常见的健壮性写法。
5. 在 testbench 中亲手写一段往返测试，验证 `cl_fix_string_from_format` 与 `cl_fix_format_from_string` 的互逆性。

## 2. 前置知识

本讲默认读者已经掌握（见 u1-l2、u1-l3）：

- **定点格式三元组 `[S, I, F]`**：`Signed`(布尔)、`IntBits`(整数)、`FracBits`(整数)，三者唯一决定一个定点格式，总位宽 `W = S + I + F`。
- **`FixFormat_t`** 是一个 VHDL `record`，含三个字段 `Signed`/`IntBits`/`FracBits`。
- **`cl_fix_string_from_format`** 把格式序列化为形如 `"(true,3,4)"` 的字符串（u1-l3 已介绍其输出形态）。

本讲会补充两个 VHDL 语言层面的概念：

- **`generic`（类属）**：VHDL 实体在**实例化时**由上层传入的编译期常量，常用于把位宽、格式、配置参数传给 testbench 或 IP。关键约束是：**Modelsim 以及大多数 VHDL 工具流只支持 `integer`、`string`、`boolean`（及其子类型）类型的 generic，不支持 `record` 类型**。因此像 `FixFormat_t` 这样的复合记录**不能直接作为 generic 传入**，必须先「拍扁」成字符串。
- **`'image` 属性**：VHDL 预定义属性，把一个标量值转成它的字符串表示，例如 `boolean'image(true)` 得到 `"true"`、`integer'image(-2)` 得到 `"-2"`。它是序列化方向唯一的工具。
- **VHDL 没有内建的 `toLower`**：标准库不提供大小写转换，需要手写。

> 提示：本讲引用的所有源码都在同一个文件 `vhdl/src/en_cl_fix_pkg.vhd` 中。其中 `cl_fix_string_from_format` 与 `cl_fix_format_from_string` 是**对外公开**的（声明在 package 声明区，第 231、241 行）；而 `toLower`、`string_find_next_match`、`string_parse_boolean`、`string_parse_int` 等都是**包体私有**的（只存在于 package body，第 1002 行之后，声明区里找不到），它们是公开函数背后的实现细节。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `vhdl/src/en_cl_fix_pkg.vhd` | 本讲全部源码都在此文件。包含两个公开的转换函数和六个私有解析原语。 |
| `vhdl/tb/en_cl_fix_pkg_tb.vhd` | testbench。**注意：它没有针对字符串转换函数的测试**，本讲的代码实践需要读者自行补写。 |

本讲涉及的源码点及其行号一览（均在 `en_cl_fix_pkg.vhd`）：

| 函数 | 行号 | 可见性 |
| --- | --- | --- |
| `cl_fix_string_from_format`（声明） | 231–232 | 公开 |
| `cl_fix_format_from_string`（声明 + docstring） | 234–242 | 公开 |
| `toLower`（字符版） | 1070–1103 | 包体私有 |
| `toLower`（字符串版） | 1107–1114 | 包体私有 |
| `string_find_next_match`（字符版） | 1118–1138 | 包体私有 |
| `string_find_next_match`（字符串版） | 1142–1168 | 包体私有 |
| `string_parse_boolean` | 1172–1197 | 包体私有 |
| `string_int_from_char` / `string_char_is_numeric` | 1201–1226 | 包体私有 |
| `string_parse_int` | 1230–1267 | 包体私有 |
| `cl_fix_string_from_format`（实现） | 1333–1337 | 公开 |
| `cl_fix_format_from_string`（实现） | 1341–1368 | 公开 |

## 4. 核心概念与源码讲解

### 4.1 工程背景：generic 限制与「格式 ↔ 字符串」对称接口

#### 4.1.1 概念说明

在真实 FPGA 工程里，我们经常需要把一个定点格式**作为参数**传给某个实体或 testbench。例如，写一个可配置的滤波器 testbench，希望在命令行用不同格式实例化它。VHDL 的标准做法是用 `generic`：

```vhdl
entity my_filter_tb is
    generic (
        InFmt  : ???;   -- 想传一个 FixFormat_t
        OutFmt : ???
    );
end entity;
```

问题在于：**Modelsim 只支持 `integer`、`string`、`boolean` 类型的 generic，不支持 `record`**。`FixFormat_t` 是 record，所以上面的写法在工具里跑不通。

源码 docstring 把这个工程背景写得非常清楚——这正是 `cl_fix_format_from_string` 存在的理由：

> Formats cannot be passed as `FixFormat_t` since Modelsim only supports Generics of types integer, string and boolean. Using this function, the formats can be passed as string and then converted to `FixFormat_t`.

解决思路是一个**对称的序列化/反序列化对**：

- **序列化** `cl_fix_string_from_format`：`FixFormat_t → string`，把记录拍扁成 `"(true,3,4)"`，可以放进 string generic。
- **反序列化** `cl_fix_format_from_string`：`string → FixFormat_t`，在实体内部把字符串还原成记录。

两者互为逆运算，构成一条完整的「过 generic」通道。这也是本讲标题「字符串解析工具与 generic 传参」的由来。

#### 4.1.2 核心流程

序列化方向非常简单，靠 VHDL 预定义的 `'image` 属性逐字段转字符串，再拼接：

```
"(" & boolean'image(Signed) & "," & integer'image(IntBits) & "," & integer'image(FracBits) & ")"
```

反序列化方向则需要一个「解析器」，本讲后续四节都在讲它。整体流程如下：

```
字符串 "(true,3,4)"
        │
        │  cl_fix_format_from_string  （4.2 主流程）
        ▼
   找 '(' → 找 ',' → 找 ',' → 找 ')'      （游标推进）
        │
        │  对每一段调用字段解析器（4.3）
        ▼
   string_parse_boolean("true...")  → Signed
   string_parse_int("3...")         → IntBits
   string_parse_int("4...")         → FracBits
        │
        │  字段解析器内部依赖底层原语（4.4）
        ▼
   toLower / string_find_next_match / string_int_from_char
        │
        ▼
   FixFormat_t(Signed=>true, IntBits=>3, FracBits=>4)
```

#### 4.1.3 源码精读

先看公开声明区的 docstring，它同时解释了两个函数的用途和 generic 背景：

[vhdl/src/en_cl_fix_pkg.vhd:L234-L242](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L234-L242) — `cl_fix_format_from_string` 的声明与 docstring，明确说明「格式不能以 `FixFormat_t` 传 generic，故用字符串传递再转换」。

序列化的实现只有一行，靠 `'image` 属性拼接：

[vhdl/src/en_cl_fix_pkg.vhd:L1333-L1337](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1333-L1337) — `cl_fix_string_from_format` 实现，用 `boolean'image` 与 `integer'image` 拼出 `"(true,3,4)"` 形态的字符串。

> 一个值得注意的细节：`boolean'image(true)` 在 VHDL 里产生**小写**的 `"true"`/`"false"`。这一点很重要，它决定了反序列化端可以假定一个「规范小写形态」，但源码并没有偷懒依赖它——反序列化端依然做了大小写归一化（见 4.4），从而对 `"True"`、`"TRUE"` 等用户手写输入也兼容。

#### 4.1.4 代码实践

**实践目标**：亲手验证序列化方向的输出形态，确认它与 docstring 承诺的 `"(true,3,4)"` 一致。

**操作步骤**：

1. 打开 testbench `vhdl/tb/en_cl_fix_pkg_tb.vhd`，找到 `p_control` 进程开头的 `cl_fix_width` 测试段（第 97–105 行）。
2. 在该段末尾临时插入一行（利用 testbench 内部已定义的 `print` 过程，第 83–88 行）：

```vhdl
-- 示例代码：临时插入到 p_control 中，验证后请删除
print("serialized = " & cl_fix_string_from_format((true, 3, 4)));
print("serialized = " & cl_fix_string_from_format((false, -2, 3)));
```

3. 按 u2-l2 介绍的方式，用 `sim/sim.tcl`（`vcom -2008` → `vsim` → `run -all`）跑一次仿真。

**需要观察的现象**：transcript 中应出现：

```
serialized = (true,3,4)
serialized = (false,-2,3)
```

**预期结果**：输出与 docstring 描述完全一致——小写 `true`/`false`、逗号无空格、负整数位原样保留。**若工具未安装无法运行，本步骤标记为「待本地验证」。**

#### 4.1.5 小练习与答案

**练习 1**：为什么不能直接把 `FixFormat_t` 作为 generic 传入实体？

> **参考答案**：因为 `FixFormat_t` 是 `record` 类型，而 Modelsim 等主流 VHDL 工具的 generic 只接受 `integer`/`string`/`boolean`，不支持 record。所以必须先序列化成字符串再传入，进入实体后再用 `cl_fix_format_from_string` 还原。

**练习 2**：`cl_fix_string_from_format((true, 3, 4))` 的输出里，`true` 为什么是小写？

> **参考答案**：因为它内部用的是 VHDL 预定义属性 `boolean'image(true)`，而 `boolean'image` 对布尔值固定产生小写的 `"true"`/`"false"`。

---

### 4.2 cl_fix_format_from_string：游标式解析主流程

#### 4.2.1 概念说明

`cl_fix_format_from_string` 是反序列化方向的**唯一公开入口**，也是整条解析链的「调度者」。它本身不做底层的字符比对，而是用一个整数游标 `Index_v` 在字符串上**逐步推进**：每推进到一个结构分隔符（`'('`、`','`、`','`、`')'`），就把夹在分隔符之间的那一段交给字段解析器（`string_parse_boolean` 或 `string_parse_int`）。

这种写法的关键设计是「**贪婪字段解析 + 结构断言**」：

- 字段解析器（如 `string_parse_int`）是**贪婪**的——它从给定起点一直读到第一个非数字字符为止，自然停在下一个逗号上。所以主流程**不需要**先算出每个字段的结束位置，只要把起点（= 上一个分隔符位置 +1）喂进去即可。
- 主流程在每一步都用 `assert Index_v > 0` 检查「上一个 `string_find_next_match` 是否真的找到了分隔符」。因为 `string_find_next_match` 找不到时会返回 `-1`（见 4.4），用 `Index_v > 0` 一判就能拦截所有「格式残缺」的输入。

#### 4.2.2 核心流程

主流程的伪代码（箭头表示游标 `Index_v` 的推进）：

```
Index := Str'low
Index := find '(' at Index              -- 找左括号
assert Index > 0                         -- 否则报「缺 '('」
Signed  := parse_boolean(Str, Index+1)   -- 解析 Signed（贪婪读到逗号）

Index := find ',' at Index+1             -- 找第一个逗号
assert Index > 0                         -- 否则报「缺 IsSigned/IntBits 之间的逗号」
IntBits := parse_int(Str, Index+1)       -- 解析 IntBits

Index := find ',' at Index+1             -- 找第二个逗号
assert Index > 0                         -- 否则报「缺 IntBits/FracBits 之间的逗号」
FracBits:= parse_int(Str, Index+1)       -- 解析 FracBits

Index := find ')' at Index+1             -- 找右括号
assert Index > 0                         -- 否则报「缺 ')'」
return FixFormat_t(Signed, IntBits, FracBits)
```

用 `"(true,3,4)"` 走一遍（字符串下标从 1 起）：

| 步骤 | 操作 | Index 结果 | 说明 |
| --- | --- | --- | --- |
| 0 | `Index := Str'low` | 1 | 起点 |
| 1 | `find '(' at 1` | 1 | 第 1 个字符就是 `(` |
| 2 | `parse_boolean(Str, 2)` | — | 从下标 2 (`t`) 起贪婪读，返回 `true` |
| 3 | `find ',' at 2` | 6 | 第 6 个字符是 `,`（`true` 占 2–5） |
| 4 | `parse_int(Str, 7)` | — | 从下标 7 (`3`) 起贪婪读，返回 `3` |
| 5 | `find ',' at 7` | 8 | 第 8 个字符是 `,` |
| 6 | `parse_int(Str, 9)` | — | 从下标 9 (`4`) 起贪婪读，返回 `4` |
| 7 | `find ')' at 9` | 10 | 第 10 个字符是 `)` |

最终得到 `FixFormat_t(true, 3, 4)`，位宽 `1+3+4 = 8`。

#### 4.2.3 源码精读

[vhdl/src/en_cl_fix_pkg.vhd:L1341-L1368](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1341-L1368) — `cl_fix_format_from_string` 实现。注意四段「`string_find_next_match` 找分隔符 → `assert Index_v > 0` → `string_parse_*` 解析字段」的重复结构。

这里有几处值得精读的细节：

1. **游标初始化为 `Str'low`**（第 1347 行），而不是硬编码 `1`。这是良好的 VHDL 习惯——字符串不一定从下标 1 开始，用 `'low` 才稳。
2. **四个 `assert` 报错消息分别指出缺哪个分隔符**（第 1349–1366 行），定位非常精确。
3. **一处源码观察（非 bug，但读代码时会撞到）**：这四个 `assert` 的 `report` 文本都以 `cl_fix_string_from_format:` 开头——这是**反向**（序列化）函数的名字，而不是当前的 `cl_fix_format_from_string`。这显然是从序列化函数复制过来时漏改的命名不一致。它不影响运行（只是报错字符串里的函数名），但读代码时不要被它误导以为自己看错了函数。类似地，`string_parse_boolean` 里的范围检查 `report` 写的是 `en_cl_string_parse_boolean:`（第 1180 行），多了一个 `en_cl_` 前缀，也是同类小笔误。

#### 4.2.4 代码实践

**实践目标**：验证 `cl_fix_format_from_string` 的解析结果，并用「往返」（round-trip）方式确认它与 `cl_fix_string_from_format` 互逆。

**操作步骤**：

1. 仍在上面的 `p_control` 进程里，紧接 4.1.4 插入的行之后，再加：

```vhdl
-- 示例代码：往返一致性测试
-- 解析 "(true,3,4)" 得到 FixFormat_t，再用 width 验证，再序列化回去比对
CheckInt(8, cl_fix_width(cl_fix_format_from_string("(true,3,4)")),
         "format_from_string roundtrip: width mismatch");
CheckBoolean(true, cl_fix_format_from_string("(TRUE,-2,3)").Signed,
             "format_from_string: case-insensitive Signed");
-- 往返：序列化 → 解析 → 再序列化，应得到相同字符串
print("roundtrip = " & cl_fix_string_from_format(
         cl_fix_format_from_string("(true,3,4)")));
```

> 说明：`CheckInt` 与 `CheckBoolean` 是 testbench 内部定义的断言式校验过程（第 47–53、74–80 行），失败时会打印 `###ERROR###` 前缀（u2-l2 已介绍）。

2. 重新运行 `sim/sim.tcl`。

**需要观察的现象**：

- transcript 出现 `roundtrip = (true,3,4)`，与原串逐字符相同。
- 不出现新的 `###ERROR###` 行。

**预期结果**：`cl_fix_format_from_string("(true,3,4)")` 解析出的格式位宽为 8；`"(TRUE,-2,3)"` 因大小写归一化也能正确识别 `Signed = true`；往返序列化结果与输入一致。**若工具未安装无法运行，本步骤标记为「待本地验证」。**

#### 4.2.5 小练习与答案

**练习 1**：主流程为什么不需要先算出每个字段（如 `IntBits`）的结束位置，再交给解析器？

> **参考答案**：因为 `string_parse_int` 是贪婪的——它从起点一直读到第一个非数字字符（即逗号）就停。所以主流程只需把「上一个分隔符 +1」作为起点传进去，解析器自己会停在下一个分隔符处。

**练习 2**：若输入字符串是 `"true,3,4)"`（漏了左括号），主流程会怎样？

> **参考答案**：第 1348 行 `string_find_next_match(Str, '(', ...)` 找不到 `(`，返回 `-1`；第 1349–1351 行的 `assert Index_v > 0` 命中，以 `severity error` 报出 `"cl_fix_string_from_format: wrong Format, missing '('"`。注意：`severity error` 不会中止仿真，所以会继续往下跑（但后续 `Index_v+1 = 0` 越界访问可能再触发别的断言）。

---

### 4.3 string_parse_int 与 string_parse_boolean：字段值解析器

#### 4.3.1 概念说明

主流程调用的两个字段解析器，负责把一段字符流翻译成 `FixFormat_t` 的字段值：

- **`string_parse_int(Str, StartIdx)`**：从 `StartIdx` 起解析一个**整数**，用于 `IntBits` 和 `FracBits`。它能处理前导空格、可选负号、任意长数字串。这一能力对定点格式至关重要——`IntBits`/`FracBits` 都可以为负（u1-l2），例如 `(true,-2,3)`，必须正确解析出 `-2`。
- **`string_parse_boolean(Str, StartIdx)`**：从 `StartIdx` 起解析一个**布尔值**，用于 `Signed`。它在串里找 `true`/`false` 两个关键字，**谁先出现就返回谁**，并对大小写不敏感。

两者都不要求调用者预先给定字段长度，都采用「读到不能读为止」的贪婪策略。

#### 4.3.2 核心流程

**`string_parse_int`** 的流程：

```
跳过前导空格                  -- while Str[idx] = ' ' : idx++
若 Str[idx] = '-' :           -- 检测负号
    IsNegative := true; idx++
while Str[idx] 是数字:
    acc ← acc × 10 + digit    -- 累加
    idx++
return IsNegative ? -acc : acc
```

其中「数字字符 → 数值」与「是否数字」由两个更底层的小工具完成：

- `string_int_from_char('0'..'9')` → `0..9`，其余字符返回 `-1`（作为「不是数字」的哨兵值）。
- `string_char_is_numeric(c)` ≡ `string_int_from_char(c) /= -1`。

核心累加公式（Horner 形式）：

\[
\text{acc} \leftarrow \text{acc} \times 10 + d
\]

例如解析 `"3"`：acc = 0×10+3 = 3；解析 `"-12"`：跳过 `-`，acc = 0×10+1 = 1，再 acc = 1×10+2 = 12，最后取负得 `-12`。

**`string_parse_boolean`** 的流程：

```
StrLower := toLower(Str)                          -- 整串归一化为小写（一次性）
TrueIdx  := find(StrLower, "true",  StartIdx)     -- 找 "true" 首次出现位置
FalseIdx := find(StrLower, "false", StartIdx)     -- 找 "false" 首次出现位置
若都没找到 (均为 -1): report error; return false
若只有 "true"  找到: return true
若只有 "false" 找到: return false
若都找到:           return (TrueIdx < FalseIdx)   -- 谁在前返回谁
```

关键点：它在**整个字符串的小写副本**上找关键字，而不是只看 `StartIdx` 处的几个字符。这意味着即便布尔字段后面还跟着别的字符（如 `"true,3,4"` 里的逗号和数字），也能正确识别开头的 `true`。

#### 4.3.3 源码精读

[vhdl/src/en_cl_fix_pkg.vhd:L1230-L1267](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1230-L1267) — `string_parse_int`。注意三段：第 1241–1243 行跳前导空格、第 1246–1249 行检测负号、第 1252–1259 行贪婪累加，最后第 1262–1266 行按符号返回。

[vhdl/src/en_cl_fix_pkg.vhd:L1172-L1197](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1172-L1197) — `string_parse_boolean`。注意第 1175 行用 `constant StrLower_c : string := toLower(Str)` **一次性**把整串转小写，再在第 1183–1184 行分别找 `true`/`false`，最后第 1185–1196 行用四个分支决定返回值。

[vhdl/src/en_cl_fix_pkg.vhd:L1201-L1218](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1201-L1218) — `string_int_from_char`，用 `case` 把 `'0'..'9'` 映射到 `0..9`，`others => -1`。第 1217 行的 `return 0;` 实际上不可达（`case` 已全覆盖并 return），是冗余语句。

[vhdl/src/en_cl_fix_pkg.vhd:L1222-L1226](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1222-L1226) — `string_char_is_numeric`，一行实现，复用 `string_int_from_char` 的 `-1` 哨兵。

#### 4.3.4 代码实践

**实践目标**：手动跟踪两个字段解析器，理解它们对边界输入的行为，无需运行仿真。

**操作步骤**：

1. **跟踪 `string_parse_int(" -12,", 1)`**（注意开头有一个空格，结尾有逗号）：
   - 跳前导空格：`idx` 从 1 → 2（跳过下标 1 的空格）。
   - 下标 2 是 `'-'`：`IsNegative := true`，`idx → 3`。
   - 下标 3 是 `'1'`：`acc = 0×10+1 = 1`，`idx → 4`。
   - 下标 4 是 `'2'`：`acc = 1×10+2 = 12`，`idx → 5`。
   - 下标 5 是 `','`：非数字，退出循环。
   - 返回 `-12`。

2. **跟踪 `string_parse_boolean("(TRUE,3,4)", 2)`**：
   - `StrLower = "(true,3,4)"`。
   - `TrueIdx = find(...,"true",2) = 2`；`FalseIdx = find(...,"false",2) = -1`（找不到）。
   - 进入「只有 true 找到」分支，返回 `true`。

**需要观察的现象 / 预期结果**：上述手工跟踪结果应与你在源码里读到的逻辑一致。把 `"-12"` 这一步对照 `(true,-2,3)` 这类带负整数位的合法格式，确认库确实支持负的 `IntBits`/`FracBits`。

> 进一步思考（不必运行）：如果把 `string_parse_int` 用在 `"(true,3,4)"` 的 `FracBits` 段（起点指向 `'4'`），它会在读到 `')'` 时停止——因为 `')'` 不是数字。这正是「贪婪到非数字字符」与「主流程用 `')'` 做结构分隔符」二者默契配合的体现。

#### 4.3.5 小练习与答案

**练习 1**：`string_parse_int("007,", 1)` 返回什么？前导零会不会出问题？

> **参考答案**：返回 `7`。前导零不会出问题：`acc = 0×10+0 = 0`，再 `0×10+0 = 0`，再 `0×10+7 = 7`。前导零被自然吸收。

**练习 2**：`string_parse_boolean` 在 `"(false,3,4)"` 上为什么返回 `false` 而不是被开头的某段干扰？

> **参考答案**：它先把整串转小写得 `"(false,3,4)"`，然后从 `StartIdx` 起找 `"false"` 和 `"true"`。`"false"` 在下标 2 命中，`"true"` 找不到（`-1`），进入「只有 false 找到」分支，返回 `false`。`"false"` 内部并不包含 `"true"` 子串，所以不会误判。

---

### 4.4 toLower 与 string_find_next_match：底层字符原语

#### 4.4.1 概念说明

再往下一层，是两个最基础的字符操作原语，所有上层解析都建立在它们之上：

- **`toLower`**：大小写归一化。VHDL 标准库**没有**内建的 `toLower`，所以源码手写了一个 `case` 映射，把 `'A'..'Z'` 转成 `'a'..'z'`，其余字符原样返回。它有两个重载：字符版和字符串版（字符串版逐字符调用字符版）。它的唯一调用者是 `string_parse_boolean`，目的是让布尔解析对 `"True"`/`"TRUE"`/`"true"` 一视同仁。
- **`string_find_next_match`**：从指定下标起，**线性扫描**字符串，返回下一个匹配位置的**下标**；找不到返回 `-1`。它是主流程推进游标的引擎。它也有两个重载：单字符版（找一个字符）和子串版（找一个多字符模式）。

`-1` 作为「找不到」的返回值是这整套工具的核心约定——上层用 `Index > 0` 一句断言就能区分「找到了」与「没找到」（字符串下标恒 ≥ 1）。

#### 4.4.2 核心流程

**`toLower`（字符版）**：

```
case c:
    'A' → 'a', 'B' → 'b', ..., 'Z' → 'z'
    others → c            -- 非大写字母原样返回（含小写字母、数字、标点）
```

**`toLower`（字符串版）**：对每个下标 `i`，`v(i) := toLower(s(i))`。

**`string_find_next_match`（字符版）**：

```
idx := StartIdx; found := false; matchIdx := -1
while (not found) and (idx <= Str'high):
    if Str[idx] = Char:
        found := true; matchIdx := idx
    idx++
return matchIdx            -- 找不到仍是初始值 -1
```

**`string_find_next_match`（子串版）**：滑动窗口，在每个起点尝试整体匹配 `Pattern'length` 个字符；窗口右边界不能超出串尾（循环条件 `CurrentIdx-1 <= Str'length-Pattern'length`）；任一字符不匹配则 `exit` 当前窗口、滑到下一起点。

#### 4.4.3 源码精读

[vhdl/src/en_cl_fix_pkg.vhd:L1070-L1103](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1070-L1103) — `toLower`（字符版）。26 个 `when` 分支显式列出 `A..Z`，`when others => v := c` 保证小写字母、数字、标点不受影响。

[vhdl/src/en_cl_fix_pkg.vhd:L1107-L1114](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1107-L1114) — `toLower`（字符串版）。用 `variable v : string(s'range)` 保持与输入相同的下标范围，再逐字符转换。

[vhdl/src/en_cl_fix_pkg.vhd:L1118-L1138](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1118-L1138) — `string_find_next_match`（字符版）。注意第 1127 行的范围 `assert`，以及 `while (not Match_v) and (CurrentIdx_v <= Str'high)` 的短路退出。

[vhdl/src/en_cl_fix_pkg.vhd:L1142-L1168](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1142-L1168) — `string_find_next_match`（子串版）。注意第 1154 行的窗口边界条件 `CurrentIdx_v-1 <= Str'length-Pattern'length`，以及内层 `for Idx in 1 to Pattern'length` 的逐字符比对、不匹配时 `exit`。

> 一个边界细节：`string_find_next_match` 在 `StartIdx` 越界时（第 1127、1151 行）用 `severity error` 的 `assert` 报错，但**不阻止**函数继续执行（`severity error` 不中止）。正常运行路径下调用方（主流程）总是传合法的 `Index_v+1`，所以这条 `assert` 主要是防御性检查。

#### 4.4.4 代码实践

**实践目标**：追踪 `string_parse_boolean` 如何借助 `toLower` 实现大小写不敏感，把 4.3 的「结论」落实到 4.4 的「机制」。

**操作步骤（源码阅读型）**：

1. 在 `string_parse_boolean`（第 1172–1197 行）里找到第 1175 行 `constant StrLower_c : string := toLower(Str)`。
2. 跟进到 `toLower`（字符串版，第 1107 行），再跟进到字符版（第 1070 行）。
3. 用三种输入手工走一遍 `string_parse_boolean`：
   - `"(true,3,4)"` → `StrLower` 不变 → `TrueIdx=2`，返回 `true`。
   - `"(TRUE,3,4)"` → `StrLower = "(true,3,4)"` → 同上，返回 `true`。
   - `"(True,3,4)"` → `StrLower = "(true,3,4)"` → 同上，返回 `true`。

**需要观察的现象 / 预期结果**：三种大小写写法都返回 `true`，因为它们在 `toLower` 之后归一化成了同一个串。**结论**：大小写不敏感不是 `string_find_next_match` 提供的（它做精确匹配），而是 `string_parse_boolean` 在调用它之前先用 `toLower` 把输入「拉平」得到的。这是一个清晰的**职责分层**——匹配原语保持简单（精确匹配），大小写容忍由调用方负责。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `string_find_next_match` 找不到目标时返回 `-1`，而不是 0？

> **参考答案**：因为 VHDL 字符串下标从 1 起（或更一般的 `'low`），任何合法命中位置都 ≥ 1。用 `-1` 这种「不可能是合法下标」的值表示「未命中」，上层就能用 `Index > 0` 一句话区分命中与未命中。

**练习 2**：如果让 `string_find_next_match` 自己做大小写不敏感匹配，而不是由 `string_parse_boolean` 先 `toLower`，会有什么缺点？

> **参考答案**：会把「大小写归一化」这个职责塞进底层匹配原语，使原语变复杂、失去通用性（其他调用方，如主流程找 `'('`/`','`/`')'` 这些本就无大小写的分隔符，不需要也不希望做归一化）。当前的分层让 `string_find_next_match` 保持为纯粹的精确匹配工具，大小写容忍只在需要它的 `string_parse_boolean` 里处理一次。

---

### 4.5 健壮性设计：三类 assert 的防御式检查

#### 4.5.1 概念说明

把前面三节散见的 `assert` 汇总来看，整条解析链其实有一套一致的**防御式设计**，可归为三类。VHDL 里 `assert` 的 `severity` 决定它的「力度」：

- `severity note/warning`：仅打印消息，绝不中止仿真。
- `severity error`：打印消息，**默认仍继续**仿真（除非仿真器配置成 error 即停）。
- `severity failure`：打印消息并**立即中止**仿真。

这条链里**所有**字符串相关的 `assert` 都用 `severity error`——这与 u2-l2 介绍的 testbench 策略一致：让一次 `run -all` 能尽量多收集错误，而不是在第一个错误处就停。

#### 4.5.2 核心流程

三类检查分布如下：

| 类别 | 检查内容 | 触发函数 | severity | 行号 |
| --- | --- | --- | --- | --- |
| ① 起点越界 | `StartIdx` 不在 `Str'low..Str'high` 内 | `string_find_next_match`(两个重载)、`string_parse_int` | error | 1127, 1151, 1238 |
| ① 起点越界（小写副本） | `StartIdx` 不在 `StrLower_c` 范围内 | `string_parse_boolean` | error | 1180 |
| ② 结构分隔符缺失 | `find '('`/`','`/`','`/`')'` 返回 `-1` | `cl_fix_format_from_string` | error | 1349, 1354, 1359, 1364 |
| ③ 值未找到 | 既找不到 `true` 也找不到 `false` | `string_parse_boolean` | error | 1187 |

处理模式高度统一：**底层返回 `-1`（未命中）→ 上层用 `assert ... > 0` 或专用消息拦截**。例如：

- `string_find_next_match` 找不到 → 返回 `-1`；主流程用 `assert Index_v > 0` 拦截，并指明缺哪个分隔符（第 ② 类）。
- `string_parse_boolean` 两个关键字都找不到 → 直接在自己内部 `report ... severity error` 后 `return false`（第 ③ 类），把兜底值交出去，避免下游用未初始化数据。

#### 4.5.3 源码精读

[vhdl/src/en_cl_fix_pkg.vhd:L1349-L1366](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1349-L1366) — 第 ② 类：四个结构分隔符缺失断言，消息精确指出缺 `'('`、第一个 `','`、第二个 `','`、`')'` 中的哪一个。

[vhdl/src/en_cl_fix_pkg.vhd:L1185-L1191](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1185-L1191) — 第 ③ 类：`string_parse_boolean` 中 `TrueIdx_v = -1` 且 `FalseIdx_v = -1` 时 `report ... severity error` 后 `return false`，给出确定的兜底返回值。

[vhdl/src/en_cl_fix_pkg.vhd:L1127](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1127) — 第 ① 类：`string_find_next_match`（字符版）的起点越界检查，`StartIdx <= Str'high and StartIdx >= Str'low`。

> 注意一个**健壮性缺口**：当第 ② 类断言触发（如缺左括号、`Index_v = -1`）时，由于 `severity error` 不中止仿真，主流程会**继续**用 `Index_v+1 = 0` 调用 `string_parse_boolean`/`string_find_next_match`，进而触发第 ① 类的越界 `assert`。也就是说，一个畸形输入可能在 transcript 里连报几条 `assert`。这不是 bug，而是「`severity error` 不中止」策略的固有副作用——读 transcript 时要把这一串关联报错视为同一个根因。

#### 4.5.4 代码实践

**实践目标**：构造各种畸形输入，预测会触发哪一类 `assert`、消息是什么、`severity` 是什么。

**操作步骤（源码阅读型 + 可选运行）**：

填下面这张预测表（先自己根据源码推断，再用仿真验证）：

| 输入字符串 | 触发的断言类别 | report 消息关键字 | severity | 解析返回值 |
| --- | --- | --- | --- | --- |
| `"(true,3,4)"` | 无（合法） | — | — | `(true,3,4)` |
| `"true,3,4)"`（缺 `(`） | ② | `missing '('` | error | 未定义（游标为 -1） |
| `"(true 3,4)"`（缺第一个 `,`） | ② | `missing ',' between IsSigned and IntBits` | error | 未定义 |
| `"(true,3,4"`（缺 `)`） | ② | `missing ')'` | error | `Signed/IntBits/FracBits` 仍被解析 |
| `"(maybe,3,4)"`（非布尔） | ③ | `no boolean string found` | error | `Signed = false`（兜底） |

**需要观察的现象**：上表每一行的「类别/消息/severity」应与你阅读源码后给出的推断一致；其中畸形行往往在 transcript 里**级联**出现多条 `assert`（见上面的健壮性缺口说明）。

**预期结果**：你能仅凭源码就准确预测每类畸形输入的报错行为。若在 testbench 里用 `cl_fix_format_from_string(...)` 包一层调用并仿真，可看到 `###ERROR###` 之外还会出现仿真器打印的 `assert` 消息（注意：testbench 的 `Check*` 不覆盖这些私有路径，需直接调用公开函数）。**若工具未安装无法运行，本步骤标记为「待本地验证」。**

#### 4.5.5 小练习与答案

**练习 1**：为什么这条链里所有 `assert` 都用 `severity error` 而不是 `severity failure`？

> **参考答案**：因为 `severity error` 默认不中止仿真，能让一次 `run -all` 收集到尽可能多的错误信息（与 u2-l2 介绍的 testbench `Check*` 策略一致）。如果改用 `severity failure`，遇到第一个畸形输入就立即停，后面的错误都看不到了。注意对比：`cl_fix_format`/`cl_fix_width` 里 `IntBits+FracBits>=1` 的约束用的是 `severity failure`（u1-l2、u1-l3），因为那是**语义级**硬约束、不可恢复；而字符串解析这里的错误是**输入级**的，用 `error` 更合适。

**练习 2**：`string_parse_boolean` 在「`true`/`false` 都找不到」时，为什么 `report` 之后还要写一句 `return false`？

> **参考答案**：因为 `severity error` 不中止函数执行。如果不写 `return false`，函数会继续往下走（落到后面的 `elsif`/`else` 分支）去读未命中的 `TrueIdx_v`/`FalseIdx_v`（均为 `-1`），行为不可控。显式 `return false` 给出一个确定的兜底返回值，保证调用方拿到的是一个明确的、可预测的结果。

## 5. 综合实践

把本讲四个最小模块串起来，完成一个「**给字符串转换补测试**」的小任务——因为 testbench 目前**完全没有**覆盖 `cl_fix_string_from_format` / `cl_fix_format_from_string`，这正是读者可以贡献的真实改进点。

**任务**：在 `vhdl/tb/en_cl_fix_pkg_tb.vhd` 的 `p_control` 进程里，新增一个 `*** cl_fix_format_from_string ***` 测试段，至少覆盖以下场景，并全部用 `Check*` 过程断言：

```vhdl
-- 示例代码：新增测试段（仿照第 97 行 cl_fix_width 段的写法）
print("*** cl_fix_format_from_string ***");

-- (1) 基本往返：width 验证解析结果
CheckInt(8, cl_fix_width(cl_fix_format_from_string("(true,3,4)")),
         "from_string: basic width");
CheckInt(3, cl_fix_width(cl_fix_format_from_string("(false,3,0)")),
         "from_string: unsigned width");

-- (2) 负整数位 / 负小数位（u1-l2 边界格式）
CheckInt(2, cl_fix_width(cl_fix_format_from_string("(true,-2,3)")),
         "from_string: negative IntBits");
CheckInt(2, cl_fix_width(cl_fix_format_from_string("(true,3,-2)")),
         "from_string: negative FracBits");

-- (3) 大小写不敏感（验证 toLower 通路）
CheckBoolean(true,  cl_fix_format_from_string("(TRUE,3,4)").Signed,
             "from_string: case-insensitive TRUE");
CheckBoolean(false, cl_fix_format_from_string("(False,3,4)").Signed,
             "from_string: case-insensitive False");

-- (4) 往返一致性：序列化 -> 解析 -> 序列化 应逐字符相等
CheckBoolean(true,
    cl_fix_string_from_format(cl_fix_format_from_string("(true,3,4)")) = "(true,3,4)",
    "from_string: roundtrip equality");
```

**完成后请验证**：

1. 按 u2-l2 的方式运行 `sim/sim.tcl`，确认新段不产生任何 `###ERROR###`。
2. 临时把第 (1) 行的期望值从 `8` 改成 `9`，重新运行，确认 transcript 出现 `###ERROR### ... [expected: 9, got: 8]`——这证明你的断言真的在起作用（改回 `8`）。
3. 写一段总结：这条测试链覆盖了 4.1（往返接口）、4.2（主流程）、4.3（负号/负数解析）、4.4（大小写归一化）中的哪些点？4.5 的健壮性 `assert` 为什么**不适合**用 `Check*` 来测（提示：它们是 `severity error` 的 `report`，不是布尔返回值，需要扫 transcript 文本而非用 `Check*` 比对）？

> 若本地无 Modelsim，可把第 3 点的总结写成纯源码阅读结论，并把第 1、2 点标记为「待本地验证」。

## 6. 本讲小结

- **工程背景**：Modelsim 的 generic 只支持 `integer`/`string`/`boolean`，不支持 `FixFormat_t` 这类 record，所以必须用字符串把定点格式传给仿真——这是 `cl_fix_format_from_string` 存在的根本原因。
- **对称接口**：`cl_fix_string_from_format`（`'image` 拼接，序列化）与 `cl_fix_format_from_string`（解析，反序列化）互为逆运算，是这趟「过 generic」通道的两端，且是本讲中**唯一公开**的两个函数。
- **游标式主流程**：`cl_fix_format_from_string` 用 `Index_v` 游标依次找 `'('`/`','`/`','`/`')'` 四个结构分隔符，把每段交给字段解析器；字段解析器是贪婪的，自然停在下一个分隔符上，所以主流程不必预算字段长度。
- **字段解析器**：`string_parse_int` 处理前导空格、负号、贪婪累加（支持负的 `IntBits`/`FracBits`）；`string_parse_boolean` 在整串小写副本上找 `true`/`false`，谁先出现返回谁。
- **底层原语与职责分层**：`toLower`（VHDL 无内建，手写 `case`）只服务 `string_parse_boolean`，把大小写容忍的职责放在调用方；`string_find_next_match`（字符/子串两重载）是纯精确匹配引擎，找不到返回 `-1`。
- **统一的健壮性约定**：底层未命中返回 `-1`，上层用 `severity error` 的 `assert` 拦截（结构分隔符缺失、起点越界、值未找到三类），`error` 不中止仿真以收集更多错误，代价是畸形输入可能级联多条报错；这与 `cl_fix_format`/`cl_fix_width` 用 `severity failure` 的语义级硬约束形成对比。

## 7. 下一步学习建议

- **横向对照 Python/MATLAB**：Python 端 `FixFormat` 有自己的 `__str__`/字符串构造，但**没有**等价的「从 `(signed,int,frac)` 字符串解析」需求（Python 不受 generic 限制）。可以去 `python/src/en_cl_fix_pkg/en_cl_fix_types.py` 读 `FixFormat.__init__`，对比三语言在「格式来源」上的差异。
- **追踪真实使用场景**：`cl_fix_format_from_string` 在本仓库内**只被定义、未被调用**（可用 `grep` 验证）。它的真正用武之地在下游工程——任何用 string generic 传格式的 PSI IP/testbench。建议在读者的实际 FPGA 工程里找一个 `generic (... Fmt : string ...)` 的实体，用 `cl_fix_format_from_string(Fmt)` 把字符串还原成格式，闭合这条链路。
- **延伸到 `en_cl_bittrue_pkg`**：docstring 提到 `cl_fix_string_from_format`「在用 `en_cl_bittrue_pkg` 时特别有用」。位真数据交换（u5-l1）与格式字符串序列化是天然搭档——格式用字符串传、数据用位整数传，可去了解 `en_cl_bittrue_pkg` 如何把这两者组合成完整的跨语言位真校验流水线。
- **回到 u1-l3**：若对 `cl_fix_string_from_format` 的输出形态（`(true,3,4)`）在三语言间的差异（VHDL/MATLAB 小写无空格 vs Python 大写带空格）印象模糊，可回看 u1-l3 的相关段落，强化「同一格式，三种字符串外壳」的认知。
