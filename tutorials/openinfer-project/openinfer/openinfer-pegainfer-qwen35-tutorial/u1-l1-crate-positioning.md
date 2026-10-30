# 从 PegaInfer 到 pegainfer-qwen35：crate 定位与混合架构总览

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 PegaInfer 整体是什么（纯 Rust + CUDA 的 LLM 推理引擎），以及一条 HTTP 请求从进入到吐出 token 经过哪些层。
2. 在 workspace 的 8 个模型 crate 中指出 `pegainfer-qwen35` 的位置，并说明它的架构特殊性：Gated DeltaNet 线性注意力与全注意力（full attention）的混合模型。
3. 解释为什么整个 crate 被 `#![cfg(feature = "qwen35")]` 门控、为什么默认构建必须保持「无 Python」。
4. 梳理 `pegainfer-qwen35` 对 `pegainfer-core` / `pegainfer-kernels` / `pegainfer-kv-cache` / `pegainfer-frontend` / `pegainfer-sample` 五个 workspace 依赖各自的职责边界。
5. 说清 Qwen3.8-27B 为什么与 Qwen3.5 共用同一条 model line。

本讲是整个 Qwen3.5 系列讲义的第一篇，重在建立**地图**而不是深入任何一条执行链。后续每一讲都会在这张地图上放大某一块。

## 2. 前置知识

本讲假设你了解以下概念（不了解也没关系，下面用通俗语言补齐）：

- **LLM 推理的两个阶段**：
  - *prefill（预填充）*：把用户输入的整段提示词一次性喂给模型，算出第一个输出 token。计算量大，可以高度并行。
  - *decode（解码）*：之后每步只喂 1 个 token，反复迭代生成后续 token。计算量小但访存密集，工程优化重点在批处理与 CUDA Graph。
- **KV cache**：全注意力层在 prefill 阶段为每个历史 token 缓存一份 K、V 向量，decode 时直接读取。它随上下文长度线性增长，是长上下文显存的主要开销。
- **注意力（attention）**：模型让当前 token 去「查询」历史 token 的机制。标准做法是 softmax 注意力，本讲称之为**全注意力**（full attention）。
- **线性注意力 / Gated DeltaNet（GDN）**：把注意力改写成「维护一个固定大小的递推状态，每步增量更新」的形式。状态大小与上下文长度无关——这是 Qwen3.5 混合架构省显存的关键，第 4.2 节展开。
- **Cargo workspace 与 feature**：一个 git 仓库里放多个 crate（`Cargo.toml` 的 `[workspace] members`），共享依赖版本；`[features]` 让代码按需编译——不开 feature，代码就不参与编译。
- **CUDA 基本概念**：只需知道 GPU 上的计算叫「内核（kernel）」，矩阵乘法常用 cuBLAS 库，多个 CUDA stream 可以并行执行。不需要会写 CUDA。

**不需要**的背景：PyTorch 或任何 Python 推理框架经验。PegaInfer 的卖点是「No PyTorch, no frameworks」——服务器运行时全程无 Python，只在构建 Qwen3.5 的 Triton 内核时借用一次 Python 工具链（第 4.3 节）。

## 3. 本讲源码地图

本讲涉及的关键文件（路径相对 `pegainfer-qwen35/`）：

| 文件 | 作用 |
| --- | --- |
| `../CLAUDE.md` | 仓库级指南：模型支持表、构建命令、架构图。本讲的「全景」主要依据它 |
| `../Cargo.toml` | workspace 清单：21 个成员 crate，其中 8 个是模型 crate |
| `Cargo.toml` | 本 crate 的依赖、`qwen35` feature、6 个集成测试与 1 个 bench 的声明 |
| `src/lib.rs` | crate 门控注释、模块树、启动入口 `start_engine*` 系列 |
| `src/ops.rs` | 算子汇总面：前半来自 `pegainfer-core`（共享算子），后半来自 `pegainfer-kernels`（本行专属内核）——依赖边界的最直观证据 |
| `src/ffi.rs` | 只有一行：重导出 `pegainfer_core::ffi::*` 的内核 FFI 符号 |
| `src/weights/layers.rs` | `FullAttentionLayer` / `LinearAttentionLayer` / `LayerKind` / `TransformerBlock35`——混合架构在源码里的落点 |
| `src/model_line.rs` | `Qwen35Line` 实现 frontend 的 `ModelLine` trait：probe 识别 config.json、增补 CLI 参数 |
| `../pegainfer-kernels/Cargo.toml` | feature 面板：`qwen35` feature 拉起 Triton AOT 内核构建 |
| `../docs/models/qwen35/model-crate.md` | 本 crate 的官方文档（职责边界、启动握手、构建校验） |
| `../docs/models/qwen35/support-qwen38.md` | Qwen3.8-27B 共线的逐字段证据 |

