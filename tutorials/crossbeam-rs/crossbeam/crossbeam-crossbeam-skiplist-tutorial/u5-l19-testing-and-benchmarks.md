# 测试体系与基准实践

## 1. 本讲目标

本讲是专家层的收尾篇，不再引入新的无锁算法，而是回答两个工程问题：**「这套并发跳表的正确性靠什么测试来保证」**与**「什么时候该选 `SkipMap` 而不是 `BTreeMap`/`HashMap`」**。

学完后你应该能够：

- 说清 `tests/{base,map,set}.rs` 三套测试文件分别覆盖哪一层、各自有哪些测试模式（串行、并发、内存泄漏、回收断言）。
- 独立写出一个并发抢占式更新测试，并解释「为什么最终值可断言」。
- 读懂 `tests/base.rs` 中 `Entry` 包装 + `release_with_pin` 这一测试惯用法背后的引用计数原理。
- 用 `cargo +nightly miri test` 验证 `base.rs` 中 `unsafe` 的内存安全性，并理解源码里 `cfg!(miri)` 缩减规模的动机。
- 看懂 `benches/` 四组基准（`insert`/`iter`/`lookup`/`insert_remove`）的同构对比设计，并据此做选型判断。

---

## 2. 前置知识

本讲假设你已经读过以下前置讲义（关键结论会直接承接，不再重复推导）：

- **u1-l4 构建、测试与基准对比**：`cargo test` 用 stable 即可，`cargo bench` 必须 nightly（首行 `#![feature(test)]`）；基准用确定性 PRNG `num.wrapping_mul(17).wrapping_add(255)` 生成 key 以保证公平对比；dev-dependency 只有 `fastrand`。
- **u2-l6 epoch 内存回收与引用计数**：被删节点不会立即释放，靠引用计数 + epoch 延迟回收；只要还有 `Entry`/`RefEntry` 句柄持有引用，节点就不会被 `finalize`。
- **u3-l9 标记指针与逻辑删除**与 **u3-l10 插入路径**：`remove`/`insert` 的线性化点、`mark_tower` 的删除权竞争，以及 `compare_insert` 用闭包决定是否替换旧值。
- **u4-l12 Entry 与 RefEntry**（讲义文件虽缺，但结论在 u4-l14 中已用）：`base::RefEntry` 跨 `Guard` 存活靠引用计数，`release_with_pin` 是「按需 pin」的释放方式。

几个本讲会用到的术语，先统一口径：

| 术语 | 含义 |
| --- | --- |
| 串行测试（serial test） | 单线程、确定性输入，验证功能正确性与边界 |
| 并发测试（concurrent test） | 多线程同时对同一结构操作，验证无锁算法活性与一致性 |
| 内存泄漏测试（memory_leak test） | 验证句柄被丢弃后节点能被正确回收，不泄漏引用计数 |
| Miri | Rust 官方的 UB 检测器，解释执行机器码，能抓 use-after-free、数据竞争等 |
| 同构基准（isomorphic bench） | 用相同输入序列、相同操作数量测不同容器，控制变量只留「容器实现」 |

---

## 3. 本讲源码地图

本讲只读测试与基准目录，**不触碰 `src/` 的实现**（仅引用两处签名做对照）。

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `tests/map.rs` | 高层 `SkipMap` 的集成测试 | `compare_and_insert` 替换语义、`concurrent_insert`/`concurrent_compare_and_insert` 并发抢占、`*_memory_leak` 三件套、`concurrent_insert_get_same_key` 的 `cfg!(miri)` 缩减 |
| `tests/base.rs` | 底层 `base::SkipList` 的测试 | 自定义 `Entry` 包装 + `release_with_pin` 惯用法、`drops` 回收断言、`remove_race` 大规模并发删除 |
| `tests/set.rs` | 高层 `SkipSet` 的集成测试 | 验证 `SkipMap<T,()>` 特化的行为对称性（本讲略读） |
| `benches/skiplist.rs` | `base::SkipList` 基准（需显式传 `Guard`） | 四组基准 `insert`/`iter`/`rev_iter`/`lookup`/`insert_remove` |
| `benches/skipmap.rs` | 高层 `SkipMap` 基准 | 同四组基准，但 `Guard` 藏在方法内 |
| `benches/btree.rs` | `BTreeMap` 对照基准 | 同构输入，单线程有序 map 基线 |
| `benches/hash.rs` | `HashMap` 对照基准 | 同构输入，无序 map 基线（注意：无 `rev_iter`） |

永久链接基准前缀统一为：
`https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/`

---

## 4. 核心概念与源码讲解

### 4.1 测试组织总览与 cfg!(miri) 规模缩减

#### 4.1.1 概念说明

`crossbeam-skiplist` 把测试拆成三份文件，恰好对应三层层级：

