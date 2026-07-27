# MLA 注意力模块与稀疏选择（SparseSelect vs Pure）

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 DeepSeek-V3.2 的注意力（MLA）在 TileRT 的 8 张卡上被**非对称**地拆成了哪两类模块：`SparseSelectMlaV2`（device 0）与 `PureMlaV2`（device 1..7）。
- 复述 device 0 在 NSA（原生稀疏注意力）里承担的「选位置」职责，以及它如何把选中的位置（`IDX_SELECTS`）广播给其余 7 张卡。
- 读懂 `mla_v2.py` 里两个类各自注册的子算子顺序，以及每层 3 个缓存 `ki_cache / kv_cache / pe_cache` 的形状与含义。
- 解释 `peer_bufs / ll_buf / partial_buf` 这三块缓冲为何**只在特定卡**上存在，以及它们如何用「直接写对端显存地址」的 V2 P2P 模型替代传统 allreduce。

## 2. 前置知识

本讲建立在 u2-l1、u2-l4、u2-l5 之上，默认你已经掌握：

- **TileRTModule / SerializableTileRTModule 抽象**（u2-l1）：算子用 `register_op` 装进容器，`get_weights_list / get_cache_vars` 递归聚合出交给后端的扁平张量列表；权重靠 `ref_weights_alias`（HF 长名）与 `tilert_weights_alias`（TileRT 短名）两套别名驱动。
- **Dsa 容器的层循环**（u2-l4）：`for layer_idx in range(n_layers)`，前 3 层用 `MlpBlock`、其余 58 层用 `MoeBlock`，每块都是「一个 MLA 注意力 + 一个 FFN」，键名模板 `layer_{层号}_{短别名}_dev_{卡号}`。
- **四元张量契约与 Idx 索引**（u2-l5）：后端执行靠 `params / temp_vars / caches / profile_logs` 四组扁平张量列表，`temp_vars[Idx.Q]` 这样的命名下标等价于一个固定整数下标，Python 与 `.so` 必须逐字段一致。

本讲用到几个模型概念，先用大白话解释：

- **MLA（Multi-head Latent Attention，多头潜注意力）**：DeepSeek-V3.2 的注意力变体。它不把每层的 K/V 直接存成「头数 × 头维度」的大矩阵，而是先把它们压到一个低秩的「潜向量」（`kv_lora_rank=512`）里缓存，注意力计算时再用 `kv_b` 权重把潜向量重新展开成多头的 K/V。这样 KV 缓存很小，是 MLA 省显存的关键。
- **NSA（Native Sparse Attention，原生稀疏注意力）**：传统注意力对历史所有位置都算一遍，序列一长就慢。NSA 多了一组「索引头」，先用索引头给历史位置打分，挑出最重要的 `index_topk=2048` 个位置，再做真正的注意力。这样注意力开销不随序列长度线性增长。
- **TP（Tensor Parallel，张量并行）头切分**：把 128 个注意力头切到多卡上，每卡只算自己那一份头。本讲会看到 128 个头在 device 0 与 device 1..7 上的切法**不一样**。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tilert/models/deepseek_v3_2/modules/mla_v2.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/mla_v2.py) | 本讲主角。定义 `SparseSelectMlaV2`（device 0）与 `PureMlaV2`（device 1..7）两个 MLA 容器，组装各自的子算子、声明通信缓冲与缓存。 |
| [tilert/models/deepseek_v3_2/modules/dsa.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/dsa.py) | `Dsa` 容器按 `device_id` 选择 MLA 类、创建 `peer_bufs/ll_buf/partial_buf`，并决定 `mla_num_devices`。 |
| [tilert/models/deepseek_v3_2/modules/end2end.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py) | 8 线程权重加载后，主线程把各卡 `ll_buf` 的显存地址回填进 device 0 的 `peer_bufs`，完成 V2 P2P「通讯录」交换。 |
| [tilert/models/deepseek_v3_2/temp_var_indices.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/temp_var_indices.py) | `Idx` 枚举给 MLA 用到的激活槽（`Q/KV/KI/IQ/IDX_SCORES/IDX_LOGITS/IDX_SELECTS/...`）命名。 |
| [tilert/models/deepseek_v3_2/model_args.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/model_args.py) | 决定 MLA 所有张量形状的超参（`q_lora_rank / kv_lora_rank / qk_nope_head_dim / index_topk` 等）。 |
| [tilert/models/deepseek_v3_2/ops/](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/) | MLA 子算子的具体实现（`rmsnorm_projx_wqakis`、`rmsnorm_projq_wqi`、`layernorm_rope_rotate`、`projx_wis`、`rmsnorm_projx_wqkva`、`rmsnorm_projq_wqb`、`rmsnorm_kv`、`projq_wqb`、`projo_wkvb`、`unproj_o_allreduce`）。 |

