# 并发语义与 Drop/IntoIter 的安全性

## 1. 本讲目标

读到这里，你已经读完了 `base.rs` 的搜索、标记、插入、删除主链路，也读过了 `map.rs`/`set.rs` 的高层封装。本讲不再引入新的算法，而是退一步，把贯穿全册的两条「承诺」讲清楚：

1. **并发语义的边界**——`SkipMap`/`SkipSet` 到底原子到哪里、又在哪里「不原子」？所谓的「竞态（race condition）」和「数据竞争（data race）」有什么本质区别？
2. **`unsafe` 的最后一块拼图**——`Drop for SkipList` 和 `IntoIter` 为什么敢用 `epoch::unprotected()` 直接解引用并发链表上的指针？

学完后你应该能：

- 准确区分**数据竞争**（违反 Rust 内存安全，UB）与**竞态**（逻辑错误，但不会段错误）。
- 说清「单操作原子、多操作非原子」这条保证在源码里对应哪几个原子操作。
- 解释 `is_removed()` 为什么只是「尽力而为的观测」，以及删除的安全性为什么不依赖它。
- 论证 `Drop`/`IntoIter` 使用 `unprotected()` 的前置条件是「独占访问」，并指出代码里哪些注释把这一前提写给了读者。

---

## 2. 前置知识

本讲假设你已经掌握以下概念（前面讲义已建立）：

- **跳表与概率性数据结构**（u1-l1）：随机高度决定塔高，期望 O(log n)。
- **epoch-based reclamation**（u2-l6）：被摘除的节点不会立即释放，要等 epoch 推进且没有线程持有 `Guard`/引用计数后，由 `finalize` 延迟回收。
- **标记指针与逻辑删除**（u3-l9）：`mark_tower` 自顶向下给指针 `fetch_or(1)` 打 tag，**level 0 的 tag 由 0 翻 1 是删除的线性化点**；`is_removed` 只看 level 0。
- **Entry / RefEntry 双生命周期句柄**（u4-l12）：`Entry<'a,'g,..>` 绑定 `Guard`，`RefEntry<'a,..>` 绑定 `SkipList` 并靠引用计数跨 Guard 存活。

本讲新引入的术语：

- **数据竞争（data race）**：多线程在没有同步的情况下访问同一内存且至少一个是写，Rust 的类型系统（`Send`/`Sync`）和原子操作负责消灭它。结果是**未定义行为（UB）**，可能段错误、内存损坏。
- **竞态（race condition）**：代码的**正确性**依赖时序，但每次访问本身都是内存安全的。结果是**逻辑错误**，比如 `insert` 之后 `contains` 却查不到。
- **线性化点（linearization point）**：一个并发操作在时间轴上「真正生效」的那一瞬间。对跳表而言，删除的线性化点是 level 0 指针 tag 的 0→1 翻转。
- **独占访问（exclusive access）**：某一时刻保证没有其他线程在访问该数据结构，这是 `unprotected()` 之所以安全的根本前提。

---

## 3. 本讲源码地图

| 文件 | 本讲聚焦 | 作用 |
| --- | --- | --- |
| `src/lib.rs` | 「Concurrent access」章节（L8–L84） | 用文档与 doctest 把「单操作原子、多操作非原子」的语义写进 crate 顶层文档 |
| `src/base.rs` | `NodeRef::is_removed`（L350–L361）、`Entry::is_removed`（L1491–L1494）、`RefEntry::is_removed`（L1633–L1636） | 逻辑删除的「观测口」 |
| `src/base.rs` | `Drop for SkipList`（L1413–L1437）、`IntoIterator for SkipList`（L1449–L1477） | 销毁/消费时用 `unprotected()` 直接释放整链 |
| `src/base.rs` | `IntoIter` 结构体与其 `Drop`/`Iterator`（L2255–L2322） | 独占消费迭代器，`ptr::read` 搬走键值、`dealloc` 仅回收内存 |

---

## 4. 核心概念与源码讲解

### 4.1 单操作原子、多操作非原子

#### 4.1.1 概念说明

这是 crossbeam-skiplist 给用户的**第一条契约**，写在 `lib.rs` 顶层文档里。它的含义可以拆成两句：

