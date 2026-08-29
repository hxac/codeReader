# chili 是什么：低开销并行化库的全景

## 1. 本讲目标

本讲是整本学习手册的第一讲，不要求你写过任何并行代码。学完后你应该能够：

1. 用一句话说清 chili 是什么、它解决什么问题、适合什么场景。
2. 理解 fork-join 计算模型，以及 README 中 "it *may* run the two passed closures in parallel" 里这个 "may"（可能）的含义。
3. 读懂 README 中两台机器（AMD Ryzen 7 4800HS / Apple M1）上的三方对比基准表，并能自己复算加速比、每节点摊销耗时等派生指标。
4. 了解 chili 与原版 Spice 库的渊源，以及它在 crates.io / docs.rs 上的发布形态（版本、许可证、零运行时依赖）。

本讲只读两个文件：`README.md` 和 `Cargo.toml`。真正的源码精读从下一讲开始。

## 2. 前置知识

本讲需要的背景知识很少，以下概念用通俗语言解释一遍即可。

### 2.1 什么是并行（parallelism）

现代 CPU 通常是多核的：一台 8 核机器可以真正**同时**执行 8 条指令流。如果一段计算可以被拆成互不依赖的部分，理论上把它分给多个核同时算，总时间就能缩短。并行关注"同时算得更快"，这和并发（concurrency，关注"如何组织多个任务的交错执行"）不完全是一回事。

### 2.2 什么是 fork-join 模型

fork-join 是最古老的并行编程模型之一，直觉来自它的名字：

- **fork（分叉）**：在计算中的某个点，把工作分成两份，两份*可能*被不同线程同时执行；
- **join（汇合）**：两份都做完后，把它们的结果合起来，程序继续往下走。

伪代码：

```
join(f, g):
    fork: 把 f 和 g 两个闭包交给（一个或两个）线程
    本线程也可能亲自执行其中一个
    等待两者都完成
    return (f 的结果, g 的结果)
```

它天然适合**分治（divide-and-conquer）**算法：树的左右子树、数组的两半，都可以递归地 fork，最后逐层 join 汇总。

### 2.3 什么是"开销"（overhead）

把任务交给另一个线程不是免费的：要打包任务、加锁、通知对方线程、等结果传回来。这些额外消耗就是并行化的**开销**。

如果一个任务本身只要 10 纳秒，而转交出去要花 100 纳秒，那并行反而更慢。所以任何并行库的核心竞争力之一，就是把单次转交的开销压到极低——这正是 chili 名字里 "low-overhead"（低开销）的含义，也是它和 rayon 这类通用数据并行库拉开差距的地方。

### 2.4 你需要会的基本操作

- 能看懂 Rust 的闭包（`|s| ...` 这种写法）和 `Option<Box<Node>>` 这样的类型。
- 知道 `cargo` 是 Rust 的构建工具。本讲的实践里会用到 `cargo tree` 和浏览器，不需要更深的工具链知识。

## 3. 本讲源码地图

chili 是一个极小的库，全部核心代码只有两个源文件。本讲涉及的关键文件如下：

| 文件 | 作用 | 本讲用法 |
| --- | --- | --- |
| `README.md` | 项目门面：定位说明、求和示例、两台机器的基准数据 | 本讲的主精读对象 |
| `Cargo.toml` | 包元信息与依赖声明 | 精读：看清"零运行时依赖"这一事实 |

下面三个文件本讲只在"源码地图"层面认个脸熟，后续单元会逐个深入：

| 文件 | 作用 | 深入的讲义 |
| --- | --- | --- |
| `src/lib.rs` | 公共 API（`Scope`、`join`、`ThreadPool`）与调度、心跳、worker 线程逻辑 | u1-l2 起 |
| `src/job.rs` | 任务对象与一个手写的单值通道（`Channel`/`Sender`/`Receiver`） | u3-l2、u3-l3 |
| `benches/overhead.rs` | 用 divan 框架写的基准，README 数据的来源 | u4-l3 |

一句话记住项目结构：**README 讲"为什么"，`src/lib.rs` 讲"怎么调度"，`src/job.rs` 讲"任务怎么传递"，`benches/overhead.rs` 讲"到底有多快"。**

## 4. 核心概念与源码讲解

### 4.1 模块一：项目定位与适用场景

#### 4.1.1 概念说明

chili 的自我定位写在 README 的副标题里：它是 [Spice] 的 Rust 移植版，一个**开销极低的并行化原语（primitive）**，行为与 [`rayon::join`] 几乎一致——在计算中的任意 fork 点，它*可能*并行地执行传入的两个闭包。

