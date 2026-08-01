# 小集合 ListSet 与分组去重

## 1. 本讲目标

本讲围绕 typst-utils 中一组「面向小集合」的切片工具展开。学完后你应当能够：

- 说清楚 `ListSet` 为何要在「线性查找」和「二分查找」之间按 `CUT_OFF` 切换，以及切换点如何选择；
- 看懂 `Rdedup::rdedup_by_key` 的双指针就地算法，并解释它为何「保留后值」而非「保留前值」，与标准库 `dedup` 的区别在哪里；
- 理解 `GroupByKey` 迭代器如何惰性地把一个切片拆成「连续相同键」的若干段；
- 掌握 `split_prefix_suffix` 用 `position` / `rposition` 完成三段切分的技巧。

本讲是 u1-l2 的延伸：u1-l2 已经在 `SliceExt` 这一扩展 trait 层面介绍过 `group_by_key`、`split_prefix_suffix` 的 API 表面，本讲则深入它们的内部实现机制，并把 `ListSet`、`Rdedup`、`GroupByKey`、`split_prefix_suffix` 串成一条「小集合处理」的主题线。

## 2. 前置知识

阅读本讲前，建议你已经掌握：

- **扩展 trait 模式**：因为孤儿规则，不能直接给标准库类型加方法，所以 typst-utils 自建 `SliceExt` 等 trait 再为 `[T]` 实现，调用前需要 `use` 引入（见 u1-l2）。
- **`Deref` / `DerefMut`**：Rust 里 `Vec<T>`、`SmallVec<[T; N]>`、`Box<[T]>` 都能解引用成 `&[T]` / `&mut [T]`，本讲的 `ListSet` 正是利用 `DerefMut<Target = [T]>` 来泛型地接收「任意可当作切片的容器」。
- **`SmallVec<[T; N]>`**：一个「N 个元素内联在栈上、超出再堆分配」的 `Vec` 替代品，typst-utils 用它承载本讲的 `Rdedup` 实现。
- **切片的 `binary_search` / `sort_unstable` / `position` / `rposition`**：本讲大量复用这些标准库原语。

> 一个贯穿全讲的直觉：**集合越小，「分配与排序的固定开销」相对查询收益就越不划算**。本讲的四个工具都在围绕这点点做优化——要么按大小选策略（`ListSet`），要么用 `SmallVec` 避免堆分配（`Rdedup`），要么零分配惰性遍历（`GroupByKey`、`split_prefix_suffix`）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/listset.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/listset.rs) | 定义 `ListSet`，一个基于可变切片、按大小自适应选择线性或二分查找的集合。 |
| [src/lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs) | 内联定义 `SliceExt`（含 `group_by_key`、`split_prefix_suffix`）、`Rdedup` trait 与其在 `SmallVec` 上的实现、以及 `GroupByKey` 迭代器。 |

公开导出关系：`ListSet` 通过 [lib.rs:23](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L23) 的 `pub use self::listset::ListSet;` 暴露；`SliceExt`、`Rdedup`、`GroupByKey` 则定义在 lib.rs 顶层模块里直接对外公开。

---

## 4. 核心概念与源码讲解

### 4.1 ListSet：大小自适应的切片集合

#### 4.1.1 概念说明

很多时候我们想要一个「集合」（判断某个值在不在里面），但数据规模很小——可能就十来个元素。这时用 `HashSet` 不划算：哈希表要分配、要计算哈希、要处理桶，固定开销远大于收益。

`ListSet` 提供了一个更朴素的方案：**直接复用一个已有的切片容器**（`Vec`、`SmallVec`、`Box<[T]>` 等），按元素个数选择查找策略：

- 元素少 → 不排序，直接线性扫描，构造几乎零开销；
- 元素多 → 构造时排序一次，之后每次查询用二分查找。

它的名字 `ListSet` 也透露了设计：底层就是「列表」，只是套了一层集合语义。

#### 4.1.2 核心流程

