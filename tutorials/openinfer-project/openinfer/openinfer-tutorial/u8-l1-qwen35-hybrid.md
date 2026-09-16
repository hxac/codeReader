# Qwen3.5：Gated DeltaNet 混合注意力

## 1. 本讲目标

本讲进入模型线巡礼的第一条线：Qwen3.5（crate `pegainfer-qwen35`，feature `qwen35`）。它是全仓库唯一一条「线性注意力 + 全注意力」混合架构的默认资料最全模型线。学完本讲你应该能够：

1. 说出 Qwen3.5 的层布局（24 层线性注意力 + 8 层全注意力）以及配置如何描述它。
2. 理解 Gated DeltaNet（GDR）线性注意力的两条前向路径：解码的单步递推、预填充的 chunkwise（分块）七阶段流水线。
3. 掌握每请求状态 `RecurrentState`（f32 循环状态 + bf16 conv 状态）的结构、大小与批量解码的指针表机制，并能解释「为什么前缀缓存命中必须有循环状态快照」。
4. 对比 TP 执行器 `Qwen35TpExecutor` 与单卡执行器的结构差异（controller/worker 广播、rank 本地状态、rank-0 采样、图槽位）。
5. 注意到 2026-09 提交 #1046 之后，TP 路径的采样 scratch 改经 `SampleScratch::with_selection_width` 携带「可解码词表宽度」，与 logits arena 的对齐宽度分离（完整机制在第 8 单元 u8-l7 展开）。
6. 说清 chunkwise 预填充内核是构建期 Triton AOT 生成的，而解码/conv 内核是原生 CUDA——两者进入二进制的路径不同。

## 2. 前置知识

**线性注意力 vs 全注意力（Softmax Attention）。** u5-l3 里 Qwen3 的每一层都是全注意力：每个新 token 都要对全部历史 token 做注意力，历史以 KV cache 的形式保存，显存随上下文长度线性增长。线性注意力换了一种记忆方式：它不保存每个历史 token 的 K/V，而是维护一个固定大小的**循环状态矩阵** \( S \)（每个头一个 \([\,K \times V\,]\) 矩阵），每来一个 token 就对 \( S \) 做一次更新。代价是历史被「有损压缩」进矩阵，收益是状态大小与上下文长度无关，解码每步是 O(1) 的矩阵-向量运算。

**Delta rule（增量规则）与门控。** 最简单的线性注意力（线性 RNN）是 \( S_t = S_{t-1} + k_t v_t^\top \)，只会累加。Delta rule 在写入前先用当前 key 修正旧状态——先把旧状态里对 \( v_t \) 的「预测误差」扣掉再写入（示意形式，具体转置布局以内核实现为准）：

\[ S_t \;=\; \alpha_t \, S_{t-1} \bigl(\mathbf{I} - \beta_t\, k_t k_t^\top\bigr) \;+\; \beta_t\, k_t v_t^\top, \qquad o_t \;=\; S_t\, q_t \]

其中 \( \beta_t \) 是写入强度（代码里的 `b_proj` 经变换得到的 beta），\( \alpha_t \) 是遗忘门（由 `a_proj`/`a_log`/`dt_bias` 变换出的衰减）。这套结构叫 Gated DeltaNet（GDN），Qwen3.5 沿用自 Qwen3-Next。

**Chunkwise（分块）并行化。** 递推式一次只能算一个 token，预填充几千个 token 逐个递推太慢。分块算法把序列切成 chunk_size=64 的块：块内 token 间的交互用小矩阵运算并行算清楚（其中包含一个 \((\mathbf{I}+A)^{-1}\) 三角矩阵求逆），块与块之间仍然靠循环状态传递。这正是 FLA（flash-linear-attention）库的经典做法，本仓库的 Triton 内核就是从 FLA 改写的。

**还需要回忆两讲内容：**
- u4-l2：`pegainfer-kernels/build.rs` 的构建流水线——nvcc 原生编译 `csrc/*.cu`，而 `qwen35` feature 额外触发构建期 Triton AOT（跑 Python 生成 cubin + C 启动代码）。本讲 4.5 会精确到符号级。
- u6-l4：Qwen3 的 CUDA Graph 解码路径——图捕获要求缓冲区指针预先固定。本讲会看到 Qwen3.5 的循环状态如何被搬进固定地址的「图槽位」。

**词表三个宽度（本讲只做铺垫，详见 u8-l7）。** Qwen3.5-4B 的 checkpoint 词表有 248320 行（`vocab_size`），分词器实际可解码的前缀是 248077（`decodable_vocab`），而 #1046 之后 logits GEMM 的宽度被向上对齐到 128 的倍数 248192（`selection_vocab`）。三个宽度各司其职，4.4.3 会看到 TP 执行器如何把它们交给采样层。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `pegainfer-qwen35/src/config/model.rs` | `Config35`：层布局（`layer_types`）、三个词表宽度、`bound_selection_vocab` 对齐规则 |
| `pegainfer-qwen35/src/prefill.rs` | 预填充入口：分块驱动、逐层前向、层类型分派 |
| `pegainfer-qwen35/src/prefill_buffers.rs` | `GdrChunkwiseScratch35`：chunkwise 预填充的预分配 scratch（含每块状态快照缓冲） |
| `pegainfer-qwen35/src/recurrent.rs` | GDR 算子的 Rust 包装：单步/批量解码、conv1d、chunkwise 七阶段编排 |
| `pegainfer-qwen35/src/recurrent_state.rs` | 每请求循环状态 `RecurrentState`、批量解码指针表 `LinearStatePointerTables` |
| `pegainfer-qwen35/src/tp_executor.rs` | TP 执行器：controller/worker、预捕获扫描、图槽位、采样 scratch |
| `pegainfer-qwen35/src/scheduler/mod.rs` | 调度器：请求状态机、单卡/TP 双后端枚举、预填充分块预算 |
| `pegainfer-qwen35/src/scheduler/tp.rs` | 调度器的 TP 半边：构造 TP 步条目、按 request_id 对齐结果 |
| `pegainfer-qwen35/src/batch_decode_graph.rs` | `BatchDecodeGraphState`：固定地址的 `slot_states` 与批量大小的桶 |
| `pegainfer-kernels/src/ffi/qwen35.rs` | FFI 声明：原生 CUDA 符号（无门控）+ Triton AOT 符号（qwen35 门控） |
| `pegainfer-kernels/build.rs` | 构建期 Triton AOT：`TritonKernelSpec` → cubin + C 包装 |
| `pegainfer-kernels/tools/triton/gated_delta_rule_chunkwise_kernels.py` | GDR chunkwise 的 Triton 源码（改编自 FLA） |
| `docs/models/qwen35/prefix-cache.md` | 前缀缓存设计文档：KV + 循环状态快照「双条件命中」规则 |
| `docs/models/qwen35/tp-design.md` | TP 设计文档：复用 Qwen3 TP 运行时的分阶段计划 |

