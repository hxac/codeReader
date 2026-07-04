# 工具模块 utils.rs 与 alloc_helper.rs

## 1. 本讲目标

本讲聚焦 crossbeam-channel 两个「不起眼但不可或缺」的工具模块：

- [`src/utils.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/utils.rs) —— 提供**非毒 `Mutex`**、**`shuffle`（洗牌）**、**`sleep_until`（睡到截止时刻）** 三个工具。
- [`src/alloc_helper.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/alloc_helper.rs) —— 提供一个最小化的 `Global` 分配器封装，处理零大小类型（ZST）与「无 provenance 指针」。

读完本讲，你应当能够：

1. 说清楚为什么通道内部的 `Mutex` 要做成「非毒」。
2. 读懂 `shuffle` 用 Xorshift 伪随机数 + Lemire 取模实现的 Fisher–Yates 洗牌，并理解它如何为 `select!` 的公平性服务。
3. 理解 `sleep_until` 用一个函数统一表达「永久阻塞 / 睡到时刻」两种语义。
4. 解释 `Global` 在 `no_std` 下的定位，以及它为什么必须处理零大小分配与 `without_provenance_mut`。

本讲是专家层的「基础设施」讲，承接 [u2-l4（阻塞与唤醒）](u2-l4-blocking-and-waking.md) 中 `SyncWaker` 用到的 `Mutex`，并为 [u3-l1（select 算法）](u3-l1-select-algorithm.md) 中用到的 `shuffle` / `sleep_until` 提供实现细节。

---

## 2. 前置知识

### 2.1 Mutex 与「毒化」（poison）

Rust 标准库的 `std::sync::Mutex` 有一套「毒化」机制：**当一个线程持有锁时 panic，这把锁就会被标记为「有毒」**。此后任何其他线程再 `lock()` 都会拿到一个 `PoisonError`（`lock()` 返回 `Result`），其设计意图是「警告你：临界区里可能数据已经被破坏了一半，不要再用了」。

但通道内部的临界区只做「搬指针、`swap` 一个原子标志、往队列里塞 entry」这类**不会失败**的操作，根本不存在「数据被破坏一半」的风险。如果让毒化语义生效，反而会把「无关线程的一个意外 panic」传染成「整个通道永久不可用」。所以 crossbeam-channel 包了一层非毒 `Mutex`。

### 2.2 伪随机数生成器：Xorshift

Xorshift 是一类极轻量的伪随机数生成器（PRNG）：只靠几次「异或 + 移位」就能从一个状态产生下一个状态，不需要复杂运算，非常适合放在线程本地做高频调用。本讲用到的是 32 位 Xorshift 变体。

### 2.3 Fisher–Yates 洗牌

Fisher–Yates 是经典的「原地打乱数组」算法：从后往前扫，每一步从「当前位置及之前」里随机挑一个下标，与当前位置交换。它能保证产生**每一个排列都等概率**（无偏）。

### 2.4 Lemire 快速取模

要从一个 32 位随机数 `x` 生成一个 `[0, n)` 范围内的下标，最朴素的写法是 `x % n`（取模/除法）。但除法在 CPU 上相对较慢。Daniel Lemire 提出一种**用乘法 + 位移替代除法**的「快速取模还原」，本讲的 `shuffle` 就用了它。

### 2.5 Rust 分配器基础：`Layout`、ZST、provenance

- `Layout` 描述一块内存的「大小 + 对齐」。
- **零大小类型（ZST）**：`()`、空结构体等类型占 0 字节。对 ZST 调用全局 `alloc` 是**未定义行为**（分配器不允许 size=0），所以必须特殊处理。
- **provenance（出处）**：Rust 正在引入的指针模型概念——一个指针不仅要携带「地址」，还要携带「它从哪块分配来」的出处信息。用「把整数直接 `as` 成指针」生成的指针**没有 provenance**，在某些内存模型（如 CHERI）下是非法的。

---

## 3. 本讲源码地图