## 4. 核心概念与源码讲解

### 4.1 SparseSelectMlaV2：device 0 的 NSA 稀疏索引职责

#### 4.1.1 概念说明

NSA 的核心是「先选位置，再做注意力」。如果每张卡都各自算一遍「该选哪些历史位置」，不仅重复劳动，更致命的是**8 张卡可能选出不同的位置**，导致注意力结果无法对齐合并。

TileRT 的解法是**非对称分工**：让 device 0 独自承担「选位置」这件事——它跑完整的 NSA 索引头，计算出本次应该关注历史序列中的哪 `index_topk=2048` 个位置，把结果（`IDX_SELECTS`）广播给其余 7 张卡；其余 7 张卡只做「按 device 0 指定的位置做真注意力」。device 0 的这个角色由 `SparseSelectMlaV2` 实现——名字里的 **SparseSelect** 就是「稀疏选择」。

device 0 同时也要算自己那份注意力（它也是 8 卡注意力的一员），所以它仍然是一个 MLA，只是**额外**背着索引头的职责和两块通信缓冲（`peer_bufs / partial_buf`）。

#### 4.1.2 核心流程

`SparseSelectMlaV2` 的子算子链产出 NSA 选择所需的全部中间量：

```text
hidden x (dim=7168)
   │
   ├─① rmsnorm_projx_wqakis  ──► Q        (q_lora_rank=1536)   到达 query 的低秩空间
   │                          ──► KI       (index_head_dim=128) 索引键
   │                          ──► IDX_SCORES(idx_n_heads=64)    索引头的初步打分
   │
   ├─② rmsnorm_projq_wqi     ──► IQ       (64 头 × 128 维)     把 Q 投影成「索引查询」
   │
   ├─③ layernorm_rope_rotate ──► KI_cache (写入 ki_cache)      对索引键做 LayerNorm+RoPE+rotate
   │
   └─④ projx_wis             ──► IDX_SCORES(64)                索引头打分（用于选位置）

   后端 C++（不在本文件）：
   IQ + KI_cache + IDX_SCORES  ──► IDX_LOGITS (每个历史位置的分) ──► IDX_SELECTS (top-2048 位置)
```

注意 `IDX_SELECTS`（被选中位置的索引，int32）才是要广播给其余卡的东西；它形状是 `[bs, seq, index_topk=2048]`，与序列长度无关——这正是 NSA 把注意力开销「钉死」在约 2k 的关键（见 u2-l2）。

> 说明：`rmsnorm_projx_wqakis` 的类名拆开是 `RMSNorm + Proj(x) + W_q_a + W_ki + W_is`，即一次 RMSNorm 后融合地用 `q_a`、`wk`（索引键）、`wis`（索引打分）三组权重做 GEMV。`projx_wis` 是独立的 `W_is` 投影，与融合算子里的 `wis` 分支对应同一份 HF 权重 `self_attn.indexer.weights_proj.weight`。融合路径的「真实落点」在后端 `.so` 里，Python 侧的 `golden_forward` 主要用于数值对拍。

#### 4.1.3 源码精读

`SparseSelectMlaV2` 在构造时按顺序注册 4 个子算子，并接收两块通信缓冲：

