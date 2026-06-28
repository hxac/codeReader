# 构建、运行与测试：cargo、features 与示例脚本

## 1. 本讲目标

在 [u1-l1](u1-l1-project-overview.md) 里我们已经认识了 ringbuf 是什么、有哪些 crate。本讲要解决一个更实际的问题：**拿到这份源码后，我该怎么编译、运行、测试它？**

读完本讲，你应当能够：

- 说出 ringbuf 核心 crate 的 6 个 feature（`std`/`alloc`/`portable-atomic`/`bench`/`test_local`/`default`）各自的作用与依赖关系，并能用不同 feature 组合编译。
- 理解 `examples/` 下的示例如何通过 `required-features` 被 cargo 「门控」，并能解释为什么有些 example 在 `--no-default-features` 下会被跳过。
- 看懂 `scripts/` 下的三个脚本（`test.sh`/`miri.sh`/`bench.sh`）以及 `.github/workflows/test.yml` 做了什么，自己能动手运行它们。

本讲是最「动手」的一篇——所有概念都配合具体命令，并标注哪些输出需要你**在本地实际验证**。

---

## 2. 前置知识

### 2.1 什么是 feature flag

Rust 项目里，不同部署环境需要的代码并不一样：桌面程序要用 `std` 标准库；嵌入式单片机可能连操作系统都没有（`no_std`）；有的小芯片甚至没有 64 位原子指令（需要 `portable-atomic` 替代）。

Cargo 用 **feature flag（特性开关）** 解决这件事：在 `Cargo.toml` 里声明若干 feature，源码里用 `#[cfg(feature = "...")]` 控制某段代码是否参与编译。这样**同一份源码能编译出能力不同的多个版本**。

打开/关闭 feature 的常见方式：

| 命令 | 含义 |
|------|------|
| `cargo build` | 启用 `default` 特性 |
| `cargo build --no-default-features` | 关闭 `default`，从「零特性」开始 |
| `cargo build --features alloc` | 在默认之外再加 `alloc` |
| `cargo build --no-default-features --features alloc` | 只开 `alloc`，不开 `std` |

### 2.2 什么是 example、test、miri、bench

- **example**：放在 `examples/` 目录下、带 `fn main()` 的小程序，用 `cargo run --example 名字` 运行，用来演示用法。
- **单元测试**：源码里 `#[test]` 标注的函数，用 `cargo test` 运行。
- **miri**：Rust 的一个工具，会解释执行程序并检查 `unsafe` 代码里是否存在**未定义行为（UB）**。它只能跑在 nightly 工具链上。
- **bench**：性能基准测试，用 `cargo +nightly bench` 运行，测量某段代码的耗时。

> 术语提示：`stable`/`nightly` 是 Rust 的两种工具链。`nightly` 含实验性功能（miri、bench 都需要它）。`cargo +nightly xxx` 表示「用 nightly 工具链来执行 xxx」。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `Cargo.toml` | 核心 crate 的清单：声明 feature、依赖、example 的门控规则 |
| `examples/*.rs` | 6 个示例程序，分别演示不同用法与环境 |
| `scripts/test.sh` | 综合测试脚本：覆盖三个 crate + `thumbv6m` 嵌入式目标 |
| `scripts/miri.sh` | 用 Miri 检查 `unsafe` 代码的内存安全 |
| `scripts/bench.sh` | 跑性能基准测试 |
| `.github/workflows/test.yml` | CI 配置：每次 push/PR 自动跑 `test.sh` |

> 本讲主要分析**核心 crate**（仓库根目录）。`scripts/test.sh` 和 `bench.sh` 还会 `cd` 进 `async`、`blocking` 两个派生 crate，我们会在「源码精读」里一并说明。

---

## 4. 核心概念与源码讲解

### 4.1 Cargo features 体系：一个 crate，多种能力

#### 4.1.1 概念说明

ringbuf 的设计目标是「能跑在各种环境」——从带完整标准库的服务器，到没有操作系统的单片机。它通过 6 个 feature 实现「能力级联」，关掉一层就退化到更小的环境：

