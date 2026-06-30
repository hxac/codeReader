# crossbeam 项目概览与定位

## 1. 本讲目标

本讲是整个 crossbeam 学习手册的第一篇。读完本讲，你应当能够：

- 说清楚 **crossbeam 是什么**：它是一个 Rust 并发编程工具集（tools for concurrent programming），而不是单一功能的库。
- 理解它**把工具分成五类**（原子 / 数据结构 / 内存管理 / 同步 / 工具），并能根据需求找到对应类别。
- 认识 crossbeam 的 **6 个子 crate**（channel / deque / epoch / queue / utils / skiplist）以及主 crate 如何把它们「重导出」成一个统一命名空间。
- 理解 **no_std / alloc / std 三级特性支持**：哪些工具能跑在没有标准库的嵌入式/内核环境，哪些需要堆分配，哪些必须依赖标准库。
- 了解项目的 **MSRV（最低支持 Rust 版本）、许可证、版本约定**，为后续在真实项目中引入 crossbeam 做好准备。

本篇**不深入任何算法细节**（自旋退避、epoch 回收、无锁队列等会在后续讲义展开），只建立「地图」。

## 2. 前置知识

本讲面向初学者，但你最好已经具备下面这些基础：

- **Rust 基础语法**：`struct`、`enum`、`trait`、泛型、生命周期的大致概念。你不需要精通 `unsafe`——本讲几乎不会用到。
- **什么是并发（concurrency）**：简单说，就是「多个线程同时访问同一份数据」。并发的难点在于：当两个线程同时读写同一块内存时，如何保证不出错、不崩溃、结果可预期。
- **什么是原子操作（atomic）**：CPU 提供的「不可被打断」的内存读写指令，例如 `compare-and-swap`（CAS）。它是无锁（lock-free）数据结构的基石。
- **什么是 channel（通道）**：线程之间传递消息的「管道」。一个线程往里塞，另一个线程往外取。
- **Cargo 的基本用法**：知道 `Cargo.toml`、`cargo build`、`cargo test`、`cargo doc` 是什么。

> 名词速查：
> - **MPMC**：Multi-Producer Multi-Consumer，多生产者多消费者。多个线程能同时发，多个线程能同时收。
> - **MPSC**：Multi-Producer Single-Consumer，多生产者单消费者。`std::sync::mpsc` 就是 MPSC。
> - **no_std**：不依赖 Rust 标准库 `std` 的环境，常见于嵌入式和操作系统内核。
> - **alloc**：Rust 的堆分配 crate。`no_std` 环境也可以单独启用 `alloc` 来用 `Box`、`Vec` 等。
> - **MSRV**：Minimum Supported Rust Version，最低支持的 Rust 版本。

## 3. 本讲源码地图

本讲只读 4 个最顶层、最容易理解的文件，它们构成了 crossbeam 的「门面与契约」：

| 文件 | 作用 | 本讲用来回答什么 |
|------|------|------------------|
| `README.md` | 面向用户的总说明：工具分类、子 crate、用法、兼容性、许可证 | crossbeam 是什么、有哪些工具、分几类 |
| `Cargo.toml` | 主 crate 的清单：版本、特性（features）、依赖、workspace 成员 | 版本号、特性门控、子 crate 依赖关系 |
| `src/lib.rs` | 主 crate 的库入口：把各子 crate 重导出为 `crossbeam::xxx` | 主 crate 如何统一对外暴露工具 |
| `CHANGELOG.md` | 版本变更记录 | MSRV 与版本演进约定 |

另外会用到一个测试文件作为「重导出是否正确」的佐证：

| 文件 | 作用 |
|------|------|
| `tests/subcrates.rs` | 用真实测试验证每个子 crate 都能通过 `crossbeam::xxx` 访问到 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 项目定位与并发工具分类** —— crossbeam 是什么、工具分五类。
- **4.2 子 crate 划分与各自职责** —— 6 个子 crate 各做什么、主 crate 如何拼装它们。
- **4.3 MSRV、许可证与版本约定** —— 工程上引入 crossbeam 要注意的契约。

