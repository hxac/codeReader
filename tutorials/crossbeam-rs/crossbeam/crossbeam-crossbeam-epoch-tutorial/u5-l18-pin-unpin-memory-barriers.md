# pin / unpin 与内存屏障（含 x86 优化 hack）

## 1. 本讲目标

本讲深入 `Local::pin` 与 `Local::unpin` 的内部实现，这是 epoch-based 内存回收（EBR）正确性的「心脏」。学完后你应该能够：

- 说清 `pin()` 何时才「真正」pin（不是每次创建 `Guard` 都做重活），以及它写入了什么、为什么。
- 解释为什么写入本地 epoch 之后、任何 `Atomic::load` 之前，**必须**有一条 `SeqCst` 内存屏障；以及去掉它会在弱内存模型（ARM/POWER）上造成什么样的数据竞争。
- 理解 x86 分支用 `compare_exchange(SeqCst, SeqCst)` 替代 `mfence` 的「hack」、它的形式化风险、以及 `compiler_fence` 的补救作用。
- 解释周期性 `collect`（每 128 次 pin 触发一次）如何在没有专用 GC 线程的情况下摊销式地回收垃圾。
- 解释 `unpin()` 如何在最后一个 guard 退出时清空本地 epoch，以及它在 `handle_count == 0` 时如何触发 `finalize`。

本讲不重复 u3-l9（`Guard` 的可重入语义）与 u5-l17（epoch 的位编码）的内容，而是把它们拼接成一条完整的「pin → 屏障 → 读共享对象 / unpin → 推进 → 回收」安全链路。

## 2. 前置知识

读本讲前，请先建立以下直觉（前置讲义已覆盖）：

- **EBR 的核心难题**：无锁数据结构里「逻辑删除」与「物理释放」必须分离。一个被摘除的对象可能正被别的线程通过 `Shared` 指针读着，必须等「宽限期」过后才能 drop（u1-l1、u1-l3）。
- **Guard 是 pin 的凭证**：`Guard` 只是一根 `*const Local` 裸指针，真正的 pin 状态住在 `Local` 里。pin 可重入——只有第一个 guard 才真正 pin，最后一个 guard 才真正 unpin（u3-l9）。
- **epoch 的位编码**：`Epoch` 用一个机器字的最低位（LSB）表示「是否被 pin」，高位是代号。`pinned()` 置 LSB、`unpinned()` 清 LSB、`successor()` 是 `wrapping_add(2)`（加 2 是为了不触碰 LSB）（u5-l17）。
- **宽限期判据**：一个在全局 epoch `g` 盖戳的垃圾袋，只有当全局 epoch 推进到 `g + 2`（`wrapping_sub >= 2`）后才可回收（u4-l16）。
- **内存序基础**：`Relaxed`、`Release`、`Acquire`、`SeqCst` 的基本含义；知道「弱内存模型 CPU（ARM/POWER）允许 store-load 重排，x86 几乎只允许 store-load 重排」。

下面用到的两条关键不变量，贯穿全讲：

> **不变量 A（公告在读之前）**：参与者必须先把自己「已 pin 在 epoch g」这件事**写入本地 epoch 并全局可见**，然后才能去 `load` 任何共享对象。
>
> **不变量 B（回收在公告之后）**：回收线程推进全局 epoch 前，必须能**观察到**所有参与者的本地 epoch；只有当所有「被 pin」的参与者都钉在当前全局 epoch 时，才允许推进。

