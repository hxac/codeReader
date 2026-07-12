# 表达式词法分析

## 1. 本讲目标

在前一讲里，我们建好了 SDB 的命令框架：命令表 + 主循环 + 分发。本讲我们开始为 SDB 增加一个真正有用的能力——**让用户在命令行里输入一个表达式，NEMU 帮忙算出它的值**。

例如，未来你希望能这样用：

```
(nemu) p $a0 + 0x10
```

让 NEMU 打印寄存器 `a0` 的值加上十六进制 `0x10` 的结果。要做到这件事，第一步不是「算」，而是先把这一串字符 `"$a0 + 0x10"` **切碎成一串有意义的「词」**：

| 字符片段 | 含义 |
| --- | --- |
| `$a0` | 寄存器名 |
| `+` | 加号运算符 |
| `0x10` | 十六进制数字 |

这个「把字符串切成词」的过程，就叫做**词法分析（lexical analysis，简称 lexing）**，切出来的每一个「词」叫做一个 **token（记号）**。

学完本讲，你应当能够：

1. 理解 NEMU 中 `rules` **规则表驱动的词法分析设计**——为什么用一张表来描述「怎么识别 token」。
2. 掌握 `make_token` 的**逐字符匹配与 token 记录流程**——扫描指针怎么前进、匹配失败怎么报错。
3. 知道 `TK_NOTYPE`、`TK_EQ` 等 **token 类型如何扩展**，并能动手为数字、寄存器、运算符增加新的 token 类型。

本讲只做「切词」，**不做求值**。求值（递归下降）是下一讲 `u2-l7` 的主题。

## 2. 前置知识

在进入源码前，先用通俗语言解释三个本讲要用到的基础概念。

### 2.1 什么是 token（记号）

用户输入是一串**没有结构的字符**。词法分析的任务，是按某种规则把这些字符归并成一个个**有类型的、不能再分的最小单位**，这就是 token。例如字符串 `"12 + 34"` 会被切成三个 token：数字 `12`、加号 `+`、数字 `34`。每个 token 至少要记录两样东西：

- **类型**（type）：它是数字？运算符？寄存器名？
- **字面值**（lexeme / 文本）：它在原文中具体是哪几个字符，比如 `"12"`、`"0x10"`、`"$a0"`。

> 一个直觉记忆：词法分析像「读句子时先把字分成词」，语法分析（下一讲）才像「把词拼成有结构的句子」。

### 2.2 正则表达式与 POSIX regex

NEMU 用**正则表达式**来描述「什么样的字符串算一个 token」。例如 `[0-9]+` 表示「一个或多个数字」。NEMU 使用 C 标准库里的 **POSIX regex** 系列 API，核心三个函数：

| 函数 | 作用 |
| --- | --- |
| `regcomp` | 把一个**正则字符串**编译成内部表示（`regex_t`），便于后续快速匹配。 |
| `regexec` | 用编译好的 `regex_t` 去匹配一段文本，返回是否匹配、匹配的位置。 |
| `regerror` | 当 `regcomp`/`regexec` 出错时，把错误码翻译成可读字符串。 |

> 想了解更多可在终端执行 `man regex`。源码里也写了一句同样的提示：`expr.c` 第 18-20 行的注释。

关键数据结构 `regmatch_t` 有两个成员：

- `rm_so`（match start offset）：匹配到的**起始偏移**（相对于传入的字符串）。
- `rm_eo`（match end offset）：匹配到的**结束偏移**（指向最后一个匹配字符的下一个位置）。

所以匹配到的子串长度 \( = \text{rm\_eo} - \text{rm\_so} \)。本讲后面会反复用到这两个字段。

### 2.3 表驱动设计（table-driven design）

NEMU 的词法分析采用「表驱动」思路：**把「识别规则」写在一张表里，匹配逻辑写一份通用代码去查这张表**。这样要新增一种 token，只需要「在表里加一行 + 在记录分支里加一个 case」，而**扫描主循环完全不用改**。这种设计与上一讲 `u2-l5` 的 `cmd_table` 命令表如出一辙——你应当已经熟悉这种风格。

## 3. 本讲源码地图

本讲几乎所有内容都集中在一个文件里：

