# Location、Locator、Tag 与 query/locate/here、Context

## 1. 本讲目标

Typst 能让你「在文档里查文档」：列出所有标题、读出当前页码、根据某个元素的位置决定排版。这套能力叫做**内省（introspection）**。本讲是「内省与上下文」单元的第一篇，只打地基——讲清楚「文档里的位置」到底是怎么表示、怎么分配、怎么被用户函数访问的。

学完后你应该能够：

- 说清 `Location` 为什么是一个 `u128` 哈希，它如何在多次编译迭代之间保持稳定、又能唯一标识一个元素。
- 看懂 `Tag` / `TagElem` 如何把「这个元素在这里」这件事写进排版帧树，让下一轮迭代的内省器能查到它。
- 理解 `Locator` / `SplitLocator` 这个分配器的设计：为什么用「分层哈希」而不是自增计数器。
- 掌握 `query`、`locate`、`here` 三个用户函数的共同套路——先过「门禁」再做查询。
- 解释 `Context` 这个「门禁」：为什么「没有 context 就无法 introspect」，以及那个经典报错是如何产生的。

本讲只讲**表示与入口**，不深入「为什么需要反复迭代才稳定」（那是 u9-l3 收敛循环的内容），也不展开 `Counter` / `State`（u9-l2）。

## 2. 前置知识

在开始之前，建议你已经了解（这些都在前序讲义中讲过）：

- **`Content` 是所有标记与函数调用的产物**，它内部有一块 `Meta` 存放 `span`、`label`、`location` 等元信息（u3-l1）。本讲会反复用到 `content.location()`，它读的就是这块 `Meta`。
- **元素可以具备「能力（capability）」**（u3-l2）。其中 `Locatable` 是「能被自动分配位置」的能力，`Unqueriable`/`Tagged` 也和内省相关。
- **`#[elem]` 宏生成的字段标注**（u3-l3），尤其是 `#[required]`、`#[internal]`。
- **`#[func]` 宏把 Rust 函数变成标准库函数**，`#[func(contextual)]` 标志表示「这个函数依赖上下文」（u3-l4）。
- **`Engine` 是编译期中央上下文**，它聚合 `world` / `library` / `introspector` 等数据，随求值/排版一路传递（u5-l2）。
- **`comemo` 的 `tracked` / `Tracked`**：把一个值或 trait 对象包装成「可被增量编译追踪」的形式，是 Typst 增量编译的根基（u5-l1、u12-l2）。

几个直觉性的比喻先放在这里，后面会逐一对照源码：

- **`Location`** = 一个元素在整篇文档里的「身份证号」，是一个 128 位哈希。
- **`Tag` / `TagElem`** = 在排版产物（帧树）里打下的「书签」，写着「身份证号 X 的元素从这里开始 / 到这里结束」。
- **`Locator`** = 一台「身份证号发号机」，在排版时给每个 locatable 元素发号。
- **`Introspector`** = 上一轮排版留下的「总索引」，按身份证号或标签反查元素。
- **`Context`** = 进入内省大厅前的「门禁卡」，没有卡就进不去。

## 3. 本讲源码地图

本讲涉及的关键文件都在 `src/introspection/` 下，外加一个 `src/foundations/context.rs`：

| 文件 | 作用 |
| --- | --- |
| [src/introspection/location.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/location.rs) | 定义 `Location`（一个 `u128`）、`LocationKey`，以及若干「按位置查询」的内省结构体（页码、坐标等）。 |
| [src/introspection/tag.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/tag.rs) | 定义 `Tag`（`Start`/`End`）、`TagFlags`、`TagElem`：把已实现的 locatable 元素「盖章」进帧树。 |
| [src/introspection/locator.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/locator.rs) | 定义 `Locator`、`SplitLocator`、`LocatorLink`：排版期的「位置发号机」。 |
| [src/introspection/introspector.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/introspector.rs) | 定义 `Introspector` trait 与其默认实现 `ElementIntrospector`，把 `Tag` 收集成索引并回答查询。 |
| [src/introspection/query.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/query.rs) | 用户函数 `query`，以及 `QueryIntrospection` 等内省结构体。 |
| [src/introspection/locate.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/locate.rs) | 用户函数 `locate`。 |
| [src/introspection/here.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/here.rs) | 用户函数 `here`。 |
| [src/foundations/context.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/context.rs) | 定义 `Context`（门禁卡）、`ContextElem`、`CONTEXT_RULE`。 |
| [src/introspection/convergence.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/convergence.rs) | 定义 `Introspect` trait 与 `History`：本讲只用到它的「记录 + 收敛诊断」骨架，细节留到 u9-l3。 |
| [src/engine.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs) | `Engine::introspect`：所有用户查询最终都走的「统一入口」。 |

另外会少量引用 [src/foundations/selector.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/selector.rs)（`LocatableSelector`）和 [src/foundations/content/mod.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/mod.rs)（`Content::location`）。

---

## 4. 核心概念与源码讲解

本讲的四个最小模块：

- **4.1 Location 与 Tag**：位置的底层表示。
- **4.2 Locator 与 SplitLocator**：位置如何被分配。
- **4.3 query / locate / here**：用户可见的内省入口。
- **4.4 Context**：内省的门控。

建议按 4.1 → 4.2 → 4.3 → 4.4 的顺序读：先知道「位置是什么」，再知道「位置从哪来」，再用三个用户函数提出疑问，最后在 4.4 里彻底搞懂那道「门禁」。

### 4.1 Location 与 Tag：文档位置的底层表示

#### 4.1.1 概念说明

要「查文档里的元素」，首先得有一个**稳定且唯一**的方式来指代「那个元素」。普通的数组下标不行——插入一个元素就会让后面所有下标错位；元素自身的内容也不行——同一个标题可能在文档里出现多次。

