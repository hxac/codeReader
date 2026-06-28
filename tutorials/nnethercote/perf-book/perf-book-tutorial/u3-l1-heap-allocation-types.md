# 堆分配从哪里来——Box/Rc/Arc/Vec/String 与 Cow

## 1. 本讲目标

本讲是「堆分配与类型大小」单元的第一篇，回答一个最朴素的问题：**Rust 程序里的堆分配到底从哪里冒出来，又该怎么少分配一点？**

学完后你应当能够：

1. 说清楚一次堆分配为什么「不算便宜」，并会用 DHAT / dhat-rs 定位程序里最热的分配点。
2. 看到 `Box` / `Rc` / `Arc` / `Vec` / `String` / `HashMap` 时，能立刻判断它**会不会**在堆上分配、分配在**哪里**、`clone` 时会不会触发**新**分配。
3. 理解 `clone` / `to_owned` 与分配的关系，知道 `clone_from` 如何复用已有缓冲。
4. 在「借入数据」和「拥有数据」混用的场景下，用 `Cow` 取代 `Vec<String>`，消除不必要的堆分配。

本讲依赖前置讲义 [u2-l2（Profiling）](u2-l2-profiling.md)：你已经知道什么是「热点（hot spot）」，也知道为 release 构建开启调试信息后，剖析器能把耗时归因到源码行。本讲把同样的思路用在**内存分配**上。

---

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

### 2.1 什么是堆分配

Rust 里一个值可以放在**栈**上，也可以放在**堆**上。栈分配由编译器在编译期就安排好，进出只是一个指针移动，几乎免费。而堆分配是**运行期**才向系统申请一块大小不确定的内存，需要：

- **获取一把全局锁**（分配器内部维护空闲链表/桶等数据结构，多线程下要加锁）。
- **做一段非平凡的数据结构操作**（在空闲链表里找一块够大的空间、切分、登记）。
- **可能触发一次系统调用**（`mmap` / `brk`）。

因此每次 `malloc` + `free` 都不便宜，而且**小分配不一定比大分配便宜**（寻找空闲块的开销与块大小无关）。理解这一点，是后面所有「少分配」技巧的出发点。

### 2.2 「分配率」是一个独立的性能维度

性能优化时，我们常说「热点」。对 CPU 热点而言，热点是「执行频率高到影响运行时间的代码」。对内存而言，对应的概念是**分配热点（hot allocation site）**：那些**分配频率高到影响运行时间**的代码位置。

perf-book 给出了一条来自 rustc（Rust 编译器自身）实战的经验法则：

> 每执行 100 万条指令减少 10 次分配，就能带来约 1% 的可观性能提升。

换句话说，分配率（allocations per million instructions，缩写 **allocs/Minstr**）本身就是一个值得监控和优化的指标：

\[
\text{allocs/Minstr} \;=\; \frac{\text{分配次数}}{\text{执行的指令数}\,/\,10^{6}}
\]

### 2.3 定位分配热点的工具

普通 CPU 剖析器里如果 `malloc` / `free` 频繁上榜，就说明该降分配率了。但 CPU 剖析器看不出「**哪一行代码**分配了多少次、每次多大、活多久」。这正是 **DHAT**（Valgrind 的一个工具）的强项，它专门回答分配的「在哪里、多少次、多大、活多久、被读写多少」。在进程内（Rust 程序里），则用 **dhat-rs** 这个 crate 做同样的统计，还能写**堆用量回归测试**。

---

## 3. 本讲源码地图

本讲的「源码」是 perf-book 的两个章节，它们的链接与作用如下：

| 文件 | 作用 |
| --- | --- |
| [src/heap-allocations.md](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md) | 第 7 章，系统讲解哪些类型/操作会触发堆分配，以及如何减少分配。是本讲的主线。 |
| [src/type-sizes.md](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/type-sizes.md) | 第 8 章，讲解如何测量和缩小类型体积。本讲只引用其中「用 DHAT 找堆热点」与「>128 字节类型会被 memcpy 复制」两点，为分配开销提供佐证。 |