- `tests/base.rs` 测底层 `base::SkipList`：所有方法都要显式传 `&Guard`，最贴近无锁原语，是验证 `unsafe` 正确性的主战场。
- `tests/map.rs` 测高层 `SkipMap`：用户视角的 API，`Guard` 被藏进方法内部，测试关注「易用性 + 高层语义」。
- `tests/set.rs` 测 `SkipSet`：本质是 `SkipMap<T,()>`，测试关注「set 语义是否与 map 对称」。

这三份文件共用一套**测试模式分类**，本讲后续四节就按这套分类逐个精读：

1. **串行功能测试**：确定性输入 + 断言，如 `insert`/`get`/`lower_bound`/`upper_bound`/`iter_range`。
2. **并发竞争测试**：多线程对同一 key 操作，如 `concurrent_insert`/`concurrent_compare_and_insert`/`remove_race`。
3. **内存安全测试**：验证句柄生命周期与节点回收，如 `*_memory_leak`/`drops`。
4. **惯用法封装测试**：`tests/base.rs` 因为不能长期持有 `Guard`，自定义了一个 `Entry` 包装结构。

#### 4.1.2 核心流程：cfg!(miri) 如何让同一份测试跑在两种环境

无锁算法的并发测试通常要跑**大量迭代**才能覆盖足够多的交错（interleaving）。但在 Miri 下，解释执行极慢，跑 10 万次并发删除会等几小时。库的解法是：在测试源码里用 `cfg!(miri)` 把规模分两档。

以 `concurrent_insert_get_same_key` 为例：

```rust
let len = if cfg!(miri) { 100 } else { 10_000 };
```

- 普通环境：`len = 10_000`，覆盖足够多的线程交错。
- Miri 环境：`len = 100`，规模缩小 100 倍，让 Miri 在可接受时间内跑完，同时仍能抓到内存不安全（UB 与规模无关，一次就越界也能抓）。

`remove_race` 用了同样的手法（见 4.2.3）。这是并发库测试的通用范式：**「正确性用 Miri 小规模验证，统计活性用普通环境大规模验证」**。

#### 4.1.3 源码精读

`concurrent_insert_get_same_key` 展示了典型的「写线程 + 读线程」并发对同一 key 操作，并配 `cfg!(miri)` 缩减：

读线程持续 `get(&key)` 断言 `is_some()`，写线程持续 `insert(0, ())` 替换。它验证了替换语义下读线程绝不会看到「不存在」的中间态（单操作原子性）：

