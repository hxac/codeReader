# Engine 与状态传递：comemo 记忆化模式

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `Engine` 这个结构体里每个字段的作用，以及它为什么是「编译上下文」而不是「排版参数」。
- 解释 comemo 的 `Tracked` / `TrackedMut` / `Track` / `#[comemo::memoize]` / `#[comemo::track]` 各自解决什么问题。
- 看懂贯穿本 crate 的「公开函数 + `#[comemo::memoize]` 的 `*_impl` 函数」这一固定写法：公开函数如何把 `Engine` 拆成一堆 tracked 参数，再交给记忆化的 impl。
- 理解这种拆解带来的三大好处：**可缓存**、**可并行**、**可跨多轮内省（introspection）收敛**。
- 解释 `Route::extend` 与 `engine.route.check_layout_depth()` 在排版嵌套中的守门作用。
- 理解 `Sink` 如何把警告、延迟错误、内省记录等副作用收集起来，从而支撑并行与收敛。

本讲承接 [u1-l4 端到端排版流程](u1-l4-end-to-end-pipeline.md)：你已经知道 content 要经过 `realize → layout_pages → PagedDocument`，但还没看清「排版函数之间到底用什么传递状态」。本讲就把这层基础设施讲透——它是理解后续 flow / inline / grid / math 所有 layouter 的前提。

## 2. 前置知识

在进入源码前，先用最朴素的语言建立三个直觉。

### 2.1 为什么要「记忆化」

Typst 排版不是一次跑完的。一个文档里如果有 `counter`、`query`、`outline`、页码这类「需要先知道结果才能决定怎么排」的东西，Typst 会反复排版直到结果稳定（这叫**内省收敛**，introspection convergence）。如果每次都把整篇文档从头到尾重排一遍，会慢得不可接受。

解决办法是**记下来**：某个排版函数给定相同输入就返回相同输出，于是第二次遇到完全相同的输入时直接查表。这就是 **comemo** 这个 crate 提供的「记忆化」（memoization）。但有个前提——「相同输入」要能被快速判断，这就引出 `Tracked`。

### 2.2 Tracked 是什么：一张「带缓存的引用」

普通引用 `&T` 没法参与记忆化，因为要判断「两次调用传入的引用是否指向同一个东西」很贵（得逐字节比较，甚至要遍历整个 `World`）。

comemo 提供了一个包装类型 `Tracked<'a, T>`，你可以理解为「**携带了被指对象哈希摘要的引用**」。它有两个关键性质：

- 它记录了所指对象的「身份/哈希」，所以判断两次调用是否用了同一个 `Tracked` 极快。
- 它可以 `Copy` / `Clone`，可以在多线程间共享（`Send + Sync`），这让并行排版成为可能。

`TrackedMut<'a, T>` 是它的可变版本，用于那种「往里塞东西」的对象（比如收集警告的 `Sink`）。

把一个普通值变成 `Tracked` 用的是 `Track` trait 提供的 `.track()` 方法；反过来从 `&mut T` 借出一个临时 `TrackedMut` 用的是 `TrackedMut::reborrow_mut`。

> 说明：comemo 是本 crate 外部依赖（见 `Cargo.toml` 第 25 行 `comemo = { workspace = true }`）。本讲只基于它在 typst-layout 里的**使用方式**讲解其语义，不深入 comemo 内部实现。

### 2.3 为什么参数那么多

你会在源码里看到 `_impl` 函数动辄七八个参数。原因很简单：`Engine` 本身**不能**被记忆化缓存（它是个临时构造物），但**它的每个字段**都应该是可追踪的。所以公开函数把 `&mut Engine` 拆成 `world / library / introspector / traced / sink / route` 等独立参数逐个传入。参数多，恰恰是为了让每一个都成为合法的缓存键组成部分。

