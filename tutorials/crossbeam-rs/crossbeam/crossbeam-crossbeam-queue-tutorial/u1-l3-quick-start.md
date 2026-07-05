# 快速上手：添加依赖、运行测试与第一个队列程序

## 1. 本讲目标

学完本讲后，你应该能够：

- 把 `crossbeam-queue` 正确地加进任意一个 Cargo 项目，并解释版本号写成 `"0.3"` 的含义；
- 在仓库里用 `cargo build` / `cargo test` 编译并跑通这个 crate 自带的测试套件，包括单独运行某一个测试；
- 通过阅读 `smoke`（冒烟）测试，在脑子里建立起 `ArrayQueue` 与 `SegQueue` 的 API 心智模型；
- 亲手写出一个最小的「两个线程之间传递整数」的程序，并能说出两种队列 API 的关键差异。

本讲是**纯动手**讲：不深入无锁算法（那是第二、三单元的事），只确保你能「装得上、跑得动、写得出」。前面两讲（[u1-l1](u1-l1-project-overview.md)、[u1-l2](u1-l2-crate-root-and-features.md)）已经让你知道了这个 crate 是什么、目录怎么排、feature 怎么开；本讲把认知落到键盘上。

## 2. 前置知识

本讲承接前两讲建立的认知：`crossbeam-queue` 只导出 `ArrayQueue`（有界）与 `SegQueue`（无界）两个 MPMC 队列，二者都依赖 `alloc` feature 与 `target_has_atomic = "ptr"` 守卫。本讲不再重复这些结论，而是直接进入「用起来」。

需要一点最基础的工程概念（都会用通俗话解释）：

- **Cargo**：Rust 的包管理器兼构建工具，`cargo build` 编译、`cargo test` 跑测试、`cargo run` 运行二进制。
- **crate**：Rust 的编译单元，也就是一个可发布的包。`crossbeam-queue` 本身就是一个 crate。
- **feature**：Cargo 的可选功能开关。本讲只关心默认的 `std`，深入开关见 [u1-l2](u1-l2-crate-root-and-features.md)。
- **MPMC / SPSC**：MPMC = 多生产者多消费者（Multiple Producer Multiple Consumer）；SPSC = 单生产者单消费者。本讲的示例先用最简单的 SPSC，综合实践再升级到 MPMC。
- **smoke test（冒烟测试）**：最小化的「点一下开关、看会不会冒烟」式的健康检查，通常只跑一两个最基本的操作。
- **backpressure（背压）**：当消费者来不及处理时，生产者被「压住」从而减速的机制。有界队列天然提供背压。

还需要一点标准库线程作用域 `std::thread::scope` 的直觉：它允许在作用域内 `spawn` 出来的子线程借用主线程的局部变量（比如借用队列 `&q`），并且保证在作用域结束前所有子线程都已 join。本讲的并发示例就靠它。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 | 本讲用它做什么 |
| --- | --- | --- |
| `README.md` | 项目对外说明，含依赖写法与最低支持 Rust 版本（MSRV） | 学习如何写依赖、版本号含义 |
| `Cargo.toml` | crate 元信息：版本号、feature、依赖 | 确认实际版本、测试依赖 |
| `src/lib.rs` | crate 根，导出 `ArrayQueue` / `SegQueue`（前讲已读） | 仅引用其导出关系与 doc-test 配置 |
| `tests/array_queue.rs` | `ArrayQueue` 的集成测试 | 精读其中的 `smoke` 测试 |
| `tests/seg_queue.rs` | `SegQueue` 的集成测试 | 精读其中的 `smoke` 测试 |

为把 API 讲准确，本讲还会**少量**引用 `src/array_queue.rs` 与 `src/seg_queue.rs` 里的方法签名（仅签名，不读算法实现）。

## 4. 核心概念与源码讲解

### 4.1 添加依赖与版本号

#### 4.1.1 概念说明

要用一个发布在 crates.io 上的 crate，标准做法是在自己项目的 `Cargo.toml` 里写一行依赖。这里有两个容易让初学者困惑的点：

