# 基准测试与性能分析

## 1. 本讲目标

学完本讲，你应该能够：

1. 会运行 `cargo bench --bench overhead`，并看懂 divan 输出的每一行数字代表什么。
2. 精读 [benches/overhead.rs](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/benches/overhead.rs) 中三个基准的代码，理解 `no_overhead` / `chili_overhead` / `rayon_overhead` 各自测的是什么、为什么这样设计。
3. 理解「对照实验」的思路：为什么需要一个零开销的理论上限做 baseline，才能量化 chili 的调度开销。
4. 能独立解读 README 中的基准表格：会算加速比、会算每节点摊销耗时，并解释「1K 节点时并行反而更慢（x0.53）」这个临界点现象——并用 u2-l1 学过的 `join` 三条路径知识给出机制层面的解释。

## 2. 前置知识

### 2.1 什么是基准测试（benchmark）

基准测试是「用一段固定的工作负载，反复测量程序耗时」的实验。它和单元测试不同：单元测试回答「对不对」，基准回答「多快」。微基准（microbenchmark）尤其难做，因为：

- 编译器可能把没有副作用的计算整个优化掉，让你测到「空气」；
- 计时器本身有开销，被测代码太小时误差会淹没信号；
- 机器状态（CPU 频率、缓存冷热、其他进程）会引入抖动。

所以成熟的基准框架会做统计（多次迭代、取最值/均值、报告偏差），而被测代码里要有「不可被优化掉」的结果消费。

### 2.2 divan 是什么

divan 是一个 Rust 基准测试框架（chili 中以 dev 依赖引入，版本 0.1.14）。它的用法是：给函数打上 `#[divan::bench]` 属性宏，框架会自动生成注册代码，由 `divan::main()` 统一驱动，对每个基准反复迭代并输出统计耗时。chili 的基准没有用 Rust 内置的 `#[bench]`（那只支持 nightly），而是在 Cargo.toml 里声明 `harness = false`，自带一个调用 `divan::main()` 的 `main` 函数——第一讲已见过这个布局，本讲我们走进它的内部。

### 2.3 三个关键度量

- **加速比（speedup）**：\[ \text{加速比} = \frac{T_{\text{baseline}}}{T_{\text{并行}}} \] 即「串行版耗时 ÷ 并行版耗时」。x7.83 意味着并行版比串行基线快 7.83 倍；8 核机器的理论上限是 x8。
- **每节点摊销耗时**：树求和的总耗时除以节点数。它是衡量「单次 fork-join 开销」的尺子，因为树上每个节点恰好对应一次 `join` 调用（u1-l3 已论证：L 层满二叉树的节点数与 join 次数都是 \(2^L - 1\)）。
- **对照（control）**：科学实验里，为了证明「是 X 导致了差异」，需要一个不含 X 但其他条件完全相同的组。本讲的 `no_overhead` 基准就是 chili 的对照组。

### 2.4 与前面讲义的衔接

- u1-l1 已经读过 README 的基准表，本讲回答「这些数字是怎么测出来的」。
- u2-l1 精读过 `join` 的三条执行路径：`join_count % TIMES` 降频、`job_queue.len() < 3` 无条件走心跳路径、`join_heartbeat` 的入队—执行—取回流程。本讲把这些机制和 1K 节点的 3.5ns/节点开销对上号。
- u3-l3 讲过 `Scope` 内的 `JobQueue` 含 `Cell` 槽位——这解释了本讲基准为什么必须用 `bench_local`。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [benches/overhead.rs](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/benches/overhead.rs) | 本讲主角：三个对照基准的全部代码，共 95 行 |
| [README.md](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/README.md) | 基准结果表格与作者的解读注释，是我们要复现的目标 |
| [Cargo.toml](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/Cargo.toml) | dev 依赖（divan、rayon）与 `[[bench]] harness = false` 声明 |
| [src/lib.rs](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs) | 只引用几处：`Scope::global`、`join` 的 TIMES=64、默认 `Config`，用于把基准行为连接到调度机制 |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**divan 基准框架** → **对照实验设计** → **结果解读**。顺序是先会跑、再懂测什么、最后会读数。

### 4.1 divan 基准框架

#### 4.1.1 概念说明

