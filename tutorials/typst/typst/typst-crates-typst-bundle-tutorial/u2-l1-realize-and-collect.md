# 从源码到 Bundle：realize 与 collect 的实现化与校验

> 所属单元：u2 bundle 编译主流程 · 学习阶段：intermediate
> 依赖讲义：u1-l2（目录结构与编译入口）

## 1. 本讲目标

上一篇我们已经看清了「`typst::compile::<Bundle>(world)` → `Output::create` → `bundle` → `bundle_impl`」这条调用链是怎么接起来的。本讲我们**走进 `bundle_impl` 的函数体**，拆解它把一段 Typst 源码内容（`Content`）变成一个 `Bundle` 的三个关键步骤：

1. **实现化（realize）**：用 `RealizationKind::Bundle` 对根内容做一次「展平」，得到一串「已知类型 + 样式」的顶层元素。
2. **收集与校验（collect）**：遍历这些顶层元素，只放行 `Tag` / `Asset` / `Document` 三类，其余报错。
3. **路径唯一性检测**：对每个 `Asset` / `Document` 的输出路径查重，重复时用 `delayed_error` 延迟上报。

学完后你应当能够：

- 画出 `bundle_impl` 从入口到返回 `Bundle` 的完整步骤；
- 说清楚 `RealizationKind::Bundle` 与 `BUNDLE_RULES`（一个**空数组**）为什么决定了「顶层只能放 document/asset」；
- 在源码里指出「不允许的顶层元素」和「路径重复」这两类错误分别走哪条上报通道，以及**为什么后者要用 `delayed_error` 延迟上报**。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**实现化（realize）是什么。** Typst 的内容树里混着 show 规则、样式、序列、段落、列表……排版引擎并不直接处理这棵杂乱的树。`realize` 子系统负责递归地应用样式与 show 规则，把内容**展平**成一个「已知类型元素 + 其生效样式」的列表。这个列表里的每一项是一个 `Pair = (&Content, StyleChain)`（见 [routines.rs:196](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/routines.rs#L196)）。

**实现化有「种类」。** 同一段内容，放在不同位置要做的事不同：放在文档根、放在段落里、放在数学里，分组与过滤规则都不一样。所以 `realize` 的第一个参数是 `RealizationKind`（见 [routines.rs:154-169](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/routines.rs#L154-L169)）。bundle 用的是 `RealizationKind::Bundle`，它是所有种类里**最宽松**的一种——几乎不做任何分组。

**两类错误、两条通道。** Typst 编译在内省循环（introspection loop）里会反复运行 `bundle_impl`。有些错误是「结构性」的（永远不可能变好），有些是「可能随迭代消失」的（比如依赖了尚未收敛的查询结果）。前者立刻返回 `Err` 终止；后者写进 `Sink` 的 `delayed` 列表**延迟上报**，只有坚持到最后一轮还没消失，才升级为致命错误（见 [engine.rs:152-167](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/engine.rs#L152-L167)）。理解这两条通道，是本讲最关键的一处设计。

> 名词小词典
>
> | 术语 | 含义 |
> | --- | --- |
> | `Pair<'a>` | `(&'a Content, StyleChain<'a>)`，realize 的输出单元 |
> | `RealizationKind` | 告诉 realize 「我们在做什么层级的实现化」 |
> | `BUNDLE_RULES` | bundle 实现化使用的分组规则表，**是个空数组 `&[]`** |
> | `Sink` | 只写式的「错误/警告/内省记录」回收槽 |
> | `delayed_error` | 把错误塞进 `Sink.delayed`，延迟到内省循环末尾才决定是否致命 |

## 3. 本讲源码地图

本讲涉及的关键文件（均已用当前 HEAD `9a1d84e` 固定）：

| 文件 | 在本讲的作用 |
| --- | --- |
| [crates/typst-bundle/src/lib.rs](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs) | `bundle_impl`（主流程）、`collect`（校验与查重）、`Child`/`Item` 枚举 |
| [crates/typst-realize/src/lib.rs](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-realize/src/lib.rs) | `realize` 入口、`RealizationKind::Bundle` 如何选 `BUNDLE_RULES`、`visit_styled` 对 bundle 的特殊放行 |
| [crates/typst-library/src/routines.rs](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/routines.rs) | `RealizationKind` 枚举定义、`realize` 例程签名、`Pair` 类型别名 |
| [crates/typst-library/src/engine.rs](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/engine.rs) | `parallelize`（并行编译各文档）、`Sink` 与 `delayed_error` |

---

## 4. 核心概念与源码讲解

### 4.1 bundle_impl 的整体流程：实现化 → collect → parallelize

#### 4.1.1 概念说明

`bundle_impl` 是整个 bundle 编译的「总调度」。它接收一段已经 eval 完的根内容 `content`，要把它变成一个 [`Bundle`](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L44-L54)（一组文件 + 一个跨文档内省器）。

它的工作可以拆成五个阶段。本模块聚焦前三个阶段（实现化、收集、并行编译），后两个（建内省器、装文件）会在 u5-l1 / u5-l2 详讲，这里只点出它们的存在。

#### 4.1.2 核心流程

用伪代码描述 `bundle_impl` 的骨架：

```text
fn bundle_impl(content, styles, ...) -> Bundle:
    1. 重建一个局部 Engine（带 Protected 内省器、root Locator）
    2. 把外部样式标记为 outside，使其在页级生效
    3. children = realize(RealizationKind::Bundle, content, styles)   # 展平 → Vec<Pair>
    4. children = collect(children)                                    # 校验 + 查重 → Vec<Child>
    5. items = engine.parallelize(children, compile_each)              # 并行编译各文档 → Vec<Item>
    6. introspector = BundleIntrospector::new(&items)                  # 统一内省器
       anchors = create_link_anchors(&items, introspector.link_targets())
    7. files = IndexMap::from(items 中除 Tag 外的 Document/Asset)
    return Bundle { files, introspector }
```

注意第 3、4 步是**串行**的：先展平，再校验。而第 5 步用 `engine.parallelize` **并行**编译每个文档——因为各文档彼此独立，可以分到不同线程。并行与记忆化是 u5-l3 的主题，这里只需记住「编译阶段是并行的」。

#### 4.1.3 源码精读

先看 `bundle_impl` 的开头：重建 Engine、处理外部样式、调用 realize。这部分在 [lib.rs:141-176](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L141-L176)：

```rust
#[comemo::memoize]
fn bundle_impl(
    world: Tracked<dyn World + '_>,
    library: &LazyHash<Library>,
    introspector: Tracked<dyn Introspector + '_>,
    traced: Tracked<Traced>,
    sink: TrackedMut<Sink>,
    route: Tracked<Route>,
    content: &Content,
    styles: StyleChain,
) -> SourceResult<Bundle> {
    let introspector = Protected::from_raw(introspector);
    let mut locator = Locator::root().split();
    let mut engine = Engine { library, world, introspector, traced, sink,
                              route: Route::extend(route).unnested() };

    // 把外部样式标记为 outside，使其在页级合法
    let styles = styles.to_map().outside();
    let styles = StyleChain::new(&styles);

    let arenas = Arenas::default();
    let children = (engine.library.routines.realize)(
        RealizationKind::Bundle, &mut engine, &mut locator,
        &arenas, content, styles,
    )?;
```

要点：

- 函数挂着 [`#[comemo::memoize]`](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L139)。这意味着它的入参（包括 `Tracked<Sink>`、`Tracked<Introspector>`）一旦没变，就直接复用缓存结果——既是性能优化，也是内省收敛检测的基础（详见 u1-l2）。
- 入参全是 `Tracked<_>` / `TrackedMut<_>` 这些「可追踪」的瘦指针，正是为了让 comemo 能对它们做依赖追踪。
- `realize` 不是直接调用 `typst_realize::realize`，而是走 `engine.library.routines.realize` 这个**函数指针表**。这是 typst 为了拆 crate 用的「动态链接」手法（见 [routines.rs:82-89](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/routines.rs#L82-L89) 的签名与注释）。

紧接着是 collect、parallelize 与收尾，在 [lib.rs:177-219](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L177-L219)：

```rust
    let children = collect(&children, &mut engine, &mut locator)?;

    let mut items = engine
        .parallelize(children, |engine, child| -> SourceResult<_> {
            Ok(match child {
                Child::Tag(tag) => Item::Tag(tag.clone()),
                Child::Asset(asset) => Item::Asset(
                    asset.path.clone().into_inner(),
                    asset.data.0.clone(),
                    asset.location().unwrap(),
                ),
                Child::Document(document, styles, locator) => Item::Document(
                    document.path.clone().into_inner(),
                    compile_document(engine, document, styles, locator)?,
                    document.location().unwrap(),
                ),
            })
        })
        .collect_combined_result::<Vec<_>>()?;
    // ……（建 BundleIntrospector、装 files，见 4.1.2 第 6-7 步）
```

注意三个数据类型在这里完成转换：

- `collect` 把 `Vec<Pair>` 变成 [`Vec<Child>`](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L222-L226)（`Tag` / `Asset` / `Document` 三类，**带生命周期借用原始内容**）。
- `parallelize` 的闭包把每个 `Child` 变成拥有所有权的 [`Item`](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L229-L233)（`Tag` / `Asset(path,bytes,loc)` / `Document(path,doc,loc)`）。这一步会 `clone()` 出路径与字节数据，从而脱离 arena 生命周期。
- 末尾 `.collect_combined_result::<Vec<_>>()?` 会**累计所有子任务的错误**一并返回，而不是遇到第一个错就停（见 [diag.rs:220-250](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/diag.rs#L220-L250)）。

关于 `engine.parallelize` 本身：它给每个子任务**新建一个独立 `Sink`**，并行跑完后把各子 Sink 的 `delayed`/`warnings`/`introspections` 合并回外层 Sink（见 [engine.rs:53-102](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/engine.rs#L53-L102)）。这点对理解「并行编译时各文档的错误怎么汇总」很关键。

#### 4.1.4 代码实践

**实践目标**：在源码里画出 `bundle_impl` 的阶段流转图，并标注每一步的输入/输出类型。

**操作步骤**：

1. 打开 [lib.rs:141](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L141) 的 `bundle_impl`。
2. 找到第 168 行的 `realize(...)`、第 177 行的 `collect(...)`、第 179 行的 `parallelize(...)`、第 197 行的 `BundleIntrospector::new(...)`。
3. 在纸上（或注释里）画出：`Content` →（realize）→ `Vec<Pair>` →（collect）→ `Vec<Child>` →（parallelize）→ `Vec<Item>` →（装填）→ `Bundle`。

**需要观察的现象**：注意 `realize` 与 `collect` 之间**没有并行**（串行单线程），而 `collect` 到 `Item` 之间是 `parallelize`（多线程）。思考：为什么校验必须先于并行、且是串行？

**预期结果**：因为查重需要看到**全部**路径才能判断是否重复，所以必须串行地一次性遍历完；而单个文档的编译彼此独立，才适合并行。这是一条很自然的「先收敛再并行」的流水线。

#### 4.1.5 小练习与答案

**练习 1**：`bundle_impl` 为什么不直接 `typst_realize::realize(...)`，而要走 `engine.library.routines.realize`？

> **参考答案**：这是 typst 的 crate 拆分手法。`typst-bundle` 不直接依赖 `typst-realize` 的具体实现，而是通过 `Library` 里一张函数指针表（`Routines`）间接调用，实现「动态链接」。这样依赖方向更干净，也方便在不同构建配置下替换实现。

**练习 2**：闭包里 `document.location().unwrap()` 直接 unwrap，意味着什么前置条件一定成立？

> **参考答案**：意味着 `DocumentElem` / `AssetElem` 在 realize 的 `prepare` 阶段一定已经被赋予了 `location`（它们是可定位 / 可被标签命中的元素）。若该前置条件不成立，这里会 panic，因此它实际上是对 realize 产出不变量的一种断言。

---

### 4.2 根级实现化：RealizationKind::Bundle 与「空」的 BUNDLE_RULES

#### 4.2.1 概念说明

为什么 bundle 顶层「只能放 document/asset」？答案藏在 realize 给 bundle 选的那张分组规则表里。普通文档实现化会把零散的文字、空格**分组成段落**（`PAR` 规则）、把列表项分组成列表（`LIST`/`ENUM`）等。而 bundle 实现化用的 `BUNDLE_RULES` 是一个**空数组**——不做任何分组。于是顶层一段裸文字「Hello」不会被包装成合法的顶层元素，最终被 `collect` 拒绝。

#### 4.2.2 核心流程

realize 入口根据 `kind` 选择规则表（[typst-realize/src/lib.rs:55-61](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-realize/src/lib.rs#L55-L61)）：

```text
match kind {
    RealizationKind::Bundle        => BUNDLE_RULES,   // = &[]
    RealizationKind::Document {..} => FLOW_RULES,     // = [TEXTUAL, PAR, CITES, LIST, ENUM, TERMS]
    RealizationKind::Fragment {..} => FLOW_RULES,
    RealizationKind::Par           => PAR_RULES,
    RealizationKind::Math          => MATH_RULES,
}
```

各表的定义在 [typst-realize/src/lib.rs:1005-1015](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-realize/src/lib.rs#L1005-L1015)：

```rust
/// Grouping rules used in bundle realization.
static BUNDLE_RULES: &[&GroupingRule] = &[];

/// Grouping rules used in normal realization.
static FLOW_RULES: &[&GroupingRule] = &[&TEXTUAL, &PAR, &CITES, &LIST, &ENUM, &TERMS];
```

空表意味着 `visit_grouping_rules` 永远找不到匹配（[typst-realize/src/lib.rs:700-704](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-realize/src/lib.rs#L700-L704)），于是内容被原样推进 `sink`。最终 `sink` 里就是一串未经分组的「已知类型元素」，交由 `collect` 逐一过安检。

#### 4.2.3 源码精读

`BUNDLE_RULES` 之所以能为空，是因为 bundle 对「放行」有一套额外规则。除了选空表，realize 在处理带样式的元素时，对 bundle 还开了两处**后门**（[typst-realize/src/lib.rs:607-624](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-realize/src/lib.rs#L607-L624)）：

```rust
if elem == DocumentElem::ELEM {
    let local = StyleChain::new(&local);
    if let RealizationKind::Document { info } = &mut s.kind {
        info.populate(local);
    } else if !matches!(s.kind, RealizationKind::Bundle) {
        bail!(style.span(), "document set rules are not allowed inside of containers");
    }
    if local.has(DocumentElem::format)
        && !matches!(s.kind, RealizationKind::Bundle)
    {
        bail!(style.span(), "setting the document format is only supported in the bundle target");
    }
}
```

读法：

- 在普通容器实现化里，`set document(...)` 会被拒绝（「不允许在容器内设置 document」）。
- 但在 `RealizationKind::Bundle` 下，这两条 `bail!` 都被跳过——也就是说 bundle 顶层**允许** `set document(...)` 与 `set document(format: ...)`。这正是 `#document("a.html", include "a.typ")` 这类写法能配合子文件里 `set document(title: ...)` 生效的根源。

把这两点连起来：bundle 的 realize = 「空分组规则」+「放行 document/page 样式」。它只负责把内容**如实地、不分组成段落地**摊开，把「谁有资格做顶层元素」的判断权完全交给下一步的 `collect`。

#### 4.2.4 代码实践

**实践目标**：从源码层面确认「空 BUNDLE_RULES」与「bundle 顶层拒绝裸段落」之间的因果。

**操作步骤**：

1. 在 [typst-realize/src/lib.rs:1006](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-realize/src/lib.rs#L1006) 确认 `BUNDLE_RULES = &[]`。
2. 跟踪 `visit`（[typst-realize/src/lib.rs:242-294](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-realize/src/lib.rs#L242-L294)）里 `visit_grouping_rules` 返回 `false` 后会发生什么：元素被 `s.sink.push((content, styles))` 原样推进结果列表。
3. 得出结论：一段裸文字在 bundle realize 后仍是一个文字/段落元素，因此会在 `collect` 的 `else` 分支被拒。

**需要观察的现象 / 预期结果**：你会确信「顶层拒绝裸段落」**不是** realize 报的错，而是 collect 报的错。realize 对 bundle 几乎「什么都不做」，校验职责被刻意后移到了 collect。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `BUNDLE_RULES` 改成 `&[&PAR]`（像普通文档那样分组段落），`collect` 还会报「not allowed at top-level」吗？

> **参考答案**：行为会改变——零散文字会被 `PAR` 规则分组成一个 `ParElem` 再进入 `collect`，但 `ParElem` 仍然不在 collect 的放行名单（`Tag`/`Asset`/`Document`）里，所以仍会报错，只是报错指向的元素类型变成了 `par` 而非 `text`。这说明「顶层白名单」由 collect 独占决定，与是否分组无关。

**练习 2**：为什么 bundle 需要放行 `set document(format: ...)`？

> **参考答案**：因为一个 bundle 里可以同时有 PDF、PNG、HTML 等不同格式的文档，每个文档的格式需要在各自范围内设定。bundle 顶层允许 `set document(format:)`，使得 `#document(path, include ...)` 配合子文件里的格式设置成为可能。

---

### 4.3 collect：顶层元素校验与路径唯一性检测

#### 4.3.1 概念说明

[`collect`](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L236-L285) 是 bundle 的「安检口」。它做两件事：

1. **白名单校验**：顶层只允许三类元素——`TagElem`（内省标签）、`AssetElem`（原始资产）、`DocumentElem`（文档）。任何其他元素（段落、标题、列表……）都触发 `"{name} is not allowed at the top-level in bundle export"`。
2. **路径查重**：每个 `Asset` / `Document` 都有一个输出路径 `BundlePath`。collect 用一个 `seen` 哈希表记录已见过的路径，重复时触发 `"path \`{}\` occurs multiple times in the bundle"`。

最值得品味的是：这两类错误走**不同的上报通道**——前者进局部 `errors` 向量、函数末尾 `return Err(errors)` **立刻致命**；后者调 `engine.sink.delayed_error(...)` **延迟上报**。

#### 4.3.2 核心流程

```text
fn collect(children) -> Vec<Child>:
    items  = []
    errors = []           # 收集「不允许的顶层元素」错误（致命）
    seen   = {}           # path -> 首次出现的 span

    for (elem, styles) in children:
        if elem 是 TagElem:   items.push(Child::Tag);  continue   # 不查路径
        if elem 是 AssetElem: items.push(Child::Asset); path = elem.path
        if elem 是 DocumentElem: items.push(Child::Document, styles, locator); path = elem.path
        else:                  errors.push("not allowed at top-level"); continue

        match seen.entry(path):
            Vacant  => 记下 span
            Occupied => engine.sink.delayed_error("path occurs multiple times"
                              + hint: "paths must be unique"
                              + hint[旧 span]: "path is already in use here")

    if errors 非空: return Err(errors)     # 立刻致命
    return Ok(items)
```

两类错误的关键差异：

| 维度 | 「不允许的顶层元素」 | 「路径重复」 |
| --- | --- | --- |
| 收集方式 | 局部 `errors: EcoVec` | `engine.sink.delayed_error` |
| 上报时机 | 函数末尾 `return Err` 立刻致命 | 写入 `Sink.delayed`，内省循环末尾才决定 |
| 为什么这样设计 | 结构性错误，迭代再多轮也不可能变好 | 路径可能由 `context`/查询动态计算，早期迭代可能是「假重复」 |

#### 4.3.3 源码精读

完整函数在 [lib.rs:236-285](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L236-L285)。先看白名单与「不允许」分支：

```rust
for (elem, styles) in children {
    let path = if let Some(elem) = elem.to_packed::<TagElem>() {
        items.push(Child::Tag(&elem.tag));
        continue;
    } else if let Some(elem) = elem.to_packed::<AssetElem>() {
        items.push(Child::Asset(elem));
        elem.path.as_ref()
    } else if let Some(elem) = elem.to_packed::<DocumentElem>() {
        items.push(Child::Document(elem, *styles, locator.next(&elem.span())));
        elem.path.as_ref()
    } else {
        errors.push(error!(
            elem.span(), "{} is not allowed at the top-level in bundle export",
            elem.func().name();
            hint: "try wrapping the content in a `document` instead";
        ));
        continue;
    };
```

注意：

- `TagElem` 走 `continue`，**跳过路径查重**——标签没有输出路径，它只服务于内省。
- `AssetElem` / `DocumentElem` 都通过 `elem.path.as_ref()` 拿到一个 `&VirtualPath`（`BundlePath` 实现了 `AsRef<VirtualPath>`，见 [path.rs:276-279](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/foundations/path.rs#L276-L279)；字段定义见 [document.rs:134](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/document.rs#L134) 与 [asset.rs:59](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/asset.rs#L59)），作为查重键。
- 不允许的元素被推进 `errors`，并附带一条 hint：「try wrapping the content in a `document` instead」。

再看查重与延迟上报，在 [lib.rs:264-282](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L264-L282)：

```rust
    match seen.entry(path) {
        Entry::Vacant(entry) => {
            entry.insert(elem.span());
        }
        Entry::Occupied(entry) => {
            engine.sink.delayed_error(error!(
                elem.span(), "path `{}` occurs multiple times in the bundle",
                path.get_without_slash();
                hint: "{} paths must be unique in the bundle",
                elem.func().name();
                hint[*entry.get()]: "path is already in use here";
            ));
        }
    }
}
// ……
if !errors.is_empty() {
    return Err(errors);
}
Ok(items)
```

这里有三个精妙之处：

1. **两条 hint**。除了主 hint「paths must be unique」，还有 `hint[*entry.get()]: "path is already in use here"`。`entry.get()` 取出的是**首次**出现该路径时存下的 `span`，于是第二条 hint 会把用户视线**指回第一次定义的位置**——非常贴心的诊断体验。
2. **`delayed_error` 而非 `errors.push`**。重复路径没有进局部 `errors`，所以它**不会**让 `return Err(errors)` 触发；它被写进了 `Sink.delayed`。
3. **`get_without_slash()`**。错误信息里展示的路径剥掉了前导分隔符（见 [typst-syntax/src/path.rs:559-561](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-syntax/src/path.rs#L559-L561)），对用户更友好。

**为什么重复路径要用 `delayed_error` 延迟上报？**

这是本讲的核心设计点。`bundle_impl` 被 `#[comemo::memoize]` 包住，并在内省循环里被反复调用。`AssetElem` / `DocumentElem` 的 `path` 字段类型是 `BundlePath`，但**它的值可能来自 `context` 或查询**——也就是说，在内省尚未收敛的前几轮，某个文档算出的路径可能是「中间态」。如果某轮里两个文档的路径恰好撞车，但收敛后并不撞车，立刻 `return Err` 就会误报。

`delayed_error` 把错误写进被追踪的 `Sink`（[engine.rs:212-214](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/engine.rs#L212-L214)）。comemo 会把这个错误与「产生它的那次 memoized 调用」绑定：

- 如果后续迭代输入变了，`bundle_impl` 重新计算，旧一轮的 delayed 错误随之作废；
- 只有当某条「路径重复」**坚持到内省循环收敛的最后一轮**仍不消失，它才会被升级为致命错误暴露给用户。

`Sink` 的文档注释把这个机制讲得很清楚（[engine.rs:154-160](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/engine.rs#L154-L160)）：「那些我们可以忽略到最后一轮的错误……先忽略并继续，只有当错误残留到最后一次迭代才升级它」。

对比之下，「不允许的顶层元素」是**结构性**错误——一个裸段落放在 bundle 顶层，无论内省迭代多少轮都不可能变成合法的 document/asset，所以它走 `return Err` 立刻致命，毫无延迟的必要。

#### 4.3.4 代码实践

**实践目标**：动手构造两类错误，并把它们对应到源码的具体分支与 hint。

**任务 A：触发「not allowed at top-level」**

写一个最小 bundle 源文件 `bad.typ`：

```typst
#document("ok.pdf")[This is fine.]

这是一段没有 document 外壳的裸文字。
```

用 `typst compile --features bundle bad.typ` 编译（命令具体行为**待本地验证**，因为本讲无法在此实机运行）。

**需要观察的现象 / 预期结果**：

- 报错信息应包含 `par is not allowed at the top-level in bundle export`（裸文字在 realize 后通常表现为 `par`；也可能是 `text`，取决于是否被分组——见练习 4.2.5-1）。
- 应附带 hint：`try wrapping the content in a \`document\` instead`。
- 对照源码 [lib.rs:256-261](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L256-L261)，确认正是这条 `error!` 宏产生。
- 注意它是**立刻致命**（`return Err(errors)`），所以即便前面有合法的 `#document`，整个 bundle 也不会产出。

**任务 B：触发「path occurs multiple times」并解释延迟上报**

写 `dup.typ`：

```typst
#document("report.pdf")[第一份。]
#document("report.pdf")[第二份，路径撞车。]
```

**需要观察的现象 / 预期结果**：

- 报错信息应包含 `path \`report.pdf\` occurs multiple times in the bundle`。
- 应附带两条 hint：一条 `document paths must be unique in the bundle`；另一条 `path is already in use here`，且**指向第一个 `#document("report.pdf")` 的位置**——这正是 `hint[*entry.get()]` 的效果（[lib.rs:274](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L274)）。
- 写一段说明：该错误走 `engine.sink.delayed_error`（[lib.rs:269](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L269)），而非局部 `errors`，原因是路径可能由 `context`/查询动态产生，在内省未收敛时可能是「假重复」，延迟到末轮才升级能避免误报（依据 [engine.rs:154-160](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/engine.rs#L154-L160) 的注释）。

> 提示：若本地未启用 `bundle` feature，编译器会先在 `warn_or_error_for_bundle` 处报 feature 缺失（见 u1-l1），你将看不到上述两类错误。请确保用 `--features bundle` 编译。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `TagElem` 在 collect 里 `continue`、不参与路径查重？

> **参考答案**：`TagElem` 是内省用的起止标签（`Tag::Start` / `Tag::End`），它不对应任何输出文件，自然没有 `BundlePath`，所以既不会进 `items` 的文件部分（末尾装填时 `Item::Tag(_) => {}` 直接丢弃，见 [lib.rs:205](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L205)），也不需要查重。它被保留下来只是为了给后续的 `BundleIntrospector` 与 `create_link_anchors` 用。

**练习 2**：如果把一个 asset 和一个 document 设成**相同路径**（如都叫 `out`），collect 会怎么判？

> **参考答案**：collect 的 `seen` 表以 `&VirtualPath` 为键，**不区分** Asset 与 Document。所以一个 `#asset("out", ...)` 与一个 `#document("out")[...]` 只要路径相同，就会触发 `path occurs multiple times`（注意主 hint 会用**后出现**的那个元素的 `func().name()` 来填空，所以信息可能是 `asset paths must be unique...` 或 `document paths must be unique...`）。

**练习 3**：`seen` 里存的是 `span` 而不是简单计数，这个设计是为了什么？

> **参考答案**：为了支持 `hint[*entry.get()]: "path is already in use here"` 这条「指向首次出现处」的二级 hint。`entry.get()` 取出的正是首次记录的 `span`，让报错不仅说「重复了」，还能把用户带到「最初定义的位置」。

---

## 5. 综合实践

把本讲三块知识串起来，完成一个「错误诊断小侦探」任务。

**背景**：有人写了下面这个 bundle，但它会报错。请用本讲学到的源码知识，**在不运行的情况下**预测报错，然后（如有条件）运行验证。

```typst
#set document(format: pdf)

#document("intro.pdf", include "intro.typ")

#asset("data.json", json.encode((title: "demo", page: 1)))

Some loose text that does not belong anywhere.

#document("intro.pdf")[我想覆盖前一个文档。]
```

请依次回答：

1. `#set document(format: pdf)` 出现在 bundle 顶层是否合法？依据哪段源码？（提示：[typst-realize/src/lib.rs:617-624](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-realize/src/lib.rs#L617-L624)）
2. 「Some loose text...」会触发哪条错误、哪个 hint？走的是立刻致命还是延迟上报？
3. 第二个 `#document("intro.pdf")` 与第一个路径撞车，会触发哪条错误？为什么这条**不像第 2 问那样立刻致命**，而是延迟上报？两条 hint 分别指向哪里？
4. 如果同时存在第 2 问（结构性错误）和第 3 问（路径重复），用户最先看到哪个？为什么？（提示：结构性错误会让 collect `return Err`，根本不会进行到后续阶段。）

**参考思路**：

1. 合法。因为 `RealizationKind::Bundle` 跳过了「setting the document format is only supported in the bundle target」的 `bail!`——反过来说，bundle 正是**唯一**允许设 format 的目标。
2. 触发 `par is not allowed at the top-level in bundle export` + hint `try wrapping the content in a \`document\` instead`；走局部 `errors`、`return Err` **立刻致命**。
3. 触发 `path \`intro.pdf\` occurs multiple times in the bundle` + 主 hint（`document paths must be unique...`）+ 二级 hint `path is already in use here`（指向**第一个** `#document("intro.pdf")`）。它走 `delayed_error` 延迟上报，因为路径可能由 `context`/查询动态产生，需等内省收敛后确认。
4. 最先看到第 2 问的结构性错误。因为 collect 末尾 `if !errors.is_empty() { return Err(errors); }` 会立刻把整个 `bundle_impl` 拉停；而延迟错误此时还没机会被升级。

> 上述运行结果**待本地验证**（本讲无法在此实机执行 `typst compile`）。但每一条结论都可以直接对照本讲引用的源码行号核对。

## 6. 本讲小结

- `bundle_impl` 的主线是：**重建 Engine → realize（展平）→ collect（校验+查重）→ parallelize（并行编译各文档）→ 建内省器 → 装填 files**。
- bundle 用 `RealizationKind::Bundle` 做根级实现化，它选中的 `BUNDLE_RULES` 是**空数组**——realize 对 bundle 几乎「不做分组」，把合法性判断权交给 collect。
- realize 对 bundle 还**放行** `set document(...)` 与 `set document(format:)`，这是 bundle 能容纳多格式文档的前提。
- collect 的顶层白名单只有 `Tag` / `Asset` / `Document` 三类；其余元素触发 `not allowed at top-level`，进局部 `errors`、`return Err` **立刻致命**。
- 路径查重用 `seen` 哈希表，重复时调 `engine.sink.delayed_error` **延迟上报**，并附两条 hint——其中 `hint[*entry.get()]` 指向首次出现处。
- 「立刻致命」与「延迟上报」的分野，对应「结构性错误」与「可能随内省收敛而消失的错误」两类，这是 comemo 记忆化 + 内省循环设计的直接体现。

## 7. 下一步学习建议

- **下一步读 [u2-l2](u2-l2-compile-document.md)**：本讲的 `parallelize` 闭包里调用了 `compile_document`，它如何为单个文档推断格式（显式 `format` 或路径扩展名）、切换 target、分派到 `typst_layout` 或 `typst_html`，以及 PNG/SVG 的单页约束——都在 u2-l2 详讲。
- **想深入并行与记忆化**：留到 [u5-l3](u5-l3-parallelism-and-memoization.md)，那里系统分析 rayon 并行与 `#[comemo::memoize]` 如何共同支撑多文档互相内省的收敛。
- **想搞懂 collect 之后的内省器与锚点**：`BundleIntrospector` 见 [u5-l1](u5-l1-bundle-introspector.md)，跨文档链接锚点见 [u5-l2](u5-l2-cross-document-links.md)。
- **延伸阅读源码**：直接打开 [`collect` 函数](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L236-L285) 与 [`realize` 入口](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-realize/src/lib.rs#L43-L74)，对照本讲读一遍，印象会更深。
