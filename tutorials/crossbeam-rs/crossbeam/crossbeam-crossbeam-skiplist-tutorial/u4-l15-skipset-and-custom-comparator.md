# SkipSet 与自定义比较器封装

> 所属单元：u4 高级——句柄、迭代器与高层封装
> 依赖讲义：u4-l14（SkipMap 高层封装）、u2-l7（Comparator 与 Equivalent）

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 `SkipSet<T, C>` 为什么能「零开销」复用 `SkipMap` 的全部并发逻辑——它本质上就是 `SkipMap<T, (), C>` 的一层 newtype。
- 解释 `set::Entry` 为什么用 `Deref<Target = T>`，以及它的 `value()` 是如何「丢弃 `()` 值、只暴露 map 的 key」的。
- 写出一个自定义 `Comparator`，用 `SkipSet::with_comparator` 构造一个按自定义规则排序的并发有序集合，并理解「`compare` 与 `equivalent` 必须对相等达成一致」这条硬性契约。

本讲不重复 u2-l7 的 trait 定义细节，也不重讲 u4-l14 的 `ManuallyDrop`/`release_with_pin` 释放机制；我们只在需要衔接时点一句，把重点放在「set 层如何复用 map 层」和「自定义比较器如何接入」。

## 2. 前置知识

在进入源码前，请确认你已经理解下面几个概念（来自前置讲义）：

- **跳表是有序的**：节点按键升序串联，遍历顺序由比较函数决定（u1-l1、u3-l8）。
- **两层比较设计**（u2-l7）：第一层是类型驱动的 `Equivalent`/`Comparable`（靠 `Borrow`+`Ord`/`Eq` 的 blanket impl 工作）；第二层是运行时驱动的 `Equivalator`/`Comparator`（带 `&self`，可被 `dyn` 分发或自定义）。默认实现 `BasicComparator` 是 ZST，把第二层委托回第一层。
- **查询走两步法**（u2-l7、u3-l8）：`search_position` 用 `compare` 二分定位，`get`/`contains` 再用 `equivalent` 二次确认命中。
- **SkipMap 封装套路**（u4-l14）：每个公共方法首行 `let guard = &epoch::pin();`，返回的 `Entry` 是 `ManuallyDrop<base::RefEntry>`，Drop 时按需 pin 释放引用计数。
- **ZST（零大小类型）**：`()` 是 ZST，占用 0 字节，对其取引用或存储都不分配内存。

> 术语提示：本文把 `()` 称为「占位值」或「dummy 值」，它在 set 里没有任何信息量，存在的唯一理由是复用 map 的 `K-V` 结构。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [`src/set.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/set.rs) | `SkipSet`、`set::Entry`、`Iter`、`Range`、`IntoIter` 的全部实现，是本讲主战场。 |
| [`src/comparator.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/comparator.rs) | `Equivalator`/`Comparator` trait、默认实现 `BasicComparator`、以及「两者必须一致」的契约说明。 |
| [`src/equivalent.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/equivalent.rs) | 第一层 `Equivalent`/`Comparable` 及其 blanket impl（自定义比较器会绕过它）。 |
| [`src/map.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs) | `SkipMap::new`/`with_comparator`——set 的每个方法最终都委托到这里。 |
| [`src/lib.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs) | `pub use crate::{map::SkipMap, set::SkipSet}` 把两者重导出到 crate 根。 |
| [`tests/set.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/tests/set.rs) | `comparator` 测试提供了一个真实的自定义比较器样例 `DynComparator`，本讲实践参照它编写。 |

---

## 4. 核心概念与源码讲解

### 4.1 SkipSet\<T, C>：用 SkipMap\<T, ()> 零开销复用全部 map 逻辑

#### 4.1.1 概念说明

`BTreeSet` 在标准库里并不是从零实现的——它内部就是一个 `BTreeMap<T, ()>`，把「集合」当成「值恒为 `()` 的映射」来处理。`crossbeam-skiplist` 的 `SkipSet` 完全沿用这个套路：它不是一套独立的并发算法，而是把底层的 `SkipMap` 整个包起来，每个方法都是一行委托（delegate）。

为什么这样做是「零开销」的？因为值类型 `()` 是 ZST：

- `Node` 的内存布局里虽然有一个 `value` 字段，但 ZST 字段不占任何字节（见 u2-l5 的 `Node::alloc`/`get_layout`）。
- `insert` 时传入的 `()` 不需要构造、不需要析构、不需要拷贝。
- 唯一的「开销」是类型签名里多出一个 `()` 参数，编译期就被擦除。

