# 坐标映射：patch_for_buffer_range 与 Patch 组合

## 1. 本讲目标

前几讲我们解决了「hunk 存在哪里、怎么查出来」。本讲解决一个更隐蔽但同样关键的问题：**hunk 的坐标是 diff 计算那一刻的，而调用方手里的 buffer 可能已经被继续编辑过了，同时很多调用方根本不用 buffer 坐标，而是用 base text（如 git HEAD）的坐标说话**。

学完本讲，你应该能：

1. 理解 `text` crate 中 `Patch` 的 `old`/`new` 区间语义，以及 `invert`、`compose`、`edit_for_old_position` 三个操作的含义。
2. 推导出「当前 buffer 坐标 → base 坐标」的映射为什么是 \( E^{-1} \circ H \)（先撤销增量编辑、再应用 hunk 编辑）。
3. 读懂 `patch_for_buffer_range` 里 `prefix_edit`（前缀折叠编辑）与 SumTree 游标拼接的配合方式。
4. 熟练使用 `buffer_point_to_base_text_point` 等四个点级映射函数，并知道它们的边界规则。

## 2. 前置知识

### 2.1 三份文本、两次坐标跳跃

buffer_diff 的世界里同一时刻存在三份文本，理解本讲的关键就是把它们分清：

```text
   ① base text                ② original buffer snapshot        ③ 当前 buffer
   (如 git HEAD 内容)          (diff 计算那一刻的 buffer)          (调用方手里的最新 buffer)
        │                            │                                │
        │◄──── H：hunk 编辑 ────────►│◄──── E：edits_since ──────────►│
```

- **① base text**：diff 的基准（`BufferDiffSnapshot.base_text`，见 [src/buffer_diff.rs:L48-L55](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L48-L55)）。
- **② original buffer snapshot**：hunks 树里锚点所依附的快照，通过 [`original_buffer_snapshot()`](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L319-L321) 访问。
- **③ 当前 buffer**：查询 API 的 `buffer` 参数，可能与 ② 相同（没有新编辑），也可能更新。

hunk 描述的是 ① 与 ② 的差异；调用方给的却是 ③ 的坐标。所以任何坐标映射都要先跨过 \( E \)（②→③ 的增量编辑），再跨过 \( H \)（②→① 的 hunk 编辑）。

### 2.2 `Edit` 与 `Patch`：一次「折叠替换」