三个关键词值得逐个展开：

1. **原语（primitive）**：chili 不是一个大框架，而是一块可以直接拿来自由组合的积木。它提供的核心就是一个 `join` 操作（外加管理线程池的少量配套 API）。你不会在 chili 里找到并行迭代器、作用域任务树这些高层设施。

2. **"可能"（may）并行**：这是 fork-join 语义的关键。`scope.join(f, g)` **保证**返回 `(f 的结果, g 的结果)`，但**不保证** f 和 g 一定跑在不同线程上。当前线程够闲，它就顺序执行两个闭包；线程池里有别的线程闲着且时机合适（后面会学到：心跳触发时），它才把其中一个分享出去。这个"可能"不是含糊其辞，而是性能策略——并行与否只影响耗时，不影响结果。

3. **与 rayon::join 的差异**：API 语义几乎相同，差别在**什么时候选择真正并行**。rayon 的 join 在每次调用时都要做相对昂贵的任务推送/窃取决策，而 chili 用"平时纯顺序执行 + 周期性心跳才检查要不要分享工作"的策略，把单次 join 的开销压到了纳秒级。本讲的基准表会给出具体数字，机制细节留到 u2-l1 精读。

关于 [Spice] 的渊源：Spice 是 judofyr（Magnus Holm）编写的极简并行库，chili 把它"心跳驱动的低开销工作分享"这一核心思路移植到了 Rust，并在 unsafe 代码、通道实现等处做了大量 Rust 风格的重新论证（这部分是 u3/u4 的内容）。README 和 `src/lib.rs` 的文档注释都明确标注了这一出处。

那 chili **适合什么场景**？README 说得很直白：

- 单次计算**很小**（many small computations）——小到不能容忍重的调度开销；
- **很难估计当前分支还剩多少工作**（expensive to estimate how many are left）——也就是没法用"剩余量超过阈值才并行"这类简单规则来决定何时停止跨线程分享工作。

反过来说，如果你手头是少量的大任务（比如 8 个各耗时 1 秒的独立请求），用 `std::thread::spawn` 手工开 8 个线程就够了，chili 帮不上什么忙；如果你能廉价地预知工作量，专门的分块并行往往更直接。

#### 4.1.2 核心流程

chili 的推荐使用形态是**递归 fork-join**。以 README 的二叉树求和为例，一次 `sum(node)` 调用的流程是：

```
sum(node):
    (left, right) = scope.join(
        闭包A: 对左子树递归 sum,
        闭包B: 对右子树递归 sum,
    )
    return node.val + left + right
```

把整棵树的执行画成图，就是一棵与原树同形的"调用树"：

```
                sum(root)
               /         \
        sum(L)             sum(R)
        /    \             /    \
    sum(LL) sum(LR)   sum(RL) sum(RR)
      ...      ...       ...      ...
```

- 每个内层节点是一次 `join`（一个 fork 点）；
- 每个fork 点上，chili **可能**把其中一个分支交给其他线程，也可能两个都在当前线程顺序执行；
- 每个分支返回后结果逐层向上汇总，最终根节点拿到总和。

注意闭包的签名很特别：`|s| ...` 里的 `s` 是 `&mut Scope`。也就是说，**闭包内部还可以继续用同一个 scope 发起下一层 `join`**——这就是递归并行得以展开的机制。这一点在 u1-l3 会动手实践，这里先留个印象。

这种模型为什么和"难以估计剩余工作量"天然契合？因为每个 fork 点都不需要做估计：它默认顺序执行（零成本），靠周期性心跳在合适的时机把积压的工作分出去。工作量估计这个难题被完全绕开了。

#### 4.1.3 源码精读

先看 README 开头的定位陈述：

