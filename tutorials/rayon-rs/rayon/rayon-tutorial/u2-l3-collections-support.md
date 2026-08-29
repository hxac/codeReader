# 集合类型的并行支持

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出八大标准集合（`HashMap`、`HashSet`、`BTreeMap`、`BTreeSet`、`VecDeque`、`LinkedList`、`BinaryHeap`，以及上一讲已讲过的 `Vec`/切片）各自对应的并行迭代入口：`into_par_iter`、`par_iter`、`par_iter_mut`、`par_drain`。
2. 理解一个重要事实并纠正一个常见误会：**当前版本的 Rayon 中 map 类集合的并行迭代产出的是 `(K, V)` 元组，并不存在 `par_keys`/`par_values` 方法**（经源码与 git 历史确认，这两个方法在本仓库中从未存在过），投影键或值要靠 `.map(|(k, v)| ...)`。
3. 看懂 collections 模块的两种实现策略：「先转成 `Vec` 再并行」（哈希家族、B 树家族、链表）与「零拷贝借用内部切片」（`VecDeque`、`BinaryHeap` 的引用迭代），以及它们如何用 `delegate_iterator!` / `delegate_indexed_iterator!` 两个宏复用 `Vec` 与切片已经写好的生产者。
4. 了解集合不仅能当并行数据源（`FromParallelIterator` 让集合也能作为 `collect` 的目标）。

## 2. 前置知识

本讲建立在 u2-l1 和 u2-l2 的认知之上，先快速回顾三个概念：

- **`IntoParallelIterator` 与三个入口**：`into_par_iter()` 按值消费集合；`par_iter()` 只是 `(&集合).into_par_iter()` 的语法糖，产出共享引用；`par_iter_mut()` 产出可变引用。判别某类型有没有 `par_iter`，只看 `&Self` 是否实现了 `IntoParallelIterator`。
- **`Sync` / `Send` 约束规则**（u2-l2 已总结）：共享读要求元素 `Sync`，可变写要求 `Send`，按值移动要求 `Send`。对 map 类集合这意味着：`par_iter` 要求 `K: Sync + V: Sync`，`par_iter_mut` 要求 `K: Sync + V: Send`（键只读共享），`into_par_iter` 要求 `K: Send + V: Send`。
- **有索引 vs 无索引**（u2-l1）：`IndexedParallelIterator` 额外提供 `len`/`drive`/`with_producer`，解锁 `zip`、`enumerate`、`with_min_len` 等操作；无索引迭代器只能走 `drive_unindexed`。本讲会看到：同样是「转成 `Vec`」实现，有的集合暴露了索引能力，有的没有。

另外补充一个本讲要用的术语——**委托（delegate）**：当一个类型内部已经包含一个实现了某 trait 的字段时，就可以把方法调用原样转发给这个字段，自己一行逻辑都不写。Rust 中这种「包装 + 转发」可以用宏批量生成，Rayon 把它做成了 `src/delegate.rs` 里的两个宏，是整个 collections 模块只用了不到 500 行就覆盖七种集合的关键。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/collections/mod.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/mod.rs) | collections 模块入口：定义 `into_par_vec!` 宏与 `DrainGuard` 排空辅助结构 |
| [src/collections/hash_map.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/hash_map.rs) | `HashMap` 的四个并行迭代器：`IntoIter`/`Iter`/`IterMut`/`Drain` |
| [src/collections/hash_set.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/hash_set.rs) | `HashSet` 的 `IntoIter`/`Iter`/`Drain`（无可变迭代） |
| [src/collections/btree_map.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/btree_map.rs) | `BTreeMap` 的 `IntoIter`/`Iter`/`IterMut` |
| [src/collections/btree_set.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/btree_set.rs) | `BTreeSet` 的 `IntoIter`/`Iter`（无可变迭代） |
| [src/collections/vec_deque.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/vec_deque.rs) | `VecDeque` 的迭代器与范围排空，是本模块中实现最丰富的一个文件 |
| [src/collections/binary_heap.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/binary_heap.rs) | `BinaryHeap` 的 `IntoIter`/`Iter`/`Drain` |
| [src/collections/linked_list.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/linked_list.rs) | `LinkedList` 的 `IntoIter`/`Iter`/`IterMut` |
| [src/delegate.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/delegate.rs) | `delegate_iterator!` / `delegate_indexed_iterator!` 委托宏 |
| [src/iter/from_par_iter.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/from_par_iter.rs) | `FromParallelIterator`：让集合成为 `collect` 的目标 |

阅读建议：先读 `mod.rs` 和 `delegate.rs` 打好地基（本讲 4.1），再按「哈希家族 → B 树家族 → 队列家族」的顺序读各集合文件，你会发现它们几乎是同一个模板的复印。

## 4. 核心概念与源码讲解

### 4.1 collections 模块总览：一个宏、两个委托、一种排空辅助

#### 4.1.1 概念说明

