# General Tips——通用性能原则

## 1. 本讲目标

前面五个单元（u2–u5）讲的都是 **Rust 特有的**性能技巧：构建配置、Clippy、堆分配、类型体积、哈希、迭代器、边界检查、内联、I/O、包装类型、机器码……本讲是「更高层次的性能原则」单元（u6）的开篇，它把镜头拉远，讲一套**与语言无关的通用性能纪律**。

perf-book 的「General Tips」章开宗明义：

> The previous sections of this book have discussed Rust-specific techniques. This section gives a brief overview of some general performance principles.

也就是说，这些原则放到 C、C++、Java 里同样成立。它们不是新工具，而是**把前面所有技巧串起来的「决策框架」**——告诉你该不该优化、优化哪里、按什么顺序优化。

学完后你应当能够：

- 理解**为什么只值得优化热点代码**，并能用 **Amdahl 定律**把这条直觉讲成一条公式。
- 掌握**算法与数据结构的改进优先于低层优化**这条最高优先级原则，避免「在错误的算法上做精细打磨」。
- 建立**缓存友好**与**分支预测**的直觉，知道什么样的代码「顺」现代硬件、什么样的代码「别扭」。
- 学会一组以「测量分布」为核心的实战技巧：**特判常见的小规模输入、按频率排序处理、用紧凑表示 + 备用表压缩重复数据、加小缓存、惰性求值**。

一句话定位：本讲是 perf-book 的「心法总纲」——它假设你已经会用手里的工具（剖析器、基准测试、各种写法），现在教你在**该出手时出手、该收手时收手**。

## 2. 前置知识

本讲默认你已读过：

- **u2-l1 Benchmarking / u2-l2 Profiling**：知道「热点」是剖析定位出的、执行频率高到足以影响运行时间的代码；知道优化前后要用基准测试比较。本讲反复强调的「只优化热点」「测量后再动手」全部建立在这两讲之上。
- **u2-l3 Build Configuration**：知道 release 构建是最易被忽视却收益最高的单项改动。General Tips 章第一句就提醒「先把明显陷阱避开，例如用了非 release 构建」，链接正指向 build-configuration 章。
- **u3-l1 堆分配 / u3-l3 类型体积**：本讲讲到「缓存友好」时会用到类型体积与内存布局的直觉（大于 128 字节会被 `memcpy`、字段重排减少 padding）。

补充一个贯穿本讲、但前面没正式给过的概念——**「优化是要付代价的」**：优化后的代码通常比未优化的代码**更复杂、更难写、更难读、更难维护**。这条事实是本讲第一条原则（「只优化热点」）的全部理由。如果你只记一句话，就记这句：**性能是用可读性、可维护性和开发时间换来的，所以只该花在真正热的地方。**

> 术语对照：本讲的「热点（hot code）」与 u2-l2 的定义一致；「低层优化（low-level optimization）」指不改变算法复杂度、只在常数因子上做文章的改动（如换 hasher、消除一次边界检查）。

---

## 3. 本讲源码地图

本讲涉及的「源码」就是 perf-book 的两个 Markdown 章节：

| 文件 | 作用 |
| --- | --- |
| [src/general-tips.md](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/general-tips.md) | 本章主体：用十几条短句给出通用性能原则——只优化热点、算法/数据结构优先、缓存/分支友好、特判小规模、按频率处理、小缓存、惰性求值、注释习惯。 |
| [src/build-configuration.md](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/build-configuration.md) | General Tips 章开篇「避开明显陷阱（如非 release 构建）」一句的链接落点；其中「Release Builds」一节解释 release 相对 dev 常有 10–100× 加速。 |

注意 perf-book 是一本用 mdBook 写的在线书，它的「源码」就是这些 Markdown 文稿（见 u1-l1）。本讲引用的代码块大多是为了练习而新写的，凡是不是书稿原文的，都会明确标注为「示例代码」。

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. 只优化热点代码（含 Amdahl 定律、加速的两个杠杆）。
2. 算法与数据结构优先。
3. 缓存友好与分支预测。
4. 特判常见小规模与按频率处理（含紧凑表示、小缓存、惰性求值）。

### 4.1 只优化热点代码

#### 4.1.1 概念说明

General Tips 章用一句话立下全书最重要的纪律：

> Optimized code is often more complex and takes more effort to write than unoptimized code. For this reason, it is only worth optimizing hot code.

这段话的逻辑链是：

1. 优化后的代码更复杂、更费工；
2. 复杂性是一种持续的成本（更难读、更难改、更容易出 bug）；
3. 所以优化的收益必须足够大，才对得起这份成本；
4. 而「收益足够大」的地方，就是**热点**——执行频率高到足以影响运行时间的代码（承接 u2-l2）。

把这条直觉量化，就是经典的 **Amdahl 定律（Amdahl's Law）**。假设程序总执行时间为 \(T\)，其中占比为 \(p\)（\(0 \le p \le 1\)）的部分被你优化、获得了 \(s\) 倍加速（即那部分时间从 \(pT\) 变成 \(pT/s\)），则整体加速比为：

\[
S_{\text{overall}} = \frac{T}{(T - pT) + pT/s} = \frac{1}{(1-p) + p/s}
\]

两个关键推论：

