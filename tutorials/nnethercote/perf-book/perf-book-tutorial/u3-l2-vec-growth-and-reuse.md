# Vec 的增长、集合复用与分配回归

## 1. 本讲目标

上一讲（u3-l1）我们已经回答了「Rust 程序的堆分配从哪里来」，并建立了「分配率（allocs/Minstr）是独立于 CPU 热点的性能维度」这一认知。本讲把镜头推进到最常见、也最值得优化的分配大户——`Vec`——回答下面三个问题：

1. `Vec` 在反复 `push` 时是按什么规律扩容的？为什么「准倍增长」能让单次 `push` 摊还到 \(O(1)\)，却又可能浪费空间？
2. 当我们已经**预先知道** `Vec` 的大致规模时，有哪些手段可以少分配甚至不分配？`with_capacity`/`reserve`、`SmallVec`/`ArrayVec` 各自适合什么场景？
3. 在无法预知规模、且 `Vec` 在循环里反复创建销毁时，如何用一个「workhorse（主力马）」集合反复复用来消除分配？又如何用 `dhat-rs` 写**回归测试**，保证未来改动不会让分配悄悄变多？

学完后你应该能：读懂 `Vec` 的容量序列、用 `eprintln!` + `counts` 量出长度分布、按分布选择 `with_capacity` 或 `SmallVec`/`ArrayVec`、用 workhorse 模式复用集合，并为关键路径加上分配量回归测试。

## 2. 前置知识

本讲默认你已完成 u3-l1，理解以下概念（若陌生请先回去复习）：

- **三字表示（three words）**：一个 `Vec<T>` 在栈上由「长度 length、容量 capacity、指针 pointer」三个机器字组成；元素本身始终在堆上。
- **长度与容量**：length 是当前已有元素数，capacity 是「不重新分配就能容纳的最大元素数」。
- **分配率与 DHAT**：堆分配中等昂贵，可用 DHAT / dhat-rs 定位「哪一行、多少次、多大、活多久」。
- **摊还成本（amortized cost）**：把偶发的昂贵操作（如整块拷贝）平摊到大量廉价操作上得到的「平均」成本。

补充一个本讲要用到的直觉：**真实的扩容不是免费的**。每次容量不够，`Vec` 都要向分配器申请新的更大内存、把旧元素逐个拷贝过去、再释放旧内存。所以「少触发扩容」≈「少分配 + 少拷贝」≈ 更快。本讲的所有技巧本质上都是在减少「扩容/重新分配」的次数。

## 3. 本讲源码地图

本讲几乎全部内容来自 perf-book 的《Heap Allocations》一章，我们按主题拆开来看：

| 源码文件 | 相关章节 | 本讲用途 |
| --- | --- | --- |
| `src/heap-allocations.md` | `### Vec Growth` | 准倍增长策略、0→4 的跳跃、长度分布测量 |
| `src/heap-allocations.md` | `### Short Vecs` | `SmallVec` / `ArrayVec` 处理短向量 |
| `src/heap-allocations.md` | `### Longer Vecs` | `with_capacity` / `reserve` / `shrink_to_fit` 预分配 |
| `src/heap-allocations.md` | `## Reusing Collections` | workhorse 集合 + `clear` 复用 |
| `src/heap-allocations.md` | `## Reading Lines from a File` | workhorse `String` 读行的经典案例 |
| `src/heap-allocations.md` | `## Avoiding Regressions` | dhat-rs 的「堆用量测试」防回归 |

> 提示：`String`、`HashMap`/`HashSet` 在「容量与增长」上与 `Vec` 同构，本讲对 `Vec` 的结论基本可直接套用。

## 4. 核心概念与源码讲解

### 4.1 Vec 的容量与准倍增长策略

#### 4.1.1 概念说明

回忆三字表示：`Vec` 自身只是「长度 + 容量 + 指针」。当 `length < capacity` 时，`push` 只是把元素写进已预留的空位，**零分配**；只有当 `length == capacity` 还要继续 `push` 时，才会触发一次**重新分配（reallocation）**：申请新内存、拷贝旧元素、释放旧内存。

