# u8-l3 集合的委托实现模式

## 1. 本讲目标

学完本讲，你应该能够：

1. 读懂 `delegate_iterator!` 与 `delegate_indexed_iterator!` 两个宏的定义，并能手工写出它们对某个具体类型的展开结果。
2. 说出 collections 模块各集合的三条实现路线（into_par_vec! 搬运、零拷贝切片借用、手写），以及「选哪个宏」如何决定一个集合是否拥有索引能力。
3. 为自己的包装类型（如 `MyBoxedVec`）手写全部转发实现，让它获得 `par_iter()` / `into_par_iter()`，并支持 `map` / `sum` / `collect` / `collect_into_vec`。

本讲是 u2-l3（集合的使用层）的下沉版：那时我们只知道「HashMap 会先搬进临时 Vec」；现在我们要逐行读这些委托是怎么写的，并亲手复刻一份。

## 2. 前置知识

**委托（delegation）模式。** 一个类型内部包含另一个功能完整的对象，自己不实现任何逻辑，只把方法调用原样转发给内部对象。Rust 没有语言级委托，通常靠宏批量生成转发代码。本讲的主角 `src/delegate.rs` 就是这样一个宏文件。

**macro_rules! 的词法作用域。** `macro_rules!` 定义的宏遵循「先定义、后可用」的文本顺序规则。要让一个模块里定义的宏被同 crate 后面的模块使用，需要在 `mod` 声明上加 `#[macro_use]` 属性；若不加 `#[macro_export]`，宏只在本 crate 内可见，**外部用户拿不到**。这一点直接决定了本讲综合实践必须「手写」而不是「调宏」。

**token tree（记法树）。** `macro_rules!` 匹配的基本单位是 token tree：一个单独 token，或一对配平的 `()`/`[]`/`{}` 整体算一个。片段说明符 `$($args:tt)*` 会「吞掉」后面所有记法树，这正是委托宏能把任意长的泛型约束列表当作参数吃进去的技巧。

**两个根 trait（回顾 u2-l1）。** `ParallelIterator` 的内部驱动方法是 `drive_unindexed` 与 `opt_len`；子 trait `IndexedParallelIterator` 追加 `drive`、`len`、`with_producer` 三个方法。委托宏转发哪些方法，就决定了包装类型暴露哪一层能力。

**plumbing 三角色（回顾 u4）。** `with_producer` 是框架向数据源「要生产者」的入口。只要包装类型把 `with_producer` 原样转发给内部迭代器，plumbing 层看到的就是内部那个真正的 Producer（比如 Vec 的 drain 生产者）——包装层凭空消失，这就是「零成本复用」的确切含义。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/delegate.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/delegate.rs) | 定义 `delegate_iterator!` 与 `delegate_indexed_iterator!` 两个委托宏，自带两个可运行示例测试 |
| [src/lib.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/lib.rs) | 用 `#[macro_use] mod delegate;` 把宏广播给全 crate |
| [src/collections/mod.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/mod.rs) | 定义 `into_par_vec!`（先搬进临时 Vec 再委托）与 `DrainGuard` 排空辅助 |
| [src/collections/hash_map.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/hash_map.rs) | 哈希家族标本：`into_par_vec!` + `delegate_iterator!`（无索引） |
| [src/collections/btree_set.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/btree_set.rs) | 最小的集合作本：三类迭代器只剩两类（无可变迭代） |
| [src/collections/vec_deque.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/vec_deque.rs) | 对照组：同样委托 `vec::IntoIter` 却保留索引；Drain 展示「何时不能委托」 |
| [src/collections/binary_heap.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/binary_heap.rs) | 零拷贝路线：`as_slice()` 直接借用内部切片并保留索引 |
| [src/vec.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/vec.rs) | 委托链的终点：`vec::IntoIter` 手写双 trait；`&Vec` 转发 `&[T]` |

## 4. 核心概念与源码讲解

### 4.1 委托宏：delegate_iterator! 与 delegate_indexed_iterator!

#### 4.1.1 概念说明

rayon 有大约 20 个集合迭代器类型（7 个集合 × 3 类入口，部分缺项）。它们的共同点是：**自己不做任何并行计算，只是把某个现成并行迭代器（几乎总是 Vec 或切片的迭代器）包了一层**。如果每个类型都手写 5 个转发方法，就会有上百行纯重复代码。

`delegate.rs` 用两个宏消灭这份重复：

- `delegate_iterator!`：为包装类型实现 `ParallelIterator`，转发 `drive_unindexed` 与 `opt_len` 两个方法。
- `delegate_indexed_iterator!`：先展开前者，再实现 `IndexedParallelIterator`，追加转发 `drive`、`len`、`with_producer` 三个方法。

两者的关系与 trait 之间的关系同构：索引版 = 无索引版 + 追加。

需要注意一个前置约定（写在宏的文档注释里）：**被包装的结构体必须已经声明，且字段名必须叫 `inner`**。宏不做声明，只做实现；`IntoParallelIterator` 的实现也不由这两个宏负责，需要另行手写或由 `into_par_vec!` 补上。

