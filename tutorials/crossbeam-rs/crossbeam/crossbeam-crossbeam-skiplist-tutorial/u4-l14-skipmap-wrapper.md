# SkipMap 高层封装：每次 pin 与 ManuallyDrop 释放

## 1. 本讲目标

本讲聚焦 `src/map.rs`，回答一个贯穿前几讲但始终被「藏起来」的问题：底层 `base::SkipList` 要求调用方自己持有 `epoch::Guard`、自己管理 `RefEntry` 的引用计数释放，而高层 `SkipMap` 的用户**完全看不到 `Guard`**，只需 `map.insert(k, v)`、`map.get(&k)` 就能拿到一个可以跨方法、跨线程长期持有的 `Entry`。这层「魔法」是怎么实现的？

学完本讲你应该能够：

1. 说清 `SkipMap` 如何把 `base::SkipList` 包一层，并用 `epoch::default_collector()` 接管内存回收。
2. 解释为什么 `SkipMap` 的**每一个方法**内部都要 `epoch::pin()` 拿一个临时 `Guard`，以及这个 `Guard` 为什么可以在方法返回前就丢掉。
3. 理解 `try_pin_loop` 在 `front`/`get`/`lower_bound`/`upper_bound` 等「读」操作里如何把 guard-bound 的 `base::Entry` 转成 refcount 的 `base::RefEntry`，以及为什么要重试。
4. 读懂 `map::Entry` 用 `ManuallyDrop<base::RefEntry>` 包装、并在 `Drop` 里调用 `release_with_pin(epoch::pin)` 的延迟释放技巧。

本讲是 u4 单元的第三篇，承接 u3-l10（插入路径）与 u1-l2（基本用法），并为 u4-l15（`SkipSet` 封装）打底。

## 2. 前置知识

本讲默认你已经读过以下讲义（或掌握等价知识）：

- **u1-l2**：知道 `SkipMap::insert/get/remove` 返回的是 `Entry` 句柄，且句柄活着节点就不会被回收。
- **u2-l6**：理解 epoch-based reclamation（EBR）的两道闸门——`Guard`（临界区内保护临时读）与引用计数（`refs_and_height` 高位，保护跨临界区长期持有）。
- **u3-l10**：理解 `insert_internal` 的两阶段插入，以及新节点初始引用计数为 2（一份给 level-0 链接，一份给返回的 entry）。
- **u4-l12/u4-l13**（可并行阅读）：理解 `base::Entry`（guard-bound）与 `base::RefEntry`（refcount）的本质区别，以及五种迭代器。

下面三个术语在本讲会反复出现，先统一口径：

| 术语 | 含义 |
|---|---|
| `Guard` | crossbeam-epoch 的临界区凭证。线程「pin」一次就进入一个 epoch，期间通过 `Guard` 加载到的 `Shared` 指针不会被回收。 |
| `Collector` | epoch 回收器的句柄。一个 `SkipList` 绑定一个 `Collector`，所有和它交互的 `Guard` 必须来自同一个 `Collector`（由 `check_guard` 校验）。 |
| pin（动词） | 在本讲有两个含义，**注意区分**：① `epoch::pin()` = 进入临界区拿 `Guard`；② `Entry::pin()` / `try_pin_loop` = 给节点引用计数 +1，把 guard-bound 句柄升级成 refcount 句柄。 |

> 阅读时若看到「pin 一次 Guard」指 ①；若看到「把 Entry pin 住」指 ②。

## 3. 本讲源码地图

本讲的核心源码是 `src/map.rs`，必要时回溯 `src/base.rs` 中的对应符号。

| 文件 | 关键符号 | 作用 |
|---|---|---|
| `src/map.rs` | `SkipMap`、`Entry`、`Iter`、`Range` | 高层封装：把 `base::SkipList` 包成易用 API |
| `src/base.rs` | `SkipList::new/with_comparator`、`check_guard` | 底层需要显式 `Collector` 与 `Guard` |
| `src/base.rs` | `Entry::pin`、`RefEntry::try_acquire`、`try_pin_loop` | guard-bound → refcount 的转换 |
| `src/base.rs` | `RefEntry::release_with_pin`、`NodeRef::decrement_with_pin` | refcount 释放，必要时才 pin |
| `src/base.rs` | `SkipList::insert` / `insert_internal` | 插入主链路（实践任务要跟踪） |

## 4. 核心概念与源码讲解

### 4.1 SkipMap 的封装骨架与 default_collector

#### 4.1.1 概念说明

底层 `base::SkipList<K, V, C>` 有一个让人不爽的约束：它的构造函数要求你**显式传入一个 `Collector`**，它的每一个方法都要求你**显式传入一个 `&Guard`**，而且这个 `Guard` 还必须来自构造时传入的那个 `Collector`（否则 `check_guard` 直接 panic）。

```rust
// base.rs —— 构造底层跳表必须自带 Collector
pub fn new(collector: Collector) -> Self { ... }
pub fn with_comparator(collector: Collector, comparator: C) -> Self { ... }
```

来源：[src/base.rs:487-489](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L487-L489) 与 [src/base.rs:494-505](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L494-L505)（`with_comparator` 同时初始化 `head`、`collector`、`hot_data`、`comparator` 四个字段）。

这意味着用 `base::SkipList` 写并发代码，用户要自己管 `Collector` 生命周期、自己 `epoch::pin()`、自己保证 `Guard` 来源一致——正确但繁琐。`SkipMap` 的全部价值就是把这些细节一次性吸收掉：

- **Collector 从哪来？** 直接用 `epoch::default_collector()`（全局默认回收器），不再让用户操心。
- **Guard 从哪来？** 每个方法内部自己 `epoch::pin()`，用完即弃。

#### 4.1.2 核心流程

`SkipMap` 的结构体本身极其单薄，就是一个 newtype 包装：

```rust
pub struct SkipMap<K, V, C = BasicComparator> {
    inner: base::SkipList<K, V, C>,
}
```

来源：[src/map.rs:28-30](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L28-L30)。注意它比 `base::SkipList` 少了 `head`/`collector`/`hot_data`/`comparator` 等字段——这些都「藏」在 `inner` 里。`C = BasicComparator` 是默认比较器（详见 u2-l7）。

