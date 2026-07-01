# Chase-Lev 双端队列实现

## 1. 本讲目标

本讲是 crossbeam-deque 的内核篇，承接 [u6-l1 工作窃取模型与公共 API](u6-l1-deque-overview.md)（你已经知道 `Worker`/`Stealer`/`Injector`/`Steal` 的角色与用法），也用到 [u5-l3 Guard 与 pin 机制](u5-l3-epoch-guard.md) 的延迟回收知识。

学完本讲，你应当能够：

- 画出 Chase-Lev 双端队列的 `Inner{front, back, buffer}` 结构，解释幂次环形缓冲如何用位掩码把单调索引映射到槽位。
- 说清 `push` 为什么写 `back`、`pop` 在 FIFO（增 `front`）与 LIFO（减 `back`）两种风味下的方向差异，以及支撑这一切的内存序与 fence。
- 讲透 `steal` 的「读 `front` → fence → 读 `back` → 读缓冲 → CAS `front`」协议，并能回答本讲的核心问题：**为什么 `steal` 返回 `Retry` 时必须重试，而绝不能当成 `Empty`**。
- 理解 `resize` 如何扩容/缩容，以及为什么旧缓冲必须交给 epoch 的 `defer_unchecked` 延迟释放，而不能立刻 `free`。

---

## 2. 前置知识

### 2.1 工作窃取回顾

工作窃取调度里，每个工作线程有一条**私有**队列（`Worker`），它高频地往里 push/pop；别的线程偶尔来**偷**（`Stealer::steal`）。把高频自取留在无争用的快路径、把低频互偷放到慢路径，是工作窃取提升吞吐的核心。详见 u6-l1。

### 2.2 Chase-Lev 双端队列的直觉

Chase-Lev（David Chase & Yossi Lev, SPAA 2005）是一种**单所有者、多窃取者**的无锁双端队列，靠两个单调递增的整数索引描述队列范围：

- `back`（尾）：所有者 push 的写入端，索引只增（LIFO pop 时会临时减 1，但净趋势是增）。
- `front`（头）：窃取者 steal 的读取端，索引只增。

队列的有效元素就是索引区间 \([front, back)\)，长度：

\[
\text{len} = back - front \quad (\text{使用 wrapping\_sub})
\]

经典 Chase-Lev 里，所有者从 `back` 端 push **和** pop（栈式 LIFO），窃取者从 `front` 端 steal。这样所有者只动 `back`、窃取者只动 `front`，两端几乎没有写争用——这是它高并发的根基。crossbeam 在此基础上**额外支持 FIFO**：所有者可以选择从 `front` 端 pop，使队列变成 FIFO。

### 2.3 关键术语

- **单调索引**：`front`/`back` 是 64 位 `isize`，几乎永不回绕（\(2^{63}\) 次操作才回绕），因此索引本身就充当「代次」，**不需要**像 [u4-l1 ArrayQueue](u4-l1-array-queue.md) 那样额外编码 lap/stamp 来防 ABA。
- **幂次环形缓冲**：缓冲容量恒为 2 的幂，用位与 `index & (cap - 1)` 代替取模，把单调索引映射到固定槽位。
- **宽限期（grace period）**：epoch 回收中的概念（见 u5-l3/u5-l5），指「所有当时处于 pin 状态的线程都已 unpin」之后的安全时机。本讲里旧缓冲只能在此之后释放。

---

## 3. 本讲源码地图

本讲只精读两个文件，重点几乎全部在 `deque.rs` 的 Chase-Lev 部分：

| 文件 | 作用 |
|------|------|
| [crossbeam-deque/src/deque.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs) | 全部实现。其中 `Buffer`/`Inner`/`Worker`/`Stealer` 是 **Chase-Lev 双端队列**（本讲主角）；文件后半的 `Injector` 是另一套**无界分块链表 FIFO 队列**（与 Chase-Lev 不同源，本讲只作提示，不展开） |
| [crossbeam-epoch/src/guard.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs) | `Guard::defer_unchecked` / `epoch::pin` / `epoch::unprotected`，`resize` 延迟释放旧缓冲的基石 |

> 提示：`deque.rs` 同时包含 `Injector` 的实现（用 `Block` 链表、`LAP=64`、`BLOCK_CAP=63`，思路接近 crossbeam-queue 的 SegQueue）。它与 Chase-Lev 是两套独立算法，**别混淆**：本讲的 `front/back/buffer` 三件套只描述 `Worker`/`Stealer`。

---

## 4. 核心概念与源码讲解

### 4.1 Inner 与 Buffer：环形缓冲与索引

#### 4.1.1 概念说明

Chase-Lev 把队列状态拆成两层：

