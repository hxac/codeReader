# CachePadded 与伪共享

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚「伪共享（false sharing）」是怎么产生的：明明两个线程在写**不同的变量**，性能却因为它们住在**同一条缓存行**上而一起崩塌。
- 读懂 `crossbeam_utils::CachePadded` 的全部源码，理解它如何用 `repr(align(N))` 把一个值按缓存行对齐、用尾部填充（padding）把邻居挤到别的缓存行上去。
- 区分不同 CPU 架构下「缓存行」的假定长度（x86-64/aarch64 取 128、arm 取 32、s390x 取 256、其余默认 64），并理解为什么现代 Intel 上要「悲观地」按 128 字节算。
- 学会判断**哪些并发字段需要 padding**（被多核频繁原子改写的「热点」字段）、哪些不需要（只读字段、线程私有字段），并明白 padding 带来的内存与缓存占用代价。
- 自己动手写一个微基准，量化相邻 `AtomicUsize` 在加与不加 `CachePadded` 时的吞吐差距。

## 2. 前置知识

本讲承接 u1-l3（门面重导出）与 u2-l1（Backoff 退避），是 crossbeam-utils 并发原语的第二站。你需要先有下面这些直觉（不要求精通）：

- **缓存与缓存行（cache line）**。CPU 访问内存时并不是按字节搬，而是按「缓存行」整条搬，一条通常是 64 字节。一条缓存行就是 CPU 缓存里最小的加载/失效单位。
- **缓存一致性协议（如 MESI）**。多核各有自己的 L1/L2 缓存。当某个核**写**了一条缓存行里的任意一个字节，硬件就会把这条缓存行在**其它所有核**的副本标记为「失效（invalid）」。失效之后别的核再读，就得重新从内存或别的核搬一遍——这叫缓存行在核之间「弹射」。
- **原子写也是写**。`AtomicUsize` 的 `fetch_add`、`store` 等操作，底层都会写内存，因此同样会让所在缓存行失效。本讲要解决的问题就发生在原子字段上。
- **导入路径**。前置讲义 u1-l3 讲过：主 crate 把 `crossbeam-utils` 的 `CachePadded` 装进 `crossbeam::utils` 模块重导出；所以本讲里的 `crossbeam_utils::CachePadded` 与 `crossbeam::utils::CachePadded` 指向同一个类型。u2-l1 的 `Backoff` 也走同一个 `utils` 模块。

## 3. 本讲源码地图

本讲聚焦一个文件，辅以测试与两处真实使用点：

| 文件 | 作用 |
| --- | --- |
| [crossbeam-utils/src/cache_padded.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/cache_padded.rs) | `CachePadded` 的全部实现，只有 220 行，是本讲主角。 |
| [crossbeam-utils/src/lib.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs) | 在这里 `pub use crate::cache_padded::CachePadded;` 把它公开为 `crossbeam_utils::CachePadded`。 |
| [crossbeam-utils/tests/cache_padded.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/cache_padded.rs) | 单元测试，验证对齐距离、不同尺寸下的行为与 `Drop`/`Clone` 是否正确转发。 |
| [crossbeam-queue/src/array_queue.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs) | 真实使用样例：队列的 `head` 与 `tail` 两个原子索引分别包进 `CachePadded`，避免 push/pop 线程互相踢缓存。 |
| [crossbeam-deque/src/deque.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs) | 真实使用样例：工作窃取队列把整个 `Inner`（含 `front`/`back`）包进 `CachePadded`，分摊给所有者线程与窃取线程。 |

## 4. 核心概念与源码讲解

### 4.1 伪共享：当「不相关的变量」住进同一条缓存行

#### 4.1.1 概念说明

先看一个会让人困惑的性能现象。假设有两个线程，线程 A 疯狂自增变量 `x`，线程 B 疯狂自增变量 `y`，二者**逻辑上毫无关系**。你会本能地认为「它们各改各的，互不干扰」。但在多核机器上，实测吞吐可能比单线程还差——元凶就是**伪共享（false sharing）**。

关键在于：CPU 缓存的最小单位是**缓存行**（一般 64 字节），而不是单个变量。如果 `x` 和 `y` 离得很近、落进了**同一条** 64 字节的缓存行，那么：

- 线程 A 所在的核写 `x`，会让这条缓存行在核 B 的 L1 缓存里**失效**；
- 线程 B 所在的核写 `y`，又会让这条缓存行在核 A 的 L1 缓存里**失效**；
- 于是这条缓存行在两个核之间像乒乓球一样来回弹射（cache line ping-pong），每次都要重新从内存或对方缓存搬数据。

这就叫伪共享：变量本身没有共享语义，却因为「住同一间房」被迫共享了缓存失效的代价。

要区分两个概念：

