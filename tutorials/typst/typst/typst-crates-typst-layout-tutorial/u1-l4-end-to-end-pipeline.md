# 端到端排版流程：Content 到 PagedDocument

## 1. 本讲目标

前几讲我们分别认识了 typst-layout 的定位（u1-l1）、目录结构（u1-l2）和公共 API（u1-l3）。本讲把所有零件串起来，回答一个核心问题：

> **一份已经求值好的 Content，是怎么一步步变成一本可以导出 PDF 的 `PagedDocument`（若干页 `Page`）的？**

学完本讲，你应当能够：

1. 说清 `realize`（现实化）这一步在排版**之前**做了什么，以及它为什么不属于「排版」本身。
2. 跟着 `layout_document → layout_document_common → realize → layout_pages` 这条链路，画出一张端到端的数据流图。
3. 看懂 `layout_pages` 内部的「**collect（切分）→ 并行排 page run → finalize（组装）**」三段式。
4. 理解最终产物 `PagedDocument` 是如何由 `PagedDocument::new` 组装出来，并理解 introspector（内省器）为什么是「派生物」。
5. 给定一个简单文档（几段文字 + 一个分页符），手动追踪 `collect` 产出的 items、并行排版的 page run，以及最终的 pages 列表。

---

## 2. 前置知识

本讲依赖以下概念（前几讲已建立，这里只做一句话回顾）：

- **Content**：Typst 中「内容」的统一表示，是求值/realize 的产物，也是排版的输入。本讲不关心它怎么来的，只关心它怎么被排成页。
- **StyleChain（样式链）**：一段内容所携带的样式上下文。排版几乎处处需要它来决定字号、页边距、对齐等。
- **Engine**：贯穿排版的「引擎/上下文」，打包了 world、library、introspector、route、sink 等可追踪（Tracked）参数。u1-l3 已经讲过入口函数会把 `&mut Engine` 拆成这些 tracked 参数后交给带 `#[comemo::memoize]` 的 `_impl` 函数。
- **Frame / Fragment / Regions**：排版结果的载体与输入画布。文档级入口（本讲主角）**不接收 Regions**，因为页面尺寸由样式链里的 `page` 配置决定——这是它与片段级入口的关键区别（见 u1-l3）。
- **introspector（内省器）**：排版完成**之后**，对最终页面做一次扫描，建出查询索引，让 `query`、计数器、目录、交叉引用等功能能工作。

本讲会用到两个之前没细讲的术语，先建立直觉：

- **realize（现实化）**：把「抽象的、嵌套的、可能含用户自定义 show 规则的」Content，转成「扁平的、由若干已知元素组成的」列表。可以理解为：排版引擎**只认识**一组「老熟人」元素（段落、块、分页符、标签……），realize 的工作就是把千变万化的 Content「翻译」成这些老熟人。
- **Pair**：realize 的输出单元，就是一个「元素 + 它适用的样式链」的二元组：`(&Content, StyleChain)`。排版时既要看「是什么元素」，也要看「在什么样式下」。

---

## 3. 本讲源码地图

本讲覆盖三个最小模块：**pages、document、routines**。涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [src/pages/mod.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/mod.rs) | 文档级排版的主入口。包含 `layout_document`、`layout_document_common`（负责 realize 并调用 `layout_pages`）、以及 `layout_pages`（collect→并行→finalize 三段式）。**本讲的核心文件。** |
| [src/pages/collect.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/collect.rs) | `layout_pages` 的第一阶段：把 realize 输出的扁平 children 切成 `Item::Run` / `Item::Tags` / `Item::Parity`。 |
| [src/pages/run.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/run.rs) | `layout_pages` 的第二阶段：`layout_page_run` 把「一段页面内容」排成 `LayoutedPage`（尚未最终化的页面，含正文 frame + 页眉/页脚/背景等边带信息）。 |
| [src/pages/finalize.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/finalize.rs) | `layout_pages` 的第三阶段：`finalize` 把 `LayoutedPage` 拼成最终 `Page`（需要知道物理页号才能处理左右页边距）。 |
| [src/document.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/document.rs) | 产物定义：`PagedDocument` 与 `Page`。`PagedDocument::new` 在这里组装并构建 introspector。 |
| [crates/typst-library/src/routines.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/routines.rs) | `realize` 的 trait 声明、`RealizationKind` 枚举、`Pair` 类型别名。**（属于 typst-library，跨 crate 引用）** |

