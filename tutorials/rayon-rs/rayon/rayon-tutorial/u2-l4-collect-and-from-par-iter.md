# collect：把结果收集回来

## 1. 本讲目标

学完本讲，你应该能够：

- 熟练使用 `collect` 把并行迭代器的结果收进 `Vec`、`String`、`HashMap` 等集合，并理解泛型参数 `C` 是如何决定目标类型的。
- 说出 `collect`、`collect_into_vec`、`collect_vec_list` 三个收集出口各自适合的场景。
- 理解 `FromParallelIterator`（从零新建集合）与 `ParallelExtend`（往已有集合追加）两个 trait 的分工，以及 `collect` 如何通过 `from_par_iter` → `par_extend` 层层落地。
- 掌握 `unzip`、`partition`、`partition_map` 这类「一次遍历、两个结果」的方法，并了解它们内部通过「假迭代器拦截消费者」实现的巧妙技巧。

本讲是使用层（单元二）的收尾：前面几讲解决了「数据从哪来」「有哪些集合能并行迭代」，本讲解决「结果怎么回去」。

## 2. 前置知识

- **惰性与立即执行**（承接 u2-l1）：`map`、`filter` 这类适配器是惰性的，只包装不计算；`collect`、`sum`、`for_each` 这类消费者是立即执行的，由它们触发真正的任务切分与线程派发。`collect` 是最常用的消费者。
- **两条驱动路径**（承接 u2-l1）：`ParallelIterator::drive_unindexed` 配合 `opt_len()` 返回 `None` 时走「任意对半切分」路径；`IndexedParallelIterator::drive` 配合 `opt_len()` 返回 `Some(len)` 时可走「已知长度、精确预分配」路径。本讲会反复看到 `opt_len` 出现在分岔口。
- **Rust 的 `FromIterator` 与 `Extend`**：标准库串行世界里，`iter.collect::<Vec<_>>()` 之所以能工作，是因为 `Vec` 实现了 `FromIterator`；`vec.extend(iter)` 则依赖 `Extend`。Rayon 镜像了这一设计，提供 `FromParallelIterator` 与 `ParallelExtend`。理解成「并行版的 collect 协议」即可。
- **turbofish 语法**：`collect::<Vec<_>>()` 中的 `::<...>` 用于显式告知编译器目标类型。并行世界里它同样不可或缺，因为 `collect` 的目标类型几乎总要靠它或类型标注来消歧。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/iter/mod.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs) | 定义 `ParallelIterator::collect`、`unzip`、`partition`、`partition_map`、`collect_vec_list`，以及 `IndexedParallelIterator::collect_into_vec`、`unzip_into_vecs`；文末定义 `FromParallelIterator` 与 `ParallelExtend` 两个 trait |
| [src/iter/collect/mod.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/collect/mod.rs) | 收集的驱动骨架：`collect_into_vec`、`special_extend`、`unzip_into_vecs` 与核心辅助函数 `collect_with_consumer`（预分配、校验写入次数） |
| [src/iter/from_par_iter.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/from_par_iter.rs) | 为 `Vec`、`Box<[T]>`、`VecDeque`、`HashMap`、`String` 等约 20 个目标类型实现 `FromParallelIterator` |
| [src/iter/extend.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/extend.rs) | 为各集合实现 `ParallelExtend`；包含 `extend!` / `extend_reserved!` 宏、`fast_collect` 与 `ListVecConsumer` |
| [src/iter/unzip.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/unzip.rs) | `unzip` / `partition` / `partition_map` 的实现：`UnzipOp` 抽象、`UnzipA`/`UnzipB` 假迭代器、`UnzipConsumer`，以及元组的 `ParallelExtend` / `FromParallelIterator` 实现 |
| [src/result.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/result.rs) | `Result<C, E>: FromParallelIterator<Result<T, E>>` 的短路收集实现 |
| [tests/collect.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/tests/collect.rs) | 集成测试：验证 panic 时已产出元素恰好被 drop 一次 |

## 4. 核心概念与源码讲解

### 4.1 collect 家族：一个方法，三个出口

#### 4.1.1 概念说明

并行计算的典型形态是「切分 → 各线程算各的 → 把结果合并回来」。`collect` 就是最后一步的统一入口：它把散落在多个线程里的产出，拼装成一个完整的集合。

但「收集」其实有三个不同层次的出口，适用于不同场景：

| 出口 | 所属 trait | 特点 |
| --- | --- | --- |
| `collect::<C>()` | `ParallelIterator` | 最通用：任何实现了 `FromParallelIterator` 的类型都能作为目标 |
| `collect_into_vec(&mut vec)` | `IndexedParallelIterator` | 复用调用方提供的 `Vec` 缓冲区，避免重复分配 |
| `collect_vec_list()` | `ParallelIterator` | 收成 `LinkedList<Vec<T>>`，是「并行转串行」的桥梁 |

三者中 `collect` 是日常主力，后两个是性能与工程上的补充。

#### 4.1.2 核心流程

`collect` 的执行流程可以概括为：

