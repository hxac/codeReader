# Regions：区域与回退队列

## 1. 本讲目标

排版的本质是「在一块有限的画布里摆放元素」。typst-layout 把「这块画布」抽象成了一个类型——`Regions`。它是**所有 layouter 的通用输入**：不管你排的是一段段落、一个栈、一张表格，还是一整页正文，函数签名里都有一份 `regions: Regions` 告诉你「现在有多少空间可以用」。

学完本讲你应该能够：

1. 说清楚 `Region`（单区域，俗称 pod）与 `Regions`（区域序列）的区别，以及二者如何互相转换。
2. 逐字段解释 `Regions` 的五个要素：`size`、`expand`、`full`、`backlog`、`last`。
3. 理解 `regions.next()`、`may_break()`、`may_progress()`、`is_full()` 四个核心方法的语义，以及它们如何驱动「逐区域排版」的主循环。
4. 区分 `base()`、`size`、`full` 三者在相对尺寸计算中的不同用途。
5. 通过 `StackLayouter` 这个真实例子，看懂一个 layouter 是如何「消费」Regions、在多个候选区域之间产出多个 frame 的。

> 一个关键认知（承接 u1-l3、u2-l1）：`Regions`/`Region` 这两个类型**并不定义在 typst-layout crate 内**，而是定义在兄弟 crate `typst-library` 里（作为 `typst_library::layout::Regions` 导出），typst-layout 只是「消费」它们。这解释了为什么本讲引用的**定义处**永久链接指向 `crates/typst-library/...`，而「使用处」链接指向 `crates/typst-layout/...`。

## 2. 前置知识