- **真共享（true sharing）**：多个线程确实在读写**同一个**变量（例如同一个 `AtomicUsize`）。这种失效是「应得的」，因为数据本来就需要同步。
- **伪共享（false sharing）**：多个线程写的是**不同**变量，只是恰好被塞进同一条缓存行。这种失效是「冤枉的」，完全可以消除。

解决办法叫**缓存行隔离（cache line isolation）**：给热点变量**填充（padding）**出足够多的空字节，让它独占一条缓存行，邻居被挤到别的行去。`CachePadded` 就是 crossbeam 提供的、做这件事的封装类型。

> 一句话直觉：**伪共享 = 不同核写不同变量，却因为它们共处一条缓存行而互相把对方的缓存踢失效。** 解药 = 给热点变量「单间」，让它独占一条缓存行。

#### 4.1.2 核心流程

伪共享的「乒乓」过程可以画成下面这样（一条缓存行 = 64 字节，`x` 和 `y` 各 8 字节，紧挨着）：

```text
初始：缓存行 [ .... x (8B) | y (8B) .... ] 同时存在于核0、核1 的 L1
                                  核0 线程写 x        核1 线程写 y
                                         │                   │
  1. 核0 写 x            → 整行在核1 失效        （核1 的 y 副本作废）
  2. 核1 要写 y，先得把整行重新搬回来 → 核0 失效 （核0 的 x 副本作废）
  3. 核0 要写 x，又得搬回来 → 核1 失效 ……       无限乒乓
```

加上 `CachePadded` 之后，`x` 被撑成 64/128 字节、按缓存行起始对齐，`y` 被挤到下一条缓存行：

```text
加 padding：
  缓存行 A: [ pad | x | pad ............ ]   ← 核0 只碰这条
  缓存行 B: [ pad | y | pad ............ ]   ← 核1 只碰这条
  → 两条行互不失效，乒乓消失。
```

代价是**内存浪费**：本来 8 字节的变量现在占了一整条缓存行（64~128 字节），而且**缓存总容量**被更快吃掉。所以 padding 只该加在「被多核高频写」的热点上，不能到处加。

#### 4.1.3 源码精读

`CachePadded` 的官方文档注释开篇就把伪共享的成因讲得非常清楚——这是理解整个类型存在的理由：

