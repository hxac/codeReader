# Vec 的增长、集合复用与分配回归

## 1. 本讲目标

上一讲 [u3-l1](u3-l1-heap-allocation-types.md) 已经回答了「堆分配从哪里来」：你知道 `Vec` 是「三字（length / capacity / pointer）」表示，元素总在堆上，反复 `push` 时按 0 → 4 → 8 → 16 → 32 … **准倍增长**，每次增长都要搬运并重分配。本讲顺着这条线往下走，回答一个更实用的问题：**既然 `Vec` 会增长、会重分配，我该怎么动手减少这些分配？**

更具体地说，学完后你应当能够：

1. 复述 `Vec` 准倍增长带来的**指数级权衡**（重分配频率 ↓ 指数下降、浪费容量 ↑ 指数上升），并能用 `eprintln!` + [`counts`] 这套组合先**测量 `Vec` 的长度分布**，再决定用哪种优化手段——而不是凭感觉改代码。
2. 针对已知大小的 `Vec`，用 `with_capacity` / `reserve` / `reserve_exact` **一次性预分配到位**，并理解 `shrink_to_fit` 何时能省内存、何时反而会触发重分配。
3. 针对大量「短 `Vec`」，判断该用 `SmallVec` 还是 `ArrayVec`，并清楚它们各自的**代价**（更慢的普通操作、可能更大的类型体积）。
4. 在「每个循环迭代都要一个 `Vec`」的热点里，用循环外复用的 **workhorse 集合 + `clear()`** 把多次分配压成一次；最后用 **dhat-rs 的堆用量回归测试**把优化成果**锁死**，防止日后悄悄倒退。

本讲依赖 [u3-l1](u3-l1-heap-allocation-types.md)（`Vec` 三字表示、准倍增长、DHAT/dhat-rs 定位分配热点）和 [u2-l2](u2-l2-profiling.md)（「先测量再优化」的工作流）。

[`counts`]: https://github.com/nnethercote/counts/

---

## 2. 前置知识

进入源码前，先用三段话把本讲的直觉立起来。

### 2.1 准倍增长是「好策略」，但它是一种权衡

`Vec` 的准倍增长让 `push` 的**摊还成本**为 \(O(1)\)（这一点 u3-l1 已证）。但 perf-book 在源码里点出了它的另一面——**这是一笔指数级的权衡**：

> As a vector grows, the frequency of reallocations will decrease **exponentially**, but the amount of possibly-wasted excess capacity will increase **exponentially**.

也就是说：向量越长，重分配越稀疏（好事），但**尾部浪费的多余容量**也越大（坏事，峰值内存被撑高）。理解这一权衡，是后续「预分配」「`shrink_to_fit`」「`SmallVec`」几种手段各自适用场景的分水岭。

### 2.2 「该用哪种优化」取决于长度分布，不取决于直觉

perf-book 反复强调一个工程态度：**在动手优化一个 `Vec` 分配热点之前，先搞清楚它的长度分布长什么样**。一个热点 `Vec::push` 处，可能：

- 大多是**很短**的 `Vec`（比如长度 0~3）→ 适合 `SmallVec` / `ArrayVec`，把元素放栈上；
- 是少数**很长**的 `Vec`，且长度可预估 → 适合 `with_capacity` 一次到位；
- 每个**循环迭代**都新建一个 `Vec` → 适合复用 workhorse 集合。

