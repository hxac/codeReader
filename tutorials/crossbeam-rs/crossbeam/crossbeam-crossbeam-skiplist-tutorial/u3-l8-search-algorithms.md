# 搜索算法：search_bound / search_position / next_node

## 1. 本讲目标

本讲进入 `base.rs` 无锁算法的**读路径**。学完本讲，你应当能够：

- 说清楚跳表「自顶向下逐层下降、每层水平推进」的搜索骨架，并解释为什么要先跳过空高层。
- 区分三个搜索函数的职责：`search_position`（为 `insert`/`remove` 收集每层邻接点）、`search_bound`（为 `get`/`front`/`back`/`lower_bound`/`upper_bound` 返回单个目标节点）、`next_node`（level 0 上的「向后走一步」）。
- 读懂 `continue 'search` 这一整体重启机制：在哪些条件下触发、为什么必须整体重启而不是局部修复。
- 理解 `help_unlink` 的「协作式清理」：搜索者顺手帮并发删除者把逻辑删除节点物理摘除。
- 读懂 `above_lower_bound` / `below_upper_bound` 两个边界谓词，以及 `upper_bound: bool` 参数如何用一个函数同时表达 lower/upper bound 两种语义。

本讲**只讲搜索**，不讲插入的随机高度、`mark_tower` 的删除权抢占与 `clear` 的分批回收——这些分别在 u3-l10 与 u3-l11。但本讲必须先建立对「带 tag 的指针」与「逻辑删除」的直觉，因为搜索全程都在和它们打交道。

## 2. 前置知识

本讲依赖前面三讲建立的认知，先做一次快速回顾：

**节点与塔的内存布局（u2-l5）。** 每个 `Node<K,V>` 用 `#[repr(C)]` 把字段固定为 `value / key / refs_and_height / tower`，其中 `tower` 是一段**变长**的 `Atomic<Node>` 指针数组，长度等于节点高度。`TowerRef` / `NodeRef` 用 `NonNull` 保留对这段动态尾部的访问 provenance，`get_level(i)` 返回第 `i` 层的原子指针。头节点 `Head` 是一个预分配满高（`MAX_HEIGHT=32`）的「假节点」，本身不存键值，只起塔顶入口作用。常量 `HEIGHT_BITS=5`、`MAX_HEIGHT=32`、`HEIGHT_MASK=31`。`max_height` 是 `HotData` 里一个只增不减的原子值，作为「从哪一层开始下降」的提示。

**epoch 回收与引用计数（u2-l6）。** 节点被从链表中「物理摘除」后不能立刻释放，因为别的线程可能正握着它的指针。crossbeam-epoch 的 `Guard` 保护临界区内临时读者加载的 `Shared` 指针；`refs_and_height` 的高位是引用计数，保护跨临界区长期持有的 `Entry`/`RefEntry`。`NodeRef::decrement` 在归零时用 `fetch_sub(Release)` + `fence(Acquire)` + `defer_unchecked(finalize)` 延迟回收。本讲里你会看到 `help_unlink` 成功摘除一个节点时调用 `curr.decrement(guard)`——正是「少了一层链入引用」。

**带 tag 的指针（crossbeam-epoch API）。** `Atomic<T>::load_consume(guard)` 用 consume 序读取一个 `Shared<T>`；`Shared::tag()` 返回最低位 tag（0 或 1）；`Shared::with_tag(0)` 清除 tag。本讲反复出现 `curr.tag() == 1` 与 `succ.tag() == 1`，其语义见 4.1 节。

**Comparator / Equivalent（u2-l7）。** `search_position` 用 `comparator.compare(&c.key, key)` 做二分定位，`get` 再用 `comparator.equivalent(&n.key, key)` 二次确认命中。本讲的 `search_bound` 同样用 `compare` 配合两个边界谓词工作。`compare` 返回 `cmp::Ordering`，`Ordering` 上的 `is_ge()/is_gt()/is_le()/is_lt()` 即 `>= / > / <= / <` 的布尔判定。

## 3. 本讲源码地图

本讲只涉及一个文件，但其中的函数彼此调用、互相复用，是 `base.rs` 里最密集的一段：

| 位置 | 作用 |
|---|---|
| `struct Position` | `search_position` 的返回类型，记录「是否命中」以及**每一层**的左右邻接点，供 `insert`/`remove` 直接取用。 |
| `SkipList::front / back / get / lower_bound / upper_bound` | 五个公开「读」入口，分别选择调用 `next_node` 或 `search_bound`。 |
| `search_position` | 按 key 搜索，填满 `Position` 的 `left`/`right` 数组。供 `insert_internal`/`remove` 使用。 |
| `search_bound` | 按 `Bound` 搜索，返回单个目标节点；用 `upper_bound: bool` 同时表达 lower/upper bound。 |
| `next_node` | 在 level 0 上从一个前驱向后走，直到遇到未删除节点；供 `front`/迭代器使用。 |
| `help_unlink` | 协作式清理：把逻辑删除的 `curr` 从某一层 CAS 摘除。被上面三个搜索函数复用。 |
| `above_lower_bound / below_upper_bound` | 两个边界谓词，把 `Bound::Included/Excluded/Unbounded` 翻译成 `compare` 的 `>=/>/<=/</` 判定。 |

调用关系（简化）：

```
get            ──► search_bound(Included(key), false) ─► equivalent 二次确认
front          ──► next_node(head, Unbounded)          ─► search_bound(兜底)
back           ──► search_bound(Unbounded, true)
lower_bound    ──► search_bound(bound, false)
upper_bound    ──► search_bound(bound, true)
insert/remove  ──► search_position(key)  （收集每层 left/right）
                  └─ 物理摘除失败时回退 ─► search_bound(Included(key), false)

search_bound / search_position / next_node  三者都 ──► help_unlink
```

