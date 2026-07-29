# Bundle 的数据模型：Bundle / BundleFile / BundleDocument / PagedExtras

## 1. 本讲目标

前面几讲我们一直在讲 bundle 的**流程**——从 `bundle_impl` 的实现化、校验，到 `compile_document` 的格式推断与分派。流程是「动词」，本讲我们把镜头切换到「名词」：bundle 编译出来之后，产物到底长什么样？它在内存里是用哪些数据结构表示的？

读完本讲你应该能够：

- 说清楚 `Bundle` 这个顶层产物对象的两个字段 `files` 与 `introspector` 各自的类型与作用，并理解为什么它们都被 `Arc` 包裹。
- 看懂 `BundleFile`（`Document` / `Asset`）与 `BundleDocument`（`Paged` / `Html`）这两层枚举的层级关系，以及它们如何对应到用户写的 `#document(...)` 与 `#asset(...)`。
- 解释 `PagedExtras` 为什么只挂在 `Paged` 变体上、`Html` 变体为什么不需要它，从而理解「格式区分」与「跨文档命名锚点」这两个职责的来源。

本讲只读「静态数据结构」，不再追编译流程。流程细节请回顾 u2-l1、u2-l2。

---

## 2. 前置知识

本讲承接 u1-l2（目录结构与编译入口），假定你已经知道：

- Typst 有三种编译目标 `Target`：`Paged`、`Html`、`Bundle`，与 `Output` trait 一一对应；`compile::<Bundle>(world)` 最终会走到带 `#[comemo::memoize]` 的 `bundle_impl`。
- `bundle_impl` 在并行编译完各文档后，会把结果装进一个 `Bundle { files, introspector }` 返回（u2-l1）。
- `compile_document` 会为每个 `#document(...)` 产出 `BundleDocument::Paged(.., PagedExtras)` 或 `BundleDocument::Html(..)` 两种形态之一（u2-l2）。

此外，本讲会用到几个 Rust 通用概念，初学者可以先这样理解：

- **`enum`（枚举）**：一个「多选一」的类型。比如 `BundleFile` 要么是 `Document`，要么是 `Asset`，二者居其一。
- **`Box<T>`**：把一个值放到**堆**上（而不是栈上），并用一个指针指向它。当一个枚举的某个变体很大时，用 `Box` 包起来能让整个枚举的体积保持紧凑。
- **`Arc<T>`**：**原子引用计数**的智能指针，让多个所有者**共享同一份不可变数据**。`Clone` 一个 `Arc` 只是增加计数，代价很小。
- **`IndexMap<K, V>`**：一个「**保留插入顺序**」的键值表。它既是字典（按键查值），又能记住「先插入谁、后插入谁」的顺序；普通 `HashMap` 不保证顺序。
- **`Bytes`**：Typst 自带的不可变字节序列类型（共享、廉价克隆），适合表示文件原始内容。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/typst-bundle/src/lib.rs` | 定义本讲的全部核心数据结构：`Bundle`、`BundleFile`、`BundleDocument`、`PagedExtras`，以及 `impl Output for Bundle`、`impl Document for BundleDocument`。本讲的主战场。 |
| `crates/typst-library/src/model/document.rs` | 定义用户侧的 `DocumentElem`、格式枚举 `DocumentFormat` / `PagedFormat`、以及 `Document` trait 与 `DocumentInfo`。理解数据模型「对应到用户写了什么」必须读它。 |
| `crates/typst-library/src/model/asset.rs` | 定义用户侧的 `AssetElem`，对应 `BundleFile::Asset`。 |
| `crates/typst-bundle/src/introspect.rs` | 定义 `BundleIntrospector`——`Bundle.introspector` 字段的真实类型。本讲只看它的结构声明，内省查询细节留到 u5-l1。 |
| `crates/typst-bundle/src/link.rs` | 定义 `create_link_anchors`，揭示 `PagedExtras.anchors` 是如何被填充、以及 HTML 为什么不需要它。 |

---

## 4. 核心概念与源码讲解

### 4.1 Bundle 顶层结构：files + introspector

#### 4.1.1 概念说明

「一次编译，一个产物」是单文件目标（pdf/png/svg）的心智模型：编译完拿到一个 `PagedDocument`，导出成一个文件。bundle 打破了这个限制——一次编译可能产出**好几个文件**（多个文档 + 若干原始 asset）。

所以 bundle 的顶层产物 `Bundle` 必须能同时回答两个问题：

1. **有哪些文件？** → 用一个「路径 → 文件内容」的映射来表示，这就是 `files` 字段。
2. **这些文件之间如何互相感知？** bundle 里的多个文档可以互相 `#link`、互相 `query`，它们共享**同一个内省循环**（见 u2-l1 的「统一内省器」）。承载这个「全局视图」的就是 `introspector` 字段。