- **共享层 `Inner<T>`**：被 `Worker` 和所有 `Stealer` 通过 `Arc` 共享，包含两个原子索引 `front`/`back` 和一个原子缓冲指针 `buffer`。
- **缓冲 `Buffer<T>`**：一个裸指针 `ptr` 加容量 `cap`，本身不拥有内存（`Drop` 不释放），只是「指向某段幂次大小内存」的视图。`Clone`/`Copy` 都是浅拷贝。

为什么 `Buffer` 只是视图、不拥有内存？因为同一段缓冲内存可能同时被多个线程「读到指针」（steal 时 `load` buffer 指针），它的生命周期由 epoch 统一管理，不能让某个 `Buffer` 值的 drop 就把内存释放掉——那会立刻 use-after-free。

`Worker` 还在**线程局部**维护一份 `buffer: Cell<Buffer<T>>` 副本（见 [deque.rs:L197-L209](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L197-L209)）。因为 `Worker` 是单所有者（`!Sync`，见 [deque.rs:L211](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L211)），所有者的 push/pop 完全无争用，每次操作都走一次 `Atomic::load` 太浪费，于是用 `Cell`（零开销的内部可变性）缓存当前缓冲指针，只在 `resize` 后同步更新。

#### 4.1.2 核心流程

幂次环形映射是全队的算术基础。设缓冲容量为 `cap = 2^k`，则：

\[
\text{槽位指针}(\text{index}) = \text{ptr} + \bigl(\text{index} \mathrel{\&} (2^k - 1)\bigr)
\]

由于 \(2^k - 1\) 是低 k 位全 1 的掩码，`index & (cap-1)` 等价于 `index % cap`，但只需一条按位与指令。这样单调的 `front`/`back` 无需取模就能落到环形槽上。

队列判空与判满：

\[
\text{len} = back.\text{wrapping\_sub}(front), \quad
\text{空} \iff \text{len} \le 0, \quad
\text{满} \iff \text{len} \ge cap
\]

只要始终保证 `len < cap`（满了就 `resize` 翻倍），同一缓冲里 `front` 与 `back` 映射到的槽位绝不会重叠，避免了「读到下一圈被覆盖的旧值」。

#### 4.1.3 源码精读

`Inner` 三字段，`front`/`back` 是 `AtomicIsize`，`buffer` 用 `CachePadded<Atomic<Buffer<T>>>` 包装（防伪共享 + 原子指针，见 u2-l2 与 u5-l2）：

> [crossbeam-deque/src/deque.rs:L114-L123](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L114-L123) — `Inner` 的三字段定义。

`Buffer` 是裸指针 + 容量，注释明确说「drop 不会释放内存」：

> [crossbeam-deque/src/deque.rs:L29-L35](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L29-L35) — `Buffer` 视图结构。

幂次位与映射的核心一行（注意 `cap` 恒为 2 的幂，构造时 `debug_assert` 保证）：

> [crossbeam-deque/src/deque.rs:L65-L70](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L65-L70) — `at(index)` 用 `index & (cap - 1)` 落到环形槽。

读写用 `write_volatile` / `read_volatile`（[deque.rs:L78-L90](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L78-L90)）。源码注释坦诚说明：所有者的 `write` 与窃取者的 `read` 在同一索引上并发，**严格说是数据竞争（UB）**，理想做法是原子读写，但泛型 `T` 难以统一原子化，于是用 volatile 当作「尽力而为」的折中——这把真正的正确性交给内存序/fence 与 epoch 来兜底。

关键常量（`MIN_CAP=64`、`MAX_BATCH=32`、`FLUSH_THRESHOLD_BYTES=1<<10`）：

> [crossbeam-deque/src/deque.rs:L17-L23](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L17-L23) — 最小容量、批量窃取上限、触发 flush 的阈值。

`Worker` 与 `Stealer` 共享同一份 `Arc<CachePadded<Inner<T>>>`，唯一区别是 `Worker` 多了局部 buffer 副本与 `Flavor`：

> [crossbeam-deque/src/deque.rs:L197-L209](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L197-L209) — `Worker` 结构。
>
> [crossbeam-deque/src/deque.rs:L574-L583](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L574-L583) — `Stealer` 结构与其 `Send + Sync` 实现（`Stealer` 可跨线程共享，`Worker` 不行）。

`Flavor` 枚举只有 `Fifo`/`Lifo` 两个变体，是 4.3 节方向差异的总开关：

> [crossbeam-deque/src/deque.rs:L147-L155](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L147-L155) — `Flavor` 枚举。

#### 4.1.4 代码实践