## 4. 核心概念与源码讲解

### 4.1 workspace 全景

#### 4.1.1 概念说明

PegaInfer 是一个**纯 Rust + CUDA 的 LLM 推理引擎**：不依赖 PyTorch，不依赖任何推理框架，对外提供 OpenAI 兼容的 `/v1/completions` API。仓库本身就是个 Cargo workspace，按职责分四层：

1. **服务层**：`pegainfer-server`（默认唯一构建目标的二进制）+ `pegainfer-frontend`（定义引擎请求/事件契约与协议栈）。
2. **模型层**：8 个模型 crate，每个模型线一个，各自拥有 config、权重、prefill/decode 执行、调度器与测试。
3. **运行时层**：`pegainfer-core`（共享 GPU 算子）、`pegainfer-kernels`（CUDA/Triton 内核的构建所有者）、`pegainfer-sample`（采样）、`pegainfer-kv-cache` / `pegainfer-kv-offload` / `pegainfer-kv-store`（分页 KV 与卸载）。
4. **辅助层**：`pegainfer-build`、`pegainfer-bench`、`pegainfer-cupti`、`pegainfer-sim`，以及从 dynamo 移植的 `kvbm/kvbm-logical`。

关键设计：**每条模型线都藏在一个 cargo feature 后面，只有 `qwen3` 是默认 feature**。于是「出厂构建」是纯 Rust + CUDA、无任何 Python；想跑 Qwen3.5 必须显式 `--features qwen35`。

#### 4.1.2 核心流程

