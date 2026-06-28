# Profiling——找出热点代码

## 1. 本讲目标

本讲承接 u2-l1「Benchmarking——建立可比较的性能基线」。上一讲我们学到：优化必须**先测量再优化**，基准测试负责回答「**改了之后变快了吗**」。但基准测试不会告诉你「**应该改哪里**」——这正是**性能剖析（profiling）**的职责。

学完本讲，你应当能够：

1. 理解性能剖析的作用——在一大段程序里定位「热点（hot code）」，即被执行得足够频繁、足以影响运行时间的代码。
2. 认识 perf-book 第 5 章「Profiling」中推荐的一组剖析器，并能根据目标（CPU 热点 / 堆分配 / 因果潜力）选出合适的工具。
3. 掌握**为 release 构建开启源码行级调试信息**（`debug = "line-tables-only"`）和**强制保留帧指针**（`-C force-frame-pointers=yes`）这两项让剖析结果「可信」的必要配置。
4. 理解 Rust 的**符号 mangling**（符号名修饰）现象，以及用 `rustfilt` 或切换到 **v0 格式**让剖析输出可读的方法。

本讲引用的真实源码是 perf-book 自身的两个 Markdown 章节文件：`src/profiling.md` 与 `src/build-configuration.md`。注意（见 u1-l2）：perf-book 是一本**书**，它的「源码」就是 Markdown 书稿，本身不是可运行的 Rust 程序。因此本讲的代码实践会使用一个**外部示例小程序**来演示剖析流程。

## 2. 前置知识

- **基准测试 vs 剖析**（来自 u2-l1）：基准测试衡量「结果是否变快」，剖析定位「该改的位置」。两者构成闭环：剖析找热点 → 改代码 → 基准测试验证。本讲只讲前半段。
- **release 构建**：剖析永远在 `--release` 构建上进行，因为 dev 构建未经优化，热点分布与生产环境完全不同，测了也没意义。
- **采样（sampling）与归因（attribution）**：大多数剖析器靠**周期性采样**程序计数器/调用栈，把样本数按函数或源码行汇总。函数拿到的样本越多，说明它越「热」。要让这种归因准确，编译产物里必须保留**源码行信息**和**可还原的调用栈**——这正是本讲第 3、4 个最小模块要解决的工程问题。
- **符号名（symbol）**：编译后的机器码里，函数以「符号名」标识。Rust 会对名字做编码（mangling），直接看会是一串难读字符，本讲第 4 个模块讲解如何还原。

> 关于指标的方差（wall-time / cycles / instruction counts 各自的优缺点），上一讲 u2-l1 已详细讨论，本讲不再重复。

## 3. 本讲源码地图

本讲涉及的「源码」是 perf-book 的两个章节文件：

| 文件 | 在书中的位置 | 本讲用它讲解什么 |
| --- | --- | --- |
| [src/profiling.md](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/profiling.md) | SUMMARY.md 第 5 项「Profiling」 | 全部 4 个最小模块的主要依据：剖析器清单、调试信息、帧指针、符号 demangling |
| [src/build-configuration.md](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/build-configuration.md) | SUMMARY.md 第 3 项「Build Configuration」 | 补充背景：为什么构建配置会影响「可剖析性（profilability）」，以及 `strip` 为何会妨碍剖析 |

这两个文件是兄弟章节。`build-configuration.md` 在开头明确把 **profilability（可剖析性）**列为构建配置会影响的一个特征；而 `profiling.md` 则告诉你具体如何开启它。把它们放在一起读，才能理解「为何要改 Cargo.toml / RUSTFLAGS」。

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：

1. 性能剖析找热点
2. 常用剖析器（perf / samply / flamegraph / DHAT 等）
3. 为 release 构建开启调试信息与帧指针
4. 符号 demangling

### 4.1 性能剖析找热点

#### 4.1.1 概念说明

当我们要优化一个程序时，首先得知道**哪些部分值得改**。perf-book 的开篇一句话定义了这个问题：

> 你需要一种方法来判断程序的哪些部分是「hot」（被执行得足够频繁、足以影响运行时间）、值得修改。

这是优化的**第一性原理**：运行时间几乎总是高度集中在一小部分代码上。通俗地说就是「二八原则」——大约 20% 的代码贡献了大部分运行时间。如果用 \(T\) 表示总运行时间，用 \(t_i\) 表示第 \(i\) 段代码贡献的时间，那么有：

\[
T = \sum_i t_i, \quad \text{而通常少数几段 } t_i \text{ 占据了绝大部分 } T
\]

所以，**优化的收益上界由「热点贡献了多少时间」决定**。把一段几乎不执行的冷代码改快 10 倍，总运行时间几乎不变；把占 80% 时间的热函数改快 10%，总时间能下降近 \(0.8 \times 9/10 = 72\%\)。**性能剖析（profiling）就是用来找出这些 \(t_i\) 最大的代码段**。

