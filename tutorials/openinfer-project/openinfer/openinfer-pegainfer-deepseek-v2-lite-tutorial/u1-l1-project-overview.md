# 项目定位与整体架构：PegaInfer 是什么，dsv2-lite 在哪里

> 本讲是《openinfer-pegainfer-deepseek-v2-lite 学习手册》第一篇（u1-l1，入门层）。手册聚焦 PegaInfer（openinfer 工作区）中的 `pegainfer-deepseek-v2-lite` crate——DeepSeek-V2-Lite 模型线：MoE + 专家并行（EP），2-GPU 部署。

## 1. 本讲目标

学完本讲，你应该能够：

1. 用自己的话说清 PegaInfer 的定位：**纯 Rust + CUDA 的 LLM 推理引擎**，不用 PyTorch、不依赖任何推理框架，对外提供 OpenAI 兼容的 `/v1/completions` 接口；默认构建里没有任何 Python。
2. 一笔画出主链路：`HTTP Request → vLLM frontend → EngineHandle → per-model scheduler/executor → TokenEvent`。
3. 说出 dsv2-lite 在模型矩阵中的架构标签（**MoE + EP、2-GPU**）、它的特性开关 `deepseek-v2-lite`，以及这个开关如何从 server 一路传递到 kernels。
4. 在仓库里准确定位每一层对应的 crate 与关键文件，为后续讲义建立一张「源码地图」。

## 2. 前置知识

本讲假设你没有读过这个仓库，只需要以下概念的通俗版本（不需要任何数学推导）：

- **LLM 推理（inference）**：把训练好的模型权重加载进 GPU 显存，接收一段文本（prompt），一个 token 一个 token 地生成回答。推理分两个阶段：**prefill**（一次性「读入」整个 prompt）和 **decode**（逐个生成新 token）。
- **token**：模型处理文本的最小单位，大致相当于一个词或半个词。
- **KV cache**：注意力计算中把已算过的 Key/Value 缓存下来，避免每生成一个 token 都重算全部历史；它是显存的主要消耗者之一。
- **MoE（Mixture of Experts，混合专家）**：模型的前馈层不是一个大网络，而是很多个「专家」小网络加一个 router（路由器）；每个 token 只被路由到少数几个专家上计算。直观写成 \( y(x)=\sum_{i\in \text{top-}k} g_i(x)\cdot E_i(x) \)，其中 \( E_i \) 是第 \( i \) 个专家，\( g_i(x) \) 是 router 分配的权重。
- **EP（Expert Parallelism，专家并行）**：把不同专家放到不同 GPU 上，token 按需跨卡送往专家所在的卡，算完再送回。dsv2-lite 用 **2 张 GPU**，仓库文档里叫 **EP2**。
- **TP（Tensor Parallelism，张量并行）**：把单个大矩阵切到多卡一起算（Qwen3 模型线用到，本讲只作对比）。
- **CUDA / cuBLAS / FlashInfer / Triton**：CUDA 是 NVIDIA GPU 的通用计算平台；cuBLAS 是 NVIDIA 官方矩阵运算库；FlashInfer 是开源的注意力/采样等 GPU 算子库；Triton 是一种编写 GPU kernel 的语言，可提前编译（AOT）。
- **Cargo workspace 与 feature**：Rust 的「一个仓库多个包」机制叫 workspace；feature 是**编译期开关**，`--features xxx` 表示这次编译把 xxx 对应的代码编进来，不开启的代码根本不进二进制。
- **OpenAI 兼容 API**：请求/响应格式与 OpenAI 官方接口一致，所以任何现成的 OpenAI 客户端（乃至 `curl`）都能直接调用本引擎。

## 3. 本讲源码地图

本讲涉及的关键文件如下。**所有永久链接均钉在当前 HEAD `8886aa47` 上，行号已在写作时用只读 git 命令逐一核验**；仓库继续演进后行号会漂移，届时以链接打开的内容为准。本讲没有运行过任何 cargo/构建命令，凡涉及运行结果的步骤都标注了「待本地验证」。

| 文件 | 作用 |
|---|---|
| [CLAUDE.md](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/CLAUDE.md)（仓库根） | 项目总说明书：模型矩阵、构建命令、环境变量、整体架构图——本讲的主教材 |
| [docs/index.md](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/docs/index.md) | 文档路由表：按领域（模型线/子系统/手册/经验）索引全部文档 |
| [Cargo.toml](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-deepseek-v2-lite/Cargo.toml) | dsv2-lite 的 crate 清单：特性开关定义、依赖清单、测试与二进制入口 |
| [src/lib.rs](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-deepseek-v2-lite/src/lib.rs) | crate 入口：模块声明（绝大多数模块带 cfg(feature) 编译门控）、config 探测、launch 启动 |
| [src/model_line.rs](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-deepseek-v2-lite/src/model_line.rs) | ModelLine 派发 trait 的 dsv2-lite 实现——模型 crate 与前端契约的接缝 |
| [src/ep.rs](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-deepseek-v2-lite/src/ep.rs) | 专家并行支撑：EP2 常量、专家布局与校验（64 个路由专家 → 每卡 32 个） |
| [pegainfer-server/Cargo.toml](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-server/Cargo.toml)、仓库根 [Cargo.toml](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/Cargo.toml)、[pegainfer-kernels/Cargo.toml](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/Cargo.toml) | 特性开关的传递链：server → 本 crate → kernels |