本讲的全部屏障代码，都是为了在硬件层面强制这两条不变量。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/internal.rs` | `Local`（参与者）与 `Global`（收集器全局数据）的实现。本讲主角是 `Local::pin` / `Local::unpin` / `Local::finalize`，以及 `Global::collect` / `Global::try_advance` 的被调用方视角。 |
| `src/epoch.rs` | `Epoch` 与 `AtomicEpoch` 的数据表示层（u5-l17 已详解），本讲只用到 `pinned()` / `starting()` / `compare_exchange()`。 |
| `src/guard.rs` | `Guard::drop` 调用 `Local::unpin`，是 unpin 的唯一入口。 |
| `src/collector.rs` | `LocalHandle::pin` 与 `LocalHandle::drop`，展示 pin/unpin 的用户侧调用链。 |

## 4. 核心概念与源码讲解

### 4.1 pin：只有第一个 guard 才真正 pin

#### 4.1.1 概念说明

对用户而言，每次 `epoch::pin()` 都返回一个 `Guard`；但 EBR 不能容忍「每次都做一次完整的内存屏障 + 原子写」的开销。于是 `pin` 被设计成**可重入的轻量路径**：同一个线程在已 pin 的情况下再次 `pin()`，只递增一个 `Cell` 计数器就返回；只有从「未 pin」到「已 pin」的第一次跨越，才执行写 epoch + 屏障这套重活。

`Local` 里负责记账的是两个字段（都是 `Cell`，因为只有拥有该 `Local` 的线程会访问，无需原子）：

[src/internal.rs:L305-L314](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L305-L314) —— `guard_count`（当前有多少个活 guard 在钉着本线程）、`handle_count`（当前有多少张 `LocalHandle` 会员卡）、`pin_count`（历史 pin 总次数，仅用于周期触发回收）。

#### 4.1.2 核心流程

`Local::pin` 的总骨架（细节在 4.2/4.3 展开）：

```text
pin():
    guard = Guard { local: self }          // 先造好凭证
    guard_count = self.guard_count.get()    // 读取旧值
    self.guard_count.set(guard_count + 1)   // 计数 +1（无条件）

    if guard_count == 0:                    // 旧值是 0 ⇒ 这是第一个 guard
        # —— 真正的 pin 重活 ——
        global_epoch = global.epoch.load(Relaxed)
        new_epoch = global_epoch.pinned()
        把 new_epoch 写进 self.epoch，并执行 SeqCst 屏障   # 见 4.2 / 4.3
        pin_count += 1
        if pin_count % 128 == 0:            # 见 4.4
            global.collect(&guard)

    return guard