全部源码位于 [src/base.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs)。

## 4. 核心概念与源码讲解

本讲拆成五个最小模块。建议按顺序读：先建立「带 tag 指针 = 逻辑删除」的直觉（4.1），再读 `search_position`（4.2）把骨架看懂，然后 `search_bound`（4.3）只是同一骨架换了个返回方式，`next_node`/`help_unlink`（4.4）讲协作清理，最后两个边界谓词（4.5）补全 `Bound` 语义。

### 4.1 tag 位与逻辑删除：搜索全程都在面对的「脏数据」

#### 4.1.1 概念说明

无锁跳表不能像普通链表那样「直接删节点并 free」——因为别的线程可能正拿着被删节点的指针。crossbeam-skiplist 采用 **Harris/Michael 风格的逻辑删除**：删除一个节点时，先把它的**出向指针**全部打上 tag（最低位置 1），这表示「该节点已死」；物理摘除（把前驱指针绕过它）则由任何一个恰好路过的搜索者「顺手」完成。

关键直觉：**tag 打在指针上，但描述的是指针源头的节点**。

- `pred.get_level(level)` 这一格存的是 `pred` 指向其后继的指针。若该指针 `tag()==1`，说明 **`pred` 自己已死**（`pred` 的出向指针被打了 tag）。因为搜索每一步都把刚走过的节点当作下一轮的 `pred`，遇到这种情况只能**整体重启**。
- `c.get_level(level)` 是节点 `c` 指向其后继 `succ` 的指针。若 `succ.tag()==1`，说明 **`c` 已死**，可以尝试帮它从这一层物理摘除（`help_unlink`）。

所以搜索算法里你会反复看到两类 tag 检查：检查 `curr.tag()`（== pred 的出向指针，发现 pred 死了→重启）和检查 `succ.tag()`（== c 的出向指针，发现 c 死了→帮忙摘除）。

> 注：`mark_tower` / `is_removed` 的具体实现见 u3-l9，本讲只需记住「tag==1 ⇒ 该节点逻辑删除」。

#### 4.1.2 核心流程

一个搜索者在某一层的「水平推进」循环，伪代码如下：

```
curr = pred.get_level(level)          # pred 的出向指针
if curr.tag() == 1:                   # pred 自己被删了
    continue 'search                  # 整体重启
while curr 非空:
    c = curr 指向的节点
    succ = c.get_level(level)         # c 的出向指针
    if succ.tag() == 1:               # c 被删了
        if help_unlink(pred.level, c, succ) 成功:   # 顺手物理摘除 c
            curr = succ.with_tag(0);  continue      # 继续在这一层走
        else:
            continue 'search          # 摘除失败，整体重启
    # c 是活节点，按 key 判定：停下 or 越过
    if 命中/越过条件: break
    pred = c;  curr = succ            # 前进一步
```

三种结局：正常推进（命中或越过则 `break`）、协作摘除后继续、整体重启。

### 4.2 search_position：按 key 收集每层邻接点

#### 4.2.1 概念说明

`search_position` 是**写入路径**专用的搜索。`insert_internal` 要把新节点插进每一层，`remove` 要从每一层把目标节点摘除，二者都需要知道「在每一层，目标 key 的左邻（pred）和右邻（succ）分别是谁」。于是 `search_position` 一次性把**所有层**的邻接关系都收集进 `Position`，避免逐层重复搜索。

`Position` 结构是这一用途的直接体现：

[src/base.rs:L429-L440](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L429-L440) —— `Position` 把「是否命中」与「每层左右邻接点」打包返回。

```rust
struct Position<'a, K, V> {
    /// Reference to a node with the given key, if found.
    /// If this is `Some` then it will point to the same node as `right[0]`.
    found: Option<NodeRef<'a, K, V>>,
    /// Adjacent nodes with smaller keys (predecessors).
    left: [TowerRef<'a, K, V>; MAX_HEIGHT],
    /// Adjacent nodes with equal or greater keys (successors).
    right: [Shared<'a, Node<K, V>>; MAX_HEIGHT],
}
```

注意三个细节：①`left[i]`/`right[i]` 是**第 `i` 层**的邻接点（`left` 是前驱塔，`right` 是后继共享指针）；②`found` 命中时与 `right[0]` 指向同一节点——也就是说「是否命中」看的就是 level 0 上有没有 key 完全相等的节点；③数组长度恒为 `MAX_HEIGHT`，未走到的层用 `head`/`null` 初始化占位。

#### 4.2.2 核心流程

`search_position` 的下降骨架（与 `search_bound` 几乎对称）：

```
'search: loop {
    result = Position { found: None, left: [head; 32], right: [null; 32] }
    level = max_height.load(Relaxed)              # 起点：当前最高层（提示值）
    # 快速跳过空的高层
    while level >= 1 && head.get_level(level-1).load(Relaxed).is_null():
        level -= 1
    pred = head
    while level >= 1:
        level -= 1
        curr = pred.get_level(level).load_consume(guard)
        if curr.tag() == 1:  continue 'search     # pred 死了 → 重启
        while let Some(c) = curr 节点:
            succ = c.get_level(level).load_consume(guard)
            if succ.tag() == 1:                    # c 死了
                if help_unlink(pred.get_level(level), c, succ) 成功:
                    curr = succ; continue          # 摘除成功，继续走
                else: continue 'search             # 摘除失败，重启
            match comparator.compare(&c.key, key):
                Greater => break                   # c 已越过 key，本层到此为止
                Equal   => { result.found = Some(c); break }   # 命中
                Less    => {}                      # c.key < key，继续向右
            pred = c.as_tower();  curr = succ      # 前进一步
        result.left[level] = pred                  # 记录本层邻接
        result.right[level] = curr
    return result
}
```

