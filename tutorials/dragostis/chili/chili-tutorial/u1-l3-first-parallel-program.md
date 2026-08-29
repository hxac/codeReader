# 写下第一个并行程序：Scope 与 join

## 1. 本讲目标

学完本讲，你应该能够：

- 不看模板，独立写出使用 `Scope::global()` 与 `scope.join(...)` 的并行程序；
- 解释 `join` 的两个闭包为什么各接收一个 `&mut Scope`，以及这如何支撑「在闭包里继续递归分叉」；
- 读懂 chili 官方文档中的二叉树并行求和示例，并能推算任意规模下的正确结果；
- 在自己的机器上新建一个二进制 crate、添加 chili 依赖、编译运行并验证结果正确性。

## 2. 前置知识

本讲承接前两讲：u1-l1 建立了 fork-join 模型与「may 并行」语义的直觉，u1-l2 建立了 `cargo check / test / clippy` 的验证环和「公共 API 全在 `src/lib.rs`」的代码地图。这里只做简要回顾，再补充几个 Rust 语言预备知识。

### 2.1 fork-join 与「may 并行」回顾

- **fork-join**：在计算的某个分叉点（fork）把工作分成两份，各自执行后再汇合（join）拿回两份结果。
- **may 并行**：README 明确说明 chili 在任何分叉点「*may* run the two passed closures in parallel」（[README.md:8-13](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/README.md#L8-L13)）。`join` 只承诺「两个闭包都执行完，结果按参数顺序还给你」；是否真的跨线程由运行时决定。**因此程序结果的正确性与是否并行无关，并行只影响耗时。**

### 2.2 Rust 语言预备

| 概念 | 一句话解释 | 在本讲哪里出现 |
|---|---|---|
| 闭包 `|s| expr` | 匿名函数，`s` 是参数名 | `join` 的两个参数 |
| `FnOnce` 约束 | 被调用一次、调用时被「拿走」的闭包 | `join` 的泛型约束 |
| `Send` | 类型值可以安全地跨线程移动 | 闭包与返回值的约束 |
| `Option<Box<Node>>` | 可空的孩子指针（`Box` 是堆分配） | `Node` 结构体 |
| `as_deref()` | `Option<Box<T>>` → `Option<&T>`，省一层解包 | `sum` 示例 |
| `bool.then(f)` | `true` 得 `Some(f())`，`false` 得 `None` | `Node::tree` 构造 |
| `unwrap_or_default()` | `None` 时返回类型默认值（`u64` 是 0） | `sum` 的叶子处理 |
| doctest | 写在 `///` 文档注释里、由 `cargo test --doc` 编译运行的示例 | 官方示例本身 |

关于 `Send` 多说两句：一个闭包如果要被送到另一个线程去执行，它捕获的所有数据和返回值都必须能安全地跨线程移动。chili 的 `join` 在类型层面按「最坏情况」要求——即使某次调用实际是顺序执行的，类型系统也无法预知，所以一律要求 `Send`（内部原因在 u2-l1 展开）。

### 2.3 Scope 在 API 版图中的位置

u1-l2 已确认：chili 对外只暴露 `Scope`、`Config`、`ThreadPool` 三个类型。本讲只用其中两个入口：

- `Scope::global()` —— 拿一个全局作用域；
- `scope.join(a, b)` —— 在上面发起一次分叉。

`ThreadPool` 与 `Config` 留到 u2-l2 再深入。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注的区块 |
|---|---|---|
| [src/lib.rs](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs) | 全部公共 API 与调度逻辑 | L5-L48 crate 级文档与官方示例；L233-L239 `Scope` 定义；L250-L252 `Scope::global`；L335-L346 `join_seq`；L409-L417 `pub fn join`；L584-L606 `ThreadPool::global`/`scope`；L656-L714 测试 `join_basic`/`join_long`/`join_very_long` |
| [README.md](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/README.md) | 项目门面 | L19-L28 同款求和示例；L30-L31「理想示例」说明 |
| [Cargo.toml](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/Cargo.toml#L1-L5) | 依赖信息 | 版本 `0.2.1`，实践时添加依赖用 |

## 4. 核心概念与源码讲解

### 4.1 Scope 与 join 基本用法

#### 4.1.1 概念说明

`Scope` 是「一个可以在上面运行 fork-join 工作负载的作用域对象」——这是 [src/lib.rs:217](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L217-L232) 文档注释的原话（*A `Scope`d object that you can run fork-join workloads on*）。你可以把它理解成一张「并行计算的工牌」：

- `Scope::global()` 免费办一张全局工牌；
- `scope.join(a, b)` 刷卡一次，发起一次分叉。

与 `rayon::join(f, g)` 相比，chili 的 `join` 闭包**多收一个参数 `&mut Scope`**。这是本讲最关键的设计：它把「继续分叉的能力」作为门票发进子任务——子任务拿到 `s` 后还能继续调用 `s.join(...)`，递归并行由此展开。这样做的调度层面的原因（子分支可能被别的线程接手，需要有自己的调度上下文）在 u2 讲清楚，本讲先记住「门票」这个比喻。

#### 4.1.2 核心流程

从调用到执行的主链路（伪代码）：

```text
Scope::global()
  └─ ThreadPool::global()                 // 全局线程池，OnceLock 惰性初始化
       └─ tp.scope()                      // 从线程池创建一个 Scope
scope.join(a, b)
  └─ join_with_heartbeat_every::<64>(a, b)
       ├─ 计数命中 或 本地队列很短 → join_heartbeat   // 有机会把任务分享给别的线程
       └─ 其余大多数情况        → join_seq          // 就在当前线程顺序执行
```

两个要点：

1. `join` 内部有两条路（顺序 / 尝试分享），**大多数调用走顺序路径**——这就是「may 并行」在实现层面的样子；
2. 无论走哪条路，对调用者呈现的行为完全一致：`a`、`b` 都被执行，结果按参数顺序返回。

#### 4.1.3 源码精读

**(1) `Scope` 结构体：一个不透明句柄**

[src/lib.rs:233-239](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L233-L239) 定义了 `Scope` 的四个字段——`context`（线程池共享上下文）、`job_queue`（本地任务队列）、`heartbeat`（心跳标记）、`join_count`（join 计数器）：

```rust
pub struct Scope<'s> {
    context: Arc<Context>,
    job_queue: ThreadJobQueue<'s>,
    heartbeat: Arc<AtomicBool>,
    join_count: u8,
}
```

这些字段全部私有——使用者只需把 `Scope` 当作不透明句柄传递，不需要（也不允许）碰它的内部状态。字段含义在本阶段不必深究，它们分别是 u2 各讲的主角。

**(2) `Scope::global()`：全局线程池的入口**

[src/lib.rs:250-252](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L250-L252) 只有一行实质代码：

```rust
pub fn global() -> Scope<'static> {
    ThreadPool::global().scope()
}
```

它委托给 [src/lib.rs:584-586](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L584-L586) 的 `ThreadPool::global()`，后者用 [src/lib.rs:478](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L478) 的 `static GLOBAL_THREAD_POOL: OnceLock<ThreadPool>` 做进程级单例——第一次调用时才创建线程池，之后每次都拿同一个。返回类型 `Scope<'static>` 表示它不借用任何局部数据，所以在任何函数里都能随手获取（本讲实践正是这么用的）。

**(3) `join` 的签名：逐行读懂约束**

[src/lib.rs:409-417](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L409-L417)：

```rust
pub fn join<A, B, RA, RB>(&mut self, a: A, b: B) -> (RA, RB)
where
    A: FnOnce(&mut Scope<'_>) -> RA + Send,
    B: FnOnce(&mut Scope<'_>) -> RB + Send,
    RA: Send,
    RB: Send,
{
    self.join_with_heartbeat_every::<64, _, _, _, _>(a, b)
}
```

读法：

- `&mut self`：`join` 会改动 `Scope` 的内部状态（比如 `join_count` 计数），所以你的 `scope` 变量、`sum` 函数的参数都得是 `&mut`；
- 闭包 `a`、`b` 各收一个 `&mut Scope<'_>`——递归的「门票」，子分支里可以继续 `s.join(...)`；
- 返回 `(RA, RB)`：元组顺序与参数顺序一致，与实际执行顺序无关；
- 四个 `Send` 约束：闭包和结果都可能跨线程移动；
- 最后一行把调用转发给 `join_with_heartbeat_every::<64, ...>`，常数 `64` 表示「每 64 次 join 才认真检查一次是否该分享工作」，其余时候顺序执行。

> 冷知识：`join` 的文档注释写的是「skips checking for a heartbeat every 16 calls」（[src/lib.rs:395-396](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L392-L417)），而代码实际转发到 `::<64>`——文档与实现有个小出入，以代码为准。

**(4) `join_seq`：顺序路径与「结果按参数顺序」的保证**

[src/lib.rs:335-346](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L335-L346)：

```rust
fn join_seq<A, B, RA, RB>(&mut self, a: A, b: B) -> (RA, RB) {
    ...
    let rb = b(self);
    let ra = a(self);

    (ra, rb)
}
```

注意一个细节：**先执行 `b` 再执行 `a`，但返回时仍是 `(ra, rb)`**。执行顺序是实现自由，结果顺序是 API 契约——这就是「结果收集方式」的全部要点：你永远按参数顺序解构元组，不用关心谁先跑。

**(5) 经典数据并行模式：写不相交切片**

`join` 自己的 doctest（[src/lib.rs:398-406](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L398-L406)）展示了另一个常用模式：

```rust
let mut vals = [0; 2];
let (left, right) = vals.split_at_mut(1);

Scope::global().join(|_| left[0] = 1, |_| right[0] = 1);

assert_eq!(vals, [1; 2]);
```

`split_at_mut` 把数组切成两段**互不重叠**的可变引用，两个闭包各写各的一半。即使两个闭包真的在不同线程同时执行，类型系统已经保证它们不可能踩到同一块内存——这是写 fork-join 代码时最重要的安全直觉。

#### 4.1.4 代码实践

**实践：让官方示例在你机器上跑一遍（doctest）**

1. **实践目标**：确认本讲的主角示例（crate 级文档里的二叉树求和）在你本地可通过编译与断言。
2. **操作步骤**：
   - 进入 chili 仓库根目录；
   - 运行 `cargo test --doc`；
   - 在输出的测试列表里找到 crate 级文档对应的那一条（名字形如 `src/lib.rs - lib (line …)`，是唯一一条名为 `lib` 的条目）。
3. **需要观察的现象**：共 10 条文档测试（u1-l2 已建立这一数字），其中 crate 级那条就是 4.2 节要精读的示例。
4. **预期结果**：全部 `ok`——CI 的 `cargo test --doc` 步骤持续验证着同一批示例，因此通过是高置信的；若失败，先检查工具链版本（`rustc --version`）。

#### 4.1.5 小练习与答案

**练习 1**：`join` 返回的元组顺序由什么决定——执行顺序还是参数顺序？

> **答**：参数顺序。`join_seq` 先执行 `b` 再执行 `a`，但返回 `(ra, rb)`（[src/lib.rs:342-345](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L335-L346)）。你可以永远按声明顺序解构结果。

**练习 2**：为什么闭包和返回值都要求 `Send`，即使很多时候任务是顺序执行的？

> **答**：`join` 的任务**可能**被分享到其他线程执行（4.1.2 的分支路），类型系统无法在编译期预知某次调用走哪条路，只能按最坏情况统一要求 `Send`。这是「may 并行」在类型签名上的体现。

**练习 3**：`Scope::global()` 返回的 `Scope<'static>` 里的 `'static` 意味着什么？

> **答**：它背后的全局线程池是进程生命周期的 `static` 单例（`OnceLock`，[src/lib.rs:478](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L478)），由此创建的 `Scope` 不借用任何局部变量，可以在任意函数里获取和使用。

### 4.2 二叉树并行求和示例

#### 4.2.1 概念说明

chili 的 crate 级文档（[src/lib.rs:5-48](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L5-L48)）用「二叉树并行求和」当门面示例，README 也原样收录并解释了原因（[README.md:30-31](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/README.md#L30-L31)）：

> This is the ideal example since per-node computation is very cheap and the nodes don't keep track of how many descendants are left.

翻译过来：每个节点的计算量极小（只是加法），且节点**不知道**自己子树还剩多少工作量——正是 u1-l1 总结的 chili 甜点区「大量小计算 + 难以估计分支剩余量」。对照组是那些可以廉价估计剩余工作量的场景，那里提前停止分叉的库（如 rayon 的 splitter 模式）更合适。

#### 4.2.2 核心流程

示例由三部分组成：数据结构 `Node`、构造器 `tree(layers)`、并行求和 `sum`。

- `tree(L)` 构造一棵 L 层满二叉树，每个节点 `val = 1`；
- 节点数为 \(N(L) = 2^L - 1\)，因此所有节点值之和也是 \(S(L) = 2^L - 1\)；
- `sum` 的执行流程：

```text
sum(node, scope):
    在 scope 上 join 两个闭包：
        左闭包(s): 若有左孩子 → sum(左孩子, s)；否则 → 0
        右闭包(s): 若有右孩子 → sum(右孩子, s)；否则 → 0
    汇合后返回 node.val + left + right
```

递归在叶子处触底（孩子为 `None`，返回 0），结果自底向上逐层相加，最终在根节点汇成总和。

#### 4.2.3 源码精读

**(1) 数据结构与构造器**（[src/lib.rs:20-34](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L20-L34)）：

```rust
struct Node {
    val: u64,
    left: Option<Box<Node>>,
    right: Option<Box<Node>>,
}

impl Node {
    pub fn tree(layers: usize) -> Self {
        Self {
            val: 1,
            left: (layers != 1).then(|| Box::new(Self::tree(layers - 1))),
            right: (layers != 1).then(|| Box::new(Self::tree(layers - 1))),
        }
    }
}
```

这段代码构造一个「每层节点数翻倍」的满二叉树：`(layers != 1).then(...)` 在最底层（`layers == 1`）返回 `None` 作为叶子标记，其余层递归建子树。

**(2) 并行求和函数**（[src/lib.rs:36-43](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L36-L43)）：

```rust
fn sum(node: &Node, scope: &mut Scope<'_>) -> u64 {
    let (left, right) = scope.join(
        |s| node.left.as_deref().map(|n| sum(n, s)).unwrap_or_default(),
        |s| node.right.as_deref().map(|n| sum(n, s)).unwrap_or_default(),
    );

    node.val + left + right
}
```

逐个拆解左闭包 `|s| node.left.as_deref().map(|n| sum(n, s)).unwrap_or_default()`：

| 步骤 | 表达式 | 结果 |
|---|---|---|
| 1 | `node.left` | `Option<Box<Node>>` |
| 2 | `.as_deref()` | `Option<&Node>`（把堆盒子解成引用） |
| 3 | `.map(\|n\| sum(n, s))` | 有孩子则递归求和（把门票 `s` 传下去）；`Option<u64>` |
| 4 | `.unwrap_or_default()` | 叶子（`None`）得 `u64` 默认值 0 |

两个细节值得注意：

- 闭包以只读方式捕获 `node: &Node`，两个闭包分别只**读**左右子树，天然无数据竞争；
- 两个闭包都带走了门票 `s`，所以子树内部还能继续 `s.join(...)`——递归并行的入口就在这里。

**(3) 验证断言**（[src/lib.rs:45-47](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L45-L47)）：

```rust
let tree = Node::tree(10);

assert_eq!(sum(&tree, &mut Scope::global()), 1023);
```

`1023 = 2^10 - 1`，正是 10 层满二叉树的节点总数。这行断言是 doctest 的一部分，`cargo test --doc`（CI 同样执行）持续验证着它。README 的 [README.md:19-28](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/README.md#L19-L28) 收录了同一个 `sum` 函数。

#### 4.2.4 代码实践（纸面推演）

1. **实践目标**：不运行代码，靠递归结构手算结果，建立「任务树」的肌肉记忆。
2. **操作步骤**：
   - 在纸上画出 `Node::tree(3)` 的 7 个节点；
   - 模拟 `sum` 的执行：每个节点发起一次 `join`，叶子闭包返回 0，自底向上标注每个节点的返回值；
   - 再填一张表：\(L = 1, 2, \ldots, 10\) 对应的总和 \(2^L - 1\)。
3. **需要观察的现象**：递归如何自底向上汇总；根节点返回值 = 节点数。
4. **预期结果**：`tree(3)` 根节点返回 7；表格为 1, 3, 7, 15, 31, 63, 127, 255, 511, 1023——最后一格正是源码断言的 1023（纯数学推导，无需本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：`Node::tree(20)` 的并行求和结果是多少？

> **答**：\(2^{20} - 1 = 1\,048\,575\)。Rust 里写成 `1_048_575`（综合实践会用到）。

**练习 2**：把每个节点的 `val` 从 1 改成 2，`tree(10)` 的结果是多少？

> **答**：\(2 \times 1023 = 2046\)。总和与节点数成正比。

**练习 3**：`unwrap_or_default()` 在这里为什么恰好合适？

> **答**：叶子节点没有孩子，对「求和」而言贡献应为 0，而 `u64::default()` 正是 0（加法单位元）。这是「默认值恰好是业务单位元」的巧合用法；换成别的聚合操作（比如求最大值）就得显式写 `unwrap_or(...)`，否则语义会错。

### 4.3 递归 fork-join 模型

#### 4.3.1 概念说明

把 4.2 的 `sum` 放大看，你会发现它生成的**执行结构与数据结构同构**：每个节点发起一次 `join`，`join` 的两个闭包又各自对子树发起 `join`……最终形成一棵「任务树」。这就是递归 fork-join 模型：

- **分叉（fork）**：`scope.join(a, b)` 把当前工作劈成两半；
- **汇合（join）**：拿回 `(left, right)`，合并成当前节点的结果；
- **门票传递**：闭包参数 `s` 让分叉可以无限嵌套。

写法上你会在源码里看到两种闭包：

- `|_| ...`——丢弃门票，该分支是叶子，不再继续分叉；
- `|s| sum(n, s)`——带走门票，该分支内部继续分叉。

对一棵 L 层满二叉树，`sum` 会在每个节点各执行一次 `join`，所以 **join 调用次数 = 节点数 = \(2^L - 1\)**（叶子上那次 `join` 是平凡的：两个闭包都直接返回 0）。`tree(10)` 意味着 1023 次 `join` 调用——其中绝大多数走 4.1.2 所说的顺序路径。

#### 4.3.2 核心流程

递归的**形状**不是唯一的。chili 的测试里有两个现成对照（都对切片元素 `+1`）：

- **链式剥离**（`join_long`）：每次 `join` 剥出 1 个元素作为左分支，剩余部分作为右分支递归——任务树退化成一条链，深度 \(O(n)\)；
- **对半切分**（`join_very_long`）：每次 `join` 把切片对半分成两个递归分支——任务树宽而浅，深度 \(O(\log n)\)。

有趣的是，对 \(n\) 个元素，两种形状的 `join` 次数都是 \(n - 1\)（链式：每层剥 1 个；对半：内部节点数 \(n-1\)）。差别在**深度**：对半切分能让工作量以对数速度铺满所有线程，链式剥离则要一层一层慢慢漏下去。这是「同样的 join 次数、不同的并行收益」的第一课，定量分析留到 u4-l3 的基准讲。

#### 4.3.3 源码精读

**(1) `join_long`：两种闭包写法并存**（[src/lib.rs:669-690](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L669-L690)）：

```rust
fn increment(s: &mut Scope, slice: &mut [u32]) {
    match slice.len() {
        0 => (),
        1 => slice[0] += 1,
        _ => {
            let (head, tail) = slice.split_at_mut(1);

            s.join(|_| head[0] += 1, |s| increment(s, tail));
        }
    }
}
```

同一个 `join` 里，左闭包 `|_| head[0] += 1` 丢弃门票只干一个小活（处理剥出来的单元素），右闭包 `|s| increment(s, tail)` 带着门票继续处理剩余切片。注意它复用了 4.1.3 (5) 的 `split_at_mut` 模式：左右两半互不相交，闭包各改各的。这个测试对 1024 个元素断言全为 1。

**(2) `join_very_long`：双分支都递归**（[src/lib.rs:692-714](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L692-L714)）：

```rust
_ => {
    let mid = slice.len() / 2;
    let (left, right) = slice.split_at_mut(mid);

    s.join(|s| increment(s, left), |s| increment(s, right));
}
```

两个闭包都拿门票、都递归——这就是 4.2 二叉树求和的「数组版」。它对 \(2^{20}\) 个元素（1 Mi）断言全为 1。

**(3) 分叉点背后的开关**（[src/lib.rs:449-455](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L438-L456)）：

```rust
self.join_count = self.join_count.wrapping_add(1) % TIMES;

if self.join_count == 0 || self.job_queue.len() < 3 {
    self.join_heartbeat(a, b)
} else {
    self.join_seq(a, b)
}
```

这是 `join` 内部决定「顺序执行还是尝试分享」的开关：每 `TIMES` 次命中一次，或本地队列很短时总是检查。本讲只需要知道这个开关存在；它的完整行为（为什么这样设计能降低开销）是 u2-l1 的主题。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：体会「同样的 join 次数、不同的递归形状」。
2. **操作步骤**：
   - 精读 `join_long`（[src/lib.rs:669-690](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L669-L690)）与 `join_very_long`（[src/lib.rs:692-714](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L692-L714)）；
   - 分别画出处理 8 个元素时的前三层展开图（一个像链，一个像满树）；
   - 回答：1024 个元素时 `join_long` 发起多少次 `join`？\(2^{20}\) 个元素时 `join_very_long` 呢？
3. **需要观察的现象**：两幅图的宽度增长速度——链式每层只多出 1 个可并行分支，对半每层翻倍。
4. **预期结果**：`join_long` 为 \(1024 - 1 = 1023\) 次；`join_very_long` 为 \(2^{20} - 1 = 1\,048\,575\) 次（由源码结构推导）。两者在实际耗时上的差距**待本地验证**，u4-l3 会用 `benches/overhead.rs` 的基准方法量化。

#### 4.3.5 小练习与答案

**练习 1**：把 `sum` 改写成完全不用 `join` 的顺序版本 `sum_seq`。

> **答**（示例代码）：
> ```rust
> fn sum_seq(node: &Node) -> u64 {
>     node.val
>         + node.left.as_ref().map(|n| sum_seq(n)).unwrap_or_default()
>         + node.right.as_ref().map(|n| sum_seq(n)).unwrap_or_default()
> }
> ```
> 注意顺序版不需要 `scope` 参数。综合实践里我们会用它做结果与耗时的对照。

**练习 2**：写成 `|_|` 的闭包分支里还能再调用 `join` 吗？

> **答**：不能——它没有拿到 `Scope` 这个值，编译器层面就没有可调用的对象。`|_|` 与 `|s|` 的选择实际上向编译器声明了「这个分支是否还要继续分叉」。

**练习 3**：`Node::tree` 的递归为什么不会无限进行？

> **答**：每层递归 `layers - 1`，当 `layers == 1` 时 `(layers != 1).then(...)` 返回 `None`，`sum` 在叶子处返回 0，递归触底。

## 5. 综合实践

**任务：从零搭建你的第一个 chili 程序 `chili-sum`，并验证到 `tree(20)` 规模。**

### 5.1 创建工程与添加依赖

```bash
cargo new chili-sum
cd chili-sum
```

添加依赖有两种方式，任选其一：

- **方式 A（crates.io）**：`cargo add chili`——拉取发布版（当前 [Cargo.toml](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/Cargo.toml#L1-L5) 中版本为 `0.2.1`）；
- **方式 B（本地源码）**：在 `chili-sum/Cargo.toml` 里写 `chili = { path = "../dragostis-chili" }`（路径指向本仓库），保证与讲义引用的 HEAD 完全一致。

### 5.2 编写 main.rs

以下为**示例代码**（改写自 [src/lib.rs:18-48](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L18-L48) 的官方文档示例）：

```rust
use chili::Scope;

struct Node {
    val: u64,
    left: Option<Box<Node>>,
    right: Option<Box<Node>>,
}

impl Node {
    pub fn tree(layers: usize) -> Self {
        Self {
            val: 1,
            left: (layers != 1).then(|| Box::new(Self::tree(layers - 1))),
            right: (layers != 1).then(|| Box::new(Self::tree(layers - 1))),
        }
    }
}

fn sum(node: &Node, scope: &mut Scope<'_>) -> u64 {
    let (left, right) = scope.join(
        |s| node.left.as_deref().map(|n| sum(n, s)).unwrap_or_default(),
        |s| node.right.as_deref().map(|n| sum(n, s)).unwrap_or_default(),
    );

    node.val + left + right
}

fn main() {
    let tree = Node::tree(10);
    let total = sum(&tree, &mut Scope::global());

    assert_eq!(total, 1023);
    println!("tree(10) sum = {total}");
}
```

运行 `cargo run`。

### 5.3 升级到 tree(20)

把 `main` 中两处改掉：

```rust
let tree = Node::tree(20);
// ...
assert_eq!(total, 1_048_575);
```

用 `cargo run --release` 运行（release 模式才能反映真实性能；debug 下并行框架的开销会被放大）。

### 5.4 可选扩展：与顺序版对照计时

在文件里追加（示例代码）：

```rust
use std::time::Instant;

fn sum_seq(node: &Node) -> u64 {
    node.val
        + node.left.as_ref().map(|n| sum_seq(n)).unwrap_or_default()
        + node.right.as_ref().map(|n| sum_seq(n)).unwrap_or_default()
}
```

并在 `main` 里：

```rust
let t0 = Instant::now();
let p = sum(&tree, &mut Scope::global());
let t_parallel = t0.elapsed();

let t1 = Instant::now();
let s = sum_seq(&tree);
let t_seq = t1.elapsed();

assert_eq!(p, s);
println!("parallel: {t_parallel:?}, sequential: {t_seq:?}");
```

### 5.5 观察与预期结果

| 观察项 | 预期 |
|---|---|
| `tree(10)` 断言 | 通过，输出 `tree(10) sum = 1023`（同款代码是 CI 持续验证的 doctest，高置信） |
| `tree(20)` 断言 | 通过，总和 `1_048_575`（数学推导：\(2^{20}-1\)） |
| 并行 vs 顺序耗时 | **待本地验证**。参考 README 数据：1023 节点时并行反而更慢（x0.53），1670 万节点时接近 8 核上限（x6.94）；`tree(20)` 约 100 万节点，处于两者之间的过渡区，加速比取决于你的核数 |
| 多次运行并行耗时 | 有波动——分叉是否真的跨线程是机会主义的（4.1.2 的两条路径） |

**内存提醒**：每个 `Node` 约 24 字节（64 位平台上 `u64` 加两个 `Option<Box>`，不含分配器开销），`tree(L)` 共 \(2^L - 1\) 个节点——`tree(25)` 约 800 MB，`tree(30)` 约 25 GB。请把层数控制在 24 以内。

## 6. 本讲小结

- `Scope::global()` 从进程级单例线程池（`OnceLock`）惰性获取作用域，`Scope<'static>` 在任何函数里都能用；
- `scope.join(a, b)` 返回 `(RA, RB)`，**元组顺序 = 参数顺序**，与实际执行顺序无关（`join_seq` 先跑 `b` 再跑 `a` 也返回 `(ra, rb)`）；
- 闭包参数 `&mut Scope` 是「继续分叉的门票」：`|s| sum(n, s)` 递归传递，`|_|` 表示叶子分支；
- 二叉树求和示例中，L 层满二叉树节点数与总和都是 \(2^L - 1\)，`join` 调用次数等于节点数；
- `join` 内部有「顺序 / 尝试分享」两条路径，大多数调用顺序执行——这就是「may 并行」：结果永远正确，并行只影响耗时；
- `join_long`（链式剥离）与 `join_very_long`（对半切分）展示了 join 次数相同、任务树形状不同的两种递归设计。

## 7. 下一步学习建议

本讲你把 `join` 当黑盒用对了；下一讲 **u2-l1「join 的三条执行路径」** 打开这个黑盒：精读 `join_with_heartbeat_every`、`join_heartbeat` 与 `join_seq`，理解 `join_count % TIMES` 降频检查和 `job_queue.len() < 3` 这两个条件为什么能把开销压到每节点约 3.5 ns。

在那之前，建议做两件热身事：

1. 通读 [src/lib.rs:635-833](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L635-L833) 的 `tests` 模块，把 `join_basic`、`join_wait` 也过一遍——它们是 u2 各讲的现成实验素材；
2. 想控制线程数重跑本讲的求和实验的话，先预习 `ThreadPool::with_config` 与 `Config`（[src/lib.rs:501-545](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L501-L545)），这是 **u2-l2** 的主题。