两个构造函数的唯一区别，是是否由用户指定比较器，但二者都调用 `epoch::default_collector().clone()`：

```rust
impl<K, V> SkipMap<K, V> {
    pub fn new() -> Self {
        Self {
            inner: base::SkipList::new(epoch::default_collector().clone()),
        }
    }
}

impl<K, V, C> SkipMap<K, V, C> {
    pub fn with_comparator(comparator: C) -> Self {
        Self {
            inner: base::SkipList::with_comparator(epoch::default_collector().clone(), comparator),
        }
    }
}
```

来源：[src/map.rs:42-46](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L42-L46)（`new`）、[src/map.rs:59-63](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L59-L63)（`with_comparator`）。

`.clone()` 的意义：`default_collector()` 返回的是 `&'static Collector`（一个全局 `Lazy` 的引用），`.clone()` 把它复制成一个拥有所有权的 `Collector` 句柄（`Collector` 内部是 `Arc`，clone 只是增引用），存进 `inner.collector`。

> 为什么是「全局默认 Collector」而不是每个 `SkipMap` 自己造一个？因为 epoch 回收依赖**所有线程用同一个 Collector**才能正确推进 epoch、安全回收。全局默认 Collector 让任意线程的 `epoch::pin()` 拿到的 `Guard` 天然和 `SkipMap` 同源——这正是 `check_guard` 能通过的根源，也是 `SkipMap` 能在方法里随手 `epoch::pin()` 而不用传 `Collector` 的前提。

#### 4.1.3 源码精读：check_guard 为什么不会 panic

回顾底层 `check_guard` 的实现：

```rust
fn check_guard(&self, guard: &Guard) {
    if let Some(c) = guard.collector() {
        assert!(c == &self.collector);
    }
}
```

来源：[src/base.rs:526-530](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L526-L530)。

`SkipMap::new()` 用 `default_collector()` 构造 `inner`，`SkipMap` 的方法里又用 `epoch::pin()` 拿 `Guard`——而 `epoch::pin()` **正是用 `default_collector()` 来 pin 的**。两者同源，断言恒成立。这就是「高层封装能彻底隐藏 `Guard` 来源」的底层保证。

#### 4.1.4 代码实践

1. **实践目标**：确认 `SkipMap` 与底层 `base::SkipList` 在构造上的差异，并验证 `default_collector` 是全局共享的。
2. **操作步骤**：
   - 阅读 [src/map.rs:42-63](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L42-L63)，确认两个构造函数都调用 `epoch::default_collector().clone()`。
   - 写一段小程序（示例代码，非项目原有）：

     ```rust
     use crossbeam_skiplist::SkipMap;
     use crossbeam_epoch as epoch;

     fn main() {
         let map: SkipMap<&str, i32> = SkipMap::new();
         // 这两个 Guard 来自同一个 default_collector，因此可以安全用于 map 的内部检查
         let _g = epoch::pin();
         map.insert("a", 1);
         println!("len = {}", map.len());
     }
     ```
   - 用 `cargo build` 确认编译通过（`crossbeam_epoch` 需作为依赖；本项目 `Cargo.toml` 已包含）。
3. **需要观察的现象**：程序正常编译运行，`len = 1`。
4. **预期结果**：`SkipMap::new()` 不暴露 `Collector`，但内部 `epoch::pin()` 拿到的 `Guard` 与 `inner.collector` 同源，`check_guard` 不会 panic。
5. 若你把依赖里的 `crossbeam_epoch` 换成另一个独立 `Collector` 实例去 pin，则**无法**直接喂给 `base::SkipList`（会 panic）——但 `SkipMap` 用户根本没机会犯错，因为它把 `Guard` 全部内化了。

#### 4.1.5 小练习与答案

**练习 1**：`SkipMap::new()` 里的 `epoch::default_collector().clone()`，去掉 `.clone()` 能编译吗？为什么？

> **答案**：不能（或会引发生命周期问题）。`default_collector()` 返回 `&'static Collector`，而 `base::SkipList::new` 的签名要求传入**拥有所有权**的 `Collector`（`collector: Collector`）。`.clone()` 把 `&Collector` 复制成拥有所有权的 `Collector`（内部 `Arc` 增计数），才能存进 `inner` 字段。

**练习 2**：为什么 `SkipMap` 把 `Collector` 写死成 `default_collector`，而不是作为泛型参数暴露给用户？

> **答案**：暴露 `Collector` 会让用户承担「保证所有 `Guard` 同源」的负担，一旦用错就 panic。固定成全局默认 `Collector` 后，任意线程随手 `epoch::pin()` 都天然同源，封装才彻底。代价是所有 `SkipMap` 共享同一个全局回收器，垃圾回收的推进节奏是全局的（这也是 u2-l6 提到「全局 Collector 可能很晚才回收垃圾」的原因）。

---

### 4.2 每次方法调用都要 pin 一次 Guard

#### 4.2.1 概念说明

翻开 `SkipMap` 的任意一个方法，你会发现一个高度一致的模式：**第一行几乎都是 `let guard = &epoch::pin();`，然后把 `guard` 透传给 `inner` 的对应方法**。这不是巧合，而是 epoch 回收机制的硬性要求。

为什么必须 pin？因为底层 `base::SkipList` 是无锁的，节点随时可能被其他线程摘除并（在 epoch 推进后）释放。一个线程要安全地从原子指针里加载出一个 `Shared<Node>` 并解引用，必须处于某个 epoch 临界区内——也就是必须持有 `Guard`。没有 `Guard`，加载到的指针可能在解引用前就被回收，造成 use-after-free（详见 u2-l6）。

#### 4.2.2 核心流程：四类方法，一个套路

`SkipMap` 的方法按对 `Guard` 的用法可分四类：

