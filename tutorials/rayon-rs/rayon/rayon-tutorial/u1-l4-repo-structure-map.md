# u1-l4 仓库结构与代码地图

## 1. 本讲目标

学完本讲，你应该能够：

1. 画出 `rayon`、`rayon-core`、`rayon-demo` 三个 crate 的分层关系图，并说出依赖为什么是单向的。
2. 说出 `src/`（rayon crate）下 `iter`、`slice`、`collections`、`str` 等目录各自的职责，理解「镜像 std」的目录设计哲学。
3. 说出 `rayon-core/src/` 下 `job`、`latch`、`registry`、`join`、`scope`、`spawn`、`sleep`、`thread_pool`、`broadcast`、`unwind` 十个模块各自的职责。
4. 在源码中快速定位三大核心 API 的定义位置：`ParallelIterator`、`join`、`ThreadPool`。
5. 读懂 `pub use` 再导出（re-export）链条，能从 `rayon::join` 一路追到它在 `rayon-core` 中的真实定义文件。

本讲是纯粹的「地图课」：不改代码、不写算法，只建立一张在你后续阅读所有讲义时都能随时对照的仓库全景图。

## 2. 前置知识

本讲需要两个你已经具备的基础和两个新概念。

**已具备（来自前三讲）：**

- `u1-l1`：Rayon 是数据并行库，API 分并行迭代器、`join`、`scope`、`ThreadPool` 四级。
- `u1-l2`：仓库是三包 Cargo workspace，`rayon` 依赖 `rayon-core`，`rayon-demo` 是 `publish = false` 的演示程序，依赖严格单向。

**新概念一：模块系统与再导出。**
Rust 中每个 `.rs` 文件就是一个模块（module），用 `mod xxx;` 声明子模块，用 `pub mod xxx;` 让它对外可见。如果一个 crate 想把另一个 crate 的项「挂到自己名下」方便用户使用，就写 `pub use other_crate::Item;`，这叫**再导出（re-export）**。例如你在代码里写的 `rayon::join`，其实真正的定义在 `rayon_core::join`——`rayon` 只是把它转发出来。本讲的实践任务就是把这些转发关系全部标注出来。

**新概念二：门面模式（facade）。**
`rayon` crate 本身几乎不含调度逻辑，它是一个「门面」：对外提供统一入口（`par_iter`、`join`、`ThreadPool`……），对内把稳定的底层 API 从 `rayon-core` 转发上来，自己再叠加并行迭代器这一大块高层功能。好处是：内核可以独立演进（甚至被其他框架单独引用），用户只需要记住一个 crate 名。

一个术语约定：下文用 **rayon crate** 指 `src/` 目录对应的包，用 **rayon-core** 指 `rayon-core/src/` 对应的包，避免和项目总名「Rayon」混淆。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
|---|---|---|
| `src/lib.rs` | rayon crate 的根：模块声明 + 从 rayon-core 的再导出 | L84-L105 模块清单，L107-L118 再导出清单 |
| `rayon-core/src/lib.rs` | rayon-core 的根：十个内部模块 + 对外再导出 | L69-L81 模块清单，L83-L92 再导出清单 |
| `src/iter/mod.rs` | 并行迭代器核心：两个大头 trait + 全部适配器子模块 | `ParallelIterator` 与 `IndexedParallelIterator` 的定义行号 |
| `rayon-demo/src/main.rs` | 演示程序入口：命令行分发到各 demo | 模块组织与 `main` 的 match 分发 |
| `src/slice/mod.rs`（辅助） | 切片并行能力的入口 | `ParallelSlice` / `ParallelSliceMut` trait 定义 |
| `src/iter/plumbing/mod.rs`（辅助） | 迭代器底层「水管设施」 | `Producer` trait，第四单元的主角 |
| `rayon-core/src/registry.rs`（辅助） | 线程注册表 | `Registry` 结构体与全局静态 `THE_REGISTRY` |
| `rayon-core/src/join/mod.rs`（辅助） | `join` 原语 | `pub fn join` 的精确位置 |
| `rayon-core/src/thread_pool/mod.rs`（辅助） | `ThreadPool` 类型 | `pub struct ThreadPool` 的精确位置 |

