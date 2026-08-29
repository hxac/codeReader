# 构建、运行与代码结构

## 1. 本讲目标

上一讲（u1-l1）我们通过 README 和 Cargo.toml 认识了 chili 的定位：一个运行时零第三方依赖的低开销 fork-join 并行原语库。本讲我们第一次真正「住进」这个仓库，学会三件事：

1. 用 `cargo check` / `cargo test` / `cargo clippy` / `cargo bench` 完整地构建和验证这个项目，建立自己的快速反馈环；
2. 看懂项目的目录组织，说清楚 `src/lib.rs`（公共 API 与调度）和 `src/job.rs`（任务与通道）各自的分工；
3. 读懂 [.github/workflows/ci.yml](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/.github/workflows/ci.yml) 中五个 CI 作业，特别是 miri 与 nextest 这两个相对少见的步骤各自在守护什么。

学完本讲，你应该可以在本地把 chili 完整验证一遍，并且知道每次改动之后该跑哪条命令、CI 会在哪些地方替你把关。

## 2. 前置知识

- **cargo 与 crate**：cargo 是 Rust 的构建系统兼包管理器；一个 Rust 包叫一个 **crate**，其元信息写在 `Cargo.toml` 里，依赖的确切版本锁定在 `Cargo.lock` 里。
- **编译目标（target）**：一个 crate 可以有多种产物——库（lib）、二进制（bin）、测试（test）、基准（bench）。chili 只有一个库目标和一个基准目标，**没有**二进制目标：它是供别人 `use` 的库，不是可执行程序。
- **dev-dependencies**：只在编译本 crate 自己的测试、基准、示例时才引入的依赖。上一讲已经确认：chili 的 `[dependencies]` 段整个不存在（运行时零依赖），`divan`（基准框架）和 `rayon`（对照组）都只是 dev 依赖。
- **文档测试（doctest）**：Rust 会把文档注释里的 \`\`\`rust 代码块当作真正的测试来编译运行。chili 的公共 API 文档全部带可运行示例，这些示例都会被 `cargo test --doc` 执行。
- **CI（持续集成）**：每次 push 或提交 PR 时，GitHub Actions 按 `.github/workflows/` 下的 YAML 文件自动执行一套检查，防止有问题的代码合入 `main`。
- **miri**：Rust 官方的一个「解释器」。它不把代码编译成机器码，而是逐条解释执行程序的中间表示（MIR），在此过程中检查未定义行为（Undefined Behavior，简称 UB）——比如越界访问、use-after-free、数据竞争。对 chili 这种大量使用 `unsafe` 的并发库，miri 是最重要的安全网。本讲只需要知道它的角色，具体命令在 4.3 节讲解。
- **nextest**：社区流行的第三方测试运行器（`cargo test` 的替代前端），把每个测试放进独立进程运行，并行度更好、输出更清晰，并且支持用过滤表达式（`-E`）挑选要跑的测试。注意：nextest **不运行文档测试**，文档测试仍由标准 `cargo test --doc` 负责。

## 3. 本讲源码地图

整个仓库非常小，被 git 跟踪的源码与配置一共只有这些（另有 `README.md`、双许可证文件 `LICENSE-MIT` / `LICENSE-APACHE`、`.gitignore`）：

| 文件 | 作用 |
| --- | --- |
| [Cargo.toml](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/Cargo.toml) | 项目清单：包名、版本、edition、dev 依赖、基准目标声明 |
| [src/lib.rs](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs) | 库入口：crate 级文档与 lint、`ThreadPool`、`Scope` 与 `join` 系列调度逻辑、全部单元测试 |
| [src/job.rs](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs) | 私有子模块：单值通道 `Channel`/`Sender`/`Receiver`、任务三件套 `JobStack`/`Job`/`JobShared`、本地任务队列 `JobQueue` |
| [benches/overhead.rs](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/benches/overhead.rs) | divan 基准：`no_overhead` / `chili_overhead` / `rayon_overhead` 三组对照实验，README 性能数据的来源 |
| [.github/workflows/ci.yml](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/.github/workflows/ci.yml) | CI 流水线：check / test / fmt / clippy / miri 五个并行作业 |

仓库里还提交了 `Cargo.lock`——它锁定 dev 依赖（divan、rayon 及其传递依赖）的确切版本，让 CI 与本地构建可复现。项目没有 `tests/`（集成测试目录）、没有 `examples/`、没有 `src/main.rs`，也没有 `rust-toolchain.toml`（工具链版本由 CI 的 action 参数固定，见 4.3 节）。

## 4. 核心概念与源码讲解

本讲的三个最小模块：**构建与测试命令**、**目录结构与入口文件**、**CI 流水线**。

### 4.1 构建与测试命令

#### 4.1.1 概念说明

cargo 的常用子命令各司其职，反馈速度从快到慢：

| 命令 | 做什么 | 产出 |
| --- | --- | --- |
| `cargo check --all` | 只做类型检查与借用检查，不生成机器码 | 无二进制，最快 |
| `cargo clippy --all` | 类型检查 + 一大批惯用法 lint | 警告（或错误）列表 |
| `cargo test --all` | 编译并运行单元测试、文档测试 | 测试结果 |
| `cargo test --doc` | 只运行文档测试 | 测试结果 |
| `cargo bench` | 编译并运行 `[[bench]]` 声明的基准 | 性能数据 |

`--all` 是 `--workspace` 的别名（对本项目而言工作区只有 chili 一个成员）。日常开发的节奏是：改代码 → `check` 快速确认能编译 → `clippy` 挑毛病 → 提交前 `test` 全量验证 → 需要看性能时才 `bench`。

一个值得注意的细节：chili 没有运行时依赖，所以 `cargo check --all` 连一个第三方 crate 都不用编译；`divan` 和 `rayon` 要到编译测试/基准目标时（`cargo test`、`cargo bench`）才会被拉进来。

#### 4.1.2 核心流程

一条 cargo 命令的执行过程可以概括为：

```text
读取 Cargo.toml
    │  （解析 [package]、[dev-dependencies]、[[bench]]）
    ▼