```

注意判据是 `guard_count == 0`，即**递增前的旧值**为 0。这与 u3-l9 讲的「可重入」完全对齐：第一个 guard 进来时旧值是 0，触发重活；后续 guard 旧值 ≥ 1，只递增计数。

#### 4.1.3 源码精读

入口在 `Local::pin`：

[src/internal.rs:L401-L408](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L401-L408) —— 构造 `Guard`、无条件递增 `guard_count`。

[src/internal.rs:L409-L411](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L409-L411) —— `guard_count == 0` 分支：读全局 epoch 并计算 `new_epoch = global_epoch.pinned()`，准备写入本地 epoch。

`pinned()` 来自 epoch 表示层，就是把 LSB 置 1：

[src/epoch.rs:L62-L68](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/epoch.rs#L62-L68) —— `Epoch::pinned`，`data | 1`。

#### 4.1.4 代码实践

**目标**：观察「可重入 pin 只有第一次做重活」。

**步骤**：

1. 在 `Local::pin` 的 `if guard_count == 0 {` 分支开头，**临时**加一行 `eprintln!("[pin] real pin, pin_count before = {}", self.pin_count.get().0);`（仅本地学习用，不要提交）。
2. 写一个测试：连续调用 5 次 `let g = epoch::pin();`，把它们存进一个 `Vec`，然后一起 drop。
3. 运行 `cargo test -- --nocapture`。

**预期**：`[pin] real pin` 只打印 **1 次**（第一次），而不是 5 次——印证可重入路径的轻量性。

> 待本地验证：实际打印次数取决于测试是否触发了线程退出/重新注册；只要 guard 全程在同一线程的同一 `Local` 上累积，就应只打印一次。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `if guard_count == 0` 改成 `if guard_count <= 1`，会发生什么？

**答案**：第二个 guard 也会被误判为「第一个」，导致重复写 epoch + 屏障，并在该 guard drop 时，由于 `unpin` 用 `guard_count == 1` 判定最后一个，计数会错乱——可能在线程仍被 pin（还有别的活 guard）时就清空了本地 epoch，破坏不变量 A，进而引发 use-after-free。

**练习 2**：为什么 `guard_count` 用 `Cell` 而不是 `AtomicUsize`？

**答案**：`Guard` 是 `!Send + !Sync`，一个 `Local` 只会被它所属的那一个线程访问，不存在并发读写，所以用无需同步开销的 `Cell` 即可（见 u3-l9、u4-l15）。

---

### 4.2 pin 的内存屏障：为什么 SeqCst fence 必须存在

#### 4.2.1 概念说明

这是本讲最核心、也最容易被跳过的一步。在 `new_epoch` 写入本地 epoch 之后，代码刻意插入了一条 `SeqCst`（顺序一致）内存屏障。源码注释一语中的：

> "The fence makes sure that any future loads from `Atomic`s will not happen before this store."

为什么要挡住「后续 load 跑到这次 store 前面」？因为这两个动作的先后，直接决定了回收线程能否安全地推进 epoch。

回顾不变量 A 和 B，把它们的时序拼起来：

- 参与者 P 想安全地读一个共享对象 O，前提是「回收线程在 P 读 O 的整个期间，不会把 O 释放」。
- 回收线程 R 推进 epoch 的条件是「所有被 pin 的参与者都钉在当前全局 epoch」。
- 所以 P 必须先**公告**「我 pin 在 epoch g 了」，**然后**再去读 O。这样 R 在推进前查 P 的本地 epoch 时，一定能看到 P 钉在 g，从而不会一口气推进两步去释放 O。

屏障的作用，就是确保「公告（store 本地 epoch）」在硬件/编译器层面**真地排在**「读共享对象（load）」之前，不被重排、不被延迟到 store buffer 里。

#### 4.2.2 核心流程

完整的「公告 + 屏障」语义路径（非 x86 或 miri 下）：

```text
global_epoch = global.epoch.load(Relaxed)     # (a) 读全局时钟 g
new_epoch    = global_epoch.pinned()          #     组装「pin 在 g」
self.epoch.store(new_epoch, Relaxed)          # (b) 公告：我 pin 在 g 了
atomic::fence(SeqCst)                         # (c) ★ 屏障 ★
# —— 之后用户才能用 guard 去 Atomic::load ——   # (d) 读共享对象
```

屏障 (c) 把 (b) 和 (d) 之间的「store-load 重排」彻底堵死。配合回收侧（`try_advance` / `push_bag` 里的 `SeqCst` 屏障），双方共同进入同一条 `SeqCst` 全序，使得「R 没看到 P 公告 ⇒ R 推进 ⇒ 释放 O」与「P 已经在读 O」不可能同时成立。

#### 4.2.3 源码精读

[src/internal.rs:L413-L416](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L413-L416) —— 注释明确点出屏障的目的：阻止后续 `Atomic` 的 load 被重排到这次 store 之前。

[src/internal.rs:L445-L448](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L445-L448) —— 非 x86 / miri 路径：`self.epoch.store(new_epoch, Relaxed)` + `atomic::fence(SeqCst)`。注意 store 用的是 `Relaxed`——因为这次的「同步力」全靠紧跟其后的 `SeqCst` 屏障提供，store 本身不需要额外的 release 语义。

回收侧的对偶屏障在 `Global::try_advance`：

[src/internal.rs:L237-L239](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L237-L239) —— R 先读全局 epoch，再插一条 `SeqCst` 屏障，然后才遍历各 `Local` 读它们的本地 epoch。这条屏障与 P 侧的 (c) 共同落在同一条 SeqCst 全序里，是「R 能正确看到 P 的公告」的形式化保障。

#### 4.2.4 代码实践

**目标**：用自己的话把「屏障为什么必须在这个位置」讲清楚——这是本讲规格指定的核心实践。

**步骤**：

1. 阅读上面 [src/internal.rs:L413-L416](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L413-L416) 的注释，以及 `try_advance` 的两条屏障。
2. 用一段话（建议 150–250 字）解释：**为什么 fence 必须在写完 local epoch 之后、而在任何 `Atomic::load` 之前？如果去掉它，在弱内存模型下会出现什么数据竞争？**

**预期答案要点**（写完再对照）：

> 屏障之前是「公告」（把 `local_epoch = g.pinned()` 写到内存），屏障之后是「使用」（用 guard 去 `load` 共享对象指针）。屏障强制公告先于使用，全局可见。若去掉它，在 ARM/POWER 这类允许 store-load 重排的弱模型上，CPU 可能把后续对共享对象的 load 提前到公告之前执行。于是：参与者 P 已经开始读对象 O，但「我 pin 在 g」的公告还压在 store buffer 里没可见；回收线程 R 这一刻读 P 的本地 epoch，看到的是旧值（未 pin），便放心地把全局 epoch 从 g 推进到 g+2，回收掉在 g 产生的、指向 O 的垃圾袋，把 O 释放。P 手里的指针随即成为悬垂指针——这就是 use-after-free，本质是一次跨线程的数据竞争。屏障的存在让 R 不可能在「没看到 P 公告」的情况下推进两步，从而保住 O 的生命期。

#### 4.2.5 小练习与答案

**练习 1**：把 store 的 `Ordering::Relaxed` 换成 `Ordering::Release`，能去掉后面的 `SeqCst` fence 吗？

**答案**：不能。`Release` store 只能阻止「之前的读写」被重排到 store 之后，无法阻止「之后的 load」被提前；而且 release-acquire 只建立两点间的 happens-before，不进入 SeqCst 全序，无法与回收侧的 SeqCst 屏障正确配对。这里需要的是「后续 load 不准提前」+「全局全序」，只有 `SeqCst` fence 能同时满足。

**练习 2**：`try_advance` 里读各 `Local` 的 `local_epoch` 用的是 `Relaxed`，为什么不加 barriers 也敢用 Relaxed？

**答案**：因为 `try_advance` 在遍历前有一条 `SeqCst` 屏障、遍历后有一条 `Acquire` 屏障，单次 `Relaxed` load 的「弱」被这两条夹击的屏障补足了。屏障提供同步语义，单点 load 只负责取值，故用 Relaxed 即可。

---

### 4.3 x86 优化 hack：用 compare_exchange 代替 mfence

#### 4.3.1 概念说明

4.2 讲的是「标准路径」。但 crossbeam-epoch 在 x86/x86_64 上（且不在 miri 下）走的是另一条路：用一次 `compare_exchange(SeqCst, SeqCst)` 来代替 `fence(SeqCst)`。这纯粹是一个**性能 hack**。

背景：x86 是 TSO（Total Store Order）模型，几乎只有一种重排——store 后面跟一个对不同地址的 load，load 可能被观察到先于 store 执行（store-load 重排）。要在 x86 上得到一个「全屏障」（同时挡住 store-load），有两条指令可选：

1. `mfence` —— `atomic::fence(SeqCst)` 在 x86 上编译出的指令。
2. `lock cmpxchg`（或任何 `lock` 前缀指令）—— `compare_exchange(SeqCst, SeqCst)` 编译出的指令。

两者在 x86 硬件上都是「全屏障」，但 crossbeam 的实测发现：**在这个特定的 pin 热路径上，`lock cmpxchg` 比 `mfence` 更快**。于是代码用 compare_exchange 兼做「写 epoch + 全屏障」两件事。

#### 4.3.2 核心流程

x86 路径的精妙之处：它不是「store + fence」，而是直接对 `self.epoch` 做一次 CAS，**期望旧值是 `starting()`（未 pin）、写入 `new_epoch`（已 pin）**：

```text
# x86 / 非 miri 路径
current = Epoch::starting()                                  # 期望：未 pin
res = self.epoch.compare_exchange(
        current, new_epoch, SeqCst, SeqCst)                  # -> lock cmpxchg（全屏障）
debug_assert!(res.is_ok(), "participant was expected to be unpinned")
atomic::compiler_fence(SeqCst)                               # 仅防编译器重排
```

为什么 CAS 必然成功？因为进入这个分支意味着 `guard_count` 刚才是 0（第一个 guard），而上一次 `unpin` 已经把 `self.epoch` 写回了 `Epoch::starting()`。所以此刻 `self.epoch` 一定是 `starting()`，CAS 的「期望值」匹配，写入成功，同时 `lock cmpxchg` 顺带充当了全屏障——一举两得。

#### 4.3.3 源码精读

条件编译门槛：仅 x86/x86_64 且非 miri 才走 hack：

[src/internal.rs:L416-L419](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L416-L419) —— `cfg!(all(any(target_arch = "x86", target_arch = "x86_64"), not(miri)))`。

完整注释把来龙去脉讲得很清楚：

[src/internal.rs:L420-L432](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L420-L432) —— HACK(stjepang) 注释：解释两种 SeqCst 屏障实现（`mfence` vs `lock cmpxchg`）、benchmark 偏好后者、以及形式化担忧。

实际的 CAS 调用：

[src/internal.rs:L433-L440](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L433-L440) —— 期望 `starting()`、写入 `new_epoch`，并 `debug_assert` 必须成功。

[src/internal.rs:L441-L444](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L441-L444) —— 追加一条 `compiler_fence(SeqCst)`，注释坦白说明：这只是为了让 LLVM「更不容易做错」，形式上不足以消除数据竞争，实际上能帮很大忙。

#### 4.3.4 概念深入：这个 hack 的「风险」到底是什么

这一节是本讲的难点，单独拎出来讲。

**形式化风险**：在 C++（以及 Rust 继承的）内存模型里，`SeqCst` 的 **fence** 和 `SeqCst` 的 **RMW（read-modify-write，如 compare_exchange）** 是两套不同的机制，它们进入「SeqCst 全序 S」的方式不同。用一个 `SeqCst` 的 RMW 去顶替一个 `SeqCst` 的 fence，**在形式模型上并不能保证给出完全相同的同步语义**。换句话说：从 C++ 标准的角度，你不能证明「把 fence 换成 CAS 后，4.2 里的不变量 A 仍然成立」。

**为何仍然这么写**：

- 在真实的 x86 硬件上，`mfence` 和 `lock cmpxchg` 都是「指令级别的全屏障」，行为等价。实测（以及多年的生产实践）表明它工作正常。
- 用内联汇编可以写出形式上正确的等价物，但 stable Rust 不支持内联汇编（成文时），所以退而求其次。

**两道补救措施**：

1. `compiler_fence(SeqCst)`：阻止 LLVM 在编译期把屏障前后的指令重排乱掉。注意它**只挡编译器，不挡 CPU**——CPU 层面的屏障由 `lock cmpxchg` 本身提供。所以这两行是「软硬兼施」的组合。
2. `not(miri)` 条件：miri 严格按 C++ 形式模型解释内存序，它不认为 `SeqCst` CAS 等价于 `SeqCst` fence，会把这个 hack 判定为有问题。因此在 miri 下回退到 4.2 的标准 `store + fence(SeqCst)` 路径，保证 miri 能跑过。

**一句话总结这个 hack**：用硬件事实（x86 上 `lock cmpxchg` 是全屏障）换取性能，代价是放弃一部分形式化保证，靠 `compiler_fence` 与 miri 排除来兜底。这是 crossbeam 这类极致优化并发库里很典型的工程取舍。

#### 4.3.5 代码实践

**目标**：亲眼看到两条路径在指令层面的差异。

**步骤**：

1. 在 x86_64 Linux 上，对当前 crate 执行 `RUSTFLAGS="--emit asm -C debuginfo=0" cargo build --release`（或在 `target/release` 产物上用 `objdump -d` / `cargo asm` 查看 `crossbeam_epoch::internal::Local::pin`）。
2. 在 `pin` 的反汇编里找到对应「写本地 epoch」的那一小段，确认它是一条 `lock cmpxchg` 形式的指令，而不是 `mfence`。
3. 用 `MIRIFLAGS="-Zmiri-tag-raw-memory" cargo +nightly miri test`（任选一个 pin 相关测试）观察：miri 下因为走了 `else` 分支，这里看到的是普通 store + fence 调用，没有 CAS。

**预期**：x86 产物里是 `lock cmpxchg`；miri 走的是 `store` + `fence`（标准路径）。

> 待本地验证：具体反汇编输出随 rustc 版本与优化等级变化；只要能区分出 `lock cmpxchg`（CAS 路径）与 `mfence`（fence 路径）即可。

#### 4.3.6 小练习与答案

**练习 1**：为什么 `debug_assert!(res.is_ok())` 用的是 `debug_assert` 而不是 `assert`？

**答案**：因为 release 构建里 `debug_assert` 会被去掉，避免在已经很快的 CAS 之后再多一次分支判断。CAS 必然成功（由 `guard_count==0` 与上次 `unpin` 写回 `starting()` 共同保证），失败只可能是逻辑 bug，开发期捕获即可。

**练习 2**：假设未来 stable Rust 支持了内联汇编，这个 hack 会被怎样改写？

**答案**：把 `compare_exchange(SeqCst, SeqCst)` + `compiler_fence(SeqCst)` 换成一条形式上正确的全屏障内联汇编（在 x86 上等价于 `mfence` 或 `lock` 前缀指令），从而既保留性能，又拿回 C++ 内存模型的形式化保证，去掉注释里的「not clear that this is permitted」担忧。

---

### 4.4 周期性 collect：每 128 次 pin 触发一次

#### 4.4.1 概念说明

EBR 没有「专用的 GC 线程」。垃圾袋被推进全局队列后，谁来真正 drop 它们？答案是：**每个参与者在 pin 的时候，偶尔顺手回收一点**。这是一种摊销（amortized）策略——把回收成本均摊到高频的 pin 操作上，避免任何一次 pin 因回收而卡顿太久。

实现方式：`Local` 维护一个 `pin_count`（历史 pin 总次数，用 `Wrapping<usize>` 防溢出），每达到 `PINNINGS_BETWEEN_COLLECT = 128` 次 pin，就调用一次 `Global::collect`。

#### 4.4.2 核心流程

```text
# 在「真正 pin」分支的末尾
pin_count += 1
if pin_count % 128 == 0:
    global.collect(&guard)     # 尝试推进 epoch + 最多回收 8 个过期袋
```

`collect` 做两件事（u5-l19 会展开第二件）：

1. `try_advance`：尝试把全局 epoch 推进一步。
2. 用 `try_pop_if` 从全局队列里弹出**至多 `COLLECT_STEPS = 8` 个**已过期（`is_expired`）的袋并 drop 它们，触发其中延迟闭包的执行。

注意 `collect` 被标了 `#[cold]`：

[src/internal.rs:L204-L207](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L204-L207) —— 注释说明：`pin()` 极少调用 `collect()`，所以用 `#[cold]` 提示编译器把这条调用放在冷路径上，让常见的「不回收」分支更紧凑、更快。

#### 4.4.3 源码精读

常量定义：

[src/internal.rs:L333-L335](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L333-L335) —— `PINNINGS_BETWEEN_COLLECT = 128`。

周期触发：

[src/internal.rs:L450-L458](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L450-L458) —— 递增 `pin_count`，取模 128 命中则 `collect`。

被调用的 `Global::collect` 与步长上限：

[src/internal.rs:L200-L226](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L200-L226) —— `collect`：先 `try_advance`，再用 `try_pop_if` 弹出至多 `COLLECT_STEPS` 个过期袋。

[src/internal.rs:L177-L178](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L177-L178) —— `COLLECT_STEPS = 8`（sanitizer 下改为 `usize::MAX`，见 [src/internal.rs:L211-L215](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L211-L215)）。

#### 4.4.4 代码实践

**目标**：观察「每次 collect 回收量有上限」这一摊销特性。

**步骤**：阅读 `collector.rs` 里的 `incremental` 测试：

[src/collector.rs:L215-L244](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs#L215-L244) —— 它在一个循环里反复 `defer_unchecked` 产生大量垃圾，然后断言 `assert!(curr - last <= 1024)`。

**解释这个 1024 上限的由来**：单线程不断 defer 时，本地袋满（`MAX_OBJECTS = 64`）就被 `push_bag` 入队；每次 `collect` 至多回收 `COLLECT_STEPS = 8` 个袋，每袋至多 64 个对象 ⇒ 单轮 collect 至多回收 `8 × 64 = 512` 个闭包。测试断言 `<= 1024` 留了余量（含 flush 等其他路径）。这正印证了「增量、有上限」的摊销设计：回收永远被切成小块，不会卡住 pin。

#### 4.4.5 小练习与答案

**练习**：`pin_count` 为什么用 `Wrapping<usize>` 而不是普通 `usize`？

**答案**：因为长期运行的线程 pin 次数会无限增长，普通 `usize` 在理论上会溢出（panic 或 wrap 取决于编译模式）。用 `Wrapping<usize>` 明确表示「就让它回绕」，而 `% 128` 在回绕下仍然正确分布触发点，省去溢出检查的开销。

---

### 4.5 unpin：清空 epoch 与 finalize 触发

#### 4.5.1 概念说明

`unpin` 是 `pin` 的镜像。它也走可重入路径：只有**最后一个** guard 退出（`guard_count` 从 1 减到 0）时，才真正 unpin——把本地 epoch 写回 `starting()`，等于向全世界公告「我不再 pin 了，回收线程推进时不必顾虑我」。

除此之外，unpin 还承担一个「会员卡注销」的职责：如果此刻 `handle_count == 0`（这张 `Local` 已经没有 `LocalHandle` 了），就顺手把这个 `Local` 从全局链表里拆除（`finalize`）。

#### 4.5.2 核心流程

```text
unpin():
    guard_count = self.guard_count.get()       # 读取旧值
    self.guard_count.set(guard_count - 1)      # 计数 -1（无条件）

    if guard_count == 1:                       # 旧值是 1 ⇒ 这是最后一个 guard
        self.epoch.store(Epoch::starting(), Release)   # 公告：未 pin
        if self.handle_count.get() == 0:       # 同时也没有会员卡了
            Self::finalize(self)               # 拆除这个 Local
```

注意两点：

1. **store 用 `Release`**：与 `try_advance` 里的 `Acquire` 屏障配对，确保回收线程看到「未 pin」时，本线程此前对共享对象的所有读写都已对它可见——即「我真的已经不再碰那些对象了」这件事被正确发布。
2. **finalize 的双触发点**：`unpin`（最后一个 guard drop + 无卡）和 `release_handle`（最后一张卡 drop + 无 guard）都会在两个计数器同时归零时触发 `finalize`。这是 u4-l15 讲过的「双计数器分治」的落地。

#### 4.5.3 源码精读

`Local::unpin` 全貌：

[src/internal.rs:L464-L479](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L464-L479) —— 最后一个 guard 时 `store(starting(), Release)`，并在 `handle_count == 0` 时 `finalize`。

对偶的 `release_handle`：

[src/internal.rs:L512-L527](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L512-L527) —— `handle_count - 1`，若 `guard_count == 0 && handle_count == 1` 也触发 `finalize`。这正是 `LocalHandle::drop` 的调用路径（见 [src/collector.rs:L100-L105](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs#L100-L105)）。

`finalize` 的逻辑退会流程（u4-l15 已讲字段含义，这里只看时序）：

[src/internal.rs:L529-L569](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L529-L569) —— 关键时序：①临时把 `handle_count` 设成 1（防止内部那次 `pin` 又触发 `finalize`，见 [src/internal.rs:L537-L541](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L537-L541)）；②`pin` 后把残留本地袋 `push_bag` 入队；③把 `handle_count` 改回 0；④`ptr::read` 取出 `Collector`（`Arc<Global>`）——必须在标记删除前读出；⑤`entry.delete` 在链表里逻辑标记删除；⑥`drop(collector)` 归还那最后一根 `Arc`，可能就此销毁整个 `Global` 并执行队列里全部延迟闭包。

入口链：`Guard::drop` → `Local::unpin`：

[src/guard.rs:L416-L423](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L416-L423) —— `Guard::drop` 解引用 `local` 调 `unpin`（`unprotected` 假守卫的 `local` 为 null，此处为 no-op）。

#### 4.5.4 代码实践

**目标**：用 `pin_holds_advance` 测试理解「pin 期间 epoch 最多推进 2 步」这一由屏障保证的性质。

**步骤**：阅读并运行：

[src/collector.rs:L190-L213](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs#L190-L213) —— 多线程各自 `pin` 后，记录 `collect` 前后的全局 epoch，断言 `after.wrapping_sub(before) <= 2`。

**解释**：每次 `collect` 内部至多 `try_advance` 一次（推进 1 步，`successor` 是 +2）。而一旦有线程正 pin 在当前 epoch，`try_advance` 就推进不了——这正是 4.2 屏障保证的「公告在读之前」让推进线程能如实看到所有 pin 者。所以单次 collect 让 epoch 最多前进 2（一个 `successor` 步），断言成立。

运行：`cargo test pin_holds_advance -- --nocapture`（miri 下用更小的 N，见测试里的 `cfg!(miri)`）。

#### 4.5.5 小练习与答案

**练习 1**：`unpin` 里 `self.epoch.store(Epoch::starting(), Release)` 用 `Release`，而 `pin` 里（标准路径）写 epoch 用的是 `Relaxed`。为什么不对称？

**答案**：pin 路径的同步力由紧跟其后的 `SeqCst` fence 提供，store 本身用 Relaxed 就够；unpin 路径**没有**后续 fence，它的「公告已不再 pin」必须靠 store 自己的 `Release` 与 `try_advance` 的 `Acquire` 配对来发布，所以必须用 Release。

**练习 2**：为什么 `finalize` 里要先 `handle_count.set(1)` 再 `pin()`，最后又 `set(0)`？

**答案**：因为 `finalize` 内部要 `pin()` 来安全地把残留袋 `push_bag` 入队，而 `pin` 返回的 guard drop 时会走 `unpin`；若不先把 `handle_count` 抬到 1，`unpin` 看到 `handle_count == 0` 就会**再次**调用 `finalize`，形成无限递归。临时设成 1 就是堵住这个重入；任务完成后再改回 0，让最终退出条件一致。

## 5. 综合实践

把本讲的四块（pin 可重入、SeqCst 屏障、x86 hack、unpin/finalize）串起来，完成下面这个「源码阅读 + 推演」任务：

**任务**：画一张「pin 的两条分支」对照表，并在每条分支上标注同步语义来源。

| 方面 | 标准 / miri 路径 | x86 hack 路径 |
| --- | --- | --- |
| 进入条件 | 非 x86，或 `miri` | x86/x86_64 且非 miri |
| 写 epoch 的方式 | `self.epoch.store(new_epoch, Relaxed)` | `self.epoch.compare_exchange(starting, new_epoch, SeqCst, SeqCst)` |
| 全屏障来源 | `atomic::fence(SeqCst)`（→ `mfence`） | CAS 本身（→ `lock cmpxchg`） |
| 额外补救 | 无 | `atomic::compiler_fence(SeqCst)` |
| 形式化保证 | 完整（C++ 内存模型） | 依赖 x86 硬件事实，形式上不完整 |
| 为何可行 | fence 是标准 SeqCst 同步原语 | `lock` 前缀指令在 x86 上是全屏障；CAS 必成功因旧值必为 `starting()` |

**接着做两件事**：

1. 在表下方用 3–5 句话写明：两条分支为何在「阻止后续 load 跑到写 epoch 之前」这件事上**最终效果等价**（一条靠 fence，一条靠 lock 前缀的硬件全屏障）。
2. 选一个本讲提到的测试（`pin_holds_advance` 或 `incremental`），用 `cargo test -- --nocapture` 跑一遍，把你观察到的现象与本讲讲的「屏障保证 epoch 不被多推」「collect 回收量有上限」对应起来；若跑不起来，记下「待本地验证」并列出你预期的输出。

## 6. 本讲小结

- `pin` 是可重入的轻量路径：只有 `guard_count` 旧值为 0（第一个 guard）时才真正 pin——读全局 epoch、算出 `pinned()` 写进本地 epoch、插屏障、按周期顺手 `collect`。
- 写完本地 epoch 后、任何 `Atomic::load` 之前的 **`SeqCst` 屏障**是 EBR 正确性的命门：它保证「我 pin 在 g」这条公告先于「我读共享对象」全局可见，使回收线程不会在有人正读对象时推进两步去释放它。
- 去掉这道屏障，在 ARM/POWER 等允许 store-load 重排的弱内存模型上，公告可能被延迟，回收线程会误判参与者未 pin 而推进 epoch、释放正被读取的对象，造成 use-after-free 形式的数据竞争。
- x86 上用 `compare_exchange(SeqCst, SeqCst)` 顶替 `fence(SeqCst)`，是因为 `lock cmpxchg` 在 x86 上是全屏障且实测更快；CAS 必然成功（旧值必为 `starting()`）。其形式化风险（SeqCst 的 RMW ≠ SeqCst fence）靠 `compiler_fence` 与 miri 排除兜底。
- 回收是摊销式的：每 128 次 pin 触发一次 `collect`，每次至多回收 8 个过期袋；`collect` 标 `#[cold]` 以保护热路径。
- `unpin` 是 `pin` 的镜像：最后一个 guard 退出时把本地 epoch 写回 `starting()`（`Release`，与 `try_advance` 的 `Acquire` 配对发布「不再 pin」），并在 `handle_count == 0` 时触发 `finalize`，把残留袋入队并从链表拆除该 `Local`。

## 7. 下一步学习建议

- **下一讲 u5-l19**：把本讲只触及表面的 `try_advance` 与 `collect` 完整展开——看回收线程如何遍历 `locals` 判定能否推进、`SealedBag::is_expired` 的两步窗口如何与 pin 的屏障闭环、`finalize` 在线程退出时的完整调用时序。
- **横向回看 u3-l12（repin）**：现在你已理解 pin 的屏障代价，再去读 `repin` 为什么在 `guard_count == 1` 时能省掉 `SeqCst` 屏障、只用 `Release` store 刷新 epoch——因为刷新发生在保持 pin 的状态下，不存在「公告与首次 load」的竞态。
- **建议精读的源码**：重读 [src/internal.rs:L237-L288](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L237-L288)（`try_advance` 的两条屏障）与 [src/internal.rs:L190-L198](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L190-L198)（`push_bag` 的 `SeqCst` 屏障），把 pin 侧与回收侧的屏障两两配对，画出完整的 happens-before 关系图。
