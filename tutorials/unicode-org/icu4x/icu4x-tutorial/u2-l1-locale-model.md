# Locale 与 LanguageIdentifier 数据模型

## 1. 本讲目标

国际化（i18n）的所有格式化、排序、分段组件，输入都从「一个 locale」开始。本讲聚焦 `icu_locale_core` 里的两个核心类型：`Locale` 与 `LanguageIdentifier`。读完本讲后你应当能够：

- 说清 `Locale` 与 `LanguageIdentifier` 各自由哪些字段组成、二者是什么包含关系。
- 会用 `locale!` / `langid!` 宏在**编译期**构造 locale，也会用 `.parse()` / `try_from_str` 在**运行期**解析字符串。
- 理解 locale 在真实格式化 API 里如何流转：先转成组件专用的「偏好（preferences）」，再被进一步压缩成数据管道用的 `DataLocale`。

本讲是单元 u2 的入口，也是 u3（格式化）、u4（文本处理）几乎所有组件的公共前置。

## 2. 前置知识

- **locale（语言区域标识）**：用一个字符串同时表达「说什么语言、用什么文字、在哪个地区」。例如 `zh-Hant-TW` 表示「中文、繁体字、台湾地区」，`es-419` 表示「西班牙语、拉丁美洲」。
- **BCP-47 / UTS #35**：互联网工程任务组（IETF）的 BCP-47 定义了语言标签的语法（`语言-文字-地区-变体`），Unicode 联盟的 UTS #35 在其上扩展了 `Unicode Locale Identifier`，允许追加 `-u-...` 这样的「Unicode 扩展」来表达更细的偏好（如日历、数字系统）。`icu_locale_core` 实现的就是 UTS #35。
- **子标签（subtag）**：标签里被 `-` 分隔的每一段，例如 `zh`、`Hant`、`TW` 都是子标签。
- **`no_std` / `alloc`**：`icu_locale_core` 默认不依赖标准库（`no_std`），只依赖 `alloc`（堆分配）。这让 locale 类型能跑在嵌入式环境里。许多构造方法标注了 `✨ *Enabled with the alloc Cargo feature.*`，即需要堆分配。
- 如果你还不会用 `cargo` 跑一个依赖 `icu` 的小程序，请先回顾 u1-l3「搭建环境与运行第一个 ICU4X 应用」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [components/locale_core/src/lib.rs](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/lib.rs) | crate 入口，定义模块边界、`pub use` 导出三大类型、说明「拿不准就用 `Locale`」的总原则。 |
| [components/locale_core/src/locale.rs](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/locale.rs) | `Locale` 结构体定义、解析/规范化/比较方法、与 `LanguageIdentifier` 的互转。 |
| [components/locale_core/src/langid.rs](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/langid.rs) | `LanguageIdentifier` 结构体定义及其解析/比较方法。 |
| [components/locale_core/src/macros.rs](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/macros.rs) | `langid!` 与 `locale!` 两个声明宏，负责编译期构造。 |
| [components/locale_core/src/data.rs](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/data.rs) | `DataLocale`——数据管道专用的精简 locale（本讲模块 4.3 引出，细节留到 u2 后续与 u5）。 |
| [components/locale_core/src/parser/mod.rs](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/parser/mod.rs) | 解析器基础设施，含按 `-` 切片的 `SubtagIterator`。 |

## 4. 核心概念与源码讲解

### 4.1 Locale 与 LanguageIdentifier 数据结构

#### 4.1.1 概念说明

ICU4X 用**两个**类型表达「语言区域」，并且有清晰的层级关系：

- `LanguageIdentifier`（语言标识符）：`语言 + 文字(script) + 地区(region) + 变体(variants)`，对应 BCP-47 的「Unicode BCP47 Language Identifier」。它是「不带扩展」的核心部分。
- `Locale`（区域）：一个 `LanguageIdentifier` **加上**一组 Unicode 扩展（`-u-ca-buddhist`、`-u-nu-latn` 等）。

换句话说，`LanguageIdentifier` 是 `Locale` 的一个**严格子集**。crate 的模块文档把二者关系和取舍讲得非常直白：

