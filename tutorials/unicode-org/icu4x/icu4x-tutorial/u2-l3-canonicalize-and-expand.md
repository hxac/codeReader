# Locale 解析、规范化与 likely subtags

## 1. 本讲目标

上一讲（u2-l1）我们已经知道：一个 `Locale` 由 `LanguageIdentifier`（language / script / region / variants）加上 Unicode 扩展组成，可以用 `locale!` 宏或 `.parse()` 构造。但当时只强调了一句关键结论——**「解析只保证 well-formed 与大小写规范化，废弃标签替换等 canonicalize 留给 `LocaleCanonicalizer`」**。本讲就把这句话彻底讲透。

学完本讲，你应当能够：

1. 说清 Unicode 对语言标识符定义的三层合规级别（well-formed / valid / canonical），以及解析器只做到哪一层。
2. 跟着源码画出 Locale 从字符串到结构的完整解析流程，并解释大小写规范化发生在哪一步。
3. 理解 `LocaleCanonicalizer` 如何实现 UTS #35 Annex C 的规范化算法，知道它为何要用「不动点循环」。
4. 理解 likely subtags 的 maximize（补全）与 minimize（精简）算法，以及 `new_common` 与 `new_extended` 的数据覆盖差异。

## 2. 前置知识

- **BCP-47**：互联网语言标签的标准格式（如 `zh-Hant-TW`、`es-419`）。本讲假定你已从 u2-l1 / u2-l2 了解 language / script / region / variant 四类子标签的含义与长度规则。
- **三层合规级别**：这是本讲最重要的概念，下文 4.1 会结合源码文档详细展开。简单说，一个字符串可以是「语法正确」「用了已注册的合法码」「且没有废弃码」三个递进层次。
- **UTS #35**：Unicode 技术标准 #35（LDML），其中 [Annex C: LocaleId Canonicalization](https://unicode.org/reports/tr35/#LocaleId_Canonicalization) 定义了规范化算法，[Likely Subtags](https://www.unicode.org/reports/tr35/#Likely_Subtags) 定义了最大化/最小化算法。本讲的 `LocaleCanonicalizer` 和 `LocaleExpander` 就是这两段算法的 Rust 实现。
- **DataMarker / DataProvider**：规范化与 likely subtags 都需要查表（别名表、likely subtags 表），这些表以 compiled data 的形式编译进二进制。如果你对「数据从哪来」还不清楚，可以先记住「构造函数 `new_common()` / `new_extended()` 会自动加载这些表」，细节留到第 5 单元（数据提供器）再讲。
- **`TransformResult`**：本讲两类操作都通过它告诉你「到底有没有改」——`Modified`（改了）或 `Unmodified`（没改）。这是一个稳定枚举。

## 3. 本讲源码地图

本讲涉及两个 crate：解析器在 `icu_locale_core`（无数据依赖的核心），规范化器与扩展器在 `icu_locale`（带 CLDR 数据）。

| 文件 | 作用 |
| --- | --- |
| `components/locale_core/src/parser/mod.rs` | 解析器的底层工具：按 `-` 切分字节的 `SubtagIterator`。 |
| `components/locale_core/src/parser/langid.rs` | `LanguageIdentifier` 的解析主逻辑：位置状态机。 |
| `components/locale_core/src/parser/locale.rs` | `Locale` 的解析入口：解析完 langid 后再解析扩展。 |
| `components/locale_core/src/parser/errors.rs` | `ParseError` 错误枚举。 |
| `components/locale_core/src/langid.rs` | `LanguageIdentifier` 类型定义、`try_from_str` 与三层合规级别的权威文档。 |
| `components/locale_core/src/subtags/language.rs` | `Language` 子标签定义，能看到大小写规范化（`to_ascii_lowercase`）发生在哪。 |
| `components/locale/src/lib.rs` | `icu_locale` 的总入口，导出 `LocaleCanonicalizer` / `LocaleExpander` 并定义 `TransformResult`。 |
| `components/locale/src/canonicalizer.rs` | **核心**：`LocaleCanonicalizer` 与 UTS #35 规范化算法。 |
| `components/locale/src/expander.rs` | **核心**：`LocaleExpander` 与 maximize / minimize 算法。 |
| `components/locale/src/provider.rs` | 规范化与 likely subtags 的数据结构（`Aliases`、`LikelySubtagsForLanguage` 等）。 |

## 4. 核心概念与源码讲解

### 4.1 Locale 解析器：从字符串到结构

#### 4.1.1 概念说明

Unicode 对语言标识符定义了**三层递进的合规级别**，理解它们是本讲的地基。`icu_locale_core` 的官方文档写得非常清楚：

