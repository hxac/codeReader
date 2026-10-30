# EAGLE-3 草稿器：捕获层换算与前向 rollout

## 1. 本讲目标

学完本讲，你应该能够：

1. 推导 EAGLE-3 的三个目标层捕获索引：为什么上游约定 `(2, N/2, N-3)` 在本 crate 的 0 基 post-layer 捕获路径上等价于 `(1, N/2-1, N-4)`，并能对 36 层的 Qwen3-4B 算出 `[1, 17, 32]`。
2. 说出 `Eagle3DraftModel` 的全部组成件：`fc` 融合矩阵、单层 `midlayer`（注意力输入是 `2 * hidden_size`、无 QK-norm）、复用目标模型的词嵌入、缩小词表上的 `lm_head` 与 `d2t` 主机侧重映射表。
3. 解释 `Eagle3RequestState`（每请求的单层 K/V cache + 边界特征）与 `Eagle3Scratch`（全部 `seq_len == 1` 的临时缓冲）的分工。
4. 手推一次 γ=3 的 `chain_round` 链式 rollout：embed 拼接 → qkv → RoPE → attention → o_proj → MLP → logits → argmax → 喂回下一 token，并写出每步张量形状。
5. 推导 `chain_capacity` 在 `cached_len` 逼近 `max_cache_len` 时返回的 k，理解 `+1` 是投机解码的奖励（bonus）token。
6. 理解 `rebuild_after_verify` 在接受前缀后如何用 verify 捕获的目标隐藏态 teacher-force 重建草稿 KV 与边界特征。

本讲只到 `forward.rs` 的三个入口（`prefill_prompt` / `chain_round` / `rebuild_after_verify`）为止；它们被谁、在什么门控下调用，属于 u6-l8（EAGLE-3 车道与门控）的内容。

## 2. 前置知识

### 2.1 EAGLE-3 是什么（直觉）

u6-l1 把投机解码建模为 decode 循环上的乐观并发事务：草稿器（drafter）先提出 K 个草稿 token，目标模型一次前向验证 K+1 个位置，接受规则取最长匹配前缀并附加一个修正/奖励 token。草稿器越「猜得准」，每轮提交的 token 越多。

EAGLE-3 是一种**利用目标模型自身隐藏态**的草稿器思路：目标模型每一层都会产生隐藏态（hidden state，即残差流 residual stream——逐层累加更新的特征向量），深层隐藏态已经包含了「接下来最可能输出什么」的大量信息。EAGLE-3 从目标的浅/中/深三个位置各取一份隐藏态，拼接后用一个小的单层「midlayer」草稿网络把它们融合，直接预测下一个 token。因为输入信息量大，这个单层草稿器能达到可观的接受率，而它的前向代价远小于目标模型。

对照同 crate 的另外两个草稿器（u6-l2～u6-l4）：DFlash 用单层 fc + 层缓存做提案，DSpark 在其上叠加 Markov 头；它们与本讲共享 u6-l1 的事务核心、目标隐藏态捕获管线与 `draft_ready_requests` ready 集，只是 worker 车道不同——`DraftModel` 枚举按 checkpoint 路径二选一（u1-l4）。

### 2.2 你需要携带的既有概念

| 概念 | 出处 | 本讲怎么用 |
|---|---|---|
| 投机事务：propose → verify → accept，verify span = K+1 | u6-l1 | γ = `EAGLE3_CHAIN_LENGTH` = 3，span = 4 |
| 草稿/验证边界是纯 token 跨界，proposer 隐藏态不越线 | u6-l1 | 本讲的「跨界」例外是**目标侧**捕获特征，它由目标前向产出、喂给草稿器 |
| `DraftMeta` 能力位（`max_draft_batch` 等） | u3-l1 / u1-l4 | `EAGLE3_MAX_BATCH = 1` 由此上报 |
| GQA：`q_dim = num_heads * head_dim`，`kv_dim = num_kv_heads * head_dim` | u2-l2 | 草稿 midlayer 沿用目标的注意力几何 |
| RoPE 位置编码 | u2-l1 | 草稿用自己的 `cos/sin_cache`，`rope_theta` 必须与目标一致 |
| teacher forcing（教师强制） | 通用 | 用真实 token 而非草稿自己的输出跑前向，只为了把 K/V 写进 cache |
| `HiddenStates` 布局：`[hidden_dim, seq_len]`，一列一个 token | u2-l2 | 本讲所有形状都按「行数 × 列数」书写 |

术语约定：本讲用 \( C \) 表示 `state.cached_len`（已提交位置数），\( L_{\max} \) 表示 `state.max_cache_len`（该请求草稿 cache 的总槽位数），\( \gamma = 3 \) 表示链长，\( P \) 表示 prompt token 数，\( N \) 表示目标模型层数。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲视角 |
|---|---|---|
| `src/eagle3.rs` | 模块根：γ 与 max batch 常量、`aux_hidden_state_layers` 捕获层换算、`Eagle3Layer` / `Eagle3DraftModel` 结构体 | 4.1、4.2 |
| `src/eagle3/forward.rs` | 前向实现：`chain_capacity`、`Eagle3RequestState` / `Eagle3Scratch`、`draft_step` / `draft_chain` / `prefill_batched` / `prefill_prompt` / `chain_round` / `rebuild_after_verify` | 4.3、4.4、4.5 |
| `src/eagle3/loading.rs` | 从 safetensors 装载草稿权重，几何对目标校验 | 4.2 |
| `src/eagle3/reservation.rs` | 草稿器显存预留（`kv_bytes_per_token` + `fixed_bytes`） | 4.2、4.3 |
| `src/config.rs` | `Eagle3Config` 解析与 `validate_for_target` 跨配置校验 | 4.2 |
| `src/executor/eagle3_lane.rs` | 三个入口的调用方（车道集成） | 仅引用行号，详讲在 u6-l8 |
| `tests/eagle3_speculative_gate.rs` | EAGLE-3 无损性行为门测试 | 4.4 实践对照 |

