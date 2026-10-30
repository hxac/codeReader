# 目录结构与模块地图：src/ 各文件职责速览

## 1. 本讲目标

学完本讲，你应该能够：

1. 看着 [src/lib.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/lib.rs) 的 `mod` 声明列表，说出 crate 里每个模块的职责。
2. 把 src/ 下全部 31 个 `.rs` 文件按 **配置 / 权重 / 执行 / 调度 / 缓存 / TP** 六大类归类，并知道每个家族的代码量占比。
3. 说清 crate 的三层公开 API 边界：根级导出（服务器用）、`runtime`（本地测试与调试用）、`runtime_ops`（本地基准用），以及它们各自的真实消费者。
4. 指出哪些模块只出现在 TP 路径、哪些只出现在单 GPU 路径，以及启动函数在哪里分叉。

这张地图是后续所有讲义的「索引页」：u2 讲配置与权重，u3/u4 讲执行，u5 讲调度，u6 讲前缀缓存，u7 讲 TP——每一讲都是本讲地图中一小块区域的放大。

## 2. 前置知识

本讲承接 u1-l1 和 u1-l2 的两个核心结论，先帮你把它们 recalled 成一句话：

- **混合架构 → 两套状态系统**（u1-l1）：Qwen3.5 的全注意力层用分页 KV cache 记历史，Gated DeltaNet（GDN）线性注意力层用固定大小的递推状态记历史。所以 crate 里会**同时**出现 KV 相关模块（`prefill`、`batch_decode`）和递推状态相关模块（`recurrent`、`recurrent_state`）——这不是重复设计，而是两种注意力各自的配套。
- **feature 门控 → 整个 crate 条件编译**（u1-l2）：[src/lib.rs:7](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/lib.rs#L7) 的 `#![cfg(feature = "qwen35")]` 意味着不开 feature 时整个 crate 编译为空。本讲看到的所有模块都活在这道门后。

另外补充三个 Rust 模块系统的小知识点，初学者容易卡住：

- **`mod` 与文件路径的对应**：`mod config;` 会让编译器去找 `src/config.rs` 或 `src/config/mod.rs`。所以 `src/config/mod.rs + src/config/model.rs` 是**一个**模块树（config）的三个子文件，不是三个独立模块。
- **可见性分层**：`mod x`（私有）、`pub(crate) mod x`（crate 内可见）、`pub mod x`（对外可见）。可见性修饰本身就是一条线索：**作者愿意把什么暴露给谁，就是划定的 API 边界**。
- **`#[path]` 属性**：可以打破路径对应规则，把 crate 外的文件挂进来。本 crate 用它共享测试夹具（见 4.1.3）。

## 3. 本讲源码地图

| 文件 | 行数 | 作用 |
|---|---|---|
| `src/lib.rs` | 416 | crate 门控、模块树声明、启动入口（`start_engine*`）、`runtime`/`runtime_ops` 公开出口 |
| `src/ops.rs` | 29 | 算子面：把 core / kernels / 本 crate recurrent 的算子重导出成统一入口 |
| `src/ffi.rs` | 1 | 一行重导出 `pegainfer_core::ffi::*`，供 unsafe FFI 调用使用 |

（其余 28 个文件在 4.3 的总表里逐一给出。）

本讲的两个精读对象是 [src/lib.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/lib.rs)（模块树 + 启动分叉）和 [src/ops.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/ops.rs)（算子面）；其他文件只读它们的顶部文档注释来提取「一句话职责」。

## 4. 核心概念与源码讲解

### 4.1 模块树：lib.rs 的 mod 声明就是 crate 的目录页

#### 4.1.1 概念说明

一个 Rust crate 的 `lib.rs` 顶部那串 `mod` 声明，等价于一本书的目录页：**声明的顺序、可见性、以及拆不拆子目录，都是作者留下的结构信息**。pegainfer-qwen35 的 `lib.rs` 只做四件事：

1. 声明整个模块树（21 个顶层模块，其中 4 个是目录型模块）。
2. 定义两个公开的启动参数枚举（调度策略、decode 重叠模式）。
3. 提供一簇启动函数，在**单 GPU 路径**和 **TP 路径**之间分叉。
4. 用 `pub mod runtime` / `pub mod runtime_ops` 开两个「侧门」给测试和基准。

#### 4.1.2 核心流程

模块声明的阅读流程：

```text
lib.rs
 ├── 21 个 mod 声明 ──→ 按文件/目录找到 31 个 .rs 文件
 ├── 其中 4 个目录型模块（config/ weights/ scheduler/ tp_executor/）
 │      各自的 mod.rs 再声明子模块
 ├── #[cfg(test)] + #[path] 把 tests/common/model_fixture.rs 挂为 test_fixture
 └── 启动函数 start_engine_with_capacity_policy_and_overlap
        ├── device_ordinals.len() > 1 → scheduler::start_tp_with_capacity   （TP 路径）
        └── 否则 → weights::Qwen35Model::from_safetensors → scheduler::start_*  （单 GPU 路径）
```

#### 4.1.3 源码精读

**（1）模块树声明**。[src/lib.rs:9-29](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/lib.rs#L9-L29) 按字母序列出了全部顶层模块。注意可见性的三种层次：

```rust
mod batch_decode;                    // 私有：crate 外不可见
pub(crate) mod batch_decode_graph;   // crate 内可见
pub mod model_line;                  // 对外可见：frontend 服务器要调用它
```

两个 `pub` 的顶层模块各有原因：`model_line` 是给根服务器二进制做模型线分派的（u1-l4 详讲）；`prefill_buffers` 公开是因为它的 `GdrChunkwiseScratch35` 被 recurrent、weights、prefill、tp_executor 四处共享，公开类型让测试与基准可以直接构造。

**（2）共享测试夹具的 `#[path]` 技巧**。[src/lib.rs:24-26](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/lib.rs#L24-L26)：

```rust
#[cfg(test)]
#[path = "../tests/common/model_fixture.rs"]
mod test_fixture;
```

这段代码把集成测试目录下的 `tests/common/model_fixture.rs` 挂成单元测试模块，让 `cargo test --lib` 也能用同一套「找模型权重、找不到就 SKIP」的夹具逻辑（u1-l2 讲过 SKIP 行为）。这解释了为什么 `tests/` 目录里的 `common/` 不是普通的 `#[cfg(test)]` 内联模块——它被两边共用。

**（3）两个容量上限常量**。[src/lib.rs:41-52](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/lib.rs#L41-L52)：

- `MAX_DECODE_BATCH = batch_decode_graph::MAX_BATCH`（= 64）：解码调度槽的上限，源头在 CUDA Graph 的桶定义（u4-l3 详讲）。
- `MAX_SHARED_SM_DECODE_BATCH = ops::GEMM_LT_MAX_N`：SharedSM 重叠模式的安全上限。它的文档注释很值得读：更大的解码桶今天仍会回落到 prefill 的 cuBLAS handle，而「从另一条流并发调用同一个 handle」依赖未公开的多流行为，所以在更大的桶有独立的 per-stream handle 之前，重叠模式保持 opt-in 且限流（u4-l4 详讲）。

**（4）启动分叉点**。[src/lib.rs:225-308](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/lib.rs#L225-L308) 的 `start_engine_with_capacity_policy_and_overlap` 是所有启动函数的汇合点（`start_engine`、`start_engine_with_capacity`、`start_engine_with_capacity_and_policy` 都只是给它填默认参数）。先做参数校验（`max_batch` 范围、SharedSm 上限），然后按设备数分叉：

- **TP 分支** [src/lib.rs:253-275](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/lib.rs#L253-L275)：多设备时拒绝 decode overlap 和 auto 策略（都是单 GPU 专属），把全部参数转发给 `scheduler::start_tp_with_capacity`。这条分支上**不直接加载权重**——加载职责下放给每个 rank 的 worker 线程（u7 详讲）。
- **单 GPU 分支** [src/lib.rs:277-307](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/lib.rs#L277-L307)：强制 CUDA Graph 开启（`--cuda-graph=false` 不被支持），然后在 [L294-299](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/lib.rs#L294-L299) 当场 `weights::Qwen35Model::from_safetensors(...)` 加载模型，再交给 `scheduler::start_with_capacity_and_policy`。

**一句话总结分叉**：单 GPU 路径「先加载模型再启动调度器」，TP 路径「把一切打包交给 TP 启动器」。这就是后面 4.4 节双路径模块划分的来源。

#### 4.1.4 代码实践

**实践目标**：亲手确认模块树声明与磁盘文件的对应关系，并验证「开与不开 feature」的差别。

**操作步骤**：

1. 在 `pegainfer-qwen35/` 目录下执行 `ls src/`，把输出与 [src/lib.rs:9-29](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/lib.rs#L9-L29) 的 mod 列表逐一对上。
2. 对四个目录型模块分别打开 `src/config/mod.rs`、`src/weights.rs`、`src/scheduler/mod.rs`、`src/tp_executor.rs`，找到它们各自的子 `mod` 声明。
3. 执行 `cargo check -p pegainfer-qwen35`（不开 feature），观察它编译为空、几乎没有产物；再执行 `cargo check -p pegainfer-qwen35 --features qwen35` 对比编译的文件数。

**需要观察的现象**：第 1 步中 `src/` 下的每个 `.rs` 文件都能在 mod 树中找到挂载点；注意 `weights` 的子模块声明不在 `src/weights/mod.rs` 而在 `src/weights.rs` 内部（Rust 允许两种写法并存）。

**预期结果**：`cargo check`（无 feature）几乎瞬间完成且不编译任何业务模块；`--features qwen35` 会拉起 pegainfer-kernels 的 CUDA/Triton 构建链（本机无 CUDA 工具链时会报错——这本身也是 u1-l2 结论的验证）。无 GPU 环境下第 3 步的对比结论为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`src/scheduler/` 目录下有 8 个文件，它们是在哪里被声明为模块的？为什么不在 `lib.rs` 里声明？

答案：在 [src/scheduler/mod.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/scheduler/mod.rs) 内部声明（`lib.rs` 只有一行 `mod scheduler;`）。Rust 的模块树是逐级声明的：子模块由父模块声明，`lib.rs` 只需要声明顶层。这也让 scheduler 内部的重构（比如把 TP 簿记拆出去）不影响 crate 根。

**练习 2**：`#[path = "../tests/common/model_fixture.rs"]` 这行如果不加 `#[cfg(test)]` 会怎样？

答案：这个文件会被编进正式构建。`#[cfg(test)]` 保证它只在 `cargo test` 时编译。由于 `tests/` 目录天然不被库构建包含，加上 `#[path]` 后必须用 `#[cfg(test)]` 守住，否则生产二进制会无谓地携带测试夹具代码（甚至可能因为依赖 `dev-dependencies` 而编译失败）。

**练习 3**：为什么 `MAX_DECODE_BATCH` 的定义是 `batch_decode_graph::MAX_BATCH` 而不是直接写 `64`？

答案：单一事实来源（single source of truth）。解码批上限的物理约束来自 CUDA Graph 桶定义（[src/batch_decode_graph.rs:14-17](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode_graph.rs#L14-L17)：`BATCH_BUCKETS = &[1, 2, 4, 8, 16, 32, 64]`，`MAX_BATCH = 64`）。如果桶列表将来扩到 128，`lib.rs` 的校验上限自动跟随，不会出现两处常量漂移。

### 4.2 公开 API 边界：runtime 与 runtime_ops 两个「侧门」

#### 4.2.1 概念说明

一个模型 crate 对外的面有多大？pegainfer-qwen35 的答案分三层：

| 层 | 入口 | 服务对象 | 内容 |
|---|---|---|---|
| 正门 | 根级 `pub` 项 | 根服务器二进制（`pegainfer/src`） | `model_line` 模块、`Qwen35LaunchOptions`、`Qwen35SchedulerPolicy`、`Qwen35DecodeOverlap`、`start_engine*`、`DEFAULT_MAX_PREFILL_TOKENS` |
| 侧门 1 | `pub mod runtime` | 本 crate 的测试、调试 | `Qwen35Executor`、`Qwen35TpExecutor`、`Qwen35Model`、`start_with_capacity` 等低层执行接口 |
| 侧门 2 | `pub mod runtime_ops` | 本 crate 的 criterion 基准 | 3 个算子函数 |

这个设计的动机写在 [src/lib.rs:54-57](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/lib.rs#L54-L57) 的文档注释里：runtime 是「Low-level Qwen3.5 execution interface… for model-local tests, debugging, and benchmarks. **The root server should use `start_engine` instead.**」——侧门只给自家人用，正门才是对外契约。

#### 4.2.2 核心流程

```text
根服务器 ──────→ model_line + start_engine*        （正门：稳定契约）
tests/*.rs ────→ runtime::{Qwen35Executor, ...}     （侧门 1：teacher-force 精度门）
tests/*.rs ────→ runtime::{Qwen35Model, start_with_capacity} （侧门 1：E2E 直启调度器）
benches/ops/* ─→ runtime_ops::{rms_norm_*, gated_delta_rule_*}（侧门 2：内核微基准）
```

#### 4.2.3 源码精读

**（1）runtime 模块**。[src/lib.rs:58-73](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/lib.rs#L58-L73) 是一串 `pub use` 重导出：把 `executor`（teacher-force logits 执行器）、`tp_executor`（TP 执行器）、`weights`（模型结构）、`scheduler::start_with_capacity` 的类型集中到一个命名空间下。它的真实消费者（用 grep 验证过）：

- [tests/hf_golden_gate.rs:21-28](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/tests/hf_golden_gate.rs#L21-L28) 导入 `DecodePlan`、`PrefillStepItem`、`Qwen35Executor`、`Qwen35TpExecutor` 等——精度门测试（u8-l1）。
- [tests/e2e_scheduler.rs:587-594](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/tests/e2e_scheduler.rs#L587-L594) 直接用 `runtime::Qwen35Model::from_safetensors_with_options` + `runtime::start_with_capacity` 绕过启动校验、手工组装调度器——E2E 测试（u8-l2）。

**（2）runtime_ops 模块**。[src/lib.rs:76-80](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/lib.rs#L76-L80) 只导出 3 个函数。为什么是这 3 个？答案在 [src/ops.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/ops.rs)：ops.rs 里绝大多数重导出是 `pub(crate)`（crate 内部统一算子面），只有这三处标了 `pub`：

```rust
pub use pegainfer_core::ops::rms_norm_batch_offset_into;   // ops.rs L15
pub use pegainfer_core::ops::rms_norm_offset_into;         // ops.rs L17
pub use recurrent::gated_delta_rule_prefill_chunkwise_into; // ops.rs L27
```

消费者是两个基准：[benches/ops/qwen35_norm_bench.rs:7](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/benches/ops/qwen35_norm_bench.rs#L7) 和 [benches/ops/qwen35_state_bench.rs:6](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/benches/ops/qwen35_state_bench.rs#L6) 都写着 `use pegainfer_qwen35::runtime_ops as ops;`（u8-l3 详讲）。

**（3）ops.rs 的三来源结构**。[src/ops.rs:3-27](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/ops.rs#L3-L27) 把算子按来源分三段重导出：

| 来源 | 行号 | 代表算子 | 性质 |
|---|---|---|---|
| `pegainfer_core::ops` | [L3-19](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/ops.rs#L3-L19) | `gemm`、`embedding_batch`、`rms_norm_gated_batch_into` | 所有模型共享的通用 GPU 算子 |
| `pegainfer_kernels::ops` | [L20-25](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/ops.rs#L20-L25) | `paged_attention_batch_decode_hd256_into`、`gated_delta_rule_decode_batch_into` | Qwen3.5 专属内核（head_dim=256 家族、GDR decode） |
| `crate::recurrent` | [L26-27](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/ops.rs#L26-L27) | `conv1d_prefill_batch_into`、`gated_delta_rule_prefill_chunkwise_into` | 本 crate 在 [src/recurrent.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent.rs) 里手写的 unsafe FFI 封装（u3-l3 详讲） |

这个 29 行的小文件回答了一个重要问题：**「执行一层 Transformer 需要的算子从哪里来？」**——通用件买现成（core），专属件定制（kernels 的 Triton AOT 产物），最贴内核的封装自己写（recurrent）。而 [src/ffi.rs:1](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/ffi.rs#L1) 那一行 `pub(crate) use pegainfer_core::ffi::*;` 则是 recurrent.rs 里 unsafe 调用 C 符号的通道（u3-l3 详讲）。

#### 4.2.4 代码实践

**实践目标**：用工具验证「侧门只给自家人用」这句话。

**操作步骤**：

1. 在仓库根目录执行 `grep -rn "pegainfer_qwen35::runtime" --include='*.rs' .`，记录每个命中文件。
2. 再执行 `grep -rn "pegainfer_qwen35::" --include='*.rs' pegainfer/src/`（根服务器目录），确认正门消费者用了哪些项。

**需要观察的现象**：第 1 步的命中只出现在 `pegainfer-qwen35/tests/` 下；第 2 步里服务器只引用 `model_line` / `start_engine` / `Qwen35LaunchOptions` 等正门项，不碰 `runtime`。

**预期结果**：与 4.2.3 列出的消费者清单一致。若仓库布局将来变化，以 grep 实时结果为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么不把 `Qwen35Executor` 直接放在 crate 根做 `pub`，而要藏在 `runtime` 子模块里？

答案：命名空间即文档。crate 根的 `pub` 项是给根服务器用的稳定契约；`runtime` 这个名字本身在告诉使用者「这是低层执行接口」。如果平铺在根上，服务器的维护者和 crate 的维护者会看到同一个扁平清单，分不清哪些是对外承诺、哪些是内部工具——将来重构内部工具时会误以为是破坏性变更。

**练习 2**：`runtime_ops` 只导出 3 个函数，为什么不把 ops.rs 的所有算子都 `pub`？

答案：公开即承诺。每个 `pub` 项都是一条不能随便改签名的契约。bench 只需要 norm 与状态搬运两类内核，那就只公开这两类。`pub(crate)` 的其余算子可以在不通知任何外部用户的情况下自由重构。

**练习 3**：`pub use scheduler::DEFAULT_MAX_PREFILL_TOKENS;`（[src/lib.rs:39](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/lib.rs#L39)）为什么值得在根级单独导出？

答案：它是 CLI 默认值的一部分——`model_line.rs` 在补全 `--max-prefill-tokens` 参数默认值时需要向外界（frontend 的参数体系）报告这条模型线的默认预填充预算。这是正门契约的一项，所以必须从私有模块提升到根。

### 4.3 源码规模分布：31 个文件的六大类总表

#### 4.3.1 概念说明

「代码量」不是质量指标，但它是**注意力分配**的最好依据：写 2457 行的地方一定是难点所在。本 crate 共 31 个 `.rs` 文件、16432 行（`find src -name '*.rs' | xargs wc -l` 的实测值）。按六大类聚合后：

| 大类 | 文件 | 合计行数 | 占比 |
|---|---|---|---|
| 配置 | config/ 4 文件 + model_line.rs | 921 + 353 = 1274 | \( 1274/16432 \approx 7.8\% \) |
| 权重 | weights.rs + weights/layers.rs | 1110 | \( \approx 6.8\% \) |
| 执行（前向 + 内核封装 + 缓冲） | ops/ffi/recurrent/recurrent_state/prefill/prefill_buffers/batch_decode/decode_buffers/batch_decode_graph/unified_forward + executor + logprobs | 4127 + 440 = 4567 | \( \approx 27.8\% \) |
| 调度 | scheduler/ 8 文件 | 4353 | \( \approx 26.5\% \) |
| 缓存 | prefix_cache.rs | 658 | \( \approx 4.0\% \) |
| TP | tp_executor/ 3 文件 + scheduler/tp.rs | 4240 | \( \approx 25.8\% \) |
| 入口 | lib.rs | 416 | \( \approx 2.5\% \) |

三个立刻可读出的结论：

1. **TP 是最贵的单项**：单是控制器 + worker + 响应协议就占去约四分之一的代码。这就是为什么大纲把 TP 放在专家层（u7）单独一个单元。
2. **调度与执行几乎等重**：batching 策略和 GPU 前向各占约 27%，说明这个 crate 的复杂度一半在「算得对」、一半在「安排得巧」。
3. **配置只占 7.8% 却是所有路径的必经之地**——u2 整个单元都建立在它之上。

#### 4.3.2 核心流程

阅读顺序建议（也是本手册 u2→u7 的顺序）：

```text
config（几何与校验） → weights（权重落 GPU） → recurrent/prefill/batch_decode（两种前向）
   → scheduler（step 循环） → prefix_cache（状态复用） → tp_executor（多卡）
```

#### 4.3.3 源码精读

下表是本讲的核心产出——**模块 → 职责一句话 → 代码量**。职责均摘自各文件顶部的 `//!` 文档注释或已验证的代码事实，链接指向源码。

**① 配置类**

| 模块 | 行数 | 职责 |
|---|---|---|
| [src/config/mod.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/mod.rs#L1-L9) | 51 | 目录 + 身份检查：`probe_config_json` 验 `model_type == "qwen3_5"`；文档注释写明方向性边界（model→tp 单向依赖） |
| [src/config/model.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/model.rs#L1-L6) | 433 | 磁盘 RawConfig 一次性 `TryFrom` 成校验后的 `Config35`，只管模型几何、完全不知道 TP |
| [src/config/tp.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/tp.rs#L1-L6) | 360 | `TensorParallelConfig` + `LocalGeometry`：从已验证 Config35 派生本 rank 分片维度 |
| [src/config/error.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/error.rs#L1-L6) | 77 | 类型化 `ConfigError`：所有静态不变量在映射权重、分配显存**之前**拒绝 |
| [src/model_line.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/model_line.rs#L1) | 353 | `ModelLine` trait 实现：probe 识别 config、augment_cli 增补 Qwen3.5 专属参数（u1-l4） |

**② 权重类**

| 模块 | 行数 | 职责 |
|---|---|---|
| [src/weights.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L31-L38) | 601 | `Qwen35Model`：mmap safetensors 分片、组装层列表、`ModelRuntimeConfig`（u2-l3） |
| [src/weights/layers.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L1-L3) | 509 | 层容器：`TransformerBlock35`、`FullAttentionLayer`、`LinearAttentionLayer`、`LayerKind` 分发（u2-l4） |

**③ 执行类（含内核封装与缓冲）**

| 模块 | 行数 | 职责 |
|---|---|---|
| [src/ops.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/ops.rs#L1) | 29 | 统一算子面：core 通用件 + kernels 专属件 + 本 crate 封装件（见 4.2.3） |
| [src/ffi.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/ffi.rs#L1) | 1 | 一行重导出 core 的 FFI 符号面 |
| [src/recurrent.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent.rs#L1) | 1360 | GDN 的 unsafe FFI 内核封装：GDR chunkwise prefill 流水线、conv1d、批解码更新（u3-l3） |
| [src/recurrent_state.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent_state.rs#L1-L6) | 252 | 递推状态布局：每层 `[H,K,V]` f32 状态 + `conv_state` bf16（u3-l2） |
| [src/prefill.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L5) | 598 | 串行分块 prefill：`PREFILL_CHUNK_LEN`、位置校验、保守 scratch 预留（u4-l1） |
| [src/prefill_buffers.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill_buffers.rs#L1) | 182 | `GdrChunkwiseScratch35`：GDR 分块算子的预分配 scratch（`pub`，单 GPU 与 TP 共用） |
| [src/batch_decode.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L1-L2) | 907 | 批解码一步前向：N 请求各 1 token，full-attn 共享批内核 + 线性层逐槽状态更新（u4-l2） |
| [src/decode_buffers.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/decode_buffers.rs#L1) | 268 | 采样 scratch 与批解码缓冲 |
| [src/batch_decode_graph.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode_graph.rs#L14-L17) | 168 | CUDA Graph 桶状态：`BATCH_BUCKETS=[1,2,4,8,16,32,64]`、稳定地址槽（u4-l3） |
| [src/unified_forward.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/unified_forward.rs#L1-L6) | 362 | 批 prefill + 统一步（prefill 与 decode 合一）；GDR 无高效批 prefill 内核故逐请求串行（u4-l4） |
| [src/executor.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/executor.rs#L1-L6) | 410 | 最小 logits 执行器：**只**服务 teacher-force 精度门，生产走 scheduler（u8-l1） |
| [src/logprobs.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/logprobs.rs#L7-L9) | 30 | `LogprobSnapshot` 类型别名 + 拷贝辅助 |

**④ 调度类（scheduler/ 模块树）**

| 模块 | 行数 | 职责 |
|---|---|---|
| [src/scheduler/mod.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/scheduler/mod.rs#L1-L6) | 727 | 专属 GPU 属主线程上的 step 循环：模型专属状态 + frontend `RequestLedger` 账本（u5-l1） |
| [src/scheduler/plan.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/scheduler/plan.rs#L3) | 1074 | 纯函数规划层：`ExecutionPlan::{Prefill, Decode, Unified}` 选择、KV 预算准入、自适应策略（u5-l2） |
| [src/scheduler/steps.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/scheduler/steps.rs#L1-L3) | 704 | 五个 step 驱动：批 prefill、overlap 发射、unified、纯 decode、retire/分发（u5-l3） |
| [src/scheduler/backend.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/scheduler/backend.rs#L7-L22) | 858 | 后端抽象：`SchedulerBackend` 枚举包 `SingleGpuBackend` 与 `TpSchedulerBackend`（u5-l3） |
| [src/scheduler/emit.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/scheduler/emit.rs#L1-L3) | 94 | 发射侧：清理已关闭请求、拒绝不支持的请求特性、送达准入拒绝 |
| [src/scheduler/telemetry.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/scheduler/telemetry.rs#L1-L4) | 52 | 环境变量 `PEGAINFER_ITL_DEBUG` 门控的逐步 ITL 日志 |
| [src/scheduler/tests.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/scheduler/tests.rs) | 658 | 调度器单元测试（准入规则等纯逻辑，不需要 GPU） |
| [src/scheduler/tp.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/scheduler/tp.rs#L1-L4) | 186 | TP step item 构造：「从 God module 里拆出来」的 TP 簿记（见 4.4） |

**⑤ 缓存类**

| 模块 | 行数 | 职责 |
|---|---|---|
| [src/prefix_cache.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefix_cache.rs#L1) | 658 | KV + 递推状态的**联合**前缀缓存：内容寻址键、LRU、快照槽（u6 全单元） |

**⑥ TP 类**

| 模块 | 行数 | 职责 |
|---|---|---|
| [src/tp_executor.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/tp_executor.rs#L1-L5) | 2457 | TP 控制器：命令通道、每步一条统一 eager 命令、采样集中在 rank 0（u7-l1） |
| [src/tp_executor/worker.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/tp_executor/worker.rs#L1-L3) | 1215 | 每个 rank 一条 worker 线程：启动门、NCCL 看门狗、命令循环（u7-l2） |
| [src/tp_executor/responses.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/tp_executor/responses.rs#L3-L6) | 382 | 响应协议 `TpWorkerReply` + 300 秒 step 超时 |

**⑦ 入口**

| 模块 | 行数 | 职责 |
|---|---|---|
| [src/lib.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/lib.rs#L1) | 416 | 门控、模块树、启动分叉、runtime/runtime_ops（见 4.1、4.2） |

顺带一提 crate 外的配套目录：`tests/` 有 6 个集成测试 + `common/` 夹具（u8 单元逐个精读），`benches/` 有 1 个 criterion 入口 + `ops/` 两个内核基准（u8-l3）。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：自己动手做出上面那张总表，并用它标出「只在 TP 路径」与「只在单 GPU 路径」的模块。

**操作步骤**：

1. 在 `pegainfer-qwen35/` 下执行：

   ```bash
   find src -name '*.rs' | xargs wc -l | sort -rn
   ```

2. 对照 [src/lib.rs:9-29](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/lib.rs#L9-L29) 与四个目录型模块的内部 `mod` 声明，给每行输出标注「职责一句话」（提示：`head -6 <文件>` 就能看到大多数模块的 `//!` 文档注释）。
3. 验证路径归属。TP-only 的判据：模块只被 TP 启动链引用。执行：

   ```bash
   grep -rn "tp_executor::" src/        # 谁引用 TP 执行器
   grep -rn "unified_forward::" src/    # unified_forward 被谁调用
   ```

**需要观察的现象与参考答案**：

- **只在 TP 路径出现的模块（4 个，合计 4240 行）**：`tp_executor.rs`、`tp_executor/worker.rs`、`tp_executor/responses.rs`、`scheduler/tp.rs`。grep 会显示 `tp_executor::` 的引用集中在 `lib.rs`（runtime 重导出）、`scheduler/backend.rs`（TpSchedulerBackend）和 `tp_executor/` 内部——没有任何单 GPU 执行路径碰它。
- **只在单 GPU 路径出现的模块**：`unified_forward.rs`（362 行）。`unified_forward::` 在 src/ 里只有一个调用点——[src/scheduler/backend.rs:442-458](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/scheduler/backend.rs#L438-L458) 的 `SingleGpuBackend::unified_step`（该行落在 `impl SingleGpuBackend` 块内）。TP 的「统一命令」是在 worker 内部自己实现的，不经过这个模块。
- 另有两个**模式级**的单 GPU 限制（不是模块级）：SharedSM decode overlap 和 auto 调度策略都在 [src/lib.rs:253-262](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/lib.rs#L253-L262) 被显式拒绝用于多设备。
- 绝大多数模块（config、weights、recurrent、prefill、batch_decode、prefix_cache……）是**两条路径共用**的底座——这正是这个 crate 的设计取向：TP 不是另一套模型代码，而是同一套前向的切分执行。

**预期结果**：一张 31 行的表 + 4 个 TP-only 标记 + 1 个单 GPU-only 标记，与本节给出的参考表一致。

#### 4.3.5 小练习与答案

**练习 1**：不看总表，`recurrent.rs`（1360 行）为什么比 `prefill.rs`（598 行）大得多？

答案：recurrent.rs 承担的是 **unsafe FFI 封装层**——GDR chunkwise prefill 是一条三步内核流水线（chunk prepare / cumsum / chunkwise，u3-l3 详讲），每个内核都要手写形状断言、指针 guard、流绑定，这些安全样板在 Rust 侧天然冗长；而 prefill.rs 只做「分块循环 + 调用封装好的算子」这种纯 Rust 编排。

**练习 2**：`scheduler/tests.rs` 有 658 行单测放在 `src/` 里而不是 `tests/` 里，说明什么？

答案：它是 `#[cfg(test)]` 的内联单元测试，测的是调度器的**纯逻辑**（如准入规则、计划选择）——这些函数是 `pub(super)`/私有可见性，`tests/` 目录的集成测试根本够不到它们；而且纯逻辑测试不需要 GPU 与权重，放进 `cargo test --lib` 的快速回路（对照 u1-l2：约 9 秒的那条命令）。

**练习 3**：如果让你预估「删掉 TP 支持能省多少代码」，你的答案是什么？

答案：约 4240 行（tp_executor 三文件 + scheduler/tp.rs），占 crate 的 \( 4240/16432 \approx 25.8\% \)。但要注意 `unified_forward.rs` 不在此列（它是单 GPU 侧的），而 `backend.rs`、`lib.rs` 里还有少量 TP 分支代码会残留，所以精确值略高于 4240。

### 4.4 双路径分叉与两个子模块树的内部分工

#### 4.4.1 概念说明

学习目标里要求说清 `scheduler/` 与 `tp_executor/` 两个子模块树的内部分工。关键认知是：**TP 不是一个「调度器插件」，而是一套并行的执行 runtime**——单 GPU 时调度器直接驱动模型；TP 时调度器（通过 `TpSchedulerBackend`）把工作委托给 `tp_executor` 的控制器-worker 结构，后者才是真正握着 GPU 的一方。

#### 4.4.2 核心流程

```text
                              ┌──────────── 单 GPU ────────────┐
start_engine_with_capacity_* ─┤ SingleGpuBackend               │
  (lib.rs L253 分叉)          │   直接持有 Qwen35Model          │
                              │   → prefill / unified_forward   │
                              │   → batch_decode (CUDA Graph)   │
                              └────────────────────────────────┘
                              ┌──────────── TP ────────────────┐
                              ├─ TpSchedulerBackend (薄委托层)  │
                              │   → Qwen35TpExecutor (控制器)   │
                              │      → worker 线程 ×N (每 rank) │
                              │         各自加载分片权重 + NCCL  │
                              └────────────────────────────────┘
```

#### 4.4.3 源码精读

**（1）scheduler/ 的拆分逻辑**。[src/scheduler/mod.rs:1-6](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/scheduler/mod.rs#L1-L6) 的文档注释点明了它的双重身份：既保留模型专属状态（递推状态 + KV），又对接 frontend 的请求账本。内部按「做什么」拆成：`plan`（决定下一步干什么，纯函数）、`steps`（执行决定）、`backend`（在哪个后端上执行）、`emit`（把结果发出去）、`telemetry`（观测）。而 [src/scheduler/tp.rs:1-4](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/scheduler/tp.rs#L1-L4) 的注释直接交代了拆分动机："Split out of scheduler.rs to keep TP bookkeeping out of the God module"——当调度器主文件膨胀成「上帝模块」时，把 TP 簿记剥离出去是刻意的防腐动作。

**（2）tp_executor/ 的控制器-worker 结构**。[src/tp_executor.rs:1-5](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/tp_executor.rs#L1-L5) 的文档注释给出三条协议级约定：每步**一条**统一的 eager 命令、GDN 权重与状态按 rank 分片、解码行每 rank 一次批前向 + rank 0 一次批采样。子模块分工：`worker.rs`（每 rank 的线程循环与 NCCL 启动门）+ `responses.rs`（响应类型与 300 秒超时）。为什么控制器自己 2457 行、比 worker 还大一倍？因为它同时要管请求生命周期、图预捕获扫描、槽位压缩和优雅关闭——这些是 u7 三个讲义的内容，此处只需记住地图位置。

**（3）两个后端的汇合点**。[src/scheduler/backend.rs:17-22](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/scheduler/backend.rs#L17-L22) 的 `SchedulerBackend` 枚举把两条路径统一在调度器视野下，`SingleGpuBackend`（L7）与 `TpSchedulerBackend`（L96）实现同一组方法。这样 `steps.rs` 的五个驱动函数可以写出与路径无关的代码——**分叉被封装在 lib.rs 的启动函数和 backend 枚举两处，其余代码尽量共享**。

#### 4.4.4 代码实践

**实践目标**：亲眼看一次「分叉点」并确认两条路径的模块互不越界。

**操作步骤**：

1. 打开 [src/lib.rs:244-275](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/lib.rs#L244-L275)，抄下三处 `anyhow::ensure!` 各自拒绝什么组合。
2. 执行 `grep -n "TpSchedulerBackend" src/scheduler/backend.rs`，找到它包装 `Qwen35TpExecutor` 的字段。

**需要观察的现象**：TP 分支的三个拒绝分别是 SharedSm 重叠（单 GPU 专属）、auto 策略（单 GPU 专属）、以及再往前 L234-237 的 `max_batch` 范围校验；`TpSchedulerBackend` 结构体里持有的是 tp_executor 的控制器类型而非 `Qwen35Model`。

**预期结果**：确认「单 GPU 后端持有模型、TP 后端持有执行器」这条结构规律。它正是 u5-l3（后端抽象）与 u7-l1（TP 架构）两讲的入口。

#### 4.4.5 小练习与答案

**练习 1**：`scheduler/tp.rs` 属于「调度类」还是「TP 类」？为什么它不在 `tp_executor/` 目录里？

答案：按文件位置属于 scheduler 模块树（`mod tp` 声明在 scheduler/mod.rs 内），按功能是给 TP 服务的簿记。它留在 scheduler 里是因为它操作的是**调度器的数据结构**（构造 TP step item、对齐 decode 产物），而 tp_executor/ 是自洽的 runtime（线程、通道、NCCL）。目录按「操作谁」划分而不是「服务哪条路径」划分。

**练习 2**：为什么 `prefill_buffers.rs` 是 `pub` 而 `batch_decode_graph.rs` 只是 `pub(crate)`？

答案：`GdrChunkwiseScratch35` 被 crate 内四个模块（recurrent、weights、prefill、tp_executor）使用，且基准与测试需要独立构造它来度量显存预留，所以公开；而 CUDA Graph 桶状态是纯内部实现细节，外部没有直接构造的需求，最小可见性原则下保持 crate 内可见即可。

**练习 3**：如果你想给 crate 加一个「模型导出」功能（把权重写回磁盘），按本讲的地图应该新建什么模块、挂在哪？

答案：开放性答案。合理方案是新建 `src/export.rs` 之类的执行类模块，在 [src/lib.rs:9-29](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/lib.rs#L9-L29) 加一行 `mod export;`，读取 `weights::Qwen35Model` 的层结构落盘。要点在于：它属于「权重/执行」家族、不触碰 scheduler 与 tp_executor，且不需要新的对外 API 面（走 `runtime` 侧门导出即可）。

## 5. 综合实践

**任务：为「一条请求的一生」在模块地图上画路线。**

假设一条请求走单 GPU 路径，从 HTTP 进入到 token 流出。请在本讲的地图上标出它依次触碰的模块，并各写一句「它在这里发生了什么」：

1. 请求以 `QueuedRequest` 形式从 frontend 到达 → `scheduler/mod.rs`（账本登记）。
2. 调度器决定下一步 → `scheduler/plan.rs`。
3. 准入后执行 prefill → 沿途经过哪些模块？（提示：`steps.rs` → `backend.rs` → `unified_forward.rs` 或 `prefill.rs` → `weights/layers.rs` → 两种注意力层各自的算子，线性层还要经过 `recurrent.rs` 的封装）。
4. 转 decode → `batch_decode_graph.rs` 重放图 + `batch_decode.rs` 前向 + `decode_buffers.rs` 采样 scratch。
5. token 发出 → `scheduler/emit.rs` + `logprobs.rs`。

完成后自检两个问题：(a) 你的路线里 `prefix_cache.rs` 出现在哪一步？（答案：准入时查询边界、生成后写入快照——如果它没出现在你的路线里，说明还没把 u6 的位置挂进地图）；(b) 这条路线完全避开了哪些大模块？（答案：`tp_executor/` 全家 + `scheduler/tp.rs` + `executor.rs`——后两者一个是 TP 专属，一个是测试专属。）

## 6. 本讲小结

- **模块树即目录页**：[src/lib.rs:9-29](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/lib.rs#L9-L29) 的 21 个 mod 声明展开成 31 个文件、16432 行；可见性修饰（`mod` / `pub(crate)` / `pub`）本身就是 API 边界的声明。
- **三层公开面**：正门（`model_line` + `start_engine*` 给服务器）、`runtime`（给本地测试与调试）、`runtime_ops`（给基准的 3 个算子）——侧门的消费者用 grep 验证过，全部在本 crate 的 `tests/` 与 `benches/` 里。
- **六大类规模分布**：TP ≈ 25.8%、执行 ≈ 27.8%、调度 ≈ 26.5% 三分天下；配置只占 7.8% 却是必经之地。代码量告诉你注意力该往哪放。
- **双路径分叉**只有两处关键点：启动函数 [lib.rs:253-275](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/lib.rs#L253-L275)（TP 委托 / 单 GPU 直载）和 `SchedulerBackend` 枚举；TP-only 模块 4 个，单 GPU-only 模块只有 `unified_forward.rs`，其余全是共享底座。
- **子模块树分工**：scheduler 按「计划 / 执行 / 后端 / 发射 / 观测」拆分，`tp.rs` 是刻意的「去上帝模块」剥离；tp_executor 是自洽的控制器-worker runtime，握 GPU 的是 worker 不是调度器。
- **阅读顺序**：config → weights → 执行（recurrent/prefill/batch_decode）→ scheduler → prefix_cache → tp_executor，这正是本手册 u2→u7 的单元顺序。

## 7. 下一步学习建议

下一讲 **u2-l1「config.json 的解析与校验」**从地图的第一站出发：精读 [src/config/model.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/model.rs)，看 `RawConfig` 如何经 `TryFrom` 一次性变成合法的 `Config35`，以及 `layer_types` 如何决定 8 层全注意力 + 24 层线性注意力的混合排布。

在进入 u2 之前，建议先做两件轻量准备：

1. 重跑 4.3.4 的 `wc -l` 命令，把总表存成自己的笔记——后面每讲结束时回到这张表，把学过的模块标灰，能直观看到进度。
2. 通读 [src/config/mod.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/mod.rs)（只有 51 行）——它是全 crate 最短的模块文档之一，「方向性边界」这个词在 u2 会反复出现。