下降过程的复杂度就是跳表的期望复杂度 \(O(\log n)\)：因为高层指针「跳过」了大量节点，每层期望只走常数步。设最高有效层为 \(h\)，则搜索总步数期望约为

\[
\sum_{i=0}^{h} \text{(第 } i \text{ 层期望水平步数)} = O(\log n)
\]

（严格的期望分析依赖随机高度服从 \(p=0.5\) 的几何分布，见 u3-l10。）

#### 4.2.3 源码精读

`search_position` 的入口与下降框架（注意 `'search` 标签的整体重启）：

[src/base.rs:922-L1008](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L922-L1008) —— `search_position` 主体。

关键片段一：起点选择与空高层跳过。`max_height` 只是一个**只增不减的提示**，真实最高层要用「从该层往下数，第一个非空的 `head` 指针」确定，所以才需要 `while level >= 1 && head.get_level(level-1).is_null()` 这个快速回退循环：

[src/base.rs:937-L952](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L937-L952) —— 用 `max_height` 起步、跳过空层、以 `head` 为初始 `pred`。

关键片段二：tag 检查 + 协作清理 + key 比较，这正是 4.1.2 伪代码的 Rust 落地：

[src/base.rs:957-L998](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L957-L998) —— 每层的水平推进循环：`curr.tag()` 检查 pred、`succ.tag()` 触发 `help_unlink`、`compare` 三分支判定。

关键片段三：把本层邻接点写回 `result`。注意它在 `while level >= 1` 内层循环的**每次 `level -= 1` 之后**执行一次，因此逐层填满 `left`/`right`：

[src/base.rs:1000-L1003](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1000-L1003) —— `result.left[level] = pred; result.right[level] = curr;`。

谁在用 `search_position`？`insert_internal` 用它查重与收集插入点（[src/base.rs:1035](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1035)），并在塔构建失败时重搜（[src/base.rs:1175](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1175)、[L1216](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1216)）；`remove` 用它定位目标与每层前驱（[src/base.rs:1285](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1285)），然后用 `search.left[level]` 手动逐层 CAS 摘除（[src/base.rs:1310-L1328](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1310-L1328)）。这就是 `Position` 收集「每层邻接」的用武之地。

#### 4.2.4 代码实践

**实践目标：** 把 `search_position` 里所有触发 `continue 'search` 的条件标出来，并验证它在「表里散落着逻辑删除节点」时仍能正确定位。

**操作步骤（源码阅读部分）：**

1. 打开 [src/base.rs:922-L1008](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L922-L1008)，在源码边上手写批注。你会找到**两处** `continue 'search`：
   - `if curr.tag() == 1`：当前层 `pred` 的出向指针被标记 ⇒ `pred` 已逻辑删除 ⇒ 整体重启（[L962-L964](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L962-L964)）。
   - `help_unlink(...)` 返回 `None`：发现 `c` 已死、尝试顺手摘除但 CAS 失败 ⇒ 无法继续从当前位置走 ⇒ 整体重启（[L977-L981](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L977-L981)）。
2. 对比 `search_bound`（4.3 节），它有**完全相同**的两处重启条件——这是二者共享骨架的直接证据。

**操作步骤（串行测试部分）：** 直接调用 `base::SkipList`（参考 `tests/base.rs` 的写法）：

```rust
// 示例代码：放在 tests/search_after_remove.rs（运行：cargo test --test search_after_remove）
#![allow(clippy::redundant_clone)]
use crossbeam_epoch as epoch;
use crossbeam_skiplist::{base::SkipList, SkipList as _}; // 只需 base 的 SkipList

#[test]
fn search_after_partial_remove() {
    let collector = epoch::default_collector();
    let s = SkipList::<i64, i64>::new(collector.clone());
    let guard = &epoch::pin();

    // 1. 批量插入 0..200
    for k in 0..200 {
        s.insert(k, k, guard).release(guard);
    }
    // 2. 删除其中一半（偶数 key）
    for k in (0..200).step_by(2) {
        s.remove(&k, guard).unwrap().release(guard);
    }
    // 3. 再插入一些「新」key 与若干被删 key 的「替换」
    s.insert(1500, -1500, guard).release(guard);   // 全新 key
    s.insert(42, 420, guard).release(guard);       // 重新插入一个被删过的 key

    // 4. 验证：搜索在存在逻辑删除节点时仍能正确定位
    for k in (1..200).step_by(2) {
        // 奇数 key 仍在表里
        assert_eq!(*s.get(&k, guard).unwrap().value(), k);
    }
    for k in (0..200).step_by(2) {
        // 偶数 key 中，只有 42 因重新插入而存在，其余应查不到
        if k == 42 {
            assert_eq!(*s.get(&42, guard).unwrap().value(), 420);
        } else {
            assert!(s.get(&k, guard).is_none());
        }
    }
    assert_eq!(*s.get(&1500, guard).unwrap().value(), -1500);
}
```

> 注意：上面是**示例代码**，未在本机运行过。`tests/base.rs` 中的 `SkipList` 指 `base::SkipList`，`insert` 返回 `RefEntry` 需要 `.release(guard)` 释放引用计数（见 u2-l6）。如果你看到 trait 导入报错，按编译器提示调整 `use`。

**需要观察的现象：** 删除一半 key 后，链表里仍残留大量**逻辑删除但尚未物理摘除**的节点（取决于 epoch 是否推进、是否有别的搜索者帮忙摘除）。但 `get` 仍返回正确结果——这正是 `search_position`/`search_bound` 在搜索中「越过死节点、必要时帮忙摘除、必要时整体重启」带来的正确性。