> - *well-formed* —— 语法正确（syntactically correct）
> - *valid* —— well-formed 且只用了已注册的 language / region / script / variant 子标签
> - *canonical* —— valid 且不含废弃码或废弃结构

关键结论：**ICU4X 的解析器只负责把一个 well-formed 的字符串变成结构，并顺手做大小写规范化；它不做 validity 校验，也不做废弃码替换。** 后两件事分别留给数据查找和 `LocaleCanonicalizer`。

为什么这样切分？因为 validity 和 canonical 都依赖 CLDR 数据表（「哪些码已注册」「哪个废弃码要替换成什么」），而解析器位于无数据依赖的 `icu_locale_core`，必须保持轻量、可在 `const` 上下文使用。这种「**语法层在 core，语义层在有数据的 crate**」的分层，是 ICU4X 反复出现的架构思想。

#### 4.1.2 核心流程

解析一条 locale 字符串的流程是：

```
"zh-Hant-TW-u-nu-latn"
   │
   ① SubtagIterator 按 '-' 切分成字节片段序列
        → ["zh", "Hant", "TW", "u", "nu", "latn"]
   │
   ② parse_language_identifier_from_iter 用「位置状态机」依次尝试
        位置 Script → 试解析为 Script，否则 Region，否则 Variant
        每个子标签的构造函数负责自己的语法校验 + 大小写规范化
        → language=zh, script=Hant, region=TW
   │
   ③ 若 mode==Locale 且迭代器还剩内容 → 解析 extensions
        单字符子标签 "u" 触发 Unicode 扩展解析
        → keywords: nu=latn
   │
   ④ 组装 Locale { id: LanguageIdentifier{...}, extensions }
```

两个要点：

- **位置状态机**：解析器按 BCP-47 的固定字段顺序（language → script → region → variants）推进，用 `ParserPosition` 记录「下一个子标签允许是什么」。例如一旦读到 region，position 就前进到 `Variant`，此后再出现 script 形态的子标签就会报错。
- **大小写规范化在子标签构造函数里**：language 转小写、script 转 Title Case、region 转大写。所以 `"eN-latn-Us"` 解析出来就是 `en-Latn-US`。这一步是解析的一部分，不需要 canonicalizer。

#### 4.1.3 源码精读

**解析入口**：`Locale::try_from_utf8` 直接转发给 `parse_locale`。

