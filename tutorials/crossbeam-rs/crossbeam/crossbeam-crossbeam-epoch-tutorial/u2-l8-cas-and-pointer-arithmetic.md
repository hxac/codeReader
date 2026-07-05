# CAS 与指针运算：compare_exchange / fetch_update / fetch_and-or-xor

## 1. 本讲目标

本讲聚焦 `Atomic<T>` 上的「读改写」操作。学完后你应当能够：

- 说清 `compare_exchange` / `compare_exchange_weak` 的入参、两个 `Ordering`、以及返回值 `Result<CompareExchangeValue, CompareExchangeError>` 在成功与失败两种情况下各装了什么。
- 理解 `fetch_update` 是如何用 `compare_exchange_weak` 包出一个「循环 CAS」的，以及它的闭包为何可能被调用多次。
- 掌握 `fetch_and` / `fetch_or` / `fetch_xor` 只作用于 tag 低位、不影响指针本身的位运算技巧，以及为何在 miri 与非 miri 下走两条不同实现。
- 理解 `Pointer` 这个 sealed trait 的设计动机：它让 `compare_exchange` / `store` / `swap` 既能接收 `Owned`、也能接收 `Shared`，并且在失败时把 `new` 原样（带类型）还给调用者。

本讲全部源码集中在 [`src/atomic.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs)，是 u2-l5、u2-l7 的直接延续。

## 2. 前置知识

阅读本讲前，你需要先建立以下心智模型（对应前置讲义 u2-l5、u2-l7）：

- **tagged pointer**：堆地址按 `T::ALIGN` 对齐，低位空闲，可存一个 tag。可用位数为 `k = ALIGN.trailing_zeros()`，tag 合法范围是 \([0, 2^k - 1]\)。`low_bits()` 给出掩码，`compose_tag` / `decompose_tag` 互为逆操作。见 [atomic.rs:58-85](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L58-L85)。
- **三类指针**：`Atomic<T>`（共享原子指针，无 `Drop`）、`Owned<T>`（独占所有权，类 `Box`）、`Shared<'g, T>`（从 `Atomic` 借出的 `Copy` 指针，生命周期绑死 guard）。
- **CAS（compare-and-swap）的通用语义**：原子地比较「当前值是否等于预期值」，若相等则写入新值并返回成功，否则什么都不做并返回失败。这是无锁数据结构实现「先读后改」的核心原语。

本讲用到但不再重复解释的工具：`Ordering`（`SeqCst`/`Acq`/`Rel`/`Acquire`/`Release`）、`&Guard`（仅用于把返回的 `Shared` 生命周期钉成 `'g`）、`Shared::with_tag` / `Shared::tag`（读写低位 tag）。

> 一个关键直觉：CAS 比较的是**完整的机器字**（指针高位 + tag 低位），所以「指向同一对象但 tag 不同」的两个指针**不相等**。这一点贯穿本讲所有操作。

## 3. 本讲源码地图

本讲只涉及一个源码文件，但其中的类型与函数分布在不同区域，先给一张地图：

| 位置 | 作用 |
|------|------|
| [atomic.rs:22-56](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L22-L56) | `CompareExchangeValue` 与 `CompareExchangeError` 两个返回类型 |
| [atomic.rs:58-95](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L58-L95) | tag 位运算工具：`low_bits` / `compose_tag` / `decompose_tag` / `map_addr` |
| [atomic.rs:460-486](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L460-L486) | `Atomic::compare_exchange` |
| [atomic.rs:544-570](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L544-L570) | `Atomic::compare_exchange_weak` |
| [atomic.rs:612-630](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L612-L630) | `Atomic::fetch_update`（循环 CAS） |
| [atomic.rs:651-744](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L651-L744) | `Atomic::fetch_and` / `fetch_or` / `fetch_xor`（tag 位运算） |
| [atomic.rs:925-939](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L925-L939) | `Pointer<T>` sealed trait 定义 |
| [atomic.rs:952-974](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L952-L974) 与 [1210-1224](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1210-L1224) | `Owned` / `Shared` 各自的 `Pointer` 实现 |
| [atomic.rs:1622-1734](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1622-L1734) | `compare_exchange_*` 的单元测试，可作为行为参考 |

## 4. 核心概念与源码讲解

### 4.1 compare_exchange 与 compare_exchange_weak

#### 4.1.1 概念说明

无锁数据结构里最常见的操作模式是：「我先读出当前值 `current`，基于它算出一个新值 `new`，然后**只有当此刻原子里仍然是 `current` 时**才写入 `new`」。这个「读-比较-写」必须是一条不可分割的硬件指令（如 x86 的 `lock cmpxchg`），否则两个线程可能同时以为自己改成功了。这个原语就是 **CAS（compare-and-swap）**。

crossbeam 在此之上要处理两个额外问题：

1. 它的「值」是带 tag 的指针（一个机器字），比较时必须**连同 tag 一起比较**。
2. 写入的新值可能来自 `Owned`（要交出所有权），也可能来自 `Shared`（只是借用），API 必须同时接纳二者——这正是后文 4.4 的 `Pointer` trait 要解决的。

`compare_exchange` 与 `compare_exchange_weak` 的唯一区别是：**后者允许「假失败」**（spurious failure），即比较明明相等也返回 `Err`。`weak` 版本在某些平台（如 ARM 的 `ldxr`/`stxr`）上能编译成更高效的指令，因此**放在循环里重试**时优先用 `weak`；**只尝试一次**的场景用非 `weak` 版本，避免无谓重试。

#### 4.1.2 核心流程

`compare_exchange(current, new, success, failure, guard)` 的执行过程：

1. 把 `current`（`Shared`）和 `new`（`Owned` 或 `Shared`）各自转成机器字 `*mut ()`。
2. 调用底层 `AtomicPtr::compare_exchange(current_ptr, new_ptr, success, failure)`：
   - 若此刻原子里 == `current_ptr`：写入 `new_ptr`，返回 `Ok(旧值)`。
   - 否则：什么都不写，返回 `Err(实际当前值)`。
3. 成功时构造 `CompareExchangeValue { old, new }`（两个都是 `Shared`）。
4. 失败时构造 `CompareExchangeError { current, new }`（`current` 是 `Shared`，`new` 是**原始类型 `P`**，原样还给调用者）。

两个 `Ordering` 参数的含义：`success` 是「比较成功时这次读改写」的排序；`failure` 是「比较失败时退化为一次 load」的排序。`failure` 只能是 `SeqCst`/`Acquire`/`Relaxed`，且不得强于 `success`。

> **所有权的关键细节**：成功时，`new` 被存进了原子、被「共享」出去，所以结果里的 `new` 降级成 `Shared`；失败时，`new` 没写进去，所有权应当归还调用者，所以结果里的 `new` 保持原类型 `P`（你传 `Owned` 进去，失败后拿回的还是 `Owned`，**不会被吞掉**）。这一点让 CAS 循环可以复用同一个 `Owned` 反复重试。

#### 4.1.3 源码精读

返回类型定义在文件顶部，两个结构体都派生了 `Debug`：

[atomic.rs:22-47](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L22-L47) — 定义 `CompareExchangeValue<'g, T>`（成功）与 `CompareExchangeError<'g, T, P>`（失败）。注意 `Value` 里 `old`/`new` 都是 `Shared`，而 `Error` 里 `new: P` 保留泛型：

```rust
pub struct CompareExchangeValue<'g, T: ?Sized + Pointable> {
    pub old: Shared<'g, T>,   // 成功：原子里原本的值（== current）
    pub new: Shared<'g, T>,   // 成功：被存进去的值（已共享，故 Shared）
}

pub struct CompareExchangeError<'g, T: ?Sized + Pointable, P: Pointer<T>> {
    pub current: Shared<'g, T>, // 失败：原子里此刻的真实值
    pub new: P,                 // 失败：没写进去的 new，原类型归还
}
```

主体方法在 [atomic.rs:460-486](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L460-L486)：

```rust
pub fn compare_exchange<'g, P>(
    &self,
    current: Shared<'_, T>,
    new: P,
    success: Ordering,
    failure: Ordering,
    _: &'g Guard,                       // 仅用于把 'g 传染给返回值
) -> Result<CompareExchangeValue<'g, T>, CompareExchangeError<'g, T, P>>
where
    P: Pointer<T>,
{
    let new = new.into_ptr();           // Owned 会 mem::forget(self)，避免 drop
    self.data
        .compare_exchange(current.into_ptr(), new, success, failure)
        .map(|old| unsafe {             // 成功分支
            CompareExchangeValue {
                old: Shared::from_ptr(old),
                new: Shared::from_ptr(new),
            }
        })
        .map_err(|current| unsafe {     // 失败分支
            CompareExchangeError {
                current: Shared::from_ptr(current),
                new: P::from_ptr(new),  // 用 P::from_ptr 还原成原始类型
            }
        })
}
```

读法要点：
- `new.into_ptr()` 对 `Owned` 会执行 `mem::forget(self)`（见 4.4），把所有权「上交」给机器字，**避免 `Owned` 在函数末尾被 drop**。
- 成功分支把 `new` 用 `Shared::from_ptr(new)` 重新包出，语义是「这个对象现在被原子持有了，你只是共享地看一眼」。
- 失败分支用 `P::from_ptr(new)` 把机器字还原成调用者原始的 `P` 类型，所有权完整归还。
- `current.into_ptr()` 对 `Shared`（`Copy`）只是拷贝出 `data` 字段，无副作用。

`compare_exchange_weak` 的实现 [atomic.rs:544-570](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L544-L570) 与上面**逐行相同**，只是底层调用换成 `self.data.compare_exchange_weak(...)`。两者共享同一套返回类型与所有权语义。

#### 4.1.4 代码实践

**目标**：用单元测试验证「成功」与「失败」两条路径下返回值的内容。

**操作步骤**：阅读 [atomic.rs:1622-1690](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1622-L1690) 的两个测试 `compare_exchange_success` 与 `compare_exchange_failure`，理解断言；然后在 `crossbeam-epoch` 目录下运行：

```bash
cargo test --features std compare_exchange
```

**需要观察的现象**：
- `compare_exchange_success`：断言成功时 `result.old.deref() == &42`（旧值）、`result.new.deref() == &100`（新值），且失败后原子里真的是 `100`。
- `compare_exchange_failure`：先用 `swap` 把值从 `42` 改成 `200`，再用过期的 `current`（仍指向 42）做 CAS，断言失败时 `error.current.deref() == &200`（真实当前值）、`error.new == &300`（被退还的新值），且原子里仍是 `200`（未被错误覆盖）。

**预期结果**：两个测试均通过，证明失败路径不会把 `new` 写进原子、且把 `new` 原样还给调用者。

**若本地无法运行**：标注「待本地验证」，但通过阅读断言即可理解行为。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `CompareExchangeError` 的 `new` 字段类型是泛型 `P`，而不是像 `CompareExchangeValue::new` 那样固定为 `Shared`？

> **答案**：成功时 `new` 已经被写进原子、被其他线程共享，调用者不再独占，所以降级成 `Shared`；失败时 `new` 根本没写进去，所有权应当完整归还。把 `new` 类型保留成原始的 `P`（你传 `Owned` 拿回 `Owned`），调用者就能在重试循环里复用同一个 `Owned`，而不会因为一次失败就丢掉对象。

**练习 2**：在「只尝试一次、失败就放弃」的场景里，应该用 `compare_exchange` 还是 `compare_exchange_weak`？为什么？

> **答案**：用 `compare_exchange`。`weak` 版本允许「假失败」（比较相等也返回 `Err`），适合外面套着循环重试的场景以换取更高效的指令；只试一次时若用 `weak`，可能明明可以成功却因假失败而放弃，导致逻辑错误。

---

### 4.2 fetch_update 的循环 CAS 实现

#### 4.2.1 概念说明

裸 CAS 通常只改一次。但很多场景需要「**不断重试直到成功**」或「**根据当前值决定是否更新**」（比如把 tag 自增 1，但要保证读到的是最新值）。手写这个循环既啰嗦又容易写错（忘了用失败返回的 `current` 更新预期值，就会死循环或逻辑错乱）。

`fetch_update` 就是把这个「load → 计算新值 → CAS → 失败则用最新值重试」的模板固化成一个通用 API。它对应标准库 `AtomicUsize::fetch_update` 的语义，但作用对象是带 tag 的 `Atomic<T>` 指针。

#### 4.2.2 核心流程

`fetch_update(set_order, fail_order, guard, func)` 的伪代码：

```
prev = load(fail_order)                 // 先读一次当前值
while let Some(next) = func(prev):      // 调用闭包，返回 None 则直接退出
    match compare_exchange_weak(prev, next, set_order, fail_order):
        Ok(result) => return Ok(result.old)   // 成功，返回旧值
        Err(e)     => prev = e.current        // 失败，用真实当前值更新 prev，重试
return Err(prev)                        // 闭包返回 None，退出
```

要点：
- 闭包 `func` 接收当前 `Shared`，返回 `Option<Shared>`。返回 `None` 表示「不动了」，整个 `fetch_update` 立即返回 `Err(prev)`。
- 闭包**可能被调用多次**（每次 CAS 失败重试都会再调一次），但它对最终存进原子的值**只生效一次**。
- 返回 `Ok(result.old)` 是**更新前的旧值**（与新值由闭包逻辑决定）。

#### 4.2.3 源码精读

[atomic.rs:612-630](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L612-L630)：

```rust
pub fn fetch_update<'g, F>(
    &self,
    set_order: Ordering,
    fail_order: Ordering,
    guard: &'g Guard,
    mut func: F,
) -> Result<Shared<'g, T>, Shared<'g, T>>
where
    F: FnMut(Shared<'g, T>) -> Option<Shared<'g, T>>,
{
    let mut prev = self.load(fail_order, guard);
    while let Some(next) = func(prev) {
        match self.compare_exchange_weak(prev, next, set_order, fail_order, guard) {
            Ok(result) => return Ok(result.old),     // 成功，返回旧值
            Err(next_prev) => prev = next_prev.current, // 失败，用真实值更新 prev
        }
    }
    Err(prev)
}
```

读法要点：
- `set_order` 对应内部 CAS 的 `success` 排序，`fail_order` 对应 `failure` 排序（也用于首次 `load`）。约束与 `compare_exchange` 一致。
- 用的是 `compare_exchange_weak` 而非 `compare_exchange`——因为外面已经有重试循环了，假失败只是多转一圈，不影响正确性，却能换取更高效的指令。
- 失败时 `prev = next_prev.current`，**用 CAS 失败返回的真实当前值更新预期值**，避免基于过期值死循环。这是写 CAS 循环最容易漏掉的一步，`fetch_update` 帮你兜底了。

#### 4.2.4 代码实践

**目标**：阅读 `fetch_update` 的文档示例与签名，确认「闭包返回 `None` 时整体返回 `Err`」。

**操作步骤**：阅读 [atomic.rs:596-611](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L596-L611) 的文档示例：

```rust
let a = Atomic::new(1234);
let guard = &epoch::pin();
let res1 = a.fetch_update(SeqCst, SeqCst, guard, |x| Some(x.with_tag(1)));
assert!(res1.is_ok());
let res2 = a.fetch_update(SeqCst, SeqCst, guard, |x| None);
assert!(res2.is_err());
```

**需要观察的现象**：`res1` 成功（闭包返回 `Some`）；`res2` 失败（闭包返回 `None`，CAS 都没尝试，直接 `Err(prev)`）。

**预期结果**：理解 `fetch_update` 的成功/失败完全由闭包是否返回 `Some` 决定；只要闭包恒返回 `Some`，最终一定 `Ok`（在无内存溢出的前提下，CAS 总会在某次竞争胜出）。

**若本地无法运行**：阅读断言即可，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`fetch_update` 内部为什么用 `compare_exchange_weak` 而不是 `compare_exchange`？

> **答案**：因为外面已经是 `while` 重试循环，假失败只会让循环多转一次，不影响正确性；而 `weak` 在某些平台能编译成更轻量的指令，在循环里用更划算。

**练习 2**：如果你在闭包里捕获了一个外部变量做累加（如 `let mut n = 0; |x| { n += 1; Some(...) }`），`n` 的最终值是否等于「成功写入的次数」？

> **答案**：不等于。`n` 等于「闭包被调用的次数」，而闭包可能因 CAS 失败被多次调用却只成功一次。文档明确指出：「函数可能被调用多次……但对存储的值只生效一次」。所以 `n` 反映的是竞争激烈程度，不是写入次数。

---

### 4.3 fetch_and / fetch_or / fetch_xor 的 tag 位运算

#### 4.3.1 概念说明

有时你只想**就地修改 tag 的一位或几位**，而不关心指针指向哪个对象——比如在无锁链表里用 tag=1 标记一个节点「逻辑删除」，用一次原子的 `fetch_or(1)` 就能完成，不需要读出指针、改 tag、再 CAS 回去（那样还要处理竞争）。

`fetch_and` / `fetch_or` / `fetch_xor` 就是这三条「对 tag 做按位与/或/异或」的原子读改写操作。它们的**核心约束**是：**只动 tag 低位，绝不能破坏指针的高位地址**。为此，库在调用底层位运算前对参数做了一层掩码处理。

#### 4.3.2 核心流程

记 `L = low_bits::<T>()`（仅 tag 位为 1 的掩码），`ptr` 为当前机器字，`addr` 为其地址整数，`tag = addr & L`。三者用不同的掩码确保指针高位不变：

- **fetch_or(val)**：先把参数截断成 `val' = val & L`（清掉所有高位），再执行 `ptr | val'`。
  效果：高位 `addr & !L` 与 0 或→不变；tag 变成 `tag | val'`。
  掩码设计：`val & L`。

- **fetch_xor(val)**：同理 `val' = val & L`，执行 `ptr ^ val'`。
  效果：高位与 0 异或→不变；tag 变成 `tag ^ val'`。

- **fetch_and(val)**：这一条最反直觉。若直接 `val & L` 再 `ptr & val'`，高位会与 0 相与→**指针被清零**！所以它必须把**高位全部置 1**：`val' = val | !L`，再执行 `ptr & val'`。
  效果：高位与 1 相与→不变；tag 变成 `tag & (val & L)`。

用公式概括（设 \(L\) 为 tag 掩码，\(\&,\mid,\oplus\) 为按位与/或/异或）：

\[
\begin{aligned}
\text{fetch\_or}(v) &: \quad \text{ptr} \;\big|\; (v \;\&\; L) \\
\text{fetch\_xor}(v) &: \quad \text{ptr} \;\oplus\; (v \;\&\; L) \\
\text{fetch\_and}(v) &: \quad \text{ptr} \;\&\; (v \;\big|\; \neg L)
\end{aligned}
\]

三条都返回**操作前的旧 `Shared`**（其 `tag()` 即旧 tag）。

另一个工程要点：底层位运算有**两条实现路径**。`AtomicPtr::fetch_and/or/xor` 保持指针 provenance（来源信息），是 strict-provenance 兼容的，但需要较新的 Rust（1.91）；而把 `AtomicPtr` 强转成 `AtomicUsize` 再 `fetch_*` 的写法走的是「地址整数」语义，在 permissive-provenance 模型下仍然 sound。库的策略是：**miri 下用前者（严格），非 miri 下用后者（兼容旧版本）**。

#### 4.3.3 源码精读

`fetch_and` 在 [atomic.rs:651-668](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L651-L668)：

```rust
pub fn fetch_and<'g>(&self, val: usize, order: Ordering, _: &'g Guard) -> Shared<'g, T> {
    let val = val | !low_bits::<T>();   // 关键：高位全置 1，保护指针
    #[cfg(miri)]
    unsafe { Shared::from_ptr(self.data.fetch_and(val, order)) }
    #[cfg(not(miri))]
    unsafe {
        Shared::from_ptr(
            (*(&self.data as *const AtomicPtr<_> as *const AtomicUsize))
                .fetch_and(val, order) as *mut (),
        )
    }
}
```

读法要点：
- `val | !low_bits::<T>()` 是 `fetch_and` 的灵魂：`!low_bits` 让所有指针高位变 1，与指针相与后不变；只有 tag 位真正受 `val` 影响。
- `#[cfg(miri)]` 分支直接用 `AtomicPtr::fetch_and`，保留 provenance；`#[cfg(not(miri))]` 把 `&AtomicPtr` 强转成 `&AtomicUsize` 后调用，注释说明这是为了兼容 MSRV（`AtomicPtr::fetch_*` 需要 Rust 1.91）。

`fetch_or` 在 [atomic.rs:689-706](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L689-L706)，掩码换成 `val & low_bits::<T>()`：

```rust
pub fn fetch_or<'g>(&self, val: usize, order: Ordering, _: &'g Guard) -> Shared<'g, T> {
    let val = val & low_bits::<T>();    // 只保留 tag 位，清掉高位
    /* 同样的 miri / 非 miri 双分支 */
}
```

`fetch_xor` 在 [atomic.rs:727-744](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L727-L744)，掩码与 `fetch_or` 相同（`val & low_bits::<T>()`）。三者结构完全对称，区别仅在掩码方向。

> 三者的文档示例（如 `a.fetch_or(2, SeqCst, guard).tag() == 1` 而 `a.load(...).tag() == 3`）正好印证：`fetch_or` 返回**旧** tag，load 出来才是**新** tag。

#### 4.3.4 代码实践

**目标**：验证「`fetch_and` 用 `val | !low_bits` 保护指针高位」这一设计，并观察它返回的是旧值。

**操作步骤**：在 `crossbeam-epoch` 目录运行 `fetch_or` 的文档示例（可写成一个临时测试或直接跑 doctest）：

```bash
cargo test --features std --doc atomic::Atomic::fetch_or
```

**需要观察的现象**：`Atomic::<i32>::from(Shared::null().with_tag(1))` 初始 tag=1；`fetch_or(2)` 后返回的旧 `Shared` 的 `tag()` 是 1，但紧接着 `load` 出来的 `tag()` 是 3（`1 | 2`）。指针部分始终为 null（未受影响）。

**预期结果**：旧值 tag=1、新值 tag=3，证明 `fetch_or` 只改 tag、返回旧值、且 `val & low_bits` 把高位参数清零，指针未被破坏。

**若本地无法运行**：标注「待本地验证」，通过阅读 [atomic.rs:680-688](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L680-L688) 的断言即可理解。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `fetch_and` 用 `val | !low_bits`，而 `fetch_or` / `fetch_xor` 用 `val & low_bits`？如果 `fetch_and` 也用 `val & low_bits` 会怎样？

> **答案**：`or`/`xor` 与 0 运算保持不变，所以清掉高位参数（`& low_bits`）后，指针高位不受影响。但 `and` 与 0 会清零，若 `fetch_and` 也用 `& low_bits`，高位参数全是 0，`ptr & 0` 会把指针地址整个清掉，破坏指针。所以 `fetch_and` 必须把高位参数置 1（`| !low_bits`），让高位「与 1 不变」。

**练习 2**：`Atomic::<i8>` 的 `low_bits` 是多少？对它调用 `fetch_or(1)` 有效果吗？

> **答案**：`i8` 对齐为 1，`trailing_zeros(1) = 0`，`low_bits = (1 << 0) - 1 = 0`。所以 `fetch_or(1)` 实际执行 `ptr | (1 & 0) = ptr | 0`，**没有任何效果**——`i8` 没有可用的 tag 位（与 u2-l5 的结论一致）。

---

### 4.4 Pointer sealed trait（Owned / Shared 共有）

#### 4.4.1 概念说明

回到本讲反复出现的 `P: Pointer<T>` 约束。`compare_exchange`、`store`、`swap` 这些写入类操作的 `new` 参数都希望**既能接收 `Owned`（交出所有权），也能接收 `Shared`（只是借用）**。如果没有统一抽象，每个方法都得为 `Owned` 和 `Shared` 各写一份几乎相同的代码。

`Pointer<T>` 就是这个统一抽象：它把「带 tag 的指针」在 `Owned`/`Shared` 与裸机器字 `*mut ()` 之间互相转换。但它**只在库内部使用**——外部用户不能给自己的类型实现 `Pointer`，否则就可能把非法指针塞进 `Atomic`。为此它采用 **sealed trait（密封 trait）** 模式：`Pointer` 继承一个空 trait `Sealed`，而 `Sealed` 在 `lib.rs` 的私有 `sealed` 模块里定义，外部既看不到也实现不了。

#### 4.4.2 核心流程

`Pointer<T>` 提供两个方法：

- `fn into_ptr(self) -> *mut ()`：**消费** self，取出机器字。
  - `Owned::into_ptr`：取 `data` 字段后 `mem::forget(self)`，避免 `Owned` 的 `Drop` 把对象释放掉（所有权已转交机器字）。
  - `Shared::into_ptr`：直接返回 `self.data`（`Shared` 是 `Copy`，无需 forget）。
- `unsafe fn from_ptr(data: *mut ()) -> Self`：把机器字还原回指针类型。
  - `Owned::from_ptr`：重建 `Owned`（带 `debug_assert!(!data.is_null())`）。
  - `Shared::from_ptr`：重建 `Shared`。
  - 契约：`data` 必须来自 `into_ptr`，且一个 `data` 不能被 `from_ptr` 多次还原（否则 `Owned` 会双重释放）。

密封机制：`lib.rs` 里有：

```rust
mod sealed { pub trait Sealed {} }
```

`Pointer` 定义为 `pub trait Pointer<T>: crate::sealed::Sealed`，而 `Sealed` 仅由 `Owned` 和 `Shared` 在库内部实现。由于 `Sealed` 是私有的，外部 crate 无法实现它，自然也就无法实现 `Pointer`。

#### 4.4.3 源码精读

`Sealed` 在 [lib.rs:180-182](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L180-L182)：

```rust
mod sealed {
    pub trait Sealed {}
}
```

`Pointer` 定义在 [atomic.rs:925-939](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L925-L939)：

```rust
pub trait Pointer<T: ?Sized + Pointable>: crate::sealed::Sealed {
    fn into_ptr(self) -> *mut ();
    unsafe fn from_ptr(data: *mut ()) -> Self;
}
```

`Owned` 的实现 [atomic.rs:952-974](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L952-L974)：

```rust
impl<T: ?Sized + Pointable> crate::sealed::Sealed for Owned<T> {}
impl<T: ?Sized + Pointable> Pointer<T> for Owned<T> {
    fn into_ptr(self) -> *mut () {
        let data = self.data;
        mem::forget(self);   // 关键：阻止 Drop 释放对象
        data
    }
    unsafe fn from_ptr(data: *mut ()) -> Self {
        debug_assert!(!data.is_null(), "converting null into `Owned`");
        Self { data, _marker: PhantomData }
    }
}
```

`Shared` 的实现 [atomic.rs:1210-1224](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1210-L1224)：

```rust
impl<T: ?Sized + Pointable> crate::sealed::Sealed for Shared<'_, T> {}
impl<T: ?Sized + Pointable> Pointer<T> for Shared<'_, T> {
    fn into_ptr(self) -> *mut () { self.data }        // Copy，无需 forget
    unsafe fn from_ptr(data: *mut ()) -> Self {
        Shared { data, _marker: PhantomData }
    }
}
```

读法要点：
- `into_ptr` 的差异是 `Owned` 必须 `mem::forget`（否则函数结束时 `Owned` 被 drop，对象就没了），`Shared` 是 `Copy` 无需此举。
- `from_ptr` 是 `unsafe` 的：契约要求 `data` 必须合法、且不能被多次还原（尤其 `Owned::from_ptr` 两次会双重释放）。
- 正是因为有了 `Pointer`，`compare_exchange` 的失败分支才能写出 `new: P::from_ptr(new)`——**按调用者原始类型重建 `new` 并归还**，这是 4.1 所有权语义的底层支撑。

#### 4.4.4 代码实践

**目标**：通过阅读源码确认 `Pointer` 的密封性，并理解 `Owned::into_ptr` 为何要 `mem::forget`。

**操作步骤**：
1. 在你自己的实验 crate 里（依赖 `crossbeam-epoch`）尝试为自定义类型实现 `Pointer`：

```rust
// 示例代码（预期编译失败）
use crossbeam_epoch::Pointer;
struct MyPtr;
impl<T> Pointer<T> for MyPtr { /* ... */ } // 期望报错
```

**需要观察的现象**：编译器报错，提示 `Sealed` 是私有的 / trait `Pointer` 不能被外部实现（因为缺少 `Sealed` 的实现）。

**预期结果**：编译失败，证明 sealed 模式确实阻止了外部实现。

2. 阅读上文 [atomic.rs:955-959](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L955-L959) 的 `Owned::into_ptr`，思考：若删掉 `mem::forget(self)` 这一行，`compare_exchange` 传入 `Owned` 时会发生什么？

**预期结论**：对象会在 `into_ptr` 返回后被 `Owned::drop` 立即释放，之后原子里存的就是悬垂指针——这就是 `mem::forget` 必须存在的原因。

**若本地无法运行**：标注「待本地验证」，但结论可由阅读 `Drop for Owned`（[atomic.rs:1100-1107](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1100-L1107)）直接推出。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `Pointer` 要做成 sealed，而不是普通的公开 trait？

> **答案**：`Pointer` 直接操作裸机器字 `*mut ()`，一旦让外部类型实现，就可能把非法、未对齐或伪造的指针塞进 `Atomic<T>`，破坏内存安全。sealed 模式保证只有库内部受控的 `Owned` 和 `Shared` 能实现它，从源头杜绝误用。

**练习 2**：`Owned::into_ptr` 用 `mem::forget(self)`，`Shared::into_ptr` 不用。为什么？

> **答案**：`Owned` 拥有对象、有 `Drop`，若不 `forget`，`into_ptr` 结束时 `Owned` 被 drop、对象被释放，机器字变成悬垂指针；`Shared` 是 `Copy`（无 `Drop`、不拥有对象），`into_ptr` 只是拷贝出 `data` 字段，不会触发任何释放，所以无需 `forget`。

---

## 5. 综合实践

**任务**：用两种方式实现「把 `Atomic<u64>` 的 tag 原子地自增 1 并在到达上限后回绕」——版本 A 手写 `compare_exchange_weak` 循环，版本 B 用 `fetch_update`。然后对比可读性。

**背景**：`u64` 对齐为 8，`low_bits = 7`（3 个 tag 位），tag 范围 \([0,7]\)。回绕即 `(tag + 1) & 7`。本例刻意用 `Atomic::<u64>::null()`（null 指针 + 纯 tag），这样不涉及堆分配与 `unsafe` 解引用，纯粹练习 tag 机器字的 CAS/位运算。

**示例代码**（请在你自己的实验 crate 中运行）：

```rust
use crossbeam_epoch::{self as epoch, Atomic, Shared};
use std::sync::atomic::Ordering::SeqCst;

const TAG_MASK: usize = 0b111; // u64: align 8 -> 3 个 tag 位

/// 版本 A：手写 compare_exchange_weak 循环
fn tag_inc_cas(a: &Atomic<u64>, guard: &epoch::Guard) -> usize {
    let mut curr: Shared<'_, u64> = a.load(SeqCst, guard);
    loop {
        let next = curr.with_tag((curr.tag() + 1) & TAG_MASK);
        match a.compare_exchange_weak(curr, next, SeqCst, SeqCst, guard) {
            Ok(result) => return result.new.tag(), // 成功：返回新 tag
            Err(e) => curr = e.current,           // 失败：用真实当前值重试
        }
    }
}

/// 版本 B：fetch_update 一行表达意图
fn tag_inc_fetch(a: &Atomic<u64>, guard: &epoch::Guard) -> usize {
    let prev = a
        .fetch_update(SeqCst, SeqCst, guard, |p| {
            Some(p.with_tag((p.tag() + 1) & TAG_MASK))
        })
        .unwrap(); // 闭包恒返回 Some，故必 Ok；返回的是旧值
    (prev.tag() + 1) & TAG_MASK // 由旧 tag 推出新 tag
}

fn main() {
    let a = Atomic::<u64>::null(); // 初始：null + tag 0
    let guard = &epoch::pin();

    for i in 0..10 {
        let t = if i % 2 == 0 {
            tag_inc_cas(&a, guard)
        } else {
            tag_inc_fetch(&a, guard)
        };
        println!("第 {i} 次，新 tag = {t}");
    }

    // 10 次自增后，tag 应为 10 & 7 = 2（回绕过一次）
    assert_eq!(a.load(SeqCst, guard).tag(), 10 & TAG_MASK);
    println!("OK，最终 tag = {}", a.load(SeqCst, guard).tag());
}
```

**操作步骤**：
1. 新建一个 binary crate，在 `Cargo.toml` 添加 `crossbeam-epoch` 依赖（路径依赖或 crates.io 均可）。
2. 把上面的代码放入 `src/main.rs`，运行 `cargo run`。
3. 再用 `cargo run` 单线程跑，多线程呢？尝试在 2 个线程里并发调用 `tag_inc_cas` / `tag_inc_fetch` 各 10000 次，最后 tag 应为 `(20000) & 7`。

**需要观察的现象**：
- 单线程：依次打印 tag = 1,2,…,7,0,1，最终断言 `tag == 2` 通过。
- 多线程：因为有竞争，CAS 会失败重试，但**最终 tag 一致**（等于总次数 mod 8）。

**可读性对比（关键结论）**：
- 版本 A（`compare_exchange_weak`）更啰嗦：要手动维护 `mut curr`、写 `loop`、处理 `Ok`/`Err` 两个分支、记得在失败时 `curr = e.current`。但它**控制力最强**——你可以在循环里加日志、统计重试次数、或在某些条件下提前退出。
- 版本 B（`fetch_update`）更易读：意图「把当前值的 tag 改成 `(tag+1)&7`」用一行闭包直接表达，循环与重试由库兜底。**在不需要精细控制的场景，版本 B 明显更优**；这也是 `fetch_update` 存在的意义——封装最常见的 CAS 循环模板。

**预期结果**：两版本行为等价，版本 B 代码量约为版本 A 的一半且意图更清晰。

**若本地无法运行**：标注「待本地验证」，但可读性结论与回绕逻辑可通过阅读 [atomic.rs:612-630](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L612-L630) 的 `fetch_update` 实现直接得出。

## 6. 本讲小结

- `compare_exchange` / `compare_exchange_weak` 比较的是**完整机器字**（指针 + tag），成功返回 `CompareExchangeValue{old, new}`（new 降级为 `Shared`），失败返回 `CompareExchangeError{current, new}`（new 保持原类型 `P` 归还）；`weak` 允许假失败，适合循环重试。
- `fetch_update` 是对 `compare_exchange_weak` 的循环封装，闭包返回 `None` 则整体 `Err`、返回 `Some` 则重试到成功；闭包可能被调用多次但只生效一次。
- `fetch_and` / `fetch_or` / `fetch_xor` 只动 tag 低位：`or`/`xor` 用 `val & low_bits` 清高位，`and` 用 `val | !low_bits` 保高位；三者返回旧 `Shared`。
- miri 下用 `AtomicPtr::fetch_*`（strict-provenance），非 miri 下用 `AtomicUsize::fetch_*`（兼容 MSRV），注释指出 `AtomicPtr::fetch_*` 需要 Rust 1.91。
- `Pointer<T>` 是 sealed trait，统一 `Owned`/`Shared` 与机器字的互转；`Owned::into_ptr` 必须 `mem::forget` 防止对象被提前 drop，这是 CAS 失败时能原样归还 `Owned` 的底层支撑。

## 7. 下一步学习建议

本讲讲清了 `Atomic` 上的所有读改写操作。接下来：

- 进入 **u3 单元（Guard 与延迟回收）**：CAS 操作的对象在「逻辑移除」后如何安全回收？`defer` / `defer_destroy` 正是配合 CAS 使用的延迟回收 API，是本讲 CAS 循环的自然下游。
- 在学完 u3 后，回头看本讲的「综合实践」里若把 `Atomic<u64>::null()` 换成真实分配的节点（如链表节点），就会用到 `defer_destroy` 回收被 CAS 替换掉的旧节点。
- 如果想看 CAS 在真实无锁数据结构里的应用，可提前浏览 **u6-l20（无锁链表）** 与 **u6-l21（Michael-Scott 队列）**，它们大量使用 `compare_exchange_weak` + `fetch_or`（逻辑删除标记）+ `defer_destroy`，是本讲所有概念的综合演练场。
