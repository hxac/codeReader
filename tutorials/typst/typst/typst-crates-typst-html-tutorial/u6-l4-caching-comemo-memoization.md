# 缓存与 comemo memoization

## 1. 本讲目标

前面几讲我们分别走过了编译主链路（u3-l1 的 `html_document` 三层结构）和片段递归（u3-l4 的 block/inline/math fragment）。但你可能一直有个疑问悬而未决：为什么 `html_document` 要拆成 `html_document` → `html_document_impl` → `html_document_common` 三层？为什么 `html_block_fragment` 也有一对「公开函数 + `_impl`」？而偏偏 `html_inline_fragment` 和 `html_math_fragment` 又是光秃秃一个函数、没有 `_impl`？

答案都指向一个词：**comemo 记忆化（memoization）**。Typst 用 comemo 库为编译过程建立缓存，让「内省循环」里反复重跑的编译尽可能复用上一次的结果。本讲就把缓存这层抽象单独拎出来讲透。

学完本讲，你应该能够：

- 说清 comemo 的 `#[comemo::memoize]` 是**以参数哈希为键**缓存返回值的，因此「要被缓存」与「能作为缓存键」是两件不同的事——返回类型不需要 `Hash`，但每个参数都必须。
- 解释为什么要把 `&mut Engine` 逐字段拆成 `world / library / introspector / traced / sink / route` 一堆 `Tracked` / `TrackedMut` 参数传进 `_impl` 函数：因为引用本身不能当缓存键，只有可哈希的「值」才行。
- 解释 `HtmlDocument` 为何**故意不实现 `Hash`**：它的内省器既不可哈希，也不是 100% 由输出派生的——`root_mut` 在创建后被用来注入链接锚点（参见 dom.rs 注释与 issue #7951）。并说清这对缓存正确性与 `root_mut` 用法的约束。
- 对比 `html_block_fragment_impl`（可缓存）与 `html_inline_fragment`（不可缓存）：前者自带独立定位子树与自包含的智能引号状态，后者借共享的 `&mut SmartQuoter` 跨元素传递上下文，与 memoize「纯函数」前提冲突。

## 2. 前置知识

本讲是 u3-l1 与 u3-l4 的续篇，请先确认以下概念：

- **编译主链路的三层结构**（u3-l1）：`html_document`（公开，拆解 `Engine`）→ `html_document_impl`（被 `#[comemo::memoize]` 包裹的缓存边界）→ `html_document_common`（真正干活、被 bundle 路径复用的共享实现）。本讲要解释的正是「为什么要这么分」。
- **片段递归**（u3-l4）：`html_block_fragment` 走标准 memoize 三明治并自建 `SmartQuoter`；`html_inline_fragment` / `html_math_fragment` 借 `&mut SplitLocator` 与 `&mut SmartQuoter`、放弃缓存；三者入口都先 `route.check_html_depth()`（上限 72）。
- **`HtmlDocument` 的三块结构**（u2-l1）：`output: HtmlOutput`（扁平节点数组 + `root_index`）、`info: DocumentInfo`、`introspector: Arc<HtmlIntrospector>`，且它**不实现 `Hash`**。u2-l1 已点到原因，本讲正式展开。
- **`Engine` 的字段构成**（u1-l3）：`world / library / introspector / traced / sink / route` 六个字段。本讲会逐个说明它们怎么变成缓存键。
- **comemo 是什么**：Typst 自研（后独立发布）的增量计算库。用 `#[comemo::track]` 给一个 `impl` 块生成「可追踪（tracked）」的代理类型，用 `#[comemo::memoize]` 给函数加上「按参数哈希缓存」的能力。被 track 的对象一旦内容变化（generation 变更），所有依赖它的缓存自动失效。本讲不深入 comemo 内部实现，只讲 typst-html 如何用它。

