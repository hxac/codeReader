# 与 layout / html / bundle / math 的集成

## 1. 本讲目标

前几讲我们一直站在 `typst-realize` 内部，把 `realize()` 当成一个自包含的函数来剖析。本讲我们把镜头拉远，从**调用方**的视角看 `realize`：

- 理解 `realize` 为什么不是被直接 `use` 进来、而是通过一张「函数指针表」`Routines` 分发，以及这张表解决了什么工程问题。
- 掌握 8 个真实调用点分别落在哪个 crate、用哪种 `RealizationKind`、在什么场景下被触发。
- 把每个调用点「调完 `realize` 之后拿 `Vec<Pair>` 做了什么」串起来，建立 `realize` 在整个 Typst 编译架构中的位置认知。

学完本讲，你应当能徒手画出一张「谁、在哪里、用什么 kind 调用了 `realize`」的调用关系图。

## 2. 前置知识

本讲默认你已经读过 u3-l4（`RealizationKind` 各模式深入对比）以及 u1-l2（`realize()` 入口与核心数据类型）。我们只复述关键事实，不重复展开：

- **realization（具现化）**：递归套用样式与 show 规则，把任意 content 树规整成「全部由后端已知元素组成、且样式齐全」的扁平清单 `Vec<Pair>`，其中 `Pair<'a> = (&'a Content, StyleChain<'a>)`。
- **`RealizationKind`** 五变体：`Bundle` / `Document { info }` / `Fragment { kind }` / `Par` / `Math`。它在 `realize()` 入口决定选用哪张静态规则表（`BUNDLE/FLOW/PAR/MATH_RULES`）以及 `outside` 初值，详见 [lib.rs:55-64](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L55-L64)。
- **`Arenas`**：临时内存池，承载 `realize` 期间由 show 规则新产出的 content/styles，必须在返回结果处理完毕前一直存活。
- **`Engine`**：编译引擎句柄，内部持有 `library`，而 `library.routines` 正是本讲的主角。

一个需要先建立的直觉：`realize` 的代码住在 `typst-realize` crate，但**调用它的代码遍布 `typst-layout`、`typst-html`、`typst-bundle` 甚至 `typst-library` 自身**。为什么不能让这些 crate 直接 `use typst_realize::realize`？这正是本讲第一个最小模块要回答的问题。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `crates/typst-library/src/routines.rs` | 用宏定义 `Routines` 函数指针表，声明 `realize` 例程的签名；定义 `RealizationKind` / `Arenas` / `Pair`。 |
| `crates/typst-library/src/lib.rs` | `Library` 结构体上的 `routines: &'static Routines` 字段——所有调用点的入口。 |
| `crates/typst/src/lib.rs` | workspace 顶层 crate，用 `static ROUTINES` 把 `realize: typst_realize::realize` 等 fn 指针**注册**进表里。 |
| `crates/typst-realize/src/lib.rs` | `realize()` 函数体本身（被注册的那个函数指针指向它）。 |
| `crates/typst-layout/src/{pages,flow,inline}/mod.rs` | layout 的三个调用点：`Document` / `Fragment` / `Par`。 |
| `crates/typst-html/src/{document,fragment}.rs` | html 的调用点：`Document` / `Fragment` / `Math`。 |
| `crates/typst-bundle/src/lib.rs` | bundle 的调用点：`Bundle`。 |
| `crates/typst-library/src/math/ir/resolve.rs` | math 解析器内部的调用点：`Math`。 |

> 说明：本讲引用了大量**跨 crate** 的源码。永久链接一律指向当前 HEAD `32fd4cc3861e0ab99f4c42ca6bea281482ba9f51` 下的绝对路径。

## 4. 核心概念与源码讲解

### 4.1 realize 作为 routine：函数指针表的注册与分发

#### 4.1.1 概念说明

