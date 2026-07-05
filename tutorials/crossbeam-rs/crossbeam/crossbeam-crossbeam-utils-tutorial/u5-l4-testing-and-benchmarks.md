# 并发测试策略与基准

## 1. 本讲目标

本讲是专家层（advanced）的收官篇，目标从「读懂某个原语的实现」转向「**怎么证明这些并发原语是对的、是快的**」。并发代码有一个恼人的特性：它在你机器上「跑通了」几乎不说明任何问题——可能只是这次恰好没踩到那个会出错的线程交错。所以 crossbeam-utils 围绕「证明」搭了一套相当完整的脚手架，本讲就把这套脚手架拆给你看。

学完后你应该能够：

1. 说出 `tests/` 目录是按什么维度组织的（一个类型一个集成测试文件），并识别出 crossbeam-utils 反复使用的几种**断言风格**：drop 计数、语义相等、并发不变量、回归用例。
2. 解释 `loom`、`Miri`、`TSan` 三种并发验证工具各通过哪条 `cfg` 切换被测代码、各自抓什么类型的 bug、为什么三者**互补而不可互相替代**。
3. 读懂 `benches/atomic_cell.rs` 里基准的**维度切分**（按类型、按操作、按竞争度），并能把 `test::Bencher` + `Barrier` 的写法迁移到自己的基准上。

本讲大量承接前几讲的结论，尤其是 [u1-l3](u1-l3-features-build-and-tests.md) 讲过的「feature → build.rs 发 cfg → src 裁剪 → tests 用 `cfg!` 消费」闭环、[u5-l3](u5-l3-cfg-loom-wideseqlock.md) 讲过的 `primitive` 抽象层与 `crossbeam_atomic_cell_force_fallback`。

## 2. 前置知识