| 类别 | 代表方法 | 模式 | 返回值 |
|---|---|---|---|
| 纯读（定位单个节点） | `front`/`back`/`get`/`lower_bound`/`upper_bound` | `pin` + `try_pin_loop` + `.map(Entry::new)` | `Option<Entry>` |
| 纯读（不返回句柄） | `contains_key`/`len`/`is_empty` | `pin` + 直接调用 | `bool`/`usize` |
| 写 | `insert`/`compare_insert`/`get_or_insert*`/`remove`/`pop_*`/`clear` | `pin` + 调用 + `.map(Entry::new)` | `Entry`/`Option<Entry>` |
| 迭代器构造 | `iter`/`range` | **不 pin**，返回 refcount 迭代器 | `Iter`/`Range` |

注意第四类的反差：`iter()` 与 `range()` 构造时**不 pin**，因为它们返回的迭代器每个 `next` 步骤自己 pin（详见 4.2.4 与 u4-l13）。这避免了「迭代器存活期间整个线程一直 pin 住」的代价。

#### 4.2.3 源码精读：insert 与 get 的对照

先看写操作 `insert`，它最直接，没有 `try_pin_loop`：

```rust
pub fn insert(&self, key: K, value: V) -> Entry<'_, K, V, C> {
    let guard = &epoch::pin();
    Entry::new(self.inner.insert(key, value, guard))
}
```

来源：[src/map.rs:403-406](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L403-L406)。三步：① `epoch::pin()` 拿 `Guard`；② 调底层 `inner.insert(key, value, guard)`；③ 把返回的 `base::RefEntry` 包成 `map::Entry`。

底层 `inner.insert` 又只是 `insert_internal` 的薄包装：

```rust
pub fn insert(&self, key: K, value: V, guard: &Guard) -> RefEntry<'_, K, V, C> {
    self.insert_internal(key, || value, |_| true, guard)
}
```

来源：[src/base.rs:1247-1249](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1247-L1249)。`insert_internal` 的细节在 u3-l10 已讲过：这里只需记住——它返回的 `RefEntry` **自带一份引用计数**（新节点初始计数为 2，一份给 level-0 链接、一份给返回的 entry）。所以 `insert` **不需要** `try_pin_loop`：节点是全新插入的，引用计数天然 ≥1，不存在「找到却被并发回收」的竞态。

再看读操作 `get`，模式多了一层 `try_pin_loop`：

```rust
pub fn get<Q>(&self, key: &Q) -> Option<Entry<'_, K, V, C>>
where
    C: Comparator<K, Q>,
    Q: ?Sized,
{
    let guard = &epoch::pin();
    try_pin_loop(|| self.inner.get(key, guard)).map(Entry::new)
}
```

来源：[src/map.rs:271-278](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L271-L278)。`inner.get` 返回的是 guard-bound 的 `base::Entry`（生命周期绑在 `guard` 上），必须经过 `try_pin_loop` 升级成 refcount 的 `RefEntry` 才能跨方法返回。这一步的细节是下一节 4.3 的主题。

`remove` 与 `insert` 同类（写操作，直接拿 `RefEntry`）：

```rust
pub fn remove<Q>(&self, key: &Q) -> Option<Entry<'_, K, V, C>>
where
    C: Comparator<K, Q>,
    Q: ?Sized,
{
    let guard = &epoch::pin();
    self.inner.remove(key, guard).map(Entry::new)
}
```

来源：[src/map.rs:456-463](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L456-L463)。底层 `remove` 内部已用 `try_acquire` 抢引用计数（详见 u3-l11），所以返回的 `RefEntry` 同样自带计数，无需 `try_pin_loop`。

> 一个高频疑问：`let guard = &epoch::pin();` 里 `epoch::pin()` 返回的是一个临时 `Guard`，对它取引用，`Guard` 不会立刻被 drop 吗？——这是 Rust 的**临时生命周期延展**（temporary lifetime extension）：绑定到 `let` 的引用，其指向的临时对象会活到当前作用域结束。所以这里的 `Guard` 会一直活到方法返回。4.4 节会看到 `Entry::drop` 里 `epoch::pin` 不绑 `let` 的另一种用法。

#### 4.2.4 源码精读：iter 不 pin 的反例

对照 `iter()`，它**不**在构造时 pin，而是把底层 refcount 迭代器直接包起来：

```rust
pub fn iter(&self) -> Iter<'_, K, V, C> {
    Iter {
        inner: self.inner.ref_iter(),
    }
}
```

来源：[src/map.rs:224-228](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L224-L228)。它选的是 `inner.ref_iter()`（refcount 版），而不是 `inner.iter(guard)`（guard-bound 版）。原因在 u4-l13 讲过：guard-bound 迭代器要求线程整个遍历期间一直 pin，而高层 `SkipMap` 不希望强迫用户长时间 pin；于是改用 refcount 迭代器，把 pin 推迟到每个 `next` 步骤：

```rust
fn next(&mut self) -> Option<Entry<'a, K, V, C>> {
    let guard = &epoch::pin();
    self.inner.next(guard).map(Entry::new)
}
```

来源：[src/map.rs:727-730](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L727-L730)。每一步独立 pin、用完即弃——这就是「每次 pin」模式在迭代器层面的体现。

#### 4.2.5 代码实践

1. **实践目标**：直观感受「每个方法 pin 一次」的开销模式，并对照 `iter` 的「每步 pin」。
2. **操作步骤**：
   - 阅读并对照 [src/map.rs:403-406](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L403-L406)（`insert`）与 [src/map.rs:727-730](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L727-L730)（`Iter::next`）。
   - 用 `grep` 在 `src/map.rs` 里数 `epoch::pin()` 的出现次数，确认「几乎每个公共方法都有一次」。
3. **需要观察的现象**：`epoch::pin()` 在 `map.rs` 中出现非常多次（一二十处），分布在几乎每个读写方法的首行与迭代器的 `next`/`next_back`/`Drop` 中。
4. **预期结果**：能总结出「`SkipMap` 的 pin 粒度是『一次方法调用』或『一次迭代步』，绝不跨多个操作」。这是它能保持 lock-free 活性的关键——任何线程都不会长时间霸占 epoch。
5. **待本地验证**：若想量化开销，可写一个微基准，对比「连续 100 万次 `map.get`」与「1 次 `iter` 遍历 100 万条」的耗时差异（`iter` 的 pin 次数也是 100 万次，但省掉了 `try_pin_loop` 的搜索开销）。

#### 4.2.6 小练习与答案