> [`Locale`] is the most common structure ... In almost all cases, this struct should be used as the base unit ...
> [`LanguageIdentifier`] is a strict subset of [`Locale`] which can be useful in a narrow range of cases where [`Unicode Extensions`] are not relevant.
> **If in doubt, use [`Locale`].**

这段话出自 [lib.rs:28-35](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/lib.rs#L28-L35)，记住一句口诀：**拿不准就用 `Locale`**。

#### 4.1.2 核心流程

两个结构体字段的对应关系如下：

| 字段 | `LanguageIdentifier` | `Locale` | 含义 | 示例值 |
| --- | --- | --- | --- | --- |
| language | ✓ | 通过 `id` | 语言主标签 | `zh`、`es`、`und` |
| script | ✓（Option） | 通过 `id` | 文字/书写系统 | `Hant`、`Latn` |
| region | ✓（Option） | 通过 `id` | 国家/地区 | `TW`、`419` |
| variants | ✓ | 通过 `id` | 变体标签集合 | `valencia` |
| extensions | ✗ | ✓（顶层字段） | Unicode/Transform/Private 等扩展 | `-u-ca-buddhist` |

`Locale` 并不是把上述字段平铺，而是**内嵌**一个 `LanguageIdentifier`。这一点会直接影响你怎么写代码——后面实践里你会看到 `loc.id.language` 而不是 `loc.language`。

一个特殊常量是「未知 locale」`und`（undetermined，未确定），它是 `Locale::UNKNOWN` 与 `LanguageIdentifier::UNKNOWN` 的取值，也是 ICU4X 回退链（u2-l4）的最终落脚点。

数据结构的设计思路可以概括为：**先有精确的语言标识，再在其上叠加扩展，得到完整的 locale**。

#### 4.1.3 源码精读

`Locale` 的定义极简，只有两个字段：

```rust
// locale.rs:100-107
#[derive(PartialEq, Eq, Clone, Hash)] // no Ord or PartialOrd: see docs
#[allow(clippy::exhaustive_structs)] // This struct is stable (and invoked by a macro)
pub struct Locale {
    /// The basic language/script/region components in the locale identifier along with any variants.
    pub id: LanguageIdentifier,
    /// Any extensions present in the locale identifier.
    pub extensions: extensions::Extensions,
}
```

引用：[locale.rs:100-107](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/locale.rs#L100-L107) —— 注意 `Locale` 内嵌一个 `id: LanguageIdentifier`，外加一个 `extensions`。字段都是 `pub`，所以 `loc.id.language`、`loc.id.script`、`loc.id.region` 这样访问是合法且常见的。

`LanguageIdentifier` 则把四个子标签字段平铺出来：

```rust
// langid.rs:86-97
#[derive(PartialEq, Eq, Clone, Hash)] // no Ord or PartialOrd: see docs
#[allow(clippy::exhaustive_structs)] // This struct is stable (and invoked by a macro)
pub struct LanguageIdentifier {
    /// Language subtag of the language identifier.
    pub language: subtags::Language,
    /// Script subtag of the language identifier.
    pub script: Option<subtags::Script>,
    /// Region subtag of the language identifier.
    pub region: Option<subtags::Region>,
    /// Variant subtags of the language identifier.
    pub variants: subtags::Variants,
}
```

引用：[langid.rs:86-97](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/langid.rs#L86-L97) —— `script` 和 `region` 是 `Option`（可缺省），`language` 与 `variants` 总是存在（`variants` 为空集时表示无变体）。

两个类型都有一个 `UNKNOWN` 常量，直接用宏在常量上下文里造出 `"und"`：

```rust
// locale.rs:133-135
impl Locale {
    /// The unknown locale "und".
    pub const UNKNOWN: Self = crate::locale!("und");
```

引用：[locale.rs:133-135](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/locale.rs#L133-L135)，`LanguageIdentifier` 对应为 [langid.rs:99-101](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/langid.rs#L99-L101)。

**关于排序的一个易错点**：`Locale` 和 `LanguageIdentifier` 都**故意不实现** `Ord`/`PartialEq` 之外的排序 trait（见源码注释 `// no Ord or PartialOrd: see docs`）。原因是 locale 有多种合理的排序方式，库不想替你做选择。如果你确实要排序，请用库显式提供的两种：`strict_cmp`（按字符串字节序，适合稳定序列化/二分查找）或 `total_cmp`（按结构体字段序，适合放进 `BTreeSet`）。见 [locale.rs:23-31](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/locale.rs#L23-L31) 与实现 [locale.rs:264-266](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/locale.rs#L264-L266)、[locale.rs:374-376](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/locale.rs#L374-L376)。

二者之间还有双向的 `From` 转换：`LanguageIdentifier → Locale` 会补上空的扩展集合；`Locale → LanguageIdentifier` 直接丢弃扩展、只取 `id`：

```rust
// locale.rs:495-508
impl From<LanguageIdentifier> for Locale {
    fn from(id: LanguageIdentifier) -> Self {
        Self { id, extensions: extensions::Extensions::default() }
    }
}

impl From<Locale> for LanguageIdentifier {
    fn from(loc: Locale) -> Self { loc.id }
}
```

引用：[locale.rs:495-508](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/locale.rs#L495-L508) —— 这个「`Locale → LanguageIdentifier` 会丢扩展」的行为，正是后面 `DataLocale` 也要进一步精简的思想源头。

#### 4.1.4 代码实践

1. **实践目标**：用类型系统亲自验证「`Locale` 内嵌 `LanguageIdentifier`」这个结构。
2. **操作步骤**：在一个依赖了 `icu` 的二进制项目里编写：
    ```rust
    // 示例代码
    use icu::locale::{langid, Locale};

    fn main() {
        // 用宏造一个 Locale
        let loc: Locale = "zh-Hant-TW".parse().unwrap();
        // LanguageIdentifier 是 Locale 的字段
        let id: &icu::locale::LanguageIdentifier = &loc.id;
        println!("{:?}", id); // zh-Hant-TW

        // 反向：LanguageIdentifier -> Locale（补空扩展）
        let from_id = Locale::from(langid!("en-US"));
        assert_eq!(from_id, "en-US".parse::<Locale>().unwrap());

        // 未知 locale
        assert_eq!(Locale::UNKNOWN, "und".parse::<Locale>().unwrap());
    }
    ```
3. **需要观察的现象**：`loc.id` 能直接当 `&LanguageIdentifier` 用；从 `langid!("en-US")` 转 `Locale` 后与字符串解析结果相等；`Locale::UNKNOWN` 序列化出来就是 `"und"`。
4. **预期结果**：打印 `zh-Hant-TW`，两个断言通过。如果你尝试写 `loc.language`（而不是 `loc.id.language`）会编译报错，正好印证字段在 `id` 下。
5. 若无可用运行环境，本实践可改为「源码阅读型」：在 [locale.rs:100-107](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/locale.rs#L100-L107) 确认 `id` 字段类型，并标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`Locale` 有 `extensions` 字段而 `LanguageIdentifier` 没有。如果把一个带 `-u-ca-buddhist` 扩展的 `Locale` 转成 `LanguageIdentifier`，会发生什么？

> **答案**：扩展被丢弃。`From<Locale> for LanguageIdentifier` 只取 `loc.id`（见 [locale.rs:504-508](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/locale.rs#L504-L508)），`-u-ca-buddhist` 这类 Unicode 扩展不属于 `LanguageIdentifier`，转换后信息丢失。

**练习 2**：为什么 `Locale` 不实现 `Ord`？

> **答案**：因为存在多种合理的排序（字符串字节序 vs. 结构体字段序），库不愿替用户做单一选择，故只提供 `strict_cmp` 和 `total_cmp` 两种显式方法（[locale.rs:23-31](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/locale.rs#L23-L31)）。

### 4.2 locale! 宏与运行时解析

#### 4.2.1 概念说明

构造一个 locale 有两条路，区别在于「何时校验」：

- **编译期（宏）**：`locale!("zh-Hant-TW")` / `langid!("en-US")`。宏在编译期就把字符串解析成结构体，语法非法直接编译失败（其实是 `const` 求值期 `panic!`）。优点是零运行期开销、可作为 `const` 常量；缺点是受 Rust `const` 求值能力限制，**不能**表达「多个变体」或「多个 Unicode 扩展关键字」的复杂 locale。
- **运行期（解析）**：`"zh-Hant-TW".parse::<Locale>()`、`Locale::try_from_str(...)`。能力完整，能解析任意合法的 BCP-47 字符串，但返回 `Result`，解析失败需处理错误。

两条路最终都走到同一套解析器，差别只在调用入口和可表达的范围。

#### 4.2.2 核心流程

解析流程：

```text
输入字符串  ──►  SubtagIterator 按 '-' 切片
              │
              ├──► 若按 LanguageIdentifier 模式：只解析 语言/文字/地区/变体
              └──► 若按 Locale 模式：继续解析 -u / -t / -x 等扩展
                          │
                          ▼
               对每个子标签做语法规范化（大小写：语言小写、文字首字母大写、地区大写）
                          │
                          ▼
                 组装成 LanguageIdentifier 或 Locale（失败返回 ParseError）
```

`ParserMode` 决定解析到哪一层为止：

```rust
// parser/langid.rs:17-22
pub enum ParserMode {
    LanguageIdentifier,
    Locale,
    #[allow(dead_code)]
    Partial,
}
```

引用：[parser/langid.rs:17-22](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/parser/langid.rs#L17-L22)。`LanguageIdentifier` 模式遇到扩展会停止（或报错），`Locale` 模式则会继续把扩展也吃进来。

无论哪条路，解析都只保证 **well-formed（语法正确）** 并做大小写规范化，**不做**废弃子标签替换等规范化（canonicalize）。后者由 `LocaleCanonicalizer` 负责，那是 u2-l3 的主题。

宏的关键限制在于 `const` 求值：Rust 常量上下文里很难做堆分配，所以宏只能处理「至多一个变体、至多一个 Unicode 关键字」的情形。

#### 4.2.3 源码精读

先看运行期解析入口。`LanguageIdentifier::try_from_str` 把字节交给解析器，并指定 `LanguageIdentifier` 模式：

```rust
// langid.rs:119-131
#[inline]
#[cfg(feature = "alloc")]
pub fn try_from_str(s: &str) -> Result<Self, ParseError> {
    Self::try_from_utf8(s.as_bytes())
}
...
#[cfg(feature = "alloc")]
pub fn try_from_utf8(code_units: &[u8]) -> Result<Self, ParseError> {
    parser::parse_language_identifier(code_units, parser::ParserMode::LanguageIdentifier)
}
```

引用：[langid.rs:119-131](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/langid.rs#L119-L131) —— 注意它需要 `alloc` feature。`Locale` 的对应方法 [locale.rs:153-165](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/locale.rs#L153-L165) 调用的是 `parse_locale`，会把扩展也解析进来。

解析器底层用 `SubtagIterator` 按 `-` 切片，它是一个为 `const` 与性能专门写的迭代器：

```rust
// parser/mod.rs:45-50
#[derive(Copy, Clone, Debug)]
pub struct SubtagIterator<'a> {
    remaining: &'a [u8],
    // Safety invariant: current is a prefix of remaining
    current: Option<&'a [u8]>,
}
```

引用：[parser/mod.rs:45-50](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/parser/mod.rs#L45-L50) —— 它把 `"es-419"` 这种串切成 `["es", "419"]` 再逐段校验。

再看编译期宏。`langid!` 包在一个 `const { ... }` 块里，调用一个专门的 `try_from_utf8_with_single_variant`（受限版本），失败时编译期 `panic!`：

```rust
// macros.rs:37-53
#[macro_export]
macro_rules! langid {
    ($langid:literal) => { const {
        match $crate::LanguageIdentifier::try_from_utf8_with_single_variant($langid.as_bytes()) {
            Ok((language, script, region, variant)) => $crate::LanguageIdentifier {
                language, script, region,
                variants: match variant {
                    Some(v) => $crate::subtags::Variants::from_variant(v),
                    None => $crate::subtags::Variants::new(),
                }
            },
            _ => panic!(concat!("Invalid language code: ", $langid, " ...")),
        }
    }};
}
```

引用：[macros.rs:37-53](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/macros.rs#L37-L53)。`locale!` 宏结构相同，只是多解析一个「单个 Unicode 关键字扩展」，见 [macros.rs:119-158](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/macros.rs#L119-L158)。

宏的文档明确列出几类**编译期会失败**的复杂 locale，例如多个变体 `sl-IT-rozaj-biske-1994`、多个关键字 `th-TH-u-ca-buddhist-nu-thai`，并建议改用运行期 `.parse()`（见 [macros.rs:71-94](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/macros.rs#L71-L94)）。

#### 4.2.4 代码实践

1. **实践目标**：完成规格要求的实践——用 `locale!` 宏创建 `zh-Hant-TW`，用运行期解析处理 `es-419`，分别打印 language/script/region。
2. **操作步骤**：
    ```rust
    // 示例代码
    use icu::locale::locale;

    fn main() {
        // 编译期构造：zh-Hant-TW（语言+文字+地区，无变体无扩展，宏可处理）
        let zh: icu::locale::Locale = locale!("zh-Hant-TW");
        println!("zh  -> language={:?} script={:?} region={:?}",
            zh.id.language, zh.id.script, zh.id.region);

        // 运行期解析：es-419（419 是 UN 区域代码，拉丁美洲）
        let es: icu::locale::Locale = "es-419".parse().unwrap();
        println!("es  -> language={:?} script={:?} region={:?}",
            es.id.language, es.id.script, es.id.region);

        // 验证大小写规范化：传入乱写的大小写，解析后被规整
        let messy: icu::locale::Locale = "eN-lAtN-Us".parse().unwrap();
        assert_eq!(messy, locale!("en-Latn-US"));
    }
    ```
3. **需要观察的现象**：`zh` 的 script 是 `Some(Hant)`、region 是 `Some(TW)`；`es-419` 的 script 是 `None`、region 是 `Some(419)`；乱写大小写的 `eN-lAtN-Us` 被规整成 `en-Latn-US`。
4. **预期结果**：两行输出分别是 `language=zh script=Some(Hant) region=Some(TW)` 与 `language=es script=None region=Some(419)`，断言通过。
5. 若无法运行，请改写为「源码阅读型实践」：阅读 [macros.rs:120-148](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/macros.rs#L120-L148) 确认 `zh-Hant-TW` 不含变体与扩展，落在宏支持范围内，并标注「待本地验证」。

> 顺带一提：`LanguageIdentifier::is_unknown()`（[langid.rs:178-183](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/langid.rs#L178-L183)）可以判断一个标识符是否就是 `und`（语言未知、无文字/地区/变体），这在数据回退逻辑里很常用。

#### 4.2.5 小练习与答案

**练习 1**：`locale!("sl-IT-rozaj-biske-1994")` 能编译通过吗？为什么？

> **答案**：不能。它有两个以上变体（`rozaj`、`biske`、`1994`），超出宏「至多一个变体」的 `const` 限制，会在编译期 `panic!`。源码在 [macros.rs:75-77](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/macros.rs#L75-L77) 把它列为 `compile_fail` 示例，应改用 `"sl-IT-rozaj-biske-1994".parse::<Locale>()`。

**练习 2**：`Locale::try_from_str` 和 `.parse()` 有区别吗？

> **答案**：没有本质区别。`FromStr` 实现直接转调 `try_from_str`（[locale.rs:485-493](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/locale.rs#L485-L493)），`.parse()` 走的就是 `FromStr`，二者最终都调用 `parse_locale`。

### 4.3 Locale 在格式化 API 中的角色（DataLocale / preferences）

#### 4.3.1 概念说明

当你把一个 `Locale` 交给 `DateTimeFormatter`、`DecimalFormatter` 这些组件时，它并不是被原样使用。ICU4X 在「用户侧的完整 `Locale`」和「数据查找用的键」之间，放了一个中间层，目的是**体积与性能**：

- **`Locale`（用户侧，完整）**：168 字节（64 位下，含庞大的扩展集合），功能最全。
- **组件 preferences（偏好）**：每个组件用 `define_preferences!` 宏定义自己的偏好类型，例如 `DecimalFormatterPreferences`。它只挑出**本组件关心的** Unicode 扩展关键字（如数字格式化只关心 `-u-nu` 数字系统），其余丢弃。
- **`DataLocale`（数据管道侧，精简）**：为「回退（fallback）+ 数据查找」专门优化的类型，功能比 `Locale` 少、比 `LanguageIdentifier` 略多，且实现了 `Copy`，传递开销极小。

这条精简链路的终点，就是 `DataLocale`。它的文档原话：

> [`DataLocale`] contains less functionality than [`Locale`] but more than [`LanguageIdentifier`] for better size and performance while still meeting the needs of the ICU4X data pipeline.

#### 4.3.2 核心流程

一个 locale 从用户输入到驱动格式化的旅程：

```text
用户字符串 "ar-EG-u-nu-latn"
        │  .parse()
        ▼
     Locale                    ← 完整，含 extensions
        │  From<Locale>（由 define_preferences! 生成）
        ▼
DecimalFormatterPreferences    ← 只保留本组件关心的 -u-nu 关键字
        │  传入 DecimalFormatter::try_new(prefs, options)
        ▼
  进一步转成 DataLocale        ← 精简、Copy，供数据管道查找符号表
        │
        ▼
   命中 compiled data → 输出本地化字符串
```

要点：组件构造函数接收的是 **preferences**（不是裸 `Locale`），但因为有 `From<Locale>`，你直接把 `&locale` 或 `locale` 传进去即可，编译器会自动转换。`DataLocale` 通常你不需要手动构造，它由组件内部生成。

#### 4.3.3 源码精读

`DataLocale` 的字段比 `LanguageIdentifier` 还要「抠门」——变体只留**单个** `Option<Variant>`（而不是集合），并多了一个用于区域细分的 `subdivision`（对应 `-u-sd`），且整体 `#[derive(Clone, Copy)]`：

```rust
// data.rs:70-84
#[derive(Clone, Copy)]
#[non_exhaustive]
pub struct DataLocale {
    /// Language subtag
    pub language: Language,
    /// Script subtag
    pub script: Option<Script>,
    /// Region subtag
    pub region: Option<Region>,
    /// Variant subtag
    pub variant: Option<Variant>,
    /// Subivision (-u-sd-) subtag
    pub subdivision: Option<Subtag>,
}
```

引用：[data.rs:70-84](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/data.rs#L70-L84) —— 注意它 `Copy`，可以随意按值复制；`non_exhaustive` 表示未来可能加字段。三个类型的体积对照可在一个测试里看到：`LanguageIdentifier` 为 32 字节、`Locale` 为 168 字节（见 [locale.rs:118-130](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/locale.rs#L118-L130)），`DataLocale` 因字段更少更小。这就是「数据管道用 `DataLocale` 而非 `Locale`」的性能动机。

在组件侧，preferences 由宏批量生成，并且自动带上 `From<Locale>` 与 `From<&Locale>`：

```rust
// preferences/mod.rs:489-495（define_preferences! 宏展开的一部分）
        impl From<$crate::Locale> for $name { ... }
        impl From<&$crate::Locale> for $name { ... }
```

引用：[preferences/mod.rs:489-495](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/preferences/mod.rs#L489-L495) —— 这两行是 `define_preferences!` 宏为每个偏好类型生成的转换实现，正是它们让你能把 `Locale` 直接喂给格式化器。

以 `DecimalFormatter` 为例，它的构造函数声明接收 `DecimalFormatterPreferences`，而该偏好由 `define_preferences!` 定义、只挑了 `numbering_system`（`-u-nu`）一个关键字：

```rust
// components/decimal/src/preferences.rs:11-24
define_preferences!(
    /// The preferences for fixed decimal formatting.
    [Copy]
    DecimalFormatterPreferences,
    {
        /// The user's preferred numbering system.
        /// Corresponds to the `-u-nu` in Unicode Locale Identifier.
        numbering_system: NumberingSystem
    }
);
```

引用：[preferences.rs:11-24](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/decimal/src/preferences.rs#L11-L24)。对应的构造入口由宏生成，签名形如 `try_new(prefs: DecimalFormatterPreferences, options: DecimalFormatterOptions)`（[decimal_formatter.rs:48-52](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/decimal/src/decimal_formatter.rs#L48-L52)），由于 `DecimalFormatterPreferences: From<Locale>`，你只需 `DecimalFormatter::try_new(&locale, Default::default())` 即可。

`DataLocale` 的官方建议是：**不要直接构造它**，即便存在 `From<Locale>`，也应经由 `LocalePreferences` 中转（见 [data.rs:24-50](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/data.rs#L24-L50)），因为直接转换会隐式采用「语言优先」回退，对部分场景不准确。这部分细节留到 u2-l4（回退链）和 u5（数据提供器）深入。

#### 4.3.4 代码实践

1. **实践目标**：观察 `Locale` 如何被组件接收、并体会「preferences 只挑关心的扩展」。
2. **操作步骤**：
    ```rust
    // 示例代码
    use icu::decimal::{DecimalFormatter, DecimalFormatterOptions};
    use icu::locale::locale;

    fn main() {
        // 带数字系统扩展的 locale：阿拉伯语、埃及、拉丁数字系统
        let loc = locale!("ar-EG-u-nu-latn");

        // 直接把 &loc 传进去：经 From<&Locale> 转 DecimalFormatterPreferences
        let options = DecimalFormatterOptions::default();
        let fmt = DecimalFormatter::try_new(&loc, options)
            .expect("compiled data should contain ar-EG");

        use icu::locale::Locale;
        // 验证：完整 Locale 仍在，扩展没丢
        let full: Locale = loc;
        println!("locale = {:?}", full);
    }
    ```
3. **需要观察的现象**：构造函数能接受 `&loc`（说明存在 `From<&Locale>`）；`-u-nu-latn` 被 preferences 捕获用于选择数字系统；`full` 仍是完整的 `ar-EG-u-nu-latn`，扩展在用户侧 `Locale` 里并未丢失。
4. **预期结果**：打印 `locale = "ar-EG-u-nu-latn"`，格式化器构造成功。
5. 若无可运行环境，改为阅读 [preferences.rs:11-24](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/decimal/src/preferences.rs#L11-L24) 与 [preferences/mod.rs:489-495](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/preferences/mod.rs#L489-L495)，说明「为何能把 `Locale` 直接传给 `try_new`」，并标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么数据管道用 `DataLocale` 而不是 `Locale`？

> **答案**：为了体积与性能。`Locale` 含庞大的扩展集合（168 字节，[locale.rs:130](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/locale.rs#L130)），而数据查找只需要语言/文字/地区/单个变体/细分区域，`DataLocale` 字段更少且 `Copy`（[data.rs:70-84](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/data.rs#L70-L84)），按值传递几乎零成本。

**练习 2**：`DecimalFormatterPreferences` 只挑了 `numbering_system` 一个关键字。如果我给一个带 `-u-ca-buddhist`（日历）扩展的 `Locale`，数字格式化器会用到日历信息吗？

> **答案**：不会。日历关键字不属于数字格式化的偏好范围（[preferences.rs:11-24](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/decimal/src/preferences.rs#L11-L24) 只声明了 `numbering_system`），转换时被忽略。日历信息要交给 `DateTimeFormatter` 那一类组件。

## 5. 综合实践

把本讲三个模块串起来：写一个小程序，把同一个用户字符串分别解释成 `LanguageIdentifier`、`Locale`、并观察它喂给一个组件后的精简形态。

1. **实践目标**：亲手走一遍「字符串 → `Locale` → 组件 preferences → 数据查找」的完整链路，体会三种 locale 表达的功能取舍。
2. **操作步骤**（示例代码）：
    ```rust
    use icu::locale::{LanguageIdentifier, Locale, locale};
    use icu::decimal::{DecimalFormatter, DecimalFormatterOptions};

    fn main() {
        let input = "es-419-u-nu-latn";

        // (1) 只当语言标识符：扩展会被忽略
        let li: LanguageIdentifier = input.parse().unwrap();
        println!("LanguageIdentifier = {:?} (region={:?})", li, li.region);

        // (2) 当完整 locale：扩展保留
        let loc: Locale = input.parse().unwrap();
        println!("Locale             = {:?}", loc);

        // (3) 喂给组件：自动转 preferences
        let fmt = DecimalFormatter::try_new(
            &locale!("es-419-u-nu-latn"),
            DecimalFormatterOptions::default(),
        ).expect("data present");

        // (4) 未知 locale 永远是合法兜底
        assert!(LanguageIdentifier::UNKNOWN.is_unknown());
        println!("fmt ready, unknown langid = {:?}", LanguageIdentifier::UNKNOWN);
    }
    ```
3. **需要观察的现象**：
    - (1) 中 `li` 不含 `-u-nu-latn`，`region` 为 `Some(419)`。
    - (2) 中 `loc` 完整保留扩展，打印 `es-419-u-nu-latn`。
    - (3) 中能直接传 `&locale!(...)`，编译通过证明存在 `From<&Locale>`。
    - (4) 中 `UNKNOWN` 通过 `is_unknown()` 判定。
4. **预期结果**：四步全部成功，输出与上述描述一致。重点体会：同一字符串，用 `LanguageIdentifier` 解析会「丢扩展」，用 `Locale` 解析「全保留」，交给组件时又被「按需精简」成 preferences/`DataLocale`。
5. 若无运行环境，请将本实践改为阅读 [locale.rs:155-165](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/locale.rs#L155-L165)、[langid.rs:172-175](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/langid.rs#L172-L175)（`try_from_locale_bytes` 会丢弃扩展）与 [data.rs:18-23](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/data.rs#L18-L23)，在源码层面完成同样的对照，并标注「待本地验证」。

## 6. 本讲小结

- ICU4X 用 `Locale`（完整，含扩展）与 `LanguageIdentifier`（其严格子集，不含扩展）两个类型表达语言区域；**拿不准就用 `Locale`**（[lib.rs:28-35](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/lib.rs#L28-L35)）。
- `Locale` 内嵌一个 `id: LanguageIdentifier`，所以访问语言/文字/地区要写 `loc.id.language` 等（[locale.rs:100-107](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/locale.rs#L100-L107)）。
- 构造有两条路：`locale!`/`langid!` 宏在编译期完成（受「单变体、单 Unicode 关键字」限制），`.parse()`/`try_from_str` 在运行期完成（能力完整、返回 `Result`）。
- 解析只做 well-formed 与大小写规范化，不做废弃标签替换；规范化交给 `LocaleCanonicalizer`（u2-l3）。
- 二者都故意不实现 `Ord`，需要排序请用 `strict_cmp`（字符串序）或 `total_cmp`（结构序）。
- 在格式化 API 里，`Locale` 经 `From<Locale>` 转成组件 preferences（只保留本组件关心的扩展），最终再压缩成 `Copy` 的 `DataLocale` 供数据管道查找——这是 ICU4X 小体积/高性能设计的一处具体体现。

## 7. 下一步学习建议

- **下一讲 u2-l2「子标签体系」**：本讲频繁出现的 `Language`/`Script`/`Region`/`Variant` 到底是什么类型、如何校验取值、为何能用整数紧凑存储，下一讲会深入 `components/locale_core/src/subtags/`。
- **u2-l3「解析、规范化与 likely subtags」**：本讲只做了 well-formed 解析，真正的规范化（`und` 替换、大小写、likely subtags 最大化/最小化）在那里展开。
- **u2-l4「回退链与文本方向性」**：本讲提到的 `und` 兜底、`DataLocale` 的回退用途，在那里系统讲解。
- **延伸阅读源码**：`components/locale_core/src/parser/mod.rs`（`SubtagIterator`）与 `components/locale_core/src/preferences/mod.rs`（`define_preferences!` 宏）能帮你把「解析」与「preferences 转换」这两条机制看透。
