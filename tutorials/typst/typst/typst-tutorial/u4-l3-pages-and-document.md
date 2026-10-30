# 页面与文档：layout_document 与 PagedDocument

## 1. 本讲目标

上一讲我们看到 realize 把语义元素（heading、strong 等）改写为排版元素，输出「元素 + 生效样式链」的扁平列表 `Vec<Pair>`。本讲回答下一个问题：**这份扁平列表如何变成一页页带页边距、页眉页脚的成品页面，并最终汇总成一个文档对象？**

学完本讲，你应该能够：

1. 跟踪 `layout_document` 从被编译循环调用、执行根级 realize、到收集出 `PagedDocument` 的完整主干。
2. 说出 `collect → run（并行）→ finalize（串行）` 三步分工，以及**为什么**要拆成这三步（双面页边距依赖物理页码，必须等并行布局结束）。
3. 在源码中精确定位 `page(width/height/margin/flipped/...)` 每个参数被读取的位置，理解 `auto` 高度如何把「多页文档」退化成「单页无限长文档」。
4. 说出 `Page` 与 `PagedDocument` 的字段构成，以及 PDF/渲染导出 crate 如何消费它们。

## 2. 前置知识

- **Pair**：上一讲 realize 的输出单元，即 `(元素, 生效样式链)` 二元组。本讲的输入就是 `&mut [Pair]`。
- **Frame（帧）**：排版结果的二维容器，内部是一组放在指定坐标上的图元（文本 run、形状、图像等）。页面正文、页眉、页脚各自先布局成独立的 Frame，最后拼成整页 Frame。
- **Region（可用区域）**：排版算法能使用的剩余空间，由尺寸加两个「该轴是否有限」的布尔值组成。多个 Region 组成 `Regions`，flow 布局用它决定何时分页/分栏。本讲的关键是：**页面的版心就是传给 flow 的第一个 region**。
- **`Smart<T>`**：Typst 标准库中表达「`auto` 或具体值」的容器（`Smart::Auto` / `Smart::Custom(v)`）。`page(width: auto)` 之类的行为都靠它表达。
- **物理页码 vs 逻辑页码**：物理页码是纸张的实际序号（第 1、2、3…张）；逻辑页码是 `counter(page)` 的值，可以被 `#counter(page).update(3)` 之类改写。两者可能不一致，`Page` 结构里存的是逻辑页码。
- **comemo memoize**：第四单元第一讲讲过编译循环会反复调用排版直到内省收敛；`layout_document` 的内部实现带 `#[comemo::memoize]`，输入没变时（比如某一轮迭代中内容和样式都没变）可以直接复用上一轮结果。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [crates/typst-layout/src/lib.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/lib.rs) | 排版 crate 的模块声明与公开导出（`layout_document`、`Page`、`PagedDocument` 等） |
| [crates/typst-layout/src/pages/mod.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/mod.rs) | `pages` 是目录模块；本文件提供 `layout_document` / `layout_document_for_bundle` 入口与共享实现 `layout_document_common`、`layout_pages` |
| [crates/typst-layout/src/pages/collect.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/collect.rs) | 把 realize 输出的扁平 `Pair` 列表按分页符切成 `Run` / `Parity` / `Tags` 三类片段 |
| [crates/typst-layout/src/pages/run.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/run.rs) | `layout_page_run`：读取 PageElem 的全部页面参数、构造 region、调用 `layout_flow` 布局正文与页眉页脚 |
| [crates/typst-layout/src/pages/finalize.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/finalize.rs) | `finalize`：已知物理页码后拼装整页 Frame、维护页码计数器，产出 `Page` |
| [crates/typst-layout/src/document.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/document.rs) | `PagedDocument` 与 `Page` 的数据结构定义，以及 `Output` trait 实现 |
| [crates/typst/src/lib.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs) | 总装 crate 的编译循环：`T::create` 对分页目标就是 `layout_document` |
| [crates/typst-library/src/layout/page.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/layout/page.rs) | `PageElem`（即脚本里的 `page` 元素）的字段定义与默认值，本讲实践的「参数源头」 |