## 4. 核心概念与源码讲解

### 4.1 三层分层图

#### 4.1.1 概念说明

仓库由三个 crate 组成，依赖严格单向，形成三层：

```
┌─────────────────────────────────────────────┐
│  rayon-demo （可执行演示程序，publish=false） │  ← 第三层：示例与基准
│  matmul / nbody / sieve / quicksort ...      │
└──────────────────┬──────────────────────────┘
                   │ 依赖
                   ▼
┌─────────────────────────────────────────────┐
│  rayon （src/ 目录，用户面对的门面）          │  ← 第二层：并行迭代器 + API 转发
│  iter/  slice/  str/  collections/  range... │
└──────────────────┬──────────────────────────┘
                   │ 依赖
                   ▼
┌─────────────────────────────────────────────┐
│  rayon-core （调度内核，可独立发布）          │  ← 第一层：线程池与任务调度
│  registry  job  latch  join  scope  sleep... │
└─────────────────────────────────────────────┘
```

三层各自解决一个问题：

- **rayon-core**：「怎么把一个闭包安全地放到别的线程上去跑，并且在跑完后把结果和 panic 都带回来」。它完全不知道 `par_iter` 的存在。
- **rayon crate**：「怎么把『遍历一堆数据』这类常见计算自动切分成无数个小闭包，交给 rayon-core 调度」。它包含全部并行迭代器逻辑，并把 rayon-core 的稳定 API 转发给用户。
- **rayon-demo**：「怎么证明这一切真的更快」。八个可直接运行的演示加十个仅基准测试的模块。

为什么依赖必须单向？因为并行迭代器需要调用 `join` 来实现任务切分，反过来调度内核绝不需要知道迭代器的存在。单向依赖让 rayon-core 可以被其他想自建调度器的项目单独引用，也让两个 crate 能各自发版。

#### 4.1.2 核心流程

以 `u1-l3` 中你写过的 `input.par_iter().map(|i| i * i).sum()` 为例，这行代码在三层中的落点：

```text
用户代码
  │  input.par_iter()
  ▼
rayon crate ── src/slice/mod.rs 提供切片的并行迭代入口，
  │             产出并行迭代器；map/sum 落在 src/iter/mod.rs 的 trait 方法上
  │             执行时把数据切分成两半，形成分治递归
  ▼
rayon-core ── 每一层「切一半」最终调用 join(a, b)
  │             join 把闭包装箱成 Job，放入工作窃取队列
  ▼
registry / sleep ── 工作线程从队列取任务执行；
                    空闲线程窃取任务；无事可做时休眠
```

本讲只要求记住这条链的**每一站所在的目录**，不要求理解内部实现——那是第五、六单元的内容。

#### 4.1.3 源码精读