## 4. 核心概念与源码讲解

### 4.1 混合层布局与三个词表宽度

#### 4.1.1 概念说明

Qwen3.5-4B 有 32 层：24 层线性注意力 + 8 层全注意力（全注意力层位于 3, 7, 11, 15, 19, 23, 27, 31，见 `docs/models/qwen35/tp-design.md`）。配置用 `layer_types` 字符串数组显式给出每层类型，运行时据此把层分派到两条完全不同的前向路径。这意味着：

- **全注意力层**的状态在分页 KV 池里（和 Qwen3 一样）；
- **线性注意力层**的状态在每请求的 `RecurrentState` 里（本讲 4.3）；
- 同一个请求同时持有两类状态，缺一不可。

配置里还有三个容易混淆的宽度字段。#1046 之前只有 `vocab_size` 一个宽度；#1046 把「GEMM 想要多宽」和「采样允许选多宽」拆成了两个字段，`selection_vocab`（arena 宽度，向上对齐到 128 的倍数）和 `decodable_vocab`（语义边界，pad 行被抑制为 -inf 且路由判定以此为分母）。

#### 4.1.2 核心流程

```
HF config.json
  ├── layer_types: Vec<"linear_attention"|"full_attention">   → 层布局
  ├── vocab_size = 248320                                     → checkpoint 行数上限
  └── (加载期) tokenizer_effective_vocab(model_path) = 248077
          ↓
Config35::bound_selection_vocab(248077)
  ├── decodable_vocab  = 248077                （语义边界）
  └── selection_vocab  = min(248077 向上取 128 倍数, 248320) = 248192
          ↓
logits arena / GEMM 宽度 = selection_vocab；采样路由宽度 = decodable_vocab
```

#### 4.1.3 源码精读