**练习 1**：`len()` 和 `is_empty()` 也需要 pin 吗？看源码它们的实现并没有 `epoch::pin()`，为什么？

> **答案**：不需要。`len()` 直接读 `self.inner.len()`，而底层 `len` 只是 `self.hot_data.len.load(Ordering::Relaxed)`（[src/base.rs:516-522](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L516-L522)）。它只读一个原子计数器，不解引用任何节点指针，因此不涉及 use-after-free 风险，不需要 `Guard` 保护。返回值本身也只是「近似值」（Relaxed）。

**练习 2**：为什么 `insert` 不用 `try_pin_loop`，而 `get` 必须用？

> **答案**：`insert` 插入的是全新节点，节点引用计数初始化时已为返回的 entry 预留了一份（初始计数 2），不可能在返回前被并发回收；`get` 找到的是已存在的节点，从「搜索到」到「拿到句柄」之间存在竞态窗口——别的线程可能在此期间把它删了，导致引用计数归零——所以必须用 `try_pin_loop` 重试（详见 4.3）。

---

### 4.3 try_pin_loop：从 guard-bound Entry 到 refcount RefEntry

#### 4.3.1 概念说明

这是本讲最微妙的一段。我们先回顾两类句柄的本质区别（u4-l12 详述）：

- `base::Entry<'a, 'g, K, V, C>`：**guard-bound**。它持有 `parent: &'a SkipList`、`node: NodeRef<'g>`、`guard: &'g Guard`。`key()`/`value()` 返回的引用生命周期是 `'g`——绑死在 `Guard` 上。`Guard` 一旦 drop，引用就失效。
- `base::RefEntry<'a, K, V, C>`：**refcount**。只持有 `parent` 与 `node`，没有 `Guard`。安全性靠节点引用计数保证。`key()`/`value()` 返回 `&'a K`/`&'a V`——绑在 `SkipList` 的生命周期上，可以长期持有。

问题来了：`SkipMap::get` 是一个方法，方法内部的 `Guard` 是局部变量，方法一返回 `Guard` 就 drop 了。如果 `get` 直接返回 `base::Entry`，那它返回的句柄会借用一个**已经 drop 的局部 `Guard`**——Rust 的借用检查器会直接拒绝编译（生命周期对不上）。

解决办法：在 `Guard` 还活着的时候，把 guard-bound 的 `base::Entry` **升级**成 refcount 的 `RefEntry`——也就是给节点的引用计数 +1。一旦计数 +1，节点就不再依赖 `Guard` 保护，可以安全跨方法返回。这个「升级」动作就是 `Entry::pin()`（pin 的第二个含义）。

#### 4.3.2 核心流程：pin 可能失败，所以要 loop

`Entry::pin()` 的实现是委托给 `RefEntry::try_acquire`：

```rust
pub fn pin(&self) -> Option<RefEntry<'a, K, V, C>> {
    unsafe { RefEntry::try_acquire(self.parent, self.node) }
}
```

来源：[src/base.rs:1516-1518](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1516-L1518)。注意它返回 `Option`——可能失败！原因看 `try_acquire`：

```rust
unsafe fn try_acquire(parent: &'a SkipList<K, V, C>, node: NodeRef<'_, K, V>) -> Option<Self> {
    if unsafe { node.try_increment() } {
        Some(RefEntry {
            parent,
            node: unsafe { NodeRef::new(node.ptr) },  // 把 node 的生命周期重绑到 'a（parent）
        })
    } else {
        None
    }
}
```

来源：[src/base.rs:1670-1682](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1670-L1682)。它调用 `node.try_increment()` 给引用计数 +1，**只有计数非零时才成功**。而 `try_increment` 在计数为 0 时返回 `false`：

```rust
unsafe fn try_increment(&self) -> bool {
    let mut refs_and_height = self.refs_and_height.load(Ordering::Relaxed);
    loop {
        // 计数为 0 → 节点已排队待删，再 +1 会 double-free
        if refs_and_height & !HEIGHT_MASK == 0 {
            return false;
        }
        let new_refs_and_height = refs_and_height
            .checked_add(1 << HEIGHT_BITS)
            .expect("SkipList reference count overflow");
        match self.refs_and_height.compare_exchange_weak(
            refs_and_height, new_refs_and_height,
            Ordering::Relaxed, Ordering::Relaxed,
        ) {
            Ok(_) => return true,
            Err(current) => refs_and_height = current,
        }
    }
}
```

来源：[src/base.rs:222-249](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L222-L249)。`refs_and_height & !HEIGHT_MASK` 取出引用计数高位（低 5 位是高度，见 u2-l5），若为 0 说明节点已被并发删除并排队等待回收，此时不能再 +1，否则会引发 double-free。

这就引出了**重试的必要性**：在 `get` 的搜索阶段找到节点 N，到 `pin()` 尝试给 N 加计数的瞬间，可能另一个线程已经把 N 删了（计数归零）。此时 `pin()` 返回 `None`。但「N 被删了」不代表 key 不存在——可能只是「搜索时存在、pin 时被删」的瞬时竞态。正确的做法是**重新搜索一次**。这正是 `try_pin_loop` 封装的逻辑：

```rust
pub(crate) fn try_pin_loop<'a: 'g, 'g, F, K, V, C>(mut f: F) -> Option<RefEntry<'a, K, V, C>>
where
    F: FnMut() -> Option<Entry<'a, 'g, K, V, C>>,
{
    loop {
        if let Some(e) = f()?.pin() {
            return Some(e);
        }
    }
}
```

来源：[src/base.rs:2332-2341](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L2332-L2341)。逐步拆解：

1. 调用 `f()` 重新执行搜索（对 `get` 而言就是 `|| self.inner.get(key, guard)`，它会重新跑一遍 `search_bound`）。
2. `f()?`：若搜索本身返回 `None`（key 真的不存在），用 `?` 直接向上传播 `None`，整体返回 `None`。
3. 搜到了 `base::Entry` 后调 `.pin()` 尝试升级：
   - 成功（`Some`）→ 返回 `RefEntry`，退出循环。
   - 失败（`None`，节点被并发删了）→ 回到循环开头，重新搜索。