- **集成测试（integration test）**：放在 `tests/` 目录下、每个 `.rs` 文件都是一个独立 crate 的测试。它把被测 crate 当作「外部依赖」来用，只能访问 `pub` API——这正是验证「对外契约」的合适层级。
- **`#[test]` 与 `#[bench]`**：Rust 内置的测试与基准函数标注。`#[bench]` 依赖不稳定的 `#![feature(test)]`，只能在 **nightly** 下编译运行。
- **数据竞争（data race）**：至少两个线程并发访问同一内存，至少一个是写，且没有同步保证顺序。Rust 的内存安全只挡住了「借用检查期」能看出的竞争，挡不住跨线程的，要靠原子/锁。
- **Miri**：Rust 官方的** UB 检测器**，运行在 nightly 上（`cargo miri`），按真实内存模型解释每一次内存访问，能抓出未定义行为（如越界、无效对齐、非法别名）。代价是慢、且只解释「这一次执行」。
- **loom**：Tokio 的并发**模型检查器**，用自带的原子类型替换标准库原子，记录每次访问后**穷举**线程交错，能抓「某种你测不到的交错下的竞争」。代价是只能跑极小模型。
- **sanitizer（ASan/MSan/TSan）**：编译器附带的运行期检查工具。本讲重点关心 **TSan**（ThreadSanitizer，数据竞争检测）。启用方式是通过专门的 `--target x86_64-unknown-linux-gnutsan`，rustc 据此设置 `CARGO_CFG_SANITIZE=thread`。
- **`cfg!()` 与 `#[cfg(...)]`**：前者把条件编译求值成运行期 `bool`，后者直接在编译期裁剪代码。本讲两者都会大量出现。
- **不变量（invariant）**：并发场景下「无论线程怎么交错，这条性质始终成立」的断言，例如「读到者看到的值永不为负」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tests/atomic_cell.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/atomic_cell.rs#L1-L404) | `AtomicCell` 的集成测试。展示 `always_use_fallback()` 辅助函数、drop 计数、语义相等、算术宏的测试镜像、`issue_833` 并发回归用例。 |
| [tests/parker.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/parker.rs#L1-L50) | `Parker` 集成测试，重点验证 `park_timeout` 的三种返回理由（`Unparked`/`Timeout`）。 |
| [tests/wait_group.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/wait_group.rs#L1-L65) | `WaitGroup` 集成测试，用 channel 观察线程是否真的阻塞/被唤醒。 |
| [tests/sharded_lock.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/sharded_lock.rs#L1-L252) | `ShardedLock` 集成测试，含 `frob` 随机读写压力、`arc` 并发不变量、poison 语义。 |
| [tests/thread.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/thread.rs#L1-L217) | `thread::scope` 集成测试，含 panic 汇总、嵌套 spawn。 |
| [tests/cache_padded.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/cache_padded.rs#L1-L112) | `CachePadded` 集成测试，含 `distance` 对齐断言、drop/clone 行为。 |
| [benches/atomic_cell.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/benches/atomic_cell.rs#L1-L159) | `AtomicCell` 基准：按类型（u8/usize）× 操作（load/store/fetch_add/CAS）× 竞争度（单线程/2 线程并发）切分维度。 |
| [src/lib.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs#L27-L100) | 顶层：`#![doc(test(...))]` 控制文档测试；`primitive` 抽象层在 loom 与标准库间二选一；feature/cfg 门控模块可见性。 |
| [build.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/build.rs#L43-L49) | 在 `CARGO_CFG_SANITIZE` 存在时发射 `crossbeam_sanitize_thread` 与 `crossbeam_atomic_cell_force_fallback` 两条 cfg。 |
| [Cargo.toml](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/Cargo.toml#L41-L50) | loom 是 `cfg(crossbeam_loom)` 下的可选依赖；`fastrand`/`rustversion` 为 dev-dependencies。 |

辅助理解用的 CI 脚本（仓库根 `ci/`，本讲只读不改）：

| 文件 | 作用 |
| --- | --- |
| [ci/miri.sh](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/ci/miri.sh#L29-L35) | 用 `cargo miri test --all-features -p crossbeam-utils` 跑 Miri。 |
| [ci/san.sh](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/ci/san.sh#L10-L35) | 用 ASan/MSan/TSan 三个目标跑 `cargo test`，注入 `--cfg crossbeam_sanitize`。 |
| [ci/crossbeam-epoch-loom.sh](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/ci/crossbeam-epoch-loom.sh#L6-L11) | 注入 `--cfg crossbeam_loom` 与 `LOOM_MAX_PREEMPTIONS=2` 跑 loom。 |
| [ci/test.sh](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/ci/test.sh#L16-L22) | 常规 `cargo test`，单线程、nightly 下额外 `cargo check --all-targets` 覆盖 bench。 |

---

## 4. 核心概念与源码讲解

### 4.1 tests/ 目录的组织与断言风格

#### 4.1.1 概念说明

crossbeam-utils 的 `tests/` 目录组织极其朴素：**一个对外类型，一个集成测试文件**。这种「一文件一类型」的组织有两个好处：

- 集成测试把 crate 当外部依赖用，只能访问 `pub` API，恰好验证**对外契约**而非内部实现细节——重构内部时只要契约不变，测试就不该挂。
- 不同原语的测试互不干扰，新增类型只需新增文件，CI 失败时一眼就能定位是哪个原语出了问题。

文件清单对应 README 的三大类：

| 类别 | 测试文件 | 覆盖类型 |
| --- | --- | --- |
| Atomics | `tests/atomic_cell.rs` | `AtomicCell` |
| Thread synchronization | `tests/parker.rs` / `tests/wait_group.rs` / `tests/sharded_lock.rs` | `Parker`/`Unparker`、`WaitGroup`、`ShardedLock` |
| Utilities | `tests/thread.rs` / `tests/cache_padded.rs` | `thread::scope`、`CachePadded` |

> 注意 `AtomicConsume` 和 `Backoff` 没有专属集成测试文件：`AtomicConsume` 的行为退化路径在前几讲已说明，`Backoff` 是「退避策略」、其效果更适合用基准衡量而非断言。

更重要的是测试**写法**。crossbeam-utils 反复用几种固定的断言套路来覆盖那些「普通断言难以表达」的性质——尤其是内存安全相关的不变量。下面三小节逐一拆解。

#### 4.1.2 核心流程

阅读整套测试后，可以把断言风格归纳为五类，它们各自盯住一类容易在并发/`unsafe` 场景下出错的性质：

```text
1. 单线程契约      ──▶ 直接断言 load/store/swap 的返回值与语义
2. drop 计数       ──▶ 用 static AtomicUsize 数 Drop 次数，验证「不多 drop、不漏 drop」
3. 语义相等        ──▶ 自定义 PartialEq，验证 CAS 在「字节不等但语义等」时的重试
4. 并发不变量      ──▶ 多线程交错下断言「永不变式」（如读到值恒 >= 0）
5. 回归用例        ──▶ 复现 GitHub issue 的最小程序，防再次破坏
```

这五类不是互斥的——一个测试文件常常混合使用。比如 `tests/sharded_lock.rs` 同时用了「并发不变量」（`arc`）和「单线程契约」（`smoke`）。

#### 4.1.3 源码精读

**(1) drop 计数：盯死「非 Copy 类型的析构」**

`AtomicCell<T>` 在 `T` 非 `Copy` 时要自己负责回收旧值（见 [u5-l1](u5-l1-atomiccell-unsafe-safety.md)）。这类「手写 Drop」最怕 double-drop 或泄漏，而普通 `assert_eq!` 根本看不出析构次数对不对。crossbeam-utils 的套路是定义一个带计数的类型：

[tests/atomic_cell.rs:L76-L116](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/atomic_cell.rs#L76-L116) —— `drops_unit` 用一个 `static CNT: AtomicUsize`，`Foo::new` 时 `CNT += 1`、`Drop` 时 `CNT -= 1`：

```rust
static CNT: AtomicUsize = AtomicUsize::new(0);
// ...
impl Drop for Foo {
    fn drop(&mut self) {
        CNT.fetch_sub(1, SeqCst);
    }
}
```

关键断言是：经过一连串 `swap`/`store` 之后 `CNT.load(SeqCst)` 始终为 `1`（恰好存活一个实例），最后 `drop(a)` 后归零。这同时挡住了「double-drop（CNT 变负）」和「泄漏（CNT 不归零）」两种 bug。`drops_u8`、`drops_usize` 是同一套路的变体，只是换了字段类型。

> 为什么计数器用 `SeqCst` 而非 `Relaxed`？因为这里要在线程间可靠地**读取最终计数**做断言，需要最强的同步语义保证「所有析构都已对计数器可见」。`tests/cache_padded.rs` 里同样的 drop 计数用的是单线程 `Cell<usize>`，因为没有跨线程需求——**断言的强弱点要匹配被测性质**。

**(2) 语义相等：盯死 compare_exchange 的「假失败」**

`compare_exchange` 要求 `T: Eq`（见 [u2-l1](u2-l1-atomiccell-api.md)），原因藏在 [u5-l1](u5-l1-atomiccell-unsafe-safety.md)：当 `T` 的字节表示里有「padding 垃圾」时，可能出现「语义相等但字节不等」，CAS 要据此重试。`modular_u8` 就是专门构造这种场景：

[tests/atomic_cell.rs:L210-L232](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/atomic_cell.rs#L210-L232) —— 自定义 `PartialEq` 让 `Foo(1)` 与 `Foo(11)`、`Foo(52)` 都「相等」（都模 5 同余）：

```rust
impl PartialEq for Foo {
    fn eq(&self, other: &Self) -> bool {
        self.0 % 5 == other.0 % 5
    }
}
```

于是 `a.compare_exchange(Foo(0), Foo(5))` 即使内部读到的旧值是 `Foo(100)`（与 `Foo(0)` 语义等），也能成功并返回 `Ok(Foo(100))`。这个测试直接锁定了「CAS 用 `T::eq` 而非字节比较」这一行为契约。

**(3) 宏驱动的算术测试：镜像源码的批量生成**

[u5-l2](u5-l2-atomiccell-arithmetic-macros.md) 讲过源码用 `impl_arithmetic!` 宏为 12 种整数批量生成 `fetch_*` 方法。测试侧用了一个**结构完全对称**的 `test_arithmetic!` 宏，对每种类型跑同一组断言：

[tests/atomic_cell.rs:L296-L338](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/atomic_cell.rs#L296-L338) —— 宏定义一次，然后逐个类型展开：

```rust
macro_rules! test_arithmetic {
    ($test_name:ident, $ty:ident) => {
        #[test]
        fn $test_name() {
            let a: AtomicCell<$ty> = AtomicCell::new(7);
            assert_eq!(a.fetch_add(3), 7);
            // ... fetch_sub / fetch_and / fetch_or / fetch_xor / fetch_max / fetch_min / fetch_nand
        }
    };
}
test_arithmetic!(arithmetic_u8, u8);
test_arithmetic!(arithmetic_i8, i8);
// ... 共 10 个整数类型
```

这是「源码用宏消除重复 → 测试也用宏消除重复」的典型范式：新增一个整数类型只需在源码和测试各加一行。注意它把 `fetch_nand` 也一并测了，这是 `impl_arithmetic!` 里最容易写错位运算的逻辑。

**(4) 并发不变量：盯死「无论怎么交错都不该发生的事」**

`tests/sharded_lock.rs::arc` 是一个经典的多读者 + 单写者场景，写者会临时把值写成 `-1` 再写回，读者断言**永远看不到负数**：

[tests/sharded_lock.rs:L102-L138](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/sharded_lock.rs#L102-L138) —— 写者线程内：

```rust
let tmp = *lock;
*lock = -1;
thread::yield_now();   // 故意让出，诱使读者插进来读
*lock = tmp + 1;
```

5 个读者线程各持读锁后断言 `assert!(*lock >= 0)`。如果 `ShardedLock` 的读写互斥有任何破绽，读者就可能读到那个中间态 `-1`，断言立刻失败。这种「**主动让出 CPU 制造交错**」的手法是并发压力测试的常用招——比裸跑更可能踩到竞争。

更猛的随机压力是 `frob`：每个线程随机决定读还是写（写概率 1/N），跑 `M` 轮：

[tests/sharded_lock.rs:L24-L49](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/sharded_lock.rs#L24-L49) —— 用 `fastrand`（dev-dependency，见 Cargo.toml L48-L50）生成随机数驱动选择。注意它的循环次数 `M` 是按 `cfg!(miri)` 缩放的（`if cfg!(miri) { 50 } else { 1000 }`），这是下一节「cfg 门控」的话题。

**(5) 回归用例：把 issue 钉死在测试里**

文件里带 GitHub 链接注释的测试都是真实历史 bug 的最小复现。最值得读的是 `issue_833`，它是一个**真·多线程并发**回归测试：

[tests/atomic_cell.rs:L360-L404](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/atomic_cell.rs#L360-L404) —— 一个线程狂 `store` 两个不同的 `NonZeroU128`，主线程在循环里反复 `match &STATIC`。它复现的是「`AtomicCell<u128>` 在某些布局下读到无效枚举判别字」的问题。关键设计：

```rust
const N: usize = if cfg!(miri) { 10_000 } else { 1_000_000 };
```

百万次迭代在真实 CPU 上才可能踩到竞争窗口，但 Miri 太慢，必须缩到一万次。注释里还特意说明 `handle.join().unwrap()` 是「为了避免 miri 的 detached-thread 泄漏告警」。这正说明：**为并发 bug 写回归测试，要同时为多种执行器调好参数**。

#### 4.1.4 代码实践

> **实践目标**：亲手用 drop 计数法为 `AtomicCell` 的析构正确性补一个测试。
>
> **操作步骤**（源码阅读 + 自己写）：
> 1. 重新读 [tests/atomic_cell.rs:L118-L162](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/atomic_cell.rs#L118-L162) 的 `drops_u8`，理解计数器涨跌节奏。
> 2. 在你自己的一个测试 crate 里，仿写一个 `struct Pair(u16, u16)`（带 `Drop` 计数），把它放进 `AtomicCell<Pair>`。
> 3. 交替调用 `store`、`swap`、`take`（注意 `take` 要 `Default`），每次操作后断言 `CNT` 恰好为 `1`，最后 `drop(cell)` 断言为 `0`。
>
> **需要观察的现象**：任意把 `store` 写成「忘记回收旧值」的错误版本（例如手动 `mem::forget` 旧值），计数会偏离 1，断言失败。
>
> **预期结果**：正确实现下计数全程稳定在 1、退出归零；故意制造泄漏时 `CNT` 持续上涨、制造 double-drop 时 `CNT` 跌到 0 以下（usize 下溢 panic）。
>
> 待本地验证：本实践需要你自己在测试 crate 中运行，仓库本身未提供该练习的可执行入口。

#### 4.1.5 小练习与答案

**练习 1**：`tests/atomic_cell.rs` 里 `drops_*` 系列为什么计数器是 `static AtomicUsize` 而不是局部 `Cell<usize>`？

**参考答案**：因为 `AtomicCell` 的 `Drop` 与测试主体可能涉及跨线程语义（且未来若把测试改成多线程压测，`Drop` 可能发生在别的线程），`AtomicUsize` 的原子读写能保证计数在线程间正确可见；`Cell` 是 `!Sync` 的，无法跨线程共享。即便当前测试是单线程，用 `SeqCst` 原子计数也是「为并发场景预留正确性」的稳健写法。

**练习 2**：`modular_u8` 把 `PartialEq` 定义成「模 5 同余」。如果 `compare_exchange` 内部改用「字节相等」而非 `T::eq`，这个测试会在哪一步失败？

**参考答案**：会在 `a.compare_exchange(Foo(0), Foo(5))` 处失败。因为 `a` 此刻存的是 `Foo(0)`（字节是 `0`），`current` 传 `Foo(0)` 字节也相等，这一步反而能过；真正暴露问题的是 `compare_exchange(Foo(10), Foo(15))`——此刻 `a` 存 `Foo(5)`，`current` 传 `Foo(10)`，二者语义等（都模 5 余 0）但字节不等。用字节比较会判「不等」直接返回 `Err`，而测试期望 `Ok(Foo(100))`。所以语义相等测试正是为了钉死「CAS 必须用 `T::eq`」这一契约。

---

### 4.2 loom / Miri / TSan 的 cfg 门控

#### 4.2.1 概念说明

4.1 节的测试在普通 `cargo test` 下跑——这只能证明「**这次**没出错」。并发原语最危险的是「某种罕见交错下的出错」，普通测试几乎抓不到。crossbeam-utils 用三个互补工具来补这个缺口：

| 工具 | 抓什么 | 怎么切换被测代码 | 代价 |
| --- | --- | --- | --- |
| **Miri** | 单次执行里的未定义行为（UB） | 编译器自动设 `cfg(miri)`，测试用 `cfg!(miri)` 读取 | 慢，需缩迭代次数 |
| **loom** | 穷举线程交错下的数据竞争 | 手动 `--cfg crossbeam_loom`，切 `primitive` 抽象层 | 只能跑极小模型 |
| **TSan** | 运行期真实数据竞争 | `--target ...gnutsan` → build.rs 发 `crossbeam_sanitize_thread` | 需真实多核、有假阳性 |

关键洞察是：**这三者运行的是「同一份测试代码」，但被测的「实现代码」会被不同的 cfg 切到不同的分支**。比如 Miri 和 TSan 下，`AtomicCell` 的无锁路径（含内联汇编）会被强制删掉、改走全局锁回退——否则 Miri 看不懂汇编、TSan 会误报竞争。这就把「验证工具」和「被验证实现」用 cfg 解耦了。

#### 4.2.2 核心流程

三条 cfg 的「谁发射 / 谁消费」链路：

```text
Miri:
  rustc 自动           ──▶ cfg(miri)            ──▶ atomic! 宏删无锁候选 + 测试用 cfg!(miri) 缩迭代

TSan (及任意 sanitizer):
  --target ...gnutsan  ──▶ CARGO_CFG_SANITIZE   ──▶ build.rs 发 crossbeam_atomic_cell_force_fallback
                                                    + crossbeam_sanitize_thread
                                                    ──▶ atomic! 宏删无锁候选，AtomicConsume 退化

loom:
  --cfg crossbeam_loom ──▶ lib.rs 切 primitive 抽象层为 loom 实现
                        ──▶ 同时关闭 AtomicCell/ShardedLock/thread::scope（loom 建模不了）
```

注意三者的「触发点」不同：`miri` 是 rustc 内置 cfg，无需 build.rs 介入；`crossbeam_loom` 是用户手动加的 `--cfg`；只有 sanitizer 相关的 `crossbeam_sanitize_thread` / `crossbeam_atomic_cell_force_fallback` 是 build.rs 在编译期读 `CARGO_CFG_SANITIZE` 后**主动发射**的。

#### 4.2.3 源码精读

**(1) build.rs：sanitizer → force_fallback**

这是整条链路的源头。build.rs 读 `CARGO_CFG_SANITIZE`（rustc 在启用 sanitizer 时设置的环境变量），据此发射两条 cfg：

[build.rs:L43-L49](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/build.rs#L43-L49)：

```rust
if let Ok(sanitize) = env::var("CARGO_CFG_SANITIZE") {
    if sanitize.contains("thread") {
        println!("cargo:rustc-cfg=crossbeam_sanitize_thread");
    }
    println!("cargo:rustc-cfg=crossbeam_atomic_cell_force_fallback");
}
```

要点：**只要有任意 sanitizer（不只 TSan），就强制 `AtomicCell` 走全局锁回退**。原因是 `AtomicCell` 的无锁路径用 `AtomicMaybeUninit` 直接读写可能含 padding 的字节，TSan 会把这当成数据竞争误报；改走 SeqLock 全局锁后，访问被锁显式同步，TSan 就安静了。`crossbeam_sanitize_thread`（仅 TSan）则被 `AtomicConsume` 消费——TSan 下 consume 路径退化为 Acquire（见 [u2-l4](u2-l4-atomic-consume.md)）。

**(2) 测试侧：用一个辅助函数汇总三条 cfg**

`tests/atomic_cell.rs` 一开头就定义了一个工具函数，把「是否该期望走全局锁回退」这件事集中算出来：

[tests/atomic_cell.rs:L8-L18](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/atomic_cell.rs#L8-L18)：

```rust
fn always_use_fallback() -> bool {
    atomic_maybe_uninit::cfg_has_atomic_cas! {
        cfg!(any(
            miri,
            crossbeam_loom,
            crossbeam_atomic_cell_force_fallback,
        ))
    }
    atomic_maybe_uninit::cfg_no_atomic_cas! { true }
}
```

读法很关键：

- 外层两个宏 `cfg_has_atomic_cas!` / `cfg_no_atomic_cas!` 是**编译期互斥**的——目标平台有原子 CAS 时编译上一分支，没有时编译 `{ true }`（因为没 CAS 就一定走全局锁）。
- 上一分支里是 `cfg!(any(miri, crossbeam_loom, crossbeam_atomic_cell_force_fallback))`，是**运行期**求值：只要 Miri/loom/sanitizer 任一在跑，就期望 `is_lock_free()` 返回 `false`。

这个函数是 4.1 节 `is_lock_free` 测试的基石：

[tests/atomic_cell.rs:L30-L35](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/atomic_cell.rs#L30-L35)：

```rust
assert_eq!(AtomicCell::<usize>::is_lock_free(), !always_use_fallback);
```

也就是说，**同一个断言在不同执行器下期望不同的值**——在普通 `cargo test` 下 `usize` 是无锁的（期望 `true`），但在 Miri/loom/TSan 下期望 `false`。一份测试代码、靠 `cfg!` 自适应多个执行器，这是 crossbeam-utils 测试体系的核心技巧。

**(3) 缩放迭代次数：`cfg!(miri)` 的直接用法**

`issue_833` 与 `frob` 都用 `cfg!(miri)` 把循环次数砍两个数量级：

[tests/atomic_cell.rs:L369](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/atomic_cell.rs#L369)：`const N: usize = if cfg!(miri) { 10_000 } else { 1_000_000 };`
[tests/sharded_lock.rs:L27](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/sharded_lock.rs#L27)：`const M: usize = if cfg!(miri) { 50 } else { 1000 };`

这是因为 Miri 逐条解释指令、比原生慢几十倍，不缩放会跑几十分钟。`loom` 也类似——模型爆炸增长，故用 `LOOM_MAX_PREEMPTIONS=2` 限制（见 [ci/crossbeam-epoch-loom.sh:L11](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/ci/crossbeam-epoch-loom.sh#L11)）。

**(4) loom 切的是 `primitive` 抽象层，且会关闭部分模块**

[u5-l3](u5-l3-cfg-loom-wideseqlock.md) 已详述 `lib.rs` 里 `primitive` 模块在 `crossbeam_loom` 下重导出 `loom::sync::*`。这里补充两点测试视角的影响：

- [src/lib.rs:L60-L65](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs#L60-L65) 有个 FIXME：loom 不支持 `compiler_fence`，只能用更强的 `fence` 顶替——**这会让 loom 漏掉某些竞争**（注释原话 "this may miss some races since fence is stronger than compiler_fence"）。这说明 loom 也不是万能的。
- [src/lib.rs:L98-L100](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs#L98-L100) 表明 `thread::scope` 整个模块在 loom 下关闭。所以 loom 覆盖的是 `AtomicConsume`/`Parker`/`WaitGroup` 这些「用 `primitive` 抽象层、且不依赖 `repr(transparent)`」的原语，**这正好补上 Miri 在穷举交错上的短板**。

**(5) CI 实际怎么跑**

三个工具在 CI 里是三条独立 job：

- [ci/miri.sh:L33](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/ci/miri.sh#L33)：`cargo miri test --all-features -p crossbeam-utils`，外加 `-Zmiri-strict-provenance -Zmiri-symbolic-alignment-check`（严格指针来源与对齐检查，对 `AtomicCell` 这种玩 `ptr::read_volatile` 的代码尤其重要）。
- [ci/san.sh:L10](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/ci/san.sh#L10) 注入 `--cfg crossbeam_sanitize`，[L17-L18](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/ci/san.sh#L17-L18) 用 ASan、[L33-L35](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/ci/san.sh#L33-L35) 用 TSan 跑，统一 `--test-threads=1`（ sanitizer 报告依赖稳定输出）。
- [ci/crossbeam-epoch-loom.sh:L6](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/ci/crossbeam-epoch-loom.sh#L6) 注入 `--cfg crossbeam_loom`。注意 CI 的 loom job 名字带 `epoch`，但 `crossbeam-utils` 的源码本身完全支持 `crossbeam_loom`——读者可以照搬这条命令到 utils 上本地跑。

#### 4.2.4 代码实践

> **实践目标**：把 4.1 节你自己写的 drop 计数测试，分别送进 Miri 与（若条件允许）loom 跑一遍，体会「同一份测试、不同执行器」。
>
> **操作步骤**（源码阅读 + 本地运行）：
> 1. 阅读 [ci/miri.sh:L12-L14](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/ci/miri.sh#L12-L14)，记下 Miri 推荐的 `MIRIFLAGS`。
> 2. 在你的测试 crate 上运行 `cargo +nightly miri test`（先 `rustup toolchain install nightly` 并 `rustup component add miri`）。
> 3. 若想试 loom：在你的 crate 加 `loom = { version = "0.7", optional = true }` 依赖，写一个 `#[cfg(crossbeam_loom)]` 的小模型测试（线程数 ≤ 2、循环 ≤ 3），用 `RUSTFLAGS="--cfg crossbeam_loom" LOOM_MAX_PREEMPTIONS=2 cargo test --features loom` 运行。
>
> **需要观察的现象**：Miri 下若你的 `unsafe`（如有）写出格，会立刻报 UB 并指出具体行；loom 下若两线程有未同步的访问，会打印交错路径并 panic。
>
> **预期结果**：纯用 `AtomicCell` 公开 API（无手写 `unsafe`）的测试，在 Miri 与 loom 下都应通过——这正是「公开 API 是 sound 的」的证据。
>
> 待本地验证：Miri/loom 需 nightly 工具链与网络安装组件，本环境不保证可运行；若无法安装，至少完成步骤 1 的源码阅读。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `crossbeam_atomic_cell_force_fallback` 在**任意** sanitizer 下都发射，而不是只在 TSan 下？

**参考答案**：因为 `AtomicCell` 无锁路径直接对「可能含 padding 的原始字节」做原子读写，任何 sanitizer（不只 TSan）都可能把这当成异常访问而误报。强制改走 SeqLock 全局锁后，所有访问都被锁的 acquire/release 显式同步，全部 sanitizer 都能正确理解。`crossbeam_sanitize_thread` 才是「只 TSan」专用，被 `AtomicConsume` 的 consume→Acquire 退化单独消费。

**练习 2**：`always_use_fallback()` 里为什么外层要套 `cfg_has_atomic_cas!` / `cfg_no_atomic_cas!` 两个互斥宏，而不是直接 `return cfg!(any(...))`？

**参考答案**：因为有些极简目标平台**根本没有原子 CAS 指令**（被 `atomic_maybe_uninit` 的宏在编译期判定）。在那种平台上，`AtomicCell` 的 `fetch_update` CAS 回退也用不了，只能走全局锁——`is_lock_free()` 必然返回 `false`，与 Miri/loom 无关。所以 `cfg_no_atomic_cas!` 分支直接编译成 `true`，表示「无 CAS 平台恒走回退」。外层宏是**编译期**裁剪，内层 `cfg!()` 是**运行期**求值，两者配合才能覆盖「平台能力」与「执行器」两个正交维度。

---

### 4.3 benches/atomic_cell.rs 的基准维度与写法

#### 4.3.1 概念说明

测试回答「对不对」，基准（benchmark）回答「快不快」。并发原语的性能不能用一个数字概括，因为它**高度依赖三个正交维度**：

1. **类型宽度**：`u8` 与 `usize` 走的原子指令不同，无锁路径的代价不同。
2. **操作种类**：`load`（只读）、`store`（只写）、`fetch_add`（读-改-写 RMW）、`compare_exchange`（CAS）的代价天差地别——RMW 通常比纯 load 贵数倍。
3. **竞争度**：单线程无竞争 vs 多线程高竞争。无锁路径在无竞争下几乎免费，高竞争下却可能因 cache line 弹来弹去而比加锁还慢。

crossbeam-utils 的 `benches/atomic_cell.rs` 就是按这三个维度的**笛卡尔积**来组织基准的。它用 Rust 内置的不稳定基准框架（`test::Bencher`），所以只在 nightly 下能跑——这也解释了 [u1-l3](u1-l3-features-build-and-tests.md) 里「bench 需要 nightly」的结论。

#### 4.3.2 核心流程

基准的组织可以画成一张二维表（竞争度 × 操作），每个类型各一张：

```text
                  load        store       fetch_add   compare_exchange
单线程 u8          ✓           ✓           ✓           ✓
单线程 usize       ✓           ✓           ✓           ✓
2 线程并发 u8      concurrent_load_u8 ──────────────── (只测 load)
2 线程并发 usize   concurrent_load_usize ──────────── (只测 load)
```

并发基准的骨架是「**用 `Barrier` 对齐起跑、固定步数循环、主线程在 `b.iter` 里只测『等待两端跑完一轮』的耗时**」：

```text
worker × 2:   start.wait() ──▶ 跑 STEPS 次 load ──▶ end.wait()  (循环)
主线程  :   b.iter(|| { start.wait(); end.wait(); })   ← 只测这一格的时间
```

这样 `b.iter` 测的就是「2 个线程各跑 100 万次 load 的总耗时」，把线程创建等一次性开销挡在 `b.iter` 之外。

#### 4.3.3 源码精读

**(1) nightly 的入口：`#![feature(test)]`**

[benches/atomic_cell.rs:L1-L7](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/benches/atomic_cell.rs#L1-L7)：

```rust
#![feature(test)]
extern crate test;
use std::sync::Barrier;
use crossbeam_utils::{atomic::AtomicCell, thread};
```

`#![feature(test)]` 是 nightly 专属，启用了内置的 `test::Bencher` 与 `#[bench]`。注意 Cargo.toml 里**没有** `[[bench]]` 段——cargo 会自动发现 `benches/` 下的文件作为基准目标（crate 名即文件名）。CI 里 bench 不实际跑分，只在 nightly 下 `cargo check --all-targets` 确保它能编译（见 [ci/test.sh:L21](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/ci/test.sh#L21)）——因为基准结果因机器而异，不适合作为 CI 门禁。

**(2) 单线程基准：四操作 × 两类型**

最简单的是 `load_u8`：

[benches/atomic_cell.rs:L9-L15](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/benches/atomic_cell.rs#L9-L15)：

```rust
#[bench]
fn load_u8(b: &mut test::Bencher) {
    let a = AtomicCell::new(0u8);
    let mut sum = 0;
    b.iter(|| sum += a.load());
    test::black_box(sum);
}
```

两个细节值得学：

- `b.iter(|| ...)` 接受一个闭包，框架会反复调用它来测平均耗时。
- `test::black_box(sum)` 是「黑洞」——告诉编译器 `sum` 之后还会被用，**防止优化器把整个 `load` 循环消除掉**。没有它，编译器可能发现 `sum` 从没被读取，直接删掉整段循环，基准就失真了。

对照看 RMW 与 CAS 的写法，注意它们故意制造了「递增」以避免每次操作等价：

[benches/atomic_cell.rs:L29-L37](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/benches/atomic_cell.rs#L29-L37) 的 `compare_exchange_u8` 每轮 `i = i.wrapping_add(1)`，让期望值不断变化，模拟真实 CAS 用法。`fetch_add_u8`（[L23-L27](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/benches/atomic_cell.rs#L23-L27)）则是纯 RMW。把 `load`/`store`/`fetch_add`/`CAS` 四者放一起看，就能横向比较「只读」「只写」「RMW」「CAS」在同一类型上的相对代价。

**(3) 并发基准：`Barrier` 对齐 + `thread::scope`**

`concurrent_load_u8` 是整篇最值得读的部分：

[benches/atomic_cell.rs:L39-L83](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/benches/atomic_cell.rs#L39-L83)。它用 `crossbeam_utils::thread::scope`（呼应 [u4-l1](u4-l1-thread-scope.md)）起 2 个工作线程，关键常量：

```rust
const THREADS: usize = 2;
const STEPS: usize = 1_000_000;
let start = Barrier::new(THREADS + 1);   // 2 worker + 主线程
let end = Barrier::new(THREADS + 1);
let exit = AtomicCell::new(false);
```

每个工作线程的循环体：

```rust
loop {
    start.wait();                        // 三方对齐起跑
    let mut sum = 0;
    for _ in 0..STEPS { sum += a.load(); }
    test::black_box(sum);
    end.wait();                          // 三方对齐收尾
    if exit.load() { break; }            // 主线程通知退出
}
```

主线程的被测部分：

```rust
b.iter(|| {
    start.wait();
    end.wait();
});
```

这个设计精妙在：`b.iter` 内部只是两次 `Barrier::wait`，**实际负载（200 万次 load）发生在两次 wait 之间的工作线程里**。框架测的耗时恰好是「等两个 worker 各跑完 100 万次 load」的墙钟时间。起跑前还有一次「预热」的 `start.wait(); end.wait();`（[L70-L71](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/benches/atomic_cell.rs#L70-L71)）让 worker 先进入循环；测完后 `exit.store(true)` 通知退出。`concurrent_load_usize`（[L115-L159](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/benches/atomic_cell.rs#L115-L159)）结构完全相同，只是类型换成 `usize`，用来对比「不同宽度的原子 load 在并发下的 cache 争用」。

**(4) 顶层 doc-test 配置**

最后补一处容易被忽略的细节：[src/lib.rs:L28-L31](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs#L28-L31) 的 `#![doc(test(...))]`：

```rust
#![doc(test(
    no_crate_inject,
    attr(allow(dead_code, unused_assignments, unused_variables))
))]
```

`no_crate_inject` 表示文档里的代码示例不会自动 `use crossbeam_utils`，必须显式写全路径；`attr(allow(...))` 让 doc-test 里允许「看起来没用」的变量——因为文档示例常展示片段而非完整程序。这是「测试/示例」侧的工程化收尾，和基准一样属于「非功能但影响质量基础设施」的部分。

#### 4.3.4 代码实践

> **实践目标**：用 `test::Bencher` + `Barrier` 模式，测量「单线程 vs 2 线程」下 `fetch_add` 的吞吐差异。
>
> **操作步骤**（自己写 + 本地运行）：
> 1. 仿照 [benches/atomic_cell.rs:L39-L83](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/benches/atomic_cell.rs#L39-L83)，把工作线程里的 `sum += a.load()` 换成 `a.fetch_add(1)`。
> 2. 同时保留一个单线程版本 `fetch_add_contention`，结构类似 `fetch_add_u8`。
> 3. 用 `cargo +nightly bench` 跑两个基准，记录它们的 `ns/iter`。
>
> **需要观察的现象**：2 线程版每次 `fetch_add` 的均摊耗时通常显著高于单线程版——这是 RMW 在 cache line 争用下的典型代价（cache line 在两核间反复 invalidate/transfer）。
>
> **预期结果**：单线程 `fetch_add` 接近一条 `LOCK XADD` 指令的延迟（数 ns）；2 线程版每次操作延迟可能高出一个数量级。把两个数字相除，就能量化「竞争开销倍数」。
>
> 待本地验证：基准需 nightly 工具链且结果因机器而异，本环境不保证可运行；若无法跑，请完成步骤 1–2 的代码编写与逻辑推演。

#### 4.3.5 小练习与答案

**练习 1**：`concurrent_load_u8` 里为什么 `b.iter` 只放两个 `Barrier::wait`，而不把 `for _ in 0..STEPS { a.load() }` 直接放进 `b.iter`？

**参考答案**：因为要把「2 个线程**并行**跑 load」的耗时测准。如果把循环放进主线程的 `b.iter`，就成了单线程顺序 load，测不到并发争用。把循环放在 2 个 worker 线程里、用 `Barrier` 让它们同时起跑同时结束，主线程的 `b.iter` 只测「等两端各跑 100 万次」的墙钟时间——这才真实反映「2 核并发读同一 `AtomicCell`」的吞吐。

**练习 2**：去掉 `test::black_box(sum)` 会怎样？

**参考答案**：编译器很可能发现 `sum` 计算完从未被读取（worker 线程里 `sum` 是局部变量，循环结束就丢弃），把整个 `for _ in 0..STEPS { sum += a.load() }` 当死代码消除。于是 `b.iter` 实际测到的几乎是「两次 `Barrier::wait` 的开销」，而非 100 万次 load——基准数字会漂亮得失真。`black_box` 强制编译器认为 `sum` 有外部可观察的副作用，从而保住负载循环。这是写任何微基准都必须警惕的「优化器消除」陷阱。

---

## 5. 综合实践

把本讲三节串起来，做一个**端到端**的「原语 + 测试 + 基准」小项目：

1. **实现**：用 `AtomicCell<u64>` 实现一个线程安全的「最新值快照」结构（多写者 `store`、一读者 `load`），这正是 [u2-l1](u2-l1-atomiccell-api.md) 综合实践要求的东西。
2. **正确性测试**（用 4.1 的套路）：
   - 单线程契约：`store` 后 `load` 立即读到新值。
   - 并发不变量：起 4 个写者各 `store` 自己的固定值、1 个读者循环 `load` 一万次，断言「读到的值**必定是某个写者写入的完整值**」——绝不会是撕裂的中间态。这正是 SeqLock 乐观读承诺的性质（见 [u2-l3](u2-l3-atomiccell-global-lock-seqlock.md)）。
3. **多执行器验证**（用 4.2 的链路）：
   - `cargo test` 通过。
   - `cfg!(miri)` 缩放迭代后，`cargo +nightly miri test` 通过。
   - 若装了 loom，写一个 2 写者 + 1 读者的 loom 模型测试，`RUSTFLAGS="--cfg crossbeam_loom" cargo test` 通过。
4. **基准**（用 4.3 的写法）：仿 `concurrent_load_usize`，测「2 写者并发 store + 1 读者 load」的吞吐，对比「读写比 1:1」与「读写比 1000:1」两种负载。

完成后再回头看 `tests/atomic_cell.rs` 与 `benches/atomic_cell.rs`，你会发现自己写的版本与仓库里的几乎是同构的——这说明你已经掌握了 crossbeam-utils 验证并发原语的**标准范式**。

> 待本地验证：综合实践涉及多工具链，本环境不保证全部可运行；建议至少完成步骤 1–2（实现 + 正确性测试），它们在普通 `cargo test` 下即可闭环。

## 6. 本讲小结

- **测试组织**：`tests/` 一个类型一个集成测试文件，验证对外 `pub` 契约而非内部实现；文件清单对应 README 的 Atomics / Thread synchronization / Utilities 三大类。
- **断言风格**：crossbeam-utils 反复用五类套路覆盖普通断言难表达的性质——单线程契约、**drop 计数**（盯死析构）、**语义相等**（盯死 CAS 假失败重试）、**并发不变量**（盯死「无论怎么交错都不该发生」）、**回归用例**（钉死历史 issue）。
- **三工具互补**：Miri 抓单次执行里的 UB（`cfg(miri)` 自动）、loom 穷举线程交错抓竞争（`--cfg crossbeam_loom` 切 `primitive` 抽象层）、TSan 抓运行期真实竞争（`--target ...gnutsan` 经 build.rs 发 `crossbeam_atomic_cell_force_fallback`）。三者不可互相替代，且各有盲区。
- **一份测试、多执行器**：`always_use_fallback()` 用编译期宏（平台有无 CAS）套运行期 `cfg!(any(miri, crossbeam_loom, ...))`，让同一个 `is_lock_free` 断言在不同执行器下期望不同值——这是整个测试体系的核心技巧。
- **基准维度**：`benches/atomic_cell.rs` 按类型（u8/usize）× 操作（load/store/fetch_add/CAS）× 竞争度（单线程/2 线程）的笛卡尔积切分；并发基准用 `Barrier` 对齐起跑、`b.iter` 只测「等待两端跑完」的耗时，并用 `black_box` 防优化器消除。
- **CI 分工**：常规 `cargo test`（stable，单线程）管正确性、Miri/sanitizer/loom 三条 nightly job 管「证明无竞争」、bench 只在 nightly `cargo check --all-targets` 确保可编译（不作为门禁，因结果因机器而异）。

## 7. 下一步学习建议

本讲是 crossbeam-utils 学习手册的最后一篇，原语、机制、测试三条线已全部收口。接下来可以：

1. **横向扩展到姊妹 crate**：把本讲学到的「drop 计数 / 并发不变量 / loom + Miri + TSan 三件套 / `Barrier` 并发基准」范式，套用到 `crossbeam-epoch`、`crossbeam-channel`、`crossbeam-queue`、`crossbeam-skiplist` 上。它们的 `tests/` 与 `ci/` 脚本结构高度相似，但各自有更复杂的并发场景（如 epoch 的 GC、channel 的 select）。可以先读 [ci/miri.sh:L17-L28](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/ci/miri.sh#L17-L28) 看 channel 在 Miri 下需要哪些特殊 flag（如 `-Zmiri-ignore-leaks`）。
2. **深入 loom 本身**：本讲只把 loom 当工具用，建议读 loom 文档，理解「执行图」「preemption boundary」「`LOOM_MAX_PREEMPTIONS` 如何影响模型规模与覆盖率」的折中，体会 [src/lib.rs:L60-L65](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs#L60-L65) 那个 `compiler_fence` FIXME 为何会「漏掉某些竞争」。
3. **回归源码做二次开发**：挑一个你认为可以优化的点（例如给 `AtomicCell` 加一个 `fetch_update` 的变体，或给 `Backoff` 接入 `Parker` 的混合等待），按本讲的范式为它同时补上：单元/集成测试、Miri/loom 验证、`#[bench]` 对比——这正是「专业地修改并发库」的完整闭环。