注意「热点」的定义是**执行频率**，而不是代码行数多寡、也不是「看起来复杂」。一段长但只执行一次的初始化代码是冷的；一段短但处在最内层循环的代码是热的。

#### 4.1.2 核心流程

一次典型的剖析驱动优化（profile-driven optimization）流程：

1. 在 **release 构建**上运行程序，用剖析器采集运行时的函数/源码行采样数据。
2. 把样本按函数（或按源码行）汇总，得到「自耗时（self time）」和「总耗时（inclusive time）」。
3. 找出耗时占比最高的若干函数——它们就是热点。
4. 针对最热的那一两个函数动手优化。
5. 回到 u2-l1 的基准测试，验证改动是否真的让总时间下降。
6. 若仍有优化空间，重复 1–5。

伪代码描述步骤 2 的归因逻辑：

```text
对每一次采样:
    展开当前调用栈 stack
    对栈中每一帧(函数) f:
        profile[f].inclusive += 1     # 包含子调用的总时间
    profile[栈顶函数].self += 1        # 只算自己、不含子调用的时间
排序输出 profile (按 self 或 inclusive 降序)
```

`self` 高，说明这个函数**自身**在做大量计算（典型热点）；`inclusive` 高但 `self` 不高，说明它只是「门面」，时间都花在它调用的子函数上。优化时优先盯 `self` 高的函数。

#### 4.1.3 源码精读

perf-book 在「Profiling」章开宗明义地点出剖析的目的：

