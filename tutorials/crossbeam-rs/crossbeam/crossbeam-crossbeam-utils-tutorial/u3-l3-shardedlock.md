# ShardedLock 分片读写锁

## 1. 本讲目标

本讲专讲 `crossbeam_utils::sync::ShardedLock`——一把「读快写慢」的读写锁。读完本讲，你应当能够：

- 说清楚 `ShardedLock` 内部为什么是 **8 个** `CachePadded<Shard>`，以及为什么这个数字必须是 **2 的幂**；
- 跟踪一次 `read()` 的完整路径：从「按当前线程选分片」到只锁住一个分片；
- 跟踪一次 `write()` 的完整路径：逐个锁住全部 8 个分片、把 guard 暂存进分片、最后倒序释放；
- 解释「读快写慢」的物理来源（cache line 争用），以及为何读路径几乎不进注册表锁；
- 理解 poison（中毒）语义为何只在写操作 panic 时触发，以及写 guard 为何要把生命周期 `transmute` 成 `'static`。

本讲是 `sync` 单元的第三篇，硬前置是 [u2-l6 CachePadded](./u2-l6-cachepadded.md)（false sharing 与 `repr(align)`），并承接 [u3-l2 WaitGroup](./u3-l2-waitgroup.md) 中建立的「`Arc` 共享 + 线程注册」直觉。

## 2. 前置知识

### 2.1 读写锁（RwLock）

