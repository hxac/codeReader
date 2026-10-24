# ops/attention.rs 与 ops/paged_plan.rs：分页注意力的调度面

> 前置讲义：u2-l3（ops.rs 算子门面总览）。本讲是 u2 单元的收官：ops.rs 里再出口之外的「自研部分」——`attention` 与 `paged_plan` 两个子模块。

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `ops/attention.rs` 三个入口（`prefill_attention_paged_into`、`paged_attention_batch_decode_into`、`paged_attention_batch_decode_split_kv_into`）各自服务的执行形态，以及它们传给 kernel 的参数流。
2. 解释 paged attention 中「计划（plan）」与「KV 数据」分离的原因：为什么 kernel 不直接吃 `KvState`，而只认设备侧的扁平 `i32` 数组。
3. 读懂 `ops/paged_plan.rs` 的两个数据结构：`PrefillPagedPlan`（prefill 的分页计划，包装 kernels 侧同名类型）与 `SplitKvCsr`（split-KV decode 的宿主侧 CSR），并能手工推演 `build_split_kv_csr` 的输出。
4. 对照 `kv_pool.rs` 的 `KvDesc<'a>`，理解「core 描述状态、kernels 消费状态」这条契约，说清 `desc()` 与 `desc_for_len()` 的差异。

## 2. 前置知识

### 2.1 从注意力公式到「为什么要分页」

Transformer 的自注意力计算：

\[ \mathrm{Attention}(Q,K,V) = \mathrm{softmax}\!\left(\frac{QK^\top}{\sqrt{d}}\right)V \]

推理时为避免每生成一个 token 都重算全部历史，会把历史的 K、V 缓存下来，这就是 **KV cache**。朴素的实现是为每个请求开一块「长度 = 最大上下文」的连续显存。问题在于：请求长度参差不齐，预留太大浪费显存，预留太小放不下长请求——本质是**显存的内部碎片与外部碎片**问题。

解法和操作系统的虚拟内存一样：把 KV cache 切成固定大小的**页（page）**，逻辑上连续的序列在物理上由一串离散页拼成。请求需要更多空间时再分配一页，请求结束时页归还池子复用。于是：

- **KV 数据**：一整块池化显存 `kv_buffer`，按 page-first 布局组织（u3-l2 会精读 `KvPool`）。
- **页表**：每个请求持有自己的页编号列表（`KvDesc` 里的 `pages`），就像进程的页表。

### 2.2 prefill 与 decode

- **prefill（预填充）**：一次处理整段 prompt，请求侧有多个 token（多个 query 行），注意力要对「已有 KV + 本段新写入的 KV」做因果计算。
- **decode（解码）**：每个请求每步只生成 1 个 token，query 只有 1 行，但要扫过该请求的全部历史 KV。把多个请求的这「1 行 query」合在一起跑，就叫 **batch decode**。

decode 的一个经典痛点：批量小、上下文长时，单个 kernel 的并行块（CUDA 里的 CTA）数量不足，喂不饱 GPU 的流处理器。**split-KV** 的思路是把每个请求的长 KV 切成若干 chunk，让多个并行块各算一段的部分结果，最后再合并——用更多的并行度换取吞吐。这也是 `SplitKvCsr` 名字的由来。

### 2.3 CSR（压缩稀疏行）格式

CSR 是一种用「值数组 + 行偏移数组」表达稀疏结构的紧凑格式。本讲的 CSR 长这样：

- `request_indices[i]`：第 `i` 个槽位属于哪个请求；
- `kv_tile_indices[i]`：第 `i` 个槽位是该请求的第几个 KV chunk；
- `block_valid_mask[i]`：该槽位是真数据（1）还是补齐用的填充（0）；
- `o_indptr[r]`：请求 `r` 的槽位区间是 `[o_indptr[r], o_indptr[r+1])`。

`indptr`（index pointer）就是这个「每行从哪开始」的前缀和数组，长度 = 行数 + 1。

### 2.4 「计划」与「数据」分离（承接 u2-l3）

u2-l3 已经确立：`ops.rs` 是 core 在再出口之外的唯一自研重镇，其中 `attention` 与 `paged_plan` 是「core 描述状态、kernels 消费状态」契约的两个端点：

- **core 侧**：拥有请求的生命周期状态（`KvState`/`KvDesc`，kv_pool.rs），并负责把这些宿主侧的 Rust 结构**翻译**成 kernel 能吃的设备侧数组（plan/CSR）。
- **kernels 侧**：只认扁平的 `CudaSlice<i32>` 页表与几何参数（`PagedKvLayout`），不认识任何 Rust 请求对象。

为什么要分离？本讲会从源码里给出四个理由（见 4.1.1）。

### 2.5 术语表

| 术语 | 含义 |
|------|------|
| page / page_size | KV cache 的固定分页单元；一页容纳 `page_size` 个 token 的全部层、全部 KV 头 |
| last_page_len | 最后一页里的有效 token 数（1..=page_size；0 表示空请求） |
| 页表（page indices） | 一个请求持有的页编号序列，如 `[7, 3, 11]` 表示逻辑第 0 页放在物理页 7 |
| CSR / indptr | 压缩稀疏行格式；`indptr` 为每行的起始偏移前缀和 |
| H2D / D2H | host（CPU 内存）到 device（显存）/ 反方向的拷贝 |
| CTA tile | kernel 内一个线程块负责的 tile 粒度（`cta_tile_q` 是 query 侧 tile 的覆盖参数） |
| newtype + Deref | 用一个只含一个字段的结构体包装另一类型，并实现 `Deref` 让引用可以自动强制转换到内层类型 |

