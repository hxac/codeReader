# 公共 API：`pdf()`、`pdf_in_bundle()` 与 `PdfOptions`

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 `typst-pdf` 对外暴露的两个入口函数 `pdf()` 与 `pdf_in_bundle()` 的签名差异，并能判断在「独立导出」和「打包导出」两种场景下该用哪一个。
- 逐字段解释 `PdfOptions` 中七个字段（`ident`、`creator`、`timestamp`、`page_ranges`、`standards`、`tagged`、`pretty`）的类型、默认值和作用。
- 看懂 `PdfOptions::default()` 的默认配置含义，并能预测「把 `tagged` 设为 `false`、`pretty` 设为 `true`」之后输出 PDF 的差异。
- 理解 `pdf_in_bundle()` 中 `anchors` 与 `link_resolver` 这两个额外参数在跨文档链接中的作用。

本讲只聚焦**公共 API 表面（API surface）**：函数签名、参数语义、默认值。至于 `PdfOptions.standards` 字段背后复杂的 PDF/A、PDF/UA 配置，会在下一讲（u1-l3）专门展开。

## 2. 前置知识

承接上一讲（u1-l1），我们已建立这样一个心智模型：

```
Frame 树  ──typst-pdf 翻译──▶  krilla 调用  ──krilla finish()──▶  PDF 字节
```

本讲你需要补充几个 Rust 与 Typst 工程的基础概念：

- **入口函数（entry function）**：一个 crate 对外公开、供别人调用的最外层函数。`typst-pdf` 只公开了两个这样的函数，都集中在 `src/lib.rs`。
- **`SourceResult<T>`**：Typst 自定义的返回类型，本质是「要么返回 `T`，要么返回一组带源码定位（span）的诊断错误」。你可以把它理解成 `Result<T, 错误列表>`。当导出失败（比如 PDF 标准冲突、图像不支持透明度）时，它会精确告诉你是哪一段源码触发的。
- **`Smart<T>`**：Typst 里一个很常见的枚举，有两个值：`Smart::Auto`（让程序自动决定）和 `Smart::Custom(value)`（用户显式指定）。`PdfOptions` 里有多个字段用了它。
- **`#[typst_macros::time(name = "...")]`**：一个属性宏，用于给函数包一层**计时埋点**，方便 Typst 在编译报告里显示「PDF 导出花了多少毫秒」。它不影响函数逻辑，你暂时可以忽略。

如果你对 Rust 的 `Option`、`Default` trait 不熟悉，只需要知道：`Option<T>` 表示「可能有值也可能没有」（类似「可空」），`#[derive(Default)]` 或手写的 `Default` 实现给出「什么都不特别指定时的默认取值」。

## 3. 本讲源码地图

本讲只涉及一个源码文件（外加一个对照文件）：

| 文件 | 作用 |
|------|------|
| [src/lib.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs) | 本讲主角。存放两个入口函数 `pdf()`、`pdf_in_bundle()`，以及配置结构 `PdfOptions` 和它的 `Default` 实现。 |
| [src/convert.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs) | 编排核心 `convert::convert()`。两个入口函数最终都汇聚到这里，本讲用它来验证「`PdfOptions` 的字段如何真正影响输出」。 |
| [src/metadata.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/metadata.rs) | 用来说明 `creator`、`timestamp` 两个字段的实际下游用法。 |

记住一句话：**`lib.rs` 是「门面」，`convert.rs` 是「后厨」**。门面负责收参数、转交后厨；真正的翻译工作都在后厨完成。

## 4. 核心概念与源码讲解

### 4.1 `pdf()`：独立导出入口

#### 4.1.1 概念说明

`pdf()` 是最常见的入口：你有一份排版好的 Typst 文档（`PagedDocument`），想把它单独导出成一个 PDF 文件，就用它。它是「**独立导出**」场景的唯一入口——CLI 里敲 `typst compile doc.typ`（输出 PDF）走的就是这条路。

它的设计哲学是**极简委托**：自己几乎不写逻辑，只做两件事——

1. 把参数原样收下；
2. 调用编排核心 `convert::convert()`，并把「跨文档链接」相关的两个参数填成「空 / 没有」。

#### 4.1.2 核心流程

