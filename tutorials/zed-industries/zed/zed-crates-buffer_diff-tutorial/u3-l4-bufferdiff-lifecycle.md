# u3-l4 BufferDiff 实体生命周期与异步更新

## 1. 本讲目标

前面三讲我们一直在看「一次 diff 是怎么算出来的」：`compute_hunks` 调用 imara-diff（u3-l1）、`HunkSink` 把行号换算成锚点与字节区间（u3-l2）、词级差异如何附加到 hunk 上（u3-l3）。但这些都是**无状态的纯计算**——发生在后台线程、算完就结束。

本讲回到实体层面，回答一个问题：**一个 `BufferDiff` 实体从出生到死亡，状态是怎么流动的？** 具体学完后你应该能：

1. 说出 `BufferDiff` 实体的五个字段各自的作用，以及 `new`、`new_unchanged`、`new_with_base_text_buffer` 三种公开构造函数分别适用于什么场景。
2. 解释 `update_diff` 为什么「只算不写」，以及它的 `unchanged_hunks` 快路径如何用三个版本比较跳过整个 diff 计算。
3. 梳理 `set_base_text` 四步异步流程中两次版本检查分别在防什么并发问题、何时丢弃结果。
4. 区分 `BaseTextChanged` 与 `DiffChanged` 两个事件的触发时机。
5. 独立写出「创建 → set_base_text → 编辑 → 重算 → 校验」的完整测试。

## 2. 前置知识

本讲大量使用 gpui 的实体与异步机制，先把这些概念用通俗语言过一遍：

- **实体（`Entity<T>`）与上下文（`Context<T>`）**：gpui 把可变状态放在实体里，所有读写都必须通过 `entity.update(cx, |this, cx| ...)` 在前台线程完成（回顾 u1-l2）。`BufferDiff` 就是一个实体。
- **任务（`Task<R>`）**：`cx.spawn(...)` 在前台启动异步任务，`cx.background_executor().spawn(...)` 把纯计算丢到后台线程池。`Task` 是惰性的：**谁持有它，谁就控制它的生命周期；把它 drop 掉等于取消任务**。这一点是理解 `set_base_text` 文档注释的钥匙。
- **版本号（version）**：text crate 的每个 buffer 快照都带版本号，记录「这份快照是在第几次编辑之后拍的」。比较两个版本号是否相等，就能知道「从我出发的这个人，中途有没有被别人抢先改过」。
- **快照模式**：`BufferDiffSnapshot` 是不可变、可廉价克隆的（内部是 `SumTree` 和 Arc），读路径拿快照、写路径整体替换——这是典型的 copy-on-write 状态机。
- **`block_on`**：前台执行器上同步等待一个 future 完成，仅测试代码使用（u1-l3 已在 `new_sync` 里见过）。

## 3. 本讲源码地图

本讲全部源码集中在一个文件里：

| 位置 | 作用 |
| --- | --- |
| [src/buffer_diff.rs:22-29](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L22-L29) | `BufferDiff` 实体的五个字段 |
| [src/buffer_diff.rs:1560-1566](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1560-L1566) | `BufferDiffEvent`（两个事件）与 `EventEmitter` 实现 |
| [src/buffer_diff.rs:1569-1675](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1569-L1675) | 四个构造函数（含测试专用 `new_with_base_text`） |
| [src/buffer_diff.rs:1933-1990](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1933-L1990) | `update_diff`：后台计算 + 快路径 |
| [src/buffer_diff.rs:1992-2146](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1992-L2146) | `set_snapshot_with_secondary` 与 `set_snapshot`：提交状态、发射事件 |
| [src/buffer_diff.rs:2164-2181](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2164-L2181) | `snapshot()`：读路径与空兜底 |
| [src/buffer_diff.rs:2189-2259](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2189-L2259) | `set_base_text`：四步异步流程 |
| [src/buffer_diff.rs:2271-2283](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2271-L2283) | `recalculate_diff_sync`：测试专用的同步重算 |

测试参照（本讲实践的基础）：

| 位置 | 作用 |
| --- | --- |
| [src/buffer_diff.rs:275-284](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L275-L284) | `BufferDiffSnapshot::new_sync` 测试快捷通道（u1-l3 已讲） |
| [src/buffer_diff.rs:2442-2496](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2442-L2496) | `test_buffer_diff_simple`，其末尾演示了 `snapshot()` 空兜底 |
| [src/buffer_diff.rs:4336-4361](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L4336-L4361) | `test_set_base_text_with_crlf`，唯一的 `set_base_text` 测试范例 |

## 4. 核心概念与源码讲解

### 4.1 BufferDiff 实体的字段与构造函数家族

#### 4.1.1 概念说明

`BufferDiff` 是一个 gpui 实体，它的定位是「**一个 buffer 与一个基准文本之间的差异状态机**」。注意它自己**不算 diff**——计算交给后台任务；它负责持有状态、接收计算结果、对外发射事件。

先看它只有五个字段：

- `buffer_id`：它服务于哪个 buffer。
- `base_text_buffer`：基准文本被放进一个**只读的 `language::Buffer` 实体**里，而不是一根裸 `Rope`。为什么？因为基准文本也需要锚点系统（`diff_base_byte_range` 要能映射回 Point）、需要语言信息（词级 diff 要知道 scope）。用现成的 `language::Buffer` 一举两得。
- `diff_snapshot`：最近一次提交的快照，`None` 表示「还没算过」。
- `secondary_diff`：三方比较用的第二个 diff（u4 再展开）。
- `buffer_snapshot`：最近一次参与计算的 buffer 快照。
- `base_kind`：基准来自哪里（u1-l2 讲过的 `DiffBaseKind`）。

#### 4.1.2 核心流程

三个公开构造函数的分工：

