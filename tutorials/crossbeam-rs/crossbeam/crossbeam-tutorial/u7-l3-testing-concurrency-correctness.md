# 测试、loom 与并发正确性

> 本讲是 crossbeam 学习手册的收官篇。前面六单元我们读完了「怎么实现」无锁数据结构与 epoch 回收,本讲回答最后一个、也是最关键的问题:**凭什么相信这些 `unsafe` 代码是对的?**

## 1. 本讲目标

学完本讲,你应当能够:

- 说清「并发 bug 为什么难以用普通单元测试发现」,并理解 crossbeam 为此部署的**三层正确性防线**。
- 掌握 **loom 模型检查**的原理:它如何用「穷举线程交错」替代「碰运气跑测试」,以及 crossbeam 用 `--cfg crossbeam_loom` 与内部 `primitive` 抽象层把同一份源码在「真线程」与「loom 模型」之间无缝切换。
- 掌握 **miri** 与 **sanitizer**(ASAN/MSAN/TSAN)各自检测什么类型的错误,以及为什么 crossbeam-deque 这种「安全的数据竞争」需要专门的抑制规则与特殊 flag。
- 读懂 crossbeam 的 **CI 流水线**如何把这些工具编排成一张矩阵,以及 **benchmarks** 如何守护「正确但不能变慢」。

## 2. 前置知识

本讲假设你已经读过 u5(crossbeam-epoch)与 u6(crossbeam-deque)。在进入测试之前,先用一段话建立直觉:为什么并发代码「测过」不等于「正确」。

串行代码的 bug 通常是**确定性的**:同样的输入永远复现同样的错误。但并发代码的 bug 由**线程交错(interleaving)**决定——哪一个线程的原子操作先执行、哪一个后执行,是操作系统调度器在运行时随机决定的。于是会出现两类典型的「隐藏 bug」:

- **数据竞争(data race)**:两个线程并发读写同一地址,且至少一个是写、且无同步。读到「半新半旧」的撕裂值,后果不可预测。
- **死锁 / 丢失唤醒**:线程 A 等 B、B 等 A,或唤醒信号在 park 之前发出而被吞掉。这类问题可能跑一百万次才触发一次。

普通 `cargo test` 用真实线程跑真实调度,本质是**抽样**——它只覆盖了天文数字般多的交错中的极少数几条。抽样能发现「频繁出现」的 bug,但无法证明「罕见交错」里没有 bug。crossbeam 全是手写 `unsafe`、满是 CAS 循环与延迟释放的并发代码,光靠抽样远远不够。于是它部署了三层互补的防线:

| 防线 | 工具 | 回答的问题 |
|------|------|-----------|
| 第一层:逻辑正确性 | **loom**(模型检查) | 在给定边界内,**所有**线程交错都安全吗? |
| 第二层:内存安全 | **miri**(UB 检测) | 有没有**未定义行为**(悬垂指针、越界、provenance 违规)? |
| 第三层:硬件级竞争 | **sanitizer**(ASAN/MSAN/TSAN) | 真实多核上有无**数据竞争 / use-after-free / 读未初始化**? |

外加一条横切的工程保障:**CI 矩阵**确保每次提交都过全套,**benchmarks** 确保「修 bug 不能把性能拖垮」。下面三节分别展开。

## 3. 本讲源码地图

本讲涉及的文件横跨 CI 脚本、构建配置与少量源码,不再深入任何算法实现(那些在前六单元已讲完):

| 文件 | 作用 |
|------|------|
| `ci/crossbeam-epoch-loom.sh` | loom 测试入口:注入 `crossbeam_loom` cfg、设置抢占上限 |
| `crossbeam-utils/src/lib.rs` | 内部 `primitive` 抽象层:在 loom / std 之间切换原子与同步原语 |
| `crossbeam-epoch/Cargo.toml` | 声明 `loom` 特性与 `loom-crate` 可选依赖 |
| `crossbeam-epoch/tests/loom.rs` | 用 loom 做模型检查的真实测试(Treiber 栈) |
| `ci/miri.sh` | miri 入口:配置严格 provenance、随机布局、按 crate 调参 |
| `ci/san.sh` | sanitizer 入口:依次跑 ASAN / MSAN / TSAN |
| `ci/tsan` | TSAN 抑制规则:声明「安全的数据竞争」 |
| `crossbeam-deque/src/deque.rs`、`crossbeam-deque/build.rs` | sanitizer 感知代码:TSAN 下用 Release store 替代 fence |
| `.github/workflows/ci.yml` | CI 流水线:把上述工具编排成 job 矩阵 |
| `crossbeam-channel/benchmarks/` | 性能基准:多实现横向对比 |

## 4. 核心概念与源码讲解

### 4.1 loom:`crossbeam_loom` cfg 与内部 `primitive` 抽象

#### 4.1.1 概念说明

loom 是 Tokio 团队出品的并发**模型检查器(model checker)**。它的核心思想是:与其在真实 CPU 上「碰运气」跑测试,不如把多线程程序的执行建模成一个**有限状态机**,然后**穷举**所有可达状态,证明其中不存在数据竞争与死锁。

