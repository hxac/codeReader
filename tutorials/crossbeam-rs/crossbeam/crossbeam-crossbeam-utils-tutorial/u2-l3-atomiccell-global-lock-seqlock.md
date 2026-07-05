# AtomicCell 的全局锁回退与 SeqLock

## 1. 本讲目标

本讲承接 u2-l2。在上一讲里我们知道了 `AtomicCell<T>` 的操作在编译期由 `atomic!` 宏二选一：能 `transmute` 成原生原子类型时走「单条 CPU 原子指令」的无锁路径；否则走「全局锁」回退路径。本讲就专门拆解这条**回退路径**。

读完本讲，你应当能够：

1. 说清 `lock()` 全局锁池的结构，并解释「为什么是 67 个锁」「为什么用素数」「为什么每个锁要包一层 `CachePadded`」。
2. 画出 SeqLock 的印戳（stamp）状态机：写者如何加锁/解锁并推进版本号、读者如何在不持锁的情况下读到一致快照。
3. 解释 `atomic_load` 回退路径的「乐观读 + `ptr::read_volatile` + 校验重试」三步，以及失败后改持写锁并用 `abort()` 不污染版本号的原因。
4. 能独立论证：在多读者单写者并发下，乐观读者**为什么永远不会读到撕裂（torn）值**。

---

## 2. 前置知识

在进入源码前，先用直觉建立三个概念。

### 2.1 为什么需要「回退路径」

`AtomicCell<T>` 想模仿 `Cell<T>`，但允许跨线程。最理想的实现是「把 `T` 当成一个原生原子类型来读写」，但这要求 `T` 的大小/对齐恰好落在 `AtomicU8/16/32/64/128` 之一。当 `T` 是 `[u8; 40]`、`[u64; 3]` 这种「既不是零大小、又不匹配任何原生原子宽度」的类型时，硬件没有单条指令能整体原子读写它。此时 crossbeam 不会拒绝编译，而是**透明地**退化为用一把锁保护这个 cell——这就是回退路径。

> 在 u2-l2 我们知道 `is_lock_free()` 的返回值与实际走的路径同源。所以 `AtomicCell::<[u8;1000]>::is_lock_free()` 返回 `false` 时，它的所有读写都走本讲解的回退路径。

### 2.2 什么是 SeqLock（sequence lock）

SeqLock 是经典的「读者优先、写者互斥」读写模式，核心想法是：

- **写者**持有一把真正的互斥锁，写之前把一个**版本号**（stamp）标记为「正在写」，写完后把版本号 +1。
- **读者完全不加锁**，而是「乐观地」读：先记下版本号 → 读数据 → 再读一次版本号；如果前后版本号相等且都表示「没人在写」，就说明读数据期间没有写者插手，读到的就是一份完整一致的快照；否则重试。

它的好处是**读者无锁、不互相阻塞**，适合「读多写少」或「读者怕被阻塞」的场景。代价是读者可能因写者干扰而重试。

### 2.3 为什么需要「锁池」而不是「一把全局锁」

如果所有回退 cell 共用一把锁，那么任何两个不相关的 cell 的写操作都会互相串行化，成为巨大的性能瓶颈。crossbeam 的做法是放**一堆**锁，按 cell 的内存地址选其中一把。这样地址不同的两个 cell（大概率）落在不同的锁上，互不打扰；只有恰好落在同一把锁上的少量 cell 才会串行。

### 2.4 你需要带的「上一讲记忆」

- `atomic!` 宏在无锁候选全部失败时执行第四段 `$fallback_op`——本讲讲的就是这段 fallback 里调用的 `lock()` 和 SeqLock。
- `can_transmute`、`AtomicUnit`、`atomic_is_lock_free` 这些是**无锁侧**的概念，本讲不重复，只承接「它们都失败了」这一前提。

---

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `src/atomic/seq_lock.rs` | SeqLock 本体与 `SeqLockWriteGuard` | 印戳编码、`optimistic_read`/`validate_read`/`write`/`abort`/`Drop` |
| `src/atomic/atomic_cell.rs` | `AtomicCell` 实现，含回退路径 | `lock()` 锁池选择、`atomic_load`/`atomic_store`/`atomic_compare_exchange_weak` 的 fallback 分支 |
| `src/atomic/mod.rs` | 模块门控 | `seq_lock` 模块如何按指针宽度选择普通版/宽版 |