这三类情况的优化手段**完全不同**，所以「先测量」不是口号，而是选择正确手段的前提。测量工具就是上讲提过的 `eprintln!` 打印长度 + [`counts`](https://github.com/nnethercote/counts/) 做频次统计。

### 2.3 优化的最后一公里：防止「分配回归」

性能优化有一个隐形敌人：**回归（regression）**。你今天把某个热点的分配从 4 次压成 1 次，半年后别人改了相关代码，分配数悄悄涨回 4 次，却没人发现。perf-book 给出的对策是用 **dhat-rs 的堆用量测试（heap usage testing）** 写成自动化测试——把「这段代码应当只分配 N 次 / M 字节」变成一条断言，让 CI 替你盯着。本讲会把「优化」和「防回归」配成一对来讲。

---

## 3. 本讲源码地图

本讲只深入 perf-book 的一个章节，但把它读「厚」。

| 文件 | 作用 |
| --- | --- |
| [src/heap-allocations.md](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md) | 第 7 章。本讲全部内容都来自这一章的四个小节：`Vec` Growth（增长策略 + 长度分布测量）、Short `Vec`s（SmallVec/ArrayVec）、Longer `Vec`s（预分配）、Reusing Collections（workhorse 复用）以及结尾的 Avoiding Regressions（dhat-rs 回归测试）。 |

> 提示：perf-book 是一本用 mdBook 渲染的书，没有可运行 Rust 工程。本讲引用的代码片段都是书中**已存在**的示例；凡是我额外写的可运行示例，都会明确标注「示例代码」。涉及 dhat-rs 具体统计/测试 API 的地方，会标注「待本地验证」，请以本地安装版本的 crate 文档为准。

---

## 4. 核心概念与源码讲解

### 4.1 Vec 的容量与准倍增长——以及如何决定优化方向

#### 4.1.1 概念说明

u3-l1 已经讲过 `Vec` 的三字表示与准倍增长的基本机制。本模块不重复证明，而是补两件 u3-l1 没展开的事：

1. **准倍增长的指数级权衡**——为什么它「总体合理」却「局部可优化」。
2. **决策框架**——在优化一个 `Vec` 热点之前，先用 `eprintln!` + `counts` 摸清长度分布，再选手段。

#### 4.1.2 核心流程

把准倍增长画成一张「容量阶梯」，权衡就一目了然：

```
push 次数  0   1-4   5-8   9-16   17-32   33-64   ...
capacity   0    4     8     16      32       64     ...
重分配?    否   是    是     是      是       是      （每次翻倍触发一次搬运）
```

关键观察：

- **重分配次数**随 `push` 增多而**指数级稀疏**：从 4 到 8 之间隔 4 次 push，从 32 到 64 之间隔 32 次。这正是摊还 \(O(1)\) 的来源。
- **浪费容量**随向量变长而**指数级膨胀**：一个恰好长 33 的 `Vec`，capacity 被撑到 64，**浪费近一半**。在「内存占用」维度上这是实打实的代价。

由此推出「先测量再优化」的标准动作：

1. 找到一个**热点 `Vec::push`**（或任何反复触发的 `Vec` 分配站点）。
2. 在该站点用 `eprintln!("{:?}", vec.len())` 打印每次到达时的长度。
3. 把程序的 stderr 重定向到文件，用 [`counts`](https://github.com/nnethercote/counts/) 统计**长度频次分布**。
4. 按分布形态选手段：大量短 `Vec` → 4.3 的 `SmallVec`/`ArrayVec`；少数可预估长 `Vec` → 4.2 的 `with_capacity`；每轮迭代新建 → 4.4 的 workhorse 复用。

#### 4.1.3 源码精读

**① 准倍增长策略与「跳过 1、2」** —— u3-l1 引过这段，这里再看一次，重点读它的权衡表述：

[src/heap-allocations.md:L107-L119](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md#L107-L119)

> 这段说明：空 `Vec`（`vec![]` / `Vec::new` / `Vec::default`）length=capacity=0、不分配；反复 `push` 时容量序列为 **0, 4, 8, 16, 32, 64**。注意两个细节——(a) 从 0 **直接跳到 4**（而非经 1、2），是为了在实践中**避免大量小分配**；(b) 增长越深，**重分配越稀疏、但浪费容量越大**，二者都是指数级变化。这正是后续「预分配」「`shrink_to_fit`」要解决的问题。

**② 决策框架：用 `eprintln!` + `counts` 量长度分布** —— 这是 perf-book 给出的、决定「用哪种优化」的关键一招：

[src/heap-allocations.md:L125-L136](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md#L125-L136)

> 这段说明：准倍增长「总体合理」，但**若你提前知道 `Vec` 的大致长度，常常能做得更好**。具体做法——对一个热的 `Vec` 分配站点（如热点 `Vec::push`），用 `eprintln!` 打印向量长度，再用 [`counts`](https://github.com/nnethercote/counts/) 做后处理得到**长度分布**。分布可能是「大量短向量」或「少量超长向量」，**对应的最佳优化方式完全不同**。这就是本讲 4.2/4.3/4.4 三类手段的分流依据。

#### 4.1.4 代码实践

**实践目标**：亲手用 `eprintln!` + `counts` 量出一个热点 `Vec` 的长度分布，体会「先测量」。

**操作步骤**：

1. 写一个会反复 `push` 的小程序，在一个热点 `push` 处插入 `eprintln!("len={}", v.len());`（示例代码）：

```rust
// 示例代码：在热点处打印 Vec 长度
fn collect_tokens(input: &str) -> Vec<&str> {
    let mut v: Vec<&str> = vec![];
    for tok in input.split_whitespace() {
        v.push(tok);
        eprintln!("len={}", v.len()); // 仅用于测量，测完删除
    }
    v
}
```

2. 用一组真实输入跑程序，把 stderr 落盘：`./your_program 2> lens.txt`。
3. 用 [`counts`](https://github.com/nnethercote/counts/) 统计 `lens.txt` 里各长度出现次数（counts 是一个按行聚合计数的命令行工具，具体用法以它的 README 为准）。

**需要观察的现象**：长度分布是集中在很小的值（如大量 0/1/2），还是呈长尾（少数非常大的值）？

**预期结果**：你会得到一张「长度 → 出现次数」的表。**待本地验证**的是具体数字，但你能据此判断：若分布集中在短 `Vec`，本讲 4.3（SmallVec/ArrayVec）是正解；若集中在可预估的长 `Vec`，4.2（with_capacity）更合适。

#### 4.1.5 小练习与答案

**练习 1**：为什么 perf-book 说准倍增长「总体合理，却常可改进」？
**答案**：因为准倍增长对**长度未知**的通用情况是摊还 \(O(1)\) 的好策略；但它的代价是指数级的浪费容量。一旦你**提前知道**长度分布（短向量居多、或长度可预估），就能用 `SmallVec`/`with_capacity` 等手段针对性消除浪费或重分配，比通用策略更优。

**练习 2**：在优化一个 `Vec` 热点前，为什么必须先量长度分布，而不能直接套用 `SmallVec`？
**答案**：因为「大量短 `Vec`」适合 `SmallVec`（元素入栈），「少数可预估长 `Vec`」适合 `with_capacity`（一次到位），「每轮新建」适合 workhorse 复用。三种分布对应三种**不同**手段；不看分布就动手，很可能选错方向，甚至让 `SmallVec` 把本就长的向量变得更慢更大。

---

### 4.2 预分配：with_capacity / reserve / shrink_to_fit

#### 4.2.1 概念说明

当你**已经知道 `Vec` 的长度**（至少知道下界，或知道确切值），就没必要让它一步步准倍增长、白白触发好几次重分配。perf-book 给出的手段是**预分配**：用 `with_capacity` / `reserve` / `reserve_exact` 一次性把容量开够。与之配对的还有 `shrink_to_fit`，用来**压掉**多余的浪费容量。

#### 4.2.2 核心流程

三类预分配 API 的分工：

| API | 语义 | 典型场景 |
| --- | --- | --- |
| `Vec::with_capacity(n)` | **新建**一个容量 ≥ n 的空 `Vec` | 一开始就知道要放多少 |
| `Vec::reserve(additional)` | 保证还能再 `push` `additional` 个而不重分配 | 边走边补，知道**还要**追加多少 |
| `Vec::reserve_exact(additional)` | 同上，但尽量**不多给**（实现仍可能多给） | 想严格压住浪费 |
| `Vec::shrink_to_fit()` | 把容量缩到尽量贴近 length | 已知不会再增长，想省内存（**可能触发一次重分配**） |

量化收益——书里给的算账（也是本模块最该记住的数字）：

- 若知道至少要 20 个元素：`with_capacity(20)` **一次分配**到位；
- 否则逐个 `push`：容量要经过 **4 → 8 → 16 → 32** 共 **4 次**重分配才够 20 个。

即「知道大小」时，预分配能把多次重分配压成**一次**。

注意可迁移性：`String`（≈ `Vec<u8>`）有 `String::with_capacity`；`HashMap`/`HashSet` 有 `with_capacity`——它们与 `Vec` 同构，同一套容量心智模型直接套用（u3-l1 已建立这点）。

#### 4.2.3 源码精读

**① Longer `Vec`s：预分配把多次重分配压成一次** ——

[src/heap-allocations.md:L166-L179](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md#L166-L179)

> 这段说明：若知道 `Vec` 的最小或确切大小，用 `Vec::with_capacity` / `Vec::reserve` / `Vec::reserve_exact` 预留容量。书里的算账很直观：知道至少 20 个元素时，这些函数能**一次分配**给出容量 ≥ 20 的 `Vec`；而逐个 `push` 则要经历 **4 次**重分配（容量 4、8、16、32）。

**② 知道上界就别多分；`shrink_to_fit` 压浪费** ——

[src/heap-allocations.md:L181-L185](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md#L181-L185)

> 这段说明：若知道 `Vec` 的**最大**长度，上面三个函数还能避免多分多余空间。`Vec::shrink_to_fit` 则可把浪费压到最小——**但它本身可能触发一次重分配**。所以 `shrink_to_fit` 不是「免费省内存」，而是一次「换一次重分配来换更小占用」的取舍，适合「知道以后不会再长」的场景。

#### 4.2.4 代码实践

**实践目标**：用一次 `with_capacity` 替代逐个 `push` 的多次重分配，并验证容量序列差异。

**操作步骤**（在一个最小 Cargo 工程里）：

1. 写两个版本，都把 20 个元素放进 `Vec`（示例代码）：

```rust
// 示例代码：逐个 push——经历 4 次重分配（cap 4,8,16,32）
fn grow_push(n: usize) -> Vec<u32> {
    let mut v: Vec<u32> = Vec::new();
    for i in 0..n as u32 {
        v.push(i);
    }
    v
}

// 示例代码：预分配——1 次分配到位
fn grow_with_capacity(n: usize) -> Vec<u32> {
    let mut v: Vec<u32> = Vec::with_capacity(n);
    for i in 0..n as u32 {
        v.push(i);
    }
    v
}
```

2. 用 dhat-rs 把它设为全局分配器，分别测量两个函数在 `n = 20` 时的**累计分配块数**（dhat-rs 的 region/stats API 随版本变化，精确写法见其 crate 文档，**待本地验证**）。

**需要观察的现象**：`grow_push` 的分配块数是否约为 4（容量 4/8/16/32 各一次）？`grow_with_capacity` 是否约为 1？

**预期结果**：预分配版本的堆分配次数从约 4 降到约 1。**待本地验证**的是具体块数（标准库实现细节、n 的取值都会影响），但「多次 → 一次」这一方向性结论是确定的，来自准倍增长的容量序列。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `shrink_to_fit` 不能算「免费的内存优化」？
**答案**：因为它**可能触发一次重分配**——要把元素从较大的堆块搬到更贴合 length 的新块。它适合「已知不会再增长」的场景，用一次重分配换取之后更小的常驻内存；在还会继续增长的 `Vec` 上调用反而是浪费。

**练习 2**：`Vec::reserve(20)` 和 `Vec::with_capacity(20)` 的区别是什么？
**答案**：`with_capacity(20)` 是**新建**一个容量 ≥ 20 的空 `Vec`（起点）；`reserve(20)` 是在**已有** `Vec` 上保证「还能再追加 20 个而不重分配」，即把容量补到 ≥ `len + 20`。前者用于构造时，后者用于运行中补容。

**练习 3**：把 `Vec` 换成 `HashMap`，预分配的思路还能用吗？
**答案**：能。u3-l1 已说明 `HashMap`/`HashSet` 与 `Vec` 同构（单一连续堆分配，随增长重分配），它们都有 `with_capacity` 等容量相关方法，同一套「知道大小就预分配」的心智模型直接迁移。

---

### 4.3 短 Vec：SmallVec 与 ArrayVec

#### 4.3.1 概念说明

当 4.1 的长度分布测量显示「**大量短 `Vec`**」（典型如长度 0~3 占多数），准倍增长再怎么调都不理想——因为这些 `Vec` 根本不该上堆。perf-book 给出的解法是把元素**直接放进 `Vec` 自身**（即栈上/结构体内联），只有溢出时才退回堆分配。这就是 `smallvec` crate 的 `SmallVec`；若你**确切知道最大长度**，还有更轻的 `arrayvec` crate 的 `ArrayVec`。

#### 4.3.2 核心流程

两种「内联短 `Vec`」类型对比：

| 类型 | 来自 | 表示 | 是否会回退堆分配？ | 关键代价 |
| --- | --- | --- | --- | --- |
| `SmallVec<[T; N]>` | `smallvec` | 内联 N 个元素，超出则退回堆 | **会**（超出 N 时） | 每次操作都要判断「内联还是堆」，普通操作略慢于 `Vec`；N 大或 T 大时类型比 `Vec<T>` 还大 |
| `ArrayVec<T, N>` | `arrayvec` | 固定容量 N，纯内联 | **不会**（无堆回退） | 不能超过 N；因无需堆回退判断，比 `SmallVec` 略快 |

选型规则（perf-book 的两条原文要点）：

- **大量短 `Vec`** → `SmallVec<[T; N]>`：能内联 N 个，超出才上堆。注意 `vec![]` 字面量要换成 `smallvec![]`。
- **大量短 `Vec` 且确切知道最大长度** → `ArrayVec` 更好：无需堆回退，**略快**。

**铁律**：`SmallVec` **可靠地降低分配率**，但**不保证更快**——它的普通操作本来就比 `Vec` 慢一点（要多一次内联/堆判断），而且若 N 选得太大或 T 很大，`SmallVec<[T; N]>` 自身体积会超过 `Vec<T>`，复制更慢。**必须 benchmark 才能确认收益**（呼应 u2-l1 的基准测试）。

#### 4.3.3 源码精读

**① SmallVec：内联 N 个，超出退回堆** —— 注意末句「`vec![]` 要换成 `smallvec![]`」这个易踩的坑：

[src/heap-allocations.md:L138-L148](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md#L138-L148)

> 这段说明：面对大量短 `Vec`，可用 `smallvec` crate 的 `SmallVec<[T; N]>` 作为 `Vec` 的「drop-in」替代——它能把 **N 个元素存在 `SmallVec` 自身**，超出 N 才切换到堆分配。附带提醒：`vec![]` 字面量必须相应替换为 `smallvec![]`。

**② SmallVec 的代价：更慢、可能更大** —— 这是决定「值不值得用」的关键：

[src/heap-allocations.md:L150-L156](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md#L150-L156)

> 这段说明：`SmallVec` 用对地方时**可靠地降低分配率**，但**不保证提升性能**——它的普通操作比 `Vec` **略慢**，因为每次都要检查「元素在堆上还是内联」；而且若 N 大或 T 大，`SmallVec<[T; N]>` 自身可能比 `Vec<T>` **更大**，复制更慢。结论仍是那句老话：**as always, benchmarking is required**。

**③ ArrayVec：确切知道最大长度时的更优选择** ——

[src/heap-allocations.md:L158-L164](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md#L158-L164)

> 这段说明：如果不仅短、还**确切知道最大长度**，`arrayvec` crate 的 `ArrayVec` 比 `SmallVec` 更好——它**不需要堆回退**，因此**略快**。适用前提是「最大长度已知且不会越界」。

#### 4.3.4 代码实践

**实践目标**：把一个「每个元素都很短」的 `Vec<Vec<u32>>` 改成 `Vec<SmallVec<[u32; 4]>>`，观察分配率下降。

**操作步骤**：

1. 在 `Cargo.toml` 加入 `smallvec`（示例代码）：

```toml
# 示例代码：Cargo.toml
[dependencies]
smallvec = "1"
```

2. 写两个版本，模拟「大量长度 ≤ 4 的内层集合」（示例代码）：

```rust
// 示例代码：基线——每个内层 Vec 都至少一次堆分配
fn baseline(groups: &[[u32; 4]]) -> Vec<Vec<u32>> {
    groups.iter().map(|g| g.to_vec()).collect()
}

// 示例代码：SmallVec——长度 ≤ 4 时内层零堆分配
use smallvec::SmallVec;
fn with_smallvec(groups: &[[u32; 4]]) -> Vec<SmallVec<[u32; 4]>> {
    groups.iter().map(|g| SmallVec::from(g.as_slice())).collect()
}
```

3. 用 dhat-rs 统计两个函数各自的内层分配块数（精确 API **待本地验证**）。

**需要观察的现象**：基线版每个内层集合都触发至少 1 次堆分配（共 ≈ N 次）；SmallVec 版因元素数 ≤ 4 全部内联，内层分配 ≈ 0 次。

**预期结果**：内层分配率显著下降。但 perf-book 同时提醒——别忘了测**总耗时**：若这些 `SmallVec` 后续被频繁复制，或 N 选得过大，可能因类型变大而**抵消**省分配的收益。所以这一步必须连同「运行时间」一起 benchmark，**待本地验证**净收益方向。

#### 4.3.5 小练习与答案

**练习 1**：`SmallVec` 既然能降分配率，为什么 perf-book 还说「不保证更快」？
**答案**：因为它的每个普通操作都要**多判断一次**「当前元素是内联还是在堆上」，这本身有开销；而且当 N 大或 T 大时，`SmallVec<[T; N]>` 自身体积可能超过 `Vec<T>`，导致复制更慢、缓存更差。省下来的分配开销是否盖过这些新增开销，只有 benchmark 能回答。

**练习 2**：在什么前提下 `ArrayVec` 优于 `SmallVec`？
**答案**：当**确切知道最大长度**（且不会越界）时。`ArrayVec` 没有「超出 N 就上堆」的回退路径，省掉了 `SmallVec` 每次操作都要做的内联/堆判断，因此**略快**；代价是它**不能**超过固定容量 N。

**练习 3**：把 `Vec` 换成 `SmallVec` 时，`vec![1, 2, 3]` 这样的字面量要怎么改？
**答案**：要相应换成 `smallvec![1, 2, 3]`（来自 `smallvec` crate）。perf-book 特意提醒了这一点——「drop-in 替代」指的是类型层面的替换，宏字面量仍需手动改。

---

### 4.4 复用 workhorse 集合与分配回归测试

#### 4.4.1 概念说明

前三个模块都在「单个 `Vec` 的一生」里做文章。本模块换一个视角：**当一个 `Vec` 在循环里被反复「建好—用掉—丢掉」**，与其每轮新建一个（每轮都从 capacity 0 开始准倍增长），不如**把同一个 `Vec` 留到循环外面反复复用**——perf-book 称之为 **workhorse collection**（干活用的主力集合）。配合 `clear()`（清空内容但**不动 capacity**），可以把「每轮若干次分配」压成「全程一次分配」。本模块最后，用 **dhat-rs 的堆用量测试** 把所有优化成果锁死成回归测试。

#### 4.4.2 核心流程

perf-book 在「Reusing Collections」一节给出两条递进的复用模式：

**模式 A：把结果 `Vec` 改成「传入并修改」** —— 与其返回新 `Vec`，不如接收一个 `&mut Vec` 往里填：

```
// 原版：每次调用都新建并返回一个 Vec
fn do_stuff(x, y) -> Vec { vec![x, y] }

// 复用版：往调用者提供的 Vec 里追加
fn do_stuff(x, y, vec: &mut Vec) { vec.push(x); vec.push(y) }
```

**模式 B：循环外的 workhorse 集合 + `clear()`** —— 当循环每轮都需要一个 `Vec` 时：

```
let mut workhorse: Vec<T> = Vec::new();   // 循环外声明一次
for _ in 0..iters {
    // ... 用 workhorse 做本轮工作（push 等）...
    workhorse.clear();                     // 清空内容，但保留 capacity
}
```

`clear()` 的关键性质：**清空元素但不释放底层容量**。所以下一轮 `push` 能直接复用上一轮攒下的 capacity，不再触发准倍增长的重分配。代价（perf-book 明说的）是：它**掩盖了「每轮使用互不相关」这一事实**，可读性略降——属于用清晰度换性能的取舍。

**变体：workhorse `String` + `read_line`** —— 逐行读文件时，`BufRead::lines()` 返回的迭代器**每行都产出一个新 `String`**（每行一次分配）。改用 workhorse `String` 配 `BufRead::read_line`，每轮 `clear()`，可把分配压到「至多 handful，甚至 1 次」。

**防回归：dhat-rs 堆用量测试** —— 优化做完后，用 dhat-rs 的 *heap usage testing* 特性写一条断言：某段代码应当只分配预期的字节数/块数。日后若有人引入了多余分配，CI 会直接失败。

#### 4.4.3 源码精读

**① Reusing Collections：传入并修改优于建好再合并** —— 含两段对照示例：

[src/heap-allocations.md:L340-L359](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md#L340-L359)

> 这段说明：分阶段构建集合时，**修改单个 `Vec`** 通常优于「建多个 `Vec` 再合并」。书中对照：原版 `do_stuff` 每次返回新 `Vec`；改写版接收 `&mut Vec<u32>` 往里 `push`。后者让调用方可以复用同一个底层分配，而非每次都新建。

**② 循环外的 workhorse + `clear()`** —— 本模块最核心的技巧：

[src/heap-allocations.md:L360-L369](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md#L360-L369)

> 这段说明：当循环每轮都需要一个 `Vec`，可把 `Vec` **声明在循环外**，循环体内使用，末尾调 [`clear`](https://doc.rust-lang.org/std/vec/struct.Vec.html#method.clear) 清空（**清空但不影响 capacity**）。这样避免了每轮重新分配，代价是**掩盖了「每轮使用互不相干」**这一事实。同样的 workhorse 集合也可放在结构体里，供反复调用的方法复用。

**③ 逐行读文件：workhorse `String` 替代 `lines()`** —— 这是 workhorse 模式在 I/O 上的应用，也是下一讲 [u5-l2（I/O）](u5-l2-io.md) 的伏笔：

[src/heap-allocations.md:L374-L390](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md#L374-L390)

> 这段说明：`BufRead::lines()` 好用，但它产出的迭代器返回 `io::Result<String>`，意味着**文件的每一行都分配一次**。

[src/heap-allocations.md:L393-L415](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md#L393-L415)

> 这段说明：替代方案是用一个 workhorse `String` 配 `BufRead::read_line` 在循环里反复读、每轮 `line.clear()`。这样**分配次数降到至多 handful、甚至 1 次**（具体取决于行长分布触发几次重分配）。前提是循环体能接受 `&str` 而非 `String`。

**④ Avoiding Regressions：用 dhat-rs 写堆用量测试** —— 把优化成果锁死：

[src/heap-allocations.md:L427-L434](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md#L427-L434)

> 这段说明：为确保代码的分配次数/大小**不会无意中增加**，可用 [dhat-rs](https://crates.io/crates/dhat) 的 *heap usage testing* 特性，编写测试来**检查特定代码片段是否分配了预期数量的堆内存**。这正是把本讲所有优化（预分配、SmallVec、workhorse 复用）的成果固化为「不会回退」的自动化护栏。

#### 4.4.4 代码实践

**实践目标**：把「每轮新建 `Vec`」的循环改写成 workhorse 复用，并用 dhat-rs 写一条防回归断言。

**操作步骤**：

1. 写一个基线版：外层循环每轮都新建一个 `Vec` 并填若干元素（示例代码）：

```rust
// 示例代码：基线——每轮新建 Vec，每轮若干次重分配
fn baseline(iters: usize) -> u64 {
    let mut acc: u64 = 0;
    for _ in 0..iters {
        let mut v: Vec<u32> = Vec::new(); // 每轮新建
        for j in 0..20 {
            v.push(j as u32);
        }
        acc += v.len() as u64;
    }
    acc
}
```

2. 改写成 workhorse 复用版：把 `Vec` 提到循环外，每轮 `clear()`（示例代码）：

```rust
// 示例代码：workhorse——全程只分配一次（外加增长）
fn workhorse(iters: usize) -> u64 {
    let mut acc: u64 = 0;
    let mut v: Vec<u32> = Vec::new(); // 循环外声明一次
    for _ in 0..iters {
        v.clear(); // 清空但保留 capacity
        for j in 0..20 {
            v.push(j as u32); // 第二轮起复用上一轮攒下的 capacity
        }
        acc += v.len() as u64;
    }
    acc
}
```

3. 用 dhat-rs 统计两个版本在 `iters` 很大时的累计分配块数（示例代码——dhat-rs 的测试/统计 API 随版本变化，以下为结构示意，精确写法**待本地验证**，请以 crate 文档为准）：

```rust
// 示例代码：把 dhat-rs 设为全局分配器以采集堆统计
#[global_allocator]
static ALLOC: dhat::Dhat = dhat::Dhat::new_heap();

// heap usage testing 的确切断言宏/Stats 结构以本地版本文档为准（待本地验证）：
// 典型用法是测量某段代码的「总分配字节/块数」并与期望值断言相等。
```

**需要观察的现象**：基线版的分配块数是否随 `iters` **线性增长**（每轮新建）？workhorse 版是否**几乎与 `iters` 无关**（全程一次分配 + 极少的增长重分配）？

**预期结果**：workhorse 版的分配次数从 `O(iters)` 降到近似 `O(1)`。dhat-rs 具体统计 API 与断言写法**待本地验证**，但「每轮新建 → 全程复用」带来的分配次数量级下降是确定的，来自 `clear()` 不释放 capacity 的语义。

#### 4.4.5 小练习与答案

**练习 1**：`Vec::clear()` 和 `Vec::new()`（重新赋一个空 `Vec`）在分配上的区别是什么？
**答案**：`clear()` **清空元素但不释放底层 capacity**，下一轮 `push` 直接复用现有缓冲，不触发准倍增长；而重新 `Vec::new()` 会丢掉旧缓冲、从 capacity 0 开始，下一轮又要经历完整的准倍增长重分配。这正是 workhorse 复用模式成立的关键。

**练习 2**：perf-book 指出 workhorse 复用有一个「代价」，是什么？
**答案**：它**掩盖了「每一轮对该 `Vec` 的使用彼此互不相干」这一事实**——读代码的人会误以为各轮之间有数据依赖。这是用一定的**可读性/清晰度**换取分配性能的取舍，应当用在经剖析证实的真正热点上，而非到处乱用。

**练习 3**：为什么 `BufRead::lines()` 会「每行分配一次」？workhorse `read_line` 方案又如何省下来？
**答案**：`lines()` 产出的迭代器项类型是 `io::Result<String>`，即每行都构造一个**拥有所有权**的新 `String`，自然每行一次分配。改用 workhorse `String` 配 `read_line(&mut line)`，每轮把内容读进同一个 `String`、用完 `clear()`，于是底层缓冲被反复复用，分配次数取决于行长分布触发的重分配次数（至多 handful、甚至 1 次）。

---

## 5. 综合实践

把本讲四个模块串成一个端到端任务：**为一个热点循环做一次完整的「测量 → 选型 → 优化 → 防回归」流程**。

**任务背景**：你有一段处理输入批次的代码，外层循环跑上万次，每次都构建一个临时 `Vec<u32>` 来存放当前批次的中间结果。你怀疑这里的分配是热点。

**要求**：

1. **测量（4.1）**：在该 `Vec` 的 `push` 处插 `eprintln!("len={}", v.len())`，用 [`counts`](https://github.com/nnethercote/counts/) 统计长度分布。判断它属于「大量短 `Vec`」「可预估长 `Vec`」还是「每轮新建」中的哪一类。
2. **选型与优化**：根据分布形态，从本讲三套手段里选**正确**的一种（或组合）：
   - 若每轮新建、长度差异大 → **workhorse 复用 + `clear()`**（4.4）；
   - 若长度可预估 → 叠加 `with_capacity` 预分配（4.2）；
   - 若大多很短 → 考虑 `SmallVec`/`ArrayVec`（4.3，并务必 benchmark 确认净收益）。
3. **量化**：用 dhat-rs 分别记录优化前后的**累计分配块数**，确认量级下降（如 `O(iters)` → `O(1)`）。
4. **防回归（4.4）**：用 dhat-rs 的 *heap usage testing* 为这段代码写一条断言——分配块数不得超过优化后的值。日后若有人引入多余分配，CI 立即失败。

**思考题**：如果你选了 workhorse 复用，但后续有人在循环体里对 `v` 调用了 `shrink_to_fit()`（4.2），会对你的优化带来什么反效果？（提示：回想 `shrink_to_fit` 与 `clear` 对 capacity 的相反作用。）

> 说明：本实践需要本地有一个可运行 dhat-rs 的 Rust 工程与（可选的）[`counts`](https://github.com/nnethercote/counts/) 工具。具体分配数值与 dhat-rs 测试 API **待本地验证**，但「测量 → 选型 → 优化 → 防回归」这条流程、以及三套手段各自的适用条件，是本讲确定性的结论。

---

## 6. 本讲小结

- 准倍增长是**指数级权衡**：重分配频率随 `push` 增多**指数下降**（摊还 \(O(1)\) 的来源），但浪费容量**指数膨胀**——所以「知道长度」时总能做得更好。
- **先测量再选型**：优化一个 `Vec` 热点前，用 `eprintln!` 打印长度 + [`counts`](https://github.com/nnethercote/counts/) 统计分布；「大量短 `Vec`」「可预估长 `Vec`」「每轮新建」分别对应三种**不同**的优化手段。
- **可预估长 `Vec`** → `Vec::with_capacity` / `reserve` / `reserve_exact` 一次到位（书里：20 个元素从 4 次重分配压成 1 次）；`shrink_to_fit` 能压浪费，但**可能触发一次重分配**。`String`/`HashMap` 同构，同套思路。
- **大量短 `Vec`** → `SmallVec<[T; N]>`（内联 N 个、超出退回堆，`vec![]` 要换 `smallvec![]`）；**确切知道最大长度**时用 `ArrayVec` 更快（无堆回退）。两者都**不保证更快**，必须 benchmark。
- **每轮新建的循环** → 循环外声明 workhorse 集合、每轮 `clear()`（**清空但不释放 capacity**），把 `O(iters)` 分配压成近似 `O(1)`；逐行读文件同理用 workhorse `String` + `read_line`。代价是**掩盖了各轮使用互不相干**这一事实。
- **防回归**：用 dhat-rs 的 *heap usage testing* 写堆用量断言，把优化成果锁死成 CI 护栏，防止分配数日后悄悄倒退。

---

## 7. 下一步学习建议

1. **横向：从「分配次数」转向「类型体积」** —— 进入 [u3-l3（Type Sizes）](u3-l3-type-sizes.md)，学用 `-Zprint-type-sizes` 测量布局、用「装箱大变体 / 更小整数 / boxed slice / ThinVec」缩小频繁实例化的类型。本讲提过「`SmallVec<[T; N]>` 选太大会比 `Vec` 还大」——type-sizes 那一章正是系统讲解如何量化和控制这种体积代价。
2. **纵向：把 workhorse 思路用到 I/O** —— 本讲 4.4 已经埋了伏笔：`BufRead::lines()` 每行一次分配、workhorse `String` 能压到几乎零分配。下一单元的 [u5-l2（I/O 锁与缓冲）](u5-l2-io.md) 会系统讲解 stdout/stdin 的锁与缓冲，是 workhorse 模式在 I/O 上的完整展开。
3. **回看基础** —— 若对 `clear()` 保留 capacity、`reserve` vs `with_capacity`、`SmallVec` 内联判定等点还有疑问，建议结合标准库 [`Vec`](https://doc.rust-lang.org/std/vec/struct.Vec.html) 文档的 capacity 一节，以及 [`smallvec`](https://docs.rs/smallvec) / [`arrayvec`](https://docs.rs/arrayvec) 的文档对照阅读，把本讲的结论在类型层面验证一遍。
