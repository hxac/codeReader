# DateTime 格式化旗舰组件

## 1. 本讲目标

`icu_datetime` 是 ICU4X 中最复杂、最具代表性的组件，也是绝大多数用户最先接触到的组件。本讲聚焦于它的「现代（neo）」API：`DateTimeFormatter`。

学完后你应该能够：

- 说清楚什么是 **field set（字段集）**，以及它为什么是类型而非字符串。
- 会用 `YMD::long()` / `YMD::medium()` 这类静态字段集 + `length` 选项构造一个格式化器，并解释二者如何共同决定输出形态。
- 复述从 `format()` 到最终本地化字符串的「惰性格式化流水线」，并理解 `FormattedDateTime` 为什么实现了 `Writeable` 而不是立刻生成字符串。
- 理解「静态字段集」相比「动态字段集」为何能减小二进制体积。

> 本讲承接 [u2-l1](u2-l1-locale-model.md)（`Locale` 数据模型）与 [u1-l4](u1-l4-metacrate-and-features.md)（`compiled_data` 体系）。`locale!` 宏与「compiled data 默认编译进二进制」这两个前置认知在本讲会反复用到。

## 2. 前置知识

### 2.1 什么是「日期时间格式化」

同样一个时刻 `2024-05-17`，不同地区的人习惯写成完全不同的样子：

| Locale | 写法 |
|---|---|
| `en-US` | `May 17, 2024` |
| `es-MX` | `17 de mayo de 2024` |
| `ja` | `2024年5月17日` |
| `de-DE` | `17. Mai 2024` |

格式化的任务就是：给定一个「机器友好的日期输入」和一个「locale」，产出符合该地区习惯、且 **正确** 的字符串。所谓正确，包括年月日顺序、分隔符、月份名拼写、数字系统、时区名、甚至哪些字段该显示。

### 2.2 Semantic Skeletons（语义骨架）

ICU4X 遵循 Unicode UTS #35 的 [Semantic Skeletons](https://unicode.org/reports/tr35/tr35-dates.html#Semantic_Skeletons) 规范。它的核心思想是 **两步走**：

1. 先选 **field set（字段集）**：决定「要显示哪些字段」（年、月、日、时、分、星期……）。
2. 再配 **options（选项）**：决定「以什么长度/风格显示」（short/medium/long、是否带纪元……）。

这一点在 crate 根文档里写得很清楚：

> First you choose a _field set_, then you configure the formatting _options_ to your desired context.

理解了「字段集是第一公民」，本讲后面的所有 API 都围绕它展开。

### 2.3 Writeable（复习）

`DateTimeFormatter::format()` 返回的不是 `String`，而是一个实现了 `Writeable` 的惰性对象。`Writeable` 是 ICU4X 自定义的输出 trait（见 [u6-l5](u6-l5-writeable.md)），核心特点是「直到真正写入时才生成文本」，便于按需写到任意缓冲区、并附带「片段标注（parts）」给富文本使用。本讲只需记住：**`FormattedDateTime` 是惰性的，打印它时才干活**。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `components/datetime/src/lib.rs` | crate 根文档，讲清 Semantic Skeletons 两步走、静态/动态字段集对体积的影响、并 `pub use` 出三个格式化器与输入类型模块。 |
| `components/datetime/src/fieldsets.rs` | 所有字段集的定义。用宏批量生成 `YMD`/`YMDT` 等静态字段集类型及其 `long()/medium()/short()` 与 `with_*` 构造器。 |
| `components/datetime/src/builder.rs` | 动态字段集的 `FieldSetBuilder`，用于「运行期才知道要格式化什么」的场景。 |
| `components/datetime/src/options/mod.rs` | `Length`、`YearStyle`、`Alignment`、`TimePrecision` 等选项枚举。 |
| `components/datetime/src/neo.rs` | 现代 API 的入口：`DateTimeFormatter`、`FixedCalendarDateTimeFormatter`、`FormattedDateTime`、`format()` 方法。 |
| `components/datetime/src/parts.rs` | 格式化结果的「片段标注」常量（`YEAR`/`MONTH`/`DAY` 等），用于富文本。 |
| `components/datetime/src/format/datetime.rs` | 流水线末端：把模式项（字面量 + 字段）逐项写到输出缓冲。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **DateTimeFormatter 与 fieldset 体系**——类型长什么样、有哪些格式化器、构造与格式化的整体数据流。
2. **静态 fieldset 与 length 选项**——`YMD` 这类类型如何由宏生成、`length`/`YearStyle`/`Alignment`/`TimePrecision` 选项如何配置。
3. **格式化流水线与 FormattedDateTime 输出**——从 `format()` 到字符串的惰性管线与片段标注。