CLAUDE.md 里的一行架构图（[../CLAUDE.md:L87-L92](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/CLAUDE.md#L87-L92)）概括了一条请求的旅程：

```text
HTTP Request → vLLM frontend → EngineHandle → per-model scheduler/executor → TokenEvent
                                               │
              ┌──────────┬─────────────┬───────┼───────────┬──────────┐
              │          │             │       │           │          │
           qwen3      qwen35       dsv2-lite kimi-k2    glm52       …(模型 crate)
              │          │             │       │           │          │
              └──────────┴─────────────┴───────┼───────────┴──────────┘
                                               │
                          pegainfer-core runtime + pegainfer-kernels
                                               │
                               ┌───────────────┼───────────────┐
                               │               │               │
                        CUDA / cuBLAS    Triton AOT      FlashInfer
```

把 `pegainfer-qwen35` 放进这张图：

- **向上**：它实现 `pegainfer-frontend` 的契约（`Engine`、`ModelLine` trait），HTTP 层对它一无所知，只认契约。
- **向下**：它调用 `pegainfer-core` 的通用算子与 `pegainfer-kernels` 的专属内核落到 GPU 上。
- **同层**：它与 `pegainfer-qwen3`、`pegainfer-qwen4` 等 7 个兄弟 crate 互不依赖、互不引用——模型之间的共性被压到 core/kernels/frontend 里，而不是互相继承。

#### 4.1.3 源码精读

**workspace 成员清单**。[../Cargo.toml:L1-L27](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/Cargo.toml#L1-L27) 声明了 `default-members = ["pegainfer-server"]` 和 21 个成员。注意第 12–19 行连续列出的 8 个模型 crate：

```toml
  "pegainfer-deepseek-v2-lite",
  "pegainfer-gemma4",
  "pegainfer-glm52",
  "pegainfer-k3",
  "pegainfer-kimi-k2",
  "pegainfer-qwen3",
  "pegainfer-qwen35",
  "pegainfer-qwen4",
```

`default-members` 只含 server 意味着：仓库根目录裸跑 `cargo build` 不会编译全部模型，只有 server 及其启用的默认 feature（`qwen3`）参与构建。

**模型支持表**。[../CLAUDE.md:L7-L20](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/CLAUDE.md#L7-L20) 给出每条线的 crate、feature 与架构。与本讲直接相关的一行（L14）：

> `Qwen3.5-4B / 9B / 27B · Qwen3.8-27B | pegainfer-qwen35 | --features qwen35 (needs build-time Python + Triton) | Hybrid Gated DeltaNet + full attention. Qwen3.8 shares the line: same model_type, same text geometry`

这张表能回答三个定位问题：

| 问题 | 答案来源 |
| --- | --- |
| 哪些模型由本 crate 服务？ | Qwen3.5-4B/9B/27B **和** Qwen3.8-27B（共线） |
| 它特殊在哪？ | 唯一「线性注意力 + 全注意力」混合的默认相邻线；也是唯一需要构建期 Python + Triton 的 feature |
| 兄弟线都是什么架构？ | qwen3 纯全注意力；dsv2-lite / kimi-k2 / glm52 是 MoE 系；gemma4 滑动窗口；k3 混合 KDA + MLA |

#### 4.1.4 代码实践

**实践目标**：用只读命令亲手确认 workspace 的分层，而不是背表。

**操作步骤**：

1. 在仓库根目录列出全部成员并数一下：

   ```bash
   grep -A 30 '^members' Cargo.toml
   ```

2. 用 cargo 自带的只读元数据命令看依赖图的一角（不需要 GPU）：

   ```bash
   cargo metadata --no-deps --format-version 1 | head -c 800
   ```

3. 对照输出，把 21 个成员按 4.1.1 的四层归类，标出 8 个模型 crate。

**需要观察的现象**：`members` 列表里模型 crate 的名字连续排列；`default-members` 只有一个 `pegainfer-server`。

**预期结果**：你得到一张手写的分层清单，其中 `pegainfer-qwen35` 位于模型层，且被 `pegainfer-server` 依赖链拉入的前提是开启 `qwen35` feature。`cargo metadata` 在本仓库的确切输出**待本地验证**（该命令只读、无副作用，可放心运行）。

#### 4.1.5 小练习与答案

**练习 1**：为什么在仓库根目录直接 `cargo build` 得不到 Qwen3.5 的支持？

**答案**：两层原因。第一，`default-members = ["pegainfer-server"]`，默认只构建 server；第二，server 对各模型线的支持走 feature 转发，只有 `qwen3` 是默认 feature，`qwen35` 必须显式 `--features qwen35` 开启（CLAUDE.md 的 Build & Run 一节，[../CLAUDE.md:L21-L30](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/CLAUDE.md#L21-L30)）。

**练习 2**：`pegainfer-server` 和 `pegainfer-frontend` 的分工是什么？

**答案**：frontend 定义「引擎请求/事件契约 + 协议栈 + `ModelLine` dispatch trait」，即 `pegainfer_frontend::engine` 的 `EngineHandle`、`GenerateRequest`、`TokenEvent` 等；server 二进制只做纯分派（CLAUDE.md Key abstractions，[../CLAUDE.md:L106-L112](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/CLAUDE.md#L106-L112)）。模型 crate 实现契约，server 组装命令行并拉起对应线。

**练习 3**：不看上文，说出 8 个模型 crate 的名字。

**答案**：`pegainfer-qwen3`、`pegainfer-qwen35`、`pegainfer-qwen4`、`pegainfer-deepseek-v2-lite`、`pegainfer-gemma4`、`pegainfer-glm52`、`pegainfer-k3`、`pegainfer-kimi-k2`（[../Cargo.toml:L12-L19](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/Cargo.toml#L12-L19)）。

### 4.2 混合架构背景

#### 4.2.1 概念说明

**为什么需要混合注意力？** 全注意力 decode 时每步要对全部历史做 softmax 注意力，KV cache 随上下文线性增长；上下文拉到 26 万 token 时，仅 KV cache 就可能吃掉几十 GiB 显存。线性注意力（Qwen3.5 采用的变体叫 **Gated DeltaNet**）把「记住历史」改成一个固定大小的递推状态矩阵：每来一个 token，状态按门控规则更新一次，显存与上下文长度**无关**。代价是它损失一部分精确检索能力，且状态更新是不折不扣的串行依赖。

**Qwen3.5 的取舍**：两种层混用——少量全注意力层保留全局精确检索，大量线性注意力层压住显存与计算。以 Qwen3.5-4B 为例：32 层里 **8 层全注意力 + 24 层线性注意力**（源码注释直接写明，见 4.2.3）；27B 规格则是 16 层全注意力 + 48 层线性注意力（[support-qwen38.md:L36](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/docs/models/qwen35/support-qwen38.md#L36)）。

**KV cache 的显存账**（理解动机用，示例演算）：一个全注意力层每缓存一个 token 需要存一份 K 和一份 V，若以 bf16（2 字节）存储：

\[
\text{KV 字节/token} = \#\text{full 层} \times 2 \times \#\text{KV heads} \times \text{head\_dim} \times 2\,\text{字节}
\]

代入 27B 的几何（16 层 full、4 个 KV head、head_dim 256）：\(16 \times 2 \times 4 \times 256 \times 2 = 65536\) 字节/token = 64 KiB/token。填满 262144 上下文就是 \(64\,\text{KiB} \times 262144 = 16\,\text{GiB}\)——而这 16 GiB 只覆盖 16 个 full 层，另外 48 个线性层贡献的增量是 0。这就是混合架构的全部动机。

**对工程结构的直接影响**：这个 crate 里同时存在两套状态系统——

- 全注意力层 → 分页 KV cache（`pegainfer-kv-cache` 的 `KvView`/`KvBuffer`）；
- 线性注意力层 → recurrent 状态与 conv 状态（`src/recurrent_state.rs`）。

后面第 6 单元你会看到，连「前缀缓存」都必须是 KV + recurrent 的**联合**快照，缺一不可——这是纯全注意力模型没有的复杂度。

#### 4.2.2 核心流程

一个 `TransformerBlock35` 的前向（两种层共享骨架，只有注意力子层不同）：

```text
hidden
  │
  ├─ input_layernorm（RMSNorm）
  ├─ attn ──按 LayerKind 分发──┬─ FullAttention：q/k/v 投影 → 读写分页 KV → attention → o_proj
  │                            └─ LinearAttention：in_proj_qkvz → causal conv1d → GDN 递推状态更新 → norm → out_proj
  ├─ 残差相加
  ├─ post_attention_layernorm
  ├─ mlp（gate_up_proj → 激活 → down_proj，两种层共用）
  └─ 残差相加 → 输出 hidden
```

要点：

- **层类型在权重加载时就固定**：`TransformerBlock35::load` 接收 config 解析出的 `LayerType`，据此构造 `LayerKind::FullAttention` 或 `LayerKind::LinearAttention`，运行期不再判断字符串。
- 两种注意力层的** MLP 完全相同**（`MLP35`），所以混合带来的复杂度被隔离在注意力子层里。

#### 4.2.3 源码精读

**两种注意力层的权重结构**。[src/weights/layers.rs:L15-L28](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L15-L28) 定义全注意力层——注意注释里写明「Qwen3.5-4B 中有 8 层」，且 `q_proj` 把门控（gate）维度拼进了投影：

```rust
/// Full attention layer weights (8 layers in Qwen3.5-4B).
pub(crate) struct FullAttentionLayer {
    /// Q projection including gate: [num_heads * head_dim * 2, hidden_size]
    pub(crate) q_proj: DeviceMatrix,
    ...
}
```

[src/weights/layers.rs:L30-L55](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L30-L55) 定义线性注意力层（24 层）。它的字段表就是 GDN 的「零件清单」：融合的 `in_proj_qkvz`、门控用的 `in_proj_ba`（beta+alpha）、前置的 depthwise `conv1d_weight`、递推参数 `a_log`/`dt_bias`，以及输出侧 `out_proj`（注释还点明它在 TP 下是 row-parallel、需要 all-reduce——这是第 7 单元的内容，这里先留个眼）。

**层分发**。[src/weights/layers.rs:L57-L77](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L57-L77) 用一个枚举表达「每层二选一」，并给出 `full_attention()` / `linear_attention()` 两个安全取值方法（返回 `Option`，拿错类型得到 `None` 而不是 panic）：

```rust
pub(crate) enum LayerKind {
    FullAttention(FullAttentionLayer),
    LinearAttention(LinearAttentionLayer),
}
```

**块的组装**。[src/weights/layers.rs:L103-L124](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L103-L124) 里 `TransformerBlock35` 持有 `attn: LayerKind`；`load` 按 `LayerType`（来自 config.json 的 `layer_types` 数组）决定构造哪种层，张量前缀也随之不同（`self_attn` vs `linear_attn`）。

**Qwen3.8 共线的证据**。[support-qwen38.md:L3-L23](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/docs/models/qwen35/support-qwen38.md#L3-L23) 的 TL;DR 说得非常直白：Qwen3.8-27B **不是**新模型线——它的文本塔与 Qwen3.5-27B 形状完全一致，`config.json` 仍报告 `model_type: qwen3_5`，于是 probe、权重加载、内核、调度器、服务器接线全部原样通过。真正阻塞它的只有两件事：GDN 标量存成 bf16（加载器原本要求 f32）和 logits golden fixture 的 key 区分度——都在已有管线上小修即可。这也是 [model_line.rs:L79-L89](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/model_line.rs#L79-L89) 里 probe 只认 `qwen3_5` / `qwen3_5_text` 两个 `model_type` 的原因：Qwen3.8 靠的就是「identity 没变」。

#### 4.2.4 代码实践

**实践目标**：用源码注释和文档数字，亲手核算混合架构的显存动机，并找到「两种层」在源码里的全部落点。

**操作步骤**：

1. 打开 `src/weights/layers.rs`，读 L15、L30 两行的文档注释，记下 4B 的层数配比（8 full / 24 linear）。
2. 读 [support-qwen38.md:L33-L40](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/docs/models/qwen35/support-qwen38.md#L33-L40) 的表格，抄下 27B 的几何：64 层（16 full + 48 linear）、24 Q / 4 KV heads、head_dim 256、上下文 262144。
3. 用 4.2.1 的公式手算两个数：27B 每 token 的 full 层 KV 字节（答案应为 65536）；填满上下文时的 KV 总量（16 GiB，示例演算，假设 KV 以 bf16 存储）。
4. 在 crate 里 `grep -n "LinearAttention\|FullAttention" src/weights/layers.rs`，数一数除了定义之外还有哪些函数需要同时处理两种层。

**需要观察的现象**：grep 会显示 `LayerKind` 的两个取值方法（L64-L76）以及 `load` 里的 match（L115-L124）是仅有的「感知两种层」的位置之一——混合性被封装得很紧。

**预期结果**：一张写着两个手算数字和一份「两种层差异点」清单的笔记。KV 的实际存储 dtype 与页大小属于第 3、6 单元的内容，此处不要求验证。

#### 4.2.5 小练习与答案

**练习 1**：`LinearAttentionLayer` 里有 `conv1d_weight`，`FullAttentionLayer` 里没有。这个卷积是干什么的？

**答案**：GDN 层把 qkv 输入先过一个 depthwise causal conv1d（kernel 维度 4）再做递推状态更新，权重形状与 qkv 段的通道布局对齐，见 [src/weights/layers.rs:L40-L43](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L40-L43) 的字段注释。全注意力层不需要它。

**练习 2**：`LayerType` 和 `LayerKind` 是两个不同的类型，为什么不合并成一个？

**答案**：`LayerType` 是 config 解析层的轻量枚举（来自 config.json 的 `layer_types` 数组，`src/config/model.rs`）；`LayerKind` 是权重侧的包装枚举，每个变体携带整层已加载的 GPU 权重（[src/weights/layers.rs:L57-L61](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L57-L61)）。前者用于决定「加载什么」，后者是「加载结果」。第 2 单元会看到这套「校验层与运行层分离」的风格贯穿整个 config 模块。

**练习 3**：如果 Qwen3.9 发布且 `model_type` 仍是 `qwen3_5`、文本几何不变，需要新建模型 crate 吗？

**答案**：按 Qwen3.8 的先例不需要——probe 按字符串识别（[src/model_line.rs:L79-L89](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/model_line.rs#L79-L89)），几何不变则内核/缓冲不变，剩下的差异（如 chat template、个别 dtype）在加载器和模板层解决（[support-qwen38.md:L3-L23](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/docs/models/qwen35/support-qwen38.md#L3-L23)）。

### 4.3 crate 依赖边界

#### 4.3.1 概念说明

「依赖边界」回答的问题是：**这个 crate 允许用谁的什么**。看 [Cargo.toml:L8-L24](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/Cargo.toml#L8-L24) 的依赖清单，workspace 内的 5 个直接依赖各管一摊：

| 依赖 | 职责 | crate 内的典型使用 |
| --- | --- | --- |
| `pegainfer-core` | 跨模型共享的 GPU 算子与基础设施：GEMM、RMSNorm、embedding、张量（`DeviceContext`/`DeviceVec`/`HiddenStates`）、CUDA Graph 状态、权重加载器、FFI 符号 | `src/ops.rs` 前 17 行的重导出；`src/weights/layers.rs` 的 `load_tensor_*` 系列 |
| `pegainfer-kernels` | 内核的**构建所有者**：nvcc 编译、Triton AOT 生成、head_dim=256 分页注意力 decode 内核族、conv1d/GDN decode 内核、`StreamOverrideGuard` | `src/ops.rs` 第 20–25 行；`src/unified_forward.rs` 的流切换守卫 |
| `pegainfer-kv-cache` | 分页 KV 原语：`KvView`/`KvBuffer`/`KvCacheManager`/`RequestKv`/`BlockPool` | `src/batch_decode.rs`、`src/scheduler/mod.rs` 的 KV 管理 |
| `pegainfer-frontend` | 引擎契约（`Engine`、`RequestLedger`、`StopPolicy` 等）+ 采样参数类型 + `ModelLine` trait + vllm 协议解析 | `src/scheduler/mod.rs` 第 31–49 行的大量导入；`src/model_line.rs` 整个文件 |
| `pegainfer-sample` | 逐请求参数采样：`select_batch`、`SampleScratch`、`token_logprob_from_row` | `src/batch_decode.rs` 的两处 `select_batch` 调用 |

外部依赖只需认识三个：`cudarc`（CUDA/cuBLAS/NCCL 的 Rust 绑定）、`half`（`bf16`/`f16` 浮点类型）、`safetensors`（权重文件格式）。其余（`clap`/`serde`/`anyhow`/`tokio`/`rand`/`log`）是常规 Rust 生态件。

一个容易混淆的点：`pegainfer-core` 和 `pegainfer-kernels` **都**提供 GPU 算子，界线是「共享 vs 专属」——core 放所有模型都用的（gemm、norm、embedding），kernels 放与内核构建耦合的模型专属面（hd256 家族、GDN decode 内核）。`src/ops.rs` 里这条线肉眼可见，4.3.3 精读。

#### 4.3.2 核心流程

**feature 转发链**是本 crate 最重要的构建机制：

```text
cargo run --release --features qwen35        （用户命令，CLAUDE.md Build & Run）
        │
        ▼
pegainfer-qwen35/Cargo.toml:  qwen35 = ["pegainfer-kernels/qwen35"]
        │                      （本 crate 的 feature 转发到 kernels）
        ▼
pegainfer-kernels 的 build.rs 触发 Triton AOT：
  用 PEGAINFER_TRITON_PYTHON 选定的 Python 跑 tools/triton/gen_triton_aot.py，
  生成 GDR（Gated Delta Rule）chunkwise prefill 内核
        │
        ▼
本 crate src/lib.rs 第 7 行 #![cfg(feature = "qwen35")] 生效，
模块树开始编译；不开 feature 时整个 crate 编译为空
```

为什么这么设计？`src/lib.rs` 开头的注释（[src/lib.rs:L1-L7](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/lib.rs#L1-L7)）写得很清楚：Qwen3.5 依赖 `pegainfer-kernels/qwen35` 的 Triton AOT 内核，而那需要构建期 Python + Triton；**不开 feature 时本 crate 编译为空 crate，普通 workspace 构建才能保持 Python-free**。

**启动分派链**（`lib.rs` 底部）：`start_engine*` 系列函数是服务器进入本 crate 的正门。`start_engine_with_capacity_policy_and_overlap`（[src/lib.rs:L225-L308](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/lib.rs#L225-L308)）先校验 `max_batch ∈ 1..=64`、SharedSM 重叠的批量上限等约束，然后按设备数二选一：

- 多设备（TP）→ `scheduler::start_tp_with_capacity`（拒绝 `auto` 策略与 decode overlap）；
- 单设备 → `weights::Qwen35Model::from_safetensors` 加载权重，再 `scheduler::start_with_capacity_and_policy` 拉起调度线程。

这条链的细节属于第 4、5 单元；本讲只需要知道「正门长这样」。

#### 4.3.3 源码精读

**依赖清单**。[Cargo.toml:L8-L24](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/Cargo.toml#L8-L24)：5 个 workspace 内依赖 + 外部件，全部 `workspace = true` 继承版本——模型 crate 不自行选版本。注意 `tokio` 只开了 `sync` feature：生产路径的调度器跑在专属 GPU 线程上，用通道同步，不需要异步运行时。

**feature 定义与测试声明**。[Cargo.toml:L35-L37](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/Cargo.toml#L35-L37)：

```toml
[features]
default = []
qwen35 = ["pegainfer-kernels/qwen35"]
```

[Cargo.toml:L42-L69](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/Cargo.toml#L42-L69)：6 个集成测试（`hf_golden_gate`、`e2e_scheduler`、`sampling_behavior`、`chunked_prefill`、`prefix_cache`、`serving_tp2`）和 1 个 bench（`qwen35_ops`）全部声明 `required-features = ["qwen35"]`——不开 feature 连测试目标都不存在，这与 crate 门控一致。

**crate 门控**。[src/lib.rs:L1-L7](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/lib.rs#L1-L7) 的注释 + `#![cfg(feature = "qwen35")]`：

```rust
// The whole crate is gated: Qwen3.5 needs the Triton AOT kernels from
// `pegainfer-kernels/qwen35`, which need Python + Triton at build time.
// Without the feature this compiles to an empty crate so plain workspace
// builds stay Python-free.
#![cfg(feature = "qwen35")]
```

**模块树**。[src/lib.rs:L9-L29](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/lib.rs#L9-L29) 声明全部子模块（`batch_decode`、`config`、`weights`、`scheduler`、`tp_executor` 等），其中只有 `model_line`、`prefill_buffers` 与两个 runtime 出口是对外公开的。两个「出口模块」见 [src/lib.rs:L54-L80](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/lib.rs#L54-L80)：

- `runtime`：低层执行接口（executor、TP 执行器、模型结构），**给模型本地测试、调试、benchmark 用**；根服务器应当用 `start_engine`（注释 L54-L57 明说）。
- `runtime_ops`：只公开 bench 需要的 3 个算子包装。

**算子来源分界**。[src/ops.rs:L1-L29](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/ops.rs#L1-L29) 前 17 行全部 `pegainfer_core::ops::*`（gemm、rms_norm、embedding 等共享算子），第 20–25 行切换到 `pegainfer_kernels::ops::*`（hd256 分页注意力 decode 家族、conv1d、GDN decode）——依赖边界在这一个文件里一览无余：

```rust
pub(crate) use pegainfer_core::ops::gemm;              // 共享：cuBLAS GEMM
...
pub(crate) use pegainfer_kernels::ops::conv1d_decode_batch_into;   // 专属：GDN conv1d
pub(crate) use pegainfer_kernels::ops::paged_attention_batch_decode_hd256_into; // 专属：hd256 注意力
```

而 [src/ffi.rs:L1](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/ffi.rs#L1) 整个文件只有一行 `pub(crate) use pegainfer_core::ffi::*;`——内核 FFI 符号统一从 core 中转，模型 crate 不直接碰 kernels 的 FFI 细节。

**kernels 侧的 feature 面板**。[../pegainfer-kernels/Cargo.toml:L31-L50](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/Cargo.toml#L31-L50)：每个模型线一个 feature（`deepseek-v2-lite`、`gemma4`、`qwen35`、`qwen4`、`glm52`、`k3`、`kimi-k2`），文件注释明确 `qwen35` 对应「Qwen3.5 Triton AOT kernels (GDR chunkwise prefill)——唯一需要构建期 Python + Triton 的 feature」。配合 [model-crate.md:L54-L59](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/docs/models/qwen35/model-crate.md#L54-L59) 的说明：`PEGAINFER_TRITON_PYTHON` 用来选定构建期 Python/Triton 解释器。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：手工画出 `pegainfer-qwen35` 的依赖草图——它指向哪些 workspace crate、每个依赖在代码里的真实用途是什么。这张图是你后续阅读所有源码的「索引页」。

**操作步骤**：

1. 读 [Cargo.toml:L8-L24](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/Cargo.toml#L8-L24)，抄下 5 个 `pegainfer-*` 依赖。
2. 对每个依赖，在 `src/` 里找它的使用点（只读命令，可放心运行）：

   ```bash
   grep -rn "use pegainfer_core" src | head -8
   grep -rn "use pegainfer_kernels" src | head -8
   grep -rn "use pegainfer_kv_cache" src | head -8
   grep -rn "use pegainfer_frontend" src | head -8
   grep -rn "pegainfer_sample::" src | head -8
   ```

3. 把每条 grep 结果里的「被导入项」归类。下面是笔者在本仓库 HEAD（af9e40e1）上运行上述命令得到的真实输出摘录，供你核对：

   ```text
   src/batch_decode_graph.rs:4:  use pegainfer_core::cuda_graph::CudaGraphState;
   src/scheduler/mod.rs:31:      use pegainfer_frontend::engine::Engine;
   src/scheduler/mod.rs:50:      use pegainfer_kv_cache::KvCacheManager;
   src/ops.rs:20:                pub(crate) use pegainfer_kernels::ops::conv1d_decode_batch_into;
   src/batch_decode.rs:68:       pegainfer_sample::select_batch(
   ```

4. 画图。以 `pegainfer-qwen35` 为中心，向外画 5 条边，每条边标注「它给了什么」；再向内画一条边指向 `pegainfer-server`（通过 `ModelLine` 接入，见 `src/model_line.rs` 顶部的 `MODEL_LINE` 静态量，[src/model_line.rs:L19-L21](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/model_line.rs#L19-L21)）。参考骨架：

   ```text
   pegainfer-server ──ModelLine──▶ pegainfer-qwen35
                                        │
     ┌──────────────┬──────────────────┼─────────────────┬──────────────┐
     ▼              ▼                  ▼                 ▼              ▼
   core          kernels          kv-cache          frontend        sample
   共享算子/张量/  hd256+GDN 内核/   分页 KV 池/        Engine 契约/     select_batch/
   CUDA Graph/    Triton AOT 产物    KvView/BlockPool  ModelLine trait  logprob
   weight_loader
   ```

5. 在每条边上补一个「证据行号」（你在第 2 步 grep 到的 `文件:行号`）。

**需要观察的现象**：`pegainfer_frontend` 的导入集中在 `src/scheduler/mod.rs` 与 `src/model_line.rs`；`pegainfer_core` 的导入遍布 `src/ops.rs`、`src/weights/`；`pegainfer_kernels` 的导入数量最少但都指向本行专属内核——三个依赖的使用位置本身就体现了边界。

**预期结果**：一张带证据行号的依赖草图。它应当与 4.3.1 的表格逐行对得上。如果你画完发现某个依赖只有一两处使用，也如实标注——那本身就是边界信息（例如 `kernels` 的导入面窄而深）。

#### 4.3.5 小练习与答案

**练习 1**：想给 Qwen3.5 加一个新的通用 GEMM 辅助函数，应该放进 `pegainfer-core`、`pegainfer-kernels` 还是本 crate？

**答案**：若它对所有模型都有用（纯 cuBLAS 封装、无内核构建耦合），放 `pegainfer-core::ops`；若它依赖本行的内核产物或 hd256/GDN 专属布局，放 `pegainfer-kernels`；若它只服务于 Qwen3.5 的某个执行路径（如 prefill 的分块逻辑），放本 crate 的 `src/ops.rs`。判断口诀：共享进 core、跟内核走进 kernels、只此一家进模型 crate。

**练习 2**：`src/ffi.rs` 为什么只要一行重导出，而不是直接在各文件里 `use pegainfer_kernels::ffi::*`？

**答案**：内核 FFI 符号统一由 `pegainfer-core` 中转（[src/ffi.rs:L1](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/ffi.rs#L1)），模型 crate 通过这一个窄口拿符号，避免各处直接耦合 kernels 的 FFI 布局——kernels 内部怎么组织符号变了，本 crate 不用跟着改。

**练习 3**：`cargo test --release -p pegainfer-qwen35`（不带 feature）会发生什么？

**答案**：什么集成测试都不会跑——6 个 `[[test]]` 目标都声明了 `required-features = ["qwen35"]`（[Cargo.toml:L42-L69](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/Cargo.toml#L42-L69)），且 crate 本体在无 feature 时编译为空。正确命令是 `cargo test --release -p pegainfer-qwen35 --features qwen35 --lib`（CLAUDE.md Tests 一节的写法）。

## 5. 综合实践

**任务：为新同事写一页 Qwen3.5 入职笔记。** 把本讲三块内容串成一份可交付的 Markdown 笔记，包含四个部分：

1. **一张请求链路图**：从 HTTP 请求到 `TokenEvent`，把 `pegainfer-qwen35`、`pegainfer-frontend`、`pegainfer-core/kernels` 放进图里（参照 4.1.2，但要用自己的话重画）。
2. **一张依赖表**：5 个 workspace 依赖 + 一句话职责 + 一个证据行号（来自 4.3.4 的实践成果）。
3. **一段「为什么需要 Python」的解释**：从 `Cargo.toml` 的 `qwen35 = ["pegainfer-kernels/qwen35"]` 出发，解释到 `#![cfg(feature = "qwen35")]` 为止，不超过 5 句话。写完后用它回答一个假想问题：「跑 `cargo run --release`（无 feature）会用到 Python 吗？」（答案：不会——无 feature 时本 crate 是空 crate，Triton AOT 根本不会执行。）
4. **一段「混合架构动机」的算账**：用 4.2 的公式和 27B 的真实几何，写出「若 64 层全是全注意力」与「实际 16 full + 48 linear」两种情况的每 token KV 字节对比（前者 \(64 \times 2 \times 4 \times 256 \times 2 = 262144\) 字节，后者 65536 字节，示例演算），并指出剩下的 48 层状态不随上下文增长。

**检验标准**：笔记里每个事实都有源码链接或文档链接支撑；没有任何「我记得/大概是」的表述。第 3、4 部分的数字你应当能现场重算。

## 6. 本讲小结

- PegaInfer 是纯 Rust + CUDA 的 LLM 推理引擎：workspace 21 个成员、8 个模型 crate，`pegainfer-qwen35` 是其中唯一的「Gated DeltaNet 线性注意力 + 全注意力」混合线，同时服务 Qwen3.5 与 Qwen3.8（后者靠 `model_type` 与文本几何完全一致而共线）。
- 混合架构的动机是显存：full 层的 KV cache 每层每 token 64 KiB（27B 几何，bf16 假设），linear 层状态固定大小；代价是 crate 里同时存在分页 KV 与 recurrent/conv 两套状态系统。
- 整个 crate 被 `#![cfg(feature = "qwen35")]` 门控：无 feature 编译为空 crate，保证默认 workspace 构建 Python-free；feature 经 `qwen35 = ["pegainfer-kernels/qwen35"]` 转发，拉起唯一需要构建期 Python + Triton 的内核构建（GDR chunkwise prefill AOT）。
- 依赖边界一句话：frontend 给契约（Engine/ModelLine）、core 给共享算子与张量、kernels 给本行专属内核、kv-cache 给分页 KV、sample 给逐请求采样；`src/ops.rs` 是这条边界最直观的剖面。
- 本 crate 的公开面极窄：服务器走 `start_engine*` 正门；`runtime`/`runtime_ops` 两个出口只服务本地测试、调试与 bench。

## 7. 下一步学习建议

- **下一讲（u1-l2）**：构建与运行——亲自动手走一遍 `--features qwen35` 的构建链，理解 `PEGAINFER_TRITON_PYTHON`、`PEGAINFER_TEST_MODEL_PATH` 等环境变量与测试入口。本讲 4.3.2 的 feature 转发链在那里会被展开成可执行的命令。
- **再下一讲（u1-l3）**：目录结构与模块地图——把 `src/` 下 20 来个模块逐个归类，本讲的模块树清单（[src/lib.rs:L9-L29](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/lib.rs#L9-L29)）会放大成完整导航。
- **配套阅读**：[../docs/models/qwen35/model-crate.md](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/docs/models/qwen35/model-crate.md)（本 crate 的所有权与边界文档，2 分钟读完）和 [../CLAUDE.md:L21-L30](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/CLAUDE.md#L21-L30)（构建命令）。
- **暂不需要**：读任何 CUDA 内核代码。第 3 单元才会进入内核边界，在那之前先把地图记熟。