```
new(buffer, language, registry, base_kind, cx)
    建一个空的只读基准 buffer，diff_snapshot = None
    → 适用于「实体先建好，base 文本稍后用 set_base_text 灌进来」（git 场景的常态）

new_with_base_text_buffer(buffer, base_text_buffer, base_kind, cx)
    直接复用调用方已经准备好的基准 buffer 实体
    → 适用于多个 diff 共享同一个基准文本、或基准来自外部（Custom）的场景

new_unchanged(buffer, language, registry, base_kind, cx)
    基准文本 = buffer 当前内容，立刻构造一个 hunk 为空的快照
    → 调用方已知「此刻零差异」时的快路径，完全跳过 diff 计算
```

#### 4.1.3 源码精读

实体字段定义，[src/buffer_diff.rs:22-29](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L22-L29)——这段代码定义了实体的全部可变状态：基准 buffer、可选快照、可选 secondary、buffer 快照与基准类型。

`new` 的实现，[src/buffer_diff.rs:1576-1593](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1576-L1593)——这段代码用 `cx.new` 建一个空字符串的本地 buffer，标记为 `Capability::ReadOnly`（基准文本不许被编辑），可选地挂上语言注册表与语言（异步设置），最后组装出 `diff_snapshot: None` 的实体。

`new_with_base_text_buffer`，[src/buffer_diff.rs:1596-1610](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1596-L1610)——这段代码直接接收外部的 `Entity<language::Buffer>` 作为基准 buffer，其余字段与 `new` 一致；注意参数 `_cx` 都没用上，它是纯装配。

`new_unchanged`，[src/buffer_diff.rs:1612-1649](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1612-L1649)——这段代码把 `buffer.text()` 整个当作基准文本建 buffer，然后**手工拼出一个 hunk 树为空的 `BufferDiffSnapshot`**（`SumTree::new` 两次），`base_text_exists: true`。它不调用任何 diff 算法——因为基准和 buffer 一模一样，差异必然为空。

测试专用的 `new_with_base_text`，[src/buffer_diff.rs:1651-1675](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1651-L1675)——这段代码先用 `new` 建实体，再把基准 buffer 换成归一化行尾（LF）后的文本，然后在前台 `block_on(this.update_diff(...))` 同步算完并 `set_snapshot` 提交。u1-l3 的 `new_sync` 就是它的包装。

#### 4.1.4 代码实践

**实践目标**：用肉眼观察到「还没算」和「算出来是空的」是两种不同的状态。

1. 在 `mod tests` 里新增一个测试（可放在 `test_buffer_diff_simple` 旁边），分别用 `new` 和 `new_unchanged` 构造实体并查询快照。
2. 操作步骤（示例代码，需放进 `#[cfg(test)] mod tests` 内）：

```rust
#[gpui::test]
async fn test_constructor_states(cx: &mut gpui::TestAppContext) {
    let buffer = Buffer::new(
        ReplicaId::LOCAL,
        BufferId::new(1).unwrap(),
        "one\ntwo\n".to_string(),
    );

    // 场景 A：new —— 尚未计算
    let a = cx.new(|cx| BufferDiff::new(&buffer, None, None, DiffBaseKind::Head, cx));
    let snapshot_a = a.update(cx, |diff, cx| diff.snapshot(cx));

    // 场景 B：new_unchanged —— 已计算，且零差异
    let b = cx.new(|cx| BufferDiff::new_unchanged(&buffer, None, None, DiffBaseKind::Head, cx));
    let snapshot_b = b.update(cx, |diff, cx| diff.snapshot(cx));

    println!("A: exists={}", snapshot_a.base_text_exists());
    println!("B: exists={}, empty={}", snapshot_b.base_text_exists(), snapshot_b.is_empty());
}
```

3. 需要观察的现象：`A: exists=false`（还没算，走空兜底），`B: exists=true, empty=true`（算了，结果是零差异）。
4. 预期结果如上；具体打印输出**待本地验证**（用 `cargo test -p buffer_diff test_constructor_states -- --nocapture` 运行）。注意场景 A 与 [test_buffer_diff_simple 的末尾](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2485-L2495) 是同一回事。

#### 4.1.5 小练习与答案

**练习 1**：为什么基准文本要存成 `Entity<language::Buffer>` 而不是 `Rope`？
**答案**：基准文本需要锚点系统（把 `diff_base_byte_range` 映射回行号）、需要语言信息（词级 diff 按 scope 切词）。`language::Buffer` 现成提供这些能力，还能被下游直接当 buffer 用（`base_text_buffer()` 访问器把实体借出去）。`Rope` 只是纯文本。

**练习 2**：`new_unchanged` 构造出的快照里 `base_text_exists` 是 true 还是 false？为什么它不需要调用 `compute_hunks`？
**答案**：true。因为基准文本就是 buffer 的当前内容，两者逐字节相等，diff 结果必然是空 hunk 集合，手工构造空 `SumTree` 即可，调用 diff 算法是浪费。

**练习 3**：`new_with_base_text` 为什么要先 `text::LineEnding::normalize(&mut base_text)`？
**答案**：git 返回的基准文本可能是 CRLF 行尾，而 buffer 内部统一为 LF。若不归一化，「同一行只差 `\r`」会被判成修改。归一化保证 diff 只反映真实内容差异（u1-l3、u5-l1 的 CRLF 测试都建立在这之上）。

### 4.2 update_diff：后台计算与 unchanged_hunks 快路径

#### 4.2.1 概念说明

`update_diff` 是实体的「计算入口」，但请记住它最重要的设计决定：**只算不写**。它返回 `Task<BufferDiffUpdate>`，把计算结果打包交还调用方，**自己绝不碰 `self.diff_snapshot`**。提交与否、何时提交、提交前要不要做版本检查，全部留给调用方（`set_base_text` 或 `recalculate_diff_sync`）。这种「计算与提交分离」正是并发安全的根基。

