# Iterators——collect、chain 与 chunks 的取舍

## 1. 本讲目标

Rust 的迭代器（iterator）既优雅又常常零开销，但「写法上的细微差别」会直接影响**分配次数**和**生成的机器码质量**。本讲精读 perf-book 第 15 章「Iterators」，并联动「Heap Allocations」一章中关于 `Vec` 分配的结论，目标是让你学完后能够：

- 理解为什么「先 `collect` 成 `Vec`、再立刻遍历一次」是一个应当消除的反模式。
- 掌握把函数返回值从 `Vec<T>` 改为 `impl Iterator<Item = T>` 的写法，并用 `size_hint` 帮 `collect`/`extend` 减少分配。
- 了解 `chain`、`filter_map` 在热点路径上的取舍。
- 学会用 `slice::chunks_exact` 取代 `chunks`、用 `iter().copied()` 取代 `iter()` 来换取更好的代码生成。

> 本讲承接 u3-l1（堆分配）。请记住那里的核心结论：**堆分配中等昂贵**，`Vec` 是「长度 / 容量 / 指针」三字表示，元素总在堆上，并以准倍策略增长。本讲所有优化，本质都是在「少分配」与「让 LLVM 生成更紧凑的代码」这两条线上做文章。

## 2. 前置知识

阅读本讲前，你最好已经了解：

- **迭代器的基本用法**：`for x in iter`、`.map()`、`.filter()`、`.collect()` 这些日常写法。
- **所有权与借用的基本概念**：尤其是「拥有（owned）」与「借入（borrowed，引用）」数据的区别——本讲的 `copied` 一节正是围绕这个区别展开。
- **`Vec` 的分配行为**：这是 u3-l1 的内容。一句话复习：`collect` 把迭代器变成 `Vec` 时通常要分配一次堆内存，这正是我们要优化的对象。
- **泛型与生命周期**：返回 `impl Iterator` 有时需要在返回类型上额外标注生命周期，本讲会解释为什么。

不需要你事先了解 `size_hint`、`ExactSizeIterator`、`chunks_exact` 等细节——这些会在讲解中逐一引入。

## 3. 本讲源码地图

本讲主要精读下面两个书稿源文件（perf-book 的「源码」就是这些 Markdown 章节本身）：

| 文件 | 作用 | 本讲用到的小节 |
|------|------|----------------|
| `src/iterators.md` | 第 15 章「Iterators」全文，是本讲的主体 | `collect` and `extend`、Chaining、Chunks、`copied` 四节 |
| `src/heap-allocations.md` | 第 9 章「Heap Allocations」 | `Vec` 三字表示、`Vec` Growth、Reusing Collections、Reading Lines from a File |

之所以同时引用后者，是因为迭代器优化与堆分配紧密相关：`collect` 的代价本质上是一次 `Vec` 分配，而理解 `Vec` 的增长策略才能理解 `size_hint` 为何能「减少分配」。此外，「按行读文件」一节是「避免逐次分配」这一思想在 I/O 领域的经典案例，与本讲的 `collect`/`extend` 主题一脉相承。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. **4.1 避免不必要的 `collect`**——`collect` 与 `extend` 的分配代价。
2. **4.2 返回 `impl Iterator` 与 `size_hint`**——从源头消灭中间 `Vec`。
3. **4.3 `chain` 与 `filter_map` 的取舍**——组合迭代器的隐藏开销。
4. **4.4 `chunks_exact` 与 `copied`**——让 LLVM 生成更好的代码。

### 4.1 避免不必要的 `collect`

#### 4.1.1 概念说明

`Iterator::collect` 把一个迭代器「收拢」成一个集合（最常见是 `Vec`）。这件事通常**需要一次堆分配**。于是 perf-book 给出第一条也是最直接的规则：**如果这个集合收完之后只是又被遍历一次，就不该 `collect`**——你为了一次额外的遍历，白白付了一次分配的钱。

这里的直觉是：`collect` 制造了一个「中间产物」。当这个中间产物既不被长期持有、也不被反复使用时，它的全部价值只是「把数据搬到一个新的堆缓冲区里」，这恰好是分配开销的定义（见 u3-l1）。