依赖解析 ──► 依据 Cargo.lock 锁定 divan 0.1.x、rayon 1.10.x 的确切版本
    ▼
按目标编译：src/lib.rs（lib 目标）+ benches/overhead.rs（bench 目标）
    ▼
执行子命令对应的动作
    （check 到类型检查为止 / test 运行测试 / bench 运行基准）
```

#### 4.1.3 源码精读

**（1）包清单与基准声明。** [Cargo.toml:L1-L12](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/Cargo.toml#L1-L12) 声明了包名 `chili`、描述 `low-overhead parallelization library`、版本 `0.2.1`、`edition = "2021"`、双许可 `MIT OR Apache-2.0`。注意两件事：没有 `[dependencies]` 段（运行时零依赖，上一讲已确认），也没有 `rust-version` 字段（未声明最低工具链版本）。

[Cargo.toml:L14-L20](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/Cargo.toml#L14-L20) 声明了两个 dev 依赖和一个基准目标：

```toml
[dev-dependencies]
divan = "0.1.14"
rayon = "1.10.0"

[[bench]]
name = "overhead"
harness = false
```

- `divan` 与 `rayon` 只在编译测试和基准时引入——使用 chili 的项目不会因此多出任何传递依赖；
- `harness = false` 的意思是：`benches/overhead.rs` **不用** libtest 标准测试骨架，自带入口。看 [benches/overhead.rs:L93-L95](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/benches/overhead.rs#L93-L95)，文件底部有自己写的 `fn main() { divan::main(); }`——divan 用 `#[divan::bench]` 宏标注基准函数（如 [benches/overhead.rs:L25-L26](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/benches/overhead.rs#L25-L26) 的 `no_overhead`）、由自己的 main 接管运行。若不写 `harness = false`，cargo 会按标准 `#[bench]` 骨架去编译它而出错。

**（2）crate 级 lint 三连。** 打开库入口的第一行就是 [src/lib.rs:L1-L3](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L1-L3)：

```rust
#![deny(missing_docs)]
#![deny(unsafe_op_in_unsafe_fn)]
#![deny(clippy::undocumented_unsafe_blocks)]
```

这三行是全局的「自我约束」，直接决定了这个代码库的样子：