- 即使把热点**无限加速**（\(s \to \infty\)），整体加速比上限也只有 \(\displaystyle S_{\max} = \frac{1}{1-p}\)。一个占比 \(p = 5\%\) 的冷代码，哪怕你让它快到「不花时间」，整体最多也只快 \(1/0.95 \approx 1.05\) 倍——**优化冷代码的天花板很低**。
- 反之，占比 \(p = 80\%\) 的热点，哪怕只加速 \(s = 2\)，整体就有 \(1/(0.2 + 0.4) \approx 1.67\) 倍——**优化热点的地板很高**。

所以「只优化热点」不是经验之谈，而是 Amdahl 定律的直接结论。

但 General Tips 章紧接着补了一句很现实的安慰：

> Most optimizations result in small speedups. Although no single small speedup is noticeable, they really add up if you can do enough of them.

——单次优化的收益往往很小，但积少成多。这两句并不矛盾：**要优化热点（而非冷代码），而且对单个热点的优化不要期望过高，要多积累几处**。

#### 4.1.2 核心流程

当剖析（u2-l2）告诉你某个函数是热点时，General Tips 章给出加速它的**两条根本路径**：

> When profiling indicates that a function is hot, there are two common ways to speed things up: (a) make the function faster, and/or (b) avoid calling it as much.

把这两条路径画清楚很重要，因为后面所有技巧都能归到其中之一：

```
发现热点函数 F（u2-l2 剖析）
        │
        ├── 路径 (a)：让 F 本身更快
        │     ├─ 换算法 / 换数据结构（模块 4.2，收益最大）
        │     ├─ 减少分配 / 减小类型（u3 单元）
        │     ├─ 消除边界检查 / 内联（u4-l4 / u5-l1）
        │     └─ 缓存友好 / 分支友好（模块 4.3）
        │
        └── 路径 (b)：少调用 F
              ├─ 惰性求值：能不算就不算（模块 4.4）
              ├─ 加小缓存：算过就存（模块 4.4）
              └─ 特判小规模：常见情况走快路径，绕开 F（模块 4.4）
```

General Tips 章还给了两条务实提醒：

1. **「消灭愚蠢的减速」比「发明聪明的加速」更容易**（It is often easier to eliminate silly slowdowns than it is to introduce clever speedups）。意思是：先把那些「白白浪费」的东西去掉（多余分配、重复锁、不必要的计算），往往比苦思冥想一个精巧优化更划算、也更安全。
2. **不同的剖析器各有长短，最好多用一个**（Different profilers have different strengths. It is good to use more than one.）。这呼应 u2-l2 讲过的：CPU 热点用 perf/samply、分配用 DHAT、指令数用 Cachegrind——它们看到的是不同的「真相」，交叉印证才不会漏掉热点。

#### 4.1.3 源码精读

