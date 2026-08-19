# 核心概念：Buffer、Excerpt 与 MultiBuffer 实体

## 1. 本讲目标

学完本讲，你应该能够：

1. 用自己的话说清 **Buffer → Excerpt → MultiBuffer** 这三层抽象各自代表什么、谁包含谁。
2. 解释 `ExcerptRange` 为什么有 `context` 和 `primary` 两个范围，以及它们在「项目搜索」场景里分别对应屏幕上的什么内容。
3. 逐字段说出 `MultiBuffer` 实体（注意是实体，不是快照）里每个字段保管的东西。
4. 解释 **singleton**（单 buffer、单 excerpt）这一特殊形态为什么存在，并列举至少三处因为它而产生的行为分叉。

本讲是全手册的概念地基：后面所有关于坐标系、SumTree、diff 变换的讲义，讨论的都是「把很多 Excerpt 拼起来之后怎么算位置」的问题。如果三层抽象本身不清楚，后面的内容会越读越糊。

## 2. 前置知识

本讲假设你已经读过 u1-l1（项目定位与 crate 结构）。在此基础上，补充三个概念：

**① GPUI 实体（Entity）**。
`MultiBuffer` 是一个 GPUI 实体：`Entity<MultiBuffer>` 是一个指向某块共享状态的句柄，用 `entity.read(cx)` 拿到 `&MultiBuffer`，用 `entity.update(cx, |mb, cx| ...)` 拿到 `&mut MultiBuffer` 并修改它。实体把「可变状态」集中在一个地方管理，这是 Zed 全代码库的通用做法。

**② Buffer 与快照**。
`language::Buffer`（来自 `language` crate）是真正存文本的对象：内部是一棵 rope，支持协作编辑、锚点（`text::Anchor`）、撤销历史等。`Buffer` 同样采用「实体 + 不可变快照」的模式，`buffer.snapshot()` 返回一个可以廉价克隆的 `BufferSnapshot`。本讲只需要知道：**文本真正存在 Buffer 里，MultiBuffer 不复制文本**。

**③ Capability（读写能力）**。
`Capability` 是一个枚举（`ReadWrite` / `ReadOnly`），标记一个 buffer 是否允许被编辑。MultiBuffer 会把它记在自己的字段里，并通过 `read_only()` 暴露出去。

一个贯穿本讲的类比：把 Buffer 想成**原书**，Excerpt 是你从原书上**摘录的一页或几行**（记录「从哪本书、哪一行到哪一行」），MultiBuffer 则是把若干摘录按顺序装订成的**剪报册**。剪报册自己不重新抄写内容——它只保存「摘录清单」，需要全文时按清单去原书取。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `crates/multi_buffer/src/multi_buffer.rs` | crate 主体：实体、快照、坐标系、读取、编辑 | `MultiBuffer` 结构体字段、`Excerpt` / `ExcerptRange`、singleton 构造器、测试构造器 |
| `crates/multi_buffer/src/path_key.rs` | excerpt 的排序身份（`PathKey`）与增删改入口 | `set_excerpts_for_buffer` / `set_excerpts_for_path`：Excerpt 是如何被装进剪报册的 |
| `crates/multi_buffer/src/multi_buffer_tests.rs` | 全部测试，也是最好的实验场 | `test_empty_singleton`、`test_singleton`、`test_set_excerpts_for_buffer` 作为实践参照 |

本讲不涉及 `anchor.rs`（u2-l4 专门讲）和 `transaction.rs`（u2-l11 专门讲）。

## 4. 核心概念与源码讲解

### 4.1 三层抽象：Buffer、Excerpt 与 ExcerptRange 的双范围

#### 4.1.1 概念说明

- **Buffer**：一份完整的文本（通常对应一个文件），由 `language` crate 管理。它是「真相的来源」。
- **Excerpt**：对某个 Buffer 中一段连续范围的**引用**。注意它不是文本的拷贝，而是「buffer id + 范围」这条目录项。剪报册（MultiBuffer）里真正存的就是这些目录项。
- **MultiBuffer**：把若干 Excerpt 按顺序拼接后得到的逻辑文本。读取 `snapshot.text()` 时，才按目录项逐段去底层 Buffer 取内容。

为什么需要 Excerpt 这一层？因为 Zed 的两个核心场景只展示文本的一部分：

- **项目搜索**：一个匹配结果往往只有几行，但要理解它需要上下文，于是每个匹配变成「一个 excerpt，中间高亮命中行，上下各带几行」。
- **git diff 视图**：每个改动 hunk 是一个 excerpt，未改动的部分根本不出现在视图里。

这直接引出 `ExcerptRange` 的双范围设计：

- `context`：excerpt 在 multibuffer 里**实际展示**的全部文本范围。
- `primary`：excerpt 中需要**高亮**的范围（搜索命中处），它总是落在 `context` 内部。

也就是说，`primary` 是语义信息（「用户要找的东西在这里」），`context` 是展示信息（「为了让它可读，我们多带了几行」）。如果你不用高亮功能，`ExcerptRange::new` 会把两者设成同一个范围。

#### 4.1.2 核心流程

从「用户给定的 primary 行范围」到「最终 excerpt 的 context 范围」，由 `build_excerpt_ranges` 按上下文行数 \( c \) 扩展：

