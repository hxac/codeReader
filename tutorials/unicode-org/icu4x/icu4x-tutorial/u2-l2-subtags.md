# 子标签体系：language / script / region / variant

> 适用讲义：`u2-l2`（承接 `u2-l1` Locale 与 LanguageIdentifier 数据模型）。
> 阅读本讲前，请先确认你已经知道 `Locale` 内嵌一个 `id: LanguageIdentifier`，且 `LanguageIdentifier` 由若干「字段」组成——本讲要拆开看的正是这些字段的类型本身。

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 BCP-47 语言标识符的四个「子标签」层级：language / script / region / variant，以及它们各自的取值规则。
- 会用 `try_from_str` / `.parse()` / `language!` 等宏分别构造与校验每一个子标签。
- 看懂 ICU4X 是如何用 `tinystr::TinyAsciiStr` 把这些子标签压成「定长、`Copy`、零堆分配」的小对象的，并理解这对嵌入式 / 小体积目标的意义。
- 会从一个带 Unicode 扩展（`-u-...`）的 `Locale` 中读出关键字（例如 `nu-latn` 数字系统）。

## 2. 前置知识

在进入源码之前，先用三段话建立直觉。

**什么是「子标签（subtag）」。** 我们在 `u2-l1` 见过 `"zh-Hant-TW"` 这样的 locale 字符串。它其实是用连字符 `-` 拼起来的若干小片段，每一个小片段就是一个「子标签」：

```
zh    - Hant - TW
│       │      └─ Region（地区）
│       └──────── Script（书写文字）
└──────────────── Language（语言）
```

BCP-47（以及它对应的 Unicode 扩展规范 UTS #35）规定了每个位置上的子标签**长度、字符种类、大小写**必须满足什么约束。ICU4X 把这四个位置分别建模成四个独立的 Rust 类型，这就是本讲的 `Language` / `Script` / `Region` / `Variant`。

**为什么要把它们做成独立类型。** 一个直接的好处是「错误前置」：非法输入（比如把 `"419"` 当成语言）在解析阶段就会被拒绝，而不是带着脏数据流到格式化器里。更深一层的好处是「体积」：由于每种子标签的取值范围都很小（语言只有 2–3 个 ASCII 字母），ICU4X 可以用一个**定长的小整数**来表示它，从而整个子标签是 `Copy` 的、不需要堆分配——这是 ICU4X 能跑在嵌入式/客户端上的重要前提（呼应 `u1-l1` 的「小而模块化」目标）。

**大小写规范化。** BCP-47 对每种子标签规定了「规范大小写」：语言小写、地区大写、文字首字母大写（Title Case）、变体小写。ICU4X 在**解析时就一次性把大小写规范化好**，之后所有操作都在规范形态上进行，序列化几乎是零成本。这一点在源码里会反复出现。

> 名词速查：
> - **BCP-47**：IETF「语言标签」最佳实践，定义了语言标签的语法骨架。
> - **UTS #35**：Unicode 的「Locale Data Markup Language」，在 BCP-47 上扩展了 `-u-`（Unicode 扩展）等用法。
> - **ISO 639 / ISO 15924 / ISO 3166-1 / UN M.49**：分别是语言、文字、国家/地区代码的标准来源。

## 3. 本讲源码地图

本讲涉及的关键文件集中在 `components/locale_core/src/subtags/` 与它的支撑层：

| 文件 | 作用 |
| --- | --- |
| [`subtags/mod.rs`](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/subtags/mod.rs) | 子标签模块的「门面」：文档说明、`mod` 声明、`pub use` 重导出，以及一个通用的 `Subtag` 类型。 |
| [`subtags/language.rs`](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/subtags/language.rs) | `Language`（语言子标签，2–3 字母，小写）。 |
| [`subtags/script.rs`](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/subtags/script.rs) | `Script`（书写文字，4 字母，Title Case）。 |
| [`subtags/region.rs`](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/subtags/region.rs) | `Region`（地区，2 字母大写或 3 数字）。 |
| [`subtags/variant.rs`](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/subtags/variant.rs) | `Variant`（变体，4–8 字符）。 |
| [`subtags/variants.rs`](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/subtags/variants.rs) | `Variants`：`Variant` 的有序、去重容器。 |
| [`helpers.rs`](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/helpers.rs) | `impl_tinystr_subtag!` 宏：四个子标签类型共享的「模板代码」生成器。 |
| [`langid.rs`](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/langid.rs) | `LanguageIdentifier` 结构体，把四个子标签组装在一起。 |
| [`extensions/unicode/mod.rs`](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/extensions/unicode/mod.rs) | Unicode 扩展 `-u-` 的入口，承接本讲的扩展简介与实践任务。 |

