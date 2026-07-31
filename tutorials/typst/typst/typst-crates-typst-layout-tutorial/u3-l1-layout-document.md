# 文档布局 layout_document 的实现

## 1. 本讲目标

本讲从「入口函数」深入到「入口函数的实现内部」。读完本讲，你应当能够：

1. 看懂 `layout_document` 的**三层函数分流**：两个公开薄封装 → 两个带 `#[comemo::memoize]` 的 `_impl` → 一个共享的 `layout_document_common`。
2. 把 `layout_document_common` 这条「装配线」拆成 9 个有序步骤，并解释每一步做了什么。
3. 说清 `styles.to_map().outside()` **标记了什么**、为什么在交给 `realize` 之前必须标记。
4. 理解 `DocumentInfo` 的**两个填充时机**：排版前用外部样式链填一次，`realize` 过程中遇到 `set document` / `set text` 再填一次。
5. 区分 `layout_document` 与 `layout_document_for_bundle`，讲清 `Locator::root()` 与 `LocatorLink` 的差别，并解释 bundle 编译场景为何需要一个单独入口。

> 本讲只关注「文档级入口」，不展开 `layout_pages` 内部的 collect / 并行 / finalize 细节——那是 u3-l2～u3-l4 的任务。

## 2. 前置知识

本讲承接 u1-l4（端到端流程）与 u2-l1（comemo 记忆化模式）。开始前请确认你已理解：

- **端到端主链路**（u1-l4）：`layout_document → layout_document_common → realize → layout_pages → PagedDocument::new`。其中 `realize`（现实化）把任意 `Content` 展平成一维的 `Vec<Pair>`（每个 `Pair` 是「已知元素 + StyleChain」），`layout_pages` 再把这些 `Pair` 切成 page run 并行排版。
- **comemo 记忆化模式**（u2-l1）：公开函数先把 `&mut Engine` 拆成 `world / library / introspector / traced / sink / route` 等 `Tracked`/`TrackedMut` 参数，再调用带 `#[comemo::memoize]` 的 `_impl`。`Engine` 是「编译上下文」而非排版参数，几何画布由独立的 `Regions` 传入（文档级入口不接收 `Regions`，画布来自 `page` 样式）。
- **Locator / Location**（u2-l4）：`Location` 是元素 128 位的稳定身份，由 `Locator` 以分层哈希生成；`Locator` 故意不实现 `Clone`，强制在 `split`（多个子内容各取身份）与 `relayout`（复用身份/测量）之间显式选择。本讲会用到它的两种构造方式：`root` 与 `link`。

> 名词速查：`StyleChain` 是只读的样式链视图；`Styles` 是可写的样式表（`Vec` of `Style`）；`Pair`、`Arenas`、`RealizationKind` 来自 `typst-library::routines`，是 realize 与 layout 之间的协议类型。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲用到什么 |
| --- | --- | --- |
| `src/pages/mod.rs` | **本讲主角** | 三个公开/私有函数 + 共享 `layout_document_common` + `layout_pages` |
| `src/document.rs` | 产物层 | `PagedDocument::new` 如何打包 pages + info 并派生 introspector |

此外，本讲会**只读引用**若干兄弟 crate 的定义（它们定义类型，typst-layout 只消费）：

| 文件（兄弟 crate） | 提供的定义 |
| --- | --- |
| `crates/typst-library/src/foundations/styles.rs` | `Styles::outside` |
| `crates/typst-library/src/model/document.rs` | `DocumentInfo` 及 `populate` / `populate_locale` |
| `crates/typst-library/src/routines.rs` | `RealizationKind`、`Arenas` |
| `crates/typst-realize/src/lib.rs` | `realize` 实现，及其在 `visit_styled` 中对 `info` 的回填 |
| `crates/typst-library/src/introspection/locator.rs` | `Locator::root` / `Locator::link` / `LocatorLink::new` |

## 4. 核心概念与源码讲解

### 4.1 函数分流：两个公开入口、两个 memoize impl、一个共享 common

#### 4.1.1 概念说明

`layout_document` 并不是「一个函数干所有事」。`pages/mod.rs` 把它拆成了**五段**：

