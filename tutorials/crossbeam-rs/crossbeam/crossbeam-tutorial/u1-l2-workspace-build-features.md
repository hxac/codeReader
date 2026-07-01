# 工作区、构建与特性系统

## 1. 本讲目标

上一讲（[u1-l1](u1-l1-project-overview.md)）我们建立了 crossbeam 的「全景地图」：它是一套并发工具集，由 6 个子 crate 组成，主 crate 以门面（facade）模式把它们重导出为统一的 `crossbeam::*`，并按 `no_std` / `alloc` / `std` 三级特性分级。

本讲要回答三个工程问题：

1. **这些子 crate 在物理上是怎么组织在一起的？** 为什么用 Cargo workspace？为什么有些子 crate 是「必选」、有些是「可选」？
2. **`std` / `alloc` / `no_std` 三级支持在 Cargo 层面是怎么实现的？** 特性（feature）是如何从主 crate 一层一层传递到子 crate 的？
3. **我该如何在本地构建、测试、运行示例？项目 CI 又在替我们守护哪些正确性？**

学完本讲，你应该能够：

- 读懂根 `Cargo.toml` 中的 workspace 定义、`path` 依赖与特性声明。
- 预测 `cargo build -p crossbeam --no-default-features --features alloc` 之后哪些模块仍然可用。
- 在本地用与 CI 等价（或子集）的命令构建与测试项目。

## 2. 前置知识

本讲是「工程入门」，不涉及任何并发算法，但需要你大致了解以下概念。下面对每个概念做一句通俗解释。

- **Cargo**：Rust 官方的构建系统与包管理器。`cargo build` 编译、`cargo test` 测试、`cargo doc` 生成文档。包（library/binary）在 Cargo 里叫 **crate**。
- **Workspace（工作区）**：把多个相关的 crate 放在同一个仓库里共享同一个 `Cargo.lock`、同一套依赖解析和同一组命令（如 `cargo build --workspace` 一把编译全部）。crossbeam 就是典型的多 crate workspace。
- **Feature（特性）**：crate 作者定义的可选开关，例如 `std`。用户在依赖时可以开启或关闭特性，从而选择性地启用/禁用一部分 API。特性之间可以「连带」开启别的特性。
- **`no_std`**：不依赖 Rust 标准库 `std` 的运行环境（如裸机、嵌入式、内核）。`no_std` 程序通常只能用 `core`（永不失败的基础原语）和可选的 `alloc`（堆分配）。
- **`alloc`**：Rust 的堆分配库（`Box`、`Vec`、`Arc` 等）。它比 `std` 小——`std = core + alloc + 操作系统封装（线程、文件、网络……）`。所以三级能力是：`no_std`（只有 core）< `alloc`（core + 堆）< `std`（全套）。
- **MSRV（Minimum Supported Rust Version）**：项目保证能编译通过的最低 Rust 版本。

> 关键直觉：crossbeam 想「一套代码、三级能力」。同一份源码，通过特性门控（`#[cfg(feature = "...")]`）在嵌入式（`no_std`）、只有堆的内核（`alloc`）、普通应用（`std`）三种环境下都能用。本讲就是拆解 Cargo 是如何把这件事安排好的。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [Cargo.toml](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/Cargo.toml) | 根清单：既是主 crate `crossbeam` 的包定义，又定义了整个 workspace、特性与依赖。本讲最重要的文件。 |
| [crossbeam-utils/Cargo.toml](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/Cargo.toml) | 基石子 crate 的清单，演示「子 crate 自己的 `std`/`atomic` 特性」是怎么定义的。 |
| [crossbeam-epoch/Cargo.toml](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/Cargo.toml) | epoch 子 crate 的清单，演示「特性向下游 crate 继续传递」的链式写法。 |
| [.github/workflows/ci.yml](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/.github/workflows/ci.yml) | GitHub Actions CI 流水线：测试、特性组合、miri、sanitizer、loom、文档、lint 等任务。 |
| [ci/test.sh](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/ci/test.sh) | CI 调用的「跑测试」脚本，等价于我们本地该用的命令。 |
| [ci/check-features.sh](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/ci/check-features.sh) | CI 调用的「检查所有特性组合」脚本，用 `cargo-hack` 枚举特性幂集，并在 `no_std` 目标上构建。 |
| [src/lib.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/src/lib.rs) | 主 crate 的门面源码，里面用 `#[cfg(feature = "...")]` 把特性「兑现」成可见的模块（下一讲 u1-l3 会精读，本讲只引用它与特性的对应关系）。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：① workspace 成员与 `path` 依赖；② 特性门控的层层传递；③ CI 任务与本地构建/测试命令。

