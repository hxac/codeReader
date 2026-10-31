# 批量解码 batch_decode：混合批的一步前向

## 1. 本讲目标

上一讲（u4-l1）我们走完了 prefill 主链路：提示词被切成 chunk 逐块喂入，最后取出末位置 hidden 过 lm_head。本讲进入生成阶段的心脏——**批量解码（batch decode）**：N 个并发请求各推进 1 个 token，混合批里全注意力层与线性注意力层在同一次前向中各自完成完全不同的工作。

学完本讲，你应该能够：

1. 讲清一次批解码中，full-attn 层与 linear-attn 层**各自读写哪些设备缓冲**（KV 池、slot_states、指针表、logits）。
2. 理解 `select_tokens_*` 的**逐请求参数采样**：为什么同一批里有的行走 argmax、有的走 FlashInfer 采样，`sample_seed` 的确定性从哪里来。
3. 解释 `DecodeGraphUse` 三种模式（`Serve` / `CaptureOnly` / `Replay`）分别服务单 GPU 与 TP 的哪条路径，以及为什么 TP 绝不允许服务中途懒捕获。

## 2. 前置知识

### 2.1 解码是怎么一回事

LLM 生成文本是自回归的：每一步把「上一步选出的 token」喂回模型，得到整个词表上的 logits（未归一化的分数），再从中挑出下一个 token。解码批（batch）就是把 N 个请求的这一步**拼在一起算**——每行 1 个 token，批矩阵是 `[hidden, N]` 而不是 `[hidden, seq]`。批得越大，单步吞吐越高，这就是 batch decode 存在的意义。

### 2.2 两种层在解码一步中各做什么（回顾）

Qwen3.5 是混合架构（4B 为 8 层 full + 24 层 linear），两种层在解码时的记忆方式完全不同：

- **全注意力层**：历史保存在**分页 KV 池**里（模型级共享，u3-l1）。解码一步只需把本 token 的 K/V 追加进池，然后用本 token 的 Q 去查询全部历史。
- **线性注意力层（Gated DeltaNet）**：历史被压缩进**固定大小的递推状态**（每请求一份 `RecurrentState`，u3-l2）。解码一步是一次状态更新：读旧状态 → 融合本 token → 写回，显存不随上下文增长。

上一讲 prefill 里两者的输入都是一整段；本讲里两者都退化为「每请求 1 行」，但共享与私有的缓冲边界一点没变。

### 2.3 你需要带上的旧结论

- **`KvView` 描述的是"步后"范围**：调度器在调模型前已为本步预留了 KV 页，所以本 token 的落位是 `view.seq_len() - 1`（u4-l1 的 `base_pos` 同源）。
- **指针表（`LinearStatePointerTables`）**：每层一张设备端 `u64` 指针数组，第 i 行指向槽 i 的状态地址；槽地址终身不变，所以表建一次就能逐 token 重放（u3-l2）。
- **`selection_vocab` / `decodable_vocab`**：logits 矩阵的宽度是对齐后的 `selection_vocab`，padding 列不是可发射 token，采样前要压 `-inf`（u2-l1、u2-l3）。
- **FFI guard 纪律**：`device_ptr` 返回（指针, guard），guard 必须活过 unsafe 发射（u3-l3）。

CUDA Graph 的捕获/重放机制本身是下一讲（u4-l3）的主角，本讲只把它当作「同一段内核序列可以整体重放」的黑盒使用。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| `src/batch_decode.rs` | **本讲主文件**：三个解码入口（eager / graph / hybrid）、逐层前向、逐请求采样入口 |
| `src/decode_buffers.rs` | `BatchDecodeBuffers35`：一步解码全部设备缓冲的预分配清单 |
| `src/batch_decode_graph.rs` | 桶常量 `BATCH_BUCKETS`、`bucket_for`、`BatchDecodeGraphState`（本讲借用，u4-l3 精讲） |
| `src/recurrent_state.rs` | `RecurrentState` 与 `LinearStatePointerTables`（指针表的三种填充方式） |
| `src/scheduler/backend.rs`、`src/scheduler/steps.rs`、`src/scheduler/mod.rs` | 单 GPU 调用侧：谁调解码、种子从哪抽 |
| `src/tp_executor/worker.rs` | TP 调用侧：eager 与 Replay 两条路 |
| `../pegainfer-sample/src/lib.rs` | `pegainfer_sample::select_batch`：逐请求采样的真正实现 |
| `src/config/model.rs` | `decode_group_is_compiled()`：GQA 组内核编译门 |

## 4. 核心概念与源码讲解

### 4.1 混合批前向：一步 decode 的完整数据流

#### 4.1.1 概念说明

「混合批前向」要解决的问题：一个 `[hidden, N]` 的批要连续穿过 32 层，其中 8 层是 full-attn、24 层是 linear-attn，两种层访问的设备缓冲完全不同——full-attn 读**所有请求共享**的分页 KV 池（按 `layer_idx` 索引到该层的 K/V 带），linear-attn 读写**每请求私有**的递推状态（按槽索引、经指针表间接寻址）。`batch_decode.rs` 就是把这两条支流缝进同一个层循环。

