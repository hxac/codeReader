# pegainfer-sim 是什么：无 GPU 的模拟推理引擎

> 本讲是 openinfer-pegainfer-sim 学习手册的第一讲（u1-l1，beginner）。所有永久链接基于 HEAD `af9e40e12108141bb18f93188c82d2583df18509`。

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 pegainfer-sim 存在的三个理由：**前端协议**、**调度器行为**与**指标**都可以在纯 CPU、无模型权重的环境下验证与基准化。
2. 对照 `Cargo.toml` 解释 `anyhow` / `clap` / `tokio` / `logforth` 与 `pegainfer-frontend` 各自承担什么，并画出这个 crate 的第一层依赖图。
3. 独立完成 `--release` 构建与 `--lib` 单元测试运行，并说出单元测试分布在哪三个源文件里。

本讲不要求你理解任何一条调度算法的细节——那是第二、三单元的事。本讲只解决一个问题：**这个 crate 是什么、由哪些文件组成、怎么跑起来验证它**。

## 2. 前置知识

- **推理引擎的分层**：一个 LLM 服务通常分三层——HTTP 前端（解析 OpenAI 格式请求、SSE 流式输出）、调度器（决定每一步算哪些请求、prefill 还是 decode）、执行层（真正的 GPU kernel）。PegaInfer 里前两层在工作区根 `Cargo.toml` 的成员 `pegainfer-frontend` 中有清晰契约（`Engine`、`Scheduler`、`spawn_scheduler` 等）。
- **两个延迟术语**：
  - **TTFT**（Time To First Token）：从请求提交到第一个输出 token 的时间，主要由 prefill（读 prompt）决定。
  - **TPOT**（Time Per Output Token）：之后每生成一个 token 的时间，由 decode 步长决定。
  - pegainfer-sim 这类"假引擎"不真的算模型，而是**按参数指定的时间节拍假装在算**，所以这两个词会在源码里到处出现。
- **Rust workspace**：多个 crate 共享一个根 `Cargo.toml`。依赖写 `{ workspace = true }` 表示"版本与特性由工作区依赖表统一决定"。命令行用 `-p <crate名>` 选定在哪个 crate 上执行。
- **cargo 的测试目标**：`--lib` 只跑库代码里 `#[cfg(test)] mod tests` 的单元测试；`tests/` 目录下每个 `.rs` 文件会被编译成一个独立的**集成测试**二进制（默认也会被 `cargo test` 跑到）。
- **vLLM / OpenAI 兼容 API**：指 `/v1/completions`、`/v1/chat/completions` 这套请求/响应格式。pegainfer-sim 的价值之一就是不需要 GPU 就能把这些协议路径全跑一遍。

## 3. 本讲源码地图

pegainfer-sim 是一个很小的 crate：`src/` 只有 4 个文件，`tests/` 只有 3 个测试文件加 4 个 fixture，全部加起来不到 6000 行。

| 文件 | 行数 | 作用 |
|---|---|---|
| [Cargo.toml](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/Cargo.toml) | 29 | crate 清单：唯一的二进制目标 + 依赖声明 |
| [src/lib.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs) | 1047 | 库主体：`SimulatedEngineConfig`（全部旋钮）、`start_engine`、`SimScheduler`（含 legacy 车道与 ProfiledRuntime 车道）、输出规划 |
| [src/main.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/main.rs) | 258 | CLI 壳：clap 参数解析、legacy/`--profile` 两种模式的装配、日志初始化，最后调用前端的 `vllm::serve` |
| [src/worker.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs) | 930 | 确定性泛型调度状态机 `WorkerState`（vLLM V1 风格的 Waiting/Prefill/Decode 三阶段、准入拒绝、取消） |
| [src/profile.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/profile.rs) | 830 | 引擎档案（JSON）契约：加载、sha256 清单校验、`SchedulerProfile` 约束、三线性插值步计时 |
| [src/logprobs.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/logprobs.rs) | 46 | logprobs 协议夹具：为 prompt echo 与补全 token 合成固定分数 |
| [tests/frontend_e2e.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/tests/frontend_e2e.rs) | 2168 | 端到端测试主力：`SimServer` 启动器 + OpenAI 协议、指标、取消、profile 车道等几十个用例 |
| [tests/frontend_e2e/logprobs.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/tests/frontend_e2e/logprobs.rs) | 201 | logprobs 边界用例子模块（被 `frontend_e2e.rs` 引入） |
| [tests/tool_call_roundtrip.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/tests/tool_call_roundtrip.rs) | 416 | 工具调用往返：脚本化补全 + 定制 tokenizer，验证 `<tool_call>` JSON 跨流式分片重组 |
| [tests/fixtures/](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/tests/fixtures) | 4 个文件 | 两份 profile bundle（`online-step-gate` 与 `online-zero-cost`），各含 profile JSON + 校准 manifest JSON |