- **单操作原子**：`insert`、`get`、`remove`、`contains(_key)` 这些**单个方法调用**，要么完整发生、要么完全不发生，多线程同时调用不会「互相穿插」导致数据结构损坏。这是靠无锁算法（CAS + 标记指针）保证的，背后是内存安全。
- **多操作非原子**：把两个方法**连起来**调用，中间**没有**任何原子性保证。在线程 A 执行 `insert(k, v)` 之后、执行 `contains(k)` 之前的这个缝隙里，线程 B 完全可以执行 `remove(k)`，于是 A 的 `contains` 就可能返回 `false`。

关键直觉：跳表的原子性粒度是「**一次方法调用**」，不是「**一段代码**」。Rust 的借用检查帮不了你跨调用，因为这里根本没有借用——方法是 `&self`。

#### 4.1.2 核心流程

用「时间线」来看 lib.rs 里的竞态 doctest（[src/lib.rs:L49-L68](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L49-L68)）：

```
主线程:                  remove 线程:
                         numbers.remove(&5)   // 待执行
numbers.insert(5)        ↕ 可能穿插
numbers.contains(&5)     ↕ 可能穿插
                         numbers.remove(&5)   // 真正执行，标记 5
                         → 此时 contains 可能返回 false
```

`insert` 和 `contains` 之间没有任何锁把它们包成一个原子事务，所以两者之间插不插得进一个 `remove`，完全由线程调度决定——这就是「非确定性」。lib.rs 在 [src/lib.rs:L70-L73](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L70-L73) 用一句话点明：

> a _single_ operation on the map, such as `insert`, operates atomically ... However, concurrent calls to functions can become interleaved across threads, introducing non-determinism.

#### 4.1.3 源码精读

文档里这个 `no_run` 的 doctest 就是「竞态」的最小可复现样本：

```rust
// [src/lib.rs:L49-L68] 竞态示例
let numbers = SkipSet::new();
scope(|s| {
    s.spawn(|_| {
        numbers.remove(&5);          // 线程 B：删除 5
    });
    numbers.insert(5);               // 主线程：插入 5
    assert!(numbers.contains(&5));   // ← 这个断言可能失败！
}).unwrap();
```

注意它标了 `no_run`——因为结果依赖时序，作为 doctest 会偶发失败，不能放进 CI。文档紧接着在 [src/lib.rs:L82-L84](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L82-L84) 给出了**最关键的一句话区分**：

> Note that race conditions do not violate Rust's memory safety rules. A race between multiple threads can never cause memory errors or segfaults. A race condition is a _logic error_ in its entirety.

这正是「竞态 ≠ 数据竞争」的官方表述。

#### 4.1.4 代码实践

**实践目标**：亲手放大并复现 lib.rs 的竞态，建立「单操作原子、多操作非原子」的肌肉记忆。

**操作步骤**（在你的项目里新建一个二进制 `examples/race_repro.rs`）：

```rust
// 示例代码：放大竞态窗口
use crossbeam_skiplist::SkipSet;
use crossbeam_utils::thread::scope;
use std::sync::atomic::{AtomicUsize, Ordering};

let misses = AtomicUsize::new(0);
// 重复足够多次，让偶发竞态稳定复现
for _ in 0..100_000 {
    let numbers = SkipSet::new();
    scope(|s| {
        s.spawn(|_| {
            numbers.remove(&5);
        });
        numbers.insert(5);
        if !numbers.contains(&5) {
            misses.fetch_add(1, Ordering::Relaxed);
        }
    }).unwrap();
}
println!("contains 在 insert 后返回 false 的次数: {}",
         misses.load(Ordering::Relaxed));
```

**需要观察的现象**：`misses` 大概率 > 0，且每次运行数值不同（非确定性）。

**预期结果**：在多核机器上能稳定观察到非零的 miss 次数；若在单核或极端调度下偶发为 0，可加大循环次数。

**待本地验证**：具体 miss 数取决于核数与调度，本讲义不预设数值。

#### 4.1.5 小练习与答案

**练习 1**：把上面的 `scope` 里 `numbers.insert(5)` 之后紧跟 `assert!(numbers.contains(&5))`，这个断言「有时对、有时错」。它违反了 Rust 的内存安全吗？为什么？