1. **目标**：在纸面上验证幂次映射，并理解 `Worker` 的单所有者约束。
2. **步骤**：
   - 读 `new_fifo`（[deque.rs:L225-L240](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L225-L240)），确认初始 `front=back=0`、`buffer` 容量 `MIN_CAP=64`。
   - 手算：当 `back = 70`、`front = 5` 时，`70 & 63 = 6`、`5 & 63 = 5`，两者不冲突（`len=65 < 64`? 不，65≥64 会先 resize——这正是满检查的意义）。
   - 编写示例代码，尝试把 `Worker` 通过 `Arc` 共享给两个线程，观察编译器报错（`Worker: !Sync`）。

```rust
// 示例代码：验证 Worker 不能跨线程共享（!Sync），而 Stealer 可以
use crossbeam_deque::Worker;
use std::sync::Arc;

let w = Worker::<i32>::new_fifo();
let s = w.stealer();
let _s2 = Arc::new(s);        // OK: Stealer: Send + Sync
// let _w2 = Arc::new(w);     // 编译失败: Worker 不是 Sync
```

3. **需要观察的现象**：取消注释 `Arc::new(w)` 后编译器报 `Worker<{integer}> cannot be shared between threads safely`。
4. **预期结果**：`Stealer` 可被 `Arc` 共享，`Worker` 不可——这正是 `pop` 能用无锁 `fetch_add` 而不必 CAS 的前提（见 4.3）。
5. 「待本地验证」：具体报错文案以本地 rustc 输出为准。

#### 4.1.5 小练习与答案

- **练习**：为什么 `Buffer` 的 `Drop` 不释放 `ptr` 指向的内存？谁负责释放？
- **答案**：`Buffer` 只是指针视图，同一段内存可能被多个线程的 `steal` 同时「读到」。真正的释放由 epoch 在宽限期后统一执行（`resize` 与 `Inner::drop` 里的 `dealloc`），若 `Buffer::drop` 直接释放，任何持有旧指针的窃取者都会 use-after-free。

---

### 4.2 push：写 back 端与发布顺序

#### 4.2.1 概念说明

`push` 是所有者的**独占快路径**：所有者写 `back` 端，没有别的线程会写 `back`，所以它不需要 CAS，只需普通的 load/store。唯一需要小心的是**发布顺序**：必须保证「任务先写入槽位，对其他线程可见」**先于**「`back` 自增对其他线程可见」。否则窃取者看到 `back` 前进了，去读那个槽，却读到未初始化的旧值。

#### 4.2.2 核心流程

```
push(task):
  b = back.load(Relaxed)
  f = front.load(Acquire)        # 用 Acquire 是为了和 resize/steal 配合
  buffer = 本地副本
  if (b - f) >= cap:             # 满了
      resize(2 * cap); buffer = 本地副本（已更新）
  buffer.write(b, task)          # 写槽位（volatile）
  fence(Release)                 # ★ 关键：让上面的写先于下面的 store 可见
  back.store(b + 1, Relaxed)     # 发布新元素
```

**内存序要点**：写槽位后插一条 `Release` fence，再用 `Relaxed` store 自增 `back`。`Release` fence 的语义是「fence 之前的所有写，对任何后来通过 acquire 读到这次 store 的线程都可见」。于是窃取者只要用 `Acquire` 读到新的 `back`，就一定也能读到槽位里的任务——发布顺序成立。

> ThreadSanitizer 不理解 fence，因此 `crossbeam_sanitize_thread` 配置下改用 `Release` store 代替 `Relaxed` store（见源码注释 [deque.rs:L422-L429](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L422-L429)），这是为了在 sanitizer 下也能检出竞争。

#### 4.2.3 源码精读

满检查触发翻倍扩容：

> [crossbeam-deque/src/deque.rs:L405-L415](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L405-L415) — `len >= cap` 时 `resize(2 * cap)`，并刷新本地 buffer 副本。

写槽位：

> [crossbeam-deque/src/deque.rs:L417-L420](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L417-L420) — `buffer.write(b, MaybeUninit::new(task))`。

发布顺序的核心 fence + store：

> [crossbeam-deque/src/deque.rs:L422-L432](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L422-L432) — `fence(Release)` 后再 `back.store(b+1, ...)`。

#### 4.2.4 代码实践

1. **目标**：体会「发布顺序」为何关键。
2. **步骤**：阅读 [deque.rs:L399-L433](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L399-L433)，在脑中假设把 `fence(Release)` 删掉、把 `store` 改成 `Relaxed`，思考在弱内存模型（ARM）上会发生什么。
3. **需要观察的现象（推理）**：弱内存 CPU 可能重排「写槽位」与「写 back」，窃取者先看到 `back` 前进、再去读槽，读到尚未写入的垃圾值。
4. **预期结果（推理）**：删掉 fence 会让发布顺序失去保证，偶发性读到错误任务。本条为源码阅读型推理，不实际运行删改后的代码（修改源码违反讲义约束）。
5. 「待本地验证」：若想实证，可在拷贝出的独立实验 crate 里复刻简化版逻辑，配合 `loom` 跑模型检查。