> 提示：perf-book 本身是一本用 mdBook 渲染的书，没有可运行的 Rust 工程。「读源码」就是读这两个 Markdown 章节的正文与代码示例。本讲引用的示例代码都来自这两个文件。

---

## 4. 核心概念与源码讲解

### 4.1 堆分配的开销与定位（DHAT）

#### 4.1.1 概念说明

这个模块回答两件事：**为什么堆分配值得专门优化**，以及**怎么找到最该优化的分配点**。

关键认识是：堆分配「中等昂贵」，且它的开销不只在分配本身，还在于它通常是**全局共享的**（一把锁）、还可能牵涉系统调用。所以——**理解哪些 Rust 类型/操作会触发分配，并主动避免它们**，能带来可观的性能提升。这正是 perf-book 设立整个「Heap Allocations」一章的动机。

#### 4.1.2 核心流程

定位分配热点的标准动作是：

1. 用普通 CPU 剖析器跑一遍，看 `malloc` / `free` 是否上榜。
2. 若上榜，改用 **DHAT**（外部程序）或 **dhat-rs**（进程内）做一次专门的分配剖析。
3. 读 DHAT 输出里的关键字段：分配站点（call stack）、**总字节数**、**总块数（blocks）**、平均大小、平均寿命、读写次数。
4. 挑出「总字节数大」或「每百万指令分配次数高」的站点优先优化。

#### 4.1.3 源码精读

**① 分配为何昂贵** —— perf-book 开篇就定调：

