# 三个核心名词：DiffBaseKind、DiffHunk 与 BufferDiffSnapshot

## 1. 本讲目标

学完本讲，你应该能够：

1. 区分 `BufferDiff`（可变的 gpui 实体）与 `BufferDiffSnapshot`（不可变快照），说清楚它们各自的用途。
2. 逐字段解释 `DiffHunk` 的含义：`range`、`buffer_range`、`diff_base_byte_range`、`secondary_status`、`buffer_word_diffs`、`base_word_diffs`，并理解 `status()` 如何推导出 Added / Modified / Deleted。
3. 说出 `DiffBaseKind` 四种变体（Head / Index / Oid / Custom）各自的语义，以及为什么只有 Head 基准的 diff 允许 stage。

本讲不涉及 diff 算法本身（那是单元三的内容），只建立数据模型的地基。

## 2. 前置知识

上一讲（u1-l1）我们已经知道：buffer_diff 是 Zed 的行级差异引擎，它把「buffer 当前内容」和「基准文本（diff base）」做对比，产出一组 hunk（差异块）供编辑器 gutter、git 面板等使用。本讲需要再补充三个基础概念：

- **实体（Entity）与句柄**：在 gpui 框架中，`Entity<T>` 是一个指向状态 `T` 的句柄（handle）。你可以通过句柄去读取（`read`）或更新（`update`）那份状态，但句柄本身很轻，可以到处克隆传递。`BufferDiff` 就是一个这样的实体。
- **快照（Snapshot）**：一份**不可变**的状态副本。渲染和查询都基于快照进行，这样即使实体正在后台更新，拿到的快照也不会中途变化。`BufferDiffSnapshot` 就是这个角色。
- **两种位置表示**：
  - `Point { row, column }`：行号 + 列号，直观但**脆弱**——在它前面插入一行后，同一个 `Point` 指向的内容就变了。
  - `Anchor`（锚点）：绑定到某个**文本位置**而不是坐标，buffer 编辑后锚点会跟随原位置移动（类似文档里的书签）。`DiffHunk` 内部用锚点存位置，查询时再换算成 `Point` 给调用方。

另外一个贯穿全讲的直觉：**diff 是「base → buffer」方向计算的**。所以每个 hunk 天然带有两侧信息——base 侧被删掉的文本区间，和 buffer 侧新出现的文本区间。

## 3. 本讲源码地图

本 crate 的全部实现都集中在一个文件里：

| 文件 | 作用 |
| --- | --- |
| `src/buffer_diff.rs`（约 4400 行） | 数据模型、diff 计算、查询 API、git staging 支持、测试，全部在此 |
| `Cargo.toml` | 依赖清单（上一讲已分析） |

本讲关注的代码区段：

| 区段 | 内容 |
| --- | --- |
| 文件开头到约 L240 | 核心数据结构定义（本讲主线） |
| 约 L1568–L1700 | `BufferDiff` 实体的构造函数与基准判定 |
| 约 L2164–L2181 | `snapshot()` 的组装逻辑 |
| 约 L2290–L2310 | `DiffHunk` 的 `status()` / `is_created_file()` |
| 约 L2425 起 | `mod tests` 测试模块（实践素材） |

## 4. 核心概念与源码讲解

### 4.1 BufferDiff：可变的 gpui 实体

#### 4.1.1 概念说明

`BufferDiff` 是「一条进行中的 diff」的本体：它知道自己为哪个 buffer 服务、基准文本是什么、当前算出的 hunk 有哪些。它是 gpui 实体而不是普通结构体，原因有二：

1. **diff 计算是异步的**——大文件的 diff 要放到后台线程算，实体提供了一个可以被后台任务回写的「家」。
2. **需要发事件**——diff 结果变化时要通知编辑器重绘 gutter、通知 git 面板刷新（通过 `EventEmitter<BufferDiffEvent>`，细节留到单元三）。

