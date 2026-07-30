# Region、Regions、Frame 与 Fragment

## 1. 本讲目标

本讲是「布局系统」单元的第二篇，承接 u6-l1 的度量与几何原语（`Abs`/`Em`/`Length`/`Rel`/`Size`/`Point`），正式进入**布局的输入与输出**。

读完本讲，你应当能够：

- 说清 `Region` / `Regions` 这套「区域序列」抽象如何描述排版时的可用空间，尤其是 `size`、`full`、`backlog`、`last` 四个字段在分页中各自扮演什么角色。
- 解释 `expand`（`Axes<bool>`）控制的「**填充** vs **收缩**」语义，并知道它在 `measure` 这类探针函数里如何被设为 `false`。
- 读懂 `Frame` 帧树的结构：它由哪些 `FrameItem` 组成、`Group` 如何递归、`Arc<LazyHash<Vec<…>>>` 为何让它廉价克隆。
- 理解 `Fragment` 只是「一组 `Frame`」的新类型包装，以及它与 `Regions` 的「输入多个区域 → 输出多个帧」的对应关系。

> 关键定位：`Region` / `Regions` / `Frame` / `Fragment` 这四个类型都定义在 `typst-library` 里，但**真正消费它们的布局算法**（`Layout` trait、`regions.next()` 的循环）在 `typst-layout` crate。这正是 u5-l4 讲过的「类型留本 crate、行为拆到行为 crate」——本讲的四个类型是两侧共享的**接口词汇**。本讲只讲「数据结构本身」，分页循环的具体编排留到后续 `typst-layout` 相关讲义。

## 2. 前置知识

本讲需要以下前置认知（来自更早的讲义）：

- **度量原语**（u6-l1）：`Abs`（绝对长度）、`Size`（二维尺寸 `{x, y}`）、`Point`（二维点 `{x, y}`）、`Axes<T>`（带水平/垂直两个分量的容器）。本讲的 `Region` 就是「一个 `Size` + 一个 `Axes<bool>`」。
- **crate 分离与 Routines**（u5-l4）：`typst-library` 不依赖 `typst-layout`，而是通过 `engine.library.routines.layout_frame` 这样的函数指针回调布局行为。本讲末尾会看到 `measure` 正是用这种方式拿到一个 `Frame`。
- **`Arc` 与 `LazyHash`**（u4-l1 / u12-l2 预告）：`LazyHash<T>` 惰性缓存哈希值，`Arc<T>` 引用计数共享。`Frame` 的字段 `items: Arc<LazyHash<Vec<…>>>` 让它在「克隆近乎免费」的同时仍可参与 comemo 增量记忆化。
- **内省基础**（u9 预告，只需知道名词）：`Tag` 是内省元素在帧里留下的「标记」，`Location` 是文档位置。本讲只把它们当作 `FrameItem` 的两个叶子类型，不展开。

一个贯穿全讲的直觉：

> 排版 = 在一串**区域（Regions）**里摆放内容，产出一棵**帧树（Frame）**；内容跨页时就产出多帧，打包成 **Fragment**。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲角色 |
|------|------|----------|
| `src/layout/regions.rs` | `Region`、`Regions` 类型与分页推进方法（`next`/`iter`/`may_break`/`may_progress`/`is_full`） | **输入**：可用空间模型 |
| `src/layout/frame.rs` | `Frame`、`FrameItem`、`GroupItem`、`FrameKind`、`FrameParent` | **输出**：帧树与叶子类型 |
| `src/layout/fragment.rs` | `Fragment`（`Vec<Frame>` 的新类型） | **输出**：多帧序列 |
| `src/layout/measure.rs` | 用户函数 `measure`，演示如何构造一个「探针区域」并回调 `layout_frame` 得到 `Frame` | **串联**：输入→输出的最小可运行例子 |
| `src/layout/axes.rs` | `Axes<T>` 容器（`expand` 字段的基础） | 辅助：`Axes<bool>` 与 `splat` |
| `src/layout/abs.rs` | `Abs` 绝对长度（`zero`/`inf`/`fits`） | 辅助：`is_full` 的判定 |

## 4. 核心概念与源码讲解

### 4.1 Region 与 Regions：布局的「输入」

#### 4.1.1 概念说明

排版的根本问题是：「我手头有多少空间可以放东西？」但真实排版里这个空间往往是**一串**，而不是一块——一页放不下就翻到下一页，下一页放不下再翻。`Regions` 就是对「一串等宽矩形可用空间」的抽象。