关键问题是：扩容时新容量取多大？Rust 语言规范**没有规定**增长策略，它由标准库的实现决定。理解这个策略，是判断「我的 `Vec` 到底分配了多少次」的前提，也是后续选择 `with_capacity` / `SmallVec` 的依据。

#### 4.1.2 核心流程

当前标准库使用**准倍增长（quasi-doubling）**，容量序列为：

\[ 0 \rightarrow 4 \rightarrow 8 \rightarrow 16 \rightarrow 32 \rightarrow 64 \rightarrow \cdots \]

有两个要点：

1. **从 0 直接跳到 4**，而不是经过 1、2。因为绝大多数 `Vec` 都会装少数几个元素，若按 0→1→2→4 慢速爬升，会白白多触发几次分配；实测跳到 4 能「在真实程序中避免大量分配」。
2. **之后大致翻倍**。这带来一个经典的「指数权衡」：
   - 随着 `Vec` 增长，**重新分配的频率按指数下降**（容量越大，离下一次翻倍越远）；
   - 但**可能浪费的剩余容量也按指数上升**（最后一次翻倍可能留下接近一半的空位）。

为什么翻倍是「好」策略？因为它让 `push` 的**摊还成本**为常数。设最终容量为 \(n\)（\(n \ge 4\)），沿途拷贝的元素总数是几何级数：

\[ 4 + 8 + 16 + \cdots + n \;<\; 2n \]

也就是说，把一个 `Vec` 从空 `push` 到 \(n\) 个元素，总工作量是 \(O(n)\)，于是平摊到每一次 `push` 上：

\[ \frac{O(n)}{n} = O(1) \]

这就是「摊还 \(O(1)\)」的来源。它很好，但不是免费的——上式只保证**平均**常数，个别 `push` 仍会触发整块拷贝；而且如果你其实只想要 5 个元素，标准库仍会先按 4→8 扩一次容。

**所以：当你能预知规模时，准倍增长通常不是最优的。** 书里给了一条非常实用的诊断手段：与其拍脑袋猜 `Vec` 有多长，不如**量出来**——在热点 `push` 处用 `eprintln!` 打印长度，再用 `counts` 工具统计长度分布，然后据此判断是「很多短向量」还是「少数超长向量」，两者的优化方向完全不同。

#### 4.1.3 源码精读

先看准倍增长策略本身的描述：

