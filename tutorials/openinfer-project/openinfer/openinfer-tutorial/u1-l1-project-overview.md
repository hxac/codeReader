# 项目总览：PegaInfer 是什么

## 1. 本讲目标

读完本讲，你应该能够：

1. 用一句话说清 PegaInfer 的定位：一个**无 PyTorch、无 Python 运行时**的纯 Rust + CUDA LLM 推理引擎，对外提供 OpenAI 兼容 API。
2. 列出七条模型线（Qwen3、Qwen3.5、Gemma 4、DeepSeek-V2-Lite、Kimi-K2、GLM-5.2、Kimi-K3）以及它们各自的 cargo feature 名，并说明为什么**只有 `qwen3` 默认开启**。
3. 理解项目的核心架构思想——**「共享基础设施，模型自持执行」**（Share the infrastructure; let each model own its execution），并能按「前端契约 / 模型 crate / 共享运行时 / KV 基础设施」四层给 20 个 workspace 成员归类。

本讲是整套手册的第一讲，不要求你写过 CUDA 或读过推理框架源码——所有术语都会先解释再使用。

## 2. 前置知识

### 2.1 什么是 LLM 推理引擎

大语言模型（LLM）收到一段文本后，是**一个 token 一个 token** 地生成回答的（token 可以粗略理解为「子词」，例如「机器学习」可能被切成两三个 token）。驱动这个过程、并把模型跑得又快又省显存的软件，就叫**推理引擎**。一次生成通常分两个阶段：

- **prefill（预填充）**：把用户输入的全部 prompt 一次性并行算完，产出第一个 token。这一步计算量大、可以充分并行。
- **decode（解码）**：之后每步只算一个新 token，循环直到模型输出结束符或达到 `max_tokens`。这一步每步计算量小，但对延迟极其敏感。

推理引擎的一项核心工作是管理 **KV cache**：注意力机制计算每个新 token 时都要回看前文，把前文的 Key/Value 中间结果缓存起来，避免重复计算。它直接决定了能同时服务多少请求、能支持多长的上下文。本讲先建立概念，KV 体系在手册单元 7 专门精讲。

### 2.2 什么是 Rust crate 与 cargo workspace

- **crate**：Rust 的编译单元，相当于「一个包/一个库或一个可执行程序」。
- **workspace（工作区）**：一组 crate 放在同一个仓库里，共享一份依赖版本锁定（`Cargo.lock`）和一套编译配置。项目根目录的 `Cargo.toml` 通过 `members` 列表声明所有成员。

PegaInfer 就是一个包含 **20 个成员** 的 workspace（19 个 `pegainfer-*` crate，外加 1 个从 NVIDIA Dynamo 衍生的 fork crate）。

### 2.3 什么是 feature flag

Rust 的 **feature** 是条件编译开关：`cargo build --features kimi-k2` 会把 `kimi-k2` 分支的代码编进二进制；不传则这部分代码**完全不参与编译**。PegaInfer 用它实现「每条模型线按需编译」——这是理解本项目构建方式的钥匙。

### 2.4 什么是 OpenAI 兼容 API

指提供与 OpenAI 官方服务相同格式的 HTTP 接口（如 `/v1/completions`、`/v1/chat/completions`），这样现成的 OpenAI 客户端、SDK 无需改代码就能直接连上你的服务。

## 3. 本讲源码地图

本讲涉及的关键文件如下（后续每讲也会有这样一张「源码地图」）：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/README.md) | 项目门面：定位、快速上手、性能数据、模型矩阵、架构说明、API 示例 |
| [CLAUDE.md](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/CLAUDE.md) | 给开发代理的仓库指南：模型表、构建命令、架构 ASCII 图、EP 纪律、文档工作流 |
| [Cargo.toml](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/Cargo.toml) | workspace 根清单：20 个成员、共享依赖版本、共享 lint 配置 |
| [pegainfer-server/Cargo.toml](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-server/Cargo.toml) | 服务二进制的清单：七条模型线的 feature 定义，`default = ["qwen3"]` |
| [pegainfer-server/src/main.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-server/src/main.rs) | 服务入口：只做「探测模型 → 校验 CLI → 启动引擎」的纯分发 |

## 4. 核心概念与源码讲解

### 4.1 README：项目定位与七条模型线

#### 4.1.1 概念说明