Typst 的选择是：给每个**locatable 元素**（具备 `Locatable` 能力的元素，或被打了 `<label>` 的元素）分配一个 `Location`。它本质上就是一个 128 位哈希，扮演「身份证号」的角色。它需要同时满足两个看似矛盾的要求：

1. **跨迭代稳定**：排版要反复多次（收敛循环），同一元素在第 1 轮和第 5 轮必须拿到**同一个** `Location`，否则 `query` 永远查不出稳定结果。
2. **跨编辑稳定**：在文档中间加一行字，不应该让后面所有元素的 `Location` 全变，否则增量编译就失效了。

而 `Tag` / `TagElem` 则负责把这个身份证号「盖章」到排版产物里：locatable 元素在实现（realization）阶段拿到 `Location` 后，会在帧树里留下一个 `Tag`，标明「身份证号 X 的元素从这一帧开始 / 到这一帧结束」。下一轮迭代的 `Introspector` 扫描这些 `Tag`，就建起了「身份证号 → 元素」的索引。

> 注意：本 crate 只负责**定义** `Location`/`Tag` 这些类型，真正「在排版时发号 + 收集 tag + 建索引」的算法住在行为 crate（`typst-realize`/`typst-layout` 等），运行期经 `Routines` 回调（见 u5-l4）。本讲关注类型与接口，不涉及具体发号调度。

#### 4.1.2 核心流程

一条 locatable 元素从「诞生」到「能被查到」的全过程：

```text
源码 content（尚无 location）
   │  （排版/实现阶段，typst-realize 调用）
   ▼
Locator/SplitLocator 发号  ──►  Location(u128)
   │
   ▼
content.set_location(loc)   （Meta.location 从 None 变 Some，见 4.1.3）
   │
   ▼
用 TagElem 把 Tag{Start(content), End(loc,key)} 塞进帧树 Frame
   │  （本轮排版结束）
   ▼
Introspector 扫描所有 Tag  ──►  建索引：loc → content, label → content
   │  （下一轮迭代）
   ▼
用户 query/locate/here  ──►  经 Introspector 查索引  ──►  返回带 location 的 content
```

关键点：**第 N 轮排版时，用户代码看到的是第 N-1 轮建好的索引**。这就是为什么内省需要「反复迭代」——首轮索引是空的，要等几轮才稳定（详见 u9-l3）。

#### 4.1.3 源码精读

**`Location` 本体极其简单——就是一个新类型包装的 `u128`：**

