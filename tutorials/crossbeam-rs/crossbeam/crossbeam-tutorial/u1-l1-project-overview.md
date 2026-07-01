# crossbeam 项目概览与定位

## 1. 本讲目标

本讲是整本学习手册的第一篇。读完本讲，你应当能够：

- 用一句话向别人说清楚 **crossbeam 是什么**、它解决 Rust 并发编程里的哪一类问题。
- 说出 crossbeam 把工具划分成的 **五大类**（原子、数据结构、内存管理、线程同步、工具）分别包含哪些东西。
- 认清 crossbeam 的 **6 个子 crate**（channel / deque / epoch / queue / utils / skiplist）各自的职责，以及主 crate 如何用「门面（facade）」模式把它们统一重导出为 `crossbeam::*`。
- 理解 `no_std` / `alloc` / `std` 这三级特性（feature）的含义，知道哪些工具能在没有标准库的环境下使用。
- 掌握 crossbeam 的 **MSRV（最低支持 Rust 版本）**、**许可证** 与 **版本约定**。

本讲不会深入任何一个算法实现，那是后续讲义的任务；本讲只负责让你「站在项目门口，看清整栋楼的平面图」。

## 2. 前置知识

在开始之前，你需要大致了解以下概念。如果某个词完全陌生，也没关系，本讲会用通俗语言再解释一遍。

- **并发（concurrency）与并行（parallelism）**：多个任务在逻辑上同时推进叫并发；在物理上真正同时执行叫并行。crossbeam 主要服务的是多线程并发场景。
- **线程（thread）**：操作系统调度的执行单元。多个线程可以同时访问同一块内存，于是就有了「数据竞争」的风险。
- **原子操作（atomic operation）**：一种「不可被打断」的内存读写，例如「比较并交换（CAS）」。它是构造无锁数据结构的砖块。
- **Cargo**：Rust 的构建工具与包管理器。`Cargo.toml` 描述依赖与特性，`workspace`（工作区）把多个相关 crate 放在一起统一构建。
- **`no_std`**：Rust 程序可以不链接标准库 `std`，只用更底层的 `core`（必要时再加 `alloc`）。这在嵌入式、内核等没有操作系统的环境里很常见。

> 名词小贴士：什么是 **crate**？在 Rust 里，crate 是最基础的编译单元，可以理解为一个「独立发布的包」。crossbeam 就是一个由多个 crate 组成的 **workspace（工作区）**。

## 3. 本讲源码地图

本讲只读「项目门面」级别的文件，不进入任何算法实现：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/README.md) | 面向用户的总说明：工具分类、子 crate 列表、用法、兼容性与许可证。 |
| [Cargo.toml](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/Cargo.toml) | 主 crate 的清单：版本、特性（feature）门控、对子 crate 的依赖、workspace 成员。 |
| [src/lib.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/src/lib.rs) | 主 crate 的入口：用 `pub use` 把各子 crate 重导出成统一的 `crossbeam` 命名空间。 |
| [CHANGELOG.md](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/CHANGELOG.md) | 版本变更记录，能看到 MSRV 的演进历史。 |

> 永久链接说明：本讲所有源码链接都指向固定 commit `6195355e`，这样即使仓库后续更新，你看到的行号也不会错位。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

- **4.1 项目定位与并发工具分类** —— crossbeam 是什么，它把工具分成哪五类。
- **4.2 子 crate 划分与门面重导出** —— 6 个子 crate 各干什么，主 crate 如何统一暴露它们。
- **4.3 no_std / alloc / std 分级特性支持** —— 三级特性如何层层传递。
- **4.4 MSRV、许可证与版本约定** —— 工程上的兼容性与法律约定。

### 4.1 项目定位与并发工具分类

#### 4.1.1 概念说明

Rust 标准库已经提供了一些并发工具，比如 `std::sync::mpsc`（多生产者单消费者通道）、`std::sync::Arc`、`std::sync::Mutex`、各种 `AtomicXxx`。那为什么还需要 crossbeam？

一句话定位：**crossbeam 是一套「比标准库更全面、更精细」的并发编程工具集，尤其擅长无锁（lock-free）数据结构与基于 epoch 的内存回收。** 它并不取代 `std`，而是在 `std`「够用但不够强」的地方补位。例如：

- `std::sync::mpsc` 是 **MPSC**（多生产者、单消费者）通道；而 crossbeam 的通道是 **MPMC**（多生产者、多消费者），还支持多路选择 `select`。
- `std` 没有现成的并发队列、工作窃取双端队列、无锁跳表；crossbeam 都有。
- `std` 没有专门为自旋锁设计的退避（backoff）原语、缓存行对齐包装；crossbeam 提供 `Backoff`、`CachePadded` 这类「细节级」工具。