1. **版本号字符串怎么写？** README 里写的是 `crossbeam-queue = "0.3"`，但 `Cargo.toml` 里实际发布的版本是 `0.3.12`。这两者并不矛盾——Cargo 默认使用「插入符要求（caret requirement）」，`"0.3"` 表示「`>=0.3.0` 且 `<0.4.0`」范围内的最新版本，所以会自动解析到当前最新的 `0.3.12`。
2. **我本地的 Rust 够不够新？** 每个 crate 会声明一个最低支持 Rust 版本（MSRV）。`crossbeam-queue` 声明的是 `1.60`，比这新的工具链都能用。

#### 4.1.2 核心流程

把 crate 加进项目的步骤：

1. 在自己的 `Cargo.toml` 的 `[dependencies]` 段写一行；
2.（可选）用 `cargo update -p crossbeam-queue` 在兼容范围内升级；
3. 在代码里 `use crossbeam_queue::ArrayQueue;` 即可。

默认开启 `std` feature，开箱即用，不需要额外的 feature 配置。

#### 4.1.3 源码精读

README 的「Usage」段直接给出了依赖写法：

```toml
[dependencies]
crossbeam-queue = "0.3"
```

详见 [README.md:26-33](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/README.md#L26-L33) —— 这就是「加这一段到你的 Cargo.toml」的官方答案。

而 crate 实际发布的版本与 MSRV 在 `Cargo.toml` 顶部：

```toml
version = "0.3.12"
edition = "2021"
# NB: Sync with msrv badge and "Compatibility" section in README.md
rust-version = "1.60"
```

详见 [Cargo.toml:7-10](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/Cargo.toml#L7-L10)。注意那行注释：MSRV 要和 README 的徽章、「Compatibility」段落保持同步——这就是为什么 README 里能看到 `Rust 1.60+` 的徽章。两点结论：

- 实际版本是 `0.3.12`，但你的依赖写 `"0.3"` 就能拿到它；
- 你的工具链只要不低于 `1.60` 就能编译。

#### 4.1.4 代码实践

**实践目标**：亲手把 `crossbeam-queue` 加进一个新项目，验证依赖能被解析。

**操作步骤**：

1. 在任意目录新建一个项目（以下为示例命令，待本地验证）：
   ```bash
   cargo new queue-demo
   cd queue-demo
   ```
2. 编辑 `queue-demo/Cargo.toml`，在 `[dependencies]` 下加一行：
   ```toml
   [dependencies]
   crossbeam-queue = "0.3"
   ```
3. 让 Cargo 解析并拉取依赖：
   ```bash
   cargo build
   ```

**需要观察的现象**：`cargo build` 会打印类似 `Updating crates.io index` 与 `Compiling crossbeam-queue v0.3.12` 的输出，说明依赖被成功解析到了 `0.3.12`。

**预期结果**：构建成功，`Cargo.lock` 里出现 `crossbeam-queue 0.3.12` 一行。若你的工具链低于 `1.60`，则会报 Rust 版本不够的错误。

#### 4.1.5 小练习与答案

1. **练习**：如果把依赖写成 `crossbeam-queue = "0.3.12"` 和写成 `"0.3"`，拉到的版本会不一样吗？
   **答案**：不会，两者都会解析到 `0.3.x` 范围内的最新版（当前即 `0.3.12`）。区别在于 `"0.3"` 允许未来自动升级到 `0.3.13` 等补丁版本，而 `"0.3.12"` 同样允许（插入符要求对 `0.x.y` 允许补丁升级）；要彻底锁死需写 `=0.3.12`。

2. **练习**：为什么 README 的依赖写 `"0.3"` 而不是 `"0.3.12"`？
   **答案**：`"0.3"` 更宽松，能自动获得 `0.3` 系列的 bug 修复，同时保证不引入 `0.4` 的破坏性变更，是文档里推荐给最终用户的写法。

---

### 4.2 构建与运行测试套件

#### 4.2.1 概念说明

一个可靠的 crate 一定自带测试。`crossbeam-queue` 的测试分两类：

- **集成测试**：放在 `tests/` 目录下，每个 `.rs` 文件被编译成一个独立的测试二进制，从「外部用户」视角调用公开 API。本讲精读的 `smoke` 测试就在这里。
- **文档测试（doc-test）**：写在 `///` 注释里的代码块，由 `cargo test` 自动抽取并编译运行，保证示例代码真能跑。

此外，测试本身也可能有依赖（`[dev-dependencies]`），只在编译测试时生效，不会污染最终用户。

#### 4.2.2 核心流程

在本仓库（crossbeam workspace）里运行测试的流程：

1. 编译：`cargo build -p crossbeam-queue`（`-p` 指定 workspace 内的某个 crate）；
2. 跑全部测试：`cargo test -p crossbeam-queue`；
3. 只跑某一类：`cargo test -p crossbeam-queue smoke`（按名字子串过滤）、`cargo test -p crossbeam-queue --test array_queue`（只跑 `tests/array_queue.rs` 这个测试二进制）、`cargo test -p crossbeam-queue --doc`（只跑文档测试）。

> 提示：你也可以 `cd crossbeam-queue && cargo test`，效果与加 `-p crossbeam-queue` 等价，因为 `crossbeam-utils` 是相对路径依赖（`path = "../crossbeam-utils"`），需要在 workspace 上下文里解析。

#### 4.2.3 源码精读

测试依赖只有一个随机数库 `fastrand`，用于 `drops` 这类需要随机扰动节奏的测试：

```toml
[dev-dependencies]
fastrand = "2"
```

详见 [Cargo.toml:42-43](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/Cargo.toml#L42-L43)。它只在测试时编译，最终用户不会引入。

`src/lib.rs` 里还有一段关于文档测试的 crate 级配置：

```rust
#![doc(test(
    no_crate_inject,
    attr(allow(dead_code, unused_assignments, unused_variables))
))]
```

详见 [src/lib.rs:9-12](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/lib.rs#L9-L12)。`no_crate_inject` 表示文档测试不会自动 `extern crate self`，`attr(...)` 则放宽了文档示例里常见的「未使用变量」等告警——这些都是为了让 `///` 里的短示例能干净地通过 `cargo test --doc`。

#### 4.2.4 代码实践

**实践目标**：跑通这个 crate 的测试套件，并学会只跑一个测试。

**操作步骤**（待本地验证）：

1. 在仓库根目录执行：
   ```bash
   cargo test -p crossbeam-queue
   ```
2. 再单独跑冒烟测试：
   ```bash
   cargo test -p crossbeam-queue smoke
   ```
3. 再单独跑 `ArrayQueue` 的测试文件：
   ```bash
   cargo test -p crossbeam-queue --test array_queue
   ```

**需要观察的现象**：第 1 步会看到多个测试二进制（`array_queue`、`seg_queue`、doc-tests）依次编译并运行，每个测试输出 `test smoke ... ok`。注意很多测试内部用了 `cfg!(miri)` 来在 Miri 下缩小规模（例如 `COUNT` 从 `100_000` 降到 `50`），普通运行下会用更大的规模。

**预期结果**：全部测试 `ok`，结尾 `test result: ok.`。若单独跑 `smoke`，应只看到两个 `smoke` 测试（`array_queue::smoke` 与 `seg_queue::smoke`）被执行。

#### 4.2.5 小练习与答案

1. **练习**：`cargo test -p crossbeam-queue smoke` 为什么能同时匹配到两个文件里的 `smoke`？
   **答案**：`cargo test` 的第一个位置参数是「名字子串过滤器」，它会在所有测试二进制里匹配函数名**包含** `smoke` 的测试，不区分来自哪个文件，所以 `array_queue.rs` 和 `seg_queue.rs` 里的 `smoke` 都会被选中。

2. **练习**：`fastrand` 出现在 `[dev-dependencies]` 而不是 `[dependencies]`，有什么好处？
   **答案**：`[dev-dependencies]` 只在编译测试、示例、benchmark 时生效，不会进入最终发布给用户的依赖树，既减小了用户的依赖体积，也避免了把测试专用工具暴露成「必需依赖」。

---

### 4.3 阅读 smoke 测试，建立 API 心智模型

#### 4.3.1 概念说明

最快的建立 API 直觉的方法，不是读文档，而是**读项目自己写的冒烟测试**——它是项目作者亲手示范的「最小正确用法」。本节把 `ArrayQueue` 与 `SegQueue` 的 `smoke` 测试并排读，一眼就能看出两者 API 的同与不同。

#### 4.3.2 核心流程

冒烟测试做的事都一样：构造队列 → push 两个数 → pop 出来比对 → 再 pop 一次确认空了。差异藏在三处细节里：

| 关注点 | `ArrayQueue` | `SegQueue` |
| --- | --- | --- |
| 构造 | `ArrayQueue::new(1)` —— **必须给容量** | `SegQueue::new()` —— **不给容量** |
| `push` 返回 | `Result<(), T>`，满时返回 `Err(value)`，所以要 `.unwrap()` | `()`（单元类型），永不失败，无需 `.unwrap()` |
| `pop` 返回 | `Option<T>`，空时 `None` | `Option<T>`，空时 `None` |

把这三点记住，你就掌握了 90% 的日常用法。

#### 4.3.3 源码精读

`ArrayQueue` 的冒烟测试：

```rust
#[test]
fn smoke() {
    let q = ArrayQueue::new(1);

    q.push(7).unwrap();
    assert_eq!(q.pop(), Some(7));

    q.push(8).unwrap();
    assert_eq!(q.pop(), Some(8));
    assert!(q.pop().is_none());
}
```

详见 [tests/array_queue.rs:6-16](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/array_queue.rs#L6-L16)。注意容量是 `1`：push 进 `7` 后队列已满，但紧接着就 pop 出来了，所以后面 push `8` 仍能成功。`push` 返回 `Result`，于是用 `.unwrap()` 断言「确实推进成功」。

`SegQueue` 的冒烟测试：

```rust
#[test]
fn smoke() {
    let q = SegQueue::new();
    q.push(7);
    assert_eq!(q.pop(), Some(7));

    q.push(8);
    assert_eq!(q.pop(), Some(8));
    assert!(q.pop().is_none());
}
```

详见 [tests/seg_queue.rs:6-15](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/seg_queue.rs#L6-L15)。和上面几乎逐行对应，唯一不同：`SegQueue::new()` 不传容量，`push` 直接调用、没有 `.unwrap()`（因为返回 `()`）。

> 为什么 `ArrayQueue::new(0)` 不可行？仓库专门有一个测试：
> ```rust
> #[test]
> #[should_panic(expected = "capacity must be non-zero")]
> fn zero_capacity() {
>     let _ = ArrayQueue::<i32>::new(0);
> }
> ```
> 详见 [tests/array_queue.rs:26-30](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/array_queue.rs#L26-L30)。容量为 0 会直接 panic，这是有界队列的硬性约束；而无界的 `SegQueue` 根本不接受容量参数，也就不存在这个问题。

为把「API 心智模型」夯实，下表给出真实的公开方法签名（**仅签名，不涉及实现**），供你查阅：

| 方法 | `ArrayQueue`（有界） | `SegQueue`（无界） |
| --- | --- | --- |
| 构造 | [`new(cap)`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L96) | [`new()`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L188) |
| 入队 | [`push -> Result<(), T>`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L203) | [`push -> ()`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L214) |
| 覆盖式入队 | [`force_push -> Option<T>`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L275) | 无 |
| 出队 | [`pop -> Option<T>`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L318) | [`pop -> Option<T>`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L364) |
| 容量 | [`capacity -> usize`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L441) | 无（无界） |
| 是否满 | [`is_full -> bool`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L483) | 无 |
| 长度 / 是否空 | [`len`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L510) / [`is_empty`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L458) | [`len`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L563) / [`is_empty`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L541) |

（两类型还各有 `push_mut` / `pop_mut` 两个「独占引用」变体，属于进阶内容，留到 [u2-l4](u2-l4-arrayqueue-exclusive-mut.md) 讲。）

#### 4.3.4 代码实践

**实践目标**：通过修改冒烟测试来确认你对「满」与「空」的理解。

**操作步骤**：

1. 复制 `tests/array_queue.rs` 里的 `smoke` 测试，在本地把它改名为 `smoke_full`，把容量改成 `1` 后尝试连续 push 两次：
   ```rust
   let q = ArrayQueue::new(1);
   assert!(q.push(7).is_ok());    // 第一个：成功
   assert!(q.push(8).is_err());   // 第二个：满，返回 Err(8)
   ```
2. 运行 `cargo test -p crossbeam-queue smoke_full`。

**需要观察的现象**：第二次 `push` 不再 `.unwrap()`，而是断言它返回 `Err`——这是有界队列在「满」时给生产者的信号。

**预期结果**：测试通过。把同样的「连续 push 两次」搬到 `SegQueue` 上则**不会**失败，因为 `SegQueue::push` 返回 `()`，无界队列不会拒绝入队——这正是两种队列最本质的行为差异。

#### 4.3.5 小练习与答案

1. **练习**：`ArrayQueue` 的 `push` 满了返回 `Err(value)`，把原始 `value` 还了回来。这样设计比起「满了就 panic」或「满了就丢弃」有什么好处？
   **答案**：把值还给调用者，调用者可以自行决定重试、丢弃或转存，**不丢失数据、不中断程序**。这在并发场景下尤其重要——生产者被背压住时，能安全地把数据留在手里等待下一次尝试。

2. **练习**：`SegQueue` 没有 `capacity` / `is_full`，这反映了它怎样的设计取舍？
   **答案**：`SegQueue` 是无界的，会按需动态分配新分段（segment），所以「容量」是无限、「是否满」恒为假，提供这两个方法没有意义。代价是内存使用不可控、且每次扩容有分配开销；好处是不会丢数据、能吸收突发流量。这正是 [u1-l1](u1-l1-project-overview.md) 讲的选型权衡。

3. **练习**：两种队列的 `pop` 在空时都返回什么？为什么不也返回 `Result`？
   **答案**：都返回 `None`。用 `Option<T>` 而非 `Result` 是因为「队列为空」是**正常的、预期的**状态（不是错误），用 `None` 表达「现在没有数据」最自然，调用者通常用 `while let Some(x) = q.pop()` 或循环重试来处理。

---

### 4.4 亲手写出第一个并发 push/pop 程序

#### 4.4.1 概念说明

冒烟测试是单线程的。真正的并发队列要在**多个线程之间**传递数据。本节带你写一个最小的 SPSC 程序：一个生产者线程把 `0..1000` 推进队列，一个消费者线程把它们弹出来，并校验顺序。

这里会出现本讲最重要的两个并发套路：

- **生产者重试循环**：`ArrayQueue` 满了时 `push` 返回 `Err`，生产者要 `while q.push(i).is_err() {}` 重试，这就是「背压」的直接体现；`SegQueue` 不会满，直接 `q.push(i)`。
- **消费者重试循环**：两种队列空时 `pop` 都返回 `None`，消费者要 `loop { if let Some(x) = q.pop() { ... break } }` 等数据。

#### 4.4.2 核心流程

程序结构（伪代码）：

```
构造队列 q
线程作用域:
    生产者线程: for i in 0..1000 { 重试 push(i) 直到成功 }
    消费者线程: for i in 0..1000 { 重试 pop 直到拿到值; 断言值 == i }
断言队列已空
```

> 关于线程作用域：下面用标准库的 `std::thread::scope`（自 Rust 1.63 稳定）。它允许子线程借用主线程的 `&q`，并在作用域结束前自动 join。本项目自己的测试用的是 `crossbeam_utils::thread::scope`（你能在 `tests/` 顶部看到 `use crossbeam_utils::thread::scope;`），二者用法几乎一样；为了让你的 demo 少一个依赖，这里用标准库版本。

#### 4.4.3 源码精读

本节的并发套路直接复刻自仓库里的 `spsc` 测试。以 `ArrayQueue` 为例，其生产者/消费者的核心写法是：

```rust
scope.spawn(|_| {
    for i in 0..COUNT {
        while q.push(i).is_err() {}   // 满 → 重试
    }
});

scope.spawn(|_| {
    for i in 0..COUNT {
        loop {
            if let Some(x) = q.pop() { // 空 → 重试
                assert_eq!(x, i);
                break;
            }
        }
    }
    assert!(q.pop().is_none());
});
```

详见 [tests/array_queue.rs:148-172](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/array_queue.rs#L148-L172)（`ArrayQueue` 的 `spsc` 测试）。`SegQueue` 的同名测试结构一致，只是生产者写成 `q.push(i)`（无 `while` 重试），详见 [tests/seg_queue.rs:102-126](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/seg_queue.rs#L102-L126)。

#### 4.4.4 代码实践

**实践目标**：新建一个临时 binary，分别用 `ArrayQueue` 与 `SegQueue` 在两个线程间传递 `0..1000` 并校验顺序，运行通过后对比两者 API 异同。这是本讲的核心动手任务。

**操作步骤**：

1. 用 4.1.4 里创建的 `queue-demo` 项目，把 `src/main.rs` 替换为下面的**示例代码**：

   ```rust
   // 示例代码：不属于 crossbeam-queue 项目，是读者自己写的 demo
   use crossbeam_queue::{ArrayQueue, SegQueue};

   const COUNT: usize = 1000;

   fn main() {
       // ---- ArrayQueue：有界，push 满了返回 Err，需要重试 ----
       {
           // 容量故意设成 100（小于 COUNT），让背压显现出来
           let q = ArrayQueue::new(100);

           std::thread::scope(|s| {
               s.spawn(|| {
                   for i in 0..COUNT {
                       while q.push(i).is_err() {} // 队列满 → 重试
                   }
               });
               s.spawn(|| {
                   for i in 0..COUNT {
                       loop {
                           if let Some(x) = q.pop() { // 队列空 → 重试
                               assert_eq!(x, i, "ArrayQueue 顺序错误");
                               break;
                           }
                       }
                   }
               });
           });

           assert!(q.pop().is_none(), "ArrayQueue 应已为空");
           println!("ArrayQueue: 0..{COUNT} 全部按序到达 ✓");
       }

       // ---- SegQueue：无界，push 永远成功 ----
       {
           let q = SegQueue::new(); // 不传容量

           std::thread::scope(|s| {
               s.spawn(|| {
                   for i in 0..COUNT {
                       q.push(i); // 无界，不会失败
                   }
               });
               s.spawn(|| {
                   for i in 0..COUNT {
                       loop {
                           if let Some(x) = q.pop() {
                               assert_eq!(x, i, "SegQueue 顺序错误");
                               break;
                           }
                       }
                   }
               });
           });

           assert!(q.pop().is_none(), "SegQueue 应已为空");
           println!("SegQueue: 0..{COUNT} 全部按序到达 ✓");
       }
   }
   ```

2. 确认 `Cargo.toml` 里有（4.1.4 已加）：
   ```toml
   [dependencies]
   crossbeam-queue = "0.3"
   ```
3. 运行：
   ```bash
   cargo run
   ```

**需要观察的现象**：程序打印两行勾号。注意 `ArrayQueue` 那段里 `while q.push(i).is_err() {}`——当队列满到 100 时，生产者线程会在此处忙等，直到消费者 pop 出空位。这就是有界队列的**背压**：生产者被消费者的速度「拽住」。而 `SegQueue` 那段没有这个 while 循环，因为它的 `push` 永远立即成功（必要时内部会分配新分段）。

**预期结果**：两次断言都通过，输出：
```
ArrayQueue: 0..1000 全部按序到达 ✓
SegQueue: 0..1000 全部按序到达 ✓
```

**API 异同小结**（运行后请自己核对）：
- 构造：`ArrayQueue::new(100)` 必须给容量；`SegQueue::new()` 不给。
- 入队：`ArrayQueue::push` 返回 `Result`，满则 `Err`；`SegQueue::push` 返回 `()`，永不失败。
- 出队：两者 `pop` 都返回 `Option`，空则 `None`，写法完全一致。

> 若无法本地运行，以上输出标注为「待本地验证」；但代码本身是对仓库 `spsc` 测试套路的忠实复刻，逻辑正确。

#### 4.4.5 小练习与答案

1. **练习**：把 `ArrayQueue::new(100)` 的容量改成 `1`，程序还能正确运行吗？为什么？
   **答案**：能，但吞吐会很低。容量为 1 意味着生产者每推一个就必须等消费者取走，`while q.push(i).is_err() {}` 几乎每次都要忙等。功能（顺序与不丢数）不受影响，因为背压会保证不会覆盖。这正是有界队列「用容量换内存可控、用背压换不丢数」的体现。

2. **练习**：为什么消费者用 `loop { if let Some(x) = q.pop() { ...; break } }` 而不是直接 `q.pop().unwrap()`？
   **答案**：因为生产者可能还没来得及 push，此时 `pop` 合法地返回 `None`。`.unwrap()` 会 panic，而循环重试能在「暂时为空」时耐心等待。这是无锁并发队列消费端的标配写法。

3. **练习**：如果在 `ArrayQueue` 的 demo 里把 `q.push(i)` 换成 `q.force_push(i)`，行为会怎样变化？
   **答案**：`force_push` 在队列满时不再阻塞/重试，而是**直接覆盖最旧的元素**并把被覆盖的值通过 `Option<T>` 返回。于是生产者不再被背压，但被覆盖的旧数据会丢失——这就是「环形缓冲」语义。顺序断言可能会失败（消费者可能错过某些 `i`）。这部分的原理留到 [u2-l3](u2-l3-arrayqueue-force-push-capacity.md) 详讲。

---

## 5. 综合实践

把 4.4 的 SPSC demo 升级成 **MPMC**，把本讲的知识串起来。

**任务**：用 2 个生产者线程（各推 `0..1000`）和 2 个消费者线程，验证「每个值恰好被消费一次」，分别对 `ArrayQueue` 和 `SegQueue` 各做一遍。

**提示与要求**：

1. 全局顺序不再是 `0,1,2,...`（两个生产者交错入队），所以**不能再断言 `x == i`**。改用一个计数向量 `v: Vec<AtomicUsize>`，每消费到一个值 `x` 就 `v[x].fetch_add(1, ...)`，最后断言每个 `v[x]` 都等于生产者线程数（这里是 2）。这正是仓库 `mpmc` 测试的做法，详见 [tests/array_queue.rs:215-249](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/array_queue.rs#L215-L249) 与 [tests/seg_queue.rs:128-162](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/seg_queue.rs#L128-L162)。
2. 对 `ArrayQueue`，生产者仍需 `while q.push(i).is_err() {}`；对 `SegQueue`，直接 `q.push(i)`。
3. 思考并记录：为什么 MPMC 下我们改用「计数向量」而不是「顺序断言」？这背后是并发队列「单生产者保序、多生产者不保全局序」的本质。
4.（进阶）给 `ArrayQueue` 版本再开一个实验：把某个生产者的 `push` 换成 `force_push`，预测并观察哪些值会被覆盖丢失、`v` 的最终计数会变成什么样。

**验收标准**：两种队列的 MPMC 版本都能跑完且每个值恰好被消费「生产者线程数」次；你能用一句话说清 SPSC 与 MPMC 在「如何验证正确性」上的差别。

## 6. 本讲小结

- 把 `crossbeam-queue` 加进项目只需 `crossbeam-queue = "0.3"`，会解析到当前最新的 `0.3.12`，要求工具链不低于 `1.60`。
- 在仓库里用 `cargo test -p crossbeam-queue` 跑测试，可用名字子串、`--test <file>`、`--doc` 精确控制运行范围；测试专用依赖 `fastrand` 只在 `[dev-dependencies]`。
- 冒烟测试是最好的 API 教材：`ArrayQueue::new(cap)` 有界、`push` 返回 `Result`；`SegQueue::new()` 无界、`push` 返回 `()`；两者 `pop` 都返回 `Option`，空则 `None`。
- 写并发程序的两条标配套路：生产者 `while q.push(i).is_err() {}`（仅 `ArrayQueue` 需要）、消费者 `loop { if let Some(x) = q.pop() { ... } }`。
- 有界队列天然提供背压（生产者被满队拽住），无界队列不丢数据但内存不可控——选型决定写法。
- 标准库 `std::thread::scope`（或项目自带的 `crossbeam_utils::thread::scope`）让子线程能借用队列并在作用域结束前自动 join，是写这类 demo 的关键工具。

## 7. 下一步学习建议

你已经能「用起来」了。接下来该「读懂它」：

- 想理解 `ArrayQueue` 为什么有界、`force_push` 怎么覆盖最旧元素、`push` 满了到底怎么判定，进入 **[u2-l1](u2-l1-arrayqueue-data-structure.md)**（`ArrayQueue` 的数据结构：stamp/lap/Slot），再顺次读 [u2-l2](u2-l2-arrayqueue-push-pop.md)、[u2-l3](u2-l3-arrayqueue-force-push-capacity.md)。
- 想理解 `SegQueue` 怎么做到无界、`push` 为什么永不失败，进入 **[u3-l1](u3-l1-segqueue-block-structure.md)**（分块链表与 Block 结构）。
- 在进入源码前，建议先回头把本讲的 `spsc` / `mpmc` demo 多跑几遍，带着「我写的这段代码，库里到底是怎么实现的」这个问题去读第二、三单元，效果最好。
