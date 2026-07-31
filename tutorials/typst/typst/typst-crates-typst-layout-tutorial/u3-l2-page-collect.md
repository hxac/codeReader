# 页面收集：分页符、奇偶校验与标签

## 1. 本讲目标

上一讲（u3-l1）我们走到了 `layout_pages` 的入口：realize 已经把任意 Content 展平成一个扁平的 `Vec<Pair>`（每个 `Pair` 是「已打包元素 + 样式链」），现在要把这串内容切成可以**并行排版**的页面单元。

本讲聚焦 `layout_pages` 的第一步——[collect](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/collect.rs#L23-L117)。它是纯「切分逻辑」，不画任何东西，只决定「哪些内容属于同一页、哪里需要补空页、哪里需要补奇偶页、标签该挂到哪一页」。

学完后你应当能够：

- 说清 `collect` 把扁平 `children` 切成的三类 `Item`（`Run`/`Tags`/`Parity`）各自代表什么、由谁消费。
- 区分 `PagebreakElem` 的三种页面符：普通（strong）、`weak`、`boundary`，并理解 `staged_empty_page` 这个状态机如何驱动空页的「暂存与刷出」。
- 理解 `Parity`（奇偶补页）为何只能放在 collect 末尾**串行**处理，且必须在已知物理页号时才能决定是否真的补页。
- 理解 `migrate_unterminated_tags` 为何要把分页符前「未终止的 start tag」迁到分页符之后，以及 `Item::Tags` 为何要把「只剩标签」的碎片单独暂存。

---

## 2. 前置知识

本讲假设你已经掌握 u3-l1 的内容，尤其是：

- **Pair**：realize 的扁平产物，类型是 `(&'a Content, StyleChain<'a>)`（见 [typst-library/src/routines.rs:195-196](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/routines.rs#L195-L196)），即「一个已打包元素 + 作用于它的样式链」。
- **collect / 并行 / finalize 三段式**：`layout_pages` 先 `collect` 切分，再 `engine.parallelize` 对每个 page run 并行排版，最后 `finalize` 串行依赖物理页号组装（见 [pages/mod.rs:182](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/mod.rs#L182)）。
- **Tag 与内省定位**（u2-l4）：`Tag::Start`/`Tag::End` 是「不可见但有序」的 FrameItem，随内容流落入 frame，用来给 query/counter/label/outline 提供位置。本讲的「标签迁移」就是为了修正这些 Tag 跨分页符时的页号归属。

补充一个通俗比喻：你可以把 `children` 想象成一长串已经折好但还没贴到纸上的内容卡片，中间穿插着「在此处撕开换页」的指令卡。`collect` 的工作就是把这串卡片按撕开指令切成一摞摞「同一页的卡片」，并额外记下「这里可能要补一张空纸」「这里有几张隐形标签卡要先留着」。

---

## 3. 本讲源码地图

本讲只涉及一个核心文件，但它会反复引用两个兄弟 crate 的类型：

| 文件 | 角色 |
| --- | --- |
| [src/pages/collect.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/collect.rs) | **主角**。定义 `Item` 枚举与 `collect` 主函数、`migrate_unterminated_tags` 辅助函数。 |
| [src/pages/mod.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/mod.rs) | `layout_pages` 在这里**消费** `collect` 产出的 `Item`：并行排 Run、串行处理 Parity 与 Tags（L175-L241）。 |
| typst-library/.../layout/page.rs | 定义被 collect 解析的输入类型 `PagebreakElem`（L553-L579）与 `Parity`（L799-L813）。它们在 typst-library，typst-layout 只负责消费。 |

记住一条主线：**`collect` 是纯函数式的「切片 + 记账」，真正的排版（画 frame）发生在它之后。**

---

## 4. 核心概念与源码讲解

### 4.1 `Item` 三种产物：collect 的输出契约

#### 4.1.1 概念说明

`collect` 不返回 frame，也不返回 page，它返回一个 `Vec<Item>`。每个 `Item` 是一条「给后续 `layout_pages` 的指令」。理解这三类指令，就理解了 collect 的全部输出：

- **`Run`**：一段连续的、属于同一页（或同一组页）的内容。这是唯一会被**并行排版**的东西。
- **`Tags`**：一段「只剩下隐形标签、没有任何可见内容」的碎片。它不该单独占一页，所以被暂存，留到下一页开头或文档末尾再贴。
- **`Parity`**：一条「可能需要在此时插入一张空页，使下一个真实页落在奇数页或偶数页」的指令。它**不能并行**，因为是否补页取决于此时已经排出了多少页（物理页号）。

#### 4.1.2 核心流程

```text
扁平 children（Pair 列表）
        │  collect 切分 + 记账
        ▼
Vec<Item>  ──┬── Item::Run(...)     → 并行 layout_page_run
             ├── Item::Tags(...)    → 暂存到 tags，贴到下一页起始 / 文档末尾
             └── Item::Parity(...)  → 串行：按当前 pages.len() 决定是否补空页
```

注意三者的**时序差异**：`Run` 在 collect 之后立刻全部并行；`Tags` 和 `Parity` 则在 `layout_pages` 的串行 for 循环里、与各 `Run` 的 finalize **交错**处理（见 4.4.3）。

#### 4.1.3 源码精读

`Item` 枚举本身只有三个变体，注释把每个变体「由谁消费」讲得很清楚：

[pages/collect.rs:8-19](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/collect.rs#L8-L19) —— 定义三类指令：

```rust
pub enum Item<'a> {
    /// A page run containing content. All runs will be layouted in parallel.
    Run(&'a [Pair<'a>], StyleChain<'a>, Locator<'a>),
    /// Tags in between pages. These will be prepended to the first start of
    /// the next page, or appended at the very end of the final page ...
    Tags(&'a [Pair<'a>]),
    /// An instruction to possibly add a page to bring the page number parity to
    /// the desired state. Can only be done at the end, sequentially, ...
    Parity(Parity, StyleChain<'a>, Locator<'a>),
}
```

要点：

- `Run` 携带 `(&[Pair], StyleChain, Locator)`：要排的内容切片、该页的初始样式、该页的定位器。
- `Tags` 只携带 `&[Pair]`：这些 Pair 全是 `TagElem`，没有样式与定位器（标签自带 location）。
- `Parity` 携带 `(Parity, StyleChain, Locator)`：期望的奇偶、补页用的样式、补页用的定位器。

#### 4.1.4 代码实践

**实践目标**：建立「collect 输出 = 指令列表」的直觉。

**操作步骤**：

1. 打开 [pages/mod.rs 的 layout_pages](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/mod.rs#L175-L241)。
2. 找到 `let items = collect(...)`（L182），再找到紧接着的 `engine.parallelize(...)`（L185）。
3. 注意 `parallelize` 的输入是 `items.iter().filter_map(...)`，**只挑出 `Item::Run`**（L186-L191），其余两类被过滤掉。

**需要观察的现象**：`Item::Tags` 和 `Item::Parity` 根本不进 `parallelize`，证明它们不是「可并行排版的内容」，而是「串行记账指令」。

**预期结果**：你能用自己的话说出「为什么只有 Run 能并行」——因为 Tags/Parity 的处理都依赖串行的全局状态（已暂存的标签、已排出的页数）。

#### 4.1.5 小练习与答案

**练习 1**：`Item::Run` 里的 `Locator` 在 `layout_pages` 中被怎样使用？

**答案**：在 [pages/mod.rs:188](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/mod.rs#L188) 处调用 `locator.relayout()`，把该页的定位器以「复用同一身份、用于测量」的模式传给 `layout_page_run`。relayout 模式保证多轮内省收敛时同一页拿到稳定的 Location（u2-l4）。

**练习 2**：为什么 `Item::Tags` 不携带 `StyleChain` 和 `Locator`？

**答案**：因为标签本身已自带 `Location`（见 u2-l4 的 `Tag`），且纯标签碎片不产生可见内容、不需要页面级样式来排版。它只需要在 finalize 时被原样塞进某个 frame，所以只保留了 `&[Pair]`。

---

### 4.2 PagebreakElem 与 staged_empty_page：空页的状态机

#### 4.2.1 概念说明

`collect` 最核心的状态是一个布尔变量 `staged_empty_page`。它表达「**当前是否已经暂存了一张待写的空页**」。这个状态加上 `PagebreakElem` 的三种形态，共同决定了「什么时候真的插入一张空白页」。

`PagebreakElem`（在 typst-library）有三个关键字段，强度从强到弱：

| 字段 | 默认 | 含义 |
| --- | --- | --- |
| `weak` | `false` | 为 `true` 时，若当前页已空则**跳过**这张页面符（不强制空页）。 |
| `to: Option<Parity>` | `None` | 给定时，要求下一页落在指定奇偶（必要时补空页）。 |
| `boundary` | `false`（内部） | 「边界页面符」：比 weak 更弱，**不仅不强制空页，也不把自身样式盖到暂存空页上**。 |

源码定义见 [typst-library/.../page.rs:553-579](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/page.rs#L553-L579)。其中 strong（普通）= `weak` 为 `false`。

通俗理解：

- **strong pagebreak**（`#pagebreak()`）：无条件换页，之后还要暂存一张空页。
- **weak pagebreak**（`weak: true`）：只有当前已有内容时才换页；若当前页本来就是空的，就什么都不做。
- **boundary pagebreak**（`boundary: true`，内部生成）：scope 结束时保证一个页边界，但它「尽量隐身」，连样式都不愿强加。

#### 4.2.2 核心流程

`staged_empty_page` 的状态机：

```text
初始：staged_empty_page = true   （文档一开始就「暂存」了一张待写的第一页）

遇到 strong pagebreak 且当前已暂存空页  → 把空页真正落盘为 Item::Run(&[])
遇到任何 strong pagebreak               → staged_empty_page = true（之后再暂存一张）
遇到真实内容（非纯标签的 Run）          → staged_empty_page = false（空页被内容「占用」）
循环结束若仍 staged                      → flush 一张 Item::Run(&[]) 作为末尾空页
```

关键直觉：**strong pagebreak 不直接产出空页，而是「预约」一张**。是否真正落盘，取决于「预约时是否已经有一张暂存空页在排队」。这就避免了连续两个 `#pagebreak()` 产生两张空页的尴尬。

#### 4.2.3 源码精读

先看状态初始化（注释强调「为真表示末尾要补一张空页」）：

[pages/collect.rs:30-31](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/collect.rs#L30-L31) —— 初始暂存一张空页：

```rust
// When this is true, an empty page should be added to `pages` at the end.
let mut staged_empty_page = true;
```

进入 pagebreak 分支后，三步处理（[pages/collect.rs:38-66](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/collect.rs#L38-L66)）：

```rust
let strong = !pagebreak.weak.get(styles);
if strong && staged_empty_page {
    let locator = locator.next(&elem.span());
    items.push(Item::Run(&[], initial, locator));   // ① 真正落盘一张空页
}

if let Some(parity) = pagebreak.to.get(styles) {
    let locator = locator.next(&elem.span());
    items.push(Item::Parity(parity, styles, locator)); // ② 记一条奇偶补页指令（见 4.3）
}

if !pagebreak.boundary.get(styles) {
    initial = styles;                                  // ③ 非 boundary：把样式更新为「本页符的样式」
}

staged_empty_page |= strong;                            // ④ strong → 之后再暂存一张空页
```

逐步解读：

- **①** `strong && staged_empty_page`：只有「强制换页」且「确实有一张暂存空页在排队」时，才把那张空页落盘为 `Item::Run(&[])`（空内容）。如果前面已有真实内容（`staged_empty_page=false`），这里就不额外加空页——因为换页本身已经由「下一段内容开新 Run」隐式完成。
- **③** `initial` 是「下一页的起始样式」。普通/weak 页面符会把自身样式传给后续页面；但 **boundary** 不会。注释（[L54-L60](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/collect.rs#L54-L60)）解释：boundary 页面符是在某个 `set page` 规则的 scope 末尾生成的，它的样式其实是「set page 之前」的样式，不该盖到潜在空页上。
- **④** `staged_empty_page |= strong`：任何 strong 页面符之后都重新暂存一张空页，等待后续内容去「占用」它，或在循环末尾被 flush。

真实内容分支则把暂存空页「占用」掉（[pages/collect.rs:105-107](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/collect.rs#L105-L107)）：

```rust
let locator = locator.next(&elem.span());
items.push(Item::Run(group, initial, locator));
staged_empty_page = false;   // 有真实内容了，暂存空页被占用
```

最后，循环结束后若仍暂存，flush 一张空页（[pages/collect.rs:112-114](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/collect.rs#L112-L114)）——这保证了「即使文档以 pagebreak 结尾，也会有一张尾部空页」以及「完全空文档也至少有一页」。

> 关于 boundary 页面符的来源：`PagebreakElem::shared_boundary()` 返回一个 `weak=true, boundary=true` 的全局单例（见 [typst-library/.../page.rs:587-593](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/page.rs#L587-L593)），由 realize 在页面 scope 边界注入。

#### 4.2.4 代码实践

**实践目标**：亲手追踪 `staged_empty_page` 在连续两个 `#pagebreak()` 下的变化，验证「不会产生两张空页」。

**操作步骤**（源码阅读型实践）：

考虑 children 序列 `[正文A, #pagebreak(), #pagebreak(), 正文B]`（两个都是 strong）。在纸上按 [collect.rs:38-66](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/collect.rs#L38-L66) 逐步填表：

| 步骤 | 当前 child | strong? | staged(前) | ①落盘空页? | ④staged(后) | 说明 |
| --- | --- | --- | --- | --- | --- | --- |
| 0 | — | — | true | — | — | 初始暂存一张 |
| 1 | 正文A | — | true | — | false | Run(A)，占用暂存空页 |
| 2 | 第1个 pb | true | false | 否 | true | 无暂存可落盘；之后暂存一张 |
| 3 | 第2个 pb | true | true | **是** → Run(&[]) | true | 把步骤2暂存的空页落盘；之后再暂存一张 |
| 4 | 正文B | — | true | — | false | Run(B)，占用步骤3暂存的空页 |
| 5 | 循环结束 | — | false | — | — | 不 flush |

**需要观察的现象**：步骤 3 落盘的 `Item::Run(&[])` 就是两个连续 `#pagebreak()` 之间产生的那**唯一一张**空页。

**预期结果**：最终 items 大致为 `[Run(A), Run(&[]), Run(B)]`，即 A、空页、B 三页——而不是两张空页。这正是 `staged_empty_page` 状态机的作用。

#### 4.2.5 小练习与答案

**练习 1**：如果把第一个 `#pagebreak()` 换成 `weak: true`（即 `#pagebreak(weak: true)`），上表会怎样变化？

**答案**：步骤 2 变成 weak，`strong=false`。于是 ① 不触发、④ 不触发（`staged_empty_page |= false` 保持 false）。步骤 3 的第二个 strong pb 此时 `staged(前)=false`，① 仍不落盘，但 ④ 把 staged 置 true。最终 items 约为 `[Run(A), Run(B)]`——weak pb 紧跟在已有内容后什么都没做，符合「当前页非空才换页」的语义。

**练习 2**：`boundary` 页面符为何要在 ③ 处跳过 `initial = styles`？

**答案**：boundary 是某个 `set page` scope 末尾由 realize 注入的「保底换页」，它携带的样式是 set page **之前**的样式。若把它盖到暂存空页上，会让那张空页错误地采用旧样式。所以 boundary 既不强制空页（因为它同时是 weak），也不强加样式（③ 跳过）。详见 [collect.rs:53-60](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/collect.rs#L53-L60) 的注释。

---

### 4.3 Parity 奇偶补页：collect 产出，layout_pages 消费

#### 4.3.1 概念说明

`#pagebreak(to: "odd")`（或 `"even"`）表示「**确保下一段内容从一个奇数（或偶数）页开始**，必要时在前面补一张空页」。这是图书排版里常见的「章节总是从奇数页开始」需求。

`Parity` 是个极简枚举（[typst-library/.../page.rs:799-804](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/page.rs#L799-L804)），只有 `Even`/`Odd` 两个变体。它的核心方法是 `matches(number)`：

[typst-library/.../page.rs:806-813](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/page.rs#L806-L813):

```rust
pub fn matches(self, number: usize) -> bool {
    match self {
        Self::Even => number % 2 == 0,
        Self::Odd => number % 2 == 1,
    }
}
```

注意这里的 `number` 是 **0 基的页数计数**（`pages.len()`），而 `Odd` 对应 `number % 2 == 1`——这暗示了它与「物理页号」之间有个精巧的偏移关系（见 4.3.3）。

#### 4.3.2 核心流程

```text
collect 阶段：遇到带 to 的页面符 → 无条件 push 一条 Item::Parity（不判断页号）
                                   （此时根本不知道物理页号）

layout_pages 阶段（串行 for 循环）：
   处理到 Item::Parity(parity) 时，pages.len() = 已排出的页数
   ├─ parity.matches(pages.len()) 为真  → 下一页(pages.len()+1)奇偶不对 → 补一张空页
   └─ parity.matches(pages.len()) 为假  → 下一页奇偶已对 → 跳过，不补页
```

为什么必须串行、必须延后？因为**是否需要补空页取决于在此之前一共排了多少页**，而这个数字只有在 finalize 推进 `counter` 之后才知道。这正是 `Item::Parity` 注释里「Can only be done at the end, sequentially」的原因。

#### 4.3.3 源码精读

collect 里，只要 `to` 存在就无条件记一条 Parity 指令（[pages/collect.rs:48-51](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/collect.rs#L48-L51)），与 strong/weak 无关：

```rust
if let Some(parity) = pagebreak.to.get(styles) {
    let locator = locator.next(&elem.span());
    items.push(Item::Parity(parity, styles, locator));
}
```

真正的「是否补页」判断在 `layout_pages` 的串行循环里（[pages/mod.rs:212-220](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/mod.rs#L212-L220)）：

```rust
Item::Parity(parity, initial, locator) => {
    if !parity.matches(pages.len()) {
        continue;   // 下一页奇偶已对，无需补页
    }
    let layouted = layout_blank_page(engine, locator.relayout(), *initial)?;
    let page = finalize(engine, &mut counter, &mut tags, layouted)?;
    pages.push(page);
}
```

**关键的偏移推导**：处理 Parity 时，已排出的页数是 `pages.len()`，那么「下一段内容将要落在的页」是第 `pages.len() + 1` 页（1 基物理页号）。代码用 `parity.matches(pages.len())` 来决定是否补页——为什么用 `pages.len()` 而不是 `pages.len()+1`？

因为 `pages.len()` 与 `pages.len()+1` 的奇偶**相反**：

- 当 `parity.matches(pages.len())` 为 **真**（即 `pages.len()` 已是期望奇偶）⟺ `pages.len()+1`（下一页）奇偶**不对** ⟺ **需要**补一张空页把它顶到正确奇偶。
- 当 `parity.matches(pages.len())` 为 **假** ⟺ 下一页奇偶**已对** ⟺ `continue` 不补页。

补页用的是 `layout_blank_page`，它其实就是对空内容跑一次 `layout_page_run` 并取第一帧（[pages/run.rs:46-53](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/run.rs#L46-L53)），所以空页也会正常带页眉页脚、页码。

#### 4.3.4 代码实践

**实践目标**：用文档注释里的官方例子验证补页逻辑。

**操作步骤**（源码阅读型实践，可本地编译验证）：

例子来自 [page.rs:562-568](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/page.rs#L562-L568):

```typst
#set page(height: 30pt)
First.
#pagebreak(to: "odd")
Third.
```

按 collect → layout_pages 手动追踪：

1. collect 产出 items ≈ `[Run(First.), Parity(Odd), Run(Third.)]`（First. 是正文，staged 翻为 false；页面符 strong 但 staged 已 false 故不落盘空页；to=Odd 故记 Parity；Third. 占用暂存空页）。
2. layout_pages 串行循环：
   - `Run(First.)` → finalize → **page 1**，`pages.len()` 变为 1。
   - `Parity(Odd)` → `Odd.matches(1)` = `1%2==1` = **真** → 不 continue → 补一张空页 → **page 2**，`pages.len()` 变为 2。
   - `Run(Third.)` → **page 3**（奇数页）。✓

**需要观察的现象**：`Third.` 最终落在第 3 页（奇数页），中间隔了一张空页 page 2。

**预期结果**：共 3 页：page1=`First.`、page2=空、page3=`Third.`。若把 `to: "odd"` 改成 `to: "even"`：处理 Parity 时 `Even.matches(1)`=`1%2==0`=假 → continue 不补页，`Third.` 直接落在 page 2（偶数页），共 2 页。

> 待本地验证：可用 `typst compile` 生成 PDF 并数页数/看页码确认。

#### 4.3.5 小练习与答案

**练习 1**：若 `First.` 之后已经有 2 页内容（即处理 Parity 时 `pages.len()=2`），`to: "odd"` 还会补页吗？

**答案**：`Odd.matches(2)` = `2%2==1` = 假 → continue，**不补页**。因为下一页是第 3 页，本就是奇数页，无需补。这正是 `to: "odd"` 想要的效果。

**练习 2**：为什么 collect 阶段「无条件」记录 Parity，而不在 collect 里就判断要不要补页？

**答案**：collect 是纯切片，**没有物理页号信息**（页号要等并行排版 + finalize 才确定）。是否补页依赖于「此时已排出多少页」这个串行累积量，所以只能延后到 `layout_pages` 的串行循环里判断。这也解释了为什么 `Item::Parity` 不能进 `parallelize`。

---

### 4.4 标签处理：migrate_unterminated_tags 与 Item::Tags

#### 4.4.1 概念说明

collect 处理两类与标签有关的边界情况，目的都是「**让内省（query/counter/outline）拿到的元素位置与人类直觉一致**」。

**情况一：未终止的 start tag 跨分页符。** 典型场景是用户写了 `show heading: it => pagebreak() + it`：标题元素的 `Tag::Start` 在 realize 后排在 `pagebreak()` **之前**，但标题的可见内容排在 pagebreak **之后**。如果不处理，introspector 会认为这个标题「在第 N 页结束之前就开始了」，导致页号归属错乱。`migrate_unterminated_tags` 把那些「排在 pagebreak 前、且在 pagebreak 前没有对应 End」的 start tag **迁移到 pagebreak 之后**。

**情况二：只剩标签的碎片。** 有时一整段连续内容全是 `TagElem`（没有任何可见元素）。这种碎片不该单独占一页（否则纯标签会无端改变分页），于是 collect 把它记成 `Item::Tags` 暂存，等下一页开始时（甚至文档末尾）再贴进去。

#### 4.4.2 核心流程

```text
情况一：migrate_unterminated_tags(children, mid)
   1. 在 [start..end] 区间内（mid 前的尾部 tag + mid 后的连续 pagebreak）
   2. 收集所有「已终止」的 End location → excluded 集合
   3. 用稳定排序把区间分三组：excluded tag(-1) | pagebreak(0) | 待迁移 tag(1)
   4. 返回新的切分点 end'（落在 pagebreak 之前），让待迁移 tag 跟到下一段

情况二：纯标签碎片
   if group 全是 TagElem 且 (非「末尾 boundary + 暂存空页」情形):
       items.push(Item::Tags(group))   // 暂存，不单独成页
```

#### 4.4.3 源码精读

先看主循环里对「连续非页面符」段的切分，以及迁移的调用点（[pages/collect.rs:68-77](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/collect.rs#L68-L77)）：

```rust
// 找出连续非页面符的长度
let end = children.iter().take_while(|(c, _)| !c.is::<PagebreakElem>()).count();

// 把分页符前「未终止的 start tag」迁到分页符之后
let end = migrate_unterminated_tags(children, end);
if end == 0 {
    continue;
}
```

`migrate_unterminated_tags` 的实现（[pages/collect.rs:127-164](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/collect.rs#L127-L164)）分四步：

```rust
let (before, after) = children.split_at(mid);
let start = mid - before.iter().rev().take_while(|&(c, _)| c.is::<TagElem>()).count();
let end   = mid + after.iter().take_while(|&(c, _)| c.is::<PagebreakElem>()).count();

let excluded: FxHashSet<_> = children[start..mid]
    .iter()
    .filter_map(|(c, _)| match c.to_packed::<TagElem>()?.tag {
        Tag::Start(..) => None,
        Tag::End(loc, ..) => Some(loc),   // 这些 location 已被终止，不迁移
    })
    .collect();

let key = |(c, _): &Pair| match c.to_packed::<TagElem>() {
    Some(elem) => if excluded.contains(&elem.tag.location()) { -1 } else { 1 },
    None => 0,   // pagebreak
};

children[start..end].sort_by_key(key);   // 稳定排序：-1 | 0 | 1
```

注释（[L157-L160](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/collect.rs#L157-L160)）特意说明：虽然可以写更高效的直接算法，但用稳定排序更不容易出 bug，而且这**不在热路径**上。

再看「纯标签碎片」的特殊处理（[pages/collect.rs:93-101](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/collect.rs#L93-L101)）：

```rust
if group.iter().all(|(c, _)| c.is::<TagElem>())
    && !(staged_empty_page
        && children.iter().all(|&(c, s)| {
            c.to_packed::<PagebreakElem>().is_some_and(|c| c.boundary.get(s))
        }))
{
    items.push(Item::Tags(group));
    continue;
}
```

解读：如果这段内容**全是标签**，就不为它单独开一页（因为「没有标签的排版」里根本不会出现这一段，标签不该影响分页）。例外是「当前暂存了一张空页、且后面只剩 boundary 页面符」——此时可以直接用自己顶替那张尾部空页，所以不走 Tags 分支。

`Item::Tags` 的消费方在 `layout_pages`（[pages/mod.rs:221-228](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/mod.rs#L221-L228)）：把标签收集进 `tags` 缓冲，**贴到下一个 Run 的页首**；若直到文档结束都没遇到下一个 Run，则贴到最后一页的末尾（[pages/mod.rs:232-238](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/mod.rs#L232-L238)）。

```rust
Item::Tags(items) => {
    tags.extend(items.iter()
        .filter_map(|(c, _)| c.to_packed::<TagElem>())
        .map(|elem| elem.tag.clone()));
}
```

#### 4.4.4 代码实践

**实践目标**：理解 `show heading: it => pagebreak() + it` 为何需要迁移 start tag。

**操作步骤**（源码阅读型实践）：

1. 设想 realize 后的 children 片段：`[Tag::Start(标题), Pagebreak(), <标题可见内容>, Tag::End(标题)]`。
2. 主循环第一轮：`children.first()` 是 `Tag::Start`（非 PagebreakElem），进入 else 分支。`end` 初值取连续非页面符长度——`Tag::Start` 算非页面符，所以 `end` 至少为 1。
3. 调用 `migrate_unterminated_tags(children, end)`：`mid` 处于 `Tag::Start` 之后、Pagebreak 之前。`start` 回退统计尾部 tag，`end` 前进统计后续 pagebreak，于是区间 `[start..end]` 覆盖 `Tag::Start` 与紧随的 Pagebreak。
4. `excluded` 集合：在 `[start..mid]` 里找 End 的 location——这里只有 Start 没有 End，所以 `excluded` 为空，`Tag::Start` 的 key 为 `1`（待迁移），Pagebreak 的 key 为 `0`。
5. 稳定排序后，`Tag::Start` 被挪到 Pagebreak **之后**。函数返回新的 `end'`（落在 pagebreak 之前），于是这一轮的 `group` 为空（`end'` 可能等于 start），循环 `continue`，`Tag::Start` 留给后续与「标题可见内容」一起成段。

**需要观察的现象**：迁移后，标题的 `Tag::Start` 与其可见内容落在**同一页**，introspector 给该标题的页号就是标题实际出现的页，而不是 pagebreak 之前那一页。

**预期结果**：`query(heading)` 或 outline 里该标题的页码正确。若没有这个迁移，页码会偏到前一页。

> 待本地验证：可写一个含 `show heading: it => pagebreak() + it` 的小文档，编译后检查 outline 中标题的页码。

#### 4.4.5 小练习与答案

**练习 1**：`migrate_unterminated_tags` 为什么只迁移「未终止」的 start tag，而已终止的（excluded）不动？

**答案**：如果一个 start tag 在 pagebreak 之前就已经有对应的 End（即元素整体在 pagebreak 之前结束），那它的页号本就属于前一页，是正确的，不该迁移。只有「开始了但还没结束、可见内容在 pagebreak 之后」的 tag 才需要迁到后面。`excluded` 集合正是用来识别这些「已终止」的 location，给它们 key=-1 让它们留在原处。

**练习 2**：`Item::Tags` 暂存的纯标签碎片，最终会被贴到哪里？

**答案**：在 `layout_pages` 里累积进 `tags` 缓冲，**prepend 到下一个 `Item::Run` 对应页的起始**（甚至早于页眉，见 [collect.rs:86-88](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/collect.rs#L86-L88) 注释）；如果直到文档结束都没有下一个 Run，则在 [pages/mod.rs:232-238](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/mod.rs#L232-L238) 贴到最后一页末尾。

---

## 5. 综合实践

给定下面这段 Typst 文档，请完整模拟 `collect` 的执行，产出 `items` 列表，并继续模拟 `layout_pages` 的串行循环，给出最终页数与每页内容。最后用一句话解释每一步 `staged_empty_page` 的真假。

```typst
#set page(height: 40pt)

正文一。
#pagebreak(weak: true)
#pagebreak()
#pagebreak(to: "even")
正文二。
```

**参考解答（要点）**：

1. children ≈ `[正文一, pb(weak), pb(strong), pb(to:even), 正文二]`。
2. collect 逐步（初始 `staged=true`）：
   - `正文一` → `Run(正文一)`，`staged=false`。
   - `pb(weak)`：strong=false，①不落盘、④不暂存；`staged` 仍 false。
   - `pb(strong)`：strong=true，但 `staged=false` → ①不落盘；`to=None` 不记 Parity；④ `staged=true`。
   - `pb(to:even)`：strong=true，`staged=true` → **①落盘 `Run(&[])`**（这就是两个连续换页之间那一张空页）；`to=Even` → 记 `Parity(Even)`；④ `staged=true`。
   - `正文二` → `Run(正文二)`，`staged=false`。
   - 循环结束 `staged=false`，不 flush。
   - items ≈ `[Run(正文一), Run(&[]), Parity(Even), Run(正文二)]`。
3. layout_pages 串行循环：
   - `Run(正文一)` → page 1，`pages.len()=1`。
   - `Run(&[])`（空页）→ page 2，`pages.len()=2`。
   - `Parity(Even)` → `Even.matches(2)`=`2%2==0`=**真** → `!true`=false → **不 continue，补一张空页** page 3，`pages.len()=3`。注意这里用 `pages.len()` 而非 `pages.len()+1`：`pages.len()=2`（偶）匹配 Even，恰好说明下一页 `pages.len()+1=3`（奇）奇偶不对，需要补页。
   - `Run(正文二)` → page 4（偶数页）。✓ 符合 `to: "even"`。
4. 最终：4 页——page1=正文一、page2=空、page3=空（Parity 补的）、page4=正文二。`正文二`落在偶数页 4，满足 `to: "even"`。

> 待本地验证：用 `typst compile` 实际生成 PDF 数页确认。

这个练习把本讲的三大主题——strong/weak 与 staged empty page（4.2）、Parity 奇偶补页（4.3）、（本例未触发标签迁移）——串了起来。

---

## 6. 本讲小结

- `collect` 是 `layout_pages` 的**纯切片**步骤，把扁平 `Vec<Pair>` 切成三类指令 `Item::Run` / `Item::Tags` / `Item::Parity`，自身不排版。
- 只有 `Item::Run` 会进 `parallelize` 并行排版；`Tags` 和 `Parity` 是依赖串行全局状态（已暂存标签、已排出页数）的记账指令。
- `staged_empty_page` 是个一布尔状态机：strong pagebreak 「预约」一张空页，真实内容「占用」它，循环末尾「刷出」它——这避免了连续换页产生多余空页。
- `PagebreakElem` 三态：strong（强制换页+预约空页）、weak（当前页空则跳过）、boundary（scope 边界保底换页，连样式都不强加）。
- `Parity`（`to: "odd"/"even"`）在 collect 阶段无条件记录，在 `layout_pages` 用 `parity.matches(pages.len())` 判断是否补空页——延后是因为补页依赖物理页号。
- `migrate_unterminated_tags` 用稳定排序把分页符前「未终止的 start tag」迁到分页符后，修正 `show heading: it => pagebreak() + it` 这类规则的页号归属；纯标签碎片则记成 `Item::Tags` 暂存，避免无端成页。

---

## 7. 下一步学习建议

collect 产出 `items` 之后，`layout_pages` 的下一步是对每个 `Item::Run` 调用 `layout_page_run` **并行**排版。这正是下一讲 **u3-l3「页面运行 layout_page_run：边距与页眉页脚」** 的主题：你会看到页面尺寸/边距如何从样式链解析、正文如何用 `layout_flow(FlowMode::Root)` 排版、numbering 等 marginal 如何根据 `number_align` 决定进 header 还是 footer，以及为何产出的是「尚未最终化、缺物理页号」的 `LayoutedPage`。

随后 **u3-l4「页面最终化 finalize」** 会讲清楚 `counter`、binding 左右互换、tags 挂载等依赖物理页号的串行收尾，与本讲的 `Item::Tags`/`Item::Parity` 消费首尾呼应。建议按 u3-l3 → u3-l4 → u3-l5（PagedIntrospector）的顺序读完整个页面子系统。
