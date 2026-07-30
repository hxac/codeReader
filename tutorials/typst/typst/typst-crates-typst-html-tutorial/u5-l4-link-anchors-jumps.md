# 链接锚点与文档内跳转

## 1. 本讲目标

本讲聚焦 typst-html 中一个「事后修补」式的关键步骤：**为一个被文档内链接指向的元素，在最终 HTML 里分配一个可以跳转的锚点（fragment ID）**。

读完本讲，你应当能够：

1. 说清 `create_link_anchors` 在整个编译主链路里的位置与职责。
2. 理解 `Work` 队列如何用 `enqueue` / `drain` / `remove` 三个动作，把「需要 ID 的元素」与「它在 DOM 里实际变成的第一个节点」精确对应起来。
3. 复述 `traverse` 对 `Tag::Start` / `Tag::End` / `Element` / `Text` / `Frame` 五类节点的不同处理，尤其是「元素没有产生任何 DOM 节点」时如何插入一个空 `<span>` 兜底。
4. 解释 `AnchorGenerator` 如何尽量复用 Typst 标签（label）生成人类可读的 ID，并在标签重复时自动去重。
5. 理解嵌入帧（`html.frame` 里的 SVG）内部的链接跳转点是如何被记录成带坐标的锚点的。

本讲承接 u5-l3 的内省子系统：u5-l3 讲清了「查询 `link_targets()` 与 `set_anchors()` 的事后注入」，本讲就回答「这些 anchors 到底是怎么算出来、又怎么写回 DOM 的」。

## 2. 前置知识

在进入源码前，先用通俗语言对齐几个概念。

- **文档内链接（intra-doc link）**：Typst 里 `#link(<intro>)[跳转]` 或 `@intro` 这样的写法，会把链接指向某个带标签的元素。在 PDF 里它变成页码坐标；在 HTML 里，它需要变成一个 URL 片段（fragment），即 `#某个ID`。
- **fragment / 锚点 ID**：浏览器里 `https://site/page.html#section-1` 的 `#section-1` 部分。要让这个跳转生效，目标元素上必须有一个 `id="section-1"` 属性。本讲要解决的就是「这个 id 从哪来、写到哪」。
- **`Location` 与 `Label`**：`Location` 是 Typst 给每个可定位元素分配的稳定身份标识；`Label` 是用户写的 `<标签名>`。一个元素可以同时拥有二者，`Location` 一定有，`Label` 可选。
- **`Tag::Start` / `Tag::End`**：typst-html 的 DOM 节点列表里夹着两类「内省哨兵」`Tag`（详见 u5-l3 与 u2-l1）。`Tag::Start(elem, ..)` 标记某个可定位元素的内容「从这里开始」，`Tag::End(loc, ..)` 标记它「到这里结束」。它们本身**不会**输出任何 HTML，只是给内省用的。`Tag` 的定义在 typst-library：

```rust
pub enum Tag {
    Start(Content, TagFlags),
    End(Location, u128, TagFlags),
}
```

