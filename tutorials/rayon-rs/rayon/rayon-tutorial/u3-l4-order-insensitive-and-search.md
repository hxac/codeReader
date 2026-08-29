# 顺序无关操作：查找与交错

## 1. 本讲目标

学完本讲，你应该能够：

- 区分 `find_any` / `find_first` / `find_last` 三者的确定性与性能取舍：`find_any` 返回**任意**一个匹配项，`find_first` / `find_last` 返回**串行顺序意义上**的第一个 / 最后一个匹配项。
- 理解 `find_any` 如何用一个共享 `AtomicBool` 加 plumbing 层的 `full()` 钩子实现「找到一个就全池停工」。
- 理解 `find_first` / `find_last` 的「虚区间」协议：给没有下标的无索引消费者人为分配一个想象中的位置区间，用区间比较实现跨线程剪枝。
- 掌握 `take_any` / `skip_any` 的「全局配额」语义：总共取 / 跳过 n 个元素，但**谁**取、**谁**跳不确定。
- 掌握 `interleave` / `intersperse` 的确定交错顺序规则，以及它们的生产器如何在任意切分点保持这个顺序。

本讲的主线是一对矛盾：**牺牲顺序可以换性能**（`*_any` 家族），**保留顺序则需要生产器做精确的坐标换算**（`interleave` / `intersperse`）。

## 2. 前置知识

本讲建立在 u3-l1（适配器骨架）与 u2-l5（try_reduce 全局短路）之上，先回顾三个关键概念：

1. **plumbing 三角色**（u3-l1）：数据从 `Producer` 流出，经过 `Consumer` 的层层包装，最终落在 `Folder::consume` 里逐元素处理；多个任务的部分结果由 `Reducer::reduce` 两两合并。
2. **`full()` 短路钩子**（u2-l5）：`Consumer::full()` 与 `Folder::full()` 是调度器在递归切分和逐元素消费时反复询问的问题——「这个消费者还需要更多数据吗？」。一旦返回 `true`，调度器就不再给它喂数据。`try_reduce` 用它实现全局短路，本讲的 `find_any`、`take_any` 用的是完全相同的机制。
3. **无索引驱动路径**（u2-l1）：`find_*`、`take_any`、`skip_any` 都定义在 `ParallelIterator` 上（不要求已知长度），走 `drive_unindexed` + `bridge_unindexed` 的「按能力任意对半分」路径；`interleave` 定义在 `IndexedParallelIterator` 上，走 `with_producer` + `bridge` 的「按下标精确切分」路径。

另外一个术语说明：**「串行顺序」**（sequential order）指把同一个并行迭代器按 `iter()` 串行消费时元素的先后。`find_first` 的「first」就是指这个虚拟串行序里的第一个，而不是「哪个线程先找到」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/iter/mod.rs` | `ParallelIterator` / `IndexedParallelIterator` 两大根 trait。本讲涉及的方法定义与文档注释都在这里：`find_any` / `find_first` / `find_last`、`any` / `all`、`position_*`、`take_any` / `skip_any`、`intersperse`、`interleave` / `interleave_shortest` |
| `src/iter/find.rs` | `find_any` 的实现：`FindConsumer` / `FindFolder` / `FindReducer`，基于共享 `AtomicBool` |
| `src/iter/find_first_last/mod.rs` | `find_first` / `find_last` 的实现：虚区间协议、`AtomicUsize` 位置竞争、方向感知的 Reducer |
| `src/iter/find_first_last/test.rs` | 针对虚区间「分辨率耗尽」边界情形的单元测试 |
| `src/iter/take_any.rs` | `take_any`：共享 `AtomicUsize` 配额，倒计数抢名额 |
| `src/iter/skip_any.rs` | `skip_any`：与 `take_any` 同构，配额耗尽后放行 |
| `src/iter/interleave.rs` | `Interleave` 适配器与 `InterleaveProducer`：交错空间到两侧下标的换算 |
| `src/iter/intersperse.rs` | `Intersperse` 适配器：在元素间克隆插入分隔项，indexed 能力保留 |
| `tests/intersperse.rs` | `intersperse` 的集成测试：长度、双重嵌套、无索引路径、`zip_eq` 索引验证、`rev` 双端验证 |

## 4. 核心概念与源码讲解

### 4.1 find_any：一个 AtomicBool 实现全局短路

#### 4.1.1 概念说明

`find_any` 回答的问题是：「序列里**存在**满足谓词的元素吗？存在就给我一个」。

串行 `Iterator::find` 从头扫到尾，天然返回第一个匹配。并行场景下，数据被切成若干段同时扫描，任何一段先找到匹配，其余段的扫描就都成了浪费。所以 `find_any` 的设计目标是：**任何一个任务找到，全体任务立即停止**。代价是返回的是「某一个」匹配而不是「第一个」——对存在性判断（配合 `any` / `all`）这个代价为零。

#### 4.1.2 核心流程

```
find_any(pi, pred):
    found = AtomicBool::new(false)          # 全局共享，栈上分配
    consumer = FindConsumer { pred, &found }
    pi.drive_unindexed(consumer)            # 无索引驱动

每个任务（每个 FindFolder）逐元素执行：
    consume(item):
        if pred(item):
            found.store(true)               # 广播：找到了
            self.item = Some(item)          # 本任务记下它
    调度器在喂数据前询问 full():
        found == true ?                     # 是 → 不再喂

