# 编译、测试与代码走读准备

## 1. 本讲目标

读完本讲，你应该能够：

1. 分清本 crate 里哪些测试**不需要 GPU**（`src/layout.rs` 与 `src/vllm_hash.rs` 的 `#[cfg(test)]` 单测，共 8 个），哪个测试**必须有真实 CUDA 设备**（`tests/cpu_roundtrip.rs`）。
2. 理解 `pegaflow-core` 作为 git 依赖被 pin 到固定 rev 的维护含义：为什么升级必须是一次显式的、可评审的 Cargo.toml 修改。
3. 会用 `-p`、`--lib`、`--test <name>`、测试名过滤精确选择要跑的测试，并理解 CLAUDE.md 里「Always use `--release`」这条全仓库约定对本 crate 的意义。

本讲不深入任何机制内部（那是 u2 的事），只解决一个问题：**在动源码之前，把验证环境搭对**。

## 2. 前置知识

### 2.1 cargo 的测试目标（test target）

cargo 把 Rust 测试分三种目标，本讲只涉及前两种：

| 种类 | 位置 | 编译方式 | 能看到什么 |
|------|------|----------|-----------|
| 单元测试 | `src/*.rs` 内联的 `#[cfg(test)] mod tests` | 编进库目标本体，`cargo test --lib` 运行 | 库的**全部**私有项（`pub(crate)`、私有函数都能直接调） |
| 集成测试 | `tests/*.rs`，每个文件编成**独立二进制** | `cargo test --test <文件名>` 运行 | 只能看到 crate 的**公开导出**（就像下游用户一样 `use pegainfer_kv_offload::...`） |
| 文档测试 | `///` 注释里的代码块 | `cargo test --doc` | 公开 API |

这条区分直接决定了本 crate 的测试布局：纯 CPU 的几何/哈希断言放在 `src` 内联（快、无硬件门槛），需要真实显存的端到端字节级回归放在 `tests/`（带 GPU 门槛、只走公开 API）。

### 2.2 git 依赖与 rev pin

workspace 内的 crate 互相用 path 依赖（`pegainfer-kv-cache = { path = ... }`），而 `pegaflow-core` 来自另一个 GitHub 仓库 `novitalabs/pegaflow`，不是本 workspace 成员。对这种外部仓库：

- `rev = "<完整 commit 哈希>"` 表示**钉死到一个具体提交**——哪怕上游 force-push、删分支，本仓库构建出来的字节也不变；
- 相对地，`branch = "master"` 会随上游漂移，今天绿明天红，且无法评审「到底升级了什么」。

`Cargo.lock` 会把这次锁定再冻结一层（记录 rev 与 checksum），所以「升级 pegaflow」在本项目里永远是一次**显式决策**：同时改 Cargo.toml 的 rev 并重新生成 lock。

### 2.3 为什么必须 `--release`