### 4.1 workspace 成员与 path 依赖

#### 4.1.1 概念说明

crossbeam 仓库里有 **8 个**被纳入 workspace 的 crate。如果各自独立成包，会出现「每个 crate 各跑一次依赖解析、各锁一份版本」的混乱。Cargo workspace 解决了这个问题：

- 共享一份 `Cargo.lock`，保证所有子 crate 用同一棵依赖树。
- 共享一组 lint 规则（见后文 `[workspace.lints]`）。
- 允许一条命令操作全部 crate：`cargo build --workspace`、`cargo test --workspace`。
- 子 crate 之间用 **`path` 依赖**互相引用本地源码（开发时实时联动），同时附带 `version` 字段以便发布到 crates.io 后被外部按版本引用。

#### 4.1.2 核心流程

workspace 的组织可以概括为：

```
crossbeam (根 workspace)
├── .                            # 主 crate "crossbeam"，门面，重导出各子 crate
├── crossbeam-utils/             # 基石：原子、CachePadded、Backoff、Parker、scope…
├── crossbeam-channel/           # MPMC 通道
├── crossbeam-channel/benchmarks/# 通道性能基准（独立 crate，不发布）
├── crossbeam-epoch/             # 基于 epoch 的内存回收
├── crossbeam-queue/             # ArrayQueue / SegQueue
├── crossbeam-deque/             # 工作窃取双端队列
└── crossbeam-skiplist/          # 无锁跳表（实验性，尚未纳入主 crate）
```

依赖关系（谁依赖谁）：

```
crossbeam (门面) ──► crossbeam-utils (必选)
                 ──► crossbeam-channel / deque / epoch / queue (可选，由特性开启)
crossbeam-epoch ──► crossbeam-utils
crossbeam-queue ──► crossbeam-utils
crossbeam-deque ──► crossbeam-epoch ──► crossbeam-utils
```

注意一个 **不对称**：`crossbeam-utils` 是主 crate 的**必选**依赖（永远在场，因为 `atomic`、`Backoff`、`CachePadded` 这些「no_std 级」工具总是要导出）；而 channel/deque/epoch/queue 四个子 crate 是**可选**依赖（只有当用户开启对应特性时才拉进来）。`crossbeam-skiplist` 虽然是 workspace 成员，却**根本不是**主 crate 的依赖——它还在实验阶段，没被门面重导出（详见上一讲 u1-l1）。

#### 4.1.3 源码精读

workspace 成员清单写在根 `Cargo.toml` 末尾的 `[workspace]` 段：

