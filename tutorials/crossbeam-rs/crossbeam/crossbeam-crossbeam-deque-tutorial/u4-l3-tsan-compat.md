# ThreadSanitizer 兼容与 crossbeam_sanitize_thread

## 1. 本讲目标

本讲解决一个具体的工程问题：**crossbeam-deque 是一套无锁队列，它的正确性依赖精心放置的内存栅栏（fence）；但当用 ThreadSanitizer（tsan）去检测它时，这些栅栏反而会让 tsan 报出「假阳性」数据竞争。** 为此，项目用编译期开关 `crossbeam_sanitize_thread` 在「生产路径」和「检测友好路径」之间切换。

读完本讲你应当掌握：

1. 为什么 tsan 不理解 fence，从而对 Chase-Lev 队列报假阳性。
2. `build.rs` 如何通过 `CARGO_CFG_SANITIZE` 探测 `sanitize=thread` 并注入 `crossbeam_sanitize_thread` cfg，以及 `cargo:rustc-check-cfg` 的作用。
3. 源码中「`#[cfg(not(...))] fence(Release)` + 运行时 `cfg!()` 切换 store 序」的双路径写法，以及为什么它零运行时开销。
4. 在 tsan 模式下用 `store(Release)` 替代 `fence(Release)` 后的语义为何仍然正确。

本讲承接 u4-l1（内存序与 volatile hack）——本讲正是那条「技术性数据竞争」注释在工程上的善后。

## 2. 前置知识

- **数据竞争与 TSan**：两线程在无 happens-before 关系下并发读写同一内存，即为数据竞争（data race），在 Rust 里属未定义行为（UB）。ThreadSanitizer 是一个动态检测工具，它在程序运行时给每次内存访问和原子操作打标签，构造一张「happens-before」图，一旦发现无同步边的并发访问就报警告。
- **fence 与 Acquire/Release**（u4-l1 已讲）：`fence(Release)` 给「之前的所有写」盖一个释放戳；配对的 `fence(Acquire)` 或 `load(Acquire)` 建立同步边。`store(Release, v)` 等价于「先盖释放戳，再写值」合并成单条原子写。
- **TSan 怎么建同步边**：tsan 主要靠「同位置的 `store(Release)` ↔ `load(Acquire)`」原子对来建立 happens-before。孤立的 `fence` 跨不同位置时，tsan 对它的建模较弱，常常连不上边。
- **build script 与 cfg**：Cargo 在编译前先运行 `build.rs`，脚本用 `println!("cargo:rustc-cfg=NAME")` 动态打开一个编译期开关；之后整个 crate 里就能用 `#[cfg(NAME)]` / `cfg!(NAME)` 了。

> 提示：本讲的「双路径」是**编译期**二选一，不是运行时分支，请始终带着这一点阅读源码。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [build.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/build.rs) | 探测 `sanitize=thread`，点亮 `crossbeam_sanitize_thread` cfg，并声明 check-cfg。 |
| [src/deque.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs) | 全部队列实现，包含 5 处 tsan 双路径。 |
| ci/san.sh（workspace 级） | 项目实际跑 tsan 的脚本，用自定义 target `x86_64-unknown-linux-gnutsan` + `-Z build-std`。 |

本讲只盯「tsan 兼容」这一条线，不重复 push/steal 的算法细节（见 u2、u3）。

## 4. 核心概念与源码讲解

### 4.1 为什么需要 tsan 兼容：fence 不可见与「假阳性」

#### 4.1.1 概念说明

回顾 u4-l1 的关键事实：`Buffer::write/read` 用 `ptr::write_volatile/read_volatile` 而非原子操作读写槽位，源码注释坦承这是「技术上属于数据竞争、属于 UB」的折中：

> This method might be concurrently called with another `read` at the same index, which is technically speaking a data race and therefore UB. … as a hack, we use a volatile write instead.
> ——[src/deque.rs:72-80](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L72-L80)

这套设计的正确性，靠的是**对槽位写完后、推进 `back` 游标前**插一道 `fence(Release)`，让消费者在 `load(Acquire)` 到新 `back` 时能看到槽位内容。生产路径长这样（以 `Worker::push` 为例）：

```
写槽位 (volatile)  →  fence(Release)  →  back.store(Relaxed)
```

问题是：**tsan 看不懂中间那道 fence**。源码注释直接点明：

> ThreadSanitizer does not understand fences, so we omit fence and do store with Release ordering.
> ——[src/deque.rs:422](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L422)