```
调用方
  │  document: &PagedDocument, options: &PdfOptions
  ▼
pdf()                              ← src/lib.rs:36-38
  │  把 anchors 传成 &[]（空）
  │  把 link_resolver 传成 None
  ▼
convert::convert(document, options, &[], None)   ← src/convert.rs:48-95
  │
  ▼
返回 SourceResult<Vec<u8>>   （成功 = PDF 字节，失败 = 诊断错误）
```

关键点：`pdf()` 比起 `pdf_in_bundle()`，**少了**「命名目的地址（anchors）」和「跨文档链接解析器（link_resolver）」这两样东西，所以它不能被别的文档反向链接进来，也不能解析指向别的文档的链接——这正是「独立」二字的含义。

#### 4.1.3 源码精读

入口函数本身非常短，值得逐行看：

[src/lib.rs:32-38](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs#L32-L38) — 定义 `pdf()`，函数体只有一行委托调用。其中 `#[typst_macros::time(name = "pdf")]` 是计时埋点；真正的逻辑是 `convert::convert(document, options, &[], None)`，注意它把第三参数 `anchors` 传成了空切片 `&[]`、第四参数 `link_resolver` 传成了 `None`。

下面这行是委托的目标，它的四个参数决定了整个导出行为：

[src/convert.rs:48-53](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L48-L53) — `convert::convert()` 的签名。第三参数 `anchors: &[(Location, EcoString)]` 是「哪些位置要作为命名目的地址暴露给外部文档」；第四参数 `link_resolver` 是「如何解析跨文档链接」。`pdf()` 把它们填成空和 `None`，等于声明「这次导出不参与跨文档链接」。

返回类型 `SourceResult<Vec<u8>>` 的含义：成功时 `Vec<u8>` 就是 PDF 文件的原始字节；失败时是一组带源码 span 的 `SourceDiagnostic`，调用方可以据此向用户报告是哪一行出问题。

#### 4.1.4 代码实践

**实践目标**：确认 `pdf()` 只是一个「薄薄的转发层」。

**操作步骤**：

1. 打开 [src/lib.rs:36-38](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs#L36-L38)。
2. 数一数它的函数体有几行（除 `#[time]` 属性外）。
3. 对照 [src/convert.rs:48-53](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L48-L53)，把 `pdf()` 实参「填空」式地映射到 `convert()` 的形参上。

**需要观察的现象**：`pdf()` 的实参 `&[]` 对应 `convert()` 形参 `anchors`；`None` 对应 `link_resolver`。

**预期结果**：你会发现 `pdf()` 完全没有自己的分支判断或循环，就是一次纯转发。这正是适配器层「门面尽量薄」的体现。

（本实践为源码阅读型实践，无需运行命令。）

#### 4.1.5 小练习与答案

**练习 1**：如果调用 `pdf()` 后导出失败，返回的错误里会包含什么信息？

**参考答案**：返回 `Err(EcoVec<SourceDiagnostic>)`，即一组诊断错误，每条通常带有源码 `Span`（定位到 Typst 源码的某一段）和提示文本（hint）。例如透明度不兼容时会指出具体哪张图。

**练习 2**：为什么 `pdf()` 不需要 `anchors` 和 `link_resolver`？

**参考答案**：因为它是「独立导出」，产出的 PDF 既不需要被别的文档通过命名目的地址反向链接（所以 anchors 为空），也不需要解析指向别的文档的链接（所以 link_resolver 为 `None`）。这两个能力只在「打包多个文档」时才有意义，由 `pdf_in_bundle()` 提供。

---

### 4.2 `pdf_in_bundle()`：打包导出与跨文档链接

#### 4.2.1 概念说明

当你不是单独导出一个 PDF，而是把**多个文档一起打包**（bundle）时，就需要文档之间能互相链接——比如文档 A 里有个链接，点了跳到文档 B 的某个章节。`pdf_in_bundle()` 就是为这种场景准备的入口。

它比 `pdf()` 多两个参数：

- **`anchors`**：一组「位置 → 名字」的映射。它告诉导出器「这些位置要作为**命名目的地址（named destination）**序列化进 PDF」。这样别的文档就能通过名字精确链接到本 PDF 的某个位置。
- **`link_resolver`**：一个**跨文档链接解析器**（`LateLinkResolver`），用来在导出时把指向其它文档的链接解析成真实目标。

#### 4.2.2 核心流程

```
调用方
  │  document, options, anchors, link_resolver
  ▼
pdf_in_bundle()                    ← src/lib.rs:47-54
  │  把 anchors 原样透传
  │  把 link_resolver 包成 Some(...)
  ▼
convert::convert(document, options, anchors, Some(link_resolver))
  │
  ├── collect_named_destinations(...anchors...)   ← 把 anchors 写成 PDF 命名目的地址
  ├── GlobalContext 里携带 link_resolver          ← 解析跨文档链接时用
  ▼
返回 SourceResult<Vec<u8>>
```

和 `pdf()` 唯一的区别就是最后两个参数「有值」：`anchors` 不再是 `&[]`，`link_resolver` 不再是 `None`。这也意味着在 `convert()` 内部，`collect_named_destinations()` 会真正把这些命名目的地址写进 PDF，而 `GlobalContext` 会持有 `link_resolver` 供 `handle_link()` 解析跨文档链接时调用。

#### 4.2.3 源码精读

[src/lib.rs:40-54](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs#L40-L54) — `pdf_in_bundle()` 的文档注释与定义。注释明确说明了 `anchors`「将被序列化为命名目的地址，使 bundle 中其它文档能链接进本 PDF」，`link_resolver`「用于解析跨文档链接」。函数体仍是单行委托：`convert::convert(document, options, anchors, Some(link_resolver))`。

注意第四参数包成了 `Some(link_resolver)`，而 `pdf()` 里是 `None`——这正是两个入口在代码上的**唯一**差异。

签名里有两个类型值得留意：

- `anchors: &[(Location, EcoString)]`：一个元组切片，`Location` 是 Typst 内部的「文档内位置锚点」，`EcoString` 是给这个锚点起的名字。
- `link_resolver: Tracked<LateLinkResolver>`：`Tracked<...>` 是 comemo（Typst 的记忆化框架）的包装类型，表示「一个被追踪的、可参与增量编译的解析器」。「Late」表示这是一种**延迟解析**——在导出阶段才真正去解析跨文档链接。

#### 4.2.4 代码实践

**实践目标**：体会两个入口函数的「对称性」——参数数量不同，但都汇聚到同一个 `convert()`。

**操作步骤**：

1. 并排打开 [src/lib.rs:36-38](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs#L36-L38)（`pdf`）和 [src/lib.rs:47-54](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs#L47-L54)（`pdf_in_bundle`）。
2. 找出两者调用 `convert::convert()` 时，第三、第四参数的不同。

**需要观察的现象**：除了多两个形参，两者的函数体结构几乎一模一样。

**预期结果**：你会确认「独立导出 vs 打包导出」在源码层面就是 `(&[], None)` 与 `(anchors, Some(link_resolver))` 的区别，没有任何额外的特殊逻辑。

（本实践为源码阅读型实践。）

#### 4.2.5 小练习与答案

**练习 1**：如果只想导出单个 PDF，但误用了 `pdf_in_bundle()` 并传入空的 `anchors` 和一个空的 `link_resolver`，结果会和 `pdf()` 一样吗？

**参考答案**：基本等价。此时第三参数 `&[]` 与 `pdf()` 相同，第四参数虽是 `Some(...)` 而非 `None`，但如果没有任何跨文档链接需要解析，`link_resolver` 不会被真正调用，最终产物与 `pdf()` 一致。区别仅在于 `GlobalContext` 多携带了一个解析器引用，不影响输出字节。

**练习 2**：`Tracked<LateLinkResolver>` 里的「Late」暗示了什么？

**参考答案**：暗示这是**延迟解析**——跨文档链接的目标在排版阶段往往还不能确定（要等所有文档都排完），所以推迟到 PDF 导出阶段才用 `link_resolver` 去解析。这与命名目的地址的「先收集位置、后解析」是配套设计。

---

### 4.3 `PdfOptions` 配置对象与 `Default` 默认值

#### 4.3.1 概念说明

`PdfOptions` 是整个导出行为的**控制面板**：上面每一个开关（字段）都对应输出 PDF 的某一方面。无论你用 `pdf()` 还是 `pdf_in_bundle()`，都得传一个 `&PdfOptions` 进来。

它是一个普通的 `pub struct`，所有字段都是 `pub`——这意味着调用方可以直接构造它、直接修改某个字段，不需要走 builder 模式。它派生了 `Debug` 和 `Hash`：`Debug` 方便调试打印，`Hash` 让 Typst 的增量编译能判断「选项没变就复用上次的 PDF 结果」。

设计上有个重要约定：**当你不确定某字段该填什么时，就用 `Default::default()` 拿到的默认值**。默认值是经过精心选择的「安全且通用」配置。

#### 4.3.2 核心流程：七个字段一览

下表汇总了 `PdfOptions` 的全部字段（定义见 [src/lib.rs:57-89](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs#L57-L89)）：

| 字段 | 类型 | 默认值 | 作用 |
|------|------|--------|------|
| `ident` | `Smart<String>` | `Smart::Auto` | 文档稳定标识符。`Auto` 时用「标题+作者」的哈希；`Custom(s)` 时用 `s` 的哈希生成 PDF 文档 ID。 |
| `creator` | `Smart<Option<String>>` | `Smart::Auto` | PDF `/Creator` 元数据。`Auto` 默认为 `Typst $version`；`Custom(None)` 表示不写。 |
| `timestamp` | `Option<Timestamp>` | `None` | 创建时间戳。**仅当** `set document(date: ..)` 为 `auto` 时才使用。 |
| `page_ranges` | `Option<PageRanges>` | `None` | 导出哪些页。`None` = 全部页面。 |
| `standards` | `PdfStandards` | `PdfStandards::default()` | 要遵循的 PDF 标准。默认 = PDF 1.7，不带任何校验器。 |
| `tagged` | `bool` | `true` | 是否生成 tagged PDF（无障碍结构树）。`false` 可减小体积。 |
| `pretty` | `bool` | `false` | 是否人类可读格式（不压缩、ASCII 兼容）。默认 `false` = 压缩输出。 |

#### 4.3.3 源码精读

**字段定义**——每个字段的文档注释就是最好的语义说明：

[src/lib.rs:56-89](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs#L56-L89) — `PdfOptions` 结构体定义与逐字段注释。注意几个细节：`ident` 的注释特别强调「**如果你拿不出稳定标识符，就直接传 `Auto`，别硬编一个**」（L60-64）；`creator` 注释说明 `Auto` 默认 `Typst $version`（L71-72）；`timestamp` 注释说明它「只在 `document.date` 为 `auto` 时使用」（L74-76）；`tagged` 注释说明默认会写 tagged PDF 以提供基础无障碍能力，关闭可减小体积（L82-86）。

**默认值实现**——这是本讲最该记住的一段：

[src/lib.rs:99-111](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs#L99-L111) — `impl Default for PdfOptions`。默认配置为：`ident: Auto`、`creator: Auto`、`timestamp: None`、`page_ranges: None`、`standards: PdfStandards::default()`、`tagged: true`、`pretty: false`。

**默认标准是什么**——`PdfStandards::default()` 的真实取值：

[src/lib.rs:250-260](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs#L250-L260) — `impl Default for PdfStandards`。默认用 `ConfigurationBuilder` 设置 `PdfVersion::Pdf17` 且不带任何归档/无障碍校验器。也就是说默认产出 **PDF 1.7** 文件，不做 PDF/A、PDF/UA 强校验（但 `tagged: true` 仍会写结构树）。

**字段如何影响真实输出**——以 `creator` 和 `timestamp` 为例，它们最终在 `metadata.rs` 被消费：

[src/metadata.rs:25-32](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/metadata.rs#L25-L32) — `creator` 字段的实际消费处。当 `creator` 为 `Auto` 时，`unwrap_or_else` 兜底成 `Some(format!("Typst {}", typst_utils::version().raw()))`，即「Typst + 版本号」；为 `Custom(None)` 时则整个不写 creator。这印证了注释里「默认 `Typst $version`」的说法。

[src/metadata.rs:59-64](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/metadata.rs#L59-L64) — `timestamp` 字段的优先级逻辑。匹配 `(document.info().date, options.timestamp)`：当 `document.date` 是 `Custom(Some(date))` 时直接用文档日期（忽略 options）；只有当 `document.date` 为 `Auto` 且 `options.timestamp` 为 `Some(..)` 时才用 options 的时间戳；其余情况返回 `None`（不写创建日期）。这说明 `timestamp` 是「文档未指定日期时的兜底」。

**`pretty` 与 `tagged` 如何影响序列化设置**——这两个 bool 直接进入 krilla 的 `SerializeSettings`：

[src/convert.rs:54-64](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L54-L64) — `SerializeSettings` 的构造。`compress_content_streams: !options.pretty`（pretty=true 则不压缩）、`ascii_compatible: options.pretty`、`enable_tagging: options.tagged`、`pretty: options.pretty`。这一段是 `PdfOptions` 字段「流入」PDF 字节的咽喉要道。

> 小贴士：本讲我们只看 `standards` 字段「默认是 PDF 1.7、不带校验器」这一点。至于如何用 `PdfStandards::new()` 组合出 PDF/A、PDF/UA 校验，以及冲突如何报错，是下一讲（u1-l3）的主题。

#### 4.3.4 代码实践

**实践目标**：动手构造 `PdfOptions`，打印默认值，并预测修改 `tagged`/`pretty` 后的输出差异。

**操作步骤**：

1. 在一个已把 `typst-pdf` 加为依赖的 crate 里（或在 `typst-pdf` 自身的测试模块里），写一段最小示例（**示例代码**，非项目原有代码）：

   ```rust
   use typst_pdf::PdfOptions;

   // 1) 看默认值
   let opts = PdfOptions::default();
   println!("{:#?}", opts);

   // 2) 关掉 tagged、打开 pretty，预测输出差异
   let mut opts = PdfOptions::default();
   opts.tagged = false;
   opts.pretty = true;
   // 此后用 pdf(document, &opts) 导出……
   ```

2. 对照 [src/lib.rs:99-111](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs#L99-L111)，逐字段核对打印出的默认值是否与 `Default` 实现一致（注意 `standards` 字段因为自定义了 `Debug`，只会显示成 `PdfStandards(..)`，这是正常的——见 [src/lib.rs:244-248](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs#L244-L248)）。

**需要观察的现象**：

- 默认打印应显示 `ident: Auto`、`creator: Auto`、`timestamp: None`、`page_ranges: None`、`tagged: true`、`pretty: false`。
- 把 `tagged=false` 后，输出的 PDF 体积通常会变小（没有了逻辑结构树）；用屏幕阅读器或 PAC 之类工具检查时，会发现没有结构树。
- 把 `pretty=true` 后，PDF 内容流**不再压缩**且**ASCII 兼容**，体积变大，但用文本编辑器打开能勉强看到对象结构。

**预期结果**：`pretty=true` 让 `compress_content_streams` 变为 `false`（见 [src/convert.rs:55](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L55)），所以体积增大、可读性提高；`tagged=false` 让 `enable_tagging` 为 `false`（见 [src/convert.rs:61](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L61)），所以不写结构树、体积减小。

> 说明：要让上面这段示例真正编译运行，需要先有 `typst-pdf` 依赖和一个 `PagedDocument`（后者来自 `typst-layout`，构造较重）。如果环境不具备，可退化为「源码阅读型实践」：只做第 1、2 步的对照，不实际导出。能否在你的本地完整跑通属于**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：用户在 Typst 源码里写了 `#set document(date: datetime(year: 2024))`，同时在 `PdfOptions.timestamp` 里传了一个 `Some(...)` 时间戳。最终 PDF 里的创建日期用哪个？

**参考答案**：用源码里 `#set document(date: ...)` 指定的 2024 年。根据 [src/metadata.rs:60-64](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/metadata.rs#L60-L64)，匹配 `(Smart::Custom(Some(date)), _)` 这个分支优先，`options.timestamp` 被忽略。`timestamp` 只是「文档日期为 auto 时的兜底」。

**练习 2**：为什么 `PdfOptions` 要派生 `Hash`？

**参考答案**：Typst 使用 comemo 做增量编译的记忆化。把 `PdfOptions`（连同文档）作为缓存键的一部分，可以判断「文档没变、选项也没变」时直接复用上次导出的 PDF，避免重复工作。注意 `PdfStandards` 因为内部的 krilla `Configuration` 不实现 `Hash`，所以单独手写了 `Hash`（见 [src/lib.rs:264-271](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/lib.rs#L264-L271)），用「版本号 + 校验器列表」来近似哈希。

**练习 3**：`creator` 字段是 `Smart<Option<String>>`，这有三层。请说明 `Smart::Auto`、`Smart::Custom(Some("X"))`、`Smart::Custom(None)` 各自的效果。

**参考答案**：`Smart::Auto` → 用默认 `Typst $version`（见 [src/metadata.rs:25-32](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/metadata.rs#L25-L32)）；`Smart::Custom(Some("X"))` → `/Creator` 写成 `X`；`Smart::Custom(None)` → 整个不写 `/Creator` 字段。

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个「参数追踪」小任务：

**任务**：假设你要写一个「把 Typst 文档导出为**体积最小**的 PDF（用于网页下载），同时**不需要无障碍**」的导出器。

1. 请你确定该用 `pdf()` 还是 `pdf_in_bundle()`？并说明理由。
   （提示：独立导出、不参与跨文档链接。）
2. 请你给出一个合适的 `PdfOptions` 构造：哪些字段相对默认值需要改？为什么？
   （提示：目标是「体积最小 + 不需无障碍」，结合 `tagged`、`pretty` 字段，参考 [src/convert.rs:54-64](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L54-L64)。）
3. 写出最终调用入口函数的那一行代码（伪代码即可），并把它的四个实参分别对应到 `convert::convert()` 的形参上。
   （提示：参考 [src/convert.rs:48-53](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L48-L53)。）

**参考思路**：

1. 用 `pdf()`——单文档、不跨文档链接，无需 `anchors` / `link_resolver`。
2. 相对默认值，应把 `tagged` 改为 `false`（去掉结构树减小体积）、`pretty` 保持 `false`（保持压缩）。`ident`/`creator`/`timestamp`/`page_ranges`/`standards` 维持默认即可。
3. 伪代码：

   ```rust
   let mut opts = PdfOptions::default();
   opts.tagged = false;          // 体积优先，关闭无障碍结构树
   // pretty 保持 false：compress_content_streams 仍为 true
   let bytes = pdf(&document, &opts)?;
   // 对应：convert::convert(&document, &opts, &[], None)
   ```

## 6. 本讲小结

- `typst-pdf` 对外只有两个入口：`pdf()`（独立导出）与 `pdf_in_bundle()`（打包导出），二者函数体都是一行对 `convert::convert()` 的委托，区别只在 `anchors`（`&[]` vs 有值）和 `link_resolver`（`None` vs `Some`）。
- 两个入口都返回 `SourceResult<Vec<u8>>`：成功即 PDF 字节，失败即带 span 的诊断错误。
- `PdfOptions` 是导出行为的「控制面板」，七个字段全部 `pub`，可直接构造与修改，并派生了 `Debug`/`Hash`（`Hash` 服务于增量编译缓存）。
- 默认配置（`PdfOptions::default()`）的关键取值：`ident: Auto`、`creator: Auto`（实际为 `Typst $version`）、`timestamp: None`、`page_ranges: None`、`standards` 为 PDF 1.7 无校验器、`tagged: true`、`pretty: false`。
- `timestamp` 是「文档日期为 auto 时的兜底」，`creator` 为 `Auto` 时兜底成 `Typst $version`，二者优先级都低于文档自身的设置。
- `pretty` 通过 `SerializeSettings` 控制「是否压缩 / ASCII 兼容」，`tagged` 控制「是否生成无障碍结构树」——这是 `PdfOptions` 字段影响最终 PDF 字节的关键咽喉（[src/convert.rs:54-64](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L54-L64)）。

## 7. 下一步学习建议

本讲我们刻意回避了 `PdfOptions.standards` 字段的深度内容。下一讲 **u1-l3《PDF 标准与校验配置：PdfStandards 与 PdfStandard》** 会专门讲解：

- `PdfStandard` 枚举的各个变体（PDF 1.4–2.0、PDF/A 各级、PDF/UA-1）；
- `PdfStandards::new()` 如何把多个标准组装成 krilla 的 `Configuration`，并检测版本/校验器冲突；
- 为什么「同时指定 PDF/A-1a 和 PDF/A-2b」会报错。

如果想提前建立对「后厨」的直觉，也可以先跳读 [src/convert.rs:48-95](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L48-L95) 的 `convert()` 函数——那是本讲两个入口函数共同的终点，也是整个 crate 的编排核心，会在 u2-l5 中逐段拆解。