- `missing_docs`：所有公共项必须有文档注释，缺了就**编译失败**——所以 docs.rs 上 chili 的每个 API 都有说明和可运行示例；
- `unsafe_op_in_unsafe_fn`：即使在 `unsafe fn` 内部，危险操作也必须再套一层显式的 `unsafe { }` 块；
- `clippy::undocumented_unsafe_blocks`：每个 `unsafe { }` 块上方必须有 `// SAFETY:` 注释说明为什么安全。

后两条合起来解释了你在源码里到处看到 `// SAFETY:` 的原因——那不是点缀，而是不写就过不了编译的硬性要求。第 4 单元的 unsafe 审读讲座将会大量受益于这些注释。

#### 4.1.4 代码实践

**实践目标**：建立本地快速反馈环，确认仓库在稳定版工具链上可以编译、无 clippy 警告，并体会增量编译缓存。

**操作步骤**（前提：已安装 rustup 与某个稳定版工具链）：

1. 在仓库根目录执行 `cargo check --all`，用 shell 的 `time` 感受耗时；
2. 再执行一次 `cargo check --all`；
3. 执行 `cargo clippy --all`。

**需要观察的现象**：

- 两次 `check` 都应以 `Finished` 结束且没有 error；第二次因为命中增量编译缓存而明显快于第一次（chili 没有第三方依赖可编译，两次的差异全部来自 chili 自身两个源文件的增量编译）；
- `clippy` 预期零警告——仓库自身在第 3 行 deny 了 `clippy::undocumented_unsafe_blocks`，这一类问题会直接以 error 形式出现。

**预期结果**：三条命令全部干净结束。具体耗时与你的机器有关，**待本地验证**（本讲义没有替你执行命令，一切输出以本地为准）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `divan` 和 `rayon` 不会成为 chili 使用者的传递依赖？

> **答案**：它们写在 `[dev-dependencies]` 里，只在编译本 crate 自己的测试、基准、示例时生效；库目标（lib）的依赖列表为空，所以下游项目的依赖树里不会出现它们。

**练习 2**：把 `harness = false` 删掉再运行 `cargo bench`，会发生什么？为什么？

> **答案**：cargo 会把 `benches/overhead.rs` 当作使用 libtest 标准骨架的基准来编译，而该文件里是 divan 的 `#[divan::bench]` 函数和自己的 `fn main()`，不满足标准骨架的约定，编译会失败。divan 的设计要求关闭标准 harness、由它自己接管入口。

**练习 3**：`cargo check` 与 `cargo build` 的本质区别是什么？

> **答案**：`check` 只进行到类型检查与借用检查（不生成机器码、不链接），因此快得多，适合写代码时的高频验证；`build` 会产出真正的 rlib / 可执行文件。

### 4.2 目录结构与入口文件

#### 4.2.1 概念说明

Rust 库 crate 的默认入口是 `src/lib.rs`。在入口文件里用 `mod job;` 声明子模块后，编译器会去找 `src/job.rs`（或 `src/job/mod.rs`）作为该模块的实现——这就是整个项目只有两个源文件的原因。

两个文件的分工可以一句话概括：

- **[src/lib.rs](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs) 是「门面 + 调度」**：对外暴露的全部公共 API（`Scope`、`ThreadPool`、`Config`）、线程池的创建与销毁（[src/lib.rs:L513-L546](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L513-L546)）、worker 线程和心跳线程的两个循环（`execute_worker` / `execute_heartbeat`）、`join` 的路径选择逻辑，全部在这一个文件里；单元测试也内嵌在文件末尾。
- **[src/job.rs](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs) 是「任务数据结构 + 通道」**：描述「一个任务长什么样、怎么在队列里排队、怎么跨线程传递、结果怎么送回来」，全部是私有模块内部的实现细节，使用者不可见。

#### 4.2.2 核心流程

两个文件之间的依赖关系与一次 `join` 的大致调用链如下（本讲只需建立地图，每个环节的语义由后续讲座展开）：