于是 tsan 把「生产者写槽位」和「消费者读槽位」看作两条无同步边的并发访问，报出一个**事实上不会发生、但工具识别不出来**的假阳性竞争。CHANGELOG 记录了这个动机：

> Change a few `Relaxed` orderings to `Release` in order to fix false positives by tsan. ——CHANGELOG 0.6.1

#### 4.1.2 核心流程

直觉修复方案：**把 release 语义直接焊到那条 `store` 上**，让 tsan 通过「同位置的 `store(Release)` ↔ `load(Acquire)`」原子对建起同步边。改造后的发布序列：

```
写槽位 (volatile)  →  back.store(Release)
```

为什么语义仍然正确？因为 `store(Release)` 的定义就是「当前线程此前**所有**内存写（包括那条 volatile 槽位写）都不能重排到这条 store 之后」，它**涵盖了**原来 `fence(Release)` 想保证的顺序。消费者 `load(Acquire)` 到这条 `back` 后，能看见槽位内容。两条路径在正确的内存模型下等价，区别只在「tsan 能不能建模」。

> 注：在弱内存模型（如 ARM）上，`store(Release)` 与 `fence(Release)+store(Relaxed)` 在某些场景下理论上可生成不同的指令序列与重排约束；本 crate 的选择是「生产用 fence 版（贴合论文证明的实现）、tsan 用 Release store 版（让工具能验证）」。

#### 4.1.3 源码精读

`Stealer::steal` 消费侧的 `SeqCst` fence，正是与 push 端配对的那道栅栏；它也是 tsan「看不懂」的对象之一：

```rust
// A SeqCst fence is needed here.
// If the current thread is already pinned (reentrantly), we must manually issue the
// fence. Otherwise, the following pinning will issue the fence anyway, so we don't have to.
if epoch::is_pinned() {
    atomic::fence(Ordering::SeqCst);
}
```

参见 [src/deque.rs:645-652](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L645-L652)。这道 `SeqCst` fence 夹在「读 front」与「读 back」之间（u4-l1 详述），是为了满足 Le 等弱内存模型论文的要求；它出现在消费侧，是 push 端 fence 的对偶。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：在脑子里把「为什么 tsan 报假阳性」还原成一张 happens-before 图。
2. **步骤**：打开 [src/deque.rs:399-433](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L399-L433)（`Worker::push`），再打开 [src/deque.rs:641-683](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L641-L683)（`Stealer::steal`）。
3. **观察**：push 端是「volatile 写槽位 → fence(Release) → back.store(Relaxed)」；steal 端是「load front(Acquire) → fence(SeqCst) → load back(Acquire) → read 槽位」。
4. **画图**：用箭头标出「人眼里的同步边」（经 fence 连通）与「tsan 能识别的同步边」（只有 `back` 上的 store/load 对，但那是 Relaxed+Acquire，tsan 不认 Relaxed store 为 release 戳）。
5. **结论**：人眼里有边，tsan 眼里没边 → 假阳性。把 push 的 `back.store` 改成 `Release` 后，tsan 就能从这条 store 建边了。预期结果：理解 4.1.2 的修复直觉。

#### 4.1.5 小练习与答案

**练习 1**：把 `Worker::push` 末尾的发布改成 `fence(Release)` + `back.store(Relaxed)`，和直接 `back.store(Release)` 相比，在 C++/Rust 内存模型下哪个「更强」？

**答案**：就「让消费者看到此前写」这一目的而言二者等价；`store(Release)` 把 release 戳合并进了 store，且禁止该 store 与之前的写重排，足以覆盖 fence 的作用。区别在于 Release store 是「单点」发布，fence 是「全局」发布——对本场景（只关心 back 这一个游标）没有差别。

**练习 2**：为什么不能干脆在生产路径也永远用 `store(Release)`、彻底删掉 fence 版本？

**答案**：fence 版本更贴近 Chase-Lev / Le 等论文中经过证明的实现形式，且在部分架构上可能与作者期望的指令序列/优化更一致；保留它是对「已验证算法」的忠实。tsan 版本只是为工具可见性而做的等价改写，两条路径都被认为正确。

### 4.2 build.rs：探测 sanitize=thread 与 cfg 注入

#### 4.2.1 概念说明

我们希望「普通构建走 fence 版，tsan 构建走 Release store 版」。为此需要两步：(1) 在构建时探测当前是否启用了 thread sanitizer；(2) 把结果变成一个编译期 cfg，供源码用 `#[cfg]` 分支。

