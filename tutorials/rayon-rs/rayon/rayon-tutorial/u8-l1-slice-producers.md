# 切片生产器家族

## 1. 本讲目标

学完本讲，你应该能够：

1. 读懂 `IterProducer` 的 `split_at` 实现，并解释为什么切片是 Rayon 里「最理想的生产者」——连续内存、长度已知、切分是 O(1) 的纯指针运算。
2. 区分 `par_chunks` / `par_chunks_exact` / `par_rchunks` / `par_windows` / `par_chunk_by` 五组视图的语义差异，以及它们各自的生产器如何在「块下标」与「元素下标」之间换算。
3. 理解可变变体（`par_chunks_mut` 等）如何凭 `split_at_mut` 把一份 `&mut [T]` 安全地分割成互不重叠的可变片段——这是「共享底层内存」的确切含义：共享的是同一块内存，但**分割**的是写权限。

本讲是 u4-l2（Producer 契约）在真实数据源上的落地：那里我们抽象地学了「可切分的 IntoIterator」，这里看 Rayon 自己怎么写生产器。

## 2. 前置知识

本讲假设你已理解以下概念（均有前置讲义覆盖）：

- **Producer 契约**（u4-l2）：`Producer` 是拉模式侧的核心 trait，关键成员是 `into_iter`（叶子任务里转成串行迭代器）、`split_at(index)`（按值消费自己、返回两个独立生产者）以及默认的粒度窗口 `min_len` / `max_len`。长度不由生产者自报，而由框架从 `IndexedParallelIterator::len()` 记账。
- **bridge 递归**（u3-l3 / u4-l1）：索引迭代器被消费时，`bridge` 取 `len` 的中点 `mid`，让生产者与消费者在**同一个 mid** 上对齐切分，再用 `join_context` 并行两半；`with_min_len` / `with_max_len` 通过 `LengthSplitter` 裁决每一刀。切分粒度只影响性能、不影响结果。
- **无索引切分**（u3-l6 / u4-l2）：`UnindexedProducer::split()` 是「按能力切分」——切分点由数据自身决定，找不到合法边界就返回 `None`，由 `bridge_unindexed` 回退串行。本讲的 `par_chunk_by` 正属此类。
- **标准库工具**：`&[T]::split_at(i)` 与 `&mut [T]::split_at_mut(i)` 是把一个切片分成前后两半的所有权操作；`div_ceil` 是向上取整除法。
- **借用约束规则**（u2-l2）：共享读要求 `T: Sync`，可变写要求 `T: Send`。本讲会反复看到这两条边界线。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/slice/mod.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/mod.rs) | slice 模块入口：定义 `ParallelSlice` / `ParallelSliceMut` 两个扩展 trait（`par_chunks` 等方法的声明处），以及最基础的 `Iter` / `IterMut` 生产器、`par_split` 家族与排序方法（排序留给 u8-l2） |
| [src/slice/chunks.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/chunks.rs) | `Chunks` / `ChunksExact` 及其可变变体的迭代器与生产器 |
| [src/slice/rchunks.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/rchunks.rs) | 从尾部开始的 `RChunks` / `RChunksExact` 家族，切分时左右互换 |
| [src/slice/windows.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/windows.rs) | 重叠滑动窗口 `Windows` 与定长数组窗口 `ArrayWindows` |
| [src/slice/chunk_by.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/chunk_by.rs) | 谓词定界的 `ChunkBy` / `ChunkByMut`，无索引生产器 |

结构规律：每个文件都是「公开迭代器结构体（小而薄，只存参数与切片）＋ `ParallelIterator` / `IndexedParallelIterator` 实现 ＋ 私有 Producer」三层。公开层做参数校验与长度记账，生产器只负责 `into_iter` 与 `split_at` 两个纯函数式的操作。

## 4. 核心概念与源码讲解

### 4.1 切片生产器：O(1) 的 split_at

#### 4.1.1 概念说明

在所有数据源里（对照 u2-l2 的梳理），切片是模范生：

- **内存连续**：第 i 个元素地址可由首地址直接算出，缓存友好；
- **长度已知**：`len()` 就是 `slice.len()`，`opt_len` 恒返回 `Some`，因此切片迭代器全部实现 `IndexedParallelIterator`，能用 `zip`、`enumerate`、`with_min_len`，`collect` 还能走 u4-l4 讲过的「精确预分配直写」快速路径；
- **切分零成本**：`split_at` 不搬数据、不分配内存，只做一次指针比较与两次指针调整，是纯所有权算术。