补充：`src/cache_padded.rs`（u2-l6 详讲）在本讲只作为「锁池元素为什么包 `CachePadded`」的引用出现；`src/backoff.rs`（u2-l5）只作为 `write()` 自旋退避的引用出现。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，正好对应三个层层递进的问题：

- **模块 4.1**：回退时怎么从全局锁池里**选**一把锁（`lock()`）。
- **模块 4.2**：选到的锁是一个 SeqLock，它的**印戳机制**与写者加锁/解锁如何运作。
- **模块 4.3**：读者在 SeqLock 上如何**无锁乐观读**，以及 `atomic_load` 把这些拼起来。

### 4.1 lock() 锁池选择

#### 4.1.1 概念说明

回退路径需要一个互斥量来保护 cell。但全局只用一把锁太慢，于是用一个**锁数组**，按 cell 数据的内存地址哈希选一把：

\[ f(\text{addr}) = \text{addr} \bmod \text{LEN} \]

设计上有两个关键决策：

1. **`LEN` 取素数 67**，而不是 2 的幂。
2. **数组元素类型是 `CachePadded<SeqLock>`**，而不是裸 `SeqLock`。

这两个决策分别解决「分布均匀」和「避免 false sharing」两个问题，下面逐一说明。

#### 4.1.2 核心流程

`lock(addr)` 的执行过程：

1. 把传入的 `addr`（cell 数据指针的整数值）对 `LEN=67` 取模。
2. 用下标从静态数组 `LOCKS` 里取出对应的 `SeqLock` 引用。
3. 返回这个 `&'static SeqLock`，调用方（写者）在其上调用 `.write()`，读者在其上调用 `.optimistic_read()`/`.validate_read()`。

整个函数 `#[inline]` 且无锁，开销几乎只剩一次取模。

#### 4.1.3 源码精读

锁池选择函数 `lock()`：

[src/atomic/atomic_cell.rs:955-995](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L955-L995) —— 接收 cell 数据地址，返回与之关联的那把全局 `SeqLock` 引用。

它的核心只有三行（其余是注释）：

```rust
const LEN: usize = 67;
const L: CachePadded<SeqLock> = CachePadded::new(SeqLock::new());
static LOCKS: [CachePadded<SeqLock>; LEN] = [L; LEN];
// ...
&LOCKS[addr % LEN]
```

**为什么是素数 67？** 注释给了两段理由，值得逐字理解：

- 地址总是按 2 的幂对齐。若 `LEN` 是偶数，那么 `addr % LEN` 也总是偶数（因为偶数减去偶数仍为偶数），结果**只有一半的锁会被用到**。
- 更狡猾的情况：由于 `#[repr(C)]` 结构体字段偏移，一组同类型 cell 的地址可能恰好都是某个非 2 的幂的倍数（注释举例 `&[Foo]` 中字段 `a` 可能落在 3 的倍数上）。若 `LEN` 能被 3 整除，又会退化。选一个**大的素数**能同时避开「2 的幂对齐」和「意外对齐」两类退化。

**为什么包 `CachePadded`？** 数组里相邻的 `SeqLock` 会被放到同一/相邻缓存行；如果两个落在不同锁但同一缓存行的 cell 被不同 CPU 核高频访问，会产生 **false sharing**（伪共享），让缓存行反复失效。`CachePadded` 把每个 `SeqLock` 按架构填充对齐到缓存行长度，使每个锁独占自己的缓存行（详见 u2-l6）。

#### 4.1.4 代码实践

**实践目标**：直观验证「不同 cell 落到不同锁桶」。

**操作步骤**：

1. 新建一个 binary crate，加入 `crossbeam-utils`，并**开启 `atomic` feature**（AtomicCell 需要 `feature = "atomic"`，见 u1-l3）。
2. 运行下面的示例代码（**示例代码**，非项目原有）：

```rust
use crossbeam_utils::atomic::AtomicCell;

const LEN: usize = 67; // 与源码 lock() 内一致

fn main() {
    // [u8; 40] 不匹配任何原生原子宽度(最大 16 字节)，必然走全局锁回退
    let cells: Vec<AtomicCell<[u8; 40]>> = (0..16).map(|_| AtomicCell::new([0; 40])).collect();
    for (i, c) in cells.iter().enumerate() {
        let addr = c.as_ptr() as usize;
        println!("cell {i:2}: addr={addr:#x}  bucket={}", addr % LEN);
    }
    println!("is_lock_free([u8;40]) = {}", AtomicCell::<[u8; 40]>::is_lock_free());
}
```

