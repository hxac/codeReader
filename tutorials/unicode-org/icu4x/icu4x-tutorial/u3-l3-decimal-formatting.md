# 十进制数字格式化（icu_decimal / DecimalFormatter）

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 `DecimalFormatter` 接收什么输入、内部存了什么数据、产出什么结果。
- 解释「分组分隔符（千分位）」「正负号」是如何由 locale 数据驱动并被 `grouper` 模块计算的。
- 理解 numbering system（数字系统）如何决定 0–9 这十个数字的字形，以及 ICU4X 为什么把「符号」和「数字字形」拆成两份数据。
- 亲手写一段代码，在 `de-DE`、`ar-EG`、`th-u-nu-thai` 等 locale 下格式化同一个数，观察输出差异。

本讲承接 [u2-l1 Locale 与 LanguageIdentifier 数据模型](u2-l1-locale-model.md)：locale 是格式化的输入，而本讲展示一个 locale 如何被翻译成「具体的分隔符、符号与字形」。

## 2. 前置知识

在进入源码前，先建立三个直觉。

### 2.1 为什么不直接格式化 `f64`？

国际化数字格式化关心的不是「数值大小」，而是「每一个十进制位」。比如要插入千分位、判断复数、对齐小数点，都必须能逐位访问数字。浮点数 `f64` 无法精确表示 `0.1` 这样的十进制小数，也不提供「第几位是几」的接口。因此 ICU4X 不接收 `f64`，而是接收一个**按数位（magnitude）索引的十进制类型** `Decimal`。

### 2.2 magnitude（量级）是什么？

ICU4X 把一个十进制数看成「每个数字位都有一个量级」的映射，量级就是该位对应的 10 的幂。以 `12.34` 为例：

| 量级 (magnitude) | 数字 | 含义 |
|---|---|---|
| 1 | 1 | 十位 |
| 0 | 2 | 个位 |
| -1 | 3 | 十分位 |
| -2 | 4 | 百分位 |

量级为正的是整数部分，为负的是小数部分。`0` 是个位。本讲会反复用到「从高量级到低量级遍历」这个动作。

### 2.3 数据驱动：格式化器自己不带「知识」

`DecimalFormatter` 本身不写死任何 locale 规则。它只是把 locale 数据（「德语用 `.` 当千分位」「阿拉伯语用 `٠` 当零」）加载进来，再按统一算法套用。这正是 [u1-l4](u1-l4-metacrate-and-features.md) 讲过的「compiled data / DataProvider 可插拔」在数字组件上的具体体现。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [components/decimal/src/lib.rs](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/decimal/src/lib.rs) | crate 入口，给出三段官方示例，并 re-export `DecimalFormatter` 与 `input` 模块 |
| [components/decimal/src/decimal_formatter.rs](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/decimal/src/decimal_formatter.rs) | `DecimalFormatter` 主体：构造、`format`、以及真正写数字的 `Writeable` 实现 |
| [components/decimal/src/grouper.rs](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/decimal/src/grouper.rs) | 决定「在哪一位插分组分隔符」的纯函数 `check` |
| [components/decimal/src/options.rs](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/decimal/src/options.rs) | `DecimalFormatterOptions` 与 `GroupingStrategy` 枚举 |
| [components/decimal/src/provider.rs](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/decimal/src/provider.rs) | 数据结构定义：`DecimalSymbols`、`GroupingSizes`、两个数据 marker |
| [components/decimal/src/parts.rs](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/decimal/src/parts.rs) | 富文本片段标注常量（整数/小数/分组/符号） |
| [utils/fixed_decimal/src/decimal.rs](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/utils/fixed_decimal/src/decimal.rs) | 输入类型 `UnsignedDecimal`：按量级存数位的底层表示 |

## 4. 核心概念与源码讲解

### 4.1 DecimalFormatter 入口 API

#### 4.1.1 概念说明

`DecimalFormatter` 做的事情用一句话概括：**把一个 `Decimal` 渲染成符合某 locale 习惯的字符串**。它解决三个子问题：