与之相对的是 `extend`：当你本来就**已经有一个集合**，想把另一个迭代器的元素追加进去时，应该用 `extend` 直接喂迭代器，而不是先 `collect` 成一个新 `Vec` 再用 `append` 合并。两者结果相同，但后者多了一次分配。

#### 4.1.2 核心流程

把「`collect` + 再次遍历」这个反模式与正确写法的执行流程对照如下：

**反模式（多一次分配）：**
```
iter → collect() → 临时 Vec（分配）→ 再次遍历 → 丢弃 Vec（释放）
```

**正确写法（直接消费迭代器）：**
```
iter → 直接消费 → 无中间 Vec
```

关键点：两条路最终「算的东西」一样，差别只在中间那个临时 `Vec` 是否存在。`collect` 的分配代价来自 `Vec` 的三字表示与准倍增长——这正是 `heap-allocations.md` 讲过的内容，下面在源码精读中会引用。

#### 4.1.3 源码精读

perf-book 在「`collect` and `extend」一节开头就点明了规则：

[src/iterators.md:5-7](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/iterators.md#L5-L7) ——「`collect` 把迭代器转成 `Vec` 这类集合，通常需要分配；如果集合之后只是又被遍历一次，就应避免 `collect`。」

关于 `extend` 优于「`collect` + `append`」，书里紧接着说：

[src/iterators.md:19-21](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/iterators.md#L19-L21) ——用 `extend` 把迭代器直接追加到已有 `Vec`，胜过先 `collect` 成新 `Vec` 再 `append`。

要理解「为什么 `collect` 是分配」，需要回到堆分配章对 `Vec` 的描述：

[src/heap-allocations.md:93-95](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md#L93-L95) ——`Vec` 由长度、容量、指针三个字（word）组成；容量非零且元素非零尺寸时，指针指向堆内存。

也就是说，`collect` 出来的 `Vec` 一旦非空，它的指针就指向一块堆分配。而分配的次数取决于增长策略：

[src/heap-allocations.md:113-119](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md#L113-L119) ——`Vec` 采用准倍（quasi-doubling）增长，容量序列为 0、4、8、16、32、64……；重配频率随元素增多而指数下降，浪费容量则指数上升。

这条增长序列告诉我们：`collect` 在「不知道有多少元素」时，可能触发 4→8→16→32 这一串重配。粗略地说，收集 \(n\) 个元素、且迭代器没有给出有用的 `size_hint` 时，重配次数约为

\[
\text{重配次数} \;\lesssim\; \lceil \log_2 n \rceil - 1 \quad (n > 4)
\]

而如果迭代器**能**提前告知元素数量（见 4.2 节的 `size_hint`），`collect` 可以一次 `Vec::with_capacity(n)` 命中容量，把重配次数压到 1。这正是 4.2 节要讲的优化。

此外，「Reusing Collections」一节也强化了同一个思想——**逐步修改一个 `Vec`，好过造多个 `Vec` 再合并**，这与 `extend` 优于 `collect`+`append` 是一回事：

[src/heap-allocations.md:340-344](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md#L340-L344) ——通常应当通过修改单个 `Vec` 来分阶段构建集合，而不是先建多个 `Vec` 再拼接。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「`collect` 后只遍历一次」会比「直接消费迭代器」多出一次（或多次）分配。

**操作步骤**（示例代码，非项目原有代码）：

1. 新建一个小 crate，加入 `dhat-rs`（u3-l1 介绍过的进程内堆分析库）作为开发依赖，并在 `main` 里开启 `dhat::Profiler`。
2. 写两个版本的同义函数：

```rust
// 版本 A：collect 后只遍历一次（反模式）
fn sum_via_collect(it: impl Iterator<Item = u64>) -> u64 {
    let v: Vec<u64> = it.collect();   // 分配一个 Vec
    v.iter().sum()                     // 紧接着只遍历一次
}

