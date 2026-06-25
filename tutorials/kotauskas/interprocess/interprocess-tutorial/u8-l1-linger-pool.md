# linger_pool：延迟句柄刷新池

## 1. 本讲目标

本讲是「Windows 高级内部机制」单元的第一讲，专攻 interprocess 最精巧的内部设施之一——`linger_pool`（延迟刷新池）。它不在公共 API 里，却默默保证了 Windows 管道的一个关键正确性：**写端 `drop` 之后，对端仍能收到此前写入的全部数据**。

学完后你应该能够：

1. 说清楚 Windows 管道的 **limbo 问题**：为什么不能在 `drop` 时直接关闭句柄，否则对端会丢失尚未读取的缓冲数据。
2. 读懂 `needs_flush` 的 **三态脏标记**（`No`/`Once`/`Always`），理解它如何决定一个句柄是否需要进 limbo。
3. 画出 `linger_pool` 的整体架构：**一个持久线程 + 弹性临时线程 + 高低水位队列**，并解释水位触发点。
4. 看懂 `QueueEnt` 的 **低比特标记指针（low-bit tagging）** 技巧，理解它如何把两种句柄形态压进单个指针。
5. 把「`drop` 一个 `PipeStream`」到「后台线程真正 `FlushFileBuffers` 并关闭句柄」的完整路径串起来。

本讲是 u4-l3（Windows 原生 named pipe API）的延续——u4-l3 在文档与 `evade_limbo` 处点到「limbo」即止，本讲钻进它的实现。

## 2. 前置知识

### 2.1 Windows 句柄与 `FlushFileBuffers`

Windows 的所有 I/O 对象（文件、管道、事件……）都由 **句柄（Handle）** 代表，本质是一个进程局部的整数（`HANDLE`，实为 `void*`）。重要事实：**Windows 句柄值恒为 4 的倍数**，即最低两位永远是 `0`。这个性质在 4.5 节的低比特标记里会被直接利用。

