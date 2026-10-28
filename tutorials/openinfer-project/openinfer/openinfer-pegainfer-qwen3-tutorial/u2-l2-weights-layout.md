# 权重布局：Qwen3Model 与内存选项

## 1. 本讲目标

上一讲（u2-l1）我们看清了 `config.json` 如何经过分层校验变成 `Config`。本讲沿着数据流往前走一步：这些配置维度最终变成**哪些显存对象、以什么形状躺在 GPU 上**。学完本讲，你应该能够：

1. 解释 Qwen3 为什么把 Q/K/V 三个投影合成一个矩阵、把 gate/up 两个投影合成一个矩阵，以及前向时如何用**行偏移**在合存矩阵里"切"出各个投影（零额外显存）。
2. 说出 `Qwen3Model` 的所有权设计：它只装"权重 + 配置"，一切请求状态都归 executor——并解释这个划分为什么是 CUDA Graph 与张量并行的前提。
3. 理解 `Qwen3MemoryOptions` 三个参数（`gpu_memory_utilization` / `kv_cache_memory_margin_bytes` / `page_size`）分别如何影响 KV 池的总量、安全余量与块粒度。
4. 手工推导 Qwen3-4B 全部 QKV 与 MLP 权重的 bf16 字节数，并会使用 `src/sizing.rs` 的 checked 乘加函数把推导写成单测。