1. `layout_document`（公开薄封装）：负责把 `&mut Engine` 拆成可追踪参数。
2. `layout_document_impl`（`#[comemo::memoize]`）：缓存边界，决定 `Locator` 来源（这里是 `Locator::root()`）。
3. `layout_document_for_bundle`（公开薄封装）：bundle 编译用的另一个入口。
4. `layout_document_for_bundle_impl`（`#[comemo::memoize]`）：bundle 的缓存边界，`Locator` 来自外部的 `LocatorLink`。
5. `layout_document_common`（**共享、不 memoize**）：真正干活的装配线，被两个 `_impl` 调用。

为什么这样切？因为「构造 Engine、标记 outside、realize、layout_pages」这套流程对两种入口完全相同，**唯一不同的只有 Locator 的来源**。把差异收敛到「传给 common 的那个 `Locator` 参数」上，公共逻辑就不必重复。

#### 4.1.2 核心流程

```
                  layout_document(content, styles)
                              │  拆 Engine 为 Tracked 参数
                              ▼
                  layout_document_impl   ← #[comemo::memoize]
                              │  Locator::root()
                              ▼
                  layout_document_common(locator) ──┐
                              │                      │ 共享装配线
                              │                      │
 layout_document_for_bundle(content, locator, styles)│
                              │  拆 Engine 为 Tracked 参数
                              ▼
            layout_document_for_bundle_impl ← #[comemo::memoize]
                              │  LocatorLink::new(locator)
                              │  Locator::link(&link)
                              ▼
                  layout_document_common(locator) ──┘
```

两个 `_impl` 都只是「算出一个 `Locator`，再调 `layout_document_common`」。

#### 4.1.3 源码精读

先看普通入口的薄封装，它只做「拆 Engine」这一件事：