难点是：表示「启用了某个 sanitizer」的标准 `cfg(sanitize = "thread")` **尚未稳定**（见 ci/san.sh 注释里的 TODO），所以不能在源码里直接写 `#[cfg(sanitize = "thread")]`。绕行办法：Cargo 会把这个不稳定 cfg **转译成 build script 的环境变量 `CARGO_CFG_SANITIZE`**，于是 build.rs 可以读环境变量来探测，再用稳定的自定义 cfg 名 `crossbeam_sanitize_thread` 回传给源码。

#### 4.2.2 核心流程

`build.rs` 的执行流程：

1. 声明 `cargo:rerun-if-changed=build.rs`（脚本本身变更时重跑）。
2. 声明 `cargo:rustc-check-cfg=cfg(crossbeam_sanitize_thread)`，告诉 rustc「这个自定义 cfg 名是合法的，别报 unexpected cfg 警告」。
3. 读取环境变量 `CARGO_CFG_SANITIZE`（缺省为空串）。
4. 若其中包含 `"thread"`，则 `println!("cargo:rustc-cfg=crossbeam_sanitize_thread")`，点亮开关。
5. 之后整个 crate 编译时 `cfg!(crossbeam_sanitize_thread)` 为真。

#### 4.2.3 源码精读

整个脚本只有几行：

```rust
fn main() {
    println!("cargo:rerun-if-changed=build.rs");
    println!("cargo:rustc-check-cfg=cfg(crossbeam_sanitize_thread)");

    // `cfg(sanitize = "..")` is not stabilized.
    let sanitize = env::var("CARGO_CFG_SANITIZE").unwrap_or_default();
    if sanitize.contains("thread") {
        println!("cargo:rustc-cfg=crossbeam_sanitize_thread");
    }
}
```