> **答**：不违反。这是竞态而非数据竞争。`insert` 和 `contains` 各自内存安全（有 `Guard`/引用计数保护），断言失败是**逻辑层面**的不一致，不会段错误。Rust 编译器和类型系统对此无能为力，因为它跨了两次独立调用。

**练习 2**：为什么这个 doctest 标了 `no_run` 而不是普通的 doctest？

> **答**：结果依赖线程调度，是非确定性的。普通 doctest 进 CI 会偶发失败，污染构建信号；`no_run` 只编译、不运行，保证「代码能编译过」这一确定性事实。

---

### 4.2 数据竞争 vs 竞态：为什么跳表「永远内存安全」

#### 4.2.1 概念说明

这一节把上一节的结论往深里挖一层：**为什么**竞态不可能升级成段错误？

答案藏在两个机制里：

1. **`&self` + 内部可变性**：所有修改方法（`insert`/`remove`/`compare_insert`/...）都接收 `&self` 而非 `&mut self`（见 [src/lib.rs:L12-L14](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L12-L14)）。Rust 的类型系统本来禁止「多个 `&T` 同时存在可变性」，但跳表用**原子操作**实现了内部可变性——所有「写」都落在 `Atomic*` 上，由硬件保证读写的原子性与可见性，于是多个线程同时调用 `&self` 方法**不存在数据竞争**。
2. **不暴露 `get_mut`**：`lib.rs` 的「Mutable access to elements」章节（[src/lib.rs:L86-L100](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L86-L100)）明确说，`SkipMap`/`SkipSet` **故意不提供**拿到 `&mut V` 的方法。因为一旦给了 `&mut V`，用户就能在不加锁的情况下并发写同一块 `V`，那就成了真正的数据竞争（UB）。需要可变值时，文档要求用户自己包 `SkipMap<K, RwLock<V>>`，把同步责任显式交还给用户。

这两条合起来，就是「竞态是逻辑错误、但永远内存安全」的根因。

#### 4.2.2 核心流程

可以用一张「错误分类」表来固化直觉：

| 现象 | 是否 UB | 谁负责避免 | 在跳表里会不会发生 |
| --- | --- | --- | --- |
| 数据竞争（data race） | **是**（UB） | Rust 类型系统 + 原子操作 | **不会**：所有写都走原子，且不暴露 `&mut` |
| 竞态（race condition） | 否（逻辑错误） | **用户**的业务代码 | **会**：跨调用的时序由调度决定 |
| use-after-free | **是**（UB） | epoch 回收 + 引用计数 | **不会**：被删节点延迟到无引用才回收 |

#### 4.2.3 源码精读

「不暴露 `get_mut`」的设计意图写在文档里：

[src/lib.rs:L86-L100](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L86-L100) — 中文说明：这段明确拒绝内置可变访问，理由是「并发调用会导致数据竞争」；给出的替代方案是 `SkipMap<Key, RwLock<Value>>`。

而 `&self` 的约定在 [src/lib.rs:L12-L14](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L12-L14)：

```rust
//! Methods which mutate the map, such as [`insert`],
//! take `&self` rather than `&mut self`. This allows
//! them to be invoked concurrently.
```

中文说明：修改方法取 `&self`，这是「能被并发调用」的类型层面前提；真正提供并发安全的是底层原子操作与 epoch 回收。

#### 4.2.4 代码实践

**实践目标**：用编译器亲手验证「类型系统挡住了数据竞争」。

**操作步骤**：

1. 尝试写 `let entry = map.get(&k).unwrap(); *entry.value_mut() = new_val;`——编译失败，因为 `Entry` 根本没有 `value_mut`。
2. 阅读 [src/lib.rs:L86-L100](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L86-L100)，对比 `SkipMap<K, V>` 与 `SkipMap<K, RwLock<V>>`：后者把「值的可变性」交给 `RwLock`，跳表本身依然只管有序存储。

**需要观察的现象**：第 1 步编译器报「no method named `value_mut` found」。

**预期结果**：确认 `Entry` API 只有不可变的 `key()`/`value()`，可变访问必须由外层锁提供。

#### 4.2.5 小练习与答案

**练习**：假设有人给 `SkipMap` 加了一个 `get_mut(&self, k: &Q) -> Option<&mut V>`，为什么即便返回的 `&mut V` 不重叠，它也几乎必然引入数据竞争？