crate 提供三个解码入口，服务三种调用场景：

| 入口 | 填充到桶？ | 图？ | 递推状态来源 | 服务对象 |
|------|-----------|------|--------------|----------|
| `batch_decode_eager_logits` | 否 | 否 | 调用方直接传 `&mut [&mut RecurrentState]` | TP worker 的无图路径（rank 本地请求状态） |
| `batch_decode_graph(_padded)` | 是 | 捕获/重放 | `graph_state.slot_states`（槽 i = 批位置 i） | 单 GPU 服务、TP 图路径、预捕获扫描 |
| `batch_decode_batched_hybrid` | 否 | 否（禁捕获） | `graph_state.slot_states` | GQA 组无编译内核时的 eager 回退 |

三个入口最终都收敛到同一组「核」函数：`batch_decode_kernels_graph`（或 hybrid 变体），区别只在主机侧怎么准备输入和是否套图。

#### 4.1.2 核心流程

一次 `batch_decode_graph`（bs 个真实请求）的主机侧序列：

```text
1. bucket_for(bs) → padded_bs（取最小覆盖桶）
2. 校验：bs>0、bs==views.len()、bs≤槽数、padded_bs 是合法桶
3. 若 GQA 组未编译 → 只允许 Serve 模式，转 hybrid 回退
4. 逐请求算位置：
      pos_i = view_i.seq_len() - 1     # KvView 是"步后"范围
      断言 slot_states[i].seq_len == pos_i   # 递推状态必须恰在该位置
      slot_states[i].seq_len += 1       # 主机侧先推进计数
5. set_batch_size(padded_bs)；H2D 拷贝 token_ids/positions（零填充到桶）
6. sync_paged_views：H2D 拷贝页表元数据，padding 行指向保留 padding 页
7. 按 DecodeGraphUse 分派：run_or_capture / capture_only / launch_captured
```

其中第 4 步的位置公式与两个关键取整函数：

\[ \text{bucket}(bs) = \min\{\, b \in \{1,2,4,8,16,32,64\} : b \ge bs \,\} \]

\[ \text{splits}(bs) = \begin{cases} 32 & bs \le 2 \\ 16 & \text{否则} \end{cases} \]

padding 行数 = `padded_bs - bs`，它们照常参与前向（ kernels 迭代 `0..padded_bs`），只是输出列被调用方丢弃——这是让 CUDA Graph 网格形状恒定的代价。

#### 4.1.3 源码精读

**入口一：eager 路径（TP worker 的形状）。** 注释直说了它的定位：不填充、不碰图，递推状态由调用方直接供给——这正是 TP worker 管理 rank 本地请求状态需要的形状。

[src/batch_decode.rs:L288-L301](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L288-L301) — eager 入口的文档与签名：`token_ids`、KV `views`、`recurrent_states` 三者长度必须一致，缓冲容量校验后先做指针表 `validate_for`。

[src/batch_decode.rs:L319-L332](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L319-L332) — 位置推导与「先断言后推进」：`pos = view.seq_len() - 1`，断言 `recurrent_states[i].seq_len == pos`，然后 `seq_len += 1` 并收集 `positions`。这条断言是 KV 侧与递推侧的**对齐契约**——两边任何一侧多算/少算一步都会在这里炸出来，而不是静默产出错位 token。

[src/batch_decode.rs:L344-L371](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L344-L371) — GQA 组未编译时的路由：构造一个「每请求 1 行」的 `PrefillPagedPlan` 走 prefill 注意力内核。注释解释了关键不变量：**头切分不改变 q/kv 组比率**，所以这个 config 级谓词在每个 rank 上决定一致，重路由不引入额外集合通信。最后把指针表的 `state_ptrs` / `conv_state_ptrs` 逐层传入内核序列。

**入口二：图路径（本讲主线）。**

[src/batch_decode.rs:L386-L403](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L386-L403) — `batch_decode_graph` 只是 `bucket_for(token_ids.len())` 后委托给 `batch_decode_graph_padded`。

[src/batch_decode.rs:L411-L447](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L411-L447) — `batch_decode_graph_padded` 的校验段：桶必须真覆盖 `bs` 且在 `BATCH_BUCKETS` 里；未编译 GQA 组只允许 `Serve` 模式（hybrid 回退不进图），并用 `Once` 保证路由日志只打一次。

[src/batch_decode.rs:L457-L486](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L457-L486) — 槽位版的位置契约（与 eager 同构，只是状态换成 `graph_state.slot_states[i]`），以及零填充的 H2D：`token_ids_padded`、`positions` 都补 0 到桶大小，页表元数据交给 `sync_paged_views`。