> 提示：本讲引用的源码以 `crates/typst-layout/` 内的文件为主；`realize` 的声明在 `typst-library`（排版引擎只**调用**它，不实现它，实现位于 typst-realize crate）。

---

## 4. 核心概念与源码讲解

本讲按数据流自然分成三个最小模块：

- **4.1 realize——排版前的「现实化」**（routines 模块）
- **4.2 layout_pages 三段式：collect → 并行 → finalize**（pages 模块，核心）
- **4.3 PagedDocument 组装与 introspector 构建**（document 模块）

### 4.1 realize：排版前的「现实化」

#### 4.1.1 概念说明

排版引擎很「专一」：它只想处理一组**已知元素**（段落 par、块 block、分页符 pagebreak、标签 tag、列断点 colbreak、浮动 place……）。但用户写出来的 Content 可能层层嵌套、含自定义 `show` 规则、含还没展开的元素。

`realize` 就是「翻译官」：它递归地把任意 Content 展开成一个**扁平的 `Vec<Pair>`**——其中每个 `Pair` 就是 `(已知元素, 它的样式链)`。这样排版引擎拿到的是一张「干净的、扁平的待排清单」，专心做几何排布即可。

关键点：**realize 不排版、不算几何、不画任何东西**。它只负责「把内容变成排版引擎认识的形式」。这也是它被放在排版**之前**、又用独立 crate（typst-realize）实现的原因。

#### 4.1.2 核心流程

realize 接受一个 `RealizationKind`（现实化种类）参数，告诉它「这次现实化是在什么语境下」：

```
RealizationKind:
  ├── Bundle     // 编译 bundle（文档 + 资源），用于主题/包
  ├── Document   // 根级文档现实化，需要回填 DocumentInfo 元信息
  ├── Fragment   // 容器内的片段现实化（如 block、html.div）
  └── Par        // 段落内的现实化
```

本讲只关心 `RealizationKind::Document`。它的特殊之处是带了一个 `&mut DocumentInfo`：realize 会顺便把文档里的 `set document(title: ..., author: ...)` 等规则读出来，填进这份元信息（标题、作者、日期、语言等）。

简化后的流程：

```
content（任意 Content）
   │
   │  routines.realize(RealizationKind::Document { info }, engine, locator, arenas, content, styles)
   ▼
Vec<Pair> = [(已知元素₁, 样式₁), (已知元素₂, 样式₂), …]   ← 扁平、干净的待排清单
   │
   │  （同时：info 被填充了文档元信息）
   ▼
交给 layout_pages 排版
```

#### 4.1.3 源码精读

`realize` 的声明在 typst-library，注意它的文档注释和返回类型：