一句话对比：`BufferDiff` 是**会动的状态**，`BufferDiffSnapshot` 是**定格的照片**。

#### 4.1.2 核心流程

`BufferDiff` 有 5 个字段，生命周期大致是：

```text
构造（new / new_unchanged / new_with_base_text*）
   │  记录 buffer_id、base_kind，准备 base_text_buffer
   ▼
外部（通常是 project 层）灌注基准文本 → base_text_buffer
   │
   ▼
后台计算 diff → 产出 BufferDiffSnapshot 存入 diff_snapshot 字段
   │
   ▼
调用方调用 snapshot(cx) 取走快照 → 查询 hunk
```

注意构造时 `diff_snapshot` 可以是 `None`（还没算过 diff），这时 `snapshot()` 会返回一个「空 diff」快照兜底——这个兜底行为在 4.3.3 精读。

#### 4.1.3 源码精读

先看实体的字段定义：

[src/buffer_diff.rs:L22-L29](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L22-L29) —— `BufferDiff` 的五个字段：

- `buffer_id`：它服务的是哪个 buffer；
- `base_text_buffer`：基准文本本身也是一个**只读的 `language::Buffer`**（这样基准文本也能享受语法高亮、锚点等能力）；
- `diff_snapshot`：最近一次算出的快照，可能还没有（`None`）；
- `secondary_diff`：git 三方模型里挂载的「另一个 diff」（HEAD vs Index），本讲只需知道它存在，单元四详解；
- `base_kind`：基准来自哪里，见 4.2。

再看三个主要构造函数（第四个 `new_with_base_text` 是测试专用，放在综合实践里讲）：

[src/buffer_diff.rs:L1569-L1594](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1569-L1594) —— `BufferDiff::new`：最常用的构造。创建一个**空内容**的只读 buffer 当基准（此时基准还没灌注），`diff_snapshot` 为 `None`。调用方之后通过 `set_base_text` 异步填入基准文本。

[src/buffer_diff.rs:L1596-L1610](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1596-L1610) —— `new_with_base_text_buffer`：和 `new` 的区别是基准 buffer 由调用方**从外面带进来**（`Entity<language::Buffer>`），而不是新建一个空的。适合基准文本已有现成 buffer 的场景。

[src/buffer_diff.rs:L1612-L1649](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1612-L1649) —— `new_unchanged`：直接把 **buffer 自己的当前文本**当作基准，于是一开始没有任何差异：两棵 `SumTree` 都是空的，但 `base_text_exists: true`、`diff_snapshot` 是 `Some(...)`。它是「我知道现在没有 diff，别浪费时间算」的快路径。

#### 4.1.4 代码实践

**实践目标**：分清三个构造函数分别产出什么初始状态。

**操作步骤**：

1. 打开 [src/buffer_diff.rs:L1568-L1610](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1568-L1610)，逐行对比 `new` 和 `new_with_base_text_buffer` 的函数体，找出唯一实质差异（提示：`base_text` 这一行是 `""` 还是由参数决定）。
2. 再读 [src/buffer_diff.rs:L1630-L1639](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1630-L1639)，观察 `new_unchanged` 手工构造的那个 `BufferDiffSnapshot`：`hunks` 与 `pending_hunks` 都是 `SumTree::new(...)`（空树），`base_text_exists: true`。
3. 填写下面这张表（答案见 4.1.5）：

| 构造函数 | 基准文本来源 | 构造后立刻有 hunk 吗 | `diff_snapshot` |
| --- | --- | --- | --- |
| `new` | ？ | ？ | ？ |
| `new_with_base_text_buffer` | ？ | ？ | ？ |
| `new_unchanged` | ？ | ？ | ？ |

**需要观察的现象 / 预期结果**：这是一道纯阅读题，不需要运行命令；对照源码即可完成表格。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `base_text_buffer` 要用 `Entity<language::Buffer>` 而不是一个简单的 `String`？