> **答**：因为 `get_mut` 在无锁的 `&self` 上被调用，多个线程可能同时拿到指向**同一节点**或**正在被并发删除/替换的节点**的 `&mut`。`&mut` 要求独占，但跳表的并发模型根本无法在 `&self` 上保证独占——这正是文档拒绝提供它的原因。正确做法是 `SkipMap<K, RwLock<V>>`，让 `RwLock` 在值层面提供独占。

---

### 4.3 is_removed：逻辑删除的观测口

#### 4.3.1 概念说明

上一节讲了「竞态不破坏内存安全」。但有一个非常现实的问题：用户拿到一个 `Entry`/`RefEntry` 句柄后，**怎么知道它还在不在表里**？这就是 `is_removed()` 的用途——它是逻辑删除暴露给用户的**唯一观测口**。

要点有三：

1. `is_removed()` 读的是 **level 0 指针的 tag 位**（u3-l9 已讲：tag=1 即被逻辑删除）。
2. 它用 `Ordering::Relaxed` 加载，是**尽力而为**的观测——可能读到旧值，但绝不会 UB。
3. **删除的安全性不依赖它**。真正保证「恰有一个赢家删掉节点」的是 `mark_tower` 在 level 0 的原子 `fetch_or`；`is_removed` 只是给用户「瞄一眼当前状态」的便利方法。

#### 4.3.2 核心流程

`is_removed` 的调用链很短：

```
Entry::is_removed()        / RefEntry::is_removed()
        │
        └──> NodeRef::is_removed()
                  │ 读 (*node).tower[level0] 的 tag（不 deref 指针本体）
                  └──> tag == 1 ?
```

由于只读 tag、不解引用后继节点本体，所以加载用 `Relaxed`、`Guard` 用 `unprotected()` 也安全——这点和 `mark_tower`/`random_height` 里的同类用法一致（详见 u5-l17）。

#### 4.3.3 源码精读

最底层的实现在 `NodeRef` 上：

[src/base.rs:L350-L361](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L350-L361) — 中文说明：加载 level 0 指针**仅为读 tag**，故 `Relaxed` + `unprotected()` 即可；返回 `tag == 1`。

```rust
fn is_removed(self) -> bool {
    let tag = unsafe {
        // We're loading the pointer only for the tag, so it's okay to use
        // `epoch::unprotected()` in this situation.
        self.get_level(0)
            .load(Ordering::Relaxed, epoch::unprotected())
            .tag()
    };
    tag == 1
}
```

两个高层句柄各自转发到它：

[src/base.rs:L1491-L1494](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1491-L1494) — 中文说明：`Entry::is_removed` 转发 `NodeRef::is_removed`。

```rust
/// Returns `true` if the entry is removed from the skip list.
pub fn is_removed(&self) -> bool {
    self.node.is_removed()
}
```

[src/base.rs:L1633-L1636](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1633-L1636) — 中文说明：`RefEntry::is_removed` 同样转发。

#### 4.3.4 代码实践

**实践目标**：体会 `is_removed` 是「尽力而为的观测」，以及「句柄活着，节点就不被回收」。

**操作步骤**（示例代码）：

```rust
// 示例代码：观察 is_removed 与延迟回收
use crossbeam_skiplist::SkipMap;
use crossbeam_utils::thread::scope;

let map = SkipMap::new();
map.insert("k", 1);
let entry = map.get("k").unwrap();   // 拿到 RefEntry，引用计数 +1
assert!(!entry.is_removed());

scope(|s| {
    s.spawn(|_| {
        map.remove("k");             // 逻辑删除：level0 tag 0->1
    });
}).unwrap();

// 此刻 entry 指向的节点已被逻辑删除，但句柄还活着 → 节点未被回收
assert!(entry.is_removed());         // 读到 tag==1
println!("removed? {} value still readable: {}", entry.is_removed(), entry.value());
// drop(entry) 后引用计数归零，节点才会被 defer_finalize
```

**需要观察的现象**：`entry.is_removed()` 在 `remove` 之后变为 `true`，但 `entry.value()` 仍可安全读取（不段错误）。

**预期结果**：验证「逻辑删除可被观测、但内存安全不受影响」。

**待本地验证**：`is_removed` 用 `Relaxed`，极端时序下你可能在 remove 刚发起时仍读到 `false`，多观察几次即可。