带着这三个直觉，我们看源码。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/flow/mod.rs` | 片段级排版的公开入口 `layout_frame` / `layout_fragment`，以及记忆化的 `layout_fragment_impl`。是本讲最重要的实例。 |
| `src/pages/mod.rs` | 文档级排版的公开入口 `layout_document` 与记忆化的 `layout_document_impl` / 共用实现 `layout_document_common`。 |
| `src/inline/mod.rs` | 段落排版的公开入口 `layout_par` 与记忆化的 `layout_par_impl`。展示同一套模式在第三处出现。 |
| `crates/typst-library/src/engine.rs` | `Engine` / `Route` / `Sink` / `Traced` 的定义（本讲大量引用，跨 crate）。 |
| `crates/typst-utils/src/protected.rs` | `Protected` 包装器，解释 `introspector` 字段的 `into_raw` / `from_raw`。 |

> 提示：本讲引用了 typst-layout 之外（typst-library、typst-utils）的源码。这些链接使用对应 crate 的 GitHub 路径，行号均基于当前 HEAD `146a58329a`。

## 4. 核心概念与源码讲解

### 4.1 Engine：贯穿排版的「编译上下文」

#### 4.1.1 概念说明

`Engine` 不是「排版参数」，而是**整个编译过程赖以运行的上下文**：它携带世界（字体、文件）、标准库、内省器、被追踪的 span、副作用回收站、以及调用链。typst-layout 里的每一个 layouter，第一行几乎都是 `engine: &mut Engine`。

排版真正需要的几何输入（画布多大、能否断行、有几列）并不在 `Engine` 里，而是通过 `Regions` / `Size` 等单独参数传入。这个区分很重要：**`Engine` 提供环境，`Regions` 提供画布**。

#### 4.1.2 核心流程

`Engine` 在一次文档编译里被反复「拆开—重建」：

1. 最外层的编译驱动（在本 crate 之外）创建第一个 `Engine`。
2. 进入 typst-layout 的某个公开排版函数，函数把 `Engine` 的**字段**逐个取出，交给记忆化的 `_impl`。
3. `_impl` 内部又把这些字段**重新拼装**成一个局部的、新的 `Engine`，供下层（realize、layout_flow 等）继续使用——并且在这一步给 `route` 加上一段（`Route::extend`），表示「我又往下嵌套了一层排版」。
4. 下层 layouter 拿到这个重建的 `&mut Engine`，重复第 2 步。

于是「拆开—拼回」在调用栈每一层都发生一次。

#### 4.1.3 源码精读

`Engine` 的定义全部字段如下：

```rust
pub struct Engine<'a> {
    pub world: Tracked<'a, dyn World + 'a>,
    pub library: &'a LazyHash<Library>,
    pub introspector: Protected<Tracked<'a, dyn Introspector + 'a>>,
    pub traced: Tracked<'a, Traced>,
    pub sink: TrackedMut<'a, Sink>,
    pub route: Route<'a>,
}
```

[crates/typst-library/src/engine.rs:L18-L36](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L18-L36) — `Engine` 结构体。注意：除了 `library`（一个 `&LazyHash` 引用）和 `route`（值类型），其余四个字段都是 `Tracked` / `TrackedMut`，这正是它们能成为记忆化缓存键的原因。

逐字段含义：

| 字段 | 类型 | 提供什么 |
| --- | --- | --- |
| `world` | `Tracked<dyn World>` | 字体、源文件、日期等「外部世界」入口 |
| `library` | `&LazyHash<Library>` | 标准库与 routines（`realize` 等函数指针就挂在 `library.routines`） |
| `introspector` | `Protected<Tracked<dyn Introspector>>` | 查询文档元素位置/编号的能力（被 `Protected` 包裹，访问需给出理由） |
| `traced` | `Tracked<Traced>` | 当前正在被「trace」的 span（用于 IDE 调试） |
| `sink` | `TrackedMut<Sink>` | 警告、延迟错误、内省记录的回收站（可变） |
| `route` | `Route<'a>` | 当前调用链，用于检测循环 import 和过深嵌套 |