[Cargo.toml:63-74](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/Cargo.toml#L63-L74) — 列出全部 8 个 workspace 成员，其中 `"."` 代表仓库根目录本身（即主 crate `crossbeam`），`crossbeam-channel/benchmarks` 是嵌套子目录里的独立 crate。

成员之间用「`path` + `version`」双重声明互相引用。以主 crate 对子 crate 的依赖为例：

[Cargo.toml:51-56](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/Cargo.toml#L51-L56) — 注意三点：
1. 每行都带 `path = "crossbeam-xxx"`，开发时直接用本地源码，无需先发布。
2. 同时带 `version = "0.x"`，发布到 crates.io 后，外部用户按版本号拉取。
3. `crossbeam-channel/deque/epoch/queue` 标了 `optional = true`（按需拉入）；唯独 `crossbeam-utils` 没有 `optional`，且额外 `features = ["atomic"]`（始终开启它的 `atomic` 模块）。

子 crate 之间的依赖写法一样，例如 epoch 依赖 utils：

[crossbeam-epoch/Cargo.toml:46](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/Cargo.toml#L46) — `path = "../crossbeam-utils"` 用相对路径指回同级目录，同样 `default-features = false` 后再显式开启 `features = ["atomic"]`。

> 这个 `default-features = false` 是 crossbeam 全仓库的一致约定：**引用任何子 crate 时都先关掉它的默认特性（即关掉 `std`），再按需打开**。这是实现「特性层层传递、不污染」的前提——否则只要有一处忘了关默认特性，`std` 就会被悄悄带上，`no_std` 构建就毁了。

workspace 还统一了 lint 规则，所有子 crate 用 `[lints] workspace = true` 继承：

[Cargo.toml:76-86](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/Cargo.toml#L76-L86) — 在 workspace 层定义 `rust_2018_idioms`、`unreachable_pub` 等 lint，并显式声明几个合法的 `cfg` 标志（`crossbeam_loom`、`crossbeam_sanitize`、`gha_macos_runner`），避免 `unexpected_cfgs` 警告。

#### 4.1.4 代码实践

1. **实践目标**：用 `cargo` 命令亲眼确认 workspace 的结构与依赖树。
2. **操作步骤**：
   ```bash
   # 列出 workspace 全部成员
   cargo metadata --no-deps --format-version 1 | jq '.packages[].name'
   # 打印依赖树（只看一层，看清主 crate 直接依赖了谁）
   cargo tree -p crossbeam --depth 1
   ```
3. **需要观察的现象**：
   - `cargo metadata` 会列出 8 个包名（含 `benchmarks`）。
   - `cargo tree -p crossbeam --depth 1` 在默认特性（`std`）下应能看到 `crossbeam-channel`、`crossbeam-deque`、`crossbeam-epoch`、`crossbeam-queue`、`crossbeam-utils` 全部出现。
4. **预期结果**：默认特性下五个子 crate 都在；若加 `--no-default-features` 再看依赖树，channel/deque/epoch/queue 会消失，只剩 `crossbeam-utils`。
5. 待本地验证（不同 Cargo 版本 `cargo tree` 输出格式可能略有差异）。

#### 4.1.5 小练习与答案

**练习 1**：`crossbeam-skiplist` 在 workspace 成员清单里，却不在主 crate 的依赖里。这意味着什么？

> **答案**：它会被 `cargo build --workspace` 编译、被 CI 检查，但不能通过 `crossbeam::skiplist` 访问（门面没有重导出它）。它是「仓库内的实验性 crate」，独立发布、独立使用，例如用户要自己 `cargo add crossbeam-skiplist`。

**练习 2**：为什么每个 `path` 依赖都要同时写 `version`？

> **答案**：`path` 只在本地 workspace 内生效；当主 crate 发布到 crates.io 后，外部用户通过 `crossbeam = "0.8"` 拉取时，Cargo 需要 `version` 才能从 crates.io 找到对应的子 crate。两者并存 = 「本地开发走 path，发布后走 version」。

---

### 4.2 特性门控：default=std、std 依赖 alloc、可选子 crate

#### 4.2.1 概念说明

特性（feature）是 Cargo 的「编译期开关」。crossbeam 用它来实现同一份源码、三级能力（`no_std` / `alloc` / `std`）。核心思想只有两条规则：

1. **特性可连带**：开启特性 A 可以自动开启特性 B（`std = ["alloc"]` 表示「要 std 就必须先要 alloc」），还可以开启**依赖 crate 的特性**（`std = ["crossbeam-channel/std"]` 表示「同时把子 crate channel 的 std 也打开」）。
2. **特性门控源码**：在 `.rs` 里用 `#[cfg(feature = "std")]` 标注的代码，只有开启 `std` 才会编译。门面 `src/lib.rs` 就是用这套机制决定哪些模块对外可见。

#### 4.2.2 核心流程

主 crate 的特性定义可以画成一张「连带图」：

```
default ──► std

std  ──► alloc
std  ──► crossbeam-channel/std   (开启可选依赖 channel，并打开它的 std)
std  ──► crossbeam-deque/std
std  ──► crossbeam-epoch/std
std  ──► crossbeam-queue/std
std  ──► crossbeam-utils/std

alloc ──► crossbeam-epoch/alloc  (开启可选依赖 epoch 的 alloc)
alloc ──► crossbeam-queue/alloc
```

于是「连带」会层层向下传递。例如用户开 `crossbeam/std`：

```
crossbeam/std
  └─► crossbeam/alloc
  └─► crossbeam-epoch/std
        └─► crossbeam-epoch/alloc
        └─► crossbeam-utils/std        ← 这里跨到了 utils 子 crate
```

最终连最底层的 `crossbeam-utils/std` 都被点亮。这就是「std/alloc 特性如何在子 crate 间层层传递」的字面含义。

这套设计带来的可预测结果（与门面 `src/lib.rs` 的 `#[cfg]` 一一对应）：

| 构建选项 | `atomic`/`utils`（Backoff/CachePadded/AtomicCell） | `epoch`/`queue` | `channel`/`deque`/`scope`/`sync` |
| --- | --- | --- | --- |
| 默认（`std`） | ✅ | ✅ | ✅ |
| `--no-default-features --features alloc` | ✅ | ✅ | ❌ |
| `--no-default-features`（纯 no_std） | ✅ | ❌ | ❌ |

> 依据：`src/lib.rs` 中 `atomic` 与 `utils` 无条件导出（始终可用）；`epoch`/`queue` 用 `#[cfg(feature = "alloc")]` 门控；`channel`/`deque`/`scope`/`sync` 用 `#[cfg(feature = "std")]` 门控。

#### 4.2.3 源码精读

主 crate 的特性定义在根 `Cargo.toml`：

[Cargo.toml:33-49](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/Cargo.toml#L33-L49) — 三段含义：
- `default = ["std"]`：默认就是「带标准库」的完整版。
- `std = ["alloc", "crossbeam-channel/std", ...]`：开 `std` 自动连带开 `alloc`，并把五个子 crate 的 `std` 一并打开。
- `alloc = ["crossbeam-epoch/alloc", "crossbeam-queue/alloc"]`：开 `alloc` 只连带 epoch/queue 两个「需要堆分配的数据结构」的 `alloc`，**不**连带 channel/deque（它们需要操作系统线程，属于 `std` 才有的能力）。

门面源码用 `#[cfg]` 把这些特性「兑现」成可见模块：

[src/lib.rs:41-55](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/src/lib.rs#L41-L55) — `#![no_std]` 声明主 crate 默认不链接 `std`；`#[cfg(feature = "std")] extern crate std;` 表示只有开 `std` 特性时才把标准库引回来。`atomic` 模块是无条件 `pub use`，所以它「永远可用」。

[src/lib.rs:68-79](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/src/lib.rs#L68-L79) — 这是上一节表格的权威依据：`scope`、`channel`、`deque`、`sync` 被 `#[cfg(feature = "std")]` 门控；`epoch`、`queue` 被 `#[cfg(feature = "alloc")]` 门控。

现在看「下游」子 crate 自己的特性是怎么定义的，验证连带确实能传过去。`crossbeam-utils` 的特性最简单：

[crossbeam-utils/Cargo.toml:27-36](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/Cargo.toml#L27-L36) — `default = ["std"]`、`std = []`（空的，仅作开关），另外有个独立的 `atomic = ["atomic-maybe-uninit"]`。注意它的 `rust-version = "1.56"`（[第 10 行](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/Cargo.toml#L10)）低于主 crate 的 1.74，但 `atomic` 特性**额外要求 1.74**——这就是 CI 在 MSRV 下要单独排除 `atomic` 来测试的原因（见 4.3）。

`crossbeam-epoch` 演示了「子 crate 把特性继续往下传」：

[crossbeam-epoch/Cargo.toml:26-37](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/Cargo.toml#L26-L37) — `std = ["alloc", "crossbeam-utils/std"]`：epoch 开 `std` 时，除了连带自己的 `alloc`，还会**跨 crate**打开 `crossbeam-utils/std`。这正是「层层传递」的中间一跳。注释还提醒一个重要约束：**同时关掉 `std` 和 `alloc` 目前不被支持**（epoch 的核心全局收集器需要堆）。`crossbeam-queue` 的写法完全相同（[crossbeam-queue/Cargo.toml:26-37](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/Cargo.toml#L26-L37)）。

> 小结：把主 crate 与三个子 crate 的特性声明连起来看，就能画出一张「点亮」传播图。`crossbeam/std` 这一个开关，最终会沿着 `crossbeam → crossbeam-epoch → crossbeam-utils` 这条链，把沿途每一级的 `std`/`alloc` 都打开。这就是「特性在子 crate 间层层传递」的全部机制——没有任何魔法，全靠 `[features]` 段里的字符串连带。

#### 4.2.4 代码实践

1. **实践目标**：亲手验证「不同特性组合下，门面暴露的模块不同」。
2. **操作步骤**：
   ```bash
   # (a) 默认 std：应能编译并暴露全部模块
   cargo build -p crossbeam

   # (b) 只开 alloc：epoch/queue 在，channel/deque/scope 不在
   cargo build -p crossbeam --no-default-features --features alloc

   # (c) 纯 no_std（不开 alloc 不开 std）：只剩 atomic + utils
   cargo build -p crossbeam --no-default-features
   ```
3. **需要观察的现象**：第 (b) 步构建成功，说明 `crossbeam::epoch`、`crossbeam::queue`、`crossbeam::atomic`、`crossbeam::utils` 可用，但 `crossbeam::channel`、`crossbeam::deque`、`crossbeam::scope` 因 `std` 未开而**不存在**（任何引用它们的代码会编译失败）。
4. **预期结果**：与上一节表格一致。可用一行小程序验证，例如在 (b) 配置下写 `use crossbeam::queue::ArrayQueue;` 能过，而 `use crossbeam::channel;` 会报「cannot find」。
5. 待本地验证（取决于你实际的工作区特性解析；可配合 `cargo tree -e features -p crossbeam` 查看特性连通图）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `alloc` 特性连带的是 epoch/queue，而不是 channel/deque？

> **答案**：channel 内部用到了操作系统线程（阻塞/唤醒、`Parker`），deque 同样依赖线程与 epoch；这些属于 `std` 才提供的能力，仅靠 `alloc`（堆）不够。而 epoch 和 queue 的有界/无界队列在 `no_std + alloc` 环境下就能工作，所以它们挂在 `alloc` 这一级。

**练习 2**：如果有人把根 `Cargo.toml` 里 `crossbeam-utils` 的依赖去掉 `default-features = false`，会发生什么？

> **答案**：`crossbeam-utils` 的 `default = ["std"]` 会被启用，导致即使主 crate 用 `--no-default-features`，`std` 仍被 utils 带进来，`no_std` 构建失败。这就是全仓库坚持「先关默认特性再按需开」的原因。

---

### 4.3 CI 任务与本地构建/测试命令

#### 4.3.1 概念说明

crossbeam 是并发底层库，正确性极其关键（一个数据竞争可能让下游所有用户出问题）。因此它的 CI 比「普通库」重得多，大致分四类任务：

1. **常规质量门**：编译警告即失败（`-D warnings`）、clippy、文档构建、格式化（rustfmt/shfmt/taplo）、外部类型检查。
2. **多平台/多版本测试**：在 MSRV（1.74）、stable、nightly 上，跨 x86_64/arm64/windows/macos 等跑测试。
3. **特性组合**：用 `cargo-hack` 枚举「特性幂集」（所有特性开关组合），并在真实 `no_std` 目标（嵌入式芯片）上构建。
4. **并发正确性专项**：miri（未定义行为检测）、sanitizer（数据竞争/内存错误）、loom（并发状态空间模型检查）、cargo-careful。

本讲不展开 miri/loom 的原理（那是 u7-l3 的主题），只让你认得这些任务、并能找到「本地能复现的命令」。

#### 4.3.2 核心流程

CI 流水线由 [.github/workflows/ci.yml](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/.github/workflows/ci.yml) 定义，触发时机覆盖：pull_request、push 到 main、每日定时（`cron: '0 2 * * *'`）和手动触发。其中与我们本地最相关的是两个 job：

```
test       ──► ci/test.sh        （多版本多平台跑测试）
features   ──► ci/check-features.sh （枚举特性组合 + no_std 目标构建）
```

`ci/test.sh` 的本地等价命令（host 目标、全部特性）：

```
cargo test --all --all-features --exclude benchmarks -- --test-threads=1
cargo test --all --all-features --exclude benchmarks --release -- --test-threads=1
```

`ci/check-features.sh` 的本地等价命令（枚举特性幂集）：

```
cargo hack build --all --feature-powerset --no-dev-deps --exclude benchmarks
```

#### 4.3.3 源码精读

先看 CI 的全局环境与触发条件：

[ci.yml:6-13](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/.github/workflows/ci.yml#L6-L13) — 触发条件：PR、push main、每日定时（用来跑那些只在 schedule 跑的重任务）、手动。

[ci.yml:15-23](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/.github/workflows/ci.yml#L15-L23) — 关键环境变量：`RUSTFLAGS: -D warnings` 与 `RUSTDOCFLAGS: -D warnings`，即**任何警告都视为错误**。这是为什么 crossbeam 源码里几乎看不到 `warning`：CI 不允许它存在。

`test` job 的测试矩阵非常宽：

[ci.yml:45-94](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/.github/workflows/ci.yml#L45-L94) — 矩阵覆盖 `rust: 1.74 / stable / nightly`，操作系统覆盖 ubuntu（x86_64）、ubuntu-24.04-arm（arm64）、windows、macos，还交叉编译到 `i686`、`armv7`、`powerpc64le`、`s390x`、`armv5te`（用来测**没有 64 位原子**或**没有内联汇编**的目标）、`sparc64`。注释 [第 85 行](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/.github/workflows/ci.yml#L85) 明说 `armv5te` 是为了测试「没有 `AtomicU64`/`AtomicI64`」的 32 位目标——这关系到 AtomicCell 在不同架构的实现分叉（u2-l3 会讲）。

`test` job 最后调用脚本：

[ci.yml:115-116](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/.github/workflows/ci.yml#L115-L116) — `run: ci/test.sh`。

该脚本的内容就是我们本地该模仿的命令：

[ci/test.sh:7-22](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/ci/test.sh#L7-L22) — 逻辑分两段：若设置了 `RUST_TARGET`（交叉编译），用 `--target` 跑测试后即退出；否则用 host 目标跑两轮（debug + release），并在 nightly 下额外 `cargo check --all --all-features --all-targets`（含 benchmark，因 benchmark 依赖 unstable 特性，只在 nightly 检查）。注意全程 `--all-features`（把所有特性打开一起测）和 `--exclude benchmarks`（基准 crate 不发布，单独排除）。

`features` job 用 `cargo-hack` 枚举特性幂集：

[ci.yml:118-144](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/.github/workflows/ci.yml#L118-L144) — 在 `msrv` 与 `nightly` 两个版本上跑 `ci/check-features.sh`。

[ci/check-features.sh:6-29](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/ci/check-features.sh#L6-L29) — 三件事：
1. `--feature-powerset`：枚举所有特性子集（含 `--no-default-features` 和 default），保证**任意特性组合都能编译**——这是「特性门控」正确的硬保证。
2. MSRV 特殊处理（[第 9-13 行](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/ci/check-features.sh#L9-L13)）：因为 `crossbeam-utils` 的 `atomic` 特性要求 1.74，而该 crate 的 MSRV 文档值是 1.56，所以在 `--rust-version` 模式下要先用 `--exclude-features atomic` 跑一遍，再用 `cargo +1.74` 单独验证 `atomic`。
3. 在 nightly 下为真实 `no_std` 目标构建（[第 18-29 行](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/ci/check-features.sh#L18-L29)）：`thumbv7m-none-eabi`（有原子 CAS）、`thumbv6m-none-eabi`（有原子但无 CAS）、`riscv32i-unknown-none-elf`（完全没有原子）。这三种目标代表了嵌入式世界的三种原子能力档位，确保 crossbeam 在最「贫瘠」的硬件上也能 `no_std` 构建。

> 把这段和 4.2 串起来看就明白了：4.2 讲「特性怎么定义」，4.3 讲「CI 怎么保证这些特性组合都合法」。两者是「设计—验证」的关系。`--feature-powerset` 会在 `no_std` 目标上尝试所有非 `std`/`default` 的组合，于是「关掉 std 还能不能编译」这件事是被 CI 反复验证过的——你本地看到的可用 API 表格，正是这套 CI 守出来的结果。

其余并发正确性 job（本讲只认名字，原理留到 u7-l3）：`miri`（[ci.yml:201-224](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/.github/workflows/ci.yml#L201-L224)）跑未定义行为检测；`san`（[ci.yml:241-252](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/.github/workflows/ci.yml#L241-L252)）跑 Address/Thread/Memory sanitizer；`loom`（[ci.yml:255-266](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/.github/workflows/ci.yml#L255-L266)）跑 epoch 的并发模型检查；`codegen`（[ci.yml:161-198](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/.github/workflows/ci.yml#L161-L198)）自动重新生成 `no_atomic.rs`（记录哪些目标没有 64 位原子）。

#### 4.3.4 代码实践

1. **实践目标**：在本地复现 CI 的「构建 + 测试 + 特性检查」核心子集。
2. **操作步骤**：
   ```bash
   # 1) 编译整个 workspace（等价 CI 的常规构建）
   cargo build --workspace

   # 2) 只测某个子 crate（开发时最常用，比 --all 快）
   cargo test -p crossbeam-utils

   # 3) 全部特性、全 workspace 测试（接近 ci/test.sh 的 host 分支）
   cargo test --all --all-features --exclude benchmarks -- --test-threads=1

   # 4) 跑一个示例（需要 nightly 才能检查 benchmarks，但示例本身 stable 可跑）
   cargo run --example fibonacci -p crossbeam-channel
   ```
3. **需要观察的现象**：
   - 步骤 1 应无警告完成（因为 CI 用 `-D warnings`，源码本身是「零警告」的）。
   - 步骤 2 只编译并运行 `crossbeam-utils` 的单元测试，速度明显快于 `--all`。
   - 步骤 4 会启动 fibonacci 生成器示例（该示例用 `bounded(0)` 会合通道，是 u3-l1 的内容，此处只验证「示例能跑」）。
4. **预期结果**：全部命令退出码为 0；步骤 3 输出所有子 crate 的测试结果且全部通过。
5. 待本地验证（首次构建会编译大量依赖，耗时较长；若你的工具链低于 MSRV 1.74，步骤 1 会因 `rust-version` 检查而失败）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ci/test.sh` 要既跑 debug 又跑 `--release`？

> **答案**：debug 和 release 是两套不同的优化与代码生成路径，某些并发 bug（尤其与编译器优化、内联、内存重排相关的）只在 release 下暴露。两轮都跑才能覆盖更真实的部署形态。

**练习 2**：`ci/check-features.sh` 里 MSRV 分支为什么要对 `crossbeam-utils` 单独用 `--exclude-features atomic` 跑一次？

> **答案**：`crossbeam-utils` 的 MSRV 文档值是 1.56，但它的 `atomic` 特性要求 1.74。`cargo hack --rust-version` 会按 crate 的 MSRV 检查，若直接开 `atomic` 会因「1.56 不支持」而误报失败。解决办法是先排除 `atomic` 跑通 1.56 的部分，再用 `cargo +1.74` 单独验证 `atomic` 特性。这体现了「同一 crate 内不同特性可以有不同 MSRV」的工程处理。

---

## 5. 综合实践

把本讲三个模块串起来，做一个「特性侦探」小任务：

**任务**：预测并验证「在 `--no-default-features --features alloc` 下，主 crate 的依赖树与可用 API」。

1. **先预测**（不跑命令，基于 4.1 与 4.2 的源码）：
   - 依赖树里会出现哪些子 crate？（提示：看 [Cargo.toml:49](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/Cargo.toml#L49) 的 `alloc` 连带了谁。）
   - `crossbeam::channel`、`crossbeam::epoch`、`crossbeam::queue`、`crossbeam::scope`、`crossbeam::atomic` 各自是否可用？（提示：看 [src/lib.rs:68-79](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/src/lib.rs#L68-L79) 的 `#[cfg]`。）
2. **再验证**：
   ```bash
   cargo tree -p crossbeam --no-default-features --features alloc --depth 1
   cargo build -p crossbeam --no-default-features --features alloc
   ```
3. **写一段小程序**（新建一个临时 bin 或用 `cargo check`）：分别尝试 `use crossbeam::queue::SegQueue;` 和 `use crossbeam::channel::unbounded;`，记录哪一个能编译、哪一个报错，并与你的预测对照。
4. **反思**：如果你的预测与实际不符，回到 [Cargo.toml:33-49](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/Cargo.toml#L33-L49) 和 [src/lib.rs:68-79](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/src/lib.rs#L68-L79) 找出哪一处判断错了。

完成本任务后，你就真正掌握了「特性如何决定依赖树与可见 API」这条因果链——它是后续阅读任何 `#[cfg]` 门控源码的基础。

## 6. 本讲小结

- crossbeam 是 **8 个 crate 组成的 Cargo workspace**，共享一份 `Cargo.lock` 和一组 lint；成员间用「`path` + `version`」双重声明的依赖互相引用，且一律先 `default-features = false` 再按需开特性。
- 主 crate 把 5 个子 crate 作为依赖：`crossbeam-utils` **必选**（始终带 `atomic`），channel/deque/epoch/queue **可选**（由特性开启）；`crossbeam-skiplist` 只是 workspace 成员，不是主 crate 依赖。
- 特性采用「default → std → alloc」连带链：`std` 自动带 `alloc` 并点亮全部子 crate 的 `std`；`alloc` 只带 epoch/queue。特性会**跨 crate 层层传递**（如 `crossbeam/std → crossbeam-epoch/std → crossbeam-utils/std`）。
- 门面 `src/lib.rs` 用 `#[cfg(feature)]` 把特性兑现成可见模块：`atomic`/`utils` 永远可用，`epoch`/`queue` 需 `alloc`，`channel`/`deque`/`scope`/`sync` 需 `std`。
- CI 用 `-D warnings` 把警告当错误，并用宽矩阵（MSRV/stable/nightly × 多 OS/架构）+ `cargo-hack --feature-powerset` + 真实 `no_std` 目标（thumbv7m/thumbv6m/riscv32i）来保证「任何特性组合、任何档位硬件都能编译」。
- 本地最常用的命令是 `cargo build --workspace`、`cargo test -p <子 crate>`，与 CI 等价的全量命令是 `cargo test --all --all-features --exclude benchmarks`。

## 7. 下一步学习建议

- **下一讲 [u1-l3](u1-l3-reexport-facade.md)**：精读 `src/lib.rs`，看门面如何用 `pub use` 与 `#[cfg(feature)]` 把上一讲说的「facade 重导出」落地——本讲只引用了它的 4 行 `#[cfg]`，下一讲会逐行拆解。
- **动手预热**：在做完第 5 节综合实践后，尝试 `cargo doc -p crossbeam --open`，对照文档里出现的模块与本讲「特性→可用 API」的表格，加深印象。
- **后续衔接**：等到 u2（crossbeam-utils）和 u3（crossbeam-channel）时，你会反复回到本讲的特性图——例如理解为何 `Parker`/`ShardedLock` 只在 `std` 下可用，而 `Backoff`/`CachePadded`/`AtomicCell` 在 `no_std` 下也能用。