> 提示：你会发现 `language.rs` / `script.rs` / `region.rs` / `variant.rs` 这四个文件**每个都只有几十行**——真正的逻辑都集中在 `helpers.rs` 的那个宏里。这是 ICU4X 刻意的代码组织：用宏消灭四种类型之间重复的样板代码。

## 4. 核心概念与源码讲解

### 4.1 四大子标签类型：Language / Script / Region / Variant

#### 4.1.1 概念说明

ICU4X 把一个语言标识符的四个位置分别建模成四个独立的 newtype 类型。它们都对用户暴露在 `icu::locale::subtags` 命名空间下（通过元 crate `icu` 的 re-export，参见 `u1-l4`）：

| 类型 | 含义 | 长度 | 字符 | 规范大小写 | 标准来源 |
| --- | --- | --- | --- | --- | --- |
| `Language` | 语言 | 2–3 | ASCII 字母 | 小写 | ISO 639 |
| `Script` | 书写文字 | 4 | ASCII 字母 | Title Case（如 `Latn`） | ISO 15924 |
| `Region` | 国家/地区 | 2 或 3 | 2 字母 **或** 3 数字 | 字母大写（如 `US`） | ISO 3166-1 / UN M.49 |
| `Variant` | 变体 | 4–8 | 字母数字 | 小写 | UTS #35 |

这四个类型最终都被组装进 `LanguageIdentifier`。`langid.rs` 里这个结构体的字段就是它们的直接对应：

```rust
// components/locale_core/src/langid.rs
pub struct LanguageIdentifier {
    pub language: subtags::Language,          // 唯一非 Option：必填，空则等于 "und"
    pub script: Option<subtags::Script>,      // 可选
    pub region: Option<subtags::Region>,      // 可选
    pub variants: subtags::Variants,          // 0 或多个 Variant
}
```