设切片长度为 \( n \)，阈值 `CUT_OFF` 记为 \( c = 15 \)。

构造阶段（`new`）：

- 若 \( n \le c \)：原样保留，**不排序**，构造代价 \( O(1) \)。
- 若 \( n > c \)：调用 `sort_unstable` 排序，构造代价 \( O(n \log n) \)。

查询阶段（`contains`）：

- 若 \( n \le c \)：线性查找，单次代价 \( O(n) \)。
- 若 \( n > c \)：二分查找，单次代价 \( O(\log n) \)。

为什么选 \( c = 15 \)？因为对小数组而言，二分查找「每次比较 + 计算中点」的常数因子并不比顺序遍历小，而且排序本身有成本。源码注释直言阈值是「凭直觉选的」（*Picked by gut feeling*），是一个典型的工程经验值。

下表概括两种路径的取舍：

| 路径 | 构造代价 | 单次查询代价 | 适用场景 |
| --- | --- | --- | --- |
| 线性（\( n \le 15 \)） | \( O(1) \) | \( O(n) \) | 元素少、查询少或仅构造一次 |
| 二分（\( n > 15 \)） | \( O(n \log n) \) | \( O(\log n) \) | 元素多、或需要多次查询摊薄排序成本 |

> 注意：`ListSet` 没有提供 `insert`。它是「一次性构造、多次查询」的只读集合——你把一个现成的切片容器交给它，之后只查不改。

#### 4.1.3 源码精读