[src/heap-allocations.md:L1-L9](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md#L1-L9)

> 这段说明：每次分配（与释放）通常涉及**获取全局锁**、做**非平凡的数据结构操作**、可能执行**系统调用**；而且**小分配不一定比大分配便宜**。

**② 用 DHAT 定位分配站点** —— 第 16 章风格的「Profiling」小节明确推荐 DHAT：

[src/heap-allocations.md:L22-L28](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md#L22-L28)

> 这段说明：当要降低分配率时，DHAT 是绝佳工具；它能**精确识别热点分配站点及其分配率**。注意那条经验数字——rustc 实测表明每百万指令减少约 10 次分配即可带来约 1% 的性能提升。

**③ DHAT 输出长什么样** —— 书中给了一段真实示例输出（来自早期 rustc）：

[src/heap-allocations.md:L31-L53](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md#L31-L53)

> 这段说明：DHAT 对**单个分配站点**给出 Total（总字节/总块数/平均大小/平均寿命）和 Reads/Writes，以及 `Allocated at {…}` 的完整调用栈（此处一直回溯到 `alloc.rs` → `raw_vec.rs` → `vec.rs` 的 `push` → 词法分析的 `parse_token_tree`）。据此你能精确回答「这块内存是谁、在哪、分配了多少次、活多久、被读写多少」。

**④ 「分配也会拖累类型体积」** —— type-sizes.md 补充了另一条线索：当内存占用高时，DHAT 同样能找出热点分配点与涉及的类型：

[src/type-sizes.md:L5-L8](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/type-sizes.md#L5-L8)

> 这段说明：缩小这些热点类型能降低**峰值内存**，还可能通过减少内存流量与缓存压力提速。DHAT 在「找分配热点」和「找体积热点」两个场景下都适用。

#### 4.1.4 代码实践

**实践目标**：用 DHAT 跑一个真实小程序，亲手看到「分配站点 + 分配率」的输出。

**操作步骤**：

1. 写一个会反复分配的小程序（例如逐行读文件、或循环里 `push` 到 `Vec`），用普通构建。
2. 用 Valgrind 套上 DHAT 运行：`valgrind --tool=dhat ./your_program`（Linux / 部分 Unix）。
3. 在 DHAT 的 HTML/文本报告里，找到 `Total bytes` 最大的那个 `AP`（allocation point），记录它的调用栈与 `blocks` 数。

**需要观察的现象**：报告里每个站点是否都带完整调用栈？`avg lifetime`（平均寿命）和 `avg size`（平均大小）是否正如书中示例那样可读？

**预期结果**：你能指出「哪一行源码贡献了最多分配字节」。如果本地没装 Valgrind/DHAT，这一步的精确数值**待本地验证**，但你能确认输出结构与书中示例一致。

#### 4.1.5 小练习与答案

**练习 1**：为什么 perf-book 说「小分配不一定比大分配便宜」？
**答案**：因为分配开销主要来自**加锁、维护空闲链表、可能的系统调用**，这些与块大小关系不大；找到一块空闲小空间和找到一块大空间，数据结构操作的代价相近。

**练习 2**：普通 CPU 剖析器和 DHAT，分别回答「分配」的什么问题？
**答案**：普通 CPU 剖析器只能告诉你 `malloc`/`free` **在总耗时里占比多少**；DHAT 才能告诉你**哪个站点分配了多少次、多大、活多久、读写多少**——即分配的「质」而不仅是「量」。

---

### 4.2 Box/Rc/Arc 的分配特性

#### 4.2.1 概念说明

这是「会主动堆分配」的最基础三类智能指针。理解它们的要点是分清两件事：**这个指针本身会不会触发一次堆分配？** 以及 **`clone` 它会不会触发新分配？** perf-book 对三者的论述很短，但恰好点出了它们在分配上的关键差别。

#### 4.2.2 核心流程

三者的分配行为对比：

| 类型 | 创建时分配？ | `clone` 时分配？ | 用途 |
| --- | --- | --- | --- |
| `Box<T>` | 是，把 `T` 放到堆上 | 是（深拷贝 `T`） | 最简单的堆类型；偶尔用来给结构体/枚举字段「装箱」以缩小体积 |
| `Rc<T>` / `Arc<T>` | 是，且额外带**两个引用计数**字 | **否**，只把引用计数 +1 | 共享同一个值，可减少内存占用 |
| `Rc` vs `Arc` | 同为「带计数的共享指针」 | 同 | `Rc` 单线程、`Arc` 多线程（原子计数） |

注意 `Rc`/`Arc` 的「双刃剑」：对**很少被共享**的值使用它们，反而会**增加**分配率——把本来可以留在栈上的值硬塞到堆上。

#### 4.2.3 源码精读

**① `Box`：最简单的堆类型** ——

[src/heap-allocations.md:L55-L67](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md#L55-L67)

> 这段说明：`Box<T>` 就是一个被放到堆上的 `T`。除了「把结构体/枚举的某些字段装箱以缩小类型」（详见 type-sizes 章节）之外，`Box` 行为很直接，**优化空间不大**。

**② `Rc`/`Arc`：共享 + 引用计数** —— 重点看两条：

[src/heap-allocations.md:L71-L83](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md#L71-L83)

> 这段说明：`Rc`/`Arc` 像 `Box`，但堆上的值**附带两个引用计数**，用于**值共享**、降低内存占用。两个关键点：
> - 对**很少共享**的值用它们，反而**抬高分配率**（把本可留在栈上的值塞到堆上）；
> - 与 `Box` 不同，对 `Rc`/`Arc` 调用 `clone` **不分配**，只是把引用计数 **+1**。

#### 4.2.4 代码实践

**实践目标**：用直觉印证「`Rc::clone` 不分配、`Box` clone 会深拷贝」。

**操作步骤**（源码阅读型，无需运行）：

1. 在标准库文档打开 [`Rc` 文档](https://doc.rust-lang.org/std/rc/struct.Rc.html)，找到 `Clone for Rc` 的实现说明，确认它「increments the strong reference count」（只增计数）。
2. 对比 [`Box` 文档](https://doc.rust-lang.org/std/boxed/struct.Box.html)：`Box` 的 `clone` 要求 `T: Clone`，即对内部值做深拷贝。

**需要观察的现象**：`Rc::clone` 的签名是否要求 `T: Clone`？（不需要。）`Box::clone` 是否要求？（要求。）

**预期结果**：从类型签名层面就能验证「共享指针 clone 免费、`Box` clone 深拷贝」这一结论，无需运行。**待本地验证**仅指具体文档措辞可能随版本微调。

#### 4.2.5 小练习与答案

**练习 1**：为什么对「很少被共享」的值用 `Arc` 反而可能拖慢程序？
**答案**：因为它把这些值从栈上搬到了堆上，凭空多出一次分配（外加引用计数的原子操作开销），却没换来「共享减少内存」的好处。

**练习 2**：`Box<T>` 和 `Rc<T>` 在 `clone` 行为上最本质的区别是什么？
**答案**：`Box` clone 会**深拷贝** `T`（产生新分配）；`Rc` clone 只**递增引用计数**，不分配、也不复制 `T`。

---

### 4.3 Vec/String/Hash 表的表示与 clone/to_owned

#### 4.3.1 概念说明

这一模块是本讲信息量最大的一块，回答「日常用得最多的集合类型，到底把内存放在哪、`clone`/`to_owned` 时会发生什么」。

核心心智模型只有一个：**`Vec<T>` 是「三字（three words）」表示**。一旦你理解了 `Vec`，`String`（≈ `Vec<u8>`）、`HashMap`/`HashSet`（与 `Vec` 同构的单一连续堆分配）就都是它的变体。

#### 4.3.2 核心流程

**`Vec` 的「三字」内存布局**：

```
Vec<T> 在栈上占三个机器字（word）：
  ┌──────────┬──────────┬──────────┐
  │ length   │ capacity │ pointer ─┼──→  堆上连续存放的 T 元素
  └──────────┴──────────┴──────────┘
```

- `length`：当前**实际有多少个**元素。
- `capacity`：**不重新分配**的前提下最多能放多少个（≥ length，多出的位置是预留给未来 `push` 的）。
- `pointer`：指向堆上那块连续内存。当 capacity 为 0 或元素大小为 0 时，它**不**指向已分配内存（即不分配）。

**增长策略（准倍增长）**：空 `Vec`（`vec![]` / `Vec::new` / `Vec::default`）length=capacity=0，**不分配**。反复 `push` 时按 0 → 4 → 8 → 16 → 32 → 64 … 准倍增长。从 0 直接到 4（跳过 1、2）是为了在实践中**避免大量小分配**。

为什么准倍增长是「好」策略？因为它让 `push` 的**摊还（amortized）成本**为常数：

\[
\text{摊还成本}(\text{push}) \;=\; \frac{\text{总搬运次数}}{\text{push 次数 } n} \;=\; O(1)
\]

直观地说：每翻倍一次就把之前所有元素整体搬运一遍，但翻倍之间的间隔越来越长，所以**平均到每次 push 上的搬运量是常数**。代价是：向量越长，尾部**浪费的多余容量**也指数级增长。

**`clone` 与 `to_owned`**：对一个**含堆内存**的值 `clone`，通常意味着**额外分配**（非空 `Vec` clone 会为元素申请新内存）。例外正是 4.2 讲的 `Rc`/`Arc`。而 `to_owned`（以及 `to_string`）把借入数据变成拥有数据，通常也靠 `clone`，所以同样会分配。

#### 4.3.3 源码精读

**① `Vec` 的三字表示** ——

[src/heap-allocations.md:L93-L101](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md#L93-L101)

> 这段说明：`Vec` 含三个字——length、capacity、pointer；pointer 在 capacity/元素大小非零时才指向堆内存；元素（若存在且非零大小）**总是**在堆上，且堆内存可能比实际所需更大（预留容量）。

**② 准倍增长策略** ——

[src/heap-allocations.md:L109-L119](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md#L109-L119)

> 这段说明：空 `Vec` 不分配；反复 `push` 时按 **0, 4, 8, 16, 32, 64** 准倍增长；从 0 直接跳到 4 是为了**避免大量小分配**。随之而来的权衡是：增长越深，**浪费的多余容量**也指数级增加。

**③ 已知长度就预分配** —— 如果知道长度，用 `with_capacity` / `reserve` 一次到位：

[src/heap-allocations.md:L166-L185](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md#L166-L185)

> 这段说明：若知道至少需要 20 个元素，`Vec::with_capacity`/`reserve`/`reserve_exact` 能**一次分配到位**；否则逐个 `push` 会触发 4 次（cap 为 4/8/16/32）重新分配。`shrink_to_fit` 则可压缩浪费，但可能引发一次重分配。

**④ `String` ≈ `Vec<u8>`，`format!` 会分配** ——

[src/heap-allocations.md:L189-L214](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md#L189-L214)

> 这段说明：`String` 的表示与操作和 `Vec<u8>` 极为相似（`with_capacity` 等都有对应版本）。关键提醒：**`format!` 宏会产生一个 `String`，也就是一次分配**——能用字符串字面量替代就别用 `format!`。

**⑤ Hash 表与 Vec 同构** ——

[src/heap-allocations.md:L219-L230](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md#L219-L230)

> 这段说明：`HashSet`/`HashMap` 也是**单一连续堆分配**，存放键值，随表增长而重分配；与 `Vec` 一样有 `with_capacity` 等容量相关方法。

**⑥ `clone` 通常分配，`clone_from` 可复用缓冲** ——

[src/heap-allocations.md:L234-L252](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md#L234-L252)

> 这段说明：对含堆内存的值 `clone` 通常触发新分配（非空 `Vec` clone 会为元素申请新内存，新 Vec 的 capacity 未必与原来相同）；例外是 `Rc`/`Arc`。而 `a.clone_from(&b)`（等价于 `a = b.clone()`）**可能复用** `a` 已有的堆分配，例如把一个 `Vec` clone 到另一个已有大 capacity 的 `Vec` 时，原分配被复用（示例中 `v1` 的 capacity 保持 99）。

**⑦ `to_owned` 同样靠 clone** ——

[src/heap-allocations.md:L269-L280](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md#L269-L280)

> 这段说明：`ToOwned::to_owned` 由借入数据造拥有数据（通常靠 clone），故常触发分配（如 `&str → String`）。有时可改为**在结构体里存借入数据的引用**来避免，但要加生命周期标注、让代码变复杂，**仅当剖析与基准证明值得时才做**。

#### 4.3.4 代码实践

**实践目标**：亲手验证「`clone_from` 复用已有缓冲」与「`format!` 会分配」。

**操作步骤**（源码阅读 + 小实验）：

1. 阅读书中 `clone_from` 示例 [src/heap-allocations.md:L247-L252](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md#L247-L252)：一个 capacity=99 的 `Vec`，用 `clone_from` 覆盖后 capacity **仍为 99**，证明底层分配被复用。
2. 在一个最小 Rust 工程里复刻这段断言：

```rust
// 示例代码：验证 clone_from 复用缓冲
let mut v1: Vec<u32> = Vec::with_capacity(99);
let v2: Vec<u32> = vec![1, 2, 3];
v1.clone_from(&v2);
assert_eq!(v1.capacity(), 99); // 旧的 99 容量被复用，没有重新分配
```

**需要观察的现象**：去掉 `clone_from` 改成 `v1 = v2.clone();` 后，`v1.capacity()` 会是多少？（通常等于 length 3，因为这是全新分配。）

**预期结果**：`clone_from` 版本 capacity 保持 99；直接赋值版本 capacity ≈ 3。**待本地验证**具体 capacity 数值（取决于标准库实现细节），但「复用 vs 重新分配」的差异是确定的。

#### 4.3.5 小练习与答案

**练习 1**：`Vec::new()` 之后没有任何 `push`，会发生堆分配吗？为什么？
**答案**：不会。空 `Vec` 的 length 和 capacity 都是 0，pointer 不指向任何已分配内存；只有当 capacity/元素大小非零时才会真正分配。

**练习 2**：`a.clone_from(&b)` 相比 `a = b.clone()` 有什么潜在收益？
**答案**：`clone_from` 可以**复用** `a` 已有的堆分配（如 `Vec` 的 capacity），避免一次不必要的释放 + 重新分配；`a = b.clone()` 则是先构造一个全新对象再整体替换 `a`。

**练习 3**：为什么书里反复提醒「`format!` 会分配」？
**答案**：因为 `format!` 的产物是一个拥有所有权的 `String`，而 `String` 是堆类型；如果本可以用字符串字面量（`&'static str`，存在程序只读段，零运行期分配）表达同样的内容，`format!` 就是多余的分配。

---

### 4.4 Cow 复用借入与拥有数据

#### 4.4.1 概念说明

`Cow<T>`（Copy-on-Write 的缩写，但更准确的解读是 **Clone-on-Write**）解决的是一个非常常见的场景：**一批数据里，有些是借入的（静态字面量），有些是必须拥有的（动态拼接出来的）**。

最朴素的写法是用 `Vec<String>` 统一存放。但这样一来，**静态字面量也得先 `to_string()` 提升成 `String`**，凭空多出一次分配。`Cow` 让你**既存借入又存拥有**，在「不修改」时零额外分配，只在「需要改」时才克隆——这就是 clone-on-write。

#### 4.4.2 核心流程

`Cow<'a, B>` 是一个枚举，两个变体：

```
enum Cow<'a, B: ?Sized + ToOwned> {
    Borrowed(&'a B),   // 借入：零分配
    Owned(<B as ToOwned>::Owned),  // 拥有：已分配
}
```

- 写「借入值」`x`：`Cow::Borrowed(x)`，例如 `Cow::Borrowed("oops")`，**不分配**。
- 写「拥有值」`y`：`Cow::Owned(y)`，例如 `Cow::Owned(format!(…))`，分配发生在 `format!` 里。
- 还能借助 `From` 用 `.into()` 写：`"oops".into()` 或 `format!(…).into()`。

它配套两种「读/写」机制：

1. **读**：`Cow` 实现了 `Deref`，可直接当成 `&B` 用（如当 `&str`），调方法无需拆包。
2. **写**：调 `Cow::to_mut()` 拿可变引用——若当前是 `Borrowed`，会**先克隆成 Owned**（这就是 clone-on-write 的来源）；若已是 `Owned`，直接返回引用。

典型搭配：`&str` / `String`、`&[T]` / `Vec<T>`、`&Path` / `PathBuf`。

#### 4.4.3 源码精读

**① 痛点：`Vec<String>` 把字面量也提升成拥有** ——

[src/heap-allocations.md:L284-L294](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md#L284-L294)

> 这段说明：错误信息里既有静态字面量、又有 `format!` 拼出来的。朴素写法用 `Vec<String>`，结果字面量也得 `to_string()` 提升——**这次提升就是一次分配**。

**② 解法：用 `Cow` 同时承载借入与拥有** ——

[src/heap-allocations.md:L296-L315](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md#L296-L315)

> 这段说明：改用 `Vec<Cow<'static, str>>`，字面量包成 `Cow::Borrowed`（零分配），`format!` 结果包成 `Cow::Owned`。最终 `errors` 混合了借入与拥有数据，**不需要任何额外分配**。除 `&str`/`String` 外，`&[T]`/`Vec<T>`、`&Path`/`PathBuf` 同样适用。

**③ clone-on-write 与 `Deref`** ——

[src/heap-allocations.md:L320-L338](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md#L320-L338)

> 这段说明：对不可变数据，以上混合存储已经够用；若还需要修改，`Cow::to_mut()` 会**在必要时把借入克隆成拥有**——这就是「clone-on-write」，也是 `Cow` 名字的由来。又因为 `Cow` 实现了 `Deref`，可以直接对它内部数据调方法，不必拆包。书中最后一句承认 `Cow`「有点啰嗦（fiddly）」，但**常常值得**。

#### 4.4.4 代码实践

**实践目标**：把书里的 `Vec<String>` 改写成 `Vec<Cow<'static, str>>`，并用 dhat-rs（或直觉推理）比较两者分配次数。

**操作步骤**：

1. 新建一个最小 Cargo 工程：`cargo new cow_demo && cd cow_demo`。
2. 在 `Cargo.toml` 加入 dhat-rs（仅用于统计；若不想引入依赖，可跳过步骤 4，改为靠推理比较分配数）：

```toml
# 示例代码：Cargo.toml
[dependencies]
dhat = "0.3"
```

3. 在 `src/main.rs` 写两个版本（示例代码）：

```rust
// 示例代码：Vec<String> 版本——每个元素都分配
fn errors_vec_string(n: u32) -> Vec<String> {
    let mut errors: Vec<String> = vec![];
    for i in 0..n {
        if i % 2 == 0 {
            errors.push("something went wrong".to_string()); // 字面量也被提升，分配！
        } else {
            errors.push(format!("something went wrong on line {}", i)); // 分配
        }
    }
    errors
}

// 示例代码：Vec<Cow<'static, str>> 版本——字面量零分配
use std::borrow::Cow;
fn errors_vec_cow(n: u32) -> Vec<Cow<'static, str>> {
    let mut errors: Vec<Cow<'static, str>> = vec![];
    for i in 0..n {
        if i % 2 == 0 {
            errors.push(Cow::Borrowed("something went wrong")); // 零分配
        } else {
            errors.push(Cow::Owned(format!("something went wrong on line {}", i))); // 仅 format! 分配
        }
    }
    errors
}
```

4. 用 dhat-rs 统计（示例代码——dhat-rs 的确切统计 API 随版本变化，以下为结构示意，精确输出**待本地验证**）：

```rust
// 示例代码：把 dhat-rs 设为全局分配器以采集堆统计
#[global_allocator]
static ALLOC: dhat::Dhat = dhat::Dhat::new_heap();

fn main() {
    // 分别测量两个函数的堆分配（dhat-rs 的 region/stats API 请以本地安装版本文档为准）
    let n = 1000;
    let _v1 = errors_vec_string(n);
    // 记录此时累计分配字节数 / 块数
    let _v2 = errors_vec_cow(n);
    // 再次记录，对比两次增量
}
```

**需要观察的现象**：对比两次调用的**累计分配块数（blocks）**。

**预期结果**：`Vec<String>` 版本约为 `n` 次堆分配（每个元素一次，外加 Vec 自身若干次增长分配）；`Vec<Cow>` 版本约为 `n/2` 次（只有奇数 `format!` 分配，字面量侧零分配），外加 Vec 增长分配。即 **Cow 版本的分配次数大约减半**。dhat-rs 具体数值与 API 形态**待本地验证**，但「字面量侧不再分配」这一结论是确定的——它来自书里 `Cow::Borrowed` 不分配的语义。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `Vec<Cow<'static, str>>` 里的生命周期是 `'static`？
**答案**：因为借入的那些 `&str` 是**字符串字面量**，它们被编译进程序的只读数据段，存活整个程序生命周期，所以借用是 `'static` 的。若借入的是运行期产生的 `&str`，则需要用更短的生命周期 `'a`。

**练习 2**：`Cow::to_mut()` 在「当前是 Borrowed」时会做什么？为什么这叫 clone-on-write？
**答案**：它会**先把借入数据克隆成 Owned**，再返回可变引用；只有真正要写时才付出克隆/分配的代价，读时不付——这就是「写时克隆（clone-on-write）」。名字虽叫 Copy-on-Write，但 perf-book 强调对堆类型而言实际是 clone 而非 memcpy。

**练习 3**：举出一个 `Cow` **不适合**的场景。
**答案**：当**绝大多数**数据都是动态拼接（`Owned`）时，`Cow` 几乎没机会省下分配，却带来枚举判别与拆包的额外开销与代码复杂度——此时直接用 `Vec<String>` 更简单。`Cow` 的收益主要来自「大量借入、少量拥有」的不对称分布。

---

## 5. 综合实践

把本讲四个模块串起来，做一个端到端的小任务：**为「错误信息收集器」做一次完整的分配优化**。

**任务背景**：你要写一个函数，收集程序运行中产生的错误信息。其中约 70% 是固定文案的静态字面量（如 `"file not found"`），约 30% 是带上下文的动态拼接（如 `format!("file {path} not found")`）。该函数在一次运行里会被调用上万次。

**要求**：

1. **基线版**：用 `Vec<String>` 实现，并用 dhat-rs（或 DHAT）记录其总分配块数与字节数。
2. **分析**：用 4.1 的方法指出基线版的分配热点——重点在「字面量被 `to_string()` 提升」这一步。
3. **优化版**：改用 `Vec<Cow<'static, str>>`（4.4），让 70% 的字面量侧零分配；同时对承载 errors 的外层 `Vec` 用 `with_capacity`（4.3）按预估长度预分配，避免准倍增长带来的多次重分配。
4. **验证**：再次用 dhat-rs 统计，对比优化前后的分配块数。是否如预期：字面量侧分配被消除、外层 Vec 重分配次数从多次降为一次？

**思考题**：如果调用点还需要**修改**某些错误信息（例如给所有信息加统一前缀），`Cow` 的 clone-on-write 行为会如何影响你的优化收益？（提示：参考 `to_mut()` 的语义。）

> 说明：本实践需要本地有一个可运行 dhat-rs 的 Rust 工程与（可选的）Valgrind/DHAT 环境。具体数值**待本地验证**，但优化方向（借入 vs 拥有、预分配 vs 准倍增长）是本讲确定性的结论。

---

## 6. 本讲小结

- 堆分配「中等昂贵」：每次都涉及**全局锁 + 数据结构操作 + 可能的系统调用**，小分配并不更便宜；分配率（allocs/Minstr）是值得单独监控的维度，rustc 实测每百万指令少 10 次分配 ≈ 1% 提速。
- **DHAT / dhat-rs** 是定位分配站点的利器，能精确给出「在哪、多少次、多大、活多久、读写多少」，dhat-rs 还能写堆用量回归测试。
- `Box` 是最简单的堆类型（优化空间小）；`Rc`/`Arc` 像 `Box` 但带引用计数，`clone` 时**只增计数不分配**，但对很少共享的值使用反而抬高分配率。
- `Vec` 是**三字（length/capacity/pointer）**表示，元素总在堆上；空 Vec 不分配；准倍增长（0→4→8→16…）使 push 摊还为 O(1)，但越长越浪费容量；`String`、`HashMap`/`HashSet` 与之同构。
- `clone` / `to_owned` 对含堆内存的值通常触发**新分配**；`clone_from` 可复用已有缓冲；`format!` 必定分配，能用字面量就别用。
- `Cow` 用 `Borrowed`/`Owned` 两态同时承载借入与拥有数据，读时零额外分配、写时 clone-on-write，是消除「字面量被迫提升成 String」这类分配的惯用法。

---

## 7. 下一步学习建议

本讲聚焦「**类型/操作会不会分配**」。下一步建议沿两条线深入：

1. **纵向：把「分配」这件事做到极致** —— 进入下一讲 [u3-l2（Vec 的增长、集合复用与分配回归）](u3-l2-vec-growth-and-reuse.md)，学习 `SmallVec`/`ArrayVec` 处理短 Vec、循环外复用 workhorse 集合、以及用 dhat-rs 写**分配量回归测试**防止性能倒退。
2. **横向：从「分配次数」转向「类型体积」** —— 阅读 [u3-l3（Type Sizes）](u3-l3-type-sizes.md) 和源码 [src/type-sizes.md](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/type-sizes.md)，学会用 `-Zprint-type-sizes` 测量布局、用「装箱大变体/更小整数/boxed slice」缩小频繁实例化的类型——因为「大于 128 字节的类型会被 `memcpy` 复制」，缩小类型既能省内存又能减拷贝。
3. **回看基础** —— 若对「借入 vs 拥有」的生命周期机制还不够熟，建议重读标准库 [`Borrow`](https://doc.rust-lang.org/std/borrow/trait.Borrow.html) / [`ToOwned`](https://doc.rust-lang.org/std/borrow/trait.ToOwned.html) / [`Cow`](https://doc.rust-lang.org/std/borrow/enum.Cow.html) 文档，它们是理解本讲 `Cow` 一节的理论基础。