**答案**：基准文本需要和 buffer 同等的能力：被 diff 算法读取、提供锚点和快照（`BufferSnapshot`）、参与语法相关的处理（如词级 diff）。用现成的 `language::Buffer` 承装就能免费获得这一切；同时它是只读的（构造时 `set_capability(Capability::ReadOnly, ...)`，见 [src/buffer_diff.rs:L1576-L1584](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1576-L1584)），保证基准不会被意外改写。

**练习 2**：一个刚用 `BufferDiff::new` 创建、还没 `set_base_text` 的实体，调用 `snapshot(cx)` 会发生什么？

**答案**：`diff_snapshot` 是 `None`，`snapshot()` 会走兜底分支：返回一个 hunks 为空、`base_text_exists: false` 的快照（见 4.3.3 的 L2164-L2181）。不会 panic。

**练习 3**：`new_unchanged` 适合什么场景？举例说明。

**答案**：适合「已知 buffer 与基准完全一致」的场景，例如刚打开文件、git 状态显示无修改时。它跳过 diff 计算，直接给出「零 hunk」快照；等将来基准或 buffer 变了再走正常计算路径。

### 4.2 DiffBaseKind：基准文本从哪来

#### 4.2.1 概念说明

`DiffBaseKind` 是一个四变体枚举，回答一个问题：**这条 diff 的基准文本是什么身份？** 同一个 buffer 可以同时挂着多条 diff（比如「HEAD vs 工作区」和「Index vs 工作区」），下游 UI 需要知道基准身份才能决定能对它做什么操作。

#### 4.2.2 核心流程

```text
调用方构造 BufferDiff 时指定 base_kind
        │
        ▼
下游询问 diff.is_stageable()
        │
        ├─ base_kind == Head → true（可以 stage / restore hunk）
        └─ 其他三种        → false（stage 会改写已提交的内容，禁止）
```

#### 4.2.3 源码精读

[src/buffer_diff.rs:L31-L45](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L31-L45) —— `DiffBaseKind` 的定义，每个变体的文档注释写得非常清楚：

- `Head`：buffer 已提交（HEAD）的内容；
- `Index`：暂存区（staged）的内容；
- `Oid`：任意 blob，比如与另一分支的 merge base；
- `Custom`：调用方任意提供的文本，比如 agent 的原始文本、剪贴板、另一个文件。

注意变体上方的类型级文档注释（L31-L33）：**只有 HEAD 基准支持 staging 和 restore hunk**——对其他基准（例如 merge base）做 stage 等于改写已经提交的工作。这个约束在代码里落地为：

[src/buffer_diff.rs:L1685-L1689](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1685-L1689) —— `is_stageable()`：一行判断，`base_kind == DiffBaseKind::Head` 才返回 `true`。

[src/buffer_diff.rs:L1681-L1683](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1681-L1683) —— `base_kind()` 访问器，供下游读取基准身份。

#### 4.2.4 代码实践

**实践目标**：验证「只有 Head 可 stage」，并理解其余三种基准的真实用途。

**操作步骤**：

1. 阅读下列示例代码（**示例代码**，可临时加入 `mod tests` 中运行，跑完后用 `git checkout -- crates/buffer_diff/src/buffer_diff.rs` 还原，注意不要提交）：

   ```rust
   // 示例代码：放在 mod tests 内，仿照 test_buffer_diff_simple 的写法
   #[gpui::test]
   async fn tutorial_u1l2_base_kinds(cx: &mut gpui::TestAppContext) {
       let buffer = Buffer::new(
           ReplicaId::LOCAL,
           BufferId::new(1).unwrap(),
           "hello\n".to_string(),
       );
       for kind in [
           DiffBaseKind::Head,
           DiffBaseKind::Index,
           DiffBaseKind::Oid,
           DiffBaseKind::Custom,
       ] {
           let stageable = cx.new(|cx| BufferDiff::new(&buffer, None, None, kind, cx))
               .update(cx, |diff, _| diff.is_stageable());
           println!("{kind:?} -> is_stageable={stageable}");
       }
   }
   ```

