# 构建特性、build.rs 与运行测试

> 本讲是「起步与认识 crossbeam-utils」单元的第三讲（u1-l3）。
> 前两讲（u1-l1、u1-l2）我们已经知道：这个 crate 有哪些公开类型、它们如何按 feature 和 cfg 被组织成模块。本讲要回答一个更落地的问题——**这些 feature 和 cfg 到底是在哪里、用什么机制定义出来的？我该用什么命令去编译、测试和压测它们？**

---

## 1. 本讲目标

学完本讲后，你应当能够：

1. 说出 crossbeam-utils 的三个特性（`default`/`std`/`atomic`）各自控制什么，以及为什么 `atomic` 特性单独要求 Rust 1.74。
2. 读懂 `build.rs`，解释它在编译期根据**目标平台**和**sanitizer 设置**自动发出了哪些 `rustc-cfg`（`crossbeam_no_atomic`、`crossbeam_sanitize_thread`、`crossbeam_atomic_cell_force_fallback`）。
3. 用不同 feature 组合（default、`--no-default-features`、`--features atomic`）构建并运行 `cargo test`，并能预测 `AtomicCell`、`AtomicConsume`、`sync`、`thread::scope` 在每种配置下是否可用。
4. 知道 `tests/`、`benches/`、loom 这三套测试/压测入口的写法和运行方式。

---

## 2. 前置知识

在进入源码前，先用最朴素的语言建立三个概念。如果你已经很熟，可以跳过。

### 2.1 什么是「特性（feature）」

Cargo 的 feature 是一个**编译期开关**。你可以在 `Cargo.toml` 里声明若干个开关，每个开关可以再打开其它开关或拉入依赖。最终：

- 开关的取值是 **true / false**，没有第三态。
- 它们会翻译成 Rust 的 `cfg(feature = "xxx")`，代码里用 `#[cfg(feature = "xxx")]` 来「按开关裁剪代码」。
- u1-l2 已经讲过：crossbeam-utils 正是用 `cfg(feature = "std")`、`cfg(feature = "atomic")` 来决定哪些模块被编译。

> 关键直觉：**「文件存在」≠「被编译」≠「对外可见」**（这是 u1-l2 的核心结论）。本讲补上最后一环——这些开关的「定义」在 `Cargo.toml`，而开关之外的「平台相关」开关在 `build.rs` 里动态产生。

### 2.2 什么是「build script（build.rs）」

build.rs 是一段**在「编译 crate 本体之前」先运行的 Rust 程序**。它最常见的输出是形如

```
cargo:rustc-cfg=crossbeam_no_atomic
```

这样的指令。Cargo 收到这条指令后，等价于在编译 crate 本体时开启了 `cfg(crossbeam_no_atomic)`。于是 crate 本体里写的 `#[cfg(not(crossbeam_no_atomic))]` 就能根据**当前正在编译的目标平台**动态生效。

crossbeam-utils 用 build.rs 解决一个问题：**不同 CPU 架构对原子操作的支持不一样**，这种「目标相关」的判断无法写死在 `Cargo.toml` 的 feature 里，必须在 build.rs 里查表决定。

### 2.3 三种「测试/压测」入口

| 入口 | 目录 | 命令 | 作用 |
|---|---|---|---|
| 单元 + 集成测试 | `tests/` | `cargo test` | 正确性断言（多线程压力下的不变量） |
| bench 基准 | `benches/` | `cargo bench` | 性能测量（吞吐 / 延迟） |
| loom 并发模型测试 | crate 内 `cfg(crossbeam_loom)` 分支 | 见第 4.3 节 | 穷举线程交错，抓数据竞争 |

注意：`benches/` 用的是 nightly 的 `#![feature(test)]`，所以 `cargo bench` 需要 nightly 工具链；而 `cargo test` 在 stable 上就能跑。

