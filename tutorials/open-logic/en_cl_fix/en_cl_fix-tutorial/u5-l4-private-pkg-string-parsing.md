# 私有包与字符串解析

## 1. 本讲目标

本讲聚焦 en_cl_fix 的「幕后工具层」——`en_cl_fix_private_pkg.vhd`，以及主包里基于它实现的字符串与格式互转函数。读完后你应该能够：

- 说清楚私有包里 `choose` / `to01` / `toInteger` / `maximum` / `minimum` 这几个小工具各自解决什么问题，以及为什么主包离不开它们。
- 理解 `toLower` / `string_find_next_match` / `string_parse_int` 这三个字符串底层函数如何配合，实现一个不依赖任何外部库的极简「格式串解析器」。
- 解释 `to_string(FixRound_t)` / `to_string(FixSaturate_t)` 为什么必须用手写 `case` 显式实现，而不能直接用 VHDL 的 `'image` 属性。
- 能够手工跟踪 `cl_fix_format_from_string` 把 `"(1,4,-2)"` 这样一个字符串解析成 `FixFormat_t` 记录的每一步。

## 2. 前置知识

本讲是 U5 单元的第四篇，承接 u5-l1（VHDL 包头类型与公共 API）。你需要已经知道：

- `FixFormat_t` 是一个 record，由 `S`（符号位，`natural range 0 to 1`）、`I`（整数位，integer）、`F`（小数位，integer）三个字段组成（见 [en_cl_fix_pkg.vhd:39-43](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L39-L43)）。
- `FixRound_t`（七种舍入模式）与 `FixSaturate_t`（四种饱和模式）是**枚举类型（enumeration type）**。
- RTL 代码遵循 **VHDL-93** 标准（这是 u1-l3 已确认的事实）。这一条对理解本讲非常关键，因为「为什么要自己实现 `maximum`」完全由它决定。

另外有两个 VHDL 语法背景，初学者可能不熟，先在这里铺垫：

- **`'image` 属性**：VHDL 给离散类型（整数、枚举）提供的属性，`T'image(x)` 会把值 `x` 转成它的字符串字面量，例如 `integer'image(4)` 返回 `" 4"`，`FixRound_t'image(Trunc_s)` 返回 `"Trunc_s"`。它在 VHDL-93 标准里就已存在，但**综合工具对它的支持参差不齐**——这正是本讲的伏笔。
- **`when ... else` 条件表达式**：VHDL-2008 才允许在表达式里写 `x when cond else y`；VHDL-93 不行。于是在「常量声明」里要做条件选择，就只能借助函数调用。

## 3. 本讲源码地图

本讲只涉及两个文件：

| 文件 | 角色 | 本讲关注的内容 |
|------|------|----------------|
| [hdl/en_cl_fix_private_pkg.vhd](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_private_pkg.vhd) | 私有工具包 | `choose` / `to01` / `toInteger` / `maximum` / `minimum` / `toLower` / `string_find_next_match` / `string_parse_int` |
| [hdl/en_cl_fix_pkg.vhd](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd) | 主包 | `to_string` 系列、`cl_fix_format_from_string` / `cl_fix_round_from_string` / `cl_fix_saturate_from_string` |

私有包并不直接被 RTL 设计者使用，而是被主包在第 29 行整体引入：[en_cl_fix_pkg.vhd:29](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L29)。可以把私有包理解为主包的「内部零件库」。

## 4. 核心概念与源码讲解

### 4.1 私有包的角色与三元工具 choose / to01 / toInteger

#### 4.1.1 概念说明

私有包 `en_cl_fix_private_pkg` 收纳了一组「最底层的、与定点语义无关」的通用小工具。它们存在的共同动机是：**弥补 VHDL-93 在表达式层面的表达力不足**。

最典型的痛点是「在常量声明里做条件选择」。看主包里 `cl_fix_add_fmt` 的一段：

```vhdl
constant rmax_growth_c  : natural := choose(minimum(a_fmt.I, b_fmt.I) + minimum(a_fmt.F, b_fmt.F) > 0, 1, 0);
```

（见 [en_cl_fix_pkg.vhd:422](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L422)）

这是一句**常量声明**，等号右边必须是一个表达式。在 VHDL-93 里，表达式不能写 `if`，也没有三元运算符 `cond ? a : b`。于是项目用 `choose` 函数来扮演三元运算符的角色：`choose(cond, if_true, if_false)`。

