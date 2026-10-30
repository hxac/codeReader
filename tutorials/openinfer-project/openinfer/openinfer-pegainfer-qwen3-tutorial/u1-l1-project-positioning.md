# 讲义 u1-l1：crate 定位与模块地图：pegainfer-qwen3 是什么

> 阅读对象：第一次接触 PegaInfer、也不熟悉本 crate 的读者。本讲不需要 GPU，不需要下载数据或模型权重，只需要能编译 Rust 代码的环境。

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `pegainfer-qwen3` 在 PegaInfer 工作区中的角色：它是 Qwen3 模型线的模型 crate，也是服务器默认构建时唯一编译进去的模型线。
2. 解释「默认 feature」的准确含义：`default = ["qwen3"]` 写在服务器 crate（`pegainfer-server`）里，而不是本 crate 里；本 crate 自身没有 default feature。
3. 对照 `src/lib.rs` 顶部的模块声明，把每个模块归类到：配置 / 权重 / 前向执行 / 调度 / 投机解码 / LoRA / KV 数据面 / 契约接入 / 诊断工具。
4. 理解 `pub mod runtime` 再导出的 plan/result 类型是本 crate 的「公共执行边界」——调度器与基准工具都只通过这一层与 GPU 执行器对话。

## 2. 前置知识

用通俗语言预先解释几个本讲会用到的 Rust / 工程术语：