1. 用本地数字系统渲染（孟加拉语 `১`、泰语 `๑`、阿拉伯语 `١`……）。
2. 在正确的位置插入分组分隔符（西方的 `1,000,000`、印度的 `1,00,000`）。
3. 渲染本地化的正负号。

它的输入 `Decimal` 并不来自本 crate，而是来自 `fixed_decimal` 工具 crate（见 [u6-l6 fixed_decimal 等小工具](u6-l6-tinystr-litemap-fixed-decimal.md)），在本 crate 里只是 re-export：

[components/decimal/src/lib.rs:137-145](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/decimal/src/lib.rs#L137-L145) —— `input` 模块把 `fixed_decimal::Decimal` 重新暴露给用户。

而 `Decimal` 本质上是一个带符号的包装：

[utils/fixed_decimal/src/signed_decimal.rs:49](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/utils/fixed_decimal/src/signed_decimal.rs#L49) —— `pub type Decimal = Signed<UnsignedDecimal>;`

[utils/fixed_decimal/src/variations.rs:53-59](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/utils/fixed_decimal/src/variations.rs#L53-L59) —— `Signed` 只是把 `sign`（正/负/无）和 `absolute`（无符号数）打包。所以 `Decimal` = 「一个符号 + 一个按量级索引的无符号十进制数」。

#### 4.1.2 核心流程

`DecimalFormatter` 的使用分两个阶段（和 [u3-l1 DateTime](u3-l1-datetime-formatting.md) 的套路完全一致）：

```text
阶段一：构造（加载数据，返回 Result）
  locale + options
     │  try_new / try_new_unstable
     ▼
  DecimalFormatter { options, symbols, digits }
                      │        │         └─ 10 个数字字形（按 numbering system）
                      │        └─ 分隔符 / 符号 / 分组尺寸（按 locale）
                      └─ 用户传入的分组策略

阶段二：格式化（惰性，不分配字符串直到真正写入）
  formatter.format(&decimal)
     │  返回 FormattedDecimal（一个借用了 formatter 的轻量对象）
     ▼
  实现 Writeable：写入 sink 时才逐位生成字符
     │  write_to_string() / println!("{}", ...) 触发
     ▼
  "১০,০০,০০৭"  /  "-1.234.567,89"  / ...
```

关键点：`format()` 几乎不做事，只返回一个**惰性的** `FormattedDecimal`。真正的字符生成发生在它被写入（`Display`、`write_to_string`）的时候。这种「先建树、后求值」的设计来自 `writeable` crate（详见 [u6-l5 writeable](u6-l5-writeable.md)），好处是 `LengthHint` 能精确预分配缓冲区，且同一次格式化既能输出字符串、也能流式写出、还能带片段标注。

#### 4.1.3 源码精读

先看 `DecimalFormatter` 结构体本身——只有三个字段：

[components/decimal/src/decimal_formatter.rs:36-40](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/decimal/src/decimal_formatter.rs#L36-L40) —— `options`（用户选项）、`symbols`（locale 的符号数据）、`digits`（数字系统的 10 个字形）。`size_test!(DecimalFormatter, ..., 96)` 断言整个格式化器只占 96 字节，体现 ICU4X 对小体积的执着（见 [u1-l1 设计目标](u1-l1-project-overview.md)）。

再看构造函数 `try_new_unstable` 的核心：它要分别加载「符号」和「字形」两份数据。这里先不纠缠 numbering system 解析细节（那是 4.3 节的主题），只看它最终把数据塞进结构体：

[components/decimal/src/decimal_formatter.rs:55-61](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/decimal/src/decimal_formatter.rs#L55-L61) 与 [components/decimal/src/decimal_formatter.rs:106-110](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/decimal/src/decimal_formatter.rs#L106-L110) —— 接收 `provider`、`prefs`、`options`，分别 load 出 `symbols` 与 `digits` 两个 `DataPayload`，组装返回。

> 名词解释：`gen_buffer_data_constructors!` 宏（第 49 行）会自动生成 `try_new`（用 compiled data）、`try_new_with_buffer_provider`（用 serde，需 `serde` feature）等一系列构造函数，它们最终都委托给手写的 `try_new_unstable`。这和 [u1-l4](u1-l4-metacrate-and-features.md) 讲的「compiled data vs 显式 provider 两种构造方式」是一回事。

然后是 `format`，它把工作拆成「符号」和「无符号数」两半：

[components/decimal/src/decimal_formatter.rs:114-119](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/decimal/src/decimal_formatter.rs#L114-L119) —— `format` 先对 `value.absolute`（无符号部分）调用 `format_unsigned`，再用 `format_sign` 把符号包在外面。这种「正负号 = 前缀 + 数值 + 后缀」的拆分很关键：很多 locale 的负号不是简单一个 `-`，而是围绕数字的前后缀（见 4.2）。

真正逐位写数字的逻辑在 `FormattedUnsignedDecimal` 的 `Writeable` 实现里，这是本组件的心脏：

[components/decimal/src/decimal_formatter.rs:200-239](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/decimal/src/decimal_formatter.rs#L200-L239) —— 阅读这段循环：

1. 取 `magnitude_range()`，用 `.rev()` **从高量级到低量级**遍历（即从最左位写到最右位）。
2. 对每个 `m >= 0` 的量级（整数部分）：用 `digit_at(m)` 取出该位数字 `0..=9`，再把它当索引查 `self.digits[d ... ]` 得到本数字系统下的字符（关键一步：字形替换就在这）。
3. 每写完一位，调 `grouper::check(...)` 判断该不该在这里插分组分隔符，该插就写入 `symbols.grouping_separator()`。
4. 整数部分写完后，如果还有更低的量级（小数部分），先写小数点 `decimal_separator()`，再继续写小数位（小数位不再分组）。

注意第 211 行注释 `// digit_at in 0..=9`：`digit_at` 永远返回 0–9，所以拿它当数组下标是安全的，这也是源码敢用 `indexing_slicing`（通常被 deny）的原因。

#### 4.1.4 代码实践

**实践目标**：跑通官方「带小数的数字」示例，亲眼看到千分位与小数点。

**操作步骤**：

1. 按 [u1-l3](u1-l3-first-app-quickstart.md) 的方式 `cargo new --bin mydecimal && cd mydecimal && cargo add icu`。
2. 把 `src/main.rs` 改成下面这段（基本是 crate 文档里的第二个示例）：

   ```rust
   use icu::decimal::input::Decimal;
   use icu::decimal::DecimalFormatter;

   fn main() {
       let formatter =
           DecimalFormatter::try_new(Default::default(), Default::default())
               .expect("locale should be present");

       let decimal = {
           let mut d = Decimal::from(200050);
           d.multiply_pow10(-2); // 200050 -> 2000.50
           d
       };

       println!("{}", formatter.format(&decimal));
   }
   ```

   （`Default::default()` 作为 locale 等价于根 locale `und`，近似英语习惯。）

**需要观察的现象**：输出应为 `2,000.50`——千分位把 `2000` 隔成 `2,000`，小数点是 `.`，末尾的 `0` 被保留（因为 `Decimal` 是精确的按位表示，不会丢 `0.50` 末尾的零）。

**预期结果**：`2,000.50`。这正是 [components/decimal/src/lib.rs:44-51](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/decimal/src/lib.rs#L44-L51) 里官方示例断言的值。若你的输出不同，请检查是否启用了 `compiled_data` feature（默认开启）。

> 本实践基于源码自带的 doctest，行为已由项目断言；但你本机的运行结果仍以实际输出为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `DecimalFormatter::try_new` 返回 `Result`，而 `locale!("en")` 不返回 `Result`？

**答案**：`locale!` 宏在编译期解析字符串、只做语法检查（见 [u2-l1](u2-l1-locale-model.md)），不可能失败故无 `Result`；而 `try_new` 要在运行期加载 CLDR 数据，数据可能缺失（locale 不存在、numbering system 无数据），所以必须返回 `Result`。

**练习 2**：`format()` 返回的 `FormattedDecimal` 如果从不被 `println!` 或 `write_to_string` 使用，会发生什么？

**答案**：什么都不会输出。`FormattedDecimal` 只是持有引用的惰性对象，字符生成完全推迟到写入时（`Writeable::write_to_parts`），不写入就不产生字符串、不分配。

---

### 4.2 分组（grouper）与符号处理

#### 4.2.1 概念说明

「分组」就是千分位——但远不止「每三位一个逗号」这么简单。世界上至少有两套主流分组规则：

- **西方分组**（primary=3, secondary=3）：`1,000,000`，每三位一组。
- **印度分组**（primary=3, secondary=2）：`10,00,000`，最右边三位一组，之后每两位一组。

此外还有「最小分组位数」（min_grouping）：德语、西班牙语等要求**至少有两位数**落在第一个分组里才显示分隔符，所以德语里 `1000` 写作 `1000`（无逗号），而 `10000` 才写作 `10.000`。英语则 `1,000` 就有逗号（min_grouping=1）。

「符号」则是 locale 化的正负号。多数 locale 负号就是一个 `-` 前缀，但 CLDR 允许把符号表达成「前缀 + 后缀」对，于是 `DecimalFormatter` 用 `minus_sign_prefix` / `minus_sign_suffix` 两个字段来承载。

#### 4.2.2 核心流程

分组判断是一个纯函数 `grouper::check(upper_magnitude, magnitude, strategy, sizes) -> bool`，意为「在量级为 `magnitude` 的这一位**之后**，要不要插一个分组分隔符」。算法（关键阈值）：

- `primary`：最右边一组的大小（西方=3）。`primary==0` 表示永不分组。
- 只有当 `magnitude >= primary` 才可能插分隔符（保证最右 `primary` 位不被切）。
- `min_grouping`：要求整数总位数足够多，否则一个都不插。判定为 `upper_magnitude < primary + min_grouping - 1` 时整体不分组。
- `secondary`：第一个分组之后，每隔多少位插一个。`secondary==0` 时继承 `primary`。
- 对首个分组之后的位，计算 `magnitude' = magnitude - primary`，当 `magnitude' % secondary == 0` 时插分隔符。

符号处理更直接：`format_sign` 根据 `Sign` 枚举选出 `(minus|plus)_sign_prefix/suffix`，在写入时先写前缀、再写数值、再写后缀。

#### 4.2.3 源码精读

先看符号。`Sign` 只有三态：

[utils/fixed_decimal/src/variations.rs:13-21](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/utils/fixed_decimal/src/variations.rs#L13-L21) —— `None`（隐含正）/ `Negative` / `Positive`（显式 `+`）。

`format_sign` 把符号映射成「带片段标注的前后缀对」：

[components/decimal/src/decimal_formatter.rs:135-152](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/decimal/src/decimal_formatter.rs#L135-L152) —— `Sign::None` 得到 `None`（不渲染符号）；`Negative` 取 `minus_sign_prefix/suffix` 并标注 `parts::MINUS_SIGN`；`Positive` 取 `plus_sign_*` 标注 `parts::PLUS_SIGN`。前缀和后缀都来自 `self.symbols.get().strings`，即 locale 数据。

再看写入时符号如何包裹数值：

[components/decimal/src/decimal_formatter.rs:187-198](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/decimal/src/decimal_formatter.rs#L187-L198) —— 先 `with_part` 写前缀，再写数值（`self.value`），再写后缀。`with_part` 给这段文本打上片段标签，便于上层做富文本高亮（如把负号染红）。

接着是分组的核心——`GroupingSizes` 数据结构：

[components/decimal/src/provider.rs:196-210](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/decimal/src/provider.rs#L196-L210) —— 三个 `u8`：`primary`（首组大小）、`secondary`（后续组大小，0 则同 primary）、`min_grouping`（触发分组所需的最小位数）。

最关键的 `grouper::check`：

[components/decimal/src/grouper.rs:15-52](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/decimal/src/grouper.rs#L15-L52) —— 逐行对应 4.2.2 的算法：第 21-25 行处理 `primary==0`；第 26-28 行跳过最右 `primary` 位；第 29-38 行根据 `GroupingStrategy` 算 `min_grouping`（`Never` 直接返回 false，`Auto`/`Always` 取 `max(1, sizes.min_grouping)`，`Min2` 取 `max(2, ...)`）；第 39-41 行用 `upper_magnitude` 做整体长度判定；第 42-51 行用取模决定后续分隔符位置。

用户能调的旋钮是 `GroupingStrategy` 枚举：

[components/decimal/src/options.rs:48-68](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/decimal/src/options.rs#L48-L68) —— `Auto`（默认，按 locale）、`Never`（不分组）、`Always`（对 `DecimalFormatter` 等同 `Auto`，注释第 60-61 行说明了这点）、`Min2`（要求至少两位落入首组，多数 locale 下 1000–9999 不分组、10000 起分组）。`DecimalFormatterOptions` 只装这一个字段（[options.rs:9-16](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/decimal/src/options.rs#L9-L16)）。

> 旁证：`grouper.rs` 的测试用例是最好的「行为说明书」。看 [components/decimal/src/grouper.rs:101-122](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/decimal/src/grouper.rs#L101-L122)：西方尺寸 + `Auto` 把 `1000..=1000000` 格式化成 `1,000 / 10,000 / 100,000 / 1,000,000`；而印度尺寸（primary=3, secondary=2）把同样四个数变成 `1,000 / 10,000 / 1,00,000 / 10,00,000`。`Min2` 策略则让 `1000` 退化成无逗号的 `1000`。

#### 4.2.4 代码实践

**实践目标**：亲手验证 `Min2` 策略与印度式分组的差异。

**操作步骤**：

1. 在 4.1.4 的项目里新增一段，显式传入 `GroupingStrategy::Min2`：

   ```rust
   use icu::decimal::options::{DecimalFormatterOptions, GroupingStrategy};

   let mut opts = DecimalFormatterOptions::default();
   opts.grouping_strategy = Some(GroupingStrategy::Min2);
   let fmt_min2 = DecimalFormatter::try_new(
       icu::locale::locale!("en").into(), opts)
       .expect("ok");
   println!("{}", fmt_min2.format(&1_000.into()));   // 期望 1000
   println!("{}", fmt_min2.format(&10_000.into()));  // 期望 10,000
   ```

**需要观察的现象**：`1000` 没有逗号，`10000` 有逗号——因为 `Min2` 要求首组至少两位，而 `1000` 只有 1 位（`1`）落在首组之外。

**预期结果**：`1000` 与 `10,000`。该断言直接来自 [components/decimal/src/options.rs:42-46](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/decimal/src/options.rs#L42-L46) 的官方 doctest。运行结果以本机实际为准。

#### 4.2.5 小练习与答案

**练习 1**：用印度分组尺寸（primary=3, secondary=2, min_grouping=1）格式化 `100000`（量级 5），写出结果并解释。

**答案**：`1,00,000`。最右 3 位 `000` 成首组（primary=3）；之后每 2 位一组（secondary=2），所以 `1,00` 。这与 `grouper.rs` 测试一致。

**练习 2**：为什么 `GroupingStrategy::Always` 对 `DecimalFormatter` 和 `Auto` 行为相同？

**答案**：源码注释（[options.rs:59-62](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/decimal/src/options.rs#L59-L62) 与 [grouper.rs:33-35](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/decimal/src/grouper.rs#L33-L35)）说明：`Always` 与 `Auto` 的差别本应留给「货币场景」（货币符号会占位，影响最小分组判定），而 `DecimalFormatter` 还不支持货币，故二者等价。未来实现货币时这一行为会改变。

---

### 4.3 numbering system 与本地数字字形

#### 4.3.1 概念说明

同一个数值 `1000007`，在英语里是 `1,000,007`，在孟加拉语里是 `১০,০০,০০৭`，在泰语里是 `๑,๐๐๐,๐๐๗`。差异不仅在分组位置（4.2），更在于**十个数字字符本身不同**——这就是 numbering system（数字系统）。

ICU4X 做了一个极其重要的拆分：**「符号（分隔符/正负号/分组尺寸）」和「数字字形（0–9 十个字符）」是两份独立的数据**，分别由两个数据 marker 承载：

- `DecimalSymbolsV1`：和 locale 绑定，装着分隔符、正负号、分组尺寸，以及一个「我属于哪个数字系统」的名字 `numsys`。
- `DecimalDigitsV1`：和 locale **无关**，存在根 locale `und` 下，用「marker 属性」标注它属于哪个数字系统（如 `latn`/`thai`/`arab`/`beng`），内容就是 `[char; 10]` 十个字符。

为什么要拆？因为「分隔符跟着语言走」「字形跟着数字系统走」是两个独立的维度。一个说英语的人可能想看泰语数字（`en-u-nu-thai`）：此时分隔符、分组尺寸仍用英语的（`,`、每三位），但十个字形要用泰语的。拆分后就能自由组合。

#### 4.3.2 核心流程

`try_new_unstable` 里的 numbering system 解析分三步（对应源码注释里那张行为表）：

```text
1. 拿到 locale（含可能的 -u-nu-xxx 扩展）。
2. 用「显式 nu + locale 回退」链加载 symbols，得到真正解析出的 numsys
   （可能因缺数据而回退，如 en-u-nu-thai 的 symbols 仍是 latn）。
3. 用解析出的 numsys 作为 marker 属性，加载对应的 digits（十个字形）。
```

由此产生几种典型情形（摘自源码注释 [decimal_formatter.rs:71-80](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/decimal/src/decimal_formatter.rs#L71-L80)）：

| 输入 locale | symbols（分隔符等） | digits（字形） | 解析出的 numsys |
|---|---|---|---|
| `en` | latn | latn | latn |
| `en-u-nu-thai` | latn | thai | thai |
| `th` | thai | thai | thai |
| `th-u-nu-latn` | latn | latn | latn |
| `en-u-nu-wxyz`（不存在） | latn | latn | latn |

要点：显式指定的数字系统会**强制覆盖**字形（`en-u-nu-thai` 的字形变 thai），但 symbols 仍按 locale 取；若指定的数字系统根本没有字形数据（`wxyz`），则回退到 locale 默认。

#### 4.3.3 源码精读

先看两个数据 marker 的定义：

[components/decimal/src/provider.rs:152-157](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/decimal/src/provider.rs#L152-L157) —— `DecimalSymbolsV1` 的数据体是 `DecimalSymbols<'static>`，和 locale 绑定。

[components/decimal/src/provider.rs:159-167](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/decimal/src/provider.rs#L159-L167) —— `DecimalDigitsV1` 的数据体就是 `[char; 10]`，注释明确说它「应存在 `und` locale 下，用 marker 属性标注数字系统代码」。`attributes_domain = "numbering_system"`（datagen 用）进一步点明属性取值域是各种数字系统。

字形如何被用上？回到 4.1.3 的写入循环：第 211 行 `self.digits[self.value.digit_at(m) as usize]`——`digits` 现在就是 `&[char; 10]`（见 [FormattedUnsignedDecimal 字段 decimal_formatter.rs:176](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/decimal/src/decimal_formatter.rs#L176)），把 0–9 映射成本地字形。整段格式化里「字形替换」就这么一行，非常干净。

再看构造函数如何分别取这两份数据：

[components/decimal/src/decimal_formatter.rs:82-104](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/decimal/src/decimal_formatter.rs#L82-L104) —— 第 85-93 行：先用「`nu_id`（显式 nu）+ locale 回退」链 load symbols；第 95-97 行：从加载到的 symbols 里读出它声明的 `numsys()`，组装成 `resolved_nu_id`；第 99-104 行：再用「显式 nu + 解析出的 nu」链 load digits。两次都走 `load_with_fallback` 这个「逐个尝试标识符、找到第一个有数据的」辅助函数（[provider.rs:510-540](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/decimal/src/provider.rs#L510-L540)）。

> 名词解释：`-u-nu-` 是 Unicode Locale Identifier 的扩展关键字（见 [u2-l2 子标签体系](u2-l2-subtags.md)），`nu` = numbering system。`th-u-nu-thai` 读作「泰语，使用 thai 数字系统」。用户也可以不通过 locale、而通过 `DecimalFormatterPreferences` 直接传 `numbering_system`（见 [preferences.rs:11-24](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/decimal/src/preferences.rs#L11-L24)）。

最权威的「行为说明书」是这条测试：

[components/decimal/src/decimal_formatter.rs:274-301](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/decimal/src/decimal_formatter.rs#L274-L301) —— `test_numbering_resolution_fallback` 把 `1234` 在多个 locale 下格式化并断言：`en` → `1,234`；`en-u-nu-arab` → `١,٢٣٤`（英语分隔符 + 阿拉伯字形）；`ar-EG` → `١٬٢٣٤`（注意分隔符是阿拉伯千分位 `٬` U+066C）；`ar-EG-u-nu-thai` → `๑٬๒๓๔`（阿拉伯分隔符 + 泰语字形，完美演示「符号跟 locale、字形跟 nu」的解耦）。

#### 4.3.4 代码实践

**实践目标**：用 `ar-EG` 和 `en-u-nu-arab` 格式化同一个数，观察字形与分隔符的组合。

**操作步骤**：

```rust
use icu::decimal::input::Decimal;
use icu::decimal::DecimalFormatter;
use icu::locale::locale;

fn show(loc: &str, n: i64) {
    let fmt = DecimalFormatter::try_new(
        locale!(loc).into(), Default::default()).expect("locale present");
    // （示例代码）用 parse 构造带符号/小数的 Decimal 也可：
    let d: Decimal = n.into();
    println!("{loc:>12} : {}", fmt.format(&d));
}

fn main() {
    show("en", 1234);
    show("en-u-nu-arab", 1234);
    show("ar-EG", 1234);
    show("ar-EG-u-nu-thai", 1234);
}
```

**需要观察的现象**：

- `en` → `1,234`（西式逗号、西式字形）。
- `en-u-nu-arab` → `١,٢٣٤`（**仍是英文逗号 `,`**，但字形变阿拉伯-印度数字）——这正是「符号跟 locale、字形跟 nu」。
- `ar-EG` → `١٬٢٣٤`（字形是阿拉伯-印度数字，分隔符变成阿拉伯千分位 `٬`）。
- `ar-EG-u-nu-thai` → `๑٬๒๓๔`（分隔符仍是阿拉伯的 `٬`，字形却变泰语）。

**预期结果**：与上一段一致，且前三条恰好等于 [test_numbering_resolution_fallback](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/decimal/src/decimal_formatter.rs#L289-L300) 的断言。第四条的「阿拉伯分隔符 + 泰语字形」组合体现了拆分的威力。本机输出以实际为准。

> 说明：本实践与规格里给出的 `de-DE` / `ar-EG` 任务等价，并额外用 `en-u-nu-arab` 把「符号 vs 字形」的差异隔离得更清楚。`de-DE` 下 `-1234567.89` 的预期是 `-1.234.567,89`（`.` 千分位、`,` 小数点、de 的 min_grouping=2 在百万级必然触发分组）——**待本地验证**确切输出。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `en-u-nu-thai` 输出里**分隔符仍是英文逗号**，而不是泰语的？

**答案**：因为分隔符属于 `DecimalSymbolsV1`，和 locale（`en`）绑定；只有字形属于 `DecimalDigitsV1`，跟随 `-u-nu-thai`。所以 `en-u-nu-thai` = 英文符号 + 泰语字形。

**练习 2**：如果用户请求 `en-u-nu-wxyz`（一个不存在的数字系统），会发生什么？

**答案**：`digits` 加载失败、回退到 locale 默认数字系统 `latn`，输出 `1,234`（见测试 [decimal_formatter.rs:298-299](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/decimal/src/decimal_formatter.rs#L298-L299)）。这是 `load_with_fallback` 「逐标识符尝试、找到第一个有数据的」机制的结果。

---

## 5. 综合实践

把本讲三个模块串起来：写一个小工具 `numfmt`，它读入一个整数和一个 locale，输出格式化结果，并额外打印「解析出的数字系统」。

**任务**：

1. 复用 4.3.4 的 `DecimalFormatter::try_new`。
2. 借鉴 [provider.rs 顶部示例](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/decimal/src/provider.rs#L17-L101)：用一个「包装 provider」拦截对 `DecimalDigitsV1` 的最后一次 `load` 请求，从 `req.id.marker_attributes` 读出解析出的数字系统名字并打印出来。
3. 分别对 `de-DE`、`ar-EG`、`th-u-nu-thai`、`bn`（孟加拉语）跑一遍 `1000007`，记录：输出字符串、解析出的 numsys、是否出现印度式分组（`bn` 的孟加拉语也用 lakh 分组）。

**验收要点**：

- `de-DE` 应给出 `1.000.007`（`.` 分组），numsys 为 `latn`。
- `ar-EG` 应给出阿拉伯-印度字形，numsys 为 `arab`。
- `th-u-nu-thai` 的 numsys 应解析为 `thai`，字形为泰文。
- `bn` 应给出 `১০,০০,০০৭`（孟加拉字形 + 印度式分组），这与 [lib.rs 顶部示例](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/decimal/src/lib.rs#L23-L30) 的断言一致。

这个任务同时调用了「入口 API（4.1）」「分组与符号（4.2）」「numbering system 解析（4.3）」三个模块，并练习了「用包装 provider 观察数据流」这一在 [u5 数据提供器系统](u5-l1-dataprovider-core.md) 会深入的主题。

## 6. 本讲小结

- `DecimalFormatter` 接收 `Decimal`（= `Signed<UnsignedDecimal>`，按量级索引的精确十进制数），产出惰性的 `FormattedDecimal`，真正生成字符发生在实现 `Writeable` 的写入循环里。
- 它只持有三个字段：`options`、`symbols`（locale 的符号/分组尺寸）、`digits`（数字系统的 10 个字形），是典型的数据驱动设计。
- 分组由纯函数 `grouper::check` + `GroupingSizes`（primary/secondary/min_grouping）+ `GroupingStrategy`（Auto/Never/Always/Min2）共同决定，能表达西方 `1,000,000` 与印度 `10,00,000` 两套规则，以及德语的「最小两位」要求。
- 正负号被建模成「前缀 + 数值 + 后缀」并带片段标注（`parts::MINUS_SIGN` 等），支持富文本。
- numbering system 是独立维度：`DecimalSymbolsV1`（跟 locale）与 `DecimalDigitsV1`（跟 `-u-nu-`，存于 `und`）拆分，使「英语符号 + 泰语字形」这类组合成为可能；缺失数据时自动回退到 locale 默认。

## 7. 下一步学习建议

- **横向**：继续 [u3-l4 PluralRules](u3-l4-plural-rules.md)，看 `Decimal`/`FixedDecimal` 如何作为复数判定的输入（`PluralOperands` 正是从 `UnsignedDecimal` 提取操作数），两讲共享同一个数值表示。
- **纵向（数据层）**：本讲反复出现的 `DataPayload`、`DataMarker`、`load_with_fallback`、包装 provider 等，都属于 [u5 数据提供器系统](u5-l1-dataprovider-core.md)。建议接着学 u5-l1 / u5-l2，把 `DecimalSymbolsV1` / `DecimalDigitsV1` 当作贯穿案例。
- **底层工具**：`Decimal` 来自 `fixed_decimal` crate，想理解它为何「按量级存数位、零拷贝友好」，可读 [u6-l6 tinystr/litemap/fixed_decimal](u6-l6-tinystr-litemap-fixed-decimal.md)；想理解惰性 `FormattedDecimal` 为何高效，可读 [u6-l5 writeable](u6-l5-writeable.md)。
- **进阶**：货币、单位、紧凑记法（`1.2M`）尚未在稳定 API 中，跟踪 [icu4x#275](https://github.com/unicode-org/icu4x/issues/275) 与 `CompactDecimalFormatter`（`unstable` feature，见 [u8-l5 experimental](u8-l5-experimental-components.md)）。
