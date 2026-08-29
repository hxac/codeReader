# 并行归并排序

## 1. 本讲目标

本讲深入 `src/slice/sort.rs` 这份 1692 行的排序实现,搞清楚 `v.par_sort()` 这一个调用背后发生了什么。学完本讲,你应该能够:

1. 说出 `par_mergesort` 的三阶段流程:分块排序 → 自然游程拼接 → 并行归并,以及 `MAX_INSERTION = 20`、`CHUNK_LENGTH = 2000`、`MAX_SEQUENTIAL = 5000` 三个阈值各自的分工。
2. 理解「两个有序序列如何并行归并」这个本讲最核心的算法问题:`split_for_merge` 用二分查找找切分点,保证左右两半可以各自独立归并、输出恰好拼满目标区间。
3. 掌握缓冲区管理方案:一块与原切片等长的临时内存如何被所有任务共享,`into_buf` 布尔翻转如何让递归各层在 `v` 与 `buf` 之间乒乓而不需要第二块缓冲。
4. 理解稳定性从哪里来(两处「相等取左」),以及 panic 安全如何由三组 Drop 守卫(`InsertionHole`、`MergeHole`、`State`)保证——比较函数在任意一次调用中 panic,切片仍然不多不少地持有原来那些元素。
5. 会用 rayon-demo 的 mergesort 基准测量并行加速比,并在自己拷贝的代码上做阈值调参实验。

一个先说清的事实:任务规格中提到的「溢出空间(out_of_buf)」是早期版本的机制。在当前 HEAD 中用 `git log -S "out_of_buf"` 检索不到任何记录,该标识符已不存在;当前实现以「单块全量缓冲 + `into_buf` 翻转」完成同样的职责。本讲以真实源码为准讲解后者。

## 2. 前置知识

### 2.1 稳定排序与不稳定排序

- **稳定排序**:两个「相等」的元素(比较函数认为互不小于对方)排序后保持原有相对顺序。对数值无所谓,但对 `(key, payload)` 这样的数据很重要——先按分数稳定排序,相同分数的学生保持按学号排列。
- **不稳定排序**:相等元素的相对顺序可能被打乱,通常因此可以换来实现上的便利(原地操作、零分配)。

