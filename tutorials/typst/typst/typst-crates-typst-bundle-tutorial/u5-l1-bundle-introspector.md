# 统一内省器 BundleIntrospector

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚为什么 bundle 不能让「每个文档各自跑内省循环」，而必须用一个**跨所有文档的统一内省器**。
- 读懂 `BundleIntrospector` 的三个字段（`children` / `elements` / `anchors`）各自承担什么职责。
- 解释「`elements` 的位置是 `children` 的 off-by-one 下标」这一设计，以及它如何把任意 `Location` 精确路由到正确的子文档内省器。
- 理解 `ChildIntrospector` 如何通过 `Deref` 复用 `PagedIntrospector` / `HtmlIntrospector` 既有的实现，让 bundle 层几乎不用重写查询逻辑。
- 区分 `BundleIntrospector` 的三类查询：直接转发给 `elements` 的「目标无关查询」、先路由到 `child` 再转发的「位置相关查询」、以及 bundle 专属的 `path()` / `document()` / `anchor()`。

本讲是专家层（u5）的第一篇，承接 u2-l1（`bundle_impl` 主流程）和 u3-l1（`Bundle` 数据模型）。u3-l1 已经告诉你 `Bundle` 顶层产物里有一个 `introspector: Arc<BundleIntrospector>` 字段；本讲就专门拆开这个字段，讲清楚它内部长什么样、怎么工作。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

### 2.1 什么是 Typst 的「内省（introspection）」

Typst 源码里可以写「自指」的内容，比如：

- `#context counter(heading).display()` —— 显示当前一共有多少个标题；
- `#query(heading)` —— 查询文档里所有的标题元素；
- `#context here().page()` —— 当前在第几页。

