# 并发测试策略与基准

## 1. 本讲目标

本讲是「深入机制与工程实践」单元（u5）的最后一篇，专门回答一个工程问题：

> `crossbeam-utils` 这样一个充满 `unsafe`、靠精细内存序和原子指令保证正确性的并发库，**怎么验证它没有数据竞争、怎么度量它快不快**？

学完本讲，你应当能够：

- 看懂 `tests/` 目录的组织方式，知道「一个并发原语该写哪些断言、用什么手段验证」。
- 说清 loom、Miri、ThreadSanitizer 三种工具各自抓什么 bug、为什么互补，以及它们在源码里通过哪些 `cfg` 被门控。
- 读懂 `benches/atomic_cell.rs`，知道并发基准要测哪些维度（单线程单操作成本、多线程竞争下的吞吐），以及 Barrier + `black_box` 的标准写法。
- 为自己写的并发原语补一个多线程压力测试，并能在 loom 或 Miri 下跑通。

本讲依赖 u1-l3（features / build.rs / cfg 闭环）。你已经在前置讲义里见过 `crossbeam_no_atomic`、`crossbeam_loom`、`crossbeam_atomic_cell_force_fallback` 这些 `cfg` 是谁发的；本讲专讲**测试和基准如何消费它们**。

## 2. 前置知识

### 2.1 并发 bug 为什么难测

并发程序的错误有一类很特殊：**绝大多数运行都正确，只在特定线程交错下才暴露**。比如「读写锁的读者读到写者写了一半的值（torn read）」，可能一百万次只出现一次。普通单元测试（`cargo test`）本质是「跑几次，看断言过不过」，对这类 bug 几乎无能为力。

因此并发库需要三件**专门**的工具：

| 工具 | 它抓什么 | 怎么抓 |
|---|---|---|
| **loom** | 线程交错导致的逻辑错误（漏唤醒、死锁、撕裂读） | 用模型检查器**穷举**所有可能的线程交错 |
| **Miri** | 单次执行里的**未定义行为**（UB），如非法内存访问、数据竞争 | 解释执行 Rust MIR，对每次访存做语义检查 |
| **ThreadSanitizer (TSan)** | 真实运行时的数据竞争（两条无 happens-before 关系的并发访问） | 给每次内存访问插桩，记录并校验 happens-before 图 |

三者**互补**：loom 穷举交错但跑的是模型不是真机器；Miri 查得细（连 UB 都报）但慢、且只跑一条交错；TSan 跑得快、在真实机器上，但只报「这条跑出来的」竞争。三者结合才能给 `unsafe` 并发代码足够的信心。

### 2.2 `cfg` 与 `cfg!` 的区别

- `#[cfg(foo)]` 是**编译期属性**：`foo` 不成立时整段代码被删掉，不参与编译。
- `cfg!(foo)` 是**编译期宏**：在条件编译的基础上返回 `bool`，可以参与 `if`、`const` 计算，但两个分支都必须能编译。

测试里大量用 `cfg!(miri)` 来「缩放迭代次数」，就是因为两个分支都得编译通过。下面会反复出现。

### 2.3 测试代码 vs 文档测试

`crossbeam-utils` 把**正确性测试**全部放在 crate 根的 `tests/` 目录（integration test，每个 `.rs` 文件编译成一个独立测试 binary），而 `src/` 里**几乎没有** `#[cfg(test)] mod tests`。`src/` 里出现的 `cfg!(miri)` / `#[cfg_attr(miri, ...)]` 几乎都在**文档示例**（doc-test）里，用来给 doctest 打补丁。这是本讲的一个关键观察：**这个 crate 的测试主体在 `tests/`，不在 `src/`**。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `tests/atomic_cell.rs` | `AtomicCell` 的集成测试：`is_lock_free`、Drop 计数、语义相等、padding、回归测试、宏生成的算术测试 |
| `tests/parker.rs` | `Parker` 的超时与跨线程唤醒测试 |
| `tests/wait_group.rs` | `WaitGroup` 的「先阻塞后放行」语义测试 |
| `tests/sharded_lock.rs` | `ShardedLock` 的并发读写、poison、`try_*`、`into_inner` 测试 |
| `tests/thread.rs` | `thread::scope` 的 join / 计数 / panic 汇总 / 嵌套 spawn 测试 |
| `tests/cache_padded.rs` | `CachePadded` 的对齐、距离、Drop、Clone 测试 |
| `benches/atomic_cell.rs` | `AtomicCell` 的基准：单线程单操作成本 + 多线程竞争读 |
| `src/lib.rs` | `primitive` 抽象层（loom 与 std 二选一），模块的 `cfg` 门控 |
| `build.rs` | 发射 `crossbeam_no_atomic` / `crossbeam_sanitize_thread` / `crossbeam_atomic_cell_force_fallback` 三条 `cfg` |
| `Cargo.toml` | `dev-dependencies`（fastrand、rustversion）、loom 的可选依赖 |
| `.github/workflows/ci.yml` | 把 test / miri / san / loom / features 串成 CI 流水线 |