[tests/map.rs#L929-L947](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/tests/map.rs#L929-L947) — 并发「同 key 反复 insert + get」，`len` 在 Miri 下缩为 100，断言读线程每次都能拿到值。

#### 4.1.4 代码实践（源码阅读型）

1. 实践目标：理解 `cfg!(miri)` 分档的动机。
2. 操作步骤：
   - 用 `grep` 在 `tests/` 下找所有 `cfg!(miri)` 出现点。
   - 对比每处的「Miri 值」与「普通值」之比。
3. 需要观察的现象：至少在 `concurrent_insert_get_same_key` 和 `remove_race` 两处看到分档。
4. 预期结果：两处都在 Miri 下把规模缩小到约 1/1000（`10_000→100`、`100_000→100`）。
5. 若无法本地运行 Miri，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Miri 下把迭代数从 10 万降到 100，仍能抓到 use-after-free？

> **答案**：UB 是「单次非法内存访问」就足以触发，与执行次数无关。Miri 解释执行每条访存指令并校验其合法性，一次越界或访问已释放内存就会立即报错。大规模迭代是为了覆盖「线程交错的多样性」与「统计意义上的活性」，而非性质成立的前提。

**练习 2**：`cfg!(miri)` 与 `#[cfg(test)]` 有何区别？

> **答案**：`#[cfg(test)]` 是编译期条件编译，仅在 `cargo test` 时编入；`cfg!(miri)` 是运行期宏（编译期求值为 `bool` 常量），在 Miri 工具链下编译时为 `true`、否则为 `false`，用于在「同一份二进制」里选不同分支。

---

### 4.2 并发测试三连：concurrent_insert / concurrent_compare_and_insert / remove_race

#### 4.2.1 概念说明

无锁算法最易出错的不是单线程逻辑，而是**多线程交错**下产生的竞争。本节三个测试分别覆盖三种典型竞争场景：

- `concurrent_insert`：两线程同时对**同一已存在 key** 插入（替换语义）。
- `concurrent_compare_and_insert`：N 线程用 `compare_insert` **抢占式更新**同一 key。
- `remove_race`：16 线程同时对 **10 万个 key** 删除，验证「每个 key 恰被删一次」。

它们都对应历史上的 GitHub issue（注释里有链接），是真实回归 bug 的回归测试。

#### 4.2.2 核心流程：compare_insert 的替换语义

先回顾 `compare_insert` 的语义（这是理解并发抢占测试的前提）。`SkipMap::compare_insert(key, value, compare_fn)` 的闭包 `compare_fn: Fn(&V) -> bool` 接收**旧值**，返回「是否要用新值替换」：

[src/map.rs#L430-L436](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L430-L436) — `compare_insert` 实现，闭包 `compare_fn` 决定是否替换。

串行测试 `compare_and_insert` 用两轮验证语义：

- 第一轮 `compare_insert(x, x*5, |x| x < &value)`：旧值是 `x*10`，`x*10 < x*5` 为假 → 不替换 → 断言值仍为 `old_value`。
- 第二轮 `compare_insert(x, x*15, |x| x < &value)`：旧值是 `x*10`，`x*10 < x*15` 为真 → 替换 → 断言值为 `x*15`。

[tests/map.rs#L56-L82](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/tests/map.rs#L56-L82) — `compare_and_insert` 两轮断言「不替换」与「替换」两种结果。

并发版 `concurrent_compare_and_insert` 的精妙之处在于：100 个线程各带 `id = i`，调用 `compare_insert(1, i, |j| j < &i)`——「只要当前值 `< i` 就替换成 `i`」。这等价于一个**单调上升的 ratchet（棘轮）**：值只会被越来越大的 `i` 覆盖，最终必然停在最大值 `len - 1 = 99`。

为什么能断言？设当前值为 `v`，线程 `i` 抢到替换权当且仅当在它执行闭包的那一刻 `v < i`。一旦某线程 `i` 成功写入，此后任何 `j ≤ i` 的闭包 `j < &i` 恒为假，再也无法覆盖。故最终幸存值必为所有「曾成功写入」的 `i` 中的最大者，即 `len - 1`。

\[ \text{final} = \max\{\, i \mid \text{线程 } i \text{ 的 compare_insert 成功替换}\,\} = \text{len}-1 \]

[tests/map.rs#L153-L170](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/tests/map.rs#L153-L170) — 100 线程 `compare_insert` 抢占更新 key=1，最终断言值为 `len - 1 = 99`。

#### 4.2.3 核心流程：remove_race 的两阶段屏障

`remove_race` 测的是大规模并发删除的「恰好一次」性质。它用两个原子计数 `barrier1`/`barrier2` 当**自旋屏障**，把所有线程的删除阶段对齐，使竞争最激烈：

1. 每线程先 `barrier1.fetch_sub(1)` 再自旋等其归零 → 全部线程同时开始删除。
2. 各线程遍历 `0..KEY_RANGE` 调 `s.remove(&x, guard)`，把拿到的 `entry` **暂存** `removed_entries`（不立即 release）。
3. 删完后再 `barrier2.fetch_sub(1)` 并自旋 → 全部线程同时结束。
4. 屏障过后才 `entry.release(guard)` 退计数。
5. 主线程断言 `total_removed == KEY_RANGE`（10 万个 key 恰被删 10 万次）。

**为什么暂存 entry、屏障后才 release？** 这是为了在删除阶段让大量节点同时处于「逻辑已删但引用计数 > 0」的状态，最大化 epoch 回收链路的压力，从而暴露 double-free 或回收过早的 bug。

[tests/base.rs#L976-L1023](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/tests/base.rs#L976-L1023) — 16 线程删 10 万 key（Miri 下缩为 100），两阶段自旋屏障对齐，断言总删除数等于 key 总数。

`concurrent_insert` 与 `concurrent_remove` 则更简单：用 `Barrier::new(2)` 让两线程同时操作同一 key，并外层套 `for _ in 0..100` 增加交错次数。

[tests/map.rs#L133-L151](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/tests/map.rs#L133-L151) — 两线程经 `Barrier` 同步后对同一 key `insert`，循环 100 轮放大竞争（对应 issue #672）。

#### 4.2.4 代码实践（编写型）

1. 实践目标：亲手写一个并发抢占式更新测试，并断言最终值。
2. 操作步骤：
   - 在 `tests/map.rs` 末尾仿照 `concurrent_compare_and_insert` 新增 `concurrent_compare_insert_ratchet`。
   - 用 8 个线程，各带 `id = i`（`i` 范围 `0..8`），对同一 key 调 `compare_insert(42, i, |j| j < &i)`。
   - 全部 `join` 后断言 `*s.get(&42).unwrap().value() == 7`。
3. 需要观察的现象：多次运行结果稳定为 7（最大 id）。
4. 预期结果：测试通过；若改成 `|j| j > &i`（下降 ratchet），最终值应变为 `0`（最小 id）。
5. 若多线程不稳定，标注「待本地验证」并加大循环轮数。

#### 4.2.5 小练习与答案

**练习 1**：把 `concurrent_compare_and_insert` 的闭包改成 `|j| j <= &i`，最终值还是 `len - 1` 吗？会不会有 bug？

> **答案**：最终值仍会是 `len - 1`，但语义变弱：当当前值 `== i` 时也会「替换成相同的值」（空替换）。这不会破坏正确性，但会产生不必要的 `mark_tower` + 重插，增加无谓开销。这也说明闭包的写法直接影响性能。

**练习 2**：`remove_race` 里如果删完立即 `entry.release(guard)`（不暂存到屏障后），测试还能通过吗？为什么仍要暂存？

> **答案**：功能上测试大概率仍通过（最终删除数仍等于 key 总数）。暂存的目的是**给 epoch 回收链路加压**——让大量节点在删除阶段同时持有引用，迫使回收推迟，从而更可能暴露「回收过早 / double-free」类 bug。这是「为放大缺陷而设计」的测试技巧。

---

### 4.3 内存安全测试：memory_leak 三件套与 drops 回收断言

#### 4.3.1 概念说明

引用计数 + epoch 回收的双重机制（u2-l6）很容易出两类 bug：**引用泄漏**（计数永远不归零，节点永不回收）和**提前回收**（节点还在被引用就被释放）。本节测试专门针对这两类。

`tests/map.rs` 的三个 `*_memory_leak` 测试针对**迭代器句柄**的生命周期；`tests/base.rs` 的 `drops` 测试则用带 `Drop` 副作用的类型**计数**回收事件，直接断言回收时机。

#### 4.3.2 核心流程：迭代器句柄的泄漏陷阱

`next_memory_leak` 的场景极小但致命：

1. map 里只有一个 key=1。
2. 拿到迭代器 `iter`，先 `iter.next_back()` 取走唯一元素 → 持有它的 `Entry` 句柄。
3. `iter.next()` 应返回 `None`。
4. `map.remove(&1)` —— 此时 key=1 在逻辑上已被迭代器句柄「占用」。

如果迭代器内部对节点的引用计数管理有误（比如 `next_back` 拿到句柄后没正确退掉迭代器持有的那份引用），`remove` 后节点就永远无法回收 → 内存泄漏。测试本身不直接断言「已回收」，而是**确保这种用法不 panic、不泄漏计数**（结合 Miri 或 `drops` 风格的计数才能看到回收）。

[tests/map.rs#L192-L201](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/tests/map.rs#L192-L201) — `next_memory_leak`：单元素 map 上 `next_back` 后再 `next`，验证迭代器双向游标的引用计数不泄漏。

三件套分别覆盖 `next`/`next_back`/`range` 三种迭代器的双向取值路径：

- [tests/map.rs#L203-L213](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/tests/map.rs#L203-L213) — `next_back_memory_leak`：先 `next` 再 `next_back`。
- [tests/map.rs#L215-L224](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/tests/map.rs#L215-L224) — `range_next_memory_leak`：`range(0..)` 上的双向取值。

#### 4.3.3 核心流程：drops 用 Drop 副作用断言回收时机

`drops` 测试是本套测试中最精巧的一个。它定义了带 `Drop` 的 `Key` 和 `Value`，在 `drop` 时给全局 `AtomicUsize` 计数 `+1`，从而把「析构发生」变成可观测事件：

```rust
impl Drop for Key  { fn drop(&mut self) { KEYS.fetch_add(1, Ordering::SeqCst); } }
impl Drop for Value { fn drop(&mut self) { VALUES.fetch_add(1, Ordering::SeqCst); } }
```

然后它用一个**自建的 `Collector`**（而非默认全局 collector）来精确控制 epoch 推进：

1. 插入 7 个节点 → 断言 `KEYS==0, VALUES==0`（都还活着）。
2. `remove` key=7 并 release → 断言计数仍为 0（节点被摘除但因 epoch 未推进，尚未析构）。
3. `drop(s)` 释放整个跳表 → 计数仍为 0（同理）。
4. 手动 `handle.pin().flush()` 两次推进 epoch → 此刻才断言 `KEYS==8, VALUES==7`。

为什么是 8 和 7？插入了 7 个 `Key(x)`，外加 `remove` 时构造的查询键 `Key(7)`（局部变量，函数内析构），共 8 个 Key 析构；Value 插入 7 个，共 7 个。这个精确数字验证了**「每个键值对都恰好被析构一次」**——既没有泄漏（计数不为 0），也没有 double-free（计数没翻倍）。

[tests/base.rs#L858-L904](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/tests/base.rs#L858-L904) — `drops` 测试：用自建 `Collector` + `Drop` 计数断言「插入 7、查询 1、drop 后两次 flush → KEYS=8, VALUES=7」。

> **关键技巧**：用自建 `Collector` 而非 `epoch::default_collector()`，是因为默认 collector 由其他线程共享，`flush` 时机不可控；自建 collector 让测试**独占** epoch 推进权，才能在断言点确定地触发回收。

#### 4.3.4 代码实践（源码阅读 + 实验型）

1. 实践目标：亲眼看到 epoch 回收的「延迟」与「触发」。
2. 操作步骤：
   - 读懂 `drops` 测试，把「插入 7 个、remove 1 个、drop 整表」后但未 `flush` 时的预期计数写下来。
   - 复制该测试到自己的文件，把 `handle.pin().flush()` 的次数从 2 改为 1，观察断言是否失败。
3. 需要观察的现象：`flush` 一次可能不够（epoch 需要两轮推进才能让所有垃圾进入可回收区间）。
4. 预期结果：改为 1 次 `flush` 后 `KEYS`/`VALUES` 计数可能仍为 0 或部分值，断言失败；恢复 2 次则通过。
5. 若本地无法稳定复现，标注「待本地验证」（epoch 推进细节依赖内部实现）。

#### 4.3.5 小练习与答案

**练习 1**：`next_memory_leak` 里为什么要在 `iter.next_back()` 之后再调 `map.remove(&1)`，而不是之前？

> **答案**：顺序是为了让迭代器句柄先持有该节点的引用。如果先 `remove`，节点进入「逻辑删除待回收」状态，此时迭代器仍持有引用，会推迟回收——这正是要测的场景（句柄活着 → 节点不能被回收）。反过来（先 remove 再 next_back）测的是另一条路径，已被 `iter` 测试覆盖。

**练习 2**：`drops` 测试断言 `VALUES == 7` 而非 `8`，为什么 Value 比 Key 少析构一个？

> **答案**：因为 `remove(&key7)` 时构造的是查询键 `Key(7)`——只有 Key 类型，没有对应的 Value。所以 Key 多析构一个（8 个），Value 仍只是插入的 7 个。这个不对称恰好验证了「查询键也参与了析构计数」。

---

### 4.4 tests/base.rs 的 RefEntry 包装与 release_with_pin 惯用法

#### 4.4.1 概念说明

`tests/map.rs` 测高层 `SkipMap` 时，`Entry` 的 `Drop` 由库自动处理（内部用 `ManuallyDrop` + `release_with_pin(epoch::pin)`，见 u4-l14）。但 `tests/base.rs` 测底层 `base::SkipList` 时，`RefEntry` 的引用计数**必须由调用方手动释放**——因为底层不提供自动 Drop。

底层 `RefEntry` 提供两种释放方式（u4-l14 已分析，此处只列签名做对照）：

[src/base.rs#L1653-L1666](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1653-L1666) — `RefEntry::release(&Guard)` 直接退计数；`release_with_pin(F: FnOnce() -> Guard)` 仅在计数归零时才调用 `pin`。

问题是：测试里大量地方拿到 `RefEntry` 后想让它「离开作用域就自动释放」，但测试作用域内**未必持有一个活着的 `Guard`**。如果用 `release(guard)`，就必须保证 `guard` 还活着；而 `release_with_pin(epoch::pin)` 把「是否 pin」推迟到 drop 那一刻，只有计数真的归零需要 `defer_unchecked` 时才 pin，最省开销。

#### 4.4.2 核心流程：Entry 包装的 Drop 委托

`tests/base.rs` 顶部定义了一个本地 `Entry` 包装结构，把 `Option<base::RefEntry>` 装进去，并为它实现 `Drop` 调 `release_with_pin(epoch::pin)`：

```rust
fn ref_entry<'a, K, V>(e: impl Into<Option<base::RefEntry<'a, K, V>>>) -> Entry<'a, K, V> {
    Entry(e.into())
}
struct Entry<'a, K, V>(Option<base::RefEntry<'a, K, V>>);
impl<K, V> Drop for Entry<'_, K, V> {
    fn drop(&mut self) {
        if let Some(e) = self.0.take() {
            e.release_with_pin(epoch::pin)
        }
    }
}
```

[tests/base.rs#L11-L26](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/tests/base.rs#L11-L26) — `ref_entry` 辅助函数把 `base` 返回值包成自动释放的 `Entry`，`Drop` 调 `release_with_pin(epoch::pin)`。

`ref_entry` 辅助函数的妙处在于 `impl Into<Option<...>>` 参数：它既能接受 `Option<RefEntry>`（如 `s.remove` 的返回值，可能为 `None`），也能接受直接的 `RefEntry`（`Into` 会自动包 `Some`）。于是测试里可以两种写法统一处理：

- `s.insert(x, ..., guard).release(guard);` —— 拿到 `RefEntry` 后立即用现有 guard 释放（最省，复用 guard）。
- `ref_entry(s.remove(x, guard));` —— 包成 `Entry` 后不绑定，离开作用域自动释放（适合不想管 guard 生命周期的场景）。

**惯用法总结**：

| 场景 | 写法 | 何时用 |
| --- | --- | --- |
| 已有活 guard，想立即退计数 | `.release(guard)` | 大多数热路径，最高效 |
| 不想管 guard，让它自动 drop | `ref_entry(...)` 包装 | 测试辅助、临时句柄 |
| 计数可能归零、需 defer | `release_with_pin(epoch::pin)` | 包装的 Drop 实现 |

#### 4.4.3 源码精读：并发场景下的 pin 重试

`get_or_insert_with_parallel_run` 是引用计数 + epoch 协作的经典展示：一个线程在闭包里 `sleep 4s` 故意拖延 `get_or_insert_with` 的返回，另一线程趁机 `get_or_insert` 先写入。这测的是「闭包求值期间被并发抢先」的处理——闭包仍会被调用，但最终值以先写入者为准。

[tests/base.rs#L490-L523](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/tests/base.rs#L490-L523) — 两线程并发 `get_or_insert_with` / `get_or_insert`，用 `sleep` 制造重叠，断言闭包被调用且最终值为先写入的 700。

#### 4.4.4 代码实践（Miri 验证型）—— 本讲核心实践之一

1. 实践目标：用 Miri 验证 `tests/base.rs` 中 `unsafe` 的内存安全性。
2. 操作步骤：
   - 确认已装 nightly：`rustup toolchain install nightly`。
   - 装 miri 组件：`rustup +nightly component add miri`。
   - 跑一个**单线程**用例（Miri 跑并发极慢，先选简单的）：
     ```bash
     cargo +nightly miri test --test base insert
     ```
   - 若环境支持，再跑一个并发小用例（注意 `cfg!(miri)` 已把规模缩到 100）：
     ```bash
     cargo +nightly miri test --test base remove_race
     ```
3. 需要观察的现象：Miri 输出 `0 undefined behaviors`（或类似无 UB 的总结）。
4. 预期结果：单线程 `insert` 用例在 Miri 下通过且无 UB 报告；`remove_race` 因 Miri 慢可能耗时数分钟但最终通过。
5. 若 Miri 不可用，标注「待本地验证」，并改用 `cargo test --test base` 至少跑通功能。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `tests/base.rs` 不直接用 `base::RefEntry` 而要包一层自己的 `Entry`？

> **答案**：因为 `base::RefEntry` 没有自动 `Drop`（底层要求调用方显式释放引用计数，否则泄漏）。包一层 `Entry` 并实现 `Drop` 调 `release_with_pin`，让测试代码像写高层 `SkipMap` 一样「离开作用域自动释放」，既避免泄漏又简化测试。这本质上是在测试里**复刻**了高层 `map::Entry = ManuallyDrop<base::RefEntry>` 的 Drop 行为（见 u4-l14）。

**练习 2**：`release_with_pin(epoch::pin)` 比 `release(guard)` 慢多少？何时值得用前者？

> **答案**：当计数**不归零**时，`release_with_pin` 根本不会调 `epoch::pin()`（闭包不被求值），开销与 `release` 几乎相同；只有计数归零需要 `defer_unchecked` 时才会 pin。所以「慢」只发生在最后那次释放。值得用的场景是：你没有现成的活 `Guard`（如 `Drop` 实现里），否则应优先 `release(guard)` 复用现有 guard。

---

### 4.5 基准对比：四组 insert / iter / lookup / insert_remove

#### 4.5.1 概念说明

`benches/` 下四个文件构成一组**同构基准矩阵**：用相同的输入序列、相同的操作数量（1000 个 key）测四种容器，只留「容器实现」这一个变量。四组基准覆盖四个最常用操作：

| 基准 | 测什么 | 参与容器 |
| --- | --- | --- |
| `insert` | 连续插入 1000 个 key | skiplist / skipmap / btree / hash |
| `iter` | 正向遍历全部元素 | 全部 |
| `rev_iter` | 反向遍历 | skiplist / skipmap / btree（**hash 无此项**） |
| `lookup` | 查 1000 个已存在 key | 全部 |
| `insert_remove` | 插 1000 个再删 1000 个 | 全部 |

注意 `hash.rs` 没有 `rev_iter`——`HashMap` 无序，反向遍历无意义。这本身就是一个选型信号：**需要有序遍历就不能用 hash**。

所有四个文件都用同一个确定性 PRNG 生成 key：

```rust
num = num.wrapping_mul(17).wrapping_add(255);
```

这保证四个容器看到**完全相同的 key 序列**，对比才公平（u1-l4 已分析）。

#### 4.5.2 核心流程：四组基准的结构对照

四个文件的 `insert` 基准结构几乎逐行相同，只有「容器类型」和「是否传 guard」不同。以 `insert` 为例对照 `btree.rs`（单线程基线）与 `skiplist.rs`（底层跳表）：

[benches/btree.rs#L9-L20](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/benches/btree.rs#L9-L20) — `BTreeMap` 基线：`b.iter` 内 `Map::new()` + 1000 次 `map.insert(num, !num)`，无 guard。

[benches/skiplist.rs#L10-L23](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/benches/skiplist.rs#L10-L23) — 底层 `SkipList`：构造时传 `epoch::default_collector().clone()`，每次 `insert` 额外传 `&guard`，且这里**故意不 release**返回的 entry（测纯插入开销）。

[benches/hash.rs#L9-L20](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/benches/hash.rs#L9-L20) — `HashMap` 基线：与 btree 几乎一样，只换容器类型。

对照 `lookup` 基准，注意 skiplist 版用 `black_box` 包裹结果防止优化器消除：

[benches/skiplist.rs#L61-L79](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/benches/skiplist.rs#L61-L79) — `lookup`：重新生成 key 序列查 1000 次，`black_box(map.get(&num, guard))` 防优化。

`insert_remove` 是最综合的一组，测「插入后立即删除」的 churn 开销，最能体现 epoch 回收的额外代价：

[benches/skiplist.rs#L81-L100](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/benches/skiplist.rs#L81-L100) — `insert_remove`：插 1000 个（每个 `.release(guard)`）再删 1000 个（`.unwrap().release(guard)`），两次 release 都退计数。

> **注意 skiplist 的 iter 基准里 `release` 的位置**：插入阶段 `map.insert(...).release(guard)` 立即释放句柄，确保 `iter` 阶段没有句柄持有引用、纯粹测遍历开销。这是基准「控制变量」的细节。

#### 4.5.3 选型直觉：跳表何时占优

基于这四组基准（u1-l4 的结论 + 本讲结构分析），单线程下的典型排序是：

\[ \text{HashMap} \;\lesssim\; \text{BTreeMap} \;\lesssim\; \text{SkipList/SkipMap} \quad (\text{单线程裸速度}) \]

跳表因每次插入要随机高度 + 多层 CAS + epoch pin，单线程下天然比 `BTreeMap`（纯指针操作）慢。**跳表的价值不在单线程，而在「并发且有序」**：

- 需要有序 + 多线程高频写 → `SkipMap`（lock-free，无锁竞争）。
- 读多写少 + 有序 → `RwLock<BTreeMap>` 可能更快（读路径无原子开销）。
- 无需有序 + 高并发 → `DashMap`/`flurry`（分片或无锁 hash）。

`insert_remove` 基准尤其值得关注：它揭示了 epoch 回收在「高 churn」场景的代价——频繁删除会产生大量待回收垃圾，`flush` 时机影响吞吐。

#### 4.5.4 代码实践（运行 + 分析型）—— 本讲核心实践之二

1. 实践目标：亲手跑四组基准，量化选型决策。
2. 操作步骤：
   - 切到 nightly：`rustup override set nightly`。
   - 跑全部基准：`cargo bench`（会编译并运行四个文件的所有 `#[bench]`）。
   - 把结果按「容器 × 操作」整理成表格。
3. 需要观察的现象：
   - `insert`：hash 最快，skiplist 最慢（单线程）。
   - `lookup`：hash 最快，btree 与 skiplist 接近。
   - `iter`/`rev_iter`：三者接近（线性遍历）。
   - `insert_remove`：skiplist 因 epoch 回收落后明显。
4. 预期结果：得到一张表，能回答「在 N=1000、单线程下，SkipMap 比 BTreeMap 慢几倍」。
5. 若无 nightly，标注「待本地验证」，可用 `cargo test --release --benches` 粗略跑（但 `#[bench]` 需 nightly 才能编译）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `hash.rs` 没有 `rev_iter` 基准？

> **答案**：`HashMap` 无序，`iter()` 的顺序未指定，反向遍历没有语义意义也无法与有序容器公平对比。它的缺席本身提醒读者：一旦需要有序遍历，`HashMap` 直接出局。

**练习 2**：`skiplist.rs` 的 `insert` 基准里返回的 entry 没有 `.release(guard)`，而 `iter` 基准的插入阶段却 `.release(guard)` 了，为什么？

> **答案**：`insert` 基准测的是「纯插入开销」，entry 在 `b.iter` 闭包结束时随 `map` 一起 drop，不需单独 release（整个跳表被销毁，引用计数无意义）。而 `iter` 基准的 map 要存活到遍历阶段，若不 release 插入产生的句柄，这些句柄会持有引用，干扰遍历基准的纯度（多了一份额外计数开销）。这是「按测试目的精确控制变量」的体现。

**练习 3**：如果要让基准反映**多线程**性能，应该怎么改？

> **答案**：在 `b.iter` 内用 `thread::scope` 起多线程并发 insert/lookup，按线程数分摊 key 范围。此时 `SkipMap` 因 lock-free 应展现出随线程数近线性扩展的吞吐，而 `RwLock<BTreeMap>` 会因写锁竞争被压制。这正是跳表真正的优势区间——但当前 `benches/` 是单线程对比，只用于建立「单线程基线」。

---

## 5. 综合实践

把本讲四块知识串起来，完成一个**「写测试 + 跑 Miri + 看基准」**的完整闭环：

**任务**：为 `SkipMap` 实现一个「并发单调递增计数器」并验证其正确性与内存安全性。

1. **写并发测试**（承接 4.2）：在 `tests/map.rs` 新增测试 `concurrent_counter`：
   - 创建 `SkipMap::<i32, i32, _>::new()`，先 `insert(0, 0)`。
   - 起 16 个线程，每个线程循环 1000 次：读出当前值 `v`，用 `compare_insert(0, v + 1, |x| *x <= v)` 把值加 1（闭包保证只在当前值仍为 `v` 时才替换，模拟 CAS）。
   - 全部 join 后断言 `*map.get(&0).unwrap().value() == 16_000`。
   - **思考**：这个闭包为何能保证「不丢更新」？（提示：每次成功替换都使值严格 +1，闭包失败则重试读新值。）
2. **跑 Miri**（承接 4.4）：把线程数与循环数用 `cfg!(miri)` 缩减（如 Miri 下 2 线程 × 5 次），运行 `cargo +nightly miri test --test map concurrent_counter`，确认无 UB。
3. **看基准**（承接 4.5）：跑 `cargo bench`，记录 `skipmap::insert` 与 `btree::insert` 的耗时比，结合你的并发计数器测试，写一段 200 字的结论：**「在 16 线程下，SkipMap 的单线程劣势是否被并发优势反转？」**
4. **预期结果**：测试通过、Miri 无 UB、基准数据记录完整。
5. 若多线程下断言偶发失败，说明你的闭包并非真正的 CAS（存在 TOCTOU 窗口），需用 `Entry` 句柄或 `loop { get → compare_insert }` 重试模式修复——这正好呼应 u5-l18 讲的「单操作原子、多操作非原子」。

> 说明：第 1 步的 `compare_insert` 闭包模式并非真正的无锁 CAS（闭包内看到的 `v` 与实际 `compare_insert` 执行的瞬间存在竞态），因此断言「== 16000」**可能失败**。这正是本任务的「陷阱」：让你亲历 u1-l1/u5-l18 反复强调的「多操作非原子」。修复方法是包成 `loop { let e = map.get(&0)?; let v = *e.value(); if map.compare_insert(0, v+1, |x| *x == v) ... }` 的重试循环，并用文字说明取舍。

---

## 6. 本讲小结

- 测试分三层文件：`tests/base.rs` 测底层无锁原语（需显式 guard），`tests/map.rs`/`tests/set.rs` 测高层封装；测试模式分四类——串行功能、并发竞争、内存安全、惯用法封装。
- 并发测试用 `cfg!(miri)` 把规模分两档（如 `10_000` vs `100`），让 Miri 小规模验证 UB、普通环境大规模验证统计活性。
- `compare_insert(key, value, |old| should_replace)` 的闭包接收旧值决定是否替换；`concurrent_compare_and_insert` 利用「单调 ratchet」性质使最终值可断言为最大 id。
- 内存安全测试有两套手法：`*_memory_leak` 针对迭代器句柄的引用计数泄漏，`drops` 用带 `Drop` 副作用的类型 + 自建 `Collector` 精确断言「恰好析构一次」（KEYS=8, VALUES=7）。
- `tests/base.rs` 用自定义 `Entry` 包装 + `release_with_pin(epoch::pin)` 复刻高层 `map::Entry` 的自动 Drop；`release_with_pin` 仅在计数归零时才 pin，是「按需 pin」的释放惯用法。
- `benches/` 四文件构成同构基准矩阵（insert/iter/rev_iter/lookup/insert_remove），用相同 PRNG 序列控制变量；结论是单线程下 `HashMap < BTreeMap < SkipMap`，跳表的价值在「并发且有序」。

---

## 7. 下一步学习建议

本讲是全册最后一篇，你已经读完了从「项目定位」到「测试与基准」的完整链路。建议接下来的学习方向：

- **动手改造**：尝试给 `base.rs` 的某个 `SeqCst` 操作（如 `mark_tower`）改成 `Acquire/Release`（u5-l17 讨论的 TODO），然后用本讲的 `remove_race` + Miri 验证是否仍正确——这是把理论内存序知识落地为工程验证的最佳练习。
- **扩展基准**：仿照 4.5.5 练习 3，写一组**多线程**基准，画出 SkipMap 吞吐随线程数的扩展曲线，与 `RwLock<BTreeMap>` 对比，亲见 lock-free 的优势区间。
- **回顾全册**：重读 `src/base.rs` 的 `search_bound` → `mark_tower` → `insert_internal` → `remove` 主链路，现在你应该能把每个函数与其对应的测试（`tests/base.rs` 的哪个用例在测它）和对应的基准（`benches/` 的哪组在量它）一一对应起来——这种「实现—测试—基准」三位一体的映射，是衡量你是否真正掌握一个并发库的标尺。
- **横向对比**：回顾 `src/lib.rs` 顶层文档的「Alternatives」章节（u1-l1 已读过），结合本讲的基准数据，思考「何时选 `SkipMap`、何时选 `RwLock<BTreeMap>`、何时选无序的并发 hash map」——形成自己的并发容器选型直觉。