- **排版的两条主轴**：Typst 用 `Axes<T>` 表示横纵两个分量。沿文字流向（通常是垂直方向）的那根轴叫**主轴（main / block axis）**，垂直于它的叫**交叉轴（cross / inline axis）**。本讲多数例子假设竖排版向，主轴 = Y。
- **绝对长度 `Abs`**：Typst 内部长度单位（pt 级）。它有一个带容差的方法 `fits`，本讲的 `is_full()` 会用到：
  [crates/typst-library/src/layout/abs.rs:115-119](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/abs.rs#L115-L119) ——`zero().fits(y)` 实际含义是「`y` 是否小于等于 0（允许微小误差）」。
- **`Frame` / `Fragment`**（u2-l3 会详讲）：`Frame` 是一块排版好的二维画面；`Fragment` 是「一串 `Frame`」。一个 layouter 的返回值通常是 `Fragment`——因为内容可能跨多个区域，每个区域产出一个 `Frame`。
- **`Engine` 与 comemo 记忆化**（u2-l1）：公开排版函数把 `&mut Engine` 拆成可追踪参数后，调用带 `#[comemo::memoize]` 的 `_impl`。`Regions` 是 `Copy + Hash` 的纯值类型，正好能成为缓存键的一部分。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `crates/typst-library/src/layout/regions.rs` | **定义** `Region` 与 `Regions` 及其全部方法（本讲的「字典」）。 |
| `crates/typst-layout/src/flow/mod.rs` | 文档/片段级排版入口；`layout_frame`/`layout_fragment` 是消费 `Regions` 的最顶层例子，`layout_flow` 是逐区域主循环。 |
| `crates/typst-layout/src/flow/compose.rs` | 演示如何**改造**一份 `Regions`：构造多列子区域、构造脚注 pod。 |
| `crates/typst-layout/src/stack.rs` | `StackLayouter`——一个完整、可读、逐区域产出 frame 的「教科书式」layouter，是本讲综合实践的对象。 |
| `crates/typst-library/src/layout/axes.rs` | `Axes<bool>::select`，解释 `expand` 如何在「填满 / 收缩」之间二选一。 |

---

## 4. 核心概念与源码讲解

### 4.1 Region 与 Regions：两种「画布」抽象

#### 4.1.1 概念说明

排版时，「画布」有两种形态：

- **单区域 `Region`**：就是「一块固定大小的矩形 + 一个 expand 开关」。当你**确信内容只占一帧**时用它（例如排一条脚注分隔线、一个图形）。代码里常把它叫 **pod**（point-of-deployment 的缩写，意为「一次性投放点」）。
- **区域序列 `Regions`**：一块「当前区域」加上「后续若干候选区域」组成的**队列**。内容可能装不下当前区域、需要溢出到下一块时用它（例如排一页正文、一个可断裂的块）。

承接 u1-l3 的结论：`layout_frame` 收一个 `Region`（单区域），`layout_fragment` 收一个 `Regions`（序列）；而 `layout_frame` 内部其实就是 `region.into()` 后转交给 `layout_fragment`。所以 **`Region` 是 `Regions` 的退化情形**。

#### 4.1.2 核心流程

`Region` 只有两个字段：`size` 和 `expand`。把它转成 `Regions` 时（`From<Region>`），其余三个字段被填成「没有后续区域」的空值：

```text
Region { size, expand }
        ──into()──▶  Regions {
                        size,                  // 与 Region 相同
                        expand,                // 与 Region 相同
                        full: size.y,          // 初始即满高
                        backlog: &[],          // 没有后续候选
                        last: None,            // 也没有可重复的末区
                     }
```

这等价于「一个永远只有当前区域、排完即止」的队列——所以 `layout_frame` 排出的内容不可能跨区域断裂。

#### 4.1.3 源码精读

`Region` 的定义与构造（定义在兄弟 crate typst-library）：
[crates/typst-library/src/layout/regions.rs:7-13](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/regions.rs#L7-L13) ——单区域只有 `size` 与 `expand` 两个字段。

`Region` 如何变成 `Regions`：
[crates/typst-library/src/layout/regions.rs:22-32](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/regions.rs#L22-L32) ——`full` 取 `size.y`，`backlog` 与 `last` 置空，语义上「排满这一块就结束」。

typst-layout 侧的「单区域入口」`layout_frame` 正是这条转换的真实使用点：
[crates/typst-layout/src/flow/mod.rs:42-51](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L42-L51) ——`region.into()` 把 pod 变成 `Regions`，再交给 `layout_fragment`，最后用 `Fragment::into_frame` 断言「恰好一帧」并取出。这条断言正是 u1-l3 提到的核心判据：内容跨帧时必须用 `layout_fragment`，注定单帧才用 `layout_frame`。

#### 4.1.4 代码实践

1. **实践目标**：理解「单区域 pod 不能跨区域断裂」这一约束的来源。
2. **操作步骤**：
   - 打开 [crates/typst-layout/src/flow/mod.rs:42-51](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L42-L51)，确认 `layout_frame` 调用了 `region.into()`。
   - 打开 [crates/typst-library/src/layout/regions.rs:22-32](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/regions.rs#L22-L32)，确认转换后 `backlog` 为空、`last` 为 `None`。
   - 再去 4.3 节看 `may_break()` 的实现（`backlog.is_empty() && last.is_none()` 时返回 `false`）。
3. **需要观察的现象**：由 pod 转来的 `Regions` 其 `may_break()` 与 `may_progress()` 恒为 `false`。
4. **预期结果**：因此 pod 里的内容永远不会触发「换到下一个区域」的分支，最多只能在当前这一块内排版。
5. 结论：若你给一个天然会跨页的元素（如一整章正文）配了 pod，它会因为无法断裂而溢出当前区域——这是为什么文档级排版必须用 `layout_fragment` + 真正的多区域队列。

#### 4.1.5 小练习与答案

**Q1**：为什么不直接让所有函数都收 `Regions`，还要单独留一个 `Region`？

> **参考答案**：`Region` 更简单、表达「只排一帧」的意图，且能让 `layout_frame` 的 API 更清晰；同时 `From<Region> for Regions` 让二者无缝衔接，pod 就是「长度为 1 且不重复」的退化队列。

**Q2**：`Region::new(size, expand)` 转成 `Regions` 后，`full` 等于多少？

> **参考答案**：`full = size.y`（见 [regions.rs:26](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/regions.rs#L26)）。

---

### 4.2 五要素拆解：size、expand、full、backlog、last

#### 4.2.1 概念说明

`Regions` 一共五个字段，是理解整个类型的关键：

| 字段 | 类型 | 含义 |
|---|---|---|
| `size` | `Size` | **当前区域剩余尺寸**。排版过程中会被不断「削短」——每放进一个元素，主轴方向的分量就减去它占用的高度。 |
| `expand` | `Axes<bool>` | **填满还是收缩**。某轴为 `true` 表示「请把输出 frame 撑到区域尺寸」；为 `false` 表示「按内容实际大小收缩」。 |
| `full` | `Abs` | **当前区域的完整高度**（仅主轴）。即便 `size.y` 被削短，`full` 始终是这块区域最初的高度，用作相对尺寸（`em`、百分比）的分母。 |
| `backlog` | `&[Abs]` | **后续候选区域的高度队列**。宽度永远与当前区域相同（整个 `Regions` 共用同一个 `size.x`）。 |
| `last` | `Option<Abs>` | **末尾可无限重复的区域高度**。`backlog` 耗尽后，若 `last` 存在，则之后的每一区域都用这个高度，永不枯竭。 |

一个重要事实：**所有区域共享同一个宽度 `size.x`**（见类型注释 [regions.rs:34-40](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/regions.rs#L34-L40)）。`backlog` 和 `last` 只存「高度」。这也是注释里说的「目前无法让文字绕着一个浮动元素排版」的原因——要绕排就需要不同宽度的区域。

#### 4.2.2 核心流程

**`base()` 与相对尺寸**：很多尺寸是相对的（如 `100%`、`2em`）。相对尺寸需要一个「基准」。沿交叉轴（宽度），基准就是 `size.x`；沿主轴（高度），基准是 `full`（而非已经被削短的 `size.y`）。`base()` 把这两者拼成一个 `Size`：

\[
\texttt{base()} = (\texttt{size.x},\ \texttt{full})
\]

直觉上：**「这块区域本来有多大，相对尺寸就按多大算」**，而不是「已经被前面元素吃掉之后剩多少就按多少算」。这样一段 `1em` 的段间距在页面顶部和页面底部才一致。

**`expand` 的语义**：`expand` 是调用方与被调用方之间的**契约**——「你产出的 frame 该多大」。它通过 `Axes<bool>::select(t, f)` 落地：
[crates/typst-library/src/layout/axes.rs:219-224](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/axes.rs#L219-L224) ——某轴 `true` 取 `t`（区域尺寸，填满），`false` 取 `f`（内容尺寸，收缩）。

#### 4.2.3 源码精读

`Regions` 结构体定义：
[crates/typst-library/src/layout/regions.rs:42-55](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/regions.rs#L42-L55) ——逐字段注释说明了每个要素的用途。

`base()` 的实现：
[crates/typst-library/src/layout/regions.rs:73-75](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/regions.rs#L73-L75) ——注意 x 取 `size.x`、y 取 `full`，二者来源不同。

typst-layout 里真实使用 `base()` 和 `full` 的地方：

- 计算**列宽**时用 `base().x` 做相对间距基准：
  [crates/typst-layout/src/flow/mod.rs:255](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L255)（`column.gutter.relative_to(regions.base().x)`）。
- `layout_flow` 在收集阶段用 `regions.full` 构造「整列高度」用于内部数据结构：
  [crates/typst-layout/src/flow/mod.rs:214](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L214)（`Size::new(config.columns.width, regions.full)`）。

还有两个**构造** `Regions` 的常用入口值得一看：

- `Regions::repeat(size, expand)`：构造「所有区域都同尺寸、无限重复」的队列——`backlog` 空、`last = Some(size.y)`：
  [crates/typst-library/src/layout/regions.rs:59-67](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/regions.rs#L59-L67)。
- `Regions::map`：对每个区域高度施加同一个函数（例如给一块区域整体加 padding 后传给子排版器）。注意它**忽略 backlog/last 上函数返回的宽度**，因为所有区域必须同宽：
  [crates/typst-library/src/layout/regions.rs:81-95](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/regions.rs#L81-L95)。

#### 4.2.4 代码实践

1. **实践目标**：验证「相对尺寸基准是 `full` 而非 `size.y`」。
2. **操作步骤**：
   - 在 [regions.rs:73-75](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/regions.rs#L73-L75) 确认 `base().y == full`。
   - 在 `layout_spacing`（[stack.rs:198-218](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/stack.rs#L198-L218)）里看到相对间距 `relative_to(self.regions.base().get(self.axis))`——即按 `full` 解析，与剩余 `size.y` 无关。
3. **需要观察的现象**：即便当前区域已被前一个块削短到只剩很少空间，`Spacing::Rel` 的解析值仍按整区域高度计算（之后再用 `min(remaining)` 限制实际占用）。
4. **预期结果**：`1em` 间距在区域顶部和底部解析出的绝对值相同；只是底部可能因 `min(remaining)` 被截短。
5. 这是一个纯源码阅读型实践；如想用 Typst 文档验证行为，可在 `.typ` 文件里写 `#v(1em)` 并观察不同位置间距，**待本地验证**。

#### 4.2.5 小练习与答案

**Q1**：`backlog` 为什么只存高度、不存宽度？

> **参考答案**：整个 `Regions` 共用同一个宽度 `size.x`（见类型注释）。若允许每区域不同宽度，就需要支持「绕排浮动元素」等更复杂的布局，目前未实现。

**Q2**：`full` 与 `size.y` 何时相等、何时不等？

> **参考答案**：刚进入一个新区域时（尚未放任何元素）二者相等；一旦放了元素、`size.y` 被减去占用高度后，`size.y < full`。`next()` 进入下一区域时会把二者都重置为新区域高度（见 4.3.3）。

---

### 4.3 队列游走：next()、may_break()、may_progress()、is_full()

#### 4.3.1 概念说明

光有「当前区域」不够，layouter 还要能**问两个问题、做一个动作**：

- 「我现在能不能换到下一区域？」→ `may_break()`
- 「换到下一区域，空间会不会变多（从而有希望装下装不下的内容）？」→ `may_progress()`
- 「当前区域是不是已经满了，该换区域了？」→ `is_full()`
- 动作：「换到下一区域」→ `next()`

这四个方法配合使用，构成了**逐区域排版循环**的控制骨架，也是防止「死循环」的关键。

#### 4.3.2 核心流程

设当前 `size.y = h0`，`backlog = [h1, h2, ...]`，`last = Some(hL)`（可能为 `None`）。

- **`next()`**：从 `backlog` 取出第一个高度 `h1`，把 `size.y` 与 `full` 都设为 `h1`，`backlog` 前移一位；若 `backlog` 已空，则改用 `last`（`hL`），且此后每次 `next()` 都重复用 `hL`（因为 `last` 不被消耗）。
- **`may_break()`**：`!backlog.is_empty() || last.is_some()`——只要还有任何后续区域就允许换页。
- **`may_progress()`**：`!backlog.is_empty() || (last.is_some() && size.y != hL)`——换区域**可能**带来更大空间。一旦你已经停在 `last` 区域（`size.y == hL`），再 `next()` 也只是重复同样的高度，`may_progress()` 即变 `false`。
- **`is_full()`**：`zero().fits(size.y) && may_progress()`——当前剩余高度 ≤ 0 **且** 还有「更大」的下一区域可去。两个条件缺一不可：光满但不能去更好的区域，就不能算「该换区域」（否则会死循环）。

\[ 
\texttt{may\_progress()} \;=\; \neg\,\texttt{backlog.is\_empty()}\;\lor\;\bigl(\texttt{last.is\_some()} \land \texttt{size.y} \ne \texttt{last}\bigr)
\]

防死循环的直觉：**「内容装不下」时，只有 `may_progress()` 为真才允许尝试换区域；否则继续换区域只是原地踏转，必须停止。**

#### 4.3.3 源码精读

四个方法的实现集中在这里：
[crates/typst-library/src/layout/regions.rs:97-127](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/regions.rs#L97-L127) ——`is_full` 用 `Abs::zero().fits(self.size.y)` 判「剩余≤0」，并额外要求 `may_progress()`；`next()` 先 `split_first` 取 backlog，再 `or(self.last)` 兜底。

`iter()` 把整条队列展开成一个（可能无限）的迭代器，便于在**不真正推进**游标的前提下「预览」所有区域尺寸：
[crates/typst-library/src/layout/regions.rs:133-138](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/regions.rs#L133-L138) ——用 `last.iter().cycle()` 实现末区无限重复。

typst-layout 的「逐区域主循环」就在 `layout_flow` 里，完美示范了这组 API 的用法：
[crates/typst-layout/src/flow/mod.rs:222-234](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L222-L234)

```text
loop {
    frame = compose(...regions);          // 用当前区域排一帧
    finished.push(frame);
    if work.done() && (!regions.expand.y || regions.backlog.is_empty()) {
        break;                            // 内容排完，且无需继续填满 backlog
    }
    regions.next();                       // 推进到下一区域
}
```

注意终止条件里的 `!regions.expand.y || regions.backlog.is_empty()`（[第 229 行](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L229)）：若 `expand.y` 为真（要求纵向填满），即使内容已排完（`work.done()`），也必须**继续把 backlog 里剩下的区域一个个填满**——典型场景是「要让每一页都铺满背景/页眉页脚」。注释把这叫「draining the backlog if necessary」。

`may_progress()` 的「防死循环」作用在脚注排版里有非常具体的体现（注释甚至花了一大段解释）：
[crates/typst-layout/src/flow/compose.rs:535-541](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L535-L541) ——当脚注在当前页放不下、首帧为空时，只有 `regions.may_progress()` 为真才会把它「迁移/排队」到下一页；否则就原地排（否则会无限排队下去）。

#### 4.3.4 代码实践

1. **实践目标**：亲手模拟 `next()` 在不同 backlog/last 配置下的游走。
2. **操作步骤**：设初始 `size.y = 30`，`backlog = [40, 50]`，`last = Some(60)`。在纸上依次调用 `next()` 四次，记录每次调用后的 `size.y`，以及在该状态下 `may_break()` / `may_progress()` 的真假。
3. **需要观察的现象**：
   - 第 1 次 `next()` → `size.y = 40`（backlog → `[50]`）；
   - 第 2 次 → `50`（backlog → `[]`）；
   - 第 3 次 → `60`（用 `last`，backlog 仍空）；
   - 第 4 次 → `60`（`last` 不被消耗，永久重复）。
4. **预期结果**：前三次 `may_progress()` 都为真；从第 3 次之后（`size.y == last == 60`）`may_progress()` 变为 `false`。`may_break()` 全程为真（因为 `last.is_some()`）。
5. 把上面的追踪与 [regions.rs:114-127](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/regions.rs#L114-L127) 的实现逐行对照，确认你的手算无误。

#### 4.3.5 小练习与答案

**Q1**：若 `backlog = []` 且 `last = None`，`is_full()` 会返回什么？为什么这样设计？

> **参考答案**：恒为 `false`。因为没有后续区域（`may_progress() == false`），即便当前区域满了，也不能换区域。这样设计避免了「满了一直换、换了还是同一块」的死循环——pod（`Region::into()`）正是这种情形。

**Q2**：`layout_flow` 主循环为什么在 `expand.y == true` 时即使 `work.done()` 也不立即 break？

> **参考答案**：因为 expand 要求每一区域都被填满；若 backlog 还有候选区域，必须继续产出填满它们的 frame（见 [mod.rs:229](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L229)）。

---

### 4.4 expand 的传播：以 StackLayouter 为例

#### 4.4.1 概念说明

`expand` 不只是「填满/收缩」的开关，它还会在 layouter 之间**逐层传播与改写**。一个父 layouter 收到的 `expand` 决定了它**自己输出**的大小；同时它会**改写传给子元素**的 `expand`，以表达「我不希望孩子在某个方向上撑开」之类的意图。

`StackLayouter`（`stack.rs`）是理解这件事最好的例子，因为它把「消费 Regions → 逐区域产出 frame」的完整流程写得非常直白。

#### 4.4.2 核心流程

`StackLayouter` 沿主轴堆放子项，每个区域产出一个 frame，流程如下：

```text
new(regions):
    保存 self.expand = regions.expand          # 栈自己的 expand（决定输出大小）
    regions.expand.set(axis, false)            # 关掉孩子在主轴上的 expand（孩子按内容收缩）

对每个子项:
    若 regions.is_full(): finish_region()      # 当前区域满 → 收尾、产出 frame、next()
    layout_fragment(child, regions)            # 用（可能已被削短的）当前区域排孩子
    regions.size.y -= child.height             # 削短当前区域
    若孩子本身跨多帧: finish_region() 在帧之间触发

finish_region():
    size = expand.select(initial, used).min(initial)   # expand 决定填满 or 收缩
    排好这一区域的 frame → finished.push(...)
    regions.next()                             # 推进到下一区域，重置 used/fr
```

关键是第一步：**栈会把自己的 `expand` 存下来用于决定输出尺寸，同时把传给孩子的 `expand` 在主轴上强制关掉**——因为栈里每个孩子只该占它自然的高度，不该各自去撑满整个栈区域。

#### 4.4.3 源码精读

`StackLayouter` 结构体与构造：
[crates/typst-layout/src/stack.rs:128-195](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/stack.rs#L128-L195) ——重点看 [第 179 行](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/stack.rs#L179) `regions.expand.set(axis, false)`：孩子的主轴 expand 被关掉；而 [第 176 行](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/stack.rs#L176) 的 `let expand = regions.expand;` 保存的是栈**自己**的 expand。

`layout_block`——消费区域、排孩子：
[crates/typst-layout/src/stack.rs:221-250](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/stack.rs#L221-L250) ——[第 227 行](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/stack.rs#L227) 先判 `is_full()`，[第 241-247 行](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/stack.rs#L241-L247) 把当前（已削短的）`self.regions` 直接传给孩子排。

`layout_fragment`（栈的方法，注意与公开入口同名但不同）——存帧并削短区域：
[crates/typst-layout/src/stack.rs:271-300](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/stack.rs#L271-L300) ——[第 281 行](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/stack.rs#L281) `self.regions.size.y -= specific_size.y`；[第 294-296 行](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/stack.rs#L294-L296) 当孩子返回多帧时在帧之间调用 `finish_region()`。

`finish_region`——expand 如何决定最终尺寸的核心：
[crates/typst-layout/src/stack.rs:303-370](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/stack.rs#L303-L370) ——[第 306-309 行](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/stack.rs#L306-L309)：

```rust
let mut size = self
    .expand
    .select(self.initial, self.used.into_axes(self.axis))
    .min(self.initial);
```

含义：对每个轴，`expand` 为真取 `initial`（区域原始尺寸，**填满**），为假取 `used`（内容实际占用，**收缩**），最后用 `.min(initial)` 保证不超出区域。[第 314-317 行](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/stack.rs#L314-L317) 还有一层：若存在 `Fr`（分数）间距且区域有限，则把主轴强制撑到 `full`。[第 363 行](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/stack.rs#L363) `self.regions.next()` 推进到下一区域。

> **一个反向例子——构造 pod**：`compose.rs` 里排脚注分隔线时，手动构造了一个单区域 pod，并把它的 `expand.y` 显式设为 `false`（分隔线不该纵向撑满）：
> [crates/typst-layout/src/flow/compose.rs:626-632](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L626-L632)。而排脚注条目时则复制 `*regions` 再改 `pod.expand.y = false`、削短高度：
> [crates/typst-layout/src/flow/compose.rs:488-491](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L488-L491)。这正是「layouter 按需改写 Regions」的典型手法。

#### 4.4.4 代码实践

1. **实践目标**：直观感受 `expand.select(...)` 在不同 expand 组合下如何改变输出尺寸。
2. **操作步骤**：假设某轮 `finish_region` 时 `initial = (100pt, 30pt)`，`used` 的主轴（Y）分量 `= 18pt`、交叉（X）分量 `= 100pt`，`Fr = 0`。按下表逐格计算 `size = expand.select(initial, used).min(initial)`。
3. **需要观察的现象 / 预期结果**（请先自填，再对答案）：

   | `expand` | X 取值来源 | Y 取值来源 | 最终 `size` |
   |---|---|---|---|
   | `(true, true)`  | ? | ? | ? |
   | `(false, false)` | ? | ? | ? |
   | `(true, false)` | ? | ? | ? |

4. **参考答案**：
   - `(true, true)` → X 取 `initial.x=100`，Y 取 `initial.y=30` → `(100, 30)`（两轴都填满）。
   - `(false, false)` → X 取 `used.x=100`，Y 取 `used.y=18` → `(100, 18)`（两轴都收缩到内容）。
   - `(true, false)` → X 取 `initial.x=100`，Y 取 `used.y=18` → `(100, 18)`（横满、纵按内容，这是页眉页脚/分隔线最常见的形态）。
   三种情况 `.min(initial)` 都不会进一步改变结果（因为都不超过 `initial`）。
5. 与 [axes.rs:219-224](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/axes.rs#L219-L224) 的 `select` 定义对照确认。

#### 4.4.5 小练习与答案

**Q1**：为什么 `StackLayouter::new` 要把孩子方向的 `expand` 关掉，而不是原样传下去？

> **参考答案**：栈里每个孩子只应占其自然高度；若让孩子的主轴 expand 保持为真，孩子会各自尝试撑满整个栈区域，导致重叠或尺寸错乱。栈自己用保存的 `self.expand` 在 `finish_region` 里统一决定整栈输出大小。

**Q2**：`finish_region` 里 `.min(self.initial)` 这一步的作用是什么？

> **参考答案**：防止输出 frame 超过区域原始尺寸。例如内容 `used` 偶尔可能因数值误差或不可断裂元素略大于 `initial`，`.min(initial)` 确保最终 frame 被夹在区域内。

---

## 5. 综合实践

**任务**：追踪 `StackLayouter` 在 backlog 提供多个候选尺寸时**逐区域产出 frame** 的完整流程，并用它解释 `expand` 如何改变最终 frame 尺寸。

**场景设定**（竖排版向，主轴 = Y）：

- 输入 `regions`：`size = (100pt, 40pt)`，`backlog = [40pt]`，`last = None`，`expand = (true, true)`。
- 栈里有**一个可断裂的块**，内容共约 70pt 高（假设在 40pt 高的区域里能整整齐齐排出 40pt，剩余 30pt 溢出）。

**步骤 1：理解 `new` 对 expand 的改写**

进入 `StackLayouter::new` 后（[stack.rs:176-179](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/stack.rs#L176-L179)）：

- `self.expand = (true, true)`（栈自己的，原样保存）；
- 传给孩子的 `regions.expand` 被改为 `(true, false)`（主轴 Y 被关掉）。

**步骤 2：追踪区域 1**

- `layout_block` 判 `is_full()`：`size.y = 40 > 0`，不满，直接排。
- 孩子用 `regions = ((100, 40), expand=(true,false))` 排版。块可断裂、高 40pt 的区域装下 40pt 内容 → 产出一个 **2 帧 Fragment**：`frame1 ≈ 40pt`、`frame2 ≈ 30pt`。
- 进入栈的 `layout_fragment`（[stack.rs:271-300](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/stack.rs#L271-L300)）：
  - `i=0`（frame1，40pt）：`size.y -= 40 → 0`；`used.main = 40`；因为 `i+1 < len`（还有第二帧），触发 `finish_region()`。
    - `finish_region`：`size = expand.select(initial=(100,40), used=(100,40)).min(initial)`。`self.expand=(true,true)` → 两轴都取 `initial` → `(100,40)`。**区域 1 输出 frame = (100, 40)，被填满。**
    - `regions.next()`：从 `backlog=[40]` 取 40，`size.y=40`、`full=40`、`backlog=[]`。
  - `i=1`（frame2，30pt）：用新区域 `(100,40)`，`size.y -= 30 → 10`；`used.main = 30`；`i+1 == len`，不再触发 finish_region。

**步骤 3：追踪区域 2（`finish` 收尾，[stack.rs:373-376](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/stack.rs#L373-L376)）**

- `finish_region`：`size = expand.select(initial=(100,40), used=(100,30)).min(initial)`。
  - `self.expand=(true,true)` → 两轴都取 `initial` → **区域 2 输出 frame = (100, 40)，即使内容只有 30pt 也被填满！**

**步骤 4：对比——把栈的 expand 改成 `(true, false)`**

只改一处：`self.expand = (true, false)`（横向仍填满、纵向按内容收缩）。其余设定不变。重跑步骤 3 的 `finish_region`：

- `expand.select`：X 取 `initial.x=100`，Y 取 `used.y=30` → **区域 2 输出 frame = (100, 30)，收缩到内容。**
- 区域 1 不变（区域 1 的 `used.y` 恰好 = 40 = `initial.y`，两种 expand 下结果相同）。

**请填写的对比表**：

| 区域 | 内容占用 (used.y) | `self.expand=(true,true)` 输出 | `self.expand=(true,false)` 输出 |
|---|---|---|---|
| 区域 1 | 40pt | ? | ? |
| 区域 2 | 30pt | ? | ? |

**参考答案**：区域 1 两列都是 `(100, 40)`；区域 2 分别是 `(100, 40)`（填满）与 `(100, 30)`（收缩）。**这就是 `expand` 对最终 frame 尺寸的影响**——同一个 layouter、同一份内容，仅因 expand 不同，末区域的 frame 就从「撑满 40pt」变成「按内容 30pt」。

**延伸思考（待本地验证）**：若把 `last` 改成 `Some(40pt)`（即「之后所有区域都 40pt 且无限重复」），`may_progress()` 在区域 2 之后会变成什么？栈会不会无限产出 frame？结合 4.3 的 `may_progress()` 语义推断：因为内容已排完（栈的 `finish()` 在 `finish_region` 后直接返回），栈不会因为 `last` 无限而无限产出——它由**内容是否排完**驱动，而非由区域是否还有驱动（这一点与 `layout_flow` 主循环的 `expand.y` 分支不同，值得对比体会）。

---

## 6. 本讲小结

- `Region`（pod，单区域）是 `Regions`（区域序列）的退化情形；`layout_frame` 通过 `region.into()` 复用 `layout_fragment`，pod 转换后 `backlog` 为空、`last` 为 `None`，故**不可跨区域断裂**。
- `Regions` 五要素：`size`（当前剩余尺寸，会被削短）、`expand`（填满/收缩契约）、`full`（整区域高度，相对尺寸基准）、`backlog`（后续候选高度队列）、`last`（可无限重复的末区高度）；所有区域**共用同一宽度**。
- `base() = (size.x, full)`，是相对尺寸（`em`、百分比）的基准，刻意**不**用被削短后的 `size.y`。
- `next()` 推进队列（先消费 `backlog`，再无限重复 `last`）；`may_break()` 判「能否换区域」，`may_progress()` 判「换区域是否可能改善空间」，`is_full()` = 「剩余≤0 且 `may_progress()`」——后三者共同**防止死循环**。
- `expand` 会在 layouter 间传播与改写：`StackLayouter` 保存自己的 expand 用于决定输出尺寸，同时关掉孩子主轴的 expand；`finish_region` 用 `expand.select(initial, used).min(initial)` 落地「填满 vs 收缩」。
- `layout_flow` 的逐区域主循环与 `compose.rs` 里构造多列子区域、脚注 pod 的代码，是「消费 / 改写 Regions」的两类真实范式。

## 7. 下一步学习建议

- **紧接 u2-l3（Frame 与 Fragment）**：本讲反复提到「每个区域产出一个 frame、若干 frame 组成 Fragment」，下一讲将打开 `Frame`/`Fragment`/`FrameItem` 的内部结构，把「排版产出物」这一侧补全。建议带着本讲的例子去读：综合实践里的 `(100,40)` frame 内部到底装了什么。
- **横向对照 u4-l1（flow 总览）**：本讲看了 `layout_flow` 的主循环骨架；u4 系列会进入 `Work`/`Stop`/`compose`/`distribute` 的内部，解释「为什么一个区域可能要 compose 多轮（浮动体/脚注触发重排）」。届时回头看 `may_progress()` 在脚注里的用法（本讲 4.3.3）会有更深的体会。
- **源码延伸阅读**：想看更复杂的「Regions 改写」，可预习 [compose.rs:116-130](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L116-L130)（多列布局如何把一条 `Regions` 拆成「列数 × 区域数」的新 backlog），这是 u4-l6（列布局）的伏笔。
