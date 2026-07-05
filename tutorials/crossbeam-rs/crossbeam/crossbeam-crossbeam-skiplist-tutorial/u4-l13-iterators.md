# 五种迭代器：Iter / RefIter / Range / RefRange / IntoIter

## 1. 本讲目标

本讲聚焦 `src/base.rs` 中的五种迭代器。读完后你应该能够：

1. 区分 **guard-bound**（`Iter`/`Range`）与 **refcount**（`RefIter`/`RefRange`）两大类迭代器的实现差异与各自代价。
2. 看懂 `head`/`tail` 双游标模型如何让迭代器同时实现 `DoubleEndedIterator`（双向遍历）与「游标相遇即提前终止」。
3. 解释为什么相遇判定用的是 `comparator.compare(h, t).is_ge()`（含等号）而不是 `is_gt()`。
4. 理解 `IntoIter` 如何在独占访问下用 `epoch::unprotected()` 安全地边遍历边释放节点。
5. 理解 `try_pin_loop` 与 `above_lower_bound`/`below_upper_bound` 两个边界辅助函数在区间裁剪中的作用。

本讲不重复讲解搜索算法（`search_bound`/`next_node`）与逻辑删除（`mark_tower`/`is_removed`），它们分别在 u3-l8、u3-l9 已建立；也不重复 `Entry`/`RefEntry` 的生命周期模型，那是 u4-l12 的主题。本讲只回答：**这些底层原语如何被组装成五种迭代器**。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**(A) 跳表的迭代本质是「沿着 level-0 链前进」。** 跳表的塔状高层索引只是加速手段，真正定义全局有序顺序的只有最底层的 level-0 单链表。所以「正向迭代」就是沿 level-0 后继走（`next_node`），「反向迭代」无法沿指针回头，必须用一次按键搜索（`search_bound(..., upper_bound=true)`）定位前驱。这就是为什么本讲的 `next` 与 `next_back` 实现长得很不一样。

**(B) 无锁并发下，迭代器访问的节点随时可能被别的线程删除。** 因此迭代器必须保证「它要返回的节点此刻不会被回收」。base 层有两套机制：借用一个 `epoch::Guard`（临界区内保护）或给节点加一份引用计数（跨临界区持有）。这直接决定了两类迭代器的分野（详见 4.1）。

**(C) 双向迭代器要解决「从两头同时走，何时停」。** 标准 `DoubleEndedIterator` 允许交替调用 `next()`（从头取）与 `next_back()`（从尾取）。base 用两个游标 `head`（头游标，已被取走的最后一个前向元素）与 `tail`（尾游标，已被取走的最后一个后向元素）来追踪进度。当两个游标相遇或越过，迭代结束。相遇判定是本讲最微妙的细节。

> 关键术语回顾（来自前置讲义）：
> - **Guard**：crossbeam-epoch 的临界区令牌，保护它存在期间加载的 `Shared` 指针不被回收（u2-l6）。
> - **RefEntry**：引用计数句柄，`try_acquire` 加计数、`release`/`decrement` 减计数，可跨 Guard 长期持有（u4-l12）。
> - **逻辑删除 tag**：指针最低位被 `fetch_or(1)` 置 1 表示源头节点已死，`is_removed()` 只看 level-0（u3-l9）。

## 3. 本讲源码地图

本讲几乎全部内容来自单一文件：