`std::collections` 里的容器大多不是内存连续的：哈希表是桶数组加探测、B 树是多路节点、链表是分散的堆节点。而 u2-l2 已讲过，切片是「模范生产者」——内存连续、`split_at` 是 O(1)。那么怎样让这些不规则容器获得并行能力？

Rayon 的答案非常务实：**不为每种容器手写生产者，而是把元素搬进 `Vec`，复用 `Vec` 的生产者**。这一「搬运」策略由模块级宏 `into_par_vec!` 统一完成；搬运之后的迭代器行为则由 `delegate.rs` 的两个宏转发给内部的 `vec::IntoIter`。

对少数「内部本来就是连续内存」的容器（`VecDeque` 的两段连续缓冲、`BinaryHeap` 直接包装的 `Vec`），则可以跳过搬运，直接借用内部切片，做到零拷贝。

#### 4.1.2 核心流程

「先转 `Vec`」策略的执行流程：

```text
HashMap::into_par_iter()
  └─ Vec::from_iter(hash_map)        # 串行搬运：遍历哈希表，把 (K, V) 逐个移入 Vec
       └─ vec.into_par_iter()        # 得到有索引的 vec::IntoIter
            └─ delegate_iterator! 转发 drive_unindexed / opt_len
```

代价是搬运本身串行且需要一次 O(n) 分配，即 \[ T_{\text{搬运}} = O(n) \]，之后的所有切分、窃取都在连续内存上进行。这是一种「用一次串行复制换取理想并行数据布局」的权衡——对元素处理开销远大于移动开销的场景（典型如词频统计、数值聚合）非常合算。

「零拷贝借用」策略则以 `VecDeque::par_iter` 为例：

```text
&VecDeque::into_par_iter()
  └─ as_slices() 拆出前后两段连续切片 (a, b)
       └─ a.into_par_iter().chain(b)   # 两个切片迭代器用 Chain 首尾相接
```

#### 4.1.3 源码精读

