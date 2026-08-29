# 性能调优与基准测试

## 1. 本讲目标

学完本讲,你应该能够:

1. 说清 `with_min_len` / `with_max_len` 改变的到底是什么——不是结果,而是 `bridge` 递归中每一刀的裁决依据。
2. 会用 rayon-demo 自带的基准程序(matmul、quicksort、nbody、mergesort、sieve 等)测量并行加速比,并配合 `RAYON_NUM_THREADS` 画出加速比曲线、找出饱和点。
3. 理解缓存与内存布局对并行程序的影响:为什么矩阵乘法要用 Z-order 布局、为什么 nbody 要双缓冲、为什么排序阈值是"手调出来的"。
4. 建立一个正确的直觉:**Rayon 只负责把任务铺满 CPU,算法与内存访问模式才决定加速比的上限**。

本讲是单元九的第三讲,视角从「写得对」转向「跑得快」。前面 u9-l1、u9-l2 教你实现自定义迭代器,本讲教你回答"我的并行程序到底快了多少、为什么不再快了"。

## 2. 前置知识

### 2.1 加速比与 Amdahl 定律

**加速比**(speedup)= 串行耗时 / 并行耗时。若程序中可并行的部分占比为 \( p \),线程数为 \( P \),则理想加速比受 Amdahl 定律约束:

\[
S(P) = \frac{1}{(1-p) + \dfrac{p}{P}}
\]

即使 \( p = 1 \)(完美并行),收益也会在 \( P \) 增大时递减;若还有 5% 串行部分,加速比的天花板就是 \( 1/0.05 = 20 \),与线程数无关。**饱和点**就是继续加线程也不再提速的位置。

另外,u1-l1 介绍过工作窃取的期望时间界 \( T \approx W/P + O(S) \)(\( W \) 是总工作量,\( S \) 是关键路径长度)。\( S \) 由你的任务依赖图决定——归并排序的逐层归并、quicksort 的递归依赖都在拉长 \( S \),这是 Amdahl 之外的第二重天花板。

### 2.2 任务粒度:每次切分都有价格

Rayon 的切分不是免费的:`split_at` 之后要 `join_context` 派发、要入队、可能被窃取、结果还要 `reduce` 归并。若一个任务只处理 1 个元素而切分开销是几十纳秒,并行就成了负资产。**粒度**(granularity)指一个叶子任务串行处理的元素数:

- 粒度太细:切分开销占比失控,吞吐下降。
- 粒度太粗:任务数少于线程数,核在空转,负载不均。

Rayon 默认用 **thief-splitting** 自适应策略(见 u3-l3、u5-l4):初始只想切出约等于线程数的任务,只有真的发生窃取时才追加切分。`with_min_len` / `with_max_len` 是在这套自适应之上手动加的硬约束。

### 2.3 缓存行与局部性

CPU 不按字节读内存,而是按 **缓存行**(cache line,通常 64 字节)整块搬入。由此推出两条本讲反复用到的规则:

- **空间局部性**:顺序扫过连续内存,一次缓存行加载喂多次访问;跳跃访问则每次都可能 miss。
- **伪共享**(false sharing):两个线程写**不同**变量,但它们落在同一缓存行,缓存一致性协议会让这行在两个核之间来回弹跳,性能骤降。分块并行(par_chunks)之所以常常比纯元素并行快,一部分原因正是同一块数据集中在同一个缓存行、由同一个线程写。

### 2.4 本讲承接的前置讲义

- **u3-l3**(有索引适配器与长度控制):已介绍 `with_min_len`/`with_max_len` 的用法与 `LengthSplitter` 的裁决。本讲 4.1 节从**源码与性能测量**角度补完它,不重复接口用法。
- **u7-l1**(ThreadPoolBuilder):已知线程数优先级链为「显式 `num_threads` > `RAYON_NUM_THREADS` 环境变量 > 逻辑核数」。本讲的实验正是靠 `RAYON_NUM_THREADS` 逐档改变线程数。
- **u5-l4**(工作窃取队列):本地队列 LIFO 弹最新任务(保缓存热度)、窃取端 FIFO 偷最旧任务。
- **u8-l2**(并行归并排序):已知 `par_mergesort` 的分块—拼接—归并三阶段,本讲引用其中 `with_max_len(1)` 作为工程案例。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/iter/len.rs` | `MinLen` / `MaxLen` 两个适配器及其生产者,`with_min_len` / `with_max_len` 的实现本体 |
| `src/iter/plumbing/mod.rs` | `Producer::min_len` / `max_len` 默认值,`Splitter`(thief-splitting)与 `LengthSplitter`(带长度的裁决器),`bridge` 递归 |
| `rayon-demo/src/main.rs` | demo 程序入口:八个可 `cargo run` 的基准 + 十个仅 bench 的 `#[cfg(test)]` 模块 |
| `rayon-demo/src/matmul/mod.rs` | 矩阵乘法基准:四种算法(行主序串行 / Z-order 串行 / Z-order 并行 / Strassen),本讲缓存布局的主样本 |
| `rayon-demo/src/quicksort/mod.rs` | 并行快排基准:`Joiner` trait 双实现控制变量法,串行阈值 `5 * 1024` |
| `rayon-demo/src/fibonacci/mod.rs` | 纯 bench 模块:join 的大小分支顺序、`iter::split` 变体,"开销主导"的反面教材 |
| `rayon-demo/src/nbody/nbody.rs` | N 体模拟:双缓冲、串行 fold 与嵌套并行 `fold`+`reduce` 的对比 |
| `rayon-demo/src/nbody/mod.rs` | nbody 的计时与 speedup 打印 |
| `rayon-demo/src/join_microbench.rs` | 五个粒度变体的微基准(仅 bench) |
| `rayon-demo/src/sieve/mod.rs` | 筛法:`with_max_len(1)` 把每个 chunk 钉成独立任务 |
| `rayon-demo/src/mergesort/mod.rs` | demo 版归并排序:`SORT_CHUNK` / `MERGE_CHUNK` 阈值出自实测调优 |
| `src/slice/sort.rs` | 库内 `par_mergesort` 对 `with_max_len(1)` 的真实使用 |

## 4. 核心概念与源码讲解

### 4.1 任务粒度调优:with_min_len / with_max_len