所以 `SkipSet` 几乎就是 `SkipMap` 的一个语义别名——所有无锁并发、epoch 回收、随机塔高、`ManuallyDrop` 释放（u4-l14）的能力，set 都原封不动地继承下来。

#### 4.1.2 核心流程

`SkipSet` 的方法实现遵循一个高度统一的模板：

```
1. 调用 self.inner.<同名 map 方法>(...)，拿到 map::Entry / map::Iter / bool / () 等；
2. 如果返回的是句柄或迭代器，用 Entry::new 或包一层 NewType 把 map 类型转成 set 类型；
3. 把 set 类型的结果返回给用户。
```

`self.inner` 的类型是 `map::SkipMap<T, (), C>`（见下方源码），所以「转换」本质上只是在类型层面把 `map::Entry<'a, T, (), C>` 包装成 `set::Entry<'a, T, C>`，运行时零成本。

bounds（trait 约束）也严格继承自 map 层，分三类：

| 操作类别 | 代表方法 | 约束 |
| --- | --- | --- |
| 纯读 / 迭代 | `front`/`back`/`get`/`contains`/`iter`/`lower_bound` | `C: Comparator<T>`（查询类还要求 `C: Comparator<T, Q>`） |
| 写 | `insert`/`remove`/`pop_front`/`pop_back`/`clear` | `C: Comparator<T>` 且 `T: Send + 'static` |
| 构造 | `new`/`with_comparator` | 无 `Comparator` 约束（此时还没存任何键） |

其中 `T: Send + 'static` 来自 epoch 回收对键值生命周期的要求（u2-l6），与 set 的「集合」语义无关——它只是被 map 层透传上来的。

#### 4.1.3 源码精读

整个故事的开头就是结构体定义本身：`SkipSet` 持有的唯一字段 `inner` 就是 `map::SkipMap<T, (), C>`。

