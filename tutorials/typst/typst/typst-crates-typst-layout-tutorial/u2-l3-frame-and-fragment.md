# Frame 与 Fragment：排版结果的载体

## 1. 本讲目标

在前两讲里，我们已经认识了排版的「输入」——`Regions`（可用画布）。本讲把镜头转向排版的「输出」：typst-layout 排完一段内容后，到底交出了什么东西。

读完本讲你应当能够：

1. 说清 `Frame` 是什么：它的字段、它的坐标系、它能做哪些变换（`push`/`translate`/`resize`/`push_frame` 等）。
2. 列举 `FrameItem` 的六种变体，理解 `Group` 与 `GroupItem` 如何把 frame 嵌套成树。
3. 区分 `Fragment` 与 `Frame`，掌握 `into_frame`（单帧断言）与 `into_frames`（多帧）的使用场景。
4. 看懂 `pages/finalize.rs` 如何用 `push_frame` 把正文与页眉/页脚/背景拼成一张完整页面。
5. 理解 `Group.parent`（`FrameParent`）如何让「跨多个 frame 的一个逻辑元素」（如表格单元格、脚注）在内省器里保持正确的顺序。

> 本讲的核心心智模型：**排版就是把内容塞进一棵 `Frame` 树**。叶子是 `Text`/`Shape`/`Image`，中间节点是 `Group`，元数据是 `Tag`/`Link`；而 `Fragment` 只是一组同属一次排版的 `Frame`。

## 2. 前置知识

本讲默认你已经具备以下认知（来自依赖讲义 u2-l2）：

- **Regions 是排版的输入**：`Regions` 描述「可用画布序列」，内容可以跨多个区域断裂（`may_break`）。
- **layouter 的三段式**：collect → 排布 → 组装，最后一步组装出的就是本讲的主角 `Frame`。
- **Engine 与 comemo 记忆化模式**（u2-l1）：入口函数把 `Engine` 拆成 `Tracked` 参数后调用 `*_impl`。

此外，两个通俗概念会反复出现：

- **坐标系**：Typst 的页面坐标系原点在**左上角**，x 轴向右、y 轴向下。`Frame` 内每个 item 的位置 `Point` 都相对该 frame 的左上角。
- **内省（introspection）**：Typst 能在排版后「回头查询」文档结构（如 `query`、`counter`、`outline`）。这依赖于排版时被打进 frame 里的 `Tag`，以及一个由全部 pages 派生出的 `PagedIntrospector`（u3-l5 详解）。

一个容易踩的坑：`Frame`、`Fragment`、`FrameItem`、`GroupItem`、`FrameParent` 这些类型**并不定义在 typst-layout 里**，而是定义在兄弟 crate `typst-library` 的 `layout` 模块。typst-layout 只负责**消费和构造**它们。所以本讲会同时引用两个 crate 的源码。

## 3. 本讲源码地图

| 文件 | 所属 crate | 作用 |
| --- | --- | --- |
| `crates/typst-library/src/layout/frame.rs` | typst-library | 定义 `Frame`、`FrameItem`、`GroupItem`、`FrameParent`、`FrameKind`、`Inherit` |
| `crates/typst-library/src/layout/fragment.rs` | typst-library | 定义 `Fragment(Vec<Frame>)` 及其方法 |
| `crates/typst-layout/src/document.rs` | typst-layout | 定义 `Page`（最终产物，核心字段就是 `frame: Frame`） |
| `crates/typst-layout/src/flow/mod.rs` | typst-layout | `layout_frame`（单帧封装）、`layout_flow` 的最终化循环（产出多帧 `Fragment`） |
| `crates/typst-layout/src/pages/finalize.rs` | typst-layout | `finalize`：把正文 frame 与各 marginal 拼成完整页面 frame |
| `crates/typst-layout/src/grid/mod.rs` | typst-layout | `layout_cell`：手动向 frame 注入 `Tag` 并设置 `FrameParent`（本讲实践任务所在） |
| `crates/typst-library/src/introspection/introspector.rs` | typst-library | `start_insertion`/`end_insertion`：解释 parent 顺序的关键 |

**数据流概览**：

```
layouter 内部
   ┌─────────────────────────────────────────────┐
   │  构造 Frame：push(Text/Shape/Image/...)      │
   │            push_frame(子 frame → Group)       │
   │            set_parent(FrameParent) [可选]     │
   └──────────────────┬──────────────────────────┘
                      │  单帧
                      ▼
                 Frame ──────────► layout_frame 返回 Frame
                      │  多帧
                      ▼
                Vec<Frame> ──► Fragment::frames(...) ──► layout_fragment 返回 Fragment
                      │
                      ▼
        pages/finalize.rs：把 inner frame + header/footer/background
        用 push_frame 拼成一张 Page.frame
```

## 4. 核心概念与源码讲解

### 4.1 Frame：排版结果的核心载体（layout.Frame）

#### 4.1.1 概念说明

`Frame` 是 Typst 里「已排版内容的通用容器」。你可以把它想成一张**透明画布**：有固定尺寸、有一条隐含基线、上面贴着若干带坐标的「贴纸」（`FrameItem`）。

它有三个关键性质：

1. **不可变共享（cheap clone）**：`items` 字段是 `Arc<LazyHash<Vec<(Point, FrameItem)>>>`。克隆一个 frame 几乎免费；只有真正修改时才会写时复制（`Arc::make_mut`）。这让 frame 可以在 comemo 缓存、并行排版、多候选测量之间被反复复用。
2. **可哈希（参与 comemo 缓存）**：`#[derive(Clone, Hash)]`。整棵 frame 树的内容决定了它的哈希值，这是记忆化缓存正确的前提。
3. **有两种「硬度」`FrameKind`**：`Soft`（默认，跟随父级尺寸，用于渐变坐标系）与 `Hard`（用自身尺寸，用于 page/block/box）。