#### 4.1.1 概念说明

`with_min_len(n)` 承诺「叶子任务至少串行处理 n 个元素」——它抬高切分的**硬下界**,防止任务被切得太碎;`with_max_len(n)` 承诺「叶子任务至多处理约 n 个元素」——它迫使 Rayon **至少**切到足够细。两者都不改变计算结果,只改变 `bridge` 递归树中「切」与「不切」的判决。

关键认知(u3-l3 已建立,这里用源码钉死):

- `min_len` 是**硬**约束:任何一刀之后若某半长度会小于 `min`,这刀就不切。
- `max_len` 是**软**约束:它只是把「期望切分次数」的下限抬高,自适应算法可能切得更细,但不会细过 `min_len`。
- 两者都要求 `IndexedParallelIterator`(长度已知才能判断「切下去还够不够 n 个」),`filter` 之后的迭代器用不了——这是 u2-l1 讲过的索引能力丢失。

什么时候需要手动调?源码文档说得很直白:Rayon 通常自动调得不错("this should not be needed"),需要手动干预的典型场景是**每个元素的工作量极端便宜**(如内存自增)或**极端昂贵**,使默认自适应的假设失效。

#### 4.1.2 核心流程

`bridge` 递归中每一层的判决流程(承接 u3-l3,补充粒度细节):

```text
helper(len, migrated, splitter, producer, consumer):
    若 consumer.full():              # 短路,直接收尾
        不再切分
    若 splitter.try_split(len, migrated) 为真:   # ← 粒度裁决在这里
        mid = len / 2
        生产者与消费者在同一 mid 上对齐切分
        join_context 并行两半(工作窃取在此发生)
        reducer 归并
    否则:
        producer.fold_with(...)      # 叶子:串行吃掉整段
```

`try_split` 的内部判决:

```text
LengthSplitter.try_split(len, stolen):
    return len / 2 >= min  and  Splitter.try_split(stolen)

Splitter.try_split(stolen):
    若 stolen(本任务被窃取过):
        把期望切分次数重置为 max(线程数, 剩余/2)   # 窃取说明还有空闲线程
        return true
    否则若 期望切分次数 > 0:
        期望次数减半,return true
    否则:
        return false                                  # 预算耗尽,进入叶子
```

`min` 与 `max` 进入裁决器的路径不同:

- `min`(来自 `with_min_len`)直接存进 `LengthSplitter.min`,卡在第一关 `len / 2 >= min`。
- `max`(来自 `with_max_len`)在构造时换算成**初始切分预算**:`min_splits = len / max`,若它大于默认预算(线程数),就取而代之。所以 len=12345、max=100 时初始预算为 123,实际切出 2 的幂(约 128)片。

#### 4.1.3 源码精读

