# 源码地图与构建配置

## 1. 本讲目标

学完本讲，你应该能够：

- 说出 `crossbeam-deque` crate 的目录结构，知道每个文件大致负责什么。
- 读懂 `src/lib.rs` 顶部的 crate 级文档、`#![no_std]` 声明，以及它如何通过 `mod` + `pub use` 把核心类型 `Worker / Stealer / Injector / Steal` 暴露出去。
- 理解 `std` feature 默认开启、且目前「禁用 `std` 暂不被支持」这一现状在源码里的具体表现。
- 看懂 `lib.rs` 文档里那段 `find_task` 示例在讲什么、它和真实调度器的关系。
- 理解 `build.rs` 如何探测 ThreadSanitizer、并通过 `cargo:rustc-cfg` 注入 `crossbeam_sanitize_thread` 这个编译开关。
- 明白 `src/alloc_helper.rs` 为什么是一个指向 `crossbeam-utils` 的符号链接。

本讲只做「建立地图」，不深入无锁算法和内存序——那是后面进阶讲义（u2/u4）的内容。

## 2. 前置知识

- **crate 与模块系统**：Rust 用 `mod` 声明子模块，用 `pub use` 把内部项重新导出（re-export）到 crate 根。本讲的 `lib.rs` 几乎只做这件事。
- **Cargo feature**：在 `Cargo.toml` 的 `[features]` 表里定义的「可选功能开关」。代码里用 `#[cfg(feature = "std")]` 判断某个 feature 是否开启。`default = ["std"]` 表示默认带上 `std`。
- **no_std**：`#![no_std]` 告诉编译器「不要自动链接 `std` 标准库」，只依赖更底层的 `core`（必要时再用 `alloc`）。这对嵌入式和内核开发很重要，但对本 crate 而言，因为实现重度依赖堆分配和线程局部存储，目前还做不到真正脱离 `std`。
- **build script（build.rs）**：Cargo 在编译 crate 前会先编译并运行根目录的 `build.rs`，它可以读环境变量、向 Cargo 回吐指令，最常见的就是 `cargo:rustc-cfg=...`，从而在主 crate 里用 `#[cfg(...)]` 做条件编译。
- **符号链接（symlink）**：一个「指向另一个文件」的特殊文件。本 crate 用它复用兄弟 crate `crossbeam-utils` 里的同一份代码，避免重复维护。

如果你对 work-stealing、Worker/Stealer/Injector 这些名词还陌生，请先看上一讲（u1-l1）。本讲会把上一讲口头描述的「三种队列角色」落到具体源码位置上。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲是否精读 |
| --- | --- | --- |
| `src/lib.rs` | crate 根：crate 级文档、`#![no_std]`、模块声明、`pub use` 导出 | 是 |
| `build.rs` | 构建脚本：探测 ThreadSanitizer 并注入 cfg | 是 |
| `Cargo.toml` | 包元数据、`std` feature、依赖声明 | 是 |
| `src/alloc_helper.rs` | 符号链接，指向 `crossbeam-utils/src/alloc_helper.rs` | 是 |
| `src/deque.rs` | 全部核心实现（约 2200 行），后续进阶讲义的主角 | 仅提及 |
| `tests/` | 集成测试（`fifo.rs` / `lifo.rs` / `injector.rs` / `steal.rs`） | 仅提及 |
| `README.md` / `CHANGELOG.md` / `LICENSE-*` | 文档与许可证 | 上一讲已覆盖 |

记住这张表：本讲只盯着 **4 个文件**（`lib.rs`、`build.rs`、`Cargo.toml`、`alloc_helper.rs`），它们决定了「crate 长什么样、怎么编译」；真正的算法都在 `deque.rs` 里，留给后面。

## 4. 核心概念与源码讲解

### 4.1 lib.rs：模块声明与 pub use 导出

#### 4.1.1 概念说明

一个 Rust crate 的「门面」是它的 crate 根（库 crate 通常是 `src/lib.rs`）。对外部使用者来说，他们能看到的只有 crate 根 `pub` 出来的东西。`crossbeam-deque` 的策略很典型：把全部实现藏在私有模块 `deque` 里，再在 `lib.rs` 用 `pub use` 把需要的类型「提升」到 crate 根。这样使用者写 `use crossbeam_deque::Worker;` 就够了，而不必写 `crossbeam_deque::deque::Worker`。