#### 4.2.5 小练习与答案

- **练习 1**：为什么 `push` 读 `front` 用 `Acquire` 而读 `back` 用 `Relaxed`？
- **答案**：`back` 只有所有者自己写，无并发写入，`Relaxed` 足够；`front` 会被窃取者（以及 FIFO 的 pop）改写，`Acquire` 用于和它们的 release 操作建立同步（尤其在判满时需要看到对手推进的进度）。
- **练习 2**：`push` 为何不需要 CAS？
- **答案**：所有者是 `back` 的唯一写者，没有竞争，普通的 load/store 即可；CAS 只在多写者争用同一字段（如 `front`）时才必要。

---

### 4.3 pop：FIFO 与 LIFO 的方向差异

#### 4.3.1 概念说明

`pop` 是所有者从自己队列取一个任务。Chase-Lev 的精髓在于：**pop 的方向由 `Flavor` 决定**，这是 FIFO 与 LIFO 唯一的本质区别。

- **LIFO**：所有者从 **`back` 端**取（与 push 同端）。先 `back - 1`「认领」该槽，再读。这是经典 Chase-Lev 的栈式行为。
- **FIFO**：所有者从 **`front` 端**取（与 push 反端，与 steal 同端）。`fetch_add(1)` 推进 `front`。

两种风味都会在长度跌破容量的 1/4 时缩容（`resize(cap/2)`），且都不会低于 `MIN_CAP`。

#### 4.3.2 核心流程

**FIFO pop**（所有者也走 `front`，等价于一次「自偷」）：

```
FIFO pop:
  b = back; f = front
  if (b - f) <= 0: return None          # 空
  f = front.fetch_add(1, SeqCst)        # 无条件推进 front（所有者不与自己竞争）
  if (b - (f+1)) < 0:                   # 推过头了（被窃取者抢走最后一个）
      front.store(f); return None       # 回滚
  task = buffer.read(f)
  可能 resize(cap/2)
  return Some(task)
```

**LIFO pop**（从 `back` 取，需与 steal 在「最后一个元素」上协调）：

```
LIFO pop:
  b = back; f = front
  if (b - f) <= 0: return None
  b = b - 1
  back.store(b, Relaxed)                # 先「认领」该槽
  fence(SeqCst)                         # ★ 让认领对所有线程可见，再读 front
  f = front.load(Relaxed)
  if (b - f) < 0:                        # 空（steal 已经把它拿走）
      back.store(b+1); return None       # 回滚
  task = buffer.read(b)
  if (b - f) == 0:                       # 这是最后一个元素，与 steal 竞争
      if front.cas(f, f+1).is_err():     # 输给 steal
          task = None
      back.store(b+1)                    # 回滚 back
  else 可能 resize(cap/2)
  return task
```

**内存序要点**：LIFO pop 的 `store(back-1, Relaxed)` → `fence(SeqCst)` → `load(front, Relaxed)` 是 push 的镜像。先写 `back` 认领槽位、再 fence、再读 `front`，确保任何窃取者读到「回退后的 back」之前，pop 已经能读到「对手推进后的 front」。这条 SeqCst fence 正是 Le/Pop/Cohen（PPoPP 2013）为弱内存模型补上的关键修正。

> **FIFO pop 为何能用无条件 `fetch_add`，而 steal 必须用 CAS？**
> 因为 `Worker` 是 `!Sync` 的单所有者，所有者不会与自己争 `front`；`fetch_add` 必然成功，事后用 `b - new_f < 0` 检查是否「推过头」（被 steal 抢了）即可回滚。而多个 `Stealer` 会同时争抢 `front`，必须用 CAS 保证只有一个赢家。这是 `Worker: !Sync` 这一类型约束换来的性能红利。

#### 4.3.3 源码精读

`pop` 的入口与判空，按 `flavor` 分流：

> [crossbeam-deque/src/deque.rs:L450-L464](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L450-L464) — 算长度、判空、`match self.flavor` 分流。

FIFO 分支用 `fetch_add` 推进 `front`，过头则回滚：

> [crossbeam-deque/src/deque.rs:L465-L487](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L465-L487) — `front.fetch_add(1, SeqCst)`，检查 `b - new_f < 0` 回滚，读任务，可能缩容。

LIFO 分支「减 back → fence → 读 front」，并在最后一个元素上与 steal 做 CAS 决胜：

> [crossbeam-deque/src/deque.rs:L489-L543](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L489-L543) — 注意 `back.store(b, Relaxed)`（[L493](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L493)）→ `fence(SeqCst)`（[L495](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L495)）→ `front.load`（[L498](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L498)）；`len == 0` 时对 `front` 的 CAS 决胜（[L515-L528](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L515-L528)）。