## 4. 核心概念与源码讲解

### 4.1 项目定位与设计取舍

#### 4.1.1 概念说明

PegaInfer 是一个**自研的 LLM 推理引擎**：自己管权重加载、自己管调度、自己封装 GPU 算子、自己实现 OpenAI 兼容协议。「纯 Rust + CUDA、无 PyTorch、无框架」这个定位带来三个直接推论：

1. **部署面小**：默认构建产物里没有 Python 运行时，也没有 PyTorch 那套庞杂依赖——这对推理服务的运维是实打实的减负。
2. **性能与行为可控**：从 kernel 编译到批调度都在本仓库的 Rust/CUDA 代码里，读得到、改得动、测得了，不需要猜框架黑盒里发生了什么。
3. **代价是所有脏活得自己干**：GPU 算子要自己封装（`pegainfer-core::ops`）、CUDA/Triton 编译要自己组织（`pegainfer-kernels` 的 `build.rs`）、跨卡通信要自己接（NCCL / DeepEP shim）。

「OpenAI 兼容 `/v1/completions`」则决定了对外接口形态：使用者不需要新客户端，拿现成 OpenAI SDK 就能连。

#### 4.1.2 核心流程

一次推理请求的鸟瞰（后续讲义会逐段展开）：

1. 客户端向 `/v1/completions` 发一个 OpenAI 格式的 HTTP 请求；
2. `pegainfer-frontend` 的 vLLM 协议栈解析请求，翻译成引擎内部的 `GenerateRequest`；
3. 请求经 `EngineHandle`（引擎契约）交给**对应模型 crate 的调度器/执行器**（本手册的主角就是 dsv2-lite 的那一套）；
4. 执行器跑 prefill/decode，与 KV cache、MoE 路由、跨卡 EP 通信协作；
5. 生成的 token 以 `TokenEvent` 事件回流前端，再被序列化成 OpenAI 格式的流式响应。

#### 4.1.3 源码精读

仓库根 CLAUDE.md 开头两句是全仓库最浓缩的定位声明：