#### 4.1.2 核心流程

`lib.rs` 的导出流程可以拆成三步：

1. 写 crate 级文档注释（`//!`），介绍三种队列角色和 `find_task` 示例——这是会出现在 `cargo doc` 首页的内容。
2. 用 `mod deque;` 声明子模块（私有，名字前没有 `pub`）。
3. 用 `pub use crate::deque::{Injector, Steal, Stealer, Worker};` 把 4 个公共类型重新导出到 crate 根。

#### 4.1.3 源码精读

crate 根文件开头是一大段文档注释，从第 1 行一直到 `find_task` 示例结束，紧接着才是真正的属性与模块声明：

[src/lib.rs:85-109](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/lib.rs#L85-L109) —— `#![no_std]` 及之后的模块声明与 `pub use` 导出整段。说明：第 85 行 `#![no_std]` 把 crate 默认设为不链接 `std`；第 98–101 行在开启 `std` feature 时显式 `extern crate alloc;` 和 `extern crate std;`，把分配器和标准库「请回来」；第 106–107 行声明私有子模块 `deque`；第 108–109 行用 `pub use` 把 `Injector / Steal / Stealer / Worker` 四个类型暴露到 crate 根。

注意 [src/lib.rs:106-107](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/lib.rs#L106-L107) 的 `mod deque;` 没有 `pub`，意味着 `crossbeam_deque::deque::...` 这条路径对使用者是私有的；外部只能通过 [src/lib.rs:108-109](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/lib.rs#L108-L109) 的 `pub use` 拿到那 4 个类型。这就是「实现细节藏在私有模块，只暴露精选 API」的典型写法。

#### 4.1.4 代码实践

**目标**：直观看到 `pub use` 是如何决定外部可见 API 的。

**步骤**：

1. 打开 [src/lib.rs:108-109](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/lib.rs#L108-L109)，确认第 109 行只导出了 `Injector, Steal, Stealer, Worker` 四个名字。
2. 在自己的一个二进制项目里（依赖 `crossbeam-deque = "0.8"`），写一段「示例代码」验证可见性：

```rust
// 示例代码（不在本仓库内）
use crossbeam_deque::Worker;          // OK：被 pub use 导出
// use crossbeam_deque::deque::Worker; // 编译失败：deque 是私有模块
fn main() {
    let w = Worker::new_fifo();
    w.push(1);
    println!("{:?}", w.pop());
}
```

3. 编译运行，观察第二行被注释掉的原因。

**需要观察的现象**：直接 `use crossbeam_deque::Worker` 能用；而写成 `crossbeam_deque::deque::Worker` 会被编译器拒绝（提示 `deque` 是私有模块）。

**预期结果**：取消注释第二行会报形如 `error[E0603]: module deque is private` 的错误；只保留第一行则正常编译运行，打印 `Some(1)`。

**如果无法确定运行结果**：编译器对私有模块访问的确切报错文案随版本略有差异，可标记为「待本地验证」，但「访问被拒」这一行为是确定的。

#### 4.1.5 小练习与答案

**练习 1**：如果把第 109 行改成 `pub use crate::deque::*;`（导出模块里所有公开项），会带来什么好处和坏处？

**答案**：好处是将来在 `deque` 模块新增的公共项会自动出现在 crate 根，省去每次手动添加。坏处是导出范围变「宽」且不可控——内部任何 `pub` 项都会泄漏成公共 API，破坏封装，也可能在重构时意外改变对外 API 表面。`crossbeam-deque` 选择显式列举 4 个类型，正是为了把公共 API 钉死。

**练习 2**：`mod deque;` 为什么不能省略，只留 `pub use`？

**答案**：`pub use crate::deque::{...}` 引用的是 `crate::deque` 这个模块；必须先用 `mod deque;` 把 `src/deque.rs` 真正「挂载」进模块树，编译器才知道 `crate::deque` 在哪。省略 `mod` 会导致 `pub use` 找不到目标模块而报错。

### 4.2 #![no_std] 与 std feature 的关系

#### 4.2.1 概念说明

第 85 行的 `#![no_std]` 容易让人误以为本 crate 可以在裸机/内核里用。但事实并非如此：`crossbeam-deque` 重度依赖堆分配（环形缓冲区、Injector 的 block 链表）和 `crossbeam-epoch`（依赖线程局部存储），所以它**实际上必须用 `std`**。源码用一种「先声明 no_std，再用 feature 把 std 加回来」的写法，来表达「我们朝 no_std 方向预留了入口，但暂时不支持」的中间状态。

#### 4.2.2 核心流程

`no_std` 与 `std` feature 的协作流程：

1. crate 级 `#![no_std]` 默认不让 `std` 自动可用。
2. `Cargo.toml` 里 `std` feature 默认开启（`default = ["std"]`）。
3. 当 `std` 开启时，`lib.rs` 用 `extern crate alloc;` 和 `extern crate std;` 显式把分配器和标准库引入作用域。
4. **所有**真正的模块声明（`mod alloc_helper`、`mod deque`）和 `pub use` 都用 `#[cfg(feature = "std")]` 包起来。
5. 一旦关掉 `std` feature，第 3、4 步全部被条件编译剔除，crate 退化成一个「空壳」——没有公共 API。

#### 4.2.3 源码精读

先看 `Cargo.toml` 里 feature 的定义：

[Cargo.toml:26-34](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/Cargo.toml#L26-L34) —— feature 表。说明：第 27 行 `default = ["std"]` 让 `std` 默认开启；第 33 行 `std = ["crossbeam-epoch/std", "crossbeam-utils/std"]` 表明本 crate 的 `std` 会顺带把两个依赖的 `std` 也打开（形成 feature 传递）；第 32 行的注释 `NOTE: Disabling std feature is not supported yet.` 是关键——官方明说「禁用 std 暂不支持」。

再看 `lib.rs` 里如何用 cfg 守卫：

[src/lib.rs:98-109](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/lib.rs#L98-L109) —— std feature 守卫。说明：第 98–101 行在 `std` 下 `extern crate alloc/std`；第 103–104 行的 `mod alloc_helper;`、第 106–107 行的 `mod deque;`、第 108–109 行的 `pub use`，**每一处都带 `#[cfg(feature = "std")]`**。这意味着关闭 `std` 后，连 `deque` 模块都不会被编译，crate 根就什么都不剩。

#### 4.2.4 代码实践

**目标**：亲手验证「禁用 std 后公共 API 消失」这一现象。

**步骤**：

1. 在本 crate 目录运行：

```bash
cargo build -p crossbeam-deque --no-default-features
```

2. 观察编译是否成功、有无报错。
3. 再生成文档看里面还有没有那 4 个类型：

```bash
cargo doc -p crossbeam-deque --no-default-features --open
```

**需要观察的现象**：`--no-default-features` 关掉了 `std`，于是 `mod deque` 与 `pub use` 都被剔除。crate 本身**仍能编译通过**（它变成了一个空 crate，没有任何公共项），不会直接报错；但生成的文档里再也找不到 `Worker / Stealer / Injector / Steal`。如果你在自己的项目里用 `default-features = false` 引入本 crate 再去用 `Worker`，才会得到类似 `cannot find type Worker in crate root` 的报错。

**预期结果**：`cargo build --no-default-features` 退出码为 0（编译成功），但产物里没有公共 API；`cargo doc` 的页面里看不到 4 个核心类型。

**如果无法确定运行结果**：退出码与具体提示文案「待本地验证」（不同 Cargo 版本表述略有差异），但「API 消失、空壳编译」这一机制由源码里的 cfg 守卫决定，是确定的。

#### 4.2.5 小练习与答案

**练习 1**：既然 `#![no_std]` 在第 85 行，为什么 [src/lib.rs:98-101](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/lib.rs#L98-L101) 还要 `extern crate std;`？

**答案**：`#![no_std]` 的作用是「不自动把 `std` 引入 prelude / 不默认链接 `std`」，但它并不禁止你之后手动 `extern crate std;` 把它请回来。这里第 98–101 行正是在 `std` feature 开启时，显式把 `alloc` 和 `std` 重新引入，从而在「保留 no_std 入口」和「实际需要 std」之间取得折中。

**练习 2**：为什么说「现在禁用 std 没有意义」？

**答案**：因为所有实质内容（`mod deque`、`mod alloc_helper`、全部 `pub use`）都挂在 `#[cfg(feature = "std")]` 下。关掉 std 后 crate 直接变空，没有任何可用功能，所以 [Cargo.toml:32](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/Cargo.toml#L32-L32) 注释明确写「Disabling std feature is not supported yet」。要做到真 no_std，得先把 `deque.rs` 里的堆分配和 epoch（线程局部）依赖都改掉，工作量很大。

### 4.3 lib.rs 顶部的 find_task 文档示例

#### 4.3.1 概念说明

crate 根文件最上面那段 `//!` 注释不只是「说明文字」——它会被 `rustdoc` 渲染成 crate 文档首页，其中夹带的 ` ``` ` 代码块还会被 `cargo test --doc` 当作「文档测试（doctest）」实际编译运行。`crossbeam-deque` 在首页放了一个完整的 `find_task` 函数示例，它浓缩了整个 crate 的典型用法，也提前把上一讲讲过的「work-stealing 调度循环」用代码写出来了。

#### 4.3.2 核心流程

`find_task` 描述的找任务回退链：

1. 先 `local.pop()` 从本地 worker 队列取一个任务；拿到就直接返回。
2. 取不到时，用 `iter::repeat_with(...)` 构造一个「不断尝试」的迭代器：
   - 先 `global.steal_batch_and_pop(local)`：从全局 injector 偷一批任务塞回本地，并顺便弹出一个。
   - 失败/需重试时，`.or_else(...)` 再遍历 `stealers` 列表，挨个 `s.steal()` 偷别的线程的任务。
3. 用 `.find(|s| !s.is_retry())` 一直循环，直到某个 steal 操作「不再是 Retry」（要么成功，要么确定空了）。
4. 最后 `.and_then(|s| s.success())` 把 `Steal::Success(t)` 里的任务 `t` 取出来（空的话返回 `None`）。

#### 4.3.3 源码精读

[src/lib.rs:52-76](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/lib.rs#L52-L76) —— `find_task` 文档示例。说明：这是 crate 首页的 doctest，完整演示了 `local.pop() → global.steal_batch_and_pop → stealers.steal` 的回退链；第 62 行 `local.pop().or_else(...)` 体现「本地优先」；第 64–69 行 `repeat_with` + `or_else` 把 injector 批量偷取和遍历 stealers 串成一条链；第 71 行 `.find(|s| !s.is_retry())` 处理 `Steal::Retry` 这种「假性失败、需重试」的情况；第 73 行 `.and_then(|s| s.success())` 提取最终任务。

[src/lib.rs:86-89](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/lib.rs#L86-L89) —— `#![doc(test(...))]`。说明：`no_crate_inject` 表示 doctest 里不自动注入 `extern crate crossbeam_deque`（因为示例里已经显式 `use crossbeam_deque::{...}`），后面那一串 `attr(allow(...))` 让 doctest 里的临时变量、未使用赋值不会触发警告——毕竟首页示例只演示用法，变量未必都被「正经用掉」。

#### 4.3.4 代码实践

**目标**：把首页 doctest 当成「可运行的真相」来用。

**步骤**：

1. 运行文档测试，确认首页示例真的能编译通过：

```bash
cargo test -p crossbeam-deque --doc
```

2. 生成并打开 crate 文档，在浏览器里看 `find_task` 的渲染效果：

```bash
cargo doc -p crossbeam-deque --open
```

3. 在浏览器首页找到 `find_task`，对照第 4.3.2 节的回退链，逐行标注每个调用对应回退链的哪一步。

**需要观察的现象**：`cargo test --doc` 会显示 doctest 通过；`cargo doc --open` 打开的页面顶部正是这段 `find_task` 代码，且 `Worker/Stealer/Injector/Steal`、`steal_batch_and_pop`、`or_else`、`is_retry`、`success` 都带超链接，可点进各自的 API 文档。

**预期结果**：doctest 通过（`test result: ok`）；文档首页能点到每个类型的详情页。

**如果无法确定运行结果**：doctest 的具体通过数量「待本地验证」，但「能编译通过」这一点由 crate 维护保证（首页示例本就是 crate 的门面，坏了 CI 会拦）。

#### 4.3.5 小练习与答案

**练习 1**：示例里为什么要用 `iter::repeat_with(...)` 而不是普通 `loop`？

**答案**：`repeat_with` 生成一个「无限调用闭包」的迭代器，配合 `.find(...)` 可以优雅地表达「反复尝试，直到满足条件」的循环，并且能让 `.or_else` 这种组合子风格自然衔接。这与普通 `loop` 在语义上等价，但更贴合 `Steal` 类型提供的 `or_else` / `FromIterator` 组合子 API，可读性更好。

**练习 2**：`.find(|s| !s.is_retry())` 找到的是一个什么值？为什么后面还要 `.success()`？

**答案**：`.find(...)` 找到的是「第一个不是 Retry 的 `Steal<T>`」——它要么是 `Steal::Success(t)`（偷到了），要么是 `Steal::Empty`（确实没任务）。`.success()` 把 `Success(t)` 解包成 `Some(t)`，把 `Empty` 映射成 `None`，正好对应函数返回类型 `Option<T>`：偷到就 `Some`，确实空了就 `None`。

### 4.4 build.rs：cargo:rustc-check-cfg 与 crossbeam_sanitize_thread

#### 4.4.1 概念说明

`build.rs` 是 Cargo 在编译主 crate 之前会先编译并运行的小程序。它最常见的用途之一是「探测环境，然后向 Cargo 回吐 `cargo:rustc-cfg=xxx` 指令」，这样主 crate 里就能用 `#[cfg(xxx)]` 做条件编译。本 crate 的 `build.rs` 干的事很聚焦：**探测用户是否在用 ThreadSanitizer（线程数据竞争检测器）跑编译**，如果是，就开启一个叫 `crossbeam_sanitize_thread` 的 cfg 开关。

为什么要这么做？因为无锁代码里那些精心放置的内存栅栏（memory fence），ThreadSanitizer 并不理解——它会把一些「合法的并发读写」误判为数据竞争。为了让 `cargo` 配合 ThreadSanitizer 也能跑（用于发现真正的 bug），源码里准备了一条「tsan 专用」的代码路径，而切换这条路径的总开关就是 `crossbeam_sanitize_thread`。本讲只讲「开关是怎么被点亮的」，开关点亮后代码怎么变，留到后续 u4-l3 讲。

#### 4.4.2 核心流程

`build.rs` 的执行流程：

1. Cargo 编译并运行 `build.rs`，注入一批 `CARGO_CFG_*` 环境变量。
2. 读环境变量 `CARGO_CFG_SANITIZE`（一个逗号分隔的 sanitizer 列表，未设置时为空）。
3. 先 `println!("cargo:rustc-check-cfg=cfg(crossbeam_sanitize_thread)");`，告诉编译器「这个 cfg 是合法的、可能被设置」，避免 `unexpected_cfgs` 警告。
4. 若 `CARGO_CFG_SANITIZE` 字符串包含 `"thread"`，则 `println!("cargo:rustc-cfg=crossbeam_sanitize_thread");`，向 Cargo 注册该 cfg。
5. 之后主 crate（`deque.rs`）里就能用 `#[cfg(crossbeam_sanitize_thread)]` / `#[cfg(not(crossbeam_sanitize_thread))]` 走不同分支。

#### 4.4.3 源码精读

`build.rs` 全文只有十几行，可以直接看：

[build.rs:5-14](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/build.rs#L5-L14) —— `main` 函数。说明：第 6 行 `cargo:rerun-if-changed=build.rs` 告诉 Cargo「只有 build.rs 本身变了才需要重新运行构建脚本」（否则 Cargo 按自己的默认策略决定）；第 7 行 `cargo:rustc-check-cfg=cfg(crossbeam_sanitize_thread)` 提前声明这个自定义 cfg 是合法的，是 Rust 1.80 起 `unexpected_cfgs` lint 的标准做法；第 10 行读 `CARGO_CFG_SANITIZE`，`unwrap_or_default()` 保证未设置时得到空字符串而不是报错；第 11–13 行判断只要里面含 `"thread"`，就回吐 `cargo:rustc-cfg=crossbeam_sanitize_thread`，点亮主 crate 里的开关。

注意 [build.rs:1](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/build.rs#L1-L1) 的注释 `// The rustc-cfg emitted by the build script are *not* public API.`——它强调「构建脚本吐出的这些 cfg 不属于对外承诺的公共 API」，使用者不应在自己的代码里依赖 `crossbeam_sanitize_thread` 这个名字，因为作者随时可能改；而 [build.rs:9](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/build.rs#L9-L9) 的注释则说明 `cfg(sanitize = "..")` 在 rustc 里尚未稳定，所以才走 `CARGO_CFG_SANITIZE` 这个「已稳定的环境变量」间接探测。

#### 4.4.4 代码实践

**目标**：亲眼看到 `crossbeam_sanitize_thread` cfg 是由环境变量驱动的，并能主动观察它。

**步骤**：

1. 普通编译，看默认情况下 `sanitize` 变量的值：

```bash
cargo build -p crossbeam-deque
```

2. 为了在 stable 上间接验证「环境变量 → cfg」这条链路（ThreadSanitizer 完整启用需要 nightly 的 `-Z sanitizer=thread`），可以**临时**在 `build.rs` 第 10 行后加一行 `println!("cargo:warning=sanitize=[{}]", sanitize);` 来打印读到的值。

**需要观察的现象**：普通编译时 `sanitize` 变量为空字符串，`crossbeam_sanitize_thread` 不会被设置，`cargo:warning` 打印 `sanitize=[]`；当编译目标带 `sanitize=thread` 时，`CARGO_CFG_SANITIZE` 会含 `"thread"`，打印变为 `sanitize=[thread]` 并点亮 cfg。

**预期结果**：默认构建不点亮 cfg；带 thread sanitizer 的构建点亮 cfg，使 `deque.rs` 里 `#[cfg(crossbeam_sanitize_thread)]` 分支生效。

**如果无法确定运行结果**：ThreadSanitizer 的完整启用流程涉及 nightly 工具链与 `-Z` flag，具体命令「待本地验证」；本实践的重点是理解「环境变量 → cfg」这条链路，可用改 `build.rs` 加 `cargo:warning` 的方式在 stable 上间接观察。

> ⚠️ 提醒：本讲的任务是「只读分析」，**禁止修改源码交付物**。如果为了观察临时改了 `build.rs`，结束后必须 `git checkout build.rs` 还原，切勿把改动带进仓库。

#### 4.4.5 小练习与答案

**练习 1**：为什么需要 `cargo:rustc-check-cfg` 这一行？去掉会怎样？

**答案**：Rust 1.80 起默认开启 `unexpected_cfgs` lint，任何在代码里用到、但编译器「不知道是否可能被设置」的 cfg 都会报警告。`cargo:rustc-check-cfg=cfg(crossbeam_sanitize_thread)` 就是提前告诉编译器「这个名字是合法的自定义 cfg，由 build script 控制」，从而消除警告。去掉它，普通编译时会出现 `unexpected cfg condition: crossbeam_sanitize_thread` 的警告。

**练习 2**：`CARGO_CFG_SANITIZE` 这个环境变量是谁设置的？

**答案**：是 Cargo 设置的。Cargo 会把影响编译的配置转成一批 `CARGO_CFG_*` 环境变量传给 build script。`cfg(sanitize = "..")` 这个配置项本身在 rustc 里尚未稳定，所以 build.rs 第 9 行特别注释说明，转而通过 `CARGO_CFG_SANITIZE` 这个「已经稳定的环境变量」来间接探测。

### 4.5 src/alloc_helper.rs：复用兄弟 crate 的符号链接

#### 4.5.1 概念说明

在 `src/` 下能看到一个特殊的文件 `alloc_helper.rs`，它不是普通文件，而是一个**符号链接**，指向仓库里兄弟 crate `crossbeam-utils` 的同名文件。这是 crossbeam-rs 这种「多 crate 单仓库（workspace）」常见的复用技巧：几个 crate 都需要同一小段「分配辅助」代码，与其在每个 crate 复制一份（容易漏改），不如用符号链接让它们共享同一份源码。

#### 4.5.2 核心流程

符号链接如何参与编译：

1. Cargo 编译 `crossbeam-deque` 时，看到 `mod alloc_helper;`（见 [src/lib.rs:103-104](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/lib.rs#L103-L104)），按惯例去找 `src/alloc_helper.rs`。
2. 操作系统把对 `src/alloc_helper.rs` 的访问透明地转发到它指向的目标 `../../crossbeam-utils/src/alloc_helper.rs`。
3. 因此编译进 `crossbeam-deque` 的，实质上是 `crossbeam-utils` 里那份 `alloc_helper.rs` 的内容。

#### 4.5.3 源码精读

[src/alloc_helper.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/alloc_helper.rs) —— 符号链接文件。说明：这个文件在仓库里是一个 symlink，目标为 `../../crossbeam-utils/src/alloc_helper.rs`（可由 `ls -l src/alloc_helper.rs` 看到 `->` 标记确认）。它只在 `std` feature 下被挂载（[src/lib.rs:103-104](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/lib.rs#L103-L104) 的 `#[cfg(feature = "std")] mod alloc_helper;`），为 `deque.rs` 里的分配逻辑提供共享的辅助函数。

需要提醒的是：链接目标位于本工作目录（`crossbeam-deque`）之外的安全可读范围，所以本讲**不引用 `alloc_helper.rs` 内部的具体行号**，只说明它在工程结构中的角色与链接关系。其内部代码内容「待确认」（需在能访问整个 crossbeam 仓库的环境下查看），但这不影响本讲「建立源码地图」的目标——你只需知道「它是共享自兄弟 crate 的辅助代码」即可。

#### 4.5.4 代码实践

**目标**：亲手确认 `alloc_helper.rs` 是符号链接，并理解它指向哪里。

**步骤**：

1. 在 `crossbeam-deque` 目录运行：

```bash
ls -l src/alloc_helper.rs
```

2. 观察输出的 `->` 部分，确认它指向 `../../crossbeam-utils/src/alloc_helper.rs`。
3. （可选）在能访问整个 crossbeam 仓库的环境下，打开 `crossbeam-utils/src/alloc_helper.rs` 看真实内容，理解它给 `deque.rs` 提供了什么辅助函数。

**需要观察的现象**：`ls -l` 输出形如 `src/alloc_helper.rs -> ../../crossbeam-utils/src/alloc_helper.rs`，证明它不是独立文件而是链接。

**预期结果**：确认链接关系成立；`deque.rs` 里使用 `alloc_helper` 模块提供的辅助函数时，实际编译的是 `crossbeam-utils` 那份源码。

**如果无法确定运行结果**：链接目标文件的内容「待本地验证」（需要能跨目录读取整个 crossbeam 仓库）。

#### 4.5.5 小练习与答案

**练习 1**：用符号链接共享源码，相比「把文件复制到每个 crate」，主要优缺点是什么？

**答案**：优点是「单一事实来源」——改一处，所有引用它的 crate 同时更新，不会出现各副本不一致。缺点是符号链接对某些版本控制流程、Windows 文件系统、打包/发布（发布到 crates.io 时往往要把链接替换成实际文件）不够友好；另外它让「文件实际内容」分散在不同 crate 目录，初学者容易困惑。本讲正是为了消除这种困惑。

**练习 2**：如果删除这个符号链接，会怎样？

**答案**：[src/lib.rs:103-104](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/lib.rs#L103-L104) 的 `mod alloc_helper;` 会找不到对应的 `src/alloc_helper.rs`，编译器报「file not found for module `alloc_helper`」。因为该 `mod` 在 `#[cfg(feature = "std")]` 下，而默认 `std` 开启，所以默认构建会直接失败。

## 5. 综合实践

把本讲的「地图」串起来，完成下面这个**全流程踩点**任务：

1. **读门面**：打开 [src/lib.rs:1-84](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/lib.rs#L1-L84)，从头读到 `#![no_std]`（第 85 行），用自己的话复述「三种队列角色 + 三种 steal 变体 + find_task 回退链」分别对应文档的哪些段落。
2. **看 feature**：对照 [Cargo.toml:26-34](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/Cargo.toml#L26-L34)，回答：`std` feature 默认开吗？它额外打开了哪两个依赖的 feature？为什么注释说「禁用 std 暂不支持」？结合 [src/lib.rs:98-109](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/lib.rs#L98-L109) 给出源码层面的理由。
3. **跑构建脚本探针**：阅读 [build.rs:5-14](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/build.rs#L5-L14)，画出「`CARGO_CFG_SANITIZE` 含 `thread` → `cargo:rustc-cfg=crossbeam_sanitize_thread` → `deque.rs` 里 `#[cfg(crossbeam_sanitize_thread)]` 生效」这条链路（点亮后的具体效果留到 u4-l3）。
4. **验证链接**：用 `ls -l src/alloc_helper.rs` 确认符号链接，并说明它为什么只有在 `std` feature 下才会被编译。
5. **生成文档**：运行 `cargo doc -p crossbeam-deque --open`，在浏览器里确认首页就是 `find_task`，并能点进 `Worker/Stealer/Injector/Steal` 四个类型的详情页。

完成上述 5 步后，你应该能在不看源码的情况下，凭记忆画出一张「文件 → 职责 → feature/cfg 守卫」的对照表。这张表就是后续阅读 `deque.rs`（2000+ 行）时的导航图。

## 6. 本讲小结

- `crossbeam-deque` 的门面是 [src/lib.rs:85-109](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/lib.rs#L85-L109)：一段 crate 级文档 + `#![no_std]` + 私有 `mod deque` + `pub use` 导出 4 个公共类型 `Worker / Stealer / Injector / Steal`。
- 实现细节全部藏在私有模块 `deque`（即 `src/deque.rs`），外部只能用 crate 根暴露的那 4 个类型，封装很干净。
- 第 85 行的 `#![no_std]` 并不代表能在裸机用——所有实质模块都被 `#[cfg(feature = "std")]` 守卫，关掉 `std` 后 crate 退化为空壳，所以 [Cargo.toml:32](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/Cargo.toml#L32-L32) 注释明说「禁用 std 暂不支持」。
- [build.rs:5-14](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/build.rs#L5-L14) 通过读 `CARGO_CFG_SANITIZE` 探测 ThreadSanitizer，并用 `cargo:rustc-cfg=crossbeam_sanitize_thread` 点亮一个供 `deque.rs` 使用的条件编译开关；`cargo:rustc-check-cfg` 用来避免「未声明 cfg」警告。
- [src/alloc_helper.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/alloc_helper.rs) 是指向兄弟 crate `crossbeam-utils` 的符号链接，是 workspace 内「单份源码、多 crate 共享」的复用技巧，且只在 `std` feature 下挂载。
- 首页的 [find_task](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/lib.rs#L52-L76) 示例既是文档也是 doctest，浓缩了 `local.pop → global.steal_batch_and_pop → stealers.steal` 的 work-stealing 回退链，是理解整个 crate 用法的最佳起点。

## 7. 下一步学习建议

本讲只建立了「代码在哪里、怎么编译」的地图，还没碰任何真正的算法。建议接着：

- **u1-l3（Worker 队列上手）**：从 `Worker::new_fifo / new_lifo`、`push / pop` 这些最直观的 API 入手，动手跑起第一个 worker，建立对 FIFO/LIFO 出队顺序的直觉。
- 之后进入第二单元（u2）深入 `deque.rs`，看 `Buffer` 环形缓冲区、`Inner`、`push/pop/steal` 的 Chase-Lev 实现。
- 想提前理解 `find_task` 里 `Steal` 枚举的 `or_else / is_retry / success` 组合子，可以先跳到 u1-l4（Stealer/Injector/Steal 工作流）。

在进入 `deque.rs` 之前，建议先回头确认：你能凭记忆说出 `lib.rs` 导出了哪 4 个类型、`build.rs` 点亮了哪个 cfg。如果这两点都清楚，就可以放心进入下一讲了。
