# 复数规则 PluralRules

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 Unicode 定义的六种复数类别（`zero/one/two/few/many/other`）分别代表什么，以及为什么不是所有语言都会用全这六种。
- 解释 `PluralOperands` 的六个字段（`i/v/w/f/t/c`）分别从数字的哪一部分提取出来，并理解「为什么 ICU4X 不接受 `f32`/`f64`」。
- 区分**基数（cardinal）**与**序数（ordinal）**两种复数规则，知道它们各自对应的数据 marker 和构造函数。
- 读懂 `PluralRules::category_for` 这条核心调用链：数字 → 操作数 → 规则求值 → 类别。
- 独立写出一个最小的复数判定程序，并用真实源码解释它为什么给出这个结果。

本讲承接 [u3-l3 十进制数字格式化]：那一讲我们用 `Decimal`/`FixedDecimal` 表示「一个精确的十进制数」，本讲则回答「这个数在某种语言里该用哪一种复数形式」。复数判定也是消息格式化（message formatting）的关键拼图——例如 `{count} items` 在英语里要根据 `count` 选 `1 item` 还是 `2 items`。

## 2. 前置知识

在进入源码前，先用直觉建立几个概念。

### 什么是「复数形式」

在英语里，数名词要分单数和复数：`1 item`、`2 items`。看起来只有「一」和「其它」两种情况，所以英语的复数系统很简单。但很多语言远比这复杂：

- **俄语**用四种形式：`1 месяц`（one）、`2 месяца`（few）、`5 месяцев`（many）、`1.5 месяца`（other）。
- **阿拉伯语**用六种形式：zero / one / two / few / many / other。
- **中文、日语、韩语、泰语**只有一种形式：other（它们不靠数字本身区分名词形态）。

Unicode 把这些形式标准化成六种类别，称为 **CLDR Plural Categories**。同一个数字在不同语言里会落到不同类别上。

### 「基数」和「序数」是两套不同的规则

这点最容易被忽略，但很重要：

- **基数（cardinal）**表达「数量」，对应「几个」：`3 doors`、`1 month`、`10 dollars`。
- **序数（ordinal）**表达「顺序」，对应「第几」：`1st place`、`2nd day`、`3rd floor`、`11th floor`。

同一个语言、同一个数字，基数和序数可能给出完全不同的类别。例如英语基数只有 `one/other` 两类，而英语序数却有 `one/two/few/other` 四类（`1st`→one, `2nd`→two, `3rd`→few, `4th`→other）。因此 ICU4X 把它们做成两条独立的规则、两份独立的数据。

### 「操作数（operands）」：把数字拆成规则能用的零件

CLDR 的复数规则不是写死的 `if n == 1`，而是一套小表达式语言，例如俄语 `one` 规则大致是：

\[ \text{one} \iff v = 0 \,\land\, i \bmod 10 = 1 \,\land\, i \bmod 100 \neq 11 \]

这里的 `v`、`i` 就是「操作数」。CLDR 定义了一组操作数符号：

| 符号 | 含义 |
|------|------|
| `n` | 源数字的绝对值（整数+小数，是个「数」） |
| `i` | 整数部分的数字 |
| `v` | 可见小数位数（**含**末尾零） |
| `w` | 可见小数位数（**不含**末尾零） |
| `f` | 可见小数数字本身（**含**末尾零） |
| `t` | 可见小数数字本身（**不含**末尾零） |
| `c`/`e` | 紧凑表示法（compact）的 10 的指数 |

举例：对 `1.50` 而言，`i=1, v=2, w=1, f=50, t=5`。注意 `v`/`f` 与 `w`/`t` 的差别只在「末尾零」上——这正是为什么浮点数（`f64`）做不了复数判定：`1.5` 和 `1.50` 在 `f64` 里是同一个值，但在英语里 `1.5 items` 与 `1.50 items` 的复数判定可能不同。所以 ICU4X 要求用 `Decimal` 或整数来提供「末尾零」信息。

> 前置讲义已建立的认知：`Decimal`（来自 `fixed_decimal`）是按「量级 + 数位」精确表示十进制数的类型；`compiled_data` 默认把数据编译进二进制；构造器因要在运行期加载数据故返回 `Result`。这些在本讲都会用到。

## 3. 本讲源码地图

本讲全部聚焦在 `components/plurals` 这个 crate（对外暴露为 `icu::plurals` 模块）。涉及的关键文件：

| 文件 | 作用 |
|------|------|
| `components/plurals/src/lib.rs` | 组件门面：定义 `PluralCategory`、`PluralRules`、核心方法 `category_for` 与 `categories`，以及 cardinal/ordinal 两套构造函数。 |
| `components/plurals/src/operands.rs` | 定义 `PluralOperands`，负责从整数、字符串、`Decimal` 提取 CLDR 操作数 `i/v/w/f/t/c`。 |
| `components/plurals/src/options.rs` | 定义 `PluralRuleType`（Cardinal/Ordinal）与 `PluralRulesOptions`。 |
| `components/plurals/src/provider.rs` | 数据结构定义：`PluralRulesData`（六个可选 Rule）、两个数据 marker `PluralsCardinalV1` / `PluralsOrdinalV1`。 |
| `components/plurals/src/provider/rules/runtime/resolver.rs` | 规则求值器：`test_rule` 解释执行一条规则，`get_value` 把操作数符号映射到 `PluralOperands` 字段。 |
| `components/plurals/src/provider/rules/runtime/ast.rs` | 规则的运行期表示（`Rule`、`Operand`、`Relation`），是数据里实际存储、零拷贝求值的形态。 |

一句话串起来：`lib.rs` 提供 API，`operands.rs` 把输入数字拆成操作数，`provider.rs` 装载规则数据，`resolver.rs` 用这些数据对操作数求值，最终回到 `lib.rs` 给出一个 `PluralCategory`。
