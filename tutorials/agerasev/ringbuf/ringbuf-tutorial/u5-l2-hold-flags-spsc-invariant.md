# Hold flags：保证单生产者单消费者的不变量

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚 ringbuf 的 **SPSC（单生产者单消费者）不变量** 到底是什么，以及它对无锁安全的意义。
- 读懂 `SharedRb` 中 `read_held` / `write_held` 两个原子 `bool` 字段，以及 `RingBuffer::hold_read` / `hold_write` 这两个 `unsafe` 方法的「置位并返回旧值」语义。
- 在源码里追踪一次完整的 hold 标志生命周期：包装器 `new` 时置位、`close` / `Drop` 时复位、重复拆分时如何触发 `panic`。
- 解释为什么 `SharedRb` 没有在 trait 上写 `T: Send`，却仍然能在多线程间安全使用，以及为什么 `push_overwrite` 必须独占访问。

本讲是上一讲（u4-l2，Direct 包装器）的深化：上一讲你已经见过 `Direct::new` 里那两行 `assert!(!hold_*(true))`，本讲把这两行背后的整套机制讲透。

## 2. 前置知识

阅读本讲前，你需要先建立以下认知（这些在依赖讲义中已讲过）：

- **环形缓冲区的双索引模型**：`read` 指向最旧元素，`write` 指向下一个空槽，二者在 `0..2*capacity` 取值（见 u2-l1）。
- **Occupied 与 vacant 槽位互斥**：当前已写入但未读走的元素落在 `read..write` 区间（occupied），未写的空槽落在 `write..read+capacity` 区间（vacant）。这两段在任意时刻都不重叠。
- **Direct / Frozen / Caching 三种包装器**：包装器是「持有对底层缓冲区引用 + 编码读写权限」的句柄；`Prod` 拥有写权、`Cons` 拥有读权（见 u4-l2、u4-l3、u4-l4）。
- **原子与内存顺序**：`Acquire` 读 / `Release` 写如何建立跨线程的 happens-before（见 u5-l1）。本讲涉及一种新的顺序 `AcqRel`，用于「读改写」操作。

两个本讲会用到的术语：

- **不变量（invariant）**：程序在运行中始终成立的性质。SPSC 不变量就是「任意时刻至多一个生产者、至多一个消费者」。
- **hold 标志（hold flag）**：缓冲区内部记录「写端 / 读端是否已被某个句柄占用」的两个布尔位。本讲的主角。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/rb/shared.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs) | 多线程 `SharedRb` 的实现：定义 `read_held` / `write_held` 两个 `AtomicBool` 字段，并用 `swap` 实现 `hold_read` / `hold_write`。 |
| [src/traits/ring_buffer.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/ring_buffer.rs) | `RingBuffer` trait：声明 `hold_read` / `hold_write`（unsafe）与 `push_overwrite`。 |
| [src/wrap/direct.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/direct.rs) | Direct 包装器：`new` 置位、`close` / `Drop` 复位、`into_rb_ref` 收尾。 |
| [src/wrap/frozen.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs) | Frozen 包装器：同样的置位 / 复位逻辑，外加 `commit` 收尾。Caching 也复用这一层。 |
| [src/traits/observer.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/observer.rs) | `Observer` trait：提供只读的 `read_is_held` / `write_is_held` 查询。 |

## 4. 核心概念与源码讲解

### 4.1 SPSC 不变量：为什么需要 hold 标志

#### 4.1.1 概念说明

ringbuf 自我定位为 **无锁 SPSC FIFO**。SPSC 三个字母是整套设计的命门：**S**ingle **P**roducer **S**ingle **C**onsumer——任意时刻，整个缓冲区至多有一个生产者往里写，至多有一个消费者从里读。

为什么要强制这个约束？因为它是「无锁却安全」的前提。设想两个生产者同时 `try_push`：它们都要先读 `write` 索引判断空位、再写元素、再推进 `write` 索引。如果没有锁，两个生产者可能读到同一个 `write`、写到同一个槽，互相覆盖——这就是数据竞争。消费者一侧同理。

SPSC 约束从根本上排除了「同侧并发」：只要保证「只有一个写者、只有一个读者」，那么：

- 写者独占 `write` 索引与全部 vacant 槽；
- 读者独占 `read` 索引与全部 occupied 槽；
- 而 vacant 与 occupied 两段永远不相交（见前置知识）。