2. 运行：`cargo test -p buffer_diff tutorial_u1l2_base_kinds -- --nocapture`。

**需要观察的现象**：输出四行，只有 `Head -> is_stageable=true`。

**预期结果**：`Head -> true`、`Index -> false`、`Oid -> false`、`Custom -> false`（依据就是 L1687-L1689 那一行比较；若输出与此不符，请回头核对源码）。

#### 4.2.5 小练习与答案

**练习 1**：`Index` 基准的 diff 和 `Head` 基准的 diff 有什么区别？

**答案**：`Head` 基准比较的是「已提交内容 vs 工作区」，展示的是**全部**未提交改动（含已 staged 和未 staged）；`Index` 基准比较的是「暂存区 vs 工作区」，展示的是**尚未 staged** 的改动。两者的 hunk 可能有重叠，正因如此 crate 里才有 secondary diff 机制（单元四）。

**练习 2**：为什么 `Oid`（比如 merge base）基准的 diff 不允许 stage？

**答案**：stage 的语义是「把工作区的这段内容写入暂存区，使暂存区前进」。如果基准是 merge base，hunk 描述的是「自共同祖先以来的变化」，其中大量内容其实已在别的提交里；照搬 stage 会把这些已提交内容当作新改动改写进暂存区。源码注释原文："a diff against any other base (e.g. the merge base with another branch) would rewrite committed work"（[src/buffer_diff.rs:L31-L33](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L31-L33)）。

**练习 3**：举一个 `Custom` 基准在 Zed 里的真实用例。

**答案**：agent 修改代码时，把 agent 动手前的原文作为 `Custom` 基准构造 diff，就能高亮「agent 改了什么」——这与 git 完全无关（文档注释里还提到剪贴板、另一个文件作为基准的用法）。

### 4.3 BufferDiffSnapshot：不可变快照

#### 4.3.1 概念说明

`BufferDiffSnapshot` 是某一时刻 diff 结果的**定格**：一份 hunk 集合、基准文本快照、buffer 快照。它 derive 了 `Clone`，可以廉价地按值传递；所有查询 API（`hunks`、`hunks_intersecting_range` 等）都定义在它身上。引入快照的理由：

1. **一致性**：渲染一帧 gutter 时需要「同一时刻」的 hunk 与 buffer 状态，快照保证中途不被改。
2. **解耦生命周期**：查询方拿到快照后立刻释放实体，不会长时间锁住实体。

#### 4.3.2 核心流程

`BufferDiffSnapshot` 的组装发生在 `BufferDiff::snapshot()`：

```text
snapshot(cx)
   │
   ├─ diff_snapshot = Some(s) → 直接克隆 s（克隆成本很低）
   │
   ├─ diff_snapshot = None    → 兜底：构造空 hunk 树 + base_text_exists=false
   │                            的「空 diff」快照
   ▼
   若实体挂了 secondary_diff → 把对方快照包成 Arc 塞进 secondary_diff 字段
   ▼
   返回完整快照
```

#### 4.3.3 源码精读

[src/buffer_diff.rs:L47-L55](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L47-L55) —— 快照的六个字段：

- `hunks: SumTree<InternalDiffHunk>`：hunk 集合，存在持久化区间树里（树结构本身是单元二的主角）；
- `pending_hunks: SumTree<PendingHunk>`：乐观更新的「待生效 hunk」（单元四）；
- `base_text: language::BufferSnapshot`：基准文本自己的快照；
- `base_text_exists: bool`：基准是否存在——文件被删除、或尚未灌注基准时为 `false`；
- `buffer_snapshot: text::BufferSnapshot`：**计算 diff 那一刻**的 buffer 快照（旧版本），用于把锚点换算回旧坐标；
- `secondary_diff: Option<Arc<BufferDiffSnapshot>>`：挂载的另一个 diff 的快照。