Rayon 提供两套:`par_sort` 系列走稳定归并排序(分配 \( O(n) \) 临时内存),`par_sort_unstable` 系列走 pattern-defeating quicksort(原地、零分配)。模块文档在 [src/slice/sort.rs:L1-L15](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/sort.rs#L1-L15) 说明了来源:这份实现大部分从 `core::slice::sort` 复制而来,仅做了最小改动以支持 stable Rust 和**可并行调用的比较函数**——标准库的比较器是 `FnMut`,而并行排序里同一比较器要被多个线程同时持有,必须放宽为 `Fn` 并追加 `Sync` 约束。

### 2.2 归并排序与自然游程

归并排序的分治结构:把序列分成两半,各自排好,再**归并**(merge)两个有序序列。归并本身是线性扫描:每次取两边头部中较小者。若两个相等取左边,结果就是稳定的。

**自然游程(natural run)**是输入中本来就有序的连续片段。TimSort 类算法先扫描出这些游程(升序游程直接用,严格降序游程反转),再逐层归并。对「几乎排好序」或「几段有序序列拼接」的输入,这类算法接近线性时间。`par_mergesort` 的第二阶段正是利用块级游程信息减少归并工作量。

### 2.3 你需要带上的旧知识

- **`join` 的工作窃取语义**(u5-l1):`join(a, b)` 把 b 入队供窃取、当前线程先执行 a。本讲中 `par_merge` 与 `merge_recurse` 的并行递归全部建立在 `rayon_core::join` 上。
- **`with_max_len` 粒度控制**(u3-l3):`par_chunks_mut(CHUNK_LENGTH).with_max_len(1)` 表示「每块一个任务、不要再切」。分块排序阶段就用这一招。
- **`SendPtr`**(u4-l4 collect 已见过):裸指针 `*mut T` 不是 `Send`,要跨线程传必须包一个 newtype 手动声明,见 [src/lib.rs:L128-L134](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/lib.rs#L128-L134)。
- **`MaybeUninit` 与浅拷贝**:归并过程中元素被 `ptr::copy_nonoverlapping` 按位搬运,这些副本是「未经初始化语义的位像」,不能让 Rust 自动为它们运行析构。后文会看到 `Vec` 保持长度为 0、`ManuallyDrop`、`mem::forget` 三种手法都服务于这一点。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [src/slice/sort.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/sort.rs) | 排序算法本体。L1274 有一行分隔注释:上面抄自 `core::slice::sort`(串行算法),下面是 rayon 自己写的并行化。 |
| [src/slice/mod.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/mod.rs) | `ParallelSliceMut` trait,定义 `par_sort`/`par_sort_by`/`par_sort_unstable` 等公开入口,各自转调 `par_mergesort` 或 `par_quicksort`。 |
| [src/slice/test.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/test.rs) | 排序正确性与稳定性测试:`sort!` 宏覆盖多种输入形态,`test_par_sort_stability` 专门验证稳定性。 |
| [rayon-demo/src/mergesort/mod.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/mergesort/mod.rs) | 独立的演示版并行归并排序(教学标本),带 seq/par 对比计时,阈值与主库不同。 |
| [rayon-demo/src/sort.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/sort.rs) | 主库 `par_sort` 系列的 bench 集合:升序/降序/近排序/随机/大元素/字符串等输入形态。 |
| [tests/sort-panic-safe.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/tests/sort-panic-safe.rs) | panic 安全的压力测试(u6-l4 已精读,本讲引用其结论)。 |

sort.rs 内部导览(自上而下):

```
L27   InsertionHole        插入排序的回填守卫
L45   insert_tail / insert_head / insertion_sort_shift_left
L264  heapsort            pdqsort 的兜底(保证最坏 O(n log n))
L829  recurse / par_quicksort   不稳定排序的并行递归
L956  merge               串行归并(复制较短一侧进 buf)
L1055 MergeHole           归并的回填守卫
L1074 MergeSortResult     块级排序结果三态
L1100 merge_sort          串行 TimSort 风格分块排序
L1240 find_streak         识别升/降序自然游程
────── L1274 分隔线:以上抄自 core,以下为 rayon 并行化 ──────
L1283 split_for_merge     为并行归并找切分点(二分)
L1333 par_merge           并行归并(本讲核心)
L1437 merge_recurse       递归归并块列表(into_buf 翻转)
L1514 par_mergesort       总控入口
```

## 4. 核心概念与源码讲解

### 4.1 递归拆分与合并

#### 4.1.1 概念说明

并行化一个 \( O(n \log n) \) 的排序,难点不在「把排序拆成两半并行」——那只需 `join(|| sort(left), || sort(right))`。真正的难点有两个:

1. **切分粒度**:递归到多小才停下来交给串行算法?切得太细,任务调度开销(`join` 入队、窃取、缓存失效)吞掉收益;切得太粗,线程喂不饱。
2. **归并本身怎么并行**:两个各自有序的序列合并成一个,朴素归并是严格串行的依赖链——下一个输出取决于上一次比较。如果归并串行,即使排序阶段完美并行,关键路径仍被顶层那次 \( O(n) \) 归并主导,加速比被 Amdahl 定律锁死:

\[ S(n) \le \frac{1}{f + (1-f)/p}, \quad f = \text{串行归并占比} \]

rayon 的答案是把归并也做成分治:`split_for_merge` 在两个有序序列上各找一个切分点,使**左半合并的结果恰好占据目标区间的前缀**,于是左右两半可以递归地并行归并。demo 的头注释([rayon-demo/src/mergesort/mod.rs:L8-L11](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/mergesort/mod.rs#L8-L11))总结了这一方案的复杂度:总时间 \( O(n \log n) \)、额外空间 \( O(n) \)、**关键路径 \( O(\log^3 n) \)**——分块排序深度 \( \log(n/c) \)、归并递归深度 \( \log(\text{块数}) \)、每层 `par_merge` 内部再递归 \( \log \) 层。

#### 4.1.2 核心流程

`par_mergesort` 的总控流程(对应源码 [src/slice/sort.rs:L1514-L1612](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/sort.rs#L1514-L1612)):

```text
par_mergesort(v):
  若 size_of::<T>() == 0: 返回            # 零大小类型无排序意义
  若 len <= 20 (MAX_INSERTION):           # 极短切片
      insertion_sort_shift_left(v)        # 原地插入排序,零分配
      返回
  buf = 分配 len 容量的未初始化内存        # 保持 Vec 长度为 0(见 4.2)
  若 len <= 2000 (CHUNK_LENGTH):
      res = merge_sort(v, buf)            # 串行 TimSort 一次完成
      若 res == Descending: v.reverse()
      返回
  # 阶段一:分块并行排序
  v.par_chunks_mut(2000).with_max_len(1)  # 每块恰好一个任务
     .enumerate().map(|(i, chunk)| {
         merge_sort(chunk, buf 对应区段)  # 串行 TimSort,返回三态结果
     }).collect::<Vec<(l, r, res)>>()
  # 阶段二:拼接相邻同向游程(降序块此时 reverse)
  chunks = 拼接后的区间列表 [(a, b), ...]
  # 阶段三:并行归并
  merge_recurse(v, buf, chunks, into_buf=false)
```

`par_merge` 的并行归并流程(对应 [L1333-L1423](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/sort.rs#L1333-L1423)):

```text
par_merge(left, right, dest):
  若任一侧为空 或 总长 < 5000 (MAX_SEQUENTIAL):
      串行归并,相等取左(稳定)
  否则:
      (left_mid, right_mid) = split_for_merge(left, right)  # 二分找切分点
      把 left、right 各自一切为二
      dest_l = dest 前缀(长度 = 左半之和)
      dest_r = dest.add(左半长度)
      join(|| par_merge(左半们, dest_l),
           || par_merge(右半们, dest_r))
```

#### 4.1.3 源码精读

**入口与三阶段**。先看公开入口如何转调(这里以 `par_sort` 为例):

[src/slice/mod.rs:L392-L397](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/mod.rs#L392-L397)——`ParallelSliceMut::par_sort` 只有一行:把比较函数定为 `T::lt`,交给 `par_mergesort`。`par_sort_by`([L453-L459](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/mod.rs#L453-L459))把 `Ordering` 压平成布尔;`par_sort_unstable`([L631-L635](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/mod.rs#L631-L635))则换到 `par_quicksort`。官方文档在 [L375-L380](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/mod.rs#L375-L380) 概括了三阶段:「先分成小块并行排序,把能拼成非降序/降序游程的相邻块拼接,最后用块的并行细分与并行归并合并」。

**三个阈值**(全部用 `const` 定义在函数体内,作用域即文档):

[src/slice/sort.rs:L1519-L1525](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/sort.rs#L1519-L1525)——`MAX_INSERTION = 20`(更短的切片直接插入排序,省掉分配)与 `CHUNK_LENGTH = 2000`(初始块长,注释说明取「让 Rayon 任务调度开销仍可忽略的最小值」)。而 [L1339-L1343](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/sort.rs#L1339-L1343) 的 `MAX_SEQUENTIAL = 5000` 刻意比 `CHUNK_LENGTH` 大:注释解释「归并比归并排序快,需要更粗的粒度才能摊平调度开销」。三个数字的关系:20(插入排序上限)< 2000(排序块宽)< 5000(归并任务下限)。

**阶段一:分块并行排序**。

[src/slice/sort.rs:L1559-L1579](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/sort.rs#L1559-L1579):

```rust
v.par_chunks_mut(CHUNK_LENGTH)
    .with_max_len(1)
    .enumerate()
    .map(move |(i, chunk)| {
        let l = CHUNK_LENGTH * i;
        let r = l + chunk.len();
        unsafe {
            let buf = buf.get().add(l);
            (l, r, merge_sort(chunk, buf, is_less))
        }
    })
    .collect::<Vec<_>>()
    .into_iter()
    .peekable()
```

这段浓缩了前面几讲的所有知识:`par_chunks_mut` 是 u8-l1 讲过的分块视图;`with_max_len(1)` 是 u3-l3 的粒度控制——块宽 2000 已是合理粒度,`with_max_len(1)` 明确禁止 `bridge` 再把这些块二分;`map` 里干的是重活(每块一次完整串行 TimSort),`collect` 作为消费者触发执行;块与 `buf` 的对应关系靠 `CHUNK_LENGTH * i` 手工对齐(第 i 块借用 `buf[l..r]` 区段,与 u4-l4 collect 的分段直写同构);`buf` 与 `is_less` 都先包进 `SendPtr`/引用才能进 `move` 闭包。每块返回 `(l, r, MergeSortResult)` 三元组——区间坐标加三态结果,供阶段二使用。

**阶段二:拼接自然游程**。

[src/slice/sort.rs:L1584-L1605](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/sort.rs#L1584-L1605)——遍历块结果列表,当某块未被真正排序(结果是 `NonDescending` 或 `Descending`,即天然有序)且下一块同类型、边界处方向延续时,合并它们的区间;`Descending` 块在此处整体 `reverse`。`MergeSortResult` 的三态定义在 [L1074-L1081](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/sort.rs#L1074-L1081):`NonDescending`(本来就升序,原样保留)、`Descending`(本来就降序,先不动、留给上层反转)、`Sorted`(真排过了)。这是典型的「自适应排序」:输入越接近有序,需要归并的块越少。

**split_for_merge:并行归并的钥匙**。

[src/slice/sort.rs:L1283-L1323](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/sort.rs#L1283-L1323):

```rust
if left_len >= right_len {
    let left_mid = left_len / 2;
    // 在 right 中二分查找第一个不小于 left[left_mid] 的位置
    let mut a = 0;
    let mut b = right_len;
    while a < b {
        let m = a + (b - a) / 2;
        if is_less(&right[m], &left[left_mid]) {
            a = m + 1;
        } else {
            b = m;
        }
    }
    (left_mid, a)
}
```

它在较长一侧取中点,在较短一侧二分定位,返回 `(a, b)` 满足(docstring 见 [L1279-L1282](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/sort.rs#L1279-L1282)):`left[..a]` 与 `right[..b]` 的全部元素 ≤ `left[a..]` 与 `right[b..]` 的全部元素。于是:

\[ \text{merge}(left[..a], right[..b]) \text{ 的输出长度恰为 } a+b, \text{占据 } dest[..a+b] \]

左右两半互不重叠、无缝拼接——归并的并行分治由此成立,且**相等时切分点偏向使左侧元素进左半**,这是稳定性的第一块拼图。测试 [L1656-L1691](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/sort.rs#L1656-L1691)(`test_split_for_merge`)用随机数据断言了这个偏序性质。代价是每次切分花 \( O(\log n) \) 次比较,这正是 `MAX_SEQUENTIAL = 5000` 要摊薄的开销之一。

**par_merge 主体**。

[src/slice/sort.rs:L1364-L1394](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/sort.rs#L1364-L1394):

```rust
if left_len == 0 || right_len == 0 || left_len + right_len < MAX_SEQUENTIAL {
    while s.left_start < s.left_end && s.right_start < s.right_end {
        // 取较小一侧;相等取左,保持稳定性
        let is_l = is_less(&*s.right_start, &*s.left_start);
        let to_copy = if is_l { s.right_start } else { s.left_start };
        ptr::copy_nonoverlapping(to_copy, s.dest, 1);
        ...
    }
} else {
    let (left_mid, right_mid) = split_for_merge(left, right, is_less);
    let (left_l, left_r) = left.split_at_mut(left_mid);
    let (right_l, right_r) = right.split_at_mut(right_mid);
    mem::forget(s);
    let dest_l = SendPtr(dest);
    let dest_r = SendPtr(dest.add(left_l.len() + right_l.len()));
    rayon_core::join(
        move || par_merge(left_l, right_l, dest_l.get(), is_less),
        move || par_merge(left_r, right_r, dest_r.get(), is_less),
    );
}
```

四个要点:

1. 串行分支里 `is_l = is_less(right, left)` 为假(即相等)时取 `left`——「相等取左」是稳定性的第二块拼图(第一块在 `split_for_merge`)。
2. 并行分支中 `dest_r = dest.add(left_l.len() + right_l.len())` 由切分点直接算出,左右输出段精确拼接,无需任何同步。
3. `mem::forget(s)` 不是随手一忘:`s` 是下面要讲的 panic 守卫,进入并行分支后它的职责移交给两个递归调用各自的新守卫,若不 forget,外层守卫 Drop 时会把已被子任务写过的区间再写一遍。
4. `join` 的两个闭包各自 `move` 走一半切片与一个 `SendPtr`——所有权一分为二,类型系统因此能证明两线程不写同一内存。`split_at_mut` 正是 u8-l1 讲过的「分割写权限」。

#### 4.1.4 代码实践

**实践 A:测量 par_sort 的加速比(可直接运行)**。

```bash
# 在仓库根目录
cargo run -p rayon-demo --release -- mergesort bench --size 1000000
```

`mergesort::main`([rayon-demo/src/mergesort/mod.rs:L257-L268](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/mergesort/mod.rs#L257-L268))会分别用 `seq_merge_sort` 与并行 `merge_sort` 排同一份随机数据并打印 `speedup: N.Nx`,计时逻辑在 [L242-L255](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/mergesort/mod.rs#L242-L255)(计时后还会 `is_sorted` 断言正确性)。注意这个 demo 基准是**独立的演示版归并排序**,不是主库的 `par_sort`,它俩的实现对照见 4.2.4。

1. 实践目标:得到一台机器上的真实加速比,并观察它与核数的关系。
2. 操作步骤:依次用 `--size 1000000`、`--size 10000000` 运行;再用 `RAYON_NUM_THREADS=1 cargo run -p rayon-demo --release -- mergesort bench --size 1000000` 强制单线程跑一次。
3. 需要观察的现象:小规模时 speedup 偏低(任务数不足以喂满线程);单线程时并行版与串行版耗时接近(调度开销的净影响)。
4. 预期结果:数千万规模、8 核左右机器上通常能看到数倍的加速比;具体数值**待本地验证**(取决于机器)。

**实践 B:验证 par_sort 与 sort 的一致性与稳定性(示例代码,新建独立 Cargo 工程)**。

```rust
// 示例代码:练习工程 src/main.rs,Cargo.toml 中加 rayon = "1"
use rayon::prelude::*;
use std::time::Instant;

fn main() {
    let n = 1_000_000;
    let base: Vec<u32> = (0..n).map(|_| fastrand::u32(..)).collect();

    let mut a = base.clone();
    let t0 = Instant::now();
    a.sort();
    let t_seq = t0.elapsed();

    let mut b = base.clone();
    let t1 = Instant::now();
    b.par_sort();
    let t_par = t1.elapsed();

    assert_eq!(a, b); // 结果必须与标准库一致
    println!("std sort: {:?}  par_sort: {:?}  speedup: {:.2}x",
             t_seq, t_par, t_seq.as_secs_f64() / t_par.as_secs_f64());

    // 稳定性:对 (key, occurrence) 元组只按 key 排序
    let mut pairs: Vec<(u8, usize)> = Vec::new();
    let mut counts = [0usize; 10];
    for _ in 0..100_000 {
        let k = fastrand::u8(0..10);
        counts[k as usize] += 1;
        pairs.push((k, counts[k as usize]));
    }
    pairs.par_sort_by(|a, b| a.0.cmp(&b.0));
    assert!(pairs.is_sorted()); // 相同 key 的出现序号必须递增 → 稳定
    println!("stable: OK");
}
```

计时前建议各跑一遍预热(线程池首次启动的耗时,见 u1-l3 的结论),并始终用 `--release`。

#### 4.1.5 小练习与答案

**练习 1**:为什么 `MAX_SEQUENTIAL`(5000)要刻意大于 `CHUNK_LENGTH`(2000)?

答案:`par_merge` 每次并行切分要调用 `split_for_merge`,花费 \( O(\log n) \) 次额外比较,加上一次 `join` 的任务调度开销;而归并本身是线性且内存友好的操作,单位工作量比排序便宜。粒度阈值的意义是「任务开销 / 单位工作量 ≈ 可忽略」,分母越小(归并),阈值就该越大,源码注释([L1339-L1342](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/sort.rs#L1339-L1342))原话是「merging is faster than merge sorting, so merging needs a bit coarser granularity」。

**练习 2**:`par_chunks_mut(CHUNK_LENGTH).with_max_len(1)` 若去掉 `with_max_len(1)`,程序还正确吗?性能会怎么变?

答案:仍然正确——`with_max_len` 只影响切分粒度、不影响结果(u3-l3 的结论)。性能上,块可能被 `bridge` 再对半切,使某些任务只排几百个元素,插入/调度开销占比上升;同时块与 `buf` 的区段对应仍按 `CHUNK_LENGTH * i` 计算,不影响正确性,只是浪费了 TimSort 的块宽选择。极端情况下(块被切到 20 以下)还会落入插入排序路径。

**练习 3**:用 `split_for_merge` 的性质说明:为什么 `par_merge` 的两个递归分支写 `dest` 时不需要任何原子变量或锁?

答案:`split_for_merge` 返回的 `(a, b)` 保证左半全部元素 ≤ 右半全部元素,因此左半的归并输出恰好是最终结果的第 `0..a+b` 个元素,右半输出恰好是第 `a+b..` 个;`dest_r = dest.add(a + b)` 精确衔接,两个区间不重叠。写位置的确定性来自切分点的偏序性质,而不是运行期协调——这是「用算法证明换掉同步开销」的典型手法,与 u4-l4 collect 的「位置由切分区间决定」一脉相承。

### 4.2 缓冲区管理

#### 4.2.1 概念说明

稳定归并排序需要临时搬走一部分元素才能腾位归并。rayon 的方案是**一块与原切片等长的缓冲区,全流程共用**:

- 分配:`Vec::<T>::with_capacity(len)`,且**永远不让它的长度增长**;
- 借用:每个任务只通过裸指针使用属于自己的 `[l, r)` 区段(阶段一)或翻转目的地(阶段三);
- 回转:`merge_recurse` 用 `into_buf` 布尔值在递归层间翻转数据的「家」:`v` 的内容归并进 `buf`,下一层又从 `buf` 归并回 `v`,乒乓往复,顶层(由 `par_mergesort` 以 `into_buf=false` 调用)保证最终结果落回 `v`。

「Vec 保持长度为 0」是整个缓冲区管理中最精妙的一笔:[src/slice/sort.rs:L1542-L1546](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/sort.rs#L1542-L1546) 的注释写明动机——buf 里只存 `v` 内容物的**浅拷贝**(shallow copies),元素所有权始终在 `v`;若让 Vec 长度非零,`is_less` panic 引发栈展开时,这些浅拷贝会被重复析构,直接造成未定义行为。长度为 0 的 Vec 在 Drop 时什么也不析构,panic 安全因此免费获得。

#### 4.2.2 核心流程

`merge_recurse` 的协议(输入是「相邻且按序覆盖全片的区间列表」):

```text
merge_recurse(v, buf, chunks, into_buf):
  若 chunks 只剩 1 个:
      若 into_buf: 把该区间从 v 拷到 buf     # 基例:只是搬运
      返回
  (start, _)   = chunks[0]
  (mid,   _)   = chunks[len/2]
  (_,   end)   = chunks[len-1]
  (left, right) = chunks 在 len/2 处一分为二
  (src, dest) = into_buf ? (v, buf) : (buf, v)   # 本层从 src 读、往 dest 写
  guard = MergeHole { src[start..end] → dest[start] }   # panic 守卫
  join(|| merge_recurse(...left,  !into_buf),
       || merge_recurse(...right, !into_buf))
  forget(guard)                    # 子递归成功,撤销守卫
  par_merge(src[start..mid], src[mid..end], dest+start)  # 本层归并
```

要点:区间的**切分发生在块列表上**(按块数取半),而非按元素取半——保证每次 `par_merge` 拿到的都是完整的块;`into_buf` 每层取反,数据因此在两个缓冲区间摆动;每层的实际归并在两个子递归**之后**执行(后序),因为要先有左右两半各自有序的产物。

#### 4.2.3 源码精读

**分配与短路径**。

[src/slice/sort.rs:L1542-L1555](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/sort.rs#L1542-L1555)——分配 `Vec::<T>::with_capacity(len)` 后立刻 `buf.as_mut_ptr()` 拿裸指针,Vec 本体留在栈上(长度 0);若 `len <= CHUNK_LENGTH`,直接一次串行 `merge_sort(v, buf)` 完事,结果为 `Descending` 时整体反转。

**merge_recurse 主体**。

[src/slice/sort.rs:L1464-L1506](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/sort.rs#L1464-L1506):

```rust
// Split the chunks into two halves.
let (start, _) = chunks[0];
let (mid, _) = chunks[len / 2];
let (_, end) = chunks[len - 1];
let (left, right) = chunks.split_at(len / 2);
...
let (src, dest) = if into_buf { (v, buf) } else { (buf, v) };

let guard = MergeHole {
    start: src.add(start),
    end: src.add(end),
    dest: dest.add(start),
};

let v = SendPtr(v);
let buf = SendPtr(buf);
rayon_core::join(
    move || merge_recurse(v.get(), buf.get(), left, !into_buf, is_less),
    move || merge_recurse(v.get(), buf.get(), right, !into_buf, is_less),
);

// Everything went all right - recursive calls didn't panic.
// Forget the guard in order to prevent its destructor from running.
mem::forget(guard);

// Merge chunks `(start, mid)` and `(mid, end)` from `src` into `dest`.
let src_left = slice::from_raw_parts_mut(src.add(start), mid - start);
let src_right = slice::from_raw_parts_mut(src.add(mid), end - mid);
par_merge(src_left, src_right, dest.add(start), is_less);
```

注意 [L1470-L1477](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/sort.rs#L1470-L1477) 的注释专门解释了 `into_buf` 翻转:「递归调用每层翻转 `into_buf`……`par_merge` 第一层把块从 `buf` 归并进 `v`,第二层从 `v` 进 `buf`,如此往复」。`MergeHole` 守卫(结构体定义在 [L1055-L1069](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/sort.rs#L1055-L1069))Drop 时把 `src[start..end]` 原样拷进 `dest[start..]`——若子递归途中 panic,这一层的数据至少「完整搬家」,不会凭空消失(顺序可能不对,但元素一个不少)。

**串行 merge 只需半个缓冲**。

阶段一调用的串行 `merge_sort` 内部用的是另一个 `merge`([src/slice/sort.rs:L956-L1053](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/sort.rs#L956-L1053)),它的策略是**只复制较短的一侧进 buf**:

[src/slice/sort.rs:L985-L1016](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/sort.rs#L985-L1016):

```rust
if mid <= len - mid {
    // The left run is shorter.
    ptr::copy_nonoverlapping(v, buf, mid);
    hole = MergeHole {
        start: buf,
        end: buf.add(mid),
        dest: v,
    };
    // 从前往后:追踪 buf 中的左游程与 v[mid..] 的右游程,较小者写入 v 前段
```

较短一侧耗尽即结束;较长一侧若先耗尽,剩余部分已被 `MergeHole` 守卫兜住。因此 `merge_sort` 的 Safety 契约([L1097-L1099](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/sort.rs#L1097-L1099))只要求 `buf` 至少容下 `v.len() / 2`——`par_mergesort` 分配的整块 len 缓冲远超所需,同一块 buf 的「前 l 到 r 区段」借给每块的串行排序绰绰有余。这是「一份分配、两种用量」的复用。

**对照:demo 版的缓冲方案**。

rayon-demo 的演示版归并([rayon-demo/src/mergesort/mod.rs:L65-L97](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/mergesort/mod.rs#L65-L97))用 `Vec<MaybeUninit<T>>` + `set_len` 显式声明「未初始化内存」:

```rust
fn rsort<T: Ord + Send + Copy>(src: &mut [T], buf: &mut [MaybeUninit<T>]) {
    if src.len() <= SORT_CHUNK {
        src.sort();
        return;
    }
    let mid = src.len() / 2;
    let (sa, sb) = src.split_at_mut(mid);
    let (bufa, bufb) = buf.split_at_mut(mid);
    let (sorta, sortb) = rayon::join(|| rsort_into(sa, bufa), || rsort_into(sb, bufb));
    // 把两半从 buf 归并回 src
    rmerge(sorta, sortb, as_uninit_slice_mut(src));
}
```

`rsort`/`rsort_into` 的交替(rsort 排回 src、rsort_into 排进 dest)与主库 `merge_recurse` 的 `into_buf` 翻转是同一个乒乓模式;`rmerge`([L105-L125](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/mergesort/mod.rs#L105-L125))的「较长一侧取中点 + 较短一侧 `binary_search`」与主库 `split_for_merge` 同构。demo 限定 `T: Copy`、用 `MaybeUninit` 换安全;主库要支持任意 `T: Send`,只能用「长度为 0 的 Vec + 浅拷贝」这一更强的手法。两相对照,正好看出同一算法在「演示易读」与「生产通用」两种约束下的实现取舍。demo 的阈值 `SORT_CHUNK = 32 * 1024`、`MERGE_CHUNK = 64 * 1024`([L43-L45](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/mergesort/mod.rs#L43-L45),注释注明「在一台机器上手工调参」)比主库的 2000/5000 大一个数量级——阈值是经验参数,不是数学常数。

#### 4.2.4 代码实践

**实践 C:手工追踪缓冲区流动(纸笔练习)**。

1. 实践目标:彻底吃透 `into_buf` 翻转,能预测任意规模下数据最后是否回到 `v`。
2. 操作步骤:设 `n = 10000`、`CHUNK_LENGTH = 2000`,分块得到 5 个块。a) 写出阶段一结束后每块数据的位置(全部在 `v`,buf 只被借用过);b) 画出 `merge_recurse` 的递归树:5 个块切分成 2+3,再切 1+1 与 1+2,标出每一层的 `into_buf` 取值与 `par_merge` 的读写方向;c) 数一数同一区间最多被搬运几次。
3. 需要观察的现象:根层 `into_buf = false`(`src = buf, dest = v`),每个子递归收到 `!into_buf`,叶子层只做搬运或什么也不做;无论哪种切分,最外层归并总是写向 `v`。
4. 预期结果:每个元素最多经历 \( O(\log(\text{块数})) \) 次搬运,最终位置在 `v`。若你在纸上推不出「最终在 v」,重读 [L1470-L1477](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/sort.rs#L1470-L1477) 的注释再试。

**实践 D:阈值调参实验(在自己的拷贝上改,不动仓库源码)**。

1. 实践目标:体会 `CHUNK_LENGTH`/`MAX_SEQUENTIAL` 对性能的敏感性。
2. 操作步骤:把 [rayon-demo/src/mergesort/mod.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/mergesort/mod.rs) 整个文件复制到你的练习工程(demo 版是独立函数,`rayon::join` 即可编译;`crate::seeded_rng` 换成你自己的随机数),把 `SORT_CHUNK`/`MERGE_CHUNK` 改成 `const` 参数或在 `merge_sort` 加参数,取值 `{1024, 4096, 32768, 131072}` 交叉实验,`--size 10000000` 计时。
3. 需要观察的现象:阈值过小时耗时上升(任务过多、调度开销);过大时并行度不足、尾部串行归并变长。
4. 预期结果:存在一个较宽的最优平台区(这点与 demo 注释「手工调参」相符),具体最优值**待本地验证**。主库选 2000/5000 是保守偏小的取值——想想为什么库作者宁可略亏也要偏小(提示:库要服务任意 `T`,元素越大搬运越贵,块就应越小)。

#### 4.2.5 小练习与答案

**练习 1**:`par_mergesort` 分配了整块 len 的缓冲,而串行 `merge_sort` 声明只需 `v.len()/2`。为什么不分配一半以省内存?

答案:这块缓冲有两个用途。阶段一每块的串行 TimSort 只需「该块长的一半」确实够;但阶段三 `merge_recurse` 的 `into_buf` 翻转要求 `buf` 能容纳**整个切片**(某一层全体数据要住进 buf 再归并回 v)。整块 len 是被归并阶段的需求决定的,顺带覆盖了分块排序的需求。

**练习 2**:demo 版用 `Vec<MaybeUninit<T>>` + `set_len(n)`,主库用长度为 0 的 `Vec<T>` + 裸指针。两者的 panic 安全策略差在哪?

答案:demo 限定 `T: Copy`——浅拷贝与深拷贝无区别,`MaybeUninit` 明示「这块内存没有合法的 T,别析构」,Drop 时 Vec 直接丢弃未初始化内存,天然安全。主库支持有析构语义的任意 `T`:buf 中的位像是从 `v` **按字节复制**的浅拷贝,若 Vec 长度非零,panic 展开时会把这些位像当合法 T 析构,而真身还在 `v` 里,造成双重释放。长度恒为 0 的 Vec 在 Drop 时不析构任何元素,浅拷贝因此无害。

**练习 3**:`merge_recurse` 为什么在块列表(`chunks: &[(usize, usize)]`)的中点切分,而不是像 `bridge` 那样在元素中点切分?

答案:归并的单位是「已排序的块」。若按元素中点切,某个块会被劈成两半,左右两半各自不再保证有序,`par_merge` 的前提(输入两段各自有序)被破坏。按块切分保证每个递归节点的输入都是整数个有序块;块内元素数略有不均在所难免(不均衡由 `split_for_merge` 在 `par_merge` 内部按元素精确补偿),这也是为什么切分点选 `chunks[len/2]` 的左端点作 `mid`。

### 4.3 稳定性与 panic 安全

#### 4.3.1 概念说明

**稳定性**是 `par_sort` 相对 `par_sort_unstable` 的核心附加值,它的保证链由三处共同构成:

1. `split_for_merge` 的二分定位使相等元素的分派方向一致(来自 `left` 的相等元素始终划入左半);
2. 串行归并的「相等取左」([par_merge 的 `to_copy` 选择](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/sort.rs#L1366-L1373)与 [merge 的对应分支](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/sort.rs#L1003-L1015)两处);
3. 阶段一 `merge_sort` 本身是 TimSort 风格的稳定排序。

第 2 点在源码里以注释形式被明确强调(「If equal, prefer the left run to maintain stability」)——相等时 `is_l` 为假,取左侧。三个环节只要有一个换成「相等取右」,稳定性就在某条并行路径上被破坏,且只在特定输入下暴露,极难排查。

**panic 安全**指:用户比较函数 `is_less` 在任意一次调用中 panic,排序中止后,切片仍**恰好持有原来那组元素**(每个元素一次、不多不少,可能顺序不对)。这依赖贯穿全文件的「hole 守卫」模式:把「正在搬迁的元素」记在一个结构体里,正常路径 `mem::forget` 掉它,panic 展开时它的 `Drop` 把元素放回去。

#### 4.3.2 核心流程

hole 守卫的统一生命周期:

```text
建立守卫(记录 src/dest 与未落位元素)
  ↓
执行可能 panic 的搬运/比较循环
  ├─ 正常结束 → mem::forget(守卫)   # 元素已各就各位,守卫无需回填
  └─ panic 展开 → Drop(守卫) 自动运行  # 把残留元素拷回,切片恢复完整
```

全文件共四组守卫,覆盖四类搬运场景:

| 守卫 | 位置 | 守护的场景 | Drop 行为 |
| --- | --- | --- | --- |
| `InsertionHole` | [L27-L41](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/sort.rs#L27-L41) | 插入排序挪动元素时挖出的洞 | 把暂存元素拷进洞 |
| `MergeHole`(merge) | [L983-L1053](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/sort.rs#L983-L1053) | 串行归并复制进 buf 的短侧 | 把 buf 残段拷回 v 的洞 |
| `State`(par_merge) | [L1356-L1362](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/sort.rs#L1356-L1362), [L1401-L1421](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/sort.rs#L1401-L1421) | 并行归并两侧的未消费部分 | 先拷 left 残段再拷 right 残段 |
| `MergeHole`(merge_recurse) | [L1484-L1501](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/sort.rs#L1484-L1501) | 子递归期间的整段 src | 整段 src 拷到 dest |

#### 4.3.3 源码精读

**par_merge 的 State 守卫**。

[src/slice/sort.rs:L1399-L1421](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/sort.rs#L1399-L1421):

```rust
// When dropped, copies arrays `left_start..left_end` and `right_start..right_end` into `dest`,
// in that order.
struct State<T> {
    left_start: *mut T,
    left_end: *mut T,
    right_start: *mut T,
    right_end: *mut T,
    dest: *mut T,
}

impl<T> Drop for State<T> {
    fn drop(&mut self) {
        unsafe {
            let left_len = self.left_end.offset_from(self.left_start) as usize;
            ptr::copy_nonoverlapping(self.left_start, self.dest, left_len);
            self.dest = self.dest.add(left_len);

            let right_len = self.right_end.offset_from(self.right_start) as usize;
            ptr::copy_nonoverlapping(self.right_start, self.dest, right_len);
        }
    }
}
```

四个指针随归并循环推进(`left_start`/`right_start` 不断前移),Drop 时把**两侧尚未消费的元素**按「先左后右」拷进 `dest` 的剩余空间——顺序天然正确(left 残段仍全 ≤ right 残段的补集),所以即使 panic 后数据也常常「恰好有序」。函数级 docstring([L1331-L1332](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/sort.rs#L1331-L1332))把这定为契约:「即使 `is_less` 在归并中途 panic,本函数也会把 left 与 right 的全部元素完整拷入 dest(未必有序)」。这很重要,因为 `par_merge` 的调用方(`merge_recurse`)的守卫以「dest 侧数据完整」为前提才能保证不丢元素。

`merge_recurse` 的守卫同理([L1479-L1488](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/sort.rs#L1479-L1488)):子递归 panic 时把 `src[start..end]` 整段拷进 `dest[start..]`,与 `par_merge` 的契约首尾相接,构成跨函数的 panic 安全链。串行侧插入排序的 `InsertionHole` 注释([L69-L76](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/sort.rs#L69-L76))把设计意图说得最直白:守卫「保护 v 的完整性,使其仍恰好持有最初持有的每个对象,且恰好一次」。

**稳定性测试**。

[src/slice/test.rs:L96-L127](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/test.rs#L96-L127) 的 `test_par_sort_stability` 用了一个聪明的构造:生成 `(随机数, 出现序号)` 元组,只按第一个元素排序;若排序稳定,则同键元素的序号必然递增,于是「用含序号的比较判有序」一步到位地断言了稳定性。测试长度特意跨过 `MAX_INSERTION`(20)、`CHUNK_LENGTH`(2000)两档(2..25、500..510、50_000..50_010),覆盖串行插入、串行 TimSort、完整并行三条路径。

**正确性宽度测试**。

[src/slice/test.rs:L10-L93](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/test.rs#L10-L93) 的 `sort!` 宏把同一套断言套在 `par_sort_by` 与 `par_sort_unstable_by` 上([L92-L93](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/test.rs#L92-L93)),输入形态包括:小数组×高重复(模数 5/10/100,触发 `partition_equal` 与游程拼接)、十万级多重复、预排序后再打乱几段(自适应路径)、**完全随机的比较函数**([L73-L78](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/test.rs#L73-L78):非全序的比较器,断言元素不丢不重)、零长数组与 `[(); 10]`(零大小类型,对应 `par_mergesort` 开头的 `size_of::<T>() == 0` 早退)。这组测试与 u6-l4 精读过的 `tests/sort-panic-safe.rs` 互补:前者测宽输入,后者用「定时 panic 的比较函数」在展开与恢复中反复验证元素守恒。

**demo 中的昂贵比较器**。

[rayon-demo/src/sort.rs:L104-L129](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/sort.rs#L104-L129) 定义了一个刻意昂贵的比较函数(原子自增、取模、三角函数、潜在 panic 的 landing pad),再由 [L131-L149](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/sort.rs#L131-L149) 展开成升序/降序/近排序/随机/大元素/字符串等一整套 `par_sort` bench。它演示的正是并行排序的适用边界:比较越贵、元素越大,任务切分开销占比越低,并行收益越显著。

#### 4.3.4 代码实践

**实践 E:亲眼看一次 panic 安全(示例代码,独立工程)**。

1. 实践目标:验证「比较函数 panic 后,切片元素一个不少」。
2. 操作步骤:

```rust
// 示例代码:统计 panic 前后多重集合是否一致
use rayon::prelude::*;
use std::collections::BTreeMap;

fn main() {
    let v: Vec<i32> = (0..1_000_000).collect();
    let before: BTreeMap<i32, usize> = v.iter().fold(BTreeMap::new(), |mut m, &x| {
        *m.entry(x).or_insert(0) += 1;
        m
    });

    let mut v2 = v.clone();
    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        // 遇到特定值就 panic 的比较函数
        v2.par_sort_by(|a, b| {
            if *a == 123_456 { panic!("boom"); }
            a.cmp(b)
        });
    }));
    assert!(result.is_err(), "应当捕获到 panic");

    let after: BTreeMap<i32, usize> = v2.iter().fold(BTreeMap::new(), |mut m, &x| {
        *m.entry(x).or_insert(0) += 1;
        m
    });
    println!("元素守恒: {}", before == after);
    // 进一步验证:panic 后线程池仍可正常工作
    let sum: i64 = (0..1000).into_par_iter().sum();
    println!("池仍可用: {}", sum == 499_500);
}
```

3. 需要观察的现象:`before == after` 应为 true(元素可能未排好,但多重集合相同);线程池没被 panic 毁掉。
4. 预期结果:两行都输出 true。若想看到「顺序未必有序」,可再打印 `v2.is_sorted()`——大概率是 false。完整行为**待本地验证**(panic 发生点不同,残留状态不同,但元素守恒不变)。

#### 4.3.5 小练习与答案

**练习 1**:如果把 `par_merge` 串行分支里的 `let is_l = is_less(&*s.right_start, &*s.left_start);` 改成先比 `left` 后比 `right` 的「相等取右」,测试能发现吗?

答案:能,但不是所有测试。`test_par_sort_stability` 专门按稳定性断言,长度覆盖并行路径,会稳定失败;而 `sort!` 宏里升序/随机输入用的是数值本身(相等元素不可区分),多数情况测不出来——这正是稳定性测试要用「(key, 序号) 元组」这种可区分相等元素的原因。

**练习 2**:`par_merge` 进入并行分支前为什么要 `mem::forget(s)`,而串行分支结束时却让 `s` 正常 Drop?

答案:串行分支的 `s` 是本次归并的守卫,循环自然结束后两侧可能还有未消费残段,Drop 把它们拷进 dest 尾部正是收尾工作([L1396-L1397](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/sort.rs#L1396-L1397) 注释:「Finally, `s` gets dropped if we used sequential merge」)。并行分支里,职责已移交给两个递归调用各自新建的 `State`(各自记录各自的指针区间);若外层 `s` 再 Drop,会把已被子任务写过的 dest 区间重复覆盖、且指针位置已过期。forget 确保每个区间只有恰好一个守卫负责。而 `rayon_core::join` 保证两个分支都执行完毕才返回(u5-l1),子守卫的 Drop/forget 链因此闭合。

**练习 3**:`[(); 10].par_sort()` 会分配缓冲、启动线程吗?

答案:不会。`size_of::<T>() == 0` 在函数开头直接 return([L1527-L1530](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/sort.rs#L1527-L1530))——零大小类型的所有元素「相同」,排序无意义;`test.rs` 的 [L82-L83](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/test.rs#L82-L83) 专门断言这条路径不 panic。

## 5. 综合实践

**任务:写一份 mini 版并行归并排序,并用它复现主库的三个设计决策。**

在练习工程中(依赖 `rayon` 与任一随机数 crate),不抄主库、只允许参照本讲画过的流程图,从空白实现以下函数(示例代码骨架):

```rust
// 示例代码:mini 并行归并排序(仅 u64,简化掉 panic 守卫)
fn mini_par_sort(v: &mut [u64], chunk: usize, merge_min: usize) {
    // 1. 阶段一:v.par_chunks_mut(chunk).with_max_len(1) 内用 v[ch].sort() 串行排块
    // 2. 阶段三:收集区间列表,实现 merge_recurse 的 into_buf 翻转 +
    //    split_for_merge(较长侧取中点、较短侧二分)+ rayon::join 递归
    // 3. 阈值 chunk/merge_min 做成参数
}
```

验收清单(逐条对应本讲三个最小模块):

1. **正确性**:与 `v.sort()` 结果逐元素相等;再构造升序、降序、「几段有序拼接」三种输入验证自适应路径(升序输入应几乎不触发归并——可加计数器验证)。
2. **缓冲区**:用一块 `Vec<u64>`(容量 n)实现 `into_buf` 乒乓,纸面推演数据最终回到 `v`;尝试故意把顶层 `into_buf` 设反,观察结果落到 buf 后 `v` 变成什么样,加深对翻转的理解。
3. **稳定性**:把元素类型换成 `(u64, usize)`,按第一个分量排序,用 4.3 中的「序号递增」断言验证你的「相等取左」实现正确。
4. **性能**:固定 `--size 10_000_000`,扫描 `chunk ∈ {1024, 2048, 4096, 8192}`、`merge_min ∈ {2*chunk, 8*chunk}`,画耗时表格,与 `std sort`、`rayon par_sort` 三方对比,解释你的最优值与主库 2000/5000 的差异原因(元素大小、机器核数、缓存层级)。

进阶(可选):给你的 mini 版加上 `State` 式 Drop 守卫,复做 4.3.4 的 panic 元素守恒实验。

## 6. 本讲小结

- `par_sort` 的三阶段:`MAX_INSERTION=20` 以下插入排序零分配;`CHUNK_LENGTH=2000` 分块 + `with_max_len(1)` 并行串行 TimSort;相邻同向游程自适应拼接后交 `merge_recurse` 并行归并。阈值 20 < 2000 < 5000 各自对应插入/排序/归并三种操作的单位工作量。
- 并行归并的钥匙是 `split_for_merge`:较长侧取中点、较短侧二分定位,保证左右输出精确拼接目标区间,写位置由算法性质决定,全程无锁无原子变量。
- 缓冲区是「一块 len 容量、长度恒为 0 的 Vec」:分块阶段按区段借用(串行 merge 只需半块),归并阶段靠 `into_buf` 逐层翻转乒乓使用;长度为 0 使浅拷贝免于重复析构,panic 安全免费获得。规格中提到的旧概念 out_of_buf 在当前 HEAD 已不存在。
- 稳定性由三处合力:`split_for_merge` 的相等分派方向、两处串行归并的「相等取左」、底层 TimSort 自身稳定。
- panic 安全由四组 hole 守卫(`InsertionHole`、`MergeHole`×2、`State`)构成跨函数链条,正常路径 `mem::forget`、展开路径 Drop 回填,保证切片元素恰好守恒;`rayon_core::join` 的「两分支必都完成」语义让并行分支的守卫移交(mem::forget 外层、子层自建)得以闭合。
- demo 的 mergesort 是同一算法的教学标本:`MaybeUninit` + `Copy` 约束、32K/64K 阈值、显式的 rsort/rsort_into 交替,适合作为调参实验的底板(在你自己的拷贝上改)。

## 7. 下一步学习建议

本讲结束,单元八还剩最后一讲 u8-l3(集合的委托实现模式)。在此之前,建议按以下顺序巩固:

1. **读 `par_quicksort` 一侧**:对照 [src/slice/sort.rs:L829-L947](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/sort.rs#L829-L947) 的 `recurse`,注意它与 `par_merge` 的对称性:快排按 pivot 划分(天然两半独立、无需二分定位),`MAX_SEQUENTIAL = 2000` 时用「短侧递归 + 长侧循环」代替 join(尾递归消除),只有两侧都可能超阈值时才 join。思考:为什么快排不需要 `split_for_merge` 这样的辅助函数?
2. **读 `par_sort_by_cached_key`**:[src/slice/mod.rs:L548-L597](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/mod.rs#L548-L597),它把「昂贵的键计算」与「排序」解耦——先并行算 `(key, index)`,用**不稳定**排序(索引唯一故天然稳定),再原地换位。这是「装饰-排序-去装饰」模式在并行世界的实现,也是 `par_sort_unstable` 的最佳使用场景。
3. **跑一遍 demo 的排序 bench**(需 nightly):`cargo bench -p rayon-demo -- sort`,观察 ascending/descending/mostly_sorted/random 四种输入下 `par_sort` 与 `par_sort_unstable` 的差距如何随输入有序度变化——你会看到自适应归并在「近排序」输入上的回报。
4. 之后进入 u8-l3,看 collections 模块如何用委托宏把这里的并行能力(间接)复用到标准集合上。