归约阶段（Reducer）：
    reduce(left, right) = left.or(right)    # 任一 Some 即 Some
```

注意一个细节：`found` 置位后，已经拿到匹配的任务仍可能在 `consume` 里多处理一个元素（store 发生在检查之后），所以多个任务可能各自持有 `Some(item)`——最终由 `left.or(right)` 任取其一，这正是「结果不确定」的来源。

#### 4.1.3 源码精读

入口定义在 `ParallelIterator` 上，转发给 `find` 模块：

[src/iter/mod.rs:L1668-L1673](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L1668-L1673) —— `find_any` 的定义：文档明确说「返回的可能不是并行序列中第一个匹配项，因为我们并行搜索整个序列」。约束 `P: Fn(&Item) -> bool + Sync + Send`，谓词以共享引用被多任务调用。

[src/iter/find.rs:L4-L12](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/find.rs#L4-L12) —— `find` 的实现：在调用方栈上创建 `AtomicBool`，把它的引用和谓词引用一起塞进 `FindConsumer`，然后 `drive_unindexed`。

[src/iter/find.rs:L46-L48](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/find.rs#L46-L48) —— `Consumer::full()`：直接读共享 `AtomicBool`。这就是全局短路的开关——任何线程置位，所有持有该消费者副本的任务在下一次询问时都会报告「满了」。

[src/iter/find.rs:L77-L83](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/find.rs#L77-L83) —— `Folder::consume`：谓词命中就置位 `found` 并保存元素；未命中什么都不做，继续等下一个元素。

[src/iter/find.rs:L85-L102](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/find.rs#L85-L102) —— `consume_iter` 快速路径：当底层生产器可以按串行迭代器批量喂数据时，用 `take_while(not_full)` 让本任务在**其他线程**找到答案时提前退出本地循环——这是比逐元素 `full()` 检查更粗粒度但更省事的短路。

[src/iter/find.rs:L113-L119](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/find.rs#L113-L119) —— `FindReducer`：`left.or(right)`，两个候选匹配任取其一（优先左边，但左右谁有值本就不确定）。

`find_any` 还是一个「基础设施」——`any` / `all` 直接搭在它上面：

[src/iter/mod.rs:L1866-L1871](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L1866-L1871) —— `any`：先 `map(predicate)` 把元素映射成布尔，再用 `find_any(bool::clone)` 找第一个 `true`。

[src/iter/mod.rs:L1888-L1898](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L1888-L1898) —— `all`：找第一个 `false`，找不到（`None`）即全部为真。找到反例即全局停止，与 `any` 对偶。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 `find_any` 的「任意性」与 `any` 的短路行为。

1. 在你的示例工程（u1-l3 创建的那个即可）里写入以下程序（**示例代码**）：

   ```rust
   use rayon::prelude::*;

   fn main() {
       let v: Vec<i32> = (0..10_000_000).map(|i| i * 2).collect();
       // 500 万个偶数里找 "第一个" 能被 3 整除的
       for _ in 0..5 {
           let r = v.par_iter().find_any(|&&x| x % 3 == 0);
           println!("find_any -> {:?}", r);
       }
       let r = v.iter().find(|&&x| x % 3 == 0);
       println!("串行 find -> {:?}", r);
   }
   ```

2. 用 `cargo run --release` 运行。
3. **观察的现象**：串行 `find` 永远返回 `Some(&0)`（下标 0 的 0 能被 3 整除）；`find_any` 的多次输出——如果你的谓词选得能命中多个元素（本例命中很多），不同轮次可能返回不同的值，也可能恰好稳定。
4. **预期结果**：`find_any` 的输出**不保证**等于串行结果，也**不保证**轮次之间一致；官方文档只承诺「返回某个匹配项或 `None`」。小数据量下切分方式确定，输出可能碰巧稳定，但这不是契约。具体输出待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`find_any` 的 `FindReducer` 写的是 `left.or(right)`。如果两个任务都持有匹配项，最终返回哪个？这影响正确性吗？

答案：返回 `left`（若 `Some`），否则 `right`。两个都是合法答案（都满足谓词），所以不影响正确性，只影响确定性——这正是 `find_any` 契约允许的任意性。

**练习 2**：为什么 `found` 用 `Ordering::Relaxed` 而不是 `Acquire/Release`？

答案：这里只需要原子的「置位 / 读取」效果做尽力而为的提示，不依赖它建立跨线程的内存序（匹配项本身通过各任务自己的 `item` 字段和归约树传递，不经过 `found`）。晚一拍读到 `true` 只是少省一点工，不会出错，`Relaxed` 足够且最快。

**练习 3**：用 `any` 重写「判断 1..=n 中是否存在完全平方数」并说明它为什么不会扫完全部数据。

答案：`(1..=n).into_par_iter().any(|x| (x as f64).sqrt().fract() == 0.0)`。`any` 内部是 `map(predicate).find_any(...)`，第一个 `true` 经共享 `AtomicBool` 触发所有任务的 `full()`，其余数据不再被消费。

### 4.2 find_first / find_last：虚区间协议

#### 4.2.1 概念说明

`find_first` 要的是**串行顺序意义上的第一个**匹配。难点在于：`find_*` 系列定义在 `ParallelIterator` 上，允许接入完全没有下标概念的数据源（比如 `filter` 之后，元素位置不可知），那怎么让各任务知道「自己负责的数据在全局排第几」，从而判断「别人的匹配比我更靠前，我可以撤了」？

答案是**虚区间**（imaginary range）：不要求真实下标，只要求**分裂结构**。初始消费者分到区间 \([0, 2^{64}-1]\)，每次 `split_off_left` 把区间对半，左半 \([lo, mid)\)、右半 \([mid, hi)\)。区间的相对大小关系与数据的相对位置关系**同构**——左边消费者的区间永远排在右边前面。于是「位置比较」退化为「区间边界比较」，无需真实下标。

性能取舍：`find_first` 找到匹配后只能停掉**右侧**的搜索，**左侧**必须继续扫完（左侧可能有更早的匹配）；`find_last` 对偶。最坏情况（匹配在最后）下 `find_first` 与全量扫描相当，而 `find_any` 一旦找到即可全停——这就是确定性的价格。

#### 4.2.2 核心流程

```
find_first(pi, pred):
    best_found = AtomicUsize::new(usize::MAX)     # 目前已知最好（最小）位置
    consumer = FindConsumer { pred, 区间 [0, usize::MAX], Leftmost, &best_found }
    pi.drive_unindexed(consumer)

