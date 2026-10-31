# CUDA Graph 与批次桶：BatchDecodeGraphState 的指针稳定性

## 1. 本讲目标

上一讲（u4-l2）我们看完了 `batch_decode` 的一次混合批前向：N 个请求各推进 1 个 token，全注意力层读共享分页 KV 池，线性注意力层按槽原地更新递推状态。本讲回答一个更底层的问题：**这条前向凭什么能被录进 CUDA Graph 反复重放？**

读完本讲你应该能够：

1. 解释「slot i ≡ 批位置 i」这条固定映射为何是图重放的正确性前提——图里烧录的是设备地址，地址变了重放就写错内存。
2. 推导 `bucket_for` 的取整行为，以及 `decode_admission_batch`（调度准入上限）与 `reserved_decode_slots`（物理桶容量）为何要分开（#470）。
3. 写出 `copy_state_to_slot`（入槽）与 `move_slot_within`（搬槽）各自的调用时机，并解释搬槽后「from 槽留旧字节」为何是良性设计。

## 2. 前置知识

**CUDA Graph 是什么。** 一次 decode 前向要发射几十上百个内核（GEMM、norm、attention、递推更新、采样……）。逐个发射的 CPU 端开销在小 batch 下会盖过 GPU 计算本身。CUDA Graph 把「发射哪些内核、按什么参数」整段录制（capture）成一张图，之后每步只需一次 `launch`（replay），把 CPU 发射开销摊平。

**重放的代价：形状与地址必须恒定。** 图录制的是内核序列和它们的参数值。重放时驱动**不会**重新计算网格形状、也不会重新取设备指针——录制那一刻是什么，重放就永远是什么。这意味着两件事必须跨步不变：

- 内核网格形状（所以 batch 维度不能每步都变）；
- 所有输入/输出缓冲的设备地址（所以内存不能每步重新分配）。

**桶（bucket）策略解决第一个问题。** decode 批大小每步都在变（请求到来、请求结束）。解法是把 batch 吸附到一个固定的 2 的幂集合 \(\{1,2,4,8,16,32,64\}\)，每个桶录一张图；真实批不足桶时用 padding 行补齐，padding 行的输出被丢弃。

**固定槽位解决第二个问题。** Qwen3.5 的难点在线性注意力层：每个请求的 GDN 递推状态是「一份独立的、每步原地更新的设备内存」。如果第 i 行请求的状态地址每步都换，录进图的指针就全部失效。所以本 crate 在启动期为每个解码槽预分配一份状态，**地址终身不变**，请求只是「被指派到某个槽」。

**两个术语。** D2D 拷贝（`memcpy_dtod`）指显存到显存的复制，不经过主机；slot（槽）指 `BatchDecodeGraphState.slot_states` 里的一份预分配递推状态，下文统一用「槽」。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `src/batch_decode_graph.rs` | 本讲主角：桶常量 `BATCH_BUCKETS`、取整函数 `bucket_for`、`BatchDecodeGraphState`（槽、指针表、每桶一张图）以及两个 D2D 原语 |
| `src/decode_buffers.rs` | `BatchDecodeBuffers35`：按最大批一次性分配的解码缓冲；`sync_paged_views` 负责 padding 行的 KV 元数据 |
| `src/weights.rs` | 加载期把 `--max-batch` 拆成「准入上限 + 物理桶」；`create_batch_decode_graph_state*` 建图状态 |
| `src/batch_decode.rs` | `batch_decode_graph_padded`：取桶、补 padding、按 `DecodeGraphUse` 三模式走图 |
| `src/recurrent_state.rs` | `LinearStatePointerTables`（每槽每层的地址表）与 `RecurrentState::copy_from`（D2D 整份拷贝） |
| `src/scheduler/plan.rs` | 纯函数：`slot_for_new_request`、`compaction_after_retire`、`SlotCompaction` |
| `src/scheduler/backend.rs` / `steps.rs` | 调度侧：晋升落槽、退休压缩、准入上限计算 |
| `src/tp_executor/worker.rs` | TP 侧：启动预捕获扫描（显式传桶）、`drop_request` 里的 `move_slot_within` |
| `src/executor.rs` | 低层执行器（测试/基准用）自带的同款入槽与压缩逻辑 |

## 4. 核心概念与源码讲解

### 4.1 桶取整：BATCH_BUCKETS、bucket_for 与「物理桶 / 准入上限」分离

#### 4.1.1 概念说明

图只能对固定 batch 录制，但服务里 batch 是连续变量。本 crate 的解法分两层：

1. **桶集合是编译期常量**：`[1, 2, 4, 8, 16, 32, 64]`，最大 64。任意 batch 向上取整到最近的桶。
2. **用户请求的 `--max-batch` 不必是桶**：比如 `--max-batch 5`。若强行只分配 5 个槽，那么 active=5 时 `bucket_for(5)=8`，图却只覆盖 5 行——对不上。所以加载期把 5 **向上取整成物理桶 8**（分配 8 个槽、8 行缓冲、捕获 ≤8 的桶），同时调度器仍然**最多准入 5 个**请求。这就是 issue #470 引入的 `decode_admission_batch` 与 `reserved_decode_slots` 分离：物理世界按桶对齐，策略世界保留用户请求的精确值。