源码顶部的文档注释把 `Bundle` 与 `PagedDocument` 做了类比：`Bundle` 之于 `bundle` 输出格式，就像 `PagedDocument` 之于 `pdf` / `png` / `svg` 输出。

#### 4.1.2 核心流程

`Bundle` 是 `bundle_impl` 的最终返回值。它的构造发生在 `bundle_impl` 的末尾，可以用下面这段伪代码概括：

```
# bundle_impl 末尾
files = IndexMap::default()
for item in items:                       # items 是编译+并行处理后的结果
    match item:
        Tag(_)      => 丢弃                # Tag 不产生文件
        Asset(path, bytes, _)  => files[path] = BundleFile::Asset(bytes)
        Document(path, doc, _) => files[path] = BundleFile::Document(doc)

return Bundle {
    files: Arc::new(files),
    introspector: Arc::new(introspector),  # 早先已用 BundleIntrospector::new(&items) 建好
}
```

注意三点：

- `files` 的键是 `VirtualPath`（bundle 内的相对输出路径），值是 `BundleFile`。
- **`Tag` 不会落地成文件**——它只是内省用的标记，所以循环里直接跳过。
- `introspector` 在装填 `files` **之前**就已经建好（`BundleIntrospector::new(&items)`），并且已经 `set_anchors`，是一个完全就绪的内省器。

#### 4.1.3 源码精读

先看 `Bundle` 结构体本身：

[crates/typst-bundle/src/lib.rs:44-54](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L44-L54)：`Bundle` 只有两个字段。`files` 是 `Arc<IndexMap<VirtualPath, BundleFile, FxBuildHasher>>`——`IndexMap` 保留插入顺序（让输出文件顺序可预测、便于确定性构建），`FxBuildHasher` 是 Typst 通用的快速哈希器；`introspector` 是 `Arc<BundleIntrospector>`。两者都用 `Arc` 包裹，使 `Bundle` 可以被廉价地 `Clone` 并在导出阶段被多个消费者共享。

再看 `Bundle` 如何实现 `Output` trait：

[crates/typst-bundle/src/lib.rs:56-72](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L56-L72)：`impl Output for Bundle` 实现了三个方法。`target()` 返回 `Target::Bundle`（这正是 `Output` 与 `Target` 一一对应的体现，回顾 u1-l2）；`introspector()` 把内部的 `BundleIntrospector` 作为 `&dyn Introspector` 暴露给上层（用于内省循环的收敛判定）；`create()` 把编译工作转交给 `bundle()` 函数——它本身不做编译，只做分发。

最后看 `bundle_impl` 末尾如何把 `items` 装进 `files` 并构造 `Bundle`：

[crates/typst-bundle/src/lib.rs:202-218](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L202-L218)：这段循环把三类 `Item` 分别处理——`Tag` 跳过，`Asset` 写成 `BundleFile::Asset(bytes)`，`Document` 写成 `BundleFile::Document(doc)`，最后用 `Arc::new` 包裹 `files` 和 `introspector` 拼出 `Bundle` 返回。这也回答了一个常见疑惑：**`Bundle` 本身不存「文件列表的顺序」之外的任何东西，所有格式与锚点信息都嵌在 `BundleFile` / `BundleDocument` 内部**（见 4.2、4.3）。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是建立「`bundle_impl` 末尾如何组装 `Bundle`」的直觉。