4. 由于跳表保证有限步内总能收敛（被删的节点会被 `help_unlink` 摘除，搜索终会落到一个稳定的活节点上或确认 key 不存在），循环有限步终止——这是 lock-free 活性的体现（u3-l8 的协作式清理保证）。

> 注意 `try_pin_loop` 的签名 `'a: 'g`：它要求 `SkipList` 的生命周期 `'a` 比 `Guard` 的 `'g` 长。返回的 `RefEntry<'a, ...>` 只绑定 `'a`（绑在 `SkipList` 上），与 `'g` 无关——这正是「升级」后句柄能脱离 `Guard` 存活的关键。

#### 4.3.3 源码精读：front / lower_bound / upper_bound 都是同一套

`front`、`back`、`lower_bound`、`upper_bound` 全部套用 `try_pin_loop`，与 `get` 完全同构：

```rust
pub fn front(&self) -> Option<Entry<'_, K, V, C>> {
    let guard = &epoch::pin();
    try_pin_loop(|| self.inner.front(guard)).map(Entry::new)
}
```

来源：[src/map.rs:124-127](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L124-L127)。`back`（[src/map.rs:144-147](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L144-L147)）、`lower_bound`（[src/map.rs:306-313](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L306-L313)）、`upper_bound`（[src/map.rs:338-345](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L338-L345)）结构完全一致。

它们与 `insert`/`remove` 的对照能精确说明「为什么读操作需要 loop、写操作不需要」：

| 操作 | 底层返回 | 是否 loop | 原因 |
|---|---|---|---|
| `insert`/`compare_insert`/`get_or_insert*` | `RefEntry`（新建节点，计数已含一份） | 否 | 全新节点，无竞态 |
| `remove`/`pop_front`/`pop_back` | `Option<RefEntry>`（内部已 `try_acquire`，失败自重试） | 否 | 底层 `remove` 自带 `loop`（[src/base.rs:1283-1335](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1283-L1335)） |
| `front`/`back`/`get`/`lower_bound`/`upper_bound` | `base::Entry`（guard-bound） | **是** | 找到的节点可能被并发删，pin 可能失败 |

也就是说，`try_pin_loop` 专治「读已有节点」这一类——只有这类操作存在「找到却被并发删」的窗口。

#### 4.3.4 代码实践

1. **实践目标**：通过阅读源码，复述 `map.get(&k)` 在「找到节点→pin 失败→重试」这条路径上的完整动作。
2. **操作步骤**：
   - 顺序阅读四段源码：[src/map.rs:271-278](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L271-L278)（`get`）→ [src/base.rs:2332-2341](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L2332-L2341)（`try_pin_loop`）→ [src/base.rs:1516-1518](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1516-L1518)（`Entry::pin`）→ [src/base.rs:222-249](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L222-L249)（`try_increment`）。
   - 画出调用链（伪代码）：

     ```
     map.get(&k)
       └─ epoch::pin()  → guard
       └─ try_pin_loop(|| inner.get(&k, guard))
            └─ f() = inner.get(&k, guard)
                 └─ search_bound(...) → Option<base::Entry>
            └─ 若 None → 返回 None（key 不存在）
            └─ e.pin() = RefEntry::try_acquire(parent, node)
                 └─ node.try_increment()  // CAS 给 refs 高位 +1
                      └─ 计数==0 → false → pin 返回 None → 回到 loop 重搜
                      └─ 计数>0  → true  → pin 返回 Some(RefEntry) → 退出
       └─ .map(Entry::new) → Option<map::Entry>
     ```
   - 用文字解释：为什么 `try_pin_loop` 的退出条件是「`f()` 返回 `None`」或「`pin()` 返回 `Some`」，而不会死循环？
3. **需要观察的现象**：你能清晰说出三种终止情形——① key 不存在（`f()` 返回 `None`）；② pin 成功（拿到 `RefEntry`）；③ （隐含）无限重试仅在理论上的极端活锁下发生，实际由 `help_unlink` 保证收敛。
4. **预期结果**：理解「读操作的 loop 不是为了等锁，而是为了在无锁竞态后重新对齐到一致状态」。
5. **待本地验证**：想触发 pin 失败路径非常困难（需要极精确的并发时序），可阅读 `tests/base.rs` 中涉及 `RefEntry` 与 `release_with_pin` 的测试来间接验证语义（见 u5-l19）。

#### 4.3.5 小练习与答案

**练习 1**：`try_pin_loop` 里的 `f()?` 这个 `?`，处理的是哪种情况？如果改成 `if let Some(e) = f() { ... } else { return None; }` 行不行？

> **答案**：`f()?` 处理「搜索本身返回 `None`」——即 key 在跳表里不存在（或范围查询无结果）。改成 `if let Some(e) = f() { ... } else { return None; }` 完全等价，只是 `?` 是语法糖。两者都表示「搜索确认无结果时，整体返回 `None`，不再重试」——因为重试也只会再次确认不存在。

**练习 2**：假设把 `try_pin_loop` 换成「只调用一次 `f()` 和一次 `pin()`，失败就返回 `None`」，会在什么场景下产生错误行为？

> **答案**：在并发删除场景下会出错。线程 A 调用 `get(&k)`，搜索到节点 N（此时 key 确实存在）；紧接着线程 B 把 N 删除（计数归零，排队待回收）；A 的 `pin()` 因此失败返回 `None`。但「key=k 的条目」可能仍存在——比如 B 删的是旧版本，或此刻有其他线程重新插入了 k。只试一次会让 A 错误地认为「key 不存在」，而正确行为是重新搜索确认。`try_pin_loop` 的重试正是为了消除这种瞬时竞态带来的假阴性。

---

### 4.4 map::Entry = ManuallyDrop<RefEntry> 与 release_with_pin

#### 4.4.1 概念说明

读操作经过 `try_pin_loop` 拿到的是 `base::RefEntry`，写操作拿到的是 `base::RefEntry`——它们都**自带一份引用计数**。但 `base::RefEntry` 有一个让高层用户很容易踩坑的特性（u4-l12 已强调）：

