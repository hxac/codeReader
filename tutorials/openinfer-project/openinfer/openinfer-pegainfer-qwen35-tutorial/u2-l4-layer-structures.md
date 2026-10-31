# 层结构：TransformerBlock35 与两种注意力层

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 `config.layer_types[i]` 如何在**加载期**决定一个 decoder 层装配成 `FullAttentionLayer` 还是 `LinearAttentionLayer`，以及在**前向期** `LayerKind` 枚举如何驱动逐层分发。
2. 逐一说出 `FullAttentionLayer` 与 `LinearAttentionLayer` 各自持有的权重张量、它们的形状、以及哪些权重在 TP 下被切分、哪些被复制。
3. 沿 `prefill_layer`（prefill 路径）与 `batch_decode_kernels_graph`（decode 路径）定位 norm / RoPE / 投影 / 门控 / 残差的精确调用顺序，并写出每一步调用的 `ops::` 函数名。
4. 理解一个贯穿全讲的结构性事实：**层结构体里只有权重，没有运行时状态**——全注意力的 KV 在模型级共享分页池里，线性注意力的递推状态在调度器侧的 `RecurrentState` 里。

## 2. 前置知识

本讲默认你已读完 u2-l1（配置校验）、u2-l2（TP 几何）、u2-l3（权重加载管线）。再用三段话补齐直觉：

**混合架构回顾**。Qwen3.5 不是「一种注意力 + 32 个相同层」，而是两种层 interleaved 排布（4B 为 8 层全注意力 + 24 层线性注意力）。全注意力层把历史存进 KV cache，显存随上下文线性增长；线性注意力层（Gated DeltaNet，下称 GDN）把历史压缩进一个**固定大小**的递推状态矩阵，显存不随上下文变化。`config.json` 里的 `layer_types` 数组逐层声明这一点，u2-l1 已看到它在加载期被升格为 `LayerType` 二值枚举。

**权重与状态的分离**。本讲的所有「层结构体」（`TransformerBlock35` 等）只装**不可变权重**。可变状态全部在层结构体之外：全注意力层的 K/V 写进 `Qwen35Model` 持有的分页 KV 池（`kv_buffer`），线性注意力层的递推状态/卷积状态由调度器侧的 `RecurrentState` 持有。这个分离是 CUDA Graph 指针稳定性和前缀缓存能工作的前提（后续单元展开）。