#### 4.3.5 小练习与答案

**练习 1**：`is_removed` 用 `Relaxed` 而非 `Acquire`，会不会导致它返回错误结果从而引发 UB？

> **答**：不会引发 UB。它只读 tag 决定布尔返回值，且**删除安全性不依赖它**（依赖 `mark_tower` 的 level0 原子 `fetch_or`）。`Relaxed` 最多让它读到「稍微旧」的 tag，属于尽力而为的观测，永远不会解引用已释放内存（节点由 epoch + 引用计数保证存活）。

**练习 2**：为什么 `is_removed` 只检查 level 0，而不遍历整座塔？

> **答**：因为 `mark_tower` 自顶向下标记，**只有 level 0 的 tag 由 0→1 才是删除的线性化点**（u3-l9）。一旦 level 0 被标记，节点就被宣告删除；高层是否标记是实现细节，不能作为「是否删除」的判据。

---

### 4.4 Drop for SkipList：独占访问下的整链释放

#### 4.4.1 概念说明

`SkipList` 被 drop 时，要遍历整条 level 0 链把每个节点析构并回收。常规并发路径里，释放节点必须经过 epoch（`defer_unchecked(finalize)`）——因为不能假设没有别的线程正拿着指针。

但 `Drop::drop(&mut self)` 拿到的是 `&mut self`。`&mut` 在 Rust 里意味着**独占**：此刻不可能有其他 `&SkipList` 借用存活，也就不可能有别的线程在并发访问这条链。既然「没有别的线程」，epoch 的延迟回收就**没必要**了——可以直接 `finalize` 同步释放。这就是 `unprotected()` 在这里安全的根本原因。

这是一个很重要的范式：**安全性来自前置条件（独占访问），而不是来自 `unprotected()` 本身**。`unprotected()` 只是把 epoch 的保护「关掉」；关掉它合不合法，要看调用点是否满足独占前提。

#### 4.4.2 核心流程

```
Drop::drop(&mut self)
  │  head.get_level(0).load(Relaxed, unprotected())   // 第一个真实节点
  ▼
while let Some(n) = node {
    next = n.get_level(0).load(Relaxed, unprotected()) // 先存好下一个
    Node::finalize(n)                                  // 同步析构 + dealloc
    node = next                                        // 再前进
}
```

注意顺序：**先把 `next` 存下来，再 `finalize` 当前节点**。因为 `finalize` 会回收当前节点的内存，若先回收再读 `next`，就是 use-after-free。

#### 4.4.3 源码精读

[src/base.rs:L1413-L1437](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1413-L1437) — 中文说明：独占 drop 整链，注释明确「this function is the only one currently using the skip list」，故 `unprotected()` 合法。

```rust
impl<K, V, C> Drop for SkipList<K, V, C> {
    fn drop(&mut self) {
        unsafe {
            let mut node = NodeRef::from_shared(
                self.head
                    .get_level(0)
                    .load(Ordering::Relaxed, epoch::unprotected()),
            );
            // Iterate through the whole skip list and destroy every node.
            while let Some(n) = node {
                // Unprotected loads are okay because this function is the only one currently using
                // the skip list.
                let next = NodeRef::from_shared(
                    n.get_level(0).load(Ordering::Relaxed, epoch::unprotected()),
                );
                // Deallocate every node.
                Node::finalize(n.ptr.as_ptr());
                node = next;
            }
        }
    }
}
```

要点逐条对应：

- `&mut self` → 独占访问（前提）。
- `Relaxed` → 只读指针本身、且独占下无并发写，不需要建立 happens-before。
- `unprotected()` → 无需 pin epoch，因为没有别的线程可能持有该节点。
- `Node::finalize` → 同步析构 key/value 并 `dealloc`（与并发路径里的「延迟 `defer_unchecked`」对照，这里**不延迟**）。

#### 4.4.4 代码实践

**实践目标**：把「独占访问」这个前提在脑中具象化。

**操作步骤**：

1. 阅读 [src/base.rs:L1413-L1437](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1413-L1437)，找到把「独占」写进注释的那一行（`// Unprotected loads are okay because this function is the only one currently using the skip list.`）。
2. 思考：如果在 `drop` 里改用 `epoch::pin()` + `defer_unchecked(finalize)`，行为还正确吗？