[src/buffer_diff.rs:L2164-L2181](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2164-L2181) —— `snapshot()` 的完整实现：`unwrap_or_else` 分支就是「还没算过 diff」的兜底——空树、`base_text_exists: false`；随后无条件用实体的 `secondary_diff` 覆盖快照里的同名字段（还会 debug_assert 对方没有嵌套 secondary，防止链条无限延长）。

[src/buffer_diff.rs:L294-L309](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L294-L309) —— 快照上的几个基础查询：`is_empty()`（有没有 hunk）、`base_text_string()`（基准全文；`base_text_exists` 为 `false` 时返回 `None`，用 `then` 组合子实现）、`base_text_exists()`。

[src/buffer_diff.rs:L275-L284](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L275-L284) —— 测试专用捷径 `new_sync`：一行 `cx.new(|cx| BufferDiff::new_with_base_text(...))` 加一行取快照，下一节实践和单元一的测试大量用到它。

#### 4.3.4 代码实践

**实践目标**：亲眼确认「`new` 的快照是空 diff、`new_unchanged` 的快照是零 hunk 但基准存在」。

**操作步骤**（示例代码，临时加入 `mod tests`，跑完还原）：

```rust
// 示例代码
#[gpui::test]
async fn tutorial_u1l2_snapshot_fallback(cx: &mut gpui::TestAppContext) {
    let buffer = Buffer::new(
        ReplicaId::LOCAL,
        BufferId::new(1).unwrap(),
        "hello\n".to_string(),
    );
    let empty = cx.new(|cx| BufferDiff::new(&buffer, None, None, DiffBaseKind::Head, cx))
        .update(cx, |diff, cx| diff.snapshot(cx));
    println!(
        "new: is_empty={} base_text_exists={}",
        empty.is_empty(),
        empty.base_text_exists()
    );

    let unchanged = cx.new(|cx| {
        BufferDiff::new_unchanged(&buffer, None, None, DiffBaseKind::Head, cx)
    })
    .update(cx, |diff, cx| diff.snapshot(cx));
    println!(
        "new_unchanged: is_empty={} base_text_exists={}",
        unchanged.is_empty(),
        unchanged.base_text_exists()
    );
}
```

运行：`cargo test -p buffer_diff tutorial_u1l2_snapshot_fallback -- --nocapture`。

**需要观察的现象**：两行输出，四个布尔值。

**预期结果**：`new: is_empty=true base_text_exists=false`（走了 L2165-L2175 的兜底分支）；`new_unchanged: is_empty=true base_text_exists=true`（L1632-L1639 手工构造的快照）。两者的 `is_empty` 都是 `true`，但含义不同：前者是「还没算」，后者是「算了，确实没有差异」。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`base_text_exists = false` 都出现在哪些情况？

**答案**：至少两种：一是文件在 git 里是新增的、还没有任何基准可灌注；二是实体刚 `new` 出来、基准尚未通过 `set_base_text` 灌注（快照兜底分支 L2171 硬编码了 `false`）。

**练习 2**：`snapshot()` 里为什么每次都要重新执行 `snapshot.secondary_diff = ...` 这一步（L2176-L2179），而不是构造快照时设置一次？

**答案**：`secondary_diff` 是实体上会变化的字段（可挂可摘），而 `diff_snapshot` 可能是很久前算好的；每次取快照时重新装配，保证拿到的 secondary 永远是**当前**挂载的那个 diff 的最新快照。

**练习 3**：快照里 `buffer_snapshot` 存的是「计算 diff 时的旧 buffer」，为什么查询 API 还要求调用方额外传入**当前** buffer？

**答案**：hunk 位置以 `Anchor` 存储，锚点要落到具体某个 buffer 版本上才能解析成坐标：对旧快照用旧 buffer 可得到 diff 计算时的坐标，对新 buffer 用新坐标渲染。`hunks(...)` 的参数 `buffer_snapshot`（见 [src/buffer_diff.rs:L430-L438](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L430-L438)）就是「以哪个版本的 buffer 视角解析锚点」的语境。