| 文件 | 作用 |
| --- | --- |
| [src/monitor/sdb/expr.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/expr.c) | 表达式的**词法分析**（本讲）与求值（下一讲）全部在此。包含 `rules` 表、`init_regex`、`make_token`、`Token` 结构、`expr` 入口。 |
| [src/monitor/sdb/sdb.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/sdb.h) | 声明对外接口 `word_t expr(char *e, bool *success);` |
| [src/monitor/sdb/sdb.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/sdb.c) | `init_sdb()` 在此调用 `init_regex()` 完成正则预编译。 |
| [include/macro.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/macro.h) | 提供 `ARRLEN` 宏，用于自动计算 `rules` 表的条目数。 |
| [src/isa/riscv32/reg.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/reg.c) | 寄存器名表 `regs[]`，本讲实践任务里识别寄存器 token 时会用到这些名字。 |

> 提示：`expr.c` 是一个**大量留空、由学生补全**的 PA 文件。本讲会先讲清楚框架已经搭好的部分（`rules`、`init_regex`、`make_token` 骨架、`Token`），再带你动手补全它。

## 4. 核心概念与源码讲解

按数据流向，本讲拆成四个最小模块。建议按顺序读：先认识「产物」长什么样（Token 结构），再认识「识别规则」怎么描述（rules），再看「编译优化」（init_regex），最后看「主循环」把它们串起来（make_token）。

---

### 4.1 Token 结构与 token 类型枚举

#### 4.1.1 概念说明

词法分析的**产物**是一个 token 序列。所以第一个要回答的问题是：**每一个 token 在内存里用什么数据结构表示？** NEMU 的答案是 `Token` 结构：一个 `type` 字段表示「这是哪一类 token」，外加一个 `str` 字符数组保存它的字面值（比如数字 `"123"`、寄存器名 `"$a0"`）。

而 token 的「类型」用一个整数表示。这里有一个**非常巧妙的设计**：

- 单字符的运算符（如 `+` `-` `*` `/` `(` `)`）直接用**该字符的 ASCII 码**作为类型值。`+` 的类型就是 `'+'`（即 43）。
- 多字符的、无法用单个字符表示的类型（如空格、`==`、数字、寄存器名），则用从 **256** 开始的自定义枚举值。

为什么是 256？因为 ASCII 字符的取值范围是 \( [0, 255] \)，即 \( 2^8 - 1 \)。从 256（\( 2^8 \)）开始定义自定义类型，可以**保证不和任何单字符类型冲突**。这样做的好处是：下一讲做求值时，可以直接 `switch (token.type)` 里写 `case '+':`、`case '*':`，单字符运算符不用单独定义枚举常量，代码非常清爽。

#### 4.1.2 核心流程

token 类型的取值规则可以用下面的伪代码描述：

```
若 token 是单字符运算符  → type = 该字符的 ASCII 值（如 '+' → 43）
若 token 是多字符类别    → type = 从 256 开始的自定义枚举值
                           （TK_NOTYPE=256, TK_EQ=257, TK_DECIMAL=258, ...）
```

`Token` 结构本身只有两个字段，记录流程为：

```
识别到一个 token 时：
  tokens[nr_token].type = 该 token 的类型;
  若是数字/寄存器等"带值"token:
    把字面值拷贝到 tokens[nr_token].str;
  nr_token ++;   // 指向下一个空位
```

#### 4.1.3 源码精读

**token 类型枚举**，起始值从 256 开始：

