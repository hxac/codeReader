# 元数据与时间戳

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 PDF 文档级元数据（标题、作者、关键词、创建者、语言、书写方向、文档 ID、创建日期）各自的**数据来源**——它们分别从 Typst 的 `document.info()` 还是 `PdfOptions` 取值。
- 讲透创建日期 `creation_date()` 的**三级回退逻辑**，尤其是 `document.date` 这个 `Smart<Option<Datetime>>` 的三态语义如何决定「到底写不写日期」。
- 理解 `Timestamp` / `Timezone` 这两个对外公开类型的构造方式，特别是 `new_local` 中 `minute_offset` 为何要取 `.abs()`、以及时区偏移边界 `-23..=23` / `0..=59` 的依据。

本讲承接 u2-l5（`convert()` 编排）。元数据写入是 `convert()` 收尾阶段的一环，位于 `tags::resolve` 之后、`finish()` 之前。

## 2. 前置知识

在进入源码前，先建立几个概念：

- **文档级元数据（document metadata）**：PDF 信息字典（Info dictionary）以及现代 PDF 中的 XMP 元数据，记录文档的标题、作者、创建者程序、创建/修改时间、关键词等。这些字段不影响页面渲染，但决定 PDF 阅读器「属性」面板里显示什么，也影响 PDF/A 归档校验与全文检索。
- **`Smart<T>`**：Typst 的两态类型，`Auto`（用默认值/自动推断）或 `Custom(T)`（用户显式指定）。本讲会频繁遇到 `Smart<Option<Datetime>>`，它有**三态**：`Auto`、`Custom(Some)`、`Custom(None)`，三态语义不同。
- **`Datetime`**：Typst 表示一个时间点或时间段落的类型，分量（年/月/日/时/分/秒）各自是 `Option`，因为 Typst 允许「只有日期」「只有时间」「完整日期时间」三种精度。
- **时区偏移（timezone offset）**：相对 UTC 的偏移量，写作 `O±HH'mm`。例如北京时间是 UTC+8（`+08'00`），印度是 UTC+5:30（`+05'30`）。
- **krilla**：typst-pdf 委托的底层 PDF 生成库（见 u1-l1）。本讲里出现的 `krilla::metadata::{Metadata, DateTime, TextDirection}` 都是 krilla 提供的类型，typst-pdf 只负责把 Typst 的数据翻译过去。

## 3. 本讲源码地图

本讲只精读一个文件，并少量引用两个上下文文件：

| 文件 | 作用 |
|------|------|
| [metadata.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/metadata.rs) | 本讲主角。包含 `build_metadata()`、`creation_date()`、对外公开的 `Timestamp` / `Timezone` 类型及其构造与边界校验，以及单元测试。 |
| [src/lib.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs) | `PdfOptions` 定义了 `ident` / `creator` / `timestamp` 三个与本讲强相关的字段；`pub use self::metadata::{Timestamp, Timezone}` 把这两个类型重新导出为 crate 的公共 API。 |
| [src/convert.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs) | 调用点：`tags::resolve` 产出 `doc_lang`，随后 `document.set_metadata(build_metadata(&gc, doc_lang))`。 |

## 4. 核心概念与源码讲解

本讲拆为三个最小模块：`build_metadata()` 的总装配、`creation_date()` 的三级回退、`Timestamp`/`Timezone` 的构造与校验。

---

### 4.1 build_metadata()：元数据的总装配

#### 4.1.1 概念说明

PDF 元数据字段众多，但它们的来源只有两个：

1. **Typst 文档自身**：用户在 Typst 源码里写的 `set document(title: .., author: .., date: .., keywords: ..)`，最终汇集到 `document.info()` 返回的 `DocumentInfo` 结构。
2. **导出选项 `PdfOptions`**：调用方（如 CLI 或上层库）传入的 `ident`、`creator`、`timestamp`。

`build_metadata()` 就是把这两个来源的字段「搬运」进 krilla 的 `Metadata` builder。它的职责是纯粹的**字段映射 + 缺省值填充**，不做任何排版或字节级工作。