Cargo.toml 里的一句话描述也印证了这点：

> `description = "Tools for concurrent programming"`（用于并发编程的工具）。

而它的关键词则更精确地暴露了它的「性格」：

[Cargo.toml:L15-L16](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/Cargo.toml#L15-L16)

> 关键词 `atomic / garbage / non-blocking / lock-free / rcu`：原子、垃圾回收（GC）、非阻塞、无锁、RCU（Read-Copy-Update）。这些词说明 crossbeam 关心的是「高性能、非阻塞、需要安全内存回收」的那一档并发编程。

#### 4.1.2 核心流程

crossbeam 把自己的工具按用途分成 **五大类**。这是理解整个项目的「总目录」：

```
┌─ Atomics 原子
│    AtomicCell        —— 任意类型的原子单元（可当「原子版 Cell」理解）
│    AtomicConsume     —— 用 consume 内存序读取原子量
│
├─ Data structures 数据结构
│    deque             —— 工作窃取双端队列（造调度器用）
│    ArrayQueue        —— 有界 MPMC 队列（一次性分配固定容量）
│    SegQueue          —— 无界 MPMC 队列（按需分配小段）
│
├─ Memory management 内存管理
│    epoch             —— 基于 epoch 的垃圾回收器（造无锁结构用）
│
├─ Thread synchronization 线程同步
│    channel           —— MPMC 通道（消息传递）
│    Parker            —— 线程停放（park/unpark）原语
│    ShardedLock       —— 分片读写锁（读路径快）
│    WaitGroup         —— 等待一组任务完成
│
└─ Utilities 工具
     Backoff           —— 自旋循环里的指数退避
     CachePadded       —— 按缓存行对齐，消除伪共享
     scope             —— 作用域线程，能借用栈上变量
```

每个工具后面带的 <sup>(no_std)</sup> 或 <sup>(alloc)</sup> 标记，表示它对运行环境的最低要求，这是 4.3 节的内容。

#### 4.1.3 源码精读

README 开篇这段就是上面那张图的原始出处：

[README.md:L15-L47](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/README.md#L15-L47)

这段代码（其实是文档）做了什么：先给出一句话总览，然后按 Atomics / Data structures / Memory management / Thread synchronization / Utilities 五个小标题逐项列出工具，每项后用 <sup>(no_std)</sup> 或 <sup>(alloc)</sup> 标注环境要求。**这五类划分就是后续整本学习手册的「章节骨架」**——你会看到第二单元讲工具类、第三单元讲 channel、第四单元讲 queue、第五单元讲 epoch、第六单元讲 deque、第七单元讲 skiplist。

注意几处关键措辞：

- `ArrayQueue` 被描述为 **bounded MPMC**（有界、多生产者多消费者），在构造时就分配固定容量的缓冲；
- `SegQueue` 被描述为 **unbounded MPMC**，按需分配称为 segment（分段）的小缓冲；
- `channel` 明确是 **multi-producer multi-consumer**，这正点出了它和 `std::sync::mpsc`（单消费者）的本质差别。

#### 4.1.4 代码实践

**实践目标**：亲手把「五大分类」对照到真实文档，而不是停留在记忆。

**操作步骤**：

1. 打开本仓库的 [README.md](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/README.md)，定位到 `## Atomics`（约第 17 行）开始往下读。
2. 准备一张五列的表，表头分别是：原子、数据结构、内存管理、线程同步、工具。
3. 把 README 里列出的每一个工具填进对应列，并记下它是否带 <sup>(no_std)</sup> 或 <sup>(alloc)</sup> 标记。

**需要观察的现象**：

- 五类里只有 `Memory management` 这一类只有 **一个** 工具（`epoch`），但它却是最难、也最核心的一块。
- `Utilities` 里的 `scope`、`Thread synchronization` 里的 `channel / Parker / ShardedLock / WaitGroup` 都 **没有** 任何 <sup>(no_std)</sup> 标记，说明它们都需要完整的 `std`。

**预期结果**：你会得到一张与上面 4.1.2 流程图基本一致的表格，并且理解「为什么有些工具能在裸金属上跑，有些不能」。

#### 4.1.5 小练习与答案

**练习 1**：标准库 `std::sync::mpsc` 与 crossbeam `channel` 最关键的一个差别是什么？

> **答案**：消费者数量。`mpsc` 是 multi-producer **single**-consumer（只能有一个接收者），而 crossbeam 的 channel 是 **multi-producer multi-consumer**（可以有多个接收者）。此外 crossbeam channel 还内建 `select` 多路选择能力。

**练习 2**：README 里 `ArrayQueue` 和 `SegQueue` 都叫 MPMC 队列，它们的区别在哪？

> **答案**：容量策略不同。`ArrayQueue` 是 **bounded（有界）**，构造时就分配固定大小的环形缓冲；`SegQueue` 是 **unbounded（无界）**，会按需不断分配称为 segment 的小缓冲。前者内存占用可预测、有背压，后者更灵活但需要按需扩容。

**练习 3**：crossbeam 的关键词里有 `rcu`，RCU 是什么意思，它和 crossbeam 有什么关系？

> **答案**：RCU 全称 Read-Copy-Update，是一种「读操作无锁、写操作先复制再替换、旧数据延迟回收」的并发模式。crossbeam 的 `epoch` 内存回收机制正是 RCU 思想的工程实现——让无锁数据结构能安全地回收被并发读取的旧节点。

---

### 4.2 子 crate 划分与门面重导出

#### 4.2.1 概念说明

crossbeam 不是一个单一的巨型 crate，而是一个 **workspace（工作区）**，由多个 **子 crate** 组成。这样做的好处是：

- **按需依赖**：如果你只需要通道，就只依赖 `crossbeam-channel`，不必把跳表、epoch 这些都拉进来，编译更快、二进制更小。
- **职责清晰**：每个子 crate 是一个独立的关注点（原子工具、通道、队列……）。
- **独立版本与发布**：每个子 crate 有自己的版本号和 CHANGELOG，可以单独发布到 crates.io。

但作为使用者，你通常不会去记 6 个 crate 名字。于是 crossbeam 提供了一个 **主 crate（也叫 `crossbeam`）**，它本身几乎不含实现，而是像一个「门面（facade）」，把各子 crate 的内容 **重导出（re-export）** 到统一的 `crossbeam::*` 命名空间下。这样你写 `use crossbeam::channel::unbounded;` 就能用到 `crossbeam-channel` 的功能。

#### 4.2.2 核心流程

子 crate 划分（共 6 个）：

| 子 crate | 职责 | 是否进入主 crate |
| --- | --- | --- |
| `crossbeam-utils` | 原子（AtomicCell/AtomicConsume）、同步原语（Parker/ShardedLock/WaitGroup）、作用域线程、Backoff/CachePadded | ✅ 是（且为非可选依赖） |
| `crossbeam-channel` | MPMC 通道与 select | ✅ 是（可选） |
| `crossbeam-deque` | 工作窃取双端队列 | ✅ 是（可选） |
| `crossbeam-epoch` | 基于 epoch 的内存回收 | ✅ 是（可选） |
| `crossbeam-queue` | 并发队列 ArrayQueue/SegQueue | ✅ 是（可选） |
| `crossbeam-skiplist` | 无锁跳表 Map/Set | ❌ **实验性，尚未纳入主 crate** |

门面重导出的「路由表」如下（这是本模块最重要的结论，建议记下）：

```
crossbeam::atomic       →  crossbeam_utils::atomic     （AtomicCell/AtomicConsume）
crossbeam::utils        →  crossbeam_utils::{Backoff, CachePadded}
crossbeam::scope        →  crossbeam_utils::thread::scope
crossbeam::thread       →  crossbeam_utils::thread
crossbeam::sync         →  crossbeam_utils::sync       （Parker/ShardedLock/WaitGroup）
crossbeam::channel      →  crossbeam_channel
crossbeam::select       →  crossbeam_channel::select
crossbeam::deque        →  crossbeam_deque
crossbeam::epoch        →  crossbeam_epoch
crossbeam::queue        →  crossbeam_queue
```

#### 4.2.3 源码精读

**① README 明确的子 crate 列表**

[README.md:L63-L83](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/README.md#L63-L83)

这段代码做了什么：列出 5 个被主 crate 重导出的子 crate，并特别强调 `crossbeam-skiplist` 是「**experimental** subcrate that is not yet included in `crossbeam`」（实验性子 crate，尚未纳入主 crate）。所以本讲规格里说「6 个子 crate」是算上了 skiplist，但你要清楚：**实际通过 `crossbeam::*` 能用到的只有 5 个**，skiplist 需要单独依赖 `crossbeam-skiplist`。

**② Cargo.toml：子 crate 是主 crate 的可选 path 依赖**

[Cargo.toml:L51-L56](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/Cargo.toml#L51-L56)

这段代码做了什么：声明主 crate 对 5 个子 crate 的依赖。要点有三：

- 每个依赖都用 `path = "crossbeam-xxx"`，即 **本地路径依赖**（因为是同一个 workspace），又指定 `version`，这样发布到 crates.io 时也能正确解析版本。
- 除 `crossbeam-utils` 外，其余子 crate 都标了 `optional = true`，意味着它们默认不编译，只在启用对应特性时才拉入。
- 所有依赖都设了 `default-features = false`，把「是否启用 std/alloc」的控制权交还给主 crate 的特性门控（见 4.3）。
- 注意 `crossbeam-utils` 是 **非可选** 的，因为它提供了最基础的原子工具，几乎所有路径都需要它。

**③ Cargo.toml：workspace 成员清单**

[Cargo.toml:L63-L74](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/Cargo.toml#L63-L74)

这段代码做了什么：定义 workspace 的全部成员，包括主 crate `"."` 自身、5 个功能子 crate、`crossbeam-skiplist`，以及 `crossbeam-channel/benchmarks`（通道的性能基准子项目）。这就是「整个仓库里到底有几个 crate」的权威答案。

**④ src/lib.rs：门面重导出的真身**

[src/lib.rs:L57-L79](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/src/lib.rs#L57-L79)

这段代码做了什么：把上面「路由表」用 Rust 代码落地。逐行看：

- 第 57 行 `pub use crossbeam_utils::atomic;` —— 无条件重导出原子模块（即使 `no_std` 也可用）。
- 第 59–66 行把 `Backoff`、`CachePadded` 包进 `crossbeam::utils` 模块再导出。
- 第 71–76 行用 `pub use { ... }` 一次性把 `channel`、`select`、`deque`、`sync` 重导出；它们都带 `#[cfg(feature = "std")]`，即 **只有启用 std 特性时才存在**。
- 第 77–79 行把 `epoch`、`queue` 重导出，带 `#[cfg(feature = "alloc")]`，即 **启用 alloc 特性即可，不一定需要完整 std**。

`#[doc(inline)]`（第 72、78 行）的作用是让文档站点把这些模块「摊平」显示，使用者在文档里看到的就像它们本来就长在 `crossbeam` 里一样。

#### 4.2.4 代码实践

**实践目标**：验证「主 crate 是门面」这一结论——同一个功能，既能通过 `crossbeam::*` 访问，也能通过子 crate 直接访问。

**操作步骤**：

1. 新建一个最小可执行项目（如果你不想新建，也可以只做「源码阅读型」步骤 2–3）：

   ```toml
   # Cargo.toml（示例代码，非项目原有文件）
   [dependencies]
   crossbeam = "0.8"
   crossbeam-channel = "0.5"
   ```

   ```rust
   // src/main.rs（示例代码）
   fn main() {
       // 路径 A：通过门面 crossbeam
       let (s1, r1) = crossbeam::channel::unbounded::<i32>();
       // 路径 B：直接用子 crate
       let (s2, r2) = crossbeam_channel::unbounded::<i32>();
       s1.send(1).unwrap();
       s2.send(2).unwrap();
       println!("{} {}", r1.recv().unwrap(), r2.recv().unwrap());
   }
   ```

2. 对照 [src/lib.rs:L71-L76](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/src/lib.rs#L71-L76)，确认 `crossbeam::channel` 确实就是 `crossbeam_channel` 的别名。
3. 在本仓库根目录执行 `cargo doc -p crossbeam --open`，在浏览器里观察 `crossbeam` 的模块树，确认 `channel`、`deque`、`epoch`、`queue`、`sync`、`utils` 都「内联」展示在顶层。

**需要观察的现象**：

- 步骤 1 里两条路径都能编译通过，运行输出 `1 2`。
- `cargo doc` 生成的文档里，`crossbeam::channel` 的页面与 `crossbeam_channel` 的页面内容一致（因为是同一个东西的重导出）。

**预期结果**：你亲眼确认了「主 crate 不含实现，只做重导出」，并且学会了两种等价的调用路径。**若本地无法联网编译依赖，步骤 1 标记为「待本地验证」，但步骤 2–3 的源码阅读部分总能完成。**

#### 4.2.5 小练习与答案

**练习 1**：为什么 `crossbeam-utils` 在主 crate 的依赖里没有 `optional = true`，而其它子 crate 都有？

> **答案**：因为 `crossbeam-utils` 提供的 `atomic`（AtomicCell 等）和 `utils`（Backoff/CachePadded）是 `no_std` 下也能用的基础工具，主 crate 在第 57、65 行无条件重导出它们，不依赖任何特性。因此它必须始终编译，不能设为可选。其余子 crate 都需要 `std` 或 `alloc` 特性才启用，故设为可选。

**练习 2**：`crossbeam-skiplist` 算不算主 crate 的一部分？

> **答案**：不算。README 明确写它是「experimental subcrate that is not yet included in `crossbeam`」。要用跳表，需直接依赖 `crossbeam-skiplist` 这个独立 crate。这也是为什么 `src/lib.rs` 里完全没有 skiplist 的重导出。

**练习 3**：`#[doc(inline)]` 在重导出时起什么作用？

> **答案**：它让 rustdoc 在生成文档时，把被重导出的模块「内联」展示在当前位置，而不是显示成一个需要点进去的超链接。这样用户在 `crossbeam` 的文档首页就能直接看到 `channel`、`epoch` 等模块的内容，门面看起来天衣无缝。

---

### 4.3 no_std / alloc / std 分级特性支持

#### 4.3.1 概念说明

Rust 的运行环境可以粗略分成三档：

- **`std`**：有完整的标准库，能用到线程、文件、网络、堆分配等一切能力。
- **`alloc`**：没有 `std`，但保留了 **堆分配** 能力（`Vec`、`Box`、`Arc` 等需要分配器的类型可用）。常见于某些嵌入式或内核场景，那里有分配器但没有完整 OS 服务。
- **`core`（即纯 `no_std`）**：连堆分配都没有，只能用最底层的、不依赖分配器的原语。

crossbeam 的工具「需要的运行环境各不相同」，所以它用 Cargo 的 **特性（feature）** 机制把能力分级。这对使用者很友好：如果你在写一个 `no_std` 固件，你也能用到 crossbeam 的 `AtomicCell`、`Backoff`、`CachePadded`。

对应到 Cargo 特性，crossbeam 主 crate 提供了两个特性开关：

- `std`：启用所有需要标准库的功能。**这是默认开启的。**
- `alloc`：启用需要堆分配的功能。`std` 会自动连带启用 `alloc`。

#### 4.3.2 核心流程

特性的「层层传递」关系如下（这是本模块最需要理解的设计）：

```
用户在 Cargo.toml 启用 crossbeam 的某个特性
        │
        ▼
主 crate 的 [features] 把它翻译成「子 crate 的对应特性」
        │
        ▼
子 crate 内部用 #[cfg(feature = "...")] 决定编译哪些代码

例如：
  crossbeam 的 std 特性
    → 连带启用 crossbeam 的 alloc 特性
    → 并向每个子 crate 传递各自的 std 特性
        crossbeam-channel/std
        crossbeam-deque/std
        crossbeam-epoch/std
        crossbeam-queue/std
        crossbeam-utils/std
```

而 `src/lib.rs` 顶部用 `#![no_std]` 声明主 crate 默认不依赖 `std`，再在需要时用 `#[cfg(feature = "std")] extern crate std;` 把 `std`「请回来」。这种写法是 Rust 里做「分级支持」的标准范式。

#### 4.3.3 源码精读

**① README：每个工具的环境要求标记**

[README.md:L45-L47](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/README.md#L45-L47)

这段代码做了什么：给出 <sup>(no_std)</sup> 与 <sup>(alloc)</sup> 两个标记的权威定义——带 <sup>(no_std)</sup> 的工具可在纯 `no_std` 环境使用；带 <sup>(alloc)</sup> 的工具可在 `no_std` 环境使用，但前提是启用了 `alloc` 特性。回顾 4.1 的工具表：`AtomicCell`、`AtomicConsume`、`Backoff`、`CachePadded` 带 <sup>(no_std)</sup>；`ArrayQueue`、`SegQueue`、`epoch` 带 <sup>(alloc)</sup>；其余（channel、deque、Parker 等）无标记，意味着需要完整 `std`。

**② Cargo.toml：主 crate 的特性定义**

[Cargo.toml:L33-L49](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/Cargo.toml#L33-L49)

这段代码做了什么：定义主 crate 的特性开关，是整个分级机制的「控制台」：

- `default = ["std"]`：默认启用 `std`，所以普通用户什么都不配就能用到全部功能。
- `std = ["alloc", "crossbeam-channel/std", ...]`：启用 `std` 时，**先连带启用 `alloc`**，再向每个子 crate 传递 `std` 特性。
- `alloc = ["crossbeam-epoch/alloc", "crossbeam-queue/alloc"]`：只启用 `alloc` 时，只把 `alloc` 传给真正需要它的两个子 crate（epoch 和 queue）。

注意这里没有出现 `crossbeam-channel/std` 之外的 channel 分级——因为 channel 完全依赖 `std`，不存在「只 alloc 不 std」的中间档。

**③ src/lib.rs：用 cfg 在代码层面落实分级**

[src/lib.rs:L41-L55](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/src/lib.rs#L41-L55)

这段代码做了什么：

- 第 41 行 `#![no_std]`：主 crate 默认按 `no_std` 编译，**不自动链接 `std`**。
- 第 54–55 行 `#[cfg(feature = "std")] extern crate std;`：只有启用 `std` 特性时，才显式引入标准库。

再看 4.2.3 里引用的 [src/lib.rs:L57-L79](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/src/lib.rs#L57-L79)：第 57 行的 `atomic`、第 59–66 行的 `utils` **没有任何 `cfg`**，所以它们在纯 `no_std` 下也存在；第 71–76 行的 channel/deque/sync 被 `#[cfg(feature = "std")]` 包住；第 77–79 行的 epoch/queue 被 `#[cfg(feature = "alloc")]` 包住。**代码层面的 `cfg` 与 Cargo.toml 的特性定义是完全对齐的。**

#### 4.3.4 代码实践

**实践目标**：亲手验证不同特性组合下，哪些 API 可用、哪些不可用。

**操作步骤**：

1. 在本仓库根目录执行默认构建，确认一切正常：

   ```bash
   cargo build -p crossbeam
   ```

2. 关闭默认特性、只开 `alloc`，观察变化：

   ```bash
   cargo build -p crossbeam --no-default-features --features alloc
   ```

3. 关闭默认特性、连 `alloc` 也不开（纯 `no_std`）：

   ```bash
   cargo build -p crossbeam --no-default-features
   ```

4. 对每一步，对照 [src/lib.rs:L57-L79](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/src/lib.rs#L57-L79) 的 `cfg` 标注，预测哪些模块（`atomic` / `utils` / `channel` / `deque` / `sync` / `epoch` / `queue` / `scope`）仍然存在。

**需要观察的现象**：

- 步骤 1（默认 std）：所有模块都存在，编译成功。
- 步骤 2（只 alloc）：`channel`、`deque`、`sync`、`scope` 会因 `#[cfg(feature = "std")]` 失效而消失，但 `atomic`、`utils`、`epoch`、`queue` 仍存在。
- 步骤 3（纯 no_std）：只剩 `atomic`、`utils`。

**预期结果**：你预测的存在/消失情况与编译结果一致。**如果你暂时无法运行这些命令，可标记为「待本地验证」，仅通过阅读 `cfg` 标注完成推理也能达到学习目的。**

#### 4.3.5 小练习与答案

**练习 1**：如果你在写一个 `no_std` 但有堆分配器的嵌入式程序，想用 crossbeam 的 `SegQueue`，该如何配置？

> **答案**：在 `Cargo.toml` 里关闭默认特性并开启 `alloc`：`crossbeam = { version = "0.8", default-features = false, features = ["alloc"] }`。因为 `SegQueue` 标记为 <sup>(alloc)</sup>，对应 `crossbeam-queue/alloc`，而 `alloc` 特性正好会传递它。注意此时 `channel`、`scope` 等 std 依赖项不可用。

**练习 2**：为什么 `std` 特性里要显式包含 `"alloc"`？

> **答案**：因为「有 std」必然意味着「也能分配堆」。`std` 内部就依赖 `alloc`。所以启用 `std` 时连带启用 `alloc`，确保那些只需要堆分配的工具（如 epoch、queue）也能在 std 环境下正常工作。这是一种特性之间的依赖关系。

**练习 3**：`#![no_std]` 写在 lib.rs 顶部，但 crossbeam 明明支持 `std`，这两者矛盾吗？

> **答案**：不矛盾。`#![no_std]` 只是让 crate 默认不链接 `std`，但代码可以用 `#[cfg(feature = "std")] extern crate std;` 在启用 std 特性时把标准库「请回来」。这是一种惯用手法：让 crate 默认保持 `no_std` 兼容，只在确实需要时才引入 `std`，从而同时服务 `no_std` 和 `std` 两类用户。

---

### 4.4 MSRV、许可证与版本约定

#### 4.4.1 概念说明

除了「能做什么」，使用一个库还要关心三件工程上的事：

- **MSRV（Minimum Supported Rust Version，最低支持的 Rust 版本）**：使用这个库至少要装多新的 Rust 工具链。这对生产环境很重要——你不能为了用一个库就随意升级编译器。
- **许可证（license）**：这个库在法律上允许你怎么用、怎么集成进你的产品。
- **版本约定**：版本号怎么变化，升级会不会破坏我的代码。

crossbeam 在这三方面都有明确的、值得信赖的约定。

#### 4.4.2 核心流程

crossbeam 的版本与兼容策略可以归纳为：

```
MSRV 策略：
  支持最近 6 个月以内的稳定版 Rust
  每次提高 MSRV，都发布一个新的 minor 版本
  当前 MSRV = 1.74

版本号约定（语义化版本 SemVer）：
  crossbeam 主 crate 当前版本 = 0.8.4
  各子 crate 各有独立版本号（如 crossbeam-utils 0.8.x）

许可证：
  MIT OR Apache-2.0（二选一双授权）
  这是 Rust 生态最常见的「双重许可」组合
```

#### 4.4.3 源码精读

**① Cargo.toml：版本、MSRV、许可证的权威字段**

[Cargo.toml:L7-L16](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/Cargo.toml#L7-L16)

这段代码做了什么：声明主 crate 的核心元信息——`version = "0.8.4"`、`edition = "2021"`、`rust-version = "1.74"`（即 MSRV）、`license = "MIT OR Apache-2.0"`。其中第 9 行的注释 `# NB: Sync with msrv badge and "Compatibility" section in README.md` 提醒维护者：**MSRV 改动时，Cargo.toml、README 徽章、README 兼容性段落三处必须同步**。这种「多处同步」的注释是大型项目维护质量的体现。

**② README：兼容性策略的文字说明**

[README.md:L93-L98](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/README.md#L93-L98)

这段代码做了什么：用人类语言解释 MSRV 策略——「支持至少 6 个月以内的稳定 Rust 版本；每次提高 MSRV 都会发布新 minor 版本；当前 MSRV 是 1.74」。这就给用户吃了一颗定心丸：**升级 Rust 版本不会是突袭式的**。

**③ CHANGELOG：MSRV 的演进历史**

[CHANGELOG.md:L1-L13](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/CHANGELOG.md#L1-L13)

这段代码做了什么：记录最近几个版本的变更。可以看到 0.8.3 版本的条目明确写了「Bump the minimum supported Rust version to 1.61」（把 MSRV 提高到 1.61），这正印证了「提高 MSRV 会发新版本」的约定。从 CHANGELOG 还能读出 crossbeam 的演进脉络：例如 0.8.0 一次性把各子 crate 的大版本对齐（channel 到 0.5、deque 到 0.8、epoch 到 0.9、queue 到 0.3、utils 到 0.8）。

**④ README：许可证声明**

[README.md:L142-L152](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/README.md#L142-L152)

这段代码做了什么：声明 crossbeam 在「Apache 2.0」和「MIT」两个许可证之间 **任选其一**（`Licensed under either of ... at your option`）。这两个都是宽泛、商业友好的开源许可证，意味着你几乎可以在任何项目（包括闭源商业项目）里使用 crossbeam。

#### 4.4.4 代码实践

**实践目标**：确认你本地的工具链满足 MSRV，并读懂许可证。

**操作步骤**：

1. 查看你本地的 Rust 版本：

   ```bash
   rustc --version
   ```

2. 对照 [Cargo.toml:L10](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/Cargo.toml#L10) 的 `rust-version = "1.74"`，判断你能否直接使用 crossbeam。
3. 阅读 [README.md:L142-L152](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/README.md#L142-L152)，确认 crossbeam 的双许可证组合。
4. 翻看 [CHANGELOG.md](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/CHANGELOG.md)，找出历史上 MSRV 至少被提高过几次、分别是哪个版本。

**需要观察的现象**：

- 步骤 1 显示的版本号 ≥ 1.74，则满足 MSRV。
- CHANGELOG 里 0.8.3 与 0.8.2 两个条目都提到了 MSRV 提升（分别到 1.61 和 1.38），可以追溯到 0.8.0 的 1.36。

**预期结果**：你能清楚地说出「crossbeam 当前 MSRV 是 1.74，许可证是 MIT 或 Apache-2.0 二选一」。**如果本地没有安装 Rust 工具链，步骤 1 标记为「待本地验证」。**

#### 4.4.5 小练习与答案

**练习 1**：crossbeam 把 MSRV 从 1.61 提到 1.74，按它的约定，这次提升会以什么形式发布？

> **答案**：发布一个新的 **minor** 版本（按 SemVer，0.x.y 里 y→y+1 不算，这里是 0.8.x 系列内的提升；严格说在 0.x 阶段，minor 与 patch 的语义比 1.x 之后更宽松，但 crossbeam 的约定是「每次提高 MSRV 都发新 minor 版本」）。README 明确写了这条策略，目的是让用户能通过版本号判断是否需要升级工具链。

**练习 2**：某公司想在一个闭源商业产品里静态链接 crossbeam，许可证允许吗？

> **答案**：允许。crossbeam 是 `MIT OR Apache-2.0` 双授权，两者都是商业友好的许可证，允许在闭源产品中使用、修改、分发，只需保留相应的许可证声明即可。你二选一遵守其中一个即可。

**练习 3**：为什么 Cargo.toml 里要用一条注释提醒「MSRV 改动要同步 README 徽章」？

> **答案**：因为 MSRV 信息同时出现在三处——`Cargo.toml` 的 `rust-version` 字段、README 顶部的徽章（`Rust 1.74+`）、README 的 Compatibility 段落。它们必须保持一致，否则会误导用户。这条注释是给维护者看的「防呆」提醒，体现了项目对文档准确性的重视。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「项目导览」小任务：

**任务**：假设你要给团队的一位新同事用 5 分钟介绍 crossbeam，请准备一份「一页速查表」，必须包含以下信息，且每条都要能指到具体源码出处：

1. **一句话定位**：crossbeam 是什么、和 `std::sync::mpsc` 的关键差别。（依据：4.1）
2. **五大分类速查**：列出 Atomics / Data structures / Memory management / Thread synchronization / Utilities 各自的代表工具，并标注哪些是 <sup>(no_std)</sup>、哪些是 <sup>(alloc)</sup>。（依据：[README.md:L15-L47](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/README.md#L15-L47)）
3. **子 crate 与门面路由**：画一张「`crossbeam::xxx` → 子 crate」的路由表，并说明 skiplist 为何不在其中。（依据：4.2）
4. **三级特性**：用一句话说清 `no_std` / `alloc` / `std` 三档的区别，并给出「想在 no_std+alloc 下用 SegQueue」的依赖配置。（依据：4.3）
5. **工程信息**：当前 MSRV、版本号、许可证。（依据：[Cargo.toml:L7-L16](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/Cargo.toml#L7-L16)）

**验收标准**：

- 每一条都能说出「这个结论来自哪个文件的哪几行」。
- 同事听完能回答：「crossbeam 能不能在我的 no_std 固件里用？能用哪几个工具？」（答：能，可用 `AtomicCell`、`AtomicConsume`、`Backoff`、`CachePadded`；若固件有分配器并开 `alloc` 特性，还能用 `epoch`、`ArrayQueue`、`SegQueue`。）

这个任务不写一行代码，但它逼着你把「定位—分类—结构—分级—工程」这条主线在脑子里走一遍，这正是本讲想交付给你的「项目全景地图」。

## 6. 本讲小结

- **crossbeam 是一套比标准库更全面、更精细的并发编程工具集**，擅长无锁数据结构与基于 epoch 的内存回收，关键词是 `atomic / lock-free / rcu`。
- 它的工具按 **五大类** 组织：原子、数据结构、内存管理、线程同步、工具；其中内存管理只有 `epoch` 一个，却是最核心的一块。
- crossbeam 是一个 **workspace**，由 6 个子 crate 组成；**主 crate 用门面（facade）模式把它们重导出为统一的 `crossbeam::*`**，但实验性的 `crossbeam-skiplist` 目前尚未纳入主 crate。
- 工具按运行环境要求分为 **`no_std` / `alloc` / `std` 三级**，通过 Cargo 特性层层传递，`src/lib.rs` 顶部用 `#![no_std]` + `#[cfg(feature)]` 落实分级。
- 当前 **MSRV 为 Rust 1.74**，版本 0.8.4，许可证为 `MIT OR Apache-2.0` 双授权；每次提高 MSRV 都会发新版本，升级不会突袭。
- 本讲只看了「门面层」文件（README、Cargo.toml、src/lib.rs、CHANGELOG），**尚未进入任何算法实现**——那是后续讲义的内容。

## 7. 下一步学习建议

本讲建立的是「平面图」，接下来你应该：

- 如果想先动手写并发代码：直接跳到 **u1-l4（作用域线程 scope 快速上手）**，它是全手册里最容易上手、也最能立刻体会到 crossbeam 价值的入口；不过建议先读 **u1-l2（工作区、构建与特性系统）** 和 **u1-l3（主 crate 重导出门面）**，把本讲的门面理解再夯实一层。
- 如果想系统理解每个子 crate：按本手册的单元顺序，第 2 单元进入 `crossbeam-utils` 的并发原语（Backoff、CachePadded、AtomicCell、Parker 等），它们是其它数据结构的公共依赖，是最佳的第二站。
- 建议的延伸阅读：阅读 [README.md:L126-L134](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/README.md#L126-L134) 提到的「Learning resources」与 RFC 仓库，建立对并发与无锁数据结构背景知识的整体认识。

> 一句话锚点：本讲之后，你已经知道 crossbeam「有什么、怎么组织、能在哪跑」；下一阶段，我们要开始拆开它「怎么实现」。