loom 怎么做到穷举?它把每一个原子操作(以及锁的获取/释放)都当成一个**让步点(yield point)**。在这些点上,loom 不是真的并发执行,而是**主动选择**「下一步让哪个线程运行」,枚举所有可能的线程交错顺序。每条交错路径走到尽头,loom 就回退到某个分叉点、换一种顺序再走一遍,直到所有交错都跑过。

这种做法的代价是**状态空间爆炸**:交错数量随线程数与让步点数量**指数级**增长。loom 用一个关键参数控制规模——`LOOM_MAX_PREEMPTIONS`,即允许「抢占」(一个线程在别人没主动让步时被中断)的最大次数。粗略地说:

\[
\text{状态数} \;\approx\; O\!\left(C^{P}\right),\quad C=\text{让步点数},\;P=\text{最大抢占次数}
\]

\(P\) 每加 1,状态数就翻数倍。因此 crossbeam 把它压到很小的值(见 4.1.2)。

> 一句话直觉:**loom 把「概率性的并发 bug」变成「确定性的、可证明不存在」的检查**——只要模型在边界内没发现问题,就说明在同样的边界约束下,真实硬件也不会触发这些特定的交错 bug。

#### 4.1.2 核心流程

要让 loom 能接管调度,代码里所有「线程并发交互」的 API(原子类型、`Mutex`、`Condvar`、`Arc`、`spin_loop` 提示)都必须换成 loom 提供的版本。crossbeam 的巧妙之处在于:**业务源码一行不改**,只靠一个编译期开关 `--cfg crossbeam_loom` 在两套实现间切换。运行 loom 测试的完整流程是:

1. 设置 `RUSTFLAGS="--cfg crossbeam_loom"`,让 `#[cfg(crossbeam_loom)]` 的代码分支被选中。
2. 开启 `loom` 特性(它又连带开启 `crossbeam-utils/loom`),引入 `loom-crate` 依赖。
3. 业务代码中 `use crate::primitive::sync::atomic::AtomicUsize` 这类引用,在 loom 模式下解析为 `loom::sync::atomic::AtomicUsize`(可被 loom 接管调度的版本),否则解析为 `core::sync::atomic::AtomicUsize`(真实硬件版本)。
4. 测试用 `loom::model(|| { ... })` 包裹一段并发场景,loom 自动在其中枚举所有交错。
5. 用 `LOOM_MAX_PREEMPTIONS` 限制抢占次数,把状态空间压在可承受范围内。

这套切换之所以能成立,关键就是下面要精读的 `primitive` 抽象层。

#### 4.1.3 源码精读

**抽象层:在 loom 与 std 之间换皮。** `crossbeam-utils/src/lib.rs` 顶部用 `#[cfg(crossbeam_loom)]` 与 `#[cfg(not(crossbeam_loom))]` 定义了**两份同名**的内部模块 `primitive`:

