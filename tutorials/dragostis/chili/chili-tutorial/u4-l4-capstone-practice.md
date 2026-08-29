# 综合实践：用 chili 实现并行归并排序

## 1. 本讲目标

这是整个学习手册的收官之讲。前面十三讲把 chili 拆开看清楚了，本讲把它装回去：**独立实现一个并行归并排序，调优它，测量它，并写出有理论依据的结论**。完成后你应当能：

1. 把任意分治算法（不只是树求和）映射到 `Scope::join` 上，并保证结果与顺序执行完全一致。
2. 用 `join_with_heartbeat_every` 的 `TIMES` 常量参数和 `Config::heartbeat_interval` 两个旋钮做性能调优，并说清它们各自控制什么。
3. 设计一个「线程数 × 心跳频率」扫描实验：控制变量、设对照组、防抖动、验证正确性，最后用 Brent 定理与 Amdahl 定律解释测到的数字。

本讲的所有排序代码都是**示例代码**（由本讲义提供，不是 chili 仓库的一部分）；引用的 chili 源码均带永久链接与行号。

## 2. 前置知识

本讲默认你已读完前置讲义，这里只提炼最必要的结论：

- **fork-join 与 may-parallel 语义**（u1-l3）：`scope.join(a, b)` 只保证两闭包结果按参数顺序汇合，是否真跨线程由调度决定；闭包与返回值都必须 `Send`。
- **join 的三条路径**（u2-l1）：入口 `join` 固定以 `TIMES=64` 委托 `join_with_heartbeat_every`；`join_count % TIMES == 0` 或本地队列长度 `< 3` 时走 `join_heartbeat`（可能分享任务），否则走零簿记的 `join_seq`。
- **ThreadPool 与 Config**（u2-l2）：`thread_count` 是**含调用线程在内**的总计算线程数，实际 worker 数 \(W = C - 1\)；全局池只能设一次，扫描实验必须用 `with_config` 显式建池。
- **测量方法学**（u4-l3）：对照组（零开销基线）、加速比、每节点摊销耗时；README 的数据来自 `benches/overhead.rs` 的 divan 基准。

此外本讲新增两个性能模型概念（初学者可能不熟，先给直觉）：

- **工作与跨度（work & span）**：把并行计算看成一张依赖图。**工作** \(W\) 是所有节点耗时总和（相当于无限快的单线程串行时间）；**跨度** \(T_\infty\) 是图上最长路径（相当于无限多线程时的下限）。两者的比值 \(P = W / T_\infty\) 叫**并行度**——无论调度多完美，加速比都不可能超过 \(P\)。
- **Amdahl 定律**：若串行分数为 \(1-f\)，则 \(p\) 线程加速比上限为 \(S(p) = 1 / ((1-f) + f/p)\)。它回答「为什么加了线程却快不了多少」。

## 3. 本讲源码地图

| 文件 | 在本讲中的角色 |
| --- | --- |
| [src/lib.rs](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs) | 全部公共 API：`Scope::join` / `join_with_heartbeat_every`、`Config`、`ThreadPool::with_config` / `scope()` / `Drop`；内嵌测试提供写法先例 |
| [benches/overhead.rs](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/benches/overhead.rs) | 基准范式：参数化注册、`bench_local`、循环内嵌断言、一个 `Scope` 复用多次迭代 |
| [README.md](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/README.md) | 参考数据：两台机器的加速比表、每节点开销不随线程数增长的表 |
| （读者自建）`chili-merge-sort/` | 本讲实践的独立 crate，不写入 chili 仓库 |

## 4. 核心概念与源码讲解

### 4.1 分治算法并行化

#### 4.1.1 概念说明

树求和是 chili 的「门面示例」，但真实工作负载往往是**分治算法**：分割输入、递归处理子问题、**合并结果**。归并排序比树求和多出一个「合并」步骤，恰好补上了从示例到实战的最后一块拼图。

chili 对分治算法的适配性来自它的核心设计（README 第 8–13 行的定位）：**大量小计算 + 难以估计分支剩余工作量**。归并排序的递归树正是这样——每个 `join` 点的子问题工作量可以粗算，但配上数据分布与缓存行为后很难精确预估，于是「先把任务挂在本地队列上，等心跳来了再考虑分享」的惰性策略正好合用。

实现前先做三个关键决策：

