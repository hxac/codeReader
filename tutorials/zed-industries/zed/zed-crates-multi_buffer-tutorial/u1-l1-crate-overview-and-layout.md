# 项目定位与 crate 结构：multi_buffer 是什么

> 对应大纲：u1-l1（入门单元第一讲，无前置依赖）

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `multi_buffer` 这个 crate 在 Zed 编辑器架构中扮演的角色：它是「把一个或多个 `Buffer` 的若干片段拼接成一条可编辑逻辑文本」的文档模型。
2. 列出 `src` 目录下五个源码文件（`multi_buffer.rs`、`anchor.rs`、`path_key.rs`、`transaction.rs`、`multi_buffer_tests.rs`）各自的职责。
3. 知道 crate 的关键依赖（`gpui`、`language`、`rope`、`sum_tree`、`buffer_diff`、`text`）分别提供了什么能力。
4. 能在本地编译这个 crate、列出它的测试清单，并画出一页「模块-职责对照表」。

本讲不深入任何算法细节，只建立「地图」。坐标换算、增量树、diff 变换等硬核内容留给后续讲义。

## 2. 前置知识

本讲是全书第一篇，尽量不假设你了解 Zed。但有几个名词先解释清楚，读源码时会顺畅很多：

- **Rust crate**：Rust 的编译单元，类似「一个库/包」。Zed 仓库的 `crates/` 目录下每个子目录都是一个 crate，`crates/multi_buffer` 就是本手册分析的对象。
- **编辑器缓冲区（Buffer）**：内存里的一段可编辑文本，加上它的元数据（文件路径、语言、语法树、撤销历史等）。在 Zed 中，单个文件的文本由 `language` crate 的 `Buffer` 类型承载（注意：不是叫 `buffer` 的 crate 提供的，`multi_buffer` 依赖的是 `language`）。
- **实体（Entity）**：Zed 自研 UI 框架 GPUI 中的核心概念。一个 `Entity<T>` 是对状态 `T` 的句柄，所有对它的读写都要通过 GPUI 提供的上下文（`cx`）进行，并且发生在一个前台线程上。`MultiBuffer` 本身就是一个 GPUI 实体。
- **快照（Snapshot）**：某一时刻文本状态的不可变只读副本。Zed 大量采用「实体可变 + 快照不可变」的组合：修改走实体，读取走快照，这样读取方可以放心持有旧快照不被并发修改破坏。
- **Excerpt（片段）**：从一个 `Buffer` 里截取的一段范围。多个 excerpt 按顺序拼接，就构成了 multi-buffer 的完整文本。这是本 crate 名字的由来，也是整个设计的核心。
- **Cargo 的工作区（workspace）**：Zed 根目录的 `Cargo.toml` 管理所有子 crate 的公共依赖版本，所以子 crate 里写 `gpui.workspace = true` 表示「用工作区统一指定的版本」。

如果你对 GPUI 的实体模型完全陌生，不用慌：本讲只需要记住「`MultiBuffer` 是一个被 GPUI 管理的可观察对象，改它会触发事件和重渲染」即可，细节在后续讲义中反复出现时会自然熟悉。

## 3. 本讲源码地图

本讲涉及的文件都在 `crates/multi_buffer/` 下：

| 文件 | 行数（HEAD 实测） | 职责 |
| --- | --- | --- |
| `Cargo.toml` | 63 | crate 元信息：库入口指向 `src/multi_buffer.rs`，声明依赖与 `test-support` feature |
| `src/multi_buffer.rs` | 8329 | 主体：`MultiBuffer` 实体、`MultiBufferSnapshot`、`Excerpt`、坐标系、游标、读取 API、编辑链路、diff 集成 |
| `src/anchor.rs` | 544 | `Anchor`：跨编辑稳定的位置引用（multibuffer 版锚点） |
| `src/path_key.rs` | 693 | `PathKey`（excerpt 的排序身份）以及整套 excerpt 增删改入口 |
| `src/transaction.rs` | 546 | `History`：跨 buffer 的撤销/重做事务 |
| `src/multi_buffer_tests.rs` | 6276 | 全部测试，包括随机化测试与不变量检查 |

几点说明：

