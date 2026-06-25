# 实验性组件 experimental

## 1. 本讲目标

学完本讲后，你应该能够：

1. 说清楚 `icu::experimental`（`icu_experimental` crate）的「孵化器」定位、版本策略，以及它和稳定组件（`icu::datetime`、`icu::decimal` 等）的区别。
2. 知道如何启用它（`icu` 元 crate 的 `unstable` feature）。
3. 认识四个最常被使用的实验模块：`displaynames`（语言/地区显示名）、`relativetime`（相对时间）、`dimension`（货币/百分比/单位）、`transliterate`（文字转写）。
4. 重点理解 `displaynames` 在 #8135 之后的一项 API 演进：语言标识显示名 `LanguageIdentifierDisplayNameOwned` 已改用专用的 `LanguageIdentifierDisplayNameOptions`，而不再复用通用的 `DisplayNamesOptions`。
5. 能够亲手写一段依赖 `unstable` 的最小代码，并体会「实验性 API 与稳定 API 在入口结构与选项类型上的差异」。

## 2. 前置知识

本讲是「专家层」讲义，默认你已经掌握：

- **元 crate 与 feature 体系**（u1-l4）：知道 `icu` 只是把各组件 `pub use` 成 `icu::*` 模块，知道 `compiled_data` / `serde` / `sync` 等 feature 的作用。本讲会再遇到一个新的 feature——`unstable`。
- **Locale / LanguageIdentifier 数据模型**（u2-l1）：知道 `langid!("fr-CA")` 这类语言标识符由 language/script/region/variant 组成，这是 `displaynames` 的输入。
- **Decimal 与 DecimalFormatter**（u3-l3）：知道 `fixed_decimal::Decimal` 是一个精确的十进制数，`relativetime` 要格式化「5 天前」时，这个 `5` 就是一个 `Decimal`。
- **PluralRules**（u3-l4）：知道不同语言有 zero/one/two/few/many/other 等复数类别。`relativetime` 必须结合复数规则，因为「1 天前」和「5 天前」在很多语言里用不同的词尾。
- **Writeable 惰性求值**（u6-l5）：实验组件的输出对象（如 `FormattedRelativeTime`、`LanguageIdentifierDisplayNameOwned`）基本都实现 `Writeable`，文本在真正写入/打印时才生成。

两个术语先交代清楚：