// 版本 B：直接消费迭代器
fn sum_direct(it: impl Iterator<Item = u64>) -> u64 {
    it.sum()                           // 无中间 Vec
}
```

3. 分别用 `sum_via_collect` 与 `sum_direct` 处理同一个长迭代器（例如 `0..1_000_000`），打印 `dhat::Stats` 中的 `total_bytes` / `total_blocks`。

**需要观察的现象**：版本 A 报告若干字节、若干块的堆分配（来自那个临时 `Vec`）；版本 B 报告的分配量明显更少（理想情况下为零额外分配）。

**预期结果**：`sum_direct` 的分配计数低于 `sum_via_collect`。

**待本地验证**：具体的分配字节数与块数取决于 `dhat-rs` 版本与迭代器是否实现 `size_hint`（见 4.2 节），请以本地实测为准。

#### 4.1.5 小练习与答案

**练习 1**：下面这段代码哪里违反了本节规则？如何改写？

```rust
fn lengths(words: impl Iterator<Item = String>) -> Vec<usize> {
    let v: Vec<String> = words.collect();
    v.iter().map(|w| w.len()).collect()
}
```

**参考答案**：`words` 被 `collect` 成 `v` 后只是立刻被 `map` 遍历一次，`v` 这个中间 `Vec` 是多余的。可以直接链式处理：

```rust
fn lengths(words: impl Iterator<Item = String>) -> Vec<usize> {
    words.map(|w| w.len()).collect()
}
```

这里**最后一次** `collect` 是必要的，因为函数确实要返回 `Vec`；被去掉的是「只遍历一次」的那个中间 `Vec`。

**练习 2**：你已有一个 `Vec<u32> out` 和一个迭代器 `it`，想把 `it` 的所有元素加进 `out`。下面两种写法哪种更优？为什么？
- (a) `out.extend(it);`
- (b) `let tmp: Vec<_> = it.collect(); out.append(&mut tmp);`

**参考答案**：(a) 更优。(b) 会为 `tmp` 额外分配一个 `Vec`，而 `extend` 直接把元素追加进 `out`，省掉这次分配，正是 perf-book 推荐的做法。

### 4.2 返回 `impl Iterator` 与 `size_hint`

#### 4.2.1 概念说明

4.1 节讨论的是「消费侧」如何避免 `collect`。本节转到「生产侧」：如果一个函数对外**返回**一堆数据，把它声明为返回 `Vec<T>` 会**强迫**调用者接受一个已分配的 `Vec`——哪怕调用者本来只想遍历一次。更友好的签名是返回 `impl Iterator<Item = T>`：把「要不要物化成集合」的决定权交还给调用者。

`impl Iterator` 是一种「不透明返回类型」（existential type）：调用者只知道「它是个 `Iterator`，产出 `T`」，不需要知道它具体是 `Map<...>` 还是 `Chain<...>` 这种冗长的具体类型，也不必把具体类型写进签名。

但这里有个隐藏陷阱：`collect`/`extend` 在收到一个迭代器时，如果**不知道它会产生多少元素**，就只能走「边 push 边准倍扩容」的老路（4.1.2 节那条重配序列），可能重配多次。解决办法是实现 `size_hint`——告诉消费方「我大概会产出多少元素」，让 `collect` 能一次 `with_capacity` 命中。

#### 4.2.2 核心流程

把「返回 `Vec`」与「返回 `impl Iterator`」对照：

```
返回 Vec<T>：        函数内必定 collect/构建 Vec（分配）→ 调用者拿到已分配的 Vec
返回 impl Iterator：  函数内只产出迭代器（通常不分配）→ 调用者按需决定是否 collect
```

`size_hint` 的作用可画成一条信息流：

```
迭代器知道元素数 ──(size_hint)──▶ collect/extend 提前 reserve 容量 ──▶ 单次分配命中
迭代器不知道元素数 ──(返回 (0, None))──▶ collect 逐个 push ──▶ 准倍重配序列（4→8→16→…）
```

`size_hint` 返回一个元组 `(lower_bound, Option<usize>)`：下界必给，上界可选。实现 `ExactSizeIterator`（其 `len` 给出精确数量）则更进一步，`collect` 可以精确预分配。

#### 4.2.3 源码精读

perf-book 在同一节里给出了「返回迭代器优于返回 `Vec`」的建议：

[src/iterators.md:11-14](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/iterators.md#L11-L14) ——通常从函数返回 `impl Iterator<Item=T>` 比返回 `Vec<T>` 更好；注意有时返回类型上需要额外的生命周期标注。

随后书里强调，自己写迭代器时值得实现 `size_hint` 或 `ExactSizeIterator::len`：

[src/iterators.md:26-30](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/iterators.md#L26-L30) ——实现 `size_hint` 或 `ExactSizeIterator::len` 后，用到该迭代器的 `collect`/`extend` 因为提前知道了元素个数，可能做更少的分配。

把这条建议与堆分配章的增长序列合起来看就完整了：`size_hint` 提供的「提前信息」让 `collect` 跳过 [src/heap-allocations.md:113-119](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md#L113-L119) 那条准倍重配序列，直接用 [src/heap-allocations.md:168-174](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md#L168-L174) 提到的 `Vec::with_capacity` 一次命中容量。

> 关于返回 `impl Iterator` 时为何可能需要额外生命周期：当迭代器借用了函数参数（例如借入一个 `&[T]`），返回类型里就要带上那个引用的生命周期，例如 `impl Iterator<Item = &T> + '_`。perf-book 把这部分留给了它链接的一篇博客，这里点到为止。