| 文件 | 作用 | 被谁调用 |
|------|------|----------|
| `src/utils.rs` | 非毒 `Mutex`、`shuffle`、`sleep_until` | `select.rs`、`waker.rs`、`flavors/zero.rs`、`flavors/at.rs`、`flavors/never.rs` |
| `src/alloc_helper.rs` | `Global` 分配器封装（软链接到 `crossbeam-utils`） | `flavors/list.rs` |

补充说明：

- `src/alloc_helper.rs` 在 git 里是一个**软链接**（`git ls-files --stage` 显示其 mode 为 `120000`，指向 `../crossbeam-utils/src/alloc_helper.rs`），所以它的实现实际来自姊妹 crate `crossbeam-utils`，本 crate 只是复用。这也呼应了 [u1-l1](u1-l1-project-overview.md) 里「`alloc_helper` 是分配器封装、软链接」的描述。
- 两个模块都在 `src/lib.rs` 中以 `#[cfg(feature = "std")]` 门控声明（见 `src/lib.rs:346-347`、`src/lib.rs:363-364`），与整个 crate「禁用 `std` 暂不支持」的策略一致（详见 [u3-l6](u3-l6-no-std-and-module-organization.md)）。

---

## 4. 核心概念与源码讲解

### 4.1 非毒 `Mutex` 包装

#### 4.1.1 概念说明

`utils::Mutex<T>` 是对 `std::sync::Mutex<T>` 的一层薄封装，唯一目的是**抹掉「毒化」语义**。如前置知识所述，通道内部的临界区不会因为数据破坏而产生风险，所以即便某次持锁期间发生 panic，我们仍希望这把锁能继续正常使用，而不是把毒传染给后续每一次 `lock()`。

#### 4.1.2 核心流程

`lock()` 内部调用底层 `Mutex::lock()`，拿到的是 `Result<MutexGuard, PoisonError>`。非毒包装用 `unwrap_or_else(PoisonError::into_inner)` 把 `PoisonError` **直接拆开取出里面的 guard**——也就是说「不管有没有毒，我都照常给你锁」。伪代码：

```
fn lock() -> MutexGuard {
    底层.lock().unwrap_or_else(|poison| poison.into_inner())
    //                                ^^^^^^^^^^^^^^^^^^ 把毒错误里的 guard 拿出来
}
```

#### 4.1.3 源码精读

非毒 `Mutex` 的完整定义只有一个 `new` 和一个 `lock`：