模块根的声明与导出见 [src/eagle3.rs:L1-L15](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3.rs#L1-L15)：头两行点名了典型 checkpoint（`AngelSlim/Qwen3-4B_eagle3`），三个子模块 `forward` / `loading` / `reservation`，向 crate 内导出 `Eagle3RequestState`、`Eagle3Scratch` 与 `Eagle3MemoryReservation`。

## 4. 核心概念与源码讲解

### 4.1 aux_hidden_state_layers：三层捕获索引换算

#### 4.1.1 概念说明

EAGLE-3 需要从目标模型的**浅（low）、中（mid）、深（high）**三个位置各捕获一份 post-layer 隐藏态，拼接成 `fc` 的输入（`3 * hidden` 列）。问题在于：「第几层」有两种数法。

- 上游 SafeAILab 的 `traineagle3` 训练代码以 `(2, N/2, N-3)` 为门槛，但它在**运行这些层之前**追加残差流——也就是说它取的是「进入第 ℓ 层之前的残差流」；
- vLLM 用 post-layer 的 `idx + 1` 捕获实现了同一语义；
- 本 crate 的捕获路径（`prefill.rs` 与 `verify_graph.rs`）是 **0 基、post-layer** 的：捕获点「块 i 的输出」。

「进入上游第 ℓ 层的残差流」就是「第 ℓ−1 块（0 基）的输出」。因此同样的三股残差流，在我们的坐标下每个索引都要**前移一块**：

\[
(2,\ N/2,\ N-3) \;\longrightarrow\; (1,\ \lfloor N/2 \rfloor - 1,\ N-4)
\]

对 36 层的 Qwen3-4B：\( N = 36 \)，得 \( (1, 17, 32) \)。这段换算的依据记录在代码注释里（含 PR #662 讨论），见 [src/eagle3.rs:L26-L35](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3.rs#L26-L35)。

#### 4.1.2 核心流程

```text
aux_hidden_state_layers(N):
    low  = 1
    mid  = saturating(N/2 - 1)
    high = saturating(N - 4)
    确保 low < mid < high < N      # 否则报错：三层必须严格递增且落在层范围内
    返回 [low, mid, high]
```

校验失败意味着目标层数太少，放不下一个严格递增的三元组——这是硬失败（装载草稿器时直接报错），不是静默降级。

#### 4.1.3 源码精读

函数本体在 [src/eagle3.rs:L36-L46](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3.rs#L36-L46)：两个 `saturating_sub` 保证小 N 时不发生算术下溢 panic，而是得到一个必然违反严格递增的三元组，再被 `ensure!` 用带具体数值的错误信息拒绝。配套单测 [src/eagle3.rs:L97-L102](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3.rs#L97-L102) 验证 N=6（得 `(1,2,2)`，mid 不小于 high）与 N=4（得 `(1,1,0)`，low 不小于 mid）都被拒绝。

这个三元组在装载期由车道计算并保存：`load_eagle3` 用**目标**配置的 `num_hidden_layers` 调用它（[src/executor/eagle3_lane.rs:L60](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/eagle3_lane.rs#L60)），存进 `Eagle3LaneState.aux_layers`（[src/executor/eagle3_lane.rs:L30-L36](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/eagle3_lane.rs#L30-L36)），再经 `aux_layers()` 暴露给捕获路径挑选目标层。

#### 4.1.4 代码实践

1. **实践目标**：手工验证捕获层换算，并跑通本模块的纯 CPU 单测。
2. **操作步骤**：
   - 对 \( N \in \{4, 6, 7, 8, 36, 40\} \) 逐个算出 `(low, mid, high)` 并判断能否通过严格递增校验；
   - 运行 `cargo test --release -p pegainfer-qwen3 --lib eagle3`（过滤串会匹配 `eagle3::tests::aux_layers_reject_tiny_target`、`eagle3::forward::tests::chain_shrinks_at_cache_edge`、`eagle3::reservation::tests::eagle3_reservation_pins_geometry` 三个纯 CPU 单测的完整路径）。
3. **需要观察的现象**：N=4 与 N=6 的用例被拒绝；其余通过。
4. **预期结果**：N=7 → `(1,2,3)` 通过；N=8 → `(1,3,4)` 通过；N=36 → `(1,17,32)`；N=40 → `(1,19,36)`。三条单测全部 pass。（命令未在本讲义生成环境中执行，属常规 `--lib` 单测入口，可本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：为什么本 crate 的 high 是 `N - 4` 而上游是 `N - 3`？
**答案**：上游索引指「进入该层的残差流」，等于上一块的输出；我们的捕获点是 0 基 post-layer 输出，所以每个索引减一，`N - 3` 变 `N - 4`。两个约定描述的是同一股残差流。

**练习 2**：如果目标只有 7 层，换算结果还能用吗？
**答案**：能。`(1, 2, 3)` 严格递增且 3 < 7，校验通过——但层数太少时浅/中/深的区分度存疑，实践中 EAGLE-3 草稿器都是配深目标模型的。

**练习 3**：为什么用 `saturating_sub` 而不是直接 `-`？
**答案**：小 N（如 N=2）时 `N/2 - 1` 与 `N - 4` 会下溢。饱和成 0 后交给 `ensure!` 拒绝，能得到一条带具体数值的错误信息，而不是一个 `usize` 减法溢出 panic。

### 4.2 Eagle3DraftModel：fc 融合、单层 midlayer 与 d2t 词表重映射

#### 4.2.1 概念说明

`Eagle3DraftModel` 是一个**极小**的自回归草稿网络，全部组成件：

1. **`fc`**：`[hidden, 3*hidden]` 矩阵，把捕获的浅/中/深三层目标隐藏态融合成一份 `[hidden]` 的「融合目标隐藏态」；
2. **`midlayer`**：单个解码器块。与目标的 `TransformerBlock` 有两处承重差异——注意力输入是 `2 * hidden_size`（EAGLE-3 把 `[input_layernorm(embed), hidden_norm(fused_hidden)]` 拼接后再过 q/k/v），且**没有 QK-norm**；
3. **`norm` + `lm_head`**：最终 RMSNorm 之后在**缩小词表**（`draft_vocab_size`，如 32 000）上出 logits；
4. **`d2t`**：长度为 `draft_vocab_size` 的 `i64` 偏移表，`target_id = draft_id + d2t[draft_id]`，把草稿 id 映射回目标词表；
5. **`cos_cache` / `sin_cache`**：草稿自己的 RoPE 表。

注意它**不存词嵌入**：checkpoint 不带 embedding，前向时复用目标模型的 `embed_tokens`（见 4.4 的 `draft_step`）。缩小词表让 `lm_head` 这一步 GEMM 小很多——代价只是每次映射查一次表。

#### 4.2.2 核心流程

装载流程（`from_safetensors_for_target`）：

```text
读 config.json → Eagle3Config
validate_for_target(target.config)          # 跨配置硬校验
load_shard_info → mmap → deserialize        # 与目标装载同一套 weight_loader
逐张量 load_tensor_2d/1d + vstack：
    q/k/v_proj   [*, 2*hidden]  → qkv_proj（行堆叠）
    gate/up_proj [inter, hidden] → gate_up_proj（行堆叠）
    fc           [hidden, 3*hidden]
    lm_head      [draft_vocab, hidden]
    d2t / t2d    主机侧表（只做长度校验）
precompute_rope(max_position_embeddings, rope_theta)
```

显存预留（`Eagle3MemoryReservation`）把草稿器的开销拆成两条账：

\[
\text{kv\_bytes\_per\_token} = \text{num\_hidden\_layers} \times 2 \times \text{kv\_dim} \times 2\text{B}
\]

（只有单层草稿 K/V 随池子规模增长，经 `effective_bytes_per_block` 记在 KV 预算上），以及 `fixed_bytes` = 权重（+10% 余量）+ 两条 RoPE 表 + prefill/decode 稠密 scratch + 捕获特征缓冲，记在 memory margin 上。

#### 4.2.3 源码精读

- **结构体**：[src/eagle3.rs:L48-L72](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3.rs#L48-L72) 是 `Eagle3Layer`，注释明确两处与目标块的差异（`2 * hidden_size` 输入、无 QK-norm）；[src/eagle3.rs:L74-L91](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3.rs#L74-L91) 是 `Eagle3DraftModel`，`d2t` 的注释点明它是主机常驻的小查表、不是 GEMM 输入。两个常量在 [src/eagle3.rs:L17-L24](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3.rs#L17-L24)：γ=3 且「v1 是固定 top-1 **链**，不是树」；`EAGLE3_MAX_BATCH = 1` 且 v1 用单 token scratch，更大批次由调度器退回纯解码（呼应 u4-l2 的 `max_draft_batch` 门）。
- **配置与校验**：`Eagle3Config` 定义在 [src/config.rs:L275-L301](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/config.rs#L275-L301)，单 schema、无需 Raw 中间层，`max_position_embeddings` 缺省 40 960。`validate_for_target`（[src/config.rs:L802-L845](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/config.rs#L802-L845)）逐条硬校验：hidden 相等、**层数必须恰为 1**、注意力几何（头数/KV 头数/head_dim）相等、`vocab_size` 相等、`draft_vocab_size` 在 `1..=vocab_size`、`rope_theta` 逐位相等、草稿位置上限不低于目标。
- **装载**：入口 [src/eagle3/loading.rs:L25-L46](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3/loading.rs#L25-L46) 先读配置并对目标校验，再走与目标模型完全相同的 `load_shard_info → mmap_shards → deserialize_shards` 管线。q/k/v 三个投影按 `2 * hidden` 输入列装载后 `vstack` 成 `qkv_proj`（[src/eagle3/loading.rs:L56-L85](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3/loading.rs#L56-L85)）；gate/up 同样行堆叠（[src/eagle3/loading.rs:L87-L105](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3/loading.rs#L87-L105)）。融合头一段的注释写死了捕获兼容不变量：「EAGLE-3 恰好融合三层捕获，fc 是 `[3*hidden] → [hidden]`」（[src/eagle3/loading.rs:L153-L166](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3/loading.rs#L153-L166)）。`d2t`/`t2d` 装到主机并只做长度校验（[src/eagle3/loading.rs:L168-L181](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3/loading.rs#L168-L181)），RoPE 表按草稿配置预计算（[src/eagle3/loading.rs:L183-L192](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3/loading.rs#L183-L192)）。
- **预留**：[src/eagle3/reservation.rs:L18-L21](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3/reservation.rs#L18-L21) 的结构体文档把两条账的记法说得很清楚（捕获的三层特征是融合步的前向输入、不持久化，所以**没有**别的量随池 token 增长）。权重项在 [src/eagle3/reservation.rs:L54-L66](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3/reservation.rs#L54-L66)（`+weights/10` 是 10% 余量），RoPE 与稠密 scratch 在 [src/eagle3/reservation.rs:L68-L84](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3/reservation.rs#L68-L84)。单测 [src/eagle3/reservation.rs:L114-L158](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3/reservation.rs#L114-L158) 用 AngelSlim 几何钉死了这些数（见下面实践）。

#### 4.2.4 代码实践

1. **实践目标**：手工复现 reservation 单测的数字，确认你理解每个组成件来自哪块权重。
2. **操作步骤**：取 `AngelSlim/Qwen3-4B_eagle3` 几何（`hidden=2560, inter=9728, q_dim=32*128=4096, kv_dim=8*128=1024, draft_vocab=32000`，见 [src/eagle3/reservation.rs:L98-L112](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3/reservation.rs#L98-L112) 的夹具），按 bf16=2B 计算：

| 组成件 | 计算 | 字节数 |
|---|---|---|
| `qkv_proj` | (4096 + 2×1024) × 5120 × 2 | 62,914,560 |
| `o_proj` | 2560 × 4096 × 2 | 20,971,520 |
| `gate_up_proj` | 2×9728 × 2560 × 2 | 99,614,720 |
| `down_proj` | 2560 × 9728 × 2 | 49,807,360 |
| 三个 norm | 3 × 2560 × 2 | 15,360 |
| `fc` | 2560 × 7680 × 2 | 39,321,600 |
| `lm_head` + `norm` | 32000 × 2560 × 2 + 2560 × 2 | 163,845,120 |

   权重合计 436,490,240 B（≈437 MB），+10% ≈ 480 MB；RoPE = 2 × 40960 × 128 × 2 ≈ 21 MB；合计 ≈ 501 MB。
3. **需要观察的现象**：数字落入单测断言区间 `(495_000_000..515_000_000)`（[src/eagle3/reservation.rs:L129-L133](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3/reservation.rs#L129-L133)）；`kv_bytes_per_token == 4096`（单层 K/V：1 × 2 × 1024 × 2B）。
4. **预期结果**：`cargo test --release -p pegainfer-qwen3 --lib eagle3_reservation_pins_geometry` 通过；你的表与断言逐项吻合。（计算为纸面推导，测试结果待本地验证。）

#### 4.2.5 小练习与答案

**练习 1**：为什么 `d2t` 放主机而不是上 GPU？
**答案**：它是对草稿 id 的逐项小查表、不是任何 GEMM 的输入；而 `draft_chain` 本来就要把 argmax 的 id 读回主机（见 4.4），映射搭这趟便车零成本。注释原文是「Host-resident (small lookup, not a GEMM input)」。

**练习 2**：为什么 `midlayer.qkv_proj` 的输入列数是 `2 * hidden_size`？
**答案**：EAGLE-3 把 `[input_layernorm(embed), hidden_norm(fused_hidden)]` 两段 `[hidden]` 拼成 `[2 * hidden]` 再投 q/k/v，所以三个投影矩阵的输入维都是 `2 * hidden`（[src/eagle3/loading.rs:L42-L46](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3/loading.rs#L42-L46) 注释「embed ++ hidden concat」）。

**练习 3**：`validate_for_target` 为什么要求草稿的 `max_position_embeddings` **不低于**目标，而不是相等？
**答案**：草稿请求 cache 的槽位按「prompt + max_output + γ + 1」裁剪且封顶在草稿自己的位置上限（见 4.3），只要求草稿能覆盖目标服务到的长度即可；预留测试注释也点明校验只下界约束（[src/eagle3/reservation.rs:L135-L139](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3/reservation.rs#L135-L139)）——更长的草稿会多花 RoPE 表显存，那由预留精确计账。

### 4.3 Eagle3RequestState 与 Eagle3Scratch：请求状态与单 token 缓冲

#### 4.3.1 概念说明

EAGLE-3 的草稿侧状态分两层：

- **`Eagle3RequestState`（每请求、跨前向持久）**：草稿自己那**一层**的 K/V cache（`[kv_dim, max_cache_len]` 两条）、`cached_len`（已写入列数）、`max_cache_len`，以及 `last_aux_hidden_states`——「边界特征」，即最后一个已提交位置上的 `[3*hidden, 1]` **pre-fc** 目标特征。它是本轮链式 rollout 的出发信号。
- **`Eagle3Scratch`（每 lane 一份、单 token）**：`draft_step` 的全部中间激活，**所有缓冲 `seq_len == 1`**，外加 argmax 的 split-partials 缓冲。v1 一次跑一个请求、一次一个 token，所以形状全部定死、一次分配、跨请求跨轮次复用。

分工的好处：请求状态只随请求数增长（且只有 K/V 是大块），scratch 与批宽无关——这正是 `EAGLE3_MAX_BATCH = 1` 的物质基础。

#### 4.3.2 核心流程

```text
new_request_state(max_cache_len):
    确保 1 <= max_cache_len <= config.max_position_embeddings
    分配 k, v: [kv_dim, max_cache_len]（零初始化）
    cached_len = 0, last_aux_hidden_states = None

new_scratch():
    对每个中间量按 [dim, 1] 零分配（见下表）
    argmax partials 长度 = argmax_batch_bf16_split_partials_len(1, draft_vocab)
```

scratch 各缓冲的形状注记直接写在字段旁（[src/eagle3/forward.rs:L34-L60](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3/forward.rs#L34-L60)）：`argmax_*` 四条是设备侧 argmax 的工作区，`token_id_d` 是当前 token id 的设备侧落点，其余从 `embed` 到 `logits` 一路是单列激活。

#### 4.3.3 源码精读

- `Eagle3RequestState` 定义与字段注释在 [src/eagle3/forward.rs:L23-L32](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3/forward.rs#L23-L32)——注意 `last_aux_hidden_states` 的类型是 `[3 * hidden, 1]`，即**未过 fc** 的目标特征。
- `new_request_state` 在 [src/eagle3/forward.rs:L71-L94](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3/forward.rs#L71-L94)：先校验 `max_cache_len` 落在 `1..=max_position_embeddings`（rope cache 也只预计算到这个长度），再零分配两条 cache。
- `new_scratch` 在 [src/eagle3/forward.rs:L96-L127](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3/forward.rs#L96-L127)：每个缓冲一行 `HiddenStates::zeros(ctx, dim, 1)`；`fc_input_dim()`（[src/eagle3/forward.rs:L129-L132](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3/forward.rs#L129-L132)）直接读 `self.fc.cols`，即 `3 * hidden`。
- `max_cache_len` 不是随便选的：车道按「每个已提交 token 加一整轮余量」裁剪——\(\min(P + \text{max\_output} + \gamma + 1,\ \text{max\_position\_embeddings})\)，见 [src/executor/eagle3_lane.rs:L119-L122](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/eagle3_lane.rs#L119-L122)。

#### 4.3.4 代码实践

1. **实践目标**：算清一个请求的草稿状态开销。
2. **操作步骤**：取 prompt=32、max_output=512 的请求（AngelSlim 几何）：
   - \( L_{\max} = \min(32 + 512 + 3 + 1,\ 40960) = 548 \)；
   - 请求级 K/V = 2 × kv_dim(1024) × 548 × 2B = 2,240,512 B ≈ 2.14 MiB；
   - 对照 `kv_bytes_per_token = 4096` B/token：548 × 4096 = 2,240,512 ✓；
   - 再按 [src/eagle3/reservation.rs:L74-L79](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3/reservation.rs#L74-L79) 的公式算 `dense_per_token = 2 × (10×2560 + 2×4096 + 2×1024 + 3×9728 + 32000) = 194,048` B，理解 scratch 每列的账。
3. **需要观察的现象**：`kv_bytes_per_token` 与逐请求总字节的两种算法一致。
4. **预期结果**：上表数字成立；请求状态约 2.14 MiB（远小于目标模型每请求动辄整页池的 KV——因为草稿只有一层且 GQA 下 kv_dim 只有 1024）。（纸面推导，待本地验证。）

#### 4.3.5 小练习与答案

**练习 1**：scratch 全是 `seq_len == 1`，注意力怎么看到整个上下文？
**答案**：注意力读的是 `state.k` / `state.v` 的 `[0..=position]` 列（`draft_step` 里 `kv_len = position + 1`，[src/eagle3/forward.rs:L237-L249](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3/forward.rs#L237-L249)）；scratch 只承载**当前 token** 的激活。

**练习 2**：`draft_step` 为什么断言 `position == cached_len`？
**答案**：草稿 KV 是顺序追加写的（先写 `position` 列再以 `kv_len = position + 1` 做注意力）。断言防止跳写或覆写历史列造成错位（[src/eagle3/forward.rs:L161-L166](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3/forward.rs#L161-L166)）。

**练习 3**：边界特征为什么存 pre-fc 的 `[3*hidden]` 而不是融合后的 `[hidden]`？
**答案**：边界保存的是**目标侧**的量（被捕获的原始目标特征）；`fc` 属于草稿权重，在每次 `chain_round` 里执行（[src/eagle3/forward.rs:L583-L589](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3/forward.rs#L583-L589)），`rebuild_after_verify` 存的新边界同样是捕获列本身（[src/eagle3/forward.rs:L657-L667](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3/forward.rs#L657-L667)），两处语义一致：请求状态里不掺草稿权重加工过的数据。

### 4.4 prefill_prompt 与 chain_round：整段建 cache 与 γ=3 链式 rollout

#### 4.4.1 概念说明

草稿器的前向分三个入口，对应请求生命周期的三段：

1. **`prefill_prompt`（prompt 刚过完目标 prefill）**：用捕获的整段目标特征 teacher-force 地把草稿 KV 一次建好，并把最后一个特征留作边界。核心是 **EAGLE 特征↔token 错位配对**：把特征 \( f_j \)（目标在第 j 个 token 上的输出）与**下一个** token 的词嵌入 \( e_{j+1} \) 配对，预测 \( t_{j+2} \)。也就是说：草稿在槽位 j 上「看着位置 j-1 的目标特征、吃着位置 j 的 token」，去预测位置 j+1 的 token。
2. **`chain_round`（每轮投机）**：从边界特征出发，自回归地滚 γ=3 步链：每步出 logits → 设备上 argmax → 读回 id → `d2t` 映射成目标 id → 作为下一步输入。链上写的 KV 是**投机的**，轮末把 `cached_len` 回退到起点，使轮次除返回 token 外无副作用。
3. **`rebuild_after_verify`（verify 之后）**：见 4.5。

链长受 `chain_capacity` 约束：

\[
k = \min\left(\gamma,\ \max\left(0,\ L_{\max} - C - 1\right)\right)
\]

那个 `+1` 是投机解码的**奖励 token**：全接受的轮会提交 γ 个草稿加 1 个 verify 自产 token，所以 cache 必须给 \( C + k + 1 \) 留满槽。

#### 4.4.2 核心流程

**`draft_step`（链上一步，全部 `seq_len == 1`）**：

```text
写入 token_id → 复用目标 embed_tokens 得 embed          [hidden, 1]
normed_embed  = rms_norm(embed,  input_layernorm)
normed_hidden = rms_norm(hidden, hidden_norm)            # hidden 是上一步的残差流
attn_input    = [normed_embed ‖ normed_hidden]           [2*hidden, 1]
q, k, v       = qkv_proj 行段 GEMM                        [q_dim|kv_dim|kv_dim, 1]
rope(q, k, position)                                     # 无 QK-norm 的朴素 RoPE
state.k/v 第 position 列 ← k, v；kv_len = position + 1
attn_out      = single_decode(q; state.k, state.v)        [q_dim, 1]
o             = o_proj @ attn_out；hidden += o            （fused_add_rms_norm）
gate, up, act = MLP(normed_post)；mlp_out = down_proj @ act；hidden += mlp_out
logits        = lm_head @ rms_norm(hidden, norm)          [draft_vocab, 1]
state.cached_len = kv_len
```

**`chain_round`**：

```text
start = cached_len；k = chain_capacity(start, L_max, γ)；k == 0 则本轮跳过
fused = fc(last_aux_hidden_states)                        # 初始化残差流
span  = draft_chain(...)：循环 k 次 { draft_step; argmax; 读回; d2t; 喂回 }
cached_len = start                                        # 丢弃投机 KV
返回 span（k 个目标词表 id）
```

**`prefill_prompt`**：

```text
要求 p >= 2 且 cached_len == 0（v1 需要整段 prompt 一个 chunk）
feat = 捕获列 [offset, offset+p-1)                        # f_0..f_{P-2}
prefill_batched(feat, tokens = prompt[1..p], start = 0)   # P-1 个错位对
last_aux_hidden_states = 捕获列 offset+p-1                # f_{P-1} 留作边界
```

#### 4.4.3 源码精读

- **`chain_capacity`**：[src/eagle3/forward.rs:L14-L21](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3/forward.rs#L14-L21)，两行 `saturating_sub` 实现上式；注释点明约束是 `cached_len + k + 1 <= max_cache_len`。
- **`draft_step` 的算子序列**（AngelSlim 几何下的形状见 4.4.4 实践表）：
  - 词嵌入复用：[src/eagle3/forward.rs:L168-L173](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3/forward.rs#L168-L173) 把 token id 拷上设备后调**目标模型**的 `get_embeddings_batch_into`——注释「The drafter ships no embedding; it reuses the target's」。
  - 双 norm 与拼接：[src/eagle3/forward.rs:L175-L192](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3/forward.rs#L175-L192)，`attn_input` 的行 0..hidden 放 `normed_embed`、行 hidden..2h 放 `normed_hidden`。
  - QKV 按行段切读融合矩阵：[src/eagle3/forward.rs:L194-L217](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3/forward.rs#L194-L217)（行偏移 0 / `q_dim` / `q_dim + kv_dim`，与 u2-l2 目标权重的访问方式同构）。
  - 朴素 RoPE（无 QK-norm）：[src/eagle3/forward.rs:L219-L233](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3/forward.rs#L219-L233)。
  - KV 落槽与单 token 注意力：[src/eagle3/forward.rs:L235-L249](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3/forward.rs#L235-L249)。
  - o_proj + 残差融合 norm、MLP、最终 norm + lm_head：[src/eagle3/forward.rs:L251-L305](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3/forward.rs#L251-L305)；两处 `fused_add_rms_norm_round_batch_into` 在归一化的同时把残差加进 `scratch.hidden`——**残差流在 scratch 内就地演进**，这就是「草稿器自己的隐藏态」。
- **`prefill_batched`**（`prefill_prompt` 与 `rebuild_after_verify` 共用的底层）：文档在 [src/eagle3/forward.rs:L311-L316](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3/forward.rs#L311-L316)——**只留 K/V**：草稿器只有一层，这些位置的 attention/MLP 输出不会喂给任何后续层；且因为 N 会变，缓冲内联分配、不走单 token scratch。关键差异在 [src/eagle3/forward.rs:L363-L366](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3/forward.rs#L363-L366)：teacher forcing 下每个位置的输入 hidden 都是 `fc(target features)`，而 `draft_step` 由自己的残差流携带。
- **`prefill_prompt`**：文档 [src/eagle3/forward.rs:L447-L456](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3/forward.rs#L447-L456) 把错位配对讲成一句：pair \( f_j \) with \( e_{j+1} \)（predicting \( t_{j+2} \)）；实现 [src/eagle3/forward.rs:L457-L503](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3/forward.rs#L457-L503)：切出 P-1 列特征、以 `prompt_tokens[1..p]` teacher-force（[src/eagle3/forward.rs:L487-L490](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3/forward.rs#L487-L490)），最后一列捕获特征存为边界（[src/eagle3/forward.rs:L491-L501](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3/forward.rs#L491-L501)）。
- **`draft_chain`**：[src/eagle3/forward.rs:L505-L553](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3/forward.rs#L505-L553)。先把 `fused_target_hidden` 拷进 `scratch.hidden` 初始化残差流（[L526](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3/forward.rs#L526)）；循环体内 argmax 走 `argmax_bf16_split_into`，随后 `clone_dtoh` + `ctx.sync()` 读回 id（文档明说「a round syncs once per draft」），`d2t` 映射后压入 span 并喂回下一轮（[src/eagle3/forward.rs:L530-L551](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3/forward.rs#L530-L551)）。`draft_to_target_id` 的越界防护在 [src/eagle3/forward.rs:L671-L679](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3/forward.rs#L671-L679)。
- **`chain_round`**：[src/eagle3/forward.rs:L555-L603](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3/forward.rs#L555-L603)。k=0 时打 debug 日志直接返回空 span（[L573-L580](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3/forward.rs#L573-L580)）；否则 `fc × 边界特征` 得融合隐藏态（[L582-L589](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3/forward.rs#L582-L589)），跑链，最后 `state.cached_len = start` 丢弃投机 KV（[L599-L602](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3/forward.rs#L599-L602)）——重建交给 `rebuild_after_verify`。
- **调用方**（一句话，详讲 u6-l8）：车道在 `execute_eagle3_draft` 里对批内唯一请求调 `chain_round(current_token, EAGLE3_CHAIN_LENGTH)`，并把返回草稿拼在 current token 前面组成 verify span（[src/executor/eagle3_lane.rs:L158-L177](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/eagle3_lane.rs#L158-L177)）。

#### 4.4.4 代码实践

1. **实践目标**：手推一次 γ=3 的 `chain_round`，写出每步张量形状；再推导 `chain_capacity` 的边界行为并对照门测试。
2. **操作步骤**：
   - **(a) 单步形状表**（AngelSlim 几何，`position = C + i`）：

| 步骤 | 输出形状 | 备注 |
|---|---|---|
| `embed` | [2560, 1] | 复用目标 embed_tokens |
| `normed_embed` / `normed_hidden` | [2560, 1] ×2 | 两套 RMSNorm 权重 |
| `attn_input` | [5120, 1] | 行拼接 |
| `q` / `k` / `v` | [4096, 1] / [1024, 1] / [1024, 1] | 行段 GEMM |
| `state.k/v` 第 position 列 | [1024] ×2 | 写入请求 cache |
| `attn_out` | [4096, 1] | `kv_len = position+1` 列注意力 |
| `o` → `hidden += o` | [2560, 1] | 残差就地累加 |
| `gate`/`up`/`act` | [9728, 1] ×3 | silu·mul |
| `mlp_out` → `hidden += mlp_out` | [2560, 1] | 残差第二次累加 |
| `logits` | [32000, 1] | 缩小词表 |
| argmax 读回 → `d2t` | 标量 ×2 | draft id → target id ∈ [0, 151936) |

   - **(b) 三次迭代**：设 `current_token = t`、起始 `C`，则 i=0 消费 `(t, fc(boundary))` 写槽 C；argmax 得 \( d_1 \)；i=1 消费 `(d_1, 残差流)` 写槽 C+1 得 \( d_2 \)；i=2 得 \( d_3 \) 写槽 C+2。返回 span = `[d_1, d_2, d_3]`（三个目标词表 id），`cached_len` 回退到 C。
   - **(c) 边界容量**：取 4.3 的 \( L_{\max}=548 \)：`chain_capacity(544)=3`、`(545)=2`、`(546)=1`、`(547)=0`、`(548)=0`。并对照单测 [src/eagle3/forward.rs:L686-L706](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3/forward.rs#L686-L706)（max=100：97→2 且 97+2+1=100；98→1；99/100→0）。
   - **(d) 对照门测试**：读 [tests/eagle3_speculative_gate.rs:L230-L260](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/tests/eagle3_speculative_gate.rs#L230-L260) 的 `eagle3_output_budget_edges_match_plain_greedy`：文档说明调度器把 verify span 钳到请求剩余输出预算，所以 `[1,2,3,4,5,6,9]` 这些短预算让请求以从 1 个 token 到整链的各种跨度收尾，每种跨度上 `rebuild_after_verify` 都会跑一遍（配合 4.5）。
3. **需要观察的现象**：形状表逐行对应 4.4.3 引用的源码行；容量表与单测断言一致。
4. **预期结果**：(a)(b)(c) 全部自洽；若本地有 GPU + `AngelSlim/Qwen3-4B_eagle3` 权重（经 `PEGAINFER_EAGLE3_TEST_MODEL_PATH` 或默认 `models/Qwen3-4B_eagle3`），运行该测试应见 `proposed > 0` 且逐预算 lossless；无权重时测试打印 `skipping eagle3 gate: .../config.json missing` 后跳过（[tests/eagle3_speculative_gate.rs:L64-L73](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/tests/eagle3_speculative_gate.rs#L64-L73)）。门测试结果待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`prefill_batched` 为什么只写 K/V、不算 attention/MLP 输出？
**答案**：草稿器只有一层，prompt 各位置的 attention/MLP 输出不会成为任何后续层的输入；边界位置由链自己的 `draft_step` 现算。文档原文见 [src/eagle3/forward.rs:L311-L315](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3/forward.rs#L311-L315)。

**练习 2**：`chain_round` 结束为什么把 `cached_len` 回退到 start？
**答案**：链在 `[C, C+k)` 写的 KV 是投机产物，可能整段被 verify 拒绝；`rebuild_after_verify` 会按**接受前缀** teacher-force 重建，回退保证本轮除返回的 token 外无副作用（文档「side-effect-free except for the returned tokens」，[src/eagle3/forward.rs:L599-L602](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3/forward.rs#L599-L602)）。

**练习 3**：每个草稿一次 `ctx.sync()` 是不是浪费？
**答案**：这是 v1 的已知取舍。argmax 在设备上跑，但 `d2t` 是主机侧表，必须把 id 读回才能映射；γ=3 意味着每轮 3 次同步。注释明说「each step still reads its id back ... so a round syncs once per draft」（[src/eagle3/forward.rs:L513-L514](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3/forward.rs#L513-L514)）。

### 4.5 rebuild_after_verify：接受前缀后的状态重建

#### 4.5.1 概念说明

verify 之后，目标模型给出两样东西：接受了多少个草稿（`matched_draft_tokens = n`），以及 verify span 上每个位置的目标隐藏态（捕获列）。`rebuild_after_verify` 用它们把草稿状态推进到「最后一个已提交位置」：

- 提交的草稿前缀是 `[current, d_1..d_n]`（n+1 个 token，`current` 是本轮入口 token）；verify 的奖励 token 不进草稿 KV——它成为下一轮的 `current_token`。
- 重建 = 把这 n+1 个 (特征, token) 错位对 teacher-force 进槽 `[C, C+n+1)`，其中第一个特征用**旧边界**（位置 C-1 的目标特征），其后用 verify 捕获的第 0..n-1 列；
- **新边界** = verify 捕获的第 n 列——最后一个已提交位置上的目标特征。下一轮链将以 `(新边界, 奖励 token)` 起跳，预测再下一个 token。

为什么可以直接覆写而不清理缓存？被链污染的列要么被这次重建覆写，要么位于 `cached_len` 之外——而 `draft_step` 写槽之前只会读 `[0..=position]` 且 `position == cached_len`，所以陈旧列在被读之前必然已被后续步骤覆写。

#### 4.5.2 核心流程

```text
n = matched_draft_tokens; len = n + 1
校验: len <= span_tokens.len()（span = [current, d_1..d_γ]）
      captured.dim == 3*hidden; token_offset + n < captured.seq_len
feat = [旧边界 (1 列) ‖ captured[offset .. offset+n) (n 列)]     # n+1 列
prefill_batched(feat, tokens = span_tokens[..n+1], start = cached_len)
last_aux_hidden_states = captured[offset + n]                    # 新边界
```

#### 4.5.3 源码精读

函数在 [src/eagle3/forward.rs:L605-L669](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3/forward.rs#L605-L669)：

- 三条前置校验（[L615-L637](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3/forward.rs#L615-L637)）：span 足够长（`1 bonus + k drafts`）、捕获维度匹配 fc、需要的捕获列存在——注意边界列要读到 `token_offset + n`，所以校验是 `<`（需要第 n 列存在）。
- 错位拼接（[L640-L652](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3/forward.rs#L640-L652)）：注释把 feat 写成 `[boundary f_{pos-1} ‖ f_pos..f_{pos+n-1}]`，共 n+1 列；n=0 时只有旧边界一列。
- teacher-force 与新边界（[L653-L667](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3/forward.rs#L653-L667)）：`prefill_batched` 把提交前缀写进 `[C, C+n+1)`；新边界取捕获列 `token_offset + n`，注释「the target feature at the last committed position」。
- 调用方 `record_verify_eagle3_context` 逐请求调它，`token_offset` 沿捕获缓冲累加（[src/executor/eagle3_lane.rs:L205-L228](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/eagle3_lane.rs#L205-L228)）。

与 `chain_capacity` 的呼应：n 最大取 k，重建最多写到槽 `C+k`（含），所以需要 \( C + k + 1 \le L_{\max} \)——正是 4.4 那个 `+1` 的来源。

#### 4.5.4 代码实践

1. **实践目标**：对三种接受结果推演重建的读写范围。
2. **操作步骤**：γ=3、起始 `cached_len = C`，为 n = 0 / 1 / 3 各写一行表：

| n | feat 列构成 | teacher-force 的 token | 写入槽位 | 新边界捕获列 |
|---|---|---|---|---|
| 0 | [旧边界] | [current] | [C, C+1) | offset+0 |
| 1 | [旧边界, f_0] | [current, d_1] | [C, C+2) | offset+1 |
| 3 | [旧边界, f_0, f_1, f_2] | [current, d_1, d_2, d_3] | [C, C+4) | offset+3 |

   再验证 n=3 时需要 `C + 4 <= L_max`，与 `chain_capacity` 保证的 `C + k + 1 <= L_max`（k=3）一致。
3. **需要观察的现象**：n=0 时重建把槽 C 用**同一对** (current, 旧边界) 重写一遍（与链的 `draft_step` 在槽 C 写的内容同 pairing，幂等），并把边界推进到当前位置特征。
4. **预期结果**：三种 n 的读写范围都落在 `[C, C+n+1)`、边界列 = `offset+n`；这正是 `eagle3_output_budget_edges` 用 `[1,2,3,4,5,6,9]` 预算制造出各种跨度后仍能逐轮 lossless 的机制基础（奖励 token 由 verify 产出、下一轮才进草稿状态）。（纸面推演，GPU 门测试待本地验证。）

#### 4.5.5 小练习与答案

**练习 1**：n=0（草稿全被拒）时重建做了什么有用的事？
**答案**：两件：幂等地重写槽 C（同 pairing，无害），以及把边界特征推进到当前位置的目标特征——这样下一轮 `(新边界, 奖励 token)` 的配对仍然正确，草稿器不会卡在旧位置上。

**练习 2**：为什么被链污染的 K/V 列不需要显式清零？
**答案**：见 4.5.1——`draft_step` 先写槽 `position` 再以 `kv_len = position+1` 做注意力，且断言 `position == cached_len`，所以 `cached_len` 之外的陈旧列在被读到之前必然已被后续写覆盖。

**练习 3**：新边界为什么取第 n 列而不是第 n+1 列？
**答案**：提交前缀是 `[current, d_1..d_n]`，最后一个已提交位置的 span 下标是 n；第 n+1 列对应的是 verify 奖励 token 之外还没提交的位置。边界的语义是「最后一个已提交位置的目标特征」，它将与下一轮的 current token（即奖励 token）配对。

## 5. 综合实践

**任务：在纸面上仿真一个请求的完整 EAGLE-3 生命周期，并跑通全部纯 CPU 单测。**

设定：Qwen3-4B（36 层）目标 + AngelSlim EAGLE-3 草稿；prompt P=6，max_output=20，故 \( L_{\max} = \min(6+20+4, 40960) = 30 \)；γ=3。

1. **捕获层**：算出 `aux_hidden_state_layers(36) = [1, 17, 32]`，说明这三个索引在 0 基 post-layer 捕获路径上对应上游的 `(2, 18, 33)`。
2. **prefill_prompt**：写出被 teacher-force 的 5 个错位对 `(f_j, e_{j+1})`, j=0..4，落槽 `[0,5)`；边界 = `f_5`；`cached_len = 5`。
3. **三轮投机**，接受数依次为 n=3、n=1、n=0：
   - 每轮记录：轮首 C、`chain_capacity(C, 30, 3)` 授予的 k、链写过的槽、返回 span、verify 提交的 token 数（n+1）、rebuild 写入的槽区间 `[C, C+n+1)`、新边界列、轮末 `cached_len`；
   - 核对三轮后 `cached_len = 5 + 4 + 2 + 1 = 12`，且每轮的读写范围都严格不超过 `C + k + 1`。
4. **边界轮**：构造一个假想的最后一轮（C=28），验证 `chain_capacity(28, 30, 3) = 1`——链只滚一步，`28 + 1 + 1 = 30` 恰好用满 cache。
5. **回归**：运行 `cargo test --release -p pegainfer-qwen3 --lib eagle3`，确认 4.1/4.2/4.4 引用的三条单测通过；有 GPU 与双权重的话再跑 `cargo test --release -p pegainfer-qwen3 --test eagle3_speculative_gate`（注意一次只驻留一个目标模型的 GPU 互斥约定，见 [tests/eagle3_speculative_gate.rs:L61-L62](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/tests/eagle3_speculative_gate.rs#L61-L62)）。

产出：一张「轮次 × (C, k, 链槽, 提交 token, rebuild 槽, 新边界, cached_len)」的表 + 一段对 `+1` 余量设计（奖励 token）的说明。这个任务把 4.1 的索引换算、4.2 的几何、4.3 的状态、4.4 的链与容量、4.5 的重建全部串起来。

## 6. 本讲小结

- **捕获层换算**：上游 `(2, N/2, N-3)` 指「进入该层的残差流」，本 crate 0 基 post-layer 捕获等价于 `(1, N/2-1, N-4)`；36 层目标得 `[1, 17, 32]`，配严格递增硬校验。
- **模型结构**：`fc` 把三层目标特征融合成 `[hidden]`；单层 midlayer 的注意力吃 `[2*hidden]` 拼接输入且无 QK-norm；词嵌入复用目标；logits 出在缩小词表上、经主机侧 `d2t` 映射回目标词表。
- **状态分层**：`Eagle3RequestState` 只存单层 K/V、`cached_len` 与 pre-fc 边界特征；`Eagle3Scratch` 全部 `seq_len == 1`、一次分配跨轮复用——`EAGLE3_MAX_BATCH = 1` 的物质基础。
- **三个入口**：`prefill_prompt` 以 `(f_j, e_{j+1})` 错位对整段建 KV 并留边界；`chain_round` 用 `fc(边界)` 起跳、γ=3 逐步 argmax 链、轮末回退 `cached_len` 使轮次无副作用；`rebuild_after_verify` 按接受前缀 teacher-force 重建 `[C, C+n+1)` 并把边界推进到最后已提交位置。
- **容量公式**：\( k = \min(\gamma, L_{\max} - C - 1) \)，`+1` 为奖励 token；它与 rebuild 最多写到槽 `C+k` 的需求精确咬合。
- **每轮一次同步**：argmax 在设备、`d2t` 在主机，v1 每个草稿读回一次 id——已知的性能取舍。

## 7. 下一步学习建议

- **u6-l6（verify 前向缓冲与分段 CUDA Graph）**：看验证那一侧如何一次前向算 K+1 个位置、attention 为何留在图外——与本讲的 `chain_round` 首尾相接。
- **u6-l8（EAGLE-3 车道与门控）**：本讲三个入口的调用方。重点读 `eagle3_prefill_eligible`（贪心 + 整段 prompt 才有资格）、`DraftMeta.max_draft_batch = Some(1)` 如何经 `speculation_decision_at` 让宽批退回纯解码，以及 `tests/common/lossless.rs` 的无损判定如何同时服务 DFlash 与 EAGLE-3。
- **回读对照**：`src/executor/spec.rs`（u6-l1 的 KV 事务 schedule/apply/revert）与 `src/executor/dflash_lane.rs`——同一套捕获/ready 集事务在两种草稿器上的车道差异。
- **纸面延伸**：把本讲综合实践扩展成 n 序列仿真（如 3,3,2,1,0,...），验证任意接受序列下 `cached_len` 单调推进且永不越界——这基本就是 `eagle3_output_budget_edges` 在 GPU 上断言的行为。