它还有一条快路径：如果 buffer 和基准文本自上次计算以来都没变，直接克隆旧的 hunk 树，**连后台线程都不用碰**。

#### 4.2.2 核心流程

```
update_diff(buffer, base_text_snapshot, base_text, cx) -> Task<BufferDiffUpdate>
  ① 归一化 base_text 行尾；debug_assert 两件事：
     a) base_text 与 base_text_snapshot.text() 内容一致
     b) base_text_snapshot 属于本实体的 base_text_buffer（remote_id 相同）
  ② 读取语言设置构造 diff 选项（词级 diff 开关，u3-l3）
  ③ 快路径判断 unchanged_hunks：
     旧快照存在 且 base_text_exists 相同 且 base 版本相同 且 buffer 版本相同
       → 直接复用旧 hunks 树
  ④ 否则 cx.background_executor().spawn：
     base_text 为 Some → compute_hunks(Some((文本, rope)), buffer, 选项)
     base_text 为 None → compute_hunks(None, buffer, 选项)   // 整文件 Added（u3-l1）
  ⑤ 打包 BufferDiffUpdate { hunks, base_text, base_text_exists, buffer_snapshot } 返回
```

快路径条件可以写成：

\[ \text{复用旧树} \iff e_{old} = e_{new} \;\wedge\; v^{base}_{old} = v^{base}_{new} \;\wedge\; v^{buf}_{old} = v^{buf}_{new} \]

其中 \( e \) 是 `base_text_exists`，\( v^{base} \)、\( v^{buf} \) 分别是基准与 buffer 的版本号。三个条件缺一不可：存在性翻转意味着「无基准 → 有基准」，必须重算；任一版本前进意味着文本变了。

#### 4.2.3 源码精读

函数签名与两个 debug 断言，[src/buffer_diff.rs:1933-1948](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1933-L1948)——这段代码先归一化行尾，再用两个断言保证「传入的文本快照对」是自洽的：文本与快照内容一致、快照确实来自本实体的基准 buffer。断言只在 debug 构建生效，是给调用方（主要是 `set_base_text`）上的一道保险。

快路径，[src/buffer_diff.rs:1959-1968](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1959-L1968)——这段代码检查旧快照的三个版本条件（存在标志、基准版本、buffer 版本全部相等），满足就把旧 `hunks` 树克隆出来，跳过 diff 计算。注意 `SumTree` 克隆是 O(1) 的结构共享，所以这条路径几乎免费。

后台计算，[src/buffer_diff.rs:1970-1989](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1970-L1989)——这段代码在后台线程池上执行：有快路径结果就用它；否则按 `base_text` 是否为 `Some` 分两路调用 `compute_hunks`（u3-l1 讲过的 imara-diff 入口），最后打包 `BufferDiffUpdate` 返回。注意闭包捕获的都是克隆好的快照，不借用实体，所以能安全地移到后台线程。

另外，`BufferDiffUpdate` 上还有一个给下游用的改写器 [src/buffer_diff.rs:75-84](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L75-L84)，这段代码允许调用方在提交前替换 update 里的基准快照——下游（如 project）拿到 Task 后可以按需调整再提交。

#### 4.2.4 代码实践

**实践目标**：亲眼确认快路径生效——内容不变时第二次计算不会走 `compute_hunks`。

1. 在本地分支给 [L1973-L1981](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1973-L1981) 的 `compute_hunks` 两个调用分支前临时各加一行 `eprintln!("computing hunks (base={})", base_text.is_some());`（**练习后记得还原，不要提交**）。
2. 写一个测试：用 `new_with_base_text` 建实体（第一次计算），立刻再调一次 `recalculate_diff_sync`，观察第二次没有打印；然后 `buffer.edit(...)` 后第三次调用，观察又打印了。
3. 需要观察的现象：第二次调用静默（快路径），编辑后的第三次调用有输出（版本变了，真算）。
4. 预期结果：打印次数 = 2（初建一次 + 编辑后一次）。具体输出**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `update_diff` 不自己调用 `set_snapshot` 提交结果？
**答案**：把「算」与「提交」分开，调用方才能在中间插入版本检查（`set_base_text` 的第④步）、替换基准快照（`BufferDiffUpdate::set_base_text_snapshot`）或干脆丢弃过期的计算结果。若它自己提交，这些并发保护就没地方放了。

**练习 2**：快路径为什么比较的是「版本号相等」而不是「文本内容相等」？
**答案**：版本号是 O(1) 的整数比较，文本相等需要 O(n) 遍历。版本号相等是文本相等的充分条件（每次编辑都推进版本），在 diff 这种高频调用的路径上必须用便宜的判据。

**练习 3**：如果只编辑了 buffer（基准没动），快路径会命中吗？
**答案**：不会。\( v^{buf}_{old} \neq v^{buf}_{new} \)，三个条件里有一个不满足，于是走 `compute_hunks` 重算——这正是每次编辑后 diff 需要更新的场景。

### 4.3 set_snapshot / set_snapshot_with_secondary：提交状态并发射事件

#### 4.3.1 概念说明

`set_snapshot` 是唯一的「写入口」：把一份算好的 `BufferDiffUpdate` 提交为实体的新状态，并发射事件。`set_snapshot` 本身只是一行转发，真正的主人是 `set_snapshot_with_secondary`——它额外处理两件事：secondary diff 带来的范围合并（u4 展开），以及可选的「清空 pending hunks」。

提交时会做三件事：

1. 用新 hunk 树 + 新基准构造新快照，**pending_hunks 从旧快照继承**（乐观 UI 状态不因重算而丢）。
2. 计算一个 `DiffChanged`，告诉订阅者「哪些范围变了」。
3. 判定 `base_text_changed`，决定是否额外发射 `BaseTextChanged`。

#### 4.3.2 核心流程