chili 的基准目标（bench target）叫 `overhead`，是一个独立的可执行程序：Cargo 以 `harness = false` 声明它不使用 libtest 的测试 harness，而是执行自己的 `main`。`main` 只有一行——把控制权交给 divan，由 divan 发现所有打了 `#[divan::bench]` 属性的函数、逐个逐参数组地迭代计时。

理解本模块要抓住三点：

1. **属性宏注册**：`#[divan::bench(args = ...)]` 在编译期把普通函数改写成「向 divan 注册的基准项」，`args` 指定参数组，每组参数会各自独立测量并各占一行输出。
2. **`bench_local` 的线程语义**：`Bencher::bench_local` 保证被测闭包始终在**发起基准的当前线程**上执行。这对 chili 至关重要——`Scope` 是绑定发起线程的对象（它的 `job_queue` 是 `ThreadJobQueue`，内部还有 `Cell`，不是 `Sync` 的），fork-join 的「发起者」必须是同一个线程。
3. **防优化**：基准闭包里用 `assert_eq!` 检查求和结果，迫使编译器真的执行整棵树的计算。

#### 4.1.2 核心流程

```text
cargo bench --bench overhead
  └─ Cargo 编译 benches/overhead.rs 为独立可执行文件（不跑 libtest harness）
       └─ main() → divan::main()
            └─ 扫描所有 #[divan::bench] 函数（共 3 个）
                 └─ 对每个基准 × 每组 args（2 组：(10, 1023) 和 (24, 16777215)）
                      ├─ 先执行函数体一次性的 setup（建树、取 Scope）
                      └─ 反复调用 bench_local 的闭包，统计每次迭代耗时
                 └─ 输出统计表（每基准每参数一行）
```

#### 4.1.3 源码精读

