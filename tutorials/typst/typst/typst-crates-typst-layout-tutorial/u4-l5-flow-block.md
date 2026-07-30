# 块布局 block：单块与可断裂多块

## 1. 本讲目标

本讲承接 u4-l3（distribute 分发）与 u4-l4（compose 组合），把镜头聚焦到 flow 管线中最常见的「块」元素 `BlockElem` 究竟是如何被排版的。

学完本讲你应当能够：

1. 说清一个块在 collect 阶段为何会被判定为「不可断裂（single）」还是「可断裂（multi）」，判据是什么。
2. 读懂 `layout_single_block` 如何用 `unbreakable_pod` 把一块内容排成**一帧**，以及 `layout_multi_block` 如何用 `breakable_pod` 把一块内容排成**多帧 Fragment**。
3. 理解 `MultiSpill` 这个跨区域「接力棒」的真正含义——它保存的不是「剩余内容」，而是「区域历史」，并据此在每进入一个新 region 时**重新整体排版并跳过已产出的帧**，从而把「一次给全所有 region」的旧块模型嫁接到「逐区域按需」的 flow 新模型上。

## 2. 前置知识

在进入本讲前，请确认你已经理解以下概念（前序讲义已建立）：

- **flow 三段式**（u4-l1）：`configuration → collect → 主循环（每区域 compose 一次）`。本讲的主角 `block.rs` 处于 collect 产出的 `Child` 与每区域的 compose/distribute 之间。
- **Child 类型**（u4-l2）：collect 把 `Pair` 预处理成 `Child`，其中块被归类为 `Child::Single(SingleChild)` 或 `Child::Multi(MultiChild)`。
- **distribute 贪心分发与 `Stop::Finish(false)`**（u4-l3）：放不下且 `may_progress()` 时返回自然断点换区域。
- **Regions / Region**（u2-l2）：`Region` 是单区域（俗称 pod，`backlog` 为空、不可跨区域断裂）；`Regions` 是带 `backlog`/`full`/`last` 的区域序列，可跨区域断裂。
- **Frame / Fragment**（u2-l3）：`Frame` 是单帧画布，`Fragment` 是 `Vec<Frame>` 的新类型；`into_frame` 带单帧断言，`into_frames` 取全部。

一句话区分两个关键入口：`layout_frame` 收 `Region` 产**一帧**，`layout_fragment` 收 `Regions` 产**多帧 Fragment**。本讲会反复用到这对关系。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| [src/flow/block.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/block.rs) | 块排版的核心实现：`layout_single_block`、`layout_multi_block`，以及两个 pod 构造器 `unbreakable_pod`、`breakable_pod` 和定高分配助手 `distribute`。 |
| [src/flow/collect.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/collect.rs) | 定义 `SingleChild`、`MultiChild`、`MultiSpill`，以及 collect 阶段判定 single/multi 的 `Collector::block`，和带 `#[comemo::memoize]` 的两个 `_impl` 胶水函数。 |
| [src/flow/distribute.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs) | `Distributor` 消费 `Single`/`Multi` child、并在新 region 开头优先处理 `spill` 的 `run`/`single`/`multi`/`multi_spill`。 |
| [src/flow/mod.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs) | `Work` 状态结构，其中 `spill: Option<MultiSpill>` 字段是跨区域接力的载体，`done()` 会检查它。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块，按调用顺序串联：

1. collect 阶段的 single/multi 分野；
2. `unbreakable_pod` 与 `layout_single_block`；
3. `breakable_pod` 与 `layout_multi_block`；
4. `MultiSpill` 跨区域接力。

### 4.1 collect 阶段的分野：何时是 single，何时是 multi

#### 4.1.1 概念说明

在 flow 管线里，一个 `BlockElem` 是否能「跨区域断裂」并不是在每区域排版时才决定的，而是在 collect 阶段一次性确定的。collect 会读取两个样式字段：

- `breakable`：用户是否允许该块在区域边界被切开。
- `height` 是否为 `Sizing::Fr`（分数高度）。

判据很简单：**只要不可断裂（`breakable == false`）或高度是分数（`Fr`），就是 single；否则是 multi。** 这背后有两条独立的理由：

- `breakable == false`：用户明确要求整块不拆，自然只能排成一帧。
- `height` 为 `Fr`：分数高度本身只在**整块**的高度分配时才有意义（它要和别的 Fr 项瓜分剩余空间），被拆成多帧后语义就乱了，因此也强制 single。

#### 4.1.2 核心流程

