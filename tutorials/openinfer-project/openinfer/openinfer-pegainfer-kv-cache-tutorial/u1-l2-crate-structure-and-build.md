# crate 结构、依赖与本地构建测试

> 学习阶段：入门（u1-l2） · 依赖上一讲 [u1-l1 PegaInfer 全景与 kv-cache 的位置](u1-l1-project-overview.md)

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `pegainfer-kv-cache` 五个源码模块（`buffer` / `layout` / `manager` / `pool` / `view`）各自的分工，并凭行数分布判断出核心文件。
2. 逐条解释 `Cargo.toml` 里每个 workspace 依赖在这个 KV 缓存 crate 中扮演的角色（`cudarc`、`kvbm-logical`、`dynamo-kv-hashing`、`half`、`tokio` 等）。
3. 掌握只针对本 crate 的 cargo 构建/测试命令，并且能区分两类测试：**无需 GPU 的纯逻辑池测试**（`tests/restore_commit.rs`）与**需要 CUDA 设备的集成测试**（`tests/lifecycle.rs`）。

## 2. 前置知识

承接 u1-l1 已建立的全景认知（逻辑块池 `BlockPool` 与物理显存 `KvBuffer` 两层分离），本讲补充几个 Rust 工程侧的基础概念：

- **crate 与 package**：一个 package（有自己的 `Cargo.toml`）编译成一个 crate。`pegainfer-kv-cache` 就是一个 library package，被各模型 crate 依赖。
- **workspace（工作区）**：多个 package 共享一份 `Cargo.toml` 顶层清单和同一个 `Cargo.lock`。PegaInfer 的 workspace 根在仓库根目录，`pegainfer-kv-cache` 是其中一个成员。workspace 的最大好处是**依赖版本单一来源**：所有成员通过 `workspace = true` 继承同一份版本声明，避免「同一个依赖出现两个不兼容版本」。
- **cargo 基础命令**：`cargo build` 编译、`cargo test` 跑测试；`-p <包名>` 把操作限定到某一个 package；`--test <名字>` 只跑 `tests/` 下某个集成测试文件；`--lib` 只跑内联在源码里的单元测试。
- **两类测试的位置约定**：Rust 中 `src/` 源码内 `#[cfg(test)] mod tests` 是单元测试（随 `--lib` 运行）；`tests/` 目录下每个 `.rs` 是一个独立编译的集成测试（只能访问 crate 的**公共** API）。
- **bf16**：brain float 16，一种 16 位浮点格式（8 位指数 + 7 位尾数），显存占用是 fp32 的一半，是本 crate KV 缓存显存的元素类型。
- **为什么强调 `--release`**：仓库约定 GPU/CUDA 相关代码在 debug 构建下极慢、会超时，所以一切构建和测试命令都带 `--release`。

## 3. 本讲源码地图

先看这个 crate 一共有哪些文件（行数来自 `wc -l`，见 4.1.4 的实践）：

| 文件 | 行数 | 作用 |
|---|---|---|
| `Cargo.toml` | 17 | 包声明 + 7 个 workspace 依赖 + lint 继承 |
| `src/lib.rs` | 19 | crate 入口：声明 5 个私有模块，再导出公共类型 |
| `src/pool.rs` | 1009 | **核心**：`BlockPool` 逻辑块池与 `RequestKv` 请求生命周期 |
| `src/buffer.rs` | 100 | `KvBuffer`：持有 GPU 显存但「不带分配器」 |
| `src/layout.rs` | 33 | `KvLayout`：页优先几何的纯值类型 |
| `src/manager.rs` | 112 | `KvCacheManager`：池 + 缓冲配对的引擎级门面 |
| `src/view.rs` | 101 | `KvView` / `KvViewDesc`：交给注意力内核的只读视图 |
| `tests/restore_commit.rs` | 56 | 纯逻辑池测试，**无需 GPU** |
| `tests/lifecycle.rs` | 270 | 端到端生命周期测试，**需要 CUDA 设备** |
| `../CLAUDE.md`、根 `Cargo.toml` | — | workspace 级构建/测试约定与依赖版本单一来源 |

## 4. 核心概念与源码讲解

### 4.1 五个源码模块的分工

#### 4.1.1 概念说明

一个 crate 的 `lib.rs` 就是它的「目录牌」：声明有哪些模块、对外暴露哪些名字。本 crate 五个模块恰好对应 KV 缓存的五个正交关注点：