先看最简单的一个区域 `Region`：它只有两个字段——尺寸 `size` 和 `expand`。

```rust
// src/layout/regions.rs:6-13
pub struct Region {
    pub size: Size,
    pub expand: Axes<bool>,
}
```

- `size`：这块矩形有多大。
- `expand`：一个**每轴独立**的布尔值（`Axes<bool>` 即 `{x: bool, y: bool}`）。它回答的是「元素应该**撑满**区域，还是**收缩**到内容自然大小？」——`true` 表示撑满（fill），`false` 表示收缩（shrink to fit）。

但单个 `Region` 无法表达「放不下就翻页」。于是有了 `Regions`：一**序列**同宽的区域。

> **为什么所有区域同宽？** 源码注释写得很直白：Typst 目前不支持内容环绕浮动元素，所以一串区域共用同一个 `size.x`，只有高度会变。

#### 4.1.2 核心流程

`Regions` 用三个字段把「一串高度」表达出来，这是本模块最关键的设计：

```rust
// src/layout/regions.rs:42-55
pub struct Regions<'a> {
    pub size: Size,        // 当前（第一块）区域的【剩余】尺寸
    pub expand: Axes<bool>,
    pub full: Abs,         // 当前区域的【完整】高度（用于相对尺寸）
    pub backlog: &'a [Abs],// 后续区域的高度队列（宽度都同 size.x）
    pub last: Option<Abs>, // 队列耗尽后无限重复的「末区域」高度
}
```

可以把一串区域的高度理解为这样一个序列：

\[ h_0,\; h_1,\; \dots,\; h_{k-1},\; h_\infty,\; h_\infty,\; \dots \]

其中：

- \(h_0 =\) `size.y`，是**当前正在排版的区域**。它可能已经被放了一些内容，所以 `size.y` 是「剩余高度」，而 `full` 记着它的「原始完整高度」。
- \(h_1 \dots h_{k-1}\) 来自 `backlog`（一个切片，长度 \(k-1\)）。
- \(h_\infty\) 来自 `last`，在 `backlog` 耗尽后**无限重复**。

**关键区分：`size.y` vs `full`。** `size.y` 是当前区域还剩多少空间；`full` 是当前区域本来有多高。当你写 `50%` 这样的相对高度时，它是相对于 `full`（整页），而不是相对于「还剩的 `size.y`」。一旦调用 `next()` 翻到下一块全新区域，`size.y` 和 `full` 就重新相等。

`expand` 的「填充 vs 收缩」语义用一个对照表说明：

| `expand` 值 | 含义 | 典型场景 |
|------------|------|----------|
| `x: true`（如页面正文） | 元素横向撑满到区域宽度 | `block` 块级元素占满页宽 |
| `x: false`（如行内盒子） | 元素横向收缩到内容宽度 | `box` 行内元素只占内容宽 |
| `Axes::splat(false)`（两轴都 false） | 只测内容自然大小，不撑满 | `measure` 探针（见 4.1.3） |

推进逻辑由三个布尔方法 + 一个 `next()` 刻画，它们的分工是本模块第二个关键点：

- `may_break()`：到底**允不允许**翻区域？（`backlog` 非空 **或** `last` 存在。）
- `may_progress()`：翻了**有没有用**？（还有 `backlog`，**或者** `last` 的高度与当前不同——即翻过去空间真的会变。）
- `is_full()`：当前区域**已经满了**吗？（剩余高度 ≈ 0 **且** 翻了有用。）

#### 4.1.3 源码精读

**构造：单区域与无限重复。** 一个 `Region` 可以无缝转成 `Regions`（`backlog` 为空、`last` 为 `None`，即只有一块、不翻页）：