#### 4.1.2 核心流程

```text
加载期（weights.rs）:
  校验 1 <= max_batch <= 64
  decode_admission_batch = max_batch          # 调度准入上限（保留原值 5）
  reserved_decode_slots    = bucket_for(5) = 8 # 物理槽/缓冲/图桶容量

调度期（backend.rs）:
  准入条件: active < decode_admission_batch.min(slot 数) = 5

解码步（batch_decode.rs）:
  padded_bs = bucket_for(active.len())   # 5 → 8，4 → 4，3 → 4
  补 padding 行到 padded_bs
  图索引 = BATCH_BUCKETS 中 padded_bs 的位置 → graphs[idx]
```

取整规则用一句话概括：**返回第一个 ≥ bs 的桶；bs > 64 直接 panic**（加载期校验保证这条路实际不可达）。

#### 4.1.3 源码精读

桶常量与取整函数，定义了整个解码路径的形状语言：

[src/batch_decode_graph.rs:13-30](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode_graph.rs#L13-L30) —— `BATCH_BUCKETS = &[1,2,4,8,16,32,64]`、`MAX_BATCH = 64`，以及 `bucket_for(bs)`：线性扫描桶表，返回第一个 ≥ `bs` 的桶。

加载期的「准入 / 物理」分离，是 #470 的落点：

[src/weights.rs:141-156](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L141-L156) —— 先 `ensure!` 请求的 `max_batch` 落在 `1..=MAX_BATCH`，再把原值存进 `decode_admission_batch`，随即 `max_batch = bucket_for(max_batch)` 向上取整。此后所有尺寸都按物理桶算，只有准入上限记得用户原值。

这两个字段的文档与存储：

[src/weights.rs:74-81](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L74-L81) —— `reserved_decode_slots`「总是某个 `BATCH_BUCKETS` 值」，`decode_admission_batch`「当请求不是桶时可以低于物理槽（例如 `--max-batch 5` 分配桶 8 但只准入 5）」。

调度器真正用来做准入判断的是二者取 min：

[src/scheduler/backend.rs:273-280](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/scheduler/backend.rs#L273-L280) —— `max_batch()` 返回 `decode_admission_batch.min(slot_states.len()).max(1)`：准入不超过用户请求，也不超过物理槽。

创建图状态时同样以桶为单位：

[src/scheduler/backend.rs:245-249](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/scheduler/backend.rs#L245-L249) —— `graph_capacity = bucket_for(max_batch)`，再交给 `create_batch_decode_graph_state_with_capacity`。

「按物理桶对齐」还有两处下游体现，说明这不是孤立参数：

- GEMM 调优只对 ≤ 物理桶的桶做：[src/weights.rs:511-518](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L511-L518) 过滤 `bucket <= GEMM_LT_MAX_N && bucket <= reserved_decode_slots`。
- TP 预捕获扫描在桶超过槽数时停止：[src/tp_executor/worker.rs:1040-1043](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/tp_executor/worker.rs#L1040-L1043)（`if bucket > graph_state.slot_states.len() { break; }`）。

另外，TP 模式下 NCCL 集合通信也按每个桶的消息尺寸预热一次——因为每个桶是一张独立的图，各自录制了自己的 all-reduce：

[src/weights.rs:433-452](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L433-L452) —— `warmup_tp_collective` 遍历 `BATCH_BUCKETS`，对每个桶尺寸各做一次 all-reduce，注释说明原因：懒连接发生在 `cuStreamBeginCapture` 之内会把捕获卡死。

#### 4.1.4 代码实践

**实践目标**：把 `bucket_for` 的取整行为变成肌肉记忆，并验证「准入 5 / 物理 8」这条链在源码里的每个环节。

**操作步骤**：

1. 纸面推导：对 bs = 1..9 逐个写出 `bucket_for(bs)` 的结果，列成表。
2. 用一条 shell 命令核对（在任意有 bash 的机器上可跑）：

   ```bash
   for bs in 1 2 3 4 5 6 7 8 9; do
     for b in 1 2 4 8 16 32 64; do
       if [ "$b" -ge "$bs" ]; then echo "bs=$bs -> bucket=$b"; break; fi
     done
   done
   ```

3. 源码核对：对照 [src/batch_decode_graph.rs:20-30](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode_graph.rs#L20-L30) 确认你的表与实现一致。
4. （可选，需构建工具链）运行调度层已有的桶/槽相关单测：`cargo test --release -p pegainfer-qwen35 --features qwen35 --lib graph_slot_assignment`。注意 `qwen35` feature 会触发 build.rs 的 CUDA + Python/Triton AOT 构建链（见 u1-l2），无工具链时此步**待本地验证**。

**需要观察的现象**：步骤 2 输出中 bs=5、6、7 都映射到 bucket=8——这正是「准入 5 个请求时，每步重放的是桶 8 的图」的来源。

**预期结果**：`1→1, 2→2, 3→4, 4→4, 5→8, 6→8, 7→8, 8→8, 9→16`。只有 `bs > 64` 才会触发 `bucket_for` 的 panic（加载期校验已把 `max_batch` 限制在 1..=64，正常运行不可达）。

#### 4.1.5 小练习与答案

**练习 1**：为什么不让用户直接输入桶值、省掉取整？
**答案**：桶是实现细节。把 `--max-batch` 约束成桶值会把 CUDA Graph 的形状要求泄漏到用户接口上；取整让用户可以填任意 1..=64，物理世界内部对齐。代价只是多占几个槽的显存（见 4.2 的显存公式）。

**练习 2**：`--max-batch 64` 时 `decode_admission_batch` 和 `reserved_decode_slots` 各是多少？`--max-batch 1` 呢？
**答案**：64/64（请求恰是桶，两者相等）；1/1（同理）。只有请求值不是 2 的幂时二者才分离，如 5→(5, 8)、33→(33, 64)。

**练习 3**：如果去掉 `decode_admission_batch`，直接用物理桶 8 做准入上限，行为会怎么变？
**答案**：`--max-batch 5` 会变成实际可同时服务 8 个请求。这改变的是用户可见的并发与显存语义（KV 页、采样缓冲都按更多请求争用），而 #470 的意图是「用户要 5 就给 5」；物理上多分配的 3 个槽只用于把图形状对齐到桶。

### 4.2 槽位不变量：slot i ≡ 批位置 i 的指针稳定性契约

#### 4.2.1 概念说明

`BatchDecodeGraphState` 的文档第一段就是本模块的契约：**槽 i 永远映射到批中的位置 i**。请求一旦进入 decode 批占据槽 i，它的递推状态就住在 `slot_states[i]`，直到它离开这个批。某个桶录制图时访问的永远是 `slot_states[0..bucket_size]`，所以每次重放的 GPU 指针地址完全一致。

这条契约的三个支点：

1. **槽的地址终身不变**。启动期 `with_capacity` 为 `max_batch` 个槽各 `RecurrentState::new` 一次，之后从不释放、从不重分配。状态迁移（下一模块）一律「写入既有分配」。
2. **指针表内容也终身不变**。`LinearStatePointerTables` 在启动期把每个槽每层的状态/卷积地址填进设备端表；线性注意力内核按 `[row][layer]` 查表取地址。因为槽地址不变，这张表填一次就永远有效，表自己的设备地址也稳定——于是「查表」这个间接层本身可以被录进图。
3. **所有解码缓冲一次性按最大批分配**。`BatchDecodeBuffers35::new` 按 `max_batch_size` 分配 hidden/logits/元数据等全部缓冲；每步 `set_batch_size(bs)` 只改主机侧的 `seq_len` 标量（内核视角的行数），不碰分配。

**padding 行是契约的另一半。** 桶图必须看到 `padded_bs` 行全都「合法」。padding 行的四类输入分别是：token id 补 0、position 补 0、KV 元数据指向保留页 `padding_page_id`（seq_len=1）、递推指针指向一个真实存在但空闲的槽。输出侧只在采样时读前 `bs` 行，padding 行的 logits 是垃圾但被丢弃。

顺带一个呼应：`split_decode_splits` 每个桶固定 splits 数而不按 KV 长度推导，理由同样是「网格形状必须跨捕获图恒定」——桶纪律不止管缓冲，也管网格。

显存代价可以估出来。递推状态预留按每槽两份（`STATES_PER_DECODE_SLOT = 2`：一份图槽 + 一份 prefill 瞬态）：

\[ \text{bytes} = S \times 2 \times L_{\text{lin}} \times \big( H_v \cdot d_k \cdot d_v \cdot 4 + d_{qkv} \cdot (k-1) \cdot 2 \big) \]

其中 \(S\) 是物理桶槽数，\(L_{\text{lin}}\) 是线性注意力层数，\(H_v \cdot d_k \cdot d_v\) 是 f32 状态元素数，\(d_{qkv}(k-1)\) 是 bf16 卷积抽头数（公式来自 `per_layer_dims`，见 u3-l2）。

#### 4.2.2 核心流程

一次图解码步的完整时序：

```text
调度器:  把 active 请求按槽序（0..bs 密集）组批，取各请求的 KvView
batch_decode_graph_padded:
  1. 校验: bs>0, bs==views.len(), bs<=槽数, padded_bs>=bs 且是桶
  2. 指针表 validate_for(padded_bs)          # 容量与层数对上
  3. 逐请求位置契约: slot_states[i].seq_len == view.seq_len()-1
     （对齐后 seq_len += 1，主机侧推进）
  4. buffers.set_batch_size(padded_bs)       # 只改标量
  5. H2D: token_ids 补 0 到 padded_bs；positions 补 0
  6. H2D: sync_paged_views(views, padded_bs) # padding 行指向 padding_page_id
  7. bucket_idx = 桶表下标
  8. 按 DecodeGraphUse:
       Serve       → run_or_capture(闭包)   # 单 GPU：没录过就现录
       CaptureOnly → capture_only(闭包)     # TP 预捕获：只录不发
       Replay      → launch_captured()      # TP 服务：纯重放，缺图即报错
```

#### 4.2.3 源码精读

契约的权威表述就在结构体文档上：

[src/batch_decode_graph.rs:32-56](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode_graph.rs#L32-L56) —— 「Slot `i` always maps to position `i` in the batch…The CUDA Graph captured for a given bucket size always accesses `slot_states[0..bucket_size]`, so GPU pointer addresses are identical on every replay」。`graphs` 字段注释说明：每个 `BATCH_BUCKETS` 条目一张 `CudaGraphState`，按位置索引（注意：向量长度恒为 7，即使槽数只有 8，超出容量的桶只是永远不被捕获）。

启动期一次性建齐槽、指针表和图槽位：

[src/batch_decode_graph.rs:58-103](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode_graph.rs#L58-L103) —— `with_capacity` 分配 `max_batch` 个 `RecurrentState`，随后借用全部槽建 `LinearStatePointerTables::from_recurrent_refs`（一次填表），再为 7 个桶各建一个空 `CudaGraphState`。

指针表的结构与「填一次」的语义：

[src/recurrent_state.rs:44-51](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent_state.rs#L44-L51) —— 每个线性层两条设备向量：`state_ptrs` 与 `conv_state_ptrs`（元素是 u64 设备地址）。

[src/recurrent_state.rs:136-147](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent_state.rs#L136-L147) —— `from_recurrent_refs` = 按容量建表 + 立即 `refill_from_recurrent_refs` 填满。填表时逐槽取 `device_ptr_mut`（[src/recurrent_state.rs:177-191](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent_state.rs#L177-L191)），把地址 H2D 进表（[src/recurrent_state.rs:192-201](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent_state.rs#L192-L201)）。**因为槽地址终身不变，这张表此后再也不用重填**——这正是 u3-l2 讲过的「一次建表、逐 token 重放」。

每步解码前的容量/层数自检：

[src/recurrent_state.rs:206-226](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent_state.rs#L206-L226) —— `validate_for` 确认表容量 ≥ batch、层数与配置一致。

解码缓冲的一次性分配与「只改标量」：

[src/decode_buffers.rs:104-186](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/decode_buffers.rs#L104-L186) —— `BatchDecodeBuffers35::new` 按 `max_batch_size` 分配全部 hidden 流、注意力/线性缓冲、元数据与采样 scratch；`padding_page_id` 只是记下的一个 i32。

[src/decode_buffers.rs:188-210](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/decode_buffers.rs#L188-L210) —— `set_batch_size(bs)` 仅把各 `HiddenStates.seq_len` 设为 `bs`，一行分配都没有。

padding 行的 KV 元数据：

[src/decode_buffers.rs:212-267](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/decode_buffers.rs#L212-L267) —— `sync_paged_views` 先写真实请求的页表/末页长/序列长，再对 `real_bs..padded_bs` 的每个槽写：页索引 = `padding_page_id`、`last_page_len=1`、`chunk_size=1`（L237-243）。字段文档（L99-101）点明目的：让注意力内核「访问合法（但被丢弃的）内存」。

图解码入口的三段式校验与逐请求位置契约：

[src/batch_decode.rs:411-431](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L411-L431) —— `batch_decode_graph_padded` 断言 `bs>0`、`bs==views.len()`、`bs<=槽数`、`padded_bs>=bs` 且 `padded_bs` 是桶。

[src/batch_decode.rs:449-468](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L449-L468) —— 先 `validate_for(padded_bs)`，然后对每个真实行断言 `slot_states[i].seq_len == view.seq_len()-1`（KV 侧与递推侧的位置必须同步，u4-l2 讲过的位置契约），通过后主机侧 `seq_len += 1`。注意循环只跑 `0..bs`——padding 行没有请求，不做位置断言。

H2D 补零与取桶：

[src/batch_decode.rs:470-488](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L470-L488) —— `set_batch_size(padded_bs)`；`token_ids_padded.resize(padded_bs, 0)`、`positions.resize(padded_bs, 0)`；随后 `sync_paged_views`；最后 `bucket_idx = BATCH_BUCKETS.iter().position(...)`。

三种图使用模式（TP 纪律，预告 u7-l3）：

[src/batch_decode.rs:30-44](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L30-L44) —— `DecodeGraphUse` 的文档写得直白：TP 服务绝不懒捕获——一个 rank 在服务中途捕获、对端在重放，会让录进去的 NCCL 集合通信错位（desync）。

[src/batch_decode.rs:488-524](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L488-L524) —— `graphs` 先 `std::mem::take` 出来规避闭包里的 split-borrow，再按模式分派：`Serve → run_or_capture`（首次用某桶就现录）、`CaptureOnly → capture_only`（只录不发）、`Replay → launch_captured`（注释 L517-521：缺图说明预捕获扫描被跳过，**响亮失败，绝不在服务中捕获**）。

网格形状恒定的同族约束：

[src/decode_buffers.rs:31-44](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/decode_buffers.rs#L31-L44) —— `split_decode_splits(batch)`：桶 ≤2 用 32 个 split、否则 16，注释明说「固定 per bucket 而非按 KV 长度推导，因为网格形状必须跨捕获图恒定；过分裂的请求只是让后面的 CTA 空转」。

#### 4.2.4 代码实践

**实践目标**：亲手确认 padding 行的每一类输入都不会越界、也不会污染真实请求。

**操作步骤**：

1. 打开 [src/decode_buffers.rs:216-267](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/decode_buffers.rs#L216-L267)，为 padding 行整理四类输入的来源：token id、position、KV 页表/长度、递推指针表行。
2. 对每一类回答两个问题：(a) 它指向的内存是否存在且大小足够？(b) 它的计算结果会影响任何真实行吗？
3. 追一下 `padding_page_id` 的来源：[src/scheduler/backend.rs:245-249](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/scheduler/backend.rs#L245-L249) 里传入的是 `kv_cache.pool().padding_block_id()`——去 `pegainfer-kv-cache` 里找这个保留页是何时分配、为何永不分配给请求（源码阅读，不改代码）。
4. 对比 eager 回退路径：[src/batch_decode.rs:433-447](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L433-L447) 与 [src/batch_decode.rs:556-563](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L556-L563)——注意 eager 路径 `set_batch_size(bs)` 用的是**未取整**的 bs，解释为什么。

**需要观察的现象**：第 4 步会发现 eager 路径完全不 padding——因为 eager 每步重新发射内核，网格形状本来就随 bs 变化，不需要桶；padding 只为图重放而存在。

**预期结果**：四类输入的结论应分别是——token/position 补 0（合法 id，结果被丢弃）；KV 指向保留页且长度为 1（内核读的是真实分配的页，只是内容无意义）；递推指针指向空闲槽（内核会往里写垃圾，但没有请求认领该槽）；logits 的 padding 行在采样时只取前 `bs` 行（u4-l2 的 `select_tokens_*` 只按请求行读）。GPU 上的运行验证**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：如果每步都重新分配 `slot_states`（比如用 `Vec::resize` 换新缓冲），会发生什么？
**答案**：槽的设备地址改变，但图里录的还是旧地址。重放会把递推状态写进已释放/错误的内存，且大概率不报错——静默数据损坏。这就是「迁移原语一律写入既有分配」的原因。

**练习 2**：为什么指针表（`LinearStatePointerTables`）要放在 `BatchDecodeGraphState` 里，而不是每次解码临时构建？
**答案**：内核发射时读的是这张表的**设备地址**。表必须自己地址稳定才能被录进图；又因为槽地址不变，表内容填一次终身有效。临时构建意味着每次 H2D 一张新表，且表地址无法录图。

**练习 3**：`graphs` 向量有 7 个条目，但 `--max-batch 5` 时只有 4 张图会被真正捕获。哪 4 张？剩下 3 张什么时候用？
**答案**：槽数为 8，所以桶 1、2、4、8 会被捕获/调优/重放（TP 预捕获在 `bucket > slot_states.len()` 处 break，GEMM 调优同样过滤）。桶 16、32、64 的图槽位存在但永远是空 `CudaGraphState`，永不使用——准入上限 5 保证 active 不可能超过 8。

### 4.3 D2D 状态搬运：copy_state_to_slot / move_slot_within 与槽位压缩契约

#### 4.3.1 概念说明

槽不会自己长出状态。有两个时刻需要搬运：

**入槽（admission）**。prefill 期间，请求的递推状态活在自己的 prefill 瞬态里（调度器的 `alloc_recurrent` 或执行器的本地状态）。prefill 完成、晋升进 decode 批的那一刻，用 `copy_state_to_slot(src, slot_idx)` 把整份状态 D2D 拷进槽。此后**槽就是正本**，原瞬态作废。落槽的槽号就是当前密集行数：`slot_for_new_request(active_count, max_batch) = active_count`（前提 `active_count < max_batch`）。

**搬槽（compaction）**。decode 批要求行 `0..bs` 密集（图按行号访问槽）。请求中途退休会在中间留洞，于是把**最后一个占用槽**搬进洞里：`compaction_after_retire(len, idx) = { moved_from: len-1, moved_to: idx }`（退休的恰是最后一行则无需搬运）。搬完把对应请求的 `graph_slot_idx` 改成新槽号。

**「from 槽留旧字节」为何良性。** `move_slot_within` 做的是拷贝不是交换——from 槽还留着旧状态的字节。这没有危害，因为一个槽的字节只有两种途径被读：被某个请求认领（那它一定先经过 `copy_state_to_slot` 整体覆写），或作为 padding 行被内核写垃圾（输出列被丢弃）。**没有请求认领的槽，它的旧字节永远无人读。** 源码注释把这称为「both benign by design」。

两个原语的分工要说清：

- `copy_state_to_slot`：任意源（prefill 瞬态）→ 槽，整份拷贝，`seq_len` 一并复制。单 GPU 与 TP 都用它入槽。
- `move_slot_within`：槽 → 槽，**TP 压缩路径专用**（文档原话 "Used by TP slot compaction"），显式拒绝 `from == to` 与越界，用 `split_at_mut` 解决同向量两处的 `&mut` 借用冲突。单 GPU 调度后端没有调用它——`SingleGpuBackend::compact_slot` 内联做了同样语义的 D2D 循环；低层执行器 `Qwen35Executor::compact_slot` 也有自己的一份。三处实现语义一致：拷 state、拷 conv_state、搬 `seq_len`、from 留旧字节。

#### 4.3.2 核心流程

```text
入槽（每个请求一次，prefill 晋升时）:
  slot_idx = slot_for_new_request(active.len(), admission)   # = active.len()
  copy_state_to_slot(ctx, prefill 瞬态 rec, slot_idx)         # D2D 整份 + seq_len
  active.push(... graph_slot_idx: slot_idx)

退休（每个请求一次，decode 结束/abort 时）:
  idx = 该请求在 active 中的下标
  compaction = compaction_after_retire(active.len(), idx)
  active.swap_remove(idx)                    # 最后一行落到 idx
  若 compaction = Some{from, to}:
      单 GPU: backend.compact_slot 内联 D2D  slot[from] → slot[to]，并把该请求的
              graph_slot_idx 从 from 改成 to
      TP:     worker.drop_request → slot_compact(slot_map) + graph_state.move_slot_within(from, to)
  下一请求准入: slot = active.len()（复用刚腾出的密集槽）
```

#### 4.3.3 源码精读

两个 D2D 原语本体：

[src/batch_decode_graph.rs:105-119](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode_graph.rs#L105-L119) —— `copy_state_to_slot` 的文档写明契约：「调用一次即请求入批（prefill 结束后）；此后 `slot_states[slot_idx]` 就是正本，原 `src` 不再使用」。实现委托给 `RecurrentState::copy_from`。

[src/recurrent_state.rs:89-106](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent_state.rs#L89-L106) —— `copy_from` 逐层 `memcpy_dtod` state 与 conv_state，最后 `self.seq_len = src.seq_len`——主机侧的推进位置也一起搬。

[src/batch_decode_graph.rs:121-167](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode_graph.rs#L121-L167) —— `move_slot_within`。文档（L121-126）说明它服务 TP 槽压缩，且点破本讲的核心问答：「Slot `from` keeps stale bytes afterwards; admission overwrites it (`copy_state_to_slot`), and padding rows may clobber it — both benign by design」。守卫（L133-141）拒绝 no-op 搬运与越界；L142-148 用 `split_at_mut(hi)` 把同一切成左右两段以同时拿到 `&` 源与 `&mut` 目的；L155-162 逐层 D2D；L163-166 把 `seq_len` 跟过去。

压缩的纯函数规划层（可单测的决策与执行分离）：

[src/scheduler/plan.rs:41-45](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/scheduler/plan.rs#L41-L45) —— `SlotCompaction { moved_from, moved_to }`。

[src/scheduler/plan.rs:202-204](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/scheduler/plan.rs#L202-L204) —— `slot_for_new_request(active_count, max_batch)`：还有余量就返回 `active_count`，即「下一个密集槽」。

[src/scheduler/plan.rs:249-263](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/scheduler/plan.rs#L249-L263) —— `compaction_after_retire`：退休下标不是最后一行时返回 `{from: len-1, to: idx}`，否则 `None`。

单 GPU 的落槽与压缩：

[src/scheduler/steps.rs:622-638](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/scheduler/steps.rs#L622-L638) —— `promote_prefill_state`：`slot_for_new_request(active_len, max_batch())` 拿槽号，`copy_recurrent_to_slot` 入槽，`graph_slot_idx` 记进后端状态。

[src/scheduler/backend.rs:546-553](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/scheduler/backend.rs#L546-L553) —— `copy_recurrent_to_slot` 是 `copy_state_to_slot` 的后端薄封装。

[src/scheduler/steps.rs:395-412](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/scheduler/steps.rs#L395-L412) —— `compact_single_slot`：`swap_remove` 后按 `compaction_after_retire` 的结果调 `backend.compact_slot`。注释说清了 swap 语义：退休后原最后一行的元素落到了 `idx`。

[src/scheduler/backend.rs:555-602](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/scheduler/backend.rs#L555-L602) —— `SingleGpuBackend::compact_slot`：先从请求的 `graph_slot_idx` 反查并 `debug_assert` 它等于 `moved_from`（调度器与后端对槽位的认知必须一致），然后**内联**做逐层 D2D（同样用 `split_at_mut`），搬 `seq_len`，最后把该请求的 `graph_slot_idx` 改写为 `moved_to`。注意它没有走 `move_slot_within`——两条路径各自维护一份等价实现。

TP 的压缩与认领：

[src/tp_executor/worker.rs:1092-1128](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/tp_executor/worker.rs#L1092-L1128) —— `drop_request`：图模式下带压缩命令时先 `slot_compact`（更新 worker 的槽位归属表），需要搬时调 `graph_state.move_slot_within(from, to)`。文档强调：调度器声明与 worker 槽位表不一致即「分歧」，直接失败并毒化执行器。

[src/tp_executor/worker.rs:1131-1149](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/tp_executor/worker.rs#L1131-L1149) —— `slot_admit`：入槽时槽必须空闲——「退休与压缩保持归属表密集，此处遇占用即调度器分歧」。

低层执行器（测试/基准路径）的同款逻辑：

[src/executor.rs:236-253](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/executor.rs#L236-L253) —— prefill 完成后 `slot_idx = active.len()`，`copy_state_to_slot` 入槽。

[src/executor.rs:354-399](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/executor.rs#L354-L399) —— `Qwen35Executor::compact_slot`：`swap_remove` 后若 `idx < active.len()`，先断言换过来的请求 `graph_slot_idx == last`（密集不变量的运行时检查），再逐层 D2D 并改写 `graph_slot_idx`。

最后是 TP 预捕获扫描对「显式传桶」的复用——它证明 padding 机制足以只用一行真实数据捕获任意桶：

[src/tp_executor/worker.rs:1054-1090](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/tp_executor/worker.rs#L1054-L1090) —— `precapture_bucket`：一条合成行（token 0、位置 0、一页 KV 视图）加显式 `bucket` 参数调用 `batch_decode_graph_padded`，其余行全部走 padding 路径。注释：「这个扫描无论池多大、桶多大，一次只占一页 KV」。

#### 4.3.4 代码实践

**实践目标**：用纯函数单测验证压缩规划，并推演一次真实的退休-搬运。

**操作步骤**：

1. 阅读 [src/scheduler/plan.rs:969-1001](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/scheduler/plan.rs#L969-L1001) 的测试 `graph_slot_assignment_stays_dense_after_retirement`，注意三条断言各自的语义：中间退休、首行退休、末行退休。
2. （可选，需 CUDA + Python/Triton 构建工具链）运行它：

   ```bash
   cargo test --release -p pegainfer-qwen35 --features qwen35 --lib graph_slot_assignment
   ```

   无工具链时**待本地验证**，但断言结果可直接从纯函数推导。
3. 纸面推演：active = [A, B, C, D, E]（分别占槽 0..4），C（idx=2）达到 max_tokens 退休。写出 `compaction_after_retire(5, 2)`、swap_remove 后 active 的顺序、哪个槽被 D2D 到哪个槽、E 的 `graph_slot_idx` 从几改成几、下一步准入的新请求拿几号槽、下一步重放哪个桶。

**需要观察的现象**：步骤 3 中 E 在 `active` 数组里的位置始终是 4（它就是最后一行），变化的是它的**槽号**从 4 变成 2——「数组下标」与「图槽号」在 swap_remove 之后短暂不一致，`compact_slot` 的职责就是把它们重新对齐。

**预期结果**：`compaction_after_retire(5,2) = Some{moved_from:4, moved_to:2}`；swap_remove 后 active = [A, B, E, D]（E 落到 idx=2）；槽 4 → 槽 2 D2D；E 的 `graph_slot_idx` 4 → 2；新请求入槽 `slot_for_new_request(4, 5) = 4`；active=4 → 重放桶 4。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `move_slot_within` 显式拒绝 `from == to`？
**答案**：`from == to` 说明调度器把 no-op 当成了压缩——这本身就是一个分歧信号（要么规划错了要么退休的不是以为的那一行），静默跳过会把 bug 藏进后续每一步。fail-closed 让它在第一时间暴露。

**练习 2**：`compact_slot`（单 GPU）为什么先 `debug_assert_eq!(src_slot, compaction.moved_from)` 再拷贝？
**答案**：压缩的正确性依赖「调度器认为请求在槽 X」与「后端记录的 `graph_slot_idx`」一致。不一致意味着两边的槽位视图已经分叉，此时拷贝会搬错状态。断言把这个前提变成运行时检查（TP 侧对应做法是失败并毒化执行器，见 `drop_request` 的文档）。

**练习 3**：`copy_state_to_slot` 之后原 prefill 瞬态为什么必须作废、而不是留着当前缀缓存快照？
**答案**：若两个「正本」并存，后续 decode 步只推进槽里的那份，瞬态会静默过期；任何读它的代码都在读旧位置。前缀缓存快照走的是另一条路——prefill 结束时把槽/池状态拷进**只读快照槽**（u6 的 `RecurrentStateStore`），与活槽严格分离。

## 5. 综合实践

把本讲三个模块串起来：**5 个并发请求的完整生命周期推演**。设定 `--max-batch 5`、无前缀缓存、单 GPU 图模式。

逐题作答（先自己填，再对照参考答案）：

| 问题 | 你的答案 |
|------|----------|
| (a) 加载期物理分配哪个桶？槽数、递推预留份数？ | |
| (b) active = 1..5 时每步分别重放哪个桶？各有多少 padding 行？ | |
| (c) padding 行的四类输入分别是什么？ | |
| (d) 请求 C（第 3 个，idx=2）先结束：压缩怎么走？之后每步重放哪个桶？ | |
| (e) `move_slot_within`/`compact_slot` 之后 from 槽的旧字节为什么无害？ | |

**参考答案**：

- **(a)** `bucket_for(5) = 8`：8 个 `slot_states`、8 行解码缓冲，图只可能用桶 1/2/4/8；准入上限仍是 5（[src/weights.rs:141-156](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L141-L156)）。递推状态预留按 `STATES_PER_DECODE_SLOT = 2`（[src/weights.rs:96-97](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L96-L97)）乘槽数 8 —— 每槽一份图槽状态加一份 prefill 瞬态。
- **(b)** active=1→桶 1（0 padding）；2→桶 2（0）；3→桶 4（1）；4→桶 4（0）；5→桶 8（3）。取整来自 `bucket_for`，padding 数 = 桶 − active。
- **(c)** token id 与 position 补 0（[src/batch_decode.rs:472-481](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L472-L481)）；KV 页表指向 `padding_page_id`、`last_page_len=1`、`chunk_size=1`（[src/decode_buffers.rs:237-243](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/decode_buffers.rs#L237-L243)）；递推指针表第 i 行指向真实存在的空闲槽 i（内核写入垃圾但无人认领）；logits 的 padding 行在采样时被丢弃（只读前 bs 行）。
- **(d)** `compaction_after_retire(5, 2) = {moved_from: 4, moved_to: 2}`：槽 4 的状态 D2D 到槽 2（单 GPU 走 [src/scheduler/backend.rs:555-602](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/scheduler/backend.rs#L555-L602) 的内联实现），E 的 `graph_slot_idx` 改为 2。active=4 → 此后每步重放桶 4；若再有请求进来（active=5），新请求入槽 4，重回桶 8。
- **(e)** 槽的字节只有两条被读的路径：被请求认领（必先经 `copy_state_to_slot` 整体覆写）或作为 padding 行被内核写垃圾（输出被丢弃）。无人认领的槽里没有读者，旧字节因此无害——[src/batch_decode_graph.rs:121-126](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode_graph.rs#L121-L126) 的注释原话是「both benign by design」。

## 6. 本讲小结

- **桶取整**：`BATCH_BUCKETS = [1,2,4,8,16,32,64]`，`bucket_for` 向上吸附；`--max-batch 5` 分配物理桶 8 但准入仍限 5（`decode_admission_batch` vs `reserved_decode_slots`，#470）。
- **槽位不变量**：槽 i ≡ 批位置 i，槽地址终身不变；`LinearStatePointerTables` 一次建表终身有效；解码缓冲一次性分配、每步只改 `seq_len` 标量。
- **padding 行**：token/position 补 0、KV 指向保留页（seq_len=1）、递推指向空闲槽、输出列丢弃——让桶图看到的全是合法地址。
- **D2D 搬运**：入槽用 `copy_state_to_slot`（prefill 瞬态 → 槽，槽成为正本）；退休压缩把最后一行搬进洞（`compaction_after_retire` 规划，单 GPU 内联 / TP 走 `move_slot_within`），行保持 0..bs 密集。
- **良性旧字节**：无人认领的槽没有读者；认领必先整体覆写，padding 只写不读。
- **图使用纪律**：单 GPU `Serve` 可懒捕获，TP 只允许启动预捕获 + `Replay`——服务中途单侧捕获会让录进去的 NCCL 集合通信错位。

## 7. 下一步学习建议

下一讲 **u4-l4（unified_step 与 SharedSM 重叠）** 会用到本讲的桶概念：`MAX_SHARED_SM_DECODE_BATCH`（[src/lib.rs:44-51](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/lib.rs#L44-L51)）把重叠模式限制在小桶，原因是大桶 decode 没有独立的 per-stream cuBLAS 路由——先弄懂「每个桶一张图」再读那段注释会非常顺。

之后建议：

- 读 `pegainfer-core` 的 `CudaGraphState`（`run_or_capture` / `capture_only` / `launch_captured` 的差异），补上本 crate 之外的录制细节。
- 进阶单元 7 的 **u7-l3**：TP 四阶段预捕获扫描（Warmup/Capture/Launch/Finalize）如何全局排序避免驱动死锁——本讲的 `precapture_bucket` 是它的单桶切片。
- 性能侧阅读 `docs/models/qwen35/decode-kernel-attribution.md`，理解桶大小与 split-KV/FlashInfer 路由的实测关系（[src/decode_buffers.rs:12-44](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/decode_buffers.rs#L12-L44) 的注释就是它的摘要）。