一个容易踩的坑：`pages.rs` 并不存在，`pages` 是一个**目录模块**（`pages/mod.rs` + 三个子文件），在 [crates/typst-layout/src/lib.rs:L13](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/lib.rs#L13) 中以 `mod pages;` 声明，公开符号经 [crates/typst-layout/src/lib.rs:L20-L24](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/lib.rs#L20-L24) 导出。

## 4. 核心概念与源码讲解

### 4.1 入口三兄弟：layout_document、layout_document_for_bundle 与共享实现

#### 4.1.1 概念说明

`layout_document` 是分页目标的**文档级排版入口**：吃进求值产物 `Content` 与样式链，吐出 `PagedDocument`。它的定位与 `layout_fragment`（下一讲的主角）不同——后者需要调用方给定 regions，而文档级排版**没有外部 regions**，因为可用空间完全由内容里的 `page` 配置决定，这正是函数文档注释强调的「regions are defined by the page configuration in the content and style chain」。

与之并存的还有 `layout_document_for_bundle`，服务于 bundle 编译场景。两者只差一件事：**Locator（定位器）的来源**——普通编译用 `Locator::root()` 从根开始编号，bundle 编译则由外部传入一个 locator，因为这个文档只是更大编译产物的一部分。其余逻辑完全共享。

另外要注意 `Engine` 的「拆包」模式：入口函数先把 `&mut Engine` 拆成若干 tracked 参数再调用内部 impl，这是为了让 `#[comemo::memoize]` 能把这些参数当作缓存键——直接传 `&mut Engine` 是没法哈希追踪的。

#### 4.1.2 核心流程

`layout_document` 的主干可以概括为：

```text
layout_document(engine, content, styles)
  └─ layout_document_impl        #[comemo::memoize]
       └─ layout_document_common  （与 bundle 版共享，不做记忆化）
            1. styles.to_map().outside()   ← 标记外部样式在页面级有效
            2. DocumentInfo / DocumentFormatOptions 收集元信息
            3. (routines.realize)(Document { info, options }, ...)
               ← 根级 realize，得到 Vec<Pair>（上一讲的内容）
            4. layout_pages(...)  → EcoVec<Page>
            5. PagedDocument::new(pages, info, options)
               ← 构造时顺带从 pages 构建 PagedIntrospector
```

而它被谁调用？第四单元第一讲讲过编译循环：对 `T = PagedDocument`，`T::create` 经 `Output` trait 的默认实现落到这里。所以**每轮内省迭代都会走一次 `layout_document`**（输入不变时被 memoize 短路）。

#### 4.1.3 源码精读

**入口与 memoize 包装**：[crates/typst-layout/src/pages/mod.rs:L34-L50](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/mod.rs#L34-L50) 定义公开入口 `layout_document`，它把 `engine.world`、`engine.library`、introspector、sink、route 逐一取出并转为 tracked 形式，转调内部实现。`#[typst_macros::time(name = "layout document")]` 让该函数被 `--timings` 计时覆盖。

内部实现 [crates/typst-layout/src/pages/mod.rs:L53-L76](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/mod.rs#L53-L76) 带 `#[comemo::memoize]`，并用 `Locator::root()` 作为定位根——这是普通编译与 bundle 编译的唯一差别点（bundle 版在 [crates/typst-layout/src/pages/mod.rs:L99-L125](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/mod.rs#L99-L125) 中把外部传入的 locator 包装成 `LocatorLink` 再使用）。规格里标注「bundle 相关行为待确认」，读源码后可以给出确切结论：**两个入口除 locator 来源外完全一致**，都汇入 `layout_document_common`。

**共享实现**：[crates/typst-layout/src/pages/mod.rs:L130-L176](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/mod.rs#L130-L176) 是真正的躯干，几个关键动作：

- [L154-L155](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/mod.rs#L154-L155)：`styles.to_map().outside()` 把外部样式标记为「页面之外」，使其在页面级查询仍然有效——这是 set/show 规则能作用于后续页面的前提。
- [L158-L162](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/mod.rs#L158-L162)：`DocumentInfo`（标题、作者等）与 `DocumentFormatOptions`（格式选项）从样式链填充。
- [L164-L171](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/mod.rs#L164-L171)：通过 `engine.library.routines.realize` 函数指针表执行**根级 realize**（跨 crate 接线方式见第二讲），`RealizationKind::Document` 带着可变的 info/options 供规则写入元信息，返回 `Vec<Pair>`。
- [L173-L175](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/mod.rs#L173-L175)：调 `layout_pages` 得到页面列表，最后 `PagedDocument::new` 收口。

**调用方**：[crates/typst/src/lib.rs:L139-L162](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L139-L162) 是编译循环，其中 [L157](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L157) 的 `document = T::create(&mut engine, &content, styles)?` 对分页目标就是 `layout_document`；随后 `constraint.validate` 判断内省是否收敛。对照 [crates/typst-layout/src/document.rs:L78-L94](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/document.rs#L78-L94) 的 `Output` 实现：`create()` 直接转调 `crate::layout_document`，`target()` 返回 `Target::Paged`。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：确认「编译循环 → Output::create → layout_document」这条链，并从计时输出里看到每轮迭代都包含一次 document layout。
2. **操作步骤**：
   - 用第 1 单元的方式从源码构建 CLI：`cargo run -p typst-cli -- compile --timings demo.typ`（`demo.typ` 任意，含一个 `#counter(page).display()` 与两三段文字即可触发内省迭代）。
   - 打开 `--timings` 生成的 Chrome Tracing JSON，搜索事件名 `layout document`。
3. **需要观察的现象**：出现 `iter(0)`、`iter(1)`… 命名的作用域，其中每个迭代作用域内有一个 `layout document` 事件。
4. **预期结果**：迭代次数 ≥ 2 的文档中 `layout document` 事件同样出现多次（输入未变的轮次可能被 memoize 短路，事件名是否出现以本地实测为准——**待本地验证**）。
5. 若无 `--timings` 输出文件，检查 CLI 帮助 `cargo run -p typst-cli -- compile --help` 确认参数拼写。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `layout_document` 入口函数本身不带 `#[comemo::memoize]`，而是拆出一个内部 impl？

**答案**：memoize 需要把所有影响结果的输入作为缓存键追踪。`Engine` 是聚合了 world/library/sink/route 等的可变结构，不能直接作为追踪参数；入口先把 Engine 拆成若干 tracked 值再调用 impl，impl 上的 `#[comemo::memoize]` 才能正确记录依赖、在重复编译时复用结果。

**练习 2**：`layout_document` 与 `layout_document_for_bundle` 的差异是什么？

**答案**：仅 locator 来源不同：前者固定用 `Locator::root()`（整篇文档就是编译产物本身），后者接受外部传入的 `Locator` 并以 `LocatorLink` 接入（该文档是更大 bundle 编译的一部分）。两者共享 `layout_document_common`，排版逻辑完全一致。

### 4.2 collect：把扁平元素流切成页面片段

#### 4.2.1 概念说明

realize 输出的是一条**扁平**的 flow 级元素流，其中混着普通内容与显式分页符（`#pagebreak()`）。而页面布局天然按「页」组织：两个分页符之间的内容构成一个**页面运行**，整个运行内所有页面共享同一套页面参数（同一份 `set page` 的效果）。`collect` 的工作就是把这条流切成分页器能理解的片段。

它产出三种片段（`Item` 枚举）：

| 片段 | 含义 |
| --- | --- |
| `Run(children, styles, locator)` | 一段连续内容，将被**并行**布局成零或多页 |
| `Parity(parity, styles, locator)` | 「把页码奇偶性调整到目标状态」的指令，对应 `#pagebreak(to: even)` |
| `Tags(children)` | 夹在页面之间的内省标签（如某元素的 start 标记），不能单独催生一页 |

`Parity` 为什么单独存在？因为「当前是第几页」只有等前面的 Run 全布局完才知道，而 Run 是并行布局的——奇偶性调整是**串行、事后**的决定。

#### 4.2.2 核心流程

```text
collect(children):
  staged_empty_page = true     # 空文档也要产出（至少）一页
  循环扫描 children:
    遇到 PagebreakElem:
      strong 且 staged_empty_page → 先补一个空 Run（空页）
      带 to: 奇偶目标 → 记录一个 Item::Parity
      非 boundary 分页符 → 其样式成为下一片的初始样式
      strong → staged_empty_page = true（后面没内容就以空页收尾）
    否则:
      向后截取到下一个分页符为止的连续段
      先把「跨过分页符仍未闭合的 start 标签」迁移到分页符之后
      若整段全是标签 → 记 Item::Tags，不生成页面
      否则 → 记 Item::Run，清除 staged_empty_page
  收尾: staged_empty_page 仍为真 → 补一个空 Run
```

#### 4.2.3 源码精读

**Item 枚举**：[crates/typst-layout/src/pages/collect.rs:L8-L19](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/collect.rs#L8-L19) 定义三种片段，注释说明了各自语义：Run 全部并行布局；Tags 会被前置到下一页开头（或附加到最后一页末尾）；Parity 因为需要具体页码只能在末尾串行处理。

**主扫描循环**：[crates/typst-layout/src/pages/collect.rs:L37-L66](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/collect.rs#L37-L66) 是分页符分支：L41 判断 `weak`（弱分页符在页首/页尾不产生空页）；L48-L51 处理 `to:` 奇偶目标；L58-L60 处理 boundary 分页符——它是页面 set 规则作用域结束时自动生成的「边界哨兵」，携带的是**规则之前**的样式，不应应用到可能出现的空页上。

**内容段分组**：[crates/typst-layout/src/pages/collect.rs:L67-L108](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/collect.rs#L67-L108) 截取连续非分页符内容。其中 [L93-L101](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/collect.rs#L93-L101) 是一个微妙的设计：**只剩标签时不为标签单独造页**（标签不该影响布局），记为 `Item::Tags` 延后安放；[L105-L107](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/collect.rs#L105-L107) 记录 Run 并取消 staged 空页。

**收尾**：[crates/typst-layout/src/pages/collect.rs:L111-L114](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/collect.rs#L111-L114) 冲刷 staged 空页——这就是「空文档也输出一页空白」的出处。

**标签迁移**：[crates/typst-layout/src/pages/collect.rs:L127-L164](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/collect.rs#L127-L164) 的 `migrate_unterminated_tags` 解决一个真实痛点：`show heading: it => pagebreak() + it` 会让 heading 的 start 标签落在分页符**之前**。若不迁移，内省器会认为该标题在上一页末尾开始。函数用稳定排序把「未闭合的 start 标签」挪到紧随其后的分页符之后（[L160](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/collect.rs#L160)）；注释还坦承排序写法不是最快但最不易出 bug，且不在热路径上。

#### 4.2.4 代码实践

1. **实践目标**：用弱/强分页符与奇偶分页符的文档行为验证 collect 的切片规则。
2. **操作步骤**（示例代码）：

   ```typst
   // parity.typ
   = 第一页
   #pagebreak(to: even)
   = 会从奇数页开始
   #lorem(80)
   ```

   编译：`cargo run -p typst-cli -- compile parity.typ parity.pdf`。再做一版把 `#pagebreak(to: even)` 换成 `#pagebreak(weak: true)` 对比。
3. **需要观察的现象**：`to: even` 版本在第一页（物理页码 1，奇数）后插入一张空白页，使标题从第 3 页开始；`weak: true` 版本紧跟其后不产生空页。
4. **预期结果**：页数分别为 3+ 与 2；PDF 阅读器中空白页可见。若行为不符，回到 collect.rs 的 L48-L51 与 run.rs 的 parity 分支核对。
5. 观察到的具体页数**待本地验证**（取决于正文 lorem 展开的实际长度）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Item::Tags` 不生成页面，而要延后安放？

**答案**：标签（TagElem）只是内省机制的定位锚点，设计原则是「标签永远不影响布局」。如果只剩标签也造一页，无标签的等价文档与有标签的文档页数就会不同，破坏该不变量。所以标签要么前置到下一页开头，要么附加到最后一页末尾。

**练习 2**：`staged_empty_page` 初始值为什么是 `true`？

**答案**：保证空文档（或全由弱分页符构成、无实际内容的文档）仍然产出至少一页。扫描结束后若它仍为真，就在收尾处补一个空 Run。

### 4.3 layout_page_run：页面参数读取与 region 分配

#### 4.3.1 概念说明

这是本讲的核心：`layout_page_run` 负责一段页面运行的布局。它做三件事：

1. **读参数**：从样式链读出 `PageElem` 的全部属性——宽、高、翻转、页边距、出血、填充色、页眉页脚、栏配置等。
2. **分配 region**：用「页面尺寸 − 页边距」构造版心区域，交给 `layout_flow` 布局正文；flow 会消耗一串等大的 region，每个 region 变成一页。
3. **布局边际**：页眉、页脚、背景、前景各自布局成独立 Frame。

注意一个刻意的设计：`layout_page_run` 的产物 `LayoutedPage` 是「就差页码的页面」——它**不含**最终整页 Frame，也不确定左右边距谁在内谁在外。原因：双面打印的 inside/outside 交换需要物理页码，而并行布局时页码未知。这个「欠下的信息」由 4.4 的 finalize 补齐。

`auto` 语义在这里落地：`width`/`height` 为 `auto` 时取 `Abs::inf()`（无限大），于是 `Regions` 在该轴上「无限」，flow 永远不会因为空间耗尽而分页——`height: auto` 的文档就是一张无限长的单页（文档示例大量使用它来动态伸缩）。

#### 4.3.2 核心流程

```text
layout_page_run(children, locator, initial):
  styles = Styles::root(children, initial)      # 从段内 set 规则提炼页面级样式
  width  = resolve(page.width)  ?? inf          # auto → 无限
  height = resolve(page.height) ?? inf
  size   = (width, height)；flipped 则交换 x/y
  min    = min(width, height)；非有限时回退 A4 宽度（供默认边距用）

  default_margin = (2.5/21) · min               # auto 边距：A4 恰为 2.5cm
  margin/bleed   = 各边 resolve 后 relative_to(size)
  fill/header/footer/background/foreground/numbering/binding … 逐一读取

  area = size − margin.sum_by_axis()            # 版心
  fragment = layout_flow(children, Regions::repeat(area, area 是否有限), 栏配置, FlowMode::Root)
                                            # ↑ 重复 region：每个 region 一页

  对 fragment 中每个 inner Frame:
    再布局 header（贴底）/footer（贴顶）/background/foreground（整页居中）
    打包成 LayoutedPage（无页码、无整页 Frame）
```

`Regions::repeat(area, expansion)` 的第二个参数是每轴「是否允许扩展（是否有限）」的布尔对——无限高页面对应 `(false, false)` 之外的情形需按 `area.map(Abs::is_finite)` 逐轴计算。

#### 4.3.3 源码精读

**LayoutedPage**：[crates/typst-layout/src/pages/run.rs:L27-L42](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/run.rs#L27-L42) 定义这个中间产物，结构体上方注释点明要害：只需知道确切页码即可定稿，**因为页边距可以依赖页码**。

**页面级样式提炼**：[crates/typst-layout/src/pages/run.rs:L100-L103](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/run.rs#L100-L103) 的 `Styles::root(children, initial)` 把运行内所有文档/页面级 set 规则归并成这一轮的页面样式——`set page` 改的是样式链，不是元素，参数全部从样式链读取。

**读宽高与翻转（本讲实践任务的锚点）**：[crates/typst-layout/src/pages/run.rs:L106-L112](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/run.rs#L106-L112)。`styles.resolve(PageElem::width).unwrap_or(Abs::inf())`——`resolve` 先把相对长度换算成绝对值，`unwrap_or(inf)` 正是 `auto` 的语义；随后 `flipped` 时交换宽高。[L114-L117](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/run.rs#L114-L117) 在两轴都无限时用 A4 宽度给默认边距一个合理基准。

**读页边距**：[crates/typst-layout/src/pages/run.rs:L119-L135](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/run.rs#L119-L135)。默认边距是 \(\frac{2.5}{21} \times \min(w, h)\)（[L120](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/run.rs#L120) 的 `Rel::<Length>::from((2.5 / 21.0) * min)`），A4（210mm 宽）恰好得到 2.5cm；未显式设置的边取该默认，随后 `.resolve(styles).relative_to(size)` 把相对值锚定到页面尺寸。bleed（出血，印刷裁切预留）同法读取。

**其余页面属性**：[crates/typst-layout/src/pages/run.rs:L137-L154](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/run.rs#L137-L154) 读取填充色、前景/背景/页眉/页脚、页眉上伸与页脚下伸、页码格式、补语（supplement）、装订方向（binding，未设置时按文字方向左起/右起推断）。

**页码放页眉还是页脚**：[crates/typst-layout/src/pages/run.rs:L156-L185](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/run.rs#L156-L185)。`numbering_marginal` 把 `counter(page)` 包装成 `CounterDisplayElem`（[L163-L168](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/run.rs#L163-L168)）；[L181-L185](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/run.rs#L181-L185) 按 `number-align` 的垂直分量决定它默认进页眉还是页脚（用户显式设置的 header/footer 优先）。

**版心与 flow 布局（region 分配的现场）**：[crates/typst-layout/src/pages/run.rs:L187-L202](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/run.rs#L187-L202)。`area = size - margin.sum_by_axis()` 得到版心；`Regions::repeat(area, area.map(Abs::is_finite))` 生成「尺寸恒定、要多少有多少」的 region 序列，`expand` 布尔按轴取「该轴是否有限」——auto 高度对应无限 region，flow 便不会分页。栏数、栏距、分隔线从样式读出（[L196-L199](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/run.rs#L196-L199)），`FlowMode::Root` 标记这是文档根 flow（下一讲展开）。

**边际布局与打包**：[crates/typst-layout/src/pages/run.rs:L204-L245](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/run.rs#L204-L245)。`layout_marginal` 闭包给内容包一层对齐后用 `crate::layout_frame` 布局到给定区域；循环里对每个 inner Frame 计算页眉区（`margin.top - header_ascent` 高）、页脚区、整页大小，组装出 `LayoutedPage`。

**参数源头**：脚本里的 `#set page(...)` 对应 [crates/typst-library/src/layout/page.rs:L54](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/layout/page.rs#L54) 的 `PageElem`。`paper`（[L59-L61](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/layout/page.rs#L59-L61)）标了 `#[external]`——它只是 `width`/`height` 的速记，不进元素数据；`width` 字段（[L81-L88](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/layout/page.rs#L81-L88)）的 `#[parse]` 显示 `paper` 参数会被换算成 width 传入，默认 A4 宽；`height`（[L97-L103](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/layout/page.rs#L97-L103)）默认 A4 高且文档明言 `auto` 时只能手动分页；`margin`（[L166-L168](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/layout/page.rs#L166-L168)）带 `#[fold]`，支持逐键叠加的字典写法。

#### 4.3.4 代码实践（本讲主打）

1. **实践目标**：亲手改变 `page(width/height/margin)`，观察页数与版心变化，并把每个现象对应到 run.rs 的具体行。
2. **操作步骤**（示例代码）：

   ```typst
   // page-lab.typ —— 三组参数，逐次取消注释做实验
   #set page(width: 15cm, height: 6cm, margin: 1cm)
   // #set page(width: 15cm, height: 6cm, margin: (x: 3cm, y: 0.5cm))
   // #set page(width: 15cm, height: auto, margin: 1cm)
   #lorem(120)
   ```

   依次编译（多页 PNG 需要带页码模板，见下方说明）：

   ```bash
   cargo run -p typst-cli -- compile page-lab.typ "page-lab{p}.png"
   ```

   > 注意：多页导出图片时输出路径必须包含 `{p}`（1 起）或 `{0p}`（0 起补零）模板，否则 CLI 会报错 "cannot export multiple images without a page number template ({p}, {0p}) in the output path"——这条报错来自 [crates/typst-cli/src/compile.rs:L557-L565](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/compile.rs#L557-L565)。输出 PDF（`page-lab.pdf`）则无此限制。
3. **需要观察的现象**：
   - 第 1 组：正文按 15cm × (6cm − 2cm) 的版心分页，页数最多；
   - 第 2 组：左右边距变大，每行容纳字符变少，页数增加；
   - 第 3 组：`height: auto` 后全部内容落在**一张极长的图**里（`Abs::inf()` 的效果）。
4. **预期结果**：三组页数满足「第 2 组 ≥ 第 1 组 ≥ 第 3 组 = 1」。随后打开 run.rs 对照：宽高读取在 [L106-L108](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/run.rs#L106-L108)，auto 边距在 [L119-L127](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/run.rs#L119-L127)（可用 `15cm·(2.5/21) ≈ 1.79cm` 手算验证第 1 组省略 margin 时的默认值），版心计算在 [L188-L194](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/run.rs#L188-L194)。精确页数**待本地验证**（lorem 展开文本随版本可能不同）。
5. 附加实验：加 `flipped: true` 观察 [L110-L112](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/run.rs#L110-L112) 的宽高交换效果（页面变横版）。

#### 4.3.5 小练习与答案

**练习 1**：`#set page(height: auto)` 的文档为什么只有一页？

**答案**：run.rs [L107-L108](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/run.rs#L107-L108) 把 auto 解析成 `Abs::inf()`，随后 [L194](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/run.rs#L194) `Regions::repeat` 的 expand 标记该轴为「无限」，flow 布局时剩余高度永不耗尽，不会触发自动分页，所有内容进入唯一一页。

**练习 2**：默认页边距是多少？为什么 A4 恰好是 2.5cm？

**答案**：默认边距是页面较短边的 \( \frac{2.5}{21} \) 倍（[L120](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/run.rs#L120)）。A4 宽 210mm，\( 210 \times \frac{2.5}{21} = 25 \)mm，即 2.5cm。两轴均无限（auto/auto）时以 A4 宽度为基准（[L114-L117](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/run.rs#L114-L117)）。

**练习 3**：`LayoutedPage` 为什么不直接存整页 Frame，而把拼装推迟到 finalize？

**答案**：双面打印时左右边距要按「inside/outside + 装订方向 + 物理页码的奇偶」交换（见 finalize 的 swap 逻辑），而页面运行是并行布局的，布局完成前不知道每页的物理页码。所以 run 阶段只产出 inner Frame 与四个边际 Frame，页码相关信息留给串行的 finalize 处理。

### 4.4 finalize 与 Page/PagedDocument：成品页与文档数据形态

#### 4.4.1 概念说明

`finalize` 把 `LayoutedPage` 变成 `Page`：交换双面边距、把背景→页眉→正文→页脚→前景按固定顺序压入整页 Frame、维护页码计数器。`layout_pages` 主循环按 collect 出的片段顺序串行调用它，最终把所有 `Page` 汇入 `PagedDocument`。

`PagedDocument` 是分页排版阶段的**最终产物**，也是所有分页导出器（PDF、PNG 渲染、SVG）的输入。它除了页面列表还带三样东西：文档元信息（`DocumentInfo`）、格式选项、以及由页面派生的 `PagedIntrospector`——后者正是编译循环判断收敛所用的内省器。

#### 4.4.2 核心流程

```text
layout_pages:
  items = collect(children)
  runs  = engine.parallelize(所有 Item::Run → layout_page_run)   # 并行
  for item in items:                                              # 串行收尾
    Run     → 逐页 finalize（页码计数器驱动）
    Parity  → 当前页数奇偶不匹配 → layout_blank_page + finalize
    Tags    → 暂存，前置到下一页或附加到最后一页末尾
  → EcoVec<Page>

finalize(LayoutedPage) -> Page:
  依 binding 与物理页码决定是否交换 left/right 边距与出血
  frame = Frame::hard(inner.size + margin)
  依序压入: background(带出血原点) → header → inner → footer → foreground
  counter.visit(frame)   # 应用页内 counter(page).update
  number = counter.logical(); counter.step()
  → Page { frame, bleed, fill, numbering, supplement, number }
```

#### 4.4.3 源码精读

**并行 + 串行的编排**：[crates/typst-layout/src/pages/mod.rs:L178-L245](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/mod.rs#L178-L245) 的 `layout_pages`。[L188-L199](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/mod.rs#L188-L199) 用 `engine.parallelize` 把所有 Run 并发布局；[L207-L234](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/mod.rs#L207-L234) 的循环再按原顺序串行取结果并 finalize——注意 Run 的结果按提交顺序 `runs.next()` 取出，保证页序确定。`Item::Parity` 在 [L216-L224](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/mod.rs#L216-L224) 处理：奇偶已匹配则跳过，否则用 [run.rs:L45-L52](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/run.rs#L45-L52) 的 `layout_blank_page` 现造一张空白页。尾部 tags 处理在 [L236-L242](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/mod.rs#L236-L242)：附加到最后一页底部（y 取页高，即「逻辑末尾」位置）。

**finalize 的双面交换**：[crates/typst-layout/src/pages/finalize.rs:L32-L41](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/finalize.rs#L32-L41)。文件头注释（[L9-L11](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/finalize.rs#L9-L11)）再次点题：inside/outside 边距需要物理页码，并行布局阶段不可知。左装订在偶数页交换、右装订在奇数页交换。

**整页拼装顺序**：[crates/typst-layout/src/pages/finalize.rs:L43-L73](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/finalize.rs#L43-L73)。`Frame::hard` 创建硬尺寸帧；压入顺序为 background（原点带负出血偏移，允许内容溢出裁切线）→ header → inner（偏移 margin.left/top）→ footer（贴页底）→ foreground。注释提醒：这个顺序影响内省元素的相对次序、进而影响 counter 的解析结果，不可随意调整。

**页码计数**：[crates/typst-layout/src/pages/finalize.rs:L75-L82](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/finalize.rs#L75-L82)。`counter.visit` 扫描页内的 counter 更新（例如 `#counter(page).update(7)`），随后取**逻辑**页号写入 `Page.number` 并 `step()`。这就是物理页码与逻辑页码分离的实现处。

**Page 结构**：[crates/typst-layout/src/document.rs:L96-L120](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/document.rs#L96-L120)。六个字段：`frame`（整页内容）、`bleed`（出血，不含在 frame 内）、`fill`（背景填充，`Smart` 三态：None 透明 / Auto 按目标而定 / 具体 Paint）、`numbering`（页码格式）、`supplement`（页码补语）、`number`（逻辑页号）。[L122-L135](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/document.rs#L122-L135) 的两个辅助方法 `fill_or_transparent`（PDF 用：Auto 视为透明）与 `fill_or_white`（位图/SVG 用：Auto 视为白底）体现了「同一份 Page，不同导出器不同解释」。

**PagedDocument 结构**：[crates/typst-layout/src/document.rs:L16-L23](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/document.rs#L16-L23)。四字段：`pages`、`info`、`options`、`introspector`。构造函数 [L25-L41](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/document.rs#L25-L41) 在 `new` 时自动从 pages 构建 `PagedIntrospector`——所以每轮迭代的内省器天然来自上一轮的页面产物（第七讲展开）。[L59-L66](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/document.rs#L59-L66) 的 `Hash` 实现刻意不哈希 introspector，注释说明它完全由 pages 派生——这正是 comemo 缓存键的正确性来源。[L68-L76](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/document.rs#L68-L76) 实现 `Document` trait 暴露 info/options 给导出器（CLI 的 PNG 选项解析在 [crates/typst-cli/src/compile.rs:L499-L501](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/compile.rs#L499-L501) 就经 `document.options()` 读取）。

**导出器如何消费**：CLI 的 `export_image`（[crates/typst-cli/src/compile.rs:L493-L511](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-cli/src/compile.rs#L493-L511)）遍历 `document.pages()`，把每个 `&Page` 交给 `typst_render::render(page, &options)` 栅格化。PDF 导出同理只依赖 `PagedDocument`/`Page` 抽象（下一单元详讲）。

#### 4.4.4 代码实践

1. **实践目标**：观察双面边距交换与 `fill: auto` 在不同导出目标下的差异。
2. **操作步骤**（示例代码）：

   ```typst
   // two-sided.typ
   #set page(
     "a5", flipped: false,
     margin: (inside: 3cm, outside: 1cm),
     numbering: "1",
   )
   #lorem(150)
   ```

   分别导出 PDF 与 PNG：

   ```bash
   cargo run -p typst-cli -- compile two-sided.typ two-sided.pdf
   cargo run -p typst-cli -- compile two-sided.typ "two-sided{p}.png"
   ```
3. **需要观察的现象**：奇数页宽边距在左（inside），偶数页宽边距在右——finalize 的 swap 在起作用；每页页脚居中出现页码（`numbering: "1"` 生成的 `CounterDisplayElem`）。
4. **预期结果**：偶数页镜像翻转的边距清晰可见；把 `inside/outside` 换成 `left/right` 则不随页码交换。PNG 是白底而 PDF 背景透明（`fill_or_white` vs `fill_or_transparent`），可用支持透明通道的看图工具对比 PDF 的渲染。**待本地验证**。
5. 源码对照：swap 逻辑 [finalize.rs:L32-L41](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/finalize.rs#L32-L41)；页码元素生成 [run.rs:L156-L177](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/run.rs#L156-L177)。

#### 4.4.5 小练习与答案

**练习 1**：`Page.fill` 是 `Smart<Option<Paint>>`，一共几种状态？各是什么含义？

**答案**：三种：`Smart::Custom(None)` 显式透明；`Smart::Custom(Some(paint))` 指定填充色；`Smart::Auto` 未指定——PDF 导出按透明处理（`fill_or_transparent`），位图/SVG 导出按白色处理（`fill_or_white`）。

**练习 2**：为什么 `PagedDocument` 的 `Hash` 不哈希 `introspector` 字段？

**答案**：introspector 完全由 pages 派生（`PagedDocument::new` 里构建），哈希 pages 已覆盖其全部信息；额外哈希派生数据既浪费又可能在结构变化时引入不一致。由于该 Hash 是 comemo 缓存键的一部分，正确性依赖「派生数据不入键」。

**练习 3**：`#counter(page).update(8)` 会让 `Page.number` 变成什么？

**答案**：从下一页起 `Page.number` 为 8（finalize 中 `counter.visit` 先应用页内的 update，再 `counter.logical()` 取号）。但物理页数（`pages.len()`、PDF 中的纸张序号）不受影响——这正是逻辑/物理页码分离。

## 5. 综合实践

设计一份「页面实验室」文档，把本讲三个机制串起来验证：

```typst
// lab.typ
#set page(width: 14cm, height: 8cm, margin: (x: 2cm, y: 1.5cm),
          numbering: "1 / 1", header: [我的页面实验室], columns: 2)

= 第一章
#lorem(60)
#pagebreak(to: odd)
= 第二章（强制奇数页开始）
#counter(page).update(10)
当前逻辑页码：#context counter(page).display()
```

任务步骤：

1. 编译为 PDF 与带 `{p}` 模板的 PNG，记录总页数、每页版心尺寸、页眉/页脚位置。
2. 解释三个现象对应的源码：第二章从奇数页开始（collect.rs 的 `Item::Parity` + mod.rs 的空白页分支）；`counter(page).update(10)` 后页脚显示的页码跳变但 PDF 纸张序号不变（finalize.rs 的 `counter.visit` + `Page.number` 取逻辑值）；两栏正文（run.rs 读取 `PageElem::columns` 后传入 `layout_flow` 的 `ColumnOptions`）。
3. 把 `height: 8cm` 改成 `height: auto` 再编译，验证页数塌缩为 1，并用自己的话解释 `Regions::repeat(area, area.map(Abs::is_finite))` 中 expand 标记的作用。
4. 写一段 200 字的总结：为什么页面布局必须拆成「并行 run + 串行 finalize」两阶段？

预期：能完整复述「奇偶调整与双面边距都依赖串行已知的物理页码」这一核心动机。具体页数与栏内断行效果**待本地验证**。

## 6. 本讲小结

- `layout_document` 是分页目标的文档级排版入口，经 `Output::create` 被编译循环每轮调用；它与 `layout_document_for_bundle` 只差 locator 来源，共享 `layout_document_common`：外部样式标记 outside → 填充元信息 → 根级 realize → `layout_pages` → `PagedDocument::new`。
- `pages` 是目录模块，页面布局分三步：`collect` 把扁平 Pair 流切成 `Run`/`Parity`/`Tags` 片段；`layout_page_run` **并行**布局各 Run；`finalize` **串行**定稿每页。
- `layout_page_run` 从样式链读取 PageElem 全部参数：`width`/`height` 的 `auto` 解析为 `Abs::inf()`，默认边距为短边的 2.5/21（A4 恰为 2.5cm），版心 = 页面尺寸 − 边距，以 `Regions::repeat` 交给 `layout_flow`。
- `finalize` 用物理页码交换双面边距，按 background → header → inner → footer → foreground 的固定顺序拼装整页 Frame，并用 `ManualPageCounter` 区分物理/逻辑页码。
- `Page` 六字段（frame/bleed/fill/numbering/supplement/number），`PagedDocument` 四字段（pages/info/options/introspector）；introspector 由 pages 派生且不入 Hash，导出器只依赖这两个抽象。

## 7. 下一步学习建议

下一讲（u4-l4「流式布局」）进入 `layout_flow` 内部：本讲把版心作为 region 序列交给了它，接下来要看 flow 如何把块级内容逐个塞进 region、何时判定「当前页放不下」并翻页，以及 `layout_fragment`/`layout_frame` 的分工。建议先读 [crates/typst-layout/src/flow/mod.rs](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/flow/mod.rs) 的模块文档。若对内省收敛感兴趣，可提前跳到第七讲，看 `PagedDocument::new` 里构建的 introspector 如何回流进编译循环。
