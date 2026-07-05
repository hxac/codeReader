# 快速上手：SkipMap 与 SkipSet 基本用法

## 1. 本讲目标

本讲承接 [u1-l1](./u1-l1-project-overview.md) 建立的全局观，从「知道是什么」过渡到「会写第一段代码」。学完本讲你应当能够：

- 用 `SkipMap` 写出 `insert / get / remove / contains_key / iter` 的完整小程序。
- 用 `SkipSet` 写出 `insert / contains / remove / iter` 的完整小程序。
- 说清楚为什么 `get` / `insert` 这些方法**返回的是 `Entry` 句柄而不是直接返回值**，以及 `key()` / `value()` 借用的是句柄内的数据而非拷贝。
- 把仓库自带的 `examples/simple.rs` 从注释状态恢复成一个可运行、可计时的小基准。

本讲只解决「怎么用」，不深入底层无锁算法；底层的搜索、删除标记、epoch 回收会在第三、第五单元展开。

## 2. 前置知识

在动手之前，先建立三个直觉（来自上一讲）：

1. **修改方法接收 `&self` 而非 `&mut self`**。这意味着声明变量时**不需要** `mut`，多线程可以同时调用 `insert` / `get`。内部的可变性由原子操作 + epoch 回收保证。
2. **单操作原子、多操作非原子**。一次 `insert` 是原子的，但「先 `insert` 再 `contains`」这两步之间可能被别的线程插队，这是竞态（race condition），是逻辑错误而不是内存错误。
3. **没有 `get_mut`**。如果需要修改某个 value，请把值包进锁里：`SkipMap<K, RwLock<V>>`。

另外要认识两个本讲会反复出现的类型：

- **`SkipMap<K, V>`**：有序并发 map，接口对标 `BTreeMap`。
- **`Entry<'a, K, V>`**：map 中一条记录的「句柄」。`get` / `insert` / `front` / `back` 都返回它，通过它的 `key()` / `value()` 拿到键和值的引用。它在内部维护引用计数，drop 时才归还。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/lib.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs) | crate 顶层文档。其中包含 `SkipMap` 与 `SkipSet` 的两段 doctest 基本用法示例，以及一段多线程 `scope` 并发插入示例，是本讲的主要教学素材。 |
| [src/map.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs) | `SkipMap` 与 `map::Entry` 的高层封装实现。本讲只看公开方法的签名与文档注释，不看底层。 |
| [src/set.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/set.rs) | `SkipSet` 的封装（内部复用 `SkipMap<T, ()>`），方法签名与 `SkipMap` 高度对应。 |
| [examples/simple.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/examples/simple.rs) | 仓库自带的（目前全部被注释的）小基准，插入 100 万条数据并测量插入与遍历耗时。 |

## 4. 核心概念与源码讲解

### 4.1 SkipMap 基本用法：insert / get / remove / contains_key / iter

#### 4.1.1 概念说明

`SkipMap<K, V>` 是一个**有序并发 map**。它的 API 设计刻意贴近 `std::collections::BTreeMap`，让你几乎可以「照搬」直觉：`insert` 插入、`get` 查找、`remove` 删除、`contains_key` 判存在、`iter` 遍历。最大的两个外观差异是：

- 声明变量**不需要 `mut`**，因为方法都是 `&self`。
- `get` / `insert` 返回的不是值本身，而是一个 `Entry` 句柄，需要再调 `.key()` / `.value()`。

#### 4.1.2 核心流程

一段最小的 `SkipMap` 程序流程如下：

```
SkipMap::new()            // 建表（用默认比较器 BasicComparator）
   │
   ├── insert(k, v)  ──► Entry      // 插入，返回新条目句柄
   ├── get(k)        ──► Option<Entry>   // 查找，返回句柄或 None
   ├── contains_key(k) ──► bool     // 仅判存在，开销比 get 小
   ├── remove(k)     ──► Option<Entry>   // 删除，返回被删条目句柄
   └── iter() / for entry in &map   // 按键升序遍历，每项是 Entry
```