## 3. 本讲源码地图

| 文件 | 行数 | 作用 |
|------|------|------|
| [pegainfer-core/src/ops.rs](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops.rs#L1-L116) | 116 | 算子门面：声明私有子模块 `attention`/`paged_plan`，并把本讲的 6 个名字再出口给模型 crate |
| [pegainfer-core/src/ops/attention.rs](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops/attention.rs#L1-L147) | 147 | 三个注意力入口：转发到 kernels 同名函数，唯一的加工是把 `KvLayout` 转成 kernel 认的 `PagedKvLayout` |
| [pegainfer-core/src/ops/paged_plan.rs](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops/paged_plan.rs#L1-L247) | 247 | 分页计划：`SplitKvCsr` + `build_split_kv_csr`（纯宿主侧计算），`PrefillPagedPlan`（kernels 类型的 newtype 包装 + 四条构造路径） |
| [pegainfer-core/src/kv_pool.rs](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/kv_pool.rs#L1-L564) | 564 | KV 池与请求状态（u3-l2 的主角）；本讲只取 `KvDesc<'a>` 与 `desc()`/`desc_for_len()`、`KvLayout::kernel_layout()` |

消费方（用于验证参数流，属模型 crate，只引用不精读）：

- [pegainfer-qwen3/src/prefill.rs](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/prefill.rs#L296-L296)：调 `ops::prefill_attention_paged_into`，用 `PrefillPagedPlan::from_raw_batch_with_cta_tile_q` 建计划。
- [pegainfer-qwen3/src/batch_decode_buffers.rs](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/batch_decode_buffers.rs#L436-L449)：调 `build_split_kv_csr` 并 `memcpy_htod` 上传 CSR。
- [pegainfer-qwen3/src/unified_forward.rs](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/unified_forward.rs#L599-L599)：经 core 门面调 `paged_attention_batch_decode_split_kv_into`。
- [pegainfer-gemma4/src/serve.rs](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-gemma4/src/serve.rs#L1592-L1617)：`desc_for_len` → `PrefillPagedPlan::new` 的完整生产链样本。

## 4. 核心概念与源码讲解

### 4.1 模块一：paged attention 三入口（ops/attention.rs）

#### 4.1.1 概念说明

`attention.rs` 只做一件事：为三种执行形态各提供一个**薄封装入口**，把 core 侧类型（`KvLayout`、core 的 `PrefillPagedPlan`）适配成 kernels 侧类型后原样转发。三个入口对应三种形态：

| 入口 | 形态 | query 形状 | 计划载体 |
|------|------|-----------|---------|
| `prefill_attention_paged_into` | prefill（可整段多 token） | 每请求 `seq_len` 行 | `PrefillPagedPlan` |
| `paged_attention_batch_decode_into` | batch decode（不切 KV） | 每请求 1 行 | 7 个独立的 `CudaSlice<i32>` 设备缓冲 |
| `paged_attention_batch_decode_split_kv_into` | batch decode（KV 切 chunk） | 每请求 1 行 | 上述 7 个缓冲 + split 系列 CSR 缓冲 + 两个临时缓冲 |

「计划与数据分离」的原因，从源码结构就能读出四条：

1. **FFI 边界只认扁平数组**：kernel 是 CUDA 代码，看不到 Rust 的 `KvState`、借用、泛型；页表必须先变成设备侧 `i32` 数组（`CudaSlice<i32>`）才能被 kernel 索引。
2. **同一份 KV 数据支持多种读法**：`kv_buffer` 是池里唯一的一块大显存，prefill 计划、decode 页表、split-KV CSR 都只是对它的不同「索引方式」，数据本身不用动。
3. **状态与调度解耦**：`KvState` 活在请求的整个生命周期，计划则随每步的批次形态重建（或原地复用）——两者的变化频率完全不同。
4. **图稳定复用**：计划可以预分配到最坏尺寸后原地重填（见 4.2），指针不变是 CUDA Graph 回放的前提（呼应 u1-l1 引入的 CUDA Graph 概念，u4-l1 精读）。

#### 4.1.2 核心流程

**prefill 路径**的参数流：

```text
KvState（请求状态，持页表）
   │  desc() / desc_for_len()            —— kv_pool.rs
   ▼
KvDesc<'a> { pages, last_page_len }       —— 宿主侧最小充分信息
   │  PrefillPagedPlan::new / from_raw_batch_with_cta_tile_q
   │    （页表 i32 化 + H2D 上传 + 算 q tile 划分）
   ▼
PrefillPagedPlan（设备侧页表 + indptr + tile 索引）
   │
   ▼
ops::prefill_attention_paged_into(ctx, q,k,v, …, kv_buffer, layout, layer, plan, …)
   │  layout.kernel_layout() 把 core 几何转成 kernels 的 PagedKvLayout
   ▼
pegainfer_kernels::ops::prefill_attention_paged_into(...)   —— 真正启动 kernel
```

**batch decode 路径**的参数流（以 qwen3 为例）：

```text
每个请求的 KvState
   │  extend_page_indices_i32() 把各请求页号按批顺序追加进一个行缓冲
   ▼
宿主侧 Vec<i32> 页表 + 每请求 last_page_len / position
   │  上传到 BatchDecodeBuffers 自有的 7 个设备缓冲
   │  （page_indices_d / page_indptr_d / last_page_len_d /
   │    positions_d / request_indices_d / kv_tile_indices_d / kv_chunk_size_d）
   ▼
paged_attention_batch_decode[_split_kv]_into(...)
```

**split-KV** 额外多两步：先用 `build_split_kv_csr` 在宿主侧把每个请求的 `kv_len` 展开成「槽位 → (请求号, chunk 号)」的 CSR（详见 4.2），再连同 `split_o_indptr_d`、`split_block_valid_mask_d` 和两个临时缓冲 `split_tmp_v`（部分输出）、`split_tmp_s`（部分统计量）一起交给 kernel；kernel 先各 chunk 算部分 softmax 结果，再按 `o_indptr` 合并。合并的数学基础是 softmax 的在线重算（log-sum-exp 分解）：

\[ \mathrm{softmax}(x)_i = \frac{e^{x_i - m}}{\sum_j e^{x_j - m}}, \quad m = \max_j x_j \]

分块后每块各自记录局部最大值与局部和，第二阶段用 \( m = \max(m_1, m_2) \)、\( s = s_1 e^{m_1 - m} + s_2 e^{m_2 - m} \) 合并——这正是「部分输出 + 部分统计量」两个临时缓冲存在的理由。（chunk 内部的精确归约实现位于 kernels crate，本讲只依据缓冲命名与 CSR 结构讲到这一层。）

#### 4.1.3 源码精读

**（1）模块与再出口。** `attention` 与 `paged_plan` 都是**私有模块**（对比 `pub mod call_spec`），外部只能通过 ops.rs 的再出口使用：

[pegainfer-core/src/ops.rs:L3-L9](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops.rs#L3-L9) —— 声明私有子模块 `attention`（L3）与 `paged_plan`（L7），公开面完全收敛到下面的再出口；

[pegainfer-core/src/ops.rs:L11-L16](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops.rs#L11-L16) —— 本讲的全部 6 个公开名字：三个注意力函数 + `PrefillPagedPlan` + `SplitKvCsr` + `build_split_kv_csr`。

**（2）prefill 入口。**

[pegainfer-core/src/ops/attention.rs:L11-L50](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops/attention.rs#L11-L50) —— prefill 入口签名：注意它一次吃下归一化权重（`q_norm`/`k_norm`）、RoPE 表（`cos_cache`/`sin_cache`）、池缓冲（`kv_buffer`）、core 几何（`layout: &KvLayout`）与计划（`plan: &PrefillPagedPlan`）——即「norm + RoPE + 追加 KV + 分页注意力」在这个 kernel 里是一体的；

[pegainfer-core/src/ops/attention.rs:L31-L49](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops/attention.rs#L31-L49) —— 唯一的加工点：第 41 行把 `&layout.kernel_layout()` 传给 kernels（core 的 `KvLayout` → kernels 的 `PagedKvLayout`，见 4.3.3），`plan` 原样透传。

**（3）batch decode 入口。**

[pegainfer-core/src/ops/attention.rs:L52-L91](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops/attention.rs#L52-L91) —— 不带 split 的批量解码；本步的新写入 K/V（`k`/`v`）与历史 `kv_buffer` 一起参与计算；

[pegainfer-core/src/ops/attention.rs:L61-L67](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops/attention.rs#L61-L67) —— 7 个设备侧 `i32` 缓冲参数，逐一对应：扁平页表、页表行偏移（CSR 的 indptr）、每请求末页有效长度、每请求位置、槽位→请求映射、槽位→KV chunk 映射、chunk 大小。**这就是「计划」在 decode 形态下的真身：一组裸设备数组**。

**（4）split-KV 入口。**

[pegainfer-core/src/ops/attention.rs:L93-L146](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops/attention.rs#L93-L146) —— split-KV 批量解码，参数表在前一入口之上再叠加 split 系列；

[pegainfer-core/src/ops/attention.rs:L99-L115](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops/attention.rs#L99-L115) —— 新增参数：`row_offset`（本步 query 行在合并缓冲里的起点，供「prefill + decode 同一步」的混合批次定位自己的行）、5 个 split CSR 缓冲、部分输出/部分统计量两个**可变**临时缓冲（`&mut`，kernel 就地写）、以及补齐后的槽位总数 `split_padded_slots`。

**（5）消费点核对（模型 crate 侧）。**

[pegainfer-qwen3/src/prefill.rs:L296-L296](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/prefill.rs#L296-L296) —— qwen3 的 prefill 调 `ops::prefill_attention_paged_into`（经 core 门面）；计划在 [prefill.rs:L526](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/prefill.rs#L526-L526) 用 `from_raw_batch_with_cta_tile_q` 构建。

[pegainfer-qwen3/src/unified_forward.rs:L599-L599](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/unified_forward.rs#L599-L599) —— qwen3 的混合步经 core 门面调 `ops::paged_attention_batch_decode_split_kv_into`，`row_offset` 正是用来跳过同批 prefill 行的。

[pegainfer-qwen3/src/batch_decode_dag.rs:L295-L316](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/batch_decode_dag.rs#L295-L316) —— 一个诚实的旁证：qwen3 这条 decode 路径**绕过 core 封装直调** `pegainfer_kernels::ops::paged_attention_batch_decode_into`，自己补上 `&self.layout.kernel_layout()`。由于 core 封装本来就只是「转发 + 几何适配」，两条路进的是同一个 kernel；这提醒我们门面的价值在「统一类型出口」，而非增加执行逻辑。

#### 4.1.4 代码实践：跟踪一条真实调用链（源码阅读型）

1. **实践目标**：不写代码，把 qwen3 prefill 一步中「状态 → 计划 → kernel」的参数流亲手走一遍，验证 4.1.2 的流程图。
2. **操作步骤**：
   - 打开 [pegainfer-qwen3/src/prefill.rs:L285-L320](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/prefill.rs#L285-L320)，找到 `ops::prefill_attention_paged_into` 调用，抄下它实参里的 `plan` 变量名；
   - 向上回溯 `plan` 的来源（`PrefillPagedPlan::from_raw_batch_with_cta_tile_q`，[prefill.rs:L526](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/prefill.rs#L526-L526)），再回溯 `page_indices`/`last_page_lens` 两个入参各自从哪个请求状态方法来；
   - 最后列出 kernel 调用还额外需要、但不来自 `KvState` 的三类信息（提示：本段 query 的起点与长度、头几何、池缓冲与布局）。
3. **需要观察的现象**：`KvState` 只贡献「页号 + 末页长度」；位置、几何、缓冲都由调用方另行提供——`KvDesc` 恰好是最小充分信息。
4. **预期结果**：得到一张与本讲 4.1.2 流程图同构的调用链笔记（各环节文件:行号可点开对上）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `paged_attention_batch_decode_into` 的 7 个缓冲参数里没有 `KvState` 或 `KvDesc`？
**答案**：这些参数是 kernel 的输入，而 kernel 在 CUDA 侧只认扁平设备数组；`KvState`/`KvDesc` 是宿主侧 Rust 类型，必须在调用前翻译（页表 i32 化 + H2D 上传）成 `CudaSlice<i32>` 系列。这正是「core 描述状态、kernels 消费状态」的边界。

**练习 2**：`row_offset` 参数为什么只在 split-KV 版本出现，普通 batch decode 版本没有？
**答案**：split-KV 版本服务于「prefill 与 decode 合并在同一步」的统一前向（见 qwen3 `unified_forward.rs` 的用法）：一个批次里 prefill 行排前面、decode 行排后面，decode kernel 需要 `row_offset` 找到本步自己那 1 行 query 在合并缓冲中的位置。不带 split 的入口假定整个缓冲都属于 decode 批（或调用方已自行保证行对齐），故无需偏移。

**练习 3**：如果让你给三个入口各写一句「一句话职责」，怎么写？
**答案**：`prefill_attention_paged_into` = 整段 prompt 的「norm+RoPE+写 KV+因果注意力」一步到位；`paged_attention_batch_decode_into` = 多请求各 1 行 query 对全量历史 KV 的合批解码；`paged_attention_batch_decode_split_kv_into` = 同上，但把长 KV 切 chunk 提高并行度，代价是要额外归并部分结果。

### 4.2 模块二：PrefillPagedPlan 与 SplitKvCsr（ops/paged_plan.rs）

#### 4.2.1 概念说明

这个文件承载两类「翻译器」：

1. **`SplitKvCsr` + `build_split_kv_csr`**：纯宿主侧计算，输入只有 `kv_lens`（每请求的 KV 长度）、`chunk_size`、`cap`（每请求最多 chunk 数）与 `padded_bs`（补齐后的批大小），输出 CSR 四个数组。它**完全不碰 GPU**——GPU 相关的只有调用方随后自己做的 `memcpy_htod`。
2. **`PrefillPagedPlan`**：kernels 侧同名类型的 **newtype 包装**（`inner` 字段）。core 不复制 kernels 的计划逻辑，只包一层并提供「用 core 类型表达」的构造入口；包装上实现的 `Deref` 让 `&PrefillPagedPlan`（core 的）在传给 kernels 函数（形参是 `&PrefillPagedPlan`（kernels 的），见 [pegainfer-kernels/src/ops/attention.rs:L604-L622](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/src/ops/attention.rs#L604-L622) 的 `plan` 形参）时自动解引用强制转换——4.1.3 里「`plan` 原样透传」能编译正是靠它。

#### 4.2.2 核心流程

`build_split_kv_csr` 的算法：

```text
输入: chunk_size = C, cap = K, kv_lens[r] (r = 0..B-1), padded_bs = P (≥ B)
输出: 4 个数组，总槽位 padded_slots = P × K

for 每个请求 r:
    chunks(r) = max(1, ⌈kv_lens[r] / C⌉)          # 空请求也占 1 个 chunk
    校验 chunks(r) ≤ K，否则报错
    for c in 0..chunks(r):
        request_indices.push(r); kv_tile_indices.push(c); block_valid_mask.push(1)
    o_indptr.push(len(request_indices))            # 行偏移前缀和
for 空槽位 r in B..P:
    o_indptr.push(len(request_indices))            # 空请求区间长度为 0
把 request_indices / kv_tile_indices / block_valid_mask 补零到 padded_slots 长（mask=0）
```

用公式表达补齐不变量：

\[ \text{padded\_slots} = P \times K, \qquad \text{chunks}(r) = \max\!\left(1,\ \left\lceil \frac{\text{kv\_len}_r}{C} \right\rceil\right) \le K \]

`PrefillPagedPlan` 的四条构造路径，对应三种使用姿势：

| 入口 | 输入 | 场景 |
|------|------|------|
| `new`（→ 私有 `new_with_cta_tile_q`） | 单个 `&KvDesc` + 段几何 | 单请求一步（gemma4 在用） |
| `from_raw_batch_with_cta_tile_q` | 每请求一行的裸页表 `&[Vec<i32>]` + 各自 last_page_len / start_pos / seq_len | 批量 prefill（qwen3 在用） |
| `new_preallocated` | 最坏尺寸四元组（max_tokens/pages/batch/tiles） | 预分配，供图捕获 |
| `update_batch_with_cta_tile_q` | 同 `from_raw_batch` 的数据 | **原地重填、零分配、指针不变**，配合 `new_preallocated` 实现 graph-stable 复用 |

#### 4.2.3 源码精读

**（1）`SplitKvCsr` 结构。**

[pegainfer-core/src/ops/paged_plan.rs:L9-L16](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops/paged_plan.rs#L9-L16) —— 文档注释写明：split-KV decode kernel 的宿主侧 CSR，含「补齐后的请求/chunk 索引、每槽位有效位掩码、每请求 chunk 偏移」；四个 `Vec` 字段全部是宿主侧普通内存。

**（2）`build_split_kv_csr` 主循环。**

[pegainfer-core/src/ops/paged_plan.rs:L20-L32](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops/paged_plan.rs#L20-L32) —— 入参与三条前置校验（chunk_size/cap 必须 > 0、请求数不得超过补齐批大小）；

[pegainfer-core/src/ops/paged_plan.rs:L40-L53](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops/paged_plan.rs#L40-L53) —— 核心循环：第 41 行 `kv_len.div_ceil(chunk_size).max(1)` 计算 chunk 数（空请求也至少 1 块）；第 42-46 行超过 `cap` 直接报错（错误信息提示「context limit misconfigured」——这是配置期就该发现的问题，不是运行期能恢复的）；随后逐 chunk 压入三个平行的槽位数组并推进 `o_indptr`；

[pegainfer-core/src/ops/paged_plan.rs:L54-L61](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops/paged_plan.rs#L54-L61) —— 补齐段：空请求槽位贡献零 chunk（`o_indptr` 原地重复），剩余槽位填 `(0, 0, mask=0)`——**填充是构造性确定的**，kernel 靠 `block_valid_mask=0` 识别并跳过（这与 u1-l1 讲过的「padding 行是协议面」一脉相承）。

**（3）`PrefillPagedPlan` 包装。**

[pegainfer-core/src/ops/paged_plan.rs:L71-L73](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops/paged_plan.rs#L71-L73) —— newtype 声明，`inner` 即 kernels 的计划类型；

[pegainfer-core/src/ops/paged_plan.rs:L76-L95](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops/paged_plan.rs#L76-L95) —— `new`：**core 与 kv_pool 契约的交汇点**——形参 `desc: &KvDesc<'_>`，第 6 行的 `use crate::kv_pool::KvDesc` 让本文件直接消费 kv_pool 的状态描述；

[pegainfer-core/src/ops/paged_plan.rs:L108-L124](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops/paged_plan.rs#L108-L124) —— 翻译现场：把 `desc.page_indices()` 的 `PageId` 逐个映射成 `i32`（L108-L112），连同 `desc.last_page_len()`（L117）一起交给 kernels 的构造器——**`KvDesc` 的全部两个字段在此各就各位**；

[pegainfer-core/src/ops/paged_plan.rs:L129-L153](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops/paged_plan.rs#L129-L153) —— `from_raw_batch_with_cta_tile_q`：批量版，不吃 `KvDesc` 而吃「每请求一行页表」的裸数据（`page_indices: &[Vec<i32>]`）——当页表来自别处（如 qwen3 的 `pegainfer_kv_cache::KvView`）时走这条路；

[pegainfer-core/src/ops/paged_plan.rs:L155-L173](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops/paged_plan.rs#L155-L173) —— `new_preallocated`：按最坏尺寸预分配，文档注明供原地重填、图稳定缓冲复用（qwen3 的 `unified_forward.rs:203` 与 `verify_graph.rs:119` 都在用）；

[pegainfer-core/src/ops/paged_plan.rs:L175-L200](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops/paged_plan.rs#L175-L200) —— `update_batch_with_cta_tile_q`：**原地重填（no allocation, pointers unchanged）**——CUDA Graph 捕获的是固定地址，每步变化的内容必须写进预分配的同一块缓冲（qwen3 `unified_forward.rs:369` 每步调它）；

[pegainfer-core/src/ops/paged_plan.rs:L202-L237](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops/paged_plan.rs#L202-L237) —— 一组只读访问器，逐个暴露设备缓冲（`page_indices_d`、`page_indptr_d`、`last_page_len_d`、`q_indptr_d`、`request_indices_d`、`qo_tile_indices_d`、`kv_tile_indices_d`、`kv_chunk_size_d`、`total_num_rows_d`）与标量（`batch_size`、`num_tiles`）——**decode 路径需要的页表缓冲正来自这里**（或模型 crate 自建的等价缓冲）；

[pegainfer-core/src/ops/paged_plan.rs:L240-L246](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ops/paged_plan.rs#L240-L246) —— `Deref` 实现，让这个包装能直接传给 kernels 的函数（见 4.2.1）。

**（4）CSR 上传的消费点。**

[pegainfer-qwen3/src/batch_decode_buffers.rs:L436-L449](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/batch_decode_buffers.rs#L436-L449) —— 生产样本：`build_split_kv_csr(...)` 之后，五个数组（含单元素 `split_kv_chunk_size`）逐个 `memcpy_htod` 进预分配设备缓冲，最后记下 `split_padded_slots = padded_bs * cap`——正好是 4.1.3 里那个形参的实参来源；

[pegainfer-qwen3/src/batch_decode_buffers.rs:L454-L466](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/batch_decode_buffers.rs#L454-L466) —— 什么时候用 split-KV：小批量走 SplitKv（用 chunk 并行度补 SM），大批量走 NonPartition——调度决策在模型 crate，core 只提供两种弹药。

#### 4.2.4 代码实践：手工推演 build_split_kv_csr（纯 CPU，可直接验证）

qwen3 的 [batch_decode_buffers.rs:L526-L549](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/batch_decode_buffers.rs#L526-L549) 已经为这个函数准备了三个现成单测。本实践分两段：

1. **实践目标**：不依赖 GPU，确认你真的会算 CSR。
2. **操作步骤**：
   - 先不看断言，自己笔算：`build_split_kv_csr(64, 4, &[40, 100], 3)`（chunk_size=64, cap=4, 两个请求 kv_len 为 40 和 100，补齐到 3 槽）的四个输出数组；
   - 再对照同文件 `padding_requests_contribute_zero_chunks` 测试（L539）的断言核对：`o_indptr == [0, 1, 3, 3]`、`request_indices[..3] == [0, 1, 1]`、`kv_tile_indices[..3] == [0, 0, 1]`、总长度 `== 3 * 4`；
   - 最后推演 `errors_when_a_request_exceeds_the_cap`（L548）：`build_split_kv_csr(64, 4, &[1000], 1)` 为何必须报错（⌈1000/64⌉ = 16 > 4）。
3. **需要观察的现象**：请求 0（kv_len=40 < 64）仍占 1 个 chunk；请求 1（kv_len=100）占 2 个 chunk；第 3 个槽位是纯填充，`o_indptr` 最后两值相同表示零 chunk；`request_indices` 数组长度被补齐到 `padded_bs × cap = 12`，填充槽 mask=0。
4. **预期结果**：手算结果与断言逐项一致。若想机器验证，可运行 `cargo test --release -p pegainfer-qwen3 --lib batch_decode_buffers`（该函数本身是纯 CPU 逻辑，但编译 qwen3 需要先经 kernels 的 build.rs 跑 nvcc，环境开销较大）——**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`build_split_kv_csr` 为什么把槽位总数固定为 `padded_bs * cap`，而不是「实际 chunk 数」？
**答案**：固定尺寸让设备缓冲可以一次性预分配到最坏情况并反复复用（同批大小/上限内指针稳定），这与 CUDA Graph 的地址稳定性要求、以及「每步集合形状固定」的纪律一致；代价是空槽，用 `block_valid_mask=0` 显式标记，kernel 构造性跳过。

**练习 2**：`PrefillPagedPlan` 为什么是 newtype + Deref，而不是 `pub use` 直接再出口 kernels 的类型？
**答案**：因为 core 要在这层提供**以 core 类型为入参**的构造入口（`new` 吃 `KvDesc`），这是 kernels 侧没有的 API；同时 newtype 隐藏 kernels 类型细节，Deref 又保留了「传给 kernels 函数」的零成本互通。`tensor.rs`/`ffi.rs` 用整行再出口是因为它们**没有**要加的 API，`ops.rs` 的 `PrefillPagedPlan` 用包装是因为**有**——门面形式的差异反映职责差异。

**练习 3**：`new_with_cta_tile_q` 里的 `cta_tile_q_override` 传 0 意味着什么？
**答案**：0 表示不覆盖、让 kernels 侧用自己的默认 tile 划分（具体默认值在 kernels 的实现里，本讲不展开）；非 0 值用于调用方要强行指定 query 侧 CTA tile 粒度的场景，例如 qwen3 用常量 `PREFILL_ATTENTION_CTA_TILE_Q` 对齐它的统一前向缓冲布局。

### 4.3 模块三：KvDesc 对拍——core 描述状态、kernels 消费状态

#### 4.3.1 概念说明

`KvDesc<'a>` 是 kv_pool.rs 定义、paged_plan.rs 消费的**最小充分信息**载体：要让一个 kernel 正确读到某请求的 KV，最少需要知道「这个请求的页号序列」和「最后一页有多少有效 token」。多了浪费，少了读错（要么越界读别人的页，要么把末页的垃圾当有效数据）。

它和 plan/CSR 的分工是**视角差异**：

- `KvDesc`：**单请求视角**、宿主侧、借用（`'a` 借的是 `KvState` 里页表的生存期）——描述「状态是什么」；
- `PrefillPagedPlan` / `SplitKvCsr`：**批次视角**、（前者）设备侧/（后者）宿主侧——描述「这一步怎么调度」。

`desc()` 与 `desc_for_len()` 的差异是本模块的关键考点：前者「照实汇报」（用 `KvState` 当前 `seq_len` 推末页长度，不校验页数）；后者「按假想长度汇报」（用调用方给的 `seq_len`，并**强校验**持有页数恰好等于 `⌈seq_len / page_size⌉`）——用于「状态还没推进、但要用推进后的长度做计划」的场景（如 gemma4 在写 KV 前就要构造本步计划）。

#### 4.3.2 核心流程

生产链（已由 gemma4 的源码证实）：

```text
KvState（持 permit 页表 + seq_len）
   │ desc_for_len(start_pos + seq_len)     ← 用「本步结束时的长度」
   ▼
KvDesc { pages, last_page_len }
   │ PrefillPagedPlan::new(ctx, &desc, start_pos, seq_len, 头几何...)
   │    ├─ PageId[] → Vec<i32>（i32 化）
   │    └─ 交 kernels 构造器：上传设备 + 算 tile
   ▼
PrefillPagedPlan（设备侧）
   │ prefill_attention_paged_into(ctx, q,k,v, ..., kv_buffer, layout, layer, plan, ...)
   ▼
kernels kernel：按 plan 的页表逐页跳读 kv_buffer
```

#### 4.3.3 源码精读

**（1）`KvDesc` 的定义。**

[pegainfer-core/src/kv_pool.rs:L423-L430](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/kv_pool.rs#L423-L430) —— 文档注释点明它是「kernel 面向的请求分页 KV 元数据」：捆绑池几何之外的全部请求级状态（页表 + 末页长度）；结构体仅两个私有字段：`pages: &'a [PageId]`（借用，零拷贝）与 `last_page_len: usize`；

[pegainfer-core/src/kv_pool.rs:L432-L446](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/kv_pool.rs#L432-L446) —— 两个读取器：`last_page_len()` 公开；`page_indices()` 仅 `pub(crate)`——**页号不外泄给模型 crate**，只有 core 内部（正是 paged_plan.rs）能翻译它，这是把「状态翻译」职责钉死在 core 的手段。

**（2）两个构造入口。**

[pegainfer-core/src/kv_pool.rs:L390-L395](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/kv_pool.rs#L390-L395) —— `desc()`：直接用当前 `seq_len`（`last_page_len()` 的实现在 [L315-L322](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/kv_pool.rs#L315-L322)：`token_count % page_size`，整除时取 `page_size`、零长度取 0）；

[pegainfer-core/src/kv_pool.rs:L397-L408](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/kv_pool.rs#L397-L408) —— `desc_for_len(seq_len)`：多出的 `ensure!(held == needed)` 强校验「持有页数与假想长度严丝合缝」——页多说明状态超前、页少说明容量没给够，两者都会让 kernel 读到错位数据，所以宁可当场报错。

**（3）几何翻译。**

[pegainfer-core/src/kv_pool.rs:L102-L114](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/kv_pool.rs#L102-L114) —— `KvLayout::kernel_layout()`：core 的 `KvLayout`（[L15-L33](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/kv_pool.rs#L15-L33)）字段逐一抄进 kernels 的 `PagedKvLayout`——三个注意力入口第 41/78/127 行传的都是它，这是除 `KvDesc` 之外的另一条「描述翻译」管道：**请求级走 desc，池级走 layout**。

**（4）生产链实证。**

[pegainfer-gemma4/src/serve.rs:L1592-L1617](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-gemma4/src/serve.rs#L1592-L1617) —— 一步到位的完整样本：`kv.global.desc_for_len(kv_len)`（L1592，kv_len = start_pos + seq_len，即「推进后长度」）→ `PrefillPagedPlan::new(ctx, &global_desc, start_pos, seq_len, ...)`（L1609）——4.3.2 流程图的每一环都有真实行号背书。

#### 4.3.4 代码实践：写一份「desc ↔ plan」对照笔记（本讲主实践）

1. **实践目标**：把「core 描述状态、kernels 消费状态」的契约固化成一张可复查的对照表，并用一道设计题检验理解。
2. **操作步骤**：
   - 建一张两栏表。**左栏**：`KvDesc<'a>` 的字段及语义（`pages`、`last_page_len`），外加两个构造入口 `desc()` / `desc_for_len()` 的差异（校验有无、seq_len 来源）；**右栏**：`PrefillPagedPlan` 的四条构造入口与暴露的设备缓冲清单、`SplitKvCsr` 的四个字段与唯一构造函数；
   - 每一格标注证据行号（本讲引用过的链接即可）；
   - 在表下回答设计题（见第 5 节综合实践，先自己写再看参考答案）。
3. **需要观察的现象**：左栏只有 2 个字段，右栏却有 8+ 个设备缓冲——「请求状态」到「批次调度」之间，多出来的信息全部是**批次上下文**（行偏移、槽位映射、tile 划分、位置），没有一样来自单个请求自身。
4. **预期结果**：一张对照表 + 一段设计题回答。核心结论应能用自己的话复述：`KvDesc` 是单请求、宿主侧、最小充分；plan/CSR 是批次级、面向 kernel 的调度投影。

#### 4.3.5 小练习与答案

**练习 1**：`KvDesc.pages` 为什么是 `&'a [PageId]` 借用而不是 `Vec<PageId>` 拷贝？
**答案**：`desc()` 是每步前向都会调的热路径，借用零拷贝、零分配；页表的真身由 `KvState` 的 RAII permit 持有，生存期由 `'a` 绑定，不会悬空。拷贝版反而制造了「两份页表不同步」的可能。

**练习 2**：把 `desc_for_len` 的 `ensure!(held == needed)` 校验去掉，最坏会发生什么？
**答案**：页数多于所需时 kernel 会把多余页也当有效历史读（读到上一个使用这些页的请求残留数据——页是复用的）；页数少于所需时 kernel 会按页表读到未分配/他人的页。两种都是静默数据污染，比 panic 恶劣得多，所以这行校验是正确性护栏。

**练习 3**：为什么 `page_indices()` 是 `pub(crate)` 而 `last_page_len()` 是 `pub`？
**答案**：末页长度只是个数字，公开无妨；页号序列一旦外泄，模型 crate 就可能绕过 core 直接拼页表、与 `KvState` 的生命周期管理脱钩（页归还后页号悬空）。`pub(crate)` 把「页号 → i32」的翻译权独占给 paged_plan.rs，契约就锁死在 core 内部。

## 5. 综合实践

**设计题：不迁移任何 KV 数据，把两个请求 A、B 的注意力合并成一个批次跑，plan 和 desc 各需要补充什么信息？**

参考分析（先自己做，再对照）：

- **前提**：A、B 必须同池（同 `kv_buffer`、同 `KvLayout`）。`KvState::belongs_to`（[kv_pool.rs:L349-L353](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/kv_pool.rs#L349-L353)）就是为这种检查而存在的——页号只在签发它的池里有意义。
- **desc 侧缺什么**：`KvDesc` 是单请求视角，只有「页表 + 末页长度」，**没有请求边界**。合并需要把两个 desc 变成批次视图：每请求各自的页表行、每请求各自的 `last_page_len`（注意：合并后只有 B 的末页长度是「全局最后一页」，A 的末页长度仍必须保留，否则 A 在页边界处的有效长度会算错）。
- **plan 侧缺什么**：需要批次级的三类索引——`page_indptr`（每请求页表在扁平页表中的起点）、`q_indptr`/query 行到请求的归属（prefill 时每请求行数不同）、`positions`/`start_pos` 数组（每请求的因果位置不同）。**这正是 `from_raw_batch_with_cta_tile_q` 的入参形态**（`page_indices: &[Vec<i32>]` 每请求一行 + `last_page_lens`/`start_positions`/`seq_lens` 三个数组）——这个构造入口的存在本身就是这道题的官方答案。
- **split-KV CSR 侧**：天然不用改——`build_split_kv_csr` 的输入本来就是 `kv_lens` 数组，把 `[len_A, len_B]` 传进去，`request_indices` 自动区分两个请求的 chunk 归属，`o_indptr` 长度 = 3。
- **结论**：合并的成本全部在「描述层」（页表按行拼接 + 增加边界数组），KV 数据一字节都不动——这就是计划与数据分离的直接红利。

## 6. 本讲小结

- `ops/attention.rs` 提供三个入口：整段 prefill（norm+RoPE+写 KV+注意力一体）、合批 decode、split-KV decode；封装本身只做一件事——把 `KvLayout` 翻译成 `PagedKvLayout` 后转发 kernels。
- 计划与 KV 数据分离的四条理由：FFI 只认扁平数组、同一数据多种读法、状态与调度变化频率不同、图稳定复用需要固定缓冲。
- `build_split_kv_csr` 是纯宿主侧计算：\( \text{chunks}(r) = \max(1, \lceil \text{kv\_len}_r / C \rceil) \)，槽位补齐到 `padded_bs × cap`，填充槽 `mask=0` 且内容构造性确定。
- `PrefillPagedPlan` 是 kernels 类型的 newtype：`new` 吃 `KvDesc`（单请求）、`from_raw_batch_with_cta_tile_q` 吃裸页表行（批次）、`new_preallocated` + `update_batch_with_cta_tile_q` 实现指针不变的图稳定复用；`Deref` 让它直传 kernels 函数。
- `KvDesc<'a>` 是「最小充分信息」：`pages`（借用、`pub(crate)` 翻译权锁在 core）+ `last_page_len`；`desc()` 照实汇报、`desc_for_len()` 按假想长度并强校验页数吻合。
- 契约一句话：**core 描述状态（KvDesc/KvLayout），kernels 消费状态（i32 页表 + PagedKvLayout）**；生产链 `KvState::desc_for_len → PrefillPagedPlan::new → prefill_attention_paged_into` 在 gemma4 serve.rs 有完整实证。

## 7. 下一步学习建议

- **u3-l1 / u3-l2（下一站）**：`KvDesc` 的另一半故事——页从哪来、怎么归还？`page_pool.rs` 的 RAII permit 与 `kv_pool.rs` 的 page-first 布局、两段式 admission 是本讲所有「状态」的地基。
- **u4-l1**：CUDA Graph 精读。本讲 `new_preallocated`/`update_batch_with_cta_tile_q` 的「指针不变」纪律在那里与图捕获、padding 页会合成一个完整的内存纪律故事。
- **横向延伸（kernels crate）**：注意力 kernel 的真身在 [pegainfer-kernels/src/ops/attention.rs](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/src/ops/attention.rs#L604-L622)，split-KV 的归并数学与 `PagedKvLayout` 的 stride 推导（`paged_kv::derive_strides`）都在那一侧。
- **u4-l3**：换成消费者视角，看 qwen3 的统一前向如何把本讲的三个入口、`call_spec`（u2-l3）与 CUDA Graph 串成一次完整的请求生命周期。