`introspector` 被 `Protected` 包裹很有意思——它要求访问者「给出理由」。`Protected` 的定义见 [crates/typst-utils/src/protected.rs:L6-L32](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-utils/src/protected.rs#L6-L32)：`access(&self, justification: &'static str)` 才能取出内层值；而 `into_raw` / `from_raw` 是「拆包/重包」的成对操作。下一节你会看到公开函数用 `into_raw()` 把它拆出来传给 `_impl`，`_impl` 里又用 `from_raw()` 装回去。

#### 4.1.4 代码实践

**目标**：建立「`Engine` 是上下文、`Regions` 是画布」的区分直觉。

**步骤**：

1. 打开 [src/flow/mod.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs)，看 `layout_fragment` 的签名（下文 4.3 会贴出）。
2. 注意它的参数分成两组：`engine: &mut Engine`（上下文）与 `regions: Regions`（画布）。
3. 在 `Engine` 里找有没有任何一个字段表示「页面宽高」——找不到。确认几何信息只存在于 `regions`。

**预期结果**：你会确认排版尺寸完全由 `Regions` 决定，`Engine` 与尺寸无关；这也解释了为什么文档级入口 `layout_document` 不接收 `regions`（画布来自样式链里的 `page` 配置，而非函数参数）。

#### 4.1.5 小练习与答案

**练习 1**：`Engine` 为什么不把 `Regions` 作为字段？
**答案**：因为同一个 `Engine` 在调用栈里会逐层复用，而每一层的画布尺寸不同（页面、列、行内宽度逐级缩小）。把 `Regions` 作为独立参数传入，才能让每一层携带自己的画布；同时 `Engine` 保持「纯环境」语义，便于记忆化。

**练习 2**：`introspector` 字段为什么是 `Protected<Tracked<...>>` 双层包装？
**答案**：外层 `Protected` 是**类型系统层面的提醒**——访问它需要写出理由字符串（见 `access(justification)`），防止随意查询内省器破坏收敛假设；内层 `Tracked` 是 comemo 的追踪引用，使其能参与缓存键计算。

---

### 4.2 comemo 记忆化与「公开函数 → memoized _impl」拆解模式

#### 4.2.1 概念说明

这是本讲的核心。typst-layout 几乎所有公开排版入口都遵循同一套模板：

```
pub fn layout_xxx(engine: &mut Engine, <领域参数>) -> Result<...> {
    layout_xxx_impl(
        engine.world,                      // 拆出 Tracked<dyn World>
        engine.library,                    // 拆出 &LazyHash<Library>
        engine.introspector.into_raw(),    // 拆出 Tracked<dyn Introspector>
        engine.traced,                     // 拆出 Tracked<Traced>
        TrackedMut::reborrow_mut(&mut engine.sink), // 借出 TrackedMut<Sink>
        engine.route.track(),              // 拆出 Tracked<Route>
        <领域参数>,
    )
}

#[comemo::memoize]
fn layout_xxx_impl(world, library, introspector, traced, sink, route, <领域参数>) -> Result<...> {
    // 重新拼一个局部 Engine
    let mut engine = Engine { library, world, introspector: Protected::from_raw(introspector), traced, sink, route: Route::extend(route) };
    // ... 真正干活 ...
}
```

这个写法要做三件事：

1. **把 `Engine` 拆成可追踪的字段**，让它们都能进入记忆化的缓存键。
2. **把真正的领域输入**（`content` / `styles` / `regions` 等）作为独立参数，与上面一起组成完整缓存键。
3. 在 `_impl` 里**重建局部 `Engine`**，让下层代码仍然能用熟悉的 `&mut Engine` 写法。

为什么这样设计能带来可缓存、可并行、可收敛？

- **可缓存**：`#[comemo::memoize]` 会把「全部入参的哈希 → 返回值」存进一张表。下次同样入参直接命中。`Tracked` 让 `world` / `introspector` 这类巨型对象的「身份」可以被廉价哈希。
- **可并行**：`Tracked` 是 `Send + Sync` 且可克隆，多个工作线程可以各拿一份；副作用集中写到各自独立的 `Sink`，最后合并（见 4.5 的 `parallelize`）。
- **可收敛**：内省循环重复调用同一函数时，只要输入没变就直接命中缓存，避免指数级重算。

#### 4.2.2 核心流程

以片段级排版 `layout_fragment` 为例，数据流如下：

1. 调用方传入 `&mut Engine` + `content` + `locator` + `styles` + `regions`。
2. 公开函数把 `Engine` 的六个字段拆出，连同领域参数一起调用 `layout_fragment_impl`。
3. `layout_fragment_impl` 带 `#[comemo::memoize]`：comemo 先算出全部入参的哈希，查表；命中则直接返回缓存的 `Fragment`，不命中才执行函数体。
4. 函数体里重建局部 `Engine`（`route` 被加长一截），调用 `check_layout_depth` 守门，然后 `realize` + `layout_flow` 真正排版。
5. 返回值被 comemo 记进缓存。

#### 4.2.3 源码精读

**实例一：`layout_fragment`（src/flow/mod.rs）**

公开函数负责「拆」：

```rust
pub fn layout_fragment(
    engine: &mut Engine,
    content: &Content,
    locator: Locator,
    styles: StyleChain,
    regions: Regions,
) -> SourceResult<Fragment> {
    layout_fragment_impl(
        engine.world,
        engine.library,
        engine.introspector.into_raw(),
        engine.traced,
        TrackedMut::reborrow_mut(&mut engine.sink),
        engine.route.track(),
        content,
        locator.track(),
        styles,
        regions,
        ColumnOptions { count: NonZeroUsize::ONE, balanced: false, gutter: Rel::zero() },
    )
}
```

[src/flow/mod.rs:L56-L80](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L56-L80) — 公开入口把 `Engine` 拆成六个 tracked 参数传给 `_impl`。注意 `layout_frame`（[src/flow/mod.rs:L42-L51](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L42-L51)）只是它的单区域便捷封装。

记忆化的 `_impl` 负责「拼回 + 干活」：

```rust
#[comemo::memoize]
#[expect(clippy::too_many_arguments)]
fn layout_fragment_impl(
    world: Tracked<dyn World + '_>,
    library: &LazyHash<Library>,
    introspector: Tracked<dyn Introspector + '_>,
    traced: Tracked<Traced>,
    sink: TrackedMut<Sink>,
    route: Tracked<Route>,
    content: &Content,
    locator: Tracked<Locator>,
    styles: StyleChain,
    regions: Regions,
    column: ColumnOptions,
) -> SourceResult<Fragment> {
    // ... 边界检查 ...

    let introspector = Protected::from_raw(introspector);
    let link = LocatorLink::new(locator);
    let mut locator = Locator::link(&link).split();
    let mut engine = Engine {
        library, world, introspector, traced, sink,
        route: Route::extend(route),          // ← 调用链加长一截
    };

    engine.route.check_layout_depth().at(content.span())?;  // ← 守门

    // realize + layout_flow ...
}
```

[src/flow/mod.rs:L114-L170](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L114-L170) — `#[comemo::memoize]` 是缓存边界；函数体内重建局部 `Engine`，并通过 `Route::extend` 把嵌套深度加 1。`#[expect(clippy::too_many_arguments)]` 这个注解本身就承认「参数确实多，但这是 comemo 模式要求的」。

**实例二：`layout_document`（src/pages/mod.rs）**

文档级入口是同一套写法，但它不接收 `regions`，且把 `_impl` 与真正干活的 `layout_document_common` 又分了一层：

```rust
pub fn layout_document(engine: &mut Engine, content: &Content, styles: StyleChain)
    -> SourceResult<PagedDocument>
{
    layout_document_impl(
        engine.world, engine.library, engine.introspector.into_raw(),
        engine.traced, TrackedMut::reborrow_mut(&mut engine.sink),
        engine.route.track(), content, styles,
    )
}

#[comemo::memoize]
fn layout_document_impl(world, library, introspector, traced, sink, route, content, styles)
    -> SourceResult<PagedDocument>
{
    layout_document_common(library, world, introspector, traced, sink, route,
                           content, Locator::root(), styles)
}
```

[src/pages/mod.rs:L33-L74](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/mod.rs#L33-L74)。注意这里 `_impl` 只做「记缓存」，把 locator 固定为 `Locator::root()` 后立即转给未记忆化的 `layout_document_common`。重建 `Engine` 的动作发生在 `layout_document_common` 里：

```rust
let mut engine = Engine {
    library, world, introspector, traced, sink,
    route: Route::extend(route).unnested(),   // ← 注意 unnested()
};
```

[src/pages/mod.rs:L141-L148](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/mod.rs#L141-L148)。这里多了个 `.unnested()`——文档级排版是顶层入口，不应当因外层调用栈而误判深度，所以把本段 `len` 置 0（详见 4.4）。

**实例三：`layout_par`（src/inline/mod.rs）**

段落排版是第三处，结构完全一致：

```rust
#[comemo::memoize]
fn layout_par_impl(elem, world, library, introspector, traced, sink, route, locator, styles, region, expand, situation)
    -> SourceResult<Fragment>
{
    let introspector = Protected::from_raw(introspector);
    let link = LocatorLink::new(locator);
    let mut locator = Locator::link(&link).split();
    let mut engine = Engine {
        library, world, introspector, traced, sink,
        route: Route::extend(route),
    };
    // realize(RealizationKind::Par) → layout_inline_impl
}
```

[src/inline/mod.rs:L70-L123](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/mod.rs#L70-L123)。公开入口 `layout_par` 在 [src/inline/mod.rs:L44-L67](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/mod.rs#L44-L67)。

三处对照后，模式已经一目了然：**公开函数只拆，`_impl` 只缓存，重建 `Engine` 后干真正的活**。

#### 4.2.4 代码实践（本讲主实践）

**目标**：亲手验证「拆解模式」与「memoize 命中」。

**步骤**：

1. 打开 [src/flow/mod.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs)。
2. **填写参数来源表**：对照 `layout_fragment`（公开）与 `layout_fragment_impl`，把 `_impl` 的每个 tracked 参数对应到 `Engine` 的哪个字段：

   | `_impl` 参数 | 表达式（来自公开函数） | 来源 `Engine` 字段 |
   | --- | --- | --- |
   | `world` | `engine.world` | `world` |
   | `library` | `engine.library` | `library` |
   | `introspector` | `engine.introspector.into_raw()` | `introspector`（`Protected` 拆包） |
   | `traced` | `engine.traced` | `traced` |
   | `sink` | `TrackedMut::reborrow_mut(&mut engine.sink)` | `sink` |
   | `route` | `engine.route.track()` | `route` |
   | `locator` | `locator.track()`（函数自己的参数） | ——（非 Engine 字段，外部传入） |

3. **观察 memoize 命中**（临时本地修改，**仅用于学习，事后请还原**）：在两处分别插一条日志：
   - 在公开函数 `layout_fragment` 里、调用 `_impl` **之前**加：`eprintln!("[pub] layout_fragment region={:?}", regions.size);`
   - 在 `layout_fragment_impl` 函数体**第一行**加：`eprintln!("[impl] layout_fragment region={:?}", regions.size);`
4. 在仓库根目录运行 typst 的集成测试（典型命令：`cargo test`；具体用例集待本地确认）。

**需要观察的现象**：

- `[pub]` 日志出现次数应**远多于** `[impl]`。因为公开函数每次调用都执行，而 `_impl` 只在缓存未命中（某个全新入参组合首次出现）时才执行——其余都被 comemo 直接返回了缓存结果。

**预期结果**：`[impl]` 的打印明显稀疏，直观证明 `#[comemo::memoize]` 在跨多次调用/跨内省多轮时复用了结果。**精确命中比例待本地验证**（取决于具体测试用例与文档复杂度）。

> 注意：本实践要求临时修改源码。本讲义禁止你保留该修改——做完请用 `git checkout -- src/flow/mod.rs` 还原。也不要提交带 `eprintln!` 的改动。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `library` 用 `&LazyHash<Library>` 直接传引用，而 `world` 要用 `Tracked<dyn World>`？
**答案**：`LazyHash<Library>` 本身已经缓存了哈希，且是不可变的共享数据（整个编译期基本不变），传引用即可参与缓存键。`World` 是个 trait 对象、体积大且背后可能是动态数据，用 `Tracked` 包装后只携带其身份摘要，才能被廉价地哈希与比较。

**练习 2**：如果把 `#[comemo::memoize]` 从 `layout_fragment_impl` 上去掉，会发生什么？
**答案**：功能不变，但每次调用都会完整重排。在内省收敛循环里，同一片段会被反复重排，性能急剧下降——这正是 comemo 存在的主要意义。

**练习 3**：`_impl` 函数体里为什么一定要重建一个局部 `Engine`，而不是直接用六个散落的 tracked 参数干活？
**答案**：下层所有函数（`realize`、`layout_flow` 等）都按惯例接收 `&mut Engine`。重建一个局部 `Engine` 既满足了这套调用约定，又能在此处统一做两件事——给 `route` 加长（`Route::extend`）表示进入新一层嵌套、把 `introspector` 用 `Protected::from_raw` 重新保护起来。

---

### 4.3 Route：调用链追踪与布局深度保护

#### 4.3.1 概念说明

`Route` 记录「编译器是怎么走到这里的」：它是若干段（segment）串起来的链表，每段可能带一个文件 id（用于检测循环 import），也可能只是表示「我又进了一层函数/排版/显示规则」。它的两大用途：

- **检测循环 import**：`Route::contains(id)` 判断某文件是否已在调用链上。
- **限制嵌套深度**：链长超过阈值就报「maximum layout depth exceeded」，防止无限递归把栈打爆。

#### 4.3.2 核心流程

`Route` 是一个不可变的单向链（每段记录 `len`，总长 = 各段 `len` 之和）。进入新一层排版时：

1. 公开函数用 `engine.route.track()` 把当前 `Route` 变成 `Tracked<Route>` 传进 `_impl`。
2. `_impl` 用 `Route::extend(route)` 造一段 `len = 1` 的新段挂在前面——表示「深了一层」。
3. 立刻调用 `engine.route.check_layout_depth()`，若超阈值则报错。
4. 退出函数时，局部 `Engine` 销毁，链自动回到上一层（无需显式 pop）。

#### 4.3.3 源码精读

`Route` 的结构：

```rust
pub struct Route<'a> {
    outer: Option<Tracked<'a, Self, ...>>,  // 父段（tracked，可缓存）
    id: Option<FileId>,                     // 若由模块求值进入，记录文件
    len: usize,                             // 本段长度（默认 1）
    upper: AtomicUsize,                     // 父链长度的已知上界（优化用）
}
```

[crates/typst-library/src/engine.rs:L258-L281](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L258-L281)。

`extend` 造新段、`track` 序列化、`unnested` 把本段长度置零：

```rust
pub fn extend(outer: Tracked<'a, Self>) -> Self {
    Route { outer: Some(outer), id: None, len: 1, upper: AtomicUsize::new(usize::MAX) }
}
pub fn unnested(self) -> Self { Self { len: 0, ..self } }
pub fn track(&self) -> Tracked<'_, Self> {
    match self.outer {
        Some(outer) if self.id.is_none() && self.len == 0 => outer,  // 无贡献则跳过本段
        _ => Track::track(self),
    }
}
```

[crates/typst-library/src/engine.rs:L295-L323](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L295-L323)。`track()` 的优化很巧妙：若本段既无 id、`len` 又是 0（比如调用了 `unnested()`），它对链长毫无贡献，就直接返回父段，避免制造无意义的缓存分支差异。

深度守门在 `check_layout_depth`：

```rust
const MAX_LAYOUT_DEPTH: usize = 72;
pub fn check_layout_depth(&self) -> HintedStrResult<()> {
    if !self.within(Route::MAX_LAYOUT_DEPTH) {
        bail!("maximum layout depth exceeded";
            hint: "try to reduce the amount of nesting in your layout");
    }
    Ok(())
}
```

[crates/typst-library/src/engine.rs:L345-L374](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L345-L374)。各类深度的阈值不同（show rule 64、layout/html 72、call 80），较低的阈值享有较高优先级，这样当 show rule 与 layout 嵌套交织时，总是优先报出更具体的那个错误（见 [engine.rs:L336-L352](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L336-L352) 的注释）。

它在 typst-layout 里的实际调用点：`engine.route.check_layout_depth().at(content.span())?;`，位于 [src/flow/mod.rs:L148](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L148)——每次进入片段排版都先自检。

`within` 用一个 `upper` 原子上界做剪枝，避免每次都把整条链走一遍（[crates/typst-library/src/engine.rs:L405-L428](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L405-L428)）。注释里点明：故意不精确计算父链长度，否则会阻碍「不同深度处的相同计算」复用缓存。

#### 4.3.4 代码实践

**目标**：理解 `Route::extend` 与 `check_layout_depth` 的配合，并解释 `unnested` 的用意。

**步骤**：

1. 阅读 [src/pages/mod.rs:L147](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/mod.rs#L147)：`layout_document_common` 里用 `Route::extend(route).unnested()`，而 [src/flow/mod.rs:L145](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L145) 与 [src/inline/mod.rs:L95](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/mod.rs#L95) 都只用 `Route::extend(route)`。
2. 思考：为什么文档级入口要把 `len` 置 0，片段级/段落级却不？

**需要观察的现象/预期结果**：`layout_document` 是整个文档排版的**最顶层**，它的嵌套计数应当从 0 重新开始，而不应被「恰好从一个深层调用栈里触发它」的外部环境连累（否则可能误报 depth exceeded）。`unnested()` 把本段 `len` 设为 0，配合 `track()` 的「无贡献则跳过」优化，使顶层文档排版的深度计数干净起步。而 `layout_fragment` / `layout_par` 通常是被上层排版嵌套调用的，必须如实 `len = 1` 累加，才能真正反映嵌套深度。

#### 4.3.5 小练习与答案

**练习 1**：`Route::track()` 为什么在 `id.is_none() && len == 0` 时直接返回父段？
**答案**：这样的段对链长和文件集合都没有贡献，保留它只会让「等价的调用链」产生不同的 `Tracked` 值，白白割裂缓存。跳过它能提升缓存命中率。

**练习 2**：若用户写了一个无限自我嵌套的布局（比如某个 show rule 输出又触发同规则），最终会怎样？
**答案**：调用链不断 `extend`，长度持续增长，达到 `MAX_LAYOUT_DEPTH`（layout 是 72）时 `check_layout_depth` 返回 `Err`，排版以 "maximum layout depth exceeded" 终止，而不是栈溢出崩溃。

---

### 4.4 Sink：可并行、可收敛的副作用收集

#### 4.4.1 概念说明

`Sink` 是一个「**只进不出**」的回收站：排版过程中产生的警告、延迟错误、内省记录、被追踪的值，都往里塞。它被设计成 `TrackedMut`（可变追踪），有两个关键意义：

- **支撑并行**：`Engine::parallelize` 给每个并行任务发一个**全新的独立 `Sink`**，各线程往自己的 sink 里写，互不干扰；最后把所有子 sink 合并回主 sink。
- **支撑收敛诊断**：内省不收敛时，记录下来的所有内省操作可用于定位「哪一处查询在两轮之间结果不稳定」。

#### 4.4.2 核心流程

`Sink` 内部是四个 `EcoVec`（内省、延迟错误、警告、追踪值）。它在 typst-layout 里的生命周期：

1. 公开函数借出 sink：`TrackedMut::reborrow_mut(&mut engine.sink)`，传给 `_impl`。
2. `_impl` 重建 `Engine` 时原样带上这个 `sink`，下层代码 `engine.sink.warn(...)` / `engine.delay(...)` 即往里写。
3. 遇到并行段落（如 `layout_pages` 里并行排各 page run）时，`parallelize` 给每个子任务新建空 `Sink`，结束后把子 sink 的内容合并回主 sink。
4. 延迟错误有一个特殊规则：在内省收敛期间允许出错（因为 introspector 还没准备好），只有到最后一轮仍存在的延迟错误才升级为致命错误。

#### 4.4.3 源码精读

`Sink` 的字段与「只进不出」语义：

```rust
#[derive(Default, Clone)]
pub struct Sink {
    introspections: EcoVec<Introspection>,
    delayed: EcoVec<SourceDiagnostic>,
    warnings: EcoVec<SourceDiagnostic>,
    warnings_set: FxHashSet<u128>,   // 用于警告去重
    values: EcoVec<(Value, Option<Styles>)>,
}
```

[crates/typst-library/src/engine.rs:L152-L167](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L152-L167)。`#[comemo::track] impl Sink { ... }`（[engine.rs:L204-L254](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L204-L254)）把这些方法暴露为可追踪方法：`introspection` / `delayed_error` / `warn` / `value` 全是 `(&mut self, ..) -> ()` 形式。源码注释点明：这类方法理论上不需要校验缓存（虽然 comemo 尚未实现该优化）。

`warn` 自带去重（按 span+message 的 128 位哈希），避免同一警告在内省多轮里重复刷屏（[engine.rs:L221-L228](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L221-L228)）。

并行的精髓在 `Engine::parallelize`：

```rust
let mut pairs: Vec<(U, Sink)> = Vec::with_capacity(work.len());
work.into_par_iter().map(|value| {
    let mut sink = Sink::new();                  // 每个任务独立 sink
    let mut engine = Engine {
        world, introspector, traced,
        sink: sink.track_mut(),                  // 各写各的
        route: route.clone(),
        library,
    };
    (f(&mut engine, value), sink)
}).collect_into_vec(&mut pairs);

// 合并回主 sink
for (_, sink) in &mut pairs {
    let sink = std::mem::take(sink);
    self.sink.extend(sink.introspections, sink.delayed, sink.warnings, sink.values);
}
```

[crates/typst-library/src/engine.rs:L53-L102](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L53-L102)。这正是 typst-layout 里 `layout_pages` 能并行排各 page run 的底层支撑（见 [src/pages/mod.rs:L185-L195](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/mod.rs#L185-L195) 的 `engine.parallelize(...)`）。

`Engine::introspect` 则把一次内省操作同时记进 sink，用于不收敛时的诊断（[crates/typst-library/src/engine.rs:L109-L117](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L109-L117)）。

#### 4.4.4 代码实践

**目标**：理解「每个并行任务独立 sink、事后合并」如何与 `layout_pages` 配合。

**步骤**：

1. 阅读 [src/pages/mod.rs:L175-L241](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/mod.rs#L175-L241) 的 `layout_pages`：它先用 `engine.parallelize(...)` 并行产出各 page run，再串行 `finalize`。
2. 对照 `Engine::parallelize`（[engine.rs:L53-L102](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L53-L102)）：确认每个 page run 排版时用的是**各自的 `Sink`**，而非共享主 sink。

**预期结果**：你会确认「并行排版期间各线程的警告/内省记录互不干扰，最后一次性合并」——这是 comemo + `Sink` 设计让并行安全的根本原因。如果共享同一个可变 sink，要么引入锁（拖慢），要么产生数据竞争。

> 待本地验证：若想直观看到合并，可在 `parallelize` 的合并循环里临时 `eprintln!` 各子 sink 的 `warnings` 数量（同样需事后还原）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `Sink` 的 tracked 方法都是 `(&mut self, ..) -> ()`（无返回值）？
**答案**：它是「只写」回收站，调用方不读取返回值。纯写入方法不改变基于输入的输出，理论上不影响记忆化正确性（注释提到 comemo 未来可跳过对它们的校验）。

**练习 2**：`engine.delay(result)` 做了什么？为什么不直接 `?` 返回错误？
**答案**：`delay`（[engine.rs:L42-L50](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L42-L50)）把错误塞进 `sink.delayed_errors` 并返回默认值，**不立即中断**排版。因为内省早期 introspector 未就绪，某些 show rule 可能暂时报错；只有到收敛最后一轮仍存在的延迟错误才升级为致命。直接 `?` 会让一次暂时性错误毁掉整个收敛过程。

---

## 5. 综合实践

把本讲四个最小模块串起来，完成下面的「调用链追踪」任务。

**任务**：选取一个会触发嵌套排版的简单场景，画出从 `layout_document` 到 `layout_fragment` 再到 `layout_par` 的「Engine 拆解—重建—Route 加深」全流程。

**建议步骤**：

1. 从 [src/pages/mod.rs:L33](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/mod.rs#L33) 的 `layout_document` 出发，标出它如何拆出六个 tracked 参数、`layout_document_impl` 如何记缓存、`layout_document_common` 如何用 `Route::extend(route).unnested()` 重建 `Engine`。
2. 跟到 `layout_pages` → 并行 `layout_page_run` → `layout_flow` → 最终 `layout_fragment`（[src/flow/mod.rs:L56](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L56)）：标出这里 `Route::extend(route)` **没有** `unnested`，且紧接着调用了 `check_layout_depth`。
3. 再跟到段落层 `layout_par`（[src/inline/mod.rs:L44](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/mod.rs#L44)）：确认它第三遍重复同一套拆解—重建。

**产出**：

- 一张表，列出在上述三层中，每一层的 `route` 长度如何变化（顶层 `unnested` 后从 0 起步 → fragment 层 +1 → par 层再 +1）。
- 一段话解释：如果某一层忘记 `Route::extend`，缓存与深度检测分别会出什么问题（缓存：不同深度会被误判为同一键，可能返回错误结果；深度：失去守门，无限嵌套不会被及时发现）。

> 待本地验证：若想确认 route 长度推断，可在各层 `_impl` 临时打印 `engine.route` 相关信息（需自行实现可观察手段），但务必事后还原源码。

## 6. 本讲小结

- `Engine` 是**编译上下文**而非排版参数：它装着 world / library / introspector / traced / sink / route 六样东西，几何画布由独立的 `Regions` 传入。
- typst-layout 的公开排版函数都遵循「拆 `Engine` → 调 `#[comemo::memoize]` 的 `_impl` → 在 `_impl` 里重建局部 `Engine`」的固定模板，目的是让每个字段都能进入记忆化缓存键。
- `Tracked` / `TrackedMut` 是 comemo 的「带哈希摘要的引用」，使巨型对象能被廉价比较与跨线程共享，从而支撑**可缓存、可并行、可收敛**。
- `Route` 用单向链记录调用链：`Route::extend` 每进入一层排版就把长度 +1，`check_layout_depth` 在超过 72 时报错；`unnested` + `track` 的优化让顶层排版的深度计数干净起步并提升缓存命中。
- `Sink` 是只写回收站，配合 `Engine::parallelize` 给每个并行任务发独立 sink、事后合并，让并行排版既安全又无需加锁；`delay` 机制让内省早期的暂时性错误不致毁掉整个收敛。

## 7. 下一步学习建议

掌握本讲的「拆解—重建」模式后，建议按以下顺序继续：

- **先补另外两个通用原语**：[u2-l2 Regions](u2-l2-regions.md)（画布抽象）与 [u2-l3 Frame 与 Fragment](u2-l3-frame-and-fragment.md)（排版产出物），它们与 `Engine` 一起构成所有 layouter 的「输入—输出」骨架。
- **再进入主链路细节**：[u3 单元](u3-l1-layout-document.md) 看文档/页面级布局如何用到本讲的 `parallelize` 与 introspector；[u4 单元](u4-l1-flow-overview.md) 看 flow 主循环如何反复调用 `layout_fragment`。
- **源码延伸阅读**：通读 `crates/typst-library/src/engine.rs` 全文（仅 ~450 行），把 `within` 的 `upper` 剪枝、各深度阈值的优先级设计彻底看懂；并在 `crates/typst-layout` 下用 `grep` 搜索 `check_layout_depth`、`Route::extend`、`parallelize` 的所有出现点，体会这套模式被复用了多少次。