详见 [langid.rs:88-97](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/langid.rs#L88-L97)，这段定义了上述四个字段（语言为必填、其余为可选）。

注意 `Language` 是唯一**必填**的字段；当语言缺失时，它取特殊值 `und`（unknown / 未确定），而不是 `Option`。这一点很重要：`und` 是整个回退链的最终落脚点（见 `u2-l4`）。

#### 4.1.2 核心流程

四个子标签的「生命周期」其实是一样的，可以用同一条流水线描述：

```
字符串输入 ──► try_from_utf8()
                  │
                  ├─ 1. 长度检查（是否落在 [len_start, len_end] 区间）
                  ├─ 2. 字符种类检查（validate：字母？数字？）
                  ├─ 3. 大小写规范化（normalize：转小写/大写/Title Case）
                  └─► 得到一个规范化的子标签对象（Copy、定长）
```

解析成功后，对象内部存的就是「已经规范化的字节」，之后无论是比较、序列化还是显示，都直接用这份数据，不再反复规整大小写。

四种类型唯一的差别，就是上面三步里的具体规则不同——而这些规则在源码里就是 `impl_tinystr_subtag!` 宏的几个参数。

#### 4.1.3 源码精读

**Language（语言）。** 看 [`language.rs:5-50`](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/subtags/language.rs#L5-L50)，整个 `Language` 类型就是一次宏调用。关键参数：

- `2..=3`：长度只能是 2 或 3（[language.rs:42](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/subtags/language.rs#L42)）。
- `s.is_ascii_alphabetic()`：必须是纯字母（[language.rs:44](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/subtags/language.rs#L44)）。
- `s.to_ascii_lowercase()`：规范化为小写（[language.rs:45](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/subtags/language.rs#L45)）。
- 错误类型是专门的 `InvalidLanguage`（[language.rs:47](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/subtags/language.rs#L47)），其它三种都用通用的 `InvalidSubtag`。

宏调用之后，文件给 `Language` 追加了它独有的常量与方法（[language.rs:52-61](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/subtags/language.rs#L52-L61)）：

```rust
impl Language {
    /// The unknown language "und".
    pub const UNKNOWN: Self = language!("und");

    pub const fn is_unknown(self) -> bool {
        matches!(self, Self::UNKNOWN)
    }
}
```

这就是「空语言 = `und`」在代码里的落点：`Language::UNKNOWN`（[language.rs:54](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/subtags/language.rs#L54)）。注意它序列化成字符串就是 `"und"`，再解析回来又变成空 `Language`——这两种形态等价。

**Script（书写文字）。** 看 [`script.rs:7-37`](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/subtags/script.rs#L7-L37)：长度恒为 4（[script.rs:29](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/subtags/script.rs#L29)），必须是字母，规范化用 `to_ascii_titlecase()`（[script.rs:32](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/subtags/script.rs#L32)），即首字母大写、其余小写，所以 `"arab"` 解析后是 `"Arab"`。`Script` 额外实现了 `From<Script> for Subtag`（[script.rs:39-43](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/subtags/script.rs#L39-L43)），说明它被当作一种「具体化的通用 Subtag」来处理（见 4.3）。

**Region（地区）。** 看 [`region.rs:5-48`](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/subtags/region.rs#L5-L48)：这是四种里规则最有趣的一种，因为它的长度决定字符种类——

```rust
// validate：长度为 2 则必须是字母，为 3 则必须是数字
if s.len() == 2 { s.is_ascii_alphabetic() } else { s.is_ascii_numeric() },
// normalize：字母形式转大写，数字形式保持原样
if s.len() == 2 { s.to_ascii_uppercase() } else { s },
```

对应 [region.rs:30-39](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/subtags/region.rs#L30-L39)。也就是说 `"us"` → `"US"`（ISO 3166-1 双字母国家码），而 `"001"` → `"001"`（UN M.49 三位数字「世界」码）两者都合法。`Region` 还有一个判断方法 `is_alphabetic`（[region.rs:60-62](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/subtags/region.rs#L60-L62)），靠「长度是否为 2」来区分这两种形态。

**Variant（变体）。** 看 [`variant.rs:5-35`](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/subtags/variant.rs#L5-L35)：长度 4–8（[variant.rs:25](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/subtags/variant.rs#L25)），有一条特殊规则——

```rust
s.is_ascii_alphanumeric() && (s.len() != 4 || s.all_bytes()[0].is_ascii_digit()),
```

对应 [variant.rs:27](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/subtags/variant.rs#L27)。含义是：字母数字都行，**但**如果长度恰好是 4，则首字符必须是**数字**。这是 BCP-47 用来在语法上区分 4 字符变体（如 `"1996"`）与 4 字符「未来语言/脚本」的规则。规范大小写统一为小写（[variant.rs:28](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/subtags/variant.rs#L28)）。

> 容器 `Variants`。变体可以有多个，所以 `LanguageIdentifier.variants` 的类型不是单个 `Variant` 而是 `Variants`——一个**已排序、已去重**的容器（[variants.rs:30](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/subtags/variants.rs#L30)）。它内部用 `ShortBoxSlice<Variant>`（一种「1 个元素放栈上、多个才装箱」的小集合，呼应 `u1-l1` 的小体积目标）。`push` 方法用二分查找维持有序并去重，详见 [variants.rs:140-148](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/subtags/variants.rs#L140-L148)。

#### 4.1.4 代码实践

> **实践目标**：亲手解析四种子标签，观察它们各自的「规范化大小写」效果。

**操作步骤**：在你的 `u1-l3` 已建好的 ICU4X 项目里新建一个 binary，加入下面这段示例代码（**示例代码**，非项目原有）：

```rust
use icu::locale::subtags::{Language, Region, Script, Variant};

fn main() {
    // 注意：故意用「不规范」的大小写输入
    let lang: Language   = "eN".parse().unwrap();
    let script: Script   = "aRaB".parse().unwrap();
    let region: Region   = "us".parse().unwrap();
    let region_num: Region = "001".parse().unwrap();
    let variant: Variant = "MacOS".parse().unwrap();

    println!("language = {}", lang);          // 期望：en
    println!("script   = {}", script);        // 期望：Arab
    println!("region   = {}", region);        // 期望：US
    println!("region   = {}", region_num);    // 期望：001
    println!("variant  = {}", variant);       // 期望：macos
}
```

**需要观察的现象**：所有输出都被规范化成了「规范大小写」——语言小写、文字 Title Case、地区字母大写、变体全小写；数字形式地区原样保留。

**预期结果**：
```
language = en
script   = Arab
region   = US
region   = 001
variant  = macos
```
若结果与此不符（待本地验证），请检查你的 `icu` 依赖是否正常启用。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `LanguageIdentifier.language` 的类型是 `Language` 而不是 `Option<Language>`？空语言如何表达？
**答案**：因为语言字段是必填的，空值用一个明确的「规范哨兵」`Language::UNKNOWN`（即 `"und"`）表达，而不是 `None`。这样省去了 `Option` 的额外 tag 开销，也让「未确定语言」成为回退链的一个合法落脚点。

**练习 2**：字符串 `"419"` 能否被解析成 `Region`？能否被解析成 `Language`？
**答案**：能解析成 `Region`（三位数字符合 UN M.49）。不能解析成 `Language`，因为语言必须是 2–3 个 ASCII **字母**，`419` 是数字，会在 `is_ascii_alphabetic()` 校验处失败。

---

### 4.2 合法取值与校验：解析、规范化、比较

#### 4.2.1 概念说明

上一节我们看到了「规则」，这一节我们看「规则在代码里是如何被执行的」。所有子标签都共享同一套校验/规范化/比较 API，理解了其中一种，就理解了全部。

需要区分三个容易混淆的概念：

- **well-formed（合式）**：语法正确，满足长度/字符种类/大小写的硬约束。**解析只保证到这一层。**
- **valid（有效）**：合式，且子标签确实在标准里登记过（如 `en` 是真英语）。
- **canonical（规范）**：有效，且不含已废弃的代码（如旧的 `iw` 应替换成 `he`）。

ICU4X 的子标签解析**只做 well-formed 检查 + 大小写规范化**；valid / canonical 的进一步处理留给 `LocaleCanonicalizer`（见 `u2-l3`）。这一点和 `u2-l1` 强调的「解析只负责 well-formed」完全一致。

#### 4.2.2 核心流程

以解析为例，统一流程是：

```
try_from_str(s)
   └─► try_from_utf8(bytes)
            ├─ 长度落在 [len_start, len_end]？ 否 ──► Err(Invalid*)
            └─ TinyAsciiStr::try_from_utf8(bytes)
                     └─ 校验 validate 通过？ ──► 应用 normalize ──► Ok(Self(...))
                                              否 ──► Err(Invalid*)
```

除了「构造」，还有几类常用操作：`as_str()` 借出字符串视图、`strict_cmp` 做字节序比较（可用于二分查找）、`normalizing_eq` 做忽略大小写的相等比较。

#### 4.2.3 源码精读

这些共享 API 全部由 `helpers.rs` 的宏生成。看 `try_from_utf8`：

```rust
// components/locale_core/src/helpers.rs （宏展开后的等价形态）
pub const fn try_from_utf8(code_units: &[u8]) -> Result<Self, ParseError> {
    if code_units.len() < $len_start || code_units.len() > $len_end {
        return Err(ParseError::$error);                       // 长度不过
    }
    match tinystr::TinyAsciiStr::try_from_utf8(code_units) {
        Ok(s) if $validate => Ok(Self($normalize)),            // 校验 + 规范化
        _ => Err(ParseError::$error),
    }
}
```

对应 [helpers.rs:44-55](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/helpers.rs#L44-L55)，长度检查在 [helpers.rs:47-49](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/helpers.rs#L47-L49)，校验+规范化在 [helpers.rs:51-54](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/helpers.rs#L51-L54)。注意这函数标了 `const`，所以它能在 `const` 上下文里调用——这正是编译期宏（`language!("en")` 等）得以工作的根基。

宏还为每种类型生成了 `FromStr`（[helpers.rs:128-135](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/helpers.rs#L128-L135)），所以 `"en".parse::<Language>()` 这种惯用写法可直接使用。比较方面，`strict_cmp` 与 `normalizing_eq` 见 [helpers.rs:104-125](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/helpers.rs#L104-L125)：

- `strict_cmp(other: &[u8])`：拿子标签的规范字节串和 `other` 做**字节序**比较，返回 `Ordering`，适合二分查找。
- `normalizing_eq(other: &str)`：先把 `other` 忽略大小写地比较，相当于「先解析再结构比较」的快捷版。

> 提示：因为子标签内部存的就是规范形态，`strict_cmp` 不需要再做任何规整，是真正的零成本比较。

#### 4.2.4 代码实践

> **实践目标**：用非法输入触发解析错误，观察「合式检查」的边界；再对比 `strict_cmp` 与 `normalizing_eq`。

**操作步骤**（**示例代码**）：

```rust
use icu::locale::subtags::{Language, Region};

fn main() {
    // 1. 非法输入应当解析失败
    assert!(Language::try_from_str("419").is_err());   // 数字不是字母
    assert!(Language::try_from_str("german").is_err()); // 太长（>3）
    assert!(Region::try_from_str("FRA").is_err());      // 3 位必须是数字，不能是字母

    // 2. 比较：同一规范值的不同写法
    let us: Region = "US".parse().unwrap();
    assert!(us.normalizing_eq("us"));   // 忽略大小写后相等
    assert_eq!(us.strict_cmp(b"US"), core::cmp::Ordering::Equal);
    assert!(us.strict_cmp(b"CN").is_ne()); // 字节序不等
    println!("all assertions passed");
}
```

**需要观察的现象**：三条 `is_err()` 断言全部成立，说明校验确实在长度/字符种类层就拦下了非法输入。

**预期结果**：打印 `all assertions passed`。若想看具体错误变体，可把 `assert!` 换成 `println!("{:?}", Language::try_from_str("german"))` 打印出 `Err(InvalidLanguage)`。

#### 4.2.5 小练习与答案

**练习 1**：`"Latn"` 能否被解析成 `Script`？`"Latin"` 呢？为什么？
**答案**：`"Latn"` 可以（4 字母、Title Case 规范）。`"Latin"` 不可以——虽然它也是 4 字母，但它是 ISO 15924 的**英文名**而非 4 字母代码；不过这里它失败的直接原因是它本身就是合法的 4 字母形式……更准确的反例见源码注释：宏给出的坏例子是 `"Latin"`，因为它虽合式却**不是登记过的 Script 代码**；严格来说解析器只做合式检查，`"Latin"` 实际会通过合式检查。要严格区分请用 `LocaleCanonicalizer`。请以本地 `try_from_str("Latin").is_ok()` 的实际结果为准（待本地验证）。

**练习 2**：`strict_cmp` 和 `normalizing_eq` 分别适合什么场景？
**答案**：`strict_cmp` 返回全序 `Ordering`，适合在排序数组里做二分查找（如 `ZeroMap` 的键）。`normalizing_eq` 返回布尔，适合「这个用户输入（可能大小写不规范）是不是等于某个已知子标签」的快速判断。

---

### 4.3 紧凑存储实现：`impl_tinystr_subtag` 宏与 `TinyAsciiStr`

#### 4.3.1 概念说明

本节回答一个关键问题：**这些子标签到底「小而高效」在哪里？**

答案是一个叫 [`tinystr`](https://github.com/unicode-org/rust#tinystr) 的外部 crate 提供的类型 `TinyAsciiStr<N>`。它把一段**至多 N 个字节的 ASCII 字符串**直接塞进一个定长整数里（不分配堆、不存指针）。于是：

- 一个 `Language` 只是 3 字节（外加对齐），整个值是 `Copy` 的，传参/赋值就是几次字节拷贝。
- 一个 `Script` 是 4 字节，`Region` 是 3 字节，`Variant` 是 8 字节。
- 一个完整的 `LanguageIdentifier`（语言+文字+地区+变体容器）因此可以非常紧凑地放进数据管道（这是 `u5` 数据机制能高效分发 locale 数据的物理基础之一）。

而 `impl_tinystr_subtag!` 宏，就是把这四个类型「共享的那一大坨实现」一次性生成出来的模板。它消除了四种类型之间关于解析、显示、序列化、ULE、`Bake` 等的重复代码。

#### 4.3.2 核心流程

宏为每个子标签类型生成的「能力清单」大致是：

```
struct $name(TinyAsciiStr<len_end>);      // transparent newtype，定长
   ├─ 构造：try_from_str / try_from_utf8 / try_from_raw / from_raw_unchecked
   ├─ 拆装：into_raw / try_from_raw           （便于零拷贝序列化）
   ├─ 显示：as_str / Writeable / Display      （显示零分配）
   ├─ 解析：FromStr                           （.parse()）
   ├─ 序列化：Serialize / Deserialize          （serde feature）
   ├─ 零拷贝：ULE / AsULE / ZeroMapKV          （zerovec feature，见 u6-l1）
   └─ 编译期烘焙：Bake                          （databake feature，见 u6-l4）
```

其中两条线索尤其值得注意：`into_raw`/`try_from_raw` 把子标签变成定长字节数组，再配合 `ULE` 实现，使子标签可以**零拷贝**地躺在 `ZeroVec` 里（`u6-l1`）；`Bake` 则让它能被**烘焙成 Rust 源码**（`u6-l4`），从而编进 compiled data。本讲只需建立「这个宏是四种类型的共同地基」的认知即可。

#### 4.3.3 源码精读

宏定义本身在 [helpers.rs:5-333](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/helpers.rs#L5-L333)。两个最关键的细节：

**第一，类型本身是 `#[repr(transparent)]` 的 newtype，且派生了 `Copy`：**

```rust
#[derive(Debug, PartialEq, Eq, Clone, Hash, PartialOrd, Ord, Copy)]
#[repr(transparent)]
pub struct $name(tinystr::TinyAsciiStr<$len_end>);
```

对应 [helpers.rs:21-24](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/helpers.rs#L21-L24)。`repr(transparent)` 保证 newtype 与底层 `TinyAsciiStr` 在内存布局上完全一致——没有额外 tag，所以它才能安全地实现下面的 `ULE`。

**第二，它实现了 `zerovec::ule::ULE`，从而可以零拷贝地存进 `ZeroVec`：**

```rust
unsafe impl zerovec::ule::ULE for $name {
    fn validate_bytes(bytes: &[u8]) -> Result<(), UleError> {
        // 按 size_of::<Self>() 切块，逐块用 try_from_raw 校验
        ...
    }
}
```

对应 [helpers.rs:289-306](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/helpers.rs#L289-L306)。这段代码意味着「一段连续字节可以被直接 re-interpret 成一组子标签，无需逐个反序列化分配」。具体原理留到 `u6-l1`（zerovec）详解，本讲你只要知道「子标签天生具备零拷贝能力」。

> 旁注：`mod.rs` 里还有一个通用类型 `Subtag`（[mod.rs:64-92](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/subtags/mod.rs#L64-L92)），它用同一个宏生成、长度放宽到 2–8。它是给扩展（extensions）里那些「不那么固定」的子标签用的更通用容器；`Script` 还能 `From` 转成它（见 4.1.3 的 `script.rs`）。本讲不深入。

#### 4.3.4 代码实践

> **实践目标**：用 `size_of` 直观感受「子标签有多小」，验证它们确实没有隐藏的堆分配。

**操作步骤**（**示例代码**，用 `std::mem::size_of` 探测布局）：

```rust
use icu::locale::subtags::{Language, Region, Script, Variant};
use std::mem::size_of;

fn main() {
    println!("Language = {} bytes", size_of::<Language>()); // 期望：4（3 字符 + 对齐）
    println!("Script   = {} bytes", size_of::<Script>());   // 期望：4
    println!("Region   = {} bytes", size_of::<Region>());   // 期望：4
    println!("Variant  = {} bytes", size_of::<Variant>());  // 期望：8
}
```

**需要观察的现象**：四种类型都只有个位数字节，且 `Language/Script/Region` 因对齐通常都是 4 字节、`Variant` 是 8 字节。它们都是 `Copy`，传递时不会有指针/堆介入。

**预期结果**：如上注释所示（具体对齐值依赖平台，待本地验证，但都在个位数字节级别）。可与 `size_of::<String>()`（在 64 位下通常 24 字节且指向堆）对比，体会「紧凑存储」的差距。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Language` 容量是 3 字节（`TinyAsciiStr<3>`），但 `size_of::<Language>()` 可能显示 4？
**答案**：因为 `TinyAsciiStr<3>` 内部用一个 3 字节的 ASCII 数组加一个「长度」记录，再受结构体对齐影响，整体大小会被向上取整到 4 字节。容量 3 是「最多存 3 个字符」，4 是「实际占用的内存字节数」。

**练习 2**：四种子标签类型能实现 `ULE`（零拷贝布局）的根本前提是什么？
**答案**：`#[repr(transparent)]` 使 newtype 与底层 `TinyAsciiStr` 布局一致、定长、无 padding 残留、字节相等即语义相等。满足了这些，才能安全地把一段连续字节直接当成子标签数组来读（详见 `u6-l1`）。

---

### 4.4 extensions 扩展简介：在 BCP-47 之外携带偏好

#### 4.4.1 概念说明

`Language` / `Script` / `Region` / `Variant` 描述的是「你说什么语言、在哪里」。但国际化还需要另一类信息：「你希望数字用什么字形、日历用哪种、小时制是 12 还是 24」等**用户偏好**。BCP-47 用「扩展」机制来携带它们，其中最常用的是 **Unicode 扩展**，写作 `-u-` 前缀。

例如 `ar-EG-u-nu-latn` 的含义是：

```
ar     -  EG  - u  - nu-latn
│         │      │     └─ Unicode 扩展：关键字 nu（numbering system）= latn
│         │      └─────── 扩展标识符（singleton）'u'
│         └────────────── Region
└──────────────────────── Language
```

在 `u2-l1` 我们说过：`Locale` 比 `LanguageIdentifier` 多出来的部分，主要就是这个 Unicode 扩展（以及 transform/private 等其它扩展）。本节作为「扩展简介」，只带你读到「如何从一个 `Locale` 里把 `nu-latn` 取出来」——这也是本讲综合实践的核心。

> 关键字速查（不需要记，知道有这些即可）：`nu` 数字系统、`hc` 小时制（h12/h24）、`ca` 日历（gregory/buddhist…）、`co` 排序、`cf` 货币格式。它们都是 UTS #35 定义的两位「key」。

#### 4.4.2 核心流程

从 `Locale` 读取一个 Unicode 扩展关键字的标准动作是：

```
loc.extensions.unicode.keywords.get(&key!("nu"))
   │            │        │         │      └─ 编译期构造 2 字母 Key
   │            │        │         └──────── map 式查询，返回 Option<&Value>
   │            │        └────────────────── Keywords 容器
   │            └─────────────────────────── Unicode 扩展对象
   └──────────────────────────────────────── Locale.extensions
```

注意这里的 `Key`（扩展关键字）和 `Value`（扩展值）本身也是用 `impl_tinystr_subtag!` 宏生成的子标签类型——这正是 4.3 那套机制的复用：`Key` 是 2 字符、`Value` 是 3–8 字符的定长小对象。例如 [`key.rs:5-32`](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/extensions/unicode/key.rs#L5-L32) 定义了 `Key` 必须是「首位字母数字、次位字母」的 2 字符串。

#### 4.4.3 源码精读

`Unicode` 扩展对象的定义在 [extensions/unicode/mod.rs:88-96](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/extensions/unicode/mod.rs#L88-L96)：

```rust
pub struct Unicode {
    pub keywords: Keywords,    // key-value 对，如 nu=latn, hc=h12
    pub attributes: Attributes,// 独立的布尔式属性（较少用）
}
```

它挂在 `Locale.extensions.unicode` 上。官方示例（[extensions/unicode/mod.rs:17-28](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/extensions/unicode/mod.rs#L17-L28)）正好演示了读取方式：

```rust
let loc: Locale = "en-US-u-foobar-hc-h12".parse().expect("Parsing failed.");
assert_eq!(
    loc.extensions.unicode.keywords.get(&key!("hc")),
    Some(&value!("h12"))
);
```

也就是：解析整个字符串得到 `Locale`，再从 `extensions.unicode.keywords` 里用 `key!` 宏查表。`get` 返回 `Option<&Value>`，用 `value!` 宏构造期望值做相等比较。

> 提示：除了 Unicode 扩展（`-u-`），`extensions/` 下还有 transform（`-t-`）、private（`-x-`）、other 等扩展，结构类似但用途不同（transform 用于语言转写标记，private 用于私有约定）。本讲不展开，后续用到再查 `extensions/` 目录。

#### 4.4.4 代码实践

> **实践目标**：从 `ar-EG-u-nu-latn` 中分别取语言、地区，并读出 `nu`（数字系统）关键字。

**操作步骤**（**示例代码**，这正是本讲规格里要求的综合实践，这里先给最小版本，第 5 节会扩展）：

```rust
use icu::locale::extensions::unicode::{key, value};
use icu::locale::Locale;

fn main() {
    let loc: Locale = "ar-EG-u-nu-latn".parse().expect("解析失败");

    // 1) 语言子标签
    println!("language = {}", loc.id.language); // ar
    // 2) 地区子标签
    println!("region   = {}", loc.id.region.unwrap()); // EG
    // 3) 读取 Unicode 扩展里的 numbering system（nu = latn）
    let nu = loc.extensions.unicode.keywords.get(&key!("nu"));
    println!("nu       = {:?}", nu); // Some(latn)
    assert_eq!(nu, Some(&value!("latn")));
}
```

**需要观察的现象**：`loc.id.language` 直接是 `Language`（`ar`），`loc.id.region` 是 `Option<Region>`（`Some(EG)`），而 `nu` 关键字通过扩展的 `keywords` map 取出为 `Some(Value("latn"))`。注意字段访问路径：基础子标签走 `loc.id.*`，扩展走 `loc.extensions.*`——这正好对应 `u2-l1` 讲过的「`Locale` = `id: LanguageIdentifier` + extensions」结构。

**预期结果**：
```
language = ar
region   = EG
nu       = Some(latn)
```
（待本地验证）

#### 4.4.5 小练习与答案

**练习 1**：为什么「语言/地区」从 `loc.id` 取，而「数字系统 nu」从 `loc.extensions` 取？
**答案**：前者属于 `LanguageIdentifier` 的四大基础子标签（language/script/region/variant），存在 `loc.id` 里；后者是 Unicode 扩展关键字，属于 `Locale` 比 `LanguageIdentifier` 多出来的 `extensions` 部分，所以走 `loc.extensions.unicode.keywords`。这正是 `Locale` 与 `LanguageIdentifier` 的核心区别（见 `u2-l1`）。

**练习 2**：把 `ar-EG-u-nu-latn` 里的 `nu-latn` 换成 `nu-arab`，`nu` 的取值会变成什么？它对实际格式化有什么影响？
**答案**：`nu` 会变成 `Some(arab)`，表示使用阿拉伯-印度数字字形。在 `icu_decimal`（见 `u3-l3`）里，这会让数字格式化输出阿拉伯-印度数字而不是拉丁数字。可见扩展是「影响组件行为」的偏好开关。

---

## 5. 综合实践

把本讲的四大子标签 + 扩展串起来，完成下面这个**端到端小任务**。

**任务**：写一个程序，接收一个 locale 字符串，把它**逐字段拆解**并打印成一张「体检表」，要求覆盖 language / script / region / variant 四类基础子标签，并在存在 Unicode 扩展时额外打印其中的关键字。

**参考实现**（**示例代码**）：

```rust
use icu::locale::extensions::unicode::key;
use icu::locale::Locale;

fn diagnose(input: &str) {
    let loc: Locale = input.parse().expect("解析失败");
    let id = &loc.id;

    println!("输入：{input}");
    println!("├─ language : {} {}", id.language, if id.language.is_unknown() { "(未知/und)" } else { "" });
    println!("├─ script   : {:?}", id.script);            // Option<Script>
    println!("├─ region   : {:?}", id.region);            // Option<Region>
    println!("├─ variants : {:?}", id.variants.first());  // Option<&Variant>

    for (k, v) in loc.extensions.unicode.keywords.iter() {
        println!("└─ u-ext    : {k} = {v}");
    }
}

fn main() {
    diagnose("zh-Hant-TW");
    diagnose("ar-EG-u-nu-latn");
    diagnose("en-US-u-hc-h12-ca-buddhist");
}
```

> 说明：`Keywords::iter()`（[keywords.rs:374](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/extensions/unicode/keywords.rs#L374)）返回 `(&Key, &Value)` 迭代器，是遍历扩展关键字的标准方式。若不想遍历，也可用 `key!("nu")` / `key!("hc")` 等已知键逐个 `get`。

**需要观察的现象**：
- `zh-Hant-TW`：language=`zh`、script=`Hant`、region=`TW`、无变体、无扩展。
- `ar-EG-u-nu-latn`：language=`ar`、region=`EG`、扩展 `nu=latn`。
- `en-US-u-hc-h12-ca-buddhist`：language=`en`、region=`US`、扩展 `hc=h12` 与 `ca=buddhist` 两条。

**预期结果**：每条输入都打印出结构化的字段表，且大小写已规范化（`Hant` 而非 `hant`、`TW` 而非 `tw`）。完成后，请尝试给 `diagnose` 喂一个非法输入（如 `"419-US"`）并观察它在解析阶段就报错——体会「合式检查前置」的效果。

## 6. 本讲小结

- 一个 locale 的「基础部分」由四类子标签组成：`Language`（必填，空为 `und`）、`Script`（可选，4 字母 Title Case）、`Region`（可选，2 字母大写或 3 数字）、`Variant`（0 或多个，4–8 字符小写）。
- 这四种类型各自只是 `impl_tinystr_subtag!` 宏的一次调用，真正的解析/校验/显示/序列化逻辑都集中在 `helpers.rs` 的宏里——这是 ICU4X 用宏消除样板代码的典型做法。
- 解析只做 **well-formed 检查 + 大小写规范化**：长度、字符种类、规范大小写；合法性/规范化的进一步处理留给 `u2-l3` 的 `LocaleCanonicalizer`。
- 每个子标签都是 `#[repr(transparent)]` 的 `Copy` newtype，底层是定长 `TinyAsciiStr`，**无堆分配、个位数字节**——这是 ICU4X 小体积、嵌入式友好的物理基础之一，也使它们天然支持 `ULE` 零拷贝（`u6-l1`）和 `Bake` 编译期烘焙（`u6-l4`）。
- `Variant` 有「4 字符首字符必须为数字」的特殊规则；`Region` 是唯一「长度决定字符种类（2 字母 vs 3 数字）」的类型。
- 在四类基础子标签之外，`Locale` 还携带 **Unicode 扩展**（`-u-`），通过 `loc.extensions.unicode.keywords.get(&key!(...))` 读取用户偏好（如数字系统 `nu`、小时制 `hc`、日历 `ca`）。

## 7. 下一步学习建议

- **`u2-l3` Locale 解析、规范化与 likely subtags**：本讲只做到 well-formed，下一讲带你用 `LocaleCanonicalizer` 把废弃标签（如 `iw → he`）替换掉、做真正的 canonical，并用 `LocaleExpander` 做 likely subtags 的最大化/最小化。
- **`u2-l4` Locale 回退链与文本方向性**：理解为什么 `und` 是回退链的最终落脚点，以及 `Region`/`Script` 如何参与回退决策。
- **横向联系**：如果你急于看「子标签的紧凑存储如何被组件消费」，可以跳到 `u3-l3`（`icu_decimal`）看 `nu` 扩展如何改变数字字形；或到 `u6-l1`（zerovec）看子标签的 `ULE` 实现如何支撑零拷贝数据。
- **源码延伸阅读**：`components/locale_core/src/parser/`（解析器如何把整串拆成子标签）、`components/locale_core/src/extensions/transform/` 与 `private/`（另外两类扩展）。