1. **数据视角**：输入用 `&[u64]` 共享只读、输出归新所有（函数式风格），两个闭包同时捕获不可变引用毫无冲突；这和树求和的形状完全同构。若要原地排序（`&mut [u64]`），则须用 `split_at_mut` 切出互不相交的可变切片——chili 自己的文档示例就是这么写的。
2. **粒度（cutoff）**：递归到某个长度以下就停止分叉、直接调标准库排序。叶片必须有足够工作量，否则 join 簿记开销会淹没计算（README 的 1K 节点行：并行反而慢，x0.53）。
3. **合并放在 join 之后**：`join` 返回即两半皆已完成且结果已汇合，此时在发起线程做顺序归并——「分叉并行、汇合串行」正是 fork-join 的本义。

#### 4.1.2 核心流程

```text
merge_sort(scope, v):
    若 len(v) <= CUTOFF:        # 叶片：顺序排序，不再分叉
        返回 sort_unstable(v 的拷贝)
    mid = len(v) / 2
    (left, right) = scope.join( # 两个闭包各拿 &mut Scope，可继续递归分叉
        |s| merge_sort(s, v[..mid]),
        |s| merge_sort(s, v[mid..]),
    )                            # join 返回 ⇔ 两半都已有序
    返回 merge(left, right)      # 顺序归并，结果按参数顺序对应
```

递归树的规模估算（分析调参时要用）：