**基准目标的声明与入口。** [Cargo.toml:14-20](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/Cargo.toml#L14-L20) 中，divan 与 rayon 只是 dev 依赖（不进入下游用户的依赖树，u1-l2 已确认），`[[bench]]` 段的 `harness = false` 表示这个基准自带入口：

```toml
[dev-dependencies]
divan = "0.1.14"
rayon = "1.10.0"

[[bench]]
name = "overhead"
harness = false
```

[benches/overhead.rs:93-95](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/benches/overhead.rs#L93-L95) 就是这个自带入口——`main` 只做一件事：把控制权交给 divan，让它驱动所有已注册的基准：

```rust
fn main() {
    divan::main();
}
```

**参数组的生成。** [benches/overhead.rs:20-23](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/benches/overhead.rs#L20-L23) 定义了基准的「实验变量」：树的层数。`LAYERS` 目前是 `&[10, 24]`，`nodes()` 把每层层数 `l` 映射成二元组 `(l, (1 << l) - 1)`——第二个分量是满二叉树的节点数（\(2^l - 1\)），预先算好传给基准，既用于建树也用于结果断言：

```rust
const LAYERS: &[usize] = &[10, 24];
fn nodes() -> impl Iterator<Item = (usize, usize)> {
    LAYERS.iter().map(|&l| (l, (1 << l) - 1))
}
```

注意一个诚实的事实：README 表格里有 1023、16777215、134217727（AMD）和 67108863（M1）四档规模，而当前的 `LAYERS = &[10, 24]` 只能复现前两档。README 中 26、27 层的数据是作者在更全的配置下测得的；想复现那两行，需要自己把层数加进 `LAYERS`（综合实践会做类似的事）。

**`#[divan::bench]` 属性与 `bench_local`。** 以 `chili_overhead` 为例，[benches/overhead.rs:56-73](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/benches/overhead.rs#L56-L73) 展示了 divan 基准的标准形状：

```rust
#[divan::bench(args = nodes())]
fn chili_overhead(bencher: Bencher, nodes: (usize, usize)) {
    fn sum(node: &Node, scope: &mut Scope<'_>) -> u64 { /* 递归求和 */ }

    let tree = Node::tree(nodes.0);          // setup：建树，不计入迭代
    let mut scope = Scope::global();         // setup：取全局作用域

    bencher.bench_local(move || {
        assert_eq!(sum(&tree, &mut scope), nodes.1 as u64);  // 被计时的迭代体
    });
}
```

三个细节值得指出：

- 函数体先执行**一次性的 setup**（`Node::tree` 和 `Scope::global`），只有传给 `bench_local` 的闭包才是被反复计时的对象——建树的时间不会污染测量。
- `bench_local`（而非 `bench`）保证迭代闭包始终在当前线程执行。这正好匹配 `Scope` 的语义：看 [src/lib.rs:234-239](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L234-L239)，`Scope` 持有 `job_queue: ThreadJobQueue` 与 `join_count`，是发起线程的私有状态（u3-l3 讲过队列里有 `Cell` 槽位，非 `Sync`）；同一棵树、同一个 `scope` 跨迭代复用，必须由同一线程驱动。
- 迭代体以 `assert_eq!(sum(&tree, &mut scope), nodes.1 as u64)` 收尾：既验证并行结果正确（每个节点 val 都是 1，总和等于节点数），又让编译器无法删掉计算。

[benches/overhead.rs:10-18](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/benches/overhead.rs#L10-L18) 的 `Node::tree` 递归构建满二叉树，所有节点 `val: 1`，`layers == 1` 时为叶子（`then` 返回 `None`）：

```rust
pub fn tree(layers: usize) -> Self {
    Self {
        val: 1,
        left: (layers != 1).then(|| Box::new(Self::tree(layers - 1))),
        right: (layers != 1).then(|| Box::new(Self::tree(layers - 1))),
    }
}
```

顺带一个实用提醒：`Node` 是 `u64` 加两个 `Option<Box<Node>>`，每节点 24 字节再加分配器开销，24 层约 1677 万节点要吃掉**数百 MB 内存**，且建树本身要花不少时间——第一次跑 24 层基准时「卡住」是正常现象，耐心等。

#### 4.1.4 代码实践

1. **实践目标**：跑通基准，学会用名称过滤器缩小运行范围，认识 divan 的输出形态。
2. **操作步骤**：
   - 在项目根目录运行 `cargo bench --bench overhead no_overhead`——只运行名字匹配 `no_overhead` 的基准（divan 支持在命令行末尾追加基准名做过滤；具体可用 `cargo bench --bench overhead -- --help` 查看当前版本支持的选项）。
   - 观察输出：应能看到 `no_overhead` 基准下两个参数组（对应 1023 与 16777215 节点）各一行的耗时统计。
   - 再运行完整基准 `cargo bench --bench overhead`（24 层建树较慢、内存较大，预留几分钟）。
3. **需要观察的现象**：每个基准 × 参数组占一行；数字是**每次迭代**（即整棵树求和一次）的耗时量级——1023 节点应在微秒级，16777215 节点应在几十毫秒级。
4. **预期结果**：三个基准都能跑完且断言不触发（结果正确）；同一参数组下 `no_overhead` 最快、`chili_overhead` 居中或在大树时最快。具体数值以本机输出为准，与 README 的差距属正常（机器不同）。本实践**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `chili_overhead` 用 `bench_local` 而不是 `bench`？如果强行换成 `bench`，最可能在哪一步出问题？

**答案**：`bench_local` 保证迭代闭包始终在当前线程执行。`Scope` 持有 `ThreadJobQueue` 与 `Cell` 槽位（非 `Sync`），且 fork-join 语义要求「发起 join 的线程」固定；若换成要求跨线程共享例程的 `bench`，捕获了 `&mut scope` 的闭包不满足线程安全约束，编译就会失败（或语义上破坏 Scope 的线程绑定）。

**练习 2**：如果把 `let tree = Node::tree(nodes.0);` 移进 `bench_local` 的闭包里，测出来的还是「求和的开销」吗？

**答案**：不是。divan 只对传给 `bench_local` 的闭包计时，setup 放在闭包外不计入。移进去后每次迭代都包含建树时间，测到的是「建树 + 求和」的总和，基准就失真了。

### 4.2 对照实验设计

#### 4.2.1 概念说明

`benches/overhead.rs` 里三个基准不是三个孤立的测试，而是一组精心控制的**对照实验**：它们用同一棵树、同一个递归形状、几乎同一份 `sum` 代码，唯一变量是「fork 点的调度策略」。

- **`no_overhead`（对照组 / 理论上限）**：定义一个本地函数 `join_no_overhead`，函数体就是 `(a(scope), b(scope))`——直接顺序调用两个闭包，零调度、零原子操作、零入队出队。它回答的问题是：**「如果 fork-join 完全免费（但也不并行），这棵树多快跑完？」**。任何并行库的成绩都不可能显著好过它（并行最多再快到核数分之一），所以它是理论上限的参照系。
- **`chili_overhead`（实验组）**：把 fork 点换成 `scope.join`，也就是 u2-l1 精读过的真实路径。
- **`rayon_overhead`（生态对照组）**：把 fork 点换成 `rayon::join`——业界最常用的 fork-join 实现，用来回答「chili 相对于主流方案的位置」。

这样设计的聪明之处在于**消除了混杂变量**：三个 `sum` 的闭包形状、树的内存布局、断言方式完全一致，那么三者耗时之差就只能归因于调度路径本身。进一步，chili 的**净调度开销**可以近似为：

\[ T_{\text{overhead}} \approx T_{\text{chili}} - T_{\text{no}} \]

再除以 join 次数（= 节点数 \(2^L - 1\)），就得到「每次 join 摊销开销」——这正是 README 第二张表里 3.5ns 的来源。

还有一个隐藏设计：对照组本身也是**测量质量的校验**。如果 `no_overhead` 在 1023 节点下测出的每节点耗时（README 为 1.8ns）明显异常（比如几十 ns），说明测量环境有问题（机器降频、编译进了 debug 档），其他数据也就不可信了。

#### 4.2.2 核心流程

三个基准共享同一个骨架，只在「fork 点」处不同：

```text
no_overhead:   sum ──> join_no_overhead(a, b) ──> (a(scope), b(scope))     # 顺序直调
chili_overhead: sum ──> scope.join(a, b)        ──> join_seq / join_heartbeat / 跨线程分享
rayon_overhead: sum ──> rayon::join(a, b)       ──> rayon 自己的调度器

每个基准：
  for layers in LAYERS:            # 10 层(1023 节点)、24 层(1677 万节点)
      建树（setup，不计入）
      bench_local: 反复执行 sum(tree)，断言结果 == 节点数
```

#### 4.2.3 源码精读

**对照组：`join_no_overhead`。** [benches/overhead.rs:25-46](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/benches/overhead.rs#L25-L46) 定义了理论上限。注意 `join_no_overhead` 的签名**刻意模仿** `Scope::join`（连 `where` 约束都一样：两个 `FnOnce(&mut Scope) -> R + Send`），函数体却只是顺序调用：

```rust
#[divan::bench(args = nodes())]
fn no_overhead(bencher: Bencher, nodes: (usize, usize)) {
    fn join_no_overhead<A, B, RA, RB>(scope: &mut Scope<'_>, a: A, b: B) -> (RA, RB)
    where
        A: FnOnce(&mut Scope<'_>) -> RA + Send,
        B: FnOnce(&mut Scope<'_>) -> RB + Send,
        RA: Send,
        RB: Send,
    {
        (a(scope), b(scope))       // 没有任何调度：直接调用
    }
    // ...
}
```

为什么连 `Send` 约束和 `&mut Scope` 参数都要保留？为了让**递归 `sum` 的代码形状与另外两个基准逐字对齐**——闭包依然接收 `&mut Scope`（[benches/overhead.rs:37-46](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/benches/overhead.rs#L37-L46)），区别只在 fork 点换成 `join_no_overhead`。变量控制到这个程度，三者的耗时差才能干净地归因于调度。

**实验组：`chili_overhead`。** [benches/overhead.rs:58-65](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/benches/overhead.rs#L58-L65) 就是 README 首页示例的原型：fork 点换成 `scope.join`。这次调用会走 u2-l1 拆解过的完整路径——`join_count` 自增加模、多数时候落进 `join_seq`、命中心跳条件时入队并可能把任务送上货架：

```rust
fn sum(node: &Node, scope: &mut Scope<'_>) -> u64 {
    let (left, right) = scope.join(
        |s| node.left.as_deref().map(|n| sum(n, s)).unwrap_or_default(),
        |s| node.right.as_deref().map(|n| sum(n, s)).unwrap_or_default(),
    );
    node.val + left + right
}
```

注意基准使用 [src/lib.rs:249-252](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L249-L252) 的 `Scope::global()`，即默认配置的全局线程池：worker 数由 [src/lib.rs:513-518](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L513-L518) 决定（`thread_count` 为 `None` 时取 `available_parallelism() - 1`），心跳间隔取 [src/lib.rs:469-476](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L469-L476) 的默认值 100µs。也就是说：**这台机器有几个核，基准就默认用几线程跑**——对比 README 数据时要知道这一点。

**生态对照组：`rayon_overhead`。** [benches/overhead.rs:75-91](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/benches/overhead.rs#L75-L91) 把 fork 点换成 `rayon::join`。闭包不再需要 `&mut Scope`（rayon 的 join 是自由函数），但递归结构保持一致：

```rust
fn sum(node: &Node) -> u64 {
    let (left, right) = rayon::join(
        || node.left.as_deref().map(sum).unwrap_or_default(),
        || node.right.as_deref().map(sum).unwrap_or_default(),
    );
    node.val + left + right
}
```

三个基准的 setup 与断言完全同构（[benches/overhead.rs:48-53](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/benches/overhead.rs#L48-L53)、[67-72](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/benches/overhead.rs#L67-L72)、[86-90](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/benches/overhead.rs#L86-L90)）：建树、（取 Scope）、`bench_local` 里断言总和。

#### 4.2.4 代码实践

1. **实践目标**：用对照法亲手估出「一次 `scope.join` 的摊销开销」，验证 README 的 3.5ns 量级。
2. **操作步骤**：
   - 运行 `cargo bench --bench overhead`，从输出中抄下 1023 节点参数组下 `no_overhead` 与 `chili_overhead` 的耗时，记为 \(T_{\text{no}}\) 与 \(T_{\text{chili}}\)。
   - 计算：\[ \text{单次 join 开销} \approx \frac{T_{\text{chili}} - T_{\text{no}}}{1023} \]
   - 例如 README 的 AMD 数据：\((3.4\,\mu s - 1.8\,\mu s) / 1023 \approx 1.6\,ns\)；而第二张表给出的 chili 每节点总耗时是 3.5ns（包含基线的 1.8ns 工作量 + 约 1.7ns 调度开销），两个口径要对分清楚。
3. **需要观察的现象**：差值除以 1023 后应落在个位纳秒；若得到几十纳秒，先怀疑是 debug 编译或机器抖动（确认没有 `--release` 缺失，`cargo bench` 默认就是 release+bench 档，一般无需担心）。
4. **预期结果**：单次 join 摊销开销为个位 ns，与 README 的 3.5ns/节点（含工作量）同量级。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：对照组 `join_no_overhead` 的闭包也写成了 `|s| node.left.as_deref().map(|n| sum(n, s))...`，为什么要保留 `&mut Scope` 参数而不是干脆写成不接收参数的闭包？

**答案**：为了让三个基准的递归代码形状逐字对齐，消除「闭包捕获与调用方式不同」这个混杂变量。若对照组闭包不接收 `s`，编译产物（闭包大小、内联方式）可能与实验组不同，耗时差就不再能单纯归因于调度路径。

**练习 2**：rayon 的 `sum` 闭包是 `|| ...`（无参数），chili 的是 `|s| ...`。这个差异会不会让对比不公平？

**答案**：会有极小差异（chili 的闭包多捕获/传递一个 `&mut Scope`），但 `Scope` 只是一个指针加计数器，且该差异正是「使用 chili API 的真实成本」的一部分——用户写 chili 代码就要传 `s`。把它算进 chili 的开销反而更诚实。

### 4.3 结果解读

#### 4.3.1 概念说明

有了机制知识和测量方法，现在解读 README 的三张表。核心是回答两个问题：**大树上为什么接近 x8？小树上为什么不到 x1？**

**大树的加速比。** AMD 表（[README.md:45-49](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/README.md#L45-L49)）：

| Number of nodes | Baseline | Rayon | chili | Baseline / chili |
|---:|---:|---:|---:|:---:|
| 1023 | 1.8 µs | 51.1 µs | 3.4 µs | **x0.53** |
| 16777215 | 94.4 ms | 58.1 ms | 13.6 ms | **x6.94** |
| 134217727 | 797.5 ms | 497.2 ms | 101.8 ms | **x7.83** |

- 1677 万节点：\(94.4 / 13.6 \approx 6.94\)；1.34 亿节点：\(797.5 / 101.8 \approx 7.83\)——8 核机器上已非常接近理论上限 x8。
- 但作者在 [README.md:40-43](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/README.md#L40-L43) 特意提醒：134M 情形 chili 的每节点实际耗时是 \(101.8\,ms / 134217727 \approx 0.8\,ns\)，而「理想线性缩放」应为 \(1.8\,ns / 8 = 0.2\,ns\)。也就是说 x7.83 离 x8 的差距虽小，绝对耗时仍是理想值的约 4 倍。README 没有给出原因；一个合理的工程解释是：树遍历是内存受限负载，8 个核同时涌向内存时带宽与缓存共享成为瓶颈，纯计算理想值不可能达到（此为解读，非 README 原文）。
- 对照 rayon 列：1K 时 rayon 高达 51.1µs（ chili 的 15 倍），说明 rayon 的 per-join 固定成本远高——这正是 chili 的存在理由：**大量小计算场景下的低开销**。

**小树的临界点：为什么 x0.53 < 1？** 这是本讲最重要的现象。1K 节点时并行版比串行基线**慢了近一倍**，机制原因可以用 u2-l1 的知识完整解释：

1. **队列长度条件**。10 层树递归深度只有 10，任一时刻 `job_queue` 的长度几乎总是小于 3。回顾 u2-l1：`job_queue.len() < 3` 时**无条件**走 `join_heartbeat` 路径——所以 1K 情形下**几乎每一次 join** 都要付出「a 打包入队 → 检查心跳标志 → 执行 b → `take_receiver` 判定 → 出队」的完整簿记（对比：大树上也一样，只是被更多节点摊薄后仍有并行收益来补偿）。
2. **心跳几乎不会触发**。整棵树 3.4µs 就跑完了，而默认心跳间隔是 100µs（[src/lib.rs:469-476](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L469-L476)）——很可能整个运行期间心跳标志一次都没被置位（旗的初始值为 true，u3-l1 讲过的「自举」，至多让第一次 join 尝试投递一次）。也就是说 1K 情形大概率**根本没有真正的跨线程并行**，3.4µs 里多出来的部分几乎全是单线程簿记，而非跨线程协调。
3. **收益上限本来就低**。就算完美并行，1023 个节点的最快耗时也只能从 1.8µs 降到约 0.2µs，省下的 1.6µs 与调度开销同量级——收益与成本打平甚至倒挂。

这三条合起来就是「临界点」：**当总工作量小到调度开销与并行收益同量级时，并行必亏**。chili 的价值不是消灭这个规律，而是把亏损的绝对值压到最小（3.4µs vs rayon 的 51.1µs）。

**开销与线程数无关。** [README.md:59-66](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/README.md#L59-L66) 的第二张表（把总耗时换算成每节点 ns）：

| Number of nodes | Baseline | 1 thread | 2 threads | 4 threads | 8 threads |
|---:|---:|---:|---:|---:|---:|
| 1023 | 1.8 ns | 3.5 ns | 3.5 ns | 3.5 ns | 3.5 ns |

1/2/4/8 线程下每节点耗时恒为 3.5ns（[README.md:60-62](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/README.md#L60-L62) 的注释点明「overhead 保持近似恒定」）。这与机制完全自洽：1K 情形的开销来自 `join_heartbeat` 的**本地**簿记（入队、出队、闭包打包），这些动作发生在发起线程上、不碰共享锁（u2-l3 讲过「热路径完全不碰锁」），自然与线程数无关。这是一次漂亮的「机制预测数据、数据印证机制」。

**跨机器对比。** Apple M1 表（[README.md:51-57](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/README.md#L51-L57)）同样是 8 核，16.7M 节点加速比却只有 x3.51（AMD 是 x6.94）。注意两台机器的 baseline：M1 串行只要 39.4ms，AMD 要 94.4ms——M1 单核更快。加速比的分母是串行基线：**单核性能越强，同样并行池带来的加速比越低**。这提醒我们：加速比衡量的是「并行化的相对收益」，不是库的绝对开销；跨机器比加速比时要先看 baseline。

#### 4.3.2 核心流程

读表的固定动作：

```text
1. 定位行：Number of nodes ↔ LAYERS 里的层数（1023 ↔ 10 层，16777215 ↔ 24 层）
2. 算加速比：Baseline / chili（表末列已给出）
3. 算每节点耗时：chili 总耗时 / 节点数，与 baseline 每节点耗时相减 ≈ 调度开销
4. 对照理论上限：核数 N → 最大加速比 xN；理想每节点 = baseline 每节点 / N
5. 机制归因：开销恒定 → 本地簿记；加速比不足 → 内存/单核强度/心跳节奏
```

#### 4.3.3 源码精读

**AMD 主表与作者的解读注释。** [README.md:38-49](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/README.md#L38-L49)：表格上方一段话专门解释 134M 情形「接近理论上限但每节点绝对耗时仍是理想值 4 倍」——读基准报告时要区分**相对指标（加速比）**与**绝对指标（每节点耗时）**，两者可能给出不同的乐观程度。

**M1 表。** [README.md:51-57](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/README.md#L51-L57)：同 8 核、同 1K 亏损现象（x0.46），大树加速比 x3.51/x3.53 明显低于 AMD——与 baseline 更快（单核更强）相一致。

**开销恒定表。** [README.md:59-66](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/README.md#L59-L66)：1K 节点、1–8 线程，每节点恒 3.5ns。机制对应物在 [src/lib.rs:409-417](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L409-L417) 与 [src/lib.rs:438-450](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L438-L450)：`join` 固定以 `TIMES = 64` 调用 `join_with_heartbeat_every`，入口先做 `join_count` 自增加模——

```rust
self.join_count = self.join_count.wrapping_add(1) % TIMES;
```

绝大多数调用被降频挡在 `join_seq`（零簿记），只有周期性命中才走 `join_heartbeat`。这套纯本地的计数逻辑没有共享内存争用，是「开销与线程数无关」的直接原因。

#### 4.3.4 代码实践（本讲主实践）

1. **实践目标**：复现 README 的测量流程，并通过修改 `LAYERS` 观察规模增长时加速比的变化趋势。
2. **操作步骤**：
   - 运行 `cargo bench --bench overhead`，把本机结果（1023 与 16777215 两档、三个基准）整理成与 README 相同格式的表格，补算 `Baseline / chili` 列。
   - 把 [benches/overhead.rs:20](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/benches/overhead.rs#L20) 的常量改为 `const LAYERS: &[usize] = &[10, 16, 24];`（这是本实践对仓库副本的临时改动，观察完请还原），重新运行基准。16 层对应 65535 节点，正好填补 1K 与 16.7M 之间的空白。
   - 对三档规模分别计算加速比，观察从 10 层到 24 层加速比如何从 <1 爬升到接近核数。
3. **需要观察的现象**：
   - 10 层：加速比 < 1（并行亏损）；
   - 16 层（65535 节点）：加速比开始大于 1，处于过渡区；
   - 24 层：加速比接近机器核数（或受内存带宽限制略低）。
4. **预期结果**：加速比随规模单调上升，存在一个「盈亏平衡点」（在 1K 到 65535 节点之间，具体位置依机器而定）；chili 在三档上都应不慢于 rayon。若 24 层加速比明显低于预期，检查机器是否在省电模式或被其他负载占用。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：README 说 1K 节点时 chili 每节点 3.5ns 且与线程数无关。如果这 3.5ns 主要来自跨线程锁争用，表格会呈现出什么不同的样子？

**答案**：锁争用开销随线程数增加而放大（竞争者越多，获取同一把 `Context.lock` 的等待越长），表格会显示出 1 → 2 → 4 → 8 线程时每节点耗时递增（如 3.5 → 4 → 5 → 7ns）。恒定的 3.5ns 说明开销是发起线程的本地簿记（计数、入队、出队），与 u2-l3「热路径不碰锁」的设计一致。

**练习 2**：同样 8 核，为什么 AMD 上 16.7M 节点加速比是 x6.94，M1 上只有 x3.51？请用两台机器的 baseline 数据给出一个不涉及「chili 在 M1 上更差」的解释。

**答案**：M1 的 baseline 是 39.4ms，AMD 是 94.4ms——M1 单核性能更强。加速比 = baseline / chili，分母（串行基线）越小，同样的并行绝对耗时换算出的加速比越低。M1 的 chili 绝对耗时 11.2ms 甚至优于 AMD 的 13.6ms，说明 chili 本身不差，只是「相对收益」被更快的单核稀释了。

**练习 3**：基准闭包里的 `assert_eq!` 去掉会发生什么？为什么它不能换成 `black_box`（对 `sum` 的返回值而言二者都能防优化，但断言多做了什么）？

**答案**：去掉后若无任何结果消费，编译器可能判定整棵递归无副作用而将其删除或弱化，测到的时间不可信。`assert_eq!` 相比 `black_box` 多了一层**正确性校验**：它不只是「使用」返回值，还验证并行求和等于节点数——基准在测性能的同时顺带守住了「并行不改结果」的语义（may 并行语义，u1-l1）。

## 5. 综合实践

**任务：为本机产出一份完整的「chili 开销报告」，并找到你自己机器上的并行盈亏平衡点。**

1. 在仓库副本上完成 4.3.4 的测量：`LAYERS = &[10, 16, 24]`，运行 `cargo bench --bench overhead`，抄录全部数据后**还原 `LAYERS`**。
2. 用 README 的表格格式产出三份对照（1023 / 65535 / 16777215 节点 × baseline / rayon / chili），补算加速比列与每节点耗时列。
3. 回答三个分析题（各写 3–5 句）：
   - 你机器上的盈亏平衡点大概在哪个规模？为什么？（用「队列长度条件 + 心跳间隔 + 收益上限」三因素解释。）
   - 你的 chili 每节点开销是否也像 README 一样与线程数无关？如果想真正验证这一点，需要怎么改基准？（提示：`Scope::global()` 用默认线程池；要扫描线程数需参照 [src/lib.rs:513-518](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L513-L518) 的 `with_config` + `set_global` 自建全局池，u2-l2 有完整用法。）
   - 规模从 16 层翻到 24 层（节点数 ×256），加速比变化了多少？离你机器核数的理论上限还差多少？差距可能来自哪里？
4. 把报告存为个人笔记。至此你完成了「机制（u2/u3）→ 安全（u4-l1/u4-l2）→ 度量（本讲）」的闭环，下一讲将带着这些工具做真正的二次开发。

## 6. 本讲小结

- chili 的基准是自带 `main`（`harness = false`）的 divan 程序：`#[divan::bench(args = nodes())]` 按层数参数化，`bench_local` 保证迭代闭包固定在发起线程执行——与 `Scope` 的线程绑定语义严格匹配。
- 三个基准是一组控制变量的对照实验：`no_overhead` 用签名相同但直接顺序调用的 `join_no_overhead` 提供理论上限，`chili_overhead` 是实验组，`rayon_overhead` 是生态对照；chili 的净开销 ≈ \(T_{\text{chili}} - T_{\text{no}}\)。
- 大树上加速比接近核数上限（x7.83 / 8 核），但每节点绝对耗时（0.8ns）仍是理想值（0.2ns）的约 4 倍——相对指标与绝对指标要分开读。
- 1K 节点 x0.53 的临界点现象由三个机制因素叠加：队列长度 < 3 使几乎每次 join 都走 `join_heartbeat` 簿记路径、3µs 的总耗时远短于 100µs 心跳间隔导致几乎没有真并行、并行收益上限与调度开销同量级。
- 每节点 3.5ns 恒定不随线程数增长，印证开销来自发起线程的本地簿记（`join_count % 64` 降频 + 入队出队），而非共享锁争用——机制与数据互证。
- 当前 `LAYERS = &[10, 24]` 只能复现 README 表格的前两档；26/27 层的数据需要自行扩展常量复测。

## 7. 下一步学习建议

下一讲（u4-l4）是毕业综合实践：用 chili 独立实现并行归并排序，扫描线程数与 `TIMES` 心跳参数，做出你自己的性能结论。本讲学会的「对照—测量—归因」方法将直接复用。在此之前，建议再动手做两件小事：一是给 4.3.4 的测量补一个 `Config::with_config` 自建线程池的版本，亲眼验证「开销与线程数无关」；二是回看 [benches/overhead.rs:27-35](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/benches/overhead.rs#L27-L35) 的 `join_no_overhead` 签名，思考如果让你为自己的库写对照组，哪些 API 细节必须对齐——这是设计基准实验的通用功力。