- [CLAUDE.md:L5](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/CLAUDE.md#L5)：这句原文是「Pure Rust + CUDA LLM inference engine. No PyTorch, no frameworks. OpenAI-compatible /v1/completions API.」——一句话讲完「用什么实现、不用什么、对外长什么样」。
- [CLAUDE.md:L9](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/CLAUDE.md#L9)：这句解释了「默认构建无 Python」的机制来源——Every model line is behind a cargo feature; only qwen3 is a default feature。不是删掉了 Python，而是**唯一需要 Python 的模型线（qwen35，构建期 Python + Triton）默认不编译**。

#### 4.1.4 代码实践

1. **实践目标**：把「定位」从口号翻译成仓库证据——训练「读文档时随手落到文件」的习惯。
2. **操作步骤**：打开 [CLAUDE.md:L3-L19](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/CLAUDE.md#L3-L19)（What is PegaInfer）与 [CLAUDE.md:L100-L111](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/CLAUDE.md#L100-L111)（Key abstractions 与 Build system），为下表的每一行找到证据（一个文件路径或文档行号），自己填表。
3. **需要观察的现象**：文档里的抽象名词（算子、kernel、契约）是否都能在仓库里指到一个具体 crate。
4. **预期结果**：得到类似下面这样的三行表（参考答案）：

| 定位推论 | 仓库证据 |
|---|---|
| GPU 算子自建 | pegainfer-core::ops 共享算子层（[CLAUDE.md:L104](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/CLAUDE.md#L104)）；pegainfer-kernels 算子与构建所有者（[CLAUDE.md:L105](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/CLAUDE.md#L105)） |
| CUDA/Triton 编译自管 | pegainfer-kernels/build.rs 负责 nvcc 编译与特性门控（[CLAUDE.md:L109-L111](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/CLAUDE.md#L109-L111)） |
| 对外协议自实现 | pegainfer-frontend 的 vLLM 协议栈与 EngineHandle / TokenEvent 契约（[CLAUDE.md:L102](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/CLAUDE.md#L102)） |

本实践为纯阅读型，不运行任何命令，无「待本地验证」项。

#### 4.1.5 小练习与答案

1. **问**：为什么说「stock build 无 Python」？Python 是被删掉了吗？
   **答**：不是删除，而是默认不编译：只有 qwen3 是默认 feature；需要构建期 Python + Triton 的是 qwen35 模型线，不开它就不引入 Python（CLAUDE.md 第 9 行与矩阵表第 14 行）。
2. **问**：「OpenAI 兼容」对使用者的直接好处是什么？
   **答**：任何现成 OpenAI 客户端或 curl 都能直连 /v1/completions，不需要为新引擎写适配代码。
3. **问**：不用 PyTorch 的代价在这个仓库里体现为什么？
   **答**：GPU 算子、kernel 编译、跨卡通信都要自己实现或封装——对应 pegainfer-kernels、pegainfer-core::ops 等基础设施（CLAUDE.md 第 104-105、109-111 行）。

### 4.2 模型 crate 矩阵与特性开关

#### 4.2.1 概念说明

PegaInfer 用「**每条模型线一个 crate + 一个同名 cargo feature**」的方式组织模型支持：想服务哪个模型，就用 --features 把对应模型线编进来。仓库当前支持 7 条模型线（摘自 [CLAUDE.md:L11-L19](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/CLAUDE.md#L11-L19) 的矩阵表，Architecture 列有删节）：

| 模型 | Crate | Feature | 架构标签 |
|---|---|---|---|
| Qwen3-4B / 8B | `pegainfer-qwen3` | `qwen3`（默认） | 全注意力，TP |
| Qwen3.5 / Qwen3.8 | `pegainfer-qwen35` | `--features qwen35`（需构建期 Python + Triton） | 混合 Gated DeltaNet + 全注意力 |
| **DeepSeek-V2-Lite** | **`pegainfer-deepseek-v2-lite`** | **`--features deepseek-v2-lite`** | **MoE + EP，2-GPU** |
| Gemma 4 | `pegainfer-gemma4` | `--features gemma4` | 滑窗 + 全局注意力 |
| Kimi-K2 | `pegainfer-kimi-k2` | `--features kimi-k2` | MLA + MoE + Marlin INT4，8-GPU EP |
| GLM5.2 | `pegainfer-glm52` | `--features glm52` | MLA + MoE + FP8，8-GPU EP |
| Kimi-K3 | `pegainfer-k3` | `--features k3` | 混合 KDA + MLA，MXFP4 MoE（bring-up） |

注意 dsv2-lite 在矩阵里的独特性：它是**规模最小的 MoE + EP 样本**——只要 2 张 GPU（对比 kimi-k2 / glm52 的 8 卡 EP），因此也是学习「专家并行」概念时最平易的入口。

#### 4.2.2 核心流程

在仓库根执行 cargo run --release --features deepseek-v2-lite -- --model-path models/DeepSeek-V2-Lite（[CLAUDE.md:L34](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/CLAUDE.md#L34)）时，这个 feature 名会发生一次「接力」：

```text
cargo run --features deepseek-v2-lite          （在仓库根执行）
  └─▶ pegainfer-server 的同名 feature
        · 把 optional 依赖 pegainfer-deepseek-v2-lite 变为实际依赖
        · 转发同名 feature：pegainfer-deepseek-v2-lite/deepseek-v2-lite
        └─▶ 本 crate 的 deepseek-v2-lite feature
              · 拉起 optional 依赖 pegainfer-kernels（GPU kernel 层）
              · 转发：pegainfer-kernels/deepseek-v2-lite
              └─▶ pegainfer-kernels 的同名标记 feature（kernels 侧条件编译）
```

接力之外还有一层**crate 内部门控**：src/lib.rs 里绝大多数模块声明行都包着 cfg(feature = "deepseek-v2-lite") 属性，不开特性时这些模块根本不参与编译；始终编译的只有 config、ep、model_line 三个模块（见 4.4 节——它们是「识别模型 + 校验形状」的最小集合）。不开特性时 crate 仍能独立编译（本 crate default 为空），只是运行时入口会直接报错占位：start_engine 与 launch 各有一个「未开特性」版本，直接返回错误信息「DeepSeek-V2-Lite runtime is feature-gated; rebuild with --features deepseek-v2-lite」。

#### 4.2.3 源码精读

- [pegainfer-server/Cargo.toml:L18](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-server/Cargo.toml#L18)：pegainfer-deepseek-v2-lite = { workspace = true, optional = true }——server 把本 crate 声明为**可选依赖**（不开特性就不链接）。
- [pegainfer-server/Cargo.toml:L39-L41](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-server/Cargo.toml#L39-L41)：server 侧 feature 定义 deepseek-v2-lite = 以 dep:pegainfer-deepseek-v2-lite 开启依赖、以 pegainfer-deepseek-v2-lite/deepseek-v2-lite 转发同名 feature——接力第一棒。
- [Cargo.toml:L10-L15](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-deepseek-v2-lite/Cargo.toml#L10-L15)（本 crate）——接力第二棒，并声明默认不开启任何特性：

```toml
[features]
deepseek-v2-lite = [
  "dep:pegainfer-kernels",
  "pegainfer-kernels/deepseek-v2-lite",
]
default = []
```

- [pegainfer-kernels/Cargo.toml:L36](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/Cargo.toml#L36)：deepseek-v2-lite = []——kernels 侧的同名**标记 feature**（无附加依赖，供 kernels 内部条件编译判断）。
- [src/lib.rs:L1-L23](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-deepseek-v2-lite/src/lib.rs#L1-L23)：模块声明区——engine、scheduler、weights、runtime 等模块全部带特性门控，而 config、ep、model_line 无门控，这就是「识别/校验最小集始终编译」的代码体现。
- [src/lib.rs:L84-L89](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-deepseek-v2-lite/src/lib.rs#L84-L89) 与 [src/lib.rs:L114-L119](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-deepseek-v2-lite/src/lib.rs#L114-L119)：start_engine 与 launch 的「未开特性」占位版本——函数仍然存在，但直接返回 rebuild with --features deepseek-v2-lite 的报错。这让不开特性的构建也能编译出一个「会解释原因」的入口，而不是链接失败。
- 仓库根 [Cargo.toml:L12](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/Cargo.toml#L12) 把 pegainfer-deepseek-v2-lite 列入工作区成员，[Cargo.toml:L125](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/Cargo.toml#L125) 以 { path = "pegainfer-deepseek-v2-lite" } 形式给出工作区依赖定义。

#### 4.2.4 代码实践

1. **实践目标**：亲手把 4.2.2 的特性接力链「grep」出来，验证文档说法。
2. **操作步骤**：在仓库任意目录执行（:/ 前缀表示从仓库根搜索）：

```bash
git grep -n "deepseek-v2-lite" HEAD -- ":/*.toml"
```

3. **需要观察的现象**：输出应同时命中三个 crate 的 Cargo.toml——pegainfer-server（依赖声明 + feature 定义）、pegainfer-deepseek-v2-lite（feature 定义 + 测试的 required-features 门）、pegainfer-kernels（标记 feature），以及仓库根 Cargo.toml 的成员/依赖行。
4. **预期结果**：与本讲 4.2.3 列出的行号一致（本讲写作时已在 HEAD 8886aa47 上实际运行过这条只读 git 命令并核对了行号；在更新的提交上运行时行号可能变化，但三个文件都会命中这一事实不变）。
5. 进阶（**待本地验证**）：cargo tree -e features -p pegainfer-deepseek-v2-lite 可以画出特性展开图，但需要可解析的依赖环境（网络或已配置镜像），本讲未运行。

#### 4.2.5 小练习与答案

1. **问**：全仓库唯一的默认模型 feature 是哪个？本 crate 自己的 default 又是什么？
   **答**：默认模型 feature 是 qwen3（CLAUDE.md 第 9 行）；本 crate 的 default = []（[Cargo.toml:L15](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-deepseek-v2-lite/Cargo.toml#L15)），即默认什么都不额外开启。
2. **问**：为什么集成测试 e2e_ep2 的定义里要写 required-features = "deepseek-v2-lite"？
   **答**：见 [Cargo.toml:L42-L45](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-deepseek-v2-lite/Cargo.toml#L42-L45)——测试依赖的模块被特性门控，不开特性时测试代码无法编译，cargo 会直接跳过该测试而不是报错。
3. **问**：如果把 pegainfer-kernels 里的 feature 名 deepseek-v2-lite 改名，会发生什么？
   **答**：接力链断裂：本 crate feature 数组里的 "pegainfer-kernels/deepseek-v2-lite"（[Cargo.toml:L13](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-deepseek-v2-lite/Cargo.toml#L13)）会指向不存在的特性，编译报错——feature 名是一份跨 crate 的契约。

### 4.3 整体架构图解读

#### 4.3.1 概念说明

把仓库按「谁负责什么」切成四层：

1. **协议与契约层**（`pegainfer-frontend`）：实现 OpenAI 兼容协议栈（今天的 vLLM 栈），并定义引擎契约——`EngineHandle`、`GenerateRequest`、`TokenEvent`。模型 crate 只面向契约编程，服务端二进制只做纯派发，因此**新增模型线不需要动前端**。
2. **模型层**（`pegainfer-qwen3`、`pegainfer-deepseek-v2-lite` 等）：每条模型线自己拥有 config、权重、prefill/decode 执行、调度器、测试与基准——「一模型一世界」。
3. **共享运行时层**（`pegainfer-core` + `pegainfer-kernels`）：core::ops 提供共享 GPU 算子封装；kernels 是张量/FFI/kernel 的构建所有者（CUDA、cuBLAS、FlashInfer、Triton AOT）。
4. **硬件后端层**：CUDA/cuBLAS、Triton AOT 产物、FlashInfer（采样、注意力、norm、MLA decode）。

#### 4.3.2 核心流程

CLAUDE.md 的架构图（[L78-L98](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/CLAUDE.md#L78-L98)）可以按行读成「一次请求的下坠路径」：

```text
HTTP Request → vLLM frontend → EngineHandle → per-model scheduler/executor → TokenEvent    ← 主链路（原图 L81）
        分叉：进入哪条模型线（qwen3 / qwen35 / dsv2-lite / kimi-k2 / glm52 / ...）            ← 原图 L83-L89
        汇聚：所有模型线都落在 pegainfer-core runtime + pegainfer-kernels                     ← 原图 L91
        再分叉：CUDA/cuBLAS、Triton AOT、FlashInfer                                            ← 原图 L93-L97
```

三个值得记住的配套机制（后续单元展开）：

- **CUDA Graph**：decode 路径在模型执行器内被捕获成 CUDA Graph，用预分配缓冲保持指针稳定，消除每步大量小 kernel 的启动开销；
- **KV 状态分工**：模型调度器拥有请求状态，共享的分页 KV 原语放在 `pegainfer-kv-cache`，主机/SSD/RDMA 卸载桥在 `pegainfer-kv-offload`；
- **构建体系**：虚拟工作区根没有构建脚本，CUDA/Triton 编译统一由 `pegainfer-kernels/build.rs` 拥有。

#### 4.3.3 源码精读

- [CLAUDE.md:L81](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/CLAUDE.md#L81)：主链路原文 HTTP Request → vLLM frontend → EngineHandle → per-model scheduler/executor → TokenEvent——这就是本讲要求背下来的那一句。
- [CLAUDE.md:L100-L107](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/CLAUDE.md#L100-L107)：六条 Key abstractions。其中 [L102](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/CLAUDE.md#L102) 定义了前端契约三件套与 ModelLine 派发 trait，并点明「模型 crate 面向契约实现，server 只做纯派发」；[L104](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/CLAUDE.md#L104) 是共享算子层；[L105](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/CLAUDE.md#L105) 是 kernels 构建所有者；[L106](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/CLAUDE.md#L106) 是 CUDA Graph；[L107](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/CLAUDE.md#L107) 是 KV 状态分工。
- [CLAUDE.md:L109-L111](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/CLAUDE.md#L109-L111)：构建体系两步——nvcc 编译 pegainfer-kernels/csrc 下的 .cu 文件（自动探测 GPU SM 目标），再做特性门控的代码生成（qwen35 走 Triton AOT、kimi-k2 / glm52 各自增加 CUDA 源）。
- 契约在本 crate 侧的「使用痕迹」：[src/lib.rs:L39-L40](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-deepseek-v2-lite/src/lib.rs#L39-L40) 的两行 use 语句从 pegainfer_frontend::engine 导入 EngineHandle 与 EngineLoadOptions——模型 crate 只 import 前端契约类型，不 import 任何 HTTP/协议实现，这就是「面向契约」的代码证据；[src/lib.rs:L44-L52](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-deepseek-v2-lite/src/lib.rs#L44-L52) 则把 runtime 模块的生成器与统计类型（含 DeepSeekV2LiteEp2Generator）重新导出给上层。

#### 4.3.4 代码实践

1. **实践目标**：把 CLAUDE.md 的 ASCII 架构图变成「自己能默画」的图。
2. **操作步骤**：打开 [CLAUDE.md:L78-L98](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/CLAUDE.md#L78-L98)，把整张图抄写（或文本重绘）一遍，给**每个箭头**写一句中文注释（数据是什么、为什么流向这一层）；再用 [src/lib.rs:L39-L40](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-deepseek-v2-lite/src/lib.rs#L39-L40) 的 use 行验证「模型 crate 不碰协议实现」这句话。
3. **需要观察的现象**：图中间一排是各模型 crate（其中就有 dsv2-lite），所有模型线之下汇聚到 pegainfer-core runtime + pegainfer-kernels（原图第 91 行），最底部分叉到 CUDA/cuBLAS、Triton AOT、FlashInfer。
4. **预期结果**：一张带注释的图，加一句结论「dsv2-lite 之上只有前端契约，之下只有共享运行时」。纯阅读型实践，无需运行命令，无「待本地验证」项。

#### 4.3.5 小练习与答案

1. **问**：CUDA Graph 主要解决什么问题？为什么需要「预分配缓冲」？
   **答**：decode 每步要发射大量小 kernel，launch 开销占比高；把整步捕获成 Graph 后一次重放即可。捕获时记住了缓冲地址，所以必须预分配缓冲、保证指针稳定（[CLAUDE.md:L106](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/CLAUDE.md#L106)）。
2. **问**：请求状态（KV）归谁所有？分页 KV 原语放在哪？
   **答**：模型调度器拥有请求状态；共享分页 KV 原语在 pegainfer-kv-cache，卸载桥在 pegainfer-kv-offload（[CLAUDE.md:L107](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/CLAUDE.md#L107)）。
3. **问**：为什么 CUDA 编译不在工作区根的构建脚本里做？
   **答**：虚拟工作区根根本没有 package 构建脚本；pegainfer-kernels 是张量/FFI/kernel 的构建所有者，其 build.rs 统一负责 nvcc/Triton 编译（[CLAUDE.md:L109-L111](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/CLAUDE.md#L109-L111)）。

### 4.4 dsv2-lite 的位置

#### 4.4.1 概念说明

在矩阵里，dsv2-lite 的标签是 **MoE + EP、2-GPU**（[CLAUDE.md:L15](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/CLAUDE.md#L15)）。三个关键事实：

- **它是模型矩阵中规模最小的 MoE + EP 入口**：2 张 GPU 就能体验完整的专家并行（kimi-k2、glm52 要 8 卡 EP）；仓库文档用 **EP2** 指代这个 2 卡形态。
- **模型几何是硬校验的**：crate 会在启动期确认这份权重真的是 DeepSeek-V2-**Lite**（model_type、hidden_size、n_routed_experts 三重门，见下），而不是「名字像就行」。
- **它有自己的活文档组**：docs/models/deepseek-v2-lite/ 下有状态台账、设备侧注意力/MoE 设计、基准方法、精度门等文档（4.4.3 给出索引）。

#### 4.4.2 核心流程

启动期的「模型识别 → 接管」流程（伪代码）：

```text
服务端启动，读取 --model-path 下的 config.json
  └─▶ 逐模型线调用 ModelLine::probe(config)
        dsv2-lite 的三重门（probe_config_json）：
          ① model_type == "deepseek_v2"     否则 → 拒绝理由：不是 deepseek_v2
          ② n_routed_experts == 64          否则 → bail: unsupported DeepSeek-V2 config
          ③ hidden_size 匹配支持值         否则 → bail: unsupported DeepSeek-V2 config
  └─▶ probe 通过 → ModelLine::launch(model_path, cuda_graph)
        └─▶ launch 组装 EP2 运行参数并调 engine::start_engine，返回 EngineHandle
              （设备固定 0 与 1 两卡、EP 后端 NCCL、seed 42、cuda_graph 请求被忽略并告警）
```

EP 形态在执行期还有一道几何校验：64 个路由专家必须被 2 张卡均分，每卡 32 个（ep.rs 的 validate_for）。

#### 4.4.3 源码精读

- [src/model_line.rs:L7-L34](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-deepseek-v2-lite/src/model_line.rs#L7-L34)：dsv2-lite 的 ModelLine 实现——MODEL_LINE 静态实例；probe() 委托给 crate::probe_config_json 判断接管与否；launch() 调 crate::launch 并包装成 LaunchedEngine::Handle。这二十来行就是「模型 crate 接入前端派发」的全部接缝（下为节选，注释与省略为本讲所加）：

```rust
pub static MODEL_LINE: DeepSeekV2LiteLine = DeepSeekV2LiteLine;

impl ModelLine for DeepSeekV2LiteLine {
    fn name(&self) -> &'static str { "DeepSeek-V2-Lite" }
    fn probe(&self, config: &serde_json::Value) -> Result<(), String> {
        match crate::probe_config_json(config) { /* Ok(true) 即接管；其余给出拒绝原因 */ }
    }
    fn launch(&self, ctx: &LaunchContext<'_>) -> anyhow::Result<LaunchedEngine> {
        crate::launch(ctx.model_path, ctx.shared.cuda_graph).map(LaunchedEngine::Handle)
    }
}
```

- [src/lib.rs:L54-L66](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-deepseek-v2-lite/src/lib.rs#L54-L66)：三重门的实现——先取 model_type 判断是否等于 "deepseek_v2"；再用 n_routed_experts == SUPPORTED_ROUTED_EXPERTS（64）且 hidden_size == SUPPORTED_HIDDEN_SIZE 联合判断是否 Lite 版本；不匹配则用 bail 宏返回带具体数值的 unsupported DeepSeek-V2 config 错误信息。
- [src/lib.rs:L91-L112](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-deepseek-v2-lite/src/lib.rs#L91-L112)：launch 的实现。源码 doc comment 明说「The EP=2 topology (devices 0..1) is fixed by the model」——EP2 拓扑由模型固定，不由用户配置；cuda_graph 请求会被忽略并打 warn。关键参数（原文节选）：

```rust
fn launch(model_path: &Path, cuda_graph: bool) -> Result<EngineHandle> {
    if cuda_graph {
        log::warn!("DeepSeek V2 Lite does not support CUDA Graph; ignoring --cuda-graph=true");
    }
    engine::start_engine(
        model_path,
        EngineLoadOptions {
            enable_cuda_graph: false,
            device_ordinals: vec![0, 1],
            parallel_config: None,
            ep_backend: EpBackend::Nccl,
            seed: 42,
        }
    )
}
```

- [src/ep.rs:L8-L9](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-deepseek-v2-lite/src/ep.rs#L8-L9)：const SUPPORTED_EP_SIZE: usize = 2 与 pub(crate) const SUPPORTED_ROUTED_EXPERTS: usize = 64——「2-GPU、64 专家」两个架构标签的直接代码出处。
- [src/ep.rs:L29-L50](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-deepseek-v2-lite/src/ep.rs#L29-L50)：validate_for 的逐条 ensure 校验——rank 必须小于 2、路由专家数必须恰为 64、必须能被 2 整除、且每卡恰好 32 个；任何一条不满足都会带着具体数值报错、拒绝启动。
- [docs/index.md:L117-L128](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/docs/index.md#L117-L128)：models / deepseek-v2-lite 文档组索引——[L121](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/docs/index.md#L121) 状态台账 status.md（EP2 的正确性/归因/可靠性分桶，明确「无 parity 或生产声明」）、[L122](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/docs/index.md#L122) 设备侧路由 MoE、[L123](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/docs/index.md#L123) 设备侧稳态 decode 注意力、[L125](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/docs/index.md#L125) 单机 EP2 路线图、[L127](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/docs/index.md#L127) HF 精度门（HF generate 与 host-staged EP2、NCCL EP2 三方对齐）。
- [Cargo.toml:L42-L50](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-deepseek-v2-lite/Cargo.toml#L42-L50)：两个带 required-features 门的入口——集成测试 e2e_ep2（tests/e2e_ep2.rs）与诊断二进制 dsv2_lite_ep2_decode_attribution（decode 逐步归因工具），名字里的 ep2 再次印证 2 卡 EP 形态。

#### 4.4.4 代码实践

1. **实践目标**：通过现成单测理解「模型识别」的边界条件，学会用测试读行为。
2. **操作步骤**：通读 [src/model_line.rs:L36-L61](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-deepseek-v2-lite/src/model_line.rs#L36-L61) 的两个测试 probe_rejects_foreign_model_type 与 probe_rejects_non_lite_shape_with_the_real_reason，记下各自的输入 config 与断言；然后（可选，**待本地验证**）运行 cargo test --release -p pegainfer-deepseek-v2-lite --lib 观察这两个用例通过（本讲未运行；它们是纯逻辑单测，不需要 GPU）。
3. **需要观察的现象**：第一个测试喂 {"model_type": "qwen3"}，断言拒绝理由包含 deepseek_v2；第二个测试喂 model_type 正确但 n_routed_experts=1、hidden_size=1 的「假 Lite」，断言拒绝理由包含 unsupported DeepSeek-V2 config——即「形状不符」必须报形状问题，而不是误报「不是 deepseek_v2」。
4. **预期结果**：能用自己的话说出三重门中每一关的拒绝语义。若有本地 DeepSeek-V2-Lite 权重目录，可打开其 config.json 核对 model_type、n_routed_experts、hidden_size 三个字段与 ep.rs 的 64/2 对得上（无权重环境可到 Hugging Face 模型页查看，属「待本地验证」）。

#### 4.4.5 小练习与答案

1. **问**：probe 为什么不只查 model_type，还要查专家数和 hidden_size？
   **答**：deepseek_v2 这个 model_type 覆盖完整版 DeepSeek-V2 等非 Lite 配置，它们的几何与本实现支持的不同；三重门保证只接管真正支持的那份形状（[src/lib.rs:L54-L66](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-deepseek-v2-lite/src/lib.rs#L54-L66)）。
2. **问**：EP2 形态下每张卡放多少个路由专家？由哪段代码保证？
   **答**：32 个。[src/ep.rs:L29-L50](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-deepseek-v2-lite/src/ep.rs#L29-L50) 的连环校验：64 必须整除以 2，且商必须恰为 32。
3. **问**：想了解 dsv2-lite 目前「做到哪一步了」，应该先读哪份文档？
   **答**：docs/models/deepseek-v2-lite/status.md——EP2 状态台账，把正确性、直接归因、生命周期可靠性等分成独立分桶，并明确当前没有 parity / 生产声明（[docs/index.md:L121](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/docs/index.md#L121)）。

## 5. 综合实践

**任务**：通读 CLAUDE.md 的「What is PegaInfer」（[L3-L19](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/CLAUDE.md#L3-L19)）与「Architecture」（[L78-L111](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/CLAUDE.md#L78-L111)）两节，以及 [docs/index.md](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/docs/index.md) 的目录表（L1-L14）与 deepseek-v2-lite 段（L117 起），然后手绘（或用文本画）一张**从 HTTP 请求到 TokenEvent 的调用链图**，图上必须标出 pegainfer-deepseek-v2-lite、pegainfer-frontend、pegainfer-core、pegainfer-kernels、pegainfer-kv-cache 五个组件所在的层；再为**每一层写一句话**说明它为什么存在（共 5 句）。

**参考答案**（依据 CLAUDE.md:L80-L98 的架构图重绘的示例）：

```text
HTTP POST /v1/completions（OpenAI 格式）
   │  ① pegainfer-frontend：vLLM 协议栈解析请求 → GenerateRequest
   ▼
EngineHandle（引擎契约：请求进 / TokenEvent 出）
   ▼
② pegainfer-deepseek-v2-lite：scheduler / executor
   │   prefill / decode · MoE 路由 → EP dispatch/combine（2 GPU，每卡 32 专家）
   │   ③ KV 状态：调度器持有请求状态，分页原语来自 pegainfer-kv-cache
   ▼
TokenEvent → 前端序列化 → 流式 HTTP 响应
（底层贯穿：④ pegainfer-core runtime + ⑤ pegainfer-kernels → CUDA/cuBLAS、Triton AOT、FlashInfer、NCCL）
```

**五句话参考**（每句对应一层，回答「为什么存在」）：

1. pegainfer-frontend 存在，是为了把「对外协议」与「引擎内核」解耦——协议怎么变（今天的 vLLM 栈、规划中的 dynamo），内核不用跟着改。
2. EngineHandle 契约存在，是为了让模型 crate 可插拔：dsv2-lite 只面向契约实现，服务端只做纯派发，新增模型线不动前端（CLAUDE.md:L102）。
3. pegainfer-deepseek-v2-lite 存在，是因为 MoE + EP 模型的调度与执行（路由、跨卡通信、2 卡专家布局）和稠密模型差异太大，值得一条独立模型线自己拥有 config / 权重 / 执行 / 调度 / 测试。
4. pegainfer-core 与 pegainfer-kernels 存在，是为了让所有模型线共享 GPU 算子封装与 CUDA / Triton / FlashInfer 构建体系，不必每条线重复造轮子（CLAUDE.md:L104-L105、L109-L111）。
5. pegainfer-kv-cache 存在，是为了把分页 KV 这一跨模型通用原语抽成共享库，而请求级状态仍留在各模型调度器手里（CLAUDE.md:L107）。

**自检清单**：图中至少出现 5 个组件层名、2 个契约类型名（GenerateRequest / TokenEvent / EngineHandle 任两个）、1 个 EP 元素（dispatch/combine 或「每卡 32 专家」）；五句话每句都回答「为什么存在」而不是「它是什么」。本实践为阅读加绘图型，无需运行命令，无待验证项。

## 6. 本讲小结

- PegaInfer 是纯 Rust + CUDA、无 PyTorch / 无框架的 LLM 推理引擎，对外提供 OpenAI 兼容 /v1/completions；默认构建（仅 qwen3 特性）不含任何 Python（[CLAUDE.md:L5](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/CLAUDE.md#L5)、[L9](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/CLAUDE.md#L9)）。
- 主链路一句话：HTTP Request → vLLM frontend → EngineHandle → per-model scheduler/executor → TokenEvent（[CLAUDE.md:L81](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/CLAUDE.md#L81)）。
- 模型支持 = 7 条模型线 × 各自的 crate + 同名 feature；dsv2-lite 的标签是 MoE + EP、2-GPU，feature 为 deepseek-v2-lite（[CLAUDE.md:L11-L19](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/CLAUDE.md#L11-L19)）。
- 特性是接力棒：pegainfer-server → 本 crate → pegainfer-kernels，三级 manifest 各自声明 / 转发同名 feature；crate 内部再用 cfg(feature) 门控模块，未开特性时入口函数以「rebuild with --features deepseek-v2-lite」报错占位。
- dsv2-lite 通过 ModelLine::probe 的三重门（model_type 为 deepseek_v2、64 路由专家、匹配的 hidden_size）接管模型；EP2 几何由 ep.rs 硬校验（每卡恰 32 专家），launch 固定设备 0 与 1、EP 后端 NCCL。
- 文档入口在 docs/index.md；dsv2-lite 的活文档组是 docs/models/deepseek-v2-lite/（状态台账、设备侧 MoE / 注意力、基准、精度门）。

## 7. 下一步学习建议

- **下一讲 u1-l2（构建环境准备与特性开关）**：动手把 --features deepseek-v2-lite 真正编译出来，弄清 PEGAINFER_NCCL_ROOT、PEGAINFER_CUDA_SM 等环境变量与 pegainfer-kernels 的 build.rs 构建流水线。
- **之后 u1-l4（crate 目录结构导航）**：系统走读 src/ 下 config、weights、model、runtime、scheduler 等模块，把本讲的「接缝」扩展成完整文件地图。
- **延伸阅读**（可用 [docs/index.md:L117-L128](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/docs/index.md#L117-L128) 定位）：docs/models/deepseek-v2-lite/status.md（EP2 现状台账）与 roadmap.md（单机 EP2 路线）。
- **想先看代码**：[src/model_line.rs](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-deepseek-v2-lite/src/model_line.rs) 全文仅 62 行（含两个单测），是理解「一条模型线如何接入引擎」最短的完整样本。