**需要观察的现象（纯源码阅读，不运行）**：用 epoch 延迟回收在逻辑上仍然正确（最终也会释放），但有两点代价：(a) 多了一次 pin；(b) `defer` 的回收依赖 Collector 后续推进，而 `SkipList` 都要销毁了，再走全局 Collector 是无谓的绕远路。所以作者选择了直接的 `finalize`。

**预期结果**：能口头解释「为什么这里可以、甚至应该绕过 epoch」。

#### 4.4.5 小练习与答案

**练习**：`Drop` 里 `finalize` 当前节点之前，为什么必须先把 `next` 取出来存好？如果改成「先 `finalize`，再读 `n.get_level(0)`」会怎样？

> **答**：`finalize` 会回收 `n` 的内存（`dealloc`）。之后再访问 `n.get_level(0)` 是访问已释放内存——use-after-free（UB）。所以必须先读 `next`、再 `finalize`、最后用已存好的 `next` 前进。源码正是这个顺序。

---

### 4.5 IntoIter：消费式遍历与「跳过死节点」

#### 4.5.1 概念说明

`IntoIter` 是跳表唯一的 **owning 迭代器**——它消费（move）整个 `SkipList`，逐个吐出 `(K, V)`。和 `Drop` 一样，它持有独占访问（`self` 被 `into_iter` 拿走），所以也可以用 `unprotected()`。

但 `IntoIter` 比 `Drop` 多两个微妙之处：

1. **边迭代边销毁**：`next` 每产出一个 `(K, V)`，就用 `ptr::read` 把键值**搬出**节点（move 语义），然后立即 `Node::dealloc` 回收节点内存——**只回收，不析构**键值（因为键值已被搬走，所有权转交给了调用者）。
2. **跳过死节点**：跳表里可能存在被逻辑删除（tag=1）但还没物理摘除的节点。`IntoIter` 用 `next.tag() == 0` 判断当前节点是否「活着」，死节点直接跳过、不产出。

#### 4.5.2 核心流程

```
into_iter(self) ── move 整个 SkipList
  │  head.get_level(0).load(unprotected())   // 记下首节点裸指针
  │  把 head 所有层指针清 null                 // 防止 Drop 重复释放
  ▼
IntoIter { node: *mut Node }

next(&mut self):
  loop {
    若 self.node 为 null → return None
    key   = ptr::read(&node.key)        // 搬走 key
    value = ptr::read(&node.value)      // 搬走 value
    next  = node.get_level(0).load(unprotected())
    Node::dealloc(node)                 // 仅回收内存（不 finalize，键值已搬走）
    self.node = next
    若 next.tag() == 0 → return Some((key,value))   // 活节点才产出
    // 否则 continue，跳过死节点
  }
```

数学上，`IntoIter` 产出的元素个数等于「未被逻辑删除的节点数」，而非「链上节点总数」。

#### 4.5.3 源码精读

`into_iter` 的构造（[src/base.rs:L1449-L1477](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1449-L1477)）—— 中文说明：move 出首节点裸指针，并把 head 全部层指针清空（防止后续 `Drop for SkipList` 二次释放）：

```rust
fn into_iter(self) -> Self::IntoIter {
    unsafe {
        let front = self.head.get_level(0)
            .load(Ordering::Relaxed, epoch::unprotected()).as_raw();
        // Clear the skip list by setting all pointers in head to null.
        for level in 0..MAX_HEIGHT {
            self.head.get_level(level).store(Shared::null(), Ordering::Relaxed);
        }
        IntoIter { node: front as *mut Node<K, V> }
    }
}
```

`IntoIter` 结构体与其 `Drop`（[src/base.rs:L2255-L2283](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L2255-L2283)）—— 中文说明：若迭代提前终止（用户没消费完），`Drop` 负责把剩余节点**整体** `finalize`（因为这些节点的键值没被 `ptr::read` 搬走，仍需析构）：

```rust
impl<K, V> Drop for IntoIter<K, V> {
    fn drop(&mut self) {
        while let Some(node) = NonNull::new(self.node) {
            unsafe {
                let node = NodeRef::new(node);
                let next = node.get_level(0)
                    .load(Ordering::Relaxed, epoch::unprotected());
                // We can safely do this without deferring because references to
                // keys & values that we give out never outlive the SkipList.
                Node::finalize(node.ptr.as_ptr());
                self.node = next.as_raw() as *mut Node<K, V>;
            }
        }
    }
}
```