\[ \text{start\_row} = \max(0,\ r_{\text{start}} - c), \qquad \text{end\_row} = \min(r_{\text{end}} + c,\ \text{buffer 的最大行}) \]

用伪代码描述整个装配过程：

```text
输入: buffer, primary 行范围列表, 上下文行数 c
1. 对每个 primary 范围，按上式扩展出 context 范围        -> build_excerpt_ranges
2. 把 Point 范围转换成稳定的 text::Anchor 范围           -> set_merged_excerpt_ranges_for_path
3. 按 PathKey 定位，插入/替换 SumTree 中的 Excerpt 节点   -> update_path_excerpts
4. 发出 Event::BufferRangesUpdated 等事件通知下游
```

第 2 步把 `Point`（裸坐标）换成 `Anchor`（锚点）很关键：底层 buffer 之后被编辑时，锚点会跟着文本移动，excerpt 的边界不需要人工修正。这一机制在 u2-l4 展开。

#### 4.1.3 源码精读

先看 Excerpt 本体——它就是剪报册里的一条目录项：

[crates/multi_buffer/src/multi_buffer.rs:823-838](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/multi_buffer.rs#L823-L838)

```rust
pub(crate) struct Excerpt {
    pub(crate) path_key: PathKey,
    pub(crate) path_key_index: PathKeyIndex,
    pub(crate) buffer_id: BufferId,
    /// The range of the buffer to be shown in the excerpt
    pub(crate) range: ExcerptRange<text::Anchor>,
    /// The last row in the excerpted slice of the buffer
    pub(crate) max_buffer_row: BufferRow,
    /// A summary of the text in the excerpt
    pub(crate) text_summary: TextSummary,
    pub(crate) has_trailing_newline: bool,
}
```

要点：

- `buffer_id` + `range` 是「去哪本书、取哪几行」；
- `text_summary` 预先缓存了这段文本的长度摘要（行数、字节数等），这样**不取正文也能做坐标换算**——这是 SumTree 增量维护的基础（u2-l2、u2-l3 展开）；
- `has_trailing_newline` 标记这个 excerpt 后面是否需要补一个换行来和下一个 excerpt 分隔（拼接细节见下文 4.2.3 的旁注）。

再看 `ExcerptRange` 的定义，字段注释把双范围的用途说得很直白：

[crates/multi_buffer/src/multi_buffer.rs:840-858](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/multi_buffer.rs#L840-L858)

```rust
pub struct ExcerptRange<T> {
    /// The full range of text to be shown in the excerpt.
    pub context: Range<T>,
    /// The primary range of text to be highlighted in the excerpt.
    /// In a multi-buffer search, this would be the text that matched the search
    pub primary: Range<T>,
}

impl<T: Clone> ExcerptRange<T> {
    pub fn new(context: Range<T>) -> Self {
        Self { context: context.clone(), primary: context }
    }
}
```

context/primary 的扩展算法在 `build_excerpt_ranges` 中，只有 8 行：

[crates/multi_buffer/src/multi_buffer.rs:3133-3151](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/multi_buffer.rs#L3133-L3151)

```rust
fn build_excerpt_ranges(
    ranges: impl IntoIterator<Item = Range<Point>>,
    context_line_count: u32,
    buffer_snapshot: &BufferSnapshot,
) -> Vec<ExcerptRange<Point>> {
    ranges
        .into_iter()
        .map(|range| {
            let start_row = range.start.row.saturating_sub(context_line_count);
            let start = Point::new(start_row, 0);
            let end_row = (range.end.row + context_line_count).min(buffer_snapshot.max_point().row);
            let end = Point::new(end_row, buffer_snapshot.line_len(end_row));
            ExcerptRange { context: start..end, primary: range }
        })
        .collect()
}
```

注意三个细节：起始侧用 `saturating_sub`（不会越界到负数行）；结束侧与 `max_point().row` 取 `min`（不会越过 buffer 末尾）；context 的起点总是取整到行首（`Point::new(start_row, 0)`），终点取整到行尾（`line_len(end_row)`）——excerpt 总是整行整行地展示，不会切半行。

Range 从 `Point` 换成 `Anchor` 的那一小段在 path_key.rs：

[crates/multi_buffer/src/path_key.rs:321-345](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/path_key.rs#L321-L345)

```rust
pub(crate) fn set_merged_excerpt_ranges_for_path<T>(...) -> (bool, PathKeyIndex) {
    let anchor_ranges = new
        .into_iter()
        .map(|r| ExcerptRange {
            context: buffer_snapshot.anchor_before(r.context.start)
                ..buffer_snapshot.anchor_after(r.context.end),
            primary: buffer_snapshot.anchor_before(r.primary.start)
                ..buffer_snapshot.anchor_after(r.primary.end),
        })
        .collect::<Vec<_>>();
    let inserted =
        self.update_path_excerpts(path.clone(), buffer, buffer_snapshot, &anchor_ranges, cx);
    ...
}
```

`anchor_before` / `anchor_after` 分别偏向范围的左侧/右侧，保证相邻 excerpt 的边界锚点不会因为一次插入而「串门」。

最后看装配入口 `set_excerpts_for_path`（`set_excerpts_for_buffer` 只是先用 `PathKey::for_buffer` 算出排序键再转调它）：

[crates/multi_buffer/src/path_key.rs:68-99](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/path_key.rs#L68-L99)

```rust
pub fn set_excerpts_for_path(
    &mut self, path: PathKey, buffer: Entity<Buffer>,
    ranges: impl IntoIterator<Item = Range<Point>>,
    context_line_count: u32, cx: &mut Context<Self>,
) -> bool {
    let buffer_snapshot = buffer.read(cx).snapshot();
    let excerpt_ranges = build_excerpt_ranges(ranges, context_line_count, &buffer_snapshot);
    let merged = Self::merge_excerpt_ranges(&excerpt_ranges);
    let (inserted, _path_key_index) =
        self.set_merged_excerpt_ranges_for_path(path, buffer, &buffer_snapshot, merged, cx);
    inserted
}
```

它就是 4.1.2 伪代码第 1–3 步的直译。完整调用链（合并重叠范围、维护 SumTree、发事件）属于 u2-l6 的主题，本讲只需建立「Excerpt 是被这样装进去的」的印象。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 `context_line_count` 如何把一个 primary 范围扩展成更大的 context 范围。

**操作步骤**（示例代码，需添加到 `crates/multi_buffer/src/multi_buffer_tests.rs` 末尾，该文件是 `#[cfg(test)]` 模块，实践后可还原）：

```rust
#[gpui::test]
fn test_excerpt_context_expansion(cx: &mut App) {
    // 5 行文本，末尾带换行：max_point 是 Point(5, 0)
    let buffer = cx.new(|cx| Buffer::local("aaa\nbbb\nccc\nddd\neee\n", cx));
    let multibuffer = cx.new(|_| MultiBuffer::new(Capability::ReadWrite));

    // primary 选最后一行（row 4），上下文 1 行：
    // start_row = 4-1 = 3；end_row = min(5+1, 5) = 5 → context 覆盖 row 3..=5
    multibuffer.update(cx, |mb, cx| {
        mb.set_excerpts_for_buffer(buffer.clone(), vec![Point::row_range(4..5)], 1, cx);
    });

    let snapshot = multibuffer.read(cx).snapshot(cx);
    // context 到达 buffer 末尾，自然带上了原有的换行符
    assert_eq!(snapshot.text(), "ddd\neee\n");

    // 换成中间行（row 1），打印观察 context 向上下各扩一行后的样子
    multibuffer.update(cx, |mb, cx| {
        mb.set_excerpts_for_buffer(buffer.clone(), vec![Point::row_range(1..2)], 1, cx);
    });
    let snapshot = multibuffer.read(cx).snapshot(cx);
    println!("middle excerpt text: {:?}", snapshot.text());
    let ranges: Vec<_> = snapshot.excerpts().collect();
    println!("excerpt ranges (anchor form): {} 个", ranges.len());
}
```

**需要观察的现象**：

1. 第一组断言通过：展示的是 `ddd\neee\n` 而不是只有 `eee`——context 比primary 多了整整一行。
2. 第二组打印中，primary 只有 `bbb` 一行，但文本里出现了 `aaa` 和 `ccc`。
3. 打印出的 excerpt 范围是 `ExcerptRange<text::Anchor>`（`snapshot.excerpts()` 的返回类型），印证「装进树里的是锚点范围」。

**预期结果**：断言通过；中间行的 excerpt 文本按 4.1.2 的公式应覆盖 row 0..=3（`aaa\nbbb\nccc\nddd`），末尾是否恰好多一个换行符取决于 `has_trailing_newline` 的拼接逻辑（整本剪报册只有这一个 excerpt 时它是最后一项，标记为假），精确的字节级输出**待本地验证**——把你观察到的记下来，u2 讲 SumTree 摘要时会用到这个现象。

#### 4.1.5 小练习与答案

**练习 1**：buffer 共 100 行（0..=99），primary 是 `Point::row_range(10..12)`，`context_line_count = 5`。context 覆盖哪些行？

答案：start_row = 10 − 5 = 5；end_row = min(12 + 5, 100) = 17。context 覆盖 row 5..=17（起始取整到 row 5 行首，结束取到 row 17 行尾）。

**练习 2**：为什么 `primary` 用 `Range<Point>` 传入、却在存进 `Excerpt` 之前换成 `Range<text::Anchor>`？

答案：`Point` 是某一时刻的裸坐标，buffer 一旦被编辑就失效；`Anchor` 是随文本移动的稳定引用。Excerpt 会长期挂在 multibuffer 里、跨越无数次编辑，只有锚点能保证「我指的是这段话」这个语义不变。转换发生在 `set_merged_excerpt_ranges_for_path`（path_key.rs:332-340）。

**练习 3**：`ExcerptRange::new(range)` 构造出的对象，context 和 primary 有什么关系？什么场景会这么用？

答案：两者相同（primary 就是 context 的克隆）。适用于不需要区分高亮区的场景，比如 git diff 视图里整个 hunk 都是要展示的内容。测试构造器 `build_multi` 用的就是它（multi_buffer.rs:3168-3171）。

### 4.2 MultiBuffer 实体：字段精读

#### 4.2.1 概念说明

u1-l1 已经建立「实体与快照分离」的印象，这里落到字段层面。`MultiBuffer` 实体保管的是**可变的工作状态**：当前有哪些 buffer、是否处于 singleton 形态、标题是什么、谁在订阅变化；而 `MultiBufferSnapshot` 保管的是**某一时刻拼接结果的只读视图**。实体身上的 `snapshot: RefCell<MultiBufferSnapshot>` 字段就是这两者的连接点——取快照时先做一次惰性同步，再克隆出来。

为什么这样设计？因为快照是不可变的，下游（编辑器渲染一帧、搜索扫描一遍）可以拿着快照在任意线程/任意长时间里慢慢读，不必担心读到一半被改。代价是实体必须维护「快照什么时候过期」的信息，这就是 `buffer_changed_since_sync` 标志位的职责。

#### 4.2.2 核心流程

先给出字段一览表（对照源码 74-93 行）：

| 字段 | 类型 | 保管的内容 |
| --- | --- | --- |
| `snapshot` | `RefCell<MultiBufferSnapshot>` | 最近一次同步得到的只读拼接视图，取用时可能触发重算 |
| `buffers` | `BTreeMap<BufferId, BufferState>` | 参与拼接的底层 buffer 实体及其事件订阅 |
| `diffs` | `HashMap<BufferId, DiffState>` | buffer → 其 git diff 状态（u3 展开） |
| `subscriptions` | `Topic<MultiBufferOffset>` | 订阅者名单，快照变化时向他们发布增量 `Edit` 流 |
| `singleton` | `bool` | 是否为「单 buffer 单 excerpt」形态（4.3 节主角） |
| `history` | `History` | 跨 buffer 的撤销/重做历史（u2-l11 展开） |
| `title` | `Option<String>` | 显式标题；`None` 时从路径或内容推导 |
| `capability` | `Capability` | 读写能力 |
| `buffer_changed_since_sync` | `Rc<Cell<bool>>` | 脏标志：底层 buffer 变了但快照还没重算 |

数据流向可以这样描述：

```text
底层 Buffer 被编辑
  -> BufferState 里的订阅触发 on_buffer_event（记为"脏"）
  -> 某人调用 multibuffer.snapshot(cx) / read(cx)
  -> sync(): 若 buffer_changed_since_sync 为真
       -> sync_from_buffer_changes() 重建快照中的各棵树
       -> subscriptions.publish(edits) 把增量编辑发给订阅者
  -> 返回最新快照的克隆
```

#### 4.2.3 源码精读

结构体定义（含官方文档注释）：

[crates/multi_buffer/src/multi_buffer.rs:71-93](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/multi_buffer.rs#L71-L93)

```rust
/// One or more [`Buffers`](Buffer) being edited in a single view.
///
/// See <https://zed.dev/features#multi-buffers>
pub struct MultiBuffer {
    snapshot: RefCell<MultiBufferSnapshot>,
    buffers: BTreeMap<BufferId, BufferState>,
    diffs: HashMap<BufferId, DiffState>,
    subscriptions: Topic<MultiBufferOffset>,
    singleton: bool,
    history: History,
    title: Option<String>,
    capability: Capability,
    buffer_changed_since_sync: Rc<Cell<bool>>,
}
```

`buffers` 里的值类型 `BufferState` 很薄，实体的引用 + 两个订阅：

[crates/multi_buffer/src/multi_buffer.rs:504-513](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/multi_buffer.rs#L504-L513)

```rust
struct BufferState {
    buffer: Entity<Buffer>,
    _subscriptions: [gpui::Subscription; 2],
}

struct DiffState {
    diff: Entity<BufferDiff>,
    main_buffer: Option<Entity<language::Buffer>>,
    _subscription: gpui::Subscription,
}
```

两个订阅分别对应「buffer 有变化需重绘」和「buffer 事件需处理」——它们在 buffer 被装入 multibuffer 时创建（`update_path_excerpts` 里 `self.buffers.insert(...)` 时），这是 4.2.2 流程图中第一环的物理实现。

两个构造器把「字段默认值」固化下来，注意它们对快照初始标志位的不同选择：

[crates/multi_buffer/src/multi_buffer.rs:1206-1260](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/multi_buffer.rs#L1206-L1260)

```rust
pub fn new(capability: Capability) -> Self {
    Self::new_(capability, MultiBufferSnapshot {
        show_headers: true,
        show_deleted_hunks: true,
        ..MultiBufferSnapshot::default()
    })
}

pub fn without_headers(capability: Capability) -> Self { /* show_headers: false */ }

pub fn singleton(buffer: Entity<Buffer>, cx: &mut Context<Self>) -> Self { /* 见 4.3 */ }

pub fn new_(capability: Capability, snapshot: MultiBufferSnapshot) -> Self {
    Self {
        snapshot: RefCell::new(snapshot),
        buffers: Default::default(),
        diffs: HashMap::default(),
        subscriptions: Topic::default(),
        singleton: false,
        capability,
        title: None,
        buffer_changed_since_sync: Default::default(),
        history: History::default(),
    }
}
```

惰性同步的入口只有几行，但它是「实体→快照」这座桥的全部：

[crates/multi_buffer/src/multi_buffer.rs:1321-1331](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/multi_buffer.rs#L1321-L1331)

```rust
/// Returns an up-to-date snapshot of the MultiBuffer.
pub fn snapshot(&self, cx: &App) -> MultiBufferSnapshot {
    self.sync(cx);
    self.snapshot.borrow().clone()
}

pub fn read(&self, cx: &App) -> Ref<'_, MultiBufferSnapshot> {
    self.sync(cx);
    self.snapshot.borrow()
}
```

`sync` 本体：脏标志没置位就直接返回（零成本），置位了才重建：

[crates/multi_buffer/src/multi_buffer.rs:2480-2494](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/multi_buffer.rs#L2480-L2494)

```rust
fn sync(&self, cx: &App) {
    let changed = self.buffer_changed_since_sync.replace(false);
    if !changed {
        return;
    }
    let edits = Self::sync_from_buffer_changes(
        &mut self.snapshot.borrow_mut(),
        &self.buffers,
        &self.diffs,
        cx,
    );
    if !edits.is_empty() {
        self.subscriptions.publish(edits);
    }
}
```

实体还有一对容易混淆的 `is_empty`，语义并不相同：

[crates/multi_buffer/src/multi_buffer.rs:1361-1369](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/multi_buffer.rs#L1361-L1369)

```rust
pub fn len(&self, cx: &App) -> MultiBufferOffset {
    self.read(cx).len()
}

pub fn is_empty(&self) -> bool {
    self.buffers.is_empty()
}
```

实体的 `is_empty()` 问的是「**有没有底层 buffer**」；而快照的 `is_empty()`（multi_buffer.rs:4136-4138）问的是「**拼接后的文本长度是否为 0**」——它看的是 `diff_transforms` 树的输出摘要。对普通场景两者结论一致，但「装了一个空 buffer」时实体 `is_empty()` 为假、快照 `is_empty()` 为真。区分这两层语义是本讲的一个小考点。

旁注（excerpt 拼接的换行处理）：SumTree 里每个 `Excerpt` 的摘要会在 `has_trailing_newline` 为真时补上一个 `"\n"`，见 [crates/multi_buffer/src/multi_buffer.rs:7368-7385](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/multi_buffer.rs#L7368-L7385)；而整棵树的最后一个 excerpt 会被去掉这个标记（[crates/multi_buffer/src/path_key.rs:495-514](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/path_key.rs#L495-L514)），保证剪报册末尾不会凭空多一个空行。这就是 4.1.4 实践里让你观察的现象背后的机制。

#### 4.2.4 代码实践

**实践目标**：验证三种构造路径得到的状态差异，并区分两层 `is_empty` 的语义。

**操作步骤**（示例代码，添加到 `multi_buffer_tests.rs`）：

```rust
#[gpui::test]
fn test_entity_state_after_construction(cx: &mut App) {
    // 路径一：空的多 buffer
    let empty = cx.new(|_| MultiBuffer::new(Capability::ReadWrite));
    assert!(empty.read(cx).is_empty());              // 实体层：buffers 映射为空
    assert!(empty.read(cx).snapshot(cx).is_empty()); // 快照层：拼接文本长度为 0
    assert!(!empty.read(cx).read_only());            // ReadWrite 可编辑

    // 路径二：装一个内容为空的 buffer —— 两层 is_empty 开始分叉
    let blank_buffer = cx.new(|cx| Buffer::local("", cx));
    let with_blank = cx.new(|_| MultiBuffer::new(Capability::ReadWrite));
    with_blank.update(cx, |mb, cx| {
        mb.set_excerpts_for_buffer(
            blank_buffer.clone(),
            vec![Point::new(0, 0)..Point::new(0, 0)],
            0,
            cx,
        );
    });
    assert!(!with_blank.read(cx).is_empty());              // 有 buffer 了
    assert!(with_blank.read(cx).snapshot(cx).is_empty());  // 但文本长度仍是 0

    // 路径三：只读能力
    let readonly = cx.new(|_| MultiBuffer::new(Capability::ReadOnly));
    assert!(readonly.read(cx).read_only());
}
```

**需要观察的现象**：路径二的两个断言一个假一个真，正是「实体 is_empty = 有没有 buffer」与「快照 is_empty = 文本是否为零长」的分叉点。

**预期结果**：全部断言通过（其中路径二的分叉行为依据源码 1367-1369 行与 4136-4138 行推导；如运行结果不符，优先检查你添加的 excerpt 范围是否真的为空范围）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `snapshot` 字段用 `RefCell` 包起来，而不是直接存 `MultiBufferSnapshot`？

答案：`snapshot(&self)` 和 `read(&self)` 都只用 `&self`（GPUI 的读取路径不允许拿 `&mut self`），但同步时需要把过期快照替换/重建掉，这是内部可变性需求，所以用 `RefCell`。同时 `read` 返回 `Ref<'_, MultiBufferSnapshot>` 让调用方短期借用，避免每次读取都克隆整棵树。

**练习 2**：`buffer_changed_since_sync` 为什么是 `Rc<Cell<bool>>` 而不是普通的 `bool`？

答案：它需要被共享给底层 buffer：buffer 通过 `record_changes(Rc::downgrade(&buffer_changed_since_sync))` 拿到它的弱引用，在自己的变更记录里顺手把这个标志置真（见 `clone` 方法中 multi_buffer.rs:1264-1268 的用法）。普通 `bool` 没法跨对象共享，`Cell` 则提供了单线程下的内部可变性。

**练习 3**：`Event` 枚举（multi_buffer.rs:98-127）里哪个变体携带了 `ExcerptRange`？这说明了什么？

答案：`Event::BufferRangesUpdated { buffer, path_key, ranges: Vec<ExcerptRange<text::Anchor>> }`。说明下游（如搜索结果面板）是通过事件拿到「这次装入了哪些片段、primary 高亮在哪」的——`ExcerptRange` 不只是内部数据结构，也是实体对外 API 的一部分。

### 4.3 singleton：单 buffer 单 excerpt 的特殊形态

#### 4.3.1 概念说明

打开一个普通文件编辑时，视图里只有一个 buffer、一个覆盖全文的 excerpt。如果这条最 frequent 的路径也要走完整的「多 excerpt 拼接」逻辑（SumTree 游标、excerpt 边界、逐段坐标换算），就是在为 99% 的场景支付 1% 场景才需要的复杂度。于是 `MultiBuffer` 用一个 `singleton: bool` 标志位开出一条快路径：

- **是 singleton**：大量查询可以直接下推给唯一的底层 buffer 快照，跳过多片段拼接逻辑；
- **不是 singleton**：走通用多 excerpt 路径。

这就是你在 u1-l1 见过的「行为分叉」。它是一种典型的工程取舍：用少量的 `if self.singleton` 分支，换取最常见场景的常数级加速和 API 简化（例如 `as_singleton()` 让下游能直接拿到底层 `Buffer` 实体去调用只有 Buffer 才有的方法）。

值得强调的是：singleton **不是**一种不同的数据结构。它依然是一个装着一个 excerpt 的 multibuffer——「单 buffer 单 excerpt」只是让很多通用算法退化为平凡情况，于是代码可以走捷径。

#### 4.3.2 核心流程

singleton 的构造只有三步：

```text
MultiBuffer::singleton(buffer, cx)
  1. new_(): 以 singleton: true 的初始快照创建实体
  2. this.singleton = true            （实体层标志）
  3. set_excerpts_for_path(
         PathKey::sorted(0),           （空的排序路径，序号 0）
         buffer,
         [Point::zero() .. buffer.max_point()],   （覆盖全文的唯一 excerpt）
         0,                            （不需要上下文扩展）
     )
```

行为分叉的三个代表性位置（后续讲义会遇到更多）：

| 位置 | singleton 时的行为 |
| --- | --- |
| `MultiBuffer::title`（multi_buffer.rs:2169-2187） | 标题直接取该 buffer 的文件名或首行内容 |
| `MultiBuffer::set_group_interval`（multi_buffer.rs:1297-1306） | 撤销分组间隔同时下推给底层 buffer |
| `MultiBufferSnapshot::excerpt_boundaries_in_range`（multi_buffer.rs:5502-5507） | 迭代器直接返回 `None`——没有 excerpt 边界可言 |

#### 4.3.3 源码精读

构造器本体：

[crates/multi_buffer/src/multi_buffer.rs:1227-1245](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/multi_buffer.rs#L1227-L1245)

```rust
pub fn singleton(buffer: Entity<Buffer>, cx: &mut Context<Self>) -> Self {
    let mut this = Self::new_(
        buffer.read(cx).capability(),
        MultiBufferSnapshot {
            singleton: true,
            show_deleted_hunks: true,
            ..MultiBufferSnapshot::default()
        },
    );
    this.singleton = true;
    this.set_excerpts_for_path(
        PathKey::sorted(0),
        buffer.clone(),
        [Point::zero()..buffer.read(cx).max_point()],
        0,
        cx,
    );
    this
}
```

注意它复用了 4.1 节的通用装配入口 `set_excerpts_for_path`——singleton 并没有一套独立的建树代码，只是喂给通用机制一组「恰好只有一个、且覆盖全文」的范围。capability 也直接继承自底层 buffer（只读文件打开的 multibuffer 自然是只读的）。

实体层的判别与取值：

[crates/multi_buffer/src/multi_buffer.rs:1333-1343](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/multi_buffer.rs#L1333-L1343)

```rust
pub fn as_singleton(&self) -> Option<Entity<Buffer>> {
    if self.singleton {
        Some(self.buffers.values().next().unwrap().buffer.clone())
    } else {
        None
    }
}

pub fn is_singleton(&self) -> bool {
    self.singleton
}
```

快照层有对应的一对，返回的是底层 `BufferSnapshot` 的引用：

[crates/multi_buffer/src/multi_buffer.rs:4116-4126](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/multi_buffer.rs#L4116-L4126)

```rust
pub fn is_singleton(&self) -> bool {
    self.singleton
}

pub fn as_singleton(&self) -> Option<&BufferSnapshot> {
    if self.is_singleton() {
        Some(self.excerpts.first()?.buffer_snapshot(&self))
    } else {
        None
    }
}
```

标题推导里的分叉——singleton 才有机会用文件名/首行当标题，否则落到默认值 `untitled`：

[crates/multi_buffer/src/multi_buffer.rs:2169-2187](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/multi_buffer.rs#L2169-L2187)

```rust
pub fn title<'a>(&'a self, cx: &'a App) -> Cow<'a, str> {
    if let Some(title) = self.title.as_ref() {
        return title.into();
    }

    if let Some(buffer) = self.as_singleton() {
        let buffer = buffer.read(cx);
        if let Some(file) = buffer.file() {
            return file.file_name(cx).into();
        }
        if let Some(title) = self.buffer_content_title(buffer) {
            return title;
        }
    };

    Self::DEFAULT_TITLE.into()
}
```

（显式 `set_title`/`with_title` 优先 → singleton 时用文件名或首行 → 兜底 `untitled`。推导细节是 u3-l8 的主题。）

测试构造器 `build_simple` 就是「本地 buffer + singleton」的一行封装：

[crates/multi_buffer/src/multi_buffer.rs:3153-3158](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/multi_buffer.rs#L3153-L3158)

```rust
#[cfg(any(test, feature = "test-support"))]
impl MultiBuffer {
    pub fn build_simple(text: &str, cx: &mut gpui::App) -> Entity<Self> {
        let buffer = cx.new(|cx| Buffer::local(text, cx));
        cx.new(|cx| Self::singleton(buffer, cx))
    }
```

现有的 `test_singleton` 展示了 singleton 最核心的性质——快照文本与底层 buffer 完全一致，且底层编辑后快照跟随：

[crates/multi_buffer/src/multi_buffer_tests.rs:41-60](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/multi_buffer_tests.rs#L41-L60)

```rust
#[gpui::test]
fn test_singleton(cx: &mut App) {
    let buffer = cx.new(|cx| Buffer::local(sample_text(6, 6, 'a'), cx));
    let multibuffer = cx.new(|cx| MultiBuffer::singleton(buffer.clone(), cx));

    let snapshot = multibuffer.read(cx).snapshot(cx);
    assert_eq!(snapshot.text(), buffer.read(cx).text());
    ...
    buffer.update(cx, |buffer, cx| buffer.edit([(1..3, "XXX\n")], None, cx));
```

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：亲手构造 singleton 与非 singleton 两种形态，验证判别方法与快照行为。

**操作步骤**（示例代码，添加到 `multi_buffer_tests.rs`；运行 `cargo test -p multi_buffer test_two_shapes_of_multibuffer`）：

```rust
#[gpui::test]
fn test_two_shapes_of_multibuffer(cx: &mut App) {
    // 形态一：singleton —— build_simple 内部调用 MultiBuffer::singleton
    let singleton = MultiBuffer::build_simple("hello", cx);
    assert!(singleton.read(cx).is_singleton());
    assert_eq!(
        singleton.read(cx).as_singleton().unwrap().read(cx).text(),
        "hello"
    );
    // 快照文本与底层 buffer 一致
    assert_eq!(singleton.read(cx).snapshot(cx).text(), "hello");

    // 形态二：空 multibuffer —— MultiBuffer::new 从零开始
    let empty = cx.new(|_| MultiBuffer::new(Capability::ReadWrite));
    assert!(!empty.read(cx).is_singleton());
    assert!(empty.read(cx).as_singleton().is_none());
    assert!(empty.read(cx).snapshot(cx).is_empty());
    // 注意：这里不需要 cx，因为实体层 is_empty 只看 buffers 映射
    assert!(empty.read(cx).is_empty());

    // 形态三：装了两个 buffer 的多 excerpt 形态
    let multi = MultiBuffer::build_multi(
        [
            ("abc\n", vec![Point::new(0, 0)..Point::new(1, 0)]),
            ("def\n", vec![Point::new(0, 0)..Point::new(1, 0)]),
        ],
        cx,
    );
    assert!(!multi.read(cx).is_singleton());
    assert!(multi.read(cx).as_singleton().is_none());
    assert_eq!(multi.read(cx).snapshot(cx).text(), "abc\ndef\n");
}
```

**需要观察的现象**：

1. 三种形态下 `is_singleton()` / `as_singleton()` 的返回值逐一符合预期。
2. singleton 的快照文本就是底层 buffer 全文；多 excerpt 形态的快照文本是两个片段的拼接。
3. `empty.read(cx).is_empty()` 不需要 `cx` 参数（对比 `snapshot(cx).is_empty()`），从签名上就能看出两者数据来源不同。

**预期结果**：全部断言通过。若 `build_multi` 一段的文本断言失败，打印 `multi.read(cx).snapshot(cx).text()` 对照——两个 excerpt 各自天然以换行结尾，拼接结果应为 `abc\ndef\n`（此断言依据 4.2.3 旁注的换行规则推导，**待本地验证**）。

#### 4.3.5 小练习与答案

**练习 1**：`MultiBuffer::singleton` 为什么必须调用 `set_excerpts_for_path` 装入一个范围，而不是让空树 + 标志位直接代表「整个 buffer」？

答案：因为 singleton 不是另一套数据结构。快照的文本读取、坐标换算都走同一条「遍历 excerpts 树」的通用路径；树上必须真的有一个覆盖 `Point::zero()..max_point()` 的 excerpt，通用算法才能正确工作。标志位只负责让部分查询可以走捷径（如 `as_singleton` 直接返回底层 buffer）。

**练习 2**：给出两种「文本长度为 0 但 `is_singleton()` 分别为真和假」的构造方式。

答案：为真——`MultiBuffer::build_simple("", cx)`（对空 buffer 建 singleton，参照 `test_empty_singleton`，multi_buffer_tests.rs:21-39）；为假——`MultiBuffer::new(Capability::ReadWrite)`（空的多 buffer）。加上 4.2.4 的「装一个空 buffer」，可以组合出实体/快照两层 `is_empty` 与 `is_singleton` 的完整真值表。

**练习 3**：`MultiBufferSnapshot::as_singleton`（multi_buffer.rs:4120-4126）返回 `Option<&BufferSnapshot>`，实体版（multi_buffer.rs:1333-1339）返回 `Option<Entity<Buffer>>`。为什么返回类型不同？

答案：快照是无生命周期的只读视图，只能给出内部 `BufferSnapshot` 的引用；实体版运行在 GPUI 上下文里，返回 `Entity<Buffer>` 句柄让调用方随后可以 `read`/`update` 这个 buffer——下游（如 editor）经常需要拿到实体去做进一步操作（例如 `is_parsing` 在 multi_buffer.rs:2249-2252 直接 `as_singleton().unwrap()` 下推）。

## 5. 综合实践

把本讲三个模块串起来：**同一个底层 buffer，两种装法**。

**任务**：在 `multi_buffer_tests.rs` 中新增一个测试 `test_same_buffer_two_shapes`：

1. 用 `cx.new(|cx| Buffer::local("fn a() {}\nfn b() {}\nfn c() {}\n", cx))` 创建一个 buffer。
2. 装法 A：`MultiBuffer::singleton(buffer.clone(), cx)`；断言 `is_singleton()` 为真，记录 `snapshot().text()` 与 `excerpts().count()`。
3. 装法 B：`MultiBuffer::new(Capability::ReadWrite)` 后调用 `set_excerpts_for_buffer(buffer.clone(), vec![Point::row_range(0..1), Point::row_range(2..3)], 0, cx)`——只展示第 1 行和第 3 行（各自作为 primary，无上下文扩展）。断言 `is_singleton()` 为假，记录同样的两项。
4. 对装法 B 直接编辑底层 buffer（`buffer.update(cx, |b, cx| b.edit(...))`），再取一次快照，观察文本如何变化。
5. 用一段注释回答：两种装法下 `as_singleton()` 各返回什么？为什么编辑底层 buffer 后两个快照都能反映变化（提示：4.2.2 的流程图）？

**验收标准**：测试通过；注释中能写明「A 的 excerpts 计数为 1、B 为 2」「两者都依赖 BufferState 的订阅 + 惰性 sync 机制保持同步」。步骤 3 中无上下文扩展时 excerpt 文本是否包含行尾换行的精确输出**待本地验证**（对照 4.2.3 的换行旁注记录实际结果）。

## 6. 本讲小结

- **三层抽象**：Buffer 是完整文本的真相来源；Excerpt 是「buffer id + 范围」的目录项（不复制文本）；MultiBuffer 是把目录项按序装订后的逻辑文本。
- **`ExcerptRange` 双范围**：`context` 是实际展示的范围，`primary` 是其中要高亮的范围（搜索命中行）；`build_excerpt_ranges` 负责把 primary 按上下文行数向两侧整行扩展。
- **实体字段分工**：`MultiBuffer` 实体持有可变工作状态（buffers、diffs、订阅、singleton 标志、title、capability、history）和一个 `RefCell` 里的快照；快照是只读的拼接结果，靠 `buffer_changed_since_sync` 脏标志实现惰性同步。
- **两层 `is_empty` 语义不同**：实体层看「有没有 buffer」，快照层看「拼接文本是否零长」。
- **singleton 是标志位而非另一套结构**：构造时依然通过通用入口装入唯一的全文 excerpt；标志位让 title 推导、撤销分组、excerpt 边界迭代等一批行为可以走快路径分叉。

## 7. 下一步学习建议

本讲建立的是「静态结构」的认知。接下来两讲补齐实验手段和读取视图：

1. **u1-l3（搭建实验环境）**：系统学习 `build_simple` / `build_multi` / `build_random` / `randomly_edit` 等 test-support 构造器，以及如何用 `cargo test -p multi_buffer <名字>` 过滤运行——本讲实践中你已经提前用到了其中三个。
2. **u1-l4（MultiBufferSnapshot 快照模型）**：本讲只看了实体的 `snapshot` 字段，下一讲深入快照内部的五棵树（`excerpts`、`buffers`、`path_keys`、`diffs`、`diff_transforms`），理解「拼接结果」是如何被组织的。
3. 想提前感受 singleton 分叉的读者，可以通读 `multi_buffer_tests.rs` 开头的 `test_empty_singleton` 与 `test_singleton`（multi_buffer_tests.rs:21-60），再对比 `test_set_excerpts_for_buffer`（multi_buffer_tests.rs:1805-1866）里多 excerpt 的断言方式。