[components/locale_core/src/locale.rs:163-165](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/locale.rs#L163-L165) —— `Locale` 的解析入口，调用 `parse_locale`。

[components/locale_core/src/parser/locale.rs:14-24](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/parser/locale.rs#L14-L24) —— 先用 `ParserMode::Locale` 解析出 `LanguageIdentifier`，若迭代器还有剩余（`iter.peek().is_some()`）再解析扩展，否则用默认空扩展。

**字节切分**：`SubtagIterator` 用 `skip_before_separator` 按第一个 `-` 切片，是 `const fn`（可在编译期求值，服务于 `locale!` 宏）。

[components/locale_core/src/parser/mod.rs:15-31](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/parser/mod.rs#L15-L31) —— `skip_before_separator`：扫描到 `-` 或末尾为止，返回前缀。注意它对 `"en-"` 会返回空串，从而允许迭代器暴露出空子标签（用于报错）。

[components/locale_core/src/parser/mod.rs:45-50](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/parser/mod.rs#L45-L50) —— `SubtagIterator` 结构：保存 `remaining`（剩余字节）和 `current`（当前前缀），并维护「current 必是 remaining 的前缀」这一安全不变量。

**位置状态机**：解析主循环。

[components/locale_core/src/parser/langid.rs:17-29](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/parser/langid.rs#L17-L29) —— `ParserMode`（区分只解析 LanguageIdentifier 还是完整 Locale）与 `ParserPosition`（Script / Region / Variant 三个推进阶段）。

[components/locale_core/src/parser/langid.rs:32-104](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/parser/langid.rs#L32-L104) —— 解析主逻辑。第 40-44 行先取出 language；第 48-51 行在 Locale 模式下遇到长度为 1 的子标签（如 `"u"`）就 `break`，把控制权交给扩展解析器；第 53-94 行是按 `ParserPosition` 逐位置尝试 Script/Region/Variant 的状态机；第 61-63、75-77、85-89 行用 `binary_search` + `insert` 保证 variants **有序且去重**。

**大小写规范化**：藏在子标签的构造宏里。

[components/locale_core/src/subtags/language.rs:5-50](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/subtags/language.rs#L5-L50) —— `impl_tinystr_subtag!` 宏定义 `Language`。注意第 44 行 `s.to_ascii_lowercase()`：解析时会把输入转小写；第 43 行 `s.is_ascii_alphabetic()` 是语法校验；第 42 行 `2..=3` 是长度约束。Script / Region / Variant 各自的宏调用里也有对应的 Title Case / 大写 / 校验逻辑。**这就是「解析即大小写规范化」的物理实现位置。**

**错误类型**：

[components/locale_core/src/parser/errors.rs:12-67](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale_core/src/parser/errors.rs#L12-L67) —— `ParseError` 只有四个变体：`InvalidLanguage`（语言子标签非法）、`InvalidSubtag`（script/region/variant 非法，或重复 variant）、`InvalidExtension`（扩展子标签非法）、`DuplicatedExtension`（如 `und-u-hc-h12-u-ca-calendar` 出现了两次 `u`）。注意它是 `#[non_exhaustive]`，未来可能新增变体。

#### 4.1.4 代码实践

**实践目标**：亲手验证「解析只做语法校验和大小写规范化，不做语义替换」。

**操作步骤**（在 u1-l3 创建的 cargo 项目里，`Cargo.toml` 已有 `icu` 依赖）：

```rust
// 示例代码：可放进 src/main.rs
use icu::locale::Locale;

fn main() {
    // 1. 大小写规范化：解析器负责，不需要 canonicalizer
    let a: Locale = "eN-latn-Us".parse().unwrap();
    println!("{a}"); // 预期：en-Latn-US

    // 2. 一个废弃/旧码 —— 解析照样成功，因为它 well-formed
    let iw: Locale = "iw".parse().unwrap();
    println!("{iw}"); // 预期：iw（不会被解析器改成 he）

    // 3. 触发解析错误
    let bad = "x2".parse::<Locale>(); // 语言子标签必须全字母
    println!("{bad:?}");
}
```

**需要观察的现象**：

1. `"eN-latn-Us"` 打印为 `en-Latn-US`——大小写被规范化了。
2. `"iw"`（旧的希伯来语码）原样打印为 `iw`——解析器并不把它改成现代的 `he`，这正是留给 `LocaleCanonicalizer` 的工作。
3. `"x2"` 解析失败，得到 `Err(InvalidLanguage)`。

**预期结果**：输出形如 `en-Latn-US` / `iw` / `Err(InvalidLanguage)`。若你对某条结果不确定，可在本地 `cargo run` 确认（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：`"und-u-hc-h12-u-ca-calendar".parse::<Locale>()` 会得到什么？

**答案**：返回 `Err(ParseError::DuplicatedExtension)`。因为字符串里出现了两次 Unicode 扩展单字符键 `u`，违反了「每种扩展只能出现一次」的规则（见 errors.rs 的 doctest）。

**练习 2**：为什么解析器不直接把 `iw` 改成 `he`？

**答案**：因为「废弃码 → 现代号」的映射属于 canonical 级别，需要查 CLDR 的别名数据表；而解析器在无数据依赖的 `icu_locale_core` 中，只能做到 well-formed + 大小写规范化。语义替换交给带数据的 `LocaleCanonicalizer`，这是「语法层在 core、语义层在数据 crate」的分层设计。

---

### 4.2 规范化与 LocaleCanonicalizer

#### 4.2.1 概念说明

`LocaleCanonicalizer` 实现 [UTS #35: Annex C, LocaleId Canonicalization](https://unicode.org/reports/tr35/#LocaleId_Canonicalization)。它的职责是：**把一个 locale 改写成它的规范形式**——替换废弃的语言/地区/脚本/variant 码、展开「复杂地区」、规范化扩展里的值。

它在解析器「上一层」工作：

- 解析器保证 **well-formed + 大小写规范化**；
- `LocaleCanonicalizer` 在此基础上保证 **canonical**（替换废弃码等）。

它的核心数据是一张**别名表 `Aliases`**，记录形如「旧标识符 → 新标识符」的映射。同时它内部持有一个 `LocaleExpander`（4.3 节的主角），用于处理「复杂地区」（complex region）这种需要借助 likely subtags 才能决定的规范化。

为什么规范化器要分这么多子表、还要做不动点循环？因为 UTS #35 的规则有优先级、且一次替换可能引入新的可替换内容（例如先把语言替换掉后，新的语言+variant 组合又命中另一条规则）。所以实现上需要**反复应用规则直到不再变化**。

#### 4.2.2 核心流程

`canonicalize(&mut locale)` 的主循环是一个**不动点循环（fixed-point loop）**，每轮按 UTS #35 的优先级顺序尝试若干类规则，只要任一类命中并修改了 locale，就 `continue` 重新开始下一轮；只有当一整轮什么都没命中，才 `break`。顺序大致是：

```
每轮：
  ① 若有 variants → 语言+variant 规则 (language_variants)
     否则           → 绝对语言规则 (language)
  ② sgn-[region] 手语规则           (sgn_region)
  ③ 语言长度规则 language_len2/len3 (如 iw→he)
  ④ 脚本规则                       (script)
  ⑤ 地区规则 region_alpha/region_num/complex_region
  ⑥ variant 规则                   (variant)
  若本轮无任何修改 → 退出循环
最后：
  ⑦ 规范化扩展 (transform / unicode 的 rg、sd)
返回 TransformResult（Modified / Unmodified）
```

其中第 ⑤ 类的 **complex_region（复杂地区）** 最有意思：当一个地区码（如某些已废弃的地区）会映射到**多个**候选地区时，规范化器不能随便挑一个，而要用 `expander.maximize` 算出当前语言+脚本的「期望地区」，如果它恰好在候选列表里就选它，否则取默认（第一个）。

`TransformResult` 的定义：

[components/locale/src/lib.rs:101-109](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale/src/lib.rs#L101-L109) —— `TransformResult::Modified` / `Unmodified`，用于报告本次操作是否真的改动了入参。注意它被标注为「稳定枚举」。

#### 4.2.3 源码精读

**结构体与数据**：

[components/locale/src/canonicalizer.rs:39-45](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale/src/canonicalizer.rs#L39-L45) —— `LocaleCanonicalizer<Expander>`：持有别名表 `aliases: DataPayload<LocaleAliasesV1>` 和一个 `expander`。它是泛型的，默认 `Expander = LocaleExpander`，但你可以注入自定义的（只要实现 `AsRef<LocaleExpander>`）。

**构造函数**：分「常用」与「扩展」两档。

[components/locale/src/canonicalizer.rs:197-262](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale/src/canonicalizer.rs#L197-L262) —— `new_common()`（compiled_data，用常用 likely subtags）与 `new_extended()`（用全量 likely subtags）。它们都委托给 `new_with_expander`，差别只在内嵌的 `LocaleExpander` 覆盖面。doctest 给出的经典例子：`ja-Latn-fonipa-hepburn-heploc` → `ja-Latn-alalc97-fonipa`（hepburn-heploc 这两个 variant 被规范成 alalc97）。

**主算法 canonicalize**：

[components/locale/src/canonicalizer.rs:321-470](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale/src/canonicalizer.rs#L321-L470) —— `canonicalize` 的全部实现。第 326 行起 `loop`（不动点循环）；第 332-340 行按「有无 variants」分流到两套语言规则；第 344-362 行是 `sgn-region` 手语特殊规则；第 364-369 行调用 `uts35_check_language_rules`（处理 `iw`→`he` 这类纯语言替换）；第 372-382 行脚本替换；第 384-434 行地区替换（含复杂地区）；第 436-460 行 variant 替换；第 462-464 行本轮无修改则 `break`；第 466-469 行最后处理扩展。每命中一类就 `result = Modified; continue;`。

**复杂地区的处理**（最能体现「借用 likely subtags」的设计）：

[components/locale/src/canonicalizer.rs:402-433](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale/src/canonicalizer.rs#L402-L433) —— 当地区命中 `complex_region`（一对多映射）时：先用当前 language+script 构造一个临时的 `maximized` 并调用 `self.expander.as_ref().maximize(&mut maximized)` 算出「期望地区」，若它在候选列表里就采用，否则取 `default_region`（候选的第 0 项）。

**规则匹配与替换的两个核心函数**：

[components/locale/src/canonicalizer.rs:47-86](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale/src/canonicalizer.rs#L47-L86) —— `uts35_rule_matches`：判断一条规则的左侧（language/script/region/variants）是否是当前 locale 的**子集**。注意 variants 的匹配利用「两边都已排序」做线性扫描，复杂度 O(n)。doctest 的例子：`und-hepburn` 能匹配 `ja-heploc-hepburn`（规则左侧是子集），反之不行。

[components/locale/src/canonicalizer.rs:88-161](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale/src/canonicalizer.rs#L88-L161) —— `uts35_replacement`：把命中规则的 locale 字段替换成规范值。第 98-107 行的「只在缺失时补全」语义很关键——例如规则左侧没写 region，就只有当源 region 为空且替换值有 region 时才补；第 108-160 行用一个三路归并（sources − skips + replacements）合并 variant 列表。

[components/locale/src/canonicalizer.rs:163-195](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale/src/canonicalizer.rs#L163-L195) —— `uts35_check_language_rules`：纯语言替换的快速路径。按语言码长度（2 或 3）查 `language_len2` / `language_len3`（如 2 字母的 `iw`→`he`）。

**数据结构 Aliases**：

[components/locale/src/provider.rs:246-287](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale/src/provider.rs#L246-L287) —— `Aliases` 结构。字段被刻意拆成多张小表（`language_len2`、`language_len3`、`sgn_region`、`script`、`region_alpha`、`region_num`、`complex_region`、`variant`、`language_variants`、`language`、`subdivision`），文档说明这样「可以避免不必要的查找」——例如 `sgn_region` 只在输入是手语时才查。这正是把 UTS #35 的优先级规则物化成数据布局。

#### 4.2.4 代码实践

**实践目标**：亲手区分「解析器的大小写规范化」与 `LocaleCanonicalizer` 的「语义规范化」，并观察 `TransformResult`。

**操作步骤**：

```rust
// 示例代码
use icu::locale::{Locale, LocaleCanonicalizer, TransformResult};

fn main() {
    let lc = LocaleCanonicalizer::new_extended();

    // A. 题目给的例子：规范化 "ES-ar"
    let mut loc: Locale = "ES-ar".parse().unwrap();
    let res = lc.canonicalize(&mut loc);
    println!("ES-ar -> {loc}  ({res:?})");

    // B. 真正发生语义替换的例子（源码 doctest 验证过）
    let mut loc: Locale = "ja-Latn-fonipa-hepburn-heploc".parse().unwrap();
    let res = lc.canonicalize(&mut loc);
    println!("hepburn -> {loc}  ({res:?})");
}
```

**需要观察的现象与预期结果**：

1. **A 例 `ES-ar`**：解析阶段就已经把大小写规范化成 `es-AR`（西班牙语、阿根廷）。`es-AR` 本身就是规范形式，没有别名命中，因此 `canonicalize` 返回 `TransformResult::Unmodified`，locale 仍为 `es-AR`。
   - 这一步恰恰说明了一个**容易踩的坑**：很多人以为「规范化」会修大小写，其实大小写早在 `parse` 时就被 `Language`/`Script`/`Region` 的构造函数修好了（见 4.1.3 的 `to_ascii_lowercase`）。`canonicalize` 看到的永远是已经大小写正确的输入。
2. **B 例 `ja-Latn-fonipa-hepburn-heploc`**：命中 `language_variants` 规则，`hepburn-heploc` 这两个 variant 被替换成 `alalc97` 并重新排序，结果是 `ja-Latn-alalc97-fonipa`，`TransformResult::Modified`。这与源码 doctest（canonicalizer.rs:27-36）完全一致。

**进阶（待本地验证）**：尝试 `let mut loc: Locale = "iw".parse().unwrap(); lc.canonicalize(&mut loc);`，预期旧希伯来语码 `iw` 被替换为现代码 `he`（命中 `language_len2`），返回 `Modified`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `canonicalize` 要用「不动点循环」而不是单趟扫描一次别名表？

**答案**：因为一次替换可能产生新的可替换内容。UTS #35 的规则有优先级（variant 数多的规则先考虑），且替换后可能让结果再次命中其它规则。单趟无法保证收敛到规范形式，所以实现上反复应用规则直到某一轮完全没有修改（`break`）才结束。

**练习 2**：`Aliases` 数据为什么要把映射拆成 `language_len2` / `language_len3` / `sgn_region` / `complex_region` 等十来张小表，而不是一张大表？

**答案**：出于性能。拆分后可以「按输入特征只查相关的表」：只有输入是手语时才查 `sgn_region`，只有 2 字母语言码才查 `language_len2`。这样绝大多数 locale 在每一轮只触发极少数查找，避免了在一张巨型表上线性/哈希查找的开销。文档（provider.rs:228-231）明确说明了这一动机。

---

### 4.3 likely subtags：LocaleExpander 的 maximize 与 minimize

#### 4.3.1 概念说明

**Likely subtags（可能子标签）**回答这样一个问题：给定一个不完整的 locale，它「最可能」的完整形式是什么？CLDR 统计了全球语言使用数据，给出如「说中文最可能的脚本是 Hans、地区是 CN」「只有地区 US 时最可能的语言是 en、脚本是 Latn」这样的推断表。

基于此，UTS #35 定义了两个互逆算法：

- **Add Likely Subtags（maximize，最大化）**：补全缺失子标签。例如 `zh` → `zh-Hans-CN`，`und-US` → `en-Latn-US`。
- **Remove Likely Subtags（minimize，最小化）**：在「最大化后仍能还原回原值」的前提下，删除可省略的子标签。例如 `zh-Hans-CN` → `zh`。

这两者非常重要：maximize 用于把用户给的稀疏 locale（如 `zh`）补全成数据查找用的完整键；minimize 用于生成更短、更通用的 locale 标识。第 5 单元你会看到，**locale 回退链（fallback）正是建立在 maximize 之上的**。

`LocaleExpander` 提供两套数据：

- `new_common()`：只含 **Basic 及以上 CLDR 覆盖** 的常用语言数据，体积小，适合数据导向的场景。
- `new_extended()`：包含**所有**语言的数据（含覆盖度低于 Basic 的），体积更大但更全。

#### 4.3.2 核心流程

**maximize（Add Likely Subtags）** 的查表优先级（按「已有信息越具体、越优先」的原则）：

```
若 language/script/region 三者齐全 → 已经是最大形式，Unmodified
若 language 已知：
    有 region        → 查 language+region 表补 script
    否则有 script    → 查 language+script 表补 region
    否则             → 查 language 表补 (script, region)
    都查不到         → Unmodified
否则（language=und）若 script 已知：
    有 region        → 查 script+region 表补 language（含 und 默认）
    否则             → 查 script 表补 (language, region)
否则若只有 region：
                     → 查 region 表补 (language, script)
否则 → Unmodified
```

**minimize（Remove Likely Subtags）** 用「先最大化，再逐步删子标签试还原」的策略：

```
先求 max = maximize(原值)
试只留 language          若 maximize(language) == max → 返回 language
（默认 minimize）试留 language+region   若能还原 → 返回 language+region
                  再试留 language+script   若能还原 → 返回 language+script
都不行 → 返回完整的 max
```

`minimize`（默认，`favor_region=true`）倾向于保留地区；`minimize_favor_script`（`favor_region=false`）倾向于保留脚本。例如对 `yue-Hans`：`minimize` 给出 `yue-CN`（保留地区），而 `minimize_favor_script` 给出 `yue-Hans`（保留脚本）。

#### 4.3.3 源码精读

**结构体与数据**：

[components/locale/src/expander.rs:65-70](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale/src/expander.rs#L65-L70) —— `LocaleExpander` 持有三块数据：`likely_subtags_l`（含 language 的映射）、`likely_subtags_sr`（不含 language、只有 script/region 的映射）、以及可选的 `likely_subtags_ext`（扩展集，`new_common` 时为 `None`）。文件顶部 doctest（expander.rs:18-62）展示了 maximize/minimize 的典型用法。

[components/locale/src/provider.rs:321-338](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale/src/provider.rs#L321-L338) —— `LikelySubtagsForLanguage` 数据结构：`language_script`、`language_region`、`language` 三张映射表，外加一个特殊的 `und`（未确定语言的默认展开）。注意每个字段只存「与查找相关的部分」以节省空间，文档（provider.rs:307-310）专门解释了这一点。

**查表封装 LocaleExpanderBorrowed**：

[components/locale/src/expander.rs:79-148](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale/src/expander.rs#L79-L148) —— `get_l` / `get_ls` / `get_lr` / `get_s` / `get_sr` / `get_r` / `get_und` 六个查找方法。每个都先查常用表，查不到再 `or_else` 查扩展表（`likely_subtags_ext`），这就是 `new_common` 与 `new_extended` 行为差异的实现机制。

**maximize 主算法**：

[components/locale/src/expander.rs:375-428](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale/src/expander.rs#L375-L428) —— `maximize`。第 379-381 行：三者齐全直接返回 `Unmodified`；第 383-399 行：language 已知时的三条分支（先 `get_lr`、再 `get_ls`、再 `get_l`）；第 400-414 行：language 未知但 script 已知（注意第 404、410 行用 `.or_else(|| (... == und_s/und_r).then_some(...))` 处理「恰为默认脚本/地区」的退化情形）；第 415-421 行：只有 region。命中任一分支后调用 `update_langid` 写回。

[components/locale/src/expander.rs:150-179](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale/src/expander.rs#L150-L179) —— `update_langid`：只在字段为空时才填充（`is_unknown` / `is_none`），并据此返回 `Modified` / `Unmodified`。这保证了 maximize「只补缺、不覆盖已有」。

**minimize 主算法**：

[components/locale/src/expander.rs:455-457](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale/src/expander.rs#L455-L457) 与 [components/locale/src/expander.rs:483-485](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale/src/expander.rs#L483-L485) —— `minimize` 与 `minimize_favor_script` 都只是 `minimize_impl` 的薄封装，差别在 `favor_region` 布尔参数。

[components/locale/src/expander.rs:487-537](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale/src/expander.rs#L487-L537) —— `minimize_impl`：第 492-493 行先把副本 `max` 最大化；第 495-502 行试「只留 language」能否还原回 `max`；第 504-534 行按 `favor_region` 决定先试 `language+region` 还是 `language+script`；都还原不了时第 536 行返回完整 `max`。每次「试还原」都是克隆一份、删掉某些字段、再 `maximize` 看是否等于 `max`——即「删了之后最大化还能回到原值，说明删掉的是冗余信息」。

**测试佐证两种最小化倾向**：

[components/locale/src/expander.rs:593-610](https://github.com/unicode-org/icu4x/blob/67a0b91c6f1e23210c9813bfe5ebb86e77f35460/components/locale/src/expander.rs#L593-L610) —— `test_minimize_favor_script` 与 `test_minimize_favor_region`：同一个 `yue-Hans`，`minimize` 得 `yue-CN`，`minimize_favor_script` 得 `yue-Hans`（且 `Unmodified`，因为已经是它偏好的形式）。

#### 4.3.4 代码实践

**实践目标**：完成规格要求的两个操作——规范化练习里用到的 maximize，以及 maximize/minimize 的互逆观察。

**操作步骤**：

```rust
// 示例代码
use icu::locale::{locale, LocaleExpander, TransformResult};

fn main() {
    let lc = LocaleExpander::new_common();

    // 1. 题目要求：把 "zh" 最大化
    let mut loc = locale!("zh");
    let res = lc.maximize(&mut loc.id);
    println!("maximize zh -> {loc}  ({res:?})");

    // 2. 反过来：把完整形式最小化
    let mut loc = locale!("zh-Hans-CN");
    let res = lc.minimize(&mut loc.id);
    println!("minimize zh-Hans-CN -> {loc}  ({res:?})");

    // 3. new_common 不支持、new_extended 才支持的例子
    let lc_ext = LocaleExpander::new_extended();
    let mut loc = locale!("ccp");
    let res = lc_ext.maximize(&mut loc.id);
    println!("maximize(ext) ccp -> {loc}  ({res:?})");
}
```

**需要观察的现象与预期结果**：

1. `maximize("zh")` → `zh-Hans-CN`，`TransformResult::Modified`。只有 language 时走 `get_l("zh")`，一次补齐 script=Hans、region=CN。这与源码 doctest（expander.rs:26-33 给出的 `zh-CN`→`zh-Hans-CN`）一致。
2. `minimize("zh-Hans-CN")` → `zh`，`Modified`。验证 maximize 与 minimize 的互逆性（doctest 在 expander.rs:44-49）。
3. 用 `new_extended()` 时 `ccp` → `ccp-Cakm-BD`（`Modified`）；而若改用 `new_common()`，`ccp` 会因常用表无数据而返回 `Unmodified`、保持 `ccp`（doctest 在 expander.rs:354-373）。这正是两套数据覆盖面的差异。

> 说明：第 1 项的结果直接由 maximize 算法的 `get_l` 分支与已知的中文 likely subtags 推出，并与源码 doctest 的 `zh-CN` 案例相互印证；若想百分百确认可在本地 `cargo run`（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`LocaleExpander::new_common().maximize("und-US")` 会得到什么？为什么？

**答案**：得到 `en-Latn-US`，`Modified`。因为 language 是 `und`（未知）、region 是 `US`，maximize 走「只有 region」分支，查 `get_r("US")` 得到 `(en, Latn)` 并补全。源码 doctest（expander.rs:658-660）正是这个例子。

**练习 2**：minimize 是怎么判断「某个子标签可以安全删除」的？

**答案**：它先求出完整最大化形式 `max`，然后克隆一份、删掉待删字段、再 `maximize` 一次；如果重新最大化的结果等于 `max`，说明被删的字段是「最大化必然能还原」的冗余信息，可以删；否则不能删。这是一个「**删后再最大化验证**」的试错过程（minimize_impl 的 trial 副本）。

**练习 3**：`new_common()` 和 `new_extended()` 在结构上的唯一区别是什么？

**答案**：`likely_subtags_ext` 字段。`new_common()` 把它设为 `None`，`new_extended()` 把它设为 `Some(...)`（见 expander.rs:224-234 与 272-284）。所有查找方法（`get_l` 等）在常用表未命中时会 `or_else` 查这张扩展表，所以 `new_extended` 能处理更多冷门语言（如 `ccp`、`atj`）。

## 5. 综合实践

把本讲三个模块串起来，写一个小工具：**输入任意「脏」的 locale 字符串，输出它的规范且最大化形式**。这个流程在真实的国际化系统里非常常见——用户/HTTP 请求传来的 locale 往往大小写混乱、带废弃码、还不完整，需要先清洗才能用于数据查找。

**任务**：实现函数 `clean_locale(input: &str) -> Result<String, icu::locale::ParseError>`，依次完成：

1. 用 `Locale::try_from_str(input)` 解析（well-formed + 大小写规范化，失败则把错误向上传）。
2. 用 `LocaleCanonicalizer::new_extended()` 规范化（替换废弃码等）。
3. 用 `LocaleExpander::new_extended()` 的 `maximize` 补全缺失子标签。
4. 把结果 `to_string()` 返回。

**参考实现骨架（示例代码）**：

```rust
use icu::locale::{Locale, LocaleCanonicalizer, LocaleExpander};

fn clean_locale(input: &str) -> Result<String, icu::locale::ParseError> {
    let canon = LocaleCanonicalizer::new_extended();
    let exp = LocaleExpander::new_extended();

    let mut loc: Locale = Locale::try_from_str(input)?; // ① 解析
    canon.canonicalize(&mut loc);                        // ② 规范化
    exp.maximize(&mut loc.id);                           // ③ 补全
    Ok(loc.to_string())                                  // ④ 输出
}

fn main() {
    for s in ["ES-ar", "iw", "zh", "und-us", "ja-Latn-fonipa-hepburn-heploc"] {
        println!("{s:40} -> {}", clean_locale(s).unwrap_or_else(|e| format!("{e:?}")));
    }
}
```

**需要观察的现象与预期结果（待本地验证）**：

- `ES-ar` → 解析即得 `es-AR`，规范化和补全后约为 `es-Latn-AR`。
- `iw` → 规范化为 `he`，再最大化约为 `he-Hebr-IL`。
- `zh` → 最大化为 `zh-Hans-CN`。
- `und-us` → 解析得 `und-US`，最大化为 `en-Latn-US`。
- `ja-Latn-fonipa-hepburn-heploc` → 规范化为 `ja-Latn-alalc97-fonipa`（已含 script，maximize 不再改动）。

**思考题**：为什么顺序必须是「解析 → 规范化 → 补全」？如果先 maximize 再 canonicalize 会出什么问题？（提示：maximize 可能补进 script，而某些 canonical 规则依赖「是否已有 script」；且对废弃码先补全再替换可能产生非规范组合。）

## 6. 本讲小结

- **三层合规级别**：well-formed < valid < canonical。ICU4X 的**解析器**只做到 well-formed + 大小写规范化，valid/canonical 留给数据层与 `LocaleCanonicalizer`。
- **解析流程**：`SubtagIterator` 按 `-` 切字节 → `parse_language_identifier_from_iter` 用 `ParserPosition` 状态机按 language→script→region→variants 顺序解析 → `parse_locale` 再接扩展。**大小写规范化发生在每个子标签的构造函数里**（如 `Language` 的 `to_ascii_lowercase`），不是 canonicalizer 做的。
- **`LocaleCanonicalizer`** 实现 UTS #35 Annex C，靠**不动点循环**按优先级反复应用别名规则，命中任一类就重来，直到一轮无修改。复杂地区（complex_region）会借用 `LocaleExpander::maximize` 来选最优候选。
- **`TransformResult`**（`Modified` / `Unmodified`）统一描述「是否真的改了」，是 canonicalize、maximize、minimize 的通用返回。
- **`LocaleExpander`** 实现 likely subtags：maximize 按查表优先级补缺、只补不覆盖；minimize 用「删后再最大化验证」删除冗余子标签，分 `minimize`（偏地区）和 `minimize_favor_script`（偏脚本）两种。
- **`new_common` vs `new_extended`** 的差别只在 `likely_subtags_ext` 是否为 `Some`，决定了能否处理 Basic 覆盖以下的冷门语言。

## 7. 下一步学习建议

- **下一步必读 u2-l4（Locale 回退链与文本方向性）**：locale 回退（如 `de-CH → de → und`）正是建立在 maximize 之上的——回退器会先把 locale 最大化，再逐级删除子标签生成回退候选。理解了本讲的 maximize/minimize，回退链的源码会非常顺。
- **数据视角**：本讲的 `Aliases`、`LikelySubtagsForLanguage` 都是 `ZeroMap` / `VarZeroVec` 零拷贝结构。当你进入第 5 单元（数据提供器）和第 6 单元（zerovec）时，可以回头重读 `components/locale/src/provider.rs`，体会这些数据结构如何做到「直接从字节切片解释、零分配」。
- **源码延伸阅读**：
  - `components/locale/src/canonicalizer.rs` 的 `canonicalize_extensions`（处理 `rg`/`sd` 等扩展值的规范化）。
  - `components/locale/src/expander.rs` 的 `get_likely_script` / `infer_likely_script`（maximize 的一个内联简化版，被日历等组件用来推断脚本）。