[src/introspection/location.rs:52-54](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/location.rs#L52-L54) 定义了它：

```rust
#[ty(scope, since = "forever")]
#[derive(Copy, Clone, Eq, PartialEq, Hash)]
pub struct Location(u128);
```

`#[ty(scope)]` 让它被注册成 Typst 的一等类型（用户写 `location` 这个类型名）；它 `Copy + Hash`，所以当哈希表的键、复制传递都很廉价。构造与读取都只围绕这个 `u128`：

[src/introspection/location.rs:56-75](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/location.rs#L56-L75) 提供了 `new` / `hash` / `variant`。注意 `variant` 用「把 `(self.0, n)` 再哈希一次」的方式，从一个已知 `Location` 派生出一个新的、可链接的 `Location`——文献管理里用它为每条参考文献条目单独造一个可链接位置：

```rust
pub fn variant(self, n: usize) -> Self {
    Self(typst_utils::hash128(&(self.0, n)))
}
```

一个 `Location` 能回答三个问题：在第几页、在页面上的坐标、所在页的页码格式。它们都是「内省」——需要查上一轮建好的索引：

[src/introspection/location.rs:96-122](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/location.rs#L96-L122) 中，`page()` / `position()` / `page_numbering()` 都委托给 `engine.introspect(...)`（详见 4.3.3）。以 `page()` 为例：

```rust
#[func(since = "forever")]
pub fn page(self, engine: &mut Engine, span: Span) -> NonZeroUsize {
    engine.introspect(PageIntrospection(self, span))
}
```

注意一个重要设计：`Location` **故意不实现 `Ord`**。按哈希值比大小没有语义意义，容易误用。如果真的需要排序（比如做集合），要用 [src/introspection/location.rs:151-159](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/location.rs#L151-L159) 的 `LocationKey`——它显式地、带文档警告地实现了 `Ord`，逼使用者意识到自己在做什么。

**`Tag` 把位置写进帧树。** 看它的枚举定义：

[src/introspection/tag.rs:10-24](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/tag.rs#L10-L24)：

```rust
pub enum Tag {
    /// The stored element starts here.（元素在这里开始）
    Start(Content, TagFlags),
    /// The element with the given location and key hash ends here.（元素在这里结束）
    End(Location, u128, TagFlags),
}
```

两个变体故意分担不同信息：`Start` 携带完整的 `Content`（元素本体），`End` 只带 `Location` 和一个 `key: u128`（元素的「键哈希」，用于测量模式，见 4.2）。源码注释解释了为什么把 `key` 放在 `End` 而非 `Start`：纯粹是为了让两个变体体积更接近，缩小 `Tag` 的内存占用，没有语义原因。

[src/introspection/tag.rs:28-33](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/tag.rs#L28-L33) 的 `Tag::location()` 统一从两个变体取位置：`Start` 时从内嵌 `Content` 取（`Content::location()` 会 `unwrap`，因此放进 `Tag` 的 content **必须**已有位置，否则 panic），`End` 时直接返回自带的位置。

`TagFlags` 标记一个 tag 是否「可被内省」：

[src/introspection/tag.rs:46-61](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/tag.rs#L46-L61)：

```rust
pub struct TagFlags {
    /// 是否会被收进 Introspector（因为 Locatable、被打了 label，或手动设了位置）
    pub introspectable: bool,
    /// 是否具备 Tagged 能力
    pub tagged: bool,
}
```

只有 `introspectable` 为真的 tag 会被内省器收进索引（见 `ElementIntrospectorBuilder::discover_tag`，[src/introspection/introspector.rs:513-530](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/introspector.rs#L513-L530)）。

**`TagElem` 是承载 tag 的元素**，它本身是 `#[internal]` 的，用户不能直接构造：

[src/introspection/tag.rs:67-89](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/tag.rs#L67-L89)：

```rust
#[elem(Construct, Unlabellable)]
pub struct TagElem {
    #[required]
    #[internal]
    pub tag: Tag,
}

impl Construct for TagElem {
    fn construct(_: &mut Engine, args: &mut Args) -> SourceResult<Content> {
        bail!(args.span, "cannot be constructed manually")
    }
}
```

[src/introspection/tag.rs:75-83](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/tag.rs#L75-L83) 的 `TagElem::packed(tag)` 是内部构造入口：建好元素后立刻 `mark_prepared()`（跳过准备阶段），把它塞进内容流。排版器在遇到 `TagElem` 时不会画任何东西，但会「读出」里面的 `Tag` 交给内省器——这就是位置信息进入帧树的方式。

最后回到「元素如何携带 location」：[src/foundations/content/mod.rs:602-604](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/mod.rs#L602-L604) 的 `Content::location()` 读的就是 u3-l1 讲过的 `Meta.location`：

```rust
pub fn location(&self) -> Option<Location> {
    self.0.meta().location
}
```

它在实现前是 `None`，发号后被 `set_location` 写成 `Some(loc)`。`query` 返回的元素之所以能 `.location()`，正是因为它们在上一轮已经走完了「发号 → set_location → Tag」的流程。

#### 4.1.4 代码实践

**实践目标**：亲手感受 `Location` 是一个稳定哈希、`Tag` 是帧树里的书签。

**操作步骤**：

1. 准备一个最小 Typst 文件 `loc.typ`：

   ```typ
   #context [
     第一个标题在：
     #query(heading).first().location().page()
   ]
   = Introduction <intro>
   = Discussion
   ```

2. 若本地装了 typst CLI，编译它：

   ```sh
   typst compile loc.typ
   ```

3. 打开源码，对照 [src/introspection/location.rs:56-75](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/location.rs#L56-L75)，回答：`query(heading).first()` 拿到的是一个 `Content`，它身上的 `location()` 返回什么类型？为什么首轮排版时这个查询可能拿不到正确结果？

**需要观察的现象**：文档能正常编译；`query(heading).first().location().page()` 打印出 `Introduction` 所在的页码。

**预期结果**：`location()` 返回 `Option<Location>`（实际为 `Some`），`.page()` 经 `engine.introspect(PageIntrospection(...))` 查上一轮索引得到页码。**待本地验证**具体页码值（取决于页面尺寸与正文长度）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Location` 不实现 `Ord`，却另造一个 `LocationKey` 来实现它？

> **答案**：两个 `u128` 哈希之间的大小比较没有语义意义（哈希是随机分布的），容易让用户误以为「`a < b` 意味着 a 在 b 前面」。`LocationKey` 用一个独立的、带警告文档的类型显式实现 `Ord`，逼使用者在「我确实只是想要一个可排序的键」时才用，避免误用（见 [location.rs:143-159](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/location.rs#L143-L159)）。

**练习 2**：`Tag::Start` 里嵌的 `Content` 为什么 `location()` 一定不能是 `None`？

> **答案**：`Tag::location()` 在 `Start` 分支里直接 `elem.location().unwrap()`（[tag.rs:30](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/tag.rs#L30)）。源码注释明确说「放进 tag 的 content 必须有 `Location`，否则会 panic」。因此只有已经发过号的 locatable 元素才会被包进 `Tag`。

---

### 4.2 Locator 与 SplitLocator：位置的分配器

#### 4.2.1 概念说明

`Location` 是「身份证号」，那「发号机」就是 `Locator`。它在排版阶段运行，为每个 locatable 元素分配一个 `u128`。

发号看似简单（自增计数器不就行了？），但源码在 [src/introspection/locator.rs:13-152](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/locator.rs#L13-L152) 用了整整 140 行文档注释讨论三种策略，最终选择了**分层哈希（hierarchical hashing）**。原因是发号机要同时满足三个目标：

1. **跨迭代稳定**（同一元素每次拿到同号）。
2. **跨编辑稳定**（局部编辑不波及全局，利于增量编译）。
3. **尽量无状态**（支持排版并行化）。

自增计数器满足不了 (2)（中间插入会让后面全错位）和 (3)（需要共享可变状态）。 Typst 的方案是：把排版看作一棵递归执行树，每一层用一个「本地哈希 + 该层内 span 去重计数」生成局部编号，再把各层编号**逐层哈希**成一个最终的 `u128`：\( h_n = \mathrm{hash}(h_{n-1}, k_n) \)。这样每层的状态都是局部的，可以并行；而 span 的引入又让普通编辑只影响被改动的那一处。

#### 4.2.2 核心流程

`Locator` 的设计围绕一个核心约束：**它故意不实现 `Copy` / `Clone`**（见 [locator.rs:45-48](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/locator.rs#L45-L48)）。这意味着一个 `Locator` 只能用一次，强迫排版器在「要给多个子元素发号」时显式做出选择：

- **`split()`** ——要给**多个不同的**子内容发号时调用。它返回一个 `SplitLocator`，每次 `next(key)` 吐出一个**新的、互不相同**的子 `Locator`。例如排版 5 个不同的 figure，每个会拿到不同的号、显示成不同的图号。
- **`relayout()`** ——要给**同一内容**多次排版时调用（典型场景：测量）。它返回一个复制的 `Locator`，表示「这其实是同一份内容」，于是同一 figure 测量和真正排版会拿到**相同**的图号。

用伪代码表示一个排版函数的典型骨架：

```text
fn layout(self, locator: Locator) -> Frame {
    if 只需要一次子排版 {
        child.layout(locator)              // 直接传走（move）
    } else if 多个不同子元素 {
        let mut split = locator.split();   // 拆成发号机
        for child in children {
            let sub = split.next(&child.span());  // 各发各的号
            child.layout(sub);
        }
    } else if 同一内容多次（测量） {
        let probe = locator.relayout();    // 复制，表示「同一份」
        measure(child, probe);
        child.layout(locator);
    }
}
```

`Locator` 内部只有两个字段：

[src/introspection/locator.rs:153-160](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/locator.rs#L153-L160)：

```rust
pub struct Locator<'a> {
    /// 自上次记忆化边界以来，累计本层及以内信息的本地哈希
    local: u128,
    /// 指向外层缓存定位器的指针，按需贡献「更外层」的信息
    outer: Option<&'a LocatorLink<'a>>,
}
```

真正算号发生在 `resolve()`：

[src/introspection/locator.rs:212-222](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/locator.rs#L212-L222)：

```rust
fn resolve(&self) -> Resolved {
    match self.outer {
        None => Resolved::Hash(self.local),
        Some(outer) => match outer.resolve() {
            Resolved::Hash(outer) => Resolved::Hash(typst_utils::hash128(&(self.local, outer))),
            Resolved::Measure(base, span) => Resolved::Measure(base, span),
        },
    }
}
```

即「把自己的 `local` 和外层的 `outer` 哈希拼起来再哈希」，正是 \( h_n = \mathrm{hash}(h_{n-1}, k_n) \)。`outer` 是惰性的（经 `LocatorLink` + `OnceLock` 缓存，[locator.rs:345-395](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/locator.rs#L345-L395)）：只有真要发号时才回溯外层，这样不含 locatable 元素的内容可以跨位置复用缓存。

`SplitLocator::next(key)` 是发号的核心：

[src/introspection/locator.rs:268-282](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/locator.rs#L268-L282)：

```rust
pub fn next_inner(&mut self, key: u128) -> Locator<'a> {
    // 为「同一 key 出现多次」去重：每次见到同一 key，计数 +1
    let disambiguator = {
        let slot = self.disambiguators.entry(key).or_default();
        std::mem::replace(slot, *slot + 1)
    };
    // 把 key、去重计数、本地哈希三者合成新的 local（外层信息留到 resolve 时再合并）
    let local = typst_utils::hash128(&(key, disambiguator, self.local));
    Locator { outer: self.outer, local }
}
```

这里的 `key` 通常是元素的 `span` 哈希（`next(&K)` 直接对 `K` 取哈希，[locator.rs:263-265](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/locator.rs#L263-L265)）。`disambiguator` 处理「同一 span 生成多个元素」的情形，比如 `#for _ in range(5) { figure() }`——5 个 figure 的 span 相同，靠递增计数区分。

最终给元素发号是 `next_location`：

[src/introspection/locator.rs:285-303](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/locator.rs#L285-L303)：

```rust
pub fn next_location(&mut self, engine: &mut Engine, key: u128, elem_span: Span) -> Location {
    match self.next_inner(key).resolve() {
        Resolved::Hash(hash) => Location::new(hash),
        Resolved::Measure(base, measure_span) => {
            // 测量模式：尽力在真实文档里找最接近的匹配元素
            let introspection = MeasureIntrospection { key, base, measure_span, elem_span };
            engine.introspect(introspection).unwrap_or(base)
        }
    }
}
```

正常情况 `resolve()` 得到一个 `Hash`，直接包成 `Location`。但如果是「测量模式」（`Resolved::Measure`，由 [locator.rs:378-383](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/locator.rs#L378-L383) 的 `LocatorLink::measure` 进入），发号会退化成「在真实文档索引里找 key 最接近、位置最靠后的元素」——这是源码注释 [locator.rs:114-152](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/locator.rs#L114-L152) 花大篇幅讨论的「测量难题」：用户用 `measure` 量一段内省内容时没有帮我们管理 locator，Typst 只能尽力匹配。失败时回退到 `base`，并发出 `MeasureIntrospection` 的收敛警告（[locator.rs:316-341](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/locator.rs#L316-L341)）。

#### 4.2.3 代码实践（源码阅读型）

**实践目标**：吃透 `split` 与 `relayout` 的语义差异，理解「不实现 Clone」的良苦用心。

**操作步骤**：

1. 读 [locator.rs:13-152](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/locator.rs#L13-L152) 的类型级文档，重点看它列举的三种发号策略及各自的 (1)(2)(3) 满足情况。
2. 读 [locator.rs:187-206](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/locator.rs#L187-L206) 的 `split()` 与 `relayout()`，注意 `relayout` 的注释明确说「这其实就是 `Clone`，但 `Locator` 不实现 `Clone` 是为了让这个操作显式化」。
3. 用一句话回答：如果给同一份 figure 内容用 `split().next()` 两次会怎样？用 `relayout()` 两次又会怎样？

**需要观察的现象**：无需运行，纯阅读理解。

**预期结果**：

- `split()` 两次 → 两个 `disambiguator` 不同（0 和 1）→ 两个不同的 `Location` → 同一 figure 排出两个不同的图号。
- `relayout()` 两次 → 两次 `local`/`outer` 完全相同 → 同一个 `Location` → 同一图号，符合「测量 + 真排是同一份内容」的预期。

#### 4.2.4 小练习与答案

**练习 1**：`Locator` 为什么不实现 `Copy`/`Clone`？

> **答案**：为了让「给多个东西发号」时必须显式选择 `split`（不同号）或 `relayout`（同号）。若能随意 `clone`，排版器可能在不知不觉中给同一份内容发不同的号（图号错乱）或给不同内容发同号（查询错乱），错误会非常隐蔽（见 [locator.rs:45-48](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/locator.rs#L45-L48)）。

**练习 2**：分层哈希 \( h_n = \mathrm{hash}(h_{n-1}, k_n) \) 相比「全局自增计数器」，在「文档中间插入一段内容」时有什么好处？

> **答案**：自增计数器下，中间插入会让后续所有元素号 +1，全文档的 `Location` 雪崩式失效，增量编译几乎归零。分层哈希下，插入只改变插入点所在层级及以其为祖先的子树的号，其他无关子树（因为它们的 `local` 没变、`outer` 链也没变）保持原号，增量编译仍能命中大部分缓存。

**练习 3**：`next(key)` 的 `key` 推荐用什么？为什么？

> **答案**：推荐用待排内容的 `span`（[locator.rs:260-262](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/locator.rs#L260-L262)）。span 大体唯一且跨编辑稳定，能让 `Location` 在编辑后尽量不变，提升增量编译性能。key 不要求唯一（可以全是 `&()`），但不唯一会让 `disambiguator` 频繁介入、稳定性下降。

---

### 4.3 query / locate / here：用户可见的内省入口

#### 4.3.1 概念说明

前面两节是「幕后」：位置的表示与分配。本节是「台前」：用户在 Typst 代码里实际调用的三个内省函数。它们都在 [src/introspection/mod.rs:35-45](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/mod.rs#L35-L45) 的 `define` 里注册为标准库函数：

- **`query(target)`** —— 按选择器找出文档里**所有**匹配元素，返回数组。最常用，比如 `query(heading.where(level: 1))`。
- **`locate(selector)`** —— 找出**唯一**匹配元素，返回它的 `Location`。要求选择器只命中一个。
- **`here()`** —— 返回**当前**上下文的 `Location`。是最低层的积木，比如 `counter.get()` 内部等价于 `counter.at(here())`。

这三个函数有完全一致的结构：参数里都有一个 `context: Tracked<Context>`（门禁卡，4.4 详讲），函数体都先「过门禁」再「查索引」。它们的 `#[func]` 都标了 `contextual`——表示「这个函数的返回值依赖上下文，必须放在 `context { ... }` 里调用」。

理解它们的关键，是看懂那个统一的「查索引」入口 `engine.introspect(...)`。

#### 4.3.2 核心流程

三个函数的共同套路：

```text
用户调用 query / locate / here
   │
   ├─ 1. 过门禁：context.introspect()? 或 context.location()   （4.4）
   │        └─ 若 context 缺失 → 报错 "can only be used when context is known"
   │
   └─ 2. 查索引：engine.introspect(XxxIntrospection { ... })
            │
            ├─ introspection.introspect(engine, introspector)   ← 真正查 Introspector
            └─ sink.introspection(...)                            ← 记录这次查询（供收敛诊断）
```

`Engine::introspect` 是所有内省的统一入口：

[src/engine.rs:109-117](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L109-L117)：

```rust
pub fn introspect<I>(&mut self, introspection: I) -> I::Output
where I: Introspect {
    let introspector = *self.introspector.access("is okay since we're recording it");
    let output = introspection.introspect(self, introspector);   // 真正查询
    self.sink.introspection(Introspection::new(introspection));   // 记录，供收敛分析
    output
}
```

注意它做了两件事：(a) 调用具体内省结构体的 `introspect` 方法去问 `Introspector`；(b) 把这次查询**记录**到 sink。第 (b) 步是为收敛循环服务的——编译结束后，[convergence.rs:24-70](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/convergence.rs#L24-L70) 的 `analyze` 会重放所有记录过的查询，比较各轮输出是否一致，不一致就发「document did not converge」警告（详见 u9-l3）。

每个用户函数都对应一个 `XxxIntrospection` 结构体，它实现 `Introspect` trait。这种「一个查询一个结构体」的设计，是为了给每种查询定制**收敛失败时的诊断信息**（[convergence.rs:82-92](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/convergence.rs#L82-L92) 解释了为什么不为图省事全用 `QueryIntrospection`）。

#### 4.3.3 源码精读

**`query` —— 先门禁，后查全部匹配。** 看 [src/introspection/query.rs:159-176](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/query.rs#L159-L176)：

```rust
#[func(contextual, since = "forever")]
pub fn query(
    engine: &mut Engine,
    context: Tracked<Context>,
    span: Span,
    target: LocatableSelector,                 // 元素函数 / <label> / heading.where(..) / selector(..).before(..)
) -> HintedStrResult<Array> {
    context.introspect()?;                       // ← 门禁：必须有 context
    let vec = engine.introspect(QueryIntrospection(target.0, span));  // ← 查索引
    Ok(vec.into_iter().map(Value::Content).collect())
}
```

两行核心：`context.introspect()?` 是门禁（4.4），`engine.introspect(QueryIntrospection(...))` 是真查询。返回值把每个匹配元素包成 `Value::Content`——这些 `Content` 已经带好了 `location`（因为上一轮发过号），所以 `query(heading).first().location()` 能直接用。

`QueryIntrospection` 的实现就是转交给 introspector：

[src/introspection/query.rs:179-203](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/query.rs#L179-L203)：

```rust
impl Introspect for QueryIntrospection {
    type Output = EcoVec<Content>;
    fn introspect(&self, _: &mut Engine, introspector: Tracked<dyn Introspector + '_>) -> Self::Output {
        introspector.query(&self.0)
    }
    fn diagnose(&self, history: &History<Self::Output>) -> SourceDiagnostic { /* 收敛诊断 */ }
}
```

`introspector.query(selector)` 真正在 [src/introspection/introspector.rs:193-342](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/introspector.rs#L193-L342) 实现：对 `Selector::Elem` 它遍历所有元素逐个 `matches`；对 `Selector::Label` 走 `labels` 加速表；对 `Before`/`After`/`Within` 先查子选择器再用二分截取。返回的元素就是索引里存的那份带 `location` 的 `Content`。

> **`query` 返回的元素如何携带 location？** 答案链：上一轮排版时 `SplitLocator::next_location` 给元素发了号 → `set_location` 写进 `Meta.location` → `TagElem` 把 `Tag` 盖进帧树 → 内省器收集 tag 建 `elems`/`locations` 索引（[introspector.rs:170-190](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/introspector.rs#L170-L190)）→ 本轮 `introspector.query` 返回的就是索引里那份 `Content`，其 `location()` 必为 `Some`。

**`locate` —— 找唯一元素的位置。** 看 [src/introspection/locate.rs:27-42](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/locate.rs#L27-L42)：

```rust
#[func(contextual, since = "forever")]
pub fn locate(engine: &mut Engine, context: Tracked<Context>, span: Span,
              selector: LocatableSelector) -> SourceResult<Location> {
    selector.resolve_unique(engine, context, span)
}
```

它把活儿交给 `LocatableSelector::resolve_unique`，这里藏着一个有意思的细节——[src/foundations/selector.rs:374-391](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/selector.rs#L374-L391)：

```rust
pub fn resolve_unique(&self, engine: &mut Engine, context: Tracked<Context>, span: Span) -> SourceResult<Location> {
    match self.0.clone() {
        Selector::Location(loc) => Ok(loc),        // ← 已是裸 Location，无需 context、无需查询！
        other => {
            context.introspect().at(span)?;          // ← 否则过门禁
            engine.introspect(QueryUniqueIntrospection(other, span))
                .map(|c| c.location().unwrap()).at(span)
        }
    }
}
```

也就是说：`locate(some_location)` 这种传「现成位置」的调用**根本不需要 context**，直接原样返回；而 `locate(<intro>)`（按标签找）才需要过门禁 + 查索引。这是 `locate` 比另外两个更「宽容」的原因。

**`here` —— 最薄的一层，直接从 context 取位置。** 看 [src/introspection/here.rs:49-52](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/here.rs#L49-L52)：

```rust
#[func(contextual, since = "0.11.0")]
pub fn here(context: Tracked<Context>) -> HintedStrResult<Location> {
    context.location()
}
```

`here()` 连 `engine.introspect` 都不调——它只是「从当前 context 里把位置取出来」。这里的「位置」是 `ContextElem` 在 show 阶段塞进 context 的（4.4.3 会看到 `CONTEXT_RULE`）。换句话说，`here()` 拿到的是「我自己被排版时所在的那张帧对应的位置」，是真正「此刻此地」的位置。

#### 4.3.4 代码实践

**实践目标**：体会「本轮查询看的是上一轮索引」，并观察自反查询导致的不收敛。

**操作步骤**：

1. 写一个文件 `q.typ`，复刻 [query.rs:79-101](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/query.rs#L79-L101) 文档里的「自反查询」例子：

   ```typ
   = Real
   #context {
     let elems = query(heading)
     let count = elems.len()
     count * [= Fake]
   }
   ```

2. 编译它：

   ```sh
   typst compile q.typ
   ```

3. 再写一个正常例子，验证 `query` 返回的元素能取位置和字段：

   ```typ
   #context {
     for h in query(heading) {
       [#h.body 在第 #h.location().page() 页 \ ]
     }
   }
   = A
   = B
   ```

**需要观察的现象**：

- 自反查询的例子会产生一条警告，且最终 `= Fake` 的数量是有限的（Typst 放弃后停在一个数）。
- 正常例子里 `h.body`、`h.location().page()` 都能正常取到。

**预期结果**：自反例子触发收敛警告（因为 `query(heading)` 的结果反过来改变标题数量，永不稳定，见 [query.rs:73-101](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/query.rs#L73-L101)）。正常例子打印每个标题的正文与页码。**待本地验证**警告的确切措辞与 `= Fake` 的最终个数（由 `MAX_ITERS = 5` 决定，见 [convergence.rs:16](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/convergence.rs#L16)）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `here()` 的函数体里没有 `engine.introspect(...)`，而 `query()` 有？

> **答案**：`here()` 只是从当前 `Context` 里取出「此刻此地」的位置（`context.location()`），这个位置是 `ContextElem` 在 show 阶段就已经塞进 context 的，不需要再去问上一轮的索引。而 `query()` 要在整篇文档里找元素，必须查 `Introspector`，所以走 `engine.introspect`（对比 [here.rs:50-51](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/here.rs#L50-L51) 与 [query.rs:173-175](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/query.rs#L173-L175)）。

**练习 2**：`locate(some_location)` 和 `locate(<intro>)` 对 context 的要求一样吗？

> **答案**：不一样。前者传的是现成 `Location`，`resolve_unique` 在 `Selector::Location` 分支直接 `Ok(loc)` 返回，不过门禁、不查索引（[selector.rs:381-382](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/selector.rs#L381-L382)）；后者要按标签查唯一元素，必须先 `context.introspect().at(span)?` 过门禁再查索引。

**练习 3**：`query` 返回数组里的元素，为什么能直接 `.location()`？

> **答案**：因为它们来自上一轮排版已发号的索引。流程是：发号 → `set_location` → `Tag` 盖进帧 → 内省器收集成带 location 的 `Content` 索引 → 本轮 `introspector.query` 返回的就是这份 `Content`，故 `location()` 为 `Some`。

---

### 4.4 Context：内省的门控

#### 4.4.1 概念说明

4.3 里三个函数都先调了 `context.introspect()?` 或 `context.location()`，失败就报一个著名错误。这节就彻底拆解这道「门禁」。

`Context` 是一段代码运行时能拿到的「上下文数据」。Typst 里很多值**只有在排版后才能确定**：当前在哪一页、当前文本语言是什么、`counter` 现在是多少。这些叫**上下文相关（contextual）**的值。如果代码在「还不知道这些」的阶段运行（比如顶层直接求值，还没进 `context { }` 块），贸然查就会得到错误结果。

`Context` 就是这些「也许可用、也许不可用」数据的载体。它的字段只有两个：

[src/foundations/context.rs:15-21](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/context.rs#L15-L21)：

```rust
pub struct Context<'a> {
    /// The location in the document.（当前位置）
    pub location: Option<Location>,
    /// The active styles.（当前样式链）
    pub styles: Option<StyleChain<'a>>,
}
```

两个都是 `Option`——「有」或「没有」。这就是门禁的全部秘密：**内省函数要求至少有一个是 `Some`，否则拒绝服务。**

#### 4.4.2 核心流程

门禁逻辑在三个方法里，套路一致——都委托给私有函数 `require`：

[src/foundations/context.rs:37-51](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/context.rs#L37-L51)：

```rust
/// Try to extract the location.
pub fn location(&self) -> HintedStrResult<Location> { require(self.location) }
/// Try to extract the styles.
pub fn styles(&self) -> HintedStrResult<StyleChain<'a>> { require(self.styles) }
/// Guard access to the introspector by requiring at least some piece of context.
pub fn introspect(&self) -> HintedStrResult<()> {
    require(self.location.map(|_| ()).or(self.styles.map(|_| ())))
}
```

`introspect()` 这个守卫最宽松——**只要有 location 或 styles 任一即可**（用 `.map(|_| ())` 把存在性压成 `()`，再 `or` 合并）。`location()` 最严格——必须有 location。

`require` 是产生那个经典错误的地方：

[src/foundations/context.rs:55-61](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/context.rs#L55-L61)：

```rust
fn require<T>(val: Option<T>) -> HintedStrResult<T> {
    val.ok_or("can only be used when context is known")
        .hint("try wrapping this in a `context` expression")
        .hint("the `context` expression should wrap everything that depends on this function")
}
```

当 `val` 是 `None` 时，返回错误信息 **"can only be used when context is known"**，并附两条提示：① 试着把它包进 `context { }` 表达式；② `context` 要包住所有依赖它的代码。这就是用户在忘写 `context` 时会看到的那段话。

那么 `Context` 是怎么「变 Some」的？答案是 `ContextElem`（对应 Typst 里的 `context { ... }`）：

[src/foundations/context.rs:64-82](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/context.rs#L64-L82)：

```rust
#[elem(Construct, Locatable)]
pub struct ContextElem {
    #[required] #[internal] func: Func,
}
// ...
pub const CONTEXT_RULE: ShowFn<ContextElem> = |elem, engine, styles| {
    let loc = elem.location().unwrap();                         // 取到自己的位置
    let context = Context::new(Some(loc), Some(styles));        // ← 构造「有 location + 有 styles」的 context
    Ok(elem.func.call::<[Value; 0]>(engine, context.track(), [])?.display())
};
```

`CONTEXT_RULE` 是 `ContextElem` 的 show 规则：它在 show 阶段拿到「自己这个 context 块被排版到的位置」`loc`，连同当前样式 `styles`，组装出一个**两个字段都是 Some** 的 `Context`，再调用用户传入的函数 `func`，把这个 `Context` 作为上下文传进去。所以一旦代码进了 `context { }`，里面的 `query`/`here` 就有了有效 context，门禁放行。

注意 `ContextElem` 标了 `Locatable`——它自己就是一个 locatable 元素，因此排版时会发到号，`elem.location().unwrap()` 才能拿到位置。这就是 `here()` 返回的「此刻此地」位置的来源。

#### 4.4.3 代码实践

**实践目标**：复现「没有 context 就无法 introspect」的错误，并解释它的产生路径。这是本讲义规格里指定的核心实践。

**操作步骤**：

1. 写一个**故意不写 context** 的文件 `ctx.typ`：

   ```typ
   // 错误用法：直接在顶层调用 here()
   当前页码是：#here().page()
   ```

2. 编译它：

   ```sh
   typst compile ctx.typ
   ```

3. 把它改成正确用法，再编译：

   ```typ
   当前页码是：#context { here().page() }
   ```

4. 对照源码，画出错误从产生到展示的完整路径。

**需要观察的现象**：

- 第 1 步报错，信息为 **"can only be used when context is known"**，并带两条提示（建议包进 `context { }`）。
- 第 3 步正常打印页码。

**错误的产生路径（请你自己对照源码填出每一步对应的行号）**：

```text
here() 被调用，但当前没有 context 块包裹
   │  here.rs:51  context.location()
   ▼
context.rs:39  require(self.location)        // self.location 是 None
   │
   ▼
context.rs:55-61  require(None)              // val.ok_or(...) 失败
   │  → 返回 Err("can only be used when context is known")
   │     + hint("try wrapping this in a `context` expression")
   │     + hint("the `context` expression should wrap ...")
   ▼
（带 span 的）StrResult 经 .at(span) 升级为 SourceResult，最终由驱动器打印
```

之所以 `self.location` 是 `None`：因为 `here()` 运行时不在任何 `context { }` 块里，也就没有 `ContextElem` 的 show 规则（`CONTEXT_RULE`）来调用 `Context::new(Some(loc), Some(styles))`——传给 `here` 的 `context` 是个空 context（`Context::none()`），两个字段都是 `None`。

**预期结果**：错误信息与提示措辞与源码 [context.rs:57-60](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/context.rs#L57-L60) 完全一致。**待本地验证**编译器是否会额外给出指向 `here()` 调用处的 span。

#### 4.4.4 小练习与答案

**练习 1**：`Context::introspect()` 为什么比 `Context::location()` 宽松？

> **答案**：`introspect()` 只要求 `location` **或** `styles` 任一存在（用 `.map(|_|()).or(..)` 合并），而 `location()` 严格要求 `location` 存在。因为 `query` 这类内省只要「处在某个排版上下文里」就足以查索引，不强求精确位置；而 `here()` 要返回的就是位置本身，非有 location 不可（对比 [context.rs:38-50](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/context.rs#L38-L50)）。

**练习 2**：`context { }` 块里的代码，其 `Context` 的两个字段分别从哪来？

> **答案**：都来自 `CONTEXT_RULE`（[context.rs:78-82](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/context.rs#L78-L82)）。`location` 来自 `ContextElem` 自身的位置（它标了 `Locatable`，排版时发到号），`styles` 来自 show 规则收到的当前样式链。两者经 `Context::new(Some(loc), Some(styles))` 组装后传给用户函数。

**练习 3**：一个 `Context` 既不实现显式的「门禁开关」、也没有运行时锁，它是如何「阻止」在内省大厅外调用的？

> **答案**：靠**数据缺失 + 错误返回**，而非权限系统。`Context` 默认是空的（`Context::none()`，两字段皆 `None`），只有在 `ContextElem` 的 show 规则里才会被填上值。内省函数调 `require(...)` 时遇到 `None` 就返回错误。换言之，门禁不是「禁止进入」，而是「没带卡就查不到东西并报错」，把控制权交给调用方去补 `context { }`。

---

## 5. 综合实践

把本讲的四块知识串起来：用 `query` + `here` + `location` 手写一个简易目录，**只**用本讲讲过的概念（不依赖 `outline`、`counter`）。

**任务**：写一个 Typst 文档，包含若干 `= 标题`，在开头打印出每个标题的正文、所在页码，并标注它出现在「当前页」之前还是之后（用 `here()` 判断）。

**参考骨架**（示例代码，需自行补全）：

```typ
#set page(numbering: "1")

// ① 在 context 块里，因为 query / here 都需要 context
#context {
  let now = here()                       // ② 当前位置
  let headings = query(heading)          // ③ 查所有标题（返回带 location 的元素）
  for h in headings {
    let page = h.location().page()       // ④ 用 location 查页码
    let where_ = if h.location() == now { "就在此页" } else { "第 " + str(page) + " 页" }
    [#h.body — #where_ \ ]
  }
}

= 引言
#lorem(20)
#pagebreak()

= 正文
#lorem(20)
```

**对照本讲知识自检**：

- ①② 对应 4.4：进了 `context { }`，`CONTEXT_RULE` 给 `here()` 喂了有效 `Context`，门禁放行。
- ③ 对应 4.3：`query` 先 `context.introspect()?` 过门禁，再 `engine.introspect(QueryIntrospection)` 查上一轮索引。
- ④ 对应 4.1：返回的 `h` 带着上一轮发好的 `location`，`.page()` 再走一次 `engine.introspect(PageIntrospection)`。
- 隐含用到 4.2：每个标题能被查到，是因为排版时 `SplitLocator` 给它发了稳定的号。

**待本地验证**：实际页码与「就在此页」的判定结果（取决于 `lorem` 填充后标题落在哪一页）。可尝试调整 `lorem` 长度，观察收敛过程（必要时会看到「document did not converge」警告，那是 u9-l3 的主题）。

---

## 6. 本讲小结

- **`Location` 就是一个 `u128`**：作为 locatable 元素的「身份证号」，`Copy + Hash` 但故意不实现 `Ord`；`variant` 可从已知位置派生新位置。
- **`Tag` / `TagElem` 是位置进入帧树的载体**：`Start`/`End` 两个变体分担信息；只有 `introspectable` 的 tag 才被内省器收进索引；`TagElem` 是 `#[internal]` 的，用户不能构造。
- **`Locator` 是分层哈希发号机**：用 \( h_n=\mathrm{hash}(h_{n-1},k_n) \) 兼顾跨迭代稳定、跨编辑稳定与可并行；故意不 `Clone`，逼排版器在 `split`（不同号）与 `relayout`（同号）间显式选择。
- **`SplitLocator::next(key)`** 用 `(key, disambiguator, local)` 合成本层哈希，`key` 推荐用 span；`next_location` 在测量模式下退化成「找最接近的真实元素」。
- **`query` / `locate` / `here` 是统一套路的三个入口**：先过 `Context` 门禁，再走 `engine.introspect(XxxIntrospection)` 查 `Introspector`；返回的元素都带 location。`here()` 最薄，只从 context 取位置；`locate(裸位置)` 连门禁都不需要。
- **`Context` 是「数据缺失式」门禁**：两字段 `Option`，`introspect()` 要求至少一个 `Some`、`location()` 要求 location 是 `Some`；缺失时经 `require` 返回 "can only be used when context is known"；`context { }` 块的 `CONTEXT_RULE` 负责把 context 填满。

## 7. 下一步学习建议

本讲只讲了「表示与入口」，建议按顺序继续：

1. **u9-l2 Counter、State 与 Metadata**：`Counter` / `State` 是构建在 `Location` 与 `Introspector` 之上的高层内省原语，`counter.at(loc)` 正是把「位置」喂给内省的典型用法；`Metadata` 则是「专为被 query 而生」的元素。
2. **u9-l3 Introspector 与收敛循环**：彻底搞懂 `Introspect` trait、`History`、`MAX_ITERS = 5` 的 `analyze`，回答本讲反复提到但未展开的「为什么内省要反复迭代、何时判定收敛、不收敛怎么报错」。特别建议结合本讲 4.3.4 的自反查询例子一起读。
3. **回到源码**：重读 [src/introspection/introspector.rs:193-342](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/introspector.rs#L193-L342) 的 `ElementIntrospector::query`，把本讲 4.3 里「`query` 返回带 location 的元素」这条结论在索引侧验证一遍；再读 `ElementIntrospectorBuilder::discover_tag`（[introspector.rs:513-530](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/introspector.rs#L513-L530)），看 `Tag` 如何被收集成索引——这正好把 4.1（Tag）与 4.3（query）两头接上。