`text` crate 的 [`Edit`](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/text/src/patch.rs#L88-L94) 是一对区间：`old`（原文里的区间）和 `new`（替换后的区间）。`Patch` 就是一组按 `old` 严格递增排列的 `Edit`。它来自 `text` crate，在 buffer_diff 顶部被直接导入（[src/buffer_diff.rs:L15-L17](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L15-L17)）。

把一个 patch 想象成「把 old 区间的内容整体折叠成 new 区间的内容」，那么对一个坐标点 `p` 的映射遵循三条规则：

| `p` 的位置 | 映射结果 |
| --- | --- |
| 在所有编辑之前 | 恒等（原样返回） |
| 落在某条编辑的 `old` 区间内 | 折叠到该编辑的 `new` 区间（整段对应整段） |
| 在某条编辑的 `old.end` 之后 | 按之前所有编辑的累计长度差平移 |

用公式表达第三条：设之前的编辑累计让文本长度变化了 \( \Delta L \)（删除为负、插入为正），则

\[ \text{map}(p) = p + \Delta L \]

这正是 git diff 里「删除了 2 行之后，后面的行号都要减 2」的推广。

## 3. 本讲源码地图

| 文件与区段 | 作用 |
| --- | --- |
| [src/buffer_diff.rs:L523-L597](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L523-L597) | `patch_for_buffer_range`：buffer 坐标 → base 坐标 |
| [src/buffer_diff.rs:L599-L638](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L599-L638) | `patch_for_buffer_range_naive`：全量对照实现（仅测试） |
| [src/buffer_diff.rs:L640-L716](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L640-L716) | `patch_for_base_text_range`：base 坐标 → buffer 坐标 |
| [src/buffer_diff.rs:L718-L749](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L718-L749) | `patch_for_base_text_range_naive`：全量对照实现（仅测试） |
| [src/buffer_diff.rs:L751-L797](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L751-L797) | 四个点级映射函数（range 版与 point 版） |
| [src/buffer_diff.rs:L487-L521](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L487-L521) | 游标定位辅助：按 base 偏移 / 按 buffer 锚点找「前一个 hunk」 |
| [src/buffer_diff.rs:L4121-L4334](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L4121-L4334) | `test_patch_for_range_random`：随机对照测试 |
| [crates/text/src/patch.rs:L40-L45](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/text/src/patch.rs#L40-L45)、[L88-L94](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/text/src/patch.rs#L88-L94)、[L214-L274](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/text/src/patch.rs#L214-L274) | `Patch` 的 `invert` / `compose` / `old_to_new` / `edit_for_old_position` |
| [crates/editor/src/split.rs:L47-L86](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/editor/src/split.rs#L47-L86) | 下游：diff 分屏视图用这些 patch 翻译选区与补丁 |
| [crates/editor/src/git.rs:L2889-L2918](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/editor/src/git.rs#L2889-L2918) | 下游：`get_permalink_to_line` 把 buffer 选区行号换成 HEAD 行号 |
| [crates/project/src/git_store.rs:L1343](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/project/src/git_store.rs#L1343) | 下游：staging 时用 `base_text_range_for_buffer_range` 求索引侧范围 |

本讲的「最小模块」划分：**Patch 代数** → **映射公式推导** → **patch_for_buffer_range** → **patch_for_base_text_range** → **点级映射函数与 naive 对照**。

## 4. 核心概念与源码讲解

### 4.1 Patch 代数：invert、compose 与 edit_for_old_position

#### 4.1.1 概念说明

`Patch` 不仅是「一组编辑」的容器，它还是一个可以做代数运算的对象：

- **`invert`**：交换每条编辑的 `old` 与 `new`，得到反向 patch。如果一个 patch 把文本 A 变成文本 B，反转后就把 B 的坐标映射回 A 的坐标。
- **`compose`**：把两个 patch 首尾相接。若 patch 甲把 A 变 B、乙把 B 变 C，则 `甲.compose(乙)` 把 A 直接变 C。
- **`edit_for_old_position`**：查询接口——给定原坐标中的一个点，返回「碰到」这个点的编辑（见下方源码注释的闭区间定义）。

buffer_diff 的坐标映射完全建立在这三个操作之上，所以先把它们吃透，后面的代码就是纯拼装。

#### 4.1.2 核心流程

`edit_for_old_position(p)` 的查找算法：

```text
1. 二分找到最后一条 old.start <= p 的编辑
2. 若没有（p 在所有编辑之前）→ 返回 p..=p 的空编辑（恒等映射）
3. 若 p > edit.old.end（p 在这条编辑之后）→ 构造空编辑：
      new = edit.new.end + (p - edit.old.end)   # 按长度差平移
4. 否则（p 落在 [old.start, old.end] 闭区间内）→ 返回整条编辑本身
```

注意第 4 步：落在编辑区间**内部**的点不映射到某个具体点，而是「沾上」整条编辑——调用方拿到 `edit.new` 这个完整区间自行决定取头还是取尾（这正是 4.5 节四个 point 函数做的事）。

#### 4.1.3 源码精读

[invert：逐条交换 old 与 new](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/text/src/patch.rs#L40-L45)，就地修改、返回 `&mut Self` 以便链式调用：

```rust
pub fn invert(&mut self) -> &mut Self {
    for edit in &mut self.0 {
        mem::swap(&mut edit.old, &mut edit.new);
    }
    self
}
```

[compose：双指针归并两串编辑](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/text/src/patch.rs#L88-L94)，维护 `old_start`/`new_start` 两个累计游标，处理新旧编辑的重叠与切分：

```rust
pub fn compose(&self, new_edits_iter: impl IntoIterator<Item = Edit<T>>) -> Self {
    let mut old_edits_iter = self.0.iter().cloned().peekable();
    let mut new_edits_iter = new_edits_iter.into_iter().peekable();
    ...
```

[edit_for_old_position：按 old 区间二分查找](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/text/src/patch.rs#L241-L274)，源码注释明确写了「触边即命中」的闭区间语义（[L236-L239](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/text/src/patch.rs#L236-L239)）。关键分支：

```rust
if old > edit.old.end {
    let translated = edit.new.end + (old - edit.old.end); // 平移
    Edit { new: translated..translated, old: old..old, ... }
} else {
    edit.clone() // 落在闭区间内：整条编辑沾上这个点
}
```

同文件的 [`old_to_new`](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/text/src/patch.rs#L214-L234) 是它的点值版本，内部区间折叠到 `edit.new.start`，越界点平移——`patch_for_buffer_range` 正是用它来换算查询范围的端点。

#### 4.1.4 代码实践

**实践目标**：用一条行内编辑验证 2.2 节表格里的三条映射规则。

**操作步骤**：在 `crates/buffer_diff/src/buffer_diff.rs` 底部的 `mod tests` 里临时新增一个测试（`Edit`、`Patch`、`Point` 已随 `use super::*` 导入），然后运行：

```bash
cargo test -p buffer_diff test_patch_albedo_practice -- --nocapture
```

```rust
#[gpui::test]
async fn test_patch_albedo_practice(_: &mut gpui::TestAppContext) {
    // 一条行内编辑：把 (0,5)..(0,10) 的 5 列替换成 3 列（删了 2 列）
    let patch = Patch::new(vec![Edit {
        old: Point::new(0, 5)..Point::new(0, 10),
        new: Point::new(0, 5)..Point::new(0, 8),
    }]);

    // 规则一：编辑之前的点 → 恒等
    assert_eq!(
        patch.edit_for_old_position(Point::new(0, 2)).new,
        Point::new(0, 2)..Point::new(0, 2)
    );
    // 规则二：编辑区间内的点 → 沾上整条编辑
    assert_eq!(
        patch.edit_for_old_position(Point::new(0, 7)).new,
        Point::new(0, 5)..Point::new(0, 8)
    );
    // 规则三：编辑之后的点 → 按长度差平移（删 2 列，左移 2）
    assert_eq!(
        patch.edit_for_old_position(Point::new(0, 20)).new,
        Point::new(0, 18)..Point::new(0, 18)
    );
}
```

**需要观察的现象**：三个断言分别命中二分查找的三个分支。

**预期结果**：测试通过。若把第三个断言的期望改成 `(0, 20)`，`pretty_assertions` 会给出红绿对照的失败输出。

**待本地验证**：以上断言系依据 `edit_for_old_position` 源码逐分支手工推演，请以本地运行结果为准。

#### 4.1.5 小练习与答案

**练习 1**：对上面的 patch 调用 `invert()` 之后，`edit_for_old_position(Point::new(0, 20)).new` 变成什么？
**答案**：invert 后 old/new 交换：`old = (0,5)..(0,8)`、`new = (0,5)..(0,10)`。`(0,20)` 在 old.end 之后，平移 `new.end + ((0,20)-(0,8))` = `(0,10)+(0,12)` = `(0,22)`（之前删了 2 列，反向看就是「补回了 2 列」，后面的点右移 2）。

**练习 2**：为什么 `invert` 不需要重新排序编辑，而 `compose` 需要 `peekable` 双指针？
**答案**：原 patch 按 `old` 严格递增且互不相交，交换后按新 `old`（原 `new`）同样严格递增且互不相交，顺序天然合法；而 `compose` 要合并两串来自不同坐标空间的编辑，必须逐条比较位置、切分重叠部分，所以是归并式算法。

**练习 3**：`edit_for_old_position` 在点恰好等于 `old.end` 时返回什么？
**答案**：返回整条编辑（条件是 `old > edit.old.end` 才走平移分支，等于不算）。此时 `.new` 是完整的新区间，`old_to_new` 则会给出 `new.end`——两个函数在边界上的细微差别正是 4.5 节 point 函数要显式处理的。

### 4.2 为什么映射是 \( E^{-1} \circ H \)：三时间点推导

#### 4.2.1 概念说明

现在把 2.1 节的三份文本形式化。定义两个 patch：

- \( E \)：`edits_since`，original buffer → 当前 buffer 的增量编辑（由 `buffer.edits_since::<Point>(&self.buffer_snapshot.version)` 取出）；
- \( H \)：hunk 编辑，original buffer → base text（每条形如 `old = hunk.buffer_range`（original 坐标）、`new = hunk.diff_base_byte_range` 换算成 base 的 Point）。

要回答「当前 buffer 的点 \( p \) 对应 base 的哪里」，路径是：

\[ P_{\text{cur} \to \text{base}} = E^{-1} \circ H \]

即先做 \( E^{-1} \)（把当前坐标折叠回 diff 计算时的 original 坐标），再做 \( H \)（把 original 坐标折叠到 base 坐标）。反方向则是：

\[ P_{\text{base} \to \text{cur}} = H^{\leftrightarrow} \circ E \]

其中 \( H^{\leftrightarrow} \) 表示每条 hunk 编辑的 old/new 交换（old = base 区间、new = original 区间），而 \( E \) **不取反**——两段方向本来就顺着。这两个公式就是 `patch_for_buffer_range` 与 `patch_for_base_text_range` 的全部数学内容。

#### 4.2.2 核心流程

以 `patch_for_buffer_range(range, buffer)` 为例：

```text
1. 取 E = buffer.edits_since(② 的 version)，得 Patch
2. E.invert()                                  → 得 E⁻¹
3. 从 hunks 树挑出与 range 相关的 hunk，逐个转成
   Edit { old: buffer_range(②), new: base 区间 } → 得 H（的部分）
4. E⁻¹.compose(H 的迭代器)                      → 返回复合 patch
```

`compose` 的参数是迭代器——这允许第 3 步「边遍历 SumTree 边产编辑」，内存里不需要中间集合。

#### 4.2.3 源码精读

两个函数开头的取材完全对应公式。先是 [patch_for_buffer_range 取 E 并取反](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L539-L544)：

```rust
let mut edits_since_diff = Patch::new(
    buffer
        .edits_since::<Point>(&self.buffer_snapshot.version)
        .collect::<Vec<_>>(),
);
edits_since_diff.invert();
```

`self.buffer_snapshot` 就是 ②（original buffer snapshot）；`edits_since` 给出 ②→③ 的增量，`invert` 后方向变为 ③→②。最后 [ compose 收尾](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L596)：

```rust
edits_since_diff.compose(hunk_iter)   // E⁻¹ ∘ H
```

而 [patch_for_base_text_range 的取材](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L656-L658) **不 invert**，最后 [compose 的顺序也反过来](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L715)：

```rust
let edits_since_diff = buffer
    .edits_since::<Point>(&self.buffer_snapshot.version)
    .collect::<Vec<_>>();          // E，方向 ②→③，保持原样
...
Patch::new(hunk_patch).compose(edits_since_diff)   // H↔ ∘ E
```

hunk 编辑在这里构造时 old/new 正好与 4.3 相反（old 是 base 区间、new 是 buffer 区间，见 [L708-L711](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L708-L711)）。

#### 4.2.4 代码实践

**实践目标**：亲眼确认「diff 快照的版本落后于当前 buffer」，理解为什么必须复合 \( E \)。

**操作步骤**：在测试模块新增（`BufferDiffSnapshot::new_sync` 是测试专用同步构造，见 [L277-L284](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L277-L284)）：

```rust
#[gpui::test]
async fn test_edits_since_albedo_practice(cx: &mut gpui::TestAppContext) {
    let mut buffer = Buffer::new(
        ReplicaId::LOCAL,
        BufferId::new(1).unwrap(),
        "one\nTWO\nthree\n",
    );
    let diff = BufferDiffSnapshot::new_sync(&buffer, "one\ntwo\nthree\n".to_string(), cx);

    // 此刻没有新编辑：E 为空
    let before: Vec<Edit<Point>> =
        buffer.edits_since::<Point>(diff.buffer_version()).collect();
    assert!(before.is_empty());

    // diff 算完之后再编辑 buffer（不重算 diff！）
    buffer.edit([(0..0, "zero\n")]);

    // E 恰好一条：在开头插入 "zero\n"
    let after: Vec<Edit<Point>> =
        buffer.edits_since::<Point>(diff.buffer_version()).collect();
    assert_eq!(after.len(), 1);
}
```

**需要观察的现象**：`diff.buffer_version()`（[L315-L317](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L315-L317)）返回的是 ② 的版本；编辑后 `edits_since` 的结果从空变成一条。

**预期结果**：测试通过。这条 `edits_since` 正是 4.2.3 源码里 `patch_for_buffer_range` 第一行取的东西。

**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：如果查询时传入的 `buffer` 与 ② 完全相同（没有新编辑），\( E \) 是什么？映射公式退化成什么？
**答案**：\( E \) 为空 patch，\( E^{-1} \circ H = H \)。这就是 u1-l3 里 `new_sync` 之后立即查询的情形——hunk 编辑直接就是最终映射。

**练习 2**：`buffer_point_to_base_text_point` 能否写成 `H ∘ E`（顺序颠倒）？
**答案**：不能。\( H \) 的 old 坐标空间是 ②，而调用方给的点在 ③ 里，直接喂给 \( H \) 会把「③ 的坐标」误当「② 的坐标」解释；必须先用 \( E^{-1} \) 把点折回 ②，类型（坐标空间）才对得上。坐标空间的匹配就像单位换算，米不能直接加英尺。

**练习 3**：为什么 `patch_for_base_text_range` 里 \( E \) 不需要取反？
**答案**：查询点本来就在 base 空间：先 \( H^{\leftrightarrow} \)（base→②），再 \( E \)（②→③），两段方向顺次衔接，天然复合；取反反而会把方向弄反。

### 4.3 patch_for_buffer_range：prefix_edit 与游标拼接

#### 4.3.1 概念说明

有了公式，为什么还需要一整节？因为直接把**全部** hunk 转成编辑再 compose 是 \( O(\text{hunk 数}) \) 的全量工作，而查询往往只关心一小段范围。函数文档（[L523-L526](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L523-L526)）承诺：**返回的 patch 只保证在给定范围内准确**。于是实现可以「跳过」范围之前的 hunk——但跳过不等于无害：之前的 hunk 累计了长度差，直接影响范围内点的平移量。`prefix_edit` 就是把「文件开头到上一个 hunk 结束」的全部累计位移**折叠成一条编辑**带进结果。

#### 4.3.2 核心流程

```text
0. 若 base 不存在：返回单条编辑 old=(0,0)..max_point → new=(0,0)..(0,0)
   （整个 buffer 都是新增的，base 里一无所有）
1. E⁻¹ = edits_since 取反
2. start_point = E⁻¹.old_to_new(range.start)，再与第一条编辑的 new.start 取 min
   range_end   = E⁻¹.old_to_new(range.end)， 再与最后一条编辑的 new.end 取 max
   —— 两处 min/max 是「宁多勿漏」校正：确保与增量编辑区域重叠的 hunk 不被漏掉
3. 用 hunk_before_buffer_anchor 把 SumTree 游标定位到 start 之前最后一个 hunk；
   item 为空时 next() 对齐
4. prefix_edit = 由游标的前一个 hunk 构造：
   old = (0,0)..prev.buffer_range.end(②)，new = (0,0)..prev.diff_base.end(①)
5. hunk_iter：先吐出 prefix_edit，再逐个吐出范围内 hunk 的
   Edit { old: buffer_range(②), new: base 区间 }，越过 range_end 即停
6. 返回 E⁻¹.compose(hunk_iter)
```

#### 4.3.3 源码精读

[base 不存在的短路分支](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L532-L537)——新文件场景，整个 buffer 折叠为空：

```rust
if !self.base_text_exists {
    return Patch::new(vec![Edit {
        old: Point::zero()..buffer.max_point(),
        new: Point::zero()..Point::zero(),
    }]);
}
```

[起点与终点的「宁多勿漏」校正](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L546-L573)：`old_to_new` 把 ③ 的端点折回 ② 后，再向两侧扩张到首末编辑的边界：

```rust
let mut start_point = edits_since_diff.old_to_new(*range.start());
if let Some(first_edit) = edits_since_diff.edits().first() {
    start_point = start_point.min(first_edit.new.start);   // 往前压
}
...
let mut range_end = edits_since_diff.old_to_new(*range.end());
if let Some(last_edit) = edits_since_diff.edits().last() {
    range_end = range_end.max(last_edit.new.end);          // 往后拉
}
```

原因：若 `range.start` 落在首条增量编辑**之后**，`old_to_new` 会给出编辑之后的 ② 坐标；而 hunk 的 `buffer_range.start` 可能落在该编辑覆盖的区域内部——不把扫描起点压回 `first_edit.new.start` 就会漏掉这类 hunk，合成结果在范围起点附近的平移量就是错的。

[游标定位与 prefix_edit 构造](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L554-L567)，借助 u2-l2 讲过的 SumTree 游标（`hunk_before_buffer_anchor` 定义在 [L505-L521](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L505-L521)）：

```rust
let mut cursor = self.hunks.cursor(original_snapshot);
self.hunk_before_buffer_anchor(
    original_snapshot.anchor_before(start_point),
    &mut cursor,
    original_snapshot,
);
if cursor.item().is_none() {
    cursor.next();
}

let mut prefix_edit = cursor.prev_item().map(|prev_hunk| Edit {
    old: Point::zero()..prev_hunk.buffer_range.end.to_point(original_snapshot),
    new: Point::zero()..prev_hunk.diff_base_byte_range.end.to_point(base_text),
});
```

prefix_edit 的形状值得琢磨：它**不描述任何具体 hunk**，而是把「从文件开头到上一个 hunk 结束」之间的净位移打包成一条编辑——范围内所有点的平移量都由它携带。之后 [hunk_iter 用 `std::iter::from_fn` 惰性产出](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L575-L594)：先吐 prefix_edit，再逐个吐范围内 hunk（old 是 ② 的 Point 区间、new 是 base 字节区间经 `to_point(base_text)` 转成的 Point 区间），hunk 起点越过 `range_end` 立即停止——范围外的 hunk 根本不会被触碰。

#### 4.3.4 代码实践

**实践目标**：亲眼看到复合 patch 里同时含有「增量编辑」和「hunk 编辑」两种成分。

**操作步骤**：

```rust
#[gpui::test]
async fn test_composed_patch_print_practice(cx: &mut gpui::TestAppContext) {
    let mut buffer = Buffer::new(
        ReplicaId::LOCAL,
        BufferId::new(1).unwrap(),
        "one\nTWO\nthree\n",
    );
    let diff = BufferDiffSnapshot::new_sync(&buffer, "one\ntwo\nthree\n".to_string(), cx);
    buffer.edit([(0..0, "zero\n")]); // diff 之后再插入一行，不重算 diff
    let snapshot = buffer.snapshot();

    // 查询当前 buffer 里 "TWO" 行中的一个点（当前坐标 (2,1)）
    let patch = diff.patch_for_buffer_range(Point::new(2, 1)..=Point::new(2, 1), &snapshot);
    for edit in patch.edits() {
        println!("{:?} -> {:?}", edit.old, edit.new);
    }
}
```

**需要观察的现象**：输出两条编辑（用 `--nocapture` 运行）：

```text
(0, 0)..(0, 5) -> (0, 0)..(0, 0)   ← E⁻¹ 的产物：diff 后插入的 "zero\n" 在 base 里不存在，折叠为空
(2, 0)..(3, 0) -> (1, 0)..(2, 0)   ← H 的产物：TWO 行 hunk，old 已折算成当前坐标
```

**预期结果**：两条编辑的 old 侧都是**当前 buffer 坐标**、new 侧都是 **base 坐标**；把这条 patch 喂给 `edit_for_old_position(Point::new(2, 1))` 会命中第二条并折叠到 `(1,0)..(2,0)`。

**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：如果删掉 prefix_edit 的构造（`cursor.prev_item().map(...)` 返回 None 时直接跳过），查询结果会怎么错？
**答案**：范围内点的平移会缺失「之前所有 hunk 累计的长度差」。例如前面有一个删了 10 行的 hunk，那么范围内点映射到 base 时会偏后 10 行。当查询范围之前没有任何 hunk 时（`prev_item()` 为 None）才无影响——这也解释了为什么实现里用 `Option<Edit>` 自然处理「没有前缀」的情形。

**练习 2**：`hunk_iter` 停止条件用的是 hunk 的**起点**与 `range_end` 比较，而不是终点，会多产出编辑吗？有害吗？
**答案**：起点 ≤ range_end 而终点越过 range_end 的 hunk 也会被产出，即结果可能比「严格相交」多包含尾部 hunk。无害——文档只承诺给定范围内准确，多出的编辑不影响范围内点的映射，却避免了边界判断的复杂化，还是「宁多勿漏」。

**练习 3**：`old: hunk.buffer_range.to_point(original_snapshot)` 为什么必须转成 ② 的 Point，而不是直接用调用方 buffer 的坐标？
**答案**：因为 `compose` 要求第二串编辑的 old 空间等于第一串（\( E^{-1} \)）的 new 空间，即 ②。hunk 锚点本来就挂在 ② 上（u2-l1 讲过的跨编辑稳定身份），`to_point(original_snapshot)` 是把锚点解析回它自己快照的坐标。

### 4.4 patch_for_base_text_range：对称的反方向

#### 4.4.1 概念说明

这个函数把 base 坐标映射回当前 buffer 坐标（\( H^{\leftrightarrow} \circ E \)），与 4.3 结构对称但有三个实质差异：

1. hunk 编辑的 old/new 交换（old = base 区间、new = ② 的 buffer 区间），因此游标定位改用 [`hunk_before_base_text_offset`](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L487-L503)（按 base 字节偏移导航 SumTree，u2-l2 讲过的第二种 SeekTarget）。
2. \( E \) 不取反，compose 在最后一步才应用。
3. 由于 \( E \) 作用在 ②→③ 方向，「范围起点附近的 hunk 是否受增量编辑影响」的判断更微妙，实现里出现了**游标重定位**的分支。

#### 4.4.2 核心流程

```text
0. 若 base 不存在：返回 old=(0,0)..(0,0) → new=(0,0)..max_point（与 4.3 方向相反）
1. E = edits_since（原样，不 invert）
2. 按 range.start 的 base 偏移定位游标（hunk_before_base_text_offset）
3. 校正：若定位到的 hunk 在 ② 里的起点晚于 E 首条编辑的 old.start，
   则 reset 游标、改按 buffer 锚点重新定位（hunk_before_buffer_anchor）
4. prefix_edit：old = (0,0)..prev.diff_base.end(①)，new = (0,0)..prev.buffer_range.end(②)
5. while 循环收集 hunk 编辑，终止条件双出口：
   a. hunk 的 base 起点 > range_end；且
   b. hunk 在 ② 的起点 > E 末条编辑的 old.end
6. 返回 Patch::new(hunk_patch).compose(E)
```

#### 4.4.3 源码精读

[游标重定位校正](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L662-L680)：先按 base 偏移找到「前一个 hunk」，再检查它在 buffer 侧的位置：

```rust
let hunk_before = self
    .hunk_before_base_text_offset(range.start().to_offset(self.base_text()), &mut cursor);

if let Some(hunk) = hunk_before
    && let Some(first_edit) = edits_since_diff.first()
    && hunk.buffer_range.start.to_point(self.original_buffer_snapshot())
        > first_edit.old.start
{
    cursor.reset();
    self.hunk_before_buffer_anchor(
        self.original_buffer_snapshot().anchor_before(first_edit.old.start),
        &mut cursor,
        self.original_buffer_snapshot(),
    );
}
```

这段与 4.3 的 `min` 校正同理：最终 compose 会用 \( E \) 移动 ②→③ 的坐标，如果增量编辑发生在查询起点之前很近的地方，受其影响的 hunk 必须纳入 `hunk_patch`，否则合成结果在范围起点附近的平移不正确。于是干脆把游标退回到「首条编辑之前」重新定位。

[while 循环的双出口条件](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L699-L713)：

```rust
while let Some(hunk) = cursor.item()
    && (hunk.diff_base_byte_range.start <= range_end
        || edits_since_diff.last().is_some_and(|last_edit| {
            hunk.buffer_range.start.to_point(self.original_buffer_snapshot())
                <= last_edit.old.end
        }))
{
    hunk_patch.push(Edit {
        old: hunk.diff_base_byte_range.to_point(self.base_text()),
        new: hunk.buffer_range.to_point(self.original_buffer_snapshot()),
    });
    cursor.next();
}
```

第一个条件是正常的范围边界（base 侧）；第二个条件对应 4.3 的 `max` 校正——即使 hunk 已在 base 侧范围之外，只要它的 buffer 侧起点仍落在末条增量编辑覆盖区域内，就继续收集。注意这里 hunk 编辑的 old 是 base 区间、new 是 ② 区间，与 4.3 恰好互为反向。

#### 4.4.4 代码实践

**实践目标**：验证 `patch_for_base_text_range` 的输出与 4.3 的实践互为 old/new 反向。

**操作步骤**：在 4.3.4 的测试里追加：

```rust
let base_patch = diff.patch_for_base_text_range(
    Point::new(1, 1)..=Point::new(1, 1),
    &snapshot,
);
for edit in base_patch.edits() {
    println!("{:?} -> {:?}", edit.old, edit.new);
}
```

**需要观察的现象**：输出一条编辑：

```text
(1, 0)..(2, 0) -> (2, 0)..(3, 0)
```

**预期结果**：old 是 base 坐标（`two` 行），new 是当前 buffer 坐标（`TWO` 行，因开头插入 `zero\n` 而后移一行）。它与 4.3.4 输出的第二条 `(2,0)..(3,0) -> (1,0)..(2,0)` 正好互为反向——同一个 hunk，两个方向各构造一次。

**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么这个函数的 prefix_edit 是 `old = (0,0)..prev.diff_base.end`、`new = (0,0)..prev.buffer_range.end`，与 4.3 的正好交换？
**答案**：因为方向反了。这里 old 空间是 base、new 空间是 ②；prefix_edit 的职责（携带「文件开头到上一个 hunk」的净位移）不变，只是两侧坐标系随方向交换。

**练习 2**：base 不存在时两个函数的短路分支有何不同？
**答案**：`patch_for_buffer_range` 返回 `old = 全 buffer → new = 空`（buffer 的所有内容在 base 里都没有对应）；`patch_for_base_text_range` 返回 `old = 空 → new = 全 buffer`（base 的空无对应到 buffer 的全部内容）。同一事实的两个方向。

**练习 3**：本函数为什么要先用 base 偏移定位、必要时再按 buffer 锚点重定位，而不是一开始就只用一种？
**答案**：查询范围天然定义在 base 空间，所以先用 base 偏移（`hunk_before_base_text_offset`）定位最直接；但 \( E \) 的校正关心的是 buffer 侧的位置（hunk 是否受增量编辑影响），这个信息只有按 buffer 锚点（`hunk_before_buffer_anchor`）才能判断，于是出现「先按 A 定位、条件不满足再按 B 重定位」的两段式。SumTree 对两种 SeekTarget 都支持（u2-l2），这正是双游标能力的价值。

### 4.5 四个 point 映射函数与 naive 对照实现

#### 4.5.1 概念说明

对调用方来说，最常用的不是整个 patch，而是「一个点映射到哪」。crate 在两个核心函数之上封装了四个点级 API，两两成对：

| 函数 | 方向 | 返回 |
| --- | --- | --- |
| `buffer_point_to_base_text_range` | buffer → base | `Range<Point>`（可能非空） |
| `base_text_point_to_buffer_range` | base → buffer | `Range<Point>`（可能非空） |
| `buffer_point_to_base_text_point` | buffer → base | 单个 `Point` |
| `base_text_point_to_buffer_point` | base → buffer | 单个 `Point` |

range 版与 point 版的区别在于处理「点落在编辑区间内部」的方式：range 版返回整个新区间（这块差异对应 base 里的哪一段），point 版按端点规则二选一。此外还有两个 `#[cfg(test)]` 的 naive 实现，它们不做任何范围优化、遍历全部 hunk，专门作为正确性对照（oracle）。

#### 4.5.2 核心流程

point 版的端点规则（以 buffer→base 为例）：

```text
patch = patch_for_buffer_range(point..=point, buffer)   # 退化成单点范围
edit  = patch.edit_for_old_position(point)
若 point == edit.old.end → 返回 edit.new.end   # 恰好压在编辑终点：取新区间终点
否则                      → 返回 edit.new.start  # 编辑区间内部：取新区间起点
```

naive 对照的思路：

```text
patch_for_buffer_range_naive(buffer):
    E⁻¹ = buffer.edits_since(②).invert()
    H   = 遍历 hunks 树的全部条目逐条转成编辑（不做范围裁剪、无 prefix_edit）
    返回 E⁻¹.compose(H)     # 与优化版公式完全相同，只是 H 是全量的
```

随机测试则把两者放在同一随机场景下逐点对比 `edit_for_old_position` 的结果。

#### 4.5.3 源码精读

[四个点级 API 集中在 L751-L797](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L751-L797)。range 版直接取 `edit.new`（[buffer_point_to_base_text_range](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L751-L759)）：

```rust
pub fn buffer_point_to_base_text_range(
    &self,
    point: Point,
    buffer: &text::BufferSnapshot,
) -> Range<Point> {
    let patch = self.patch_for_buffer_range(point..=point, buffer);
    let edit = patch.edit_for_old_position(point);
    edit.new
}
```

point 版多一层端点判断（[buffer_point_to_base_text_point](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L771-L783)）：

```rust
pub fn buffer_point_to_base_text_point(
    &self,
    point: Point,
    buffer: &text::BufferSnapshot,
) -> Point {
    let patch = self.patch_for_buffer_range(point..=point, buffer);
    let edit = patch.edit_for_old_position(point);
    if point == edit.old.end {
        edit.new.end
    } else {
        edit.new.start
    }
}
```

（[base_text_point_to_buffer_range](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L761-L769) 与 [base_text_point_to_buffer_point](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L785-L797) 是对称的另一对，结构完全相同。）

[naive 对照实现](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L600-L638) 没有游标、没有 prefix_edit、没有 min/max 校正——`self.hunks.iter()` 全量遍历，另外补上「base 不存在且无 hunk」的整文件编辑：

```rust
inverted_edits_since.compose(
    self.hunks
        .iter()
        .map(|hunk| { /* old: ② 的区间, new: base 的区间 */ ... })
        .chain(if !self.base_text_exists && self.hunks.is_empty() {
            Some(Edit { old: Point::zero()..original_snapshot.max_point(),
                        new: Point::zero()..Point::zero() })
        } else { None }),
)
```

[随机对照测试 test_patch_for_range_random](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L4121-L4122) 用 `#[gpui::test(iterations = 100)]` 让框架注入 100 个不同种子的 `StdRng`，随机生成 base 文本与初始 buffer，再随机做 1~5 次编辑，最后 [逐点比对两个实现](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L4286-L4309)：

```rust
let optimized_patch = diff.patch_for_buffer_range(range.clone(), &buffer_snapshot);
let naive_patch = diff.patch_for_buffer_range_naive(&buffer_snapshot);

for point in points {
    let optimized_edit = optimized_patch.edit_for_old_position(point);
    let naive_edit = naive_patch.edit_for_old_position(point);
    assert_eq!(optimized_edit, naive_edit, "patch_for_buffer_range mismatch at point {:?} ...", ...);
}
```

base 侧还有对称的一段（[L4311-L4333](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L4311-L4333)）。

**下游真实用法**（帮助理解这些 API 为什么存在）：

- [editor 的 get_permalink_to_line](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/editor/src/git.rs#L2912-L2918)：生成行级永久链接时，用 `buffer_point_to_base_text_point` 把用户在 buffer 里的选区端点换成 HEAD 版本的行号——链接必须指向提交后的行号，而不是工作区里改过的行号。
- [editor 的 diff 分屏](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/editor/src/split.rs#L47-L71)：左栏显示 base、右栏显示 buffer，两侧选区互相翻译时分别用 `patch_for_base_text_range` 与 `patch_for_buffer_range`（[L73-L86](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/editor/src/split.rs#L73-L86) 的 `buffer_range_to_base_text_range`、[L138-L141](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/editor/src/split.rs#L138-L141) 的 `base_text_point_to_buffer_point`）。
- [project 的 git_store](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/project/src/git_store.rs#L1343)：staging 时用 [`base_text_range_for_buffer_range`](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L375-L388)（内部就是调 `patch_for_buffer_range`，见 [L381-L383](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L381-L383)）求一个 buffer 范围在索引文本里的足迹——u4-l4 将深入这条链路。

#### 4.5.4 代码实践（本讲主实践）

**实践目标**：写一个测试，先用手工选定的点验证四个映射函数的期望值，再随机选点与 naive 实现逐一比对——完整复刻本讲的内容。

**操作步骤**：把下面的测试加入 `mod tests`（所需导入 `Buffer`、`BufferId`、`ReplicaId`、`StdRng`、`Rng` 均已在 [L2426-L2434](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2426-L2434) 就位），运行 `cargo test -p buffer_diff test_point_mapping_practice`：

```rust
// 示例代码：建议放入 crates/buffer_diff/src/buffer_diff.rs 的 mod tests 中学习使用
#[gpui::test(iterations = 10)]
async fn test_point_mapping_practice(cx: &mut gpui::TestAppContext, mut rng: StdRng) {
    let mut buffer = Buffer::new(
        ReplicaId::LOCAL,
        BufferId::new(1).unwrap(),
        "one\nTWO\nthree\nfour\n",
    );
    let diff = BufferDiffSnapshot::new_sync(&buffer, "one\ntwo\nthree\nfour\n".to_string(), cx);

    // ── 第一部分：未再编辑。唯一 hunk：buffer (1,0)..(2,0) ↔ base (1,0)..(2,0) ──
    // hunk 之前的点：恒等映射
    assert_eq!(
        diff.buffer_point_to_base_text_point(Point::new(0, 2), &buffer),
        Point::new(0, 2)
    );
    // hunk 内部的点：折叠到 hunk 的 base 起点
    assert_eq!(
        diff.buffer_point_to_base_text_point(Point::new(1, 1), &buffer),
        Point::new(1, 0)
    );
    // 恰好等于 hunk 的 old.end：取 new.end
    assert_eq!(
        diff.buffer_point_to_base_text_point(Point::new(2, 0), &buffer),
        Point::new(2, 0)
    );
    // 反方向：base 的 two 行内部 → buffer 的 TWO 行首
    assert_eq!(
        diff.base_text_point_to_buffer_point(Point::new(1, 1), &buffer),
        Point::new(1, 0)
    );
    // 反方向：hunk 之后（three 行）：平移，因两侧行数相同而恒等
    assert_eq!(
        diff.base_text_point_to_buffer_point(Point::new(3, 0), &buffer),
        Point::new(3, 0)
    );

    // ── 第二部分：diff 之后再编辑，验证 E⁻¹ 的参与 ──
    buffer.edit([(0..0, "zero\n")]); // buffer 变为 "zero\none\nTWO\nthree\nfour\n"
    // 新插入的 zero 行：base 里不存在，折叠回 (0,0)
    assert_eq!(
        diff.buffer_point_to_base_text_point(Point::new(0, 2), &buffer),
        Point::new(0, 0)
    );
    // 当前行 2 是 TWO：仍是 hunk 内部 → base (1,0)
    assert_eq!(
        diff.buffer_point_to_base_text_point(Point::new(2, 1), &buffer),
        Point::new(1, 0)
    );
    // hunk 之后（当前行 3 three）：等于复合编辑的 old.end → new.end = base (2,0)
    assert_eq!(
        diff.buffer_point_to_base_text_point(Point::new(3, 0), &buffer),
        Point::new(2, 0)
    );
    // 反方向：base 的 two 行 → 当前 buffer 的第 2 行首
    assert_eq!(
        diff.base_text_point_to_buffer_point(Point::new(1, 1), &buffer),
        Point::new(2, 0)
    );

    // ── 第三部分：随机点位，优化实现 vs naive 实现逐点对照 ──
    let buffer_snapshot = buffer.snapshot();
    let naive_patch = diff.patch_for_buffer_range_naive(&buffer_snapshot);
    let lines: Vec<&str> = buffer_snapshot.text().lines().collect();
    for _ in 0..10 {
        let row = rng.random_range(0..lines.len() as u32);
        let line = lines[row as usize];
        let col = if line.is_empty() {
            0
        } else {
            rng.random_range(0..=line.len() as u32)
        };
        let point = Point::new(row, col);

        let optimized_patch =
            diff.patch_for_buffer_range(point..=point, &buffer_snapshot);
        assert_eq!(
            optimized_patch.edit_for_old_position(point),
            naive_patch.edit_for_old_position(point),
            "optimized 与 naive 在 {point:?} 处不一致"
        );
    }
}
```

**需要观察的现象**：

1. 第一、二部分全部通过——每个期望值都能用 2.2 节的三条规则在纸上推出来（建议真的拿纸推一遍 `(0,2) → (0,0)` 这个例子：`zero\n` 是 diff 之后插入的，`E⁻¹` 把它折叠回 `(0,0)`，之后不再有任何 hunk 沾上它）。
2. 第三部分在 10 次框架迭代 × 10 个随机点上无失败。

**预期结果**：测试通过。想看失败输出，可把第二部分 `(0,2)` 的期望改成 `Point::new(0, 2)`——`pretty_assertions` 会打印红绿对照。

**待本地验证**：期望值均由源码语义手工推演，请以本地 `cargo test -p buffer_diff test_point_mapping_practice` 的结果为准。

#### 4.5.5 小练习与答案

**练习 1**：`buffer_point_to_base_text_range` 与 `buffer_point_to_base_text_point` 在点落在 hunk 内部时返回什么？
**答案**：range 版返回该 hunk 的**整个 base 区间**（如 `(1,0)..(2,0)`，一个可能非空的范围）；point 版按端点规则返回单个点——内部取 `new.start`，恰好压在 `old.end` 上取 `new.end`。选哪个取决于调用方要「这块差异」还是「一个位置」。

**练习 2**：naive 实现里为什么要 `chain` 一条「base 不存在且 hunks 为空」的整文件编辑，而优化版不需要？
**答案**：优化版在函数开头就有 `!base_text_exists` 的短路分支直接返回这条编辑；naive 版没有这个分支，若此时 hunks 树为空（比如从未计算过 diff），compose 出的 patch 就是空的、任何点都恒等映射，与优化版不一致。这条 `chain` 补齐了 oracle 的完备性——对照实现必须覆盖所有特例才配当 oracle。

**练习 3**：随机测试为什么用 `assert_eq!(optimized_edit, naive_edit)` 比较整条编辑，而不是只比较映射后的点？
**答案**：比较整条 `Edit`（old 与 new 两个区间）比单点严格得多——同一点沾上的编辑区间若不同，说明两个 patch 在该点附近的结构就有分歧，即使碰巧映射点相同也暴露出来了。oracle 对照要抓的是结构差异，不是恰好一致的输出。

## 5. 综合实践

**任务：写一个「迷你 permalink 生成器」**，模拟 [editor/src/git.rs 的 get_permalink_to_line](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/editor/src/git.rs#L2889-L2918) 的核心一步，把本讲所有内容串起来：

1. **构造场景**：base 为 6 行文本，buffer 改动其中两行（一个 hunk），用 `new_sync` 建 diff；然后**在文件末尾再追加一行**（制造 \( E \neq \varnothing \)）。
2. **正向映射**：取当前 buffer 里被改过那行的行首与行尾两个点，分别用 `buffer_point_to_base_text_point` 映到 base，打印 `(buffer 行, base 行)` 对——这就是 permalink 需要的「工作区行号 → HEAD 行号」。
3. **反向验证**：把第 2 步得到的 base 点用 `base_text_point_to_buffer_point` 映回来，确认落在原 hunk 的 buffer 区间内（往返不一定回到同一个点——hunk 内部的点会折叠到区间端点，这正是观察重点）。
4. **oracle 兜底**：随机取 20 个当前 buffer 的点，逐点断言 `patch_for_buffer_range` 与 `patch_for_buffer_range_naive` 的 `edit_for_old_position` 相等（复用 4.5.4 第三部分的写法）。
5. **思考题**（写进测试注释）：如果用户选中的行恰好是被删除的行（base 有、buffer 无），第 2 步的映射会给出什么？用 2.2 节规则预答：删除行的点在 buffer 里根本不存在，反向映射 `base_text_point_to_buffer_point` 会把 base 的该行折叠到删除点之后的位置——可以再写一个删除场景验证你的预答。

**完成标志**：测试通过；你能不查资料说出 `(buffer 点) → (base 点)` 途中经过的每一次折叠与平移。

## 6. 本讲小结

- 坐标映射的根源是**三份文本**：base ①、diff 计算时的 original buffer ②、当前 buffer ③；hunk 只描述 ①↔②，而调用方坐标在 ③。
- 两个方向的公式：\( P_{\text{cur}\to\text{base}} = E^{-1} \circ H \)，\( P_{\text{base}\to\text{cur}} = H^{\leftrightarrow} \circ E \)；`edits_since` 是否取反、compose 的先后顺序是两函数的全部差异。
- `Patch` 的点映射三规则：编辑前恒等、编辑内折叠到新区间、编辑后按累计长度差平移；`edit_for_old_position` 用闭区间「触边即命中」。
- `patch_for_buffer_range` 的性能来自**只访问范围内外的最少 hunk**：SumTree 游标定位 + `prefix_edit` 把之前的累计位移折叠成一条编辑 + 对查询端点做「宁多勿漏」的 min/max 校正。
- `patch_for_base_text_range` 结构对称，但因为 \( E \) 方向是顺的，校正体现为「按 base 偏移定位后、必要时按 buffer 锚标重定位」与 while 循环的双出口条件。
- naive 实现遍历全部 hunk、无任何优化，只存在于 `#[cfg(test)]`，作为随机对照测试的 oracle——这是 crate 保证复杂优化正确性的方法论。

## 7. 下一步学习建议

本讲结束了单元二「数据结构与 hunk 查询」。到目前为止我们一直把 `hunks` 树当作已知的数据，下一讲进入单元三的第一讲（u3-l1「compute_hunks：imara-diff 的使用方式」），看看这棵树里的 hunk 最初是怎么算出来的：`InternedInput` 如何按行装载文本、`Algorithm::Histogram` 是什么、以及 `postprocess_lines` 的启发式为什么必须存在。

如果想先巩固本讲，建议带着一个问题重读 [`test_patch_for_range_random`](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L4121-L4134) 的生成器（`gen_line` 故意以 20% 概率生成空行、`gen_edits_from` 保证每次改动至少一行）：这些「刁钻」文本形状是在攻击坐标映射的哪些边界？另外可以提前翻一眼 [base_text_range_for_buffer_range](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L368-L388) 的文档注释——它提到的「index-write path」正是 u4-l4 舞台的入口。