分裂（split_off_left）：
    median = lo + (hi - lo) / 2
    左consumer: [lo, median)   右consumer: [median, hi)      # 有重叠也无妨，见下

每个任务消费元素时：
    current_index = Leftmost 取 lo（区间内最小可能位置）
    if pred(item) && 本任务尚未有匹配:
        CAS 循环：若 current_index < best_found 则写入
        写入成功（或别人已写入同值）→ 保存 item

full() 判定（Leftmost）：
    本任务已有匹配（区间内第一个就是最好的）
    或 best_found < current_index（别人的匹配严格更靠左 → 我扫下去也是输）

归约：
    Leftmost:  left.or(right)      # 优先左侧
    Rightmost: right.or(left)      # 优先右侧
```

数学上，经过 \(k\) 次分裂，区间宽度约为 \(2^{64-k}\)；`usize` 只有 64 位，分裂约 64 次后宽度归 1，两个子消费者拿到**相同**区间。此时无法再用区间区分谁更靠左（不能互相剪枝），两边都会扫完自己的数据——但归约器按方向优先合并，答案依然正确。源码注释与单元测试专门覆盖了这个边界。

#### 4.2.3 源码精读

[src/iter/find_first_last/mod.rs:L8-L23](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/find_first_last/mod.rs#L8-L23) —— 官方注释，完整阐述了虚区间动机：消费者需要「相对位置」的概念，包括没有内建位置概念的无索引消费者；方案是给每个消费者分配想象中区间的上下界，初始 `0..usize::MAX`，逐次对半；分辨率耗尽时归约器兜底。

[src/iter/find_first_last/mod.rs:L40-L58](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/find_first_last/mod.rs#L40-L58) —— `find_first` 与 `find_last` 的入口：同一个 `FindConsumer`，区别只有方向枚举与 `best_found` 的初值——`Leftmost` 用 `usize::MAX`（任何位置都更小、更优），`Rightmost` 用 `0`（任何位置都更大、更优）。

[src/iter/find_first_last/mod.rs:L31-L38](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/find_first_last/mod.rs#L31-L38) —— `better_position`：「更好」的定义随方向翻转：`Leftmost` 是更小，`Rightmost` 是更大。

[src/iter/find_first_last/mod.rs:L79-L85](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/find_first_last/mod.rs#L79-L85) —— `current_index`：本消费者「名义位置」。`Leftmost` 取下界（区间内能找到的最早位置），`Rightmost` 取上界。这就是 `full()` 比较的基准。

[src/iter/find_first_last/mod.rs:L117-L126](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/find_first_last/mod.rs#L117-L126) —— `Consumer::full()`：若 `best_found` **严格**好于 `current_index`，本消费者扫到的任何匹配都不会赢，可以停。注释里「strictly」很关键——相等时不能停（可能自己就是赢家）。

[src/iter/find_first_last/mod.rs:L133-L156](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/find_first_last/mod.rs#L133-L156) —— `split_off_left`：虚区间对半分。注意 `lower_bound` 用 `Cell` 存储以便原地修改自身（`&self` 签名所迫）；注释解释了上下界允许重叠的原因——每个消费者只用其中一个界参与比较，另一个界只为继续对半分而保留。

[src/iter/find_first_last/mod.rs:L176-L198](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/find_first_last/mod.rs#L176-L198) —— `FindFolder::consume`：`Leftmost` 下若本任务已有匹配（`found_best_in_range`）则直接跳过后续元素（区间内第一个就是最好的）；否则命中时用 `fetch_update`（CAS 循环）在「我的 boundary 更好」时更新 `best_found`，成功或「失败但当前值恰好等于我的 boundary」（别的同级任务写入了同一位置）都保存 item。`Rightmost` 下 `found_best_in_range` 恒为 `false`——后面永远可能出现更靠右的匹配，必须继续消费。

[src/iter/find_first_last/mod.rs:L223-L230](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/find_first_last/mod.rs#L223-L230) —— `FindReducer`：方向感知的归约——`Leftmost` 优先左、`Rightmost` 优先右。递归归约树的结构恰好镜像了数据的左右结构，所以这个简单的 `or` 顺序就能在多候选中选出正确一端。

[src/iter/find_first_last/test.rs:L4-L35](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/find_first_last/test.rs#L4-L35) —— 边界测试：手动把消费者连续 `split_off_left` 共 `usize::BITS` 次，逼到区间分辨率耗尽、两个 folder 拿到相同 `boundary`；断言此时互不剪枝（`!right_folder.full()`）但归约结果仍是正确的 `Some(0)`。

顺带一提 `position_*` 家族——它们完全复用 find 的消费者，只是先给元素编上号：

[src/iter/mod.rs:L3009-L3019](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L3009-L3019) —— `position_any`：`self.map(predicate).enumerate().find_any(check)`，找到后取元组的下标分量。`position_first`（L3046 起）、`position_last`（L3083 起）同理换成 `find_first` / `find_last`。文档示例 `assert!(i == 2 || i == 3)` 明示了 `position_any` 的不确定性。

[src/iter/mod.rs:L1682-L1683](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L1682-L1683) —— 文档给出性能提示：`find_first` 可配合 `by_exponential_blocks()` 提升表现（分块方式让左侧更早完整扫完）。本讲不展开，u3-l5 的 `blocks` 会涉及分块思想。

#### 4.2.4 代码实践

**实践目标**：对比三个 find 的返回值确定性与相对耗时。

1. 写入以下程序（**示例代码**）：

   ```rust
   use rayon::prelude::*;
   use std::time::Instant;

   fn main() {
       // 1000 万个数，能被 7 整除的都算"匹配"（匹配很多）
       let v: Vec<i32> = (0..10_000_000).collect();
       let pred = |&x: &i32| x % 7 == 0 && x > 9_999_000; // 匹配集中在序列尾部

       for _ in 0..3 {
           println!("find_any   -> {:?}", v.par_iter().find_any(pred));
       }
       let t = Instant::now();
       let ff = v.par_iter().find_first(pred);
       println!("find_first -> {:?} ({:?})", ff, t.elapsed());

       let t = Instant::now();
       let fl = v.par_iter().find_last(pred);
       println!("find_last  -> {:?} ({:?})", fl, t.elapsed());
   }
   ```

2. `cargo run --release` 运行多次。
3. **观察的现象**：谓词把匹配集中在序列**尾部**——这对 `find_first` 是最坏情况（左侧必须扫完），对 `find_last` 是最好情况（右侧找到即可向左剪枝）。
4. **预期结果**：`find_first` 与 `find_last` 每次都返回**同一个**确定值（串行意义上的第一个 / 最后一个匹配）；`find_any` 返回值在多次运行间可能变化。耗时上 `find_first` 明显慢于 `find_last`（匹配在尾部时）。具体数值待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`find_first` 找到匹配后，哪些部分的搜索可以停止，哪些必须继续？为什么？

答案：匹配点**右侧**的任务可以停（它们的虚区间下界都大于等于 `best_found`，`full()` 返回真）；匹配点**左侧**的任务必须继续，因为左侧可能存在更靠前的匹配。这正是 `find_first` 在「匹配靠后」场景下退化为近全量扫描的原因。

**练习 2**：虚区间分裂 64 次后两个消费者拿到相同区间，为什么最终答案 still 正确？

答案：两个消费者的 `boundary` 相同，`better_position` 要求**严格**更好，互不触发剪枝，于是各自扫完自己的数据、各自持有候选；归约阶段 `FindReducer` 按方向（`Leftmost` 优先左）合并，而归约树结构与数据左右结构一致，正确的候选在合并中胜出。`same_range_first_consumers_return_correct_answer` 测试验证了这条路径。

**练习 3**：如果数据源本身是 `IndexedParallelIterator`（如切片），`find_first` 用的还是虚区间吗？

答案：是的。`find_first` 走 `drive_unindexed`，对底层是否 indexed 一视同仁，位置信息一律来自虚区间而非真实下标（`split_at` 的 `index` 参数在实现里被忽略，见 find_first_last/mod.rs L96 的 `_index`）。真实下标只在 `take` / `skip` / `zip` 那类 indexed 适配器里使用（u3-l3）。

### 4.3 take_any / skip_any：全局配额

#### 4.3.1 概念说明

`take(n)` / `skip(n)`（u3-l3 讲过）要求 indexed：取「前」n 个、跳「前」n 个，因为要按下标切。如果把「前」放宽为「任意」，就不需要下标了——于是 `take_any(n)` / `skip_any(n)` 定义在 `ParallelIterator` 上：**全局总共**取 / 跳 n 个元素，至于哪些任务的名额被用掉不确定。

实现模式与 `find_any` 同源但方向相反：`find_any` 是共享一个布尔（有 / 无），`take_any` 是共享一个**倒计数配额**（`AtomicUsize`）。每个元素在被下游消费前先尝试原子扣减配额，扣成功才算数。

一个重要澄清：**「取谁」不确定，但「剩下的」相对顺序保留**。官方文档对 `take_any` 的示例断言 `result.is_sorted()`（见 mod.rs L2225-L2236 的文档注释）——被取走的元素进入输出时仍按原相对顺序排列。`skip_any` 的文档（L2241-L2246）说得更直白：「剩余元素在 collect、reduce 等输出中保持相对顺序」。

#### 4.3.2 核心流程

```
take_any(n):
    quota = AtomicUsize::new(n)             # 全局共享配额
    consumer = TakeAnyConsumer { 下游consumer, &quota }
    base.drive_unindexed(consumer)