[regions.rs:22-32](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/regions.rs#L22-L32) — 把单个 `Region` 转成恰好只有一块、无后续的 `Regions`（注意 `last: None`）。

反过来，`repeat` 构造一个**无限**同尺寸区域序列（`last = Some(size.y)`，靠它无限重复）：

[regions.rs:59-67](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/regions.rs#L59-L67) — `repeat`：`backlog` 空、`last = Some(size.y)`，于是 `iter()` 会无限产出同尺寸区域。

**翻页：`next()`。** 这是分页模型的核心。它先尝试从 `backlog` 头部取一个高度（`split_first` 把头摘下、切片前移），取不到再退回 `last`：

```rust
// regions.rs:114-127（节选关键分支）
pub fn next(&mut self) {
    if let Some(height) = self.backlog.split_first()
        .map(|(first, tail)| { self.backlog = tail; *first })
        .or(self.last)       // backlog 取空后，退回 last
    {
        self.size.y = height; // 新区域的剩余高度
        self.full = height;   // 新区域完整高度（全新区域，二者相等）
    }
}
```

注意 `.or(self.last)`：`last` **不会被消费**，它被无限复用。所以 `backlog` 是「用一次少一个」的有限队列，`last` 是「永不耗尽」的末尾区域。

**整序列视图：`iter()`。** 这个方法把上面三个字段拼成一条逻辑序列，是最能帮助理解 backlog/last 模型的代码：

```rust
// regions.rs:133-138
pub fn iter(&self) -> impl Iterator<Item = Size> + '_ {
    let first = std::iter::once(self.size);   // 当前区域，一次
    let backlog = self.backlog.iter();        // backlog，各一次
    let last = self.last.iter().cycle();      // last，无限循环
    first.chain(backlog.chain(last).map(|&h| Size::new(self.size.x, h)))
}
```

读法：`first`（1 次）→ `backlog`（每个 1 次）→ `last`（**无限循环**）。注释也明说「This iterator may be infinite」。所有产出的 `Size` 都共享同一个 `size.x`。

**三个判定方法。**

[regions.rs:97-111](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/regions.rs#L97-L111) — `is_full` / `may_break` / `may_progress` 三个判定。其中 `is_full` 里 `Abs::zero().fits(self.size.y)` 的含义见下。

`is_full` 的判定依赖 `Abs::fits`：

[abs.rs:117-119](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/abs.rs#L117-L119) — `fits(self, other)` 即 `self + EPS >= other`（允许一点误差）。

所以 `Abs::zero().fits(self.size.y)` 等价于「`size.y ≤ 一个极小正数 EPS`」，即剩余高度已≈0（甚至为负，被溢出撑爆）。配合 `&& self.may_progress()`，`is_full()` 的完整语义是：**「当前区域已被填满，且翻到下一区域确实能改善处境」**——这正是布局算法决定「强制断行/断页」的时机。

**真实串联：`measure`。** `measure` 函数是 typst-library 内部把「构造一个 `Region` 输入 → 回调布局 → 拿到 `Frame` 输出」串起来的最小可读例子：

```rust
// measure.rs:79-85（构造「探针区域」pod）
let pod = Region::new(
    Axes::new(
        width.resolve(styles).unwrap_or(Abs::inf()),
        height.resolve(styles).unwrap_or(Abs::inf()),
    ),
    Axes::splat(false),   // 两轴都不撑满：只测自然大小
);
```

[measure.rs:96-102](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/measure.rs#L96-L102) — 把 `pod` 区域交给 `routines.layout_frame` 回调，拿回一个 `Frame`（u5-l4 的 Routines 机制）。

注意两个细节：(1) `auto` 宽高被解析成 `Abs::inf()`（无限大），所以 `measure` 默认给内容「无限空间」；(2) `expand = Axes::splat(false)` 表示**不撑满**——这正是 `measure` 文档里「measured dimensions may not necessarily match the final dimensions」的原因：真实排版上下文里元素可能被要求撑满，而探针不撑满。

#### 4.1.4 代码实践

**实践目标**：用一个具体的数字例子，验证你对 `backlog` / `last` / `next` 模型的理解。

**操作步骤（源码阅读 + 推演型）**：

1. 打开 [regions.rs:114-127](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/regions.rs#L114-L127) 的 `next()` 和 [regions.rs:133-138](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/regions.rs#L133-L138) 的 `iter()`。
2. 假设有这样一个 `Regions`（伪代码，**示例代码**，非项目原有）：
   - 宽度 `size.x = 400pt`
   - 第一块区域：`size.y = 30pt`（当前页**剩余**），`full = 200pt`（当前页**完整**）
   - `backlog = [200pt, 150pt]`（后续两页高度）
   - `last = Some(200pt)`（之后所有页都是 200pt）
3. 推演 `iter()` 产出的前 6 个 `Size`：
   - 第 1 个：当前区域 `(400, 30)` —— 注意是剩余 30，不是完整 200
   - 第 2 个：backlog[0] `(400, 200)`
   - 第 3 个：backlog[1] `(400, 150)`
   - 第 4、5、6 个：last 重复 `(400, 200)`、`(400, 200)`、`(400, 200)`……
4. 推演连续调用 3 次 `next()` 后的状态：
   - 第 1 次：`size.y = 200`、`full = 200`、`backlog = [150pt]`
   - 第 2 次：`size.y = 150`、`full = 150`、`backlog = []`
   - 第 3 次：`backlog` 已空，退回 `last`，`size.y = 200`、`full = 200`、`backlog` 仍为 `[]`（`last` 不被消费）

**需要观察的现象**：

- `size.y`（剩余）与 `full`（完整）在第一块区域上**不相等**（30 ≠ 200），这正是「当前页已经排了一部分内容」的体现；翻页后二者重新相等。
- `last` 永不耗尽，无论 `next()` 多少次都退回它。

**预期结果**：第 3 步、第 4 步的推演结果如上。若你想确认 `full` 的真实用途，可在 `typst-layout` 的 `flow/compose.rs` 里看到消费侧同时读取 `regions.base()`（即 `full`）做相对尺寸、读取 `regions.size.y` 做剩余空间判断——两个字段各司其职。

#### 4.1.5 小练习与答案

**练习 1**：`may_break()` 和 `may_progress()` 有何区别？构造一个 `may_break() == true` 但 `may_progress() == false` 的 `Regions`。

> **答案**：`may_break` 问「允不允许翻」（有 backlog 或有 last 即可）；`may_progress` 问「翻了有没有用」（backlog 非空，**或** last 高度 ≠ 当前 `size.y`）。当 `backlog = []` 且 `last = Some(h)` 且 `size.y == h` 时：`may_break()` 为真（有 last），但 `may_progress()` 为假（翻过去高度没变，没意义）。这表示「末区域正在用，且翻页不会带来新空间」。

**练习 2**：为什么 `measure` 要把 `expand` 设成 `Axes::splat(false)` 而不是 `true`？

> **答案**：`measure` 的目的是测内容的**自然大小**。若 `expand = true`，内容会被撑满到给定的（默认无限）区域，测出来的就是「撑满后的尺寸」而非「内容本来的尺寸」。设 `false` 让内容收缩到自然大小，才符合「测量」语义。这也是文档说测量值「可能不等于最终尺寸」的原因——真实上下文可能要求撑满。

### 4.2 Frame 与 FrameItem：布局的「输出」之一（单帧）

#### 4.2.1 概念说明

`Frame`（帧）是排版的**成品**：内容已经在固定位置上摆好了。文档注释一句话点题——「A finished layout with items at fixed positions.」

可以把它想成一张**画布**：

- 它有 `size`（画布多大）。
- 它有 `baseline`（基线在哪，用于文字对齐；没有就默认在底部）。
- 它有 `items`（画布上摆了哪些东西，每件东西带一个左上角坐标 `Point`）。
- 它有 `kind`（软/硬，影响渐变的坐标系参考）。

帧是**树**形结构：一张帧里可以摆「另一张帧」（通过 `Group`），子帧又能继续套子帧。最终整篇文档就是一棵帧树。

#### 4.2.2 核心流程

帧的生命周期：

1. **创建**：`Frame::new(size, kind)` / `Frame::soft(size)` / `Frame::hard(size)`，断言尺寸有限。
2. **填充**：`push(pos, item)` 放前景、`prepend(pos, item)` 放背景（插到 0 层）、`insert(layer, pos, item)` 放指定 z 层。
3. **嵌套**：`push_frame(pos, child)` 把子帧挂进来，内部用 `should_inline` 决定「把子帧的元素就地搬进来（摊平）」还是「包成一个 `Group`」。
4. **变换**：`translate` / `resize` / `transform` / `clip` 改变内容位置或形状。
5. **产出**：最终 `Frame` 被塞进 `Fragment`，交给 PDF/HTML 导出器渲染。

帧树的叶子类型由 `FrameItem` 枚举穷举——这是本模块第二个必须记住的清单：

| `FrameItem` 变体 | 含义 |
|-----------------|------|
| `Group(GroupItem)` | **子帧**（带变换/裁剪/标签/逻辑父级），递归形成树 |
| `Text(TextItem)` | 一段已塑形的文字（shaped text run） |
| `Shape(Shape, Span)` | 几何形状（带可选填充/描边），如矩形、曲线 |
| `Image(Image, Size, Span)` | 位图/矢量图，附带尺寸 |
| `Link(Destination, Size)` | 超链接区域（内/外部跳转） |
| `Tag(Tag)` | 内省元素留下的标记（供 query/locate 定位） |

而 `GroupItem` 自身带五个字段，是帧树「非叶子节点」的全部修饰能力：`frame`（子帧）、`transform`（变换）、`clip`（裁剪曲线）、`label`（标签）、`parent`（逻辑父级，用于内省排序）。

#### 4.2.3 源码精读

**帧的结构与共享存储。**

```rust
// frame.rs:17-30
pub struct Frame {
    size: Size,
    baseline: Option<Abs>,     // None ⇒ 基线默认在底部
    items: Arc<LazyHash<Vec<(Point, FrameItem)>>>,
    kind: FrameKind,
}
```

`items` 用 `Arc<LazyHash<Vec<(Point, FrameItem)>>>` 而非裸 `Vec`，原因有二：(1) `Arc` 让 `Frame` 克隆近乎免费（帧在排版/导出间会被多处持有）；(2) `LazyHash` 缓存哈希值，满足 comemo 增量记忆化对 `Hash` 的高频需求（u4-l1 讲过 `LazyHash<Style>`，这里是同一手法）。

**基线与 ascent/descent。**

[frame.rs:111-113](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/frame.rs#L111-L113) — `baseline()`：无显式基线时回退到 `size.y`（底部）。基线从**顶部**量起；`ascent` = 基线到顶，`descent` = `size.y - baseline`（基线到底）。

**写入：写时复制。**

[frame.rs:161-163](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/frame.rs#L161-L163) — `push` 用 `Arc::make_mut(&mut self.items)`：当 `Arc` 独占时原地改，共享时才深拷贝。这是「廉价克隆」与「可变性」并存的关键。

**嵌套与「摊平」优化。** 把子帧挂进来时，`push_frame` 会判断要不要摊平：

[frame.rs:227-230](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/frame.rs#L227-L230) — `should_inline`：只有「软帧 **且**（父帧为空 **或** 子帧元素 ≤ 5）」才摊平。

[frame.rs:233-275](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/frame.rs#L233-L275) — `inline`：把子帧的 `(Point, FrameItem)` 逐个搬进父帧，坐标加上 `pos` 偏移；还用 `Arc::try_unwrap` 尽量直接拿走内部 `Vec`，避免逐项克隆。这个优化让帧树在常见情况下保持**扁平**，减少 `Group` 嵌套深度。

> **为什么软帧才摊平？** 硬帧（`FrameKind::Hard`）有自己的尺寸边界，是渐变等特性的坐标系锚点（见下），摊平会破坏这个语义，所以硬帧一律保留为 `Group`。

**软/硬帧 `FrameKind`。**

[frame.rs:458-470](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/frame.rs#L458-L470) — `FrameKind`：`Soft`（默认，跟随父级尺寸）与 `Hard`（用自己的尺寸，用于 page/block/box）。

它的作用体现在**渐变坐标系**：注释说「This is used to determine the coordinate reference system for gradients.」一个软帧不会打断父级渐变的坐标连续性，硬帧则会——这是为什么页/块/盒要标 `Hard`。

**叶子类型 `FrameItem`。**

[frame.rs:485-499](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/frame.rs#L485-L499) — `FrameItem` 枚举六变体（见上表）。这就是「帧树由哪些东西组成」的**权威清单**。

**`GroupItem`（非叶子节点）。**

[frame.rs:515-530](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/frame.rs#L515-L530) — `GroupItem` 五字段：`frame`、`transform`、`clip`、`label`、`parent`。其中 `parent: Option<FrameParent>` 让一组元素的逻辑顺序「插到某个父 location 之后」——这是内省（u9）在帧层面的钩子。

**变换的统一实现：`group` 私有助手。**

[frame.rs:376-386](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/frame.rs#L376-L386) — `group()`：把当前帧的**全部内容**包进一个新的软帧，再作为 `GroupItem` 推回自己。`transform` / `clip` / `label` / `set_parent` 都通过它实现——即「要给整帧加变换，就先把它套进一个 Group」。以 `transform` 为例：

[frame.rs:345-349](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/frame.rs#L345-L349) — `Frame::transform`：非空才调用 `group(|g| g.transform = transform)`。

**调试工具。** `Frame` 还自带一组「画标记」方法，排版调试时给帧加可视化标注：

[frame.rs:398-416](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/frame.rs#L398-L416) — `mark_box_in_place`：给帧加半透明青色背景 + 红色基线，用于直观看到帧的边界与基线位置。这是理解 `Frame` API 的一个干净、自包含的真实例子。

#### 4.2.4 代码实践

**实践目标**：把「帧树由哪些 `FrameItem` 组成」从抽象清单变成具体认识，并用 `measure` 真正拿到一个 `Frame`。

**操作步骤**：

1. 列清单：对照 [frame.rs:485-499](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/frame.rs#L485-L499) 的 `FrameItem`，在纸上画一棵帧树：根 `Frame` → `items` 是 `Vec<(Point, FrameItem)>` → 其中 `Group(GroupItem{frame, …})` 指向子 `Frame` → 递归。叶子是 `Text`/`Shape`/`Image`/`Link`/`Tag`。
2. 读一个真实生产者：打开 [measure.rs:96-104](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/measure.rs#L96-L104)，看 `layout_frame` 回调返回的 `frame` 如何被 `frame.size()` 取出宽高，组装成 `dict! { "width" => x, "height" => y }` 返回给用户。这说明 `Frame` 是布局的最终交付物。
3. 读一个真实变换：对照 [frame.rs:337-342](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/frame.rs#L337-L342) 的 `fill`，理解「加背景填充 = 在第 0 层 `prepend` 一个铺满帧的填充矩形 `Shape`」——这正是 `FrameItem::Shape` 的典型用途。

**需要观察的现象**：

- 帧树的非叶子节点只有 `Group` 一种；其余五个 `FrameItem` 变体都是叶子。
- `Group` 既能承载子帧，又能承载 `transform`/`clip` 等修饰，所以「变换/裁剪」在帧树里表现为「套一层 Group」。

**预期结果**：能画出 `Frame → Vec<(Point, FrameItem)> → Group{Frame → …}` 的递归结构，并说出 `Text`/`Shape`/`Image`/`Link`/`Tag` 五种叶子各自代表什么。

#### 4.2.5 小练习与答案

**练习 1**：`push_frame` 什么时候会把子帧「摊平」而不是包成 `Group`？为什么硬帧不摊平？

> **答案**：当 `should_inline` 为真，即子帧是**软帧**且（父帧为空或子帧元素 ≤ 5）时摊平。硬帧不摊平，因为硬帧是自身尺寸的坐标系锚点（用于渐变），摊平会丢失这个边界语义。

**练习 2**：`Frame::transform` 为什么不直接修改各元素坐标，而是「套一层 Group」？

> **答案**：任意仿射变换（旋转/缩放/倾斜）无法简单地逐元素平移坐标来实现，且把变换记录在 `GroupItem::transform` 上，可以让导出器在渲染时统一应用（例如 SVG/PDF 的变换矩阵），既正确又高效。`group()` 私有助手就是「把当前内容打包进一个带 `transform` 的 Group」的统一机制。

### 4.3 Fragment：布局的「输出」之二（多帧序列）

#### 4.3.1 概念说明

`Regions` 是「一串输入区域」，那「一串输出帧」用什么装？答案就是 `Fragment`。它是一个极薄的新类型（newtype）：

```rust
// fragment.rs:7
pub struct Fragment(Vec<Frame>);
```

如果说 `Frame` 是「一页」，`Fragment` 就是「一次布局产出的若干页」。一篇跨 3 页的文档，其顶层布局结果就是一个含 3 个 `Frame` 的 `Fragment`。

#### 4.3.2 核心流程

`Fragment` 的输入输出对应关系：

\[ \text{布局}(\text{Regions}) \longrightarrow \text{Fragment} = [ \text{Frame}_0,\; \text{Frame}_1,\; \dots ] \]

粗略地，每消费一个区域（调一次 `regions.next()`）就可能产出一个 `Frame`。但两者**数量未必相等**：

- 单区域内容不跨页 → `Regions`（一区域）→ `Fragment` 含 1 个 `Frame`（用 `into_frame()` 取出）。
- 内容跨多页 → 多区域 → `Fragment` 含多个 `Frame`。
- 某些元素（如 `box`/`measure` 探针）约定只产出单帧，所以 `into_frame()` 在多于 1 帧时会 **panic** 作为契约保护。

`Fragment` 提供的能力全是「容器视角」：构造（`frame`/`frames`）、计数（`len`/`is_empty`）、取出（`into_frame`/`into_frames`/`as_slice`）、迭代（`iter`/`iter_mut`/`IntoIterator`）。

#### 4.3.3 源码精读

**构造：单帧与多帧。**

[fragment.rs:11-18](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/fragment.rs#L11-L18) — `frame()` 装 1 帧、`frames()` 装多帧。

**取单帧的契约保护。**

```rust
// fragment.rs:33-37
#[track_caller]
pub fn into_frame(self) -> Frame {
    assert_eq!(self.0.len(), 1, "expected exactly one frame");
    self.0.into_iter().next().unwrap()
}
```

`#[track_caller]` 让 panic 指向**调用方**而非 `Fragment` 内部，便于定位是哪个布局违反了「只产一帧」的契约。注意它**只**断言恰好 1 帧：0 帧或 ≥2 帧都会 panic。

**迭代便利。**

[fragment.rs:69-94](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/fragment.rs#L69-L94) — 为 `Fragment`、`&Fragment`、`&mut Fragment` 分别实现 `IntoIterator`，使 `for frame in &fragment` 这类写法直接可用，无需手动 `as_slice()`。

**Debug 的体贴。**

[fragment.rs:60-67](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/fragment.rs#L60-L67) — `Debug` 在只有 1 帧时直接打印那帧，多帧时才打印列表。调试时单帧是常态，省去一层嵌套括号。

#### 4.3.4 代码实践

**实践目标**：理解 `into_frame()` 的 panic 契约，并厘清「输入区域数」与「输出帧数」的关系。

**操作步骤（源码阅读型）**：

1. 打开 [fragment.rs:33-37](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/fragment.rs#L33-L37)，确认 `into_frame` 的断言是 `== 1`（不是 `>= 1`）。
2. 回到 4.1 的 `measure` 例子：[measure.rs:96-102](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/measure.rs#L96-L102) 调用 `layout_frame` 直接返回 `Frame`（而非 `Fragment`）——这说明 routine 层的 `layout_frame` 契约就是「单区域→单帧」；而多区域的多帧编排由更上层的 `typst-layout` 流式布局（flow）负责，它消费 `Regions`、产出 `Fragment`。
3. 推演：一个跨 3 页的段落，布局时会被喂一个 `backlog = [h1, h2]`、`last = Some(h∞)` 的 `Regions`，消费 3 个区域，产出含 3 个 `Frame` 的 `Fragment`。

**需要观察的现象**：

- `Fragment` 是 `Vec<Frame>` 的薄包装，本身没有「帧之间关系」的额外信息——页与页的关系在更高层（如 `PageElem` / flow）维护。
- `into_frame` 的 panic 是**防御性契约**：调用方承诺「我只接受单帧结果」，多帧即编程错误。

**预期结果**：能说出 `measure`（单区域→单帧）与跨页文档（多区域→多帧 `Fragment`）这两种典型情形，并解释 `into_frame` 何时安全、何时 panic。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `into_frame` 用 `assert_eq!(len, 1)` 而不是返回 `Option<Frame>` 或 `Result`？

> **答案**：因为「恰好一帧」是调用方与被调方之间的**不变量（invariant）**而非可恢复的运行时错误。若出现 0 帧或多帧，说明上游布局逻辑写错了，应该尽早暴露（fail fast）而非静默吞掉。`#[track_caller]` 进一步把错误定位到违反契约的调用点。

**练习 2**：`Fragment` 为什么不直接是 `type Fragment = Vec<Frame>`，而要用 `struct Fragment(Vec<Frame>)` 新类型？

> **答案**：新类型（newtype）能给它挂专属方法（`into_frame` 的断言、定制 `Debug`、多个 `IntoIterator` 实现），并阻止把任意 `Vec<Frame>` 误当 `Fragment` 传入布局接口——类型层面区分「一堆帧」与「一次布局的产出」。代价只是薄薄一层解引用。

## 5. 综合实践

**目标**：从 Typst **用户侧**亲手「摸到」本讲的区域与帧概念，把 `Regions` 的 `full` vs `size.y`、`expand` 的填充/收缩语义串成一个可运行的小文档。

**操作步骤**：

1. 新建文件 `observe-region.typ`（**示例代码**，非项目源文件），写入：

   ```typst
   // 1) layout 暴露当前区域的完整尺寸（对应 Regions 的 base/full 概念）
   #layout(size => [
     页面正文区：宽 #size.width、高 #size.height。
   ])

   // 2) 用 block(height: 1fr) 占满「当前页剩余空间」，于是内部的 layout
   //    看到的是「剩余高度」(对应 Regions 的 size.y)
   #block(height: 1fr, layout(size => [
     被填满后，这块剩余高度是 #size.height。
   ]))

   // 3) measure 默认给「无限空间 + 不撑满」(expand=false)，测的是内容自然大小
   #context {
     let s = measure([hello world])
     [“hello world” 的自然尺寸：宽 #s.width、高 #s.height。]
   }
   ```

2. 编译：`typst compile observe-region.typ`（命令本身需你本地具备 typst CLI，**待本地验证**）。
3. 打开生成的 PDF，对比三段文字里的尺寸数值。

**需要观察的现象与预期结果**：

- 第 1 段：`size.height` ≈ 页面高度减去上下边距（`full`：整页可用高度）。
- 第 2 段：因为前面已排了一些内容、且 `1fr` 抢占了剩余空间，`size.height` 会**明显小于**第 1 段（这就是 `size.y` 剩余高度 vs `full` 完整高度的区别）。
- 第 3 段：`measure` 不撑满，测出的是 `[hello world]` 的自然宽高，与它在页面里被撑满后的尺寸不同——印证 4.1.3 里 `expand = Axes::splat(false)` 的设计。

> 这一步把抽象的 `Region`/`Regions` 字段映射到了你能在 PDF 里读到的数字，是理解「输入区域模型」最直观的方式。帧树（`Frame`/`Fragment`）的内部结构则藏在编译器里，需结合 4.2 的源码阅读来认识。

## 6. 本讲小结

- `Region` = 一块可用空间（`size` + `expand`）；`Regions` = 一串**同宽**区域，用 `size`(当前剩余) / `full`(当前完整) / `backlog`(后续有限队列) / `last`(无限重复末区域) 四字段表达分页。
- `expand: Axes<bool>` 控制**填充**(true) vs **收缩**(false)，每轴独立；`measure` 探针特意设 `false` 以测内容自然大小。
- `next()` 从 `backlog` 头部摘高度、耗尽后退回 `last`（不消费）；`iter()` = `first(1次) → backlog(各1次) → last(无限循环)`。
- `may_break`（能不能翻）/ `may_progress`（翻了有没有用）/ `is_full`（满了且翻了有用）是分页决策的三把尺子；`Abs::fits` 提供带误差的「≤」判定。
- `Frame` 是排版成品：`Arc<LazyHash<Vec<(Point, FrameItem)>>>` 让它廉价克隆又可哈希；`FrameItem` 六变体（`Group`/`Text`/`Shape`/`Image`/`Link`/`Tag`）是帧树全部组成，`Group` 是唯一非叶子。
- `FrameKind` 软/硬帧决定渐变坐标系参考；变换/裁剪/标签统一靠「套一层 `Group`」实现；`should_inline` 把小软帧摊平以保持帧树扁平。
- `Fragment = struct(Vec<Frame>)` 是多帧新类型，对应「多区域→多帧」；`into_frame()` 用 `assert_eq!(len, 1)` 守护「只产单帧」契约。

## 7. 下一步学习建议

- **下一讲 u6-l3（流式布局：stack/grid/columns）**：本讲建立了「输入区域 → 输出帧」的通用词汇，下一讲将看具体元素（`StackElem`/`GridElem`/`ColumnsElem`）如何调用 `regions.map`、`regions.next()` 来切分区域、产出 `Fragment`——尤其是 `columns` 如何把一个区域切成多列，是 `Regions` 模型的直接应用。
- **回到 typst-layout crate**：本讲的类型是接口词汇，真正的 `Layout` trait、`regions.next()` 循环、flow 多页编排都在 `typst-layout`。学完 u6 全单元后，建议打开 `typst-layout/src/flow/compose.rs` 与 `stack.rs`，看消费侧如何同时读 `regions.base()`（相对尺寸）与 `regions.size.y`（剩余空间）。
- **衔接内省（u9）**：本讲出现的 `FrameItem::Tag`、`GroupItem::parent`、`Location` 是内省在帧层面的钩子；等学到 u9 的 `query`/`locate` 时，你会明白这些标记是如何被回填、又如何支撑「跨页计数器」这类依赖收敛循环的能力。