| 模块 | 一句话职责 | 关键词 |
|---|---|---|
| `layout` | 纯几何计算：给定层数/头数/头维度/页大小，推导各步长 | 无 GPU、无分配 |
| `buffer` | 一次性持有 `num_blocks × page_stride` 的 bf16 显存 | 有显存、无分配器 |
| `pool` | 发放/共享/回收逻辑块，管理每请求 KV 生命周期 | 有分配器、无显存 |
| `manager` | 把同几何的池和缓冲配成一台「引擎级 KV 缓存」 | 组合门面 |
| `view` | 把请求的块 ID 列表打包成注意力内核要的形状 | 只读、轻量 |

这正是 u1-l1 讲过的「逻辑/物理两层分离」在文件层面落地的样子：`pool` 完全不碰 GPU（所以它能被无 GPU 的测试覆盖），`buffer` 完全不做分配决策（所以指针永远稳定，CUDA Graph 捕获才安全），两者靠块 ID 这一抽象关联。

#### 4.1.2 核心流程

五个模块的内部依赖方向（箭头表示「使用」）：

```text
manager ──→ pool      （持有逻辑池）
manager ──→ buffer    （持有物理缓冲）
buffer  ──→ layout    （按几何算总元素数）
view    ←─  pool      （RequestKv 产出页索引，KvView 携带它）
view    ──→ buffer/layout（KvViewDesc 打包底层 buffer 与 layout 给内核）
pool    ──→ kvbm-logical（外部依赖：块管理器与哈希链）
```

要点：`layout` 处于依赖链最底端（33 行、零依赖的纯值类型），`pool` 是体量和逻辑上的核心（1009 行，占 src 总行数约 72%），`manager` 只是薄薄的组合层。

#### 4.1.3 源码精读

