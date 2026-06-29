# 讲义：feature flags、portable-atomic 与 no_std/no-alloc 支持

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 ringbuf 的 6 个 feature（`default`/`std`/`alloc`/`portable-atomic`/`bench`/`test_local`）各自的作用，并画出它们之间的依赖级联图。
- 理解 Cargo 的「弱依赖」`?` 语法如何在 `std = [..., "portable-atomic?/std"]` 这样的声明中工作。
- 解释为什么 `portable-atomic` 能让 ringbuf 跑在 `thumbv6m-none-eabi` 这类「没有硬件 CAS 指令」的小芯片上，以及 `portable-atomic/critical-section` 这一「跨 crate 透传 feature」的含义。
- 看懂源码里两处「后端切换」：`SharedRb` 把 `AtomicUsize`/`AtomicBool` 在 `core::sync::atomic` 与 `portable_atomic` 之间切换；`alias.rs` 把 `Arc` 在 `alloc::sync::Arc` 与 `portable_atomic_util::Arc` 之间切换。
- 在 `#![no_std]` 甚至「无 `alloc`」的环境下，只用 `StaticRb` + `split_ref` 完成 push/pop，并解释为什么去掉 `alloc` 后 `HeapRb` 就消失了。

本讲属于专家层，但内容并不晦涩——它讲的是「同一个数据结构如何用 Cargo 的 feature 机制，在同一份源码里同时适应标准平台、嵌入式 no_std、乃至无堆裸机」。

## 2. 前置知识

阅读本讲前，建议你已经建立以下认知（对应前置讲义）：

- **三大 crate 的关系**（u1-l3）：核心 crate `ringbuf` 提供 `SharedRb`，派生 crate 在其上叠加同步原语；workspace 用 `default-features = false` 引用核心，以便夺回 feature 控制权。
- **存储后端抽象**（u2-l2）：`Storage` trait 把「连续的 `MaybeUninit<T>` 内存区」抽象出来，`Array<T,N>`（编译期容量、无需堆）、`Heap<T>`（堆分配、需 `alloc`）等是不同实现；`StaticRb = SharedRb<Array<T,N>>`、`HeapRb = SharedRb<Heap<T>>`。
- **`SharedRb` 的无锁实现**（u5-l1）：它用 `CachePadded<AtomicUsize>` 存读写索引、`AtomicBool` 存 hold 标志，靠 Acquire/Release 内存顺序建立跨线程可见性。

本讲会把上述知识点连起来：feature 决定「能用哪些存储后端」「原子操作来自哪里」「`Arc` 来自哪里」，从而决定 ringbuf 能部署到什么硬件上。

下面补充三个本讲会用到的术语：

- **`#![no_std]`**：Rust crate 级别的属性，表示「不依赖标准库 `std`，只用 `core`（必要时加 `alloc`）」。库 crate 写了它，就能在嵌入式、内核等无操作系统的环境编译。
- **CAS（Compare-And-Swap）**：硬件级原子指令，是无锁数据结构的基石。x86、aarch64 等主流平台都有；但 Cortex-M0（`thumbv6m`）这类极小核没有硬件 CAS，`core::sync::atomic` 的 `swap`/`compare_exchange` 在其上不可用。
- **critical section（临界区）**：在单核 MCU 上，用「关中断」即可制造一段不可打断的代码，从而用软件模拟原子操作。这正是 `portable-atomic` 在无 CAS 目标上的回退方案之一。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`Cargo.toml`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/Cargo.toml) | 定义 6 个 feature、它们的依赖级联、`portable-atomic` 等可选依赖，以及 examples 的 `required-features` 门控。 |
| [`src/lib.rs`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/lib.rs) | crate 根，`#![no_std]` 在此声明，并按 feature 条件引入 `alloc`/`std`。 |
| [`src/rb/shared.rs`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs) | `SharedRb` 实现，包含「原子后端切换」与 `Split`（alloc 门控）/`SplitRef`（无条件）两组拆分。 |
| [`src/alias.rs`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/alias.rs) | 类型别名，包含「`Arc` 后端切换」与 `StaticRb`/`HeapRb` 定义。 |
| [`src/storage.rs`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/storage.rs) | `Storage` 及其后端；`Array`/`Slice` 无需堆，`Heap` 被 `#[cfg(feature = "alloc")]` 门控。 |
| [`src/rb/macros.rs`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/macros.rs) | `rb_impl_init!` 宏生成构造器，其中 `Default`/`From<[T;N]>`（静态）无条件，`new`/`From<Vec>`（堆）被 alloc 门控。 |
| [`examples/static.rs`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/examples/static.rs) | 一个完整的 `#![no_std]` 示例：只用 `StaticRb` + `split_ref`。 |
| [`examples/global_static.rs`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/examples/global_static.rs) | `#![no_std]` 下用 `OnceMut` 把 `StaticRb` 放进全局静态变量。 |
| [`scripts/test.sh`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/scripts/test.sh) | CI 脚本，固化了各种 feature 组合的校验命令，是本讲实践的「权威依据」。 |