```text
用户代码
   │  Scope::global() / ThreadPool::scope() / scope.join(a, b)
   ▼
┌──────────────── src/lib.rs ────────────────┐
│  Config / ThreadPool（线程池生命周期）      │
│  Scope + join / join_seq / join_heartbeat  │
│  execute_worker / execute_heartbeat        │
│    │ mod job; + use job::{...}            │
└────┼───────────────────────────────────────┘
     ▼
┌──────────────── src/job.rs ────────────────┐
│  JobStack（存放闭包，只允许取一次）         │
│  Job（本地队列条目）/ JobShared（跨线程）   │
│  JobQueue（每线程的本地双端队列）           │
│  Channel + Sender + Receiver（结果通道）    │
└────────────────────────────────────────────┘
```

#### 4.2.3 源码精读

**（1）模块声明与导入。** [src/lib.rs:L63-L65](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L63-L65)：

```rust
mod job;

use job::{Job, JobQueue, JobShared, JobStack, Receiver};
```

`mod job;` 没有加 `pub`，所以这是一个**私有模块**——即使 `src/job.rs` 内部写着 `pub struct Receiver`（[src/job.rs:L45-L46](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L45-L46)），这个 `pub` 的可见范围也止步于模块边界，chili 的使用者完全看不到它。`Receiver` 被 lib.rs 用在 `wait_for_sent_job` 的签名里（[src/lib.rs:L284-L284](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L284-L284)）——这是两个文件协作的一个具体交点。

**（2）极小的公共 API 面。** 顶层的 `pub` 项只有 3 个类型，全在 lib.rs；加上它们 `impl` 块里的 `pub fn` 方法一共 8 个，外加 `Config` 的 2 个公共字段：

| 公共项 | 位置 | 一句话职责 |
| --- | --- | --- |
| `Scope` | [src/lib.rs:L234-L239](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L234-L239) | 承载 fork-join 工作负载的作用域对象 |
| `Scope::global` | [src/lib.rs:L250-L252](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L250-L252) | 取全局线程池派生的 `'static` 作用域 |
| `Scope::join` | [src/lib.rs:L409-L417](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L409-L417) | 可能并行地运行两个闭包并汇合结果 |
| `Scope::join_with_heartbeat_every` | [src/lib.rs:L438-L456](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L438-L456) | 同上，但允许自定义心跳检查频率 |
| `Config`（含 2 个公共字段） | [src/lib.rs:L461-L467](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L461-L467) | 线程数与心跳间隔两个配置项 |
| `ThreadPool` | [src/lib.rs:L482-L486](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L482-L486) | 线程池本体 |
| `ThreadPool::new` / `with_config` | [src/lib.rs:L497-L546](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L497-L546) | 用默认/自定义配置创建线程池 |
| `ThreadPool::set_global` / `global` / `scope` | [src/lib.rs:L565-L606](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L565-L606) | 设置/获取全局池、派生 `Scope` |

一个并行库对外只有 3 个类型、8 个方法，这是典型的「小 API 面」设计：复杂度全部被压进私有模块 `job`。

**（3）文档即测试。** [src/lib.rs:L18-L48](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L18-L48) 是 crate 级文档里的二叉树并行求和示例，结尾断言 `assert_eq!(sum(&tree, &mut Scope::global()), 1023);`。因为 `missing_docs` 被设为 deny，每个公共项都带这样的示例（例如 `join` 的示例在 [src/lib.rs:L400-L408](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L400-L408)）。数一数全文件的文档代码块，一共 **10 个**：crate 级 1 个 + `Scope`、`Scope::global`、`join`、`join_with_heartbeat_every`、`ThreadPool::new`、`with_config`、`set_global`、`global`、`scope` 各 1 个。这 10 段示例全部会被 `cargo test --doc` 当作测试执行——文档永远不会悄悄失效。

**（4）内嵌的测试模块。** [src/lib.rs:L635-L833](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L635-L833) 是 `#[cfg(test)] mod tests`，共 **8 个**单元测试，只在测试编译时存在，不进入发布产物：