- 叶片数约为 \( \lceil n / \text{CUTOFF} \rceil \)（对半切分下，\(n\) 为 2 的幂时恰好相等）；
- `join` 调用次数等于内部节点数，即 \( \#\text{join} = \#\text{叶片} - 1 \)。

例如 \( n = 2^{20} \)（约 100 万）、`CUTOFF = 1024 = 2^{10}`：叶片 1024 片、join 共 1023 次；把 CUTOFF 降到 32，join 暴涨到约 32767 次。CUTOFF 直接控制「喂给调度器的任务数量」。

#### 4.1.3 源码精读

我们要泛化的模板就是 crate 文档里的树求和——闭包收 `&mut Scope` 并继续向下传递 `|s|`：

- [src/lib.rs:L36-L43](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L36-L43)：`sum` 递归。两个闭包各自拿到子 `Scope` 再递归调用 `sum`，`join` 返回 `(left, right)` 后做最终合并（这里是加法 `node.val + left + right`）。归并排序只需把「加法」换成「归并」。
- [src/lib.rs:L692-L714](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L692-L714)：内嵌测试 `join_very_long`，对半切分数组的既定写法：`split_at_mut(mid)` 后 `s.join(|s| increment(s, left), |s| increment(s, right))`。这是**原地版**归并排序分割步骤的现成范式。
- [src/lib.rs:L221-L232](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L221-L232)：`Scope` 的文档示例，展示 `split_at_mut` 产出互不相交的可变切片供两个闭包并行写入——原地排序变体的安全基础。
- [benches/overhead.rs:L58-L65](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/benches/overhead.rs#L58-L65)：`chili_overhead` 基准的 `sum` 递归——README 所有数据的测量对象。我们的排序实验与它同构，只是把节点求和换成了排序，因此测得的行为可以和 README 互相印证。

#### 4.1.4 代码实践

**实践目标**：在独立 crate 中跑通一个正确的并行归并排序（先不管性能）。

**操作步骤**：

1. 新建 crate 并添加依赖（chili 已发布到 crates.io，版本见 [Cargo.toml:L4](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/Cargo.toml#L4)，当前为 0.2.1）：

   ```bash
   cargo new chili-merge-sort
   cd chili-merge-sort
   cargo add chili
   ```

2. 写入以下示例代码（src/main.rs）：

   ```rust
   // 示例代码：第 1 步——正确性优先
   use chili::Scope;

   /// 叶片粒度：不大于该长度就停止分叉，交给标准库
   const CUTOFF: usize = 1024;

   /// 把两个有序切片归并成一个新的有序 Vec
   fn merge(left: &[u64], right: &[u64]) -> Vec<u64> {
       let (mut i, mut j) = (0usize, 0usize);
       let mut out = Vec::with_capacity(left.len() + right.len());
       while i < left.len() && j < right.len() {
           if left[i] <= right[j] {
               out.push(left[i]);
               i += 1;
           } else {
               out.push(right[j]);
               j += 1;
           }
       }
       out.extend_from_slice(&left[i..]);
       out.extend_from_slice(&right[j..]);
       out
   }

   /// 并行归并排序：输入共享只读、输出归新所有，与树求和示例同构
   fn merge_sort(scope: &mut Scope<'_>, v: &[u64]) -> Vec<u64> {
       if v.len() <= CUTOFF {
           let mut leaf = v.to_vec();
           leaf.sort_unstable();
           return leaf;
       }
       let mid = v.len() / 2;
       let (left, right) = scope.join(
           |s| merge_sort(s, &v[..mid]),
           |s| merge_sort(s, &v[mid..]),
       );
       merge(&left, &right)
   }

   fn main() {
       // 确定性伪随机数据（xorshift64），不引入第三方依赖、结果可复现
       let mut x: u64 = 0x9E37_79B9_7F4A_7C15;
       let data: Vec<u64> = (0..1_000_000)
           .map(|_| {
               x ^= x << 13;
               x ^= x >> 7;
               x ^= x << 17;
               x
           })
           .collect();

       let mut expected = data.clone();
       expected.sort_unstable();

       let got = merge_sort(&mut Scope::global(), &data);

       assert_eq!(got, expected); // 正确性以标准库为裁判
       println!("1M u64 sorted correctly");
   }
   ```

3. `cargo run --release`（务必 `--release`，debug 下原子操作与闭包都不具代表性）。

**需要观察的现象**：程序打印 `1M u64 sorted correctly`，无 panic、无死锁。

**预期结果**：断言通过即正确性达成。may-parallel 语义保证无论任务是否真的跨线程，输出都与顺序执行一致；此时耗时多少并不重要。具体耗时期待第 4.3 节测量。

#### 4.1.5 小练习与答案

1. **\( n = 2^{20} \)、CUTOFF = 1024 时递归树有多少叶片、多少次 `join`？**
   答：对半切分恰好每层减半，叶片全部落在深度 10：叶片 \( 2^{10} = 1024 \) 片，`join` 次数 \( = 1024 - 1 = 1023 \)。\(n\) 不是 2 的幂或 CUTOFF 不整除时为近似值。
2. **两个闭包同时使用 `v` 为什么合法？若要做原地排序呢？**
   答：两个闭包对 `v` 都只是不可变借用（`&[u64]`），多个读共享不冲突；`join` 只要求闭包与结果 `Send`。原地排序需要两个可变切片，必须 `split_at_mut` 保证互不相交——即 [src/lib.rs:L227](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L227) 文档示例与 `join_very_long` 的写法。
3. **把 CUTOFF 改成 1（递归到底）会发生什么？为什么？**
   答：`join` 次数涨到 \( 2^{20} - 1 \approx 100\) 万次，而每片叶子只剩一次比较。对照 README 的 1K 节点行（并行 x0.53、每节点摊销 3.5ns）：当叶片工作量与 join 簿记同量级时，开销淹没收益。粒度选择的原则是**叶片工作量 >> 单次 join 开销**。

### 4.2 心跳参数调优

#### 4.2.1 概念说明

chili 暴露了两个正交的「心跳旋钮」，分别位于任务分享链路的两端：

| 旋钮 | 位置 | 控制什么 | 默认值 |
| --- | --- | --- | --- |
| `TIMES` | 调用侧（`join_with_heartbeat_every`） | 每 `TIMES` 次 join 才**查一次**心跳旗（降频检查） | 64 |
| `heartbeat_interval` | 心跳线程侧（`Config`） | 心跳线程隔多久**升一次**旗 | 100µs |

两者正交：一个决定「多久抬头看一眼」，一个决定「旗多久被立起来一次」。此外还有一个**隐式旋钮**——递归形状本身：本地队列长度 `< 3` 时无条件走心跳路径，因此对半切分的归并排序在最上面几层（队列尚短）总是走 `join_heartbeat`。

从 join 调用到任务真正被偷走，时延链是三段之和：

\[
T_{\text{偷走}} \;\le\; \underbrace{(TIMES - 1) \cdot t_{join}}_{\text{等计数归零}} \;+\; \underbrace{heartbeat\_interval}_{\text{等心跳升旗}} \;+\; \underbrace{t_{wake}}_{\text{worker 被唤醒}}
\]

调小的收益是任务更早被分享、worker 更早有活干；代价分别是热路径查旗更频繁（Relaxed 原子读，廉价但非零）与心跳线程更忙碌（每轮持锁扫描全部心跳登记项）。

#### 4.2.2 核心流程

```text
join_with_heartbeat_every::<TIMES>(a, b):
    join_count = (join_count + 1) mod TIMES
    若 join_count == 0 或 本地队列长度 < 3:
        join_heartbeat(a, b)   # a 入队 → 查旗 →（旗立着则 heartbeat() 送货上货架）→ 先执行 b
    否则:
        join_seq(a, b)         # 零簿记顺序执行
```

心跳线程侧（execute_heartbeat）：被唤醒后遍历所有登记的心跳，把到期（距上次 ≥ `heartbeat_interval`）的旗置位，然后睡眠 `heartbeat_interval / N`（N 为心跳数）——间隔在线程间**均摊**，保证每个旗的周期落在 \([H,\; H + H/N]\)。

#### 4.2.3 源码精读

- [src/lib.rs:L438-L456](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L438-L456)：`join_with_heartbeat_every` 全貌。[L449](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L449) 用 `wrapping_add(1) % TIMES` 计数，[L451](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L451) 的双条件 `join_count == 0 || job_queue.len() < 3` 决定走哪条路径。泛型参数 `TIMES` 是 const 泛型，调用点写 `::<8, _, _, _, _>` 即可换档。
- [src/lib.rs:L416](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L416)：普通 `join` 就是固定 `TIMES = 64` 的转发——这是作者在树求和负载上选定的默认折中，我们的实验就是要检验它在排序负载上是否仍然合适。
- [src/lib.rs:L465-L475](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L465-L475)：`Config` 的两个字段与默认值；`heartbeat_interval` 默认 `Duration::from_micros(100)`。
- [src/lib.rs:L716-L747](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L716-L747)：**调参先例**。`join_wait` 测试同时把两个旋钮拧到极致——[L720](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L720) 设 `heartbeat_interval = 1µs`、[L731](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L731) 用 `join_with_heartbeat_every::<1, …>`——因为测试只有毫秒级寿命，默认参数下心跳可能一次都不触发，跨线程分享就无法断言。这告诉我们：**参数选择必须与负载时间尺度匹配**。
- [src/lib.rs:L165-L178](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L165-L178)：心跳线程的升旗逻辑——`retain` 持锁遍历全部心跳，仅对到期者 `store(true, Relaxed)` 并更新时间戳。间隔调得越小，这段 Θ(N) 扫描跑得越频繁，锁争用与 CPU 消耗随之上升。

#### 4.2.4 代码实践

**实践目标**：把 `TIMES` 变成排序函数自身的常量参数，并能手动换档对比。

**操作步骤**：

1. 利用 const 泛型沿递归传播 `TIMES`（示例代码）：

   ```rust
   // 示例代码：TIMES 提升为函数常量参数
   fn merge_sort<const TIMES: u8>(scope: &mut Scope<'_>, v: &[u64]) -> Vec<u64> {
       if v.len() <= CUTOFF {
           let mut leaf = v.to_vec();
           leaf.sort_unstable();
           return leaf;
       }
       let mid = v.len() / 2;
       let (left, right) = scope.join_with_heartbeat_every::<TIMES, _, _, _, _>(
           |s| merge_sort::<TIMES>(s, &v[..mid]),
           |s| merge_sort::<TIMES>(s, &v[mid..]),
       );
       merge(&left, &right)
   }
   ```

2. 在 `main` 里分别用 `merge_sort::<8>(…)`、`::<64>`、`::<256>` 排同一份数据，各跑 3 次，用 `std::time::Instant` 粗测耗时。
3. 想想每档下查旗次数：n=2²⁰、CUTOFF=1024 时总 join 约 1023 次，TIMES=8 约查旗 128 次，TIMES=256 约查旗 4 次（注意计数从 1 开始：首次 join 时 `join_count` 为 1，非 0）。

**需要观察的现象**：三档结果都正确（断言通过）；耗时有差异但可能不大。

**预期结果**：定性上，TIMES 小 → 任务更早被送上货架 → 多线程档位（下一节的 C=4/8）更可能受益；单线程时三档差异应淹没在噪声里（没人来偷任务，查旗是白查）。具体差值**待本地验证**——CUTOFF=1024 时 join 只有约一千次，查旗开销总量本身很小，差异可能要等 CUTOFF 调小、join 数上万后才明显。

#### 4.2.5 小练习与答案

1. **TIMES=8 与 TIMES=256 各自的取舍是什么？**
   答：TIMES=8 查旗密度高（每 8 次 join 一次），任务分享时延低，但热路径进入 `join_heartbeat` 分支的频率升高（入队/出队 + Relaxed 读旗）；TIMES=256 簿记最省，但旗立了也可能要等最多 255 次 join 才被看见，worker 空转时间变长。默认 64 是树求和基准上的折中，不同负载最优值不同——这正是本实验要测的东西。
2. **`join_wait` 测试为什么要把 `heartbeat_interval` 和 `TIMES` 同时调到最小？只调一个行不行？**
   答：不行。只调 `TIMES=1` 而心跳间隔仍是 100µs：测试毫秒级寿命内心跳线程可能从未升旗，`heartbeat()` 里的投递分支从不触发，任务从未跨线程。只调间隔而 `TIMES=64`：join 次数可能不足以让 `join_count` 归零（还要叠加 `queue.len() < 3` 的豁免条件）。两个旋钮必须同时开到最大频率，跨线程路径才必然被执行（且测试为单核环境准备了人工通过出口，见 [src/lib.rs:L757-L762](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L757-L762)）。
3. **把 `heartbeat_interval` 配成 1 纳秒有什么坏处？**
   答：`execute_heartbeat` 的睡眠步长退化为忙循环节奏，每轮都持锁 `retain` 扫描全部心跳登记项（[src/lib.rs:L166-L176](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L166-L176)），心跳线程吃满一个核并加剧 `Context` 锁争用——恰好压在被所有 worker 共享的那把锁上。反之配成 1 秒，短任务全程等不到一次心跳、纯串行。100µs 默认值意味着只有运行 **数百毫秒以上** 的负载才真正依赖心跳分享。

### 4.3 多线程性能对比

#### 4.3.1 概念说明

有了正确的实现和可换档的参数，剩下的问题是设计一个**可信的实验**。三个要素：

- **对照组**：(a) 标准库 `sort_unstable` 作外部基线；(b) `thread_count = 1` 的 chili 档——0 个 worker、一切本地执行，测得「算法本身 + chili 纯簿记」，对应 README 中开销表的 1 thread 列思路。
- **自变量与因变量**：自变量是 `thread_count`（1/2/4/8）与 `TIMES`（8/64/256）；因变量取中位耗时，再换算加速比 \( S(p) = T_1 / T_p \) 与效率 \( E(p) = S(p) / p \)。
- **防污染**：每个配置新建 `ThreadPool`（用完 Drop，线程全部 join 回收）；同一份输入数据；每档重复多次取中位数；每次运行都内嵌正确性断言——这是从 `benches/overhead.rs` 学来的做法（基准循环里就带 `assert_eq!`）。

**先算理论预期，再对照实测。** 对采用**顺序归并**的归并排序：

- 工作 \( W = \Theta(n \log n) \)：每层归并共 \( \Theta(n) \)，共 \( \log_2 n \) 层；
- 跨度 \( T_\infty = \Theta(n) \)：关键路径是「顶层归并 \( n \) + 某一侧子树归并 \( n/2 + n/4 + \cdots \approx n \)」，共约 \( 2n \) 个元素步——**归并本身是顺序的**，它决定了跨度的量级；
- 并行度 \( P = W / T_\infty \approx \log_2 n / 2 \)。\( n = 2^{20} \) 时 \( P \approx 10 \)。

由 Brent 定理 \( T_p \le W/p + T_\infty \)，8 线程的加速比满足

\[
S(8) \;\ge\; \frac{8}{1 + 8/P} = \frac{8}{1 + 0.8} \approx 4.4,
\qquad\text{同时}\qquad S(8) \;\le\; \min(8,\; P) = 8 .
\]

即理想调度下预期落在约 4.4～8 之间，**实际调度器、内存带宽与任务分享时延只会让它更低**。对比树求和（跨度 \( \Theta(\log n) \)、并行度 \( \Theta(n) \)，README 实测 x7.83 逼近 8 核上限），就能解释为什么归并排序注定到不了同样的高度——这不是 chili 的问题，是算法的顺序归并决定的。把这一点写进结论，就是「有依据的结论」。

#### 4.3.2 核心流程

```text
扫描流程（单进程内串行完成）:
    生成数据一次（xorshift64，确定性）→ 生成 expected 一次
    基线: std sort_unstable × REPS → 中位数 T_base
    for C in [1, 2, 4, 8]:
        for TIMES in [8, 64, 256]:
            with_config(thread_count=C) 建池        # W = C - 1 个 worker
            scope = pool.scope()                     # 一个 Scope 复用全部重复（仿基准）
            预热 1 次（不计入）
            计时 REPS 次，每次断言结果 == expected    # 内嵌正确性验证
            池离开作用域 → Drop: 置停机旗 → 唤醒 → join 全部线程
            记录中位数，打印表格行（耗时 + 加速比 + 效率）
```

#### 4.3.3 源码精读

- [src/lib.rs:L513-L546](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L513-L546)：`with_config`。[L514-L518](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L514-L518) 把 `thread_count` 减一得到 worker 数——所以配置 `C=1` 意味着零 worker 的纯本线程执行；[L537](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L537) 的 barrier 保证池构造返回时所有 worker 已就绪，计时不含线程启动的抖动。
- [src/lib.rs:L604-L606](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L604-L606)：`scope()` 从池借出 `Scope`。创建 `Scope` 会登记一个心跳并唤醒心跳线程（[src/lib.rs:L254-L259](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L254-L259)）。
- [src/lib.rs:L615-L633](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L615-L633)：`Drop` 的停机顺序——持锁置 `is_stopping`、`notify_all` 唤醒 worker、`notify_one` 唤醒心跳线程、逐个 `join`。**正因为 Drop 会干净回收线程，扫描实验才敢在每个档位重建池**，档位之间互不渗漏。
- [benches/overhead.rs:L20-L23](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/benches/overhead.rs#L20-L23)：`LAYERS` 常量 + `nodes()` 把参数组喂给属性宏——一个基准函数自动展开成多个规模，我们的「配置循环」就是它的手写版。
- [benches/overhead.rs:L49-L53](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/benches/overhead.rs#L49-L53)：`no_overhead` 基准在循环外创建一次 `Scope`、循环内 `bench_local` 反复执行并**内嵌 `assert_eq!`**。两个可借鉴的细节：`Scope` 跨迭代复用是官方认可用法；测得快不代表测得对，断言必须跟着计时走。
- [README.md:L64-L67](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/README.md#L64-L67)：chili 每节点开销在 1～8 线程下恒为 3.5ns 的表格——开销属于发起线程的本地簿记、不随线程数增长的实证。我们的 C=1 档正是同一思路在排序负载上的复刻。

#### 4.3.4 代码实践

**实践目标**：跑通完整扫描，产出一张「线程数 × TIMES」的耗时表。

**操作步骤**：

1. 把 main.rs 扩充为测量入口（示例代码）：

   ```rust
   // 示例代码：src/main.rs（完整测量入口，concat 接 4.1/4.2 的 merge/merge_sort）
   use std::{
       num::NonZero,
       time::{Duration, Instant},
   };
   use chili::{Config, Scope, ThreadPool};

   const N: usize = 1_000_000;
   const REPS: usize = 5;
   const THREAD_COUNTS: [usize; 4] = [1, 2, 4, 8];
   const TIMES_CHOICES: [u8; 3] = [8, 64, 256];

   fn run_once<const TIMES: u8>(
       scope: &mut Scope<'_>,
       data: &[u64],
       expected: &[u64],
   ) -> Duration {
       let start = Instant::now();
       let got = merge_sort::<TIMES>(scope, data);
       let elapsed = start.elapsed();
       assert_eq!(got, expected); // 仿基准：每次计时都验证正确性
       elapsed
   }

   fn sweep_config(thread_count: usize, times: u8, data: &[u64], expected: &[u64]) -> Duration {
       let pool = ThreadPool::with_config(Config {
           thread_count: NonZero::new(thread_count),
           heartbeat_interval: Duration::from_micros(100), // 先固定，聚焦 TIMES
       });
       let mut scope = pool.scope();

       let mut samples: Vec<Duration> = match times {
           8 => (0..=REPS).map(|_| run_once::<8>(&mut scope, data, expected)).collect(),
           64 => (0..=REPS).map(|_| run_once::<64>(&mut scope, data, expected)).collect(),
           256 => (0..=REPS).map(|_| run_once::<256>(&mut scope, data, expected)).collect(),
           _ => unreachable!(),
       };
       samples.sort();
       samples[REPS / 2] // 取中位数（首轮兼作预热）
   } // pool 在此 Drop：停机旗 + notify + join，档位间零渗漏

   fn main() {
       let mut x: u64 = 0x9E37_79B9_7F4A_7C15;
       let data: Vec<u64> = (0..N)
           .map(|_| { x ^= x << 13; x ^= x >> 7; x ^= x << 17; x })
           .collect();
       let mut expected = data.clone();
       expected.sort_unstable();

       // 外部基线：标准库
       let mut base = Vec::with_capacity(REPS);
       for _ in 0..REPS {
           let mut v = data.clone();
           let start = Instant::now();
           v.sort_unstable();
           base.push(start.elapsed());
       }
       base.sort();
       let t_base = base[REPS / 2];

       println!("| 配置 | 中位耗时 | 加速比(对 std) | 效率 S/p |");
       println!("|---|---:|---:|---:|");
       println!("| std::sort_unstable | {:?} | x1.00 | - |", t_base);

       for &c in &THREAD_COUNTS {
           for &t in &TIMES_CHOICES {
               let d = sweep_config(c, t, &data, &expected);
               let s = t_base.as_secs_f64() / d.as_secs_f64();
               println!("| C={c} TIMES={t} | {d:?} | x{s:.2} | {:.2} |", s / c as f64);
           }
       }
   }
   ```

   注意 `NonZero::new(thread_count)` 返回 `Option<NonZero<usize>>`，恰好就是 `Config::thread_count` 字段的类型。
2. `cargo run --release`。
3. 记录机器核数；若核数不足 8，C=8 属于超订，结论要注明。

**需要观察的现象**：C=1 行耗时与 std 基线同量级（略慢）；随 C 增大耗时先降后趋于平缓；同一 C 下不同 TIMES 的差异相对线程数效应偏小。

**预期结果**：加速比随 C 上升但**明显低于线性**——依据 4.3.1 的推导，n=2²⁰ 时并行度上限 \( P \approx 10 \)，C=8 的合理预期在 x4～x6 区间（理想调度下界约 4.4）。C=1 与 TIMES 无关（无人偷任务）。具体数字**待本地验证**；若实测远离该区间，按 4.3.5 第 3 题的排查路径找原因。

#### 4.3.5 小练习与答案

1. **为什么每个档位都要 `with_config` 新建池，而不能全用 `Scope::global()`？**
   答：全局池由 `OnceLock` 惰性初始化、只能设置一次（[src/lib.rs:L565-L586](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L565-L586)），且默认用 `available_parallelism`——线程数不可控、心跳间隔固定 100µs，两个自变量都动不了。显式 `with_config` + 离开作用域触发 `Drop`（[src/lib.rs:L615-L633](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L615-L633)）才能保证每个数据点在自己的线程组里测得。
2. **C=1 档测到的数字有什么独立价值？**
   答：`C=1` 经 `with_config` 减一后 worker 数为 0，所有 join 只走本地路径（`join_seq` 或入队再收回的 `join_heartbeat`），没有任务分享、没有锁争用——它 isolates 出「chili 簿记 + 归并算法本身」的串行成本，是天然的内部基线，对应 README 开销表（[README.md:L64-L67](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/README.md#L64-L67)）的 1 thread 列。它对 std 基线的偏差就是该负载下的纯框架开销。
3. **若 C=8 只测到 x2.5，给出三个可能原因及各自的验证办法。**
   答：(a) **算法并行度上限**——顺序归并使 \( T_\infty = \Theta(n) \)、\( P \approx \log_2 n / 2 \approx 10 \)，加上调度损耗 x4～x6 才是合理预期，x2.5 可能只是调度不理想；验证：把 n 提到 8 个量级（2²³）看 S 是否随 \( \log n \) 抬升。(b) **内存带宽饱和**——归并是流式访存，多线程同抢内存控制器；验证：元素改 `u32` 或减小 n 重测，若 S 比例上升则带宽是瓶颈。(c) **任务分享时延**——心跳太稀导致 worker 空转；验证：把 `heartbeat_interval` 降到 10µs、`TIMES` 用 8 档重测，S 明显改善即坐实。

## 5. 综合实践

把三块拼成完整闭环。在第 4.3 节程序的基础上完成：

1. **正确性闭环**：已由 `assert_eq!(got, expected)` 保证——任何调参（CUTOFF、TIMES、线程数）后都不许摘掉。
2. **扩展扫描一个新维度**：把 `CUTOFF` 也变成扫描项（如 64 / 1024 / 16384 三档），固定 C=机器核数、TIMES=64，观察粒度对加速比的影响。预期方向：CUTOFF 太小 → join 数以万计、簿记摊薄不掉；太大 → 任务太少、worker 喂不饱。中间存在一个谷底。**待本地验证**。
3. **写出结论**（这是本实践的交付物，不少于如下四点，每点都要有表中数据或 4.3.1 的公式支撑）：
   - 并行收益最大的条件：n 足够大（\( \gg \) CUTOFF）、C 不超过并行度上限 \( P \approx \log_2 n / 2 \)、叶片工作量远大于单次 join 开销（约 3.5ns/节点）；
   - 顺序归并造成的理论上限，及实测与 Brent 下界 \( S(8) \ge 8/(1+8/P) \) 的距离；
   - TIMES 与 heartbeat_interval 的实测影响及其随 C 的变化（单线程应无差异）；
   - C=1 档与 std 基线之差给出的框架纯开销。
4. **可选进阶**（任选其一）：
   - 用 `split_at_mut` 改写为原地排序变体（范式见 [src/lib.rs:L221-L232](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L221-L232) 与 `join_very_long`），消除每层归并的堆分配，再重测一遍——预期函数式版本的分配流量是主要浪费；
   - 加一个 `rayon` 依赖，照 [benches/overhead.rs:L75-L91](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/benches/overhead.rs#L75-L91) 的 `rayon_overhead` 写法用 `rayon::join` 实现同构排序，作生态对照列；
   - 把实验 divan 化：仿 [benches/overhead.rs:L20-L25](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/benches/overhead.rs#L20-L25) 用属性宏参数化 CUTOFF，得到统计更严谨的数字。

## 6. 本讲小结

- 分治算法到 `Scope::join` 的映射是机械的：分割 → `join` 里并行递归（闭包收 `&mut Scope` 继续传）→ `join` 返回后顺序合并；正确性由 may-parallel 语义无条件保证，性能由粒度（CUTOFF）与调度决定。
- 两个正交的心跳旋钮——调用侧 `TIMES`（查旗降频）与 `Config::heartbeat_interval`（升旗节奏）——前者用 const 泛型沿递归传播即可换档；参数必须与负载时间尺度匹配（`join_wait` 测试的 1µs + `::<1>` 是极端先例）。
- 可信的扫描实验 = 外部基线 + C=1 内部基线 + 每档独立建池（`Drop` 干净回收）+ 中位数抗抖动 + 计时内嵌正确性断言。
- 顺序归并使归并排序的跨度 \( T_\infty = \Theta(n) \)、并行度 \( P \approx \log_2 n / 2 \)，Brent 定理给出 \( S(8) \ge 8/(1 + 8/P) \approx 4.4 \)——加速比远低于树求和的 x7.83 是算法属性，不是库的缺陷。
- 「何时并行收益最大」的判据链条：叶片工作量 >> 3.5ns、\( n/\text{CUTOFF} \) 决定 join 次数、\( \log_2 n / 2 \) 决定加速比天花板、\( C \le P \) 决定线程投入的边际收益。

## 7. 下一步学习建议

至此手册正文完结。三个方向继续深入：

1. **回到源码带着实战问题重读**：现在你知道 TIMES 调优的痛点了吗？重读 [src/lib.rs:L438-L456](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L438-L456)，思考「自适应 TIMES」（如按队列长度动态降频）是否可行、会不会破坏 `join_count` 的 u8 语义——有兴趣可以把想法做成 PR。
2. **横向对比**：读 Spice 原版（README 链接）看同一机制在 OCaml/C 里的形态；读 rayon 的工作窃取调度器，对比「惰性心跳分享」与「主动窃取」在归并排序这种跨度受限负载上的表现差异。
3. **迁移到自己的项目**：找一个你手头「大量小任务 + 难估剩余量」的真实负载（解析、遍历、仿真），按本讲的流程走一遍：正确性映射 → CUTOFF 定粒度 → 线程与心跳扫描 → 写结论。能独立完成这一步，才算真正掌握了 chili。