[crossbeam-utils/src/lib.rs:47-69](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs#L47-L69) —— loom 版本,把 `AtomicBool/AtomicUsize/fence`、`Arc/Mutex/Condvar`、`hint::spin_loop` 全部 `pub(crate) use` 自 `loom::`:

```rust
#[cfg(crossbeam_loom)]
mod primitive {
    pub(crate) mod hint {
        pub(crate) use loom::hint::spin_loop;
    }
    pub(crate) mod sync {
        pub(crate) mod atomic {
            pub(crate) use loom::sync::atomic::{
                AtomicBool, AtomicIsize, AtomicUsize, Ordering, fence, /* ... */,
            };
            // loom 暂不支持 compiler_fence,用更强的 fence 顶替(可能多报一些竞争)
            pub(crate) use self::fence as compiler_fence;
        }
        pub(crate) use loom::sync::{Arc, Condvar, Mutex};
    }
}
```

[crossbeam-utils/src/lib.rs:70-83](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs#L70-L83) —— 真机版本,同样的符号名来自 `core`/`alloc`/`std`:

```rust
#[cfg(not(crossbeam_loom))]
mod primitive {
    pub(crate) mod hint {
        pub(crate) use core::hint::spin_loop;
    }
    pub(crate) mod sync {
        #[cfg(feature = "std")] pub(crate) use alloc::sync::Arc;
        pub(crate) use core::sync::atomic;
        #[cfg(feature = "std")] pub(crate) use std::sync::{Condvar, Mutex};
    }
}
```

于是,`backoff.rs`、`parker.rs`、`atomic_cell.rs` 等所有源文件里写的是 `use crate::primitive::sync::{...}`(见前几讲),而非直接写 `std::sync::...`。这就是「换皮」的落点:**业务代码用统一的 `primitive` 路径,编译期 cfg 决定它背后是 loom 还是 std**。注意 loom 版有个小妥协——loom 尚不支持 `compiler_fence`,这里用更强的 `fence` 顶替(源码注释明确说明这可能多报一些竞争,是当前能做到的最好折中)。

**特性与依赖。** `crossbeam-epoch` 把 loom 声明为可选特性,且明确标注它**不受 semver 保证**:

[crossbeam-epoch/Cargo.toml:32-38](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/Cargo.toml#L32-L38) —— `loom` 特性连带开启 `crossbeam-utils/loom`:

```toml
# Enable the use of loom for concurrency testing.
# NOTE: This feature is outside of the normal semver guarantees ...
loom = ["loom-crate", "crossbeam-utils/loom"]
```

[crossbeam-epoch/Cargo.toml:44-46](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/Cargo.toml#L44-L46) —— 仅当 `cfg(crossbeam_loom)` 时才把 `loom-crate`(`package = "loom"`)作为可选依赖拉进来:

```toml
[target.'cfg(crossbeam_loom)'.dependencies]
loom-crate = { package = "loom", version = "0.7.1", optional = true }
```

**入口脚本。** [ci/crossbeam-epoch-loom.sh:6](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/ci/crossbeam-epoch-loom.sh#L6) 同时点亮两个 cfg(loom 与 sanitize),[ci/crossbeam-epoch-loom.sh:11](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/ci/crossbeam-epoch-loom.sh#L11) 把抢占上限设为 2:

```bash
export RUSTFLAGS="${RUSTFLAGS:-} --cfg crossbeam_loom --cfg crossbeam_sanitize"
# With MAX_PREEMPTIONS=2 the loom tests (currently) take around 11m.
# If we were to run with =3, they would take several times that ...
env LOOM_MAX_PREEMPTIONS=2 cargo test --test loom --release --features loom -- --nocapture
```

脚本注释道破状态空间爆炸的代价:`=2` 已耗时约 11 分钟,`=3` 将是好几个 11 分钟——这正是 4.1.1 中那条指数曲线的真实体现。

**一个真实的 loom 测试。** [crossbeam-epoch/tests/loom.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/tests/loom.rs) 整个文件用 `#[cfg(crossbeam_loom)]` 包裹(平时根本不编译)。它内嵌了一个完整的 **Treiber 无锁栈**,再用 `loom::model` 枚举两个线程并发 push/pop 的所有交错:

[crossbeam-epoch/tests/loom.rs:139-159](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/tests/loom.rs#L139-L159) —— 测试体里主线程与 spawn 出去的线程各 push/pop 5 次,最后断言栈已空。注意它刻意用 `5`(大于 sanitize 特性用的 4),让交错更丰富:

```rust
loom::model(|| {
    let stack1 = Arc::new(TreiberStack::new());
    let stack2 = Arc::clone(&stack1);
    let jh = spawn(move || {
        for i in 0..5 { stack2.push(i); assert!(stack2.pop().is_some()); }
    });
    for i in 0..5 { stack1.push(i); assert!(stack1.pop().is_some()); }
    jh.join().unwrap();
    assert!(stack1.pop().is_none());
    assert!(stack1.is_empty());
});
```

loom 会在 `loom::model` 闭包内,把所有原子操作(CAS、load、store)与 spawn/join 当作让步点,穷举这两个线程在这些点上的交错顺序。只要存在**任何一条**交错会让断言失败或触发数据竞争,loom 就会报告并给出那条具体的执行路径。这就是「证明」而非「抽样」。

#### 4.1.4 代码实践

**实践目标:** 在本地亲手跑一次 loom 模型检查,感受它如何枚举交错并报告问题。

**操作步骤:**

1. 进入 epoch 子目录:`cd crossbeam-epoch`(脚本本身也会 `cd`)。
2. (可选)把测试里某个断言改坏,人为注入 bug,例如把 `treiber_stack` 的 `pop` 中的 `compare_exchange` 误改成 `Relaxed` 内存序(去掉 `Release`),保存。
3. 运行官方脚本:`bash ../ci/crossbeam-epoch-loom.sh`(或直接 `env LOOM_MAX_PREEMPTIONS=2 cargo test --test loom --release --features loom -- --nocapture`)。

**需要观察的现象:**

- 编译会重新拉取 `loom` crate(`loom` 特性被点亮)。
- 测试开始运行后,单条用例会反复执行(loom 在枚举不同交错),耗时明显长于普通测试。

**预期结果:** 正常代码应全部通过且**无数据竞争报告**;若你改坏了内存序,loom 大概率会报告一条具体的交错路径触发了问题(数据竞争或断言失败)。**待本地验证**(loom 报告的精确文本取决于你注入的 bug)。

> 若本地无 nightly/stable 工具链或不愿改源码,可改为**源码阅读型实践**:打开 `crossbeam-epoch/tests/loom.rs`,对照 u5 讲过的 `Atomic`/`Owned`/`compare_exchange`/`defer_destroy`,逐行解释 Treiber 栈的 push/pop 为何在 loom 的任意交错下都安全。

#### 4.1.5 小练习与答案

**练习 1:** 为什么 `primitive` 抽象层必须用 `pub(crate)` 而不能直接在每个源文件里 `use std::sync::atomic::AtomicUsize`?

**参考答案:** 因为那样写就把原子类型**写死**成 std 实现了,无法在 loom 模式下替换成 `loom::sync::atomic::AtomicUsize`。统一走 `crate::primitive::...`,再用 `#[cfg(crossbeam_loom)]` 在一处决定背后是哪套实现,业务源码才能「一份源码、两种执行语义」。

**练习 2:** `ci/crossbeam-epoch-loom.sh` 为什么把 `LOOM_MAX_PREEMPTIONS` 设为 2 而不是 10?

**参考答案:** loom 状态数随抢占次数指数增长(约 \(O(C^P)\))。`=2` 已耗时约 11 分钟;设为 10 会让状态空间爆炸到 CI 无法承受。这是「覆盖深度」与「运行成本」之间的工程取舍——用较小的边界保证「在 2 次抢占内的所有交错都正确」。

---

### 4.2 miri 与 sanitizer:UB 与数据竞争检测

#### 4.2.1 概念说明

loom 检查的是「逻辑层的交错正确性」,但它**不执行真实机器码**,因而无法发现一类更底层的问题:**未定义行为(UB, undefined behavior)**与**真实的硬件数据竞争**。这两者由另外两组工具负责。

**miri** 是 Rust 官方的 UB 检测器。它不把代码编译成机器码运行,而是**解释执行** Rust 的中间表示(MIR)。因为是解释器,它能精确追踪每块内存的「借用状态」「provenance(指针来源)」「是否已初始化」,从而发现:

- 解引用悬垂指针、越界访问、重复释放(use-after-free / out-of-bounds)。
- 违反别名规则(Stacked Borrows / Tree Borrows 模型)。
- 用整数拼出「假指针」再解引用(provenance 违规)。
- **通过弱内存模型模拟发现的数据竞争**(miri 可模拟乱序与弱序)。

miri 是 crossbeam 这种满地 `unsafe` 与裸指针代码的「体检仪」——它能在 CI 里直接拦截绝大多数 UB。

**sanitizer** 是编译器层面的运行时插桩,在真实(或接近真实)的多核上跑出问题。crossbeam 用到三种:

| 工具 | 全称 | 检测 |
|------|------|------|
| ASAN | AddressSanitizer | 堆/栈/全局越界、use-after-free、栈返回后使用 |
| MSAN | MemorySanitizer | 读取**未初始化**的内存 |
| TSAN | ThreadSanitizer | **数据竞争**(无同步的并发读写) |

三者互补:ASAN/MSAN 管内存,TSAN 管并发。

#### 4.2.2 核心流程

**miri 的严格化配置。** [ci/miri.sh:12-14](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/ci/miri.sh#L12-L14) 给 miri 叠了一组「最严格」的开关:

```bash
export RUSTFLAGS="${RUSTFLAGS:-} -Z randomize-layout"
export MIRIFLAGS="${MIRIFLAGS:-} -Zmiri-strict-provenance -Zmiri-symbolic-alignment-check -Zmiri-disable-isolation"
```

- `-Z randomize-layout`:随机化结构体内存布局,暴露代码对字段偏移/布局的隐式依赖(裸指针代码极易踩雷)。
- `-Zmiri-strict-provenance`:严格追踪指针来源,禁止「整数拼指针」。
- `-Zmiri-symbolic-alignment-check`:更严格的对齐检查。
- `-Zmiri-disable-isolation`:允许测试调用系统 API(miri 默认隔离)。

CI 还会在 miri job 的矩阵里加跑 `-Zmiri-tree-borrows`(见 4.3),即用 **Tree Borrows** 别名模型替代默认的 Stacked Borrows——两种模型各能抓到对方抓不到的别名违规,所以两个都跑。

**按 crate 调参。** 不同子 crate 有不同的「已知怪癖」,miri.sh 用 per-crate 的环境变量与 flag 处理:[ci/miri.sh:36-40](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/ci/miri.sh#L36-L40):

```bash
# -Zmiri-preemption-rate=0 is needed because this code technically has UB and Miri catches that.
MIRI_FALLIBLE_WEAK_CAS='1' \
MIRIFLAGS="${MIRIFLAGS} -Zmiri-preemption-rate=0" \
  cargo miri test --all-features -p crossbeam-deque 2>&1 | ts -i '%.s  '
```

这里有个**非常重要的工程信号**:Chase-Lev 双端队列(u6)的某些操作「技术上存在 UB」(注释原话),靠 `-Zmiri-preemption-rate=0`(禁止抢占,让每步原子操作不被打断)与 `MIRI_FALLIBLE_WEAK_CAS`(让弱 CAS 可失败)绕过 miri 的报告。channel 的 detached 线程测试则用 `-Zmiri-ignore-leaks`(线程不 join,内存「泄漏」是预期的)。

**sanitizer 三连。** [ci/san.sh:10](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/ci/san.sh#L10) 先点亮 `--cfg crossbeam_sanitize`(让源码知道「现在在做 sanitize 检测」),然后依次:

- [ci/san.sh:16-18](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/ci/san.sh#L16-L18) ASAN,用预编译了 sanitizer 的 std target `x86_64-unknown-linux-gnuasan`。
- [ci/san.sh:29-30](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/ci/san.sh#L29-L30) MSAN,用 `-Z build-std` **自行编译插桩版标准库**(MSAN 要求连 std 都插桩)。
- [ci/san.sh:32-35](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/ci/san.sh#L32-L35) TSAN,同样 `-Z build-std`,并加载抑制文件 `ci/tsan`。

**为什么 deque 需要 TSAN 抑制?** Chase-Lev 队列的 `push` 与 `steal` **故意**存在无同步的并发读写——这是算法的固有特性:读到「半新半旧」的值时,算法会**丢弃它并重试**(u6 讲过 `Steal::Retry`)。这种「安全的数据竞争」对 TSAN 来说看上去就是数据竞争,会被误报。于是 [ci/tsan:3-11](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/ci/tsan#L3-L11) 显式声明「这些竞争是安全的,别报」:

```text
# Push and steal operations in crossbeam-deque may cause data races, but such
# data races are safe. If a data race happens, the value read by `steal` is
# forgotten and the steal operation is then retried.
race:crossbeam_deque*push
race:crossbeam_deque*steal

# Non-lock-free AtomicCell uses SeqLock which uses fences.
race:crossbeam_utils::atomic::atomic_cell::atomic_compare_exchange_weak
```

第三条同样关键:非 lock-free 的 `AtomicCell`(u2-l3 的全局序列锁兜底)也靠「乐观读 + 重试」处理竞争,故其 CAS 也被抑制。

#### 4.2.3 源码精读

**sanitize 感知代码:fence vs store 的取舍。** 上面的抑制只是「让工具闭嘴」;更优雅的做法是**让代码主动配合工具**。`crossbeam-deque` 就这么做了——它用构建脚本探测「当前是否在 TSAN 下」,据此切换内存序策略。

[crossbeam-deque/build.rs:9-13](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/build.rs#L9-L13) 读取 `CARGO_CFG_SANITIZE`,若含 `thread` 就发出 cfg `crossbeam_sanitize_thread`:

```rust
// `cfg(sanitize = "..")` is not stabilized.
let sanitize = env::var("CARGO_CFG_SANITIZE").unwrap_or_default();
if sanitize.contains("thread") {
    println!("cargo:rustc-cfg=crossbeam_sanitize_thread");
}
```

> 注意 `cfg(sanitize = "thread")` 在 Rust 中尚未稳定,crossbeam 不能直接用,所以走「构建脚本读环境变量 + 自定义 cfg」的曲线方案;`Cargo.toml` 里的 `unexpected_cfgs` 同时把这些自定义 cfg 登记进 `check-cfg`(详见 4.3.3),让编译器不再警告「未知 cfg」。

[crossbeam-deque/src/deque.rs:422-432](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L422-L432) 是 `push` 写入任务后的同步点。普通构建走「`fence(Release)` + `Relaxed` store」;TSAN 下改成「无 fence + `Release` store」:

```rust
// ThreadSanitizer does not understand fences, so we omit fence and do store with Release ordering.
#[cfg(not(crossbeam_sanitize_thread))]
atomic::fence(Ordering::Release);
let store_order = if cfg!(crossbeam_sanitize_thread) {
    Ordering::Release
} else {
    Ordering::Relaxed
};
self.inner.back.store(b.wrapping_add(1), store_order);
```

直觉解释(承接 u6):`push` 先把任务写进槽位,再推进 `back` 索引。读者通过 `back` 的前进来「看见」任务,所以「写任务」必须 happens-before「推进 back」。真机上用 `fence(Release)` + `Relaxed store` 即可建立这条顺序;但 **TSAN 不理解 fence**(它会把 `fence` 后的 `Relaxed` 读写误判为竞争),因此在 TSAN 下改成等价但 TSAN 友好的 `Release store`——同样的同步语义,换一种 TSAN 认得的表达。这是「在工具的限制下,用等价写法让工具既能跑通、又不误报」的典范,也是为什么 TSAN 抑制文件里只列了 `push/steal` 而没有列 `back.store`——后者已被代码层面解决。

#### 4.2.4 代码实践

**实践目标:** 用 miri 在 crossbeam-utils 上跑一次 UB 检测,理解它报告什么。

**操作步骤:**

1. 安装 miri:`rustup +nightly component add miri`(CI 用的是 `rust@nightly+miri`)。
2. 运行:`cargo +nightly miri test -p crossbeam-utils`(或带 `--all-features`)。
3. (对比)再跑 sanitizer 版:`RUSTFLAGS="--cfg crossbeam_sanitize" bash ci/san.sh`(仅 Linux)。

**需要观察的现象:**

- miri 会先「Preparing a sysroot」(为解释执行重建标准库),首次较慢。
- 测试会逐个跑,遇到任何 UB 会立即停下并打印调用栈。

**预期结果:** 全部通过、无 UB 报告。若想看到 miri「抓 bug」,可临时写一个解引用 `Box::into_raw` 后已 `drop` 的指针的小测试,miri 会报 `pointer ... has been freed`。**待本地验证**(精确输出取决于环境与 nightly 版本)。

#### 4.2.5 小练习与答案

**练习 1:** 为什么 MSAN/TSAN 要用 `-Z build-std` 重新编译标准库,而 ASAN 不需要?

**参考答案:** ASAN 有官方预编译好的插桩 std target(`x86_64-unknown-linux-gnuasan`)可直接用;而 MSAN/TSAN 没有这样的现成 target,必须用 `-Z build-std` 把标准库一并插桩编译——否则标准库内部的不插桩读写会成为检测盲区甚至误报源头。

**练习 2:** `ci/tsan` 抑制了 `crossbeam_deque*steal` 的竞争。这会不会把**真正的** bug 也藏起来?

**参考答案:** 有风险,但可控。抑制是按符号名的粗粒度规则,理论上可能掩盖该符号下新增的真正竞争。crossbeam 通过**多工具交叉验证**来对冲这个风险:deque 的竞争正确性同时由 loom(模型检查)和 miri(UB 检测,且用 `MIRI_FALLIBLE_WEAK_CAS` + `preemption-rate=0` 适配)兜底;TSAN 这里只负责「会不会出现工具不认得的额外同步问题」,而算法本身的安全竞争已被另两层覆盖。

---

### 4.3 CI 流水线与 benchmarks 性能对比

#### 4.3.1 概念说明

前两节介绍了 loom、miri、sanitizer 这些「重武器」。但工具再多,如果不在每次提交时**自动、全员**地跑,就形同虚设。CI(持续集成)就是把它们编排成一张**作业矩阵**的调度系统。crossbeam 的 CI 有两条贯穿始终的纪律:

1. **`-D warnings`**(见 ci.yml 的 `RUSTFLAGS`/`RUSTDOCFLAGS`):把**警告当错误**。对满地 `unsafe` 的代码,任何告警都可能是潜在 UB 的信号,不能放过。
2. **宽矩阵**:同时在 MSRV(1.74)/ stable / nightly、多 OS、多架构、多特性组合、多个无原子 no_std 目标上构建与测试,保证「换台机器也能编、也能跑」。

此外,并发代码还有个常被忽视的维度:**性能回归**。修一个 bug 顺手把吞吐砍半,是「正确但不可接受」的。crossbeam 在 `crossbeam-channel/benchmarks/` 下维护了一套与其它主流通道实现**横向对比**的基准,守护性能不退化。

#### 4.3.2 核心流程

**CI 把每类检查拆成独立 job,各自有矩阵与超时。** 与本讲相关的几个 job:

- `test`:核心功能测试。矩阵极宽——Rust 版本(MSRV/stable/nightly)× 操作系统(ubuntu/arm/windows/macos)× 目标架构(含 32 位、PowerPC、s390x、sparc64 等验证不同原子能力的平台)。脚本见 `ci/test.sh`,核心命令是 `cargo test --all --all-features --exclude benchmarks -- --test-threads=1`(`--test-threads=1` 避免测试间争用干扰诊断)。
- `features`:用 `cargo-hack --feature-powerset` 枚举特性**幂集**(所有子集组合),并在 `thumbv7m`(有原子 CAS)、`thumbv6m`(有原子但无 CAS)、`riscv32i`(完全无原子)三个 no_std 目标上构建,保证任意特性组合可编译。
- `miri`:见 4.2,矩阵为 `group(channel/others) × miriflags(默认/tree-borrows)`。
- `san`:见 4.2,依次 ASAN/MSAN/TSAN。
- `loom`:见 4.1。
- `tidy`:格式与 lint 守门——`rustfmt`、`shfmt`、`taplo`、`shellcheck`、`zizmor`(GitHub Actions 安全审计)。

**benchmarks 的对比维度。** 基准不只测「快不快」,更测「在不同并发拓扑下快不快」。`crossbeam-channel/benchmarks/README.md` 定义了 6 种拓扑:

| 拓扑 | 含义 |
|------|------|
| `seq` | 单线程发 N 条再收 N 条(无并发,测纯开销) |
| `spsc` | 1 发 1 收(单生产者单消费者) |
| `mpsc` | T 发 1 收 |
| `mpmc` | T 发 T 收(crossbeam 的强项,因 std 只支持 MPSC) |
| `select_rx` | T 路发送 + 1 个接收者用 select 多路收取 |
| `select_both` | 双侧都用 select |

默认 `N = 5_000_000`、`T = 4`,与 `mpsc`(std)、`flume`、`futures-channel`、Go 等实现同场竞技,最终用 `plot.py` 出图。

#### 4.3.3 源码精读

**miri / san / loom 三个 job 的编排。** [.github/workflows/ci.yml:200-224](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/.github/workflows/ci.yml#L200-L224) 的 miri job 用矩阵 `group × miriflags` 交叉,把 channel 与其它 crate、默认模型与 Tree Borrows 模型**全部覆盖**:

```yaml
miri:
  strategy:
    fail-fast: false
    matrix:
      group: [channel, others]
      miriflags: ['', '-Zmiri-tree-borrows']
  steps:
    - name: Install Rust
      uses: taiki-e/install-action@...
      with: { tool: rust@nightly+miri }
    - name: miri
      run: ci/miri.sh "${GROUP}"
      env:
        MIRIFLAGS: ${{ matrix.miriflags }}
        GROUP: ${{ matrix.group }}
```

`fail-fast: false` 意味着某条矩阵失败不会立即取消其它条——这样一次 CI 能看到**所有**平台/配置的结果,便于一次性修完。

[.github/workflows/ci.yml:240-266](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/.github/workflows/ci.yml#L240-L266) 的 `san` 与 `loom` job 则各自调用 `ci/san.sh` 与 `ci/crossbeam-epoch-loom.sh`:

```yaml
san:
  steps:
    - name: Install Rust
      with: { tool: rust@nightly+rust-src+x86_64-unknown-linux-gnuasan+x86_64-unknown-linux-gnutsan+x86_64-unknown-linux-gnumsan }
    - name: Run sanitizers
      run: ci/san.sh
loom:
  steps:
    - name: Install Rust
      with: { tool: rust@stable }
    - name: loom
      run: ci/crossbeam-epoch-loom.sh
```

注意 san job 一次性装齐三个 sanitizer target,loom job 只需 stable(loom 已稳定)。每个 job 都有 `timeout-minutes: 60` 防止状态空间爆炸把 CI 拖死。

**宽矩阵的 test job。** [.github/workflows/ci.yml:52-93](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/.github/workflows/ci.yml#L52-L93) 列出了全部测试矩阵条目,几条值得注意:

```yaml
- rust: '1.74'        # MSRV:最低支持版本,每个 PR 都守这条底线
  os: ubuntu-latest
- rust: nightly
  os: ubuntu-latest
  target: powerpc64le-unknown-linux-gnu   # 验证弱内存架构
- rust: nightly
  os: ubuntu-24.04-arm
  target: armv5te-unknown-linux-gnueabi   # 注释:无 AtomicU64/AtomicI64 的 32 位目标
- rust: stable
  os: ubuntu-latest
  target: sparc64-unknown-linux-gnu       # 注释:无稳定 inline asm 支持
```

这些「冷门架构」并非摆设:PowerPC/s390x 是弱内存模型,能暴露 `Relaxed`/`Acquire` 误用;armv5te 缺 64 位原子,验证 `AtomicCell` 退化为序列锁的路径;sparc64 验证无内联汇编时的降级(Backoff 的 `PAUSE` 指令)。配合 u1-l2 讲过的 `no_atomic.rs` 与 `crossbeam_no_atomic` cfg,这套矩阵钉死了「跨平台分级支持」的承诺。

**自定义 cfg 的合法化。** crossbeam 自定义了 `crossbeam_loom`、`crossbeam_sanitize` 等非标准 cfg。新版 Rust 会对未知 cfg 发 `unexpected_cfgs` 警告,而 CI 又是 `-D warnings`——这会直接报错。解法在根 [Cargo.toml:80-84](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/Cargo.toml#L80-L84) 把它们登记进 `check-cfg` 白名单:

```toml
unexpected_cfgs = { level = "warn", check-cfg = [
    'cfg(crossbeam_loom)',
    'cfg(crossbeam_sanitize)',
    'cfg(gha_macos_runner)',
] }
```

**benchmarks 如何对比。** [crossbeam-channel/benchmarks/crossbeam-channel.rs:5-6](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benchmarks/crossbeam-channel.rs#L5-L6) 定义规模,[:147-160](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benchmarks/crossbeam-channel.rs#L147-L160) 用 `run!` 宏计时每个拓扑 × 容量组合:

```rust
const MESSAGES: usize = 5_000_000;
const THREADS: usize = 4;
// ...
macro_rules! run {
    ($name:expr, $f:expr) => {
        let now = ::std::time::Instant::now();
        $f;
        let elapsed = now.elapsed();
        println!("{:25} {:15} {:7.3} sec", $name, "Rust crossbeam-channel",
            elapsed.as_secs() as f64 + elapsed.subsec_nanos() as f64 / 1e9);
    };
}
```

`main` 里 `run!` 把 6 种拓扑 × 多种容量(`bounded0`/`bounded1`/`bounded(MESSAGES)`/`unbounded`)全跑一遍(见 [:162-186](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benchmarks/crossbeam-channel.rs#L162-L186))。[crossbeam-channel/benchmarks/run.sh:6-10](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/benchmarks/run.sh#L6-L10) 再把 crossbeam-channel 与 `mpsc`/`flume`/`futures-channel`/Go 同场跑、各自落盘 `*.txt`,最后 `./plot.py` 出图:

```bash
cargo run --release --bin crossbeam-channel | tee crossbeam-channel.txt
cargo run --release --bin futures-channel | tee futures-channel.txt
cargo run --release --bin mpsc | tee mpsc.txt
cargo run --release --bin flume | tee flume.txt
go run go.go | tee go.txt
```

#### 4.3.4 代码实践

**实践目标:** 读 benchmarks 的 `run.sh`,说清 crossbeam 与其它通道实现的对比维度。

**操作步骤:**

1. 打开 `crossbeam-channel/benchmarks/run.sh` 与 `crossbeam-channel/benchmarks/README.md`。
2. 对照 `crossbeam-channel.rs` 的 `main`(列出所有 `run!` 调用),整理出「拓扑 × 容量」二维表。
3. 说明 `run.sh` 选了哪 5 个实现做横向对比,以及为什么注释说「图里只放 5 个,太多会让柱子重叠」。

**需要观察的现象 / 预期结果:** 你应当能画出一张表:行是 6 种拓扑(seq/spsc/mpsc/mpmc/select_rx/select_both),列是容量档(bounded0/bounded1/bounded/unbounded);并指出对比对象覆盖了 Rust 生态的 `std::sync::mpsc`、`flume`、`futures` channel 与 Go 的 channel,体现了「同语义、不同实现、同负载」的公平横评。

**若想实跑**(需 Rust + Go + Python+matplotlib):`cd crossbeam-channel/benchmarks && ./run.sh`,产出 `plot.png`。**待本地验证**(数值依赖机器,README 记录的参考机为 i7-5500U)。

#### 4.3.5 小练习与答案

**练习 1:** CI 的每个 job 都设 `fail-fast: false`。为什么不「失败即停」以节省时间?

**参考答案:** 因为宽矩阵的价值在于「一次看到所有平台/配置的结果」。若失败即停,你只能看到第一个失败的平台,修完重跑可能又冒出第二个平台的失败,反复多轮才能修完。`fail-fast: false` 让所有矩阵条目跑完,一次性暴露全部问题。

**练习 2:** benchmarks 为什么坚持把 Go 的 channel 也纳入对比,而不是只比 Rust 内部实现?

**参考答案:** Go 以 channel 为核心并发原语、久经验证,是行业事实基准。把它纳入对比,既证明 crossbeam-channel 在「与语言原生通道同语义」的赛道上具有竞争力,也避免「只跟自己人比」的自嗨——性能结论必须放在跨语言的公认参照系里才有说服力。

---

## 5. 综合实践

把本讲三层防线串起来,做一次「假想回归排查」小任务:

设想你给 crossbeam-deque 的 `steal` 提交了一个改动(比如把 `front` 的 `fetch_add` 改成了 `Relaxed` 的 `compare_exchange` 循环)。请按下面的顺序,逐一回答**哪个工具会在哪一步拦下你**:

1. **本地 `cargo test`**:大概率**全绿**(数据竞争是概率性的,抽样测不出来)——这正是为什么不能只靠它。
2. **`cargo +nightly miri test -p crossbeam-deque`**:若改动引入了 UB,miri 可能报错;但 deque 走 `MIRI_FALLIBLE_WEAK_CAS` + `preemption-rate=0` 的特殊配置,未必能抓到纯「内存序弱化」的问题。
3. **`bash ci/crossbeam-epoch-loom.sh`** 等价的 loom 模型检查:若有逻辑层的交错导致丢任务,loom 会给出**具体的那条交错路径**。
4. **`ci/san.sh` 的 TSAN**:若改动制造了新的、未被 `ci/tsan` 抑制的真实竞争,TSAN 会报 `WARNING: ThreadSanitizer: data race` 并给出两个线程的调用栈。
5. **benchmarks**:即使上述都过了,若改动让吞吐明显下降,`benchmarks` 的横评图会显示回归。

把这张「防线 × 触发条件」表写进你的笔记。它就是 crossbeam 用工程手段把「并发正确性」这个本来近乎玄学的问题,落成「可复现、可拦截、可回归」的关键。

## 6. 本讲小结

- 并发 bug 由线程交错决定,**普通单元测试只是抽样**,无法证明罕见交错里没有问题;crossbeam 部署 **loom + miri + sanitizer** 三层互补防线。
- **loom** 用模型检查**穷举**所有线程交错,把概率性 bug 变成可证明的不存在;crossbeam 靠 `--cfg crossbeam_loom` 与 `crossbeam-utils` 里的 `primitive` 抽象层,让业务源码在「真线程」与「loom 模型」间无缝切换,代价是状态空间随 `LOOM_MAX_PREEMPTIONS` 指数膨胀。
- **miri** 解释执行 MIR,精确检测 UB(悬垂指针、provenance、别名违规);配 `-Z randomize-layout`、`-Zmiri-strict-provenance`、`-Zmiri-tree-borrows` 等最严格开关,并按 crate 调参。
- **sanitizer** 在真实多核上抓内存与竞争:ASAN/MSAN/TSAN;对 deque 这种「安全的数据竞争」用 `ci/tsan` 抑制 + 构建脚本驱动的 `crossbeam_sanitize_thread` cfg(让代码在 TSAN 下用 `Release store` 替代它不认得的 `fence`)。
- **CI** 用 `-D warnings` + 宽矩阵(MSRV/stable/nightly × 多 OS × 多架构 × 特性幂集 × 无原子目标)把上述工具编排成自动门禁;`unexpected_cfgs` 的 `check-cfg` 白名单合法化了自定义 cfg。
- **benchmarks** 用 6 种并发拓扑 × 多容量档,与 `mpsc`/`flume`/`futures`/Go 横向对比,守护「正确但不能变慢」。

## 7. 下一步学习建议

至此,crossbeam 学习手册的 33 篇讲义已全部完成。建议你:

- **回归实战**:挑一个最小模块(如 `AtomicCell` 或 `ArrayQueue`),自己用 `loom::model` 写一个并发场景测试,体验「穷举交错」。
- **二次开发参考**:若你要写自己的无锁结构,直接复刻 crossbeam 的测试脚手架——把 `ci/miri.sh`、`ci/san.sh`、loom 测试与 `ci/tsan` 抑制文件搬到你的项目,是最低成本的并发正确性保障。
- **深入阅读**:对照本讲,精读 [`loom` 文档](https://docs.rs/loom)与 [Rust Reference 的「Undefined Behavior」](https://doc.rust-lang.org/reference/behavior-considered-undefined.html),理解 Tree Borrows 与 Stacked Borrows 的差异。
- **关注上游**:crossbeam-epoch 的 loom 测试与 `ci/tsan` 抑制规则会随上游演进,定期 `git log ci/` 可跟踪测试策略的更新。