### 4.4 DiffHunk：一块差异的完整描述

#### 4.4.1 概念说明

`DiffHunk` 是对外暴露的「一块差异」，描述：base 里的一段文本，在 buffer 里被替换成了另一段文本。它是查询 API（如 `snapshot.hunks(&buffer)`）逐个吐出的条目。内部还有个孪生结构 `InternalDiffHunk`，区别很小：内部版多存一个 `diff_base_point_range`（base 侧行号区间），对外版多存 `range`（buffer 侧行号区间）——对外版在遍历时按需换算生成。

#### 4.4.2 核心流程

一个 hunk 的字段可以按「两侧 + 状态 + 词级」分组理解：

```text
                 base 侧                    buffer 侧
        ┌───────────────────────┐  ┌────────────────────────┐
        │ diff_base_byte_range  │  │ buffer_range (锚点)     │
        │ (字节区间)             │  │ range (换算后的 Point)  │
        │ base_word_diffs       │  │ buffer_word_diffs      │
        │ (相对删除段起点的偏移)  │  │ (锚点)                 │
        └───────────────────────┘  └────────────────────────┘
                     状态：secondary_status（是否已 staged 等）

status() 的三分支推导：
  buffer_range 为空（start == end）      → Deleted（base 有、buffer 没有）
  否则 diff_base_byte_range 为空          → Added  （buffer 新增）
  否则                                    → Modified
```

#### 4.4.3 源码精读

[src/buffer_diff.rs:L115-L129](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L115-L129) —— `DiffHunk` 六个字段，逐一说明：

- `range: Range<Point>`：hunk 覆盖的 buffer 行区间（便捷字段，由 `buffer_range` 换算而来）；
- `buffer_range: Range<Anchor>`：同一区间的锚点表示，buffer 继续编辑后仍然有效；
- `diff_base_byte_range: Range<usize>`：**基准文本里的字节区间**——用它去切基准字符串，得到被删掉的文本（注意单位是字节不是行）；
- `secondary_status`：这一块在「工作区 vs 暂存区」视角下的状态，默认 `NoSecondaryHunk`；
- `buffer_word_diffs: Vec<Range<Anchor>>`：hunk 内**词级**差异的位置（锚点），供编辑器在行内高亮改动的单词；
- `base_word_diffs: Vec<Range<usize>>`：词级差异在 base 侧的表示——**相对删除段起点的偏移量**，所以两侧用了不同类型。

[src/buffer_diff.rs:L131-L139](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L131-L139) —— `InternalDiffHunk`：存储在 SumTree 里的内部形态，多一个 `diff_base_point_range`（base 侧行号），没有 `range`。

[src/buffer_diff.rs:L86-L97](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L86-L97) —— `DiffHunkStatus` 与 `DiffHunkStatusKind`：状态 = 「种类（Added/Modified/Deleted）」+「secondary 状态」的组合。

[src/buffer_diff.rs:L2297-L2309](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2297-L2309) —— `DiffHunk::status()`：就是 4.4.2 流程图的那三条 if-else，判定顺序很重要——**先看 buffer 侧是否为空（Deleted），再看 base 侧是否为空（Added）**，都非空才是 Modified。

[src/buffer_diff.rs:L2290-L2295](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2290-L2295) —— `is_created_file()`：三个条件同时成立（base 区间为 `0..0`、buffer 侧覆盖整个 buffer 从头到尾）即「整个文件是新增的」——这是 git 面板判断「新文件」的依据。

[src/buffer_diff.rs:L99-L113](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L99-L113) —— `DiffHunkSecondaryStatus` 五种取值，本讲只需认识 `NoSecondaryHunk`（无三方信息）；其余四种（已 staged / 部分 staged / 正在 stage / 正在 unstage）留到单元四。

#### 4.4.4 代码实践