[src/heap-allocations.md:L109-L119](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md#L109-L119) — 说明空 `Vec`（由 `vec![]`/`Vec::new`/`Vec::default` 创建）长度与容量都为 0、不分配；反复 `push` 会周期性重新分配；增长策略未由语言规定，但当前实现为准倍增长，容量序列 0, 4, 8, 16, 32, 64……；并解释了 0→4 的跳跃是为了「在实践中避免大量分配」（对应 rustc 的 [PR #72227](https://github.com/rust-lang/rust/pull/72227)），以及「重分配频率指数下降、浪费容量指数上升」这一权衡。

再看「量长度分布」的诊断建议：

[src/heap-allocations.md:L125-L132](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md#L125-L132) — 准倍增长在一般情况下合理，但若能预知长度往往可以做得更好。建议在热点分配点（例如热的 `Vec::push` 调用）用 `eprintln!` 打印向量长度，再用 `counts` 做后处理得到长度分布；分布形态（「很多短向量」对「少数超长向量」）决定了最优的优化方式。

> 旁注：`Vec` 的三字表示与 length/capacity 的精确定义见 [src/heap-allocations.md:L93-L101](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md#L93-L101)，u3-l1 已详细讲过，这里不重复。

#### 4.1.4 代码实践：量出 Vec 的长度分布

1. **实践目标**：亲手看到一个热点 `Vec` 的真实长度分布，体会「先测量再优化」。
2. **操作步骤**：准备一段会在循环里反复 `push` 的程序（示例代码），在 `push` 之后用 `eprintln!` 把当前长度打到 stderr：

   ```rust
   // 示例代码：仅用于演示测量，非书中代码
   fn build(items: &[u32]) -> Vec<u32> {
       let mut v = Vec::new();
       for &x in items {
           v.push(x);
           eprintln!("len: {}", v.len()); // 热点诊断输出
       }
       v
   }
   ```

   运行时把 stderr 重定向到文件：`cargo run --release 2> lens.txt`，再用 [`counts`](https://github.com/nnethercote/counts/) 统计 `lens.txt` 里每个长度出现的频次。
3. **需要观察的现象**：直方图会告诉你长度集中在哪一段——是大量 0~3 的短向量，还是少数几百上千的长向量？
4. **预期结果**：得到一张「长度 → 出现次数」的频率表。
5. **待本地验证**：`counts` 的具体命令行参数请以其仓库 README 为准（典型用法是 `counts lens.txt`），不同版本可能略有差异。

#### 4.1.5 小练习与答案

**练习 1**：为什么标准库从 0 直接跳到 4，而不是 0→1→2→4？

> **参考答案**：因为真实程序里大量 `Vec` 只装很少几个元素。若慢速爬升 0→1→2，每个短向量都会多触发 1~2 次重新分配；直接跳到 4 能在实测中「避免大量分配」，对短向量场景净赚。

**练习 2**：一个 `Vec` 从空开始 `push` 到 20 个元素，按准倍增长会经历几次重新分配？容量分别变成多少？

> **参考答案**：4 次。容量依次变为 4、8、16、32（第 5、9、17 个元素时各触发一次扩容，最终容量 32）。

**练习 3**：既然摊还成本是 \(O(1)\)，为什么我们还要费心优化扩容？

> **参考答案**：摊还 \(O(1)\) 只是「平均」廉价；它仍包含偶发的整块拷贝，且默认策略对「已知规模」并非最优。当你能预知长度，用一次 `with_capacity` 直接命中容量，可以省掉全部中间扩容与对应拷贝，在热点上收益明显。

### 4.2 with_capacity / reserve 预分配

#### 4.2.1 概念说明

模块 4.1 的诊断告诉我们长度分布后，最常见的一种结论是：「我知道这个 `Vec` 大约会装 N 个元素」。这时最直接的优化就是**预分配**：一开始就向分配器要一块够大的内存，让后续 `push` 全部落进已预留的空位，彻底跳过准倍增长的中间几次扩容。

标准库提供了三个相关入口，区别在于你给出的是「下界」还是「精确值」：

| 方法 | 你提供的信息 | 保证 |
| --- | --- | --- |
| `Vec::with_capacity(n)` | 期望容量 | 创建即拥有至少 `n` 容量 |
| `Vec::reserve(additional)` | 还要再 push 多少 | 至少能再容纳 `additional` 个 |
| `Vec::reserve_exact(n)` | 精确容量 | 容量「恰好」够（实际仍可能略大） |

#### 4.2.2 核心流程

书里的经典对照：若你知道 `Vec` 至少会长到 20 个元素——

- **逐个 push**：触发 4 次重新分配，容量依次爬升为 4、8、16、32；
- **`Vec::with_capacity(20)`**：1 次分配，直接拿到容量 ≥ 20。

两者的元素总数一样，但分配次数从 4 降到 1，拷贝次数也从 \(4+8+16=28\) 次降到 0。

预分配还有一个「反向」用途：当你知道**最大**长度时，可以用 `reserve`/`reserve_exact` 避免分配多余空间；此外 `Vec::shrink_to_fit` 能把容量收缩到贴近长度以减少浪费，但要注意它**本身可能触发一次重新分配**，所以只适合在「这个 `Vec` 之后会长期占用内存」的场景下用，而不是在热路径上反复调用。

#### 4.2.3 源码精读

[src/heap-allocations.md:L166-L185](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md#L166-L185) — `### Longer Vec's` 一节。说明若已知最小或精确大小，可用 `Vec::with_capacity` / `Vec::reserve` / `Vec::reserve_exact` 预留容量；以「长到 20 个元素」为例，预分配只需 1 次分配，而逐个 push 需要 4 次（容量 4/8/16/32）。随后指出：若已知最大长度，这些函数还能避免多余空间；`Vec::shrink_to_fit` 可最小化浪费，但可能引发重新分配。

#### 4.2.4 代码实践：用 with_capacity 砍掉中间扩容

1. **实践目标**：直观验证「预知规模 → 预分配」能把分配次数降到 1。
2. **操作步骤**：写一个返回固定大小 `Vec` 的函数，先不预分配，再用 `with_capacity` 改写（示例代码）：

   ```rust
   // 示例代码
   // 改写前：逐个 push
   fn squares_naive(n: usize) -> Vec<u64> {
       let mut v = Vec::new();
       for i in 0..n as u64 { v.push(i * i); }
       v
   }
   // 改写后：预分配
   fn squares_prealloc(n: usize) -> Vec<u64> {
       let mut v = Vec::with_capacity(n);
       for i in 0..n as u64 { v.push(i * i); }
       v
   }
   ```

   在 `n = 20` 下，用 `dhat-rs`（见 4.4）或 DHAT 统计两个版本各自触发的分配次数。
3. **需要观察的现象**：`squares_naive` 应出现约 4 次分配（容量 4/8/16/32），`squares_prealloc` 应只有 1 次。
4. **预期结果**：分配次数 4 → 1，且 `squares_prealloc` 不含任何元素拷贝。
5. **待本地验证**：精确分配次数以本地 `dhat-rs`/DHAT 输出为准。

#### 4.2.5 小练习与答案

**练习 1**：`reserve` 和 `reserve_exact` 有何区别？什么时候选 `reserve_exact`？

> **参考答案**：`reserve(additional)` 保证容量「至少」增加 `additional`（分配器可能给更多）；`reserve_exact` 请求「恰好」够的容量（仍允许略大）。当你对上限很敏感、想尽量省内存时选 `reserve_exact`，但要注意它不保证真的精确，也不一定比 `reserve` 更省。

**练习 2**：`Vec::shrink_to_fit` 看似「白捡」的省内存，为什么不能随便在热路径上调用？

> **参考答案**：因为它可能触发一次重新分配（把元素搬到更小的内存块），这本身就是一次分配 + 拷贝。它适合在「`Vec` 此后会长期驻留、想压低常驻内存」时用一次，而不适合在反复增删的热路径上调用。

### 4.3 SmallVec / ArrayVec 处理短 Vec

#### 4.3.1 概念说明

4.1 的长度分布有时会给出另一种结论：**「非常多、但每一个都很短」的 `Vec`**。比如编译器里每个 token tree、每个语法节点可能只挂 1~3 个子节点。这时准倍增长哪怕跳到 4，也仍是「为短数据付出一次堆分配」。

`with_capacity` 解决不了这个问题——它只能减少扩容次数，不能消除「短向量也要上堆」这件事。解决办法是把少量元素**直接存进 `Vec` 自身所在的栈内存**，跳过堆分配。两个常用 crate：

- **`SmallVec<[T; N]>`**（[`smallvec`](https://crates.io/crates/smallvec)）：前 `N` 个元素就地内联存放，超过 `N` 才回退到堆分配（spilled）。
- **`ArrayVec<T, N>`**（[`arrayvec`](https://crates.io/crates/arrayvec)）：容量恒为 `N`，**永不**上堆，超过即越界。

#### 4.3.2 核心流程

`SmallVec` 的核心是「**就地存放 + 按需上堆**」：

```
元素数 ≤ N  →  全部存在 SmallVec 自身的 N 个槽位里（零堆分配）
元素数 > N  →  把元素搬到堆上，后续行为退化为普通 Vec
```

它的代价不在分配上，而在**每次操作都要多一次判断**：「当前是内联还是已经上堆？」因此：

- `SmallVec` 能可靠地降低分配率，但**不保证更快**——正常操作比 `Vec` 略慢。
- 若 `N` 取太大或元素类型 `T` 本身很大，`SmallVec<[T; N]>` 自身会比 `Vec<T>` 大得多，拷贝/传参反而更慢。
- 结论依旧是那句话：**必须 benchmark 才能确认收益**。

如果不仅「短」而且你能**精确知道最大长度**，`ArrayVec` 更好：它没有「上堆回退」分支，少了一次判断，因此比 `SmallVec` 再快一点点，且永不堆分配——代价是超过 `N` 会直接报错而非自动扩容。

一句话决策表：

| 场景 | 选择 |
| --- | --- |
| 数量未知 / 可能很大 | `Vec`（必要时 `with_capacity`） |
| 多数很短、偶尔超长，难定上限 | `SmallVec<[T; N]>` |
| 多数很短、且**精确**知道上限 | `ArrayVec<T, N>` |

#### 4.3.3 源码精读

[src/heap-allocations.md:L138-L156](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md#L138-L156) — `### Short Vec's` 开头介绍 `SmallVec`：它是 `Vec` 的 drop-in 替代，可在自身内存里存 `N` 个元素、超出才转堆分配；并提醒 `vec![]` 字面量要换成 `smallvec![]`。随后点明 `SmallVec` 能降低分配率但**不保证提速**：每次操作都要检查是否已上堆而略慢于 `Vec`；`N` 过大或 `T` 过大时 `SmallVec` 自身会比 `Vec` 更大、拷贝更慢，必须 benchmark。

[src/heap-allocations.md:L158-L164](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md#L158-L164) — 介绍 `ArrayVec`：当你既有很多短向量**又**精确知道最大长度时，它比 `SmallVec` 更优，因为不需要「上堆回退」分支，略快一些。

#### 4.3.4 代码实践：把短 Vec 换成 SmallVec

1. **实践目标**：体会「短而多」的 `Vec` 换成 `SmallVec` 后分配率下降，但运行时间未必更快。
2. **操作步骤**：构造大量只装 2 个元素的集合（示例代码），先后用 `Vec` 与 `SmallVec<[u32; 4]>`，用 `dhat-rs` 统计分配次数、用基准测试比较耗时：

   ```rust
   // 示例代码（Cargo.toml 需加 smallvec 依赖）
   use smallvec::{SmallVec, smallvec};
   // 改写前：let pairs: Vec<Vec<u32>> = ... 每个内层 Vec 都分配
   // 改写后：
   let pairs: Vec<SmallVec<[u32; 4]>> = (0..100_000)
       .map(|i| smallvec![i as u32, (i + 1) as u32])
       .collect();
   ```

3. **需要观察的现象**：`SmallVec` 版本的内层集合**零堆分配**（2 ≤ 4，全内联）；但基准测试里它未必比 `Vec` 版快，甚至略慢。
4. **预期结果**：分配次数显著下降； wall-time 收益取决于拷贝/判断的额外开销，可能为正也可能为负。
5. **待本地验证**：是否真提速以本地 benchmark 为准——这正是书中「benchmarking is required」的含义。

#### 4.3.5 小练习与答案

**练习 1**：既然 `SmallVec` 能降分配率，为什么书里反复强调「不保证更快」？

> **参考答案**：因为它的每次操作都要先判断「元素当前是内联还是已上堆」，这给正常路径增加了开销；此外大 `N` 或大 `T` 会让 `SmallVec` 自身变大、按值传递更慢。分配少了≠整体快了，必须实测。

**练习 2**：什么前提下 `ArrayVec` 比 `SmallVec` 更合适？

> **参考答案**：当你不仅有很多短向量，还**精确知道**它们的最大长度时。`ArrayVec` 没有上堆回退分支，省掉一次判断、略快，且永不堆分配；代价是超过容量会直接失败而非自动扩容。

### 4.4 复用 workhorse 集合与分配回归测试

#### 4.4.1 概念说明

前三个模块都在「单个 `Vec` 生命周期内」做文章。但很多热点的真实形态是：**循环每一轮都需要一个临时 `Vec`/`String`，用完就丢**。如果每轮都 `let v = Vec::new();`，等于每轮都重新走一遍准倍增长、用完再释放——分配率被白白拉高。

这类场景的解法不是预分配，而是**复用**：把同一个集合留在循环外（或结构体里）反复使用，每轮结束时 `clear()` 清空内容但**保留容量**，下一轮就能直接往已预留的空位里写。书里把这个被反复骑乘的集合叫做 **「workhorse」collection（主力马集合）**。

本模块还要引入一个**与定位热点不同**的 `dhat-rs` 用法：不是跑一次看报告，而是写**回归测试**，断言「这段代码恰好分配了这么多次/这么多字节」，从而在未来某次改动让分配悄悄变多时让 CI 失败。

#### 4.4.2 核心流程

复用集合有两个层次：

1. **就地修改优于反复新建**。一个会被多次调用的函数，与其每次 `return vec![x, y]`（每次新建并分配），不如接受一个 `&mut Vec` 往里 push，让调用方决定缓冲区的命运。
2. **workhorse 集合 + `clear`**。循环外声明、循环内使用、循环末尾 `clear()`：

   ```
   声明 workhorse Vec（循环外）
   for 每一轮:
       使用 workhorse（push / 查找 / ...）
       workhorse.clear()   // 清空 length，保留 capacity
   ```

   `clear()` 的妙处在于：它把长度归零但**不动容量**，于是上一轮攒下的容量在下一轮直接复用，多数情况下整个循环只有最初几次扩容，之后零分配。

书里给了一个特别清晰的经典案例——**按行读文件**：

- `BufRead::lines()` 返回的迭代器产出 `io::Result<String>`，**每一行都分配一个新 `String`**；
- 改用 workhorse `String` 配合 `BufRead::read_line(&mut line)`，循环末尾 `line.clear()`，分配次数能降到「最多几次、甚至只有一次」——具体取决于最长那一行触发了几次扩容。

最后是防回归。`dhat-rs` 的 **heap usage testing（堆用量测试）** 让你写这样的测试：「跑这段代码，断言它分配的总字节 / 总次数等于某个期望值」。这样一旦有人改了实现、无意中多分配，测试立刻变红。

#### 4.4.3 源码精读

[src/heap-allocations.md:L340-L358](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md#L340-L358) — `## Reusing Collections`：分阶段构建集合时，修改单个 `Vec` 通常优于建多个再合并。对比 `do_stuff` 的两种写法——返回新 `Vec`（每次调用都分配）对改为接收 `&mut Vec<u32>` 往里 push（复用调用方的缓冲）。

[src/heap-allocations.md:L360-L372](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md#L360-L372) — workhorse 集合模式：循环外声明 `Vec`、循环内使用、循环末尾 `clear()`（清空但不影响容量）；这能避免分配，代价是「各轮对 `Vec` 的使用彼此无关」这一事实被掩盖。同样地，也可把 workhorse 集合放在结构体里，供被反复调用的方法复用。

[src/heap-allocations.md:L374-L410](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md#L374-L410) — `## Reading Lines from a File`：`BufRead::lines()` 每行分配一个 `String`；改用 workhorse `String` + `BufRead::read_line`，把分配降到最多几次、甚至一次；前提是循环体能用 `&str` 而非 `String`。

[src/heap-allocations.md:L427-L434](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md#L427-L434) — `## Avoiding Regressions`：用 `dhat-rs` 的 *heap usage testing* 写测试，检查特定代码片段是否分配了期望的堆内存量，防止分配数/大小无意中上升。

#### 4.4.4 代码实践：循环外复用 workhorse Vec

1. **实践目标**：把「循环内每轮新建 `Vec`」改成「循环外复用 + `clear`」，观察分配次数骤降。
2. **操作步骤**：准备一个每轮都要收集若干中间结果的循环（示例代码），先写「每轮新建」版，再写 workhorse 版：

   ```rust
   // 示例代码
   // 改写前：每轮新建一个 Vec
   for round in 0..100_000 {
       let mut buf = Vec::new();      // 每轮都分配
       collect_into(&mut buf, round);
       consume(&buf);
   }
   // 改写后：循环外复用
   let mut workhorse: Vec<u32> = Vec::new();
   for round in 0..100_000 {
       workhorse.clear();             // 清空内容，保留容量
       collect_into(&mut workhorse, round);
       consume(&workhorse);
   }
   ```

   用 `dhat-rs` 在两版上各跑一遍，统计 `collect_into` 相关的分配次数。
3. **需要观察的现象**：改写前每轮 1 次以上分配（共十万次级）；改写后整个循环只在最初几轮扩容，之后几乎零分配。
4. **预期结果**：分配次数从「与循环轮数同阶」降到「常数级」。
5. **待本地验证**：因 `collect_into` 单轮元素数而异，以本地 `dhat-rs` 输出为准。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `clear()` 之后容量还在？这与「节省分配」有什么关系？

> **参考答案**：`clear()` 只把长度置零、并按值 drop 掉已有元素，**不释放底层内存**，容量保持不变。下一轮 `push` 直接落进预留空位，于是复用了上一轮攒下的容量，避免了重新分配。

**练习 2**：`BufRead::lines()` 为什么「每行都分配」？workhorse `String` 方案的限制是什么？

> **参考答案**：`lines()` 的迭代器产出 `io::Result<String>`，每个 `String` 都是新的堆分配。改用 `read_line(&mut line)` 复用同一个 `String` 后，分配降到常数次；但前提是循环体能接受 `&str`（借用那一行）而非拥有 `String`，否则复用就无法成立。

**练习 3**：`dhat-rs` 的「定位热点」与「堆用量回归测试」两种用法，区别在哪里？

> **参考答案**：定位热点是**一次性诊断**——跑完看报告，人工判断改哪里；堆用量测试是**持续守护**——把期望的分配量写进断言，未来任何让分配变多的改动都会让测试失败，把「分配回归」变成 CI 能拦下的硬约束。

## 5. 综合实践

把本讲四条主线串起来，完成一次完整的「测量 → 决策 → 优化 → 防回归」闭环：

1. **测量**：找一段在热点循环里反复 `push` 的 `Vec` 代码。先按 4.1.4 的方法，用 `eprintln!` 打印长度、用 `counts` 得到长度分布直方图。
2. **决策**：依据分布下判断——
   - 若长度集中在某个明确的较大值 → 走 4.2 的 `Vec::with_capacity`；
   - 若是「大量极短向量」且上限可知 → 走 4.3 的 `SmallVec`/`ArrayVec`；
   - 若是「每轮都用完即丢」→ 走 4.4 的 workhorse + `clear`。
3. **优化**：按所选方向改写代码。
4. **验证 + 防回归**：用 `dhat-rs` 统计改写前后的分配次数，确认下降；再按 4.4.3 的 `dhat-rs` heap usage testing，为这段代码写一个回归测试，把优化后的分配量固化为断言。

> 这个任务刻意把「先测量再优化」（u2 单元的工作流）和本讲的 `Vec` 专项技巧拧在一起：分布形态决定优化手段，没有银弹；而回归测试保证今天的优化不会被明天的改动悄悄抹掉。

## 6. 本讲小结

- `Vec` 的准倍增长容量序列为 0, 4, 8, 16, 32, 64……，0→4 的跳跃是为了在真实程序中少分配；它让 `push` 摊还 \(O(1)\)，但「重配频率指数下降」与「浪费容量指数上升」是一对权衡。
- 不要靠猜，要靠量：在热点 `push` 处用 `eprintln!` 打长度、用 `counts` 出分布，再决定怎么优化。
- **已知规模** → `Vec::with_capacity` / `reserve` / `reserve_exact` 一次命中容量；`shrink_to_fit` 可省内存但自身可能触发重配。
- **大量短向量** → `SmallVec`（内联 N 个、超出上堆，不保证更快）；**且精确知上限** → `ArrayVec`（永不上堆，略快）。
- **循环里反复用完即丢** → workhorse 集合 + `clear()` 复用容量；按行读文件用 workhorse `String` + `read_line` 而非 `lines()`。
- 用 `dhat-rs` 的 heap usage testing 把分配量写成回归测试，让「分配变多」成为 CI 能拦下的硬错误。

## 7. 下一步学习建议

- **横向迁移**：本讲对 `Vec` 的结论几乎都适用于 `String`、`HashMap`/`HashSet`（同为「单块连续堆内存 + 按需重配」），可对照阅读 `src/heap-allocations.md` 的 `## String` 与 `## Hash Tables` 两节。
- **纵向深入（下一讲 u3-l3）**：当 `Vec` 之外的字段也开始膨胀，下一步是《Type Sizes》——用 `-Zprint-type-sizes` 量类型布局、用「装箱大变体」「更小整数」「boxed slice」缩小频繁实例化的类型，并用 `static_assertions` 防体积回归，与本讲的 dhat-rs 防分配回归遥相呼应。
- **工具链补全**：若尚未熟悉 `dhat-rs`/DHAT 的实际接入，建议先回到 u2-l2（Profiling）补齐「为 release 构建开调试信息与帧指针」的配置，再回到本讲写堆用量回归测试。