[src/decode_buffers.rs:L212-L267](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/decode_buffers.rs#L212-L267) — `sync_paged_views`：把每个 `KvView` 的页索引、indptr、末页长度、chunk 大小拼成扁平数组拷上 GPU。**padding 槽的构造**在 [L237-L243](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/decode_buffers.rs#L237-L243)：指向保留的 `padding_page_id`，`seq_len=1`、`last_page_len=1`——FlashInfer 内核因此访问的是**合法但被丢弃**的内存，而不是野指针。

**层循环：两种支流汇合处。**

[src/batch_decode.rs:L632-L730](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L632-L730) — `batch_decode_kernels_graph`，图录制的闭包体。结构：

- [L644-L649](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L644-L649) `embedding_batch`：`token_ids_d` → `hidden`。
- [L651-L684](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L651-L684) 层循环：`input_layernorm` 后 `match &layer.attn` 分派——`FullAttention` 走批量内核（或 via_prefill 回退）并 `full_idx += 1`；`LinearAttention` 传入该层的两张指针表并 `linear_idx += 1`。**两个独立计数器正是 u2-l4 讲过的「KV 池按 full_idx 索引、递推状态按 linear_idx 索引」在解码侧的落地。**
- [L686-L716](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L686-L716) 残差相加 → `post_attention_layernorm` → MLP（`gate_up_proj` → `silu_mul` 融合 → `down_proj` → `all_reduce_hidden`）→ 残差回写。
- [L719-L727](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L719-L727) 最终 norm + `output_logits_into` 产出 `bufs.logits`，`debug_assert` 行数等于桶大小。

**full-attn 支流：三路由。**

[src/batch_decode.rs:L126-L144](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L126-L144) — 三个投影 GEMM（Q 带 gate 维、K/V 各一），随后融合预备内核一次完成 qk_norm、partial RoPE、K/V 落页与 GQA 广播——注意这里与 prefill 不同的是 K/V **不单独落页内核**，scatter 藏在注意力包装的开头（u3-l1 已讲）。

[src/batch_decode.rs:L146-L192](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L146-L192) — decode 内核三路由的批侧两条（第三条 via_prefill 见上）：

- `bs ≤ 16` 且本地组比率恰为 4 → **split-KV 内核**，附带 `split_partial_o/m/l` 三块 f32 累加器 scratch；
- 否则 → **FlashInfer 批量内核**（`kv_tile_indices_d` 版元数据）。

[src/decode_buffers.rs:L12-L44](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/decode_buffers.rs#L12-L44) — 路由依据的三个常量，注释就是一份微型基准报告：FlashInfer 每（请求, kv 头）一个 CTA、网格不随 KV 长度增长，小批时设备闲置（16 请求只有 64 个 CTA），所以小桶切 KV range 更快；`split_decode_splits` **按桶固定**而非按 KV 长度推导，因为捕获进 CUDA Graph 的网格形状必须恒定——过切分的请求只是让靠后的 CTA 空转。

[src/batch_decode.rs:L194-L212](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L194-L212) — gate 乘法（gated Q 的 sigmoid 门，u2-l4）走 `crate::ffi::attention_gate_batch_hd256_cuda`，两个 guard 活过 unsafe 发射（u3-l3 纪律）；然后 `o_proj` 回到 hidden 维、TP 下 `all_reduce_hidden`。

**linear-attn 支流：指针表驱动的五步。**

[src/batch_decode.rs:L840-L906](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L840-L906) — `batch_decode_linear_attention_slots`，文档点明关键性质：迭代 `0..padded_bs`，**每个槽的 GPU 地址稳定**，因此 CUDA Graph 安全；padding 槽照跑、输出列被忽略。五步：

1. [L860-L861](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L860-L861) 两个融合 GEMM：`in_proj_qkvz`（qkv 带在下、z 带在上）与 `in_proj_ba`（beta 下、alpha 上），消费者各读各的带；
2. [L863-L870](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L863-L870) `conv1d_decode_batch_into`：读 `conv_state_ptrs` 指向的每槽卷积抽头，原地滚动并融合 SiLU（u3-l2）；
3. [L871-L885](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L871-L885) `gated_delta_rule_decode_batch_into`：**唯一读写持久 GDN 状态的内核**，经 `state_ptrs` 按槽更新 `[H,K,V]` f32 状态；
4. [L887-L897](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L887-L897) z 门控 RMSNorm；
5. [L898-L904](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L898-L904) `out_proj` 回 hidden 维 + `all_reduce_hidden`——线性层在 TP 下的**唯一**通信点。

**缓冲清单。** [src/decode_buffers.rs:L46-L102](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/decode_buffers.rs#L46-L102) 把一步解码的全部设备缓冲列成字段组：共享 hidden 流（8 块）、full-attn 专属（5 块 + split scratch 3 块）、linear 专属（5 块）、元数据（8 个 `CudaSlice<i32>`）、采样 scratch。分配在 [L104-L186](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/decode_buffers.rs#L104-L186)，两处值得注意：`logits` 宽度是 `config.selection_vocab`（[L136](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/decode_buffers.rs#L136)，承接 u2-l1 的词表宽度故事）；split scratch 只按 `bs.min(SPLIT_DECODE_MAX_BATCH)` 分配（[L144-L159](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/decode_buffers.rs#L144-L159)），因为大桶不走 split 路径。[L188-L210](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/decode_buffers.rs#L188-L210) 的 `set_batch_size` 只改各 `HiddenStates` 的 `seq_len` 视图、不动分配——这就是「分配一次、逐桶重放」的显存纪律。

#### 4.1.4 代码实践

**实践：纸上推演一次 bs=3 的图解码主机侧序列。**

1. **实践目标**：在不碰 GPU 的前提下，完整说出 `batch_decode_graph` 从入参到图发射的每一步主机侧动作，检验对填充协议的理解。
2. **操作步骤**：
   - 读 [src/batch_decode.rs:L411-L525](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L411-L525)，把每个语句归入四类：校验 / 位置推进 / H2D / 图分派；
   - 假设三个请求的 `view.seq_len()` 分别为 100、57、2000，写出 `positions` 向量、`token_ids_padded` 与 `positions` 填充后的长度、`sync_paged_views` 里 padding 行的四项元数据（页索引、indptr 增量、last_page_len、chunk_size）；
   - 用 `bucket_for(3)` 核对桶，再算 `split_decode_splits(该桶)` 并确认本桶走哪条注意力路由。
3. **需要观察的现象**：纯源码阅读；若有 GPU + 权重，可运行 `PEGAINFER_TEST_MODEL_PATH=models/Qwen3.5-4B cargo test --release -p pegainfer-qwen35 --features qwen35 --test e2e_scheduler` 观察解码步真实发生（**待本地验证**）。
4. **预期结果**：一张「语句 → 类别」表 + 一份填充协议答案（桶 4、padding 1 行、`positions = [99, 56, 1999, 0]`、padding 元数据指向 `padding_page_id` 且 `seq_len=1`）。

#### 4.1.5 小练习与答案

**练习 1**：bs=5 时 `bucket_for` 返回几？padding 几行？它们的 KV 元数据指向哪里、为什么不会崩？

答案：返回 8（最小覆盖桶），padding 3 行；页索引指向保留的 `padding_page_id`，`seq_len=1`、`last_page_len=1`，FlashInfer 访问的是合法但被丢弃的内存，输出列被调用方忽略。

**练习 2**：为什么 eager 路径要断言 `recurrent_states[i].seq_len == view.seq_len() - 1`，而不是直接信任某一边？

答案：`KvView` 描述的是调度器预留页之后的"步后"范围，本 token 落在 `seq_len-1`；递推状态必须恰好在同一位置起算。KV 侧与递推侧是两套独立推进的状态，任何一侧多算/少算一步都会让 RoPE 位置或 GDN 状态与 KV 错位——断言把这种错位在发射前变成显式错误。

**练习 3**：GQA 组未编译时图路径为什么不直接报错，而是转 `batch_decode_batched_hybrid`？该回退为何在 TP 下也是安全的？

答案：组比率不在编译清单 `SUPPORTED_GQA_GROUP_SIZES` 内（如 27B 的组 6）只是缺批量解码内核，模型仍可服务，所以降级到"每请求 1 行的 prefill 注意力内核"保持可用；头切分不改变 q/kv 组比率，config 级谓词在每个 rank 上结论一致，重路由不引入额外集合通信，因此 TP 安全。但回退是 eager 的，所以只允许 `Serve` 模式（禁止捕获，见 4.3）。

### 4.2 逐请求采样：select_tokens_* 与 sample_seed

#### 4.2.1 概念说明

前向产出 `bufs.logits`（`[selection_vocab, padded_bs]`）之后，还差最后一步：每个请求**用自己的采样参数**从自己那一行 logits 里挑 token。难点在于批内异构：请求 A 可能是 `temperature=0`（贪心），请求 B 是 `top_p=0.9` 的随机采样，请求 C 甚至自带逐请求 `seed`。`batch_decode.rs` 提供两个薄封装把这件事委托给 `pegainfer_sample::select_batch`，自己只负责断言与 scratch 复用。

「逐请求」（varied）这个名字就来自：参数按行变化，而不是全批共享一组。

#### 4.2.2 核心流程

种子与数据的两条链：

```text
种子链（单 GPU）：
  启动 seed ─→ SchedulerCore { rng: StdRng::seed_from_u64(seed) }
          ─→ 每步 steps.rs: sample_seed = rand::RngExt::random(rng)
          ─→ select_tokens_*(..., sample_seed, ...)
          ─→ select_batch(seed) ─→ FlashInfer philox 引擎

数据链：
  bufs.logits[行 i] + params[i]（该请求的 SamplingParams）
          ─→ 行内路由：
              effectively_greedy(params[i]) → 批量 argmax
              否则                        → FlashInfer 批量采样（top-k/top-p）
              带 seed 的行                → 单行调用,种子 = mix_seed(seed, step)
```

确定性来源有三层：调度器 `StdRng` 由启动 seed 决定；每步种子按调度事件顺序推进；带请求级 seed 的行采样是 `(request_seed, step)` 的纯函数——与批组合无关，请求换个批次邻居也不改变它的 token 流。

#### 4.2.3 源码精读

**两个采样入口。**

[src/batch_decode.rs:L47-L76](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L47-L76) — `select_tokens_from_logits_varied`：接收**外部传入**的 logits（prefill 末块的输出），断言行数与 params 对齐、不超容量，然后 `bufs.steps` 清零重置到 `params.len()`，委托 `pegainfer_sample::select_batch`。

[src/batch_decode.rs:L78-L110](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L78-L110) — `select_tokens_batch_varied`：唯一区别是 logits **固定读 `bufs.logits`**（decode 图缓冲）。注意断言 `params.len() <= bufs.logits.seq_len`：图路径下 logits 行数是桶大小，采样只消费前 `bs` 行真实行，padding 行的 logits（来自 token 0 的嵌入前向）自然被忽略。

`bufs.steps` 的复用见 [src/decode_buffers.rs:L92-L97](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/decode_buffers.rs#L92-L97)：注释说明它目前恒为 0——调度器还没把各请求的生成计数接进来（标注 `sampling-parity 1b`），它只在「params 带 seed 的行」才影响采样。

**采样实现的路由规则。**

[../pegainfer-sample/src/lib.rs:L162-L188](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L162-L188) — `select_batch` 的文档即规范：argmax 行合并成**一次批量索引 argmax**，其余行合并成**一次** FlashInfer temperature/top-k/top-p 采样；`effectively_greedy` 包括显式贪心和「top_p 核小于等于 1/vocab」的退化情形——把这种行走 argmax 是刻意的确定性选择（拒绝采样器会在 bf16 并列最大值里任选一个）；带 seed 的行必须单独走单行调用，因为 FlashInfer 把批内位置折进 philox 子序列，批组合一变流就变。

[../pegainfer-sample/src/lib.rs:L189-L229](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L189-L229) — 函数体的前置校验：steps 长度、scratch 行容量、logits 行数、词表宽度逐一 `ensure!`；`vocab == scratch.vocab` 用的正是 logits 矩阵的 `hidden_dim`（即 `selection_vocab`），印证 u2-l1「padding 列不能移动 top_p 核」的下游约束。

**调度侧的调用与种子抽取。**

[src/scheduler/mod.rs:L388-L415](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/scheduler/mod.rs#L388-L415) — 调度器持有 `rng: StdRng`，构造时 `StdRng::seed_from_u64(seed)`（[L392](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/scheduler/mod.rs#L392)、[L412](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/scheduler/mod.rs#L412)）。

[src/scheduler/steps.rs:L9-L52](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/scheduler/steps.rs#L9-L52) — 每个调度步骤先 `let sample_seed = rand::RngExt::random(rng)`（[L18](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/scheduler/steps.rs#L18)），prefill 采样再独立抽一次（[L31](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/scheduler/steps.rs#L31)）——种子由调度事件顺序决定，与前向内部行为解耦。

[src/scheduler/backend.rs:L493-L540](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/scheduler/backend.rs#L493-L540) — 单 GPU 两个消费点：`sample_prefill_logits` 调 `select_tokens_from_logits_varied`（prefill logits），`sample_decode_logits` 调 `select_tokens_batch_varied`（`graph_state.buffers.logits`）；两者都在采样前先 `snapshot_requested_logprobs` 把需要的行拷回 CPU（logits 缓冲马上会被下一步覆盖）。TP 侧的采样集中在 rank 0：[src/tp_executor/worker.rs:L818-L825](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/tp_executor/worker.rs#L818-L825) 在 `rank != 0` 时直接返回空，种子则随命令下发（`RunDecodeStep { sample_seed, .. }`，[L563-L575](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/tp_executor/worker.rs#L563-L575)）——控制器抽种、全员同种，rank 0 的采样结果与各 rank 前向天然对齐。

#### 4.2.4 代码实践

**实践：画出种子传递链路图。**

1. **实践目标**：把 `sample_seed` 从调度器 RNG 到 GPU philox 引擎的完整路径画成两张图（单 GPU / TP），并标出「哪一步引入确定性、哪一步引入变化」。
2. **操作步骤**：
   - `Grep` 搜索 `sample_seed` 在 `src/scheduler/` 与 `src/tp_executor/` 的全部出现点；
   - 对单 GPU：标注 `mod.rs 构造 → steps.rs 抽取 → backend.rs 传递 → batch_decode.rs 封装 → select_batch`；
   - 对 TP：标注 `steps.rs 抽取 → tp 命令字段序列化 → worker.rs 反序列化 → rank 0 采样 / 其余 rank 丢弃`；
   - 读 [../pegainfer-sample/src/lib.rs:L179-L188](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L179-L188)，在图上补注「带请求 seed 的行」这条特殊支路。
3. **需要观察的现象**：纯源码阅读；可对照 `tests/sampling_behavior.rs`（有 GPU + 权重时）验证行为断言（**待本地验证**）。
4. **预期结果**：两张链路图，能回答「同一请求换一个批次邻居，它的 token 流会不会变」（不会：未带 seed 的行由批级种子+批组合决定，带 seed 的行是 `(request_seed, step)` 的纯函数）。

#### 4.2.5 小练习与答案

**练习 1**：`select_tokens_from_logits_varied` 和 `select_tokens_batch_varied` 为什么是两个函数而不是一个？

答案：logits 来源不同——前者接收调用方传入的 `&HiddenStates`（prefill 末块输出，行数恰为请求数），后者固定读解码缓冲 `bufs.logits`（行数是桶大小，真实行在前）。分开封装让每个入口的断言能精确表达自己的不变量（`params.len() == logits.seq_len` vs `params.len() <= logits.seq_len`）。

**练习 2**：`bufs.steps` 目前恒为 0，这会影响不带请求 seed 的普通请求吗？

答案：不会。`steps[i]` 只对 params 带 `seed` 的行有意义（参与 `mix_seed(request_seed, step)`）；普通行走批级种子的 FlashInfer 采样或 argmax，与 steps 无关。这也是它当前可以安全地保持全 0 的原因。

**练习 3**：为什么「top_p 核小于等于 1/vocab」的行被路由到 argmax？

答案：这种核里只剩 argmax 一个幸存者，走拒绝采样反而会在 bf16 并列最大值中任选一个（不确定性），且白付一次 softmax；argmax 保证_effectively greedy_ 的请求行为确定。`vocab` 用的是 scratch 的 selection 宽度，保证 padding 列不算进核。

### 4.3 图使用模式 DecodeGraphUse：单 GPU 懒捕获与 TP 只重放

#### 4.3.1 概念说明

CUDA Graph 的捕获（record）与重放（replay）不是对称操作：捕获需要在真实流上跑一遍内核序列并录制；重放只是整段入队。对单 GPU，第一次遇到某桶时捕获、以后重放（懒捕获）是最省事的策略。但 TP 下这条路是**死锁级错误**：图里录着 NCCL 集合通信，一个 rank 在服务中途进入捕获时，对端 rank 在重放旧图——两边执行的集合通信序列错开（desync），集群挂死。所以 TP 的纪律是：**启动时全局有序地预捕获全部桶，服务期只重放**。`DecodeGraphUse` 就是把这条纪律编码进类型的三值开关。

#### 4.3.2 核心流程

| 模式 | 行为 | 使用者 |
|------|------|--------|
| `Serve` | 已捕获则重放，未捕获则**懒捕获后重放** | 单 GPU：调度器 `decode_graph`、`unified_forward` 三处、teacher-forcing 执行器 |
| `CaptureOnly` | 只录制 + 实例化 + 上传，**不发射** | TP 启动预捕获扫描的 Capture 相位 |
| `Replay` | 纯重放；图缺失即报错 | TP 服务期每步；预捕获扫描的 Launch 相位（排空录制期集合通信） |

#### 4.3.3 源码精读

[src/batch_decode.rs:L30-L44](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L30-L44) — 枚举定义，文档注释直述动机：「TP 服务绝不懒捕获：一个 rank 中途捕获而对端重放，会让录制中的 NCCL 集合通信 desync」。

[src/batch_decode.rs:L488-L524](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L488-L524) — 分派点：先 `std::mem::take(&mut graph_state.graphs)` 把图挪出结构体避免闭包分裂借用，再按模式三分支。`Replay` 分支的注释（[L517-L521](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L517-L521)）值得逐字读：重放是纯入队，每个桶都该被启动扫描录好——这里缺图意味着扫描被跳过或不完整，**大声失败，绝不服务中途捕获**。

调用点全景（可用 `Grep "DecodeGraphUse::"` 复核）：

- **Serve**：[src/scheduler/backend.rs:L476-L491](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/scheduler/backend.rs#L476-L491)（单 GPU `decode_graph`）、[src/unified_forward.rs:L137](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/unified_forward.rs#L137)/[L244](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/unified_forward.rs#L244)/[L330](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/unified_forward.rs#L330)（统一步）、[src/executor.rs:L305](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/executor.rs#L305)（teacher-forcing，u8-l1 的精度门就骑在这条路上）；
- **Replay**：[src/tp_executor/worker.rs:L903-L909](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/tp_executor/worker.rs#L903-L909)（TP 图解码每步）；
- **CaptureOnly + Replay**：[src/tp_executor/worker.rs:L1031-L1034](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/tp_executor/worker.rs#L1031-L1034)（预捕获扫描逐桶 Capture 然后 Launch——四阶段扫描的细节在 u7-l3）。

另外注意 [src/batch_decode.rs:L433-L437](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L433-L437) 的交叉约束：hybrid 回退（未编译 GQA 组）只支持 `Serve`——eager 的注意力内核每步读增长的 KV，捕获进图反而会「欠读」（注释引 `verify_graph` 的记录），所以非 Serve 模式直接拒绝。

#### 4.3.4 代码实践

**实践：枚举全部调用点并分类。**

1. **实践目标**：亲手验证「三种模式各服务哪条路径」的结论，而不是背表格。
2. **操作步骤**：
   - 在 crate 根目录运行 `grep -rn "DecodeGraphUse::" src/`（或用编辑器的全局搜索）；
   - 对每个命中点记录：文件、行号、所在函数、模式、判定该调用属于单 GPU 还是 TP（提示：看函数所在模块——`scheduler/backend.rs` 与 `unified_forward.rs` 是单 GPU，`tp_executor/` 是 TP，`executor.rs` 是独立执行器）；
   - 特别读一遍 `tp_executor/worker.rs` 中预捕获扫描处 `CaptureOnly` 与 `Replay` 成对出现的代码，写下两行的先后顺序。
3. **需要观察的现象**：搜索结果的分布本身——你会看到 `Serve` 占绝对多数，`Replay`/`CaptureOnly` 只出现在 `tp_executor`。
4. **预期结果**：一张 8 行左右的调用点分类表，与 4.3.2 的表格互相印证。
5. 本实践为纯源码阅读，无需 GPU（**无需验证**）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Replay` 模式下图缺失时要报错而不是现场捕获？

答案：TP 的每个桶都应由启动预捕获扫描录制；缺图说明扫描被跳过或不完整。此时若现场捕获，本 rank 进入录制、对端重放旧图，录制中的 NCCL 集合通信与对端错开，集群 desync——比报错严重得多。单 GPU 的 `Serve` 才被允许懒捕获，因为它没有对端。

**练习 2**：`batch_decode_graph_padded` 为什么允许调用方直接传桶大小，而不是永远从 `bs` 推导？

答案：注释给出了动机——padding 行本来就走保留 padding 页和空闲递推槽，所以「只需要一个桶」的调用方（TP 预捕获扫描）可以只带 1 个真实行，捕获/发射桶 `padded_bs` 的图，而不必真的占住 `padded_bs` 个 KV 页。这把「图的形状」与「真实负载」解耦。

**练习 3**：hybrid 回退为什么禁止 `CaptureOnly`/`Replay`？

答案：hybrid 的全注意力走每步重建的 prefill 计划（`one_token_paged_plan` 读当步 KV 视图），是 eager 语义；捕获成图会固定住录制时的 KV 范围，之后 KV 增长图却重放旧范围（「欠读」）。所以它只允许单 GPU 的懒 Serve 模式，且永远不进 TP 图路径。

## 5. 综合实践

**任务：整理 batch=8 的 decode 步「层类型 → 输入 → 输出 → 内核」缓冲表。**

这是本讲的收口实践：把 4.1 的数据流、4.2 的 logits 去向、4.3 的图约束拼在一张表里。假设 8 个并发请求、全部层已捕获（走图重放）、GQA 组比率 4。

1. **实践目标**：对一步 `batch_decode_graph(bs=8)`，逐层列出 full-attn 与 linear-attn 各自消费与生产的设备缓冲及内核，做到不查源码也能默写。
2. **操作步骤**：
   - 先自己填下面两张空表的「输入/输出/内核」三列（提示：full-attn 看 [src/batch_decode.rs:L112-L214](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L112-L214)，linear 看 [L840-L906](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L840-L906)，公共部分看 [L632-L730](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L632-L730)）；
   - 填完再对照下方参考答案；
   - 思考追加题：bs=8 时桶取多少？split 路由选哪条？`split_decode_splits` 给几？
3. **需要观察的现象**：纯源码阅读（有 GPU + 权重时可跑 `e2e_scheduler` 集成测试对照，**待本地验证**）。
4. **预期结果（参考答案）**：

**公共流水（每层之前/之后）**

| 阶段 | 输入 | 输出 | 内核 |
|------|------|------|------|
| 嵌入 | `token_ids_d` | `hidden` | `ops::embedding_batch` |
| 入层 norm | `hidden` + `input_layernorm` | `normed` | `ops::rms_norm_batch_offset_into` |
| 残差 1 | `hidden` + `attn_results` | `hidden_mid` | `ops::add_batch_into` |
| MLP | `hidden_mid` → norm → `gate_up_proj` → 激活 → `down_proj` | `mlp_out` | `rms_norm…` / `gemm_into` / `silu_mul_fused_batch_into` / `gemm_into`（TP 再 `all_reduce_hidden`） |
| 残差 2 | `hidden_mid` + `mlp_out` | `hidden` | `ops::add_batch_into` |
| 末层 | `hidden` → norm → lm_head | `logits`（宽 `selection_vocab`） | `rms_norm…` / `output_logits_into`（内含 pad 抑制） |
| 采样 | `logits` 前 8 行 + 每行 `params` + `sample_seed` | `Vec<u32>` 8 个 token | `pegainfer_sample::select_batch`（argmax 批 + FlashInfer 批） |

**full-attn 层（每层一次）**

| 输入 | 输出 | 内核 |
|------|------|------|
| `normed`；权重 `q/k/v_proj`、`q_norm/k_norm`；`cos/sin_cache`；`positions_d` | `q_full`、`k_attn`、`v_attn`；**本步 K/V 落入 KV 池**（`layer_idx` 层） | 3×`ops::gemm_into` + `qk_norm_partial_rope_batched_decode_hd256_into`（含落页 scatter） |
| `q_attn/k_attn/v_attn`；**共享 KV 池** `kv_buffer`（该层历史）；页表元数据 `page_indices_d/page_indptr_d/last_page_len_d/request_indices_d/kv_chunk_size_d` | `attn_out_full` | bs=8≤16 且组=4 → `paged_attention_batch_decode_split_hd256_into`（splits=16，用 `split_partial_o/m/l`）；否则 FlashInfer `paged_attention_batch_decode_hd256_into` |
| `q_full`（gate 半区）+ `attn_out_full` | `attn_out_full`（门控后） | `ffi::attention_gate_batch_hd256_cuda` |
| `attn_out_full` + `o_proj` | `attn_results` | `gemm_into` + `all_reduce_hidden`（TP） |

**linear-attn 层（每层一次）**

| 输入 | 输出 | 内核 |
|------|------|------|
| `normed` + `in_proj_qkvz` / `in_proj_ba` | `qkvz`（qkv 带 + z 带）、`ba`（beta + alpha） | 2×`ops::gemm_into` |
| `qkvz` 的 qkv 带；`conv1d_weight`；**指针表 `conv_state_ptrs[l]`** → 各槽 conv_state | `qkv_conv`；**conv_state 原地滚动** | `ops::conv1d_decode_batch_into` |
| `qkv_conv`；`ba` 两带；`dt_bias/a_log`；**指针表 `state_ptrs[l]`** → 各槽 GDN 状态 | `gdr_out`；**GDN 状态原地更新**（唯一读写持久递推状态的内核） | `ops::gated_delta_rule_decode_batch_into` |
| `gdr_out` + z 带 + `norm_weight` | `normed_gated` | `ops::rms_norm_gated_batch_into` |
| `normed_gated` + `out_proj` | `attn_results` | `gemm_into` + `all_reduce_hidden`（TP 唯一通信点） |

追加题答案：bs=8 → 桶 8（恰好命中，无 padding）；`SPLIT_DECODE_MAX_BATCH=16` 且组比率 4 → 走 split-KV；`split_decode_splits(8) = 16`（8 > 2）。

## 6. 本讲小结

- 批解码是「N 个请求各 1 token」的一次混合前向：full-attn 读**共享**分页 KV 池（按 `full_idx` 索引层），linear-attn 经**每层指针表**按槽原地更新私有递推状态（按 `linear_idx` 索引），两条支流在同一个层循环里由 `LayerKind` 分派。
- 三个入口收敛到同一组内核序列：eager（TP 无图、状态由调用方直传）、graph（桶填充 + 图）、hybrid（GQA 组未编译的 eager 回退，只许 Serve）。位置契约 `pos = view.seq_len() - 1` 与「断言递推状态恰在该位置」把 KV 侧与递推侧锁死对齐。
- padding 行是协议表面：KV 元数据指向保留 padding 页（seq_len=1），递推侧用空闲槽，输出列被丢弃——以此换取 CUDA Graph 恒定网格。
- full-attn 解码三路由：小桶 + 组比率 4 走 split-KV（splits 按桶固定：≤2 给 32、否则 16），否则 FlashInfer 批量内核，组未编译走 via_prefill 回退。
- 逐请求采样把异构参数折进两次批内核：贪心行合一次 argmax，其余行合一次 FlashInfer 采样；种子链 = 启动 seed → `StdRng` → 每步抽取 → philox，带请求 seed 的行是 `(seed, step)` 的纯函数。
- `DecodeGraphUse` 三值开关编码 TP 纪律：单 GPU 可懒捕获（Serve），TP 启动预捕获（CaptureOnly）后服务期只重放（Replay），缺图大声失败——因为一 rank 捕获一 rank 重放会让录制中的 NCCL 集合通信 desync。

## 7. 下一步学习建议

- **u4-l3（CUDA Graph 与批次桶）**：本讲把 `BatchDecodeGraphState` 当黑盒，下一讲拆开它——`BATCH_BUCKETS` 桶策略、slot i 与批位置 i 的固定映射、`copy_state_to_slot` / `move_slot_within` 的槽位管理契约。
- **u5-l3（步骤执行与后端）**：本讲的 `decode_graph` / `sample_decode_logits` 是被 `steps.rs` 的驱动函数调用的，下一单元把这些点接成完整调度循环。
- **u8-l1（精度门）**：`executor.rs` 用 `DecodeGraphUse::Serve` + teacher-forcing 复用本讲的解码前向对拍 HF golden logits——读完本讲再去看精度门会非常顺畅。
- 顺手延伸：`docs/models/qwen35/decode-kernel-attribution.md` 记录了 split-KV 与 FlashInfer 路由切换背后的实测数据，是 `SPLIT_DECODE_MAX_BATCH=16` 这个常数的出处。