**实践目标**：用测试当标本，练习「从 hunk 字段反推 diff 结果」。

**操作步骤**：

1. 精读 [src/buffer_diff.rs:L2442-L2496](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2442-L2496) 的 `test_buffer_diff_simple`：base 是 `one/two/three`，buffer 把 `two` 改成了 `HELLO`。断言四元组 `(1..2, "two\n", "HELLO\n", modified_none())` 的含义依次是：buffer 第 1 行、被删文本、新增文本、状态。
2. 再看断言工具 [src/buffer_diff.rs:L2387-L2423](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2387-L2423)：它把每个 hunk 变形成 `(hunk.range, &diff_base[hunk.diff_base_byte_range], buffer 中 hunk.range 的文本, hunk.status())` 再与期望逐项比较——这正好就是本讲四个字段的「读取姿势」。
3. 留意测试第二段的两个 hunk：`buffer.edit([(0..0, "point five\n")])` 在最前面插入一行后，`two→HELLO` 那个 hunk 的**行号**从 `1..2` 平移到了 `2..3`——这就是锚点自动跟随后换算成新 Point 的效果。

**需要观察的现象 / 预期结果**：无需修改代码，运行 `cargo test -p buffer_diff test_buffer_diff_simple` 应通过（该测试是仓库既有测试）。

#### 4.4.5 小练习与答案

**练习 1**：base 为 `"a\nb\n"`，buffer 为 `"a\n"`（删除了 `b`）。请预测 hunk 的 `diff_base_byte_range` 与 `status().kind`。

**答案**：`diff_base_byte_range = 2..4`（`"b\n"` 的字节区间：`a\n` 占 0..2）；buffer 侧区间为空，`status().kind = Deleted`。精确的 buffer 侧锚点落点建议待本地验证。

**练习 2**：为什么 `buffer_word_diffs` 用锚点、`base_word_diffs` 用整数偏移？

**答案**：buffer 侧位置要长期有效（用户还会继续编辑，行内高亮必须跟着走），所以用 `Anchor`；base 侧文本是**只读**的、永远不会变，且词级差异只需要相对删除段起点的偏移即可定位，用 `usize` 更省也更简单。

**练习 3**：`is_created_file()` 为什么不检查「hunk 数量是否为 1」？

**答案**：它检查的是**这个 hunk 自身**的形状：base 侧区间为空（`0..0`）且 buffer 侧覆盖全 buffer（起点是 min 锚点、终点是 max 锚点）。整文件新增时 diff 算法只会产出一个满足此形状的 hunk，所以逐 hunk 判断即可，不需要全局信息。

## 5. 综合实践

**任务**：亲手构造一个 diff，把每个 hunk 的关键字段全部打印出来，并与手工推算的结果逐项核对——这是把本讲四个名词串起来的最好方式。

**背景**：实践任务指定使用 `BufferDiff::new_with_base_text`，它是测试专用构造函数（`#[cfg(any(test, feature = "test-support"))]`），一步完成「构造 + 灌注基准 + 同步计算 diff」：见 [src/buffer_diff.rs:L1651-L1675](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1651-L1675)。注意三处细节：内部复用 `new` 并**硬编码 `DiffBaseKind::Head`**（L1657）；会先做行尾归一化 `LineEnding::normalize`（L1659，CRLF 会被转成 LF）；然后用 `block_on` 同步算完 diff 并 `set_snapshot`（L1667-L1673）。

**操作步骤**（以下为**示例代码**，临时加入 `mod tests` 内；练习属于你自己的工作副本改动，跑完请用 `git checkout -- crates/buffer_diff/src/buffer_diff.rs` 还原，不要提交）：