遍历时**键按升序（对字符串即字典序）产出**，这是「有序」的体现。

#### 4.1.3 源码精读

顶层文档里 `SkipMap` 的 doctest 示例就是最佳入门材料，[src/lib.rs:167-199](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L167-L199) 说明了「插入 → 用 get 拿到 Entry → 通过 key()/value() 读 → remove → 升序遍历」的完整闭环。注意这几行：

- `let movie_reviews = SkipMap::new();` 没有 `mut`。
- `let pulp_fiction = movie_reviews.get("Pulp Fiction").unwrap();` 拿到 `Entry`。
- `assert_eq!(*pulp_fiction.key(), "Pulp Fiction");` —— `key()` 返回 `&K`，比较时对它解引用。
- `movie_reviews.remove("The Blues Brothers");` 删除后再 `get` 返回 `None`。
- `for entry in &movie_reviews { ... entry.key(); entry.value(); }` 升序遍历。

这些方法的真实签名在 [src/map.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs) 中，逐一对应：

- `insert`：[src/map.rs:403-406](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L403-L406) —— 返回新插入条目的 `Entry`。注意其文档（[src/map.rs:386-392](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L386-L392)）说明：若 key 已存在，会**先删旧值再插新值**。
- `get`：[src/map.rs:271-278](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L271-L278) —— 返回 `Option<Entry>`，键不存在时为 `None`。
- `contains_key`：[src/map.rs:247-254](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L247-L254) —— 返回 `bool`，比 `get` 更轻量（不构造句柄）。
- `remove`：[src/map.rs:456-463](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L456-L463) —— 返回被删条目的 `Option<Entry>`；其文档（[src/map.rs:438-445](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L438-L445)）强调：值**不会立刻释放**，要等所有引用消失后才 drop（epoch 回收，第五单元细讲）。
- `iter`：[src/map.rs:224-228](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L224-L228) —— 返回按 key 升序的 `Iter`。

#### 4.1.4 代码实践

实践目标：亲手跑通 `SkipMap` 的增删查改与升序遍历。

操作步骤：

1. 在 `crossbeam-skiplist/` 目录下新建一个临时二进制（例如 `examples/try_map.rs`，可被 `cargo run --example try_map` 直接识别）。
2. 写入下面这段「示例代码」（基于 lib.rs doctest 改写，**非仓库原有文件**）：

   ```rust
   // 示例代码：examples/try_map.rs
   use crossbeam_skiplist::SkipMap;

   fn main() {
       let scores = SkipMap::new(); // 注意没有 mut
       scores.insert("alice", 90);
       scores.insert("bob", 75);
       scores.insert("carol", 88);

       // get 返回 Entry，再调 value() 拿引用
       let bob = scores.get("bob").unwrap();
       assert_eq!(*bob.value(), 75);

       // contains_key 仅判存在
       assert!(scores.contains_key("alice"));
       assert!(!scores.contains_key("dave"));

       // remove 返回被删条目
       let removed = scores.remove("bob").unwrap();
       assert_eq!(*removed.value(), 75);
       assert!(scores.get("bob").is_none());

       // 升序遍历：carol, alice（按字典序应为 alice, carol）
       let keys: Vec<&str> = scores.iter().map(|e| *e.key()).collect();
       assert_eq!(keys, vec!["alice", "carol"]);
   }
   ```

3. 运行 `cargo run --example try_map`（在 `crossbeam-skiplist/` 目录下）。

需要观察的现象 / 预期结果：程序无 panic 退出；`assert_eq!` 全部通过；遍历得到的 key 列表是 `["alice", "carol"]`（字典序）。若你把插入顺序打乱，遍历顺序仍然按 key 升序——这就是「有序 map」。