[mla_v2.py:33-66](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/mla_v2.py#L33-L66) 定义类与注册 4 个算子：`rmsnorm_projx_wqakis`（融合投影，产出 Q/KI/IDX_SCORES）、`rmsnorm_projq_wqi`（设为 `BF16MMA`，产出索引查询 IQ）、`layernorm_rope_rotate`（对索引键做 LN+RoPE）、`projx_wis`（索引打分）；末尾把外部传入的 `peer_bufs / partial_buf` 存为成员。

子算子的算法与权重别名，本讲不逐行展开，只点名用途，便于你建立「这个 op 对应 NSA 的哪一步」的映射：

- [rmsnorm_projx_wqakis.py:58-66](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/rmsnorm_projx_wqakis.py#L58-L66)：ref 别名 `input_layernorm.weight / self_attn.q_a_proj / self_attn.indexer.wk / self_attn.indexer.weights_proj`——这正是 NSA 索引器的入口权重，确认 device 0 才持有索引器。
- [rmsnorm_projq_wqi.py:151-164](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/rmsnorm_projq_wqi.py#L151-L164)：ref 别名 `self_attn.indexer.wq_b`，把 Q 投影成多索引头的查询 IQ。
- [layernorm_rope_rotate.py:86-98](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/layernorm_rope_rotate.py#L86-L98)：ref 别名 `self_attn.indexer.k_norm.weight/bias`，对索引键做归一化 + RoPE 后写入 `ki_cache`。
- [projx_wis.py:43-54](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/projx_wis.py#L43-L54)：ref 别名 `self_attn.indexer.weights_proj.weight`，产出 64 个索引头的打分。

通信缓冲在 `get_weights_list` 里被当作「权重」一并交给后端（它们要在 CUDA Graph 捕获时固化地址）：

[mla_v2.py:72-91](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/mla_v2.py#L72-L91) `peer_bufs` 默认形状 `[num_devices-1]` 的 int64（存放其余 7 张卡的 `ll_buf` 显存地址），`partial_buf` 默认形状 `[max_batch_size, 4, dim]` 的 bf16（`4 = num_mtp+1`，即一次 forward 的序列长度）。两者都被 `append` 进权重列表。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：确认 device 0 持有 NSA 索引器、而 device 1..7 不持有。
2. **步骤**：在 `SparseSelectMlaV2.__init__` 里列出 4 个算子用到的 HF 权重键（`self_attn.indexer.*`）；再打开 `PureMlaV2.__init__`（见 4.2）对照，看后者是否出现任何 `indexer.*` 权重。
3. **观察**：你会发现 `indexer.wk / wq_b / weights_proj / k_norm` **只**出现在 `SparseSelectMlaV2` 链路里。
4. **预期结论**：NSA「选位置」所需的全部权重只在 device 0，从权重层面印证了「device 0 独自负责稀疏选择」。
5. 运行命令不涉及 GPU：`grep -rn "indexer\." tilert/models/deepseek_v3_2/ops/` 自行核对命中文件即可。

#### 4.1.5 小练习与答案

**练习 1**：`IDX_SELECTS` 的形状是 `[bs, seq, 2048]`，为什么末维是固定的 2048 而不是「当前序列长度」？

**参考答案**：因为 NSA 只挑 `index_topk=2048` 个最重要的历史位置做真注意力（见 `model_args.index_topk`，u2-l2 已说明）。无论序列多长，真注意力的范围被钉死在约 2k，所以广播给其余卡的选择结果末维恒为 2048，注意力开销不随序列长度线性增长。

**练习 2**：如果让 8 张卡各自独立做稀疏选择，会出现什么问题？

**参考答案**：一是 8 倍重复计算；更严重的是各卡可能选出**不同**的位置，导致各卡注意力作用域不一致，跨卡 allreduce 合并出来的结果在数学上失去意义。所以必须由 device 0 统一选择、统一广播。

---

### 4.2 PureMlaV2：device 1..7 的真注意力与 0 卡广播

#### 4.2.1 概念说明

device 1..7 用 `PureMlaV2`——名字里的 **Pure** 指「纯粹的 MLA 注意力」，不带 NSA 索引头。它们不选位置，而是**消费** device 0 广播过来的 `IDX_SELECTS`，只对被选中的那些位置做 MLA 注意力。

「0 卡广播」是本模块的关键词。它不是用 NCCL allreduce 那样的集合通信，而是 TileRT 自有的 **V2 P2P** 模型：device 0 握有一本「通讯录」`peer_bufs`（记录其余 7 张卡接收缓冲 `ll_buf` 的显存地址），直接把 `IDX_SELECTS` **写进**对端显存。这样省掉了集合通信的同步开销，更贴合 tile 级运行时「极致重叠」的目标（见 u1-l1）。

#### 4.2.2 核心流程

`PureMlaV2` 的子算子链是标准 MLA 的「压-算-展」三段：

```text
hidden x (dim=7168)
   │
   ├─① rmsnorm_projx_wqkva  ──► Q  (q_lora_rank=1536)
   │                        ──► KV (kv_lora_rank=512)   压缩到潜向量
   │                        ──► PE (qk_rope_head_dim=64) 解耦 RoPE 部分
   ├─② rmsnorm_projq_wqb    ──► Q_NOPE + Q_PE           Q 展开成多头（本卡 n_local_heads 个头）
   ├─③ rmsnorm_kv           ──► kv_cache                对潜 KV 做 RMSNorm 写缓存
   │
   │  ── 后端按 device 0 广播的 IDX_SELECTS 做稀疏 MLA 注意力 ──► O（潜空间）
   │
   ├─④ projq_wqb            ──► Q_NOPE_DOWN             用 wkv_b1 把 Q_NOPE 投到潜空间
   ├─⑤ projo_wkvb           ──► PROJ_O                  用 wkv_b2 把 O 展开到 v_head_dim
   └─⑥ unproj_o_allreduce   ──► UNPROJ_O (dim=7168)     升回 hidden 维 + 跨卡 allreduce
```

注意头切分的非对称：device 0 有 `n_local_heads = n_heads // num_devices = 128/8 = 16` 个头；而 device 1..7 是 7 卡分 128 个头，128 不能被 7 整除，所以要**补齐对齐**。这正是 `get_temp_vars` 里对 `n_local_heads` 做特判的原因（见 4.3.3）。

#### 4.2.3 源码精读

`PureMlaV2` 注册 6 个子算子，并接收一块 `ll_buf` 接收缓冲：

[mla_v2.py:116-163](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/mla_v2.py#L116-L163) 依次注册 `rmsnorm_projx_wqkva`（融合 RMSNorm+三路 GEMV，设为 `DECOUPLED`）、`rmsnorm_projq_wqb`（Q 展多头，设为 `BF16MMA`）、`rmsnorm_kv`（KV 归一化写缓存）、`projq_wqb`、`projo_wkvb`、`unproj_o_allreduce`（设为 `BF16MMA`），末尾存 `ll_buf`。

子算子用途逐一对应：

- [rmsnorm_projx_wqkva.py:240-260](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/rmsnorm_projx_wqkva.py#L240-L260)：ref 别名 `input_layernorm.weight / q_a_proj / kv_a_proj_with_mqa`——把 hidden 压成 Q + 压缩 KV + 解耦 PE，是 MLA 省 KV 缓存的入口。
- [rmsnorm_projq_wqb.py:294-311](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/rmsnorm_projq_wqb.py#L294-L311)：ref 别名 `self_attn.q_a_layernorm.weight / q_b_proj`，把低秩 Q 升回多头并切出 `Q_NOPE / Q_PE`。
- [rmsnorm_kv.py:46-57](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/rmsnorm_kv.py#L46-L57)：ref 别名 `self_attn.kv_a_layernorm.weight`，对压缩 KV 做 RMSNorm 后就地写入 `kv_cache`。
- [projq_wqb.py:219-231](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/projq_wqb.py#L219-L231) 与 [projo_wkvb.py:238-250](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/projo_wkvb.py#L238-L250)：两者 ref 别名都是 `self_attn.kv_b_proj.weight`，但 `projq_wqb` 取 `qk_nope_head_dim` 段、`projo_wkvb` 取 `v_head_dim` 段——同一份 `kv_b` 权重的不同切片分别服务 Q 的下投影与 O 的上投影。
- [unproj_o_allreduce.py:68-80](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/unproj_o_allreduce.py#L68-L80)：ref 别名 `self_attn.o_proj.weight`，融合地把 O 升回 hidden 维并在 7 卡间 allreduce（类名 `UnProjOAllReduce`）。

`ll_buf` 的尺寸与含义：

[mla_v2.py:213-226](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/mla_v2.py#L213-L226) `ll_buf` 默认形状 `[max_seq_len * topk * 2]` 的 int32，其中 `max_seq_len = num_mtp+1 = 4`、`topk = index_topk = 2048`。它是 device 0 写入 `IDX_SELECTS` 的「收件箱」。

> 「0 卡广播」的真正写入发生在后端 C++ 里（device 0 凭 `peer_bufs` 里的地址直接写各卡的 `ll_buf`）。Python 侧能观察到的是：地址在加载阶段被填好（见 4.3），缓冲被注册进权重列表从而在 CUDA Graph 里地址固定。

#### 4.2.4 代码实践（源码阅读 + 思考）

1. **目标**：体会「同一份 `kv_b` 权重被两个算子按不同切片复用」。
2. **步骤**：读 [projq_wqb.py:374-386](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/projq_wqb.py#L374-L386)（取 `wkvb[:, :, :wkvb_nope_head_dim]`）与 [projo_wkvb.py:373-384](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/projo_wkvb.py#L373-L384)（取 `wkvb[:, :, -wkvb_v_head_dim:]`），对照 `device_sharding` 的切分。
3. **观察**：两段切片分别取 `qk_nope_head_dim=128` 的前段与 `v_head_dim=128` 的后段，拼起来正好是 `kv_b_proj` 一行（`wkvb_head_dim = qk_nope_head_dim + v_head_dim = 256`）。
4. **预期结论**：MLA 用一份 `kv_b` 同时承担「把 Q 投到潜空间」和「把 O 从潜空间展开」两个方向，是它参数更省的原因之一。
5. 待本地验证：无 GPU 也能完成上述阅读对照。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `PureMlaV2` 里既没有 `rmsnorm_projx_wqakis` 也没有 `indexer.*` 权重？

**参考答案**：因为「选位置」由 device 0 的 `SparseSelectMlaV2` 独自完成并广播。device 1..7 只接收 `IDX_SELECTS`、对选中位置做真注意力，所以它们不需要索引器，`PureMlaV2` 的算子链里自然没有 `wqakis / wqi / wis` 这些索引相关算子。

**练习 2**：`unproj_o_allreduce` 的名字里为什么带 `AllReduce`？它在哪几张卡之间做归约？

**参考答案**：因为 O 是按头切分到多卡算的，每卡只拿到自己那一份头的部分和，必须跨卡求和才能得到完整输出。它在本卡的 `mla_num_devices` 组内归约——对 device 1..7 而言是 7 卡之间（见 4.3.3 的 `mla_num_devices = num_devices - 1`）。

---

### 4.3 MLA 子算子组装顺序与 KI/KV/PE 缓存契约

#### 4.3.1 概念说明

本模块把视角拉到「组装与契约」层面，回答三个问题：

1. **谁决定用哪个 MLA 类？** `Dsa` 在构造每层 block 时按 `device_id` 注入 `mla_cls`。
2. **通信缓冲在哪创建、何时接线？** `peer_bufs / ll_buf / partial_buf` 在 `Dsa.__init__` 里按卡创建，在 8 线程加载 join 之后再回填地址。
3. **缓存长什么样？** 每层 3 个 bf16 缓存 `ki_cache / kv_cache / pe_cache`，形状由 `ModelArgs` 决定，两个 MLA 类的缓存布局完全一致（都实现 `get_cache_vars` 返回这 3 个）。

理解这三点，就理解了「MLA 在 8 卡上是怎么被拧成一股绳的」。

#### 4.3.2 核心流程

```text
Dsa.__init__（每张卡各跑一次，device_id=0..7）
  │
  ├─ 选类:  mla_cls = SparseSelectMlaV2 if device_id==0 else PureMlaV2
  ├─ 建缓冲:
  │     device 0    → v2_peer_bufs[7] int64, v2_partial_buf[bs,4,dim] bf16
  │     device 1..7 → v2_ll_buf[(num_mtp+1)*index_topk*2] int32
  ├─ 定 mla_num_devices:
  │     device 0    → None（=num_devices=8）
  │     device 1..7 → num_devices-1 = 7   ← 7 卡自成一组做头重分布与 allreduce
  └─ for layer in range(61):
        MlpBlock/MoeBlock(mla_cls=mla_cls, mla_num_devices=..., mla_kwargs={缓冲})

8 线程 _init_weights 并行加载 → join
  │
  └─ V2 P2P 交换（主线程，在 prepare_money 之前）:
        把 device 1..7 各自 v2_ll_buf 的 data_ptr() 填进 device 0 的 v2_peer_bufs
        → device 0 从此「知道」每张卡收件箱的显存地址
```

`prepare_money`（捕获 CUDA Graph）必须排在地址回填**之后**：因为图一旦捕获，缓冲地址就被固化进图里；若先捕获后回填，图里记的是错误地址。

#### 4.3.3 源码精读

**选类与建缓冲**：

[dsa.py:34-56](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/dsa.py#L34-L56) `mla_cls` 三元选择；device 0 建 `v2_peer_bufs`（`[num_devices-1]` int64）与 `v2_partial_buf`（`[max_batch_size, 4, dim]` bf16），其余卡建 `v2_ll_buf`（`[(num_mtp+1)*index_topk*2]` int32）；并设 `mla_num_devices = num_devices - 1`（仅对 device≠0），让 `PureMlaV2` 在 7 卡组内做头切分。

**注入到每层 block**：

[dsa.py:63-85](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/dsa.py#L63-L85) 层循环把 `mla_cls / mla_num_devices / mla_kwargs` 透传给 `MlpBlock`（前 3 层）或 `MoeBlock`（其余 58 层）；block 内部 `self.mla = mla_class(num_devices=mla_nd, **mla_kwargs)`（见 [mlp.py:59-64](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/mlp.py#L59-L64)、moe.py 同构）。所以同一个 `Dsa` 在不同卡上会长出**不同形状**的 MLA 子树。

**头数非对称**（体现在 `get_temp_vars`）：

[dsa.py:125-135](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/dsa.py#L125-L135) device 0 取 `n_local_heads = n_heads // num_devices = 16`；device 1..7 调 `RmsnormProjqWqbWeightsConverter._compute_n_local_heads(128, 7, 192)` 得到补齐对齐后的 `n_local_heads = 20`（128/7 不整除，按 80×8 对齐单位向上补，多出的头零填充，详见 [rmsnorm_projq_wqb.py:96-106](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/rmsnorm_projq_wqb.py#L96-L106)）。因此 `Q_NOPE / O / PROJ_O` 等多头激活的 `n_local_heads` 维，device 0 是 16、device 1..7 是 20。

**V2 P2P 地址回填**（加载阶段收尾）：

[end2end.py:491-501](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L491-L501) 8 个加载线程 `join` 之后，主线程把 device 1..7 各自 `v2_ll_buf.data_ptr()` 收集到一个 CPU int64 张量，再 `copy_` 进 device 0 的 `v2_peer_bufs`。日志会打印这些地址的十六进制。地址回填完成后才进入 `prepare_money` 捕获 CUDA Graph（[end2end.py:503-524](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L503-L524)）。

**三层缓存契约**：两个 MLA 类的 `get_cache_vars` 完全同构，都返回 `[ki_cache, kv_cache, pe_cache]`：

[mla_v2.py:93-113](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/mla_v2.py#L93-L113) 与 [mla_v2.py:228-248](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/mla_v2.py#L228-L248) 两个类各分配 3 个 bf16 缓存，长度都是 `max_seq_len + kv_cache_pad`：

| 缓存 | 末维 | 含义 |
| --- | --- | --- |
| `ki_cache` | `index_head_dim = 128` | NSA 索引键缓存（仅 device 0 的 `layernorm_rope_rotate` 写入并用于选位置） |
| `kv_cache` | `kv_lora_rank = 512` | MLA 压缩 KV 潜向量缓存（`rmsnorm_kv` 写入） |
| `pe_cache` | `qk_rope_head_dim = 64` | 解耦 RoPE 部分的缓存 |

对应到 `Idx` 命名，MLA 主线读写的关键激活槽见 [temp_var_indices.py:18-29](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/temp_var_indices.py#L18-L29)：`Q(0) / KV(1) / KI(2) / IQ(5) / IDX_SCORES(7) / IDX_LOGITS(8) / IDX_SELECTS(9) / Q_NOPE(10) / O(11) / PROJ_O(15) / UNPROJ_O(16)`。这些槽的形状在 `Dsa.get_temp_vars` 里集中分配（[dsa.py:150-180](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/dsa.py#L150-L180)），与本节超参一一对应。

#### 4.3.4 代码实践（可在 CPU 运行的「通讯录」模拟）

这是本讲唯一可脱离 8×B200 运行的实践——用 CPU 张量模拟 V2 P2P 的「地址回填」思想。

1. **目标**：用代码体会「device 0 的 `peer_bufs` 里存的是其余卡 `ll_buf` 的地址」。
2. **操作步骤**（示例代码，可在任意环境运行）：

   ```python
   # 示例代码：模拟 V2 P2P 通讯录回填（仅演示 data_ptr 收集，不代表真实多卡语义）
   import torch

   num_devices = 8
   # device 1..7 各自的「收件箱」ll_buf
   ll_bufs = {d: torch.zeros((4 * 2048 * 2,), dtype=torch.int32) for d in range(1, num_devices)}

   # device 0 的「通讯录」peer_bufs：存放其余 7 张卡 ll_buf 的地址
   peer_bufs = torch.zeros(num_devices - 1, dtype=torch.int64)

   # 加载阶段 join 之后，主线程回填地址（对应 end2end.py:491-501）
   for i in range(num_devices - 1):
       peer_bufs[i] = ll_bufs[i + 1].data_ptr()

   print("peer_bufs（通讯录）=", [hex(int(x)) for x in peer_bufs])
   print("与各 ll_buf 自身地址是否一致:",
         all(int(peer_bufs[i]) == ll_bufs[i + 1].data_ptr() for i in range(num_devices - 1)))
   ```

3. **观察现象**：`peer_bufs` 打印出 7 个不同的十六进制地址；第二行校验为 `True`，证明「通讯录」确实记录了每个对端缓冲的真实地址。
4. **预期结果**：校验输出 `True`。这模拟了 [end2end.py:491-501](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L491-L501) 的 `peer_bufs.copy_(peer_bufs_cpu)` 把 `data_ptr()` 写进 device 0 通讯录的行为。
5. **延伸思考**：若把回填语句放到 `prepare_money`（CUDA Graph 捕获）**之后**会怎样？地址已固化进图，图回放时写到的仍是旧地址 → 结果错乱。这就是「地址回填必须先于捕获」的原因。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `peer_bufs` 在 device 0、而 `ll_buf` 在 device 1..7，二者不互换？

**参考答案**：因为通信方向是「device 0 → 其余卡」的扇出（fan-out）广播。device 0 是发送方，需要一本记录所有接收方地址的「通讯录」`peer_bufs`；其余 7 卡是接收方，只需要一个「收件箱」`ll_buf`。互换会让发送方不知道往哪写、接收方却没有信箱。

**练习 2**：为什么只有 device 0 需要 `partial_buf`，而 device 1..7 没有？

**参考答案**：`partial_buf`（`[bs, 4, dim]` bf16）是 device 0 在 V2 P2P 方案里**汇聚/累积注意力部分输出**的缓冲。device 0 是这套 P2P 拓扑的非对称锚点：它既是选择的「源」（凭 `peer_bufs` 把 `IDX_SELECTS` 推出去），又是部分结果的「汇」（用 `partial_buf` 累积）；而 device 1..7 是对称的对等体，它们只接收选择、贡献自己的部分和，并在 7 卡组内经 `unproj_o_allreduce` 自行归约，因此不需要各自的 `partial_buf`。给每张卡都配一个会冗余。（精确的累积时序由后端 `.so` 实现，Python 侧只确立缓冲形状与归属。）

## 5. 综合实践：画一张 8 卡 MLA 数据流图

把本讲三个模块串起来，完成下面这张图（纸笔或任意画图工具，无需 GPU）。

**任务**：

1. 画出 8 张卡的相对位置，标注 device 0 与 device 1..7。
2. 在 device 0 上标出 `SparseSelectMlaV2` 的 4 个算子（`rmsnorm_projx_wqakis / rmsnorm_projq_wqi / layernorm_rope_rotate / projx_wis`），并画出它产出 `IDX_SELECTS` 的箭头。
3. 画出 device 0 如何凭 `peer_bufs`（通讯录）把 `IDX_SELECTS` **直接写入** device 1..7 各自的 `ll_buf`（7 条 P2P 写箭头）。
4. 在 device 1..7 上标出 `PureMlaV2` 的 6 个算子（`rmsnorm_projx_wqkva / rmsnorm_projq_wqb / rmsnorm_kv / projq_wqb / projo_wkvb / unproj_o_allreduce`），画出它们消费 `ll_buf` 里的选择结果做稀疏 MLA 注意力，最后 `unproj_o_allreduce` 在 7 卡间归约。
5. 标注每层 3 个缓存 `ki_cache / kv_cache / pe_cache` 分别由哪个算子写入（`ki_cache←layernorm_rope_rotate`、`kv_cache←rmsnorm_kv`、`pe_cache←rmsnorm_projx_wqkva` 的 PE 分支）。

**验收要点**：

- 图里能一眼看出「device 0 既选位置又广播、device 1..7 只算注意力」的非对称分工。
- `peer_bufs / ll_buf / partial_buf` 三块缓冲各在哪些卡上，标注正确（`peer_bufs` 与 `partial_buf` 仅 device 0；`ll_buf` 仅 device 1..7）。
- 能口头解释「为什么地址回填（[end2end.py:491-501](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L491-L501)）必须发生在 `prepare_money` 之前」。

> 本实践为源码阅读 + 画图型，不依赖真实 8×B200 环境；若要验证运行时行为，需在目标硬件上 `load_backend('deepseek_v3_2')` 后跑一次生成并观察 `logger.info("V2 P2P exchange complete: ...")` 日志（待本地验证）。

## 6. 本讲小结

- TileRT 把 DeepSeek-V3.2 的 MLA 在 8 卡上**非对称**拆分：device 0 用 `SparseSelectMlaV2` 独自做 NSA 稀疏选择并广播，device 1..7 用 `PureMlaV2` 只做真注意力。
- `SparseSelectMlaV2` 多出索引器权重（`self_attn.indexer.*`）和 4 个索引算子（`wqakis / wqi / rope_rotate / wis`），产出 `IDX_SELECTS`（末维恒为 `index_topk=2048`，与序列长度无关）。
- `PureMlaV2` 是「压-算-展」标准 MLA：`projx_wqkva` 压缩、`projq_wqb` 展头、`rmsnorm_kv` 写缓存、`projq_wqb/projo_wkvb` 复用同一份 `kv_b`、`unproj_o_allreduce` 升维并归约。
- 通信靠 V2 P2P：device 0 的 `peer_bufs`（通讯录）存其余卡 `ll_buf` 的显存地址，地址在 8 线程加载 `join` 后回填、且必须在 CUDA Graph 捕获之前完成。
- 头切分非对称：device 0 每卡 16 头；device 1..7 是 7 卡分 128 头、补齐对齐到每卡 20 头（`mla_num_devices=7`）。
- 每层 3 个 bf16 缓存 `ki_cache / kv_cache / pe_cache` 在两个 MLA 类里布局一致，末维分别为 `index_head_dim=128 / kv_lora_rank=512 / qk_rope_head_dim=64`；`partial_buf` 仅在 device 0，因为它既是选择之「源」、又是部分输出之「汇」。

## 7. 下一步学习建议

- **横向对照 MoE/MLP 前馈**：下一篇 u2-l7 会讲 `MoeBlock` 与 `MlpBlock` 里的 FFN 算子链，注意它同样用 `unproj/expert_down_allreduce` 做跨卡归约，与本讲 `unproj_o_allreduce` 是同一套通信思路。
- **纵向下沉到算子层**：想理解 `rmsnorm_projx_wqakis` 这类融合算子的「device_sharding 既服务离线转换又服务运行时加载」双用途，可读 u3-l1（算子层设计）。
- **看解码如何驱动 MLA**：本讲只到「单层 MLA 的组装与通信」，而这些算子在解码主循环里如何被 `dsa_show_hands(token_id)` 一步步驱动、`IDX_SELECTS` 在哪一步真正生成，留待 u3-l2（非 MTP 解码主循环）与 u3-l3（MTP 投机解码）展开。