#### 4.2.4 代码实践

**实践目标**：把一个返回 `Vec` 的函数改写为返回 `impl Iterator`，并体会 `size_hint` 如何减少 `collect` 分配。

**操作步骤**（示例代码）：

1. 起点——一个返回 `Vec` 的函数：

```rust
fn evens_up_to(n: u32) -> Vec<u32> {
    (0..n).filter(|x| x % 2 == 0).collect()   // 必定分配
}
```

2. 改写为返回迭代器，**不**分配：

```rust
fn evens_up_to(n: u32) -> impl Iterator<Item = u32> {
    (0..n).filter(|x| x % 2 == 0)             // 这里没有 collect，无分配
}
```

3. 注意：`Range`（`0..n`）已经实现了 `ExactSizeIterator`，所以这条链上的 `size_hint` 是精确的。验证这一点——当调用者真的要 `collect` 时，分配次数应为 1 次（而非准倍多次）。你可以用 4.1.4 节的 `dhat-rs` 方法对比「`size_hint` 精确」与「故意返回 `(0, None)`」两种情况下 `collect` 的分配块数。

**需要观察的现象**：改写后，单纯调用 `evens_up_to(1_000_000)`（不 `collect`）应当**零分配**；即便调用者之后 `collect`，因为 `size_hint` 精确，也只需一次分配。

**预期结果**：返回 `impl Iterator` 的版本在「只遍历」场景下分配数为 0。

**待本地验证**：若你手写的自定义迭代器没有实现 `size_hint`，效果会退化，请用 `dhat-rs` 实测确认。

#### 4.2.5 小练习与答案

**练习 1**：下面两个函数签名，哪个对「只想遍历一次」的调用者更友好？为什么？

```rust
fn parse_tokens(s: &str) -> Vec<Token>;            // (a)
fn parse_tokens(s: &str) -> impl Iterator<Item = Token> + '_;  // (b)
```

**参考答案**：(b) 更友好。(a) 强制在函数内部 `collect` 出一个 `Vec`，调用者无论是否需要都得承担这次分配；(b) 把决定权交给调用者——想遍历就直接遍历（零分配），想物化成集合再 `collect`。注意 (b) 因为借用了 `s`，所以返回类型带了 `+ '_`。

**练习 2**：你写了一个自定义迭代器 `struct Counter { cur: usize, max: usize }`，它会从 `cur` 数到 `max`。为了让 `collect` 能一次命中容量，你应当实现什么？

**参考答案**：实现 `Iterator::size_hint`，返回 `(max - cur, Some(max - cur))`；更彻底的做法是直接实现 `ExactSizeIterator`（提供 `len`）。这样 `collect` 就能用 `with_capacity` 预分配，避免准倍重配。

### 4.3 `chain` 与 `filter_map` 的取舍