这些功能都依赖**内省**：编译产物需要能回答「某个元素在文档里的位置 / 某个选择器匹配了哪些元素」这类问题。负责回答这些问题的对象就叫**内省器（introspector）**，它实现了一个统一的 trait `Introspector`（见 [crates/typst-library/src/introspection/introspector.rs:28-89](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/introspection/introspector.rs#L28-L89)）。

### 2.2 为什么需要「内省循环」

内省有个鸡生蛋的难题：要回答「当前第几页」，可能需要先排版；但排版时某些元素的大小又取决于「当前第几页」（比如目录长度会影响分页）。Typst 的解法是**迭代到收敛**：先排版一遍、得到一个内省器，再用它重新排版，如此反复，直到内省结果不再变化（或达到 5 次上限）。这个「重排直到稳定」的过程就叫**内省循环（introspection loop）**。

> 单文档（paged / html 目标）里，这个循环的收敛判据是「单文档内省器稳定」。bundle 的关键区别在于：**收敛判据是整个 bundle 的统一内省器稳定**——本讲的核心就在这里。

### 2.3 一个关键抽象：ElementIntrospector

`Introspector` trait 有很多方法（`query` / `page` / `position` …）。 Typst 把其中「与具体目标无关」的那部分（`query` / `query_label` / `query_count_before` 等）抽到一个泛型结构 `ElementIntrospector<P>` 里（[crates/typst-library/src/introspection/introspector.rs:169-190](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/introspection/introspector.rs#L169-L190)）。其中类型参数 `P` 是「位置类型」：

- 在 paged 文档里 `P = PagedPosition`（第几页 + 坐标）；
- 在 HTML 文档里 `P = HtmlPosition`（DOM 树路径）；
- 在 bundle 里 `P = Option<NonZeroUsize>`——**这正是本讲的重点**。

`PagedIntrospector` 和 `HtmlIntrospector` 内部都各自持有一个 `ElementIntrospector<自己的位置>`，并在此基础上补上「位置相关」的查询。理解这一点，你才能看懂 `BundleIntrospector` 是怎么把多个子内省器拼到一起的。

## 3. 本讲源码地图

本讲主要围绕 `typst-bundle` 的两个文件，并少量引用兄弟 crate 作为支撑：

| 文件 | 作用 | 本讲用到的地方 |
| --- | --- | --- |
| [crates/typst-bundle/src/introspect.rs](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/introspect.rs) | **本讲主角**。定义 `BundleIntrospector`、`ChildIntrospector` 和构造器 `BundleIntrospectorBuilder`，并实现 `Introspector` trait。 | 几乎全部章节 |
| [crates/typst-bundle/src/lib.rs](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs) | `Bundle` 结构里持有 `introspector` 字段；`bundle_impl` 负责构造并填充它。 | 4.1 节讲「统一内省循环」时看构造时机 |
| [crates/typst-library/src/introspection/introspector.rs](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/introspection/introspector.rs) | 定义 `Introspector` trait 与泛型 `ElementIntrospector<P>`、`ElementIntrospectorBuilder<P>`。 | 4.2、4.3 节理解转发与合并 |
| [crates/typst/src/lib.rs](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst/src/lib.rs) | `compile_impl` 里的内省循环。 | 4.1 节讲「收敛判据是谁」 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **4.1 统一内省设计动机**：为什么 bundle 要用一个大的内省循环。
2. **4.2 `children` 与 `elements`**：用「子下标 + 1」当位置，做 Location → 子文档的路由。
3. **4.3 `Introspector` trait 的查询实现**：三类查询的转发策略，以及 `path()` / `document()` / `anchor()`。

---

### 4.1 统一内省设计动机：一个大的内省循环

#### 4.1.1 概念说明

bundle 一次编译会产出**多个文档**（若干 PDF / PNG / SVG / HTML）。一个自然的问题是：内省循环该怎么跑？

- **方案 A（每文档独立循环）**：每个文档各自跑内省循环，各自收敛。
- **方案 B（统一循环）**：把所有文档当成一个整体，跑**一个**大的内省循环，所有文档共用同一个内省器，彼此可见。

Typst 选的是方案 B。`Bundle` 结构上的文档注释把这层意图写得很直白：

> The whole bundle is subject to one large introspection loop (as opposed to each document iterating separately). They can introspect each other and all contribute to this one introspector.

见 [crates/typst-bundle/src/lib.rs:46-54](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L46-L54)。

为什么必须如此？因为 bundle 的核心价值之一就是**跨文档内省**：`index.html` 里可以查询 `appendix.pdf` 里的标题、一个 figure 计数器可以跨越多个文档累加、一个文档里的 `#link` 可以指向另一个文档里的锚点。如果每个文档各自循环、互不可见，这些能力就全废了。统一内省器让「整个 bundle」成为一个可被查询的单一名字空间。

#### 4.1.2 核心流程

统一循环的实际驱动点不在 `typst-bundle`，而在 `typst` crate 的 `compile_impl`。对 bundle 目标而言，循环体里调用的 `T::create` 就是 `bundle()` → `bundle_impl()`。伪代码如下：

```
loop {                                       // 重排直到内省稳定
    introspector = 上一轮的 document.introspector()  // bundle 的话就是 BundleIntrospector
    document = T::create(engine, content)    // = bundle_impl，内部重建 BundleIntrospector
    if constraint.validate(document.introspector()) {
        break                                 // 整个 bundle 的内省稳定了才算收敛
    }
    if 已达 5 次: 分析并报警后 break
}
```

关键点：收敛判据 `constraint.validate(document.introspector())` 用的就是 `Bundle::introspector()`（[crates/typst-bundle/src/lib.rs:57-59](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L57-L59)），即 `BundleIntrospector`。所以「稳定」是**跨所有文档**一起判定的——任何一个文档的内省发生变化，整个 bundle 都得再来一轮。

而 `BundleIntrospector` 的构造发生在 `bundle_impl` 的末尾，**在所有文档并行编译完成之后**：

```
items = parallelize(各文档 -> compile_document)   // 先并行编译各文档
introspector = BundleIntrospector::new(&items)    // 再用全部 items 建统一内省器
targets      = introspector.link_targets()        // 收集跨文档链接目标
anchors      = create_link_anchors(&mut items, targets)  // 生成锚点
introspector.set_anchors(anchors)                 // 把锚点回填进内省器
```

这条顺序（先并行编译、后统一内省、再锚点）是刻意安排的，它和 u5-l3 要讲的并行 + 记忆化设计紧密相关。本讲只需记住：**`BundleIntrospector::new` 接收的是已经编译好的全部 `items`**。

#### 4.1.3 源码精读

`BundleIntrospector::new` 用一个 builder 逐项「发现」可内省内容：

```rust
// crates/typst-bundle/src/introspect.rs:38-45
#[typst_macros::time(name = "introspect bundle")]
pub(crate) fn new(items: &[Item]) -> BundleIntrospector {
    let mut builder = BundleIntrospectorBuilder::default();
    for item in items {
        builder.discover_item(item);
    }
    builder.finish()
}
```

[crates/typst-bundle/src/introspect.rs:38-45](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/introspect.rs#L38-L45) —— 它遍历 `bundle_impl` 传进来的全部 `items`（每个 `Item` 是一个文档、资产或顶层 tag），把它们登记进统一内省器。

`bundle_impl` 里负责调它、并做后续锚点回填的几行：

```rust
// crates/typst-bundle/src/lib.rs:197-200
let mut introspector = BundleIntrospector::new(&items);
let targets = introspector.link_targets();
let anchors = crate::link::create_link_anchors(&mut items, &targets);
introspector.set_anchors(anchors);
```

[crates/typst-bundle/src/lib.rs:197-200](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L197-L200)。注意 `set_anchors` 是「后置」的：内省器先建好，链接锚点（跨文档跳转用，详见 u5-l2）在文档 DOM / 命名目的地生成出来之后才回填。这正是 `BundleIntrospector` 有一个独立 `anchors` 字段的原因。

而驱动循环本身在 `compile_impl`：

```rust
// crates/typst/src/lib.rs:138-161（节选）
loop {
    let introspector = history.last().map(|doc| doc.introspector())
        .unwrap_or(&empty_introspector);
    ...
    document = T::create(&mut engine, &content, styles)?;   // 对 bundle 即 bundle_impl
    if timed!("check stabilized", constraint.validate(document.introspector())) {
        sink.extend_from_sink(subsink);
        break;
    }
    ...
}
```

[crates/typst/src/lib.rs:133-185](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst/src/lib.rs#L133-L185) —— 收敛判据是 `document.introspector()`，对 bundle 就是 `BundleIntrospector`。

#### 4.1.4 代码实践

**实践目标**：通过阅读源码，确认「bundle 的收敛是跨文档的」，并尝试构造一个跨文档内省的例子。

**操作步骤**：

1. 打开 [crates/typst/src/lib.rs:156-161](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst/src/lib.rs#L156-L161)，确认 `T::create` 的返回值 `document.introspector()` 被用作下一轮的输入和收敛判据。
2. 打开 [crates/typst-bundle/src/lib.rs:57-59](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L57-L59)，确认 `Bundle::introspector()` 返回的正是 `BundleIntrospector`，即收敛对象是「整个 bundle」。
3. （可选，需 `--features bundle`）写一个最小 bundle，让两个文档互相可见：

```typst
// 示例代码：跨文档 query（仅示意，需本地用 --features bundle 验证）
#document("index.html")[
  // 这里的 query 能看到 list.html 里的 heading 吗？
  共有 #context str(query(heading).len()) 个标题。
]
#document("list.html")[
  = 第一章
  = 第二章
]
```

**需要观察的现象**：第 3 步里 `query(heading).len()` 是否等于 2（即 `index.html` 能看到 `list.html` 里的标题）。如果是，就证明了统一内省器让跨文档查询成立。

**预期结果**：跨文档 query 成立，`len()` 为 2。若本地无法启用 bundle feature，则**待本地验证**，但源码层面（统一内省循环）已能推出该结论。

#### 4.1.5 小练习与答案

**练习 1**：如果把 bundle 改成「每个文档各自跑内省循环」，跨文档 `query` 会出什么问题？

> **参考答案**：每个文档只能看到自己的元素，`query(heading)` 在 `index.html` 里只会返回 `index.html` 自己的标题，看不到 `list.html` 的标题；跨文档的计数器、链接锚点也会失效。这正是 Typst 坚持用统一内省器的原因。

**练习 2**：`BundleIntrospector::new` 为什么接收的是 `&[Item]` 而不是 `&Bundle`？

> **参考答案**：因为构造顺序是「先并行编译得到 `items` → 用 `items` 建内省器 → 再把 `items` 装进 `Bundle.files`」（见 `bundle_impl`）。建内省器时 `Bundle` 还没组装好，能拿到的就是中间产物 `items`。

---

### 4.2 `children` 与 `elements`：用「子下标 + 1」做位置路由

#### 4.2.1 概念说明

统一内省器要解决的核心难题是：**给定一个 `Location`，怎么知道它属于哪个文档？**

`Location` 只是一个 128 位哈希（[crates/typst-library/src/introspection/location.rs:52-54](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/introspection/introspector.rs#L52-L54)），它本身不带「我在哪个文档」的信息。`BundleIntrospector` 的解法非常巧妙：**复用 `ElementIntrospector<P>` 的「位置」字段来记录归属**。

回顾 2.3 节：`ElementIntrospector<P>` 给每个可内省元素存一个位置 `P`。bundle 令 `P = Option<NonZeroUsize>`，并约定：

- `Some(n)`（`n ≥ 1`）—— 该元素位于第 `n - 1` 个子文档里（off-by-one！）；
- `None` —— 该元素不在任何子文档内（即顶层的 document / asset 元素本身，或顶层 tag）。

这样一来，「查询某元素属于哪个文档」就退化成「查 `elements` 里它的位置」，O(1) 完成。

#### 4.2.2 核心流程

构造时，builder 给**每个子文档**分配一个 1-based 下标 `pos = 1 + 已有子文档数`，然后把该文档内**所有元素**都登记进 `elements`，位置统一打成这个 `pos`（丢弃子文档原本的几何/DOM 位置——那些位置留在子内省器里）：

```
对第 i 个文档 (i 从 0 开始):
    pos = i + 1                          // NonZeroUsize，1-based
    把该文档 introspector.elements() 里每个元素登记进 bundle.elements，位置 = pos
    children.push((path, 子内省器, 文档自身 Location))
```

查询时，把位置换算回下标即可路由：

\[
\text{child\_index} \;=\; \text{pos} \;-\; 1
\]

\[
\text{pos} \in \{1, 2, \dots, N\},\quad \text{pos} = \text{None} \;\Leftrightarrow\; \text{不在任何子文档内}
\]

为什么用 `NonZeroUsize` 而不是普通 `usize`？这是 Rust 的**niche 优化**：`NonZeroUsize` 保证不为 0，于是 `Option<NonZeroUsize>` 可以用 `0` 这个原本非法的值来表示 `None`，整个 `Option` 和单个 `usize` 占一样大的内存（8 字节）。换句话说，用 `None` 表达「顶层」几乎是零成本的。

#### 4.2.3 源码精读

先看 `BundleIntrospector` 的三个字段：

```rust
// crates/typst-bundle/src/introspect.rs:22-34
pub struct BundleIntrospector {
    /// 所有 bundle 文档的路径与内省器（不含 asset）。
    children: Vec<(VirtualPath, ChildIntrospector, Location)>,
    /// 用于大多数查询的目标无关内省器。
    /// 这里的 positions 是 children 的 off-by-one 下标。
    elements: ElementIntrospector<Option<NonZeroUsize>>,
    /// 从元素 location 到已分配链接锚点的映射（跨文档链接用，锚点局部于所属文档）。
    anchors: FxHashMap<Location, EcoString>,
}
```

[crates/typst-bundle/src/introspect.rs:22-34](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/introspect.rs#L22-L34) —— 注意 `elements` 字段上方那句注释正是本节主题：「The positions are off-by-one indices into `children`.」

`children` 三元组的含义：(文档路径, 子内省器, 文档自身的 Location)。下标 `.0`/`.1`/`.2` 在 `path()` / `child()` / `document()` 里分别被取用。

路由函数 `child()` 只有 4 行，是整个设计的精华：

```rust
// crates/typst-bundle/src/introspect.rs:69-76
fn child(&self, location: Location) -> Option<&ChildIntrospector> {
    let pos = *self.elements.position(location)?;
    let index = pos?.get() - 1;
    Some(&self.children[index].1)
}
```

[crates/typst-bundle/src/introspect.rs:69-76](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/introspect.rs#L69-L76) —— `self.elements.position(location)` 返回 `Option<&Option<NonZeroUsize>>`；外层 `?` 处理「该 location 完全不存在」，内层 `pos?` 处理「存在但位置是 `None`（顶层）」，剩下的 `Some(n)` 减 1 得到 `children` 下标，取出子内省器（`.1`）。

位置是在构造期打上的，看 `discover_document`：

```rust
// crates/typst-bundle/src/introspect.rs:230-250（节选）
fn discover_document(&mut self, path: &VirtualPath, doc: &BundleDocument, loc: Location) {
    let pos = NonZeroUsize::new(1 + self.subdocuments.len());   // 1-based
    let subintrospector = match doc {
        BundleDocument::Paged(doc, _) => {
            self.elements
                .discover_elements(doc.introspector().elements(), |_| pos);  // 位置统一打成 pos
            ChildIntrospector::Paged(doc.introspector().clone())
        }
        BundleDocument::Html(doc) => {
            self.elements
                .discover_elements(doc.introspector().elements(), |_| pos);
            ChildIntrospector::Html(doc.introspector().clone())
        }
    };
    self.subdocuments.push((path.clone(), subintrospector, loc));
}
```

[crates/typst-bundle/src/introspect.rs:230-250](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/introspect.rs#L230-L250)。关键在 `discover_elements(..., |_| pos)`：闭包把子文档每个元素原本的位置（`PagedPosition` / `HtmlPosition`）**统一映射成常量 `pos`**，也就是「丢弃真实位置，只留归属下标」。`discover_elements` 本身定义在 [crates/typst-library/src/introspection/introspector.rs:533-558](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/introspection/introspector.rs#L533-L558)，它负责把另一个已建好的内省器的元素「搬」进来（用 `seen` 集合去重，保证一个 location 只归属一个文档）。

而顶层元素（不在任何文档里的）由 `discover_item` 用 `None` 位置登记：

```rust
// crates/typst-bundle/src/introspect.rs:221-227
fn discover_item(&mut self, item: &Item) {
    match item {
        Item::Tag(tag) => self.elements.discover_tag(tag, None),   // 顶层 tag → None
        Item::Asset(..) => {}
        Item::Document(path, doc, loc) => self.discover_document(path, doc, *loc),
    }
}
```

[crates/typst-bundle/src/introspect.rs:221-227](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/introspect.rs#L221-L227)。注意 `Item::Tag` 用 `None`：因为 `DocumentElem`、`AssetElem` 都是 `Locatable`（见 [crates/typst-library/src/model/document.rs:125](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/document.rs#L125) 和 [crates/typst-library/src/model/asset.rs:52](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/asset.rs#L52)），实现期它们会作为顶层 tag 流转进来，于是被登记为 `None` 位置——这正是 4.3 节 `path()` / `document()` 能识别「文档/资产自身」的前提。

#### 4.2.4 代码实践

> 这也是本讲指定的核心实践任务。

**实践目标**：通过精读 `child()` 与 `position()`，解释 off-by-one 路由设计；并说明 `ChildIntrospector` 的 `Deref` 如何复用既有内省器。

**操作步骤**：

1. 阅读 [crates/typst-bundle/src/introspect.rs:69-76](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/introspect.rs#L69-L76) 的 `child()`，回答：对一个位于第 3 个文档里的元素，`elements.position(loc)` 返回什么？`child()` 最终取到 `children[?]` 的下标是多少？
2. 阅读 [crates/typst-bundle/src/introspect.rs:192-201](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/introspect.rs#L192-L201) 的 `impl Deref for ChildIntrospector`，确认 `ChildIntrospector` 能被当作 `dyn Introspector` 使用。
3. 对照 [crates/typst-bundle/src/introspect.rs:116-122](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/introspect.rs#L116-L122) 的 `page` / `position`，确认它们是「先 `child()` 路由，再通过 Deref 调用子内省器的同名方法」。

**需要观察的现象**：

- 第 1 步：第 3 个文档（下标 2）里的元素，`pos = Some(3)`，`child()` 算出 `index = 3 - 1 = 2`，取 `children[2].1`。任意 `Location` 都能这样被一路路由到正确的子文档。
- 第 2 步：`Deref::Target = dyn Introspector`，`Paged`/`Html` 两个变体分别返回 `introspector.as_ref()`（`&PagedIntrospector` / `&HtmlIntrospector` 自动协程到 `&dyn Introspector`）。

**预期结果**：你能用一句话说清——「`elements` 把每个元素的位置存成它所属子文档的 1-based 下标，`child()` 减 1 还原成 0-based 下标取出子内省器；而子内省器通过 `Deref<Target = dyn Introspector>` 直接复用了 `PagedIntrospector` / `HtmlIntrospector` 已实现好的全部查询，bundle 层一行都不用重写。」

#### 4.2.5 小练习与答案

**练习 1**：为什么 `elements` 的位置类型是 `Option<NonZeroUsize>` 而不是 `usize`（用 `0` 表示「顶层」）？

> **参考答案**：为了 niche 优化。`NonZeroUsize` 不可能为 0，于是 `Option<NonZeroUsize>` 可用 `0` 表示 `None`，与 `usize` 等宽。这样既表达「不在任何子文档」的语义，又不增加每个元素的内存开销。

**练习 2**：`discover_elements(..., |_| pos)` 这个闭包「丢弃」了子文档的真实位置。这些真实位置丢了吗？为什么之后 `page()` / `position()` 还能返回正确结果？

> **参考答案**：没丢。真实位置（`PagedPosition` / `HtmlPosition`）保存在子内省器 `PagedIntrospector` / `HtmlIntrospector` 自己的 `ElementIntrospector` 里。bundle 层的 `elements` 只需要知道「归属哪个子文档」，所以用常量 `pos` 覆盖；真正要算页码/DOM 路径时，`page()` / `position()` 会先 `child()` 路由到子内省器，再由子内省器用自己的真实位置回答。

---

### 4.3 `Introspector` trait 的查询实现：转发、path()、document()、anchor()

#### 4.3.1 概念说明

`Introspector` trait 有十几个方法（[crates/typst-library/src/introspection/introspector.rs:28-89](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/introspection/introspector.rs#L28-L89)）。`BundleIntrospector` 实现它们时，把方法分成**三类**，分别走不同路径：

| 类别 | 方法 | 走哪条路 | 原因 |
| --- | --- | --- | --- |
| 目标无关查询 | `query` / `query_first` / `query_unique` / `query_label` / `query_labelled` / `query_count_before` / `label_count` / `locator` | 直接转发给 `self.elements` | 这些只依赖「元素集合 + 顺序」，不依赖真实位置。`elements` 已把所有文档的元素合并成一个全局集合。 |
| 位置相关查询 | `pages` / `page` / `position` / `page_numbering` / `page_supplement` | 先 `self.child(loc)?` 路由，再转发给子内省器 | 这些要返回真实页码/坐标/DOM 路径，只有子内省器知道。 |
| bundle 专属 | `anchor` / `document` / `path` | bundle 自己实现 | 跨文档概念，单文档内省器（返回 `None`）无法回答。 |

这个三分法是理解 `BundleIntrospector` 的钥匙：**目标无关的查询在 bundle 层一次性合并处理；位置相关的查询下放给子内省器；跨文档概念由 bundle 自己负责。**

#### 4.3.2 核心流程

```
query(selector)        →  self.elements.query(selector)            # 全局合并集合
page(loc)              →  self.child(loc)?.page(loc)                # 路由到子内省器
anchor(loc)            →  self.anchors.get(&loc)                    # bundle 自己的锚点表
document(loc):
    if elements 里该 loc 的位置是 Some(i):  return children[i-1].2   # 元素在某个文档里 → 返回该文档自身 Location
    else (位置 None, 即文档/资产自身):      返回 DocumentElem.location
path(loc):
    if 位置 Some(i):                        return &children[i-1].0  # 元素在某文档里 → 返回该文档路径
    else:  DocumentElem → doc.path ;  AssetElem → asset.path ;  否则 None
```

`document()` 和 `path()` 都有「两层」逻辑：先看 `elements` 给出的位置——如果是 `Some`，说明这个 location 是文档**内部**的元素，于是返回它所属文档的 Location / 路径；如果是 `None`，说明这个 location 就是文档或资产**自身**，于是去 `elements` 里把它取出来，判断是 `DocumentElem` 还是 `AssetElem`，返回各自的路径。这正是 4.2.3 节末尾「顶层元素登记为 `None`」的用武之地。

#### 4.3.3 源码精读

**第一类：目标无关查询**，一律直接转发给 `elements`：

```rust
// crates/typst-bundle/src/introspect.rs:80-110（节选）
fn query(&self, selector: &Selector) -> EcoVec<Content> { self.elements.query(selector) }
fn query_first(&self, selector: &Selector) -> Option<Content> { self.elements.query_first(selector) }
fn query_label(&self, label: Label) -> StrResult<&Content> { self.elements.query_label(label) }
fn query_count_before(&self, selector: &Selector, end: Location) -> usize {
    self.elements.query_count_before(selector, end)
}
fn locator(&self, key: u128, base: Location) -> Option<Location> {
    self.elements.locator(key, base)
}
```

[crates/typst-bundle/src/introspect.rs:80-110](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/introspect.rs#L80-L110)。因为 `elements` 已经合并了所有文档的元素，这些方法天然就是「跨文档」的——这正是统一内省器能支持跨文档 `query` 的底层原因。

**第二类：位置相关查询**，先路由再转发：

```rust
// crates/typst-bundle/src/introspect.rs:112-130（节选）
fn pages(&self, location: Location) -> Option<NonZeroUsize> {
    self.child(location)?.pages(location)
}
fn page(&self, location: Location) -> Option<NonZeroUsize> {
    self.child(location)?.page(location)
}
fn position(&self, location: Location) -> Option<DocumentPosition> {
    self.child(location)?.position(location)
}
fn page_numbering(&self, location: Location) -> Option<&Numbering> {
    self.child(location)?.page_numbering(location)
}
```

[crates/typst-bundle/src/introspect.rs:112-130](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/introspect.rs#L112-L130)。注意 `self.child(location)?` 返回 `&ChildIntrospector`，随后 `.pages(location)` 等调用是通过 `Deref<Target = dyn Introspector>` 转发到 `PagedIntrospector` / `HtmlIntrospector` 的实现（见 4.2.4 第 2 步）。比如 `DocumentPosition` 是个跨目标枚举（[crates/typst-library/src/introspection/position.rs:14-21](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/introspection/position.rs#L14-L21)）：子内省器是 paged 就返回 `Paged(PagedPosition)`，是 html 就返回 `Html(HtmlPosition)`，bundle 自己完全不用关心。

**第三类：bundle 专属查询。**

`anchor` 直接查 bundle 自己的锚点表：

```rust
// crates/typst-bundle/src/introspect.rs:132-134
fn anchor(&self, location: Location) -> Option<&EcoString> {
    self.anchors.get(&location)
}
```

[crates/typst-bundle/src/introspect.rs:132-134](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/introspect.rs#L132-L134)。`anchors` 由 `set_anchors` 在构造期末尾回填（4.1.3 节），里面存的是跨文档链接要跳转到的锚点（HTML 的 DOM id / paged 的命名目的地），详见 u5-l2。

`document` 用「两层」逻辑返回「包含该 location 的文档」自身的 Location：

```rust
// crates/typst-bundle/src/introspect.rs:136-146
fn document(&self, location: Location) -> Option<Location> {
    let index = *self.elements.position(location)?;
    if let Some(index) = index {
        return Some(self.children[index.get() - 1].2);   // 内部元素 → 所属文档的 Location（.2）
    }
    self.elements                                              // 文档自身 → 它自己的 Location
        .get_by_loc(&location)?
        .to_packed::<DocumentElem>()
        .map(|doc| doc.location().unwrap())
}
```

[crates/typst-bundle/src/introspect.rs:136-146](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/introspect.rs#L136-L146)。

`path` 结构相同，只是把「文档自身」再细分成 DocumentElem / AssetElem：

```rust
// crates/typst-bundle/src/introspect.rs:148-165（节选）
fn path(&self, location: Location) -> Option<&VirtualPath> {
    let index = *self.elements.position(location)?;
    if let Some(index) = index {
        return Some(&self.children[index.get() - 1].0);   // 内部元素 → 所属文档的路径（.0）
    }
    let content = self.elements.get_by_loc(&location)?;
    if let Some(doc) = content.to_packed::<DocumentElem>() {
        Some(doc.path.as_ref())
    } else if let Some(asset) = content.to_packed::<AssetElem>() {
        Some(asset.path.as_ref())
    } else {
        None
    }
}
```

[crates/typst-bundle/src/introspect.rs:148-165](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/introspect.rs#L148-L165)。`path()` 是跨文档链接解析的基础：导出期 `LateLinkResolver` 要判断一个链接是「文档内」还是「跨文档」，靠的就是把链接目标的 location 喂给 `path()`，拿到它所属文档的路径（详见 u4-l1、u4-l2）。

#### 4.3.4 代码实践

**实践目标**：用一个具体例子验证「`path()` 能把任意 location 映射到正确的文档路径」，并理解 `document()` 与 `path()` 的「两层」结构。

**操作步骤**：

1. 读 [crates/typst-bundle/src/introspect.rs:148-165](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/introspect.rs#L148-L165) 的 `path()`，列出它对以下三种 location 各返回什么：
   - (a) `index.html` 文档内部某个标题的 location；
   - (b) `index.html` 这个 `#document` 元素自身的 location；
   - (c) 一个 `#asset("data.json", ...)` 元素自身的 location。
2. （可选，需 `--features bundle`）构造一个含跨文档链接的 bundle，编译后查看链接是否正确指向目标文件：

```typst
// 示例代码：跨文档链接（仅示意，需本地用 --features bundle 验证）
#document("index.html")[
  去 #link(<sec>)[详情页] 看看。       // sec 定义在 list.html 里
]
#document("list.html")[
  = 详情 <sec>
]
```

**需要观察的现象**：第 1 步——(a) 返回 `index.html` 的路径（走 `Some` 分支，`children[i-1].0`）；(b) 返回 `index.html` 的路径（走 `None` 分支，`DocumentElem.path`）；(c) 返回 `data.json` 的路径（走 `None` 分支，`AssetElem.path`）。三种情况都拿到正确路径，正是导出期链接解析能工作的前提。

**预期结果**：三种 location 都能被 `path()` 正确分类。若本地无法启用 bundle feature，第 2 步**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`BundleIntrospector::position()` 为什么不直接用 `self.elements.position()`，而要先 `child(location)?`？

> **参考答案**：`self.elements` 里元素的位置只是「子文档下标」（`Option<NonZeroUsize>`），不是真实的 `DocumentPosition`。要拿到「第几页 / DOM 哪个节点」这种真实位置，必须先路由到子内省器，由子内省器用自己的 `ElementIntrospector<PagedPosition>` / `<HtmlPosition>` 回答。

**练习 2**：`document()` 和 `path()` 都有一个「`Some` 走 children，`None` 走 get_by_loc」的两层结构。为什么需要第二层？

> **参考答案**：第一层处理「location 是某文档**内部**的元素」——返回它所属文档的 Location / 路径。但当一个 location 指向的**正是文档或资产自身**时（它们的顶层 tag 被登记为 `None` 位置），第一层走不通，需要第二层把该元素从 `elements` 取出来，判断它是 `DocumentElem` 还是 `AssetElem`，再返回其 `path` / `location`。没有第二层，就无法链接到「文档/资产本身」。

**练习 3**：单文档的 `PagedIntrospector::document()` 和 `path()` 都直接返回 `None`（见 [crates/typst-layout/src/introspect.rs:136-142](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-layout/src/introspect.rs#L136-L142)）。为什么 bundle 版本却要「认真实现」这两个方法？

> **参考答案**：单文档只有一个文件，没有「跨文档」概念，所以 `document` / `path` 无意义、返回 `None`。bundle 有多个文件，需要回答「这个元素属于哪个文档 / 哪个路径」，这是跨文档链接、跨文档内省的基础，所以 `BundleIntrospector` 必须认真实现它们。

---

## 5. 综合实践

**任务**：把本讲三个模块串起来，画一张「`BundleIntrospector` 全景图」，并用源码证据支撑图里每一处。

请产出一张包含以下要素的图（手绘或文字描述均可），并标注对应的源码行号：

1. **输入**：`bundle_impl` 传给 `BundleIntrospector::new(&items)` 的 `items`（[lib.rs:197](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L197)）。
2. **构造期**：`discover_item` 对 `Item::Tag` 打 `None` 位置（[introspect.rs:223](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/introspect.rs#L223)）、对 `Item::Document` 调 `discover_document` 给每个子文档元素打「下标 + 1」位置（[introspect.rs:236-248](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/introspect.rs#L236-L248)）。
3. **三个字段**：`children` / `elements` / `anchors`（[introspect.rs:22-34](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/introspect.rs#L22-L34)），其中 `anchors` 由 `set_anchors` 后置回填（[introspect.rs:65-67](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/introspect.rs#L65-L67)）。
4. **查询期三类路径**：目标无关 → `elements`（[introspect.rs:80-110](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/introspect.rs#L80-L110)）；位置相关 → `child()` 路由 + `Deref` 转发子内省器（[introspect.rs:69-76](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/introspect.rs#L69-L76)、[112-130](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/introspect.rs#L112-L130)、[192-201](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/introspect.rs#L192-L201)）；bundle 专属 → `path()` / `document()` / `anchor()`（[introspect.rs:132-165](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/introspect.rs#L132-L165)）。
5. **收敛**：`compile_impl` 用 `document.introspector()` 即 `BundleIntrospector` 当收敛判据（[typst/src/lib.rs:156-161](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst/src/lib.rs#L156-L161)）。

**验收标准**：图里每条箭头都能指向一个具体的源码行号；你能口头解释「一个跨文档链接的 location，如何从 `path()` 拿到目标文档路径，再被导出期 `LateLinkResolver` 解析成相对 URI」（这一步把本讲与 u4-l1/u4-l2 串起来）。

## 6. 本讲小结

- bundle 不让每个文档各自跑内省循环，而是用**一个统一内省器 `BundleIntrospector`**，让所有文档彼此可见、共同参与一次大的内省循环，收敛判据是整个 bundle 的内省稳定。
- `BundleIntrospector` 三个字段分工明确：`children` 存各子文档的（路径、子内省器、文档自身 Location）；`elements` 是合并了所有文档元素的 `ElementIntrospector<Option<NonZeroUsize>>`；`anchors` 是后置回填的跨文档链接锚点表。
- 「`elements` 的位置 = `children` 的 off-by-one 下标」是核心设计：`Some(n)` 表示元素在第 `n-1` 个子文档，`None` 表示顶层文档/资产自身；`child()` 用 `pos - 1` 还原下标完成路由。
- 用 `Option<NonZeroUsize>` 既表达了「顶层」语义，又因 niche 优化不增加内存。
- `ChildIntrospector` 通过 `Deref<Target = dyn Introspector>` 直接复用 `PagedIntrospector` / `HtmlIntrospector` 的实现，bundle 层无需重写位置相关查询。
- `Introspector` 的方法被分成三类：目标无关查询直接走 `elements`、位置相关查询先 `child()` 路由再转发、`path()`/`document()`/`anchor()` 由 bundle 专属实现；其中 `path()`/`document()` 的「两层」结构用 `None` 位置识别文档/资产自身。

## 7. 下一步学习建议

- **u5-l2 跨文档链接与锚点 `create_link_anchors`**：本讲多次提到 `anchors` 字段和 `link_targets()`，下一讲会讲清楚这些锚点是如何被收集、生成（HTML 注入 DOM id / paged 命名目的地）并回填到 `BundleIntrospector` 的。
- **u5-l3 并行与记忆化**：本讲强调了「先并行编译各文档、再统一建 `BundleIntrospector`」的顺序，下一讲会从 rayon + comemo 的角度解释为什么必须是这个顺序，以及它如何保证跨文档内省收敛的正确性。
- **延伸阅读**：可对照阅读 [crates/typst-layout/src/introspect.rs](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-layout/src/introspect.rs) 和 [crates/typst-html/src/introspect.rs](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-html/src/introspect.rs)，理解 `ChildIntrospector` 这两个变体各自的 `elements()` / `frame_link_targets()` 是怎么建出来的——这能帮你彻底看清「bundle 层只做路由与合并，真实工作都在子内省器」的分工。
