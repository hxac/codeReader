# Backoff 指数退避自旋

## 1. 本讲目标

本讲专讲 `crossbeam-utils` 中的 `Backoff` 类型——一个用于「自旋循环里做指数退避」的小工具。读完本讲你应当能够：

- 说清 `Backoff` 的内部状态（单个 `step` 计数器）和两个常量上限 `SPIN_LIMIT`、`YIELD_LIMIT` 的含义。
- 区分 `spin()` 与 `snooze()` 两种退避方式各自适合的场景，并解释它们的实现差异。
- 解释为什么退避次数要「按 2 的幂指数增长」并设置上限，以及这为何能降低多核争用（contention）。
- 掌握 `is_completed()` 的用法：何时应当停止退避、改用 `thread::park()` 等真正阻塞的同步机制。
- 理解 `Backoff` 在 `no_std` 环境下如何退化为纯自旋。

本讲只覆盖 `src/backoff.rs` 一个文件，是 `crossbeam-utils` 中最独立、最易读的模块之一，不依赖 atomic 模块的任何内部机制。

## 2. 前置知识

在进入源码之前，先用通俗语言建立三个直觉。

### 2.1 为什么自旋循环需要「退避」

考虑一个 lock-free 的 CAS（compare-and-swap）循环：多个线程同时尝试修改同一个原子变量，每次只有一个线程赢，其余线程的 CAS 失败后立刻重试。问题是：失败后「立刻重试」会让所有失败的线程在同一时刻再次撞在一起，形成**高争用（contention）**。CPU 的缓存一致性协议（如 MESI）不得不在多个核之间反复搬运同一条 cache line，这种来回搬运的开销往往比真正做一次有用功还大。

退避（backoff）的思路借鉴了以太网的 CSMA/CD：碰撞后不要立刻重试，而是**先等一段随机/递增的时间**，让争用错峰。`Backoff` 采用「指数增长」的等待：第一次失败等 1 个单位，第二次等 2 个，第三次 4 个……这样高争用时等待时间迅速拉长，争用自然缓解；而低争用时不影响第一个重试的线程快速拿到机会。

### 2.2 三种「让出」的层次

理解 `Backoff` 的关键是分清三种不同强度的「让出 CPU」：