- 这个 crate 没有常规的 `src/lib.rs`，而是通过 `Cargo.toml` 的 `[lib] path = "src/multi_buffer.rs"` 把库入口直接指到主文件（见 [Cargo.toml:11-13](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/Cargo.toml#L11-L13)），这符合 Zed 的命名规范。`multi_buffer.rs` 开头的 `mod` 声明把其余四个文件挂进来（见 [src/multi_buffer.rs:1-5](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/multi_buffer.rs#L1-L5)），其中测试模块用 `#[cfg(test)]` 门控，只在 `cargo test` 时编译。
- 行数用 `wc -l` 在当前 HEAD 测得，仅用于感受体量对比：主文件和测试文件各占半壁江山，三个辅助模块都很小。

## 4. 核心概念与源码讲解

### 4.1 MultiBuffer 类型定位

#### 4.1.1 概念说明

先想一个具体场景：你在 Zed 里按 `Ctrl-Shift-F` 做全项目搜索，命中了 37 个文件里的 100 处文本。编辑器界面上呈现的是一整份可以滚动、可以就地编辑的「搜索结果文档」——但实际上这些文本来自 37 个不同的文件缓冲区，每处命中只截取了前后几行。

把「若干 `Buffer` 的若干片段，拼成一份可编辑的统一视图」，就是 `MultiBuffer` 要解决的问题。源码的文档注释只有一句话加一个官网链接：

```rust
/// One or more [`Buffers`](Buffer) being edited in a single view.
///
/// See <https://zed.dev/features#multi-buffers>
pub struct MultiBuffer { ... }
```

见 [src/multi_buffer.rs:71-74](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/multi_buffer.rs#L71-L74)。

反过来，当你只是打开一个普通文件编辑时，并不需要「多」。所以 `MultiBuffer` 还有一个贯穿全库的特殊形态——**singleton**：只含一个 buffer、一个覆盖全文的 excerpt。Zed 编辑器视图统一用 `MultiBuffer` 承载文档，普通文件就是 singleton 形态，搜索结果/diff 视图则是多 excerpt 形态。这样编辑器、搜索、vim 模式等下游只需要面对一套 API。

谁在消费它？在仓库里检索 `multi_buffer.workspace`，能找到 13 个直接依赖它的 crate：`editor`（编辑器视图，最重的消费者）、`search`（项目搜索）、`git_ui`（git diff/blame 视图）、`vim`、`go_to_line`、`repl`、`agent_ui`、`acp_thread`、`edit_prediction_ui`、`picker_preview`、`svg_preview`、`benchmarks`、`editor_benchmarks`。记住前四个就够了。

#### 4.1.2 核心流程

`MultiBuffer` 的生命周期可以概括为：

```text
创建
  ├─ MultiBuffer::new(capability)          → 空 multibuffer（搜索结果视图的起点）
  ├─ MultiBuffer::singleton(buffer, cx)    → 单 buffer 全文 excerpt（普通文件编辑）
  └─ （测试）build_simple / build_multi / build_random
       │
       ▼
管理 excerpt（增删改，见 path_key.rs）
       │
       ▼
读取：snapshot(cx) 惰性同步底层 buffer 变化后，克隆出不可变快照
编辑：edit(...) 把 multibuffer 范围翻译回各 buffer 的编辑并分发
       │
       ▼
对外的变化通知：Event 枚举（Edited / BuffersEdited / BuffersRemoved / ...）
```

两个关键设计在本讲先记住结论：

1. **实体与快照分离**：`MultiBuffer`（实体）持有 `RefCell<MultiBufferSnapshot>`，读取方拿到的是克隆出来的快照，互不干扰。
2. **惰性同步**：底层 buffer 自己也在变（协作编辑、语言工具修改等），实体用一个 `buffer_changed_since_sync` 标志记录「底层变了」，等到有人调用 `snapshot(cx)`/`read(cx)` 时才真正重建内部树。

#### 4.1.3 源码精读

`MultiBuffer` 实体的全部字段，见 [src/multi_buffer.rs:74-93](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/multi_buffer.rs#L74-L93)：

```rust
pub struct MultiBuffer {
    /// A snapshot of the [`Excerpt`]s in the MultiBuffer.
    snapshot: RefCell<MultiBufferSnapshot>,
    /// Contains the state of the buffers being edited
    buffers: BTreeMap<BufferId, BufferState>,
    /// Mapping from buffer IDs to their diff states
    diffs: HashMap<BufferId, DiffState>,
    subscriptions: Topic<MultiBufferOffset>,
    /// If true, the multi-buffer only contains a single [`Buffer`] and a single [`Excerpt`]
    singleton: bool,
    history: History,
    title: Option<String>,
    capability: Capability,
    buffer_changed_since_sync: Rc<Cell<bool>>,
}
```

逐个看：

- `snapshot`：快照本体，用 `RefCell` 包住以便在 `read(cx)` 里同步更新。
- `buffers`：参与拼接的底层 buffer 集合。`BufferState` 很薄，只是 buffer 句柄加两个订阅（观察 + 事件），见 [src/multi_buffer.rs:504-507](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/multi_buffer.rs#L504-L507)。
- `diffs`：buffer 到 diff 状态的映射，支撑 git diff 视图里「显示被删除的行」等能力（专家单元细讲）。
- `subscriptions`：文本订阅总线，订阅者可以拿到增量编辑流 `Edit<MultiBufferOffset>`。
- `singleton`：前文说的单 buffer 形态标志，很多方法会沿这个标志分叉（比如事务直接委托给 buffer）。
- `history`：跨 buffer 撤销/重做（见 4.2 的 `transaction.rs`）。
- `title` / `capability`：显式标题（不设则从路径或内容推导）与读写能力。
- `buffer_changed_since_sync`：惰性同步标志。

三种构造入口，见 [src/multi_buffer.rs:1205-1245](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/multi_buffer.rs#L1205-L1245)：

```rust
pub fn new(capability: Capability) -> Self { ... }        // 空的多 buffer
pub fn without_headers(capability: Capability) -> Self { ... } // 不显示 excerpt 头部
pub fn singleton(buffer: Entity<Buffer>, cx: &mut Context<Self>) -> Self {
    // ...
    this.singleton = true;
    this.set_excerpts_for_path(
        PathKey::sorted(0),
        buffer.clone(),
        [Point::zero()..buffer.read(cx).max_point()], // 覆盖全文的单个 excerpt
        0,
        cx,
    );
    this
}
```

注意 `singleton` 构造本质上也是「设置一个覆盖 `Point::zero()..max_point()` 的 excerpt」，只是额外打了 `singleton` 标志——统一模型，特殊形态只是标志位。

读取与 singleton 判定，见 [src/multi_buffer.rs:1321-1343](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/multi_buffer.rs#L1321-L1343)：

```rust
pub fn snapshot(&self, cx: &App) -> MultiBufferSnapshot {
    self.sync(cx);                      // 先做惰性同步
    self.snapshot.borrow().clone()      // 再克隆快照
}
pub fn read(&self, cx: &App) -> Ref<'_, MultiBufferSnapshot> { ... }
pub fn as_singleton(&self) -> Option<Entity<Buffer>> { ... }
pub fn is_singleton(&self) -> bool { ... }
```

对外的事件通知是 `Event` 枚举，见 [src/multi_buffer.rs:98-127](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/multi_buffer.rs#L98-L127)，下游（比如编辑器）用 `cx.subscribe` 监听这些事件决定重绘什么。本讲只需要扫一眼变体名感受「它对外汇报哪些变化」：`Edited`、`BuffersEdited`、`BuffersRemoved`、`BufferRangesUpdated`、`TransactionUndone`、`Saved`、`LanguageChanged`、`Reparsed`、`DiffHunksToggled` 等。

顺带认识 excerpt 的「双重范围」：`ExcerptRange` 有 `context` 和 `primary` 两个字段，见 [src/multi_buffer.rs:840-849](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/multi_buffer.rs#L840-L849)：

```rust
pub struct ExcerptRange<T> {
    /// The full range of text to be shown in the excerpt.
    pub context: Range<T>,
    /// The primary range of text to be highlighted in the excerpt.
    /// In a multi-buffer search, this would be the text that matched the search
    pub primary: Range<T>,
}
```

`context` 是实际展示的范围（搜索命中前后各扩几行，默认上下文行数是 2，由 [src/multi_buffer.rs:65-69](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/multi_buffer.rs#L65-L69) 的 `excerpt_context_lines` 提供），`primary` 是要高亮的命中范围。

#### 4.1.4 代码实践

**实践一（源码阅读型）：验证 singleton 与普通构造的差异**

1. **实践目标**：不看运行结果，先通过读构造函数预言两种 `MultiBuffer` 的行为差异，再用测试验证。
2. **操作步骤**：
   - 打开 [src/multi_buffer.rs:3153-3188](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/multi_buffer.rs#L3153-L3188)，阅读三个测试专用构造器 `build_simple`、`build_multi`、`build_from_buffer`（它们都在 `#[cfg(any(test, feature = "test-support"))]` 块内，普通构建不参与编译）。
   - 在 `src/multi_buffer_tests.rs` 末尾添加一个测试（示例代码，非项目原有代码）：

     ```rust
     #[gpui::test]
     fn test_singleton_vs_empty(cx: &mut gpui::App) {
         let simple = MultiBuffer::build_simple("hello", cx);
         assert!(simple.read(cx).is_singleton());
         assert_eq!(simple.read(cx).text(), "hello");

         let empty = cx.new(|_| MultiBuffer::new(Capability::ReadWrite));
         assert!(!empty.read(cx).is_singleton());
         assert_eq!(empty.read(cx).text(), "");
     }
     ```

   - 运行 `cargo test -p multi_buffer test_singleton_vs_empty`。
3. **需要观察的现象**：两个断言组分别通过；如果错误地用 `build_multi` 的空数组形态去断言 `is_singleton()`，会得到 `false`。
4. **预期结果**：测试通过。若 `Capability` 等符号未导入，参考 `multi_buffer_tests.rs` 文件顶部的 `use` 块补齐。断言中 `text()`/`singleton()` 的行为（待本地验证——本讲未替你运行该测试）。
5. 失败时优先检查：`MultiBufferSnapshot` 上是否真有 `singleton()` 与 `text()` 方法（可在主文件里搜索 `fn singleton` 与 `fn text` 确认签名）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Zed 不为「普通单文件编辑」单独实现一个 `Buffer` 视图类型，而统一用 `MultiBuffer` 的 singleton 形态？

**参考答案**：统一模型让编辑器、搜索、vim、git diff 等下游只面对一套 API 和一套坐标系；singleton 只是 `MultiBuffer` 上一个标志位（[src/multi_buffer.rs:83-84](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/multi_buffer.rs#L83-L84)），很多方法沿它走「直接委托给底层 buffer」的快路径，避免多态分叉。代价是每个使用点都要同时考虑两种形态。

**练习 2**：`ExcerptRange` 里 `context` 和 `primary` 为什么是两个范围而不是一个？

**参考答案**：展示范围和高亮范围语义不同：搜索结果需要展示命中处上下各几行（`context`），但只有命中本身需要高亮/跳转（`primary`）。分开表示后，扩展上下文行数不会影响命中位置信息；`build_excerpt_ranges` 负责从 `primary` 出发扩出 `context`（见 [src/multi_buffer.rs:3140-3150](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/multi_buffer.rs#L3140-L3150)）。

### 4.2 模块划分

#### 4.2.1 概念说明

主文件有 8300 多行，如果所有内容都堆在里面会不可维护。crate 的拆法是：**主文件放数据模型与读取/编辑主链路，三个小模块各管一个正交概念**：

- `anchor.rs` 管「位置如何稳定地被引用」；
- `path_key.rs` 管「片段如何排序、如何增删改」；
- `transaction.rs` 管「跨 buffer 的撤销/重做」。

这三个概念彼此独立，也都能用几百行讲清楚，所以单独成文件。

#### 4.2.2 核心流程

模块之间的依赖方向（箭头表示「使用」）：

```text
multi_buffer.rs（主体：实体 + 快照 + 游标 + 读取 + 编辑 + diff）
   ▲          ▲          ▲
   │          │          │
anchor.rs   path_key.rs  transaction.rs
（稳定位置） （排序身份 +  （跨 buffer
             excerpt 管理） 事务/撤销）
   └──────────┴──────────┴── 都要回头使用主体中定义的
                              MultiBufferSnapshot / Excerpt / 坐标类型

multi_buffer_tests.rs —— #[cfg(test)] 门控，覆盖以上全部
```

注意这是一种「双向 acquaintance」：主体通过 `mod` 引入子模块并 `pub use` 其中的公开类型，子模块又通过 `crate::`/`super::` 使用主体类型。Rust 的模块系统允许这种互相引用。

#### 4.2.3 源码精读

模块声明与再导出，见 [src/multi_buffer.rs:1-13](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/multi_buffer.rs#L1-L13) 和 [src/multi_buffer.rs:63](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/multi_buffer.rs#L63)：

```rust
mod anchor;
#[cfg(test)]
mod multi_buffer_tests;
mod path_key;
mod transaction;

pub use anchor::{Anchor, AnchorRangeExt};
// ...
pub use self::path_key::PathKey;
```

也就是说，crate 对外暴露的名字（`MultiBuffer`、`MultiBufferSnapshot`、`Anchor`、`PathKey`……）几乎全部由 `multi_buffer.rs` 汇聚出口。

**anchor.rs（544 行）** 定义 multibuffer 版锚点。`Anchor` 是一个三变体枚举，见 [src/anchor.rs:27-35](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/anchor.rs#L27-L35)：

```rust
pub enum Anchor {
    /// An anchor that always resolves to the start of the multibuffer.
    Min,
    /// An anchor that's attached to a specific excerpted buffer.
    Excerpt(ExcerptAnchor),
    /// An anchor that always resolves to the end of the multibuffer.
    Max,
}
```

其中 `ExcerptAnchor` 由「底层 buffer 的锚点 + 所在路径的索引 + 可选的 diff 基文本锚点」组成，见 [src/anchor.rs:16-21](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/anchor.rs#L16-L21)。普通 offset 在文本编辑后会失效（指向错误的字符），锚点则跟着文本移动——这是搜索结果在编辑后还能正确高亮的基础。

**path_key.rs（693 行）** 做两件事。第一，定义 excerpt 的排序身份 `PathKey`，见 [src/path_key.rs:18-23](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/path_key.rs#L18-L23)：

```rust
pub struct PathKey {
    // Used by the derived PartialOrd & Ord
    pub sort_prefix: Option<u64>,
    pub path: Arc<RelPath>,
}
```

搜索结果里各文件按路径排序出现；同一 buffer 若被换到新路径（文件重命名），旧锚点会失效。第二，容纳整套 excerpt 管理入口——对 `MultiBuffer` 的 `impl` 块直接写在这个文件里，如 `set_excerpts_for_buffer`、`set_excerpts_for_path`、`update_excerpts_for_path`，见 [src/path_key.rs:60-126](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/path_key.rs#L60-L126)。这是 Rust 常见的「按主题拆 impl 块」手法：类型定义在主文件，方法实现散在主题文件。

**transaction.rs（546 行）** 定义跨 buffer 的 `History`，见 [src/transaction.rs:15-22](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/transaction.rs#L15-L22)：

```rust
pub(super) struct History {
    next_transaction_id: TransactionId,
    undo_stack: Vec<Transaction>,
    redo_stack: Vec<Transaction>,
    transaction_depth: usize,
    group_interval: Duration,
}
```

一个 `Transaction` 记录的是「各 buffer 各自的子事务 id」的映射（[src/transaction.rs:36-43](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/transaction.rs#L36-L43)），这样一次 undo 能把多个 buffer 的同组编辑一起回滚。默认分组间隔 300ms（[src/transaction.rs:31](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/transaction.rs#L31)）：300ms 内的连续编辑算同一组。

**multi_buffer_tests.rs（6276 行）** 不只是「测试」，它还是本手册最重要的实验场：`check_multibuffer` 系列不变量检查、`build_random` 随机构造、`format_diff` 等辅助都在这里，后续每讲的实践都会往这个文件里加代码。

#### 4.2.4 代码实践

**实践二（源码阅读型）：给每个模块找「一句话职责」**

1. **实践目标**：不借助本讲义，从源码本身推断每个文件的职责，形成自己的对照表。
2. **操作步骤**：
   - 依次打开四个非测试源码文件，只读每个文件的前 60 行（`use` 列表往往暴露了它关心什么）。
   - 对每个文件回答三个问题：它定义了哪些核心类型？它 `use crate::{...}` 了什么（即依赖主体的哪些名字）？它有没有对主体类型的 `impl` 块？
   - 把答案填进一张三列表格：文件 / 核心类型 / 一句话职责。
3. **需要观察的现象**：`path_key.rs` 顶部 `use` 了 `Excerpt`、`ExcerptRange`、`MultiBuffer` 等主体类型（见 [src/path_key.rs:12-16](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/path_key.rs#L12-L16)），印证「excerpt 管理实现放在这里、数据定义在主体」的分工。
4. **预期结果**：得到类似 4.2.3 开头的表格，但用你自己的话写。可与本讲第 3 节的表格对照，检查有没有遗漏的职责点。

#### 4.2.5 小练习与答案

**练习 1**：为什么 excerpt 管理方法（`set_excerpts_for_path` 等）写在 `path_key.rs` 而不是主文件？

**参考答案**：excerpt 的增删改总是围绕「按哪个路径排序、替换哪个路径的既有片段」进行，与 `PathKey` 强相关；拆出去能让 8300 行的主文件少掉约 700 行，且读者想找 excerpt 管理入口时有唯一去处。这是 Zed 代码库里「类型定义与 impl 块分离」的常见做法。

**练习 2**：`transaction.rs` 里的 `History` 为什么放在独立模块，而不是直接用 `language::Buffer` 自带的撤销？

**参考答案**：底层 `Buffer` 的撤销只覆盖单个 buffer；multibuffer 的一次用户操作可能同时改动多个 buffer，需要把「各 buffer 的子事务 id」收拢到一个 `Transaction` 里才能一起 undo（[src/transaction.rs:37-39](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/transaction.rs#L37-L39)）。另外 singleton 形态下确实会委托给 buffer 自己的 history，这属于后续讲义（u2-l11）的内容。

**练习 3**：`mod multi_buffer_tests;` 上的 `#[cfg(test)]` 起什么作用？如果下游 crate 想复用 `build_simple`，靠什么机制？

**参考答案**：`#[cfg(test)]` 使该模块只在 `cargo test` 编译单 crate 时参与编译，不进入发布产物。下游复用靠 `test-support` feature：`Cargo.toml` 中 `[features] test-support` 会转发开启一组依赖的 test-support（见 [Cargo.toml:15-22](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/Cargo.toml#L15-L22)），而构造器本身用 `#[cfg(any(test, feature = "test-support"))]` 门控（[src/multi_buffer.rs:3153-3154](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/multi_buffer.rs#L3153-L3154)），两条路径都能打开它。

### 4.3 依赖关系

#### 4.3.1 概念说明

`multi_buffer` 是一个纯模型层 crate：它不含任何 UI 绘制代码，也不直接读磁盘/网络。它的依赖可以分成四组来记：

| 组 | crate | 提供什么 |
| --- | --- | --- |
| 运行时底座 | `gpui` | `App`/`Context`/`Entity`/`EventEmitter`——让 `MultiBuffer` 成为可观察实体 |
| 文本基础 | `language`、`text`、`rope` | `Buffer`/`BufferSnapshot`/`Capability`/语言设置；`BufferId`/`Edit`/`TextSummary`/订阅；rope 与 `DimensionPair` |
| 增量结构 | `sum_tree`、`collections` | `SumTree`/`Cursor`/`Dimension`/`TreeMap`——excerpts 与 diff_transforms 两棵树的载体 |
| diff 集成 | `buffer_diff` | `BufferDiff`/`DiffHunkStatus` 等——git diff 视图所需 |

另有辅助依赖：`settings`（语言设置读取）、`theme`（`SyntaxTheme`，语法高亮着色需要）、`tree-sitter`（语法查询底层）、`util`（`RelPath` 相对路径类型）、`ztracing`/`tracing`/`log`（打点）、`anyhow`/`itertools`/`smallvec`/`parking_lot`/`rand`/`serde`/`futures-lite`/`clock`/`unicode-segmentation` 等通用工具。

#### 4.3.2 核心流程

依赖决定了一条清晰的分层：

```text
gpui（实体/事件/上下文）
  │
  ▼
multi_buffer ──uses──▶ language（Buffer、语言能力）──uses──▶ text / rope（坐标、rope 文本）
  │                        │
  │                        ▼
  │                    buffer_diff（BufferDiff）
  ▼
sum_tree（增量平衡树：excerpts / diff_transforms）
```

要点：`multi_buffer` 不依赖 `editor`，方向是反过来——`editor` 依赖 `multi_buffer`。保持模型层不依赖 UI 层，是这套设计能被 13 个 crate 复用的前提。

#### 4.3.3 源码精读

依赖声明的两处入口：

- `[lib] path = "src/multi_buffer.rs"` 与 `[features] test-support`：[Cargo.toml:11-22](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/Cargo.toml#L11-L22)。注意 `test-support` feature 做的事是「转发依赖们的 test-support」，自己的测试构造器由此对下游可见。
- 全部运行时依赖：[Cargo.toml:24-48](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/Cargo.toml#L24-L48)。

主文件的 `use` 块直接反映了这些依赖的用法，见 [src/multi_buffer.rs:13-16](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/multi_buffer.rs#L13-L16)（buffer_diff）、[src/multi_buffer.rs:20-30](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/multi_buffer.rs#L20-L30)（language）、[src/multi_buffer.rs:35](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/multi_buffer.rs#L35)（rope）、[src/multi_buffer.rs:54](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/multi_buffer.rs#L54)（sum_tree）、[src/multi_buffer.rs:55-58](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/multi_buffer.rs#L55-L58)（text）：

```rust
use buffer_diff::{BufferDiff, BufferDiffEvent, BufferDiffSnapshot, ...};
use gpui::{App, Context, Entity, EventEmitter};
use language::{AutoIndentExclusion, AutoindentMode, Buffer, BufferChunks, ...};
use rope::DimensionPair;
use sum_tree::{Bias, Cursor, Dimension, Dimensions, SumTree, TreeMap};
use text::{BufferId, Edit, LineIndent, TextSummary, subscription::{Subscription, Topic}};
```

快照字段则展示了这些依赖如何组合成数据结构，见 [src/multi_buffer.rs:692-710](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/multi_buffer.rs#L692-L710)：

```rust
pub struct MultiBufferSnapshot {
    excerpts: SumTree<Excerpt>,
    buffers: TreeMap<BufferId, BufferStateSnapshot>,
    path_keys: Arc<IndexSet<PathKey>>,
    diffs: SumTree<DiffStateSnapshot>,
    diff_transforms: SumTree<DiffTransform>,
    // ... 标志位：singleton、is_dirty、show_headers、show_deleted_hunks 等
}
```

五个字段五棵「索引」：`excerpts` 是拼接主树，`buffers`/`path_keys` 是按 id/路径的辅助索引，`diffs`/`diff_transforms` 支撑 diff 视图。`SumTree` 是本 crate 的性能地基——它是一棵按摘要（summary）组织的增量平衡树，改动后不必整体重建。本讲只需记住「树上有摘要、可以按维度 O(log n) 定位」，细节从 u2-l3 开始展开。

#### 4.3.4 代码实践

**实践三（命令行型）：画出真实依赖图**

1. **实践目标**：用 Cargo 工具亲自验证 4.3.1 的分组，而不是背诵表格。
2. **操作步骤**：
   - 在仓库根目录运行 `cargo tree -p multi_buffer --depth 1`，记录直接依赖清单。
   - 对照 `Cargo.toml` 的 `[dependencies]`，确认两者一致；再运行 `cargo tree -p multi_buffer -i sum_tree`，看有哪些路径汇入 `sum_tree`。
   - 把输出整理成 4.3.2 那样的分层小图（手画或 Mermaid 均可）。
3. **需要观察的现象**：`--depth 1` 输出的列表与 [Cargo.toml:24-48](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/Cargo.toml#L24-L48) 一一对应；`-i sum_tree`（逆向模式）会显示 `multi_buffer` 经由哪些父依赖间接引入它。
4. **预期结果**：得到一张包含 `gpui`、`language`、`text`、`rope`、`sum_tree`、`buffer_diff` 等的分层图，且 `editor`、`search`、`git_ui`、`vim` 不出现在 `multi_buffer` 的依赖方向里（它们是反方向消费者）。具体输出的 crate 版本号以你本地的 lock 文件为准（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`MultiBufferSnapshot` 为什么要同时维护 `excerpts`、`buffers`、`path_keys` 三套结构，而不是一个 `Vec<Excerpt>`？

**参考答案**：不同查询需要不同索引：按 multibuffer 偏移定位 excerpt（`excerpts` 树，可 O(log n) 按维度游标查找）、按 buffer id 查它参与的片段（`buffers` 映射）、按路径排序/查路径索引（`path_keys`）。平面数组无法在频繁编辑下增量维护这些查询的性能，`SumTree` 的摘要机制才能做到「局部改动、对数更新」。细节在 u2 单元展开。

**练习 2**：`multi_buffer` 依赖 `theme`（一个 UI 相关 crate），这和「纯模型层」的说法矛盾吗？

**参考答案**：不算矛盾但要留意：这里只用到了 `theme::SyntaxTheme` 作为语法高亮的配色描述传给 `chunks()` 读取接口（见 [src/multi_buffer.rs:59](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/multi_buffer.rs#L59) 的导入），即「生成带样式信息的文本块」仍是数据而非绘制。这属于边界上的妥协，是阅读大型代码时常见的灰色地带。

## 5. 综合实践

本讲的综合实践把三个小实践串成一份可留存的「crate 档案」，这也是本手册后续所有讲义的实验基座：

1. **编译与测试清单**：在仓库根目录运行：

   ```bash
   cargo build -p multi_buffer
   cargo test -p multi_buffer -- --list
   ```

   记录：编译是否成功、测试清单里有多少个测试函数（数量以实际输出为准，待本地验证）。`--list` 只枚举不执行；测试名基本按功能分组（如 `test_editing_*`、`test_diff_*`、`test_random_*`），顺便扫一遍名字就能印证 4.2 节的职责划分。

2. **体量档案**：运行 `wc -l crates/multi_buffer/src/*.rs`，与第 3 节表格对照（本讲给出的数字测自 HEAD `00c0e96e`，你本地可能略有出入）。

3. **依赖档案**：运行 `cargo tree -p multi_buffer --depth 1`，按 4.3.1 的四组归类输出。

4. **产出**：写一份 Markdown「模块-职责对照表」，包含：文件、行数、核心类型、一句话职责、主要依赖。建议直接放在你自己的笔记里（不要放进仓库，避免污染源码目录）。

   参考骨架（示例，非项目原有内容）：

   | 文件 | 核心类型 | 一句话职责 | 关键依赖 |
   | --- | --- | --- | --- |
   | multi_buffer.rs | `MultiBuffer` / `MultiBufferSnapshot` / `Excerpt` | 实体+快照+主链路 | gpui, language, sum_tree |
   | anchor.rs | `Anchor` / `ExcerptAnchor` | 跨编辑稳定位置 | text, sum_tree |
   | path_key.rs | `PathKey` | 排序身份 + excerpt 管理 | util(RelPath), sum_tree |
   | transaction.rs | `History` / `Transaction` | 跨 buffer 撤销 | clock, text |
   | multi_buffer_tests.rs | — | 测试 + 实验场 | 全部 + test-support |

   完成后自测：能否不看讲义，向别人解释「为什么搜索结果的视图需要一个专门的 crate」？能说出「统一 singleton 与多 excerpt 两形态」「excerpts 用 SumTree 增量维护」「模型层不依赖 UI 层」三个要点，本讲就达标了。

## 6. 本讲小结

- `multi_buffer` 是 Zed 的文档模型层：把一个或多个 `language::Buffer` 的 excerpt 拼成一份可编辑逻辑文本；普通文件编辑走 singleton 形态，项目搜索、git diff 视图走多 excerpt 形态，下游 `editor`、`search`、`git_ui`、`vim` 等 13 个 crate 依赖它。
- `MultiBuffer` 是 GPUI 实体，持有 `RefCell<MultiBufferSnapshot>`；读取走 `snapshot(cx)`/`read(cx)`（先惰性同步再克隆），变化通过 `Event` 枚举对外发布。
- 源码分五个文件：主文件（实体、快照、坐标系、游标、读取、编辑、diff）、`anchor.rs`（稳定位置）、`path_key.rs`（排序身份 + excerpt 管理）、`transaction.rs`（跨 buffer 事务）、`multi_buffer_tests.rs`（测试与实验场）。
- 关键依赖四分组：`gpui`（实体底座）、`language`/`text`/`rope`（文本基础）、`sum_tree`/`collections`（增量结构）、`buffer_diff`（diff 集成）；模型层不依赖 UI 层。
- `ExcerptRange` 的 `context`/`primary` 双范围分别对应「展示范围」与「高亮范围」；excerpt 默认上下文行数为 2。

## 7. 下一步学习建议

下一讲（u1-l2「核心概念：Buffer、Excerpt 与 MultiBuffer 实体」）将深入 `MultiBuffer` 实体的字段与 singleton 分叉，建议先自行浏览：

- [src/multi_buffer.rs:692-710](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/multi_buffer/src/multi_buffer.rs#L692-L710) 的 `MultiBufferSnapshot` 字段——想想每个字段服务什么查询。
- `src/multi_buffer_tests.rs` 里任何一个以 `build_multi` 开头的测试，感受「两个 buffer 各取一段」的构造方式。
- 如果想提前建立 `SumTree` 直觉，可以去 `crates/sum-tree` 读它的模块文档；不过这不是必须的，u2-l3 会从零讲起。