---

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| [Cargo.toml](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/Cargo.toml) | crate 元信息、特性、依赖 | `features`、`atomic` 特性为何要 1.74、loom 依赖 |
| [build.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/build.rs) | 编译期脚本 | 按目标平台 / sanitizer 发出 cfg |
| [no_atomic.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/no_atomic.rs) | 不支持原子的目标清单 | 被 build.rs `include!` 进来查表 |
| [build-common.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/build-common.rs) | build.rs 共用工具 | Linux 自定义目标的归一化 |
| [src/lib.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs) | crate 根 | 用 feature/cfg 门控模块（u1-l2 已读，本讲作为对照） |
| [src/atomic/mod.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/mod.rs) | atomic 模块门面 | `target_has_atomic` + loom 的 cfg 组合 |
| [benches/atomic_cell.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/benches/atomic_cell.rs) | 基准测试 | `#![feature(test)]` 写法、并发 bench |
| [tests/atomic_cell.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/atomic_cell.rs) | 集成测试示例 | 如何在测试里读取 build.rs 发出的 cfg |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 Cargo.toml 的 features 与依赖**——开关在哪里定义、依赖怎么挂。
- **4.2 build.rs 的 cfg 发射**——平台 / sanitizer 相关的开关如何在编译期动态产生。
- **4.3 benches 与测试入口（含 loom）**——怎么跑测试和压测、loom 是怎么接进来的。

### 4.1 Cargo.toml 的 features 与依赖

#### 4.1.1 概念说明

`Cargo.toml` 的 `[features]` 段定义了这个 crate 对外暴露的所有开关。crossbeam-utils 只有两个**真正的**开关：`std` 和 `atomic`，外加一个把 `std` 默认打开的聚合开关 `default`。

这看似简单，但它决定了整个 crate 的「可裁剪性」：

- 想用在 `no_std` 嵌入式环境？关掉 `std`，但 `Parker`/`ShardedLock`/`WaitGroup`/`scope` 就没了（它们要 `std::sync`、`std::thread`）。
- 想用 `AtomicCell`？必须显式打开 `atomic`，因为它依赖一个外部 crate `atomic-maybe-uninit`，而这个 crate 要求 Rust 1.74。

#### 4.1.2 核心流程

特性之间的依赖关系如下（伪代码）：

```text
default ──打开──> std
std                # 自身是空开关，仅作「需要标准库」的标记
atomic ──拉入依赖──> atomic-maybe-uninit (optional)
```

把特性翻译成「模块可见性」的规则（承接 u1-l2，这里只做汇总）：

| 模块 / 类型 | 控制它的开关 | 备注 |
|---|---|---|
| `atomic::AtomicCell` | `feature = "atomic"` + `target_has_atomic="ptr"` + 非 loom | 三重门控 |
| `atomic::AtomicConsume` | `feature = "atomic"` + `not(crossbeam_no_atomic)` | 在 `atomic` 模块内，无独立门控 |
| `sync::{Parker,WaitGroup,ShardedLock}` | `feature = "std"` | ShardedLock 还要求非 loom |
| `thread::scope` | `feature = "std"` + 非 loom | |
| `Backoff`、`CachePadded` | 无门控 | 任何时候都可用，对应 README 的 `(no_std)` 标记 |

#### 4.1.3 源码精读

先看元信息，注意 `rust-version` 与 `atomic` 特性要求的版本**不一样**：