```
set_snapshot(update) = set_snapshot_with_secondary(update, None, false)

set_snapshot_with_secondary(update, secondary_diff_change, clear_pending_hunks, cx)
  ① old = 现有快照（没有则用「base 不存在」的空快照顶替）
     new = 新 hunk 树 + 新基准 + 继承 old 的 pending_hunks
  ② base_text_changed = 存在标志翻转 || 基准 buffer 换了(remote_id) || 新版本 changed_since 旧版本
  ③ 三分支算 DiffChanged 的三个范围：
     (不存在, 不存在) 且已有快照 → DiffChanged::default()（什么都不变）
     (存在, 存在)               → compare_hunks(...)（u3-l5 专门讲）
     其他（存在性翻转）          → 全范围 min_max
  ④ 若 base_text_changed || clear_pending_hunks：
     把 pending hunk 的范围并入三个范围，然后清空 pending 树
  ⑤ 若给了 secondary_diff_change：用 range_to_hunk_range 把它也并入三个范围
  ⑥ 提交：self.diff_snapshot = Some(new); self.buffer_snapshot = new_buffer
  ⑦ 发射：先 BaseTextChanged（仅当 base_text_changed），再 DiffChanged（总是）
     返回 changed_range 供调用方继续传播
```

两个事件的触发时机由此定死：

- `DiffChanged`：**每次提交都发**，哪怕内容没变（范围可能是 `None`）。
- `BaseTextChanged`：**只有基准文本变了才发**——存在性翻转、换 buffer、或版本前进。

#### 4.3.3 源码精读

新快照的组装，[src/buffer_diff.rs:2008-2026](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2008-L2026)——这段代码克隆旧快照（或构造空顶替），然后拼出新快照；注意 `pending_hunks: old_snapshot.pending_hunks.clone()`，乐观状态被显式带过一次重算。

`base_text_changed` 的判定，[src/buffer_diff.rs:2031-2036](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2031-L2036)——这段代码用三个条件的或判定基准是否真的变了：存在标志不同、基准 buffer 不是同一个（remote_id 不同）、或新版本相对旧版本有编辑（`changed_since`）。

三分支，[src/buffer_diff.rs:2042-2062](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2042-L2062)——这段代码按新旧存在性分派：两边都不存在且已有快照则报告「无变化」；两边都存在则交给 `compare_hunks` 做新旧 hunk 树的归并比较（下一讲主角）；存在性翻转（首次灌入基准、或基准被删除）则直接报告全范围变更。

pending 合并与清空，[src/buffer_diff.rs:2064-2095](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2064-L2095)——这段代码在基准变化（或显式要求清空）时，把旧 pending hunk 覆盖的范围并入三个变更范围（保证乐观标记的区域能被重绘），再把 pending 树替换为空树。

提交与事件发射，[src/buffer_diff.rs:2123-2137](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2123-L2137)——这段代码是提交的原子动作：一步替换 `diff_snapshot` 与 `buffer_snapshot`，然后按「先 `BaseTextChanged`（若有）、后 `DiffChanged`（总是）」的顺序发射，最后把 `changed_range` 返回给调用方。

薄封装 `set_snapshot`，[src/buffer_diff.rs:2140-2146](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2140-L2146)——这段代码把 secondary 变更与清空参数填默认值后转发，是绝大多数调用方（包括 `set_base_text`）用的入口。

#### 4.3.4 代码实践

**实践目标**：订阅事件，验证「编辑 buffer 后重算」只发 `DiffChanged`、不发 `BaseTextChanged`。

1. 操作步骤（示例代码，放在 `mod tests` 内；`std::sync::mpsc` 该模块已导入）：

```rust
#[gpui::test]
async fn test_diff_events_on_recalculate(cx: &mut gpui::TestAppContext) {
    let (events_tx, events_rx) = mpsc::channel();
    let base_text = "one\ntwo\nthree\n".to_string();
    let mut buffer = Buffer::new(
        ReplicaId::LOCAL,
        BufferId::new(1).unwrap(),
        "one\nTWO\nthree\n".to_string(),
    );
    let diff = cx.new(|cx| BufferDiff::new_with_base_text(&base_text, &buffer, cx));

    cx.update(|cx| {
        cx.subscribe(&diff, move |_, event: &BufferDiffEvent, _| {
            events_tx.send(event.clone()).unwrap();
        })
        .detach();
    });

    buffer.edit([(0..0, "zero\n")]);
    diff.update(cx, |diff, cx| diff.recalculate_diff_sync(&buffer, cx));
    cx.run_until_parked();

    let mut seen = Vec::new();
    while let Ok(event) = events_rx.try_recv() {
        seen.push(event);
    }
    assert_eq!(seen.len(), 1);
    assert!(matches!(seen[0], BufferDiffEvent::DiffChanged(_)));
}
```