[文件路径:utils.rs:L58-L71](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/utils.rs#L58-L71) — `Mutex<T>(std::sync::Mutex<T>)` 是个元组结构体，`lock()` 用 `PoisonError::into_inner` 忽略毒化、直接取出 `MutexGuard`。这就是「非毒」的全部实现，非常薄。

它的调用方都在「需要阻塞者队列 / 内部状态」的地方：

- [`src/waker.rs:184`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L184) — `SyncWaker` 的 `inner: Mutex<Waker>`，保护 `selectors` / `observers` 两个队列（见 [u2-l4](u2-l4-blocking-and-waking.md)）。
- [`src/flavors/zero.rs:104`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L104) — 会合通道的 `inner: Mutex<Inner>`，保护发送方 / 接收方两个 `Waker` 与 `is_disconnected` 标志（见 [u2-l7](u2-l7-zero-flavor.md)）。

#### 4.1.4 代码实践

**实践目标**：体会「毒化」与「非毒」的区别。

**操作步骤**（示例代码，非项目原有）：

```rust
// 1) 标准库毒化行为
let m = std::sync::Mutex::new(0u32);
let _g = m.lock().unwrap(); // 持锁
// 模拟持锁线程 panic（这里只是演示思路）
// 实际需要在另一个线程里 panic 后释放锁，
// 之后本线程 m.lock() 会拿到 Err(PoisonError)。

// 2) crossbeam 非毒行为（等价于 utils::Mutex 的写法）
let g = m.lock().unwrap_or_else(std::sync::PoisonError::into_inner);
println!("即使毒化也能读到: {}", *g);
```

**需要观察的现象**：标准库 `Mutex` 在毒化后 `lock()` 返回 `Err`；而用 `unwrap_or_else(PoisonError::into_inner)` 后，即便毒化也能拿到一个可用的 guard。

**预期结果**：理解通道内部为何要包这层——把「别的线程的 panic」隔离在锁之外，通道仍可继续收发。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `utils::Mutex` 改成直接用 `std::sync::Mutex` 且 `lock().unwrap()`，当一个阻塞者在 `register` 队列时 panic，会带来什么后果？

**参考答案**：该 `Mutex` 会被毒化，之后所有线程对 `SyncWaker::register` / `notify` 的 `lock().unwrap()` 都会 panic，整个通道永久瘫痪。非毒 `Mutex` 正是为避免这种「恐慌传染」。

**练习 2**：`PoisonError::into_inner` 拿出的 `MutexGuard` 是否仍然安全可用？

**参考答案**：在 crossbeam-channel 的使用场景下是安全的——临界区里只做不可失败的指针/原子操作，不存在「数据改了一半」的状态，所以即便上一个持锁者 panic，内部数据仍然处于一致状态。这正是「非毒」成立的**前提假设**。

---

### 4.2 `shuffle`：Xorshift + Lemire 取模的 Fisher–Yates

#### 4.2.1 概念说明

`shuffle` 解决的问题是 `select!` 的**公平性**。当多个通道操作「同时就绪」时，`select!` 不应总是偏袒列表里靠前的那一个，而应该**随机挑一个**（详见 [u2-l9](u2-l9-select-macro-usage.md) 与 [u3-l1](u3-l1-select-algorithm.md)）。实现方式很简单：在真正尝试各操作之前，先把操作数组**随机打乱**，再按打乱后的顺序尝试。

为了高频调用且不依赖系统随机源，`shuffle` 用了一个**线程本地的 Xorshift PRNG**，并用 **Lemire 快速取模**把随机数映射到下标范围。

#### 4.2.2 核心流程

这是标准的 Fisher–Yates 洗牌（从下标 `1` 开始向后扫，等价于「从后往前」的对称写法）：

```
对 i 从 1 到 len-1：
    1. 用 Xorshift32 更新线程本地 RNG 状态 x
    2. 令 n = i + 1
    3. 用 Lemire 取模从 x 得到 j ∈ [0, i]
    4. 交换 v[i] 与 v[j]
```

其中 Xorshift32 的状态迁移为（每一步都把 `x` 异或上自己的某个移位）：

\[ x \leftarrow x \oplus (x \ll 13) \]
\[ x \leftarrow x \oplus (x \gg 17) \]
\[ x \leftarrow x \oplus (x \ll 5) \]

Lemire 把 32 位随机数 `x` 映射到 `[0, n)` 的方法是「乘以 `n` 再右移 32 位」：

\[ j = \left\lfloor \frac{x \cdot n}{2^{32}} \right\rfloor \]

这等价于 `x % n` 的「按比例缩放」版本，但用一次乘法 + 一次移位替代了除法。

> 关于概率均匀性：当 `n` 不能整除 `2^32` 时，朴素的 `x % n` 与 Lemire 方法都存在轻微的「模偏差」，但对 `select!` 的公平性而言足够（操作数通常很少，偏差可忽略）。

#### 4.2.3 源码精读

`shuffle` 的全部实现：

[文件路径:utils.rs:L6-L40](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/utils.rs#L6-L40) — 注意几个要点：

- `len <= 1` 直接返回（无需洗牌）。
- RNG 用 `thread_local! { static RNG: Cell<Wrapping<u32>> }`，**种子固定**为 `1_406_868_647`（`const` 初始化）。这意味着每个线程的随机序列在进程内是确定的，跨进程也一致——`shuffle` 追求的是「无偏」，而不是密码学意义上的不可预测。
- Xorshift 三步异或移位（13/17/5）正对应上面的公式。
- Lemire 取模那行 `((x as u64).wrapping_mul(n as u64) >> 32) as u32 as usize` 与公式一一对应：先把 `x` 和 `n` 提升到 `u64` 相乘（避免溢出），右移 32 位即为除以 `2^32`。
- 整个洗牌包在 `RNG.try_with(|rng| ...)` 里，`let _ =` 故意忽略 `try_with` 在线程本地析构期间可能返回的 `AccessError`——这种边缘情况下不洗牌也能接受。

它在 `select.rs` 里被 `run_select` 与 `run_ready` 调用，且只在「非 biased」模式下调用：

[`src/select.rs:196-199`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L196-L199) — `if !is_biased { utils::shuffle(handles); }`。`is_biased` 来自 `select_biased!` 宏透传的开关（详见 [u3-l3](u3-l3-macro-expansion.md)）：biased 模式跳过 shuffle，于是「同时就绪时永远优先列表靠前」，这就是 `select!`（随机）与 `select_biased!`（有偏）的唯一行为差异。

#### 4.2.4 代码实践

**实践目标**：把 `shuffle` 抽出来，直观观察它的「无偏性」。

**操作步骤**（示例代码）：

1. 把下面这段独立的小程序（非项目原有代码）放进一个临时文件运行：

```rust
use std::cell::Cell;
use std::num::Wrapping;

// 直接复制自 src/utils.rs 的 shuffle 逻辑
fn shuffle<T>(v: &mut [T]) {
    let len = v.len();
    if len <= 1 { return; }
    std::thread_local! {
        static RNG: Cell<Wrapping<u32>> = const { Cell::new(Wrapping(1_406_868_647)) };
    }
    let _ = RNG.try_with(|rng| {
        for i in 1..len {
            let mut x = rng.get();
            x ^= x << 13;
            x ^= x >> 17;
            x ^= x << 5;
            rng.set(x);
            let x = x.0;
            let n = i + 1;
            let j = ((x as u64).wrapping_mul(n as u64) >> 32) as u32 as usize;
            v.swap(i, j);
        }
    });
}

fn main() {
    let mut counts = [0u32; 3]; // 统计 v[0] 落到每个值的次数
    for _ in 0..600_000 {
        let mut v = [0u32, 1, 2];
        shuffle(&mut v);
        counts[v[0] as usize] += 1;
    }
    println!("{:?}", counts); // 三个数应接近 200000 / 200000 / 200000
}
```

2. 运行（待本地验证）：`cargo run`（放在你自己的临时 crate 里）。

**需要观察的现象**：`counts` 三个桶的计数值应当**接近相等**（约 20 万各），说明每个元素出现在 `v[0]` 的概率均等——这就是 Fisher–Yates 的无偏性。

**预期结果**：直观验证「Xorshift + Lemire」组合能为 `select!` 提供足够均匀的随机顺序。

#### 4.2.5 小练习与答案

**练习 1**：为什么 RNG 用线程本地（`thread_local!`）而不是一把全局锁保护一个共享种子？

**参考答案**：避免多线程 select 时争用同一把锁/同一个原子，让每个线程有独立的 PRNG 状态、无同步开销。这也与 [u2-l4](u2-l4-blocking-and-waking.md) 里 `Context` 用线程本地缓存的思路一致。

**练习 2**：把 Lemire 那行换成 `let j = (x % n) as usize;`，结果还正确吗？

**参考答案**：正确性不变（`j` 仍在 `[0, i]` 范围内），只是性能略差（一次除法/取模代替了乘法+位移）。`shuffle` 用 Lemire 是为速度，因为 select 路径可能被频繁调用。

**练习 3**：固定种子 `1_406_868_647` 是否会让 `select!` 变得「可预测」从而有安全问题？

**参考答案**：不会。`shuffle` 的目的只是「避免总偏袒某个分支」的公平性，不承担任何安全/抗预测职责。通道操作的就绪与否由真实事件决定，shuffle 只是决定「同时就绪时先试谁」。

---

### 4.3 `sleep_until`：睡到截止时刻

#### 4.3.1 概念说明

`sleep_until(deadline: Option<Instant>)` 用**一个函数**统一表达两种语义：

- `None` —— 「永久阻塞」（近似）。
- `Some(d)` —— 「睡到时刻 `d`」。

它在 `at` / `never` 这类**时间驱动**的 flavor（见 [u2-l8](u2-l8-special-channels.md)）以及 `select.rs` 里「操作列表为空」的兜底分支里被使用。

#### 4.3.2 核心流程

```
loop {
    match deadline {
        None        => 睡 1000 秒（近似永久，循环回来再睡）
        Some(d)     =>
            now = Instant::now()
            if now >= d { break }      // 到点了
            else        { 睡 (d - now) }
    }
}
```

要点：

- `None` 用「睡 1000 秒」+ `loop` 来近似永久阻塞——既不是真的无限，也足够长；外层 `loop` 让它在被唤醒后能继续睡。
- `Some(d)` 用 `loop` 包住 `thread::sleep`，是为了容忍**虚假唤醒 / 调度器提前叫醒**：哪怕 `sleep(d - now)` 比预期早返回，循环也会重新计算剩余时间继续睡，直到 `now >= d`。

#### 4.3.3 源码精读

[文件路径:utils.rs:L42-L56](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/utils.rs#L42-L56) — 完整实现只有十几行，逻辑与上面伪代码一致。`None` 分支的 `Duration::from_secs(1000)` 是「近似永久」的工程取舍。

典型调用方：

- [`src/flavors/never.rs:40-43`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/never.rs#L40-L43) — `never` 通道的 `recv` 直接 `utils::sleep_until(deadline)`，醒来后返回 `Timeout`。`deadline` 为 `None` 时即「永久阻塞」。
- [`src/flavors/at.rs:65-68`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/at.rs#L65-L68) 与 [`src/flavors/at.rs:94-96`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/at.rs#L94-L96) — `at` 通道在「消息已被取走」后用 `sleep_until(deadline)`（可能为 `None` 表示永久阻塞）。
- [`src/select.rs:185-192`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L185-L192) — `run_select` 在 `handles.is_empty()`（没有可注册的操作）时，用 `sleep_until` 配合 `Timeout` 三态实现「空操作也要等到超时」的语义。

#### 4.3.4 代码实践

**实践目标**：观察 `None` 与 `Some(d)` 两种入参的实际阻塞时长。

**操作步骤**（源码阅读型 + 示例代码）：

1. 阅读 `src/flavors/never.rs` 的 `recv`，确认 `never().recv()` 在无超时参数时会走到 `sleep_until(None)`。
2. 在你自己的临时 crate 里写：

```rust
fn main() {
    let now = std::time::Instant::now();
    // 模拟 sleep_until(Some(d))
    let d = now + std::time::Duration::from_millis(120);
    loop {
        let n = std::time::Instant::now();
        if n >= d { break; }
        std::thread::sleep(d - n);
    }
    println!("实际经过: {:?}", now.elapsed()); // 约 120ms 上下
}
```

**需要观察的现象**：即便 `thread::sleep(d - n)` 偶尔提前返回，`loop` 也会补睡，最终 `elapsed()` 略大于或等于 120ms。

**预期结果**：理解为什么 `sleep_until` 必须用循环——单次 `thread::sleep` 不能保证精确睡到目标时刻。

**待本地验证**：不同操作系统的睡眠精度不同，实际耗时可能在 120ms~130ms 之间。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `None` 分支不直接写 `thread::sleep(Duration::MAX)`？

**参考答案**：`Duration::MAX` 在某些平台上会溢出底层系统调用的参数或被立即返回。睡 1000 秒 + 外层 `loop` 是一个安全且足够长的近似，且被唤醒后能重新评估。

**练习 2**：`at` 通道「消息已被取走」后调用 `sleep_until(None)` 然后紧跟 `unreachable!()`，为什么这不会真的 panic？

**参考答案**：`sleep_until(None)` 会进入近似永久的阻塞，正常情况下永远不会返回；只有在线程被强制中断（如进程关闭）时才会结束，那时也不会执行到 `unreachable!()`。这是用「永久阻塞」表达「这个分支在语义上不该到达终点」的常见写法（详见 [u2-l8](u2-l8-special-channels.md)）。

---

### 4.4 `Global` 分配器与 ZST / provenance 处理

#### 4.4.1 概念说明

`alloc_helper::Global` 是一个**最小化的全局分配器封装**，注释里写明它「Based on unstable `alloc::alloc::Global`」——也就是说，标准库里有一个 unstable 的 `Global` 类型，crossbeam 为了在 stable 上能用、且为了 `no_std + alloc` 友好，自己实现了一份精简版。

它与标准 `Global` 的差异（注释也提到）：标准 `Global` 的 `allocate` 返回 `NonNull<[u8]>`（带长度），而这里返回 `NonNull<u8>`（裸指针），更贴合 channel 内部「我只关心首地址、自己记得 Layout」的用法。

它的唯一使用者是 `flavors/list.rs`——无界通道的「块（Block）」需要按需堆分配（见 [u2-l6](u2-l6-list-flavor.md)）。

#### 4.4.2 核心流程

分配的核心是一个 `match layout.size()`：

```
fn alloc_impl(layout, zeroed) -> Option<NonNull<u8>>:
    match layout.size():
        0        => 返回 dangling 指针（对齐到 layout.align()，不真正分配）
        _size    => 调全局 alloc::alloc::alloc 或 alloc_zeroed，返回 NonNull<u8>
```

释放 `deallocate(ptr, layout)` 则对称地：

```
if layout.size() != 0 { dealloc(ptr, layout) }
// size == 0 时什么都不做（当初也没真正分配）
```

为什么要分 `size == 0`？因为 **ZST（零大小类型）不允许调用全局分配器**——Rust 规定 `Layout` 的 size 为 0 时传给 `alloc::alloc::alloc` 是未定义行为。所以 ZST 必须返回一个「悬空但非空、对齐合法」的指针（`dangling`），并且释放时跳过 `dealloc`。

`dangling` 的实现需要构造一个「地址等于 `layout.align()`、但没有 provenance」的指针，这正是 `without_provenance_mut` 的职责。

#### 4.4.3 源码精读

[文件路径:alloc_helper.rs:L7-L66](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/alloc_helper.rs#L7-L66) — `Global` 提供 `allocate` / `allocate_zeroed` / `deallocate` 三个方法，全部委托给私有的 `alloc_impl`。注意：

- `0 => Some(dangling(layout))` 这条分支处理 ZST——不调用真正的分配器，返回一个对齐合法的悬空指针。
- 非 0 分支调 `alloc::alloc::alloc(layout)` 或 `alloc_zeroed(layout)`，并用 `NonNull::new(raw_ptr)` 把 `*mut u8` 转成 `Option<NonNull<u8>>`（分配失败时返回 `None`，由调用方决定如何处理）。
- `deallocate` 用 `if layout.size() != 0` 守卫——和分配端对称，ZST 不调用 `dealloc`。

`without_provenance_mut` 的实现：

[文件路径:alloc_helper.rs:L68-L85](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/alloc_helper.rs#L68-L85) — 它把一个 `usize`（这里是 `layout.align()`）变成一个「无 provenance」的 `*mut T`。注意它针对 `miri` 与非 `miri` 走两条路：

- 非 `miri`：`addr as *mut T`（普通的 int-to-pointer 转换，目前在大多数目标上确实生成无 provenance 指针）。
- `miri`：用 `core::mem::transmute(addr)`，因为 Miri 对「整数转指针」的语义检查更严格，注释也说明这依赖 sysroot 的特殊地位。

注释还专门提到 CHERI 架构（每个指针带出处标签的硬件），说明作者对「provenance」模型有充分意识：在 CHERI 上普通的 `as` 转换会丢失标签，所以这里其实是「尽力而为」，真正稳妥的方案要等标准库稳定 `ptr::without_provenance_mut`。

`Global` 的真实调用方：

[`src/flavors/list.rs:90-104`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L90-L104) — `Block::new()` 用 `Global.allocate_zeroed(Self::LAYOUT)` 分配一个新块，分配失败时调 `handle_alloc_error`（标准库的「分配失败即终止」处理）。注释 `[1]~[4]` 逐字段论证了「为什么零初始化对这些字段是安全的」（`AtomicPtr`、`MaybeUninit`、`AtomicUsize` 都允许零初始化）。

#### 4.4.4 代码实践

**实践目标**：理解 ZST 分配路径与 `without_provenance_mut` 的必要性。

**操作步骤**（源码阅读型）：

1. 打开 `src/alloc_helper.rs`，定位 `alloc_impl` 的 `match layout.size()` 分支。
2. 打开 `src/flavors/list.rs:80-105`，确认 `Block::LAYOUT` 的断言 `layout.size() != 0`——即 `Block` 不是 ZST，所以它走的 `Global.allocate_zeroed` 会落到真正的 `alloc` 分支，而非 `dangling` 分支。
3. 思考：既然 `Block` 不是 ZST，为什么 `Global` 还要处理 `size == 0`？

**需要观察的现象 / 解释**：

- `Global` 是一个**通用**封装，它要保证「对任意 `Layout`（包括 ZST）调用都不触发未定义行为」。`Block` 虽然不为 0，但封装本身必须健壮。
- `without_provenance_mut` 必要，是因为 `Layout::dangling` 在 stable 上还不稳定（注释 `// Layout::dangling is unstable`），于是这里用 `without_provenance_mut(layout.align())` 自己造一个等价的「对齐到 `align`、无 provenance、非空」的悬空指针——这正是 ZST 分配该返回的东西。

**预期结果**：能复述「ZST 不调真分配器、返回悬空指针；非 ZST 才调 `alloc`；`deallocate` 用 `size != 0` 对称守卫」这条链。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `alloc_impl` 里 `0 => Some(dangling(layout))` 这条分支删掉、直接对所有 size 调 `alloc::alloc::alloc(layout)`，会发生什么？

**参考答案**：对 ZST 调用全局分配器是**未定义行为**（UB）。虽然 `Block` 不是 ZST 不会触发，但 `Global` 作为通用封装会变成「对 ZST 不安全」，违反其封装契约。

**练习 2**：为什么 `dangling` 用 `layout.align()` 作为地址，而不是随便一个非零值？

**参考答案**：Rust 要求返回的指针满足类型的对齐约束。把地址设为 `align`（一个 2 的幂）能保证指针本身对该 `Layout` 是对齐的；同时它非空，满足 `NonNull` 语义。这是一个「对齐合法、非空、但不指向任何真实分配」的标准悬空指针构造。

**练习 3**：`alloc_helper.rs` 为什么是软链接到 `crossbeam-utils`？

**参考答案**：避免在 `crossbeam` 工作区里重复实现同一份分配器封装。`crossbeam-utils` 已经维护了一份稳定的 `Global` 替代，channel 通过软链接直接复用，保证两个 crate 行为一致、改一处即同步。这也体现 crossbeam 工作区「跨 crate 复用基础设施」的组织方式（见 [u3-l6](u3-l6-no-std-and-module-organization.md)）。

---

## 5. 综合实践

把本讲三个工具串起来，做一次「从工具到上层行为」的追踪：

**任务**：解释 `select!` 在「3 个 Receiver 同时有消息」时是如何做到「随机选一个」的，并把整条链路上的工具调用都标注出来。

**步骤**：

1. 从 [`src/select.rs:196-199`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L196-L199) 的 `utils::shuffle(handles)` 出发，画出调用链：`select!` 宏 → `internal::select` → `run_select` → `utils::shuffle` → Xorshift + Lemire Fisher–Yates。
2. 标注：shuffle 之后，「先试谁」被随机化了；接着 `for &(handle, i, addr) in handles { if handle.try_select(...) { return } }` 用打乱后的顺序找到第一个就绪的操作。
3. 假设 3 个 Receiver 同时就绪，写一个小程序验证「被选中的 Receiver 在大量循环里接近 1/3、1/3、1/3」（待本地验证）：

```rust
use crossbeam_channel::{select, unbounded};
fn main() {
    let (s0, r0) = unbounded::<u32>();
    let (s1, r1) = unbounded::<u32>();
    let (s2, r2) = unbounded::<u32>();
    let mut hit = [0u32; 3];
    for _ in 0..300_000 {
        s0.send(0).unwrap(); s1.send(1).unwrap(); s2.send(2).unwrap();
        // 三个都就绪，select! 应随机选一个
        let which = select! {
            recv(r0) -> _ => 0,
            recv(r1) -> _ => 1,
            recv(r2) -> _ => 2,
        };
        hit[which] += 1;
    }
    println!("{:?}", hit); // 三个数应接近 100000
    drop(s0); drop(s1); drop(s2);
}
```

4. 思考：如果改成 `select_biased!`，结果会变成什么样？（提示：跳过 `shuffle`，靠前的分支被优先选中，`hit` 会几乎全是 `[300000, 0, 0]`。）

**预期结果**：能够清楚说明 `shuffle` 是 `select!` 公平性的直接来源，并把 Xorshift、Lemire、Fisher–Yates 三个概念对应到 `utils.rs` 的具体代码行。

---

## 6. 本讲小结

- `utils::Mutex` 是对 `std::sync::Mutex` 的**非毒封装**：用 `PoisonError::into_inner` 在 `lock()` 时忽略毒化，避免「别的线程 panic」传染成「整个通道瘫痪」。前提是通道临界区只做不可失败的操作。
- `utils::shuffle` 用**线程本地 Xorshift32 + Lemire 快速取模的 Fisher–Yates** 打乱操作数组，是 `select!`（非 biased 模式）**公平性**的直接来源；biased 模式跳过它即变为 `select_biased!`。
- `utils::sleep_until(Option<Instant>)` 用一个函数统一表达「永久阻塞（`None`）」与「睡到时刻（`Some`）」，并用 `loop` 容忍虚假唤醒，服务于 `at` / `never` 与 select 的空操作兜底。
- `alloc_helper::Global` 是 unstable `alloc::alloc::Global` 的 stable 精简替代，返回 `NonNull<u8>`；它必须对 **ZST** 特殊处理（返回 `dangling` 悬空指针、释放时跳过 `dealloc`），并用 `without_provenance_mut` 构造「无 provenance、对齐合法」的指针。
- 这两个模块都在 `src/lib.rs` 中以 `#[cfg(feature = "std")]` 门控，呼应整 crate「禁用 `std` 暂不支持」的策略；`alloc_helper.rs` 是软链接到 `crossbeam-utils` 的复用文件。

---

## 7. 下一步学习建议

- 若想看 `shuffle` 如何嵌入完整 select 调度，回到 [u3-l1（select 核心算法 run_select / run_ready）](u3-l1-select-algorithm.md)，对照 `run_select` 全流程理解 shuffle 的位置与意义。
- 若想看 `Mutex` 在阻塞者队列里的真实用法，重读 [u2-l4（阻塞与唤醒机制）](u2-l4-blocking-and-waking.md) 中 `SyncWaker` 的 `register` / `notify`。
- 若想了解 `no_std` / feature 门控与软链接等模块组织，继续阅读 [u3-l6（no_std、feature 与模块组织）](u3-l6-no-std-and-module-organization.md)。
- 进阶可阅读 `crossbeam-utils` 中 `Backoff`、`CachePadded`、`AtomicCell` 的实现，与本讲的工具放在一起，构成 crossbeam 并发基础设施的完整图景。