[Cargo.toml:7-16](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/Cargo.toml#L7-L16) —— 版本 `0.8.21`、edition `2021`、`rust-version = "1.56"`。这里的 `1.56` 是 crate 整体的 MSRV（最低支持 Rust 版本）。

接下来是关键 `[features]` 段：

[Cargo.toml:27-36](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/Cargo.toml#L27-L36) —— 这段定义了三个特性。要点：

- `default = ["std"]`：默认打开 `std`。这意味着**不传任何参数的 `cargo build` 就带上了 `std`**。
- `std = []`：空开关，纯粹作标记用，供 `#[cfg(feature = "std")]` 判断。
- `atomic = ["atomic-maybe-uninit"]`：打开 `atomic` 的同时**会自动把 `atomic-maybe-uninit` 这个 optional 依赖激活**。注释明确写着「This requires Rust 1.74」——这就是为什么 crate 整体 MSRV 是 1.56、但单独用 `atomic` 特性需要 1.74 的原因。

依赖声明：

[Cargo.toml:38-46](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/Cargo.toml#L38-L46) —— 注意两点：

1. `atomic-maybe-uninit` 带 `optional = true`，只有当 `atomic` 特性被打开时才会被编译进来。
2. `loom` 是**带 target cfg 的 optional 依赖**：`[target.'cfg(crossbeam_loom)'.dependencies]`。也就是说，只有当编译环境设置了 `--cfg crossbeam_loom`（这是给并发测试用的，见 4.3 节）时，`loom` 才会被纳入。`crossbeam_loom` 是一个**手动设置的 cfg**，不在 `Cargo.toml` 的 feature 里，注释也强调它「不受 semver 约束」，随时可能变。

[Cargo.toml:48-50](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/Cargo.toml#L48-L50) —— `dev-dependencies`（`fastrand`、`rustversion`）只在测试和 bench 时编译，不会影响下游使用者。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 `atomic` 特性需要 Rust 1.74、且默认并不打开。

**操作步骤**：

1. 用 `cargo build` 看默认特性集合：

   ```bash
   cargo build --verbose 2>&1 | grep -E "feature"
   ```

2. 显式列出某次编译实际启用的特性：

   ```bash
   cargo build --features atomic --verbose 2>&1 | grep -oE 'crossbeam-utils/[^ ]*'
   ```

**需要观察的现象**：

- 不加参数时，编译命令里只有 `feature="std"`，**没有** `atomic-maybe-uninit` 依赖。
- 加上 `--features atomic` 后，依赖列表里出现 `atomic-maybe-uninit`。

**预期结果**：默认配置下 `atomic` 模块根本不会被编译，所以 `use crossbeam_utils::atomic::AtomicCell;` 在默认配置下会编译失败（这个事实会在第 5 节综合实践中正式验证）。

**待本地验证**：如果你的工具链低于 1.74，第二步会因 `atomic-maybe-uninit` 要求更高版本而报错——这正是「crate MSRV 1.56、但 atomic 特性要 1.74」的体现，请在本机确认。

#### 4.1.5 小练习与答案

**练习 1**：如果用户写 `cargo build --no-default-features`，最终启用了哪些特性？
**答案**：一个都没有——`std` 被关、`atomic` 本来也没默认开。此时只有不受任何门控的 `Backoff`、`CachePadded` 可用。

**练习 2**：为什么 `loom` 依赖不放在普通 `[dependencies]` 而要加 `[target.'cfg(crossbeam_loom)']`？
**答案**：因为 loom 只在并发模型测试时才需要，普通用户和 CI 默认编译绝不应该把它拉进来。用 target + cfg 双重限定，可以保证「不设 `crossbeam_loom` 这个 cfg 时，loom 依赖完全不存在」。

**练习 3**：crate 的 MSRV 是 1.56，但有文档说 `atomic` 要 1.74。这两者矛盾吗？
**答案**：不矛盾。1.56 是「不使用 `atomic` 特性」时的 MSRV；`atomic` 是可选特性，只有需要它的人才必须用 1.74+。这是一种常见的「特性抬高 MSRV」的妥协。

---

### 4.2 build.rs 的 cfg 发射

#### 4.2.1 概念说明

`Cargo.toml` 的 feature 只能回答「用户想不想要某个功能」，但它回答不了「**当前目标平台支不支持原子操作**」这种问题——这取决于编译目标（`TARGET`，例如 `thumbv6m-none-eabi`）。

build.rs 就是用来填这个空白的。它在编译 crate 本体之前运行，读取 `TARGET` 等环境变量，查表，然后通过 `println!("cargo:rustc-cfg=...")` 把判断结果「广播」回编译过程。

crossbeam-utils 的 build.rs 会发出**三个** cfg，其中第一个被声明为「公开但不稳定 API」：

| cfg | 含义 | 谁来设置 |
|---|---|---|
| `crossbeam_no_atomic` | 假设目标**完全不支持**原子操作 | build.rs 自动检测（可手动覆盖） |
| `crossbeam_sanitize_thread` | 当前启用了 ThreadSanitizer | build.rs 检测 `CARGO_CFG_SANITIZE` |
| `crossbeam_atomic_cell_force_fallback` | 强制 `AtomicCell` 走全局锁回退 | build.rs 在任何 sanitize 时设置 |

#### 4.2.2 核心流程

build.rs 的执行流程（伪代码）：

```text
main():
    include!("no_atomic.rs")        # 把 NO_ATOMIC 数组常量编译进来
    include!("build-common.rs")     # 把 convert_custom_linux_target 编译进来

    target = env::var("TARGET")     # 拿到编译目标三元组
    target = convert_custom_linux_target(target)   # 归一化 Linux 自定义目标

    if NO_ATOMIC.contains(target):
        emit cfg: crossbeam_no_atomic          # 目标在「无原子」黑名单里

    if CARGO_CFG_SANITIZE 包含 "thread":
        emit cfg: crossbeam_sanitize_thread
    if CARGO_CFG_SANITIZE 非空(任何 sanitize):
        emit cfg: crossbeam_atomic_cell_force_fallback
```

为什么用 `no_*` 而不是 `has_*`？build.rs 顶部注释给了一个很巧妙的设计理由：

> 用 `no_atomic`（否定式）而不是 `has_atomic`，是为了让「build script 没有运行」的情况**默认等同于使用了最新稳定 rustc 的行为**。这对那些不跑 build script 的非 cargo 构建系统很关键——它们会被当作「支持原子」处理。

也就是说：**build.rs 不跑 = 假设支持原子**。这是一种对缺失信息时的「乐观默认」。

`CARGO_CFG_SANITIZE` 这一支的设计意图是：ThreadSanitizer 会把 `AtomicCell` 的内部 SeqLock 乐观读误报为数据竞争，所以在 sanitizer 下干脆强制走全局锁回退（`force_fallback`），让行为对 sanitizer 友好。这部分会在 u5-l1（unsafe 安全性）和 u2-l3（全局锁回退）深入。

#### 4.2.3 源码精读

先看顶部对「公开 cfg」的声明：

[build.rs:1-16](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/build.rs#L1-L16) —— 开头注释明确：`crossbeam_no_atomic` 是**公开但不稳定**的 cfg（受 semver 豁免），其它 cfg 则完全不是公开 API。随后用 `include!` 把两个辅助文件直接文本插入：`no_atomic.rs` 提供黑名单常量，`build-common.rs` 提供目标归一化函数。

黑名单本身（由脚本 `no_atomic.sh` 生成，不要手改）：

[no_atomic.rs:4-13](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/no_atomic.rs#L4-L13) —— 一共 8 个目标，都是连指针宽度原子都没有的极简嵌入式目标（`armv4t-none-eabi`、`msp430-none-elf`、`bpfel-unknown-none` 等）。在它们上编译，build.rs 就会打开 `crossbeam_no_atomic`。

接下来看 `main` 主体：

[build.rs:18-34](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/build.rs#L18-L34) —— 第 19 行声明 `no_atomic.rs` 变化时重跑 build script；第 20-22 行用 `rustc-check-cfg` 告诉 rustc「这三个 cfg 是合法的」，避免 unknown-cfg 警告。第 24-34 行读取并归一化 `TARGET`，拿不到就打印 warning 直接返回（不阻断编译，对应「乐观默认」）。

[build.rs:36-41](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/build.rs#L36-L41) —— **第一处 cfg 发射**：目标在黑名单里 → 发出 `crossbeam_no_atomic`。这正是 `consume.rs` 顶部 `#[cfg(not(crossbeam_no_atomic))]`（见 [src/atomic/consume.rs:1-2](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/consume.rs#L1-L2)）所依赖的开关。

[build.rs:43-49](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/build.rs#L43-L49) —— **第二、三处 cfg 发射**：从 `CARGO_CFG_SANITIZE` 读取 sanitizer 设置。只要含 `thread` 就发 `crossbeam_sanitize_thread`；而只要**任何** sanitizer 开着，就发 `crossbeam_atomic_cell_force_fallback`。这两个 cfg 随后会被 `AtomicCell` 的实现用来切换路径。

附带看一下目标归一化为什么要做：

[build-common.rs:1-13](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/build-common.rs#L1-L13) —— 当目标是自定义 Linux 三元组（如 `x86_64-mycompany-linux-gnu`）时，把 vendor 段改成 `unknown`，归一化成 `x86_64-unknown-linux-gnu`，这样才能和 `NO_ATOMIC` 这张「按标准目标写的表」正确匹配。

#### 4.2.4 代码实践

**实践目标**：让 build.rs 打印出它发出了哪些 cfg。

**操作步骤**：

1. 直接运行 build script 阶段，观察输出：

   ```bash
   cargo build -vv 2>&1 | grep -iE "rustc-cfg|no_atomic|sanitize"
   ```

2. 模拟「无原子目标」，强制触发 `crossbeam_no_atomic`（这会真正改变编译行为）：

   ```bash
   # 仅当你装了对应 target 时；没有可跳过
   rustup target add thumbv5te-none-eabi
   cargo build --target thumbv5te-none-eabi --no-default-features -vv 2>&1 | grep "rustc-cfg"
   ```

**需要观察的现象**：

- 在常规 `x86_64-unknown-linux-gnu` 上，第 1 步**不会**打印任何 `crossbeam_no_atomic`（因为该目标支持原子），也不会有 sanitize 相关 cfg。
- 第 2 步切换到 `thumbv5te-none-eabi`（在黑名单里）后，输出里会出现 `cargo:rustc-cfg=crossbeam_no_atomic`。

**预期结果**：cfg 的发出与否完全由 `TARGET` 决定，证明这是「平台相关」开关，而非用户 feature。

**待本地验证**：第 2 步是否可跑取决于你是否装了 nightly（部分 `none-eabi` target 需要 nightly）和对应 target。若不可用，仅观察第 1 步即可。

#### 4.2.5 小练习与答案

**练习 1**：为什么 build.rs 用 `no_atomic`（黑名单）而不是 `has_atomic`（白名单）？
**答案**：见 4.2.2 节——为了让「build script 没运行」（如非 cargo 构建系统）时默认等价于「最新 rustc，支持原子」，避免把新平台误判成无原子。

**练习 2**：`crossbeam_atomic_cell_force_fallback` 和 `crossbeam_sanitize_thread` 有什么区别？
**答案**：后者只在 ThreadSanitizer 时开；前者在**任何** sanitizer 时都开。换言之，前者条件更宽——`force_fallback` 是个「跑 sanitizer 时就别用 SeqLock 乐观读」的总开关，`sanitize_thread` 是其中更细的子情形。

**练习 3**：如果 build.rs 因为拿不到 `TARGET` 直接 `return` 了，crate 本体会被编译成「支持原子」还是「不支持」？
**答案**：被当成「支持原子」。因为没有任何 `crossbeam_no_atomic` 被发出，crate 本体里 `#[cfg(not(crossbeam_no_atomic))]` 的分支会全部生效。

---

### 4.3 benches 与测试入口（含 loom）

#### 4.3.1 概念说明

crossbeam-utils 的「验证」分成三套，各自回答不同的问题：

1. **`tests/` 下的集成测试**——回答「它行为对吗？」。针对每个公开类型都有独立的测试文件（`atomic_cell.rs`、`parker.rs`、`wait_group.rs`、`sharded_lock.rs`、`thread.rs`、`cache_padded.rs`），里面是多线程压力下的不变量断言。
2. **`benches/` 下的基准测试**——回答「它快吗？快多少？」。注意它用的是 nightly 专用的不稳定 `test` 特性。
3. **loom 并发模型测试**——回答「在所有可能的线程交错下，会不会数据竞争？」。loom 用一个特殊的执行器去穷举线程切换顺序，为此 crate 内部专门准备了一套 `primitive` 抽象层（u1-l2 已介绍）在 `loom` 实现和标准库实现之间二选一。

#### 4.3.2 核心流程

**loom 是怎么接进来的**——这是本节最绕的部分，用伪代码梳理：

```text
# 编译时二选一的「标准库替身」(src/lib.rs)
if cfg(crossbeam_loom):              # 测试模式
    primitive.sync.atomic.* = loom::sync::atomic::*    # 用 loom 的原子
    primitive.sync.{Arc,Mutex,Condvar} = loom::sync::* # 用 loom 的同步原语
else:                                # 正常模式
    primitive.sync.atomic.* = core::sync::atomic::*
    primitive.sync.{Arc,Mutex,Condvar} = {alloc,std}::sync::*
```

crate 内部所有代码都只用 `primitive::sync::...`，绝不直接写 `std::sync::...`。于是同一份源码，在 `--cfg crossbeam_loom` 下就变成「loom 模型下的版本」，可以被 loom 穷举交错；否则就是正常生产版本。

> 注意 loom 有覆盖盲区（u1-l2 已指出）：`AtomicCell`、`ShardedLock`、`thread::scope` 在 loom 下**不可用**，因为它们的实现依赖 loom 无法表达的内存表示或栈借用。

**运行测试 / bench 的命令一览**：

```bash
# 1. 普通集成测试（stable 即可）
cargo test

# 2. 只测某个文件
cargo test --test wait_group

# 3. 跑 bench（需要 nightly，因为用了 #![feature(test)]）
cargo +nightly bench

# 4. loom 模型测试（设置 crossbeam_loom 这个 cfg）
RUSTFLAGS="--cfg crossbeam_loom" cargo test --features std

# 5. Miri / TSan（验证 UB 和数据竞争）
cargo +nightly miri test
```

第 4、5 条的 cfg 正是由 4.2 节 build.rs / 环境变量驱动的。

#### 4.3.3 源码精读

先看 `primitive` 抽象层如何为 loom 让路（承接 u1-l2，这里聚焦「为什么这样分叉」）：

[src/lib.rs:47-83](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs#L47-L83) —— 两个互斥的 `mod primitive` 块。`crossbeam_loom` 时（47-69 行）把原子类型、`Arc`、`Mutex`、`Condvar` 全部 `use` 自 `loom::sync`；并特别注明 loom 暂不支持 `compiler_fence`，临时用更强的 `fence` 顶替（可能漏检一些竞争，属已知折中）。否则（70-83 行）用 `core`/`alloc`/`std` 的实现。

[src/lib.rs:85-100](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs#L85-L100) —— 这几行汇总了所有模块门控，是本讲和 u1-l2 的「真相源」：`atomic` 需 `feature="atomic"`，`sync` 需 `feature="std"`，`thread` 需 `feature="std"` **且** 非 loom。`thread` 多出的 `not(crossbeam_loom)` 正是上面说的「loom 覆盖盲区」之一。

再看 `atomic` 模块内部如何叠加「平台 + loom」门控：

[src/atomic/mod.rs:6-29](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/mod.rs#L6-L29) —— `AtomicCell` 需要 `target_has_atomic="ptr"` 且非 loom；私有 `seq_lock` 在 16/32 位指针宽度下改用更宽的版本（`seq_lock_wide.rs`，u5-l3 会讲）。这展示了「同一个类型，在不同平台/不同测试模式下走不同实现」的全貌。

接着看测试是如何读取 build.rs 发出的 cfg 的：

[tests/atomic_cell.rs:9-18](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/atomic_cell.rs#L9-L18) —— 测试里用 `cfg!(crossbeam_atomic_cell_force_fallback)` 等判断当前是否被强制走回退路径，从而让 `is_lock_free` 的断言在不同 sanitizer 配置下都成立。这是「build.rs 发 cfg → 测试消费 cfg」的完整闭环示例。

一个典型的集成测试长这样：

[tests/wait_group.rs:7-34](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/wait_group.rs#L7-L34) —— `wait()` 测试：spawn 10 个线程各自 `wg.wait()` 阻塞，主线程先断言「此时收不到消息」，再 `wg.wait()`，然后断言「能收到全部消息」。这类测试关心的不变量是「计数归零前不会 notify / 归零后才 notify」。

最后看 bench 的写法：

[benches/atomic_cell.rs:1-15](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/benches/atomic_cell.rs#L1-L15) —— 顶部 `#![feature(test)]` + `extern crate test;` 是 nightly bench 的固定写法。`#[bench] fn load_u8(b: &mut test::Bencher)` 用 `b.iter(|| ...)` 包裹被测代码。bench 文件按「操作类型 × 数据类型」组织：`load/store/fetch_add/compare_exchange` × `{u8, usize}`，外加两个 `concurrent_*` 并发基准。

[benches/atomic_cell.rs:40-83](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/benches/atomic_cell.rs#L40-L83) —— `concurrent_load_u8` 展示了**并发 bench 的标准骨架**：用两个 `Barrier` 做「起点/终点同步」，启动 `THREADS` 个工作线程在 `STEPS` 次循环里反复 `load`，主线程在 `b.iter` 里只测「同步一次迭代」的耗时，并用一个 `exit` 标志优雅退出。`thread::scope` 在这里被用来保证 bench 结束前所有线程已 join——这正是 u4-l1 即将讲的作用域线程。

#### 4.3.4 代码实践

**实践目标**：跑通测试和 bench，并在 loom 模式下编译一次。

**操作步骤**：

1. 跑普通测试：

   ```bash
   cargo test
   ```

2. 跑单个测试文件，便于看清楚：

   ```bash
   cargo test --test wait_group -- --nocapture
   ```

3. （需 nightly）跑 bench：

   ```bash
   cargo +nightly bench --bench atomic_cell
   ```

4. 用 loom cfg 编译，确认 `primitive` 抽象层切换成功（不要求跑通全部用例，确认能编译即可）：

   ```bash
   RUSTFLAGS="--cfg crossbeam_loom" cargo build --features std
   ```

**需要观察的现象**：

- 第 1 步：所有测试通过。
- 第 2 步：能看到 `wait_group` 的两个测试 `wait`、`wait_and_drop`。
- 第 3 步：输出每个 bench 的平均耗时（ns/iter）。
- 第 4 步：编译时 loom 版 `primitive` 生效；但 `AtomicCell`、`thread`、`ShardedLock` 因 loom 盲区**不会**被编译，相关测试会被跳过。

**预期结果**：测试全绿；bench 给出数值；loom 编译成功但可用类型变少。

**待本地验证**：第 3、4 步是否成功取决于本机是否装了 nightly 工具链和（loom 模式下）`loom` 依赖能否解析。若未安装 nightly，可跳过第 3 步；loom 模式如报 `loom` 版本问题，记录报错即可。

#### 4.3.5 小练习与答案

**练习 1**：为什么 crate 内部要用 `primitive::sync::atomic::*` 而不直接用 `core::sync::atomic::*`？
**答案**：为了可测试性。在 `--cfg crossbeam_loom` 下，`primitive` 指向 `loom` 的实现，让同一份源码能在 loom 的交错模型下被穷举验证；否则指向标准库。这是一层「为并发测试引入的抽象」。

**练习 2**：`cargo test` 默认会编译 `benches/` 吗？
**答案**：不会。`benches/` 需要 nightly 的 `#![feature(test)]`，且只有 `cargo bench` 才会编译它们。`cargo test` 只编译 `tests/` 和 `src/` 里的 `#[test]`。

**练习 3**：为什么 `AtomicCell` 在 `cfg(crossbeam_loom)` 下不可用？
**答案**：见 [src/atomic/mod.rs:21-26](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/mod.rs#L21-L26) 的注释——loom 的原子类型「内存表示与底层类型不同」，而 `AtomicCell` 恰恰依赖把用户类型 transmute 成原生原子类型，两者不兼容，故在 loom 下直接禁用。注释里留了 TODO：新版 loom 支持 fence 后，基于 SeqLock 的回退路径可能可以在 loom 下用。

---

## 5. 综合实践

把本讲三个模块串起来，做一个**特性矩阵对照实验**。这是本讲的主实践任务。

**任务**：用三种 feature 配置编译 crossbeam-utils，并整理出一张「哪些类型可用」的对照表。

**操作步骤**：

1. 准备一个最小检查程序 `check_api.rs`（**示例代码**，非项目原有文件），尝试 `use` 各类公开 API：

   ```rust
   // 示例代码：仅用于探测哪些 API 在当前 feature 下可见
   #![allow(dead_code)]
   use crossbeam_utils::Backoff;                 // 无门控
   use crossbeam_utils::CachePadded;             // 无门控
   #[cfg(feature = "atomic")]
   use crossbeam_utils::atomic::{AtomicCell, AtomicConsume};
   #[cfg(feature = "std")]
   use crossbeam_utils::sync::{Parker, WaitGroup, ShardedLock};
   #[cfg(feature = "std")]
   use crossbeam_utils::thread;

   fn main() {
       // 仅做存在性引用，不真正调用
       let _ = Backoff::new();
       let _ = CachePadded::new(0u8);
       #[cfg(feature = "atomic")]
       let _ = AtomicCell::new(0u8);
       #[cfg(feature = "std")]
       { let _ = Parker::new(); }
   }
   ```

2. 分别用三种配置运行 `cargo test`（注意是测 crossbeam-utils 自带测试，验证 crate 在该配置下能编译并通过）：

   ```bash
   # 配置 A：默认 (std)
   cargo test

   # 配置 B：关掉所有默认特性
   cargo test --no-default-features

   # 配置 C：在默认基础上加 atomic
   cargo test --features atomic
   ```

3. 针对每种配置，对照 [src/lib.rs:85-100](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs#L85-L100) 的门控，把可用性填入下表。

**预期对照表**（按本讲源码推导，需本地验证）：

| 类型 \ 配置 | default (`std`) | `--no-default-features` | `--features atomic` (= std + atomic) |
|---|:---:|:---:|:---:|
| `Backoff` | ✅ | ✅ | ✅ |
| `CachePadded` | ✅ | ✅ | ✅ |
| `AtomicCell` | ❌ | ❌ | ✅ |
| `AtomicConsume` | ❌ | ❌ | ✅ |
| `Parker` / `WaitGroup` / `ShardedLock` | ✅ | ❌ | ✅ |
| `thread::scope` | ✅ | ❌ | ✅ |

> 关键洞察：`AtomicCell` 和 `AtomicConsume` 在默认配置下都**不可用**——因为整个 `atomic` 模块由 `feature="atomic"` 门控（[src/lib.rs:85-87](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs#L85-L87)）。这纠正了一个常见误解：README 给 `AtomicConsume` 标了 `(no_std)`，那只代表「不需要 std」，并不代表「默认可用」。

**需要观察的现象**：

- 配置 B 下，`tests/` 里依赖 `std` 或 `atomic` 的测试文件会**编译失败**或被跳过——记录哪些文件受影响。
- 配置 A 下，`tests/atomic_cell.rs` 因为 `use crossbeam_utils::atomic::AtomicCell;` 而**无法编译**（默认没开 atomic）。这是一个值得记录的现象：默认 `cargo test` 并不能跑全部测试，需要 `--features atomic`。

**待本地验证**：上表的每一格请在本机实测确认；尤其注意配置 A 下 `tests/atomic_cell.rs` 的编译表现，以及配置 B 下 `benches/` 是否还能编译。

---

## 6. 本讲小结

- crossbeam-utils 只有两个真特性：`std`（默认开）和 `atomic`（默认关，且因依赖 `atomic-maybe-uninit` 单独要求 Rust 1.74）。crate 整体 MSRV 是 1.56。
- `Cargo.toml` 的 feature 只能表达「用户意图」，**平台相关**的判断（目标是否支持原子、是否跑 sanitizer）由 `build.rs` 在编译期查表后用 `cargo:rustc-cfg` 动态发出。
- build.rs 发出三个 cfg：`crossbeam_no_atomic`（目标在 `no_atomic.rs` 黑名单）、`crossbeam_sanitize_thread`（ThreadSanitizer）、`crossbeam_atomic_cell_force_fallback`（任意 sanitizer 时强制 AtomicCell 走全局锁回退）。
- crate 内部的 `primitive` 抽象层在 `loom` 实现与标准库实现间二选一，使同一份源码既能在生产用，也能在 loom 下穷举线程交错；代价是 `AtomicCell`/`ShardedLock`/`thread::scope` 在 loom 下不可用。
- 测试有三套入口：`tests/`（正确性，stable）、`benches/`（性能，需 nightly）、loom 模型测试（`RUSTFLAGS="--cfg crossbeam_loom"`）；默认 `cargo test` 不开 `atomic`，跑不到 `AtomicCell` 相关测试。
- 完整闭环：`Cargo.toml` 定义 feature → `build.rs` 按平台发 cfg → `src/` 用 `cfg` 裁剪代码 → `tests/` 用 `cfg!` 消费这些开关做条件断言。

---

## 7. 下一步学习建议

本讲建立了「特性与 cfg 如何驱动编译」的全局观，接下来建议：

1. **进入 atomic 模块的实现**：先读 [u2-l1 AtomicCell 公共 API 与数据结构](u2-l1-atomiccell-api.md)，理解 `AtomicCell` 的 `new/load/store/swap/compare_exchange`，为后续的无锁路径（u2-l2）和全局锁回退（u2-l3，它会用到本讲的 `force_fallback` cfg）打基础。
2. **想先看 no_std 友好的小原语**：可跳读 [u2-l5 Backoff](u2-l5-backoff.md) 和 [u2-l6 CachePadded](u2-l6-cachepadded.md)，它们无门控、依赖少，最适合作为第一段「读实现」的练习。
3. **对 build.rs / cfg 机制还想深入**：在读完 u2-l3（SeqLock 回退）后，回到 [u5-l3 跨平台 cfg、loom 抽象与宽 SeqLock](u5-l3-cfg-loom-wideseqlock.md)，那里会系统讲 `seq_lock_wide.rs`、`target_has_atomic` 与 16/32 位指针宽度的处理。