`typst-realize` 的 `Cargo.toml` 依赖里只有 `typst-library`、`typst-html` 等底层 crate，**刻意不依赖 `typst-layout`**（见 [Cargo.toml:15-26](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/Cargo.toml#L15-L26)）。但真正消费 `realize` 结果的恰恰是 `typst-layout`。

如果让 `typst-layout` 直接 `use typst_realize::realize`，依赖方向是 `layout → realize → library`，看似没问题；可 `typst-layout` 的符号又要被 `typst-library` 通过内置 show 规则注册（`typst_layout::register`），于是会出现 `library → layout → realize → library` 的环。Rust 不允许循环依赖。

Typst 的解法是**「函数指针表」**（routine table）：把 `realize`、`layout_frame`、`eval_string` 等跨 crate 共享的入口都声明成一个 `fn` 指针字段，集中放进 `Routines` 结构体。源码注释把它直白地称作「本质上是动态链接，用于支持 crate 拆分」。这样一来：

- `typst-library` 只声明 `Routines` 的**类型**（不含实现），任何 crate 都能依赖它的类型。
- 真正的**实现**（`typst_realize::realize` 等）由 workspace 顶层 crate `typst` 在一个 `static` 里**一次性注册**。
- 各调用方通过 `engine.library.routines.realize(...)` 间接调用，谁都不直接 `use` 对方的实现，环被切断。

#### 4.1.2 核心流程

`realize` 从「被声明」到「被调用」经过四步：

1. **声明**：`routines!` 宏把 `fn realize<'a>(...) -> SourceResult<Vec<Pair<'a>>>` 翻译成 `Routines` 结构体里的一个字段 `pub realize: for<'a> fn(...) -> SourceResult<Vec<Pair<'a>>>`。
2. **注册**：`typst` crate 的 `static ROUTINES: LazyLock<Routines>` 用 `realize: typst_realize::realize` 把实现绑定到字段上，懒加载、进程级单例。
3. **挂载**：`Library` 持有 `pub routines: &'static Routines`，于是每个 `Engine`（进而每个调用方）都能经由 `engine.library.routines` 拿到这张表。
4. **分发**：调用方写 `(engine.library.routines.realize)(...)`——用括号把字段当函数指针调用。`realize` 内部再依 `RealizationKind` 选规则表（详见 4.x.2 的 u3-l4）。

#### 4.1.3 源码精读

**声明——宏把函数签名变成字段**：

[crates/typst-library/src/routines.rs:20-48](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/routines.rs#L20-L48) —— `routines!` 宏。每个 `fn $name(...) -> $ret` 都会生成 `pub $name: fn(...) -> $ret` 字段，注释点明这是「为支持 crate 拆分而做的动态链接」。

[crates/typst-library/src/routines.rs:81-89](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/routines.rs#L81-L89) —— `realize` 例程的签名声明，文档说它「把 content 具现化为一份扁平的、样式齐全的已知元素清单」。注意这里**只描述接口**，不提供实现。

**注册——把实现绑到字段上**：

[crates/typst/src/lib.rs:311-325](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L311-L325) —— `static ROUTINES`。其中第 320 行 `realize: typst_realize::realize,` 就是关键一句：把 `typst-realize` 里的实现赋给字段。同一张表里还注册了 `layout_frame: typst_layout::layout_frame`、`html_module: typst_html::module` 等——注意 `typst` 这个顶层 crate 同时依赖 `typst-realize`、`typst-layout`、`typst-html`，所以只有它有资格把这些实现「粘」在一起而不产生环。

**挂载——Library 上的字段**：

[crates/typst-library/src/lib.rs:166-169](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L166-L169) —— `Library` 结构体的 `pub routines: &'static Routines` 字段。`&'static` 说明它指向那个进程级单例，所有 `Engine` 共享同一张表。

**被指向的实现——realize() 函数体**：

[crates/typst-realize/src/lib.rs:41-74](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L41-L74) —— `realize` 函数本体。第 55-64 行依 `kind` 选规则表、第 64 行设 `outside` 初值，第 70-71 行调 `visit` 与 `finish`，第 73 行返回 `Ok(s.sink)`。这正是「函数指针指向的那段代码」。

#### 4.1.4 代码实践

**实践目标**：亲手走一遍「声明 → 注册 → 挂载 → 分发」四步链路。

**操作步骤**：

1. 打开 [crates/typst-library/src/routines.rs:81-89](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/routines.rs#L81-L89)，确认这里只有签名、没有 `{| ... |}` 函数体——它是「接口」。
2. 打开 [crates/typst/src/lib.rs:311-325](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L311-L325)，找到第 320 行，确认 `realize` 字段被赋值为 `typst_realize::realize`。
3. 在仓库根目录用搜索工具查 `realize: typst_realize::realize`，确认**全仓库只有这一处**注册点。
4. 打开任一调用点（例如 [flow/mod.rs:152](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-layout/src/flow/mod.rs#L152)），观察 `(engine.library.routines.realize)(...)` 的写法——字段名外层包了一对括号，表示「取出函数指针再调用」。

**需要观察的现象**：声明处没有函数体；注册处把实现名赋给字段；调用处用 `engine.library.routines.<字段名>()` 而非裸函数名。

**预期结果**：四步链路闭合，`realize` 的实现只在 `typst` crate 被引用一次，其余 crate 全程只通过 `Routines` 表间接调用，从而没有 crate 之间的循环依赖。

#### 4.1.5 小练习与答案

**练习 1**：如果删掉 `static ROUTINES` 里第 320 行的 `realize: typst_realize::realize,`，编译会报什么错？

**答案**：`Routines { ... }` 字面量会缺少 `realize` 字段，编译期报「missing field `realize`」。因为 `Routines` 没有 `Default`，每个 fn 指针字段都必须显式赋值——这也是「漏注册」能被编译器兜住的原因。

**练习 2**：为什么注册放在 `typst` crate 而不是 `typst-library`？

**答案**：`typst-library` 不能依赖 `typst-realize` 的**实现**所在依赖图的下游（那样会形成 `library → realize → library` 的环或层次倒挂）。`typst` 是最顶层的「组装」crate，唯一可以同时看见所有实现 crate 的位置，所以由它来粘合。

---

### 4.2 typst-layout 的三处调用：pages / flow / inline

#### 4.2.1 概念说明

`typst-layout` 是 `realize` 最大的消费者。排版本质上是「把 content 摆进二维区域算坐标」，而 `realize` 的产出 `Vec<Pair>` 正是排版的输入。layout 里有三个层级，各对应一个 `RealizationKind`：

- **文档顶层（pages）**：把整篇文档具现化为块级清单，对应 `RealizationKind::Document`——它会回填 `DocumentInfo`、把外部样式标为 `outside` 以便页面级 set 规则生效。
- **块级容器（flow）**：一个 `block`、一个 `box`、表格单元格等「容器」内部的内容，对应 `RealizationKind::Fragment`——它带回一个 `FragmentKind`（Inline/Block），让调用方知道容器是否纯行内。
- **段落正文（inline）**：一个 `par` 的 body，对应 `RealizationKind::Par`——不做段落分组（因为整个 body 本身就是一个段落），只做行内级的 show 规则、空格折叠等。

这三个 kind 选用的规则表呈逐级裁剪关系（`FLOW ⊃ PAR`），详见 u3-l4。

#### 4.2.2 核心流程

三处调用点的代码结构高度同构，都遵循同一个「四件套」模板：

1. **重建 Engine**：从 comemo 的 `Tracked`/`TrackedMut` 原语重新组装一个 `Engine { library, world, introspector, traced, sink, route: Route::extend(route)... }`，并 `engine.route.check_layout_depth()` 防递归过深。
2. **分配 Arenas**：`let arenas = Arenas::default();`——每次调用都新建一个临时池。
3. **调 realize**：`(engine.library.routines.realize)(<kind>, &mut engine, &mut locator, &arenas, content, styles)?`，拿到 `children: Vec<Pair>`。
4. **下游处理**：把 `children` 喂给对应的排版函数（`layout_pages` / `layout_flow` / `layout_inline_impl`），产出 `Frame` 或 `Fragment`。

唯一的差异是 `<kind>` 与第 4 步的下游函数。

#### 4.2.3 源码精读

**① pages——`RealizationKind::Document`**：

[crates/typst-layout/src/pages/mod.rs:128-171](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-layout/src/pages/mod.rs#L128-L171) —— `layout_document_common`（`layout_document` 与 `layout_document_for_bundle` 的共享实现）。关键片段（第 150-169 行）：

```rust
// 把外部样式标记为 outside，使其在 page 层合法
let styles = styles.to_map().outside();
let styles = StyleChain::new(&styles);
let arenas = Arenas::default();

let mut info = DocumentInfo::default();
info.populate(styles);
info.populate_locale(styles);

let mut children = (engine.library.routines.realize)(
    RealizationKind::Document { info: &mut info },   // 回填文档元信息
    &mut engine, &mut locator, &arenas, content, styles,
)?;

let pages = layout_pages(&mut engine, &mut children, &mut locator, styles)?;
Ok(PagedDocument::new(pages, info))
```

注意 `route: Route::extend(route).unnested()`（第 147 行）——文档顶层会把 route 标记为「不再嵌套」，因为这里是排版链路的真正起点。下游 `layout_pages` 把 `children` 切分成页面 run 并行排版。

**② flow——`RealizationKind::Fragment`**：

[crates/typst-layout/src/flow/mod.rs:116-170](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-layout/src/flow/mod.rs#L116-L170) —— `layout_fragment_impl`（带 `#[comemo::memoize]` 缓存）。关键片段（第 150-169 行）：

```rust
let mut kind = FragmentKind::Block;          // 默认块级
let arenas = Arenas::default();
let children = (engine.library.routines.realize)(
    RealizationKind::Fragment { kind: &mut kind },  // 可能被改写成 Inline
    &mut engine, &mut locator, &arenas, content, styles,
)?;

layout_flow(&mut engine, &children, &mut locator, styles, regions, column, kind.into())
```

这里把一个**可变引用** `kind: &mut kind` 交给 `realize`；当容器内容「完全行内」时，`realize` 的 `finish()` 会把它改写成 `FragmentKind::Inline`（详见 u2-l8 的 `is_fully_inline_or_neutral`）。最后 `kind.into()` 把 `FragmentKind` 翻译成 `FlowMode`，决定 flow 的排版模式。

**③ inline——`RealizationKind::Par`**：

[crates/typst-layout/src/inline/mod.rs:72-123](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-layout/src/inline/mod.rs#L72-L123) —— `layout_par_impl`（同样 memoize）。关键片段（第 98-106 行）：

```rust
let arenas = Arenas::default();
let children = (engine.library.routines.realize)(
    RealizationKind::Par,
    &mut engine, &mut locator, &arenas, &elem.body, styles,
)?;

layout_inline_impl(&mut engine, &children, &mut locator, styles, region, expand, /* ... */)
```

注意被具现化的是 `&elem.body`——段落元素本身不进 realize，进的是它的 body；`Par` kind 选用 `PAR_RULES`，不做段落分组。

**三处对照表**：

| 调用点 | 函数 | kind | 被具现化的 content | 下游处理 |
| --- | --- | --- | --- | --- |
| pages | `layout_document_common` | `Document { info }` | 整篇文档根 | `layout_pages` → `PagedDocument` |
| flow | `layout_fragment_impl` | `Fragment { kind }` | 容器内部内容 | `layout_flow` → `Fragment` |
| inline | `layout_par_impl` | `Par` | `ParElem.body` | `layout_inline_impl` → `Fragment` |

#### 4.2.4 代码实践

**实践目标**：验证三处调用点的「四件套」同构性，并理解 kind 差异如何影响下游。

**操作步骤**：

1. 分别打开 pages/flow/inline 三处的 realize 调用（链接见 4.2.3）。
2. 在每处的 `let arenas = Arenas::default();` 后面**临时**加一行 `eprintln!("[layout] kind before realize: {:?}", std::stringify!(<kind>));`（示例代码，仅用于观察；改完务必还原，不要提交）。
3. 用一个最小文档 `#block[A whole #emph[paragraph] of text.]` 编译，观察哪个调用点被触发、触发几次。

**需要观察的现象**：`Document` kind 在文档顶层只触发一次；`Fragment` kind 在每个块级容器触发；`Par` kind 在每个段落触发。

**预期结果**：一次编译里，三处按「文档 → 容器 → 段落」的层次被多次递归触发，印证 4.2.2 的同构模板。若无法本地编译，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 flow 要把 `kind` 作为 `&mut FragmentKind` 传进去，而 pages/inline 不传任何可变引用？

**答案**：flow 处理的是「容器内部」，需要让 `realize` 反馈「内容是否纯行内」以决定 `FlowMode`（行内容器 vs 块容器）。pages 的产物固定是文档，inline 的产物固定是单段落，都不存在这种二选一，故无需回填。

**练习 2**：三处都写了 `let arenas = Arenas::default();`，能不能把 arenas 提到外层共享一个？

**答案**：不能简单共享。`Arenas` 的契约是「必须在返回结果处理完毕前一直存活」（见 routines.rs 文档），但每次 realize 产出的 `Vec<Pair>` 里 `&Content` 指向的正是 arena 里的内存；若复用同一 arena 给嵌套调用，可能在前一层结果未消费完时就回收/干扰内存。每次新建一个 `default()` 是最稳妥的做法（math 解析器是个特例，见 4.3）。

---

### 4.3 html / bundle / math 的调用

#### 4.3.1 概念说明

除了 layout，还有三个消费方：

- **typst-html**：HTML 导出。它和 layout 一样要把 content 具现化，但下游不是「算坐标」，而是「转 HTML 节点」。它复用了 `Document`、`Fragment`、`Math` 三种 kind——这正说明 `realize` 是与「后端」无关的纯内容规整步骤。
- **typst-bundle**：把 content 具现化为「打包资产」（嵌入的图片、字体、子文档等），用独有的 `Bundle` kind（规则表为空，不做任何分组）。
- **typst-library/math**：math 解析器 `MathResolver` 在遇到「数学环境里的任意 content」时，用 `Math` kind 把它具现化成已知元素再逐个解析。这是 `realize` 被**它自己所在的依赖层（typst-library）**调用的特殊情况。

一个值得注意的差异：layout/html/bundle 每次都 `Arenas::default()` 新建池；而 math 解析器复用 `self.arenas`（整个解析过程共享一个池）。

#### 4.3.2 核心流程

- **html/document**：`html_document_common`（`html_document` 与 `html_document_for_bundle` 共享）→ `Document` kind → `convert_to_nodes(Block)` → DOM。
- **html/fragment（块/行内）**：`realize_fragment` 私有助手 → `Fragment` kind（忽略回填的 `FragmentKind`）→ 交 `html_block_fragment`/`html_inline_fragment` 转 nodes。
- **html/fragment（math）**：`html_math_fragment` → `Math` kind → `convert_to_nodes(Inline(quoter))`（MathML 内容，不做段落分组）。
- **bundle**：`bundle_impl` → `Bundle` kind → `collect` → `Item`（Tag/Asset/Document）。
- **library/math**：`MathResolver::resolve_into_self` → `Math` kind → 逐 pair 调 `resolve_realized` 转 `MathItem`。

#### 4.3.3 源码精读

**① html/document——`RealizationKind::Document`**：

[crates/typst-html/src/document.rs:128-170](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-html/src/document.rs#L128-L170) —— `html_document_common`。和 pages 几乎一样的开头（`outside()` 样式、`info.populate`），第 163-170 行调 realize 拿 `Document` 结果，随后第 172-178 行 `convert_to_nodes(..., ConversionLevel::Block, Whitespace::Normal)` 把扁平清单转成 HTML 节点树。

**② html/fragment——`RealizationKind::Fragment`**：

[crates/typst-html/src/fragment.rs:149-168](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-html/src/fragment.rs#L149-L168) —— 私有助手 `realize_fragment`。注释明说「我们忽略 `FragmentKind`，因为块/行内两种我们统一处理」，所以传了 `&mut FragmentKind::Block` 当占位：

```rust
(engine.library.routines.realize)(
    RealizationKind::Fragment { kind: &mut FragmentKind::Block },
    engine, locator, arenas, content, styles,
)
```

它被 `html_block_fragment`（[fragment.rs:69](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-html/src/fragment.rs#L69)）和 `html_inline_fragment` 复用。

**③ html/fragment（math）——`RealizationKind::Math`**：

[crates/typst-html/src/fragment.rs:116-147](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-html/src/fragment.rs#L116-L147) —— `html_math_fragment`，文档注释「使用 math realization，从而不发生段落分组」。第 129-136 行用 `Math` kind，随后 `convert_to_nodes(..., ConversionLevel::Inline(quoter), ...)`。

**④ bundle——`RealizationKind::Bundle`**：

[crates/typst-bundle/src/lib.rs:141-177](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-bundle/src/lib.rs#L141-L177) —— `bundle_impl`。同样有 `outside()` 样式标记，第 168-175 行用 `Bundle` kind。`Bundle` 选用空的 `BUNDLE_RULES`（不做任何分组），因此 realize 在这里几乎只做 show 规则与 kind 规则。下游 `collect` 把清单分类为 `Child::Tag` / `Child::Asset` / `Child::Document`。

**⑤ library/math——`RealizationKind::Math`**：

[crates/typst-library/src/math/ir/resolve.rs:127-146](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/resolve.rs#L127-L146) —— `MathResolver::resolve_into_self`。这是 `realize` 被它「自己的依赖层」调用的特例：

```rust
let pairs = (self.engine.library.routines.realize)(
    RealizationKind::Math,
    self.engine, &mut self.locator, self.arenas, content, styles,
)?;

for (elem, styles) in pairs {
    resolve_realized(elem, self, styles)?;
}
```

注意它复用 `self.arenas`（解析器自带的池），而不是新建 `Arenas::default()`——因为整个 math 解析过程是一个长寿上下文，元素需要存活到解析结束。

**消费方对照表**：

| 调用点 | 函数 | kind | arenas | 下游处理 |
| --- | --- | --- | --- | --- |
| html/document | `html_document_common` | `Document { info }` | 新建 | `convert_to_nodes(Block)` → DOM |
| html/fragment | `realize_fragment` | `Fragment { .. }` | 新建 | `convert_to_nodes` |
| html/fragment(math) | `html_math_fragment` | `Math` | 新建 | `convert_to_nodes(Inline)` |
| bundle | `bundle_impl` | `Bundle` | 新建 | `collect` → `Item` |
| library/math | `resolve_into_self` | `Math` | **复用 `self.arenas`** | `resolve_realized` → `MathItem` |

#### 4.3.4 代码实践

**实践目标**：理解为何 HTML 导出能复用与 layout 相同的 kind，以及 bundle/math 的特殊之处。

**操作步骤**：

1. 打开 [typst-html/src/document.rs:163](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-html/src/document.rs#L163) 与 [typst-layout/src/pages/mod.rs:160](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-layout/src/pages/mod.rs#L160)，对比两处 `Document` 调用的参数与紧随其后的下游函数（`convert_to_nodes` vs `layout_pages`）。
2. 打开 [typst-bundle/src/lib.rs:168](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-bundle/src/lib.rs#L168)，确认 `Bundle` kind 之后紧跟的是 `collect`（分类成 Tag/Asset/Document），而非任何排版/转节点。
3. 打开 [math/ir/resolve.rs:132](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/resolve.rs#L132)，确认第 4 个参数是 `self.arenas`（复用），对比其余 7 处的 `&arenas`（新建）。

**需要观察的现象**：同一 `Document` kind 在 html 和 layout 的「下游处理」完全不同——realize 的产出是后端无关的。

**预期结果**：能说清「realize 只负责把 content 规整成扁平清单，至于清单拿去做 frame、HTML 节点还是 bundle 资产，是各消费方自己的事」。

#### 4.3.5 小练习与答案

**练习 1**：`typst-bundle` 用 `Bundle` kind（空规则表）。如果它改用 `Document` kind 会怎样？

**答案**：`Document` kind 选用 `FLOW_RULES`，会把内容做段落/列表分组、生成 `ParElem` 等，并回填 `DocumentInfo`。但 bundle 只关心「有哪些资产（图片/字体/子文档）和 tag」，根本不需要段落结构——空规则表的 `Bundle` 恰好让它只做最小程度的 show/kind 规则就收尾，避免无谓的分组开销。

**练习 2**：为什么唯独 math 解析器复用 `self.arenas`，而其它 7 处都新建？

**答案**：math 解析是一个长寿的递归过程，`MathResolver` 自始至终在解析一棵 math 子树，期间产出的元素都要存活到整棵子树解析完。新建 arena 会让早期元素的引用失效。而 layout/html/bundle 的每次调用都是「具现化 → 立即消费 → 结束」的短链路，新建 `default()` 最简单也最安全。

---

## 5. 综合实践

**任务**：绘制一张覆盖全部 8 个调用点的「realize 调用关系图」，标注每处的 crate、函数、`RealizationKind`、触发场景与下游处理。

**操作步骤**：

1. 用 4.1.3、4.2.3、4.3.3 给出的 8 个链接，逐处确认 crate / 函数 / kind。
2. 按下面的骨架填空，把每处映射到对应的 `RealizationKind`：

```
                      typst-realize::realize  (实现)
                               ▲
                               │ realize: typst_realize::realize  (注册)
                      static ROUTINES (typst crate)
                               ▲
                      Library.routines: &'static Routines
                               ▲
              ┌────────────────┼────────────────┬───────────────┐
              │                │                │               │
        typst-layout       typst-html      typst-bundle    typst-library
              │                │                │            (math)
   ┌──────────┼──────────┐    │                │               │
   │          │          │    │                │               │
 pages      flow      inline  │             bundle         resolve_into_self
 Document  Fragment   Par     │             Bundle          Math
 (根文档)  (容器)    (段落)   │             (打包)          (数学环境)
                              │
                    ┌─────────┼─────────┐
                    │         │         │
              document   fragment   fragment(math)
              Document   Fragment   Math
              (HTML根)  (HTML块/行内)(MathML)
```

3. 为每个调用点写一句话「触发场景」，例如：`flow / Fragment —— 每个块级容器（block、表格单元格）内部内容排版时触发；realize 回填 FragmentKind 决定容器是否纯行内。`

**需要观察的现象 / 预期结果**：8 个调用点按 `RealizationKind` 归类后恰好是 `Bundle×1 / Document×2 / Fragment×2 / Par×1 / Math×2`。每个 kind 的语义（选哪张规则表、是否 outside、是否回填）与它的触发场景一一对应——这正是 u3-l4 所讲的「场景标签」在真实代码里的落点。

**进阶（可选）**：在每处 realize 调用前临时插入 `eprintln!` 打印 kind 与 content 的元素名，编译一个含「段落 + 列表 + 行内公式 + 图片」的文档，把真实触发顺序抄下来，与你画的图互相印证（标注「待本地验证」亦可）。

## 6. 本讲小结

- `realize` 通过一张**函数指针表 `Routines`** 被分发，而非被直接 `use`——这是为了在「`typst-realize` 不依赖 `typst-layout`」的前提下切断 crate 间的循环依赖，注释称之为「支持 crate 拆分的动态链接」。
- 链路四步：`routines!` 宏**声明**签名 → `typst` crate 的 `static ROUTINES` **注册** `realize: typst_realize::realize` → `Library.routines: &'static Routines` **挂载** → 调用方 `(engine.library.routines.realize)(...)` **分发**。
- `typst-layout` 有三处调用、遵循同构的「重建 Engine → 新建 Arenas → 调 realize → 下游排版」模板：pages=`Document`、flow=`Fragment`、inline=`Par`。
- `typst-html` 复用 `Document`/`Fragment`/`Math` 三种 kind，但下游换成 `convert_to_nodes` 转 HTML 节点；`typst-bundle` 用独有的 `Bundle`（空规则表）只做最小规整；`typst-library/math` 用 `Math` kind 且复用 `self.arenas` 而非新建。
- 全仓库共 **8 个** `routines.realize` 调用点，按 kind 归类为 `Bundle×1 / Document×2 / Fragment×2 / Par×1 / Math×2`。
- 关键认知：`realize` 只负责「把任意 content 规整成扁平的、后端无关的 `Vec<Pair>`」，至于清单拿去做 Frame、HTML 节点还是 bundle 资产，完全是各消费方自己的事——这正是它在 eval 与 layout/导出之间充当「唯一桥梁」的体现。

## 7. 下一步学习建议

- **横向**：若想看「同一张 `Routines` 表里其它例程（`layout_frame`、`eval_string`、`html_module`）是如何被同样注册与分发的」，可对照阅读 [typst/src/lib.rs:311-325](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L311-L325)，把本讲的「四步链路」复用到它们身上。
- **纵向（下一个专家层主题）**：建议接着学 u3-l6「递归深度限制与错误处理」——它解释了本讲反复出现的 `engine.route.check_layout_depth()` / `check_html_depth()`、`Route::extend` 与 `engine.delay` 如何共同保证 8 个调用点在嵌套递归与内省循环中不会失控。
- **补全 math 链路**：本讲只点到 `MathResolver::resolve_into_self` 调用 `Math` kind；想了解它拿到 `Vec<Pair>` 之后如何逐个解析成 `MathItem`，可顺着 [resolve.rs:141-143](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/resolve.rs#L141-L143) 的 `resolve_realized` 往下读。