- **孵化器 crate（incubator crate）**：一个专门收容「尚未稳定、API 还可能大改」的能力的 crate。能力成熟后会「毕业」迁到顶层稳定组件。
- **BCP-47-T**：BCP-47 语言标签的 `-t-` 扩展，用于描述转写（transliteration）的「源 → 目标」文字系统，例如 `und-Arab-t-und-beng` 表示「从孟加拉文转写到阿拉伯文」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [components/experimental/src/lib.rs](https://github.com/unicode-org/icu4x/blob/1569b93140c14c1261dd47f3e0e7bdc889280ddf/components/experimental/src/lib.rs) | 实验性 crate 的根，声明 8 个子模块、定义孵化器定位、内嵌 `Baked` 数据 provider |
| [components/icu/src/lib.rs](https://github.com/unicode-org/icu4x/blob/1569b93140c14c1261dd47f3e0e7bdc889280ddf/components/icu/src/lib.rs) | 元 crate 把 `icu_experimental` 以 `experimental` 模块 re-export，受 `unstable` feature 门控 |
| [components/experimental/src/displaynames/mod.rs](https://github.com/unicode-org/icu4x/blob/1569b93140c14c1261dd47f3e0e7bdc889280ddf/components/experimental/src/displaynames/mod.rs) | `displaynames` 模块入口，对比 `multi` / `single` 两种设计，re-export 选项类型 |
| [components/experimental/src/displaynames/options.rs](https://github.com/unicode-org/icu4x/blob/1569b93140c14c1261dd47f3e0e7bdc889280ddf/components/experimental/src/displaynames/options.rs) | 选项类型：`DisplayNamesOptions` 与（#8135 新增的）`LanguageIdentifierDisplayNameOptions` |
| [components/experimental/src/displaynames/single/language.rs](https://github.com/unicode-org/icu4x/blob/1569b93140c14c1261dd47f3e0e7bdc889280ddf/components/experimental/src/displaynames/single/language.rs) | `LanguageIdentifierDisplayNameOwned`：把 `fr-CA` 显示成 `Canadian French` 的核心类型 |
| [components/experimental/src/relativetime/relativetime.rs](https://github.com/unicode-org/icu4x/blob/1569b93140c14c1261dd47f3e0e7bdc889280ddf/components/experimental/src/relativetime/relativetime.rs) | `RelativeTimeFormatter`：相对时间格式化器与 24 个构造函数 |
| [components/experimental/src/relativetime/options.rs](https://github.com/unicode-org/icu4x/blob/1569b93140c14c1261dd47f3e0e7bdc889280ddf/components/experimental/src/relativetime/options.rs) | `RelativeTimeFormatterOptions` 与 `Numeric` 枚举 |
| [components/experimental/src/transliterate/mod.rs](https://github.com/unicode-org/icu4x/blob/1569b93140c14c1261dd47f3e0e7bdc889280ddf/components/experimental/src/transliterate/mod.rs) | `transliterate` 模块入口，re-export `Transliterator` / `TransliteratorBuilder` |
| [components/experimental/src/dimension/mod.rs](https://github.com/unicode-org/icu4x/blob/1569b93140c14c1261dd47f3e0e7bdc889280ddf/components/experimental/src/dimension/mod.rs) | `dimension` 模块入口，含 currency / percent / units 三个子模块 |

---

## 4. 核心概念与源码讲解

### 4.1 experimental：孵化器 crate 的定位与版本策略

#### 4.1.1 概念说明

ICU4X 的稳定组件（`icu_datetime`、`icu_collator` 等）遵循严格的 SemVer，API 一旦发布就尽量不再破坏。但 i18n 领域永远有「想做但还没想清楚最终形态」的新能力——货币格式化、文字转写、人名格式化、相对时间……这些能力需要时间打磨，又不能让用户等到完全稳定才能用。

`icu_experimental`（在 `icu` 元 crate 里以 `icu::experimental` 出现）就是为这类能力准备的**孵化器**：它让能力**现在就能被使用**，同时明确告知「API 不稳定」。crate 文档里写得很直白：

> 🚧 The experimental development module of the ICU4X project.
> It will usually undergo a major SemVer bump for every ICU4X release. Components in this crate will eventually stabilize and move to their own top-level components.

三个关键含义：

1. **每个 ICU4X 版本都会做大版本号 bump**：`0.1 → 0.2 → 0.3 …`，不保证向后兼容。当前版本是 `0.6.0-dev`，仍是 pre-1.0。
2. **「毕业」机制**：成熟后会迁出，成为独立的顶层稳定组件（就像 `datetime` 当年经历过的一样）。
3. **它既是独立 crate，也作为 `icu` 的一部分发布**：用户可以单独依赖 `icu_experimental`，也可以通过 `icu` 元 crate 的 `experimental` 模块访问。

#### 4.1.2 核心流程

启用实验能力的调用流程：

```text
用户 Cargo.toml
   ├── 方式 A：依赖 icu，开启 unstable feature
   │       → icu::experimental::xxx  （推荐，与其它组件同命名空间）
   └── 方式 B：单独依赖 icu_experimental
           → icu_experimental::xxx

进入 experimental 后
   ├── displaynames   语言/地区显示名
   ├── dimension      货币 / 百分比 / 单位
   ├── duration       持续时间
   ├── measure        度量
   ├── personnames    人名
   ├── relativetime   相对时间
   ├── transliterate  文字转写
   └── units          单位换算
```

注意：`unstable` 这个 feature 名本身就是给用户的**契约**——你主动打开了「不稳定」的开关，等于接受了它随时可能改的代价。

#### 4.1.3 源码精读

crate 根的开头两段就点明了定位与约束。

首先，与所有稳定组件一样，它是 `no_std` 友好的，并在非测试环境下禁用 `panic`/`unwrap` 等便捷但危险的操作——这是 ICU4X「客户端/嵌入式友好」全局风格的体现：

[components/experimental/src/lib.rs:6-24](https://github.com/unicode-org/icu4x/blob/1569b93140c14c1261dd47f3e0e7bdc889280ddf/components/experimental/src/lib.rs#L6-L24)
```rust
#![cfg_attr(not(any(test, doc)), no_std)]
//! 🚧 The experimental development module of the `ICU4X` project.
//!
//! It will usually undergo a major `SemVer` bump for every ICU4X release.
//! Components in this crate will eventually stabilize and move to their own
//! top-level components.
```

接着是 8 个子模块的声明，正是本讲要分别认识的对象：

[components/experimental/src/lib.rs:30-37](https://github.com/unicode-org/icu4x/blob/1569b93140c14c1261dd47f3e0e7bdc889280ddf/components/experimental/src/lib.rs#L30-L37)
```rust
pub mod dimension;
pub mod displaynames;
pub mod duration;
pub mod measure;
pub mod personnames;
pub mod relativetime;
pub mod transliterate;
pub mod units;
```

元 crate 侧，`experimental` 是受 `unstable` feature 门控的可选 re-export：

[components/icu/src/lib.rs:179-181](https://github.com/unicode-org/icu4x/blob/1569b93140c14c1261dd47f3e0e7bdc889280ddf/components/icu/src/lib.rs#L179-L181)
```rust
#[doc(inline)]
#[cfg(feature = "unstable")]
pub use icu_experimental as experimental;
```

对应的依赖声明也是 `optional = true`，并在 `unstable` feature 里被聚合启用——这正是一处可以亲手验证「关掉 feature 就消失」的开关：

[components/icu/Cargo.toml:37](https://github.com/unicode-org/icu4x/blob/1569b93140c14c1261dd47f3e0e7bdc889280ddf/components/icu/Cargo.toml#L37)
```toml
icu_experimental = { workspace = true, optional = true }
```

> 提示：`lib.rs` 里还有一个 `pub mod provider`（带 `#[doc(hidden)]`），里面是 `compiled data` 用的内嵌 `Baked` provider。它把上面所有实验组件的数据 marker（如 `impl_locale_names_language_medium_v1!(Baked)`、`impl_transliterator_rules_v1!(Baked)`）挂在一个 `Baked` 结构上——这与稳定组件的 baked data 机制完全一致，是 u5-l4「存储后端」讲过的模式。

#### 4.1.4 代码实践

**实践目标**：亲手验证「`unstable` feature 是访问 `experimental` 的唯一入口」。

**操作步骤**：

1. 新建一个二进制项目并加依赖：
   ```bash
   cargo new exp_demo --bin
   cd exp_demo
   cargo add icu
   ```
2. 在 `Cargo.toml` 里**不要**开启 `unstable` feature，编写 `src/main.rs`：
   ```rust
   fn main() {
       let _ = icu::experimental::relativetime::RelativeTimeFormatter::try_new_long_second;
   }
   ```
3. 运行 `cargo build`，**预期编译失败**，错误信息类似 `unresolved module experimental`。
4. 在 `Cargo.toml` 中改为开启 feature：
   ```toml
   [dependencies]
   icu = { version = "*", features = ["unstable"] }
   ```
5. 再次 `cargo build`，确认模块可解析。

**需要观察的现象**：第 3 步失败、第 5 步成功的对比。

**预期结果**：`experimental` 模块随 `unstable` feature 的开关而出现/消失，印证它确实是「可选且不稳定」的能力。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `icu_experimental` 当前版本是 `0.6.0-dev` 而不是 `1.x`？

> **参考答案**：因为它是孵化器 crate，遵循「每个 ICU4X 版本做一次大版本 bump、不保证向后兼容」的策略，仍是 pre-1.0。版本号本身就向用户传递了「API 不稳定」的信号。

**练习 2**：`experimental` 里的某项能力成熟后，会以什么方式让用户感知到「它稳定了」？

> **参考答案**：它会「毕业」迁出 experimental，成为一个独立的顶层稳定组件，并在 `icu` 元 crate 中以不带 `unstable` 门控的模块出现；同时遵守正常的 SemVer 兼容承诺。

---

### 4.2 displaynames：语言/地区显示名（重点：#8135 的选项类型演进）

#### 4.2.1 概念说明

`displaynames` 解决的问题是：**把一个语言代码/地区代码翻译成「给人看」的名字**。例如把 BCP-47 里的 `fr-CA` 显示成英语里的 `Canadian French`、把 `US` 显示成 `United States`。

这是一个仍在打磨设计、尚未定型的组件。模块文档明确说存在两套并行设计，并公开征求反馈：

[components/experimental/src/displaynames/mod.rs:5-16](https://github.com/unicode-org/icu4x/blob/1569b93140c14c1261dd47f3e0e7bdc889280ddf/components/experimental/src/displaynames/mod.rs#L5-L16)
```rust
//! Display names for languages and regions.
//!
//! There are currently two designs for how to use this component:
//!
//! 1. [`multi`]: Load multiple display names at once.
//! 2. [`single`]: Load a single display name at a time.
//!
//! There are multiple use cases for this component, so we are not yet committed
//! to either of these designs being the "primary" design. Please share feedback at
//! <https://github.com/unicode-org/icu4x/issues/7824>.
//!
//! Note: Currently, the data between the two modules is NOT being shared.
```

- **`multi`**：一次构造一个能查询多个名字的对象（如 `RegionDisplayNames`，调用 `.of(region!("US"))`）。
- **`single`**：一次只加载需要的那个名字（如 `RegionDisplayNameOwned`），适合只需要一两个名字的场景。

本讲聚焦 `single` 里的 `LanguageIdentifierDisplayNameOwned`，因为它正是 **#8135 改动的对象**。

#### 4.2.2 核心流程

把 `fr-CA` 显示成 `Canadian French` 的流程（Dialect 模式，默认）：

```text
输入：subject = LanguageIdentifier("fr-CA")，formatting_locale = "en"
  │
  ▼  Step 1：按"方言优先级"尝试加载最有信息量的基础名
  尝试组合 (language+script+region) → (language+script) → (language+region)
  命中 "fr-CA" 的完整名字？ → 用它作为 base_name，并"消费"掉已命中的子标签
  未命中                   → 退回纯 language 名 "fr"，script/region 留作限定词
  │
  ▼  Step 2/3/4：把仍未被消费的 script / region / variant 各自查名
  │
  ▼  Step 5：加载 essentials（locale_pattern / locale_separator）
  │
  ▼  Writeable::write_to：用 locale_pattern 把 base_name 与限定词拼接
  例：base="Canadian French"，无限定词 → 直接输出 "Canadian French"
  例：base="Chinese"，region="Taiwan"   → "Chinese (Taiwan)"
```

这里的「先查最具体的组合名、命中就消费子标签」就是 **Dialect（方言）模式**的核心；另一选项 **Standard（标准）模式**则不做这种合并，按子标签逐个翻译后拼接。

#### 4.2.3 源码精读（含 #8135 变更）

**变更点总览（67a0b91c6f → 1569b93140，PR #8135）**：`LanguageIdentifierDisplayNameOwned` 原本复用通用的 `DisplayNamesOptions`（含 `style`/`fallback`/`language_display` 三个字段），现在改用**专用**的 `LanguageIdentifierDisplayNameOptions`（**仅含** `language_display` 一个字段）。理由是：对一个「语言标识」显示名来说，`style`（Short/Long/Narrow/Menu，主要服务于地区名）和 `fallback` 都没有意义，复用一个什么都装的大袋子反而误导用户。

先看通用选项（用于 region/script/variant 等名字）：

[components/experimental/src/displaynames/options.rs:26-36](https://github.com/unicode-org/icu4x/blob/1569b93140c14c1261dd47f3e0e7bdc889280ddf/components/experimental/src/displaynames/options.rs#L26-L36)
```rust
#[derive(Copy, Debug, Eq, PartialEq, Clone, Default)]
#[non_exhaustive]
pub struct DisplayNamesOptions {
    /// The optional formatting style to use for display name.
    pub style: Option<Style>,
    /// The fallback return when the system does not have the
    /// requested display name, defaults to "code".
    pub fallback: Fallback,
    /// The language display kind, defaults to "dialect".
    pub language_display: LanguageDisplay,
}
```

再看 #8135 **新增**的专用选项——只有 `language_display`，且类型是 `Option<LanguageDisplay>`（注意是 `Option` 包裹，这与通用版不同）：

[components/experimental/src/displaynames/options.rs:38-44](https://github.com/unicode-org/icu4x/blob/1569b93140c14c1261dd47f3e0e7bdc889280ddf/components/experimental/src/displaynames/options.rs#L38-L44)
```rust
/// A bag of options defining how a language identifier display name will be formatted.
#[derive(Copy, Debug, Eq, PartialEq, Clone, Default)]
#[non_exhaustive]
pub struct LanguageIdentifierDisplayNameOptions {
    /// The language display kind, defaults to "dialect".
    pub language_display: Option<LanguageDisplay>,
}
```

新选项随之被 re-export 到模块顶层（这一行 `pub use` 也是本次改动新增的）：

[components/experimental/src/displaynames/mod.rs:66-70](https://github.com/unicode-org/icu4x/blob/1569b93140c14c1261dd47f3e0e7bdc889280ddf/components/experimental/src/displaynames/mod.rs#L66-L70)
```rust
pub use displaynames::DisplayNamesPreferences;
pub use options::DisplayNamesOptions;
pub use options::Fallback;
pub use options::LanguageDisplay;
pub use options::LanguageIdentifierDisplayNameOptions;   // ← #8135 新增
pub use options::Style;
```

`LanguageIdentifierDisplayNameOwned` 的 `options` 字段类型随之从 `DisplayNamesOptions` 变为 `LanguageIdentifierDisplayNameOptions`，其文档示例也同步更新——这正是读者照抄即可运行的最小用法：

[components/experimental/src/displaynames/single/language.rs:18-39](https://github.com/unicode-org/icu4x/blob/1569b93140c14c1261dd47f3e0e7bdc889280ddf/components/experimental/src/displaynames/single/language.rs#L18-L39)
```rust
/// ```
/// use icu::experimental::displaynames::{
///     DisplayNamesPreferences, LanguageIdentifierDisplayNameOptions,
///     single::LanguageIdentifierDisplayNameOwned,
/// };
/// use icu::locale::{locale, langid};
/// use writeable::assert_writeable_eq;
///
/// let prefs = DisplayNamesPreferences::from(locale!("en"));
/// let options = LanguageIdentifierDisplayNameOptions::default();
/// let display_name = LanguageIdentifierDisplayNameOwned::try_new(
///     prefs,
///     langid!("fr-CA"),
///     options,
/// )
/// .expect("Data should load successfully");
///
/// assert_writeable_eq!(display_name, "Canadian French");
/// ```
```

对应的字段与构造器签名也都换成了新类型：

[components/experimental/src/displaynames/single/language.rs:42-63](https://github.com/unicode-org/icu4x/blob/1569b93140c14c1261dd47f3e0e7bdc889280ddf/components/experimental/src/displaynames/single/language.rs#L42-L63)
```rust
pub struct LanguageIdentifierDisplayNameOwned {
    formatting_locale: DataLocale,
    options: LanguageIdentifierDisplayNameOptions,          // ← 原为 DisplayNamesOptions
    // ...各种 payload 字段...
}

impl LanguageIdentifierDisplayNameOwned {
    icu_provider::gen_buffer_data_constructors!(
        (prefs: DisplayNamesPreferences,
         subject: icu_locale::LanguageIdentifier,
         options: LanguageIdentifierDisplayNameOptions)     // ← 原为 DisplayNamesOptions
         -> result: Result<Self, DataError>,
        functions: [ try_new, try_new_with_buffer_provider, try_new_unstable, Self ]
    );
```

因为新选项的 `language_display` 是 `Option`，使用处改用 `unwrap_or_default()` 来获得默认的 `Dialect`——这也是 #8135 改动的一处细节（原先是直接 `== LanguageDisplay::Dialect` 比较）：

[components/experimental/src/displaynames/single/language.rs:95-96](https://github.com/unicode-org/icu4x/blob/1569b93140c14c1261dd47f3e0e7bdc889280ddf/components/experimental/src/displaynames/single/language.rs#L95-L96)
```rust
        // Only try dialect if requested (which is the default)
        if options.language_display.unwrap_or_default() == LanguageDisplay::Dialect {
```

> **教学要点**：这是一个「API 成熟过程中选项类型被特化」的典型案例。当一种用法（语言标识显示名）其实只关心通用选项里的一个字段时，复用大袋子会让用户误以为还能设置 `style`/`fallback`（而设置了也不生效）。把它拆成专用、最小化的选项结构，既消除了误导，也让 `Default` 行为更清晰。这正是 experimental 孵化器存在的意义——在不破坏稳定 SemVer 的前提下迭代出更合理的 API。

#### 4.2.4 代码实践

**实践目标**：用更新后的 API 把 `fr-CA` 显示成 `Canadian French`，并切换到 `Standard` 模式观察差异。

**操作步骤**：

1. 项目开启 `unstable` feature（见 4.1.4）。
2. 编写：
   ```rust
   use icu::experimental::displaynames::{
       single::LanguageIdentifierDisplayNameOwned,
       DisplayNamesPreferences, LanguageIdentifierDisplayNameOptions, LanguageDisplay,
   };
   use icu::locale::{langid, locale};

   fn main() {
       let prefs = DisplayNamesPreferences::from(locale!("en"));

       // (a) 默认 Dialect 模式
       let opts = LanguageIdentifierDisplayNameOptions::default();
       let name = LanguageIdentifierDisplayNameOwned::try_new(
           prefs, langid!("fr-CA"), opts,
       ).expect("data");
       println!("Dialect  : {name}");

       // (b) 显式 Standard 模式
       let opts = LanguageIdentifierDisplayNameOptions {
           language_display: Some(LanguageDisplay::Standard),
       };
       let name = LanguageIdentifierDisplayNameOwned::try_new(
           prefs, langid!("fr-CA"), opts,
       ).expect("data");
       println!("Standard : {name}");
   }
   ```
3. `cargo run`。

**需要观察的现象**：(a) Dialect 模式输出合并后的 `Canadian French`；(b) Standard 模式更接近逐标签组合的形式。

**预期结果**：Dialect 模式为 `Canadian French`（与源码文档示例一致）。Standard 模式的确切措辞依 CLDR 数据而定，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 #8135 要为语言标识显示名单独造一个 `LanguageIdentifierDisplayNameOptions`，而不是继续用 `DisplayNamesOptions`？

> **参考答案**：因为语言标识显示名只用得到 `language_display` 这一个选项；`style`（Short/Long/Narrow/Menu）和 `fallback` 对它没有意义。复用通用袋子会让用户以为可以设置这些字段，造成误导。专用、最小化的选项结构消除了这种歧义。

**练习 2**：新选项里 `language_display` 的类型是 `Option<LanguageDisplay>`，而通用 `DisplayNamesOptions` 里却是裸 `LanguageDisplay`。这带来哪一行代码的变化？

> **参考答案**：使用处从 `options.language_display == LanguageDisplay::Dialect` 变成 `options.language_display.unwrap_or_default() == LanguageDisplay::Dialect`，因为现在是 `Option`，需要先 `unwrap_or_default()` 取出默认值 `Dialect`。

---

### 4.3 relativetime：相对时间格式化

#### 4.3.1 概念说明

`relativetime` 把一个带符号的数值格式化成「相对于现在」的时间描述，例如 `+5`（秒）→ `in 5 seconds`、`-10`（秒）→ `10 seconds ago`、`0`（天，Auto 模式）→ `today`/`hoy`。

它不是孤立的：相对时间天然依赖**复数规则**（「1 second ago」和「5 seconds ago」在很多语言里词尾不同），也依赖**十进制数字格式化**（决定千分位、本地数字字形）。所以 `RelativeTimeFormatter` 内部同时持有 `PluralRules` 与 `DecimalFormatter`——这正是它「站在 u3-l3/u3-l4 肩膀上」的体现。

#### 4.3.2 核心流程

```text
构造：RelativeTimeFormatter::try_new_long_day(prefs, options)
  ├── 加载复数规则数据（cardinal）
  ├── 构造一个 DecimalFormatter（决定数字字形）
  └── 加载该 (length, unit) 对应的相对时间 pattern 数据
       （length ∈ {long, short, narrow}，unit ∈ {second..year}，共 3×8=24 种 → 24 个构造函数）

格式化：formatter.format(Decimal)
  ├── 取符号：正数→将来式("in N …")，负数→过去式("N … ago")
  ├── 用 PluralRules 选出该数字对应的复数分支
  ├── 用 DecimalFormatter 把数值渲染成本地数字
  └── 把数值与单位词代入 pattern，输出 FormattedRelativeTime（Writeable）
```

`Numeric` 选项控制「是否优先使用特殊表述」：`Always`（默认）永远用数字式；`Auto` 则在数据允许时用 `today`/`yesterday`/`anteayer` 这类特殊词。

#### 4.3.3 源码精读

`RelativeTimeFormatter` 由四个字段组成，前三个都是「借来的能力」：

[components/experimental/src/relativetime/relativetime.rs:136-142](https://github.com/unicode-org/icu4x/blob/1569b93140c14c1261dd47f3e0e7bdc889280ddf/components/experimental/src/relativetime/relativetime.rs#L136-L142)
```rust
#[derive(Debug)]
pub struct RelativeTimeFormatter {
    pub(crate) plural_rules: PluralRules,            // 复数规则（u3-l4）
    pub(crate) rt: DataPayload<ErasedMarker<RelativeTimePatternData<'static>>>,
    pub(crate) options: RelativeTimeFormatterOptions,
    pub(crate) decimal_formatter: DecimalFormatter,  // 数字格式化（u3-l3）
}
```

构造函数通过一个宏批量生成 24 个（3 种 length × 8 种 unit），每个都对应一个独立的数据 marker，例如 `LongDayRelativeV1`、`NarrowYearRelativeV1`。下面是宏定义和一个典型展开（`try_new_long_second`）：

[components/experimental/src/relativetime/relativetime.rs:144-176](https://github.com/unicode-org/icu4x/blob/1569b93140c14c1261dd47f3e0e7bdc889280ddf/components/experimental/src/relativetime/relativetime.rs#L144-L176)
```rust
macro_rules! constructor {
    ($unstable: ident, $baked: ident, $buffer: ident, $marker: ty) => {
        #[cfg(feature = "compiled_data")]
        pub fn $baked(
            prefs: RelativeTimeFormatterPreferences,
            options: RelativeTimeFormatterOptions,
        ) -> Result<Self, DataError> {
            let locale = <$marker>::make_locale(prefs.locale_preferences);
            let plural_rules = PluralRules::try_new_cardinal((&prefs).into())?;
            let decimal_formatter = DecimalFormatter::try_new(
                (&prefs).into(), DecimalFormatterOptions::default())?;
            let rt: DataResponse<$marker> = crate::provider::Baked.load(/* ... */)?;
            // ...
        }
```

为什么「一种 length × unit 一个 marker」？因为相对时间数据按 `long_day`、`short_month` 等独立成 key，这样编译期只链接你真正用到的那些——又是「把需求编码进类型/数据 key 以裁体积」的思路（与 u3-l1 的 fieldset 同源）。

`format` 把符号抽出后委托给 `FormattedRelativeTime`：

[components/experimental/src/relativetime/relativetime.rs:371-381](https://github.com/unicode-org/icu4x/blob/1569b93140c14c1261dd47f3e0e7bdc889280ddf/components/experimental/src/relativetime/relativetime.rs#L371-L381)
```rust
    pub fn format(&self, value: Decimal) -> FormattedRelativeTime<'_> {
        let is_negative = value.sign() == Sign::Negative;
        FormattedRelativeTime {
            options: &self.options,
            formatter: self,
            value: value.with_sign(Sign::None),   // 数值取绝对值，符号单独记
            is_negative,
        }
    }
```

选项与 `Numeric` 枚举：

[components/experimental/src/relativetime/options.rs:9-26](https://github.com/unicode-org/icu4x/blob/1569b93140c14c1261dd47f3e0e7bdc889280ddf/components/experimental/src/relativetime/options.rs#L9-L26)
```rust
#[derive(Debug, Copy, Clone, Default, PartialEq, Eq)]
#[non_exhaustive]
pub struct RelativeTimeFormatterOptions {
    pub numeric: Numeric,
}

#[derive(Debug, Copy, Clone, PartialEq, Eq, Default)]
#[non_exhaustive]
pub enum Numeric {
    #[default]
    Always,   // 永远用数字式
    Auto,     // 数据允许时用 today/yesterday 等特殊词
}
```

源码里的文档示例是可信的参照（英语 `long_second`）：

[components/experimental/src/relativetime/relativetime.rs:60-74](https://github.com/unicode-org/icu4x/blob/1569b93140c14c1261dd47f3e0e7bdc889280ddf/components/experimental/src/relativetime/relativetime.rs#L60-L74)
```rust
/// let f = RelativeTimeFormatter::try_new_long_second(
///     locale!("en").into(), RelativeTimeFormatterOptions::default(),
/// ).expect("locale should be present");
/// assert_writeable_eq!(f.format(Decimal::from(5i8)),  "in 5 seconds");
/// assert_writeable_eq!(f.format(Decimal::from(-10i8)), "10 seconds ago");
```

#### 4.3.4 代码实践

**实践目标**：在英语下格式化「5 天后 / 5 天前」式的相对时间，并体会它对 `Decimal` 与复数规则的依赖。

**操作步骤**：

1. 依赖加上 `fixed_decimal`（`cargo add fixed_decimal`），并开启 `icu` 的 `unstable`。
2. 编写（这是基于源码示例改写、可直接编译的最小版本）：
   ```rust
   use fixed_decimal::Decimal;
   use icu::experimental::relativetime::{
       RelativeTimeFormatter, RelativeTimeFormatterOptions,
   };
   use icu::locale::locale;

   fn main() {
       let f = RelativeTimeFormatter::try_new_long_day(
           locale!("en").into(),
           RelativeTimeFormatterOptions::default(),
       ).expect("locale should be present");

       println!("{}", f.format(Decimal::from(5i8)));    // 将来
       println!("{}", f.format(Decimal::from(-5i8)));   // 过去
       println!("{}", f.format(Decimal::from(1i8)));    // 单数，触发复数分支
   }
   ```
3. `cargo run`。

**需要观察的现象**：正数得到将来式（`in N days`），负数得到过去式（`N days ago`），数值 `1` 时单位词应变为单数。

**预期结果**：依据 CLDR，英语 `long_day` 下应得到形如 `in 5 days` / `5 days ago` / `in 1 day`（注意 `day` 与 `days` 的单复数差异，这正是 `PluralRules` 的功劳）。确切的措辞**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `RelativeTimeFormatter` 内部要同时持有 `PluralRules` 和 `DecimalFormatter`？

> **参考答案**：因为相对时间的单位词随数字的复数类别变化（`1 day` vs `5 days`，俄语等还会区分 few/many），需要 `PluralRules` 选分支；而数字本身的渲染（千分位、阿拉伯-印度字形、孟加拉数字等）由 `DecimalFormatter` 负责。两者合在一起才能产出本地化的相对时间。

**练习 2**：`Numeric::Auto` 和 `Numeric::Always` 的区别是什么？

> **参考答案**：`Always`（默认）永远用数字式表述（`in 0 days`）；`Auto` 在 CLDR 数据提供特殊词时优先使用（如西班牙语 `short_day` 的 `0` → `hoy`「今天」、`-2` → `anteayer`「前天」），没有特殊词时才退回数字式。

---

### 4.4 dimension：货币、百分比与单位

#### 4.4.1 概念说明

`dimension` 是孵化器里体量最大的模块，覆盖三类「带量纲的数值」的本地化格式化：

- **currency**：货币（`CurrencyFormatter`、`CompactCurrencyFormatter`、`LongCurrencyFormatter` 等）。
- **percent**：百分比（`PercentFormatter`）。
- **units**：度量单位（`UnitsFormatter`、`CategorizedFormatter`，如长度、质量、体积）。

它们尚未稳定，API 仍在演进，因此被归在 experimental 下。

#### 4.4.2 核心流程

这类格式化器的共同骨架与 `DecimalFormatter`（u3-l3）一脉相承：

```text
数值/标识 + Locale 偏好 + Options
   │
   ├── 加载该量纲对应的符号/模式数据（compiled data）
   ├── （货币/单位）结合 PluralRules 选词、结合 DecimalFormatter 渲染数字
   └── 输出一个实现 Writeable 的格式化结果（惰性求值）
```

#### 4.4.3 源码精读

模块入口暴露三个子模块，结构清晰：

[components/experimental/src/dimension/mod.rs:12-15](https://github.com/unicode-org/icu4x/blob/1569b93140c14c1261dd47f3e0e7bdc889280ddf/components/experimental/src/dimension/mod.rs#L12-L15)
```rust
pub mod currency;
pub mod percent;
pub mod provider;
pub mod units;
```

各子模块的入口类型（均含 `try_new` 构造 + 专用 Options，遵循 ICU4X 统一的「Preferences + Options + Writeable 输出」范式）：

| 量纲 | 入口类型 | 选项类型 |
| --- | --- | --- |
| 百分比 | `PercentFormatter<R>` | `PercentFormatterOptions` |
| 货币 | `CurrencyFormatter`、`CompactCurrencyFormatter`、`LongCurrencyFormatter`、`LongCompactCurrencyFormatter` | `CurrencyFormatterOptions` |
| 单位 | `UnitsFormatter`、`CategorizedFormatter<C>` | `UnitsFormatterOptions` |

> 这些类型都定义在各自子模块里（如 [components/experimental/src/dimension/percent/formatter.rs:39](https://github.com/unicode-org/icu4x/blob/1569b93140c14c1261dd47f3e0e7bdc889280ddf/components/experimental/src/dimension/percent/formatter.rs#L39) 的 `pub struct PercentFormatter<R>`、[components/experimental/src/dimension/currency/formatter.rs:48](https://github.com/unicode-org/icu4x/blob/1569b93140c14c1261dd47f3e0e7bdc889280ddf/components/experimental/src/dimension/currency/formatter.rs#L48) 的 `pub struct CurrencyFormatter`、[components/experimental/src/dimension/units/formatter.rs:45](https://github.com/unicode-org/icu4x/blob/1569b93140c14c1261dd47f3e0e7bdc889280ddf/components/experimental/src/dimension/units/formatter.rs#L45) 的 `pub struct UnitsFormatter`）。它们背后挂载的数据 marker 数量众多（参见 lib.rs 的 `Baked` 里 `impl_currency_*`、`impl_percent_essentials_v1`、`impl_units_*` 一长串），这也解释了 dimension 为何是 experimental 里最「重」的一块。

#### 4.4.4 代码实践（源码阅读型）

**实践目标**：通过阅读源码理解 dimension 各格式化器的统一构造范式，不追求运行（这些 API 仍可能变动）。

**操作步骤**：

1. 打开 `components/experimental/src/dimension/percent/formatter.rs`，阅读 `PercentFormatter::try_new` 的签名（[第 67 行起](https://github.com/unicode-org/icu4x/blob/1569b93140c14c1261dd47f3e0e7bdc889280ddf/components/experimental/src/dimension/percent/formatter.rs#L67)）。
2. 对比 `CurrencyFormatter::try_new`（[currency/formatter.rs:77](https://github.com/unicode-org/icu4x/blob/1569b93140c14c1261dd47f3e0e7bdc889280ddf/components/experimental/src/dimension/currency/formatter.rs#L77)）与 `UnitsFormatter::try_new`（[units/formatter.rs:93](https://github.com/unicode-org/icu4x/blob/1569b93140c14c1261dd47f3e0e7bdc889280ddf/components/experimental/src/dimension/units/formatter.rs#L93)）。

**需要观察的现象**：三者是否都遵循 `(prefs, options) -> Result<Self, DataError>` 的统一签名，是否都通过 `gen_buffer_data_constructors!` 派生出 `try_new` / `try_new_with_buffer_provider` / `try_new_unstable` 三件套。

**预期结果**：是的。这说明 dimension 虽未稳定，但已经采用了与稳定组件一致的构造范式——这也是它「正在走向毕业」的信号。

#### 4.4.5 小练习与答案

**练习**：`PercentFormatter<R>` 的类型参数 `R` 大概率代表什么？这与 u3-l3 的哪个设计思路一致？

> **参考答案**：`R` 通常是一个标记类型（marker），用于区分不同的数据来源/渲染后端，使同一个格式化器能适配 compiled data 与运行时 provider 两种数据方式（与 u3-l1 `DateTimeFormatter<FSet>`「把需求编码进类型参数」、u5「compiled data vs 显式 provider」的思路一致）。

---

### 4.5 transliterate：文字转写

#### 4.5.1 概念说明

**转写（transliteration）**是把文本从一种书写系统（script）转换到另一种，例如孟加拉文 → 阿拉伯文、拉丁文 → 西里尔文、德文 → ASCII（去重音）。ICU4X 的 `transliterate` 模块实现了基于规则（rule-based）的转写，规则数据来自 CLDR。

转写的「源 → 目标」用 BCP-47-T 的 `-t-` 扩展描述。一个 locale 形如 `und-Arab-t-und-beng`，读法是：

- `t-und-beng`：源是 `und-beng`（默认语言 + 孟加拉文）。
- 前半 `und-Arab`：目标是 `und-Arab`（默认语言 + 阿拉伯文）。

#### 4.5.2 核心流程

```text
Transliterator::try_new(&locale)       // locale 形如 "und-Arab-t-und-beng"
   ├── 解析 BCP-47-T，定位转写规则数据
   ├── 编译规则（可能递归依赖嵌套转写器，如 NFD/NFKD 规范化、大小写）
   └── 组装成一个可执行的状态机

t.transliterate(input: String) -> String
   ├── 把 String 包成可修改的 Replaceable 缓冲
   ├── 按规则组逐条匹配/替换（含 filter 过滤、变量表）
   └── 返回转写后的 String
```

进阶用法 `TransliteratorBuilder`（仅 `compiled_data`）允许用户用 `replace`/`call` 添加自定义规则，甚至实现 `CustomTransliterator` trait 来覆盖嵌套转写器。

#### 4.5.3 源码精读

`Transliterator` 持有编译后的规则数据与一个「环境」（env，即嵌套转写器表）：

[components/experimental/src/transliterate/transliterator/mod.rs:178-182](https://github.com/unicode-org/icu4x/blob/1569b93140c14c1261dd47f3e0e7bdc889280ddf/components/experimental/src/transliterate/transliterator/mod.rs#L178-L182)
```rust
#[derive(Debug)]
pub struct Transliterator {
    transliterator: DataPayload<TransliteratorRulesV1>,
    env: Env,
}
```

构造入口 `try_new` 接收一个 `&Locale`（即 BCP-47-T id），内部委托给 `try_new_unstable`，并把规范化器/大小写 provider 一并喂进去（因为规则转写常依赖 NFD/NFKD 与大小写）：

[components/experimental/src/transliterate/transliterator/mod.rs:482-504](https://github.com/unicode-org/icu4x/blob/1569b93140c14c1261dd47f3e0e7bdc889280ddf/components/experimental/src/transliterate/transliterator/mod.rs#L482-L504)
```rust
impl Transliterator {
    /// Construct a [`Transliterator`] from the given [`Locale`].
    /// ```
    /// use icu::experimental::transliterate::Transliterator;
    /// // BCP-47-T ID for Bengali to Arabic transliteration
    /// let locale = "und-Arab-t-und-beng".parse().unwrap();
    /// let t = Transliterator::try_new(&locale).unwrap();
    /// let output = t.transliterate("অকার্যতানাযা".to_string());
    /// assert_eq!(output, "اكاريتانايا");
    /// ```
    #[cfg(feature = "compiled_data")]
    pub fn try_new(locale: &Locale) -> Result<Self, DataError> {
        Self::try_new_unstable(
            &crate::provider::Baked,
            &icu_normalizer::provider::Baked,
            &icu_casemap::provider::Baked,
            locale,
        )
    }
```

转写执行方法把 `String` 包成可变缓冲后交给编译后的规则器：

[components/experimental/src/transliterate/transliterator/mod.rs:808-815](https://github.com/unicode-org/icu4x/blob/1569b93140c14c1261dd47f3e0e7bdc889280ddf/components/experimental/src/transliterate/transliterator/mod.rs#L808-L815)
```rust
    pub fn transliterate(&self, input: String) -> String {
        let mut buffer = TransliteratorBuffer::from_string(input);
        let rep = Replaceable::new(&mut buffer);
        self.transliterator.get().transliterate(rep, &self.env);
        buffer.into_string()
    }
```

> 提示：`try_new_unstable` 需要**三类** provider（转写规则 `PT`、规范化器 `PN`、大小写 `PC`），这正说明转写是一个「组合」能力——它把 normalizer（u4-l3）和 casemap（u4-l4）当作内部子步骤来用。

#### 4.5.4 代码实践

**实践目标**：跑通一次转写，观察实验性 API 与稳定 API 在「输入/输出形态」上的差异。

**操作步骤**：

1. 开启 `icu` 的 `unstable`。
2. 编写（基于源码示例，孟加拉文 → 阿拉伯文，可编译可运行）：
   ```rust
   use icu::experimental::transliterate::Transliterator;
   use icu::locale::Locale;

   fn main() {
       // BCP-47-T ID for Bengali to Arabic transliteration
       let locale: Locale = "und-Arab-t-und-beng".parse().unwrap();
       let t = Transliterator::try_new(&locale).expect("transliterator should load");
       let output = t.transliterate("অকার্যতানাযা".to_string());
       println!("{output}");
       assert_eq!(output, "اكاريتانايا");
   }
   ```
3. `cargo run`，确认输出为阿拉伯文 `اكاريتانايا`。
4. **（选做，如可用）** 改成拉丁 → 西里尔方向，例如 `let locale: Locale = "ru-t-und-latn".parse().unwrap();`，对一段拉丁化俄文调用 `transliterate`，观察是否能转出西里尔字母。

**需要观察的现象**：步骤 3 输出与断言一致；步骤 4（拉丁→西里尔）能否成功取决于 compiled data 是否包含对应规则。

**预期结果**：步骤 3 输出 `اكاريتانايا`（与源码示例一致）。步骤 4 的可用性**待本地验证**——若数据缺失，`try_new` 会返回 `DataError`，这也是实验性 API 与稳定 API 的一个差异：你更可能遇到「数据未编译进二进制」的情况，需要用 u5 讲过的显式 provider 或 datagen 补数据。

**与稳定 API 的差异小结**：注意 `transliterate` 直接吃 `String`、吐 `String`（而非 `Writeable` 惰性对象），因为转写过程需要反复就地修改缓冲，难以做成零拷贝惰性输出——这是它和多数稳定格式化器（输出 `Writeable`）在形态上的明显不同。

#### 4.5.5 小练习与答案

**练习 1**：locale `"und-Arab-t-und-beng"` 里，源文字系统和目标文字系统分别是什么？

> **参考答案**：`-t-` 之后是源，即 `und-beng`（孟加拉文 Bengali）；`-t-` 之前是目标，即 `und-Arab`（阿拉伯文 Arabic）。整体含义：把孟加拉文转写成阿拉伯文。

**练习 2**：为什么 `Transliterator::try_new_unstable` 需要规范化器和大小写两个额外的 provider？

> **参考答案**：因为基于规则的转写在匹配/替换时经常需要先把文本规范化（NFD/NFKD，使组合字符与预组合字符等价）或改变大小写，这些是转写规则的内部子步骤。所以转写器内部会调用 normalizer（u4-l3）和 casemap（u4-l4），必须把它们的数据 provider 一并传入。

---

## 5. 综合实践

**任务**：写一个 `locale_info` 小工具，**只**用一个程序演示三种实验能力，并把它与「等价的稳定 API」做对照，体会 experimental 的特点。

要求：

1. 启用 `icu` 的 `unstable` feature 与 `fixed_decimal` 依赖。
2. 用 `displaynames` 把 `langid!("fr-CA")` 显示成英文名（用 #8135 后的 `LanguageIdentifierDisplayNameOptions`）。
3. 用 `relativetime` 在 `locale!("en")` 下格式化「5 天前」（`Decimal::from(-5)`，`long_day`）。
4. 用 `transliterate` 把一段孟加拉文转写成阿拉伯文。
5. 在每一步旁边，用一句注释写明「如果改用稳定 API，这里会是什么样」——例如第 2 步可注释「稳定侧没有等价的 displaynames，它正是 experimental 的典型候选；而 relativetime 的数字渲染底层复用的 `DecimalFormatter`（稳定）则是它俩的连接点」。

**验收**：程序能编译运行，三段输出都符合预期；你能向同伴解释：(a) 为什么这些能力在 experimental 而非稳定层；(b) #8135 让第 2 步的选项类型变成了什么、为什么。

> 提示：如果某一步因 compiled data 缺失而 `Err`，这正是练习 u5「数据提供器」的好契机——用 `BlobDataProvider` 或 `icu4x-datagen`（u5-l5）补上所需 locale 的数据再重试。

## 6. 本讲小结

- `icu::experimental`（`icu_experimental`）是**孵化器 crate**：pre-1.0（`0.6.0-dev`）、每个 ICU4X 版本都做大版本 bump、能力成熟后「毕业」迁到顶层稳定组件；通过 `icu` 的 `unstable` feature 启用。
- 它包含 8 个子模块（dimension / displaynames / duration / measure / personnames / relativetime / transliterate / units），本讲覆盖了最常用的四个。
- **#8135 的关键变更**：语言标识显示名 `LanguageIdentifierDisplayNameOwned` 不再复用通用 `DisplayNamesOptions`，改用专用、最小化的 `LanguageIdentifierDisplayNameOptions`（仅 `language_display: Option<LanguageDisplay>`），消除了对它无意义的 `style`/`fallback` 字段的误导。
- `relativetime` 是组合型组件：内部同时持有 `PluralRules` 与 `DecimalFormatter`，把「数值 + 复数 + 本地数字字形 + 相对时间 pattern」合成为 `in 5 seconds` 这类输出。
- `transliterate` 用 BCP-47-T 的 `-t-` 扩展描述「源→目标」文字系统，`try_new(&Locale)` + `transliterate(String) -> String`；它内部依赖 normalizer 与 casemap。
- 实验性 API 与稳定 API 的差异不仅在于「门控」，也体现在形态上（如 transliterate 直接返回 `String` 而非惰性 `Writeable`）和「数据可能未编译进二进制」的更高概率上。

## 7. 下一步学习建议

- **数据补全**：本讲多处出现「compiled data 可能缺失」的情况。建议接着学 **u5-l4（存储后端 baked/blob/fs）** 与 **u5-l5（icu4x-datagen 数据生成）**，学会为 experimental 组件按需生成并加载 locale 数据。
- **复用关系溯源**：`relativetime`/`dimension` 都依赖 `DecimalFormatter` + `PluralRules`。可回看 **u3-l3（十进制数字格式化）** 与 **u3-l4（复数规则）**，理解 experimental 组件如何站在稳定组件肩膀上。
- **追踪毕业动向**：`displaynames` 的设计仍在征求反馈（issue #7824 / #7825，见源码注释）。可以用 `git log -- components/experimental` 观察这些模块的演进，体会「实验 → 稳定」的真实过程。
- **进阶转写**：阅读 `transliterate/transliterator/` 下的规则编译（`compile/`）与 `TransliteratorBuilder`，理解如何用自定义规则与 `CustomTransliterator` 扩展转写能力。