> [README.md:L1-L13](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/README.md#L1-L13)
>
> 这一段是项目的"自我介绍"：第 6 行给出副标题——"Rust port of [Spice], a low-overhead parallelization library"；第 8-10 行说明它是一个与 `rayon::join` 几乎相同的极低开销并行原语，在计算中的任意 fork 点*可能*并行执行传入的两个闭包；第 12-13 行给出最适合的场景——大量小计算、且难以估计当前分支剩余工作量。

其中第 12-13 行的原文是：

```text
It works best in cases where there are many small computations and where it is
expensive to estimate how many are left on the current branch in order to stop trying to share work across threads.
```

这句话是理解 chili 全部设计取舍的钥匙，值得反复读。

接着是 README 的示例——对二叉树所有节点求和：

> [README.md:L19-L28](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/README.md#L19-L28)
>
> 这段代码定义了递归函数 `sum`：它用 `scope.join` 同时发起"左子树求和"与"右子树求和"两个闭包，拿到 `(left, right)` 结果后返回 `node.val + left + right`。注意两个闭包的参数 `s`，它们把 `scope` 传进了下一层递归。

README 还特意解释了为什么这是"理想示例"：

> [README.md:L30-L31](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/README.md#L30-L31)
>
> 因为每个节点的计算量极小（读一个 `u64` 加一次法），而且节点并不记录自己还有多少后代——正好命中 chili 的两大适用条件。

这个示例在 `src/lib.rs` 的 crate 级文档注释里有一个**可直接运行的完整版本**，包含 `Node` 的定义、`Node::tree(layers)` 构造器和最终断言：

> [src/lib.rs:L5-L14](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L5-L14)
>
> crate 文档的开头复述了 README 的定位：面向可潜在并行执行的极低开销 fork-join 工作负载，同样强调"大量小计算 + 难以估计剩余工作量"，并附上 Spice 的出处链接。

> [src/lib.rs:L18-L48](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L18-L48)
>
> 这是一段 Rust 文档测试（doctest）：`Node::tree(layers)` 自顶向下构造一棵满二叉树（第 27-34 行），第 36-43 行是与 README 相同的 `sum` 函数，第 45-47 行构造 `tree(10)` 并断言 `sum(&tree, &mut Scope::global()) == 1023`。注意第 47 行出现的 `Scope::global()`——它是获取全局线程池作用域的入口，u1-l3 会用到。

顺带读一下 `src/lib.rs` 最顶上的三个 lint 开关，它们透露了这个库的工程品味：

> [src/lib.rs:L1-L3](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L1-L3)
>
> `#![deny(missing_docs)]` 要求所有公共项必须有文档；`#![deny(unsafe_op_in_unsafe_fn)]` 和 `#![deny(clippy::undocumented_unsafe_blocks)]` 要求所有 unsafe 操作必须带 `SAFETY` 论证注释。一个大量使用 unsafe 的并发库把这两条设为 deny，等于把"安全论证"变成了编译期强制。

#### 4.1.4 代码实践

**实践目标**：不写代码，通过精读 + 手算，确认你理解了 fork-join 模型和 "may" 语义。

**操作步骤**：

1. 打开 [README.md:L19-L28](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/README.md#L19-L28) 的示例，对照 [src/lib.rs:L18-L48](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L18-L48) 的完整版本，弄清 `Node::tree(layers)` 如何构造出一棵满二叉树。
2. 在纸上为 `Node::tree(3)` 画出那棵树，标出每个节点的 `val`。
3. 在树旁边画出对应的"join 调用树"，标出每次 `join` 发生在哪个节点、返回什么。
4. 手算 `sum(&tree(3), ...)` 的返回值，再手算 `tree(10)` 的返回值，与 doctest 第 47 行的断言值对照。

**需要观察的现象**：满二叉树的节点数满足

\[ N = 2^{L} - 1 \]

其中 \( L \) 是层数。所以 `tree(3)` 有 \( 2^3 - 1 = 7 \) 个节点，每个 `val` 都是 1，总和为 7；`tree(10)` 有 \( 2^{10} - 1 = 1023 \) 个节点，总和 1023——这正是 doctest 断言里的数字。README 基准表里的 1023、16777215（\( 2^{24}-1 \)）、134217727（\( 2^{27}-1 \)）也都来自同一个公式，记住它，4.2 节会反复用到。

**预期结果**：你能在不看答案的情况下说出"`tree(10)` 有 1023 个节点，因为满二叉树层数为 L 时节点数是 2^L - 1"，并能解释 `join` 调用树与原树同形、每个内层节点是一个 fork 点。

（本实践为纯阅读手算型，无需运行命令。）

#### 4.1.5 小练习与答案

**练习 1**：`scope.join(f, g)` 是否保证 f 和 g 一定运行在两个不同的线程上？如果不保证，结果会因此不确定吗？

**参考答案**：不保证。README 第 8-10 行明确用了 "*may* run the two passed closures in parallel" 的措辞——chili 只保证两个闭包都会被执行、结果被正确汇合；至于是否真的并行，取决于运行时的调度决策。因此无论是否并行，`join` 的返回值都一样，"可能"只影响耗时，不影响语义正确性。

**练习 2**：README 第 30-31 行说求和示例是"理想示例"，它理想在哪两点？

**参考答案**：（1）每个节点的计算量极小（many small computations），任何过重的调度开销都会显著拖慢整体，所以必须用低开销原语；（2）节点不记录自己还剩多少后代（expensive to estimate how many are left），无法用"剩余工作量阈值"来决定何时停止分享工作，正好需要 chili 那种不做工作量估计的策略。

**练习 3**：如果你要并行化的任务是"8 个各耗时 1 秒的独立 HTTP 请求"，chili 是好选择吗？为什么？

**参考答案**：不是好选择。chili 的优势在于把**海量小任务**的转交开销压到纳秒级；8 个大任务直接 `std::thread::spawn` 开 8 个线程、`join_handle.join()` 收结果即可，转交开销相比 1 秒的计算量完全可以忽略，用不上 chili 的低开销特性。

### 4.2 模块二：基准数据解读

#### 4.2.1 概念说明

README 用三张表展示性能。理解它们需要先明确几个概念：

- **Baseline（基线）**：纯顺序执行的单线程版本——对同一棵树直接递归求和，不做任何并行。它是所有对比的参照物。
- **Rayon**：用 `rayon::join` 实现的版本。rayon 是 Rust 生态中最主流的数据并行库，把它放进表里是为了给 chili 一个强有力的参照。
- **chili**：本库的版本。
- **加速比（speedup）**：表中最后一列 `Baseline / chili`，即基线耗时除以 chili 耗时。比值大于 1 表示并行赚了（例如 x6.94 表示快了 6.94 倍）；**比值小于 1 表示并行反而更慢**——第 1K 节点行的 x0.53 就是这种情况，这是完全正常的，不是数据错误。

测试方法是：对一棵含指定节点数的**平衡二叉树**求和所有节点值，记录总耗时。节点数取 1023、16777215、134217727 这类 \( 2^L - 1 \) 的值，正是因为它们对应整层数的满二叉树。

#### 4.2.2 核心流程

读懂这三张表需要会算三个派生指标。

**指标一：加速比**

\[ S = \frac{T_{\text{baseline}}}{T_{\text{chili}}} \]

以 AMD 机器 16M 节点行为例：\( S = 94.4\,\text{ms} / 13.6\,\text{ms} \approx 6.94 \)，与表中一致。

**指标二：每节点摊销耗时**

把总耗时除以节点数，就能看出"处理一个节点平均花多久"：

\[ t = \frac{T}{N} \]

这是衡量并行库**单次开销**最直接的口径。AMD 机器上 134M 节点行：\( t = 101.8\,\text{ms} / 134217727 \approx 0.76\,\text{ns} \)，README 把它 rounded 成 0.8ns。

**指标三：理论并行上限下的每节点耗时**

8 核机器若达到理想线性加速，每节点摊销耗时应当是单线程的八分之一：

\[ t_{\text{ideal}} = \frac{t_{\text{seq}}}{8} = \frac{1.8\,\text{ns}}{8} \approx 0.2\,\text{ns} \]

其中 1.8ns 来自 1K 节点行的基线（\( 1.8\,\mu s / 1023 \approx 1.8\,\text{ns} \)）。README 第 40-43 行的比较正是这两个数：实际 0.8ns vs 理想 0.2ns，说明已接近（但未达到）理论上限，剩余差距来自缓存、内存带宽等并行不可避免的代价。

三张表各自的叙事重点：

1. **AMD 表（第 45-49 行）**：chili 在大数据量下拿到 x6.94 / x7.83 的加速比，接近 8 核理论上限；同时提醒读者实际的每节点 0.8ns 高于理想 0.2ns。
2. **Apple M1 表（第 53-57 行）**：在不同架构上重复实验，chili 稳定 x3.5 左右；特别值得注意的是 16M 行 rayon 是 40.5ms、基线是 39.4ms——**rayon 在这一行几乎没有从并行中获益**，而 chili 是 11.2ms。
3. **开销表（第 64-66 行）**：把 1K 节点案例的每节点耗时按线程数展开，结论是开销**几乎不随线程数增长**（1/2/4/8 线程都是 3.5ns）。这说明 chili 的低开销不是靠"少开线程"换来的。

#### 4.2.3 源码精读

基准测试的总说明：

> [README.md:L33-L36](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/README.md#L33-L36)
>
> 说明测量内容：对一棵节点数可变的平衡二叉树求所有节点值之和所需的时间。这解释了为什么节点数都是 \( 2^L - 1 \) 形式的满二叉树规模。

AMD Ryzen 7 4800HS（8 核）上的数据与那段重要的"上限分析"：

> [README.md:L40-L43](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/README.md#L40-L43)
>
> 这段话指出：134M 节点案例相对基线的提升接近理论最大值，但每节点实际耗时是 0.8ns，而若按 1K 节点案例的单线程水平理想地除以 8，应该是 0.2ns。这是 README 中唯一一段"自我批判式"的分析，展示了对并行开销的诚实态度。

> [README.md:L45-L49](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/README.md#L45-L49)
>
> AMD 主表。逐行读：1023 节点时 baseline 1.8µs、rayon 51.1µs、chili 3.4µs——两者都比顺序版慢，chili 慢约 1.9 倍而 rayon 慢约 28 倍；16M 节点时 chili 拿到 x6.94；134M 节点时 x7.83。

Apple M1（8 核）上的数据：

> [README.md:L51-L57](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/README.md#L51-L57)
>
> M1 表。1023 节点时同样是"并行更慢"（x0.46）；16M 与 67M 节点时 chili 稳定在 x3.5 左右，而 rayon 在 16M 行（40.5ms vs 39.4ms）几乎与基线打平。

chili 开销随线程数的变化：

> [README.md:L59-L66](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/README.md#L59-L66)
>
> 开销表。结论句在第 61-62 行：1K 节点案例的开销相对线程数近似恒定。表中 1/2/4/8 线程的每节点耗时均为 3.5ns。换个角度验证：\( 3.5\,\text{ns} \times 1023 \approx 3.58\,\mu s \)，与主表中 1K 行的 3.4µs 量级吻合。

表下的两个参考链接：

> [README.md:L68-L69](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/README.md#L68-L69)
>
> `[Spice]` 指向原版 Spice 仓库，`[`rayon::join`]` 指向 rayon 文档——本讲 4.1 提到的两个"参照物"的出处。

#### 4.2.4 代码实践

**实践目标**：亲手复算 README 表格中的派生数字，确认你能独立解读这些基准数据，而不是被动接受结论。

**操作步骤**：

1. 打开 [README.md:L45-L49](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/README.md#L45-L49)，用计算器（或 `python3` / `bc`）复算最后一列：
   - `94.4 / 13.6`、`797.5 / 101.8`，对照表中的 x6.94 / x7.83。
2. 复算 M1 表（第 53-57 行）的 `39.4 / 11.2` 与 `156.5 / 44.3`。
3. 复算每节点摊销耗时：
   - \( 1.8\mu s / 1023 \)（应约等于 1.8ns，即基线单节点耗时）；
   - \( 101.8\text{ms} / 134217727 \)（应约等于 0.76ns，README 取 0.8ns）；
   - \( 51.1\mu s / 1023 \)（rayon 在 1K 规模下的每节点耗时，观察它与 chili 的 \( 3.4\mu s / 1023 \approx 3.3\text{ns} \) 差多少倍）。
4. 把第 3 步的三个数填进你自己画的一张小表：`1K 节点、每节点耗时：baseline / rayon / chili`。

**需要观察的现象**：rayon 在 1K 规模下的每节点耗时约为 chili 的十几倍；而 baseline 每节点约 1.8ns，chili 约 3.3ns——**chili 把"并行化的单节点代价"压到了只比纯顺序多约 1.5ns 的水平**。

**预期结果**：所有复算值与 README 表格及第 40-43 行的文字一致（允许四舍五入误差）；你得到一张类似下面的小表：

| 1K 节点每节点耗时 | baseline | rayon | chili |
| --- | --- | --- | --- |
| AMD 4800HS | ≈1.8 ns | ≈50 ns | ≈3.3 ns |

（本实践只需计算器；如在本机运行 `python3 -c "print(51.1/1023)"` 之类的命令，结果以你本地输出为准。）

#### 4.2.5 小练习与答案

**练习 1**：AMD 表 1023 节点行的加速比是 x0.53，这个小于 1 的数字说明了什么？它是 chili 的缺陷吗？

**参考答案**：说明在任务总量极小（1K 个节点、总计算量约 1.8µs）时，并行的固定开销超过了计算本身，chili（3.4µs）比顺序执行（1.8µs）更慢。这不是 chili 特有的缺陷——同一行里 rayon 是 51.1µs（慢约 28 倍）；任何并行方案在小任务上都有此现象，chili 只是把"扭亏为盈"的规模临界点压得极低（在 16M 节点行就拿到 x6.94）。

**练习 2**：为什么说第 64-66 行的开销表"每节点耗时恒定为 3.5ns"是一个重要的结论？如果它随线程数增长会发生什么？

**参考答案**：它说明把线程池从 1 线程扩到 8 线程，单次 join 的代价并不上涨——低开销特性在多线程下仍然成立。如果该值随线程数增长（例如 8 线程时涨到 20ns），那么加线程带来的并行收益会被更高的单任务开销部分吃掉，小任务场景的临界点会随线程数变差，库的适用范围就大大缩小了。

**练习 3**：M1 表 16M 节点行，rayon 耗时 40.5ms 而基线是 39.4ms。结合两台机器的数据，你能对 "chili vs rayon::join" 的差异下一个什么结论？

**参考答案**：在"大量极小任务"这一 chili 明确瞄准的场景里，rayon 的每次 join 决策开销太高，以至于在中等规模（16M 节点）下几乎无法从并行中获益（AMD 上仅 94.4→58.1ms，M1 上甚至基本打平），而 chili 分别做到 13.6ms 与 11.2ms。结论：两者 API 语义几乎相同，但 chili 通过降低单次 fork 的开销，把并行有效的任务规模区间大幅向下扩展了。

### 4.3 模块三：依赖与技术栈

#### 4.3.1 概念说明

打开 `Cargo.toml`，最先应注意到的事实是：**chili 没有任何运行时依赖**。整个文件里只有 `[dev-dependencies]`（开发依赖），没有 `[dependencies]` 小节。这意味着：

- chili 编译进你的项目时不会拖进任何第三方 crate；
- 供应链风险面为零，编译时间极短；
- 它所依赖的全部并发设施——`std::thread`、`std::sync` 里的 `Mutex`/`Condvar`/`atomic` 等——都来自标准库。`src/lib.rs` 第 50-61 行的 `use std::{...}` 可以印证这一点。

两个开发依赖各有用途：

- **divan**：基准测试框架。README 那三张表的数据来源 `benches/overhead.rs` 就是用它写的（u4-l3 精读）。
- **rayon**：只作为基准里的**对照组**出现，不是功能依赖。这解释了为什么 API 几乎相同的 rayon 可以心安理得地出现在 dev-dependencies 里——它是被测对象，不是被用对象。

包元信息方面值得注意的点：

- `version = "0.2.1"`、`edition = "2021"`：仍在 0.x 阶段，API 尚未承诺稳定；
- `license = "MIT OR Apache-2.0"`：Rust 生态最常见的双许可，使用者可任选其一；
- `keywords` 与 `categories`：crates.io 上的检索标签，全部围绕并发/并行；
- `documentation = "https://docs.rs/chili"`：文档由 docs.rs 自动构建，README 顶部的 Docs 徽章（第 4 行）就指向它。

#### 4.3.2 核心流程

Cargo 对依赖的分级处理流程：

```
解析 Cargo.toml:
    [dependencies]        -> 进入「正常依赖」：编译 lib 本体时需要      （chili: 空）
    [dev-dependencies]    -> 只用于 tests / examples / benches        （chili: divan, rayon）
    [[bench]] 声明:
        name = "overhead"
        harness = false   -> 不使用 libtest 默认 harness，
                             由 benches/overhead.rs 自己提供 main（divan 的惯例）
```

`harness = false` 是理解 `benches/overhead.rs` 结构的钥匙：默认情况下 `cargo bench` 用 libtest 驱动基准，而 divan 这类框架需要自己接管 `main` 函数，因此要关掉默认 harness。这也是为什么 `cargo bench` 能直接跑起 divan 的基准。

对使用者而言的发布形态链路：crates.io 上的 `chili` 包（当前 0.2.1）→ docs.rs 自动构建的 API 文档 → GitHub 仓库（`repository` 字段指向）。三者由 `Cargo.toml` 的元信息串起来。

#### 4.3.3 源码精读

包的元信息与许可证：

> [Cargo.toml:L1-L12](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/Cargo.toml#L1-L12)
>
> 第 2-3 行是包名与一句话描述 "low-overhead parallelization library"；第 4 行版本 0.2.1，第 5 行 edition 2021；第 7-8 行分别指向 GitHub 仓库与 docs.rs 文档；第 9-10 行是 crates.io 的检索关键词（join/concurrency/parallel/spice——"spice" 再次印证渊源）与分类（concurrency）；第 11 行是 MIT OR Apache-2.0 双许可；第 12 行指定 README 文件。

开发依赖与基准声明：

> [Cargo.toml:L14-L20](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/Cargo.toml#L14-L20)
>
> 第 14-16 行声明 dev-dependencies：divan 0.1.14（基准框架）与 rayon 1.10.0（基准对照组）。第 18-20 行声明名为 `overhead` 的 bench 目标并设置 `harness = false`，把基准入口交给 divan。**注意整个文件没有 `[dependencies]` 小节——运行时零依赖。**

README 顶部与 crates.io / docs.rs 相关的徽章：

> [README.md:L1-L4](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/README.md#L1-L4)
>
> 两个徽章分别链接到 crates.io 的包页面（版本信息）和 docs.rs 的文档页面——也就是综合实践中要去看的两个页面。

标准库设施的使用（作为"零第三方依赖"的佐证）：

> [src/lib.rs:L50-L61](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L50-L61)
>
> chili 的全部并发原料都来自 `std`：`BTreeMap`/`HashMap` 集合、`NonZero`、`Mutex`/`Condvar`/`Barrier`/`OnceLock`/`Weak`/`Arc` 同步原语、`atomic::AtomicBool` 原子类型、`thread` 与时间类型。没有一行第三方 import。

（仓库中还有 `LICENSE-APACHE` 与 `LICENSE-MIT` 两个许可文本文件，与 `Cargo.toml` 第 11 行的双许可声明对应。）

#### 4.3.4 代码实践

**实践目标**：亲眼验证"零运行时依赖"，并熟悉 chili 在 crates.io / docs.rs 上的发布形态。

**操作步骤**：

1. 在仓库根目录运行：

   ```bash
   cargo tree
   ```

   `cargo tree` 打印依赖树。观察输出的依赖树里 `chili` 节点下面有没有任何子节点。
2. 再运行一次带过滤的版本，只看正常构建依赖：

   ```bash
   cargo tree --edges normal
   ```
3. 在浏览器打开 <https://crates.io/crates/chili>，记录：当前发布版本号、最近更新时间、依赖（Dependencies）一栏的内容、License 标签。
4. 再打开 <https://docs.rs/chili>，找到 `Scope` 结构体的文档页（这是下一讲就会用到的类型），随便浏览一下它的方法列表。

**需要观察的现象**：`cargo tree` 的输出应当只有 `chili v0.2.1 (<本地路径>)` 这一个根节点，没有子依赖；crates.io 页面的 Dependencies 栏应为 0 个（或仅列出 dev 依赖且明确标注）；docs.rs 上能看到与 `src/lib.rs` 文档注释对应的渲染结果（例如 `Scope` 的说明和 4.1.3 节引用的那个二叉树示例）。

**预期结果**：你确认了"chili 运行时零第三方依赖"这一事实，并知道以后查 API 该去 docs.rs 的哪个页面。若 `cargo tree` 显示出 divan/rayon，说明你大概率漏掉了 `--edges normal` 的语义（它们只在 dev 环境出现）。具体输出以你本地为准，网络访问 crates.io / docs.rs 的结果**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：rayon 出现在 `Cargo.toml` 的 `[dev-dependencies]` 里而不是 `[dependencies]` 里，这保证了什么？

**参考答案**：保证 rayon 只在编译 tests/benches 时被拉进来。任何依赖 chili 的下游项目都不会因此多编译 rayon，chili 的库本体保持零第三方依赖、编译极快、供应链面为零。同时它又完整保留了"与 rayon 对比测性能"的能力——因为基准属于 dev 环境。

**练习 2**：`[[bench]]` 里的 `harness = false` 是什么意思？为什么 divan 需要它？

**参考答案**：表示该 bench 目标不使用 cargo/libtest 默认的基准 harness，而由 `benches/overhead.rs` 自己提供 `main` 函数。libtest 的默认 harness 面向 `#[bench]` 风格，而 divan 这类独立框架需要接管整个基准流程（自己解析参数、自己跑迭代、自己输出表格），所以要显式关闭默认 harness。

**练习 3**：`license = "MIT OR Apache-2.0"` 中的 "OR" 意味着使用者拥有什么权利？

**参考答案**：双许可任选：使用者可以在 MIT 和 Apache-2.0 两份许可中**任选其一**遵循，而不需要同时满足两者。这是 Rust 生态的事实标准组合——MIT 简短宽松，Apache-2.0 则提供明确的专利授权条款，两者互补。

## 5. 综合实践

本讲的综合实践把三个模块串起来：**读懂官方基准 → 查证发布形态 → 反思自己的项目**。

### 5.1 任务说明

阅读 README 中的两台机器基准表，在 docs.rs 或 crates.io 上查看 chili 的发布信息，然后写下你自己项目（或你熟悉的任何项目）中三类适合、三类不适合使用 chili 的 workload，并说明理由。

### 5.2 操作步骤

1. **精读基准表**：对照 [README.md:L38-L57](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/README.md#L38-L57) 和 [README.md:L59-L66](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/README.md#L59-L66)，把两台机器的数据各抄一遍，标出每行的加速比和"并行是否已经扭亏为盈"。
2. **查证发布形态**：打开 <https://crates.io/crates/chili> 与 <https://docs.rs/chili>，记录版本号、发布时间、依赖数量、许可证、文档入口（对应 [Cargo.toml:L1-L12](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/Cargo.toml#L1-L12) 中的元信息）。
3. **写 workload 笔记**：新建一个笔记文件（放在你自己的笔记目录，不要放进 chili 仓库），按下面的模板写六条：

   ```text
   ## 适合 chili 的 workload
   1. <名称>：<调用形态（是否递归分治）>、<单次计算量级>、<能否估计剩余工作量>、<为什么适合>
   2. ...
   3. ...

   ## 不适合 chili 的 workload
   1. <名称>：<不满足哪条适用条件>、<更合适的替代方案>
   2. ...
   3. ...
   ```

4. **用 README 的语言复盘**：每条理由至少引用一次 README 第 12-13 行的两个适用条件（"大量小计算"、"难以估计剩余工作量"）或基准表里的一个数字。

### 5.3 需要观察的现象

- 你写"适合"的三条，应当都同时满足"任务数量多且单任务小"与"无法廉价估计分支剩余工作量"（或者至少满足前者且不排斥后者）；
- 你写"不适合"的三条，通常落入这几类：任务数少而单个大（直接 `std::thread`）、任务之间有顺序依赖（fork-join 帮不上忙）、需要流式/迭代器式数据并行（rayon 的并行迭代器更合适）、或总计算量小到连 3.5ns/节点的开销都 cover 不住（对照 1K 节点行 x0.53 的教训）。

### 5.4 预期结果

产出一份包含六条 workload 判断的笔记，每条都有引用了 README 依据的理由。判断标准示例（供对照，你应写出自己场景的版本）：

| 判断 | 例子 | 依据 |
| --- | --- | --- |
| 适合 | 对一棵大 JSON/AST 树的每个节点做轻量变换后归并 | 节点多、单节点计算小、子树大小不遍历不知道 |
| 适合 | 大规模空间数据（四叉树/八叉树）的递归求交 | 天然分治，分支工作量取决于数据分布，难预估 |
| 不适合 | 批量下载 100 个 URL | 任务数少、单个耗时长，`std::thread` 即可 |
| 不适合 | 对一个大数组每个元素乘 2 | 规则数据并行，rayon 的 `par_iter` 表达力更强 |

## 6. 本讲小结

- chili 是 [Spice] 的 Rust 移植版：一个与 `rayon::join` API 语义几乎相同的**低开销 fork-join 并行原语**，在任意 fork 点"*可能*"并行执行两个闭包——只影响耗时，不影响结果。
- 它的黄金场景是**大量小计算 + 难以估计分支剩余工作量**，README 的二叉树求和示例同时命中这两条。
- 基准数据三个关键读数：大数据量下接近理论上限的加速比（AMD 134M 节点 x7.83）；极小任务下并行必然更慢但 chili 亏得最少（1K 节点 chili 3.4µs vs rayon 51.1µs）；每节点开销约 3.5ns 且**不随线程数增长**。
- 项目体积极小：`src/lib.rs` + `src/job.rs` 两个源文件，基准在 `benches/overhead.rs`。
- **运行时零第三方依赖**：`Cargo.toml` 只有 `[dev-dependencies]`（divan 做基准、rayon 做对照组），全部并发设施来自标准库。
- 发布形态：crates.io 上的 `chili` 0.2.1（MIT OR Apache-2.0 双许可），文档由 docs.rs 自动构建，README 顶部徽章即入口。

## 7. 下一步学习建议

下一讲 **u1-l2《构建、运行与代码结构》** 将把项目真正跑起来：依次执行 `cargo check / test / clippy / bench`，弄清 `src/lib.rs` 与 `src/job.rs` 的分工，并解读 CI 流水线（包括专门审读 unsafe 并发代码的 miri）每一步在守护什么。

在进入下一讲之前，建议先做两件小事：

1. 通读一遍 [src/lib.rs 的 crate 文档](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L5-L48)，把 4.1.3 节那个 doctest 示例完全看懂——它是 u1-l3 动手写第一个并行程序的底稿。
2. 到 [Spice 原仓库](https://github.com/judofyr/spice) 浏览一下 README，直观感受 chili 所移植的原始设计思想；带着"chili 在 Rust 里如何重新表达这些想法"的问题进入后续单元。

[Spice]: https://github.com/judofyr/spice
[`rayon::join`]: https://docs.rs/rayon/latest/rayon/fn.join.html