> 说明：示例代码中变量名与本讲无关，仅用于演示；仓库本身并没有 `examples/try_map.rs`，需要你自行创建。运行结果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：把上面示例里的 `let scores = SkipMap::new();` 改成 `let mut scores = SkipMap::new();`，程序还能编译运行吗？为什么？

> **答案**：能。`mut` 只是允许变量自身被重新赋值；`SkipMap` 的方法本来就接收 `&self`，加不加 `mut` 都能调用。这也是为什么文档示例刻意不加 `mut`——它不是必需的。

**练习 2**：对同一个 key 连续 `insert` 两次不同的 value，`get` 最终看到的是哪个值？为什么？

> **答案**：看到第二次的值。`insert` 文档（[src/map.rs:386-392](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L386-L392)）明确说明：key 已存在时会先删除旧条目再插入新条目，所以是「替换」语义。

### 4.2 SkipSet 基本用法：insert / contains / remove / iter

#### 4.2.1 概念说明

`SkipSet<T>` 是有序并发 **set**（集合），对标 `BTreeSet`。它只存「键」，没有独立的值。它的实现窍门是内部复用一个 `SkipMap<T, ()>`（详见 [u4-l15](./u4-l15-skipset-and-custom-comparator.md)），所以方法名几乎与 `SkipMap` 一一对应：

| `SkipMap` | `SkipSet` | 含义 |
| --- | --- | --- |
| `insert(k, v)` | `insert(t)` | 插入 |
| `get(k)` | `get(t)` / `contains(t)` | 查找 / 判存在 |
| `remove(k)` | `remove(t)` | 删除 |
| `entry.value()` | `entry.value()` | set 的 `value()` 返回的就是元素本身 |

#### 4.2.2 核心流程

```
SkipSet::new()
   │
   ├── insert(t)     ──► Entry          // 插入元素，返回句柄
   ├── contains(t)   ──► bool            // 判存在
   ├── get(t)        ──► Option<Entry>   // 查找句柄
   ├── remove(t)     ──► Option<Entry>   // 删除
   └── iter() / for entry in &set        // 升序遍历，entry.value() 即元素
```

同样地，遍历**按升序**产出。

#### 4.2.3 源码精读

`SkipSet` 的 doctest 示例在 [src/lib.rs:202-229](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L202-L229)，演示了「插入 → contains 判存在 → remove → 升序遍历」。要点：

- `books.contains("The Winds of Winter")` 返回 `bool`；`books.len()` 给出元素个数。
- 删除用 `books.remove("To Kill a Mockingbird")`。
- 遍历时 `entry.value()` 直接拿到元素（set 没有单独的 value 概念，`value()` 即 key）。

`SkipSet` 的方法签名在 [src/set.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/set.rs)：