---

### 4.1 DateTimeFormatter 与 fieldset 体系

#### 4.1.1 概念说明

`DateTimeFormatter` 是一个 **泛型类型**，它的类型参数是一个 **字段集类型**：

```rust
pub struct DateTimeFormatter<FSet: DateTimeNamesMarker> { ... }
```

也就是说 `DateTimeFormatter<YMD>` 和 `DateTimeFormatter<YMDT>` 是 **两个不同的类型**。这是一种「把要显示的字段编码进类型」的设计。它的好处在 4.2 节会看到：编译器能据此裁掉用不到的数据。

除了主格式化器，crate 还导出两个「瘦身版」（[components/datetime/src/lib.rs:147-151](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/datetime/src/lib.rs#L147-L151)）：

| 格式化器 | 日历处理 | 适用场景 |
|---|---|---|
| `DateTimeFormatter<FSet>` | 运行期 `AnyCalendar`（按 locale 自动选） | 通用场景 |
| `FixedCalendarDateTimeFormatter<C, FSet>` | 编译期固定日历 `C` | 只支持单一日历、省体积 |
| `NoCalendarFormatter<FSet>` | 完全不带日历 | 纯时间（`Time`）场景 |

日历系统（公历、佛历、希吉拉、日本历等十余种）通常从 locale 推导，也可显式指定。这是格式化正确性的关键：同一个 ISO 日期在不同日历下是完全不同的年月日。

构造函数遵循 [u1-l4](u1-l4-metacrate-and-features.md) 讲过的 compiled_data 约定：

- `try_new(prefs, fieldset)` —— 用编译期内嵌的 compiled data，需 `compiled_data` feature（默认开）。
- `try_new_with_buffer_provider(...)` —— 运行期喂数据，需 `serde`。
- `try_new_unstable(provider, ...)` —— 显式 `DataProvider`。

第一个参数 `prefs` 是 `DateTimeFormatterPreferences`（由 `Locale` 经 `.into()` 得到），它只挑本组件关心的几个 Unicode 扩展关键字：`-u-nu`（数字系统）、`-u-hc`（小时制）、`-u-ca`（日历），见 [components/datetime/src/neo.rs:31-79](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/datetime/src/neo.rs#L31-L79)。

#### 4.1.2 核心流程

整个「构造 → 格式化」的数据流可以概括为：

```text
① 选字段集 + 配选项            ② 构造格式化器
   YMD::long()                   DateTimeFormatter::try_new(prefs, YMD::long())
   .with_year_style(...)              │
        │                             ├──(compiled_data) 加载内嵌 CLDR 数据
        ▼                             ├── 解析日历（prefs 的 -u-ca- 或 locale 默认）
   fieldset 值                        └── 选择本地化 pattern（模式）
                                             │
                                             ▼
③ 格式化输入                          ④ 惰性结果
   formatter.format(&date)  ───────►   FormattedDateTime { pattern, input, names }
                                             │  (写入/打印时才求值)
                                             ▼
                                       try_write_pattern_items → 本地化字符串
```

要点：

- **构造期（②）** 负责把数据、日历、pattern 都准备好；这一步返回 `Result`，因为加载 compiled data 可能失败（locale 数据缺失）。
- **格式化期（③④）** 是惰性的：`format()` 只做日历转换与字段提取，真正的文本生成推迟到写入时。

#### 4.1.3 源码精读

**(a) crate 根的两步走范式与示例**

根文档开宗明义说明 Semantic Skeletons 范式，并给出一个完整示例：[components/datetime/src/lib.rs:18-76](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/datetime/src/lib.rs#L18-L76)。示例核心是这几行（`es-AR` 阿根廷西班牙语，年月日时分，medium 长度）：

```rust
let field_set_with_options = fieldsets::YMD::medium().with_time_hm();
let locale = locale!("es-AR");
let dtf = DateTimeFormatter::try_new(locale.into(), field_set_with_options).unwrap();
// ... 输出 "15 de ene de 2025, 4:09 p. m."
```

注意 `locale.into()`：`Locale` 被转换成了 `DateTimeFormatterPreferences`。

**(b) 三个格式化器的 re-export**

[components/datetime/src/lib.rs:147-151](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/datetime/src/lib.rs#L147-L151) 把 `neo` 模块里的类型重新导出为 crate 顶层名字：

```rust
pub use neo::DateTimeFormatter;
pub use neo::DateTimeFormatterPreferences;
pub use neo::FixedCalendarDateTimeFormatter;
pub use neo::FormattedDateTime;
pub use neo::NoCalendarFormatter;
```

**(c) 主结构体与构造器**

`DateTimeFormatter` 由三部分组成：选好的 pattern 数据、名字表（年/月/星期等本地化名称）、运行期日历：[components/datetime/src/neo.rs:428-433](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/datetime/src/neo.rs#L428-L433)。

```rust
pub struct DateTimeFormatter<FSet: DateTimeNamesMarker> {
    pub(crate) selection: DateTimeZonePatternSelectionData,
    pub(crate) names: RawDateTimeNames<FSet>,
    pub(crate) calendar: FormattableAnyCalendar,
}
```

compiled_data 构造器 `try_new` 把字段集 `get_field()` 成动态复合字段集后交给内部函数：[components/datetime/src/neo.rs:451-466](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/datetime/src/neo.rs#L451-L466)。

```rust
#[cfg(feature = "compiled_data")]
pub fn try_new(
    prefs: DateTimeFormatterPreferences,
    field_set_with_options: FSet,
) -> Result<Self, DateTimeFormatterLoadError>
where
    crate::provider::Baked: AllAnyCalendarFormattingDataMarkers<FSet>,
{
    Self::try_new_internal(
        &crate::provider::Baked,
        &ExternalLoaderCompiledData,
        prefs,
        field_set_with_options.get_field(),
    )
}
```

注意 `where crate::provider::Baked: AllAnyCalendarFormattingDataMarkers<FSet>`：这个 trait bound 把「字段集类型 `FSet`」与「需要哪些 compiled data」在编译期绑定。这是后续裁体积的关键。

#### 4.1.4 代码实践

**实践目标**：亲手跑通「构造 → 格式化」一条龙，观察构造返回 `Result`、格式化返回惰性对象。

**操作步骤**（示例代码，基于 `cargo new --bin dtf-demo` + `cargo add icu`）：

```rust
// 示例代码
use icu::datetime::fieldsets::YMD;
use icu::datetime::input::Date;
use icu::datetime::DateTimeFormatter;
use icu::locale::locale;

fn main() {
    // ① 选字段集：年月日，medium 长度
    let fset = YMD::medium();

    // ② 构造（运行期加载 compiled data，故返回 Result）
    let dtf = DateTimeFormatter::try_new(locale!("en").into(), fset)
        .expect("en 的 YMD 数据应该存在于 compiled data 中");

    // ③ 格式化（惰性）
    let date = Date::try_new_iso(2024, 5, 17).unwrap();
    let formatted = dtf.format(&date);

    // ④ 打印（此刻才真正生成文本）
    println!("{}", formatted);
}
```

**需要观察的现象**：

1. 构造语句必须用 `.expect(...)` 或 `match`，因为 `try_new` 返回 `Result`——这印证了「compiled data 加载是运行期行为」。
2. `formatted` 不能用 `let s: &str = formatted;`，但可以用 `println!("{}", formatted)` 或 `formatted.write_to_string()`——因为它实现的是 `Writeable`/`Display`，不是 `Display` 直接返回 `String`。

**预期结果**：在 `en` 下应输出 `May 17, 2024`（与根文档示例同一字段集/长度一致的风格）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `DateTimeFormatter::try_new` 的第二个参数是「字段集值」，而类型本身却带一个「字段集类型参数 `FSet`」？二者是什么关系？

> **答案**：`FSet` 是编译期类型（如 `YMD`），决定「这个格式化器能格式化哪些字段、需要链接哪些数据」；构造时传入的 `field_set_with_options`（如 `YMD::medium()`）是运行期值，携带具体选项（长度等）。类型参数在编译期固定了能力边界，值在运行期给出具体配置。`FSet` 实例通过 `get_field()` 转成内部用的 `CompositeFieldSet` 供流水线使用。

**练习 2**：如果你只想格式化「一天内的时刻」（`Time`），用三个格式化器中的哪一个最省体积？为什么？

> **答案**：`NoCalendarFormatter`。它完全不带日历，因此既不链接日历数据，也不链接「把输入转换到目标日历」的代码。这与根文档「For field sets that don't contain dates, this can also be achieved using `NoCalendarFormatter`」的说明一致。

---

### 4.2 静态 fieldset 与 length 选项

#### 4.2.1 概念说明

**字段集（field set）决定「显示哪些字段」**。`fieldsets.rs` 模块文档把字段集归为四大类：[components/datetime/src/fieldsets.rs:13-29](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/datetime/src/fieldsets.rs#L13-L29)

1. **Date（日期）**：定位某一天，如 `YMD`（年月日）、`YMDE`（年月日 + 星期）。
2. **Calendar period（日历周期）**：跨度大于一天，如 `Y`（年）、`M`（月）、`YM`（年月）。
3. **Time（时间）**：一天内的时刻，如 `T`。
4. **Zone（时区）**：时区或 UTC 偏移，如 `zone::SpecificLong`。

字段集分两种 API 风格，这是理解体积优化的关键：

| 类型 | 例子 | 字段集是…… | 体积影响 |
|---|---|---|---|
| **静态字段集** | `fieldsets::YMD` | 一个 **类型** | 编译器能裁掉用不到的数据，**体积最小** |
| **动态字段集** | `fieldsets::enums::CompositeFieldSet` | 一个 **值** | 会链接它能表示的所有 pattern 的数据，即使代码里用不到 |

**选项（options）决定「以什么风格显示」**。核心枚举在 `options/mod.rs`：

- `Length`：`Short` / `Medium`（默认）/ `Long`。注意当前只有这三个变体，且是 `#[non_exhaustive]`（未来可能新增，例如 `Full`）。
- `YearStyle`：`Auto`（默认）/ `Full` / `WithEra` / `NoEra`——控制是否显示世纪与纪元（AD/BC）。
- `Alignment`：`Auto`（默认）/ `Column`——给「列布局」做零填充提示。
- `TimePrecision`：`Hour` / `Minute` / `Second`（默认）/ `Subsecond(n)` / `MinuteOptional`——时间精度。

#### 4.2.2 核心流程

静态字段集的「构造器链」由两个宏配合生成：

```text
impl_marker_length_constructors!      生成  long() / medium() / short() / for_length(L)
        │                              返回一个 Self（字段集值）
        ▼
impl_marker_with_options!             生成  字段集 struct 字段 + with_X 链式构造器
        │                              如 with_year_style / with_alignment / with_time_precision
        ▼
字段集值  ──传入──►  DateTimeFormatter::try_new(prefs, 字段集值)
```

`length` 是 **提示而非保证**（`options/mod.rs` 第 11-13 行说明）：某些 locale/日历没有数字名称，即便要求 `Short` 也可能给出拼写形式，反之亦然。

#### 4.2.3 源码精读

**(a) `long/medium/short` 构造器由宏统一生成**

几乎所有字段集都需要相同的长度构造器，所以用一个宏批量生成：[components/datetime/src/fieldsets.rs:126-163](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/datetime/src/fieldsets.rs#L126-L163)

```rust
macro_rules! impl_marker_length_constructors {
    ($type:ident, ...) => {
        impl $type {
            pub const fn for_length(length: Length) -> Self { Self { length, ... } }
            pub const fn long() -> Self   { Self::for_length(Length::Long) }
            pub const fn medium() -> Self { Self::for_length(Length::Medium) }
            pub const fn short() -> Self  { Self::for_length(Length::Short) }
        }
    };
}
```

注意它们都是 `const fn`——字段集值可以在编译期求值。

**(b) 字段集 struct 与 `with_*` 链式构造器也由宏生成**

[components/datetime/src/fieldsets.rs:186-285](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/datetime/src/fieldsets.rs#L186-L285) 中的 `impl_marker_with_options!` 宏会生成 `#[non_exhaustive]` 的 struct（字段为 `length` / `alignment` / `year_style` / `time_precision`，按字段集能力选择性出现）以及 `with_length` / `with_alignment` / `with_year_style` / `with_time_precision` 等链式方法。

**(c) `YMD` 与 `YMDT` 的实例化**

`YMD`（年月日）和带时间的 `YMDT` 由 `impl_date_marker!` 宏一次性生成：[components/datetime/src/fieldsets.rs:1101-1115](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/datetime/src/fieldsets.rs#L1101-L1115)

```rust
impl_date_marker!(
    YMD,
    YMDT,
    description = "year, month, and day",
    sample_length = short,
    sample = "5/17/24",
    sample_time = "5/17/24, 3:47:50 PM",
    years = yes,
    months = yes,
    input_year = yes,
    input_month = yes,
    input_day_of_month = yes,
    input_any_calendar_kind = yes,
    option_alignment = yes,
);
```

这里的 `years = yes` / `months = yes` 等开关，决定该字段集生成哪些 marker trait 实现（即「需要哪些输入字段、哪些本地化名称、哪些数据」）。`sample` 给出该字段集在 `short` 长度下 `en` 的参考输出 `5/17/24`。

**(d) `Length` 三变体与默认值**

[components/datetime/src/options/mod.rs:58-78](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/datetime/src/options/mod.rs#L58-L78) 定义了 `Length`，文档示例清楚展示 `en-US` 下三种长度的差异：

```rust
pub enum Length {
    Long,                  // "January 1, 2000"
    #[default]
    Medium,                // "Jan 1, 2000"   ← 默认
    Short,                 // "1/1/00"
}
```

**(e) 静态 vs 动态字段集的体积差异（本讲核心）**

crate 根文档专门用一节解释：[components/datetime/src/lib.rs:78-109](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/datetime/src/lib.rs#L78-L109)。要点原文复述：

> Static field sets on the other hand leverage the type system to let the compiler drop unneeded data.

并给出对比：用 `DateTimeFormatter<YMD>` 构造只链接 `YMD` 所需数据；而 `DateTimeFormatter<CompositeFieldSet>` 即使代码里只用 `YMD`，也会链接 **所有可能字段集** 的数据。文档建议：若确实需要 `CompositeFieldSet` 类型，**先**用静态构造、**再**用 `cast_into_fset()` 转型，而不是直接用动态构造。

**(f) 动态字段集的 `FieldSetBuilder`**

当字段集要到运行期才确定（如从网络/配置文件读入），用 `builder.rs` 的 `FieldSetBuilder`：[components/datetime/src/builder.rs:437-464](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/datetime/src/builder.rs#L437-L464)。它把 `length` / `date_fields` / `time_precision` / `zone_style` / `alignment` / `year_style` 全部放成 `Option`，再按 `build_date()` / `build_composite()` 等方法组装成动态字段集枚举。其文档示例（[components/datetime/src/builder.rs:22-95](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/datetime/src/builder.rs#L22-L95)）展示了「同一字段集，静态写法 vs builder 写法」的等价对照。

#### 4.2.4 代码实践（本讲必做任务）

**实践目标**：用 `YMD::long()` 与 `YMD::medium()` 在 `ja`（日语）下格式化同一个 ISO 日期，对比输出差异；并思考字段集如何在编译期帮助减小二进制体积。

**操作步骤**：

```rust
// 示例代码
use icu::datetime::fieldsets::YMD;
use icu::datetime::input::Date;
use icu::datetime::DateTimeFormatter;
use icu::locale::locale;

fn main() {
    let date = Date::try_new_iso(2024, 5, 17).unwrap();

    // (1) long 长度
    let dtf_long = DateTimeFormatter::try_new(locale!("ja").into(), YMD::long())
        .unwrap();
    println!("ja YMD::long()   = {}", dtf_long.format(&date));

    // (2) medium 长度
    let dtf_medium = DateTimeFormatter::try_new(locale!("ja").into(), YMD::medium())
        .unwrap();
    println!("ja YMD::medium() = {}", dtf_medium.format(&date));
}
```

**需要观察的现象**：

1. `long` 通常给出更「完整/拼写化」的形式，`medium` 通常更「紧凑/数字化」。
2. 改变 `length` **不需要** 改变字段集类型——两次都是 `DateTimeFormatter<YMD>`，只是字段集 **值** 的 `length` 字段不同。

**预期结果**：日语 `ja` 下，`YMD::long()` 与 `YMD::medium()` 的确切字符串 **待本地验证**（请实际运行确认）。作为对比，`en-US` 下的等价行为在 `options/mod.rs` 文档中有真实断言：`Short → "1/1/00"`、`Medium → "Jan 1, 2000"`、`Long → "January 1, 2000"`（[components/datetime/src/options/mod.rs:43-56](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/datetime/src/options/mod.rs#L43-L56)），可据此理解「长度越长越拼写化」的趋势。

**思考题（编译期减体积）**：上例中只用到 `YMD` 一种字段集。如果把 `DateTimeFormatter<YMD>` 换成 `DateTimeFormatter<CompositeFieldSet>`（动态），即便代码里仍只格式化年月日，二进制体积通常更大。结合 4.2.3 (e) 解释原因：动态字段集是「值」，编译器无法证明其它变体不会被触达，于是必须把它们对应的 pattern 数据全部链接进来；静态字段集是「类型」，类型参数 `FSet` 在每个 monomorphization 中是固定的，未用到的数据 trait bound 不满足、可被链接器丢弃。

#### 4.2.5 小练习与答案

**练习 1**：`YMD::medium()` 与 `YMD::long()` 是同一个类型吗？它们的差异体现在哪里？

> **答案**：是同一个类型 `YMD`，差异体现在运行期 **值** 的 `length` 字段（`Length::Medium` vs `Length::Long`）。类型相同意味着它们链接的数据集合相同，只是 pattern 选择不同。

**练习 2**：若想让年份始终带上纪元（如 `2024 AD`），该用哪个选项？它对所有字段集都可用吗？

> **答案**：用 `.with_year_style(YearStyle::WithEra)`。它只在「包含年份的字段集」（如 `YMD`/`Y`/`YM`）上可用；对不含年份的字段集（如 `T`、`M`）使用会被 builder 判为 `SuperfluousOptions` 错误（见 `builder.rs` 的 `check_options_consumed`，[components/datetime/src/builder.rs:1074-1080](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/datetime/src/builder.rs#L1074-L1080)）。

---

### 4.3 格式化流水线与 FormattedDateTime 输出

#### 4.3.1 概念说明

`format()` 返回的 `FormattedDateTime` 是一个 **中间对象，不应长期保存**（其文档明确写 "Not intended to be stored: convert to a string first"）。它只持有三样东西的引用：选好的 pattern、提取出的输入字段、名字表。

惰性设计带来两个好处：

1. **零成本不输出**：如果你只是 `format()` 后由于分支没打印，文本生成就没发生。
2. **富文本片段（parts）**：写入时可以给每段文本打上「这段是年份」「这段是月份」的标注，供上层做高亮/样式。

#### 4.3.2 核心流程

`format()` 内部三步，写入时第四步：

```text
format(&input)
  │
  ├─① to_calendar(formatter.calendar)        // 把输入转到格式化器的日历
  │                                            （DateTimeFormatter 是 AnyCalendar，故 ISO 输入会被转）
  ├─② DateTimeInputUnchecked::extract_from_neo_input  // 按字段集 marker 提取字段
  └─③ 组装 FormattedDateTime { pattern: selection.select(&input), input, names }
        │
        │  ── 写入时（write_to_parts / Display）──
        ▼
  ④ try_write_pattern_items：
        for item in pattern_items {
            Literal(ch) => 直接写字符
            Field(field) => try_write_field(...)   // 用 input + names 格式化该字段
        }
```

注意：`pattern_items` 是一个「字面量字符」与「字段」交错的序列——这正是 UTS#35 模式串的内部表示（如 `y年M月d日` 被拆成「字段 y、字面量 `年`、字段 M、字面量 `月`……」）。

#### 4.3.3 源码精读

**(a) `format()` 方法**

`DateTimeFormatter::format` 位于 [components/datetime/src/neo.rs:727-744](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/datetime/src/neo.rs#L727-L744)：

```rust
pub fn format<'a, I>(&'a self, datetime: &I) -> FormattedDateTime<'a>
where
    I: ?Sized + ConvertCalendar,
    I::Converted<'a>: Sized + AllInputMarkers<FSet>,
{
    let datetime = datetime.to_calendar(self.calendar.any_calendar());          // ① 日历转换
    let datetime = DateTimeInputUnchecked::extract_from_neo_input::<             // ② 字段提取
        FSet::D, FSet::T, FSet::Z, I::Converted<'a>,
    >(&datetime);
    FormattedDateTime {                                                          // ③ 组装
        pattern: self.selection.select(&datetime),
        input: datetime,
        names: self.names.as_borrowed(),
    }
}
```

trait bound `I::Converted<'a>: AllInputMarkers<FSet>` 正是把「字段集类型」与「输入类型」在编译期对齐的关口——所以 `format(&Time)` 喂给 `DateTimeFormatter<YMD>` 会编译失败（`Time` 不提供年月日字段），根文档把这种 `compile_fail` 当作示例（[components/datetime/src/neo.rs:712-725](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/datetime/src/neo.rs#L712-L725)）。这种「类型不匹配在编译期就报错」是静态字段集的安全收益。

**(b) `FormattedDateTime` 与 `Writeable`**

[components/datetime/src/neo.rs:1138-1175](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/datetime/src/neo.rs#L1138-L1175):

```rust
pub struct FormattedDateTime<'a> {
    pattern: DateTimeZonePatternDataBorrowed<'a>,
    input: DateTimeInputUnchecked,
    names: RawDateTimeNamesBorrowed<'a>,
}

impl Writeable for FormattedDateTime<'_> {
    fn write_to_parts<S: writeable::PartsWrite + ?Sized>(&self, sink: &mut S) -> Result<(), fmt::Error> {
        let result = try_write_pattern_items(/* ... */ sink);   // ④ 真正生成文本
        // ...
    }
}

impl_display_with_writeable!(FormattedDateTime<'_>);   // 因此也实现了 Display
```

`impl_display_with_writeable!` 让 `FormattedDateTime` 同时获得 `Display`，所以 `println!("{}", formatted)` 和 `formatted.write_to_string()` 都能用——但底层都是走 `Writeable::write_to_parts`。

**(c) 流水线末端：逐项写模式**

[components/datetime/src/format/datetime.rs:72-100](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/datetime/src/format/datetime.rs#L72-L100) 是文本生成的真正落点：

```rust
pub(crate) fn try_write_pattern_items<W>(
    pattern_metadata: PatternMetadata,
    pattern_items: impl Iterator<Item = PatternItem>,
    input: &DateTimeInputUnchecked,
    datetime_names: &RawDateTimeNamesBorrowed,
    decimal_formatter: Option<&DecimalFormatter>,
    w: &mut W,
) -> Result<Result<(), FormattedDateTimePatternError>, fmt::Error> {
    for item in pattern_items {
        match item {
            PatternItem::Literal(ch) => w.write_char(ch)?,
            PatternItem::Field(field) => {
                r = r.and(try_write_field(field, pattern_metadata, input, datetime_names, decimal_formatter, w)?);
            }
        }
    }
    Ok(r)
}
```

`PatternItem` 只有两种：`Literal`（原样输出的字符）和 `Field`（一个待格式化的字段，如「年」）。每个 `Field` 再交给 `try_write_field`，后者用 `input`（数值）+ `datetime_names`（本地化名称，如「五月」「May」）+ `decimal_formatter`（数字格式化，见 [u3-l3](u3-l3-decimal-formatting.md)）拼出文本。

**(d) 片段标注（parts）**

[components/datetime/src/parts.rs:60-93](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/datetime/src/parts.rs#L60-L93) 定义了一批 `Part` 常量，给输出文本的每一段打上语义标签：

```rust
pub const YEAR:  Part = Part { category: "datetime", value: "year" };
pub const MONTH: Part = Part { category: "datetime", value: "month" };
pub const DAY:   Part = Part { category: "datetime", value: "day" };
// 还有 ERA / WEEKDAY / HOUR / MINUTE / SECOND / DAY_PERIOD / TIME_ZONE_NAME ...
```

parts 的用途见模块示例：用 `assert_writeable_parts_eq!` 可以断言「`Nov 20, 2566 BE, ...` 中第 0..3 字符是 `MONTH`、第 8..12 是 `YEAR`……」（[components/datetime/src/parts.rs:8-55](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/datetime/src/parts.rs#L8-L55)）。上层 UI 据此把年份数字加粗、把月份染成不同颜色。

#### 4.3.4 代码实践

**实践目标**：观察「惰性」与「片段标注」，并取出 formatter 实际选用的 pattern。

**操作步骤**：

```rust
// 示例代码
use icu::datetime::fieldsets::YMD;
use icu::datetime::input::Date;
use icu::datetime::DateTimeFormatter;
use icu::locale::locale;
use writeable::Writeable;

fn main() {
    let dtf = DateTimeFormatter::try_new(locale!("en").into(), YMD::medium()).unwrap();
    let date = Date::try_new_iso(2024, 5, 17).unwrap();

    // (1) 两种取字符串的方式，都走 Writeable
    let s1 = format!("{}", dtf.format(&date));
    let s2 = dtf.format(&date).write_to_string().unwrap();
    assert_eq!(s1, s2);
    println!("{s1}");

    // (2) 看看 formatter 实际选用了什么 pattern（可用于调试小时制/字段顺序）
    let p = dtf.format(&date).pattern();
    println!("pattern items: {:?}", p);
}
```

**需要观察的现象**：

1. `dtf.format(&date)` 可以反复调用、各自取字符串，互不影响（每次返回新的 `FormattedDateTime`）。
2. `.pattern()` 能告诉你 formatter 在 `en + YMD::medium` 下到底用哪条模式，便于排查「为什么小时制/字段顺序是这样」。

**预期结果**：`s1` 在 `en` 下应为 `May 17, 2024`（与根文档 `YMD::medium` 风格一致）；`pattern` 的具体内部表示 **待本地验证**。

**进阶（源码阅读型）**：阅读 [components/datetime/src/format/datetime.rs:72-100](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/datetime/src/format/datetime.rs#L72-L100)，回答：为什么这个函数叫 `try_write_pattern_items`（带 `try_`）而不是 `write_pattern_items`？提示：看它的返回类型 `Result<Result<(), FormattedDateTimePatternError>, fmt::Error>`——它要区分「底层 IO 失败（`fmt::Error`）」与「字段数据缺失（`FormattedDateTimePatternError`）」两类错误。

#### 4.3.5 小练习与答案

**练习 1**：为什么文档强调 `FormattedDateTime` 「不应长期保存」？

> **答案**：它持有对 formatter 内部（pattern 借用、names 借用）的生命周期引用 `'a`，且本身只是「惰性中间态」。保存它相当于把 formatter 也钉死在内存里；正确做法是立刻转成 `String`（`write_to_string` / `format!`）再保存。

**练习 2**：`format(&Time)` 喂给 `DateTimeFormatter<YMD>` 会怎样？为什么？

> **答案**：编译失败。`format` 的 bound 要求 `I::Converted: AllInputMarkers<FSet>`，而 `Time` 不提供 `YMD` 需要的年/月/日输入字段，trait 不满足。这正是静态字段集把「字段需求」编码进类型、在编译期拦截错误输入的体现（见 neo.rs 的 `compile_fail` 文档示例）。

---

## 5. 综合实践

把本讲三个模块串起来：写一个小程序，**对同一个 ISO 日期，用同一个字段集类型 `YMD`、两种长度（`long`/`medium`）、两个 locale（`en`、`ja`）**，打印四行输出，并尝试取出其中一条的 pattern。

```rust
// 示例代码
use icu::datetime::fieldsets::YMD;
use icu::datetime::input::Date;
use icu::datetime::DateTimeFormatter;
use icu::locale::locale;

fn main() {
    let date = Date::try_new_iso(2024, 5, 17).unwrap();
    let locales = [locale!("en"), locale!("ja")];
    let builders: [(&str, fn() -> YMD); 2] = [
        ("long",   YMD::long),
        ("medium", YMD::medium),
    ];

    for loc in locales {
        for (name, ctor) in builders {
            let dtf = DateTimeFormatter::try_new(loc.into(), ctor()).unwrap();
            println!("{:?} / {:<6} => {}", loc, name, dtf.format(&date));
        }
    }

    // 顺带验证「字段集类型不变」：两个 dtf 都是 DateTimeFormatter<YMD>
    let _a: DateTimeFormatter<YMD> =
        DateTimeFormatter::try_new(locale!("en").into(), YMD::long()).unwrap();
    let _b: DateTimeFormatter<YMD> =
        DateTimeFormatter::try_new(locale!("en").into(), YMD::medium()).unwrap();
}
```

完成后再做两件事：

1. 在「长度」之外，给 `YMD::long()` 追加 `.with_year_style(YearStyle::WithEra)`，观察 `en` 下年份末尾是否出现 `AD`（对照 4.2.3 (d) 的 `YearStyle` 表）。
2. 把 `DateTimeFormatter<YMD>` 改成 `DateTimeFormatter<CompositeFieldSet>`（参考 lib.rs 第 100-108 行的对比示例），用 `cargo build --release` 比较两次产物的二进制大小差异，亲身体验 4.2.3 (e) 所说的「动态字段集链接更多数据」。具体数值 **待本地验证**。

## 6. 本讲小结

- `icu_datetime` 遵循 UTS#35 **Semantic Skeletons**：先选 **字段集（field set，决定显示哪些字段）**，再配 **选项（options，决定以什么风格显示）**。
- `DateTimeFormatter<FSet>` 把字段集编进 **类型参数**；另有固定日历的 `FixedCalendarDateTimeFormatter<C, FSet>` 与无日历的 `NoCalendarFormatter<FSet>` 两个瘦身版。
- 静态字段集（`YMD`/`YMDT`/`T` 等，由宏批量生成）让编译器能裁掉用不到的数据，**体积最小**；动态字段集（`CompositeFieldSet` + `FieldSetBuilder`）用于运行期才知道字段集的场景，代价是链接更多数据。
- `Length` 当前有 `Short`/`Medium`（默认）/`Long` 三变体（`#[non_exhaustive]`）；`YearStyle`/`Alignment`/`TimePrecision` 等选项按字段集能力选择性可用。
- `format()` 返回 **惰性** 的 `FormattedDateTime`，它实现 `Writeable`（并借此获得 `Display`）；真正文本生成发生在写入时的 `try_write_pattern_items`，逐个写「字面量字符」与「字段」。
- 输出文本可带 **片段标注（parts）**，如 `YEAR`/`MONTH`/`DAY`，供富文本高亮；`format().pattern()` 可取出实际选用的模式用于调试。

## 7. 下一步学习建议

- **日历系统**：本讲的输入与日历转换（`to_calendar`）依赖 `icu_calendar`，下一讲 [u3-l2 日历系统与日期类型](u3-l2-calendar-and-dates.md) 会讲透 `Calendar` trait、`AnyCalendar` 与多日历转换。
- **数字格式化**：流水线里 `try_write_field` 用到 `DecimalFormatter` 来格式化年/日等数字，见 [u3-l3 十进制数字格式化](u3-l3-decimal-formatting.md)。
- **数据机制**：`try_new` 背后的 compiled data 与 `Baked` provider 属于 DataProvider 体系，见 [u5 数据提供器系统](u5-l1-dataprovider-core.md)；想自定义数据可用 `try_new_with_buffer_provider`（需 `serde`）。
- **Writeable 深入**：`FormattedDateTime` 的惰性输出根基是 `writeable` crate，见 [u6-l5 writeable 高效字符串构建](u6-l5-writeable.md)。
- **模式串组件**：`try_write_pattern_items` 处理的「字面量 + 字段」模式来自 `components/pattern`，见 [u8-l3 pattern 模式串解析组件](u8-l3-pattern-component.md)。