1. **实践目标**：确认 `Bundle` 的两个 `Arc` 字段是在哪两行被 `Arc::new` 包裹的，以及 `introspector` 是在装填 `files` 之前还是之后创建的。
2. **操作步骤**：
   - 打开 [crates/typst-bundle/src/lib.rs:197-218](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L197-L218)。
   - 找到 `BundleIntrospector::new(&items)`、`link_targets()`、`create_link_anchors(...)`、`set_anchors(...)` 这四步的顺序。
   - 再找到 `for item in items` 装填 `files` 的循环。
3. **需要观察的现象**：`introspector` 在 `set_anchors` 之后已经「定型」，而 `files` 的装填发生在它之后、且不依赖锚点。
4. **预期结果**：你会看到 `BundleIntrospector` 的创建与锚点设置构成了一个独立的「内省阶段」，与「文件收集阶段」是**先后两段**——这正是后续 u5 讲「并行编译 → 统一内省」顺序的数据层体现。
5. 如果你无法本地运行，明确标注「待本地验证」相关断点观察。

#### 4.1.5 小练习与答案

**练习 1**：`Bundle` 的两个字段各自是什么类型？为什么都用 `Arc` 包裹？

> **参考答案**：`files: Arc<IndexMap<VirtualPath, BundleFile, FxBuildHasher>>` 与 `introspector: Arc<BundleIntrospector>`。用 `Arc` 包裹是为了让 `Bundle` 可以被廉价 `Clone`，并在导出阶段被 `typst_bundle::export`、CLI 的 `export_bundle` 等多个消费者**共享同一份不可变数据**，避免深拷贝整张文件表。

**练习 2**：`impl Output for Bundle` 的 `target()` 和 `create()` 分别返回 / 做了什么？

> **参考答案**：`target()` 返回 `Target::Bundle`；`create()` 接收 `engine`、`content`、`styles`，直接转交给 `bundle()` 函数。`Bundle` 自身不实现编译逻辑，只是 `Output` 抽象在 bundle 目标上的一个「入口适配」。

**练习 3**：为什么 `files` 用 `IndexMap` 而不是标准库的 `HashMap`？

> **参考答案**：`IndexMap` **保留插入顺序**。bundle 里文件的插入顺序对应源码中 `#document(...)` / `#asset(...)` 的出现顺序，保留它能带来**可预测、可复现**的输出顺序，对调试与确定性构建（reproducible build）很重要；`HashMap` 不保证遍历顺序。

---

### 4.2 BundleFile 与 BundleDocument：两类文件与两种文档

#### 4.2.1 概念说明

`Bundle.files` 是「路径 → `BundleFile`」的映射。那么 `BundleFile` 是什么？它是 bundle 里**单个文件**的抽象。bundle 的文件分成截然不同的两类：

1. **文档（Document）**：由用户写的 `#document("a.pdf", ...)` 产生，需要经过 Typst 排版/编译，再导出成 PDF / PNG / SVG / HTML。
2. **资产（Asset）**：由用户写的 `#asset("note.txt", read(...))` 产生，是**原始字节**，Typst 不排版、不解释，原样写出到目标路径。

这两类东西性质完全不同（一个要排版、一个只是字节），所以 `BundleFile` 是个枚举：`Document(BundleDocument)` 或 `Asset(Bytes)`。

再往下，文档这一类又可以按「最终导出格式」分成两种：

- **Paged（分页）**：PDF / PNG / SVG，它们共享同一种「分页排版」的中间产物 `PagedDocument`（来自 `typst-layout`），只是最后的字节编码不同。
- **Html**：网页，走完全不同的编译路径，产物是 `HtmlDocument`（来自 `typst-html`）。

所以 `BundleDocument` 也是个枚举：`Paged(Box<PagedDocument>, PagedExtras)` 或 `Html(Box<HtmlDocument>)`。