#### 4.3.4 代码实践

1. **目标**：用单元测试风格的断言（来自源码 doc 示例）验证 FIFO 与 LIFO 的出序差异。
2. **步骤**：阅读 `Worker::pop` 顶部 doc 示例（[deque.rs:L435-L449](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L435-L449)）与 `Worker` 结构上方对 FIFO/LIFO 的对比示例（[deque.rs:L166-L196](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L166-L196)）。
3. **需要观察的现象**：对同一序列 push 1,2,3：FIFO 的 `pop` 返回 1,2,3（与 push 反序进入，正序取出）；LIFO 的 `pop` 返回 3,2,1（栈式）。两种风味下 `steal` 永远从 `front` 取，因此 `steal` 第一个都拿到 1。
4. **预期结果**：运行 `cargo test -p crossbeam-deque --doc` 应全部通过。
5. 「待本地验证」：实际命令输出以本地为准。

#### 4.3.5 小练习与答案

- **练习 1**：LIFO pop 在 `len == 0`（即取最后一个元素）时，为什么必须对 `front` 做一次 CAS，而不是直接返回任务？
- **答案**：当队列只剩一个元素时，pop（从 back 端）和某个 steal（从 front 端）瞄准的是**同一个**元素。CAS 让两者中只有一个赢家：CAS 成功者拿到任务，失败者放弃。若不用 CAS 而直接返回，pop 和 steal 可能重复消费同一元素。
- **练习 2**：FIFO pop 与 `steal` 在「操作的字段」上是否相同？为何一个用 `fetch_add` 一个用 CAS？
- **答案**：都操作 `front`。差别在于写者数量：FIFO pop 是单所有者，无自竞争，`fetch_add` 即可；`steal` 是多窃取者争用，必须 CAS。

---

### 4.4 steal：CAS front 与 Retry/Empty 语义

#### 4.4.1 概念说明（本讲核心）

`steal` 是多窃取者的**争用慢路径**，始终从 `front` 端取。它返回 `Steal<T>` 三态之一（见 u6-l1）：

- `Success(T)`：成功偷到一个任务。
- `Empty`：偷的那一刻队列**真的没有**任务（`back - front <= 0`）。
- `Retry`：偷的那一刻队列**本有**任务，本线程也读到了它，但**没抢到**——需要重试。

本讲的核心问题就是：**`Retry` 为什么必须重试，而不能当 `Empty` 处理？**

#### 4.4.2 核心流程

```
steal:
  f = front.load(Acquire)
  if is_pinned(): fence(SeqCst)        # 若已 pin，需手动补 fence；否则下面的 pin 会补
  guard = &epoch::pin()                 # ★ pin 在 load buffer 之前
  b = back.load(Acquire)
  if (b - f) <= 0: return Empty         # 真空
  buffer = inner.buffer.load(Acquire, guard)
  task = buffer.read(f)                 # 先乐观地读出来
  if inner.buffer.load(...) != buffer   # 缓冲被 resize 换掉了
     || front.cas(f, f+1, SeqCst).is_err():  # 或被别的窃取者/FIFO-pop 抢了
      return Retry                      # ★ 没抢到，重试
  return Success(task)
```

**为什么 `Retry` 不能当 `Empty`？**

返回 `Retry` 的两个条件，发生时**队列都非空**（`b - f > 0` 已成立）：

1. **缓冲被换**：在「读 buffer 指针」与「CAS front」之间，所有者做了 `resize`，把 `inner.buffer` 换成了新缓冲。此时任务**仍然存在**（resize 只是把元素搬到更大的缓冲），只是我们手里这个旧指针不可信了，必须重读。
2. **CAS 失败**：另一个窃取者（或 FIFO 的所有者 pop）抢先推进了 `front`，抢走了这一格。但队列里**可能还有后续任务**，重试有机会拿到。

如果把 `Retry` 当 `Empty`，调用者会以为队列没活可干而停止寻找，**白白漏掉真实存在的任务**——这会丢工作。所以 `Steal` 类型标记了 `#[must_use]`，并提供 `or_else`/`FromIterator` 让 `Retry` **传染**：只要链路里有任何一次 `Retry`，聚合结果就是 `Retry`，提示上层（如 `find_task` 重试循环）继续轮询。

**为什么 steal 要先 `pin` 再 `load` buffer？**
这正是与 epoch 安全的衔接点：`pin` 之后读到的 buffer 指针，在整个 pin 期间（guard 存活）都保证有效——即便所有者在此期间 `resize` 并换走缓冲，旧缓冲也被 `defer_unchecked` 延迟到所有 pin 者离开后才释放。于是 `buffer.read(f)` 绝不会读到已释放内存。详见 4.5。