---

### 4.1 项目定位与并发工具分类

#### 4.1.1 概念说明

crossbeam 是一个 **Rust 并发编程工具集**。它不是「一个数据结构」或「一个 channel 库」，而是一整套「搭并发程序会用到的零件」。

为什么需要这样一个工具集？因为 Rust 标准库 `std` 在并发方面提供的工具相对基础：`std::sync` 给了 `Mutex`、`RwLock`、`Arc`、`mpsc`（单消费者通道），`std::thread` 给了 `spawn`。但当你想写**高性能无锁数据结构**、**多消费者通道**、**工作窃取调度器**时，标准库就不够用了。crossbeam 填补的就是这块空白。

crossbeam 把它的工具**按用途分成五大类**。这个分类直接出现在 README 开头：

> 原子（Atomics）/ 数据结构（Data structures）/ 内存管理（Memory management）/ 线程同步（Thread synchronization）/ 工具（Utilities）

这个分类非常重要：它既是文档的组织方式，也是你后续遇到并发问题时「去哪找工具」的索引。

#### 4.1.2 核心流程

遇到一个并发需求时，可以按下面的「分类决策树」找到对应工具：

```text
我要做什么？
│
├─ 我要「读写一个值」，但希望它线程安全
│   └─ Atomics ── AtomicCell / AtomicConsume
│
├─ 我要一个「线程间传递数据的容器」
│   ├─ 先进先出队列 ── Data structures ── ArrayQueue / SegQueue
│   └─ 工作窃取双端队列 ── Data structures ── deque
│
├─ 我要「安全回收无锁结构里被删除的节点」
│   └─ Memory management ── epoch
│
├─ 我要「线程之间协调/通信」
│   ├─ 传消息 ── Thread synchronization ── channel
│   ├─ 停放/唤醒线程 ── Parker
│   ├─ 读写锁 ── ShardedLock
│   └─ 等一组任务完成 ── WaitGroup
│
└─ 我要一些「写并发代码的小帮手」
    ├─ 自旋退避 ── Utilities ── Backoff
    ├─ 缓存行对齐 ── CachePadded
    └─ 借用栈数据的线程 ── scope
```

这棵树不需要你背下来，只要记住：**先想清楚需求属于哪一类，再去那一类里找具体工具**。

#### 4.1.3 源码精读

README 开头一句话点明了项目定位：