> 见 [crates/typst-library/src/introspection/tag.rs:12-24](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/tag.rs#L12-L24)。这是理解本讲的关键——`traverse` 正是靠这一对 `Start`/`End` 来追踪元素边界。

- **DOM 节点的四种变体**：回忆 u2-l1，`HtmlNode` 有 `Tag` / `Text` / `Element` / `Frame` 四种。其中只有后三种会真正出现在最终 HTML 里；`Tag` 是「幽灵节点」。

**核心直觉**：链接锚点分配之所以复杂，是因为「一个 Typst 元素」与「它在 HTML DOM 里变成的节点」并非一一对应。一个元素可能变成单个 HTML 元素、变成一段纯文本、变成多个节点，甚至**一个节点都不产生**（比如被 show 规则清空、或是纯元数据）。typst-html 必须为这四种情况各设计一条路径，保证「无论目标元素变成什么，链接总能落到一个真实存在的 DOM 节点上」。LinkElem 的官方文档把这四种情况说得很清楚：

> - 单个 HTML 元素 → id 直接挂在该元素上
> - 单个文本节点 → 把文本包进 `<span>`，id 挂在 span 上
> - 多个节点 → 第一个节点拿 id
> - 没有节点 → 生成一个空 `<span>` 当跳转目标

见 [crates/typst-library/src/model/link.rs:56-78](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/link.rs#L56-L78) 的「Links in HTML export」一节。本讲就是把这段描述翻译成源码。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [src/link.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/link.rs) | **本讲主角**。`create_link_anchors`、`traverse`、`traverse_frame`、`Work` 队列、`AnchorGeneratorExt` 全部在此。 |
| [src/document.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs) | 调用方。`html_document_impl` 在编译主链路末尾调用 `create_link_anchors` 并 `set_anchors`。 |
| [src/introspect.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/introspect.rs) | 提供 `link_targets()`（算出谁被链接）与 `set_anchors()` / `anchor()`（事后存取锚点）。u5-l3 已详述。 |
| [crates/typst-library/src/model/link.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/link.rs) | `AnchorGenerator`、`identify`、`can_use_label_as_id`、`disambiguate` 的定义。这是 ID 生成逻辑的真正实现，被 typst-html 复用。 |
| [src/dom.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs) | `HtmlFrame` 的 `id` 与 `anchors` 字段，帧内锚点最终存这里。 |

## 4. 核心概念与源码讲解

### 4.1 create_link_anchors：链接锚点的总入口

#### 4.1.1 概念说明

`create_link_anchors` 是 link.rs 对外暴露的唯一函数（在 lib.rs 中 [pub use](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/lib.rs#L24) 重导出）。它的任务是：给定一份已经编译好的 `HtmlDocument` 和「所有被链接命中的 Location 集合 `targets`」，遍历 DOM，为这些目标元素挂上人类可读的 `id`，并返回一张「Location → ID」的映射表。

它的签名与文档注释：

```rust
pub fn create_link_anchors(
    document: &mut HtmlDocument,
    targets: &FxHashSet<Location>,
) -> FxHashMap<Location, EcoString>
```

> 见 [src/link.rs:26-45](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/link.rs#L26-L45)。注释明确说明它「可能为目标是文本节点或空节点的链接生成 `<span>`」。

#### 4.1.2 核心流程

这个函数本身很薄，真正的活都在 `traverse` 里。它的流程是：

```text
create_link_anchors(document, targets):
  1. 若 targets 为空 → 直接返回空 map（快速短路）
  2. 新建一个空的工作队列 Work
  3. 取出文档内省器的共享引用（Arc::clone）
  4. 新建 AnchorGenerator（绑定该内省器）
  5. 调 traverse(Work, targets, AnchorGenerator, &mut 根元素的 children)
  6. 返回 Work.ids（Location → ID 映射）
```

注意第 5 步传入的是 `document.root_mut().children`——也就是对根 `<html>` 元素的孩子列表做**可变借用**。整个锚点挂载过程就是靠这个可变借用直接改写 DOM。

它在主链路里的位置：在 [html_document_impl](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L42-L73) 里，紧跟在 `html_document_common`（真正编译出 DOM 的核心，详见 u3-l1）之后：

```rust
let mut document = html_document_common(...)?;

// Assigns HTML fragment IDs to linked-to elements.
let targets = document.introspector().link_targets();
let anchors = crate::link::create_link_anchors(&mut document, &targets);
document.introspector_mut().set_anchors(anchors);
```

> 见 [src/document.rs:67-70](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L67-L70)。

这里有一个关键设计：**锚点分配必须在 DOM 编译完成之后做**，因为只有等 DOM 全部生成，才能知道每个被链接的元素「到底变成了哪种节点」。同时它又**必须在内省器定稿之后、`set_anchors` 之前**做——因为 `AnchorGenerator` 要用内省器的 `label_count` 来去重，而锚点本身要到 `set_anchors` 才写进内省器。这也正是 u5-l3 与 u6-l4 反复强调的「HtmlDocument 不实现 Hash、`root_mut` 会事后改 DOM」的来源之一：这一步确实在 memoize 缓存壳内部对 DOM 做了改写，但改写结果会随返回值一起被缓存。

#### 4.1.3 源码精读

```rust
pub fn create_link_anchors(
    document: &mut HtmlDocument,
    targets: &FxHashSet<Location>,
) -> FxHashMap<Location, EcoString> {
    if targets.is_empty() {
        return FxHashMap::default();
    }

    let mut work = Work::new();
    let introspector = Arc::clone(document.introspector());
    traverse(
        &mut work,
        targets,
        &mut AnchorGenerator::new(introspector.as_ref()),
        &mut document.root_mut().children,
    );
    work.ids
}
```

> 见 [src/link.rs:26-45](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/link.rs#L26-L45)。

几个要点：

- 第 30 行的 `targets.is_empty()` 短路是个重要优化：没有任何文档内链接时，整个遍历都免了。
- 第 37 行 `Arc::clone(document.introspector())` 只复制 `Arc` 指针（引用计数 +1），不复制内省器本身。`AnchorGenerator::new` 只需要一个 `&dyn Introspector`。
- 第 42 行 `document.root_mut().children` 取到根元素的可变孩子列表，`traverse` 会就地修改它。`root_mut` 的实现见 [src/document.rs:50-52](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L50-L52)，注释也坦承「改 root 可能搞乱内省器，待 issue #7951 修复」。

#### 4.1.4 代码实践

**实践目标**：确认 `create_link_anchors` 的调用时机与前置条件。

**操作步骤**：

1. 打开 [src/document.rs:42-73](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L42-L73)，定位 `html_document_impl`。
2. 回答：在调用 `create_link_anchors` 之前，`html_document_common` 已经完成了哪几步（参考 u3-l1 的 realize → convert_to_nodes → finalize_dom → resolve_inline_styles）？
3. 思考：为什么 `link_targets()` 必须在 `create_link_anchors` 之前调用，而 `set_anchors()` 必须在它之后？

**预期结果**：你能说清「DOM 必须先成型才能算 anchors，而 anchors 算完才能让内省器对外暴露」这个先后约束。

#### 4.1.5 小练习与答案

**练习 1**：如果一份 Typst 文档里没有任何文档内链接（只有外部 URL 链接），`create_link_anchors` 会遍历 DOM 吗？

**参考答案**：不会。`LinkElem::find_destinations` 只收集指向 `Destination::Location` 的链接，纯 URL 链接不会进入 `targets`；当 `targets.is_empty()` 为真时，函数在第 30 行直接返回空 map，连 `traverse` 都不调用。

**练习 2**：`create_link_anchors` 返回的映射表交给谁保存？

**参考答案**：交给 `document.introspector_mut().set_anchors(anchors)`（[document.rs:70](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L70)），存入 `HtmlIntrospector` 的 `anchors` 字段，之后通过 `Introspector::anchor(loc)` 查询（[introspect.rs:123-125](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/introspect.rs#L123-L125)）。

---

### 4.2 Work 队列：enqueue / drain / remove 的协同

#### 4.2.1 概念说明

`Work` 是 link.rs 内部的一个小结构，它解决的问题可以用一句话概括：**记住「哪些元素还欠一个 ID」，等遇到它们在 DOM 里的第一个真实节点时，再把 ID 发下去。**

为什么需要这么一个队列？因为 `traverse` 是线性扫描孩子列表的。当它看到一个 `Tag::Start(A)`（A 是被链接的元素）时，A 的 ID **还不能**马上挂——因为 A 在 DOM 里可能根本没有自己的元素节点（它可能只是一段文本的「逻辑容器」，或者干脆什么都不产生）。正确的做法是：**先把 A 记下来，继续往后扫，等撞见 A 范围内的第一个真实节点（Element/Text/Frame），再把 ID 挂到那个节点上**。如果一直扫到 `Tag::End(A)` 都没遇到真实节点，说明 A 啥也没产生，这时就补一个空 `<span>`。

`Work` 就承载这个「欠条」机制。

#### 4.2.2 核心流程

`Work` 有两个字段、三个动作：

```text
struct Work:
  queue: VecDeque<(Location, Option<Label>)>   # 欠条队列（元素位置 + 标签）
  ids:   FxHashMap<Location, EcoString>         # 最终结果：位置 → ID

动作：
  enqueue(loc, label)  # 开欠条：某元素需要 ID
  drain(f)             # 结算：把队列里【所有】欠条一次性换成同一个 ID，调用 f 决定 ID 并应用
  remove(loc, f)       # 定点结算：只结算队列里指定的那个 loc（用于空元素兜底）
```

`drain` 与 `remove` 的关键差异：

- **`drain`**：当遇到一个真实节点时调用。它取出队首欠条的 label，调用 `f(label)` 算出 ID 并把这个 ID 应用到「当前节点」上，然后把队列里**全部**欠条都映射到这个 ID，最后清空队列。
- **`remove`**：当遇到 `Tag::End(loc)` 时调用。它在队列里查找特定的 `loc`，如果找到了（说明这个元素从头到尾没遇到真实节点），就调用 `f(label)` 生成一个空 span 并插回去，只把这一个 `loc` 映射到新 ID。

#### 4.2.3 源码精读

```rust
struct Work {
    queue: VecDeque<(Location, Option<Label>)>,
    ids: FxHashMap<Location, EcoString>,
}

impl Work {
    fn new() -> Self {
        Self { queue: VecDeque::new(), ids: FxHashMap::default() }
    }

    fn enqueue(&mut self, loc: Location, label: Option<Label>) {
        self.queue.push_back((loc, label));
    }

    fn drain(&mut self, f: impl FnOnce(Option<Label>) -> EcoString) {
        if let Some(&(_, label)) = self.queue.front() {
            let id = f(label);
            for (loc, _) in self.queue.drain(..) {
                self.ids.insert(loc, id.clone());
            }
        }
    }

    fn remove(&mut self, loc: Location, f: impl FnOnce(Option<Label>) -> EcoString) {
        if let Some(i) = self.queue.iter().position(|&(l, _)| l == loc) {
            let (_, label) = self.queue.remove(i).unwrap();
            let id = f(label);
            self.ids.insert(loc, id.clone());
        }
    }
}
```

> 见 [src/link.rs:150-190](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/link.rs#L150-L190)。

几个精读要点：

- `drain` 只取**队首**的 label 来生成 ID（第 174 行 `self.queue.front()`），但会把**整队**欠条都映射到这一个 ID（第 176 行 `self.queue.drain(..)`）。这是处理「嵌套的被链接元素」的关键：若 A 包着 B，二者都被链接，且它们在 DOM 里的第一个真实节点是同一个，那么 A、B 应共享这一个 ID。`drain` 把队首 label 用作生成依据，然后让队里所有 loc 都指向同一 ID，正好实现这点。
- `drain` 里 `f` 只被调用一次（`FnOnce`），因为所有欠条共享同一个 ID、同一个节点。
- `remove` 用 `position` 在队列里线性查找指定 `loc`（第 184 行）。只有当该元素在 `Tag::End` 时仍在队列里（即从未被 `drain` 结算过），才会触发兜底。`f` 在这里负责「造一个空 span」。

#### 4.2.4 代码实践

**实践目标**：用纸笔推演 `Work` 在两种典型场景下的状态变化。

**操作步骤**：

场景一（元素 A 变成单个 `<div>`，B 嵌在 A 里也变成单个 `<span>`，二者都被链接）：

| 步骤 | 扫到的节点 | Work.queue 变化 | ids 变化 |
| --- | --- | --- | --- |
| 1 | `Tag::Start(A)` | enqueue A → `[A]` | — |
| 2 | `Tag::Start(B)` | enqueue B → `[A, B]` | — |
| 3 | `Element(div)` | drain：id₁ 挂到 div，`[A,B]` 全映射到 id₁ → `[]` | `{A:id₁, B:id₁}` |
| 4 | `Element(span)` | queue 空，drain 无操作 | 不变 |

场景二（元素 A 什么节点都不产生）：

| 步骤 | 扫到的节点 | Work.queue 变化 | ids 变化 |
| --- | --- | --- | --- |
| 1 | `Tag::Start(A)` | enqueue A → `[A]` | — |
| 2 | `Tag::End(A)` | remove(A)：造空 span，映射 A→id₂ → `[]` | `{A:id₂}` |

**需要观察的现象**：场景一里 A、B 共享 id₁；场景二里 A 单独拿到 id₂，且 DOM 里多了一个空 `<span>`。

**预期结果**：你能解释为什么场景一里 B 没有自己的独立 ID（因为它的第一个真实节点和 A 相同）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `drain` 用 `FnOnce` 而 `remove` 也是 `FnOnce`？能否改成 `FnMut` 多次调用？

**参考答案**：因为无论 `drain` 还是 `remove`，每个 ID 只应生成一次。`drain` 把整队欠条映射到同一个 ID，`f` 只调一次；`remove` 只为一个 loc 生成一个 ID。改成 `FnMut` 会语义错误：那意味着可能为同一节点生成多个不同 ID。

**练习 2**：如果队列里同时有 A、B 两个欠条，扫描时遇到一个真实节点触发了 `drain`，之后又遇到 `Tag::End(A)`，此时 `remove(A)` 会做什么？

**参考答案**：什么都不做。`drain` 已经把队列清空，`remove` 的 `position` 找不到 A，第 184 行的 `if let Some(i)` 不匹配，函数直接返回，A 不会被重复处理。

---

### 4.3 traverse：DOM 遍历与四类节点的处理

#### 4.3.1 概念说明

`traverse` 是 link.rs 的核心。它递归地扫描一个孩子列表，对每个节点按类型分派，把「欠条机制」与「DOM 改写」粘合在一起。它要处理的节点类型正是 u2-l1 定义的 `HtmlNode` 四变体（外加 `Tag` 的两个子变体），每一种对应 LinkElem 文档里的一种「目标变成什么」的情况。

#### 4.3.2 核心流程

```text
traverse(work, targets, generator, nodes):
  i = 0
  while i < len(nodes):
    node = &mut nodes[i]
    match node:
      Tag::Start(elem):  若 elem.location() 在 targets 里 → work.enqueue(loc, elem.label())
      Tag::End(loc):     work.remove(loc, 造空 span 并插回 i+1 处)
      Element(e):        work.drain(把 id 挂到 e 上); 递归 traverse(e.children)
      Text(..):          work.drain(把文本包进 <span>，id 挂 span，替换原文本节点)
      Frame(f):          work.drain(给 f.id 赋值); traverse_frame(f.inner, f.anchors)
    i += 1
```

五种分支对应五种语义：

1. **`Tag::Start`**：只登记欠条，不改 DOM（`Tag` 本来就不输出 HTML）。
2. **`Tag::End`**：空元素兜底——若该元素仍在队列，造空 `<span>`。
3. **`Element`**：把 id 挂到该元素，然后递归进它的孩子（孩子里可能还有被链接的元素）。
4. **`Text`**：把纯文本包进 `<span>` 再挂 id（因为文本节点没法直接挂属性）。
5. **`Frame`**：把 id 赋给 SVG 容器，并钻进帧内部处理帧内链接。

#### 4.3.3 源码精读

先看整体骨架与 `Tag` 两个分支：

```rust
fn traverse(
    work: &mut Work,
    targets: &FxHashSet<Location>,
    generator: &mut AnchorGenerator<'_>,
    nodes: &mut EcoVec<HtmlNode>,
) {
    let mut i = 0;
    while i < nodes.len() {
        let node = &mut nodes.make_mut()[i];
        match node {
            HtmlNode::Tag(Tag::Start(elem, _)) => {
                let loc = elem.location().unwrap();
                if targets.contains(&loc) {
                    work.enqueue(loc, elem.label());
                }
            }
            HtmlNode::Tag(Tag::End(loc, _, _)) => {
                work.remove(*loc, |label| {
                    let mut element = HtmlElement::new(tag::span);
                    let id = generator.assign(&mut element, label);
                    nodes.insert(i + 1, HtmlNode::Element(element));
                    id
                });
            }
            // ... Element / Text / Frame 分支见下 ...
        }
        i += 1;
    }
}
```

> 见 [src/link.rs:48-78](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/link.rs#L48-L78)。

要点：

- 用 `while i < nodes.len()` 而非 `for`，因为 `Tag::End` 分支会 `nodes.insert(...)` 改变列表长度——若用迭代器会在插入后失效。插入位置是 `i + 1`（紧挨着 `Tag::End` 之后），这样空 span 落在该元素逻辑范围的尾部。
- 第 62 行 `elem.location().unwrap()`：能进 `Tag::Start` 的 Content 必有 Location（见 tag.rs 的注释），所以 `unwrap` 安全。

接着看 `Element` 与 `Text` 两个「把 id 发下去」的分支：

```rust
HtmlNode::Element(element) => {
    work.drain(|label| generator.assign(element, label));
    traverse(work, targets, generator, &mut element.children);
}

HtmlNode::Text(..) => {
    work.drain(|label| {
        let mut element =
            HtmlElement::new(tag::span).with_children(eco_vec![node.clone()]);
        let id = generator.assign(&mut element, label);
        *node = HtmlNode::Element(element);
        id
    });
}
```

> 见 [src/link.rs:82-97](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/link.rs#L82-L97)。

- `Element` 分支调 `generator.assign(element, label)` 直接在已有元素上挂 id，然后**递归**进它的孩子——这一点很重要：嵌套元素里的链接目标会在递归层被处理。
- `Text` 分支不能直接挂属性，于是把原文本 `node.clone()` 塞进一个新建的 `<span>`，给 span 挂 id，再用 `*node = HtmlNode::Element(...)` 把原来的文本节点原地替换成这个 span。

最后看 `Frame` 分支：

```rust
HtmlNode::Frame(frame) => {
    work.drain(|label| {
        frame.id.get_or_insert_with(|| generator.identify(label)).clone()
    });
    traverse_frame(
        work, targets, generator, &frame.inner, &mut frame.anchors,
    );
}
```

> 见 [src/link.rs:101-112](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/link.rs#L101-L112)。

- 帧本身用一个 `id` 字段（[dom.rs:513](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L513)）。`get_or_insert_with` 的语义是「已有 id 就复用，没有才生成」——这样若用户或别的逻辑已给帧设过 id，不会覆盖。
- 之后调 `traverse_frame` 钻进 SVG 内部处理帧内链接（见 4.5 节）。

#### 4.3.4 代码实践（本讲的主实践）

**实践目标**：亲手追踪「被链接的元素未产生任何 DOM 节点」时，`traverse` 如何靠 `Tag::End` 兜底插入空 `<span>`。

**操作步骤**：

1. 假设有这样一段 DOM 孩子列表（元素 A 被链接，但其内容被 show 规则清空，于是 Start/End 之间没有任何 Element/Text/Frame）：

   ```text
   [ Tag::Start(A),  Tag::End(A),  Element(div),  ... ]
   ```

2. 逐步模拟 `traverse`（参考 4.3.3 的源码）：
   - `i=0`：`Tag::Start(A)`，A 在 targets 中 → `work.enqueue(loc_A, A.label)`，队列 `[A]`。`i` → 1。
   - `i=1`：`Tag::End(loc_A)` → 调 `work.remove(loc_A, |label| {...})`。`remove` 在队列找到 A（index 0），取出它，执行闭包：新建空 `HtmlElement::new(tag::span)`，`generator.assign` 给它挂 id（假设得 `loc-1`），然后 `nodes.insert(1+1=2, Element(span))`。列表变为：

     ```text
     [ Tag::Start(A),  Tag::End(A),  Element(span#loc-1),  Element(div),  ... ]
     ```

     并写入 `ids { loc_A: "loc-1" }`。`i` → 2。
   - `i=2`：现在扫到刚插入的空 `Element(span)`，队列已空，`drain` 不动作，递归进空孩子立即返回。`i` → 3。
   - 继续……

3. **需要观察的现象**：空 `<span>` 被插在 `Tag::End(A)` **之后**（位置 `i+1`），即元素 A 逻辑范围的末尾。最终 HTML 里会出现一个 `<span id="loc-1"></span>`，链接 `#loc-1` 就能跳到这里。

**预期结果**：你能复述「`remove` 在队列里命中 → 造空 span → insert 到 End 之后 → 映射 loc→id」这条完整链路，并解释为何空 span 必须插在 End 之后而不是 Start 之后（因为此时才算确认元素确实没有产出节点）。

**本地验证（可选）**：想亲眼看这个空 span，可写一个让目标元素不产出内容的 Typst 例子，例如把标签贴在一个被 show 规则替换为空内容的元素上，再用 `#link` 指向它，编译为 HTML 后在输出里搜索空的 `<span id=...>`。具体的元素选择与最终 DOM 结构**待本地验证**（不同元素的渲染行为可能不同），但「空 span 出现在 End 之后」这一规律由源码保证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `traverse` 用 `while` + 手动下标，而不用 `for node in nodes.iter_mut()`？

**参考答案**：因为 `Tag::End` 分支会 `nodes.insert(...)` 改变向量长度与元素位置，借用检查器也不允许在持有 `&mut nodes[i]` 的同时再 `&mut nodes` 去插入。手动下标配合 `nodes.make_mut()` 与 `insert(i+1, ...)` 才能安全地边遍历边插入，且插入后继续从正确的位置扫描。

**练习 2**：`Text` 分支用 `*node = HtmlNode::Element(element)` 原地替换，而不是 `nodes.insert`。这两种写法在这里等价吗？

**参考答案**：基本等价但有细微差别。`*node = ...` 是**替换**当前下标的节点（长度不变），而 `insert` 会**新增**一个节点并把后续元素后移。这里目标是「把文本变成包着文本的 span」，节点数量不变，所以用替换更准确；若用 insert 还得额外删掉原文本节点，反而啰嗦。

---

### 4.4 AnchorGenerator：人类可读 ID 的生成与去重

#### 4.4.1 概念说明

到目前为止，我们把 id 的「发牌」机制讲清了，但「id 到底长什么样」还没展开。这部分逻辑不在 typst-html，而在 typst-library 的 `AnchorGenerator`——typst-html 直接复用它。设计目标有两个：

1. **尽量人类可读**：如果被链接的元素带了 Typst 标签 `<intro>`，生成的 id 最好是 `intro`，而不是 `loc-7`。
2. **全局唯一**：HTML 里 id 不能重复。若同一标签出现多次（比如多个 `<fig>`），要用后缀去重；若元素没标签，就用 `loc-序号` 兜底。

LinkElem 文档把规则总结得很清楚（[link.rs:80-102](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/link.rs#L80-L102)）：标签「可复用」要求全是字母数字/连字符/下划线、且不以数字或连字符开头；可复用且唯一就直接用；可复用但不唯一就加 `-序号`；否则用 `loc-序号`。

#### 4.4.2 核心流程

`AnchorGenerator` 维护两个计数器：

```text
struct AnchorGenerator:
  introspector:    &dyn Introspector   # 用来查 label_count 判断标签是否唯一
  loc_counter:     usize               # 给无标签元素编号
  label_counter:   FxHashMap<Label, usize>  # 给重复标签编号

identify(label):
  if 有标签 且 标签文本可用(can_use_label_as_id):
      if 该标签在文档里唯一(label_count == 1): 直接返回标签文本
      else: label_counter[label] += 1; 返回 "标签-序号"（disambiguate 防与现有标签撞）
  else:
      loc_counter += 1; 返回 "loc-序号"
```

去重函数 `disambiguate` 还有一层保护：即便加后缀，也要确认 `标签-序号` 不与文档里**已有的某个 Typst 标签**重名（比如恰好有人写了 `<mylabel-1>` 标签），若重名就继续递增序号。

#### 4.4.3 源码精读

`AnchorGenerator` 与 `identify`：

```rust
pub struct AnchorGenerator<'a> {
    introspector: &'a dyn Introspector,
    loc_counter: usize,
    label_counter: FxHashMap<Label, usize>,
}

impl<'a> AnchorGenerator<'a> {
    pub fn new(introspector: &'a dyn Introspector) -> Self {
        Self { introspector, loc_counter: 0, label_counter: FxHashMap::default() }
    }

    pub fn identify(&mut self, label: Option<Label>) -> EcoString {
        if let Some(label) = label {
            let resolved = label.resolve();
            let text = resolved.as_str();
            if can_use_label_as_id(text) {
                if self.introspector.label_count(label) == 1 {
                    return text.into();
                }
                let counter = self.label_counter.entry(label).or_insert(0);
                *counter += 1;
                return disambiguate(self.introspector, text, counter);
            }
        }
        self.loc_counter += 1;
        disambiguate(self.introspector, "loc", &mut self.loc_counter)
    }
}
```

> 见 [crates/typst-library/src/model/link.rs:515-555](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/link.rs#L515-L555)。

辅助函数：

```rust
fn can_use_label_as_id(label: &str) -> bool {
    !label.is_empty()
        && label.chars().all(|c| c.is_alphanumeric() || matches!(c, '-' | '_'))
        && !label.starts_with(|c: char| c.is_numeric() || c == '-')
}

fn disambiguate(introspector: &dyn Introspector, text: &str, counter: &mut usize) -> EcoString {
    loop {
        let disambiguated = eco_format!("{text}-{counter}");
        if PicoStr::get(&disambiguated)
            .and_then(Label::new)
            .is_some_and(|label| introspector.label_count(label) > 0)
        {
            *counter += 1;
        } else {
            break disambiguated;
        }
    }
}
```

> 见 [crates/typst-library/src/model/link.rs:562-586](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/link.rs#L562-L586)。

要点：

- `can_use_label_as_id` 比规范的 CSS 标识符规则更严格（注释明说「slightly more restrictive, but easier to explain」），既要是合法 CSS 标识符、也要是合法 URL fragment。
- `disambiguate` 的循环只在「生成的候选 id 恰好等于文档里某个已存在标签」时才递增，避免一种隐蔽碰撞：用户既有 `<mylabel>`（重复，需去重为 `mylabel-1`）又恰好有 `<mylabel-1>` 标签。

现在把视角拉回 typst-html：`identify` 是「无副作用地算出一个 id 字符串」，但真正把它挂到元素上的是 link.rs 的扩展 trait `AnchorGeneratorExt::assign`：

```rust
impl AnchorGeneratorExt for AnchorGenerator<'_> {
    fn assign(&mut self, element: &mut HtmlElement, label: Option<Label>) -> EcoString {
        element.attrs.get(attr::id).cloned().unwrap_or_else(|| {
            let id = self.identify(label);
            element.attrs.push_front(attr::id, id.clone());
            id
        })
    }
}
```

> 见 [src/link.rs:197-205](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/link.rs#L197-L205)。

`assign` 多了一层「复用已有 id」的逻辑：若元素**已经有** `id` 属性（比如用户手写 `html.elem("div", id: "foo")`），就直接用它（`.get(attr::id).cloned()`），不再生成；否则才调 `identify` 生成并用 `push_front` 把 id 放到属性列表最前面（id 习惯上排在最前）。`attr::id` 是 attr.rs 里预定义的常量（[attr.rs:114](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/attr.rs#L114)）。

#### 4.4.4 代码实践

**实践目标**：预测几种标签场景下 `identify` 的返回值。

**操作步骤**：假设文档里标签分布如下，推演 `identify` 的输出（`label_count` 表示该标签在文档里出现的次数）：

| 输入 label | label_count | `identify` 输出 | 理由 |
| --- | --- | --- | --- |
| `<intro>` | 1 | `intro` | 可用作 id 且唯一 |
| `<fig>`（第 1 次调用） | 3 | `fig-1` | 可用但不唯一，加序号 |
| `<fig>`（第 2 次调用） | 3 | `fig-2` | 同一 label_counter 续编 |
| `<1st>` | — | `loc-1` | 以数字开头，不可用作 id，走 loc 兜底 |
| `<my-label>` | 1 | `my-label` | 含连字符但合法且唯一 |
| 无标签 | — | `loc-1` | 无 label，走 loc 兜底 |

**需要观察的现象**：`label_counter` 是按 `Label`（不是按文本）记账的，所以同一个标签的多次调用序号连续递增；无标签元素共用 `loc_counter`。

**预期结果**：你能解释为什么「无标签」和「标签不可用作 id」都落到同一条 `loc-N` 路径。

> 注：上表中 `label_count == 3` 时返回 `fig-1` 而非 `fig`，是因为 `identify` 在 `label_count != 1` 时无条件进入去重分支（即使这是该标签第一次被 `identify` 调用）。序号从 1 起，是因为 `counter` 先自增再使用（`*counter += 1` 在 `disambiguate` 之前）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `assign` 要先检查元素是否已有 `id`？

**参考答案**：为了尊重用户已显式设置的 id（如 `html.elem("section", id: "overview")`），避免覆盖。这也让生成的锚点更稳定——用户自定的 id 不会被编译器改写。

**练习 2**：`disambiguate` 里为何要 `PicoStr::get(&disambiguated).and_then(Label::new)`？直接比较字符串不行吗？

**参考答案**：它要判断「候选 id 是否等于文档里某个**已存在的 Typst 标签**」，而内省器只能按 `Label`（驻留的 PicoStr）查询 `label_count`，不能按任意字符串查。所以得先把候选字符串 intern 成 Label 再查。只要 `label_count > 0` 就说明这个候选 id 会和一个真实标签重名，必须继续递增。

---

### 4.5 traverse_frame：嵌入帧内部的 SVG 锚点

#### 4.5.1 概念说明

前面四节处理的都是「正常 DOM 树」里的链接。但 typst-html 还有一条特殊路径：`html.frame`。当一个被链接的元素落在 `html.frame` 包裹的内容里时，它在 DOM 里不是普通元素，而是被排版引擎画进了一个 `Frame`，最终渲染成内联 SVG。SVG 内部没有「元素 id」的概念，跳转要靠 SVG 里的坐标点（`<a>` 或命名的跳转点）。

`traverse_frame` 负责把帧内被链接的目标记录成「**坐标 + id**」对，存进 `HtmlFrame.anchors`，最终由编码阶段交给 `typst_svg::svg_in_html` 生成 SVG 内的跳转锚点。

#### 4.5.2 核心流程

```text
traverse_frame(work, targets, generator, frame, anchors):
  for (pos, item) in frame.items():
    match item:
      FrameItem::Tag(Tag::Start(elem)):
        loc = elem.location()
        if loc ∈ targets:
          查 introspector.position(loc) 得到 HtmlPosition
          若其 details() 是 InnerHtmlPosition::Frame(point):
            id = generator.identify(elem.label())
            work.ids[loc] = id
            anchors.push((point, id))     # 记录「SVG 内坐标 point → id」
      FrameItem::Group(group):
        traverse_frame(..., &group.frame, anchors)   # 递归进分组
      其它(Text/Shape/Image/Link): 忽略
```

注意：`traverse_frame` **不**用 `Work` 队列，而是直接把结果写进 `work.ids` 和 `anchors`。因为帧内每个可定位项都有一个明确的 `pos`（坐标），不存在「变成多个节点 / 不产生节点」的歧义——每个被链接的帧内目标直接对应一个坐标点。

#### 4.5.3 源码精读

```rust
fn traverse_frame(
    work: &mut Work,
    targets: &FxHashSet<Location>,
    generator: &mut AnchorGenerator<'_>,
    frame: &Frame,
    anchors: &mut EcoVec<(Point, EcoString)>,
) {
    for (_, item) in frame.items() {
        match item {
            FrameItem::Tag(Tag::Start(elem, _)) => {
                let loc = elem.location().unwrap();
                if targets.contains(&loc)
                    && let Some(DocumentPosition::Html(position)) =
                        generator.introspector().position(loc)
                    && let Some(InnerHtmlPosition::Frame(point)) = position.details()
                {
                    let id = generator.identify(elem.label());
                    work.ids.insert(loc, id.clone());
                    anchors.push((*point, id));
                }
            }
            FrameItem::Group(group) => {
                traverse_frame(work, targets, generator, &group.frame, anchors);
            }
            _ => {}
        }
    }
}
```

> 见 [src/link.rs:120-147](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/link.rs#L120-L147)。

要点：

- 三个 `if let` 链式条件（Rust 的 `let` chains）层层收紧：目标在被链接集合里 → 它的 `position` 是 HTML 位置 → 该位置的细节是「帧内坐标 `Frame(point)`」。只有三者都满足，才登记锚点。回忆 u5-l3：`HtmlPosition` 的 `details()` 返回 `InnerHtmlPosition`，其中 `Frame(Point)` 表示「这是帧内某个坐标」（见 [position.rs:160-167](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/position.rs#L160-L167) 与 [position.rs:138-143](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/position.rs#L138-L143) 的 `in_frame`）。
- `point` 来自内省器在 `discover_frame` 阶段记录的坐标（u5-l3 已讲），所以 `traverse_frame` 实际是在「消费」内省器先前算好的位置信息。
- `anchors.push((*point, id))`：把 `(坐标, id)` 追加到 `HtmlFrame.anchors`。这个字段在 dom.rs 定义（[dom.rs:517-518](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L517-L518)），编码时由 `write_frame` 透传给 SVG 生成器：

```rust
fn write_frame(w: &mut Writer, frame: &HtmlFrame) {
    let svg = typst_svg::svg_in_html(
        &frame.inner,
        frame.text_size,
        w.pretty,
        frame.id.as_deref(),
        &eco_format!("{}", frame.css.to_inline()),
        &frame.anchors,
        w.link_resolver,
    );
    ...
}
```

> 见 [src/encode.rs:391-400](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/encode.rs#L391-L400)。`frame.id.as_deref()` 是帧（整段 SVG）的 id（4.3 节 Frame 分支赋的值），`&frame.anchors` 是帧内各跳转点的坐标-id 对。

- `FrameItem::Group` 递归处理分组（group 有自己的 transform，但 `traverse_frame` 这里没传累积变换 `ts`，坐标已由内省器在 `discover_frame` 时算好，所以直接用 `group.frame`）。

#### 4.5.4 代码实践

**实践目标**：理清「帧内链接目标」从坐标到 SVG 锚点的数据流。

**操作步骤**：

1. 画一条数据流箭头图：
   - `introspect.rs::discover_frame`（u5-l3）→ 把帧内可定位项的 `HtmlPosition` 记成 `in_frame(point)`
   - `link.rs::traverse_frame`（本节）→ 查 `position(loc).details()` 拿到 `point`，连同生成的 `id` 存进 `HtmlFrame.anchors`
   - `encode.rs::write_frame` → 把 `anchors` 传给 `typst_svg::svg_in_html`
   - `typst-svg` → 在 SVG 内对应坐标处生成可跳转的命名锚点

2. 回答：为什么 `traverse_frame` 里不调 `work.enqueue` / `drain`？

**需要观察的现象**：帧内锚点的「坐标」完全来自内省器（u5-l3 的 `discover_frame`），`traverse_frame` 只是把坐标和新生成的 id 配对；它不依赖 DOM 节点遍历，所以不需要队列。

**预期结果**：你能解释「帧内一个被链接元素 → SVG 内一个坐标锚点」的完整链路，并说出 `HtmlFrame.anchors` 在其中扮演的「中转容器」角色。

#### 4.5.5 小练习与答案

**练习 1**：`traverse_frame` 的 `for (_, item)` 里第一个字段（下划线）是什么？为什么不用？

**参考答案**：`frame.items()` 返回 `(Point, FrameItem)`，第一个字段是该 item 在帧内的位置偏移 `pos`。这里不用它，是因为真正要存进 `anchors` 的坐标来自内省器的 `HtmlPosition.details()`（即 `InnerHtmlPosition::Frame(point)`），那个 point 已经是经 `discover_frame` 累积变换后的最终坐标，比 item 自身的局部 `pos` 更准确。

**练习 2**：如果一个被链接的元素既不在 targets 里、也不在帧内，`traverse_frame` 会怎么处理？

**参考答案**：`targets.contains(&loc)` 为假，整个 `if` 链不执行，什么也不记录。`traverse_frame` 只关心「被链接且位于帧内」的目标，其余一律忽略。

---

## 5. 综合实践

把本讲五个最小模块串起来，做一个端到端的追踪任务。

**任务**：给定下面这段 Typst 源（一份含文档内链接、且链接目标多样的小文档），预测 typst-html 为每个链接目标生成的 id，以及对应的 DOM 改写动作。

```typst
= 引言 <intro>

见 #link(<intro>)[回到引言] 与 #link(<empty>)[跳到空目标]。

// metadata 通常不产生可见 DOM 节点
#metadata("k", "v") <empty>

= 图表 <fig>
```

**操作步骤**：

1. 先列出 `link_targets()` 会收集到哪些 Location（提示：两个 `#link` 指向 `<intro>` 与 `<empty>`）。
2. 对 `<intro>`：它变成单个 `<h2>` 元素。追踪 `traverse`：`Tag::Start(引言)` → enqueue → 撞见 `Element(h2)` → `drain` → `assign(h2, Some(<intro>))`。因为 `intro` 合法且（假设）唯一，`identify` 返回 `intro`，h2 得到 `id="intro"`。
3. 对 `<empty>`：它不产生可见节点。追踪 `traverse`：`Tag::Start(empty)` → enqueue → 直接到 `Tag::End(empty)` → `remove` 造空 `<span>`，`identify(None)`（metadata 无可见标签或标签不可用）返回 `loc-1`，span 得到 `id="loc-1"`，插在 End 之后。
4. 核对 `Work.ids` 最终应为 `{ loc_intro: "intro", loc_empty: "loc-1" }`，并经 `set_anchors` 存入内省器。
5. 推演链接解析：`#link(<intro>)` 在编码时通过内省器查 `anchor(loc_intro)` 得 `intro`，渲染成 `<a href="#intro">`；`#link(<empty>)` 得 `loc-1`，渲染成 `<a href="#loc-1">`。

**需要观察的现象**：两类目标分别走了 `Element`（drain + assign）与 `Tag::End`（remove + 空 span）两条不同分支，但最终都产出了可跳转的 id。

**预期结果**：你能完整复述「targets → traverse 分派 → Work/drain/remove → assign/identify → ids → set_anchors → 编码时 anchor() 查询 → href」这条贯穿全讲的链路。

> 说明：`<empty>` 贴在 `metadata` 上是否真的「不产生任何 DOM 节点」**待本地验证**——它取决于 metadata 元素在 HTML 转换阶段的具体行为。若实际产生了节点，则会走 Text/Element 分支而非空 span 分支；但无论走哪条，`traverse` 的分派逻辑本身是确定的，可作为阅读源码的练习基准。

## 6. 本讲小结

- `create_link_anchors` 是编译主链路末尾（`html_document_impl` 内）的事后步骤：先 `link_targets()` 算出被链接目标，再遍历 DOM 挂 id，最后 `set_anchors()` 写回内省器。
- `Work` 队列用 `enqueue`（开欠条）/ `drain`（结算全部欠条到首个真实节点）/ `remove`（为空元素定点兜底）三个动作，把「被链接元素」与「它在 DOM 里的首个真实节点」精确对应。
- `traverse` 对五类节点分派：`Tag::Start` 登记、`Tag::End` 插空 span、`Element` 挂 id 并递归、`Text` 包 span、`Frame` 设帧 id 并进帧内处理。
- 空元素兜底的关键是 `Tag::End` 分支：元素若从未被 `drain` 结算，`remove` 就在 End 之后插入一个空 `<span>` 并分配 id。
- `AnchorGenerator::identify` 优先复用合法且唯一的 Typst 标签，重复标签加 `-序号`，无标签或标签非法用 `loc-序号`；`disambiguate` 还防止与已存在标签撞名。typst-html 的 `assign` 在此基础上尊重元素已有的 `id`。
- `traverse_frame` 处理 `html.frame` 内的链接：直接把内省器算好的帧内坐标与生成的 id 配对存进 `HtmlFrame.anchors`，编码时交给 SVG 生成器，不走 Work 队列。

## 7. 下一步学习建议

- **u5-l5（数学公式到 MathML）**：同样涉及「Typst 结构 → 另一种标记语言」的转换，可与本讲的 DOM 改写对照阅读。
- **u6-l1（html.frame 与 SVG 嵌入）**：本讲 4.5 节只讲了帧内锚点的「记账」，帧如何排版、如何编码成 SVG 的完整链路在 u6-l1 展开。
- **u6-l4（缓存与 comemo memoization）**：本讲提到 `create_link_anchors` 在 memoize 壳内改写 DOM，这与 `HtmlDocument` 不实现 `Hash` 的根因密切相关，u6-l4 会系统讨论这个设计取舍。
- 继续阅读源码时，建议顺着 `LinkElem::find_destinations`（[model/link.rs:211-222](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/link.rs#L211-L222)）与 `EarlyLinkResolver`（[model/link.rs:588-639](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/link.rs#L588-L639)）追到编码侧，把「anchor 怎么变成 href」的另一半链路补全。