| 测试 | 位置 | 验证什么 |
| --- | --- | --- |
| `thread_pool_stops` | [src/lib.rs:L644-L646](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L644-L646) | 默认线程池能创建并正常销毁 |
| `thread_pool_with_one_thread` | [src/lib.rs:L649-L654](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L649-L654) | 单线程配置下线程池也能工作 |
| `join_basic` | [src/lib.rs:L657-L667](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L657-L667) | 两个闭包各执行一次、结果正确 |
| `join_long` | [src/lib.rs:L670-L690](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L670-L690) | 1024 个元素逐个 fork-join 递归展开后全部被处理 |
| `join_very_long` | [src/lib.rs:L693-L714](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L693-L714) | 1024×1024 个元素二分递归展开后全部被处理（全库最重的测试） |
| `join_wait` | [src/lib.rs:L717-L747](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L717-L747) | 2 线程 + 1µs 心跳 + 每次 join 都检查心跳时，任务真正跨线程执行 |
| `join_panic` | [src/lib.rs:L750-L805](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L750-L805) | worker 线程里的 panic 能传回原线程（`#[should_panic]`） |
| `concurrent_scopes` | [src/lib.rs:L808-L832](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L808-L832) | 128 个外部线程同时在同一个线程池上开 Scope 并 join |

**（5）job.rs 的内部布局。** [src/job.rs](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs) 从上到下依次是：通道三态 `State` 与 `Channel`（[L17-L33](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L17-L33)）、接收端 `Receiver::recv`（[L53-L81](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L53-L81)）与发送端 `Sender::send`（[L88-L105](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L88-L105)）、存放闭包的 `JobStack`（[L113-L134](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L113-L134)）、本地队列条目 `Job` 与它的类型擦除 `harness`（[L140-L188](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L140-L188)）、可跨线程发送的 `JobShared`（[L207-L234](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L207-L234)）、双端队列 `JobQueue`（[L236-L275](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L236-L275)）。本讲只需记住名字和大致位置，第三单元的两篇讲座会逐个精读。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：亲手验证「公共 API 面只有 3 个顶层类型 + 8 个方法」，并跟踪一条贯穿两个文件的调用链。

**操作步骤**：

1. 在仓库根目录执行 `grep -n "^pub " src/lib.rs`。因为方法都缩进 4 个空格，`^pub ` 只会匹配**顶层** `pub` 项；
2. 再执行 `grep -n "    pub fn" src/lib.rs`（4 空格缩进），列出全部公共方法，与 4.2.3（2）的表格对照；
3. 对 `src/job.rs` 执行 `grep -n "pub " src/job.rs`，确认它虽然满是 `pub`，但因为 `mod job;` 是私有模块，这些名字不会出现在 chili 使用者的视野里；
4. 阅读调用链 `join`（[L409](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L409-L417)）→ `join_with_heartbeat_every::<64>`（[L438](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L438-L456)）→ 按条件走 `join_seq` 或 `join_heartbeat` → `join_heartbeat` 内的 `JobStack::new(a)` / `Job::new(&stack)`（[src/lib.rs:L355-L356](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L355-L356)）→ job.rs 的 `JobQueue::push_back`（[src/job.rs:L247-L249](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L247-L249)），在纸上抄下每一步的文件名与行号。

**需要观察的现象**：

- 第 1 步的输出应当**恰好 3 行**：`pub struct Scope`（L234）、`pub struct Config`（L461）、`pub struct ThreadPool`（L482）；
- 第 2 步的输出应当是 8 个方法：`global`、`join`、`join_with_heartbeat_every`、`new`、`with_config`、`set_global`、`global`、`scope`；
- 第 4 步你会看到 `join` 的默认实现只有一行转发：`self.join_with_heartbeat_every::<64, _, _, _, _>(a, b)`（[src/lib.rs:L416-L416](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L416-L416)），而路径选择的条件在 [src/lib.rs:L449-L455](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L449-L455)——本讲不展开语义，只要求找到位置。

**预期结果**：得到一张与 4.2.3（2）一致的 API 清单和一条手抄调用链。若 `grep` 输出与预期不符，先检查匹配模式是否把多行声明截断了。具体输出**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`src/job.rs` 里的 `pub struct Receiver` 为什么不会出现在 chili 使用者的编译视野里？

> **答案**：可见性受模块边界限制。`mod job;` 本身是私有模块，其中的 `pub` 项最多对父模块（crate 根）可见；lib.rs 只 `use job::{...}` 而没有 `pub use job::Receiver` 重导出，所以对外完全隐藏。

**练习 2**：如果想让外部用户复用 chili 的 `Channel`，至少要做哪两件事？