[README.md:15](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/README.md#L15)

> `This crate provides a set of tools for concurrent programming:`

紧接着就是五大类的清单，每类下面列出具体工具，并用上标标注了特性等级（`(no_std)` / `(alloc)`）：

[README.md:17-47](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/README.md#L17-L47)

这段是本讲最重要的引用，它把「工具」和「能跑在什么环境」绑在了一起。我们把它的核心信息整理成下表（结合 README 标注）：

| 分类 | 工具 | 一句话作用 | 特性等级 |
|------|------|-----------|----------|
| Atomics | `AtomicCell` | 线程安全的可变内存单元（任意类型） | no_std |
| Atomics | `AtomicConsume` | 以 consume 内存序读取原子量 | no_std |
| Data structures | `deque` | 工作窃取双端队列，做任务调度用 | std |
| Data structures | `ArrayQueue` | 有界 MPMC 队列，构造时一次性分配 | alloc |
| Data structures | `SegQueue` | 无界 MPMC 队列，按需分配小段 | alloc |
| Memory management | `epoch` | 基于 epoch 的垃圾回收（给无锁结构用） | alloc |
| Thread synchronization | `channel` | MPMC 消息传递通道 | std |
| Thread synchronization | `Parker` | 线程停放（park/unpark）原语 | std |
| Thread synchronization | `ShardedLock` | 分片读写锁，读路径快 | std |
| Thread synchronization | `WaitGroup` | 同步一组计算的开始/结束 | std |
| Utilities | `Backoff` | 自旋循环里的指数退避 | no_std |
| Utilities | `CachePadded` | 按缓存行对齐，消除伪共享 | no_std |
| Utilities | `scope` | 派生能借用栈上变量的线程 | std |

> 注意：README 对部分工具标注了 `(no_std)` 或 `(alloc)`，未标注的工具默认需要 `std`。这一点会在 4.2.3 里结合 `src/lib.rs` 的 `#[cfg(feature=...)]` 做精确核对。

#### 4.1.4 代码实践

**实践目标**：亲手确认 crossbeam 工具的分类与「能跑在哪个层级」。

1. 在浏览器打开上面的 `README.md:17-47` 永久链接（或本地阅读仓库根目录的 `README.md`）。
2. 准备一张空表，表头为 `工具 | 所属分类 | 特性等级(no_std/alloc/std)`。
3. 逐条把 13 个工具填进表格，特性等级以上标 `(no_std)` / `(alloc)` 为准，没标注的填 `std`。
4. **需要观察的现象**：你会发现 `AtomicCell`、`AtomicConsume`、`Backoff`、`CachePadded` 这 4 个是 `(no_std)`；`ArrayQueue`、`SegQueue`、`epoch` 是 `(alloc)`；其余需要 `std`。
5. **预期结果**：你得到一张和上文 4.1.3 表格一致的对照表。这正是后续讲义按「先 utils 后 channel/epoch」排序的依据。
6. 如果手头没有可运行环境，明确记为「待本地验证」，不要假装已经跑过。

#### 4.1.5 小练习与答案

**练习 1**：如果我要在一段自旋等待的 CAS 循环里减少 CPU 占用，应该用哪一类、哪个工具？

> **答案**：Utilities 类的 `Backoff`。它在 CAS 失败时做指数退避，先用 `spin` 自旋，再切换到 `snooze`（让出 CPU），从而降低高争用下的 CPU 消耗。详见后续讲义 `u2-l1-backoff.md`。

**练习 2**：`ArrayQueue` 和 `SegQueue` 都是 MPMC 队列，它们的根本区别是什么？

> **答案**：`ArrayQueue` 是**有界**的，构造时就分配固定容量的缓冲；`SegQueue` 是**无界**的，按需分配小块（segment）。容量是否可预知、是否允许无限堆积，是选择二者的关键。

---

### 4.2 子 crate 划分与各自职责

#### 4.2.1 概念说明

crossbeam 在代码组织上有一个关键设计：**主 crate `crossbeam` 本身几乎不写实现，它只是把若干个独立的子 crate「重导出」（re-export）成一个统一的命名空间。**

这种模式叫**门面（facade）模式**：用户只需要 `use crossbeam::xxx`，而不用关心 `xxx` 实际来自 `crossbeam_xxx` 这个底层 crate。好处是：

- 用户只依赖一个 crate 名 `crossbeam`，API 路径统一好记。
- 底层子 crate 也可以被单独依赖（比如只想要通道的人可以直接用 `crossbeam-channel`）。
- 各子 crate 可以独立演进、独立版本号。

crossbeam 一共有 **6 个子 crate**，其中 5 个被主 crate 重导出，1 个（skiplist）还是实验性的、暂未进入主 crate。

#### 4.2.2 核心流程

子 crate 之间的逻辑依赖关系大致如下（箭头表示「被主 crate 引用 / 提供基础能力」）：

```text
                  主 crate crossbeam (门面，重导出)
                          │
        ┌──────────┬──────┴───────┬──────────┬──────────┐
        ▼          ▼              ▼          ▼          ▼
  crossbeam-   crossbeam-    crossbeam-  crossbeam-  crossbeam-
   channel      deque         epoch       queue       utils
   (通道)      (工作窃取)    (内存回收)   (并发队列)  (原子/同步原语基石)
                                                           ▲
                                          （epoch/deque/skiplist 都依赖 utils 的原子原语）
                                                           ▲
                                                   crossbeam-skiplist  ← 实验性，暂未进主 crate
```

要点：

1. `crossbeam-utils` 是**基石**：它提供原子单元、退避、停放等最底层的原语，其他子 crate（epoch、deque、skiplist）都会用到它。
2. `crossbeam-epoch` 是**无锁结构的安全基石**：deque 和 skiplist 都依赖它来安全回收节点（所以学习路线里 epoch 排在它们前面）。
3. `crossbeam-skiplist` 目前**还没被主 crate 重导出**，需要单独依赖 `crossbeam-skiplist`。

#### 4.2.3 源码精读

**① 6 个子 crate 的职责** —— 直接看 README 的 Crates 章节：

[README.md:63-82](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/README.md#L63-L82)

这一段明确列出了 5 个被重导出的子 crate，并单独说明 skiplist「not yet included in `crossbeam`」（尚未纳入主 crate）。注意这句原文：

[README.md:79-82](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/README.md#L79-L82)

> `There is one more experimental subcrate that is not yet included in crossbeam: crossbeam-skiplist ...`

**② workspace 成员** —— 主 `Cargo.toml` 把所有子 crate 列为 workspace 成员：

[Cargo.toml:63-74](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/Cargo.toml#L63-L74)

注意 members 里既有 `.`（主 crate 本身），也有 6 个子 crate，外加 `crossbeam-channel/benchmarks`（基准测试子项目）。这说明它们在**同一个 workspace 里统一编译**。

**③ 主 crate 用 path 依赖各子 crate** —— 依赖表里清一色是 `path = "crossbeam-xxx"`：

[Cargo.toml:51-56](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/Cargo.toml#L51-L56)

关键细节：除 `crossbeam-utils` 外，其余子 crate 都是 `optional = true`（可选依赖），并带 `default-features = false`。这正是「按特性按需启用子 crate」的基础（见 4.2.4 表格来源）。`crossbeam-utils` 不带 `optional`，因为它提供的 `atomic`/`utils` 即使在 no_std 下也要可用。

**④ 门面的真正实现：重导出** —— `src/lib.rs` 是整个门面的核心，全文只有 80 行：

[src/lib.rs:57-79](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/src/lib.rs#L57-L79)

这段是本讲的「重头戏」，逐条解读：

- `pub use crossbeam_utils::atomic;` —— 无条件重导出 `atomic` 模块（含 `AtomicCell`、`AtomicConsume`），所以它在 **no_std** 也能用。
- `pub mod utils { pub use crossbeam_utils::{Backoff, CachePadded}; }` —— 同样无条件，`Backoff`、`CachePadded` 在 **no_std** 可用。
- `#[cfg(feature = "std")] pub use crossbeam_utils::thread::{self, scope};` —— `scope`（作用域线程）需要 **std**。
- `#[cfg(feature = "std")] pub use { crossbeam_channel as channel, ..., crossbeam_utils::sync };` —— `channel`、`select`、`deque`、`sync` 都需要 **std**。
- `#[cfg(feature = "alloc")] pub use { crossbeam_epoch as epoch, crossbeam_queue as queue };` —— `epoch`、`queue` 只需要 **alloc**（不一定要 std）。

把这段 `#[cfg]` 信息反推，就能精确得到一张「主 crate 重导出 ↔ 特性等级」对照表：

| 主 crate 路径 | 来自子 crate | `cfg` 条件 | 等级 |
|---------------|-------------|-----------|------|
| `crossbeam::atomic` | crossbeam-utils | 无 | no_std |
| `crossbeam::utils::{Backoff,CachePadded}` | crossbeam-utils | 无 | no_std |
| `crossbeam::epoch` | crossbeam-epoch | `alloc` | alloc |
| `crossbeam::queue` | crossbeam-queue | `alloc` | alloc |
| `crossbeam::scope` / `crossbeam::thread` | crossbeam-utils | `std` | std |
| `crossbeam::channel` / `crossbeam::select!` | crossbeam-channel | `std` | std |
| `crossbeam::deque` | crossbeam-deque | `std` | std |
| `crossbeam::sync` | crossbeam-utils | `std` | std |

**⑤ 用测试验证重导出真的有效** —— `tests/subcrates.rs` 对每个命名空间都写了一个最小用例：

[tests/subcrates.rs:6-13](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/tests/subcrates.rs#L6-L13) （channel：用 `crossbeam::channel::bounded` + `select!`）
[tests/subcrates.rs:16-20](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/tests/subcrates.rs#L16-L20) （deque：用 `crossbeam::deque::Worker`）
[tests/subcrates.rs:22-25](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/tests/subcrates.rs#L22-L25) （epoch：调用 `crossbeam::epoch::pin()`）
[tests/subcrates.rs:27-32](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/tests/subcrates.rs#L27-L32) （queue：用 `crossbeam::queue::ArrayQueue`）
[tests/subcrates.rs:34-47](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/tests/subcrates.rs#L34-L47) （utils：`CachePadded` + 两种 `scope` 写法）

这个测试文件本身就是「重导出门面是否正确」的活文档。

#### 4.2.4 代码实践

**实践目标**：确认「主 crate 的某个工具到底来自哪个子 crate、需要哪个特性」。

1. 打开 [src/lib.rs:57-79](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/src/lib.rs#L57-L79)。
2. 对下列 5 个主 crate 工具，逐一回答「来自哪个子 crate + 需要 std 还是 alloc 还是 no_std」：
   - `crossbeam::channel::unbounded`
   - `crossbeam::epoch::pin`
   - `crossbeam::queue::SegQueue`
   - `crossbeam::utils::Backoff`
   - `crossbeam::scope`
3. **需要观察的现象**：你会看到它们的 `pub use` 分别落在 `std`、`alloc`、`alloc`、无条件、`std` 这几个 `cfg` 分支下。
4. **预期结果**：与上表一致。
5. 进阶（可选）：执行 `cargo doc --open`，在浏览器里展开 `crossbeam` 的模块树，亲眼确认 `channel`、`epoch`、`queue`、`utils`、`atomic`、`sync`、`deque`、`thread` 都作为子模块出现。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `crossbeam-utils` 在主 `Cargo.toml` 里**没有** `optional = true`，而其他子 crate 都有？

> **答案**：因为 `crossbeam-utils` 提供的 `atomic`、`Backoff`、`CachePadded` 等是无条件重导出的（no_std 也可用，见 `src/lib.rs:57` 和 `:65`），主 crate 在任何特性组合下都依赖它。而 `channel`/`deque`/`epoch`/`queue` 是按特性门控、可选启用的，所以标为 optional。

**练习 2**：`crossbeam-skiplist` 能否通过 `use crossbeam::skiplist` 访问？为什么？

> **答案**：不能。README 明确说它是「not yet included in `crossbeam`」的实验性子 crate（README.md:79-82），`src/lib.rs` 里也没有对它的重导出。要用必须单独依赖 `crossbeam-skiplist`。

---

### 4.3 MSRV、许可证与版本约定

#### 4.3.1 概念说明

把一个库引入生产项目前，除了「它好不好用」，还要确认三件事：

- **MSRV（最低支持 Rust 版本）**：你的工具链够不够新？老项目能否用？
- **许可证（License）**：能不能合法地用在你的商业/开源项目里？
- **版本约定**：升级时会不会破坏兼容性？版本号怎么读？

crossbeam 在这三方面都有明确的、写在文件里的契约，不是「待确认」。

#### 4.3.2 核心流程

crossbeam 的版本/兼容性「工作流」可以概括为：

```text
确定 MSRV（写进 Cargo.toml 的 rust-version + README 徽章）
   │
   ▼
每发布新版本 → 在 CHANGELOG.md 记一行
   │
   ▼
每次「提高 MSRV」→ 必须发布一个新 minor 版本（向后兼容承诺）
   │
   ▼
许可证固定为 MIT OR Apache-2.0（双许可，二选一）
```

关键承诺（来自 README）：crossbeam 支持至少最近 6 个月的 stable Rust；每次提高 MSRV 都会发布新 minor 版本。这样用户可以根据版本号预判兼容性。

#### 4.3.3 源码精读

**① MSRV 在三处保持同步** —— `Cargo.toml` 顶部明确写了：

[Cargo.toml:7-11](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/Cargo.toml#L7-L11)

> `version = "0.8.4"`、`rust-version = "1.74"`、`license = "MIT OR Apache-2.0"`

注意第 9 行的注释 `# NB: Sync with msrv badge and "Compatibility" section in README.md`，意思是这个 MSRV 必须和 README 徽章、Compatibility 章节保持一致。README 里对应的承诺在：

[README.md:93-97](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/README.md#L93-L97)

> `...the minimum supported Rust version is 1.74.`

README 顶部的徽章也标注了 `[![Rust 1.74+]...]`（README.md:11）。

**② 许可证** —— 双许可，二选一：

[README.md:142-149](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/README.md#L142-L149)

> Apache-2.0 或 MIT，任选其一。

第 151-152 行还提示：部分子 crate 有额外的许可说明，要看各子 crate 自己的 readme。

**③ 版本约定与 MSRV 演进** —— CHANGELOG 记录了每次 MSRV 变动：

[CHANGELOG.md:1-3](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/CHANGELOG.md#L1-L3) （0.8.4：移除 cfg-if 依赖）
[CHANGELOG.md:5-7](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/CHANGELOG.md#L5-L7) （0.8.3：把 MSRV 提到 1.61）
[CHANGELOG.md:9-11](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/CHANGELOG.md#L9-L11) （0.8.2：把 MSRV 提到 1.38）

可以看到 MSRV 是逐版本缓慢抬升的（1.38 → 1.61 → 1.74），每次都伴随新 minor 版本发布，印证了 README 的兼容性承诺。

#### 4.3.4 代码实践

**实践目标**：核对你当前环境能否使用 crossbeam，并读懂它的版本契约。

1. 在终端执行 `rustc --version`，记下你的 Rust 版本。
2. 对照 [Cargo.toml:10](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/Cargo.toml#L10) 的 `rust-version = "1.74"`。
3. **需要观察的现象**：如果你的 `rustc` ≥ 1.74，可以直接使用当前版本 crossbeam 0.8.4；若低于，则可能要用更早的 minor 版本（参考 CHANGELOG 找对应 MSRV）。
4. **预期结果**：你能给出「我的 Rust 版本是 X，可以用 crossbeam 版本 ≥ Y」的结论。
5. 若环境不可用，明确记「待本地验证」。
6. （可选）阅读 [CHANGELOG.md](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/CHANGELOG.md) 全文，体会「提高 MSRV 一定发新 minor 版本」这条规则如何体现在历史记录里。

#### 4.3.5 小练习与答案

**练习 1**：crossbeam 0.8.4 的 MSRV 是多少？这个值在仓库里有哪几个地方必须保持一致？

> **答案**：MSRV 是 Rust 1.74。必须在三处保持一致：`Cargo.toml` 的 `rust-version`（第 10 行）、README 顶部的 `Rust 1.74+` 徽章（第 11 行）、README 的 Compatibility 章节（第 97 行）。`Cargo.toml:9` 的注释也专门提醒要同步。

**练习 2**：为什么 crossbeam 每次提高 MSRV 都要发一个新 minor 版本（比如 0.8.2 → 0.8.3）？

> **答案**：为了履行 README 第 95-97 行的兼容性承诺——支持至少最近 6 个月的 stable Rust。把 MSRV 提升放在新 minor 版本里，旧版本的用户可以继续锁定旧 minor 版本而不被强制升级工具链，这是一种向后兼容的工程约定。

---

## 5. 综合实践

把本讲的三个模块串起来，完成下面这个「**向同事介绍 crossbeam**」的小任务：

> **场景**：你的团队想引入一个并发通道，有人提议用标准库的 `std::sync::mpsc`，有人提议用 `crossbeam`。请你读完 README 后，用**一句话**向同事说明 crossbeam 相对 `std::sync::mpsc` 的差异，并证明 crossbeam 的「门面」确实方便。

具体步骤：

1. **读 README**：打开 [README.md](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/README.md)，重点看 Atomics / Thread synchronization 两节。
2. **写一句话差异**：参考要点——`std::sync::mpsc` 是**多生产者单消费者（MPSC）**通道；而 `crossbeam::channel` 是**多生产者多消费者（MPMC）**通道，还提供有界/无界/零容量三种语义和 `select!` 多路选择。（这是基于 README 对 `channel` 描述 "multi-producer multi-consumer channels" 的合理推论；若要逐字引用，以 `crossbeam-channel` 子 crate 文档为准。）
3. **列 5 个重导出工具**：对照 [src/lib.rs:57-79](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/src/lib.rs#L57-L79)，列出主 crate 重导出的任意 5 个工具及其来源子 crate。例如：
   - `crossbeam::channel`（来自 crossbeam-channel）
   - `crossbeam::deque`（来自 crossbeam-deque）
   - `crossbeam::epoch`（来自 crossbeam-epoch）
   - `crossbeam::queue`（来自 crossbeam-queue）
   - `crossbeam::atomic` / `crossbeam::utils` / `crossbeam::sync`（来自 crossbeam-utils）
4. **浏览文档树**：在仓库根目录执行 `cargo doc --open`，在浏览器里展开 `crossbeam` crate，确认上面这些模块都在模块树里。如果无 Rust 环境，记为「待本地验证」。
5. **产出**：一段话（一句话差异）+ 一张 5 行的小表（工具 / 来源子 crate / 特性等级）。

> 这个任务同时检验了 4.1（工具分类）、4.2（门面与子 crate）、4.3（你顺手确认了 MSRV/许可适合引入）三个模块。

## 6. 本讲小结

- crossbeam 是一个 **Rust 并发编程工具集**，不是单一功能库，工具分为**原子 / 数据结构 / 内存管理 / 线程同步 / 工具**五大类（README.md:15-47）。
- 它由 **6 个子 crate** 组成：`channel`、`deque`、`epoch`、`queue`、`utils`、`skiplist`；前 5 个被主 crate 重导出，`skiplist` 仍是实验性、暂未纳入（README.md:63-82）。
- 主 crate `crossbeam` 本身是**门面（facade）**，几乎只做 `pub use` 重导出，统一对外路径（src/lib.rs:57-79）。
- 工具按 **no_std / alloc / std 三级** 分层：`atomic`、`utils` 无条件可用；`epoch`、`queue` 需 `alloc`；`channel`、`deque`、`sync`、`scope` 需 `std`（对应 `src/lib.rs` 的 `#[cfg(feature=...)]`）。
- 工程契约明确：MSRV = **Rust 1.74**（Cargo.toml:10），许可证 = **MIT OR Apache-2.0** 双许可（README.md:142-149），每次提高 MSRV 会发新 minor 版本（CHANGELOG）。
- 重导出的正确性由 `tests/subcrates.rs` 的 5 个最小用例持续验证。

## 7. 下一步学习建议

本讲建立了「地图」，下一步建议：

1. **先动手写第一段并发代码**：进入讲义 `u1-l4-scoped-threads-intro.md`，学习 `crossbeam::scope`（作用域线程），它是 crossbeam 里最容易上手、最能体现「借用栈上数据」优势的入口。
2. **理解构建与特性系统**：先看 `u1-l2-workspace-build-features.md`，搞清楚 workspace、std/alloc 特性如何在子 crate 间层层传递，以及怎么本地构建测试。
3. **看懂门面细节**：`u1-l3-reexport-facade.md` 会更细致地剖析 `src/lib.rs` 的条件编译与 `#[doc(inline)]`。
4. **之后再按单元深入**：第 2 单元（utils 原语）→ 第 3 单元（channel）→ 第 4 单元（queue）→ 第 5 单元（epoch）→ 第 6 单元（deque）→ 第 7 单元（skiplist + 测试）。

> 提示：后续讲义会大量引用具体源码行号，建议你现在已经能在本地把仓库 `cargo build` 通过，并会用 `cargo doc --open` 查阅 API。