于是我们得到了两层「二选一」：

```
BundleFile ──┬─ Document(BundleDocument)
             └─ Asset(Bytes)

BundleDocument ──┬─ Paged(Box<PagedDocument>, PagedExtras)
                 └─ Html(Box<HtmlDocument>)
```

#### 4.2.2 核心流程

用户侧元素到数据模型的对应关系：

| 用户写的元素 | 经过 compile_document | 落入的数据结构 |
| --- | --- | --- |
| `#document("a.pdf", ...)` | 推断格式为 PDF，走分页排版 | `BundleFile::Document(BundleDocument::Paged(doc, PagedExtras{format: Pdf, ..}))` |
| `#document("b.svg", ...)` | 推断为 SVG，走分页排版 | `BundleFile::Document(BundleDocument::Paged(doc, PagedExtras{format: Svg, ..}))` |
| `#document("c.png", ...)` | 推断为 PNG，走分页排版（且受单页约束） | `BundleFile::Document(BundleDocument::Paged(doc, PagedExtras{format: Png, ..}))` |
| `#document("i.html", ...)` | 推断为 HTML，走 HTML 编译 | `BundleFile::Document(BundleDocument::Html(doc))` |
| `#asset("n.txt", read("n.txt"))` | 不排版，取原始字节 | `BundleFile::Asset(bytes)` |

注意 `AssetElem.data` 字段的类型是 `AssetData`，在落入 `BundleFile::Asset` 时被取出成 `Bytes`（见 [crates/typst-bundle/src/lib.rs:183-187](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L183-L187) 里的 `asset.data.0.clone()`）。

#### 4.2.3 源码精读

先看 `BundleFile` 与 `BundleDocument` 两个枚举：

[crates/typst-bundle/src/lib.rs:74-82](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L74-L82)：`BundleFile` 两个变体——`Document(BundleDocument)`（来自 `document` 元素）与 `Asset(Bytes)`（来自 `asset` 元素的原始字节）。

[crates/typst-bundle/src/lib.rs:84-92](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L84-L92)：`BundleDocument` 两个变体——`Paged(Box<PagedDocument>, PagedExtras)` 与 `Html(Box<HtmlDocument>)`。用 `Box` 是因为 `PagedDocument` / `HtmlDocument` 体积较大，装箱能让枚举本身保持紧凑；`Paged` 多带一个 `PagedExtras`（见 4.3），`Html` 不带。

`BundleDocument` 还实现了一个 `Document` trait，让上层无需关心具体格式就能取到文档元信息（标题、作者等）：

[crates/typst-bundle/src/lib.rs:94-101](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L94-L101)：`impl Document for BundleDocument` 的 `info()` 只是 `match self`——`Paged` 时委托给内层 `PagedDocument.info()`，`Html` 时委托给 `HtmlDocument.info()`。这样上层代码拿着一个 `BundleDocument` 就能统一读取元数据，不必为每种格式写分支。

格式枚举本身定义在 `typst-library`，是用户侧也能看到的概念：