**第一站:适配器本体只是"贴标签"。** [src/iter/len.rs:L9-L19](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/len.rs#L9-L19) 定义 `MinLen` 为「上游迭代器 + 一个数字」,它不切分任何东西,只是在 `with_producer` 时把上游生产者包进 `MinLenProducer`:

> [src/iter/len.rs:L51-L81](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/len.rs#L51-L81) —— `MinLen::with_producer` 经回调拿到上游生产者 `base`,包一层 `MinLenProducer { base, min }` 再交给下游。u4-l1 讲过:适配器包装生产者、消费者自链尾向链头包装,这里是前者的实例。

真正的语义在包装生产者的两个方法里。`MinLenProducer` 只改粒度窗口的下沿,切分本身纯转发:

> [src/iter/len.rs:L103-L109](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/len.rs#L103-L109) —— `min_len()` 返回 `max(self.min, base.min_len())`:与上游窗口取交集,不会因为包装而放松约束;`max_len()` 原样透传。

> [src/iter/len.rs:L111-L123](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/len.rs#L111-L123) —— `split_at(index)` 只是调用 `base.split_at(index)` 后把左右两个子生产者再各自包一层 `MinLenProducer`,自己不做任何下标算术。这就是 u4-l2 说的「包装生产者只改粒度、切分纯转发」。

`MaxLenProducer` 完全对称,唯一区别是改的是窗口上沿:

> [src/iter/len.rs:L233-L239](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/len.rs#L233-L239) —— `max_len()` 返回 `min(self.max, base.max_len())`,同样取交集。

**第二站:裁决器。** 先看 `Producer` 契约中的默认值:

> [src/iter/plumbing/mod.rs:L68-L93](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L68-L93) —— `min_len()` 默认 1(可以一路切到单个元素),`max_len()` 默认 `usize::MAX`(可以完全不切)。文档注释明说:Rayon 通常会自动调整切分大小来压低开销,这两个旋钮"一般不需要动"。

自适应的核心是 `Splitter` 的**窃取重置**:

> [src/iter/plumbing/mod.rs:L245-L284](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L245-L284) —— `Splitter` 只有一个字段 `splits`(还想要切几刀)。`new()` 从 `current_num_threads()` 起步;`try_split(stolen)` 中,一旦 `stolen == true`(任务被别的线程偷走过),就把预算重置回线程数——**窃取是最真实的"还有人在等活"信号**,于是自适应地追加切分。未被窃取时预算逐刀减半,减到 0 就停止切分。

`LengthSplitter` 在此之上叠加长度约束,构造函数揭示了 `max` 的换算方式:

> [src/iter/plumbing/mod.rs:L297-L326](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L297-L326) —— `min` 经 `max(min, 1)` 钉死硬下界;`min_splits = len / max` 算出「要降到 max 以下至少要切几刀」,只在它**大于**当前预算时才覆盖(注释举例:len=12345、max=100 → 123 刀 → 实际 2 的幂 128 片)。注意它只抬高预算、不会降低——自适应的窃取重置依然生效。

判决本身是一行浓缩的布尔式:

> [src/iter/plumbing/mod.rs:L328-L332](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L328-L332) —— `len / 2 >= self.min && self.inner.try_split(stolen)`:先保证切完的每一半都不小于 `min`(min_len 是硬下界),再问自适应预算答不答应。

最后看裁决器在 `bridge` 中的接入点:

> [src/iter/plumbing/mod.rs:L385-L433](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L385-L433) —— `bridge_producer_consumer` 用 `producer.min_len()` / `producer.max_len()` / `len` 构造 `LengthSplitter`,随后 `helper` 递归:先查 `consumer.full()` 短路,再 `splitter.try_split(len, migrated)` 裁决;切分时生产者与消费者在同一 `mid = len / 2` 对齐切开,`join_context` 并行两半(`context.migrated()` 就是喂给 `Splitter` 的窃取信号,见 u5-l1),末尾 `reducer.reduce` 归并;不切则 `producer.fold_with(consumer.into_folder())` 串行收尾。

**第三站:库内的真实用法。** `par_mergesort` 的第一阶段要「每个 2000 元素的块各自成为独立任务」,用的正是 `with_max_len(1)`:

> [src/slice/sort.rs:L1565-L1576](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/sort.rs#L1565-L1576) —— `v.par_chunks_mut(CHUNK_LENGTH).with_max_len(1).enumerate().map(...)`:`par_chunks_mut` 已把数据切成 CHUNK_LENGTH 大小的块,`with_max_len(1)` 再强制「每块必切」,杜绝自适应策略因预算不足而把两块合进一个叶子任务——这里的正确性依赖各块独立写入互不重叠的区间,任务边界清晰即性能边界清晰(u8-l2 详述了三阶段)。

sieve demo 的注释把同一个意图写得更直白:

> [rayon-demo/src/sieve/mod.rs:L125-L128](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/sieve/mod.rs#L125-L128) —— `high.par_chunks_mut(CHUNK_SIZE).enumerate().with_max_len(1)` 注释原话:"ensure every single chunk is a separate rayon job"(确保每个 chunk 都是独立的 rayon 任务)。

而微基准 `join_microbench` 则把五个粒度档位摆在一起供对比(该文件是仅 bench 模块,见 4.2 节的运行说明):

> [rayon-demo/src/join_microbench.rs:L14-L34](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/join_microbench.rs#L14-L34) —— 同一个「10 万个 usize 自增」的 `for_each`,分别加 `with_min_len(1024)` 与 `with_min_len(usize::MAX)`(后者相当于彻底禁止切分、退化为串行)。

> [rayon-demo/src/join_microbench.rs:L36-L56](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/join_microbench.rs#L36-L56) —— 再加 `with_max_len(100)` 与 `with_max_len(1)`(后者 10 万元素切成 10 万个单元素任务,切分开销彻底压过有效工作)。

#### 4.1.4 代码实践

**实践目标**:亲手观测粒度对耗时的影响,验证「粒度只改性能、不改结果」。

**操作步骤**(在示例工程中新建 `bin/granularity.rs`,以下为示例代码,非仓库原有):

```rust
use rayon::prelude::*;
use std::time::Instant;

fn main() {
    let v: Vec<u64> = (0..10_000_000).collect();
    for (label, len) in [
        ("default", None),
        ("min_len=1", Some((1usize, usize::MAX))),
        ("min_len=1024", Some((1024, usize::MAX))),
        ("min_len=1M", Some((1 << 20, usize::MAX))),
        ("min_len=MAX(串行)", Some((usize::MAX, usize::MAX))),
    ] {
        let start = Instant::now();
        let sum = match len {
            None => v.par_iter().sum::<u64>(),
            Some((min, _)) => v.par_iter().with_min_len(min).sum::<u64>(),
        };
        let dur = start.elapsed();
        println!("{label:<20} sum={sum} 用时 {dur:?}");
    }
}
```

**需要观察的现象**:

1. 所有档位的 `sum` 完全一致(粒度不影响结果)。
2. `min_len=1` 与 `default` 差别很小(自适应本来就不打算切到 1)。
3. `min_len=1M` 或 `usize::MAX` 时,任务数少于线程数,耗时显著上升(并行度不足)。
4. 具体数值**待本地验证**——不同机器、不同核数下最优档位不同,这正是「调优」的含义。

**预期结果**:耗时随粒度变化呈 U 形曲线:太细一端(切分开销)与太粗一端(并行度不足)都慢,中段最快。用 `--release` 运行,并在正式计时前先跑一次预热(触发全局线程池初始化,见 u5-l3 的惰性单例)。

#### 4.1.5 小练习与答案

**练习 1**:为什么 `with_min_len(usize::MAX)` 几乎等于串行,而 `with_max_len(usize::MAX)` 却几乎等于默认行为?

**答案**:`min` 是硬下界,判决式 `len / 2 >= min` 在 `min = usize::MAX` 时几乎恒为假,一刀都切不下去,递归直接落到 `fold_with` 串行收尾。`max` 只用于构造时抬高初始切分预算(`min_splits = len / max`),`max = usize::MAX` 时 `min_splits = 0`,不会覆盖默认的线程数预算,自适应行为原样保留。

**练习 2**:`with_max_len(1)` 在 `par_chunks_mut(CHUNK_LENGTH)` 之后(如 sort.rs:1565)与直接对一个大切片用 `with_max_len(1)`,开销有何不同?

**答案**:前者先按块切好,迭代器的每个元素已经是 2000 元素的块,`with_max_len(1)` 只是要求「每个块一个任务」,任务总数约为 len/2000,量级可控;后者若对 `par_iter()` 直接 `with_max_len(1)`,意味着每个**元素**一个任务,任务数等于元素数,切分与派发开销彻底失控。粒度旋钮的效果取决于「迭代器的元素是什么」,不只是参数本身。

**练习 3**:`Splitter::try_split` 在 `stolen == true` 时为什么把预算**重置回线程数**而不是加一?

**答案**:任务被窃取说明存在空闲线程来偷活——这是对负载分布的免费实时采样。重置回线程数让这个子树重新具备「够每个线程分一份」的切分潜力,把被偷走的那部分并行度补回来;只加一刀只能制造一个新任务,无法应对多个空闲线程同时扒窃的局面。

### 4.2 rayon-demo 基准:测量并行加速比

#### 4.2.1 概念说明

rayon-demo 是仓库自带的基准程序集合,它同时提供两类入口(u1-l2 已跑通环境):

1. **八个可 `cargo run` 的 demo**:`matmul`、`mergesort`、`nbody`、`quicksort`、`sieve`、`tsp`、`life`、`noop`,命令行直接运行,自己用 `Instant` 计时并打印 speedup。
2. **十个仅 bench 的模块**:`factorial`、`fibonacci`、`find`、`join_microbench`、`map_collect`、`pythagoras`、`sort`、`str_split`、`tree`、`vec_collect`,它们标了 `#[cfg(test)]`,只在 `cargo bench`(需要 nightly,因 `#![cfg_attr(test, feature(test))]`)时参与,从命令行调用只会打印用法并以退出码 1 退出。

基准测量的三个方法论要点,rayon-demo 都做了示范:

- **控制变量**:quicksort 用同一个 `quick_sort` 泛型函数,只把 `Joiner` 换成 `Parallel` 或 `Sequential`,串行与并行跑完全相同的算法,差异只来自并行本身。
- **公平计时**:每组计时都重新构造相同输入(matmul、quicksort 用固定种子的 RNG,`seeded_rng`),且计时后断言结果正确(`is_sorted`)。
- **同机对比**:speedup = 串行耗时 / 并行耗时,同一进程内先后运行,排除机器差异。

#### 4.2.2 核心流程

以 `cargo run --release -p rayon-demo -- matmul bench --size 1024` 为例:

```text
main(argv)
  └─ match "matmul" → matmul::main(&args[1..])
       └─ Docopt 解析子命令与 --size
            └─ cmd_bench:
                 ① size ≤ 1024 时:timed_matmul(seq_matmul)   "seq row-major"
                 ② size ≤ 2048 时:timed_matmul(seq_matmulz)   "seq z-order"
                 ③ 总是:        timed_matmul(matmulz)         "par z-order"
                 ④ 总是:        timed_matmul(matmul_strassen) "par strassen"
                 ⑤ speedup = ② / ③
```

要改变线程数,外层套环境变量:`RAYON_NUM_THREADS=k cargo run --release -p rayon-demo -- matmul bench`(u7-l1 的优先级链保证环境变量在未显式 build 时生效)。

#### 4.2.3 源码精读

**入口路由**:

> [rayon-demo/src/main.rs:L42-L66](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/main.rs#L42-L66) —— USAGE 列出七个可用 demo 及各自的简介;注意 `rayon-demo bench` 可以整套运行(`cargo bench` 的入口)。

> [rayon-demo/src/main.rs:L18-L40](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/main.rs#L18-L40) —— 十个仅 bench 模块挂在 `#[cfg(test)]` 下,`extern crate test` 同样条件编译:普通 `cargo run` 的二进制里根本不存在这些代码,这就是它们"命令行只打印用法"的原因。

> [rayon-demo/src/main.rs:L81-L91](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/main.rs#L81-L91) —— `main` 的 match 只认识八个名字,其余走 `_ => usage()`(打印用法、退出码 1)。`seeded_rng` 用固定字节种子构造 RNG,保证每次运行输入一致。

**matmul:四种算法一次跑齐。**

> [rayon-demo/src/matmul/mod.rs:L375-L398](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/matmul/mod.rs#L375-L398) —— `timed_matmul`:size 向上取整为 2 的幂,构造 a/b/dest 三个 `size*size` 的 f32 矩阵(元素由下标决定,可复现),`Instant` 计时后打印秒数并返回纳秒,供外部算 speedup。

> [rayon-demo/src/matmul/mod.rs:L400-L420](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/matmul/mod.rs#L400-L420) —— `main` 的 bench 分支:size ≤ 1024 才跑行主序串行("Crappy algorithm takes several minutes on larger inputs",O(n³) 的朴素实现太慢);size ≤ 2048 才跑 Z-order 串行;并行版与 Strassen 总是跑;最后 `speedup = seq / par`——注意分子是 **Z-order 串行**而非行主序串行,这是「相同布局下并行 vs 串行」的公平对比。若 size > 2048,seq 计为 0,speedup 打印为 0,此时需自己记录绝对耗时。

并行结构是 u5-l1 `join` 的直接应用:

> [rayon-demo/src/matmul/mod.rs:L141-L154](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/matmul/mod.rs#L141-L154) 与 [rayon-demo/src/matmul/mod.rs:L157-L190](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/matmul/mod.rs#L157-L190) —— `join4` / `join8` 用嵌套的 `rayon::join` 把 4 份、8 份闭包并行铺开,这是「join 组合出 N 叉并行」的惯用法。

> [rayon-demo/src/matmul/mod.rs:L193-L222](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/matmul/mod.rs#L193-L222) —— `matmulz` 的分治:小于 `MULT_CHUNK`(1×1024 个 f32)时落到 `seq_matmulz` 串行;否则把 a、b、dest 各四等分,`join8` 同时算 8 个子矩阵乘积(其中 4 个直接写 dest、4 个写临时缓冲 tmp),最后 `rmatsum` 把 tmp 并行累加回 dest——**递归基 `MULT_CHUNK` 就是手写的粒度阈值**,与 4.1 节的 `with_min_len` 异曲同工:join 路径没有 LengthSplitter,阈值全靠递归函数自己写。

**quicksort:Joiner 控制变量法。**

> [rayon-demo/src/quicksort/mod.rs:L30-L56](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/quicksort/mod.rs#L30-L56) —— `Joiner` trait 只有两个方法:`is_parallel()` 和 `join`。`Parallel` 实现转调 `rayon::join`——这是 u5-l1 讲过的「B 入队、先执行 A、再认领」协议;泛型参数让算法代码完全不感知并行。

> [rayon-demo/src/quicksort/mod.rs:L58-L76](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/quicksort/mod.rs#L58-L76) —— `Sequential` 的 `join` 只是先算 A 再算 B、`is_parallel()` 返回 false。同一个 `quick_sort<J>` 跑两种模式,唯一变量是并行——教科书级的基准设计。

> [rayon-demo/src/quicksort/mod.rs:L78-L90](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/quicksort/mod.rs#L78-L90) —— `quick_sort` 本体:`v.len() <= 1` 返回;并行模式下 `v.len() <= 5 * 1024` 直接转串行递归——**又一个手写阈值**(单位元素的工作量很小,512 个元素以下的子问题不值得派发);然后 `partition` 选末元素为轴分区,`split_at_mut` 得到 lo/hi,`J::join` 并行递归两半。注意轴选择是固定的(末元素),对特定输入会退化,基准用随机数据回避了这一点。

> [rayon-demo/src/quicksort/mod.rs:L114-L143](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/quicksort/mod.rs#L114-L143) —— `timed_sort` 计时后立刻 `assert!(is_sorted(&v))` 把正确性检查焊死在基准里;`main` 里 `--par-only` 可跳过串行对照,默认跑 seq → par → 打印 speedup。默认 `--size` 是 250000000(约 1GB u32),做实验时务必调小。

**fibonacci:开销主导的反面教材。** 模块文档本身就把结论写透了:

> [rayon-demo/src/fibonacci/mod.rs:L1-L16](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/fibonacci/mod.rs#L1-L16) —— 注释原文:"We're doing very little real work in each job, so the rayon overhead is going to dominate. The serial recursive version will likely be faster, unless you have a whole lot of CPUs."(每个任务的真实工作量太小,rayon 开销将占主导;除非 CPU 非常多,串行递归版很可能更快)。递归斐波那契是 \( O(2^n) \) 的坏算法,基准的意义在于它的**不均衡切分**(F(n-1) 的工作量约是 F(n-2) 的两倍)恰好考验工作窃取。

它的六个 bench 变体全是控制变量法的样本:

> [rayon-demo/src/fibonacci/mod.rs:L44-L74](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/fibonacci/mod.rs#L44-L74) —— `fibonacci_join_1_2`(大分支 F(n-1) 先)与 `fibonacci_join_2_1`(小分支 F(n-2) 先)只交换 join 参数顺序。结合 u5-l1 的协议「B 入队、先执行 A」:大分支先意味着入队的是小分支、被偷走损失也小;两种写法的差异完全由调度语义决定,是观察 join 顺序影响的现成实验。

> [rayon-demo/src/fibonacci/mod.rs:L76-L94](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/fibonacci/mod.rs#L76-L94) —— `fibonacci_split_recursive` 用 `rayon::iter::split`(u3-l6 讲过的按值切分函数)把递归树变成并行迭代器再 `map(fib_recursive).sum()`,与 join 版本对照。

> [rayon-demo/src/fibonacci/mod.rs:L96-L121](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/fibonacci/mod.rs#L96-L121) —— `fibonacci_split_iterative` 与 `fibonacci_iterative` 揭示玩笑的谜底:迭代版 fib 是 \( O(n) \),任何并行都救不了 \( O(2^n) \) 的算法——"Parallelism can't make up for a bad choice of algorithm"(并行弥补不了糟糕的算法选择)。

#### 4.2.4 代码实践

**实践目标**:测出 matmul 与 quicksort 在不同线程数下的加速比曲线,找出饱和点并解释收益递减。

**操作步骤**:

1. 确认机器逻辑核数 `nproc`(记为 N)。
2. 依次运行(必须 `--release`):

```bash
for t in 1 2 4 8 $(nproc); do
  echo "=== RAYON_NUM_THREADS=$t ==="
  RAYON_NUM_THREADS=$t cargo run --release -p rayon-demo -- matmul bench --size 1024
done
```

3. 换 quicksort(默认 size 是 250000000,先调小到千万级,并保留一次完整尺寸的运行作对照):

```bash
for t in 1 2 4 8 $(nproc); do
  RAYON_NUM_THREADS=$t cargo run --release -p rayon-demo -- quicksort bench --size 10000000
done
```

4. 把每次输出的 `par z-order` 耗时与 `speedup: ...x` 抄进表格,以线程数为横轴、speedup 为纵轴画曲线。

**需要观察的现象**:

- matmul 是 \( O(n^3) \) 计算、\( O(n^2) \) 数据的密集型负载,理论可并行度极高,低线程数时 speedup 应接近线性。
- 曲线在中段开始弯折:到达某线程数 \( k \) 后 speedup 几乎不再增长,即**饱和点**。
- `RAYON_NUM_THREADS=1` 时 `par z-order` 应明显慢于 `seq z-order`(单线程还要付切分与窃取探测的账)。
- 具体数值**待本地验证**。

**预期结果与解释**(收益递减的四类来源,按你的测量逐一对号入座):

1. **内存带宽饱和**:matmul 的有效工作是乘加,每字节数据的计算量固定;当 N 个线程同时压满内存控制器后,加核只在排队。这是饱和点最常见的原因。
2. **关键路径**:quicksort 的递归依赖(lo/hi 分区有先后)、mergesort 的逐层归并,都使 \( T \approx W/P + O(S) \) 里的 \( S \) 不可压缩;分区末尾总有一个最大的 half 在拖尾。
3. **非并行部分**:计时包含 RNG 生成输入、结果校验等串行段,服从 Amdahl 定律。
4. **调度开销**:任务派发、窃取扫描、join 的 Latch 同步都要时间,线程越多窃取竞争越频繁。

另外注意:`RAYON_NUM_THREADS=1` 与串行版本并不等价——单线程池仍会经历完整的切分/入队/认领循环,这个差值就是「纯调度开销」的直观估计。

#### 4.2.5 小练习与答案

**练习 1**:为什么 matmul 的 speedup 用 `seq_matmulz`(Z-order 串行)作分母,而不是更"朴素"的 `seq_matmul`(行主序串行)?

**答案**:控制变量。`seq_matmul` 与 `matmulz` 不仅串行/并行不同,**内存布局也不同**,两个变量同时变化就无法归因。用相同 Z-order 布局的串行版作分母,差值才纯粹来自并行化;行主序版本另作布局影响的参照(4.3 节)。

**练习 2**:quicksort 的 `--size` 默认 250000000,而递归阈值是 `5 * 1024`;粗略估算叶子任务的数量级,并说明为什么这个粒度合理。

**答案**:快排每层近似对半,递归到 5120 个元素为止,叶子数约为 250000000 / 5120 ≈ 48800 个(随数据分布波动)。每个叶子做约 5120 次比较加 swap,工作量在微秒级,远大于任务派发开销(几十纳秒量级);同时叶子数量远超线程数,工作窃取有足够的任务可以均衡负载。若阈值取 1,叶子数等于元素数,调度开销淹没有效工作。

**练习 3**:fibonacci 基准中 `fibonacci_join_1_2` 与 `fibonacci_join_2_1` 哪个可能更快?结合 u5-l1 的 join 协议说明。

**答案**:一般预期 `fibonacci_join_1_2`(大分支先执行、小分支入队)更稳。join 的协议是「B 入队、当前线程先执行 A、再认领」:若入队的 B 是大分支,它大概率被空闲线程偷走,当前线程又要再认领等它,同步开销更大;若入队的 B 是小分支,即使被偷走,当前线程做完大分支后很快就能等到它完成。但由于递归树不均衡、窃取时机随机,两者差异通常不大且随机器波动——这正是它作为微基准想暴露的东西。(实测结论待本地验证。)

### 4.3 缓存与内存布局

#### 4.3.1 概念说明

并行程序的性能下限常常不在 CPU,而在内存子系统。本讲看 rayon-demo 里的三个布局决策:

1. **Z-order(Morton)布局**:矩阵乘法把二维下标 (i,j) 的二进制位交织成一维下标,让四个空间相邻的元素在一维数组里也相邻,缓存行加载一次喂四次访问——同时天然适配四等分分治。
2. **双缓冲(ping-pong buffer)**:nbody 用两个 body 数组交替读写,新值写 A 数组、读 B 数组,下一 tick 反过来。不用任何锁就消除了读写冲突,还保证读侧是稳定的上一帧快照。
3. **嵌套并行的代价**:nbody 的 `tick_par` 外层并行、内层串行 fold;`tick_par_reduce` 内层再展开成 `par_iter().fold().reduce()`。任务更细不一定更快——切分、归并与缓存压力都会随粒度变细而上升。

此外,阈值常量本身就是「调优的化石」:mergesort demo 的 `SORT_CHUNK` / `MERGE_CHUNK` 注释直说"Values from manual tuning gigasort on one machine"(来自在一台机器上手工调优 gigasort 的经验值)——**粒度阈值没有公式,只有实测**。

#### 4.3.2 核心流程

Z-order 下标交织的原理:把 i 的二进制位放在偶数位、j 的放在奇数位:

\[
\text{index} = \sum_{k} \left( i_k \cdot 2^{2k+1} + j_k \cdot 2^{2k} \right)
\]

于是把一维下标按位拆开就能还原 (i,j),且 2×2 的四个相邻元素恰好占据连续的 4 个下标。nbody 的双缓冲则是一条简单的状态机:

```text
tick t:  读 bodies[t % 2]  →  写 bodies[(t+1) % 2]   (借 time & 1 选择方向)
tick 结束: time += 1,下一 tick 读写互换
```

#### 4.3.3 源码精读

**matmul:布局决定访存模式。** 先看行主序基线的病灶,源码里留着一条直指缓存问题的 TODO:

> [rayon-demo/src/matmul/mod.rs:L24-L45](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/matmul/mod.rs#L24-L45) —— 顶部 TODO 注释("Investigate other cache patterns for row-major order that may be more parallelizable")指向的正是 `seq_matmul` 的内层循环:计算 \( D_{ij} \) 时沿 k 扫过 `b[k << bits | j]`,对 b 的访问按**列**跳跃,每次前进一整行,缓存行利用率极差;`get_unchecked` 只省掉了边界检查,救不了 miss。

Z-order 版本用一个"位交织计数器"沿对角方向扫 k:

> [rayon-demo/src/matmul/mod.rs:L47-L76](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/matmul/mod.rs#L47-L76) —— `SplayedBitsCounter` 的 `next()`:先用 `& 0x5555_5555` 取出奇数位(即 j 的位)作为产出值,再把所有偶数位置 1 后加 1——进位只沿偶数位(即 i 的位)传播。产出序列 0b0, 0b1, 0b100, 0b101, …(见其单元测试),也就是 k 的高半位与低半位交替推进。

> [rayon-demo/src/matmul/mod.rs:L89-L117](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/matmul/mod.rs#L89-L117) —— `seq_matmulz` 用 `i = ij & 0xaaaa_aaaa`、`j = ij & 0x5555_5555` 从单个下标还原出 (i,j),再让 k 走 `SplayedBitsCounter`;布局换成 Z-order 后,a、b 的访问都沿交织方向推进,与 dest 的遍历方向协同,局部性显著好于行主序按列跳。函数标了 `#[inline(never)]` 防止编译器把它优化没了,保住基准的测量对象。

而 Z-order 布局对**并行**还有第二重红利——它和四分分治同构:

> [rayon-demo/src/matmul/mod.rs:L123-L139](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/matmul/mod.rs#L123-L139) —— `quarter_chunks` 对一维切片连做两次 `split_at` 得到四等分。在 Z-order 布局下,这四段恰好是矩阵的四个象限(交织位的高位决定象限):**子矩阵 = 连续内存段**,`split_at` 之后每个任务拿到的还是一块连续内存,缓存行为不因切分而恶化。行主序布局切出的"子矩阵"则是跨步切片,并行切分与缓存局部性互相打架——这就是 TODO 注释里 "more parallelizable" 的含义。

> [rayon-demo/src/matmul/mod.rs:L316-L338](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/matmul/mod.rs#L316-L338) —— `rmatsum` / `rmatsub` 用 `par_iter_mut().zip(par_iter())` 并行逐元素加减(u3-l3 的 zip 在中点对齐切分),`rcopy` 小片直接 `copy_from_slice`、大片 `rayon::join` 递归对半——布局无关的辅助运算也不忘并行。

**nbody:双缓冲与嵌套并行的分岔口。**

> [rayon-demo/src/nbody/nbody.rs:L39-L49](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/nbody/nbody.rs#L39-L49) —— `NBodyBenchmark` 持有 `bodies: (Vec<Body>, Vec<Body>)` 两个等长数组;`Body` 是含 7 个 f64 的 `Copy` 结构体(position + velocity + velocity2),结构体数组(AoS)布局让一个 body 的全部字段共享缓存行,内层循环读完位置读速度,局部性良好。

> [rayon-demo/src/nbody/nbody.rs:L92-L115](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/nbody/nbody.rs#L92-L115) —— `tick_par` 用 `self.time & 1` 选择方向:偶数 tick 读 bodies.0 写 bodies.1,奇数反之;外层 `par_iter_mut().zip(&in_bodies[..])` 并行,每支任务读**上一帧的稳定快照**、写**自己独占的输出槽**,无锁也无伪共享(不同任务写不同区段)。这就是双缓冲:读侧永远是完整的旧帧,下一帧再互换角色。

两种 tick 的差异全部集中在内层力计算:

> [rayon-demo/src/nbody/nbody.rs:L243-L295](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/nbody/nbody.rs#L243-L295) —— `tick_par` 调用的 `next_velocity`:对全部 bodies 做一次**串行** `iter().fold`,累加邻居的斥/吸/对齐力。外层 4000 个 body 各自是一个并行任务,每个任务内部串行扫 4000 个邻居——任务粒度适中,内层顺序扫数组缓存友好。(源码 TODO 也自问要不要把这层也并行化。)

> [rayon-demo/src/nbody/nbody.rs:L379-L438](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/nbody/nbody.rs#L379-L438) —— `tick_par_reduce` 调用的 `next_velocity_par` 把内层换成 `par_iter().fold(...).reduce(...)`:fold 让每个子任务各自累加 `(diff, diff2)`,reduce 再把部分和加起来(u3-l2 的 fold/reduce 组合)。这是**嵌套并行**——外层任务的执行线程内部再铺一层并行迭代,总任务数从 4000 涨到千万级。它演示了"能并行"不等于"更快":更细的任务带来更多切分与归并,还让两层数据划分互相干扰;demo 把两种模式并排计时,正是留给读者实测对比的实验位。

计时与 speedup 打印在 nbody 入口:

> [rayon-demo/src/nbody/mod.rs:L79-L142](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/nbody/mod.rs#L79-L142) —— `run_benchmarks` 依次跑 par、parreduce、seq 三种模式,各自新建同种子输入、跑 `ticks` 轮后 `Instant` 计时,最后 `speedup = seq_time / par_time` 逐项打印。`--mode` 可单选一种,`--bodies` / `--ticks` 控制规模。

**mergesort demo:阈值是调出来的。**

> [rayon-demo/src/mergesort/mod.rs:L43-L45](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/mergesort/mod.rs#L43-L45) —— `SORT_CHUNK = 32 * 1024`、`MERGE_CHUNK = 64 * 1024`,注释原话 "Values from manual tuning gigasort on one machine":不是理论推导,是一台机器上实测调出来的。对照 u8-l2 讲过的库内排序三阈值(20 / 2000 / 5000,demo 的 u32 元素更小、工作更廉价,所以阈值更大),同一规律:**单位工作量越小,串行下限阈值越大**。

> [rayon-demo/src/mergesort/mod.rs:L65-L79](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/mergesort/mod.rs#L65-L79) —— `rsort` 递归:小于 `SORT_CHUNK` 落到 `src.sort()`(标准库串行排序)——递归基就是粒度阈值;否则对半 `join` 递归排序进缓冲区,再 `rmerge` 归并回去。与 4.2 节 quicksort 的 `5 * 1024` 一样,join 路径的粒度全靠递归函数里的这一行判断。

#### 4.3.4 代码实践

**实践目标**:用 nbody 基准实测「外层并行 + 内层串行」(par)与「嵌套并行」(parreduce)的性能差异,体会粒度下探的代价。

**操作步骤**:

```bash
# 全模式对比(默认 bodies=4000, ticks=100)
cargo run --release -p rayon-demo -- nbody bench

# 只看两种并行模式的耗时
cargo run --release -p rayon-demo -- nbody bench --mode parreduce
cargo run --release -p rayon-demo -- nbody bench --mode par

# 缩小规模排除内存带宽干扰,再放大观察趋势
cargo run --release -p rayon-demo -- nbody bench --bodies 1000 --ticks 200
cargo run --release -p rayon-demo -- nbody bench --bodies 20000 --ticks 50
```

**需要观察的现象**:

1. 输出三行耗时(par / parreduce / seq)与两行 speedup。
2. 对比 `ParReduce speedup` 与 `Parallel speedup`:多数机器上 parreduce(嵌套并行)**不高于**甚至低于 par——内层再并行意味着外层每个任务切出的子任务要去和其它外层任务的子任务抢线程,归并开销也随之上升。
3. bodies 增大后,内层扫描变长,两种模式的差距可能缩小(内层工作量变大,切分开销占比下降)。
4. 具体结论**待本地验证**。

**预期结果**:par 模式以「外层 4000 个中等粒度任务」覆盖全部线程,负载已够均衡;parreduce 的额外切分层次买不到并行度,只买到开销。这印证本讲主线:**切分要恰好铺满线程为止,多一刀都是浪费**——自适应 thief-splitting 的默认行为正是按这个原则设计的(4.1 节)。

#### 4.3.5 小练习与答案

**练习 1**:nbody 为什么用 `(Vec<Body>, Vec<Body>)` 双缓冲,而不是单数组就地更新或加锁?

**答案**:模拟第 t 帧时,每个 body 的新速度取决于**上一帧**其它 body 的位置。单数组就地更新会让"新值"污染"旧值",结果依赖更新顺序(且无法并行);加锁则扼杀并行。双缓冲读旧写新,读侧是稳定快照,写侧各任务写互不重叠的输出槽,不需要任何同步,代价只是翻倍内存与一次克隆。

**练习 2**:`Body` 用结构体数组(AoS,`Vec<Body>`)而不是三个平行数组(SoA,`Vec<Vector3>` × 3),对这个程序的缓存行为意味着什么?

**答案**:内层力循环对每个 body 主要读 `position`(偶尔读 `velocity`/`velocity2`)。AoS 布局下一个 body 的 7 个 f64(56 字节)基本占满一条缓存行,顺序扫描时每一行都物尽其用,且坐标与速度同行命中。若计算只频繁用其中一个字段,SoA 让扫描更紧凑、 SIMD 更友好——布局优劣取决于实际访存模式,没有普适答案。本程序三字段都用,AoS 是合理选择。

**练习 3**:matmul 的 Z-order 布局让 `quarter_chunks` 的四等分恰好是四个象限。若用行主序布局做同样的四分,子矩阵在内存中是什么形状?

**答案**:行主序下整个矩阵按行连排,四等分得到的是「上半两半的行」与「下半两半的行」——横向切是连续的,纵向切(左右象限)则变成跨步访问:左上象限的元素彼此相隔一整行宽度。子任务拿到的是跨步切片而非连续内存,缓存行按整块加载却只用一小部分,任务越多浪费越大。Z-order 把二维邻近性编码进一维下标,切分与局部性不再冲突。

## 5. 综合实践

**综合任务:为一亿 u32 的求和程序做一次完整的粒度调优,并写出你的"调优报告"。**

在示例工程中新建(示例代码,非仓库原有):

```rust
use rayon::prelude::*;
use std::time::Instant;

fn main() {
    let n: usize = 100_000_000;
    // 预热线程池:一次不计时的求和,触发全局 Registry 初始化(u5-l3)
    let _ = (0..n).into_par_iter().sum::<u64>();

    let threads = std::env::var("RAYON_NUM_THREADS")
        .ok().and_then(|s| s.parse().ok())
        .unwrap_or(rayon::current_num_threads());
    println!("threads = {threads}");

    let expected = n as u64 * (n - 1) as u64 / 2;
    let mut rows: Vec<(&str, u128)> = Vec::new();

    // 每种配置:标签 + 闭包,计时后断言结果一致(粒度旋钮不许碰结果)
    let mut run = |label: &str, f: impl FnOnce() -> u64| {
        let start = Instant::now();
        assert_eq!(f(), expected);
        rows.push((label, start.elapsed().as_nanos()));
    };

    run("default", || (0..n).into_par_iter().sum::<u64>());
    run("min_len=1024", || {
        (0..n).into_par_iter().with_min_len(1024).sum::<u64>()
    });
    run("max_len=1024", || {
        (0..n).into_par_iter().with_max_len(1024).sum::<u64>()
    });
    run("max_len=65536", || {
        (0..n).into_par_iter().with_max_len(65536).sum::<u64>()
    });

    for (label, ns) in rows {
        println!("{label:<16} {ns:>12} ns");
    }
}
```

要求完成:

1. **正确性锚点**:每种配置都断言与 \( \sum_{i=0}^{n-1} i = n(n-1)/2 \) 相等——粒度旋钮不许碰结果。
2. **粒度扫描**:补齐 `with_max_len` 从 16 到 2²⁴ 的若干档位(或反向用 `with_min_len`),用 `--release` 运行,记录耗时。
3. **线程数扫描**:外层套 `RAYON_NUM_THREADS=1,2,4,...,N` 重复第 2 步,观察最优粒度是否随线程数移动。
4. **解释曲线**:用本讲的三重框架解释你的数据——(a) 切分开销 vs 并行度不足的 U 形;(b) 一亿次 u64 求和是内存带宽受限负载,线程数增大后加速比应远低于线性(与 matmul 这类计算受限负载对照);(c) `RAYON_NUM_THREADS=1` 与最优档的差值即纯调度开销估计。
5. **对照官方微基准**:阅读 [rayon-demo/src/join_microbench.rs:L6-L56](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/join_microbench.rs#L6-L56),指出你的实验与它的 `increment_all` 系列在设计上的对应关系(它用 `par_iter_mut` 写内存、你用范围求和读内存,访存方向不同,饱和行为也不同)。

**验收标准**:一份含数据表、曲线趋势描述、三条以上归因分析的简短报告;所有断言通过;结论明确回答"这台机器上这个负载的最优粒度与饱和线程数大约是多少"。

## 6. 本讲小结

- `with_min_len` / `with_max_len` 是 `bridge` 递归里粒度裁决的两个旋钮:`min` 经 `len / 2 >= min` 成为硬下界,`max` 经 `min_splits = len / max` 抬高初始切分预算;二者只影响性能、不影响结果,且只在 thief-splitting 自适应失准(元素工作量过轻或过重)时才值得手动设置。
- 自适应策略的核心是窃取重置:任务被偷过就把切分预算重置回线程数——切到刚好铺满 CPU 为止,多一刀都是浪费。
- rayon-demo 提供两类基准:八个可 `cargo run` 的 demo(matmul、quicksort、nbody 等,自带计时与 speedup)与十个 `#[cfg(test)]` 仅 bench 模块(fibonacci、join_microbench 等,需 nightly `cargo bench`)。quicksort 的 `Joiner` trait 双实现是控制变量法的范本:同一算法、唯一变量是并行。
- 加速比曲线必然弯折,归因按四类排查:内存带宽饱和、关键路径(\( T \approx W/P + O(S) \))、非并行段(Amdahl)、调度开销;`RAYON_NUM_THREADS=1` 与串行版的差值是纯调度开销的直观估计。
- 缓存与布局是并行性能的隐形上限:matmul 用 Z-order 位交织让四分分治切出的每个象限都是连续内存;nbody 用双缓冲免锁读写分离;`tick_par`(外层并行)与 `tick_par_reduce`(嵌套并行)的对比说明任务切得更细未必更快。
- 阈值没有公式:mergesort demo 的 `SORT_CHUNK`/`MERGE_CHUNK` 注释明说是一台机器上手调出来的;单位元素工作量越小,串行阈值应越大(quicksort 512、demo 归并 32768、库内排序 2000)。

## 7. 下一步学习建议

- **u9-l4(测试体系与平台可移植性)**:性能改完要防回归——学习本仓库单元测试、集成测试、compile_fail 三层测试如何守住并行代码,以及 wasm 等平台的编译策略。
- **精读 `rayon-demo/src/pythagoras/mod.rs`**:它用 `with_min_len(usize::MAX)` 定义"串行"闭包、`with_max_len(1)` 定义"并行"闭包,把粒度旋钮当实验变量用,是 4.1 节的进阶样本。
- **对比 matmul 的 `par strassen` 与 `par z-order`**:Strassen 用 7 次子矩阵乘法替代 8 次,渐进乘法量更低但额外加减与内存往返更多;在你的机器上测两者交叉点,体会"算法改进与常数开销"的取舍。
- **回顾 u5-l4 / u5-l5**:本地队列 LIFO(保热度)与窃取 FIFO(偷最旧)的方向差异、睡眠唤醒的定量决策,是本讲所有"自适应"行为的调度底座;带着本讲的测量数据重读,理解会闭合。
- 若要系统性对比 Rayon 与其他方案,可在同一负载上对照 std::thread 手写分块、std 迭代器串行版,复用本讲的控制变量法。