| 文件 | 作用 |
| --- | --- |
| [`src/base.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs) | 定义 `Iter`/`RefIter`/`Range`/`RefRange`/`IntoIter` 五种迭代器、`try_pin_loop`、`above_lower_bound`/`below_upper_bound` |
| [`src/map.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs) | 高层 `SkipMap` 的迭代器封装，揭示 `map::Iter` 其实包了 `base::RefIter` |
| [`tests/map.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/tests/map.rs) | 提供 `next_back`、`range`、内存泄漏等可参考的测试惯用法 |

base 层相关入口（已在 u3-l8/u4-l12 讲过，本讲只调用）：

- `SkipList::iter / ref_iter / range / ref_range`：四个迭代器构造器（行 649–705）。
- `SkipList::front / back / lower_bound / upper_bound`：定位首/尾/边界元素，返回 `Entry`（行 538–627）。
- `Entry::next / prev` 与 `RefEntry::next / prev`：单步移动（行 1586、1611、1755、1782）。
- `IntoIterator for SkipList`：消费整个跳表得 `IntoIter`（行 1449–1477）。

---

## 4. 核心概念与源码讲解

### 4.1 两类迭代器的根本分野：guard-bound vs refcount

#### 4.1.1 概念说明

base 层把五种迭代器分成两类，理解这个划分是本讲的总钥匙：

| 类别 | 成员 | 是否实现标准 `Iterator` | `head`/`tail` 字段类型 | 每步是否需要重新 `pin` |
| --- | --- | --- | --- | --- |
| **guard-bound** | `Iter`、`Range` | ✅ 实现 `Iterator`+`DoubleEndedIterator` | `Option<NodeRef<'g, ..>>` | ❌ 复用构造时借入的同一个 `Guard` |
| **refcount** | `RefIter`、`RefRange` | ❌ 不实现（`next` 多一个 `&Guard` 参数） | `Option<RefEntry<..>>` | ✅ 每步可换一个新 `Guard` |
| **owning** | `IntoIter` | ✅ 仅 `Iterator`（单向） | `*mut Node` | ❌ 用 `unprotected()` |

为什么会有两套并行的非 owning 迭代器？因为它们各有取舍：

- **`Iter`/`Range`（guard-bound）**：构造时把一个 `&'g Guard` 存进结构体（[src/base.rs:1799-1804](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1799-L1804)）。整个迭代期间线程都 pin 在某个 epoch 上，**优点是每一步都不用重新 pin、开销低**，`next(&mut self)` 因此能符合标准 `Iterator` 签名；**代价是 Guard 必须比迭代器活得更久**，长时间迭代会让该线程在全局 Collector 里长期 pinned（回忆 u2-l6：长时间 pin 会拖慢垃圾回收）。
- **`RefIter`/`RefRange`（refcount）**：不存 Guard，只存两个 `RefEntry`（[src/base.rs:1877-1881](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1877-L1881)、[2095-2106](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L2095-L2106)）。每一步用调用方临时传入的 `&Guard` 完成搜索，靠 `RefEntry` 的引用计数保住游标节点。**优点是迭代器可以跨多个短命 Guard 长期存在**（甚至存进结构体、跨函数返回），线程不必一直 pin；**代价是每步要做引用计数加减（CAS）**，且因为签名多了 `&Guard`，无法直接 `impl Iterator`。

> 🔑 **关键洞察**：高层 `SkipMap::iter()` 返回的 `map::Iter` **内部包的是 `base::RefIter` 而不是 `base::Iter`**（见 [src/map.rs:717-731](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L717-L731)）。原因正是人体工学：高层 API 不希望用户在迭代期间把线程一直 pin 住，于是在每个 `next()` 里临时 `epoch::pin()` 一次，把 refcount 迭代器适配成标准 `Iterator`。换句话说，**`base::Iter` 反而是个「专家直连」的低层入口**，普通用户基本用不到。

#### 4.1.2 核心流程：双游标如何实现双向 + 提前终止

五种迭代器（除 `IntoIter` 外）都用同一个 `head`/`tail` 双游标骨架：

```
构造：head = None, tail = None          # 都还没起步
next()     → 把 head 推进到下一个更大元素；若 head 越过 tail，清空两个游标，返回 None
next_back()→ 把 tail 推进到下一个更小元素；若 tail 越过 head，清空两个游标，返回 None
```

设某时刻 `head` 指向键 \(h\)、`tail` 指向键 \(t\)（均已不是 None），则「相遇/越过」的判定为：

\[
\text{stop} \iff \mathrm{compare}(h, t) \in \{\text{Greater}, \text{Equal}\}
\]

即源码里的 `compare(h, t).is_ge()`。为什么用 `is_ge()`（含等号）而不是 `is_gt()`？因为当 `head` 与 `tail` 指向**同一个节点**时（\(h = t\)），那个节点已经被另一端取走过一次了——若再用 `is_gt()`（严格大于），`next()` 会把它再返回一次，造成重复；用 `is_ge()` 在 \(h = t\) 时就立即停止，恰好避免重复。这就是 4.2.4 实践题的答案。

#### 4.1.3 源码精读：四个构造器

四个构造器都很薄，只是把 `head`/`tail` 初始化为 `None`。注意 guard-bound 的两个还多存了 `guard`，refcount 的两个则没有：

- `SkipList::iter`：返回 guard-bound 的 `Iter`，存 `guard`（[src/base.rs:649-657](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L649-L657)）。
- `SkipList::ref_iter`：返回 refcount 的 `RefIter`，**不存 guard**（[src/base.rs:660-666](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L660-L666)）。
- `SkipList::range`：返回 guard-bound 的 `Range`，额外存 `range: R` 与 `PhantomData<fn() -> Q>`（[src/base.rs:669-688](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L669-L688)）。`PhantomData<fn() -> Q>` 让 `Q` 协变，方便用 `&str` 范围查 `String` 键（u2-l7）。
- `SkipList::ref_range`：返回 refcount 的 `RefRange`（[src/base.rs:692-705](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L692-L705)）。

四个构造器都不立即定位首元素——`head`/`tail` 起步为 `None`，真正的定位发生在第一次 `next`/`next_back` 时（懒求值）。

#### 4.1.4 代码实践：辨认同类

1. **目标**：用类型签名分辨「哪个迭代器需要 Guard、哪个不需要」。
2. **步骤**：阅读 `src/base.rs` 中 `Iter`、`RefIter`、`Range`、`RefRange` 四个结构体的字段定义，再阅读 `src/map.rs:717` 处 `map::Iter` 的字段。
3. **观察**：`Iter`/`Range` 有 `guard: &'g Guard` 字段；`RefIter`/`RefRange` 没有；`map::Iter` 只有一个 `inner: base::RefIter` 字段。
4. **预期结果**：能口头复述「高层 `SkipMap::iter()` 用的其实是 refcount 那一套」。

#### 4.1.5 小练习与答案

**Q1**：为什么 `base::RefIter` 不实现标准 `Iterator` trait，而 `base::Iter` 实现？

**答**：标准 `Iterator::next(&mut self)` 的签名里没有 `&Guard` 参数。`Iter` 把 Guard 存进结构体，`next` 内部直接用 `self.guard`，能匹配标准签名；`RefIter` 故意不存 Guard（为了让线程不必长期 pin），每步需要的 Guard 必须由调用方现传，于是签名变成 `next(&mut self, guard: &Guard)`，与标准 trait 不兼容。

**Q2**：如果高层 `map::Iter` 改成包 `base::Iter` 而不是 `base::RefIter`，会有什么问题？

**答**：那么 `map::Iter` 必须在构造时 `epoch::pin()` 一次并让该 Guard 活到迭代器 drop。整个 `for e in map.iter() { ... }` 期间线程都被 pin 在某个 epoch，长时间遍历会阻塞全局 Collector 推进垃圾回收（u2-l6 讲过的「长时间 pin 代价」）。

---

### 4.2 Iter 与 Range：guard-bound 标准双端迭代器

#### 4.2.1 概念说明

`Iter` 是「遍历全表」的 guard-bound 迭代器，`Range` 是「遍历某个区间」的 guard-bound 迭代器。两者都实现标准 `Iterator` + `DoubleEndedIterator`，因此可以直接喂给 `for` 循环、`.rev()`、`.collect()`。

二者的差异只有一处：**终止边界从何而来**。
- `Iter`：唯一的边界就是「另一端游标」（双向相遇时停）。
- `Range`：除了另一端游标，还有用户传入的 `range: R`（`RangeBounds`），需要在每一步检查「当前元素是否仍在区间内」。

#### 4.2.2 核心流程：Iter::next / next_back

```
Iter::next(&mut self):
  new_head = match head:
      Some(n) → next_node(n, Excluded(n.key), guard)   # 沿 level-0 走到下一个更大节点
      None    → next_node(head_node, Unbounded, guard) # 第一次：从虚拟头节点走一步
  if new_head 与 tail 都存在 且 compare(new_head.key, tail.key).is_ge():
      head=None; tail=None    # 相遇/越过，提前终止
  else:
      head = new_head         # 直接覆盖旧 head（guard-bound，无需引用计数）
  返回 new_head 对应的 Entry

Iter::next_back(&mut self):
  new_tail = match tail:
      Some(n) → search_bound(Excluded(n.key), upper_bound=true, guard)  # 反向靠按键搜索
      None    → search_bound(Unbounded, upper_bound=true, guard)        # 第一次：定位最大元素
  同样的相遇判定（compare(head.key, new_tail.key).is_ge()）
  覆盖 tail，返回 Entry
```

注意两点：(1) **正向走用 `next_node`，反向走用 `search_bound`**，这是因为跳表只有后继指针、没有前驱指针，回头必须靠搜索（u3-l8 已讲过 `search_bound` 的 `upper_bound=true` 分支找「最后一个 ≤ key 的节点」）。(2) `Iter` 覆盖旧游标时**什么都不做**——因为游标是 guard-bound 的 `NodeRef`，不带引用计数，Guard 活着节点就不会被回收。

`Range` 的流程几乎一致，只是把「另一端游标」与「区间端点」合并成一个上界/下界检查（见 4.2.3）。

#### 4.2.3 源码精读

**Iter 的双游标相遇**（[src/base.rs:1812-1833](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1812-L1833)）：

```rust
fn next(&mut self) -> Option<Self::Item> {
    self.head = match self.head {
        Some(n) => self.parent.next_node(n.as_tower(), Bound::Excluded(&n.key), self.guard),
        None => self.parent.next_node(self.parent.head.as_tower(), Bound::Unbounded, self.guard),
    };
    if let (Some(h), Some(t)) = (self.head, self.tail) {
        if self.parent.comparator.compare(&h.key, &t.key).is_ge() {  // ← 含等号
            self.head = None;
            self.tail = None;
        }
    }
    self.head.map(|n| Entry { parent: self.parent, node: n, guard: self.guard })
}
```

旁注：`Ordering::is_ge()` 是标准库在 `core::cmp::Ordering` 上的方法，当结果为 `Greater` 或 `Equal` 时返回 `true`。这里用 `is_ge()` 而非 `is_gt()` 正是 4.1.2 解释的「相遇即停、避免重复」。

**Iter::next_back 用 search_bound 反向**（[src/base.rs:1840-1861](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1840-L1861)）：注意 `next_back` 在 `None` 分支用 `search_bound::<K>(Bound::Unbounded, true, ...)`，`true` 表示取「上界方向」即最大元素。

**Range::next 的区间裁剪**（[src/base.rs:2001-2033](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L2001-L2033)）：

```rust
fn next(&mut self) -> Option<Self::Item> {
    self.head = match self.head {
        Some(n) => self.parent.next_node(n.as_tower(), Bound::Excluded(&n.key), self.guard),
        None => self.parent.search_bound(self.range.start_bound(), false, self.guard),
    };
    if let Some(h) = self.head {
        let bound = match self.tail {
            Some(t) => Bound::Excluded(&t.as_ref().key),  // 已有尾游标：以它为上界
            None => self.range.end_bound(),               // 还没反向走过：以区间上界为准
        };
        if !below_upper_bound(&self.parent.comparator, &bound, &h.key) {
            self.head = None; self.tail = None;           // 越过上界，终止
        }
    }
    self.head.map(|n| Entry { /* .. */ })
}
```

关键设计：`Range` 的上界是「尾游标」与「区间 `end_bound`」二选一的动态边界。如果用户已经从右端 `next_back` 过（`tail` 已存在），就以尾游标为上界（相遇判定）；否则用用户给的区间上界。`below_upper_bound` 把 `Bound` 翻译成 `compare` 的真值（详见 4.5）。

#### 4.2.4 代码实践：对比 next 与 next_back，写一个反向 range 测试

1. **目标**：亲手验证 `Iter::next` 与 `next_back` 的实现差异，并体会 `is_ge()` 的作用。
2. **操作步骤**（在仓库根目录新增一个临时示例 `examples/iter_demo.rs`，**注意这是示例代码，不是项目原有文件**）：

   ```rust
   // 示例代码：演示 base 层迭代器的双向行为（需通过高层 SkipMap 间接验证）
   use crossbeam_skiplist::SkipMap;

   fn main() {
       let m: SkipMap<i32, i32> = (1..=5).map(|i| (i, i * 10)).collect();

       // 1) 正向
       let fwd: Vec<i32> = m.iter().map(|e| *e.key()).collect();
       assert_eq!(fwd, vec![1, 2, 3, 4, 5]);

       // 2) range(start..end).rev() 反向遍历 [2, 4)
       let rev: Vec<i32> = m.range(2..4).rev().map(|e| *e.key()).collect();
       assert_eq!(rev, vec![3, 2]);

       // 3) 双向交替：next 取 1, next_back 取 5, next 取 2, next_back 取 4, next 取 3 后应停止
       let mut it = m.iter();
       assert_eq!(it.next().map(|e| *e.key()), Some(1));
       assert_eq!(it.next_back().map(|e| *e.key()), Some(5));
       assert_eq!(it.next().map(|e| *e.key()), Some(2));
       assert_eq!(it.next_back().map(|e| *e.key()), Some(4));
       assert_eq!(it.next().map(|e| *e.key()), None); // ← head 追上 tail，is_ge() 触发，返回 None
   }
   ```
   在 `crossbeam-skiplist/` 目录下运行（**待本地验证**）：
   ```bash
   cargo run --release --example iter_demo
   ```
3. **观察现象**：第 3 组断言里，`next()` 在 `head` 推进到 3 之后，与 `tail`（指向 4）相比，`compare(3,4)` 是 `Less`，不触发终止——返回 `Some(3)`。下一次 `head` 会推进到 4，此时 `compare(4,4).is_ge()` 为真，提前终止返回 `None`。如果断言里期望 `Some(3)` 处写成了 `Some(4)`，你会看到测试失败。
4. **预期结果**：三组 `assert_eq!` 全部通过，证明 (a) `next` 沿 level-0 正向、`next_back` 靠搜索反向；(b) 双向游标相遇时**不会重复**返回中间元素。
5. **运行结果**：待本地验证（本讲未实际执行）。

#### 4.2.5 小练习与答案

**Q1**：把 `Iter::next` 里的 `is_ge()` 改成 `is_gt()`，对上面第 3 组交替测试会有什么影响？

**答**：当 `head` 与 `tail` 都指向同一个节点（例如元素 3）时，`compare(3,3)` 是 `Equal`，`is_gt()` 返回 `false`，于是不会提前终止，`next()` 会把 3 再返回一次——产生重复元素。所以必须用 `is_ge()`（含等号）。

**Q2**：为什么 `Iter::next` 直接 `self.head = ...` 覆盖旧 head，而 `RefIter::next` 要先 `mem::replace` 再 `decrement`？

**答**：`Iter` 的游标是 guard-bound 的 `NodeRef`，不带引用计数，覆盖旧值不需要任何清理（Guard 还活着 → 节点不会被回收）。`RefIter` 的游标是 `RefEntry`，每存一个就占了一份引用计数，替换前必须把旧的减回去，否则会泄漏计数（节点永不回收）。

---

### 4.3 RefIter 与 RefRange：refcount 迭代器

#### 4.3.1 概念说明

`RefIter`/`RefRange` 是 refcount 版本的「全表 / 区间」迭代器。它们与 `Iter`/`Range` 行为对称，但有三处关键不同：

1. **不实现标准 `Iterator`**：`next`/`next_back` 多一个 `&Guard` 参数，因为每步都要现 pin。
2. **游标是 `RefEntry`**：持有引用计数，迭代器可以跨 Guard 存活；相应地，每步要做引用计数维护。
3. **必须显式释放**：游标持有的引用计数不会自动归还（`RefEntry` 没有 `Drop`，回忆 u4-l12「必须手动 release」），因此提供 `drop_impl(&Guard)` 在迭代结束时把 `head`/`tail` 的计数减掉。

#### 4.3.2 核心流程：RefIter::next 的引用计数账本

```
RefIter::next(&mut self, guard):
  next_head = match head:
      Some(e) → e.next(guard)                          # RefEntry::next 内部已 loop try_acquire
      None    → try_pin_loop(|| parent.front(guard))   # 第一次定位要循环重试 pin
  match (next_head, tail):
      (Some(next), Some(t)) 且 compare(next.key, t.key).is_ge():
          next.node.decrement(guard)   # 相遇：把刚拿到的 next 计数退掉，返回 None
          → None
      (Some(_), _):
          用 next_head.clone() 替换 head（clone 加一）
          把旧 head decrement（减一）   # 净效果：旧游标释放，新游标 + 返回值各占一份
          → next_head
      (None, _) → None
```

引用计数账本：每一步前进时，**新节点同时被「游标」与「返回给调用方的 `RefEntry`」各持有一份**（所以净 +1，由旧游标释放的 −1 与 `try_acquire` 的 +1 与 `clone` 的 +1 之间的差额平衡）。这套加减法是无锁迭代器的「代价」。

`drop_impl` 把残留在 `head`/`tail` 上的两份计数退掉，否则节点永不回收（即内存泄漏）。

#### 4.3.3 源码精读

**RefIter::next**（[src/base.rs:1907-1933](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1907-L1933)）：注意相遇分支里 `unsafe { next.node.decrement(guard); }`——刚 `try_acquire` 到的 `next` 因为要被丢弃，必须立刻减计数。正常分支用 `mem::replace(&mut self.head, next_head.clone())` 取出旧 head 并 `decrement`。

**RefIter::drop_impl**（[src/base.rs:1967-1975](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1967-L1975)）：把 `head` 与 `tail` 各 `take` 出来 `decrement`。高层 `map::Iter` 的 `Drop`（[src/map.rs:749-754](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L749-L754)）正是 `epoch::pin()` 后调用这个 `drop_impl`。

**RefRange::next**（[src/base.rs:2148-2193](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L2148-L2193)）：结构与 `RefIter::next` 对称，只是把相遇判定换成 `below_upper_bound`（动态上界 = 尾游标或区间上界，同 `Range`）。注意它写成「满足上界才替换 head 并返回，否则 decrement 掉 next 返回 None」的正面分支写法，与 `RefIter::next` 的「先算 next 再分支」等价但顺序不同。

**RefRange::next_back**（[src/base.rs:2196-2241](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L2196-L2241)）与 **RefRange::drop_impl**（[src/base.rs:2244-2252](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L2244-L2252)）同理。

> 旁注：`RefRange` 显式 `unsafe impl Send/Sync`（[src/base.rs:2108-2122](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L2108-L2122)），因为 `RefEntry` 内含裸指针，编译器不会自动推导 `Send/Sync`，但逻辑上引用计数让它是线程安全的。

#### 4.3.4 代码实践：观察「忘记 drop_impl 会泄漏」

1. **目标**：体会 refcount 迭代器必须显式释放的约束。
2. **步骤**：阅读 `tests/map.rs` 中的 `next_memory_leak` / `next_back_memory_leak` / `range_next_memory_leak` 三个用例（[tests/map.rs:192-224](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/tests/map.rs#L192-L224)）。它们构造单元素 map，取一个元素后立刻验证另一端返回 `None`，再 `remove` 该元素。
3. **观察**：这些测试关心的不是「能不能取到」，而是「取完之后内部 `head`/`tail` 游标的引用计数是否被正确退掉」。如果 `drop_impl` 漏掉某个分支，节点计数不为 0，`remove` 后节点不会被回收——在 Miri 或泄漏检测下会暴露。
4. **预期结果**：用 `cargo +nightly miri test next_memory_leak` 等三个用例应全部通过、无泄漏报告。**待本地验证**。

#### 4.3.5 小练习与答案

**Q1**：为什么 `RefIter::next` 在「相遇」分支里要 `next.node.decrement(guard)`，而 `Iter::next` 的相遇分支什么都不做？

**答**：`RefIter` 的 `next_head` 是刚通过 `try_acquire`/`clone` 拿到的新 `RefEntry`，占了一份计数；既然要丢弃它返回 `None`，就必须把这份计数退掉，否则泄漏。`Iter` 的 `head` 是 guard-bound `NodeRef`，不带计数，无需任何退计数操作。

**Q2**：`RefIter` 第一次调用 `next` 时为什么用 `try_pin_loop(|| self.parent.front(guard))`，而后续步用 `e.next(guard)`？

**答**：`front(guard)` 返回的是 guard-bound 的 `Entry`，要变成 `RefEntry` 必须调 `Entry::pin()`（即 `try_acquire`）。在找到 entry 与 pin 之间，节点可能被并发删除导致 pin 失败返回 `None`；`try_pin_loop` 就是为了在这种竞态下重试。而 `RefEntry::next`（[src/base.rs:1755-1768](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1755-L1768)）内部已经 `loop` 直到 `try_acquire` 成功，所以后续步不必再套 `try_pin_loop`。

---

### 4.4 IntoIter：独占访问下的 owning 迭代器

#### 4.4.1 概念说明

`IntoIter` 是唯一的 **owning**（拥有式）迭代器：它由 `SkipList` 的 `IntoIterator` 得到，会**消费整个跳表**——边遍历边释放节点，遍历完跳表也就空了。它有三个特点：

1. **只正向、不双向**：只实现 `Iterator`，没有 `next_back`。
2. **不需要 Guard、不做引用计数**：因为跳表已被独占消费，没有其他线程竞争，可以用 `epoch::unprotected()` 直接操作。
3. **`Item = (K, V)`**：直接返回键值的所有权，而不是 `Entry` 句柄。

#### 4.4.2 核心流程

```
into_iter(self):
  unprotected 加载 head 的 level-0 后继 → front 原始指针
  把 head 的所有层指针清空（断开跳表与节点的链接）
  构造 IntoIter { node: front }

IntoIter::next():
  loop:
    if self.node == null → 返回 None
    key   = ptr::read(&node.key)      # 把 key 移出（不析构，因为是 move）
    value = ptr::read(&node.value)
    next  = load level-0(unprotected)
    Node::dealloc(node)               # 仅还内存（不析构 key/value，它们已被 ptr::read 搬走）
    self.node = next
    if next.tag() == 0  → 返回 Some((key, value))   # 活节点
    else                → 跳过（被逻辑删除的死节点，丢弃其 key/value）

Drop for IntoIter: 遍历剩余链，对每个节点 Node::finalize（先析构 key/value 再 dealloc）
```

两个关键区分（连接 u2-l5）：`next` 用 `dealloc`（只还内存，因为 key/value 已被 `ptr::read` 搬走）；`Drop` 用 `finalize`（对**未被访问过**的残留节点，需要先析构键值再还内存）。还有一个细节：`next` 会**跳过 `next.tag()==1` 的死节点**——即使消费时跳表里残留着被逻辑删除但还没物理摘除的节点，`IntoIter` 也不会把它们当作有效元素吐出来。

#### 4.4.3 源码精读

**IntoIterator for SkipList**（[src/base.rs:1449-1477](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1449-L1477)）：注释明确写了 `Unprotected loads are okay because this function is the only one currently using the skip list.`——这是后续所有 unprotected 操作的安全性依据。它还把 head 各层指针 store 为 null，等于把跳表「清空」，从此原 `SkipList` 不再持有任何节点。

**IntoIter::next**（[src/base.rs:2285-2322](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L2285-L2322)）：`ptr::read` 移走键值 → `Node::dealloc` → 根据 `next.tag()` 决定返回还是跳过。

**Drop for IntoIter**（[src/base.rs:2263-2283](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L2263-L2283)）：对未消费完的节点（例如 `for` 循环提前 break）调 `Node::finalize` 兜底析构。注意它用 `NodeRef::new(node).get_level(0).load(Relaxed, unprotected())` 取下一节点——`Relaxed` 足矣，因为独占访问下没有跨线程同步需求（u5-l17 会专题讨论）。

#### 4.4.4 代码实践：跟踪 owning 迭代的内存回收

1. **目标**：理解 `IntoIter` 与 `Drop for SkipList`/`clear` 在回收路径上的区别。
2. **步骤**：在 `src/base.rs` 内分别阅读 `IntoIter::next`（行 2285）、`Drop for IntoIter`（行 2263）与 `Drop for SkipList`（在 u3-l11 已讲）。整理一张表：每个入口对「活节点」「死节点（tag=1）」「被搬空节点」分别用 `finalize` 还是 `dealloc`。
3. **观察**：`IntoIter::next` 对当前节点用 `dealloc`（因为键值已被 `ptr::read` 搬走，无需再析构）；`Drop for IntoIter` 对残留节点用 `finalize`（这些节点的键值还在，需要析构）。
4. **预期结果**：能解释「为什么 next 用 dealloc 而 drop 用 finalize」——核心是 `ptr::read` 是否已经把键值移走。

#### 4.4.5 小练习与答案

**Q1**：`IntoIter::next` 里 `if next.tag() == 0` 的判断在做什么？为什么需要它？

**答**：判断下一个节点是否是活节点。`next.tag()==1` 表示该节点已被逻辑删除（Harris/Michael tag，u3-l9）。消费整个跳表时，可能存在「已被 mark 但尚未被读路径 help_unlink 物理摘除」的死节点残留在 level-0 链上；`IntoIter` 用此判断跳过它们，不把死节点的键值吐给调用方。

**Q2**：为什么 `IntoIter` 敢用 `epoch::unprotected()`，而 `Iter` 必须用真实 `Guard`？

**答**：`IntoIter` 由 `into_iter(self)` 消费整个 `SkipList`，此时已无其他引用、无并发线程，节点不可能在它操作时被回收，因此 unprotected（无 Guard）加载是安全的。`Iter` 是借用的，迭代期间跳表仍可被其他线程并发读写，必须靠 Guard 保护临界区内加载的指针。

---

### 4.5 try_pin_loop 与边界辅助函数

#### 4.5.1 概念说明

最后一块拼图是两个小工具：

- **`try_pin_loop`**：把「找一个 `Entry` 再 `pin()` 成 `RefEntry`」的竞态重试封装成循环。用于 refcount 迭代器的**首次定位**（`front`/`back`/`lower_bound`/`upper_bound`）。
- **`above_lower_bound` / `below_upper_bound`**：把标准库的 `Bound<&T>`（`Included`/`Excluded`/`Unbounded`）翻译成 `Comparator::compare` 返回的真值，供 `Range`/`RefRange` 做区间裁剪。

#### 4.5.2 核心流程

`try_pin_loop` 逻辑极简：

```
loop:
    e = f()?            # f 返回 Option<Entry>；None 直接整体返回 None
    if let Some(re) = e.pin():   # pin 成功
        return Some(re)
    # pin 失败（节点刚被并发回收）→ 重试，重新调 f 再找一个
```

边界辅助函数（把 `Bound` 与 `compare` 的 `Ordering` 组合成布尔）：

| 函数 | Included(key) | Excluded(key) | Unbounded |
| --- | --- | --- | --- |
| `above_lower_bound(x)` | `compare(x, key).is_ge()`（x ≥ key） | `compare(x, key).is_gt()`（x > key） | `true` |
| `below_upper_bound(x)` | `compare(x, key).is_le()`（x ≤ key） | `compare(x, key).is_lt()`（x < key） | `true` |

这两个函数其实是对 u3-l8 讲过的「`Bound` 真值表」的复用：`Range::next`/`RefRange::next` 检查上界用 `below_upper_bound`，`Range::next_back`/`RefRange::next_back` 检查下界用 `above_lower_bound`。

#### 4.5.3 源码精读

**try_pin_loop**（[src/base.rs:2332-2341](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L2332-L2341)）：闭包 `F: FnMut() -> Option<Entry>`，循环里 `f()?.pin()`。`?` 在 `f()` 返回 `None` 时直接整体返回 `None`（区间为空等情况）；`pin()` 返回 `None` 时落空进入下一轮重试。

**above_lower_bound**（[src/base.rs:2344-2354](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L2344-L2354)）与 **below_upper_bound**（[src/base.rs:2357-2367](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L2357-L2367)）：两个 `match` 把三种 `Bound` 映射成 `Ordering` 的 `is_ge/is_gt/is_le/is_lt`，注意 `Included` 带等号、`Excluded` 不带。

#### 4.5.4 代码实践：用自定义比较器验证区间裁剪

1. **目标**：感受 `above_lower_bound`/`below_upper_bound` 如何依赖 `Comparator`，并验证自定义比较器下区间语义仍正确。
2. **步骤**：参考 u2-l7 的「大小写不敏感比较器」，构造一个 `SkipMap<String, i32>` 并用 `range("abc"..="abd")` 取子集。
3. **观察**：迭代结果完全由比较器决定，`below_upper_bound` 内部调用的就是 `Comparator::compare`。
4. **预期结果**：区间端点的 `Included`/`Excluded` 与 `is_le`/`is_lt` 一一对应。**待本地验证**。

#### 4.5.5 小练习与答案

**Q1**：`try_pin_loop` 里 `f()?.pin()` 的 `?` 作用在哪个 `Option` 上？什么情况下触发？

**答**：作用在外层 `f()` 返回的 `Option<Entry>` 上。当 `f()` 返回 `None`（例如 `front` 在空跳表上返回 `None`、或区间内没有任何元素）时，`?` 让 `try_pin_loop` 整体直接返回 `None`，不再循环。

**Q2**：`below_upper_bound` 对 `Bound::Excluded(key)` 用 `is_lt()`（严格小于）。如果误写成 `is_le()`（含等号），`range(2..4)` 会多吐还是少吐一个元素？

**答**：会**多吐**一个等于上界 4 的元素（如果存在）。`Excluded(4)` 本应排除 4，写 `is_le()` 会把 `compare(x,4)==Equal` 也判为「在上界内」，从而错误地包含 4。

---

## 5. 综合实践

把本讲全部知识点串起来，完成下面这个「双向迭代器行为画像」小任务。

**任务**：在 `crossbeam-skiplist/` 下新建临时示例 `examples/iter_profiling.rs`（**示例代码，非项目原有文件**），完成以下三件事并在注释里作答：

1. **正向 vs 反向**：构造 `SkipMap<i32, i32>`，插入 `1..=6`。用 `m.iter()` 正向收集，用 `m.range(..).rev()` 反向收集，验证两者互为逆序。
2. **双向相遇**：对同一个 `m.iter()`，交替调用 `next()` 与 `next_back()` 各 3 次，记录返回的 key 序列；在注释里画出 `head`/`tail` 游标的位置变化，并指出在哪一步 `compare(h,t).is_ge()` 触发了提前终止。
3. **owning 消费**：用 `for (k, v) in m.into_iter() { ... }` 消费跳表，验证返回的是 `(K, V)` 而非 `Entry`；在注释里说明为什么这里能用 `unprotected()` 而普通 `iter()` 不能。

参考骨架：

```rust
// 示例代码
use crossbeam_skiplist::SkipMap;

fn main() {
    let m: SkipMap<i32, i32> = (1..=6).map(|i| (i, i * 10)).collect();

    // 1) 正向 / 反向
    let fwd: Vec<_> = m.iter().map(|e| *e.key()).collect();
    let rev: Vec<_> = m.range(..).rev().map(|e| *e.key()).collect();
    assert_eq!(fwd, vec![1, 2, 3, 4, 5, 6]);
    assert_eq!(rev, vec![6, 5, 4, 3, 2, 1]);

    // 2) 双向相遇：head 从左、tail 从右推进
    let mut it = m.iter();
    println!("{:?}", it.next());         // head=1
    println!("{:?}", it.next_back());    // tail=6
    println!("{:?}", it.next());         // head=2
    println!("{:?}", it.next_back());    // tail=5
    println!("{:?}", it.next());         // head=3 → 与 tail=5 未相遇
    println!("{:?}", it.next());         // head=4 → compare(4,5) 仍未相遇，返回 4
    println!("{:?}", it.next());         // head=5 == tail=5 → is_ge() 触发，返回 None

    // 3) owning 消费
    let m2: SkipMap<i32, i32> = (1..=3).map(|i| (i, i)).collect();
    let owned: Vec<(i32, i32)> = m2.into_iter().collect();   // Item = (K, V)
    assert_eq!(owned, vec![(1, 1), (2, 2), (3, 3)]);
    // 注：into_iter 消费 m2 后无并发访问，可用 unprotected；iter() 是借用，迭代期间
    // 仍有并发读写，必须用真实 Guard。
}
```

运行（**待本地验证**）：

```bash
cargo run --release --example iter_profiling
```

完成后再回答：第 2 步里，如果把 `is_ge()` 换成 `is_gt()`，哪一次 `next()` 会返回**重复**的 key？为什么？

> 参考答案：当 `head` 与 `tail` 都指向 5 时（最后一次 `next()`），`compare(5,5)` 为 `Equal`，`is_gt()` 返回 `false` 不会终止，于是 `next()` 会返回 `Some(5)`——而 5 已经被之前的 `next_back()` 返回过，造成重复。这正是源码用 `is_ge()` 的原因。

## 6. 本讲小结

- base 层有 **五种**迭代器：guard-bound 的 `Iter`/`Range`、refcount 的 `RefIter`/`RefRange`、owning 的 `IntoIter`。
- **guard-bound vs refcount** 是核心分野：前者存 `&Guard`、实现标准 `Iterator`、每步便宜但线程长期 pin；后者不存 Guard、每步现 pin、靠引用计数跨 Guard 存活，但不实现标准 `Iterator` 且需手动 `drop_impl` 释放计数。
- 高层 `SkipMap::iter()` 内部包的是 **`base::RefIter`**（[src/map.rs:717-731](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L717-L731)），从而避免长期 pin。
- 五种都用 **`head`/`tail` 双游标**实现双向遍历与提前终止；相遇判定用 `compare(h,t).is_ge()`（含等号），保证中间元素不被两端重复返回。
- `next` 用 `next_node`（沿 level-0 正向），`next_back` 用 `search_bound(..., upper_bound=true)`（按键搜索反向）——因为跳表只有后继指针。
- `IntoIter` 在独占访问下用 `epoch::unprotected()` 安全遍历：`next` 用 `ptr::read`+`dealloc` 搬走键值，`Drop` 用 `finalize` 兜底析构残留节点，并跳过 `tag==1` 的死节点。

## 7. 下一步学习建议

- 本讲出现的 `Ordering::Relaxed`/`Release`/`Acquire`/`SeqCst` 与 `unprotected()` 的安全性条件，将在 **u5-l17 内存序分析** 专题展开——届时可带着「`IntoIter` 为何全用 Relaxed」「`mark_tower` 为何用 SeqCst」的问题重读本讲。
- refcount 迭代器的 `decrement` 与 `try_pin_loop` 背后的引用计数 + epoch 回收协议，在 **u2-l6** 已建立，可对照重读 `RefEntry::try_acquire`/`release`。
- 想看这五种迭代器如何被高层 `SkipMap`/`SkipSet` 对外暴露并隐藏 Guard，进入 **u4-l14（SkipMap 高层封装）**。
- 若关心「迭代期间并发修改会发生什么」「迭代器返回的 `Entry` 是否可能立刻 `is_removed()`」，进入 **u5-l18（并发语义与 Drop/IntoIter 的安全性）**。