## 4. 核心概念与源码讲解

### 4.1 tests 目录的组织与断言

#### 4.1.1 概念说明

`tests/` 目录的组织原则极简：**一个公开类型一个文件，文件名就是类型名**。这让「我要找某类型的测试」变成一次文件查找：

```
tests/
├── atomic_cell.rs    # AtomicCell
├── cache_padded.rs   # CachePadded
├── parker.rs         # Parker / Unparker
├── sharded_lock.rs   # ShardedLock
├── thread.rs         # thread::scope
└── wait_group.rs     # WaitGroup
```

但「有测试」不等于「测得对」。并发原语的测试难点在于：很多正确性性质（如「不会撕裂读」「不会 double-drop」「唤醒不丢失」）**不能直接断言**，必须用**间接证据**——比如用一个全局计数器追踪 Drop 次数、用一个不变量（`>= 0`）排除撕裂读、用 channel 编排时序。

#### 4.1.2 核心流程

`tests/atomic_cell.rs` 里能提炼出五类典型断言套路，几乎覆盖了整个 crate 的测试风格：

1. **能力探测型**：`is_lock_free` —— 断言某类型是否走无锁路径，但断言值要随当前 `cfg` 调整。
2. **Drop 计数型**：`drops_unit/u8/usize` —— 用 `static CNT: AtomicUsize` 数 new/drop 次数，验证不泄漏、不 double-drop。
3. **语义相等型**：`modular_u8/modular_usize` —— `PartialEq` 按 `% 5` 定义，验证 `compare_exchange` 用的是 `T::eq` 而非字节比较。
4. **不变量型**：`issue_833`、`sharded_lock::arc` —— 让读者反复读，断言读到的值永远「合法」（非零、`>= 0`），以此排除撕裂读。
5. **回归型**：`issue_748`、`issue_833`、`garbage_padding` —— 文件名/注释直接挂 GitHub issue 号，复现历史上踩过的坑。

#### 4.1.3 源码精读

**(1) Drop 计数：用全局原子计数器追踪生命周期**

`drops_u8` 定义了一个带 `Drop` 的 `Foo(u8)`，`new` 时 `CNT += 1`、`drop` 时 `CNT -= 1`。任何 `AtomicCell` 操作之后，`CNT` 应当恒为 1（cell 里始终恰好存着一个活的 `Foo`），最后 `drop(a)` 后归零：