**需要观察的现象**：16 个 cell 的 `bucket` 取值应分散在 0..66 之间，基本不重复或很少重复；`is_lock_free` 应打印 `false`（确认走回退路径）。

**预期结果**：bucket 分散；`is_lock_free` 为 `false`。具体地址值依赖运行环境，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `LEN` 改成 64（2 的幂），对一组全部按 8 字节对齐的 `AtomicCell<u64>`（地址都是 8 的倍数）地址取模，会有多少把锁被实际命中？

**答案**：`addr` 是 8 的倍数，`addr % 64` 也是 8 的倍数，所以只会命中下标为 0、8、16、…、56 的 8 把锁，即 64 把锁里只有 8 把（1/8）被用到。这就是源码坚持用素数的原因。

**练习 2**：为什么 `LOCKS` 是 `static` 而不是每个 `AtomicCell` 自己存一把锁？

**答案**：把锁放进 `AtomicCell` 会改变其内存布局（`AtomicCell` 是 `#[repr(transparent)]`，布局必须等同 `T`，否则无锁路径的 transmute 就不成立）。所以锁只能外置；用全局 `static` 锁池 + 地址哈希，既不污染布局，又能让「同一 cell 反复操作总是命中同一把锁」从而保证互斥。

---

### 4.2 SeqLock 印戳与 write/abort

#### 4.2.1 概念说明

`SeqLock` 用**一个 `AtomicUsize`** 同时编码「锁状态」和「版本号」，这是它最巧妙的地方：

- **最低位（LSB）是锁位**：为 1 表示正在写（locked），为 0 表示空闲。
- **其余位是版本号（stamp）**：每次成功写完，版本号 +1。

由于写者持锁时把整个 `state` 设成 `1`（锁位 1、版本部分视作无效），而写完释放时把版本号推进，所以读者看到的合法状态永远是**偶数**（锁位 0）。读者用「读到的偶数值」当作 stamp 去比较。

#### 4.2.2 核心流程

**写者加锁 `write()`**（自旋直到拿到锁）：

```
loop:
    previous = state.swap(1, Acquire)   # 无论原来是什么，原子地写成 1
    if previous != 1:                    # 原来不是 1，说明原来空闲，我现在拿到锁了
        fence(Release)                   # 让随后要写的数据“挂”到这次释放序列上
        return guard{ state: previous }  # 记下加锁前的 stamp，供解锁时还原
    else:
        backoff.snooze()                 # 别人正持有，退避后重试
```

**写者解锁（`SeqLockWriteGuard::drop`）**：把 `state` 从加锁前的 `previous` 推进到 `previous + 2`（用 `Release`）。`+2` 而不是 `+1`，是因为 LSB 是锁位：偶数 `previous` 加 2 后仍是偶数（释放锁），同时版本号（高位）正好 +1。

**写者放弃 `abort()`**：把 `state` 还原成 `previous`（**不**推进版本号），然后 `mem::forget(self)` 跳过 `drop`。用于「我拿了写锁但其实没改数据」的场景——既然没改，就不该让其他乐观读者误以为数据变了而白白重试。

印戳随连续写入的演化（设两次写）：

| 时刻 | state | 含义 |
| --- | --- | --- |
| 初值 | `0` | 空闲，版本 0 |
| 写者A 持锁 | `1` | locked |
| 写者A 释放 | `2` | 空闲，版本 1（`0+2`） |
| 写者B 持锁 | `1` | locked |
| 写者B 释放 | `4` | 空闲，版本 2（`2+2`） |

#### 4.2.3 源码精读

SeqLock 结构与字段注释：