> `RefEntry` **没有实现 `Drop`**。你必须显式调用 `release(guard)` 或 `release_with_pin(pin)` 来给引用计数 −1，否则节点永远泄漏（计数永远不归零，内存永不释放）。

原因是：释放 `RefEntry` 需要一个 `Guard`（因为计数归零时要 `defer_unchecked` 把 `finalize` 推迟到 epoch 安全），而 `RefEntry` 自己不带 `Guard`。把释放逻辑放进 `Drop` trait 会要求 `Drop` 里凭空变出一个 `Guard`——这可行，但需要谨慎设计。

`map::Entry` 正是把这个「凭空变出 `Guard`」的职责接了过来，让用户拿到一个**可以像普通值一样 drop** 的句柄，而不会泄漏。

#### 4.4.2 核心流程：ManuallyDrop + ptr::read + release_with_pin

先看 `map::Entry` 的定义：

```rust
pub struct Entry<'a, K, V, C = BasicComparator> {
    inner: ManuallyDrop<base::RefEntry<'a, K, V, C>>,
}
```

来源：[src/map.rs:597-599](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L597-L599)。`ManuallyDrop<T>` 是一个「包住 `T` 但禁用其自动析构」的包装器——即便 `T` 实现了 `Drop`，`ManuallyDrop<T>` 也不会自动 drop 内部的 `T`，除非你显式调 `ManuallyDrop::into_inner` 或 `unsafe` 手动 drop。这里用它有两层目的：

1. `base::RefEntry` 本身就**没有** `Drop`（它要求手动 release），所以即便不用 `ManuallyDrop`，自动析构也不会做事。但 `map::Entry` 需要在自己的 `Drop` 里**接管**释放逻辑，用 `ManuallyDrop` 可以确保「内部的 `RefEntry` 不会被任何其他机制意外 drop」——把释放权完全收归 `map::Entry::drop`。
2. `Drop::drop` 只拿到 `&mut self`，无法「消费」`self.inner`。要调用消费式的 `release_with_pin(self, ...)`，必须用 `ptr::read` 把 `RefEntry` 按位「搬」出来。

关键就在 `Drop for Entry`：

```rust
impl<K, V, C> Drop for Entry<'_, K, V, C> {
    fn drop(&mut self) {
        unsafe {
            ManuallyDrop::into_inner(ptr::read(&self.inner)).release_with_pin(epoch::pin);
        }
    }
}
```

来源：[src/map.rs:624-630](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L624-L630)。三步拆解：

1. `ptr::read(&self.inner)`：把 `self.inner`（类型是 `ManuallyDrop<RefEntry>`）按位复制一份出来，得到一个拥有所有权的 `ManuallyDrop<RefEntry>` 值。这是 unsafe 操作，因为现在内存里「逻辑上」有两份相同的 `RefEntry`（原位一份、读出来一份），如果两者都被释放，就会 double decrement。
2. `ManuallyDrop::into_inner(...)`：把读出来的 `ManuallyDrop<RefEntry>` 解包成 `RefEntry`（消费它）。
3. `.release_with_pin(epoch::pin)`：调用 `RefEntry::release_with_pin`，传入 `epoch::pin`（**注意传的是函数指针 `epoch::pin`，不是调用结果**）——这是「按需 pin」的关键，见 4.4.3。

那原位的 `self.inner` 怎么办？——它被 `ManuallyDrop` 包着，**不会**被自动 drop，所以原位的那份「逻辑重复」不会触发释放，不会 double decrement。这就是 `ManuallyDrop + ptr::read` 在 `Drop` 里的标准用法：用「禁用原位析构」来换取「在 `drop` 里消费 self」的能力。

#### 4.4.3 源码精读：release_with_pin 为什么「按需 pin」

`release_with_pin` 接收一个**产生 `Guard` 的闭包** `pin: F`，而不是一个现成的 `Guard`：

```rust
pub fn release_with_pin<F>(self, pin: F)
where
    F: FnOnce() -> Guard,
{
    unsafe { self.node.decrement_with_pin(self.parent, pin) }
}
```

来源：[src/base.rs:1661-1666](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1661-L1666)。它委托给 `NodeRef::decrement_with_pin`：

```rust
unsafe fn decrement_with_pin<F, C>(self, parent: &SkipList<K, V, C>, pin: F)
where
    F: FnOnce() -> Guard,
{
    if self.refs_and_height.fetch_sub(1 << HEIGHT_BITS, Ordering::Release)
        >> HEIGHT_BITS == 1
    {
        fence(Ordering::Acquire);
        let guard = &pin();                 // 只在计数 1→0 时才真的 pin！
        parent.check_guard(guard);
        unsafe { guard.defer_unchecked(move || Node::finalize(self.ptr.as_ptr())) }
    }
}
```

来源：[src/base.rs:309-324](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L309-L324)。这段是 u2-l6 的核心，本讲只强调**与高层封装相关的那个性能优化**：

- `fetch_sub(1 << HEIGHT_BITS, Ordering::Release)` 先把引用计数 −1。
- `>> HEIGHT_BITS == 1` 判断「减之前的旧值，其引用计数部分是否恰好为 1」——也就是「这次 −1 是否让计数从 1 变成 0」。只有这种情况节点才需要被回收。
- **绝大多数 drop 不会命中这个分支**：节点要么还在跳表里（至少有 in-list 那份计数），要么还有别的 `Entry` 句柄持有（clone 过）。此时 `pin()` 闭包**根本不会被调用**——零开销。
- 只有「我是最后一个引用」的 drop 才会执行 `let guard = &pin();`，临时 pin 一个 `Guard`，然后 `defer_unchecked` 把 `finalize`（析构 key/value + dealloc）推迟到 epoch 安全时执行。

这就是「按需 pin」：把 pin 的代价**从「每次 drop 都 pin」摊销到「仅最后一次 drop 才 pin」**。用数学直觉表达——若一个节点被 clone 了 \(k\) 次（共 \(k+1\) 份句柄，算上 in-list 那份共 \(k+2\) 处计数来源），那么只有 1 次 drop 会真正 pin，其余 \(k+1\) 次 drop 都只是廉价的原子 `fetch_sub`。pin 的期望次数为：