先看搬运宏本身。[src/collections/mod.rs:L8-L22](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/mod.rs#L8-L22) 定义了 `into_par_vec!`：注释直言「先收集进临时 `Vec`，再迭代它」，宏体内 `Vec::from_iter(self).into_par_iter()` 一行完成搬运与接管，供七种集合中的五种（哈希两兄弟、B 树两兄弟、链表）复用：

```rust
macro_rules! into_par_vec {
    ($t:ty => $iter:ident<$($i:tt),*>, impl $($args:tt)*) => {
        impl $($args)* IntoParallelIterator for $t {
            type Item = <$t as IntoIterator>::Item;
            type Iter = $iter<$($i),*>;

            fn into_par_iter(self) -> Self::Iter {
                use std::iter::FromIterator;
                $iter { inner: Vec::from_iter(self).into_par_iter() }
            }
        }
    };
}
```

模块声明的七个集合子模块见 [src/collections/mod.rs:L24-L30](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/mod.rs#L24-L30)，与上一讲代码地图中「镜像标准库」的组织方式一致。

再看两个委托宏（u1-l4 已提过它们的存在，这里正式精读）。[src/delegate.rs:L11-L29](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/delegate.rs#L11-L29) 的 `delegate_iterator!` 要求被包装结构体已有名为 `inner` 的字段，它只实现无索引的 `ParallelIterator`，把 `drive_unindexed` 与 `opt_len` 原样转发：

```rust
fn drive_unindexed<C>(self, consumer: C) -> C::Result ... {
    self.inner.drive_unindexed(consumer)
}
fn opt_len(&self) -> Option<usize> {
    self.inner.opt_len()
}
```

[src/delegate.rs:L34-L61](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/delegate.rs#L34-L61) 的 `delegate_indexed_iterator!` 先展开一次 `delegate_iterator!`，再追加实现 `IndexedParallelIterator` 的 `drive`/`len`/`with_producer` 三个方法，同样全部转发 `inner`。注意关键差异：**用了哪个宏，就决定了用户能不能对这个集合调用 `zip`/`enumerate`/`with_min_len`**。

最后是排空辅助结构。[src/collections/mod.rs:L34-L69](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/mod.rs#L34-L69) 的 `DrainGuard` 专门服务 `BinaryHeap` 与 `VecDeque` 的 `par_drain`：构造时用 `mem::take` 把集合「偷」成 `Vec`（[L55-L61](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/mod.rs#L55-L61)），`Drop` 时再把剩余 `Vec` 转回原集合并保留容量（[L64-L69](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/mod.rs#L64-L69)）。文档注释（[L39-L44](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/mod.rs#L39-L44)）诚实地说这是「零分配但非零成本」的转换：`BinaryHeap` 要重新建堆（好在排空后为空）、`VecDeque` 转回时可能要把元素挪到偏移 0。它对 `&mut DrainGuard` 实现 `ParallelDrainRange` 并转调 `vec.par_drain(range)`，见 [src/collections/mod.rs:L71-L82](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/mod.rs#L71-L82)。

#### 4.1.4 代码实践

**实践目标**：用两个宏的测试代码验证「包装 + 委托」确实可以让自定义类型免费获得并行能力。

1. 打开 [src/delegate.rs:L63-L109](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/delegate.rs#L63-L109)，阅读 `unindexed_example` 与 `indexed_example` 两个单元测试：前者把 `BTreeMap` 的并行迭代器包进 `MyIntoIter` 再套 `delegate_iterator!`，后者包装 `vec::IntoIter` 套 `delegate_indexed_iterator!`。
2. 在仓库根目录运行 `cargo test -p rayon delegate`，只跑这两个测试。
3. 需要观察的现象：两个测试通过，说明宏生成的转发实现行为与内层迭代器完全一致。
4. 预期结果：输出 `test delegate::unindexed_example ... ok` 与 `test delegate::indexed_example ... ok`（测试运行结果待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：`delegate_iterator!` 和 `delegate_indexed_iterator!` 各自生成哪些 trait 实现？用它们包装同一个 `vec::IntoIter`，二者能力差在哪？

**答案**：前者只生成 `ParallelIterator`（`drive_unindexed` + `opt_len`）；后者在此基础上追加 `IndexedParallelIterator`（`drive` + `len` + `with_producer`）。差异体现在用户侧：只有后者能调用 `zip`、`enumerate`、`rev`、`with_min_len`/`with_max_len`，以及走 `collect` 的索引快速路径。

**练习 2**：为什么 `into_par_vec!` 里要先 `Vec::from_iter(self)` 而不是直接对原集合并行切分？

**答案**：哈希表、B 树、链表的内存布局不规则，无法像切片那样 O(1) 地 `split_at` 出两段连续数据。先把元素移进 `Vec`，就能复用 `Vec` 的索引生产者获得理想切分；代价是一次串行 O(n) 搬运与分配。

### 4.2 HashMap 与 HashSet

#### 4.2.1 概念说明

`HashMap<K, V>` 与 `HashSet<T>` 是「搬运策略」的标准样板：三个迭代器全部内部持有 `vec::IntoIter`。使用入口与产出类型如下表（`HashSet` 把 `V` 去掉即可）：

| 入口 | 产出元素 | 约束 |
| --- | --- | --- |
| `map.into_par_iter()` | `(K, V)` | `K: Send, V: Send` |
| `map.par_iter()` | `(&K, &V)` | `K: Sync, V: Sync` |
| `map.par_iter_mut()` | `(&K, &mut V)` | `K: Sync, V: Send` |
| `(&mut map).par_drain()` | `(K, V)` | `K: Send, V: Send` |

注意键永远是共享引用——并行修改键会破坏哈希表不变量，类型系统直接封死。

还要特别澄清：**map 的并行迭代产出的是元组，不是键值分离的迭代器**。一些资料（包括本手册早期大纲）提到过 `par_keys`/`par_values` 方法，但在当前源码中用 `Grep` 检索 `src/` 全目录无任何匹配，`git log -S "par_keys"` 在全部 2334 个提交中也无命中——**这两个方法在本仓库从未存在过**。想要键或值，用 `.map(|(k, v)| ...)` 投影；想按条件过滤条目，用 `.filter(|&(k, _)| ...)`。

#### 4.2.2 核心流程

以 `par_iter().map(f).sum()` 为例：

```text
&HashMap ──into_par_vec!──▶ Vec<(&K, &V)> ──vec.into_par_iter()──▶ 并行迭代器
        ──map(f)──▶ 惰性适配 ──sum()──▶ 触发切分：Vec 在中点二分，
        各线程对连续片段做局部求和，再两两 reduce 合并
```

由于 `Iter` 用的是无索引的 `delegate_iterator!`，切分由 `bridge_unindexed` 驱动（任意对半分，靠窃取自适应），而非索引中点精确切分。

#### 4.2.3 源码精读

按值迭代。[src/collections/hash_map.rs:L15-L27](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/hash_map.rs#L15-L27)：`IntoIter` 结构体的唯一字段就是 `vec::IntoIter<(K, V)>`；`into_par_vec!` 为 `HashMap<K, V, S>`（注意 `S` 哈希器参数不设约束）实现 `IntoParallelIterator`；`delegate_iterator!` 转发迭代行为：

```rust
pub struct IntoIter<K, V> {
    inner: vec::IntoIter<(K, V)>,
}

into_par_vec! {
    HashMap<K, V, S> => IntoIter<K, V>,
    impl<K: Send, V: Send, S>
}
```

共享引用迭代。[src/collections/hash_map.rs:L31-L51](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/hash_map.rs#L31-L51) 的 `Iter` 包装 `vec::IntoIter<(&'a K, &'a V)>`，为 `&'a HashMap` 实现，约束 `K: Sync, V: Sync`。可变迭代见 [src/collections/hash_map.rs:L53-L67](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/hash_map.rs#L53-L67)，约束 `K: Sync, V: Send`，与前置知识里的规则表完全吻合——这三个结构体就是那张约束表的最直接证据。

排空。[src/collections/hash_map.rs:L69-L93](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/hash_map.rs#L69-L93) 为 `&mut HashMap` 实现 `ParallelDrainFull` trait（该 trait 定义在 [src/iter/mod.rs:L3370-L3404](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L3370-L3404)，文档明确「迭代器被 drop 时移除所有元素、即使没消费完；原容量保留」）。实现体先 `self.drain().collect()` 成 `Vec` 再接管，`PhantomData<&'a mut HashMap>` 维持借用：

```rust
fn par_drain(self) -> Self::Iter {
    let vec: Vec<_> = self.drain().collect();
    Drain { inner: vec.into_par_iter(), marker: PhantomData }
}
```

`HashSet` 的对应实现在 [src/collections/hash_set.rs:L13-L51](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/hash_set.rs#L13-L51)，排空见 [L57-L74](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/hash_set.rs#L57-L74)。[第 53 行](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/hash_set.rs#L53) 有一行注释值得注意：`// HashSet doesn't have a mutable Iterator`——所以 `HashSet` 没有 `par_iter_mut`。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：统计 `HashMap<String, i32>` 所有值之和，并与 `BTreeMap` 版本互相验证；顺带确认 `par_keys` 不存在。

1. 新建（或复用 u1-l3 的）Cargo 项目，`Cargo.toml` 加 `rayon = "1.12"`，`main.rs` 写入：

```rust
use rayon::prelude::*;
use std::collections::{BTreeMap, HashMap};

fn main() {
    let hash: HashMap<String, i32> =
        (1..=100).map(|i| (format!("k{i}"), i)).collect();

    // map 的并行迭代产出 (&K, &V) 元组，用 map 投影出值
    let hash_sum: i32 = hash.par_iter().map(|(_k, v)| *v).sum();

    // 转成 BTreeMap 后同样方式求和
    let btree: BTreeMap<String, i32> = hash.clone().into_iter().collect();
    let btree_sum: i32 = btree.par_iter().map(|(_k, v)| *v).sum();

    // 串行基准
    let serial: i32 = hash.values().sum();

    println!("hash={hash_sum} btree={btree_sum} serial={serial}");
    assert_eq!(hash_sum, serial);
    assert_eq!(btree_sum, serial);

    // 键投影：BTreeMap 有序，collect 出的键向量应升序。
    // 闭包按值收到 (&String, &i32)，直接返回引用 k 即可收集成 Vec<&String>
    let keys: Vec<&String> = btree.par_iter().map(|(k, _v)| k).collect();
    assert_eq!(keys, btree.keys().collect::<Vec<_>>());

    // 验证 par_keys 确实不存在：取消下面这行注释将无法编译
    // let _: Vec<&String> = btree.par_keys().collect();
}
```

（示例代码，为练习手写；`v` 的类型是 `&i32`（元组 `(&String, &i32)` 的值侧引用），`*v` 解引用后交给 `sum`。）

2. 运行 `cargo run --release`；然后取消最后一行注释再 `cargo check`，观察编译错误。
3. 需要观察的现象：三个求和结果一致；`par_keys()` 调用处编译器报「no method named `par_keys` found」。
4. 预期结果：打印 `hash=5050 btree=5050 serial=5050`；注释取消后编译失败，错误信息确认该方法不存在（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `par_iter_mut()` 的约束是 `K: Sync, V: Send`，而键不能是 `&mut K`？

**答案**：并行执行时多个线程同时读键来定位与合并；键若可变会破坏哈希表不变量（例如键的哈希值变了，桶定位失效），所以键必须 `Sync` 只读共享，只有值需要 `Send` 以允许跨线程写。

**练习 2**：`hash.par_iter()` 与 `hash.into_par_iter()` 各自触发 `into_par_vec!` 中哪条路径？开销差别？

**答案**：`par_iter()` 走 `&'a HashMap` 的实现，把 `(&K, &V)` 引用收集进临时 `Vec`（原表保留）；`into_par_iter()` 走按值实现，把 `(K, V)` 移出、原表被搬空。两者都要一次 O(n) 的 `Vec` 构建分配；后者顺带拥有元素，之后无需再解引用。

**练习 3**：`(&mut hash).par_drain(..)` 执行完之后，`hash` 里还剩什么？

**答案**：元素全部移出、`hash` 变空，但总容量保留（trait 文档「moves all items ... retaining the original capacity」），后续插入可复用已分配内存。顺带一提：对 `HashMap` 这种无索引集合，trait 选的是 `ParallelDrainFull`（全量排空）而非 `ParallelDrainRange`（范围排空）。

### 4.3 BTreeMap 与 BTreeSet

#### 4.3.1 概念说明

`BTreeMap`/`BTreeSet` 是有序容器，内部是多路平衡树。它们在 Rayon 里的实现与哈希家族几乎逐行同构——同样「转 `Vec`」、同样三个（set 只有两个）迭代器、同样用无索引的 `delegate_iterator!`。`BTreeMap` 没有 `par_drain` 实现（源码中没有对应 impl），这点与 `HashMap` 不同。

真正值得关注的差异是**顺序语义**：标准库的 `BTreeMap` 串行迭代严格按键升序，`Vec::from_iter` 保持了这一顺序，而并行 `collect` 到 `Vec` 时各元素按迭代顺序写入预分配槽位（u2-l4 将详述），所以**并行收集的结果仍是有序的**；`HashMap` 的迭代顺序本来就任意，并行化后依旧任意。

#### 4.3.2 核心流程

```text
&BTreeMap ──Vec::from_iter──▶ Vec<(&K, &V)>（已按键升序）
         ──into_par_iter──▶ 无索引并行迭代器
         ──collect::<Vec<_>>()──▶ 顺序保持升序
```

#### 4.3.3 源码精读

[src/collections/btree_map.rs:L13-L27](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/btree_map.rs#L13-L27) 是按值迭代：与 `hash_map.rs` 相比只是少了哈希器参数 `S`，结构体同样只包一个 `vec::IntoIter<(K, V)>`。共享与可变迭代分别在 [src/collections/btree_map.rs:L29-L50](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/btree_map.rs#L29-L50) 与 [L52-L66](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/btree_map.rs#L52-L66)，注意委托处的约束比哈希家族多写了 `+ 'a`（如 `impl<'a, K: Sync + 'a, V: Sync + 'a>`）：

```rust
delegate_iterator! {
    Iter<'a, K, V> => (&'a K, &'a V),
    impl<'a, K: Sync + 'a, V: Sync + 'a>
}
```

这是因为 `delegate_iterator!` 会把约束原样贴到生成的 `impl` 上，而 `Iter<'a, K, V>` 里引用的生命周期必须显式约束。

[src/collections/btree_set.rs:L13-L50](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/btree_set.rs#L13-L50) 是 `BTreeSet` 的 `IntoIter` 与 `Iter`，[第 52 行](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/btree_set.rs#L52) 注释 `// BTreeSet doesn't have a mutable Iterator` 解释了为何没有 `par_iter_mut`——Rayon 的原则是镜像标准库的串行迭代器集合，不凭空发明。

#### 4.3.4 代码实践

**实践目标**：验证 B 树家族并行迭代「保持升序」的特性。

1. 在上面的示例工程中追加：

```rust
let ordered: BTreeMap<i32, &str> =
    [(3, "c"), (1, "a"), (2, "b")].into_iter().collect();
let keys: Vec<i32> = ordered.par_iter().map(|(k, _)| *k).collect();
let set: std::collections::BTreeSet<i32> =
    ordered.par_iter().map(|(k, _)| *k).collect();
println!("{keys:?} {set:?}");
```

（示例代码。）

2. 多运行几次（顺序保持不应受线程调度影响）。
3. 需要观察的现象：`keys` 始终为 `[1, 2, 3]`，`set` 打印为 `{1, 2, 3}`。
4. 预期结果：与串行 `ordered.keys()` 的顺序一致（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`BTreeMap` 的并行迭代器是有索引的吗？这会影响哪些操作？

**答案**：不是。它用 `delegate_iterator!`（无索引），尽管内层 `vec::IntoIter` 其实有索引。影响：不能调用 `zip`、`enumerate`、`rev`、`with_min_len` 等需要 `IndexedParallelIterator` 的方法；源码未注释说明为何不暴露索引能力，属于「接受现状」的事实。

**练习 2**：把一个无序 `HashMap` 收集成 `BTreeMap` 用哪条并行路径？反过来行吗？

**答案**：`hash.par_iter().collect::<BTreeMap<_, _>>()` 可行，`FromParallelIterator<(K, V)> for BTreeMap` 已实现（见 4.5）；反向（`BTreeMap` 收集成 `HashMap`）同样有实现。两个方向的键顺序语义分别由源容器决定。

### 4.4 VecDeque 与其他队列：零拷贝、索引与范围排空

#### 4.4.1 概念说明

`VecDeque` 是环形缓冲的双端队列，一段逻辑连续的数据可能被拆成物理上的两段；`BinaryHeap` 内部就是一个 `Vec`；`LinkedList` 则是完全离散的节点。三者在 collections 模块里呈现三种不同「成色」：

| 集合 | `into_par_iter` | `par_iter` | `par_iter_mut` | `par_drain` | 索引能力 |
| --- | --- | --- | --- | --- | --- |
| `VecDeque` | 转 `Vec`（可能搬移） | `as_slices()` 两段切片 `chain`，零拷贝 | `as_mut_slices()`，零拷贝 | 范围排空 `ParallelDrainRange` | 有 |
| `BinaryHeap` | 转 `Vec` | `as_slice()` 借用，零拷贝 | 无（标准库就没有） | 全量排空 `ParallelDrainFull` | 有 |
| `LinkedList` | 转 `Vec` | 转 `Vec` | 转 `Vec` | 无 | 无 |

「有索引」意味着 `VecDeque`/`BinaryHeap` 是本模块中唯一能用 `zip`/`enumerate`/`with_min_len` 的集合——因为它们的引用迭代直接建立在切片生产者之上。

#### 4.4.2 核心流程

`VecDeque::par_iter` 的零拷贝流程：

```text
&VecDeque::into_par_iter()
  └─ as_slices() -> (a: &[T], b: &[T])   # 环形缓冲被拆成首尾两段
       └─ a.into_par_iter().chain(b)     # Chain 组合两个索引迭代器
            # chain 的索引实现：len = a.len + b.len，
            # split_at 先切 a 段、超出部分再切 b 段
            # （见 src/iter/chain.rs 中 ChainProducer 的实现）
```

范围排空 `par_drain(1..4)` 的流程：

```text
&mut VecDeque --simplify_range(range)--> Drain{deque, range, orig_len}
  消费时: DrainGuard::new(deque)         # mem::take 偷出 Vec
          .par_drain(range)              # 借用 Vec 的范围排空
          .with_producer(callback)
  Drop 时: 若从未消费，退回普通串行 drain 兜底
```

#### 4.4.3 源码精读

按值迭代。[src/collections/vec_deque.rs:L17-L35](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/vec_deque.rs#L17-L35)：`Vec::from(self)` 后接管，[第 26 行](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/vec_deque.rs#L26) 的注释诚实标注「若队列数据不从偏移 0 开始则需要搬移数据」；关键是它用了 `delegate_indexed_iterator!`，暴露索引能力。

零拷贝引用迭代。[src/collections/vec_deque.rs:L37-L66](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/vec_deque.rs#L37-L66)：`Iter` 的内部类型是 `Chain<slice::Iter<'a, T>, slice::Iter<'a, T>>`，由 `as_slices()` 拆出的前后两段拼接，无任何复制：

```rust
fn into_par_iter(self) -> Self::Iter {
    let (a, b) = self.as_slices();
    Iter { inner: a.into_par_iter().chain(b) }
}
```

可变版本在 [src/collections/vec_deque.rs:L68-L89](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/vec_deque.rs#L68-L89)，用 `as_mut_slices()`，同样零拷贝。`Chain` 之所以能托起这份索引能力，是因为它自己的生产者也按「长度相加、先切前段」实现：`len` 为两段之和（[src/iter/chain.rs:L71-L73](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/chain.rs#L71-L73)），`split_at` 在切点不超过 `a` 段长度时只切 `a`（[src/iter/chain.rs:L177-L183](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/chain.rs#L177-L183)）。

范围排空。[src/collections/vec_deque.rs:L91-L159](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/vec_deque.rs#L91-L159) 是本模块最长的一段：`ParallelDrainRange` 实现（[L100-L111](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/vec_deque.rs#L100-L111)，用 `simplify_range` 把任意 `RangeBounds` 归一为 `Range<usize>`）；手工实现的 `ParallelIterator`（[L113-L126](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/vec_deque.rs#L113-L126)）与 `IndexedParallelIterator`（[L128-L149](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/vec_deque.rs#L128-L149)），其中 `with_producer` 借道 `super::DrainGuard` 复用 `Vec` 的生产者；以及兜底的 `Drop`（[L151-L159](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/vec_deque.rs#L151-L159)）——如果迭代器从未被真正消费（长度没变过），就调用标准库的串行 `drain` 保证元素仍被移除，兑现 trait 文档的承诺。

`BinaryHeap`。[src/collections/binary_heap.rs:L15-L33](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/binary_heap.rs#L15-L33) 按值迭代转 `Vec` 且有索引；[L36-L63](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/binary_heap.rs#L36-L63) 引用迭代直接包 `slice::Iter`（`as_slice()`，零拷贝、有索引）。排空见 [L67-L129](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/binary_heap.rs#L67-L129)：[第 74-75 行](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/binary_heap.rs#L74-L75) 的注释解释了一个微妙约束——排空本身不需要 `Ord`，但 `DrainGuard` 在 `Drop` 时要把剩余 `Vec` 重建回堆（`From<Vec<T>> for BinaryHeap` 需要 `T: Ord`），所以 `par_drain` 的约束是 `T: Ord + Send`。

`LinkedList`。[src/collections/linked_list.rs:L13-L66](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/linked_list.rs#L13-L66) 三个迭代器全部走 `into_par_vec!` + 无索引委托——离散节点没有任何可借用的连续内存，搬运是唯一选择。

#### 4.4.4 代码实践

**实践目标**：对比 `VecDeque` 与 `LinkedList` 的索引能力差异，并体验范围排空。

1. 追加代码：

```rust
use std::collections::{LinkedList, VecDeque};

let mut deque: VecDeque<i32> = (0..10).collect();
// VecDeque 有索引：可以 enumerate
let pairs: Vec<(usize, i32)> = deque.par_iter().enumerate().map(|(i, &x)| (i, x)).collect();
println!("{pairs:?}");

// 范围排空：移出 2..5，容量保留
let drained: Vec<i32> = deque.par_drain(2..5).collect();
println!("drained={drained:?} left={} cap>={}", deque.len(), deque.capacity());

let list: LinkedList<i32> = (0..10).collect();
// LinkedList 无索引：下一行取消注释将编译失败
// let _ = list.par_iter().enumerate();
let total: i32 = list.par_iter().sum();
println!("{total}");
```

（示例代码。）

2. 运行 `cargo run --release`；再取消被注释的 `enumerate` 行，`cargo check` 观察报错。
3. 需要观察的现象：`pairs` 为 `[(0,0), (1,1), ... (9,9)]`；`drained` 为 `[2, 3, 4]`，排空后 `deque.len()` 为 7；`LinkedList` 上调用 `enumerate` 报「method cannot be called」类错误。
4. 预期结果：如上；`total` 为 45（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：`deque.par_iter()` 是零拷贝的，`deque.into_par_iter()` 却可能搬移数据，为什么？

**答案**：`par_iter` 用 `as_slices()` 把环形缓冲拆成两段连续切片直接 `chain`，不碰数据；`into_par_iter` 要把按值元素装进一个 `Vec`，而 `Vec` 要求逻辑连续从偏移 0 开始，若队列数据横跨缓冲区末尾与开头，就必须搬移（源码 [第 26 行](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/vec_deque.rs#L26) 注释原话「requires data movement if the deque doesn't start at offset 0」）。

**练习 2**：`VecDeque::par_drain` 的 `Drop` 实现在什么情况下会走「串行 drain 兜底」？

**答案**：当迭代器构造后从未被消费（`deque.len()` 仍等于 `orig_len`）就被 drop 时——例如你创建了 `par_drain` 迭代器却提前丢弃它。此时必须保证 trait 承诺的「drop 时移除范围内元素」依然成立，于是退回标准库串行 `drain`。

**练习 3**：`BinaryHeap::par_drain` 的约束为何多了 `T: Ord`？

**答案**：见 [src/collections/binary_heap.rs:L74-L75](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/binary_heap.rs#L74-L75) 注释：`DrainGuard` 在 `Drop` 时要把剩余的 `Vec<T>` 重建为 `BinaryHeap`，而 `impl From<Vec<T>> for BinaryHeap<T>` 要求 `T: Ord`，即使排空后堆为空该约束也无法免除。

### 4.5 集合作为 collect 目标：FromParallelIterator

#### 4.5.1 概念说明

「集合的并行支持」是双向的：集合既能当数据源（前四节），也能当 `collect` 的目标。`FromParallelIterator` 就是并行世界的 `FromIterator`——`collect::<HashMap<_,_>>()` 能编译，靠的正是它。有意思的是，目标方向的实现策略与源方向恰好互为镜像：**先收进 `Vec`（或先 `par_extend` 进临时容器）再整体转换**，把「如何并行写入不规则容器」简化成「如何并行写入 `Vec`」这个已解决的问题。

#### 4.5.2 核心流程

```text
par_iter.collect::<VecDeque<T>>()
  └─ Vec::from_par_iter(par_iter)  # 并行收进 Vec
       .into()                     # Vec -> VecDeque（标准库转换）

par_iter.collect::<HashMap<K,V>>()
  └─ collect_extended: HashMap::default() 后 par_extend
```

#### 4.5.3 源码精读

[src/iter/from_par_iter.rs:L76-L101](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/from_par_iter.rs#L76-L101)：`VecDeque` 与 `BinaryHeap` 都是「先收进 `Vec` 再 `into()`」，`BinaryHeap` 的文档注释特别说明「堆序在全部收齐后串行计算」：

```rust
/// Collects items from a parallel iterator into a binaryheap.
/// The heap-ordering is calculated serially after all items are collected.
impl<T> FromParallelIterator<T> for BinaryHeap<T> ...
```

[src/iter/from_par_iter.rs:L117-L133](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/from_par_iter.rs#L117-L133) 是 `HashMap` 版本，文档说明了重复键的语义——「较早产出的对会被较晚的覆盖，与串行迭代器一致」（[L117-L120](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/from_par_iter.rs#L117-L120)）；实现走 [第 14-22 行](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/from_par_iter.rs#L14-L22) 的 `collect_extended` 辅助函数：先 `C::default()` 再 `par_extend`。`BTreeMap` 版本在 [L135-L149](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/from_par_iter.rs#L135-L149)，`LinkedList` 在 [L103-L115](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/from_par_iter.rs#L103-L115)。

注意「较早被较晚覆盖」的措辞依赖**产出顺序**而非执行顺序——并行执行时谁先产出由迭代器的顺序语义决定（有序迭代器按下标序、无序迭代器任意）。

#### 4.5.4 代码实践

**实践目标**：验证并行 collect 成 map 时重复键的覆盖语义。

1. 追加代码：

```rust
let pairs = vec![(1, 'a'), (1, 'b'), (2, 'c')];
let m: HashMap<i32, char> = pairs.par_iter().map(|&(k, c)| (k, c)).collect();
// 按 Vec 顺序，键 1 的 'a' 先产出、'b' 后产出 -> 最终是 'b'
println!("{m:?}");
assert_eq!(m.get(&1), Some(&'b'));
```

（示例代码。）

2. 多运行几次。
3. 需要观察的现象：键 `1` 对应的值始终是 `'b'`。
4. 预期结果：与串行 `pairs.into_iter().collect::<HashMap<_,_>>()` 行为一致（待本地验证）。

#### 4.5.5 小练习与答案

**练习 1**：`collect::<VecDeque<T>>()` 与 `collect::<BinaryHeap<T>>()` 的实现策略相同，哪里不同？

**答案**：都是 `Vec::from_par_iter(par_iter).into()`；不同在转换语义——`Vec→VecDeque` 是纯搬移，`Vec→BinaryHeap` 需要串行建堆（`BinaryHeap::from(vec)` 内部 heapify），因此文档注明「堆序在收齐后串行计算」。

**练习 2**：为什么 `FromParallelIterator for HashMap` 用 `collect_extended`（default + par_extend）而 `VecDeque` 用「先 `Vec` 再转换」？

**答案**：`Vec` 有专门的并行收集快速路径（预分配 + 分段写入，u2-l4/u4-l4 详述），转 `VecDeque`/`BinaryHeap` 是一次 O(n) 标准库转换即可；而 `HashMap` 插入依赖哈希定位，没有便宜的「整体转换」，只能靠 `par_extend` 逐条插入（其内部实现同样会借助中间缓冲，此处不展开）。

## 5. 综合实践

**任务：词频统计与有序汇总**。把本讲的元组投影、双 map 互验、有序 collect、范围排空串成一个程序：

1. 构造 `Vec<String>`（例如 26 个小写字母按随机权重重复约 100_000 次，或直接用一段英文文本分词）。
2. 用 `par_iter().fold(HashMap::new, ...).reduce(...)`（fold/reduce 的细节在 u3-l2，此处可先用 `for_each` + `Mutex` 或串行统计替代）得到 `HashMap<String, i32>` 词频表。
3. 用 `par_iter().map(|(_k, v)| *v).sum::<i32>()` 统计总词数，与输入长度断言相等。
4. 把词频表收集成 `BTreeMap<String, i32>`（`par_iter().collect()`），再用 `par_iter().map(|(k, _)| k.clone()).collect::<Vec<_>>()` 取出键向量，断言其升序。
5. 把键向量装进 `VecDeque`，用 `par_drain(10..20)` 移出第 10..20 个键，打印它们并断言与串行 `drain` 结果一致、且队列剩余长度正确。

**验收标准**：所有断言通过；程序多次运行结果稳定（有序部分不受线程调度影响）。并行统计部分若尚不会写，可先串行完成第 2 步，学完 u3-l2 后回来替换。

## 6. 本讲小结

- collections 模块的核心策略是「**借 `Vec` 的壳**」：`into_par_vec!` 宏把不规则容器串行搬进临时 `Vec`，再用 `delegate_iterator!` / `delegate_indexed_iterator!` 把迭代行为转发给 `vec::IntoIter`，自己几乎不写调度逻辑。
- `VecDeque` 与 `BinaryHeap` 的引用迭代是例外：`as_slices()`/`as_slice()` 直接借用内部连续内存，零拷贝且**有索引**，是本模块中唯一支持 `zip`/`enumerate`/`with_min_len` 的集合。
- 约束规则在源码中清晰可查：共享 `Sync`、可变 `Send`、按值 `Send`；map 的键在可变迭代中只读共享。
- **`par_keys`/`par_values` 在当前 Rayon 中不存在**（源码与 git 历史双重确认），map 并行迭代产出 `(K, V)` 元组，靠 `.map()` 投影；`HashSet`/`BTreeSet`/`BinaryHeap` 没有 `par_iter_mut`，因为标准库本身就没有可变迭代器——Rayon 只做镜像。
- 排空家族：`HashMap`/`HashSet`/`BinaryHeap` 走 `ParallelDrainFull` 全量排空，`VecDeque` 走 `ParallelDrainRange` 范围排空，底层都经 `DrainGuard`「偷出 `Vec`、drop 时还原」实现，保留原容量。
- 集合也是 `collect` 目标（`FromParallelIterator`）：`VecDeque`/`BinaryHeap` 先收进 `Vec` 再转换，`HashMap`/`BTreeMap` 走 default + `par_extend`，重复键「后产出覆盖先产出」。

## 7. 下一步学习建议

下一讲（u2-l4）将深入 `collect` 的内部：并行结果如何无锁地写进同一个 `Vec`、空间预分配与分段写入协议——这正是本讲反复借用的那条「`Vec` 快速路径」的原理。之后建议：

- 学完 u2-l4 后回头读本讲的 `from_par_iter.rs`，理解 `collect_extended` 与 `Vec::from_par_iter` 的关系。
- 到 u3-l1 精读 `delegate.rs` 的完整宏展开与 `map`/`filter` 适配器，本讲的委托模式是那里的热身。
- 若对「为什么哈希家族不暴露索引能力」感兴趣，可带着这个问题读 u4-l2 的 `Producer` 契约，思考 `with_producer` 对实现方的隐含要求。