| 强度 | 机制 | 说明 |
|------|------|------|
| 最轻 | `core::hint::spin_loop()`（CPU 的 *PAUSE*/*YIELD* 指令） | 不切换线程，只是提示 CPU「我在自旋」，让出超流水线资源、降低功耗、避免内存顺序违例惩罚。线程仍在运行。 |
| 中等 | `thread::yield_now()` | 把当前线程剩余的时间片还给操作系统调度器，调度器可能切换到别的线程。仍处于「就绪」状态，随时会被重新调度。 |
| 最重 | `thread::park()` | 真正阻塞线程，直到有人 `unpark` 或虚假唤醒。线程不再消耗 CPU。 |

`spin()` 只用第一种；`snooze()` 在初期用第一种、后期升级到第二种；`is_completed()` 返回 `true` 后建议你自己改用第三种。这就是 `Backoff` 的「三级火箭」式递进。

### 2.3 单线程状态与 `Cell`

`Backoff` 内部只有一个计数器 `step`，用 `Cell<u32>` 而非 `AtomicU32` 存储。这是因为一个 `Backoff` 实例**只服务于一个线程**——它本身就是线程私有的退避工具。`Cell<T>` 是 `!Sync` 的，所以 `Backoff` 也是 `!Sync`，编译器会在编译期阻止你通过共享引用跨线程使用同一个 `Backoff`，从而免去原子操作的开销。你可以把 `Backoff` **移动**到某个线程里（它是 `Send` 的），但不能在多线程间**共享**它。

> 前置讲义承接：本讲用到的 `core::hint::spin_loop` 在 crate 内是通过 `crate::primitive::hint` 抽象层引入的（见 [u1-l2](u1-l2-module-map.md) 与 [u1-l3](u1-l3-features-build-and-tests.md) 讲过的 `primitive` 抽象层），它在 loom 下会被替换成 `loom::hint::spin_loop` 以参与模型测试。

## 3. 本讲源码地图

本讲只涉及两个文件：

| 文件 | 作用 |
|------|------|
| `src/backoff.rs` | `Backoff` 类型的全部实现：结构定义、`new`/`reset`/`spin`/`snooze`/`is_completed` 方法、`Debug` 与 `Default` trait 实现。 |
| `src/lib.rs` | 顶层模块声明与导出，本讲关注它如何把 `Backoff` 暴露到 crate 根，以及 `primitive::hint` 抽象层如何提供 `spin_loop`。 |

`Backoff` 在 `lib.rs` 的顶层文档里被归类为「Utilities」之一，与 `CachePadded`、`scope` 并列，且**不受任何 feature 门控**——即使关闭 `std` 与 `atomic`，`Backoff` 依然可用（只是 `snooze` 会退化为纯自旋）。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：先认识 `Backoff` 的状态模型，再依次精读 `spin`、`snooze`、`is_completed`。

### 4.1 Backoff 的状态模型：step 与两个上限

#### 4.1.1 概念说明

`Backoff` 的全部状态就是「我已经退避了几次」这一个整数，源码里叫 `step`。围绕它有两个常量上限：

- `SPIN_LIMIT = 6`：区分「纯自旋」与「让出时间片」的边界。
- `YIELD_LIMIT = 10`：区分「继续退避」与「放弃退避、改用阻塞」的边界。

整条退避曲线由这两个常量切成三段：step 在 `0..=6` 时只发 CPU pause 指令；step 在 `7..=10` 时（仅 `snooze`）让出时间片；step `> 10` 时 `is_completed()` 返回 `true`，建议你不再退避。

#### 4.1.2 核心流程

状态转移可用下面的伪状态机描述（每次调用 `spin`/`snooze` 都会让 `step` 至多前进一格）：

```
step = 0
   │  spin()/snooze() 调用
   ▼
[ 0 ..= 6 ]  ── spin 阶段：发 2^step 次 pause 指令
   │  step 每次 +1
   ▼
[ 7 ..= 10 ] ── yield 阶段（仅 snooze）：std 下 yield_now()
   │  step 每次 +1
   ▼
[ 11 ]       ── is_completed() == true：建议 park/condvar
```

指数增长用数学表达即：在 spin 阶段，第 \(s\) 次退避要执行的 pause 指令数为

\[
n(s) = 2^{s}, \quad 0 \le s \le 6
\]

`spin()` 为了防止 `step` 越界还做了一次 `min` 截断（见 4.2），所以 `spin()` 单次最多发 \(2^6 = 64\) 条 pause 指令。相邻两次退避的等待时间「大约」翻倍，这就是文档里 *Each step of the back off procedure takes roughly twice as long as the previous step* 的来历。

#### 4.1.3 源码精读

先看常量与结构定义：

[src/backoff.rs:3-6](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/backoff.rs#L3-L6) 引入 `primitive::hint`（即 `spin_loop` 的来源）并定义两个上限常量 `SPIN_LIMIT=6`、`YIELD_LIMIT=10`。

[src/backoff.rs:80-82](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/backoff.rs#L80-L82) 定义 `Backoff` 结构：单个 `Cell<u32>` 字段 `step`。注意是 `Cell` 不是 `Atomic`——这是 `!Sync` 的，强制一个实例只能被一个线程使用。

[src/backoff.rs:94-97](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/backoff.rs#L94-L97) `new()` 把 `step` 初始化为 0，是 `const fn`，可以在常量上下文里构造。

[src/backoff.rs:109-112](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/backoff.rs#L109-L112) `reset()` 把 `step` 归零。当你成功完成一次操作、即将进入下一轮可能的争用时，调用它可以让退避「从头开始」，避免上一次的激进退避残留下来拖慢本来很快就能成功的下一轮。

[src/lib.rs:92-93](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs#L92-L93) 顶层用私有 `mod backoff;` 加 `pub use crate::backoff::Backoff;`，把类型直接重导出到 crate 根，所以用户写 `crossbeam_utils::Backoff` 即可，无需 `crossbeam_utils::backoff::Backoff`。这里没有任何 `#[cfg(feature = ...)]`，印证了 `Backoff` 始终可用。

[src/lib.rs:70-75](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs#L70-L75) 非 loom 分支下，`primitive::hint::spin_loop` 就是 `core::hint::spin_loop`，所以 `Backoff` 不依赖 `std`。

#### 4.1.4 代码实践

**实践目标**：通过 `Debug` 输出观察 `step` 随调用次数的增长。

操作步骤（示例代码，非项目原有代码）：

```rust
// 示例代码：观察 Backoff 的 step 增长
use crossbeam_utils::Backoff;

fn main() {
    let b = Backoff::new();
    println!("初始: {:?}", b);
    for i in 0..13 {
        b.snooze(); // 用 snooze 让 step 能一路涨到 11
        println!("第 {} 次 snooze 后: {:?}", i + 1, b);
    }
}
```

需要观察的现象：`Debug` 输出里 `step` 字段从 0 逐次 +1，到 11 后不再增长；`is_completed` 字段在第 11 次后变为 `true`。

预期结果：`step` 序列为 `1,2,3,4,5,6,7,8,9,10,11,11,11`，`is_completed` 在 step 到 11 时变 `true`。若把 `snooze` 换成 `spin`，`step` 会在 7 处封顶（因为 `spin` 的递增上限是 `SPIN_LIMIT`）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `SPIN_LIMIT` 改成 0，`spin()` 的行为会变成什么？
**答案**：`1 << min(step, 0)` 恒为 `1 << 0 = 1`，所以每次 `spin()` 只发一条 pause 指令；同时 `step <= 0` 仅在初始 step=0 时成立，调用一次后 step=1 就不再增长。退避彻底失效，相当于「失败后只 pause 一次就重试」。

**练习 2**：为什么 `Backoff` 用 `Cell<u32>` 而不是 `AtomicU32`？
**答案**：因为 `Backoff` 是单线程私有的（每次进入循环都在栈上 `Backoff::new()`），不存在跨线程共享 `step` 的需求。`Cell` 没有 `Sync`，编译器会阻止跨线程共享，同时避免了不必要的原子操作开销；`Cell` 的读写也比原子操作更廉价。

---

### 4.2 spin：lock-free 循环里的指数退避

#### 4.2.1 概念说明

`spin()` 专为 **lock-free 重试**设计：你预期「下一次重试很可能就成功了」，只是因为别的线程抢先一步才失败。这种情况下你**不愿意**让出时间片（让出再调度回来的代价比自旋还高），只想极短地「抖一下」让争用错峰。所以 `spin()` 全程只发 CPU pause 指令，从不调用 `yield_now`。

#### 4.2.2 核心流程

```
spin():
    n = 1 << min(step, SPIN_LIMIT)   // 计算本次要发多少条 pause
    for _ in 0..n:
        spin_loop()                  // CPU 的 PAUSE/YIELD 指令
    if step <= SPIN_LIMIT:           // 还没到 spin 上限
        step += 1                    // 下次等更久
```

注意两点细节：

1. 用 `min(step, SPIN_LIMIT)` 给指数截断，所以即便 `step` 很大，单次 `spin()` 最多发 \(2^6=64\) 条 pause。
2. `step` 的递增条件是 `step <= SPIN_LIMIT`（即 `<= 6`），所以连续调用 `spin()` 时 `step` 在 7 处封顶，之后每次都固定发 64 条 pause。换句话说，**`spin()` 单独使用时 `step` 永远在 `[0, 7]` 范围内**。

各 `step` 下的 pause 次数：

| step | `1 << min(step,6)` |
|------|--------------------|
| 0 | 1 |
| 1 | 2 |
| 2 | 4 |
| 3 | 8 |
| 4 | 16 |
| 5 | 32 |
| ≥6 | 64 |

#### 4.2.3 源码精读

[src/backoff.rs:145-154](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/backoff.rs#L145-L154) 是 `spin()` 的全部实现。第一段循环用 `min(SPIN_LIMIT)` 截断后左移，确定 pause 次数；第二段 `if` 在未达上限时让 `step` 自增。整个函数标了 `#[inline]`，因为它会在热路径（CAS 循环）里被频繁调用，内联可以消除函数调用开销。

[src/backoff.rs:114-119](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/backoff.rs#L114-L119) 文档明确指出 `spin()` 用于「因为别的线程取得进展而需要重试」的场景，处理器可能用 *YIELD*/*PAUSE* 指令让出——它**不**会让出时间片。