先看最顶层的演示程序如何组织。[rayon-demo/src/main.rs:6-14](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/main.rs#L6-L14) 声明了八个常驻演示模块，而 [rayon-demo/src/main.rs:18-37](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/main.rs#L18-L37) 用 `#[cfg(test)]` 又挂了十个只在测试时编译的基准模块——这正是 `u1-l2` 里「fibonacci 只能在 nightly bench 下运行」的源码依据。

[rayon-demo/src/main.rs:81-91](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-demo/src/main.rs#L81-L91) 是命令行分发：`main` 读第一个参数，用 `match` 把 `"matmul"`、`"nbody"` 等名字转发给对应子模块的 `main` 函数，未知名字走 `usage()` 打印用法并以退出码 1 结束。所以 demo 没有统一的 benchmark 框架，每个演示自治。

再看门面层的关键证据——rayon crate 根部对 rayon-core 的再导出。[src/lib.rs:107-118](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/lib.rs#L107-L118) 一共 12 条 `pub use rayon_core::...`，把 `join`、`scope`、`spawn`、`ThreadPool`、`ThreadPoolBuilder`、`broadcast` 等全部稳定 API 转发到 `rayon` 名下：

```rust
pub use rayon_core::ThreadPool;
pub use rayon_core::ThreadPoolBuilder;
pub use rayon_core::{Scope, in_place_scope, scope};
pub use rayon_core::{join, join_context};
pub use rayon_core::{spawn, spawn_fifo};
// ... 共 12 条
```

这就是「用户只需要 `use rayon::prelude::*` 加 `rayon::join`」的实现的全部秘密：没有复制代码，只有名字转发。

最后看一个容易被忽略的细节：`ThreadPoolBuilder` 这个类型并不在 `rayon-core` 的某个子模块里，而是直接定义在 [rayon-core/src/lib.rs:165-197](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L165-L197)——它保存线程数、线程名闭包、栈大小、panic 处理器等九个配置字段。跟踪 re-export 时要留意：并非所有公开项都来自子模块，少数类型就住在 crate 根部（`FnContext` 也是，见 [rayon-core/src/lib.rs:836](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L836)）。

#### 4.1.4 代码实践

**实践目标**：用工具验证依赖方向确实是单向的（而不是只听讲义说）。

**操作步骤**：

1. 在仓库根目录执行 `cargo tree -p rayon-demo -e normal --depth 1`。
2. 再执行 `cargo tree -p rayon --depth 1`。
3. 打开根目录 `Cargo.toml`，找到 `[workspace.dependencies]` 或各子包的 `dependencies` 段，对照 `rayon = { path = "rayon-core", version = "..." }` 这类双约束写法（`u1-l2` 已讲过其含义）。

**需要观察的现象**：

- `rayon-demo` 的依赖树里同时出现 `rayon` 和 `rayon-core` 吗？还是只出现 `rayon`？
- `rayon` 的依赖树里是否出现任何指回 `rayon-demo` 或彼此循环的边？

**预期结果**：`rayon-demo` 依赖 `rayon`（`rayon` 再依赖 `rayon-core`）；依赖图是一棵树，没有环。`cargo tree` 的具体输出格式随 Cargo 版本略有差异，待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：如果让你把 `rayon-demo` 删掉，`rayon` 和 `rayon-core` 还能编译吗？反过来呢？

答案：删掉 `rayon-demo` 完全不影响另外两个包编译——依赖图中没有任何包依赖它，它是纯叶子节点。反过来删 `rayon-core` 则 `rayon` 与 `rayon-demo` 都无法编译，因为整条依赖链的根没了。

**练习 2**：为什么 `rayon` 要费力地把 `rayon-core` 的 API 再导出，而不是让用户直接依赖 `rayon-core`？

答案：一是用户体验——只记一个 crate 名、只写一行依赖；二是解耦——`rayon-core` 保持极简（运行时仅依赖 crossbeam 系列），可被其他调度需求单独引用，`rayon` 则专注高层迭代器，两边可以按各自节奏发版；三是 `rayon-core` 用 `links` 强制全局唯一（`u1-l2` 讲过），统一入口也降低了用户直接引多个版本的风险。

### 4.2 src 目录导览：rayon crate

#### 4.2.1 概念说明

rayon crate 的 `src/` 目录遵循一条明确设计原则：**镜像 std 的模块结构**。[src/lib.rs:55-68](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/lib.rs#L55-L68) 的文档注释原话是：rayon 的模块镜像 `std` 自身——`option` 模块对应 `std::option`，`collections` 模块对应 `std::collections`。所以你找某个类型的并行支持时，「std 里它在哪个模块，rayon 里它就大概在哪个模块」。

目录可分四类：

| 类别 | 文件/目录 | 职责 |
|---|---|---|
| 数据源适配 | `slice/`、`str.rs`、`string.rs`、`vec.rs`、`array.rs`、`option.rs`、`result.rs`、`range.rs`、`range_inclusive.rs`、`collections/`、`par_either.rs` | 为 std 的各类容器实现并行迭代入口 |
| 迭代器核心 | `iter/`（约 60 个 `.rs` 文件） | 两个核心 trait、全部适配器与消费者 |
| 基础设施 | `delegate.rs`、`split_producer.rs`、`math.rs` | 宏与工具，被上面的模块复用 |
| 对外入口 | `lib.rs`、`prelude.rs` | 模块声明、re-export、prelude 汇总 |

#### 4.2.2 核心流程

`iter/` 目录的组织逻辑值得单独讲。它内部一条主线是「一个适配器一个文件」：

```text
src/iter/
├── mod.rs          ← ParallelIterator + IndexedParallelIterator 两个 trait
│                      以及所有 trait 方法的默认实现
├── map.rs          ← Map 适配器（map 方法的返回类型）
├── filter.rs       ← Filter 适配器
├── fold.rs / reduce.rs / sum.rs / ...
├── collect/        ← collect 的实现（带子模块）
├── plumbing/       ← Producer/Consumer 底层协议（第四单元主角）
└── ... 共约 60 个文件
```

当你写 `.map(...)` 时，编译器把迭代器包装成 `iter::map::Map` 类型；再 `.filter(...)` 时继续包装成 `iter::filter::Filter<Map<...>>`。**类型即图层**：链式调用有多长，最终类型名就有多长，而每一层类型都能在同名文件里找到。这个「按方法组织文件」的约定就是 `src/iter/` 的阅读地图。

#### 4.2.3 源码精读

rayon crate 的全部模块声明集中在 [src/lib.rs:84-105](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/lib.rs#L84-L105)。注意声明分两档：`pub mod`（用户可见，如 `iter`、`slice`、`collections`）和裸 `mod`（私有基础设施，如 `delegate`、`math`、`split_producer`）：

```rust
#[macro_use]
mod delegate;          // 私有：委托宏，供适配器消除样板代码

pub mod array;         // 公开：镜像 std 的各数据源模块
pub mod collections;
pub mod iter;
pub mod slice;
pub mod str;
pub mod vec;
// ...
```

三大定位目标之一的 **`ParallelIterator`** 定义在 [src/iter/mod.rs:359](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L359)。它是「并行版 `Iterator`」，全部通用方法（`map`、`for_each`、`filter`、`fold`、`sum`……）都以默认方法的形式写在 trait 体里，所以这个文件长达两千多行：

```rust
pub trait ParallelIterator: Sized + Send {
    /// The type of item that this parallel iterator produces.
    type Item: Send;
    ...
}
```

注意 `type Item: Send` 这个约束——产出元素必须能跨线程移动，这是 `u1-l1` 讲的「数据竞争自由由类型系统保证」在 trait 签名上的直接体现。

带索引的增强版 **`IndexedParallelIterator`** 定义在 [src/iter/mod.rs:2449](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L2449)，文档说明它是「支持随机访问、可在任意下标处切分」的迭代器。`zip`、`enumerate`、`collect` 等需要预知长度的操作只在这个 trait 上提供——经过 `filter` 之后元素个数未知，这些方法就会不可用，这是 `u2-l1` 的主题。

适配器子模块的组织约定写在 [src/iter/mod.rs:96-105](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L96-L105) 的一段注释里，作者称之为「madness 中的方法」：出现在公共 API 上的类型（如 `Enumerate`）在本模块内**一律不带前缀使用**，强迫维护者补上 `pub use`，漏了就编译报错；只出现在方法体内的辅助函数（如 `find::find()`）则**一律带前缀**，一眼可分。随后 [src/iter/mod.rs:107-154](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L107-L154) 按字母序列出全部私有适配器子模块（`mod blocks; mod chain; mod chunks; ...`）。

切片是最重要的数据源，入口是 [src/slice/mod.rs:31](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/mod.rs#L31) 的 `ParallelSlice` 与 [src/slice/mod.rs:222](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/mod.rs#L222) 的 `ParallelSliceMut`，提供 `par_split`、`par_windows` 和一族并行排序方法；`slice/` 目录下还有 `sort.rs`（约 1600 行的并行归并排序，`u8-l2` 的主角）。

最后，`iter/plumbing/` 是驱动一切的底层协议。[src/iter/plumbing/mod.rs:56](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L56) 定义 `Producer` trait，文档称其为「可切分的 `IntoIterator`」——`split_at` 把数据一分为二正是任务切分的物理基础。现在只需记住位置，第四单元会精读。

#### 4.2.4 代码实践

**实践目标**：建立「方法名 → 实现文件」的肌肉记忆，验证「一个适配器一个文件」的约定。

**操作步骤**：

1. 在 `src/iter/` 目录下盲猜以下方法对应的文件名：`map`、`filter`、`enumerate`、`flat_map`、`panic_fuse`、`with_min_len`。
2. 用 `ls src/iter/` 对照你的猜测（或用编辑器全局搜索 `mod map;`）。
3. 对 `with_min_len` 这种猜不到独立文件的，在 `src/iter/mod.rs` 里搜索 `fn with_min_len`，看它定义在哪个 trait 上、返回类型来自哪个文件（提示：`len.rs`）。

**需要观察的现象**：除 `with_min_len` 这类「配置切分粒度」的方法外，绝大多数适配器方法都能直接命中同名文件。

**预期结果**：`map.rs`、`filter.rs`、`enumerate.rs`、`flat_map.rs`、`panic_fuse.rs` 全部存在；`with_min_len` 没有同名文件，而是定义在 `IndexedParallelIterator` 上，配套类型在 `len.rs`。

#### 4.2.5 小练习与答案

**练习 1**：`Vec<T>` 的并行支持在哪个文件？`HashMap<K, V>` 呢？

答案：`Vec` 对应 `src/vec.rs`，`HashMap` 对应 `src/collections/hash_map.rs`。依据是镜像 std：`Vec` 在 std 里是顶层类型（所以 rayon 里是顶层文件），`HashMap` 在 `std::collections` 里（所以 rayon 里在 `collections/` 目录下）。

**练习 2**：为什么 `ParallelIterator` 的方法都写成 trait 默认方法，集中在一个两千多行的文件里，而不是像适配器那样每方法一个文件？

答案：因为这些方法分两类：惰性适配器方法只做类型包装，一行 `Map::new(self, map_op)` 就完事，实现体在各自适配器文件里；立即执行的消费者方法（`sum`、`collect`、`for_each`）则在此处调用底层实现函数。trait 的方法签名必须集中声明（Rust 不允许 trait 方法分散在多个文件），而方法**体**可以只是转发——所以 `mod.rs` 是「方法目录」，适配器文件是「方法实现」。

**练习 3**：`src/delegate.rs` 是私有的（裸 `mod`），它定义的宏如何被其他模块使用？

答案：见 [src/lib.rs:84-85](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/lib.rs#L84-L85) 的 `#[macro_use] mod delegate;`——`#[macro_use]` 让该模块内定义的宏在声明之后的所有模块中可用，无需路径引用。这是宏（旧版 `macro_rules!`）与普通项的作用域差异。

### 4.3 rayon-core 目录导览：调度内核

#### 4.3.1 概念说明

rayon-core 只回答一个问题：**给定一堆闭包，怎么让一组线程把它们高效、安全地跑完**。它的公开 API 面很窄（`join`、`scope`、`spawn`、`ThreadPool`、`broadcast` 及若干线程信息函数），内部却由十个模块精密配合。这一节先给每个模块发一张「职责卡片」，你在第五、六单元精读时会反复回来看这张表。

| 模块 | 职责 | 精读讲义 |
|---|---|---|
| `job.rs` | 任务对象：闭包如何被装箱成可入队的 `JobRef` | u5-l2 |
| `latch.rs` | 唤醒原语：任务完成后如何通知等待者 | u5-l2 |
| `join/` | 最小并行原语 `join`/`join_context` | u5-l1 |
| `registry.rs` | 线程注册表：全局线程池、工作窃取主循环 | u5-l3、u5-l4 |
| `sleep/` | 休眠唤醒协议：无事可做时省 CPU | u5-l5 |
| `scope/` | 借用安全的作用域任务 | u6-l1 |
| `spawn/` | fire-and-forget 任务派发 | u6-l2 |
| `broadcast/` | 向池内每个线程广播任务 | u6-l3 |
| `thread_pool/` | `ThreadPool` 类型与 `install`、线程信息函数 | u7-l2、u7-l3 |
| `unwind.rs` | panic 的捕获与跨线程重放 | u6-l4 |

#### 4.3.2 核心流程

十个模块在一次 `join(a, b)` 调用中的协作（只看数据流向，不看实现）：

```text
join/mod.rs     join(oper_a, oper_b) 被调用
   │
   ▼
job.rs          oper_b 被装箱成 Job，得到 JobRef
   │
   ▼
registry.rs     JobRef 压入当前线程的本地 deque（工作窃取队列）
   │             当前线程先执行 oper_a；
   │             其他空闲线程可从队列另一端窃取 oper_b
   ▼
latch.rs        oper_b 执行完毕后 set 对应 Latch，
   │             唤醒可能正在等待结果的线程
   ▼
unwind.rs       若任一闭包 panic，先捕获、
                等另一分支也跑完后在 join 调用处重放
```

线程本身则由 `registry.rs` 创建并纳入管理；线程没事做时进入 `sleep/` 模块描述的休眠状态，被新任务唤醒。这条链是第五单元的主线，现在只要能把每个动词对应到模块名即可。

#### 4.3.3 源码精读

十个内部模块的声明在 [rayon-core/src/lib.rs:69-81](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L69-L81)，全部是私有 `mod`——rayon-core 的用户看不到模块边界，只能通过根部的再导出访问：

```rust
mod broadcast;
mod job;
mod join;
mod latch;
mod registry;
mod scope;
mod sleep;
mod spawn;
mod thread_pool;
mod unwind;
```

紧随其后的 [rayon-core/src/lib.rs:83-92](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L83-L92) 是十来条 `pub use self::模块::...`，把各模块的公开项提升到 crate 根。**每条 `pub use self::X` 就是下一站路标**：`pub use self::join::{join, join_context};` 告诉你 `join` 函数定义在 `join/` 目录下。

三大定位目标在 rayon-core 中的精确落点：

- **`join`**：[rayon-core/src/join/mod.rs:93-106](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/join/mod.rs#L93-L106)。签名 `pub fn join<A, B, RA, RB>(oper_a: A, oper_b: B) -> (RA, RB)`，四个泛型参数上的 `Send` 约束清晰可见；函数体只有一行——转调 `join_context`，把无上下文版本适配成带上下文版本（[rayon-core/src/join/mod.rs:115](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/join/mod.rs#L115)）。
- **`ThreadPool`**：[rayon-core/src/thread_pool/mod.rs:46](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/thread_pool/mod.rs#L46)，`pub struct ThreadPool`（内部包着一个 `Arc<Registry>`）。
- **`Registry`**：[rayon-core/src/registry.rs:126](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L126)。它是包级私有类型（`pub(super)`），外部世界只能通过 `ThreadPool` 间接使用——而全局唯一的实例存放在 [rayon-core/src/registry.rs:154-155](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L154-L155) 的静态变量 `THE_REGISTRY` 中，配合 `Once` 保证只初始化一次。`u1-l2` 讲的「全局线程池惰性初始化」的落点就是这两行。

顺带一提，[rayon-core/src/lib.rs:39-55](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L39-L55) 的文档解释了为什么这个 crate 用 `links` 属性防止重复链接——若两个版本同时存在，就会有两个「全局」线程池，协调即失效。这是分层设计里内核必须唯一的根源。

#### 4.3.4 代码实践

**实践目标**：为 rayon-core 的十个模块制作职责卡片，并验证 re-export 路标。

**操作步骤**：

1. 对照 [rayon-core/src/lib.rs:69-81](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L69-L81) 的模块清单，逐个打开文件，只读每个文件开头的 `//!` 文档注释（不读实现）。
2. 给每个模块写一句话职责（可抄改 4.3.1 的表格，但必须核对文档注释原文）。
3. 挑 `pub use self::spawn::{spawn, spawn_fifo};` 这一条验证：打开 `rayon-core/src/spawn/mod.rs`，用编辑器跳转到 `pub fn spawn` 的定义行。

**需要观察的现象**：`spawn/mod.rs` 里除了 `spawn` 还有 `spawn_fifo`；两者签名相同但入队位置不同（一个栈序一个队序，`u6-l2` 详述）。

**预期结果**：`pub fn spawn<F>(func: F)` 位于 [rayon-core/src/spawn/mod.rs:58](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/spawn/mod.rs#L58)，与 re-export 路标完全一致。

#### 4.3.5 小练习与答案

**练习 1**：`current_thread_index()` 这个函数，用户从 `rayon` 名下调用。请按 re-export 链写出它的完整路径。

答案：`rayon::current_thread_index`（[src/lib.rs:116](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/lib.rs#L116) 再导出）→ `rayon_core::current_thread_index`（[rayon-core/src/lib.rs:91](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L91) 再导出）→ 最终定义在 `rayon_core::thread_pool::current_thread_index`（[rayon-core/src/thread_pool/mod.rs:438](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/thread_pool/mod.rs#L438)）。两跳转发，一个定义点。

**练习 2**：`Registry` 为什么是 `pub(super)` 而不是 `pub`？

答案：`Registry` 承载全局线程池和工作窃取队列的内部状态，暴露它会使用户绕过 `ThreadPool` 的安全封装直接操作队列。`pub(super)` 让它只在 rayon-core 内部（含子模块）可见，对外只留下 `ThreadPool` 这个把手——这是「窄接口、深实现」的封装原则。

**练习 3**：不看 4.3.1 的表格，说出 `sleep/` 模块存在的意义。

答案：工作线程找不到任务时不应该忙等烧 CPU，也不应该直接退出（新任务来了还得重建线程）。`sleep/` 实现了一套休眠-唤醒协议：线程空闲一段时间后休眠在条件变量上，registry 注入新任务时批量唤醒。它要在「省电」和「不损失唤醒及时性」之间维护几个原子计数器的不变量，是 `u5-l5` 的全部内容。

## 5. 综合实践

这是本讲指定的核心实践任务：**手工绘制仓库模块依赖图，并把两份 re-export 清单的每一条标注到来源子模块**。

### 实践目标

产出两张可以贴在显示器旁的成果：

1. 一张三层模块依赖图（含每层内部的主要目录）。
2. 一张「re-export 来源对照表」，从 `rayon::X` 出发能查到 `X` 的最终定义文件。

### 操作步骤

**第一步：画依赖图。** 用纸笔或任意画图工具，画出如下骨架并自己补全每个框内的目录名（参考 4.1.1 的 ASCII 图，但这次要求把 `src/iter`、`src/slice`、`rayon-core/src/registry` 等目录都画进去，并标注每条依赖边）。

**第二步：标注 `src/lib.rs` 的 12 条再导出。** 打开 [src/lib.rs:107-118](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/lib.rs#L107-L118)，对每条 `pub use rayon_core::X` 查 [rayon-core/src/lib.rs:83-92](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L83-L92)，确定 `X` 来自哪个 `self::模块`。

**第三步：处理例外。** 有两条在 rayon-core 的 `pub use self::...` 清单里找不到，需要单独处理（提示见下方答案表）。

**第四步：抽查两条链。** 任选两个项（建议 `join` 和 `Yield`），从 `rayon::` 名下出发，用编辑器「转到定义」逐跳验证你标注的终点是否正确。

### 需要观察的现象

- 绝大多数项走「子模块 → rayon-core 根 → rayon 根」的两跳路线；
- 少数项只有一跳（直接定义在 rayon-core 根部）；
- 编辑器的「转到定义」有时一步直达最终文件（编译器内联了 re-export），有时停在转发行——两种都正常。

### 预期结果（参考答案表）

| `rayon::` 下的名字 | rayon-core 内来源 | 最终定义文件 |
|---|---|---|
| `join`, `join_context` | `self::join` | `rayon-core/src/join/mod.rs`（L93、L115） |
| `scope`, `in_place_scope`, `Scope` | `self::scope` | `rayon-core/src/scope/mod.rs` |
| `scope_fifo`, `in_place_scope_fifo`, `ScopeFifo` | `self::scope` | `rayon-core/src/scope/mod.rs` |
| `spawn`, `spawn_fifo` | `self::spawn` | `rayon-core/src/spawn/mod.rs`（L58） |
| `broadcast`, `spawn_broadcast`, `BroadcastContext` | `self::broadcast` | `rayon-core/src/broadcast/mod.rs` |
| `ThreadPool` | `self::thread_pool` | `rayon-core/src/thread_pool/mod.rs`（L46） |
| `current_thread_index`, `current_thread_has_pending_tasks`, `Yield`, `yield_now`, `yield_local` | `self::thread_pool` | `rayon-core/src/thread_pool/mod.rs`（L438、L452、L497） |
| `ThreadBuilder` | `self::registry` | `rayon-core/src/registry.rs`（L22） |
| `ThreadPoolBuilder`, `ThreadPoolBuildError`, `FnContext` | 无（直接定义在根部） | `rayon-core/src/lib.rs`（L165、L137、L836） |
| `current_num_threads`, `max_num_threads` | 无（根部函数，转发给 registry） | `rayon-core/src/lib.rs`（L108-L133） |

依赖图则应呈现：`rayon-demo → rayon → rayon-core`，无环；`rayon` 内部 `iter/slice/... → delegate/split_producer`；`rayon-core` 内部 `join/scope/spawn/broadcast/thread_pool → job/latch/registry → sleep/unwind`（这条内部依赖在第五单元还会细化）。

## 6. 本讲小结

- 仓库是三层单向依赖：`rayon-demo → rayon → rayon-core`；rayon 是门面，rayon-core 是可独立发布的调度内核，rayon-demo 是纯叶子。
- rayon crate 的 `src/` **镜像 std 的模块结构**：`option.rs`、`collections/`、`str.rs`……找类型先想它在 std 的哪个模块。
- `src/iter/` 遵循「一个适配器一个文件」约定，`mod.rs` 集中声明两个核心 trait：`ParallelIterator`（L359）与 `IndexedParallelIterator`（L2449）。
- rayon-core 由十个私有模块组成，通过根部的 `pub use self::模块::...` 对外暴露窄接口；`pub use self::X` 就是定位定义文件的路标。
- 三大 API 的定义落点：`ParallelIterator` 在 `src/iter/mod.rs:359`，`join` 在 `rayon-core/src/join/mod.rs:93`，`ThreadPool` 在 `rayon-core/src/thread_pool/mod.rs:46`。
- `Registry` 是 `pub(super)` 的内部类型，全局唯一实例存于 `THE_REGISTRY` 静态变量——`links` 属性强制的「内核唯一」是整个分层的地基。

## 7. 下一步学习建议

本讲之后，入门单元告一段落，你已具备整张地图。接下来进入单元二「并行迭代器使用入门」：

- 下一讲 `u2-l1` 将深入 [src/iter/mod.rs:359](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L359) 的 `ParallelIterator` trait 本身，逐类清点它提供的方法（本讲只定位了它，还没读它）。
- 建议提前浏览 [src/iter/plumbing/README.md](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/README.md)——官方写的 plumbing 总览，是第四单元的预习材料，此刻读不懂细节没关系，混个眼熟即可。
- 如果你对调度内核更好奇，也可以直接跳到单元五从 `u5-l1`（join）读起，但建议至少先完成 `u2-l1`，掌握 trait 面貌后再下潜。
- 持续使用本讲的成果：以后每读一个新模块，先在依赖图上找到它的位置，再问「它在为谁服务、它依赖谁」。