```rust
// 示例代码
#[gpui::test]
async fn tutorial_u1l2_print_hunks(cx: &mut gpui::TestAppContext) {
    let diff_base = "one\ntwo\nthree\nfour\n".to_string();
    let buffer_text = "one\nTWO\nthree\nFOUR\n".to_string();

    let buffer = Buffer::new(ReplicaId::LOCAL, BufferId::new(1).unwrap(), buffer_text);
    let diff = cx.new(|cx| BufferDiff::new_with_base_text(&diff_base, &buffer, cx));
    let snapshot = diff.update(cx, |diff, cx| diff.snapshot(cx));

    for hunk in snapshot.hunks(&buffer) {
        let deleted = &diff_base[hunk.diff_base_byte_range.clone()];
        let added = buffer.text_for_range(hunk.range.clone()).collect::<String>();
        println!(
            "range={:?} diff_base_byte_range={:?} deleted={:?} added={:?} status={:?}",
            hunk.range, hunk.diff_base_byte_range, deleted, added, hunk.status()
        );
    }
}
```

运行：`cargo test -p buffer_diff tutorial_u1l2_print_hunks -- --nocapture`。

**手工推算的预期结果**（请逐项核对实际输出）：

| 项 | hunk 1 | hunk 2 |
| --- | --- | --- |
| 改动 | `two` → `TWO` | `four` → `FOUR` |
| `range`（行区间） | `Point(1,0)..Point(2,0)` | `Point(3,0)..Point(4,0)` |
| `diff_base_byte_range` | `4..8`（`"two\n"`） | `14..19`（`"four\n"`） |
| `deleted` / `added` | `"two\n"` / `"TWO\n"` | `"four\n"` / `"FOUR\n"` |
| `status().kind` | `Modified` | `Modified` |
| `status().secondary` | `NoSecondaryHunk` | `NoSecondaryHunk` |

字节区间的算法：`"one\n"` 占 0..4，`"two\n"` 占 4..8，`"three\n"` 占 8..14，`"four\n"` 占 14..19。两个改动之间隔着未修改的第 2 行（`three`），所以是**两个独立 hunk**。hunk 终点是否恰好落在下一行行首（`Point(2,0)` / `Point(4,0)`）请以实际输出为准——待本地验证。

**延伸**（可选）：把 buffer 文本换成只改一处且新增一整行的版本，预测哪些字段变成 `Added`、`diff_base_byte_range` 变成空区间，再运行核对。

## 6. 本讲小结

- `BufferDiff` 是 gpui 实体：持有 buffer_id、只读的 `base_text_buffer`、最近一次的 `diff_snapshot`，以及可选的 `secondary_diff`；diff 计算是异步回写到它身上的。
- `BufferDiffSnapshot` 是不可变快照：两棵 `SumTree`（hunks 与 pending hunks）加基准与 buffer 的快照；实体还没算过 diff 时 `snapshot()` 返回「空 hunk + `base_text_exists=false`」的兜底快照。
- `DiffHunk` 用锚点描述 buffer 侧区间（`buffer_range`），用字节区间描述 base 侧（`diff_base_byte_range`）；`status()` 按「buffer 空 → Deleted，base 空 → Added，否则 Modified」推导种类。
- 词级差异两侧表示不同：`buffer_word_diffs` 存锚点、`base_word_diffs` 存相对删除段起点的偏移。
- `DiffBaseKind` 有 Head / Index / Oid / Custom 四种；只有 `Head` 使 `is_stageable()` 为 `true`，因为对其他基准 stage 会改写已提交的工作。

## 7. 下一步学习建议

下一讲（u1-l3「把项目跑起来」）将带你熟悉 `cargo test -p buffer_diff` 的运行方式、`#[gpui::test]` 与 `TestAppContext` 的关系，以及 `assert_hunks` 四元组断言的写法——本讲综合实践里那些「待本地验证」的项目，届时都会一一落实。

之后进入单元二时，建议重点重读 [src/buffer_diff.rs:L183-L213](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L183-L213)（`InternalDiffHunk` 的 `sum_tree::Item` 实现）和 [src/buffer_diff.rs:L176-L181](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L176-L181)（`DiffHunkSummary`），那是理解「hunk 怎么被存进区间树」的入口。