## 4. 核心概念与源码讲解

### 4.1 feature 体系全景：能力级联与依赖关系

#### 4.1.1 概念说明

ringbuf 是一个「同一份源码、多种部署形态」的库：它既能在带操作系统的服务器上用堆分配，也能在 `#![no_std]` 的嵌入式固件里用静态内存，甚至能在没有硬件原子的极小 MCU 上运行。这套「形态切换」完全靠 Cargo 的 **feature flag** 机制实现，没有任何 `#[cfg(target_...)]` 硬编码目标——形态由使用者在 `Cargo.toml` 里按需开启。

ringbuf 一共定义了 6 个 feature，可以分成三类：

- **能力级联类**：`default` → `std` → `alloc`，三者层层递进，开启上层自动开启下层。
- **后端替换类**：`portable-atomic`，把原子操作的来源从「标准库」换成「`portable-atomic` crate」。
- **纯开关类**：`bench`、`test_local`，定义体是空的 `[]`，本身不引入代码，仅作为编译条件被别处 `#[cfg]` 引用。

#### 4.1.2 核心流程

feature 之间的依赖关系如下图（箭头表示「开启我会自动开启你」）：

```
                     default
                       │
                       ▼
            ┌───────── std ─────────┐
            │     (需要 alloc)       │
            │                       │
            ▼                       ▼
         alloc            portable-atomic?/std
     (需要 pa-util           (弱依赖：仅当
      ?/alloc)               portable-atomic
            │                 开启时才透传)
            ▼
   portable-atomic-util?/alloc

  portable-atomic ──► dep:portable-atomic
                └──► dep:portable-atomic-util

  bench       = []   (空开关)
  test_local  = []   (空开关)
```

要点：

1. `default = ["std"]`：默认就是「全功能、带 std」。
2. `std = ["alloc", ...]`：开 `std` 必开 `alloc`——所以标准平台下 `HeapRb` 一定可用。
3. `alloc = ["portable-atomic-util?/alloc"]`：开 `alloc` 会把（若启用的）`portable-atomic-util` 的 `alloc` feature 也打开，让 `portable_atomic_util::Arc` 可用。
4. `portable-atomic?/...` 中的 `?` 是 **弱依赖语法**：只有当 `portable-atomic` 这个可选依赖被启用时，才向它透传 `std` feature；若未启用，这一项被忽略，不会报错。这正是「`std` 想照顾 `portable-atomic`，但不能强制要求它存在」的写法。

#### 4.1.3 源码精读