- [crates/typst-library/src/routines.rs:81-89](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/routines.rs#L81-L89) —— 注释明确写着「Realizes content into a flat list of well-known, styled items」，返回 `Vec<Pair<'a>>`。这就是本小节直觉的官方表述。
- [crates/typst-library/src/routines.rs:195-196](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/routines.rs#L195-L196) —— `Pair` 的定义：`(&'a Content, StyleChain<'a>)`。

`RealizationKind` 的定义与各分支注释：

- [crates/typst-library/src/routines.rs:154-166](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/routines.rs#L154-L166) —— 注意 `Document { info: &'a mut DocumentInfo }` 分支注释：「Requires a mutable reference to document metadata that will be filled from `set document` rules.」

排版引擎里**调用** realize 的地方在 `layout_document_common`：

- [src/pages/mod.rs:160-167](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/mod.rs#L160-L167) —— 这里调用 `engine.library.routines.realize(RealizationKind::Document { info: &mut info }, …)`，把返回的 `children` 直接传给下一行的 `layout_pages`。注意 `info` 是以可变引用传入的——realize 会回填它。

#### 4.1.4 代码实践

**实践目标**：确认「realize 在排版之前、且只产出扁平清单」这一直觉，并理解 `Document` 现实化如何回填元信息。

**操作步骤**：

1. 打开 [src/pages/mod.rs:156-167](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/mod.rs#L156-L167)，注意 `info` 先用默认值创建（L156-158 调 `populate` / `populate_locale`），再以 `&mut info` 传给 realize（L161）。
2. 跳转到 [crates/typst-library/src/routines.rs:81-89](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/routines.rs#L81-L89)，确认 realize 的返回类型 `Vec<Pair>`。
3. 搜索 `RealizationKind::Document` 的**实现**（在 typst-realize crate，可用 `Grep` 全工程搜 `RealizationKind::Document`），观察它如何把 `set document(...)` 的样式读进 `info`。

**需要观察的现象**：realize 的调用处没有任何尺寸计算、没有 `Frame`、没有 `Regions`——它纯粹是「数据形态转换」。

**预期结果**：你会得出结论——realize 是排版流水线的「前置翻译层」，与几何排版正交。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `RealizationKind::Document` 要带一个 `&mut DocumentInfo`，而 `RealizationKind::Fragment` / `Par` 不需要？

> **参考答案**：因为只有文档级现实化需要从用户的 `set document(...)` 规则里提取标题、作者等元信息并回填；片段级和段落级现实化不涉及文档元信息，所以没有这个可变引用参数。

**练习 2**：`Pair` 为什么要把「样式链」和「元素」绑在一起，而不是只给一个元素列表？

> **参考答案**：同一个元素在不同样式下排版结果完全不同（字号、颜色、对齐、是否分页都由样式决定）。把样式随元素一起扁平化，排版时才能逐个元素独立处理，也才能安全地**并行**排版不同片段（每个 Pair 自带完整样式上下文，互不依赖）。

---

### 4.2 layout_pages 三段式：collect → 并行 → finalize

> 这是本讲的核心。`layout_document_common` 拿到 realize 的 `children` 后，立即调用 [src/pages/mod.rs:169](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/mod.rs#L169) 的 `layout_pages`。该函数内部严格分成三段。

#### 4.2.1 概念说明

`layout_pages` 要解决的问题是：**把一长串扁平 children 排成若干页**。这件事天然分三步：

1. **collect（切分）**：children 里混杂着正文内容和分页符 `PagebreakElem`。要按分页符把内容切成一段段「页面运行（page run）」。同时还要处理纯标签、奇偶页补页等边角情况。产物是一个 `Item` 列表。
2. **并行排版 page run**：每段 page run 可以**独立**排版（这正是 realize 把样式随元素扁平化的回报）。于是用 `engine.parallelize` 多线程跑 `layout_page_run`，每段产出若干个「几乎完成、但还差物理页号」的 `LayoutedPage`。
3. **finalize（组装）**：并行阶段无法知道「这一页是第几页」（页号要在所有页确定后才能算），所以涉及物理页号的工作（左右页边距互换、计数器更新、页码步进、标签挂载）必须留到这一步，串行地把 `LayoutedPage` 拼成最终 `Page`。

#### 4.2.2 核心流程

```
children: Vec<Pair>  （realize 的输出，扁平）
   │
   │  ① collect
   ▼
items: Vec<Item>
   ├── Item::Run(&[Pair], styles, locator)   // 一段页面内容，可并行排版
   ├── Item::Tags(&[Pair])                   // 页与页之间的标签，挂到下一页开头/末页末尾
   └── Item::Parity(parity, styles, locator) // 指令：必要时补一页以满足奇偶要求
   │
   │  ② parallelize：对每个 Item::Run 并行调用 layout_page_run
   ▼
runs: 迭代器，每项是 Vec<LayoutedPage>      // 每个 LayoutedPage = 正文 frame + 页眉/页脚/背景等
   │
   │  ③ 串行 finalize：按 items 顺序消费 runs，
   │     处理 Parity 补页、Tags 挂载、计数器
   ▼
pages: EcoVec<Page>   （最终页面列表）
```

三段的分工用一句话概括：**collect 负责「按页切分」，并行段负责「排正文」，finalize 负责「依赖页号的收尾」**。

#### 4.2.3 源码精读

**入口 `layout_pages` 全貌**（本讲最重要的一个函数）：

- [src/pages/mod.rs:175-241](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/mod.rs#L175-L241) —— 通读这个函数即可看到三段式。

**① collect**：

- [src/pages/mod.rs:182](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/mod.rs#L182) —— 一行 `let items = collect(children, locator, styles);` 开启第一阶段。
- `Item` 三种变体见 [src/pages/collect.rs:8-19](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/collect.rs#L8-L19)，每个变体的注释已经讲清了它们的归宿。
- `collect` 的主循环见 [src/pages/collect.rs:37-109](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/collect.rs#L37-L109)：遇到分页符（L38）就判断 weak/strong、是否补空页（L41-45）、是否产生 Parity 指令（L48-51）；遇到普通内容就找出连续非分页符段，产出 `Item::Run`（L106）。`staged_empty_page` 是一个贯穿全程的布尔标志，决定结尾是否补一页（L112-114）。

**② 并行排版**：

- [src/pages/mod.rs:185-195](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/mod.rs#L185-L195) —— `engine.parallelize` 用 `filter_map` 只挑出 `Item::Run`，对每个 run 调 `layout_page_run`。注意 `parallelize` 返回的是一个**保持顺序**的迭代器（typst-library 里特意 collect 成 Vec 再 `into_par_iter`，正是因为并行桥接会丢顺序）。
- `layout_page_run` 在 [src/pages/run.rs:55-74](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/run.rs#L55-L74) 是薄封装，真正逻辑在记忆化的 `layout_page_run_impl`（[src/pages/run.rs:77-248](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/run.rs#L77-L248)）：它解析页面尺寸/边距（L108-136），用 `layout_flow` 排正文（L190-202），再分别排页眉/页脚/背景/前景（L205-244），产出 `Vec<LayoutedPage>`。
- `LayoutedPage` 结构见 [src/pages/run.rs:28-43](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/run.rs#L28-L43)，注释点明它「只差一个物理页号就能 finalize」。

**③ finalize**：

- [src/pages/mod.rs:203-230](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/mod.rs#L203-L230) —— 串行遍历 `items`：`Item::Run` 取 `runs.next()` 逐个 finalize（L206-211）；`Item::Parity` 在页数奇偶不匹配时补一张空页（L212-220）；`Item::Tags` 把标签暂存到 `tags` 缓冲（L221-229）。
- [src/pages/mod.rs:232-238](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/mod.rs#L232-L238) —— 收尾：剩余未挂载的标签全部塞到最后一页末尾。
- `finalize` 本体在 [src/pages/finalize.rs:12-83](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/finalize.rs#L12-L83)：按 `binding.swap(物理页号)` 决定左右页边距是否互换（L35-41），按 background→header→正文→footer→foreground 顺序拼装 frame（L56-73），最后 `counter.visit` 应用页内计数器更新、`counter.logical()` 取本页页号、`counter.step()` 步进（L76-82）。

#### 4.2.4 代码实践

**实践目标**：给定一个简单文档，手动模拟 `collect` 的输出、并行排版的 page run，以及最终 pages 列表。这是本讲的主实践。

**操作步骤**：假设有一份文档，realize 后得到的 `children`（简化表示，每个非分页符项视为一段正文 `P`）如下：

```
children = [ P1, P2, Pagebreak(strong), P3 ]
                       └ weak=false，强分页
```

请对照 [src/pages/collect.rs:23-117](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/collect.rs#L23-L117) 逐步推导。

**推导过程**（初始 `staged_empty_page = true`，`initial = s0`）：

| 步骤 | 当前 child | 命中分支 | 动作 | `staged_empty_page` | items 累积 |
| --- | --- | --- | --- | --- | --- |
| 1 | P1（非分页符） | else 分支 L67 | 连续非分页符段 = [P1]；非全标签；`Run([P1], s0)`；`staged=false` | false | `[Run([P1], s0)]` |
| 2 | Pagebreak(strong) | if 分支 L38 | `strong && staged`? staged=false → **不补空页**；`to=None` → 无 Parity；`boundary=false` → `initial = s_break`；`staged |= strong` | **true** | `[Run([P1], s0)]` |
| 3 | P3（非分页符） | else 分支 L67 | 连续段 = [P3]；`Run([P3], s_break)`；`staged=false` | false | `[Run([P1], s0), Run([P3], s_break)]` |
| 4 | 结束 | L112 | `staged` 为 false → 不补尾页 | false | （不变） |

**所以 collect 产出**：

```
items = [
  Item::Run([P1], s0, loc0),
  Item::Run([P3], s_break, loc1),
]
```

**并行排版（步骤 ②）**：`parallelize` 对两个 `Run` 并行调 `layout_page_run`：

- `Run([P1], s0)` → `Vec<LayoutedPage>`，假设 P1 只占 1 页 → `[L1]`
- `Run([P3], s_break)` → `[L2]`

**finalize（步骤 ③）**：按 items 顺序消费 runs，逐个 `finalize`：

- `Item::Run([P1], …)` → 取 `runs.next()` 得 `[L1]` → `finalize(L1)` → `Page{number:1}`
- `Item::Run([P3], …)` → 取 `runs.next()` 得 `[L2]` → `finalize(L2)` → `Page{number:2}`

**最终 pages**：`[Page{1}, Page{2}]`，共 2 页。

**需要观察的现象**：强分页符出现在 P1 之后但**没有**额外多出一张空白页——因为 `staged_empty_page` 在 P1 排版后已被置为 false。如果你把 `P1` 去掉（文档以分页符开头），`staged_empty_page` 会保持 true，从而在开头补一张空页。

**预期结果 / 待本地验证**：上面的手算结果是「2 页」。你可以用 typst CLI 编译一个等价文档（`#page(P1) #pagebreak() #page(P3)` 风格的强分页）查看页数是否吻合；若行为与本讲描述不一致，请以本地实际版本为准并回来核对 `collect.rs` 的当前实现。

#### 4.2.5 小练习与答案

**练习 1**：如果把上面的强分页符改成**弱分页符**（`weak=true`），并且 P1 不占满一页，`collect` 会产出几个 `Item::Run`？

> **参考答案**：弱分页符 `strong=false`，不会触发补空页（L42 的 `strong && staged_empty_page` 不成立），`staged_empty_page |= strong` 也不会变 true。但它仍然是一条分页符，会把 children 在此处切开。所以仍然产出两个 `Item::Run`：`Run([P1], s0)` 和 `Run([P3], s_break)`。弱与强的差别主要体现在「是否补空白页」和与相邻分页符的合并语义上，而不是「是否切分」。

**练习 2**：为什么 `finalize` 必须放在并行排版**之后**、且是**串行**的？

> **参考答案**：因为 finalize 里有些工作依赖「物理页号」——比如 `binding.swap(counter.physical())` 决定左右页边距互换（[finalize.rs:35](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/finalize.rs#L35)），以及 `counter.visit/step` 维护页码计数（L76-82）。而物理页号只有在所有页按顺序排定后才知道，并行排版阶段无法确定。所以 finalize 必须在并行之后、按 pages 顺序串行执行。

---

### 4.3 PagedDocument 组装与 introspector 构建

#### 4.3.1 概念说明

`layout_pages` 返回 `EcoVec<Page>` 后，整条链路就到了终点：在 [src/pages/mod.rs:171](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/mod.rs#L171) 调用 `PagedDocument::new(pages, info)` 组装出最终产物。

`PagedDocument` 内部有三大块：

1. **pages**：最终页面列表（每个 `Page` 的核心是一张 `Frame`）。
2. **info**：文档元信息（标题、作者、日期、语言……），就是 4.1 里 realize 回填的那份 `DocumentInfo`。
3. **introspector**：内省器。它**不是输入**，而是对 pages 做一次扫描后**派生**出来的查询索引。

强调「派生」这一点很重要：introspector 完全由 pages 决定，所以它不参与 `Hash`（否则会造成缓存依赖循环）。introspector 是让 `query`、计数器定位、目录、交叉引用等功能在排版**完成后**能工作的基础设施。

#### 4.3.2 核心流程

```
pages: EcoVec<Page>          ← 来自 layout_pages
info:  DocumentInfo          ← 来自 realize 的回填
            │
            │  PagedDocument::new(pages, info)
            ▼
      内部调用 PagedIntrospector::new(&pages)
            │
            │  扫描每个 Page 的 frame，收集 Tag/Group/Link，建索引
            ▼
PagedDocument { pages, info, introspector }
```

#### 4.3.3 源码精读

- [src/document.rs:17-21](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/document.rs#L17-L21) —— `PagedDocument` 的三字段：`pages`、`info`、`introspector`。
- [src/document.rs:27-30](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/document.rs#L27-L30) —— `new` 构造函数：注释「Internally builds the introspector」，内部 `PagedIntrospector::new(&pages)` 即派生过程。
- [src/document.rs:48-55](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/document.rs#L48-L55) —— `Hash` 实现只哈希 `pages` 和 `info`，注释明确：「The introspector is fully derived from the pages. Thus, there is no need to hash it.」这正是 introspector「派生性」在代码里的直接体现，也是 comemo 缓存能正确工作的前提（introspector 不参与哈希，就不会因为自身重建导致缓存键变化）。
- [src/document.rs:63-79](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/document.rs#L63-L79) —— `Output` trait 实现：`target()` 返回 `Target::Paged`，而 `create` 直接委托给 `crate::layout_document`。这说明 `layout_document` 正是「把 Content 排成分页文档」这一 `Output` 的标准实现。
- [src/document.rs:82-105](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/document.rs#L82-L105) —— `Page` 结构：核心字段是 `frame`，其余（`bleed`、`fill`、`numbering`、`supplement`、`number`）都是导出器需要的边带信息。注意 `number` 注释：逻辑页号（受 `counter(page)` 控制），可能与物理页号不一致。

#### 4.3.4 代码实践

**实践目标**：确认「introspector 是 pages 的纯派生物、不参与 Hash」这一关键性质。

**操作步骤**：

1. 打开 [src/document.rs:27-30](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/document.rs#L27-L30)，确认 `PagedIntrospector::new(&pages)` 的输入**只有 pages**——没有任何额外的外部状态。
2. 打开 [src/document.rs:48-55](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/document.rs#L48-L55)，确认 `Hash` 只对 `pages` 和 `info` 调 `.hash(state)`，**没有**对 `introspector` 哈希。
3. 思考：如果 `introspector` 也参与 Hash，会出什么问题？

**需要观察的现象 / 预期结果**：你会得出——因为 introspector 完全由 pages 决定且不参与哈希，所以「相同 pages → 相同缓存键」，comemo 才能安全地缓存 `layout_document_impl` 的结果。这也是为什么 introspector 的构建放在 `PagedDocument::new`（组装时）而不是更早。

#### 4.3.5 小练习与答案

**练习 1**：`Page` 的 `number` 字段注释说「逻辑页号，可能与物理页号不一致」。请举一个会让二者不一致的例子。

> **参考答案**：当用户用 `set page(numbering: "1")` 之后又用 `counter(page).update(n)` 跳号，或在某页 `#counter(page).update(1)` 重置，逻辑页号就会与「这是第几张物理纸」不一致。物理页号是纸张的物理序号（第 1、2、3 张……），逻辑页号是 `counter(page)` 解析出来的编号，可被用户任意改写。

**练习 2**：为什么 `layout_document`（[document.rs:72-78](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/document.rs#L72-L78) 的 `Output::create`）和 `PagedDocument::new` 之间要先经过 `layout_pages`，而不是 `new` 自己去排页？

> **参考答案**：职责分离。`layout_pages` 负责「把扁平内容排成页面列表 + 处理分页/补页/计数器」这一重活；`PagedDocument::new` 只负责「把现成的 pages 和 info 打包，并派生 introspector」。让 `new` 保持轻量（纯组装 + 派生），既利于理解，也让 introspector 的派生时机明确（一定在所有页都排定之后）。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「端到端追踪」。**实践目标**：亲手把一份带分页符的文档，从 Content 一路追到 `PagedDocument`，画出完整数据流。

**场景**：一份 Typst 文档，正文是「段落 A」，中间一个**弱分页符**加一个**强分页符**，最后「段落 B」：

```typst
段落 A
#pagebreak(weak: true)
#pagebreak()      // 默认 strong
段落 B
```

**任务**：

1. **realize 阶段（对应 4.1）**：写出 realize 之后 `children`（`Vec<Pair>`）的**简化形态**。提示：弱/强分页符各自会变成一个 `PagebreakElem` Pair；段落会变成段落级的 Pair。
2. **collect 阶段（对应 4.2）**：对照 [src/pages/collect.rs:37-114](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/collect.rs#L37-L114)，逐步推导 `staged_empty_page` 与 `initial` 的变化，写出最终 `items` 列表（标明每个 `Item::Run` / `Item::Tags` / `Item::Parity`）。**关键观察点**：连续的弱+强分页符，`staged_empty_page` 会被置为 true 吗？在何处会补一张 `Run(&[])` 空页？
3. **并行 + finalize 阶段（对应 4.2/4.3）**：写出每个 `Item::Run` 并行排版得到的 `Vec<LayoutedPage>`（按段落数假设每段 1 页），再写出串行 finalize 后的 `pages` 列表与每页 `number`。
4. **组装阶段（对应 4.3）**：说明 `PagedDocument::new(pages, info)` 在这一步做了什么，introspector 是何时、由什么派生的。

**预期产出**：一张类似 4.2.4 的推导表 + 一段对 `PagedDocument::new` 的说明。

**验证方式**：用 typst CLI 编译该文档，查看实际页数与你在第 3 步得到的 `pages.len()` 是否一致；若不一致，回到 `collect.rs` 核对当前实现（弱/强分页符、`staged_empty_page`、`boundary` 的处理细节以本地源码为准）。若你只读源码、暂未编译，请在结论处标注「待本地验证」。

> 提示：本实践是「源码阅读型实践」+「可选运行验证」。即使不编译，只要能把 children→items→pages 的推导说清楚，就达到了本讲的训练目的。

---

## 6. 本讲小结

- typst-layout 的文档级排版是一条清晰链路：`layout_document → layout_document_impl → layout_document_common`，在 `layout_document_common` 里先 `realize`、再 `layout_pages`、最后 `PagedDocument::new`。
- **realize** 是排版**之前**的「翻译层」：把任意 Content 展开成扁平的 `Vec<Pair>`（每个 Pair = 已知元素 + 样式链），`RealizationKind::Document` 还会顺便回填 `DocumentInfo` 元信息；它不算几何、不画东西。
- **layout_pages 是三段式**：① `collect` 按分页符把 children 切成 `Item::Run` / `Item::Tags` / `Item::Parity`；② `engine.parallelize` 对每个 `Run` 并行调 `layout_page_run` 得到 `LayoutedPage`；③ 串行 `finalize` 依赖物理页号做左右边距互换、计数器、页码步进、标签挂载，产出最终 `Page`。
- 并行排版之所以可行，是因为 realize 把样式随元素一起扁平化了，每个 page run 自带完整上下文、彼此独立。
- **introspector 是纯派生物**：`PagedDocument::new` 扫描 pages 建索引，它不参与 `Hash`，这是 comemo 缓存正确的前提。
- `PagedDocument` 同时实现 `Output`（`create` 委托 `layout_document`、`target` 是 `Target::Paged`），是「分页文档」这一导出形态的标准产物。

---

## 7. 下一步学习建议

本讲建立了「文档 → 页面」这一层的端到端认知。建议按以下顺序继续：

1. **深入 pages 子系统**：下一篇（u3 单元）会逐文件讲解 `collect.rs`（分页符、奇偶校验、标签迁移）、`run.rs`（页面尺寸/边距/页眉页脚）、`finalize.rs`（组装与页号）、以及 `introspect.rs`（PagedIntrospector 如何建查询索引）。本讲的 4.2 三段式是它们的总纲。
2. **理解通用原语**：u2 单元会讲透 `Engine`/comemo 记忆化、`Regions`、`Frame`/`Fragment`、`Locator`/`Tag`。这些是 `layout_page_run` 内部调用 `layout_flow` 时真正用到的「排版词汇表」。
3. **进入 flow 与 inline**：`layout_page_run` 里排正文调用的 [src/pages/run.rs:190-202](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/run.rs#L190-L202) 的 `layout_flow`，是 u4（块级流）的入口；而 flow 内部又会调用 inline（u5）排段落。这样就从「文档级」一路下钻到「段落级」。
4. **阅读建议**：在进入下一篇前，回头重读 [src/pages/mod.rs:175-241](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/mod.rs#L175-L241) 的 `layout_pages`，确保你能对着代码复述三段式——它将是后续所有细化讲解的「锚点」。