**预期结果：** 测试通过。注意 `len()` 由于 `Relaxed` 加载只是近似值，**不要**用它做精确断言；要用 `get` 逐个验证。

#### 4.2.5 小练习与答案

**练习 1.** `Position.found` 注释说「If this is `Some` then it will point to the same node as `right[0]`」。请根据源码解释为什么。

> **答：** 在 level 0 的水平推进循环里，当 `compare` 返回 `Equal` 时执行 `result.found = Some(c)` 然后 `break`；循环退出后 `result.right[0] = curr`，而此时 `curr` 仍是 `c`（命中时没有执行 `pred = c; curr = succ` 前进一步），所以 `found` 与 `right[0]` 指向同一个节点 `c`。

**练习 2.** 为什么 `left` 数组的元素类型是 `TowerRef`，而 `right` 是 `Shared<Node>`？

> **答：** `left[level]` 之后会被 `insert`/`remove` 用来读取它的 `get_level(level)` 并做 CAS（写操作），需要保留对动态塔的 provenance，所以是 `TowerRef`（带 `NonNull`）。`right[level]` 主要作为「后继指针的值」参与 CAS 的期望值或新值的对比，存成 `Shared`（带 tag 的指针）即可，不需要进一步解引用其塔。

### 4.3 search_bound：一个函数同时服务 lower/upper bound 与 front/back

#### 4.3.1 概念说明

`search_bound` 是**读路径**的主力。它和 `search_position` 共用同一套「自顶下降、水平推进、tag 检查、协作清理」的骨架，但返回值不同：它只返回**一个目标节点**（`Option<NodeRef>`），不收集每层邻接。它用两个手段把多种查询统一起来：

1. **`bound: Bound<&Q>`**：把查询点表达成 `Unbounded` / `Included(q)` / `Excluded(q)`，于是「front」「查某个 key」都可以套用同一套代码。
2. **`upper_bound: bool`**：一个布尔开关决定语义——`false` 表示「找第一个 ≥ bound 的节点」（lower bound 语义），`true` 表示「找最后一个 ≤ bound 的节点」（upper bound 语义）。

公开入口怎么调用它（这一段是理解整个读路径的钥匙）：

| 公开方法 | 对 `search_bound` 的调用 | 含义 |
|---|---|---|
| `get(key)` | `search_bound(Included(key), false)` + `equivalent` 二次确认 | 第一个 ≥ key 的节点，且 key 真的相等 |
| `front()` | 走 `next_node`（等价于 lower bound 无界） | 第一个节点 |
| `back()` | `search_bound(Unbounded, true)` | 最后一个节点 |
| `lower_bound(b)` | `search_bound(b, false)` | 第一个 ≥ b 的节点 |
| `upper_bound(b)` | `search_bound(b, true)` | 最后一个 ≤ b 的节点 |

源码对应：`get` 在 [src/base.rs:569-L585](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L569-L585)（关键调用 [L575](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L575) 与二次确认 [L576](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L576)）；`back` 在 [L548-L557](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L548-L557)（调用 [L551](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L551)）；`lower_bound` 在 [L590-L606](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L590-L606)（调用 [L600](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L600)）；`upper_bound` 在 [L611-L627](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L611-L627)（调用 [L621](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L621)）。

> 为什么 `get` 拿到「第一个 ≥ key」的节点后还要用 `equivalent` 二次确认？因为 `search_bound` 的停止条件基于 `compare` 的序关系（「≥ key」），可能停在 key 更大的节点上。`equivalent` 才严格判定「相等」。这就是 u2-l7 强调的「写用 `compare`、读多一步 `equivalent`」两步法。

#### 4.3.2 核心流程

`search_bound` 与 `search_position` 的骨架几乎逐行一致，唯一差别在「水平推进时的停止/记录逻辑」。它用一个 `result: Option<NodeRef>` 累积「当前最佳节点」，用 `upper_bound` 开关选择两种策略：

```
'search: loop {
    level = max_height.load(Relaxed); 跳过空高层; pred = head
    result = None
    while level >= 1:
        level -= 1
        curr = pred.get_level(level).load_consume(guard)
        if curr.tag() == 1: continue 'search        # pred 死 → 重启
        while let Some(c) = curr 节点:
            succ = c.get_level(level).load_consume(guard)
            if succ.tag() == 1:                      # c 死 → 帮忙摘除
                if help_unlink(...).is_some(): curr = succ; continue
                else: continue 'search
            if upper_bound:                          # 找最后一个 ≤ bound
                if !below_upper_bound(cmp, bound, &c.key): break   # c.key > bound，停下
                result = Some(c)                     # c 合格，记下，继续往右找
            else:                                    # 找第一个 ≥ bound
                if above_lower_bound(cmp, bound, &c.key):
                    result = Some(c);  break         # 找到第一个 ≥ bound，停下
            pred = c.as_tower();  curr = succ
    return result
}
```

注意两种语义的对称美感：

- **lower bound（`upper_bound == false`）**：要「第一个」满足条件的节点，所以**条件一旦满足就 `break`**，把该节点记为 `result`。
- **upper bound（`upper_bound == true`）**：要「最后一个」满足条件的节点，所以**条件不满足才 `break`**，每遇到一个合格节点就更新 `result` 并继续向右。

跨层下降时，每层结束都把更精确的 `result` 带到下一层，最终在 level 0 收敛到目标节点。

#### 4.3.3 源码精读

`search_bound` 的函数签名与文档把两种语义讲得很清楚：