\[
\mathbb{E}[\text{pin 次数}] = 1 \ll \mathbb{E}[\text{drop 次数}] = O(1) \;/\; O(k)
\]

即 pin 摊销开销随 clone 次数增长趋向 0。

#### 4.4.4 对照：Iter / Range 的 Drop 也走 drop_impl(guard)

高层迭代器 `Iter`、`Range` 同样持有底层 refcount 迭代器（`base::RefIter`/`RefRange`），它们也必须在 `Drop` 里退计数。与 `Entry` 不同，迭代器退计数时**总是需要 `Guard`**（因为它要释放当前游标位置的计数，且后续可能还要 `drop_impl` 做清理），所以它的 `Drop` 直接 pin：

```rust
impl<K, V, C> Drop for Iter<'_, K, V, C> {
    fn drop(&mut self) {
        let guard = &epoch::pin();
        self.inner.drop_impl(guard);
    }
}
```

来源：[src/map.rs:749-754](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L749-L754)。`Range` 的 Drop 完全同构（[src/map.rs:809-819](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L809-L819)）。对照 `Entry::drop` 的「按需 pin」，能看出设计者区分了两类场景：

| 句柄 | Drop 策略 | 原因 |
|---|---|---|
| `Entry`（单个节点） | `release_with_pin`（按需 pin） | 单节点退计数，仅计数归零时才需 `Guard`，可优化 |
| `Iter`/`Range`（迭代器） | `drop_impl(epoch::pin())`（总是 pin） | 退计数逻辑更复杂（`drop_impl` 内部要做游标清理），统一 pin 更简单 |

#### 4.4.5 代码实践

1. **实践目标**：验证 `Entry` 在 drop 时正确退计数，且大量 clone 不会泄漏。
2. **操作步骤**：
   - 阅读三段源码：[src/map.rs:624-630](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L624-L630)（`Drop for Entry`）、[src/base.rs:1661-1666](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1661-L1666)（`release_with_pin`）、[src/base.rs:309-324](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L309-L324)（`decrement_with_pin`）。
   - 用 `Arc` 包装 value 来观测回收时机（示例代码，非项目原有）：

     ```rust
     use crossbeam_skiplist::SkipMap;
     use std::sync::Arc;

     fn main() {
         let map: SkipMap<i32, Arc<()>> = SkipMap::new();
         let v = Arc::new(());
         map.insert(1, v.clone());
         assert_eq!(Arc::strong_count(&v), 2); // 一份 v，一份在跳表里

         let e = map.get(&1).unwrap();
         assert_eq!(Arc::strong_count(&v), 2); // get 返回 Entry，但 Arc 还是同一份
         drop(e);                              // Entry drop → release_with_pin（按需 pin）

         map.remove(&1);                       // 标记删除 + 摘除
         // 注意：value 真正被 drop 发生在 epoch 推进之后，可能有延迟
         println!("after remove, count = {}", Arc::strong_count(&v));
     }
     ```
   - 把这段放进一个 binary（如 `examples/entry_drop.rs`），运行 `cargo run --example entry_drop`。
3. **需要观察的现象**：`drop(e)` 之后 `Arc` 强引用计数没有立刻降到 1，因为 `release_with_pin` 只是退掉**节点**的引用计数；真正的 `Arc<()>` value 要等节点 `finalize` 执行（epoch 推进后）才会被 drop。
4. **预期结果**：能观察到「value 的延迟回收」——这正是 epoch GC 的特征（u2-l6）。多次 clone `Entry` 后再全部 drop，最终计数会回落（epoch 推进后）。
5. **待本地验证**：精确的回收时机依赖全局 `Collector` 的 epoch 推进节奏，单次运行可能看不到计数立即下降。可在 `remove` 后插入一次 `epoch::pin().defer(|| {})` 之类的人为推进，或循环若干次后再观测。

#### 4.4.6 小练习与答案

**练习 1**：`Entry::drop` 里为什么是 `release_with_pin(epoch::pin)`（传函数），而不是 `release(epoch::pin())`（传值）？

> **答案**：`release_with_pin` 接收一个 `FnOnce() -> Guard` 闭包，**只有**在引用计数恰好从 1 变 0 时才会调用它。如果改成 `release(epoch::pin())`，则**每次** `Entry` drop 都会先 pin 一个 `Guard`（因为函数参数会先求值），白白付出 pin 代价。传函数实现「按需 pin」，把 pin 摊销到最后一次 drop，是关键的性能优化。

**练习 2**：`map::Entry` 为什么必须用 `ManuallyDrop`？直接 `struct Entry { inner: base::RefEntry }` 然后在 `Drop` 里调 `self.inner.release_with_pin(epoch::pin)` 行不行？

> **答案**：不行。`release_with_pin(self, ...)` 按值消费 `RefEntry`，但 `Drop::drop` 只拿到 `&mut self`，无法把 `self.inner` move 出来。用 `ManuallyDrop + ptr::read` 才能在 `drop` 里「按位搬出」`RefEntry` 并消费它，同时靠 `ManuallyDrop` 禁用原位析构避免 double decrement。（注：`base::RefEntry` 本身无 `Drop`，但 `ptr::read` 制造的逻辑重复仍需 `ManuallyDrop` 来保证原位不会被任何兜底逻辑二次释放，这是该 idiom 的标准写法。）

**练习 3**：`Entry` 实现了 `Clone`（[src/map.rs:676-682](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L676-L682)），它 clone 时做什么？多次 clone 后全部 drop 会泄漏吗？

> **答案**：`Entry::clone` 委托给 `base::RefEntry::clone`，后者调用 `Node::try_increment` 给引用计数 +1（[src/base.rs:1713-1724](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1713-L1724)）。每份 clone 各持一份计数，各自 drop 时退一份。只要每份都被 drop，计数最终归零，节点被回收，**不会泄漏**。这要求用户不要忘记让 clone 出来的 `Entry` 离开作用域——好在 `Entry` 有 `Drop`，离开作用域自动释放，不像 `base::RefEntry` 那样需要手动 release。

## 5. 综合实践

**任务**：跟踪一次 `map.insert("k", 42)` 的完整调用栈，并写一份说明文档解释两个核心问题。