2. 需要观察的现象：只收到一个事件，且是 `DiffChanged`；基准没变所以没有 `BaseTextChanged`。
3. 预期结果：断言通过。`cx.subscribe` 是 `App` 上的方法（[gpui/src/app.rs:1158-1171](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/gpui/src/app.rs#L1158-L1171)），闭包签名是 `(实体, &事件, &mut App)`。运行结果**待本地验证**（`cargo test -p buffer_diff test_diff_events_on_recalculate`）。
4. 思考：构造阶段（`new_with_base_text` 内部那次 `set_snapshot`）也发过事件，但订阅发生在其后，所以收不到——这正好演示了事件是「发完即走」的，没有历史回放。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `DiffChanged` 每次提交都发，即使什么都没变？
**答案**：订阅者（编辑器 gutter、git 面板）需要的是「diff 状态可能变了，请重新查询」的通知语义，而不是精确的变更日志。范围字段（`changed_range` 等）为 `None` 时订阅者可以自行判断；漏发事件的代价（UI 显示过期数据）远大于多发一次。

**练习 2**：新快照为什么继承旧快照的 `pending_hunks`？什么情况下会被清空？
**答案**：pending hunks 是 stage/unstage 的乐观 UI 状态（u4-l3），buffer 的普通编辑重算 diff 不应把它抹掉。只有基准文本真的变了（`base_text_changed`）或调用方显式传 `clear_pending_hunks` 时才清空——因为基准一变，旧 pending 对应的基准区间已经没有意义。

**练习 3**：`BufferDiffEvent::BaseTextChanged` 与 `DiffChanged.base_text_changed` 字段是什么关系？
**答案**：同一个布尔值的两种消费方式。`set_snapshot_with_secondary` 内部算出 `base_text_changed` 后，既把它放进 `DiffChanged` 事件（[L2130](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2130)），又在为真时额外发射独立的 `BaseTextChanged`（[L2132-L2134](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2132-L2134)），方便订阅方只关心基准变化的场景做粗粒度订阅。

### 4.4 snapshot()：读路径与「尚未计算」的空兜底

#### 4.4.1 概念说明

`snapshot()` 是读入口：把实体当前状态以 `BufferDiffSnapshot` 的形式交出去。它有两个特殊行为：

1. **空兜底**：如果 `diff_snapshot` 是 `None`（从没算过），不报错、不 panic，而是返回一个 `base_text_exists: false`、hunk 树为空的快照。这样下游查询代码永远拿到合法对象，不需要处理 `Option`。这呼应 u1-l2 的结论：「尚未计算」与「无基准」对外表现为同一种状态——零 hunk。
2. **动态挂载 secondary**：每次读取时，若实体挂着 secondary diff，就地克隆其快照并包成 `Arc` 附上。secondary 快照不参与持久状态，属于「读取时组装」的派生数据。

#### 4.4.2 核心流程

```
snapshot(cx) -> BufferDiffSnapshot
  ① diff_snapshot 存在 → 克隆它
     不存在 → 现场造一个空兜底：
       hunks/pending 为空树，base_text 取基准 buffer 当前快照，
       base_text_exists = false，buffer_snapshot 取实体记录的那份
  ② secondary_diff 存在 → 递归取其 snapshot（断言它没有自己的 secondary，防止嵌套）
     包成 Arc 挂到快照上
  ③ 返回
```

#### 4.4.3 源码精读

兜底与克隆，[src/buffer_diff.rs:2164-2175](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2164-L2175)——这段代码先尝试克隆已有快照，没有就用空树和 `base_text_exists: false` 现场构造一份，保证调用方永远能拿到合法快照。

secondary 挂载，[src/buffer_diff.rs:2176-2181](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2176-L2181)——这段代码在返回前把 secondary diff 的快照包装成 `Arc` 附上；`debug_assert` 禁止 secondary 再挂 secondary（三方比较只允许一层，防止无限递归与语义混乱）。

配套的访问器也值得一看：[src/buffer_diff.rs:2148-2156](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2148-L2156) 这段代码提供 `base_text()`（直接读基准 buffer 的新快照，绕过 diff 快照）与 `base_text_exists()`（委托给快照，未计算时恒为 false）；[src/buffer_diff.rs:2285-2287](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2285-L2287) 这段代码把基准 buffer 实体本身借给下游。

#### 4.4.4 代码实践

**实践目标**：验证「尚未计算」状态下游查询不炸。

1. 操作步骤：复用 4.1.4 的场景 A（`new` 构造、不 set_base_text），在拿到空兜底快照后继续调用查询 API：

```rust
// 接 4.1.4 的 snapshot_a
let hunks: Vec<_> = snapshot_a
    .hunks_intersecting_range(
        Anchor::min_max_range_for_buffer(buffer.remote_id()),
        &buffer,
    )
    .collect();
assert!(hunks.is_empty());
assert_eq!(snapshot_a.base_text_string(), None);
```

2. 需要观察的现象：查询正常返回空集合，`base_text_string()` 为 `None`，全程无 panic。
3. 预期结果：断言通过；仓库里 [test_buffer_diff_simple 的这段](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2485-L2495) 做的就是同一件事。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`snapshot()` 为什么不直接返回 `Option<BufferDiffSnapshot>`？
**答案**：下游查询（gutter、git 面板）每帧都可能取快照，如果每个调用点都要 `unwrap`/分支处理 `None`，代码会爆炸且容易出错。空兜底把「未计算」规约为「零 hunk 的合法快照」，语义上也说得通：没算差异就当没有差异。

**练习 2**：`base_text()`（访问器）与 `snapshot().base_text`（快照字段）什么时候会不一样？
**答案**：`base_text()` 直接读 `base_text_buffer` 实体的**当前**快照，而 `snapshot().base_text` 是**上次 diff 计算时**的基准快照。在 `set_base_text` 的中间态（基准 buffer 已被 `fast_forward`、新 diff 还没提交，或反之）两者可能短暂不同；这就是为什么 `update_diff` 要用 debug 断言强制两者一致。

**练习 3**：为什么 secondary 快照在每次 `snapshot()` 时重新组装，而不是存在 `diff_snapshot` 里？
**答案**：secondary diff 是独立实体，自己会更新。若把它的快照固化进主快照，secondary 每次变化都得通知主实体重写快照。读取时动态组装（Arc 克隆，代价低）让两个实体的生命周期解耦。

### 4.5 set_base_text：四步异步流程与并发丢弃保护

#### 4.5.1 概念说明

`set_base_text` 是下游最常用的入口：git 拉到了新的基准文本（HEAD 或 index），灌进实体。它要同时完成「更新基准 buffer」和「重算 diff」两件事，而且是异步的——大文件的 diff 要在后台算，期间用户可能继续打字、git 可能又推来新基准。

它面临的并发问题：**如果两次 `set_base_text` 交叠执行，后启动的可能先完成，先启动的后完成，实体会被旧结果覆盖**。源码的解法是「版本号守卫」：在两个关键提交点检查基准 buffer 的版本，若已被别人动过就**丢弃自己算了一半的结果**。

先看文档注释规定的调用契约，[src/buffer_diff.rs:2183-2188](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2183-L2188)——这段注释说明：丢弃返回的 Task 即取消更新；调用不允许交叠，想重入就把 Task 存在会被下次调用覆盖的位置，让旧任务自动被取消。注意 `None` 表示「基准不存在」（如未跟踪文件），此时基准 buffer 会被清空、`base_text_exists` 记为 false，diff 走 u3-l1 的「整文件 Added」特例。

#### 4.5.2 核心流程

四步流程（每步一个 `this.update(...)` 包裹的前台事务，步间用 await 让出）：

```
set_base_text(base_text, buffer, cx) -> Task<()>
  前置：base_text_exists = base_text.is_some()；None → 空字符串
  ─────────────────────────────────────────────────────────────
  步骤① 请求基准 diff
     base_text_buffer.diff(new_text)  → Task<Diff>
     await 得到 base_text_diff（含 base_version 与把旧基准变成新基准的最小编辑集）
  步骤② 生成「未来基准快照」
     若 base_text_buffer 当前版本 != base_text_diff.base_version：
        warn「dropping concurrent diff update」+ debug_panic → 整个任务终止  ✗丢弃点1
     否则 set_line_ending + snapshot_with_edits(edits)
     await 得到 edited_base_text（一个尚未提交给实体的未来快照）
  步骤③ 后台算 diff
     update_diff(buffer, &未来快照, base_text_exists.then(|| 文本))
     await 得到 BufferDiffUpdate（期间实体的基准 buffer 仍是旧内容）
  步骤④ 原子提交
     若 base_text_buffer 当前版本 != edited_base_text.base_version：
        warn + debug_panic → 终止                                    ✗丢弃点2
     否则 base_text_buffer.fast_forward(edited_base_text)   // 基准 buffer 一步换新
          set_snapshot(state)                              // diff 快照一步换新
          → 发射 BaseTextChanged? / DiffChanged
```

对应的时序图（综合实践会让你亲手画一遍，这里是参照答案）：

```
调用方                BufferDiff 实体              base_text_buffer        后台线程
  │ set_base_text(text) │                             │                      │
  │────────────────────>│ cx.spawn                    │                      │
  │                     │① diff(new_text) ────────────>│                      │
  │                     │<──────────── Task ───────────│                      │
  │                     │        await base_text_diff  │                      │
  │                     │② 版本检查(≠base_version → 丢弃①')                   │
  │                     │   snapshot_with_edits ──────>│                      │
  │                     │<──────── Task ───────────────│                      │
  │                     │        await edited_base_text│                      │
  │                     │③ update_diff(未来快照) ──────│──spawn compute_hunks>│
  │                     │<────────────────────────────│────BufferDiffUpdate──│
  │                     │④ 版本检查(≠edited 版本 → 丢弃②')                    │
  │                     │   fast_forward + set_snapshot│                      │
  │                     │   emit BaseTextChanged? → DiffChanged               │
```

#### 4.5.3 源码精读

步骤①，[src/buffer_diff.rs:2195-2208](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2195-L2208)——这段代码 spawn 异步任务后，先在 `this.update` 里让基准 buffer 自己算出「从旧内容到新文本的编辑集」（`language::Buffer::diff`），拿到的是一个记录了 `base_version` 的 diff 任务，随后 await。

步骤②与第一个丢弃点，[src/buffer_diff.rs:2209-2229](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2209-L2229)——这段代码在应用编辑前检查基准 buffer 的版本是否仍等于 diff 开始时的 `base_version`；若不等说明有并发调用已经改写了基准，于是 `log::warn!("dropping concurrent diff update")` 并返回 `None` 终止整个任务。通过检查后用 `snapshot_with_edits` 生成「未来快照」——注意编辑此时**只存在于这份未来快照里**，实体可见的基准 buffer 还是旧的。

步骤③，[src/buffer_diff.rs:2231-2244](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2231-L2244)——这段代码用未来快照调 `update_diff`（4.2 讲过的「只算不写」），把真正的 diff 计算派发到后台线程并 await；`base_text_exists.then(|| ...)` 保证基准不存在时传 `None` 给 `compute_hunks`。

步骤④与第二个丢弃点，[src/buffer_diff.rs:2245-2257](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2245-L2257)——这段代码在提交前做第二次版本检查（对象换成未来快照的 `base_version()`）；通过后 `fast_forward` 一步把基准 buffer 推进到未来快照，紧接着 `set_snapshot` 提交 diff 结果——**基准与 diff 在同一个前台事务里换新，外部观察者看不到中间态**。

测试专用的同步通道 `recalculate_diff_sync`，[src/buffer_diff.rs:2271-2283](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2271-L2283)——这段代码跳过 `set_base_text` 的基准更新流程（基准不动），直接以现有基准 `update_diff` + 前台 `block_on` + `set_snapshot` 完成一次同步重算，是测试里模拟「buffer 编辑后 diff 刷新」的标准工具。

仓库里唯一驱动 `set_base_text` 的测试范例，[src/buffer_diff.rs:4350-4358](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L4350-L4358)——这段代码展示标准用法：`cx.new` 建实体 → `diff.update` 里调 `set_base_text` 拿到 Task 并 `.await` → `cx.run_until_parked()` 排空后台任务 → 再取快照查询。它用 CRLF 文本正是为了验证行尾归一化路径不崩。

#### 4.5.4 代码实践（本讲主实践）

**实践目标**：其一，亲手画一遍 `set_base_text` 时序图；其二，验证「编辑 buffer 后重算，hunk 更新而基准不变」。

**任务 A：画时序图**

1. 只读 [src/buffer_diff.rs:2189-2259](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2189-L2259)，在纸上（或 Mermaid）画出：调用方、`BufferDiff` 实体、`base_text_buffer`、后台线程四个泳道。
2. 必须标出的要素：四个步骤的边界（每个 `this.update` + `await`）；两个版本比较的位置与各自的**丢弃分支**（警告日志内容）；`fast_forward` 与 `set_snapshot` 的先后与同事务关系。
3. 画完与 4.5.2 的参照图对照，检查是否漏了「未来快照在步骤③期间不可见」这一细节。

**任务 B：recalculate 前后对比测试**

1. 操作步骤（示例代码，放在 `mod tests` 内）：

```rust
#[gpui::test]
async fn test_recalculate_after_edit(cx: &mut gpui::TestAppContext) {
    let base_text = "one\ntwo\nthree\n".to_string();
    let mut buffer = Buffer::new(
        ReplicaId::LOCAL,
        BufferId::new(1).unwrap(),
        "one\nTWO\nthree\n".to_string(),
    );

    let diff = cx.new(|cx| BufferDiff::new_with_base_text(&base_text, &buffer, cx));

    // 第一次：显式重算（内容未变，走 unchanged_hunks 快路径）
    diff.update(cx, |diff, cx| diff.recalculate_diff_sync(&buffer, cx));
    let first = diff.update(cx, |diff, cx| diff.snapshot(cx));
    assert_hunks(
        first.hunks_intersecting_range(
            Anchor::min_max_range_for_buffer(buffer.remote_id()),
            &buffer,
        ),
        &buffer,
        &base_text,
        &[(1..2, "two\n", "TWO\n", DiffHunkStatus::modified_none())],
    );

    // 编辑 buffer：文件头插入一行
    buffer.edit([(0..0, "zero\n")]);

    // 第二次：重算后 hunk 应更新
    diff.update(cx, |diff, cx| diff.recalculate_diff_sync(&buffer, cx));
    let second = diff.update(cx, |diff, cx| diff.snapshot(cx));
    assert_hunks(
        second.hunks_intersecting_range(
            Anchor::min_max_range_for_buffer(buffer.remote_id()),
            &buffer,
        ),
        &buffer,
        &base_text,
        &[
            (0..1, "", "zero\n", DiffHunkStatus::added_none()),
            (2..3, "two\n", "TWO\n", DiffHunkStatus::modified_none()),
        ],
    );

    // diff base 保持不变
    assert_eq!(second.base_text_string(), Some(base_text.clone()));
}
```

2. 需要观察的现象：第二次重算后 hunk 从 1 个变 2 个，且原 hunk 的行号整体下移一行（锚点系统在起作用）；`base_text_string()` 始终等于初始基准。
3. 预期结果：断言全部通过（第二个 `assert_hunks` 的期望值与 [test_buffer_diff_simple 同场景](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2470-L2483) 完全一致）。运行 `cargo test -p buffer_diff test_recalculate_after_edit`，**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：两个丢弃点检查的对象有什么不同？各自防的是什么？
**答案**：丢弃点1（步骤②）比较「基准 buffer 当前版本」与「步骤① diff 任务记录的 `base_version`」，防的是步骤①执行期间有并发调用先改了基准 buffer；丢弃点2（步骤④）比较「基准 buffer 当前版本」与「未来快照的版本」，防的是步骤②~③这段较长的后台计算期间基准被并发更新。两个检查合起来保证「提交的 diff 一定是针对仍然当前的基准算的」。

**练习 2**：为什么 `fast_forward` 和 `set_snapshot` 必须在同一个 `this.update` 事务里？
**答案**：gpui 的 update 是原子的，两个写操作之间不会有其他代码插进来。若分开提交，中间态会出现「基准 buffer 已是新文本、diff 快照还是旧基准算的」或反之，读取方（`snapshot()` 的 secondary 组装、下游查询）会看到自相矛盾的状态。

**练习 3**：`set_base_text(None, ...)` 之后，`snapshot()` 会返回什么样的结果？
**答案**：`base_text_exists` 变为 false（存在性翻转，会发射 `BaseTextChanged`），`compute_hunks(None, ...)` 产出整文件 Added 的 hunk（`is_created_file()` 为 true，见 u3-l1），基准 buffer 内容被清为空字符串。对外表现为「这个文件相对基准是全新的」。

**练习 4**：调用方「把 Task 存在会被覆盖的字段里」这个契约，为什么能防止调用交叠？
**答案**：第二次调用 `set_base_text` 时，对同一字段的赋值会 drop 掉上一次返回的 Task，而 drop Task 即取消任务——旧任务即使已经跑到某个 await 点，也不会再继续提交。这样即使调用方不小心重入，最多浪费一次计算，不会出现旧结果覆盖新结果。

## 5. 综合实践

设计一个贯穿本讲的任务：**「迷你 diff 会话」——用库的方式驱动一个 BufferDiff 走完整个生命周期，并用事件流验证每一步**。

场景剧本：文件已被 git 跟踪（有 HEAD 基准）→ 用户编辑 → diff 刷新 → 基准被删除（模拟文件从跟踪中移除）。

步骤（示例代码，整合了本讲全部知识点）：

```rust
#[gpui::test]
async fn test_mini_diff_session(cx: &mut gpui::TestAppContext) {
    let (events_tx, events_rx) = mpsc::channel();
    let base_text = "one\ntwo\nthree\n".to_string();
    let mut buffer = Buffer::new(
        ReplicaId::LOCAL,
        BufferId::new(1).unwrap(),
        "one\nTWO\nthree\n".to_string(),
    );

    // ① new 构造：尚未计算
    let diff = cx.new(|cx| BufferDiff::new(&buffer, None, None, DiffBaseKind::Head, cx));
    assert!(!diff.read(cx).base_text_exists());

    // ② 订阅事件
    cx.update(|cx| {
        cx.subscribe(&diff, move |_, event: &BufferDiffEvent, _| {
            events_tx.send(event.clone()).unwrap();
        })
        .detach();
    });

    // ③ 灌入基准文本（异步四步流程）
    diff.update(cx, |diff, cx| {
        diff.set_base_text(Some(Arc::from(base_text.as_str())), buffer.clone(), cx)
    })
    .await;
    cx.run_until_parked();

    // ④ 基准就位：1 个 modified hunk
    let first = diff.update(cx, |diff, cx| diff.snapshot(cx));
    assert_hunks(
        first.hunks_intersecting_range(
            Anchor::min_max_range_for_buffer(buffer.remote_id()),
            &buffer,
        ),
        &buffer,
        &base_text,
        &[(1..2, "two\n", "TWO\n", DiffHunkStatus::modified_none())],
    );

    // ⑤ 用户编辑 + 同步重算
    buffer.edit([(0..0, "zero\n")]);
    diff.update(cx, |diff, cx| diff.recalculate_diff_sync(&buffer, cx));
    let second = diff.update(cx, |diff, cx| diff.snapshot(cx));
    assert_eq!(second.base_text_string(), Some(base_text.clone())); // 基准没动

    // ⑥ 基准被删除：None → 整文件 Added
    diff.update(cx, |diff, cx| {
        diff.set_base_text(None, buffer.clone(), cx)
    })
    .await;
    cx.run_until_parked();
    let third = diff.update(cx, |diff, cx| diff.snapshot(cx));
    assert!(!third.base_text_exists());
    let hunks: Vec<_> = third
        .hunks_intersecting_range(
            Anchor::min_max_range_for_buffer(buffer.remote_id()),
            &buffer,
        )
        .collect();
    assert_eq!(hunks.len(), 1);
    assert!(hunks[0].is_created_file());

    // ⑦ 核对事件流
    let mut seen = Vec::new();
    while let Ok(event) = events_rx.try_recv() {
        seen.push(event);
    }
    // 预期：BaseTextChanged, DiffChanged（③）→ DiffChanged（⑤）→ BaseTextChanged, DiffChanged（⑥）
    assert_eq!(seen.len(), 4);
}
```

要求与检查点：

1. 逐行标注每一步用到的知识点（构造、订阅、四步流程、快路径、事件、空基准特例）。
2. 关键预期：`Buffer` 需要 `Deref` 成 `BufferSnapshot` 才能传给 `set_base_text`/`recalculate_diff_sync`（u1-l3）；`buffer.clone()` 传的是快照克隆。
3. 事件顺序依据是 [L2132-L2136](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2132-L2136)：`BaseTextChanged` 先于 `DiffChanged`，且只有存在性翻转的那两次提交会带 `BaseTextChanged`。
4. 本测试为示例代码，运行结果**待本地验证**：`cargo test -p buffer_diff test_mini_diff_session -- --nocapture`。若事件数量不符，优先检查 `run_until_parked` 的位置——事件回调靠它排空。

## 6. 本讲小结

- `BufferDiff` 实体只持状态不算 diff：五个字段里 `base_text_buffer`（只读 `language::Buffer`）与 `diff_snapshot`（可选快照）是核心；`new`（先建后灌）、`new_with_base_text_buffer`（外部基准）、`new_unchanged`（零差异快路径）覆盖三种初始化场景。
- `update_diff` **只算不写**：返回 `Task<BufferDiffUpdate>` 交给调用方提交；`unchanged_hunks` 快路径用「存在标志 + 基准版本 + buffer 版本」三相等判断直接复用旧 hunk 树。
- `set_snapshot`（薄封装）/`set_snapshot_with_secondary` 是唯一写入口：组装新快照（继承 pending）、按三分支算 `DiffChanged` 的范围、最后**先 `BaseTextChanged`（仅基准变化时）后 `DiffChanged`（总是）**。
- `snapshot()` 是安全读入口：未计算时返回 `base_text_exists: false` 的空兜底快照，secondary 快照每次读取时动态组装成 `Arc` 并禁止嵌套。
- `set_base_text` 是四步异步流程：①基准 buffer 自 diff → ②生成未来快照（丢弃点1：版本不符即弃）→ ③后台算 diff → ④`fast_forward` 基准 + `set_snapshot` 同事务提交（丢弃点2）；丢弃返回的 Task 即取消，调用不许交叠。
- 测试侧 `recalculate_diff_sync` = 「基准不动 + 前台 block_on + set_snapshot」，是模拟编辑后刷新的标准工具。

## 7. 下一步学习建议

下一讲 **u3-l5「compare_hunks 与 DiffChanged：增量通知的精确范围」**深入本讲只当黑盒用的 `compare_hunks`：新旧两棵 hunk 树如何用双游标归并出 `changed_range`、`base_text_changed_range`，以及 `extended_range` 为什么要把变更区之外最近未变 hunk 与其间的 buffer 编辑也纳入——那正是编辑器 gutter 决定重绘范围的依据。

延伸阅读：

- 对照 [test_buffer_diff_compare（src/buffer_diff.rs:3162）](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L3162) 与 [test_extended_range（src/buffer_diff.rs:3714）](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L3714)，看本讲的 `DiffChanged` 三个范围在真实编辑序列下长什么样。
- `language::Buffer::diff` / `snapshot_with_edits` / `fast_forward` 的定义在 language crate（`crates/language/src/buffer.rs`），理解它们有助于看清 `set_base_text` 步骤①②的底细。
- 进入单元四之前，回顾 `DiffBaseKind` 与 `is_stageable`（[src/buffer_diff.rs:1685-1689](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1685-L1689)），那是 staging 三方模型的入口。
