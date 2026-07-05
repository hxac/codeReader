# 测试、基准与示例：Treiber 栈与压测

## 1. 本讲目标

学完本讲后，读者应当能够：

1. 说清楚 crossbeam-epoch 用**三种互补手段**来确保「极易写错、又极难复现」的并发内存回收算法是正确的：loom 模型检验（穷举线程交错）、benches 基准（测量热路径开销）、examples 压测（给 ThreadSanitizer / Miri 喂真实负载）。
2. 读懂 `tests/loom.rs`，理解 `loom::model` 如何把 `it_works` 与 `treiber_stack` 两个用例的所有并发交错做**有界状态穷举**，以及 `LOOM_MAX_PREEMPTIONS` 如何在「覆盖率」与「耗时」之间取舍。
3. 读懂 `benches/pin.rs`、`benches/defer.rs`、`benches/flush.rs`，理解 `single_*`（单线程热路径）与 `multi_*`（16 线程竞争）两组维度的测量意图，以及 `flush` 基准用 `Barrier` 协调出的特殊场景。
4. 读懂 `examples/sanitize.rs`，理解这个「不是测试、不是 bench」的 example 为什么存在——它是一份给消毒器喂的多线程负载。
5. 读懂 `build.rs`，理解它如何探测 `CARGO_CFG_SANITIZE`、在 ThreadSanitizer 开启时发射 `crossbeam_sanitize_thread` cfg，以及这个 cfg 如何改写 `try_advance` 的内存屏障逻辑；同时分清两个易混的 cfg：`crossbeam_sanitize`（手动开、缩小数据结构）与 `crossbeam_sanitize_thread`（build.rs 自动开、改屏障）。

本讲是「专家层」的**工程验证**专题，几乎不引入新算法，而是把前面 22 讲建立的 EBR 机制（pin、defer_destroy、try_advance、collect）放到「怎么验证它真的对、真的快」的视角下重新审视。前置认知来自 u3-l10（defer / defer_destroy / flush）和 u4-l14（默认收集器与线程局部 HANDLE）。

## 2. 前置知识

在进入源码前，先用最朴素的语言建立几个直觉。

### 2.1 为什么 EBR 需要「三件套」验证

前面几讲反复强调一个事实：EBR 的正确性建立在**内存序（Ordering）与内存屏障（fence）的精确配合**上——比如 pin 时必须先写本地 epoch、再插 `SeqCst` 屏障，屏障晚一步就是 use-after-free（见 u5-l18）。这种 bug 的特点是：

- **难触发**：只在特定线程交错与特定 CPU 内存模型（ARM/POWER 的 store-load 重排）下才出现。
- **难复现**：普通的单元测试跑一万次可能都碰不到那个致命交错。
- **难定位**：一旦出现就是悬垂解引用，离根因（漏了一个 fence）十万八千里。

所以 crossbeam-epoch 不靠「多跑几次单元测试」来保证正确性，而是用三个分工明确的工具：