### 步骤

1. **准备**：在 `examples/` 下新建 `trace_insert.rs`（示例代码）：

   ```rust
   use crossbeam_skiplist::SkipMap;

   fn main() {
       let map: SkipMap<&str, i32> = SkipMap::new();
       let _e = map.insert("k", 42);   // 跟踪这一次调用
       println!("len = {}", map.len());
   }
   ```

2. **静态跟踪调用栈**（纯阅读，不运行）：按下列顺序打开源码，逐层标注行号与作用：

   | 层 | 文件:行 | 函数 | 作用 |
   |---|---|---|---|
   | 1 | `src/map.rs:403-406` | `SkipMap::insert` | `epoch::pin()` 拿 `guard`，调 `inner.insert`，包 `Entry::new` |
   | 2 | `src/base.rs:1247-1249` | `base::SkipList::insert` | 转调 `insert_internal(key, \|\| value, \|_\| true, guard)` |
   | 3 | `src/base.rs` 的 `insert_internal` | 两阶段插入 | u3-l10 已详述：level-0 CAS 安装 + 向上建塔 |
   | 返回 | `src/map.rs:601-606` | `Entry::new` | 把 `base::RefEntry` 包成 `ManuallyDrop<RefEntry>` |

3. **回答两个核心问题**（写进 `trace_insert.md` 笔记，**不要**写进讲义目录之外的项目文件，可用本地草稿）：

   - **问题 A：为何 map 层每次调用都要 pin 一次 Guard？**
     - 提示：底层 `insert_internal` 在搜索/插入时要解引用原子指针加载的节点；没有 `Guard` 保护，这些指针可能已被并发回收。`Guard` 保护的是「方法执行期间」的指针访问安全性。方法返回前，新节点的引用计数已为返回的 entry 预留一份（初始计数 2），所以 `Guard` 可以放心丢弃——后续安全性由引用计数接管。

   - **问题 B：Entry 在 drop 时如何在没有显式 Guard 的情况下安全释放引用？**
     - 提示：`Entry::drop`（[src/map.rs:624-630](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L624-L630)）用 `ManuallyDrop + ptr::read` 搬出 `RefEntry`，调 `release_with_pin(epoch::pin)`。`decrement_with_pin`（[src/base.rs:309-324](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L309-L324)）先 `fetch_sub`，**只在计数 1→0 时**才调用传入的 `epoch::pin` 闭包拿 `Guard`，再 `defer_unchecked(finalize)`。绝大多数 drop 不命中该分支，零 pin 开销。

4. **运行验证**：`cargo run --example trace_insert`，确认输出 `len = 1`。

### 观察与预期

- 程序正常编译运行，输出 `len = 1`。
- `_e` 离开作用域时触发 `Entry::drop`，由于还有 in-list 那份计数（计数从 2 降到 1），`decrement_with_pin` **不**调用 `epoch::pin`，仅一次廉价 `fetch_sub`。
- `map` 离开作用域时 `Drop for SkipList` 用 `unprotected()` 释放整链（u3-l11）。

## 6. 本讲小结

- `SkipMap` 是 `base::SkipList` 的 newtype 包装（[src/map.rs:28-30](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L28-L30)），构造时固定用 `epoch::default_collector()`（[src/map.rs:42-63](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L42-L63)），使得方法内随手 `epoch::pin()` 拿到的 `Guard` 与 `inner.collector` 天然同源，`check_guard` 恒成立。
- **每个方法首行 `let guard = &epoch::pin();`** 是统一套路：`Guard` 保护搜索/插入期间解引用指针的安全性，方法返回前丢弃（[src/map.rs:403-406](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L403-L406) 等）。
- 读操作（`front`/`get`/`lower_bound`/`upper_bound`/`back`）多一层 `try_pin_loop`（[src/map.rs:124-127](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L124-L127)、[src/base.rs:2332-2341](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L2332-L2341)）：把 guard-bound 的 `base::Entry` 经 `Entry::pin`（`try_increment`）升级成 refcount 的 `RefEntry`；pin 失败（节点被并发删）则重搜。
- 写操作（`insert`/`remove`/`pop_*`）不需要 `try_pin_loop`：返回的 `RefEntry` 自带一份计数，无竞态窗口。
- `map::Entry = ManuallyDrop<base::RefEntry>`（[src/map.rs:597-599](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L597-L599)），`Drop` 用 `ManuallyDrop::into_inner(ptr::read(&self.inner)).release_with_pin(epoch::pin)`（[src/map.rs:624-630](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L624-L630)）按需退计数。
- **按需 pin** 是核心优化：`decrement_with_pin`（[src/base.rs:309-324](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L309-L324)）只在引用计数 1→0 时才调用 `epoch::pin` 闭包，把 pin 摊销到最后一次 drop；`Iter`/`Range` 的 `Drop` 则总是 pin（游标清理更复杂）。

## 7. 下一步学习建议

- **u4-l15（SkipSet 封装）**：`SkipSet<T, ()>` 是 `SkipMap<T, ()>` 的零开销特化，`set::Entry` 的 `Deref<Target=T>` 直接复用本讲的 `map::Entry`。学完本讲再读 `src/set.rs` 会非常轻松。
- **u4-l12（Entry 与 RefEntry 双生命周期）**：若你对 `base::Entry` 与 `base::RefEntry` 的生命周期细节（`'a: 'g`、`try_acquire` 里重绑 `NodeRef` 生命周期）还想深入，可回看该篇。
- **u5-l17（内存序分析）**：本讲多次出现 `Ordering::Release`/`Acquire`/`Relaxed`（如 `decrement_with_pin` 的 `fetch_sub(Release) + fence(Acquire)`），系统梳理留到内存序专题。
- **u5-l18（并发语义）**：本讲的 `try_pin_loop` 是「单操作原子、多操作非原子」中「单操作」内部如何容忍并发竞态的典型例子，结合该篇能更完整理解竞态与一致性的边界。
- 想动手实验：仿照本讲 4.4.5 的 `Arc` 观测法，写一个并发 clone + drop 的测试，用 `Arc::strong_count` 间接验证「按需 pin」是否真的只在最后一次 drop 触发回收。