[src/monitor/sdb/expr.c:L23-L28](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/expr.c#L23-L28) —— 定义 `TK_NOTYPE = 256`、`TK_EQ`，并留 `TODO` 给你新增类型。`TK_NOTYPE` 表示「空白字符」，匹配到后**不产生 token**（直接丢弃）。

**Token 结构**：

[src/monitor/sdb/expr.c:L65-L68](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/expr.c#L65-L68) —— `type` 是 int 类型；`str[32]` 用来保存字面值，长度 32 足够装下一个 32 位十六进制数（`"0xffffffff"` 仅 10 个字符）或寄存器名。

**存放 token 的全局数组与计数器**：

[src/monitor/sdb/expr.c:L70-L71](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/expr.c#L70-L71) —— `tokens[32]` 表示一条表达式最多切出 32 个 token；`nr_token` 记录当前已经切出多少个。`__attribute__((used))` 是为了让编译器在变量暂时未被引用时也不要报「未使用」警告（因为现在 `make_token` 还没真正写入它们）。

#### 4.1.4 代码实践

**实践目标**：亲手感受「单字符类型用 ASCII、自定义类型从 256 起」这条约定。

**操作步骤**：

1. 打开 `src/monitor/sdb/expr.c`，找到第 23-28 行的 `enum`。
2. 在注释 `/* TODO: Add more token types */` 下方，新增你计划用到的类型，例如：

   ```c
   /* 示例代码：新增 token 类型 */
   TK_DECIMAL, TK_HEX, TK_REG,
   ```

   （这是示例代码，按本讲惯例标注；后续模块会用到它们。）

3. 不需要编译运行，直接对照下面的「预期结果」回答问题。

**需要观察的现象 / 预期结果**：

- `TK_NOTYPE` 显式赋值 256，其后逗号分隔的枚举项自动递增：`TK_EQ = 257`、`TK_DECIMAL = 258`、`TK_HEX = 259`、`TK_REG = 260`……
- 思考并验证：如果把 `TK_NOTYPE` 改成从 `0` 开始，会发生什么冲突？（提示：`'+'` 的 ASCII 是 43，`'('` 是 40；只要自定义枚举值越过 40 就会和某个单字符运算符撞车，下一讲求值的 `switch` 就会判错类型。）

> 待本地验证：如果你好奇某个字符的 ASCII 值，可以写一段最小 C 程序 `printf("%d\n", '+');` 打印确认。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `TK_NOTYPE` 从 256 开始，而不是 0 或 1？

**参考答案**：因为单字符运算符直接用其 ASCII 码作为 `type`，ASCII 范围是 0–255。自定义类型从 256 开始可避免与任何单字符类型冲突，让求值阶段的 `switch` 可以同时处理 `case '+'` 和 `case TK_DECIMAL` 而互不干扰。

**练习 2**：`Token.str[32]` 里，哪些 token 需要写字面值，哪些不需要？

**参考答案**：数字（十进制、十六进制）和寄存器名**需要**写字面值，因为后续要把字符串转成数值。单字符运算符（`+ - * / ( )`）和 `==` **不需要**写字面值——它们的 `type` 本身就足以表达含义，`str` 留空即可。

---

### 4.2 rules 规则表

#### 4.2.1 概念说明

有了 token 的「产物结构」，下一个问题是：**怎么描述「什么样的字符串算哪一类 token」？** NEMU 的回答是一张 `rules` 表——每一行描述「一条正则 + 它对应的 token 类型」。这就是 2.3 节说的**表驱动设计**：规则是数据，扫描逻辑是代码，两者分离。

这张表有两点需要特别理解：

1. **规则的顺序很重要**。扫描时，代码会**从上到下**依次尝试每条规则，**第一个能匹配上的规则胜出**。因此，当两条规则可能匹配同一段文本时，必须把「更具体 / 更长」的规则写在前面。例如（实践任务里你会遇到）识别 `0x1f`：若把十进制规则 `[0-9]+` 写在十六进制 `0x[0-9a-fA-F]+` 之前，`0x1f` 会被当成十进制 `0` 先匹配走，造成错误。
2. **正则里特殊字符要在 C 字符串里转义**。例如字面量加号在正则里要写成 `\+`，而在 C 字符串里反斜杠本身要再转义一次，于是写成 `"\\+"`。

#### 4.2.2 核心流程

规则表里每一条的结构是：

```
{ 正则字符串, 该 token 的类型 }
```

当扫描指针停留在某个位置时，匹配流程为：

```
for 表里每一条规则 rules[i]（按顺序）:
    用 rules[i].regex 去匹配「从当前位置开始的剩余串」
    若匹配且起点就在当前位置 → 选中这条规则，token 类型 = rules[i].token_type
```

> 注意：第一个匹配上的就 break，后面的规则不再尝试。所以**顺序 = 优先级**。

#### 4.2.3 源码精读

**rules 表的定义**：

[src/monitor/sdb/expr.c:L30-L42](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/expr.c#L30-L42) —— 当前框架只给了三条规则作为示范：

- `{" +", TK_NOTYPE}` —— 一个或多个空格，类型为 `TK_NOTYPE`（空白，将被丢弃）。
- `{"\\+", '+'}` —— C 字符串 `"\\+"` 对应正则 `\+`，匹配字面量 `+`，类型直接用字符 `'+'`。
- `{"==", TK_EQ}` —— 匹配 `==`，类型为 `TK_EQ`。

注释 L35-37 提醒你「注意不同规则的优先级（precedence）」——正是 4.2.1 讲的顺序问题。

**用 `ARRLEN` 自动计算条目数**：

[src/monitor/sdb/expr.c:L44](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/expr.c#L44) —— `#define NR_REGEX ARRLEN(rules)`。

[include/macro.h:L29](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/macro.h#L29) —— `ARRLEN` 的定义是 `sizeof(arr) / sizeof(arr[0])`，即「整个数组字节数 ÷ 一个元素字节数 = 元素个数」。这样你往 `rules` 里加几行，`NR_REGEX` 会自动更新，扫描循环不用改任何数字。这和 `u2-l5` 里 `NR_CMD ARRLEN(cmd_table)` 是同一个手法。

#### 4.2.4 代码实践

**实践目标**：体会「规则顺序 = 优先级」以及 C 字符串里的正则转义。

**操作步骤**：

1. 在 `rules[]` 里临时加一条十进制规则（**示例代码**）：

   ```c
   {"[0-9]+", TK_DECIMAL},    // 十进制数（临时演示，正式放综合实践）
   ```

2. 暂时**不要**编译运行，先在脑海里推演：对输入 `"0x1f"`，扫描到第 0 个字符 `0` 时，`[0-9]+` 会匹配到什么？

**需要观察的现象 / 预期结果**：

- `[0-9]+` 会贪婪匹配 `0`（因为 `x` 不是数字），匹配长度为 1。于是 `0x1f` 被错误地切成 `0`、`x`（无法识别）……
- 结论：等做综合实践时，**十六进制规则 `0x[0-9a-fA-F]+` 必须写在十进制规则 `[0-9]+` 之前**，才能让 `0x1f` 整体被识别为一个十六进制 token。

> 待本地验证：综合实践（第 5 节）会让你真正加上所有规则并跑通；这里只做静态推演。

#### 4.2.5 小练习与答案

**练习 1**：写出「十六进制数」「寄存器名（`$` 开头）」「减号」「左括号」四条规则。

**参考答案**（示例代码）：

```c
{"0x[0-9a-fA-F]+", TK_HEX},   // 十六进制数，必须放在十进制规则之前
{"\\$[A-Za-z0-9]+", TK_REG},  // 寄存器名：C 串 "\\$" → 正则 \$ → 字面量 $
{"-", '-'},                    // 减号，类型用字符 '-'
{"\\(", '('},                  // 左括号：正则 \( → 字面量 (
```

**练习 2**：为什么 `+` 的规则写成 `"\\+"`，而 `==` 直接写成 `"=="`？

**参考答案**：因为 NEMU 用 `REG_EXTENDED`（扩展正则，见 4.3 节），在扩展正则里 `+` 是「重复一次或多次」的特殊字符，要表示字面量加号必须转义为 `\+`，对应 C 字符串 `"\\+"`。而 `=` 在正则里不是特殊字符，`==` 就是它本身，无需转义。

---

### 4.3 init_regex 预编译

#### 4.3.1 概念说明

正则表达式如果在每次匹配时都临时解析，会很慢。POSIX regex 的做法是：先用 `regcomp` 把正则字符串**编译**成一种内部表示（`regex_t`），之后用 `regexec` 直接拿编译好的结果去匹配，速度快得多。

由于 NEMU 的 `rules` 表在运行期间是**固定不变**的，所以最划算的策略是：**程序启动时把所有规则各编译一次，之后反复用**。这正是 `init_regex` 的职责——它把「编译一次」这件事集中在一个函数里完成。

#### 4.3.2 核心流程

```
init_regex():
  for 表里每一条规则 rules[i]:
    用 regcomp 把 rules[i].regex 编译进 re[i]（使用 REG_EXTENDED 扩展正则）
    若编译失败（返回非 0）:
      用 regerror 取得错误信息
      panic 终止（规则写错了是程序员 bug，必须立刻暴露）
```

编译好的 `regex_t` 数组 `re[]` 在整个程序生命周期内有效，无需 `regfree`（因为 `re` 是 static 的，随进程结束自动回收）。

#### 4.3.3 源码精读

**预编译好的正则数组**：

[src/monitor/sdb/expr.c:L44-L46](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/expr.c#L44-L46) —— `NR_REGEX` 条 `regex_t re[]`，初始为空 `{}`，等待 `init_regex` 填充。

**init_regex 函数**：

[src/monitor/sdb/expr.c:L51-L63](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/expr.c#L51-L63) —— 遍历每条规则，调用：

- `regcomp(&re[i], rules[i].regex, REG_EXTENDED)`：第二个参数是正则字符串，第三个 `REG_EXTENDED` 表示用**扩展正则语法**（`+`、`?`、`|`、`()` 等无需反斜杠即为特殊含义）。
- 若 `ret != 0`：调用 `regerror(ret, &re[i], error_msg, 128)` 把错误码翻译成可读字符串，再用 `panic` 打印错误信息和出错的正则，终止程序。

L48-50 的注释说明了设计意图：「规则会被使用很多次，因此只在首次使用前编译一次。」

**谁调用 init_regex**：

[src/monitor/sdb/sdb.c:L137-L143](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/sdb.c#L137-L143) —— `init_sdb()` 第一件事就是 `init_regex()`（其次是初始化监视点池）。回忆 `u1-l3`，`init_sdb` 是 `init_monitor` 初始化链中的一环，所以**正则编译发生在程序启动阶段、进入命令循环之前**，时机正好。

#### 4.3.4 代码实践

**实践目标**：直观感受「正则写错会被 init_regex 立刻拦截」。

**操作步骤**：

1. 在 `rules[]` 里临时塞一条**故意写错**的正则（**示例代码**，验证完务必删掉）：

   ```c
   {"[0-9", TK_DECIMAL},   // 故意少写右方括号，这是非法正则
   ```

2. 重新编译并运行 NEMU（不必加载程序，启动阶段就会触发）。
3. 观察终端输出后，**删掉这条错误规则**。

**需要观察的现象 / 预期结果**：

- 程序启动时立即 `panic`，打印类似 `regex compilation failed` 的信息，并附上出错的那条正则 `[0-9`。
- 这验证了 `init_regex` 的「快速失败」设计：规则表的错误属于编译期/启动期 bug，越早暴露越好。

> 待本地验证：具体 panic 文本格式取决于 `panic` 实现，但一定会包含你写错的那条正则字符串，便于定位。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `re[]` 不需要调用 `regfree` 释放？

**参考答案**：`re` 是文件作用域的 static 数组，生命周期与整个进程相同；规则在运行期不变，编译一次后一直要用到程序退出。进程结束时操作系统会统一回收内存，所以无需手动 `regfree`。

**练习 2**：如果删掉 `REG_EXTENDED` 标志，`{"\\+", '+'}` 这条规则还能正确匹配 `+` 吗？

**参考答案**：仍然能。`\+` 在基本正则（BRE）和扩展正则（ERE）里都表示字面量加号。但删掉 `REG_EXTENDED` 会影响**你新增的规则**——例如实践任务里类似 `[0-9]+` 的 `+` 在 BRE 里需写成 `[0-9]\+` 才是「重复」，否则 `+` 被当字面量。所以保持 `REG_EXTENDED` 能让你写更直观的正则。

---

### 4.4 make_token 主匹配循环

#### 4.4.1 概念说明

前三个模块备齐了「产物结构（Token）」「识别规则（rules）」「编译好的正则（re）」。`make_token` 是把它们**串起来的核心**：它拿着用户输入的字符串，从头到尾**逐段扫描**，每一段去查 `rules` 表，识别出一个 token 就记录到 `tokens[]` 数组里，扫描指针向前推进，直到字符串末尾。

这里有一个**最关键的细节**：`regexec` 默认**不是锚定的**——它会在传入的整段字符串里找「任意位置」的第一个匹配。但词法分析要求「必须从当前位置开始匹配」。所以 NEMU 用一个小技巧：传入 `e + position`（从当前位置开始的剩余串），并**额外检查 `pmatch.rm_so == 0`**（匹配的起始偏移恰好是 0，也就是当前位置）来强制「锚定」。少了这个检查，就会把「后面才出现的匹配」误当成「当前位置的匹配」。

#### 4.4.2 核心流程

`make_token` 的主循环伪代码：

```
make_token(e):
  position = 0          // 扫描指针
  nr_token = 0          // 已切出的 token 数
  while e[position] != '\0':           // 还没到字符串末尾
    选中 = false
    for i in 0 .. NR_REGEX:            // 依次试每条规则
      若 regexec(re[i], e+position) 成功 且 pmatch.rm_so == 0:   // 锚定在当前位置
        substr_len = pmatch.rm_eo      // 因为 rm_so==0，长度就是 rm_eo
        position += substr_len          // 指针前进
        switch rules[i].token_type:
          case TK_NOTYPE: 不记录（空白丢弃）
          case 数字/寄存器: 把字面值拷进 tokens[nr_token].str，记录类型，nr_token++
          case '+','-',...: 仅记录类型，nr_token++
        选中 = true
        break                          // 选中一条就不再试后面的规则
    if not 选中:                        // 所有规则都匹配不了当前位置
      打印错误位置（带 ^ 指示符）
      return false                     // 词法错误
  return true                          // 全部切完，成功
```

注意循环结束判断里的一个 C 语言细节：内层 `for` 若被 `break` 提前退出，循环变量 `i` 会停在「选中的那条规则」上（`i < NR_REGEX`）；若一直没 `break`、自然结束，则 `i == NR_REGEX`。外层正是用 `if (i == NR_REGEX)` 来判断「一条都没匹配上」。

#### 4.4.3 源码精读

**make_token 函数签名与状态初始化**：

[src/monitor/sdb/expr.c:L73-L78](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/expr.c#L73-L78) —— `position` 是扫描指针，`pmatch` 用来接收匹配位置，`nr_token = 0` 每次重新清零（保证 `make_token` 可重复调用）。

**主循环：逐字符扫描**：

[src/monitor/sdb/expr.c:L80-L83](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/expr.c#L80-L83) —— `while (e[position] != '\0')` 驱动扫描；内层 `for` 依次尝试每条规则，关键调用是：

```c
regexec(&re[i], e + position, 1, &pmatch, 0) == 0 && pmatch.rm_so == 0
```

两个条件缺一不可：`== 0` 表示匹配成功，`pmatch.rm_so == 0` 表示**匹配起点就在当前位置**（锚定）。

**截取子串与调试日志**：

[src/monitor/sdb/expr.c:L84-L90](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/expr.c#L84-L90) —— `substr_start` 指向匹配起点，`substr_len = pmatch.rm_eo`（因为锚定 `rm_so==0`，所以长度就是 `rm_eo`）；随后打印一条 `Log` 显示「第几条规则、在哪个位置、匹配了多长、内容是什么」——这是**最重要的调试手段**，调词法时全靠看这些 Log。最后 `position += substr_len` 让指针前移，准备识别下一个 token。

**TODO：记录 token**：

[src/monitor/sdb/expr.c:L92-L101](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/expr.c#L92-L101) —— 注释明确告诉你「现在用 rules[i] 识别出了一个新 token，请补代码把它记进 `tokens[]`；某些类型的 token 还要做额外动作」。目前的 `switch` 只有一个 `default: TODO()`，意味着**只要匹配到任何一个 token 就会触发 TODO 停下**——这正是你要补全的地方（见下方实践）。`break` 跳出内层 `for`，不再试后续规则。

**匹配失败的报错**：

[src/monitor/sdb/expr.c:L105-L108](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/expr.c#L105-L108) —— 若 `i == NR_REGEX`（所有规则都没匹配当前位置），打印出错位置并用 `^` 指示符指出来，返回 `false`。例如输入里有 `@` 这种规则里没定义的字符，就会在这里报「no match at position N」。

#### 4.4.4 代码实践

**实践目标**：补全 `make_token` 的 `switch`，让词法分析真正能切出 token，并观察 Log 调试输出。

**操作步骤**（基于框架已有的 `{" +", TK_NOTYPE}`、`{"\\+", '+'}`、`{"==", TK_EQ}` 三条规则）：

1. 把第 97-99 行的 `switch` 改成（**示例代码**）：

   ```c
   switch (rules[i].token_type) {
     case TK_NOTYPE: break;   // 空白：丢弃，不记录
     default:
       tokens[nr_token].type = rules[i].token_type;
       // 对于单字符运算符/==，type 就够；这里统一记录类型
       nr_token++;
       break;
   }
   ```

   （此时还没有数字/寄存器规则，所以暂时不需要往 `str` 里写值；带值 token 的处理放在第 5 节综合实践。）

2. 在 `expr` 函数（[L115-L125](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/expr.c#L115-L125)）的 `make_token(e)` 成功之后、`TODO()` 之前，临时加一段调试打印（**示例代码**）：

   ```c
   for (int i = 0; i < nr_token; i++) {
     printf("token[%d]: type=%d\n", i, tokens[i].type);
   }
   ```

3. 编译运行 NEMU（先确保日志开启，能看到 `Log` 输出）。

**需要观察的现象 / 预期结果**：

- 当 `make_token` 处理 `"  + =="` 时，会依次打印多行 `Log`：先匹配到空格（`TK_NOTYPE`），再 `+`，再空格，再 `==`。
- 你临时加的 `printf` 会打印出：跳过空白后，记录了 `+`（type=43）和 `==`（type=257=TK_EQ）两个 token。
- 输入 `"1+2"` 会看到「no match at position 0」——因为还没有数字规则，`1` 无法被识别。这正是下一节综合实践要解决的。

> 待本地验证：`Log` 是否输出到终端取决于 `nemu-log.txt` 配置与日志等级（详见 `u8-l25`）。若没看到 Log，可检查日志设置。

#### 4.4.5 小练习与答案

**练习 1**：如果把判断条件里的 `&& pmatch.rm_so == 0` 去掉，对输入 `"1+2"`（假设已有数字规则）会造成什么问题？

**参考答案**：`regexec` 默认在整段剩余串里找第一个匹配，不要求从起点开始。去掉 `rm_so == 0` 后，当指针停在某个位置、该位置的字符自己不匹配任何规则，但**后面**出现了可匹配的字符时，会被误判为「匹配成功」并错误地跳过中间字符，导致词法错误被掩盖、token 位置错乱。`rm_so == 0` 是把匹配**锚定到当前位置**的关键。

**练习 2**：`tokens[32]` 只能存 32 个 token。如果用户输入一个超长表达式（超过 32 个 token）会怎样？应该如何防御？

**参考答案**：会数组越界，行为未定义。应在每次 `nr_token++` 前加边界检查，例如 `if (nr_token >= 32) return false;`（或用 `ARRLEN(tokens)` 代替硬编码 32），越界时返回 `false` 表示词法失败。

**练习 3**：`Log("match rules[%d] ...")` 这行（L87-88）在最终产品里似乎「多余」，为什么 PA 仍保留它？

**参考答案**：它是**调试利器**。词法分析最容易出错（正则写错、顺序写反、锚定遗漏），而这行 Log 会逐个 token 报告「命中第几条规则、位置、长度、内容」，让你一眼看出切词是否正确。`u8-l25` 会讲到 NEMU 如何用日志窗口（`TRACE_START/END`）控制这类输出的开销。

---

## 5. 综合实践

把本讲的四个模块串起来，完成 PA 规定的词法分析扩展任务。这是本讲的主实践，也是下一讲表达式求值的前置准备。

### 5.1 实践目标

让 `make_token` 能正确识别以下几类 token：**十进制数、十六进制数、寄存器名（`$` 开头）、加减乘除、括号**，并把数字和寄存器的字面值记录到 `token.str` 中。

### 5.2 操作步骤

1. **扩展 token 类型枚举**（4.1 模块）。在 `expr.c` 第 23-28 行的 `enum` 中加入：

   ```c
   /* 示例代码 */
   TK_DECIMAL, TK_HEX, TK_REG,
   ```

2. **扩展 rules 表**（4.2 模块）。在 `rules[]` 里按**正确顺序**新增规则（顺序很关键）：

   ```c
   /* 示例代码：注意顺序 */
   {"0x[0-9a-fA-F]+", TK_HEX},     // 十六进制，必须在前
   {"[0-9]+",        TK_DECIMAL},  // 十进制
   {"\\$[A-Za-z0-9]+", TK_REG},    // 寄存器名：$0, $a0, $ra ...
   {"\\+", '+'},                   // 加（框架已有）
   {"-",    '-'},                  // 减
   {"\\*", '*'},                   // 乘（* 在正则里要转义）
   {"/",    '/'},                  // 除
   {"\\(", '('},                   // 左括号
   {"\\)", ')'},                   // 右括号
   {"==",   TK_EQ},                // 等于（框架已有，求值时用）
   ```

   > 思考：为什么 `0x...` 必须在 `[0-9]+` 之前？为什么 `\*` 要转义而 `/` 不用？（答案见 4.2.5）

3. **补全 make_token 的 switch**（4.4 模块）。对于带值 token，把字面值拷进 `str`：

   ```c
   /* 示例代码 */
   switch (rules[i].token_type) {
     case TK_NOTYPE: break;   // 空白丢弃
     case TK_DECIMAL:
     case TK_HEX:
     case TK_REG:
       tokens[nr_token].type = rules[i].token_type;
       // substr_start / substr_len 在上方已算好（L84-L85）
       // 注意按 str[32] 截断，防止越界
       if (substr_len >= sizeof(tokens[nr_token].str)) substr_len = sizeof(tokens[nr_token].str) - 1;
       strncpy(tokens[nr_token].str, substr_start, substr_len);
       tokens[nr_token].str[substr_len] = '\0';
       nr_token++;
       break;
     default:
       // 单字符运算符与 TK_EQ：只记类型
       tokens[nr_token].type = rules[i].token_type;
       nr_token++;
       break;
   }
   ```

   并在 `nr_token++` 前加 `if (nr_token >= ARRLEN(tokens)) return false;` 防止越界。

4. **验证识别结果**。临时在 `expr` 里（`make_token` 成功后）打印 token 流（参考 4.4.4），用以下输入测试：

   | 输入 | 期望切出的 token |
   | --- | --- |
   | `1+2` | DECIMAL("1"), '+', DECIMAL("2") |
   | `0x10 * ($a0 - 3)` | HEX("0x10"), '*', '(', REG("$a0"), '-', DECIMAL("3"), ')' |
   | `==` | TK_EQ |
   | `@` | `no match at position 0`（词法错误） |

5. **可选：接入 SDB 的 `p` 命令**。在 `sdb.c` 的 `cmd_table`（参考 `u2-l5`）里新增 `p` 命令，调用 `expr(args, &success)` 并打印结果。本讲只需 `make_token` 成功即可（`expr` 的求值部分 `TODO()` 留给下一讲）。

### 5.3 需要观察的现象

- `nemu-log.txt`（或终端）里的 `Log` 行应逐 token 报告命中规则，顺序与你输入一致。
- 对带值 token，`token.str` 中应能看到 `"1"`、`"0x10"`、`"$a0"` 等字面值。
- 对非法字符应触发「no match at position N」并带上 `^` 指示。

### 5.4 预期结果

- 所有合法输入都能被完整切分为 token 流，`make_token` 返回 `true`。
- 规则顺序错误（如 `0x...` 写在 `[0-9]+` 之后）会导致 `0x10` 被切成 `DECIMAL("0")` 后紧跟无法识别的 `x`——这时应回头检查顺序。

> 待本地验证：寄存器名的具体拼写（`$0`、`a0`、`ra` 等）以 [src/isa/riscv32/reg.c:L19-L24](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/reg.c#L19-L24) 的 `regs[]` 表为准；下一讲求值时会把 `str` 里的寄存器名交给 `isa_reg_str2val` 解析（见 `u5-l15`）。

## 6. 本讲小结

- **词法分析**是把用户输入的字符串切成一串 token 的过程；本讲只做「切词」，不做「求值」。
- **表驱动设计**：`rules[]` 表把「识别规则」写成数据（`{正则, token 类型}`），扫描逻辑 `make_token` 通用且无需改动，新增 token 只需「加表项 + 加 case」。
- **token 类型编码**：单字符运算符直接用 ASCII 码，自定义类型从 256 起，二者天然不冲突，求值时可统一 `switch`。
- **规则顺序即优先级**：`regexec` 依次试规则、第一个匹配胜出，故 `0x...` 须写在 `[0-9]+` 之前。
- **预编译优化**：`init_regex` 用 `regcomp` 在启动时把每条正则编译一次，之后高速复用；正则写错会立即 `panic`。
- **锚定匹配**：`make_token` 靠传入 `e+position` 并检查 `pmatch.rm_so == 0` 强制「从当前位置开始匹配」，这是最容易出错也最关键的细节。

## 7. 下一步学习建议

本讲结束时，`make_token` 已经能把字符串切成 token 流，但 `expr` 里的求值部分仍是 `TODO()`（[expr.c:L121-L124](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/expr.c#L121-L124)）。

- **下一讲 `u2-l7 表达式求值（递归下降）`** 将基于本讲的 token 流，用递归下降算法算出表达式值，并处理运算符优先级、括号、一元负号、寄存器与内存解引用；还会用 `tools/gen-expr/gen-expr.c` 做随机对比测试。请确保本讲的 `make_token` 已能正确切出带值 token，否则求值无从谈起。
- **横向衔接**：寄存器 token 的字面值最终由 `isa_reg_str2val` 解析（`u5-l15`）；内存解引用 `*0x80000000` 形式的求值会用到 `vaddr_read`（`u4-l13`）。可先记住这两处联系，学到对应章节时再回看。
- **建议阅读的源码**：本讲之外，建议浏览 `tools/gen-expr/gen-expr.c`（下一讲要用）以及 `man regex`，加深对 POSIX regex 的理解。