> 命名提示：大纲里把这组结构称为 AttentionWeights / MlpWeights，源码中的真实名字是 `Attention` 与 `MLP`（见 [src/weights.rs:L100-L120](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights.rs#L100-L120)）。本讲一律使用源码名。

## 2. 前置知识

- **bf16（brain float 16）**：Qwen3 权重与激活的数值类型，每个元素 2 字节。本讲所有字节数都按 \( \text{元素数} \times 2 \) 计算。
- **row-major 矩阵与 GEMM 视角**：本 crate 的权重矩阵是"行 = 输出维、列 = 输入维"的 row-major 布局。一次线性层就是 \( y = W x \)，其中 \( W \in \mathbb{R}^{\text{out} \times \text{in}} \)。把两个线性层的权重**纵向堆叠**（行拼接）后，一次 GEMM 就能同时算出两者的输出——这是"合存"的全部动机。
- **GQA 几何**（u2-l1 已建立）：`q_dim = num_attention_heads × head_dim`，`kv_dim = num_key_value_heads × head_dim`，且 Qwen3-4B 的 `head_dim` 直接写在 config.json 里（128），并非 `hidden_size / num_heads`。
- **tie_word_embeddings**：Qwen3-4B 等小尺寸模型把输出投影（lm_head）与输入词嵌入 `embed_tokens` 共用同一份矩阵，不单独存权重。
- **页式 KV cache**：KV 缓存按"页/块"（page/block）为单位管理，每块容纳 `page_size` 个 token。本讲只关心 `page_size` 的合法值与每块字节数公式；完整的启动期显存画像留给 u2-l4。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [src/weights.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights.rs) | 权重数据布局的"户口本"：`Attention`、`MLP`、`TransformerBlock`、`Qwen3Model`、`Qwen3MemoryOptions`、`KvBudget`，以及 KV 每块字节数的唯一定义点；文件尾部还有 `#[cfg(test)] mod tests` |
| [src/weights/load.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights/load.rs) | 装载侧：把 checkpoint 里三个/两个独立张量经 `fused_rows` 融合成单矩阵（本讲引用，深讲在 u2-l3） |
| [src/sizing.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/sizing.rs) | 全部 23 行：`product` / `sum` 两个 checked 乘加函数，防预算溢出的第一道闸 |
| [src/batch_decode.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/batch_decode.rs) | 消费侧证据：解码前向如何用行偏移 `0 / q_dim / q_dim + kv_dim` 从 `qkv_proj` 里分别读出 Q、K、V |
| [src/batch_decode_dag.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/batch_decode_dag.rs) | 消费侧证据：`mlp_gate_proj` / `mlp_up_proj` 如何用行偏移读 `gate_up_proj` |
| [src/config.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/config.rs) | `Config` 字段与 `local_*` 系列辅助函数（TP 下的本地维度换算，[src/config.rs:L346-L364](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/config.rs#L346-L364)） |

## 4. 核心概念与源码讲解

### 4.1 AttentionWeights（源码名 `Attention`）：QKV 合存矩阵

#### 4.1.1 概念说明

HuggingFace checkpoint 里，每层注意力有三个独立张量：`q_proj.weight`（\( q_{\text{total}} \times \text{hidden} \)）、`k_proj.weight` 与 `v_proj.weight`（各 \( kv_{\text{total}} \times \text{hidden} \)）。推理引擎在服务时每步都要对同一段输入 \( x \)（hidden 维）做三次投影。三次小 GEMM 有三次 kernel 启动开销，且 batch 小的时候 GEMM 形状"瘦长"、算力利用率低。

解法很朴素：装载时把三个矩阵**按行堆叠**成一个 \( (q_{\text{dim}} + 2 \times kv_{\text{dim}}) \times \text{hidden} \) 的大矩阵。之后有两种用法：

- **融合路径**：一次 GEMM 算出整个 \( q_{\text{dim}} + 2kv_{\text{dim}} \) 的输出，再拆成 Q/K/V（u3-l8 的投影融合）；
- **拆分路径**：对同一大矩阵分别以行区间 \( [0, q_{\text{dim}}) \)、\( [q_{\text{dim}}, q_{\text{dim}} + kv_{\text{dim}}) \)、\( [q_{\text{dim}} + kv_{\text{dim}},\ q_{\text{dim}} + 2kv_{\text{dim}}) \) 各做一次 GEMM——这就是"行偏移访问"，不需要拷贝出子矩阵，零额外显存。

注意 `Attention` 里还有 Qwen3 特有的 `q_norm` / `k_norm`（各 `head_dim` 维的 RMSNorm 权重，Qwen3 在 Q、K 上逐头做 norm）以及独立的 `o_proj`。

#### 4.1.2 核心流程

以 TP=1、Qwen3-4B（hidden=2560, heads=32, kv_heads=8, head_dim=128）为例：

1. 装载期（u2-l3 详讲）：三个 safetensors 张量 → `fused_rows` → 一个 \( 6144 \times 2560 \) 的 `DeviceMatrix`（\( 6144 = 4096 + 2 \times 1024 \)）。
2. 运行期拆分路径（数值基线，#75 保留）：
   - Q = GEMM(qkv_proj 的行 \( [0, 4096) \), x)
   - K = GEMM(行 \( [4096, 5120) \), x)
   - V = GEMM(行 \( [5120, 6144) \), x)
3. 运行期融合路径：一次 GEMM 得 \( 6144 \) 维输出，再 `split_qkv` 拆三段。
4. QK norm + RoPE → KV 写入页池 → 注意力 → `o_proj`（独立矩阵，\( \text{hidden} \times q_{\text{dim}} \)）回投影。

行偏移的通用公式：

\[
\text{offset}(Q) = 0,\qquad \text{offset}(K) = q_{\text{dim}},\qquad \text{offset}(V) = q_{\text{dim}} + kv_{\text{dim}}
\]

TP>1 时各部分先在源矩阵的行维上按 `shard_range` 切片再融合，所以每 rank 的 `q_dim`/`kv_dim` 是本地值（见 4.1.3 第一条链接）。

#### 4.1.3 源码精读

**结构体定义**——注释即契约："QKV stored as a single concatenated matrix [q_dim + 2*kv_dim, hidden_size]. Individual projections accessed via row offsets (zero extra memory)"。[src/weights.rs:L100-L111](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights.rs#L100-L111) 定义了 `Attention`：`qkv_proj`（融合矩阵）、`o_proj`、`q_norm`/`k_norm`（head_dim 维），以及记录本地几何的 `q_dim`/`kv_dim` 两个字段——**偏移量不是算出来的，是随权重一起带着的**。

**装载时怎么融合**——[src/weights/load.rs:L152-L157](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights/load.rs#L152-L157) 先从 `Config` 算出全量行数 `q_total = num_attention_heads × head_dim`、`kv_total = num_key_value_heads × head_dim`，再交给 `tensor_parallel.shard_range` 得到每 rank 的 `(row_offset, rows)`；随后 [src/weights/load.rs:L163-L188](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights/load.rs#L163-L188) 用三个 `FusedPart`（q/k/v 各一个，带各自的 `src_rows/row_offset/rows`）调用 `loader.fused_rows` 生成单矩阵。注释点明：`shard_range` 对 world_size == 1 也成立（offset 0、全行），所以一条代码路径同时服务单卡与 TP。

**构造 `Attention`**——[src/weights/load.rs:L277-L295](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights/load.rs#L277-L295) 在装载收尾时把 `q_dim: q_rows`、`kv_dim: kv_rows` 写进结构体——再次强调这两个字段是 TP 切分后的**本地**维度。

**消费侧行偏移**——[src/batch_decode.rs:L325-L372](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/batch_decode.rs#L325-L372) 是解码前向的注意力投影段：`fused_qkv` 为真时一次 `dag.qkv_proj` + `dag.split_qkv`；否则走拆分路径，三次 `dag.gemm_rows` 的偏移参数依次是 `0`（Q）、`q_dim`（K）、`q_dim + kv_dim`（V），与上面公式逐字对应。拆分路径旁的注释"the split path is the numerical baseline retained by #75"说明它是数值对照基线。

#### 4.1.4 代码实践

**实践目标**：不靠运行，纯靠读源码推导出 Qwen3-4B 与 TP=2 两种情况下 `qkv_proj` 的形状与三段行偏移。

1. 操作步骤：
   - 假设 Qwen3-4B：`hidden_size=2560, num_attention_heads=32, num_key_value_heads=8, head_dim=128`（可对照本地 `models/Qwen3-4B/config.json` 核实）。
   - 写出 TP=1 与 TP=2 下的 `q_dim`、`kv_dim`、`qkv_proj` 行数、Q/K/V 三段的 `[起始行, 结束行)`。
   - 用 [src/batch_decode.rs:L346-L372](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/batch_decode.rs#L346-L372) 的三个偏移实参核对你的表。
2. 需要观察的现象：TP=2 时 Q 段与 K/V 段的行数都减半（4096→2048，1024→512），总行数 6144→3072；偏移公式形式不变，只是代入本地值。
3. 预期结果（TP=1）：矩阵 \( 6144 \times 2560 \)；Q ∈ [0,4096)、K ∈ [4096,5120)、V ∈ [5120,6144)。
4. 本实践为源码阅读型，推导结果如需机器验证，可在本地仓库临时打印 `q_dim`/`kv_dim`——运行结果待本地验证（需要 GPU + 权重才能走到装载路径）。

#### 4.1.5 小练习与答案

**练习 1**：为什么把 Q/K/V 融合后"零额外显存"？拆出来的 Q/K/V 又存到哪里？

答案：融合只是把三个 checkpoint 张量的字节拼进**同一个** `DeviceMatrix`，总元素数不变（\( q_{\text{dim}} + 2kv_{\text{dim}} \) 行 × hidden 列）。行偏移访问是对同一块显存按区间做 GEMM，不产生副本。GEMM 的**输出** Q/K/V 是激活缓冲（如 `bufs.q/bufs.k/bufs.v`），属于 executor 的运行时缓冲而非权重，本讲不展开（u3-l3/u3-l4）。

**练习 2**：`o_proj` 为什么不合进 `qkv_proj`？

答案：`o_proj` 的输入是注意力输出（\( q_{\text{dim}} \) 维）而不是 hidden 维的 \( x \)，且它发生在注意力计算**之后**；融合的前提是"多个线性层吃同一段输入、权重形状的第二维相同"。输入不同的投影没法用一次 GEMM 共享，所以 `o_proj` 保持独立矩阵（[src/weights.rs:L106](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights.rs#L106)）。

**练习 3**：TP=2 时 `Attention.q_dim` 字段的值是 4096 还是 2048？依据是哪一行代码？

答案：2048。依据 [src/weights/load.rs:L286-L287](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights/load.rs#L286-L287)：`q_dim: q_rows`、`kv_dim: kv_rows`，而 `q_rows` 来自 `tensor_parallel.shard_range(q_total)`（L155），是本 rank 分到的行数。

### 4.2 MlpWeights（源码名 `MLP`）：gate+up 合存

#### 4.2.1 概念说明

Qwen3 的 MLP 是 SwiGLU：\( y = \text{down}\big(\text{SiLU}(\text{gate}(x)) \odot \text{up}(x)\big) \)。`gate_proj` 与 `up_proj` 都是 \( \text{intermediate} \times \text{hidden} \)，且**吃同一段输入** \( x \)——正好满足融合条件。于是两者按行堆叠成 \( 2 \times \text{intermediate} \times \text{hidden} \) 的 `gate_up_proj`；`down_proj`（\( \text{hidden} \times \text{intermediate} \)）输入是逐元素乘积，保持独立。

这里"行偏移"同样只有两个值：gate 在 \( [0, \text{inter}) \)、up 在 \( [\text{inter}, 2\text{inter}) \)（TP 下用本地 `inter`）。

#### 4.2.2 核心流程

1. 装载期：`gate_proj.weight` + `up_proj.weight` → `fused_rows` → \( (2 \times \text{inter}_{\text{local}}) \times \text{hidden} \) 单矩阵。
2. 运行期：
   - gate_out = GEMM(gate_up_proj 行 \( [0, \text{inter}) \), normed)
   - up_out = GEMM(gate_up_proj 行 \( [\text{inter}, 2\text{inter}) \), normed)
   - act = SiLU(gate_out) ⊙ up_out
   - mlp_out = GEMM(down_proj, act)

Qwen3-4B（inter=9728, hidden=2560，TP=1）：`gate_up_proj` 是 \( 19456 \times 2560 \)，gate 段 [0, 9728)，up 段 [9728, 19456)。

#### 4.2.3 源码精读

**结构体定义**——[src/weights.rs:L113-L120](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights.rs#L113-L120)：`MLP` 只有 `gate_up_proj`（"Fused [gate_proj; up_proj] row-major"）与 `down_proj` 两个字段。

**装载融合**——[src/weights/load.rs:L190-L208](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights/load.rs#L190-L208) 与 QKV 同构：两个 `FusedPart`，`src_rows = inter_total`，行区间来自 `shard_range(inter_total)`（[src/weights/load.rs:L157](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights/load.rs#L157)）。注意 TP 的切片发生在**源矩阵的行（intermediate 维）**上，先切再融合，所以每 rank 的 gate 段仍是本地 inter 行、总行数 \( 2\text{inter}_{\text{local}} \)。

**消费侧行偏移**——[src/batch_decode_dag.rs:L372-L390](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/batch_decode_dag.rs#L372-L390)：`mlp_gate_proj` 调 `gemm_rows(label, weight, 0, out.hidden_dim, ...)`（偏移 0）；`mlp_up_proj` 调 `gemm_rows(label, weight, out.hidden_dim, out.hidden_dim, ...)`（偏移 = 输出自身的 intermediate 维，即本地 inter）。调用点在 [src/batch_decode.rs:L433-L444](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/batch_decode.rs#L433-L444)。

#### 4.2.4 代码实践

**实践目标**：验证"融合不改变总元素数"。

1. 操作步骤：分别写出融合前（gate \( 9728 \times 2560 \) + up \( 9728 \times 2560 \)）与融合后（\( 19456 \times 2560 \)）的元素数；再算 TP=2 时（本地 inter = 4864）融合矩阵的行数。
2. 需要观察的现象：\( 2 \times 9728 \times 2560 = 19456 \times 2560 = 49{,}807{,}360 \) 个元素，两种表述恒等；TP=2 时行数 \( 2 \times 4864 = 9728 \)，恰等于单卡时 gate+up 中**一个**矩阵的行数。
3. 预期结果：数值演算即可完成，无需 GPU；若想看真实形状，可在本地装载后打印 `gate_up_proj.rows/cols`——运行结果待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`down_proj` 为什么不合进 `gate_up_proj`？

答案：与 `o_proj` 同理：`down_proj` 的输入是 SiLU(gate)⊙up 的中间激活（intermediate 维），与 gate/up 的输入（hidden 维的 normed）不同源、形状也不同，无法共享一次 GEMM。

**练习 2**：TP=2、intermediate=9728 时，每 rank 的 `gate_up_proj` 与 `down_proj` 形状是什么？

答案：`gate_up_proj` 为 \( 9728 \times 2560 \)（\( 2 \times 4864 \) 行）；`down_proj` 的行（hidden=2560）不切、**列**按 inter 切成 4864，即 \( 2560 \times 4864 \)——注意 `down_proj` 在 TP 下走的是列切（`loader.col_shard`，[src/weights/load.rs:L242-L256](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights/load.rs#L242-L256)），因为它消费的是被切的 intermediate 维。TP 细节在 u2-l3/u9-l1 展开。

### 4.3 Qwen3Model：权重 + 配置，不含请求状态

#### 4.3.1 概念说明

`Qwen3Model` 是"这台推理发动机的静止部分"：词嵌入、每层权重、最终 norm、RoPE 表、配置、TP 通信子，以及（若启用）打包的 LoRA 注册表。它的文档注释只有一句话——"weights and config only. Request state is owned by the executor."这句话是整个 crate 所有权设计的分水岭：

- **模型侧（本讲）**：装载后基本不可变（唯一例外是 LoRA 热装卸）。权重指针稳定，是 CUDA Graph 捕获/重放的前提（图会把指针"烤"进录制流，u3-l5）。
- **执行器侧（u3 单元）**：每个请求的 KV 页、位置、采样参数、草稿状态都由 executor/调度器持有，随请求生灭。

这样切分的直接收益：模型可以在 TP 下"每 rank 一个、绝不跨线程共享"地被 move 进各自的 worker 线程；服务循环对模型只读，天然无数据竞争。

#### 4.3.2 核心流程

一个 `Qwen3Model` 的字段可以分四组来记：

1. **设备与配置**：`ctx`（CUDA 设备上下文）、`config`（u2-l1 的 `Config`）、`tensor_parallel`、`decode_projection_path`（u3-l8 的融合路径选择结果，装载期定死）。
2. **权重本体**：`embed_tokens`（\( \text{vocab} \times \text{hidden} \)）、`lm_head: Option<DeviceMatrix>`（tied 时为 `None`）、`layers: Vec<TransformerBlock>`（36 层）、`norm`（最终 RMSNorm）。
3. **常量表**：`cos_cache` / `sin_cache`（RoPE 预计算表）、`logits_tail_suppress`（对齐填充的词表尾部抑制 ids）。
4. **服务机制开关与 LoRA**：`enable_cuda_graph`、`tp_comm`（NCCL 通信子）、`lora_adapters` / `packed_lora` / `max_loras` / `max_lora_rank`（u7 详讲）。

其中 `TransformerBlock`（[src/weights.rs:L122-L128](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights.rs#L122-L128)）= 两个 RMSNorm 向量 + 一个 `Attention` + 一个 `MLP`，正是一个解码层的全部静止状态。

#### 4.3.3 源码精读

**结构体与文档注释**——[src/weights.rs:L331-L351](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights.rs#L331-L351)：`Qwen3Model` 全字段如上分组；注意 `lm_head` 是 `Option`。

**Send/Sync 的 SAFETY 说明**——[src/weights.rs:L353-L357](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights.rs#L353-L357)：手动 `unsafe impl Send/Sync`，理由写在注释里——"每个模型实例钉在一个 CUDA 设备上，同一时刻只由一个 worker 线程驱动；TP 路径每 rank 一个模型，从不在多线程间共享同一个 rank 本地模型"。这是 4.3.1 所有权设计的 unsafe 背书。

**tied embeddings 的读取点**——[src/weights.rs:L360-L364](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights.rs#L360-L364)：`output_projection()` 返回 `lm_head.as_ref().unwrap_or(&self.embed_tokens)`——Qwen3-4B（tied）没有独立 lm_head，输出投影就是词嵌入本身。注释还警告：只有 GEMM 调参采样直接用它，正常 logits 必须走 `project_logits`。

**为什么必须走 project_logits**——[src/weights.rs:L366-L384](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights.rs#L366-L384)：`logits_rows` 是把可解码词表向上对齐后的投影行数，多出来的"对齐尾部"不可解码，`project_logits` 在 GEMM 后用 `suppress_logits_bf16_in_place` 把尾部概率压掉。绕过它就会让填充行参与采样——这就是 4.3.2 里 `logits_tail_suppress` 字段的用途。

**TP 维度换算的来源**——`Qwen3Model` 上的一组 `local_*` 方法（如 [src/weights.rs:L394-L412](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights.rs#L394-L412)）只是把 `Config::local_*`（[src/config.rs:L346-L364](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/config.rs#L346-L364)，全部按 `world_size` 整除）转发给调用方，保证"本地几何"只有一个定义点。

#### 4.3.4 代码实践

**实践目标**：在纸面上把"模型 = 静止、执行器 = 动态"这条线画清楚。

1. 操作步骤：通读 [src/weights.rs:L331-L351](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights.rs#L331-L351) 的字段清单，逐个标注"装载后是否再变"；再用 `rg "struct Qwen3Executor" -n src/executor.rs` 找到执行器结构体，列出它持有的请求侧状态（计划缓冲、KV 池引用等）作对照。
2. 需要观察的现象：模型侧在稳态服务中唯一可变的是 LoRA 相关字段（`install/uninstall_lora_adapter`，[src/weights.rs:L447-L494](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights.rs#L447-L494)）——这也解释了 u1-l4 提过的"LoRA 强制关 CUDA Graph"：图烤住的指针会被热装卸改写。
3. 预期结果：得到一张两列对照表（模型字段 vs 执行器状态），为 u3-l1 的 executor 总览做铺垫。本实践无需运行。

#### 4.3.5 小练习与答案

**练习 1**：`Qwen3Model` 为什么不能直接 `derive(Send)`？

答案：字段里有裸的 CUDA 资源（`DeviceContext`、NCCL `Comm` 等），Rust 类型系统无法证明它们跨线程安全，所以由作者手工 `unsafe impl`，并用注释写下成立前提（单设备、单驱动线程、TP 每 rank 一份）。一旦未来有人让两个线程同时驱动一个模型实例，这个 unsafe 前提就被打破——阅读时应当把这段注释当作"使用合同"。

**练习 2**：Qwen3-4B 装载后，`lm_head` 字段里是什么？

答案：`None`。Qwen3-4B `tie_word_embeddings = true`，装载代码（[src/weights/load.rs:L139-L145](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights/load.rs#L139-L145)）只在 untied 时才申请 lm_head 槽位；输出投影经 `output_projection()` 落到 `embed_tokens`。

### 4.4 Qwen3MemoryOptions：显存预算三参数

#### 4.4.1 概念说明

权重装载完只是显存账本的第一行。KV 池能开多大，由 `Qwen3MemoryOptions` 的三个参数决定。它模仿 vLLM 的 `gpu_memory_utilization` 语义：**KV 池拿走"预算内扣除权重、画像出的非 KV 运行时峰值、安全余量"之后的剩余部分**。三个参数各管一件事：

| 参数 | 默认值 | 管什么 |
| --- | --- | --- |
| `gpu_memory_utilization` | 0.90 | 总预算 = \( \text{设备总显存} \times \) 该比例；要求 \( (0, 1] \) |
| `kv_cache_memory_margin_bytes` | 150 MiB | 在画像峰值之外再扣的固定安全垫，覆盖分配器碎片与未画像到的小额漂移 |
| `page_size` | 16 | KV 页粒度（每页容纳的 token 数）；合法值只有 {16, 64}，由 FlashInfer 分页注意力内核决定（见源码引用的 #545） |

定性公式（完整画像流程在 u2-l4）：

\[
\text{kv\_budget} = \underbrace{\text{total} \times u}_{\text{requested}} - \Big( \underbrace{\text{initial\_used}}_{\text{权重等}} + \underbrace{\text{non\_kv\_peak}}_{\text{画像峰值增量}} + \underbrace{m}_{\text{margin}} \Big)
\]

每 KV 块的字节数则有唯一公式（`kv_bytes_per_block`，见下）：

\[
\text{bytes\_per\_block} = \text{num\_layers} \times 2 \times \text{num\_kv\_heads} \times \text{head\_dim} \times \text{block\_size} \times 2\,\text{B}
\]

（因子 2 = K 与 V 各一份；最后一个 2 = bf16。）`page_size` 越大，块越"粗"，前缀匹配的粒度越粗（u5-l1），但每块字节数成比例增大；它不改变总预算，只改变切分粒度与最小可分配单位。

#### 4.4.2 核心流程

1. `Qwen3MemoryOptions::new(u, margin, page)` 构造（const fn），`Default` 给 `(0.90, 150 MiB, 16)`。
2. 使用前必须 `validate()`：\( u \in (0, 1] \)，`page_size ∈ {16, 64}`，否则立即报错。
3. 启动画像（`profiled_kv_budget`，u2-l4 详讲）先 `validate()`，再以 `page_size` 构造 KV 几何、按上式算每块字节，最后从预算里数出块数（不足 64 块的最低限直接拒绝启动）。
4. `page_size` 同时决定调度器的 token 粒度：一个请求占用的 KV 页数 = \( \lceil \text{len} / \text{page\_size} \rceil \)。

#### 4.4.3 源码精读

**默认常量**——[src/weights.rs:L31-L36](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights.rs#L31-L36)：`DEFAULT_GPU_MEMORY_UTILIZATION = 0.90`、`DEFAULT_KV_CACHE_MEMORY_MARGIN_BYTES = 150 MiB`、`DEFAULT_KV_PAGE_SIZE = 16`，以及 `VALID_KV_PAGE_SIZES = &[16, 64]`——注释点明这来自 FlashInfer 分页注意力内核的接受集（issue #545）。

**结构体与字段注释**——[src/weights.rs:L38-L50](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights.rs#L38-L50)：`gpu_memory_utilization` 的注释就是那句"KV 拿预算内剩余"的语义；`kv_cache_memory_margin_bytes` 说明是"画像结果之外的额外扣留，覆盖分配器碎片与小幅未画像漂移"；`page_size` 注释指向 `VALID_KV_PAGE_SIZES` 与 CLI 旗标 `--kv-page-size`。

**校验**——[src/weights.rs:L65-L78](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights.rs#L65-L78)：两条 `anyhow::ensure!`，错误信息分别带上非法的 utilization 数值与完整合法页尺寸列表——"失败越早越便宜"（u2-l1 的同一信条在内存选项上的落点）。

**Default**——[src/weights.rs:L81-L89](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights.rs#L81-L89)：三个默认常量的组装点。

**每块字节数的唯一公式Owner**——[src/weights.rs:L823-L861](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights.rs#L823-L861)：`kv_page_bytes` 的文档写着"The Single owner of this formula — callers that bill KV pages must not re-derive it"（凡给 KV 页记账的调用方不得自行推导）；`kv_bytes_per_block` 在构造 layout **之前**先用 `crate::sizing::product` 对全乘积做 checked 计算，注释解释：`KvLayout::new` 内部用普通乘法推导步长，而这里的全乘积支配一切部分乘积，先清零它等于全部清零。`debug_assert_eq!` 再对照 layout 的 `page_stride` 自证一致。

**最低块数**——[src/weights.rs:L863-L864](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights.rs#L863-L864)：`MIN_KV_BLOCKS = 64`，"调度器能推进的最小池"；预算数不出 64 块时启动失败（[src/weights.rs:L893-L906](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights.rs#L893-L906)，且按"含 DFlash 摊派的 effective 字节"判，见该处注释）。

**已有单测**——[src/weights.rs:L1143-L1154](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights.rs#L1143-L1154)：`memory_options_rejects_invalid_page_sizes` 遍历 0/1/15/17/32/63/65/128 断言全部被拒且错误信息提到 `page_size`——这就是 4.4.4 实践要跑的测试。

#### 4.4.4 代码实践

**实践目标**：跑通一条不依赖 GPU 的单测，并亲手代入每块字节数公式。

1. 操作步骤：
   - 运行 `cargo test --release -p pegainfer-qwen3 --lib memory_options`，应看到 `memory_options_rejects_invalid_page_sizes` 通过。
   - 手工代入 Qwen3-4B、TP=1、page_size=16：\( 36 \times 2 \times 8 \times 128 \times 16 \times 2\,\text{B} = 2{,}359{,}296\,\text{B} = 2.25\,\text{MiB/块} \)。
   - 再算 page_size=64 时的每块字节（应为 9 MiB），并算 24 GB 卡、utilization=0.90、暂不计权重时最多能容纳多少个 16-token 块（量级估算即可）。
2. 需要观察的现象：lib 单测在无 GPU 环境也能编译并通过（它只做整数与字符串断言）；`--lib` 过滤器只命中一个测试。
3. 预期结果：每块 2.25 MiB（page 16）/ 9 MiB（page 64）；24 GB × 0.9 ≈ 21.6 GB 的量级里若全给 KV（现实中要先扣权重，此处仅为公式练习）约可容纳 ~9100 个 16-token 块。单测运行结果待本地验证（取决于本地工具链）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `page_size` 只允许 16 或 64？

答案：KV 池的分页注意力内核来自 FlashInfer，其内核只接受这两个页尺寸（源码注释指向 #545）。选 64 时每块覆盖更多 token、页表更短，但小请求的尾部浪费更大；选 16 更细但页表项更多。这是内核能力约束，不是任意调参。

**练习 2**：把 `gpu_memory_utilization` 设成 1.0 合法吗？设成 0.0 呢？

答案：1.0 合法（区间是半开的 \( (0, 1] \)），但画像若发现非 KV 占用已经吃满预算会拒绝启动；0.0 非法，`validate()` 的第一条 ensure 直接拒绝——预算为零意味着连 64 块的最小池都不可能（[src/weights.rs:L66-L70](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights.rs#L66-L70)）。

**练习 3**：`kv_cache_memory_margin_bytes` 与 `gpu_memory_utilization` 都是"留余量"，区别是什么？

答案：utilization 在**总量**层面封顶（比例），margin 在**分配**层面扣减（固定字节，盖碎片与未画像漂移）。前者决定池子最大多大，后者决定画像峰值之外还要再让出多少才敢把剩余全部划给 KV——两者叠加后才是 `kv_budget_bytes`。

## 5. 综合实践

**任务：手工推导 Qwen3-4B 的 QKV + MLP 权重字节数，并用 `sizing.rs` 的 checked 乘加写成单测。**

这是本讲的收官实践，把 4.1/4.2 的形状、4.3 的所有权、4.4 的预算意识串成一条线：**知道权重占多少字节，才能看懂 u2-l4 里"预算扣掉权重之后剩多少给 KV"的账本。**

### 5.1 手工推导（Qwen3-4B，TP=1，bf16）

几何：`hidden=2560, layers=36, heads=32, kv_heads=8, head_dim=128, intermediate=9728`，故 \( q_{\text{dim}} = 32 \times 128 = 4096 \)，\( kv_{\text{dim}} = 8 \times 128 = 1024 \)。

| 矩阵 | 形状 | 元素数 | bf16 字节 | 每层 |
| --- | --- | --- | --- | --- |
| `qkv_proj` | \( 6144 \times 2560 \) | 15,728,640 | 31,457,280 B | **30 MiB** |
| `gate_up_proj` | \( 19456 \times 2560 \) | 49,807,360 | 99,614,720 B | **95 MiB** |
| `down_proj` | \( 2560 \times 9728 \) | 24,903,680 | 49,807,360 B | **47.5 MiB** |

- 每层 QKV+MLP 合计：\( 30 + 95 + 47.5 = 172.5 \) MiB。
- 36 层合计：\( 172.5 \times 36 = 6210 \) MiB ≈ 6.06 GiB（精确值 6,511,656,960 字节）。
- （延伸）`o_proj` \( 2560 \times 4096 \) 每层 20 MiB，36 层 720 MiB；`embed_tokens`（tied，兼作输出投影）\( 151936 \times 2560 \times 2 \approx 741.6 \) MiB。全模型约 4.02B 参数 ≈ 7.5 GiB——与"Qwen3-4B"的名字对上。

### 5.2 写成单测

先看工具：[src/sizing.rs:L1-L22](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/sizing.rs#L1-L22) 只有两个函数——`product`（`checked_mul` 折叠，溢出报 "sizing overflow in product"）与 `sum`（`checked_add`）。模块注释说明了它存在的意义：**预算在配置维度通过张量校验之前就要计算，坏配置必须在这里响亮失败，而不是回绕成少预留**。这正是 u2-l4 显存画像所有乘法的地基。

由于 `product`/`sum` 是 `pub(crate)`（同理 `Attention`/`MLP`/`Qwen3Model` 也只在 crate 内可见，u1-l1 已建立"runtime 再导出才是公共边界"的认知），这个练习测试**必须写进 crate 内部**——在**你自己的工作副本**里把它加到 [src/weights.rs:L959](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights.rs#L959) 之后的 `#[cfg(test)] mod tests` 中（学习练习，勿提交）：

```rust
// 示例代码：加入 src/weights.rs 的 tests 模块（学习用，不改动生产逻辑）
use crate::sizing::{product, sum};

const HIDDEN: usize = 2560;
const LAYERS: usize = 36;
const Q_DIM: usize = 32 * 128;      // 4096
const KV_DIM: usize = 8 * 128;      // 1024
const INTER: usize = 9728;
const BF16: usize = std::mem::size_of::<half::bf16>();

#[test]
fn qwen3_4b_qkv_and_mlp_weight_bytes() {
    let qkv = product(&[Q_DIM + 2 * KV_DIM, HIDDEN, BF16])?;
    let gate_up = product(&[2 * INTER, HIDDEN, BF16])?;
    let down = product(&[HIDDEN, INTER, BF16])?;
    let per_layer = sum(&[qkv, gate_up, down])?;
    let all_layers = product(&[per_layer, LAYERS])?;

    assert_eq!(qkv, 31_457_280);          // 30 MiB
    assert_eq!(gate_up, 99_614_720);      // 95 MiB
    assert_eq!(down, 49_807_360);         // 47.5 MiB
    assert_eq!(all_layers, 6_511_656_960); // ≈6.06 GiB
}
```

> 注：`product`/`sum` 返回 `Result<usize>`，测试里用 `?` 需要 conclude 返回 `Result`，或改用 `.expect("sane dims")`——推荐后者，与本文件既有测试的 `expect` 风格一致。

### 5.3 对照与验证

1. 与 `weights.rs` 已有测试对照组织方式：[src/weights.rs:L969-L1003](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights.rs#L969-L1003) 的 KV 预算测试同样用字面常量构造几何（36 层/8 头/128 维/16 块）、用 `expect_err` + `contains("overflow")` 断言失败形态——你的字节数测试与它们同处一个模块、共享同一风格。
2. 与真实 checkpoint 对照：拿本地 `models/Qwen3-4B/config.json` 的五个维度代入，若与 5.1 的表完全一致即通过。可再用 `ls -l` 对比分片 safetensors 的总大小（含 embed 与 o_proj，约 7.5 GiB + 元数据）。
3. 运行：`cargo test --release -p pegainfer-qwen3 --lib weights::tests::qwen3_4b`。该测试只做整数运算，无需 GPU；通过与否取决于你手工推导的数字——本文的推导已逐步列出，运行结果待本地验证。

## 6. 本讲小结

- `Attention` 把 Q/K/V 按行堆成单矩阵 \( (q_{\text{dim}} + 2kv_{\text{dim}}) \times \text{hidden} \)，前向用行偏移 \( 0 / q_{\text{dim}} / q_{\text{dim}}+kv_{\text{dim}} \) 读取（[src/batch_decode.rs:L346-L372](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/batch_decode.rs#L346-L372)），融合只是拼字节，零额外显存。
- `MLP` 同理把 gate+up 合成 \( 2\text{inter} \times \text{hidden} \)，偏移 0 与 inter；`o_proj`/`down_proj` 因输入不同源而保持独立。
- `Qwen3Model` = 权重 + 配置 + 常量表（RoPE/norm）+ 机制开关，**不含任何请求状态**；请求状态归 executor，这使稳态服务对模型只读，是 CUDA Graph 指针稳定与 TP 每 rank 一份的前提。
- `Qwen3MemoryOptions` 三参数分工：utilization 定总预算、margin 定画像外安全垫、page_size 定 KV 块粒度（只允许 16/64，FlashInfer 内核约束）。
- 每块 KV 字节数有唯一公式Owner `kv_bytes_per_block`，且一切尺寸乘法必须走 [src/sizing.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/sizing.rs) 的 checked 乘加——坏配置要响亮失败，不许回绕成少预留。
- Qwen3-4B 的 QKV+MLP 权重共约 6210 MiB（每层 172.5 MiB：30 + 95 + 47.5），全模型约 7.5 GiB——这是 u2-l4 显存账本里"权重"那一行的数值。

## 7. 下一步学习建议

本讲看到的是"静止的模型"。下一讲 **u2-l3（checkpoint 装载）** 讲这堆矩阵是怎么从 safetensors 分片走进显存的：`load_shard_info` 的分片探测、`StagedWeightLoader` 的"先记录整个计划再上传"两阶段结构、以及 TP>1 时 `shard_range`/`col_shard` 的行切与列切边界。之后 **u2-l4（显存画像与 KV 预算）** 会把本讲 4.4 的定性公式展开成 `profiled_kv_budget` 的完整账本：画像临时块、激活峰值测量、`servable_len` 折算。建议顺带重读 [src/weights.rs:L692-L818](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights.rs#L692-L818)，此时你已能认出其中每一个字段。