[crates/typst-library/src/model/document.rs:253-260](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/document.rs#L253-L260)：`DocumentFormat` 是文档级的格式枚举，要么 `Paged(PagedFormat)`（再细分为 Pdf/Png/Svg），要么 `Html`。它是 `document(format: ...)` 字段的取值类型。

[crates/typst-library/src/model/document.rs:288-298](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/document.rs#L288-L298)：`PagedFormat` 只有 `Pdf` / `Png` / `Svg` 三个变体——这正是 `PagedExtras.format` 的取值类型。注意它**不包含 Html**：HTML 是与「分页」并列的另一大类，所以它在 `DocumentFormat` 这一层就分道扬镳了。

最后看一眼用户侧产生这两类文件的元素：

[crates/typst-library/src/model/asset.rs:53-66](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/asset.rs#L53-L66)：`AssetElem` 只有两个必填字段——`path: BundlePath`（输出路径）与 `data: AssetData`（原始数据）。它不参与排版，所以在 `BundleFile::Asset(Bytes)` 里只剩下「字节」这一本质信息，路径则被提升为 `files` 映射的键。

#### 4.2.4 代码实践

这是一个**跟踪型实践**，目标是把「用户元素 → 数据模型」的对应关系在源码里走一遍。

1. **实践目标**：验证一个 `#asset(...)` 的字节是如何从 `AssetElem` 流到 `BundleFile::Asset` 的。
2. **操作步骤**：
   - 在 [crates/typst-bundle/src/lib.rs:179-195](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L179-L195) 找到 `engine.parallelize` 的闭包。
   - 关注 `Child::Asset(asset) => Item::Asset(asset.path.clone().into_inner(), asset.data.0.clone(), ...)` 这一行：`asset.path` 提升为路径，`asset.data.0` 取出字节。
   - 再跳到 [crates/typst-bundle/src/lib.rs:206-208](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L206-L208)，看 `Item::Asset(path, bytes, _)` 如何变成 `files.insert(path, BundleFile::Asset(bytes))`。
3. **需要观察的现象**：asset 全程**没有经过任何排版/编译**，只是「路径 + 字节」两份数据在不同结构间搬运。
4. **预期结果**：你会确认 asset 是 bundle 里「最便宜」的文件类型——它的处理成本几乎只有一次 `clone` 和一次 `insert`。
5. 字节最终落盘的细节在 u4 导出阶段，本讲不展开。

#### 4.2.5 小练习与答案

**练习 1**：`BundleFile` 有哪两个变体？分别由哪种用户元素产生？

> **参考答案**：`Document(BundleDocument)` 由 `#document(...)` 产生；`Asset(Bytes)` 由 `#asset(...)` 产生。前者需要排版与格式编码，后者只是原始字节。

**练习 2**：`document("report.pdf")` 和 `document("index.html")` 分别会变成 `BundleDocument` 的哪个变体？`Paged` 变体里 `PagedExtras.format` 分别是什么？

> **参考答案**：`report.pdf` → `BundleDocument::Paged(doc, PagedExtras { format: PagedFormat::Pdf, anchors: Vec::new() })`；`index.html` → `BundleDocument::Html(doc)`。注意 PDF/SVG/PNG 三者都落到 `Paged` 变体，只是 `format` 不同；HTML 走单独的 `Html` 变体，没有 `PagedExtras`。

**练习 3**：`impl Document for BundleDocument` 的 `info()` 为什么写成 `match self`？

> **参考答案**：因为 `BundleDocument` 是个枚举，真正的元数据（标题、作者等）存在内层的 `PagedDocument` 或 `HtmlDocument` 里。`info()` 用 `match` 把请求**委托**给内层文档，对上层屏蔽「这是 paged 还是 html」的差异——这是典型的「外观/委托」模式。

---

### 4.3 PagedExtras：格式与跨文档命名锚点

#### 4.3.1 概念说明

`Paged` 变体除了内层的 `PagedDocument`，还多带了一个 `PagedExtras`。这个「附件」承担两个 `PagedDocument` 自己装不下的职责：

1. **`format: PagedFormat`**——这份分页文档**最终要编码成什么字节**。`PagedDocument` 是格式无关的中间产物（一堆排好的页），同一段 `PagedDocument` 既能编码成 PDF、也能渲染成 PNG 或 SVG；到底选哪个，由 `PagedExtras.format` 决定。换句话说，`PagedDocument` 回答「页长什么样」，`PagedExtras.format` 回答「最后输出成什么文件」。

2. **`anchors: Vec<(Location, EcoString)>`**——为了支持**跨文档链接**而生成的命名锚点。bundle 里的文档 A 可以 `#link` 到文档 B 里的某个具体位置；要让这个链接在导出后的 PDF / SVG 里能精确跳转，就需要给「被链接到的位置」生成一个稳定的名字（命名目的地 / named destination），在编码阶段写进文件。

那为什么 `Html` 变体不需要这两个东西？

- **格式**：HTML 只有一种格式，`HtmlDocument` 天然就是 HTML，不存在「同一段中间产物要选三种编码之一」的问题，所以不需要格式区分字段。
- **锚点**：HTML 的锚点是 DOM 元素上的 `id` 属性，在 `create_link_anchors` 阶段被**直接注入进 `HtmlDocument` 的 DOM 树**（原地修改），锚点信息已经「住进」文档本身了；而 PDF / SVG 的命名目的地是**编码阶段**才生成的东西，且 `PagedDocument` 是被多处共享的不可变结构，不能往里塞锚点，所以只能用 `PagedExtras.anchors` 这个「侧车列表」把锚点带在旁边，留给导出阶段消费。

#### 4.3.2 核心流程

`PagedExtras` 的两个字段在**不同阶段**被填充：

```
# 阶段 1：compile_document（u2-l2）
PagedExtras { format: <推断出的 Pdf/Png/Svg>, anchors: Vec::new() }   # anchors 此时为空

# 阶段 2：bundle_impl 末尾，并行编译之后
introspector = BundleIntrospector::new(&items)
targets = introspector.link_targets()             # 收集所有被链接的目标
anchors = create_link_anchors(&mut items, &targets)  # 为 Paged 填充 anchors，为 Html 注入 DOM id
introspector.set_anchors(anchors)
```

关键在于 `create_link_anchors` 对两种文档的**不同处理**：

```
match doc:
    Html(doc)   => 原地修改 DOM，把 id 注入文档本身（不需要侧车）
    Paged(doc, options) => options.anchors = create_paged_link_anchors(doc, targets)  # 填充侧车
```

#### 4.3.3 源码精读

先看 `PagedExtras` 的定义：

[crates/typst-bundle/src/lib.rs:103-114](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L103-L114)：`PagedExtras` 有两个字段。`format: PagedFormat` 记录最终编码格式；`anchors: Vec<(Location, EcoString)>` 是「位置 → 锚点名」的列表。注释明确说明：并非所有导出目标都支持命名锚点（比如 PNG 就不支持），那种情况下这个列表可以被忽略。

再看 `create_link_anchors` 如何按文档类型分派：

[crates/typst-bundle/src/link.rs:20-63](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/link.rs#L20-L63)：这是本模块「为什么 Paged 需要侧车、Html 不需要」的直接证据。重点看这两个分支：

- [crates/typst-bundle/src/link.rs:33-40](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/link.rs#L33-L40)：`Html` 分支调用 `typst_html::create_link_anchors(doc.as_mut(), ...)`，注释写明 **"Mutates the DOM in place to insert IDs as necessary"**——锚点被直接写进 `HtmlDocument` 的 DOM，所以 `Html` 变体不需要 `PagedExtras` 这样的侧车。
- [crates/typst-bundle/src/link.rs:41-46](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/link.rs#L41-L46)：`Paged` 分支调用 `create_paged_link_anchors(doc, targets)`，把结果**赋值给 `options.anchors`**（即 `PagedExtras.anchors`），注释写明 **"Mutates the export options so that named destinations are generated"**——锚点作为侧车列表挂到 `PagedExtras` 上，留给导出阶段（PDF 的命名目的地、SVG 的片段标识）使用。

最后看 paged 锚点是如何生成的：

[crates/typst-bundle/src/link.rs:69-88](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/link.rs#L69-L88)：`create_paged_link_anchors` 遍历被链接到的目标，用 `AnchorGenerator` 为每个目标生成一个稳定名字，收集成 `Vec<(Location, EcoString)>`。这里先 `targets.sort_by_key(|loc| elements.loc_index(loc))` 按文档内顺序排序再生成分配锚点，是为了让锚点分配在多次内省迭代中保持**稳定**（同样的输入产生同样的名字），这直接影响内省能否收敛——细节留到 u5-l2。

#### 4.3.4 代码实践

这是一个**对比阅读型实践**，目标是亲手验证两种文档在锚点处理上的不对称。

1. **实践目标**：在 `create_link_anchors` 里确认「Html 改文档、Paged 改 options」这一关键差异。
2. **操作步骤**：
   - 打开 [crates/typst-bundle/src/link.rs:32-47](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/link.rs#L32-L47)。
   - 在 `Html(doc)` 分支旁标注「原地改 DOM」；在 `Paged(doc, options)` 分支旁标注「写 options.anchors」。
   - 回到 [crates/typst-bundle/src/lib.rs:105-114](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L105-L114)，对照 `anchors` 字段的注释「Not all export targets support this (e.g. PNG), in which case it can simply be ignored」。
3. **需要观察的现象**：`Html` 分支没有返回需要额外存储的锚点列表（锚点已在 DOM 里）；`Paged` 分支返回的锚点列表被存进了 `PagedExtras`。
4. **预期结果**：你能用自己的话解释「为什么只有 `Paged` 变体需要 `PagedExtras`」——一句话：HTML 的锚点是文档自身的一部分（DOM id），而 paged 的命名目的地是编码阶段的产物、且 `PagedDocument` 不可变，所以必须用侧车携带。
5. 本实践为源码阅读，无需运行命令。

#### 4.3.5 小练习与答案

**练习 1**：`PagedExtras` 有哪两个字段？分别解决什么问题？

> **参考答案**：`format: PagedFormat`（决定 `PagedDocument` 最终编码成 PDF / PNG / SVG 哪种字节）与 `anchors: Vec<(Location, EcoString)>`（跨文档链接用的命名锚点列表，供导出阶段生成命名目的地）。两者都是 `PagedDocument` 自身装不下的「导出侧」信息。

**练习 2**：为什么 `Html` 变体不需要像 `Paged` 那样额外携带锚点列表？

> **参考答案**：因为 HTML 的锚点是 DOM 元素上的 `id`，在 `create_link_anchors` 里被**原地注入**进 `HtmlDocument` 的 DOM 树，锚点信息已经「住进」文档本身；而 paged 的命名目的地是编码阶段才生成、且 `PagedDocument` 是共享不可变结构，无法内嵌锚点，所以只能用 `PagedExtras.anchors` 这个侧车列表带在旁边。

**练习 3**：`PagedExtras.anchors` 在 `compile_document` 阶段是什么值？它什么时候才被真正填充？

> **参考答案**：在 `compile_document` 阶段是 `Vec::new()`（空列表，见 u2-l2）；它要到 `bundle_impl` 末尾、并行编译**之后**，由 `create_link_anchors` 调用 `create_paged_link_anchors` 才被填充。这正是因为锚点收集依赖于「所有文档都已编译完成」的全局视图。

---

## 5. 综合实践

把本讲的三个数据结构串成一张完整的包含关系图，并回答核心问题。

### 任务

1. **画一张包含关系图**，表示 `Bundle` → `files(IndexMap)` → `BundleFile` → `BundleDocument` 的层级，并标出每个字段的类型。参考答案如下（你可以照着源码自己重画一遍）：

   ```
   Bundle
   ├── files: Arc<IndexMap<VirtualPath, BundleFile, FxBuildHasher>>
   │   └── BundleFile                          # 单个文件，二选一
   │       ├── Document(BundleDocument)
   │       │   └── BundleDocument              # 文档，二选一
   │       │       ├── Paged(Box<PagedDocument>, PagedExtras)
   │       │       │   ├── Box<PagedDocument>  # 来自 typst-layout，格式无关的分页产物
   │       │       │   └── PagedExtras         # paged 专属「侧车」
   │       │       │       ├── format: PagedFormat      (Pdf | Png | Svg)
   │       │       │       └── anchors: Vec<(Location, EcoString)>  # 跨文档命名锚点
   │       │       └── Html(Box<HtmlDocument>)  # 来自 typst-html，无 PagedExtras
   │       └── Asset(Bytes)                    # 来自 #asset(...)，原始字节，直通
   └── introspector: Arc<BundleIntrospector>    # 跨所有文档的统一内省器
   ```

2. **解释核心问题**：为什么 `Paged` 变体需要额外携带 `PagedExtras`，而 `Html` 变体不需要？请从「格式区分」和「跨文档锚点」两个角度分别作答，并引用 [crates/typst-bundle/src/link.rs:33-46](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/link.rs#L33-L46) 作为证据。

### 参考答案要点

- **格式区分**：`PagedDocument` 是格式无关的中间产物，同一段产物可编码成 PDF/PNG/SVG，需要 `PagedExtras.format` 来指定；而 `HtmlDocument` 天然只有 HTML 一种格式，无需区分。
- **跨文档锚点**：HTML 的锚点（DOM `id`）在 `create_link_anchors` 中被**原地注入** `HtmlDocument`（link.rs:33-40 的 `Mutates the DOM in place`）；而 paged 的命名目的地是编码阶段产物、且 `PagedDocument` 不可变共享，所以锚点只能作为 `PagedExtras.anchors` 侧车携带（link.rs:41-46 的 `Mutates the export options`），留给导出阶段消费。

### 进阶（可选）

写一个最小 bundle 项目，包含一个 PDF 文档、一个 HTML 文档和一个 asset，编译后对照上面的图，说出每个产出文件分别对应图里的哪一条路径。编译与落盘的完整链路将在 u4-l1（export 与 VirtualFs）和 u5-l4（CLI 集成）详细讲解；如果你现在还无法本地开启 `--features bundle`，可先把这道题当成「源码阅读题」，标注「待本地验证」即可。

---

## 6. 本讲小结

- `Bundle` 是 bundle 编译的顶层产物，只有两个字段：`files`（一个保留插入顺序的 `IndexMap`，路径 → 文件）与 `introspector`（跨所有文档的统一 `BundleIntrospector`），两者都用 `Arc` 包裹以便廉价共享。
- `impl Output for Bundle` 让 bundle 目标接入 `compile::<Bundle>` 泛型入口：`target()` 返回 `Target::Bundle`，`create()` 转交 `bundle()`。
- `BundleFile` 是单文件抽象，分两类：`Document(BundleDocument)`（来自 `#document`，需排版）与 `Asset(Bytes)`（来自 `#asset`，原始字节直通）。
- `BundleDocument` 是文档抽象，分两种：`Paged(Box<PagedDocument>, PagedExtras)`（PDF/PNG/SVG 共用）与 `Html(Box<HtmlDocument>)`；`impl Document` 让上层统一读取元数据。
- `PagedExtras` 承担两个 `PagedDocument` 装不下的职责：`format`（最终编码格式）与 `anchors`（跨文档命名锚点）。
- `Html` 变体不需要 `PagedExtras`：HTML 只有一种格式，且锚点（DOM `id`）被原地注入文档本身；而 paged 的命名目的地是编码阶段产物，只能用侧车携带。

---

## 7. 下一步学习建议

本讲搞清楚了「`Bundle` 里装了什么」，但还没讲「`Bundle` 怎么变成磁盘上的文件」。建议接下来：

- **u4-l1 export 主流程与 VirtualFs**：看 `export.rs` 如何把 `Bundle` 并行转换成 `VirtualFs`（路径 → 字节），以及 `BundleOptions` 如何聚合四种格式的导出选项。这是 `BundleFile` / `BundleDocument` 被真正「编码成字节」的地方。
- **u3-l2 用户视角的 document 与 asset 元素**：如果你想先从用户侧（`DocumentElem` 的 `path` / `format` / `title` 字段、`AssetElem`、`BundlePath`、以及 `ShowSet` 如何把元数据传播成上下文可用）深入，可以先读这一篇。
- **u5-l1 统一内省器 BundleIntrospector**：本讲只点了 `Bundle.introspector` 字段的存在；它如何把多个文档聚合到一个内省循环、让它们彼此可见，是专家层的内容。