[src/profiling.md:3-5](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/profiling.md#L3-L5) —— 用中文说明：优化程序时，你需要一种方式来判断哪些部分是「hot」（被执行得足够频繁、足以影响运行时间）从而值得修改，而这件事最好通过 profiling 来完成。

这两行就是本讲的纲领：**剖析 = 定位值得修改的热点**。它没有给出具体工具（那是 4.2 的事），而是先建立「为什么要剖析」的共识。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，帮助你在脑子里建立「self vs inclusive」的直觉。

1. **实践目标**：用一段示例代码，预测哪一行最热，再理解剖析会如何归因。
2. **操作步骤**：阅读下面这段**示例代码**（非 perf-book 原有代码），不运行，先自己判断：

   ```rust
   // 示例代码：用于体会 hot / cold 与 self / inclusive
   fn main() {
       let data: Vec<u64> = (0..10_000_000).collect(); // 冷：只执行一次
       let total = sum(&data);                          // 热点入口：inclusive 高
       println!("{}", total);                           // 冷：只执行一次
   }

   fn sum(xs: &[u64]) -> u64 {
       let mut acc = 0u64;
       for &x in xs {                                   // 热点本体：self 最高
           acc = acc.wrapping_add(x);
       }
       acc
   }
   ```

3. **需要观察的现象**：
   - `data` 的构造与 `println!` 各执行一次 → 冷，不应在热点榜前列。
   - `sum` 的 `inclusive` 很高（它几乎占了 `main` 的全部时间），但其 `self` 主要落在那一行 `for` 循环上。
4. **预期结果**：剖析器会把绝大多数样本归因到 `sum` 内部的循环行（`self` 最高），`main` 的 `inclusive` 与 `sum` 接近但 `self` 很低。
5. 待本地验证：可在完成 4.3 的配置后，用真实剖析器核对上述预测。

#### 4.1.5 小练习与答案

**练习 1**：「热点」的定义是「执行频率高到足以影响运行时间」，而不是「代码行数多」或「看起来复杂」。为什么？

> **答案**：运行时间几乎全部花在被频繁执行的代码上（二八原则）。行数多但几乎不执行的代码对 \(T\) 贡献极小，优化它几乎不改变总时间；而处在最内层循环的短代码虽小，却会因高频执行而主导 \(T\)。所以以「频率/耗时占比」而非「复杂度」来定义热点。

**练习 2**：基准测试（benchmarking）和性能剖析（profiling）分别在优化流程中扮演什么角色？

> **答案**：基准测试回答「**改了之后变快了吗**」，提供可比较的基线（见 u2-l1）；剖析回答「**应该改哪里**」，定位热点。前者衡量结果，后者指引方向，二者配合形成「定位热点 → 改 → 再测量」的闭环。

---

### 4.2 常用剖析器

#### 4.2.1 概念说明

perf-book 强调「有很多不同的剖析器，各有长短」，并给出一份**不完整但确实在 Rust 程序上成功用过**的清单。理解这些工具的关键，是按「它们到底在测什么」来分类：

- **CPU 采样剖析器**：周期性采样硬件性能计数器/调用栈，找 CPU 热点。代表：perf、samply、Instruments、VTune、AMD μProf。
- **火焰图封装**：在底层调用 perf/DTrace 采集后，把结果渲染成火焰图。代表：flamegraph（Cargo 子命令）。
- **指令计数与缓存模拟（Valgrind 系）**：精确统计每条指令、每次缓存命中/缺失、分支预测情况，方差极低。代表：Cachegrind、Callgrind。
- **堆 / 分配剖析器**：找哪段代码在做大量分配、峰值内存多少。代表：DHAT、dhat-rs、heaptrack、bytehound。
- **领域特化的「临时剖析」**：用 `eprintln!` 打日志再按频率后处理，得到贴合业务语义的洞察。代表：`counts`。
- **因果剖析器**：通过虚拟地「加速」某段代码来测量它的**优化潜力**。代表：Coz（配合 coz-rs）。

> 与 u2-l1 的呼应：Cachegrind/Callgrind 给的是**指令计数**（instruction counts），正是 u2-l1 中方差最低的那类指标；perf/samply 给的是带缓存的**采样**，更贴近真实硬件行为但方差更高。

#### 4.2.2 核心流程

不同类别工具的工作流略有差别：

- **采样剖析器（perf/samply/flamegraph）**：
  1. 准备一个带源码行信息与帧指针的 release 二进制（见 4.3）。
  2. 用剖析器启动程序并施加负载，例如 `samply record ./mybin` 或 `cargo flamegraph`。
  3. 剖析器周期性采样调用栈，结束后输出可交互的 profile / 火焰图。
  4. 在火焰图里找最宽的「平台」——那就是 self 时间最长的热点函数。

- **Valgrind 系（Cachegrind/Callgrind/DHAT）**：
  1. 同样准备 release 二进制。
  2. 在 Valgrind 模拟器里运行程序：`valgrind --tool=callgrind ./mybin` 等。
  3. 得到精确的指令/缓存/分配统计。注意：程序在模拟器里会**慢几十倍**，所以负载要小而具代表性。

- **dhat-rs（堆剖析，全平台）**：需要**改一点 Rust 代码**接入，换取在所有平台上可用的分配统计。

#### 4.2.3 源码精读

perf-book 的「Profilers」节给出完整清单，我们挑几个重点逐条对应（以下每条都注明该工具在做什么）：

[src/profiling.md:12-14](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/profiling.md#L12-L14) —— **perf** 是基于硬件性能计数器的通用剖析器，数据可用 **Hotspot** 或 **Firefox Profiler** 查看，运行于 Linux。

[src/profiling.md:19-20](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/profiling.md#L19-L20) —— **samply** 是一个采样剖析器，产出的 profile 可在 Firefox Profiler 中查看，跨 Mac/Linux/Windows。

[src/profiling.md:21-24](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/profiling.md#L21-L24) —— **flamegraph** 是一个 Cargo 子命令，底层调用 perf（Linux）或 DTrace（macOS/FreeBSD 等）来剖析代码，再把结果渲染成火焰图。

[src/profiling.md:25-27](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/profiling.md#L25-L27) —— **Cachegrind** 与 **Callgrind**（Valgrind 系）给出全局、逐函数、逐源码行的指令计数，以及模拟的缓存与分支预测数据，运行于 Linux 等部分 Unix。

[src/profiling.md:28-33](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/profiling.md#L28-L33) —— **DHAT** 擅长找出哪段代码在做大量**堆分配**、洞察**峰值内存**，也能定位对 `memcpy` 的热点调用；**dhat-rs** 是一个实验性替代，功能略弱、需要小幅改动你的 Rust 代码，但**全平台**可用。

[src/profiling.md:35-37](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/profiling.md#L35-L37) —— **`counts`** 支持「临时剖析（ad hoc profiling）」：把 `eprintln!` 与基于频率的后处理结合，适合得到贴合业务语义的局部洞察，全平台。

[src/profiling.md:38-39](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/profiling.md#L38-L39) —— **Coz** 进行**因果剖析（causal profiling）**来测量「优化潜力」，并通过 **coz-rs** 提供 Rust 支持，运行于 Linux。

把上面几条对照「4.2.1 概念说明」的分类表，就能按需求选工具：想看 CPU 热点选 perf/samply/flamegraph；想看精确指令数选 Cachegrind/Callgrind；想看分配与峰值内存选 DHAT/dhat-rs；想看业务语义的频率分布选 `counts`。

#### 4.2.4 代码实践

这是一个**源码阅读型实践**，强化「按需求选工具」的判断。

1. **实践目标**：给定一个具体的剖析需求，选出 perf-book 清单里最合适的工具并说明理由。
2. **操作步骤**：针对下面 3 个需求，分别写出你会用哪个工具（参考 4.2.3 的源码逐条）：
   - (a) 我的程序在 macOS 上跑，我想用 Firefox Profiler 看一个交互式火焰图，找出最耗 CPU 的函数。
   - (b) 我怀疑某段代码分配了过多内存、推高了峰值内存，想精确找出分配来源。
   - (c) 我想知道「如果我把函数 X 加速，程序整体能快多少」这种**潜力**评估。
3. **需要观察的现象**：每个需求都应能映射到清单中一个明确的工具。
4. **预期答案**（待你先思考后再对照）：
   - (a) **samply**：跨平台（含 macOS）、产出可在 Firefox Profiler 查看的采样 profile。
   - (b) **DHAT**（Linux/Unix）或 **dhat-rs**（全平台但需改代码）：专攻分配与峰值内存。
   - (c) **Coz**（配合 coz-rs）：进行因果剖析、直接测量优化潜力。
5. 待本地验证：若本地环境具备，可在完成 4.3 配置后实际运行 (a) 的 samply 流程。

#### 4.2.5 小练习与答案

**练习 1**：你的目标是找出哪段代码分配了大量内存，应该选 DHAT 还是 perf？为什么？

> **答案**：选 **DHAT**（或 dhat-rs）。perf 是基于硬件性能计数器的通用采样剖析器，主要反映 CPU 热点，并不直接统计堆分配次数与峰值内存；DHAT 专门用于定位分配密集的代码并给出峰值内存洞察。

**练习 2**：flamegraph（Cargo 子命令）和 samply 都能产出可查看的火焰图/profile，它们的底层机制有何不同？

> **答案**：flamegraph 是一个 Cargo 子命令，**底层调用 perf（Linux）或 DTrace（macOS 等）**来采集数据再渲染；samply **自身就是一个采样剖析器**，直接产出可在 Firefox Profiler 中查看的 profile，无需依赖 perf/DTrace。

**练习 3**：Cachegrind 给出的「指令计数」指标，相比 perf 的 wall-time 采样，主要优缺点是什么？（提示：回忆 u2-l1）

> **答案**：优点是**方差极低、可重复**（受内存布局波动影响小）；缺点是**不反映缓存与分支的真实硬件行为**（除非配合其模拟数据），且在 Valgrind 下程序运行慢几十倍，负载需精简。这正是 u2-l1 讨论的「无普适最优指标」的体现。

---

### 4.3 为 release 构建开启调试信息与帧指针

#### 4.3.1 概念说明

这是本讲最关键的工程要点。光有一个剖析器还不够——**剖析结果的质量，取决于编译产物里保留了什么信息**。perf-book 在「Profiling」章用三小节专门讲如何「喂」给剖析器正确的信息：

1. **源码行级调试信息（Debug Info）**：让剖析器能把样本归因到**具体源码行**。release 构建默认**不**生成这些信息，所以必须手动开启。
2. **帧指针（Frame Pointers）**：让剖析器在采样时能**可靠地展开调用栈**。Rust 编译器可能把帧指针优化掉，这会损害栈回溯（stack trace）的质量。
3. （配套认知）反过来，`build-configuration.md` 提醒：**`strip = "symbols"` 会剥离符号，让程序更难调试和剖析**。可见「为剖析准备二进制」与「为瘦身而 strip」是**对立**的两件事，需要权衡。

为什么这三点彼此关联？因为它们共同决定剖析器能否回答两个问题：「**这个样本属于哪个函数/哪一行**」（依赖调试信息 + 符号）和「**调用栈是怎么走到这里的**」（依赖帧指针）。任一项缺失，剖析图就会出现「归因错位」「栈断裂」「全是 `未知函数`」等问题。

#### 4.3.2 核心流程

为剖析准备一个「干净可读」的 release 二进制：

1. 在 `Cargo.toml` 里为 release profile 开启行级调试信息：

   ```toml
   [profile.release]
   debug = "line-tables-only"
   ```

2. 在构建命令里强制保留帧指针：

   ```bash
   RUSTFLAGS="-C force-frame-pointers=yes" cargo build --release
   ```

3. （可选，但推荐，见 4.4）把符号 mangling 切到可读性更好的 v0 格式：

   ```bash
   RUSTFLAGS="-C force-frame-pointers=yes -C symbol-mangling-version=v0" cargo build --release
   ```

4. 用剖析器运行这个二进制，此时火焰图里的函数名可读、调用栈完整、热点能归因到源码行。

注意事项（来自源码）：

- **标准库默认不带调试信息**，所以即便开启上述选项，标准库代码的剖析也不会很详细。perf-book 给了两种「重武器」方案（自建编译器与 std、或 nightly 的 `build-std`），但都标注「麻烦/有局限」，属于进阶选项。
- 这些设置**只影响剖析用的构建**，不要直接套用到要发布的最终二进制（尤其是会增大体积的项）。

#### 4.3.3 源码精读

**（一）调试信息**。perf-book 在「Debug Info」节开头就说明：要有效剖析 release 构建，可能需要开启源码行调试信息：

[src/profiling.md:62-67](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/profiling.md#L62-L67) —— 在 `Cargo.toml` 的 `[profile.release]` 下加 `debug = "line-tables-only"`，即可为 release 构建开启源码行级调试信息。

随后它点出一个重要限制——**标准库默认没有调试信息**：

[src/profiling.md:72-74](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/profiling.md#L72-L74) —— 即便做了上一步，标准库代码仍得不到详细剖析信息，因为随 Rust 发布的标准库并不是带调试信息构建的。

针对这一限制，perf-book 给出两个进阶（且麻烦）的方案：自建编译器与标准库（在 `bootstrap.toml` 里设 `debuginfo-level = 1`），以及 nightly 的 `build-std` 特性（可让标准库随你的程序一起、按相同配置编译，但生成的调试信息文件名不指向源码，所以对**需要源码**的 Cachegrind/samply 帮助有限）：

[src/profiling.md:76-83](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/profiling.md#L76-L83) —— 最可靠的办法是按官方说明自建编译器与标准库，并在仓库根的 `bootstrap.toml` 里加 `[rust] debuginfo-level = 1`；书中坦言这很麻烦，但某些情况下值得。

[src/profiling.md:87-92](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/profiling.md#L87-L92) —— 另一种是用不稳定的 `build-std` 特性把标准库纳入正常编译；但其调试信息里的文件名不指向源码，故对依赖源码的 Cachegrind、samply 等无法完全发挥作用。

**（二）帧指针**。perf-book 在「Frame pointers」节解释：Rust 编译器可能优化掉帧指针，从而损害栈回溯质量，因此需要用 `-C force-frame-pointers=yes` 强制保留：

[src/profiling.md:96-103](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/profiling.md#L96-L103) —— Rust 编译器可能优化掉帧指针，损害栈回溯等剖析信息的质量；用 `RUSTFLAGS="-C force-frame-pointers=yes" cargo build --release` 强制使用帧指针。

该书还给出了「持久化」写法——把同样的选项写进 Cargo 的 `config.toml`，对单个或多个项目统一生效：

[src/profiling.md:105-110](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/profiling.md#L105-L110) —— 也可以在 `config.toml` 的 `[build]` 下写 `rustflags = ["-C", "force-frame-pointers=yes"]`，对若干项目统一启用帧指针。

**（三）配套认知：strip 与可剖析性的权衡**。`build-configuration.md` 在开篇就把 **profilability（可剖析性）**列为构建配置会影响的特征之一：

[src/build-configuration.md:4-8](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/build-configuration.md#L4-L8) —— 构建配置会影响编译时间、运行速度、内存、二进制体积、**可调试性（debuggability）**、**可剖析性（profilability）**以及可运行的架构。

而在「Strip Symbols」节，它明确警告剥离符号会让程序更难剖析：

[src/build-configuration.md:299-302](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/build-configuration.md#L299-L302) —— 剥离符号可能让编译产物更难调试和剖析（例如 panic 的 backtrace 信息会变少），具体影响因平台而异。

把这两处与 `profiling.md` 的「开调试信息/帧指针」对照，就能得到一条完整权衡链：**为剖析要尽量保留信息（行表 + 帧指针 + 符号）；为瘦身要 strip 掉符号**——这两者方向相反，不能同时满足，所以要在「剖析专用构建」与「发布构建」之间区分对待。

#### 4.3.4 代码实践（本讲主实践）

这是本讲的**完整代码实践**，对应任务规格。请在一个**外部示例小程序**上完成（perf-book 本身无可运行代码）。

1. **实践目标**：为一个 release 构建加上 `debug = "line-tables-only"` 与 `-C force-frame-pointers=yes`，用 samply 或 perf+flamegraph 剖析一个小程序，定位最热的函数。
2. **操作步骤**：
   1. 新建一个有明确热点的示例项目：

      ```bash
      cargo new hot --bin && cd hot
      ```

   2. 把 `src/main.rs` 替换为下面这段**示例代码**（非 perf-book 原有代码；刻意让 `is_prime` 成为热点）：

      ```rust
      // 示例代码
      fn is_prime(n: u64) -> bool {
          if n < 2 { return false; }
          let mut i = 2u64;
          while i.checked_mul(i).map_or(false, |sq| sq <= n) {
              if n % i == 0 { return false; }
              i += 1;
          }
          true
      }

      fn main() {
          let mut count = 0u64;
          for n in 0..300_000 {
              if is_prime(n) { count += 1; }
          }
          println!("primes below 300000: {}", count);
      }
      ```

   3. 在 `Cargo.toml` 末尾追加（只给剖析用的 release 构建开行表）：

      ```toml
      [profile.release]
      debug = "line-tables-only"
      ```

   4. 开启帧指针并构建：

      ```bash
      RUSTFLAGS="-C force-frame-pointers=yes" cargo build --release
      ```

   5. 选一种剖析器运行（二选一）：
      - **samply**（推荐，跨平台、产出可在 Firefox Profiler 查看）：

        ```bash
        cargo install samply
        samply record ./target/release/hot
        # 按提示在浏览器打开链接查看火焰图
        ```

      - **perf + flamegraph**（Linux）：

        ```bash
        cargo install flamegraph
        cargo flamegraph --bin hot --release
        # 生成 flamegraph.svg，用浏览器打开
        ```

3. **需要观察的现象**：
   - 火焰图里最宽的「平台」应对应 `is_prime`，它占据了绝大部分 self 时间。
   - 因为开了 `line-tables-only`，把鼠标悬停应能看到**源码行**级别的归因（热点落在 `while`/`n % i` 那一行），而不是笼统的「某函数」。
   - 因为开了帧指针，调用栈 `main → is_prime` 应清晰可见、未被截断。
4. **预期结果**：`is_prime` 是最热函数；`main` 的 `inclusive` 与它接近、但 `self` 很低。
5. 待本地验证：剖析工具的安装与输出依赖本地环境（samply/flamegraph 是否可用、是否需配 perf 权限等）。若 `samply`/`flamegraph` 暂不可用，可改用 `valgrind --tool=callgrind ./target/release/hot`（Linux）再加 `rustfilt` 查看，同样能确认 `is_prime` 是热点。

> 进阶尝试：把第 3 步的 `debug = "line-tables-only"` 临时去掉、重新剖析，对比火焰图是否还能归因到源码行——你会直观看到「调试信息缺失」带来的归因退化。**注意改完记得改回来或单独留一个剖析专用构建，不要污染发布构建。**

#### 4.3.5 小练习与答案

**练习 1**：为什么 `debug = "line-tables-only"` 比 `debug = true` 更适合「只为剖析」的场景？

> **答案**：`line-tables-only` 只生成**源码行号表**（行级信息），足以让剖析器把样本归因到具体源码行，又不引入完整调试信息（如变量类型等）带来的体积与编译开销。它兼顾了「可剖析」与「不显著拖慢构建/增大体积」，是剖析专用构建的理想档位。

**练习 2**：如果不加 `-C force-frame-pointers=yes`，剖析结果会受到什么具体影响？

> **答案**：编译器可能优化掉帧指针，导致剖析器在采样时难以可靠地展开调用栈（stack trace）。表现为火焰图里调用栈**断裂或归并错误**、self/inclusive 时间分布失真，热点定位变模糊甚至误导。强制帧指针能保证栈回溯完整。

**练习 3**：`build-configuration.md` 警告 `strip = "symbols"` 会损害剖析。那么在生产发布时，该如何兼顾「要瘦身」和「要能剖析线上问题」？

> **答案**：常见做法是**分离两套构建**——发布构建照常 strip 以瘦身；同时保留一份带调试信息与符号的「剖析专用」构建（或单独保存 debug info / 符号文件），在需要剖析（含事后剖析线上采集到的 profile）时使用后者。不要把 strip 后的二进制直接拿去剖析。

---

### 4.4 符号 demangling

#### 4.4.1 概念说明

即便开启了调试信息和帧指针，你还可能遇到一个「表观」问题：剖析输出里的函数名是一串乱码，例如以 `_ZN` 或 `_R` 开头的字符串。这是因为 **Rust（像 C++ 一样）会对函数名做「mangling（修饰/编码）」**，把泛型参数、模块路径、闭包等信息编码进符号名，以保证最终符号的唯一性。

perf-book 给出的典型乱码例子（直接来自源码）包括：

- `_ZN3foo3barE` —— legacy（传统）格式，`_ZN` 前缀来自 Itanium C++ ABI 风格。
- `_ZN28_$u7b$$u7b$closure$u7d$$u7d$E` —— 同样是 legacy 格式，这里还把 `{{closure}}` 等特殊字符也编码了。
- `_RMCsno73SFvQKx_1cINtB0_3StrKRe616263_E` —— **v0 格式**，以 `_R` 开头，是 Rust 较新的、信息更丰富的 mangling 格式。

这些乱码本身不影响程序运行，但会**严重影响剖析的可读性**——你看不懂哪个函数最热。解决办法有两条路：**事后用工具还原**，或**换一种更友好的 mangling 格式**。

#### 4.4.2 核心流程

两条互补的路线：

1. **事后 demangle（不改构建）**：把剖析输出里的乱码符号喂给 `rustfilt`，它会还原成可读的 Rust 名字。适合「已经跑完剖析、不想重新构建」的场景。
2. **改 mangling 格式（重新构建）**：把默认的 legacy 格式切换到 **v0 格式**，很多工具（含部分剖析器）能直接识别 v0 并自动 demangle，输出更友好。重新构建：

   ```bash
   RUSTFLAGS="-C symbol-mangling-version=v0" cargo build --release
   ```

   或持久化进 `config.toml`：

   ```toml
   [build]
   rustflags = ["-C", "symbol-mangling-version=v0"]
   ```

注意：mangling 格式只影响**符号的可读性/可被工具识别的程度**，不改变程序行为，也不解决「缺少源码行信息」的问题——后者要靠 4.3 的调试信息。所以通常与 4.3 一起配置。

#### 4.4.3 源码精读

[src/profiling.md:115-119](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/profiling.md#L115-L119) —— Rust 用一种 mangling 把函数名编码进编译产物；若剖析器不知道这一点，输出里就会出现以 `_ZN` 或 `_R` 开头的符号，例如 `_ZN3foo3barE`、`_ZN28_$u7b$$u7b$closure$u7d$$u7d$E` 或 `_RMCsno73SFvQKx_1cINtB0_3StrKRe616263_E`。

[src/profiling.md:121-123](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/profiling.md#L121-L123) —— 这类名字可以用 **`rustfilt`** 手动 demangle（还原）。

[src/profiling.md:125-127](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/profiling.md#L125-L127) —— 若剖析时 demangle 有困难，可考虑把 mangling 格式从默认的 legacy 换成更新的 **v0 格式**。

[src/profiling.md:131-135](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/profiling.md#L131-L135) —— 命令行用 `RUSTFLAGS="-C symbol-mangling-version=v0" cargo build --release` 即可启用 v0 格式。

[src/profiling.md:137-142](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/profiling.md#L137-L142) —— 也可在 `config.toml` 的 `[build]` 下写 `rustflags = ["-C", "symbol-mangling-version=v0"]` 来对若干项目统一启用 v0。

#### 4.4.4 代码实践

1. **实践目标**：体会 legacy 与 v0 两种 mangling 格式在符号可读性上的差异，并学会用 `rustfilt` 还原。
2. **操作步骤**：
   1. 准备一个含闭包/泛型的示例函数（在 4.3 的 `hot` 项目里即可）：

      ```rust
      // 示例代码
      fn make_adder(base: u64) -> impl Fn(u64) -> u64 {
          move |x| base + x   // 闭包会触发 mangling
      }
      ```

   2. 用默认（legacy）格式构建，再用 `nm`/`objdump` 看符号（任选其一）：

      ```bash
      cargo build --release
      nm target/release/hot | grep -i closure | head
      ```

      预计会看到形如 `_ZN...closure...E` 的 legacy 乱码。

   3. 用 `rustfilt` 还原：

      ```bash
      cargo install rustfilt
      nm target/release/hot | grep -i closure | rustfilt | head
      ```

   4. 改用 v0 格式重新构建并对比：

      ```bash
      RUSTFLAGS="-C symbol-mangling-version=v0" cargo build --release
      nm target/release/hot | grep -i ' R' | head
      ```

      预计符号以 `_R` 开头。
3. **需要观察的现象**：legacy 符号以 `_ZN` 开头且特殊字符被编码（如 `$u7b$$u7d$` 代表 `{}`）；v0 符号以 `_R` 开头；`rustfilt` 能把二者都还原成可读的 Rust 路径。
4. **预期结果**：经 `rustfilt` 处理后，能看到类似 `hot::make_adder::{{closure}}` 这样可读的名字。
5. 待本地验证：`nm`/`objdump` 在不同平台可用性不同（macOS 用 `nm` 即可，Windows 需另行处理）；若工具不可用，至少能在 4.3 的剖析输出中直接观察到两类前缀。

#### 4.4.5 小练习与答案

**练习 1**：符号 `_ZN3foo3barE` 是 legacy 还是 v0 格式？依据是什么？`_RMCsno73SFvQKx_...` 又是哪种？

> **答案**：`_ZN3foo3barE` 是 **legacy** 格式（`_ZN` 前缀，Itanium ABI 风格）；`_RMCsno73SFvQKx_...` 是 **v0** 格式（`_R` 前缀）。判据就是首字母前缀：`_ZN` → legacy，`_R` → v0。

**练习 2**：切换到 v0 mangling 后，是否就能让剖析「自动」看到源码行归因？

> **答案**：不能。v0 解决的是**符号可读性/可被工具识别**的问题，与「源码行归因」无关。后者要靠 4.3 的 `debug = "line-tables-only"` 等调试信息。完整可读的剖析通常需要同时配置调试信息 + 帧指针 + 友好的 mangling（必要时再 `rustfilt`）。

**练习 3**：`rustfilt` 和切换 v0 格式，二者分别在什么场景更合适？

> **答案**：`rustfilt` 适合**事后还原**——剖析已跑完、不想重新构建，直接管道处理输出即可。切换 v0 适合**从一开始就让工具友好**——重新构建一次，之后很多剖析器能自动识别 v0 符号，省去手动 demangle。两者互补：能用 v0 就用 v0，遇到历史产物或工具不识别时再用 `rustfilt` 兜底。

## 5. 综合实践

把本讲四个模块串起来，完成一次「端到端」的剖析驱动定位：

1. **准备**：沿用 4.3 的 `hot` 示例项目（含 `is_prime` 与 4.4 的 `make_adder` 闭包）。
2. **配置**：在 `Cargo.toml` 设 `debug = "line-tables-only"`，并用一条 `RUSTFLAGS` 同时开启帧指针与 v0 mangling：

   ```bash
   RUSTFLAGS="-C force-frame-pointers=yes -C symbol-mangling-version=v0" \
     cargo build --release
   ```

3. **剖析**：用 samply（或 perf+flamegraph）剖析 `./target/release/hot`。
4. **验证四件事**（对应四个模块）：
   - 热点定位：火焰图最宽的平台是否落在 `is_prime`（对应 4.1「找热点」）。
   - 工具理解：说明你为什么选 samply 而非 DHAT（对应 4.2「选对工具」）。
   - 行归因 + 栈完整：热点能否归因到 `is_prime` 内的具体源码行，`main → is_prime` 调用栈是否完整（对应 4.3「调试信息 + 帧指针」）。
   - 符号可读：函数名是否可读、无需再手动 demangle（对应 4.4「mangling」）。
5. **对照实验**：把 `debug` 去掉并去掉帧指针、改回 legacy mangling，重新剖析，记录「可读性/归因/栈」三个维度各退化多少。
6. **产出**：写一句话结论——「本次剖析定位到的头号热点是 `___`，依据是 `self` 占比约 `___`」（数值待本地验证）。

> 如果本地暂时没有剖析器，可降级为「源码阅读型」版本：只完成步骤 1–2 的构建配置，再用 `nm | rustfilt` 验证符号可读性，并口头推断 `is_prime` 为热点，待具备剖析环境后再补全步骤 3–6。

## 6. 本讲小结

- **剖析的职责**：在一大段程序里定位「热点」（执行频率高到足以影响运行时间的代码），回答「该改哪里」——这是 u2-l1 基准测试（回答「变快了吗」）的另一半。
- **按「测什么」选工具**：CPU 热点用 perf/samply/flamegraph；精确指令计数用 Cachegrind/Callgrind；堆分配与峰值内存用 DHAT/dhat-rs；业务频率用 `counts`；优化潜力用 Coz。
- **剖析质量取决于编译产物保留的信息**：release 默认缺调试信息、可能丢帧指针，必须手动开启 `debug = "line-tables-only"` 与 `-C force-frame-pointers=yes`，剖析才能归因到源码行、栈才完整。
- **strip 与可剖析性对立**：`build-configuration.md` 明确警告 `strip = "symbols"` 会损害剖析，故「发布构建」与「剖析构建」应分开。
- **符号 mangling**：剖析输出里以 `_ZN`（legacy）或 `_R`（v0）开头的乱码可用 `rustfilt` 还原，或用 `-C symbol-mangling-version=v0` 让工具直接识别。
- **标准库盲区**：随 Rust 发布的标准库不带调试信息，要详细剖析 std 需自建编译器与 std 或用 nightly `build-std`，二者都麻烦、属进阶。

## 7. 下一步学习建议

- **横向**：阅读 `src/build-configuration.md` 全章（u2-l3 会系统精读），理解「可剖析性」如何与运行速度、二进制体积等维度一起构成构建配置的权衡空间——尤其 `strip`、`codegen-units`、`lto` 与剖析/性能的关系。
- **纵向（代码优化）**：掌握了「先剖析找热点」后，下一步进入真正的代码级优化。本手册 u3（堆分配与类型大小）将从 DHAT/dhat-rs 指出的分配热点切入，建议结合本讲 4.2 的 DHAT 一起读 `src/heap-allocations.md` 与 `src/type-sizes.md`。
- **机器码校验**：当剖析把热点缩到极小的内层循环时，可进一步用 `src/machine-code.md`（对应 u5-l4）查看生成的汇编，确认优化是否如预期生效——这条路径需要本讲的边界检查与内联知识作为铺垫（见 `src/bounds-checks.md`、`src/inlining.md`）。