读任何开源项目的第一站都是 README。PegaInfer 的 README 第一段正文就给出了定位：

> PegaInfer serves LLMs through an OpenAI-compatible API. Each model owns its scheduler, state, and kernels; serving and KV infrastructure are shared. **No PyTorch or Python runtime.**

拆开看有三层信息：

1. **它做什么**：LLM 推理服务，OpenAI 兼容 API，监听 8000 端口。
2. **它怎么组织**：每个模型**自带**调度器、状态与内核；服务层和 KV 基础设施是**共享**的。
3. **它不是什么**：不含 PyTorch，运行时没有 Python。「纯 Rust + CUDA」意味着内存安全、快速冷启动（README 的性能小节给出 Qwen3-4B 冷启动到 HTTP ready 约 3 秒，而 vLLM 0.24.0 为 70 秒）、常驻内存更小（771 MB vs 3814 MB，单进程对比）。

为什么「无 Python」值得写进第一句话？主流推理引擎（vLLM、SGLang 等）以 Python 生态为骨架，带来启动慢、依赖树庞大、GIL 限制等代价。PegaInfer 选择把整条服务链路（HTTP、调度、算子、采样）全部用 Rust 实现，只在**构建期**为个别模型线调用 Python 工具生成 CUDA 内核（如 Qwen3.5 的 Triton AOT）——产物是纯静态的 CUDA 代码，运行时依然没有 Python。

#### 4.1.2 核心流程

README 的信息组织顺序本身就是一条「认识项目」的路线：

1. **Quickstart**：预编译二进制（一行 `install.sh`）或源码构建（`cargo run --release -- --model-path models/Qwen3-4B`）。
2. **Supported Models**：七条模型线 × 注意力/专家结构 × feature 名 × 服务范围。
3. **Architecture**：一张所有权分层图 + 一张「边界 → 职责」表。
4. **API**：两条 `curl` 示例（非流式 completions 与流式 chat/completions）。
5. **Development**：环境准备脚本、测试命令、文档索引。

本讲我们重点关注第 2、3 步；第 1、4 步在下一讲（构建与运行）和单元 3（前端契约）展开。

#### 4.1.3 源码精读