[set.rs:24-26](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/set.rs#L24-L26) 定义了 `SkipSet<T, C = BasicComparator>`，`inner` 即 `map::SkipMap<T, (), C>`。注意 `C` 的默认值是 `BasicComparator`，所以 `SkipSet<T>` 不写比较器类型时，行为与 `BTreeSet` 一致。

构造方法直接转发到 map 层，比较器原样透传：

[set.rs:38-42](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/set.rs#L38-L42) 是 `new()`，内部 `map::SkipMap::new()`；而 `map::SkipMap::new` 在 [map.rs:42-45](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L42-L45) 用 `epoch::default_collector().clone()` 构造底层 `base::SkipList`（u4-l14 讲过）。

[set.rs:55-59](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/set.rs#L55-L59) 是 `with_comparator(comparator: C)`，把比较器交给 `map::SkipMap::with_comparator`，后者在 [map.rs:59-62](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L59-L62) 同样用 `default_collector` 构造底层跳表——这正是本讲 4.3 节「自定义排序」的入口。

读操作都是「先 `inner.xxx(...)`，再 `Option::map(Entry::new)`」的一行式委托。以 `get` 为例：

[set.rs:167-173](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/set.rs#L167-L173) 中 `get<Q>` 调 `self.inner.get(key)`（返回 `Option<map::Entry<...>>`），再 `map(Entry::new)` 转成 set 句柄。注意 `where C: Comparator<T, Q>`——查询用的 `Q` 可以不同于存储类型 `T`（例如用 `&str` 查 `String`），这正是 u2-l7 两层 Borrow 机制带来的灵活性。

[set.rs:148-154](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/set.rs#L148-L154) 的 `contains` 更简单：它直接复用 map 的 `contains_key`，返回 `bool`，无需包装句柄。

写操作的约束块单独成段，要求 `T: Send + 'static`：

[set.rs:304-308](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/set.rs#L304-L308) 是写操作的 `impl` 头，可见 `C: Comparator<T>` 与 `T: Send + 'static` 两个约束。

[set.rs:323-325](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/set.rs#L323-L325) 的 `insert(key)` 内部是 `Entry::new(self.inner.insert(key, ()))`——注意第二个参数传的是 `()`，这就是「set 没有 value」在源码里的全部体现：占位值被塞进 map 的 V 槽位。

[set.rs:342-348](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/set.rs#L342-L348) 的 `remove`、[set.rs:371-373](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/set.rs#L371-L373) 的 `pop_front`、[set.rs:414-416](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/set.rs#L414-L416) 的 `clear` 都是同样的「单行委托 + 句柄包装」模式，不再赘述。

> 一句话总结 4.1：`SkipSet` 是一个编译期擦除的薄包装，它的全部运行时行为都来自 `SkipMap<T, ()>`；读这套源码的过程，就是不断在 map 层的对应方法之间跳转。

#### 4.1.4 代码实践

**实践目标**：通过跟踪调用链，亲眼看一遍 set → map → base 的委托关系，确认 set 层没有「私藏」任何独立逻辑。

**操作步骤**：

1. 打开 [set.rs:323-325](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/set.rs#L323-L325) 的 `SkipSet::insert`。
2. 跳到它调用的 `self.inner.insert(key, ())`，即 `map::SkipMap::insert`（在 `src/map.rs` 中搜索 `pub fn insert`）。
3. 再跳到 map 层调用的 `self.inner.insert_internal(...)`，进入 `src/base.rs` 的 `insert_internal`（u3-l10 精读过）。
4. 用同样的方法跟踪一次 `SkipSet::get`：set.rs → `map::SkipMap::get` → base 层的 `get`/`search_bound`（u3-l8）。

**需要观察的现象**：set 层的方法体应当全部是「单行委托」，没有任何针对 set 的特殊分支或算法。

**预期结果**：你会在每一跳都看到「参数多一个 `()` 或少一个句柄包装」，但找不到任何只属于 set 的并发/内存逻辑。这正是「零开销复用」的直接证据。

**待本地验证**：可选地，运行 `cargo test --doc set::SkipSet::insert`（在 `crossbeam-skiplist` 目录下）确认 set 的 doctest 通过，验证委托链确实跑通。

#### 4.1.5 小练习与答案

**练习 1**：`SkipSet<T, C>` 的 `len()` 返回值与一个存了相同 key 的 `SkipMap<T, (), C>` 是否相等？为什么？

> **参考答案**：相等。`SkipSet::len` 直接委托 `self.inner.len()`（[set.rs:94-96](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/set.rs#L94-L96)），底层是同一个 `base::SkipList` 的节点计数，`()` 值不产生额外节点。

**练习 2**：为什么 `insert` 需要 `T: Send + 'static`，而 `get`/`contains` 不需要？

> **参考答案**：`insert` 会把 `T` 存进跳表节点，节点由 epoch 回收延迟释放，且可能被其他线程读取，因此要求 `T: Send`（跨线程转移）和 `T: 'static`（不持有非 `'static` 借用，u2-l6）。`get`/`contains` 只是临时读取并返回句柄或 `bool`，不长期持有 `T`，故无此约束。

---

### 4.2 set::Entry：Deref\<Target = T> 与 FromIterator 的映射技巧

#### 4.2.1 概念说明

`SkipMap::get` 返回的 `map::Entry` 同时暴露 `key()` 和 `value()` 两个借用方法（u1-l2、u4-l14）。但 set 里没有 value——元素本身就是 key。于是 `set::Entry` 面临一个设计问题：**它的 `value()` 应该返回什么？**

答案是：返回 map 的 **key**。在 set 的语义里，元素 `T` 既是 map 的键，也是用户眼中的「值」，所以 `set::Entry::value()` 内部调用的是 `self.inner.key()`，把 map 的键借用直接当成 set 的值暴露出来。那个 `()` 则被永远丢弃。

第二个设计巧思是 `Deref<Target = T>`。有了它，用户可以写 `*entry` 直接拿到 `&T`，而不必每次都写 `entry.value()`。这就是 set 的 doctest 里 `assert_eq!(*set.front().unwrap(), 1)` 这种简洁写法的来源。

#### 4.2.2 核心流程

`set::Entry` 的关键映射关系如下：

```
set::Entry::value()       ──委托──▶  map::Entry::key()        ──▶ &T
set::Entry::is_removed()  ──委托──▶  map::Entry::is_removed()  ──▶ bool
set::Entry::next()/prev() ──委托──▶  map::Entry::next()/prev() ──▶ set::Entry
set::Entry::remove()      ──委托──▶  map::Entry::remove()      ──▶ bool
*entry (Deref)            ──委托──▶  set::Entry::value()       ──▶ &T
```

集合层面的三个便利 trait 也都建立在「丢弃 `()`」之上：

- `FromIterator<T>`：建一个默认 set，对每个元素调 `get_or_insert`（幂等插入）。
- `IntoIterator for SkipSet`：消费整个集合，`IntoIter::next` 把 map 吐出的 `(K, ())` 解构成 `K`，扔掉 `()`。
- `IntoIterator for &SkipSet`：借用迭代，等价于 `self.iter()`。

#### 4.2.3 源码精读

先看 `Entry` 结构体本身——它只是 `map::Entry<'a, T, (), C>` 的 newtype：

[set.rs:477-480](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/set.rs#L477-L480) 定义 `Entry<'a, T, C = BasicComparator>`，唯一字段 `inner: map::Entry<'a, T, (), C>`。`new` 是私有构造器（[set.rs:482-485](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/set.rs#L482-L485)），只在 set.rs 内部用来包装 map 句柄。

核心映射在 `value()`：

[set.rs:488-490](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/set.rs#L488-L490)：`pub fn value(&self) -> &T { self.inner.key() }`。**这就是「set 的值 = map 的键」的全部秘密**——它把 map 的 key 借用当作 set 的 value 返回，`()` 永远不会出现在用户视野里。

`Deref` 把这种便利固化下来：

[set.rs:555-561](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/set.rs#L555-L561) 实现 `Deref<Target = T>`，`deref` 直接调用 `self.value()`。有了它，`*entry` 与 `entry.value()` 完全等价，于是 set 的 doctest（如 [set.rs:108-114](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/set.rs#L108-L114) 里的 `*set.front().unwrap()`)才能写得像直接取值一样自然。

句柄的导航与删除都是对 map 句柄的转发，约束同样是 `C: Comparator<T>`：

[set.rs:498-521](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/set.rs#L498-L521) 是 `move_next`/`move_prev`/`next`/`prev`，逐个委托 `self.inner.<同名方法>()`，返回值再用 `Entry::new` 包回 set 类型。

[set.rs:523-534](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/set.rs#L523-L534) 是 `Entry::remove(&self) -> bool`，要求 `T: Send + 'static`，委托 `self.inner.remove()`。这里复用了 u3-l9/u3-l11 讲过的「标记指针抢删除权」协议。

`FromIterator` 的实现值得一看，它揭示了「collect 一个 SkipSet」的代价：

[set.rs:461-475](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/set.rs#L461-L475)：先 `Self::default()` 建空 set（约束 `C: Default`），再对每个元素 `get_or_insert`。注意它用的是 `get_or_insert` 而非 `insert`——对重复元素是幂等的（已存在则保留旧的），这与 `BTreeSet::from_iter` 的语义一致。

`IntoIter` 在消费时扔掉 `()`：

[set.rs:568-574](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/set.rs#L568-L574)：`Iterator::next` 把 `self.inner.next()`（`Option<(T, ())>`）用 `.map(|(k, ())| k)` 解构，丢弃 unit 值，只返回 `T`。这是 set 在消费迭代时最后一次「甩掉 `()`」。

#### 4.2.4 代码实践

**实践目标**：直观感受 `Deref<Target = T>` 与 `FromIterator` 带来的使用便利，并验证 `value()` 与 `*entry` 的等价性。

**操作步骤**（示例代码，可放入 `examples/entry_deref.rs` 运行）：

```rust
// 示例代码：演示 set::Entry 的 Deref 与 FromIterator
use crossbeam_skiplist::SkipSet;

fn main() {
    // FromIterator：直接 collect 成 SkipSet（默认比较器，按 Ord 排序）
    let set: SkipSet<_> = (1..=3).collect();
    assert_eq!(set.len(), 3);

    // Deref 让 *entry 直接拿到值
    let front = set.front().unwrap();
    assert_eq!(*front, 1);          // 用 Deref
    assert_eq!(front.value(), &1);  // 与 value() 等价

    // 借用迭代也 yield Entry，可继续用 *e
    let sum: i32 = set.iter().map(|e| *e).sum();
    assert_eq!(sum, 6);
}
```

**需要观察的现象**：`*front` 与 `*front.value()` 行为一致；`(1..=3).collect()` 产生的 set 大小为 3 且升序。

**预期结果**：所有断言通过，证明 set 层的 API 表现得就像在操作一个普通 `BTreeSet`，`()` 完全隐形。

**待本地验证**：在 `crossbeam-skiplist` 目录执行 `cargo run --example entry_deref`（需先把上面的文件放进 `examples/`），确认输出无 panic。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `set::Entry::value()` 改成返回 `self.inner.value()`（即 map 的 value，类型 `&()`），用户代码会受什么影响？

> **参考答案**：用户将拿到无意义的 `&()`，再也无法读取元素本身；同时 `Deref<Target = T>` 也无法实现（`()` 不是 `T`），所有 `*entry` 写法都会编译失败。这正说明 set 必须把 value 映射到 map 的 **key**。

**练习 2**：`FromIterator` 要求 `C: Default`。如果你想 `(1..3).collect()` 成一个带**自定义**比较器的 `SkipSet`，还能用 `collect` 吗？

> **参考答案**：不能直接 `collect`（除非你的自定义比较器实现了 `Default`）。需要先 `let s = SkipSet::with_comparator(MyCmp);` 再手动循环 `s.insert(...)`，或给 `MyCmp` 实现 `Default`。

---

### 4.3 自定义比较器：让集合按你的规则排序

#### 4.3.1 概念说明

默认情况下，`SkipSet<T>` 用 `BasicComparator`，行为与 `BTreeSet` 完全一致（按 `T: Ord` 排序）。但很多场景需要自定义顺序：大小写不敏感的字符串、按长度排序、按编码后的字节序排序等等。`crossbeam-skiplist` 允许你通过 `SkipSet::with_comparator` 注入一个自定义的 `Comparator`，从而改变**排序与判等**。

回忆 u2-l7 的两层设计和「查询两步法」：搜索时先用 `compare` 二分定位，再用 `equivalent` 确认命中。这意味着自定义比较器必须满足一条硬性契约（写在 [comparator.rs:62-63](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/comparator.rs#L62-L63) 的 `Comparator` 文档里）：

\[ \forall x, y:\quad \mathrm{compare}(x, y) = \mathrm{Equal} \;\Longleftrightarrow\; \mathrm{equivalent}(x, y) = \mathrm{true} \]

也就是说，`compare` 判定相等的两个元素，`equivalent` 也必须判定相等，反之亦然。如果两者不一致，`contains`/`get` 会出现「定位到 A 却因为 `equivalent` 不命中而返回 `None`」或「定位错位却误判命中」的诡异 bug，而且这种 bug 在并发下极难复现。

> 陷阱提示：千万不要写「只按长度判等」的比较器。例如让 `equivalent` 只比较 `len`、`compare` 却按字典序——那么 `"banana"` 和 `"cherry"`（同为长度 6）会被 `equivalent` 当成相等，`contains("cherry")` 可能命中 `"banana"`，集合语义彻底崩溃。最稳妥的写法是让 `equivalent` 直接复用 `compare` 的结果（`.is_eq()`），从结构上保证一致。

#### 4.3.2 核心流程

实现一个自定义比较器并接入 `SkipSet` 的步骤：

```
1. 定义一个比较器类型（通常是 ZST，如 struct LenThenLex;）。
2. 为它 impl Equivalator<L, R>（提供 equivalent）。
   —— 因为 Comparator 是 Equivalator 的子 trait（comparator.rs:66），必须先实现后者。
3. 为它 impl Comparator<L, R>（提供 compare）。
   —— 让 equivalent 复用 compare，从源头保证契约。
4. 用 SkipSet::with_comparator(cmp) 构造集合。
5. 之后 insert/get/contains/remove/iter 全部自动遵循新规则。
```

为了让比较器能同时支持「存储类型 `T`」和「查询类型 `Q`」（例如用 `&str` 查 `String`），通常把 `impl` 写成对 `L: Borrow<str>, R: Borrow<str>` 的泛型版本，像标准库的 blanket impl 那样工作。

#### 4.3.3 源码精读

先确认 trait 关系与契约：

[comparator.rs:48-51](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/comparator.rs#L48-L51) 定义 `Equivalator<L, R = L>`，只有 `equivalent(&self, lhs, rhs) -> bool`。

[comparator.rs:66-69](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/comparator.rs#L66-L69) 定义 `Comparator<L, R = L>: Equivalator<L, R>`，新增 `compare(&self, lhs, rhs) -> Ordering`。**子 trait 关系意味着实现 `Comparator` 必须连带实现 `Equivalator`**，这正是契约能被类型系统强制的前提。

[comparator.rs:62-63](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/comparator.rs#L62-L63) 是契约的文字说明：「The `Comparator` and `Equivalator` implementations for a comparator must agree on which inputs are equal.」

默认实现 `BasicComparator` 是 ZST，把第二层委托回第一层（u2-l7）：

[comparator.rs:75-76](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/comparator.rs#L75-L76) 定义 `BasicComparator`（派生 `Default`，故能作 `SkipSet` 默认类型参数）。

[comparator.rs:78-96](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/comparator.rs#L78-L96)：`Equivalator` impl 委托 `<K as Equivalent<Q>>::equivalent`，`Comparator` impl 委托 `<K as Comparable<Q>>::compare`——最终落到 [equivalent.rs:23-54](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/equivalent.rs#L23-L54) 的 `Borrow`+`Ord`/`Eq` blanket impl。所以默认行为与 `BTreeSet` 一致，且 `compare` 与 `equivalent` 天然一致（一个用 `Ord::cmp`、一个用 `PartialEq::eq`，对满足 `Ord` 的类型必然协调）。

接入点在 set 层：

[set.rs:55-59](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/set.rs#L55-L59) 的 `with_comparator` 把比较器透传给 `map::SkipMap::with_comparator`，最终存进 `base::SkipList` 的 `C` 字段（u2-l7 讲过比较器是结构体的泛型字段）。一旦注入，set 的全部读写都会用这个 `C`，无需也不能在运行时更换。

真实的自定义比较器样例在测试里，本讲实践参照它：

[tests/set.rs:721-749](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/tests/set.rs#L721-L749) 定义 `DynComparator`，内部装一个 `Box<dyn Fn(&[u8], &[u8]) -> Ordering>`，并为所有 `L, R: Borrow<[u8]>` 实现 `Equivalator` 与 `Comparator`。**关键写法**：`equivalent` 调 `(self.inner)(...).is_eq()`、`compare` 调 `(self.inner)(...)`——两者指向同一个闭包，从结构上保证了 4.3.1 的契约。这是写自定义比较器最稳妥的范式。

[tests/set.rs:795-817](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/tests/set.rs#L795-L817) 用它构造 `SkipSet::with_comparator(DynComparator { inner: Box::new(compare) })`，存的是把 `f32` 数组小端编码后的 `Vec<u8>`。因为比较器按解码后的 `f32` 序比较，`0f32` 与 `-0f32` 编码不同但解码比较结果为 `Equal`，于是 `s.len() == 3`（四个 insert 去重成一个），且 `contains` 能用任一编码命中——这正是「比较器决定判等」的威力。

#### 4.3.4 代码实践

**实践目标**：实现一个「字符串长度升序、长度相同按字典序」的比较器，构造自定义排序的 `SkipSet`，验证迭代顺序与 `contains`/`get` 命中。

**操作步骤**（示例代码，仿照 [tests/set.rs:727-749](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/tests/set.rs#L727-L749) 的写法，可放入 `examples/len_cmp.rs`）：

```rust
// 示例代码：长度优先、长度相同按字典序的自定义比较器
use std::borrow::Borrow;
use std::cmp::Ordering;

use crossbeam_skiplist::SkipSet;
use crossbeam_skiplist::comparator::{Comparator, Equivalator};

struct LenThenLex;

// 把真正的比较逻辑集中在一个函数里，让 equivalent 与 compare 共用它，
// 从结构上满足 comparator.rs:62-63 的「二者必须一致」契约。
fn len_then_lex(a: &str, b: &str) -> Ordering {
    a.len().cmp(&b.len()).then_with(|| a.cmp(b))
}

impl<L, R> Equivalator<L, R> for LenThenLex
where
    L: Borrow<str> + ?Sized,
    R: Borrow<str> + ?Sized,
{
    fn equivalent(&self, lhs: &L, rhs: &R) -> bool {
        len_then_lex(lhs.borrow(), rhs.borrow()).is_eq()
    }
}

impl<L, R> Comparator<L, R> for LenThenLex
where
    L: Borrow<str> + ?Sized,
    R: Borrow<str> + ?Sized,
{
    fn compare(&self, lhs: &L, rhs: &R) -> Ordering {
        len_then_lex(lhs.borrow(), rhs.borrow())
    }
}

fn main() {
    // T 被推断为 &'static str（字符串字面量），满足 T: Send + 'static
    let s = SkipSet::with_comparator(LenThenLex);
    s.insert("banana"); // len 6
    s.insert("apple");  // len 5
    s.insert("kiwi");   // len 4
    s.insert("cherry"); // len 6
    s.insert("fig");    // len 3

    // 期望顺序：fig(3) < kiwi(4) < apple(5) < banana(6) == cherry(6 字典序在后)
    let order: Vec<&str> = s.iter().map(|e| *e.value()).collect();
    assert_eq!(order, vec!["fig", "kiwi", "apple", "banana", "cherry"]);

    // contains/get 走两步法，能在自定义序下正确命中
    assert!(s.contains("cherry"));
    assert!(s.get("apple").is_some());
    assert!(!s.contains("grape"));
}
```

**需要观察的现象**：

1. 迭代顺序是「先按长度、再按字典序」，而非纯字典序（否则 `apple` 会排在 `banana` 前，但 `cherry` 排在 `fig` 前，整体顺序完全不同）。
2. 即便查询时比较的是 `&str`、存储推断为 `&'static str`，`contains` 仍能命中——因为泛型 `impl<L, R: Borrow<str>>` 同时覆盖了 `Comparator<&str, &str>` 和查询所需的约束。

**预期结果**：所有断言通过，迭代序列恰为 `["fig", "kiwi", "apple", "banana", "cherry"]`。

**待本地验证**：在 `crossbeam-skiplist` 目录执行 `cargo run --example len_cmp`（需先把文件放进 `examples/`），或改写成 `#[test]` 后 `cargo test len_cmp`，确认无 panic。

> 对照实验（可选）：把 `equivalent` 改成「只比较长度」（`lhs.borrow().len() == rhs.borrow().len()`），重新运行，观察 `contains("cherry")` 是否仍返回 `true`（它可能命中 `banana` 而非真正的 `cherry`）——以此亲手体会 4.3.1 契约被破坏的后果。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `LenThenLex` 必须同时实现 `Equivalator` 和 `Comparator`，只实现后者会怎样？

> **参考答案**：`Comparator` 在 [comparator.rs:66](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/comparator.rs#L66) 声明为 `: Equivalator<L, R>`，是子 trait 关系；不实现 `Equivalator` 编译器会直接报「未满足 supertrait」错误，根本无法把 `LenThenLex` 当作 `Comparator` 使用。

**练习 2**：若两个不同字符串（如 `"ab"` 和 `"ba"`）在你的比较器下 `compare` 返回 `Equal`，会发生什么？

> **参考答案**：跳表会把它们视为「同一个键」。`insert("ab")` 后再 `insert("ba")` 不会新增节点（命中旧 key 的替换语义，u3-l10），`len` 不会增加，集合实际丢失元素——这是比较器**未能建立全序**导致的语义错误。合格的比较器必须对任意 `x != y` 给出确定的非 `Equal` 顺序。

**练习 3**：能否在集合构造后，运行时把比较器换成另一个？

> **参考答案**：不能。比较器是 `SkipSet<T, C>` 类型的一部分（结构体泛型字段 `C`），构造时通过 `with_comparator` 固化，之后所有读写都依赖它建立的全序。换比较器等于换类型，需要新建一个 `SkipSet` 并重新插入所有元素。

---

## 5. 综合实践

把本讲三块知识串起来：自定义比较器 + `SkipSet` 委托模型 + `Deref`/`FromIterator` 句柄语义。

**任务**：构造一个「长度优先、长度相同按字典序」的并发有序字符串集合，完成以下子任务并用断言验证。

**操作步骤**（示例代码，可放入 `examples/len_set_demo.rs`）：

```rust
// 示例代码：综合实践
use std::borrow::Borrow;
use std::cmp::Ordering;

use crossbeam_skiplist::SkipSet;
use crossbeam_skiplist::comparator::{Comparator, Equivalator};

struct LenThenLex;

fn len_then_lex(a: &str, b: &str) -> Ordering {
    a.len().cmp(&b.len()).then_with(|| a.cmp(b))
}

impl<L, R> Equivalator<L, R> for LenThenLex
where
    L: Borrow<str> + ?Sized,
    R: Borrow<str> + ?Sized,
{
    fn equivalent(&self, lhs: &L, rhs: &R) -> bool {
        len_then_lex(lhs.borrow(), rhs.borrow()).is_eq()
    }
}

impl<L, R> Comparator<L, R> for LenThenLex
where
    L: Borrow<str> + ?Sized,
    R: Borrow<str> + ?Sized,
{
    fn compare(&self, lhs: &L, rhs: &R) -> Ordering {
        len_then_lex(lhs.borrow(), rhs.borrow())
    }
}

fn main() {
    let s = SkipSet::with_comparator(LenThenLex);
    for w in ["apple", "fig", "banana", "kiwi", "cherry", "date", "grape"] {
        s.insert(w);
    }

    // 1) 全序遍历：长度升序，同长度字典序
    let all: Vec<&str> = s.iter().map(|e| *e.value()).collect();
    // 长度分组：3:[fig] 4:[date,kiwi] 5:[apple,grape] 6:[banana,cherry]
    assert_eq!(all, vec!["fig", "date", "kiwi", "apple", "grape", "banana", "cherry"]);

    // 2) Deref 语义：*entry 与 entry.value() 等价
    let front = s.front().unwrap();
    assert_eq!(*front, "fig");

    // 3) 自定义序下的命中与缺失
    assert!(s.contains("cherry"));
    assert!(!s.contains("zzzzz")); // 长度 5 但字典序最大，不在集合中

    // 4) 删除后句柄仍可安全读取其值（epoch 回收 + 引用计数，u2-l6/u4-l12）
    let removed = s.remove("apple").unwrap();
    assert_eq!(*removed, "apple");
    assert!(!s.contains("apple"));
    assert_eq!(s.len(), 6);

    // 5) 用集合自身的迭代器做一次范围式统计：长度 >= 5 的元素个数
    let long_count = s.iter().filter(|e| e.value().len() >= 5).count();
    // 剩余：fig,date,kiwi,grape,banana,cherry -> len>=5: grape(5),banana(6),cherry(6) = 3
    assert_eq!(long_count, 3);
}
```

**需要观察的现象与预期结果**：

1. `all` 严格符合「长度优先、同长度字典序」。
2. `*front` 直接给出 `"fig"`，证明 `Deref<Target = T>` 生效。
3. `contains("zzzzz")` 返回 `false`，证明查询走的是自定义比较器而非标准 `Ord`（否则 `"zzzzz"` 与集合元素的字典序关系会不同）。
4. `remove` 返回的句柄 `removed` 在 `drop` 之前仍可读值（`*removed == "apple"`），印证 set 句柄继承了 map 句柄的引用计数语义。
5. `long_count == 3`，印证迭代器同样遵循自定义序，且 `value()` 暴露的是元素本身。

**待本地验证**：在 `crossbeam-skiplist` 目录执行 `cargo run --example len_set_demo`，确认所有断言通过、无 panic。

**反思题（文字作答）**：

- 如果把 `LenThenLex::equivalent` 改成「只比较长度相等」，第 3 步的 `contains("cherry")` 是否一定命中真正的 `cherry`？为什么？请结合 [comparator.rs:62-63](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/comparator.rs#L62-L63) 的契约与 u3-l8 的「两步法」说明。
- 参考 [tests/set.rs:791-793](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/tests/set.rs#L791-L793) 的 `compare` 闭包写法，说明把比较逻辑集中在单一函数、让 `equivalent` 与 `compare` 同时委托给它，为什么是「从结构上保证契约」的最佳工程实践。

---

## 6. 本讲小结

- `SkipSet<T, C>` 本质是 `map::SkipMap<T, (), C>` 的 newtype 包装，全部方法都是单行委托，无任何独立的并发/内存逻辑；`()` 是 ZST，零开销。
- set 层的 bounds 完全继承自 map 层：读操作要 `C: Comparator<T>`（查询要 `Comparator<T, Q>`），写操作额外要 `T: Send + 'static`（epoch 回收要求）。
- `set::Entry::value()` 内部调的是 `self.inner.key()`——set 的「值」就是 map 的「键」；`Deref<Target = T>` 让 `*entry` 等价于 `entry.value()`，doctest 因此能写得像直接取值。
- `FromIterator` 用 `get_or_insert` 幂等收集（要求 `C: Default`）；`IntoIter::next` 用 `.map(|(k, ())| k)` 在消费时丢弃 unit 值。
- 自定义比较器通过 `SkipSet::with_comparator` 注入，会同时改变排序与判等；最稳妥的写法是让 `equivalent` 复用 `compare` 的结果（`.is_eq()`），从结构上满足「二者必须对相等达成一致」的契约。
- 真实样例 [tests/set.rs:727-749](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/tests/set.rs#L727-L749) 的 `DynComparator` 展示了「单一闭包同时驱动 `compare` 与 `equivalent`」的范式，可据此实现任意自定义序集合。

## 7. 下一步学习建议

- 若想理解「自定义比较器在搜索算法里如何被消费」，回看 u3-l8 的 `search_position`/`search_bound`，重点观察 `compare` 与 `equivalent` 在「定位」与「确认命中」两阶段的分工。
- 若想进一步研究句柄在并发下的生命周期与回收，继续阅读 u4-l12（Entry 与 RefEntry 双生命周期）和 u5-l18（并发语义与 Drop/IntoIter 的安全性）。
- 若对内存序与 epoch 回收的工程细节感兴趣，进入 u5-l17（内存序分析）和 u2-l6（epoch 内存回收与引用计数），它们能解释本讲中 `Entry` 句柄「drop 之前节点不被回收」的底层保证。
- 动手延伸：仿照本讲 `LenThenLex`，实现一个「大小写不敏感」的 `Comparator`（参考 [comparator.rs:24-47](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/comparator.rs#L24-L47) 的 `Equivalator` 文档示例），构造 `SkipSet<String, _>` 并验证 `"Abc"` 与 `"abc"` 被视为同一元素。