#### 4.1.2 核心流程

`choose` 提供了两个重载（整数版与 `std_logic` 版），逻辑都极简：

```
choose(cond, a, b):
    若 cond 为真 → 返回 a
    否则          → 返回 b
```

`toInteger` 把布尔值压成 0/1 整数，方便参与算术；`to01` 把含 `'H'/'L'/'Z'/'X'` 等「弱值/元值」的 `std_logic` 归一化成干净的 `'0'/'1'`，避免仿真期出现 `'X'` 污染计算结果。

#### 4.1.3 源码精读

`choose` 的两个重载声明在包头：[en_cl_fix_private_pkg.vhd:31-32](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_private_pkg.vhd#L31-L32)，实现就是直白的 `if` 判断：[en_cl_fix_private_pkg.vhd:51-65](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_private_pkg.vhd#L51-L65)。

`to01` 把 `'1'` 与 `'H'` 都当作 `'1'`，其余一律 `'0'`：[en_cl_fix_private_pkg.vhd:67-85](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_private_pkg.vhd#L67-L85)。注意向量版用递归调用逐位处理，是处理「弱驱动」信号时的标准套路。

`toInteger` 把布尔映射成整数：[en_cl_fix_private_pkg.vhd:87-94](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_private_pkg.vhd#L87-L94)。主包 `cl_fix_addsub` 就用它把 `add` 信号先压成 0/1 再分发（见 [en_cl_fix_pkg.vhd:1208](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L1208) 的 `to01(add)`）。

#### 4.1.4 代码实践

**实践目标**：体会 `choose` 作为「常量里的三元运算符」的必要性。

1. 打开 [en_cl_fix_pkg.vhd:422-429](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L422-L429)，找到两处 `choose(..., 1, 0)`。
2. 设想：如果 VHDL-93 允许在常量表达式里写 `if`，这两行会怎么改写？
3. **预期结果**：你会发现它们都形如「某个布尔条件是否成立 → 取 1 或 0」。这正是三元运算符的标准用途，项目用函数调用绕过了 VHDL-93 的语法限制。无需运行仿真，这是一道源码阅读练习。

> 待本地验证：如果你手头有 VHDL-2008 仿真器，可尝试把 `choose(c, 1, 0)` 改写成 VHDL-2008 的 `1 when c else 0`，确认两者在仿真结果上等价（本讲不要求改源码）。

#### 4.1.5 小练习与答案

**练习 1**：主包里还有第三个 `choose` 重载（针对 `FixFormat_t`，见 [en_cl_fix_pkg.vhd:101](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L101) 与 [en_cl_fix_pkg.vhd:630-636](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L630-L636)）。它被用在哪里？

**答案**：被各数学函数用来实现「哨兵回退」——当 `result_fmt = NullFixFormat_c`（未指定结果格式）时回退到全精度 `mid_fmt_c`。例如 `cl_fix_abs` 里的 `choose(result_fmt = NullFixFormat_c, mid_fmt_c, result_fmt)`（[en_cl_fix_pkg.vhd:1119](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L1119)）。这是 u5-l1 讲过的「缺省即无损」语义在源码里的落点。

**练习 2**：`to01` 为什么要把 `'H'` 也归一化成 `'1'`？

**答案**：`'H'` 是 std_logic_1164 里的「弱 1」（weak high），常出现在开漏/线与结构中。算术运算前若不归一化，`'H'` 与 `'1'` 比较不相等会污染结果，所以先 `to01` 再计算更安全。

---

### 4.2 maximum / minimum：为什么不能直接用 VHDL 内置的？

#### 4.2.1 概念说明

VHDL-2008 标准为整数等标量类型提供了内置的 `maximum` / `minimum` 函数。但 **VHDL-93 没有这两个函数**。由于 en_cl_fix 的 RTL 严格按 VHDL-93 编译（见 u1-l3），主包里又大量需要在常量表达式里取两个整数的大值（例如 `union` 取 `S/I/F` 各自最大值），所以必须在私有包里自行实现。

#### 4.2.2 核心流程

```
maximum(a, b):  若 a >= b 返回 a，否则返回 b
minimum(a, b):  若 a <= b 返回 a，否则返回 b
```

两者都是纯组合函数，可被综合工具当作普通比较器/选择器展开。

#### 4.2.3 源码精读

声明见 [en_cl_fix_private_pkg.vhd:37-38](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_private_pkg.vhd#L37-L38)，实现见 [en_cl_fix_private_pkg.vhd:96-112](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_private_pkg.vhd#L96-L112)。

最典型的消费点是主包内部的 `union` 函数——它对 `S/I/F` 三个字段各调用一次 `maximum`，得到「最小公共超集」格式：[en_cl_fix_pkg.vhd:353-360](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L353-L360)。而 `union` 又是 `cl_fix_addsub_fmt`、`cl_fix_abs_fmt` 等格式预测函数的公共积木（见 u2-l4）。

#### 4.2.4 代码实践

**实践目标**：验证「自实现 `maximum/minimum` 是 VHDL-93 的硬性要求」。

1. 在主包中用搜索功能查找所有 `maximum(` 与 `minimum(` 的调用点（例如 [en_cl_fix_pkg.vhd:422](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L422) 的 `minimum(a_fmt.I, b_fmt.I)`）。
2. 注意它们几乎都出现在 `constant ... :=` 的右边或 record 字段初始化里——这些都是**表达式上下文**。
3. **预期结果**：你会确认这些位置都无法用 VHDL-93 的 `if` 语句替代，必须借助函数调用；而 VHDL-93 又不提供现成的 `maximum`，于是私有包的这两个函数不可省略。这是源码阅读型实践，无需运行。

#### 4.2.5 小练习与答案

**练习**：如果把项目从 VHDL-93 升级到 VHDL-2008，私有包里的 `maximum` / `minimum` 还需要吗？

**答案**：理论上可以删掉，改用 VHDL-2008 内置的 `maximum` / `minimum`（它们可在表达式中使用）。但实际上保留也无害，且能维持 VHDL-93 兼容性，让 RTL 在只支持 93 的老旧工具链上也能综合，所以项目选择保留自实现。

---

### 4.3 字符串底层工具 toLower / string_find_next_match / string_parse_int

#### 4.3.1 概念说明

en_cl_fix 需要从文本（例如测试台读取的配置文件、或 cosim 生成的格式描述文件，见 u7）把字符串解析回 `FixFormat_t` 和两个枚举类型。VHDL-93 的标准库里**几乎没有字符串处理能力**——没有正则、没有 `split`、没有 `parseInt`。于是私有包从零搭起一个极简的「手摇解析器」，由三件套构成：

- `toLower`：把字符串统一转小写，使枚举名匹配**大小写不敏感**。
- `string_find_next_match`：从指定位置向后扫描，找到下一个出现的指定字符（例如分隔符 `','`），返回其下标。这是手写的「找下一个分隔符」。
- `string_parse_int`：从指定位置开始解析一个整数（支持前导空格和负号），返回整数值。这是手写的 `parseInt`。

#### 4.3.2 核心流程

`string_find_next_match(Str, Char, StartIdx)`：

```
从 CurrentIdx = StartIdx 开始向后扫描：
    若 Str(CurrentIdx) == Char → 记录下标，停止
    否则 CurrentIdx++
若一直没匹配到 → 返回 -1（哨兵值，表示「未找到」）
```

`string_parse_int(Str, StartIdx)`：

```
跳过前导空格
若当前字符是 '-' → 标记负数，前进一位
循环读取连续数字字符，累加：val = val*10 + digit
应用符号，返回 val
```

`toLower` 则遍历每个字符，靠一个 `case` 把 `'A'..'Z'` 映射成 `'a'..'z'`，其余字符原样保留。

#### 4.3.3 源码精读

`toLower` 的字符串版声明见 [en_cl_fix_private_pkg.vhd:40](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_private_pkg.vhd#L40)，实现见 [en_cl_fix_private_pkg.vhd:149-156](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_private_pkg.vhd#L149-L156)。它逐字符调用单字符版 `toLower(character)`——后者**没有出现在包头里**，是包体内部的私有辅助函数：[en_cl_fix_private_pkg.vhd:114-147](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_private_pkg.vhd#L114-L147)。这是一个值得学习的细节：**VHDL 包体里可以定义只对本包可见的局部函数**，把它们排除出公共 API。

`string_find_next_match` 实现见 [en_cl_fix_private_pkg.vhd:158-175](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_private_pkg.vhd#L158-L175)。注意它先用 `assert` 校验 `StartIdx` 范围（[L164](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_private_pkg.vhd#L164)），再用 `while` 循环扫描，未命中返回 `-1`。

`string_parse_int` 实现见 [en_cl_fix_private_pkg.vhd:200-235](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_private_pkg.vhd#L200-L235)。它依赖两个同样包体私有的小帮手：
- `string_int_from_char`：把一个数字字符转成 0..9，非数字返回 `-1`：[en_cl_fix_private_pkg.vhd:177-193](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_private_pkg.vhd#L177-L193)。
- `string_char_is_numeric`：判断字符是否为数字：[en_cl_fix_private_pkg.vhd:195-198](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_private_pkg.vhd#L195-L198)。

`string_parse_int` 的核心累加公式是一个经典的手写进制转换：

\[
\text{val} = \text{val} \times 10 + \text{digit}
\]

对应 [en_cl_fix_private_pkg.vhd:224](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_private_pkg.vhd#L224)。当遇到非数字字符时（即分隔符或结尾），它通过把 `CurrentIdx_v` 设为 `Str'high+1` 来跳出循环（[L222](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_private_pkg.vhd#L222)），这是一种常见的「用越界下标终止 while」的写法。

#### 4.3.4 代码实践

**实践目标**：理解 `string_parse_int` 如何处理负数与停止条件。

1. 阅读 [en_cl_fix_private_pkg.vhd:200-235](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_private_pkg.vhd#L200-L235)。
2. 假设输入字符串 `"-12abc"`、`StartIdx=1`，手工推演：跳过空格（无）→ 见 `'-'` 标记负数并前进 → 读 `'1'`（val=1）→ 读 `'2'`（val=12）→ 读 `'a'`（非数字，越界退出）→ 返回 `-12`。
3. **预期结果**：解析器停在第一个非数字字符处，不报错，安静返回已解析部分。这正符合「分隔符自然终止解析」的设计。这是源码阅读型实践。

> 待本地验证：若你想确认行为，可写一个最小测试台调用 `string_parse_int("-12abc", 1)` 并 `report` 结果，期望输出 `-12`。

#### 4.3.5 小练习与答案

**练习 1**：`string_find_next_match` 找不到目标字符时返回什么？调用方如何据此判断？

**答案**：返回 `-1`（[en_cl_fix_private_pkg.vhd:161](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_private_pkg.vhd#L161) 的初值）。调用方（如 `cl_fix_format_from_string`）紧跟一句 `assert Index_v > 0`，把 `-1` 翻译成致命错误并指明缺哪个分隔符。

**练习 2**：`toLower(character)` 与 `string_int_from_char` 为什么不放进包头？

**答案**：它们只是包体内部的实现细节，外部无需也不应调用。VHDL 允许在 `package body` 里定义这类局部函数，从而保持包头（公共 API）的简洁。

---

### 4.4 to_string 显式实现：为什么不直接用 'image

#### 4.4.1 概念说明

主包提供了四个 `to_string` 重载（声明见 [en_cl_fix_pkg.vhd:106-112](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L106-L112)）：

| 重载 | 入参 | 实现方式 |
|------|------|----------|
| `to_string(slv, fmt)` | 位串 + 格式 | 手写逐位拼字符 |
| `to_string(fmt)` | `FixFormat_t` | **用** `natural'image` / `integer'image` |
| `to_string(rnd)` | `FixRound_t` | 手写 `case` |
| `to_string(sat)` | `FixSaturate_t` | 手写 `case` |

关键反差在于：给**整数/自然数**转字符串时，项目放心地用了 `'image`；给**枚举类型**转字符串时，项目却绕开 `'image`、手写 `case`。原因写在源码注释里：「**Some synthesis tools do not support `FixRound_t'image()`**」。

#### 4.4.2 核心流程

枚举类型的 `'image` 在 VHDL-93 标准里是合法的（仿真器都支持），但**综合工具对它的支持很不一致**：不少工具只实现了整数 `'image`（用于 `report` 消息），却把枚举 `'image` 视为仿真专属、综合期报错或丢弃。由于 `to_string` 的结果可能进入需要被综合的逻辑路径（例如经由 `report`/常量进入设计），项目选择对两个枚举类型都用 `case` 显式列出每个枚举字面量对应的字符串，保证结果在所有目标工具链下都确定可知。

而 `FixFormat_t` 是 record，本就没有 `'image`；其字段 `S/I/F` 是 `natural`/`integer`，它们的 `'image` 被广泛支持，所以 `to_string(fmt)` 直接拼接 `'image` 即可。

#### 4.4.3 源码精读

`to_string(FixRound_t)` 的显式 `case` 见 [en_cl_fix_pkg.vhd:700-714](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L700-L714)，关键注释在 [L702](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L702)：

```vhdl
-- Some synthesis tools do not support FixRound_t'image(), so we implement explicitly.
case rnd is
    when Trunc_s     => return "Trunc_s";
    when NonSymPos_s => return "NonSymPos_s";
    ...
```

`to_string(FixSaturate_t)` 同构，见 [en_cl_fix_pkg.vhd:716-727](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L716-L727)，注释在 [L718](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L718)。两者都加了 `when others => report ... severity Failure` 作为「出现新枚举值时立刻报警」的护栏（[L711](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L711)、[L724](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L724)），并在函数末尾 `return ""` 以满足 VHDL「所有路径必须有返回」的要求。

对比 `to_string(FixFormat_t)`：它直接用 `natural'image(fmt.S)` 与 `integer'image(...)` 拼出 `"(S,I,F)"`，见 [en_cl_fix_pkg.vhd:695-698](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L695-L698)。这正是「整数 `'image` 可用、枚举 `'image` 不可用」差异的同一份代码里的直接对照。

#### 4.4.4 代码实践

**实践目标**：本讲规定的核心实践之一——解释为何枚举 `to_string` 不用 `'image`。

1. 打开 [en_cl_fix_pkg.vhd:700-714](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L700-L714)，阅读 `to_string(FixRound_t)` 的 `case` 与第 702 行注释。
2. 用一句话写下你的解释：**为什么不写 `return FixRound_t'image(rnd)`？**
3. **预期答案要点**：
   - 不是标准问题——`'image` 在 VHDL-93 就有。
   - 是**综合工具支持问题**——部分综合工具不支持枚举类型的 `'image`，会综合失败或行为异常。
   - 对比佐证：同文件里 `to_string(FixFormat_t)` 对其整数字段用了 `'image`，说明整数 `'image` 在目标工具链下是安全的，问题仅出在枚举。
   - 显式 `case` 还顺带保证了输出字符串完全可控，并能在新增枚举值时通过 `when others` 立即报错。

无需运行仿真，这是一道理解型练习。

#### 4.4.5 小练习与答案

**练习 1**：函数末尾为什么还要 `return ""`（[L713](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L713)）？`when others` 不是已经 `report ... Failure` 了吗？

**答案**：VHDL 要求函数每条路径都有返回值。`report ... severity Failure` 在仿真期会终止，但在语法/语义层面它不是 `return`，编译器仍认为 `when others` 分支「没有返回值」。补一句 `return ""` 只是为了满足语法，实际运行时若走到该分支已被 `Failure` 终止。

**练习 2**：如果将来给 `FixRound_t` 新增一种舍入模式，却忘了更新 `to_string(FixRound_t)`，会发生什么？

**答案**：新值会落入 `when others`，触发 `report ... severity Failure`，仿真立即终止并打印诊断信息。这正是 `when others` 护栏的价值——强制维护者在新增枚举值时同步更新字符串映射。

---

### 4.5 from_string：从文本解析格式与枚举

#### 4.5.1 概念说明

与 `to_string` 相反，三个 `*_from_string` 函数把字符串还原成结构化值：

- `cl_fix_format_from_string`：把 `"(1,4,-2)"` 还原成 `FixFormat_t`。
- `cl_fix_round_from_string`：把 `"trunc_s"`（大小写不敏感）还原成 `FixRound_t`。
- `cl_fix_saturate_from_string`：把 `"satwarn_s"` 还原成 `FixSaturate_t`。

它们是测试台读取 cosim 黄金数据时的入口（见 u7-l2：测试台用文件 I/O 读格式文件，再据此驱动 UUT）。解析逻辑完全建立在 4.3 的三件套之上。

#### 4.5.2 核心流程

`cl_fix_format_from_string` 的解析套路是「**用 `string_find_next_match` 逐个定位分隔符，用 `string_parse_int` 抽数值**」：

```
找到 '('                 → 记录位置
取 '(' 后一个字符          → 它必须是 '0' 或 '1'，作为 S
找下一个 ','             → 从其后开始 parse_int 得到 I
找下一个 ','             → 从其后开始 parse_int 得到 F
找下一个 ')'             → 校验格式串完整
```

每一步找不到分隔符都 `assert ... severity Failure` 报具体缺失。

两个枚举解析则更简单：先 `toLower` 归一化大小写，再逐字面量 `if-elsif` 比对，未命中则 `report failure`。

#### 4.5.3 源码精读

`cl_fix_format_from_string` 声明见 [en_cl_fix_pkg.vhd:114](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L114)，实现见 [en_cl_fix_pkg.vhd:729-758](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L729-L758)。注意它对 `S` 字段是**直接读单字符**（[L739-L745](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L739-L745)），而对 `I`、`F` 才用 `string_parse_int`——因为 `S` 只能是 0 或 1，单字符足够。

`cl_fix_round_from_string` 见 [en_cl_fix_pkg.vhd:760-781](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L760-L781)：先 `toLower(Str)`（[L761](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L761)），再逐字面量比对，因此 `"TRUNC_S"`、`"Trunc_S"`、`"trunc_s"` 都能解析成功。`cl_fix_saturate_from_string` 同构，见 [en_cl_fix_pkg.vhd:783-798](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L783-L798)。

#### 4.5.4 代码实践

**实践目标**：本讲规定的核心实践之二——手工跟踪 `cl_fix_format_from_string` 解析 `"(1,4,-2)"`。

字符串 `"(1,4,-2)"` 共 8 个字符，下标 1..8：

| 下标 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 |
|------|---|---|---|---|---|---|---|---|
| 字符 | `(` | `1` | `,` | `4` | `,` | `-` | `2` | `)` |

按 [en_cl_fix_pkg.vhd:729-758](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L729-L758) 的步骤逐步推演：

1. `Index_v := Str'low = 1`。
2. `string_find_next_match(Str, '(', 1)`：下标 1 即 `(`，命中 → `Index_v = 1`。`assert 1>0` 通过。
3. 读 S：`Str(Index_v+1) = Str(2) = '1'` → `Format_v.S := 1`。
4. `string_find_next_match(Str, ',', 2)`：下标 2 是 `'1'`、下标 3 是 `','`，命中 → `Index_v = 3`。
5. `Format_v.I := string_parse_int(Str, 4)`：从下标 4 的 `'4'` 开始，读到下标 5 的 `','` 停止 → 返回 `4`。
6. `string_find_next_match(Str, ',', 4)`：下标 5 是 `','`，命中 → `Index_v = 5`。
7. `Format_v.F := string_parse_int(Str, 6)`：下标 6 是 `'-'`（标记负数并前进），下标 7 是 `'2'`，下标 8 是 `')'` 停止 → 返回 `-2`。
8. `string_find_next_match(Str, ')', 6)`：下标 8 是 `')'`，命中 → `Index_v = 8`。`assert 8>0` 通过。
9. 返回 `Format_v = (S=>1, I=>4, F=>-2)`，即 `[1,4,-2]`。

**预期结果**：解析得到 `[1,4,-2]`，位宽 \(W = S+I+F = 1+4-2 = 3 \)。这是一道纸笔推演练习，**无需运行**即可完成；若要复核，可在测试台里调用 `cl_fix_format_from_string("(1,4,-2)")` 并 `report to_string(...)` 期望回印 `(1,4,-2)`。

> 待本地验证：上述步骤基于当前 HEAD 的源码逻辑手算得出，未在仿真器中实跑。

#### 4.5.5 小练习与答案

**练习 1**：若输入字符串漏掉右括号，写成 `"(1,4,-2"`，解析器会怎样？

**答案**：第 8 步的 `string_find_next_match(Str, ')', 6)` 会一直扫到串尾都没命中，返回 `-1`；随后的 `assert Index_v > 0` 失败，打印 `"cl_fix_format_from_string: Format string is missing ')'"` 并以 `Failure` 严重级别终止（见 [en_cl_fix_pkg.vhd:754-756](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L754-L756)）。

**练习 2**：为什么 `cl_fix_round_from_string` 要先 `toLower`，而 `cl_fix_format_from_string` 不需要？

**答案**：枚举名是字母，用户/文件可能写成任意大小写（`"Trunc_S"`、`"TRUNC_S"`），归一化为小写后比对更宽容；而格式串里的 `(`、`,`、`)`、数字、`-` 都是固定符号，没有大小写问题，故无需 `toLower`。

**练习 3**：`cl_fix_format_from_string` 对 `S` 字段用单字符判断（[L739-L745](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L739-L745)），对 `I`、`F` 却用 `string_parse_int`。为什么？

**答案**：因为 `S` 受类型约束只能是 0 或 1，永远是一位数字，直接读 `Str(Index_v+1)` 并判断 `'0'/'1'` 最简单；而 `I`、`F` 可以是任意整数（包括多位和负数，如本例的 `-2`），必须用支持多位与负号的 `string_parse_int`。

## 5. 综合实践

**任务**：把本讲的「序列化 + 反序列化」闭环串起来，做一次源码阅读级的端到端跟踪。

1. 选定一个格式，例如 `[1,4,-2]`，和一个舍入模式 `NonSymPos_s`。
2. 在源码中定位把它们各自转成字符串的函数：`to_string(FixFormat_t)`（[L695-698](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L695-L698)）产出 `"(1,4,-2)"`，`to_string(FixRound_t)`（[L700-714](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L700-L714)）产出 `"NonSymPos_s"`。
3. 再反向走一遍：`cl_fix_format_from_string("(1,4,-2)")`（[L729-758](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L729-L758)）应还原出 `(1,4,-2)`；`cl_fix_round_from_string("nonsympos_s")`（[L760-781](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L760-L781)）应还原出 `NonSymPos_s`（注意大小写不敏感）。
4. 画一张「值 ↔ 字符串」的双向映射表，并在每条箭头上标注用到的函数与它依赖的私有包工具（如 `string_find_next_match` / `string_parse_int` / `toLower`）。
5. **预期结果**：你会清楚看到，正反向各走一遍所依赖的底层零件都来自 `en_cl_fix_private_pkg`，且解析侧对错误输入（缺括号、缺逗号、未知枚举名）都通过 `assert ... severity Failure` 给出明确诊断。这是源码阅读型综合实践，**无需运行仿真**。

> 待本地验证：若环境允许，可在 VUnit 测试台里真正调用这四个函数并比对往返结果，以验证你的映射表。

## 6. 本讲小结

- 私有包 `en_cl_fix_private_pkg` 是主包的「内部零件库」，收纳与定点语义无关的通用工具，被主包整体 `use` 进来（[en_cl_fix_pkg.vhd:29](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L29)）。
- `choose` 扮演 VHDL-93 缺失的「表达式内三元运算符」，`to01`/`toInteger` 负责值域归一化，三者广泛用于常量声明与哨兵回退。
- `maximum`/`minimum` 之所以要自己实现，是因为 **VHDL-93 没有内置版本**；它们是 `union` 及各种格式预测函数的公共积木。
- 字符串三件套 `toLower` / `string_find_next_match` / `string_parse_int` 在不依赖任何外部库的前提下，搭出了一个极简解析器；其中 `toLower(character)`、`string_int_from_char` 等是包体私有辅助函数。
- `to_string(FixRound_t)` / `to_string(FixSaturate_t)` 手写 `case` 而不用 `'image`，原因是**部分综合工具不支持枚举类型的 `'image`**；而整数 `'image`（用于 `to_string(FixFormat_t)`）是安全的。
- `cl_fix_format_from_string` 通过「逐个定位分隔符 + 抽数值」把 `"(1,4,-2)"` 解析为 `(1,4,-2)`；枚举解析则借 `toLower` 实现大小写不敏感匹配。所有解析错误都用 `assert ... severity Failure` 精确报错。

## 7. 下一步学习建议

- 本讲讲清了「字符串 ↔ 值」的转换机制，这些函数的真实用武之地在 **u7（协同仿真验证流程）**：测试台通过文件 I/O 读 cosim 生成的格式文件与黄金数据，建议接着读 u7-l1 与 u7-l2，看 `cl_fix_format_from_string` 如何在 `en_cl_fix_fileio_pkg` 里被调用。
- 若你想了解 `to_string(slv, fmt)` 如何处理负 `I`/负 `F` 这类「隐含位」的特殊打印（本讲只点了它的存在，未展开），可重读 [en_cl_fix_pkg.vhd:638-693](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L638-L693)，结合 u2-l1 的位权重模型理解 `pre_v`/`post_v` 的来历。
- u8-l2 会系统梳理源码中针对各 EDA 工具链 bug 的工程化规避；本讲提到的「枚举 `'image` 不被综合支持」「VHDL-93 缺 `maximum`」正是该主题的早期实例，可作为预习。