`Iterator::next`（[src/base.rs:L2285-L2322](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L2285-L2322)）—— 中文说明：`ptr::read` 搬走键值后只 `dealloc`（不析构）；用 `next.tag() == 0` 跳过死节点：

```rust
fn next(&mut self) -> Option<Self::Item> {
    loop {
        if self.node.is_null() { return None; }
        unsafe {
            let key = ptr::read(&(*self.node).key);
            let value = ptr::read(&(*self.node).value);
            let next = {
                let node = NodeRef::new(NonNull::new_unchecked(self.node));
                node.get_level(0).load(Ordering::Relaxed, epoch::unprotected())
            };
            Node::dealloc(self.node);               // 仅回收内存
            self.node = next.as_raw() as *mut Node<K, V>;
            // 死节点（tag==1）直接跳过，不产出
            if next.tag() == 0 { return Some((key, value)); }
        }
    }
}
```

这里有一个**关键区分**：`next` 走的是 `dealloc`（只回收内存），`Drop` 走的是 `finalize`（析构键值 + 回收内存）。原因就是 `next` 已经 `ptr::read` 把键值 move 走了，节点里的是「被搬空的躯壳」，不能再析构；而 `Drop` 处理的是「没人消费过」的剩余节点，键值还在，必须析构。这个 `finalize` vs `dealloc` 的二分在 u3-l11 里也出现过。

#### 4.5.4 代码实践

**实践目标**：验证 `IntoIter` 跳过死节点，且独占消费不会段错误。

**操作步骤**（示例代码）：

```rust
// 示例代码：IntoIter 跳过逻辑删除的节点
use crossbeam_skiplist::SkipMap;

let map = SkipMap::new();
map.insert(1, "a");
map.insert(2, "b");
map.insert(3, "c");
map.remove(&2);                 // 逻辑删除 2（可能尚未物理摘除）

let collected: Vec<(i32, &str)> = map.into_iter().collect();
// 2 不会被产出，即便它还挂在链上等待回收
assert_eq!(collected, vec![(1, "a"), (3, "c")]);
```

**需要观察的现象**：`collect` 出来的结果**不含**被删除的 key `2`。

**预期结果**：验证 `next.tag() == 0` 的过滤生效。注意：此例依赖「remove 后节点仍在链上」这一时序；在单线程下 remove 走完后 help_unlink 通常已物理摘除，但即便物理上还在，`IntoIter` 也会正确跳过——两种情况下断言都成立。

#### 4.5.5 小练习与答案

**练习 1**：`IntoIter::next` 里为什么用 `Node::dealloc` 而 `Drop for IntoIter` 用 `Node::finalize`？

> **答**：`next` 已用 `ptr::read` 把 key/value move 出去（所有权转交给返回的 `(K, V)`），节点里只剩搬空的内存，故只需 `dealloc` 回收内存、**不能**再析构键值（会重复释放）。`Drop` 处理的是用户没消费完的剩余节点，键值未被搬走，必须 `finalize` 先析构键值再 `dealloc`。

**练习 2**：`into_iter` 里为什么要把 head 的**所有层**指针清成 null，而不是只清 level 0？

> **答**：因为 `into_iter(self)` 消费了 `SkipList`，随后 `SkipList` 会被 drop，触发 `Drop for SkipList`——它会从 `head.get_level(0)` 开始遍历整链释放。如果不清空，就会和 `IntoIter` 自己的释放**重复释放**同一批节点。把 head 全层清 null 后，`Drop for SkipList` 看到空链直接什么都不做，释放责任完全移交给 `IntoIter`。

---

## 5. 综合实践

把本讲的「竞态复现」与「稳妥改造」串成一个完整小任务。

**任务**：复现 lib.rs 的 SkipSet `insert`/`contains` 竞态，再把它改造成一个窗口更小、语义更明确的方案，并写一段取舍说明。

**步骤**：