对于管道，`FlushFileBuffers`（见 [src/os/windows/c_wrappers.rs:152-154](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/c_wrappers.rs#L152-L154)）会**阻塞调用线程，直到对端把发送缓冲里现存的数据全部读走**。

### 2.2 关闭与刷新的区别（limbo 的根因）

直接 `CloseHandle` 一个尚有未读数据的管道写端，**内核会丢弃缓冲里来不及发送的数据**，对端随即收到 `BrokenPipe`/EOF。若希望在写端关闭后、对端仍能读到全部已写数据，就必须在关闭前先 `FlushFileBuffers`。

问题在于：`FlushFileBuffers` 会阻塞，而 Rust 的 `Drop` 是在**调用者线程**上同步运行的。如果在 `Drop` 里直接 `FlushFileBuffers`，会把这个「可能很慢、要等对端读完」的阻塞强加给 `drop()` 的调用方——这违背了「`drop` 应当尽快返回」的直觉，还可能在单线程收发场景里造成死锁。

interprocess 的解法是：**把句柄的所有权从 `Drop` 线程「扔」给一个后台线程池，让后台线程去阻塞刷新、刷新完再关闭**。这个后台池就是 `linger_pool`，被刷新的句柄处于「悬而未决」的状态，文档称之为 **limbo**。

### 2.3 `ManuallyDrop` 与「取走所有权」

u5-l1、u6-l2 已讲过：Windows 后端的 `Sender`/`RawPipeStream` 用 `ManuallyDrop` 持有内部句柄字段。这样 `Drop` 时可以先用 `ManuallyDrop::take` 把句柄「拿出来」，再决定是立即关闭（干净）还是交给 `linger_pool`（脏）。本讲会反复看到这个模式。

### 2.4 本模块的可见性

`linger_pool` 与 `needs_flush` 都是 **crate 私有**基础设施（见 [src/os/windows.rs:18-19](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows.rs#L18-L19) 的 `mod needs_flush; mod linger_pool;`，以及第 25 行 `pub(crate) use ... needs_flush::*`）。使用者无法直接调用它们，本讲纯属「内部机制解读」。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/os/windows/linger_pool.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/linger_pool.rs) | **本讲主角**：延迟刷新线程池的全部实现——三个公共入口（`linger`/`linger_boxed`/`linger_arc`）、低比特标记的 `QueueEnt`、`Queue` 同步队列、高低水位调度、持久线程与临时线程。 |
| [src/os/windows/needs_flush.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/needs_flush.rs) | **三态脏标记** `NeedsFlush`：用 `AtomicEnum` 记录「是否需要 flush」，决定一个句柄 `drop` 时是否进 limbo。 |
| [src/os/windows/named_pipe/stream.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream.rs) | named pipe 流的 limbo 文档说明（32-44 行）与 `RawPipeStream::drop`（79-85 行），是调用 `linger_pool::linger` 的典型现场。 |
| [src/os/windows/unnamed_pipe.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/unnamed_pipe.rs) | 匿名管道后端 `Sender`，其 `Drop`（151-158 行）同样把脏句柄交给 `linger_pool::linger`。 |
| [src/os/windows/c_wrappers.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/c_wrappers.rs) | `flush()`（152-154 行）封装 `FlushFileBuffers`——后台线程最终调用的就是它。 |
| [tests/unnamed_pipe/basic.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/tests/unnamed_pipe/basic.rs) | 一个明确注释「Sender 此刻必在 limbo」的集成测试，是观察 limbo 行为的最佳样例。 |

## 4. 核心概念与源码讲解

本讲按「动机 → 判定 → 架构 → 调度 → 指针技巧」的顺序展开，最后在综合实践里把它们串成一条完整路径。

### 4.1 limbo 问题：为何 drop 不能立即关闭句柄

#### 4.1.1 概念说明

interprocess 在 named pipe 流的文档里把 limbo 说得很清楚——这是本讲的「第一性原理」：

> Upon being dropped, streams that haven't been flushed since the last send are transparently sent to **limbo** – a thread pool that ensures that the peer does not get `BrokenPipe`/EOF immediately after all data has been sent, which would otherwise discard everything. Named pipe handles on this thread pool are flushed first and only then closed, ensuring that they are only destroyed when the peer is done reading them.

见 [src/os/windows/named_pipe/stream.rs:32-44](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream.rs#L32-L44)。

翻译成关键结论：**先刷新、后关闭**。刷新（`FlushFileBuffers`）阻塞到对端读完缓冲；只有刷新完成后才关闭句柄，从而保证对端不会因为写端过早关闭而丢数据。匿名管道同理，见 [src/unnamed_pipe.rs:82-85](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe.rs#L82-L85)。

为什么不能在 `Drop` 里同步做？因为 `FlushFileBuffers` 要等对端读——可能等很久，甚至在对端不读时**永远阻塞**。把这种阻塞塞进 `drop()` 会让所有 `drop` 写端的代码路径都变得不可预测。所以解法是：`drop` 只负责「把句柄移交给后台」，立即返回；阻塞刷新由后台线程承担。

#### 4.1.2 核心流程

```text
写端 Sender / RawPipeStream
   │  write() 成功 → 标记 needs_flush = 脏
   │
   ▼  drop()
   ┌─────────────────────────────────────────┐
   │ ManuallyDrop::take 取出句柄            │
   │ if needs_flush 为脏:                    │
   │     linger_pool::linger(句柄)  ← 移交   │  ← drop 立即返回，句柄进入 limbo
   │ else:                                   │
   │     句柄随 OwnedHandle 自然 drop 关闭   │  ← 干净句柄不进 limbo（省事）
   └─────────────────────────────────────────┘
                ···（后台线程池，见 4.3）···
   ▼
   FlushFileBuffers(句柄)   ← 阻塞到对端读完
   ▼
   CloseHandle(句柄)        ← OwnedHandle 的 Drop 关闭
```

「干净句柄不进 limbo」是重要的优化：从没写过、或上次写后已显式 `flush()` 过的句柄，`drop` 时直接关闭，零开销、不惊动后台线程。

### 4.2 NeedsFlush：三态脏标记（决定是否进 limbo）

#### 4.2.1 概念说明

在交给后台池之前，先要回答一个问题：**这个句柄到底脏不脏？** 如果用单个 `AtomicBool`，会丢失一个关键信息——「被克隆过」。克隆（`try_clone`）之后，两个副本共享底层缓冲，各自独立写，谁也说不清对方的写状态，于是一旦克隆过，就必须**保守地永远视作脏**。

`needs_flush.rs` 用一个三态枚举 `NeedsFlushVal` 精确刻画这一点：

- `No` —— 干净，无需 flush。
- `Once` —— 有一次待刷新（自上次 flush 以来写过）。
- `Always` —— 永久脏（一旦克隆过就置此态，再也回不去 `No`/`Once`）。

见 [src/os/windows/needs_flush.rs:46-53](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/needs_flush.rs#L46-L53)。

容器 `NeedsFlush(AtomicEnum<NeedsFlushVal>)` 用 `AtomicEnum` 把这个 `#[repr(u8)]` 枚举塞进一个原子字节（`AtomicEnum` 的定义见 [src/atomic_enum.rs:13](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/atomic_enum.rs#L13)，它要求被包装类型实现 `ReprU8`）。整个结构体因此只占 1 字节。

#### 4.2.2 核心流程：四个原子操作的状态机

| 方法 | 操作 | 语义 |
|---|---|---|
| `mark_dirty` | CAS `No → Once`（失败无所谓） | 写成功后调用；已是 `Once`/`Always` 则不动。 |
| `on_clone` | STORE `Always`（Release） | 克隆时调用，永久「钉死」为脏。 |
| `take` | CAS `Once → No`；或读到 `Always` | 返回是否需要 flush：`Once` 被消费成 `No`，`Always` 恒真。 |
| `clear` | CAS `Once → No` | 异步 flush 成功后清掉一次性脏标记。 |

逐行看 `take`，它最能体现三态设计：

```rust
pub(crate) fn take(&self) -> bool {
    match self.0.compare_exchange(NeedsFlushVal::Once, NeedsFlushVal::No, AcqRel, Acquire) {
        Ok(..) => true,                       // 原本是 Once，消费掉，返回「要 flush」
        Err(NeedsFlushVal::Always) => true,   // 克隆过的，永远要 flush，但不改状态
        Err(.. /* NeedsFlushVal::No */) => false, // 干净，跳过 flush
    }
}
```

见 [src/os/windows/needs_flush.rs:20-27](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/needs_flush.rs#L20-L27)。

这正是文档里说的「连续两次 `.flush()` 第二次是 no-op」的实现：第一次 `take()` 把 `Once` 消费成 `No`，第二次 `take()` 命中 `Err(No)` 返回 `false`，直接跳过 `FlushFileBuffers`。

#### 4.2.3 源码精读：标记如何被读写

**写时标记脏**——named pipe 流的 `send` 成功后调 `mark_dirty`（[src/os/windows/named_pipe/stream/impl/send.rs:5-9](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/impl/send.rs#L5-L9)）；匿名管道 `Sender::write` 同理（[src/os/windows/unnamed_pipe.rs:131-137](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/unnamed_pipe.rs#L131-L137)）。

**克隆时钉死为 Always**——`try_clone` 里调 `on_clone`，新副本以 `NeedsFlushVal::Always` 构造（[src/os/windows/named_pipe/stream/impl/handle.rs:92-98](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/impl/handle.rs#L92-L98)）。

**drop 时判定进不进 limbo**——named pipe 的 `RawPipeStream::drop`（[src/os/windows/named_pipe/stream.rs:79-85](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream.rs#L79-L85)）：

```rust
impl Drop for RawPipeStream {
    fn drop(&mut self) {
        let h = unsafe { ManuallyDrop::take(&mut self.handle) };
        if self.needs_flush.get_mut() {   // 脏 → 交 limbo；干净 → 自然关闭
            linger_pool::linger(h);
        }
    }
}
```

匿名管道 `Sender::drop`（[src/os/windows/unnamed_pipe.rs:151-158](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/unnamed_pipe.rs#L151-L158)）结构完全一致。

#### 4.2.4 代码实践：读测试理解 limbo 判定

**实践目标**：用一个真实测试验证「写后 drop，数据仍在」的 limbo 保证。

**操作步骤**：阅读 [tests/unnamed_pipe/basic.rs:11-36](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/tests/unnamed_pipe/basic.rs#L11-L36)。关键在第 18-19 行——发送线程先 `write_all` 再 `drop(tx)`，第 25 行主线程 `wait.recv()` 确认 drop 已发生，**然后**第 31 行才去 `read_line`。第 26 行注释直白写道：「Sender is guaranteed to be in limbo by this point (Windows only)」。

**需要观察的现象**：写端已经 `drop`、句柄理论上已不存在，但读端仍能完整读到 `MSG`。

**预期结果**：
- **Windows**：`drop(tx)` 触发 `linger_pool::linger`，后台线程随后 `FlushFileBuffers` + 关闭；缓冲数据得以保留，`read_line` 成功读到完整消息。（若没有 limbo，`drop` 会立即丢弃缓冲，`read_line` 读到空或出错。）
- **Unix**：内核托管管道缓冲，写端关闭读端照常读到 EOF 前的数据，行为相同但机制不同（不经过 linger_pool）。

可在 Windows 上执行（待本地验证）：`cargo test --test unnamed_pipe`，或直接运行 `tests/unnamed_pipe` 对应示例路径。

> 说明：此测试是跨平台编写的，注释里的 limbo 行为只在 Windows 编译路径上成立；在 Unix 上 `Sender::drop` 不调用 `linger_pool`（见 [src/os/windows/unnamed_pipe.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/unnamed_pipe.rs) 仅在 `cfg(windows)` 下编译）。

#### 4.2.5 小练习与答案

**练习 1**：假设一个流 `mark_dirty()` 后连续调用两次 `flush()`（内部走 `take()`），两次分别返回什么？为什么？

**答案**：第一次 `take()` 把 `Once → No` 返回 `true`，触发真正的 `FlushFileBuffers`；第二次 `take()` 命中 `Err(No)` 返回 `false`，`flush` 提前返回 `Ok(())`，不触碰系统调用。这就是「连续 flush 第二次是 no-op」。

**练习 2**：为什么 `on_clone` 要直接 STORE 成 `Always`，而不是 `Once`？

**答案**：克隆后两个副本共享底层缓冲、各自独立写，`Once` 这种「一次性」语义无法表达「两个副本都在持续制造脏数据」的事实；且 `Once` 会被 `take` 消费成 `No`，会导致克隆副本被错误地判定为干净。`Always` 既不可消费、又恒真，是唯一安全的选择。

### 4.3 linger_pool 的入口与三种句柄形态

#### 4.3.1 概念说明

`linger_pool` 收纳的「待刷新句柄」有三种来源形态，对应三个公共入口函数（见 [src/os/windows/linger_pool.rs:20-36](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/linger_pool.rs#L20-L36)）：

| 入口 | 接收类型 | 适用场景 | 是否有堆开销 |
|---|---|---|---|
| `linger` | `T: Into<OwnedHandle>` | 句柄能直接转 `OwnedHandle`（匿名管道 `Sender`、同步 named pipe 流） | 无 |
| `linger_boxed` | `T: AsHandle + Send + Sync`（装箱） | 持有的是 `Box`/具体类型，句柄藏在里面（tokio 匿名管道、tokio named pipe 流） | 有（一次 `Box`/已有堆） |
| `linger_arc` | `LingerableArc<T>` | 持有的是 `Arc`，且可能被多任务共享 | 复用既有 `Arc` 堆分配 |

设计意图：**最常见的「裸句柄」走零堆开销的 `linger` 路径**；只有确实需要堆的对象才走另两个。`linger` 的文档注释开门见山：「Sends the given handle owner off to the linger pool without a heap indirection.」

#### 4.3.2 核心流程：统一的 `linger_ent` 派发

三个入口都把入参包成 `QueueEnt` 枚举变体，再调私有 `linger_ent`：

```rust
fn linger_ent(h: QueueEnt) {
    if !HAS_PERSISTENT_THREAD.fetch_or(true, AcqRel) {
        spawn_persistent_thread(h);          // 第一次：拉起唯一的持久线程，顺带把 h 带进去
    } else if let Err(h) = QUEUE.enqueue(h) {
        spawn_high_wm_thread(h);             // 持久线程已在但队列满（≥64）：开临时线程带 h
    }
    // 否则：h 已入队，持久/临时线程会取走
}
```

见 [src/os/windows/linger_pool.rs:37-43](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/linger_pool.rs#L37-L43)。

这里有两条静态全局状态（[src/os/windows/linger_pool.rs:17-18](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/linger_pool.rs#L17-L18)）：`HAS_PERSISTENT_THREAD`（`AtomicBool`，标记持久线程是否已存在）和 `QUEUE`（全局队列）。

`fetch_or(true, AcqRel)` 是个精妙之处：它原子地「置 true 并返回旧值」。返回 `false` 表示「我刚把它从 false 变 true，我是第一个」，于是负责拉起持久线程；返回 `true` 表示「早已有人拉起过」，走入队路径。全程无锁、无竞争地选出「唯一的持久线程创建者」。

#### 4.3.3 源码精读：`HandleFini`——刷新 + 关闭的最小单元

无论哪种入口，最终后台线程做的事都是「`FlushFileBuffers` 然后 `CloseHandle`」。对裸句柄，这件事封装在 `HandleFini`：

```rust
struct HandleFini(OwnedHandle);
impl Drop for HandleFini {
    fn drop(&mut self) { let _ = c_wrappers::flush(self.0.as_handle()); }
}
```

见 [src/os/windows/linger_pool.rs:113-116](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/linger_pool.rs#L113-L116)。

当后台线程 `drop` 一个 `HandleFini` 时：先跑 `HandleFini::drop`（调 `flush`，阻塞到对端读完），再跑 `OwnedHandle::drop`（关闭句柄）。**先刷新、后关闭的顺序由字段 drop 顺序天然保证**——这正是 limbo 的核心承诺在代码里的落地。

`c_wrappers::flush` 见 [src/os/windows/c_wrappers.rs:152-154](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/c_wrappers.rs#L152-L154)，即一行 `FlushFileBuffers` 加错误转换。

对 `linger_boxed`/`linger_arc`，刷新逻辑由各自的析构闭包 `dtor` 承担（[src/os/windows/linger_pool.rs:60-66](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/linger_pool.rs#L60-L66) 与 [src/os/windows/linger_pool.rs:86-98](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/linger_pool.rs#L86-L98)）：闭包先重建出 `Arc`/`Box`，取出内部句柄，调同一个 `c_wrappers::flush`，再随重建对象的 drop 自然关闭。

### 4.4 持久线程 + 弹性临时线程 + 高低水位队列

#### 4.4.1 概念说明

现在句柄已经进了队列，谁来取走它、刷新它？`linger_pool` 采用一个 **弹性线程池** 模型：

- **1 个持久线程**（persistent）：进程生命周期内只创建一次，永不退出，是稳态的「值班」消费者。
- **N 个临时线程**（temporary）：在负载高时按需创建，空闲超时（500ms）后自动退出。

为什么要弹性？刷新操作（`FlushFileBuffers`）是**长阻塞**——可能等对端读很久。如果只有一个线程，突发大量 drop 会堆积在队列里迟迟得不到处理；但常驻一大堆线程又浪费。解法是用两个水位（watermark）做负载跟随：

- **高水位 `HIGH_WATERMARK = 64`**：队列容量上限。达到即拒绝入队，转而立即开一个临时线程「自带任务」处理。
- **低水位 `LOW_WATERMARK = 8`**：消费者每消费一个任务后检查，若队列仍高于低水位，就再开一个临时线程来帮忙加速排空。

见 [src/os/windows/linger_pool.rs:210-212](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/linger_pool.rs#L210-L212)。

用公式表达两个触发条件（记队列长度为 \(L\)）：

- 入队时：若 \(L \geq 64\)，入队失败 → 立即开临时线程（高水位溢出）。
- 出队后：若 \(L > 8\)，开辅助临时线程（低水位之上的负载跟随）。

#### 4.4.2 核心流程：线程主循环

**持久线程**（[src/os/windows/linger_pool.rs:243-252](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/linger_pool.rs#L243-L252)）：

```rust
fn persistent_thread_main(first_h: Option<QueueEnt>) {
    drop(first_h);          // 先把创建时「顺手带进来」的第一个任务处理掉
    loop {
        let (h, above_wm) = QUEUE.get();   // 阻塞等任务
        drop(h);                           // 刷新 + 关闭（HandleFini/DynHandleOwner 的 drop）
        if above_wm { spawn_low_wm_thread(); }  // 仍高于低水位 → 拉一个帮手
    }
}
```

`QUEUE.get()` 在队列空时阻塞（靠 `Condvar`），有任务时返回 `(任务, 是否高于低水位)`。

**临时线程**（[src/os/windows/linger_pool.rs:254-263](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/linger_pool.rs#L254-L263)）：

```rust
fn temporary_thread_main(first_h: Option<QueueEnt>) {
    drop(first_h);
    loop {
        // 500ms 超时取任务；取不到（None）就退出
        let Some((h, above_wm)) = QUEUE.get_timeout(TEMP_TIMEOUT).0 else { return };
        drop(h);
        if above_wm { spawn_low_wm_thread(); }
    }
}
```

`TEMP_TIMEOUT = 500ms`（[src/os/windows/linger_pool.rs:241](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/linger_pool.rs#L241)）。临时线程在 500ms 内取不到新任务即自我退出——这就是「弹性收缩」。

两个线程的创建函数见 [src/os/windows/linger_pool.rs:232-239](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/linger_pool.rs#L232-L239)，栈大小都压到 `128 * 1024`（128 KiB），见 [src/os/windows/linger_pool.rs:265-274](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/linger_pool.rs#L265-L274)——这些线程不需要大栈。

#### 4.4.3 源码精读：水位检查与队列同步

`QueueInner::dequeue_and_check_watermark` 把「出队」与「水位检查」合成一步（[src/os/windows/linger_pool.rs:223-229](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/linger_pool.rs#L223-L229)）：

```rust
fn dequeue_and_check_watermark(&mut self) -> Option<(QueueEnt, bool)> {
    self.dequeue().map(|ent| (ent, self.above_low_watermark()))
}
fn above_low_watermark(&self) -> bool { self.queue.len() > Self::LOW_WATERMARK }
```

注意：`above_low_watermark` 在 `dequeue`（`pop_front`）**之后**读取长度。所以「above_wm 为真」意味着「我取走一个之后，队列里还剩 8 个以上」——即积压严重，需要帮手。

队列的入队带容量保护（[src/os/windows/linger_pool.rs:215-222](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/linger_pool.rs#L215-L222)）：

```rust
fn enqueue(&mut self, e: QueueEnt) -> Result<(), QueueEnt> {
    if self.queue.len() >= Self::HIGH_WATERMARK {
        return Err(e);   // 满了，把 e 原样还给调用者，由它开临时线程处理
    }
    self.queue.reserve_exact(Self::HIGH_WATERMARK);
    self.queue.push_back(e.into_raw());
    Ok(())
}
```

`Queue` 本身是 `Mutex<QueueInner> + Condvar` 的经典「阻塞队列」组合（[src/os/windows/linger_pool.rs:148-202](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/linger_pool.rs#L148-L202)）：`enqueue` 后 `notify_one` 唤醒一个等待的消费者；`get` 在空队列上 `cv.wait` 阻塞；`get_timeout` 用 `wait_timeout` 实现 500ms 超时。锁中毒时用 `PoisonError::into_inner` 强取（见 `lk_loop`，[src/os/windows/linger_pool.rs:163-172](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/linger_pool.rs#L163-L172)）——池子不能因别的线程 panic 而彻底卡死。

#### 4.4.4 小练习与答案

**练习 1**：假设瞬时 drop 了 200 个脏句柄（持久线程已存在），描述会发生什么。

**答案**：前 64 个入队（队列从 0 涨到 64）；第 65 个起 `enqueue` 返回 `Err`，每个都触发 `spawn_high_wm_thread` 各自带 1 个任务开临时线程。同时，持久线程和已有临时线程每消费一个、发现队列仍 \(>8\)，就不断 `spawn_low_wm_thread`。最终大量临时线程并发刷新，队列迅速排空；排空后临时线程陆续 500ms 超时退出，只剩持久线程值班。

**练习 2**：把 `LOW_WATERMARK` 调到 0（即 `above_low_watermark` 恒为 `len > 0`），会有什么副作用？

**答案**：只要有任务，消费者每处理一个都会再拉一个临时线程，临时线程数量会随队列长度爆炸式增长（接近「每任务一线程」），远超必要。低水位=8 的意义正是「积压不严重（≤8）时不再扩容」，避免在正常轻负载下无谓地开线程。

### 4.5 QueueEnt：低比特标记指针

#### 4.5.1 概念说明

队列里存的是什么？直觉上该存 `QueueEnt` 枚举：

```rust
enum QueueEnt {
    Handle(HandleFini),                 // 裸句柄
    IndirectHandle(DynHandleOwner),     // 堆上的间接句柄（Box/Arc）
}
```

见 [src/os/windows/linger_pool.rs:118-121](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/linger_pool.rs#L118-L121)。但 `QueueInner.queue` 的元素类型是 `VecDeque<*mut ()>`——**裸指针**，不是枚举（[src/os/windows/linger_pool.rs:204-206](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/linger_pool.rs#L204-L206)）。为什么？

因为 `QueueEnt` 作为枚举，其 `size` 等于最大变体加判别式，且 `VecDeque<QueueEnt>` 移动元素时要复制整个枚举（含 `OwnedHandle` 字段）。而这两个变体都可以**用一个指针表达**：

- `Handle` 变体：句柄值本身（`HANDLE`，一个指针大小的整数）。
- `IndirectHandle` 变体：指向堆对象（`Box`/`Arc`）的指针。

于是用一个 **低比特标记（low-bit tagging）** 把两者压进同一个指针：

- 指针最低位 = `0` → 是裸句柄（句柄值原样存，反正它是 4 的倍数，最低位必为 0）。
- 指针最低位 = `1` → 是间接句柄（真实指针 = 存的值清除最低位）。

这把每个队列项精确压到 8 字节（64 位下），且区分变体零额外开销。

#### 4.5.2 核心流程：编解码

`into_raw`（编码，[src/os/windows/linger_pool.rs:122-135](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/linger_pool.rs#L122-L135)）：

```rust
fn into_raw(self) -> *mut () {
    match self {
        Self::Handle(h) => ManuallyDrop::new(h).0.as_raw_handle().cast(),
        // 句柄值原样存（保证是 4 的倍数，低 2 位为 0）
        Self::IndirectHandle(bh) => (bh.into_raw() as usize | 1) as *mut (),
        // 堆指针 | 1，打上「间接」标记
    }
}
```

`from_raw`（解码，[src/os/windows/linger_pool.rs:137-145](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/linger_pool.rs#L137-L145)）：

```rust
unsafe fn from_raw(raw: *mut ()) -> Self {
    if raw as usize & 1 == 1 {                       // 最低位为 1 → 间接
        let raw = (raw as usize & !1) as *mut ();    // 清掉标记位还原指针
        Self::IndirectHandle(unsafe { DynHandleOwner::from_raw(raw) })
    } else {                                         // 最低位为 0 → 裸句柄
        Self::Handle(HandleFini(unsafe { OwnedHandle::from_raw_handle(raw.cast()) }))
    }
}
```

#### 4.5.3 源码精读：两个不变式如何被守护

低比特标记成立依赖两个前提，代码里各有保证：

1. **裸句柄最低位为 0**：靠 Windows 内核保证（句柄是 4 的倍数），代码注释写明「Windows handles don't conflict with low-bit-tagging because the OS guarantees that they're all multiples of 4」（同上 122-135 行）。
2. **堆指针最低位为 0**：靠对齐保证。`DynHandleOwner::boxed` 构造时 `assert!(align_of::<Self>() >= 2)`（[src/os/windows/linger_pool.rs:86-98](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/linger_pool.rs#L86-L98)），断言堆分配对齐至少 2，即最低位必为 0，可安全借用。

另一个精巧点：`into_raw` 用 `ManuallyDrop::new(h).0` **取出内部值而不触发 drop**——因为所有权已经转移到裸指针，若此时 drop 就会把句柄关掉，队列里就存了个野值。`from_raw` 则用 `OwnedHandle::from_raw_handle` 重新接管所有权，形成「编码 = 析构抑制 + 取值；解码 = 重获所有权」的对称闭环。

#### 4.5.4 小练习与答案

**练习 1**：为什么标记位只用了最低 1 位，而 Windows 句柄有 2 位（低 2 位）可用？

**答案**：因为「间接句柄」的堆指针只保证对齐 ≥ 2（最低 1 位为 0），不保证 4 对齐。两种变体共享同一种编码方案，标记位必须取两者都「保证为 0」的交集，即最低 1 位。所以只用 1 位即可区分，剩下高位不受影响。

**练习 2**：如果删掉 `DynHandleOwner::boxed` 里的 `assert!(align_of::<Self>() >= 2, ...)`，最坏后果是什么？

**答案**：理论上若某个平台/类型对齐为 1，堆指针最低位可能为 1，与标记位冲突，导致 `from_raw` 把一个「间接句柄」误判为「裸句柄」，进而把指针值当成 `HANDLE` 去 `FlushFileBuffers`/`CloseHandle`——未定义行为、极可能崩溃或损坏其它句柄。这个 assert 是该 unsafe 编码的安全闸。

## 5. 综合实践

**任务**：画出一条「`drop` 一个同步 named pipe `PipeStream`（写过数据、未显式 flush）」到「后台线程真正 `FlushFileBuffers` 并关闭句柄」的完整路径，并标注两个水位触发点。

请按下面的步骤完成（源码阅读型实践，可在任意平台阅读，但行为只在 Windows 生效）：

1. **起点——写**：从 [src/os/windows/named_pipe/stream/impl/send.rs:5-9](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/impl/send.rs#L5-L9) 的 `send` 出发，确认写成功后调了 `needs_flush.mark_dirty()`，把状态从 `No` 推到 `Once`。

2. **drop 判定**：进入 [src/os/windows/named_pipe/stream.rs:79-85](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream.rs#L79-L85) 的 `RawPipeStream::drop`。`ManuallyDrop::take` 取出 `AdvOwnedHandle`，`needs_flush.get_mut()` 为真 → 调 `linger_pool::linger(h)`。

3. **入口派发**：进 [src/os/windows/linger_pool.rs:21-24](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/linger_pool.rs#L21-L24) 的 `linger`：`h.into()` 得到 `OwnedHandle`，包成 `HandleFini`，再包成 `QueueEnt::Handle`，调 `linger_ent`（[src/os/windows/linger_pool.rs:37-43](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/linger_pool.rs#L37-L43)）。

4. **持久线程存在性判定**：若 `HAS_PERSISTENT_THREAD` 为 false → `fetch_or` 选中创建者，`spawn_persistent_thread` 把这个 `QueueEnt` 作为 `first_h` 带进新线程；否则尝试 `QUEUE.enqueue`。

5. **入队/溢出（高水位触发点 ①）**：`enqueue`（[src/os/windows/linger_pool.rs:215-222](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/linger_pool.rs#L215-L222)）发现 \(L \geq 64\) 返回 `Err` → `spawn_high_wm_thread` 开临时线程自带任务；否则 `into_raw` 把 `QueueEnt::Handle` 编成裸句柄指针压入 `VecDeque`，`notify_one` 唤醒消费者。

6. **后台消费（低水位触发点 ②）**：持久/临时线程在 `persistent_thread_main`/`temporary_thread_main`（[src/os/windows/linger_pool.rs:243-263](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/linger_pool.rs#L243-L263)）里 `QUEUE.get()`，`dequeue_and_check_watermark` 出队并解码（`from_raw` 重建 `HandleFini`）。`drop(h)` 触发 `HandleFini::drop`（[src/os/windows/linger_pool.rs:113-116](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/linger_pool.rs#L113-L116)）→ `c_wrappers::flush`（[src/os/windows/c_wrappers.rs:152-154](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/c_wrappers.rs#L152-L154)）阻塞到对端读完 → 随后 `OwnedHandle::drop` 关闭句柄。消费后若 `above_wm`（\(L > 8\)）则 `spawn_low_wm_thread` 拉帮手。

7. **产物**：把上述 7 步画成一张时序图/流程图，在步骤 5 标注「高水位触发点（\(L \geq 64\) → 立即开临时线程）」，在步骤 6 标注「低水位触发点（消费后 \(L > 8\) → 开辅助线程）」。

> 进阶（可选）：把起点换成 tokio 版 `RawPipeStream::drop`（[src/os/windows/named_pipe/tokio/stream.rs:70-79](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/tokio/stream.rs#L70-L79)），对比它为何走 `linger_boxed` 而非 `linger`（因为它持的是 `InnerTokio` 枚举，不能直接 `Into<OwnedHandle>`）。

## 6. 本讲小结

- **limbo 问题**：Windows 管道写端若在有未读缓冲时直接 `CloseHandle`，会丢弃数据、让对端收到 `BrokenPipe`/EOF。interprocess 的解法是 `drop` 时把脏句柄移交给后台 `linger_pool`，由后台线程**先 `FlushFileBuffers`（阻塞到对端读完）、后关闭**。
- **`NeedsFlush` 三态脏标记**：`No`/`Once`/`Always` 用单字节 `AtomicEnum` 存储。`mark_dirty` 推 `No→Once`，`on_clone` 直接钉死 `Always`（克隆后无法可靠追踪），`take` 消费 `Once` 或对 `Always` 恒真——既决定是否进 limbo，又实现了「连续 flush 第二次是 no-op」。
- **弹性线程池**：1 个永不退出的持久线程值班 + 按需创建、500ms 空闲即退出的临时线程。`HAS_PERSISTENT_THREAD` 的 `fetch_or` 无锁选出唯一的持久线程创建者。
- **高低水位调度**：高水位 64 是队列上限，达到即溢出开临时线程；低水位 8 是负载跟随阈值，消费者发现消费后队列仍 \(>8\) 就开辅助线程排空。
- **低比特标记指针**：`QueueEnt` 的两种变体（裸句柄 / 堆指针）压进单个 `*mut ()`，用最低位区分——裸句柄靠「Windows 句柄恒为 4 的倍数」、堆指针靠 `assert!(align_of >= 2)` 保证最低位可用。
- **先刷新后关闭的所有权闭环**：`HandleFini::drop` 调 `flush`、随后 `OwnedHandle::drop` 关闭，drop 顺序天然保证刷新先于关闭；`into_raw`/`from_raw` 用 `ManuallyDrop` 与 `from_raw_handle` 在裸指针与所有权对象间安全往返。

## 7. 下一步学习建议

- **u8-l2（maybe_arc）**：本讲反复出现的「间接句柄」`DynHandleOwner`、`LingerableArc`，以及 tokio 流 `drop` 时为何走 `linger_boxed`，都和 `MaybeArc` 的「未拆分零开销、拆分才升级 Arc」设计紧密相关。读完 maybe_arc 再回看 `linger_arc` 会豁然开朗。
- **u7-l2（句柄/FD 抽象与所有权管理）**：`AdvOwnedHandle`（低比特标记叠加布尔状态）与本讲 `QueueEnt` 的低比特标记是同一类技巧的不同应用，可对照阅读。
- **u6-l2（异步 Listener 与 Stream）**：tokio 路径的 limbo 刷新走 `tokio_flusher`（[src/os/windows/tokio_flusher.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/tokio_flusher.rs)）的 `spawn_blocking`，与本讲的「直接移交给 linger 线程池」是两条不同的刷新路径，值得对比「显式 `flush()` 用 TokioFlusher、`drop` 用 linger_pool」的分工。
- **直接续读源码**：带着本讲的路径图重读 [src/os/windows/linger_pool.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/linger_pool.rs) 全文，重点体会 `lk_loop_timeout`（[src/os/windows/linger_pool.rs:173-201](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/linger_pool.rs#L173-L201)）里 `Condvar::wait_timeout` 的剩余时间计算——这是临时线程 500ms 超时精确实现的细节。