> 一句话定位：**memoize 缓存的是「给定输入的输出」，所以输入必须可哈希、函数必须近似纯函数**。typst-html 里几乎所有「可缓存」与「不可缓存」的取舍，都可以用这两条去检验。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`src/document.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs) | **主角之一**：`html_document` / `html_document_for_bundle` 两个公开入口，以及它们的缓存层 `html_document_impl` / `html_document_for_bundle_impl` 与共享层 `html_document_common` |
| [`src/fragment.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/fragment.rs) | **主角之二**：可缓存的 `html_block_fragment_impl` 与不可缓存的 `html_inline_fragment` / `html_math_fragment`，对比鲜明 |
| [`src/dom.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs) | `HtmlDocument` 的定义及其「为何不实现 `Hash`」的注释、`root_mut` 关联 issue #7951 的注释——本讲代码实践的依据 |
| [`crates/typst-library/src/engine.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs) | `Engine` 结构体定义、`Route` 与 `check_html_depth`（`MAX_HTML_DEPTH = 72`）、`Sink` 的 track 实现 |
| [`crates/typst-utils/src/protected.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-utils/src/protected.rs) | `Protected` 包装器，解释 `introspector.into_raw()` / `Protected::from_raw(...)` 这对拆装 |

## 4. 核心概念与源码讲解

### 4.1 comemo memoize 与「三层夹心」结构

#### 4.1.1 概念说明

comemo 的 `#[comemo::memoize]` 给一个函数加上一层缓存：第一次调用时正常执行，并把**「参数 → 返回值」**这条记录存进缓存表；以后只要参数的哈希相同，就直接返回缓存的返回值，不再执行函数体。要让它正确工作，必须满足两点：

1. **所有参数都可哈希**——comemo 用参数的 `Hash` 来判定「这是不是同一次调用」。
2. **函数近似纯函数**——同样的输入应当产生同样的输出、同样的副作用。

> 名词解释：**纯函数**指输出与副作用完全由输入决定、不依赖任何隐藏外部状态的函数。如果函数偷偷读了某个可变全局变量，那么「输入相同但结果不同」就会让缓存给出陈旧值——这就是 memoize 最怕的「脏缓存」。

问题来了：typst-html 编译 HTML 远不是纯函数。它会查内省器（`query`）、会发警告、会收集内省记录、会受当前调用深度（route）影响。这些「不纯」的输入怎么进缓存键？答案是 comemo 的 **tracked** 机制（见 4.2）。而「副作用」怎么办？typst-html 的做法是把副作用**折叠进被缓存的执行体里**，让它们只在缓存未命中时跑一次（见 4.3）。

但还有一个更朴素的难题：`html_document` 的公开签名收的是 `&mut Engine`，而 `Engine` 是个大结构体、还带着引用和生命周期。直接把 `&mut Engine` 当 memoize 参数既不可哈希、也不优雅。于是 typst-html 采用了「三层夹心」：

```
html_document(&mut Engine, …)           公开入口：把 Engine 拆成可哈希的零件，转发
        │  （不在缓存边界内）
        ▼
html_document_impl(Tracked, Tracked, …)  #[comemo::memoize] 包裹 —— 缓存边界
        │  （缓存命中则直接返回，函数体不执行）
        ▼
html_document_common(…)                  共享实现：真正 realize / convert / finalize
        （不被 bundle 路径单独缓存，但被两个 _impl 复用）
```

- **最外层 `html_document`**：唯一的职责是把 `&mut Engine` 拆解成一组可哈希的参数，转发给 `_impl`。它自己**不被 memoize**，每次调用都跑（但它很轻，只做参数搬运）。
- **中间层 `html_document_impl`**：被 `#[comemo::memoize]` 标注，是真正的**缓存边界**。缓存命中时函数体整段跳过。
- **最内层 `html_document_common`**：真正干活的实现，既被 `html_document_impl` 调用，也被 `html_document_for_bundle_impl` 复用（见 4.3）。

这种「拆解 → 缓存 → 复用」的分层，是 typst-html 处理「带状态的编译过程 + 缓存」的标准范式。

#### 4.1.2 核心流程

把一次 `html_document` 调用按缓存视角拆开：

1. 外层 `html_document` 接到 `&mut Engine`。
2. 它把 `Engine` 的六个字段逐一「展平」为可哈希的 tracked 句柄，转发给 `html_document_impl`。
3. comemo 计算 `html_document_impl` 全部参数的哈希，查缓存表：
   - **命中**：直接返回缓存的 `HtmlDocument`，函数体不执行——`realize`、`convert_to_nodes`、锚点注入通通跳过。
   - **未命中**：执行 `html_document_impl` 函数体（含 `html_document_common` 与锚点后处理），把结果塞进缓存再返回。
4. 内省循环（核心引擎里反复重编译直到查询收敛）的每一轮都会再调一次 `html_document`；只要输入没变，第 3 步就持续命中缓存，这正是 memoize 的价值。

#### 4.1.3 源码精读

公开入口 `html_document` 极薄，只做参数搬运，没有任何 `#[comemo::memoize]`——它每次都跑，但只跑这几行：

[document.rs:25-40](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L25-L40) — 公开入口把 `Engine` 的每个字段拆成 tracked 参数转发给 `_impl`：

```rust
pub fn html_document(
    engine: &mut Engine,
    content: &Content,
    styles: StyleChain,
) -> SourceResult<HtmlDocument> {
    html_document_impl(
        engine.world,
        engine.library,
        engine.introspector.into_raw(),      // 拆掉 Protected 外壳
        engine.traced,
        TrackedMut::reborrow_mut(&mut engine.sink),
        engine.route.track(),
        content,
        styles,
    )
}
```

中间层 `html_document_impl` 才是缓存边界。注意 `#[comemo::memoize]` 标注、以及它的全部参数都是「可哈希的值/句柄」：

[document.rs:42-73](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L42-L73) — 被 memoize 的缓存层，函数体在缓存命中时整段跳过：

```rust
#[comemo::memoize]
#[expect(clippy::too_many_arguments)]
fn html_document_impl(
    world: Tracked<dyn World + '_>,
    library: &LazyHash<Library>,
    introspector: Tracked<dyn Introspector + '_>,
    traced: Tracked<Traced>,
    sink: TrackedMut<Sink>,
    route: Tracked<Route>,
    content: &Content,
    styles: StyleChain,
) -> SourceResult<HtmlDocument> {
    let mut document = html_document_common(/* …转发全部参数… */)?;
    // 锚点后处理（副作用）折叠进缓存体，见 4.3
    let targets = document.introspector().link_targets();
    let anchors = crate::link::create_link_anchors(&mut document, &targets);
    document.introspector_mut().set_anchors(anchors);
    Ok(document)
}
```

最内层 `html_document_common` 没有任何 memoize 标注——它是被两个 `_impl` 复用的纯实现，承担 realize / convert / finalize 的全部重活：

[document.rs:125-138](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L125-L138) — 共享实现，真正干活，不被单独缓存。

#### 4.1.4 代码实践

**实践目标**：亲眼确认「缓存命中时函数体不执行」这一事实，建立对 memoize 边界的直觉。

**操作步骤**：

1. 打开 [document.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs)，在 `html_document_impl` 函数体第一行（`let mut document = html_document_common(...)` 之前）临时加一句 `eprintln!("html_document_impl body executed");`。
2. 在最外层 `html_document` 的函数体第一行也加一句 `eprintln!("html_document entry");`。
3. 编译整个 typst（`cargo build` 在仓库根目录），然后用 typst CLI 把任意一份 `.typ` 文件导出为 HTML：`typst compile --format html input.typ output.html`。

**需要观察的现象**：

- 「html document」这个 `#[typst_macros::time]` 计时名（[document.rs:24](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L24)）在终端统计里出现。由于单次 CLI 调用通常只编译一两轮，缓存效果可能不明显。
- 若想观察多轮内省，可在导出含交叉引用（如 `#ref`、`query`）的复杂文档时留意 `eprintln` 输出次数。

**预期结果**：外层 `html_document` 的 `eprintln` 每轮内省都打印一次（因为它不被缓存）；而 `html_document_impl` 的 `eprintln` 只在**首轮或输入变化时**打印，后续命中缓存的轮次不再打印。这正是「拆解层每次跑、缓存层命中即跳过」的可观测体现。

> **待本地验证**：具体打印次数取决于文档触发的内省轮数与缓存命中率，请以本地实际输出为准。**实践结束后务必用 `git checkout` 还原你加的 `eprintln`，切勿提交对源码的改动。**

#### 4.1.5 小练习与答案

**练习 1**：如果把 `#[comemo::memoize]` 从 `html_document_impl` 挪到最内层 `html_document_common` 上，缓存还能正确工作吗？为什么？

**参考答案**：不完全可以。`html_document_common` 的参数里包含一个 owned `locator: Locator`（[document.rs:136](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L136)），而 owned `Locator` 不是为 memoize 设计的可追踪句柄；更重要的是，`html_document_impl` 里的锚点后处理（`create_link_anchors` + `set_anchors`）会落在缓存体**之外**，导致每次调用都重跑锚点注入、却复用了「未注入锚点」的 DOM。当前设计把 memoize 放在 `_impl`、把副作用包进 `_impl` 体，正是为了让缓存存的是「已完成锚点注入」的成品。

**练习 2**：`#[expect(clippy::too_many_arguments)]` 出现在每个 `_impl` 上（[document.rs:44](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L44)）。这反映了什么设计权衡？

**参考答案**：memoize 必须把所有影响输出的输入都列为参数，才能正确构造缓存键。编译过程依赖 `world / library / introspector / traced / sink / route / content / styles` 这一大堆上下文，于是参数必然很多。typst-html 宁可接受「参数过多」的 lint、也要保证缓存键完整，避免漏掉某个输入导致脏缓存。

---

### 4.2 Tracked / TrackedMut 参数：为什么要把 Engine 逐字段拆开传

#### 4.2.1 概念说明

4.1 说「所有参数都必须可哈希」。可是 `html_document` 手里只有一个 `&mut Engine`——一个胖引用，既不能直接 `Hash`（哈希一个地址毫无意义），又会把可变借用横跨整个缓存查找，这在 Rust 借用检查器眼里几乎寸步难行。所以必须把 `Engine` 拆开。

先看 `Engine` 里到底装了什么：

[engine.rs:18-36](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L18-L36) — `Engine` 的六个字段，每个都是一种「编译上下文」：

| 字段 | 类型 | 在 memoize 里怎么处理 |
| --- | --- | --- |
| `world` | `Tracked<dyn World>` | 已经是 tracked，直接传 |
| `library` | `&'a LazyHash<Library>` | `LazyHash` 按内容哈希，直接传引用 |
| `introspector` | `Protected<Tracked<dyn Introspector>>` | `into_raw()` 拆掉 `Protected`，取出内层 tracked |
| `traced` | `Tracked<Traced>` | 已经是 tracked，直接传 |
| `sink` | `TrackedMut<Sink>` | `reborrow_mut` 重借出 `TrackedMut` |
| `route` | `Route<'a>` | **不是** tracked，需 `.track()` 变成 `Tracked<Route>` |

这里有两个关键概念：

- **`Tracked<T>`**：对「被 `#[comemo::track]` 标注的类型」的不可变追踪句柄。它的 `Hash` **不是**把底层对象整个哈希一遍，而是哈希它的「身份/版本」。底层对象一变（generation 变更），所有用到它的缓存自动失效。这样既便宜又正确。
- **`TrackedMut<T>`**：可变追踪句柄，用于那些「边编译边收集副作用」的对象，比如 `Sink`（收集警告、内省记录、延迟错误）。`Sink` 的 track 方法全是 `(&mut self, …) -> ()` 形态（[engine.rs:204-235](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L204-L235)），注释里明确说「原则上不需要校验返回值」——它只是个单向漏斗。

> 名词解释：**reborrow（重借）**。`TrackedMut::reborrow_mut(&mut engine.sink)` 不是 move，而是从现有的 `TrackedMut` 里临时再借出一个新的 `TrackedMut`，生命周期更短，但指向同一个底层 `Sink`。这让 `_impl` 能拿到一个可哈希的 `TrackedMut<Sink>` 当缓存键，又不夺走 `engine.sink` 的所有权。

#### 4.2.2 核心流程

把 `Engine` 转成 memoize 参数链的关键转换：

```
engine.world                        ──直接──>  world:        Tracked<dyn World>
engine.library                      ──直接──>  library:     &LazyHash<Library>
engine.introspector.into_raw()      ──拆壳──>  introspector: Tracked<dyn Introspector>
engine.traced                       ──直接──>  traced:      Tracked<Traced>
TrackedMut::reborrow_mut(&mut …sink)──重借──>  sink:        TrackedMut<Sink>
engine.route.track()                ──打包──>  route:       Tracked<Route>
content, styles                     ──直接──>  content: &Content, styles: StyleChain
```

每一步都把「带生命周期的引用 / 非 tracked 的值」规整成「comemo 能哈希、能比较」的形态。`Route` 是唯一需要 `.track()` 的，因为它本身是一个普通结构体而非 tracked 句柄——`.track()` 把它包成 `Tracked<Route>` 才能进缓存键。

特别注意 `introspector.into_raw()`：`Engine.introspector` 的类型是 `Protected<Tracked<dyn Introspector>>`，外面包了一层 `Protected`。`Protected` 是 typst-utils 里的一个「访问时要求说明理由」的 newtype（[protected.rs:1-32](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-utils/src/protected.rs#L1-L32)），本身只起类型层面的提醒作用。`into_raw()` 把它拆开取出内层 tracked 句柄；进入 `_impl` 后又用 `Protected::from_raw(...)` 重新包回去（见 [document.rs:139](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L139)），从而在 `_impl` 内部重建一个 `Engine`。

> 易混点：这里传进 memoize 的 `introspector` 是**编译期用来查 query 的父级内省器**（来自 `Engine`），**不是**即将新建的 `HtmlDocument` 自己的内省器——后者此刻还不存在。这两者不要混淆。

#### 4.2.3 源码精读

`Engine` 的完整字段定义，注意 `introspector` 被包在 `Protected<Tracked<…>>` 里：

[engine.rs:18-36](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L18-L36) — 六个字段，每个对应 memoize 的一个参数。

`Route` 需要 `.track()` 才能进缓存键，且 `track()` 会做一个小优化：如果本段 route 不携带信息（无 id 且长度为 0），就跳过这一链路直接返回外层，避免无意义的缓存分叉：

[engine.rs:316-323](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L316-L323) — `Route::track` 的「贡献为零则跳过」优化，旨在提升缓存复用。

`Sink` 的 track 方法全是 `(&mut self, …) -> ()`，正是它能作为 `TrackedMut` 安全推进副作用的基础：

[engine.rs:204-235](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L204-L235) — `Sink` 的 track 方法：`introspection` / `delayed_error` / `warn` / `value` 全是无返回值的单向写入。

`Protected` 的定义，证明 `into_raw` / `from_raw` 只是无成本的拆装：

[protected.rs:1-32](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-utils/src/protected.rs#L1-L32) — `Protected` 在运行期零开销，仅作类型层面的「访问需说明」提醒。

#### 4.2.4 代码实践

**实践目标**：亲手验证「漏掉一个 Engine 字段会破坏缓存正确性」这一论断。

**操作步骤**（源码阅读型实践，不改源码）：

1. 对照 [engine.rs:18-36](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L18-L36) 的 `Engine` 字段表，列出 `html_document_impl` 的全部参数（[document.rs:45-54](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L45-L54)），做一个一一对应表。
2. 思考：如果 `html_document_impl` 漏掉 `traced: Tracked<Traced>` 参数（即不在缓存键里包含它），会出什么问题？提示——`Traced` 用来在 IDE「跳转到查询结果」时追踪某个 span（[engine.rs:120-143](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L120-L143)）。
3. 再思考：如果漏掉 `sink`（警告漏斗），用户改了文档但 warnings 不更新，会发生什么？

**需要观察的现象**：你能说出每个字段「一旦从缓存键里消失，哪一类正确性会塌」。

**预期结果**：

| 漏掉的参数 | 后果 |
| --- | --- |
| `traced` | IDE 追踪查询时拿到陈旧的 traced 值，定位错误 |
| `sink` | 警告/延迟错误不随文档更新而刷新，缓存返回旧的副作用集合 |
| `route` | 深度限制（`check_html_depth`）相关的报错可能错位或丢失 |
| `introspector` | query 结果失效却不重算，交叉引用全部错乱 |

结论：**memoize 的正确性等价于「缓存键完整覆盖所有影响输出的输入」**，少一个就会脏缓存。这就是为什么参数宁可多（`#[expect(clippy::too_many_arguments)]`）也不能少。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `library` 用 `&LazyHash<Library>` 就能直接进缓存键，而 `route` 必须先 `.track()`？

**参考答案**：`LazyHash<T>` 是一个「缓存了哈希值」的 `Arc<T>` 包装，它实现了 `Hash`（按内容），所以一个引用就能当缓存键。`Route` 本身是一个带生命周期、带 `AtomicUsize` 字段的普通结构体（[engine.rs:258-281](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L258-L281)），既不适合直接哈希、其「深度」信息也需要以 tracked 方式参与缓存失效，因此要先 `.track()` 包成 `Tracked<Route>`。

**练习 2**：`Protected::from_raw` 与 `Protected::new` 实现一模一样（[protected.rs:10-20](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-utils/src/protected.rs#L10-L20)），为什么 typst-html 在 `_impl` 内部重建 `Engine` 时非要用 `from_raw`？

**参考答案**：这是语义上的自我约束。`new` 表示「首次包装一个值」，`from_raw` 表示「把先前用 `into_raw` 拆出来的值原样装回去」。`html_document` 用 `into_raw` 拆、`_impl` 用 `from_raw` 装，配对使用，表明「这是同一份 introspector 句柄的接力，不是凭空造的新值」。运行期零差别，但在代码审查时能提醒读者这段 tracked 句柄的来源。

---

### 4.3 html_document_impl 与 html_document_for_bundle_impl：缓存边界与内省器副作用

#### 4.3.1 概念说明

到目前为止，memoize 看起来很完美：参数可哈希、函数近似纯函数。但 typst-html 偏偏有一处「不纯」的硬伤——**编译完成后还要回头改 DOM 与内省器**。

具体来说：`html_document_common` 产出 `HtmlDocument` 后，typst-html 还要做两步**事后处理**：

1. `create_link_anchors(&mut document, &targets)`：为被文档内链接指向的元素分配人类可读的 fragment ID，**就地改写 DOM**（经 `root_mut`）。
2. `introspector_mut().set_anchors(anchors)`：把锚点 ID 写回内省器。

这两步是**有副作用的、改写已生成结构**的操作，与「纯函数」相悖。typst-html 的处理方式很巧妙：**把它们折叠进 `html_document_impl` 的函数体**，让它们只在「缓存未命中」时随函数体一起跑一次。于是缓存里存的是「已注入锚点的成品」，缓存命中时这些副作用自然也「复用」了，不会再跑。

但这引出本讲最核心的一个结论：**`HtmlDocument` 故意不实现 `Hash`**。原因写在 dom.rs 的类型注释里：

> Unlike the `PagedDocument`, this does not implement `Hash` because the HTML introspector is neither hashable nor guaranteed to be 100% derived from the output (due to the presence of `root_mut` which is used for cross-linking).

翻译过来：HTML 内省器**既不可哈希，也不是 100% 由输出派生的**——因为 `root_mut`（用于交叉链接）的存在。这句话信息量很大，4.3.4 的实践会逐字拆它。

#### 4.3.2 核心流程

`html_document_impl` 的完整流程（缓存未命中时）：

```
html_document_impl（缓存边界）
  ├─ html_document_common(...)        → HtmlDocument（DOM + info + 内省器）
  │     ├─ realize → convert_to_nodes → finalize_dom
  │     ├─ resolve_inline_styles
  │     └─ HtmlDocument::new(...)     ← 内省器在此刻构建（anchors 为空）
  ├─ document.introspector().link_targets()   → 被链接的目标集合
  ├─ create_link_anchors(&mut document, …)    → 改写 DOM（root_mut）+ 返回 anchors
  ├─ document.introspector_mut().set_anchors(anchors)  → 改写内省器
  └─ Ok(document)                      ← 缓存的就是这个「已注入锚点」的成品
```

关键点：

- **内省器构建于 `HtmlDocument::new`**（[document.rs:36-39](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L36-L39)），此时 anchors 为空。
- **锚点注入发生在 `_impl` 内、`new` 之后**，所以注入前的内省器状态与注入后不同——内省器不是「输出 DOM 的纯函数」。
- **`bundle` 路径跳过锚点注入**：`html_document_for_bundle_impl` 只调 `html_document_common`，不做 `create_link_anchors` / `set_anchors`（因为 bundle 场景下锚点由外层统一处理）。这就是两个 `_impl` 复用同一份 `common`、却各有不同后处理的原因。

#### 4.3.3 源码精读

`HtmlDocument` 的定义与「不实现 Hash」的注释——本讲代码实践的依据：

[dom.rs:20-30](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L20-L30) — `#[derive(Debug, Clone)]` 里**没有** `Hash`，注释给出两条理由。

`root_mut` 的注释，把「改 root 可能搞坏内省器」的隐患与 issue #7951 直接挂钩：

[dom.rs:46-52](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L46-L52) — `root_mut` 文档承认这是已知技术债，待 #7951 修复：

```rust
/// Technically, mutating the root can mess up the introspector. This should
/// be fixed at some point (https://github.com/typst/typst/issues/7951).
pub fn root_mut(&mut self) -> &mut HtmlElement {
    self.output.root_mut()
}
```

锚点后处理折叠在 `html_document_impl` 体内——这就是「副作用只在缓存未命中时跑」的落点：

[document.rs:55-73](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L55-L73) — `create_link_anchors` 与 `set_anchors` 紧跟 `html_document_common` 之后，全在 memoize 函数体内。

`create_link_anchors` 内部正是通过 `document.root_mut().children` 改写 DOM（u5-l4 详述）：

[link.rs:26-45](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/link.rs#L26-L45) — `&mut HtmlDocument` 经 `root_mut()` 拿到可变 children 并就地挂 id。

bundle 版本的 `_impl`：复用 `html_document_common`，但**没有**锚点后处理：

[document.rs:97-123](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L97-L123) — bundle 缓存层，直接 `Ok(html_document_common(...))`，跳过锚点注入。

#### 4.3.4 代码实践（本讲核心实践）

**实践目标**：结合 dom.rs 注释与 issue #7951，把「`HtmlDocument` 为何不实现 `Hash`」这句注释拆解成三条可检验的论断，并讨论它对缓存正确性与 `root_mut` 用法的影响。

**操作步骤**：

1. 读 [dom.rs:20-30](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L20-L30) 的注释，把它说的「不实现 Hash 的原因」拆成两条：
   - (a) 内省器**不可哈希**；
   - (b) 内省器**不是 100% 由输出派生的**。
2. 用本讲 4.3 的源码链为每一条找证据：
   - 对 (a)：看 `HtmlDocument.introspector` 是 `Arc<HtmlIntrospector>`（[dom.rs:29](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L29)）。`HtmlIntrospector` 内含事后注入的 `anchors`（u5-l3），不是「由 DOM 决定的纯派生量」，所以无法稳定实现 `Hash`。
   - 对 (b)：看 [document.rs:67-70](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/document.rs#L67-L70)——`create_link_anchors` 通过 `root_mut` 改写 DOM 后，内省器并没有跟着重建；也就是说「同一个 DOM」可以对应「注入锚点前后」两个不同的内省器状态。
3. 读 [dom.rs:46-52](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L46-L52) 的 `root_mut` 注释，并打开 issue #7951（链接就在注释里）了解「改 root 可能搞坏内省器」的已知隐患。
4. 回答下面三个问题（见「预期结果」）。

**需要观察的现象**：你能用源码位置（而非凭空想象）论证「为什么 `HtmlDocument` 不能实现 `Hash`」。

**预期结果**：

- **问题 1——不实现 `Hash` 会影响缓存吗？** 不会。comemo 缓存 `html_document_impl` 用的是**参数**的哈希，返回类型 `HtmlDocument` 不需要 `Hash`。所以「不实现 Hash」与「能被缓存」并不矛盾。
- **问题 2——那不实现 `Hash` 限制了什么？** 它意味着 `HtmlDocument` **不能作为另一个 memoize 函数的参数**。事实上 typst-html 的编码入口 `html(&HtmlDocument, …)` 就**没有** memoize（u5-l1），它直接读 `&HtmlDocument` 输出字符串。缓存止步于 `html_document_impl`，这是有意为之的边界。
- **问题 3——对 `root_mut` 用法的影响？** 因为 `root_mut` 会改 DOM 却不重建内省器，内省器与 DOM 可能短暂不一致（issue #7951 的技术债）。typst-html 的对策是：把所有「`root_mut` 改写」集中在 `html_document_impl` 体内、在缓存写入**之前**一次性完成（`create_link_anchors`），此后不再对外暴露「边改 DOM 边查内省」的机会。`root_mut` 虽然是 `pub`，但注释明确警告调用者「改它可能搞坏内省器」。

> **待本地验证**：issue #7951 的具体讨论内容请以 GitHub 上的实际 issue 为准（注释中的链接即为其 URL）。本实践不要求修改源码。

#### 4.3.5 小练习与答案

**练习 1**：假如有人强行给 `HtmlDocument` 派生 `Hash`，并在某个 memoize 函数里把它当参数，会在什么时刻给出错误结果？

**参考答案**：会在「锚点注入前后」给出错误结果。注入锚点会改变 DOM（`root_mut`）与内省器，但若按「注入前的 DOM + 注入前的内省器」算出一个哈希并存进缓存，后续以「注入后的 DOM」再次调用时，comemo 可能因哈希恰好相同而返回注入前的陈旧结果，或在哈希不同时无谓地重算。更根本地，`HtmlIntrospector` 内部状态本就难以稳定哈希，强行 derive 会得到一个「语义上不稳定」的哈希，违背 memoize 的纯函数前提。

**练习 2**：为什么 bundle 路径（`html_document_for_bundle_impl`）可以不做锚点注入？这跟 bundle 的语义有什么关系？

**参考答案**：bundle 表示这份 HTML 是某个更大文档 bundle 的一部分，文档内链接的锚点要在「整份 bundle」的层面统一分配与去重（`AnchorGenerator` 的去重是 per-document 的，bundle 场景下由外层统筹）。所以 bundle 的 `_impl` 只产出「未注入锚点」的 `HtmlDocument`，把锚点职责上交，避免子文档各自注入导致 id 冲突。这也说明 memoize 缓存边界要与「副作用的归属层级」对齐。

---

### 4.4 html_block_fragment_impl 的缓存，与 inline 片段的不缓存

#### 4.4.1 概念说明

u3-l4 已经介绍过三类片段入口。本讲从「能不能 memoize」的视角重看它们，会发现一个鲜明的对照：

- **`html_block_fragment` → `html_block_fragment_impl`**：标准三明治，`_impl` 被 `#[comemo::memoize]` 包裹，**可缓存**。
- **`html_inline_fragment`**：单个函数，**没有 `_impl`、没有 memoize，不可缓存**。
- **`html_math_fragment`**：与 inline 同形态，**也不可缓存**。

为什么 block 能缓存、inline 不能？根因在两个共享状态：**定位（Locator）**与**智能引号（SmartQuoter）**。

- **block 片段**收到的是 owned `Locator`（公开签名 [fragment.rs:21](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/fragment.rs#L21)），在 `_impl` 内用 `LocatorLink::new(locator.track())` 派生出一棵**独立的定位子树**。这棵子树完全由 tracked 的 locator 决定，可复现——满足「输入决定输出」。
- **block 片段**用的是 `ConversionLevel::Block`，`convert_to_nodes` 会**内部自建一个全新的 `SmartQuoter::new()`**（[convert.rs:72-74](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L72-L74)）。也就是说，块级片段的智能引号状态**自包含、不外泄**，与外界无关——满足「近似纯函数」。

而 inline 片段恰好相反：

- 它收的是 `&mut SplitLocator` 与 `&mut SmartQuoter`——**借用**了外层的、正在流式推进的可变状态。
- 智能引号是**有上下文**的：一个引号该渲染成开引号还是闭引号，取决于它前面的字符。所以 inline 片段的输出**不是它自身输入的纯函数**，而是「输入 + 当前累积的 quoter 状态」的函数。一旦 memoize，就会把「某个上下文里的引号结果」冻结下来套用到别的上下文，给出错的引号。

fragment.rs 的文档注释把这件事说得很直白：

> The difference to block-level content is that inline-level content has shared smartquoting state with surrounding inline-level content. This requires mutable state, which is at odds with memoization.

注释还补了一句自我安慰：「不过要是每个 inline 片段都缓存，缓存粒度也细得没必要，所以这样刚好两全其美。」

#### 4.4.2 核心流程

block 片段（可缓存）的处理链：

```
html_block_fragment(&mut Engine, content, Locator, styles, ws)   公开入口
  └─ 拆 Engine → 转发
html_block_fragment_impl(world, …, locator: Tracked<Locator>, …)  #[comemo::memoize]
     ├─ Protected::from_raw(introspector)
     ├─ LocatorLink::new(locator) → Locator::link(&link).split()   独立定位子树
     ├─ 新建 Engine（route = Route::extend(route)）
     ├─ engine.route.check_html_depth()?                            深度上限 72
     ├─ realize_fragment(...)                                       RealizationKind::Fragment
     └─ convert_to_nodes(..., ConversionLevel::Block, ws)           ← 内部新建 SmartQuoter
```

inline / math 片段（不可缓存）的处理链（以 inline 为例）：

```
html_inline_fragment(&mut Engine, content, &mut SplitLocator, &mut SmartQuoter, styles, ws)
     ├─ engine.route.increase(); check_html_depth()?                直接在借用上递增
     ├─ realize_fragment(...)                                       同 block
     ├─ convert_to_nodes(..., ConversionLevel::Inline(quoter), ws)  ← 共享外层 quoter
     └─ engine.route.decrease()
```

两条链最大的区别就在 `ConversionLevel` 与 `SmartQuoter` 的归属：block 自建、inline 共享。这决定了能否 memoize。

#### 4.4.3 源码精读

`html_block_fragment_impl` 被 memoize，并用 `LocatorLink` 从 tracked locator 派生独立子树：

[fragment.rs:39-77](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/fragment.rs#L39-L77) — 缓存层：owned/tracked locator 派生独立子树，`ConversionLevel::Block` 自带全新 quoter。

```rust
#[comemo::memoize]
fn html_block_fragment_impl(
    world: Tracked<dyn World + '_>,
    // …其余 tracked 参数…
    locator: Tracked<Locator>,
    styles: StyleChain,
    whitespace: Whitespace,
) -> SourceResult<EcoVec<HtmlNode>> {
    let introspector = Protected::from_raw(introspector);
    let link = LocatorLink::new(locator);
    let mut locator = Locator::link(&link).split();   // 独立、可复现的定位子树
    let mut engine = Engine { /* …route: Route::extend(route)… */ };
    engine.route.check_html_depth().at(content.span())?;
    let arenas = Arenas::default();
    let children = realize_fragment(&mut engine, &mut locator, &arenas, content, styles)?;
    crate::convert::convert_to_nodes(
        &mut engine, &mut locator, children.iter().copied(),
        ConversionLevel::Block,   // ← 内部会 SmartQuoter::new()
        whitespace,
    )
}
```

inline 片段**没有** memoize，且把 `&mut SmartQuoter` 透传进 `ConversionLevel::Inline`：

[fragment.rs:79-111](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/fragment.rs#L79-L111) — inline 片段：文档注释明确「与 memoization 冲突」，函数无 `#[comemo::memoize]`。

`ConversionLevel::Block` 分支在 `convert_to_nodes` 内部新建 quoter，是 block 可缓存的最后一块拼图：

[convert.rs:71-74](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L71-L74) — Block 自建 quoter、Inline 复用传入 quoter。

深度上限定义与 `check_html_depth`：把无限递归（如 `show math.equation: html.frame` 造成的 Frame 嵌套，见 u3-l5）转化为友好报错：

[engine.rs:340-385](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L340-L385) — `MAX_HTML_DEPTH = 72`，`check_html_depth` 超限即 bail。

#### 4.4.4 代码实践

**实践目标**：用一个智能引号的例子，亲手验证「inline 片段输出依赖上下文，因此不能安全缓存」。

**操作步骤**（源码阅读 + 推理型实践，不改源码）：

1. 准备一段含智能引号的 Typst 文本，例如先后两处引号：`"Hello," she said, "world!"`（在 Typst 里智能引号默认开启）。
2. 读 [convert.rs:23-31](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L23-L31) 的 `ConversionLevel` 文档，确认 `Inline` 变体携带的是 `&mut SmartQuoter`（共享），而 `Block` 是「自带本地引号状态」。
3. 推理：假设 `html_inline_fragment` 被（错误地）加上了 `#[comemo::memoize]`。考虑同一个 inline 片段内容（比如 `"world!"`）在两种上下文里出现：
   - 上下文 A：前一个字符是空格——这里应是**开引号**。
   - 上下文 B：前一个字符是字母——这里应是**闭引号**。
4. 由于 memoize 只按「片段内容 + styles」做键（它无法把 quoter 的累积状态纳入键，因为那是 `&mut` 借用、不可哈希），第一次调用（上下文 A）的结果会被缓存，第二次（上下文 B）命中缓存，返回**错误的开引号**。

**需要观察的现象**：你能说清「为什么 quoter 状态无法进缓存键」——它是 `&mut SmartQuoter`，既是引用又是可变的，没有稳定的 `Hash` 实现。

**预期结果**：inline 片段若被 memoize，会在「同内容、不同上下文」时返回陈旧的引号方向，导致 HTML 里引号开闭错乱。这正是 typst-html 让 inline/math 片段**保持不缓存**的根本理由。block 片段因为自建 quoter、状态自包含，不存在此问题，故可安全 memoize。

> **待本地验证**：上述「上下文 A/B」是推理示意，无需真正给源码加 memoize（那会破坏正确性）。你可以用 typst CLI 实际导出含智能引号的文档，观察正常（未缓存）输出里引号方向是否随上下文正确变化，以此佐证「引号是上下文相关」的前提。

#### 4.4.5 小练习与答案

**练习 1**：除了 `SmartQuoter`，inline 片段还借用了什么「共享可变状态」使它无法 memoize？

**参考答案**：还借用了 `&mut SplitLocator`（[fragment.rs:91](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/fragment.rs#L91)）。`SplitLocator` 在行内流里被就地推进（每处理一个元素就消耗一段定位），它的状态也是累积的、`&mut` 的，同样无法稳定哈希。相比之下 block 片段从 tracked locator 派生一棵**独立**子树，定位状态自包含、可复现，这是它能 memoize 的另一半理由。

**练习 2**：`html_block_fragment_impl` 在函数体里调 `engine.route.check_html_depth()`（[fragment.rs:66](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/fragment.rs#L66)）。既然函数被 memoize，这个深度检查会不会在缓存命中时被跳过、从而漏报过深？

**参考答案**：会，缓存命中时函数体（含深度检查）确实整段跳过。但这不会「漏报」：缓存键里包含 `route: Tracked<Route>`（[fragment.rs:48](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/fragment.rs#L48)），相同的 route 意味着相同的深度上下文——既然上次在这个深度下检查通过、产出了结果，那么在完全相同的深度下复用该结果是安全的。换句话说，「深度是否超限」本身也是 route 的函数，纳入键后就不会出现「换个更深的 route 还命中旧缓存」的情况。

**练习 3**：block 片段在 `handle_html_elem` 里，块级子元素处理完后会把外层 quoter 重置（`*converter.quoter = SmartQuoter::new();`，[convert.rs:210](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/convert.rs#L210)）。这与「block 片段可缓存」有什么关系？

**参考答案**：这次重置只发生在「父级本身是 inline 流」时（因为只有 inline 父级才有一个被共享的外层 quoter 可重置）。它的语义是「块级元素会切断行内引号上下文」。这与缓存无关——它影响的是**外层 inline 流**的 quoter，而非 block 片段**内部**自建的 quoter。block 片段内部的 quoter 始终是全新的、用完即弃的，这正是它能被当作纯函数缓存的前提。

## 5. 综合实践

把本讲四节串起来，做一次「缓存边界审计」。

**任务**：假设团队要在 typst-html 里新增一个「把指定元素子树单独导出为 HTML 片段」的函数 `html_subtree_fragment`，它需要把某个 `HtmlElement` 的 body 重新走一遍 realize + convert。请你为它设计函数签名与分层，并回答：

1. 它应该用「公开入口 + `_impl` + memoize」的三明治结构，还是单个不缓存函数？依据是什么？
2. 如果选择 memoize，列出它必须作为参数传入的全部 tracked/可哈希上下文（对照 4.2 的 Engine 字段表）。
3. 它的 body 转换应当用 `ConversionLevel::Block` 还是 `Inline`？这个选择会如何影响「能否缓存」？
4. 如果它内部需要事后改写 DOM（类似 `create_link_anchors`），应当把这段改写放在 memoize 函数体的**内**还是**外**？为什么？

**参考思路**：

1. 若 body 是块级、引号状态自包含，且能从 tracked locator 派生独立定位子树，则值得用三明治 + memoize（复用 `html_block_fragment_impl` 的范式）；若 body 是行内、需要共享外层 quoter/locator，则必须像 `html_inline_fragment` 那样不缓存。判断依据就是 4.4 的两条标准：**定位是否独立、引号状态是否自包含**。
2. 至少需要 `world / library / introspector / traced / sink / route / content / styles`（与 `html_block_fragment_impl` 一致），外加描述「子树位置」的 `locator: Tracked<Locator>`。少任何一个都会脏缓存（见 4.2.4 的后果表）。
3. 块级用 `Block`（可缓存）、行内用 `Inline`（不可缓存）。这是「能否 memoize」的决定性开关。
4. 放在 memoize 函数体**内**（像 `html_document_impl` 那样把 `create_link_anchors` 折叠进去）。这样副作用只在缓存未命中时随函数体跑一次，缓存里存的是「已改写」的成品；若放在体**外**，每次调用都会重跑改写，却复用未改写的陈旧 DOM，与 4.1.5 练习 1 的反例完全相同。

> 这个练习不需要你真的写代码——它的价值在于让你用本讲的四条原则（参数可哈希、近似纯函数、副作用折叠进缓存体、缓存边界对齐副作用归属）去**审计**一个新设计。

## 6. 本讲小结

- typst-html 用 **comemo memoize** 给编译过程加缓存，缓存以**参数哈希**为键——返回类型不必 `Hash`，但每个参数都必须可哈希。
- 为此采用**三层夹心**：公开入口拆解 `&mut Engine` → `_impl`（`#[comemo::memoize]` 缓存边界）→ `common`（共享实现）。公开层不被缓存、每次只做参数搬运。
- 把 `Engine` **逐字段**拆成 `Tracked` / `TrackedMut` 参数，是因为引用本身不能当缓存键；`world/library/introspector/traced/sink/route` 每一个都对应一项影响输出的输入，少一个就会脏缓存（故有 `#[expect(clippy::too_many_arguments)]`）。
- `HtmlDocument` **故意不实现 `Hash`**：其内省器既不可哈希、也不 100% 由输出派生（`root_mut` 用于交叉链接，见 issue #7951）。这不影响它被缓存（返回值无需 `Hash`），但意味着它不能再当别的 memoize 函数的参数——编码入口 `html` 因此不缓存。
- `html_document_impl` 把 `create_link_anchors` + `set_anchors` 这两步**有副作用的 DOM/内省器改写折叠进缓存体**，让它们只在未命中时跑一次，缓存里存的是「已注入锚点」的成品；bundle 路径跳过此步，因锚点由外层统一处理。
- `html_block_fragment_impl` **可缓存**（独立定位子树 + `ConversionLevel::Block` 自建 `SmartQuoter`）；`html_inline_fragment` / `html_math_fragment` **不可缓存**（借 `&mut SplitLocator` + `&mut SmartQuoter` 共享行内上下文，输出不是输入的纯函数）。三类片段入口都先 `check_html_depth`（上限 72）防无限递归。

## 7. 下一步学习建议

- **回看内省器的构建与锚点注入**：本讲反复提到「内省器事后被 `set_anchors` 改写」。完整的内省器构建流程见 u5-l3（`HtmlIntrospector` 的 `new`/`set_anchors` 两段式生命周期），锚点分配细节见 u5-l4（`create_link_anchors` 的 Work 队列与 `AnchorGenerator`）。把本讲与这两讲对照，能看清「缓存边界」与「内省副作用」是如何咬合的。
- **阅读 comemo 本身**：若想彻底理解 `Tracked` 的「按版本哈希、按依赖失效」机制，可去 typst 仓库的 `crates/typst-macros` 看 `#[comemo::track]` / `#[comemo::memoize]` 宏的展开，或阅读 comemo crate 的源码。本讲只在「使用层」讲了它，宏层面的细节是自然的下一步。
- **横向对比 Paged 导出**：dom.rs 注释特意拿 `PagedDocument`（实现了 `Hash`）与 `HtmlDocument`（未实现）对比。可以去 `crates/typst` 看 `PagedDocument` 的定义与它在 memoize 中的角色，理解「为什么分页导出能实现 Hash、而 HTML 不能」——这会加深你对「内省器是否由输出派生」这一判据的理解。
- **关注 issue #7951**：本讲点名的这项技术债（`root_mut` 改 DOM 可能搞坏内省器）若被修复，`HtmlDocument` 的缓存与内省设计可能随之调整。追踪该 issue 是观察 typst-html 架构演进的一个好切入点。