**RMSNorm 的 (1+w) 变体**。Qwen3.5 的所有 norm 都是 RMSNorm 的 offset 变体：输出乘以 `(1 + w)` 而不是 `w`，因此代码里到处是 `rms_norm_*_offset_into`（见 [src/prefill.rs:253-255](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L253-L255) 的注释「We need the (1+w) variant」）。 checkpoint 里存的是 `w` 本身，偏移在内核里加。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| [src/weights/layers.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L1-L509) | 本讲主战场：`TransformerBlock35` / `FullAttentionLayer` / `LinearAttentionLayer` / `LayerKind` / `MLP35` 的定义与加载器，外加 `WeightSource` 的 TP 分片描述 |
| [src/weights.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L1-L602) | `Qwen35Model` 组装：逐层加载循环、KV 池按全注意力层数开池、`output_logits_into` |
| [src/prefill.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L236-L564) | prefill 前向：`prefill_layer` 五步骨架 + 两种注意力的 prefill 实现 |
| [src/batch_decode.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L632-L906) | decode 前向：图捕获用的逐层循环 + 两种注意力的 decode 实现 |
| [src/ops.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/ops.rs#L1-L29) | 算子重导出面：`pegainfer-core` 通用算子 + `pegainfer-kernels` 专属算子 + 本 crate 封装，画数据流图时的「函数名字典」 |
| [src/config/model.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/model.rs#L17-L20) | `LayerType` 枚举与各维度派生函数 |

## 4. 核心概念与源码讲解

### 4.1 层分发：从 `config.layer_types[i]` 到 `LayerKind`

#### 4.1.1 概念说明

`TransformerBlock35` 是「一个 decoder 层」的容器。它把层内成员分成两类：

- **两种层共有的**：`input_layernorm`、`post_attention_layernorm`、`mlp`——不管注意力是哪种，这两个 norm 和 MLP 的权重形状与用法完全一致；
- **因层而异的**：`attn: LayerKind`，一个双变体枚举，装着 `FullAttentionLayer` 或 `LinearAttentionLayer`。

分发的源头是配置：u2-l1 里 `layer_types: ["linear_attention", ...]` 字符串数组被校验层升格为 `LayerType` 枚举（[src/config/model.rs:17-20](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/model.rs#L17-L20)）：

```rust
pub(crate) enum LayerType {
    FullAttention,
    LinearAttention,
}
```

于是「第 i 层是什么」在模型加载时就固定下来，前向期只需 `match`。

#### 4.1.2 核心流程

加载期分发（weights.rs → layers.rs）：

```
for i in 0..num_hidden_layers:
    prefix = "model.language_model.layers.{i}"
    layer_type = config.layer_types[i]          # 加载期已知
    TransformerBlock35::load(src, prefix, layer_type)
        ├─ FullAttention   → FullAttentionLayer::load(src, "{prefix}.self_attn")
        ├─ LinearAttention → LinearAttentionLayer::load(src, "{prefix}.linear_attn")
        ├─ input_layernorm  ← "{prefix}.input_layernorm.weight"        # 共有
        ├─ post_attention_layernorm ← "{prefix}.post_attention_layernorm.weight" # 共有
        └─ MLP35::load(src, "{prefix}")                                # 共有
```

前向期分发（prefill 与 decode 两个路径同构）：

```
linear_idx = 0; full_idx = 0
for layer in layers:
    normed = rms_norm(hidden, layer.input_layernorm)
    match layer.attn:
        FullAttention(attn)   → 全注意力内核族; full_idx += 1
        LinearAttention(attn) → GDN 内核族;     linear_idx += 1
    hidden_mid = hidden + attn_out
    normed = rms_norm(hidden_mid, layer.post_attention_layernorm)
    hidden = hidden_mid + mlp(normed)
```

注意两个独立计数器：`full_idx` 索引**共享 KV 池里的层号**（池只按全注意力层数开），`linear_idx` 索引**递推状态数组里的层号**。这正是「权重在层结构体里、状态在层外」的直接后果。

#### 4.1.3 源码精读

层容器与枚举。`TransformerBlock35` 的四个字段就是上面流程图的具象（[src/weights/layers.rs:103-109](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L103-L109)）：

```rust
pub(crate) struct TransformerBlock35 {
    pub(crate) input_layernorm: DeviceVec,
    pub(crate) attn: LayerKind,
    pub(crate) post_attention_layernorm: DeviceVec,
    pub(crate) mlp: MLP35,
}
```

`TransformerBlock35::load` 按 `LayerType` 二选一装配注意力子层，共用同一个 `prefix` 下的 norm 与 MLP 权重名（[src/weights/layers.rs:114-132](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L114-L132)）。关键差异是 checkpoint 张量前缀：全注意力层在 `self_attn` 下，线性注意力层在 `linear_attn` 下——HF checkpoint 就是这么命名的，加载器原样对齐。

`LayerKind` 除了是分发枚举，还提供两个类型收窄的 `Option` 访问器（[src/weights/layers.rs:63-77](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L63-L77)）：`full_attention()` / `linear_attention()`。它们让「按类型过滤层」的代码不必写嵌套 match，例如 GEMM 调优时从所有全注意力层采样 `q_proj`、从所有线性层采样 `in_proj_qkvz`（[src/weights.rs:490-509](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L490-L509)）。

组装循环在 weights.rs：逐层取 `config.layer_types[i]` 传入 `load`（[src/weights.rs:221-232](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L221-L232)）。加载完成后，KV 池按 `num_full_attention_layers()`（对 `layer_types` 计数得出，[src/config/model.rs:131-136](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/model.rs#L131-L136)）开池（[src/weights.rs:261-269](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L261-L269)）——24 个线性层在这座池里没有座位，这是混合架构最直观的显存结构。

前向分发在两条路径上各出现一次，形态相同：

- prefill 路径：`match &layer.attn` 分派到 `prefill_full_attention` / `prefill_linear_attention`（[src/prefill.rs:261-284](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L261-L284)），match 之前还用引用匹配先算出注意力输出维度（`local_full_attn_q_dim` vs `local_linear_z_dim`）；
- decode 路径（CUDA Graph 捕获用）：同样的 match，全注意力走批量 paged 内核并 `full_idx += 1`，线性注意力走按槽位指针表的 GDN 内核并 `linear_idx += 1`（[src/batch_decode.rs:653-684](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L653-L684)）。

顺带看清 MLP 的形状约定（两种层共享，[src/weights/layers.rs:86-101](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L86-L101)）：checkpoint 里 `gate_proj` 与 `up_proj` 是两个矩阵，加载时 `vstack` 成一个 `gate_up_proj`，前向一次 GEMM 出 `[gate | up]` 两段，再用 `silu_mul_fused_batch_into` 做门控乘法——这与 u2-l3 讲过的「装载期融合」是同一手法。

#### 4.1.4 代码实践

**实践目标**：不运行任何代码，纯源码追踪，验证「加载期分发」与「前向期分发」用的是同一个事实来源 `config.layer_types`，并数清双计数器的含义。

**操作步骤**：

1. 打开 [src/weights.rs:221-232](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L221-L232)，确认 `layer_type` 来自 `config.layer_types[i]`，且 `prefix` 形如 `model.language_model.layers.{i}`。
2. 打开 [src/weights/layers.rs:114-124](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L114-L124)，记下两种 `LayerType` 分别映射到哪个 checkpoint 子前缀（`self_attn` / `linear_attn`）。
3. 用 `Grep` 在 `src/` 里搜 `linear_idx`，列出全部出现文件，按 prefill / batch_decode / 其他归类。
4. 假设某 checkpoint 的 `layer_types` 为 `["full_attention", "linear_attention", "linear_attention"]`，手工填表：每行的 `i`、加载前缀、前向时 `full_idx`/`linear_idx` 在进入该层时的值。

**需要观察的现象**：`linear_idx` 只出现在 prefill.rs 与 batch_decode.rs 的前向代码（以及状态管理模块），从不出现在加载代码里；加载循环根本没有索引变量——`Vec<TransformerBlock35>` 的下标就是层号。

**预期结果**：第 4 步的表格应为——i=0（full，进入时 full_idx=0, linear_idx=0，离开时 full_idx=1）；i=1（linear，进入时 full_idx=1, linear_idx=0，离开时 linear_idx=1）；i=2（linear，进入时 linear_idx=1，离开时 linear_idx=2）。KV 池层数为 1，递推状态层数为 2。

如果本地有 Qwen3.5-4B 权重目录，可用 `python3 -c "import json;c=json.load(open('models/Qwen3.5-4B/config.json'));print(c['text_config']['layer_types'])"` 直接对照真实排布（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `LayerKind` 不用 trait 对象（`Box<dyn AttentionLayer>`）而用枚举？
**答案**：枚举是封闭集合且大小静态已知：两种变体的权重形状完全不同（`FullAttentionLayer` 全是 `DeviceMatrix/DeviceVec`，`LinearAttentionLayer` 还有 `CudaSlice<f32>`），枚举让 `match` 成为穷尽性检查——新增层类型时编译器强制更新所有分派点；同时避免虚函数表间接调用，字段可以直接按 TP 分片规则逐个装载，加载代码（`TransformerBlock35::load`）与分派代码共享同一类型信息。

**练习 2**：`full_idx` 与 `linear_idx` 为什么不能合并成一个 `layer_idx`？
**答案**：因为两套状态系统各自独立编号。KV 池只为全注意力层开（`num_full_layers` 层），第 3 个全注意力层在池里的层号是 2，即使它在模型里是第 7 层；递推状态数组只为线性层开，编号同理。`layer_idx`（模型层号）只用于日志和错误上下文（如 batch_decode.rs 混合路径的 `with_context`），不用于索引状态。

**练习 3**：`input_layernorm` 与 `post_attention_layernorm` 在两种层上的形状和用法有区别吗？
**答案**：没有。二者都是 `[hidden_size]` 的 `DeviceVec`，从同一组 checkpoint 名字加载，在全注意力层与线性层上以完全相同的 `rms_norm_batch_offset_into` 调用使用（对比 [src/prefill.rs:257](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L257) 与 [src/prefill.rs:291](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L291)，与层类型无关）。这就是它们被放进 `TransformerBlock35` 而不是两个注意力层结构体的原因。

### 4.2 全注意力层：gated Q、QK-norm、partial RoPE 与 paged KV

#### 4.2.1 概念说明

`FullAttentionLayer` 是「标准」Transformer 注意力加三个 Qwen3.5 特有的 twists：

1. **gated attention**：`q_proj` 的输出不只含 Q，还含每个 head 的一个**门控标量带**。HF/PegaInfer 的内核把 `q_proj` 的行解释为「每个 head 先 `head_dim` 行 Q、再 `head_dim` 行 gate」的 per-head 交错块（注释见 [src/weights/layers.rs:417-420](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L417-L420)）。注意力算完后用 gate 做 sigmoid 乘法。
2. **QK-norm**：Q 和 K 在 RoPE 之前各过一次 head 共享的 RMSNorm（`q_norm`/`k_norm` 都是 `[head_dim]`，广播到所有 head）。
3. **partial RoPE**：只旋转每头前 `rotary_dim = head_dim × partial_rotary_factor` 维（u2-l1 已推导），cos/sin 表在模型加载时预计算（[src/weights.rs:240-248](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L240-L248)），属于模型而非层。

维度派生（[src/config/model.rs:139-146](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/model.rs#L139-L146)）：`full_attn_q_dim = num_attention_heads × head_dim`，`full_attn_kv_dim = num_key_value_heads × head_dim`。注意 `q_proj` 矩阵的行数是这个的两倍（Q + gate）。GQA：`num_key_value_heads < num_attention_heads` 时多头共享 KV，组比率是 TP 不变量（u2-l2）。

#### 4.2.2 核心流程

prefill 路径（[src/prefill.rs:305-446](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L305-L446)）：

```
输入: normed_batch [hidden, seq]
1. q_full = gemm(q_proj, normed)        # [heads*head_dim*2, seq]（Q 与 gate 交错）
   k     = gemm(k_proj, normed)         # [kv_heads*head_dim, seq]
   v     = gemm(v_proj, normed)         # 同上
2. 融合预备内核（一个 CUDA kernel 做三件事）:
   a. Q/K 各自 RMSNorm（q_norm/k_norm）
   b. Q/K 前 rotary_dim 维做 partial RoPE（查 cos/sin 表）
   c. K/V 直接写进分页 KV 池（按 page_indices 定位页）
   输出 q_prepped
3. 批量 paged attention: attn_out = softmax(QK^T/√head_dim)·V
4. gate 内核: attn_out *= sigmoid(gate 带来自 q_full)
5. out = gemm(o_proj, attn_out) + TP all-reduce
```

decode 路径（[src/batch_decode.rs:112-214](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L112-L214)）结构相同，差异有二：第 2 步换成 decode 专用融合内核 `qk_norm_partial_rope_batched_decode_hd256_into`——它只做 QK-norm 与 partial RoPE（Q 从交错 `q_full` 取出写入 `q`，K 原地处理），**不碰 KV 池**；落页藏在第 3 步的包装函数里：`paged_attention_batch_decode_hd256_into` 先调 `scatter_decode_kv_into_paged` 把本步 K/V 写入池，再发射注意力内核（[pegainfer-kernels/src/ops/qwen35.rs:205-219](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ops/qwen35.rs#L205-L219)）。所以两条路径的「谁写池」位置不同：prefill 在 prep 内核、decode 在 attention 包装的开头。第 3 步内核本身二选一：批量小且 GQA 组比率在编译清单内（u2-l2 的 P2c 门）走 split-KV 两阶段归约内核（`paged_attention_batch_decode_split_hd256_into`），否则走单内核。

#### 4.2.3 源码精读

权重与形状声明（[src/weights/layers.rs:15-28](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L15-L28)）：

```rust
pub(crate) struct FullAttentionLayer {
    /// Q projection including gate: [num_heads * head_dim * 2, hidden_size]
    pub(crate) q_proj: DeviceMatrix,
    /// K projection: [num_kv_heads * head_dim, hidden_size]
    pub(crate) k_proj: DeviceMatrix,
    /// V projection: [num_kv_heads * head_dim, hidden_size]
    pub(crate) v_proj: DeviceMatrix,
    /// Output projection: [hidden_size, num_heads * head_dim]
    pub(crate) o_proj: DeviceMatrix,
    /// QK norm weights: [head_dim] (broadcast to all heads)
    pub(crate) q_norm: DeviceVec,
    pub(crate) k_norm: DeviceVec,
}
```

加载器逐个标注了 TP 分片方式（[src/weights/layers.rs:135-146](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L135-L146)）：

- `q_proj` 用**专用**的 `gated_q_proj`：切分必须按 head 整块切，保持每个 head 的 `[q 行 | gate 行]` 相邻，不能对融合后的行做平坦对切（实现见 [src/weights/layers.rs:421-431](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L421-L431)，单测 [src/weights/layers.rs:493-508](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L493-L508) 验证了 rank1 从自己第一个 head 的 Q 行起步）；
- `k_proj`/`v_proj` 按 `kv_rows`（KV 头维行切），`o_proj` 按 `q_cols`（列切，配合 attention 输出的行切，attention 输出本 rank 只产本 rank 头的结果，列切 o_proj 后各 rank 出部分 hidden 和，再 all-reduce）；
- `q_norm`/`k_norm` 是 `[head_dim]` 的 per-head 广播权重，**不切分**，每个 rank 整取。

前向三步中最值得精读的是那个「一个内核做三件事」的融合预备调用（[src/prefill.rs:337-373](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L337-L373)）：`ffi::prefill_attention_hd256_prep_paged_cuda` 一次吃进 `q_full/k/v` 原始投影、`q_norm/k_norm`、`cos/sin` 表和分页池指针，吐出 norm+RoPE 后的 `q_prepped` 并把 K/V 落页。函数名里的 `hd256` 就是 u2-l1 内核门的 head_dim=256 约定。第 3 步批量 paged attention 用 `ffi::batch_prefill_paged_cuda_hd256`（[src/prefill.rs:375-423](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L375-L423)），sm_scale 为 \( 1/\sqrt{256} \)。第 4 步 gate 应用是独立小内核 `ffi::attention_gate_batch_hd256_cuda`（[src/prefill.rs:425-438](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L425-L438)）。最后 `o_proj` GEMM 后调 `all_reduce_hidden`（[src/prefill.rs:442-445](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L442-L445)）——单 GPU 时它是 no-op，TP 时做 NCCL sum。

decode 版内核选择逻辑在 [src/batch_decode.rs:126-192](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L126-L192)：先三个 `gemm_into`，再融合 `qk_norm_partial_rope_batched_decode_hd256_into`，然后按 `SPLIT_DECODE_MAX_BATCH` 与编译期 GQA 组大小二选一 paged 内核，gate 与 o_proj 收尾（[src/batch_decode.rs:194-213](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L194-L213)）。这些函数名都能在算子重导出面 [src/ops.rs:22-25](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/ops.rs#L22-L25) 里查到来源。

#### 4.2.4 代码实践

**实践目标**：把 prefill 全注意力的「投影 → 融合预备 → 注意力 → gate → 输出投影」五步整理成「张量形状 + 全局副作用」清单，弄清哪一步动了 KV 池。

**操作步骤**：

1. 读 [src/prefill.rs:320-322](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L320-L322) 的三个 GEMM，写下 `q_full/k/v` 的行列数（用 `HiddenStates` 的 `[hidden_dim, seq_len]` 约定）。
2. 读 [src/prefill.rs:337-373](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L337-L373)，在参数列表里逐个标出：哪些是输入指针、哪些是输出指针、哪个参数是 KV 池缓冲、哪些是池索引（`page_indices`）。
3. 读 [src/prefill.rs:425-438](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L425-L438)，确认 gate 内核的输入 `q_full` 早在第 1 步就产出——也就是说 gate 带与 Q 共享一次 GEMM。

**需要观察的现象**：整个 `prefill_full_attention` 函数里对 KV 池的写入只发生在 prep 内核一处；后续 attention 内核对池是只读的。

**预期结果**：清单大致为——`q_full [heads×256×2, seq]`（只读消费 gate 带）、`k/v [kv_heads×256, seq]`（prep 内核读入并写池）、`q_prepped [heads×256, seq]`（prep 输出、attention 输入）、`attn_out [heads×256, seq]`（attention 输出、gate 原地修改）、`projected [hidden, seq]`（o_proj 输出，TP 下是部分和）。KV 池副作用：K/V 各写 `seq` 个 token 到 `page_indices` 指定的页。若本地有 GPU 环境，可在 `prefill_full_attention` 前后打印 `recurrent.seq_len` 与 KV 池已用页数对照（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `q_norm`/`k_norm` 是 `[head_dim]` 而不是 `[num_heads, head_dim]`？
**答案**：因为 Qwen3.5 的 QK-norm 权重在所有 head 之间共享（注释明确写着 "broadcast to all heads"，[src/weights/layers.rs:25-27](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L25-L27)）。每个 head 对同一组 `[head_dim]` 权重做 RMSNorm。这也顺带使它在 TP 下天然无需切分：任何 head 子集用的都是同一份权重。

**练习 2**：attention 计算完之后、`o_proj` 之前，输出张量的行数是多少？TP=2 时 `o_proj` 为什么是列切？
**答案**：行数是 `local_full_attn_q_dim = local_num_attention_heads × head_dim`——本 rank 的头算出的本 rank 头维结果。`o_proj` 形状 `[hidden, num_heads×head_dim]`，其「输入侧」是 attention 输出、已被 head 切分，所以按 q 维列切后每个 rank 对完整 hidden 出**部分和**，经 `all_reduce_hidden` 求和得到完整输出。这是 Megatron 式 attention TP 的标准列切 q/k/v + 行切 o 布局，只是这里以「行切投影产部分 hidden」的视角表述。

**练习 3**：gate 应用为什么放在 attention 之后、而不是投影时直接乘进 Q？
**答案**：gate 调制的是注意力**输出**（value 加权和），不是注意力权重本身。若乘进 Q 会改变 softmax 分布（等价于缩放 logits），语义完全不同。内核名 `attention_gate_batch_hd256_cuda` 与调用位置（attention 输出缓冲 `attn_out_batch` 上原地修改，[src/prefill.rs:425-438](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L425-L438)）都体现了这一点：`out = attn_out ⊙ sigmoid(gate)`。

### 4.3 线性注意力层：Gated DeltaNet 的权重与一次前向

#### 4.3.1 概念说明

`LinearAttentionLayer` 实现的是 Gated DeltaNet。直觉版：每个线性注意力层维护一组 per-head 的**状态矩阵** \( S \)（形状概念上 `[K, V]`），每个 token 到来时做一次「遗忘 + 写入」的递推更新，然后用当前 query 从状态里读出输出。示意形式（本 crate 的精确状态布局与内核数学在 u3-l2 精读，此处只给直觉）：

\[ S_t = \alpha_t \cdot S_{t-1} \;(\text{遗忘门衰减}) \; + \; \text{新写入}(\beta_t, \mathbf{k}_t, \mathbf{v}_t), \qquad \mathbf{o}_t = S_t\, \mathbf{q}_t \]

其中 \( \alpha_t \) 由 `A_log`/`dt_bias` 参数化的衰减得出，\( \beta_t \) 是写入强度（beta 投影），「Delta Rule」指新写入是对预测误差的修正而非盲目叠加。与传统 RNN 的区别在于更新是**外积形式**的，可以分块（chunkwise）并行——这正是 u1-l2 讲过的 Triton AOT 生成的 GDR chunkwise prefill 内核所做的事。

这层的输入侧还有两个配角：

- **causal depthwise conv1d**：QKV 投影结果先过一个核宽 4 的因果逐通道卷积（带 `conv_state` 记住跨 chunk 的尾拍），给线性层一点短程上下文记忆；
- **gated RMSNorm**：GDN 输出用 z 投影做 SiLU 门控的 RMSNorm（`rms_norm_gated_batch_into`，权重是 head 共享的 `norm_weight`）。

与全注意力层最大的结构差异：**这层没有任何 KV cache**。历史全部活在 `RecurrentState`（调度器侧持有）的 `layer_state.state`（f32）与 `layer_state.conv_state`（bf16）里，尺寸恒定。

#### 4.3.2 核心流程

加载期（[src/weights/layers.rs:148-189](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L148-L189)）——checkpoint 按四个独立投影存储，装载期融合成两个矩阵：

```
in_proj_qkv + in_proj_z  --vstack-->  in_proj_qkvz   # 行序 [qkv | z]
in_proj_b  + in_proj_a   --vstack-->  in_proj_ba     # 行序 [beta | alpha]
conv1d.weight   → conv1d_weight（展平 [qkv_dim × kernel_dim]，通道序镜像 qkv 带）
dt_bias / A_log → 按 value head 分片（A_log 装载时从 bf16 加宽为 f32）
norm.weight     → norm_weight（[value_head_dim] f32，head 共享，整取不切）
out_proj        → 列切（行并行：各 rank 出部分 hidden 和，层内 all-reduce）
```

融合的收益（注释原话）：「one GEMM per group instead of four launches」——decode 路径整层只发 2 次 GEMM 就拿到 qkvz 和 ba。

前向期 prefill（[src/prefill.rs:448-552](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L448-L552)）：

```
输入: normed [hidden, seq]
1. qkv = GEMM(in_proj_qkvz 的 [0, qkv_dim) 行带)
   z   = GEMM(in_proj_qkvz 的 [qkv_dim, +z_dim) 行带)   # 同一矩阵两次行带读取
   b   = GEMM(in_proj_ba 的 [0, H_v) 行带)
   a   = GEMM(in_proj_ba 的 [H_v, +H_v) 行带)
2. qkv_conv = conv1d(qkv, conv1d_weight)  # 因果逐通道卷积，更新 conv_state
3. gdr_out  = GDR chunkwise 内核(qkv_conv, b, a, dt_bias, a_log)  # 读改 layer_state.state
4. normed_out = gated RMSNorm(gdr_out, norm_weight, gate=z, act=SiLU)
5. out = GEMM(out_proj, normed_out) + TP all-reduce
```

decode（[src/batch_decode.rs:845-906](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L845-L906)）同样五步，但两处不同：第 1 步对融合矩阵各发**一次**完整 `gemm_into`，之后用 `columns()` 视图切行带（不再分两次 GEMM）；第 2、3 步换成 decode 专用内核，通过**按槽位的设备指针表**（`state_ptrs`/`conv_state_ptrs`）直接寻址每个请求的固定地址状态——这是 CUDA Graph 重放指针稳定性的关键，u4-l3 展开。

#### 4.3.3 源码精读

权重清单与注释非常完整（[src/weights/layers.rs:30-55](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L30-L55)），几个要点直接读注释即可：

- `in_proj_qkvz`：行 `[0, local_linear_qkv_dim)` 是 qkv、之上是 z，行序由 `linear_qkv_shard_segments` 约定；
- `in_proj_ba`：行 `[0, H_v)` 是 beta、之上是 alpha；
- `conv1d_weight`：从 `[qkv_dim, 1, 4]` 展平，**通道布局镜像 qkv 带**——所以 TP 分片时用与 qkv 相同的段偏移乘上 kernel dim（[src/weights/layers.rs:409-415](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L409-L415)）；
- `a_log`/`norm_weight`：bf16 存储在 checkpoint、装载时加宽为 f32（经 `tensor_f32_cow` 路径），因为衰减与 norm 对精度敏感。

TP 分片最讲究的是 qkv：checkpoint 把全局行存成 `[所有 q 行 | 所有 k 行 | 所有 v 行]` 三段，每个 rank 从**每段里**切出自己的 head 局部片再拼接，使 rank 拿到的行保持 `[q_local | k_local | v_local]`（[src/weights/layers.rs:386-407](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L386-L407)，注释里点名的「gated-q lesson」：绝不能跨段重排，q 行按 key head 计数、v 行按 value head 计数）。配套单测 [src/weights/layers.rs:473-491](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L473-L491) 用 16k/32v head 的 fixture 验证两 rank 的段拼起来恰好无重叠覆盖全局。

维度上，`z` 维 = `linear_num_value_heads × linear_value_head_dim`（[src/config/model.rs:155-157](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/model.rs#L155-L157)），且 local z 维等于 local v 维（[src/config/tp.rs:225-228](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/tp.rs#L225-L228)）——z 作为门控与 GDN 输出同宽，才能在 gated norm 里逐元素相乘。

前向侧的两个状态外置点（[src/prefill.rs:504-514](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L504-L514)）：`layer_state = &mut recurrent.layers[*linear_idx]`，conv 内核拿走 `&mut layer_state.conv_state`，GDR 内核拿走 `&mut layer_state.state`——层结构体 `attn` 自始至终只有不可变借用。GDR chunkwise 调用（[src/prefill.rs:516-531](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L516-L531)）同时吃进 `qkv_conv/b/a/dt_bias/a_log`、输出 `gdr_out` 并原地推进状态。gated norm（[src/prefill.rs:533-544](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L533-L544)）的激活函数显式传 `GatedNormActivation::Silu`。

decode 版的行带读取与装载行序的对应关系（[src/batch_decode.rs:858-897](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L858-L897)）：`bufs.qkvz.columns(0, qkv_dim)` 取 qkv 带、`columns(qkv_dim, z_dim)` 取 z 带、`bufs.ba.columns(0, value_heads)` 是 beta、`columns(value_heads, value_heads)` 是 alpha——注释点明「row order is the one the loader stacked the weights in」，装载期融合与消费期行带是同一份契约的两端。

#### 4.3.4 代码实践

**实践目标**：验证「装载期融合行序」与「前向期行带读取」互相咬合——把 checkpoint 张量名、融合后矩阵的行段、decode 读取带三列对齐。

**操作步骤**：

1. 从 [src/weights/layers.rs:148-168](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L148-L168) 列出 4 个 checkpoint 张量名（`in_proj_qkv.weight`、`in_proj_z.weight`、`in_proj_b.weight`、`in_proj_a.weight`）与它们 vstack 后的落位。
2. 从 [src/batch_decode.rs:860-875](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L860-L875) 抄下 `columns()` 的四组 `(offset, len)` 参数。
3. 画一张两行的对照表：上行是 `in_proj_qkvz` 与 `in_proj_ba` 的行段划分，下行是 decode 消费的行带，逐段连线。
4. 思考题自检：如果有人把 vstack 顺序改成 `[z | qkv]`，哪些调用点会静默出错？（提示：`gemm_rows_into` 的 offset 参数、`columns()` 的 offset、conv1d 的通道镜像三处。）

**需要观察的现象**：没有任何一处代码在运行时「检查」行序——正确性完全靠装载与消费两端的字面约定。这正是注释把行序写进文档字符串的原因。

**预期结果**：对照表为 `qkvz: [0, qkv_dim) → conv1d 输入带`、`[qkv_dim, z_dim) → gated norm 的 z 门`；`ba: [0, H_v) → beta`、`[H_v, 2·H_v) → alpha`。若顺序颠倒，prefill 的 `gemm_rows_into`（[src/prefill.rs:468-502](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L468-L502)）会静默读到错误的行带、输出形状却可能仍对——这类 bug 只能靠 u8-l1 的精度门暴露。

#### 4.3.5 小练习与答案

**练习 1**：`out_proj` 是 `[hidden_size, local_linear_z_dim]`，注释说它是 row-parallel 且「the layer all-reduces the partial hidden sum under TP」。结合 u2-l2，解释为什么线性层在 TP 下需要 all-reduce，而全注意力层的 qkv 投影不需要？
**答案**：线性层的输入投影 `in_proj_qkvz` 按 head 切行（等价 Megatron 的列切），每个 rank 对**完整 hidden** 的 normed 输入只算出自己 head 子集的 z 维输出；`out_proj` 把 z 维映射回 hidden，各 rank 持 `[hidden, z_local]`，乘出的 hidden 是**部分和**，必须 all-reduce。全注意力层的 q/k/v 投影虽然也切行，但它们的输出（各 rank 的 head 子集）直接进注意力内核，不需要聚合到 hidden——聚合发生在 `o_proj` 之后的 hidden 维。两种层的通信点都在「回到 hidden 维」的那一刻：全注意力在 o_proj 后、线性在 out_proj 后，代码里都是同一行 `all_reduce_hidden`。

**练习 2**：为什么 `a_log` 和 `norm_weight` 要从 checkpoint 的 bf16 加宽成 f32 存储，而 `dt_bias` 保持 bf16？
**答案**：`a_log` 参数化遗忘门衰减（指数里的量），`norm_weight` 逐元素缩放 GDN 输出——两者都是**乘性/指数路径**上的敏感参数，bf16 的 8 位尾数会在长序列递推中累积误差；`dt_bias` 与 a_log 相加后同样进入 softplus，但作为加性偏置其本身精度损失影响相对小，且 decode 内核签名按 bf16 消费它（`gated_delta_rule_decode_batch_into` 的参数，[src/batch_decode.rs:876-877](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L876-L877)）。这是精度与内核接口的折中，装载代码注释明确指向 `tensor_f32_cow` 的「widened exactly at load」路径。

**练习 3**：conv1d 的 TP 分片为什么不能直接用 `Shard::flat(qkv_dim)` 一刀切？
**答案**：因为 conv1d 权重是**展平**的 `[qkv_dim × kernel_dim]` 向量，每个通道的 4 个 kernel tap 连续存放；同时通道布局镜像融合 qkv 的行段 `[q|k|v]`，而 q/k 段按 key head 切、v 段按 value head 切，三段宽度不同。所以分片要把 qkv 的三段偏移各自乘以 kernel_dim 换算成「tap 单位」再拼接（`conv1d_shard_segments`，[src/weights/layers.rs:409-415](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L409-L415)）。一刀切会把某个 head 的 tap 组切一半给邻居 rank。

## 5. 综合实践

**任务：亲手画出两种层的数据流图（本讲规格指定的实践）。**

画出**一批 token**（seq 长度为 S）经过一个 `TransformerBlock35` 的完整数据流图，FullAttention 与 LinearAttention 各画一份。要求：

1. **骨架两种图共享**：`hidden [hidden, S]` → `input_layernorm`（(1+w) RMSNorm）→ 注意力子层 → 残差相加得 `hidden_mid` → `post_attention_layernorm` → MLP（`gate_up_proj` GEMM → SiLU·mul 融合 → `down_proj` GEMM → TP all-reduce）→ 残差相加得新 `hidden`。
2. **每个节点标注函数名**，以两条真实路径为准：
   - prefill 骨架：[src/prefill.rs:238-302](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L238-L302)（`batched_rms_norm_offset_into` / `add_batch` / `gemm` / `silu_mul_fused_batch_into` / `all_reduce_hidden`）；
   - decode 骨架：[src/batch_decode.rs:653-717](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L653-L717)（`rms_norm_batch_offset_into` / `add_batch_into` / `gemm_into` 等 `_into` 变体——注意 decode 路径写进预分配缓冲，为图重放服务）。
3. **FullAttention 分支**展开为五步：q/k/v 三 GEMM → 融合预备（QK-norm + partial RoPE + K/V 落页）→ paged attention → gate → `o_proj` + all-reduce；函数名取 [src/prefill.rs:305-446](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L305-L446)。
4. **LinearAttention 分支**展开为五步：融合 GEMM（`gemm_rows_into` 取 qkv/z/b/a 四带）→ conv1d → GDR chunkwise → gated RMSNorm（z 门）→ `out_proj` + all-reduce；函数名取 [src/prefill.rs:448-552](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L448-L552)。
5. **用虚线标出层外状态**：FullAttention 图上画 `kv_buffer`（prep 内核写入、attention 内核读取，标 `full_idx` 层号）；LinearAttention 图上画 `layer_state.state` 与 `layer_state.conv_state`（标 `linear_idx`）。两张图并排时，这条虚线就是「KV 系统 vs 递推系统」的分界。

**验收标准**：合上图能在 5 分钟内向同伴讲清——(a) 两种层共享哪些节点、差在哪个子图；(b) `all_reduce_hidden` 在两张图的哪两个位置出现、为什么恰好是那里；(c) 为什么 LinearAttention 图里没有 KV 池、它的「记忆」画在哪。

（本实践为源码阅读型，无需 GPU。若想锦上添花，可在有 GPU + 权重的环境跑 `PEGAINFER_TEST_MODEL_PATH=models/Qwen3.5-4B cargo test --release -p pegainfer-qwen35 --features qwen35 --test e2e_scheduler`，对照日志里逐层加载的 `layer_types` 输出检查你的图——待本地验证。）

## 6. 本讲小结

- `TransformerBlock35` = 共有的双 norm + MLP + 因层而异的 `LayerKind`；分发源头是 `config.layer_types[i]`，加载期决定变体、前向期 `match` 分派，两条索引流（`full_idx` 进 KV 池、`linear_idx` 进递推状态）各自编号。
- **层结构体只装权重**：全注意力的 K/V 在模型级分页池（只按 8 个全注意力层开池），线性注意力的状态在调度器侧 `RecurrentState`——权重与状态分离是图重放与前缀缓存的前提。
- `FullAttentionLayer` 的三个特性：gated Q（q_proj 行含 per-head `[q|gate]` 交错块，TP 必须按 head 整块切）、head 共享 QK-norm、partial RoPE；prefill 把 norm+RoPE+落页融成一个内核。
- `LinearAttentionLayer` 的装载期融合：四个 checkpoint 投影 vstack 成 `in_proj_qkvz`/`in_proj_ba` 两个矩阵，前向一次 GEMM 一组；conv1d 通道布局镜像 qkv 带的段偏移；`a_log`/`norm_weight` 加宽 f32。
- 前向五步序（norm → 注意力 → 残差 → norm → MLP+残差）在 prefill 与 decode 两条路径同构，差别只在 `_into` 后缀（预分配缓冲）与内核族选择；TP 通信点统一在「回到 hidden 维」处（`o_proj`/`out_proj` 之后）。

## 7. 下一步学习建议

本讲看清了层的**结构与调用序**，但刻意绕开了两类细节，正好是下一单元的入口：

- **u3-l1（全注意力层）**：深入 `KvView`/`KvBuffer` 分页布局、RoPE 表的按需扩容（`ensure_rope_cache_covers`）、hd256 内核族的张量布局参数——把本讲 4.2 的「落页」一步放大。
- **u3-l2（线性注意力层）**：精读 `RecurrentState`/`LayerRecurrentState` 的 `[H,K,V]` f32 状态布局、`per_layer_dims` 的元素数公式、`LinearStatePointerTables` 指针表——把本讲 4.3 的虚线状态画实。
- 想先看这两种层在**整批解码**里如何混跑，可跳读 u4-l2（batch_decode 混合批），再回头补 u3 的状态细节。

建议顺手做一个自测：合上讲义，只凭 [src/ops.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/ops.rs#L1-L29) 的 29 行重导出清单，把每个函数名归类到「全注意力用 / 线性注意力用 / 两者共用 / 模型级」——归类完毕，本讲的地图就内化了。