#### 4.1.2 核心流程

`delegate_indexed_iterator! { IntoIter<T> => T, impl<T: Send> }` 的展开分两步：

```text
第 1 步：内部先展开 delegate_iterator!
┌──────────────────────────────────────────────┐
│ impl<T: Send> ParallelIterator for IntoIter<T>│
│   type Item = T;                              │
│   drive_unindexed → self.inner.drive_unindexed│
│   opt_len         → self.inner.opt_len        │
└──────────────────────────────────────────────┘
第 2 步：再追加索引 trait
┌─────────────────────────────────────────────────────┐
│ impl<T: Send> IndexedParallelIterator for IntoIter<T>│
│   drive         → self.inner.drive                  │
│   len           → self.inner.len                    │
│   with_producer → self.inner.with_producer          │
└─────────────────────────────────────────────────────┘
```

宏调用的语法形状是 `$iter:ty => $item:ty, impl $($args:tt)*`：

1. `$iter:ty`：包装类型（如 `IntoIter<T>`），填进 `impl ... for $iter`。
2. `$item:ty`：元素类型，填进 `type Item = $item`。
3. `impl $($args:tt)*`：把 `impl` 之后的所有 token 原样吐回 `impl $($args)* ParallelIterator for ...` 的位置。这就是为什么调用时泛型约束必须写在最后的 `impl<T: Send>` 里——`$($args:tt)*` 是「吞掉剩余一切」的记法树通配，只有放在末尾才不会吃掉前面的参数。

#### 4.1.3 源码精读

首先是文件头的注释，解释了「impl 约束放在末尾」的原因：