每个任务逐元素：
    consume(item):
        if quota.fetch_update(减一) 成功:    # 名额还剩 > 0
            下游.consume(item)               # 放行
        else:                                # 配额已耗尽
            丢弃 item
    full():
        quota == 0 || 下游.full()            # 配额抢完即停

skip_any(n): 完全对称
    consume(item):
        if !quota.fetch_update(减一) 成功:   # 只有配额耗尽后
            下游.consume(item)               # 才放行
```

「配额抢完即停」对 `take_any` 是很强的优化：100 万个元素 `take_any(5)`，理论上只需消费到第 5 个抢到名额的元素，其余任务全部短路。

#### 4.3.3 源码精读

[src/iter/mod.rs:L2237-L2239](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L2237-L2239) —— `take_any` 的定义（文档示例在 L2225-L2236：100 个有序数取 5 个，断言 `result.len() == 5` 且 `result.is_sorted()`，注意它**不断言**是哪 5 个）。

[src/iter/take_any.rs:L28-L37](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/take_any.rs#L28-L37) —— `TakeAny::drive_unindexed`：在调用方栈上创建 `AtomicUsize` 配额，包装下游消费者为 `TakeAnyConsumer` 再驱动上游。这是典型的「包装消费者」模式（u3-l1）：适配器自己不生产数据，只在外围加一层过滤逻辑。

[src/iter/take_any.rs:L103-L107](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/take_any.rs#L103-L107) —— `checked_decrement`：用 `fetch_update` 做「减一且不为零」的原子操作（`checked_sub` 在 0 时返回 `None`、CAS 失败），成功返回 `true`。多个线程同时扣最后一个名额时只有一个成功。

[src/iter/take_any.rs:L115-L120](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/take_any.rs#L115-L120) —— `Folder::consume`：扣到名额才把元素转交下游，否则丢弃。

[src/iter/take_any.rs:L122-L131](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/take_any.rs#L122-L131) —— `consume_iter` 快速路径：用 `take_while(扣名额)` 在迭代器层面截流，配额耗尽即停止本地消费。

[src/iter/take_any.rs:L76-L78](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/take_any.rs#L76-L78) —— `Consumer::full()`：`count == 0 || base.full()`——配额抢完，全体任务停工。

[src/iter/skip_any.rs:L115-L120](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/skip_any.rs#L115-L120) —— `SkipAnyFolder::consume`：与 take 恰好互补——`!checked_decrement(...)` 时（配额已耗尽）才放行。注意 `SkipAnyConsumer::full()`（L76-L78）**不看**配额：跳过操作无法提前知道后面还有没有元素，必须陪跑到底。

#### 4.3.4 代码实践

**实践目标**：验证 `take_any` 的「数量确定、成员不定、顺序保留」三要素。

1. 写入以下程序（**示例代码**）：

   ```rust
   use rayon::prelude::*;

   fn main() {
       let v: Vec<i32> = (1..=1_000_000).collect();
       for _ in 0..5 {
           let r: Vec<i32> = v.par_iter().take_any(5).cloned().collect();
           println!("{:?}  (sorted={}, len={})", r, r.is_sorted(), r.len());
       }
       let r: Vec<i32> = v.par_iter().skip_any(999_995).cloned().collect();
       println!("skip_any -> len={} sorted={}", r.len(), r.is_sorted());
   }
   ```

2. `cargo run --release` 运行。
3. **观察的现象**：每轮 `take_any(5)` 输出的 5 个数。
4. **预期结果**：每轮 `len == 5` 恒成立、`is_sorted()` 恒为 `true`，但**具体是哪 5 个数**轮次之间可能不同（小数据下也可能碰巧稳定）。`skip_any(999_995)` 的结果长度恰为 5 且有序。具体输出待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `TakeAnyConsumer::full()` 检查 `count == 0`，而 `SkipAnyConsumer::full()` 不检查？

答案：take 的目标是「凑够 n 个」，配额归零即目标达成，可以全池停工。skip 的目标是「扔掉 n 个然后要剩下的一切」——配额归零后剩下的元素**全部**要放行，无法预知后面还有多少，只能陪跑到数据耗尽或下游喊满。

**练习 2**：`(1..=100).into_par_iter().take_any(10).collect::<Vec<_>>()` 与 `take(10)`（indexed 版）的输出有何异同？

答案：长度都可能是 10，但 `take(10)` 恒等于 `[1..=10]`（按下标取前 10 个），`take_any(10)` 是从任意位置凑的 10 个（保持相对顺序）。此外 `take` 只对 `IndexedParallelIterator` 可用，`take_any` 对任意 `ParallelIterator` 可用——比如 `filter` 之后只能用 `take_any`。

**练习 3**：`take_any` 与 4.1 的 `find_any` 共享了哪个设计骨架？

答案：「共享原子变量 + `full()` 钩子」的全局短路骨架。`find_any` 共享 `AtomicBool`（有没有找到），`take_any` 共享 `AtomicUsize`（名额剩多少）；两者都在包装消费者里读取该变量决定 `full()`，都在 `consume` / `consume_iter` 中先查询再放行。

### 4.4 interleave / intersperse：确定的交错顺序

#### 4.4.1 概念说明

前三个模块都在「放弃顺序换性能」，这一模块反过来：**给出严格的顺序定义，并证明它在任意并行切分下不变**。

- `interleave(j)`：把两个迭代器的元素按 `i0, j0, i1, j1, ...` 交替输出，一侧耗尽后接着输出另一侧的剩余部分。例：`[1,2]` 与 `[3,4,5,6]` 交错得 `[1,3,2,4,5,6]`。它定义在 `IndexedParallelIterator` 上——两侧长度已知才能做切分换算。
- `intersperse(x)`：在相邻两个元素之间插入 `x` 的克隆，`[1,2,3]` intersperse `-1` 得 `[1,-1,2,-1,3]`，长度 \(2n-1\)。要求 `Item: Clone`（分隔项要被克隆到每个缝隙里）。

难点在切分：并行执行会把生产器从中间劈开交给不同线程，劈开点在**交错后的坐标空间**里，怎么换算回两侧各自的坐标，才能保证左右两半拼起来仍是严格的交错序？这是本模块源码的看点。

#### 4.4.2 核心流程

**interleave 的切分换算**：设切分点为交错空间的 `index`（\(0 < index \le i\_len + j\_len\)），需求解 \(a, b\) 满足：

\[
0 < a \le i\_len,\quad 0 < b \le j\_len,\quad a + b = index
\]

即「左半拿 i 的 a 个和 j 的 b 个」。偶数切分取 \(a = b = index/2\)；奇数时多出的一个分给「下一个该出元素」的那侧（由 `i_next` 标志决定）。若某侧太短不够分，则先取满该侧，剩下的全给另一侧（`index - j_len` 或 `index - i_len`）。右半的 `i_next` 标志按 `even == i_next` 翻转，保证拼接处的交替关系正确。

**intersperse 的切分换算**：交错序列前 `index` 个元素中，基准元素约占一半：`base_index = (index + !clone_first) / 2`——左半不是以克隆项开头时向上取整。右半的 `clone_first` 标志按 `index` 的奇偶翻转：奇数切分意味着右半将以一个克隆项开头。

**顺序不变性**：无论 bridge 在哪个 `index` 劈开，左右两半各自维持局部交错序，且拼接边界处「谁出下一个元素」的标志被正确传递——所以任意切分组合的结果都等于串行交错序。

#### 4.4.3 源码精读

[src/iter/mod.rs:L2638-L2643](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L2638-L2643) —— `interleave` 的定义：要求参数 `I: IntoParallelIterator<Item = Self::Item, Iter: IndexedParallelIterator>`，即另一侧不仅元素类型相同、还必须是 indexed。文档示例（L2632-L2637）`[1,2]` 与 `[3,4,5,6]` 得 `[1,3,2,4,5,6]`，后半段 `[5,6]` 是 i 耗尽后 j 的直接续接。

[src/iter/interleave.rs:L42-L56](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/interleave.rs#L42-L56) —— `Interleave` 实现 `IndexedParallelIterator`：`len` 是两侧长度之和（`checked_add` 防溢出）。保留 indexed 意味着 `collect` 走「精确预分配直写」快速路径（u2-l4），`zip` / `rev` 等继续可用。

[src/iter/interleave.rs:L58-L129](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/interleave.rs#L58-L129) —— `with_producer` 的双层回调：先向 i 索要生产者（`CallbackI`），再向 j 索要（`CallbackJ`），凑齐后合成 `InterleaveProducer` 交给真正的消费者。这是「组合两个生产者」的标准写法——u3-l3 的 `zip` 也是这个模式。

[src/iter/interleave.rs:L184-L235](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/interleave.rs#L184-L235) —— `InterleaveProducer::split_at` 全文：注释先给出数学约束（三条不等式），`odd_offset` 处理奇偶，然后三分支处理「两侧都够 / j 太短 / i 太短」，最后 `trailing_i_next = even == self.i_next` 决定右半的先行侧。这一段是本模块的核心，值得逐行读。

[src/iter/interleave.rs:L256-L277](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/interleave.rs#L256-L277) —— `InterleaveSeq::next`：任务内部的串行消费走这里，`i_next` 翻转决定取 i 还是 j，一侧 `None`（已 fuse）则直接取另一侧。注释说明实现取自 itertools，为支持 `DoubleEndedIterator` 而复制。

[src/iter/mod.rs:L2656-L2661](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L2656-L2661) —— `interleave_shortest`：任一侧耗尽即停（`[1,2,3,4]` 与 `[5,6]` 得 `[1,5,2,6,3]`），尾接语义不同于 `interleave`。

[src/iter/intersperse.rs:L64-L71](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/intersperse.rs#L64-L71) —— `Intersperse::len`：\(2n-1\)（n 为基准长度，n 为 0 时也是 0），`checked_add` 防溢出。

[src/iter/intersperse.rs:L158-L185](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/intersperse.rs#L158-L185) —— `IntersperseProducer::split_at`：`base_index = (index + !clone_first) / 2` 把交错坐标换算回基准坐标；右侧的 `clone_first: (index & 1 == 1) ^ self.clone_first`——奇数切分点意味着右半以克隆项开头。

[src/iter/intersperse.rs:L358-L369](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/intersperse.rs#L358-L369) —— `IntersperseFolder::consume`：`clone_first` 为真（说明前面已有元素）就先喂一个克隆的分隔项再喂元素；首次消费只翻标志不插分隔——「只在元素**之间**插入」的语义由这个标志实现。

[src/iter/intersperse.rs:L371-L391](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/intersperse.rs#L371-L391) —— `consume_iter` 快速路径：用 `flat_map` 在串行流里把「分隔项 + 元素」成对展开，一次转交下游。

[tests/intersperse.rs:L3-L10](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/tests/intersperse.rs#L3-L10) —— `check_intersperse`：`(0..1000).into_par_iter().intersperse(-1)` 收集后断言长度 1999（\(2 \times 1000 - 1\)）且偶数位是 `i/2`、奇数位是 `-1`——这就是交错序的逐位规格说明。

[tests/intersperse.rs:L12-L28](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/tests/intersperse.rs#L12-L28) —— `check_intersperse_again`：两层嵌套 intersperse（`-1` 与 `-2`），长度 3997（\(\approx 2 \times 1999 - 1\)），模 4 的位置分别断言——嵌套时切分换算仍正确。

[tests/intersperse.rs:L30-L37](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/tests/intersperse.rs#L30-L37) —— `check_intersperse_unindexed`：`par_split(',')` 是无索引迭代器，验证 `intersperse` 在 `drive_unindexed` 路径（走 `IntersperseConsumer` 而非 `IntersperseProducer`）下同样正确——注意 `intersperse` 同时实现了两条驱动路径。

[tests/intersperse.rs:L50-L60](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/tests/intersperse.rs#L50-L60) —— `check_intersperse_rev`：`intersperse` 后接 `zip_eq(0..1999)` 再 `rev()`，从尾部反向消费仍逐位对上——`IntersperseIter` 的 `DoubleEndedIterator` 实现（intersperse.rs L238-L257）保证了这一点。

#### 4.4.4 代码实践

**实践目标**：验证 `interleave` 的交错序与尾接规则。

1. 写入以下程序（**示例代码**）：

   ```rust
   use rayon::prelude::*;

   fn main() {
       let x: Vec<i32> = vec![1, 2];
       let y: Vec<i32> = vec![3, 4, 5, 6];

       let r: Vec<i32> = x.clone().into_par_iter().interleave(y.clone()).collect();
       println!("interleave          -> {:?}", r);

       let r: Vec<i32> = x.into_par_iter().interleave_shortest(y).collect();
       println!("interleave_shortest -> {:?}", r);

       // 大数据上验证顺序确定性：与串行 itertools 式交错逐位对比
       let a: Vec<i32> = (0..5000).map(|i| i * 2).collect();
       let b: Vec<i32> = (0..3000).map(|i| i * 2 + 1).collect();
       let par: Vec<i32> = a.par_iter().interleave(b.par_iter()).cloned().collect();
       let mut seq = Vec::with_capacity(8000);
       let (mut ai, mut bi) = (a.iter(), b.iter());
       loop {
           match (ai.next(), bi.next()) {
               (Some(x), Some(y)) => { seq.push(*x); seq.push(*y); }
               (Some(x), None) => seq.extend(ai.by_ref().map(|v| *v).chain([*x].into_iter())),
               (None, Some(y)) => seq.extend(bi.by_ref().map(|v| *v).chain([*y].into_iter())),
               (None, None) => break,
           }
       }
       println!("大样本顺序一致: {}", par == seq && par.len() == 8000);
   }
   ```

2. `cargo run --release` 运行多次。
3. **观察的现象**：三种输出的元素排列；大样本对比的布尔值。
4. **预期结果**：`interleave` 输出 `[1, 3, 2, 4, 5, 6]`（i 耗尽后 j 的尾部 `[5,6]` 直接续接）；`interleave_shortest` 输出 `[1, 3, 2, 6, ...]`——准确说 `[1, 3, 2, 4]` 之后较短侧（x，仅 2 个）已耗尽即停，得 `[1, 3, 2, 4]`（以官方文档 L2652-L2655 的示例为准，该示例是 `[1,2,3,4]` 与 `[5,6]` 得 `[1,5,2,6,3]`）。大样本顺序一致应为 `true` 且每次运行相同。具体输出待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `interleave` 定义在 `IndexedParallelIterator` 上，而 `intersperse` 虽然也实现了 indexed 却同时提供无索引路径？

答案：`interleave` 需要在**两个**生产器之间做坐标换算（`a + b = index`），两侧长度未知就无法在正确的点同时劈开两侧，所以强制 indexed。`intersperse` 只有一个基准生产器，坐标换算是自包含的；它实现了 `with_producer`（indexed 快速路径）也实现了 `drive_unindexed`（`IntersperseConsumer` 包装路径），因此无索引数据源（如 `par_split` 的结果）也能用。

**练习 2**：`intersperse` 为什么要求 `Item: Clone`？`intersperse` 的长度公式是什么？

答案：分隔项只是一个值，却要出现在 \(n-1\) 个缝隙里，只能不断克隆。长度为 \[ 2n - 1 \quad (n > 0) \]，n 为 0 时为 0。

**练习 3**：`InterleaveProducer::split_at` 中 `trailing_i_next = even == self.i_next` 这行的作用是什么？

答案：切分后右半生产器需要一个正确的「下一个该谁出元素」标志。偶数切分不改变交替相位（右半的先行侧与切分前相同），奇数切分会翻转相位；`even == i_next` 恰好编码了这一规则，保证左半的最后一个元素与右半的第一个元素不违反交替关系。

## 5. 综合实践

把本讲三个模块串成一个「订单抽查」小工具（**示例代码**）：

```rust
use rayon::prelude::*;