[src/base.rs:825-L842](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L825-L842) —— `upper_bound==true` 返回「最后一个 ≤ key 的节点」，`false` 返回「第一个 ≥ key 的节点」。

核心的「下降 + tag 检查 + 停止/记录」逻辑：

[src/base.rs:843-L919](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L843-L919) —— `search_bound` 主体。

最值得逐字读的是这段「upper/lower 分支」，它是两种语义的全部差别所在：

[src/base.rs:895-L914](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L895-L914) —— `upper_bound` 分支用 `below_upper_bound` 决定 `break`、用 `result = Some(c)` 累积；lower 分支用 `above_lower_bound` 决定「记录并 break」。

`search_bound` 还在「写入路径」里扮演兜底角色：`remove` 在逐层 CAS 摘除失败时，调用 `search_bound(Included(key), false)` 把残留的死节点彻底清理掉（[src/base.rs:1325](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1325)）；`insert_internal` 在塔顶指针被标记时也用它清理（[src/base.rs:1227-L1229](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1227-L1229)）。原因正是：任何一次 `search_bound` 都会顺手把沿途能摘的死节点摘掉（4.4 节的 `help_unlink`）。

#### 4.3.4 代码实践

**实践目标：** 用同一张表验证 `get`/`lower_bound`/`upper_bound`/`front`/`back` 都正确，并理解它们如何映射到 `search_bound` 的两种语义。

**操作步骤：**