- `contains`：[src/set.rs:148](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/set.rs#L148) —— 返回 `bool`。
- `get`：[src/set.rs:167](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/set.rs#L167) —— 返回 `Option<Entry>`。
- `insert`：[src/set.rs:323](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/set.rs#L323) —— 返回新条目 `Entry`。
- `remove`：[src/set.rs:342](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/set.rs#L342) —— 返回被删条目 `Option<Entry>`。
- `Entry::value`：[src/set.rs:488](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/set.rs#L488) —— 返回 `&T`。

#### 4.2.4 代码实践

实践目标：用 `SkipSet` 实现一个「去重 + 排序输出」的小工具。

操作步骤：新建 `examples/try_set.rs`（示例代码，非仓库原有文件）：

```rust
// 示例代码：examples/try_set.rs
use crossbeam_skiplist::SkipSet;

fn main() {
    let set = SkipSet::new();
    for w in ["banana", "apple", "cherry", "apple", "banana"] {
        set.insert(w);
    }

    // 去重后只剩 3 个；contains 可判存在
    assert_eq!(set.len(), 3);
    assert!(set.contains("apple"));
    assert!(!set.contains("durian"));

    // 升序输出：apple, banana, cherry
    let ordered: Vec<&str> = set.iter().map(|e| *e.value()).collect();
    assert_eq!(ordered, vec!["apple", "banana", "cherry"]);

    set.remove("banana");
    assert_eq!(set.iter().map(|e| *e.value()).collect::<Vec<_>>(),
               vec!["apple", "cherry"]);
}
```

需要观察的现象 / 预期结果：重复插入同一个元素不会增加 `len()`；遍历结果按字典序；删除后该元素不再出现。运行结果待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`SkipSet` 的 `get` 和 `contains` 都能判断元素是否存在，二者有什么区别？什么时候该用哪个？

> **答案**：`contains` 返回 `bool`，开销小；`get` 返回 `Option<Entry>`，会构造一个引用计数句柄，开销略大但能继续调用 `value()` / `next()` / `remove()` 等。只判断存在用 `contains`；拿到句柄做后续操作用 `get`。

**练习 2**：为什么 `SkipSet::Entry` 也有 `value()` 方法，而且它返回的就是元素本身？

> **答案**：`SkipSet` 内部是 `SkipMap<T, ()>`，set 的「元素」对应 map 的「key」。为了 API 一致，set 的 `Entry::value()` 直接返回该元素（即底层 map 的 key）的引用，详见 [u4-l15](./u4-l15-skipset-and-custom-comparator.md)。

### 4.3 Entry 句柄设计：为什么返回的是句柄而不是值

#### 4.3.1 概念说明

`BTreeMap::get` 返回 `Option<&V>`，直接借用容器内的值。而 `SkipMap::get` 返回 `Option<Entry>`，必须再 `.value()` 才拿到 `&V`。这层「多绕一圈」的设计是并发数据结构的关键：

- **借用容器**（`&V`）在并发场景下行不通——你拿到 `&V` 的瞬间，另一个线程可能把这条记录删了，于是引用悬空，造成 use-after-free。
- **句柄 + 引用计数**则不同：`Entry` 内部持有一个**引用计数**，只要句柄还活着，对应节点就不会被回收，即使它已经从逻辑上被删除。这正是 epoch 回收能安全工作的前提（详见 [u2-l6](./u2-l6-epoch-gc-and-refcount.md)）。

所以「返回 `Entry`」不是 API 啰嗦，而是**用引用计数替代对容器的借用**，从而在无锁并发下仍保证内存安全。

#### 4.3.2 核心流程

```
get / insert / front / back
        │
        ▼
   Option<Entry<'a, K, V>>      ← 句柄，内部维护引用计数
        │
        ├── key()   → &K        ← 借用句柄内的键（不是拷贝）
        ├── value() → &V        ← 借用句柄内的值（不是拷贝）
        ├── is_removed() → bool ← 这条记录是否已被删除
        ├── next() / prev()     ← 游标移动，返回新句柄
        └── remove() → bool     ← 通过句柄删除自己
        │
   drop(Entry)
        │
        ▼
   引用计数 -1；归零后由 epoch 延迟回收
```

`Entry` 句柄本身是 `Clone` 的（[src/map.rs:676-682](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L676-L682)），每 clone 一次就增加一次引用计数。

#### 4.3.3 源码精读

`map::Entry` 的定义在 [src/map.rs:596-622](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L596-L622)：

- `pub struct Entry<'a, K, V, C = BasicComparator>` —— 生命周期 `'a` 绑定到 `SkipMap`。
- `key()` ([src/map.rs:609-611](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L609-L611)) 返回 `&K`，`value()` ([src/map.rs:614-616](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L614-L616)) 返回 `&V`——**借用而非拷贝**，所以 doctest 里写的是 `*pulp_fiction.value()`（解引用后再比较）。
- `is_removed()` ([src/map.rs:619-621](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L619-L621)) 反映「逻辑删除」状态。

句柄的 `Drop` 实现特别值得注意，[src/map.rs:624-630](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L624-L630)：它用 `ManuallyDrop` 包裹底层 `RefEntry`，drop 时调用 `release_with_pin(epoch::pin)` 来归还引用计数。这正是「句柄活着 → 节点不被回收」的落点，具体机制留到 [u4-l12](./u4-l12-entry-refentry-lifetimes.md) 与 [u4-l14](./u4-l14-skipmap-wrapper.md) 精读。

另外两个常用句柄方法：`next()` ([src/map.rs:649-652](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L649-L652)) 与 `prev()` ([src/map.rs:655-658](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L655-L658)) 可以把句柄当成游标，向前后移动；`Entry::remove()` ([src/map.rs:670-673](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L670-L673)) 返回 `bool`，表示这次调用是否真正完成了删除。

#### 4.3.4 代码实践

实践目标：体会「句柄活着 → 值仍可访问」这一性质，与 `BTreeMap` 形成对比直觉。

操作步骤：新建 `examples/try_entry.rs`（示例代码，非仓库原有文件）：

```rust
// 示例代码：examples/try_entry.rs
use crossbeam_skiplist::SkipMap;

fn main() {
    let map = SkipMap::new();
    map.insert("k", 1);

    // 拿到句柄后，即使从 map 删除该 key，句柄仍能访问旧值。
    let entry = map.get("k").unwrap();
    map.remove("k");                 // 逻辑删除
    assert!(!map.contains_key("k")); // map 里已经没有了
    println!("entry still sees: {}", entry.value()); // 句柄仍可读到 1
    println!("is_removed: {}", entry.is_removed());  // true
}
```

需要观察的现象 / 预期结果：`map.contains_key("k")` 为 `false`，但 `entry.value()` 仍返回 `&1`，`entry.is_removed()` 为 `true`。句柄 `entry` 在 `main` 结束时 drop，届时引用计数归零，节点才被纳入 epoch 回收。运行结果待本地验证。

> 思考题（不必写代码）：如果是 `BTreeMap`，`get` 拿到的 `&V` 借用了 map，你根本无法在持有 `&V` 的同时调用 `&mut self` 的删除——编译器会拒绝。`SkipMap` 用「句柄 + 引用计数」绕开了这个限制，代价是每次访问多一次间接与计数维护。

#### 4.3.5 小练习与答案

**练习 1**：`Entry::key()` 返回的是 `K` 还是 `&K`？为什么这样设计？

> **答案**：返回 `&K`（借用）。因为键可能很大（如 `String`），拷贝代价高；而且句柄本身已经持有节点，借用其内存零成本。所以 doctest 里写 `*pulp_fiction.key()`。

**练习 2**：一个 `Entry` 被 `clone()` 后，再 `drop` 其中一个，节点会被回收吗？

> **答案**：不会。`clone` 增加引用计数，`drop` 减少引用计数；只有当所有 clone 都 drop、计数归零后，节点才会被 epoch 延迟回收。

### 4.4 examples/simple.rs：性能测量小基准

#### 4.4.1 概念说明

仓库自带一个性能示例 `examples/simple.rs`，目前**整段被注释掉**。它的作用是：插入 100 万条 `(u64, u64)` 数据，分别测量「插入」与「遍历」的耗时，并方便地把底层换成 `BTreeMap` / `HashMap` 做横向对比。它是本讲综合实践的素材，也是后续 [u1-l4](./u1-l4-build-test-bench.md) 基准学习的引子。

#### 4.4.2 核心流程

被注释的逻辑等价于：

```
map = SkipMap::new()         // 或 BTreeMap / HashMap
now = Instant::now()
for 1_000_000:
    num = num.wrapping_mul(17).wrapping_add(255)   // 伪随机递推
    map.insert(num, !num)
打印 insert 耗时

now = Instant::now()
for _ in map.iter() {}        // 完整遍历一次
打印 iterate 耗时
```

`num = num.wrapping_mul(17).wrapping_add(255)` 是一个简单的线性同余递推，用来生成「看起来随机」的 u64 序列，避免顺序插入让跳表 / B 树都处于最优路径而失真。

#### 4.4.3 源码精读

完整（被注释的）内容见 [examples/simple.rs:1-26](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/examples/simple.rs#L1-L26)。关键点：

- 第 1 行 `// use std::time::Instant;` 与第 4 行 `// let map = ...SkipMap::new();` 都被注释。
- 第 5、6 行留了 `BTreeMap` / `HashMap` 的注释行，方便你切换对比。
- 第 10-14 行是 100 万次插入循环。
- 第 16-17 行用 `Instant::now()` 差值换算秒数打印，`subsec_nanos()` 处理纳秒部分。
- 第 21 行 `for _ in map.iter() {}` 是一次完整遍历。

> 注意：当前 `main()` 体是空的（第 3-25 行全是注释），所以 `cargo run --example simple` 现在不会有任何输出。恢复方法见下面的实践。

#### 4.4.4 代码实践

实践目标：把 `examples/simple.rs` 恢复成可运行版本并计时。

操作步骤：

1. 打开 `examples/simple.rs`，去掉第 1 行 `use std::time::Instant;` 与第 4-24 行 `main` 内部的注释符号 `//`，使其成为正常代码（保持用 `SkipMap`）。
2. 在 `crossbeam-skiplist/` 目录运行 `cargo run --release --example simple`（用 release 让计时更有意义）。
3. 把第 5 行换成 `let mut map = std::collections::BTreeMap::new();`（同时取消注释），重新运行对比。

需要观察的现象 / 预期结果：终端先打印 `insert: X sec`，再打印 `iterate: X sec`。`SkipMap` 的插入通常比 `BTreeMap` 慢一些（无锁有额外开销），但在多线程并发写时 `SkipMap` 优势才显现——这正是顶层文档「Performance versus B-trees」一节（[src/lib.rs:127-142](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L127-L142)）强调的：单线程顺序写 `BTreeMap` 常更快，并发写才轮到跳表发挥。具体数值待本地验证。

> 提醒：修改 `examples/simple.rs` 属于练习操作；若你想保持仓库干净，练习完成后可用 `git checkout examples/simple.rs` 还原。

#### 4.4.5 小练习与答案

**练习 1**：为什么这个基准要用伪随机递推 `num.wrapping_mul(17).wrapping_add(255)`，而不是直接 `for i in 0..1_000_000` 顺序插入？

> **答案**：顺序插入会让跳表与 B 树都命中「最理想」的访问模式，掩盖真实差异；伪随机打乱顺序更接近真实负载，对比才有意义。

**练习 2**：为什么建议用 `--release` 而不是默认的 debug 构建来计时？

> **答案**：debug 构建未做优化（且含溢出检查等），测得的耗时主要反映「debug 开销」而非数据结构本身的性能，结论会失真。基准必须用 release。

## 5. 综合实践

把本讲三块内容串起来，完成下面这个贯通任务：

**任务**：在 `examples/` 下新建一个示例 `bench_concurrent.rs`，完成两件事。

1. **单线程大批量 + 计时**：参考 `examples/simple.rs` 的恢复版本，插入 100 万条 `(u64, u64)`（用同样的 `wrapping_mul(17).wrapping_add(255)` 递推），分别打印 `SkipMap` 的 insert 耗时与 iterate 耗时。

2. **两线程并发插入**：仿照 [src/lib.rs:16-41](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L16-L41) 的 `scope` 示例，用 `crossbeam_utils::thread::scope` 起两个线程，向同一个 `SkipMap` 各插入若干不同 key 的数据（注意线程间不要插入同一个 key，避免触发 insert 的「替换」语义干扰计数）。`scope` 返回后，断言 `map.len()` 等于两线程插入总数，并用 `iter()` 升序遍历确认结果可被读取。

参考框架（示例代码，非仓库原有文件，需自行补全）：

```rust
// 示例代码：examples/bench_concurrent.rs
use std::time::Instant;
use crossbeam_skiplist::SkipMap;
use crossbeam_utils::thread::scope;

fn main() {
    // ---- 第 1 部分：单线程 100 万插入 + 遍历计时 ----
    let map = SkipMap::new();
    let now = Instant::now();
    let mut num = 0u64;
    for _ in 0..1_000_000 {
        num = num.wrapping_mul(17).wrapping_add(255);
        map.insert(num, !num);
    }
    let d = Instant::now() - now;
    println!("insert: {:.3} sec", d.as_secs_f64());

    let now = Instant::now();
    for _ in map.iter() {}
    let d = Instant::now() - now;
    println!("iterate: {:.3} sec", d.as_secs_f64());

    // ---- 第 2 部分：两线程并发插入 ----
    let map2 = SkipMap::new();
    scope(|s| {
        s.spawn(|_| {
            for i in 0..50_000u64 { map2.insert(i, i); }
        });
        s.spawn(|_| {
            for i in 50_000u10000 { map2.insert(i, i); } // 注意：这里故意留空类型，需你改成正确的范围
        });
    }).unwrap();

    // 两线程插入的 key 互不重叠，len 应等于总数
    assert_eq!(map2.len(), 100_000);
    // 升序遍历确认可读
    let first = map2.iter().next().unwrap();
    assert_eq!(*first.key(), 0);
    println!("concurrent insert ok, len = {}", map2.len());
}
```

> 上面第 2 部分的第二个线程循环留了一处需要你修正的类型笔误（`50_000u10000` 不合法），请改成正确的写法（例如让线程二插入 `50_000..100_000`）。修正后用 `cargo run --release --example bench_concurrent` 运行。预期：打印两条耗时，最后打印 `concurrent insert ok, len = 100000`。运行结果待本地验证。

完成本任务后，你就把「单线程用法 → Entry 句柄 → 并发插入 → 计时」这条主线完整跑通了。

## 6. 本讲小结

- `SkipMap` 的 `insert / get / remove / contains_key / iter` 与 `BTreeMap` 高度相似，但变量不需要 `mut`，且 `get` / `insert` 返回 `Entry` 句柄。
- `SkipSet` 的 `insert / contains / get / remove / iter` 与 `SkipMap` 一一对应，内部复用 `SkipMap<T, ()>`；set 的 `Entry::value()` 即元素本身。
- 遍历一律按 key 升序（字符串即字典序），这是「有序容器」的核心性质。
- 操作返回 `Entry` 而非直接返回值，本质是用**引用计数替代对容器的借用**，从而在无锁并发下避免 use-after-free；句柄活着，节点就不会被回收。
- `key()` / `value()` 返回的是**借用**（`&K` / `&V`），不是拷贝，比较大小时需要解引用。
- `examples/simple.rs` 是一个被注释的 100 万条插入 + 遍历计时小基准，是本讲综合实践与下一讲基准学习的素材。

## 7. 下一步学习建议

- 想系统对比 `SkipMap` 与 `BTreeMap` / `HashMap` 的性能，进入 [u1-l4 构建测试与基准对比](./u1-l4-build-test-bench.md)，阅读 `benches/` 下四组基准。
- 想理解「目录怎么分层、feature 怎么门控」，进入 [u1-l3 目录结构与 feature 门控](./u1-l3-module-structure-and-features.md)，理清 `base → map → set` 的包装关系。
- 对「句柄为什么能在删除后仍访问值」好奇，可以提前跳到 [u2-l6 epoch 内存回收与引用计数](./u2-l6-epoch-gc-and-refcount.md) 与 [u4-l12 Entry 与 RefEntry 双生命周期](./u4-l12-entry-refentry-lifetimes.md)。