```text
par_iter.collect::<C>()
    └─> C::from_par_iter(par_iter)          // 委托给目标类型
            └─> C::default() 再 par_extend  // 大多数实现走的通用路线
                    └─> opt_len() 分岔：
                        Some(len) -> 预分配精确空间，按段直接写入（快路径）
                        None      -> 每个任务局部攒一个 Vec，最后按序拼接
```

关键点：`collect` 自己几乎不做工作，全部行为由目标类型 `C` 的 `FromParallelIterator` 实现决定。这就是「目标端契约」的设计——Rayon 不枚举「支持哪些收集」，而是开放一个 trait 让任何类型自证「我知道怎么收」。

#### 4.1.3 源码精读

先看 `collect` 本体，它只有一行，是个纯粹的转发：

[src/iter/mod.rs:L2063-L2068](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L2063-L2068) — `ParallelIterator::collect` 把自己交给 `C::from_par_iter`，目标类型 `C` 必须实现 `FromParallelIterator<Self::Item>`。`Item` 是元素类型，`C` 是集合类型，二者的匹配由编译器检查。

它的文档注释里展示了一个容易被忽略的能力——收集成嵌套的元组与 `Either`：

[src/iter/mod.rs:L1991-L2002](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L1991-L2002) — 元组 `(Vec<_>, Vec<_>)` 本身也可以是 collect 的目标：产出 `(A, B)` 元组的迭代器直接 `.collect()` 就能一步得到两个 `Vec`，效果等同于 `unzip`。

再看快速路径 `collect_into_vec`：

[src/iter/mod.rs:L2532-L2534](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L2532-L2534) — 只对 `IndexedParallelIterator` 开放，因为它需要 `len()` 来预分配。文档明确提示：反复调用时复用同一个 `Vec` 可以复用底层缓冲区。