> **答案**：一是改可见性——把 [src/lib.rs:L63-L63](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L63-L63) 的 `mod job;` 改成 `pub mod job;`，或在 crate 根加 `pub use job::Channel;` 重导出；二是为对外暴露的项补齐文档注释——crate 开了 `#![deny(missing_docs)]`，缺文档会直接编译失败。

**练习 3**：单元测试为什么写成 `#[cfg(test)] mod tests` 内嵌在 lib.rs 里，而不是独立的 `tests/` 集成测试目录？

> **答案**：内嵌 `#[cfg(test)]` 模块可以测试**私有项**（`use super::*;` 直接拿到父模块的一切），并且条件编译保证测试代码完全不进入正常构建；`tests/` 目录下的集成测试只能通过公共 API 测试。chili 的公共 API 只有 3 个类型，大量逻辑是私有实现，内嵌单元测试是最合适的形式。

### 4.3 CI 流水线

#### 4.3.1 概念说明

[.github/workflows/ci.yml](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/.github/workflows/ci.yml) 定义了五个彼此独立的作业（job），在 push 到 `main` 或提交 PR 时**并行**运行：

| job | 本地等价命令 | 守护什么 |
| --- | --- | --- |
| Check | `cargo check --all` | 代码能通过类型/借用检查 |
| Test | `cargo test --all` | 单元测试 + 文档测试全部通过 |
| Rustfmt | `cargo fmt --all -- --check` | 代码风格统一（`--check` 只报告不修改） |
| Clippy | `cargo clippy --all` | 无 clippy 警告 |
| Miri | 见 4.3.3（2） | 无未定义行为（UB） |

前四个 job 用固定版本的稳定工具链（action `dtolnay/rust-toolchain@1.81.0`，固定版本保证 CI 可复现），只有 Miri job 用 nightly——因为 miri 组件只随 nightly 发布。每个 job 都先 `actions/checkout@v4` 拉代码，再用 `Swatinem/rust-cache@v2` 缓存编译产物。

#### 4.3.2 核心流程

```text
push 到 main / 提交 PR
        │
        ├──► Check   （stable 1.81.0）cargo check --all
        ├──► Test    （stable 1.81.0）cargo test --all
        ├──► Rustfmt （stable 1.81.0）cargo fmt --all -- --check
        ├──► Clippy  （stable 1.81.0）cargo clippy --all
        └──► Miri    （nightly + miri 组件 + nextest）
                ├─ cargo +nightly miri setup
                ├─ cargo +nightly miri nextest run -j8 -E 'not (test(join_very_long))'
                └─ MIRIFLAGS=-Zmiri-many-seeds cargo +nightly miri test --lib -- join_wait
```

#### 4.3.3 源码精读