有一个重要的外部输入：`doc_lang`。它来自 `tags::resolve()`（u5-l23 会详述），即从无障碍结构树解析过程中得到的**文档主语言**。语言信息并不取自 `DocumentInfo`（那里只有 `locale: Smart<Locale>`），而是取自 tag 树。

#### 4.1.2 核心流程

`build_metadata(gc, doc_lang)` 的执行步骤：

1. **确定语言**：`doc_lang.unwrap_or(Locale::DEFAULT)`。无论用户是否设置，都强制写一个语言，因为 PDF/UA-1 隐式要求文档有语言，以便元数据和大纲条目有适用的语言上下文。
2. **确定书写方向**：根据语言的默认书写方向（阿拉伯/希伯来/波斯等是 RTL，其余 LTR）映射到 krilla 的 `TextDirection`。
3. **构造基础 builder**：写入 `keywords`、`authors`、`language`（经 `rfc_3066()` 转成字符串）。
4. **按条件追加可选字段**：`creator`、`title`、`description`、`document_id`、`creation_date`，每个字段只有满足条件才写入。
5. **无条件写入书写方向**，返回 `Metadata`。

#### 4.1.3 源码精读

[metadata.rs:9-53](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/metadata.rs#L9-L53) 是 `build_metadata` 的全部实现。关键片段：

```rust
// 始终写一个语言，PDF/UA-1 隐式要求文档语言
let lang = doc_lang.unwrap_or(Locale::DEFAULT);

let dir = if lang.lang.dir() == Dir::RTL {
    TextDirection::RightToLeft
} else {
    TextDirection::LeftToRight
};

let mut metadata = Metadata::new()
    .keywords(gc.document.info().keywords.iter().map(Into::into).collect())
    .authors(gc.document.info().author.iter().map(Into::into).collect())
    .language(lang.rfc_3066().to_string());
```

`create`（创建者）字段体现了「选项优先、自动兜底」的典型模式：

```rust
if let Some(creator) = gc.options.creator.clone()
    .unwrap_or_else(|| Some(format!("Typst {}", typst_utils::version().raw())))
{
    metadata = metadata.creator(creator);
}
```

`creator` 字段类型是 `Smart<Option<String>>`（见 [lib.rs:71-73](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs#L71-L73)）。`Smart::unwrap_or_else` 在 `Auto` 时执行闭包，返回 `Some("Typst $version")`；在 `Custom(opt)` 时直接返回 `opt`（可能是 `Some` 或 `None`）。最外层 `if let Some(creator)` 再过滤掉 `None`，所以三种状态的行为是：

| `options.creator` | 结果 |
|---|---|
| `Smart::Auto` | 写入 `"Typst <版本号>"` |
| `Custom(Some("My App"))` | 写入 `"My App"` |
| `Custom(None)` | 不写 creator 字段 |

`document_id` 只在 `ident` 为 `Custom` 时写入：

```rust
if let Smart::Custom(ident) = gc.options.ident.clone() {
    metadata = metadata.document_id(ident);
}
```

注意 `Auto` 时**完全跳过**——文档 ID 不由这里生成。当 `ident` 是 `Auto` 时，krilla 内部会用文档标题/作者的哈希派生一个标识符（见 [lib.rs:59-70](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs#L59-L70) 的文档注释）。`title` / `description` / `creation_date` 三个可选字段同理，用 `if let Some/Some(date)` 守卫，缺省即不写。

调用点在 [convert.rs:88-92](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L88-L92)，元数据写入排在 `tags::resolve` 之后，因为语言要等结构树解析：

```rust
let (doc_lang, tree) = tags::resolve(&mut gc)?;
// ...
document.set_metadata(build_metadata(&gc, doc_lang));
```

#### 4.1.4 代码实践

**实践目标**：建立「字段 → 来源」的映射表，并理解每个字段的缺省行为。

**操作步骤**：

1. 打开 [metadata.rs:9-53](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/metadata.rs#L9-L53)。
2. 对 `title` / `description` / `keywords` / `author` / `date` 这几个字段，确认它们全部来自 `gc.document.info()`（即 Typst 源码里的 `set document(..)`），与 `PdfOptions` 无关。
3. 对 `creator` / `ident` / `timestamp`（经 `creation_date`），确认它们来自 `gc.options`。
4. 观察 `language` / `text_direction` 既不来自 `info` 也不直接来自 `options`，而是来自 `doc_lang`（tag 树解析产物）。

**需要观察的现象**：你会发现「关键字段分三个来源」——文档信息字典、导出选项、结构树语言。

**预期结果**：完成下面这张表（自行填写第三列）：

| 字段 | 来源 | 缺省/回退行为 |
|---|---|---|
| title | `document.info().title` | 为 `None` 则不写 |
| author | `document.info().author` | 空列表则写入空 |
| creator | `options.creator` | `Auto` → `"Typst $version"` |
| document_id | `options.ident` | `Auto` → krilla 内部派生 |
| language | `doc_lang` | `None` → `Locale::DEFAULT`（英语） |

> 待本地验证：用 Typst 编译一个**不写任何 `set document(..)`** 的最小文档并导出，然后用 `pdfinfo` 或 `exiftool` 查看 PDF 属性，确认 `Creator` 字段是否为 `Typst <版本号>`、`Language` 是否被写成 `en`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `language` 始终写入、且为 `None` 时回退到 `Locale::DEFAULT`，而 `title` 可以为空？

**参考答案**：PDF/UA-1 隐式要求文档有适用语言，使元数据和大纲条目能被朗读/检索引擎正确处理；语言缺失会影响无障碍性。而标题是纯展示字段，缺失不会破坏规范合规性，故允许不写。源码注释 [metadata.rs:10-11](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/metadata.rs#L10-L11) 明确说明了这一点。

**练习 2**：书写方向 `TextDirection` 是依据什么决定的？它与页面内文字的 `Dir::RTL` 是同一个东西吗？

**参考答案**：依据 `lang.lang.dir()`，即文档主语言的**默认书写方向**（阿拉伯/希伯来等 → RTL）。它和页面内某个段落的方向不是一回事——这里是文档级元数据，告诉阅读器「整篇文档的主导方向」，用于默认滚动条方向、页面布局提示等。

---

### 4.2 creation_date()：创建日期的三级回退

#### 4.2.1 概念说明

创建日期是元数据里最「绕」的字段，因为它有两个潜在来源、三种状态，且优先级容易搞反。typst-pdf 用一个明确的**三级回退（three-tier fallback）**策略来决定写不写、写哪个值。

关键在于 `document.date` 的类型是 `Smart<Option<Datetime>>`——**三态**：

- `Smart::Auto`：用户没写 `set document(date: ..)`，或写了 `date: auto`。
- `Custom(Some(dt))`：用户写了一个具体日期。
- `Custom(None)`：用户**显式**写了 `date: none`，表示「我不要日期」。

#### 4.2.2 核心流程

回退逻辑用源码上方 [metadata.rs:55-58](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/metadata.rs#L55-L58) 的文档注释已概括：

1. 若 `document.date` 是 `Custom(Some)` 或 `Custom(None)` → 以它为准（`Some` 用值，`None` 表示明确不要）。
2. 若 `document.date` 是 `Auto` → 尝试用 `options.timestamp`。
3. 否则（`Auto` 且无 `timestamp`）→ 不写日期。

随后把分量逐个搬运进 krilla `DateTime`：年份必须 `>= 0`，否则整体返回 `None`；月/日/时/分/秒各分量只有在 `Some` 时才写入，保留 Typst 的精度语义（只有日期、或只有时间也能导出）。最后根据时区写入 UTC 偏移。

#### 4.2.3 源码精读

[metadata.rs:59-99](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/metadata.rs#L59-L99) 是 `creation_date` 的实现。核心是开头那个 `match`：

```rust
let (datetime, tz) = match (gc.document.info().date, gc.options.timestamp) {
    (Smart::Custom(Some(date)), _) => (date, None),
    (Smart::Auto, Some(timestamp)) => (timestamp.datetime, Some(timestamp.timezone)),
    _ => return None,
};
```

这三行决定了**一切**。注意两个容易被忽略的分支：

- **`(Smart::Custom(Some(date)), _)`**：只要用户显式设了日期，就用它，**忽略 `timestamp`**，且时区为 `None`（`Datetime` 自身不带时区）。这里的 `_` 同时覆盖了「设了日期但调用方也传了 timestamp」的情况——文档设置永远优先。
- **`(Smart::Custom(None), Some(timestamp))` 落入 `_`**：这是最反直觉的一点。用户**显式**写 `set document(date: none)` 表示「我不要日期」，此时即便调用方传了 `timestamp`，也**不写日期**。显式的 `none` 是一种有意义的否定，不会被选项悄悄覆盖。
- **`(Smart::Auto, None)` 也落入 `_`**：两边都没提供 → 不写。

取到 `(datetime, tz)` 后，搬运分量：

```rust
let year = datetime.year().filter(|&y| y >= 0)? as u16;
let mut kd = krilla::metadata::DateTime::new(year);
if let Some(month) = datetime.month() { kd = kd.month(month); }
// ... day / hour / minute / second 同理，各自 if let Some
```

`Datetime::year()` 返回 `Option<i32>`（见 typst-library 的 `datetime.rs`），`month/day/hour/minute/second` 返回 `Option<u8>`。年份负值（公元前）被 `filter` 掉后整体放弃写日期——PDF 的日期格式不支持负年份。

最后应用时区偏移：

```rust
match tz {
    Some(Timezone::UTC) => kd = kd.utc_offset_hour(0).utc_offset_minute(0),
    Some(Timezone::Local { hour_offset, minute_offset }) => {
        kd = kd.utc_offset_hour(hour_offset).utc_offset_minute(minute_offset);
    }
    None => {}
}
```

注意：只有当日期来自 `options.timestamp`（即 `Auto` 分支）时，`tz` 才是 `Some`。因为 `Timestamp` 自带时区，而 Typst 的 `Datetime` 不带时区——这是一个设计上的不对称：**通过 `set document(date: ..)` 设的日期没有时区信息，通过 `PdfOptions::timestamp` 设的才有。**

#### 4.2.4 代码实践

**实践目标**：彻底搞清「什么情况下 PDF 里会出现创建日期」。

**操作步骤**：

1. 对照 [metadata.rs:60-64](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/metadata.rs#L60-L64) 的 `match`，列出 `(document.date, options.timestamp)` 的全部组合与结果。
2. 特别注意 `Custom(None)` 这一列。

**需要观察的现象**：`document.date` 是三态，`options.timestamp` 是 `Option<Timestamp>`（两态），组合后有些行会落入 `_ => return None`。

**预期结果**（请自行推导后对照）：

| `document.info().date` | `options.timestamp` | 是否写创建日期 | 来源 |
|---|---|---|---|
| `Custom(Some(dt))` | 任意 | 是 | `dt`（无时区） |
| `Custom(None)` | `Some(ts)` | **否** | ——（显式 none 胜出） |
| `Custom(None)` | `None` | 否 | —— |
| `Auto` | `Some(ts)` | 是 | `ts`（带时区） |
| `Auto` | `None` | 否 | —— |

> 待本地验证：写两份 Typst 源码，一份 `#set document(date: none)` 并在导出时传 `timestamp`，另一份什么都不设、也不传 `timestamp`，分别用 `pdfinfo` 查看 `CreationDate`，确认两者都**没有**该字段。

#### 4.2.5 小练习与答案

**练习 1**：为什么「用户设了 `date: none` 但调用方传了 `timestamp`」时不写日期？

**参考答案**：因为 `document.date` 是 `Smart<Option<Datetime>>`，`Custom(None)` 是用户**显式**表达「不要日期」，落入 `match` 的 `_` 分支直接 `return None`。文档作者的意图优先于导出选项，避免选项悄悄覆盖用户的显式否定。

**练习 2**：通过 `set document(date: datetime(..))` 设的日期，与通过 `PdfOptions::timestamp` 设的日期，导出到 PDF 后有何不同？

**参考答案**：前者没有时区信息（`tz = None`，PDF 里写成不带 `O±HH'mm` 后缀），后者带时区（写成如 `D:20241217101010+08'00`）。原因是 Typst 的 `Datetime` 类型本身不带时区，而 `Timestamp` 自带 `Timezone`。

---

### 4.3 Timestamp 与 Timezone：时间戳的构造与边界校验

#### 4.3.1 概念说明

`Timestamp` 和 `Timezone` 是 typst-pdf 对外公开的两个类型（在 [lib.rs:16](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs#L16) 通过 `pub use` 重导出）。调用方构造一个 `Timestamp` 塞进 `PdfOptions::timestamp`，就可以让导出的 PDF 携带带时区的创建日期（前提是 `document.date` 为 `Auto`，见 4.2）。

`Timestamp` 内部就是一个 `Datetime` 加一个 `Timezone`：

```rust
pub struct Timestamp {
    pub(crate) datetime: Datetime,
    pub(crate) timezone: Timezone,
}
```

`Timezone` 有两个变体：`UTC`（零偏移）和 `Local { hour_offset: i8, minute_offset: u8 }`。注意 `minute_offset` 是 `u8`（无符号），因为分钟数总是非负——符号由 `hour_offset` 统一承载。现实中的时区都满足「分钟与小时同号」（如 UTC-3:30 的分钟是 `+30`，符号体现在 `-3` 小时上），这一假设是下面 `.abs()` 操作安全的前提。

#### 4.3.2 核心流程

构造一个本地时区时间戳的流程（`new_local`）：

1. 入参是 `datetime` 和 `whole_minute_offset: i32`（一个**以分钟为单位**的整数偏移，如 `+330` 表示 UTC+5:30）。
2. 拆分成 `hour_offset`（整数小时）和 `minute_offset`（剩余分钟）。
3. 校验拆分结果落在合法范围内，否则返回 `None`。

#### 4.3.3 源码精读

两个构造函数在 [metadata.rs:110-134](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/metadata.rs#L110-L134)。`new_utc` 很简单：直接把 `timezone` 设为 `UTC`。

`new_local` 的精髓在于拆分与校验：

```rust
pub fn new_local(datetime: Datetime, whole_minute_offset: i32) -> Option<Self> {
    let hour_offset = (whole_minute_offset / 60).try_into().ok()?;
    // Rust 的 `%` 是「求余」不是「取模」，可能返回负数
    let minute_offset = (whole_minute_offset % 60).abs().try_into().ok()?;
    match (hour_offset, minute_offset) {
        (-23..=23, 0..=59) => Some(Self {
            datetime,
            timezone: Timezone::Local { hour_offset, minute_offset },
        }),
        _ => None,
    }
}
```

`Timezone` 枚举本身定义在 [metadata.rs:136-144](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/metadata.rs#L136-L144)。

**为什么 `minute_offset` 要取 `.abs()`？**

Rust 的 `%` 是**求余运算符（remainder）**而非取模（modulo），它的结果**与被除数同号**。对负的 `whole_minute_offset`，`%` 会得到负的分钟数。例如 `whole_minute_offset = -210`（UTC-3:30）：

\[
(-210) / 60 = -3 \quad(\text{向零截断}),\qquad (-210) \bmod 60 = -30
\]

而我们希望分钟数是非负的 `30`，符号已经由 `hour_offset = -3` 表达。所以用 `.abs()` 把 `-30` 修正为 `30`。源码注释 [metadata.rs:119-122](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/metadata.rs#L119-L122) 明确说明了这个动机，并指出它依赖于「`minute_offset` 与 `hour_offset` 同号」这一现实假设（所有真实时区都满足）。`try_into().ok()?` 同时把 `i32` 收窄为 `u8`/`i8`，越界直接返回 `None`。

**为什么合法范围是 `-23..=23` 小时和 `0..=59` 分钟？**

这是一个**结构性的上限**而非「现实时区集合」的上限：

- 分钟 `0..=59`：一小时 60 分钟，`60` 分钟应当并入下一个整小时。
- 小时 `-23..=23`：时区偏移的**绝对值必须严格小于 24 小时**。一个恰好等于 ±24:00 的偏移在语义上等于「换一天、零偏移」，不是合法的独立偏移量，PDF 的日期格式也不接受它。

注意代码故意比现实世界宽松：真实极端时区是 UTC-12（AoE，Anywhere on Earth）和 UTC+14（基里巴斯的莱恩群岛），都远在 ±23 之内。代码不校验「是否真实存在」，只校验「表示上是否合法」（严格小于一天），从而既挡住 `1440`（正好 24 小时）、`i32::MAX` 这类无意义值，又不误伤任何合理偏移。测试 [metadata.rs:150-181](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/metadata.rs#L150-L181) 正好覆盖了这些边界：`1439` / `-1439`（±23:59）合法，而 `1440` / `-1440` / `i32::MAX` / `i32::MIN` 返回 `None`。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：通过阅读 `test_timestamp_new_local`，验证你对 `.abs()` 与边界校验的理解，并回答两个关键问题。

**操作步骤**：

1. 打开 [metadata.rs:150-181](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/metadata.rs#L150-L181)。
2. 对照下面几个用例，手算 `hour_offset` 与 `minute_offset`：
   - `test(330, Local { 5, 30 })`：印度 UTC+5:30。`330/60=5`，`330%60=30`。
   - `test(-210, Local { -3, 30 })`：UTC-3:30（纽芬兰）。`-210/60=-3`，`(-210%60).abs()=(-30).abs()=30`。
   - `test(-225, Local { -3, 45 })`：`-225/60=-3`，`(-225%60)=-45`，`.abs()=45`。
   - `test(-720, Local { -12, 0 })`：UTC-12（AoE）。
   - `assert!(Timestamp::new_local(.., 1440).is_none())`：`1440/60=24`，落在 `-23..=23` 之外 → `None`。
3. 回答两个问题（见「预期结果」）。

**需要观察的现象**：负偏移的分钟分量经过 `.abs()` 后总是非负，与小时分量的符号「解耦」。

**预期结果**：

- **问题一：为何 `minute_offset` 要取 `abs`？** 因为 Rust 的 `%` 是求余（与被除数同号），负偏移会算出负分钟；而 `Timezone::minute_offset` 是 `u8`（无符号，符号由 `hour_offset` 承载），故用 `.abs()` 修正。这依赖「真实时区的分钟与小时同号」假设，所有现实时区都满足。
- **问题二：`-23..=23` 与 `0..=59` 的依据？** 偏移绝对值须严格小于 24 小时（±24:00 等价于换天零偏移，非合法独立偏移），分钟须小于 60。代码取「表示合法」而非「现实存在」为上限，既挡住 `1440` 这类无意义值，又不误伤任何合理偏移。
- **问题三：用户既没设 `document.date` 也没传 `timestamp` 时，PDF 里是否有创建日期？** **没有。** 此时 `document.info().date` 为 `Smart::Auto`、`options.timestamp` 为 `None`，`creation_date` 的 `match` 落入 `_ => return None`（见 [metadata.rs:60-64](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/metadata.rs#L60-L64)），PDF 的信息字典里不会写入 `CreationDate` 字段。

> 本实践为「源码阅读 + 手算」型，不依赖运行环境。若想运行测试确认，可在仓库根目录执行 `cargo test -p typst-pdf test_timestamp_new_local`。

#### 4.3.5 小练习与答案

**练习 1**：`new_local(dt, -300)` 会得到什么 `Timezone`？

**参考答案**：`-300/60 = -5`，`(-300 % 60) = 0`，`.abs() = 0`，落在 `(-23..=23, 0..=59)` 内，得到 `Local { hour_offset: -5, minute_offset: 0 }`（即 UTC-5，如美东标准时间）。

**练习 2**：`new_local(dt, 600)` 呢？为什么不会因为 `600` 超过现实最大偏移而被拒绝？

**参考答案**：`600/60 = 10`，`600%60 = 0`，得到 `Local { 10, 0 }`（UTC+10）。因为代码只校验「表示是否合法」（小时在 ±23 内、分钟在 0..59 内），不校验「是否是现实存在的时区」。`10` 在 `-23..=23` 内，故被接受。

**练习 3**：`minute_offset` 字段为何是 `u8` 而 `hour_offset` 是 `i8`？

**参考答案**：约定「符号由小时统一承载」，分钟永远是绝对值（0..59），所以用无符号 `u8`；小时需要表示正负偏移，故用有符号 `i8`。这个设计使得 `.abs()` 成为必要操作，也使得 `new_local` 必须保证两者同号（注释中明示的假设）。

## 5. 综合实践

把本讲三个模块串起来，完成一次「端到端元数据追踪」。

**任务**：假设你要让导出的 PDF 同时满足：创建者为 `"Acme Publisher"`、文档 ID 为 `"stable-id-42"`、创建日期为 `2024-12-17 10:10:10 (UTC+8)`、语言为中文。请回答下列问题并给出 Typst 源码与 `PdfOptions` 的配置思路。

1. **创建者**：`PdfOptions.creator` 应填什么？（答：`Smart::Custom(Some("Acme Publisher".into()))`。）
2. **文档 ID**：`PdfOptions.ident` 应填什么？（答：`Smart::Custom("stable-id-42".into())`，这样 `build_metadata` 才会走 `document_id` 分支。）
3. **创建日期**：应通过 `set document(date: ..)` 还是 `PdfOptions::timestamp` 设置？为什么？（答：用 `PdfOptions::timestamp = Some(Timestamp::new_local(dt, 480))`。因为中文场景需带时区，而 `set document(date:)` 设的 `Datetime` 不带时区；同时必须确保源码里**没有** `set document(date: ..)` 或写的是 `date: auto`，否则 `document.date` 会变成 `Custom` 而覆盖 `timestamp`、且丢失时区。）
4. **语言**：`doc_lang` 由 tag 树解析得到，对应源码里的 `set text(lang: "zh")`。若完全不设语言，`build_metadata` 会回退到 `Locale::DEFAULT`（英语）。

**跟踪验证**：对照 [build_metadata](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/metadata.rs#L9-L53) 与 [creation_date](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/metadata.rs#L59-L99)，逐个字段确认你的配置会命中哪个分支。这能帮你建立「字段来源 → 分支选择」的完整心智模型。

## 6. 本讲小结

- `build_metadata()` 是纯字段映射：标题/作者/关键词/描述/日期来自 `document.info()`，创建者/标识/时间戳来自 `PdfOptions`，语言/书写方向来自 `tags::resolve` 产出的 `doc_lang`。
- **语言永远写入**（缺省回退 `Locale::DEFAULT`），因为 PDF/UA-1 隐式要求；其余字段缺省即不写。
- `creation_date()` 是三级回退：`document.date` 为 `Custom(Some)` 时用它（无时区）；为 `Auto` 且 `options.timestamp` 存在时用时间戳（带时区）；为 `Custom(None)`（显式 none）或两者皆空时**不写日期**。
- 显式的 `set document(date: none)` 不会被 `PdfOptions::timestamp` 覆盖——文档作者意图优先。
- `Timestamp` / `Timezone` 是对外公开类型；`new_local` 的 `minute_offset` 取 `.abs()` 是因为 Rust `%` 是求余（与被除数同号），而分钟约定由小时统一承载符号。
- 时区偏移边界 `-23..=23` / `0..=59` 校验的是「表示合法」（偏移绝对值严格小于 24 小时），而非「现实存在」，从而既挡住无意义值又不误伤合理偏移。

## 7. 下一步学习建议

本讲是「文档级特性」单元（u4）的第三讲。建议按顺序继续：

- **u4-l17 文件附件**：`attach_files()` 与本讲的 `creation_date()` 共享同一个日期来源（附件的 `modification_date` 复用 `metadata::creation_date`），可以连贯阅读。
- **u5-l18 错误处理与校验结果映射**：本讲提到的 `document_id` 缺省时由 krilla 派生、`CreationDate` 格式等，在 PDF/A、PDF/UA 校验失败时会产生 `ValidationError`，下一专家单元会讲这些校验如何映射回 Typst 诊断。
- 若想深入「语言如何从结构树提取」，直接跳到 **u5-l23 解析为 TagTree 与 PDF/UA 结构校验**，那里解释 `tags::resolve` 如何产出 `doc_lang`。