fn main() {
    // 10 万条订单，其中 7 的倍数视为"可疑订单"
    let orders: Vec<u64> = (1..=100_000).collect();
    let is_suspect = |&o: &u64| o % 7 == 0;

    // 1) find 家族：三种语义各取所需
    let any_one = orders.par_iter().find_any(is_suspect);
    let first = orders.par_iter().find_first(is_suspect);
    let last = orders.par_iter().find_last(is_suspect);
    println!("any={:?} first={:?} last={:?}", any_one, first, last);
    // first 应恒为 Some(&7)，last 应恒为 Some(&99995)（100000 以内最大的 7 的倍数）
    // any_one 是三者中任意一个，多次运行可能变化

    // 2) position_*：找到下标而不是值
    let pos = orders.par_iter().position_first(is_suspect);
    println!("position_first={:?}", pos); // 期望 Some(6)

    // 3) take_any + intersperse：抽查 8 条样本做报告
    let samples: Vec<String> = orders
        .par_iter()
        .take_any(8)
        .map(|o| format!("#{o}"))
        .collect::<Vec<_>>()
        .into_par_iter()          // 示例：二次并行化做穿插
        .intersperse("|".to_string())
        .collect();
    println!("samples: {}", samples.concat());
    // 长度应为 15（2*8-1），奇数位全是 "|"，8 个样本保持原相对顺序

    // 4) interleave：把"可疑"与"正常"样本交错生成对照表
    let (sus, ok): (Vec<u64>, Vec<u64>) = orders
        .par_iter()
        .take_any(200)
        .cloned()
        .partition(is_suspect);
    let report: Vec<u64> = sus.into_par_iter().interleave(ok.into_par_iter()).collect();
    println!("report len = {}", report.len());
}
```

验证清单（全部待本地验证）：

1. `first` / `last` / `position_first` 每次运行都返回同一个确定值。
2. `any_one` 的值在多次运行间**允许**变化（也可能碰巧稳定）。
3. `samples` 的长度恒为 15，穿插符位置固定，但 8 个样本成员可能变化。
4. 思考题：第 4 步里 `sus` 与 `ok` 的长度通常不等，`interleave` 的尾接规则会让较长一侧的剩余部分整体接在后面——对照 4.4 的规则解释输出形态。

## 6. 本讲小结

- **`find_any` = 共享 `AtomicBool` + `full()` 钩子**：任一任务命中即置位，全体任务停工；返回「某一个」匹配，结果不确定；`any` / `all` 直接构建在它之上。
- **`find_first` / `find_last` = 虚区间协议**：给无索引消费者分配想象中的位置区间并逐次对半，用区间边界与共享 `AtomicUsize best_found` 的比较实现「向右 / 向左剪枝」；匹配点的另一侧必须扫完，这是确定性换来的性能代价；分辨率耗尽时由方向感知的 Reducer 兜底。
- **`position_*` 是 `map + enumerate + find_*` 的组合**，`take_any` / `skip_any` 是「共享 `AtomicUsize` 配额 + 原子扣减」：数量确定、成员不定、幸存元素的相对顺序保留；`take_any` 配额归零即可全停，`skip_any` 必须陪跑到底。
- **`interleave` / `intersperse` 定义了确定的交错顺序**：前者要求两侧 indexed 并在切分时求解 \(a + b = index\) 的坐标换算，后者按 \(base\_index = (index + !clone\_first)/2\) 换算并以奇偶翻转 `clone_first` 标志；任意切分下顺序不变，由集成测试逐位验证。
- 本讲出现的「包装消费者 + 共享原子变量」骨架与 u2-l5 的 `try_reduce` 一脉相承，是 rayon 里实现**跨任务协同决策**（短路、配额）的通用手法。

## 7. 下一步学习建议

- 下一讲 u3-l5（展平与分块）将继续适配器机制：`flat_map` / `flatten` 的嵌套并行、`flat_map_iter` 的内层串行取舍，以及 `chunks` / `fold_chunks` / `blocks` 分块模式——其中 `blocks` 与本讲 `find_first` 文档提到的 `by_exponential_blocks` 性能提示相关。
- 想深入顺序敏感的切分换算，可回读 u3-l3（zip / take / skip / step_by 的纯下标算术），对比本讲 `InterleaveProducer::split_at` 的「双坐标联立求解」——复杂度明显更高。
- 想追踪 `full()` 钩子在调度器里被询问的位置，可以 `Grep` `\.full()` 在 `src/iter/plumbing/` 下的调用点，并结合 u4-l3（Consumer 与驱动流程）的 bridge 递归讲解。
- `intersperse` 同时实现 indexed 与 unindexed 两条驱动路径，是对比 `drive` / `drive_unindexed` 差异的绝佳样本，建议在进入单元四前重读 intersperse.rs 的 L30-L62。