## 4. 核心概念与源码讲解

### 4.1 crate 依赖图：一个"只依赖前端"的极简引擎

#### 4.1.1 概念说明

PegaInfer 的绝大多数成员（qwen3、kimi-k2、glm52……）都要 CUDA、cuBLAS、模型权重才能跑。这带来一个很现实的工程问题：**前端协议、调度器行为、Prometheus 指标这些"GPU 之外"的逻辑，如何在没有 GPU 的机器上测试和基准化？**

pegainfer-sim 就是答案。它在 [src/main.rs:L24-L27](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/main.rs#L24-L27) 里自我介绍：

```rust
#[command(
    name = "pegainfer-sim",
    about = "CPU-only simulated inference server for OpenAI/vLLM serving benchmarks"
)]
```

由此可以归纳它存在的三个理由（也是它的三类测试各自锁定的东西）：

1. **前端协议可在 CPU 上验证**——OpenAI 兼容的 `/v1/completions`、SSE 流、`logprobs`、tool calls，全部走真实前端代码，但引擎是假的（见 `tests/frontend_e2e.rs` 与 `tests/tool_call_roundtrip.rs`）。
2. **调度器行为可确定性复现**——`src/worker.rs` 是一个纯内存的 vLLM V1 风格调度状态机，同样的输入永远得到同样的步序列（单元测试直接断言这一点）。
3. **指标与性能基准可测**——Prometheus 计数器（投机解码、前缀缓存）在 e2e 里断言；工作区文档 `docs/subsystems/frontend/cpu-profiling-baseline.md` 与 `docs/subsystems/frontend/sim-high-concurrency-bench.md` 记录了用 pegainfer-sim 做前端 CPU 基准的完整方法（在仓库根 [docs/index.md:L185-L189](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/docs/index.md#L185-L189) 可查到这两篇的索引行）。

它不是玩具：仓库文档明确记录 pegainfer-sim 已从旧的 `EngineHandle`/`TokenEvent` 契约迁移到与真实模型 crate 相同的 **step contract**（`docs/subsystems/frontend/sim-step-contract.md`），所以它测过的协议路径对真实引擎同样有效。

#### 4.1.2 核心流程

pegainfer-sim 启动后的请求路径（本讲只需要看懂箭头方向）：

```text
HTTP 请求（OpenAI 格式）
   │
   ▼
pegainfer_frontend::vllm::serve   ← axum HTTP 栈、SSE、tokenizer 装配（真实代码）
   │
   ▼
Engine { schedulers: [...], info, lora }
   │  由 start_engine() 装配，每个分区一个线程
   ▼
SimScheduler（pegainfer-sim 的"假"调度器）
   ├─ 未配置 profile → step_legacy 车道：固定 TTFT/TPOT 节拍发 token
   └─ 配置了 --profile → ProfiledRuntime 车道：WorkerState 状态机 + 三线性插值步计时
   │
   ▼
RequestLedger（前端契约）→ TokenEvent/SSE 响应
```

关键点：**HTTP 层与 ledger 全是真实生产代码，只有最底下的"算 token"被替换成了模拟**。这正是它作为测试基础设施的份量所在。

#### 4.1.3 源码精读

先看 crate 清单的依赖段——[Cargo.toml:L11-L25](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/Cargo.toml#L11-L25)：

```toml
[dependencies]
anyhow = { workspace = true }
clap = { workspace = true }
log = { workspace = true }
logforth = { workspace = true }
pegainfer-frontend = { workspace = true }
serde = { workspace = true }
serde_json = { workspace = true }
sha2 = { workspace = true }
tokio = { workspace = true, features = ["full"] }

[dev-dependencies]
reqwest = { workspace = true, features = ["json"] }
tempfile = { workspace = true }
tokio-util = { workspace = true }
```

每个依赖的分工：

| 依赖 | 类别 | 在本 crate 里承担什么 |
|---|---|---|
| `pegainfer-frontend` | **工作区成员**（本地 crate） | 引擎契约（`Engine`/`Scheduler`/`spawn_scheduler`/`RequestLedger`）+ vLLM HTTP 服务 `serve`。sim 只依赖它，不依赖任何 kernel/GPU crate——这就是"纯 CPU"的结构保证 |
| `anyhow` | 外部（crates.io） | 错误类型与 `ensure!`/`bail!` 断言，所有 `Result` 的载体 |
| `clap` | 外部 | `main.rs` 的 CLI 参数定义（`#[derive(Parser)]`） |
| `tokio`（full） | 外部 | 异步运行时：`#[tokio::main]` 驱动 HTTP 服务；注意调度器线程本身是同步 OS 线程 |
| `log` + `logforth` | 外部 | 日志门面与实现：`init_logging` 用 logforth 的 `EnvFilter` 读 `RUST_LOG` |
| `serde` / `serde_json` | 外部 | profile JSON 的反序列化（`deny_unknown_fields` 严格契约） |
| `sha2` | 外部 | profile 校准 manifest 的 sha256 摘要校验 |
| `reqwest` / `tempfile` / `tokio-util` | 外部，仅 dev | e2e 测试发 HTTP 请求、建临时目录等 |

两点值得注意：

- **没有 `pegainfer-kernels`、没有 CUDA 相关依赖**。整个 crate 的 GPU 相关内容为零，这从依赖层面锁定了"任何机器都能跑"。
- 所有依赖都以 `{ workspace = true }` 继承自工作区依赖表（仓库根 [Cargo.toml:L40-L48](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/Cargo.toml#L40-L48) 起的 `[workspace.dependencies]`）。其中只有 `pegainfer-frontend` 解析到工作区内的本地路径成员（成员列表见 [Cargo.toml:L3-L26](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/Cargo.toml#L3-L26)，`pegainfer-sim` 自己在 L5）。

另外注意 [Cargo.toml:L7-L9](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/Cargo.toml#L7-L9) 显式声明了二进制目标：这个 crate 既是一个库（被自己的测试和潜在的基准工具引用），也是一个可执行文件 `pegainfer-sim`（入口 `src/main.rs`）。

```toml
[[bin]]
name = "pegainfer-sim"
path = "src/main.rs"
```

#### 4.1.4 代码实践

**实践目标**：亲眼看到"pegainfer-sim 的依赖树里没有任何 GPU crate"。

**操作步骤**（在工作区根目录执行）：

1. 运行 `cargo tree -p pegainfer-sim --depth 1`，列出直接依赖。
2. 对照上面 4.1.3 的表格，把输出里的每个依赖分成两栏：**工作区成员**（本地 crate）与**外部 crate**（crates.io / git）。判据：出现在仓库根 `Cargo.toml` 的 `members` 列表里的是前者。
3. 再运行 `cargo tree -p pegainfer-sim --depth 2`，观察 `pegainfer-frontend` 向下引出了哪些 crate（例如 axum 一系的 HTTP 依赖），确认 GPU 相关 crate（`pegainfer-kernels`、`cudarc` 之类）不在树里。

**需要观察的现象**：第一层依赖恰好是 `Cargo.toml` 里那 9 个；树里搜不到任何 CUDA/cudarc/nvcc 相关包名。

**预期结果**：`pegainfer-frontend` 是唯一的本地工作区成员直接依赖。（`cargo tree` 输出与本地环境有关，具体渲染待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：如果删掉 `Cargo.toml` 里的 `sha2` 依赖，哪个源文件会编译失败？

<details><summary>参考答案</summary>

`src/profile.rs`。它在 [profile.rs:L14-L15](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/profile.rs#L14-L15) `use sha2::{Digest, Sha256}`，用于 [load_from_path](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/profile.rs#L48-L70) 里对校准 manifest 做 sha256 校验。
</details>

**练习 2**：`reqwest` 为什么放在 `[dev-dependencies]` 而不是 `[dependencies]`？

<details><summary>参考答案</summary>

`reqwest` 只被 `tests/` 下的集成测试用来向启动好的服务器发 HTTP 请求（如 `tests/frontend_e2e.rs` 中各用例）。dev-dependencies 不参与正常构建，所以 `cargo build` 出来的 `pegainfer-sim` 二进制不会带上 HTTP 客户端——服务器自己只需要监听，不需要主动请求。
</details>

**练习 3**：仓库根 `Cargo.toml` 里 `default-members = ["pegainfer-server"]`，这对你运行 pegainfer-sim 意味着什么？

<details><summary>参考答案</summary>

在工作区根直接 `cargo run` / `cargo build` 只作用于默认成员 `pegainfer-server`，不会构建 sim。必须显式加 `-p pegainfer-sim`（例如 `cargo run --release -p pegainfer-sim`），或者进入 `pegainfer-sim/` 子目录执行。
</details>

### 4.2 src 模块划分：一个 lib、两个调度车道、一组协议夹具

#### 4.2.1 概念说明

`src/` 的四个文件各管一件事，职责边界很干净：

- **`lib.rs`** 是对外的"引擎装配车间"：定义 `SimulatedEngineConfig`（所有旋钮）和 `start_engine`（把配置变成前端可用的 `Engine`），并实现 `Scheduler` trait 的 `SimScheduler`。
- **`worker.rs`** 是与定时无关的纯调度状态机（`WorkerState`），只在 profile 车道被使用；它不睡眠、不看时钟，输入相同时输出永远相同。
- **`profile.rs`** 是档案（JSON）契约：怎么加载、怎么校验、怎么用三线性插值把"一步的形状"换算成"一步要多少微秒"。
- **`logprobs.rs`** 是协议夹具：当请求要 `logprobs` 时，为每个 token 合成固定的分数结构，让前端的 logprobs 通路有东西可传。

一个重要的结构性事实：`main.rs` **只是壳**。真正的库入口在 [lib.rs:L24-L38](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L24-L38)：

```rust
pub mod profile;
pub mod worker;

use profile::EngineProfile;
use profile::LoadedEngineProfile;
use worker::WorkerState;
// …若干 use…

mod logprobs;
```

`profile` 与 `worker` 是公开模块（测试直接 `use pegainfer_sim::worker::WorkerState` 这样引用），`logprobs` 是 crate 私有模块（只在内部使用）。

#### 4.2.2 核心流程

`SimScheduler` 有**两条互斥的车道**，在 `step` 入口处分发（见下方 4.2.3 源码）。这决定了 crate 的学习顺序：

- **legacy 车道**（不配置 profile）：每个请求的 TTFT 与 TPOT 由常量参数直接给出：

  \[ \text{TTFT} = \text{base\_ttft\_ms} + \frac{\text{prompt\_len}}{\text{prefill\_tokens\_per\_ms}} \qquad \text{TPOT} = \text{tpot\_ms} \]

  调度器按这两个节拍"到点发一个 token"，输出内容则循环复用 prompt 里的 token（或重放脚本）。

- **profiled 车道**（`--profile FILE`）：先由 `WorkerState` 决定"这一步装哪些请求、什么形状"，再用档案里的计时网格插值出这一步的微秒数，睡够后才推进。这条车道是对真实 vLLM V1 调度的可复现模仿。

两条车道在 [SimScheduler::step](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L624-L641) 的分发逻辑（伪代码）：

```text
step(ledger):
    if 配置了 profile:
        交给 ProfiledRuntime::step   # 准入 → 规划 → 定价 → 睡眠 → 完成
    else:
        走 step_legacy               # 准入 → TTFT 到点发首 token → 每 TPOT 发一个
```

而 `main.rs` 的 `main()` 做的事可以概括为一行链：**解析参数 → 装配 RuntimeConfig → start_engine → 交给前端 serve**（见 4.2.3 最后一段引用）。

#### 4.2.3 源码精读

**（1）车道的安装点**。[lib.rs:L591-L605](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L591-L605)：`SimScheduler::new` 在构造时就根据配置决定是否装配 `ProfiledRuntime`：

```rust
fn new(config: SimulatedEngineConfig) -> Self {
    let spec_decode = config
        .spec_decode
        .map(|(k, _)| SpecDecodeCounters::new(k).expect("K checked at config time"));
    let profiled = config.profile.as_ref().map(ProfiledRuntime::new);
    // …
}
```

**（2）车道的分发点**。[lib.rs:L619-L641](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L619-L641) 是 `SimScheduler` 对前端 `Scheduler` 契约的实现——`step` 里那个 `if self.profiled.is_some()` 就是两条车道的岔口：

```rust
impl Scheduler for SimScheduler {
    fn submit(&mut self, request: QueuedRequest) {
        self.queued.push(request);
    }

    fn step(&mut self, ledger: &mut RequestLedger) -> Result<()> {
        if self.profiled.is_some() {
            // …取出 ProfiledRuntime，走 profiled.step(…)，再放回去
            return result;
        }
        self.step_legacy(ledger)
    }
```

**（3）引擎的装配函数**。[lib.rs:L174-L198](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L174-L198)：`start_engine_with_partitions` 为每个分区 `spawn_scheduler` 起一个独立线程，`EngineInfo` 里 `kv_capacity`/`servable_len` 都是 `None`——模拟引擎没有 KV 缓存预算可言：

```rust
pub fn start_engine_with_partitions(config: &SimulatedEngineConfig, partitions: usize) -> Engine {
    assert!(partitions > 0, "an engine must expose at least one scheduler");
    Engine {
        schedulers: (0..partitions)
            .map(|index| spawn_scheduler(
                &format!("pegainfer-sim-{index}"),
                SimScheduler::new(config.clone()),
            ))
            .collect(),
        info: EngineInfo { kv_capacity: None, servable_len: None },
        lora: None,
    }
}
```

**（4）main 的收尾**。[main.rs:L207-L225](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/main.rs#L207-L225)：`main()` 把装配好的引擎交给前端的 `vllm::serve`，之后 HTTP 层的一切都由 `pegainfer-frontend` 接管：

```rust
#[tokio::main]
async fn main() -> Result<()> {
    init_logging();
    let args = Args::parse();
    let runtime = build_runtime(&args)?;
    report_profile(&runtime);
    let engine = start_engine(&runtime.engine);

    pegainfer_frontend::vllm::serve(
        std::future::ready(Ok(engine.into())),
        &runtime.model_path,
        runtime.served_model_name,
        pegainfer_frontend::vllm::ParserSelection::Auto,
        args.port,
        Some(runtime.max_model_len),
        pegainfer_frontend::vllm::shutdown_token_from_ctrl_c(),
    )
    .await
}
```

这段 18 行的 `main` 是"库做全部工作、二进制只做胶水"的典型样板。

#### 4.2.4 代码实践

**实践目标**：确认单元测试的分布，并跑通 `--lib` 测试。

**操作步骤**：

1. 先不运行，只数一数：分别在 [src/lib.rs:L802](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L802)、[src/worker.rs:L583](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L583)、[src/profile.rs:L636](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/profile.rs#L636) 的 `#[cfg(test)] mod tests` 里统计 `#[test]` 个数（提示：分别为 8、5、5 个）。
2. 运行 `cargo test --release -p pegainfer-sim --lib -- --list`，列出所有测试名。
3. 运行 `cargo test --release -p pegainfer-sim --lib`，记录结尾的 `test result: ok. N passed` 中 的 N。

**需要观察的现象**：`--list` 输出里每个测试名带模块前缀，例如 `tests::fake_token_id_cycles_prompt_tokens`（lib.rs）、`worker::tests::…`、`profile::tests::…`——前缀直接告诉你它住在哪个文件。

**预期结果**：`--lib` 应跑 18 个测试（8 + 5 + 5）并全部通过。注意 [main.rs:L227-L258](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/main.rs#L227-L258) 里还有 2 个测试，但它们属于**二进制目标**，不会被 `--lib` 跑到（要 `cargo test -p pegainfer-sim --bins`）。精确数字以本地输出为准（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：`SimScheduler` 实现的 `Scheduler` trait 来自哪里？这说明 sim 与真实模型 crate（如 qwen3）之间是什么关系？

<details><summary>参考答案</summary>

来自 `pegainfer_frontend::engine::Scheduler`（[lib.rs:L19-L22](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L19-L22) 的 use 列表）。真实模型 crate 实现的是同一个 trait、走同一个 `spawn_scheduler`/`RequestLedger` 契约，所以 sim 测出的前端协议与调度语义对真实引擎同样成立——这正是它作为测试基础设施的价值。
</details>

**练习 2**：为什么 `logprobs` 模块用 `mod logprobs;`（私有）而不是 `pub mod`？

<details><summary>参考答案</summary>

它只提供 crate 内部的协议夹具（`prompt`/`completion` 两个函数，见 [logprobs.rs:L9-L33](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/logprobs.rs#L9-L33)），供两条车道在请求带 `logprobs`/`prompt_logprobs` 时调用。外部使用者通过 `SimulatedEngineConfig` 间接驱动它，没有直接调用的需要，所以不进公共 API。
</details>

**练习 3**：不看源码回答：`start_engine(config)` 和 `start_engine_with_partitions(config, 3)` 返回的 `Engine` 有什么差别？

<details><summary>参考答案</summary>

前者的 `engine.schedulers.len() == 1`（它只是转发到 `partitions = 1`，见 [lib.rs:L174-L176](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L174-L176)），后者是 3——即起 3 个名为 `pegainfer-sim-0/1/2` 的调度器线程。单元测试 `start_engine_with_partitions_exposes_that_many_schedulers`（[lib.rs:L1043-L1046](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L1043-L1046)）锁定的就是这个行为。
</details>

### 4.3 tests 布局：把模拟引擎当真实服务来测

#### 4.3.1 概念说明

`tests/` 目录的每个 `.rs` 文件都是一个独立的集成测试二进制，它们把 pegainfer-sim 当成一个**真实被测服务**：

- **`frontend_e2e.rs`（2168 行）**：最大的一个。核心是 `SimServer` 启动器——用 `start_engine` + 前端 `serve` 在环回地址上真起一个 HTTP 服务器，然后用 `reqwest` 像客户端一样发请求、断言响应与 `/metrics`。它覆盖 OpenAI completions 格式、流式 SSE、取消、profile 车道确定性回放、指标计数器等。
- **`frontend_e2e/logprobs.rs`**：`frontend_e2e.rs` 通过模块引入的子文件，专攻 logprobs 边界（`logprobs=0`、单 token prompt 等）。
- **`tool_call_roundtrip.rs`**：用脚本化补全（`with_scripted_completion`）+ 一份手工定制的 Fuse tokenizer，让 `<tool_call>` JSON 跨多个流式分片送达客户端，验证重组正确。
- **`fixtures/`**：两份 **profile bundle**。每份是两个文件：`*.json`（引擎档案本体）+ `*.manifest.json`（校准清单，档案里记录它的相对路径和 sha256）。它们是 `--profile` 车道在 e2e 里的测试数据。

#### 4.3.2 核心流程

`frontend_e2e.rs` 里 `SimServer` 的启动套路（各 spawn 变体的公共骨架）：

```text
SimServer::spawn_xxx()
  1. 写一份最小模型元数据到临时目录（tokenizer 等必需文件）
  2. 构造 SimulatedEngineConfig（按需叠加 with_* 旋钮）
  3. start_engine → pegainfer_frontend::vllm::serve 起在预留的环回端口上
  4. 轮询 /health 直到就绪（最多 SERVER_START_ATTEMPTS 次）
  5. 返回 SimServer（Drop 时优雅关闭）
```

端口预留（`reserve_loopback_port`）+ 多次重试（`SERVER_START_ATTEMPTS = 5`）是为了在并行 CI 下不因端口冲突而抖动；各测试用不同的 `MODEL_NAME` 常量（如 `pegainfer-sim-e2e`、`pegainfer-sim-e2e-metrics`……）避免 Prometheus 指标串台。

#### 4.3.3 源码精读

**（1）测试专属常量群**。[tests/frontend_e2e.rs:L40-L66](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/tests/frontend_e2e.rs#L40-L66) 开头先声明一组模型名与阈值常量——每个名字对应一组测试场景，从名字就能读出用途：

```rust
const HTTP_TIMEOUT: Duration = Duration::from_secs(10);
const MODEL_NAME: &str = "pegainfer-sim-e2e";
const METRICS_MODEL_NAME: &str = "pegainfer-sim-e2e-metrics";
const SLOW_METRICS_MODEL_NAME: &str = "pegainfer-sim-e2e-slow-metrics";
const SPEC_METRICS_MODEL_NAME: &str = "pegainfer-sim-e2e-spec-metrics";
// …
const SERVER_START_ATTEMPTS: usize = 5;
```

**（2）SimServer 及其一族 spawn 变体**。[tests/frontend_e2e.rs:L68-L199](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/tests/frontend_e2e.rs#L68-L199)：`struct SimServer` 只有 68 行处一个定义，但 `impl` 里有整整一族 `spawn*` 构造器——每个变体对应一种引擎配置（默认、分区、带投机解码、带前缀缓存、慢 TTFT、自定义模型目录……），最终都汇到通用的 `spawn_with_config` 与最底层的 `spawn_once`：

```rust
struct SimServer { /* L68 */ }
impl SimServer {
    async fn spawn() -> Result<Self> { /* L77 */ }
    async fn spawn_partitioned() -> Result<Self> { /* L81 */ }
    async fn spawn_with_drafter() -> Result<Self> { /* L93 */ }
    async fn spawn_with_prefix_cache_as(model_name: &'static str) -> Result<Self> { /* L110 */ }
    // …spawn_slow / spawn_with_model_dir / spawn_with_config / spawn_once…
}
```

**（3）fixture 的结构**。以 [tests/fixtures/online-zero-cost.json](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/tests/fixtures/online-zero-cost.json#L1-L40) 为例，一份档案分四段：`schema_version`、`calibration`（指向 manifest 及其 sha256）、`scheduler`（调度画像：policy/seqs/batched_tokens/model_len/prefill 策略）、`predictor`（覆盖域 + 计时网格，这里全部填 0 表示"每步零成本"，让 e2e 只测调度逻辑不真等待）：

```json
{
  "schema_version": 1,
  "calibration": {
    "manifest": "online-zero-cost.manifest.json",
    "sha256": "ff93f08f9f81855cea5b96c99cbad188ba708d99ca340aa6a9b05282c1123926"
  },
  "scheduler": {
    "policy": "vllm_v1",
    "max_num_seqs": 2,
    "max_num_batched_tokens": 4,
    "max_model_len": 16,
    "prefill": { "mode": "chunked", "max_chunk_tokens": 2 }
  },
  "predictor": { "coverage": { "…": [0, 2] }, "grid": { "step_duration_us": [0, 0, …] } }
}
```

加载它的函数是 [profile_fixture](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/tests/frontend_e2e.rs#L278-L297)（把 fixture 拷进临时目录再走正式的 `load_from_path`），第一个 e2e 用例 [simulated_engine_serves_openai_completions_over_http](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/tests/frontend_e2e.rs#L393) 则演示了最基础的一条链。

#### 4.3.4 代码实践

**实践目标**：把 fixture 文件与 CLI 的 `--profile` 参数对上号，建立"测试数据 ↔ 运行入口"的映射。

**操作步骤**：

1. 打开 [tests/fixtures/online-zero-cost.json](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/tests/fixtures/online-zero-cost.json) 与 [tests/fixtures/online-step-gate.json](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/tests/fixtures/online-step-gate.json)，逐段抄下两份档案在 `scheduler` 段的差异（`max_model_len`、prefill 的 mode 等）。
2. 在 [src/main.rs:L63-L65](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/main.rs#L63-L65) 找到 `--profile` 参数定义，再追踪 [build_runtime](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/main.rs#L77-L114) 里 `if let Some(path) = &args.profile` 分支——注意它同时要求档案里的 `model_id`、`max_model_len` 与命令行声明一致，否则拒绝启动。
3. 思考题自答：为什么 fixture 的 `step_duration_us` 全填 0？（答案在本节概念说明里。）

**需要观察的现象**：两份 fixture 的差异恰好对应两个测试场景（零成本调度回放 vs 步进门控行为）；档案与 manifest 是一对，靠 sha256 绑定。

**预期结果**：能说出"`--profile tests/fixtures/online-zero-cost.json`（连同同目录 manifest）就是让服务器走 profiled 车道的最小测试数据"。实际启动验证留到下一讲（u1-l2）的实践中完成。

#### 4.3.5 小练习与答案

**练习 1**：`tests/` 下为什么没有 `worker_state_test.rs` 这样的独立文件，`WorkerState` 的测试在哪里？

<details><summary>参考答案</summary>

`WorkerState` 的单元测试内嵌在源文件里：[worker.rs:L583-L584](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L583-L584) 的 `#[cfg(test)] mod tests`（5 个测试）。它不需要 HTTP、端口或进程，所以放单元测试；`tests/` 目录留给需要真实起服务器的端到端场景。
</details>

**练习 2**：`frontend_e2e/logprobs.rs` 为什么放在 `frontend_e2e.rs` 旁边的一个**目录**里，而不是 `tests/` 顶层？

<details><summary>参考答案</summary>

Cargo 把 `tests/` 顶层的每个 `.rs` 编译成独立测试二进制，但目录不算新目标。把 logprobs 用例放进 `frontend_e2e/` 子目录、由 `frontend_e2e.rs` 以模块方式引入（如 `mod logprobs;`），它们就能**复用同一个 `SimServer` 启动骨架与常量**，同时避免顶层再生成一个重复编译公共设施的二进制。
</details>

**练习 3**：如果让你给 `frontend_e2e.rs` 增加一个"新旋钮"的 e2e 场景，按现有惯例第一步应该做什么？

<details><summary>参考答案</summary>

在常量区（[L40-L66](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/tests/frontend_e2e.rs#L40-L66)）加一个专属的 `*_MODEL_NAME` 常量（如 `pegainfer-sim-e2e-<场景名>`），防止 Prometheus 指标与其他场景串台；然后在 `impl SimServer` 里加一个对应的 `spawn_xxx` 变体。这一模式在第四单元（u4-l4）的综合实践里会完整走一遍。
</details>

## 5. 综合实践

**任务**：完成一次"构建 → 测试 → 依赖图标注"三连，产出一份本讲学习笔记。

**操作步骤**（都在仓库根目录执行；本讲是阅读型验证，不修改任何源码）：

1. **构建**：
   ```bash
   cargo build --release -p pegainfer-sim
   ```
   按仓库约定始终用 `--release`。构建产物是 `target/release/pegainfer-sim` 可执行文件。
2. **单元测试**：
   ```bash
   cargo test --release -p pegainfer-sim --lib
   ```
   记录 `test result:` 行里通过的测试数量，并对照 4.2.4 的预测（应为 18 个：lib.rs 8 + worker.rs 5 + profile.rs 5；以本地输出为准）。
3. **依赖图**：
   ```bash
   cargo tree -p pegainfer-sim --depth 1 > /tmp/sim-deps.txt
   ```
   把输出整理成两栏表格：左栏"工作区成员"（判据：出现在仓库根 `Cargo.toml` 的 `members` 列表，如 `pegainfer-frontend`），右栏"外部 crate"（crates.io 或 git 来源，如 anyhow/clap/tokio/logforth/serde/sha2）。
4. **交叉验证**：确认 `/tmp/sim-deps.txt` 里没有任何 CUDA/GPU 相关 crate；再翻回 [Cargo.toml:L11-L20](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/Cargo.toml#L11-L20) 逐项对上"9 个直接依赖、8 个来自外部 + 1 个本地成员"。

**需要观察的现象**：

- 构建无需 GPU、无需模型权重，在任何装了 Rust 工具链的机器上都能完成；
- `--lib` 测试在数秒内完成（不启动任何服务器、不绑定端口）；
- 依赖树第一层与 Cargo.toml 声明完全一致。

**预期结果**：一份包含"通过的测试数、两栏依赖表、确认无 GPU 依赖"的记录。本讲不启动服务器——那是 u1-l2 的实践内容。（具体输出数字待本地验证。）

## 6. 本讲小结

- **pegainfer-sim 是纯 CPU 的"假"推理引擎**：HTTP 前端、请求契约、ledger 全是真实生产代码，只有 token 计算与定时被模拟，用于无 GPU 环境下验证前端协议、调度行为与指标（仓库文档用它做前端 CPU 基准）。
- **依赖图极简且无 GPU**：9 个直接依赖全部 `{ workspace = true }` 继承，其中只有 `pegainfer-frontend` 是本地工作区成员；`reqwest`/`tempfile`/`tokio-util` 只在 dev-dependencies 供 e2e 使用。
- **src 四文件分工**：`lib.rs` 装配引擎与两条调度车道（legacy 定时 vs profiled 档案，岔口在 `SimScheduler::step`）；`worker.rs` 是确定性调度状态机；`profile.rs` 是 JSON 档案契约与插值计时；`logprobs.rs` 是协议夹具。`main.rs` 只是 18 行的 CLI 壳。
- **单元测试藏在源文件里**：`cargo test -p pegainfer-sim --lib` 跑 lib.rs(8) + worker.rs(5) + profile.rs(5)；main.rs 的 2 个测试属二进制目标，需 `--bins`。
- **tests/ 布局对应测试形态**：`frontend_e2e.rs`（含 `frontend_e2e/logprobs.rs` 子模块）用 `SimServer` 真起 HTTP 服务做端到端断言；`tool_call_roundtrip.rs` 专攻工具调用流式重组；`fixtures/` 是两份"档案 + manifest"的 profile bundle。
- **运行入口**：工作区根有 `default-members = ["pegainfer-server"]`，操作 sim 必须带 `-p pegainfer-sim`。

## 7. 下一步学习建议

- **下一讲（u1-l2）**：《CLI 与服务器引导：legacy 定时模式与 --profile 档案模式》——精读 `main.rs` 的 clap 参数、`build_runtime` 的两种模式装配、`--profile` 与定时参数互斥的防御校验，并第一次真正把服务器跑起来用 `curl` 打它。
- **延伸阅读**（可选）：仓库文档 [docs/subsystems/frontend/sim-step-contract.md](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/docs/subsystems/frontend/sim-step-contract.md) 讲 sim 如何被切到 step contract；[docs/subsystems/frontend/sim-high-concurrency-bench.md](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/docs/subsystems/frontend/sim-high-concurrency-bench.md) 展示它做基准的完整用法。
- **源码预读**：把 [src/lib.rs:L672-L740](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L672-L740) 的 `step_legacy` 通读一遍，带着"TTFT/TPOT 怎么变成睡眠"的问题进入第二单元。