[layout_document：公开薄封装，把 Engine 拆成 tracked 参数](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/mod.rs#L33-L48)

注意它把 `engine.introspector.into_raw()` 传进去——`into_raw()` 把 `Protected` 内省器剥成裸 `Tracked`，以便跨 `#[comemo::memoize]` 边界传递（tracked 引用是可哈希的缓存键）。

接着是 memoize 层。普通入口用 `Locator::root()`：

[layout_document_impl：缓存边界，传入 Locator::root()](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/mod.rs#L51-L74)

关键在第 71 行：`Locator::root()`。它的定义在兄弟 crate：

[Locator::root：创建一个没有外层 link 的根 locator](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/locator.rs#L163-L170)

`outer: None` 表示这个 locator 不依赖任何外部上下文——普通编译时，文档就是整棵排版树的根，身份从 0 开始自然生成，无需回指外层。

bundle 入口的薄封装多收一个 `locator: Locator` 参数：

[layout_document_for_bundle：bundle 入口，多收一个 locator](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/mod.rs#L78-L95)

它的 memoize 层用 `LocatorLink` 把这个外部 `locator` 接进来：

[layout_document_for_bundle_impl：用 LocatorLink 接外部 locator](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/mod.rs#L98-L123)

两行是重点：
- 第 111 行 `LocatorLink::new(locator)`：用外部 tracked locator 建一个 link。
- 第 120 行 `Locator::link(&link)`：造一个「指向该 link」的 locator。

对照定义即可看到差别——`link` 构造时 `outer: Some(link)`，而 `root` 是 `outer: None`：

[Locator::link：创建一个指向外部 link 的 locator](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/locator.rs#L181-L184)

[LocatorLink::new：把外部 tracked locator 包成一个可跨 memoize 边界的 link](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/locator.rs#L364-L371)

最后，两边都汇聚到 `layout_document_common`，它的注释写得很直白：**「The shared, unmemoized implementation」**——共享、且**不**做 memoize（因为 memoize 已在各自的 `_impl` 完成）：

[layout_document_common：两个入口共享的装配线（不 memoize）](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/mod.rs#L125-L138)

#### 4.1.4 代码实践

**目标**：用一张表把五个函数的「身份」钉死，避免后续阅读时混淆。

**操作步骤**（源码阅读型）：
1. 打开 `src/pages/mod.rs`，定位第 33、51、78、98、128 这五个函数。
2. 用下表逐行核对，特别留意「是否 `pub`」「是否带 `#[comemo::memoize]`」「Locator 来源」三列：

| 函数 | pub? | memoize? | Locator 来源 | 谁调用它 |
| --- | --- | --- | --- | --- |
| `layout_document` | ✅ | ❌ | —— | 外部（`Output::create` 等） |
| `layout_document_impl` | ❌ | ✅ | `Locator::root()` | `layout_document` |
| `layout_document_for_bundle` | ✅ | ❌ | —— | bundle 编译流程 |
| `layout_document_for_bundle_impl` | ❌ | ✅ | `LocatorLink` | `layout_document_for_bundle` |
| `layout_document_common` | ❌ | ❌ | 由调用方传入 | 两个 `_impl` |

**需要观察的现象**：memoize 标注**只**出现在两个 `_impl` 上，`common` 上没有——这印证了「缓存边界在 `_impl`，公共逻辑在 `common`」的切分。

**预期结果**：你应能解释为什么 `common` 不能再加一层 memoize（会与外层 `_impl` 的缓存键重复、且 `Locator` 在此处已是具体值而非 tracked）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `layout_document_impl` 上的 `#[comemo::memoize]` 去掉，会有什么直接后果？
**答案**：每次调用 `layout_document` 都会重新执行 `realize` + `layout_pages` 全套排版，失去增量缓存；Typst 的「改一处只重排受影响部分」能力会退化成「每次全量重排」。

**练习 2**：`layout_document_common` 为什么「不 memoize」？
**答案**：缓存命中/失效的判定已在调用它的 `_impl` 完成；`common` 收到的 `Locator` 已是具体值（非 tracked），再 memoize 既无额外收益，也可能因参数形态不同而无法正确命中。

---

### 4.2 layout_document_common：文档排版的统一装配线

#### 4.2.1 概念说明

`layout_document_common` 是真正「把 content 变成 pages」的地方。它本身很短（约 45 行），但串起了排版的全部前置准备：重建一个局部 `Engine`、把外部样式标记为 outside、建临时 `Arenas`、用默认 `DocumentInfo` 先填一轮、调用 `realize` 拿到扁平 children、交给 `layout_pages`、最后打包成 `PagedDocument`。

理解这条装配线的意义在于：**文档级排版的「上下文」是在这里被构造出来的**，后续 `realize` 与 `layout_pages` 拿到的 engine、styles、locator、info 全部来自这里。

#### 4.2.2 核心流程

`layout_document_common` 的 9 个有序步骤：

```
输入: library, world, introspector, traced, sink, route, content, locator, styles
 1. introspector = Protected::from_raw(introspector)   // 重新包成 Protected
 2. locator = locator.split()                          // 得到可分配身份的 SplitLocator
 3. engine = Engine { library, world, introspector, traced, sink,
                     route: Route::extend(route).unnested() }  // 重建局部 Engine
 4. styles = styles.to_map().outside()                 // 标记外部样式为 outside
    styles = StyleChain::new(&styles)                  // 再包回只读链
 5. arenas = Arenas::default()                         // 临时存储（realize 期间存活）
 6. info = DocumentInfo::default()
    info.populate(styles); info.populate_locale(styles)// 第一轮填充（外部样式链）
 7. children = routines.realize(                       // 现实化：content → Vec<Pair>
        RealizationKind::Document { info: &mut info }, engine, locator, arenas,
        content, styles)
 8. pages = layout_pages(engine, children, locator, styles)  // 排版出 EcoVec<Page>
 9. PagedDocument::new(pages, info)                    // 打包，并派生 introspector
```

步骤 6 与 7 的协同是本讲的一个重点（详见 4.3）：`info` 先被外部样式填一次，又以 `&mut info` 交给 realize 让它在遇到 `set document` 时继续填。

#### 4.2.3 源码精读

整段装配线的源码（带行内注释对应上面 9 步）：

[layout_document_common 全函数：文档排版的装配线](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/mod.rs#L128-L172)

几个关键点逐一看：

**重建局部 Engine（第 141–148 行）**。注意 `route: Route::extend(route).unnested()`：

- `Route::extend(route)`：把传入的 tracked route 延长一层，使排版深度计数能**跨越 memoize 边界**继续累加（u2-l1 讲过，超阈值 72 时 `check_layout_depth` 会报错）。
- `.unnested()`：表示这一层是「顶层排版」而非嵌套子排版，让计数干净起步、提升缓存命中。

**调用 realize（第 160–167 行）**。`engine.library.routines.realize` 是一个**函数指针字段**（不是直接调用某个函数）——这是 typst 解耦「接口与实现」的手段：`routines` 里的函数指针在 `typst` 顶层 crate 装配时被指向 `typst_realize::realize` 等具体实现。`RealizationKind::Document { info: &mut info }` 告诉 realize「这是文档级现实化，请把 `set document` 规则回填到这个 info」。`Arenas`（步骤 5）是 realize 用来延长临时 content/styles 生命周期的竞技场，**必须存活到 children 处理完毕**：

[Arenas：临时存储，注释明确「必须存活到 realize 返回的内容处理完毕」](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/routines.rs#L182-L193)

**交给 layout_pages（第 169 行）并打包（第 171 行）**。`PagedDocument::new` 内部会派生 introspector：

[PagedDocument::new：打包 pages + info，并在内部构建 introspector](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/document.rs#L23-L30)

注意第 28 行 `PagedIntrospector::new(&pages)`：introspector 是 `&pages` 的**纯派生物**。正因如此，`PagedDocument` 的 `Hash` impl 故意只哈希 `pages` 与 `info`、不哈希 introspector（u1-l4 讲过，这是 comemo 缓存正确的前提）：

[PagedDocument::Hash：只哈希 pages 与 info，introspector 不参与](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/document.rs#L48-L55)

#### 4.2.4 代码实践

**目标**：验证「realize 的函数指针」确实指向 `typst_realize::realize`，并理解 routines 是接口层。

**操作步骤**（源码阅读型）：
1. 在 `crates/typst-library/src/routines.rs` 找到 `realize` 在 `Routines` 里的签名（它是一个 `fn` 字段）。
2. 在 `crates/typst/src/lib.rs` 找到 `static ROUTINES` 的初始化，确认 `realize: typst_realize::realize`。
3. 打开 `crates/typst-realize/src/lib.rs`，对照 `realize` 的实现体：当 `kind` 为 `Document { .. }` 时选用 `FLOW_RULES`、并把 `outside` 标记设为 `true`。

[realize 实现体：Document 选用 FLOW_RULES，outside 标记为 true](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-realize/src/lib.rs#L43-L74)

**需要观察的现象**：第 57 行 `RealizationKind::Document { .. } => FLOW_RULES`，以及第 64 行 `outside: matches!(kind, RealizationKind::Document { .. })`——后者正是 realize 内部那个 `outside` 布尔位，它和 4.3 要讲的 `styles.to_map().outside()` 是**同一件事的两面**。

**预期结果**：你能说清「`layout_document_common` 在样式上标 outside」与「`realize` 内部 State 也有一个 outside 位」如何配合。

> 如想进一步实验：可在 `layout_document_common` 第 169 行前临时加一条 `eprintln!("realize produced {} pairs", children.len());`，再用 `cargo test -p typst-layout`（或整体 `cargo test`）运行，观察不同输入下 children 数量。是否能在本机直接跑通取决于字体/world 配置，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：装配线里 `Route::extend(route).unnested()` 的 `.unnested()` 去掉会怎样？
**答案**：排版深度计数会从「带嵌套标记」开始累加，可能让原本应判定为顶层的排版被误判为嵌套，影响缓存命中，极端情况下可能提早触发布局深度上限报错（u2-l1）。

**练习 2**：为什么 `Arenas` 必须在 `realize` 调用**之前**创建，且存活到 `layout_pages` 之后？
**答案**：`realize` 会把临时构造的 content/styles 存入 arena 并返回引用它们的 `Pair`；若 arena 提前释放，`children` 里的引用就悬空了。注释已明确「Must be kept live while the content returned from realization is processed」。

---

### 4.3 outside 标记与 DocumentInfo 的两个填充时机

#### 4.3.1 概念说明

本模块解决学习目标 1 与 2。先说直觉：

- **`outside` 标记**：在 Typst 里，「在 show 规则之外直接写的 `set`」与「show 规则内部产生的 `set`」需要被区分。`layout_document` 收到的外部样式链是前者——它对整篇文档都生效，应当能被「提升」到页面级。`styles.to_map().outside()` 就是给这些样式盖一个「我是外部来的」章。
- **`DocumentInfo` 何时填**：`DocumentInfo`（标题、作者、描述、关键词、日期、locale）不是在某一个时刻一次性填好的，而是**分两次**：排版前用外部样式链填一次（拿 `set document` 顶层字段），`realize` 走过内容里的 `set document` / `set text` 时再填一次。

#### 4.3.2 核心流程

`outside` 标记的流转：

```
外部样式链 styles (StyleChain)
   │  styles.to_map()        // 展开成可写的 Styles (Vec<Style>)
   │  .outside()             // 遍历每个 Property/Recipe，置 outside = true
   │  StyleChain::new(&..)   // 重新包回只读链
   ▼
交给 realize 的 styles（每个条目都已标记 outside）
```

`DocumentInfo` 的两次填充：

```
布局层（pages/mod.rs 第 156–158 行）：
   info.populate(styles)         // 外部样式链里的 set document 字段
   info.populate_locale(styles) // 外部样式链里的 set text(lang/region)

realize 层（typst-realize visit_styled）：
   遍历内容，遇到 set document → info.populate(local)   // 第 610 行
                 遇到 set text     → info.populate_locale(...) // 第 628 行
```

两次都调用**同名方法**，因为 `populate` / `populate_locale` 内部用 `if styles.has(...)` 守卫，多次调用是安全的；后调用者（更内层的 set 规则）覆盖先调用者，符合 set 规则「内层覆盖外层」的语义。

#### 4.3.3 源码精读

**先看 outside 标记**。`layout_document_common` 第 150–153 行：

[layout_document_common：标记外部样式为 outside 并重新包成 StyleChain](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/mod.rs#L150-L153)

`outside()` 的定义很直白——遍历每个样式条目，把 `Property` 和 `Recipe` 的 `outside` 字段置为 `true`（`Revocation` 没有 outside 概念）：

[Styles::outside：把所有 Property/Recipe 标记为「在 show 规则之外应用」](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L92-L102)

为什么要标记？因为 `realize` 在遇到页面样式时，会用 `s.outside = true` 来表示「从 show 规则笼子里挣脱出来、可以提升到页面级」（见 4.2.3 引用的 realize 源码附近）。布局层在**进入 realize 之前**就先给外部样式盖好章，两边的 `outside` 语义就对齐了：凡标记为 outside 的样式，都是合法的页面级样式。

**再看 DocumentInfo 的结构与方法**。`DocumentInfo` 有 6 个字段：

[DocumentInfo：文档元信息（title/author/description/keywords/date/locale）](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/document.rs#L328-L347)

`populate` 逐字段用 `if styles.has(...)` 判断后赋值：

[DocumentInfo::populate：从 styles 读取 set document 字段](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/document.rs#L349-L375)

`populate_locale` 处理 locale，且有一条短路：如果 locale 已被显式设定（`is_custom()`）就不再覆盖：

[DocumentInfo::populate_locale：从 set text(lang/region) 推断 locale](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/document.rs#L377-L391)

**关键：填充的两个时机**。布局层先填（第 156–158 行）：

[layout_document_common：排版前的第一轮 populate](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/mod.rs#L156-L158)

而 realize 在 `visit_styled` 中遇到 `set document` / `set text` 时**再填一次**。这是「填充时机」的另一半，必须读 realize 才能看全：

[visit_styled：遇到 set document 调 info.populate，遇到 set text 调 info.populate_locale](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-realize/src/lib.rs#L603-L629)

注意第 607–616 行的守卫：只有当 `kind` 是 `Document { info }` 时才回填 info；否则（且不是 Bundle）会直接报错「document set rules are not allowed inside of containers」。这印证了 `set document` 只允许出现在文档顶层。

> 为什么布局层要先填一次？因为传给 `layout_document` 的**外部 ambient 样式链**（比如求值层在最外层应用的 `#set document(...)`）不会被 realize 的 `visit_styled` 当作「Styled 节点」访问到——它是环境式的 `outer` 链。所以必须在排版前用 `info.populate(styles)` 显式捕获它；而内容内部显式的 `#set document` 规则才由 realize 在遍历时捕获。两者合起来覆盖了「外部 + 内部」两条来源。

#### 4.3.4 代码实践

**目标**：用一段最小 Typst 源码追踪 `DocumentInfo` 各字段的填充来源。

**操作步骤**（源码阅读 + 行为推理型）：
1. 设想（或在本机编译）这样一段文档：

   ```typ
   #set document(title: "外部标题", author: ("Alice",))
   #set text(lang: "zh", region: "cn")
   #set document(date: datetime(year: 2026))

   = 标题
   正文。#set document(title: "内部标题") 内部 set 规则。
   ```

2. 推理每字段最终取值与来源：
   - `title`：先被外部样式填成 `"外部标题"`，随后 realize 遇到内容里的 `#set document(title: "内部标题")`，在第 610 行 `info.populate(local)` 中**覆盖**为 `"内部标题"`。
   - `author`：外部样式填 `["Alice"]`，内容里没有覆盖，保持不变。
   - `date`：外部样式填 `2026`。
   - `locale`：外部 `set text` 在排版前 `populate_locale` 填成 `zh-cn`；若内容里后续没有新的 `set text`，则因 `is_custom()` 短路不再覆盖。

**需要观察的现象**：`title` 的最终值是「内部标题」而非「外部标题」，说明**后调用的 populate 覆盖先调用的**。

**预期结果**：你能用一句话概括——「`DocumentInfo` 的外部样式字段在 `layout_document_common` 排版前填一次，内容内的 `set document` / `set text` 在 realize 遍历时再填一次，后者覆盖前者」。

> 这段行为推理若要在本机实证，需借助 typst CLI 编译并 dump 元信息，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`styles.to_map().outside()` 为什么不能省略？
**答案**：省略后，外部样式不会被标记为「页面级合法」，realize 在处理页面样式提升时无法识别它们是外部来源，可能导致页面级配置（如 `set page`）无法正确从 show 规则笼子里挣脱，或引发样式归属判断错误。

**练习 2**：为什么 `populate` 用 `if styles.has(...)` 而不是直接赋值？
**答案**：`has(...)` 只在该字段确实被 set 规则设置过时才写入，避免用「字段未设置时的默认值」覆盖掉前一次（更外层）已经填好的有效值；这也让多次调用 populate 安全叠加。

**练习 3**：`populate_locale` 开头的 `if self.locale.is_custom() { return; }` 有什么用？
**答案**：一旦 locale 已被显式设定（custom），就不再被后续的 `set text(lang/region)` 覆盖，保证第一个有效的 locale 设定「固化」，符合「locale 取自第一个顶层 set 规则」的文档约定。

---

### 4.4 Locator 的两种来源：root 与 LocatorLink，以及 bundle 入口

#### 4.4.1 概念说明

本模块直接对应学习目标 3 和本讲的核心实践任务。两种入口的唯一差别是** Locator 怎么来**：

- **普通编译**（`layout_document`）：文档是排版树的根，用 `Locator::root()`——`outer: None`，身份从 0 开始自然递推，不依赖任何外部上下文。
- **bundle 编译**（`layout_document_for_bundle`）：文档只是某个**更大编译单元**的一个子产物（bundle 会把多个文档、资源一起编译），它的身份需要**回指外层**已有的 locator，于是用 `LocatorLink` 把外部 `locator` 接进来——`outer: Some(link)`。

为什么要区分？因为 Typst 的 introspection（query/counter/label/outline）要求每个排版实例有**稳定且唯一**的 `Location`。普通编译时根身份无歧义；bundle 编译时，多个文档共用一个外层定位上下文，必须用 link 把身份挂到外层树上，才能保证跨文档不冲突、且能被外层统一索引。

#### 4.4.2 核心流程

两种 Locator 的构造路径：

```
普通入口:
  layout_document_impl
     └─ Locator::root()                  // { local: 0, outer: None }
           │
           ▼ 传给 layout_document_common

bundle 入口:
  layout_document_for_bundle(engine, content, locator /*外部*/, styles)
     └─ _impl 收 locator.track()         // 转 tracked 跨 memoize
           ├─ LocatorLink::new(locator)  // 包成 link
           └─ Locator::link(&link)       // { local: 0, outer: Some(link) }
                 │
                 ▼ 传给 layout_document_common
```

两者随后都进入 `layout_document_common` 的第 140 行 `locator.split()`，从这里开始身份分配逻辑完全一致——差异只在「外层是否挂着一个 link」。

#### 4.4.3 源码精读

普通入口传 root（已在 4.1.3 引用，重看关键行）：

[layout_document_impl 第 71 行：Locator::root()](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/mod.rs#L63-L73)

bundle 入口用 link 桥接外部 locator：

[layout_document_for_bundle_impl：第 111 行建 link，第 120 行用 link 造 locator](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/mod.rs#L111-L122)

`LocatorLink::new` 把外部 tracked locator 包成一个可在 memoize 缓存键里出现的 link（`OnceLock` 用于惰性解析外层位置）：

[LocatorLink::new：包外部 tracked locator，带惰性解析缓存](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/locator.rs#L363-L371)

`Locator::link` 与 `Locator::root` 的结构对比（u2-l4 已讲过 Locator 的分层哈希，这里只看构造差异）：

- `root()`：`Self { local: 0, outer: None }` —— 无外层。
- `link(link)`：`Self { local: 0, outer: Some(link) }` —— 外层指向传入 link。

> 注意 `layout_document_for_bundle` 多收的那个 `locator: Locator`（第 81 行形参）来自 bundle 编译流程的外层——它已经携带了「这个文档在整个 bundle 中的位置」信息。`LocatorLink` 的作用就是把这份信息**跨过 `#[comemo::memoize]` 边界**带进 `_impl`，再交给 `common`。

#### 4.4.4 代码实践（本讲核心实践任务）

**目标**：对比 `layout_document_impl` 与 `layout_document_for_bundle_impl`，写出二者在 locator 处理上的差异，并说明 bundle 编译场景为何需要单独入口。

**操作步骤**（源码阅读型）：
1. 并排打开 `src/pages/mod.rs` 第 51–74 行（`layout_document_impl`）与第 98–123 行（`layout_document_for_bundle_impl`）。
2. 逐项填表：

| 维度 | `layout_document_impl` | `layout_document_for_bundle_impl` |
| --- | --- | --- |
| 额外形参 | 无 | `locator: Tracked<Locator>` |
| Locator 来源 | `Locator::root()`（第 71 行） | `LocatorLink::new(locator)` + `Locator::link(&link)`（第 111、120 行） |
| `outer` 字段 | `None` | `Some(link)` |
| 含义 | 文档是排版树根，身份自起 | 文档挂在外层 bundle 的定位树下，身份回指外层 |
| 适用场景 | 普通单文档编译 | bundle：多文档/资源一起编译 |

3. 回答「为何 bundle 需要单独入口」：bundle 把多个文档与资源并入一次编译，每个文档只是外层定位树的一个分支；若仍用 `Locator::root()`，各文档会各自从 0 起身份、彼此无法区分，跨文档 query/label 会冲突。`LocatorLink` 让每个文档的身份挂到统一的外层树上，保证全局唯一且可被外层索引。

**需要观察的现象**：两个 `_impl` 除 locator 外的其余参数（world/library/introspector/traced/sink/route/content/styles）与对 `layout_document_common` 的调用**完全相同**，差异被干净地隔离到 locator 一项。

**预期结果**：你能用一句话总结——「普通入口用 `Locator::root()` 自起身份，bundle 入口用 `LocatorLink` 把身份挂到外层树上；其余流程共享 `layout_document_common`」。

#### 4.4.5 小练习与答案

**练习 1**：`Locator::root()` 与 `Locator::link(link)` 在结构上的唯一差别是什么？
**答案**：`outer` 字段——`root` 是 `None`，`link` 是 `Some(link)`。`local` 都是 0（表示当前层从 0 号开始分配身份）。

**练习 2**：如果 bundle 场景误用 `layout_document`（而非 `layout_document_for_bundle`），最可能出什么问题？
**答案**：该文档会以 `Locator::root()` 起身份，与 bundle 中其他文档的身份空间重叠，导致跨文档的 label / query / counter 解析错乱或冲突。

**练习 3**：为什么 `LocatorLink::new` 收的是 `Tracked<Locator>` 而不是普通 `&Locator`？
**答案**：因为它要跨过 `#[comemo::memoize]` 的缓存边界；tracked 引用携带身份哈希、是 `Copy` 且 `Send+Sync`，能安全地作为缓存键的一部分，而普通借用做不到。

---

## 5. 综合实践

把本讲的知识串起来：**追踪一份带元信息的文档如何被 `layout_document_common` 装配成 `PagedDocument`**。

设文档源码为：

```typ
#set document(title: "Demo", author: ("Bob",))
#set page(paper: "a4")
#set text(lang: "en")

= Intro
Hello world.
#pagebreak()
Second page.
```

请按 `layout_document_common` 的 9 步装配线，回答下列问题（可对照 4.2.2 的步骤编号）：

1. **步骤 3（Engine）**：这里重建的 `Engine` 的 `route` 为什么是 `Route::extend(route).unnested()`？`.unnested()` 带来什么效果？
2. **步骤 4（outside）**：`#set page(paper: "a4")` 这条样式，经过 `styles.to_map().outside()` 后多了什么标记？为什么必须标记后才能交给 realize？
3. **步骤 6+7（info 双重填充）**：`title` 和 `author` 分别在**哪两个时机**被写入 `info`？为什么需要两次？locale 又是从哪里推断的？
4. **步骤 7（realize）**：`RealizationKind::Document { info: &mut info }` 里的 `&mut info` 让 realize 拥有了什么能力？如果文档里把 `#set document(...)` 写在一个 `#block[...]` 内部，realize 会如何反应（参考 4.3.3 引用的 visit_styled 守卫）？
5. **步骤 8→9（产物）**：`layout_pages` 产出的 `pages` 与 `info` 一起进入 `PagedDocument::new`。introspector 是何时、如何被构建的？为什么 `PagedDocument::Hash` 不哈希 introspector？

**参考要点**：
1. `extend` 让排版深度跨 memoize 边界累加；`unnested` 让顶层计数干净起步、提升缓存命中。
2. 多了 `outside = true` 标记；它是页面级样式「合法提升」的凭证，与 realize 内部 `s.outside` 位语义对齐。
3. `title/author` 先在步骤 6 由外部样式链的 `populate` 写入，再在步骤 7 realize 遍历到 `#set document` 时由 `visit_styled` 的 `info.populate(local)` 覆盖/补充；两次是因为外部 ambient 样式链不会被 `visit_styled` 当 Styled 节点访问。locale 由 `populate_locale` 从 `set text(lang)` 推断。
4. `&mut info` 让 realize 能在遇到 `set document` 时回填元信息；写在容器内部的 `set document` 会触发「document set rules are not allowed inside of containers」报错。
5. introspector 在 `PagedDocument::new` 内由 `PagedIntrospector::new(&pages)` 派生；不哈希它是因为它是 pages 的纯派生物，哈希会冗余且可能破坏缓存正确性。

## 6. 本讲小结

- `layout_document` 是**五段分流**：两个公开薄封装 → 两个 `#[comemo::memoize]` 的 `_impl` → 一个共享且不 memoize 的 `layout_document_common`；缓存边界在 `_impl`，公共逻辑在 `common`。
- `layout_document_common` 是一条 9 步装配线：重建局部 `Engine`（`Route::extend(route).unnested()`）→ 标记 outside 样式 → 建 `Arenas` → 先填一轮 `DocumentInfo` → 调 `realize` → 调 `layout_pages` → `PagedDocument::new`。
- `styles.to_map().outside()` 给外部样式盖「页面级合法」章，与 realize 内部的 `outside` 位语义对齐；这是 `set page` 等能从 show 规则笼子提升到页面级的前提。
- `DocumentInfo` 有**两个填充时机**：排版前用外部 ambient 样式链填一次（`populate` / `populate_locale`），realize 遇到内容里的 `set document` / `set text` 时再填一次；`populate` 用 `if styles.has(...)` 守卫，多次调用安全且内层覆盖外层。
- `realize` 以 `RealizationKind::Document { info: &mut info }` 被调用——`&mut info` 让 realize 能回填元信息；非 Document 场景下的 `set document` 会被拒并报错。
- 普通入口用 `Locator::root()`（`outer: None`），bundle 入口用 `LocatorLink` + `Locator::link`（`outer: Some`）把身份挂到外层树；二者其余流程完全共享 `layout_document_common`，bundle 单独入口只为保证跨文档身份唯一。

## 7. 下一步学习建议

本讲把「文档级入口的实现」讲透了，但**刻意没有展开 `layout_pages` 的内部**。建议接着学：

- **u3-l2（页面收集 page collect）**：`pages/collect.rs` 如何把 realize 产出的扁平 children 切成 `Item::Run` / `Tags` / `Parity`，处理 weak/strong pagebreak 与未终止 tag 迁移。
- **u3-l3（页面运行 page run）**：`pages/run.rs` 的 `layout_page_run` 如何解析页面尺寸/边距/页眉页脚并排版正文（`LayoutedPage` 为何暂存两套 margin）。
- **u3-l4（页面最终化 finalize）**：`pages/finalize.rs` 如何在已知物理页号后做左右页边距互换、计数器步进与标签挂载。
- **u3-l5（PagedIntrospector）**：`introspect.rs` 如何从 `&[Page]` 派生查询索引，与本讲 `PagedDocument::new` 里派生 introspector 的那一行形成闭环。

> 建议阅读顺序：u3-l2 → u3-l3 → u3-l4 → u3-l5，恰好对应 `layout_pages` 内部「collect → 并行 run → finalize → 派生 introspector」的真实执行顺序。