[src/iter/collect/mod.rs:L13-L21](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/collect/mod.rs#L13-L21) — 实现分三步：`clear` 清掉旧数据、取 `len`、调用 `collect_with_consumer` 预分配并驱动迭代器。

真正精妙的是 `collect_with_consumer` 的收尾校验：

[src/iter/collect/mod.rs:L75-L114](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/collect/mod.rs#L75-L114) — 这段代码先 `vec.reserve(len)` 预留空间，构造 `CollectConsumer::appender` 让各任务把元素写进指定区间；执行完毕后断言「实际写入数 == 预期长度」（L99-L103），只有校验通过才 `set_len` 把向量长度一次性设为新值。这意味着：**任何时刻都不会把未初始化的内存暴露为有效元素**；如果某个生产者少产出了元素，这里会直接 panic 而不是返回半成品。`CollectConsumer` 内部如何按区间写入属于单元四的 plumbing 深水区，本讲只需记住「预分配 + 分段写入 + 写满校验」三步。

最后是第三个出口 `collect_vec_list`：

[src/iter/mod.rs:L2385-L2396](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L2385-L2396) — 把结果收成 `LinkedList<Vec<T>>`：有长度信息时是一整个 `Vec`，无长度信息时是多个分块。文档给出的用法（L2380-L2382 注释）非常实用：`collect_vec_list().into_iter().flatten()` 可以把并行迭代器「降级」回普通串行迭代器，中间不引入锁。

#### 4.1.4 代码实践

1. **实践目标**：体验三个收集出口的差异。
2. **操作步骤**（示例代码，可放在任一依赖 rayon 的工程里）：

```rust
use rayon::prelude::*;
use std::collections::LinkedList;

fn main() {
    // 出口一：普通 collect
    let v: Vec<(usize, i32)> = (100..108)
        .into_par_iter()
        .enumerate()
        .collect();
    println!("{v:?}");

    // 出口二：collect_into_vec，复用缓冲区
    let mut buf = vec![0; 8]; // 旧数据会被清空
    (0..8).into_par_iter().map(|x| x * x).collect_into_vec(&mut buf);
    assert_eq!(buf, [0, 1, 4, 9, 16, 25, 36, 49]);

    // 出口三：collect_vec_list
    let list: LinkedList<Vec<i32>> = (0..10)
        .into_par_iter()
        .filter(|x| x % 2 == 0)
        .collect_vec_list();
    let serial: Vec<i32> = list.into_iter().flatten().collect();
    assert_eq!(serial, vec![0, 2, 4, 6, 8]);
}
```

3. **需要观察的现象**：`v` 的顺序是 `(0,100), (1,101), ...`——与串行 `enumerate` 完全一致；`buf` 的旧值消失；`collect_vec_list` 的结果展平后仍是升序。
4. **预期结果**：三次断言全部通过。并行 `collect` 到 `Vec` 保持迭代器的相对顺序，这是收集协议的承诺，不是巧合。
5. 运行命令为 `cargo run --release`（需 `rayon = "1"` 依赖）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `collect_into_vec` 只定义在 `IndexedParallelIterator` 上，而 `collect` 定义在 `ParallelIterator` 上？

**答案**：`collect_into_vec` 要预先 `reserve` 精确长度的空间并让各任务按固定区间写入，这必须先知道总长度 `len()`，而 `len` 是 `IndexedParallelIterator` 的契约；`collect` 走 `FromParallelIterator`，目标类型可以退而求其次用「分块收集再拼接」的策略（见 4.3），因此不要求长度已知。

**练习 2**：`.collect::<Vec<_>>()` 与 `.collect_into_vec(&mut v)` 都能得到 `Vec`，性能差异主要来自哪里？

**答案**：前者通常新建一个 `Vec`（虽然长度已知时也会精确预分配）；后者复用调用方 `Vec` 已有的缓冲区（`reserve` 在容量足够时不再分配），在循环中反复收集同一规模数据时可省去反复分配与释放。

### 4.2 FromParallelIterator：collect 的目标端契约

#### 4.2.1 概念说明

`FromParallelIterator<T>` 回答的问题是：「**某种集合如何从零创建，并被一个并行迭代器灌满**」。它是标准库 `FromIterator` 的并行镜像。上一讲（u2-l3）我们已经从使用者视角见过它——「FromParallelIterator 让集合作为 collect 目标」。本讲深入它的实现层，看各类集合分别选择了什么策略。

实现策略分三类：

1. **通用路线**：`collect_extended` —— 先 `Default::default()` 建空集合，再 `par_extend` 灌数据（`HashMap`、`BTreeMap`、`LinkedList` 等）。
2. **先收 Vec 再转换**：`VecDeque`、`BinaryHeap`、`Box<[T]>` 等——先收成 `Vec`（享受快路径），再 `into()` 一步转换。
3. **特殊语义**：`String`（拼接字符/字符串）、`()`（丢弃所有元素）、`Result`/`Option`（短路）。

#### 4.2.2 核心流程

以最常用的 `collect::<Vec<_>>()` 和 `collect::<HashMap<_,_>>()` 为例：

```text
Vec<T>::from_par_iter(pi)
    └─> collect_extended(pi)
            └─> Vec::default() + par_extend(pi)     // 见 4.3，内部再按 opt_len 分岔

HashMap<K,V>::from_par_iter(pi)
    └─> collect_extended(pi)
            └─> HashMap::default() + par_extend(pi)
                    └─> fast_collect: 每任务攒 Vec<(K,V)>，按迭代器顺序合并
                    └─> reserve(总长度) 后逐段串行 extend 进 map
```

值得强调的语义保证：文档承诺重复键的行为「与串行迭代器一致」——先产出的值会被后产出的覆盖。这能成立，是因为各分块在合并时保持了迭代器的相对顺序，最后逐段按序 `extend`，等价于串行插入。

#### 4.2.3 源码精读

先看 trait 定义本身：

[src/iter/mod.rs:L3290-L3313](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L3290-L3313) — `FromParallelIterator` 只有一个方法 `from_par_iter`。文档注释（L3294-L3303）给出了为自定义类型实现它的三条建议：收进中间结构再串行 extend、用 `fold` 构造、或天然并行时直接 `for_each`。`FromParallelIterator` 与 `ParallelExtend` 都在 prelude 中（[src/prelude.rs:L5-L14](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/prelude.rs#L5-L14)）。

通用路线的实现辅助函数：

[src/iter/from_par_iter.rs:L14-L22](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/from_par_iter.rs#L14-L22) — `collect_extended`：`C::default()` 建空集合，`par_extend` 灌入，返回。整个文件里超过一半的实现都是这三行的变体。

「先收 Vec 再转换」的代表：

[src/iter/from_par_iter.rs:L77-L101](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/from_par_iter.rs#L77-L101) — `VecDeque` 与 `BinaryHeap` 的实现一模一样：`Vec::from_par_iter(par_iter).into()`。`BinaryHeap` 的文档（L89-L90）特别说明堆序是在全部收齐之后串行建立的——并行阶段只负责把元素装进来。

[src/iter/from_par_iter.rs:L38-L48](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/from_par_iter.rs#L38-L48) — `Box<[T]>` 同样借道 `Vec` 后 `into()`；紧随其后的 `Rc<[T]>`、`Arc<[T]>`（L51-L74）是同一模板。

`HashMap` 的实现及其语义承诺：

[src/iter/from_par_iter.rs:L117-L133](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/from_par_iter.rs#L117-L133) — 文档写明「若多个 pair 对应同一个键，先产出的会被覆盖，与串行迭代器一致」。实现走 `collect_extended`，即默认哈希器建空表后 `par_extend`。

`String` 用宏批量生成实现：

[src/iter/from_par_iter.rs:L179-L208](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/from_par_iter.rs#L179-L208) — `collect_string!` 宏对 `char`、`&str`、`String`、`Cow<str>`、`Box<str>` 六种元素类型各生成一对实现（`String` 目标 + `Box<str>` 目标），全部走 `collect_extended`。所以 `.par_chars().map(...).collect::<String>()` 是合法的。

两个「特殊语义」的实现：

[src/iter/from_par_iter.rs:L273-L280](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/from_par_iter.rs#L273-L280) — `()` 类型的实现用 `NoopConsumer` 把所有元素直接丢弃。文档示例展示了它的用途：当元素是 `Result<(), E>` 时，`collect::<Result<()>>()` 只关心有没有错误。

[src/result.rs:L93-L131](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/result.rs#L93-L131) — `Result` 的短路收集：`map` 把 `Err` 存进 `Mutex` 并转成 `None`，`while_some()` 过滤掉 `None`（提前短路剩余任务），最后根据「是否存过错误」决定返回 `Ok(集合)` 还是 `Err(首个错误)`。注意文档（L88-L92）的说明：多个错误并存时**返回哪一个是不确定的**；`Option` 目标有完全对称的实现（[src/option.rs:L167](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/option.rs#L167)）。

#### 4.2.4 代码实践

1. **实践目标**：实现规格中要求的「把 `(String, i32)` 对收集成 `HashMap` 的函数」，并验证重复键语义。
2. **操作步骤**（示例代码）：

```rust
use rayon::prelude::*;
use std::collections::HashMap;

fn pairs_to_map(pairs: Vec<(String, i32)>) -> HashMap<String, i32> {
    pairs.into_par_iter().collect() // 返回类型由签名决定，无需 turbofish
}

fn main() {
    let pairs = vec![
        ("apple".to_string(), 1),
        ("banana".to_string(), 2),
        ("apple".to_string(), 3), // 重复键
    ];
    let map = pairs_to_map(pairs);
    assert_eq!(map.len(), 2);
    assert_eq!(map["apple"], 3); // 后产出覆盖先产出
    println!("{map:?}");
}
```

3. **需要观察的现象**：`map["apple"]` 的值是 `3` 而不是 `1`。
4. **预期结果**：断言通过。即使并行执行，「apple」最终保留迭代器中靠后的值——与串行 `collect` 行为一致。
5. 若想进一步实验：把目标换成 `BTreeMap`（结果按键升序）或 `Vec<(String, i32)>`（保留重复），只需改函数返回类型。

#### 4.2.5 小练习与答案

**练习 1**：`(0..1_000_000).into_par_iter().map(Result::<i32, String>::Ok).collect::<Result<Vec<i32>, String>>()` 中间会先物化一个完整的 `Vec<i32>` 再包一层 `Ok` 吗？

**答案**：会物化 `Vec<i32>`。`Result<C, E>` 的实现（result.rs L121-L125）先把 `Ok` 值经 `while_some()` 收集到内层集合 `C`，之后才检查有没有存下错误。短路的意义在于「一旦出现 `Err`，后续任务尽快停止」，而不是避免分配。

**练习 2**：为什么 `VecDeque` 不直接实现「自己专用的并行收集」，而是先收成 `Vec` 再 `into()`？

**答案**：`Vec` 的收集有最优路径——长度已知时精确预分配、各段按区间直写、无需逐元素加锁。`Vec` 到 `VecDeque` 的转换是一次 `O(n)` 的整体搬运，但避免了为 `VecDeque` 单独实现一整套消费者协议。这是「复用最优实现 + 一次廉价转换」的典型工程取舍（u2-l3 中集合模块的 `into_par_vec!` 是同一思想的另一处应用）。

### 4.3 ParallelExtend 与 extend 的两条路径

#### 4.3.1 概念说明

`ParallelExtend<T>` 回答另一个问题：「**如何往一个已存在的集合里追加**并行产出的元素」，对应标准库的 `Extend`。它与 `FromParallelIterator` 的分工是：

- `FromParallelIterator`：从零新建 → 适合 `collect`。
- `ParallelExtend`：追加到已有实例 → 既是用户 API（`map.par_extend(...)`），也是大多数 `from_par_iter` 实现的内部基石（`collect_extended` 就是「default + par_extend」）。

两个 trait 是上下游关系：`collect` 的通用实现最终都会落到某个 `par_extend` 上。

#### 4.3.2 核心流程

`par_extend` 的核心是 `fast_collect` 先行、按 `opt_len` 分岔：

```text
par_extend(pi)
  └─> fast_collect(pi)
        ├─ opt_len() = Some(len)：调 special_extend 直接预分配写入
        │               返回 Either::Left(Vec<T>)        —— 一整块
        └─ opt_len() = None：用 ListVecConsumer 驱动
                        返回 Either::Right(LinkedList<Vec<T>>) —— 多块
  └─> 目标集合逐块串行 extend（Left 只有一块，Right 依序拼接）
```

无索引路径下，每个任务（或任务的每一段）在自己的 `Vec` 里局部攒元素，段与段之间**没有任何锁或原子计数**——这是 `collect` 家族高性能的根源。各块的合并顺序由归约器保持，与迭代器相对顺序一致。

代价方面可以粗略估计：设切分为 \( k \) 块、总元素 \( n \) 个，无索引路径的总搬运量为 \( O(n) \)（每块一次 `extend` 搬运），加上 \( O(k) \) 次块间拼接；索引路径则连这 \( O(n) \) 的二次搬运都省掉了——元素直接写进最终位置。

#### 4.3.3 源码精读

trait 定义：

[src/iter/mod.rs:L3343-L3363](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L3343-L3363) — 只有一个方法 `par_extend`，文档示例演示了 `vec.par_extend(0..5)` 与 `vec.par_extend((0..5).into_par_iter().map(...))` 两种用法：参数是任何 `IntoParallelIterator`。

分岔的源头 `fast_collect`：

[src/iter/extend.rs:L67-L82](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/extend.rs#L67-L82) — 先 `opt_len()` 探测：`Some(len)` 走 `special_extend`（伪特化快路径），`None` 走 `ListVecConsumer` 攒 `LinkedList<Vec<T>>`。注释里的「Pseudo-specialization」指的是：Rust 尚无稳定的特化（specialization）机制，这里靠 `opt_len` 在运行期模拟「如果恰好是索引迭代器就用更快的实现」。

无索引路径的消费者：

[src/iter/extend.rs:L84-L145](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/extend.rs#L84-L145) — `ListVecConsumer` 的 `Folder::consume` 只是 `vec.push(item)`（L121-L124），`complete` 把攒好的非空 `Vec` 挂进 `LinkedList`（L134-L140）。每个并行分支各自持有一个独立 `Vec`，互不竞争。

两个宏把「收块 → 逐块 extend」固化成模板：

[src/iter/extend.rs:L15-L39](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/extend.rs#L15-L39) — `extend!` 处理 `Either::Left/Right` 两种结果：单块直接 `extend`，多块循环逐块 `extend`；`extend_reserved!` 先按总长度 `reserve` 再 extend，避免目标集合反复扩容。选择哪个宏是各集合实现自己做的性能决策。

`Vec` 的实现——两条路径都暴露无遗：

[src/iter/extend.rs:L569-L596](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/extend.rs#L569-L596) — `Some(len)` 时调 `special_extend` 直接写入自身；`None` 时收 `LinkedList<Vec<T>>`，先 `reserve` 总长度，再逐块 `append`（比逐元素 `extend` 更高效）。注释指向 rayon-demo 的 `vec_collect` 基准，说明这是经过实测权衡的策略。

`HashMap` 与 `String` 的策略差异：

[src/iter/extend.rs:L228-L241](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/extend.rs#L228-L241) — `HashMap` 用 `extend_reserved!`（先 `reserve` 再逐块插入），注释指向 `map_collect` 基准。

[src/iter/extend.rs:L414-L425](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/extend.rs#L414-L425) — `String: ParallelExtend<char>` 不走 `Vec<char>` 中转（注释解释：`Vec<char>` 每元素 4 字节，不如直接攒 `String` 紧凑），改为 `ListStringConsumer` 攒 `LinkedList<String>`，最后按总字节数 `reserve` 再拼接。

`special_extend` 的「伪特化」契约：

[src/iter/collect/mod.rs:L23-L40](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/collect/mod.rs#L23-L40) — 函数签名接受任意 `ParallelIterator`，但注释说明：调用方已用 `opt_len` 确认过长度，只有索引迭代器才会到这里；类型系统表达不了这个约定，所以 `CollectConsumer` 被迫同时实现 `UnindexedConsumer`（其实现里是 `unreachable!`）。这是 u2-l1 讲过的「两条驱动路径」在收集侧的具体交汇点。

#### 4.3.4 代码实践

1. **实践目标**：直接使用 `par_extend` API，并观察它与 `collect` 的关系。
2. **操作步骤**（示例代码）：

```rust
use rayon::prelude::*;

fn main() {
    let mut v: Vec<i32> = Vec::new();
    v.par_extend(0..5);                          // 直接以范围为参数
    v.par_extend((0..5).into_par_iter().map(|i| i * i));
    assert_eq!(v, [0, 1, 2, 3, 4, 0, 1, 4, 9, 16]);
    println!("{v:?}");
}
```

3. **需要观察的现象**：两次 `par_extend` 的结果按调用顺序前后相接，第二次不会清空第一次的数据。
4. **预期结果**：断言通过。这正是 [src/iter/mod.rs:L3353-L3359](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L3353-L3359) 文档示例的断言值。
5. **待本地验证**：如果想观察两条路径的差别，可在 4.3.3 提到的 rayon-demo 基准（`vec_collect`、`map_collect`）上对比不同策略的耗时——这两个基准仅在 nightly 下可用（见 u1-l2），本讲不作要求。

#### 4.3.5 小练习与答案

**练习 1**：`filter` 之后的迭代器 `opt_len()` 返回什么？这会如何影响它 `collect::<Vec<_>>()` 的路径？

**答案**：返回 `None`——`filter` 无法预知保留多少元素（承接 u2-l1「filter 丢失索引能力」）。因此 `par_extend` 走 `ListVecConsumer` 多块路径：每个分支局部攒 `Vec`，最后按序拼接。功能正确，但比索引路径多一次块间搬运。

**练习 2**：`String: ParallelExtend<char>` 为什么不先收成 `Vec<char>` 再转换？

**答案**：`Vec<char>` 中每个 `char` 占 4 字节，而 UTF-8 编码的 `String` 中 ASCII 只占 1 字节；先攒 `Vec<char>` 会多占用内存且转换仍需一次全量扫描。直接攒 `LinkedList<String>` 让每个分支就地完成 UTF-8 编码，最后按精确总字节数 `reserve` 一次拼齐。

### 4.4 unzip 与 partition：一次遍历、两个结果

#### 4.4.1 概念说明

有时一次并行遍历想同时产出两个集合：

- `unzip`：元素是 `(A, B)` 元组，左边收进一个集合、右边收进另一个。
- `partition`：按谓词把元素分流到两个集合（两边元素类型相同）。
- `partition_map`：谓词同时做映射，左右可以是不同类型（用 `Either` 标记去向）。

朴素的替代方案是遍历两遍（一遍 `filter` 收左边、一遍收右边），或者加锁共享两个 `Vec`。前者浪费一倍计算，后者引入竞争。Rayon 的做法是**一次遍历、双份消费者**：每个元素被处理时同时喂给左右两个 `Folder`，两个结果集合各自独立增长，最后各自归并。

#### 4.4.2 核心流程

实现上有一个「先有鸡还是先有蛋」的问题：要并行收集就需要消费者，但左右两个集合的消费者分别由它们各自的 `par_extend` 内部构造——怎么同时拿到两个？Rayon 的解法是两层「假迭代器」拦截：

```text
unzip(pi)
  └─> execute_into(a, b, pi, Unzip)
        └─> 构造 UnzipA{base: pi, op, b}        // 伪装成产出左元素的迭代器
              a.par_extend(UnzipA)               // 骗出 a 的消费者 CA
              └─> UnzipA::drive_unindexed(CA)
                    构造 UnzipB{base, op, left_consumer: CA}  // 伪装成产出右元素
                    b.par_extend(UnzipB)         // 骗出 b 的消费者 CB
                    └─> UnzipB::drive_unindexed(CB)
                          合成 UnzipConsumer{left: CA, right: CB}
                          pi.drive_unindexed(合成消费者)        // 真正的一次遍历
```

`UnzipOp` trait 把三种语义统一为「一个元素如何喂给左右两个 Folder」：`Unzip` 拆元组、`Partition` 按谓词二选一、`PartitionMap` 按 `Either` 二选一。

#### 4.4.3 源码精读

三个用户 API 都是一行转发（定义在 `ParallelIterator` 上）：

[src/iter/mod.rs:L2104-L2113](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L2104-L2113) — `unzip`，目标只需 `Default + ParallelExtend`，文档示例展示了嵌套元组也能一层层拆开。

[src/iter/mod.rs:L2134-L2141](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L2134-L2141) — `partition`，注意文档提示：与标准库不同，左右可以是**不同**的集合类型。

[src/iter/mod.rs:L2186-L2195](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L2186-L2195) — `partition_map`，闭包返回 `Either<L, R>`，文档里的 fizzbuzz 示例展示了嵌套 `Either` 拆成四个集合的玩法。

统一抽象 `UnzipOp`：

[src/iter/unzip.rs:L6-L25](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/unzip.rs#L6-L25) — 关联类型 `Left`/`Right` 是两个消费者各自吃的元素类型；`consume` 接收一个元素和左右两个 `Folder`，返回喂过之后的两个 `Folder`；`indexable()` 报告该操作是否保持数量（`unzip` 是，`partition` 否——分流前不知道各自数量）。

三种具体操作：

[src/iter/unzip.rs:L93-L108](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/unzip.rs#L93-L108) — `Unzip`：把 `(a, b)` 拆开分别喂给左右，`indexable() = true`。

[src/iter/unzip.rs:L129-L148](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/unzip.rs#L129-L148) — `Partition`：按谓词只喂其中一边，另一边原样传回。

[src/iter/unzip.rs:L171-L190](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/unzip.rs#L171-L190) — `PartitionMap`：按 `Either` 变体路由，且左右类型可以不同。

两层拦截的关键代码：

[src/iter/unzip.rs:L193-L235](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/unzip.rs#L193-L235) — `UnzipA` 是个「假迭代器」：它实现了 `ParallelIterator`，但 `drive_unindexed` 并不产出元素，而是把 `a.par_extend` 传进来的消费者 `CA` 暂存，转手构造 `UnzipB` 去驱动 `b.par_extend`（L207-L226）。注意 L225 的 `expect("unzip consumers didn't execute!")`——如果某个 `par_extend` 实现根本不驱动迭代器，这里会 panic。

[src/iter/unzip.rs:L250-L281](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/unzip.rs#L250-L281) — `UnzipB::drive_unindexed` 终于集齐左右两个消费者，合成 `UnzipConsumer` 后驱动**原始**迭代器——真正的单次遍历在这里发生。

合成消费者与「双方都满才算满」：

[src/iter/unzip.rs:L284-L334](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/unzip.rs#L284-L334) — `UnzipConsumer` 的 `split_at` 同时切分左右两个消费者（L300-L320）；`full()` 返回 `left.full() && right.full()`（L330-L333）——短路信号只有在**两边都**说满了才向上传播，否则另一边会丢元素。

元组本身也是收集目标：

[src/iter/unzip.rs:L411-L424](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/unzip.rs#L411-L424) — `(FromA, FromB): ParallelExtend<(A, B)>`，即 4.1 里「`.collect()` 直接收成元组」的幕后实现：它就是 `execute_into`。

[src/iter/unzip.rs:L464-L478](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/unzip.rs#L464-L478) — `(FromA, FromB): FromParallelIterator<(A, B)>` 通过一个一次性垫片 `Collector`（[L501-L523](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/unzip.rs#L501-L523)）借用 `unzip` 实现，`Either` 版本的 `partition_map` 同理（L480-L498）。

索引版本：已知长度时连左右两个 `Vec` 都能精确预分配：

[src/iter/collect/mod.rs:L45-L65](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/collect/mod.rs#L45-L65) — `unzip_into_vecs` 对左右两个向量各做一次 `collect_with_consumer` 预分配，然后经 `unzip_indexed` 用合成消费者一次写满两者。用户入口在 [src/iter/mod.rs:L2557-L2564](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L2557-L2564)。

#### 4.4.4 代码实践

1. **实践目标**：完成规格中的第三个任务——用 `unzip` 一次得到两个集合，再叠加 `partition_map`。
2. **操作步骤**（示例代码）：

```rust
use rayon::iter::Either;
use rayon::prelude::*;

fn main() {
    // 任务：enumerate + unzip，一次遍历得到下标集合与值集合
    let words = ["alpha", "beta", "gamma", "delta"];
    let (indexes, lengths): (Vec<usize>, Vec<usize>) =
        words.par_iter().enumerate().map(|(i, w)| (i, w.len())).unzip();
    assert_eq!(indexes, [0, 1, 2, 3]);
    assert_eq!(lengths, [5, 4, 5, 5]);

    // 进阶：partition_map 按长度分流，且左右类型不同
    let (short_names, long_lens): (Vec<&str>, Vec<usize>) = words
        .par_iter()
        .partition_map(|&w| {
            if w.len() < 5 { Either::Left(w) } else { Either::Right(w.len()) }
        });
    assert_eq!(short_names, ["beta"]);
    assert_eq!(long_lens, [5, 5, 5]);
    println!("{indexes:?} {lengths:?} {short_names:?} {long_lens:?}");
}
```

3. **需要观察的现象**：`indexes` 与 `lengths` 的顺序和串行遍历一致；`partition_map` 左右两侧元素类型不同（`&str` vs `usize`）也能编译通过。
4. **预期结果**：三条断言全部通过。
5. 如果把 `unzip` 换成 `.collect()`（目标类型标注为 `(Vec<_>, Vec<_>)`），结果应完全相同——可自行验证 4.4.3 讲的「元组也是 FromParallelIterator 目标」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `UnzipConsumer::full` 要写 `left.full() && right.full()` 而不是 `||`？

**答案**：`full()` 是短路协议的信号（u2-l5 的 `try_fold`、`while_some` 会用到）。若用 `||`，左边一满整条管道就会提前停止，右边会**丢元素**；用 `&&` 保证只要还有一边没满就继续产出，元素会被喂给不满的那一侧（`Partition::consume` 里被跳过的一侧只是原样传回 Folder，不影响正确性）。

**练习 2**：`UnzipA` / `UnzipB` 为什么要实现 `ParallelIterator`？它们的元素真的被生产出来了吗？

**答案**：因为 `par_extend` 的参数约束是 `IntoParallelIterator`，只有实现（伪装修）成并行迭代器才能「骗」目标集合启动 `par_extend`，从而在 `drive_unindexed` 回调里截获集合内部构造的消费者。元素从未以 `OP::Left` / `OP::Right` 的形式真正产出——真正被驱动的是包在最里面的原始迭代器 `self.base`。

**练习 3**：`unzip` 与 `unzip_into_vecs` 有何区别，何时选后者？

**答案**：`unzip` 目标可以是任意 `Default + ParallelExtend` 集合，走通用收集；`unzip_into_vecs` 只面向 `IndexedParallelIterator` 与两个 `Vec`，利用 `len()` 对两个向量都做精确预分配，且允许复用已有缓冲区。在循环中反复拆分同规模数据、且左右都收 `Vec` 时，`unzip_into_vecs` 更快。

## 5. 综合实践

**任务：一个小型「成绩分析器」，把本讲四个模块串起来。**

新建独立 Cargo 项目（`cargo new collect-lab`，`Cargo.toml` 加 `rayon = "1"`），实现如下程序（示例代码）：

```rust
use rayon::iter::Either;
use rayon::prelude::*;
use std::collections::HashMap;

/// 把 (姓名, 分数) 对收集成 HashMap；同名后出现者覆盖先出现者（4.2）
fn to_score_map(rows: Vec<(String, i64)>) -> HashMap<String, i64> {
    rows.into_par_iter().collect()
}

fn main() {
    let rows: Vec<(String, i64)> = [
        ("alice", 82), ("bob", 55), ("carol", 91),
        ("dave", 47), ("alice", 90), // alice 出现两次
    ]
    .iter()
    .map(|&(n, s)| (n.to_string(), s))
    .collect();

    // 1. collect 家族：enumerate + collect 成 Vec（4.1）
    let ranked: Vec<(usize, &(String, i64))> =
        rows.par_iter().enumerate().collect();
    println!("带序号: {ranked:?}");

    // 2. FromParallelIterator：收进 HashMap，重复键后值生效（4.2）
    let scores = to_score_map(rows.clone());
    assert_eq!(scores["alice"], 90);

    // 3. unzip + partition：一次遍历双产出（4.4）
    let (names, marks): (Vec<String>, Vec<i64>) =
        rows.par_iter().cloned().unzip();
    let (passed, failed): (Vec<String>, Vec<String>) =
        rows.par_iter().partition(|(_, s)| *s >= 60);
    println!("通过: {passed:?}, 未过: {failed:?}");

    // 4. collect_into_vec 复用缓冲区（4.1），partition_map 左右异构（4.4）
    let mut buf = Vec::with_capacity(rows.len());
    rows.par_iter().map(|(_, s)| s * 2).collect_into_vec(&mut buf);
    let (high, low): (Vec<String>, Vec<i64>) = rows
        .par_iter()
        .partition_map(|(n, s)| {
            if *s >= 85 { Either::Left(n.clone()) } else { Either::Right(*s) }
        });
    println!("高分名单: {high:?}, 低分原值: {low:?}");
    println!("双倍分: {buf:?}");
    println!("unzip 校验: {} {} ", names.len(), marks.len());
}
```

验收标准：

1. 程序编译通过，所有断言通过（`cargo run --release`）。
2. 能回答：`scores["alice"]` 为什么是 90 而不是 82？（提示：4.2.2 的顺序保证）
3. 能回答：`partition` 与 `partition_map` 在这个例子里分别丢失/保留了什么信息？

**源码阅读附加题**：阅读 [tests/collect.rs:L8-L62](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/tests/collect.rs#L8-L62) 的 `collect_drop_on_unwind` 测试：它在 `map` 中途人为 panic，然后断言「插入的元素数 == drop 的元素数」。结合 4.1.3 讲的 `collect_with_consumer` 写满校验（[src/iter/collect/mod.rs:L86-L103](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/collect/mod.rs#L86-L103) 注释中关于 unwind 时 `CollectResult` 被 drop 的说明），解释为什么 panic 不会造成「已产出元素既不在结果里、也没被 drop」的泄漏。运行方式：`cargo test -p rayon --test collect`。

## 6. 本讲小结

- `collect` 本体只有一行（`C::from_par_iter(self)`），所有行为由目标类型的 `FromParallelIterator` 实现决定；`collect_into_vec`（复用缓冲区）与 `collect_vec_list`（并行转串行桥梁）是两个重要的补充出口。
- `FromParallelIterator` 负责「从零新建」，`ParallelExtend` 负责「往已有集合追加」；前者的大多数实现是 `default + par_extend`（`collect_extended`），后者是前者的内部基石。
- `par_extend` 以 `fast_collect` 的 `opt_len()` 分岔为界：有长度走 `special_extend` 精确预分配直写，无长度走 `ListVecConsumer` 分块收集再按序拼接——两条路径的结果顺序都与串行一致，重复键语义也与串行一致。
- `Result` / `Option` 目标的 `collect` 具备短路能力（`while_some` + `Mutex` 存错误），但多个错误并存时返回哪个不确定；`()` 目标则直接丢弃所有元素。
- `unzip` / `partition` / `partition_map` 通过 `UnzipOp` 抽象与 `UnzipA`/`UnzipB` 两层「假迭代器」拦截，做到一次遍历同时填满两个集合，全程无锁。
- 收集的正确性有硬校验兜底：`collect_with_consumer` 断言实际写入数等于预期长度，未写满即 panic，绝不暴露未初始化内存；panic 安全性由集成测试 `collect_drop_on_unwind` 守护。

## 7. 下一步学习建议

- 下一讲（u2-l5）转向并行世界的错误处理与 panic 语义：`try_fold` / `try_reduce` / `panic_fuse`，本讲 4.2 提到的 `while_some` 短路机制会在那里展开。
- 若想彻底搞懂 `CollectConsumer` 如何「按区间写入、写满校验」，请在进入单元四后精读 u4-l4（collect 的内部实现），配合 [src/iter/collect/consumer.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/collect/consumer.rs)。
- 建议顺带浏览 [src/iter/collect/test.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/collect/test.rs)，里面有大量「收集结果与串行对照」的单元测试，是验证本讲结论的最直接证据。
- 性能取向的读者可以阅读 extend.rs 注释中指向的 rayon-demo `vec_collect` / `map_collect` 基准（需 nightly，见 u1-l2），实测两条收集路径的差异。