先看目录牌。[src/lib.rs:L1-L5](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/lib.rs#L1-L5) 用五个 `mod` 声明把源码切成五块——注意都是**私有模块**，外部只能走下面的再导出：

```rust
mod buffer;
mod layout;
mod manager;
mod pool;
mod view;
```

[src/lib.rs:L7-L19](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/lib.rs#L7-L19) 是整个 crate 的公共 API 表面，只有 12 行：

```rust
pub use buffer::KvBuffer;
pub use kvbm_logical;
pub use kvbm_logical::events::KvCacheEvent;
pub use layout::KvLayout;
pub use manager::KvCacheManager;
pub use pool::BlockPool;
pub use pool::KvBlockGuard;
pub use pool::LoadReservation;
pub use pool::PrefixProbe;
pub use pool::RegisteredBlock;
pub use pool::RequestKv;
pub use view::KvView;
pub use view::KvViewDesc;
```

这告诉我们两件事：公共类型一共 11 个自有类型（其中 6 个来自 `pool`，印证它是核心），外加两条**再导出**——把上游依赖 `kvbm_logical` 整个和它的 `KvCacheEvent` 事件类型直接透出给下游，模型 crate 不必再单独依赖 kvbm-logical。

各模块开头的文档注释把分工说得很直白，值得逐个点开：

- [src/layout.rs:L1-L4](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/layout.rs#L1-L4)："Page-first geometry ... Pure value type — no GPU, no allocation."（页优先几何……纯值类型——无 GPU、无分配。）
- [src/buffer.rs:L16-L19](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/buffer.rs#L16-L19)："GPU KV cache buffer **without an allocator**"——拥有设备内存与布局几何，但把块分配委托给外部 `BlockManager`（kvbm-logical）。
- [src/manager.rs:L10-L14](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/manager.rs#L10-L14)：全注意力模型的引擎级 KV 缓存 = 一个逻辑 `BlockPool` + 一个同几何 GPU `KvBuffer`；并明确 MLA 模型（kimi-k2）绕过这个门面、直接消费 `BlockPool` 并自持物理缓冲。
- [src/view.rs:L7-L10](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/view.rs#L7-L10)："Lightweight, non-owning view"——每次前向传播前由已分配的块 ID 构建，块生命周期由外部 `BlockManager` 管理。
- `pool.rs` 内部的公共结构体位置（本讲只看骨架，细节留给 u2）：[`BlockPool` 定义于 src/pool.rs:L31](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L31)、[`RequestKv` 于 L379](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L379)、前缀探测/加载预留/事件相关的 [`PrefixProbe`(L263)](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L263)、[`KvBlockGuard`(L350)](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L350)、[`LoadReservation`(L354)](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L354)、[`RegisteredBlock`(L728)](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L728) 都聚在这个文件里，文末 L741-L742 还挂着内联单元测试 `mod tests`。

#### 4.1.4 代码实践

**实践目标**：用行数分布亲手验证「哪个文件是核心」，并对照 lib.rs 的再导出理解模块归属。

**操作步骤**：

1. 在 crate 目录（`pegainfer-kv-cache/`）执行：

   ```bash
   wc -l src/*.rs tests/*.rs
   ```

2. 对照上表核对每个文件的行数。
3. 打开 `src/lib.rs`，为 4.1.1 表格中每个模块名找到它对应的 `pub use` 行，确认 6 个池类型确实全部来自 `pool`。

**需要观察的现象**（以下为笔者在本仓库实际运行 `wc -l` 的真实输出）：

```text
 100 src/buffer.rs
  33 src/layout.rs
  19 src/lib.rs
 112 src/manager.rs
1009 src/pool.rs
 101 src/view.rs
 270 tests/lifecycle.rs
  56 tests/restore_commit.rs
```

**预期结果**：`pool.rs` 以 1009 行一骑绝尘——src 目录合计约 1374 行，`pool.rs` 独占约 72%。这说明本 crate 的智力密度几乎全在「逻辑块池与请求生命周期」上，`buffer`/`view` 反而各只有百行左右，因为「无分配器的显存持有」和「只读视图」本身就该是薄层。这也解释了后续课程安排：u2 的五讲里有三讲都在解剖 `pool.rs`。

#### 4.1.5 小练习与答案

**练习 1**：`lib.rs` 里五个 `mod` 声明都是私有的，外部使用者怎么拿到 `KvBuffer`？

**参考答案**：通过 `pub use buffer::KvBuffer;` 这条再导出。私有模块 + 选择性 `pub use` 是控制 API 表面的标准手法——模块内部实现细节（比如 `buffer.rs` 里的 `Inner` 结构体）对外完全不可见，公共清单一目了然。

**练习 2**：为什么 `KvBuffer` 的文档注释特意强调「without an allocator」？如果把分配器做进 `KvBuffer` 会破坏什么？

**参考答案**：强调的是职责单一。「无分配器」意味着显存一次分配、永不移动、页的发放与回收完全交给逻辑池。若 `KvBuffer` 自己带分配器，显存地址就可能随分配/释放而变化，指针稳定性被破坏——CUDA Graph 捕获的内核地址会失效，u1-l1 提到的「CUDA Graph 捕获安全的前提」也就不成立了。

**练习 3**：`lib.rs` 为什么还要 `pub use kvbm_logical;` 把一个**依赖**整个再导出？

**参考答案**：让下游模型 crate 能通过 `pegainfer_kv_cache::kvbm_logical::...` 使用上游块管理器的类型（例如事件、块注册相关类型），而不必在自己的 `Cargo.toml` 里再声明一次 kvbm-logical 依赖。配合 `pub use kvbm_logical::events::KvCacheEvent;`，保证下游拿到的 `KvCacheEvent` 与本 crate 事件通道里流动的是**同一个类型**（类型同一性），否则 broadcast channel 的收发双方会对不上。

### 4.2 Cargo.toml：edition 2024 与 workspace 依赖继承

#### 4.2.1 概念说明

这个 crate 的 `Cargo.toml` 只有 17 行——因为所有能外包给 workspace 的信息都外包了。这是大型 Rust 仓库的典型风格：

- **版本单一来源**：依赖的版本号、feature、来源（registry / git / path）全部声明在 workspace 根的 `[workspace.dependencies]` 一张表里，成员 crate 只写 `workspace = true`。
- **edition 2024**：Rust 2024 版次（较新的语言标准，需要较新的 cargo）。本 crate 直接写 `edition = "2024"`，与 workspace 的 `edition = "2024"` 保持一致。
- **lint 继承**：`[lints] workspace = true` 让 workspace 统一的 clippy/rustc lint 配置对成员生效。

#### 4.2.2 核心流程

依赖解析的流程可以画成：

```text
pegainfer-kv-cache/Cargo.toml          仓库根 Cargo.toml
─────────────────────────             ─────────────────────────────
[dependencies]                        [workspace.dependencies]
cudarc  = { workspace = true } ──────→ cudarc = { version = "0.19.3", features = [...] }
kvbm-logical = { workspace = true } ─→ kvbm-logical = { path = "kvbm/kvbm-logical" }
dynamo-kv-hashing = { workspace… } ──→ dynamo-kv-hashing = { git = "...dynamo", rev = "364cc8…" }
（其余同理）
```

编译时 cargo 先读成员清单，遇到 `workspace = true` 就去根清单查真实来源，最终锁进共享的 `Cargo.lock`。workspace 根还明确要求：所有 ai-dynamo/dynamo 的 git 依赖必须钉在同一个 rev 上，否则同源 crate 的共享内部依赖会分裂成两个类型。

#### 4.2.3 源码精读

本 crate 的清单全文只有三段。[Cargo.toml:L1-L5](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/Cargo.toml#L1-L5) 是包声明（名字 `pegainfer-kv-cache`、版本 0.1.0、edition 2024、Apache-2.0）：

```toml
[package]
edition = "2024"
license = "Apache-2.0"
name = "pegainfer-kv-cache"
version = "0.1.0"
```

[Cargo.toml:L7-L14](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/Cargo.toml#L7-L14) 是 7 个依赖，全部 `workspace = true`：

```toml
[dependencies]
anyhow = { workspace = true }
cudarc = { workspace = true }
dynamo-kv-hashing = { workspace = true }
half = { workspace = true }
kvbm-logical = { workspace = true }
pegainfer-kernels = { workspace = true }
tokio = { workspace = true }
```

[Cargo.toml:L16-L17](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/Cargo.toml#L16-L17) 继承 workspace 的 lint 配置。

每个依赖的真实来源在仓库根 [Cargo.toml:L40-L47](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/Cargo.toml#L40-L47)，其中两条最值得注意：

```toml
dynamo-kv-hashing = { git = "https://github.com/ai-dynamo/dynamo", rev = "364cc8aa…" }
kvbm-logical = { path = "kvbm/kvbm-logical" }
```

结合源码里的实际使用点，逐个依赖的角色如下：

| 依赖 | 来源（根 Cargo.toml） | 在本 crate 里的角色 | 源码证据 |
|---|---|---|---|
| `cudarc` | registry，0.19.3，带 cuda-12090 等特性 | CUDA 驱动 API 的安全封装：`CudaSlice` 分配、`DevicePtr` 取地址、`memcpy_dtod_async` 整页拷贝 | [src/buffer.rs:L3-L5](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/buffer.rs#L3-L5)、[src/buffer.rs:L96](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/buffer.rs#L96) |
| `kvbm-logical` | **本地 path 依赖**（`kvbm/kvbm-logical`，fork 自 NVIDIA dynamo） | 块管理器 `BlockManager`、不可变块 `ImmutableBlock`、块哈希链、事件 `KvCacheEvent`——`BlockPool` 的地基 | [src/pool.rs:L31](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L31) 的 `BlockPool` 即包装它；[src/manager.rs:L4](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/manager.rs#L4) |
| `dynamo-kv-hashing` | git，钉死单 rev | 加盐哈希 `compute_salt_hash`：用 LoRA 名做盐隔离不同适配器的前缀缓存 | [src/pool.rs:L3](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L3)、[src/pool.rs:L174-L175](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L174-L175) |
| `half` | registry，2.7 | `bf16` 类型：KV 显存与设备拷贝的元素类型 | [src/buffer.rs:L6](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/buffer.rs#L6)、[src/view.rs:L2](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/view.rs#L2) |
| `tokio` | registry，1.x（full features） | 只用 `sync::broadcast`：给缓存感知路由广播 KV 块事件的多消费者通道 | [src/manager.rs:L5](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/manager.rs#L5)、`new_with_events` 于 [L85-L103](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/manager.rs#L85-L103) |
| `anyhow` | registry | 运行时错误处理：所有构造函数返回 `anyhow::Result` | 如 [src/buffer.rs:L38](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/buffer.rs#L38) |
| `pegainfer-kernels` | workspace path 依赖 | 已声明，但当前 `src/` 与 `tests/` 中均无 `pegainfer_kernels::` 路径的直接引用；具体用途**待确认**（可能是链接层面或预留依赖） | 无 |

诚实标注：`pegainfer-kernels` 这一项在依赖表里存在、代码里却搜不到直接使用，这正是「读 manifest 要对照源码验证」的好例子——不臆断、存疑待查。

#### 4.2.4 代码实践

**实践目标**：亲手完成一次「依赖声明 → 真实来源 → 源码使用点」的三点对齐。

**操作步骤**：

1. 读 [pegainfer-kv-cache/Cargo.toml:L7-L14](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/Cargo.toml#L7-L14)，列出 7 个依赖名。
2. 打开仓库根 `Cargo.toml` 的 `[workspace.dependencies]` 段（[L40-L47](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/Cargo.toml#L40-L47) 起，`cudarc` 在 [L78-L88](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/Cargo.toml#L78-L88)、`tokio` 在 [L172](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/Cargo.toml#L172)），为每个依赖填上「来源类型」（path / git / registry）。
3. 用下面的命令在 crate 目录验证各依赖在源码中的使用点（只读搜索，不改任何文件）：

   ```bash
   grep -rn "cudarc" src/ | head
   grep -rn "kvbm_logical" src/ | head
   grep -rn "compute_salt_hash" src/
   grep -rn "broadcast" src/
   ```

**需要观察的现象**：每个依赖名都能在 `src/` 里搜到 `use` 或路径引用——除了 `pegainfer_kernels`（搜不到）。

**预期结果**：你会得到与 4.2.3 表格一致的结论；`dynamo_kv_hashing::compute_salt_hash` 恰好只在 `pool.rs` 的加盐链路出现一次（L174-L175）。若 `pegainfer_kernels` 搜不到使用点，不要硬找理由，如实记「待确认」即可。以上 grep 命令笔者已用等价的检索方式核实过结论；完整运行输出**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`kvbm-logical` 为什么用 path 依赖（本地 fork），而 `dynamo-kv-hashing` 用 git 依赖？

**参考答案**：`kvbm-logical` 是本仓库 fork 的 dynamo 组件（见根 Cargo.toml workspace members 里 `kvbm/kvbm-logical` 与注释「forked from upstream」），本地 fork 意味着引擎可以直接改它的内部实现（比如 #704 提到的注册表 PRT 修复），path 依赖让改动即时生效；`dynamo-kv-hashing` 只需要它的稳定哈希函数，不需要改，钉住上游某个 rev 即可。同时根清单要求 dynamo 的 git 依赖统一 rev，保证 `dynamo-tokens` 等共享内部依赖类型唯一。

**练习 2**：如果每个模型 crate 各自在自己的 `Cargo.toml` 里写 `cudarc = "0.19"`，会发生什么？

**参考答案**：版本漂移风险——某个 crate 写 `0.18`、另一个写 `0.19` 时，cargo 会解析出两个不兼容的 cudarc，`CudaSlice` 等类型出现两套身份，跨 crate 传参直接编译失败（或静默重复编译）。workspace 继承从机制上消灭这类问题，这正是 `workspace = true` 的价值。

**练习 3**：`half` crate 在这个 crate 里的唯一用途是什么？

**参考答案**：提供 `bf16` 类型。`KvBuffer` 的显存是 `CudaSlice<bf16>`（buffer.rs L11），`copy_page` 计算字节数时用 `size_of::<bf16>()`（每元素 2 字节，buffer.rs L91），`view.rs` 同样引用它描述内核视图的数据类型。

### 4.3 测试分层：纯逻辑池测试 vs 需 CUDA 的生命周期测试

#### 4.3.1 概念说明

本 crate 只有两个集成测试文件，但它们被刻意分成了两层：

- **`tests/restore_commit.rs`——纯逻辑层**：只测 `BlockPool` 这个 CPU 数据结构（块发放、前缀恢复、哈希注册），不碰任何 GPU。文件顶部的文档注释自述「Pure logical-pool tests, no GPU」。这意味着没有 NVIDIA 显卡的机器（CI 的普通 runner、你的笔记本）也能完整跑它。
- **`tests/lifecycle.rs`——设备集成层**：走 `KvCacheManager` 端到端建池、预填、解码、释放，第一步就是创建 CUDA 上下文，必须有真实 GPU。

为什么能这样分层？因为 4.1 讲的架构把「逻辑池」与「物理显存」解耦了：池的全部行为（调度成功吗、块够不够、前缀命中几块）都不依赖显存，只有真正要把 KV 写进显存、生成视图交给内核时才需要设备。分层测试是这种解耦的直接红利。

另外还有第三层：`src/pool.rs` 末尾 [L741-L742](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L741-L742) 的内联 `#[cfg(test)] mod tests` 单元测试（守护哈希稳定性等内部不变量），它们随 `cargo test --lib` 运行，同样不需要 GPU。

#### 4.3.2 核心流程

两类测试的构成对比：

| | restore_commit.rs | lifecycle.rs |
|---|---|---|
| 入口对象 | `BlockPool::new(BLOCK_SIZE, 64)` 直接建池 | `KvCacheManager::new(&stream, 1, 1, 1, 16, num_blocks)` |
| 是否需要 GPU | 否 | 是（`CudaContext::new(0)`） |
| 覆盖路径 | 前缀探测 → 预留 → 提交 → 再匹配 | 建池 → 请求 → 预填 → 解码循环 → 释放 |
| 运行条件 | 任何能编译的机器 | 有 CUDA 设备与驱动 |

#### 4.3.3 源码精读

[tests/restore_commit.rs:L1-L5](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/tests/restore_commit.rs#L1-L5) 的文档注释把定位、范围和额外的性能锚职责一次说清：

```rust
//! Restore-commit path: reserved blocks staged + registered under the probe's
//! continuation hashes must be re-matchable as one contiguous prefix. Pure
//! logical-pool tests, no GPU. Also serves as the registration-cost regression
//! anchor for #704 (a 1024-block commit is ~1.6ms after the registry PRT fix;
//! it was 36ms before).
```

译解：恢复-提交路径——预留的块在探测器的 continuation 哈希下登记后，必须能作为**一个连续前缀**被重新匹配；纯逻辑池测试，无 GPU；同时兼作 #704 的注册成本回归锚（注册表 PRT 修复后 1024 块的提交约 1.6ms，修复前 36ms）。一个测试文件同时承担「行为正确性」和「性能回归锚」两个职责，这是本仓库很有特色的工程实践。

第一个测试 [tests/restore_commit.rs:L11-L29](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/tests/restore_commit.rs#L11-L29) 全程只见 `pool.` 调用，不见任何 CUDA 类型：`probe_prefix` 探测 → `reserve_loaded_blocks(33)` 预留 → `commit_loaded_blocks` 提交 → 新请求 `match_and_add_prefix` 断言命中整段前缀（33 × 16 个 token）。

反观 [tests/lifecycle.rs:L3-L8](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/tests/lifecycle.rs#L3-L8) 的辅助函数第一行就是设备调用：

```rust
fn make_manager(num_blocks: usize) -> KvCacheManager {
    let ctx = cudarc::driver::CudaContext::new(0).expect("CUDA context");
    let stream = ctx.default_stream();
    // Tiny geometry: 1 layer, 1 KV head, dim=1, block_size=16
    KvCacheManager::new(&stream, 1, 1, 1, 16, num_blocks).expect("KvCacheManager::new")
}
```

`CudaContext::new(0)` 打开 0 号 GPU——没有设备的机器上这一步就会失败。注意它用的几何是特意缩小的（1 层、1 个 KV 头、维度 1、块大小 16），让显存开销降到最低。

第一个生命周期测试 [tests/lifecycle.rs:L10-L50](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/tests/lifecycle.rs#L10-L50) 走完「建池（32 块，1 块留给 padding，可用 31）→ 10 token 预填 → 5 步解码 → release 后可用块复原」的完整闭环，这条链路将在 u2-l2 逐行精读，本讲只需认识它的形状。

#### 4.3.4 代码实践

**实践目标**：在**没有 GPU 的机器上**跑通本 crate 的纯逻辑测试，体会上文的分层红利。

**操作步骤**：

1. 在仓库任意位置（workspace 内）执行：

   ```bash
   cargo test --release -p pegainfer-kv-cache --test restore_commit
   ```

2. 记录输出的测试名与结果（`commit_absorbs_full_prefix`、`commit_extends_partial_gpu_hit` 两个测试）。
3. 顺带跑内联单元测试：

   ```bash
   cargo test --release -p pegainfer-kv-cache --lib
   ```

**需要观察的现象**：编译阶段会拉取并编译 cudarc 等依赖（首次耗时较长）；运行阶段两个测试都应 `ok`，总耗时以毫秒计。

**预期结果**：`test result: ok. 2 passed; 0 failed`（`--test restore_commit`），以及 `--lib` 下 pool.rs 内联测试全部通过。注意：本讲义写作环境未执行 cargo（避免长时间依赖编译），以上预期是依据测试源码的断言推导的，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`restore_commit.rs` 里 `BlockPool::new(BLOCK_SIZE, 64)` 的两个参数分别是什么？`lifecycle.rs` 里 `KvCacheManager::new` 却有六个参数，多出来的参数都去哪了？

**参考答案**：`BlockPool::new(block_size, block_count)` 只描述逻辑池。`KvCacheManager::new(&stream, num_layers, num_kv_heads, head_dim, block_size, num_blocks)` 多出的 stream 与三个几何参数（层数、KV 头数、头维度）是给 `KvBuffer` 分配物理显存用的——`buffer.rs` 里总元素数为 \( \text{num\_blocks} \times \text{page\_stride} \)，而 `page_stride` 由那三个几何参数经 `KvLayout` 推出。逻辑池根本不需要几何信息，这正是两层解耦的体现。

**练习 2**：如果你想给 `BlockPool` 的前缀匹配逻辑加一个新测试，应该放进哪个文件？为什么？

**参考答案**：如果它只涉及逻辑行为，放 `tests/restore_commit.rs`（或新建一个纯逻辑集成测试文件），保持「无 GPU 也能跑」的性质；只有牵涉真实显存分配与内核视图的端到端行为才进 `tests/lifecycle.rs`。当然，若是测试 crate 私有内部（非公共 API），则应放 `src/pool.rs` 的内联 `mod tests`。

**练习 3**：`restore_commit.rs` 顶部注释提到的 #704 回归锚是什么意思？它防的是什么问题？

**参考答案**：它把一次真实性能事故固化成测试：曾经 1024 块的 `commit_loaded_blocks` 提交要 36ms（注册表的性能缺陷），修复（PRT）后约 1.6ms。这个测试文件常年在 CI 里跑同一条提交路径，一旦未来改动让注册成本回升，测试耗时的显著劣化就会被发现——用测试的存在性守住一条性能红线，而不只是功能正确性。

### 4.4 只构建/测试本 crate：命令与 workspace 约定

#### 4.4.1 概念说明

workspace 有 20 多个成员，全量构建一次代价不小。日常开发一个 crate 时，用 `-p`（`--package`）把 cargo 的作用域限制到目标包，能把编译时间和反馈循环缩到最小。同时要遵守仓库的两条硬约定：

1. **永远带 `--release`**——仓库指南原话：「Always use `--release` — debug builds are extremely slow for GPU/CUDA and will timeout」（GPU/CUDA 代码 debug 构建极慢、会超时）。
2. **区分测试的运行条件**——单元测试和纯逻辑集成测试随处可跑；需 GPU/模型权重的精度与集成测试必须在有设备的机器上运行。

#### 4.4.2 核心流程

本 crate 相关的常用命令一览（均可在 workspace 内任意目录执行）：

| 命令 | 作用 | 需要 GPU？ |
|---|---|---|
| `cargo build --release -p pegainfer-kv-cache` | 只编译本 crate 及其依赖 | 编译不需要 |
| `cargo test --release -p pegainfer-kv-cache --lib` | 只跑 src 内联单元测试（pool.rs L741 起） | 否 |
| `cargo test --release -p pegainfer-kv-cache --test restore_commit` | 只跑纯逻辑恢复-提交测试 | 否 |
| `cargo test --release -p pegainfer-kv-cache --test lifecycle` | 端到端生命周期测试 | **是** |
| `cargo test --release -p pegainfer-kv-cache` | 本 crate 全部测试 | 是（包含 lifecycle） |
| `cargo test --release --workspace --lib` | workspace 全部单元测试（仓库指南标注约 9 秒） | 否 |

补充两个技巧：单个测试可按名过滤并打印输出，例如 `cargo test --release -p pegainfer-kv-cache --test restore_commit commit_absorbs -- --nocapture`（`--` 之后是传给测试二进制的参数）；本 crate 没有 feature 门控，无需 `--features`。

#### 4.4.3 源码精读

这些约定的权威出处是仓库指南。构建约定在 [CLAUDE.md:L21-L36](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/CLAUDE.md#L21-L36)（「Build & Run」一节，L23 即 `--release` 铁律）；测试约定在 [CLAUDE.md:L61-L80](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/CLAUDE.md#L61-L80)：

```bash
# Unit tests (~9s)
cargo test --release --workspace --lib
...
# Accuracy and integration tests — require GPU + model weights
cargo test --release -p pegainfer-qwen3 --test hf_golden_gate
...
# Single test (filter by name)
cargo test --release --workspace --lib prefix_cache -- --nocapture
```

注意指南里「require GPU + model weights」标注的是模型 crate 的精度门测试（如 qwen3 的 `hf_golden_gate`，还要 `PEGAINFER_TEST_MODEL_PATH` 指向权重）；本 crate 的 `lifecycle.rs` 同样需要 GPU 但不需要模型权重——它的几何是测试里现造的迷你尺寸。

还有一条对贡献者有用的约定：CI 会跑 `cargo fmt --check`，提交前需 `cargo fmt`。虽然本教程只读不写源码，但你在练习中新建临时测试文件时也应保持格式化习惯。

#### 4.4.4 代码实践

**实践目标**：验证「只构建本 crate」的可行性，并用一次最小编译确认环境（Rust 工具链版本需支持 edition 2024）。

**操作步骤**：

1. 在 crate 目录执行：

   ```bash
   cargo build --release -p pegainfer-kv-cache
   ```

2. 观察输出里被编译的包列表：应该是 `anyhow`、`cudarc`、`dynamo-kv-hashing`（从 git 拉取）、`half`、`kvbm-logical`、`tokio` 等依赖 + `pegainfer-kv-cache` 本身，**不应**出现 `pegainfer-qwen3` 等模型 crate。
3. 接着跑 4.3.4 的 `restore_commit` 测试，完成「编译 → 测试」闭环。

**需要观察的现象**：首次构建会下载 git 依赖 dynamo-kv-hashing 并编译 cudarc（依赖树较大，耗时以分钟计）；二次构建几乎瞬时（增量缓存命中）。

**预期结果**：`Finished \`release\` profile` 且无错误；编译产物里不含任何模型 crate。与 4.3.4 一样，本讲义写作环境未执行该构建，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `cargo build -p pegainfer-kv-cache`（不带 `--release`）在本仓库被视为错误用法？

**参考答案**：仓库指南明确规定 GPU/CUDA 相关代码在 debug profile 下极慢且会超时（CLAUDE.md L23）。本 crate 链接到 cudarc，debug 构建的 CUDA 交互慢得不可用；更重要的是协作一致性——所有人、CI 都用 `--release`，行为才有可比性。

**练习 2**：执行 `cargo test --release -p pegainfer-kv-cache`（不带 `--test` 过滤）在一台无 GPU 的机器上会发生什么？

**参考答案**：`restore_commit` 与 `--lib` 单测正常通过，但 `lifecycle` 测试会在 `CudaContext::new(0).expect("CUDA context")` 处 panic 失败（找不到 CUDA 设备/驱动）。所以无 GPU 环境应显式用 `--lib` 和 `--test restore_commit` 分别运行。

**练习 3**：如何只运行 `restore_commit.rs` 里名字含 `partial` 的那一个测试，并看到测试内的打印输出？

**参考答案**：`cargo test --release -p pegainfer-kv-cache --test restore_commit partial -- --nocapture`。cargo 按「测试文件 → 名称子串」两级过滤，`--` 之后的 `--nocapture` 传给测试二进制，让 `println!` 输出不被吞掉（用法同仓库指南 CLAUDE.md L79 的示例）。

## 5. 综合实践

**任务：为 `pegainfer-kv-cache` 写一份「crate 体检报告」。** 把本讲的三块知识串成一次完整巡检，产出一份 Markdown 笔记（放你自己的目录，不要写进仓库）：

1. **结构面**：运行 `wc -l src/*.rs tests/*.rs`，画一张模块依赖小图（参考 4.1.2），标出核心文件与行数占比。
2. **依赖面**：抄录 `Cargo.toml` 的 7 个依赖，逐个追到仓库根 `[workspace.dependencies]` 的来源行，再在 `src/` 里各找一处使用证据（`pegainfer-kernels` 若找不到就如实写「待确认」）。
3. **构建面**：依次执行并记录结果——

   ```bash
   cargo build --release -p pegainfer-kv-cache
   cargo test --release -p pegainfer-kv-cache --lib
   cargo test --release -p pegainfer-kv-cache --test restore_commit
   ```

   记下每条命令的测试数量与耗时；如果你的机器有 GPU，再补一条 `--test lifecycle` 并对比它与 `restore_commit` 的耗时差距。
4. **结论面**：用三句话回答——这个 crate 的核心在哪、它凭什么能在无 GPU 环境下测试大半逻辑、workspace 继承给它带来了什么。

预期产出：一张行数表、一张依赖三列表（名字/来源/角色）、三条命令的运行记录、三句结论。所有运行结果以你本机实际输出为准；无法运行的条目标注「待本地验证」。

## 6. 本讲小结

- 本 crate 五模块正交分工：`layout`（纯几何，33 行）→ `buffer`（显存持有、无分配器，100 行）→ `pool`（逻辑池与请求生命周期，**1009 行、占 src 约 72%，是绝对核心**）→ `manager`（池 + 缓冲配对门面，112 行）→ `view`（内核只读视图，101 行）；公共 API 共 11 个自有类型外加 `kvbm_logical` 与 `KvCacheEvent` 两条再导出。
- `Cargo.toml` 只有 17 行：edition 2024、7 个依赖全部 `workspace = true` 继承、lint 也继承 workspace。依赖角色：cudarc 提供 CUDA 驱动 API，kvbm-logical（本地 fork）是块管理器地基，dynamo-kv-hashing（git 钉 rev）做 LoRA 加盐哈希，half 提供 bf16，tokio 只用 broadcast 事件通道，pegainfer-kernels 声明了但源码中无直接引用（待确认）。
- 测试分两层：`tests/restore_commit.rs` 自述「纯逻辑池测试、无 GPU」，兼作 #704 注册成本回归锚（36ms → 约 1.6ms）；`tests/lifecycle.rs` 第一步就 `CudaContext::new(0)`，必须有 GPU。分层的底气来自逻辑池与物理显存解耦的架构。
- 工程约定：一切命令带 `--release`（debug 构建的 CUDA 代码会超时）；`-p pegainfer-kv-cache` 限定作用域；`--lib` / `--test <名>` / 名称子串过滤 + `-- --nocapture` 精确控制运行的测试。

## 7. 下一步学习建议

下一讲 [u1-l3 分页 KV 与页优先布局 KvLayout](u1-l3-paged-kv-and-layout.md) 将进入第一个「机制」主题：为什么要把 KV 切成固定大小的页，以及 `KvLayout` 如何用三个乘法推导出 `kv_block_len`、`layer_stride`、`page_stride`。预习建议：`src/layout.rs` 全文只有 33 行，现在就通读它，数一数 `KvBuffer::new`（[src/buffer.rs:L26-L46](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/buffer.rs#L26-L46)）里 `total_elements = num_blocks * layout.page_stride` 这一行，下一讲你会彻底理解它的每一项是怎么来的。