1. **复现**：用 4.1.4 的 `examples/race_repro.rs`，统计 10 万次循环里 `contains` 在 `insert` 后返回 `false` 的次数。
2. **改造方案 A（缩小竞态窗口）**：把「先 insert 再 contains」改成一次 `lower_bound`/`upper_bound` 区间查询，把「判定存在」压缩成单次调用。参考 [src/map.rs:L306-L338](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L306-L338)（`SkipMap::lower_bound`/`upper_bound`，单操作原子）。

   ```rust
   // 示例代码：用单次 lower_bound 取代「insert + contains」两步
   use crossbeam_skiplist::SkipMap;
   use std::ops::Bound;
   let m = SkipMap::new();
   m.insert(5, ());
   // 一次调用拿到 Entry（单操作原子），不存在 insert 与 contains 之间的缝隙
   if let Some(e) = m.lower_bound(Bound::Included(&5)) {
       assert_eq!(*e.key(), 5);
   }
   ```

3. **改造方案 B（用句柄锁定观察结果）**：直接用 `get` 返回的 `Entry`/`RefEntry` 句柄（[src/map.rs:L271-L304](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L271-L304)），配合 `is_removed()`（[src/base.rs:L1491-L1494](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1491-L1494)）。句柄存活期间节点不会被回收，于是「拿到句柄 → 读值」这一段无惧并发 `remove` 导致的 use-after-free；至于「业务上是否仍认为它在表里」，用 `is_removed()` 在关键决策点复查即可。
4. **取舍说明（文字）**：
   - 方案 A 把「两步」压成「一步」，从结构上消除了竞态窗口，正确性最强；但只能回答「某个 bound 处的元素」，语义受限。
   - 方案 B 保留了 `insert` 后继续操作的灵活性，把不可消除的竞态降级为「可被 `is_removed` 观测的逻辑状态」，适合「读后还要基于该值做决策」的场景；代价是句柄持有期间会推迟该节点的 epoch 回收（轻微内存压力）。
   - 两者都不会引入数据竞争——因为底层始终走 `&self` + 原子操作，内存安全由类型系统与 epoch 共同兜底。

**预期结果**：能输出竞态 miss 次数 > 0，并能口头论证 A/B 两方案在「正确性 vs 灵活性 vs 内存」上的取舍。

---

## 6. 本讲小结

- **竞态 ≠ 数据竞争**：跳表所有写都走 `&self` + 原子操作、且不暴露 `get_mut`，故**数据竞争不会发生**（内存安全）；但跨调用的**竞态**是逻辑错误，由用户负责。
- **单操作原子、多操作非原子**：`insert`/`get`/`remove`/`contains` 各自原子，连起来调用没有事务性；lib.rs 的 `no_run` doctest 是这条语义的最小样本。
- **`is_removed` 是尽力而为的观测**：用 `Relaxed` 读 level 0 tag，可能读到旧值，但删除安全性依赖 `mark_tower` 的 level 0 原子翻转，**不依赖**它。
- **`Drop for SkipList` 与 `IntoIter` 用 `unprotected()` 的前提是独占访问**（`&mut self` / move `self`），安全性来自前置条件、而非 `unprotected()` 本身。
- **`IntoIter` 的 `dealloc` vs `Drop` 的 `finalize` 二分**：`next` 已 `ptr::read` 搬走键值，只回收内存；`Drop` 处理未被消费的剩余节点，需先析构键值。死节点靠 `next.tag() == 0` 跳过。

---

## 7. 下一步学习建议

- 下一讲 **u5-l19 测试体系与基准实践** 会把「并发正确性如何被测试验证」讲透：`tests/map.rs` 里的 `concurrent_insert`/`compare_insert`/`memory_leak` 用例正是检验本讲「竞态不破坏内存安全」与「延迟回收不泄漏」的工程手段，并用 Miri 验证本讲那些 `unsafe` 块（`ptr::read`、`unprotected`、`finalize`）确实无 UB。
- 若想再往前走一步，建议对照阅读 `crossbeam-epoch` 的 `unprotected()` 与 `defer_unchecked` 文档，从源头理解「为什么独占访问下可以关掉 epoch」。
- 复盘建议：回到 u3-l9（标记指针）与 u3-l11（remove/clear），把本讲的「线性化点 = level 0 tag 0→1」「Drop/IntoIter 跳过死节点」与那两讲的删除主链路对齐阅读，能形成完整的「删除全生命周期」图景。