**项目一句话定位**，见 [README.md:L31](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/README.md#L31)。这一行浓缩了全项目的架构决策，值得背下来。

**七条模型线总表**，见 [README.md:L142-L156](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/README.md#L142-L156)。表格列出了每条线的注意力/专家结构、feature 名与服务范围，开头一句尤其重要：

> Only **`qwen3`** is enabled by default, including in the prebuilt binary. Build other lines with `--features <feature>`. At launch, `--model-path` selects a checkpoint and its `config.json` identifies the model family.

把这张表提炼成速查表（feature 名决定了你怎么编译）：

| 模型线 | 架构关键词 | feature | 备注 |
| --- | --- | --- | --- |
| Qwen3（0.6B–32B dense） | 全注意力 + GQA | `qwen3` | **默认开启**，预编译二进制只含它 |
| Qwen3.5（0.8B–27B） | Gated DeltaNet 线性注意力 + 全注意力混合 | `qwen35` | 构建期需要 Python + Triton |
| Gemma 4（12B / 26B-A4B） | 滑动窗口 + 全局注意力，26B 用 NVFP4 路由专家 | `gemma4` | 单卡 |
| DeepSeek-V2-Lite | MLA + MoE | `deepseek-v2-lite` | 2 卡 EP2，正确性路径 |
| Kimi-K2 / K2.5 | MLA + MoE + Marlin INT4 | `kimi-k2` | 8 卡专家并行 |
| GLM-5.2 | 稀疏 MLA + MoE + FP8 | `glm52` | Blackwell，bring-up 阶段 |
| Kimi-K3 | KDA + MLA + latent MoE（MXFP4） | `k3` | Blackwell，bring-up 阶段 |

几个缩写的通俗解释：**GQA**（Grouped-Query Attention）是让多个查询头共享同一组 KV 头、从而缩小 KV cache 的注意力变体；**MoE**（Mixture of Experts）是「每层有很多专家网络，路由器只激活其中几个」的结构，用少量激活量换来大参数量；**MLA**（Multi-head Latent Attention）是 DeepSeek 系的注意力变体，把 KV 压缩到低维隐空间再缓存；**EP**（Expert Parallelism）是把不同专家分布到不同 GPU 上的并行方式。

**「config.json 识别模型家族」**的机制在 [README.md:L144](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/README.md#L144)：`--model-path` 指向 HuggingFace 格式的 checkpoint 目录，服务启动时读里面的 `config.json` 判断这是哪条模型线——所以同一个二进制不用改命令行参数就能伺服不同模型，只要对应 feature 编译进来了。

**API 示例**见 [README.md:L186-L196](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/README.md#L186-L196)：`/v1/completions` 与 `/v1/chat/completions` 都支持 `stream: true` 流式返回。

#### 4.1.4 代码实践

**实践目标**：亲手验证「只有 qwen3 默认编译」这句话，并体验 feature 如何改变二进制里有什么模型。

**操作步骤**：

1. 打开仓库根目录，通读 [README.md:L31-L68](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/README.md#L31-L68)（定位 + Quickstart）。
2. 在有 Rust 工具链的机器上运行：

   ```bash
   cargo run --release -- --help
   ```

3. 再运行带 feature 的版本对比：

   ```bash
   cargo run --release --features gemma4 -- --help
   ```

4. 对比两次 `--help` 输出中模型相关选项的差异。

**需要观察的现象**：默认构建的 `--help` 只出现 Qwen3 家族的选项；`--features gemma4` 会重新编译并追加 Gemma 4 家族的选项——因为各模型的 CLI 选项类型定义在各自的模型 crate 里，feature 关了它们就不存在。

**预期结果**：两个二进制的 `--help` 都能正常列出通用参数；模型特定参数随 feature 增减。

**待本地验证**：以上命令需要本机具备 Rust 工具链（CUDA 只在真正启动模型时必需，`--help` 不触发 GPU 初始化；若 `cargo run` 阶段仍尝试编译 CUDA 内核导致失败，可改用 `cargo check` 观察 feature 对代码裁剪的影响，或记录报错留待下一讲解决）。

#### 4.1.5 小练习与答案

**练习 1**：为什么预编译发行版只捆绑 Qwen3，而不是把七条模型线全编进去？

**参考答案**：见 [README.md:L144](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/README.md#L144)——只有 `qwen3` 默认开启。全量编译会让每个二进制都背上所有模型的内核与依赖：其中 Qwen3.5 还需要构建期 Python + Triton，GLM-5.2/K3 需要 Blackwell（sm_90a/sm_100 级）专用内核。按 feature 裁剪让默认构建保持「纯 Rust + CUDA、无 Python、快速冷启动」的形态，其余模型线按需重编。

**练习 2**：`config.json` 在启动流程里扮演什么角色？如果 `--model-path` 指向一个 Gemma 4 的 checkpoint，但二进制没开 `gemma4` feature，会发生什么？

**参考答案**：服务启动时读取 checkpoint 的 `config.json`，用 `model_type` 等字段识别模型家族，再交给对应模型线认领。没开 feature 时该模型线被编译出去、无人认领，服务不会静默失败——入口的 `feature_gate_hint` 会识别出这是已知家族，明确提示用户「用 `--features gemma4` 重新构建」（实现见 [pegainfer-server/src/main.rs:L52-L80](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-server/src/main.rs#L52-L80)，细节在下一单元 u2-l1 精读）。

**练习 3**：README 说「No PyTorch or Python runtime」，但 Qwen3.5 构建又要装 Triton。矛盾吗？

**参考答案**：不矛盾。Triton 只在**构建期**做 AOT（提前编译）：`gen_triton_aot.py` 把 Triton 内核生成为静态 CUDA 产物编进 `pegainfer-kernels`，运行时的进程里没有 Python 解释器。这正是「构建期可用 Python 工具、运行时纯 Rust + CUDA」的分工（构建管线在手册 u4-l2 精读）。

### 4.2 Cargo.toml workspace：crate 组织与 feature 开关

#### 4.2.1 概念说明

根 [Cargo.toml](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/Cargo.toml) 是整个项目的「户口本」：它声明所有成员 crate、统一钉住第三方依赖版本、统一 lint 规则。读懂它，你就能回答「这个功能在哪个 crate」这一类问题——这是在大型仓库里定位代码的第一技能。

#### 4.2.2 核心流程

workspace 根清单做三件事：

1. **圈定成员**：`members` 列出 20 个 crate；`default-members` 限定不带 `-p` 参数时 cargo 默认操作谁。
2. **钉住依赖**：`[workspace.dependencies]` 统一声明版本，成员 crate 用 `{ workspace = true }` 继承，保证全仓库版本一致。
3. **统一规范**：`[workspace.lints]` 让所有成员共享同一套 clippy/rustc lint 阈值。

#### 4.2.3 源码精读

**成员清单与默认成员**，见 [Cargo.toml:L1-L25](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/Cargo.toml#L1-L25)：

```toml
[workspace]
default-members = ["pegainfer-server"]
members = [
  "pegainfer-frontend",
  "pegainfer-sim",
  "pegainfer-server",
  # ……共 20 项，完整清单见源文件
  "kvbm/kvbm-logical",
]
```

两个值得注意的细节：

- `default-members = ["pegainfer-server"]`（[Cargo.toml:L2](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/Cargo.toml#L2)）：在仓库根直接 `cargo run --release -- --model-path ...` 时，编译并运行的就是服务二进制（它的 bin 名叫 `pegainfer`）。这就是 README 里那条启动命令无需 `-p` 的原因。
- 最后一个成员 `kvbm/kvbm-logical` 带注释「dynamo kvbm (forked from upstream, Apache-2.0)」（[Cargo.toml:L23-L24](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/Cargo.toml#L23-L24)）：它是从 NVIDIA Dynamo 项目 fork 来的逻辑 KV 块管理层，保留原版权头（README 的 License 节也有说明）。这是本仓库唯一的「外来」crate。

**与 vLLM 生态的关系**，见 [Cargo.toml:L182-L186](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/Cargo.toml#L182-L186)：`vllm-server`、`vllm-tokenizer`、`vllm-chat`、`vllm-text`、`vllm-engine-core-client` 五个 git 依赖。PegaInfer 不是从零造 HTTP/分词轮子，而是**复用 vLLM 官方的 Rust frontend crates** 承载 OpenAI 协议与分词，自己专注引擎本体。「用 vLLM 的 Rust 前端、不用它的 Python 引擎」是这个项目的独特站位。另外 [Cargo.toml:L39-L46](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/Cargo.toml#L39-L46) 还钉住了两个 `ai-dynamo/dynamo` 的依赖（`dynamo-kv-hashing`、`dynamo-tokens`），服务于 kvbm 这条线。

**feature 开关的真正定义处**在服务 crate 的清单里，见 [pegainfer-server/Cargo.toml:L33-L46](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-server/Cargo.toml#L33-L46)：

```toml
[features]
# Qwen3-4B is the default model line: pure Rust + CUDA, no Python at build time.
default = ["qwen3"]
qwen3 = ["dep:pegainfer-qwen3"]
gemma4 = ["dep:pegainfer-gemma4", "pegainfer-gemma4/gemma4"]
qwen35 = ["dep:pegainfer-qwen35", "pegainfer-qwen35/qwen35"]
# ……其余模型线同理
```

注意双层结构：feature 先把对应模型 crate 作为**可选依赖**（`optional = true`，见 [pegainfer-server/Cargo.toml:L18-L25](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-server/Cargo.toml#L18-L25)）拉进来（`dep:pegainfer-qwen3`），再向内透传同名 feature 打开该 crate 里更重的内核编译（如 `pegainfer-gemma4/gemma4`）。

**feature 如何变成代码里的分支**，见 [pegainfer-server/src/main.rs:L27-L44](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-server/src/main.rs#L27-L44) 的 `model_lines()`：

```rust
fn model_lines() -> Vec<&'static dyn ModelLine> {
    vec![
        #[cfg(feature = "deepseek-v2-lite")]
        &pegainfer_deepseek_v2_lite::model_line::MODEL_LINE,
        #[cfg(feature = "qwen3")]
        &pegainfer_qwen3::model_line::MODEL_LINE,
        // ……每个模型线一行 cfg 门控
    ]
}
```

每个模型 crate 导出一个静态的 `MODEL_LINE`（实现 `ModelLine` trait），入口按 `#[cfg(feature = ...)]` 决定注册哪些。文件开头的文档注释（[pegainfer-server/src/main.rs:L1-L5](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-server/src/main.rs#L1-L5)）把 server 的哲学说得很直白：**「pure dispatch over the compiled-in ModelLines」**——所有模型专属的参数、规则、选项类型都住在模型 crate 里，入口只做接线。

#### 4.2.4 代码实践

**实践目标**：给 20 个 workspace 成员建立「一句话职责」的第一印象，为综合实践的四层架构图收集素材。

**操作步骤**：

1. 通读 [Cargo.toml:L1-L25](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/Cargo.toml#L1-L25) 的 `members` 列表。
2. 对每个成员，进入其目录打开 `Cargo.toml` 的 `description` 字段或 `src/lib.rs` / `src/main.rs` 顶部的文档注释（Rust 惯例：文件最开头的 `//!` 注释就是该 crate 的自我介绍）。
3. 把 20 个成员记入一张表：crate 名 | 一句话职责 | 你猜测的架构层。

**需要观察的现象**：模型 crate（如 `pegainfer-qwen3`）的文件结构里会出现 `scheduler`、`executor`、`prefill`、`decode` 等推理专有模块；共享 crate（如 `pegainfer-core`）则出现 `ops`、`weight_loader`、`cuda_graph` 等基础设施模块——职责差异一眼可辨。

**预期结果**：得到一张 20 行的职责表（本讲综合实践给出参考版分层答案）。

**待本地验证**：目录浏览不需要任何工具链，可以直接做。

#### 4.2.5 小练习与答案

**练习 1**：在仓库根运行 `cargo build --release`（不指定包），会编译哪些 crate？为什么？

**参考答案**：只编译 `pegainfer-server` 及其依赖闭包，因为 [Cargo.toml:L2](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/Cargo.toml#L2) 设了 `default-members = ["pegainfer-server"]`。默认 feature 又只有 `qwen3`，所以其余六条模型线的 crate 完全不参与编译。

**练习 2**：`qwen35 = ["dep:pegainfer-qwen35", "pegainfer-qwen35/qwen35"]` 这一行里的两段各起什么作用？

**参考答案**：`dep:pegainfer-qwen35` 让 server 这个 crate 依赖并链接模型 crate 本体；`pegainfer-qwen35/qwen35` 是**向依赖透传 feature**，打开 `pegainfer-qwen35` crate 内部同名的 feature——后者会一路传导到 `pegainfer-kernels/build.rs` 触发 Triton AOT 内核生成（构建管线在 u4-l2 详述）。

**练习 3**：项目为什么复用 vLLM 的 Rust crates，而不是自己实现 OpenAI 协议层？

**参考答案**：[README.md:L176](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/README.md#L176) 写明 frontend 职责包括「OpenAI protocol, tokenization, chat templates, streaming, metrics, and engine contracts; **uses vLLM's Rust frontend crates**」。协议、分词、聊天模板这类成熟且规格固定的部分直接复用官方 Rust 实现，把工程精力集中在调度、内核与 KV 这些性能关键路径上——这是「不重复造轮子」与「引擎自主可控」之间的清晰切分。

### 4.3 架构分层：共享基础设施、模型自持执行

#### 4.3.1 概念说明

README 的 Architecture 一节开宗明义（[README.md:L158-L160](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/README.md#L158-L160)）：

> **Share the infrastructure; let each model own its execution.** The frontend submits requests through an engine contract. Model schedulers decide how to batch work, manage state, and execute kernels on their target hardware.

这 18 个字是全仓库最重要的设计决策，值得逐词理解：

- **Share the infrastructure**：张量、算子封装、权重加载、采样、KV 分页池这些「每条模型线都需要、且彼此高度相似」的能力下沉到共享 crate，写一次、七条线复用。
- **let each model own its execution**：但**怎么调度请求、怎么组 batch、状态怎么摆、内核按什么顺序发射**这些差异巨大的部分，不做统一抽象强求一致，而是让每个模型 crate 全权自持。

这与很多框架「一个大执行器适配所有模型」的思路相反。代价是模型线之间有重复代码，收益是每条线可以为其架构（全注意力 / 线性注意力 / MoE / MLA）与硬件（单卡 / TP / EP）做毫不妥协的深度优化。理解了这个取舍，后面读任何模型 crate 都不会困惑「为什么这段逻辑不在 core 里」。

支撑这句话的是 README 的**边界职责表**（[README.md:L173-L181](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/README.md#L173-L181)），它给出了六条边界：server（探测/校验/启动）、frontend（协议+契约）、per-model crates（权重/调度/执行/并行）、core+sample（共享 GPU 原语与批量采样）、kernels（原生内核与 FFI）、KV infrastructure（kv-store 与 kv-cache/kv-offload，PegaFlow 提供 host/SSD/RDMA 深层）。

#### 4.3.2 核心流程

把边界表映射到 workspace 成员，就得到本讲要求的**四层架构图**。一次请求的自上而下穿越路径是：

```text
OpenAI 客户端 (curl / SDK)
        │  HTTP: POST /v1/chat/completions
        ▼
┌─ 第 1 层：服务入口与前端契约 ─────────────────────────────┐
│  pegainfer-server   纯分发：探测 config.json → 校验 CLI   │
│                     → 启动选中模型线（main.rs 约百行）     │
│  pegainfer-frontend 引擎契约(EngineHandle/step) + vLLM     │
│                     协议栈 + 分词/模板/流式/指标           │
│  pegainfer-sim      无 GPU 的模拟引擎（开发/压测前端用）    │
└────────────────────────┬─────────────────────────────────┘
                         │  引擎契约: GenerateRequest / Step
                         ▼
┌─ 第 2 层：模型 crate（每线一 crate，自持执行）─────────────┐
│  pegainfer-qwen3      全注意力 + GQA + TP（默认线）        │
│  pegainfer-qwen35     Gated DeltaNet + 全注意力混合        │
│  pegainfer-gemma4     滑动窗 + 全局双族 KV，单卡           │
│  pegainfer-deepseek-v2-lite  MLA + MoE + EP2              │
│  pegainfer-kimi-k2    MLA + MoE + Marlin INT4, 8 卡 EP    │
│  pegainfer-glm52      稀疏 MLA + FP8 + MTP（bring-up）    │
│  pegainfer-k3         KDA + latent MoE（bring-up）        │
└────────────────────────┬─────────────────────────────────┘
                         │  调用共享算子/加载/图捕获
                         ▼
┌─ 第 3 层：共享运行时 ────────────────────────────────────┐
│  pegainfer-core    GPU 算子门面(ops)、权重加载、CUDA Graph │
│  pegainfer-kernels CUDA/cuBLAS/FlashInfer FFI、内核构建    │
│  pegainfer-sample  批量采样（贪心/温度/top-k/top-p）       │
│  pegainfer-build   构建期工具链发现（nvcc/SM 探测）        │
│  pegainfer-bench   内核基准框架   pegainfer-cupti 剖析绑定 │
└────────────────────────┬─────────────────────────────────┘
                         │  分页 KV 池 / 换页
                         ▼
┌─ 第 4 层：KV 基础设施 ───────────────────────────────────┐
│  pegainfer-kv-cache    GPU 分页 KV（BlockPool + KvBuffer）│
│  pegainfer-kv-offload  GPU↔主机↔SSD/RDMA 换页             │
│  pegainfer-kv-store    统一 KV 读写编排（演进方向）        │
│  kvbm/kvbm-logical     Dynamo 衍生的逻辑块层（类型状态块）  │
└──────────────────────────────────────────────────────────┘
                         │
                         ▼
        CUDA / cuBLAS / FlashInfer / Triton AOT / NCCL（GPU 栈）
```

三条阅读线索：

1. **依赖只允许自上而下**：第 2 层调用第 3、4 层；第 1 层的 server 只通过「引擎契约」类型与第 2 层对话，不 import 任何模型内部类型。
2. **第 2 层内部互不依赖**：七条模型线彼此是兄弟，不互相引用——换掉任何一条线不影响其余六条。
3. **两套引擎契约并存**：[README.md:L182](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/README.md#L182) 提到 step-based 契约与旧的 `EngineHandle` 契约目前共存，迁移边界记录在 `docs/subsystems/frontend/frontend-architecture.md`。本讲只需知道「有两代契约」，单元 3 会分别精读。

#### 4.3.3 源码精读

**架构原则原句与所有权分层图**，见 [README.md:L158-L171](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/README.md#L158-L171)。README 用 SVG 图展示所有权分层（图中还标了 PegaFlow 供应 host/SSD/RDMA 存储），可编辑源文件在 `docs/assets/architecture.drawio`。

**边界职责表**，见 [README.md:L173-L181](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/README.md#L173-L181)：六行分别对应 server、frontend、per-model crates、core/sample、kernels、KV infrastructure 的职责边界——上面的四层图就是把这张表展开到具体 crate 名。

**ASCII 版架构图**见 [CLAUDE.md](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/CLAUDE.md) 的 Architecture 一节：它画出了「HTTP Request → vLLM frontend → EngineHandle → per-model scheduler/executor → TokenEvent」的主干，以及底部 `pegainfer-core runtime + pegainfer-kernels` 与 CUDA/cuBLAS、Triton AOT、FlashInfer 的支撑关系。CLAUDE.md 与 README 的模型表内容一致，可交叉印证。

**server 只做分发的代码证据**，见 [pegainfer-server/src/main.rs:L1-L5](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-server/src/main.rs#L1-L5) 的模块文档注释：

```rust
//! The pegainfer server binary: pure dispatch over the compiled-in
//! [`ModelLine`]s. Every model-specific flag, rule, and option type lives in
//! its model crate; this file only wires detection, CLI validation, and the
//! serve path selection together.
```

「Every model-specific flag, rule, and option type lives in its model crate」正是第 1 层与第 2 层之间边界的形式化表述。

#### 4.3.4 代码实践

**实践目标**：验证「server 不含模型逻辑」这一分层断言。

**操作步骤**：

1. 打开 [pegainfer-server/src/main.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-server/src/main.rs)，统计文件总行数（可本地 `wc -l`）。
2. 在该文件中搜索（`grep -n "attention\|prefill\|decode\|kv_" pegainfer-server/src/main.rs`）任何注意力/解码/KV 相关实现代码。
3. 对照 [pegainfer-server/Cargo.toml:L13-L28](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-server/Cargo.toml#L13-L28) 的依赖表：server 只依赖 frontend、core 与**可选的**模型 crate，没有 kernels/sample/kv-*。

**需要观察的现象**：main.rs 行数很少（相对模型 crate 数千行的 executor 而言），全文搜不到任何算子或 KV 实现细节；它引用的每个模型符号都只是 `model_line::MODEL_LINE` 这个注册点。

**预期结果**：验证「入口纯分发、模型自持执行」在依赖与代码两个层面都成立。

**待本地验证**：grep/wc 命令的输出请以本地实际结果为准。

#### 4.3.5 小练习与答案

**练习 1**：如果要让 Qwen3 支持 MoE，改动应该落在四层中的哪一层？为什么？

**参考答案**：第 2 层（`pegainfer-qwen3` crate）。MoE 改变的是模型结构——路由、专家权重布局、执行与调度策略，按「模型自持执行」原则全部属于模型 crate。只有当某段能力（比如专家分发的 all-to-all 通信）被多条线共用时，才考虑下沉到第 3 层的共享运行时。

**练习 2**：`pegainfer-sim`（模拟引擎）放在第 1 层（前端契约层）而不是第 2 层，透露了什么设计信息？

**参考答案**：说明「引擎契约」是第 1、2 层之间唯一的接口：只要实现了契约，一个不含任何 GPU/权重的假引擎（`pegainfer-sim`）就能替换真模型跑通整个前端——协议、分词、流式全链路可以脱离 GPU 开发与压测。契约本身（`GenerateRequest`/`TokenEvent`/step 协议）定义在 `pegainfer-frontend` 里（u1-l4、u3 系列精读）。

**练习 3**：`kvbm/kvbm-logical` 是 fork 来的代码，为什么让它作为 workspace 成员而不是发布为普通 crates.io 依赖？

**参考答案**：fork 意味着需要随本仓库需求同步修改（类型状态块、radix 树注册表等将深度嵌入 kv-store 的统一编排，见 u7 系列讲义）。作为 path 成员可直接改动、参与统一 lint 与依赖锁定；同时 [Cargo.toml:L40-L46](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/Cargo.toml#L40-L46) 的注释强调所有 dynamo 依赖钉在同一 rev 以保证类型一致——本地 fork 与固定 rev 的上游依赖配合，兼顾可改性与兼容性。

## 5. 综合实践

**任务：亲手产出「四层架构图」**（这是本讲规格指定的实践任务）。

1. **实践目标**：把 README 的架构原则、边界职责表与 `Cargo.toml` 的 members 列表三者对齐，画出「前端契约 / 模型 crate / 共享运行时 / KV 基础设施」四层架构图，并在每层标注它包含哪些 crate。
2. **操作步骤**：
   1. 通读 [README.md:L158-L182](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/README.md#L158-L182)（Architecture 全节）与 [Cargo.toml:L1-L25](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/Cargo.toml#L1-L25)（members 列表）。
   2. 对 20 个成员逐一判定所属层。判不准的（如 `pegainfer-bench`、`pegainfer-cupti`、`pegainfer-build`、`pegainfer-sim` 这几个「工具型」crate），读其 `Cargo.toml` 的 description 或 `src` 顶部 `//!` 注释再定。
   3. 用你顺手的工具（mermaid、drawio、纸笔照片均可）画出分层图，层间用箭头标出依赖方向，并在图旁写一句每层「为什么存在」。
3. **需要观察的现象**：归类过程中你会遇到边界案例——例如 `pegainfer-sample`（采样既是共享原语又是独立 crate）、`pegainfer-kv-store`（README 称其为演进方向）。把这些疑点记下来，它们正是后续单元（u4-l6、u7-l5）要回答的问题。
4. **预期结果**：一张覆盖全部 20 个成员的四层图。可对照 4.3.2 节的参考图自评：
   - 前端契约层：`pegainfer-server`、`pegainfer-frontend`、`pegainfer-sim`
   - 模型 crate 层：`pegainfer-qwen3`、`pegainfer-qwen35`、`pegainfer-gemma4`、`pegainfer-deepseek-v2-lite`、`pegainfer-kimi-k2`、`pegainfer-glm52`、`pegainfer-k3`
   - 共享运行时层：`pegainfer-core`、`pegainfer-kernels`、`pegainfer-sample`、`pegainfer-build`、`pegainfer-bench`、`pegainfer-cupti`
   - KV 基础设施层：`pegainfer-kv-cache`、`pegainfer-kv-offload`、`pegainfer-kv-store`、`kvbm/kvbm-logical`

   注意工具型 crate（bench/cupti/build）不直接出现在请求路径上，把它们放共享运行时层的「侧翼」并注明「离线/构建期工具」是更好的画法。
5. **交付物**：一张图 + 一张 20 行的 crate 职责表。这张图建议保留，整个手册后面每讲都会在这张图上「点亮」新的区块。

## 6. 本讲小结

- **PegaInfer 是什么**：纯 Rust + CUDA、无 PyTorch/Python 运行时的 LLM 推理引擎，对外提供 OpenAI 兼容 API（8000 端口，`/v1/completions` 与 `/v1/chat/completions` 均支持流式）。
- **七条模型线、七个 feature**：`qwen3`（默认）、`qwen35`、`gemma4`、`deepseek-v2-lite`、`kimi-k2`、`glm52`、`k3`；启动时由 `--model-path` 指向的 checkpoint 的 `config.json` 识别家族，未编译的线会被 `feature_gate_hint` 明确提示重建。
- **一句话架构**：Share the infrastructure; let each model own its execution——张量/算子/加载/采样/KV 共享，调度/执行/状态每模型自持。
- **workspace 全景**：20 个成员分四层——前端契约（server/frontend/sim）、模型 crate（7 个）、共享运行时（core/kernels/sample/build/bench/cupti）、KV 基础设施（kv-cache/kv-offload/kv-store/kvbm-logical）。
- **站位独特**：复用 vLLM 官方的 Rust frontend crates 承载协议与分词，但引擎本体完全自研；`pegainfer-sim` 证明引擎契约是前端与模型之间唯一的接缝。
- **server 是纯分发器**：`pegainfer-server/src/main.rs` 只做探测、校验、启动三件事，所有模型专属选项类型都住在模型 crate 里。

## 7. 下一步学习建议

下一讲（**u1-l2 构建与运行：从源码到 GPU 服务**）将把本讲的静态地图变成动态体验：安装 Rust 工具链与 CUDA Toolkit，用 `cargo run --release` 启动 Qwen3-4B，理解 `--release` 为什么是硬要求，以及 `CUDA_HOME`、`PEGAINFER_CUDA_SM`、`PEGAINFER_TRITON_PYTHON` 等环境变量的作用。

在进入下一讲之前，建议先自行浏览：

- [README.md](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/README.md) 的 Quickstart 与 Development 两节——下一讲的实践直接依赖这里的命令。
- [docs/index.md](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/docs/index.md)——项目工程文档的路由表，看看官方如何按 models/subsystems/playbooks 组织知识（与我们的四层图互为镜像）。
- 若你对「token 是怎么一个个生成的」还没有直觉，可先读 README 的 API 一节，下一讲亲手发出第一个请求。