标准库的 [`std::sync::RwLock`](https://doc.rust-lang.org/std/sync/struct.RwLock.html) 是一把读写锁：

- **多个读 guard 可以同时持有**（共享访问）；
- **任意时刻只能有一个写 guard**（独占访问），且写 guard 存在时不能再有读 guard。

`ShardedLock` 在语义上和 `RwLock` 完全等价——它对外暴露的 `read()/write()/try_read()/try_write()` 以及 poison 行为都和 `RwLock` 一致。区别只在内部实现：它不是「一把大锁」，而是「很多把小锁」。

### 2.2 false sharing（虚假共享）与 CachePadded

这是本讲的核心前置。当多个线程高频访问的、彼此逻辑独立的变量恰好落在 **同一条 cache line**（通常 64 或 128 字节）里时，一个核的写会让整条 cache line 在其他核上失效，于是其他核哪怕读的是另一个变量，也要重新从内存拉取——这就是 false sharing。

`CachePadded<T>` 用 `#[repr(align(N))]` 把 `T` 撑大到独占一整条 cache line，从而消除 false sharing。详见 [u2-l6](./u2-l6-cachepadded.md)。本讲的关键观察是：**并发读想要可扩展（scale），就必须让不同线程的读操作落到不同的 cache line 上。**

### 2.3 一个直觉：为什么要把锁拆开

假设你有一把 `RwLock`，100 个线程都在反复 `read()`。即便读是共享的，标准库 `RwLock` 内部仍有一份状态（计数器、等待队列）集中在一个 cache line 上，100 个核同时去碰这一份状态，就会产生剧烈的 cache line 争用（也叫「热点」）。

如果把锁拆成 8 把独立的小锁，让不同线程去锁不同的小锁，每把小锁的 cache line 上平均只有 1/8 的流量，争用就大幅下降——这就是「分片（sharding）」的动机。代价是：**写操作必须把 8 把小锁全部锁住**（否则它无法保证没有任何读者正在看数据），所以写变慢了。

读快写慢，正是 `ShardedLock` 的设计取舍。

## 3. 本讲源码地图

本讲涉及的关键源码文件：

| 文件 | 作用 |
|------|------|
| [src/sync/sharded_lock.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/sharded_lock.rs) | `ShardedLock` 及其两个 guard、线程索引注册表的全部实现 |
| [src/sync/mod.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/mod.rs) | 模块声明与 `pub use` 重导出，含 `not(crossbeam_loom)` 门控 |
| [src/cache_padded.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/cache_padded.rs) | `CachePadded<T>` 定义（u2-l6 已详述，本讲只消费它） |
| [src/sync/once_lock.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/once_lock.rs) | `pub(crate) OnceLock`，用于惰性建立全局线程索引注册表 |

`ShardedLock` 在 `src/sync/mod.rs` 中以私有 `mod` + `pub use` 的方式重导出，且被 `#[cfg(not(crossbeam_loom))]` 门控（loom 下不可用，因为它依赖 `ThreadId` 与标准库 `RwLock` 的 poison 行为）：

[src/sync/mod.rs:10-15](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/mod.rs#L10-L15) —— 私有声明 `mod sharded_lock` 并在非 loom 下重导出 `ShardedLock` 与两个 guard 类型。

---

## 4. 核心概念与源码讲解

### 4.1 分片数据结构：Shard、NUM_SHARDS 与 CachePadded

#### 4.1.1 概念说明

`ShardedLock<T>` 内部不是一把锁，而是 **8 把独立的读写锁**，外加受保护的数据 `T`。这 8 把锁中的每一把称为一个 **shard（分片）**。

为什么是 8？这是一个工程经验值：大到足以让常见并发度的线程分散开来，小到不让写操作（要锁全部）过于昂贵。更关键的约束是注释里的这句话：

> The number of shards per sharded lock. Must be a power of two.

「必须是 2 的幂」是给 **读路径的取模优化** 用的（见 4.2）。每个 shard 用 `CachePadded` 包裹，确保 8 把锁各自独占一条 cache line，互不干扰——这是消除 shard 之间 false sharing 的前提。

#### 4.1.2 核心流程

整体结构可以这样画：

```
ShardedLock<T>
├── shards: Box<[CachePadded<Shard>; 8]>
│        ├── CachePadded<Shard>  ← 独占一条 cache line
│        │     ├── lock: RwLock<()>
│        │     └── write_guard: UnsafeCell<Option<RwLockWriteGuard<'static, ()>>>
│        ├── CachePadded<Shard>  ← 独占另一条 cache line
│        └── ... (共 8 个)
└── value: UnsafeCell<T>          ← 真正受保护的数据
```

注意几个设计要点：

1. **`Shard.lock` 保护的是 `()`，不是 `T`**。`T` 单独放在 `value` 字段里。也就是说，shard 上的 `RwLock` 只起「发放读/写许可」的作用，guard 通过裸指针去访问 `value`。这样 8 个 shard 的锁大小都极小（`RwLock<()>`），且彼此类型一致。
2. **`write_guard` 字段** 用来在写操作期间「暂存」每个 shard 的写 guard，详见 4.3。
3. **`value: UnsafeCell<T>`** 提供内部可变性，因为 `read()/write()` 都只拿 `&self`。

#### 4.1.3 源码精读

`NUM_SHARDS` 常量与「必须是 2 的幂」的注释：

[src/sync/sharded_lock.rs:21-22](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/sharded_lock.rs#L21-L22) —— 定义分片数为 8，并注释要求 2 的幂。

单个 `Shard` 结构：

[src/sync/sharded_lock.rs:24-34](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/sharded_lock.rs#L24-L34) —— 每个 shard 由一把 `RwLock<()>` 和一个暂存写 guard 的 `UnsafeCell` 组成。注释说明写操作会把每个 shard 的 guard 存进来，等大 guard 释放时一起 drop。

`ShardedLock` 顶层结构与 `Send/Sync` 实现：

[src/sync/sharded_lock.rs:82-91](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/sharded_lock.rs#L82-L91) —— `shards` 是 `Box<[CachePadded<Shard>]>`，`value` 是 `UnsafeCell<T>`。`Send`/`Sync` 的实现要求和 `RwLock` 一致：`T: Send` 即可 `Send`，`T: Send + Sync` 才能 `Sync`。

`new()` 构造 8 个 shard：

[src/sync/sharded_lock.rs:106-118](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/sharded_lock.rs#L106-L118) —— 用 `(0..NUM_SHARDS).map(...)` 生成 8 个 `CachePadded::new(Shard { ... })` 并收集成 `Box<[...]>`。

#### 4.1.4 代码实践

**实践目标**：直观感受「8 个 shard 各自独占一条 cache line」。

**操作步骤**：

1. 新建一个依赖 `crossbeam_utils` 的 binary crate。
2. 写一段代码打印 `ShardedLock<()>` 的大小，并与「裸 `RwLock<()>` 的大小」对比。

```rust
// 示例代码（非项目原有，需自行放入你的 binary crate）
use crossbeam_utils::sync::ShardedLock;
use std::sync::RwLock;

fn main() {
    println!("sizeof ShardedLock<()> = {}", std::mem::size_of::<ShardedLock<()>>());
    println!("sizeof RwLock<()>      = {}", std::mem::size_of::<RwLock<()>>());
}
```

**需要观察的现象**：在 x86-64 上，`CachePadded` 按 128 字节对齐（见 u2-l6），`Box<[CachePadded<Shard>; 8]>` 是一个堆指针（size_of 为一个 usize），所以 `ShardedLock<()>` 本体很小；真正占空间的是堆上的 8 个 128 字节 shard。你可以用 `std::mem::align_of::<ShardedLock<()>>()` 观察对齐。

**预期结果**：`ShardedLock<()>` 的 `size_of` 与一个 `Box` 差不多（一个指针宽度），因为它把 8 个 shard 放在堆上。具体的 `RwLock<()>` 大小随平台/标准库版本变化。

**待本地验证**：确切的字节数请以你本机实测为准。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `NUM_SHARDS` 改成 6（不是 2 的幂），代码里哪一行会出错或产生错误结果？

> **答案**：`shard_index = current_index & (self.shards.len() - 1)` 这一行（见 4.2）。当 `len()` 不是 2 的幂时，`len() - 1` 不再是一个干净的「掩码」，`&` 运算不再等价于取模，会产生一个不均匀甚至越界的 shard 分布（例如 `len=6` 时 `len-1=5=0b101`，多个不同 index 会被映射到同一个 shard，且永远不会命中某些 shard）。注释里的「Must be a power of two」正是为此而设。

**练习 2**：为什么每个 `Shard` 用 `CachePadded` 单独包裹，而不是把 8 个 `Shard` 放进一个连续数组后整体对齐？

> **答案**：整体对齐只能保证数组起点对齐，不能保证相邻 `Shard` 不共享 cache line。`CachePadded` 让**每一个** `Shard` 都撑满一整条 cache line，从而保证 8 把锁的状态字段必然分散在 8 条不同的 cache line 上，彻底消除 shard 间的 false sharing。

---

### 4.2 读路径：current_index 选分片

#### 4.2.1 概念说明

读操作的核心思想是：**每个线程根据自己的身份，固定地选一个 shard 来锁**。这样不同线程大概率选到不同 shard，各自碰各自的 cache line，互不干扰。

那么「线程身份」用什么？标准库的 [`ThreadId`](https://doc.rust-lang.org/std/thread/struct.ThreadId.html) 是个不透明的值，无法直接拿来取模。`ShardedLock` 维护了一个 **全局线程索引注册表**，把每个 `ThreadId` 映射成一个紧凑的小整数 `index`（0, 1, 2, ...），读路径就用这个 `index` 来选 shard。

关键性能点：**注册表锁不在读热路径上**。每个线程的 `index` 被缓存在线程本地存储（TLS）里，`read()` 只需读一次 TLS，做一次按位与，就能选定 shard——不需要碰任何 `Mutex`。注册表锁只在「线程第一次访问」和「线程退出」时才上。

#### 4.2.2 核心流程

一次 `read()` 的完整流程：

```
read()
  │
  ├─ current_index()                       ← 读 TLS（O(1)，无锁）
  │     └─ REGISTRATION.try_with(|r| r.index)
  │           （首次访问时，TLS 初始化器去注册表申请一个 index）
  │
  ├─ shard_index = index & (NUM_SHARDS-1)  ← 按位与，等价于 index % 8
  │
  ├─ shards[shard_index].lock.read()       ← 只锁一个 shard 的 RwLock<()>
  │
  └─ 返回 ShardedLockReadGuard { lock: &self, _guard, _marker }
        └─ guard 的 Deref 直接读 self.value（裸指针）
```

shard 选择的数学等价性：

\[ \text{shard} = \text{index} \mathbin{\&} (\text{NUM\_SHARDS} - 1) = \text{index} \bmod 8 \]

后者成立 **当且仅当** NUM_SHARDS 是 2 的幂。按位与比取模快（取模是一条除法指令，与运算是一条 `AND`），这也是「必须 2 的幂」的真正原因。

由于线程索引是顺序分配的（0, 1, 2, ...），连续创建的线程会被均匀地分配到 8 个 shard 上：

| 线程 index | index & 7 | 选中的 shard |
|------------|-----------|--------------|
| 0          | 0         | 0            |
| 1          | 1         | 1            |
| 2          | 2         | 2            |
| ...        | ...       | ...          |
| 7          | 7         | 7            |
| 8          | 0         | 0（循环）    |
| 9          | 1         | 1            |

#### 4.2.3 源码精读

读路径入口 `read()` 与分片选择：

[src/sync/sharded_lock.rs:290-308](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/sharded_lock.rs#L290-L308) —— 注意第 293-294 行：取 `current_index().unwrap_or(0)`，再 `& (self.shards.len() - 1)` 选 shard。`try_read()` 的逻辑完全对称：

[src/sync/sharded_lock.rs:230-252](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/sharded_lock.rs#L230-L252) —— `try_read` 同样按 `current_index & (len-1)` 选 shard，只是把阻塞的 `read` 换成非阻塞的 `try_read`，并处理 `WouldBlock` 与 `Poisoned` 两种错误。

`current_index()` 读取 TLS：

[src/sync/sharded_lock.rs:577-580](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/sharded_lock.rs#L577-L580) —— `REGISTRATION.try_with(|reg| reg.index).ok()`。注释说明：因为该函数访问 TLS，若当前线程的 TLS 正在销毁，可能返回 `None`（`.ok()` 把 `AccessError` 转成 `None`），此时 `unwrap_or(0)` 让它回落到 shard 0。

TLS 的初始化器（线程首次访问时执行）——这是注册表锁唯一参与读路径的地方：

[src/sync/sharded_lock.rs:622-642](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/sharded_lock.rs#L622-L642) —— 取当前 `ThreadId`，锁住全局注册表，优先从 `free_list` 复用一个已释放的 index，否则分配 `next_index` 并自增；然后把 `ThreadId → index` 写进 `mapping`，构造一个 `Registration` 存入 TLS。

注册表本身用 [u3-l4 OnceLock](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/once_lock.rs) 惰性初始化：

[src/sync/sharded_lock.rs:583-604](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/sharded_lock.rs#L583-L604) —— `ThreadIndices` 含三部分：`mapping: HashMap<ThreadId, usize>`、`free_list: Vec<usize>`、`next_index: usize`。`thread_indices()` 用一个 `static OnceLock<Mutex<ThreadIndices>>` 保证全局唯一且线程安全地初始化。

线程退出时回收 index：

[src/sync/sharded_lock.rs:609-620](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/sharded_lock.rs#L609-L620) —— `Registration::drop` 从 `mapping` 移除自己，把自己的 index 推回 `free_list`，供未来的线程复用。这就是 index 能保持「紧凑小整数」的原因。

读 guard 通过裸指针访问 `value`：

[src/sync/sharded_lock.rs:498-504](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/sharded_lock.rs#L498-L504) —— `ShardedLockReadGuard::deref` 直接 `unsafe { &*self.lock.value.get() }`。注意 guard 内部持有一把 `RwLockReadGuard<'a, ()>`（即 `_guard` 字段），正是这把 `_guard` 在生命周期上「守住」了共享访问许可；`_marker: PhantomData<RwLockReadGuard<'a, T>>` 只是为了让 drop 检查器认为 guard 持有 `T` 的共享借用。

#### 4.2.4 代码实践

**实践目标**：跟踪一次 `read()` 的调用链，确认注册表锁不在热路径上。

**操作步骤**：

1. 打开 [src/sync/sharded_lock.rs:290](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/sharded_lock.rs#L290)，从 `pub fn read` 开始往下读。
2. 在 `current_index()`（[第 578 行](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/sharded_lock.rs#L578)）处记录：它只调 `REGISTRATION.try_with`，没有 `lock()`。
3. 跳到 TLS 初始化器（[第 622 行](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/sharded_lock.rs#L622)），确认 `thread_indices().lock()` 只出现在「首次访问初始化」和 `Registration::drop` 两处，不在 `read` 函数体里。

**需要观察的现象**：`read` 函数体内（290-308 行）没有任何 `Mutex::lock` 调用；唯一可能的锁操作发生在 TLS 初始化器的惰性路径里，且每个线程只走一次。

**预期结果**：你能画出一条「`read → current_index(TLS 读) → & 7 → shards[i].lock.read()`」的链路，且这条链路上没有注册表锁。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `current_index()` 返回 `Option<usize>`，而调用处用 `unwrap_or(0)`？如果直接 `unwrap()` 会怎样？

> **答案**：`REGISTRATION.try_with` 在「当前线程的 TLS 已经在销毁」时会返回 `Err(AccessError)`（典型场景是线程退出过程中还有析构逻辑触发了 `read()`）。`.ok()` 把它转成 `None`，`unwrap_or(0)` 让这种情况回落到 shard 0，保证 `read()` 不会 panic。直接 `unwrap()` 会在这种边界场景下 panic，破坏 `RwLock` 语义所要求的「读永不 panic」约定。

**练习 2**：两个不同线程是否可能选中同一个 shard？这是 bug 吗？

> **答案**：完全可能，且不是 bug。例如 index 为 0 和 8 的两个线程都选中 shard 0。设计上只是「尽量」分散，并不保证一一对应；即便两个线程撞到同一个 shard，也只是一把普通 `RwLock` 的两个读者同时存在，仍然完全正确，只是这两把读操作的 cache line 仍会争用。当线程数远大于 8 时，碰撞不可避免，但相比「所有线程挤一把大锁」已经好得多。

**练习 3**：`mapping: HashMap<ThreadId, usize>` 这个映射看起来在读路径里没用到（读路径只用了 TLS 里的 `index`）。它存在的意义是什么？

> **答案**：它是 index 分配的记账数据。新线程来时用它判断是否已登记（实际实现是直接走 free_list/next_index 分配），线程退出时 `Registration::drop` 据此从 `mapping` 移除并把 index 推回 `free_list` 复用。读路径确实不查它——`index` 已经缓存在 TLS 里，所以读路径才那么快。

---

### 4.3 写路径：逐个锁全部 8 个分片与 guard 暂存

#### 4.3.1 概念说明

写操作要保证「独占访问 `T`」。但 `T` 的访问许可被分散在 8 个 shard 上，任何一个 shard 上有读者，都意味着有人在读 `T`。所以 **写操作必须把 8 个 shard 全部写锁住**，才能确认没有任何读者或写者在临界区里。

这就是「写慢」的来源：写一次 = 8 次阻塞的 `RwLock::write()`，且每次都可能和读者/其他写者竞争。

还有一个工程难题：写操作拿到了 8 个 guard，但 `ShardedLockWriteGuard` 这个返回类型里并不直接持有它们。它们被 **暂存到各 shard 的 `write_guard` 字段** 里，大 guard 只保留一个 `&ShardedLock` 引用；当大 guard drop 时，再遍历 shard 把这些暂存的 guard 取出来 drop（释放写锁）。

#### 4.3.2 核心流程

`write()` 的流程：

```
write(&self)
  │
  ├─ for shard in shards:           ← 顺序遍历 8 个 shard
  │     ├─ guard = shard.lock.write()       ← 阻塞，直到拿到该 shard 的写锁
  │     └─ 把 guard transmute 成 'static，存进 shard.write_guard
  │
  └─ 返回 ShardedLockWriteGuard { lock: &self }
        └─ Drop：倒序遍历 shards，把每个暂存的 guard take() 出来 drop
```

`try_write()` 多了一层「半途失败要回滚」的逻辑：

```
try_write(&self)
  │
  ├─ for (i, shard) in shards.enumerate():
  │     ├─ shard.lock.try_write()
  │     │     ├─ Ok(guard) → 暂存
  │     │     ├─ Poisoned(err) → 标记 poisoned，继续（取出 err 内部的 guard 暂存）
  │     │     └─ WouldBlock  → 记录 blocked = Some(i)，break
  │     └─ （若 break）倒序 drop 已锁住的 shards[0..i]，返回 WouldBlock
  │
  └─ 全部锁成功 → 返回 Ok(WriteGuard)；任一 poisoned → 返回 Poisoned
```

倒序（reverse order）释放是经典锁协议：**后锁的先放**，避免不必要的等待。

#### 4.3.3 源码精读

`write()` 主体：

[src/sync/sharded_lock.rs:414-447](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/sharded_lock.rs#L414-L447) —— 第 418 行起顺序 `for shard in self.shards.iter()`，对每个 shard 调 `shard.lock.write()`；第 427-433 行把 guard `transmute` 成 `'static` 后存进 `shard.write_guard`。注意第 421-424 行：若某 shard 的 `write()` 返回 `Poisoned`，会取出其内部 guard **继续锁剩下的 shard**，并把 `poisoned` 置真——这保证即使在中毒状态下，写操作仍会把全部 8 把锁拿全，guard 结构才一致。

`try_write()` 与回滚：

[src/sync/sharded_lock.rs:334-382](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/sharded_lock.rs#L334-L382) —— 第 339-358 行逐个 `try_write`；第 346-349 行遇 `WouldBlock` 记 `blocked = Some(i)` 后 `break`；第 360-369 行是回滚：对 `shards[0..i]` **倒序** take 出暂存的 guard 并 drop。第 370-381 行据 `blocked`/`poisoned` 决定返回 `WouldBlock` / `Poisoned` / `Ok`。

大 guard 的 `Drop`：

[src/sync/sharded_lock.rs:529-540](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/sharded_lock.rs#L529-L540) —— `ShardedLockWriteGuard::drop` 倒序遍历 `self.lock.shards`，对每个 shard `(*dest).take()` 取出暂存 guard 并 `drop`。这一步才真正释放 8 把写锁。

写 guard 的 `DerefMut`：

[src/sync/sharded_lock.rs:564-568](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/sharded_lock.rs#L564-L568) —— 与读 guard 对称，直接 `unsafe { &mut *self.lock.value.get() }`。能这样写的前提是：当前线程已通过全部 8 把写锁，独占了 `value`。

#### 4.3.4 代码实践

**实践目标**：直观体会「写慢于读」，并验证 `try_write` 的半途回滚。

**操作步骤**：

1. 阅读项目已有的测试 [tests/sharded_lock.rs:174-187](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/sharded_lock.rs#L174-L187)（`try_write` 测试）：先持有一个读 guard，再 `try_write()`，应得到 `WouldBlock`。
2. 自己写一个微基准（示例代码，非项目原有）：

```rust
// 示例代码：仅作示意，需放入你的 binary crate
use crossbeam_utils::sync::ShardedLock;
use std::sync::{Arc, RwLock};
use std::time::Instant;

fn bench_read<N: Fn() -> usize>(make: N, label: &str) {
    let lock = Arc::new((make(), ()));
    let start = Instant::now();
    let iters = 1_000_000u64;
    // 单线程读，观察单次 read 的开销
    for _ in 0..iters {
        let _g = { /* 取一个读 guard，立即 drop */ };
    }
    println!("{}: {:?}", label, start.elapsed());
}
```

**需要观察的现象**：在「单写者频繁写」的场景下，`ShardedLock::write()` 的吞吐显著低于 `RwLock::write()`（因为前者要锁 8 次）；而在「多读者少写者」场景下，`ShardedLock::read()` 的可扩展性更好（见综合实践）。

**预期结果**：写吞吐 `ShardedLock < RwLock`；读吞吐随读线程数增加，`ShardedLock` 下降更平缓。

**待本地验证**：具体数值取决于机器核心数与缓存行为，请以本地实测为准。

#### 4.3.5 小练习与答案

**练习 1**：`write()` 第 421-424 行在 `Poisoned` 时为什么不直接返回错误，而是取出内部 guard 继续锁剩下的 shard？

> **答案**：为了保证「成功返回时，要么 8 把锁全部拿在手里、要么一把都不拿」这一不变量。如果遇到一个中毒的 shard 就立刻返回，那么此前已经锁住的若干 shard 的 guard 就成了孤儿——它们被暂存在 shard 里，却没有任何 `ShardedLockWriteGuard` 来负责释放，导致这把锁被永久持有（死锁）。所以必须先把 8 把锁都拿全（哪怕其中有的是从 `Poisoned` 错误里「抢救」出来的 guard），再统一用大 guard 的 `Drop` 释放。

**练习 2**：为什么 `Drop` 要倒序（`.rev()`）释放 shard？

> **答案**：锁协议的惯例——「后申请的先释放」。虽然这里 8 把锁之间没有严格的层次依赖（它们都是 `RwLock<()>`，互不相干），倒序释放主要是与 `try_write` 的回滚路径（第 362 行 `shards[0..i].iter().rev()`）保持一致，形成统一的「按申请逆序释放」风格，便于推理与维护。

**练习 3**：写操作要锁 8 个 shard。如果在锁到第 5 个时，第 5 个 shard 上有大量读者正在持有读锁，会发生什么？

> **答案**：`shard.lock.write()` 会阻塞，直到那批读者全部释放。这正是写操作「慢」且「延迟不可预测」的原因之一：写者要等所有 8 个 shard 上的现存读者散去。在此期间，写者也不持有任何可以阻止新读者进入其他 shard 的东西——但因为 `RwLock` 通常会让写者优先或排队，新读者在写者排队的那个 shard 上也会被挡住。

---

### 4.4 poison 语义与 guard 生命周期的 transmute

#### 4.4.1 概念说明

**poison（中毒）** 是从标准库 `RwLock` 继承的语义：当一个线程在持有 **写 guard** 时 panic（且 unwind），这把锁就被标记为「中毒」。此后所有 `read()/write()` 都会返回 `Err(PoisonError)`，强制调用者显式处理这种异常状态（通常意味着不变量可能被破坏）。

`ShardedLock` 的两个特别之处：

1. **读操作不会中毒**。因为读 guard 是共享的，一个读者 panic 不代表数据被写坏。
2. **判断是否中毒只查 `shards[0]`**。因为写操作会把全部 8 个 shard 都写锁住，任何一个 shard 的 panic 都会同时让 8 把锁都中毒——查第一个就够。

本讲另一个重点是写路径里那处看起来「危险」的 `mem::transmute`：把 `RwLockWriteGuard<'a, ()>` 强制改成 `RwLockWriteGuard<'static, ()>`。把它说成 `'static` 是为了让所有 8 个 guard 能存进同一个 `UnsafeCell<Option<RwLockWriteGuard<'static, ()>>>` 字段（否则各 guard 的生命周期与各自 shard 的借用绑定，类型不统一）。这个「谎报生命周期」为何安全，见 4.4.3 的安全性论证。

#### 4.4.2 核心流程

中毒的传播路径：

```
线程持 write guard 时 panic
   │
   ├─ unwind 开始
   ├─ ShardedLockWriteGuard::drop 在 unwind 中被调用
   │     └─ 倒序 take 并 drop 8 个 RwLockWriteGuard
   │           └─ 每个 RwLockWriteGuard 在 panic 期间 drop → 对应 RwLock 被标记 poison
   └─ 此后 shards[0].lock.is_poisoned() == true
```

guard 生命周期的「名义 `'static`」与「实际 `'a`」：

```
write(&self) -> LockResult<ShardedLockWriteGuard<'a, T>>
   │  （'a 是 &self 的借用周期）
   ├─ 每个 guard 名义上被 transmute 成 'static，存进 shard.write_guard
   └─ ShardedLockWriteGuard<'a> { lock: &'a ShardedLock<T> }
         └─ Drop 在 'a 内执行 → take 出名义 'static 的 guard → drop
               （guard 实际存活时间 ≤ 'a，并未真正越界）
```

#### 4.4.3 源码精读

`is_poisoned()` 只查第一个 shard：

[src/sync/sharded_lock.rs:173-175](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/sharded_lock.rs#L173-L175) —— `self.shards[0].lock.is_poisoned()`。文档（[第 52-56 行](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/sharded_lock.rs#L52-L56)）明确说明：只有写操作 panic 才会中毒，读操作 panic 不会。

`Shard.write_guard` 字段（生命周期设为 `'static` 的存储槽）：

[src/sync/sharded_lock.rs:29-34](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/sharded_lock.rs#L29-L34) —— `UnsafeCell<Option<RwLockWriteGuard<'static, ()>>>`。`UnsafeCell` 是必需的，因为 `write(&self)` 拿的是 `&self`，却要往这个字段里写 guard（内部可变性）。

写路径里的 `transmute`：

[src/sync/sharded_lock.rs:427-433](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/sharded_lock.rs#L427-L433) —— 把 `RwLockWriteGuard<'_, ()>` 经 `mem::transmute` 改写成 `RwLockWriteGuard<'static, ()>` 后存入字段。

**安全性论证（为什么这个 transmute 是 sound）**：

- 真正持有这些 guard 的是返回给调用者的 `ShardedLockWriteGuard<'a, T>`，它内部只保存 `lock: &'a ShardedLock<T>`。
- 这个 `'a` 借用在整个大 guard 存活期间一直有效，意味着 `ShardedLock`（及其 `shards`）不会被销毁、也不会被可变借用。
- 大 guard 的 `Drop`（[第 529-540 行](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/sharded_lock.rs#L529-L540)）在 `'a` 结束之前运行，把暂存的 guard 全部 `take()` 并 drop。
- 因此，名义上 `'static` 的 guard **实际从未活过 `'a`**——`'static` 只是塞进统一字段类型的一个「类型谎言」，真实的生命周期由大 guard 的 `Drop` 兜底。这正是典型的「用 `Drop` 与借用锚点（`&'a ShardedLock`）来履行 unsafe 契约」的手法。

`write_guard` 字段为 `Option` 的原因：未写锁时它是 `None`，写锁时才 `Some(guard)`；`take()` 同时完成「取出」与「清空」两件事，避免重复释放。

#### 4.4.4 代码实践

**实践目标**：复现 poison 行为，验证「写 panic 中毒、读 panic 不中毒」。

**操作步骤**：直接运行项目已有的测试，它们正是为此设计的：

1. 读 [tests/sharded_lock.rs:51-61](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/sharded_lock.rs#L51-L61)（`arc_poison_wr`）：子线程持写 guard 时 panic，主线程随后 `read()` 应得 `Err`。
2. 读 [tests/sharded_lock.rs:77-88](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/sharded_lock.rs#L77-L88)（`arc_no_poison_rr`）：子线程持读 guard 时 panic，主线程随后 `read()` 仍 `Ok`。
3. 在 `crossbeam-utils` 目录下运行：

```bash
cargo test --test sharded_lock arc_poison
cargo test --test sharded_lock arc_no_poison
```

**需要观察的现象**：`arc_poison_wr` 与 `arc_poison_ww` 通过（写 panic 后读写都报错）；`arc_no_poison_rr` 与 `arc_no_poison_sl` 通过（读 panic 后仍可读写）。

**预期结果**：两组测试均 pass，印证 4.4.1 的两条规则。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `is_poisoned()` 只查 `shards[0]`，而不需要遍历 8 个 shard？

> **答案**：因为写操作会把全部 8 个 shard 都写锁住，写 guard drop 时（即便在 unwind 中）会逐个 drop 这 8 把 `RwLock` 的写 guard。在 panic 期间 drop 的 `RwLockWriteGuard` 会让对应 `RwLock` 进入中毒态。由于一次写操作触发的 drop 要么全部在 unwind 中（中毒），要么全部不在（不中毒），8 个 shard 的中毒状态必然一致，查任意一个（取 `shards[0]` 最简单）即可代表整体。

**练习 2**：如果有人误删了 `ShardedLockWriteGuard::drop` 的实现，会发生什么？编译能通过吗？

> **答案**：编译大概率**会失败**。因为 shard 的 `write_guard` 字段里被塞入了 `RwLockWriteGuard<'static, ()>`，这些 guard 的 drop 需要在 `ShardedLock` 本身被销毁之前完成；但 `'static` 让编译器认为它们可以活到程序结束。实际上由于使用了 `mem::transmute` 和 `UnsafeCell`，借用检查器看不到这层关系，但 guard 没有被正确释放会导致「写锁被永久持有」的死锁。设计上，`Drop` 实现是不可省略的安全契约——这正是 4.4.3 强调的「用 Drop 履行 unsafe 契约」。

**练习 3**：读 guard panic 会中毒吗？为什么 `arc_no_poison_rr` 里读者 panic 后，主线程还能正常读到值 1？

> **答案**：不会中毒。`RwLock` 的中毒机制只针对 **写 guard** 在持有期间 panic 的情形；读 guard 是共享的，它的 panic 不影响数据完整性（读者本来就不该修改数据）。`ShardedLock` 完全继承这一行为——读 guard drop 时即便在 unwind 中，也只是释放共享锁，不会标记中毒。所以主线程的 `read()` 仍能成功。

---

## 5. 综合实践

**任务**：在「多读少写」场景下，对比 `ShardedLock` 与 `std::sync::RwLock` 的读吞吐随读线程数增长的趋势，画出折线并解释 `ShardedLock` 的优势来源。

### 5.1 实践目标

把本讲的知识串起来：分片降低 cache line 争用 → 多读者并发时 `ShardedLock` 更具可扩展性。你需要用数据看到这条曲线，并用 4.1-4.3 的原理解释它。

### 5.2 操作步骤

1. 新建一个 binary crate，依赖 `crossbeam_utils`。
2. 编写基准程序（示例代码，非项目原有）：

```rust
// 示例代码：sharded_vs_rwlock.rs
use crossbeam_utils::sync::ShardedLock;
use std::sync::{Arc, RwLock};
use std::thread;

trait Readable { fn r(&self); }
impl Readable for RwLock<u64>       { fn r(&self) { let _g = self.read().unwrap(); } }
impl Readable for ShardedLock<u64>  { fn r(&self) { let _g = self.read().unwrap(); } }

fn bench<R: Readable + Send + Sync + 'static>(lock: R, readers: usize) -> u64 {
    let arc = Arc::new(lock);
    let total = Arc::new(std::sync::atomic::AtomicU64::new(0));
    let mut handles = Vec::new();
    for _ in 0..readers {
        let arc = arc.clone();
        let total = total.clone();
        handles.push(thread::spawn(move || {
            let iters = 200_000u64;
            for _ in 0..iters { arc.r(); }
            total.fetch_add(iters, std::sync::atomic::Ordering::Relaxed);
        }));
    }
    let start = std::time::Instant::now();
    for h in handles { h.join().unwrap(); }
    let elapsed = start.elapsed().as_micros() as u64;
    // 吞吐 = 总读次数 / 耗时（微秒）
    total.load(std::sync::atomic::Ordering::Relaxed) / elapsed.max(1)
}
```

3. 在 `main` 中分别对 `readers ∈ {1, 2, 4, 8, 16, 32}` 调用 `bench(RwLock::new(0), n)` 与 `bench(ShardedLock::new(0), n)`，打印结果。
4. 把结果画成折线图（横轴读线程数，纵轴读吞吐）。

### 5.3 需要观察的现象

- 线程数为 1 时，两者接近（`ShardedLock` 略慢，因为它多一次 TLS 读和按位与）。
- 随线程数增加，`RwLock` 的吞吐较早出现下降或停滞（cache line 热点）。
- `ShardedLock` 的吞吐曲线随线程数增长更平缓，甚至继续上升（分摊到 8 条 cache line）。

### 5.4 预期结果

在多核机器上，读线程数 ≥ 核心数后，`ShardedLock` 的读吞吐应明显高于 `RwLock`，且差距随线程数增加而扩大。优势来源正是 4.1-4.2 描述的：

- 读只锁一个 shard（一条 cache line），不同线程大概率落不同 shard；
- `current_index` 走 TLS、注册表锁不在热路径；
- `CachePadded` 保证 8 把锁不互相 false sharing。

### 5.5 待本地验证

实际曲线取决于 CPU 架构、核心数、缓存层级、`RwLock` 的实现是否公平等。请以本地实测数据为准；若 `ShardedLock` 优势不明显，可检查读线程是否真的被调度到不同物理核（例如在容器内可能受 cgroup 限制）。

---

## 6. 本讲小结

- `ShardedLock<T>` 内部是 **8 个 `CachePadded<Shard>`** 加一个 `UnsafeCell<T>`；`NUM_SHARDS = 8` 且**必须是 2 的幂**，因为读路径用 `index & (len-1)` 当掩码取模。
- **读路径**用 `current_index()`（一次 TLS 读）选定一个 shard，只锁那一个 shard 的 `RwLock<()>`；全局线程索引注册表的锁**不在读热路径上**，只在首次访问与线程退出时上。
- **写路径**必须**逐个写锁全部 8 个 shard**，并把每个 guard `transmute` 成 `'static` 后暂存进 shard；写慢 = 8 次阻塞写锁 + 与所有现存读者竞争。
- `try_write()` 半途失败时**倒序回滚**已锁的 shard；大 guard 的 `Drop` 也**倒序**释放 8 把锁。
- **poison 只在写 panic 时触发**，读 panic 不中毒；`is_poisoned()` 只查 `shards[0]`，因为写操作让 8 个 shard 的中毒状态始终一致。
- 写 guard 的 `'static` transmute 是一个**类型谎言**：真实生命周期由 `ShardedLockWriteGuard<'a>` 持有的 `&'a ShardedLock` 与其 `Drop` 兜底，保证 guard 实际不越界。

## 7. 下一步学习建议

- **接 u3-l4 OnceLock**：本讲读到的 `static OnceLock<Mutex<ThreadIndices>>` 是惰性建立线程索引注册表的关键，下一讲会拆解 `OnceLock` 的「快路径 `is_completed` + 慢路径 `call_once`」两阶段初始化，并解释 `get_unchecked` 的安全性。
- **回看 u2-l6 CachePadded**：若你对 `repr(align(128))` 如何撑满 cache line 还有疑问，可重读 u2-l6，并思考「若去掉 `CachePadded`，8 把 `RwLock<()>` 是否会挤在同一条 cache line」。
- **延伸阅读**：对照标准库 [`std::sync::RwLock`](https://doc.rust-lang.org/std/sync/struct.RwLock.html) 的源码与 poison 语义，体会 `ShardedLock` 如何在「等价语义」下用「分片」换取读可扩展性。
- **后续 thread::scope（u4-l1）**：作用域线程会用到 `WaitGroup`（u3-l2）来 fork-join；本讲建立的「`Arc` 共享 + 多线程并发读」直觉会直接复用。