CLAUDE.md 在 Build & Run 一节的第一句就是全仓库红线：**debug 构建对 GPU/CUDA 代码极慢、会超时**——见 [CLAUDE.md:L23](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/CLAUDE.md#L23)。本 crate 的集成测试要发起真实的 D2H/H2D 拷贝（背后是 cudarc 驱动调用与 pegaflow runtime），debug 下慢得不可用，所以本讲所有 `cargo test` 命令都带 `--release`。

### 2.4 名词表

- **feature**：cargo 的条件编译开关。本 crate 对 pegaflow-core 用 `default-features = false` 关掉它的默认 feature，再显式打开 `rdma`。
- **workspace member**：虚拟 workspace 根 `Cargo.toml` 里 `members` 列出的成员目录，共享一套 `[workspace.dependencies]` 版本表。
- **golden vector（黄金向量）**：从参考实现（这里是 vLLM/Python）现场抓取的期望字节，测试只做逐字节比对——不解释算法对不对，只保证和参考完全一致。

## 3. 本讲源码地图

| 文件 | 在本讲中的角色 |
|------|----------------|
| [pegainfer-kv-offload/Cargo.toml](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/Cargo.toml) | 依赖声明：pegaflow-core 的 git rev pin、`default-features=false`、`rdma` |
| [pegainfer-kv-offload/src/layout.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/layout.rs) | 纯 CPU 单测 ×3：arena 注册几何、交错布局 size 上界、stride 断言 |
| [pegainfer-kv-offload/src/vllm_hash.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/vllm_hash.rs) | 纯 CPU 单测 ×5：对 vLLM 抓取的 golden vector 逐字节比对 |
| [pegainfer-kv-offload/tests/cpu_roundtrip.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/tests/cpu_roundtrip.rs) | 唯一的 GPU 集成测试：GPU→CPU→GPU 字节级 roundtrip |
| [Cargo.toml（workspace 根）](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/Cargo.toml) | `members` 列表与 path 依赖表 |
| [Cargo.lock](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/Cargo.lock) | 冻结 pegaflow rev 的证据 |
| [CLAUDE.md](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/CLAUDE.md) | 全仓库构建/测试约定的权威出处 |
| [pegainfer-kv-cache/src/layout.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/layout.rs) | `KvLayout` 定义——roundtrip 测试几何常量的「对账」依据 |

## 4. 核心概念与源码讲解

### 4.1 依赖声明：git rev pin、`default-features=false` 与 `rdma`

#### 4.1.1 概念说明

本 crate 只有 9 个直接依赖，其中 8 个走 workspace 版本表（`workspace = true`），唯一的例外是外部仓库 `pegaflow-core`——它是 host/SSD/RDMA 深层存储的所有者。对它的声明做了三件刻意的事：

1. **rev pin 到固定提交**：锁定在 landed PR #395（P 侧部分尾块保存 + 首 token 路由转发）的那个 master rev，包含 PR #381 的 metaserver flush barrier 与可嵌入的 `P2pTransferService`。也就是说，选哪个 rev 不只是「最新就好」，而是**选一个已知语义的版本**——P/D 生产者版本与默认布局都在部署文档里按这个 rev 钉死。
2. **`default-features = false`**：pegaflow 的默认 feature 会拉 `cuda-12`，进而带进一个 clashing 的 cudarc `cuda-12080` selector；workspace 自己的 cudarc 已经提供 `cuda-12090 + nvrtc + runtime`，足够覆盖 pegaflow-core 与 pegaflow-transfer 的需求。不关默认 feature 就会出现两套 CUDA 绑定打架。
3. **显式打开 `rdma`**：启用 pegaflow-transfer——跨节点块抓取（RDMA READ）+ MetaServer 发现，即 P/D 分离数据面。注意 rdma 是**无条件**打开的（不在本 crate 自己的 feature 后面），这是文档里已记录的打包欠账之一（u3-l5 会展开）。

#### 4.1.2 核心流程

依赖解析的先后关系（伪代码）：

```text
workspace 根 Cargo.toml
  ├─ [workspace.dependencies] 定义版本表          ← cudarc/half/log/... 的版本来自这里
  ├─ members 含 "pegainfer-kv-offload"            ← 本 crate 是 workspace 成员
  └─ pegainfer-kv-offload = { path = ... }        ← 成员间 path 依赖
pegainfer-kv-offload/Cargo.toml
  ├─ X = { workspace = true }                     ← 查表继承版本
  └─ pegaflow-core = { git = ..., rev = "...",    ← 外部仓库：钉死提交
                       default-features = false,
                       features = ["rdma"] }
Cargo.lock
  └─ 记录 source = "git+...?rev=1411...#1411..."  ← 再冻结一层，构建可复现
```

升级 pegaflow 的工作流因此固定为：改 Cargo.toml 里的 rev → `cargo update -p pegaflow-core` 刷新 lock → 全量测试 → 一次可评审的提交。

#### 4.1.3 源码精读

依赖声明集中在 Cargo.toml 的 `[dependencies]` 段，注释比代码还长——这段注释本身就是文档：

- [pegainfer-kv-offload/Cargo.toml:L8-L20](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/Cargo.toml#L8-L20)：注释说明为什么 pin 这个 rev（PR #395/#381 的语义）、为什么 `default-features=false`（cudarc selector 冲突）、`rdma` 打开了什么（跨节点 RDMA READ + MetaServer 发现）。
- [pegainfer-kv-offload/Cargo.toml:L21-L30](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/Cargo.toml#L21-L30)：实际声明。workspace 继承的 6 个常规依赖之外，pegaflow-core 完整写出 git/rev/feature 三元组；`tokio-stream` 额外开 `net` feature（host.rs 的 P2P serving 需要）。
- [Cargo.toml:L21-L22](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/Cargo.toml#L21-L22)：workspace `members` 里的两行，确认本 crate 的位置——与 `pegainfer-kv-cache` 相邻，都是被模型 crate 复用的底层库。
- [Cargo.toml:L132-L133](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/Cargo.toml#L132-L133)：workspace 版本表里给两个 kv crate 的 path 条目，这就是「`workspace = true` 能查到表」的出处。
- [Cargo.lock:L3509-L3511](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/Cargo.lock#L3509-L3511)：lock 里记录 `pegaflow-core` 版本 `0.23.5`、source 带 `?rev=14112403...#14112403...`——rev pin 落到 lock 的实证。

另外注意 Cargo.toml **没有 `[dev-dependencies]` 段**：[cpu_roundtrip.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/tests/cpu_roundtrip.rs) 用到的 `cudarc`、`half`、`pegainfer-kv-cache` 全都是常规依赖，集成测试目标天然可见，无需额外声明。

#### 4.1.4 代码实践

**实践目标**：亲眼确认「rev pin」不只是 Cargo.toml 里的一行字，而是被 lock 冻结的事实。

**操作步骤**：

1. 在仓库根目录打开 `Cargo.lock`，搜索 `name = "pegaflow-core"`；
2. 核对 lock 里 `source` 字段中的 rev 与 `pegainfer-kv-offload/Cargo.toml` 第 25 行的 rev 完全一致（应为 `14112403bc1d0ed5b44717c0a93743fb0de66fee`）；
3. （可选，需网络能拉 git 依赖，待本地验证）运行 `cargo tree -p pegainfer-kv-offload -i pegaflow-core`，观察它是由本 crate 直接引入的。

**需要观察的现象**：lock 的 rev 与 Cargo.toml 逐字符相同；`cargo tree` 显示 pegaflow-core 的父依赖就是 `pegainfer-kv-offload`。

**预期结果**：一致。若有人只改了 Cargo.toml 的 rev 而没刷新 lock，`cargo build` 会报 lock 与 manifest 不一致并要求更新——这正是 pin 机制在起作用。

#### 4.1.5 小练习与答案

1. **为什么用 `rev = "<40 位哈希>"` 而不是 `branch = "master"`？**
   答：branch 会随上游漂移，同一次 clone 在不同日期得到不同代码，无法复现、无法评审升级内容；rev 钉死提交，升级必须显式改这一行，diff 里一眼可见。
2. **如果去掉 `default-features = false` 会发生什么？**
   答：pegaflow 的默认 feature 会拉进它的 `cuda-12` 依赖，带入 cudarc 的 `cuda-12080` selector，与 workspace 已有的 `cuda-12090` 绑定冲突（两套 CUDA runtime 符号/类型不一致），编译失败或产生重复链接。
3. **`version = "0.23.5"` 这个版本号写在哪里？**
   答：不在本仓库任何 manifest 里，而是 Cargo.lock 在解析 git 依赖时记录的 pegaflow 仓库自身的 `Cargo.toml` 版本（[Cargo.lock:L3509-L3511](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/Cargo.lock#L3509-L3511)）——它是 lock 的产物，不是我们的输入。

### 4.2 测试分层：`--lib` 纯 CPU 单测 vs `tests/` GPU 集成测试

#### 4.2.1 概念说明

本 crate 的测试天然分成两层，对应两种硬件门槛：

- **`--lib` 单测（8 个，纯 CPU，任何机器可跑）**：全部位于两个 `#[cfg(test)] mod tests` 里——`src/layout.rs` 3 个（arena 注册几何映射）、`src/vllm_hash.rs` 5 个（对 vLLM 抓取的 golden vector 做逐字节比对）。它们只做整数算术与字节编码断言，完全不触碰 CUDA。这是 CLAUDE.md 所说「Unit tests (~9s)」快速门的一部分。
- **`tests/cpu_roundtrip.rs`（1 个测试，需要真实 CUDA 设备）**：整个文件只有一个 `#[test]`，从 `CudaContext::new(0)` 开始建立真实设备上下文。它的文件头注释明说：*Requires a CUDA GPU; skipped from `--lib` unit runs*。

一个容易误会的细节：`src/handle.rs` 里有 `settled` / `in_flight` 两个「测试构造器」，但它们**不是** `#[cfg(test)]` 单测——它们是给下游（pegainfer-qwen3 调度器）的测试用的公开工具，不属于本讲的 8 个之列（u2-l5 详述）。

这个分层与 CLAUDE.md 的测试命令一一对应：

```bash
# 纯 CPU 快速门（~9s，全 workspace）——本 crate 的 8 个单测在这里跑
cargo test --release --workspace --lib

# 需要 GPU + 模型权重的集成测试——单独点名
cargo test --release -p <crate> --test <name>
```

#### 4.2.2 核心流程

`cargo test` 的目标选择逻辑：

```text
cargo test -p pegainfer-kv-offload --lib --release
    → 只编译库目标 + 内联 #[cfg(test)]，运行 8 个单测（预测值，待本地验证）
cargo test -p pegainfer-kv-offload --test cpu_roundtrip --release
    → 只编译 tests/cpu_roundtrip.rs 这一个独立二进制并运行（需要 GPU）
cargo test -p pegainfer-kv-offload --test cpu_roundtrip -- --list
    → 只列出该二进制里的测试名，不执行（-- 之后是传给测试二进制的参数）
cargo test --release --workspace --lib vllm -- --nocapture
    → workspace 级单测运行，但按名字过滤含 "vllm" 的测试
```

最后一种「名字过滤 + `--nocapture`」是走读源码时最有用的姿势：只跑关心的几个测试，并打印它们内部 `println!` 的输出。

#### 4.2.3 源码精读

- **layout.rs 的 3 个单测**（[pegainfer-kv-offload/src/layout.rs:L115-L180](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/layout.rs#L115-L180)）：
  - `arena_registration_geometry`（L123-L152）——GLM5.2 形态：每层两个 arena（MLA latent + index-K sidecar），断言逐字段映射与 `size_bytes`；
  - `interleaved_arena_size_is_last_block_reach`（L157-L167）——qwen3 融合布局：`block_stride_bytes`(4096) > `bytes_per_block`(512) 时，size 上界是最后一块的到达界 `3*4096+512`，而非 `4*4096`；
  - `arena_copy_unit_must_fit_its_stride`（L169-L179）——`#[should_panic]`：拷贝单元大于步长时必须 panic（防呆）。
- **vllm_hash.rs 的 5 个单测**（[pegainfer-kv-offload/src/vllm_hash.rs:L124-L218](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/vllm_hash.rs#L124-L218)）：`none_hash_matches_vllm`、`cbor_encoding_matches_cbor2_canonical`、`block_chain_matches_vllm`、`realistic_block_size_matches_vllm`、`key_chain_covers_full_blocks_only`——全部是 2026-07-03 从 vLLM 抓取的 golden vector 逐字节断言（注释见 L128-L130），不依赖任何设备。
- **cpu_roundtrip.rs 的门槛声明**（[pegainfer-kv-offload/tests/cpu_roundtrip.rs:L1-L12](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/tests/cpu_roundtrip.rs#L1-L12)）：文档注释讲清它覆盖整个 connector（分层注册、K/V split、异步 save、前缀查询、进程内 load），并明确「需要 CUDA GPU；不会出现在 `--lib` 单测运行里」。
- **集成测试只能用公开 API**：对照 [pegainfer-kv-offload/src/lib.rs:L24-L34](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/lib.rs#L24-L34) 的导出面——测试文件 import 的 `OffloadConfig`、`OffloadEngine`、`QueryOutcome` 都在其中，而 `KvArena`/`Registration` 等 `pub(crate)` 内部件对它不可见。
- **全仓库测试约定**：[CLAUDE.md:L61-L79](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/CLAUDE.md#L61-L79) 给出单元测试（~9s）与「require GPU + model weights」集成测试的分界，L79 是按名过滤单测的示例命令。

#### 4.2.4 代码实践

**实践目标**：亲手跑出本 crate 的测试清单，验证「8 个纯 CPU 单测 + 1 个 GPU 集成测试」的分层。

**操作步骤**：

1. 在仓库根目录运行：
   ```bash
   cargo test -p pegainfer-kv-offload --lib --release
   ```
2. 把输出里 `test result: ok. N passed` 的 N 记下来，并按输出里的模块路径（`layout::tests::...` / `vllm_hash::tests::...`）分组；
3. 再运行（无 GPU 环境也能执行到「列出」这一步）：
   ```bash
   cargo test -p pegainfer-kv-offload --test cpu_roundtrip -- --list
   ```
4. 写一段 3-5 句的说明：为什么 `gpu_cpu_gpu_roundtrip_preserves_kv_bytes` 必须放在 `tests/` 而不是 `src` 内联单测。

**需要观察的现象**：步骤 1 输出应为 3 个 `layout::tests` + 5 个 `vllm_hash::tests` 共 **8 个通过**（依据源码清点得出的预测值，具体数字待本地验证）；步骤 3 会编译集成测试并列出 `gpu_cpu_gpu_roundtrip_preserves_kv_bytes`，但在没有 GPU 的机器上真正**运行**它（去掉 `--list`）会在 `CudaContext::new(0)` 处 panic。

**预期结果**：单测全绿；`--list` 能看到集成测试名。「必须放 tests/」的参考答案（三条例证，综合实践里还会展开）：

1. **硬件门槛隔离**：`--lib` 单测是全 workspace 的快速门（CLAUDE.md 标注 ~9s），必须能在任何无 GPU 的 CI 机器上全绿；把需要 CUDA 的用例内联进 `src`，最便宜的验证路径就带上了硬件要求。
2. **只测公开 API**：`tests/` 目标把 crate 当外部用户，只能 import `lib.rs` 导出的 11 个名字——这恰好保证「下游（pegainfer-qwen3）能用的接口面足以完成完整 save/query/load 闭环」，防止测试偷偷依赖 `pub(crate)` 内部。
3. **编译隔离**：每个 `tests/*.rs` 是独立 crate，只在显式 `--test` 时编译；内联 `#[cfg(test)]` 则进入每次 `cargo test --lib` 的编译图，拖慢所有日常验证。

#### 4.2.5 小练习与答案

1. **`--test cpu_roundtrip -- --list` 里第一个 `--` 的作用是什么？**
   答：分隔符。`--` 之后的参数不传给 cargo，而是传给编译出来的测试二进制（libtest），`--list` 是 libtest 的选项。
2. **为什么这 8 个单测不需要 GPU？**
   答：它们断言的都是主机侧纯计算——layout 测试构造假的 arena 指针/大小做整数断言，vllm_hash 测试对固定输入做哈希/编码后与 golden 字节比对；两处都没有 `CudaContext` 或设备内存分配。
3. **如果把这 8 个单测也搬进 `tests/`，损失什么？**
   答：损失访问私有项的能力——例如 layout 的 `Registration::from_arenas` 是 `pub(crate)`，集成测试根本无法构造它；这些细粒度几何断言只能以单元测试形式紧贴私有实现存在。

### 4.3 cpu_roundtrip.rs 解剖：几何常量与测试结构

#### 4.3.1 概念说明

`tests/cpu_roundtrip.rs` 是本 crate 唯一的端到端回归：向一组源 GPU 块写入互不相同的位模式，卸载到 pegaflow 的 host 层，再加载进**另一组**目标块，逐字节比对。它的测试结构有三个值得学的防呆设计：

1. **几何常量镜像 + 对账断言**：测试在文件顶部手抄了一套与 `KvBuffer` 内部布局相同的常量，随后立刻断言 `buffer.layout()` 与手抄值相等——布局上游一变，测试立刻红，而不是静默地比错位置还「通过」。
2. **pattern 按逻辑序号而非物理块号生成**：期望字节绑定「保存列表里的第 i 个块」，而不是「物理块号 b」。这样一旦 load 的顺序映射（第 i 个租约块 → 第 i 个目标块）出错，期望与实际必然对不上。
3. **负向控制**：一个从未加载的块必须保持为零——抓住「load 写出界、涂脏相邻块」这类回归。

#### 4.3.2 核心流程

**几何常量的推导**（元素单位，bf16 每元素 2 字节）：

\[
\text{SEGMENT\_LEN} = \text{PAGE\_SIZE} \times \text{NUM\_KV\_HEADS} \times \text{HEAD\_DIM} = 16 \times 2 \times 8 = 256
\]

\[
\text{LAYER\_STRIDE} = 2 \times \text{SEGMENT\_LEN} = 512 \quad(\text{一页内先 K 后 V})
\]

\[
\text{PAGE\_STRIDE} = \text{NUM\_LAYERS} \times \text{LAYER\_STRIDE} = 4 \times 512 = 2048
\]

整个 buffer 占 \( \text{NUM\_BLOCKS} \times \text{PAGE\_STRIDE} = 32 \times 2048 = 65536 \) 个元素，即 \( 65536 \times 2 = 131072 \) 字节 = 128 KiB。这三条公式与 `pegainfer-kv-cache` 的 `KvLayout` 定义逐字对应（见下方源码精读），这就是「page-first 融合布局」：一个物理块（页）里打包**所有层**的 K|V，层与层之间隔 `LAYER_STRIDE`。

**测试主流程**：

| 步骤 | 代码位置 | 做什么 |
|------|----------|--------|
| 1 | L61-L63 | 建 CUDA 上下文（device 0）并绑到测试线程 |
| 2 | L65-L76 | 按几何常量分配 `KvBuffer`，**对账断言** layout |
| 3 | L85-L96 | 向源块 [1,2,3] 逐 (块,层,segment) 写入确定性 pattern |
| 4 | L99-L104 | `OffloadEngine::new` 注册融合 buffer（64 MiB pinned host 池） |
| 5 | L110-L111 | `save_blocking` 保存 3 块 + `flush_saves` 让写入对查询可见 |
| 6 | L115-L125 | `query`：断言 `Ready` 且 3 块全命中，拿到 lease |
| 7 | L128-L134 | `load` 把租约块拷进目标块 [10,11,12] 并 `wait()` |
| 8 | L137-L154 | 逐层逐 segment 逐字节比对目标块 == pattern(i,…) |
| 9 | L157-L163 | **负向控制**：块 20 从未被加载，必须全零 |

#### 4.3.3 源码精读

- [pegainfer-kv-offload/tests/cpu_roundtrip.rs:L22-L31](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/tests/cpu_roundtrip.rs#L22-L31)：5 个模型形状常量 + 3 个派生几何常量（SEGMENT_LEN/LAYER_STRIDE/PAGE_STRIDE）。注释点明 SEGMENT_LEN 是「一个块内一段 K（或 V）的元素数」。
- [pegainfer-kv-cache/src/layout.rs:L5-L16](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/layout.rs#L5-L16)：`KvLayout` 的权威定义——`kv_block_len = page_size × num_kv_heads × head_dim`、`layer_stride = 2 × kv_block_len`、`page_stride = num_layers × layer_stride`，与测试常量同式。测试 L75-L76 的 `assert_eq!(buffer.layout().page_stride, PAGE_STRIDE)` / `kv_block_len == SEGMENT_LEN` 就是对这份定义的对账。
- [pegainfer-kv-offload/tests/cpu_roundtrip.rs:L37-L50](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/tests/cpu_roundtrip.rs#L37-L50)：两个核心 helper——`pattern(logical, layer, segment)` 用一个线性同余式生成确定性、有限、互异的 bf16 序列，注释强调「logical 是块在保存哈希列表中的位置」；`segment_ptr` 计算 (块, 层, segment) 首元素在融合 buffer 内的字节地址：\( \text{base} + (b \times \text{PAGE\_STRIDE} + l \times \text{LAYER\_STRIDE} + s \times \text{SEGMENT\_LEN}) \times 2 \)。
- [pegainfer-kv-offload/tests/cpu_roundtrip.rs:L52-L57](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/tests/cpu_roundtrip.rs#L52-L57)：`block_hash(logical)` 造 16 字节的假哈希（基线 0xA5，前两字节编入逻辑序号）——集成测试只走本地 host 层，哈希只需互异且充当内容寻址键，不必是真实模型 token 的哈希。
- [pegainfer-kv-offload/tests/cpu_roundtrip.rs:L109-L125](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/tests/cpu_roundtrip.rs#L109-L125)：save→flush→query 主链。两处值得注意：`flush_saves` 使写入对后续查询可见；`let QueryOutcome::Ready(hit) = ... else { panic!(...) }` 用 let-else 断言「只配 host 内存时查询必须是终态」——`Loading` 只在配置了更深数据面（远端/SSD prefetch）时出现（u2-l7 展开）。
- [pegainfer-kv-offload/tests/cpu_roundtrip.rs:L136-L163](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/tests/cpu_roundtrip.rs#L136-L163)：校验循环按 `to_bits()`（u16 位模式）而非浮点值比较——bf16 的位级保真才是 KV 搬运的正确性标准；结尾负向控制断言未加载块全零。

#### 4.3.4 代码实践（源码阅读型，无需 GPU）

**实践目标**：不运行任何东西，用几何公式手算三个地址，检验你是否真的理解了 page-first 布局。

**操作步骤**：

1. 用 4.3.2 的公式手算整个 buffer 的字节数（应为 131072 B = 128 KiB）；
2. 手算两个地址差：
   - `segment_ptr(base, 10, 0, 0) − segment_ptr(base, 1, 0, 0)`；
   - `segment_ptr(base, 1, 1, 0) − segment_ptr(base, 1, 0, 0)`；
3. 把你的答案与 `segment_ptr` 的公式（L47-L50）逐项对照；
4. 思考题（写下答案）：如果把 `pattern` 的第一个参数从 `logical` 换成物理 `block_id`，测试会漏掉哪类 bug？

**需要观察的现象**：无（纯纸面推导）；若想验证，可在有 GPU 的机器上把两个差值打印出来对照。

**预期结果**：

- 地址差 1：\( (10-1) \times 2048 \times 2 = 36864 \) 字节（跨 9 个页，每页 2048 元素）；
- 地址差 2：\( 1 \times 512 \times 2 = 1024 \) 字节（同块内跨一层，LAYER_STRIDE 个元素）；
- 思考题答案：换成物理块号后，期望字节跟着「数据原来在哪」走——如果 load 把租约块映射到目标块时**顺序错乱**（第 i 块落到第 j 个目标），源块 [1,2,3] 与目标块 [10,11,12] 的 pattern 会恰好仍然各自「自洽」（pattern(1,…) 出现在块 11 而非块 10），比较全部通过，顺序 bug 静默漏网。按 logical 生成期望，顺序契约（第 i 租约块 → 第 i 目标块）才真正被检验。

#### 4.3.5 小练习与答案

1. **为什么要有 L74-L76 的对账断言，而不是直接信任手抄常量？**
   答：手抄常量是 `KvLayout` 定义的**镜像副本**，上游布局一旦改变（比如 K|V 顺序调整），没有对账断言的测试会在错误的位置写入/比对，可能把「写错位置」误判为通过或给出误导性失败；对账断言让任何布局变化第一时间在这条测试上显式爆红。
2. **`block_hash` 可以随便造，为什么？**
   答：这条测试的被测对象是「搬运与布局」，不是「哈希算法」——host 层按 (namespace, hash) 内容寻址存取，测试只需 3 个互异且稳定的 16 字节键。真实模型场景的哈希由 vllm_hash.rs 的链式哈希负责，那里另有 5 个 golden-vector 单测把关。
3. **负向控制（块 20 全零）防的是哪类回归？**
   答：防「load 写出界/写错块」：如果 stride 或 size 计算错误导致一次拷贝多写、串写到相邻块，正向比对（只看目标块）可能依然通过，但被涂脏的旁观块会立刻暴露问题。

## 5. 综合实践

把 4.2.4 与 4.1.4 串成一个完整的「环境验收」流程，在仓库根目录执行并记录：

1. **纯 CPU 单测门**：`cargo test -p pegainfer-kv-offload --lib --release`，记录通过的测试数并按模块分组填表：

   | 模块 | 测试名 | 数量 |
   |------|--------|------|
   | `layout::tests` | arena_registration_geometry / interleaved_arena_size_is_last_block_reach / arena_copy_unit_must_fit_its_stride | 3（预测，待本地验证） |
   | `vllm_hash::tests` | none_hash_matches_vllm / cbor_encoding_matches_cbor2_canonical / block_chain_matches_vllm / realistic_block_size_matches_vllm / key_chain_covers_full_blocks_only | 5（预测，待本地验证） |

2. **GPU 集成测试清单**：`cargo test -p pegainfer-kv-offload --test cpu_roundtrip -- --list`，确认只列出 `gpu_cpu_gpu_roundtrip_preserves_kv_bytes` 一个测试；（可选，有 GPU 时）去掉 `--list` 真跑一遍，预期输出 `ok`。
3. **rev pin 证据链**：按 4.1.4 核对 Cargo.lock 与 Cargo.toml 的 rev 一致。
4. **写作输出**：写一段 5-8 句的说明，覆盖——本 crate 测试分几层、各自硬件门槛是什么、为什么 GPU 测试必须在 `tests/`（用 4.2.4 给出的三条论据）、`--release` 为什么不可省。

完成标志：三步命令的输出与你的预测一致（或差异都能解释），且说明段落能让一个没读过本 crate 的同事正确决定「在无 GPU 的笔记本上我能验证什么」。

## 6. 本讲小结

- 本 crate 的验证分两层：`--lib` 纯 CPU 单测 8 个（layout ×3 + vllm_hash ×5，任意机器可跑），`tests/cpu_roundtrip.rs` 1 个 GPU 集成测试（需要真实 CUDA 设备，且只经 `lib.rs` 的公开 API）。
- `pegaflow-core` 是唯一的非 workspace 依赖：git rev pin（对应已知语义的 PR #395/#381 提交）+ `default-features=false`（避开 cudarc selector 冲突）+ 显式 `rdma`（P/D 数据面），Cargo.lock 再冻结一层保证可复现。
- cargo 的目标选择：`--lib` 只跑库内联单测；`--test <name>` 跑单个集成测试二进制；`--` 之后的参数（如 `--list`、`--nocapture`）传给测试二进制；再加测试名过滤即可精确到单个用例。
- `--release` 是全仓库硬性约定：CLAUDE.md 明言 debug 构建对 GPU/CUDA 极慢会超时。
- cpu_roundtrip.rs 的三个测试设计范式值得带走：几何常量镜像 + 对账断言、期望值按逻辑序号（而非物理块号）生成、未触碰区域保持零的负向控制。

## 7. 下一步学习建议

验证环境就绪后，进入 u2 单元沿数据流精读核心机制。建议从 **u2-l1（config.rs：OffloadConfig、HostConfig 与 P2pConfig）** 开始——你已经在本讲见过 `OffloadConfig::new("roundtrip-test", 0, 64 * 1024 * 1024)` 这个调用，下一讲把它拆开讲清三个参数与 builder 方法。之后再顺次读 u2-l2（layout.rs 的几何映射——本讲 4.3 的公式将完整展开为 `Registration` 的构造）与 u2-l3（host.rs）。若想先看「下游怎么用这个 crate」，可跳读 u3-l3（qwen3 executor 的接线），但建议至少先完成 u2-l1。