| feature | 作用 | 依赖 |
|---------|------|------|
| `default` | 默认开启，等于 `["std"]` | — |
| `std` | 启用标准库相关能力（线程、`io::Read/Write` 等） | `alloc`、`portable-atomic?/std`、`portable-atomic-util?/std` |
| `alloc` | 启用堆分配（`HeapRb`/`Heap` 存储），需要 `alloc` crate | `portable-atomic-util?/alloc` |
| `portable-atomic` | 用 [`portable-atomic`](https://crates.io/crates/portable-atomic) 替代标准原子，支持没有 CAS 的小型系统 | `dep:portable-atomic`、`dep:portable-atomic-util` |
| `bench` | 空特性，仅作为开关，用来在代码里 `#[cfg(feature = "bench")]` 门控基准测试代码 | — |
| `test_local` | 空特性，仅作为开关，让测试套件改测 `LocalRb` 而非默认的 `SharedRb` | — |

两个要点：

1. **`std` 依赖 `alloc`**：要标准库，自然也允许堆分配。所以开 `std` 会自动带上 `alloc`。
2. **`bench` 和 `test_local` 是「空特性」**：它们后面跟的是 `[]`，本身不引入任何代码，只是给 `#[cfg(feature = "...")]` 当条件用。这是一种常见的 Cargo 惯用法。

> 关于 `portable-atomic?/std` 里的问号 `?`：这是**弱依赖（weak dependency）**语法。意思是「只有当 `portable-atomic` 这个 feature 被启用时，才顺带把它的 `std` 也打开」。它不会强制启用 `portable-atomic`。

#### 4.1.2 核心流程

feature 之间形成一条「能力阶梯」，从下往上越开越强：

```
纯静态内存 (no_std, no-alloc)
        │  +alloc
        ▼
   堆分配可用 (no_std, alloc)   ──► HeapRb 可用
        │  +std
        ▼
  完整标准库 (std)               ──► 线程、io::Read/Write 可用

旁路开关：
  +portable-atomic  → 把内部原子换成 portable-atomic（适配无 CAS 目标）
  +bench            → 解锁基准测试代码
  +test_local       → 测试改测 LocalRb
```

编译时，cargo 先决定开哪些 feature（默认带 `std`，从而带 `alloc`），再把对应依赖（如 `portable-atomic`）按需拉进来。

#### 4.1.3 源码精读

feature 全部声明在核心 crate 的 `[features]` 段：

[Cargo.toml:27-33](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/Cargo.toml#L27-L33) —— 定义 6 个 feature 及其依赖关系。注意 `bench = []` 与 `test_local = []` 是空特性开关。

这些 feature 背后实际引入的依赖写在 `[dependencies]` 段。`portable-atomic` 和 `portable-atomic-util` 都带 `optional = true`，只有开对应 feature 时才会被编译进来：

[Cargo.toml:35-38](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/Cargo.toml#L35-L38) —— 声明 `crossbeam-utils`（始终需要）与两个可选的 `portable-atomic` 依赖。

#### 4.1.4 代码实践

**目标**：亲手验证 feature 的开闭如何改变编译产物。

**操作步骤**：

1. 用默认 feature 编译并确认 `std` 被启用：
   ```sh
   cargo build
   ```
2. 关掉默认特性，只保留 `alloc`（模拟「no_std 但有堆分配」）：
   ```sh
   cargo build --no-default-features --features alloc
   ```
3. 完全关掉所有特性（模拟「纯静态、无堆」）：
   ```sh
   cargo build --no-default-features
   ```
4. 想看某个 feature 把哪些代码编译进来了，可以加 `--message-format=json` 或直接在源码里搜 `#[cfg(feature = "alloc")]`。

**需要观察的现象**：三条命令都应**编译成功**（核心 crate 本身声明为 `#![no_std]`，所以即使关掉 `std` 也能编译）。区别在于能用到哪些类型——例如关掉 `alloc` 后 `HeapRb` 就不可用。

**预期结果**：编译通过。具体每个 feature 让哪些 API 可用，需要你对照源码 `#[cfg]` 标注确认（**待本地验证**）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `cargo build` 默认就能用到 `HeapRb`，而 `cargo build --no-default-features` 就用不了？

**参考答案**：默认 feature 是 `["std"]`，而 `std` 依赖 `alloc`，于是 `alloc` 被自动打开，`HeapRb`（依赖堆分配）可用。`--no-default-features` 关掉 `std`，连带关掉 `alloc`，堆分配相关类型随之不可用。

**练习 2**：`bench` 和 `test_local` 这两个 feature 后面是 `[]`，它们有什么用？

**参考答案**：它们是「空特性」，不引入任何依赖或代码，只作为条件开关供 `#[cfg(feature = "bench")]` / `#[cfg(feature = "test_local")]` 使用。这是 Cargo 把「开关」与「依赖」解耦的惯用法。

---

### 4.2 examples 与 required-features 门控

#### 4.2.1 概念说明

仓库根目录的 `examples/` 下有 6 个示例程序。它们既是文档（演示 API 怎么用），也是环境探测器（展示在不同 feature 组合下能用什么）。

问题在于：有些示例用了 `HeapRb`（需要 `alloc`），有些示例用了 `std::thread`（需要 `std`）。如果用户用 `--no-default-features` 编译，这些示例就会编译失败。为此 Cargo 提供 **`required-features`** 机制：在 `Cargo.toml` 里给某个 example 声明「只有当这些 feature 开启时才编译它」。

ringbuf 的 6 个示例及门控规则：

| 示例文件 | 演示内容 | required-features | 何时可用 |
|----------|----------|-------------------|----------|
| `simple.rs` | `HeapRb` 创建、拆分、push/pop | `alloc` | 默认即可 |
| `overwrite.rs` | `push_overwrite` 覆盖写入 | `alloc` | 默认即可 |
| `message.rs` | 双线程用 `io` 传递字节消息 | `std` | 默认即可 |
| `test_ordering.rs` | 弱内存序架构下的原子顺序压测 | `std` | 默认即可 |
| `static.rs` | `#![no_std]` 下用 `StaticRb` | （无） | 任何 feature 组合 |
| `global_static.rs` | `#![no_std]` 下用全局静态 `StaticRb` | （无） | 任何 feature 组合 |

注意后两个示例**没有 required-features**——它们本身就是 `#![no_std]` 且只用静态内存，所以在「纯静态、无堆」环境下也能编译。

#### 4.2.2 核心流程

当你执行 `cargo run --example 简单` 时，cargo 会：

1. 枚举 `examples/` 下所有 `.rs`。
2. 对每个 example，检查它声明的 `required-features` 是否全部满足当前 feature 集合。
3. 满足才编译并运行；不满足则**跳过**（不会报错，只是这个 example 不在可选列表里）。

所以：

- 默认 feature（`std`）下，6 个示例全部可见。
- `--no-default-features` 下，`simple`/`overwrite`/`message`/`test_ordering` 全部被跳过，只剩 `static`/`global_static`。

#### 4.2.3 源码精读

门控规则集中在 `Cargo.toml` 末尾，每个 `[[example]]` 块声明一个示例及其所需 feature：

[Cargo.toml:43-57](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/Cargo.toml#L43-L57) —— 4 个示例各自声明 required-features（`simple`/`overwrite` 要 `alloc`，`message`/`test_ordering` 要 `std`）。

对应的示例源码（任选两个印证「为什么需要这些 feature」）：

[examples/simple.rs:1](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/examples/simple.rs#L1) —— 引入 `HeapRb`，而 `HeapRb` 依赖堆分配，所以这个 example 必须 `required-features = ["alloc"]`。

[examples/static.rs:1-3](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/examples/static.rs#L1-L3) —— 顶部是 `#![no_std]`，只用 `StaticRb`（静态内存），不需要任何额外 feature，所以**没有** required-features。

> 对照记忆：**有 required-features 的示例依赖 std/alloc；没门控的两个是纯 no_std 静态示例。** 这是理解 4.3 节脚本里各种 `--no-default-features` 检查为何能通过的关键。

#### 4.2.4 代码实践

**目标**：跑通示例，并验证 `--no-default-features` 会跳过哪些示例。

**操作步骤**：

1. 跑两个 `alloc` 示例（核心 crate 根目录下）：
   ```sh
   cargo run --example simple
   cargo run --example overwrite
   ```
   `simple` 没有输出（全是 `assert_eq!` 断言），程序正常退出即代表通过；`overwrite` 同理。
2. 列出当前 feature 下「可见」的示例：
   ```sh
   cargo run --example
   ```
   末尾会列出所有可运行的 example 名字。
3. 关掉默认特性再列一次：
   ```sh
   cargo run --example --no-default-features
   ```
4. 尝试在无 `alloc` 下强行跑 `simple`（预期失败/找不到）：
   ```sh
   cargo run --example simple --no-default-features
   ```

**需要观察的现象**：步骤 2 应能看到 6 个示例；步骤 3 应只看到 `static`、`global_static`（其余 4 个被门控跳过）；步骤 4 会因 `simple` 不在可选列表而报错。

**预期结果**：
- 步骤 1 两个程序都「安静地」正常退出（断言全过）。
- 步骤 3 可见示例数量减少到 2 个。

> 提示：`cargo run --example` 不带名字时会打印帮助并列出 example，这是查看门控效果的最快方式。具体列表显示格式以本地 cargo 版本为准（**待本地验证**）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `message.rs` 的 required-features 是 `["std"]` 而不是 `["alloc"]`？

**参考答案**：因为 `message.rs` 里用了 `std::thread`、`std::io::Read` 等标准库功能（[examples/message.rs:2](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/examples/message.rs#L2)）。这些只在 `std` feature 下可用，所以门控级别更高，必须是 `std`。

**练习 2**：用 `--no-default-features` 时，哪两个示例仍然能跑？为什么？

**参考答案**：`static.rs` 和 `global_static.rs` 仍然能跑。因为它们顶部声明了 `#![no_std]`，只使用静态内存的 `StaticRb`，不依赖 `std` 或 `alloc`，所以没有声明 required-features，在任何 feature 组合下都可用。

---

### 4.3 scripts 脚本与 CI：项目的「官方」验证流程

#### 4.3.1 概念说明

`scripts/` 目录下有三个 shell 脚本，它们把「官方推荐」的验证流程固化下来——不是只跑一次 `cargo test`，而是**系统地覆盖各种 feature 组合、三个 crate、甚至嵌入式目标**。CI（持续集成）则会在每次提交时自动跑其中之一。

| 脚本 | 作用 | 工具链 |
|------|------|--------|
| `scripts/test.sh` | 综合测试：三个 crate × 多种 feature × `thumbv6m` 嵌入式目标 | stable |
| `scripts/miri.sh` | 用 Miri 检查 `unsafe` 代码有无未定义行为 | nightly |
| `scripts/bench.sh` | 跑性能基准测试 | nightly |

`thumbv6m-none-eabi` 是一个 ARM Cortex-M0 的编译目标，用来验证 ringbuf 真的能跑在没有 64 位原子指令的小芯片上（这正是 `portable-atomic` feature 的用武之地）。

#### 4.3.2 核心流程

**`test.sh` 的流程**（顺序很重要，前一步失败会用 `&&` 中断）：

```
1. rustup target add thumbv6m-none-eabi        # 安装嵌入式目标
2. 核心 crate 多轮测试：
   cargo test                                  # 默认（std）测试
   cargo test --features test_local            # 改测 LocalRb
   cargo test --features portable-atomic       # 用 portable-atomic
   cargo check --no-default-features --features alloc    # 检查 no_std+alloc
   cargo check --no-default-features                     # 检查纯静态
   cargo check --target thumbv6m ... --features alloc,portable-atomic,critical-section
3. cd async   → 对 async-ringbuf 做类似的多轮测试/check
4. cd blocking → 对 ringbuf-blocking 做类似的多轮测试/check
5. echo "Done!"
```

**`miri.sh` 的流程**：

```
cargo +nightly miri test                       # 核心 crate
cargo +nightly miri test --features test_local # 核心改测 LocalRb
cd async && cargo +nightly miri test           # async crate
```

**`bench.sh` 的流程**：

```
cargo +nightly bench --features=bench          # 核心
cd async && cargo +nightly bench --features=bench
```

**CI（`.github/workflows/test.yml`）**：在每次 `push` 或 `pull_request` 时，跑在 `ubuntu-latest` 上，安装 stable 工具链后执行 `./scripts/test.sh`。

#### 4.3.3 源码精读

**`scripts/test.sh`** —— 三段式结构（核心 / async / blocking），每段都用多种 feature 组合验证：

[scripts/test.sh:3-9](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/scripts/test.sh#L3-L9) —— 核心 crate 的测试与 check：注意第 4 行 `cargo test` 是默认 feature、第 5 行加 `test_local` 切换被测 RB 类型、第 9 行用 `thumbv6m-none-eabi` 目标验证嵌入式可用性。

[scripts/test.sh:10-20](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/scripts/test.sh#L10-L20) —— `cd async` 与 `cd ../blocking` 段落，对两个派生 crate 重复「test + 多种 check + 嵌入式 check」的模式。

**`scripts/miri.sh`** —— 用 nightly 工具链跑 Miri：

[scripts/miri.sh:3-7](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/scripts/miri.sh#L3-L7) —— 对核心 crate 跑两遍 Miri（默认 + `test_local`），再对 async crate 跑一遍。

**`scripts/bench.sh`** —— 注意 `--features=bench` 启用了 4.1 节说的那个空特性开关：

[scripts/bench.sh:3-5](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/scripts/bench.sh#L3-L5) —— 核心与 async crate 都用 `cargo +nightly bench --features=bench`。

**CI 配置**：

[.github/workflows/test.yml:1-13](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/.github/workflows/test.yml#L1-L13) —— 触发条件是 `push`/`pull_request`；关键在第 12 行直接调用 `./scripts/test.sh`，说明 CI 与本地验证用的是**同一套脚本**。

> 重要观察：CI 只跑 `test.sh`（stable 工具链），并不跑 miri 和 bench。这是因为 Miri/bench 需要 nightly，跑得也慢，通常留给开发者本地或专门的工作流。三个脚本之间的分工由此清晰可见。

#### 4.3.4 代码实践

**目标**：亲手跑通综合测试脚本，理解它覆盖了哪些维度。

**操作步骤**：

1. 先确认已安装 stable 工具链，然后运行综合测试：
   ```sh
   ./scripts/test.sh
   ```
2. 观察输出：它会依次打印核心、async、blocking 三段的编译与测试过程，最后打印 `Done!`。
3. 如果只关心核心 crate 的测试，可以直接手动跑等价命令：
   ```sh
   cargo test
   cargo test --features test_local
   ```
4. 对比两次 `cargo test` 的输出，留意测试项数量/名称是否随 `test_local` 改变。

**需要观察的现象**：
- `./scripts/test.sh` 中间会先执行 `rustup target add thumbv6m-none-eabi`（联网安装目标），随后是若干轮 `Compiling`/`Running`/`test result: ok`。
- 全部通过时末尾出现 `Done!`。

**预期结果**：脚本末尾打印 `Done!`，过程中所有 `cargo test` 行都显示 `test result: ok`。若某轮 `cargo check`（如纯静态那轮）失败，说明环境缺少必要组件（**待本地验证**：实际是否一次跑通取决于本机是否已装好 rustup 与各 target）。

> 提示：`scripts/test.sh` 涉及联网安装 target 和编译三个 crate，首次运行可能较慢、需要网络。如果你只想验证核心 crate，用手动 `cargo test` 更快。

#### 4.3.5 小练习与答案

**练习 1**：`scripts/test.sh` 里为什么要单独加一轮 `cargo test --features test_local`？

**参考答案**：`test_local` 是空特性开关，启用后测试套件里的 `#[cfg(feature = "test_local")]` 分支会把被测类型从默认的 `SharedRb`（多线程）切换成 `LocalRb`（单线程）。这样同一套测试代码能同时验证两种 RB 实现，保证它们行为一致。

**练习 2**：为什么 CI（`test.yml`）只跑 `test.sh`，而不跑 `miri.sh` 和 `bench.sh`？

**参考答案**：`test.sh` 用 stable 工具链、覆盖面广，适合作为每次提交的门禁。而 miri 和 bench 需要 nightly 工具链、执行慢，且 bench 对结果稳定性要求高，通常留给开发者按需手动运行，所以没放进基础 CI。

**练习 3**：`scripts/test.sh` 第 9 行为什么针对 `thumbv6m-none-eabi` 目标要带上 `portable-atomic` 和 `portable-atomic/critical-section`？

**参考答案**：`thumbv6m`（Cortex-M0）这类小芯片没有原生的 64 位原子/CAS 指令，标准原子不可用。`portable-atomic` 提供替代实现，而 `critical-section` 让它在没有 OS 的裸机上用临界区（关中断等方式）模拟原子操作。带上这两个 feature，才能在这个目标上编译通过。

---

## 5. 综合实践

**任务**：用本讲学到的 feature 与脚本知识，为 ringbuf 做一次「全维度」本地体检，并把结果整理成一张表。

**步骤**：

1. **核心 crate 测试**：运行 `cargo test` 和 `cargo test --features test_local`，记录两次的「test result」行（通过数、失败数）。
2. **示例验证**：运行 `cargo run --example simple` 与 `cargo run --example overwrite`，确认它们正常退出；再运行 `cargo run --example test_ordering`（需要 `std`，这是个跑 1000 万次跨线程传输的压测，确认它最终打印 `Success!`）。
3. **环境退化实验**：执行 `cargo run --example --no-default-features`，列出仍可见的示例，验证「只剩 `static`、`global_static`」。
4. **no_std 编译实验**：执行 `cargo check --no-default-features`，确认核心 crate 在「纯静态、无堆」下能编译通过。
5. **整理一张表**：列出行号引用，把「命令 → 期望现象 → 实际现象（待本地验证）」三列填好。

**预期**：你能据此向别人解释「ringbuf 如何用一份源码同时支持 std、no_std+alloc、纯静态、嵌入式四种环境」。

> 注意：`test_ordering` 会跑约一千万次循环，在 release 下可能也要数秒到数十秒。如想更快验证，可只跑 `simple`/`overwrite`。压测耗时与机器性能有关（**待本地验证**）。

---

## 6. 本讲小结

- ringbuf 核心 crate 用 6 个 feature（`default`/`std`/`alloc`/`portable-atomic`/`bench`/`test_local`）实现「能力级联」，开 `std` 会自动带 `alloc`；`bench`、`test_local` 是空特性纯开关。
- `examples/` 下的示例通过 `required-features` 被门控：`simple`/`overwrite` 要 `alloc`，`message`/`test_ordering` 要 `std`，`static`/`global_static` 无门控（纯 no_std）。
- `--no-default-features` 会跳过所有带门控的示例，只留下两个静态示例——这正是 ringbuf「无堆也能用」的直接证据。
- `scripts/test.sh` 用 stable 工具链系统覆盖三个 crate × 多种 feature × 嵌入式目标；`miri.sh` 用 nightly 校验 `unsafe` 内存安全；`bench.sh` 用 nightly 跑性能基准。
- CI（`.github/workflows/test.yml`）在每次 push/PR 时只跑 `./scripts/test.sh`，与本地用的是同一套脚本，保证环境一致。
- `test_local` 让同一套测试代码既能测 `SharedRb` 也能测 `LocalRb`，是理解后续 trait 体系测试的基础。

---

## 7. 下一步学习建议

本讲结束后，你已经能编译、运行、测试 ringbuf，并对它的 feature 体系有了整体认识。接下来建议：

- 进入 **u2 单元**（环形缓冲区原理），先读 [u2-l1 双索引与 2*capacity 模运算](u2-l1-indices-modular-arithmetic.md)，理解 `try_push`/`try_pop` 背后真正的数学原理。
- 在那之前，可以**边读边验证**：把本讲的 `cargo test --features test_local` 输出保留下来，后续学到 `LocalRb` vs `SharedRb` 时再回头看，体会 `test_local` 这个开关的价值。
- 想提前感受 `unsafe` 的安全性保障，可在装好 nightly + miri 后尝试运行 `./scripts/miri.sh`，等到 [u5-l3（MaybeUninit 与 unsafe 内存管理）](u5-l3-maybeuninit-unsafe-memory.md) 时再深入理解它的输出。