```
Collector::block(elem, styles):
  读取 align / alone / sticky / breakable / fr(height)
  push  above 的间距 child
  if (不可断裂) 或 (高度为 Fr):
      push Child::Single(SingleChild { ... })
  else:
      push Child::Multi(MultiChild  { ... })
  push  below 的间距 child
  par_situation = Other
```

其中 `alone` 标记「该块是否是 flow 的唯一 child」，它会在后面用来控制**纵向 expand** 是否保留——只有独占一列/一页的块才允许纵向撑满。

#### 4.1.3 源码精读

判定的核心在 [src/flow/collect.rs:254-275](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/collect.rs#L254-L275)：

```rust
if !breakable || fr.is_some() {
    self.output.push(Child::Single(self.boxed(SingleChild {
        align, sticky, alone, fr, elem, styles, locator, cell: CachedCell::new(),
    })));
} else {
    self.output.push(Child::Multi(self.boxed(MultiChild {
        align, sticky, alone, elem, styles, locator, cell: CachedCell::new(),
    })));
}
```

注意一个细节：`SingleChild` 多带了一个 `fr: Option<Fr>` 字段，而 `MultiChild` 没有。这是因为只有不可断裂块才需要参与 Fr 高度的瓜分（在 distribute 里以 `Item::Fr` 形式登记），可断裂块不处理 Fr。

`alone` 的来源见 [src/flow/collect.rs:237](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/collect.rs#L237)，它等于 `self.children.len() == 1`。

#### 4.1.4 代码实践

1. **实践目标**：亲手确认「分野只看 breakable 与 Fr」这条规则，不被其它字段干扰。
2. **操作步骤**：阅读 [src/flow/collect.rs:234-279](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/collect.rs#L234-L279) 的 `Collector::block` 全函数。
3. **需要观察的现象**：除了 `breakable` 与 `height==Fr`，其它读取的字段（`align`/`sticky`/`alone`/`above`/`below`）都**只是数据搬运**，不参与 single/multi 判定。
4. **预期结果**：你能用一句话向同伴解释「一个块何时变成 SingleChild」。
5. 待本地验证：在 `Collector::block` 的 `if` 前临时加 `eprintln!("breakable={} fr={:?}", breakable, fr);`，编译一个含 `#block(breakable: false)[…]`、普通段落、`#block(height: 1fr)[…]` 的小文档，运行后核对打印结果是否与你的预测一致。

#### 4.1.5 小练习与答案

**练习 1**：若一个块同时满足 `breakable == true` 且 `height` 是 `Sizing::Auto`，它会变成 single 还是 multi？

**答案**：multi。判据是 `!breakable || fr.is_some()`，两个条件都不成立（breakable 为真、fr 为 None），走 `else` 分支。

**练习 2**：为什么 `height: 1fr` 的块要被强制当成 single？

**答案**：分数高度 `Fr` 的语义是「与其它 Fr 项瓜分整块可用高度」，它要求块有一个确定的整体高度来参与分配；一旦被拆成多帧，每一帧的高度由各自 region 决定，Fr 的分配语义就失效了，因此 collect 把它归入 single，让 distribute 以 `Item::Fr` 形式登记并统一瓜分。

---

### 4.2 `unbreakable_pod` 与 `layout_single_block`

#### 4.2.1 概念说明

不可断裂块的排版目标是「**给一块内容，产出一帧**」。要做到这点，需要一个 `Region`（单区域 pod）作为排版画布。`unbreakable_pod` 就是这个 pod 的构造器：它把块的 `width`/`height`（都是 `Sizing` 枚举）、`inset`（内边距）和外部可用尺寸 `base` 综合成一个带尺寸和 `expand` 标记的 `Region`。

`Sizing` 有三种变体，决定了 pod 的尺寸与 expand：

| `Sizing` 变体 | 尺寸取值 | 是否触发该轴 expand |
|---------------|----------|--------------------|
| `Auto` | 取整个 `base` | 否（尺寸由内容收缩决定） |
| `Fr(_)` | 取整个 `base`（Fr 已在外层处理） | 否 |
| `Rel(rel)` | `rel.resolve(styles).relative_to(base)` | 是（且尺寸有限时） |

`expand` 为真的轴意味着「该轴尺寸被强制锁定，要求内容填满」。

#### 4.2.2 核心流程

`unbreakable_pod` 的构造逻辑：

```
size.x = match width  { Auto|Fr => base.x;  Rel(r) => r 相对 base.x }
size.y = match height { Auto|Fr => base.y;  Rel(r) => r 相对 base.y }
若 inset 非零: size = pad::shrink(size, inset)   // 给内边距让位
expand.x = width  != Auto && size.x 有限
expand.y = height != Auto && size.y 有限
返回 Region::new(size, expand)
```

expand 的判定用布尔式表达：

\[
\text{expand}_x = (\text{width} \neq \text{Auto}) \wedge \text{finite}(\text{size}.x)
\]

即只有用户**显式指定了宽度**且该宽度有限时，才要求横向填满；`Auto` 尺寸由内容收缩，不 expand。

`layout_single_block` 拿到 pod 后，按 `BlockBody` 的四种变体排版，最后统一施加 inset/fill/stroke/clip，返回单帧。

#### 4.2.3 源码精读

`unbreakable_pod` 全函数见 [src/flow/block.rs:255-291](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/block.rs#L255-L291)，其中尺寸解析与 expand 判定的关键片段：

```rust
let mut size = Size::new(
    match width {
        Sizing::Auto | Sizing::Fr(_) => base.x,
        Sizing::Rel(rel) => rel.resolve(styles).relative_to(base.x),
    },
    /* height 同理 */
);
if !inset.is_zero() { size = crate::pad::shrink(size, inset); }
let expand = Axes::new(
    *width != Sizing::Auto && size.x.is_finite(),
    *height != Sizing::Auto && size.y.is_finite(),
);
Region::new(size, expand)
```

`layout_single_block` 见 [src/flow/block.rs:19-103](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/block.rs#L19-L103)。注意它的签名收的是**单 `Region`**，对应「不可断裂」语义。body 的四个分支见 [src/flow/block.rs:36-59](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/block.rs#L36-L59)：

```rust
None                           => Frame::hard(Size::zero()),        // 空块：零尺寸硬帧
Some(Content(body))            => layout_frame(engine, body, …, pod),// 内容：layout_frame 产一帧
Some(SingleLayouter(cb))       => cb.call(engine, locator, styles, pod)?,
Some(MultiLayouter(cb))        => cb.call(…, pod.into())?.into_frame(), // 注意 into_frame() 强制单帧
```

两个关键点：

- `Content` 分支用的是 `crate::layout_frame`（收 `Region`、产单帧），与 single 语义天然匹配。
- `MultiLayouter` 分支虽然回调返回多帧 `Fragment`，但末尾 `.into_frame()` 强制取单帧（复用了 u2-l3 讲过的 `Fragment::into_frame` 单帧断言）。

随后 [src/flow/block.rs:68-95](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/block.rs#L68-L95) 在 expand 轴上修正尺寸、施加 inset（`pad::grow`）、按需 clip 与 fill/stroke。

#### 4.2.4 代码实践

1. **实践目标**：验证 `expand` 标记对最终帧尺寸的影响。
2. **操作步骤**：阅读 [src/flow/block.rs:68](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/block.rs#L68) 的 `frame.set_size(pod.expand.select(pod.size, frame.size()))`。`expand.select(a, b)` 的语义是「expand 为真取 a（pod 尺寸），否则取 b（内容自然尺寸）」。
3. **需要观察的现象**：当块 `width` 为 `Auto` 时 `expand.x` 为假，最终帧宽度等于内容宽度；当 `width` 为固定值（如 `100pt`）时 `expand.x` 为真，帧宽度被锁定为 pod 宽度。
4. **预期结果**：你能解释「为什么显式宽度会让块变宽、自动宽度会让块收缩」。
5. 待本地验证：构造 `#block(width: 200pt)[短]` 与 `#block[短]` 两种块，分别在 `layout_single_block` 末尾打印 `frame.size()`，核对前者的宽度是否被锁定为（200pt − inset）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `unbreakable_pod` 在计算 `expand` 之外，还要额外检查 `size.is_finite()`？

**答案**：因为用户可能写出 `width: auto` 之外的极端值，或者 `base` 本身是无穷大（如某些无界排版场景）。即使 `width != Auto`，若解析出的尺寸不有限，强行 expand 会产生无穷大帧，没有意义；所以用 `is_finite()` 把这种情形排除，退化为收缩。

**练习 2**：`layout_single_block` 的 `MultiLayouter` 分支为何必须 `.into_frame()`？

**答案**：`MultiLayouter` 回调按设计返回的是 `Fragment`（多帧），但 `layout_single_block` 的契约是「不可断裂、产单帧」。`.into_frame()` 内部对「恰好一帧」做断言，确保 multi-layouter 在 single 语境下不会偷偷产出多帧；若真的多帧，会在断言处暴露问题。

---

### 4.3 `breakable_pod` 与 `layout_multi_block`

#### 4.3.1 概念说明

可断裂块的目标与 single 相反：**给一块内容和一个区域序列，产出多帧 `Fragment`**，让块能在区域边界被拆开。因此它需要一个 `Regions`（带 `backlog`）而非单 `Region`。`breakable_pod` 就是这个多区域 pod 的构造器，它比 `unbreakable_pod` 复杂得多，因为要处理「定高块如何把一个固定高度摊到多个区域」这件事。

高度分两种情形：

- **`Auto` / `Fr` 高度**：块是自动高度，直接**继承**外部传入的 `regions`（`first`、`full`、`backlog`、`last` 原样拿来）。
- **`Rel` 定高**：块有固定高度 `resolved`。这时要把这个总高度摊到外部区域的序列上——第一区域分一部分、剩余的进 `backlog`，并且 `last = None`（定高块不希望有可无限重复的末区）。摊分由助手函数 `distribute` 完成。

#### 4.3.2 核心流程

`breakable_pod` 的主流程（伪代码）：

```
base = regions.base()
match height {
  Auto | Fr =>  first = regions.size.y; full = regions.full;
                backlog = 复制 regions.backlog; last = regions.last
  Rel(r)    =>  resolved = r 相对 base.y; full = resolved;
                (first, backlog) = distribute(resolved, regions, buf);  // 摊分
                last = None
}
size = Size(width 解析, first)
若 inset 非零: shrink_multiple(size, full, backlog, last, inset)
expand = (width  != Auto && 有限, height != Auto && 有限)
返回 Regions { size, full, backlog, last, expand }
```

`distribute(height, regions, buf)` 把一个固定总高度 `height` 沿 `regions` 的区域序列切成「第一区域高度 + backlog」：

```
remaining = height
若 remaining <= 0: 直接返回 (remaining, 空 backlog)   // 负/零高总在第一区域装下
loop:
  limited = clamp(regions.size.y, 0, remaining)        // 本区域最多吃掉 remaining
  buf.push(limited); remaining -= limited
  若 remaining 近似为空 或 无法再换区域: break
  regions.next()
若仍有剩余: 把它加到最后一个 buf 项（会溢出，但无可奈何）
返回 (buf[0], buf[1..])
```

关键不变量：若定高能完全装进第一区域，则没有 backlog，且第一区域高度收缩为恰好 `height`（注释明确说明）。

`layout_multi_block` 拿到多区域 pod 后按 body 变体排版，返回 `Fragment`，再逐帧做 inset/fill/stroke/clip 后处理。

#### 4.3.3 源码精读

`breakable_pod` 见 [src/flow/block.rs:294-364](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/block.rs#L294-L364)，其中定高分支调用 `distribute` 见 [src/flow/block.rs:323-336](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/block.rs#L323-L336)：

```rust
Sizing::Rel(rel) => {
    let resolved = rel.resolve(styles).relative_to(base.y);
    full = resolved;
    (first, backlog) = distribute(resolved, regions, buf);
    last = None;   // 定高块不要可重复末区
}
```

定高摊分助手 `distribute` 见 [src/flow/block.rs:374-419](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/block.rs#L374-L419)，其循环核心：

```rust
let limited = regions.size.y.clamp(Abs::zero(), remaining);
buf.push(limited);
remaining -= limited;
if remaining.approx_empty() || !regions.may_break()
   || (!regions.may_progress() && limited.approx_empty()) { break; }
regions.next();
```

这里复用了 u2-l2 讲过的 `may_break()`/`may_progress()` 守卫，避免在过小区域上无限循环。

`layout_multi_block` 见 [src/flow/block.rs:107-252](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/block.rs#L107-L252)。它收的是 `Regions`，body 的 `Content` 分支用 `crate::layout_fragment`（多帧），见 [src/flow/block.rs:145-169](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/block.rs#L145-L169)。注意它和 single 形成镜像：**single 用 `layout_frame`，multi 用 `layout_fragment`**。

值得专门一提的是「宽度一致性重排」逻辑 [src/flow/block.rs:152-166](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/block.rs#L152-L166)：自动宽度的可断裂块若产出了多帧且帧间宽度不一致（内容在不同区域里舒展宽度不同），就取最大宽度、开启 `expand.x` 重排一次，保证块在所有区域里宽度统一。这一步是 multi 独有的，single 不需要（single 只有一帧）。

逐帧后处理见 [src/flow/block.rs:217-241](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/block.rs#L217-L241)，用 `fragment.iter_mut().zip(pod.iter())` 把每帧与其对应区域配对，施加 inset/clip/fill/stroke。其中 `skip_first`（[src/flow/block.rs:211-214](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/block.rs#L211-L214)）会在「第一帧空、但后续帧非空」时跳过第一帧的填充/描边/打标，避免给孤立空帧贴上背景或标签而使其「变非空」。

#### 4.3.4 代码实践

1. **实践目标**：理解定高块如何把一个固定高度摊到多个区域。
2. **操作步骤**：阅读 [src/flow/block.rs:374-419](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/block.rs#L374-L419) 的 `distribute` 函数。
3. **需要观察的现象**：假设外部 `regions` 提供三个候选高度 `[100pt, 100pt, 100pt]`（即 `size.y=100`、`backlog=[100]`、`last=100`），块定高 `height: 250pt`。手动走一遍循环：
   - 第 1 轮：`limited=100`，`remaining=150`，push 100；
   - 第 2 轮：`limited=100`，`remaining=50`，push 100；
   - 第 3 轮：`limited=50`（clamp 到 remaining），`remaining≈0`，push 50，break。
4. **预期结果**：`distribute` 返回 `first=100`、`backlog=[100, 50]`，即定高 250pt 被切成 `100 + 100 + 50` 三段。
5. 待本地验证：用 `cargo test` 跑一个定高可断裂块跨两页的用例，在 `distribute` 末尾打印 `buf`，核对与手算一致。

#### 4.3.5 小练习与答案

**练习 1**：定高块为什么要把 `last` 设为 `None`？

**答案**：`last` 是「可无限重复的末区高度」，用于自动高度块在内容超出所有 backlog 时继续往下排。但定高块的总高度已经确定，摊完就是摊完，不应再无限重复末区（否则会排超出块自身高度的内容），所以 `last = None`。

**练习 2**：`layout_multi_block` 的 `Content` 分支末尾那段 `windows(2)` 宽度比较在解决什么问题？

**答案**：自动宽度的可断裂块在不同区域里，内容可能因换行不同而舒展出不同宽度，导致块跨区域时宽度忽大忽小、视觉抖动。这段代码检测「任意相邻两帧宽度不等」，若有则取最大宽度并以 `expand.x=true` 重排，强制所有帧宽度一致。

---

### 4.4 `MultiSpill`：跨区域接力

> ⚠️ 这是本讲最关键、也最容易被误解的模块。请特别注意：`MultiSpill` 保存的**不是「剩余待排内容」本身**，而是「区域历史」，它据此**重新整体排版整个块并跳过已产出的帧**。这个设计是新旧两种区域模型之间的兼容层。

#### 4.4.1 概念说明

回顾 flow 的工作方式（u4-l1/u4-l3）：flow **逐区域**排版，每个 region 调用一次 compose/distribute，`regions.next()` 推进。但 `layout_multi_block`（以及它内部调用的 `layout_fragment`）是**「一次性接收所有 region」**的旧模型——它把传入的整个 `Regions`（含 backlog）一次性吃掉，产出**全部帧**的 Fragment。

冲突来了：flow 每次只递给可断裂块「当前 region」（当前高度 + 后续 backlog），可断裂块排版后可能产出 N 帧，但 flow 这一轮**只能收下第一帧**（当前 region 用），剩下 N−1 帧对应后续 region。怎么把「剩下的帧」留到后续 region？这就是 `MultiSpill` 要解决的问题。

关键洞察是：flow **无法把 N−1 帧原样存起来**，因为后续 region 的具体高度 flow 此刻未必知道，而且块的排版结果依赖完整的区域序列（旧模型里后面的区域会影响前面帧的切分）。因此 `MultiSpill` 采用了「**记下已提交的区域高度，下次重新整体排版、再跳过已用帧**」的策略。

源码注释把这一点说得非常清楚，见 [src/flow/collect.rs:592-599](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/collect.rs#L592-L599)：

> It is a compatibility layer between the old (all regions provided upfront) & new (each region provided on-demand, like an iterator) layout model. This approach is not 100% correct, as in the old model later regions could have an effect on earlier frames, but it's the best we can do for now.

#### 4.4.2 核心流程

整个跨区域接力涉及三处代码，按调用时序：

```
【region R0】distribute.multi(multi):
    pod = 当前 regions
    (frame0, spill?) = multi.layout(pod)        // 产全量 Fragment，取首帧，余者打包成 spill
    work.spill = spill                            // 若有剩余，存进 Work.spill
    work.advance(); return Stop::Finish(false)   // 结束本区域

【region R1】distribute.run():                    // 新区域一开始先处理 spill
    if let Some(spill) = work.spill.take():
        multi_spill(spill)

distribute.multi_spill(spill):
    (frame, spill?) = spill.layout(pod)          // 见下方
    work.spill = spill                            // 仍有剩余则继续接力
    若仍剩: return Stop::Finish(false)
```

`MultiSpill` 结构体保存的是「区域历史」，见 [src/flow/collect.rs:545-552](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/collect.rs#L545-L552)：

```rust
pub struct MultiSpill<'a, 'b> {
    pub(super) exist_non_empty_frame: bool,
    multi: &'b MultiChild<'a>,   // 指回原块，用于重新整体排版
    first: Abs,                  // 首区域高度（不变量）
    full: Abs,
    backlog: Vec<Abs>,           // 已提交的区域高度历史，逐区域累积
    min_backlog_len: usize,      // 防止 backlog 意外收缩
}
```

`MultiSpill::layout`（每个新 region 调一次）的精髓见 [src/flow/collect.rs:556-612](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/collect.rs#L556-L612)：

```
self.backlog.push(当前 region.size.y)             // 把当前区域也记进历史
backlog = self.backlog ⌃ 当前 regions.backlog     // 合并历史 + 外部后续
裁掉末尾与 last 相同的冗余项                        // 防止 backlog 无谓增长、改变哈希
pod = Regions { size:(x, self.first), full:self.full, backlog, last }
fragment = self.multi.layout_full(pod)            // 用合并后的全量区域重新整体排版！
frames = fragment.skip(self.backlog.len())        // 跳过已提交给前序区域的帧
frame = frames.next()                             // 取本区域应得的那一帧
若 frames 还有: 返回 self 作为新 spill            // 仍有剩余，继续接力
```

也就是说，每进入一个新 region，`MultiSpill` 都把「到目前为止所有已访问区域的高度」拼成一个完整的 `Regions`，**让块从头重新排版一遍**，然后用 `skip(self.backlog.len())` 跳过已经在前序区域提交过的帧，只取属于当前区域的那一帧。剩余的帧再打包成新的 spill 传给下一个 region，直到某次排版后没有剩余帧，spill 变 `None`，接力结束。

这个「跳过已用帧」的帧数正好等于 `self.backlog.len()`：因为每提交一个区域，`backlog` 就多一项，而重新排版产出的帧中前 `backlog.len()` 帧正是已经分配给那些区域的。

#### 4.4.3 源码精读

接力在 flow 侧的入口——`Distributor::run` 一开始就优先处理 spill，见 [src/flow/distribute.rs:119-133](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L119-L133)：

```rust
fn run(&mut self) -> FlowResult<()> {
    // First, handle spill of a breakable block.
    if let Some(spill) = self.composer.work.spill.take() {
        self.multi_spill(spill)?;
    }
    while let Some(child) = self.composer.work.head() {
        self.child(child)?;
        self.composer.work.advance();
    }
    Ok(())
}
```

`distribute.multi`（首次遇到可断裂块）见 [src/flow/distribute.rs:354-392](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L354-L392)，核心是把剩余帧存进 `work.spill` 并结束本区域：

```rust
let (frame, spill) = multi.layout(self.composer.engine, pod)?;
if frame.is_empty() && spill.as_ref().is_some_and(|s| s.exist_non_empty_frame)
   && self.regions.may_progress() {
    return Err(Stop::Finish(false));   // 避免区域末尾出现不可见孤儿帧
}
self.frame(frame, multi.align, multi.sticky, true)?;
if let Some(spill) = spill {
    self.composer.work.spill = Some(spill);
    self.composer.work.advance();
    return Err(Stop::Finish(false));   // 块没排完，结束本区域，下区域续排
}
```

注意上面那个「空首帧 + spill 非空」的守卫（[src/flow/distribute.rs:371-379](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L371-L379)）：若当前区域给块的空间太小、首帧为空但后续帧非空，就直接把整块挪到下一区域，避免在本区域留下一个看不见的空帧（「不可见孤儿」）。

`Work.spill` 字段见 [src/flow/mod.rs:304-305](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L304-L305)，它是 `Option<MultiSpill>`；`Work::done()`（[src/flow/mod.rs:345-351](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L345-L351)）会检查 `self.spill.is_none()`——**只要还有未排完的可断裂块，flow 主循环就不会终止**。

`MultiChild::layout`（首次排版、构造 spill）见 [src/flow/collect.rs:457-483](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/collect.rs#L457-L483)，它把首帧与「首区域高度 + 原始 backlog 长度」一起封进 `MultiSpill`：

```rust
let mut frames = fragment.into_iter();
let frame = frames.next().unwrap();
let mut spill = None;
if frames.next().is_some() {
    spill = Some(MultiSpill {
        exist_non_empty_frame,
        multi: self,
        full: regions.full,
        first: regions.size.y,
        backlog: vec![],
        min_backlog_len: regions.backlog.len(),
    });
}
```

#### 4.4.4 代码实践（本讲主实践）

1. **实践目标**：完整说清一个可断裂块跨两个 region 被拆分时，`MultiSpill` 如何保存「剩余待排内容」并在下一 region 续排，直到排完。
2. **操作步骤**：
   - 阅读三段代码：[MultiChild::layout](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/collect.rs#L457-L483)（构造 spill）、[distribute.multi](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L354-L392)（存 spill、结束区域）、[MultiSpill::layout](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/collect.rs#L556-L612)（重新整体排版 + skip 已用帧）。
   - 设想一个会排成 3 帧、跨 3 个 region 的可断裂块。按 region R0/R1/R2 分别写下 `self.backlog`、`skip` 的帧数、返回的 spill 状态。
3. **需要观察的现象**（关键，请逐 region 填写）：
   - **R0**：`multi.layout` 产出 3 帧 `f0,f1,f2`，取 `f0`，`spill` 非空。此时 `spill.backlog = []`、`first = R0 高度`、`min_backlog_len = 原 backlog 长度`。`work.spill = spill`，结束 R0。
   - **R1**：`run` 取出 spill 调 `multi_spill` → `MultiSpill::layout`：先把 `R1 高度` push 进 `self.backlog`（现在 `[R1高]`），合并外部 backlog 成完整区域，`layout_full` 重新整体排版又得 3 帧 `f0',f1',f2'`，`skip(1)` 跳过 `f0'`，取 `f1'`。若 `f2'` 仍在，返回 `spill`，`work.spill = spill`，结束 R1。
   - **R2**：`self.backlog` 再 push `R2 高度`（现在 `[R1高, R2高]`），重新排版得 3 帧，`skip(2)` 跳过前两帧，取 `f2''`。无剩余，spill 为 `None`，接力结束。
4. **预期结果**：你能讲清「`MultiSpill` 的 `backlog` 长度始终等于已提交区域数，因此 `skip(backlog.len())` 恰好跳过已用帧」这一不变量，并指出它保存的是**区域历史**而非内容本身。
5. 待本地验证：在一个跨两页的长块用例里，于 `MultiSpill::layout` 的 `self.backlog.push(...)` 之后与 `skip(...)` 之前分别打印 `self.backlog` 与重排版帧数，核对「skip 数 == 已提交区域数」。

> **正确性边界**：源码注释坦承这一策略并非 100% 正确——在纯粹的旧模型里，后续区域本可能影响较早帧的切分；而这里每次重排版都假设「后续区域对已提交帧无影响」。这是当前新旧模型过渡期的折中。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `MultiSpill` 不直接保存「剩余的 Frame 列表」，而是保存区域历史并每次重排版？

**答案**：因为块排版（`layout_multi_block`/`layout_fragment`）依赖**完整的区域序列**，后续区域的高度会影响前面帧在哪里被切开。flow 是逐区域推进的，第一次排版时往往还不知道所有后续 region 的确切高度，无法一次性定死所有帧；而且把帧「提前存起来」会丢失重排能力（如列平衡、浮动体触发的 relayout）。保存区域历史、每次重新整体排版，能让块始终基于「截至目前最完整的区域信息」做切分，并复用 comemo 缓存（重排版若区域哈希相同会命中缓存）。

**练习 2**：`Work::done()` 为什么要检查 `self.spill.is_none()`？

**答案**：spill 非空意味着有一个可断裂块还没排完、正等着在下一个 region 续排。若此时就判定 done 并终止主循环，块的尾部帧会丢失。所以 `done()` 必须等到 children、spill、floats、footnotes、footnote_spill **全部清空**才算真正完成（见 [src/flow/mod.rs:345-351](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L345-L351)）。

**练习 3**：`MultiSpill::layout` 里那段 `while backlog.len() > min_backlog_len && backlog.last() == regions.last { backlog.pop(); }`（[collect.rs:570-574](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/collect.rs#L570-L574)）在解决什么问题？

**答案**：它裁掉 backlog 末尾「与 `last`（可无限重复的末区高度）相同」的冗余项。因为 `last` 本身就是可无限重复的，显式把它写进 backlog 等价于多写一份，只会让 backlog 无谓增长、改变区域的哈希值，从而**破坏 comemo 缓存命中**。裁掉它能让缓存键更稳定。

## 5. 综合实践

把本讲四个模块串起来，完成下面这个**源码阅读 + 手动追踪**任务。

**场景**：一个文档里只有一个可断裂块，它内部有足够多的内容，会跨 3 个等高区域（每区域高 `H`）排版，即产出 3 帧。外部 flow 提供的 `regions` 为 `size.y=H, backlog=[H], last=H`。

**任务**：

1. **collect 侧**：确认它会被归类为 `Child::Multi`（写出判据），并指出若用户写 `#block(breakable: false)[同样内容]` 会变成哪一类、最终会排成几帧。
2. **block.rs 侧**：写出 `breakable_pod` 为该自动高度块构造的 pod（`first`/`full`/`backlog`/`last`/`expand` 各是什么）。
3. **distribute + MultiSpill 侧**：按下表逐 region 填写 `spill.backlog`、重新排版得到的帧数、`skip` 的帧数、返回给当前 region 的帧、`work.spill` 在区域结束时的状态：

   | region | `spill.backlog`（进入 layout 时） | 重排版帧数 | `skip` 数 | 返回帧 | 区域结束时 `work.spill` |
   |--------|-----------------------------------|-----------|-----------|--------|-------------------------|
   | R0 | （首次走 multi.layout，无 spill） | 3 | 0 | f0 | Some |
   | R1 | ? | ? | ? | ? | ? |
   | R2 | ? | ? | ? | ? | ? |

4. **结论**：用一句话概括 `MultiSpill` 保存的到底是什么，以及它为何不能直接保存剩余帧。

**参考答案要点**：
- (1) `breakable==true` 且 height 非 Fr → `Child::Multi`；若 `breakable:false` → `Child::Single`，因 single 只能产一帧，内容会被压缩/溢出到单帧内（不跨区域）。
- (2) Auto 高度继承 regions：`first=H, full=H, backlog=[H], last=H, expand=(取决于 width)`。
- (3) R1：`backlog=[H]`（push 了 R1 高度 H，但注意首次 spill 的 backlog 起始为空）、重排版 3 帧、`skip(1)`、返回 f1、`work.spill=Some`；R2：`backlog=[H,H]`、重排版 3 帧、`skip(2)`、返回 f2、`work.spill=None`（接力结束）。
- (4) `MultiSpill` 保存的是**已访问区域的「高度历史」**（外加指回原块的引用），而非剩余内容；每次重排版整块并 `skip` 已用帧，是为了在「块需要完整区域序列」与「flow 逐区域推进」两种模型间搭桥——直接存帧会丢失对后续区域高度的响应能力与重排能力。

## 6. 本讲小结

- collect 阶段用 `if !breakable || fr.is_some()` 一条判据把块分成 `SingleChild`（不可断裂）与 `MultiChild`（可断裂）；只有不可断裂块携带 `fr` 字段参与 Fr 高度瓜分。
- `unbreakable_pod` 把 `Sizing` + `inset` + `base` 综合成单 `Region`，`expand` 仅在「显式尺寸且有限」时为真；`layout_single_block` 收单 `Region`、用 `layout_frame`、产**一帧**。
- `breakable_pod` 构造多区域 `Regions`：自动高度直接继承外部 regions，定高则用 `distribute` 把总高度摊成「首区域 + backlog」并置 `last=None`；`layout_multi_block` 收 `Regions`、用 `layout_fragment`、产**多帧 Fragment**，并有「宽度一致性重排」与「跳过孤立空首帧」等 multi 独有逻辑。
- `MultiSpill` 是跨区域接力棒，但保存的是**区域历史**而非剩余内容：每进一个新 region 就把当前高度记进 `backlog`、合并外部 backlog、**重新整体排版整块**、再 `skip(backlog.len())` 跳过已用帧。它是「一次给全 region 的旧块模型」与「逐区域按需的 flow 新模型」之间的兼容层（源码注释自承非 100% 正确）。
- 接力的控制流由 `Stop::Finish(false)` 驱动：`distribute.multi`/`multi_spill` 在块未排完时把 spill 存进 `Work.spill` 并结束本区域；下一区域 `Distributor::run` 一开始就优先消费 spill；`Work::done()` 检查 `spill.is_none()` 保证不丢帧。

## 7. 下一步学习建议

- **u4-l6 列布局 columns 与列平衡**：本讲的 `MultiSpill` 重排版机制与「列平衡」共享同一个底层能力——把 `layout_multi_block`/`distribute` 当作「测量尺」反复重排。学完列平衡后，你会对「重新整体排版」这一手法的普适性有更深的体会。
- **回顾 u2-l2 Regions**：本讲反复出现 `may_break`/`may_progress`/`base`/`full`/`backlog`/`last`，建议带着 block.rs 的实例回头重读 Regions，理解每个字段在真实 layouter 里如何被消费与改写。
- **阅读 `CachedCell`**（[src/flow/collect.rs:671-705](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/collect.rs#L671-L705)）：`SingleChild`/`MultiChild` 用它在 comemo 之外做一层「同输入连续调用复用」的轻量缓存，理解它与 comemo 的分工，能帮你解释为什么 `MultiSpill::layout` 反复调 `layout_full` 却不一定真的反复全量排版。