1. 阅读现有测试 [tests/base.rs:277-（lower_bound）](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/tests/base.rs#L277) 与 `upper_bound` 测试，确认你对 `Included/Excluded/Unbounded` 语义的理解。
2. 写一个最小示例（放在 `tests/search_bound_semantics.rs`）：

```rust
// 示例代码
use std::ops::Bound;
use crossbeam_epoch as epoch;
use crossbeam_skiplist::base::SkipList;

#[test]
fn bound_semantics() {
    let s = SkipList::new(epoch::default_collector().clone());
    let guard = &epoch::pin();
    for k in [10, 20, 30, 40, 50] {
        s.insert(k, k, guard).release(guard);
    }

    // get = lower_bound(Included) + equivalent
    assert_eq!(*s.get(&30, guard).unwrap().value(), 30);
    assert!(s.get(&25, guard).is_none());                // 没有等于 25 的

    // lower_bound: 第一个 >= bound
    assert_eq!(*s.lower_bound(Bound::Included(&25), guard).unwrap().value(), 30);
    assert_eq!(*s.lower_bound(Bound::Excluded(&30), guard).unwrap().value(), 40);
    assert_eq!(*s.lower_bound(Bound::Unbounded, guard).unwrap().value(), 10); // == front

    // upper_bound: 最后一个 <= bound
    assert_eq!(*s.upper_bound(Bound::Included(&25), guard).unwrap().value(), 20);
    assert_eq!(*s.upper_bound(Bound::Excluded(&30), guard).unwrap().value(), 20);
    assert_eq!(*s.upper_bound(Bound::Unbounded, guard).unwrap().value(), 50); // == back
}
```

**需要观察的现象：** 把 `Bound::Included(&25)` 同时喂给 `lower_bound`（期望 30，因为「第一个 ≥ 25」）和 `upper_bound`（期望 20，因为「最后一个 ≤ 25」），两者结果不同却都来自同一个 `search_bound` 函数——差别只在 `upper_bound: bool` 这一位。

**预期结果：** 所有断言通过。若某条失败，回到 [src/base.rs:895-L914](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L895-L914) 核对你对两种 `break` 时机（「不满足才 break」vs「满足即 break」）的理解。

#### 4.3.5 小练习与答案

**练习 1.** `back()` 为什么用 `search_bound(Unbounded, true)` 而不是「一直向右走到尾」？

> **答：** `Unbounded` 使 `below_upper_bound` 恒为 `true`（见 4.5.2），于是 upper-bound 分支每遇到一个活节点就更新 `result` 并继续向右，跨层下降后最终 `result` 就是全局最大的活节点，即 `back`。复用 `search_bound` 还顺带获得 tag 检查与 `help_unlink` 的正确性，无需另写一套「向右走」的循环。

**练习 2.** 如果把 `upper_bound` 分支里那句 `if !below_upper_bound(...) { break; }` 的 `!` 去掉（改成满足条件就 break），语义会变成什么？

> **答：** 会变成「第一个 **严格大于** bound 的节点之后立刻停下，且不记录它」——但由于此时没有 `result = Some(c)`，`result` 还停留在上一个合格节点，整体行为会错乱（对 `Unbounded` 还会陷入「第一个节点就 break、result 为 None」的错误）。这正说明 `break` 与「是否记录 `result`」的配对必须精确。

### 4.4 next_node 与 help_unlink：在 level 0 上协作前进

#### 4.4.1 概念说明

`next_node` 解决一个更窄的问题：给定一个前驱 `pred`，返回它在 level 0 上的**下一个活节点**。这是 `front()`（从 `head` 找第一个节点）和迭代器「向前走一步」的基础。它只走 level 0，因为迭代天然按键升序，而 level 0 是完整的有序单链表。

`help_unlink` 是被 `search_position`、`search_bound`、`next_node` 三个函数**共同复用**的协作清理原语。当搜索者发现「后继指针 `succ` 被 tag」（即当前节点 `c` 已逻辑删除），它会尝试把 `c` 从这一层物理摘除：用 CAS 把 `pred` 的出向指针从 `c` 直接改指到 `succ`（清掉 tag）。这就是无锁算法里常说的「helping」——任何线程都可以推进任意未完成的删除，从而保证系统总能取得进展（lock-free 的活性）。

#### 4.4.2 核心流程

**`help_unlink(pred, curr, succ)` 的流程：**

```
# 尝试 CAS：把 pred 的出向指针从 curr 改为 succ.with_tag(0)
ok = pred.compare_exchange(
        expected = Shared::from(curr.ptr),     # 必须仍是 curr
        new     = succ.with_tag(0),            # 直接绕过 curr，清 tag
        Release, Relaxed)
if ok:
    curr.decrement(guard)        # curr 少了一层链入，引用计数 -1（可能触发延迟回收）
    return Some(succ.with_tag(0))   # 让调用者从 succ 继续
else:
    return None                     # CAS 失败（有人改了 pred），调用者整体重启
```

CAS 成功的含义：`pred → curr → succ` 变成 `pred → succ`，`curr` 在这一层被物理绕过。注意它**只摘这一层**；`curr` 在更高层可能还链着，要等更高层的搜索也帮忙摘掉，或被 `remove` 的逐层 CAS 摘掉。

**`next_node(pred, lower_bound)` 的流程：**

```
curr = pred.get_level(0).load_consume(guard)
if curr.tag() == 1:                       # pred 自己已死
    return search_bound(lower_bound, false, guard)   # 回退到完整搜索
while let Some(c) = curr 节点:
    succ = c.get_level(0).load_consume(guard)
    if succ.tag() == 1:                   # c 已死
        if help_unlink(pred.get_level(0), c, succ) 成功:
            curr = succ; continue         # 摘掉 c，继续在 level 0 走
        else:
            return search_bound(...)      # 摘除失败，回退完整搜索
    return Some(c)                        # c 是活节点，返回它
None                                      # 走到链表末尾
```

`next_node` 与 `search_bound` 在 level 0 上的水平推进几乎一样，区别有二：①`next_node` **不判 key**，只是「走到下一个活节点就返回」；②它的「pred 死了」和「摘除失败」两个失败分支不是 `continue 'search`（因为它没有外层 `'search` 循环），而是**回退调用一次完整的 `search_bound`** 来恢复。

#### 4.4.3 源码精读

`help_unlink` 标了 `#[cold]`（提示编译器它是冷路径，减少对热路径的影响），只做一次 CAS：

[src/base.rs:753-L781](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L753-L781) —— `help_unlink`：CAS 把 `pred → curr` 改成 `pred → succ.with_tag(0)`，成功则 `curr.decrement` 并返回 `succ`。

`next_node` 全文（注意 level 0 的 `load_consume`、两处 `search_bound` 回退）：

[src/base.rs:783-L823](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L783-L823) —— `next_node`：从 `pred` 在 level 0 上向后走到下一个活节点，遇到死节点就 `help_unlink`，失败/前驱已死则回退 `search_bound`。

`front()` 直接用 `head` 作为 `pred`、`Unbounded` 作为下界调用它：

[src/base.rs:538-L546](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L538-L546) —— `front` 调用 `next_node(self.head.as_tower(), Bound::Unbounded, guard)`。

`help_unlink` 里 `curr.decrement(guard)` 是 u2-l6 的延迟回收协议：摘掉一层链入相当于「少一个引用」，归零时由 epoch 回收。这条线索把「搜索算法」与「内存回收」紧密扣在一起。

#### 4.4.4 代码实践

**实践目标：** 通过阅读测试，理解 `Entry::is_removed()` 与「逻辑删除但尚未物理摘除」的中间态，并验证 `front` 在头部节点被删后能跳过它。

**操作步骤：**

1. 阅读 [tests/base.rs:144-L146](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/tests/base.rs#L144)：`assert!(!e.is_removed()); assert!(e.remove()); assert!(e.is_removed());`。这是 u3-l9 的「标记」语义：`e.remove()` 返回 `true` 表示本线程赢得了对 level 0 tag 的 CAS（即赢得了删除权），此后 `e.is_removed()` 为真，但节点可能还**物理链在表里**，直到某次搜索 `help_unlink` 它。
2. 写一个串行测试，观察 `front` 跳过被删头节点：

```rust
// 示例代码
use crossbeam_epoch as epoch;
use crossbeam_skiplist::base::SkipList;

#[test]
fn front_skips_removed_head() {
    let s = SkipList::new(epoch::default_collector().clone());
    let guard = &epoch::pin();
    s.insert(1, 10, guard).release(guard);
    s.insert(2, 20, guard).release(guard);
    s.insert(3, 30, guard).release(guard);

    assert_eq!(*s.front(guard).unwrap().value(), 10);   // 头是 key=1
    s.remove(&1, guard).unwrap().release(guard);        // 删头节点

    // front() 内部走 next_node：head 的 level0 后继曾是 key=1（已死），
    // 搜索会 help_unlink 把它摘掉，然后返回 key=2。
    assert_eq!(*s.front(guard).unwrap().value(), 20);
}
```

**需要观察的现象：** 删除 key=1 后，`head` 的 level 0 指针仍指向 key=1 节点（其出向指针被 tag）。下一次 `front()` → `next_node` 会发现 `succ.tag()==1`，于是 `help_unlink` 把 `head → key1 → key2` 改成 `head → key2`，返回 key=2。这就是「搜索者顺手清理」的直接体现。

**预期结果：** 断言通过。若想「看到」清理动作，可在 `help_unlink` 的 `Ok(_)` 分支加一行 `eprintln!("help_unlinked a node");`（仅用于学习，**不要提交**），再跑测试观察输出——但需注意 epoch 回收时机不定，加日志只能看到摘除，看不到 dealloc。

#### 4.4.5 小练习与答案

**练习 1.** `next_node` 的两处 `return self.search_bound(...)` 分别在什么条件下触发？为什么不直接 `continue` 一个内层循环？

> **答：** 第一处：`curr.tag() == 1`，即 `pred` 的 level 0 出向指针被标记，说明 `pred` 已死——`pred` 这条「立足点」失效，无法继续。第二处：`help_unlink` 返回 `None`，即 CAS 失败，说明有别的线程改动了 `pred` 的指针，当前位置已不可靠。两种情况下我们都失去了确定的「立足点」，所以回退到一次完整 `search_bound`（它能从 `head` 重新安全地定位）。`next_node` 本身没有外层 `'search` 循环可 `continue`，回退 `search_bound` 正是「重启」的等价手段。

**练习 2.** `help_unlink` 为什么用 `succ.with_tag(0)` 作为 CAS 的新值，而不是直接 `succ`？

> **答：** `succ` 此刻带 tag==1（正是它带 tag 才触发了 `help_unlink`）。物理摘除后，`pred` 的新后继应当指向一个「干净」的活节点 `succ`，必须把 tag 清掉（`.with_tag(0)`）；否则 `pred` 的出向指针会带着 tag==1，下一个搜索者会误判 `pred` 已死而整体重启，破坏正确性。

### 4.5 above_lower_bound / below_upper_bound：把 Bound 翻译成比较

#### 4.5.1 概念说明

`Bound<&T>` 是标准库的枚举：`Unbounded`、`Included(&T)`（含端点）、`Excluded(&T)`（不含端点）。`search_bound` 需要把「节点 key 是否在 bound 之上/之下」翻译成 `compare` 返回的 `Ordering` 上的布尔判定。这就是这两个辅助函数的全部职责——它们只是「翻译表」，不含任何并发逻辑。

- `above_lower_bound(cmp, bound, other)`：`other` 是否**严格在 lower bound 之上**（即满足「可作 lower-bound 候选」）。
- `below_upper_bound(cmp, bound, other)`：`other` 是否**在 upper bound 之下**（即满足「可作 upper-bound 候选」）。

#### 4.5.2 核心流程（真值表）

设 `o = comparator.compare(other, key)`，`is_ge/is_gt/is_le/is_lt` 分别表示 `>=/>/<=/</`：

| `bound` | `above_lower_bound(other)`（other 在下界之上） | `below_upper_bound(other)`（other 在上界之下） |
|---|---|---|
| `Unbounded` | `true`（恒成立） | `true`（恒成立） |
| `Included(key)` | `o.is_ge()`（`other >= key`） | `o.is_le()`（`other <= key`） |
| `Excluded(key)` | `o.is_gt()`（`other > key`） | `o.is_lt()`（`other < key`） |

记忆窍门：`Included` 用「带等号」的 `ge/le`，`Excluded` 用「不带等号」的 `gt/lt`，`Unbounded` 恒真。

这两个谓词配合 4.3.2 的两种停止策略，完整覆盖了 `lower_bound`/`upper_bound` 的所有六种 `Bound` 组合。

#### 4.5.3 源码精读

两个函数都极短，直接对照真值表读：

[src/base.rs:2343-L2354](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L2343-L2354) —— `above_lower_bound`：`Unbounded→true`、`Included→is_ge`、`Excluded→is_gt`。

[src/base.rs:2356-L2367](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L2356-L2367) —— `below_upper_bound`：`Unbounded→true`、`Included→is_le`、`Excluded→is_lt`。

它们在 `search_bound` 里的唯一调用点就是 [src/base.rs:902](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L902) 与 [src/base.rs:906](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L906)（见 4.3.3 的 upper/lower 分支）。

#### 4.5.4 代码实践

**实践目标：** 用手算验证谓词真值表，确认它覆盖所有 `Bound` 组合。

**操作步骤（源码阅读型实践）：**

1. 在 [src/base.rs:895-L914](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L895-L914) 旁，列出 6 种 `(upper_bound: bool, Bound 变体)` 组合，手算一次 `search_bound` 在一个具体表（如 `[10,20,30]`）上对 `bound = Included(20)` 的两种语义：
   - `upper_bound=false`（lower bound）：`above_lower_bound(10,20)`=`10>=20`=`false` → 继续；`above_lower_bound(20,20)`=`20>=20`=`true` → `result=20; break`。返回 20。✓
   - `upper_bound=true`（upper bound）：`below_upper_bound(10,20)`=`10<=20`=`true` → `result=10` 继续；`below_upper_bound(20,20)`=`true` → `result=20` 继续；`below_upper_bound(30,20)`=`30<=20`=`false` → `break`。返回 20。✓
2. 验证 `Excluded` 的差别：对 `bound=Excluded(20)`，lower bound 应返回 30（`20>20` 为 `false`，继续；`30>20` 为 `true`），upper bound 应返回 10（`20<20` 为 `false`，break，`result` 停在 10）。

**需要观察的现象：** 同一个 `bound` 值，两种语义给出的结果恰好「夹住」bound 点：lower bound 给「右侧第一个」，upper bound 给「左侧最后一个」。

**预期结果：** 手算结果与 4.3.4 的测试断言一致。若不一致，检查你是否混淆了 `is_ge`（≥）与 `is_gt`（>）。

#### 4.5.5 小练习与答案

**练习 1.** 为什么这两个函数对 `Unbounded` 都返回 `true`？

> **答：** `Unbounded` 表示「没有边界限制」。对 lower bound 而言「无下界」意味着任何节点都在下界之上（恒真）；对 upper bound 而言「无上界」意味着任何节点都在上界之下（恒真）。这正好让 `lower_bound(Unbounded)` 退化成「第一个节点」（= front），`upper_bound(Unbounded)` 退化成「最后一个节点」（= back），与 4.3.1 的调用表吻合。

**练习 2.** 这两个谓词可以写成 `Comparator` 的方法吗？为什么作者选择写成自由函数？

> **答：** 可以，但写成自由函数更灵活：它们是泛型 `fn above_lower_bound<V,T,C>(comparator: &C, ...)`，对任意实现了 `Comparator<V,T>` 的 `C` 都成立，无需在 trait 上增加方法（保持 `Comparator` trait 精简）。这是 Rust 里「能用自由函数就不用 trait 方法」的常见取舍，便于复用与测试。

## 5. 综合实践

把本讲的五个模块串起来，完成一个「搜索行为观察」综合任务。

**任务：** 用 `base::SkipList` 构造一个含 50 个 key 的表，制造一组「逻辑删除但可能尚未物理摘除」的节点，然后跟踪 `get` / `front` / `back` / `lower_bound` / `upper_bound` 在此之上的行为，并画一张「某次搜索在 level 0 上的路径」示意图。

**操作步骤：**

1. 插入 `0, 10, 20, …, 490`（共 50 个 key）。
2. 删除其中 `10, 30, 50, …`（每隔一个），**不**显式触发别的搜索（即不调用 `get`），制造残留死节点。
3. 立刻调用 `s.front(guard)`，记下返回值；按 u4 的预期它应返回最小活节点（0，若未被删；或第一个未删的）。
4. 调用 `s.lower_bound(Bound::Included(&145), guard)` 与 `s.upper_bound(Bound::Included(&145), guard)`，分别记录结果；按本讲语义，lower 应是「≥145 的第一个活节点」，upper 应是「≤145 的最后一个活节点」。
5. **画图：** 取 `lower_bound` 那次调用，在纸上画出 level 0 的节点序列，把「被删节点（tag==1）」标红、把搜索走过的每一步（包括 `help_unlink` 摘除动作与任何 `continue 'search` 重启）用箭头串起来。如果你无法确定真实运行时究竟在哪一步重启（这取决于并发与 epoch 时机），就在图中标注「待本地验证：此处可能发生 0 次或多次重启」。

**预期结果：** 所有读操作返回的都应是**活节点**且 key 满足对应的 `Bound` 语义；图能说清「搜索如何越过死节点、必要时协作摘除」。这个任务把本讲的 tag 语义、`continue 'search`、`help_unlink`、两套停止策略与两个边界谓词全部用上。

> 说明：本任务以**源码阅读 + 推理**为主，运行结果是「待本地验证」的——因为无锁算法的中间步骤（几次重启、几次帮忙摘除）依赖运行时调度，不可由静态分析精确预测。你能精确预测的只有**最终返回值满足的语义不变式**。

## 6. 本讲小结

- 读路径有两个搜索入口：`search_position`（为 `insert`/`remove` 收集**每层**邻接点，返回 `Position`）与 `search_bound`（为 `get`/`front`/`back`/`lower_bound`/`upper_bound` 返回**单个**节点）。二者共享同一套「自顶下降、水平推进、tag 检查、协作清理」骨架。
- 搜索全程都在面对**逻辑删除**：`curr.tag()==1` 表示立足的 `pred` 已死 → 整体 `continue 'search` 重启；`succ.tag()==1` 表示当前节点 `c` 已死 → 调 `help_unlink` 顺手摘除。
- `help_unlink` 是三个搜索函数复用的「协作清理」原语：CAS 把 `pred → curr` 改成 `pred → succ.with_tag(0)`，成功则 `curr.decrement`（可能触发 epoch 延迟回收），失败则整体重启。这是 lock-free 活性的来源。
- `search_bound` 用 `bound: Bound<&Q>` + `upper_bound: bool` 两个参数统一了五种读操作：`false` 找「第一个 ≥ bound」（满足即 break），`true` 找「最后一个 ≤ bound」（不满足才 break，边走边记 `result`）。`get` 在它之上多一步 `equivalent` 二次确认。
- `next_node` 只在 level 0 上「走到下一个活节点」，供 `front`/迭代器使用；它的两个失败分支没有外层 `'search`，于是回退调用一次完整 `search_bound` 作为「重启」。
- `above_lower_bound` / `below_upper_bound` 是两张真值表，把 `Bound` 翻译成 `compare` 的 `is_ge/is_gt/is_le/is_lt`；`Included` 带等号、`Excluded` 不带、`Unbounded` 恒真。

## 7. 下一步学习建议

本讲把「读路径」与「搜索时的协作清理」讲透了，但刻意没碰两件事：**删除权是怎么抢的**、**新节点是怎么插进去的**。建议按以下顺序继续：

1. **u3-l9 标记指针与逻辑删除**：精读 `NodeRef::mark_tower` 与 `is_removed`，理解「自顶向下逐层 `fetch_or(1)`，以 level 0 tag 决定删除胜负」的协议——本讲里所有 `succ.tag()==1` 都来自这里。
2. **u3-l10 插入路径**：精读 `insert_internal` 与 `random_height`，看 `search_position` 收集的 `Position.left/right` 如何被用来逐层 CAS 安装新节点，以及随机高度的概率分布如何保证 \(O(\log n)\)。
3. **u3-l11 删除与清理**：精读 `remove`/`pop_front`/`clear`，看 `Position.left` 如何用于逐层 CAS 摘除、为何物理摘除失败要回退 `search_bound` 兜底、以及 `clear` 为何要分批 + `guard.repin()`。

读完这三讲，你就能完整解释 crossbeam-skiplist 的 search / insert / remove 三大主链路，并具备进入 u4（句柄与迭代器）与 u5（内存序、并发语义）的基础。