- **crate**：Rust 的编译单元，近似「一个包」。本讲的主角是一个名为 `pegainfer-qwen3` 的 crate。
- **workspace（工作区）**：多个 crate 共享一个锁文件和依赖版本的仓库组织方式。PegaInfer 的 workspace 根在仓库根目录的 `Cargo.toml`，`pegainfer-qwen3` 是其中一个成员。
- **feature（特性）**：cargo 的条件编译开关。依赖可以按 feature 打开（`optional = true` + `dep:xxx`），代码可以用 `#[cfg(feature = "...")]` 圈定，二进制目标可以用 `required-features` 声明「只有打开该 feature 才构建」。
- **再导出（re-export）**：用 `pub use` 把别的模块里的类型挂到当前路径下，让外部使用者不需要知道内部文件结构。`src/lib.rs` 里的 `pub mod runtime` 就是一个集中的再导出层。
- **plan / result 相位边界（phase boundary）**：引擎每一步先构造一份纯数据的「计划」（plan：这一步要算哪些请求、哪些 token），执行后得到一份纯数据的「结果」（result：采样出的 token 等）。计划与结果都是普通数据结构，不含引擎句柄，因此调度器可以在完全不碰 GPU 的情况下决定下一步做什么。
- **LLM 推理的两个阶段**：**prefill**（把整段 prompt 一次性前向，写入 KV 缓存）与 **decode**（之后每次只前向一个新 token）。本 crate 的很多模块名都带这两个词。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [Cargo.toml:L1-L64](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/Cargo.toml#L1-L64) | 本 crate 的依赖、feature 与二进制目标声明，是理解「这个 crate 由什么组成」的入口 |
| [src/lib.rs:L1-L556](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/lib.rs#L1-L556) | crate 根：全部模块声明、面向服务器的启动选项与 `launch` 入口、`runtime` 再导出层 |
| [pegainfer-server/Cargo.toml:L34-L48](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-server/Cargo.toml#L34-L48) | 服务器 crate 的 feature 表，`default = ["qwen3"]` 在这里——这是「默认模型线」这句话的出处 |
| [Cargo.toml:L1-L27（工作区根）](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/Cargo.toml#L1-L27) | workspace 成员列表与 `default-members`，说明裸 `cargo build` 构建的是谁 |

## 4. 核心概念与源码讲解

### 4.1 crate 定位与 Cargo.toml features

#### 4.1.1 概念说明

PegaInfer 是一个纯 Rust + CUDA 的 LLM 推理引擎：不依赖 PyTorch，不依赖 Python 运行时，对外提供 OpenAI 兼容的 `/v1/completions` API。它采用「每个模型线一个 crate」的组织方式——Qwen3、Qwen3.5、DeepSeek-V2-Lite、Kimi-K2 等各自是一个独立 crate，服务器二进制按 feature 选择编译哪些模型线。

`pegainfer-qwen3` 就是 Qwen3（4B / 8B，全注意力，支持张量并行）这条模型线的 crate。它的特殊地位来自服务器 crate 的默认 feature：`pegainfer-server` 声明了 `default = ["qwen3"]`，而工作区根又把 `default-members` 设为 `pegainfer-server`。两层叠加的结果是：**在仓库根目录裸执行 `cargo run --release`，构建出来的服务器只带 Qwen3 一个模型线，且全程没有 Python 参与**。仓库文档把这条路径称为「stock build」。其他模型线（如 Qwen3.5）需要显式 `--features qwen35`，其中 Qwen3.5 还要求构建期有 Python + Triton。

本 crate 自己声明的 feature 只有三类，全部是「工具/测试」性质，与模型线选择无关：

| feature | 作用 |
| --- | --- |
| `kernel-call-trace` | 透传给 `pegainfer-core` 的同名 feature，打开内核调用追踪 |
| `kernel-report` | 拉起一批可选依赖（`comfy-table`、`hex`、`pegainfer-bench`、`pegainfer-cupti`、`sha2`、`toml`），并隐含 `kernel-call-trace`；两个诊断二进制以它为 `required-features` |
| `test-fixtures` | 只为测试存在的夹具开关；dev-dependencies 里本 crate 以 `features = ["test-fixtures"]` 自引用，让 `tests/` 下的集成测试也能用到夹具 |

#### 4.1.2 核心流程

从「一条 cargo 命令」到「哪些代码被编译」的因果链：

```text
cargo run --release（仓库根）
  └─ workspace default-members = ["pegainfer-server"]
       └─ 服务器 crate 的 default = ["qwen3"]
            └─ 打开服务器 feature "qwen3" = ["dep:pegainfer-qwen3"]
                 └─ 本 crate 以「无额外 feature」的方式被编译
                      ├─ 依赖：pegainfer-frontend / core / kernels /
                      │        kv-cache / kv-offload / sample（必选）
                      ├─ 可选依赖（comfy-table 等）：不编译
                      └─ 两个 [[bin]]（qwen3_kernel_report / qwen3_model_report）：
                         因 required-features = ["kernel-report"] 未满足而跳过
```

反过来，想启用诊断二进制时执行 `cargo build --release -p pegainfer-qwen3 --features kernel-report`，可选依赖与 `src/bin/` 下的两个报告工具才会进入编译。

#### 4.1.3 源码精读

本 crate 依赖的 workspace 内部 crate 一览（这是「模型 crate 建立在哪些基础设施上」的答案）：

- [Cargo.toml:L19-L25](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/Cargo.toml#L19-L25) — 必选的内部依赖：`pegainfer-core`（GPU 算子与张量封装）、`pegainfer-frontend`（引擎契约与 ModelLine trait）、`pegainfer-kernels`（CUDA/cuBLAS/FlashInfer 内核与构建）、`pegainfer-kv-cache`（分页 KV 原语）、`pegainfer-kv-offload`（主机/远端 KV 数据面）、`pegainfer-sample`（采样）。
- [Cargo.toml:L11-L32](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/Cargo.toml#L11-L32) — 可选依赖全部由 feature 圈定：`pegainfer-bench`、`pegainfer-cupti`（CUPTI 计时）等只属于 `kernel-report` 世界。
- [Cargo.toml:L34-L45](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/Cargo.toml#L34-L45) — feature 表全文。注意本 crate **没有** `default` feature；`test-fixtures` 是给集成测试自引用用的空 feature。
- [Cargo.toml:L53-L61](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/Cargo.toml#L53-L61) — 两个 `[[bin]]` 目标都带 `required-features = ["kernel-report"]`，所以默认构建不产出它们。
- [Cargo.toml:L47-L51](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/Cargo.toml#L47-L51) — dev-dependencies 里 `pegainfer-qwen3 = { workspace = true, features = ["test-fixtures"] }`：crate 在自己的测试配置里把自身当作依赖打开夹具，这是 Rust 里让集成测试共享测试设施的常见手法。

「默认模型线」的证据链在服务器与工作区根的两个 Cargo.toml 里：

- [pegainfer-server/Cargo.toml:L34-L37](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-server/Cargo.toml#L34-L37) — `default = ["qwen3"]`，注释写明「Qwen3-4B is the default model line: pure Rust + CUDA, no Python at build time」。
- [Cargo.toml:L2-L17](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/Cargo.toml#L2-L17)（工作区根）— `default-members = ["pegainfer-server"]`，成员列表第 17 行就是 `pegainfer-qwen3`。

#### 4.1.4 代码实践

1. **实践目标**：亲眼确认「默认构建只编译 Qwen3，且诊断二进制被 feature 挡住」。
2. **操作步骤**：
   - 在仓库根执行 `cargo tree -p pegainfer-qwen3 --depth 1 --edges normal`，列出本 crate 的直接依赖。
   - 再执行 `cargo tree -p pegainfer-qwen3 --features kernel-report --depth 1 --edges normal`，对比多出来的可选依赖。
   - 执行 `cargo build --release -p pegainfer-qwen3`，构建结束后到 `target/release/` 下确认没有 `qwen3_kernel_report` 可执行文件。
3. **需要观察的现象**：第二次 `cargo tree` 的输出里出现 `comfy-table`、`hex`、`pegainfer-bench`、`pegainfer-cupti`、`sha2`、`toml`；第一次没有。
4. **预期结果**：依赖差集恰好等于 [Cargo.toml:L36-L44](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/Cargo.toml#L36-L44) 里 `kernel-report` 列出的六项；默认构建不产出诊断二进制。**待本地验证**（具体输出以本机 cargo 版本为准）。

#### 4.1.5 小练习与答案

**练习 1**：同事说「pegainfer-qwen3 默认打开了 qwen3 feature」，这句话错在哪里？

**答案**：`pegainfer-qwen3` 自身没有声明任何 default feature（见 [Cargo.toml:L34-L45](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/Cargo.toml#L34-L45)，只有 `kernel-call-trace` / `kernel-report` / `test-fixtures` 三个非默认 feature）。「默认」指的是服务器 crate `pegainfer-server` 的 `default = ["qwen3"]`（[pegainfer-server/Cargo.toml:L36](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-server/Cargo.toml#L36)），它选择把本 crate 编进服务器。

**练习 2**：为什么 `cargo run --release --features kernel-report -- --model-path ...`（在仓库根执行）不会构建出 `qwen3_kernel_report` 二进制？

**答案**：仓库根的裸 cargo 命令作用于 `default-members`，即 `pegainfer-server`；`--features kernel-report` 是服务器的 feature 名，不存在（服务器的 feature 表里没有这一项），命令会直接报错——即便存在也不会传导到 `pegainfer-qwen3`。要构建诊断二进制必须用 `-p pegainfer-qwen3 --features kernel-report`，因为 `required-features` 检查的是**被构建包自身**的 feature（[Cargo.toml:L53-L61](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/Cargo.toml#L53-L61)）。

**练习 3**：`pegainfer-kv-offload` 是必选依赖还是可选依赖？它对应本讲模块分类中的哪一层？

**答案**：必选依赖（[Cargo.toml:L24](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/Cargo.toml#L24)，没有 `optional = true`）。它属于「KV 数据面」层——主机层/远端 KV 的保存与预取；是否启用由运行时选项 `Qwen3OffloadOptions` 控制（默认关闭），而不是编译期 feature。

### 4.2 src/lib.rs 模块声明：一张模块地图

#### 4.2.1 概念说明

`src/lib.rs` 的第 1–25 行集中声明了全部子模块，这 25 行就是整个 crate 的「目录页」。每个模块名对应 `src/` 下同名文件或目录（例如 `mod dflash;` 对应 `src/dflash.rs` 与 `src/dflash/` 目录）。把这些模块按职责归类，是快速建立全局认知的最有效方法——后续整个学习手册（调度、前向、投机解码……）都是沿着这张地图逐层展开的。

注意声明的可见性差异：`mod xxx;`（私有）、`pub mod xxx;`（公开）、`pub(crate) mod xxx;`（仅 crate 内可见）。可见性本身就透露了设计意图：`model_line`、`batch_decode_trace` 与 `runtime` 是 crate 对外仅有的几个公开面，其余全是内部实现。

#### 4.2.2 核心流程

按「一个请求进来之后会经过什么」把模块串成一条链：

```text
HTTP 请求
  → frontend_adapter（把引擎契约翻译成 ledger 写入；组装 Engine）
      → scheduler（纯调度：排队、装箱、产出 plan 与 effects，不碰契约）
          → executor（GPU 线程：消费 plan，驱动前向，产出 result）
              → prefill / batch_decode / unified_forward（三条前向路径）
                  → weights（模型权重） + config（模型配置）
                  → pegainfer-core / kernels / kv-cache（底层算子与 KV）
侧支：
  speculative + dflash/dspark/eagle3（投机解码草稿与验证）
  lora（适配器装载与服务）
  green_ctx / split_kv / projection_fusion / verify_graph（前向的执行形态变体）
  sizing / spec_env / model_line / bin/*（显存预算、旋钮校验、CLI、诊断）
```

#### 4.2.3 源码精读

crate 的全部模块声明，一行一个职责：

- [src/lib.rs:L1-L25](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/lib.rs#L1-L25) — 25 行 `mod` 声明即完整目录页。
- [src/lib.rs:L14-L15](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/lib.rs#L14-L15) — `lora_fixtures` 只在 `test` 或 `test-fixtures` 下再导出，与 4.1 的 feature 自引用呼应。
- [src/lib.rs:L31-L41](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/lib.rs#L31-L41) — 从各模块提升到 crate 根的少量公共项：`probe_config_json`（crate 内可见）、`DEFAULT_MAX_PREFILL_TOKENS`、`Qwen3MemoryOptions` 与两个 KV 默认常量。
- [src/lib.rs:L88](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/lib.rs#L88) — `pub use green_ctx::DecodeOverlap;`，decode 重叠模式的三态枚举（与 `green_ctx` 的流管理定义在一起）。

把这 25 个模块按职责归类（这是本讲最重要的表，后续讲义编号均可对照）：

| 分类 | 模块（文件） | 一句话职责 |
| --- | --- | --- |
| 契约接入 | `model_line.rs` | Qwen3 的 `ModelLine` 实现：config.json 探测、Qwen3 专属 CLI 段、跨标志规则 |
| 契约接入 | `frontend_adapter.rs` | 引擎契约适配：effects → ledger 写入、取消处理、Engine 组装 |
| 配置 | `config.rs` | `config.json` → `Config`：身份检查、TP 切分推导、投机配置入口 |
| 配置/显存预算 | `sizing.rs` | 防溢出的 checked 乘加算术，先于张量校验守住预算计算 |
| 权重 | `weights.rs`、`weights/load.rs` | 权重布局（QKV 合存、gate+up 合存）、显存画像与 checkpoint 装载 |
| 前向执行 | `prefill.rs` | prefill 前向：分块、临时缓冲、KV 写入 |
| 前向执行 | `batch_decode.rs`、`batch_decode_buffers.rs`、`batch_decode_dag.rs`、`batch_decode_trace.rs` | 批量解码前向、一次性预分配缓冲、DAG 元数据契约、调用追踪 |
| 前向执行 | `unified_forward.rs` | prefill 行与 decode 行同一步混合前向 |
| 前向执行 | `split_kv.rs`、`projection_fusion.rs` | split-KV 解码的 chunk 公式、QKV 投影融合资格表 |
| 前向执行 | `green_ctx.rs` | decode-overlap 双流与 Green Context SM 分区 |
| 调度 | `scheduler.rs`、`scheduler/{effects,plan,resolve,phase_trace}.rs` | 纯调度核心：队列、执行计划、effects 解析、相位追踪（不含引擎契约） |
| 调度/投机旋钮 | `spec_env.rs` | `PEGAINFER_SPEC_*` 环境旋钮的读取与校验 |
| 执行器 | `executor.rs`、`executor/{logprobs,remote_fetch,spec,...}.rs` | GPU 线程模型、plan→result 执行、logprobs、远端 KV 取数决策 |
| 投机解码 | `speculative.rs` | 方法无关核心：propose/verify/accept 事务与 KV 回滚 |
| 投机解码 | `dflash.rs`+`dflash/`、`dspark.rs`、`eagle3.rs`+`eagle3/` | 三种草稿器（DFlash、DSpark Markov 头、EAGLE-3）及其装载/预留 |
| 投机解码 | `verify_graph.rs` | verify 前向的缓冲与分段 CUDA Graph |
| LoRA | `lora.rs`、`lora/fixtures.rs` | 适配器解析/校验/装载与测试夹具 |
| KV 数据面 | `lib.rs` 中的 `Qwen3OffloadOptions`/`Qwen3P2pOptions`/`Qwen3VllmCompatOptions`（[src/lib.rs:L98-L156](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/lib.rs#L98-L156)） | 主机层 offload、P2P 共享、vLLM 兼容 P/D 的选项面 |
| 诊断工具 | `bin/qwen3_kernel_report.rs`、`bin/qwen3_model_report.rs`、`bin/qwen3_decode_context.rs`、`bin/report_support/` | 清单驱动的内核扫参、执行级算子报告、上下文长度基准 |

模块可见性佐证设计意图的三处：

- [src/lib.rs:L13](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/lib.rs#L13) — `pub mod model_line;`：服务器二进制只看得到 `&MODEL_LINE`，不见模型内部。
- [src/lib.rs:L4](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/lib.rs#L4) — `pub mod batch_decode_trace;`：内核调用追踪类型需要被 `kernel-report` 工具链跨 crate 使用。
- [src/lib.rs:L11](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/lib.rs#L11) — `pub(crate) mod green_ctx;`：流管理纯内部，对外只露出 `DecodeOverlap` 枚举（[src/lib.rs:L88](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/lib.rs#L88)）。

#### 4.2.4 代码实践

1. **实践目标**：亲手制作并验证上面那张模块分层表，建立后续全部讲义的「地图感」。
2. **操作步骤**：
   - 打开 [src/lib.rs:L1-L25](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/lib.rs#L1-L25)，把 25 个模块抄进表格左列。
   - 逐个打开对应文件，只读**顶部 10 行左右的模块级文档注释**（`//!`），为每个模块补一句自己的话职责描述，并归入分类。
   - 用 `rg "^//!|^mod |^pub mod |^pub\(crate\) mod "` 在 `src/` 下抽查，确认没有漏掉声明。
3. **需要观察的现象**：`scheduler.rs` 的模块注释明确写着「engine-contract-free……契约相关的一切都在 frontend_adapter」（[src/scheduler.rs:L1-L9](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/scheduler.rs#L1-L9)）；`speculative.rs` 的注释写着「投机解码是 decode 循环上的乐观并发事务」（[src/speculative.rs:L1-L9](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/speculative.rs#L1-L9)）。注释与表格分类应当一致。
4. **预期结果**：得到一张 25 行、每行有出处（文件 + 顶部注释）的分层表。分类有模糊地带是正常的（例如 `sizing.rs` 既是「配置」又是「显存预算」，`executor/remote_fetch.rs` 既是「执行器」又是「KV 数据面」）——请把双重身份也记进表里。

#### 4.2.5 小练习与答案

**练习 1**：`frontend_adapter.rs` 与 `scheduler.rs` 的分工边界是什么？用两处源码注释佐证。

**答案**：`frontend_adapter` 负责引擎契约（`Scheduler` trait 的实现、ledger 写入、Engine 组装），`scheduler` 是不含契约的纯调度（队列 + 纯 effects 数据）。证据：[src/frontend_adapter.rs:L1-L13](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/frontend_adapter.rs#L1-L13)（「How Qwen3 plugs into the engine contract — the whole adaptation, in one file」，并自述拥有 ledger 写入与 engine assembly 两项职责）；[src/scheduler.rs:L1-L9](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/scheduler.rs#L1-L9)（「This module is engine-contract-free……everything that touches the engine contract …… lives in `crate::frontend_adapter`」）。

**练习 2**：`pub mod`、`pub(crate) mod`、`mod` 三种声明在本 crate 各举一例，并说说为什么这样选。

**答案**：`pub mod model_line`（[src/lib.rs:L13](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/lib.rs#L13)）——服务器要用它挂 CLI；`pub(crate) mod green_ctx`（[src/lib.rs:L11](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/lib.rs#L11)）——流管理是内部实现，但同 crate 的 executor 要用，对外仅暴露 `DecodeOverlap`；`mod speculative`（[src/lib.rs:L21](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/lib.rs#L21)）——投机核心只经 `runtime` 边界间接可见。规律：对外面越窄，内部重构自由度越大。

**练习 3**：`dflash`、`dspark`、`eagle3` 三个名字为什么是三个独立模块而不是一个 `spec` 模块？

**答案**：它们是三种**不同草稿器**（DFlash 草稿模型、DSpark 低秩 Markov 头、EAGLE-3 midlayer），而 `speculative.rs` 才是方法无关的共享核心。`speculative.rs` 顶部注释说明 verify/accept/KV 事务共享、只有 propose 随方法变化（[src/speculative.rs:L1-L14](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/speculative.rs#L1-L14)），把可变部分（三个 drafter）与不变部分（事务核心）拆开，正是这种模块划分的动机。

### 4.3 runtime 再导出：本 crate 的公共执行边界

#### 4.3.1 概念说明

`src/lib.rs` 里的 `pub mod runtime`（[src/lib.rs:L202](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/lib.rs#L202)）是一个**纯再导出**模块：它不定义任何新类型，只把散落在 `executor.rs`、`batch_decode_buffers.rs`、`split_kv.rs` 里的类型挂到 `pegainfer_qwen3::runtime` 这一个名字空间下。它的文档注释直白地说明了定位：

> Low-level Qwen3 execution interface. This is the production phase boundary used by the Qwen3 scheduler and by model-local benchmarks. The root server should use `start_engine` instead.

翻译过来就是三层含义：

1. **它是低层执行接口**：消费者拿着 plan（「这一步算什么」）调用 `Qwen3Executor`，拿回 result（「算出了什么」）。
2. **它是生产相位边界**：Qwen3 自己的调度器和模型本地基准（`src/bin/` 下的诊断工具）都从这里进入执行器——同一个入口保证基准测到的就是服务时执行的。
3. **服务器不该用它**：服务器二进制应使用高层的 `launch` / `start_engine`（返回组装好的 `Engine`），直接操作 executor 是留给调度器与基准的内部通道。

plan/result 三组成对出现：`PrefillPlan`/`PrefillResult`、`DecodePlan`/`DecodeResult`、`UnifiedPlan`/`UnifiedResult`，另有逐请求粒度的 `*StepItem` 与 `*RequestResult`，以及贯穿全程的 `RequestId`。

#### 4.3.2 核心流程

`runtime` 边界两侧的协作方式：

```text
调度器（scheduler，CPU 侧，不碰 GPU）
   │  构造 PrefillPlan / DecodePlan / UnifiedPlan（纯数据）
   ▼
runtime::Qwen3Executor（GPU 线程侧）
   │  消费 plan，驱动 prefill/batch_decode/unified_forward 前向
   ▼
PrefillResult / DecodeResult / UnifiedResult（纯数据：token、KV 位置…）
   │
   ▼
调度器 resolve 成 StepEffects → frontend_adapter 写入 RequestLedger → 事件给服务器
```

关键性质：plan 与 result 都是**纯数据**，序列里没有任何引擎句柄或回调。这让「决定下一步」与「执行下一步」彻底解耦，也让模型本地基准可以绕开整个调度器、手工构造 plan 直接复用同一执行路径测时。

与 `runtime` 相对的高层入口在同一个文件里：`launch`（[src/lib.rs:L263](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/lib.rs#L263)）接收服务器翻译好的 `Qwen3LaunchOptions`，持有「Qwen3 启动策略」——TP→设备映射、LoRA↔CUDA Graph 互斥等，再分派到低层启动函数。`Qwen3LaunchOptions` 的文档同样强调「策略跟着模型走、不漏进服务器」（[src/lib.rs:L222-L227](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/lib.rs#L222-L227)）。

#### 4.3.3 源码精读

- [src/lib.rs:L198-L201](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/lib.rs#L198-L201) — `runtime` 的定位注释：production phase boundary；根服务器改用 `start_engine`。
- [src/lib.rs:L202-L220](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/lib.rs#L202-L220) — 再导出全文，可按来源分三组：
  - `executor` 组（L206-L218）：`Qwen3Executor`、`RequestId`、三对 plan/result 与四个 step/request 条目类型、`ConnectorLookupCounters`（offload 查询/命中计数）；
  - `split_kv` 组（L219）：`SplitKvConfig`；
  - `batch_decode_buffers` 组（L203-L205）：`SPLIT_KV_CHUNK_TOKENS`、`SPLIT_KV_TUNED_MAX_CHUNKS`、`split_chunk_size_for`（split-KV 的 chunk 常量与公式，供基准工具推算形状）。
- [src/lib.rs:L228-L256](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/lib.rs#L228-L256) — `Qwen3LaunchOptions`：服务器面启动旋钮（device/TP、CUDA Graph、dump 图、offload、LoRA、decode_overlap、batch_invariant、DFlash 草稿路径）。
- [src/lib.rs:L263-L352](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/lib.rs#L263-L352) — `launch` 全文：L271-L278 LoRA 服务强制关闭 CUDA Graph；L279-L289 校验图导出请求；L306-L320 三条互斥检查（decode-overlap×TP、DFlash×LoRA、DFlash×decode-overlap）；L321-L351 按 LoRA 与否分派到两个低层启动函数。
- [src/lib.rs:L354-L366](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/lib.rs#L354-L366) — `start_engine`：以默认值（offload 关闭、`DEFAULT_MAX_PREFILL_TOKENS` 等）转进 offload 版启动入口，是「不关心 Qwen3 细节的调用方」最省事的入口。

#### 4.3.4 代码实践

1. **实践目标**：验证「plan/result 只经 `runtime` 暴露、且调度器确实从这条边界进入执行器」。
2. **操作步骤**：
   - 执行 `rg -n "PrefillPlan" src/ | head -30`，观察定义处（`executor.rs`）与使用处（`scheduler/plan.rs`、`runtime` 再导出、`bin/` 下的基准）分别导入自哪条路径。
   - 执行 `rg -n "runtime::" src/ | head -20`，看 crate 内部谁在用 `runtime::` 前缀引用这些类型。
   - 对比 `cargo doc --release -p pegainfer-qwen3 --no-deps --open` 里 `pegainfer_qwen3` 的模块列表：应当只看到 `model_line`、`batch_decode_trace`、`runtime` 等少数公开模块。
3. **需要观察的现象**：`PrefillPlan` 的引用要么来自 `crate::executor`（crate 内），要么来自 `pegainfer_qwen3::runtime`（外部消费者）；rustdoc 首页的公开模块数量远少于 25 个。
4. **预期结果**：`runtime` 是外部使用 plan/result 的唯一入口；这正是「公共执行边界」的含义。**待本地验证**（rg 输出因版本略有差异）。

#### 4.3.5 小练习与答案

**练习 1**：为什么把 `SPLIT_KV_CHUNK_TOKENS` 这类**常量**也放进 `runtime` 再导出，而不是只导出类型？

**答案**：`runtime` 的消费者除了调度器还有模型本地基准（`bin/` 下的报告工具）。基准要为不同 batch × kv_len × variant 组合准备缓冲，就必须用与执行器**同一份** chunk 公式与常量（[src/lib.rs:L203-L205](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/lib.rs#L203-L205)）推算形状，否则「基准测的形状」与「服务执行的形状」会悄悄分叉。集中再导出保证两边物理上不可能不一致。

**练习 2**：`launch`（[src/lib.rs:L263](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/lib.rs#L263)）与 `Qwen3Executor`（经 `runtime` 暴露）分别服务谁？

**答案**：`launch` 服务服务器二进制：吃 `Qwen3LaunchOptions`，返回组装好、可直接进入引擎循环的 `Engine`，并在此层拦下非法组合（例如 LoRA 下强制关 CUDA Graph，[src/lib.rs:L271-L278](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/lib.rs#L271-L278)）。`Qwen3Executor` 服务调度器与基准：step 粒度的 plan→result 执行。前者是「一次性启动边界」，后者是「每步执行边界」。

**练习 3**：如果不设 `runtime` 层、让外部直接 `use pegainfer_qwen3::executor::DecodePlan`，会失去什么？

**答案**：至少失去三点——(1) `executor` 目前是私有模块，内部可以自由重排文件结构而不破坏外部；(2) 单一入口让「哪些类型属于稳定执行契约」一目了然，评审时只需盯住 `runtime` 的 diff；(3) 基准与调度器从同一名字空间取类型/常量，杜绝两套形状定义漂移。这正是「相位边界」在工程上的价值。

## 5. 综合实践

把本讲三块内容串成一个任务——**制作你的「pegainfer-qwen3 实验基线手册」**：

1. **模块分层表**（对应 4.2）：按 4.2.4 的步骤完成 25 个模块的分类表，存为 `notes/modules.md`（放在你自己的笔记目录，不要写进仓库）。表至少三列：模块 / 分类 / 一句话职责（注明来自哪个文件的顶部注释）。允许一个模块多标签。
2. **验证默认构建**（对应 4.1）：完成 4.1.4 的 `cargo tree` 对比实验，把两次输出的差集记录进笔记。
3. **测试基线**（为后续讲义铺路）：在仓库根执行

   ```bash
   cargo test --release -p pegainfer-qwen3 --lib
   ```

   记录：总测试数、通过数、失败数，以及是否有测试因为缺少 GPU 被跳过（skip）。按仓库 CLAUDE.md 的说明，`--lib` 单元测试不需要 GPU 与模型权重，而 `tests/` 目录下的集成测试（`hf_golden_gate` 等）需要 GPU + `PEGAINFER_TEST_MODEL_PATH` 指向的权重——把这条区别连同你的实测数字一起记下。**待本地验证**：不同机器上跳过的测试集合可能不同，以实际输出为准。
4. **边界确认**（对应 4.3）：完成 4.3.4 的 rg 实验，把「谁引用了 `PrefillPlan`」的清单贴进笔记。

这份笔记就是后续所有讲义的实验基线：以后每讲开头的实践都默认你能跑 `--lib` 单测，并知道哪些行为必须等 GPU 环境才能验证。

## 6. 本讲小结

- `pegainfer-qwen3` 是 PegaInfer 工作区中 Qwen3 模型线的模型 crate；「默认模型线」来自 `pegainfer-server` 的 `default = ["qwen3"]` + 工作区 `default-members`，因此 stock build 是纯 Rust + CUDA、无 Python。
- 本 crate 自身只有三个工具性 feature：`kernel-call-trace`、`kernel-report`（带出两个诊断二进制与可选依赖）、`test-fixtures`；没有 default feature。
- `src/lib.rs` 的 25 行模块声明是全 crate 目录页，可归类为：配置 / 权重 / 前向执行 / 调度 / 执行器 / 投机解码 / LoRA / KV 数据面 / 契约接入 / 诊断工具。
- 两个契约接入模块分工明确：`model_line` 面向 CLI，`frontend_adapter` 面向引擎契约；`scheduler` 是 engine-contract-free 的纯调度。
- `pub mod runtime` 是公共执行边界：三对 plan/result、`Qwen3Executor`、`RequestId` 与 split-KV 常量从这里对外暴露，调度器与模型本地基准共用这一个入口；服务器走更高层的 `launch` / `start_engine`。

## 7. 下一步学习建议

- 下一讲（u1-l2）将进入测试地图：`--lib` 单测与 `tests/` 下 GPU 集成测试的区别、`tests/common/harness.rs` 如何把 step 批量协议还原成阻塞流——你在综合实践第 3 步记录的基线正好用上。
- 想先睹调度与契约分层的话，直接读 [src/scheduler.rs:L1-L9](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/scheduler.rs#L1-L9) 与 [src/frontend_adapter.rs:L1-L13](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/frontend_adapter.rs#L1-L13) 的模块注释，这两段注释是本手册整个第 4 单元的纲。
- 想了解工作区全貌，读仓库根 `README`/`CLAUDE.md` 中「Architecture」一节的架构图，再回到本讲 4.2.2 的调用链对照。