#### 4.1.2 核心流程

一个 frame 的生命周期通常是：

```
1. Frame::hard(size) 或 Frame::soft(size)   创建空画布
2. push(pos, item) / push_frame(pos, child)  逐层贴内容
3. translate(offset) / resize(target, align) 必要时整体平移/缩放
4. transform(t) / clip(curve) / set_parent(p) 必要时包一层 Group 做变换
5. 交给上层 layouter 继续组合，或直接作为产物
```

注意第 3、4 步里有一个隐藏动作：`transform`/`clip`/`set_parent` 并不是「原地修改 item」，而是把当前所有内容**包进一个新的 `Group`**，再把这个 group 作为唯一 item 放回 frame（见 4.1.3 的 `group` 辅助函数）。

#### 4.1.3 源码精读

**字段定义**——四个字段撑起整个容器：

```rust
// crates/typst-library/src/layout/frame.rs
pub struct Frame {
    size: Size,                                          // 画布尺寸
    baseline: Option<Abs>,                               // 从顶部量的基线（None=底部）
    items: Arc<LazyHash<Vec<(Point, FrameItem)>>>,       // 带坐标的内容列表
    kind: FrameKind,                                     // Soft / Hard
}
```

完整定义见 [crates/typst-library/src/layout/frame.rs:18-L30](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/frame.rs#L18-L30)（这段定义了 frame 的全部状态）。

**构造**：`hard`/`soft` 只是 `new` 加上不同 `FrameKind`，并断言尺寸有限：

```rust
pub fn new(size: Size, kind: FrameKind) -> Self {
    assert!(size.is_finite());
    Self { size, baseline: None, items: Arc::new(LazyHash::new(vec![])), kind }
}
```

见 [crates/typst-library/src/layout/frame.rs:38-L46](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/frame.rs#L38-L46)（构造空 frame，尺寸必须有限）。

**`push`——最基本的写入**，用 `Arc::make_mut` 触发写时复制：

```rust
pub fn push(&mut self, pos: Point, item: FrameItem) {
    Arc::make_mut(&mut self.items).push((pos, item));
}
```

见 [crates/typst-library/src/layout/frame.rs:161-L163](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/frame.rs#L161-L163)（向前景压入一个 item；`make_mut` 保证不污染共享的克隆）。

**`push_frame`——压入子 frame，自动决定「内联」还是「成组」**，这是理解 frame 嵌套的关键：

```rust
pub fn push_frame(&mut self, pos: Point, frame: Frame) {
    if self.should_inline(&frame) {
        self.inline(self.layer(), pos, frame);
    } else {
        self.push(pos, FrameItem::Group(GroupItem::new(frame)));
    }
}
```

见 [crates/typst-library/src/layout/frame.rs:180-L186](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/frame.rs#L180-L186)（压入子 frame：小而软的内联，大的或硬的包成 Group）。「内联」意味着把子 frame 的 item 直接搬进父 frame（平铺），不再保留 group 边界；判断标准是 `should_inline`：

```rust
fn should_inline(&self, frame: &Frame) -> bool {
    // We do not inline big frames and hard frames.
    frame.kind().is_soft() && (self.items.is_empty() || frame.items.len() <= 5)
}
```

见 [crates/typst-library/src/layout/frame.rs:227-L230](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/frame.rs#L227-L230)（只有「软且不超过 5 个 item」的子 frame 才会被内联，避免 frame 树过深也避免破坏 hard 容器语义）。

**`translate` 与 `resize`——两种最常用的几何操作**：

```rust
pub fn translate(&mut self, offset: Point) {
    if !offset.is_zero() {
        if let Some(baseline) = &mut self.baseline { *baseline += offset.y; }
        for (point, _) in Arc::make_mut(&mut self.items).iter_mut() {
            *point += offset;
        }
    }
}
```

见 [crates/typst-library/src/layout/frame.rs:304-L313](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/frame.rs#L304-L313)（`translate` 把所有 item 坐标和基线一起平移）。

```rust
pub fn resize(&mut self, target: Size, align: Axes<FixedAlignment>) -> Point {
    if self.size == target { return Point::zero(); }
    let offset = align.zip_map(target - self.size, FixedAlignment::position).to_point();
    self.size = target;
    self.translate(offset);
    offset
}
```

见 [crates/typst-library/src/layout/frame.rs:292-L301](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/frame.rs#L292-L301)（`resize` 按 alignment 计算偏移后改尺寸再平移内容，常用于把「fit 出来的小 frame」对齐进更大的区域）。

**`transform`/`clip`/`set_parent` 都走同一个 `group` 包装**——这是「包一层 Group」的实现：

```rust
fn group<F>(&mut self, f: F) where F: FnOnce(&mut GroupItem) {
    let mut wrapper = Frame::soft(self.size);
    wrapper.baseline = self.baseline;
    let mut group = GroupItem::new(std::mem::take(self));   // 原内容掏空成 group
    f(&mut group);                                            // 设置 transform/clip/parent/label
    wrapper.push(Point::zero(), FrameItem::Group(group));
    *self = wrapper;                                          // self 现在只含这一个 group
}
```

见 [crates/typst-library/src/layout/frame.rs:376-L386](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/frame.rs#L376-L386)（`transform`/`clip`/`set_parent` 的共同实现：把原 frame 整体包成一个 Group）。理解了它，你就理解了为何「施加旋转」后 frame 会多出一层 group。

**`FrameKind`——硬度枚举**，决定渐变坐标系参考：

```rust
pub enum FrameKind {
    Soft,   // 默认：跟随父级尺寸
    Hard,   // page/block/box：用自身尺寸
}
```

见 [crates/typst-library/src/layout/frame.rs:459-L470](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/frame.rs#L459-L470)（也正是因为 hard frame 不会被内联，page 这类容器边界才得以保留）。

#### 4.1.4 代码实践

**实践目标**：用源码里的调试工具亲眼「看见」一个 frame。

**操作步骤**：

1. 打开 [crates/typst-library/src/layout/frame.rs:392-L416](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/frame.rs#L392-L416)，阅读 `mark_box_in_place`。它做了两件事：
   - 在 `insert(0, …)` 处插入一个铺满尺寸的半透明青色矩形（`Geometry::Rect`）；
   - 在 `insert(1, …)` 处沿基线插入一条红色水平线（`Geometry::Line`）。
2. 在 `flow/mod.rs` 的 `layout_flow` 循环里（[crates/typst-layout/src/flow/mod.rs:224-L225](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L224-L225)）找到 `finished.push(frame);`，临时改成 `finished.push(frame.mark_box());`（需 `use typst_library::layout::Frame;` 已在作用域）。

**需要观察的现象**：编译运行任意一个简单 `.typ` 文档（例如一段文字 + 一个 block），导出为 PDF/PNG 后，每个被 flow 直接产出的 frame 都会被套上青色底 + 红色基线。

**预期结果**：你会直观看到每个块级区域的尺寸与基线位置；基线红线的 y 坐标即 `frame.baseline()`（默认在底部，除非显式 `set_baseline`）。

> 本实践会临时修改源码。请在本地分支上操作，验证后用 `git checkout -- crates/typst-layout/src/flow/mod.rs` 还原，**不要提交**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Frame::new` 要 `assert!(size.is_finite())`？如果允许无限尺寸会出什么问题？

**参考答案**：frame 的尺寸会直接参与 item 坐标计算、哈希和导出。无限尺寸意味着「画布没有边界」，后续 `resize`/`translate`/`align` 的偏移计算会出现 NaN，哈希也会失效，破坏 comemo 缓存。因此在最底层就拦截。

**练习 2**：`translate` 和 `translate_visual` 的区别是什么？什么场景下必须用后者？

**参考答案**：`translate` 同时移动 item 坐标和基线（`baseline += offset.y`）；`translate_visual` 只移动 item 坐标、**保持基线不变**。当一个变换只改变视觉位置但不应当改变「文字站立的基线」时（例如某些纯视觉位移），必须用 `translate_visual`，否则基线会错位。见 [crates/typst-library/src/layout/frame.rs:304-L322](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/frame.rs#L304-L322)。

### 4.2 FrameItem 与 GroupItem：画布上的内容单元（layout.FrameItem）

#### 4.2.1 概念说明

`FrameItem` 是画布上「贴纸」的类型，一共六种。理解它你就理解了 frame 里能装什么：

| 变体 | 含义 | 是否可内省 |
| --- | --- | --- |
| `Text(TextItem)` | 一段已整形（shaped）的文字 run | 否 |
| `Shape(Shape, Span)` | 几何图形（线、矩形、曲线），带 fill/stroke | 否 |
| `Image(Image, Size, Span)` | 位图/矢量图 | 否 |
| `Link(Destination, Size)` | 内部或外部超链接区域 | 是（Location 链接） |
| `Tag(Tag)` | 内省标记（元素 start/end） | 是 |
| `Group(GroupItem)` | 嵌套子 frame（带变换/裁剪/parent） | 视子内容 |

`Group(GroupItem)` 是唯一的「非叶子」节点，正是它让 frame 成为**树**而非扁平列表。`GroupItem` 携带：

- `frame: Frame` —— 子画布；
- `transform: Transform` —— 对子内容的变换（旋转/缩放/倾斜）；
- `clip: Option<Curve>` —— 裁剪曲线；
- `label: Option<Label>` —— 标签（`<label>`）；
- `parent: Option<FrameParent>` —— 逻辑父级（4.4 详解）。

#### 4.2.2 核心流程

frame 树的遍历方式（也是 `introspect.rs` 里 `discover_frame` 的方式）：

```
对 frame.items() 里的每个 (pos, item):
    Text/Shape/Image    → 纯视觉，内省时跳过
    Link                → 记录链接目标（若是 Location）
    Tag                 → 登记一个元素的 start/end
    Group               → 递归进入 group.frame，并把 pos + group.transform 累积进变换栈
```

关键点：**进入 Group 时要把 group 自身的 `pos` 与 `transform` 一并拼进「当前变换栈」**，这样叶子 item 的全局坐标才正确。

#### 4.2.3 源码精读

**`FrameItem` 枚举定义**：

```rust
pub enum FrameItem {
    Group(GroupItem),               // 子 frame，可带变换/裁剪
    Text(TextItem),                 // 已整形文字
    Shape(Shape, Span),             // 几何图形
    Image(Image, Size, Span),       // 图片
    Link(Destination, Size),        // 链接
    Tag(Tag),                       // 内省标记
}
```

见 [crates/typst-library/src/layout/frame.rs:486-L499](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/frame.rs#L486-L499)（六种 frame 内容变体）。注意 `Shape`/`Image` 带 `Span`（用于诊断定位），而 `Text`/`Tag`/`Link`/`Group` 不直接带 span。

**`GroupItem` 定义**——frame 树的「中间节点」：

```rust
pub struct GroupItem {
    pub frame: Frame,
    pub transform: Transform,
    pub clip: Option<Curve>,
    pub label: Option<Label>,
    pub parent: Option<FrameParent>,
}
```

见 [crates/typst-library/src/layout/frame.rs:516-L530](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/frame.rs#L516-L530)（group 的五个属性：子 frame、变换、裁剪、标签、逻辑父级）。

**内省器遍历 Group 的真实代码**（理解变换栈累积的最佳例子）：

```rust
FrameItem::Group(group) => {
    let ts = ts
        .pre_concat(Transform::translate(pos.x, pos.y))   // 累积 group 自身位置
        .pre_concat(group.transform);                      // 累积 group 变换
    if let Some(parent) = group.parent {
        self.elements.start_insertion();
        self.discover_frame(&group.frame, ts, to_pos);
        self.elements.end_insertion(parent.location);
    } else {
        self.discover_frame(&group.frame, ts, to_pos);
    }
}
```

见 [crates/typst-layout/src/introspect.rs:186-L198](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/introspect.rs#L186-L198)（遍历 group 时把 pos 与 transform 累积进 `ts`；若有 parent 则用 start/end_insertion 包裹）。`Text`/`Shape`/`Image` 在这个 match 里被显式忽略（`=> {}`）。

#### 4.2.4 代码实践

**实践目标**：确认「内联」与「成组」在 frame 树里的实际差异。

**操作步骤**：

1. 回到 [crates/typst-library/src/layout/frame.rs:227-L230](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/frame.rs#L227-L230) 的 `should_inline`。
2. 阅读紧随其后的 `inline` 函数 [crates/typst-library/src/layout/frame.rs:233-L275](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/frame.rs#L233-L275)。注意它有三条快路径：
   - 子 frame 为空 → 直接返回；
   - 偏移为零且父 frame 为空 → 直接 `self.items = frame.items`（整块转移所有权）；
   - 偏移为零 → `splice` 进来，能 `try_unwrap` 就复用，否则克隆。

**需要观察的现象**：跟踪一段调用 `push_frame` 的代码（如 `pages/finalize.rs` 里的页面拼装），判断每次调用走的是 `inline` 分支还是 `Group` 分支。

**预期结果**：`pages/finalize.rs` 里构造的页面 frame 初始为空（`Frame::hard(...)`），第一个压入的 `background`/`header` 因为父 frame 为空且偏移可能为零会触发内联；而 `inner`（正文）frame 通常 item 较多（>5）或为 hard，会走 `Group` 分支。这正是「页面 frame = 一个含若干 group 的 hard 容器」的来源。

> 待本地验证：在 `should_inline` 临时加一行 `eprintln!("inline={} items={}", ..., frame.items.len())`，编译运行后查看日志确认上述判断。

#### 4.2.5 小练习与答案

**练习 1**：`Shape` 和 `Image` 都带 `Span`，而 `Group` 不带。为什么 group 不需要 span？

**参考答案**：`Span` 用于把出错信息指回源码位置，只在「直接由源码某处生成的叶子内容」上有意义。`Group` 是排版过程中因变换/嵌套而产生的结构包装，不对应单一源码位置；它内部各叶子 item 自带 span 即可定位。见 [crates/typst-library/src/layout/frame.rs:486-L499](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/frame.rs#L486-L499)。

**练习 2**：`Frame::hide` 是如何实现的？它保留了什么、丢弃了什么？

**参考答案**：`hide` 用 `retain` 过滤 item：保留所有 `Tag`（元数据不能丢，否则内省会错乱），对 `Group` 递归 `hide` 其子 frame 后若子 frame 变空则丢弃，其余 `Text`/`Shape`/`Image`/`Link` 一律丢弃。见 [crates/typst-library/src/layout/frame.rs:325-L334](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/frame.rs#L325-L334)。这样能「藏起内容但保留查询能力」，是 `hide` / 测量场景的关键。

### 4.3 Fragment：一序列 Frame（layout.Fragment）

#### 4.3.1 概念说明

`Frame` 描述「一张画布」，但很多内容一次排版会**跨多个区域**（多页、多列）——一个可断裂的 block、一段超长段落、一个跨页表格，都会产出不止一个 frame。`Fragment` 就是「一次排版产出的 `Vec<Frame>`」。

它的定义极简，本质上就是 `Vec<Frame>` 的新类型包装（newtype）。它的存在让函数签名能清楚表达「我可能返回多帧」：

```rust
pub struct Fragment(Vec<Frame>);
```

#### 4.3.2 核心流程

`Fragment` 的两条消费路径，对应两个核心方法：

```
layout_fragment(...)  -> Fragment
   │
   ├─ 内容注定单帧（如 layout_frame、数学单 fragment）：
   │     fragment.into_frame()  -> Frame   （断言恰好一帧，否则 panic）
   │
   └─ 内容可能多帧（如 flow、grid、columns）：
         fragment.into_frames() -> Vec<Frame>  （直接取走全部）
```

**判据**（承接 u1-l3）：调用方是否假定「恰好一帧」。`layout_frame` 就是 `layout_fragment(...).map(Fragment::into_frame)`——它用 `into_frame` 的断言来强制「单帧」契约。

#### 4.3.3 源码精读

**`Fragment` 全部方法**——很短，值得通读：

```rust
pub struct Fragment(Vec<Frame>);

impl Fragment {
    pub fn frame(frame: Frame) -> Self { Self(vec![frame]) }      // 单帧构造
    pub fn frames(frames: Vec<Frame>) -> Self { Self(frames) }    // 多帧构造
    pub fn is_empty(&self) -> bool { self.0.is_empty() }
    pub fn len(&self) -> usize { self.0.len() }

    pub fn into_frame(self) -> Frame {                            // 单帧提取（会断言）
        assert_eq!(self.0.len(), 1, "expected exactly one frame");
        self.0.into_iter().next().unwrap()
    }

    pub fn into_frames(self) -> Vec<Frame> { self.0 }             // 多帧提取
    pub fn as_slice(&self) -> &[Frame] { &self.0 }
    pub fn iter(&self) -> ... { self.0.iter() }
}
```

见 [crates/typst-library/src/layout/fragment.rs:7-L57](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/fragment.rs#L7-L57)。重点看 `into_frame` 的断言 [crates/typst-library/src/layout/fragment.rs:33-L37](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/fragment.rs#L33-L37)：`assert_eq!(self.0.len(), 1, "expected exactly one frame")`。

**`layout_frame` 如何用这个断言**：

```rust
pub fn layout_frame(...) -> SourceResult<Frame> {
    layout_fragment(engine, content, locator, styles, region.into())
        .map(Fragment::into_frame)   // 单帧契约在这里落地
}
```

见 [crates/typst-layout/src/flow/mod.rs:42-L51](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L42-L51)（`layout_frame` = `layout_fragment` + `into_frame`，单区域输入理应单帧输出）。

**反例：多帧产出**——`layout_flow` 的最终化循环：

```rust
let mut finished = vec![];
loop {
    let frame = compose(engine, &mut work, &config, locator.next(&()), regions)?;
    finished.push(frame);
    if work.done() && (!regions.expand.y || regions.backlog.is_empty()) {
        break;
    }
    regions.next();
}
Ok(Fragment::frames(finished))   // 多帧：每个区域一帧
```

见 [crates/typst-layout/src/flow/mod.rs:219-L236](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L219-L236)（flow 每个区域 compose 出一个 frame，全部收集成 `Fragment::frames`）。这就是为什么 flow 必须返回 `Fragment` 而非 `Frame`：它天然跨区域。

#### 4.3.4 代码实践

**实践目标**：在源码里统计 `into_frame` 与 `into_frames` 的使用模式，体会两种契约的分工。

**操作步骤**：

1. 在 `crates/typst-layout/src` 下搜索 `into_frame` 与 `into_frames`（可用 IDE 的符号查找）。
2. 把命中点分类：
   - **`into_frame`（单帧契约）**：典型出现在数学元素排版（`math/fraction.rs`、`math/radical.rs`、`math/scripts.rs`、`math/run.rs`）、`math/text.rs`、`flow/block.rs` 的 single-block 路径。这些场景内容**不会跨区域**，所以敢用断言。
   - **`into_frames`（多帧契约）**：出现在 `grid/mod.rs`、`flow/collect.rs`、`flow/compose.rs`（脚注）、`stack.rs`、`lists.rs`、`inline/finalize.rs`。这些场景内容**可能跨区域**。

**需要观察的现象**：哪些 layouter 用 `into_frame`、哪些用 `into_frames`，是否与其「是否可断裂」一致。

**预期结果**：可断裂的（flow、grid、stack、columns、段落）用 `into_frames`；不可断裂的叶子（单个数学结构、single block、unbreakable pod）用 `into_frame`。这与 u2-l2 讲的「pod 因 backlog 为空不可断裂」完全对应。

> 这是「源码阅读型实践」，不需要运行命令；结论可直接从 grep 结果得出（参考本讲 4.4.3 引用的命中行）。

#### 4.3.5 小练习与答案

**练习 1**：如果一段内容实际产出了 2 个 frame，却被人用 `into_frame` 取出，会发生什么？

**参考答案**：`into_frame` 内部 `assert_eq!(self.0.len(), 1, ...)` 会触发 panic，报 "expected exactly one frame"。这正是 `layout_frame` 把「单区域单帧」做成硬契约的方式：一旦调用方误用 `layout_frame` 去排版可断裂内容，会立即暴露而非悄悄出错。见 [crates/typst-library/src/layout/fragment.rs:33-L37](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/fragment.rs#L33-L37)。

**练习 2**：`Fragment` 为什么没有实现 `Hash`，而 `Frame` 实现了？

**参考答案**：comemo 记忆化的缓存键是函数的**参数**。排版函数的返回值（`Fragment`）不需要参与自身哈希；真正进入缓存键的是输入（`Content`、`StyleChain`、`Regions` 等）和被哈希的 `Frame`（当 frame 作为输入参与测量时）。`Fragment` 只是 `Vec<Frame>` 的搬运工，其内部的 `Frame` 已各自可哈希，所以 newtype 不必再派生 `Hash`。见 [crates/typst-library/src/layout/fragment.rs:7](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/fragment.rs#L7) 与 [crates/typst-library/src/layout/frame.rs:17](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/frame.rs#L17)。

### 4.4 从 Fragment 到完整页面：finalize 拼装与 FrameParent 顺序

#### 4.4.1 概念说明

本模块回答两个学习目标：「finalize 如何把多个 frame 拼成完整页面」和「Group/parent 机制如何保证内省顺序」。

**拼装**：一张最终页面 = 正文 frame（`inner`）+ 页眉/页脚/背景/前景（marginal）。这些是各自独立排版的 frame，`pages/finalize.rs` 用 `push_frame` 把它们贴进一个更大的 `Frame::hard(...)` 容器。

**FrameParent**：有些「逻辑元素」在物理上会**散落到多个 frame**里。最典型的就是跨页的表格单元格——同一个 cell 被拆进多个 region，产出多个 frame。内省器按 frame 顺序遍历时，这些分散的 frame 会被错误地「插队」到别的内容之后。`FrameParent` 通过给这些 frame 包一个 `parent`，告诉内省器：「把它们整体当作紧跟在父元素 start tag 之后插入」，从而纠正顺序。

`FrameParent` 本身极小：

```rust
pub struct FrameParent {
    pub location: Location,   // 父元素的 location
    pub inherit: Inherit,     // 子内容是否继承父级样式
}
```

#### 4.4.2 核心流程

**页面 finalize 的拼装顺序**（顺序很关键，影响内省与计数器）：

```
1. 算出是否需要左右互换 margin/bleed（依物理页号 + binding）
2. Frame::hard(inner.size + margin.sum_by_axis)   建整页画布
3. push 所有 tags（Point::zero）                   先登记元数据
4. push_frame(background)   [bleed 原点]            背景
5. push_frame(header)       [margin.left, 0]       页眉
6. push_frame(inner)        [margin.left, margin.top]  正文
7. push_frame(footer)       [margin.left, 底部]     页脚
8. push_frame(foreground)   [bleed 原点]            前景
9. counter.visit(frame)                            收集计数器更新
10. number = counter.logical(); counter.step()     得到逻辑页号并步进
```

**FrameParent 纠正顺序的流程**（以表格单元格为例）：

```
layout_cell 排出一个 cell：
  ├─ 若只产 1 帧：首尾 Tag 直接 prepend/push 进该帧（普通情况）
  └─ 若产 N 帧（跨区域）：
        对每一帧 frame.set_parent(FrameParent(cell_loc, Inherit::Yes))
        把首帧 prepend [Start tag, End tag]（空内容，仅占位）

内省器遍历时遇到带 parent 的 Group：
        start_insertion()              ← 暂存当前已收集序列
        递归 discover_frame(group)     ← 把该 group 的元素收进「暂存区」
        end_insertion(parent.location) ← 把暂存区登记为「插在 parent 之后」
```

#### 4.4.3 源码精读

**页面 finalize 的拼装主体**：

```rust
// 建整页画布（hard 容器）
let mut frame = Frame::hard(inner.size() + margin.sum_by_axis());

// 先登记 tags
for tag in tags.drain(..) {
    frame.push(Point::zero(), FrameItem::Tag(tag));
}

// 背景 → 页眉 → 正文 → 页脚 → 前景（顺序即图层/内省顺序）
if let Some(background) = background {
    frame.push_frame(bleed_origin, background);
}
if let Some(header) = header {
    frame.push_frame(Point::with_x(margin.left), header);
}
frame.push_frame(Point::new(margin.left, margin.top), inner);   // 正文
if let Some(footer) = footer {
    let y = frame.height() - footer.height();
    frame.push_frame(Point::new(margin.left, y), footer);
}
if let Some(foreground) = foreground {
    frame.push_frame(bleed_origin, foreground);
}

counter.visit(engine, &frame)?;          // 收集本页计数器更新
let number = counter.logical();
counter.step();
Ok(Page { frame, bleed, fill, numbering, supplement, number })
```

见 [crates/typst-layout/src/pages/finalize.rs:43-L82](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/finalize.rs#L43-L82)（页面 frame 的拼装：先 tags，再按 background→header→inner→footer→foreground 顺序 `push_frame`，最后计数器步进）。注释 [crates/typst-layout/src/pages/finalize.rs:53-L55](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/finalize.rs#L53-L55) 明确说：「push 的顺序很重要，因为它影响可内省元素的相对顺序，从而影响计数器如何解析」。`Page` 结构里 `frame` 就是这一步的产物，见 [crates/typst-layout/src/document.rs:82-L105](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/document.rs#L82-L105)。

**`grid/mod.rs::layout_cell`——手动注入 Tag + set_parent（本讲核心实践）**：

```rust
// HACK: manually generate tags for table and grid cells. Ideally table and
// grid cells could just be marked as locatable, but the tags are somehow
// considered significant for layouting. This hack together with a check in
// the grid layouter makes the test suite pass.
let mut locator = locator.split();
let mut tags = None;
if let Some(table_cell) = cell.body.to_packed::<TableCell>() { /* 生成 tags */ }
else if let Some(grid_cell) = cell.body.to_packed::<GridCell>() { /* 生成 tags */ }

let locator = locator.next(&cell.body.span());
let fragment = crate::layout_fragment(engine, &cell.body, locator, styles, regions)?;

// Manually insert tags.
let mut frames = fragment.into_frames();
if let Some((elem, loc, key)) = tags
    && let Some((first, remainder)) = frames.split_first_mut()
{
    let flags = TagFlags { introspectable: true, tagged: true };
    if remainder.is_empty() {
        // 单帧：直接首尾 tag
        first.prepend(Point::zero(), FrameItem::Tag(Tag::Start(elem, flags)));
        first.push(Point::zero(), FrameItem::Tag(Tag::End(loc, key, flags)));
    } else {
        // 多帧：每帧设 parent，首帧插入空的首尾 tag
        for frame in &mut frames {
            frame.set_parent(FrameParent::new(loc, Inherit::Yes));
        }
        frames.first_mut().unwrap().prependMultiple([
            (Point::zero(), FrameItem::Tag(Tag::Start(elem, flags))),
            (Point::zero(), FrameItem::Tag(Tag::End(loc, key, flags))),
        ]);
    }
}
Ok(Fragment::frames(frames))
```

见 [crates/typst-layout/src/grid/mod.rs:38-L84](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/mod.rs#L38-L84)（layout_cell 的全部手动 tag 逻辑）。重点看 `set_parent` 调用 [crates/typst-layout/src/grid/mod.rs:74-L76](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/mod.rs#L74-L76) 和首帧空 tag 的注释 [crates/typst-layout/src/grid/mod.rs:67-L73](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/mod.rs#L67-L73)。

**`set_parent` 的实现**——调用 4.1.3 的 `group` 包装：

```rust
pub fn set_parent(&mut self, parent: FrameParent) {
    if !self.is_empty() {
        self.group(|g| g.parent = Some(parent));   // 包一层 Group 并设 parent
    }
}
```

见 [crates/typst-library/src/layout/frame.rs:369-L373](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/frame.rs#L369-L373)（`set_parent` 把整个 frame 包成带 parent 的 group）。

**内省器如何消费 parent**——`start_insertion`/`end_insertion` 用「栈式暂存」实现「整体后插」：

```rust
pub fn start_insertion(&mut self) {
    self.stack.push(std::mem::take(&mut self.sink));   // 把当前输出流暂存
}

pub fn end_insertion(&mut self, parent: Location) {
    let elems = std::mem::replace(                     // 取出暂存区里收集到的元素
        &mut self.sink,
        self.stack.pop().expect("insertion to have been started"),
    );
    self.insertions.insert(parent, elems);             // 登记为「插在 parent 之后」
}
```

见 [crates/typst-library/src/introspection/introspector.rs:562-L574](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/introspector.rs#L562-L574)。配合 `discover_tag` 对 `Tag::Start`/`Tag::End` 的登记 [crates/typst-library/src/introspection/introspector.rs:513-L530](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/introspector.rs#L513-L530)，就能把「分散在多帧的 cell 内容」整体安排到「cell 的 Start tag 紧后方」。

**另一个 set_parent 例子：脚注**（`Inherit::No`，与表格的 `Yes` 形成对照）：

```rust
.map(|mut fragment| {
    for frame in &mut fragment {
        frame.set_parent(FrameParent::new(loc, Inherit::No));
    }
    fragment
})
```

见 [crates/typst-layout/src/flow/compose.rs:655-L660](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L655-L660)（脚注条目设 parent 以排在脚注标记之后，但 `Inherit::No` 表示不继承父级样式——所以脚注内容不会被父级的下划线等样式影响）。

#### 4.4.4 代码实践

**实践目标**（本讲指定任务）：在 `grid/mod.rs::layout_cell` 中找到手动注入 Tag、设置 `FrameParent` 的代码，解释 group/parent 机制在跨多帧单元格中如何保证内省顺序正确。

**操作步骤**：

1. 打开 [crates/typst-layout/src/grid/mod.rs:57-L82](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/mod.rs#L57-L82)。
2. 沿调用链走一遍：
   - `fragment.into_frames()` 把 cell 的排版结果拆成 `frames: Vec<Frame>`（[grid/mod.rs:58](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/mod.rs#L58)）。
   - `split_first_mut()` 区分单帧 / 多帧（[grid/mod.rs:60](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/mod.rs#L60)）。
   - 多帧分支：对每帧 `set_parent(FrameParent::new(loc, Inherit::Yes))`（[grid/mod.rs:74-L76](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/mod.rs#L74-L76)），再给首帧 `prependMultiple` 一对空的 `Start`/`End` tag（[grid/mod.rs:77-L80](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/mod.rs#L77-L80)）。
3. 再打开 [crates/typst-layout/src/introspect.rs:186-L198](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/introspect.rs#L186-L198) 和 [crates/typst-library/src/introspection/introspector.rs:562-L574](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/introspector.rs#L562-L574)，确认消费端。

**需要观察的现象 / 思考题**：

- 假设一个 cell 跨 2 页，产出 `frame_A`（第 1 页）和 `frame_B`（第 2 页）。如果**不**设 parent，内省器会按 frame 顺序遍历：先遇到 `frame_A` 里的 cell 内容，但 `frame_B` 里的内容会被排在「第 1 页里排在 cell 之后的所有元素」之后——于是 cell 的内容被拆散，`query` 出来的顺序与「cell 是一个整体」的直觉相悖。
- 设了 parent 之后：内省器遍历到 `frame_A` 的 group 时 `start_insertion`，把 cell 在 A 帧的内容收进暂存区；到 `frame_B` 的 group 又 `start_insertion` 收进暂存区；最终两批内容都被登记为「紧跟在 cell 的 Start tag 之后」，从而在逻辑上被合并、排在正确位置。

**预期结果**：你能用自己的话讲清这条因果链——「`set_parent` → frame 被包成带 parent 的 group → `discover_frame` 遇到 parent 调用 `start/end_insertion` → 内容被整体登记为插在父 location 之后 → 跨帧 cell 的内省顺序正确」。这正是 [grid/mod.rs:67-L73](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/mod.rs#L67-L73) 注释所说「logical children are currently inserted immediately after the start tag of the parent element」的含义。

> 若想本地验证：构造一个跨多页的表格，对其单元格做 `query`，观察返回顺序是否与表格行顺序一致；再临时注释掉 `set_parent` 那行（仅本地试验），重新编译看查询顺序是否错乱（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：表格单元格设 parent 用 `Inherit::Yes`，脚注设 parent 用 `Inherit::No`。给出一个能体现二者差别的 Typst 例子。

**参考答案**：`Inherit` 控制「子内容是否继承父元素样式」。考虑：

```typ
#underline[
  文字 #footnote[脚注内容].
]
```

脚注条目是脚注标记的「逻辑子级」，但脚注内容**不应**继承父级的 `underline`（否则脚注里所有字都被下划线）。所以脚注用 `Inherit::No`（[compose.rs:657](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L657)）。而表格单元格的样式本就应当随单元格走，故用 `Inherit::Yes`（[grid/mod.rs:75](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/mod.rs#L75)）。`Inherit` 枚举见 [crates/typst-library/src/layout/frame.rs:592-L596](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/frame.rs#L592-L596)，其文档注释里正好给了这两个例子。

**练习 2**：为什么 `layout_cell` 在多帧分支里，给首帧插入的 `Start`/`End` tag 是「空的」（中间没有夹内容）？

**参考答案**：因为真正的 cell 内容分散在多个 frame 里，已经被各自的 `set_parent` 安排成「紧跟父元素 start tag 之后插入」。首帧那对空 tag 只负责**登记 cell 这个元素的 location 区间**（让 `query` 能找到它、让计数器知道它的起止），不承载视觉内容。如果还在首帧 tag 里夹内容，就会和 parent 机制重复登记、破坏顺序。见 [grid/mod.rs:67-L80](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/mod.rs#L67-L80)。

**练习 3**：`pages/finalize.rs` 里 `push` tags 用的是 `Point::zero()`，位置都是 (0,0)。这些 tag 的「位置」最后是如何变成真实页面坐标的？

**参考答案**：tag 的位置在 `discover_frame` 里通过变换栈 `ts` 累积计算（见 [introspect.rs:186-L189](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/introspect.rs#L186-L189)）。虽然这些页面级 tag 压在整页 frame 的 `(0,0)`，但内省器记录的是其相对位置 / 所在页号；对于需要精确坐标的查询，位置会随 frame 被嵌套放置时的 pos 与 transform 一起换算到页面坐标系。页面级 tag 通常只关心「属于哪一页」，不关心页内精确坐标，所以 (0,0) 足矣。

## 5. 综合实践

把本讲四个模块串起来，完成一个「**追踪一个 frame 从诞生到成为页面**」的端到端阅读任务。

**任务**：构造如下文档（心算即可，不必运行）：

```typ
#set page(width: 200pt, height: 60pt, margin: 10pt)
A short line.
```

按以下顺序追踪 frame 的流转，并在每一步标注「是 `Frame` 还是 `Fragment`、用了哪个方法」：

1. 段落排版 `inline/finalize.rs::finalize` 把若干行 `commit` 成 frame，最后 `.map(Fragment::frames)` 返回——这是 `Fragment`（多行可能多帧，见 [inline/finalize.rs:30-L34](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/finalize.rs#L30-L34)）。
2. flow 的 `layout_flow` 循环里 `compose` 产出每区域一个 frame，收集为 `Fragment::frames(finished)`（[flow/mod.rs:224-L236](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L224-L236)）。
3. `pages/run.rs` 把 flow 结果作为 `inner`（连同 header/footer 等装进 `LayoutedPage`）。
4. `pages/finalize.rs::finalize` 用 `Frame::hard(...)` 建整页画布，按 background→header→inner→footer→foreground 顺序 `push_frame`，得到最终的 `Page.frame`（[finalize.rs:43-L82](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/finalize.rs#L43-L82)）。
5. `PagedDocument::new` 把所有 `Page` 收齐，并派生 `PagedIntrospector`（[document.rs:27-L30](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/document.rs#L27-L30)）。

**交付物**：画一张时序图，横轴是「段落 → flow → page run → finalize → document」，纵轴标注每一步的数据类型（`Frame` / `Fragment` / `Vec<Frame>` / `Page` / `PagedDocument`）和关键方法调用（`into_frames` / `Fragment::frames` / `push_frame` / `Frame::hard`）。

**进阶**：在时序图上再标出「如果这段文字恰好跨两页，会在哪一步产生第二个 frame，以及 `layout_cell` 那套 `set_parent` 机制对普通段落是否适用」（答案：普通段落内容不设 parent，因为它不是一个需要被整体查询的「元素」；只有被 `Tag` 标记为可内省的元素跨帧时才需要 parent 纠正）。

## 6. 本讲小结

- **`Frame`** 是排版的通用产物：一张带尺寸、基线、`FrameKind` 的画布，内容是 `Arc<LazyHash<Vec<(Point, FrameItem)>>>`，克隆廉价、可哈希、支持 `push`/`push_frame`/`translate`/`resize`/`transform`/`set_parent` 等操作。
- **`FrameItem`** 有六种：`Text`/`Shape`/`Image`/`Link`/`Tag`/`Group`；其中 `Group(GroupItem)` 是唯一的非叶子节点，让 frame 成为树，携带 transform/clip/label/parent。
- **`Fragment`** = `Vec<Frame>` 的新类型；`into_frame` 带单帧断言（`layout_frame` 用它强制单帧契约），`into_frames` 取全部（可断裂 layouter 用它）。
- **`push_frame` 自动决定内联 vs 成组**：软且 ≤5 个 item 的子 frame 被内联（平铺），否则包成 `Group`。
- **页面 finalize** 用 `Frame::hard` 建整页容器，按 background→header→inner→footer→foreground 的顺序 `push_frame`，这个顺序直接影响内省与计数器解析。
- **`FrameParent` + `set_parent`** 把跨多帧的逻辑元素（表格单元格、脚注）包成带 parent 的 group，配合内省器的 `start/end_insertion` 实现「整体插在父元素 start tag 之后」，纠正跨帧内省顺序；`Inherit` 决定子内容是否继承父级样式。

## 7. 下一步学习建议

本讲补全了「排版输出」一侧。接下来：

1. **u2-l4（Locator、Tag 与内省定位）**：本讲多次提到 `Tag`、`Location`、`FrameParent.location`，下一讲会讲清它们是如何被分配和打进的——这是理解 `set_parent` 为何能纠正顺序的更深一层。
2. **u3-l5（PagedIntrospector）**：本讲只展示了消费 parent 的 `discover_frame` 片段，u3-l5 会完整讲解内省器如何遍历整棵 frame 树、构建查询索引。
3. **u4（flow）与 u6（grid）**：届时你会看到 `Fragment::frames` 与 `into_frame` 在真实 layouter 里的大规模使用，本讲是它们的共同前置。
4. 若想立即上手感受 frame 结构，可阅读 typst 导出端（如 `typst-pdf`、`typst-svg`）如何遍历 `PagedDocument.page.frame` 的 `FrameItem` 树——那是 frame 树最直接的「消费者」视角。