**（1）常规四连。** Check、Test、Rustfmt、Clippy 四个 job 分别位于 [.github/workflows/ci.yml:L10-L17](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/.github/workflows/ci.yml#L10-L17)、[L19-L26](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/.github/workflows/ci.yml#L19-L26)、[L28-L36](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/.github/workflows/ci.yml#L28-L36)、[L38-L47](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/.github/workflows/ci.yml#L38-L47)，与本地命令一一对应。唯一的差别是 fmt 带了 `-- --check`：把「自动格式化」变成「只检查」——CI 里当然不能改动代码。其中 Rustfmt 和 Clippy job 还通过 `components:` 参数额外安装了 rustfmt / clippy 组件（[L34-L35](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/.github/workflows/ci.yml#L34-L35)、[L44-L45](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/.github/workflows/ci.yml#L44-L45)）。

**（2）Miri job 的两条命令。** Miri job 位于 [.github/workflows/ci.yml:L49-L61](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/.github/workflows/ci.yml#L49-L61)：用 nightly 工具链加 miri 组件（[L54-L56](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/.github/workflows/ci.yml#L54-L56)）、通过 `taiki-e/install-action@nextest` 装 nextest（[L58](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/.github/workflows/ci.yml#L58-L58)）、先 `miri setup` 预取组件（[L59](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/.github/workflows/ci.yml#L59-L59)），然后跑两条核心命令。

第一条（[L60](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/.github/workflows/ci.yml#L60-L60)）：

```bash
cargo +nightly miri nextest run -j8 -E 'not (test(join_very_long))'
```

- `cargo +nightly miri` 用 nightly 的 miri 解释执行测试；
- `nextest run` 借助刚装的 nextest 运行，`-j8` 是 8 路并行；
- `-E 'not (test(join_very_long))'` 是 nextest 的过滤表达式：**排除** `join_very_long`。理由很实际：miri 解释执行比原生慢一到两个数量级，而 `join_very_long`（[src/lib.rs:L693-L714](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L693-L714)）要对 1024×1024 个元素做上百万次 join，在 miri 下代价过高；
- 因此这条命令覆盖的是其余 **7 个单元测试**。注意 nextest 不运行文档测试，所以那 10 个 doctest 只在稳定版的 Test job 里被执行。

第二条（[L61](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/.github/workflows/ci.yml#L61-L61)）：

```bash
MIRIFLAGS=-Zmiri-many-seeds cargo +nightly miri test --lib -- join_wait
```

- 只针对 `join_wait`（[src/lib.rs:L717-L747](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L717-L747)）这一个测试。它配置了 2 个 worker、1µs 心跳间隔、`join_with_heartbeat_every::<1>`（每次 join 都检查心跳），是**跨线程任务共享路径**压力最大的测试；
- `-Zmiri-many-seeds` 让 miri 用多个不同的随机种子反复运行同一段代码，覆盖不同的线程调度交错——并发 bug 往往只在特定执行顺序下暴露，单次通过不代表没有问题；
- 这里改用 miri 内置的 `miri test`（而不是 nextest）来配合 many-seeds 模式。

**（3）为什么 miri 对 chili 特别重要。** 全库的 unsafe 规模是：**15 个 `unsafe { }` 块**（lib.rs 5 个 + job.rs 10 个）、**4 个 `unsafe fn`**（`JobStack::take_once`、`harness`、`JobShared::execute`、`JobQueue::push_back`）和 **1 个 `unsafe impl Send`**（[src/job.rs:L227-L234](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L227-L234)），合计 20 处，涉及 `NonNull` 裸指针、`mem::transmute`、`UnsafeCell`。普通测试只能验证「跑过的路径没出错」，miri 则能检测这些 unsafe 是否引入了 UB（悬垂指针、数据竞争、无效内存访问等）。可以说：前四个 job 守护「功能正确」，miri job 守护「内存与并发安全」。

#### 4.3.4 代码实践

**实践目标**：在本地复现 CI Test job 的内容，并核对测试数量。

**操作步骤**：

1. 运行 `cargo test --all`。观察输出中「running 8 tests」的单元测试段落（lib.rs 的 tests 模块）和随后的「Doc-tests chili」段落；
2. 运行 `cargo test --doc`，核对文档测试数量是否为 10（按 4.2.3（3）的统计：crate 级 1 个 + 9 个公共项各 1 个）；
3. 对照 [ci.yml:L19-L26](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/.github/workflows/ci.yml#L19-L26)：CI 的 Test job 跑的就是你第 1 步的命令；
4. （可选，需要 nightly）`rustup toolchain install nightly && cargo +nightly miri setup` 之后，运行 `cargo +nightly miri test --lib -- join_wait`，体验 CI 第 61 行的本地版。

**需要观察的现象**：

- 第 1 步：`join_panic` 是 `#[should_panic(expected = "panicked across threads")]` 测试，输出里它显示为 passed；单元测试段与 Doc-tests 段分别汇总；
- 第 2 步：doctest 数量与手工统计一致（若不一致，回到 4.2.3（3）找找漏数了哪个示例）；
- 第 4 步：miri 运行明显慢于普通测试，这是解释执行的正常代价。

**预期结果**：8 个单元测试 + 10 个文档测试全部通过；`join_wait` 在 miri 下运行时间以秒计。具体数字与耗时**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 Miri job 使用 nightly，而其余四个 job 使用稳定版 1.81.0？

> **答案**：miri 作为组件只随 nightly 工具链发布（CI 里 `dtolnay/rust-toolchain@nightly` 并带 `components: miri`）；常规检查不需要不稳定特性，固定到一个稳定版本（1.81.0）既足够新又能保证 CI 结果可复现。

**练习 2**：`-E 'not (test(join_very_long))'` 中为什么恰好排除 `join_very_long`，而不是别的测试？

> **答案**：它是全库最重的测试——对 1024×1024 个元素递归 join（约百万次调用）。miri 解释执行有一到两个数量级的减速，跑完它代价过高；其余测试规模都在 KB/µs 级，可以在 miri 下接受。

**练习 3**：`MIRIFLAGS=-Zmiri-many-seeds` 解决什么问题？为什么只对 `join_wait` 用？

> **答案**：并发程序的行为依赖线程调度交错，单次运行可能恰好踩不到出问题的顺序；many-seeds 用多个随机种子重复执行来提高覆盖。它会让运行时间成倍增加，所以 CI 只对最依赖跨线程交错路径的 `join_wait`（2 线程、1µs 心跳、每次 join 都检查心跳）启用。

## 5. 综合实践

**任务：在你的机器上完整模拟一次 CI（本地命令 ↔ CI job 对照）。**

1. 依次执行下面四条命令，逐条记录结果（成功/失败、关键输出行、耗时）：

   ```bash
   cargo check --all
   cargo test --all
   cargo clippy --all
   cargo test --doc
   ```

2. 把结果整理成一张对照表：

   | 本地命令 | 对应 CI job（ci.yml 行号） | 本地结果 | 耗时 |
   | --- | --- | --- | --- |
   | `cargo check --all` | Check（[L10-L17](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/.github/workflows/ci.yml#L10-L17)） |  |  |
   | `cargo test --all` | Test（[L19-L26](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/.github/workflows/ci.yml#L19-L26)），`--doc` 是它的子集 |  |  |
   | `cargo clippy --all` | Clippy（[L38-L47](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/.github/workflows/ci.yml#L38-L47)） |  |  |
   | `cargo test --doc` | Test 的子集 |  |  |

3. 补充运行 `cargo fmt --all -- --check`（对应 Rustfmt job），如果报告差异，用 `cargo fmt --all` 修复后重新检查（注意不要顺手提交无关改动）；
4. 用两三句话回答：做完以上步骤，本地验证与 CI 的差别还剩什么？（提示：工具链版本是否固定、miri 是否运行、CI 在干净环境从零编译。）

**预期结果**：四条命令全部通过（8 个单元测试 + 10 个文档测试）；你会发现本地跑完这几条，就覆盖了 CI 五个 job 中的四个——miri 是本地默认缺失的唯一一环。具体输出与耗时**待本地验证**。

## 6. 本讲小结

- chili 是单库 crate：入口 [src/lib.rs](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs)（公共 API 与调度）+ 私有子模块 [src/job.rs](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs)（任务与通道），外加一个 divan 基准文件；顶层公共项只有 `Scope`、`Config`、`ThreadPool` 三个类型加 8 个方法。
- crate 级三条 `#![deny]` lint（[src/lib.rs:L1-L3](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L1-L3)）强制了「公共项必有文档、unsafe 块必带 SAFETY 注释」的代码风格。
- 日常验证环：`cargo check --all`（最快反馈）→ `cargo clippy --all` → `cargo test --all`（8 个单元测试 + 10 个文档测试）→ `cargo bench`（divan 基准，`harness = false`）。
- CI 有五个并行 job：Check / Test / Rustfmt / Clippy 用稳定版 1.81.0，与本地命令一一对应；Miri 用 nightly 守护 unsafe 代码的内存与并发安全。
- miri job 两条命令各司其职：nextest 批量跑除最重的 `join_very_long` 外的 7 个单元测试（文档测试不在其内）；`-Zmiri-many-seeds` 对跨线程压力最大的 `join_wait` 用多种调度种子反复验证。

## 7. 下一步学习建议

下一讲（u1-l3「写下第一个并行程序：Scope 与 join」）我们将第一次**使用**这个库：基于 [src/lib.rs:L18-L48](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L18-L48) 的文档示例，动手写二叉树并行求和，建立对 fork-join 模型的直觉。

在进入下一讲之前，建议先做一件事巩固本讲：把 [src/lib.rs:L635-L833](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L635-L833) 的 8 个测试通读一遍——它们是 chili 自带的最小可运行示例集，下一讲的实践会直接复用其中的写法。后续想深入了解 miri，可以回到 4.3 节的两条命令，配合 Rust 官方文档的 miri 章节练习。