[tests/atomic_cell.rs:118-162](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/atomic_cell.rs#L118-L162) —— 关键不变量是每次 `swap`/`store` 之后 `CNT.load(SeqCst) == 1`。这正是 u2-l1 / u5-l1 讲的「store 经 swap 回收旧值、Drop 用 `needs_drop` 分流」机制的正确性证据：如果实现漏掉了某次回收，`CNT` 会 > 1（泄漏）；如果 double-drop，`CNT` 会下溢成 `usize::MAX`。这种测试**不直接 assert 源码逻辑，而是 assert 可观察的资源计数**，是测 `unsafe` 析构代码的标准手段。

**(2) 语义相等：验证 `compare_exchange` 走的是 `T::eq`**

`modular_u8` 把 `PartialEq` 定义成「按 5 取模相等」，于是 `Foo(1)`、`Foo(11)`、`Foo(52)` 两两「相等」：

[tests/atomic_cell.rs:210-232](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/atomic_cell.rs#L210-L232) —— `a.swap(Foo(2))` 返回 `Foo(11)` 而非 `Foo(1)`，因为 cell 里存的字面值是 `11`，但断言用 `==`（mod 5）所以 `Foo(11)` 也通过。这条测试专门锁定 u5-l1 讲的「语义相等但字节不等时 `compare_exchange` 需重试」语义：`compare_exchange(Foo(0), Foo(5))` 在 cell 字面值是 `100`（`100 % 5 == 0`）时返回 `Ok(Foo(100))`。

**(3) 不变量型：`issue_833` 排除 NonZeroU128 的非法读**

这是 crate 里最重要的并发压力测试之一。一个写线程反复 `store` 两个不同的 `NonZeroU128`，主线程反复 match 那个枚举：

[tests/atomic_cell.rs:361-404](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/atomic_cell.rs#L361-L404) —— 它针对的真实 bug 是：`NonZeroU128` 的合法值**不能是全零字节**，如果乐观读路径（u2-l3 / u5-l1 的 `ptr::read_volatile` + `validate_read`）返回了「写者写了一半」的比特，可能凑出非法值导致 UB。测试通过让主线程做 `N` 次 match（`N` 在普通运行时是 100 万、Miri 下缩到 1 万），只要不 `unreachable!` 就算通过。`const N` 的 `cfg!(miri)` 缩放是下一节的话题。

注意结尾 `handle.join().unwrap()` 带注释 `// join thread to avoid https://github.com/rust-lang/miri/issues/1371` —— Miri 要求测试结束时没有游离线程，所以必须 join。

**(4) 通道编排时序：`wait_group::wait`**

`WaitGroup` 的语义是「计数未归零时 `wait()` 必须阻塞」。测试用 `mpsc` 通道 + `try_recv` 来**证明线程确实被卡住**：

[tests/wait_group.rs:7-34](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/wait_group.rs#L7-L34) —— 关键两步：spawn 完后 `sleep(100ms)` 再 `rx.try_recv()` 断言**收不到**（证明 10 个线程都卡在 `wg.wait()`），然后主线程 `wg.wait()` 触发归零放行，再 `recv` 收到全部 10 条。这种「先证明被阻塞、再证明被放行」的双段断言，是测同步原语时序的标准范式，parker.rs 的 `park_timeout_unpark_called_other_thread` 也是同款写法。

**(5) 宏生成测试，对应宏生成实现**

u5-l2 讲过 `impl_arithmetic!` 用一份模板给 12 种整数生成 8 个 `fetch_*` 方法。测试侧用对偶的 `test_arithmetic!` 宏，一份模板生成 10 个测试函数：

[tests/atomic_cell.rs:296-338](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/atomic_cell.rs#L296-L338) —— 「实现用宏批量生成，测试也用宏批量生成」是消除重复的双向对称设计。改一个运算的语义，只需改一处宏定义，实现和测试一起更新。

#### 4.1.4 代码实践

**实践目标**：用「Drop 计数型」套路为 `CachePadded` 写一个并发 Drop 测试，体会间接断言。

**操作步骤**：

1. 阅读 `tests/cache_padded.rs:65-85`（已有的单线程 `drops` 测试）作为模板。

[tests/cache_padded.rs:65-85](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/cache_padded.rs#L65-L85)

2. 仿照它，写一个**多线程**版本：用 `Arc<AtomicUsize>` 作计数器，在 `thread::scope` 里 spawn 几个线程，每个线程构造并 drop 若干 `CachePadded<Foo>`，最后断言计数器等于「构造总数」。

**需要观察的现象**：测试结束后计数器恰好等于所有线程构造的 `Foo` 总数。

**预期结果**：`cargo test drops` 通过。若数字不符，说明存在 double-drop 或漏 drop —— 但 `CachePadded` 透明转发 `T` 的 Drop，正常不会出错。

> 待本地验证：具体计数值取决于你写的循环次数，本讲不假设已运行。

#### 4.1.5 小练习与答案

**练习 1**：`modular_u8` 里，为什么 `a.swap(Foo(2))` 期望返回 `Foo(11)` 而不是 `Foo(1)`？

**答案**：因为前一步 `a.swap(Foo(11))` 把字面值 `11` 写进了 cell（`swap` 返回的是**旧值** `Foo(1)`，但 cell 现在是 `11`）。`Foo(11)` 与 `Foo(1)` 在 `==`（mod 5）意义下相等，所以读者无法从 `==` 区分，但字面值确实是 `11`。这个测试同时验证了「swap 返回旧值」和「相等判断走 `T::eq`」两件事。

**练习 2**：`issue_833` 为什么要 join 写线程，而不像普通测试那样直接结束？

**答案**：因为 Miri 在测试结束时若仍有游离线程会报错（miri/issues/1371）。join 是为了让写线程的 `while` 循环正常退出（`FINISHED.store(true)`），保证没有线程还在访问 `STATIC`。

---

### 4.2 loom / Miri / TSan 的 cfg 门控

#### 4.2.1 概念说明

第 2.1 节的三种工具，**每种都需要让源码或测试用不同的方式编译**：

- **loom** 要求把 `std::sync::atomic` 换成 `loom::sync::atomic`，否则模型检查器看不到访存事件。
- **Miri** 比真机慢 10–100 倍，测试必须**砍掉迭代次数**，否则跑不完。
- **TSan / ASan / MSan** 会让 `AtomicCell` 的乐观读路径（u2-l3）误报数据竞争，所以 sanitizer 下应**强制走全局锁回退**。

这些切换都靠 `cfg` 完成。本节回答：每个 `cfg` 是**谁设的**、**在哪消费**、**测试侧要写什么代码配合**。

#### 4.2.2 核心流程

整个 cfg 闭环可以画成一条链（承接 u1-l3）：

```
               ┌─ rustc 自动 ──▶ cfg(miri)               （编译器内建）
工具/环境 ──────┤
               ├─ CI 设 RUSTFLAGS=--cfg crossbeam_loom ─▶ cfg(crossbeam_loom)
               │
               └─ CARGO_CFG_SANITIZE=thread ─▶ build.rs ─▶ cfg(crossbeam_sanitize_thread)
                                                   └─────▶ cfg(crossbeam_atomic_cell_force_fallback)
                          │
                          ▼
            build.rs 读 no_atomic.rs 黑名单 ─▶ cfg(crossbeam_no_atomic)
                          │
                          ▼
        atomic! 宏在编译期裁剪无锁/回退候选段
                          │
                          ▼
        测试用 cfg!(miri) 缩迭代、用 always_use_fallback() 调整断言
```

三条「crossbeam_*」cfg 全部由 `build.rs` 发射，声明在 check-cfg 里：

[build.rs:20-22](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/build.rs#L20-L22) —— `cargo:rustc-check-cfg=cfg(...)` 告诉编译器「这些 cfg 是合法的」，避免 `unexpected_cfgs` 警告。

发射逻辑分两段：

[build.rs:39-49](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/build.rs#L39-L49) —— 目标命中 `no_atomic.rs` 黑名单则发 `crossbeam_no_atomic`；`CARGO_CFG_SANITIZE` 含 `thread` 则发 `crossbeam_sanitize_thread`；**任意** sanitizer 都额外发 `crossbeam_atomic_cell_force_fallback`（注意是 `if let Ok` 内无条件 `println`，即只要有 sanitizer 就强制回退）。

#### 4.2.3 源码精读

**(1) loom：primitive 抽象层的整体切换**

`src/lib.rs` 顶部用 `cfg(crossbeam_loom)` 在两套 `primitive` 模块间二选一：

[src/lib.rs:47-83](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs#L47-L83) —— loom 分支把 `AtomicUsize`/`Arc`/`Mutex`/`Condvar` 全部重导出为 loom 版本；非 loom 分支用标准库。值得注意的是注释里坦白：「loom 暂不支持 `compiler_fence`，用更强的 `fence` 顶替，可能漏报一些 race」—— 这是 u5-l3 提到的 loom 覆盖盲区之一。

但 loom 有**结构性盲区**：`AtomicCell`、`ShardedLock`、`thread::scope` 在 loom 下**整个模块被关掉**：

[src/lib.rs:95-100](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs#L95-L100) —— `thread` 模块带 `cfg(not(crossbeam_loom))`，`AtomicCell` 与 `ShardedLock` 内部同理（见 u5-l3）。原因是这些类型用 `repr(transparent)` 直接重解释内存，而 loom 的原子类型有独立的内部表示，两者布局不兼容。**所以 loom 实际只能测 `Parker`、`WaitGroup`、`AtomicConsume` 这些不依赖 `AtomicCell` 的原语**——这是阅读 loom CI 时必须知道的前提。

loom 的可选依赖写在 Cargo.toml 的 target 段：

[Cargo.toml:41-46](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/Cargo.toml#L41-L46) —— 注意它**不是 feature**，而是 `cfg(crossbeam_loom)` 门控的可选依赖，并声明「不受 semver 保证」。CI 里通过 `RUSTFLAGS="--cfg crossbeam_loom"` + `--features loom` 启用（见 `.github/workflows/ci.yml` 的 `loom` job，调用 `ci/crossbeam-epoch-loom.sh`）。

**(2) Miri：`cfg!(miri)` 缩放迭代次数**

Miri 是 rustc 自动设的 `cfg`，测试侧最直接的用法是缩放循环次数：

[tests/atomic_cell.rs:369](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/atomic_cell.rs#L369) —— `const N: usize = if cfg!(miri) { 10_000 } else { 1_000_000 };`

[tests/sharded_lock.rs:27](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/sharded_lock.rs#L27) —— `const M: usize = if cfg!(miri) { 50 } else { 1000 };`

两次都砍两个数量级。CI 的 miri job 还会跑两种变体：默认与 `-Zmiri-tree-borrows`（用 Tree Borrows 而非 Stacked Borrows 模型复查 `unsafe`，见 `.github/workflows/ci.yml:200-224`）。

**(3) sanitizer / loom / miri：`always_use_fallback()` 统一探测**

`is_lock_free` 测试最棘手：它断言「某类型是否无锁」，但**答案取决于当前是否被强制回退**。测试用一个辅助函数把三种「强制回退」情形合并探测：

[tests/atomic_cell.rs:8-18](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/atomic_cell.rs#L8-L18) —— `always_use_fallback()` 借助 `atomic-maybe-uninit` 提供的 `cfg_has_atomic_cas!` / `cfg_no_atomic_cas!` 宏（这两个宏只在**平台支持原子 CAS** 时才编译第一分支），在第一分支里检查 `cfg!(any(miri, crossbeam_loom, crossbeam_atomic_cell_force_fallback))`。于是断言写成：

[tests/atomic_cell.rs:30-35](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/atomic_cell.rs#L30-L35) —— `AtomicCell::<usize>::is_lock_free() == !always_use_fallback`。在普通 x86-64 上 `always_use_fallback` 是 `false`，断言 `is_lock_free() == true`；在 TSan 下 `force_fallback` 生效，断言翻转为 `false`。**同一段测试代码在所有 sanitizer/loom/miri 配置下都自洽**——这是「build.rs 发 cfg → 测试消费 cfg」最完整的闭环示例，也是 u1-l3 留的伏笔的落地。

#### 4.2.4 代码实践

**实践目标**：亲手让同一段 AtomicCell 测试在「正常」和「force_fallback」两种 cfg 下走不同路径，观察 `is_lock_free` 翻转。

**操作步骤**：

1. 在 `crossbeam-utils` 目录运行正常测试（确认基线）：
   ```
   cargo test --features atomic is_lock_free
   ```
   预期 `AtomicCell::<usize>::is_lock_free()` 为 `true`。

2. 用环境变量强制回退（模拟 sanitizer 行为）：
   ```
   RUSTFLAGS="--cfg crossbeam_atomic_cell_force_fallback" cargo test --features atomic is_lock_free
   ```
   但注意：这条 cfg 由 `build.rs` 通过 check-cfg 声明，直接在 RUSTFLAGS 里设可能触发 `unexpected_cfgs`。更可靠的做法是用真正的 sanitizer：
   ```
   RUSTFLAGS="-Zsanitizer=thread" cargo +nightly test --features atomic is_lock_free
   ```
   （需 nightly + TSan target）

**需要观察的现象**：第 1 步断言成立是因为无锁路径；第 2 步断言仍成立，但 `is_lock_free()` 返回 `false`（因为 `always_use_fallback` 变 `true`，`!true == false`）。

**预期结果**：两次测试都**通过**，但 `is_lock_free` 的运行时返回值不同——这正是 cfg 门控让「同一断言跨配置自洽」的体现。

> 待本地验证：TSan 需要 nightly 工具链与 `x86_64-unknown-linux-gnu` 的 tsan runtime；若环境不具备，仅跑第 1 步即可。

#### 4.2.5 小练习与答案

**练习 1**：为什么 loom job 能测 `Parker` 却不能测 `AtomicCell`？

**答案**：`AtomicCell` 用 `repr(transparent)` 把 `T` 直接重解释为原子类型，依赖原生内存布局；loom 的原子类型有独立的内部表示（用于追踪访存事件），两者布局不兼容，所以 `atomic` 模块在 `cfg(not(crossbeam_loom))` 下才编译。`Parker` 内部只用 `AtomicUsize` + `Mutex` + `Condvar`（都经 `primitive` 抽象层换成了 loom 版本），不依赖 `repr(transparent)` 重解释，故可测。

**练习 2**：`build.rs` 为什么用 `crossbeam_no_atomic`（否定式）而不是 `crossbeam_has_atomic`？

**答案**：否定式让「build script 没跑」（非 cargo 构建系统）时**乐观默认为支持原子**——因为现代主流目标都支持原子，默认支持是更合理的退化方向。若用肯定式 `has_atomic`，build script 缺席时会误判为「无原子」，错误地关掉所有无锁路径。

---

### 4.3 benches 基准写法

#### 4.3.1 概念说明

`benches/atomic_cell.rs` 是 crate 唯一的基准文件，回答两个性能问题：

1. **单操作成本**：`load` / `store` / `fetch_add` / `compare_exchange` 各自一次调用要多少纳秒？这个成本随类型宽度（`u8` vs `usize`）怎么变？
2. **竞争下的吞吐**：多个线程**同时** `load` 同一个 `AtomicCell` 时，单次 `load` 成本会被 cache line 争用放大多少？

这两个维度对应「无锁路径 vs 锁回退」的核心取舍（u2-l2 / u2-l3）：无锁路径的优势在低竞争下单操作便宜；而 `CachePadded` 全局锁池的优势在高竞争下减少 false sharing。基准要把这两个维度**分别测出来**，否则优化方向会判错。

#### 4.3.2 核心流程

Rust 的 unstable test harness（`#[bench]`）要求 nightly 与 `#![feature(test)]`。每个 bench 函数签名固定：

```rust
#[bench]
fn name(b: &mut test::Bencher) {
    b.iter(|| { /* 被测代码 */ });
}
```

`b.iter` 会把闭包跑很多轮、测总耗时再除以轮数。两个**必须**的工程细节：

- **`test::black_box(x)`**：把值喂给编译器的「黑盒」，阻止优化器把整个循环优化掉（否则 `let sum = 0; sum += a.load();` 会被发现「sum 没被用」而整段删除）。
- **`Barrier` 编排并发**：多线程 bench 不能简单 `for thread in threads { spawn }`，因为 `b.iter` 是**单线程**循环——必须让工作线程在每轮 iter 的边界上同步，否则测的是「线程启动开销」而非「load 吞吐」。

#### 4.3.3 源码精读

**(1) harness 声明与单线程单操作基准**

[benches/atomic_cell.rs:1-7](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/benches/atomic_cell.rs#L1-L7) —— `#![feature(test)]` + `extern crate test` 是 nightly bench 的固定开场。

[benches/atomic_cell.rs:9-15](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/benches/atomic_cell.rs#L9-L15) —— `load_u8`：累加 `sum` 并在循环外 `black_box(sum)`，迫使编译器保留每次 `load`。文件对 `u8` 和 `usize` 各测了 `load/store/fetch_add/compare_exchange` 四种操作，组成一张「操作 × 宽度」的 2×4 矩阵（benches/atomic_cell.rs:9-113），让你能横向比操作、纵向比宽度。

**(2) 并发基准：Barrier + thread::scope**

`concurrent_load_u8` 是全文件的精华，展示如何用两个 Barrier 把 `b.iter` 的轮次边界对齐到工作线程：

[benches/atomic_cell.rs:39-83](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/benches/atomic_cell.rs#L39-L83) —— 设计要点拆解：

- **三把同步量**：`start`（开跑栅栏）、`end`（结束栅栏）、`exit`（退出标志，用 `AtomicCell<bool>`）。`start`/`end` 各 `THREADS + 1`，多出的 1 是主线程自己。
- **工作线程循环**：每轮 `start.wait()` → 跑 `STEPS`（100 万）次 `load` → `end.wait()` → 查 `exit` 决定是否退出。
- **被测的就是 `start.wait(); end.wait();` 本身**：

[benches/atomic_cell.rs:73-76](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/benches/atomic_cell.rs#L73-L76) —— `b.iter` 里只有两次 Barrier 等待。每轮 iter 期间，两个工作线程各跑 100 万次 `load`，iter 结束时主线程在 `end.wait()` 等它们完成。Bencher 测的是「一轮 iter 的墙钟时间」——除以 `THREADS * STEPS` 就是「高竞争下单次 load 的成本」。这个数字会比单线程 `load_u8` 大得多，差额就是 cache line 争用的代价。
- **干净退出**：`exit.store(true)` 后再过一次 `end.wait()` 确保工作线程读到 `exit` 后退出，scope 才能正常返回（呼应 4.1 节「Miri 要求无游离线程」）。

对比 `concurrent_load_usize`（benches/atomic_cell.rs:115-159）与 `concurrent_load_u8`，可以看出竞争成本还随类型宽度变化——`usize` 占满缓存行，争用更激烈。

> 运行方式：`cargo +nightly bench`（需 nightly，因 `#![feature(test)]`）。stable 上 `#[bench]` 不可用，所以 bench 不在普通 `cargo test` 流程里。

#### 4.3.4 代码实践

**实践目标**：用本节的 Barrier 模式，写一个 `concurrent_fetch_add` bench，比较高竞争下 `AtomicCell::fetch_add` 与「裸自旋 CAS」的吞吐差异。

**操作步骤**：

1. 复制 `concurrent_load_u8` 的骨架。
2. 把工作线程里的 `sum += a.load()` 换成 `a.fetch_add(1)`，把 `STEPS` 调小到例如 100_000（`fetch_add` 比 `load` 贵）。
3. 跑 `cargo +nightly bench concurrent_fetch_add`。

**需要观察的现象**：记录单次 `fetch_add` 的平均纳秒数。

**预期结果**：高竞争下 `fetch_add` 单次成本显著高于单线程 `fetch_add_u8` bench（benches/atomic_cell.rs:23-27），因为 CAS 失败重试和 cache line 弹跳。具体数值**待本地验证**——本讲不假设已运行。

#### 4.3.5 小练习与答案

**练习 1**：`b.iter` 里如果去掉 `test::black_box(sum)`，会发生什么？

**答案**：编译器会发现 `sum` 算出来后从未被读取，于是把整个 `for _ in 0..STEPS { sum += a.load() }` 循环整体删除，bench 测到的将是「空循环 + 两次 Barrier」的时间，`load` 成本被完全漏掉。`black_box` 强制编译器认为 `sum` 被「外部消费」，从而保留每次 `load`。

**练习 2**：为什么并发 bench 用 `THREADS + 1` 把主线程也算进 Barrier？

**答案**：`b.iter` 在主线程里循环。每轮 iter 主线程要在 `start.wait()` 放工作线程跑、在 `end.wait()` 等它们跑完——主线程本身是同步的一方，所以 Barrier 容量必须包含主线程（`THREADS + 1`），否则 Barrier 永远凑不齐人数而死锁。

## 5. 综合实践

把本讲三块内容串起来：为你**自己**写一个最小的并发原语，并配上「能被 loom / Miri 验证」的测试。

**任务**：实现一个 `Snapshot<T: Copy>` 类型——内部一个 `AtomicCell<T>`，提供 `set(&self, t: T)` 和 `get(&self) -> T`，多写一读。这是 u2-l1 综合实践里「最新值快照」的简化版。

**步骤**：

1. **实现**：用 `crossbeam_utils::atomic::AtomicCell`，`set` 调 `store`，`get` 调 `load`。约 15 行。

2. **写不变量压力测试**（套用 4.1 的「不变量型」）：
   - 一个写线程不断 `set(0)` / `set(usize::MAX)` 交替。
   - 一个读线程不断 `get()`，断言读到的值**要么是 0 要么是 usize::MAX**，绝不可能是「写了一半」的中间值。
   - 循环次数用 `const N: usize = if cfg!(miri) { 10_000 } else { 1_000_000 };`（照搬 issue_833 的写法）。

3. **分别在三套工具下验证**：
   - 正确性基线：`cargo test --features atomic`。
   - Miri（查 UB / 撕裂读）：`cargo +nightly miri test --features atomic`。若 `Snapshot` 内部依赖 `AtomicCell` 的乐观读，理论上 force_fallback 下 Miri 应通过；若直接 `load`，Miri 会校验无数据竞争。
   - loom：注意 `AtomicCell` 在 loom 下**不可用**（4.2.3 的盲区），所以这个 `Snapshot` 没法直接上 loom。**改用 `AtomicUsize` 重写一版** `SnapshotUsize`，再设 `RUSTFLAGS="--cfg crossbeam_loom" cargo test --features atomic,loom`，让 loom 穷举线程交错。

4. **记录**：哪个工具抓到了什么（理想情况下三者都通过），并解释为什么 `Snapshot` 版本上不了 loom 而 `SnapshotUsize` 可以。

**预期结果**：你应当体会到——**正确性测试（`cargo test`）只能证明「没在这一次跑出错」，而 Miri/loom/TSan 才是给 `unsafe` 并发代码真正兜底的三件套**；并且 loom 的覆盖盲区逼着你在「可测性」与「实现自由度（`repr(transparent)`）」之间做权衡。

> 待本地验证：Miri 与 loom 均需 nightly 工具链；具体能否跑通取决于本地环境，本讲不假设已运行。

## 6. 本讲小结

- `tests/` 采用「一类型一文件」组织；并发正确性多用**间接断言**——Drop 计数（`static AtomicUsize`）、不变量（`>= 0` / 非法值检测）、通道编排时序（`try_recv` 证明阻塞）。
- `cfg` 闭环：rustc 自动设 `miri`，CI 设 `crossbeam_loom`，build.rs 据 TARGET/sanitizer 设 `crossbeam_no_atomic` / `crossbeam_sanitize_thread` / `crossbeam_atomic_cell_force_fallback`；测试用 `cfg!(miri)` 缩迭代、用 `always_use_fallback()` 让断言跨配置自洽。
- loom / Miri / TSan **互补**：loom 穷举交错（但测不了 `AtomicCell`/`ShardedLock`/`thread::scope`）、Miri 查 UB（慢，需缩迭代）、TSan 查真实数据竞争（故 force_fallback）。
- `benches/atomic_cell.rs` 测两个维度：单线程「操作 × 宽度」矩阵、多线程竞争读；并发 bench 的关键是 **Barrier 编排 `b.iter` 边界** + **`black_box` 防优化**。
- `#[bench]` 需 nightly（`#![feature(test)]`），故 bench 独立于 `cargo test` 流程，靠 `cargo +nightly bench` 运行。
- 测试侧用宏生成（`test_arithmetic!`）对偶实现侧的宏生成（`impl_arithmetic!`），实现与测试对称演进。

## 7. 下一步学习建议

- **横向对比同族 crate 的测试**：阅读 `crossbeam-epoch` / `crossbeam-channel` 的 `tests/` 与 loom 配置，体会「不同并发模型需要不同的测试策略」；`crossbeam-channel` 的 loom 覆盖比 `crossbeam-utils` 更完整，可作为 loom 实战的进阶范例。
- **深入 loom**：照着本讲的 `SnapshotUsize` 实践，系统学习 loom 的 `model.rs`（`loom::model::Builder`）如何控制交错枚举上界，以及 `lazy_static!` / `loom::sync::Arc` 的写法差异。
- **回归源码**：把 u5-l1（AtomicCell unsafe 安全论证）与本讲的 `issue_833` / `garbage_padding` 对读——每条 SAFETY 论证都对应一个回归测试，理解「为什么这样测」。
- **学完本讲，u5 单元全部完成**：建议以综合实践里的 `Snapshot` 为题，写一篇自己的「测试 + 基准」小报告，作为整本手册的收官练习。