#### 4.4.3 源码精读

读 `front` 后的 fence 决策（已 pin 则手动补，否则交给 `pin`）：

> [crossbeam-deque/src/deque.rs:L641-L654](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L641-L654) — `steal` 开头：load front → `is_pinned` 判断 → `epoch::pin()`。

判空、读缓冲与读任务：

> [crossbeam-deque/src/deque.rs:L656-L666](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L656-L666) — `b - f <= 0` 返回 `Empty`；否则 `buffer.load(Acquire, guard)` 后 `buffer.read(f)`。

`Retry` 与 `Success` 的分水岭——「缓冲未变 且 CAS 成功」才成功：

> [crossbeam-deque/src/deque.rs:L668-L683](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L668-L683) — `buffer.load(...) != buffer || front.compare_exchange(...).is_err()` 返回 `Retry`；否则 `Steal::Success`。

`Steal::or_else` 中 `Retry` 的传染语义（任一边是 Retry 则保留 Retry）：

> [crossbeam-deque/src/deque.rs:L2185-L2200](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L2185-L2200) — `or_else` 实现。

#### 4.4.4 代码实践（对应本讲指定实践任务）

1. **目标**：亲眼看到多个窃取者竞争同一 `Worker` 时 `Retry`/`Success`/`Empty` 的分布，理解「Retry 不是空」。
2. **操作步骤**：新建一个独立实验 crate（`cargo new --bin steal_demo`，`cargo add crossbeam-deque crossbeam-utils`），写入下面的示例代码并运行。
3. **需要观察的现象**：在并发窃取下，`Retry` 会出现非零计数（CAS 竞争失败），`Success` 总数恰好等于被偷走的任务数，`Empty` 出现在队列被抽干后。若把重试循环去掉、见到 `Retry` 就停，会**漏偷**任务。

```rust
// 示例代码：观察多窃取者竞争下的 Retry/Success/Empty 分布
use crossbeam_deque::{Steal, Worker};
use crossbeam_utils::thread::scope;

const N: usize = 100_000;   // 任务总数
const K: usize = 4;         // 窃取者数量

let w = Worker::new_lifo();
for i in 0..N { w.push(i); }
let s = w.stealer();

scope(|scope| {
    let mut handles = Vec::new();
    // 每个窃取者用一个局部 Worker 接收偷来的任务，并统计结果
    for _ in 0..K {
        let s = s.clone();
        handles.push(scope.spawn(move |_| {
            let local = Worker::<usize>::new_fifo();
            let (mut retry, mut succ, mut empty) = (0u64, 0u64, 0u64);
            loop {
                // 关键：见 Retry 必须重试，否则会漏任务
                match s.steal() {
                    Steal::Success(t) => { succ += 1; std::mem::forget(t); }
                    Steal::Retry      => { retry += 1; continue; }   // 重试！
                    Steal::Empty      => { empty += 1; break; }
                }
            }
            let _ = local;
            (retry, succ, empty)
        }));
    }
    // 所有者同时 pop 剩余任务，制造对 front 的额外竞争
    while w.pop().is_some() {}
    for h in handles { let (r, s, e) = h.join().unwrap(); println!("retry={r} success={s} empty={e}"); }
}).unwrap();
```

4. **预期结果**：四个窃取者的 `success` 之和 + 所有者 pop 的数量 ≈ `N`（不丢不重）；`retry > 0` 几乎必然出现；`empty` 各 1 次（抽干时）。把 `continue` 换成 `break`（即把 Retry 当 Empty）会导致 `success` 总和明显小于 `N`——这就是「漏任务」。
5. 「待本地验证」：具体计数受调度影响，但 `retry` 非零、`success` 之和守恒这两个性质应稳定成立。

#### 4.4.5 小练习与答案

- **练习 1**：把 `Retry` 当作 `Empty` 处理会导致什么后果？
- **答案**：丢任务。`Retry` 出现时队列非空，只是本线程没抢到；若停止重试，真实存在的任务就被漏掉，最终被处理的任务总数少于 push 的总数。
- **练习 2**：`steal` 里 `epoch::pin()` 为什么必须放在 `buffer.load` **之前**？
- **答案**：pin 保证了「pin 期间读到的 buffer 指针所指向的内存不会被释放」。若先 load buffer 再 pin，存在一个窗口：所有者 resize 换走缓冲并在我们 pin 之前就释放了旧缓冲，我们随后 `read` 旧指针便是 use-after-free。pin 在前，旧缓冲的释放被 epoch 延迟到我们 unpin 之后。

---

### 4.5 resize：扩缩容与 epoch 延迟回收

#### 4.5.1 概念说明