> 更新一个原子值会让它所在的**整条缓存行**失效，使其它核下次访问同一条缓存行时变慢。用 `CachePadded` 来保证更新一块数据不会让**其它**缓存数据失效。
>
> [crossbeam-utils/src/cache_padded.rs:6-12](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/cache_padded.rs#L6-L12)（中文意译，建议对照原文）

这段注释对应了 4.1.1 里讲的「写一个字节 → 整行失效」机制。文档还直接给出了一个最典型的应用场景：并发队列的 `head`/`tail` 索引应当分属不同缓存行，这样「push 的线程（写 tail）」与「pop 的线程（写 head）」就不会互相踢缓存：

[crossbeam-utils/src/cache_padded.rs:50-63](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/cache_padded.rs#L50-L63)（文档给出的 `Queue` 示例：`head` 与 `tail` 都用 `CachePadded<AtomicUsize>`）

这并非纸上谈兵。crossbeam 自己的 `ArrayQueue` 就是这么做的——`head`（pop 端）与 `tail`（push 端）两个原子索引分别包进 `CachePadded`：

[crossbeam-queue/src/array_queue.rs:52-74](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L52-L74)（`ArrayQueue` 结构体：第 59 行 `head: CachePadded<AtomicUsize>`，第 67 行 `tail: CachePadded<AtomicUsize>`）

把 `head` 与 `tail` 分到不同缓存行后，生产者线程疯狂 `push`（CAS `tail`）与消费者线程疯狂 `pop`（CAS `head`）就再也不会因为伪共享而互相拖慢了。这正是 u4-l1（ArrayQueue）会反复用到的前提。

工作窃取队列 `crossbeam-deque` 走得更进一步——它把**整个** `Inner`（包含 `front`、`back` 两个原子索引）整体塞进 `CachePadded`，让队列的所有者线程与窃取线程在缓存行层面也尽量分离：

[crossbeam-deque/src/deque.rs:196-209](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L196-L209)（`Worker` 持有 `inner: Arc<CachePadded<Inner<T>>>`，第 199 行）

可以看到，`CachePadded` 是 crossbeam 全家桶里**用得最广**的并发原语之一——只要一个数据结构有「被不同核分别原子改写的字段」，几乎都会用它。

#### 4.1.4 代码实践

下面这个微基准最能体现伪共享的杀伤力。我们定义两组计数器，一组「裸」排在一起，另一组各自用 `CachePadded` 隔离，然后让两个线程分别狂自增各自那一个计数器，比较耗时。

> 这是**示例代码**，需要你自己在本地新建一个 crate（或加到现有 crate 的 `benches/` 下，配合 `criterion`）来运行。下面给出可直接编译运行的最小版本（不依赖 criterion，用 `std::time`）。

1. **实践目标**：亲眼看到「两个线程写不同变量」时，伪共享会让吞吐明显下降，而 `CachePadded` 能恢复它。

2. **操作步骤**：

```rust
// 示例代码：在 crossbeam-utils 目录外的任意二进制 crate 里运行
// Cargo.toml 需加：crossbeam-utils = "0.8"
use crossbeam_utils::CachePadded;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::Instant;

// 两组挨在一起的计数器（会发生伪共享）
struct Counters {
    a: AtomicUsize,
    b: AtomicUsize,
}

// 用 CachePadded 隔离后的版本
struct PaddedCounters {
    a: CachePadded<AtomicUsize>,
    b: CachePadded<AtomicUsize>,
}

const ITERS: usize = 50_000_000;

fn main() {
    // —— 不加 padding ——
    let shared = Arc::new(Counters { a: 0.into(), b: 0.into() });
    let (s1, s2) = (Arc::clone(&shared), Arc::clone(&shared));
    let t0 = Instant::now();
    let h1 = thread::spawn(move || { for _ in 0..ITERS { s1.a.fetch_add(1, Ordering::Relaxed); } });
    let h2 = thread::spawn(move || { for _ in 0..ITERS { s2.b.fetch_add(1, Ordering::Relaxed); } });
    h1.join().unwrap(); h2.join().unwrap();
    let unpadded = t0.elapsed();

    // —— 加 padding ——
    let shared = Arc::new(PaddedCounters { a: CachePadded::new(0.into()), b: CachePadded::new(0.into()) });
    let (s1, s2) = (Arc::clone(&shared), Arc::clone(&shared));
    let t0 = Instant::now();
    let h1 = thread::spawn(move || { for _ in 0..ITERS { s1.a.fetch_add(1, Ordering::Relaxed); } });
    let h2 = thread::spawn(move || { for _ in 0..ITERS { s2.b.fetch_add(1, Ordering::Relaxed); } });
    h1.join().unwrap(); h2.join().unwrap();
    let padded = t0.elapsed();

    println!("不加 padding : {:?}", unpadded);
    println!("加了 padding : {:?}", padded);
}
```

3. **需要观察的现象**：用 `Relaxed` 内存序是为了排除 acquire/release 的干扰，把差距完全归因于缓存失效。注意 `Counters` 里 `a` 与 `b` 是相邻字段，几乎一定会落进同一条缓存行。

4. **预期结果**：在典型的多核 x86 机器上，「不加 padding」的版本通常**慢数倍**（差距随核心争用程度变大）。如果差距不明显，可能是编译器/分配器恰好把结构体对齐到了边界——可以把 `AtomicUsize` 换成结构体里更靠中间的位置再试。**待本地验证**具体倍数。

5. **进阶观察**：把线程数从 2 提到 4（A/B 各两个线程写同一变量），「不加 padding」版本会因为「真共享 + 伪共享」叠加而变得更慢，而「加了 padding」版本受影响较小。

#### 4.1.5 小练习与答案

**练习 1**：如果把上面例子里的两个线程改成**都只写 `a`、不写 `b`**，padding 还有用吗？为什么？

> **答案**：基本没用。此时两个线程写的是**同一个**变量 `a`，属于「真共享」，缓存行失效本就无法避免；`CachePadded` 只能解决「不同变量住同一行」的伪共享，救不了真共享。padding 的收益恰恰来自把**逻辑无关**的写者分开。

**练习 2**：下面三个字段，哪些**值得**用 `CachePadded` 包？（a）一个只在单线程内读写的计数器；（b）一个被多个工作线程各自 CAS 推进的 `tail` 索引；（c）一个只在程序启动时写一次、之后只读的全局配置。

> **答案**：只有（b）值得。（a）是线程私有，从不跨核失效，padding 纯属浪费；（c）启动后只读，读不会让缓存行失效（只有写才会），padding 也没意义；（b）被多核高频原子改写，是典型热点，值得隔离。

### 4.2 按缓存行对齐：repr(align) 与架构相关的对齐值

#### 4.2.1 概念说明

知道了「要给热点变量单间」，下一个问题是：**怎么让一个变量独占一条缓存行？** 这需要两件事一起做：

1. **对齐（alignment）**：让变量的**起始地址**落在缓存行的起点（即地址是 64/128 的整数倍）。
2. **填充（padding）**：让变量的**总大小**也是 64/128 的整数倍，这样紧跟在它后面的邻居才会从下一条缓存行开始。

幸运的是，Rust 的 `#[repr(align(N))]` 属性刚好能**同时**满足这两点：编译器会保证这种类型的对齐为 `N`，并且——关键细节——**对齐为 N 的类型，其大小也一定是 N 的倍数**（否则数组里相邻元素就无法都满足 N 对齐）。所以只要把对齐设成缓存行长度，Rust 会自动把尾部填满，padding 就到手了。

`CachePadded` 的全部魔法，就是一组针对不同 CPU 架构选择不同 `N` 的 `#[repr(align(N))]`。

那 `N` 该取多少？它应当等于目标机器的**缓存行长度**。但不同架构的缓存行长度不一样：

- x86（32 位）、wasm、riscv、sparc64：64 字节；
- x86-64、aarch64、powerpc64：crossbeam **故意取 128**（下面解释）；
- arm、mips、mips64、sparc、hexagon：32 字节；
- m68k：16 字节；
- s390x：256 字节。

为什么 x86-64 不取「真实」的 64 而取 128？源码注释里有个重要细节：从 Intel Sandy Bridge 架构起，**空间预取器（spatial prefetcher）** 会**成对地**一次抓取两条相邻的 64 字节缓存行（即 128 字节的块）。所以即使你把变量按 64 对齐，硬件仍可能把「相邻 64 字节」当作一个预取单位来回搬动，导致 64 对齐**不足以**消除乒乓。于是 crossbeam 悲观地按 128 处理。

> 一句话直觉：**`#[repr(align(N))]` 既保证起始对齐为 N，又顺带把大小撑成 N 的倍数，于是变量两头都被填满，整条缓存行归它独占。** `N` 取目标架构的缓存行长度（现代 x86-64 因空间预取器取 128）。

#### 4.2.2 核心流程

`CachePadded<T>` 的「撑大」可以用两句话概括：

- **大小**：能容纳 `T` 的、最小的 N 的整数倍，即 \(\lceil \text{size}(T) / N \rceil \times N\)。
- **对齐**：`N` 与 `T` 自身对齐取较大者，即 \(\max(N,\, \text{align}(T))\)。

选择 `N` 的判定是一个 **`#[cfg_attr(...)]` 编译期分派**：编译器根据 `target_arch` 选中互斥的某一条 `repr(align(...))`。五档取值互不重叠，逻辑如下：

```text
target_arch 是 x86_64 / aarch64 / arm64ec / powerpc64 ?  → align(128)
否则 是 arm / mips* / sparc / hexagon            ?        → align(32)
否则 是 m68k                                      ?        → align(16)
否则 是 s390x                                     ?        → align(256)
否则（x86/wasm/riscv/其余）                                → align(64)
```

最后一档用的是 `not(any(...))`（「以上都不是」），所以即便未来出现新架构，也会安全地落到默认的 64 字节对齐，而不是编译失败。

#### 4.2.3 源码精读

整个类型最核心的代码就是这一大段 `#[cfg_attr]`。先看「取 128」这档——它专门点名了 Intel 的空间预取器，并附了 Intel 优化手册和 Facebook folly 库的引用作为依据：

[crossbeam-utils/src/cache_padded.rs:64-90](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/cache_padded.rs#L64-L90)（`#[derive(...)]` 之后，第一条 `#[cfg_attr(...)]` 把 x86_64/aarch64/arm64ec/powerpc64 对齐到 128 字节；注释解释了 Sandy Bridge 空间预取器为何要 128 而非 64）

注意这条注释里专门留了「为什么 aarch64 也取 128」的理由：aarch64 的 big.LITTLE 架构是**非对称多核**，「大核」的缓存行是 128 字节。

接下来是「取 32」和「取 16」两档，分别覆盖 arm/mips 等小缓存行架构和老式 m68k：

[crossbeam-utils/src/cache_padded.rs:100-116](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/cache_padded.rs#L100-L116)（arm/mips/mips32r6/mips64/mips64r6/sparc/hexagon 取 `align(32)`；m68k 取 `align(16)`，每档都附 Linux 内核 `arch/*/include/asm/cache.h` 的引用）

[crossbeam-utils/src/cache_padded.rs:117-122](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/cache_padded.rs#L117-L122)（s390x 取 `align(256)`，这是所有架构里最大的缓存行）

最后是「兜底取 64」，用 `not(any(...))` 列出所有上面已处理过的架构、对其取反：

[crossbeam-utils/src/cache_padded.rs:123-149](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/cache_padded.rs#L123-L149)（x86/wasm/riscv/sparc64 及一切未列出架构落到 `align(64)`）

五条 `cfg_attr` 选完 `N` 之后，结构体本身简单得令人安心——它只有一个字段：

[crossbeam-utils/src/cache_padded.rs:150-152](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/cache_padded.rs#L150-L152)（`pub struct CachePadded<T> { value: T }`——所有「撑大」都来自外层的 `repr(align(N))`，字段本身不掺水）

也就是说，`CachePadded<T>` 在**逻辑上**就是 `T`，所有的 padding 字节都由编译器在布局时隐式插入，你看不到任何 `pad: [u8; N]` 字段。这让它可以透明地 `Deref` 到 `T`（见 4.3）。

文档还贴心地提醒：`N` 只是个「合理猜测」，**并不保证**等于程序实际运行机器的缓存行长度——它是**编译期**按目标架构选定的，而同一份二进制跑到缓存行不同的机器上时不会自适应。所以对于 x86-64，干脆按「最坏情况」的 128 来算：

[crossbeam-utils/src/cache_padded.rs:24-27](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/cache_padded.rs#L24-L27)（文档说明 N 只是合理猜测；现代 Intel 因空间预取器成对抓取 64 字节行，故悲观地按 128 算）

#### 4.2.4 代码实践

让我们用 `std::mem` 把「撑大」的效果直接量出来，验证 4.2.2 里的大小/对齐公式。

1. **实践目标**：确认 `CachePadded<T>` 的大小是「能容纳 T 的最小 N 倍数」，对齐是 N（当 T 自身对齐 ≤ N 时）。

2. **操作步骤**（示例代码，可在任意 crate 的 `tests/` 或 `fn main` 里跑）：

```rust
// 示例代码
use crossbeam_utils::CachePadded;
use std::mem;

fn main() {
    // align_of::<CachePadded<()>> 就是当前架构选定的 N
    let n = mem::align_of::<CachePadded<()>>();
    println!("当前架构 N = {}", n);          // x86-64 上是 128

    for size in [1usize, 8, 9, 64, 65, 128] {
        // 用数组造出任意 size 字节的类型不太方便，这里用 [u64; k] 近似观察
    }
    // 直接打印几个典型尺寸：
    println!("size CachePadded<u8>     = {}", mem::size_of::<CachePadded<u8>>());
    println!("size CachePadded<u64>    = {}", mem::size_of::<CachePadded<u64>>());
    println!("size CachePadded<(u64,u64)> = {}", mem::size_of::<CachePadded<(u64, u64)>>());
    println!("size CachePadded<[u64;9]> = {}", mem::size_of::<CachePadded<[u64; 9]>>());
    println!("align CachePadded<u64>   = {}", mem::align_of::<CachePadded<u64>>());
}
```

3. **需要观察的现象**：在 x86-64 上 `N=128`。`CachePadded<u8>`（T=1 字节）应被撑成 128 字节；`CachePadded<[u64;9]>`（T=72 字节）应被撑成 256 字节（因为 72 超过一个 128，要进位到两个 128）。

4. **预期结果**：

| 类型 | `size_of(T)` | `size_of(CachePadded<T>)`（N=128 时） |
| --- | --- | --- |
| `u8` | 1 | 128 |
| `u64` | 8 | 128 |
| `(u64, u64)` | 16 | 128 |
| `[u64; 9]` | 72 | 256 |

即严格满足 \(\text{size} = \lceil \text{size}(T) / N \rceil \times N\)。**待本地验证**你在自己机器上得到的 `N`。

5. **测试佐证**：crossbeam 自带的测试 `distance` 就是用同样的思路——取两个相邻 `CachePadded<u8>` 的地址，断言它们的差正好等于对齐值，且对齐 ≥ 32：

[crossbeam-utils/tests/cache_padded.rs:24-32](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/cache_padded.rs#L24-L32)（测试 `distance`：`a.add(align) == b`，说明两个相邻元素正好相隔一条缓存行）

#### 4.2.5 小练习与答案

**练习 1**：在 x86-64 上（N=128），`CachePadded<[u8; 200]>` 的大小是多少？

> **答案**：`[u8; 200]` 是 200 字节。 \(\lceil 200 / 128 \rceil = 2\)，所以大小是 \(2 \times 128 = 256\) 字节。验证思路：`align(128)` 要求大小是 128 的倍数，200 不是，需进位到 256。

**练习 2**：为什么源码里五档 `repr(align)` 用的是互斥的 `cfg_attr` 链，而不是写一个 `const N: usize = if ... { 128 } else { 64 };` 再 `repr(align(N))`？

> **答案**：因为 `#[repr(align(...))]` 的参数必须是**字面整数常量**，不能是任意的 `const` 表达式或 `cfg` 运行期求值。所以只能在编译期用多条 `#[cfg_attr(target_arch = "...", repr(align(N)))]` 互斥分派。这也是为什么最后一档要用 `not(any(...))` 兜底，而不是 `else`。

### 4.3 Deref 透明访问与性能取舍

#### 4.3.1 概念说明

`CachePadded<T>` 把 `T` 撑大了一两百倍，那使用起来会不会很麻烦？要不要每次都 `.into_inner()` 再操作？答案是不用——它实现了 `Deref` 和 `DerefMut`，目标类型就是 `T`，所以你可以**把它当成 `T` 透明地用**。比如 `CachePadded<AtomicUsize>` 可以直接 `.fetch_add(...)`，因为 `Deref` 会自动把 `&CachePadded<AtomicUsize>` 解引用成 `&AtomicUsize`。

它的 trait 实现相当完整，几乎是对 `T` 的「忠实转发」：

- **构造/析构**：`new(T)` 包进去、`into_inner()` 拆出来、`From<T>` 转换；
- **访问**：`Deref<Target=T>` + `DerefMut`，透明读写内部值；
- **派生**：`#[derive(Clone, Copy, Default, Hash, PartialEq, Eq)]`——只要 `T` 实现了，`CachePadded<T>` 也自动实现；
- **`Send`/`Sync`**：用 `unsafe impl<T: Send> Send` / `<T: Sync> Sync` 透传给 `T`（因为只是包了一层，没引入新的线程安全隐患）；
- **格式化**：`Debug` 打成 `CachePadded { value: ... }`，`Display` 直接转发给 `T`。

理解了透明访问，就要正视**性能取舍**。padding 不是免费午餐：

1. **内存占用暴增**：一个 8 字节的原子变量变成 64~256 字节。对一两个字段无所谓，对大量小元素（比如队列里每个槽位都包一层）就会显著膨胀。
2. **缓存容量被更快吃掉**：每个热点变量独占一条缓存行，意味着同样大小的 L1 缓存能装下的「热点」更少，缓存命中率反而可能下降。
3. **只对「写」热点有意义**：如 4.1.5 练习所示，只读字段、线程私有字段加 padding 纯属浪费。

所以使用准则很明确：**只给「被多个核频繁原子改写」的字段加 padding**，而且通常是数据结构里少数几个「控制字段」（索引、头尾指针、锁），而不是每个数据元素。

> 一句话直觉：**`CachePadded<T>` 在逻辑上就是 `T`（靠 `Deref` 透明访问），但物理上独占一条缓存行；代价是内存膨胀，所以只该用在多核高频写的热点字段上。**

#### 4.3.2 核心流程

把 `CachePadded<T>` 当 `T` 用的典型流程：

```text
let c: CachePadded<AtomicUsize> = CachePadded::new(AtomicUsize::new(0));
                                    // 构造：把 T 包进单间
c.fetch_add(1, Ordering::Relaxed);
    // ↑ 自动 DerefMut → &AtomicUsize → 调 AtomicUsize::fetch_add
let v: AtomicUsize = c.into_inner();
                                    // 拆箱：取出 T，丢弃 padding
```

派生与 trait 转发的覆盖关系：

| 你想要的 | 由谁保证 |
| --- | --- |
| `CachePadded::new(x)` / `x.into()` | `new` / `From<T>` |
| 当 `T` 用（读） | `Deref<Target = T>` |
| 当 `T` 用（写） | `DerefMut` |
| `c.clone()`（当 `T: Clone`） | `#[derive(Clone)]` |
| 跨线程共享（当 `T: Send/Sync`） | `unsafe impl Send/Sync` 透传 |
| 打印 | `Debug`（结构化）/ `Display`（转发 `T`） |

#### 4.3.3 源码精读

构造与拆箱的两个方法都很短——`new` 是 `const fn`，可以在常量上下文里用：

[crossbeam-utils/src/cache_padded.rs:157-185](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/cache_padded.rs#L157-L185)（`pub const fn new(t: T)` 与 `pub fn into_inner(self) -> T`，都只是包/拆 `value` 字段）

透明访问靠的是经典的 `Deref`/`DerefMut` 一对，目标类型直接写死为 `T`：

[crossbeam-utils/src/cache_padded.rs:187-199](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/cache_padded.rs#L187-L199)（`impl Deref { type Target = T; fn deref(&self) -> &T }` 与 `DerefMut`）

`Send`/`Sync` 的透传是 `unsafe impl`——因为类型里只有一个 `value: T`，没有新增任何共享状态，所以 `T: Send` 时 `CachePadded<T>` 也该 `Send`：

[crossbeam-utils/src/cache_padded.rs:154-155](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/cache_padded.rs#L154-L155)（`unsafe impl<T: Send> Send` / `unsafe impl<T: Sync> Sync`）

派生列表在结构体正上方——注意 `CachePadded<T>` 是 `Copy`（当 `T: Copy`），所以 `AtomicUsize` 这种非 `Copy` 类型会自动不满足 `Copy`，但 `Clone` 仍可用：

[crossbeam-utils/src/cache_padded.rs:64](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/cache_padded.rs#L64)（`#[derive(Clone, Copy, Default, Hash, PartialEq, Eq)]`）

格式化方面，`Debug` 会打成 `CachePadded { value: ... }`（测试里有断言），而 `Display` 直接转发给内部 `T`，便于把 `CachePadded<u64>` 当普通数字打印：

[crossbeam-utils/src/cache_padded.rs:201-219](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/cache_padded.rs#L201-L219)（`Debug` 输出 `CachePadded { value: .. }`；`Display` 转发 `T`）

`Debug` 格式可由测试佐证：

[crossbeam-utils/tests/cache_padded.rs:57-63](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/cache_padded.rs#L57-L63)（测试 `debug`：`format!("{:?}", CachePadded::new(17u64)) == "CachePadded { value: 17 }"`）

最后，`CachePadded` 在 `crossbeam-utils` 的导出位置——一个私有的 `mod cache_padded` 加一行 `pub use`：

[crossbeam-utils/src/lib.rs:89-90](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs#L89-L90)（`mod cache_padded; pub use crate::cache_padded::CachePadded;`，于是它对外就是 `crossbeam_utils::CachePadded`）

#### 4.3.4 代码实践

我们沿用 4.1.4 的双线程计数器，但这次把注意力放在「**用起来和 `T` 一样**」上，验证 `Deref` 的透明性。

1. **实践目标**：体会 `CachePadded<AtomicUsize>` 可以**直接调用** `AtomicUsize` 的方法，无需手动拆箱。

2. **操作步骤**（示例代码）：

```rust
// 示例代码
use crossbeam_utils::CachePadded;
use std::sync::atomic::{AtomicUsize, Ordering};

fn main() {
    // 包进单间
    let counter: CachePadded<AtomicUsize> = CachePadded::new(AtomicUsize::new(0));
    // 直接调用 AtomicUsize 的方法 —— Deref 自动解引用
    counter.fetch_add(41, Ordering::Relaxed);
    counter.fetch_add(1, Ordering::Relaxed);
    assert_eq!(counter.load(Ordering::Relaxed), 42);

    // 也能用 += 风格的 store / swap 等，都走 DerefMut/Deref
    let prev = counter.swap(100, Ordering::Relaxed);
    assert_eq!(prev, 42);

    // 拆箱取出原值
    let inner: AtomicUsize = counter.into_inner();
    assert_eq!(inner.load(Ordering::Relaxed), 100);

    println!("ok");
}
```

3. **需要观察的现象**：代码里**从未**出现 `.into_inner()` 之前的拆箱操作就直接调了 `fetch_add`/`load`/`swap`——说明 `Deref`/`DerefMut` 把 `CachePadded<AtomicUsize>` 透明地当成了 `AtomicUsize`。

4. **预期结果**：编译通过、断言全部成立。如果你把鼠标悬停在 IDE 里的 `counter.fetch_add` 上，会看到它解析到 `AtomicUsize::fetch_add`，证明是 `Deref` 在起作用。**待本地验证**。

5. **配合测试阅读**：建议同时打开 `crossbeam-utils/tests/cache_padded.rs` 通读一遍——它只有 113 行，覆盖了 `Default`/`store`/`distance`/`different_sizes`/`large`/`debug`/`drops`/`clone` 等场景，是理解「`CachePadded` 在各种 `T` 下的行为」的最佳速查表。

#### 4.3.5 小练习与答案

**练习 1**：`CachePadded<AtomicUsize>` 是 `Copy` 吗？为什么？

> **答案**：不是。`AtomicUsize` 没有实现 `Copy`（原子类型为了防止意外复制破坏原子语义，故意不 `Copy`）。虽然 `CachePadded` 派生了 `#[derive(Copy)]`，但 derive 只在「所有字段都 `Copy`」时才生效，`AtomicUsize` 不满足，所以 `CachePadded<AtomicUsize>` 也不 `Copy`（但 `Clone` 仍可用）。测试 `clone` 里特意写了 `#[allow(clippy::clone_on_copy)]` 是针对 `CachePadded::new(17)`（`i32: Copy`）的情形。

**练习 2**：假设你要做一个并发哈希表，每个桶里有「一个原子计数器 + 一个数据指针」。这个计数器值得用 `CachePadded` 吗？考虑两种情形：（a）每个桶一个独立计数器；（b）所有桶共用一个全局计数器。

> **答案**：（a）通常**不值得**——桶是按 key 分散访问的，不同线程大概率访问不同桶，相邻桶的计数器很少被不同核同时高频写，padding 带来的内存膨胀（桶数量可能很大）不划算。（b）**值得**——全局计数器是所有线程争用的热点，正是 4.1 里描述的典型伪共享来源，把它隔离能显著降低争用。这印证了准则：**只给「被多核高频写」的少数热点字段加 padding**。

## 5. 综合实践

把本讲三个模块串起来，设计并验证一个「并发计数器组」。

**任务背景**：你要实现一组计数器，供 4 个线程分别统计各自的事件数，最后汇总。第一版朴素实现把 4 个 `AtomicUsize` 放进同一个数组；结果发现性能不理想。请用本讲学到的知识定位并修复。

**步骤**：

1. 写一个 `struct Counters([AtomicUsize; 4])`，4 个线程各负责 `counters[i]`，每个线程自增 1e7 次，计时。
2. 用 `cargo run --release` 跑，记录总耗时（此时 4 个 `AtomicUsize` 共 32 字节，很可能挤在一条缓存行里，发生伪共享）。
3. 改写成 `struct Counters([CachePadded<AtomicUsize>; 4])`，构造时用 `CachePadded::new(...)`，自增代码**不用改**（因为 `Deref` 透明，`counters[i].fetch_add(...)` 仍然有效），再次计时。
4. 对比两次耗时，量化伪共享的代价；同时用 `mem::size_of::<Counters>()` 看内存膨胀了多少倍（预期从 32 字节涨到至少 512 字节，即 4 × 128）。
5. 进一步思考：如果把 `Counters` 从数组改成链表节点（每个节点一个计数器并分散分配），不加 `CachePadded` 是否也会没有伪共享？为什么？（提示：分配器返回的地址是否落在同一缓存行。）

**预期**：release 构建下，第 3 步应明显快于第 2 步（在 4 核以上机器上常快数倍）。这一步把「伪共享的成因」「`repr(align)` 的对齐原理」「`Deref` 的透明访问」三个模块一次性串了起来。**待本地验证**具体加速比。

## 6. 本讲小结

- **伪共享**：不同核写**不同**变量，却因为这些变量落进**同一条缓存行**而互相把对方的缓存踢失效，导致吞吐崩塌。它和「真共享」不同，是**可以**消除的。
- **解药**：把热点变量按缓存行**对齐并填充**，让它独占一条缓存行，邻居被挤到别的行去。
- **`CachePadded<T>` 的实现**：靠一组互斥的 `#[cfg_attr(target_arch=..., repr(align(N)))]` 在编译期选定 `N`——x86-64/aarch64/powerpc64 取 128（因 Intel 空间预取器成对抓取 64 字节行，悲观按 128 算），arm/mips 取 32，m68k 取 16，s390x 取 256，其余默认 64。结构体本身只有一个 `value: T` 字段，padding 全由对齐隐式产生。
- **大小/对齐公式**：大小为能容纳 `T` 的最小 N 倍数 \(\lceil \text{size}(T)/N \rceil \times N\)，对齐为 \(\max(N,\text{align}(T))\)。
- **透明使用**：通过 `Deref`/`DerefMut`，`CachePadded<T>` 可以当 `T` 直接用；`Send`/`Sync`/`Clone`/`Debug` 等都忠实转发给 `T`。
- **使用准则**：只给「被多核高频原子改写」的少数热点字段（队列头尾索引、全局计数器、锁等）加 padding；只读字段、线程私有字段、海量小元素都**不该**加，否则内存膨胀反噬缓存命中率。

## 7. 下一步学习建议

本讲讲完了一个最「纯」的并发原语——它没有任何运行时逻辑，纯靠编译期布局消除伪共享。接下来两条路：

- **横向继续 crossbeam-utils**：下一讲 **u2-l3 AtomicCell** 会用到一个更复杂的场景——`AtomicCell` 为了给「无法转成原子类型的大尺寸 `T`」提供原子操作，内部维护了一组**全局的、用 `CachePadded` 隔离的**序列锁（seq lock）。届时你会看到 `CachePadded` 在真实算法里如何配合 `Backoff`、序列锁一起工作，把「隔离热点字段」从理论变成工程实践。
- **纵向看真实数据结构**：等学到 **u4-l1 ArrayQueue** 时，回头看本讲引用的 [array_queue.rs:52-74](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L52-L74) 的 `head`/`tail`，你会更深刻地理解：为什么无锁队列的性能**强依赖**于把头尾索引分到不同缓存行——这是 push/pop 两条路径能并行的物理前提。

建议同时把 [crossbeam-utils/tests/cache_padded.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/cache_padded.rs) 通读一遍，作为本讲的「行为速查表」，并在进入 u2-l3 之前确认自己能回答：给定任意 `T`，`CachePadded<T>` 在你的目标架构上大小和对齐分别是多少。