| 工具 | 文件位置 | 回答的问题 | 代价 |
|------|----------|-----------|------|
| [loom](https://github.com/tokio-rs/loom) | `tests/loom.rs` | 「所有可能的线程交错下，有没有数据竞争 / 逻辑错误？」 | 状态空间爆炸，需限深 |
| 基准 `#[bench]` | `benches/*.rs` | 「pin / defer / flush 的热路径到底多快？多线程下退化多少？」 | 需要 nightly |
| 压测 example | `examples/sanitize.rs` | 「在真实多线程负载下，ThreadSanitizer / Miri 报不报警？」 | 跑得慢，但贴近生产 |

### 2.2 术语速查

- **loom**：把多线程程序的「所有可能执行顺序」建模成一棵有限状态树并穷举遍历的工具。它把 `loom::sync::atomic` 等替换成「会记录每次访问、能回放」的假原子，从而在单线程进程里「模拟」出所有交错。详见 u6-l22。
- **`LOOM_MAX_PREEMPTIONS`**：loom 的核心限深参数——一次执行中**最多允许线程被抢占（切换）几次**。值越大覆盖率越高、状态空间越大、越慢。
- **`#[bench]` / `test::Bencher`**：Rust **nightly** 专用的内置基准宏（需 `#![feature(test)]`），`b.iter(|| {...})` 测量闭包平均耗时。stable 版没有这个宏。
- **ThreadSanitizer（TSan）**：编译期插桩、运行期检测数据竞争的工具，用 `-Zsanitizer=thread`（nightly）开启。它**不理解内存屏障（fence）的语义**，因此对「靠 fence 同步」的 lock-free 代码会误报，需要特殊处理。
- **Miri**：Rust 官方 UB 检测器，对指针来源（provenance）极严格（见 u6-l22）。
- **`crossbeam_sanitize` cfg**：**手动**用 `--cfg crossbeam_sanitize` 开启，作用是**缩小内部数据结构**（如每袋垃圾数 `MAX_OBJECTS` 从 64 降到 4），让穷举/检测更省时。
- **`crossbeam_sanitize_thread` cfg**：由 `build.rs` **自动**在检测到 ThreadSanitizer 时发射，作用是**改写 `try_advance` 的屏障逻辑**，弥补 TSan 不懂 fence 的缺陷。

> ⚠️ **易混点**：`crossbeam_sanitize`（缩小数据结构）与 `crossbeam_sanitize_thread`（改屏障）是**两个独立 cfg**，来源不同、作用不同，下文 4.4 会专门对照。

## 3. 本讲源码地图

本讲涉及的关键文件（均在 `crossbeam-epoch/` 下，CI 脚本在仓库根 `ci/`）：

| 文件 | 作用 |
|------|------|
| [tests/loom.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/tests/loom.rs) | loom 模型检验：`it_works`（延迟销毁时序）与 `treiber_stack`（无锁栈） |
| [benches/pin.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/benches/pin.rs) | `single_pin` / `multi_pin` 基准 |
| [benches/defer.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/benches/defer.rs) | `single_alloc_defer_free` / `single_defer` / `multi_alloc_defer_free` / `multi_defer` |
| [benches/flush.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/benches/flush.rs) | `single_flush`（用 `Barrier` 协调）/ `multi_flush` |
| [examples/sanitize.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/examples/sanitize.rs) | 16 线程 `swap`/`load` + `defer_destroy` 压测负载 |
| [build.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/build.rs) | 探测 ThreadSanitizer，发射 `crossbeam_sanitize_thread` cfg |
| [src/internal.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs) | 被 cfg 改写的 `try_advance` / `MAX_OBJECTS` / `collect` 实现 |
| [ci/crossbeam-epoch-loom.sh](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/ci/crossbeam-epoch-loom.sh) | CI 里运行 loom 测试的脚本（揭示 `crossbeam_loom` 如何激活） |

永久链接基准（本讲所有链接均基于此 HEAD）：

```
https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/
```

## 4. 核心概念与源码讲解

### 4.1 loom 模型检验：把并发交错「穷举」出来

#### 4.1.1 概念说明

`tests/loom.rs` 是 crossbeam-epoch 最重的正确性防线。它的思路是：**与其祈祷单元测试恰好跑到那个致命交错，不如让 loom 把所有交错都跑一遍。**

loom 把每个原子操作、每个 `spawn`、每个 `thread_local` 访问都变成「可回放」的节点，然后系统地遍历「线程可能在哪些点被切换」的所有组合。只要遍历深度（`LOOM_MAX_PREEMPTIONS`）足够，某个数据竞争就**必然**被 loom 在某次遍历中命中并报错——而不是像普通测试那样「跑了九千次没崩就当没事」。

为什么 EBR 特别需要 loom？因为 EBR 的正确性等价于一个**跨线程的全序断言**：「回收线程释放对象时，绝不能有读线程还握着它」。这种断言靠人脑和单元测试都盯不住，只能交给状态穷举。

#### 4.1.2 核心流程

loom 测试的激活是一条**两段式链路**（这点容易踩坑，普通 `cargo test` 跑不出来）：

1. **手动设两个 cfg**：必须用 `RUSTFLAGS="--cfg crossbeam_loom --cfg crossbeam_sanitize"`。`crossbeam_loom` 让 `src/lib.rs` 切到 loom 版的 `primitive` 抽象层（u6-l22）、并让 `loom-crate` 依赖生效；`crossbeam_sanitize` 把 `MAX_OBJECTS` 从 64 缩到 4，压缩状态空间。
2. **开启 `loom` feature**：`--features loom`，拉入 `loom-crate` 包。
3. **限深运行**：用 `LOOM_MAX_PREEMPTIONS=2`（CI 的选择）限制每次执行最多 2 次抢占，在覆盖率与耗时间折中。

完整命令由 CI 脚本 [ci/crossbeam-epoch-loom.sh:6-11](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/ci/crossbeam-epoch-loom.sh#L6-L11) 给出：

```bash
export RUSTFLAGS="${RUSTFLAGS:-} --cfg crossbeam_loom --cfg crossbeam_sanitize"
env LOOM_MAX_PREEMPTIONS=2 cargo test --test loom --release --features loom -- --nocapture
```

脚本注释提到：`MAX_PREEMPTIONS=2` 时 loom 测试约需 **11 分钟**；若改成 3 则要数倍时间，对 CI 太贵。这正是「覆盖率 vs 代价」的直观体现。

每个用例的骨架是 `loom::model(|| { ... })`：闭包里写多线程场景（用 `loom::thread::spawn`、`loom::sync::Arc`），loom 会反复调用这个闭包、每次喂不同的交错，直到穷举完毕或某个交错触发断言失败。

#### 4.1.3 源码精读

整个 `tests/loom.rs` 用 `#[cfg(crossbeam_loom)]` 包成模块——不开这个 cfg 时文件是空的，因此它**只能在 loom 配置下编译**：

- [tests/loom.rs:1-3](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/tests/loom.rs#L1-L3)：模块顶部的 `#[cfg(crossbeam_loom)]` 守卫。注意它故意用「包成模块」而非「`#![cfg(..)]`」来绕开一个 `-Z crate-attr` 的 rustc/cargo bug。

第一个用例 `it_works` 验证一个微妙的时序不变量：

- [tests/loom.rs:17-49](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/tests/loom.rs#L17-L49)：主线程先 `pin`，**然后**子线程才 `defer` 销毁。由于 pin 在 defer 之前，对象在主线程的 guard 释放前不可能被回收，因此断言 `*item.deref() == "boom"` 必须成立。这直接检验了「宽限期不能跨越已持有的 guard」这条 EBR 铁律。注意 [tests/loom.rs:30-31](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/tests/loom.rs#L30-L31) 的注释坦白说这个 `into_owned` 在真实多线程下并不安全，只是本测试场景能保证没有其他访问者。

第二个用例 `treiber_stack` 是一个**完整的无锁栈**——这是 loom 检验 EBR 最有说服力的场景，因为 Treiber 栈的 `pop` 正是「摘除节点 → `defer_destroy` 延迟释放」的经典模式（u3-l10）：

- [tests/loom.rs:57-65](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/tests/loom.rs#L57-L65)：`TreiberStack` 与 `Node` 结构。`data` 用 `ManuallyDrop<T>` 包裹，是为了让 `pop` 能用 `ptr::read` 把值「搬走」而不触发双重释放（见 [tests/loom.rs:117](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/tests/loom.rs#L117)）。
- [tests/loom.rs:76-96](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/tests/loom.rs#L76-L96)：`push` 用 `compare_exchange` 把新节点 CAS 到栈顶，失败则拿回 `Owned` 重试（u2-l8）。
- [tests/loom.rs:101-124](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/tests/loom.rs#L101-L124)：`pop` 的核心三步——`load` head、CAS 把 head 换成 next、成功后 `guard.defer_destroy(head)` 延迟释放旧节点。这正是 EBR 的标准用法。
- [tests/loom.rs:139-159](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/tests/loom.rs#L139-L159)：`loom::model` 闭包体。两个线程各 push/pop 5 次，最后断言栈空。

一个值得玩味的细节在 [tests/loom.rs:143](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/tests/loom.rs#L143)：

```rust
// use 5 since it's greater than the 4 used for the sanitize feature
let jh = spawn(move || { for i in 0..5 { ... } });
```

循环次数特意取 **5**，是为了严格大于「sanitize feature 用的 4」。后者指 `crossbeam_sanitize` 下 `MAX_OBJECTS = 4`（4.4 节展开）。意思是：要让单个线程产生的垃圾**超过一个 bag 的容量**，从而强制触发「本地袋满 → 推入全局 queue」的换袋路径（u3-l11）。否则 loom 可能根本覆盖不到换袋逻辑。

#### 4.1.4 代码实践

**实践目标**：亲手跑通 loom 测试，观察它「单线程进程却跑很久」的穷举特性。

**操作步骤**：

1. 准备 nightly 工具链（loom 与 `crossbeam_loom` 路径都需 nightly）。
2. 在 `crossbeam-epoch/` 目录执行（**待本地验证**耗时，建议先用更小的 `LOOM_MAX_PREEMPTIONS=1` 试跑）：

   ```bash
   RUSTFLAGS="--cfg crossbeam_loom --cfg crossbeam_sanitize" \
   LOOM_MAX_PREEMPTIONS=1 \
   cargo +nightly test --test loom --release --features loom -- --nocapture
   ```

**需要观察的现象**：

- 即使把 `MAX_PREEMPTIONS` 设成 1，测试也会运行数秒到数十秒——这就是状态穷举的代价。
- 控制台会打印 loom 探索的交错数（`--nocapture` 让日志可见）。
- 测试通过 = 在所有被穷举的交错下，`treiber_stack` 没有数据竞争、没有 use-after-free、断言全成立。

**预期结果**：两个用例 `it_works`、`treiber_stack` 均 `passed`。若你故意把 `pop` 里的 `guard.defer_destroy(head)` 删掉（改成立即 `drop`），重跑后 loom 应当报出 use-after-free 类错误——这正是 loom 的价值。

> 若本地无 nightly 或跑不动，至少把 `tests/loom.rs` 当作「一份经过状态穷举检验的 Treiber 栈参考实现」来精读。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `tests/loom.rs` 不写成普通的 `#[test]`（不带 `crossbeam_loom`）放到 `src/` 内联测试里？

**参考答案**：因为它依赖 `loom::model`、`loom::thread::spawn`、`loom::sync::Arc` 等 loom 专属 API，这些只在 `crossbeam_loom` cfg 下才存在（loom 版 `primitive` 层）。普通 `cargo test` 不开此 cfg，文件会被 `#[cfg(crossbeam_loom)]` 整体裁掉；硬塞进内联测试会让正常构建编译失败。

**练习 2**：把 `treiber_stack` 里循环次数从 5 改成 3，是否仍能触发「换袋」路径？为什么？

**参考答案**：不一定。`crossbeam_sanitize` 下 `MAX_OBJECTS = 4`，循环 3 次最多产生 3 个垃圾，**不超过** 4，可能整轮都不换袋；改成 5 是为了严格大于 4，确保至少触发一次「本地袋满 → 入全局 queue」。循环 3 时该路径可能完全没被覆盖，削弱了检验意义。

**练习 3**：`it_works` 里注释说 `into_owned`「在真实多线程下并不安全」，那这个测试到底在验证什么？

**参考答案**：它验证的是**时序不变量**而非「这段代码能直接用于生产」。关键是 pin 发生在 defer 之前，所以在主线程 guard 释放前对象绝不被回收。loom 穷举所有交错，确认「无论线程怎么切换，这个先 pin 后 defer 的顺序都能保证读到的值是 `"boom"`」。

---

### 4.2 基准测试 benches：pin / defer / flush 的测量维度

#### 4.2.1 概念说明

`benches/` 下三个文件回答的是一个不同的问题：**「这些操作到底有多快？」** 它们用 Rust nightly 的内置 `#[bench]` 宏（`#![feature(test)]`），不检验正确性，只测耗时。

bench 一律按两个维度切分：

- **`single_*`**：单线程、无竞争。测的是操作的**绝对理论开销**（一次 pin 在没有别人争用时几个纳秒）。
- **`multi_*`**：16 线程同时跑。测的是**竞争下的退化**（cache line 弹跳、`try_advance` 遍历、epoch 推进的开销）。

`single` 与 `multi` 的差距，就是 lock-free 数据结构「单线程快、多线程退化」特性的量化体现。

#### 4.2.2 核心流程

所有 bench 的骨架一致：

```rust
#![feature(test)]
extern crate test;
use test::Bencher;

#[bench]
fn xxx(b: &mut Bencher) {
    b.iter(|| { /* 被测代码 */ });
}
```

`b.iter` 会把闭包跑很多轮、取平均耗时（单位纳秒/次）。多线程版本用 `crossbeam_utils::thread::scope` 派生 16 个 scoped 线程，每个线程循环 `STEPS` 次。`flush.rs` 的 `single_flush` 比较特殊，用了一对 `Barrier` 把 16 个「陪跑」线程卡在 pinned 状态，迫使被测的 `flush` 在「全局有别人 pin 着」的场景下运行。

#### 4.2.3 源码精读

**pin 基准**（[benches/pin.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/benches/pin.rs)）：

- [benches/pin.rs:9-12](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/benches/pin.rs#L9-L12)：`single_pin` 直接 `b.iter(epoch::pin)`，测一次可重入 pin 的开销（首个 guard 真正 pin、立即 drop 即 unpin）。
- [benches/pin.rs:14-31](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/benches/pin.rs#L14-L31)：`multi_pin` 让 16 线程各跑 100 000 次 `epoch::pin()`。每个线程首次 pin 会写本地 epoch + 屏障，且 pin 每 128 次触发一次 `collect`（u5-l18），多线程下 `try_advance` 还要遍历花名册——这些都计入耗时。

**defer 基准**（[benches/defer.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/benches/defer.rs)）有四个，对应「分配 vs 不分配」×「单 vs 多」：

- [benches/defer.rs:9-18](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/benches/defer.rs#L9-L18)：`single_alloc_defer_free`——`Owned::new(1)` 分配、`into_shared`、`defer_destroy`。含一次堆分配。
- [benches/defer.rs:20-26](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/benches/defer.rs#L20-L26)：`single_defer`——只 `defer(move || ())`，**不分配**。两者相减≈单次堆分配开销。
- [benches/defer.rs:28-49](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/benches/defer.rs#L28-L49) 与 [benches/defer.rs:51-69](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/benches/defer.rs#L51-L69)：对应的 16 线程版本，`STEPS = 10_000`。多线程 defer 会触发「本地袋满 → push_bag → 全局 queue」的争用。

**flush 基准**（[benches/flush.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/benches/flush.rs)）：

- [benches/flush.rs:11-32](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/benches/flush.rs#L11-L32)：`single_flush` 的精妙处——先用 `Barrier` 让 16 个线程**先各自 `epoch::pin()` 再等待**（[benches/flush.rs:21-22](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/benches/flush.rs#L21-L22)），人为制造「全局有 16 个参与者 pin 着」的状态，然后才测主线程的 `epoch::pin().flush()`（[benches/flush.rs:28](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/benches/flush.rs#L28)）。这样 `flush`→`collect`→`try_advance` 会发现 epoch 推不进（有人 pin 在旧 epoch），从而测到「flush 在无法推进时的开销」。没有 Barrier 协调，主线程独占时 epoch 一推就进，测不到这个分支。
- [benches/flush.rs:34-52](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/benches/flush.rs#L34-L52)：`multi_flush`，16 线程各 10 000 次 `pin().flush()`，测竞争下 flush 的退化。

#### 4.2.4 代码实践

**实践目标**：量化 `single_pin` 与 `multi_pin` 的差距，建立对「锁无关热路径开销」的直觉。

**操作步骤**：

1. 安装 nightly：`rustup toolchain install nightly`。
2. 在 `crossbeam-epoch/` 下跑（**待本地验证**；benches 必须 nightly）：

   ```bash
   cargo +nightly bench --bench pin
   ```

**需要观察的现象**：

- `single_pin` 通常在 **个位数纳秒** 量级（一次 `Cell` 自增 + 偶尔的 pin/屏障，摊销后极低）。
- `multi_pin`（按 `STEPS=100_000` × 16 线程的总时间分摊）会比 `single_pin` 显著更高，因为含 cache line 弹跳与 `try_advance` 遍历。

**预期结果**：`single_pin` 远快于 `multi_pin` 的单次分摊开销。把两者数字记下来，这就是「EBR 在无竞争时几乎免费、有竞争时仍有可控开销」的实证。若你的机器是多核，差距会更明显。

#### 4.2.5 小练习与答案

**练习 1**：`single_defer` 和 `single_alloc_defer_free` 几乎只差一次 `Owned::new(1)`，为什么要把它们分开测？

**参考答案**：为了**隔离变量**。`defer` 本身的开销（入本地袋、可能的换袋）与堆分配开销是两件事。`single_alloc_defer_free - single_defer` ≈ 单次 `Owned::new` 的分配开销；`single_defer` 单独看就是「纯 defer 路径」的开销。分开测才能定位瓶颈是分配还是回收机制本身。

**练习 2**：`single_flush` 为什么必须用 `Barrier` 让 16 个线程先 pin 住？不用 Barrier 会怎样？

**参考答案**：为了让被测的 `flush` 处于「全局有别的参与者 pin 在当前 epoch」的状态，使 `try_advance` 推不动 epoch、走「尝试推进失败但仍遍历花名册」的分支。若不用 Barrier，主线程独占时 `try_advance` 一推就成功，测到的是另一条（更便宜的）代码路径，无法反映 flush 在真实多 pin 场景的开销。

**练习 3**：为什么 `multi_pin` 的 `STEPS`（100 000）远大于 `multi_defer` 的 `STEPS`（10 000）？

**参考答案**：因为 pin 比 defer 便宜得多（defer 涉及入袋、可能的换袋与全局同步）。若 `multi_defer` 也跑 100 000 次，单轮 bench 耗时会过长；反之 `multi_pin` 跑太少则单次误差大。`STEPS` 是按「让单轮 bench 耗时落在合理区间」来分别调的。

---

### 4.3 压测示例 examples/sanitize.rs：16 线程真实负载

#### 4.3.1 概念说明

`examples/sanitize.rs` 既不是 `#[test]` 也不是 `#[bench]`，而是一个**普通可执行程序**。它存在的目的是**给 ThreadSanitizer / Miri 提供一份贴近真实的多线程负载**——这些工具是「运行期插桩」的，必须有真实的多线程交错去跑，才能暴露数据竞争。

loom 解决的是「逻辑正确性穷举」，但它跑的是 loom 假原子、状态空间受限；TSan/Miri 解决的是「真实硬件内存模型下的数据竞争与 UB」，跑的是真原子、真线程。两者互补，`examples/sanitize.rs` 就是后者的喂料。

#### 4.3.2 核心流程

程序结构是「100 轮外循环 × 每轮一个全新 Collector × 16 线程并发」：

1. 每轮新建独立 `Collector` 与一个共享 `Arc<Atomic<AtomicUsize>>`。
2. 16 个线程各执行 `worker`：随机睡 0–10ms 制造交错，然后在窗口内反复 `pin`→`flush`→（随机二选一）`swap` 旧值 + `defer_destroy` 或 `load` 读值。
3. 每轮结束后，主线程用 `epoch::unprotected()` 把残留对象安全取回 `into_owned`（无并发，故可用 unprotected）。

随机的 `sleep` 和随机分支选择，是为了**最大化线程交错的多样性**，让 TSan 有更多机会抓到竞争。

#### 4.3.3 源码精读

- [examples/sanitize.rs:15-47](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/examples/sanitize.rs#L15-L47)：`worker` 主体。注意 [examples/sanitize.rs:19-22](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/examples/sanitize.rs#L19-L22) 用 `fastrand` 随机决定是否先睡 1ms、以及窗口长度，专门用来制造交错。
- [examples/sanitize.rs:30-40](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/examples/sanitize.rs#L30-L40)：核心压测分支——
  - 一半概率走 `swap`：用新 `Owned` 换出旧值 `p`，`guard.defer_destroy(p)` 延迟释放，再 `flush` 加速回收，最后 `deref` 读旧值。这正是「换出 → 延迟释放 → 可能被回收」的危险路径。
  - 一半概率走 `load`：直接读当前值并 `fetch_add`。

  注意 `p.deref()`（[examples/sanitize.rs:35](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/examples/sanitize.rs#L35)）在 `defer_destroy` **之后**仍安全，是因为 guard 还 pin 着、回收尚未发生（u3-l10 的宽限期保证）。
- [examples/sanitize.rs:49-71](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/examples/sanitize.rs#L49-L71)：`main`，外层 100 轮，每轮 16 线程 `collector.clone().register()` 得到本线程 `LocalHandle` 后 `worker`。
- [examples/sanitize.rs:66-69](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/examples/sanitize.rs#L66-L69)：收尾——所有线程 join 后已无并发，用 `epoch::unprotected()`（u3-l9 的假守卫）把 `Atomic` 里残留的最后一个对象 `swap` 成 null 并 `into_owned` 释放。`unsafe` 责任是「此刻单线程、无人持有指针」。

`fastrand` 依赖来自 `Cargo.toml` 的 `[dev-dependencies]`（[Cargo.toml:55-56](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/Cargo.toml#L55-L56)），examples/tests/benches 都能用。

#### 4.3.4 代码实践

**实践目标**：把这份压测跑起来，体会「100 轮 × 16 线程 × 随机交错」的真实负载。

**操作步骤**：

1. 在 `crossbeam-epoch/` 下运行（**待本地验证**耗时；不开消毒器时它只是个普通并发程序）：

   ```bash
   cargo run --release --example sanitize
   ```

**需要观察的现象**：

- 程序正常跑完、无输出（`main` 无打印），退出码 0。
- 用 `time` 观察总耗时——100 轮 × 16 线程 × 随机 sleep，可能数秒到十几秒。

**预期结果**：无 panic、无崩溃。它本身不断言任何东西——**它的价值在于被消毒器「监视着跑」**（见 4.4）。单跑它只是确认负载本身健康。

#### 4.3.5 小练习与答案

**练习 1**：`worker` 里 `p.deref()` 紧跟在 `guard.defer_destroy(p)` 之后，为什么不是 use-after-free？

**参考答案**：因为 `defer_destroy` 只是**登记**延迟销毁，真正执行要等宽限期（全局 epoch 前进 ≥2，u3-l10）。当前线程的 guard 还 pin 着，回收线程推进 epoch 时会发现本参与者仍 pin 在旧 epoch 而无法推进，故 `p` 在本 guard 释放前绝不会被销毁，`deref()` 安全。

**练习 2**：为什么收尾用 `epoch::unprotected()` 而不是普通 `epoch::pin()`？

**参考答案**：所有 16 线程已 join，此刻**单线程、绝无并发**，对象不会被他人触达。`unprotected()` 是 u3-l9 的假守卫，`defer` 立即执行、`flush` 是 no-op，开销最低，且语义上正好对应「无并发的构造/析构」场景。用普通 `pin()` 也能正确，但多此一举地走了一遍 pin/屏障。

**练习 3**：`worker` 里随机 `thread::sleep` 对 TSan 检测有什么帮助？

**参考答案**：sleep 会改变线程调度的真实交错，让 16 个线程在运行期的命中顺序更多样。TSan 是运行期插桩，交错越多样、能观察到的「写-写/读-写 无同步」竞争越可能暴露。固定无 sleep 的紧凑循环反而可能让线程趋于「各跑各的」，降低竞争检出率。

---

### 4.4 build.rs 与 ThreadSanitizer 联动：两个易混的 cfg

#### 4.4.1 概念说明

这一节澄清一个最容易混淆的点：crossbeam-epoch 里**有两个名字相近、但来源和作用都不同的 cfg**。

| cfg | 谁来开 | 作用 |
|-----|--------|------|
| `crossbeam_sanitize` | **人工** `--cfg crossbeam_sanitize`（CI 脚本里和 loom 一起开） | 缩小内部数据结构（`MAX_OBJECTS` 64→4、`COLLECT_STEPS`→`usize::MAX`），让穷举/检测更省时省内存 |
| `crossbeam_sanitize_thread` | **build.rs 自动**检测到 ThreadSanitizer 时发射 | 改写 `try_advance` 的屏障逻辑，弥补 TSan 不懂 fence 的缺陷 |

为什么要分两个？因为它们解决的问题正交：

- `crossbeam_sanitize` 是**性能/可行性**调整——loom/TSan/Miri 跑大数据结构太慢，缩小后才能跑完。
- `crossbeam_sanitize_thread` 是**正确性**调整——TSan 不理解 `atomic::fence(SeqCst)`，会把合法的 fence 同步误报为数据竞争（false positive），必须换一种 TSan 能看懂的写法。

build.rs 的职责就是「自动探测后者」：它读取 `CARGO_CFG_SANITIZE` 环境变量，若含 `"thread"` 就发射 `crossbeam_sanitize_thread`。注意 `cfg(sanitize = "thread")` 语法本身未稳定，所以 build.rs 走读环境变量的老办法（[build.rs:9](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/build.rs#L9) 的注释点明了这一点）。

#### 4.4.2 核心流程

**build.rs 的发射逻辑**：

1. Cargo 构建 `crossbeam-epoch` 前先编译运行 `build.rs`。
2. 当用户用 `RUSTFLAGS="-Zsanitizer=thread"`（nightly）编译时，Cargo 会把 `sanitize = "thread"` 信息放进 `CARGO_CFG_SANITIZE`。
3. build.rs 读到 `CARGO_CFG_SANITIZE` 含 `"thread"`，打印 `cargo:rustc-cfg=crossbeam_sanitize_thread`。
4. 该 cfg 在 `src/internal.rs` 的 `try_advance` 里被消费——把原本的 `SeqCst`/`Acquire` fence 替换成「对每个参与者各做一次 `Acquire` load」的等价写法，让 TSan 看懂同步关系。

**`crossbeam_sanitize`（手动）被消费的地方**：

- `MAX_OBJECTS`：64 → 4（缩小 bag）。
- `collect` 的步数：`COLLECT_STEPS` → `usize::MAX`（一次性把所有过期袋都回收）。
- 一个 collector 测试在该 cfg 下 `#[ignore]`。

#### 4.4.3 源码精读

**build.rs 全文很短**（[build.rs:5-14](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/build.rs#L5-L14)）：

```rust
let sanitize = env::var("CARGO_CFG_SANITIZE").unwrap_or_default();
if sanitize.contains("thread") {
    println!("cargo:rustc-cfg=crossbeam_sanitize_thread");
}
```

注意第 7 行的 `cargo:rustc-check-cfg=cfg(crossbeam_sanitize_thread)` 告诉 Cargo「这个 cfg 是合法的、可能出现」，避免 `unexpected_cfg` 警告。第 1 行注释强调：**build.rs 发射的 cfg 不属于公开 API**，外部 crate 不应依赖它。

**消费端：`try_advance` 的双实现**（[src/internal.rs:237-288](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L237-L288)）。回忆 u5-l19：`try_advance` 遍历所有 `Local` 判断能否推进 epoch，关键的两道屏障是开头 `SeqCst fence`（与 pin 的 `SeqCst` 配对，保证「公告在读之前」）和遍历后的 `Acquire fence`。问题在于 **TSan 不懂 fence**：

- [src/internal.rs:244](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L244)：TSan 分支下，先把遍历到的每个 `local` 收集到 `alloc::vec![]` 里。
- [src/internal.rs:266-274](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L266-L274)：遍历结束后，对每个 `local.epoch` 各做一次 `Ordering::Acquire` 的 `load`。这一连串**真实的原子 load**，TSan 是看得懂的，于是它能把回收线程与本线程的读写正确关联，不再误报。
- [src/internal.rs:275-276](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L275-L276)：非 TSan 分支才用 `atomic::fence(Ordering::Acquire)`——更便宜，但 TSan 看不懂。

[src/internal.rs:241-243](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L241-L243) 的注释解释了代价：「需要分配一个 vec 是无奈之举，但不这么做 TSan 可能在不该同步的地方误判同步，产生 false positive」。

**消费端：`crossbeam_sanitize`（手动 cfg）缩小数据结构**：

- [src/internal.rs:65-69](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L65-L69)：`MAX_OBJECTS` 在 `crossbeam_sanitize` 或 `miri` 下为 4，否则 64。这正是 4.1 节 treiber_stack 注释里「sanitize feature 用的 4」的出处。
- [src/internal.rs:211-215](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L211-L215)：`collect` 在 `crossbeam_sanitize` 下用 `usize::MAX` 步数（一次清空），否则用 `COLLECT_STEPS`（增量回收 8 个，u5-l19）。
- [src/collector.rs:216-217](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs#L216-L217)：一个测试因 `crossbeam_sanitize` 改了 `MAX_OBJECTS` 而断言失效，故在该 cfg 下 `#[ignore]`（链接的 TODO 指向 issue #662）。

> 这两套 cfg 也常被**同时**开启——CI 的 loom 脚本就是 `--cfg crossbeam_loom --cfg crossbeam_sanitize` 一起设；而跑 TSan 时 `-Zsanitizer=thread` 自动触发 `crossbeam_sanitize_thread`，开发者通常会再手动补 `--cfg crossbeam_sanitize` 来顺便缩小数据结构。

#### 4.4.4 代码实践

**实践目标**：用 ThreadSanitizer 跑 `examples/sanitize.rs`，亲眼看到 build.rs 的 cfg 生效、以及 TSan 报告（理想情况下无报告）。

**操作步骤**：

1. 用 nightly + TSan 运行压测 example（**待本地验证**；TSan 仅 nightly、且需支持 TSan 的目标，如 Linux x86_64）：

   ```bash
   RUSTFLAGS="-Zsanitizer=thread" \
   cargo +nightly run --release --target x86_64-unknown-linux-gnu --example sanitize
   ```

2. 想确认 cfg 真的发射了，可在 build 输出里查找，或临时在 `src/internal.rs` 的 `try_advance` 加一行 `eprintln!("tsan branch")`（仅观察，事后还原——**不要提交对源码的修改**）。

**需要观察的现象**：

- 若 build.rs 正确探测，`crossbeam_sanitize_thread` 生效，`try_advance` 走「vec 收集 + 逐个 Acquire load」分支。
- TSan 若发现真有数据竞争，会打印 `WARNING: ThreadSanitizer: data race` 并指出两个冲突访问；若 EBR 实现正确，应**无**竞争报告。
- 程序会比不开 TSan 慢一个数量级以上（插桩开销）。

**预期结果**：example 跑完、无 TSan data race 报告（因为 `try_advance` 已为 TSan 改写了屏障）。如果**移除** build.rs 的探测（或让 TSan 走 fence 分支），同样的负载很可能出现 false positive——这就是 build.rs 存在的意义（仅记录现象，不必修复）。

> 注意：TSan 与 loom 不能同时跑（loom 用假原子，TSan 插真桩）；它们是**互斥**的两条验证通道。

#### 4.4.5 小练习与答案

**练习 1**：`crossbeam_sanitize` 和 `crossbeam_sanitize_thread` 的最大区别是什么？为什么需要两个？

**参考答案**：来源不同（前者人工 `--cfg`、后者 build.rs 自动）且作用不同（前者缩小 `MAX_OBJECTS`/放大 `collect` 步数以省穷举耗时；后者改写 `try_advance` 屏障以避免 TSan 误报）。一个管「跑得快跑得完」，一个管「TSan 别误报」，正交故需两个独立 cfg。

**练习 2**：为什么 `try_advance` 在 TSan 分支下要 `alloc::vec![]` 收集所有 local、再逐个 `Acquire load`，而不是直接用一个 `Acquire fence`？

**参考答案**：因为 ThreadSanitizer **不理解 `atomic::fence` 的同步语义**，对它而言 fence 是「空操作」，于是会把合法的 fence 同步误判为数据竞争。改写成「对每个参与者的 epoch 各做一次真实的 `Acquire` load」，TSan 能识别这种 load-load/load-store 的 happens-before 关系，从而正确关联读写、消除 false positive。代价是多了 vec 分配。

**练习 3**：build.rs 第 1 行强调「发射的 cfg 不是公开 API」。如果某个下游 crate 在自己的代码里写 `#[cfg(crossbeam_sanitize_thread)]`，会怎样？

**参考答案**：不会按预期工作。`crossbeam_sanitize_thread` 是 `crossbeam-epoch` 的 build.rs **针对自身 crate** 发射的 cfg，下游 crate 自己的 build.rs 没发射它，所以下游编译时这个 cfg 不会被定义，相关分支会被裁掉。cfg 不跨 crate 传递，所以它只是内部实现细节，外部不应依赖。

---

## 5. 综合实践

把本讲三件套串起来，做一个「**对比验证**」小项目：

1. **实现一个最小 Treiber 栈**：参照 `tests/loom.rs` 的 `treiber_stack`（[tests/loom.rs:57-131](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/tests/loom.rs#L57-L131)），用 `crossbeam_epoch` 写一个 `push`/`pop`，pop 里用 `defer_destroy`。

2. **三通道分别验证它**（**待本地验证**，按你的工具链情况选做）：
   - **loom**：把栈包进 `loom::model` 闭包，两线程各 push/pop 若干次，`LOOM_MAX_PREEMPTIONS=2` 跑 `cargo +nightly test --features loom`（RUSTFLAGS 带 `--cfg crossbeam_loom --cfg crossbeam_sanitize`）。
   - **TSan**：写一个像 `examples/sanitize.rs` 那样的 16 线程随机 push/pop 压测，`RUSTFLAGS="-Zsanitizer=thread"` 跑，确认无 data race（此时 build.rs 已为 crossbeam-epoch 发射了 `crossbeam_sanitize_thread`）。
   - **bench**：仿 `benches/pin.rs` 写 `single_push_pop` 与 `multi_push_pop`，`cargo +nightly bench`，量化单线程 vs 16 线程的差距。

3. **观察对照**：
   - loom 报不报错？（报错说明你的栈有逻辑/同步 bug。）
   - TSan 报不报 race？（不报说明 EBR + 你栈的同步在真实内存模型下成立。）
   - bench 的 single/multi 比值是多少？（量化你栈的竞争退化。）

这个任务把「正确性穷举（loom）」「真实内存模型检测（TSan）」「性能量化（bench）」三种视角叠在同一份代码上，正是 crossbeam-epoch 自身的验证哲学。如果三关都过，你就拥有了一份达到 crossbeam-epoch 验证标准的无锁栈。

## 6. 本讲小结

- crossbeam-epoch 用**三件套互补**验证 EBR：loom 穷举线程交错（逻辑正确性）、benches 量化热路径（性能）、examples 喂真实负载给 TSan/Miri（真实内存模型下的数据竞争与 UB）。
- `tests/loom.rs` 需 `--cfg crossbeam_loom`（+ `--features loom`）才能编译，`loom::model` 穷举交错；`treiber_stack` 特意循环 5 次（> sanitize 的 `MAX_OBJECTS=4`）以覆盖换袋路径。
- `benches/` 一律按 `single_*`（无竞争理论值）与 `multi_*`（16 线程退化）两维度切分；`flush.rs` 还用 `Barrier` 人为制造「别人 pin 着」的场景以测 flush 的特定分支；所有 bench 需 nightly 的 `#![feature(test)]`。
- `examples/sanitize.rs` 是给消毒器喂的 16 线程 `swap`/`load` + `defer_destroy` 负载，用随机 `sleep` 最大化交错；收尾用 `unprotected()` 释放残留对象。
- **两个易混 cfg**：`crossbeam_sanitize`（人工开、缩 `MAX_OBJECTS` 64→4、`collect` 步数→`usize::MAX`）与 `crossbeam_sanitize_thread`（build.rs 自动开、改写 `try_advance` 用「vec + 逐个 Acquire load」代替 fence，弥补 TSan 不懂 fence）。
- loom 与 TSan **互斥**：loom 用假原子、TSan 插真桩，不能同跑；它们加上 bench 共同构成「逻辑 + 真实模型 + 性能」的完整验证矩阵。

## 7. 下一步学习建议

本讲是整个 6 单元学习手册的收官篇。建议读者：

1. **回头交叉验证**：把本讲的 loom `treiber_stack` 与 u6-l20（侵入式链表）、u6-l21（Michael-Scott 队列）对照阅读——你会发现 list/queue 本身就是 crossbeam-epoch 用来管理参与者与垃圾袋的无锁数据结构，它们的正确性同样依赖本讲这套 loom + 单元测试（`#[cfg(all(test, not(crossbeam_loom)))]` 分支）的验证。
2. **动手扩展**：挑一个 bench（如 `multi_defer`）改成你自己的并发数据结构，观察性能曲线；或给 `treiber_stack` 加一条 loom 用例，专门检验「pop 时 head 已被别的线程换走」的 CAS 失败重试路径。
3. **深入可移植性**：若对 miri / strict provenance / CHERI 还想深挖，回到 u6-l22；若想再追「为什么 pin 必须有 SeqCst 屏障」的底层原理，回到 u5-l18。
4. **通读 CI**：把仓库根的 `ci/` 目录脚本（如 `ci/crossbeam-epoch-loom.sh`）当作「如何正确运行这些验证」的权威说明书，理解每个 `RUSTFLAGS` 与环境变量的来由。

至此，你已经从「EBR 是什么」一路走到「如何证明 EBR 既正确又快」，具备了对一个生产级 lock-free 内存回收库进行源码审阅、验证复现与二次开发的能力。