#### 4.3.1 概念说明

迭代器适配器（adapter）可以像积木一样组合：`map`、`filter`、`chain`、`filter_map` 等。它们大多「零开销」，但「零开销」不等于「没有差别」——在**热点**迭代器上，不同的组合方式会让编译器生成质量不同的机器码。

perf-book 在「Chaining」一节指出两点经验：

1. **`chain` 很方便，但可能比单个迭代器慢**，热点上能避开就避开。
2. **`filter_map` 可能比 `filter` 再接 `map` 更快**——把两步合成一步。

`chain` 把两个迭代器首尾相连（如 `a.iter().chain(b.iter())`）。它的潜在代价是：每产生一个元素，迭代器内部要判断「当前在第一段还是第二段」，这个分支与状态切换在某些场景下会阻碍优化。`filter_map` 之所以可能优于 `filter().map()`，是因为它把「过滤」和「映射」合并成一次闭包调用，少了一层适配器嵌套与中间传递。

> 注意本节的语气：perf-book 用的是「**可能**（may）」更慢/更快，而非「一定」。这正是全书的纪律——这些是**候选优化**，是否真有收益要靠基准测试（u2-l1）和必要时看机器码（见 4.4 节末尾）来确认。

#### 4.3.2 核心流程

两种组合的执行模型对照：

**`filter` 后接 `map`（两段适配器）：**
```
源元素 → filter 闭包(判断去留) → 通过 → map 闭包(变换) → 输出
```

**`filter_map`（一段适配器）：**
```
源元素 → filter_map 闭包(判断+变换，返回 Option) → Some → 输出 / None → 跳过
```

`filter_map` 的闭包返回 `Option<T>`：返回 `Some(v)` 表示「保留并映射成 v」，返回 `None` 表示「丢弃」。一次闭包调用同时完成「过滤」与「映射」，少了一层迭代器嵌套。

`chain` 的两段切换模型：

```
请求下一个元素 → 当前段还有？─Yes─▶ 取出并返回
                         └─No──▶ 切换到第二段 → 取出并返回
```

这个「当前在哪一段」的分支是 `chain` 潜在开销的来源。

#### 4.3.3 源码精读

关于 `chain`：