于是写者和读者**永远不会同时触碰同一个槽**。这正是「无锁」得以成立的核心：不需要锁来保护数据本身，只需要用 Acquire/Release 顺序同步两个索引（u5-l1 已讲）。

#### 4.1.2 核心流程

那么「至多一个生产者 / 消费者」这个约束由谁来强制？答案就是两个 hold 标志。它们的工作方式像一个「取号锁」：

```text
缓冲区创建时：write_held = false, read_held = false

有人想成为生产者（new 一个 Prod）：
  old = swap(write_held, true)   # 把标志设为 true，同时拿到旧值
  assert!(old == false)          # 旧值必须是 false（之前没人占）
  # 否则 panic：已经有生产者了

生产者销毁（drop Prod）：
  swap(write_held, false)        # 归还，标志复位为 false
```

`hold_read` 对消费者做完全对称的事。两个标志相互独立：你可以有「一个生产者 + 一个消费者」（最常见的 split 后状态），也可以两个都没有（未拆分的缓冲区），但**绝不能**有两个生产者或两个消费者。

> 官方文档在 [lib.rs 的 "Hold flags" 段](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/lib.rs#L144-L147) 把它一句话总结为：环形缓冲区同一时刻至多一个生产者、至多一个消费者，这两个标志指示「现在是否还能安全地再拿一个生产者或消费者」。

#### 4.1.3 源码精读

先把 hold 标志在 `SharedRb` 里的物理形态看清楚。`SharedRb` 一共有 4 个状态字段，本讲关注后两个：

[shared.rs:51-57](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L51-L57) — `SharedRb` 的字段定义。前两个字段是 `CachePadded<AtomicUsize>` 存读写索引（u5-l1 讲过，`CachePadded` 防伪共享）；`read_held` 和 `write_held` 就是两个普通 `AtomicBool`，分别编码「读端 / 写端是否被占用」。

缓冲区构造时这两个标志初始化为 `false`，表示两端都还空着、可以拆分：

[shared.rs:66-75](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L66-L75) — `from_raw_parts` 把 `read_held` / `write_held` 都设为 `AtomicBool::new(false)`。这是「未占用」的初始状态。

而对这两个标志的**查询**入口在 `Observer` trait 里，是两个安全的只读方法：

[observer.rs:41-44](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/observer.rs#L41-L44) — `read_is_held` / `write_is_held` 声明。它们只是观察、不修改，因此安全。

`SharedRb` 用一次 `Acquire` load 实现这两个查询：

[shared.rs:113-120](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L113-L120) — `SharedRb` 的 `read_is_held` / `write_is_held` 实现：`self.read_held.load(Ordering::Acquire)`。用 `Acquire` 是为了确保读到的是另一个线程最新发布的占用状态。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：从官方文档确认 hold 标志的语义，并对照源码找到它的物理存储。

**操作步骤**：

1. 打开 [lib.rs 第 144–147 行](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/lib.rs#L144-L147)，阅读 "Hold flags" 段。
2. 在 `src/rb/shared.rs` 中找到 `read_held` / `write_held` 字段的定义与初始化。

**需要观察的现象**：库把 hold 标志列为「实现细节三件套」之一（Storage / Indices / Hold flags），与索引并列，说明它在设计上是一等公民，而非可有可无的调试信息。

**预期结果**：你能用自己的话回答「为什么一个无锁队列需要两个布尔标志」——因为它们把 SPSC 这个数学性质变成了运行时可检查的不变量。

#### 4.1.5 小练习与答案

**练习 1**：如果允许两个生产者同时存在，会破坏哪一条安全性？  
**答案**：会破坏「写者独占 write 索引与 vacant 槽」的假设。两个生产者可能读到同一个 `write`、写入同一个槽、或交错推进 `write` 索引，导致元素被覆盖或丢失——即对 vacant 槽和 `write` 索引的数据竞争。

**练习 2**：hold 标志能否用普通 `bool` 而非 `AtomicBool` 实现（针对 `SharedRb`）？为什么？  
**答案**：不能。`SharedRb` 跨线程共享，生产者线程要置位 / 复位 `write_held`，消费者线程要查询或置位 `read_held`；普通 `bool` 的并发读写是未定义行为。必须用 `AtomicBool` 配合恰当的内存顺序。

---

### 4.2 hold 标志的存储与 hold_read / hold_write 接口

#### 4.2.1 概念说明

`read_is_held` / `write_is_held` 只能「看」，不能「改」。真正去占位 / 退位的是另外两个方法：`hold_read` 和 `hold_write`，它们定义在 `RingBuffer` trait 上，并且是 **unsafe** 的。

它们的关键设计是 **「置位并返回旧值」**（test-and-set 语义）：调用 `hold_write(flag)` 会把 `write_held` 设成 `flag`，同时返回它**原来的值**。这种「读改写」一步原子完成，是构建「互斥占用」的经典原语：

- 想占用：`let old = hold_write(true); assert!(!old);` —— 若旧值是 `false`（没人占），占用成功；若旧值是 `true`，说明已被占用。
- 想释放：`hold_write(false);` —— 把标志退回 `false`，归还占用权。

为什么是 `unsafe`？因为库无法阻止调用方写出危险的组合，例如「明明还有一个活着的生产者，却把 `write_held` 设回 `false` 让第二个生产者进来」。这种调用会直接打破 SPSC 不变量，引发数据竞争。`unsafe` 把这份责任交还给调用方——在 ringbuf 内部，只有包装器（`Direct` / `Frozen`）在严格受控的 `new` / `close` 路径上调用它们。

#### 4.2.2 核心流程

`hold_read` / `hold_write` 的统一流程：

```text
unsafe fn hold_write(&self, flag: bool) -> bool:
    返回 self.write_held.swap(flag, AcqRel)
    #        ^^^^^ 该原子操作：写入新值 flag，同时返回旧值
```

`swap` 是「读改写」（read-modify-write）操作，因此内存顺序必须是 `AcqRel`（同时具备 Acquire 和 Release 语义）：

- **Release 部分**：把本线程对占用状态的修改发布出去，让随后查询 `write_is_held`（Acquire load）的线程能看到。
- **Acquire 部分**：读到其他线程此前发布的占用状态，避免本线程基于过期信息做决策。

这与 u5-l1 讲的「索引用 Acquire 读 / Release 写」是同一套思路，只是这里操作的是 hold 标志而非索引。

#### 4.2.3 源码精读

先看 trait 声明，注意它的 `unsafe` 标注和安全约定：

[ring_buffer.rs:7-24](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/ring_buffer.rs#L7-L24) — `RingBuffer` trait 声明 `hold_read` / `hold_write`。注释明确两点：**返回旧值**；**安全约定是「当 consumer / producer 仍然存在时，不得把对应标志设为 false」**。这条约定正是 unsafe 的根源——库信任调用方不会在生产者还活着时释放写占用。

再看 `SharedRb` 的具体实现，就一行 `swap`：

[shared.rs:137-146](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L137-L146) — `SharedRb` 的 `RingBuffer` 实现：`self.read_held.swap(flag, Ordering::AcqRel)` 和 `self.write_held.swap(flag, Ordering::AcqRel)`。test-and-set 一步完成，`AcqRel` 保证跨线程可见性。

作为对照，单线程的 `LocalRb` 用 `Cell<bool>` 而非 `AtomicBool`（单线程无需原子），但对外接口 `hold_read` / `hold_write` 的语义完全一致：

[local.rs:121-130](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/local.rs#L121-L130) — `LocalRb` 用 `Cell::replace(flag)`（返回旧值）实现，与 `AtomicBool::swap` 行为相同。字段定义见 [local.rs:22-25](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/local.rs#L22-L25) 的 `Endpoint { index, held: Cell<bool> }`。

#### 4.2.4 代码实践（可运行）

**实践目标**：用 `read_is_held` / `write_is_held` 观察 split 前后 hold 标志的真实变化。

**操作步骤**：

1. 在项目根目录新建临时文件 `examples/observe_hold.rs`（**示例代码**，便于观察，做完可删除）：

   ```rust
   use ringbuf::{traits::*, HeapRb};

   fn main() {
       let rb = HeapRb::<i32>::new(4);
       // 未拆分：两端都未被占用
       println!("before split: write_is_held={}, read_is_held={}",
                rb.write_is_held(), rb.read_is_held());

       let (prod, cons) = rb.split(); // split 内部置位两个标志

       // split 后 rb 已被 move，通过 prod/cons 包装器观察底层标志
       println!("after  split: write_is_held={}, read_is_held={}",
                prod.write_is_held(), cons.read_is_held());
   }
   ```

2. 运行 `cargo run --example observe_hold`。

**需要观察的现象**：第一行打印两个 `false`，第二行打印两个 `true`。

**预期结果**：验证了「split 之前 hold 标志为 false、split 之后被置为 true」。这证明 `split()` 内部确实调用了 `hold_write(true)` 和 `hold_read(true)`。

#### 4.2.5 小练习与答案

**练习 1**：`hold_write(true)` 返回值的含义是什么？为什么 `assert!(!hold_write(true))` 能检测重复占用？  
**答案**：它返回「调用前的旧值」。若旧值是 `false`（之前没人占用），`!false == true`，断言通过、占用成功；若旧值是 `true`（已被占用），`!true == false`，断言失败、触发 panic。而且此时 `swap(true, ...)` 把 `true` 换成了 `true`，标志仍保持被占用，不会破坏原有状态。

**练习 2**：为什么 `hold_read` / `hold_write` 用 `AcqRel` 而不是单纯的 `Release`？  
**答案**：`swap` 是读改写操作，既要读取（可能读到别的线程刚发布的占用状态）又要写入（发布本线程的修改）。纯 `Release` 只保证写发布、不保证读到的值是最新的；`AcqRel` 同时满足 Acquire（看到他线程发布）和 Release（发布自己修改），是读改写操作的正确选择。

---

### 4.3 包装器生命周期：new 置位、close / drop 复位、重复拆分 panic

#### 4.3.1 概念说明

有了 `hold_read` / `hold_write` 这个原语，谁来调用它、在什么时候调用？答案是**包装器**（`Direct` / `Frozen` / `Caching`）。包装器把 hold 标志的置位 / 复位绑定到自己的生命周期上，从而把「调用 unsafe 的 `hold_*`」这件危险事变成对用户完全安全的操作——用户只要正常创建和销毁包装器即可。

一个包装器句柄（如 `Prod` / `Cons`）的生命周期里，hold 标志经历三个关键时刻：

1. **`new`（创建）**：构造时立刻 `hold_*(true)` 并断言旧值为 `false`。占用成功或 panic，二者必居其一。
2. **存活期**：标志保持 `true`，阻止任何第二个同侧句柄出现。
3. **`close` / `Drop`（销毁）**：`hold_*(false)` 归还占用，标志复位为 `false`，让缓冲区可以被再次拆分。

这套机制把 SPSC 不变量从「靠程序员自觉」升级为「靠运行时强制」——你想偷偷造第二个生产者？会在 `new` 阶段直接 panic。

#### 4.3.2 核心流程

以 `Prod`（写端，`P=true, C=false`）为例的生命周期：

```text
Direct::new(rb):                  # 或 Frozen::new / Caching::new
    if P: assert!(!rb.hold_write(true))   # 置位 + 断言，失败则 panic
    if C: assert!(!rb.hold_read(true))    # 读端对称
    返回包装器（标志现在为 true）

用户使用 prod.try_push(...)       # 存活期，标志保持 true

prod 被 drop（或 into_rb_ref）:
    close():
        if P: rb.hold_write(false)        # 复位
        if C: rb.hold_read(false)
    # 标志回到 false，缓冲区可再次拆分
```

`Cons`（读端）把 `P` / `C` 对调即可。注意 `Frozen` 和 `Caching` 在销毁时还多一步 `commit()`（把本地缓存的索引写回底层），但 hold 标志的置位 / 复位逻辑与 `Direct` 完全相同。

#### 4.3.3 源码精读

**置位点**——`Direct::new`：

[direct.rs:42-50](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/direct.rs#L42-L50) — `Direct::new`：若 `P`（写权）则 `assert!(!unsafe { rb.rb().hold_write(true) })`，若 `C`（读权）则对 `hold_read` 做同样的事。这就是「重复拆分会 panic」的源头。注意 `Direct<R, P, C>` 用 const generic 布尔 `P` / `C` 在编译期编码读写权限（u4-l2 已讲）。

`Frozen::new` 和 `Caching::new` 逐字复用这套断言：

- [frozen.rs:40-52](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs#L40-L52) — `Frozen::new`：同样的 `assert!(!hold_write(true))` / `assert!(!hold_read(true))`，然后转交给 `new_unchecked`。
- [caching.rs:26-32](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/caching.rs#L26-L32) — `Caching::new`：直接 `Frozen::new(rb)`，因此 Caching 的置位逻辑完全继承自 Frozen。这也是 `SharedRb::split` 默认产出 `CachingProd` / `CachingCons` 时实际执行置位的地方。

**复位点**——`Direct::close`：

[direct.rs:63-73](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/direct.rs#L63-L73) — `Direct::close`（unsafe，仅供内部）：`if P { hold_write(false) }`、`if C { hold_read(false) }`，把占用归还。`Frozen::close` 见 [frozen.rs:72-79](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs#L72-L79)，逻辑相同。

**何时调用 close**——有两条路径都会触发复位：

1. **`Drop`**：句柄正常被丢弃时。`Direct` 的 `Drop` 直接调 `close`：

   [direct.rs:148-152](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/direct.rs#L148-L152) — `Direct::drop` 调用 `close()` 复位标志。

   `Frozen` 的 `Drop` 在 `close` 之前先 `commit()`，保证未提交的本地索引不丢失（见 u4-l3）：

   [frozen.rs:199-204](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs#L199-L204) — `Frozen::drop`：先 `commit()` 再 `close()`。`Caching` 没有自己的 `Drop`，它持有的 `Frozen` 字段被丢弃时会自动触发这条路径，从而释放 hold 标志。

2. **`into_rb_ref`**：当用 `into_rb_ref` 把包装器拆解回底层引用时（u4-l1 讲过，它用 `ManuallyDrop` 绕过 `Drop`，所以必须手动 `close`）：

   [direct.rs:81-88](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/direct.rs#L81-L88) — `Direct::into_rb_ref`：先 `close()` 复位，再用 `ptr::read` 搬出钥匙。`Frozen` 的版本见 [frozen.rs:88-96](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs#L88-L96)，多一步 `commit()`。

#### 4.3.4 代码实践（可运行，复现 panic）

**实践目标**：亲手复现「对已 split 的缓冲区再次拆分会 panic」的现象，并解释原因。

**操作步骤**：

1. 在项目根目录新建临时文件 `examples/double_split.rs`（**示例代码**，用于复现 panic，做完可删除）：

   ```rust
   use ringbuf::{traits::*, HeapRb};
   use std::sync::Arc;

   fn main() {
       // 装进 Arc，这样可以从同一个底层缓冲区克隆出多份共享句柄
       let rb: Arc<HeapRb<i32>> = Arc::new(HeapRb::new(4));

       // 第一次 split：CachingProd::new 调用 hold_write(true) 返回 false（通过），
       //               CachingCons::new 调用 hold_read(true) 返回 false（通过）。
       // 标志现在 write_held=true, read_held=true。
       let (prod1, cons1) = rb.clone().split();

       // 第二次 split：CachingProd::new 再次 hold_write(true)，这次返回 true（已被占用），
       //               assert!(!true) => panic!
       let (prod2, cons2) = rb.split(); // 期望在此处 panic

       // 这一行不会执行
       let _ = (prod1, cons1, prod2, cons2);
   }
   ```

2. 运行 `cargo run --example double_split`。

**需要观察的现象**：程序在第二次 `split()` 处 panic，打印类似 `assertion failed: !unsafe { rb.rb().hold_write(true) }`，并指向 `frozen.rs` 的 `Frozen::new`。

**预期结果**：panic 信息直接暴露了 hold 标志的检查逻辑——`assert!(!hold_write(true))` 失败，证明 write 端已被第一个生产者占用，SPSC 不变量在运行时被强制。

> 说明：`Arc<HeapRb<T>>` 之所以能调用 `split()`，是因为库为 `Arc<SharedRb<S>>` 实现了 `Split`（见 [shared.rs:163-171](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L163-L171)）。本例用 Arc 只是为了能保留一份克隆做第二次拆分；若直接 `HeapRb::new(4).split()`，所有权被消费，连第二次拆分的机会都没有。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Obs`（只读观察者，`P=false, C=false`）可以自由 `Clone`，而 `Prod` / `Cons` 不能？  
**答案**：`Obs` 在 `new` 时 `P` 和 `C` 都是 `false`，根本不调用 `hold_write` / `hold_read`，不占用任何一端（见 [direct.rs:32-36](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/direct.rs#L32-L36) 的 `Clone for Obs`）。它只是只读观察、不触碰 SPSC 不变量，因此可以任意克隆。`Prod` / `Cons` 各自占用一端，克隆会制造第二个同侧句柄，直接违反 SPSC。

**练习 2**：`prod1` 必须在第二次 `split()` 之前仍然存活（未被 drop），为什么？如果先 `drop(prod1)` 再第二次 `split()`，结果会怎样？  
**答案**：`prod1` 存活期间 `write_held=true`，第二次 `split()` 才会 panic。若先 `drop(prod1)`，它的 `Drop`（经 Frozen）会 `commit()` + `hold_write(false)`，把写端释放回 `false`，此时第二次 `split()` 反而能成功（注意此时 `cons1` 仍占用读端，所以第二次会卡在 `hold_read` 的断言上 panic——除非连 `cons1` 也 drop 掉）。这正好说明 hold 标志的复位完全跟随句柄的生命周期。

---

### 4.4 不要求 T: Send 却能跨线程，以及并发 overwrite 为何不安全

#### 4.4.1 概念说明

本模块把两件看似无关、实则同源的事放在一起讲，因为它们都根植于 SPSC 不变量。

**第一件事：为什么 `SharedRb` 没有在 trait 上写 `T: Send`，却仍能多线程使用？**

如果你翻看 `RingBuffer` / `Producer` / `Consumer` 这些 trait 的定义，会发现它们**没有任何 `T: Send` 或 `T: Sync` 的约束**。官方文档在 `SharedRb` 的注释里点明了用意：

> 即使 `T: !Send`，环形缓冲区也能正常工作——直到你试图把它的 producer 或 consumer 发送到另一个线程。

这句话有两层含义：

1. **trait 层不设约束**：于是 `HeapRb<Rc<i32>>`（`Rc` 非 `Send`）也能编译通过、在单线程里正常用。库把「能不能跨线程」的决定权推迟到具体类型层面。
2. **跨线程时由 auto-trait 接管**：当你真的想把一个 `Producer` 句柄 move 到另一线程，Rust 的自动 trait 推导会要求该句柄类型满足 `Send`。句柄内部包着 `Arc<SharedRb<S>>`，而 `Arc<X>: Send` 要求 `X: Send + Sync`，于是约束最终落到存储后端 `S`（它装着 `MaybeUninit<T>`）上——也就是落到 `T` 身上。所以约束不是不存在，而是「用到才出现」。

**关键的安全性来源**：为什么两个线程共享同一个 `SharedRb` 不会产生对 `T` 的数据竞争？正是 SPSC 不变量 + hold 标志。生产者只写 vacant 槽、消费者只读 occupied 槽，二者集合不相交，所以**永远不会并发访问同一个 `T`**。数据是「生产者写完 → 经 Release/Acquire 顺序交接 → 消费者读」的单向 hand-off，而非两线程同时读写同一份数据。hold 标志把这种 hand-off 的「单写单读」前提固化下来，是无锁安全的基石。

**第二件事：为什么并发 `push_overwrite` 不安全、必须用 mutex 保护？**

`push_overwrite`（满了就丢弃最旧元素再写新元素）的特别之处在于：它**同时动 read 和 write 两个索引**——先 `try_pop`（推进 read）、再 `try_push`（推进 write）。但在 SPSC 模型里，read 索引归消费者独占、write 索引归生产者独占。`push_overwrite` 让「一个端」同时改两个索引，这就打破了独占约定：如果此时还有一个消费者在并发 `try_pop`，两者会对 read 索引 / occupied 槽产生竞争。

因此 `push_overwrite` 的签名是 `&mut self`（独占借用），并且只在**未拆分**的缓冲区（实现了 `RingBuffer` 的拥有者）上可用；split 之后的 `Prod` / `Cons` 只实现单侧 trait，类型层面就拿不到这个方法。要在并发环境下用 overwrite，文档明确建议**外加 mutex**。

#### 4.4.2 核心流程

**T: Send 的真相**：

```text
trait Producer: ... { /* 无 T: Send 约束 */ }       # trait 层不限制

HeapRb<Rc<i32>>                                     # 编译通过，单线程可用 ✓

要跨线程：
CachingProd<Arc<SharedRb<S>>>: Send
  ⟸ Arc<SharedRb<S>>: Send
  ⟸ SharedRb<S>: Send + Sync
  ⟸ S: Send + Sync
  ⟸ MaybeUninit<T>: Send + Sync
  ⟸ T: Send + Sync                                  # 约束在「真正跨线程」时才浮现
```

**overwrite 为何独占**：

```text
push_overwrite(&mut self, elem):        # 签名就要 &mut self（独占）
    if is_full(): try_pop()             # 推进 read 索引（本属消费者）
    try_push(elem)                      # 推进 write 索引（本属生产者）
    # 同时改两个索引 ⟹ 必须独占整个缓冲区
```

#### 4.4.3 源码精读

**T: Send 的注释**：

[shared.rs:27-30](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L27-L30) — `SharedRb` 的文档注释明确说明：没有显式的 `T: Send` 要求，缓冲区即使对 `T: !Send` 也能正常工作，直到你试图把 producer / consumer 发到另一个线程。

**overwrite 同时改两个索引**：

[ring_buffer.rs:26-33](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/ring_buffer.rs#L26-L33) — `push_overwrite` 的默认实现：满则先 `try_pop()`（消费者行为，动 read 索引），再 `try_push(elem)`（生产者行为，动 write 索引）。注意签名是 `&mut self`，独占整个缓冲区。

**官方对并发 overwrite 的建议**：

[lib.rs:101-102](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/lib.rs#L101-L102) — 文档原话：`push_overwrite` 需要对环形缓冲区的独占访问，因此若要并发执行，必须用 mutex 或其他锁把它保护起来。

可运行的 overwrite 用法见 [examples/overwrite.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/examples/overwrite.rs)——注意它操作的是**未 split** 的 `HeapRb`，全程单线程独占。

#### 4.4.4 代码实践（源码阅读 + 推理）

**实践目标**：理解为什么 overwrite 必须独占，并验证 split 之后根本调不到 `push_overwrite`。

**操作步骤**：

1. 阅读 [ring_buffer.rs:26-33](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/ring_buffer.rs#L26-L33) 的 `push_overwrite`，确认它同时调用了 `try_pop`（动 read）和 `try_push`（动 write）。
2. 对照 u3-l4：split 出来的 `Prod` 只实现 `Producer`、`Cons` 只实现 `Consumer`，二者都**不**实现 `RingBuffer`，因此 `push_overwrite` 在类型层面就不可调用。
3. 写一小段**示例代码**验证类型层面被拒（新建 `examples/overwrite_split.rs`，预期**编译失败**）：

   ```rust
   use ringbuf::{traits::*, HeapRb};

   fn main() {
       let (mut prod, _cons) = HeapRb::<i32>::new(4).split();
       prod.push_overwrite(0); // 预期编译错误：Prod 没有实现 RingBuffer
   }
   ```

   运行 `cargo build --example overwrite_split`。

**需要观察的现象**：第 3 步编译失败，报错大致是 `no method named push_overwrite found for struct CachingProd<...>`。

**预期结果**：确认 overwrite 在 split 后既「语义上」也「类型上」不可用。要并发使用 overwrite，唯一正确做法是用 `Mutex<HeapRb<T>>` 包一层，把对整个缓冲区的访问串行化（独占），这正是 lib.rs 文档的建议。

**关于 T: Send 的推理（无需运行）**：设想把上一模块 4.3.4 的缓冲区换成 `HeapRb<Rc<i32>>`，再尝试 `let (prod, cons) = rb.split(); std::thread::spawn(move || prod.try_push(...));`。由于 `Rc: !Send`，`prod` 不满足 `Send`，编译器会在 `spawn` 处直接拒绝——这印证了「约束在跨线程时才浮现」。完整行为待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `SharedRb` 跨线程时不会对单个 `T` 产生数据竞争？请用 SPSC 不变量解释。  
**答案**：SPSC 不变量（由 hold 标志强制）保证任意时刻只有一个生产者和一个消费者。生产者只写 vacant 槽、消费者只读 occupied 槽，而 vacant 与 occupied 在任意时刻不相交，因此两线程永远不会并发访问同一个 `T`。加上 write 索引的 Release store 与 read 索引的 Acquire load 建立的 happens-before，数据以单向 hand-off 安全传递，无需对 `T` 本身加锁。

**练习 2**：`push_overwrite` 的签名为什么是 `&mut self` 而不是 `&self`？  
**答案**：因为它要同时推进 read 和 write 两个索引（先 `try_pop` 动 read，再 `try_push` 动 write），等价于同时扮演消费者和生产者，必须独占整个缓冲区。`&mut self` 在编译期保证这种独占：同一时刻不可能有别的引用并存。split 出来的 `Prod` / `Cons` 只有 `&self` 的单侧方法且不实现 `RingBuffer`，自然调不到 overwrite。

---

## 5. 综合实践

把本讲的三条主线串起来，完成下面这个综合任务。

**任务**：写一个程序，用 `Arc<HeapRb<i32>>` 演示 hold 标志的完整生命周期，并解释每一步对应源码里的哪一行。

**要求**：

1. 创建 `Arc<HeapRb<i32>::new(2)`，打印 `write_is_held()` / `read_is_held()`（应为 `false, false`）。
2. `clone()` 后 `split()` 得到 `prod` / `cons`，通过它们打印底层标志（应为 `true, true`），并说明置位发生在 `Frozen::new`（[frozen.rs:40-52](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs#L40-L52)）。
3. 用 `prod` 跨一个线程 `try_push`、用 `cons` 在另一线程 `try_pop`，验证 SPSC hand-off 正常工作（参考 [shared.rs:27-50](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L27-L50) 的文档示例）。
4. `drop(prod)` 后，说明 `write_held` 经 `Frozen::drop` → `close`（[frozen.rs:199-204](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs#L199-L204)）复位为 `false`；此时若再 `split()`，将在 `hold_read` 断言处 panic（因为 `cons` 仍占用读端）。
5. 在报告中用一句话回答：如果把元素类型改成 `Rc<i32>`，第 3 步会发生什么？为什么？

**预期结果**：你能完整复述「创建 → split 置位 → 跨线程 hand-off → drop 复位 → 再拆分 panic」的全过程，并把它精确对应到 `hold_read` / `hold_write` 的源码行。第 5 问的答案是：`Rc: !Send` 导致 `prod` 不满足 `Send`，第 3 步在 `thread::spawn` 处编译失败，呼应「约束在跨线程时才浮现」。

> 综合实践涉及多线程与潜在 panic，若在你的环境运行结果与本讲描述不符，请以本地实际输出为准（待本地验证）。

## 6. 本讲小结

- **SPSC 不变量是无锁安全的根基**：至多一个生产者、至多一个消费者，保证 vacant 与 occupied 槽不相交，写者与读者永不同时触碰同一个元素。
- **hold 标志是不变量的运行时守卫**：`SharedRb` 用两个 `AtomicBool`（`read_held` / `write_held`）记录两端是否被占用；`RingBuffer::hold_read` / `hold_write` 提供 test-and-set 语义（`swap` 返回旧值，`AcqRel` 顺序）。
- **包装器把 unsafe 的 `hold_*` 绑定到安全生命周期**：`Direct` / `Frozen` / `Caching` 在 `new` 时 `assert!(!hold_*(true))` 置位、在 `close` / `Drop` / `into_rb_ref` 时 `hold_*(false)` 复位。
- **重复拆分会 panic**：对已 split 的缓冲区再次 `split()`，会在 `Frozen::new` 的 `hold_write` 断言处失败，SPSC 不变量被强制执行。
- **`T: Send` 约束推迟到跨线程时才浮现**：trait 层不限制，但句柄跨线程时由 auto-trait 推导要求 `T` 满足条件；其安全性正来自 SPSC 的单向 hand-off。
- **`push_overwrite` 必须独占**：它同时动 read / write 两个索引，签名 `&mut self`，只在未拆分缓冲区可用，并发使用需外加 mutex。

## 7. 下一步学习建议

- **u5-l3（MaybeUninit 与 unsafe 内存管理）**：hold 标志解决了「谁能写索引」，下一讲解决「槽位本身的初始化状态如何安全表达」，是 unsafe 体系的另一半。
- **回顾 u4-l1（Wrap 与 RbRef）**：本讲多次提到 `into_rb_ref` / `close`，结合那一讲能完整理解「钥匙 → 缓冲区」的销毁收尾。
- **阅读派生 crate 的同步原语**：async-ringbuf（`AtomicWaker`）和 ringbuf-blocking（`Semaphore`）都建立在 SPSC 之上，本讲的 hold 标志是它们「等待语义」能成立的前提——可以带着「为什么等待方确信只有一个对端」的问题去读 `async/src/rb.rs` 与 `blocking/src/rb.rs`。
- **跑 Miri 验证**（u8-l5）：用 `scripts/miri.sh` 校验本讲涉及的 `swap`、`AtomicBool`、`unsafe hold_*` 在弱内存序下无未定义行为，把「正确性论证」变成「工具验证」。