[src/atomic/seq_lock.rs:9-15](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/seq_lock.rs#L9-L15) —— 用一个 `AtomicUsize` 同时承担锁位与版本号；注释明确「locked 时 state==1，不含合法 stamp」。

`write()` 自旋加锁：

[src/atomic/seq_lock.rs:43-61](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/seq_lock.rs#L43-L61) —— `swap(1, Acquire)` 抢锁；抢到后 `fence(Release)` 建立「数据写入 → 解锁 store」的释放序列；抢不到用 `Backoff::snooze()` 退避（u2-l5）。

`SeqLockWriteGuard` 的两条释放路径：

[src/atomic/seq_lock.rs:73-93](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/seq_lock.rs#L73-L93) —— `abort()` 用 `store(previous, Release)` 还原并用 `mem::forget` 跳过 `drop`；而真正的 `drop` 用 `store(previous.wrapping_add(2), Release)` 同时「解锁 + 版本号 +1」。`wrapping_add` 处理溢出，这与 16/32 位平台改用宽计数器的 `seq_lock_wide.rs` 直接相关（见 u5-l3）。

配套测试，佐证 `abort` 不改变版本号：

[src/atomic/seq_lock.rs:99-109](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/seq_lock.rs#L99-L109) —— `optimistic_read` 在 `write().abort()` 前后返回相同 stamp。

#### 4.2.4 代码实践

**实践目标**：通过阅读测试与源码，验证「`abort` 不推进版本号、`drop` 推进版本号」。

**操作步骤**：

1. 打开 `src/atomic/seq_lock.rs` 末尾的 `tests::test_abort`（见上面链接）。
2. 在脑海里把它改写成「`drop` 版」：把 `guard.abort();` 换成什么都不做（让 guard 自然 drop）。
3. 推断 `before` 与 `after` 的关系会变成什么。

**需要观察的现象 / 预期结果**：

- 原测试：`before == after`（abort 不改版本号），断言通过。
- 改写为 drop 版后：`after == before + 2`（版本号 +1，且仍是偶数），原 `assert_eq!(before, after)` 会失败。
- 你可以真的复制这个测试到本地改写运行，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `write()` 用 `swap(1, Acquire)` 而不是 `compare_exchange(0, 1)`？

**答案**：`swap` 无条件写成 1 并返回旧值。若旧值 `!= 1`，说明加锁前是空闲态（偶数），即可判定抢锁成功；若旧值 `== 1`，说明别人持有，自旋重试。用 `compare_exchange(0,1)` 只能识别「旧值恰好是 0」这一种空闲态，遇到「旧值是 2/4/6…」（也是空闲，只是版本号更大）会误判为失败，从而错误地自旋。

**练习 2**：`drop` 里用 `previous.wrapping_add(2)` 而不是 `previous + 2`，是为了应对什么？

**答案**：防止长时间运行的 cell 上版本号溢出。`usize` 在 32 位平台上只有 32 位，约 21 亿次写入后高位会回绕；`wrapping_add` 让回绕不触发 panic。在指针宽度 ≤ 32 的架构上，crossbeam 进一步改用「宽 SeqLock」（`seq_lock_wide.rs`），把计数器加宽以降低回绕风险——这正是 u5-l3 的主题。

---

### 4.3 atomic_load 的乐观读路径

#### 4.3.1 概念说明

现在把 4.1、4.2 拼起来。`atomic_load` 的回退分支要无锁地、不读到撕裂值地把一个「无法原子化的 `T`」读出来。它的策略是 SeqLock 的标准用法：

1. 先 `optimistic_read()` 拿一个 stamp（如果此刻正被写、stamp 是 1，就拿到 `None`）。
2. 用 `ptr::read_volatile` 把数据「粗鲁地」按字节读出来——读的时候**不持锁**，所以可能读到写者写到一半的中间态。
3. 再 `validate_read(stamp)` 校验：stamp 没变 = 没有写者插手 = 读到的是一致快照，直接返回；变了 = 作废，重试。
4. 如果乐观读拿不到（stamp 是 1）或校验失败，就退一步：**自己拿写锁**读，读完用 `abort()` 放锁（不污染版本号）。这一步同时保证「写者不会饿死读者」。

#### 4.3.2 核心流程

读者乐观读的时序（成功路径）：

```
读者              state        数据
─────             ─────        ────
optimistic_read  -> 读到 stamp=S（偶数）
read_volatile    -> 读出数据 D（可能撕裂）
validate_read(S) -> state 仍是 S 且为偶数？
   是 -> return D            # S 前后都没变 => D 是版本 S 下的一致快照
   否 -> 落到下面的「持锁读」
```

写者与读者的并发交错（这是本讲代码实践要画的核心时序）：

```
写者                  读者
────                  ────
                      optimistic_read() -> S=2
swap(1)  state=1      read_volatile -> 读到撕裂的 D'
fence(Release)
写数据 D
store(4) state=4      validate_read(2): state=4 != 2 => 作废 D'
                      -> 改走 lock.write() 持锁读 D，abort 放锁
```

关键点：读者读到撕裂值 `D'` **不要紧**，因为 `validate_read` 会发现 stamp 从 2 变成了 4（写者完成了一次写入），从而丢弃 `D'`。读者只会返回「stamp 前后都等于同一个偶数」时读到的值——那个窗口里没有写者，数据是一致的。

#### 4.3.3 源码精读

`optimistic_read`：读 stamp（locked 时返回 `None`）：

[src/atomic/seq_lock.rs:24-31](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/seq_lock.rs#L24-L31) —— `load(Acquire)` 读 state；`== 1` 说明正被写，返回 `None`，否则返回该偶数值作为 stamp。

`validate_read`：校验 stamp 是否仍有效：

[src/atomic/seq_lock.rs:33-41](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/seq_lock.rs#L33-L41) —— 先 `fence(Acquire)`（防止编译器/CPU 把前面的 `read_volatile` 数据读重排到校验之后），再 `load(Relaxed)` 比 stamp。

`atomic_load` 的回退分支，把上面两步与「持锁读」拼起来：

[src/atomic/atomic_cell.rs:1029-1069](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L1029-L1069) —— 先尝试乐观读，失败则持写锁读。重点看 fallback 闭包：

```rust
let lock = lock(src as usize);
if let Some(stamp) = lock.optimistic_read() {
    // 读成 MaybeUninit，因为读到的字节可能不是合法 T（撕裂态）
    let val = unsafe { ptr::read_volatile(src.cast::<MaybeUninit<T>>()) };
    if lock.validate_read(stamp) {
        return unsafe { val.assume_init() };   // 校验通过，确认是合法 T
    }
}
// 写者可能持续推进 stamp 饿死乐观读；改持写锁，确保读到一致值
let guard = lock.write();
let val = unsafe { ptr::read(src) };
guard.abort();   // 没改数据，不推进版本号，避免误伤其他乐观读者
val
```

几个需要特别说明的点：

- **为什么 `read_volatile`**：读者不持锁，写者可能正在并发改这块内存。volatile 防止编译器把这次读优化掉或合并；注释诚实地承认「理论上数据竞争总是 UB」，这是当前 stable Rust 没有可用 `AtomicU8` 等的**务实折中**（深入论证见 u5-l1）。
- **为什么按 `MaybeUninit<T>` 读再 `assume_init`**：撕裂读到的字节组合**可能不是合法的 `T`**（例如枚举的非法判别式）。先按 `MaybeUninit` 读，只有 `validate_read` 通过、确认这其实是一致快照后，才 `assume_init` 当成合法 `T` 返回。
- **为什么持锁读要用 `abort()`**：读者拿了写锁只是为了「安静地读」，并没有改数据。若走正常 `drop`，会把 stamp +1，导致**其他正在乐观读的读者**误以为数据变了而白白重试。`abort()` 还原 stamp、跳过 `drop`，对其他读者「无副作用」。
- **为什么持锁读能防饿死**：乐观读最多尝试一次；若 stamp 一直被写者推进，读者立刻升级为持锁读，与写者互斥，必定能读到一致值。

`atomic_store`/`atomic_swap`/`atomic_compare_exchange_weak` 的回退分支用同一把 `lock().write()`，写完自然 `drop`（推进 stamp，使正在进行中的乐观读者作废重试）；其中 `compare_exchange_weak` 在「值不匹配、没写成」时也调用 `guard.abort()` 不污染版本号：

[src/atomic/atomic_cell.rs:1154-1166](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L1154-L1166) —— CAS 回退：读旧值，相等则写新值并正常 drop（推进 stamp）；不相等则 `abort()` 还原。

#### 4.3.4 代码实践（本讲指定实践）

**实践目标**：画出「多读者 + 单写者」并发时，乐观读失败重试的完整时序图，并据此解释「为什么读不到撕裂值」。

**操作步骤**：

1. 重读 [src/atomic/atomic_cell.rs:1043-1067](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L1043-L1067) 与 [src/atomic/seq_lock.rs:24-61](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/seq_lock.rs#L24-L61)。
2. 在纸上画出两条泳道（Writer / Reader），含 state 变量在每个动作后的取值。要求覆盖以下三种情形：
   - **情形 A（乐观读成功）**：读者在两次 stamp 读取之间，没有任何写者活动。
   - **情形 B（乐观读被作废，升级为持锁读）**：写者在读者 `read_volatile` 期间完成了一次完整写入（state 2→1→4）。
   - **情形 C（写者持锁时读者发起）**：读者 `optimistic_read()` 返回 `None`（state==1），直接进入持锁读分支。
3. 对每个情形，标出读者最终返回的值是「哪个版本下的完整快照」。

**需要观察的现象 / 预期结果**：

- 情形 A：stamp 全程 = `S`，读者返回 `S` 版本的值。
- 情形 B：stamp 从 `S` 变为 `S+2`，`validate_read` 失败，读者丢弃撕裂值，持锁读到 `S+2`（或更新）版本的完整值。
- 情形 C：`optimistic_read` 直接 `None`，读者持锁读到当前完整值。
- 结论：三种情形下读者都**不会**返回「两个字段来自不同版本」的撕裂值。

参考时序（情形 B，可在你的图上对照）：

```
state 初值 = 2 (版本1, 空闲)
Writer: swap(1,Acquire)         state=1
Reader: optimistic_read()       -> None? 不，这一行在 swap 之前
```

> 提示：编排时把 Reader 的 `optimistic_read` 放在 Writer `swap` **之前**拿到 `S=2`，然后 Writer 完成整次写入（state 2→1→4），最后 Reader `validate_read(2)` 看到 4，作废。这正是本讲 4.3.2 给出的时序。完整时序图的精确绘制**待本地完成**。

#### 4.3.5 小练习与答案

**练习 1**：如果删除 `validate_read` 里的 `fence(Acquire)`，会出什么问题？

**答案**：CPU/编译器可能把「读数据」重排到「校验 stamp」之后，于是读者可能先看到 stamp 没变（校验通过），再读到被写者改到一半的撕裂数据——破坏一致性。`fence(Acquire)` 强制「数据读」发生在「校验 stamp 读」之前，配合写者的 `fence(Release)` + `store(Release)`，才能保证「stamp 前后都等于 S ⇒ 数据就是版本 S 的完整快照」。

**练习 2**：乐观读失败后，读者为什么是「升级为持锁读」而不是「循环重试乐观读」？

**答案**：两个原因。其一，若持续重试乐观读，写者只要写得很勤，stamp 就一直推进，读者可能永远等不到稳定的 stamp——即被饿死；持锁读与写者互斥，保证读者必然能读到一个一致值。其二，最多只做一次乐观读再升级，开销有上界，避免在高竞争下退化为忙等自旋。

**练习 3**：`atomic_load` 持锁读后调用 `guard.abort()`，而 `atomic_store` 写完后让 guard 正常 `drop`。这一区别的根本原因是什么？

**答案**：`load` 没有修改数据，若推进版本号会让其他正在进行乐观读的读者误判「数据变了」而白白重试，所以 `abort()` 还原 stamp、对他人无副作用；`store` 确实写了新数据，**必须**推进版本号，才能正确地让所有正在乐观读旧值的读者作废重试，否则它们会校验通过并返回过期的旧值。

---

## 5. 综合实践

**任务**：用一个「内置不变量」的大类型压测 `AtomicCell`，证明 SeqLock 回退路径不会产生撕裂读。

设计思路：写者始终写入满足不变量 `[v, v, v]` 的 `Triple([u64;3])`（24 字节，超过最大原生原子宽度 16，**必然走全局锁 + SeqLock 回退**）。多个读者不断 `load` 并校验「三个分量必须相等」。如果回退路径有任何撕裂，读者就能观测到三个分量不一致；若 SeqLock 正确，撕裂次数恒为 0。

**示例代码**（非项目原有代码）：

```rust
use crossbeam_utils::atomic::AtomicCell;
use std::sync::Arc;
use std::thread;

#[repr(C)]
#[derive(Copy, Clone, Debug)]
struct Triple([u64; 3]); // 24 字节，is_lock_free() == false

fn main() {
    let cell = Arc::new(AtomicCell::new(Triple([0u64; 3])));
    const ITERS: usize = 1_000_000;

    // 单写者：始终维护不变量 [v, v, v]
    let writer = {
        let cell = cell.clone();
        thread::spawn(move || {
            let mut v = 0u64;
            for _ in 0..ITERS {
                v = v.wrapping_add(1);
                cell.store(Triple([v, v, v]));
            }
        })
    };

    // 多读者：校验不变量，统计“撕裂”次数
    let readers: Vec<_> = (0..4)
        .map(|_| {
            let cell = cell.clone();
            thread::spawn(move || {
                let mut torn = 0u64;
                for _ in 0..ITERS {
                    let Triple(a) = cell.load();
                    if !(a[0] == a[1] && a[1] == a[2]) {
                        torn += 1; // 读到了来自不同版本的撕裂值
                    }
                }
                torn
            })
        })
        .collect();

    writer.join().unwrap();
    let total_torn: u64 = readers.into_iter().map(|h| h.join().unwrap()).sum();
    println!("is_lock_free = {}", AtomicCell::<Triple>::is_lock_free());
    println!("torn reads observed: {total_torn}");
    assert_eq!(total_torn, 0, "SeqLock 回退路径必须阻止撕裂读");
}
```

**串联本讲三个模块**：

- 写者的 `store` 走 `atomic_store` 回退分支 → 调 `lock(addr).write()` 取到 4.1 的锁池里那把锁 → SeqLock 加锁、写数据、`drop` 推进 stamp（4.2）。
- 读者的 `load` 走 `atomic_load` 回退分支 → `optimistic_read` → `read_volatile` → `validate_read`，必要时升级为持锁读 + `abort`（4.3）。
- 三分量恒相等 = 读者每次拿到的都是某个完整版本 `[v,v,v]`，没有跨版本拼接。

**预期结果**：`is_lock_free = false`；`torn reads observed: 0`，断言通过。由于线程调度依赖环境，**待本地验证**；若想亲见「撕裂」长什么样，可以把 `cell.load()` 换成对裸 `UnsafeCell` 的非原子读作为对照组（仅用于理解，不是 AtomicCell 的行为）。

---

## 6. 本讲小结

- 回退路径用一个**全局锁池**而非单锁：`lock(addr) = LOCKS[addr % 67]`，`LEN=67` 是素数以避开 2 的幂对齐导致的退化，每个锁包 `CachePadded` 防 false sharing。
- `SeqLock` 用单个 `AtomicUsize` 同时编码「锁位(LSB)+ 版本号」：空闲态是偶数，持锁态是 `1`；`write()` 用 `swap(1,Acquire)` 抢锁，`drop` 用 `previous+2` 解锁并推进版本号。
- `abort()` 是「拿了写锁但没改数据」时还原 stamp、跳过 `drop` 的专用释放，避免误伤其他乐观读者。
- `atomic_load` 回退路径：先 `optimistic_read` 拿 stamp → `ptr::read_volatile` 读成 `MaybeUninit<T>` → `validate_read` 校验；通过则 `assume_init` 返回一致快照，失败则升级为持锁读 + `abort`，防写者饿死读者。
- 撕裂值不会泄漏给调用方：任何被写者插手的乐观读都会因 stamp 变化而在 `validate_read` 处作废；读者只接受「前后 stamp 相等且偶数」窗口内的数据。
- 写类操作（`store`/`swap`/`fetch_*`）走 `write()` + 正常 `drop` 推进 stamp，从而正确作废进行中的乐观读者；`compare_exchange` 未命中时也用 `abort()` 不污染版本号。

---

## 7. 下一步学习建议

- **横向**：本讲的 SeqLock 印戳是「状态编码」的小型范例；u3 的 `Parker`（三态机 EMPTY/PARKED/NOTIFIED）和 `WaitGroup`（引用计数 + Condvar）是另一类同步原语，可对比「无锁乐观」与「Mutex+Condvar 阻塞」两种思路。
- **纵向（深入 unsafe）**：本讲刻意回避了 `ptr::read_volatile` 在数据竞争下「理论上仍 UB」的严格论证，以及 acquire/release fence 如何形式化地保证 SeqLock 正确性——这正是 u5-l1《AtomicCell 的 unsafe 与内存安全深析》的主题。
- **纵向（跨平台）**：`mod.rs` 会按指针宽度把 `seq_lock` 切换成 `seq_lock_wide.rs`，用宽计数器防回绕；这条 cfg 链在 u5-l3《跨平台 cfg、loom 抽象与宽 SeqLock》里完整拆解。
- **建议阅读的源码**：先把本讲的 `seq_lock.rs` 通读一遍（仅 110 行），再回到 `atomic_cell.rs` 把四个 `atomic_*` 自由函数的 fallback 分支对照着看，体会「同一把锁池 + 同一套 SeqLock」如何复用于 load/store/swap/cas。