[src/iterators.md:37-38](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/iterators.md#L37-L38) ——`chain` 很方便，但可能比单个迭代器慢；对热点迭代器，能避开就避开。

关于 `filter_map`：

[src/iterators.md:41-42](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/iterators.md#L41-L42) ——类似地，`filter_map` 可能比「先 `filter` 再 `map`」更快。

perf-book 为这两条都附了真实 PR 示例链接（rust-lang/rust 仓库），说明它们都来自真实工程验证，而非凭空推断。

#### 4.3.4 代码实践

**实践目标**：在一段热点代码上比较 `filter().map()` 与 `filter_map`，体会「可能更快」是否成立。

**操作步骤**（示例代码）：

1. 写两个等价函数：

```rust
// 写法 A：filter 后接 map
fn squares_of_evens_a(v: &[i32]) -> Vec<i32> {
    v.iter().filter(|&&x| x % 2 == 0).map(|&x| x * x).collect()
}

// 写法 B：filter_map 一步到位
fn squares_of_evens_b(v: &[i32]) -> Vec<i32> {
    v.iter().filter_map(|&x| (x % 2 == 0).then(|| x * x)).collect()
}
```

2. 用 Criterion 或 Divan（u2-l1 介绍过的基准测试库）对二者在一份大数组上做对比基准测试。

**需要观察的现象**：多数情况下两者差不多（LLVM 常能把它们优化成同一份代码）；但在某些更复杂的闭包组合里，`filter_map` 会略快。

**预期结果**：差距通常很小；这正是 perf-book 说「**可能**更快」的原因。若测量显示无差异，说明这个候选优化在你的场景下不成立——这是正常且符合预期的结论。

**待本地验证**：请以本地基准测试结果决定是否采纳，不要假设一定有收益。

#### 4.3.5 小练习与答案

**练习 1**：把下面的 `filter().map()` 改写为等价的 `filter_map`。

```rust
nums.iter()
    .filter(|&&n| n > 0)
    .map(|&n| n as usize)
```

**参考答案**：

```rust
nums.iter().filter_map(|&n| (n > 0).then_some(n as usize))
```

`(n > 0).then_some(value)` 在条件为真时返回 `Some(value)`，否则返回 `None`，正好契合 `filter_map` 的语义。

**练习 2**：perf-book 说 `chain`「**可能**比单个迭代器慢」。这句话能不能理解成「任何时候都该避免 `chain`」？为什么？

**参考答案**：不能。「可能」意味着它是一个**值得在热点上审视的候选**，而非普适禁令。`chain` 在非热点代码、或可读性收益显著的场景下完全可以用；只有当剖析（u2-l2）显示某个 `chain` 处在热点、且基准测试证明它拖慢了速度时，才值得动手改写（例如手动合并两段逻辑成一个迭代器）。

### 4.4 `chunks_exact` 与 `copied`

#### 4.4.1 概念说明

本节讲两个「让 LLVM 生成更好代码」的小技巧，它们的共同点是：**不改语义，只改写法，从而给编译器更多优化空间**。

**`chunks` vs `chunks_exact`**：当你想把一个切片按固定大小切块时，有两个选择：

- `slice::chunks(size)`：切成若干块，**最后一块可能不足 `size`**。
- `slice::chunks_exact(size)`：只产出**恰好** `size` 大小的完整块，余下的零头通过 `ChunksExact::remainder()` 单独取出。

`chunks_exact` 之所以可能更快，是因为它的「主迭代」里每一块都**保证**恰好 `size` 个元素——编译器据此可以展开循环、向量化（SIMD）、并省去对最后一块的边界判断。代价是你得显式处理那个 `remainder`。

**`iter()` vs `iter().copied()`**：对一个装着小类型（如整数）的集合，`iter()` 给出的是 `&T`（引用），而 `iter().copied()` 给出的是 `T`（按值）。把小数据按值传递，常常让 LLVM 生成更好的代码（值可以放进寄存器，免去间接寻址）。perf-book 明确标注这是**进阶技巧**，效果需要通过检查机器码确认——这条与 u5-l4（Machine Code）相连。

#### 4.4.2 核心流程

设切片长度为 \(n\)，块大小为 \(k\)（\(k > 0\)）。

**`chunks(k)`：** 产出 \(\lceil n/k \rceil\) 块，最后一块长度为 \(n - k\lfloor n/k \rfloor\)（可能小于 \(k\)）。

**`chunks_exact(k)`：** 产出 \(\lfloor n/k \rfloor\) 块，每块恰好 \(k\) 个；余数 \(r = n \bmod k\) 通过 `remainder()` 单独取出。

\[
\text{完整块数} = \left\lfloor \frac{n}{k} \right\rfloor, \qquad
\text{余数} = n \bmod k
\]

`chunks_exact` 的优势在于：主循环里每块长度恒为 \(k\)，是一个**编译期可推断的不变量**，便于循环展开与向量化；而 `chunks` 每块长度 $\leq k$，最后一块不定，编译器保守地插入额外判断。

`copied` 的流程：

```
iter()       → 产出 &T（引用，可能要解引用、间接寻址）
iter().copied() → 产出 T（按值，小类型可直接进寄存器）
```

#### 4.4.3 源码精读

关于 `chunks_exact`：

[src/iterators.md:51-52](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/iterators.md#L51-L52) ——当块大小恰好整除切片长度时，用更快的 `chunks_exact` 取代 `chunks`。

即便不整除，也可以用 `chunks_exact` 配合 `remainder` 或手工处理零头：

[src/iterators.md:54-56](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/iterators.md#L54-L56) ——块大小不整除时，用 `chunks_exact` 配合 `ChunksExact::remainder` 或手工处理多余元素，仍可能更快。

perf-book 还列出了一批同族的「exact」变体（反向切块、可变切块等），规律完全一致：

[src/iterators.md:60-63](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/iterators.md#L60-L63) ——同样的取舍适用于 `rchunks`/`rchunks_exact`、`chunks_mut`/`chunks_exact_mut`、`rchunks_mut`/`rchunks_exact_mut` 及各自的 remainder 方法。

关于 `copied`：

[src/iterators.md:83-86](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/iterators.md#L83-L86) ——遍历整数等小类型集合时，用 `iter().copied()` 取代 `iter()`，消费方拿到的是按值的整数而非引用，LLVM 可能生成更好的代码。

perf-book 随即提醒这是进阶技巧，需要看机器码才能确认效果：

[src/iterators.md:90-92](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/iterators.md#L90-L92) ——这是进阶技巧，可能需要检查生成的机器码才能确认效果；详见 Machine Code 一章。

#### 4.4.4 代码实践

**实践目标**：把一处 `chunks` 换成 `chunks_exact`，并把一处 `iter()` 换成 `iter().copied()`，对比生成的代码。

**操作步骤**（示例代码）：

1. 起点——用 `chunks` 处理一个 `&[u8]`，每 4 字节求和：

```rust
fn chunk_sums_a(data: &[u8]) -> Vec<u32> {
    data.chunks(4).map(|c| c.iter().map(|&b| b as u32).sum()).collect()
}
```

2. 改写——用 `chunks_exact`（假设长度能被 4 整除，或单独处理余数），并用 `copied`：

```rust
fn chunk_sums_b(data: &[u8]) -> Vec<u32> {
    let mut out = Vec::with_capacity(data.len() / 4);
    for c in data.chunks_exact(4) {
        out.push(c.iter().copied().map(|b| b as u32).sum());
    }
    // 若 data.len() 不能被 4 整除，还需处理 data.chunks_exact(4).remainder()
    out
}
```

3. 用 [Compiler Explorer (godbolt.org)](https://godbolt.org/) 或 `cargo-show-asm`（u5-l4 会详细介绍）查看两个版本的汇编：观察 `chunk_sums_b` 的内层循环是否更规整、是否少了「最后一块不定长」的判断。

**需要观察的现象**：`chunks_exact` 版本的内层循环里，每块长度恒为 4，编译器更可能把它展开或向量化；`copied` 让 `b` 直接按值进入求和，少了一层解引用。

**预期结果**：`chunk_sums_b` 生成的汇编更紧凑。但 perf-book 已提醒 `copied` 的效果需要看机器码确认。

**待本地验证**：汇编差异因 Rust/LLVM 版本而异，请以本地 `cargo-show-asm` 或 Compiler Explorer 输出为准。

#### 4.4.5 小练习与答案

**练习 1**：一个长度为 10 的切片，按块大小 3 切分。分别用 `chunks(3)` 和 `chunks_exact(3)` 时，会产出哪些块？

**参考答案**：
- `chunks(3)`：4 块，长度分别为 3、3、3、1（最后一块不足）。
- `chunks_exact(3)`：3 个完整块（各 3 个元素），外加 `remainder()` 返回的 1 个余数元素。完整块数 \(\lfloor 10/3 \rfloor = 3\)，余数 \(10 \bmod 3 = 1\)。

**练习 2**：为什么 perf-book 把 `iter().copied()` 称为「进阶技巧」，并建议看机器码确认？

**参考答案**：因为 `copied` 的收益**不保证**——它取决于 LLVM 是否真能把按值的整数放进寄存器、省掉间接寻址。在简单循环里，`iter()` 和 `iter().copied()` 常被优化成同一份代码；只有检查生成的机器码才能确认 `copied` 是否真的带来了更优代码，否则可能只是「换了写法却没收益」。

## 5. 综合实践

把本讲四个模块串起来，做一个完整的重构练习（源码阅读 + 动手改写型实践）。

**任务背景**（示例代码）：下面这个函数综合了本讲讨论的多个反模式——返回 `Vec`、`collect` 后只遍历一次、用 `chunks` 而非 `chunks_exact`、用 `iter()` 而非 `iter().copied()`：

```rust
// 处理一批采样：每 4 个采样为一组，过滤掉含 0 的组，对剩下的组求和
fn process_samples(samples: &[u32]) -> Vec<u32> {
    let groups: Vec<&[u32]> = samples.chunks(4).collect();  // (1) collect 后只遍历一次
    groups.iter()                                            // (2) 返回 Vec 强迫物化
         .filter(|g| !g.iter().any(|&x| x == 0))            // (3) iter() 而非 copied
         .map(|g| g.iter().sum())                           // (4) iter() 而非 copied
         .collect()
}
```

**要求**：综合运用本讲知识逐步改写。

1. **消除中间 `Vec`（模块 4.1）**：去掉 `let groups = ...collect()`，直接对 `samples.chunks(4)` 链式处理，避免「collect 后只遍历一次」。
2. **优先 `chunks_exact`（模块 4.4）**：若业务上能保证 `samples.len()` 是 4 的倍数（或你能接受用 `remainder()` 处理余数），把 `chunks(4)` 换成 `chunks_exact(4)`，并说明对余数的处理。
3. **用 `copied`（模块 4.4）**：在判断与求和处把 `g.iter()` 改为 `g.iter().copied()`，让整数按值流动。
4. **可选——合并 filter/map（模块 4.3）**：如果改写后仍有 `filter(...).map(...)`，评估能否用 `filter_map` 合并。
5. **可选——返回迭代器（模块 4.2）**：如果调用者通常只遍历一次，考虑把签名改为 `impl Iterator<Item = u32> + '_`，并把决策权交还调用者；同时确认链上的迭代器是否提供了有用的 `size_hint`。

**预期产物**：一段更少分配、代码生成更友好的实现；并能口头说明每一处改动对应本讲哪条规则、为什么「可能」更快（以及为何需要实测确认）。

**待本地验证**：最终是否更快，请用 u2-l1 的基准测试与 u2-l2 的剖析确认；`copied` 的机器码收益请按 4.4 节方法检查。

## 6. 本讲小结

- **`collect` 是一次分配**：若收集出的集合只是又被遍历一次，就应当避免 `collect`；这是迭代器优化中最直接的一条。
- **`extend` 优于 `collect`+`append`**：往已有集合追加元素时，直接喂迭代器，省掉中间 `Vec` 的分配。
- **返回 `impl Iterator` 比返回 `Vec` 更友好**：它把「要不要物化」的决定权交给调用者，常能从源头消灭一次分配。
- **`size_hint` / `ExactSizeIterator` 让 `collect` 少分配**：提前告知元素数量，`collect` 即可一次 `with_capacity` 命中容量，跳过准倍重配序列。
- **`chain`、`filter_map` 在热点上值得审视**：`chain` 可能比单个迭代器慢，`filter_map` 可能比 `filter().map()` 快——但都是「可能」，需实测。
- **`chunks_exact` 与 `copied` 是代码生成优化**：前者保证每块定长、利于向量化；后者让小类型按值流动、利于寄存器分配；其中 `copied` 是进阶技巧，效果要看机器码确认。

## 7. 下一步学习建议

- **本讲的「`copied` 需要看机器码」直接指向 u5-l4（Machine Code）**：学会用 Compiler Explorer 与 `cargo-show-asm` 查看 Rust 生成的汇编后，回头验证本讲的 `chunks_exact`、`copied`、`filter_map` 改动是否真的改变了机器码。
- **`collect`/`extend` 的分配话题，可与 u5-l2（I/O——锁与缓冲）合读**：那一章里 perf-book 用「按行读文件」展示了 `BufRead::lines`（每行一个 `String`，逐次分配）与 `read_line` + workhorse `String`（少量分配）的对比，与本讲的「避免逐次分配」是同一个思想在 I/O 领域的体现（参见 [src/heap-allocations.md:388-389](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md#L388-L389)）。
- **若想进一步消除边界检查**：本讲的迭代器改写常与「帮助编译器消除 bounds check」相伴，下一站可学 u4-l4（Bounds Checks）。
- **建议精读的源码**：`src/iterators.md` 全文（本讲的主体）以及 `src/heap-allocations.md` 的 `Vec`、Reusing Collections、Reading Lines 三节，把「迭代器写法 ↔ 分配代价」的因果链彻底打通。