对 `&v` 调 `par_iter()` 时（u2-l2 讲过这是 `(&v).into_par_iter()` 的 blanket 语法糖），最终落到 `&[T]` 的 `IntoParallelIterator` 实现：

[src/slice/mod.rs:L776-L783](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/mod.rs#L776-L783) —— 为 `&[T]` 实现 `IntoParallelIterator`，产出元素类型为 `&T` 的 `Iter` 迭代器；这是所有共享读切片并行的总入口。

`Iter` 本身只是切片的包装（[src/slice/mod.rs:L813-L816](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/mod.rs#L813-L816)），它的两个 trait 实现把「长度记账」与「交出生产器」分离开：

[src/slice/mod.rs:L824-L857](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/mod.rs#L824-L857) —— `ParallelIterator` 实现里 `opt_len` 返回 `Some(self.len())`（自报长度已知）；`IndexedParallelIterator` 实现里 `drive` 走 `bridge`，`len` 直接转发 `slice.len()`，`with_producer` 则把内部的 `IterProducer` 交给回调。

#### 4.1.2 核心流程

从 `par_iter()` 到元素被消费的完整链路：

```text
&[T]
 │ into_par_iter()                    （IntoParallelIterator）
 ▼
Iter { slice }                        （公开迭代器：薄包装）
 │ drive / drive_unindexed            （两条驱动路径都进 bridge）
 ▼
bridge(self, consumer)
 │ with_producer(Callback)            （索引进代器必须交出生产者）
 ▼
IterProducer { slice }
 │ split_at(mid)  ──递归──┐           （bridge 每层取 len 中点，
 │                        │            生产者/消费者在同一 mid 对齐切分）
 ▼                        │
叶子任务: into_iter() ◄────┘           （转成 std::slice::Iter）
 │ fold_with                            （拉转推，Folder 串行吃元素）
 ▼
消费者完成
```

关键点回顾（u4-l2）：`split_at(index)` 的 `index` 是**元素下标**（由框架的 `mid` 给出），不是任务编号；生产者按值消费自己，返回的两个新生产器各持有一半的所有权，彼此完全独立。

#### 4.1.3 源码精读

[src/slice/mod.rs:L859-L875](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/mod.rs#L859-L875) —— `IterProducer` 的完整 `Producer` 实现，只有十几行：`into_iter` 转成标准库的 `std::slice::Iter`，`split_at` 直接转发 `self.slice.split_at(index)` 再各自包一层。注意这里没有任何加锁、计数或数据搬动——切分就是「把一段内存的边界换个说法」。

可变版本 `IterMutProducer` 结构完全相同，唯一差别是调用 `split_at_mut`：

[src/slice/mod.rs:L922-L937](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/mod.rs#L922-L937) —— 可变生产器用 `split_at_mut(index)` 把一份 `&mut [T]` 分成前后两半；这是标准库提供的、把一个可变借用安全拆成两个**不重叠**可变借用的唯一惯用原语（详见 4.3 节）。

顺带一提同文件里的 `par_split` 家族：`Split` 没有实现 `IndexedParallelIterator`，因为按谓词切分的段数在求值前不可知，它走 `drive_unindexed`：

[src/slice/mod.rs:L960-L974](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/mod.rs#L960-L974) —— `Split` 的 `drive_unindexed` 构造 `SplitProducer` 后交给 `bridge_unindexed`；其切分能力由 [src/slice/mod.rs:L1017-L1063](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/mod.rs#L1017-L1063) 的 `Fissile for &[T]` 提供（`midpoint`、向前/向后找分隔符、`split_once`）。这一「中点附近找边界」的设计思想在 4.2 的 `ChunkBy` 里会再次出现。

#### 4.1.4 代码实践

**实践目标**：亲眼确认「切分只改边界、不改结果顺序」，并验证切片迭代器的索引能力。

**操作步骤**（示例代码，非仓库原有）：

1. 新建一个 Cargo 项目并添加 `rayon = "1"` 依赖（步骤同 u1-l3）。
2. 写入以下 `main.rs`：

```rust
use rayon::prelude::*;

fn main() {
    let v: Vec<i32> = (0..16).collect();

    // 切片迭代器是 indexed 的：enumerate 可用，i 就是元素下标
    let starts: Vec<(usize, i32)> = v
        .as_slice()
        .par_iter()
        .enumerate()
        .filter(|&(i, _)| i % 4 == 0)
        .map(|(i, x)| (i, *x))
        .collect();
    println!("every 4th: {:?}", starts);

    // 强制细粒度切分，观察 collect 结果顺序仍与串行一致
    let fine: Vec<i32> = v
        .as_slice()
        .par_iter()
        .with_max_len(1)
        .copied()
        .collect();
    let serial: Vec<i32> = v.iter().copied().collect();
    assert_eq!(fine, serial);
    println!("order preserved: {}", fine == serial);
}
```

**需要观察的现象**：`every 4th` 打印 `[(0, 0), (4, 4), (8, 8), (12, 12)]`；`with_max_len(1)` 把每个元素切成独立任务后，`collect` 的结果仍与串行完全相同。

**预期结果**：断言通过。原因在 u4-l4 讲过——各段写入的位置由切分区间决定，与任务完成顺序（工作窃取）无关。

#### 4.1.5 小练习与答案

**练习 1**：`IterProducer::split_at` 里没有任何同步原语，为什么两个子生产器可以安全地被不同线程持有？

**参考答案**：`split_at` 按值消费 `self`，用 `slice.split_at` 把**所有权**拆成前后两段；两个子切片在内存上不重叠，类型系统保证各自独占。不需要运行期同步——安全性由借用检查在编译期静态保证。

**练习 2**：`Iter` 为什么同时实现 `drive` 与 `drive_unindexed`？既然有索引，留 `drive_unindexed` 有何意义？

**参考答案**：`drive_unindexed` 是 `ParallelIterator` 的底层入口（无索引消费者走这条路），任何并行迭代器都必须提供；`drive` 是 `IndexedParallelIterator` 追加的精确路径。回顾 u4-l3 的驱动关系：任何生产者都能驱动无索引消费者，反之不行——所以两个入口都要有，`opt_len` 返回 `Some` 还能让无索引消费方（如 `collect`）自动选择更快的路径。

**练习 3**：如果 `bridge` 递归切分时传给 `split_at` 的 `index` 越界（大于 `len`），`IterProducer` 会怎样？

**参考答案**：`slice.split_at(index)` 在 `index > len` 时 panic。但这条路径不会发生：`mid` 由框架从 `len()` 计算（通常取 `len / 2`），天然在 `0..=len` 内。生产器契约假设框架守约——这也解释了 u4-l2 强调「长度由框架记账」：切分点永远来自已知长度，而非生产者自估。

### 4.2 分块视图家族：chunks / windows / chunk_by / rchunks

#### 4.2.1 概念说明

「视图」指**不搬运数据**的只读窗口：迭代器的每个元素本身是一段子切片 `&[T]`，指向原内存。四组视图按「块的边界由谁决定」分类：

| 视图 | 块边界 | 块宽 | 是否重叠 | 是否索引 |
| --- | --- | --- | --- | --- |
| `par_chunks(c)` | 固定步长 \(c\) | 恰 \(c\)（末块可短） | 否 | 是，块数 \(\lceil n/c \rceil\) |
| `par_chunks_exact(c)` | 固定步长 \(c\) | 恒 \(c\) | 否 | 是，块数 \(\lfloor n/c \rfloor\)，余数单独取 |
| `par_rchunks(c)` / `par_rchunks_exact(c)` | 同上，但从尾部数起 | 同上 | 否 | 是 |
| `par_windows(w)` | 步长 1，窗口滑动 | 恒 \(w\) | **相邻重叠 \(w-1\) 个元素** | 是，窗口数 \(n-w+1\) |
| `par_chunk_by(pred)` | 谓词在相邻两元素间为假处 | 任意 | 否 | **否**（段数求值前不可知） |

`par_chunk_by` 无索引是必然的：块边界依赖谓词的计算结果，切分前无法知道段数，因此不能 `enumerate`、不能 `zip`——与 u2-l1「`filter` 丢索引」同理，且不可恢复。

所有方法的声明集中在两个扩展 trait 上：

- [src/slice/mod.rs:L110-L148](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/mod.rs#L110-L148) —— `par_chunks` 与 `par_chunks_exact` 的声明：两者都带 `#[track_caller]` 并断言 `chunk_size != 0`，分别构造 `Chunks` / `ChunksExact`。
- [src/slice/mod.rs:L82-L108](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/mod.rs#L82-L108) —— `par_windows` 与 `par_array_windows`：前者返回 `Windows`，后者返回元素为 `&[T; N]` 的 `ArrayWindows`。
- [src/slice/mod.rs:L190-L211](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/mod.rs#L190-L211) —— `par_chunk_by`：谓词作用在**相邻两个**元素上（`slice[0]` 与 `slice[1]`、`slice[1]` 与 `slice[2]`……），为假即切块。

#### 4.2.2 核心流程

设切片长度为 \(n\)。`par_chunks(c)` 的第 \(i\) 块覆盖元素区间：

\[ [\,i \cdot c,\ \min((i{+}1) \cdot c,\ n)\,) \]

块数为 \(\lceil n/c \rceil\)（源码用 `div_ceil`）。**块下标 → 元素下标**的换算是乘法，于是 `split_at(index)`（`index` 为块下标）三步走：

```text
块下标 index ──×chunk_size──► 元素下标 elem_index（clamp 到 n）
              ──slice.split_at──► 左右两个子切片
              ──各包一层 ChunksProducer──► 返回（左, 右）
```

`par_windows(w)` 的第 \(i\) 个窗口覆盖 \([i,\ i+w)\)，窗口数 \(\max(0,\ n-w+1)\)。切分时**两侧必须重叠**：左侧要完整产出前 `index` 个窗口，需要多带 \(w-1\) 个元素；右侧从第 `index` 个窗口的起点（元素下标 `index`）开始。

`par_rchunks(c)` 的第 \(i` 块（从尾部数）覆盖 \([\max(0,\ n-(i{+}1)c),\ n-i \cdot c)\)。切分时先算出元素分界点，再**交换左右**返回——因为块序与内存序方向相反，这与 u3-l3 `rev` 的镜像处理同源。

`par_chunk_by` 走无索引路径：`split()` 在中点附近**先向前、找不到再向后**搜索谓词失配点作为块边界；找不到任何边界则返回 `None` 表示不可再分（回退串行），由 `bridge_unindexed` 递归（对照 u3-l6）。

#### 4.2.3 源码精读

**Chunks：乘法换算 + clamp**

[src/slice/chunks.rs:L46-L48](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/chunks.rs#L46-L48) —— `len` 用 `div_ceil` 报告块数；这就是框架记账用的长度。

[src/slice/chunks.rs:L74-L87](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/chunks.rs#L74-L87) —— `ChunksProducer::split_at` 的全部逻辑：`elem_index = Ord::min(index * chunk_size, slice.len())` 把块下标换算成元素下标并夹到尾部（最后一块可能不满，换算可能越过末端），再 `split_at` 一分为二。`Ord::min` 的存在正是「末块可短」的体现。

**ChunksExact：构造时先把余数切走**

[src/slice/chunks.rs:L98-L108](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/chunks.rs#L98-L108) —— `ChunksExact::new` 在**构造时**就把余数 `split_at` 出去存进 `rem` 字段，之后主切片长度恰为块数的整倍数。

[src/slice/chunks.rs:L175-L188](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/chunks.rs#L175-L188) —— 因此 `ChunksExactProducer::split_at` 的换算是裸乘法 `index * chunk_size`，**不再需要** `Ord::min` 夹逼——不变式「长度整除」在构造时已建立。余数经 `remainder()` 取回（[src/slice/chunks.rs:L110-L115](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/chunks.rs#L110-L115)）。

**Windows：重叠切分**

[src/slice/windows.rs:L46-L49](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/windows.rs#L46-L49) —— `len` 断言 `window_size >= 1` 后用 `saturating_sub(window_size - 1)` 得到窗口数（短切片返回 0 而不是下溢）。

[src/slice/windows.rs:L75-L89](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/windows.rs#L75-L89) —— `WindowsProducer::split_at` 是本讲最值得琢磨的五行：左侧切到 `min(len, index + window_size - 1)`（多带 \(w-1\) 个元素才能凑出最后一个完整窗口），右侧从 `index` 开始——**两个子切片在内存上重叠 \(w-1\) 个元素**。能这样做的前提是元素类型为共享引用 `&[T]`；这也解释了 API 里根本没有 `par_windows_mut`：两个可变窗口重叠是别名错误，`split_at_mut` 造不出来。

[src/slice/windows.rs:L138-L146](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/windows.rs#L138-L146) —— `ArrayWindows::with_producer` 没有独立生产器，而是复用 `Windows` 生产器再接一个 `map(|slice| slice.try_into().unwrap())` 把 `&[T]` 转成 `&[T; N]`——库内部也在用「适配器组合」而非重写。

**RChunks：换算后交换左右**

[src/slice/rchunks.rs:L74-L87](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/rchunks.rs#L74-L87) —— `RChunksProducer::split_at`：`elem_index = len.saturating_sub(index * chunk_size)`，切开后返回 `(right, left)`——**前 index 个块（块序在前）位于内存尾部**，所以块序在前的一半对应内存的右半。`saturating_sub` 同样是给「首块不满」兜底。

[src/slice/rchunks.rs:L98-L107](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/rchunks.rs#L98-L107) —— 对照 `RChunksExact::new`：余数被切在内存**前端**（`split_at(rem_len)` 的左半是 `rem`）——与 `ChunksExact` 的尾部余数正好镜像。

**ChunkBy：无索引的谓词切分**

[src/slice/chunk_by.rs:L6-L23](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/chunk_by.rs#L6-L23) —— 私有 trait `ChunkBySlice` 抽象出 `&[T]` 与 `&mut [T]` 的公共能力：`split`、`chunk_by`，以及用 `windows(2).position(|w| !pred(...))` 实现的边界查找 `find` / `rfind`（找到的是**新块首元素**的下标）。

[src/slice/chunk_by.rs:L60-L102](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/chunk_by.rs#L60-L102) —— `ChunkByProducer::split()`（`UnindexedProducer`）：`tail < 2` 时返回 `None`（单个元素无从再分）；否则取 `mid = tail / 2`，**先在 `[mid, tail)` 向前找边界、找不到再从 `mid+1` 向后找**，找到一个就沿边界 `split` 成两半。`tail` 字段标记「尚未闭合的尾段」——它对应的块可能延续到邻居生产者的区域，所以不能贸然按块迭代，而是在 `fold_with` 里单独处理：

[src/slice/chunk_by.rs:L104-L135](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/chunk_by.rs#L104-L135) —— `fold_with` 三分支：`tail` 覆盖整个切片时直接 `consume_iter(slice.chunk_by(pred))` 串行产块；尾段能找到闭合边界时，前段走 `consume_iter`、尾段作为**一个完整块**经 `folder.consume(tail)` 交出；完全没有边界时整段就是一块，同样单点 `consume`。文件顶部的注释点明它与 `SplitProducer` 的同构性（呼应 mod.rs 的 `Fissile`）。

[src/slice/chunk_by.rs:L170-L191](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/chunk_by.rs#L170-L191) —— `ChunkBy` 只实现 `ParallelIterator`（无 `len` / `opt_len`），`drive_unindexed` 构造 `ChunkByProducer` 后交给 `bridge_unindexed`。注意 `pred` 是以引用 `&self.pred` 借给生产器的，因此谓词只需 `Send + Sync` 而不必 `Clone`。

#### 4.2.4 代码实践

**实践目标**：用运行结果验证三件事——块数公式、窗口重叠计数、`chunk_by` 无索引。

**操作步骤**（示例代码，非仓库原有）：

```rust
use rayon::prelude::*;

fn main() {
    let a = [1, 2, 2, 3, 3, 3, 4, 5, 6, 7]; // n = 10

    // (1) chunks：块数 = ceil(10/3) = 4，末块短
    let cs: Vec<_> = a.par_chunks(3).collect();
    println!("par_chunks(3) = {:?}，块数 = {}", cs, cs.len());

    // (2) windows：窗口数 = 10-2+1 = 9，相邻窗口重叠 1 个元素
    let ws: Vec<_> = a.par_windows(2).collect();
    let flat: usize = ws.iter().map(|w| w.len()).sum();
    println!("par_windows(2) 窗口数 = {}，元素总引用次 = {}", ws.len(), flat);

    // (3) chunk_by：相邻相等归为一块
    let bs: Vec<_> = a.par_chunk_by(|&x, &y| x == y).collect();
    println!("par_chunk_by = {:?}", bs);

    // (4) 试取消下一行注释，观察编译错误：
    // let _: Vec<_> = a.par_chunk_by(|&x, &y| x == y).enumerate().collect();
}
```

**需要观察的现象**：

1. `par_chunks(3)` 打印 4 块：`[1,2,2] [3,3,3] [4,5,6] [7]`；
2. 窗口数为 9，但元素总引用次为 18——**同一元素被多个窗口引用**，这正是重叠的直接证据；
3. `par_chunk_by` 打印 `[1] [2,2] [3,3,3] [4] [5] [6] [7]`；
4. 取消第 (4) 步注释后编译失败，错误信息是 `enumerate` 找不到方法——`ChunkBy` 未实现 `IndexedParallelIterator`。

**预期结果**：以上 4 项全部如期出现。前 3 项顺序是确定的：这些生产器按序产块，`collect` 按序拼接（u4-l4）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ChunksProducer::split_at` 要 `Ord::min` 而 `ChunksExactProducer` 不用？

**参考答案**：`Chunks` 的末块可能不足 `chunk_size`，若 `index` 恰为最后一块的下标，`index * chunk_size` 可能超过 `n`，必须夹逼到 `slice.len()`；`ChunksExact` 在 `new` 时已把余数切走，主切片长度恒为 `chunk_size` 的整倍数，`index * chunk_size ≤ n` 由不变式保证，无需运行期检查。

**练习 2**：`WindowsProducer::split_at` 返回的两个子切片在内存上是什么关系？若把 `Item` 改成 `&mut [T]` 会发生什么？

**参考答案**：重叠关系——左切片结尾与右切片开头共享 \(w-1\) 个元素（右切片从元素下标 `index` 开始，左切片延伸到 `index + w - 1`）。共享引用允许别名，所以安全；可变引用不允许别名，`split_at_mut` 无法产出重叠的两段 `&mut [T]`，因此 mutable windows 在类型层面就不可能实现——API 里也没有 `par_windows_mut`。

**练习 3**：`par_rchunks(2)` 对 `[1,2,3,4,5]` 产出什么？写出 `RChunksProducer::split_at(1)` 的换算过程。

**参考答案**：产出 `[4,5]`、`[2,3]`、`[1]`（从尾部起，首块在内存最后）。`split_at(1)`：`index=1`，`elem_index = 5.saturating_sub(1*2) = 3`，`split_at(3)` 得内存左段 `[1,2,3]`、右段 `[4,5]`；块序在前的一块（`[4,5]`）在内存右段，故返回 `(right, left)`。

### 4.3 可变变体：split_at_mut 与写权限的分割

#### 4.3.1 概念说明

「可变切片生产器共享底层内存」容易引起误解，先把话说准：所有可变块**共享同一块内存区域**（不拷贝、不换缓冲），但生产器把**写权限**做了不重叠的分割——任意时刻每个元素只有一个所有者。实现这一点的唯一安全原语就是 `split_at_mut`：它把 `&mut [T]` 拆成前后两段互不相交的 `&mut [T]`。

由此推出两条硬边界：

- **约束从 `Sync` 换成 `Send`**：`ParallelSlice<T: Sync>`（共享读，元素 `&T` 跨线程只需 `Sync`）与 `ParallelSliceMut<T: Send>`（可变写，`&mut T` 跨线程移动需要 `T: Send`）——正是 u2-l2 的规则在这两个 trait 签名上的直接体现（[src/slice/mod.rs:L31-L34](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/mod.rs#L31-L34) 与 [src/slice/mod.rs:L222-L226](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/mod.rs#L222-L226)）。
- **重叠视图无可变版**：windows 因重叠而无 `mut` 变体（4.2 已证）；chunks / rchunks / chunk_by / split 的块互不重叠，故都有对应 `_mut` 版本。

#### 4.3.2 核心流程

可变生产器的递归切分与只读版完全同构，只是每一步用 `split_at_mut` 替代 `split_at`：

```text
&mut [T]
 │ par_chunks_mut(c)                    （ParallelSliceMut，断言 c != 0）
 ▼
ChunksMut { chunk_size, slice }
 │ with_producer
 ▼
ChunksMutProducer
 │ split_at(块下标 index)
 │   elem_index = min(index * c, len)
 │   (左, 右) = slice.split_at_mut(elem_index)   ← 唯一差异点
 │   返回两个 ChunksMutProducer，各持不相交的 &mut [T]
 ▼
bridge 并行两半 → 叶子 into_iter() = slice.chunks_mut(c)
 ▼
每个任务在自己的 &mut 块里自由写入，无锁
```

#### 4.3.3 源码精读

[src/slice/mod.rs:L287-L291](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/mod.rs#L287-L291) —— `par_chunks_mut` 的声明：与只读版同样断言 `chunk_size != 0`，构造 `ChunksMut`。

[src/slice/chunks.rs:L255-L268](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/chunks.rs#L255-L268) —— `ChunksMutProducer::split_at`：与 `ChunksProducer` 逐行对照，唯一差别是 `split_at` 换成 `split_at_mut`。这就是「共享内存、分割写权限」的全部实现——没有锁、没有原子变量、没有运行期别名检查，安全性由类型系统静态承担。

[src/slice/rchunks.rs:L254-L267](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/rchunks.rs#L254-L267) —— `RChunksMutProducer::split_at`：`split_at_mut` 之后同样交换左右返回（块序与内存序相反）。

[src/slice/chunks.rs:L279-L319](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/chunks.rs#L279-L319) —— `ChunksExactMut` 的三个余数取法值得注意：`into_remainder(self)` 会消耗迭代器、夺走原生命周期，与「迭代器本身也要被消耗才能并行」冲突，因此文档建议用 `remainder(&mut self)` 或 `take_remainder(&mut self)`（后者用 `mem::take` 拿回 `'data` 生命周期，重复调用返回空切片）。这是「并行消费与借用生命周期打架」的一个小而真实的 API 设计案例。

[src/slice/chunk_by.rs:L218-L239](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/chunk_by.rs#L218-L239) —— `ChunkByMut`：`Item = &'data mut [T]`，与只读版共用同一个 `ChunkByProducer`——这正是 4.2 里 `ChunkBySlice` trait 存在的意义：`&[T]` 与 `&mut [T]` 两套实现（[chunk_by.rs:L25-L43](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/chunk_by.rs#L25-L43)）各提供 `split` / `chunk_by` 的对应版本（`split_at` vs `split_at_mut`、`chunk_by` vs `chunk_by_mut`），生产器逻辑只写一遍。

#### 4.3.4 代码实践

**实践目标**：验证可变块互不重叠，并体会 `T: Send` 边界。

**操作步骤**（示例代码，非仓库原有；第一段源自官方文档示例 [src/slice/mod.rs:L278-L286](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/mod.rs#L278-L286)）：

```rust
use rayon::prelude::*;
use std::sync::atomic::{AtomicUsize, Ordering};

fn main() {
    // (1) 官方示例：块内反转
    let mut array = [1, 2, 3, 4, 5];
    array.par_chunks_mut(2).for_each(|slice| slice.reverse());
    assert_eq!(array, [2, 1, 4, 3, 5]);
    println!("reversed in chunks: {:?}", array);

    // (2) 多线程计数器：每块各自累加，总次数 = 元素数
    let mut data = vec![0usize; 100];
    let calls = AtomicUsize::new(0);
    data.par_chunks_mut(10).for_each(|chunk| {
        for x in chunk {
            *x = 1;
            calls.fetch_add(1, Ordering::Relaxed);
        }
    });
    assert_eq!(calls.into_inner(), 100);
    assert_eq!(data.iter().sum::<usize>(), 100);
    println!("all {} elements written exactly once", data.len());
}
```

再做一个编译期实验：把上面的 `AtomicUsize` 换成普通 `usize` 计数器（`calls += 1`），重新编译。

**需要观察的现象**：程序 (1)(2) 正常运行且断言通过；换普通计数器后编译失败——闭包捕获了 `&mut usize`，而 `&mut usize` 不是 `Sync`，不满足跨线程共享调用的要求。

**预期结果**：断言全部通过；计数实验里 100 个元素恰好各被写一次，说明块与块之间没有重叠也没有遗漏。编译期实验失败是预期行为，错误信息会指出闭包不满足 `Send`/`Sync` 约束。

#### 4.3.5 小练习与答案

**练习 1**：两个并行的 `par_chunks_mut` 任务同时运行，为什么不需要锁来防止写冲突？

**参考答案**：因为它们写的内存区间在 `split_at_mut` 处就已经互不相交——每个任务拿到的是独占的 `&mut [T]`，Rust 的借用规则在编译期就排除了别名。锁是用来协调「对同一数据的访问」的；这里根本没有共享的写目标，自然无需锁。

**练习 2**：`par_chunk_by_mut` 的谓词需要什么约束？为什么它比块大小参数更「贵」？

**参考答案**：`Fn(&T, &T) -> bool + Send + Sync`。`Send + Sync` 因为谓词以共享引用传给多个线程的生产器（4.2.3 末尾提到的 `&self.pred` 借用方式）；「更贵」在于每次切分都要在数据上**实际运行**谓词来定位边界（`find`/`rfind` 扫描），而 `par_chunks_mut` 的边界是纯算术换算——这也是 chunk_by 无索引、不能 `enumerate` 的根因。

**练习 3**：如果要在并行任务之间真正**共享可变状态**（比如全局计数器），正确姿势是什么？

**参考答案**：不要通过切片数据去做（切片被分割成互不相交的块），而是让闭包捕获 `AtomicUsize` 之类的同步原语（如 4.3.4 的实践），或者回顾 u3-l2 的 `map_with` / `fold`：每任务一份本地状态、最后 `reduce` 合并——避免共享计数器的原子争用。

## 5. 综合实践

本讲综合实践即规格中指定的任务：**对同一数组分别用 `par_chunks(3)`、`par_chunk_by`、`par_windows(2)` 打印分组结果，并画出各自在内存中的视图示意图**，外加反向与可变变体收尾。

1. **实践目标**：把三组视图的语义差异「画」出来，内化块边界由谁决定这一主线。
2. **操作步骤**（示例代码，非仓库原有）：

```rust
use rayon::prelude::*;

fn main() {
    let a = [1, 2, 2, 3, 3, 3, 4, 5, 6, 7];

    let chunks: Vec<String> = a
        .par_chunks(3)
        .map(|c| format!("{:?}", c))
        .collect();
    let by: Vec<String> = a
        .par_chunk_by(|&x, &y| x == y)
        .map(|c| format!("{:?}", c))
        .collect();
    let wins: Vec<String> = a
        .par_windows(2)
        .map(|w| format!("{:?}", w))
        .collect();
    let rchunks: Vec<String> = a
        .par_rchunks(3)
        .map(|c| format!("{:?}", c))
        .collect();

    println!("par_chunks(3)    : {}", chunks.join(" | "));
    println!("par_chunk_by     : {}", by.join(" | "));
    println!("par_windows(2)   : {}", wins.join(" | "));
    println!("par_rchunks(3)   : {}", rchunks.join(" | "));

    let mut b = a;
    b.par_chunks_mut(5).for_each(|c| c.reverse());
    println!("par_chunks_mut(5): {:?}", b);
}
```

3. **需要观察的现象与预期结果**（顺序确定，可直接核对）：

```text
par_chunks(3)    : [1, 2, 2] | [3, 3, 3] | [4, 5, 6] | [7]
par_chunk_by     : [1] | [2, 2] | [3, 3, 3] | [4] | [5] | [6] | [7]
par_windows(2)   : [1, 2] | [2, 2] | [2, 3] | [3, 3] | [3, 3] | [3, 4] | [4, 5] | [5, 6] | [6, 7]
par_rchunks(3)   : [5, 6, 7] | [3, 4, 5] | [2, 2, 3] | [1]
par_chunks_mut(5): [3, 3, 2, 2, 1, 7, 6, 5, 4, 3]
```

4. **画出内存视图示意图**（下标 0..9，参考答案）：

```text
内存:          0   1   2   3   4   5   6   7   8   9
               1   2   2   3   3   3   4   5   6   7

par_chunks(3): [=======] [=======] [=======] [==]        等宽不重叠，末块短
par_chunk_by:  [=] [====] [=========] [=] [=] [=] [=]    谓词定界，宽度任意
par_windows(2):[==][==][==][==][==][==][==][==][==]       步长 1，相邻重叠 1
par_rchunks(3):[=] [=====] [=========] [=========]        从尾部起数（块序 ← 内存序）
par_chunks_mut:[=============] [=============]            与 chunks 同构 + 写权限分割
```

画完后自检三问：`par_chunks` 与 `par_rchunks` 的短块各在哪一端？`par_windows` 的窗口数为什么比元素数少 1？`par_chunk_by` 为什么画不出等宽的格子？

## 6. 本讲小结

- 切片是 Rayon 最理想的生产者：`IterProducer::split_at` 只是 `slice.split_at` 的转发，纯指针运算、零分配零同步，`opt_len` 恒有值使其全享索引能力。
- `par_chunks` 家族的生产器核心是「块下标 ↔ 元素下标」的乘法换算：`Chunks` 用 `Ord::min` 给末块兜底，`ChunksExact` 在构造时切走余数从而免于夹逼，`RChunks` 换算后再交换左右（块序与内存序相反）。
- `WindowsProducer::split_at` 让两个子切片**重叠** \(w-1\) 个元素——共享引用允许别名故可行，也正因如此 API 里不存在可变窗口。
- `par_chunk_by` 是谓词定界的无索引生产器：`split()` 在中点附近先向前、再向后找谓词失配点，`tail` 字段标记可能延续到邻居的未闭合尾段，在 `fold_with` 里作为完整块单独交出。
- 可变变体与只读版逐行同构，唯一差异是 `split_at_mut`：「共享底层内存」的准确含义是共享内存区域、**分割写权限**，安全由类型系统静态保证，无需锁。
- 约束边界清晰可循：共享视图要求 `T: Sync`，可变视图要求 `T: Send`。

## 7. 下一步学习建议

下一讲 **u8-l2 并行归并排序** 将把本讲的生产器用到大户上：[src/slice/sort.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/sort.rs) 里的 `par_mergesort` / `par_quicksort` 正是靠 `par_chunks` 式的分割与 `join` 递归完成分治，还涉及临时缓冲区的借用管理。建议先自行浏览该文件的 `split` 与合并循环，留意它如何在不引入别名的前提下在输入与缓冲区之间搬运数据；读完后再对照 [src/slice/test.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/test.rs) 的测试体会确定性检验的写法。