参见 [build.rs:5-14](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/build.rs#L5-L14)。

关键点逐条说明：

- **第 7 行 `cargo:rustc-check-cfg`**（[build.rs:7](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/build.rs#L7)）：自 Rust 1.80 起，rustc 会检查 `#[cfg(...)`/`cfg!()` 里出现的名字是否「已知」。自定义名字若不声明就会触发 `unexpected_cfgs` 警告。这一行把 `crossbeam_sanitize_thread` 登记为「期望出现」的 cfg，消除告警。
- **第 9-10 行**（[build.rs:9-13](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/build.rs#L9-L13)）：`CARGO_CFG_SANITIZE` 是 Cargo 把 `cfg(sanitize = "x")` 展开后给 build script 的环境变量（多个 sanitizer 用空白分隔）。注释明说「`cfg(sanitize = "..")` 未稳定」，所以走环境变量这条路。
- **第 11-13 行**：用 `contains("thread")` 判定是否开了 thread sanitizer，命中就点亮 cfg。

那么 `CARGO_CFG_SANITIZE=thread` 是怎么被设上的？两种典型途径：

- 直接给 rustc 传 `-Z sanitizer=thread`（nightly），会让当前编译目标带上 `cfg(sanitize = "thread")`。
- 用一个内置 sanitizer 的**自定义 target spec**。项目 CI 走的就是这条路：见 workspace 级 `ci/san.sh` 第 32-35 行，用 target `x86_64-unknown-linux-gnutsan` 并配合 `-Z build-std`，且用 `ci/tsan` 文件做 TSan 抑制列表。

#### 4.2.4 代码实践（可本地验证）

1. **目标**：亲眼看到 `crossbeam_sanitize_thread` 能被 `--cfg` 直接点亮，并验证 `cfg!()` 宏随之翻转。
2. **步骤**：在一个临时二进制 crate 里写一行：

   ```rust
   fn main() { println!("tsan cfg = {}", cfg!(crossbeam_sanitize_thread)); }
   ```

3. 分别运行：

   ```bash
   cargo run                                  # 期望打印 tsan cfg = false
   RUSTFLAGS="--cfg crossbeam_sanitize_thread" cargo run   # 期望打印 tsan cfg = true
   ```

4. **观察**：`--cfg NAME` 会直接点亮该 cfg（绕过 build.rs），用来快速验证下游 `cfg!()`/`#[cfg]` 的分支选择。
5. **预期结果**：两次输出分别为 `false` 与 `true`。
6. **待本地验证**：若想验证「真正的 build.rs 探测路径」，需要 nightly 且用 `RUSTFLAGS="-Z sanitizer=thread"`（通常还要 `-Z build-std` 和自定义 target），此时 build.rs 的第 11 行命中、自动点亮 cfg——这一步依赖本地 nightly 工具链，环境不具备时标记为「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么源码里写的是 `crossbeam_sanitize_thread`，而环境变量是 `CARGO_CFG_SANITIZE`？

**答案**：`CARGO_CFG_SANITIZE` 是 Cargo 把**未稳定**的 `cfg(sanitize = "..")` 翻译给 build script 的标准环境变量名；而 `crossbeam_sanitize_thread` 是本 crate 自己定义的、**稳定**可用的 cfg 名，由 build.rs 在探测到 thread sanitizer 后点亮。前者是「输入信号」，后者是「对源码暴露的开关」。

**练习 2**：删掉 `cargo:rustc-check-cfg=cfg(crossbeam_sanitize_thread)` 这一行，在 Rust ≥ 1.80 下会发生什么？

**答案**：编译器在解析 `#[cfg(not(crossbeam_sanitize_thread))]` 与 `cfg!(crossbeam_sanitize_thread)` 时，会因为遇到「未声明」的 cfg 名而发出 `unexpected_cfgs` 告警。该声明就是用来登记此名字合法、抑制告警的。

### 4.3 双路径写法：fence vs Release store（5 处调用点）

#### 4.3.1 概念说明

点亮 cfg 之后，源码要做的就是在「fence 版」和「Release store 版」之间二选一。这里有个值得学的写法：**同时用 `#[cfg(...)]` 与 `cfg!(...)` 两种机制**，因为它们各管一种语法位置：

- `atomic::fence(Ordering::Release);` 是一条**语句**。要去掉它，最干净的是 `#[cfg(not(crossbeam_sanitize_thread))]`——整条语句在 tsan 构建里根本不存在。
- 而要决定 `back.store(_, X)` 里的 `X` 取 `Release` 还是 `Relaxed`，这是一个**表达式里的值**，没法用 `#[cfg]` 直接挑值，于是改用 `cfg!(crossbeam_sanitize_thread)` 这个编译期布尔宏，配合 `if` 让优化器把它折成常量。

二者都是**编译期**求值，最终二选一，**没有任何运行时分支开销**。

#### 4.3.2 核心流程

每个发布点的固定骨架（tsan 关时 vs 开时）：

```
普通构建 (cfg 关):
    写槽位(volatile)
    atomic::fence(Release)        ← 语句被编译进来
    back.store(Relaxed)           ← store_order 折叠为 Relaxed

tsan 构建 (cfg 开):
    写槽位(volatile)
                                  ← fence 语句被 #[cfg(not(...))] 整条移除
    back.store(Release)           ← store_order 折叠为 Release
```

这套骨架在本文件里出现 **5 次**，分布在「向目的 `Worker` 的 `back` 游标发布一批写入」的所有位置。注意：`Injector::push` **不在其列**——它写的是 Slot 的原子 `state`（`fetch_or(WRITE, Release)`，是原子 RMW，tsan 本就能建模），不需要这个双路径；只有「写完非原子槽位/批量拷贝后，用 Relaxed/Release 推进某个 `back`」的地方才需要。

#### 4.3.3 源码精读

**调用点 1 — `Worker::push`**（[src/deque.rs:422-432](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L422-L432)）：写完单个任务槽位后发布 `back`。

```rust
// ThreadSanitizer does not understand fences, ...
#[cfg(not(crossbeam_sanitize_thread))]
atomic::fence(Ordering::Release);
let store_order = if cfg!(crossbeam_sanitize_thread) {
    Ordering::Release
} else {
    Ordering::Relaxed
};
self.inner.back.store(b.wrapping_add(1), store_order);
```

**调用点 2 — `Stealer::steal_batch_with_limit`**（[src/deque.rs:911-921](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L911-L921)）：批量偷取后，向目的 `Worker` 的 `back` 发布整批写入。`steal_batch`（无 `_with_limit`）只是转发到本方法（上限 `MAX_BATCH=32`）。

**调用点 3 — `Stealer::steal_batch_with_limit_and_pop`**（[src/deque.rs:1164-1174](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1164-L1174)）：同上，但额外弹出一个任务。

**调用点 4 — `Injector::steal_batch_with_limit`**（[src/deque.rs:1712-1724](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1712-L1724)）：从 Injector 批量搬到目的 `Worker` 后发布 `back`。

**调用点 5 — `Injector::steal_batch_with_limit_and_pop`**（[src/deque.rs:1921-1933](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1921-L1933)）：同上，但额外弹出一个任务。

这 5 处的源码片段**逐字相同**（只有缩进或上下文不同），项目用注释串起来：

```rust
// ThreadSanitizer does not understand fences, so we omit fence and do store with Release ordering.
#[cfg(not(crossbeam_sanitize_thread))]
atomic::fence(Ordering::Release);
let store_order = if cfg!(crossbeam_sanitize_thread) {
    Ordering::Release
} else {
    Ordering::Relaxed
};
dest.inner.back.store(dest_b, store_order);
```

一句话总结这条线：**凡是「写完普通内存后用一条 store 把成果发布出去」的地方，都套这个双路径**；而 Injector 内部 `push` 用 `fetch_or(WRITE, Release)` 这种「带 release 序的原子 RMW」发布，天然对 tsan 友好，故不需要。

#### 4.3.4 代码实践（本讲主实践）

对照本讲规格里的代码实践任务，分两步走。

**步骤 A — 确认双路径确实被编译期二选一（可本地验证）：**

1. 在本 crate 加一个临时 example（或临时改一个 `#[test]`），打印：
   ```rust
   println!("crossbeam_sanitize_thread = {}", cfg!(crossbeam_sanitize_thread));
   ```
2. 普通 `cargo build`：输出 `false`。
3. `RUSTFLAGS="--cfg crossbeam_sanitize_thread" cargo build`：输出 `true`。
4. 结论：`cfg!()` 与 `#[cfg]` 都随构建参数翻转，确认下游 5 处双路径会被正确选中。

**步骤 B — 解释为什么 tsan 模式要用 Release store 替代 fence（本实践核心）：**

回顾 [src/deque.rs:911-921](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L911-L921)（`steal_batch_with_limit` 末尾）这一段。在普通构建里，它先 `fence(Release)` 再 `dest.back.store(Relaxed)`；在 tsan 构建里，fence 被 `#[cfg(not(...))]` 移除，store 改成 `Release`。写一段说明，要点如下：

- tsan 以「同位置的 `store(Release)` ↔ `load(Acquire)`」原子对作为建立 happens-before 边的主要来源。
- `steal_batch` 先把一批任务**非原子**地拷进目的 `Worker` 的 `Buffer` 槽位（`dest_buffer.write(...)`），再用一条 `dest.back.store(...)` 公布这批写入。
- 若该 store 是 `Relaxed`，tsan 不会把它当成 release 戳，于是「拷贝槽位」与「目的线程后续 `pop` 读槽位」之间没有可见同步边 → 假阳性。
- 改成 `store(Release)` 后，这条 store 自带 release 戳，目的线程 `pop` 里 `load(Acquire)`（见 [src/deque.rs:452-453](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L452-L453) 附近读 `back`）就能与之建边，把整批拷贝纳入可见范围，假阳性消失。

**关于「对比生成的汇编」的说明（重要，避免误判）：**

> 在 x86_64 上，由于该架构是 TSO（强内存模型），`fence(Release)` 与 `store(Release)`、甚至 `store(Relaxed)` 大概率都编译成同样的普通 `mov` 指令（`fence(Release)` 在 x86 上通常被优化为空操作）。因此**在 x86 上对比两种构建的汇编，可能看不出差异**——这并不代表双路径无效，而是 x86 本身不需要这些屏障。要看到实质差异，应在**弱内存架构（如 aarch64）**上对比，或直接通过步骤 A 的 `cfg!()` 打印确认源码路径已被选中。本步骤的汇编对比在 x86 上的具体表现属于「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 fence 用 `#[cfg(not(crossbeam_sanitize_thread))]` 移除，而 store 的序却用 `cfg!(...)` 运行时 `if` 来选？

**答案**：因为 `fence(...)` 是一条语句，用属性 `#[cfg]` 可以整条编译期删除；而 `Ordering` 是 `store` 调用里的一个**参数值**，属性 cfg 不能用来「挑值」，只能用编译期布尔宏 `cfg!()` 配合 `if`/`else`，再由优化器折成常量。两者都零成本、都在编译期定案。

**练习 2**：5 处双路径都出现在「向某个 `Worker` 的 `back` 发布写入」的地方。`Injector::push` 也写内存（写 Slot），为什么它不需要这个双路径？

**答案**：`Injector::push` 发布时用的是 `slot.state.fetch_or(WRITE, Ordering::Release)`（[src/deque.rs:1435](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1435)），这是一条**带 Release 序的原子读-改-写**，消费侧 `Slot::wait_write` 用 `load(Acquire)` 配对（[src/deque.rs:1224-1229](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1224-L1229)）。因为发布本身是原子的、且直接带 release 戳，tsan 能直接建模，所以不需要额外的 fence/Release-store 二选一。

## 5. 综合实践

把本讲三块知识串起来，完成一次「让 crossbeam-deque 在 tsan cfg 下编译并自检」的端到端走查。

**任务**：在本仓库（或一个依赖 `crossbeam-deque` 的临时 crate）里，验证 tsan 双路径被正确点亮，并解释它如何消除一个具体的假阳性。

1. **点亮 cfg**：用 `RUSTFLAGS="--cfg crossbeam_sanitize_thread" cargo build`（快速路径）或按 `ci/san.sh` 用自定义 target `x86_64-unknown-linux-gnutsan` + `-Z build-std`（项目真实路径）构建。
2. **确认路径**：用 4.3.4 步骤 A 的 `cfg!()` 打印，确认 `true`。
3. **追踪一条发布链**：选 `Stealer::steal_batch_with_limit`（[src/deque.rs:746-925](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L746-L925)）：
   - 找到批量拷贝槽位的循环（如 [src/deque.rs:797-802](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L797-L802) 的 `dest_buffer.write(...)`）。
   - 走到末尾 [src/deque.rs:911-921](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L911-L921)，确认在 tsan cfg 下走的是「无 fence + `store(Release)`」。
4. **写说明**：用一段话回答——如果没有这个双路径（即始终 `fence(Release)` + `store(Relaxed)`），tsan 会在 `dest_buffer.write` 与目的线程随后 `pop` 读同一槽位之间报什么样的竞争？为什么改成 `store(Release)` 后 tsan 就能连上同步边？
5. **预期产出**：一段 200 字左右的说明，覆盖「tsan 以原子 store/load 对建边 → Relaxed store 不带戳 → Release store 带戳」这条因果链。
6. **待本地验证**：真正用 TSan 跑出「修改前报警告、修改后干净」的对照实验，需要 nightly + 自定义 target（见 `ci/san.sh`）；本地无该工具链时，本步标注为「待本地验证」，仅完成 1-5 的静态走查即可。

## 6. 本讲小结

- tsan 报假阳性的根因：`Buffer` 槽位用 volatile 非原子读写，正确性靠 `fence(Release)` 保证，而 **tsan 不理解 fence**，无法建立同步边。
- 修复思路：tsan 模式下把 release 语义**焊到 `store` 上**（`store(Release)`），让 tsan 通过同位置的 `Release` store ↔ `Acquire` load 对建边；语义仍正确。
- `build.rs` 读 `CARGO_CFG_SANITIZE`（Cargo 对未稳定的 `cfg(sanitize="..")` 的转译），命中 `thread` 时点亮自定义 cfg `crossbeam_sanitize_thread`；并用 `cargo:rustc-check-cfg` 抑制 unexpected-cfg 告警。
- 源码用 `#[cfg(not(crossbeam_sanitize_thread))]` 移除 fence 语句、用 `cfg!(...)` 编译期挑 store 序，二者都是**编译期**定案、零运行时开销。
- 双路径共 **5 处**，全在「向目的 `Worker` 的 `back` 发布写入」的位置；`Injector::push` 因用原子 `fetch_or(WRITE, Release)` 发布而无需此路径。
- 在 x86 上对比汇编可能看不出差异（TSO 强模型），需在弱内存架构上或用 `cfg!()` 打印来验证路径选择。

## 7. 下一步学习建议

- 回到 **u4-l1** 对照本讲：把「volatile 读写 hack」与「tsan 双路径」看成同一个问题的两面——一个是为通用 `T` 做的性能折中，另一个是给这个折中做的工具兼容。
- 继续 **u4-l4（测试体系）**：看 `tests/` 里 `cfg!(miri)`、`option_env!(MIRI_FALLIBLE_WEAK_CAS)` 等测试技巧，与本讲的 `crossbeam_sanitize_thread` 一起，构成 crossbeam-deque 的「多检测器协同」工程实践。
- 拓展阅读：本 crate 依赖的 `crossbeam-epoch` 也有类似的 sanitizer 兼容写法，可作为横向对照；并建议阅读 ci/san.sh 与目标 spec，理解「自定义 target + `-Z build-std` 跑 ASAN/MSAN/TSAN」的完整工作流。