「只优化热点」这条总纲见 [src/general-tips.md:13-14](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/general-tips.md#L13-L14)，它把「优化有代价」与「只优化热点」一句话连起来：

> Optimized code is often more complex and takes more effort to write than unoptimized code. For this reason, it is only worth optimizing hot code.

「小加速会累积」见 [src/general-tips.md:25-26](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/general-tips.md#L25-L26)：

> Most optimizations result in small speedups. Although no single small speedup is noticeable, they really add up if you can do enough of them.

加速热点的「两条路径」见 [src/general-tips.md:30-32](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/general-tips.md#L30-L32)，措辞是「(a) make the function faster, and/or (b) avoid calling it as much」。这两条路径覆盖了本书后续几乎所有章节。

「消灭愚蠢减速优先」见 [src/general-tips.md:34-35](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/general-tips.md#L34-L35)；「多用一个剖析器」见 [src/general-tips.md:28](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/general-tips.md#L28)。

最后，General Tips 章开篇还埋了一个「前置陷阱」提醒，链接指向 build-configuration 章：

[src/general-tips.md:6-11](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/general-tips.md#L6-L11)

> As long as the obvious pitfalls are avoided (e.g. using non-release builds), Rust code generally is fast and uses little memory.

这句的意思是：在谈任何高阶优化之前，先确认你没踩「用了 dev 构建」这种**一票否决级**的低级坑。dev 构建不做优化、保留 debug 断言与整数溢出检查，相对 release 常有 10–100× 的差距（见 [src/build-configuration.md:52-57](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/build-configuration.md#L52-L57)）。这是「消灭愚蠢减速」的终极案例：一行 `--release` 抵得上一整章微优化。

#### 4.1.4 代码实践（确认热点，并说出两条路径）

这是一个**剖析型 + 思辨型**实践，用来把「只优化热点」和「两条路径」从口号变成肌肉记忆。

**实践目标**：用剖析器在一个小程序里定位出唯一的真热点，并用两条路径重新表述它。

**操作步骤**：

1. 写一个**示例程序**，故意制造「一处热、多处冷」：

   ```rust
   // 示例代码：src/main.rs
   fn slow_hash(s: &str) -> u64 {
       // 故意低效：O(n^2) 地反复遍历
       let mut h = 0u64;
       for _ in 0..100 {
           for b in s.bytes() {
               h = h.wrapping_mul(31).wrapping_add(b as u64);
           }
       }
       h
   }

   fn setup() -> Vec<String> {
       // 冷代码：只跑一次
       (0..16).map(|i| format!("item-{i}")).collect()
   }

   fn main() {
       let data = setup();                       // 冷：1 次
       let mut acc = 0u64;
       for w in &data {
           for _ in 0..100_000 {                 // 热：百万级
               acc ^= slow_hash(w);
           }
       }
       println!("{acc}");
   }
   ```

2. 确认你用的是 release 构建（避开 General Tips 章开篇点名的陷阱）：

   ```bash
   cargo build --release
   ```

3. 用 u2-l2 的某个剖析器跑一遍，例如 `samply`（CPU 采样火焰图）：

   ```bash
   cargo install samply   # 一次性安装
   samply record ./target/release/<crate名>
   ```

**需要观察的现象**：火焰图里 `slow_hash` 应当占据绝大部分样本，`setup` 与 `main` 的循环骨架几乎不可见——这就是「热点高度集中在 `slow_hash`」。

**预期结果**：你能指着火焰图说出「`slow_hash` 是热点」，并能把它拆成两条路径：  让 `slow_hash` 本身更快（例如去掉那层 ×100 的冗余循环）； 让它被少调用（例如把 `slow_hash(w)` 的结果提到内层循环外，因为 `w` 在内层不变——这正是「避免重复计算」）。

**待本地验证**：采样工具与样本量不同，火焰图形态略有差异；若手头无 `samply`，可改用 `perf record`/`cargo flamegraph`，结论一致即可。

#### 4.1.5 小练习与答案

**练习 1**：某段代码占程序总运行时间的 4%，你花一周把它加速了 10 倍。用 Amdahl 定律算整体加速比，并据此评价这一周花得值不值。

**参考答案**：\(p = 0.04,\ s = 10\)，\(S = 1/((1-0.04) + 0.04/10) = 1/(0.96 + 0.004) \approx 1.04\)。整体只快约 4%。即便把它加速到「无穷快」，上限也只有 \(1/0.96 \approx 1.04\) 倍。结论：**优化占比 4% 的代码天花板极低，一周的投入基本打水漂**——这正是「只优化热点」的定量依据。

**练习 2**：General Tips 章说加速热点有「让函数更快」和「少调用它」两条路径。u5-l2 讲的「循环前先 `stdout().lock()`」属于哪一条？u4-l1 讲的「把 `HashMap` 换成 `FxHashMap`」又属于哪一条？

**参考答案**：「锁 stdout」是把每次 `println!` 的锁开销从 n 次摊还到 1 次，属于**让函数（`println!`/输出路径）更快**（路径 a），更准确地说是降低单次调用的固定开销。换 `FxHashMap` 是降低每次查询的哈希计算成本，也属于**让函数更快**（路径 a）。两者都没改变「调用次数」。想让一个查询「少调用」，典型手段是**缓存**（模块 4.4 的小缓存）或**惰性求值**。

---

### 4.2 算法与数据结构优先

#### 4.2.1 概念说明

General Tips 章给出全书**最高优先级**的一条优化原则：

> The biggest performance improvements often come from changes to algorithms or data structures, rather than low-level optimizations.

这条原则把所有优化分成两个层级：

- **复杂度层级（algorithm / data structure）**：改变「随着输入变大，工作量怎么增长」这一根本关系，即 Big-O。例如线性查找 \(O(n)\) 改成哈希查找 \(O(1)\)、\(O(n^2)\) 排序改成 \(O(n \log n)\)。
- **常数因子层级（low-level）**：不改 Big-O，只在「每次操作快多少」上做文章。本书大部分章节（换 hasher、消除边界检查、内联、缓存友好）都属于这一层。

关键直觉：**一个错误的算法，再怎么打磨常数因子也救不回来**。线性扫描一个百万元素的列表，换成 SIMD 加速也还是 \(O(n)\)；而换成 `HashSet` 是 \(O(1)\)，无论常数多小都赢。所以优化任何热点前，先问一句：**它的算法和数据结构选对了吗？** 如果没选对，先改这里——这是唯一可能带来「数量级」提升的层级。

perf-book 给这条原则配了两个真实示例链接（rustc 自身的 PR），都是靠**换数据结构**而非微优化拿到大幅提速的案例。

#### 4.2.2 核心流程

把「算法优先」落成一道决策闸门，放在每次优化的最前面：

```
确认热点函数 F（模块 4.1）
        │
        ▼
【第一问】F 的算法复杂度对吗？
   ├─ 不对（如 O(n^2)、本可 O(n log n)）→ 换算法（收益数量级）
   ├─ 对，但数据结构选错（如该用哈希却用线性表）→ 换数据结构（收益数量级）
   └─ 都对，复杂度已最优
        │
        ▼
【第二问】常数因子还能优化吗？
   └─ 这才轮到本书其余章节：减分配 / 消边界检查 / 内联 / 缓存友好 …（收益有限但可累积）
```

一个常被忽视的判断点：**复杂度的优劣取决于「输入规模」**。处理 5 个元素时，\(O(n^2)\) 的简单代码可能比 \(O(n \log n)\) 但常数大的代码更快；处理 5 万个元素时则相反。这正是模块 4.4「特判小规模」要承接的点——**大输入用对数复杂度，小输入用简单直接**，二者靠「测量分布」来衔接。

#### 4.2.3 源码精读

「算法与数据结构优先」原则见 [src/general-tips.md:16-19](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/general-tips.md#L16-L19)：

> The biggest performance improvements often come from changes to algorithms or data structures, rather than low-level optimizations.
> [**Example 1**]…, [**Example 2**]….

这两个 Example 链接到 rust-lang/rust 的真实 PR。perf-book 的写作惯例是：凡通用原则都配 rustc 自身的真实案例作为佐证（见 u1-l1 讲过的「广度优先、附真实 PR」风格）。读这些链接不是为了复现它们，而是建立信心：**「换数据结构带来大幅提速」不是理论，是 rustc 这种已高度优化过的代码里反复发生过的事**。

值得对照的是，本书其余章节几乎都在「第二问」里打转：u4-l1 换 hasher、u4-l4 消边界检查、u5-l1 内联——它们都是「常数因子优化」。General Tips 章这一句相当于给全书排了个序：**先问算法/数据结构，再用那些常数因子技巧锦上添花。**

#### 4.2.4 代码实践（换数据结构 vs 微优化同一个坏算法）

这是一个**对比型**实践，直观感受「数量级提升」与「常数因子提升」的差距。

**实践目标**：在同一个「去重」任务上，对比「微优化一个坏算法」与「换成对的数据结构」的收益差距。

**操作步骤**：

1. 写两个版本的**示例代码**，都是「统计一个随机数列表中的不同值的个数」：

   ```rust
   // 示例代码：版本 A —— O(n^2)，但已经做了「微优化」（用 swap_remove、用 FxHasher 的思路）
   pub fn distinct_count_a(data: &[u32]) -> usize {
       let mut seen: Vec<u32> = Vec::new();
       for &x in data {
           if !seen.iter().any(|&y| y == x) {   // 线性查找，O(n)
               seen.push(x);
           }
       }
       seen.len()
   }

   // 示例代码：版本 B —— 换数据结构：Vec → HashSet，O(n) 整体
   pub fn distinct_count_b(data: &[u32]) -> usize {
       use std::collections::HashSet;
       let seen: HashSet<u32> = data.iter().copied().collect();
       seen.len()
   }
   ```

2. 用一组大小递增的随机输入（如 1k、1 万、10 万、100 万个元素）分别跑两个版本，用 u2-l1 的基准测试（Criterion 或 Hyperfine）计时。

**需要观察的现象**：输入变大时，版本 A 的耗时**平方级**上涨（10 万→100 万，约慢 100 倍），版本 B 近似**线性**上涨（约慢 10 倍）。两条曲线在双对数坐标下斜率截然不同（A≈2，B≈1）。

**预期结果**：在 100 万元素量级，版本 B 比版本 A 快成百上千倍——**换数据结构带来的提升，是任何常数因子微优化都追不上的数量级**。

**待本地验证**：具体倍数依赖输入分布与机器；关键是观察**两条曲线随规模增长的斜率差异**，而非某个绝对数字。

#### 4.2.5 小练习与答案

**练习 1**：你发现一个热点函数是「在一个 `Vec` 里线性查找某个键」，于是按 u4-l1 把默认 hasher 换成了 `FxHash`、按 u5-l1 给查找函数加了 `#[inline]`。结果几乎没变快。最可能的原因是什么？

**参考答案**：你优化的是**常数因子**，但瓶颈是**算法复杂度**——线性查找是 \(O(n)\)。换 hasher、内联都改变不了「每次查找都要遍历整个 Vec」这一事实。正确做法是先问第一问：把 `Vec` 换成 `HashSet`/`BTreeSet`，把查找从 \(O(n)\) 降到 \(O(1)\) 或 \(O(\log n)\)。这就是「算法与数据结构优先于低层优化」。

**练习 2**：为什么说「复杂度的优劣取决于输入规模」？这对模块 4.4 的「特判小规模」有什么启示？

**参考答案**：Big-O 描述的是增长趋势，但常数因子在小规模下起决定作用——\(O(n^2)\) 处理 5 个元素可能比 \(O(n \log n)\)（常数大）更快。启示是：不必对**所有**输入都用对数复杂度的高级数据结构；当测量表明小规模输入占多数时，可以**对常见的小输入特判走简单路径、对罕见的大输入才落到复杂度更优的结构**（4.4 详述）。这正是把模块 4.2 与 4.4 衔接起来的桥梁。

---

### 4.3 缓存友好与分支预测

#### 4.3.1 概念说明

General Tips 章在算法之后、给出一条关于硬件的笼统但重要的建议：

> Writing code that works well with modern hardware is not always easy, but worth striving for. For example, try to minimize cache misses and branch mispredictions, where possible.

这句点到为止，但「缓存」和「分支预测」是理解现代 CPU 性能的两块基石。本书没有展开（广度优先、深度靠外链，见 u1-l1），本讲补充建立直觉。

**缓存（cache）与缓存未命中（cache miss）**：CPU 访问内存极慢（相对寄存器），于是在 CPU 与内存之间架了几级 **缓存（L1/L2/L3）**。缓存以**缓存行（cache line，通常 64 字节）**为单位搬运数据。访问一个已在缓存里的字节，几十个周期就够；访问一个不在缓存里的字节，要先从内存搬一整行进来，要几百个周期——这就是**缓存未命中**的代价。因此：

- **顺序访问**一整片连续内存，第一个字节未命中、之后整行都在缓存里，摊下来很便宜；
- **随机 / 大步长（strided）访问**，每次跳到一个新地方都可能未命中，很贵。

这直接联系 u3-l3：**类型体积小、字段紧凑，意味着一个缓存行能装下更多元素**，遍历时未命中更少。这就是「缓存友好」。

**分支预测（branch prediction）与预测失败（misprediction）**：现代 CPU 用流水线，遇到 `if` 时不会停下来等条件算完，而是**猜**一个方向继续往下执行（分支预测）。猜对了几乎免费；猜错了要**冲刷流水线**、回退重来，付出**预测失败惩罚（misprediction penalty）**。一次分支的期望代价约为：

\[
\text{期望代价} = (\text{预测失败率}) \times (\text{失败惩罚})
\]

所以：

- **高度可预测的分支**（如几乎总是走同一边）几乎免费；
- **高度不可预测的分支**（如 50/50 随机数据上的判断）每次都可能罚一笔，累积起来很贵。

注意：这与 u4-l4 讲的「边界检查」是两回事——边界检查之所以便宜，不仅因为 LLVM 常能消除它，还因为它**极度可预测**（几乎总是不越界）；本模块关心的是**业务逻辑里的**不可预测分支。

#### 4.3.2 核心流程

把「缓存友好」与「分支友好」落成可操作的清单：

**缓存友好的写法**：

1. **数据紧凑**：用更小的整数、装箱罕见大变体（u3-l3），让一个缓存行装更多元素。
2. **顺序访问**：优先用 `Vec`/切片的连续遍历（`for x in &vec`），而非在 `HashMap`、链表、指针链上跳跃。
3. **数据布局匹配访问模式**：频繁一起访问的字段放一起；如果遍历时只用其中一两个字段，可考虑把「热点字段」单独拆成一个紧凑数组（结构数组 SoA 思路），避免把整条大记录都搬进缓存。
4. **少间接**：`Vec<T>` 比 `Vec<Box<T>>` 缓存友好——后者每次都要跳一次指针。

**分支友好的写法**：

1. **让常见情况可预测**：把最常见的分支写在前、让它倾向「总是走这边」。
2. **对可预测性差的数据先排序**：若必须对一批数据做条件判断，**先把数据排好序**，让连续的数据大量命中同一分支，把「随机 50/50」变成「一段全真、一段全假」——失败率骤降。
3. **必要时无分支化（branchless）**：用位运算替代 `if`（如 `condition as usize` 把布尔转成 0/1 再参与算术），消除分支本身。但这是进阶技巧，须用机器码与基准测试验证（承接 u5-l4）。

把两条合起来看：**「顺序、紧凑、可预测」的代码顺现代硬件；「随机、稀疏、不可预测」的代码别扭**——这正是 General Tips 章那句「works well with modern hardware」的具体含义。

#### 4.3.3 源码精读

「缓存与分支」这条建议见 [src/general-tips.md:21-23](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/general-tips.md#L21-L23)：

> Writing code that works well with modern hardware is not always easy, but worth striving for. For example, try to minimize cache misses and branch mispredictions, where possible.

注意措辞「where possible（在可能的地方）」与「not always easy（不容易）」——General Tips 章对缓存/分支的态度是**值得追求，但承认它难、且不保证每次都做得到**。这与它对其他原则（如「只优化热点」是硬纪律）的笃定语气不同：缓存/分支优化更依赖具体场景，需要测量来确认收益。

本书把「缓存友好」的**具体手段**分散在各章而非集中讲：缩小类型体积在 [src/type-sizes.md](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/type-sizes.md)（u3-l3）、用 `chunks_exact` 让循环连续可向量化在 [src/iterators.md](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/iterators.md)（u4-l3）。General Tips 章这一句相当于给这些散落的技巧提供了一个共同的「为什么」。

#### 4.3.4 代码实践（亲眼看见分支预测的代价）

这是一个**经典演示**实践，能让你在几秒钟内直观看到「不可预测分支」有多贵。

**实践目标**：对比「对一批数据做条件求和」在**随机排列**与**排好序**两种输入下的耗时差异，体会分支预测失败惩罚。

**操作步骤**：

1. 写一段**示例代码**：

   ```rust
   // 示例代码：src/main.rs
   use std::time::Instant;

   fn conditional_sum(data: &[u32], threshold: u32) -> u64 {
       let mut s = 0u64;
       for &x in data {
           if x >= threshold {        // 这就是那个分支
               s += x as u64;
           }
       }
       s
   }

   fn main() {
       let n = 50_000_000;
       // 用一个简单的伪随机序列填充（不依赖外部 crate）
       let mut data: Vec<u32> = Vec::with_capacity(n);
       let mut state = 0x1234_5678u32;
       for _ in 0..n {
           state = state.wrapping_mul(1664525).wrapping_add(1013904223);
           data.push(state);
       }
       let threshold = u32::MAX / 2;   // 约一半数据 >= 阈值 → 分支最难预测

       // 情形 A：随机顺序
       let t = Instant::now();
       let s1 = conditional_sum(&data, threshold);
       let d1 = t.elapsed();

       // 情形 B：先排序，让 < 阈值的连续在前、>= 阈值的连续在后
       let mut sorted = data.clone();
       sorted.sort_unstable();
       let t = Instant::now();
       let s2 = conditional_sum(&sorted, threshold);
       let d2 = t.elapsed();

       println!("random  sum={s1} time={d1:?}");
       println!("sorted  sum={s2} time={d2:?}");
   }
   ```

2. 用 release 构建、关闭调试输出后运行：

   ```bash
   cargo run --release
   ```

**需要观察的现象**：`conditional_sum` 函数体完全一样、`s1 == s2`（结果相同），但随机输入的耗时通常**数倍于**排序后的输入。差距全部来自 `if x >= threshold` 这个分支：随机数据上它约 50/50，预测失败率最高；排序后数据上它「先全假后全真」，几乎不失败。

**预期结果**：两条数据相加结果相同，但 random 的耗时显著大于 sorted。这就是分支预测失败惩罚的可视化。

**待本地验证**：具体倍数因 CPU、数据规模而异（现代 CPU 有的有更复杂的预测器，差距会缩小）；若差距不明显，可把阈值调到更接近 50% 分界、或增大 `n`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Vec<T>` 的遍历通常比 `Vec<Box<T>>` 的遍历更缓存友好？

**参考答案**：`Vec<T>` 把元素**紧密连续**地放在一起，遍历时一个 64 字节缓存行能装下多个元素，命中率高。`Vec<Box<T>>` 存的是指针，每个 `T` 散落在堆上各处；遍历时每解引用一个 `Box` 都可能跳到一个全新的缓存行，触发缓存未命中。连续布局对缓存友好、指针链对缓存不友好。

**练习 2**：General Tips 章在讲缓存/分支时用了「worth striving for」「where possible」这样较软的措辞，而讲「只优化热点」时用的是硬性纪律。为什么语气不同？

**参考答案**：「只优化热点」有 Amdahl 定律的硬性支撑，违反它必定低效，所以是铁律。而缓存/分支优化**收益高度依赖具体场景**（数据布局、访问模式、CPU 微架构），且常常与可读性冲突、需要测量才能确认收益，所以 perf-book 把它定位为「值得追求但不强求、且不保证每次都能做到」的方向性建议，而非一刀切的规则。

---

### 4.4 特判常见小规模与按频率处理

#### 4.4.1 概念说明

这是本讲最实战的一个模块，也是规格要求的主实践任务所在。General Tips 章给了一组互相关联的技巧，核心思想是同一个：**用测量出来的「输入分布」来给最常见的输入开快路径**。

**技巧一：特判常见的小规模输入。**

> Complex general cases can often be avoided by optimistically checking for common special cases that are simpler. In particular, specially handling collections with 0, 1, or 2 elements is often a win when small sizes dominate.

许多处理集合的通用代码，在输入只有 0/1/2 个元素时会被「通用机制」拖累（循环开销、迭代器状态、空检查）。如果测量表明**小规模输入占多数**，可以在函数开头加一条快路径：直接处理 0/1/2 个元素的情况，绕开通用逻辑。perf-book 给了四个真实 PR 示例，都是 rustc 里这类「特判小集合」带来提速的案例。

**技巧二：按频率排序，先处理最常见的情况。**

> When code deals with multiple cases, measure case frequencies and handle the most common ones first.

`match` / `if-else` 链里，把**最常命中**的分支放最前面，既让控制流更可预测（承接 4.3 的分支友好），也让常见情况少走几步判断。前提是**先测量**频率，而不是拍脑袋猜。

**技巧三：紧凑表示 + 备用表（轻量数据压缩）。**

> When dealing with repetitive data, it is often possible to use a simple form of data compression, by using a compact representation for common values and then having a fallback to a secondary table for unusual values.

当数据高度重复（某个值出现极多），可以给常见值用一个小而紧凑的表示，罕见的值才落到一张备用表里查。这是一种「用空间换命中率」、本质是缓存友好的压缩。

**技巧四：高局部性的查找前加小缓存。**

> When dealing with lookups that involve high locality, it can be a win to put a small cache in front of a data structure.

如果查找有**局部性**（同一个键近期很可能再被查），在一个大结构（如大 `HashMap`）前面加一个极小的缓存（甚至只缓存「上一次的键值」），就能挡掉大量重复查找——这正是 4.1 里「路径：少调用」的典型手段。

**技巧五：惰性 / 按需计算。**

> Avoid computing things unless necessary. Lazy/on-demand computations are often a win.

能不算就不算。这呼应 u4-l2 讲过的 `ok_or_else`（惰性求值替代 `ok_or`）——只在真正需要时才付出计算代价。

把这五条合起来看，它们都是 **4.1 中「路径：少调用 F」** 的不同招式，共同的方法论是：**先测量输入的分布（多少种情况、各占多少、规模多大、是否有局部性），再据分布把最常见的输入走最短的快路径**。

#### 4.4.2 核心流程

把「按分布开快路径」落成一套可复用的流程：

```
找到一个处理集合/多种情况的通用函数 F
        │
        ▼
【测量分布】（关键前提！）
   ├─ 元素数量分布：0/1/2/大 各占多少？（可用 counts 工具或 eprintln 直方图，承接 u3-l2）
   ├─ 分支频率：各 case 命中率多少？
   └─ 局部性：同一键是否短期内重复查找？
        │
        ▼
据分布定制快路径
   ├─ 小规模主导 → 特判 0/1/2 个元素
   ├─ 某个 case 最常见 → 把它放 match/if 最前
   ├─ 某值极度重复 → 紧凑表示 + 备用表
   ├─ 查找有局部性 → 前置小缓存
   └─ 有计算可推迟 → 改惰性求值
        │
        ▼
基准测试验证收益（u2-l1）；并给快路径写注释（模块结尾强调）
```

一个直觉公式支撑「先处理最常见情况」：设快路径覆盖的输入占比为 \(f\)，通用路径耗时为 \(T\)、快路径耗时为 \(T/r\)（\(r>1\) 为加速倍数），则期望耗时为：

\[
\text{期望} = f \cdot \frac{T}{r} + (1-f) \cdot T
\]

\(f\) 越大（常见情况占比越高），收益越大。所以**特判只值得为「占比高」的情况做**——这正是为什么必须先测量分布。

> 收尾提醒（贯穿全书）：优化后的代码「结构往往不再一目了然」，所以**注释很值钱**，尤其是**引用了剖析测量数据的注释**。General Tips 章原话举例：一句「99% 的情况下这个 vector 只有 0 或 1 个元素，所以先处理这两种」就能让后续读者豁然开朗。给快路径写注释，是这一模块不可省的一步。

#### 4.4.3 源码精读

「特判常见小规模」见 [src/general-tips.md:42-52](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/general-tips.md#L42-L52)，其中点名的典型场景是「specially handling collections with 0, 1, or 2 elements is often a win when small sizes dominate」，并配了四个 rustc PR 示例：

> Complex general cases can often be avoided by optimistically checking for common special cases that are simpler. … In particular, specially handling collections with 0, 1, or 2 elements is often a win when small sizes dominate.

「紧凑表示 + 备用表」见 [src/general-tips.md:54-59](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/general-tips.md#L54-L59)，把它定性为「a simple form of data compression」。

「按频率处理、最常见的先处理」见 [src/general-tips.md:61-62](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/general-tips.md#L61-L62)：「measure case frequencies and handle the most common ones first」。注意「measure（测量）」是前置动词——不测量就排序等于瞎排。

「小缓存」见 [src/general-tips.md:64-65](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/general-tips.md#L64-L65)，前提限定为「lookups that involve high locality」——没有局部性的查找加缓存没用。

「惰性求值」见 [src/general-tips.md:37-40](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/general-tips.md#L37-L40)：「Avoid computing things unless necessary」。

最后，「优化代码要有引用剖析数据的注释」见 [src/general-tips.md:67-70](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/general-tips.md#L67-L70)，并给了那句经典注释示范：「99% of the time this vector has 0 or 1 elements, so handle those cases first」。它正好与本模块的「特判小规模」呼应——你为 0/1/2 写的快路径，正是「结构不再显然」的代码，最需要这种注释。

#### 4.4.4 代码实践（测量分布 → 特判 0/1/2 → 对比性能）

这是本讲的**核心实践**，对应规格里的主实践任务。

**实践目标**：亲手走一遍「测量元素数量分布 → 为最常见的 0/1/2 元素加快路径 → 基准对比」的完整流程。

**操作步骤**：

1. 先写一个**通用版本**，处理任意长度的整数列表求和（**示例代码**）：

   ```rust
   // 示例代码：通用版本
   pub fn sum_all(data: &[u64]) -> u64 {
       let mut s = 0u64;
       for &x in data {
           s += x;          // 通用循环，对 0/1/2 个元素也要进循环、判断
       }
       s
   }
   ```

2. **测量分布**：先搞清楚真实输入里到底多大概率是 0/1/2 个元素。用一个直方图统计（承接 u3-l2 用 `eprintln!` 打分布的思路）：

   ```rust
   // 示例代码：统计输入长度分布
   fn main() {
       // 假设 inputs 是你真实场景里收集到的一批切片
       let inputs: Vec<Vec<u64>> = generate_inputs();
       let mut hist = [0u64; 4]; // [0个, 1个, 2个, >=3个]
       for v in &inputs {
           match v.len() {
               0 => hist[0] += 1,
               1 => hist[1] += 1,
               2 => hist[2] += 1,
               _ => hist[3] += 1,
           }
       }
       eprintln!("len 0: {}, 1: {}, 2: {}, >=3: {}", hist[0], hist[1], hist[2], hist[3]);
   }
   # fn generate_inputs() -> Vec<Vec<u64>> { vec![] }
   ```

   > 若你有 u3-l2 提到的 [`counts`](https://github.com/nnethercote/counts) 工具，也可直接用它出长度分布直方图。

3. 假设测量结果表明「绝大多数输入是 0 或 1 个元素」，加一条**特判快路径**：

   ```rust
   // 示例代码：特判小规模版本
   pub fn sum_all_fast(data: &[u64]) -> u64 {
       // 99% 的情况下 data 只有 0 或 1 个元素，先特判这两种（来源：本地分布测量）
       match data.len() {
           0 => 0,
           1 => data[0],
           _ => {
               let mut s = 0u64;
               for &x in data {
                   s += x;
               }
               s
           }
       }
   }
   ```

   注意上面那行注释——它正是 General Tips 章呼吁的「引用测量数据的注释」。

4. 用 u2-l1 的基准测试，在**符合测量分布**的输入（绝大多数是 0/1 个元素）上对比 `sum_all` 与 `sum_all_fast`。

**需要观察的现象**：当输入绝大多数是 0/1 个元素时，`sum_all_fast` 应当明显更快（少了进入循环、迭代器建立的固定开销）；当输入几乎全是长列表时，两者差不多（特判的快路径用不上）。

**预期结果**：收益与「小规模输入的占比 \(f\)」成正比——验证了 4.4.2 的期望耗时公式。特判只在「小规模主导」时才是 win，这与 perf-book 原文「when small sizes dominate」的限定完全一致。

**待本地验证**：实际加速取决于输入分布与调用频率；务必用**你自己场景的真实分布**测量，而非臆测。若分布测量显示小规模并不占多数，则不应加此特判——**测量决定要不要做**。

#### 4.4.5 小练习与答案

**练习 1**：General Tips 章在讲「按频率处理」时，强调的是「measure case frequencies」里的哪个词？为什么不能省略这一步？

**参考答案**：强调 **measure（测量）**。因为人脑对「哪种情况最常见」的直觉常常不准，按猜出来的频率排序可能把罕见情况排到最前、反而让常见情况多走判断。必须先用剖析/日志测出真实命中率，再据此排序——这与全书「先测量再优化」一脉相承。

**练习 2**：什么前提下「在大 HashMap 前加一个小缓存」才划算？什么前提下不划算？

**参考答案**：划算的前提是查找具有**高局部性**——同一个键在短期内很可能被重复查询，小缓存（哪怕只存「上一次的键值」）能挡掉大量重复查找。不划算的前提是查找**均匀随机**——每次查的键都不同，小缓存几乎永远 miss，徒增开销而无收益。所以 perf-book 原文把前提严格限定为「lookups that involve high locality」。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「**用通用原则走完一遍优化**」的小任务。任务以规格里的主实践为骨架，但要求你把四个模块都用上。

**任务**：给定下面这个处理「一批短整数列表」的函数（**示例代码**），按 General Tips 的原则优化它：

```rust
// 示例代码：起点 —— 待优化的通用函数
pub fn process(batches: &[Vec<u64>]) -> u64 {
    let mut total = 0u64;
    for batch in batches {
        // 对每个 batch：先去重，再求和
        let mut seen: Vec<u64> = Vec::new();
        for &x in batch {
            if !seen.iter().any(|&y| y == x) {   // 线性查找
                seen.push(x);
            }
        }
        total += seen.iter().sum::<u64>();
    }
    total
}
```

**步骤**：

1. **模块 4.1（只优化热点）**：先用剖析器（u2-l2）确认 `process` 确实是热点、并确认用了 release 构建（避开 General Tips 章开篇点名的陷阱）。若不热，就此打住——不要优化冷代码。
2. **模块 4.2（算法/数据结构优先）**：`seen.iter().any(...)` 是 \(O(n)\) 的线性去重，整个去重是 \(O(n^2)\)。把它换成 `HashSet`（`let seen: HashSet<u64> = batch.iter().copied().collect();`），整体降到 \(O(n)\)。这一步通常是最大的一笔收益。
3. **模块 4.3（缓存友好）**：确认遍历是连续的（`batch.iter()`）、且 `u64` 紧凑，无大步长跳跃；若 `batch` 实际是 `Vec<Box<u64>>` 之类带间接的布局，考虑去掉间接。
4. **模块 4.4（特判小规模）**：测量 `batch.len()` 的分布。若绝大多数 batch 只有 0/1/2 个元素，在去重前加一条快路径：`match batch.len() { 0 => continue, 1 => { total += batch[0]; continue; } _ => {...} }`，绕开建 `HashSet` 的固定开销。并为这条快路径写一句引用测量数据的注释。
5. **验证**：用 u2-l1 的基准测试，在**真实分布**的 `batches` 上对比每一步改动的耗时，**每次只改一项再测量**（承接 u2-l3 的纪律）。

**预期收获**：你会经历一次典型的「通用原则驱动」优化——先用 4.1/4.2 决定**该不该、改哪里、改算法**（拿到数量级收益），再用 4.3/4.4 在**常数因子**上锦上添花，全程用测量做决策、用注释记录「为什么这么写」。这正是 General Tips 章作为全书「心法总纲」的全部价值。

**待本地验证**：每一步的具体收益取决于真实输入的规模与分布；务必以本地剖析与基准测试输出为准，不要套用本讲的任何具体数字。

## 6. 本讲小结

- **只优化热点**：优化后的代码更复杂、更费工，所以只该花在执行频率高到足以影响运行时间的热点上；Amdahl 定律 \(S = 1/((1-p) + p/s)\) 给出硬性依据——优化冷代码的天花板 \(1/(1-p)\) 很低。
- **加速热点只有两条路径**： 让函数本身更快、 少调用它。本书其余所有技巧都可归入这两条；其中「消灭愚蠢的减速」往往比「发明聪明的加速」更划算。
- **算法与数据结构优先**：最大幅度的提升通常来自换算法/数据结构（复杂度层级、数量级收益），低层优化（常数因子）只能锦上添花；优化任何热点前先问「算法/数据结构选对了吗」。
- **缓存友好与分支预测**：让代码「顺」现代硬件——数据紧凑连续、访问顺序、分支可预测；不可预测的分支每次可能罚一笔预测失败惩罚。这类优化收益依赖场景、须测量确认，perf-book 对它持「值得追求但不强求」的态度。
- **特判常见小规模 + 按频率处理**：先测量输入分布，再为最常见的输入（尤其 0/1/2 元素、最高频的 case）开快路径；配合「紧凑表示 + 备用表」「高局部性查找加小缓存」「惰性求值」等招式。收益与「常见情况占比」成正比。
- **优化代码要有注释**：优化后的结构往往不再一目了然，引用剖析测量数据的注释（如「99% 的情况只有 0/1 个元素」）对后续读者极其宝贵。

## 7. 下一步学习建议

- **进入 u6-l2「Compile Times」**：本讲全部围绕「运行期」性能；下一讲把视角转向**构建期**，讲解如何用 `cargo build --timings`、`-Zmacro-stats`、`cargo llvm-lines` 诊断并缩短编译时间，并把泛型函数的非泛型部分拆成内部函数以减少 IR 膨胀。它依赖 u2-l3 的构建配置知识。
- **回到 u2-l1 / u2-l2**：本讲反复强调「测量决定一切」——分布要测、热点要测、收益要测。如果对基准测试的方差控制或剖析器的多工具交叉印证还不够熟，值得回看这两讲，它们是本讲所有原则的落地工具。
- **延伸阅读**：直接读 [src/general-tips.md](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/general-tips.md) 原文，并点开它附的十几个真实 rustc PR 示例链接——这些「在已高度优化的编译器代码里仍能靠换数据结构/特判小规模拿到提速」的案例，是理解本讲原则最有力的佐证。读完你会发现，本讲的每条原则都不是抽象口号，而是 rustc 团队反复验证过的工程经验。