`Worker` 的缓冲容量可变：满了翻倍（`push` 触发），闲到 1/4 以下折半（`pop` 触发，不低于 `MIN_CAP`）。`resize` 做三件事：分配新缓冲、把 `[front, back)` 的元素线性拷过去、把 `inner.buffer` 原子换成新指针。

难点在**旧缓冲何时释放**。换指针的瞬间，可能有窃取者正持有旧指针（在 4.4 的 `steal` 里，pin 着、读旧缓冲）。立刻 `free` 会让这些窃取者 use-after-free。解法是把旧缓冲的释放交给 epoch：`guard.defer_unchecked(move || 旧缓冲.dealloc())`，让它在宽限期（所有当时 pin 的线程都 unpin）之后才真正释放。

#### 4.5.2 核心流程

```
resize(new_cap):
  b = back; f = front; old = 本地副本
  new = Buffer::alloc(new_cap)            # 新缓冲
  把 [f, b) 的元素从 old 拷到 new         # 线性 copy_nonoverlapping
  guard = &epoch::pin()
  本地副本 = new
  old_shared = inner.buffer.swap(new, Release, guard)   # 原子换指针，拿回旧指针
  guard.defer_unchecked(move || old_shared.dealloc())   # ★ 延迟释放旧缓冲
  if 大缓冲: guard.flush()                # 主动 flush，加快回收
```

**为什么 `defer_unchecked` 是 unsafe？** 见 [u5-l3](u5-l3-epoch-guard.md)：它要求「闭包运行时该对象已无其他线程引用」这一运行期不变量，类型系统证不出。这里它之所以安全，是因为：换指针用 `Release`，所有后来读到新指针的窃取者（`Acquire`）都不会再访问旧缓冲；而换指针**之前**已经 pin 并持有旧指针的窃取者，受 epoch 宽限期保护——他们 unpin 后旧缓冲才被释放。

**`flush` 的作用**：被 defer 的闭包先进线程局部垃圾袋（攒满 64 个才入全局队列）。若刚释放的是大缓冲（`size_of::<T>() * new_cap >= 1<<10` 即 1 KiB），就调用 `guard.flush()` 把局部袋立刻推进全局队列，缩短大块内存的滞留时间。

#### 4.5.3 源码精读

`resize` 全貌：拷贝 → pin → 换指针 → 延迟释放 → 可能 flush：

> [crossbeam-deque/src/deque.rs:L289-L322](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L289-L322) — 重点关注 `epoch::pin()`（[L305](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L305)）、`inner.buffer.swap(... Release ...)`（[L309-L312](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L309-L312)）、`guard.defer_unchecked(move || old...dealloc())`（[L315](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L315)）、大缓冲 `guard.flush()`（[L319-L321](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L319-L321)）。

扩容由 `push` 的满检查触发（4.2 已引用），缩容由 `pop` 触发（4.3 已引用）。`reserve` 则在批量窃取前预扩容目标队列，避免窃取途中触发 resize：

> [crossbeam-deque/src/deque.rs:L324-L350](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L324-L350) — `reserve` 按需翻倍直到够用。

`Inner::drop`（单线程析构，无并发）用 `unprotected()` 假守卫直接释放最后的缓冲：

> [crossbeam-deque/src/deque.rs:L125-L145](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L125-L145) — 析构时无其他线程，用 `epoch::unprotected()` 跳过 pin，直接 drop 任务并 `dealloc`。

epoch 侧的 `defer_unchecked` 与 `unprotected` 定义：

> [crossbeam-epoch/src/guard.rs:L189-L200](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L189-L200) — `Guard::defer_unchecked`：有 local 则入袋延迟，无 local（`unprotected`）则立即执行。
>
> [crossbeam-epoch/src/guard.rs:L517-L528](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L517-L528) — `unprotected()` 返回 `local` 为 null 的假守卫。

#### 4.5.4 代码实践

1. **目标**：触发 resize，确认扩容不丢元素，并定位延迟释放点。
2. **操作步骤**：在 4.4 的示例代码基础上，把 `MIN_CAP` 之上的扩容路径走一遍——对容量 64 的初始缓冲 push 70 个元素，必然触发一次 `resize(128)`。阅读 [deque.rs:L289-L322](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L289-L322)，在脑中标注：哪一行分配新缓冲、哪一行原子换指针、哪一行 defer 旧缓冲释放。
3. **需要观察的现象（源码阅读型）**：resize 期间若有窃取者并发 steal，它 pin 在先、持旧指针在后，旧缓冲的 dealloc 被 defer，等它 unpin 后才执行——不会崩溃。
4. **预期结果**：push 70 个、再全部 steal+pop 出来，元素无丢失、无重复。
5. 「待本地验证」：可用 `cargo +nightly miri test`（单线程）或 `loom` 跑模型检查确认无 UB。

#### 4.5.5 小练习与答案