[src/delegate.rs:1-4](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/delegate.rs#L1-L4) —— 文档说明这两个宏「把新类型迭代器委托给内部类型」，并注明 `impl` 约束必须放在参数末尾，因为吞掉任意约束列表的唯一办法就是用 `$($args:tt)*` 做记法树通配。

无索引宏的全部实现：

[src/delegate.rs:11-29](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/delegate.rs#L11-L29) —— `delegate_iterator!` 为 `$iter` 实现 `ParallelIterator`，其中：

- [src/delegate.rs:18-22](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/delegate.rs#L18-L22) —— `drive_unindexed` 把消费者原封不动交给 `self.inner`，包装层不介入驱动过程。
- [src/delegate.rs:24-26](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/delegate.rs#L24-L26) —— `opt_len` 同样转发。这一行很关键：它意味着即使是无索引委托，长度信息仍然保留（内部 `vec::IntoIter` 返回 `Some(len)`），`collect` 的预分配快速路径（u4-l4）依然生效。丢掉的只是索引三方法。

索引宏通过「宏调用宏」实现叠加：

[src/delegate.rs:34-61](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/delegate.rs#L34-L61) —— `delegate_indexed_iterator!` 的第一件事：

- [src/delegate.rs:38-41](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/delegate.rs#L38-L41) —— 原样转调 `delegate_iterator!`，先铺好 `ParallelIterator` 基础实现，模仿「子 trait 必须先有父 trait」的约束。
- [src/delegate.rs:43-59](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/delegate.rs#L43-L59) —— 再追加 `IndexedParallelIterator`，其中 [src/delegate.rs:54-58](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/delegate.rs#L54-L58) 的 `with_producer` 转发是「零成本复用生产者」的核心：框架索要 Producer 的回调被直接递给内部迭代器，plumbing 层最终拿到的是内部的 Vec/切片生产者，包装类型在编译后不复存在。

宏本身自带两个可运行的用法示范（也是本讲综合实践的模板）：

- [src/delegate.rs:63-86](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/delegate.rs#L63-L86) —— `unindexed_example`：自定义 `MyIntoIter` 包装 `BTreeMap` 的并行迭代器，[src/delegate.rs:75-78](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/delegate.rs#L75-L78) 一行宏即完成委托，随后 `map`/`collect` 全部可用。
- [src/delegate.rs:88-109](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/delegate.rs#L88-L109) —— `indexed_example`：包装 `vec::IntoIter` 并用索引版宏，[src/delegate.rs:106-108](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/delegate.rs#L106-L108) 调用了 `collect_into_vec`——这个方法只对索引迭代器开放，证明索引委托完整保留了快速收集路径。

宏的可见性机制在 lib.rs：

[src/lib.rs:84-85](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/lib.rs#L84-L85) —— `#[macro_use] mod delegate;` 把宏按文本顺序广播给其后声明的所有模块（第 89 行起的 `pub mod array`/`collections` 等）。注意 delegate 模块本身是私有的、宏也没有 `#[macro_export]`，所以**这两个宏是 crate 内部设施，rayon 的用户无法调用**——这正是综合实践要手写转发的根本原因。

#### 4.1.4 代码实践

1. **实践目标**：亲眼确认委托宏的用法与效果，验证「包装后能力不变」。
2. **操作步骤**：在仓库根目录运行 `cargo test -p rayon delegate`。这会按名称匹配到 delegate 模块里的 `unindexed_example` 与 `indexed_example` 两个测试。
3. **需要观察的现象**：测试输出中应出现 `delegate::unindexed_example` 与 `delegate::indexed_example` 两项。
4. **预期结果**：两个测试均 passed（合计 2 个）。若想看宏展开的真实产物，可再运行 `cargo expand -p rayon collections::hash_map`（需安装 `cargo-expand` 并使用 nightly 工具链），在输出中搜索 `impl ParallelIterator for IntoIter`。待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`delegate_iterator!` 为什么也要转发 `opt_len`？不转发会怎样？

**答案**：`ParallelIterator` 的 `opt_len` 有返回 `None` 的默认实现。若不转发，包装类型的 `opt_len` 会退化为 `None`，`collect` 就无法在收集前得知长度，只能走「各任务分块收集 + 拼接」的慢路径（u4-l4），白 白放弃预分配直写优化。转发后内部 `vec::IntoIter` 返回 `Some(len)`，快速路径保留。

**练习 2**：为什么宏参数里 `impl<T: Send>` 必须写在最后，而不是写成 `delegate_iterator!(impl<T: Send>, IntoIter<T> => T)`？

**答案**：因为 `$($args:tt)*` 会无差别吞掉其位置之后的所有记法树。若约束列表放在中间，`=>`、元素类型等后续参数也会被吞进 `$args`，匹配失败或生成错误代码。源码文件头第 3-4 行的注释明确说明了这一点。

**练习 3**：`delegate_indexed_iterator!` 生成的代码里，`ParallelIterator` 的实现是谁写的？

**答案**：由它自己转调 `delegate_iterator!` 生成（delegate.rs:38-41），索引宏只追加 `IndexedParallelIterator` 部分。这是宏层面的「先有父、后有子」。

### 4.2 集合内部结构：三条实现路线

#### 4.2.1 概念说明

u2-l3 曾给出使用层结论：七大集合里哈希家族、B 树家族、LinkedList 走「先搬进临时 Vec」路线，VecDeque 与 BinaryHeap 走「零拷贝借用切片」路线。本模块读源码把这两条路线落实，并补上第三条——**手写**。

先澄清一个容易误解的表述：「HashMap 委托给它的底层 vec」并不准确。HashMap 私有的底层是哈希表（`RawTable`），外部根本无法安全访问。真实的委托对象是一个**新建的临时 Vec**：先把整个集合串行地收集进 `Vec`，再委托给这个 Vec 的并行迭代器。所以精确的说法是「委托给一个临时 Vec 的迭代器」。

三条路线总览：

| 路线 | 代表集合 | 机制 | 是否有索引 |
|---|---|---|---|
| into_par_vec! 搬运 | HashMap、HashSet、BTreeMap、BTreeSet、LinkedList | 串行收进临时 `Vec`，委托 `vec::IntoIter` | 否（`delegate_iterator!`） |
| 零拷贝切片借用 | BinaryHeap、VecDeque 的 `Iter`/`IterMut` | `as_slice()`/`as_slices()` 借内部切片（或两段 chain），委托 `slice::Iter` | 是（`delegate_indexed_iterator!`） |
| 手写 | VecDeque 的 `Drain`、各集合 `par_drain` 入口 | 自定义 `with_producer` 与 `Drop` 守卫 | 视实现 |

#### 4.2.2 核心流程

以 `HashMap::into_par_iter()` 为例的搬运路线：

```text
HashMap<K, V>
   │  Vec::from_iter(self)        ← 串行遍历哈希表，逐个移出 (K, V)，分配一块连续内存
   ▼
Vec<(K, V)>                        ← 临时 Vec，一次性 O(n) 搬运
   │  .into_par_iter()            ← 复用 Vec 的并行迭代器（委托链终点）
   ▼
vec::IntoIter<(K, V)>              ← 真正干活的迭代器，自带 Producer
   │  collections::hash_map::IntoIter { inner: 上面这个 }
   ▼
delegate_iterator! 转发全部驱动方法
```

零拷贝路线以 `&VecDeque` 为例：

```text
&VecDeque<T>
   │  self.as_slices()            ← 借出 (前段切片, 后段切片)，无任何复制
   ▼
(a: &[T], b: &[T])
   │  a.into_par_iter().chain(b)  ← 两个切片迭代器 chain 起来
   ▼
Chain<slice::Iter, slice::Iter>    ← 长度已知，天然有索引
   │  collections::vec_deque::Iter { inner: 上面这个 }
   ▼
delegate_indexed_iterator! 转发全部五个方法
```

手写路线只在「宏不够用」时出场，见 4.2.3 的 VecDeque Drain 分析。

#### 4.2.3 源码精读

**搬运宏 into_par_vec!。** 它在 collections/mod.rs 里，负责补上委托宏不管的 `IntoParallelIterator`：

[src/collections/mod.rs:10-22](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/mod.rs#L10-L22) —— 为目标类型实现 `IntoParallelIterator`：`type Item` 直接取自标准库 `IntoIterator::Item`；关键是 [src/collections/mod.rs:16-19](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/mod.rs#L16-L19) 的方法体——`Vec::from_iter(self)` 把集合整个搬进临时 Vec，再 `into_par_iter()` 拿到 Vec 的并行迭代器，塞进声明好 `inner` 字段的包装结构体。文档注释直言这是一条「先收集进临时 Vec 再迭代」的路线。

宏可见性再次依赖声明顺序：[src/collections/mod.rs:24-30](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/mod.rs#L24-L30) 的七个 `pub mod` 全部声明在第 10 行的宏定义**之后**，子模块才能调用它（与 lib.rs 中 `#[macro_use] mod delegate` 领先于 `pub mod collections` 是同一个道理）。

**哈希家族标本。** hash_map.rs 的三个迭代器结构体全部形如「inner 装一个 `vec::IntoIter`」：

[src/collections/hash_map.rs:15-27](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/hash_map.rs#L15-L27) —— `IntoIter<K, V>` 的 inner 是 `vec::IntoIter<(K, V)>`（临时 Vec 的迭代器）；第 19 行用 `into_par_vec!` 实现 `HashMap → IntoIter` 的入口，第 24 行用 `delegate_iterator!` 补上 `ParallelIterator`。注意第二个宏用的是**无索引版**——这是主动降级，见本节末尾的分析。共享借用版同理：[src/collections/hash_map.rs:43-51](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/hash_map.rs#L43-L51) 把 `&HashMap` 收集成 `Vec<(&K, &V)>`（收集的是引用，元素本体不动），约束相应从 `Send` 换成 `Sync`。

哈希家族的 `par_drain` 入口是宏覆盖不到的地方，只能手写：

[src/collections/hash_map.rs:77-88](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/hash_map.rs#L77-L88) —— `ParallelDrainFull for &mut HashMap` 手写实现：第 82 行先 `self.drain().collect()` 用标准库排空收进 Vec（保留原容量），再包成 `Drain { inner, marker }`；`marker` 是 `PhantomData<&'a mut HashMap>`，仅用于把借用挂到类型上（[src/collections/hash_map.rs:72-75](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/hash_map.rs#L72-L75)）。`into_par_vec!` 只会为集合自身实现 `IntoParallelIterator`，套不进「`&mut` 集合的方法 + 带生命周期的返回类型」这种形状，所以手写；但随后的 [src/collections/hash_map.rs:90-93](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/hash_map.rs#L90-L93) 又回到委托宏——能委托的一律委托。

**最薄标本 BTreeSet。** 它展示了「标准库没有的入口就不做」：

[src/collections/btree_set.rs:14-26](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/btree_set.rs#L14-L26) —— 按值版与 HashMap 完全同构：`into_par_vec!` + `delegate_iterator!` 两行宏完事。[src/collections/btree_set.rs:52](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/btree_set.rs#L52) 一行注释说明原因：`BTreeSet` 在标准库里就没有可变迭代器，rayon 镜像标准库，也就不提供 `IterMut`。rayon 的集合模块整体遵循「镜像 std」原则，能少一个入口就少一个。

**对照组 VecDeque：同样的 inner，不同的宏。** 这是本模块最重要的对照实验：

[src/collections/vec_deque.rs:17-35](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/vec_deque.rs#L17-L35) —— `IntoIter<T>` 的 inner 与 HashMap 的一样是 `vec::IntoIter<T>`，但入口因带注释而手写：[src/collections/vec_deque.rs:21-30](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/vec_deque.rs#L21-L30) 第 26 行注明 `Vec::from(self)` 在队列头不在偏移 0 时需要搬移数据；第 32 行用的是 **`delegate_indexed_iterator!`**。同一个被委托对象，HashMap 交出索引能力、VecDeque 保留索引能力——**被委托者的能力是相同的，宏的选择决定暴露多少**。`delegate_iterator!` 相当于主动把接口降级到无索引层，`enumerate`/`zip`/`collect_into_vec` 随之不可用（编译期报错），但 `opt_len` 仍转发、collect 预分配路径仍生效。

VecDeque 的共享借用版展示零拷贝路线：[src/collections/vec_deque.rs:39-66](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/vec_deque.rs#L39-L66) —— inner 是 `Chain<slice::Iter, slice::Iter>`：第 56 行 `self.as_slices()` 借出环形缓冲的两段连续切片（零复制），第 58 行 chain 起来。两个切片迭代器都有索引（[src/slice/mod.rs:814-824](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/mod.rs#L814-L824) 定义的 `slice::Iter` 实现了 `IndexedParallelIterator`），其 chain 组合也是索引的，于是第 63 行用索引版宏顺理成章。

**零拷贝的另一形态 BinaryHeap：**

[src/collections/binary_heap.rs:49-63](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/binary_heap.rs#L49-L63) —— `Iter` 的 inner 直接是 `slice::Iter`：第 55 行 `self.as_slice()` 借出堆内部数组（堆在标准库里就是包着 Vec 的结构，切片视图零成本），随后索引版委托。按值版 [src/collections/binary_heap.rs:15-33](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/binary_heap.rs#L15-L33) 的 `Vec::from(self)` 是零分配的类型解包（内部 Vec 直接交出来），同样保留索引。第 65 行注释同样说明堆没有可变迭代器。

**手写路线的充分条件：需要自定义 with_producer 或 Drop 守卫时。** VecDeque 的范围排空 `Drain` 是教科书案例：

[src/collections/vec_deque.rs:113-126](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/vec_deque.rs#L113-L126) —— `Drain` 手写 `ParallelIterator`（`drive_unindexed` 直接调 `bridge`，`opt_len` 返回 `Some(self.len())`），不走委托。[src/collections/vec_deque.rs:140-148](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/vec_deque.rs#L140-L148) 的 `with_producer` 是手写的真正原因：它要先经 `DrainGuard::new(self.deque)` 把队列整体「偷」成一个 Vec（[src/collections/mod.rs:50-69](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/mod.rs#L50-L69) 定义：构造时 `mem::take` 拿走内容、`Drop` 时从 Vec 还原并保留容量），再借 Vec 的 `par_drain` 拿生产者——这个「偷出去、用完还回来」的生命周期管理无法用转发表达。[src/collections/vec_deque.rs:151-158](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/vec_deque.rs#L151-L158) —— 手写的 `Drop` 兜底：若从未被驱动（长度未变），补一次标准库 drain 把元素移除，保证无论迭代是否发生、语义都正确。

最后看委托链的终点——`vec::IntoIter` 自己，理解「委托到底委托了什么」：

[src/vec.rs:60-94](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/vec.rs#L60-L94) —— 它是手写而非委托（所有集合委托的正是它，它是生产者的提供者）：`drive`/`drive_unindexed` 都直通 `bridge`，[src/vec.rs:87-93](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/vec.rs#L87-L93) 的 `with_producer` 把自己转成 `par_drain(..)` 去要生产者——Vec 用「搬空元素、只留缓冲待释放」的方式供出 Producer。另外 [src/vec.rs:18-25](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/vec.rs#L18-L25) 显示 `&Vec` 到 `&[T]` 的转发是不用宏的单行委托——当类型完全对得上时，一行 `impl` 转发就够，宏只是批量重复的解法。

#### 4.2.4 代码实践

1. **实践目标**：通过编译器的行为验证「宏选择决定索引能力」。
2. **操作步骤**：
   - 在任意依赖 rayon 的示例工程里写入两段代码：
     - A：`let v: Vec<_> = (0..10).collect::<VecDeque<i32>>(); v.par_iter().enumerate().collect::<Vec<_>>();`
     - B：`let m: HashMap<i32, i32> = (0..10).map(|i| (i, i)).collect(); m.par_iter().enumerate().collect::<Vec<_>>();`
   - 分别编译，观察一个成功、一个失败。
3. **需要观察的现象**：B 报错，错误信息应指向 `enumerate` 找不到方法或 `IndexedParallelIterator` 未实现（HashMap 的 `Iter` 只被 `delegate_iterator!` 实现到无索引层）；A 正常（VecDeque 的 `Iter` 走 `delegate_indexed_iterator!`）。
4. **预期结果**：与 u2-l3 的使用层结论互相印证——七大集合中仅 VecDeque 与 BinaryHeap 可用 `enumerate`/`zip`/`collect_into_vec`。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：「HashMap 委托给它的底层 vec」这句话错在哪里？

**答案**：HashMap 的底层是私有的哈希表，外部无法访问。实际委托对象是 `Vec::from_iter(map)` 新建的**临时 Vec**（collections/mod.rs:18），付出一次 O(n) 串行搬运与一次分配，换来连续内存上的理想切分。

**练习 2**：HashMap 的 `IntoIter` 与 VecDeque 的 `IntoIter` 的 `inner` 字段是同一个类型（`vec::IntoIter`），为什么后者能用 `enumerate` 而前者不能？

**答案**：`vec::IntoIter` 本身实现了 `IndexedParallelIterator`（vec.rs:75），两者都能拿到索引能力；差别在包装层调用的宏不同——VecDeque 用 `delegate_indexed_iterator!` 转发了 `drive`/`len`/`with_producer`，HashMap 用 `delegate_iterator!` 主动降级到无索引层。接口暴露多少由宏选择决定，与被委托对象的实际能力无关。

**练习 3**：VecDeque 的 `Drain` 为什么不能像 `IntoIter` 那样用委托宏？

**答案**：它的 `with_producer` 需要先经 `DrainGuard` 把整个队列偷成 Vec、用完在 `Drop` 里还原（vec_deque.rs:145-147），还自带一个「从未被驱动就补一次 drain」的 `Drop` 兜底（vec_deque.rs:151-158）。这种自定义生命周期管理超出了「把方法原样转发给 inner」的表达范围，只能手写。

### 4.3 自定义容器扩展：为包装类型添加 par_iter

#### 4.3.1 概念说明

掌握了前两个模块，为自定义容器添加并行迭代就成了一件按模板填空的工作。设有一个包装类型 `MyBoxedVec<T>`（内含 `Vec<T>`），目标是让它支持：

```rust
mbv.par_iter().map(...).sum();          // 共享借用
mbv.par_iter().map(...).collect();      // 收集
mbv.into_par_iter().map(...).collect(); // 按值
mbv.into_par_iter().collect_into_vec(); // 索引快速路径
```

由于委托宏是 crate 私有的，我们的实现就是**把宏的展开结果手写出来**——这反而是最好的练习：写完你就彻底理解宏生成了什么。需要三件套：

1. **包装迭代器结构体**：`inner` 字段装 `vec::IntoIter<T>`（按值）或 `slice::Iter<'a, T>`（借用），对应搬运/零拷贝两条路线的微缩版。因为 `MyBoxedVec` 的内部本来就是 `Vec`，我们连临时搬运都不需要，属于「零成本委托」的最理想情形。
2. **两个 trait 的转发实现**：照抄 delegate.rs 第 18-26 行与第 44-58 行的五个转发方法。
3. **`IntoParallelIterator` 入口**：为 `MyBoxedVec<T>`（要求 `T: Send`）与 `&'a MyBoxedVec<T>`（要求 `T: Sync`）各实现一次。`par_iter()` 不用专门实现——它是「`&I: IntoParallelIterator`」的 blanket 方法（u2-l2），引用版入口写好后 `par_iter()` 自动可用。

#### 4.3.2 核心流程

```text
用户调用 mbv.par_iter()
   │  blanket 规则：par_iter() = (&mbv).into_par_iter()
   ▼
impl IntoParallelIterator for &MyBoxedVec   ← 我们手写的入口
   │  inner: self.inner.par_iter()          ← 借用 Vec → 借用 [T]（vec.rs:18-25 的单行委托）
   ▼
Iter { inner: slice::Iter }                  ← 我们的包装迭代器
   │  map/sum/collect 触发驱动
   ▼
ParallelIterator::drive_unindexed ──转发──▶ slice::Iter::drive_unindexed
IndexedParallelIterator::with_producer ─转发─▶ slice::Iter::with_producer
   ▼
plumbing 层拿到的就是切片 Producer           ← 包装层零成本消失
```

约束的对应关系（承接 u2-l3 的规则）：

| 入口 | 约束 | 原因 |
|---|---|---|
| `into_par_iter()`（按值） | `T: Send` | 元素被移动到任意线程 |
| `par_iter()` / 引用版入口 | `T: Sync` | 多线程共享 `&T` |
| `par_iter_mut()` | `T: Send` | 每线程独占一段 `&mut T` |

#### 4.3.3 源码精读

手写实现的每一块都有源码模板，逐一对应：

- 结构体声明模板：[src/collections/hash_map.rs:15-17](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/hash_map.rs#L15-L17) —— `pub struct IntoIter<K, V> { inner: vec::IntoIter<(K, V)> }`，字段名必须是 `inner`（若想日后换成真宏，现在就遵守这个约定）。
- `ParallelIterator` 转发模板：[src/delegate.rs:15-27](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/delegate.rs#L15-L27) —— `type Item` + 两个转发方法，方法体各只有一行 `self.inner.xxx(consumer)`。
- `IndexedParallelIterator` 转发模板：[src/delegate.rs:43-59](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/delegate.rs#L43-L59) —— 三个追加方法。注意手写时这两个 impl 的泛型约束要与结构体一致。
- 引用版入口模板：[src/collections/vec_deque.rs:51-61](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/vec_deque.rs#L51-L61) —— `impl<'a, T: Sync> IntoParallelIterator for &'a VecDeque<T>`，方法体借出内部数据再包一层。
- 按值版入口模板：[src/collections/vec_deque.rs:21-30](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/collections/vec_deque.rs#L21-L30) —— `impl<T: Send> IntoParallelIterator for VecDeque<T>`。
- 内部 Vec 转发到切片的支点：[src/vec.rs:18-25](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/vec.rs#L18-L25) —— 我们的 `self.inner.par_iter()`（inner 是 `Vec<T>`）正是落到这一行 `<&[T]>::into_par_iter(self)`，得到 `slice::Iter`。
- plumbing trait 的导入位置：[src/iter/mod.rs:91](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L91) —— `pub mod plumbing` 是公开模块，外部用户可以 `use rayon::iter::plumbing::*;` 拿到 `Consumer`/`UnindexedConsumer`/`ProducerCallback`，这正是手写转发的方法签名所需。

#### 4.3.4 代码实践

1. **实践目标**：把手写转发与宏展开逐行对照，确认「宏只是替你写了这十行」。
2. **操作步骤**：
   - 打开 [src/delegate.rs:34-61](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/delegate.rs#L34-L61)，把 `delegate_indexed_iterator! { MyIter<T> => T, impl<T: Send> }` 的展开写在纸上（两个 impl、五个方法）。
   - 再对照综合实践里 `IntoIter<T>` 的手写代码逐一核对。
3. **需要观察的现象**：五个方法的方法体应当完全一致——都是 `self.inner.同名方法(参数)` 的一行转发；唯一新增内容是 `type Item = T;`。
4. **预期结果**：手写版与宏展开版逐字符等价（仅宏版多了 `delegate_iterator!` 转调的展开层次）。若有出入，说明你对某个方法签名的记忆有误，回读 delegate.rs 纠正。

#### 4.3.5 小练习与答案

**练习 1**：如果只实现了 `IntoParallelIterator`，不写转发 impl，会发生什么？

**答案**：编译失败。`into_par_iter()` 的返回类型必须实现 `ParallelIterator`（它出现在关联类型 `Iter` 的约束里），否则链式调用 `map` 等方法无从谈起。入口与转发 impl 缺一不可。

**练习 2**：`MyBoxedVec` 的引用版入口为什么不需要像 HashMap 那样先收集进临时 Vec？

**答案**：HashMap 的内部结构（哈希表）不是连续内存、也无法安全借出；而 `MyBoxedVec` 的内部本身就是 `Vec`，`&Vec` 可以零成本 deref 成 `&[T]` 直接供出切片 Producer（vec.rs:18-25）。是否需要搬运取决于内部表示能否廉价映射为 Vec/切片——这正是 4.2 两条路线的分野。

**练习 3**：想让 `MyBoxedVec` 支持 `par_iter_mut()`，还需要补什么？

**答案**：为 `&'a mut MyBoxedVec<T>` 实现 `IntoParallelIterator`（`Item = &'a mut T`，约束 `T: Send`），包装迭代器的 inner 换成 `rayon::slice::IterMut<'a, T>` 并同样手写五个转发方法。`par_iter_mut()` 本身是 blanket 方法，入口实现后自动可用。

## 5. 综合实践

**任务**：为自定义包装类型 `MyBoxedVec`（内含 `Vec<T>`）完整实现并行迭代支持，验证 `map`/`sum`/`collect` 与索引快速路径。这是本讲三个模块的总装：结构体设计抄 hash_map、转发实现抄 delegate.rs 的展开、入口实现抄 vec_deque。

新建一个独立 Cargo 工程（`cargo new my_boxed_vec`），`Cargo.toml` 加 `rayon = "1"`，`src/main.rs` 写入：

```rust
// 示例代码：手写委托宏的展开，为包装类型添加并行迭代
use rayon::iter::plumbing::{Consumer, UnindexedConsumer, ProducerCallback};
use rayon::prelude::*;
use rayon::slice;
use rayon::vec;

/// 自定义容器：内含 Vec 的包装类型
#[derive(Debug, Clone, Default)]
struct MyBoxedVec<T> {
    inner: Vec<T>,
}

impl<T> From<Vec<T>> for MyBoxedVec<T> {
    fn from(v: Vec<T>) -> Self {
        MyBoxedVec { inner: v }
    }
}

// ---------- 按值迭代器：delegate_indexed_iterator! 的手工展开 ----------

struct IntoIter<T> {
    inner: vec::IntoIter<T>,
}

impl<T: Send> ParallelIterator for IntoIter<T> {
    type Item = T;

    fn drive_unindexed<C>(self, consumer: C) -> C::Result
    where
        C: UnindexedConsumer<Self::Item>,
    {
        self.inner.drive_unindexed(consumer)
    }

    fn opt_len(&self) -> Option<usize> {
        self.inner.opt_len()
    }
}

impl<T: Send> IndexedParallelIterator for IntoIter<T> {
    fn drive<C>(self, consumer: C) -> C::Result
    where
        C: Consumer<Self::Item>,
    {
        self.inner.drive(consumer)
    }

    fn len(&self) -> usize {
        self.inner.len()
    }

    fn with_producer<CB>(self, callback: CB) -> CB::Output
    where
        CB: ProducerCallback<Self::Item>,
    {
        self.inner.with_producer(callback)
    }
}

impl<T: Send> IntoParallelIterator for MyBoxedVec<T> {
    type Item = T;
    type Iter = IntoIter<T>;

    fn into_par_iter(self) -> Self::Iter {
        IntoIter {
            inner: self.inner.into_par_iter(),
        }
    }
}

// ---------- 共享借用迭代器：同样手写，inner 换成切片迭代器 ----------

struct Iter<'a, T> {
    inner: slice::Iter<'a, T>,
}

impl<'a, T: Sync> ParallelIterator for Iter<'a, T> {
    type Item = &'a T;

    fn drive_unindexed<C>(self, consumer: C) -> C::Result
    where
        C: UnindexedConsumer<Self::Item>,
    {
        self.inner.drive_unindexed(consumer)
    }

    fn opt_len(&self) -> Option<usize> {
        self.inner.opt_len()
    }
}

impl<'a, T: Sync> IndexedParallelIterator for Iter<'a, T> {
    fn drive<C>(self, consumer: C) -> C::Result
    where
        C: Consumer<Self::Item>,
    {
        self.inner.drive(consumer)
    }

    fn len(&self) -> usize {
        self.inner.len()
    }

    fn with_producer<CB>(self, callback: CB) -> CB::Output
    where
        CB: ProducerCallback<Self::Item>,
    {
        self.inner.with_producer(callback)
    }
}

impl<'a, T: Sync> IntoParallelIterator for &'a MyBoxedVec<T> {
    type Item = &'a T;
    type Iter = Iter<'a, T>;

    fn into_par_iter(self) -> Self::Iter {
        // 借用 Vec → 借用 [T]，落到 vec.rs 的单行委托
        Iter {
            inner: self.inner.par_iter(),
        }
    }
}

// ---------- 验证 ----------

fn main() {
    let mbv = MyBoxedVec::from(vec![1, 2, 3, 4, 5]);

    // 模块一验证：map + sum（立即执行消费者触发驱动）
    let sum: i64 = mbv.par_iter().map(|&x| x as i64 * x as i64).sum();
    assert_eq!(sum, 55);

    // 模块二验证：collect 回 Vec，顺序与串行一致
    let doubled: Vec<i64> = mbv.par_iter().map(|&x| x as i64 * 2).collect();
    assert_eq!(doubled, vec![2, 4, 6, 8, 10]);

    // 模块三验证：索引能力——enumerate 与 collect_into_vec 只有索引迭代器才可用
    let idx_sum: i64 = mbv.par_iter().enumerate().map(|(i, _)| i as i64).sum();
    assert_eq!(idx_sum, 10);

    let mut out: Vec<i64> = Vec::new();
    mbv.clone()
        .into_par_iter()
        .map(|x| x as i64 + 100)
        .collect_into_vec(&mut out);
    assert_eq!(out, vec![101, 102, 103, 104, 105]);

    println!("all assertions passed");
}
```

操作步骤与观察点：

1. `cargo run --release`，预期打印 `all assertions passed`。
2. 注释掉 `Iter` 的 `IndexedParallelIterator` impl 再编译：`enumerate` 与 `collect_into_vec` 两处应报错，`map`/`sum`/`collect` 不受影响——亲手复现 4.2.4 的「宏选择决定索引能力」。
3. 把 `IntoIter` 的 `with_producer` 方法体改成 `todo!()` 再运行：map/sum 仍可能通过（走 `drive`），但 `collect_into_vec` 会 panic——体会每个转发方法各自服务哪条驱动路径（u4-l3 的两条路径）。
4. 进阶：为 `&'a mut MyBoxedVec<T>` 补 `IterMut` 版本，解锁 `par_iter_mut()`。

以上运行结果均为预期推演，待本地验证。

## 6. 本讲小结

- 委托宏 `delegate_iterator!` / `delegate_indexed_iterator!` 为「已声明且字段名为 `inner`」的包装类型生成全部转发实现；后者通过转调前者实现「先父后子」的叠加，`impl` 约束必须放在参数末尾是 `$($args:tt)*` 记法树通配的必然要求。
- 两个宏都是 crate 私有设施（`#[macro_use] mod delegate`，无 `#[macro_export]`），rayon 用户无法调用——自定义容器必须手写宏的展开，而这恰好是最好的学习方式。
- collections 的三条实现路线：into_par_vec! 串行搬进**临时 Vec** 再委托（哈希家族、B 树家族、链表，无索引）；`as_slice()`/`as_slices()` 零拷贝借内部切片（堆、双端队列，有索引）；需要自定义 `with_producer` 或 `Drop` 守卫时手写（VecDeque 的 Drain）。
- 最关键的对照：HashMap 与 VecDeque 的 `IntoIter` 委托的是同一个 `vec::IntoIter`，但前者用无索引宏主动降级、后者用索引宏保留全部能力——接口暴露多少由宏选择决定；无索引委托仍转发 `opt_len`，collect 预分配快速路径因此不受影响。
- 为自定义容器添加 par_iter 的完整路径 = 包装迭代器结构体（inner 装现成并行迭代器）+ 两个 trait 的五个转发方法 + 两三个 `IntoParallelIterator` 入口（按值 `Send` / 借用 `Sync`）；`par_iter()` 由 blanket 规则自动获得。

## 7. 下一步学习建议

本讲是单元八的收尾。三条后续路线供选择：

1. **向上写自己的 Producer**：如果你的容器内部不是 Vec/切片（比如树或环形缓冲），「委托现成迭代器」走不通，就需要按 u4-l2 的 `Producer` 契约自己实现 `split_at`——u9-l1「实现自定义 ParallelIterator」与 u9-l2「自定义 Producer 与 Split 扩展」正是这条路，`tests/producer_split_at.rs` 的「三刀四段」测试会兜住你的实现。
2. **横向对照适配器的 delegate**：`src/delegate.rs` 只服务集合；迭代器适配器（map/filter 等）用的是 `src/delegate.rs` 之外的另一套模式（u3-l1 讲过的「包装消费者 + 转发驱动」），对照阅读能看清「委托生产侧」与「委托消费侧」的镜像关系。
3. **回顾收集闭环**：委托保住的 `opt_len` 如何变成 `collect` 的预分配直写，可重读 u4-l4 的 `CollectConsumer` 与 `src/iter/collect/consumer.rs`，把「集合委托 → opt_len → 精确预分配」这条链在脑子里走通。