整个 feature 体系集中在 [`Cargo.toml:27-33`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/Cargo.toml#L27-L33)，这段定义了上文那张依赖图：

```toml
[features]
default = ["std"]
std = ["alloc", "portable-atomic?/std", "portable-atomic-util?/std"]
portable-atomic = ["dep:portable-atomic", "dep:portable-atomic-util"]
alloc = ["portable-atomic-util?/alloc"]
bench = []
test_local = []
```

对应的依赖声明在 [`Cargo.toml:35-38`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/Cargo.toml#L35-L38)：`portable-atomic` 与 `portable-atomic-util` 都是 `optional = true` 且 `default-features = false`，所以只有显式开启 `portable-atomic` feature（即 `dep:portable-atomic`）时它们才会被拉入，且默认不携带任何子 feature：

```toml
portable-atomic = { version = "1", default-features = false, optional = true }
portable-atomic-util = { version = "0.2", default-features = false, optional = true }
```

examples 的门控在 [`Cargo.toml:43-57`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/Cargo.toml#L43-L57)：用 `HeapRb` 的 `simple`/`overwrite` 需要 `alloc`，用线程的 `message`/`test_ordering` 需要 `std`，而两个 `#![no_std]` 静态示例 `static`/`global_static` 没有任何 `required-features`——它们故意不带门控，正是为了证明「无堆、无 std 也能编译」。当读者用 `--no-default-features` 构建时，前 4 个示例会被跳过，只剩这两个静态示例，直观印证了 feature 的能力裁剪。

> 提示：`default-features = false` 在「workspace 自己引用核心 crate」时也出现于 [`Cargo.toml:9-10`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/Cargo.toml#L9-L10)，这一点在 u1-l3 已详述，这里不再展开。

#### 4.1.4 代码实践

1. **实践目标**：亲手验证「能力级联」与 `--no-default-features` 的裁剪效果。
2. **操作步骤**：
   - 打开 [`Cargo.toml:27-33`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/Cargo.toml#L27-L33)，对照本讲的依赖图，逐行标注每个 feature 依赖了谁。
   - 执行 `cargo check --no-default-features --features alloc`（这条命令正是 [`scripts/test.sh:7`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/scripts/test.sh#L7)）。
   - 再执行 `cargo check --no-default-features`（对应 [`scripts/test.sh:8`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/scripts/test.sh#L8)）。
3. **需要观察的现象**：两条命令都应编译通过；前者会拉入 `alloc`（`HeapRb` 可用），后者连 `alloc` 都没有（只剩 `StaticRb`）。
4. **预期结果**：编译成功。若你顺便用 `cargo check --no-default-features --examples` 查看 examples，会发现 `simple`/`overwrite`/`message`/`test_ordering` 因门控缺失而被跳过，只剩 `static`/`global_static`。
5. 本步骤仅做「编译校验」，不涉及运行；运行结果无关紧要。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 `std` 要写成 `["alloc", "portable-atomic?/std", ...]` 而不能写成 `["alloc", "portable-atomic/std"]`？
  - **答案**：`portable-atomic` 是 `optional` 依赖，可能未被启用。去掉 `?` 会变成「强依赖」，要求 `portable-atomic` 必须存在，于是在未开启 `portable-atomic` feature 时编译失败。`?` 表示「弱依赖」——仅当该可选依赖被启用时才透传 `std`，否则忽略该项。
- **练习 2**：`bench` 和 `test_local` 的定义体是 `[]`，它们有什么用？
  - **答案**：它们是「纯开关」，本身不引入任何依赖或代码，但可以被源码里的 `#[cfg(feature = "...")]` 引用。例如 `bench` 在 [`src/lib.rs:151`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/lib.rs#L151) 用于开启 nightly 的 `test` feature 并编译 benchmarks 模块；`test_local` 用于切换测试目标为 `LocalRb`（见 u8-l5）。

### 4.2 portable-atomic：让 ringbuf 跑在没有硬件 CAS 的芯片上

#### 4.2.1 概念说明

`SharedRb` 的「无锁」承诺（u5-l1）依赖原子操作：读写索引用 `AtomicUsize` 的 `load`/`store`，hold 标志用 `AtomicBool` 的 `swap`。在 x86、aarch64 等主流平台上，这些原子类型直接来自 `core::sync::atomic`，背后是硬件指令。

但在 `thumbv6m-none-eabi`（Cortex-M0）这类极小核上，**硬件没有 CAS 指令**。`core::sync::atomic` 的 `AtomicUsize::swap`（属于读改写操作）在其上不可用——直接用会导致编译或链接失败。于是 ringbuf 引入 [`portable-atomic`](https://crates.io/crates/portable-atomic) 这个 crate：它在不支持硬件原子的目标上，用「关中断（critical section）」「spin lock」等回退方案软件模拟原子操作，让同一份 `swap`/`load`/`store` 调用代码在小芯片上也能工作。

关键点：这一切对调用方**透明**——`portable_atomic::AtomicUsize` 与 `core::sync::atomic::AtomicUsize` 提供几乎一样的 API，ringbuf 只需在源码里用 `cfg` 把「类型来源」切换一下，业务逻辑（`Acquire`/`Release`/`AcqRel`、`swap`）一个字都不用改。

#### 4.2.2 核心流程

`SharedRb` 的原子后端切换发生在文件顶部的 `use` 声明处，逻辑极简：

```
是否启用 portable-atomic feature？
        │
   ┌────┴────┐
   ▼         ▼
  否         是
   │         │
   ▼         ▼
core::sync  portable_atomic
::atomic    ::{AtomicBool,
::{AtomicBool,  AtomicUsize,
 AtomicUsize,   Ordering}
 Ordering}
   │         │
   └────┬────┘
        ▼
  struct SharedRb 的字段类型
  AtomicUsize / AtomicBool
  保持不变，业务代码不变
```

切换之后，`SharedRb` 结构体的字段定义（`read_index: CachePadded<AtomicUsize>` 等）完全不变，所有 `load`/`store`/`swap` 调用也不变。这就是「后端替换」的妙处：换的是类型的「出处」，不是用法。

至于 `portable-atomic` 在小芯片上具体用什么回退策略，则由**它的子 feature** 控制，最常见的是 `critical-section`（关中断）。由于 ringbuf 在 [`Cargo.toml:37`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/Cargo.toml#L37) 把 `portable-atomic` 设为 `default-features = false`，使用者必须自己显式透传 `portable-atomic/critical-section`，这正是 [`scripts/test.sh:9`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/scripts/test.sh#L9) 里 `--features ...,portable-atomic/critical-section` 的来历。

#### 4.2.3 源码精读

原子后端切换的全部代码在 [`src/rb/shared.rs:20-23`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L20-L23)，两个互斥的 `cfg` 各导入一组原子类型：

```rust
#[cfg(not(feature = "portable-atomic"))]
use core::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
#[cfg(feature = "portable-atomic")]
use portable_atomic::{AtomicBool, AtomicUsize, Ordering};
```

紧接着，[`src/rb/shared.rs:51-57`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L51-L57) 定义了结构体——四个原子字段，类型名 `AtomicUsize`/`AtomicBool` 在两种后端下都成立：

```rust
pub struct SharedRb<S: Storage + ?Sized> {
    read_index: CachePadded<AtomicUsize>,
    write_index: CachePadded<AtomicUsize>,
    read_held: AtomicBool,
    write_held: AtomicBool,
    storage: S,
}
```

业务逻辑（位于同文件下游）完全不受后端影响，例如索引的 `load(Ordering::Acquire)`、`store(Ordering::Release)`、hold 标志的 `swap(flag, Ordering::AcqRel)`，无论原子类型来自 `core` 还是 `portable_atomic`，调用方式一模一样——这印证了「换出处、不改用法」。

在 CI 一侧，[`scripts/test.sh:9`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/scripts/test.sh#L9) 给出了在 `thumbv6m-none-eabi` 目标上的完整校验命令，它把 `alloc`、`portable-atomic`、以及向依赖透传的 `portable-atomic/critical-section` 一起开启：

```sh
cargo check --target thumbv6m-none-eabi \
  --no-default-features \
  --features alloc,portable-atomic,portable-atomic/critical-section
```

这一行同时演示了本讲三个机制：`--no-default-features` 裁掉 std、`portable-atomic` 切换原子后端、`portable-atomic/critical-section` 透传子 feature 选定回退策略。

> 补充：`LocalRb`（单线程版）根本不用原子，它用 `Cell<usize>`/`Cell<bool>`（见 [`src/rb/local.rs:22-25`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/local.rs#L22-L25)），因此 `portable-atomic` 对它毫无影响——本模块讨论的「原子后端」只关系到多线程的 `SharedRb`。

#### 4.2.4 代码实践

1. **实践目标**：理解 `portable-atomic` 是「按需启用」的可选后端，并看清它如何被 CI 验证。
2. **操作步骤**：
   - 先执行 `cargo check --features portable-atomic`（默认带 std）。这条命令在 [`scripts/test.sh:6`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/scripts/test.sh#L6) 也出现过。
   - 对比 [`src/rb/shared.rs:20-23`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L20-L23)，确认此时 `AtomicUsize` 来自 `portable_atomic`。
3. **需要观察的现象**：编译通过；若你愿意，可在 `src/rb/shared.rs` 临时加一行 `println!("{:?}", core::any::type_name::<AtomicUsize>())` 风格的调试（示例代码，非项目原有），观察类型名前缀变化。
4. **预期结果**：启用 `portable-atomic` 时类型形如 `portable_atomic::AtomicUsize`，未启用时形如 `core::sync::atomic::AtomicUsize`。运行结果**待本地验证**。
5. 若手头没有 `thumbv6m` 工具链，跳过嵌入式目标的实际交叉编译，仅做源码理解即可。

#### 4.2.5 小练习与答案

- **练习 1**：为什么 `SharedRb` 要用 `swap`（读改写）来管理 hold 标志，而不是 `store`？
  - **答案**：hold 标志采用 test-and-set 语义——需要「读取旧值并同时设为新值」这一原子复合操作，才能在 [`src/rb/shared.rs:139-145`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L139-L145) 里实现「置位并检测是否已被占用」。`store` 只写不读，无法返回旧值。而 `swap` 正是 CAS 类操作，在无 CAS 的目标上必须有 `portable-atomic` 才能用。
- **练习 2**：`portable-atomic/critical-section` 中的斜杠表示什么？
  - **答案**：这是 Cargo 的「向依赖 crate 透传 feature」语法——斜杠左边是依赖名（`portable-atomic`），右边是该依赖的一个 feature（`critical-section`）。它开启的是 **`portable-atomic` 这个外部 crate** 的子功能（选定「关中断」回退策略），而不是 ringbuf 自己的 feature。

### 4.3 portable_atomic_util::Arc：portable-atomic 下的引用计数切换

#### 4.3.1 概念说明

多线程共享 `SharedRb` 靠 `Arc`：`split()` 把缓冲区包进 `Arc`，两端各持一份克隆（u2-l4）。标准平台的 `Arc` 来自 `alloc::sync::Arc`，它内部的引用计数用的是 `core::sync::atomic` 的原子。

问题来了：在 `thumbv6m` 这类无 CAS 的目标上，`alloc::sync::Arc` 的引用计数（依赖原子 `fetch_add`/`compare_exchange`）同样不可用。为了让 `split()`（基于 `Arc`）也能在小芯片上工作，ringbuf 在启用 `portable-atomic` 时，把 `Arc` 换成 [`portable-atomic-util`](https://crates.io/crates/portable-atomic-util) 提供的 `portable_atomic_util::Arc`——它的 API 与 `alloc::sync::Arc` 一致，但内部引用计数用的是 `portable-atomic` 的原子，因此在无 CAS 目标上也能跑。

注意前提：`Arc` 本身需要堆分配，所以这处切换**只在 `alloc` 启用时**才有意义；纯静态（无 alloc）模式下根本用不到 `Arc`，改用 `split_ref` 借用拆分（见 4.4）。

#### 4.3.2 核心流程

`Arc` 的后端切换发生在 `alias.rs` 顶部，由两个条件的组合决定：

```
alloc 是否启用？        portable-atomic 是否启用？
      │                         │
 ┌────┴────┐              ┌─────┴─────┐
 ▼         ▼              ▼           ▼
是         否             是          否
 │         │              │           │
 └─────────┴──────────────┴───────────┘
                  │
   只有「alloc 且 portable-atomic」时
   才用 portable_atomic_util::Arc；
   「alloc 且非 portable-atomic」时
   用 alloc::sync::Arc；
   没有 alloc 时，根本没有 Arc。
```

切换后，下游所有用到 `Arc` 的别名（`HeapProd`/`HeapCons`）与 `SharedRb::split` 实现都自动指向新的 `Arc` 类型，业务代码无需改动——又是「换出处、不改用法」。

#### 4.3.3 源码精读

`Arc` 后端切换的全部代码在 [`src/alias.rs:9-12`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/alias.rs#L9-L12)，两个互斥 `cfg` 各导出一个 `Arc`：

```rust
#[cfg(all(feature = "alloc", not(feature = "portable-atomic")))]
pub use alloc::sync::Arc;
#[cfg(all(feature = "alloc", feature = "portable-atomic"))]
pub use portable_atomic_util::Arc;
```

随后 [`src/rb/shared.rs:24-25`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L24-L25) 在 alloc 下导入这个（已被切换好的）`Arc`：

```rust
#[cfg(feature = "alloc")]
use {crate::alias::Arc, alloc::boxed::Box};
```

`split()` 的实现 [`src/rb/shared.rs:154-162`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L154-L162) 就直接用这个 `Arc`——无论它底层是 `alloc::sync::Arc` 还是 `portable_atomic_util::Arc`，代码完全一致：

```rust
#[cfg(feature = "alloc")]
impl<S: Storage> Split for SharedRb<S> {
    type Prod = CachingProd<Arc<Self>>;
    type Cons = CachingCons<Arc<Self>>;
    fn split(self) -> (Self::Prod, Self::Cons) {
        Arc::new(self).split()
    }
}
```

而别名 `HeapProd`/`HeapCons`（[`src/alias.rs:29-35`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/alias.rs#L29-L35)）也用的是同一个被切换的 `Arc`，所以在 `portable-atomic + alloc` 下，`HeapRb::split()` 拿到的两端背后是 `portable_atomic_util::Arc`，能在 `thumbv6m` 上安全共享。

#### 4.3.4 代码实践

1. **实践目标**：看清 `Arc` 来源随 feature 切换。
2. **操作步骤**：阅读 [`src/alias.rs:9-12`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/alias.rs#L9-L12)，再追踪 `Arc` 被 [`src/rb/shared.rs:24-25`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L24-L25) 导入、又在 [`src/rb/shared.rs:154-162`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L154-L162) 的 `split()` 中使用这一条链路。
3. **需要观察的现象**：在「alloc + portable-atomic」组合下，`split()` 内的 `Arc::new` 解析为 `portable_atomic_util::Arc::new`。
4. **预期结果**：源码阅读型实践，无需运行；结论是同一份 `split()` 代码天然适配两种 `Arc`。
5. 运行层面**待本地验证**（若想确认，可在 `split()` 加调试打印观察 `type_name::<Arc<Self>>()`）。

#### 4.3.5 小练习与答案

- **练习 1**：为什么 `Arc` 的切换 `cfg` 要同时包含 `feature = "alloc"`，而 `AtomicUsize` 的切换不需要？
  - **答案**：`Arc` 是堆上的引用计数指针，必须依赖 `alloc`；没有 `alloc` 时根本不存在 `Arc`，`split()` 也整个被门控掉。而 `AtomicUsize` 是定长的栈上原子类型，属于 `core`/`portable-atomic`，不需要堆，所以它的切换只看 `portable-atomic` 一个条件。
- **练习 2**：在「`portable-atomic` 开、`alloc` 不开」的组合下，`Arc` 来自哪里？
  - **答案**：哪里都不来——两个 `cfg` 都要求 `alloc`，故该组合下没有 `Arc` 被导出，`HeapRb` 与 `split()` 都不可用，使用者只能用 `StaticRb` + `split_ref`。也就是说 `portable-atomic` 的原子能力在无 alloc 时仍可用于 `StaticRb` 的索引，但 `Arc` 共享能力需要 alloc 才有。

### 4.4 no_std 与 no-alloc：纯静态内存下的 StaticRb

#### 4.4.1 概念说明

把 feature 一路裁到底——`--no-default-features`——意味着既没有 `std` 也没有 `alloc`。此时 ringbuf 还能用吗？能，但只能用「不需要堆、不需要 `Arc`」的子集：

- **存储后端**：`Heap` 被 `#[cfg(feature = "alloc")]` 门控而消失，只剩 `Array<T,N>`（编译期定长数组，放在栈或静态区）和 `Slice`/`Ref`（借用既有内存）。于是只能用 `StaticRb = SharedRb<Array<T,N>>`。
- **拆分方式**：`split()` 需要把缓冲区包进 `Arc`（要 alloc），故不可用；只能用 `split_ref(&mut self)`，它返回借用 `&'a Self` 的两端，零堆分配。
- **构造方式**：`HeapRb::new` 消失，但 `StaticRb::<T,N>::default()`（来自 `rb_impl_init!` 宏的 `Default` 实现）无条件可用，直接给出一个容量为 `N` 的空缓冲区。

这套「纯静态」组合正是 `#![no_std]` 嵌入式场景的标准用法：缓冲区内存完全来自静态数组，运行期零分配。

#### 4.4.2 核心流程

从「有哪些存储后端」到「能怎么拆分」，整条链路由 feature 串联：

```
--no-default-features（无 std、无 alloc）
        │
        ├──► Heap 后端被门控消失  ──► HeapRb 不可用
        │    (src/storage.rs:133, src/alias.rs:26)
        │
        ├──► Array 后端保留        ──► StaticRb = SharedRb<Array<T,N>> 可用
        │    (src/storage.rs:102, src/alias.rs:17)
        │
        ├──► Arc 被门控消失        ──► split() 不可用
        │    (src/alias.rs:9-12)
        │
        └──► SplitRef 无条件保留   ──► split_ref(&mut self) 可用
             (src/rb/shared.rs:181)
                    │
                    ▼
        StaticRb::default() + split_ref()
        ── 零堆分配、#![no_std] 可用的完整 push/pop 链路
```

为什么 `split_ref` 不需要 `Arc`？因为它把两端都建立在「对同一个 `&mut self` 的共享引用」之上，靠 `Storage` 的内部可变性（`UnsafeCell`，见 u2-l2）让两端各自读写，生命周期由借用而非引用计数管理（u2-l4）。

#### 4.4.3 源码精读

`StaticRb` 的定义无条件存在，见 [`src/alias.rs:17`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/alias.rs#L17)：

```rust
pub type StaticRb<T, const N: usize> = SharedRb<Array<T, N>>;
```

它底层的 `Array` 同样无条件，见 [`src/storage.rs:102`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/storage.rs#L102)：

```rust
pub type Array<T, const N: usize> = Owning<[MaybeUninit<T>; N]>;
```

`Owning<T>` 只是一个包着 `UnsafeCell<T>` 的薄壳（[`src/storage.rs:90-100`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/storage.rs#L90-L100)），不涉及任何堆。对比之下，`Heap` 整段被门控，见 [`src/storage.rs:133-153`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/storage.rs#L133-L153) 起首的 `#[cfg(feature = "alloc")]`。

构造器方面，`rb_impl_init!` 宏为 `Array` 后端无条件生成 `Default`/`From<[T;N]>`，见 [`src/rb/macros.rs:3-14`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/macros.rs#L3-L14)：

```rust
impl<T, const N: usize> Default for $type<crate::storage::Array<T, N>> {
    fn default() -> Self {
        unsafe { Self::from_raw_parts(crate::utils::uninit_array().into(), usize::default(), usize::default()) }
    }
}
```

而堆构造器 `new`/`From<Vec>` 则被 alloc 门控（[`src/rb/macros.rs:16-49`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/macros.rs#L16-L49)），无 alloc 时不存在。

拆分方面，`SplitRef` 无条件实现，见 [`src/rb/shared.rs:181-194`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L181-L194)，它用借用而非 `Arc`：

```rust
impl<S: Storage + ?Sized> SplitRef for SharedRb<S> {
    type RefProd<'a> = CachingProd<&'a Self> where Self: 'a;
    type RefCons<'a> = CachingCons<&'a Self> where Self: 'a;
    fn split_ref(&mut self) -> (Self::RefProd<'_>, Self::RefCons<'_>) {
        (CachingProd::new(self), CachingCons::new(self))
    }
}
```

而基于 `Arc` 的 `Split` 被门控，见 [`src/rb/shared.rs:154`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L154) 的 `#[cfg(feature = "alloc")]`。

这一切的最佳佐证是项目自带的两个示例。[`examples/static.rs`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/examples/static.rs) 是一个完整、自包含的 `#![no_std]` 程序，全文件仅用 `StaticRb` + `split_ref`：

```rust
#![no_std]
use ringbuf::{traits::*, StaticRb};

fn main() {
    const RB_SIZE: usize = 1;
    let mut rb = StaticRb::<i32, RB_SIZE>::default();
    let (mut prod, mut cons) = rb.split_ref();

    assert_eq!(prod.try_push(123), Ok(()));
    assert_eq!(prod.try_push(321), Err(321));
    assert_eq!(cons.try_pop(), Some(123));
    assert_eq!(cons.try_pop(), None);
}
```

更贴近真实嵌入式的 [`examples/global_static.rs`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/examples/global_static.rs) 把 `StaticRb` 放进全局静态变量（用 dev-dependency `lock-free-static` 的 `OnceMut` 做一次性初始化），同样 `#![no_std]`、同样 `split_ref`：

```rust
#![no_std]
use lock_free_static::OnceMut;
use ringbuf::{traits::*, StaticRb};

static RB: OnceMut<StaticRb<i32, 1>> = OnceMut::new();

fn main() {
    RB.set(StaticRb::default()).ok().expect("RB already initialized");
    let (mut prod, mut cons) = RB.get_mut().expect("...").split_ref();
    // ... try_push / try_pop 同上
}
```

这两个示例没有任何 `required-features` 门控（见 [`Cargo.toml:43-57`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/Cargo.toml#L43-L57) 里没有它们的条目），正是为了证明「无堆、无 std 也能编译」。

#### 4.4.4 代码实践

1. **实践目标**：亲手在 `--no-default-features` 下编译一个 `#![no_std]` 程序，确认 `StaticRb` + `split_ref` 链路可用。
2. **操作步骤**：
   - 直接校验项目自带示例能通过编译（这等同于项目 CI 的做法）：`cargo check --example static --no-default-features`。
   - 对照 [`examples/static.rs:1-15`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/examples/static.rs#L1-L15)，确认它没有用到任何需要 `alloc`/`std` 的 API。
3. **需要观察的现象**：编译通过，不触发任何 `alloc`/`std` 相关符号。
4. **预期结果**：`cargo check` 成功。
5. 关于「能否 `cargo run` 直接运行」：一个纯粹的 `#![no_std]` 二进制要真正链接运行，通常还需提供 `#[panic_handler]` 与运行时入口；本项目用 `cargo check` 来验证 no_std 可编译性（与 [`scripts/test.sh:8`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/scripts/test.sh#L8) 一致），实际在裸机上的运行行为**待本地验证**。

#### 4.4.5 小练习与答案

- **练习 1**：去掉 `alloc` 后，`HeapRb` 为什么不可用？请从「别名定义」和「构造器」两个层面回答。
  - **答案**：层面一，`HeapRb` 别名本身被 `#[cfg(feature = "alloc")]` 门控（[`src/alias.rs:26`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/alias.rs#L26)），无 alloc 时该类型根本不存在。层面二，它的底层存储 `Heap`、构造器 `HeapRb::new`/`From<Vec>` 也都被 alloc 门控（[`src/storage.rs:133`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/storage.rs#L133)、[`src/rb/macros.rs:16`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/macros.rs#L16)），它们依赖 `Vec`/`Box` 等堆类型。两层消失，`HeapRb` 自然不可用。
- **练习 2**：在无 alloc 的 `StaticRb` 上，为什么必须用 `split_ref` 而不能用 `split`？
  - **答案**：`split()` 需要把缓冲区包进 `Arc` 以实现共享所有权，而 `Arc` 必须依赖堆（alloc），其实现 [`src/rb/shared.rs:154-162`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L154-L162) 被 `#[cfg(feature = "alloc")]` 门控。`split_ref` 改用借用 `&'a Self`（[`src/rb/shared.rs:181-194`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L181-L194)），不分配内存，故在无 alloc 下可用。

## 5. 综合实践

**任务**：构造一张「feature 组合 × 可用能力」的对照表，并用编译命令逐一验证。

1. **目标**：把本讲四个模块串起来，亲手确认不同 feature 组合下 ringbuf 的「能用什么」。
2. **操作步骤**：
   - 在仓库根目录依次执行以下命令（它们大多直接取自 [`scripts/test.sh`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/scripts/test.sh)），并记录每条是否通过：

     | 命令 | 对应脚本行 | 预期能力 |
     | --- | --- | --- |
     | `cargo check` | （默认） | `std`+`alloc`，`HeapRb`/`split` 可用，原子来自 `core` |
     | `cargo check --no-default-features --features alloc` | [`test.sh:7`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/scripts/test.sh#L7) | 无 std、有 alloc，`HeapRb`/`split` 可用，原子来自 `core` |
     | `cargo check --no-default-features` | [`test.sh:8`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/scripts/test.sh#L8) | 无 std、无 alloc，仅 `StaticRb`/`split_ref`，原子来自 `core` |
     | `cargo check --features portable-atomic` | [`test.sh:6`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/scripts/test.sh#L6) | 原子与 `Arc` 切到 `portable_atomic`/`portable_atomic_util` |

   - 接着做一个「负面验证」：在一个临时的小 crate 里写 `use ringbuf::HeapRb;`，然后用 `cargo check --no-default-features` 检查它。预期编译失败，错误信息形如「`HeapRb` not found / 未导出」（因为 `HeapRb` 被 alloc 门控）。**待本地验证**确切的报错文本。
   - 最后回答两个问题并写入你的笔记：
     - 为什么「无 alloc」时 `HeapRb` 不可用？（参考 4.4.5 练习 1）
     - `portable-atomic` 与 `alloc` 是不是必须同时开？（参考 4.3.5 练习 2：不是；`portable-atomic` 的原子能力可独立于 alloc 用于 `StaticRb`。）
3. **需要观察的现象**：前 4 条 `cargo check` 全部通过；负面验证那条报 `HeapRb` 不可用。
4. **预期结果**：得到一张经验证的特征矩阵，能清楚说明「哪个 feature 组合解锁哪些类型与方法」。
5. 若无 `thumbv6m` 工具链，跳过 [`test.sh:9`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/scripts/test.sh#L9) 的嵌入式交叉编译；嵌入式实际运行行为**待本地验证**。

## 6. 本讲小结

- ringbuf 用 6 个 feature 实现形态切换：`default→std→alloc` 是能力级联，`portable-atomic` 是后端替换，`bench`/`test_local` 是空开关（[`Cargo.toml:27-33`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/Cargo.toml#L27-L33)）。
- `std = [..., "portable-atomic?/std"]` 中的 `?` 是弱依赖语法，只在 `portable-atomic` 启用时才透传子 feature，避免强制依赖。
- `portable-atomic` 让 `SharedRb` 能跑在 `thumbv6m` 这类无硬件 CAS 的芯片上：靠 [`src/rb/shared.rs:20-23`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L20-L23) 把 `AtomicUsize`/`AtomicBool` 的来源在 `core::sync::atomic` 与 `portable_atomic` 间切换，业务代码不变。
- `portable-atomic/critical-section` 是「向依赖透传 feature」语法，为 `portable-atomic` 选定「关中断」回退策略（[`scripts/test.sh:9`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/scripts/test.sh#L9)）。
- 启用 `portable-atomic + alloc` 时，`Arc` 从 `alloc::sync::Arc` 切到 `portable_atomic_util::Arc`（[`src/alias.rs:9-12`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/alias.rs#L9-L12)），使基于 `Arc` 的 `split()` 也能在小芯片上工作。
- 纯静态模式（`--no-default-features`）下，`Heap`/`HeapRb`/`split()` 因 alloc 门控消失，只能用 `StaticRb = SharedRb<Array<T,N>>` + `split_ref`，实现零堆分配的 `#![no_std]` push/pop（[`examples/static.rs`](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/examples/static.rs)）。

## 7. 下一步学习建议

- 想深入「自定义存储后端」如何在 no_std 下接入外部静态内存/DMA 缓冲，请继续学习 **u8-l2 自定义 Storage 与 from_raw_parts**，它会用到本讲的 `Storage` trait 与 `from_raw_parts` 构造器。
- 想了解构造器与 io/fmt trait 如何被宏批量生成（本讲提到的 `rb_impl_init!` 的另一半用途），请学习 **u8-l3 宏系统**。
- 若你想把 no_std 能力扩展到派生 crate，可阅读 `async/Cargo.toml`、`blocking/Cargo.toml` 中对核心 crate 的 feature 透传（u1-l3 已铺垫），并对照它们的 `scripts/test.sh` 中同样的 `--no-default-features` 与 `thumbv6m` 校验行，体会「核心 feature 体系被派生 crate 复用」的全貌。