- **练习 1**：为什么 `resize` 不能在 `swap` 换指针后立刻 `dealloc` 旧缓冲？
- **答案**：可能有窃取者在换指针**之前**已经 `pin` 并 load 到旧 buffer 指针，此刻正要 `read`。立刻 free 会让它读到已释放内存。必须等这些 pin 者离开（宽限期）再释放，这正是 `defer_unchecked` 的作用。
- **练习 2**：`Inner::drop` 里为什么用 `unprotected()` 而不是 `pin()`？
- **答案**：`drop` 发生在 `Arc` 引用计数归零、所有 `Worker`/`Stealer` 都已销毁之后，绝无并发访问，pin 只是徒增开销并拖延 GC。`unprotected()` 是「无并发时跳过 pin」的优化（见 guard.rs 文档注释 [L469-L477](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L469-L477)）。

---

## 5. 综合实践

把本讲四条主线串起来，做一个「会扩容、会被多线程偷、还要安全回收缓冲」的端到端验证。

**任务**：用 1 个 `Worker` + 4 个 `Stealer`，所有者持续 push 直到触发至少一次扩容（push > 64 个），同时 4 个窃取者并发偷；要求：

1. 用 `crossbeam_utils::thread::scope`（见 [u2-l7](u2-l7-scoped-threads-internals.md)）组织线程，避免 `'static` 约束。
2. 窃取循环必须正确处理 `Steal` 三态（参考 4.4.4 的示例代码）：`Success` 计数、`Retry` 重试、`Empty` 退出。
3. 所有线程结束后，验证「所有者剩余 pop 数 + 各窃取者 success 数 == 总 push 数」（守恒，不丢不重）。
4. 阅读源码回答：在这次运行中，扩容发生时旧缓冲的释放被哪一行 `defer_unchecked` 调度（[deque.rs:L315](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L315)）；并解释为何即便窃取者持有旧指针也不会崩溃（pin 在 load buffer 之前，宽限期保护）。

**预期结果**：守恒等式成立；若启用 `cargo +nightly miri test` 或 `crossbeam_sanitize_thread`（见 [ci/tsan](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/ci) 与 u7-l3）应无 UB/数据竞争告警。「待本地验证」：具体计数受线程调度影响，但守恒与无告警两条应稳定成立。

---

## 6. 本讲小结

- Chase-Lev 用 `Inner{front, back, buffer}` 描述队列：`front` 是窃取端、`back` 是 push 端，两个 64 位单调索引本身充当代次，**无需 lap/stamp**（区别于 ArrayQueue）。幂次缓冲用 `index & (cap-1)` 映射到环形槽。
- `push` 是所有者独占快路径：写槽位 → `fence(Release)` → `back.store(+1)`，靠 fence 保证「任务可见」先于「索引前进」。
- `pop` 的方向由 `Flavor` 决定：**FIFO 增 `front`**（`fetch_add`，因 `Worker: !Sync` 无自竞争）、**LIFO 减 `back`**（`store(back-1)` → `fence(SeqCst)` → 读 `front`，取最后一个元素时与 steal 用 CAS 决胜）。
- `steal` 始终从 `front` 取，用 CAS（多窃取者争用）。返回 `Retry` 时队列**非空**只是没抢到，**必须重试**，否则丢任务——这是 `Steal` 设计与 `#[must_use]`/`or_else` 传染语义的根因。
- `resize` 把 `inner.buffer` 原子换成新缓冲后，旧缓冲交给 `guard.defer_unchecked` 在 epoch 宽限期后释放；`steal` 先 `pin` 再 `load` buffer，从而持旧指针也不会 use-after-free——这是 Chase-Lev 与 epoch 回收（u5 单元）的衔接点。

---

## 7. 下一步学习建议

- **横向对比无锁队列**：回头读 [u4-l1 ArrayQueue](u4-l1-array-queue.md) 与 [u4-l2 SegQueue](u4-l2-seg-queue.md)，对比三种并发队列的 ABA 防护策略（Chase-Lev 用单调索引、ArrayQueue 用 lap/stamp、SegQueue 用链表+状态位），体会同一问题（槽位复用）的不同解法。
- **补全 epoch 内部**：若想彻底搞懂「旧缓冲到底何时、由谁释放」，继续读 [u5-l5 internal：全局状态、epoch 推进与垃圾回收](u5-l5-epoch-internals.md)，理解 `try_advance` 与「差 ≥ 2 才销毁」的判据。
- **正确性验证**：进入 [u7-l3 测试、loom 与并发正确性](u7-l3-testing-concurrency-correctness.md)，学习如何用 `miri`/`tsan`/`loom` 给本讲的内存序与 fence 把关——Chase-Lev 在弱内存模型上的正确性正是靠这些工具验证的。