先看阈值常量与结构体定义（[src/listset.rs:3-15](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/listset.rs#L3-L15)）：

```rust
/// Picked by gut feeling. Could probably even be a bit larger.
const CUT_OFF: usize = 15;

/// A set backed by a mutable slice-like data structure.
///
/// This data structure uses two different strategies depending on size:
/// - When the list is small, it is just kept as is and searched linearly ...
/// - When the list is a bit bigger, it's sorted in `new` and then binary-searched ...
pub struct ListSet<S>(S);
```

`ListSet<S>` 是一个元组结构体，`S` 是「任何能解引用成 `&mut [T]`」的容器。泛型约束写在 `impl` 块上而非结构体上（[src/listset.rs:17-21](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/listset.rs#L17-L21)）：

```rust
impl<T, S> ListSet<S>
where
    S: DerefMut<Target = [T]>,
    T: Ord,
{
```

这里要求 `S: DerefMut<Target = [T]>`（容器可解引用为切片）且 `T: Ord`（元素可排序、可比较）。

构造函数（[src/listset.rs:25-30](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/listset.rs#L25-L30)）：

```rust
pub fn new(mut list: S) -> Self {
    if list.len() > CUT_OFF {
        list.sort_unstable();
    }
    Self(list)
}
```

关键点：`list` 被声明为 `mut`，这样能拿到 `&mut [T]` 调用 `sort_unstable()`。注意阈值是**严格大于** `CUT_OFF`（`> CUT_OFF`），即长度恰好为 15 时仍走线性路径。选用 `sort_unstable` 而非 `sort`，是因为相等元素无需保持原相对顺序，unstable 排序更快且不分配。

查询函数（[src/listset.rs:41-47](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/listset.rs#L41-L47)）：

```rust
pub fn contains(&self, value: &T) -> bool {
    if self.0.len() > CUT_OFF {
        self.0.binary_search(value).is_ok()
    } else {
        self.0.contains(value)
    }
}
```

`self.0` 直接解引用到底层切片。`binary_search` 返回 `Result<usize, usize>`，`is_ok()` 表示找到了；线性分支用切片自带的 `contains`。

#### 4.1.4 代码实践

**实践目标**：亲手触发 `ListSet` 的二分路径，并验证「构造期自动排序」让原始输入是否有序不再重要。

**操作步骤**：

1. 在一个临时 crate 的 `Cargo.toml` 里加入 `typst-utils` 与 `smallvec` 依赖。
2. 写如下 `main.rs`（**示例代码**，非项目原有代码）：

```rust
use typst_utils::ListSet;

fn main() {
    // 故意乱序，且长度 20 > CUT_OFF(15)，触发二分路径
    let raw: Vec<i32> = vec![42, 7, 19, 3, 88, 5, 100, 1, 64, 12,
                             9, 33, 71, 2, 55, 18, 90, 4, 27, 15];
    assert_eq!(raw.len(), 20);
    let set = ListSet::new(raw);
    // 由于 new() 内部已排序，contains 走二分查找
    assert!(set.contains(&88));
    assert!(set.contains(&1));
    assert!(!set.contains(&999));
    println!("ListSet(20) 查询正常，二分路径已启用");
}
```

3. 运行 `cargo run`。

**需要观察的现象**：断言全部通过，说明即便输入乱序，`new()` 排序后 `contains` 仍能正确判断成员关系。

**预期结果**：打印 `ListSet(20) 查询正常，二分路径已启用`。（本例的断言由源码逻辑可推得，但实际运行结果**待本地验证**——不要假设你已经跑过。）

#### 4.1.5 小练习与答案

**练习 1**：如果把上面 `raw` 的长度改成 10（`< CUT_OFF`），`new()` 内部还会排序吗？`contains` 走哪条路径？

**参考答案**：不会排序（`10 > 15` 为假），构造保持原序；`contains` 走线性 `slice::contains` 分支。

**练习 2**：`ListSet` 为何不实现 `std::hash::BuildHasher` 那样的哈希？换言之，它适合替代 `HashSet` 吗？

**参考答案**：它定位为「小规模、只读、低固定开销」的集合，用排序+二分或线性扫描即可，省去了哈希表的分配与哈希计算开销；但元素很多时，`HashSet` 的 \( O(1) \) 查询更优，所以 `ListSet` 不适合替代大规模 `HashSet`。

---

### 4.2 Rdedup：保留后值的就地去重

#### 4.2.1 概念说明

标准库 `Vec::dedup` / `dedup_by_key` 在去重时**保留前一个值**、丢掉后续重复值。但有些场景我们需要相反的语义：**保留后一个值**（即同一组键里「最新」的那条覆盖「较旧」的）。

举个例子：对已按键排序的日志 `[(a, v1), (a, v2), (a, v3)]`，标准 `dedup_by_key` 会保留 `(a, v1)`，而 typst-utils 的 `rdedup_by_key`（r = rear / later）会保留 `(a, v3)`。这种「后者覆盖前者」的语义在「按 key 取最新值」的合并场景里非常自然。

typst-utils 把它实现为 `Rdedup` trait，并**只为 `SmallVec` 实现**——这又是一处「面向小集合」的取舍：去重通常作用于短序列，用 `SmallVec` 能避免堆分配。

#### 4.2.2 核心流程

`rdedup_by_key` 采用经典的**双指针就地算法**，要求输入已按 key 排序（或至少相同 key 连续）。设两个游标：

- `k`：写入位置（指向当前已确认保留的最后一个元素的下一个槽位）；
- `i`：扫描位置（从 1 遍历到末尾）。

不变式：`self[0..k]` 始终是「处理到当前位置为止、去重后保留的元素」。

伪代码：

```
k = 0
for i in 1 .. len:
    if key(self[i]) != key(self[k]):   # 遇到新键 → 开新组
        k += 1
    # 无论是否新键，都把 self[i] 复制到 self[k]
    # 关键：同键时 k 不前进，导致后续元素不断覆盖 self[k] → 保留最后一个
    if k < i:
        self[k] = self[i]
truncate(len = k + 1)
```

为什么这样能「保留后值」？当连续多个元素同键时，`k` 不动，`self[k] = self[i]` 反复把更靠后的元素写入同一个槽位 `k`，于是同组的最后一个元素最终留在了 `self[k]`。这与标准库「同键时跳过后续写入、保留首个」正好相反。

#### 4.2.3 源码精读

trait 定义（[src/lib.rs:193-203](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L193-L203)）：

```rust
/// A variant of `dedup` that keeps the later value rather than the earlier one.
pub trait Rdedup {
    type Item;

    /// Deduplicates values in a sorted sequence using a key function, but
    /// unlike the standard version keeps the later one.
    fn rdedup_by_key<K, F>(&mut self, key: F)
    where
        F: Fn(&mut Self::Item) -> K,
        K: PartialEq<K>;
}
```

注意 `key` 的签名是 `Fn(&mut Self::Item) -> K`——接受**可变引用**，因此可以在去重过程中顺便修改元素。`K: PartialEq`（只要求判等，不要求全序）。

实现只针对 `SmallVec`（[src/lib.rs:205-225](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L205-L225)）：

```rust
impl<T: Copy, const N: usize> Rdedup for SmallVec<[T; N]> {
    type Item = T;

    fn rdedup_by_key<K, F>(&mut self, mut key: F)
    where
        T: Copy,
        K: PartialEq<K>,
        F: FnMut(&mut T) -> K,
    {
        let mut k = 0;
        for i in 1..self.len() {
            if key(&mut self[i]) != key(&mut self[k]) {
                k += 1;
            }
            if k < i {
                self[k] = self[i];
            }
        }
        self.truncate(k + 1);
    }
}
```

逐行剖析：

- **`T: Copy` 约束**：算法用 `self[k] = self[i]` 做按位拷贝覆盖，必须 `Copy`。
- **`if k < i`**：当 `k == i`（即一路上没有遇到重复、写入位置与扫描位置重合）时，跳过自赋值，省一次拷贝。
- **`self.truncate(k + 1)`**：循环结束后 `k` 指向最后一个保留元素的索引，长度应为 `k + 1`。`SmallVec::truncate` 只缩短不增长，所以空输入（`len == 0`）时循环不执行、`truncate(1)` 对长度 0 的容器是空操作，结果仍为空——这与 [src/lib.rs:507](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L507) 的空输入测试一致。

**手工跟踪**测试用例 `[(a,1),(a,2),(a,3),(b,2)]`（见 [src/lib.rs:508](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L508)）：

| 步骤 | i | key(self[i]) | key(self[k]) | k 变化 | 覆盖后 self |
| --- | --- | --- | --- | --- | --- |
| 初始 | — | — | — | k=0 | `[(a,1),(a,2),(a,3),(b,2)]` |
| 1 | 1 | a | a（相等） | 不变 | `self[0]=self[1]` → `[(a,2),...]` |
| 2 | 2 | a | a（相等） | 不变 | `self[0]=self[2]` → `[(a,3),...]` |
| 3 | 3 | b | a（不等） | k=1 | `self[1]=self[3]` → `[(a,3),(b,2),...]` |
| truncate(2) | | | | | `[(a,3),(b,2)]` ✓ |

最终 `(a,*)` 组保留了**最后一个** `(a,3)`，正是「保留后值」语义。

#### 4.2.4 代码实践

**实践目标**：用 `SmallVec` + `rdedup_by_key` 复现「保留后值」去重，并与标准库 `dedup_by_key` 对照，直观感受二者差异。

**操作步骤**：

```rust
use smallvec::SmallVec;
use typst_utils::Rdedup; // trait 需手动引入

fn main() {
    let data = [('a', 1), ('a', 2), ('a', 3), ('b', 2)];

    // typst-utils：保留后值
    let mut sv: SmallVec<[_; 2]> = data.into();
    sv.rdedup_by_key(|&mut (c, _)| c);
    assert_eq!(sv.as_slice(), &[('a', 3), ('b', 2)]);

    // 对照：标准库保留前值
    let mut v: Vec<_> = data.into();
    v.dedup_by_key(|(c, _)| *c);
    assert_eq!(v, vec![('a', 1), ('b', 2)]);

    println!("rdedup(后值) = {:?}, dedup(前值) = {:?}", sv, v);
}
```

**需要观察的现象**：typst-utils 版结果首项是 `(a,3)`，标准库版首项是 `(a,1)`。

**预期结果**：打印 `rdedup(后值) = [('a', 3), ('b', 2)], dedup(前值) = [('a', 1), ('b', 2)]`。（**待本地验证**。）注意：`Rdedup` 只为 `SmallVec` 实现，若你把上面的 `sv` 换成 `Vec`，会直接编译失败——这是刻意设计。

#### 4.2.5 小练习与答案

**练习 1**：若输入是 `[('b',2),('c',3),('c',4)]`，`rdedup_by_key` 结果是什么？

**参考答案**：`[('b',2),('c',4)]`——`(c,*)` 组保留后值 `(c,4)`，对应 [src/lib.rs:509](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L509) 的断言。

**练习 2**：算法对未排序的输入（如 `[('a',1),('b',2),('a',3)]`）会得到什么？为什么？

**参考答案**：得到 `[('a',1),('b',2),('a',3)]`，即几乎不去重。因为 `('a')` 与 `('b')` 键不同使 `k` 前进到 1，而后再遇到的 `('a')` 与当前 `self[k]=('b')` 不同，又开新组。该算法假设「同键连续」，所以调用前必须按键排序。

---

### 4.3 GroupByKey：惰性分组迭代器

#### 4.3.1 概念说明

`SliceExt::group_by_key` 把一个切片按「连续相同键」拆成多段，返回一个 `GroupByKey` 迭代器，每次 `next()` 吐出 `(这一段的键, 这一段的切片)`。

它与标准库的 `slice::group_by` / 实验 API `chunks_by` 思路一致：**只合并相邻的相同键**，不要求全局相同键聚到一起。因此若你想要「所有同键元素归并」，需先排序（或配合上一节的 `Rdedup`、`ListSet`）。

它的键约束是 `K: PartialEq`（仅判等），所以键不要求可排序。

#### 4.3.2 核心流程

`GroupByKey` 是个**惰性**迭代器：构造时不做任何计算，仅在 `next()` 时才推进。其状态就是「剩余尚未分组的切片 `slice`」加一个键函数 `f`。

每次 `next()`：

1. 从剩余切片取出第一个元素，算出它的键 `key`；
2. 从第二个元素起，用 `take_while` 数出连续多少个元素的键等于 `key`；
3. `split_at(count)` 把这段切下来作为本组，剩余部分作为新的 `slice`；
4. 返回 `(key, head)`；若切片为空则返回 `None` 终止。

因为「移动切片边界」本身就是状态推进，所以它天然支持 `next` 反复调用，无需额外游标。

#### 4.3.3 源码精读

trait 方法与结构体（[src/lib.rs:129-134](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L129-L134) 与 [src/lib.rs:227-231](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L227-L231)）：

```rust
fn group_by_key<K, F>(&self, f: F) -> GroupByKey<'_, T, F>
where
    F: FnMut(&T) -> K,
    K: PartialEq;
```

```rust
/// This struct is created by [`SliceExt::group_by_key`].
pub struct GroupByKey<'a, T, F> {
    slice: &'a [T],
    f: F,
}
```

`group_by_key` 的实现只是把 `self` 和闭包塞进结构体（[src/lib.rs:175-177](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L175-L177)），真正干活在 `Iterator` 实现里：

```rust
impl<'a, T, K, F> Iterator for GroupByKey<'a, T, F>
where
    F: FnMut(&T) -> K,
    K: PartialEq,
{
    type Item = (K, &'a [T]);

    fn next(&mut self) -> Option<Self::Item> {
        let mut iter = self.slice.iter();
        let key = (self.f)(iter.next()?);          // 空切片 → 提前返回 None
        let count = 1 + iter.take_while(|t| (self.f)(t) == key).count();
        let (head, tail) = self.slice.split_at(count);
        self.slice = tail;                          // 推进剩余切片
        Some((key, head))
    }
}
```

逐行看几个细节：

- `iter.next()?`：用 `?` 优雅处理「切片已耗尽」——`Option::None` 直接作为 `next` 的返回值，迭代结束。
- `let count = 1 + ...take_while(...).count()`：`1` 是首元素，`take_while` 从第二个元素开始数连续同键个数。注意 `take_while` 基于一个新建的 `iter`（已消费首元素），故不会重复计首元素。
- `split_at(count)` 与 `self.slice = tail`：把已分组那段交出去，剩余段留到下次。整个迭代**零分配**，所有切片都是原数据的借用。

#### 4.3.4 代码实践

**实践目标**：用 `group_by_key` 对连续相同首字母的单词分组，并验证它**只合并相邻**同键。

**操作步骤**（**示例代码**）：

```rust
use typst_utils::SliceExt; // trait 需手动引入

fn main() {
    let words = ["apple", "ant", "banana", "bear", "cat", "apple"];
    // 按首字母分组（只合并相邻）
    let groups: Vec<(char, &[&str])> =
        words.group_by_key(|w| w.chars().next().unwrap()).collect();

    for (c, ws) in &groups {
        println!("{c}: {ws:?}");
    }
    assert_eq!(groups.len(), 4); // a / b / c / a —— 末尾的 apple 单独成组
}
```

**需要观察的现象**：末尾的 `"apple"` 与开头的 `"apple"`/`"ant"` **没有被合并**，因为它们不相邻；输出共 4 组。

**预期结果**：依次打印 `a: ["apple", "ant"]`、`b: ["banana", "bear"]`、`c: ["cat"]`、`a: ["apple"]`。（**待本地验证**。）

#### 4.3.5 小练习与答案

**练习 1**：若想得到「全部 a 开头的词合并为一组」，应如何预处理输入？

**参考答案**：先按首字母排序（如 `words.sort_by_key(...)`），使同键元素相邻，再调用 `group_by_key`。这正好可与 `ListSet`/`Rdedup` 的「输入需有序」前提呼应。

**练习 2**：`GroupByKey::next` 里为何 `count = 1 + take_while(...).count()` 而不是直接 `take_while(...).count()`？

**参考答案**：因为创建的 `iter` 已经 `next()` 消费了第一个元素来求 `key`，首元素不在后续 `take_while` 范围内，必须显式 `+1` 把首元素计入本组长度。

---

### 4.4 split_prefix_suffix：三段切分辅助

#### 4.4.1 概念说明

`split_prefix_suffix` 解决一个常见需求：把一个切片切成**前缀 / 中段 / 后缀**三段，其中前缀和后缀都满足某个谓词 `f`，且后缀不与前缀重叠。

典型应用：剥离一段数据首尾连续的「某种元素」（如首尾的空白、首尾的对齐填充），只保留中间有实质内容的部分。它与 `trim_start_matches` / `trim_end_matches` 互补——后者直接返回剥除后的中段，而 `split_prefix_suffix` 同时给出两个边界索引，让你既能拿到中段，也能单独访问被剥下的前缀和后缀。

#### 4.4.2 核心流程

函数返回 `(start, end)` 两个索引，含义如下：

- 前缀：`&self[..start]`（连续满足 `f` 的开头部分）
- 中段：`&self[start..end]`（不满足 `f` 的核心部分）
- 后缀：`&self[end..]`（连续满足 `f` 的结尾部分，且与前缀不重叠）

计算步骤：

1. `start` = 从头开始第一个**不满足** `f` 的位置，用 `position(|v| !f(v))`，找不到则整个切片都是前缀 → `start = len`。
2. 从 `start` 之后找最后一个**不满足** `f` 的位置，`+1` 得到 `end`；若 `start` 之后全满足 `f`，则 `end = start`（中段为空）。

边界约定（见 [src/lib.rs:142-145](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L142-L145)）：若所有元素都满足 `f`，前缀取整个 `self`，后缀为空。

#### 4.4.3 源码精读

实现（[src/lib.rs:179-190](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L179-L190)）：

```rust
fn split_prefix_suffix<F>(&self, mut f: F) -> (usize, usize)
where
    F: FnMut(&T) -> bool,
{
    let start = self.iter().position(|v| !f(v)).unwrap_or(self.len());
    let end = self
        .iter()
        .skip(start)
        .rposition(|v| !f(v))
        .map_or(start, |i| start + i + 1);
    (start, end)
}
```

几个值得品味的写法：

- **`position(|v| !f(v)).unwrap_or(self.len())`**：`position` 找不到返回 `None`，用 `unwrap_or(len)` 把「全是前缀」这一边界统一成 `start = len`，无需额外 `match`。
- **`skip(start).rposition(...)`**：跳过前缀，只在剩余部分从右往左找最后一个不满足 `f` 的元素。`rposition` 返回的是相对 `skip(start)` 之后子迭代器的索引，所以要 `start + i + 1` 还原到原切片坐标。
- **`map_or(start, |i| start + i + 1)`**：若 `start` 之后全部满足 `f`（即 `rposition` 返回 `None`），说明中段为空、后缀直接从 `start` 开始，故 `end = start`。

对照 `trim_start_matches` / `trim_end_matches`（[src/lib.rs:152-173](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L152-L173)）：那两个函数本质上是 `split_prefix_suffix` 的「只取中段」特例——`trim_start_matches` 相当于只算 `start` 并返回 `&self[start..]`，`trim_end_matches` 相当于只算 `end` 并返回 `&self[..end]`。

#### 4.4.4 代码实践

**实践目标**：用 `split_prefix_suffix` 剥离数组首尾的 `0`，同时拿到被剥下的前缀、中段、后缀三部分。

**操作步骤**（**示例代码**）：

```rust
use typst_utils::SliceExt;

fn main() {
    let xs = [0, 0, 3, 7, 0, 1, 0, 0];
    let (start, end) = xs.split_prefix_suffix(|&v| v == 0);
    let (prefix, mid, suffix) = (&xs[..start], &xs[start..end], &xs[end..]);
    println!("prefix={prefix:?} mid={mid:?} suffix={suffix:?}");
    assert_eq!(prefix, &[0, 0]);
    assert_eq!(mid, &[3, 7, 0, 1]);
    assert_eq!(suffix, &[0, 0]);
}
```

**需要观察的现象**：中段 `[3, 7, 0, 1]` 里**夹着一个 `0`** 但未被当作后缀，因为后缀必须是「连续满足 `f` 的结尾」，中间的 `0` 不连续到末尾。

**预期结果**：打印 `prefix=[0, 0] mid=[3, 7, 0, 1] suffix=[0, 0]`。（**待本地验证**。）

#### 4.4.5 小练习与答案

**练习 1**：若输入是 `[0, 0, 0]`（全部满足 `f`），`(start, end)` 是多少？三段分别是什么？

**参考答案**：`position` 找不到不满足 `f` 的元素 → `start = 3`；`skip(3)` 后为空，`rposition` 返回 `None` → `end = start = 3`。于是前缀 = `[0,0,0]`、中段 = `[]`、后缀 = `[]`，符合「全是前缀」约定。

**练习 2**：能否用 `split_prefix_suffix` 复刻 `trim_start_matches` 的结果？

**参考答案**：能。`trim_start_matches(f)` 的结果等价于 `split_prefix_suffix(f)` 返回的 `(start, _)` 所对应的 `&self[start..]`。

---

## 5. 综合实践

把本讲的四个工具串起来，完成一个小任务：**对一组按部门排序的销售额记录，去重（同部门只保留最新一条）后，按部门分组汇总并打印**。

要求：

1. 准备按键（部门）已排序的数据，例如 `[('a',10),('a',20),('a',30),('b',5),('b',7),('c',100)]`，装入 `SmallVec`。
2. 用 `Rdedup::rdedup_by_key` 去重，验证得到 `[('a',30),('b',7),('c',100)]`（同部门保留后值）。
3. 用 `SliceExt::group_by_key` 对去重后的切片按部门分组（虽然此时每部门只剩一条，但流程仍成立），遍历打印每个部门及其金额。
4. 进阶：再构造一个含 20 个元素的 `ListSet`，对去重后的每个部门名做 `contains` 判断，模拟「活跃部门白名单」查询。

参考骨架（**示例代码**，需自行补全并**待本地验证**结果）：

```rust
use smallvec::SmallVec;
use typst_utils::{ListSet, Rdedup, SliceExt};

fn main() {
    // 1. 已按键排序的记录
    let mut records: SmallVec<[_; 4]> =
        [('a', 10), ('a', 20), ('a', 30), ('b', 5), ('b', 7), ('c', 100)].into();

    // 2. 去重：同部门保留后值
    records.rdedup_by_key(|&mut (dept, _)| dept);
    assert_eq!(records.as_slice(), &[('a', 30), ('b', 7), ('c', 100)]);

    // 3. 按部门分组并打印
    for (dept, group) in records.group_by_key(|(d, _)| *d) {
        let total: i32 = group.iter().map(|(_, v)| *v).sum();
        println!("部门 {dept}: 金额合计 {total}");
    }

    // 4. 活跃部门白名单（>15 个元素触发 ListSet 二分路径）
    let whitelist: Vec<char> = ('a'..='z').collect(); // 26 个
    let active = ListSet::new(whitelist);
    assert!(active.contains(&'a'));
    assert!(!active.contains(&'1'));
    println!("白名单查询正常");
}
```

这个任务把「保留后值去重 → 连续键分组 → 大集合快查」三步连成一条线，正好覆盖本讲主线。

## 6. 本讲小结

- `ListSet` 把任意 `DerefMut<Target=[T]>` 容器包成只读集合，按 `CUT_OFF = 15` 在「构造零开销 + 线性查询」与「一次排序 + 二分查询」间自适应切换，适合小规模、只查不改的场景。
- `Rdedup::rdedup_by_key` 用双指针就地算法去重，关键是同键时写入游标 `k` 不前进、不断被后值覆盖，从而**保留最后一个**，与标准库 `dedup`（保留首个）相反；它要求输入同键连续，且只为 `SmallVec` 实现。
- `GroupByKey` 是 `group_by_key` 返回的惰性迭代器，零分配地按「连续相同键」切片，每次 `next` 用首元素定键、`take_while` 数连续个数、`split_at` 推进边界。
- `split_prefix_suffix` 用 `position` / `rposition` 计算两个索引，把切片切成「满足谓词的前缀 / 中段 / 满足谓词的后缀」三段，是 `trim_start/end_matches` 的更通用形式。
- 四个工具共享一条主线：**面向小集合、避免不必要的分配**——`ListSet` 复用现成切片、`Rdedup` 锚定 `SmallVec`、`GroupByKey` 与 `split_prefix_suffix` 全程借用原数据。

## 7. 下一步学习建议

- 下一讲 **u2-l6（哈希体系）** 会进入「真正用哈希」的集合工具（`hash128`、`LazyHash`、`HashLock`），与本讲「不靠哈希的小集合」形成对照，建议对比体会两种思路各自的适用规模。
- 若想看 `ListSet` / `Rdedup` / `group_by_key` 在 Typst 主仓里的真实调用点，可在仓库内搜索 `ListSet::new`、`rdedup_by_key`、`group_by_key`，观察它们被用于哪些「规模不大但要快速判重 / 分组」的环节。
- 进阶读者可思考：如果把 `Rdedup` 的实现从 `SmallVec` 扩展到任意 `Vec`，需要改动哪些约束？为什么作者刻意只实现 `SmallVec`？这有助于理解 trait 实现的「精准定向」设计。