层布局与两个宽度字段的定义：[pegainfer-qwen35/src/config/model.rs:100-112](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/config/model.rs#L100-L112)——`layer_types: Vec<LayerType>` 决定每层走哪条路径；`selection_vocab` 注释明确说明它是「先钳到前端可解码词表、再向上取 logits GEMM tile 倍数」的宽度，`decodable_vocab` 是「超出它的行被抑制为 -inf、argmax-vs-sample 路由以它为分母」的语义宽度。

数全注意力层的辅助函数：[pegainfer-qwen35/src/config/model.rs:126-131](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/config/model.rs#L126-L131)——线性层数在别处一律用 `num_hidden_layers - num_full_attention_layers()` 推出（本讲后面会反复遇到这个表达式）。

对齐规则本体：[pegainfer-qwen35/src/config/model.rs:161-181](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/config/model.rs#L161-L181)——`bound_selection_vocab` 做三件事：分词器比 checkpoint 还宽就直接拒绝（fail-closed）；`effective_vocab.next_multiple_of(128)` 得到对齐宽度；记录 `decodable_vocab`。注释解释了动机：GEMM 的 M 维和输出矩阵的 leading dimension 都取这个宽度，248077 这种奇数会把 cublasLt 逼到 align-1 的 sm_75 时代内核（A100 上每解码步约 1.7 ms，约占 c16 TPOT 的 12%）。

加载期的调用点：[pegainfer-qwen35/src/weights.rs:158-181](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/weights.rs#L158-L181)——模型加载时从分词器元数据读出 `tokenizer_effective_vocab`，交给 `bound_selection_vocab`，如果对齐发生了就打一行日志说明「selection 宽度 = decodable 词表 + tile 对齐 pad」。

对应的单元测试：[pegainfer-qwen35/src/config/model.rs:343-358](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/config/model.rs#L343-L358)——977 在 vocab 1000 内对齐到 1024 但被 min 钳回 1000（decodable=977）；769 对齐到 896（decodable=769）。这是纯 CPU 逻辑，值得精读。

#### 4.1.4 代码实践

1. **实践目标**：用真实公式手工复现 4B 的三个宽度，确认你理解对齐与钳制。
2. **操作步骤**：在纸（或编辑器）上算：`248077.next_multiple_of(128)` 是多少？`min(它, 248320)` 是多少？再算 9B checkpoint 若 decodable=250678（示例假设值，待用真实 checkpoint 确认）时的对齐结果。然后精读 4.1.3 引用的测试，与你的手算对照。
3. **需要观察的现象**：对齐只在「decodable 不是 128 的倍数」时生效；钳制只在「对齐越过 checkpoint 行数」时生效。
4. **预期结果**：248077 → 248192（未被钳制）；测试里 977 → 1000（被钳制）。
5. 若想运行该测试验证：`cargo test --release -p pegainfer-qwen35 --features qwen35 --lib selection_vocab`——注意这需要构建期 Python+Triton（见 4.5），本环境无法确认运行结果，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `selection_vocab` 不直接用 `vocab_size`（248320）？多算一点 pad 行不行？
**答案**：可以但不划算。GEMM 宽度越大每步算的无效乘法越多；248192 是「能让 cublasLt 选中向量化内核的最小对齐宽度」，比 248320 少 128 行无效计算。反向问题（为什么不直接用 248077）见 u8-l7：奇数宽度让 GEMM 落到 align-1 内核。

**练习 2**：`num_full_attention_layers()` 是 `layer_types` 数出来的，为什么线性层数到处都用减法 `num_hidden_layers - num_full_attention_layers()` 而不单独存一个字段？
**答案**：单一事实来源。`layer_types` 是 config.json 的原始事实，全注意力层数与线性层数都从它派生，避免两个字段在解析/校验路径上失去同步（`recurrent_state.rs` 的 `per_layer_dims` 也是这么算的）。

### 4.2 Gated DeltaNet 前向：单步解码与 chunkwise 预填充

#### 4.2.1 概念说明

GDR 的 Rust 包装层是 `recurrent.rs`，它把 CUDA/Triton 内核包成带几何校验的安全函数。两条路径：

- **解码（单步递推）**：每个请求每步一个 token。批量解码内核接受「每层一张设备侧指针表」，一次 launch 处理整个 batch 的所有请求。这是原生 CUDA（`csrc/qwen35/gated_delta_rule.cu`），无条件编译。
- **预填充（chunkwise）**：一段 prompt 一次吃 64 token 一块，七阶段流水线。内核是构建期 Triton AOT 生成的（4.5）。

线性层还有一个 **causal depthwise conv1d** 前置模块（conv_kernel_dim=4）：token 序列先过卷积再进 GDR。卷积同样有「单步解码（改 conv state）」与「整段预填充（留下最后 kernel-1 个输入作为 conv state）」两种内核。conv state 和循环状态一样是每请求长期状态。

#### 4.2.2 核心流程

解码一步（每层、每请求）：

```
输入: qkv(bf16), b_proj, a_proj, dt_bias, a_log, state S[H,K,V] f32
  1. 由 a_proj/a_log/dt_bias 算门控 α_t（衰减）
  2. 由 b_proj 算写入强度 β_t
  3. S ← α_t · S · (I − β_t k kᵀ) + β_t k vᵀ     （原地更新）
  4. o = S q
输出: o(bf16)，state 已就地前进
```

预填充一个 chunk（64 token）的七阶段（对应 `gated_delta_rule_prefill_chunkwise_into` 的七次内核调用）：

```
prepare   : qkv 展开/归一化出 q,k,v；算出原始门 g 与 β
cumsum    : 块内累积门 g_cumsum（chunk-local 前缀和）
A         : 由 k, g_cumsum, β 构造下三角块内矩阵 A
solve     : 求 (I+A)^{-1}（Wy 表示的关键步骤）
recompute : 用 A^{-1} 构造 w, u（修正后的 key/value 表示）
state     : S_prev × 块间信息 → 更新循环状态 S；同时把每块边界状态写入 chunk_state 快照
o         : 输出 = q 对块首状态 + 块内 (q, v_new) 双路注意力
```

上层 `prefill_chunk_forward` 的职责是：对每个线性层跑一遍「conv1d 预填充 + GDR chunkwise」，对每个全注意力层跑分页注意力，最后 `recurrent.seq_len += seq_len` 推进 token 计数。

#### 4.2.3 源码精读

批量 GDR 解码包装：[pegainfer-qwen35/src/recurrent.rs:57-109](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/recurrent.rs#L57-L109)——注意签名里的 `state_ptrs: &CudaSlice<u64>`：内核不是收一组状态张量，而是收一张**设备侧指针表**（每行指向一个请求的状态），这样一次 launch 就能处理 batch 中不同请求各自的 \( S \)。函数前半段的 `assert_eq!` 系列把 head 数、维度、batch 全部钉死（`key_dim`/`val_dim` 必须等于 AOT 常量 128）。

conv1d 的解码与预填充双内核：[pegainfer-qwen35/src/recurrent.rs:111-179](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/recurrent.rs#L111-L179)——`conv1d_decode_batch_into` 同样吃指针表（`conv_state_ptrs`）；`conv1d_prefill_batch_into` 整段卷积并把「最后 kernel_size−1 个输入」写进 `conv_state`，供解码续算。

chunkwise 七阶段编排：[pegainfer-qwen35/src/recurrent.rs:428-548](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/recurrent.rs#L428-L548)——这是本讲最重要的单段代码。文档注释明说设计意图：「chunkwise 路径是一个显式的多阶段算子 + 预分配 scratch，而不是一次不透明的内核 launch」。函数开头 20 多行 `assert_eq!` 把 scratch 每个缓冲的形状钉死，然后顺序调用 prepare→cumsum→A→solve→recompute→state→o 七个阶段，每阶段失败都有独立的错误上下文。最后一个 `o` 阶段传入缩放因子 `1/sqrt(key_dim)`。

预填充的 chunk 驱动：[pegainfer-qwen35/src/prefill.rs:53-91](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/prefill.rs#L53-L91)——`prefill_last_hidden` 先校验整段范围（位置溢出 + RoPE 缓存覆盖）再动任何状态，然后按 `PREFILL_CHUNK_LEN`（20000，[pegainfer-qwen35/src/prefill.rs:14-20](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/prefill.rs#L14-L20) 的注释解释：分块是为了把随 pass 长度线性增长的 GDR scratch 钳在启动期预留内）串行推进，每块结束才分配下一块的 scratch，峰值显存不超过一块的预留。注意这 20000 是**内核层**的保险丝；调度器还有自己的每步预算（`DEFAULT_MAX_PREFILL_TOKENS = 1024`，见 4.4.2）。

逐层分派：[pegainfer-qwen35/src/prefill.rs:184-206](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/prefill.rs#L184-L206)——`for (layer_idx, layer) in self.layers.iter().enumerate()` 用 `linear_idx`/`full_idx` 两个游标分别索引循环状态向量和 KV 层偏移，循环末尾 `recurrent.seq_len += seq_len`。层内分派在 [pegainfer-qwen35/src/prefill.rs:211-259](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/prefill.rs#L211-L259)：`match &layer.attn { LayerKind::FullAttention(..) => …, LayerKind::LinearAttention(..) => … }`——4.1 的配置在这里变成运行时分叉。MLP 部分（269-273 行）gate_up GEMM + SiLU 融合 + down GEMM 后跟 `all_reduce_hidden`，TP 下每层 MLP 一次 all-reduce。

chunkwise scratch：[pegainfer-qwen35/src/prefill_buffers.rs:22-49](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/prefill_buffers.rs#L22-L49)——每个字段对应七阶段的一个中间量，特别注意最后一个 `chunk_state: CudaSlice<f32>`，注释写明它是「每块的循环状态快照 \[num_chunks, H, K, V\]」——**chunkwise 内核天然物化了每个 64-token 边界的循环状态**，这正是 4.3 前缀缓存快照的物质基础。`CHUNK_SIZE = 64` 定义在 [pegainfer-qwen35/src/prefill_buffers.rs:52](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/prefill_buffers.rs#L52)。

#### 4.2.4 代码实践

1. **实践目标**：把七阶段流水线的每个阶段与其 scratch 缓冲一一对应，做到「看着函数签名就能说出每阶段读什么写什么」。
2. **操作步骤**：打开 `recurrent.rs` 428-548 行与 `prefill_buffers.rs` 22-49 行，画一张两列对照表：左列是七个阶段函数名，右列填它读/写的 `GdrChunkwiseScratch35` 字段。第一行示例：`prepare` 读 `qkv/b_proj/a_proj/dt_bias/a_log`（模型权重），写 `q_expanded/k_expanded/v_raw/g_cumsum(原始 g)/beta`。补完七行后，再用 `state` 参数标注哪个阶段既读又写循环状态（提示：`state` 指针被同时以 `*const` 和 `*mut` 传入同一个内核，即原地前进）。
3. **需要观察的现象**：`cumsum` 之前 `g_cumsum` 里存的是原始门 g（prepare 写入），`cumsum` 之后才变成前缀和——同一缓冲两阶段复用。
4. **预期结果**：你应能在表里指出「块间信息只经 `state` 与 `chunk_state` 两个缓冲流动，其余全是块内中间量」。
5. 想在 GPU 上验证行为可精读（或运行）[pegainfer-qwen35/src/recurrent.rs:707-718 附近的测试 `gdr_decode_batch_matches_single_slot_reference`](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/recurrent.rs#L707-L718)——它断言「批量解码 = 逐请求单槽解码」；运行需 GPU 与 `--features qwen35`，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么解码内核用「设备侧指针表」而不是直接传一个大张量把所有请求的状态拼在一起？
**答案**：拼接要求状态在显存里连续，但每个请求的状态是独立分配、生命周期独立（请求退出即释放）的；图捕获模式还要求地址固定。指针表让内核间接寻址到各自的 `CudaSlice`，批量组合只改表内容（一次 H2D 拷贝），不动状态本体——4.3 的 `refill_from_recurrent_refs` 就是干这个的。

**练习 2**：`docs/models/qwen35/decode-kernel-attribution.md`（#1046 附带）报告 GDN 解码内核每层步 97.0 µs，是 vLLM FLA `fused_recurrent`（43.6 µs）的 2.2 倍。结合本节，说说这个差距的「形态原因」。
**答案**：两边算的是同一个递推式，但实现形态不同——FLA 把整层递推融合在少数内核里且针对 packed 布局调优；本仓库的解码路径把 conv1d、GDR 递推、norm/投影拆成多个内核 launch，且指针表间接寻址。这是逐 launch 开销 + 内核本身调优程度的差距，不是算法差距（算法上 chunkwise 预填充与 FLA 同源）。

### 4.3 每请求循环状态：RecurrentState、指针表与前缀缓存快照

#### 4.3.1 概念说明

线性层的「KV cache」不是页池里的页，而是每请求一份 `RecurrentState`：24 个线性层，每层一个 f32 状态矩阵 \([\text{local\_value\_heads}, K, V]\) + 一个 bf16 conv 状态。Qwen3.5-4B 单卡上每请求约 49.125 MiB——**与上下文长度无关**，这是线性注意力的核心红利；对比全注意力 KV 随 token 数增长。

关键结论（本讲学习目标 2）：**前缀缓存命中必须有循环状态快照**。Qwen3 的前缀缓存只回放 KV 页（u7-l3）；Qwen3.5 若只回放全注意力层的 KV 页，线性层在断点处的 \( S \) 无法从 KV 页推导——递推式的信息已经被压缩进矩阵，不可逆。所以设计文档规定：命中必须是「同一 256-token 边界上，全注意力 KV **和** 完整循环/conv 快照同时存在」。注意：**当前代码里共享前缀缓存尚未落地**（`docs/models/qwen35/prefix-cache.md` 是 issue #257 的设计文档），现在每请求的状态是私有分配。

#### 4.3.2 核心流程

```
请求准入（单卡）
  PrefillBackendState::Single { kv: KvState, rec: RecurrentState }
      └─ 每个预填充 chunk: prefill_chunk_forward 原地推进 rec
      └─ prompt 结束: (kv, rec) 交给解码批
解码批（单卡图模式）
  BatchDecodeGraphState.slot_states[slot]   ← 固定地址的每槽位 RecurrentState
      └─ 首个解码行: 请求私有 rec D2D 拷入槽位，私有分配释放
解码批（TP eager 模式）
  LinearStatePointerTables（每层两张 u64 表，容量 = max_batch）
      └─ 每步 refill_from_recurrent_refs: 只做 H2D 覆写，零设备分配
```

#### 4.3.3 源码精读

模块文档把 TP 语义说得很清楚：[pegainfer-qwen35/src/recurrent_state.rs:1-9](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/recurrent_state.rs#L1-L9)——「TP 下 value 头（和融合 qkv 通道）按 rank 切分，每个 rank 拥有自己的循环/conv 状态，**这些状态从不 all-reduce**」。这是 4.4 TP 设计的基石：通信只发生在 MLP 的隐藏维 all-reduce。

状态结构：[pegainfer-qwen35/src/recurrent_state.rs:20-35](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/recurrent_state.rs#L20-L35)——`LayerRecurrentState { state: CudaSlice<f32>, conv_state: DeviceVec }` 每层一个；`RecurrentState { layers: Vec<..>, seq_len }` 的 `seq_len` 是「已处理 token 数」，prefill 每块累加（4.2.3 见过），调度器用它和 `kv.seq_len()` 对账。

指针表及其复用契约：[pegainfer-qwen35/src/recurrent_state.rs:37-51](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/recurrent_state.rs#L37-L51)——文档注释是本模块的精华：底层 `CudaSlice` 在请求生命周期内地址固定，表「每槽位构建一次、跨 token 复用」；而行集会变化的 eager TP 解码则「按容量分配一次 + 每步 `refill_from_recurrent_refs` 覆写活着的行：只做 H2D 拷贝，无每步分配」。

refill 实现：[pegainfer-qwen35/src/recurrent_state.rs:129-184](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/recurrent_state.rs#L129-L184)——逐层收集 batch 内每个请求的 state/conv 指针，`memcpy_htod` 覆写表的前 `batch_size` 行；`batch_size` 之外的行保持旧值但内核只被告知 `batch_size`，所以不会被寻址。两个 `anyhow::ensure!` 分别拒绝「refs 不够」和「超容量」。

每请求字节数公式与单测：[pegainfer-qwen35/src/recurrent_state.rs:209-215](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/recurrent_state.rs#L209-L215) 给出 `bytes_per_request`，[pegainfer-qwen35/src/recurrent_state.rs:223-232](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/recurrent_state.rs#L223-L232) 的测试把它钉在 49 MiB + 128 KiB：`24 × (32×128×128×4B + 8192×3×2B)`。拆开看：每层状态 32 头 × 128×128 f32 = 2 MiB，conv 状态 8192 通道 ×（kernel 4 − 1）× bf16 = 48 KiB。

图模式槽位：[pegainfer-qwen35/src/batch_decode_graph.rs:36-53](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/batch_decode_graph.rs#L36-L53)——`slot_states: Vec<RecurrentState>` 预分配到桶容量，文档注释解释了图捕获的地址约束：给定桶大小的图永远只访问 `slot_states[0..bucket_size]`，所以槽位交换（`move_slot_within`）与准入（`copy_state_to_slot`，[同文件:111-119](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/batch_decode_graph.rs#L111-L119)）用 D2D 拷贝保持指针稳定。批量大小的桶集合 `BATCH_BUCKETS = [1,2,4,8,16,32,64]` 定义在 [pegainfer-qwen35/src/batch_decode_graph.rs:15-29](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/batch_decode_graph.rs#L15-L29)。

前缀缓存的设计契约：[docs/models/qwen35/prefix-cache.md:1-5](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/docs/models/qwen35/prefix-cache.md#L1-L5)——TL;DR 一句话：「Qwen3.5-4B 的前缀命中只有当全注意力 KV 与完整循环/conv 快照在**同一个 256-token 边界**上都存在时才有效；`Qwen35PrefixCache` 把两者一起检查、一起恢复，调度器看到的要么是完整命中、要么是 miss」。文档的 Decisions 节还规定快照每 256 个 prompt token 发布一次（即每 4 个 GDR chunk）、恢复时拷贝进请求**自己的** `RecurrentState`（绝不共享可变槽位）。

调度器侧的状态归属：[pegainfer-qwen35/src/scheduler/mod.rs:96-125](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/scheduler/mod.rs#L96-L125)——`PrefillingRequest35` 的注释写明「它拥有自己不断增长的 KV **和** 循环状态直到 prompt 耗尽」；`PrefillBackendState::Single { kv, rec }` vs `Tp { request_id }` 的对比正是 4.4 的伏笔：**单卡后端调度器直接持有状态，TP 后端调度器只持有逻辑身份，物理状态在各 rank 的 worker 里**。

#### 4.3.4 代码实践

1. **实践目标**：算出「循环状态 vs KV 页」的每请求显存账，直观感受两类状态的量级差异。
2. **操作步骤**：用 4B 参数手算：(a) `bytes_per_request` = 24×(32×128×128×4 + 8192×3×2) ≈ 49.125 MiB；(b) 假设全注意力 8 层、每层 K/V 各 4 个头 × head_dim 256、bf16、page 池按需增长，算 4096 token 上下文的 KV 显存（8 层 × 2 × 4×256×4096×2B ≈ 128 MiB，头数请以真实 checkpoint 的 config.json 核对，此处为演示量级）。列成表。
3. **需要观察的现象**：循环状态是**常数**（与 prompt 长度无关），KV 页随长度线性增长；在 4B 的 24:8 层配比下，短上下文时循环状态占比反而更高。
4. **预期结果**：写出两条曲线的交点估计，并回答「混合架构在什么上下文长度下显存开始占优」。
5. 数值 (b) 依赖具体 checkpoint 配置，**待本地验证**（用你的 `models/Qwen3.5-4B/config.json` 的 `num_key_value_heads`、`head_dim`、`linear_*` 字段代入重算）。

#### 4.3.5 小练习与答案

**练习 1**：前缀缓存快照为什么选 256-token 边界而不是任一 token 位置？
**答案**：状态只在「完整的整模型 chunk」边界上是一致的（设计文档明确：边界是完成的 whole-model chunk，不是 GDR 内部的 64-token tile）。调度器以 1024 token 为预算分块预填充，256 是它的约数且是 GDR chunk（64）的倍数，能保证任意调度切法都落在可快照的边界上。

**练习 2**：如果只回放 KV 页、不恢复循环状态就继续解码，会发生什么？会崩吗？
**答案**：大概率不崩，但输出是错的。全注意力层拿回了正确历史，线性层的 \( S \) 却从零（或旧值）开始，等于让模型「失忆」24 层——错误是语义级的、静默的。这正是设计文档坚持「两者一起检查、一起恢复」的原因，也是为什么回放类请求（echo/prompt-logprob）在第一版设计里干脆留在冷预填充路径上。

### 4.4 TP 执行器与调度器的 TP 半边

#### 4.4.1 概念说明

Qwen3.5 的 TP **复用 Qwen3 的 controller/worker 运行时形状**（见 `docs/models/qwen35/tp-design.md` 的 TL;DR：Phase 1/2a/2b/2c 已实现——eager 稠密 TP、混合步 unified 执行、线性注意力/GDR 状态分片、以及门控在「已编译解码 GQA 组」上的解码 CUDA Graph）。与单卡执行器（`executor.rs` 的 `Qwen35Executor`，调度器线程直接调用模型）相比，结构差异是：

| 维度 | 单卡执行器 | TP 执行器 `Qwen35TpExecutor` |
| --- | --- | --- |
| 驱动方式 | 调度器直接调用前向 | controller（rank 0 视角）向每个 rank 的 worker 线程广播命令 |
| 状态归属 | 调度器持有 `kv`+`rec` | worker 各自持有 rank 本地 `kv`+`rec`；调度器只持 `RequestId` |
| 线性层状态 | 单份 | 每 rank 一份（value 头切分），从不 all-reduce |
| 采样 | 调度器线程 | 仅 rank 0 做批量采样，结果以命令序返回 |
| CUDA Graph | 槽位在本进程图状态里 | 启动期全 rank 预捕获扫描（Warmup→Capture→Launch→Finalize） |
| 显存容量 | — | 容量数学按「每请求循环状态字节」迭代收敛 |

#### 4.4.2 核心流程

一次 TP 解码步：

```
调度器 tp_decode_items(active)          ← 按 dense 槽位序构造 TpDecodeStepItem
controller dispatch_mutating(RunDecodeStep{.., start_gate})
  ├─ 每个 worker 线程 wait(start_gate)   ← 全 rank 同一起跑线
  ├─ eager:  decode_pointer_tables.refill_from_recurrent_refs()  (H2D)
  │          model.batch_decode_eager_logits(...)                (内部含 MLP all-reduce)
  └─ graph:  首解码行 copy_state_to_slot(rec → slot_states[slot])，之后整图回放
rank≠0: 返回空
rank 0:  sample_decode_rows → select_batch(logits, ...)          ← 采样只在这里
controller recv_runtime_responses × world_size → 校验 → DecodeResult
```

调度器每步的预填充预算：`take_prefill_chunks` 把等待队列前面的请求切出 ≤ `max_prefill_tokens`（默认 1024）的一批；TP 模式下再由 `tp_prefill_items` 翻译成带采样参数的 chunk 条目。

#### 4.4.3 源码精读

模块文档一句话定调：[pegainfer-qwen35/src/tp_executor.rs:1-5](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/tp_executor.rs#L1-L5)——「每步一条 canonical eager unified 命令；线性注意力/GDR 权重与状态按 rank 分片；解码行每 rank 一次批量前向 + rank-0 一次批量采样」。

命令协议：[pegainfer-qwen35/src/tp_executor.rs:89-141](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/tp_executor.rs#L89-L141)——`TpWorkerCommand` 枚举覆盖 RunPrefillChunks / RunDecodeStep / RunUnifiedStep / DropRequest（带 `TpSlotCompaction` 槽位搬移）与启动期的 Precapture。每个命令都带 `start: Arc<TpCommandStartGate>`：[pegainfer-qwen35/src/tp_executor.rs:178-212](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/tp_executor.rs#L178-L212) 的门是一个 Pending/Execute/Cancel 三态 condvar，controller 解析决定后所有 worker 同时放行——这是「全 rank 执行同一有序命令序列」的同步点。

worker 侧请求状态与容量数学：[pegainfer-qwen35/src/tp_executor.rs:1295-1309](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/tp_executor.rs#L1295-L1309)——`TpRequestState { kv, recurrent: Option<RecurrentState> }` 的注释交代了图模式的搬家规则：首个解码行时 prefill 私有状态搬进槽位后置 `None`，eager 路径全程持有。[同文件:1343-1378](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/tp_executor.rs#L1343-L1378) 的容量循环值得精读：图模式要先为槽位桶预留 `bucket × recurrent_bytes`，再算有效批量，且必须按**有效批量的桶**（而非请求批量）迭代收敛——注释解释了否则紧凑显存的 rank 会被饿到零容量。

**#1046 更新点——采样 scratch 携带可解码宽度**：[pegainfer-qwen35/src/tp_executor.rs:1397-1403](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/tp_executor.rs#L1397-L1403)。此处原来是 `SampleScratch::new(ctx, selection_vocab, max_batch)`；#1046 把 logits GEMM 宽度对齐到 248192 后，采样路由阈值 `top_p <= 1/vocab` 若仍按 arena 宽度算会从 1/248077 漂移到 1/248192，把一个本应走确定性 argmax 的请求推进 rejection sampler（bf16 平局时会随机挑选）。修复是让 scratch 同时携带两个宽度：`with_selection_width(arena=selection_vocab, 路由宽度=decodable_vocab)`。采样侧的落地见 [pegainfer-sample/src/lib.rs:93-115](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-sample/src/lib.rs#L93-L115)（`new` 退化为 `with_selection_width(vocab, vocab)`）与 [pegainfer-sample/src/lib.rs:217-225](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-sample/src/lib.rs#L217-L225)（`effectively_greedy(p, scratch.selection_width)`——路由按语义宽度度量）。同一改动也出现在单卡路径的 [pegainfer-qwen35/src/decode_buffers.rs:119-124](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/decode_buffers.rs#L119-L124) 和 unified 路径（`unified_forward.rs`），logits arena 本体则按 `selection_vocab` 分配（[pegainfer-qwen35/src/decode_buffers.rs:93](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/decode_buffers.rs#L93)）。**完整机制（pad 行 -inf 抑制、两条门禁测试、为何 HF golden 门禁看不见）在 u8-l7 专讲。**

eager 解码步：[pegainfer-qwen35/src/tp_executor.rs:1705-1782](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/tp_executor.rs#L1705-L1782)——先解析命令序到槽位映射，收集 `kv_refs`/`recurrent_refs`，4.3 的 `refill_from_recurrent_refs` 刷新指针表（注释强调「分配一次、每步只 H2D，swap_remove 退役不会留下被寻址的陈旧行」），一次 `batch_decode_eager_logits`，然后 `rank != 0` 直接返回空、rank 0 走 `sample_decode_rows`（[同文件:2192-2211](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/tp_executor.rs#L2192-L2211)，文档注释即「rank-0 sampling pass」）。

图模式解码步：[pegainfer-qwen35/src/tp_executor.rs:1784-1864](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/tp_executor.rs#L1784-L1864)——强制 dense 槽位序（`slot_idx == row`）；请求首次进入槽位时把 prefill 私有的循环状态 `take()` 出来，`graph_state.copy_state_to_slot` D2D 拷入固定地址槽位（4.3.3 的图槽位机制在 TP 下的翻版），之后每步纯回放。

启动期预捕获扫描：[pegainfer-qwen35/src/tp_executor.rs:58-76](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/tp_executor.rs#L58-L76)——`PrecapturePhase` 四相位的注释是理解 TP 图捕获的钥匙：**Warmup**（每个桶消息大小先做一次 eager all-reduce，让 NCCL 选好算法建好连接，之后 `cuStreamBeginCapture` 录到的才不会在回放时重新握手）→ **Capture**（录制+实例化+上传，不 launch）→ **Launch**（纯入队）→ **Finalize**。注释解释了为什么捕获与启动必须分相位：被捕获集合通信的首次启动会阻塞对端，与对端的捕获/实例化/上传（争驱动锁、分配显存）重叠会死锁驱动。所以「每个 rank 先全部捕获完一个桶，任何 rank 才启动它」。

调度器的 TP 半边：[pegainfer-qwen35/src/scheduler/tp.rs:34-59](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/scheduler/tp.rs#L34-L59) 的 `tp_decode_items` 从 `ActiveBackendState::Tp { request_id, slot_idx }` 构造命令条目并 `debug_assert` 槽位稠密；文件开头的 [tp.rs:1-2](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/scheduler/tp.rs#L1-L2) 注释说明拆分动机（把 TP 簿记从调度器「上帝模块」里拿出来）。TP 的前缀/解码结果都按 `RequestId` 集合严格对齐（[tp.rs:126-177](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/scheduler/tp.rs#L126-L177)，未知 id、重复 id、缺失 id 三种失配全部报错）。

调度器每步预填充预算：[pegainfer-qwen35/src/scheduler/mod.rs:1620-1636](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/scheduler/mod.rs#L1620-L1636)——`take_prefill_chunks` 从队首取一批、按预算切 chunk、写 `step_chunk`；`DEFAULT_MAX_PREFILL_TOKENS = 1024` 在 [同文件:283](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/scheduler/mod.rs#L283)。这层 1024 的调度分块与 4.2 的 20000 内核保险丝是两回事，不要混淆。

#### 4.4.4 代码实践

1. **实践目标**：亲眼看到 #1046 在 TP 执行器的这一行改动，并能向别人解释它为什么必须存在。
2. **操作步骤**：(a) 运行 `git show 72cbbe8a -- pegainfer-qwen35/src/tp_executor.rs`（只读命令），观察 diff：`SampleScratch::new` → `SampleScratch::with_selection_width`，多传一个 `model.config().decodable_vocab`。(b) 打开 `pegainfer-sample/src/lib.rs` 的 217-225 行，找到 `effectively_greedy(p, scratch.selection_width)`。(c) 回答：如果没有这个参数，`select_batch` 会拿什么当分母？（提示：`let vocab = logits.hidden_dim`，即 arena 宽度 248192。）
3. **需要观察的现象**：diff 只有 3 行——「arena 宽度（几何）」与「路由宽度（语义）」的分离在调用侧只体现为多传一个数字。
4. **预期结果**：你能写出这个推理链——GEMM 对齐 ⇒ arena 变宽 ⇒ 若路由仍按 arena 算，`top_p` 落在 \( (1/248192,\, 1/248077] \) 的请求从 argmax 路径掉进 rejection sampler ⇒ bf16 平局时输出不再确定。
5. **待本地验证**：运行 `pegainfer-sample` 的新门禁测试 `padded_arena_width_does_not_suppress_the_argmax_routing` 需要 GPU，本环境无法执行。

#### 4.4.5 小练习与答案

**练习 1**：TP 下为什么 MLP 需要每层 all-reduce，而线性层状态完全不用通信？
**答案**：MLP 的 gate/up 按行切（输出维），down_proj 按列切（输入维），列切产生部分和，必须 all-reduce（4.2.3 的 `all_reduce_hidden`）。线性层的 value 头整体切给各 rank——每 rank 拥有不同头的完整 \( S \)，拼起来就是全局状态，天然无重叠，也就无需通信。这正是 `recurrent_state.rs` 模块文档「never all-reduced」的含义。

**练习 2**：`TpSlotCompaction`（请求退役时的槽位搬移）为什么必须由**调度器**先做簿记、worker 只是「应用并校验」？
**答案**：调度器是请求生命周期的唯一事实来源（哪个请求占哪个槽位决定了下一步命令的 dense 行序）。若让各 worker 自行决定搬移，多 rank 之间可能瞬间不一致；把决定放在 controller 侧、worker 应用时校验占用是否匹配（不匹配即 fail/poison 执行器），保证了「每个 rank 运行同一有序命令序列」这条 TP 不变量。

### 4.5 Triton AOT：chunkwise 内核如何进入构建

#### 4.5.1 概念说明

回顾 u4-l2 的结论并精确化：**qwen35 的 FFI 符号分两族**——解码/conv/全注意力内核是 `csrc/qwen35/*.cu` 的原生 CUDA，**无条件编译**（默认构建也含它们，只是没有模型 crate 去 `--features qwen35` 使用）；chunkwise 预填充七阶段内核是**构建期 Triton AOT** 生成的，`#[cfg(feature = "qwen35")]` 门控——不开 feature 这些符号不存在，且构建需要 Python + Triton（`PEGAINFER_TRITON_PYTHON` 或 `.venv/bin/python`）。这就是 CLAUDE.md 说 qwen35「needs build-time Python + Triton」的确切含义：**构建期**用 Python，**运行期**仍然零 Python。

Triton 源码改编自 FLA（flash-linear-attention），pegainfer 侧的改动写在了文件头注释里：固定 Qwen3.5 维度（batch=1、K=V=128、chunk=64，头数为运行时参数）、去掉 backward/varlen/通用 autotune、解码兼容的终态布局 \([H,V,K]\)、以及融合的 prepare 阶段。

#### 4.5.2 核心流程

```
cargo build --features qwen35
  └─ pegainfer-kernels/build.rs
       ├─ csrc/qwen35/*.cu ──nvcc──► libkernels_cuda.a（无条件）
       └─ (feature qwen35) 对每个 TritonKernelSpec：
            python gen_triton_aot.py <spec>  ──►  cubin + C 启动器
            write_wrapper(...)               ──►  extern "C" gated_delta_rule_prefill_chunk_*_cuda
       rustc 链接 ──► pegainfer_kernels::ffi::qwen35 的 cfg(feature="qwen35") 块可解析
  └─ pegainfer-qwen35/src/recurrent.rs 调用 ffi::gated_delta_rule_prefill_chunk_*_cuda
```

#### 4.5.3 源码精读

FFI 的两族分界：[pegainfer-kernels/src/ffi/qwen35.rs:1-9](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-kernels/src/ffi/qwen35.rs#L1-L9) 的文件头注释说明符号来源 `csrc/qwen35/*.cu`；首个 `unsafe extern "C"` 块（如 [同文件:62-92](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-kernels/src/ffi/qwen35.rs#L62-L92) 的 GDR 解码双符号）**没有 cfg**，而 [同文件:118-139](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-kernels/src/ffi/qwen35.rs#L118-L139) 起的第二个块带 `#[cfg(feature = "qwen35")]`，注释直说：「chunk-wise GDR prefill 内核是构建期 Triton AOT 生成的；`qwen35` feature 就是把 Python+Triton 拉进构建的东西，没有它这些符号不存在」。还要注意返回值差异：原生解码符号返回 `void`（视为不可失败，u4-l3 的约定），Triton AOT 符号返回 `CUresult`（`result.result()?` 检查）。

构建期的 spec 与包装生成：[pegainfer-kernels/build.rs:1300-1326](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-kernels/build.rs#L1300-L1326)——`gdr_prepare_spec` 给出 kernel 路径、Triton 签名（`*bf16,*bf16,...,128,128` 末尾两个 constexpr）、grid 表达式与 warp/stage 配置；`generate_triton_artifacts` 产出 cubin 与 C 启动器，`write_wrapper` 再生成一层薄薄的 C 包装，把 TVM 风格的 `(CUstream, CUdeviceptr...)` 签名翻译成 `gated_delta_rule_prefill_chunk_prepare_cuda(const uint16_t*, ..., CUstream)`——也就是 4.2 里 Rust 侧调用的那个符号名。七阶段各有自己的 spec，串起来正好覆盖 4.2.3 的七次调用。

Triton 源码头：[pegainfer-kernels/tools/triton/gated_delta_rule_chunkwise_kernels.py:1-25](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-kernels/tools/triton/gated_delta_rule_chunkwise_kernels.py#L1-L25)——列出 FLA 上游参考文件（`chunk.py`、`chunk_delta_h.py`、`chunk_o.py`、`wy_fast.py`）与 pegainfer 的改动清单。想理解七阶段的数学细节，这个文件是唯一权威。构建通道的 README：[pegainfer-kernels/tools/triton/README.md:1-10](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-kernels/tools/triton/README.md#L1-L10)。

#### 4.5.4 代码实践

1. **实践目标**：建立「七阶段 ↔ 七个 TritonKernelSpec ↔ 七个 FFI 符号」的完整映射，确认你理解 AOT 产物如何变成可调用符号。
2. **操作步骤**：在 `build.rs` 中 grep `TritonKernelSpec`（约 1300 行起有 7 个构造），把每个 spec 的 `kernel_name`、`out_name` 与 `ffi/qwen35.rs` cfg 块里的 `gated_delta_rule_prefill_chunk_*_cuda` 符号一一对应；再对照 `recurrent.rs:472-547` 的调用顺序排列。
3. **需要观察的现象**：每个 spec 的 `signature` 末尾都带 constexpr（如 `128,128` 或 `64`），对应 Triton kernel 的 `tl.constexpr` 参数；wrapper 只是签名翻译，不含任何计算。
4. **预期结果**：一张 7 行映射表；并能回答「删掉 `--features qwen35` 重建，哪些符号消失、哪些仍在」。
5. **待本地验证**：实际跑一次 `cargo build --release --features qwen35` 并用 `nm` 查 `libkernels_cuda.a` 中的符号（需要 CUDA 工具链 + Python/Triton，本环境不可用）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 chunkwise 预填充选 Triton AOT，而解码内核手写 CUDA？
**答案**：chunkwise 是块内小矩阵运算（求逆、前缀和、WY 变换），逻辑密集、形状固定，Triton 表达力和开发效率优势大，且只在预填充期跑、启动开销可摊销；解码内核每步每层都要 launch，是延迟敏感热路径（decode-kernel-attribution.md 正在逐个优化它），手写 CUDA 便于控制指针表间接寻址与融合度。AOT（而非运行时 JIT）则保证了运行期零 Python 的项目铁律。

**练习 2**：`GDN_AOT_KEY_HEAD_DIM` / `GDN_AOT_VALUE_HEAD_DIM`（`recurrent.rs:80-81` 断言 key_dim/val_dim 必须等于它们）体现了一条什么构建哲学？
**答案**：把形状固化进编译产物（Triton constexpr、AOT 常量），运行时只校验、不泛化。收益是内核可以按固定 tile 深度优化；代价是 config 里不一致的维度必须在加载期拒绝（`Config35` 校验注释里说的「静态、加载期不变量」），而不是等到第一次 kernel launch 才炸。

## 5. 综合实践

**任务：写一份「全注意力（Qwen3）vs 混合注意力（Qwen3.5）」的调度影响清单。**

这是本讲的收官作业，产出一张可以长期维护的对照表。建议按以下维度展开（每行都注明两侧的源码证据）：

1. **状态清单**：Qwen3 每请求只有 KV 页（`pegainfer-qwen3` 的调度器，参见 u6-l1）；Qwen3.5 每请求 = KV 页（8 个全注意力层）+ `RecurrentState`（24 个线性层，f32 循环状态 + bf16 conv 状态，常量约 49 MiB）。证据：`scheduler/mod.rs:96-125` 的两个 backend 枚举 vs Qwen3 的对应结构（u6-l1 讲义）。
2. **准入的容量约束**：Qwen3 的准入看 KV 页余量；Qwen3.5 的 TP worker 容量数学把 `RecurrentState::allocation_bytes` 当一等公民（`tp_executor.rs:1343-1378`）——每请求显存下限不再是「一个 KV 页」而是「49 MiB 状态 + 首页」。
3. **预填充缓冲**：Qwen3 的分块预填充只受注意力 scratch 约束；Qwen3.5 多了随 pass 长度线性增长的 `GdrChunkwiseScratch35`，因此有 `PREFILL_CHUNK_LEN=20000` 内核保险丝 + 调度器 1024 预算的双层钳制（`prefill.rs:6-20`、`scheduler/mod.rs:283`）。
4. **断点续算（前缀命中）条件**：Qwen3 命中 = 内容哈希的 KV 块；Qwen3.5 命中 = 同一 256-token 边界上的 KV **加** 循环/conv 快照（`docs/models/qwen35/prefix-cache.md`，设计阶段）。在表里明确标注「Qwen3.5 共享前缀缓存未落地」这一现状。
5. **图捕获的指针稳定面**：Qwen3 图固定的是激活/KV 缓冲；Qwen3.5 还要固定循环状态地址——`slot_states` + `copy_state_to_slot`/`move_slot_within`（`batch_decode_graph.rs:36-53`），TP 下再叠加预捕获扫描的四相位纪律（`tp_executor.rs:58-76`）。
6. **退役语义**：Qwen3 退役还页即可；Qwen3.5 退役要同时释放 KV 与循环状态（TP 下每个 rank 按 `RequestId` 释放，且图模式伴随 `TpSlotCompaction` 槽位搬移）。

验收标准：拿你的表给一个没读过 qwen35 代码的同事看，他们应能只凭表回答两个问题——「为什么混合模型的每请求显存下限更高」和「为什么它的前缀缓存更难做对」。

## 6. 本讲小结

- Qwen3.5-4B 是 24 层 Gated DeltaNet 线性注意力 + 8 层全注意力的混合体；`layer_types` 配置驱动逐层运行时分派，两类状态（分页 KV 与每请求 `RecurrentState`）并存。
- GDR 解码是 O(1) 单步递推（原生 CUDA、指针表批量）；预填充是 chunk_size=64 的七阶段 chunkwise 流水线（Triton AOT 生成，改编自 FLA），`chunk_state` 缓冲天然物化每块边界的循环状态快照。
- 每请求循环状态约 49.125 MiB 且与上下文长度无关；TP 下按 value 头切分、每 rank 一份、从不 all-reduce；图模式搬进固定地址槽位保证回放指针稳定。
- 前缀缓存命中必须「KV 与循环/conv 快照同边界同时存在」——只回放 KV 不崩溃但静默出错；当前共享缓存是设计文档状态，尚未落地。
- TP 执行器复用 Qwen3 的 controller/worker 广播形状：start gate 对齐起跑、rank-0 独占采样、预捕获扫描四相位（Warmup/Capture/Launch/Finalize）避免 NCCL 死锁；调度器只持逻辑 `RequestId`。
- #1046 之后，TP/单卡/unified 三条路径的采样 scratch 都经 `SampleScratch::with_selection_width(selection_vocab, decodable_vocab)` 构造：logits arena 按 tile 对齐宽度分配，argmax-vs-sample 路由按可解码宽度判定——细节在 u8-l7。

## 7. 下一步学习建议

- **下一讲 u8-l2（Gemma 4）**：另一条「双族 KV」路线——Gemma 4 用滑动窗+全局两种**全注意力**族共存于一个模型，对比 Qwen3.5 的「线性+全注意力」混合，体会「混合状态管理」这个设计空间的两种答案。
- **若要深挖 #1046 的词表宽度机制**：直接跳到 u8-l7（Qwen3.5 logits 宽度对齐），配套阅读 [docs/models/qwen35/decode-kernel-attribution.md](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/docs/models/qwen35/decode-kernel-attribution.md)（nsys 内核归因的四条发现）与 `pegainfer-sample/tests/select_batch.rs` 的两道新门禁。
- **若要继续 TP 主题**：`docs/models/qwen35/tp-design.md` 与 `tp-implementation.md` 记录了 Phase 1→2c 的完整落地史；对照 u9-l4（Qwen3 的 RankWorker/StepCommand）看「复用同一运行时形状」具体复用了什么。
- **若要读懂 chunkwise 数学**：从 `tools/triton/gated_delta_rule_chunkwise_kernels.py` 的 FLA 上游引用（`chunk.py`、`wy_fast.py`）进入 flash-linear-attention 的文档，再回来按七阶段顺序读内核。