`hint::spin_loop` 的来源见 [src/lib.rs:73-75](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs#L73-L75)（非 loom，即 `core::hint::spin_loop`）与 [src/lib.rs:50-52](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs#L50-L52)（loom，即 `loom::hint::spin_loop`）。在 loom 模型测试里，`spin_loop` 会让 loom 切换到其它线程，从而能枚举不同的线程交错顺序。

#### 4.2.4 代码实践

**实践目标**：体验 `spin()` 在 lock-free CAS 循环里的标准用法。

操作步骤：阅读并理解项目自带文档示例 [src/backoff.rs:19-36](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/backoff.rs#L19-L36)，它实现了一个 `fetch_mul`（原子乘法，标准库没有提供）。

```rust
// 摘自项目文档示例（src/backoff.rs:19-36）
fn fetch_mul(a: &AtomicUsize, b: usize) -> usize {
    let backoff = Backoff::new();
    loop {
        let val = a.load(SeqCst);
        if a.compare_exchange(val, val.wrapping_mul(b), SeqCst, SeqCst).is_ok() {
            return val;
        }
        backoff.spin();   // CAS 失败时退避，然后重试
    }
}
```

需要观察的现象：在只有少量竞争时，`spin()` 几乎不引入延迟（第一次只 pause 1 条指令）；竞争激烈时它指数拉长，缓解 cache line 争用。

预期结果：函数语义上等价于「原子地把 `a` 乘以 `b` 并返回旧值」。本讲的 4.5 综合实践会把它扩展成一个完整的吞吐对比实验。

#### 4.2.5 小练习与答案

**练习 1**：在 `fetch_mul` 里把 `backoff.spin()` 换成 `backoff.snooze()` 会更慢还是更快？为什么？
**答案**：通常更慢。`fetch_mul` 是 lock-free 重试，你预期很快能成功；而 `snooze()` 在 step 超过 6 后会调用 `thread::yield_now()` 让出时间片，调度回来的开销大于短暂自旋。`spin()` 正是为「预期很快成功」的场景量身定做的。

**练习 2**：连续调用 100 次 `spin()` 后，第 100 次会发多少条 pause 指令？
**答案**：64 条。因为 `step` 在到达 7 后就不再增长（`step <= SPIN_LIMIT` 不再成立），此后每次 `1 << min(step, 6)` 都等于 `1 << 6 = 64`。

---

### 4.3 snooze：阻塞循环里的退避（含 no_std 退化）

#### 4.3.1 概念说明

`snooze()` 用于**阻塞等待**场景：你在等**别的线程**取得进展（例如等一个 `AtomicBool` 变 `true`），而不是自己去抢一个 CAS。这种情况下，长时间纯自旋会浪费 CPU，所以 `snooze()` 在退避的后期会调用 `thread::yield_now()` 让出时间片。

但它仍然遵守「先自旋、后让出」的渐进策略：初期（step ≤ 6）只发 pause 指令快速自旋，给「很快就发生」的事件留出低延迟路径；只有当等待变久（step > 6）才升级为 `yield_now`。

#### 4.3.2 核心流程

```
snooze():
    if step <= SPIN_LIMIT:              // 仍在 spin 阶段
        for _ in 0..(1 << step):
            spin_loop()
    else:                               // 进入 yield 阶段
        if not(feature std):            // no_std：没有 yield_now
            for _ in 0..(1 << step):
                spin_loop()             // 只能继续自旋（128/256/512/...）
        else:                           // std
            thread::yield_now()         // 让出整个时间片
    if step <= YIELD_LIMIT:             // 未达总上限
        step += 1
```

注意三个关键差异（与 `spin` 对比）：

1. `snooze` 的递增上限是 `YIELD_LIMIT=10`（而不是 `SPIN_LIMIT=6`），所以 `step` 能一路涨到 11。
2. 在 spin 阶段，`snooze` 用的是 `1 << step`（无 `min` 截断），但因为该分支只在 `step <= 6` 时进入，所以效果一致。
3. no_std 下没有 `yield_now`，`snooze` 的 else 分支退化为继续自旋 `1 << step` 次（step=7 时 128 次、step=8 时 256 次……）。这正是文档里 *In `#[no_std]` environments, this method is equivalent to `spin`* 的精确含义——功能上仍可调用，但失去了「让出时间片」的能力。

`snooze` 在各 step 下的行为（std vs no_std）：

| step | snooze 行为 (std) | snooze 行为 (no_std) |
|------|-------------------|----------------------|
| 0..=6 | spin \(2^{step}\) 次 | spin \(2^{step}\) 次 |
| 7 | `yield_now()` | spin 128 次 |
| 8 | `yield_now()` | spin 256 次 |
| 9 | `yield_now()` | spin 512 次 |
| 10 | `yield_now()` | spin 1024 次 |
| ≥11 | `yield_now()`（step 不再增长） | spin 2048 次（step 不再增长） |

#### 4.3.3 源码精读

[src/backoff.rs:206-225](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/backoff.rs#L206-L225) 是 `snooze()` 的全部实现。`if step <= SPIN_LIMIT` 分支处理 spin 阶段；`else` 分支用 `#[cfg(not(feature = "std"))]` 与 `#[cfg(feature = "std")]` 两段互斥代码分别处理 no_std（继续自旋）与 std（`yield_now`）。最后 `if step <= YIELD_LIMIT` 控制 step 的总上限。

注意 [src/backoff.rs:218-219](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/backoff.rs#L218-L219) 用了全路径 `::std::thread::yield_now()`，这是为了在 `#![no_std]` 的 crate 里显式跳过 `std` prelude、直接定位到 `std` crate——和 [src/lib.rs:44-45](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs#L44-L45) 的 `extern crate std;` 配合，确保只有开启 `std` feature 时这段代码才编译。

[src/backoff.rs:156-166](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/backoff.rs#L156-L166) 文档明确说明 `snooze` 用于「等待别的线程取得进展」，并提示「如有可能，用 `is_completed` 判断何时改用其它阻塞机制」。

#### 4.3.4 代码实践

**实践目标**：用 `snooze()` 自旋等待一个条件成立，体会它与 `spin()` 的差别。

操作步骤（示例代码）：

```rust
// 示例代码：用 snooze 等待 AtomicBool
use crossbeam_utils::Backoff;
use std::sync::atomic::{AtomicBool, Ordering::SeqCst};

fn spin_wait(ready: &AtomicBool) {
    let backoff = Backoff::new();
    while !ready.load(SeqCst) {
        backoff.snooze();      // 等待别人把 ready 设为 true
    }
}
```

需要观察的现象：等待初期线程满载自旋（CPU 占用高），约 7 次 `snooze` 后开始 `yield_now`，CPU 占用明显下降但仍可调度（不像 `park` 那样完全阻塞）。

预期结果：当 `ready` 在短时间内被置位时，函数迅速返回；若置位很慢，`snooze` 会逐渐「冷静下来」不再霸占 CPU。具体 CPU 占用曲线「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：在 `#![no_std]` 且 `feature = "std"` 关闭时，`snooze()` 还能让出时间片吗？
**答案**：不能。此时 `#[cfg(feature = "std")]` 分支不编译，else 分支执行的是 `for _ in 0..(1<<step) { spin_loop() }`，即继续自旋。`no_std` 环境没有 OS 调度器接口，无法 `yield_now`，所以 `snooze` 退化为「等比拉长的高级自旋」。

**练习 2**：为什么 `snooze` 在 step ≤ 6 阶段也要先自旋一段时间才 `yield_now`，而不是一上来就 `yield_now`？
**答案**：为了给「很快就会发生」的事件保留低延迟路径。很多等待其实只需要几微秒就会结束，纯自旋 + pause 的延迟远低于「yield 出去再被调度回来」。只有当等待确实变久（step 超过 6）才值得付出调度开销去 `yield_now`。这是一种「先乐观后悲观」的自适应策略。

---

### 4.4 is_completed：何时停止退避、改用阻塞

#### 4.4.1 概念说明

`is_completed()` 返回一个 `bool`：当 `step > YIELD_LIMIT`（即 `step >= 11`）时返回 `true`，表示「指数退避已经做完了一整轮，继续退避没有意义了」。此时文档建议你**改用真正阻塞的同步机制**（如 `thread::park()`、`Condvar`、`Parker`），把 CPU 让给别的线程做有用的事，而不是继续自旋或 yield。

注意 `is_completed()` 本身不会阻塞，它只是一个「该不该换策略」的判断器。

#### 4.4.2 核心流程

`is_completed()` 的典型用法是与 `snooze()` 配合，构成「先退避、后阻塞」的混合等待循环：

```
backoff = Backoff::new()
while not condition:
    if backoff.is_completed():
        thread::park()       # 退避用尽，真正阻塞
    else:
        backoff.snooze()     # 还在退避范围内
```

这个模式的好处是：

- 短等待：在退避阶段就完成，延迟低、无需 park/unpark 的唤醒开销。
- 长等待：退避用尽后自动切换到 `park`，不再浪费 CPU。
- 与 `unpark` 配合：唤醒方在置位条件后调用 `thread::current().unpark()`，被 park 的线程就能醒来重新检查。

#### 4.4.3 源码精读

[src/backoff.rs:270-273](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/backoff.rs#L270-L273) `is_completed()` 的实现只有一行：`self.step.get() > YIELD_LIMIT`。由于 `snooze` 把 `step` 上限卡在 11，所以一旦 `step` 到 11，`is_completed()` 永久返回 `true`（除非 `reset()`）。

[src/backoff.rs:52-73](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/backoff.rs#L52-L73) 项目自带的 `blocking_wait` 文档示例展示了 `is_completed` + `snooze` + `thread::park` 的标准组合：先 `snooze` 退避，退避用尽后 `park`，唤醒方在置位 `ready` 后调用 `waiter.unpark()`。

[src/backoff.rs:227-228](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/backoff.rs#L227-L228) 文档说 `is_completed` 表示「exponential backoff has completed and blocking the thread is advised」。

> 衔接后续：`thread::park`/`unpark` 这一对原语在 `crossbeam-utils` 内部被 `sync::Parker` 封装得更安全、更易用（见 [u3-l1 Parker](u3-l1-parker.md)）。`Backoff` + `Parker` 经常组合使用：`Backoff` 处理短等待，`Parker` 处理 `is_completed` 之后的长等待。

#### 4.4.4 代码实践

**实践目标**：跑通「退避 + park」混合等待，观察从自旋到阻塞的切换。

操作步骤：复制 [src/backoff.rs:233-266](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/backoff.rs#L233-L266) 的 `blocking_wait` 示例，把唤醒线程的 `sleep` 时长从 1ms 改成 500ms，分别观察。

需要观察的现象：

- 1ms 时：等待很短，`backoff` 大概率还没 `is_completed` 条件就成立了，全程在退避阶段。
- 500ms 时：`backoff` 很快退避完，`is_completed()` 变 `true`，主线程进入 `park`，CPU 占用降到接近 0，直到唤醒线程 `unpark`。

预期结果：长等待场景下主线程 CPU 占用显著低于纯 `snooze` 自旋版本。具体数值「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：如果不调用 `reset()`，一个 `Backoff` 一旦 `is_completed()` 返回 `true`，还能回到退避阶段吗？
**答案**：不能。`step` 一旦达到 11，`snooze` 的递增条件 `step <= YIELD_LIMIT` 不再成立，`step` 永远停在 11，`is_completed()` 永久为 `true`。要重新进入退避阶段，必须调用 `reset()` 把 `step` 归零。所以在「循环等待」的场景里，每次成功推进后调用 `reset()` 是常见做法。

**练习 2**：为什么 `is_completed` 的阈值用 `YIELD_LIMIT=10` 而不是 `SPIN_LIMIT=6`？
**答案**：因为 `is_completed` 标志的是「连 yield 阶段都走完了」。`SPIN_LIMIT` 只划分 spin/yield，`YIELD_LIMIT` 才划分「还在退避」/「退避已尽」。如果用 `SPIN_LIMIT`，线程在刚进入 yield 阶段就会被建议 park，白白浪费了「让出时间片但仍快速响应」的中间档位。

---

## 5. 综合实践

设计一个贯穿本讲的小任务：**对比 `Backoff::spin()` 与裸自旋在高争用 CAS 下的吞吐差异**，亲手验证指数退避为何能减少 contention。

### 实践目标

实现两种「原子乘法」`fetch_mul`——一种失败后 `backoff.spin()`，另一种失败后立即重试——在多线程高竞争下分别测量总耗时，解释差异。

### 操作步骤

新建一个 binary crate（示例代码，非项目原有代码）：

```rust
// 示例代码：Cargo.toml 需要 [dependencies] crossbeam-utils = "0.8"
use crossbeam_utils::Backoff;
use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering::SeqCst};
use std::thread;
use std::time::Instant;

// 版本 A：失败后用 Backoff 退避
fn fetch_mul_backoff(a: &AtomicUsize, b: usize) -> usize {
    let backoff = Backoff::new();
    loop {
        let val = a.load(SeqCst);
        if a.compare_exchange(val, val.wrapping_mul(b), SeqCst, SeqCst).is_ok() {
            return val;
        }
        backoff.spin();
    }
}

// 版本 B：失败后立即重试（裸自旋）
fn fetch_mul_bare(a: &AtomicUsize, b: usize) -> usize {
    loop {
        let val = a.load(SeqCst);
        if a.compare_exchange(val, val.wrapping_mul(b), SeqCst, SeqCst).is_ok() {
            return val;
        }
    }
}

fn bench(name: &str, f: fn(&AtomicUsize, usize) -> usize, threads: usize, iters: usize) {
    let a = Arc::new(AtomicUsize::new(1));
    let start = Instant::now();
    let handles: Vec<_> = (0..threads)
        .map(|_| {
            let a = Arc::clone(&a);
            thread::spawn(move || {
                for _ in 0..iters {
                    f(&a, 3); // 用奇数 3，避免值过早变成 0
                }
            })
        })
        .collect();
    for h in handles { h.join().unwrap(); }
    println!("{name}: {threads} 线程 × {iters} 次 = {} ms", start.elapsed().as_millis());
}

fn main() {
    for &threads in &[1usize, 2, 4, 8] {
        bench("backoff", fetch_mul_backoff, threads, 100_000);
        bench("bare   ", fetch_mul_bare,    threads, 100_000);
    }
}
```

### 需要观察的现象

- `threads = 1` 时两个版本耗时接近（无争用，`spin` 只 pause 1 条指令）。
- `threads` 增大（2、4、8）时，裸自旋版本的耗时应**显著**高于 backoff 版本，且差距随线程数扩大而拉大。

### 预期结果

在高争用下，`Backoff` 版本明显更快。原因：裸自旋让所有失败线程在下一拍同时再撞同一条 cache line，触发昂贵的 cache line 弹跳；`Backoff` 的指数错峰让重试时间散开，大幅减少同时碰撞的线程数，从而提高总体吞吐。具体倍数关系「待本地验证」（取决于核数、缓存架构、内存带宽），但定性结论在多核机器上稳定成立。

### 进阶（可选）

1. 把 `spin()` 换成 `snooze()`，比较三者；解释为何在纯 lock-free 场景里 `snooze` 通常不如 `spin`。
2. 用 `perf stat` 观察裸自旋版本的 `cache-misses` / `context-switches`，与 backoff 版本对照。
3. 在 loom 下（设置 `RUSTFLAGS="--cfg crossbeam_loom"`）把 `spin_loop` 的交错行为跑一遍，理解 `Backoff` 如何被纳入并发模型测试（参考 [u5-l4 测试与基准](u5-l4-testing-and-benchmarks.md)）。

## 6. 本讲小结

- `Backoff` 的全部状态是单个 `Cell<u32>` 计数器 `step`，由 `SPIN_LIMIT=6` 与 `YIELD_LIMIT=10` 两个常量切出「spin / yield / completed」三段。
- `spin()` 专为 lock-free CAS 重试设计：只发 CPU pause 指令、从不让出时间片，单次最多 64 条 pause，`step` 在 7 处封顶。
- `snooze()` 专为阻塞等待设计：先指数自旋，`step > 6` 后在 std 下 `yield_now()` 让出时间片；no_std 下退化为继续自旋（无法让出）。
- 指数增长 + 上限的组合，让低争用时延迟极低、高争用时自动错峰，缓解 cache line 弹跳。
- `is_completed()`（`step > 10`）表示退避已尽，建议改用 `thread::park()` / `Condvar` / `Parker` 等真正阻塞的机制，构成「先退避、后阻塞」的混合等待。
- `Backoff` 不受任何 feature 门控，始终可用；它是 `!Sync` 的线程私有工具，用 `Cell` 而非 `Atomic` 以避免无谓的原子开销。

## 7. 下一步学习建议

- 想看 `Backoff` 在 crate 内的真实用武之地，可阅读 `crossbeam-epoch`、`crossbeam-deque` 等姊妹 crate，它们在 CAS 循环里大量使用 `Backoff::spin()`。
- 若你关心「`is_completed` 之后该交给谁」，下一站是 [u3-l1 Parker / Unparker](u3-l1-parker.md)：`Parker` 正是 `Backoff` 退避用尽后的标准继任者，二者经常成对出现。
- 若想理解 `Backoff` 依赖的 `primitive::hint::spin_loop` 在 loom 下如何被替换以参与模型测试，可回顾 [u1-l3 构建特性与 build.rs](u1-l3-features-build-and-tests.md)，并在 [u5-l4 并发测试策略与基准](u5-l4-testing-and-benchmarks.md) 中看到完整的 loom/Miri 验证方法。
